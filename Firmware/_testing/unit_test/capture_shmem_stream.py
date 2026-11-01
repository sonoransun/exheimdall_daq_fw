"""
   HeIMDALL DAQ Firmware
   Description : Capture and record an IQ frame stream transfered via a shared memory interface
   Author      : Tamas Peto
   License     : GNU GPL V3
    
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

   WARNING: Check the native size of the IQ header on the target device

   Usage: python3 capture_shmem_stream.py SHMEM_NAME OUT_FILE EN_WRITE [KIND]
     OUT_FILE ending in .hcap writes an .hcap capture (iq_capture_format.py)
     of KIND post_sync_cf32 or raw_u8 (required for .hcap); any other name
     gets the plain [header][payload] concatenation, as before.
"""

import logging
import sys
from os.path import join, dirname, realpath
import time
import threading

current_path      = dirname(realpath(__file__))
root_path         = dirname(dirname(current_path))
daq_core_path     = join(root_path, "_daq_core")

sys.path.insert(0, daq_core_path)
sys.path.insert(0, dirname(current_path))
from iq_header import IQHeader
from shmemIface import inShmemIface
import iq_capture_format as hcap

class IQFrameRecorder(threading.Thread):
    
    def __init__(self, shmem_name, out_fname, en_write, hcap_kind=None, hcap_meta=None):
        threading.Thread.__init__(self)
        logging.basicConfig(level=logging.DEBUG)
        self.logger = logging.getLogger(__name__)        
        
        self.fw_ctr_gen_frame = None
        self.bw_ctr_gen_frame = None              
        
        self.iq_header = IQHeader()
        
        # Open record output file: an .hcap capture (with sidecar) when the
        # name ends in .hcap, else the plain frame concatenation
        self.output_file = None
        self.hcap_writer = None
        if out_fname.endswith(".hcap"):
            if hcap_kind not in hcap.KINDS:
                raise ValueError("an .hcap output needs a capture kind: {}".format(" or ".join(hcap.KINDS)))
            meta = {"source": "shmem:" + shmem_name}
            meta.update(hcap_meta or {})
            self.hcap_writer = hcap.HcapWriter(out_fname, hcap_kind, meta)
        else:
            self.output_file = open(out_fname,"wb")
        
        self.en_write = int(en_write)

        self.start_time = 0
        self.elapsed_time = 0
        # Open shared memory interface to capture the module output
        self.in_shmem_iface = inShmemIface(shmem_name)
        if not self.in_shmem_iface.init_ok:
            self.logger.critical("Shared memory initialization failed")
            self.in_shmem_iface.destory_sm_buffer()        
    
    def run(self):
        """
            Start the main processing loop
        """
        logging.info("Starting IQ frame capture on shared memory iface")
        while True:            
            #############################################
            #           OBTAIN NEW DATA BLOCK           #  
            #############################################
                        
            # Read and convert header            
            active_buff_index = self.in_shmem_iface.wait_buff_free()            
            if self.start_time == 0:
                self.start_time = time.time()
                
            if active_buff_index < 0 or active_buff_index > 1:
                logging.info("Terminating.., signal: {:d}".format(active_buff_index))                
                break;          
            
            buffer = self.in_shmem_iface.buffers[active_buff_index]
                       
            iq_header_bytes = buffer[0:1024].tobytes()
            try:
                self.iq_header = hcap.decode_header(iq_header_bytes)
            except hcap.HcapError as err:
                logging.error("IQ header check failed (%s), exiting..", err)
                break
            #self.iq_header.dump_header()            
            incoming_payload_size = self.iq_header.cpi_length*self.iq_header.active_ant_chs*2*int(self.iq_header.sample_bit_depth/8)                                                            
            # Raw bytes: a u8 payload is not necessarily a whole number of float32
            payload = buffer[1024:1024 + incoming_payload_size].tobytes() if incoming_payload_size > 0 else b""
            if self.en_write:
                if self.hcap_writer is not None:
                    try:
                        self.hcap_writer.write(iq_header_bytes, payload[:hcap.payload_length(self.iq_header)])
                    except (OSError, hcap.HcapError) as err:
                        # e.g. a capture KIND that does not match the stream
                        logging.error("Cannot record the IQ frame (%s), exiting..", err)
                        break
                else:
                    self.output_file.write(iq_header_bytes + payload)
            
            self.logger.info("IQ frame received. [{:d}]".format(self.iq_header.daq_block_index))
            self.in_shmem_iface.send_ctr_buff_ready(active_buff_index)
            #self.logger.info("Buff ready: {:d}".format(active_buff_index))
            
        self.elapsed_time = time.time()-self.start_time        
        self.in_shmem_iface.destory_sm_buffer()
        self.close_output()
        logging.info("Capture module exited")

    def close_output(self):
        """Close the output file / capture (idempotent)."""
        if self.hcap_writer is not None:
            self.hcap_writer.close()
        if self.output_file is not None:
            self.output_file.close()
        

if __name__ == '__main__':
    if len(sys.argv) not in (4, 5):
        print(__doc__.split("Usage:")[1].strip().splitlines()[0], file=sys.stderr)
        sys.exit(2)
    try:
        recorder = IQFrameRecorder(sys.argv[1], sys.argv[2], sys.argv[3],
                                   sys.argv[4] if len(sys.argv) == 5 else None)
    except ValueError as err:
        print("ERROR: {}".format(err), file=sys.stderr)
        sys.exit(2)
    if recorder.in_shmem_iface.init_ok:
        recorder.start()
    else:
        recorder.close_output()
        sys.exit(1)

    
    
