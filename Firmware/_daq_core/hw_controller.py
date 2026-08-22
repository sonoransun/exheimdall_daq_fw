"""    
    Hardware Controller Module 

    Project  : HeIMDALL DAQ Firmware 
    Author   : Tamás Pető
    RF front : R820T compatible, For ADPIS dedicated IQ modulator is required
    License  : GNU GPL V3
        
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
# Import built in modules
from struct import pack, unpack
import threading
import queue
import logging
import json
import os

# Import third-party modules
import numpy as np
import socket
from configparser import ConfigParser

# Import HeIMDALL modules
from iq_header import IQHeader
from shmemIface import inShmemIface, TERMINATE
from transportIface import TransportConsumer
import zmq
import inter_module_messages
from daq_db_records import (CAL_EVENT_NOISE_ON, CAL_EVENT_NOISE_OFF,
                            CAL_EVENT_FREQ_CHANGE, CAL_EVENT_GAIN_CHANGE)


class CtrRequest:
    """One in-flight control request handed from the CtrIfaceServer thread to
    the HWC main loop. The server thread waits on `done` (with timeout) and
    then reads `reply_payload` (bytes, query replies only, None otherwise).
    On timeout the server thread sets `cancelled` so the main loop withdraws
    the request instead of executing it at an arbitrary later frame."""

    def __init__(self, command, params):
        self.command = command
        self.params = params
        self.done = threading.Event()
        self.reply_payload = None
        self.cancelled = False


# Global: request queue between the Control Interface server thread and the
# HWC main loop. The server enqueues CtrRequest objects; the main loop drains
# the queue once per frame (queries in any state, hardware mutations only in
# the safe STATE_IQ_CAL state) and sets each request's `done` event.
ctr_request_queue = queue.Queue(maxsize=8)

class HWC():
    
    def __init__(self):                 
        
        logging.basicConfig(level=10)
        self.logger = logging.getLogger(__name__)
        self.log_level=0 # Set from the ini file        
        self.module_identifier = 6 # Inter-module message module identifier
        self.track_lock_ctr_fname = "_data_control/iq_track_lock"
        self.track_lock_ctr_fd = None
        self.in_shmem_iface = None
        # Gain index:       0  1   2   3   4   5   6    7    8    9   10   11   12   13   14   15   16   17   18   19   20   21   22   23   24   25   26   27   28
        self.valid_gains = [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496]
        # Defined by the R820T tuner
        
        self.cal_gain_table=np.array([[100,200,300,400,500,600,700,1700],[6, 10, 13, 14, 18, 22, 28, 28]]) # First column frequeny [MHz], second column gain index [valid_gains]
        self.cal_gain_table[0,:]*=10**6 # Convert to Hz
        self.M = 7 # Number of receiver channels 
        self.N = 2**18 # Number of samples per channel
        self.N_proc = 2**13
        self.iq_mod = None
        self.en_adpis= 0 # Enables or Disables of the ADPIS hardware usage 
        self.gains=[0]*self.M  # Initial gain values (Indices, not exact values!)
        self.noise_source_state = False
        self.gain_tune_states=[False]*self.M
        self.gain_lock_cntr = 0 # Auxiliary counter used for the gain lock
        self.gain_lock_interval = 0 # Required num. of frames to finish gain tuning
        self.unified_gain_control = True # Gain values will be equal for all the receivers
        self.last_gains=[0]*self.M # Stores the gain values to reset them after calibration
        self.agc=False
        self.last_agc=False
        
        # Support for calibration track mode 
        self.cal_track_mode = 0
        self.cal_frame_interval = 50 # Number of frames between two cal frames in cal_track_mode=2
        self.cal_frame_burst_size = 5 # Number of cal frames in a burst
        self.cal_frame_cntr = 0
        self.en_iq_cal = False
        self.sync_failed_cntr = 0 # Counts the number of iq or sample sync fails in track mode
        self.max_sync_fails = 3 # Maximum number of synchronization fails before the sync track is lost

        self.require_track_lock_intervention = True
        self.rf_center_frequency = 1

        # Scheduler and database
        self.scheduler = None
        self.db = None
        self._en_db = False
        self._hw_snapshot_interval = 100
        self._hw_snapshot_counter = 0

        # Monitoring
        self.event_bus = None

        # Federation
        self.instance_id = 0
        self.port_stride = 100

        # Control interface bind address ([daq] listen_address, optional)
        self.listen_address = "0.0.0.0"

        # ZMQ control-link robustness (rtl_daq REQ/REP)
        self.ZMQ_TIMEOUT_MS = 5000
        self.zmq_context = None
        self.zmq_port = 1130

        # Offload / transport defaults
        self.transport_type = 'shm'

        # RF front-end (amplified receivers) and antenna profile
        self.rf_frontend = None
        self.antenna_profile = None
        self._en_amplification = False
        self.ext_lna_gains_db = [0.0]*self.M      # runtime-editable external LNA gain, raw dB
        self.total_system_gains_db = [0.0]*self.M
        self.system_nf_db = [0.0]*self.M
        self.compression_flags = 0
        self.bias_tee_state = [0]*self.M

        # Antenna orientation controller
        self.orientation = None
        self._orientation_state_fname = "_data_control/orientation_state"
        self._orientation_write_counter = 0

        # Bias-tee relay file (read by delay_sync for v8 header stamping)
        self._bias_tee_state_fname = "_data_control/bias_tee_state"

        # Control requests deferred while the FSM is not in a safe state
        self._pending_ctr_requests = []

        # Overwrite default configuration
        self._read_config_file("daq_chain_config.ini")
        self.iq_header = IQHeader()
        
        self.current_state = "STATE_INIT" 
        """
            Block sizes measured in bytes        
            1 IQ sample consist of 2 32bit float number
        """
        # Configure logger                        
        self.logger.setLevel(self.log_level)

        # Control interface server
        hwc_port = 5001 + self.instance_id * self.port_stride
        self.ctr_iface_server = CtrIfaceServer(self.M, port=hwc_port,
                                               listen_address=self.listen_address)
        self.ctr_iface_server.start()

        self.logger.info("Antenna channles {:d}".format(self.M))
        self.logger.info("IQ samples per channel {:d}".format(self.N))        
        self.logger.info("Processing sample size: {:d}".format(self.N_proc))
        self.logger.info("ADPIS state: {:d}".format(self.en_adpis))
        self.logger.warning("Reference channel index is fixed 0 ")
        self.logger.info("Hardware Controller initialized")
        
    def _read_config_file(self, config_filename):
        """
            Configures the internal parameters of the processing module based 
            on the values set in the confiugration file.

            TODO: Handle configuration field read failure
            Parameters:
            -----------
                :param: config_filename: Name of the configuration file
                :type:  config_filename: string
                    
            Return values:
            --------------
                :return: 0: Confiugrations fields succesfully applied
                        -1: Configuration file not found
        """
        parser = ConfigParser()
        found = parser.read([config_filename])
        if not found:
            self.logger.error("DAQ core configuration file not found. Default parameters will be used!")
            return -1
        self.N = parser.getint('pre_processing', 'cpi_size')
        self.M = parser.getint('hw', 'num_ch')                
        self.N_proc = parser.getint('adpis', 'adpis_proc_size')
        gains_init_str=parser.get('adpis','adpis_gains_init')
        self.cal_track_mode = parser.getint('calibration','cal_track_mode')        
        self.rf_center_frequency = parser.getint('daq','center_freq')
        self.max_sync_fails = parser.getint('calibration','maximum_sync_fails')
        self.cal_frame_burst_size = parser.getint('calibration','cal_frame_burst_size')
        self.cal_frame_interval = parser.getint('calibration','cal_frame_interval')
        self.gain_lock_interval = parser.getint('calibration','gain_lock_interval')     
        
        if parser.getint('calibration', 'unified_gain_control'):
            self.unified_gain_control=1
        else:
            self.unified_gain_control=0
        if parser.getint('calibration','en_gain_tune_init'):
            self.gain_tune_states=[True]*self.M
        else:
            self.gain_tune_states=[False]*self.M
        if parser.getint('adpis','en_adpis'):
            self.en_adpis=1
        else:
            self.en_adpis=0
        if parser.getint('calibration','require_track_lock_intervention'):
            self.require_track_lock_intervention=True
        else:
            self.require_track_lock_intervention=False
        if parser.getint('calibration', 'en_iq_cal'):
            self.en_iq_cal = True
        else:
            self.en_iq_cal = False
        self.log_level = parser.getint('daq','log_level')*10
        # Optional bind address for the TCP control servers (default: all interfaces)
        self.listen_address = parser.get('daq', 'listen_address', fallback='0.0.0.0').strip() or '0.0.0.0'

        # Convert the gain list
        gains_init_str = gains_init_str.split(',')
        gains_init_ind = list(map(int, gains_init_str))
        # -> Channel number check
        if len(gains_init_ind) != self.M:
            logging.warning("Channel number missmatch when reading initial gain values")
            logging.warning("Gain values leaved in default state")
            self.gains=[0]*self.M
            self.last_gains=[0]*self.M
            logging.warning("GAINS: {0}".format(self.gains))
            logging.warning("Last GAINS: {0}".format(self.last_gains))
        else:
            self.gains=[]
            self.last_gains=[]
            try:
                for gain_item in gains_init_ind:
                    self.gains.append(self.valid_gains.index(gain_item))
                    self.last_gains.append(self.valid_gains.index(gain_item))
            except ValueError:
                logging.error("Improper inital gain values are given, set all to 0")
                self.gains=[0]*self.M
                self.last_gains=[0]*self.M

        # Database configuration (parsed BEFORE the schedule section so the
        # scheduler can be handed the database for schedule persist/resume)
        if parser.has_section('database'):
            self._en_db = parser.getint('database', 'en_db', fallback=0) == 1
            self._hw_snapshot_interval = parser.getint('database', 'hw_snapshot_interval', fallback=100)
            if self._en_db:
                try:
                    from daq_db import DAQDatabase
                    self.db = DAQDatabase(
                        db_dir=parser.get('database', 'db_dir', fallback='_db'),
                        max_db_size_mb=parser.getint('database', 'max_db_size_mb', fallback=500),
                        rotation_max_age_hours=parser.getint('database', 'rotation_max_age_hours', fallback=168),
                        write_batch_size=parser.getint('database', 'write_batch_size', fallback=50),
                        write_flush_interval_sec=parser.getfloat('database', 'write_flush_interval_sec', fallback=1.0),
                        num_channels=self.M
                    )
                    logging.info("DAQ database enabled")
                except Exception as e:
                    logging.error("Failed to initialize DAQ database: {:s}".format(str(e)))
                    self.db = None

        # Schedule configuration
        if parser.has_section('schedule'):
            en_schedule = parser.getint('schedule', 'en_schedule', fallback=0)
            if en_schedule:
                try:
                    from signal_scheduler import SignalScheduler, ScheduleParser
                    sample_rate = parser.getint('daq', 'sample_rate', fallback=2400000)
                    decimation_ratio = parser.getint('pre_processing', 'decimation_ratio', fallback=1)
                    # db= activates schedule-state persist/resume when the
                    # database is enabled too (no-op with db=None)
                    self.scheduler = SignalScheduler(self.M, sample_rate, self.N,
                                                     decimation_ratio, db=self.db)
                    self.scheduler.max_cal_wait_frames = parser.getint('schedule', 'max_cal_wait_frames', fallback=500)
                    sched_mode = parser.get('schedule', 'schedule_mode', fallback='none')
                    if sched_mode == 'inline':
                        sched = ScheduleParser.from_ini_section(parser, 'schedule')
                        if sched is not None:
                            self.scheduler.load_schedule(sched)
                            logging.info("Inline schedule loaded with {:d} entries".format(len(sched.entries)))
                    elif sched_mode == 'file':
                        sched_file = parser.get('schedule', 'schedule_file', fallback='')
                        if sched_file:
                            sched = ScheduleParser.from_file(sched_file)
                            self.scheduler.load_schedule(sched)
                            logging.info("File schedule loaded from {:s}".format(sched_file))
                except Exception as e:
                    logging.error("Failed to initialize scheduler: {:s}".format(str(e)))
                    self.scheduler = None

        # Monitoring configuration
        if parser.has_section('monitoring'):
            en_monitoring = parser.getint('monitoring', 'en_monitoring', fallback=0) == 1
            if en_monitoring:
                try:
                    from daq_events import (EventBus, LoggingHandler, SysLogEventHandler, ZMQPubHandler)
                    ring_size = parser.getint('monitoring', 'event_ring_size', fallback=500)
                    self.event_bus = EventBus(enabled=True, ring_size=ring_size)
                    self.event_bus.register_handler(LoggingHandler())

                    if parser.getint('monitoring', 'en_syslog', fallback=0) == 1:
                        syslog_handler = SysLogEventHandler(
                            address=parser.get('monitoring', 'syslog_address', fallback='/dev/log'),
                            facility=parser.get('monitoring', 'syslog_facility', fallback='daemon'),
                            min_severity=parser.get('monitoring', 'syslog_min_severity', fallback='warning')
                        )
                        self.event_bus.register_handler(syslog_handler)

                    logging.info("Event bus enabled (hw_controller)")
                except Exception as e:
                    logging.error("Failed to initialize event bus: {:s}".format(str(e)))
                    self.event_bus = None

        # Wire the event bus into the database (queue-full diagnostics; the
        # DAQDatabase docstring expects this to be set externally)
        if self.db is not None:
            self.db.event_bus = self.event_bus

        # Federation configuration
        if parser.has_section('federation'):
            self.instance_id = parser.getint('federation', 'instance_id', fallback=0)
            self.port_stride = parser.getint('federation', 'port_stride', fallback=100)
            if self.instance_id != 0:
                logging.info("Federation instance_id={:d}, port_stride={:d}".format(
                    self.instance_id, self.port_stride))

        # Offload / transport configuration
        if parser.has_section('offload'):
            self.transport_type = parser.get('offload', 'delay_sync_transport', fallback='shm')

        # RF front-end (amplification) + antenna profile configuration.
        # (Re)size per-channel budget arrays now that self.M is final.
        self.ext_lna_gains_db = [0.0]*self.M
        self.total_system_gains_db = [0.0]*self.M
        self.system_nf_db = [0.0]*self.M
        self.compression_flags = 0
        self.bias_tee_state = [0]*self.M
        if parser.has_section('antenna'):
            try:
                from antenna_profile import AntennaProfileParser
                self.antenna_profile = AntennaProfileParser.from_ini_section(parser, 'antenna')
                if self.antenna_profile is not None:
                    logging.info("Antenna profile '{:s}' loaded ({:.1f} dBi)".format(
                        self.antenna_profile.name, self.antenna_profile.element_gain_dbi))
            except Exception as e:
                logging.error("Failed to load antenna profile: {:s}".format(str(e)))
                self.antenna_profile = None
        if parser.has_section('amplification'):
            try:
                from gain_budget import RfFrontendParser
                self.rf_frontend = RfFrontendParser.from_ini_section(parser, self.M, 'amplification')
                if self.rf_frontend is not None:
                    self._en_amplification = True
                    self.ext_lna_gains_db = list(self.rf_frontend.ext_lna_gains_db)
                    self.bias_tee_state = list(self.rf_frontend.bias_tee_state)
                    logging.info("Amplification enabled: external LNA gains {0} dB".format(
                        self.ext_lna_gains_db))
            except Exception as e:
                logging.error("Failed to load RF front-end config: {:s}".format(str(e)))
                self.rf_frontend = None
                self._en_amplification = False

        # Antenna orientation controller (owns a rotator hardware backend)
        if parser.has_section('orientation'):
            try:
                from orientation_controller import OrientationController, OrientationParser
                from rotator_controller import create_rotator_backend
                ori_cfg = OrientationParser.from_ini_section(parser, 'orientation')
                if ori_cfg is not None:
                    backend = create_rotator_backend(ori_cfg)
                    self.orientation = OrientationController(ori_cfg, backend, self.M,
                                                             antenna_profile=self.antenna_profile)
                    logging.info("Antenna orientation controller enabled (backend={:s}, mode={:s})".format(
                        ori_cfg.backend, ori_cfg.bearing_mode))
            except Exception as e:
                logging.error("Failed to initialize orientation controller: {:s}".format(str(e)))
                self.orientation = None

    def init(self):
        """
            Initializes the Hardware Controller module
                - Opens inter-module communication sockets
                - Opens the control FIFOs
                - Initializes the shared memory data interface
                - Initializes the DAC controller module (ADPIS control)
                - Sets initial IQ values in the ADPIS
        """
        # Open RTL-DAQ control socket (bounded timeouts so a wedged/dead
        # rtl_daq degrades the control plane instead of deadlocking it)
        self.zmq_port = 1130 + self.instance_id * self.port_stride
        self.zmq_context = zmq.Context()
        self.rtl_daq_socket = self._create_rtl_daq_socket()

        # Open control FIFOs (namespaced by instance_id)
        if self.instance_id != 0:
            self.track_lock_ctr_fname = "_data_control/inst{:d}_iq_track_lock".format(self.instance_id)
            self._orientation_state_fname = "_data_control/inst{:d}_orientation_state".format(self.instance_id)
            self._bias_tee_state_fname = "_data_control/inst{:d}_bias_tee_state".format(self.instance_id)
        try:
            self.track_lock_ctr_fd = open(self.track_lock_ctr_fname, 'r')
        except OSError as err:
            self.logger.critical("OS error: {0}".format(err))
            self.logger.critical("Failed to open control fifos, exiting..")
            return -1
        # Initialize data input interface (shared memory or transport abstraction)
        if self.transport_type == 'shm':
            self.in_shmem_iface = inShmemIface("delay_sync_hwc", instance_id=self.instance_id)
        else:
            self.in_shmem_iface = TransportConsumer("delay_sync_hwc",
                                                     instance_id=self.instance_id,
                                                     transport_type=self.transport_type)
        if not self.in_shmem_iface.init_ok:
            logging.critical("Data input interface initialization failed (transport={:s})".format(self.transport_type))
            return -3
        # ADPIS
        if self.en_adpis:
            try:
                from dac_controller import DACController
            
                # Initialize DAC controller
                self.iq_mod = DACController(iface="I2C")
                if not self.iq_mod.init_status:
                    logging.critical("DAC Controller initialization failed")
                    return -2
                self.iq_mod.logger.setLevel(self.log_level)

                # Set inital IQ values
                for m in range(self.M-1):
                    self.iq_mod.set_IQ_value(0.5, 0.5, m)
            except:
                logging.error("DAC Controller initialization failed")

        # Open the antenna orientation controller (rotator hardware)
        if self.orientation is not None:
            try:
                self.orientation.open()
                self._write_orientation_state()
            except Exception as e:
                logging.error("Orientation controller open failed: {:s}".format(str(e)))

        # Publish the initial bias-tee state for delay_sync header stamping
        if self._en_amplification:
            self._write_bias_tee_state()
        return 0

    def _create_rtl_daq_socket(self):
        """Create the REQ socket toward rtl_daq with bounded send/recv timeouts."""
        sock = self.zmq_context.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, self.ZMQ_TIMEOUT_MS)
        sock.setsockopt(zmq.SNDTIMEO, self.ZMQ_TIMEOUT_MS)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect("tcp://localhost:{:d}".format(self.zmq_port))
        return sock

    def _rtl_daq_transaction(self, msg_byte_array):
        """Send one 128-byte control message to rtl_daq and wait for the reply.

        Bounded by the socket timeouts. On timeout/error the strict REQ/REP
        lockstep would brick the socket, so it is closed and rebuilt (lazy
        pirate pattern) and None is returned; the caller must treat None as
        'command not confirmed' and carry on.
        """
        try:
            self.rtl_daq_socket.send(msg_byte_array)
            reply = self.rtl_daq_socket.recv()
            self.logger.debug(f"Received reply: {reply}")
            return reply
        except zmq.ZMQError as e:
            self.logger.critical("rtl_daq control transaction failed (%s), rebuilding socket", e)
            try:
                self.rtl_daq_socket.close(linger=0)
            except zmq.ZMQError:
                pass
            self.rtl_daq_socket = self._create_rtl_daq_socket()
            return None

    @staticmethod
    def _atomic_write(fname, content):
        """Write a small control file atomically (temp file + rename) so a
        concurrent reader never observes a torn/partial value."""
        tmp_fname = fname + '.tmp'
        try:
            with open(tmp_fname, 'w') as f:
                f.write(content)
            os.replace(tmp_fname, fname)
        except OSError:
            pass

    def _write_orientation_state(self):
        """Relay the current antenna bearing + rotator state to delay_sync via a
        small control file (throttled), so delay_sync can stamp them into the
        outbound IQ header reserved region for the downstream DoA app."""
        if self.orientation is None:
            return
        self._atomic_write(self._orientation_state_fname,
                           "{:.3f} {:.3f} {:d}".format(
                               self.orientation.cmd_az, self.orientation.cmd_el,
                               self.orientation.rotator_state_enum()))

    def _write_bias_tee_state(self):
        """Relay the per-channel bias-tee state (as a bitmask) to delay_sync so
        it can stamp the v8 RSV_BIAS_TEE_STATE header slot."""
        mask = 0
        for m, state in enumerate(self.bias_tee_state):
            if state:
                mask |= (1 << m)
        self._atomic_write(self._bias_tee_state_fname, "{:d}".format(mask))

    def close(self):
        """
            Close the communication and data interfaces that are opened during the start of the module
        """
        if self.track_lock_ctr_fd is not None:
            self.track_lock_ctr_fd.close()

        if self.in_shmem_iface is not None:
            self.in_shmem_iface.destory_sm_buffer()

        if self.db is not None:
            self.db.close()

        if self.orientation is not None:
            self.orientation.close()

        self.logger.info("Module interfaces are closed")


    def _enable_agc(self):
        if self.agc:
            msg_byte_array = inter_module_messages.pack_msg_enable_agc(self.module_identifier)
            self._rtl_daq_transaction(msg_byte_array)


    def _change_gains(self):
        """
            Sends gain tuning request to the receiver module through the 
            receiver configuration FIFO
            
            Used important object parameter:
            
            :param: self.gains: Gain index array
            :type: gains: list of integers [gain index0, gain index1 , ..]
        """
        if self.unified_gain_control:
            # Force the same gain value for all the channels
            self.gains = [min(self.gains)]*self.M
            
        # Prepare gain list
        gains=[]
        for m in range(self.M):
            gains.append(self.valid_gains[self.gains[m]])
            self.logger.info("Send Ch {:d} Gain: {:d} [{:d}]".format(m, int(gains[m]), self.iq_header.cpi_index))
        # Send gain list
        msg_byte_array = inter_module_messages.pack_msg_set_gain(self.module_identifier, gains)
        self._rtl_daq_transaction(msg_byte_array)
        # Recompute the external-LNA link budget (advisory; does not change tuner gains)
        self._update_link_budget(gains)
    def _tune_gains(self):
        """
            Performs IF gain tuning in order to maximaze the SINR in each channels by
            minimizing the effect of the quantization noise.
        """
        # Check gain states
        for m in range(self.M):
            if self.valid_gains[self.gains[m]] != self.iq_header.if_gains[m]:
                self.logger.error("Incosistent IF gains. Tuning is bypassed")
                return -1

        for m in range(self.M):
            # Check overdrive
            if self.iq_header.adc_overdrive_flags & 1<<m:
                self.logger.warning("ADC overdriven at channel: {:d}".format(m))
                if self.gains[m] != 0:
                    self.gains[m] -=1
                else:
                    self.logger.error("Overdrive detected, but the gain can not be decreased further")
                self.gain_tune_states[m]=False
                if self.unified_gain_control: # Disable tuning in all channels
                    self.gain_tune_states = [False]*self.M
            # Perform gain change if tuning is not finished
            if self.gain_tune_states[m]:
                if self.gains[m] < len(self.valid_gains)-1:
                    self.gains[m] +=1
                else:
                    self.logger.warning("Maximum gain reached, without overdrive, ch: {:d}".format(m))
                    self.gain_tune_states[m]=False
                    if self.unified_gain_control: # Disable tuning in all chanels
                        self.gain_tune_states = [False]*self.M 
        self._change_gains()

    def _current_tuner_gains_tenths(self):
        """Return the current per-channel tuner IF gains in tenths of dB."""
        return [self.valid_gains[self.gains[m]] for m in range(self.M)]

    def _update_link_budget(self, gains_tenths):
        """Recompute per-channel total system gain, cascaded noise figure, and
        compression flags from the current tuner gains plus the external LNA
        chain. Advisory only — it never changes the tuner gains or if_gains.

        :param gains_tenths: current per-channel tuner IF gain, tenths of dB
        """
        if not self._en_amplification or self.rf_frontend is None:
            return
        # Keep the runtime-editable external gains in sync with the config object
        self.rf_frontend.ext_lna_gains_db = list(self.ext_lna_gains_db)
        comp_flags = 0
        for m in range(self.M):
            tuner_gain_db = gains_tenths[m] / 10.0
            budget = self.rf_frontend.channel_budget(
                m, tuner_gain_db,
                antenna_profile=self.antenna_profile,
                freq_hz=self.rf_center_frequency)
            self.total_system_gains_db[m] = budget["total_gain_db"]
            self.system_nf_db[m] = budget["system_nf_db"]
            if budget["compressed"]:
                comp_flags |= (1 << m)
                self.logger.warning("Possible external-LNA compression ch {:d} (headroom {:.1f} dB)".format(
                    m, budget["p1db_headroom_db"]))
            if budget["over_max_gain"]:
                self.logger.warning("Ch {:d} total system gain {:.1f} dB exceeds max {:.1f} dB".format(
                    m, budget["total_gain_db"], self.rf_frontend.max_total_gain_db))
        prev = self.compression_flags
        self.compression_flags = comp_flags
        if comp_flags and comp_flags != prev and self.event_bus is not None:
            from daq_events import DAQEvent, EVT_COMPRESSION
            self.event_bus.emit(DAQEvent(severity="warning", module="hw_controller",
                event_type=EVT_COMPRESSION, payload={"flags": comp_flags,
                    "total_gains_db": list(self.total_system_gains_db)}))

    def _handle_control_reqest(self, command, params):
        """
            Handles external control requests that has arrived thorugh the control Ethernet interface

            Parameters:
            -----------
            :param: command: String identifier of the requested configuration command 
            :param: params : List of parameters of the configuration command

            :type: command : string (4 character length)
            :type: params  : python list

            Implemented valid command strings:
            - FREQ: Changes the center frequency of the receiver
            - GAIN: Sets the IF gain values

        """
        if command == "FREQ":
            # The client frame carries an uint64, but the ZMQ 'c' message toward
            # rtl_daq is an uint32 (wire contract). Range-check here instead of
            # letting struct.error kill the main loop.
            frequency = int(params[0])
            if not (0 < frequency < 2**32):
                self.logger.error("Requested center frequency out of range: {:d} Hz".format(frequency))
                return
            msg_byte_array = inter_module_messages.pack_msg_rf_tune(self.module_identifier, frequency)
            if self._rtl_daq_transaction(msg_byte_array) is None:
                # Not confirmed by rtl_daq: the tuner may still be on the old
                # frequency, so do not record a retune that never happened.
                self.logger.error("FREQ change to {:d} Hz not confirmed by rtl_daq".format(frequency))
                return
            # Track the retune so the link budget uses the live RF frequency
            self.rf_center_frequency = frequency
            if self._en_amplification:
                self._update_link_budget(self._current_tuner_gains_tenths())
            if self.db is not None:
                self.db.put_cal_event(CAL_EVENT_FREQ_CHANGE, self.iq_header)
            if self.event_bus is not None:
                from daq_events import DAQEvent, EVT_FREQ_CHANGE
                self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                    event_type=EVT_FREQ_CHANGE, payload={"frequency": frequency}))
        elif command == "GAIN":
            try:
                if self.noise_source_state: # The noise source is turned on, we are storing only the gains
                    for m in range(self.M):
                        self.last_gains[m] = self.valid_gains.index(params[m])
                else:
                    for m in range(self.M):
                        self.gains[m] = self.valid_gains.index(params[m])
                    self._change_gains()
                    if self.db is not None:
                        self.db.put_cal_event(CAL_EVENT_GAIN_CHANGE, self.iq_header)
                    if self.event_bus is not None:
                        from daq_events import DAQEvent, EVT_GAIN_CHANGE
                        self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                            event_type=EVT_GAIN_CHANGE, payload={"gains": list(params)}))
                # Setting gain implies disabling AGC
                self.agc = False
            except ValueError:
                self.logger.error("Improper gain value {:d}".format(params[m]))
        elif command == "AGC ":
            if self.noise_source_state: # The noise source is turned on, we are only storing the AGC state
                self.last_agc = True
            else:
                self.agc = True
                self._enable_agc()
        elif command == "SCHD":
            # Load schedule from JSON payload or file path
            if self.scheduler is not None:
                try:
                    from signal_scheduler import ScheduleParser
                    payload = params[0] if params else ""
                    if isinstance(payload, str) and payload.strip():
                        if payload.strip().startswith('{'):
                            sched = ScheduleParser.from_json(payload)
                        else:
                            sched = ScheduleParser.from_file(payload.strip())
                        self.scheduler.load_schedule(sched)
                        self.logger.info("Schedule loaded via control command")
                except Exception as e:
                    self.logger.error("Failed to load schedule: {:s}".format(str(e)))
        elif command == "SCHS":
            # Stop/clear schedule
            if self.scheduler is not None:
                self.scheduler.clear_schedule()
        elif command == "SCHQ":
            # Query status (logged for now)
            if self.scheduler is not None:
                status = self.scheduler.get_status()
                self.logger.info("Schedule status: {:s}".format(str(status)))
        elif command == "SCHN":
            # Skip to next entry
            if self.scheduler is not None:
                self.scheduler.skip_to_next()
        elif command == "EGAN":
            # Set per-channel external LNA gain (received as tenths of dB)
            try:
                for m in range(self.M):
                    self.ext_lna_gains_db[m] = params[m] / 10.0
                self._update_link_budget(self._current_tuner_gains_tenths())
                self.logger.info("External LNA gains set: {0} dB".format(self.ext_lna_gains_db))
                if self.event_bus is not None:
                    from daq_events import DAQEvent, EVT_EXT_GAIN_CHANGE
                    self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                        event_type=EVT_EXT_GAIN_CHANGE,
                        payload={"ext_lna_gains_db": list(self.ext_lna_gains_db),
                                 "total_gains_db": list(self.total_system_gains_db)}))
            except (IndexError, TypeError):
                self.logger.error("Improper external gain values: {0}".format(params))
        elif command == "RFQ ":
            # Query the current link budget
            self.logger.info("Link budget: total gains {0} dB, NF {1} dB, compression flags {2}".format(
                [round(g, 1) for g in self.total_system_gains_db],
                [round(n, 2) for n in self.system_nf_db], self.compression_flags))
        elif command == "BIAS":
            # Runtime inline-LNA bias-tee toggle (requires en_bias_tee_runtime=1)
            if self.rf_frontend is not None and self.rf_frontend.en_bias_tee_runtime:
                try:
                    states = [1 if params[m] else 0 for m in range(self.M)]
                    self.bias_tee_state = states
                    msg_byte_array = inter_module_messages.pack_msg_set_bias_tee(self.module_identifier, states)
                    self._rtl_daq_transaction(msg_byte_array)
                    self.logger.info("Bias-tee state set: {0}".format(states))
                    # Relay to delay_sync for v8 header stamping
                    self._write_bias_tee_state()
                    if self.event_bus is not None:
                        from daq_events import DAQEvent, EVT_BIAS_TEE_CHANGE
                        self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                            event_type=EVT_BIAS_TEE_CHANGE, payload={"bias_tee_state": list(states)}))
                except (IndexError, TypeError):
                    self.logger.error("Improper bias-tee states: {0}".format(params))
            else:
                self.logger.warning("Bias-tee runtime control disabled (set en_bias_tee_runtime=1)")
        elif command == "ORNT":
            # Slew the antenna to a commanded azimuth/elevation
            if self.orientation is not None:
                try:
                    az, el = float(params[0]), float(params[1])
                    self.orientation.set_bearing(az, el)
                    self._write_orientation_state()
                    if self.event_bus is not None:
                        from daq_events import DAQEvent, EVT_ORIENTATION_SLEW
                        self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                            event_type=EVT_ORIENTATION_SLEW, payload={"az_deg": az, "el_deg": el}))
                except (IndexError, ValueError, TypeError):
                    self.logger.error("Improper orientation bearing: {0}".format(params))
            else:
                self.logger.warning("Orientation control disabled (set en_orientation=1)")
        elif command == "PARK":
            if self.orientation is not None:
                self.orientation.park()
                if self.event_bus is not None:
                    from daq_events import DAQEvent, EVT_ORIENTATION_PARK
                    self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                        event_type=EVT_ORIENTATION_PARK, payload={}))
        elif command == "SCAN":
            if self.orientation is not None:
                self.orientation.start_scan()
                if self.event_bus is not None:
                    from daq_events import DAQEvent, EVT_ORIENTATION_SCAN_START
                    self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                        event_type=EVT_ORIENTATION_SCAN_START, payload={}))
        elif command == "OSTP":
            if self.orientation is not None:
                self.orientation.stop()
        elif command == "OQRY":
            if self.orientation is not None:
                self.logger.info("Orientation status: {0}".format(self.orientation.get_status()))
        elif command == "INIT":
            # Re-run the hardware initialization sequence: restore configured
            # gains and disable the noise source by re-entering STATE_INIT.
            # _drain_control_requests defers this command while a calibration
            # noise burst is active, so self.gains hold the user gains here.
            self.logger.info("Re-initialization requested, re-entering STATE_INIT")
            self.current_state = "STATE_INIT"


    # Read-only query verbs: replied with 'FNSD' + NUL-padded UTF-8 JSON in
    # bytes 4-127 of the 128-byte reply frame; safe to execute in any FSM state.
    QUERY_COMMANDS = ("RFQ ", "OQRY", "SCHQ")

    def _handle_query_request(self, command):
        """Build the JSON reply payload (bytes, <= 124) for a query verb.

        Reply shapes (flat, documented for external clients):
          RFQ  -> {"gains": [per-ch total system gain dB, 1 decimal],
                   "nf": [per-ch system NF dB, 2 decimals],
                   "comp": <compression bitmask int>}
          OQRY -> {"az": <deg>, "el": <deg>, "state": <int enum
                   0=IDLE 1=SLEWING 2=SETTLED 3=SCANNING>}
          SCHQ -> {"state": <str>, "active": <bool>, "idx": <int>,
                   "total": <int>, "freq": <int Hz>, "frames": <int>,
                   "dwell": <int>} (only {"state","active"} without a schedule)
        """
        try:
            if command == "RFQ ":
                data = {"gains": [round(g, 1) for g in self.total_system_gains_db],
                        "nf": [round(n, 2) for n in self.system_nf_db],
                        "comp": int(self.compression_flags)}
            elif command == "OQRY":
                if self.orientation is not None:
                    status = self.orientation.get_status()
                    data = {"az": status["bearing_az_deg"],
                            "el": status["bearing_el_deg"],
                            "state": self.orientation.rotator_state_enum()}
                else:
                    data = {"az": 0.0, "el": 0.0, "state": 0}
            elif command == "SCHQ":
                if self.scheduler is not None:
                    status = self.scheduler.get_status()
                    data = {"state": status.get("state"),
                            "active": status.get("active", False)}
                    if status.get("active", False):
                        data.update({"idx": status.get("current_index", 0),
                                     "total": status.get("total_entries", 0),
                                     "freq": status.get("current_freq", 0),
                                     "frames": status.get("frames_at_current", 0),
                                     "dwell": status.get("dwell_frames", 0)})
                else:
                    data = {"state": "IDLE", "active": False}
            else:
                data = {"error": "unknown query"}
            payload = json.dumps(data, separators=(',', ':')).encode('utf-8')
            if len(payload) > 124:  # trim rounding, then give up gracefully
                if command == "RFQ ":
                    data["gains"] = [int(round(g)) for g in self.total_system_gains_db]
                    data["nf"] = [int(round(n)) for n in self.system_nf_db]
                    payload = json.dumps(data, separators=(',', ':')).encode('utf-8')
                if len(payload) > 124:
                    payload = b'{"error":"overflow"}'
            return payload
        except Exception as e:
            self.logger.error("Query handling failed ({:s}): {:s}".format(command, str(e)))
            return b'{"error":"internal"}'

    def _drain_control_requests(self, allow_mutations):
        """Drain pending external control requests from the CtrIfaceServer.

        Query verbs are read-only and served in ANY state. Hardware-mutating
        commands are executed only when `allow_mutations` is True (the FSM is
        in the safe STATE_IQ_CAL state on a non-dummy frame); otherwise the
        request stays pending and is retried on the next frame, keeping the
        original safety property that hardware is reconfigured only between
        calibration activities.
        """
        work = self._pending_ctr_requests
        self._pending_ctr_requests = []
        while True:
            try:
                work.append(ctr_request_queue.get_nowait())
            except queue.Empty:
                break
        for request in work:
            if request.cancelled:
                # The server thread stopped waiting (timeout) and already
                # replied: withdraw the request instead of executing it at an
                # arbitrary later frame. Known limitation: a request that
                # begins executing just before the flag is set still runs.
                self.logger.warning("Dropping timed-out control request: {:s}".format(
                    request.command))
                continue
            if request.command in self.QUERY_COMMANDS:
                request.reply_payload = self._handle_query_request(request.command)
                request.done.set()
            elif allow_mutations and not (request.command == "INIT" and
                                          self.noise_source_state):
                # INIT is additionally deferred while a calibration noise
                # burst is active: re-entering STATE_INIT with the cal-table
                # gains loaded would overwrite the saved user gains and leave
                # noise_source_state stuck True.
                self.logger.info("Control request: {:s}".format(request.command))
                try:
                    self._handle_control_reqest(request.command, request.params)
                except Exception as e:
                    self.logger.error("Control request {:s} failed: {:s}".format(
                        request.command, str(e)))
                request.done.set()
            else:
                # Not safe to touch the hardware now - retry next frame
                # (relative order of deferred mutating commands is preserved)
                self._pending_ctr_requests.append(request)

    def _control_noise_source(self, noise_source_state):
        """
            Enables or disables the internal noise of the receiver and set the proper gain values.
            
            Gain and frequency values are fittet to the hardware of the Kerberos SDR v2
            You can change here the preset gain values depending on the calibration frequency here!

            Parameters:
            -----------
            :param: noise_source_state: Requested state of the noise control 
                    (When set to True, the noise source will be enabled)
            :type : noise_source_state: Bool

        """
        if noise_source_state:
            self.logger.info("Set gain values to perform calibration") 
            
            self.last_agc = self.agc
            self.agc = False

            for m in range(self.M):
                self.last_gains[m]=self.gains[m]
                self.gains[m]=self.cal_gain_table[1,np.argmin(abs(self.cal_gain_table[0,:]-self.iq_header.rf_center_freq))]
            self._change_gains()

            self.logger.info("Enable noise source, [{:d}]".format(self.iq_header.cpi_index))
            msg_byte_array = inter_module_messages.pack_msg_noise_source_ctr(self.module_identifier, True)
            self._rtl_daq_transaction(msg_byte_array)
            if self.db is not None:
                self.db.put_cal_event(CAL_EVENT_NOISE_ON, self.iq_header)
            if self.event_bus is not None:
                from daq_events import DAQEvent, EVT_NOISE_SOURCE_ON
                self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                    event_type=EVT_NOISE_SOURCE_ON, payload={"cpi_index": self.iq_header.cpi_index}))
            self.noise_source_state = True # Next state
            self.current_state = "STATE_NOISE_CTR_WAIT"
        else:
            self.logger.info("Disabling noise source [{:d}]".format(self.iq_header.cpi_index))
            msg_byte_array = inter_module_messages.pack_msg_noise_source_ctr(self.module_identifier, False)
            self._rtl_daq_transaction(msg_byte_array)
            if self.db is not None:
                self.db.put_cal_event(CAL_EVENT_NOISE_OFF, self.iq_header)
            if self.event_bus is not None:
                from daq_events import DAQEvent, EVT_NOISE_SOURCE_OFF
                self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                    event_type=EVT_NOISE_SOURCE_OFF, payload={"cpi_index": self.iq_header.cpi_index}))
            self.noise_source_state = False # Next state

            self.logger.info("Restore gain values after calibration")            
            for m in range(self.M):                
                self.gains[m] = self.last_gains[m]
            self._change_gains()

            self.logger.info("Restore AGC state after calibration")
            self.agc = self.last_agc
            self._enable_agc()

            self.current_state = "STATE_NOISE_CTR_WAIT"


    def start(self):
        """
            Start the main processing loop
        """
        while True:
            
            #############################################
            #           OBTAIN NEW DATA BLOCK           #  
            #############################################
                        
            # Obtained new data
            active_buff_index = self.in_shmem_iface.wait_buff_free()
            if active_buff_index == TERMINATE:
                self.logger.info("Terminate signal received, exiting")
                break
            elif active_buff_index == -2:  # Read timeout - keep the loop alive
                self.logger.warning("IQ frame wait timed out, retrying..")
                continue
            elif active_buff_index < 0 or active_buff_index > 1:
                logging.critical("Failed to acquire iq frame, exiting ..")
                break;

            buffer = self.in_shmem_iface.buffers[active_buff_index]
            iq_header_bytes = buffer[0:1024].tobytes()
            self.iq_header.decode_header(iq_header_bytes)            
            #self.iq_header.dump_header()

            if self.iq_header.check_sync_word():
                logging.critical("IQ header sync word check failed, exiting..")
                break
                
            # IQ samples are currently not required in this module, hence this section is disabled
            # incoming_payload_size = self.iq_header.cpi_length*self.iq_header.active_ant_chs*2*int(self.iq_header.sample_bit_depth/8)
            # if incoming_payload_size > 0:
            	# iq_samples = buffer[1024:1024 + incoming_payload_size].view(dtype=np.complex64).reshape(self.iq_header.active_ant_chs, self.iq_header.cpi_length) [:,0:self.N_proc] 
                
            self.logger.debug("Type:{:d}, CPI: {:d}, State:{:s}".format(self.iq_header.frame_type, self.iq_header.cpi_index, self.current_state))

            # --> Periodic HW snapshot
            if self.db is not None:
                self._hw_snapshot_counter += 1
                if self._hw_snapshot_counter >= self._hw_snapshot_interval:
                    self._hw_snapshot_counter = 0
                    self.db.put_hw_snapshot(self.iq_header,
                                            overdrive_events=self.iq_header.adc_overdrive_flags)

            ##############################################
            #  Hardware Controller Finite State Machine  #
            ##############################################
            
            if self.iq_header.frame_type != IQHeader.FRAME_TYPE_DUMMY : # Not Dummy Frame 
                
                # -> Check power levels and gain values from the header
                if self.iq_header.frame_type == IQHeader.FRAME_TYPE_DATA:
                    for m in range(self.M):
                        power = 0 # TODO: Read out from the header
                        self.logger.debug("Channel {:d} power:{:.2f} dB, gain:{:d} [{:d}]".format(m, power, 
                                        self.iq_header.if_gains[m], self.iq_header.cpi_index))                                            

                # -> Chech overdrive per channel
                overdrive_detected = False
                for m in range(self.M):
                    if(self.iq_header.adc_overdrive_flags & 1<<m):
                        self.logger.warning("Overdrive ch {:d} [{:d}]".format(m, self.iq_header.cpi_index))
                        overdrive_detected = True
                if overdrive_detected and self.event_bus is not None:
                    from daq_events import DAQEvent, EVT_OVERDRIVE
                    self.event_bus.emit(DAQEvent(severity="warning", module="hw_controller",
                        event_type=EVT_OVERDRIVE, payload={"flags": self.iq_header.adc_overdrive_flags,
                            "cpi_index": self.iq_header.cpi_index}))

                #
                #------------------------------------------>
                #            
                if self.current_state == "STATE_INIT": 
                    # Set initial gain values
                    self._change_gains()

                    # Disable internal noise source
                    msg_byte_array = inter_module_messages.pack_msg_noise_source_ctr(self.module_identifier, False)
                    self._rtl_daq_transaction(msg_byte_array)

                    # TODO: Set initial ADPIS values here
                    if any(self.gain_tune_states):
                        self.current_state = "STATE_GAIN_CTR_WAIT" 
                    else:
                        self.current_state = "STATE_IQ_CAL" 
                #
                #------------------------------------------>
                #   
                elif self.current_state == "STATE_GAIN_CTR_WAIT":
                    gain_ctr_ready = True
                    # Check gain states
                    for m in range(self.M):
                        if self.valid_gains[self.gains[m]] != self.iq_header.if_gains[m]:
                            gain_ctr_ready = False
                    if gain_ctr_ready:
                        self.current_state = "STATE_GAIN_TUNE"

                #
                #------------------------------------------>
                #   
                elif self.current_state == "STATE_GAIN_TUNE":
                    # Decrease gain if overdrive is detected
                    if self.iq_header.adc_overdrive_flags:
                        self._tune_gains()
                        self.current_state="STATE_GAIN_CTR_WAIT"
                        self.gain_lock_cntr = 0

                    # Increse gain to drive full scale
                    elif any(self.gain_tune_states):
                        self._tune_gains()
                        self.current_state="STATE_GAIN_CTR_WAIT"
                        self.gain_lock_cntr = 0

                    else: # Gain tuning may finished - check gain lock
                        if self.gain_lock_cntr == self.gain_lock_interval:
                            self.current_state = "STATE_IQ_CAL"
                            self.gain_lock_cntr = 0
                        else:
                            self.gain_lock_cntr +=1

                #
                #------------------------------------------>
                #
                elif self.current_state == "STATE_IQ_CAL":             

                    # --> Noise Source Control 
                    if  self.cal_track_mode == 0 or self.cal_track_mode == 1 or \
                        (self.cal_track_mode == 2 and self.cal_frame_cntr < self.cal_frame_interval):
                        
                        # Enable noise source 
                        if self.iq_header.sync_state < 5 : # Delay synchronizer is not in track or track lock mode
                            if self.iq_header.noise_source_state == 0:
                                self._control_noise_source(noise_source_state=True)
                        else: # Delay synchronizer is in track mode - Disable Noise Source
                            if self.iq_header.noise_source_state == 1: # Has sync, disable noise source if enabled                                    
                               # Check Track lock approval (Used, when the system is calibrated and the antennas are detached)
                               track_lock_approval = True
                               if self.require_track_lock_intervention:
                                   self.track_lock_ctr_fd.seek(0, 0)
                                   ctr_char = self.track_lock_ctr_fd.read(1)
                                   if ctr_char == '1':
                                       logging.info("User has approved track lock state initialization")
                                       self.track_lock_ctr_fd.seek(0, 0)
                                   else:
                                       logging.info("Waiting for track lock init. approval")
                                       track_lock_approval = False                               
                               if track_lock_approval:
                                   self._control_noise_source(noise_source_state=False)
                       
                    # --> Burst calibration frames
                    if self.cal_track_mode == 2: 
                        if self.iq_header.sync_state == 6:
                            self.cal_frame_cntr +=1
                            if self.cal_frame_cntr == self.cal_frame_interval:
                                self.logger.info("Enable noise source burst [{:d}]".format(self.iq_header.cpi_index))
                                self._control_noise_source(noise_source_state=True)
                                
                            elif self.cal_frame_cntr == self.cal_frame_interval+self.cal_frame_burst_size:
                                self.logger.info("Disabling noise source burst [{:d}]".format(self.iq_header.cpi_index))
                                self._control_noise_source(noise_source_state=False)
                                self.cal_frame_cntr=0
                        else: # self.iq_header.sync_state < 6
                            self.cal_frame_cntr = 0
                    
                    # --> Scheduler tick
                    if self.scheduler is not None:
                        sched_cmd = self.scheduler.tick(self.iq_header)
                        if sched_cmd is not None:
                            self.logger.info("Schedule command: {:s}".format(sched_cmd[0]))
                            if self.event_bus is not None:
                                from daq_events import DAQEvent, EVT_SCHEDULE_TRANSITION
                                self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                                    event_type=EVT_SCHEDULE_TRANSITION, payload={
                                        "command": sched_cmd[0], "value": sched_cmd[1]}))
                            self._handle_control_reqest(sched_cmd[0], [sched_cmd[1]])
                            if sched_cmd[0] == "FREQ":
                                self.cal_frame_cntr = 0  # Reset burst counter for new freq
                                # Issue gain command if entry has gains
                                gain_cmd = self.scheduler.get_pending_gain()
                                if gain_cmd is not None:
                                    self._handle_control_reqest(gain_cmd[0], gain_cmd[1])

                    # --> Antenna orientation tick (slew/settle/scan-and-peak)
                    if self.orientation is not None:
                        ori_event = self.orientation.tick(self.iq_header)
                        # Relay current bearing/state to delay_sync (throttled write)
                        self._orientation_write_counter += 1
                        if self._orientation_write_counter >= 5 or ori_event is not None:
                            self._orientation_write_counter = 0
                            self._write_orientation_state()
                        if ori_event is not None and self.event_bus is not None:
                            from daq_events import (DAQEvent, EVT_ORIENTATION_SETTLED,
                                                    EVT_ORIENTATION_SCAN_PEAK)
                            etype = (EVT_ORIENTATION_SCAN_PEAK
                                     if ori_event[1].get("event") == "scan_peak"
                                     else EVT_ORIENTATION_SETTLED)
                            self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                                event_type=etype, payload=ori_event[1]))

                #
                #------------------------------------------>
                #   
                elif self.current_state == "STATE_NOISE_CTR_WAIT":
                    if self.noise_source_state == self.iq_header.noise_source_state:
                        gain_ctr_ready = True
                        # Check gain states
                        for m in range(self.M):
                            if self.valid_gains[self.gains[m]] != self.iq_header.if_gains[m]:
                                gain_ctr_ready = False
                        if gain_ctr_ready:
                            self.current_state = "STATE_IQ_CAL"

            # --> External control request handling (every frame: queries are
            #     answered in any state; hardware mutations only in the safe
            #     STATE_IQ_CAL state on non-dummy frames)
            self._drain_control_requests(
                allow_mutations=(self.current_state == "STATE_IQ_CAL" and
                                 self.iq_header.frame_type != IQHeader.FRAME_TYPE_DUMMY))

            self.in_shmem_iface.send_ctr_buff_ready(active_buff_index)
            
                        

class CtrIfaceServer(threading.Thread):
    """
        TCP control interface server (port 5001 + instance offset).

        Wire protocol (fixed, external contract):
          Request : exactly 128 bytes = 4-byte ASCII command + 124-byte payload.
          Reply   : exactly 128 bytes starting with 'FNSD'.
                    - non-query commands: bytes 4-127 are all zeros
                    - query commands (RFQ /OQRY/SCHQ): bytes 4-127 carry a
                      NUL-padded UTF-8 JSON object (see HWC._handle_query_request)

        Command payload layouts:
          FREQ = uint64 LE @4, GAIN/EGAN/BIAS = M x uint32 LE @4,
          ORNT = 2 x float32 LE @4, SCHD = UTF-8 path/JSON @4,
          STHU = float32 @4 (accepted, currently not implemented),
          AGC /INIT/SCHS/SCHQ/SCHN/RFQ /PARK/SCAN/OSTP/OQRY/EXIT = no payload.
    """

    # How long the server waits for the main loop to execute one request
    REQUEST_TIMEOUT_S = 10.0

    def __init__(self, M, port=5001, listen_address="0.0.0.0"):
        """
            Initialize the Ethernet socket based control interface
            Parameters:
            -----------
            :param: M: Number of receiver channels in the system
            :type:  M: int
            :param: port: TCP port number for the control interface
            :type:  port: int
            :param: listen_address: Bind address ([daq] listen_address, default all interfaces)
            :type:  listen_address: string
        """

        self.logger = logging.getLogger(__name__)
        threading.Thread.__init__(self, daemon=True)

        # Control interface server parameters
        self.ctr_iface_port_no = port
        self.ctr_iface_socket =  socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ctr_iface_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ctr_iface_addr = (listen_address, self.ctr_iface_port_no)
        self.M = M
        self.status=True
        self._shutdown_requested = False

    def shutdown(self):
        """Request a clean server stop: closing the listening socket unblocks
        accept() and the run loop exits."""
        self._shutdown_requested = True
        try:
            self.ctr_iface_socket.close()
        except OSError:
            pass

    @staticmethod
    def _recv_exact(connection, length):
        """Receive exactly `length` bytes (TCP gives no message boundaries).
        Returns None if the peer closes the connection first."""
        data = bytearray()
        while len(data) < length:
            chunk = connection.recv(length - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    def run(self):
        """
            Starts the control server service thread
        """

        self.logger.info("Opening control interface server on ip address: {:s} and port: {:d}".format(
            self.ctr_iface_addr[0], self.ctr_iface_port_no))
        try:
            self.ctr_iface_socket.bind(self.ctr_iface_addr)
            self.ctr_iface_socket.listen(4)
        except socket.error:
            self.logger.error("Unable to open the Hardware configuration interface")
            self.status=False
            return -1

        while not self._shutdown_requested:
            # Wait for a connection
            self.logger.info("Waiting for new connection")
            try:
                connection, client_address = self.ctr_iface_socket.accept()
            except OSError:
                break  # Listening socket closed by shutdown()

            try:
                self.logger.info("Conenction established ")

                # Main server loop
                while True:
                    ctr_frame = self._recv_exact(connection, 128)
                    if ctr_frame is None:
                        self.logger.info("No more data from client, closing connection")
                        break
                    exit_flag, reply = self.process_ctr_frame(ctr_frame)
                    if exit_flag:
                        break
                    connection.sendall(reply)
            except Exception as e:
                # A misbehaving client must never take the control plane down
                self.logger.error("Control connection error: {:s}".format(str(e)))
            finally:
                # Clean up the connection
                connection.close()

        self.logger.info("Control interface server stopped")

    def _submit_request(self, command, params):
        """Enqueue one request for the HWC main loop and wait for completion.
        Returns the 128-byte reply frame."""
        request = CtrRequest(command, params)
        try:
            ctr_request_queue.put(request, timeout=2.0)
        except queue.Full:
            self.logger.error("Control request queue full, rejecting {:s}".format(command))
            if command in HWC.QUERY_COMMANDS:
                return self._build_reply(b'{"error":"busy"}')
            return self._build_reply(None)
        if not request.done.wait(timeout=self.REQUEST_TIMEOUT_S):
            self.logger.warning("Control request {:s} timed out waiting for the main loop".format(command))
            # Withdraw the request so the main loop does not execute it at an
            # arbitrary later frame (checked by _drain_control_requests before
            # execution). Documented limitation: the frozen wire contract has
            # no distinct timeout reply for non-query verbs, so the client
            # receives the standard 'FNSD' ack even though the command was
            # never applied; a request that starts executing right before the
            # flag is set can still complete.
            request.cancelled = True
            if command in HWC.QUERY_COMMANDS:
                return self._build_reply(b'{"error":"busy"}')
            return self._build_reply(None)
        return self._build_reply(request.reply_payload)

    @staticmethod
    def _build_reply(payload):
        """Assemble the fixed 128-byte reply frame: 'FNSD' + 124 payload bytes
        (NUL-padded; all zeros when there is no payload)."""
        if payload:
            payload = payload[:124]
            return b"FNSD" + payload + bytes(124 - len(payload))
        return b"FNSD" + bytes(124)

    def process_ctr_frame(self, msg_bytes):
        """
            Processes one control interface message: decodes the command,
            hands it to the HWC main loop, and assembles the reply frame.

            The input message is composed as follows:
            Total length: 128 byte
            -----------------------------------
            |4 byte cmd|...124 byte payload...|
            -----------------------------------
            See the class docstring for the per-command payload layouts.

            Parameters:
            -----------
            :param: msg_bytes: Received command, that has to be processed
            :type : msg_bytes: 128 byte length byte array

            Return values:
            --------------
            :return: (exit_flag, reply): exit_flag True closes the connection
                     (no reply is sent); otherwise reply is the 128-byte frame
                     to send back.
        """
        command = msg_bytes[0:4].decode('ascii', errors='replace')
        self.logger.warning("Got command %s", command)
        try:
            if command == "EXIT":
                return True, None

            elif command == "STHU":
                # Accepted for wire compatibility; no threshold consumer exists
                # in the current chain (there is no matching rtl_daq command).
                threshold = unpack('f',msg_bytes[4:8])[0]
                self.logger.warning("STHU threshold {:f} accepted but not implemented".format(threshold))
                return False, self._build_reply(None)

            elif command == "FREQ":
                frequency = unpack('Q',msg_bytes[4:12])[0]
                self.logger.info("Received frequency value: {:f} Hz".format(frequency))
                return False, self._submit_request(command, [frequency])

            elif command == "GAIN":
                gains = unpack('I'*self.M, msg_bytes[4:4+4*self.M])
                for m in range(self.M):
                    self.logger.info("Received gain values - CH{:d}: {:d} dB x 10".format(m, gains[m]))
                return False, self._submit_request(command, list(gains))

            elif command == "AGC ":
                self.logger.info("Received AGC request")
                return False, self._submit_request(command, [])

            elif command == "INIT":
                self.logger.info("Inititalization command received")
                return False, self._submit_request(command, [])

            elif command == "SCHD":
                # Schedule load: payload is file path or compact JSON
                payload = msg_bytes[4:128].decode('utf-8', errors='ignore').rstrip('\x00').strip()
                self.logger.info("Received schedule load command")
                return False, self._submit_request(command, [payload])

            elif command == "SCHS":
                self.logger.info("Received schedule stop command")
                return False, self._submit_request(command, [])

            elif command == "SCHQ":
                self.logger.info("Received schedule query command")
                return False, self._submit_request(command, [])

            elif command == "SCHN":
                self.logger.info("Received schedule next command")
                return False, self._submit_request(command, [])

            elif command == "EGAN":
                gains = unpack('I'*self.M, msg_bytes[4:4+4*self.M])
                for m in range(self.M):
                    self.logger.info("Received external LNA gain - CH{:d}: {:d} dB x 10".format(m, gains[m]))
                return False, self._submit_request(command, list(gains))

            elif command == "RFQ ":
                self.logger.info("Received link-budget query command")
                return False, self._submit_request(command, [])

            elif command == "BIAS":
                states = unpack('I'*self.M, msg_bytes[4:4+4*self.M])
                for m in range(self.M):
                    self.logger.info("Received bias-tee state - CH{:d}: {:d}".format(m, states[m]))
                return False, self._submit_request(command, list(states))

            elif command == "ORNT":
                az, el = unpack('ff', msg_bytes[4:12])
                self.logger.info("Received orientation bearing: az={:.2f} el={:.2f}".format(az, el))
                return False, self._submit_request(command, [az, el])

            elif command in ("PARK", "SCAN", "OSTP", "OQRY"):
                self.logger.info("Received orientation command: {:s}".format(command))
                return False, self._submit_request(command, [])

            else:
                self.logger.error("Unidentified control command: {:s}".format(command))
                return False, self._build_reply(None)
        except Exception as e:
            # Malformed payloads must not kill the server thread
            self.logger.error("Failed to process control frame {:s}: {:s}".format(command, str(e)))
            return False, self._build_reply(None)

if __name__ == "__main__":
    import signal

    def _sig_handler(signum, frame):
        # Raise SystemExit so the finally block below runs the normal
        # clean-shutdown path (daq_stop.sh sends SIGTERM by default).
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sig_handler)

    HWC_inst0 = HWC()
    try:
        if HWC_inst0.init() == 0:
            if HWC_inst0.event_bus is not None:
                from daq_events import DAQEvent, EVT_PROCESS_START
                HWC_inst0.event_bus.emit(DAQEvent(severity="info",
                    module="hw_controller", event_type=EVT_PROCESS_START, payload={}))
            HWC_inst0.start()
    finally:
        HWC_inst0.ctr_iface_server.shutdown()
        if HWC_inst0.event_bus is not None:
            from daq_events import DAQEvent, EVT_PROCESS_STOP
            HWC_inst0.event_bus.emit(DAQEvent(severity="info",
                module="hw_controller", event_type=EVT_PROCESS_STOP, payload={}))
            HWC_inst0.event_bus.close()
        HWC_inst0.close()
