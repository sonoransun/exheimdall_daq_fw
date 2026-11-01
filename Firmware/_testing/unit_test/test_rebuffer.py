"""
	Description :
	Unit test for the rebuffer module

	Project : HeIMDALL DAQ Firmware
	License : GNU GPL V3
	Author  : Tamas Peto
  
	Copyright (C) 2018-2021  Tamás Pető
	
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
import unittest
from os.path import join, dirname, realpath
import os
import shutil
import sys
import subprocess
import tempfile
import logging
import numpy as np
from struct import pack
import time
import threading
from configparser import ConfigParser

current_path      = dirname(realpath(__file__))
root_path         = dirname(dirname(current_path))
daq_core_path     = join(root_path, "_daq_core")
log_path          = join(root_path, "_logs")
unit_test_path    = join(root_path, "_testing", "unit_test")
test_logs_path    = join(root_path, "_testing", "test_logs")

live_config_filename = join(root_path, 'daq_chain_config.ini')

# Import IQ header module
sys.path.insert(0, daq_core_path)
sys.path.insert(0, unit_test_path)
from iq_header import IQHeader
from capture_shmem_stream import IQFrameRecorder
from shmemIface import inShmemIface

class TesterRebufferModule(unittest.TestCase):

    """
        Runs inside a per-test temporary working directory holding a private
        COPY of daq_chain_config.ini plus the _data_control FIFOs, so the
        live developer config is never modified (rebuffer.out and the Python
        shmem interfaces resolve both relative to the cwd).
    """

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(filename=join(test_logs_path,'unit_test.log'),
                             level=logging.INFO)
        logging.info("--> Starting Rebuffer unit test <--")

    def setUp(self):
        # Isolated working directory with a private copy of the config
        self.workdir = tempfile.mkdtemp(prefix="rebuffer_test_")
        self.prev_cwd = os.getcwd()
        self.config_filename = join(self.workdir, 'daq_chain_config.ini')
        shutil.copyfile(live_config_filename, self.config_filename)
        data_control_path = join(self.workdir, "_data_control")
        os.makedirs(data_control_path)
        os.chdir(self.workdir)

        # Open log files
        self.fd_log_gen_err  = open(join(log_path,"gen.log"),  "w")
        self.fd_log_rebuffer_err = open(join(log_path,"rebuffer.log"), "w")

        # Set-up control FIFOs
        os.mkfifo(join(data_control_path,"fw_decimator_in"))
        os.mkfifo(join(data_control_path,"bw_decimator_in"))

        # Default config file parameters (from the private copy)
        self.N, self. R, self.N_daq, self.N_cal = self._read_config_file()

    def tearDown(self):
        # Close log files
        self.fd_log_gen_err.close()
        self.fd_log_rebuffer_err.close()

        # Remove test output file
        try:
            os.remove(join(unit_test_path,'rebuffer_test_0.dat'))
        except OSError:
            pass

        # Drop the isolated working directory (config copy + FIFOs)
        os.chdir(self.prev_cwd)
        shutil.rmtree(self.workdir, ignore_errors=True)

    #############################################
    #               TEST FUNCTIONS              #  
    #############################################
    #@unittest.skip("Skipped during development")
    def test_case_2_0(self):
        logging.info("-> Starting Test Case [2_0] :")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_DATA
        self._write_config_file(N=2**18, R=1, N_daq=2**18, N_cal=2**18)
        # -> Action <-
        self._run_std_frame_test(frame_count, frame_type)               
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'rebuffer_test_0.dat'), frame_count, frame_type))    
    def test_case_2_1(self):
        logging.info("-> Starting Test Case [2_1] :")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_DUMMY
        self._write_config_file(N=2**18, R=1, N_daq=2**18, N_cal=2**18)
        # -> Action <-
        self._run_std_frame_test(frame_count, frame_type)        
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'rebuffer_test_0.dat'), frame_count, frame_type))
    def test_case_2_2(self):
        logging.info("-> Starting Test Case [2_2] :")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_CAL
        self._write_config_file(N=2**18, R=1, N_daq=2**18, N_cal=2**18)
        # -> Action <-
        self._run_std_frame_test(frame_count, frame_type)
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'rebuffer_test_0.dat'), frame_count, frame_type))
    def test_case_2_3(self):
        logging.info("-> Starting Test Case [2_3] :")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_DATA
        N     = 2**19
        R     = 1
        N_daq = 2**18
        N_cal = 2**20
        self._write_config_file(N,R,N_daq,N_cal)
        output_frame_count = (N_daq*frame_count)//(N*R)
        # -> Action <-
        self._run_std_frame_test(frame_count, frame_type)
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'rebuffer_test_0.dat'), output_frame_count, frame_type))
    def test_case_2_4(self):
        logging.info("-> Starting Test Case [2_4] :")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_CAL
        N     = 31    # Should be irrelevant
        R     = 10    # Should be irrelevant
        N_daq = 2**18
        N_cal = 2**20
        self._write_config_file(N,R,N_daq,N_cal)
        output_frame_count = (N_daq*frame_count)//(N_cal)
        # -> Action <-
        self._run_std_frame_test(frame_count, frame_type)
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'rebuffer_test_0.dat'), output_frame_count, frame_type))
    def test_case_2_5(self):
        logging.info("-> Starting Test Case [2_5] :")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_DATA     
        N     = 101
        R     = 1
        N_daq = 37
        N_cal = 257 # Should irrelevant
        self._write_config_file(N,R,N_daq,N_cal)
        output_frame_count = (N_daq*frame_count)//(N*R)
        # -> Action <-
        self._run_std_frame_test(frame_count, frame_type,sample_size=N_daq)
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'rebuffer_test_0.dat'), output_frame_count , frame_type))
    def test_case_2_6(self):
        logging.info("-> Starting Test Case [2_6] :")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_DATA     
        N     = 2**18
        R     = 3
        N_daq = 2**18   
        N_cal = 257 # Should irrelevant        
        self._write_config_file(N,R,N_daq,N_cal)
        output_frame_count = (N_daq*frame_count)//(N*R)
        # -> Action <-
        self._run_std_frame_test(frame_count, frame_type)
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'rebuffer_test_0.dat'), output_frame_count , frame_type))
    def test_case_2_10(self):
        logging.info("-> Starting Test Case [2_10] : Ramp test")
        # -> Assume <-
        frame_count = 10
        N     = 2**18
        R     = 1
        N_daq = 2**18
        N_cal = 257 # Should irrelevant  
        frame_type  = IQHeader.FRAME_TYPE_DATA
        output_frame_count = (N_daq*frame_count)//(N*R)
        self._write_config_file(N, R, N_daq, N_cal)        
        # -> Action <-        
        self._run_ramp_test(frame_count, frame_type)        
        # -> Assert <-
        self.assertFalse(self.check_ramp(join(unit_test_path,'rebuffer_test_0.dat'),output_frame_count))
    def test_case_2_11(self):
        logging.info("-> Starting Test Case [2_11] :")
        # -> Assume <-
        frame_count = 10
        N     = 31    # Should irrelevant  
        R     = 4     # Should irrelevant  
        N_daq = 2**18
        N_cal = 2**18
        frame_type  = IQHeader.FRAME_TYPE_CAL
        output_frame_count = (N_daq*frame_count)//(N_cal)
        self._write_config_file(N, R, N_daq, N_cal)
        # -> Action <-        
        self._run_ramp_test(frame_count, frame_type)
        # -> Assert <-
        self.assertFalse(self.check_ramp(join(unit_test_path,'rebuffer_test_0.dat'),output_frame_count))
    def test_case_2_12(self):
        logging.info("-> Starting Test Case [2_12] :")        
        # -> Assume <-
        frame_count = 1000
        frame_type  = IQHeader.FRAME_TYPE_DATA
        N     = 101
        R     = 2
        N_daq = 37
        N_cal = 257 # Should irrelevant  
        output_frame_count = (N_daq*frame_count)//(N*R)
        self._write_config_file(N,R,N_daq,N_cal)
        # -> Action <-        
        self._run_ramp_test(frame_count, frame_type, sample_size=N_daq)
        # -> Assert <-
        self.assertFalse(self.check_ramp(join(unit_test_path,'rebuffer_test_0.dat'), output_frame_count))

    #############################################
    #              TEST RUN WRAPPERS            #  
    #############################################
    
    def _run_std_frame_test(self,frame_count, frame_type, sample_size=2**18):        
        # Build up and start test chain
        generator = subprocess.Popen(["python3",join(unit_test_path,"gen_std_frame.py"),
                                      "-t",str(frame_type),
                                      "-b",str(frame_count),
                                      "-n", str(sample_size)],
                                      stdout=subprocess.PIPE,
                                      stderr=self.fd_log_gen_err)
        gen_out, gen_err = generator.communicate()
        rebuffer_module = subprocess.Popen([join(daq_core_path,"rebuffer.out"), "0"], # 0-Drop mode disabled 
                                           stdin=subprocess.PIPE, 
                                           stdout=subprocess.DEVNULL, 
                                           stderr=self.fd_log_rebuffer_err)        
        recorder = IQFrameRecorder("decimator_in",
                                   join(unit_test_path,'rebuffer_test_0.dat'),
                                   "1")
        if recorder.in_shmem_iface.init_ok:
            recorder.start()
        else:
            self.assertTrue(0)
               
        rebuffer_module.communicate(gen_out)
        generator.wait()
        rebuffer_module.wait()
        recorder.join()
        

    def _run_ramp_test(self,frame_count, frame_type, sample_size=2**18):
        # Build up and start test chain
        generator = subprocess.Popen(["python3",join(unit_test_path,"gen_ramp.py"),
                                      "-t",str(frame_type),
                                      "-b",str(frame_count),
                                      "-n",str(sample_size)],
                                      stdout=subprocess.PIPE,
                                       stderr=self.fd_log_gen_err)
        gen_out, gen_err = generator.communicate()
        rebuffer_module = subprocess.Popen([join(daq_core_path,"rebuffer.out"), "0"], # 0-Drop mode disabled 
                                           stdin=subprocess.PIPE, 
                                           stdout=subprocess.DEVNULL, 
                                           stderr=self.fd_log_rebuffer_err)
        recorder = IQFrameRecorder("decimator_in",
                                   join(unit_test_path,'rebuffer_test_0.dat'),
                                   "1")
        if recorder.in_shmem_iface.init_ok:
            recorder.start()
        else:
            self.assertTrue(0)

        rebuffer_module.communicate(gen_out)
        recorder.join()        
    #############################################
    #         RESULT CHECKER FUNCTIONS          #  
    #############################################
    
    def check_frame(self, file_name, frame_count, frame_type):
        iq_header = IQHeader()
        blocks = 0
        with open(file_name, "rb") as file_descr:
            while True:                                
                """
                ------------------
                  IQ Frame Reading
                ------------------
                """
                try:
                    # Reading IQ header
                    iq_header_bytes = file_descr.read(1024)                                          
                    if len(iq_header_bytes) == 0: break                               
                    iq_header.decode_header(iq_header_bytes)                                                        
                    # Reading Multichannel IQ data
                    iq_data_length = int((iq_header.cpi_length * iq_header.active_ant_chs * (2*iq_header.sample_bit_depth))/8)                    
                    if iq_data_length > 0:
                        iq_data_bytes = file_descr.read(iq_data_length)                    
                        if len(iq_data_bytes) ==0: break                    
                    
                    if iq_header.check_sync_word() !=0:
                        logging.error("Sync word error")
                        return -1
                    if iq_header.frame_type != frame_type:
                        logging.error("Unexpected frame type")
                        return -2
                    
                    blocks+=1
                except EOFError:
                    pass
        if blocks == frame_count:
            return 0
        else:
            logging.error("Frame count error [Expected/Received]: {:d}/{:d}".format(frame_count, blocks))
            return -3    
    
    def check_ramp(self, file_name, frame_count):
        iq_header = IQHeader()
        ramp_max = 29
        block_index = 0
        time_index = 0
        with open(file_name, "rb") as file_descr:
            while True:
                data_valid = False                
                block_index += 1                
                """
                ------------------
                  IQ Frame Reading
                ------------------
                """
                try:
                    # Reading IQ header
                    iq_header_bytes = file_descr.read(1024)                      
                    if len(iq_header_bytes) == 0: break                    
                    iq_header.decode_header(iq_header_bytes)
                    iq_data_length = int((iq_header.cpi_length * iq_header.active_ant_chs * (2*iq_header.sample_bit_depth))/8)
                    # Reading Multichannel IQ data
                    iq_data_bytes = file_descr.read(iq_data_length)                    
                    if len(iq_data_bytes) ==0: break
                    data_valid = True
                except EOFError:
                    pass                    
                """
                ------------------
                  Check Payload content
                ------------------
                """
                # Check payload content
                if data_valid:                
                    iq_data = np.frombuffer(iq_data_bytes, dtype=np.uint8).reshape(iq_header.active_ant_chs, iq_header.cpi_length*2)            
                    raw_sig = np.arange(time_index, time_index + iq_header.cpi_length,1, dtype=np.uint32)%ramp_max
                    for m in range(iq_header.active_ant_chs):
                        ch_m_data_i = iq_data[m,0::2]
                        ch_m_data_q = iq_data[m,1::2]    
                        if (raw_sig[:]+2*m*ramp_max == ch_m_data_i).all() and (raw_sig[:]+(2*m+1)*ramp_max == ch_m_data_q).all(): 
                            pass                           
                        else:
                            logging.error("Ramp test failed!")
                            logging.error("Block: {:d}".format(block_index))
                            logging.error("Channel: {:d}".format(m))
                            for n in range(10):
                                logging.error("CH I ind:{:d}, [{:d}<->{:d}]"\
                                    .format(n,(raw_sig[:]+2*m*ramp_max)[n],ch_m_data_i[n]))
                                logging.error("CH Q ind:{:d}, [{:d}<->{:d}]"\
                                    .format(n,(raw_sig[:]+(2*m+1)*ramp_max)[n],ch_m_data_q[n]))
                            return -1   
                    time_index+=iq_header.cpi_length
            if block_index-1 != frame_count:
                logging.error("Frame count error occured in ramp test!")
                logging.error("Expected: {:d}, Received: {:d}".format(frame_count, block_index-1))
                return -2
            
        logging.info("Ramp test check {:d} frames".format(block_index-1))
        return 0

    #############################################
    #            AUXILIARY FUNCTIONS            #  
    #############################################

    def _read_config_file(self):
        """

        """
        parser = ConfigParser()
        found = parser.read([self.config_filename])
        if not found:
            # Callers unpack four values - keep the arity right
            return 0, 0, 0, 0
        N = parser.getint('pre_processing', 'cpi_size')
        R = parser.getint('pre_processing', 'decimation_ratio')
        N_daq  = parser.getint('daq','daq_buffer_size')
        N_cal  = parser.getint('calibration', 'corr_size')
               
        return (N, R, N_daq, N_cal)
    
    def _write_config_file(self, N, R, N_daq, N_cal):
        """
        
        """
        parser = ConfigParser()
        found = parser.read([self.config_filename])
        if not found:
            return -1

        parser['pre_processing']['cpi_size'] = str(N)
        parser['pre_processing']['decimation_ratio'] = str(R)
        parser['daq']['daq_buffer_size'] = str(N_daq)
        parser['calibration']['corr_size'] = str(N_cal)
        with open(self.config_filename, 'w') as configfile:
            parser.write(configfile)
        return 0

#############################################
#   WP-04: drop-mode CPI discard (G73) and  #
#   unchanged decimator_in object size      #
#############################################

REBUFFER_BIN = join(daq_core_path, "rebuffer.out")
SHM_DIR = "/dev/shm"
RAMP_MAX = 29  # gen_ramp.py


def _ramp_block(t0, n, M):
    """gen_ramp.py payload: per channel interleaved I/Q, I = t%29 + 2m*29,
    Q = t%29 + (2m+1)*29, t = t0 .. t0+n-1."""
    raw = (np.arange(t0, t0 + n, dtype=np.uint32) % RAMP_MAX).astype(np.uint8)
    out = np.empty((M, 2 * n), dtype=np.uint8)
    for m in range(M):
        out[m, 0::2] = raw + 2 * m * RAMP_MAX
        out[m, 1::2] = raw + (2 * m + 1) * RAMP_MAX
    return out


class _RampWriter(threading.Thread):
    """Feeds rebuffer.out's stdin with paced DATA ramp frames."""

    def __init__(self, stdin, blocks, N_daq, M, delay):
        threading.Thread.__init__(self, daemon=True)
        self.stdin, self.blocks, self.N_daq, self.M, self.delay = stdin, blocks, N_daq, M, delay

    def run(self):
        header = IQHeader()
        header.sync_word = IQHeader.SYNC_WORD
        header.frame_type = IQHeader.FRAME_TYPE_DATA
        header.active_ant_chs = self.M
        header.cpi_length = self.N_daq
        header.sample_bit_depth = 8
        header.sampling_freq = 2400000
        header.adc_sampling_freq = 2400000
        try:
            for b in range(self.blocks):
                header.daq_block_index = b
                self.stdin.write(header.encode_header())
                self.stdin.write(_ramp_block(b * self.N_daq, self.N_daq, self.M).tobytes())
                self.stdin.flush()
                if self.delay:
                    time.sleep(self.delay)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                self.stdin.close()
            except OSError:
                pass


