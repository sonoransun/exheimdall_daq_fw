"""
    HeIMDALL DAQ Firmware
    Python based shared memory interface implementations

    Author: Tamás Pető
    License: GNU GPL V3

   This program is free software: you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation, either version 3 of the License, or
   any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import atexit
import logging
import select
from struct import pack, unpack
from multiprocessing import shared_memory
import numpy as np
import os

A_BUFF_READY =   1
B_BUFF_READY =   2
INIT_READY   =  10
TERMINATE    = 255

class outShmemIface():

    def __init__(self, shmem_name, shmem_size, drop_mode=False,
                 instance_id=0, unlink_on_exit=True):

        self.init_ok = True
        self.logger = logging.getLogger(__name__)
        self.ignore_frame_drop_warning = True
        self.drop_mode = drop_mode
        self.dropped_frame_cntr = 0
        self._unlink_on_exit = unlink_on_exit
        self._destroyed = False

        original_name = shmem_name
        if instance_id != 0:
            shmem_name = f"inst{instance_id}_{shmem_name}"

        self.shmem_name = shmem_name
        self.buffer_free = [True, True]

        self.memories = []
        self.buffers = []

        # Clean up stale shared memories if they exist
        for suffix in ('_A', '_B'):
            try:
                stale = shared_memory.SharedMemory(
                    name=shmem_name + suffix, create=False, size=shmem_size)
                stale.close()
                if self._unlink_on_exit:
                    stale.unlink()
            except FileNotFoundError:
                pass

        self.memories.append(shared_memory.SharedMemory(
            name=shmem_name + '_A', create=True, size=shmem_size))
        self.memories.append(shared_memory.SharedMemory(
            name=shmem_name + '_B', create=True, size=shmem_size))
        self.buffers.append(np.ndarray(
            (shmem_size,), dtype=np.uint8, buffer=self.memories[0].buf))
        self.buffers.append(np.ndarray(
            (shmem_size,), dtype=np.uint8, buffer=self.memories[1].buf))

        fifo_prefix = '_data_control/'
        if instance_id != 0:
            fifo_prefix += f'inst{instance_id}_'
        if self.drop_mode:
            bw_fifo_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
        else:
            bw_fifo_flags = os.O_RDONLY | os.O_CLOEXEC
        try:
            self.fw_ctr_fifo = os.open(
                fifo_prefix + 'fw_' + original_name,
                os.O_WRONLY | os.O_CLOEXEC)
            self.bw_ctr_fifo = os.open(
                fifo_prefix + 'bw_' + original_name, bw_fifo_flags)
        except OSError as err:
            self.logger.critical("OS error: %s", err)
            self.logger.critical("Failed to open control fifos")
            self.bw_ctr_fifo = None
            self.fw_ctr_fifo = None
            self.init_ok = False

        if self.init_ok:
            os.write(self.fw_ctr_fifo, pack('B', INIT_READY))

        atexit.register(self.destory_sm_buffer)

    def send_ctr_buff_ready(self, active_buffer_index):
        if active_buffer_index == 0:
            os.write(self.fw_ctr_fifo, pack('B', A_BUFF_READY))
        elif active_buffer_index == 1:
            os.write(self.fw_ctr_fifo, pack('B', B_BUFF_READY))

        self.buffer_free[active_buffer_index] = False

    def send_ctr_terminate(self):
        os.write(self.fw_ctr_fifo, pack('B', TERMINATE))
        self.logger.info("Terminate signal sent")

    def destory_sm_buffer(self):
        if self._destroyed:
            return
        self._destroyed = True
        for memory in self.memories:
            memory.close()
            if self._unlink_on_exit:
                try:
                    memory.unlink()
                except FileNotFoundError:
                    pass

        if self.fw_ctr_fifo is not None:
            os.close(self.fw_ctr_fifo)
        if self.bw_ctr_fifo is not None:
            os.close(self.bw_ctr_fifo)

    def wait_buff_free(self):
        if self.buffer_free[0]:
            return 0
        elif self.buffer_free[1]:
            return 1
        else:
            try:
                buffer = os.read(self.bw_ctr_fifo, 1)
                signal = unpack('B', buffer)[0]

                if signal == A_BUFF_READY:
                    self.buffer_free[0] = True
                    return 0
                if signal == B_BUFF_READY:
                    self.buffer_free[1] = True
                    return 1
            except BlockingIOError:
                self.dropped_frame_cntr += 1
                if not self.ignore_frame_drop_warning:
                    self.logger.warning(
                        "Dropping frame.. Total: [%d]",
                        self.dropped_frame_cntr)
            return 3
        return -1


class inShmemIface():

    def __init__(self, shmem_name, instance_id=0, read_timeout_ms=0):

        self.init_ok = True
        self.logger = logging.getLogger(__name__)
        self.drop_mode = False
        self._read_timeout_ms = read_timeout_ms
        self._destroyed = False

        original_name = shmem_name
        if instance_id != 0:
            shmem_name = f"inst{instance_id}_{shmem_name}"

        self.shmem_name = shmem_name

        self.memories = []
        self.buffers = []

        fifo_prefix = '_data_control/'
        if instance_id != 0:
            fifo_prefix += f'inst{instance_id}_'
        try:
            self.fw_ctr_fifo = os.open(
                fifo_prefix + 'fw_' + original_name,
                os.O_RDONLY | os.O_CLOEXEC)
            self.bw_ctr_fifo = os.open(
                fifo_prefix + 'bw_' + original_name,
                os.O_WRONLY | os.O_CLOEXEC)
        except OSError as err:
            self.logger.critical("OS error: %s", err)
            self.logger.critical("Failed to open control fifos")
            self.bw_ctr_fifo = None
            self.fw_ctr_fifo = None
            self.init_ok = False

        if self.fw_ctr_fifo is not None:
            if unpack('B', os.read(self.fw_ctr_fifo, 1))[0] == INIT_READY:
                self.memories.append(
                    shared_memory.SharedMemory(name=shmem_name + '_A'))
                self.memories.append(
                    shared_memory.SharedMemory(name=shmem_name + '_B'))
                self.buffers.append(np.ndarray(
                    (self.memories[0].size,),
                    dtype=np.uint8, buffer=self.memories[0].buf))
                self.buffers.append(np.ndarray(
                    (self.memories[1].size,),
                    dtype=np.uint8, buffer=self.memories[1].buf))
            else:
                self.init_ok = False

        atexit.register(self.destory_sm_buffer)

    def send_ctr_buff_ready(self, active_buffer_index):
        if active_buffer_index == 0:
            os.write(self.bw_ctr_fifo, pack('B', A_BUFF_READY))
        elif active_buffer_index == 1:
            os.write(self.bw_ctr_fifo, pack('B', B_BUFF_READY))

    def destory_sm_buffer(self):
        if self._destroyed:
            return
        self._destroyed = True
        for memory in self.memories:
            memory.close()

        if self.fw_ctr_fifo is not None:
            os.close(self.fw_ctr_fifo)
        if self.bw_ctr_fifo is not None:
            os.close(self.bw_ctr_fifo)

    def wait_buff_free(self):
        if self._read_timeout_ms > 0:
            ready, _, _ = select.select(
                [self.fw_ctr_fifo], [], [],
                self._read_timeout_ms / 1000.0)
            if not ready:
                self.logger.warning("SHM read timeout (%d ms)", self._read_timeout_ms)
                return -2

        signal = unpack('B', os.read(self.fw_ctr_fifo, 1))[0]
        if signal == A_BUFF_READY:
            return 0
        elif signal == B_BUFF_READY:
            return 1
        elif signal == TERMINATE:
            return TERMINATE
        return -1
