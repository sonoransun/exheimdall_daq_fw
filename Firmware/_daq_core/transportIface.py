"""
   Transport Abstraction Layer for HeIMDALL DAQ Firmware.

   Provides pluggable transport backends as drop-in replacements for
   outShmemIface and inShmemIface. Supported backends:
     - shm   : POSIX shared memory (default, delegates to shmemIface)
     - spi   : SPI transport via C extension (stub)
     - net   : Network transport via TCP/ZMQ (stub)
     - usb3  : USB 3.0 bulk transport (stub)
     - pcie  : PCIe DMA transport (stub)

   Project: HeIMDALL DAQ Firmware
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
import logging
import numpy as np

from shmemIface import outShmemIface, inShmemIface

# ---------------------------------------------------------------------------
# Stub transport backends -- these will be backed by C shared libraries later
# ---------------------------------------------------------------------------

class _StubProducerBase:
    """Base for transport producers that are not yet implemented."""

    _TRANSPORT_LABEL = "unknown"

    def __init__(self, shmem_name, shmem_size, drop_mode=False, instance_id=0,
                 **kwargs):
        raise NotImplementedError(
            "{} transport producer requires the native C extension library "
            "(lib{}_transport.so). Build and install it first.".format(
                self._TRANSPORT_LABEL, self._TRANSPORT_LABEL.lower()))


class _StubConsumerBase:
    """Base for transport consumers that are not yet implemented."""

    _TRANSPORT_LABEL = "unknown"

    def __init__(self, shmem_name, instance_id=0, **kwargs):
        raise NotImplementedError(
            "{} transport consumer requires the native C extension library "
            "(lib{}_transport.so). Build and install it first.".format(
                self._TRANSPORT_LABEL, self._TRANSPORT_LABEL.lower()))


class SPITransportProducer(_StubProducerBase):
    _TRANSPORT_LABEL = "SPI"

class SPITransportConsumer(_StubConsumerBase):
    _TRANSPORT_LABEL = "SPI"

class NetTransportProducer(_StubProducerBase):
    _TRANSPORT_LABEL = "Net"

class NetTransportConsumer(_StubConsumerBase):
    _TRANSPORT_LABEL = "Net"

class USB3TransportProducer(_StubProducerBase):
    _TRANSPORT_LABEL = "USB3"

class USB3TransportConsumer(_StubConsumerBase):
    _TRANSPORT_LABEL = "USB3"

class PCIeTransportProducer(_StubProducerBase):
    _TRANSPORT_LABEL = "PCIe"

class PCIeTransportConsumer(_StubConsumerBase):
    _TRANSPORT_LABEL = "PCIe"


# ---------------------------------------------------------------------------
# Dispatch map
# ---------------------------------------------------------------------------

_PRODUCER_BACKENDS = {
    'shm':  outShmemIface,
    'spi':  SPITransportProducer,
    'net':  NetTransportProducer,
    'usb3': USB3TransportProducer,
    'pcie': PCIeTransportProducer,
}

_CONSUMER_BACKENDS = {
    'shm':  inShmemIface,
    'spi':  SPITransportConsumer,
    'net':  NetTransportConsumer,
    'usb3': USB3TransportConsumer,
    'pcie': PCIeTransportConsumer,
}

# ---------------------------------------------------------------------------
# Public transport wrappers
# ---------------------------------------------------------------------------

class TransportProducer:
    """Drop-in replacement for outShmemIface with pluggable transport backend.

    All public attributes and methods of outShmemIface are forwarded to the
    chosen backend so that existing callers need no modification.
    """

    def __init__(self, shmem_name, shmem_size, drop_mode=False, instance_id=0,
                 transport_type='shm'):
        self.logger = logging.getLogger(__name__)
        self.transport_type = transport_type

        backend_cls = _PRODUCER_BACKENDS.get(transport_type)
        if backend_cls is None:
            raise ValueError("Unknown transport type '{}'. Available: {}".format(
                transport_type, list(_PRODUCER_BACKENDS.keys())))

        self.logger.info("Initializing producer transport: %s (%s)",
                         transport_type, backend_cls.__name__)
        # Unavailable backends must fail through init_ok (the documented
        # failure path checked by every caller), not with a traceback.
        self._backend = None
        try:
            self._backend = backend_cls(shmem_name, shmem_size,
                                        drop_mode=drop_mode,
                                        instance_id=instance_id)
        except NotImplementedError as e:
            self.logger.critical("Producer transport '%s' unavailable: %s",
                                 transport_type, e)

    # -- forwarded properties ------------------------------------------------

    @property
    def init_ok(self):
        return self._backend is not None and self._backend.init_ok

    @init_ok.setter
    def init_ok(self, value):
        if self._backend is not None:
            self._backend.init_ok = value

    @property
    def buffers(self):
        return self._backend.buffers if self._backend is not None else []

    @property
    def memories(self):
        return self._backend.memories if self._backend is not None else []

    # -- forwarded methods ---------------------------------------------------

    def send_ctr_buff_ready(self, active_buffer_index):
        if self._backend is None:
            return None
        return self._backend.send_ctr_buff_ready(active_buffer_index)

    def send_ctr_terminate(self):
        if self._backend is None:
            return None
        return self._backend.send_ctr_terminate()

    def destory_sm_buffer(self):
        """Note: name preserved from outShmemIface for backward compatibility."""
        if self._backend is None:
            return None
        return self._backend.destory_sm_buffer()

    def wait_buff_free(self):
        if self._backend is None:
            return -1
        return self._backend.wait_buff_free()


class TransportConsumer:
    """Drop-in replacement for inShmemIface with pluggable transport backend.

    All public attributes and methods of inShmemIface are forwarded to the
    chosen backend so that existing callers need no modification.
    """

    def __init__(self, shmem_name, instance_id=0, transport_type='shm',
                 read_timeout_ms=0):
        self.logger = logging.getLogger(__name__)
        self.transport_type = transport_type

        backend_cls = _CONSUMER_BACKENDS.get(transport_type)
        if backend_cls is None:
            raise ValueError("Unknown transport type '{}'. Available: {}".format(
                transport_type, list(_CONSUMER_BACKENDS.keys())))

        self.logger.info("Initializing consumer transport: %s (%s)",
                         transport_type, backend_cls.__name__)
        # Unavailable backends must fail through init_ok (the documented
        # failure path checked by every caller), not with a traceback.
        self._backend = None
        try:
            self._backend = backend_cls(shmem_name, instance_id=instance_id,
                                        read_timeout_ms=read_timeout_ms)
        except NotImplementedError as e:
            self.logger.critical("Consumer transport '%s' unavailable: %s",
                                 transport_type, e)

    # -- forwarded properties ------------------------------------------------

    @property
    def init_ok(self):
        return self._backend is not None and self._backend.init_ok

    @init_ok.setter
    def init_ok(self, value):
        if self._backend is not None:
            self._backend.init_ok = value

    @property
    def buffers(self):
        return self._backend.buffers if self._backend is not None else []

    @property
    def memories(self):
        return self._backend.memories if self._backend is not None else []

    # -- forwarded methods ---------------------------------------------------

    def send_ctr_buff_ready(self, active_buffer_index):
        if self._backend is None:
            return None
        return self._backend.send_ctr_buff_ready(active_buffer_index)

    def destory_sm_buffer(self):
        """Note: name preserved from inShmemIface for backward compatibility."""
        if self._backend is None:
            return None
        return self._backend.destory_sm_buffer()

    def wait_buff_free(self):
        if self._backend is None:
            return -1
        return self._backend.wait_buff_free()
