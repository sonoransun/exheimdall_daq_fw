"""
	Description :
	IQ framer recorder module
	
	Project : HeIMDALL DAQ Firmware
	License : GNU GPL V3
	Author  : Tamas Peto
	Copyright (C) 2018-2020  Tamás Pető
	
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
import os
import sys
import logging
import socket
# Import IQ header module
currentPath = os.path.dirname(os.path.realpath(__file__))
rootPath = os.path.dirname(currentPath)
sys.path.insert(0, os.path.join(rootPath, "_daq_core"))
from iq_header import IQHeader

class IQRecorder():

    # Module-level defaults (kept so the historic no-argument invocation
    # behaves exactly as before)
    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 5000
    DEFAULT_FRAMES = 200
    DEFAULT_MIN_BLOCK_INDEX = 300
    DEFAULT_PREFIX = "VEGAM"
    DEFAULT_TEST_CASE = "phase_cont"
    DEFAULT_CTRL_FILE = "_data_control/rec_ctr"
    DEFAULT_OUT_DIR = "_testing"

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 frames=DEFAULT_FRAMES, min_block_index=DEFAULT_MIN_BLOCK_INDEX,
                 prefix=DEFAULT_PREFIX, test_case=DEFAULT_TEST_CASE,
                 ctrl_file=DEFAULT_CTRL_FILE, out_dir=DEFAULT_OUT_DIR):
        """
            Initialzes the IQ Recorder module
        """
        self.logger = logging.getLogger(__name__)
        logging.basicConfig(level=logging.INFO)
        """
            IQ Record control file is a text file
            '0' - Recording disabled
            '1' - Recording enabled
            '2' - Exit
        """
        self.rec_ctr_fd = None
        try:
            self.rec_ctr_fd = open(ctrl_file, 'r')
        except OSError:
            self.logger.error("Failed to open control file: %s", ctrl_file)


        self.receiver_connection_status = False
        self.port = port
        self.rec_ip_addr = host
        self.socket_inst = socket.socket()

        self.receiverBufferSize = 2 ** 18  # Size of the Ethernet receiver buffer measured in bytes
        self.channel_number = 4
        self.sample_number = 2**18
        self.iq_header = IQHeader()


        # Misc parameters
        self.en_save_iq = False # Enables saving iq samples
        self.fname_prefix = prefix
        self.out_dir = out_dir
        self.proc_index=-1

        self.logger.info("IQ Src Processing block initialized")
        self.no_frames_to_rec = frames # Number of frames to record
        self.rec_min_block_index = min_block_index # Minimum daq block index to start the recording
        self.frame_rec_cntr = 0
        self.rec_start_tigger = False
        self.test_case = test_case
        
    def start_process(self):                            
        # Establishing connection 
        """
            Compatible only with DAQ firmwares that has the IQ streaming mode option. 
            HeIMDALL-RTL DAQ Firmware version: --- or later
        """

        try:
            self.socket_inst.connect((self.rec_ip_addr, self.port))            
            self.socket_inst.sendall(str.encode('streaming'))                                
            test_iq=self.receive_iq_frame()
            self.receiver_connection_status = True
            self.logger.info("Connection established")
        except Exception:
            self.logger.exception("Failed to connect to the IQ server")
            self.receiver_connection_status = False
                
        self.listen()
        
    def listen(self):

        self.logger.info("Starting IQ acquisition loop")        

        while True:
            
            self.proc_index+=1            
            
            # Check Rec control character
            ctr_char = ''
            if self.rec_ctr_fd is not None:
                self.rec_ctr_fd.seek(0, 0)
                ctr_char = self.rec_ctr_fd.read(1)
            if ctr_char == '0':
                self.logger.debug("Recording disabled") 
                self.en_save_iq = 0
            elif ctr_char == '1':
                self.logger.debug("Recording enbaled") 
                self.en_save_iq = 1
            elif ctr_char == '2':
                self.logger.info("Exiting")
                break
                    
            try:
                self.socket_inst.sendall(str.encode("IQDownload")) # Send iq request command
                iq_frame_bytes = self.receive_iq_frame()
                
                self.en_save_iq = False
                
                if self.test_case == "decimator":
                   if self.iq_header.delay_sync_flag == 1 and self.iq_header.iq_sync_flag == 1 and \
                       self.iq_header.frame_type == 0 and self.iq_header.daq_block_index >self.rec_min_block_index:
                        if self.frame_rec_cntr < self.no_frames_to_rec:
                            self.en_save_iq = True
                
                elif self.test_case == "calibration":
                    if self.iq_header.frame_type == 3:             
                        self.en_save_iq = True
                        self.logger.info("Trigger: Callibration frame received")
                        
                elif self.test_case == "phase_cont":
                    if self.iq_header.delay_sync_flag == 1 and \
                       self.iq_header.frame_type == 0 and self.iq_header.daq_block_index >self.rec_min_block_index:
                        self.logger.info("Trigger: Start recording")
                        if self.frame_rec_cntr < self.no_frames_to_rec:
                            self.en_save_iq = True
                """                
                if self.iq_header.delay_sync_flag == 1 and self.iq_header.iq_sync_flag == 1 and \
                   self.iq_header.frame_type == 0:
                       
                    self.rec_start_tigger = True
                    self.logger.info("Trigger: Sample and IQ sync achieved")

                # To Save Calibration frames
                if self.iq_header.frame_type == 3:
                    self.en_save_iq = True
                """    
                # Used to record multiple frames automatically
                #if self.frame_rec_cntr < self.no_frames_to_rec and self.rec_start_tigger:
                #    self.en_save_iq = True
                        
                # Used to catch calibration frames
                #if self.iq_header.noise_source_state == 0:
                #    self.en_save_iq = False
            
                
                # Save iq samples
                if self.en_save_iq:        
                    self.logger.info("Recording,  CPI index: {:d}".format(self.iq_header.cpi_index))
                    iq_file_descr = open(os.path.join(self.out_dir, self.fname_prefix+"_"+str(self.frame_rec_cntr)+".iqf"), "wb")
                    iq_file_descr.write(iq_frame_bytes)
                    iq_file_descr.close()
                    self.frame_rec_cntr +=1
                else:
                    self.logger.info("IQ Frame received and dropped, CPI index: {:d}".format(self.iq_header.cpi_index))
                
                # Check overdrive
                if self.iq_header.adc_overdrive_flags != 0:
                    self.logger.warning("Overdrive detected!")

            except Exception:
                self.logger.exception("Unable to get new IQ samples, exiting..")
                return -1      
            
            
        return 0
    
    def receive_iq_frame(self):
        """
                Receives IQ samples over Ethernet connection
        """
        
        total_received_bytes = 0
        recv_bytes_count = 0
        iq_header_bytes = bytearray(self.iq_header.header_size)  # allocate array
        view = memoryview(iq_header_bytes)  # Get buffer
        
        self.logger.debug("Starting IQ header reception")
        
        while total_received_bytes < self.iq_header.header_size:
            # Receive into buffer
            recv_bytes_count = self.socket_inst.recv_into(view, self.iq_header.header_size)
            view = view[recv_bytes_count:]  # reset memory region
            total_received_bytes += recv_bytes_count
        
        self.iq_header.decode_header(iq_header_bytes)
        self.logger.debug("IQ header received and decoded")
        
        # Calculate total bytes to receive from the iq header data        
        total_bytes_to_receive = int((self.iq_header.cpi_length * self.iq_header.active_ant_chs * (2*self.iq_header.sample_bit_depth))/8)
        receiver_buffer_size = 2**18
        
        self.logger.debug("Total bytes to receive: {:d}".format(total_bytes_to_receive))
        
        total_received_bytes = 0
        recv_bytes_count = 0
        iq_data_bytes = bytearray(total_bytes_to_receive + receiver_buffer_size)  # allocate array
        view = memoryview(iq_data_bytes)  # Get buffer
        
        self.logger.debug("Starting IQ reception")
        
        while total_received_bytes < total_bytes_to_receive:
            # Receive into buffer
            recv_bytes_count = self.socket_inst.recv_into(view, receiver_buffer_size)
            view = view[recv_bytes_count:]  # reset memory region
            total_received_bytes += recv_bytes_count
        
        self.logger.debug("Succesfully received")
        
        # Dump IQ header        
        #self.iq_header.dump_header()
        iq_frame_bytes=bytearray()+iq_header_bytes+iq_data_bytes
        return iq_frame_bytes
        
def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Record IQ frames from the port-5000 streaming server "
                    "('streaming'/'IQDownload' stop-and-wait protocol)")
    parser.add_argument("--host", default=IQRecorder.DEFAULT_HOST,
                        help="IQ server address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=IQRecorder.DEFAULT_PORT,
                        help="IQ server TCP port (default: %(default)s)")
    parser.add_argument("--frames", type=int,
                        default=IQRecorder.DEFAULT_FRAMES,
                        help="number of frames to record (default: %(default)s)")
    parser.add_argument("--min-block-index", type=int,
                        default=IQRecorder.DEFAULT_MIN_BLOCK_INDEX,
                        help="minimum daq_block_index before recording starts "
                             "(default: %(default)s)")
    parser.add_argument("--prefix", default=IQRecorder.DEFAULT_PREFIX,
                        help="output file name prefix (default: %(default)s)")
    parser.add_argument("--test-case", default=IQRecorder.DEFAULT_TEST_CASE,
                        choices=["decimator", "calibration", "phase_cont"],
                        help="recording trigger mode (default: %(default)s)")
    parser.add_argument("--ctrl-file", default=IQRecorder.DEFAULT_CTRL_FILE,
                        help="record-control file path (default: %(default)s)")
    parser.add_argument("--out-dir", default=IQRecorder.DEFAULT_OUT_DIR,
                        help="output directory for .iqf files "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)

    recorder = IQRecorder(host=args.host, port=args.port, frames=args.frames,
                          min_block_index=args.min_block_index,
                          prefix=args.prefix, test_case=args.test_case,
                          ctrl_file=args.ctrl_file, out_dir=args.out_dir)
    recorder.start_process()


if __name__ == "__main__":
    # Importable module now; the connection is only opened when executed
    # (the historic behavior of connecting at import time is gone)
    main()