class _U8Capture(threading.Thread):
    """Slow 'decimator_in' consumer keeping every frame in memory."""

    def __init__(self, delay):
        threading.Thread.__init__(self, daemon=True)
        self.delay = delay
        self.frames = []  # (IQHeader, payload ndarray (M, 2*cpi_length))
        self.iface = inShmemIface("decimator_in")
        self.object_size = self.iface.memories[0].size if self.iface.init_ok else None

    def run(self):
        while True:
            idx = self.iface.wait_buff_free()
            if idx not in (0, 1):
                break
            buf = self.iface.buffers[idx]
            header = IQHeader()
            header.decode_header(buf[0:1024].tobytes())
            n = header.cpi_length * header.active_ant_chs * 2
            payload = buf[1024:1024 + n].copy().reshape(header.active_ant_chs, 2 * header.cpi_length)
            self.frames.append((header, payload))
            if self.delay:
                time.sleep(self.delay)
            self.iface.send_ctr_buff_ready(idx)
        self.iface.destory_sm_buffer()


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists(REBUFFER_BIN),
                     "needs Linux and a built rebuffer.out")
class TesterRebufferDropMode(unittest.TestCase):
    """rebuffer.out 1 (drop mode, dormant: the start scripts pass 0) with a
    slow consumer: a dropped CPI is discarded exactly as a delivered one is
    consumed, so every delivered CPI is the contiguous next-in-line block of
    the input stream (spec I.4 #8)."""

    M = 4
    N_DAQ = 2**10
    N = 2**12           # 4 input blocks per output CPI
    N_CAL = 2**10
    BLOCKS = 240

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="rebuffer_wp04_")
        self.prev_cwd = os.getcwd()
        shutil.copyfile(live_config_filename, join(self.workdir, "daq_chain_config.ini"))
        os.makedirs(join(self.workdir, "_data_control"))
        os.chdir(self.workdir)
        os.mkfifo("_data_control/fw_decimator_in")
        os.mkfifo("_data_control/bw_decimator_in")
        parser = ConfigParser()
        parser.read("daq_chain_config.ini")
        parser["hw"]["num_ch"] = str(self.M)
        parser["pre_processing"]["cpi_size"] = str(self.N)
        parser["pre_processing"]["decimation_ratio"] = "1"
        parser["daq"]["daq_buffer_size"] = str(self.N_DAQ)
        parser["calibration"]["corr_size"] = str(self.N_CAL)
        with open("daq_chain_config.ini", "w") as f:
            parser.write(f)

    def tearDown(self):
        os.chdir(self.prev_cwd)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _run(self, drop_arg, consumer_delay, writer_delay):
        with open(join(self.workdir, "rebuffer.log"), "w") as log:
            proc = subprocess.Popen([REBUFFER_BIN, drop_arg], stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=log)
            try:
                capture = _U8Capture(consumer_delay)
                self.assertTrue(capture.iface.init_ok)
                writer = _RampWriter(proc.stdin, self.BLOCKS, self.N_DAQ, self.M, writer_delay)
                capture.start()
                writer.start()
                writer.join(timeout=120)
                capture.join(timeout=120)
                proc.wait(timeout=60)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        with open(join(self.workdir, "rebuffer.log")) as log:
            self.assertEqual(proc.returncode, 0, log.read())
        return capture

    def _assert_contiguous_cpis(self, frames):
        """Each delivered CPI holds the samples [(b+1)*N_daq - N, (b+1)*N_daq)
        of the input stream, b = daq_block_index of the frame that completed it."""
        for header, payload in frames:
            self.assertEqual(header.cpi_length, self.N)
            t0 = (header.daq_block_index + 1) * self.N_DAQ - self.N
            expected = _ramp_block(t0, self.N, self.M)
            self.assertTrue(np.array_equal(payload, expected),
                            "CPI completed by block {:d} is not the input samples {:d}..{:d}"
                            .format(header.daq_block_index, t0, t0 + self.N - 1))

    def test_drop_mode_discards_dropped_cpis(self):
        capture = self._run("1", consumer_delay=0.04, writer_delay=0.004)
        total_cpis = self.BLOCKS * self.N_DAQ // self.N
        delivered = len(capture.frames)
        self.assertGreater(delivered, 2)
        self.assertLess(delivered, total_cpis, "the slow consumer caused no drop")
        # The run drops many more CPIs than the ring holds (rebuffer's ring is
        # N/N_daq + 2 input blocks), so a backlog that outgrew the ring would
        # have overwritten unread samples
        ring_blocks = self.N // self.N_DAQ + 2
        self.assertGreater(self.BLOCKS, 10 * ring_blocks)
        self.assertGreater((total_cpis - delivered) * (self.N // self.N_DAQ), 2 * ring_blocks)
        self._assert_contiguous_cpis(capture.frames)
        # Deliveries after the first drop are still on CPI boundaries
        blocks = [h.daq_block_index for h, _ in capture.frames]
        self.assertEqual(blocks, sorted(set(blocks)))
        self.assertTrue(all((b + 1) % (self.N // self.N_DAQ) == 0 for b in blocks))
        # Every delivered CPI ends at the block that completed it (checked
        # above), so `available` was exactly one CPI at each delivery: the
        # backlog stays within the ring (available <= buffer_num*N_daq*2)

    def test_backpressure_mode_delivers_every_cpi(self):
        capture = self._run("0", consumer_delay=0.01, writer_delay=0.0)
        self.assertEqual(len(capture.frames), self.BLOCKS * self.N_DAQ // self.N)
        self._assert_contiguous_cpis(capture.frames)

    def test_decimator_in_object_size_unchanged(self):
        """The decimator_in object stays 1024 + max(N*R, Nc)*M*2 bytes."""
        capture = self._run("0", consumer_delay=0.0, writer_delay=0.0)
        self.assertEqual(capture.object_size, 1024 + max(self.N, self.N_CAL) * self.M * 2)
