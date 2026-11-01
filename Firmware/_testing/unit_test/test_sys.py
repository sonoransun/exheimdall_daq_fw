"""
	Description : System level testing

	Project : HeIMDALL DAQ
	License : GNU GPL V3
	Author  : Tamas Peto
	Copyright (C) 2018-2020  Tamás Pető
	
	This program is free Falsesoftware: you can redistribute it and/or modify
	it under the terms of the GNU General Public License as published by
	the Free Software Foundation, either version 3 of the License, or
	any later version.
	
	This program is distributed in the hope that it will be useful,
	but WITHOUT ANY WARRANTY; without even the implied warranty of
	MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
	GNU General Public License for more details.
	
	You should have received a copy of the GNU General Public License
	along with this program.  If not, see <https://www.gnu.org/licenses/>.                


	Manual lane (needs real or synthetic hardware, sudo, built C binaries):

	    sudo python3 -W ignore -m unittest -v _testing/unit_test/test_sys.py

	HEIMDALL_TEST_FIRMWARE_DIR runs the chain of a provisioned instance
	directory instead of this tree's Firmware/ (util/provision_instance.py):
	daq_start_sm.sh is started there, the IQ client connects to that
	instance's port (5000 + instance_id * port_stride, read from its
	daq_chain_config.ini) and gen.log goes to its log directory (_logs/ for
	instance 0, _logs/inst<N>/ otherwise). sudo resets the environment, so
	pass the variable through env:

	    sudo env HEIMDALL_TEST_FIRMWARE_DIR=/opt/heimdall/instances/1/Firmware \
	        python3 -W ignore -m unittest -v _testing/unit_test/test_sys.py

"""
import unittest
import os
from configparser import ConfigParser
from os.path import join, dirname, realpath, isfile
import sys
import subprocess
import logging
import numpy as np
from struct import pack
import time

current_path      = dirname(realpath(__file__))
root_path         = dirname(dirname(current_path))
daq_core_path     = join(root_path, "_daq_core")
unit_test_path    = join(root_path, "_testing", "unit_test")
test_logs_path    = join(root_path, "_testing", "test_logs")


def chain_dir():
    """The Firmware directory whose chain is tested: HEIMDALL_TEST_FIRMWARE_DIR
    (a provisioned instance directory) or this tree's Firmware/."""
    return realpath(os.environ.get("HEIMDALL_TEST_FIRMWARE_DIR") or root_path)


def instance_layout(firmware_dir):
    """(instance_id, IQ server port, log directory) of the chain in *firmware_dir*."""
    ini = join(firmware_dir, "daq_chain_config.ini")
    if not isfile(join(firmware_dir, "daq_start_sm.sh")) or not isfile(ini):
        raise RuntimeError("HEIMDALL_TEST_FIRMWARE_DIR={0} is not a Firmware/instance directory "
                           "(daq_start_sm.sh and daq_chain_config.ini expected)".format(firmware_dir))
    cfg = ConfigParser(interpolation=None)
    cfg.read(ini)
    instance_id = cfg.getint("federation", "instance_id", fallback=0)
    stride = cfg.getint("federation", "port_stride", fallback=100)
    logs = join(firmware_dir, "_logs") if instance_id == 0 else \
        join(firmware_dir, "_logs", "inst{0:d}".format(instance_id))
    return instance_id, 5000 + instance_id * stride, logs


firmware_path = chain_dir()
instance_id, iq_port, log_path = instance_layout(firmware_path)
data_control_path = join(firmware_path, "_data_control")


# Import IQ header module
sys.path.insert(0, daq_core_path)
from iq_header import IQHeader


# Import IQ frame sink module
from iq_eth_sink import IQRecorder

class TesterSystem(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(filename=join(test_logs_path,'unit_test.log'),
                             level=logging.INFO)   
        logging.info("--> Starting System level testing <--")
        logging.info("Chain: %s (instance %d, IQ port %d)", firmware_path, instance_id, iq_port)
        
    def setUp(self):        
        self.logger = logging.getLogger(__name__)
        
        # Open log files
        os.makedirs(log_path, exist_ok=True)
        self.fd_log_gen_err  = open(join(log_path,"gen.log"),  "w")        

    def tearDown(self):
        
        # Close log files
        self.fd_log_gen_err.close()
    
    #############################################
    #               TEST FUNCTIONS              #  
    #############################################
    def test_case_0_200(self):
        self.logger.info("-> Starting Test Case [200] : System level throughput testing with synthetic data")
        
        # -> Assume <-
        frame_count = 10000
        
        # -> Action
        # Start the daq chain
        daq_chain_start = subprocess.Popen(["./daq_start_sm.sh"], cwd=firmware_path,
                                    stdout=subprocess.DEVNULL)
        daq_chain_start.wait()
        
        # Start ethernet IQ frame receiver
        IQ_rec_inst0 = IQRecorder(frame_count, port=iq_port)
        time.sleep(2)             
        IQ_rec_inst0.listen()
        self.logger.info("IQ frame reception interval finished")
        time.sleep(2)
        
        frame_drop_rate = IQ_rec_inst0.dropped_frames/IQ_rec_inst0.max_received_frames 
        self.logger.info("IQ Frame drop statistic:")
        self.logger.info("------------------------")
        self.logger.info("total / dropped frames: [{:d}/{:d}], drop rate {:.2f}".format(IQ_rec_inst0.max_received_frames,IQ_rec_inst0.dropped_frames,frame_drop_rate))

        # -> Assert <-
        self.assertTrue(IQ_rec_inst0.dropped_frames < 50)
        
    #############################################
    #              TEST RUN WRAPPERS            #  
    #############################################

    #############################################
    #         RESULT CHECKER FUNCTIONS          #  
    #############################################
    
    