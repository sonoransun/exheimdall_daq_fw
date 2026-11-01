"""
	Description :
	Unit test for the decimator module

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
import warnings
from struct import pack
import time
import threading
from configparser import ConfigParser
from scipy import signal

current_path      = dirname(realpath(__file__))
root_path         = dirname(dirname(current_path))
daq_core_path     = join(root_path, "_daq_core")
log_path          = join(root_path, "_logs")
unit_test_path    = join(root_path, "_testing", "unit_test")
test_logs_path    = join(root_path, "_testing", "test_logs")

live_config_filename = join(root_path, 'daq_chain_config.ini')

# Import HeIMDALL modules
sys.path.insert(0, daq_core_path)
sys.path.insert(0, unit_test_path)
from iq_header import IQHeader
from capture_shmem_stream import IQFrameRecorder
from gen_ramp import rampFrameGenator
from gen_std_frame import stdFrameGenator
from gen_utils import wait_consumer_done
from shmemIface import inShmemIface, outShmemIface

# plotly is only needed by the (development-skipped) transfer/phase report
# checkers - do not make the whole suite depend on it. Without plotly the
# checkers still run, they just skip the HTML report.
try:
    import plotly.graph_objects as go
except ImportError:
    class _NullFigure:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class _NullPlotly:
        @staticmethod
        def Figure(*args, **kwargs):
            return _NullFigure()

        @staticmethod
        def Scatter(*args, **kwargs):
            return None

    go = _NullPlotly()

class TesterDecimatorModule(unittest.TestCase):

    """
        The whole test chain (config file, FIFOs, fir_coeffs.txt) runs
        inside a per-test temporary working directory: the live
        Firmware/daq_chain_config.ini is copied there and only the COPY is
        modified, so an aborted run can never corrupt the developer config.
        decimate.out and the Python shmem interfaces resolve
        daq_chain_config.ini and _data_control/ relative to the cwd.
    """

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(filename=join(test_logs_path,'unit_test.log'),
                             level=logging.INFO)
        logging.info("--> Starting Decimator unit test <--")
        logging.warning("Divide by zero warnings are ignored from numpy")
        warnings.filterwarnings("ignore", message="numpy.dtype size changed")
        warnings.filterwarnings("ignore", message="numpy.ufunc size changed")
        np.seterr(divide = 'ignore')

    def setUp(self):
        # Isolated working directory with a private copy of the config
        self.workdir = tempfile.mkdtemp(prefix="decimator_test_")
        self.prev_cwd = os.getcwd()
        self.config_filename = join(self.workdir, 'daq_chain_config.ini')
        shutil.copyfile(live_config_filename, self.config_filename)
        data_control_path = join(self.workdir, "_data_control")
        os.makedirs(data_control_path)
        os.makedirs(join(self.workdir, "_logs"))
        os.chdir(self.workdir)

        # Open log files
        self.fd_log_decimator_err = open(join(log_path,"decimator.log"), "w")
        self.fd_log_gen_err  = open(join(log_path,"gen.log"),  "w")

        # Create fake gate control file
        self.fd_gate_ctr  = open(join(data_control_path,"m_gate_control_fifo"),  "w")
        self.fd_gate_ctr.close()

        # Set-up control FIFOs
        os.mkfifo(join(data_control_path,"fw_decimator_in"))
        os.mkfifo(join(data_control_path,"bw_decimator_in"))
        os.mkfifo(join(data_control_path,"fw_decimator_out"))
        os.mkfifo(join(data_control_path,"bw_decimator_out"))

        # Default config file parameters (from the private copy)
        self.N, self. R, self.N_daq, self.N_cal, self.fir_bw, self.K, self.win, self.reset = self._read_config_file()

    def tearDown(self):
        # Close log files
        self.fd_log_decimator_err.close()
        self.fd_log_gen_err.close()

        # Remove test data file
        try:
            os.remove(join(unit_test_path,'decimator_test_0.dat'))
        except OSError:
            pass

        # Drop the isolated working directory (config copy, FIFOs, coeffs)
        os.chdir(self.prev_cwd)
        shutil.rmtree(self.workdir, ignore_errors=True)
    
    #############################################
    #               TEST FUNCTIONS              #
    #############################################    
    #@unittest.skip("Skipped during development")
    def test_case_5_0(self):
        logging.info("-> Starting Test case [5_0]: Data frame forward test")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_DATA
        self._write_config_file(N=2**18, R=1, N_daq=2**18, N_cal=2**10, fir_bw=1.0, K=1, win='hann', reset=0) 
        output_frame_count = frame_count   
        # -> Action <-
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        self._run_std_frame_test(frame_count, frame_type)               
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'decimator_test_0.dat'), output_frame_count, frame_type))
    #@unittest.skip("Skipped during development")
    def test_case_5_1(self):
        logging.info("-> Starting Test case [5_1]:")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_DUMMY
        self._write_config_file(N=2**18, R=1, N_daq=2**18, N_cal=2**10, fir_bw=1.0, K=1, win='hann', reset=0)
        output_frame_count = frame_count
        # -> Action <-
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        self._run_std_frame_test(frame_count, frame_type)               
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'decimator_test_0.dat'), output_frame_count, frame_type))
    #@unittest.skip("Skipped during development")
    def test_case_5_2(self):
        logging.info("-> Starting Test case [5_2]:")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_CAL
        # K must exceed R or fir_filter_designer refuses to run (the old
        # K=1 setting only worked against a STALE fir_coeffs.txt left in the
        # repo _data_control by earlier tests; the isolated temp cwd exposed
        # it). CAL frames bypass the FIR, so K does not affect the output.
        self._write_config_file(N=2**18, R=3, N_daq=2**10, N_cal=2**18, fir_bw=1.0, K=4, win='hann', reset=0)
        output_frame_count = frame_count   
        # -> Action <-
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        self._run_std_frame_test(frame_count, frame_type)               
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'decimator_test_0.dat'), output_frame_count, frame_type))    
    #@unittest.skip("Skipped during development")
    def test_case_5_3(self):
        logging.info("-> Starting Test case [5_3]:")
        # -> Assume <-
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_TRIGW
        # K > R required by fir_filter_designer (see test_case_5_2 note);
        # TRIGW frames carry no payload, so K does not affect the output.
        self._write_config_file(N=2**18, R=7, N_daq=2**14, N_cal=2**10, fir_bw=1.0, K=8, win='hann', reset=0)
        output_frame_count = frame_count   
        # -> Action <-
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        self._run_std_frame_test(frame_count, frame_type)     
        # -> Assert <-        
        self.assertFalse(self.check_frame(join(unit_test_path,'decimator_test_0.dat'), output_frame_count, frame_type))
    #@unittest.skip("Skipped during development")
    def test_case_5_4(self):
        logging.info("-> Starting Test case [5_0]: Data frame forward test with non zero decimation ratio")
        # -> Assume <-
        decimation_ratio = 128
        daq_block_size = 8182
        cpi_size = 8192
        cal_size = 2048
        frame_size = daq_block_size*decimation_ratio
        frame_count = 10
        frame_type  = IQHeader.FRAME_TYPE_DATA
        self._write_config_file(N=cpi_size, R=decimation_ratio, N_daq=daq_block_size, N_cal=cal_size, 
                                fir_bw=1.0, K=2*decimation_ratio, win='hann', reset=0)
        output_frame_count = frame_count
        # -> Action <-
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        self._run_std_frame_test(frame_count, frame_type, frame_size)
        # -> Assert <-
        self.assertFalse(self.check_frame(join(unit_test_path,'decimator_test_0.dat'),output_frame_count, frame_type))
    @unittest.skip("Skipped during development")
    def test_case_5_10(self):
        logging.info("-> Starting Test Case [5_11] : Transfer function test")
        # -> Assume <-        
        decimation_ratio = 383
        N = 2**10
        N_cal = 2**13
        fir_bw = 0.9
        K = 500
        win='hann'
        self._write_config_file(N=N, R=decimation_ratio, N_daq=N, N_cal=N_cal, fir_bw=fir_bw, K=K, win=win, reset=0)
        # -> Action <-
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        self._run_swept_cw_test(decimation_ratio, sample_size=N)
        # -> Assert <-        
        transfer_pass = self.check_transfer(join(unit_test_path,'decimator_test_0.dat'), decimation_ratio, fir_bw, K, win)
        self.assertFalse(np.array(transfer_pass).all())
    @unittest.skip("Skipped during development")
    def test_case_5_11(self):
        logging.info("-> Starting Test Case [5_11] : Phase continuity test")
        # -> Assume <-        
        keep_out  = 5 # Number of samples skipped at the begining due to transient effect
        tolerance = 0.05 # Allowed phase jump in radian
        decimation_ratio = 5  
        N = 2**7
        N_cal = 2**7
        self._write_config_file(N=N, R=decimation_ratio, N_daq=N, N_cal=N_cal, fir_bw=1.0, K=2*decimation_ratio, win='hann', reset=0)
        # -> Action <-
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        self._run_swept_cw_test(decimation_ratio, source_type="cw", sample_size=N)
        # -> Assert <-      
        phase_diff_check = self.check_phase_continuity(join(unit_test_path,'decimator_test_0.dat'), decimation_ratio, tolerance, keep_out)  
        self.assertFalse(phase_diff_check)
    #@unittest.skip("Skipped during development")
    def test_case_5_20(self):        
        logging.info("-> Starting Test case [5_20]: Ramp test on data type frames")
        # -> Assume <-
        frame_type  = IQHeader.FRAME_TYPE_DATA
        frame_count = 100
        N = 2**18
        N_cal = 2**10
        self._write_config_file(N=N, R=1, N_daq=N, N_cal=N_cal, fir_bw=1.0, K=1, win='hann', reset=0)
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        # -> Action
        # Start the frame generator
        generator = rampFrameGenator(frame_type, frame_count, N, 'CINT8', 'decimator_in')
        generator.start()
        
        # Start the decimator module
        decimator_module = subprocess.Popen([join(daq_core_path,"decimate.out"),'0'],                                             
                                    stdout=subprocess.DEVNULL,
                                    stderr=self.fd_log_decimator_err)        
        recorder = IQFrameRecorder("decimator_out",
                                   join(unit_test_path,'decimator_test_0.dat'),
                                   "1")
        if recorder.in_shmem_iface.init_ok:
            recorder.start()
        else:
            self.assertTrue(0)        
        generator.join()
        recorder.join()
        logging.info("Ramp test finished, checking output..")        
        self.assertFalse(self.check_ramp(join(unit_test_path,'decimator_test_0.dat'), frame_count))
    #@unittest.skip("Skipped during development")
    def test_case_5_21(self):        
        logging.info("-> Starting Test case [5_21]: Ramp test on calibration type frames")
        # -> Assume <-
        frame_type  = IQHeader.FRAME_TYPE_CAL
        frame_count = 100
        N_cpi = 7919 # Prime number, should be irrelevent in this test
        N_daq = 2**10
        N_cal = 2**10
        # K > R required by fir_filter_designer (see test_case_5_2 note);
        # CAL ramp frames bypass the FIR, so K does not affect the payload.
        self._write_config_file(N=N_cpi, R=3, N_daq=N_daq, N_cal=N_cal, fir_bw=1.0, K=4, win='hann', reset=0)
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        # -> Action
        # Start the frame generator
        generator = rampFrameGenator(frame_type, frame_count, N_cal, 'CINT8', 'decimator_in')
        generator.start()
        
        # Start the decimator module
        decimator_module = subprocess.Popen([join(daq_core_path,"decimate.out"),'0'],                                             
                                    stdout=subprocess.DEVNULL,
                                    stderr=self.fd_log_decimator_err)        
        recorder = IQFrameRecorder("decimator_out",
                                   join(unit_test_path,'decimator_test_0.dat'),
                                   "1")
        if recorder.in_shmem_iface.init_ok:
            recorder.start()
        else:
            self.assertTrue(0)        
        generator.join()
        recorder.join()
        logging.info("Ramp test finished, checking output..")        
        self.assertFalse(self.check_ramp(join(unit_test_path,'decimator_test_0.dat'), frame_count))
    @unittest.skip("Skipped during development")
    def test_case_5_100(self):
        logging.info("-> Starting Test Case [5_100] : Throughput testing")
        
        # -> Assume <-
        frame_count = 100 
        decimation_ratio = 5
        tap_size = 20
        N = 2**18
        self._write_config_file(N=N, R=decimation_ratio, N_daq=N, N_cal=N, fir_bw=1.0, K=tap_size, win='hann', reset=0)            
        
        # -> Action
        proc = subprocess.Popen([sys.executable, join(root_path, "fir_filter_designer.py")], stdout=subprocess.DEVNULL)
        proc.wait()
        data_throughput_ratio = self._run_throughput_test(frame_count, decimation_ratio, N)
                
        # -> Assert <-
        self.assertTrue(data_throughput_ratio > 1)

    #############################################
    #              TEST RUN WRAPPERS            #  
    #############################################
    
    def _run_std_frame_test(self,frame_count, frame_type, sample_size=2**18):        
        # Build up and start test chain
        generator = stdFrameGenator(frame_type, frame_count, sample_size, 'CINT8', 'decimator_in')
        generator.start()
 
        decimator_module = subprocess.Popen([join(daq_core_path,"decimate.out"),'0'],                                             
                                            stdout=subprocess.DEVNULL,
                                            stderr=self.fd_log_decimator_err)
        recorder = IQFrameRecorder("decimator_out",
                                   join(unit_test_path,'decimator_test_0.dat'),
                                   "1")
        if recorder.in_shmem_iface.init_ok:
            recorder.start()
        else:
            self.assertTrue(0)        
        decimator_module.wait()    
        generator.join()
        recorder.join()  
        
    def _run_swept_cw_test(self, decimation_ratio=1, source_type="swept-cw", sample_size=2**18):
        # Build up and start test chain
        generator = subprocess.Popen(["python3",join(unit_test_path,"gen_cw.py"),
                                      '-r',str(decimation_ratio),
                                      '-s', source_type,
                                      '-n',str(sample_size),
                                      "-m", "decimator_in"], 
                                     stdout=subprocess.DEVNULL, 
                                     stderr=self.fd_log_gen_err)
        decimator_module = subprocess.Popen([join(daq_core_path,"decimate.out"),'0'],                                              
                                            stdout=subprocess.DEVNULL,
                                            stderr=self.fd_log_decimator_err)
        recorder = IQFrameRecorder("decimator_out",
                                   join(unit_test_path,'decimator_test_0.dat'),
                                   "1")
        if recorder.in_shmem_iface.init_ok:
            recorder.start()
        else:
            self.assertTrue(0)
        generator.wait()        
        decimator_module.wait()
        recorder.join()
        
    def _run_throughput_test(self, frame_count, decimation_ratio=1, sample_size=2**18):
        # Start frame generator
        generator = subprocess.Popen(["python3",join(unit_test_path,"gen_std_frame.py"),                                
                                "-b",str(frame_count),
                                "-n", str(sample_size),
                                "-m", "decimator_in"], 
                                stdout=subprocess.DEVNULL, 
                                stderr=self.fd_log_gen_err)
        # Start decimator module        
        decimator_module = subprocess.Popen([join(daq_core_path,"decimate.out"),'0'],                                             
                                            stdout=subprocess.DEVNULL,
                                            stderr=self.fd_log_decimator_err)   
        recorder = IQFrameRecorder("decimator_out",
                                   join(unit_test_path,'decimator_test_0.dat'),
                                   "0")        
        if recorder.in_shmem_iface.init_ok:
            recorder.start()
        else:            
            self.assertTrue(0)
        decimator_module.wait()
        recorder.join()        
                
        # -> Evaluation
        data_throughput  = (frame_count)*sample_size/10**6/recorder.elapsed_time
        data_throughput_ratio = data_throughput/2.4
        frame_throughput = (frame_count)/recorder.elapsed_time
        nominal_frame_throughput = 1/(sample_size/(2.4*10**6))

        logging.info("Decimator module total runtime: {:.2f}".format(recorder.elapsed_time))
        logging.info("Total frame count:  {:d}".format(frame_count))
        logging.info("Throughput: {:.1f}/{:.1f} [MSps], {:.1f}/{:.1f} [frame/sec] - ".format(data_throughput,2.4,frame_throughput,nominal_frame_throughput))
        logging.info("Throughput ratio: {:.1f}".format(data_throughput_ratio))
        
        return data_throughput_ratio
    
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
                    #iq_header.dump_header()                                                   
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
        
    def check_transfer(self, file_name, decimation_ratio, fir_bw, K, win):
        iq_header = IQHeader()
        frequencies = []
        transfers   = []        
        with open(file_name, "rb") as file_descr:
            while True:
                data_valid = False                
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
                    #iq_header.dump_header() # For debug purposes
                    iq_data_length = int((iq_header.cpi_length * iq_header.active_ant_chs * (2*iq_header.sample_bit_depth))/8)
                    # Reading Multichannel IQ data
                    if iq_data_length > 0:
                        iq_data_bytes = file_descr.read(iq_data_length)                    
                        if len(iq_data_bytes) == 0: break
                        data_valid = True
                except EOFError:
                    pass                    
                """
                -------------------------
                  Extract trasnfer value
                -------------------------
                """
                # Check payload content
                if data_valid:
                    current_freq=iq_header.rf_center_freq/10**6-100
                    fs = iq_header.sampling_freq/10**6
                    #if current_freq !=0 :
                    frequencies.append(current_freq)
                    iq_cf64 = np.frombuffer(iq_data_bytes, dtype=np.complex64).reshape(iq_header.active_ant_chs, iq_header.cpi_length)
                    iq_cf64 = iq_cf64.copy()                         
                    for m in range(iq_header.active_ant_chs):
                        # DC offset compensation - Use only with max peak search
                        # iq_cf64[m, :]-=-np.average(iq_cf64[m, :]
                        xw = np.fft.fft(iq_cf64[m, :])
                        xw = np.fft.fftshift(xw)
                        xw = abs(xw)
                        
                        max_index = np.argmax(xw)
                        
                        aliased_freq = current_freq-1*fs*round(current_freq/fs)
                        freq_bin = ((aliased_freq/fs)* len(xw)+len(xw)/2)
                        freq_bin = round(freq_bin)
                        if freq_bin == len(xw): freq_bin=0                        
                                                
                        #transfers.append(xw[max_index]/iq_header.cpi_length) # Extract transfer value with max peak search
                        transfers.append(xw[freq_bin]/iq_header.cpi_length) # Extract transfer value with frequency bin indexing
        """
        -------------------------------
            Compare transfer functions
        -------------------------------
        """        
        transfer_pass=[]        
        freq_points = len(frequencies)
        excitation_signal_amplitude = -3 # dB
        tolerance = 0.5 # dB
        min_transfer_value = -45
        logging.info("Measurement frequency points: {:d}".format(freq_points))

        # Generate reference filter response
        
        cut_off = fir_bw/decimation_ratio
        if cut_off < 1:
            # Design band pass FIR Filter
            filter_coeffs = signal.firwin(K, cut_off, window=win)
        else:
            filter_coeffs=np.array([1])

        #filter_coeffs = np.ones(decimation_ratio)
        #filter_coeffs /= np.sum(np.abs(filter_coeffs)) 
        w, h = signal.freqz(b=filter_coeffs, a=1, worN=freq_points, whole=True)
        h = np.abs(h)
        h = np.fft.fftshift(h)
        h_log = 20*np.log10(h)+excitation_signal_amplitude

        # Prepare figure to display responses        
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=frequencies, y=h_log,  name= "Reference", line=dict(width=2, dash='dash')))

        # Compare obtained responses with the reference
        for m in range(iq_header.active_ant_chs):
            # -> Calulcation
            trasnfer_ch_m = transfers[m::iq_header.active_ant_chs]
            meas_transfer_log = 20*np.log10(trasnfer_ch_m)
            transfer_diff = h_log- meas_transfer_log
            transfer_diff [h_log < min_transfer_value] = 0
            transfer_diff = abs(transfer_diff)
            tolerance_check = (transfer_diff > tolerance).any()
            transfer_pass.append(tolerance_check)
            
            # -> Logging
            logging.info('Checking transfer on channel: {:d}'.format(m))            
            logging.info("Maximum deviation: {:.2f} dB".format(max(transfer_diff)))            
            fig.add_trace(go.Scatter(x=frequencies,y=meas_transfer_log, name = "Channel {:d}".format(m), line=dict(width=2, dash='solid')))
            fig.add_trace(go.Scatter(x=frequencies,y=transfer_diff, mode='lines+markers', name = "Channel {:d} deviation".format(m), line=dict(width=2, dash='solid')))

        # Export figure
        fig.update_layout(
            title = "Test Case: 5_10 : Decimator Transfer",
            xaxis_title = "Frequency [MHz]",
            yaxis_title = "Normalized amplitude [dB]",
            font=dict(size=18),
            hovermode='x')
        fig.write_html(join(test_logs_path,'TestCase-5_10.html'))
        
        #np.save("transfers.npy",transfers)
        #np.save("frequencies.npy",frequencies)

        return transfer_pass
    def check_phase_continuity(self, file_name, decimation_ratio, tolerance, keep_out):
        iq_header = IQHeader()
        signal_array=None
        blocks = 0
        with open(file_name, "rb") as file_descr:
            while True:
                data_valid = False                
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
                    #iq_header.dump_header() # For debug purposes
                    iq_data_length = int((iq_header.cpi_length * iq_header.active_ant_chs * (2*iq_header.sample_bit_depth))/8)
                    # Reading Multichannel IQ data
                    if iq_data_length > 0:
                        iq_data_bytes = file_descr.read(iq_data_length)                    
                        if len(iq_data_bytes) == 0: break
                        data_valid = True
                        blocks +=1
                except EOFError:
                    pass                    
                """
                -------------------------
                    Accumulate signal
                -------------------------
                """
                # Check payload content
                if data_valid:
                    iq_cf64 = np.frombuffer(iq_data_bytes, dtype=np.complex64).reshape(iq_header.active_ant_chs, iq_header.cpi_length)
                    iq_cf64 = iq_cf64.copy()    
                    # Note: This is quite inefficient but simple and safe
                    if blocks ==1:
                        signal_array = iq_cf64
                    else: 
                        signal_array = np.append(signal_array, iq_cf64,axis=1)  
        
        logging.info("Received blocks: {:d}".format(blocks))
        if blocks == 0 or signal_array is None:
            logging.error("Phase continuity check: no frames were recorded")
            return True  # caller asserts falsiness -> this is a failure
        signal_array = signal_array[:,keep_out:]
        """
        -------------------------------
            Check phase continuity
        -------------------------------
        """                
        fs = 2.4*10**6
        f  = fs/256
        
        # Generate reference signal
        amplitude = decimation_ratio
        ref = amplitude*np.exp(1j*(2*np.pi*f/(fs/decimation_ratio)*np.arange(0,len(signal_array[0,:]),1)))

        # Prepare figure to display responses        
        fig = go.Figure()
        #fig.add_trace(go.Scatter(y=ref.real,  name= "Reference-real", line=dict(width=2, dash='dash')))            
        #fig.add_trace(go.Scatter(y=ref.imag,  name= "Reference-imag", line=dict(width=2, dash='dash')))            

        # Compare obtained responses with the reference. Every channel's
        # verdict is accumulated (the old code leaked the loop variable and
        # effectively checked only the LAST channel).
        channel_failed = []
        for m in range(iq_header.active_ant_chs):
            # -> Calulcation
            mf_output  = signal_array[m,:] * ref.conj()
            phase_diff = np.abs(np.diff(np.angle(mf_output)))
            channel_failed.append(bool((phase_diff > tolerance).any()))

            # -> Logging
            logging.info('Checking phase conitnuity on channel: {:d}'.format(m))
            logging.info("Maximum deviation: {:.2f} rad".format(max(np.abs(phase_diff))))
            fig.add_trace(go.Scatter(y=signal_array[m,:].real, name = "Channel {:d} - real".format(m), line=dict(width=2, dash='solid')))
            #fig.add_trace(go.Scatter(y=signal_array[m,:].imag, name = "Channel {:d} - imag".format(m), line=dict(width=2, dash='solid')))
            fig.add_trace(go.Scatter(y=np.append(np.zeros(1),phase_diff), name = "Phase diff, channel :{:d}".format(m), line=dict(width=2, dash='solid')))

        # Export figure
        fig.update_layout(
            title = "Test Case: 5_11 : Decimator Phase Conitnuity",
            xaxis_title = "Time index [sample]",
            yaxis_title = "Phase difference",
            font=dict(size=18),
            hovermode='x')
        fig.write_html(join(test_logs_path,'TestCase-5_11.html'))

        return any(channel_failed)
    
    def check_ramp(self, file_name, frame_count):
        iq_header = IQHeader()
        ramp_max = 29
        DC = 127.5
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
                    #iq_data = np.frombuffer(iq_data_bytes, dtype=np.uint8).reshape(iq_header.active_ant_chs, iq_header.cpi_length*2)            
                    iq_cf64 = np.frombuffer(iq_data_bytes, dtype=np.complex64).reshape(iq_header.active_ant_chs, iq_header.cpi_length)
                    iq_cf64 = iq_cf64.copy()

                    # Revert normalization and DC compensation made in the decimator
                    iq_cf64 = iq_cf64*DC + DC*(1+1j)

                    raw_sig = np.arange(time_index, time_index + iq_header.cpi_length,1, dtype=np.uint32)%ramp_max
                    for m in range(iq_header.active_ant_chs):
                        ch_m_data_i = iq_cf64[m,:].real
                        ch_m_data_q = iq_cf64[m,:].imag
                        # abs(): corruption in EITHER direction must fail
                        # (the old signed compare passed when received >
                        # expected)
                        diff_i = np.abs(raw_sig[:]+2*m*ramp_max - ch_m_data_i)
                        diff_q = np.abs(raw_sig[:]+(2*m+1)*ramp_max - ch_m_data_q)
                        if (diff_i < 0.1).all() and (diff_q < 0.1).all():
                            pass
                            logging.info("Block {:d}, Channel {:d} - Passed".format(iq_header.daq_block_index, m))
                        else:
                            errors = 0
                            logging.error("Ramp test failed!")
                            logging.error("DAQ block index: {:d}".format(iq_header.daq_block_index))
                            logging.error("Channel: {:d}".format(m))
                            logging.error("Expected <-> Received")              
                            for n in range(iq_header.cpi_length):                                
                                if diff_i[n] > 0.1 or diff_q[n] > 0.1:
                                    errors +=1
                                    #logging.error("Diff I: {:f}, Diff Q: {:f}".format(diff_i[n], diff_q[n]))
                                    logging.error("CH I ind:{:d}, [{:d}<->{:f}]"\
                                        .format(n,(raw_sig[:]+2*m*ramp_max)[n],ch_m_data_i[n]))
                                    logging.error("CH Q ind:{:d}, [{:d}<->{:f}]"\
                                        .format(n,(raw_sig[:]+(2*m+1)*ramp_max)[n],ch_m_data_q[n]))      
                                if errors > 10:
                                    break

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
            return 0,0,0,0,0,'',0
        N       = parser.getint('pre_processing', 'cpi_size')        
        R       = parser.getint('pre_processing', 'decimation_ratio')        
        N_daq   = parser.getint('daq','daq_buffer_size')
        fir_bw  = parser.getfloat('pre_processing', 'fir_relative_bandwidth')
        K       = parser.getint('pre_processing', 'fir_tap_size')
        win     = parser.get('pre_processing','fir_window')
        reset   = parser.getint('pre_processing','en_filter_reset')
        N_cal   = parser.getint('calibration', 'corr_size')
        
        return (N, R, N_daq, N_cal, fir_bw, K, win, reset)
    
    def _write_config_file(self, N, R, N_daq, N_cal, fir_bw, K, win, reset):
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
        parser['pre_processing']['fir_relative_bandwidth'] = str(fir_bw)
        parser['pre_processing']['fir_tap_size'] = str(K)
        parser['pre_processing']['fir_window'] = win
        parser['pre_processing']['en_filter_reset'] = str(reset) 
        with open(self.config_filename, 'w') as configfile:
            parser.write(configfile)
        return 0


#############################################
#   WP-04: segment sizing, drop stamping,   #
#   per-instance FIR coefficients           #
#############################################

DECIMATE_BIN = join(daq_core_path, "decimate.out")
SHM_DIR = "/dev/shm"
MAX_IQFRAME_PAYLOAD_SIZE = 2**23  # rtl_daq.h
RSV_DECIMATOR_DROP_CNT = 109      # iq_header.h IQH_RSV_DECIMATOR_DROP_CNT


def _page_round(nbytes):
    page = os.sysconf("SC_PAGE_SIZE")
    return (nbytes + page - 1) // page * page


def _shm_name(instance_id, base):
    return base if instance_id == 0 else "inst{:d}_{:s}".format(instance_id, base)


def _shm_mapping_len(pid, shm_name):
    """Length of the mapping of /dev/shm/<shm_name> in process <pid>."""
    with open("/proc/{:d}/maps".format(pid)) as maps:
        for line in maps:
            fields = line.split()
            if len(fields) >= 6 and fields[5] == join(SHM_DIR, shm_name):
                start, end = (int(x, 16) for x in fields[0].split("-"))
                return end - start
    return None


class _CU8FrameSource(threading.Thread):
    """Producer of 'decimator_in' (rebuffer.out's role): DATA frames of
    M x N complex uint8 samples with a seeded, frame-dependent payload."""

    def __init__(self, frames, N, M=4, instance_id=0, seed=1, delay=0.0):
        threading.Thread.__init__(self, daemon=True)
        self.frames, self.N, self.M = frames, N, M
        self.instance_id, self.seed, self.delay = instance_id, seed, delay
        self.size = 1024 + N * M * 2

    def run(self):
        iface = outShmemIface("decimator_in", self.size, drop_mode=False,
                              instance_id=self.instance_id)
        if not iface.init_ok:
            iface.destory_sm_buffer()
            return
        rng = np.random.default_rng(self.seed)
        header = IQHeader()
        header.sync_word = IQHeader.SYNC_WORD
        header.frame_type = IQHeader.FRAME_TYPE_DATA
        header.active_ant_chs = self.M
        header.cpi_length = self.N
        header.sample_bit_depth = 8
        header.adc_sampling_freq = 2400000
        header.sampling_freq = 2400000
        for b in range(self.frames):
            header.daq_block_index = b
            payload = rng.integers(0, 256, self.N * self.M * 2, dtype=np.uint8)
            idx = iface.wait_buff_free()
            if idx not in (0, 1):
                break
            buf = iface.buffers[idx]
            buf[0:1024] = np.frombuffer(header.encode_header(), dtype=np.uint8)
            buf[1024:1024 + len(payload)] = payload
            iface.send_ctr_buff_ready(idx)
            if self.delay:
                time.sleep(self.delay)
        iface.send_ctr_terminate()
        wait_consumer_done(iface)
        iface.destory_sm_buffer()


class _FrameCapture(threading.Thread):
    """Consumer of a cf32 stream that keeps every frame in memory. A
    per-frame delay makes it a slow consumer (drop-mode producers drop)."""

    def __init__(self, shmem_name, instance_id=0, delay=0.0):
        threading.Thread.__init__(self, daemon=True)
        self.delay = delay
        self.frames = []  # (header_bytes, payload_bytes)
        self.iface = inShmemIface(shmem_name, instance_id=instance_id)
        self.object_sizes = [m.size for m in self.iface.memories]

    def run(self):
        header = IQHeader()
        while True:
            idx = self.iface.wait_buff_free()
            if idx not in (0, 1):
                break
            buf = self.iface.buffers[idx]
            hdr = buf[0:1024].tobytes()
            header.decode_header(hdr)
            nbytes = header.cpi_length * header.active_ant_chs * 2 * (header.sample_bit_depth // 8)
            self.frames.append((hdr, buf[1024:1024 + nbytes].tobytes()))
            if self.delay:
                time.sleep(self.delay)
            self.iface.send_ctr_buff_ready(idx)
        self.iface.destory_sm_buffer()


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists(DECIMATE_BIN),
                     "needs Linux and a built decimate.out")
class TesterDecimatorWP04(unittest.TestCase):
    """[pre_processing] size_segments_from_config, [monitoring]
    stamp_decimator_drops and the instance-prefixed FIR coefficient file
    (spec I.4 #6, G.6, B.2). Runs in a private temp cwd like the suite above."""

    M = 4

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="decimator_wp04_")
        self.prev_cwd = os.getcwd()
        shutil.copyfile(live_config_filename, join(self.workdir, "daq_chain_config.ini"))
        os.makedirs(join(self.workdir, "_data_control"))
        os.makedirs(join(self.workdir, "_logs"))
        os.chdir(self.workdir)
        self.instance_id = 0
        self.log_name = join(self.workdir, "decimator.log")

    def tearDown(self):
        os.chdir(self.prev_cwd)
        shutil.rmtree(self.workdir, ignore_errors=True)
        # A decimate.out that failed after its transport init leaves its
        # inst<N>_ producer segments behind (instance 0 names are never
        # touched: they may belong to a running chain)
        if self.instance_id != 0:
            for name in ("decimator_out_A", "decimator_out_B"):
                path = join(SHM_DIR, _shm_name(self.instance_id, name))
                if os.path.exists(path):
                    os.remove(path)

    # ---------- helpers ----------
    def _configure(self, N, R, N_cal, K, instance_id=0, **keys):
        """keys: {'section__key': value} for optional keys."""
        parser = ConfigParser()
        parser.read("daq_chain_config.ini")
        parser["hw"]["num_ch"] = str(self.M)
        parser["pre_processing"]["cpi_size"] = str(N)
        parser["pre_processing"]["decimation_ratio"] = str(R)
        parser["daq"]["daq_buffer_size"] = str(N)
        parser["calibration"]["corr_size"] = str(N_cal)
        parser["pre_processing"]["fir_relative_bandwidth"] = "1.0"
        parser["pre_processing"]["fir_tap_size"] = str(K)
        parser["pre_processing"]["fir_window"] = "hann"
        parser["pre_processing"]["en_filter_reset"] = "0"
        parser["daq"]["log_level"] = "2"
        if not parser.has_section("federation"):
            parser.add_section("federation")
        parser["federation"]["instance_id"] = str(instance_id)
        for name, value in keys.items():
            section, key = name.split("__")
            if not parser.has_section(section):
                parser.add_section(section)
            parser[section][key] = str(value)
        with open("daq_chain_config.ini", "w") as f:
            parser.write(f)
        self.instance_id = instance_id
        prefix = "" if instance_id == 0 else "inst{:d}_".format(instance_id)
        for fifo in ("fw_decimator_in", "bw_decimator_in", "fw_decimator_out", "bw_decimator_out"):
            path = join("_data_control", prefix + fifo)
            if not os.path.exists(path):
                os.mkfifo(path)

    def _design(self):
        res = subprocess.run([sys.executable, join(root_path, "fir_filter_designer.py")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def _run(self, frames, N, args=("0",), delay=0.0, seed=1, source_delay=0.0):
        """Runs source -> decimate.out -> capture; returns a result dict."""
        source = _CU8FrameSource(frames, N, M=self.M, instance_id=self.instance_id,
                                 seed=seed, delay=source_delay)
        source.start()
        with open(self.log_name, "w") as log:
            proc = subprocess.Popen([DECIMATE_BIN] + list(args),
                                    stdout=subprocess.DEVNULL, stderr=log)
            try:
                capture = _FrameCapture("decimator_out", instance_id=self.instance_id, delay=delay)
                result = {"maps_in": None, "object_size": None}
                if capture.iface.init_ok:
                    result["maps_in"] = _shm_mapping_len(
                        proc.pid, _shm_name(self.instance_id, "decimator_in_A"))
                    result["object_size"] = os.stat(join(
                        SHM_DIR, _shm_name(self.instance_id, "decimator_out_A"))).st_size
                    capture.start()
                    capture.join(timeout=120)
                result["returncode"] = proc.wait(timeout=120)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        source.join(timeout=30)
        with open(self.log_name) as log:
            result["log"] = log.read()
        result["frames"] = capture.frames
        result["init_ok"] = capture.iface.init_ok
        return result

    @staticmethod
    def _headers(frames):
        out = []
        for hdr, _ in frames:
            h = IQHeader()
            h.decode_header(hdr)
            out.append(h)
        return out

    # ---------- tests ----------
    def test_default_segment_sizes_unchanged(self):
        """No new key: decimator_out object = the 2^23-sample worst case and
        the decimator_in mapping = today's expression (ledger L4 values)."""
        N, R, N_cal = 2**12, 2, 2**10
        self._configure(N, R, N_cal, K=8)
        self._design()
        res = self._run(frames=4, N=N * R)
        self.assertEqual(res["returncode"], 0, res["log"])
        self.assertEqual(len(res["frames"]), 4)
        self.assertEqual(res["object_size"], MAX_IQFRAME_PAYLOAD_SIZE * self.M * 4 * 2 + 1024)
        self.assertEqual(res["maps_in"], _page_round(N * self.M * R * 4 * 2 + 1024))
        for h in self._headers(res["frames"]):
            self.assertEqual(h.reserved[RSV_DECIMATOR_DROP_CNT], 0)
        self.assertIn("FIR coefficients: _data_control/fir_coeffs.txt (8 taps)", res["log"])

    def test_default_consumer_map_uses_corr_size_when_larger(self):
        N, R, N_cal = 2**10, 1, 2**13
        self._configure(N, R, N_cal, K=1)
        self._design()
        res = self._run(frames=2, N=N)
        self.assertEqual(res["returncode"], 0, res["log"])
        self.assertEqual(res["maps_in"], _page_round(N_cal * self.M * 4 * 2 + 1024))

    def test_config_sized_segments_identical_output(self):
        """size_segments_from_config = 1: object = 1024 + max(N, Nc)*M*8 and
        every output frame byte-identical to the default run."""
        N, R, N_cal = 2**12, 2, 2**10
        self._configure(N, R, N_cal, K=8)
        self._design()
        ref = self._run(frames=6, N=N * R, seed=7)
        self.assertEqual(ref["returncode"], 0, ref["log"])

        self._configure(N, R, N_cal, K=8, pre_processing__size_segments_from_config=1)
        new = self._run(frames=6, N=N * R, seed=7)
        self.assertEqual(new["returncode"], 0, new["log"])
        self.assertEqual(new["object_size"], 1024 + max(N, N_cal) * self.M * 8)
        self.assertEqual(len(new["frames"]), 6)
        self.assertEqual(new["frames"], ref["frames"])
        self.assertIn("decimator_out segments sized from config", new["log"])
        self.assertNotIn("sized from config", ref["log"])

    def test_config_sized_segments_corr_size_dominates(self):
        N, R, N_cal = 2**10, 1, 2**12
        self._configure(N, R, N_cal, K=1, pre_processing__size_segments_from_config=1)
        self._design()
        res = self._run(frames=2, N=N)
        self.assertEqual(res["returncode"], 0, res["log"])
        self.assertEqual(res["object_size"], 1024 + N_cal * self.M * 8)

    def _drop_run(self, stamp):
        N = 2**12
        keys = {"monitoring__stamp_decimator_drops": 1} if stamp else {}
        self._configure(N, 1, 2**10, K=1, **keys)
        self._design()
        # Drop mode; the consumer is slower than the paced source, so
        # deliveries and output drops interleave
        res = self._run(frames=60, N=N, args=("1",), delay=0.05, source_delay=0.02)
        self.assertEqual(res["returncode"], 0, res["log"])
        headers = self._headers(res["frames"])
        self.assertGreater(len(headers), 2)
        gaps = [h.cpi_index - i for i, h in enumerate(headers)]
        self.assertGreater(max(gaps), 0, "no output frame was dropped between deliveries")
        return headers

    def test_drop_count_stamped(self):
        """stamp_decimator_drops = 1: slot 109 = output frames dropped before
        this frame = cpi_index - frames delivered before it."""
        headers = self._drop_run(stamp=True)
        for delivered_before, h in enumerate(headers):
            self.assertEqual(h.reserved[RSV_DECIMATOR_DROP_CNT], h.cpi_index - delivered_before)
            self.assertEqual(h.get_decimator_drop_cnt(), h.cpi_index - delivered_before)

    def test_drop_count_not_stamped_by_default(self):
        for h in self._drop_run(stamp=False):
            self.assertEqual(h.reserved[RSV_DECIMATOR_DROP_CNT], 0)

    def test_instance_reads_prefixed_coefficients(self):
        """instance_id = 1: designer writes inst1_fir_coeffs.txt, decimate.out
        reads it and streams on the inst1_ segments."""
        N, R = 2**11, 2
        self._configure(N, R, 2**10, K=8, instance_id=1)
        self._design()
        self.assertTrue(os.path.exists("_data_control/inst1_fir_coeffs.txt"))
        self.assertFalse(os.path.exists("_data_control/fir_coeffs.txt"))
        res = self._run(frames=3, N=N * R)
        self.assertEqual(res["returncode"], 0, res["log"])
        self.assertEqual(len(res["frames"]), 3)
        self.assertIn("FIR coefficients: _data_control/inst1_fir_coeffs.txt (8 taps)", res["log"])

    def test_instance_with_only_unprefixed_coefficients_fails(self):
        """An instance N > 0 never falls back to the unprefixed file."""
        self._configure(2**10, 1, 2**10, K=1, instance_id=1)
        np.savetxt("_data_control/fir_coeffs.txt", np.array([1.0]))
        res = self._run(frames=2, N=2**10)
        self.assertNotEqual(res["returncode"], 0)
        self.assertEqual(res["frames"], [])
        self.assertIn("_data_control/inst1_fir_coeffs.txt: cannot open coefficients", res["log"])
