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
sys.path.insert(0, currentPath)
from iq_header import IQHeader
import iq_capture_format as hcap

class IQRecorder():

    # Module-level defaults (kept so the historic no-argument invocation
    # behaves exactly as before)
    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 5000
    DEFAULT_PORT_STRIDE = 100
    DEFAULT_FRAMES = 200
    DEFAULT_MIN_BLOCK_INDEX = 300
    DEFAULT_PREFIX = "VEGAM"
    DEFAULT_TEST_CASE = "phase_cont"
    DEFAULT_CTRL_FILE = "_data_control/rec_ctr"
    DEFAULT_OUT_DIR = "_testing"
    # Receive slack appended to every .iqf payload (kept: .iqf files have
    # always carried it and offline tools read only the header-given length)
    IQF_RX_SLACK = 2**18

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 frames=DEFAULT_FRAMES, min_block_index=DEFAULT_MIN_BLOCK_INDEX,
                 prefix=DEFAULT_PREFIX, test_case=DEFAULT_TEST_CASE,
                 ctrl_file=DEFAULT_CTRL_FILE, out_dir=DEFAULT_OUT_DIR,
                 hcap_path=None, hcap_meta=None):
        """
            Initialzes the IQ Recorder module

            hcap_path: write the recorded frames into this .hcap capture
                       (post_sync_cf32) instead of one .iqf file per frame;
            hcap_meta: sidecar overrides (instance_id, config, notes, ...)
        """
        self.logger = logging.getLogger(__name__)
        logging.basicConfig(level=logging.INFO)
        """
            IQ Record control file is a text file
            '0' - Recording disabled (frames are received and dropped)
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

        self.iq_header = IQHeader()

        # Misc parameters
        self.fname_prefix = prefix
        self.out_dir = out_dir
        self.proc_index=-1

        self.logger.info("IQ Src Processing block initialized")
        self.no_frames_to_rec = frames # Number of frames to record
        self.rec_min_block_index = min_block_index # Minimum daq block index to start the recording
        self.frame_rec_cntr = 0
        self.test_case = test_case
        self.hcap_path = hcap_path
        self.hcap_meta = dict(hcap_meta or {})
        self.hcap_meta.setdefault("source", "tcp://{}:{:d}".format(host, port))
        self.hcap_writer = None

    def start_process(self):
        """
            Connects, performs the 'streaming' handshake and records until
            the frame budget is reached, the control file says '2' or the
            stream fails. Returns 0 on a clean finish, -1 on an error.

            Compatible only with DAQ firmwares that has the IQ streaming mode option.
        """
        try:
            self.socket_inst.connect((self.rec_ip_addr, self.port))
            self.socket_inst.sendall(str.encode('streaming'))
            self.receive_iq_frame()
            self.receiver_connection_status = True
            self.logger.info("Connection established")
        except (OSError, hcap.HcapError) as err:
            self.logger.error("Failed to connect to the IQ server: %s", err)
            self.receiver_connection_status = False
            self.socket_inst.close()
            self._close_ctrl()
            return -1
        try:
            if self.hcap_path is not None:
                try:
                    self.hcap_writer = hcap.HcapWriter(self.hcap_path, hcap.KIND_POST_SYNC_CF32,
                                                       self.hcap_meta)
                except OSError as err:
                    self.logger.error("Cannot create capture %s: %s", self.hcap_path, err)
                    return -1
            return self.listen()
        finally:
            if self.hcap_writer is not None:
                self.hcap_writer.close()
                self.logger.info("%d frames written to %s", self.hcap_writer.frames, self.hcap_path)
            self.socket_inst.close()
            self._close_ctrl()

    def _close_ctrl(self):
        if self.rec_ctr_fd is not None:
            self.rec_ctr_fd.close()
            self.rec_ctr_fd = None

    def _ctrl_char(self):
        if self.rec_ctr_fd is None:
            return ''
        self.rec_ctr_fd.seek(0, 0)
        return self.rec_ctr_fd.read(1)

    def _triggered(self):
        """Whether the current frame (self.iq_header) is recorded by the
        selected trigger mode."""
        hdr = self.iq_header
        if self.test_case == "all":
            return True
        if self.test_case == "decimator":
            return hdr.delay_sync_flag == 1 and hdr.iq_sync_flag == 1 and \
                hdr.frame_type == 0 and hdr.daq_block_index > self.rec_min_block_index
        if self.test_case == "calibration":
            return hdr.frame_type == 3
        # phase_cont
        return hdr.delay_sync_flag == 1 and hdr.frame_type == 0 and \
            hdr.daq_block_index > self.rec_min_block_index

    def listen(self):

        self.logger.info("Starting IQ acquisition loop")

        while self.frame_rec_cntr < self.no_frames_to_rec:

            self.proc_index+=1

            # Check Rec control character
            ctr_char = self._ctrl_char()
            if ctr_char == '2':
                self.logger.info("Exiting")
                break

            try:
                self.socket_inst.sendall(str.encode("IQDownload")) # Send iq request command
                header_bytes, payload = self.receive_iq_frame()
            except (OSError, hcap.HcapError) as err:
                self.logger.error("Unable to get new IQ samples (%s), exiting..", err)
                return -1

            en_save_iq = ctr_char != '0' and self._triggered()
            if en_save_iq and self.test_case == "calibration":
                self.logger.info("Trigger: Callibration frame received")

            # Save iq samples
            if en_save_iq:
                self.logger.info("Recording,  CPI index: {:d}".format(self.iq_header.cpi_index))
                try:
                    if self.hcap_writer is not None:
                        self.hcap_writer.write(header_bytes, payload)
                    else:
                        fname = os.path.join(self.out_dir, self.fname_prefix+"_"+str(self.frame_rec_cntr)+".iqf")
                        with open(fname, "wb") as iq_file_descr:
                            iq_file_descr.write(header_bytes)
                            iq_file_descr.write(payload)
                            iq_file_descr.write(bytes(self.IQF_RX_SLACK))
                except (OSError, hcap.HcapError) as err:
                    self.logger.error("Unable to save the IQ frame (%s), exiting..", err)
                    return -1
                self.frame_rec_cntr +=1
            else:
                self.logger.info("IQ Frame received and dropped, CPI index: {:d}".format(self.iq_header.cpi_index))

            # Check overdrive
            if self.iq_header.adc_overdrive_flags != 0:
                self.logger.warning("Overdrive detected!")

        if self.frame_rec_cntr >= self.no_frames_to_rec:
            self.logger.info("Recorded %d frames", self.frame_rec_cntr)
        return 0

    def receive_iq_frame(self):
        """
            Receives one IQ frame over the Ethernet connection and decodes
            its header into self.iq_header. Returns (header_bytes, payload)
            with the exact payload; ConnectionError when the server closes
            the connection, hcap.HcapError on a bad sync word.
        """
        header_bytes, header, payload = hcap.recv_wire_frame(self.socket_inst)
        self.iq_header = header
        return header_bytes, payload

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Record IQ frames from the port-5000 streaming server "
                    "('streaming'/'IQDownload' stop-and-wait protocol)")
    parser.add_argument("--host", default=IQRecorder.DEFAULT_HOST,
                        help="IQ server address (default: %(default)s)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--port", type=int, default=None,
                        help="IQ server TCP port (default: {:d})".format(IQRecorder.DEFAULT_PORT))
    target.add_argument("--instance", type=int, default=None, metavar="N",
                        help="federation instance: port 5000 + N * --port-stride")
    parser.add_argument("--port-stride", type=int, default=IQRecorder.DEFAULT_PORT_STRIDE,
                        metavar="S", help="port stride for --instance (default: %(default)s)")
    parser.add_argument("--frames", type=int,
                        default=IQRecorder.DEFAULT_FRAMES,
                        help="number of frames to record, in every trigger mode; "
                             "the recorder exits when they are recorded (default: %(default)s)")
    parser.add_argument("--min-block-index", type=int,
                        default=IQRecorder.DEFAULT_MIN_BLOCK_INDEX,
                        help="minimum daq_block_index before recording starts "
                             "(default: %(default)s)")
    parser.add_argument("--prefix", default=IQRecorder.DEFAULT_PREFIX,
                        help="output file name prefix (default: %(default)s)")
    parser.add_argument("--test-case", default=IQRecorder.DEFAULT_TEST_CASE,
                        choices=["decimator", "calibration", "phase_cont", "all"],
                        help="recording trigger mode; 'all' records every frame, "
                             "whatever its type and sync flags (default: %(default)s)")
    parser.add_argument("--ctrl-file", default=IQRecorder.DEFAULT_CTRL_FILE,
                        help="record-control file path (default: %(default)s)")
    parser.add_argument("--out-dir", default=IQRecorder.DEFAULT_OUT_DIR,
                        help="output directory for .iqf files "
                             "(default: %(default)s)")
    parser.add_argument("--hcap", default=None, metavar="PATH",
                        help="write the recorded frames into the .hcap capture PATH "
                             "(+ PATH.json sidecar) instead of .iqf files")
    parser.add_argument("--config", default=None, metavar="INI",
                        help="with --hcap: embed this INI's full text in the sidecar")
    parser.add_argument("--notes", default=None, help="with --hcap: free-text sidecar notes")
    args = parser.parse_args(argv)

    if args.instance is not None:
        if args.instance < 0 or args.port_stride < 1:
            parser.error("--instance must be >= 0 and --port-stride >= 1")
        port = IQRecorder.DEFAULT_PORT + args.instance * args.port_stride
    else:
        port = IQRecorder.DEFAULT_PORT if args.port is None else args.port
    if args.frames < 1:
        parser.error("--frames must be >= 1")
    meta = None
    if args.hcap is not None:
        try:
            meta = {"instance_id": args.instance, "notes": args.notes,
                    "config": hcap.config_text(args.config)}
        except OSError as err:
            parser.error("cannot read --config: {}".format(err))

    recorder = IQRecorder(host=args.host, port=port, frames=args.frames,
                          min_block_index=args.min_block_index,
                          prefix=args.prefix, test_case=args.test_case,
                          ctrl_file=args.ctrl_file, out_dir=args.out_dir,
                          hcap_path=args.hcap, hcap_meta=meta)
    return 0 if recorder.start_process() == 0 else 1


if __name__ == "__main__":
    # Importable module now; the connection is only opened when executed
    # (the historic behavior of connecting at import time is gone)
    sys.exit(main())
