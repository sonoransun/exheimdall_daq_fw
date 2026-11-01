"""
   Delay Synchronizer Module for multichannel coherent receivers.
   
   Project: HeIMDALL DAQ Firmware
   Author: Tamas Peto
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

   WARNING: Check the native size of the IQ header on the target device
   
   Fractional sample delay compensation by sampling frequency tuning:
        Mikko Laakso, "Multichannel coherent receiver on the RTL-SDR" 2019
"""
# Import built-in modules
import hashlib
import logging
import os
import socket
import sys
import time
import zlib
from struct import pack
from time import sleep
from os.path import join

# Import third-party modules
import numpy as np
import numpy.linalg as lin
from scipy import fft
from configparser import ConfigParser
import zmq
# NOTE: skrf (scikit-rf) is imported lazily inside the touchstone branch of
# _read_config_file - it is an optional dependency and slow to import on a Pi.

try:
    from numba import njit
except ImportError:
    # numba is an optional accelerator: without it the @njit kernels below run
    # as plain numpy (correct, slower). Keeps minimal/CI environments working.
    def njit(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def wrap(func):
            return func
        return wrap

# Import HeIMDALL modules
from iq_header import IQHeader
from shmemIface import outShmemIface, inShmemIface, TERMINATE
from transportIface import TransportProducer, TransportConsumer
from offload_engines import FFTEngine, CorrelationEngine, resolve_workers, DEFAULT_WORKERS
import inter_module_messages
from daq_db_records import (CAL_EVENT_CAL_START, CAL_EVENT_SAMPLE_CAL_DONE,
                            CAL_EVENT_IQ_CAL_DONE, CAL_EVENT_TRACK_LOCK,
                            CAL_EVENT_TRACK_LOST, CAL_EVENT_FREQ_CHANGE)
try:
    from version import FW_VERSION
except ImportError:
    FW_VERSION = None

#: Cal-history event code of a calibration timeout ([calibration]
#: cal_timeout_frames). daq_db_records defines codes 0-8; the record field is
#: an unsigned int, so a new code needs no schema change.
CAL_EVENT_CAL_TIMEOUT = 9

#: FSM states in which a calibration is in progress (sync_state 2-4).
CALIBRATION_STATES = ("STATE_SAMPLE_CAL", "STATE_SYNC_WAIT", "STATE_FRAC_SAMPLE_CAL",
                      "STATE_FRAC_SYNC_WAIT", "STATE_IQ_CAL")

#: The hw_controller relay files are polled every this many frames.
RELAY_POLL_FRAMES = 5

#: File descriptor on which the start scripts hand the instance lock down
#: (spec B.3.3, [host] instance_registry = 1).
INSTANCE_LOCK_FD = 9
INSTANCE_LOCK_REFUSED_EXIT = 3

AMPLITUDE_TOLERANCE_MODES = ("legacy", "symmetric")
HEALTH_MODES = ("mesh", "hub", "off")


# ---------------------------------------------------------------------------
# Pure helpers (module level so tests and the closed-loop model import them)
# ---------------------------------------------------------------------------

def int_fs_tune_gain(delay, gain_table):
    """Tune gain of an integer sample delay from the [limits] [gains] table."""
    return (gain_table[1, (gain_table[0, :] <= abs(delay))])[0]


def int_fs_ppm_offsets(delays, gain_table, min_ppm, max_ppm):
    """Sampling-rate offsets for measured integer sample delays (STATE_SAMPLE_CAL).

    ``fs_ppm[m] = -delays[m] * gain(|delays[m]|) * min_ppm``, clipped to
    ``+-max_ppm``; computed for every entry of *delays* (a zero delay gives 0).
    """
    fs_ppm_offsets = [0] * len(delays)
    for m in range(len(delays)):
        fs_tune_gain_m = int_fs_tune_gain(delays[m], gain_table)
        fs_ppm_offsets[m] = -1 * delays[m] * fs_tune_gain_m * min_ppm
        if abs(fs_ppm_offsets[m]) > max_ppm:
            fs_ppm_offsets[m] = np.sign(fs_ppm_offsets[m]) * max_ppm
    return fs_ppm_offsets


def frac_fs_ppm_offsets(taus, channel_list, M, tol, gain, min_ppm):
    """Sampling-rate offsets for fractional delays (STATE_FRAC_SAMPLE_CAL).

    *taus* are ordered as *channel_list*; channels whose ``|tau| <= tol``
    (and the reference channel) get 0.
    """
    fs_ppm_offsets = [0] * M
    for i, m in enumerate(channel_list):
        if abs(taus[i]) > tol:
            fs_ppm_offsets[m] = np.sign(taus[i]) * np.abs(taus[i] * gain) * min_ppm
    return fs_ppm_offsets


def amplitude_out_of_tolerance(ratios, tol_ratio, tol_db, mode="legacy"):
    """Amplitude tolerance test of [calibration] amplitude_tolerance_mode.

    legacy    : ``(abs(r) > tol_ratio).any()`` (one-sided, today's test)
    symmetric : ``(abs(20*log10(abs(r))) > tol_db).any()``
    """
    if mode == "symmetric":
        with np.errstate(divide="ignore"):
            return bool((np.abs(20 * np.log10(np.abs(ratios))) > tol_db).any())
    return (abs(ratios) > tol_ratio).any()


def _check_inherited_lock(path, fd=INSTANCE_LOCK_FD):
    """Did this process inherit the instance lock on *fd*? (spec B.3.3)

    ``"ok"`` when *fd* is open on the same file as *path*, ``"missing"`` when
    *fd* is not open, ``"mismatch"`` when it is open on another file or
    *path* cannot be stat'ed.
    """
    try:
        fd_stat = os.fstat(fd)
    except OSError:
        return "missing"
    try:
        path_stat = os.stat(path)
    except (OSError, TypeError, ValueError):
        return "mismatch"
    if (fd_stat.st_ino, fd_stat.st_dev) == (path_stat.st_ino, path_stat.st_dev):
        return "ok"
    return "mismatch"


def enforce_instance_lock(config_filename="daq_chain_config.ini", environ=None,
                          logger=None, fd=INSTANCE_LOCK_FD):
    """Defence-in-depth lock check, active only with HEIMDALL_INSTANCE_GUARD=refuse.

    Returns 0 (proceed) or INSTANCE_LOCK_REFUSED_EXIT after logging
    ``instance <N> started without its instance lock; refusing``. Must run
    before any shared-memory interface is opened.
    """
    if environ is None:
        environ = os.environ
    if environ.get("HEIMDALL_INSTANCE_GUARD") != "refuse":
        return 0
    if logger is None:
        logger = logging.getLogger(__name__)
    lock_path = environ.get("HEIMDALL_INSTANCE_LOCK", "")
    result = _check_inherited_lock(lock_path, fd) if lock_path else "missing"
    if result == "ok":
        return 0
    parser = ConfigParser()
    instance_id = 0
    try:
        if parser.read([config_filename]):
            instance_id = parser.getint('federation', 'instance_id', fallback=0)
    except Exception:
        pass
    logger.critical("instance %d started without its instance lock; refusing", instance_id)
    logger.critical("lock check result: %s (fd %d, HEIMDALL_INSTANCE_LOCK=%r)",
                    result, fd, lock_path)
    return INSTANCE_LOCK_REFUSED_EXIT


def _parse_core_list(text):
    """``[host] delay_sync_cores`` ("2,3") -> sorted list of distinct ints."""
    cores = set()
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        value = int(item)
        if value < 0:
            raise ValueError(item)
        cores.add(value)
    return sorted(cores)


def _local_default_node_id(instance_id):
    """Fallback for federation_common.default_node_id (same rule)."""
    try:
        from federation_common import default_node_id
        return default_node_id(instance_id)
    except Exception:
        host = socket.gethostname().split(".", 1)[0].lower() or "host"
        return "{}:{}".format(host, int(instance_id))


def _strip_state(state):
    """FSM name on the wire: the state without its ``STATE_`` prefix."""
    return state[6:] if state.startswith("STATE_") else state


class _OptionReader(object):
    """Defensive reader for the opt-in keys: a missing key gives its default
    (today's behaviour); a malformed one gives the default and one WARNING."""

    def __init__(self, parser, logger):
        self.parser = parser
        self.logger = logger

    def has(self, section, key):
        return self.parser.has_option(section, key)

    def raw(self, section, key, default=""):
        if not self.parser.has_option(section, key):
            return default
        return self.parser.get(section, key).strip()

    def _bad(self, section, key, value, default, expected):
        self.logger.warning("[%s] %s = %r is invalid (expected %s); using %r",
                            section, key, value, expected, default)
        return default

    def int(self, section, key, default, minimum=None, maximum=None):
        if not self.parser.has_option(section, key):
            return default
        value = self.parser.get(section, key).strip()
        try:
            n = int(value)
        except ValueError:
            return self._bad(section, key, value, default, "an integer")
        if (minimum is not None and n < minimum) or (maximum is not None and n > maximum):
            return self._bad(section, key, value, default,
                             "an integer in [{}, {}]".format(minimum, maximum))
        return n

    def float(self, section, key, default, minimum=None, maximum=None):
        if not self.parser.has_option(section, key):
            return default
        value = self.parser.get(section, key).strip()
        try:
            x = float(value)
        except ValueError:
            return self._bad(section, key, value, default, "a number")
        if x != x or (minimum is not None and x < minimum) or (maximum is not None and x > maximum):
            return self._bad(section, key, value, default,
                             "a number in [{}, {}]".format(minimum, maximum))
        return x

    def flag(self, section, key, default=False):
        if not self.parser.has_option(section, key):
            return default
        value = self.parser.get(section, key).strip()
        if value in ("0", "1"):
            return value == "1"
        return self._bad(section, key, value, default, "0 or 1")

    def choice(self, section, key, default, choices):
        if not self.parser.has_option(section, key):
            return default
        value = self.parser.get(section, key).strip().lower()
        if value in choices:
            return value
        return self._bad(section, key, value, default, " | ".join(choices))


class delaySynchronizer():
    
    def __init__(self):
        
        logging.basicConfig(level=10)
        self.logger = logging.getLogger(__name__)

        self.module_identifier = 5 # Inter-module message module identifier        
        self.in_shmem_iface = None
        self.in_shmem_iface_name = ""
        self.out_shmem_iface_iq = None
        self.out_shmem_iface_hwc = None
        
        self.log_level = 0
        self.ignore_frame_drop_warning = True
        
        self.sync_delay_byte = 'd'.encode('ascii')
        self.sync_reset_byte = 'r'.encode('ascii')
        
        self.M = 8 # Number of receiver channels 
        self.N = 2**18 # Number of samples per channel
        self.R = 12 # Decimation ratio
        
        # Calibration control parameters
        self.N_proc = 2**18        
        self.std_ch_ind = 0 # Index of standard channel. All channels are matched in delay to this one        
        self.en_iq_cal = False # Enables amlitude and phase calibration        
        # IQ calibration adjustment
        self.iq_adjust = np.zeros(self.M, dtype=np.complex64)
        self.iq_adjust_source = "explicit-time-delay" # "explicit-time-delay" / "touchstone"
        self.iq_adjust_amplitude = None
        self.iq_adjust_time = None
        self.iq_adjust_table  = None # Frequency - Phase table for all channels 

        self.min_corr_peak_dyn_range = 20 # [dB]
        self.corr_peak_offset = 100 # [sample]
        self.cal_track_mode = 0        
        self.amplitude_cal_mode = "channel_power" # "default" / "disabled" / "channel_power"  -> Updated from .ini

        self.phase_diff_tolerance = 3 # deg, maximum allowable phase difference
        self.amp_diff_tolerance = 0.5 # power ratio  maximum allowable amplitude difference, not dB!
        self.frac_delay_tolerance = 0.03
        self.sync_failed_cntr = 0 # Counts the number of iq or sample sync fails in track mode
        self.max_sync_fails = 3 # Maximum number of synchronization fails before the sync track is lost
        self.sync_failed_cntr_total = 0

        self.MIN_FS_PPM_OFFSET = 0.0000001
        self.MAX_FS_PPM_OFFSET = 0.01
        self.INT_FS_TUNE_GAIN  = np.array([[100, 2, 0],[50, 25, 15]]) #Reference table for tuning - [Delay limits] [Tune gains]
        self.FRAC_FS_TUNE_GAIN = 20
        
        # Auxiliary state variables
        self.sample_compensation_cntr = 0 # Count the number of issued delay compensations
        self.iq_compensation_cntr = 0 # Count the number of issued iq compensations 
        self.last_update_ind=-3 # Hold the last index when the compensation has sent
        self.last_rf = 0 # Tracks the RF center frequency, recalibration is initiated when changed 
                
        # Database
        self.db = None
        self._en_db = False
        # Latest calibration-quality estimate: the minimum correlation-peak
        # dynamic range over the non-reference channels [dB] (0.0 = unknown)
        self._cal_quality = 0.0

        # Monitoring
        self.metrics = None
        self.event_bus = None
        self.status_server = None
        self._cal_start_frame = 0
        self._last_frame_time = 0.0
        self._heartbeat_interval = 100
        self._heartbeat_counter = 0
        # Optional [daq] listen_address for the TCP/ZMQ listeners
        self.listen_address = "0.0.0.0"
        # Previous cumulative drop counters, so metrics record per-frame DELTAS
        # (statistics of a monotonic counter are meaningless)
        self._prev_dropped_iq = 0
        self._prev_dropped_hwc = 0

        # Federation
        self.instance_id = 0
        self.port_stride = 100
        self.federation_health = None

        # RF front-end / orientation telemetry (v8 header reserved-region stamping)
        self.rf_frontend = None
        self.antenna_profile = None
        self._en_frontend_telemetry = False
        self._en_orientation = False
        self._agg_power_mdb = 0
        self._orientation_state_fname = "_data_control/orientation_state"
        self._orientation_read_counter = 0
        self._orientation_cache = (0.0, 0.0, 0)  # (az_deg, el_deg, rotator_state)
        self._bias_tee_state_fname = "_data_control/bias_tee_state"
        self._bias_tee_mask = 0
        # Gain-budget stamping cache: the budget only changes when the tuner
        # gains or the RF center frequency change (keyed on both)
        self._budget_cache_key = None
        self._budget_cache = None

        # Scratch frame reused on the dropped-IQ-buffer path (avoids a large
        # per-frame allocation when the consumer is slow)
        self._drop_scratch = None

        # ZMQ control-link robustness (rtl_daq REQ/REP)
        self.ZMQ_TIMEOUT_MS = 5000
        self.zmq_context = None
        self.zmq_port = 1130

        # Offload / transport defaults
        self.transport_type = 'shm'
        self.fft_engine_type = 'cpu_scipy'

        # Opt-in extensions (status, relays, federation, stamping, levers);
        # every default reproduces the behaviour without the new keys
        self._init_extension_state()

        # Overwrite default configuration
        self._read_config_file("daq_chain_config.ini")

        # Configure logger
        self.logger.setLevel(self.log_level)
        float_formatter = "{:.2f}".format
        np.set_printoptions(formatter={'float_kind':float_formatter})

        # Initialize compute engines ([offload] fft_workers / xcorr_workers;
        # both default to the engines' own default of 4 workers)
        self.fft_engine = FFTEngine(engine_type=self.fft_engine_type, workers=self.fft_workers)
        if self.xcorr_workers == self.fft_workers:
            self.xcorr_engine = self.fft_engine
        else:
            self.xcorr_engine = FFTEngine(engine_type=self.fft_engine_type,
                                          workers=self.xcorr_workers)
        self.logger.info("FFT engine: {:s}".format(self.fft_engine_type))

        self.iq_header = IQHeader()
        """
            Block sizes measured in bytes        
            1 IQ sample consist of 2 32bit float number
        """        
        self.in_block_size = self.N *self.M * 2 * 4
        
        self.logger.info("Antenna channles {:d}".format(self.M))
        self.logger.info("IQ samples per channel {:d}".format(self.N))  
        self.current_state = "STATE_INIT" 
        
        # List of the channels to be mathced 
        self.channel_list=(np.arange(self.M).tolist())
        self.channel_list.remove(self.std_ch_ind)        
        
        # Allocations
        self.corr_functions = np.zeros((self.M, self.N_proc*2))
        self.delays = np.zeros(self.M, dtype=int) # Holds the calculated samples delay
        self.iq_corrections = np.ones(self.M, dtype=np.complex64) # This vector holds the IQ compensation values
        self.iq_diff_ref = np.ones(self.M, dtype=np.complex64) # Reference IQ difference vector used in the tracking mode
        
        self.logger.info("Delay synchronizer initialized")

    def _init_extension_state(self):
        """Defaults of every opt-in extension (design spec B.3.3, C.3, E.4,
        G.1-G.7, I.4, I.8). Each default is the behaviour without the key."""
        self.config_sha256 = None
        self.ini_version = None
        self.sample_rate = 0
        self.amp_diff_tolerance_db = 20 * np.log10(self.amp_diff_tolerance)

        # [federation] identity
        self.node_id = None
        self.cluster_id = "default"
        self.advertise_host = ""
        self._node_id_crc32 = 0
        self._stamp_node_id = False

        # [monitoring] extensions
        self._status_extended = False
        self._status_update_interval = 0
        self._status_counter = 0
        self._last_status_key = None
        self._stale_after_sec = 0.0
        self._en_hwc_state_relay = False
        self._event_tag_identity = False
        self._event_topic_prefix = False
        self._stamp_cal_telemetry = False
        self._stamp_decimator_drops = False

        # [calibration] extensions
        self._en_wire_recal = False
        self._track_decimation = 1
        self._track_check_counter = 0
        self._track_last_flags = (True, True)
        self.amplitude_tolerance_mode = "legacy"
        self._cal_timeout_frames = 0
        self._cal_timeout_count = 0
        self._invalidate_on_overrun = False
        self._frames_after_overrun = 1
        self._overrun_invalidate_left = 0
        self._overrun_invalidated_frames = 0

        # [offload] throughput levers
        self.fft_workers = DEFAULT_WORKERS
        self.xcorr_workers = DEFAULT_WORKERS
        self._skip_copy_on_drop = False
        self.xcorr_engine = None

        # Input-header continuity (extended STATUS, overrun invalidation)
        self._track_continuity = False
        self._prev_cpi_index = None
        self._prev_daq_block_index = None
        self._prev_overrun_cnt = None
        self._overrun_increased = False
        self._cpi_gaps = 0
        self._daq_block_gaps = 0
        self._last_sync_state = 0
        self._iq_samples = None

        # hw_controller relays (G.3, G.5)
        self._hwc_watcher = None
        self._hwc_doc = None
        self._recal_watcher = None
        self._recal_generation = 0
        self._relay_counter = 0

        # Federation wiring (C.3)
        self._fed_info = None
        self._fed_members = []
        self._register_meta = None
        self._process_start_ms = int(time.time() * 1000)

    def _read_extension_config(self, reader, config_filename):
        """Parse the opt-in keys. Missing keys keep the defaults set by
        _init_extension_state(); malformed values fall back with a WARNING."""
        try:
            with open(config_filename, "rb") as f:
                self.config_sha256 = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            self.config_sha256 = None
        version = reader.raw('meta', 'ini_version', None)
        if version is not None:
            self.ini_version = int(version) if version.isdigit() else version
        self.sample_rate = reader.int('daq', 'sample_rate', 0, minimum=0)

        # Identity ([federation]): node_id defaults to <short host>:<N>
        node_id = reader.raw('federation', 'node_id', "")
        self.node_id = node_id if node_id else _local_default_node_id(self.instance_id)
        self.cluster_id = reader.raw('federation', 'cluster_id', "") or "default"
        self.advertise_host = reader.raw('federation', 'advertise_host', "")
        self._stamp_node_id = reader.flag('federation', 'stamp_node_id')
        self._node_id_crc32 = zlib.crc32(self.node_id.encode()) & 0xFFFFFFFF

        # [monitoring]
        self._status_extended = reader.flag('monitoring', 'status_extended')
        self._status_update_interval = reader.int('monitoring', 'status_update_interval', 0,
                                                  minimum=0)
        self._stale_after_sec = reader.float('monitoring', 'stale_after_sec', 0.0, minimum=0.0)
        self._en_hwc_state_relay = reader.flag('monitoring', 'en_hwc_state_relay')
        self._event_tag_identity = reader.flag('monitoring', 'event_tag_identity')
        self._event_topic_prefix = reader.flag('monitoring', 'event_topic_prefix')
        self._stamp_cal_telemetry = reader.flag('monitoring', 'stamp_cal_telemetry')
        self._stamp_decimator_drops = reader.flag('monitoring', 'stamp_decimator_drops')

        # [calibration]
        self._en_wire_recal = reader.flag('calibration', 'en_wire_recal')
        self._track_decimation = reader.int('calibration', 'track_decimation', 1, minimum=1)
        self.amplitude_tolerance_mode = reader.choice('calibration', 'amplitude_tolerance_mode',
                                                      "legacy", AMPLITUDE_TOLERANCE_MODES)
        self._cal_timeout_frames = reader.int('calibration', 'cal_timeout_frames', 0, minimum=0)
        self._invalidate_on_overrun = reader.flag('calibration', 'invalidate_on_overrun')
        self._frames_after_overrun = reader.int('calibration', 'frames_after_overrun', 1,
                                                minimum=0)

        # [offload] throughput levers; [host] delay_sync_cores feeds "auto"
        cores = None
        cores_text = reader.raw('host', 'delay_sync_cores', "")
        if cores_text:
            try:
                cores = _parse_core_list(cores_text)
            except ValueError:
                reader._bad('host', 'delay_sync_cores', cores_text, "", "comma-separated core ids")
        for key in ('fft_workers', 'xcorr_workers'):
            value = reader.raw('offload', key, None)
            try:
                workers = resolve_workers(value, cores)
            except ValueError:
                workers = reader._bad('offload', key, value, DEFAULT_WORKERS,
                                      "an integer >= 1 or auto")
            setattr(self, key, workers)
        self._skip_copy_on_drop = reader.flag('offload', 'skip_copy_on_drop')

        self._track_continuity = self._status_extended or self._invalidate_on_overrun
        if self._status_extended:
            self.logger.info("Extended STATUS enabled (status_protocol 2)")

        # hw_controller relays: baseline at start, never replay a leftover file
        prefix = "" if self.instance_id == 0 else "inst{:d}_".format(self.instance_id)
        if self._en_hwc_state_relay or self._en_wire_recal:
            try:
                import hwc_relay
            except ImportError as e:
                self.logger.error("hw_controller relays unavailable (hwc_relay: %s)", e)
            else:
                if self._en_hwc_state_relay:
                    self._hwc_watcher = hwc_relay.HwcStateWatcher(hwc_relay.hwc_state_path(prefix))
                    self._hwc_doc = hwc_relay.read_hwc_state(self._hwc_watcher.path)
                if self._en_wire_recal:
                    self._recal_watcher = hwc_relay.RecalWatcher(
                        hwc_relay.recal_request_path(prefix))

    @property
    def frame_period_sec(self):
        """``cpi_size * decimation_ratio / sample_rate`` [s], None when unknown."""
        if self.sample_rate <= 0:
            return None
        return self.N * self.R / float(self.sample_rate)

    def _event_source(self):
        """EventBus identity tag ([monitoring] event_tag_identity = 1)."""
        try:
            from federation_common import self_host
            host = self_host(self.advertise_host or None)
        except Exception:
            host = socket.gethostname().split(".", 1)[0].lower()
        return {"instance_id": self.instance_id, "node_id": self.node_id,
                "host": host, "cluster_id": self.cluster_id}

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
        self.R = parser.getint('pre_processing', 'decimation_ratio')
        self.N_proc = parser.getint('calibration', 'corr_size')
        self.std_ch_ind = parser.getint('calibration','std_ch_ind')
        self.amp_diff_tolerance = parser.getint('calibration', 'amplitude_tolerance')
        self.phase_diff_tolerance = parser.getint('calibration', 'phase_tolerance')
        self.cal_track_mode = parser.getint('calibration','cal_track_mode')
        self.max_sync_fails = parser.getint('calibration','maximum_sync_fails')
        self.amplitude_cal_mode = parser.get('calibration','amplitude_cal_mode')
        
        if parser.getint('calibration', 'en_iq_cal'):
            self.en_iq_cal = True
        else:
            self.en_iq_cal = False
        
        self.log_level=(parser.getint('daq', 'log_level')*10)
        # Optional bind address for the TCP/ZMQ listeners this module owns
        # (status server, ZMQ event PUB). Default: all interfaces.
        self.listen_address = parser.get('daq', 'listen_address', fallback='0.0.0.0').strip() or '0.0.0.0'

        # Convert to voltage ratio (the dB value is kept for the symmetric
        # amplitude tolerance test, [calibration] amplitude_tolerance_mode)
        self.amp_diff_tolerance_db = self.amp_diff_tolerance
        self.amp_diff_tolerance = 10**(self.amp_diff_tolerance/20)
        
        # External IQ calibration adjustment
        self.iq_adjust_source = parser.get('calibration','iq_adjust_source')

        daq_rf  = parser.getint('daq', 'center_freq') # Read RF center frequency for phase offset calculation

        if self.iq_adjust_source == "explicit-time-delay":
            iq_adjust_amplitude_str = parser.get('calibration','iq_adjust_amplitude')
            iq_adjust_amplitude_str = iq_adjust_amplitude_str.split(',')[0:self.M-1]
            iq_adjust_amplitude     = list(map(float, iq_adjust_amplitude_str))
            self.iq_adjust_amplitude     = 10**(np.array(iq_adjust_amplitude)/20) # Convert to voltage relations
            
            iq_adjust_time_str  = parser.get('calibration','iq_adjust_time_delay_ns')
            iq_adjust_time_str  = iq_adjust_time_str.split(',')[0:self.M-1]
            self.iq_adjust_time = np.array(list(map(float, iq_adjust_time_str)))*10**-9

            iq_adjust_phase     = self.iq_adjust_time*daq_rf*2*np.pi  # Convert time delay to phase         

            self.iq_adjust = self.iq_adjust_amplitude * np.exp(1j*iq_adjust_phase) # Assemble IQ adjustment vector
            self.iq_adjust = np.insert(self.iq_adjust, self.std_ch_ind, 1+0j)
        elif self.iq_adjust_source == "touchstone":
            import skrf as rf  # Optional heavy dependency, only needed here
            for m in range(self.M):
                fname = join("_calibration", f"cable_ch{m}.s1p")
                self.logger.info(f"Loading: {fname}")
                net = rf.Network(fname)
                if self.iq_adjust_table is None:
                    self.iq_adjust_table = np.zeros((len(net.f),self.M+1), dtype=complex)
                    self.iq_adjust_table[:,0] = net.f[:]
                self.iq_adjust_table[:,m+1] = net.s[:,0,0]
            self.logger.info(f"{self.iq_adjust_table.shape}")
            self.iq_adjust = self.iq_adjust_table[np.argmin(abs(self.iq_adjust_table[:,0]-daq_rf)), 1::]
        
        self.iq_adjust /= self.iq_adjust[self.std_ch_ind]
        self.logger.info(f"IQ adjustment vector: abs:{abs(self.iq_adjust)}")
        self.logger.info(f"IQ adjustment vector: phase:{np.rad2deg(np.angle(self.iq_adjust))}")

        # Database configuration
        if parser.has_section('database'):
            self._en_db = parser.getint('database', 'en_db', fallback=0) == 1
            if self._en_db:
                try:
                    from daq_db import DAQDatabase
                    db_kwargs = dict(
                        db_dir=parser.get('database', 'db_dir', fallback='_db'),
                        max_db_size_mb=parser.getint('database', 'max_db_size_mb', fallback=500),
                        rotation_max_age_hours=parser.getint('database', 'rotation_max_age_hours', fallback=168),
                        write_batch_size=parser.getint('database', 'write_batch_size', fallback=50),
                        write_flush_interval_sec=parser.getfloat('database', 'write_flush_interval_sec', fallback=1.0),
                        num_channels=self.M
                    )
                    # Instance awareness (spec B.2); the defaults keep db_dir
                    # verbatim. An older DAQDatabase without these keywords
                    # is constructed exactly as before.
                    db_reader = _OptionReader(parser, self.logger)
                    instance_kwargs = dict(
                        instance_id=parser.getint('federation', 'instance_id', fallback=0),
                        instance_subdir=db_reader.flag('database', 'instance_subdir'),
                        write_meta=db_reader.flag('database', 'write_instance_meta'))
                    node_id_cfg = parser.get('federation', 'node_id', fallback='').strip()
                    if node_id_cfg:
                        instance_kwargs["node_id"] = node_id_cfg
                    try:
                        self.db = DAQDatabase(**db_kwargs, **instance_kwargs)
                    except TypeError:
                        self.db = DAQDatabase(**db_kwargs)
                    self.logger.info("DAQ database enabled")
                except Exception as e:
                    self.logger.error("Failed to initialize DAQ database: {:s}".format(str(e)))
                    self.db = None

        # Federation configuration (read early so instance_id is available for port offsets).
        # The FederationHealth monitor is constructed AFTER the [monitoring]
        # section below (_build_federation), so it receives the event bus and
        # the configured status base port.
        if parser.has_section('federation'):
            self.instance_id = parser.getint('federation', 'instance_id', fallback=0)
            self.port_stride = parser.getint('federation', 'port_stride', fallback=100)
            if self.instance_id != 0:
                self.logger.info("Federation instance_id={:d}, port_stride={:d}".format(
                    self.instance_id, self.port_stride))

        # Opt-in extension keys (identity, extended status, relays, stamping,
        # calibration robustness, throughput levers)
        self._read_extension_config(_OptionReader(parser, self.logger), config_filename)

        # Monitoring configuration
        if parser.has_section('monitoring'):
            en_monitoring = parser.getint('monitoring', 'en_monitoring', fallback=0) == 1
            if en_monitoring:
                try:
                    from daq_events import (EventBus, LoggingHandler, SysLogEventHandler, ZMQPubHandler)
                    ring_size = parser.getint('monitoring', 'event_ring_size', fallback=500)
                    if self._event_tag_identity:
                        self.event_bus = EventBus(enabled=True, ring_size=ring_size,
                                                  source=self._event_source())
                    else:
                        self.event_bus = EventBus(enabled=True, ring_size=ring_size)
                    self.event_bus.register_handler(LoggingHandler())

                    if parser.getint('monitoring', 'en_syslog', fallback=0) == 1:
                        syslog_handler = SysLogEventHandler(
                            address=parser.get('monitoring', 'syslog_address', fallback='/dev/log'),
                            facility=parser.get('monitoring', 'syslog_facility', fallback='daemon'),
                            min_severity=parser.get('monitoring', 'syslog_min_severity', fallback='warning')
                        )
                        self.event_bus.register_handler(syslog_handler)

                    if parser.getint('monitoring', 'en_zmq_pub', fallback=0) == 1:
                        zmq_port = parser.getint('monitoring', 'zmq_pub_port', fallback=5003)
                        zmq_port += self.instance_id * self.port_stride
                        if self._event_topic_prefix:
                            self.event_bus.register_handler(
                                ZMQPubHandler(port=zmq_port, listen_address=self.listen_address,
                                              topic_prefix="inst{:d}.".format(self.instance_id)))
                        else:
                            self.event_bus.register_handler(
                                ZMQPubHandler(port=zmq_port, listen_address=self.listen_address))

                    self.logger.info("Event bus enabled")
                except Exception as e:
                    self.logger.error("Failed to initialize event bus: {:s}".format(str(e)))
                    self.event_bus = None

            en_metrics = parser.getint('monitoring', 'en_metrics', fallback=0) == 1
            if en_metrics:
                try:
                    from daq_metrics import MetricsCollector
                    window_size = parser.getint('monitoring', 'metrics_window_size', fallback=1000)
                    self.metrics = MetricsCollector(window_size=window_size)
                    self.logger.info("Performance metrics enabled")
                except Exception as e:
                    self.logger.error("Failed to initialize metrics: {:s}".format(str(e)))
                    self.metrics = None

            self._heartbeat_interval = parser.getint('monitoring', 'heartbeat_interval', fallback=100)

            en_status_server = parser.getint('monitoring', 'en_status_server', fallback=0) == 1
            if en_status_server:
                try:
                    from daq_status_server import StatusServer
                    port = parser.getint('monitoring', 'status_server_port', fallback=5002)
                    port += self.instance_id * self.port_stride
                    status_kwargs = dict(
                        port=port, metrics=self.metrics,
                        event_bus=self.event_bus,
                        db=self.db,
                        instance_id=self.instance_id,
                        listen_address=self.listen_address,
                        drop_window_sec=parser.getfloat('monitoring', 'drop_window_sec',
                                                        fallback=60.0))
                    if self._status_extended:
                        # Extended grammar and freshness (spec G.1, G.2, G.7)
                        status_kwargs.update(
                            extended=True,
                            stale_after_sec=self._stale_after_sec,
                            frame_period_sec=self.frame_period_sec,
                            update_interval_frames=self._effective_status_interval(),
                            max_clients=_OptionReader(parser, self.logger).int(
                                'monitoring', 'status_max_clients', StatusServer.MAX_CLIENTS,
                                minimum=1, maximum=1024),
                            hwc_provider=self._hwc_status if self._en_hwc_state_relay else None,
                            federation_provider=self._federation_status)
                    self.status_server = StatusServer(**status_kwargs)
                    self.logger.info("Status server configured on port {:d}".format(port))
                except Exception as e:
                    self.logger.error("Failed to initialize status server: {:s}".format(str(e)))
                    self.status_server = None

        # Wire the event bus into the database (queue-full diagnostics; the
        # DAQDatabase docstring expects this to be set externally)
        if self.db is not None:
            self.db.event_bus = self.event_bus

        # Deferred federation health monitor construction (see the [federation]
        # block above): by now the event bus and the monitoring config exist.
        self.federation_health = self._build_federation(parser, self.event_bus)

        # Offload / transport configuration
        if parser.has_section('offload'):
            self.transport_type = parser.get('offload', 'delay_sync_transport', fallback='shm')
            fft_engine_cfg = parser.get('offload', 'fft_engine', fallback='auto')
            if fft_engine_cfg == 'auto':
                self.fft_engine_type = 'cpu_scipy'
            else:
                self.fft_engine_type = fft_engine_cfg
            self.logger.info("Offload config: transport={:s}, fft_engine={:s}".format(
                self.transport_type, self.fft_engine_type))

        # RF front-end / orientation telemetry configuration. delay_sync is the
        # producer of the outbound IQ header, so it stamps per-frame gain-budget +
        # aggregate power + antenna bearing into the v8 reserved region.
        try:
            from gain_budget import RfFrontendParser
            from antenna_profile import AntennaProfileParser
            self.antenna_profile = AntennaProfileParser.from_ini_section(parser, 'antenna')
            self.rf_frontend = RfFrontendParser.from_ini_section(parser, self.M, 'amplification')
        except Exception as e:
            self.logger.error("Failed to load RF front-end telemetry config: {:s}".format(str(e)))
            self.rf_frontend = None
            self.antenna_profile = None
        if parser.has_section('orientation'):
            self._en_orientation = parser.getint('orientation', 'en_orientation', fallback=0) == 1
        if self.instance_id != 0:
            self._orientation_state_fname = "_data_control/inst{:d}_orientation_state".format(self.instance_id)
            self._bias_tee_state_fname = "_data_control/inst{:d}_bias_tee_state".format(self.instance_id)
        self._en_frontend_telemetry = (self.rf_frontend is not None
                                       or self.antenna_profile is not None
                                       or self._en_orientation)
        if self._en_frontend_telemetry:
            self.logger.info("RF front-end / orientation telemetry stamping enabled (v8 header)")

        return 0

    # ------------------------------------------------------------------
    # Federation wiring (spec C.2, C.3, C.5)
    # ------------------------------------------------------------------

    def _build_federation(self, parser, bus):
        """FederationHealth construction gate and wiring.

        Constructed iff ``en_federation = 1`` and (``peer_list`` or
        ``cluster_members`` non-empty or ``register_with_coordinator = 1``).
        With only ``peer_list`` and none of the new keys the constructor call
        is the pre-extension one. New keyword arguments are passed only when
        their keys are present; an older FederationHealth that rejects them
        (TypeError) is constructed the old way and coordinator registration
        is reported unavailable. Returns the monitor (not started) or None.
        """
        self._fed_info = None
        self._fed_members = []
        if not parser.has_section('federation'):
            return None
        if parser.getint('federation', 'en_federation', fallback=0) != 1:
            return None
        reader = _OptionReader(parser, self.logger)
        peer_list_str = parser.get('federation', 'peer_list', fallback='')
        federation_peers = [p.strip() for p in peer_list_str.split(',') if p.strip()]
        members_str = reader.raw('federation', 'cluster_members', "")
        register_on = reader.flag('federation', 'register_with_coordinator')
        if not (federation_peers or members_str or register_on):
            return None

        status_base_port = parser.getint('monitoring', 'status_server_port', fallback=5002)
        health_mode = reader.choice('federation', 'health_mode', "mesh", HEALTH_MODES)

        # Instance-side mesh peers: peer_list (legacy form, today's meaning)
        # when non-empty, else cluster_members minus the self entry
        members = None
        if not federation_peers and members_str:
            members = self._mesh_members(members_str, status_base_port)
            self._fed_members = members

        legacy_kwargs = {}
        if reader.has('federation', 'health_poll_interval'):
            legacy_kwargs["poll_interval"] = reader.float('federation', 'health_poll_interval',
                                                          5.0, 0.2, 300.0)
        new_kwargs = {}
        for key, kwarg, kind, default, lo, hi in (
                ('peer_timeout', 'peer_timeout', float, 2.0, 0.1, 30.0),
                ('peer_up_successes', 'up_successes', int, 1, 1, 10),
                ('peer_down_misses', 'down_misses', int, None, 1, 20),
                ('peer_flap_window_sec', 'flap_window_sec', float, 0.0, 0.0, None),
                ('peer_flap_threshold', 'flap_threshold', int, 0, 0, None),
                ('health_workers', 'max_workers', int, 1, 1, 64)):
            if reader.has('federation', key):
                read = reader.float if kind is float else reader.int
                value = read('federation', key, default, lo, hi)
                if value is not None:
                    new_kwargs[kwarg] = value
        if reader.has('federation', 'emit_peer_health_events'):
            new_kwargs["emit_health_events"] = reader.flag('federation', 'emit_peer_health_events')
        if reader.has('federation', 'node_id'):
            new_kwargs["node_id"] = self.node_id
        if reader.has('federation', 'cluster_id'):
            new_kwargs["cluster_id"] = self.cluster_id
        auth_token = reader.raw('federation', 'auth_token', "")
        if auth_token:
            new_kwargs["auth_token"] = auth_token
        if reader.has('host', 'health_thread_sched_other'):
            new_kwargs["sched_other"] = reader.flag('host', 'health_thread_sched_other')

        if health_mode == "mesh":
            peer_addresses = federation_peers
            if members is not None:
                new_kwargs["members"] = members
        else:
            # hub: the peer table comes from the REGISTER ack; off: no polling
            peer_addresses = []
        register = self._register_config(reader, status_base_port, health_mode) if register_on else None
        if register is not None:
            new_kwargs["register"] = register

        try:
            from federation_health import FederationHealth
        except ImportError as e:
            self.logger.error("Failed to initialize federation health: {:s}".format(str(e)))
            return None
        base_kwargs = dict(instance_id=self.instance_id, peer_addresses=peer_addresses,
                           event_bus=bus, status_base_port=status_base_port,
                           port_stride=self.port_stride, **legacy_kwargs)
        try:
            if new_kwargs:
                try:
                    fh = FederationHealth(**base_kwargs, **new_kwargs)
                except TypeError as e:
                    self.logger.warning(
                        "FederationHealth does not accept the configured [federation] options "
                        "(%s); using the legacy constructor%s", e,
                        "; coordinator registration is unavailable" if register else "")
                    if "members" in new_kwargs:
                        base_kwargs["peer_addresses"] = [
                            "{}:{}".format("[{}]".format(m["host"]) if ":" in m["host"] else m["host"],
                                           m["instance_id"]) for m in members]
                    fh = FederationHealth(**base_kwargs)
                    register = None
            else:
                fh = FederationHealth(**base_kwargs)
        except Exception as e:
            self.logger.error("Failed to initialize federation health: {:s}".format(str(e)))
            return None
        polled = len(members) if (members is not None and health_mode == "mesh") else len(peer_addresses)
        self.logger.info("Federation health monitor configured with {:d} peers".format(polled))

        endpoint = None
        if register is not None:
            endpoint = "{}:{}".format(*register["endpoints"][0])
        else:
            host = reader.raw('federation', 'coordinator_host', "")
            if host:
                endpoint = "{}:{}".format(host, reader.int('federation', 'coordinator_port',
                                                           6000, 1, 65535))
        self._fed_info = {"health_mode": health_mode,
                          "members_from": "cluster_members" if members is not None else
                                          ("peer_list" if federation_peers else None),
                          "registering": register is not None,
                          "endpoint": endpoint}
        return fh

    def _mesh_members(self, members_str, status_base_port):
        """``cluster_members`` minus this instance's self entry (spec C.2).

        The self entry has ``instance_id == N`` and a host that is local or
        equals ``self_host(advertise_host)``. When no entry matches, every
        entry is polled (and a warning is logged).
        """
        try:
            from federation_common import parse_member_list, is_local_host, self_host
            parsed = parse_member_list(members_str, default_stride=self.port_stride,
                                       status_base_port=status_base_port)
        except (ImportError, ValueError) as e:
            self.logger.error("[federation] cluster_members is unusable: %s", e)
            return []
        try:
            own_host = self_host(self.advertise_host or None)
        except ValueError:
            own_host = None

        def is_self(member):
            if member["instance_id"] != self.instance_id:
                return False
            host = member["host"].lower()
            return host == own_host or is_local_host(host)

        if not any(is_self(m) for m in parsed):
            self.logger.warning("cluster_members does not contain this instance (expected "
                                "%s:%d); polling every entry", own_host, self.instance_id)
            return parsed
        return [m for m in parsed if not is_self(m)]

    def _register_config(self, reader, status_base_port, health_mode):
        """The FederationHealth ``register=`` dict (spec C.3), or None."""
        endpoints = []
        for item in reader.raw('federation', 'coordinator_candidates', "").split(","):
            item = item.strip()
            if not item:
                continue
            host, _, port = item.rpartition(":")
            try:
                port = int(port)
                if not host or not 1 <= port <= 65535:
                    raise ValueError(item)
            except ValueError:
                self.logger.warning("[federation] coordinator_candidates entry %r ignored "
                                    "(expected host:port)", item)
                continue
            endpoints.append((host.strip("[]"), port))
        if not endpoints:
            host = reader.raw('federation', 'coordinator_host', "")
            if host:
                endpoints.append((host, reader.int('federation', 'coordinator_port',
                                                   6000, 1, 65535)))
        if not endpoints:
            self.logger.error("register_with_coordinator = 1 but no coordinator endpoint is "
                              "configured (coordinator_candidates or coordinator_host); "
                              "registration disabled")
            return None
        self._register_meta = {
            "status_base_port": status_base_port,
            "zmq_pub_port": reader.int('monitoring', 'zmq_pub_port', 5003, 1, 65535),
            "want_peers": health_mode == "hub",
        }
        return {"endpoints": endpoints,
                "interval": reader.float('federation', 'register_interval_sec', 5.0, 0.5, 300.0),
                "body_fn": self._register_body,
                "want_peers": health_mode == "hub"}

    def _register_body(self):
        """REGISTER body (spec C.3); called by the registrar thread."""
        from federation_common import compute_ports, node_key, self_host
        meta = self._register_meta
        host = self_host(self.advertise_host or None)
        ports = compute_ports(self.instance_id, self.port_stride, meta["status_base_port"])
        ports["events"] = meta["zmq_pub_port"] + self.instance_id * self.port_stride
        health = None
        if self.status_server is not None:
            health = self.status_server.get_pipeline_health()
        return {"node_key": node_key(host, self.instance_id),
                "node_id": self.node_id,
                "cluster_id": self.cluster_id,
                "instance_id": self.instance_id,
                "advertise_host": host,
                "port_stride": self.port_stride,
                "ports": ports,
                "nonce": "{}-{}-{}".format(os.getpid(), self._process_start_ms,
                                           (self.config_sha256 or "")[:12]),
                "fw_version": FW_VERSION,
                "config_sha256": self.config_sha256,
                "health": health,
                "want_peers": meta["want_peers"]}

    def _federation_status(self):
        """STATUS ``federation`` block / PEERS source (None without FederationHealth)."""
        fh = self.federation_health
        if fh is None:
            return None
        info = self._fed_info or {}
        coordinator = {"endpoint": info.get("endpoint"), "role": None, "term": None,
                       "registered": False, "last_register_ok_age_sec": None,
                       "last_error": None}
        status_fn = getattr(fh, "get_coordinator_status", None)
        if info.get("registering") and callable(status_fn):
            reg = status_fn()
            if isinstance(reg, dict):
                coordinator.update({k: reg[k] for k in coordinator if k in reg})
                if "endpoints" in reg:
                    coordinator["endpoints"] = reg["endpoints"]
        leader = fh.get_coordinator_id()
        block = {"instance_id": self.instance_id,
                 "node_id": self.node_id,
                 "cluster_id": self.cluster_id,
                 "health_mode": info.get("health_mode", "mesh"),
                 "coordinator": coordinator,
                 "advisory_leader": leader,
                 "peers": fh.get_peer_table()}
        if info.get("members_from") == "cluster_members":
            block["advisory_leader_node_key"] = self._leader_node_key(leader)
        return block

    def _leader_node_key(self, leader):
        """Node key of the advisory leader when membership is cluster_members."""
        if leader == self.instance_id:
            try:
                from federation_common import node_key, self_host
                return node_key(self_host(self.advertise_host or None), self.instance_id)
            except (ImportError, ValueError):
                return None
        matches = [m["node_key"] for m in self._fed_members if m["instance_id"] == leader]
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------------------
    # hw_controller relays (spec G.3, G.5)
    # ------------------------------------------------------------------

    def _tick_relays(self):
        """Poll the relay files every RELAY_POLL_FRAMES frames."""
        self._relay_counter += 1
        if self._relay_counter >= RELAY_POLL_FRAMES:
            self._relay_counter = 0
            self._poll_relays()

    def _poll_relays(self):
        """Read the hwc_state document (re-emitting new hw_controller events
        on this bus, deduped by (epoch, seq)) and act on a new recal request."""
        if self._hwc_watcher is not None:
            doc, events = self._hwc_watcher.poll()
            self._hwc_doc = doc
            if events and self.event_bus is not None:
                self._reemit_hwc_events(events)
        if self._recal_watcher is not None:
            request = self._recal_watcher.poll()
            if request is not None:
                self._apply_wire_recal(request)

    def _reemit_hwc_events(self, events):
        from daq_events import DAQEvent
        for ev in events:
            payload = ev.get("payload")
            payload = dict(payload) if isinstance(payload, dict) else {"value": payload}
            payload["relayed"] = True
            try:
                timestamp = float(ev.get("timestamp") or 0.0)
            except (TypeError, ValueError):
                timestamp = 0.0
            self.event_bus.emit(DAQEvent(timestamp=timestamp,
                                         severity=str(ev.get("severity") or "info"),
                                         module=str(ev.get("module") or "hw_controller"),
                                         event_type=str(ev.get("event_type") or ""),
                                         payload=payload))

    def _apply_wire_recal(self, request):
        """RECL from the wire: restart the calibration from STATE_INIT."""
        epoch, gen, _ts_ms, reason = request
        self.logger.info("Recalibration request (%s, epoch %s, generation %d): "
                         "re-entering STATE_INIT", reason, epoch, gen)
        self.current_state = "STATE_INIT"
        self.sync_failed_cntr = 0
        self._cal_timeout_count = 0
        self._recal_generation = gen
        if self.event_bus is not None:
            from daq_events import DAQEvent, EVT_CAL_START
            self.event_bus.emit(DAQEvent(severity="info", module="delay_sync",
                                         event_type=EVT_CAL_START,
                                         payload={"reason": reason, "generation": gen}))
        if self.db is not None:
            self.db.put_cal_event(CAL_EVENT_CAL_START, self.iq_header,
                                  sync_state_before=self._last_sync_state, sync_state_after=1)

    def _hwc_status(self):
        """hwc provider of the status server: ``(view, error)``."""
        doc = self._hwc_doc
        if doc is None:
            return None, "hwc state unreadable (missing or torn {})".format(
                self._hwc_watcher.path if self._hwc_watcher is not None else "file")
        import hwc_relay
        return hwc_relay.hwc_status_view(doc), None

    # ------------------------------------------------------------------
    # Extended STATUS (spec G.1, G.2)
    # ------------------------------------------------------------------

    def _effective_status_interval(self):
        """k_eff: frames between snapshot refreshes."""
        if self._status_update_interval > 0:
            return self._status_update_interval
        return self._heartbeat_interval

    def _update_continuity(self, header):
        """Continuity counters over consecutive input headers and the
        slot-102 overrun detection of [calibration] invalidate_on_overrun."""
        cpi = header.cpi_index
        block = header.daq_block_index
        overruns = header.get_buffer_overrun_cnt()
        if self._prev_cpi_index is not None and cpi != self._prev_cpi_index + 1:
            self._cpi_gaps += 1
        if self._prev_daq_block_index is not None and block < self._prev_daq_block_index:
            self._daq_block_gaps += 1
        self._overrun_increased = (self._prev_overrun_cnt is not None
                                   and overruns > self._prev_overrun_cnt)
        self._prev_cpi_index = cpi
        self._prev_daq_block_index = block
        self._prev_overrun_cnt = overruns
        if self._invalidate_on_overrun and self._overrun_increased:
            self._overrun_invalidate_left = 1 + self._frames_after_overrun

    def _extended_counters(self):
        counters = {"buffer_overrun_cnt": self.iq_header.get_buffer_overrun_cnt(),
                    "cpi_gaps": self._cpi_gaps,
                    "daq_block_gaps": self._daq_block_gaps,
                    "overrun_invalidated_frames": self._overrun_invalidated_frames}
        if self._stamp_decimator_drops:
            counters["decimator_drops"] = self.iq_header.get_decimator_drop_cnt()
        if self._hwc_watcher is not None:
            counters["hwc_relay_errors"] = self._hwc_watcher.errors
        return counters

    def _build_extended_status(self):
        """Additive top-level STATUS keys of status_extended = 1."""
        header = self.iq_header
        period = self.frame_period_sec
        quality = self._cal_quality
        return {
            "node_id": self.node_id,
            "cluster_id": self.cluster_id,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "fw_version": FW_VERSION,
            "ini_version": self.ini_version,
            "config_sha256": self.config_sha256,
            "num_channels": self.M,
            "sampling_freq_hz": self.sample_rate,
            "cpi_length": self.N,
            "decimation_ratio": self.R,
            "frame_period_sec": None if period is None else round(period, 6),
            "frame_type": header.frame_type,
            "cpi_index": header.cpi_index,
            "last_frame_ts_ms": header.time_stamp,
            "fsm_state": _strip_state(self.current_state),
            "cal": {"quality_db": round(quality, 3) if np.isfinite(quality) else None,
                    "iq_compensations": self.iq_compensation_cntr,
                    "sample_compensations": self.sample_compensation_cntr,
                    "track_mode": self.cal_track_mode,
                    "recal_generation": self._recal_generation},
        }

    # ------------------------------------------------------------------
    # Per-frame helpers of the FSM / outbound header
    # ------------------------------------------------------------------

    def _compute_corr_functions(self, iq_samples):
        """|IFFT(conj(X) Y)|^2 of every channel against the reference
        (STATE_SAMPLE_CAL), on the xcorr FFT engine, into self.corr_functions."""
        engine = self.xcorr_engine if self.xcorr_engine is not None else self.fft_engine
        np_zeros = np.zeros(self.N_proc, dtype=np.complex64)
        x_padd = np.concatenate([iq_samples[self.std_ch_ind, 0:self.N_proc], np_zeros])
        x_fft = engine.forward(x_padd, overwrite_x=True)

        for m in self.channel_list:
            y_padd = np.concatenate([np_zeros, iq_samples[m, 0:self.N_proc]])
            y_fft = engine.forward(y_padd, overwrite_x=True)
            self.corr_functions[m,:] = np.abs(engine.inverse(x_fft.conj() * y_fft, overwrite_x=True))**2

    def _amplitude_out_of_tolerance(self, ratios):
        return amplitude_out_of_tolerance(ratios, self.amp_diff_tolerance,
                                          self.amp_diff_tolerance_db,
                                          self.amplitude_tolerance_mode)

    def _track_cal_timeout(self, state_before, sync_state):
        """[calibration] cal_timeout_frames: count consecutive CAL frames
        processed in the calibration states; warn once per calibration run."""
        if (self.current_state not in CALIBRATION_STATES
                or state_before not in CALIBRATION_STATES):
            # Outside the calibration states, or entering them from outside
            # (STATE_INIT after start-up, a frequency change or a wire
            # recalibration): the next CAL frame starts a new run
            self._cal_timeout_count = 0
            return
        if self.iq_header.frame_type != IQHeader.FRAME_TYPE_CAL:
            return
        self._cal_timeout_count += 1
        if self._cal_timeout_count != self._cal_timeout_frames:
            return
        state = _strip_state(state_before)
        self.logger.warning("Calibration timeout: %d consecutive calibration frames in "
                            "the calibration states (state %s, sync_state %d)",
                            self._cal_timeout_count, state, sync_state)
        if self.event_bus is not None:
            from daq_events import DAQEvent, EVT_CAL_TIMEOUT
            self.event_bus.emit(DAQEvent(severity="warning", module="delay_sync",
                                         event_type=EVT_CAL_TIMEOUT,
                                         payload={"state": state,
                                                  "frames": self._cal_timeout_count}))
        if self.db is not None:
            self.db.put_cal_event(CAL_EVENT_CAL_TIMEOUT, self.iq_header,
                                  sync_state_before=sync_state, sync_state_after=sync_state)

    def _finalize_header(self, sample_sync_flag, iq_sync_flag, sync_state):
        """Sync flags, sync state and the opt-in reserved-slot telemetry of
        the outbound header (slots 108, 110-113; slot 109 and every other
        input slot are carried through by the decode/encode round trip)."""
        if sample_sync_flag:
            self.iq_header.delay_sync_flag=1
        else:
            self.iq_header.delay_sync_flag=0
        if iq_sync_flag:
            self.iq_header.iq_sync_flag=1
        else:
            self.iq_header.iq_sync_flag=0

        # [calibration] invalidate_on_overrun: no sync claim on the frame
        # that saw a USB ring overrun and the next frames_after_overrun ones
        if self._overrun_invalidate_left > 0:
            self._overrun_invalidate_left -= 1
            self._overrun_invalidated_frames += 1
            self.iq_header.delay_sync_flag = 0
            self.iq_header.iq_sync_flag = 0

        self.iq_header.sync_state = sync_state
        self._last_sync_state = sync_state

        # -> Stamp RF front-end + orientation telemetry into the reserved region (v8)
        if self._en_frontend_telemetry:
            self._stamp_frontend_telemetry()

        # -> Calibration telemetry (slots 110-113) and node attribution (108)
        if self._stamp_cal_telemetry:
            self.iq_header.set_cal_quality_mdb(self._cal_quality * 1000)
            self.iq_header.set_iq_comp_cnt(self.iq_compensation_cntr)
            self.iq_header.set_sample_comp_cnt(self.sample_compensation_cntr)
            self.iq_header.set_recal_gen(self._recal_generation)
        if self._stamp_node_id:
            self.iq_header.set_node_id_crc32(self._node_id_crc32)

    def _stamp_frontend_telemetry(self):
        """Populate the v8 reserved-region telemetry on the outbound header:
        the aggregate channel-power scan objective, per-channel total system
        gain / cascaded noise figure / compression (from the amplification
        config and the tuner if_gains), and the antenna bearing relayed from
        hw_controller via a small control file."""
        # Aggregate received power objective (milli-dB)
        self.iq_header.set_aggregate_power_mdb(self._agg_power_mdb)

        # Throttled relay-file polling (hw_controller -> delay_sync)
        self._orientation_read_counter += 1
        poll_relay_files = self._orientation_read_counter >= 5
        if poll_relay_files:
            self._orientation_read_counter = 0

        # Gain budget from the external LNA chain + antenna profile.
        # The Friis cascade only changes when the tuner gains or the RF center
        # frequency change, so the computed slots are cached on that key.
        if self.rf_frontend is not None:
            cache_key = (tuple(self.iq_header.if_gains[0:min(self.M, 32)]),
                         self.iq_header.rf_center_freq)
            if cache_key != self._budget_cache_key:
                ext_tenths = [int(round(g * 10)) for g in self.rf_frontend.ext_lna_gains_db]
                total_tenths = []
                nf_mdb = []
                comp = 0
                for m in range(min(self.M, 32)):
                    tuner_gain_db = self.iq_header.if_gains[m] / 10.0
                    budget = self.rf_frontend.channel_budget(
                        m, tuner_gain_db, antenna_profile=self.antenna_profile,
                        freq_hz=self.iq_header.rf_center_freq)
                    total_tenths.append(int(round(budget["total_gain_db"] * 10)))
                    nf_mdb.append(int(round(budget["system_nf_db"] * 1000)))
                    if budget["compressed"]:
                        comp |= (1 << m)
                self._budget_cache = (ext_tenths, total_tenths, nf_mdb, comp)
                self._budget_cache_key = cache_key
            ext_tenths, total_tenths, nf_mdb, comp = self._budget_cache
            self.iq_header.set_ext_lna_gains(ext_tenths)
            self.iq_header.set_total_system_gains(total_tenths)
            self.iq_header.set_system_nf_mdb(nf_mdb)
            self.iq_header.set_compression_flags(comp)

            # Runtime bias-tee state relayed from hw_controller (throttled read)
            if poll_relay_files:
                try:
                    with open(self._bias_tee_state_fname, 'r') as f:
                        self._bias_tee_mask = int(f.read().split()[0])
                except (OSError, ValueError, IndexError):
                    pass
            self.iq_header.set_bias_tee_state(self._bias_tee_mask)

        # Antenna bearing / rotator state relayed from hw_controller (throttled read)
        if self._en_orientation:
            if poll_relay_files:
                try:
                    with open(self._orientation_state_fname, 'r') as f:
                        parts = f.read().split()
                    if len(parts) >= 3:
                        self._orientation_cache = (float(parts[0]), float(parts[1]), int(float(parts[2])))
                except (OSError, ValueError):
                    pass
            az, el, state = self._orientation_cache
            self.iq_header.set_antenna_bearing(az, el)
            self.iq_header.set_rotator_state(state)

    def open_interfaces(self):
        """
            Opens the communication interfaces of the module including the
            input and output shared memory interfaces and the FIFO control interface.
            
            Input shared memory interface: IQ data from the decimator module
            Out shared memory interfaces: Towards the IQ Server or the DSP module and to
            the Hardware Controller module.

            Through the Control FIFO interface the module sends delay compensation values 
            to the sync module.

            Return values:
            --------------
                :return: 0: All interfaces have been succesfully initialized
                        -1: Failed to initialize one the interfaces
        """ 
        # Open RTL-DAQ control socket (bounded timeouts so a wedged/dead
        # rtl_daq degrades the sync loop instead of deadlocking it)
        self.zmq_port = 1130 + self.instance_id * self.port_stride
        self.zmq_context = zmq.Context()
        self.rtl_daq_socket = self._create_rtl_daq_socket()

        # Open input interface to receive data from the decimator
        if self.transport_type == 'shm':
            self.in_shmem_iface = inShmemIface("decimator_out", instance_id=self.instance_id)
        else:
            self.in_shmem_iface = TransportConsumer("decimator_out",
                                     instance_id=self.instance_id,
                                     transport_type=self.transport_type)
        if not self.in_shmem_iface.init_ok:
            self.logger.critical("Input interface (Decimator) initialization failed, exiting..")
            return -1

        # Open output interface towards the iq server module
        if self.N >= self.N_proc: out_shmem_size = int(1024+self.N*2*self.M*(32/8))
        else: out_shmem_size = int(1024+self.N_proc*2*self.M*(32/8))
        if self.transport_type == 'shm':
            self.out_shmem_iface_iq = outShmemIface("delay_sync_iq",
                                     out_shmem_size,
                                     drop_mode = True,
                                     instance_id=self.instance_id)
        else:
            self.out_shmem_iface_iq = TransportProducer("delay_sync_iq",
                                     out_shmem_size,
                                     drop_mode = True,
                                     instance_id=self.instance_id,
                                     transport_type=self.transport_type)
        if not self.out_shmem_iface_iq.init_ok:
            self.logger.critical("Output interface (IQ server) initialization failed, exiting..")
            return -1

        # Open output interface towards the hardware controller module.
        # Only the 1024-byte IQ header is ever relayed on this ring (the HWC
        # module does not consume IQ samples), so the segments are sized to
        # the header alone - the consumer sizes its buffers from the segment.
        hwc_shmem_size = 1024
        if self.transport_type == 'shm':
            self.out_shmem_iface_hwc = outShmemIface("delay_sync_hwc",
                                     hwc_shmem_size,
                                     drop_mode = True,
                                     instance_id=self.instance_id)
        else:
            self.out_shmem_iface_hwc = TransportProducer("delay_sync_hwc",
                                     hwc_shmem_size,
                                     drop_mode = True,
                                     instance_id=self.instance_id,
                                     transport_type=self.transport_type)
        if not self.out_shmem_iface_hwc.init_ok:
            self.logger.critical("Output interface (HWC) initialization failed, exiting..")
            return -1
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
        'command not confirmed' (typically: stay in the current FSM state and
        retry on a later frame).
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
    def close_interfaces(self):
        """
            Close the communication and data interfaces that are opened during the start of the module
        """
        if self.in_shmem_iface is not None:
            self.in_shmem_iface.destory_sm_buffer()
                
        if self.out_shmem_iface_iq is not None:
            self.out_shmem_iface_iq.send_ctr_terminate()
            sleep(2)
            self.out_shmem_iface_iq.destory_sm_buffer()        

        if self.out_shmem_iface_hwc is not None:
            self.out_shmem_iface_hwc.send_ctr_terminate()
            sleep(2)
            self.out_shmem_iface_hwc.destory_sm_buffer()  
            
        self.logger.info("Interfaces are closed")
    def calc_iq_sync(self, iq_samples):
        """
            This function calculates the synchronization status of the signal processing channels.
            
            Implementation notes:
            ---------------------
            It checks the sample level synchrony with calculating the cross correlation fucntion of 
            all the channels at two points. At zero and at non-zero offsets. In case the value obtained
            at zero offset is not remarkably higher than the value calculated at non-zero offet, we can
            consider the channels to be misaligned. 

            The amplitude and phase offsets are determined from the eigendecomposition of the spatial-correlation matrix

            Parameters:
            -----------
                :param: iq_samples: Processed IQ samples  (May contain less samples than what can be found in a frame)
                :type : iq_samples: Complex 2D numpy array
            
            Return values:
            --------------
                :return: dyn_ranges: Estimated peak-to-sidelobe ratio of the cross-correlation functions
                :return: iq_diffs  : Amplitude and Phase differences across the channels
                :rtype : dyn_ranges: list of floats
                :rtype : iq_diffs  : Complex 1D numpy array
                
        """
        dyn_ranges = []

        # Calculate cross-correlations to check sample level synchrony
        # (the conjugated standard channel is hoisted out of the loop - this
        # runs on every data frame in track mode)
        std_conj = iq_samples[self.std_ch_ind, :].conj()
        for m in self.channel_list:
            # Correlation at zero offset
            corr_at_zero_m   = self.N_proc / (np.dot(iq_samples[m, :], std_conj))
            # Correlation at the spcified offset
            corr_at_offset_m =  self.N_proc / (np.dot(iq_samples[m, self.corr_peak_offset::],
                                                   std_conj[0:-self.corr_peak_offset]))
            # Check dynamic range
            dyn_ranges.append(-20*np.log10(abs(corr_at_zero_m) / abs(corr_at_offset_m)))

        # Calculate Spatial correlation matrix to determine amplitude-phase missmatches
        Rxx = iq_samples.dot(np.conj(iq_samples.T))
        # Perform eigen-decomposition (Rxx is Hermitian PSD by construction,
        # so eigh applies; its eigenvalues are real, ascending - the dominant
        # eigenvector is the last column)
        eigenvalues, eigenvectors = lin.eigh(Rxx)
        vmax  = eigenvectors[:, -1]
        iq_diffs = 1 / vmax
        iq_diffs /= iq_diffs[self.std_ch_ind]

        # Amplitude correction -  scaling IQ diferences
        if self.amplitude_cal_mode == "channel_power":
            channel_powers = np.einsum('ij,ij->i', iq_samples, iq_samples.conj()).real / self.N_proc
            iq_diffs       = iq_diffs / np.abs(iq_diffs) * \
                             np.sqrt(channel_powers[self.std_ch_ind] / channel_powers)
        elif self.amplitude_cal_mode == "disabled":
            iq_diffs        = iq_diffs / np.abs(iq_diffs)

            return np.array(dyn_ranges), iq_diffs

        if self.logger.isEnabledFor(logging.DEBUG):
            for i, m in enumerate(self.channel_list):
                self.logger.debug("Channel: {:d}, Peak dyn. range: {:.2f}[min: {:.2f}], Amp.:{:.2f}, Phase:{:.2f} ".format(\
                                m, dyn_ranges[i], self.min_corr_peak_dyn_range, 20*np.log10(abs(iq_diffs[m])),
                                np.rad2deg(np.angle(iq_diffs[m]))))

        return np.array(dyn_ranges), iq_diffs
    def estimate_frac_delays(self, iq_samples, block_size=2**10):
        """
            This function estimates the fractional sample delay between the coherent receiver channels
            
            Implementation notes:
            ---------------------
            The estimation is performed based on the phase-frequency difference curve of the channels, which theroretical can be
            described with a linear curve. The slope of this curve is in direct relation to time delay between the two channels.
            In the first processing step the phase-frequency function is estimated  and in the second step a linear curve is fitted
            to the estimated values.
            
            The phase-frequency function estimation is realized by splitting the full signal array into smaller block, on which 
            the phase difference is estimated individually. The block-wise obtained results are then are averaged. 
            The sample size of a cohrent-block is controlled by the "block_size" parameter of the function.

            Parameters:
            -----------
                :param: iq_samples: Processed IQ samples  (May contain less samples than what can be found in a frame)
                :param: block_size: Number of samples in coherent block (default:1024)
                
                :type : iq_samples: Complex 2D numpy array
                :type : block_size: int
            
            Return values:
            --------------
                :return: taus: Estimated fractional sample delays for the
                               non-reference channels, ordered as
                               self.channel_list (all channels except
                               self.std_ch_ind)
                :rtype : taus: List of floats

            Implementation notes:
            ---------------------
            - The reference channel is self.std_ch_ind (not hardcoded 0).
            - The input array is NEVER modified: the constant phase correction
              is applied to the frequency-domain blocks (FFT is linear), so the
              outbound shared-memory frame this array may alias is untouched.
            - On the scipy backend the per-block transforms are issued as one
              batched 2-D FFT per channel instead of N//block_size Python
              iterations.
        """

        """
            Initialization
        """
        taus = []
        N = iq_samples.shape[1] # Number of samples
        M = iq_samples.shape[0] # Number of channels
        ref = self.std_ch_ind   # Reference (standard) channel index
        n_blocks = N // block_size

        freq_scale = np.arange(-0.5,0.5,1/block_size)
        fit_mask   = np.logical_and(freq_scale < 0.4, freq_scale > -0.4)

        batched = (getattr(self.fft_engine, 'engine_type', None) == 'cpu_scipy')
        """
            Processing
        """
        # Estimate phase transfer
        #  - Transform standard channel to frequency domain block-wise
        if batched:
            std_ch_w_block = fft.fftshift(self.fft_engine.forward(
                iq_samples[ref, 0:n_blocks*block_size].reshape(n_blocks, block_size),
                overwrite_x=False), axes=1)
        else:
            std_ch_w_block = np.zeros((n_blocks, block_size), dtype=np.complex64)
            for block_index, block_start in enumerate(np.arange(0,N, block_size)):
                std_ch_w_block[block_index,:] =  fft.fftshift(self.fft_engine.forward(iq_samples[ref, block_start:block_start+block_size], overwrite_x=False))

        for m in [ch for ch in range(M) if ch != ref]:
            # Constant phase offset between the reference and this channel
            phase_shift_w0 = np.average(iq_samples[ref]*iq_samples[m].conj())

            if batched:
                corr_ch_w_block = fft.fftshift(self.fft_engine.forward(
                    iq_samples[m, 0:n_blocks*block_size].reshape(n_blocks, block_size),
                    overwrite_x=False), axes=1)
                # Apply the phase correction in the frequency domain (linearity)
                corr_ch_w_block *= phase_shift_w0
                # Phase transfer with non-coherent integration over the blocks
                phase_diff_w_m = np.mean(std_ch_w_block/corr_ch_w_block, axis=0)
            else:
                phase_diff_w_m = np.zeros(block_size, dtype=np.complex64)
                for block_index, block_start in enumerate(np.arange(0,N, block_size)):
                    corr_ch_w_block  = fft.fftshift(self.fft_engine.forward(iq_samples[m, block_start:block_start+block_size], overwrite_x=False))
                    corr_ch_w_block *= phase_shift_w0
                    phase_diff_w_m += std_ch_w_block[block_index,:]/corr_ch_w_block
                phase_diff_w_m /= n_blocks # Normalization

            angle_diff_w = np.angle(phase_diff_w_m).real # Convert complex phasor to angle

            # Fit linear curve on to the estimated phase transfers and derive
            # fractional delay (least-squares line fit - polyfit is equivalent
            # to the previous 2-parameter curve_fit, without scipy.optimize)
            slope, _ = np.polyfit(freq_scale[fit_mask], angle_diff_w[fit_mask], 1)
            taus.append(slope/(2*np.pi))

        return taus


    def start(self):
        """
            Start the main processing loop
        """
        while True:
            ch_powers        = None # Per-channel power estimate for the metrics DB

            #############################################
            #           OBTAIN NEW DATA FRAME           #  
            #############################################
            
            # Acquire data
            active_buff_index_dec = self.in_shmem_iface.wait_buff_free()
            if self.metrics is not None:
                _t_frame_start = time.monotonic()
            if active_buff_index_dec == TERMINATE:
                self.logger.info("Terminate signal received, exiting")
                break
            elif active_buff_index_dec < 0 or active_buff_index_dec > 1:
                self.logger.critical("Failed to acquire new data frame, exiting..")
                break;
            iq_frame_buffer_in = self.in_shmem_iface.buffers[active_buff_index_dec]

            # Read and convert header
            iq_header_bytes = iq_frame_buffer_in[0:1024].tobytes()
            self.iq_header.decode_header(iq_header_bytes)            
            #self.iq_header.dump_header()
            
            if self.iq_header.check_sync_word():
                self.logger.critical("IQ header sync word check failed, exiting..")
                break
            
            # Prepare payload buffer
            incoming_payload_size = self.iq_header.cpi_length*self.iq_header.active_ant_chs*2*int(self.iq_header.sample_bit_depth/8)
            if incoming_payload_size > 0:
                iq_samples_in = (iq_frame_buffer_in[1024:1024 + incoming_payload_size].view(dtype=np.complex64))\
                                .reshape(self.iq_header.active_ant_chs, self.iq_header.cpi_length)
                
            # Get buffers from the sink blocks (IQ server, HW controller)
            active_buffer_index_iq = self.out_shmem_iface_iq.wait_buff_free()
            active_buffer_index_hwc = self.out_shmem_iface_hwc.wait_buff_free() 
            
            self.logger.debug("Type:{:d}, CPI: {:d}, State:{:s}".format(
                    self.iq_header.frame_type, 
                    self.iq_header.cpi_index, 
                    self.current_state))

            # -> Input-header continuity / overrun tracking (opt-in)
            if self._track_continuity:
                self._update_continuity(self.iq_header)
            # -> hw_controller relays: state document and wire recalibration (opt-in)
            if self._hwc_watcher is not None or self._recal_watcher is not None:
                self._tick_relays()

            #############################################
            #  Delay Synchronizer Finite State Machine  #
            #############################################

            if (self.iq_header.frame_type != IQHeader.FRAME_TYPE_DUMMY) and incoming_payload_size > 0:
                ch_powers = self._preprocess_frame(iq_samples_in, active_buffer_index_iq)
            sample_sync_flag, iq_sync_flag, sync_state = self._fsm_step()

            #############################################
            #        DATABASE: FRAME METRICS            #
            #############################################
            if self.db is not None and self.iq_header.frame_type != IQHeader.FRAME_TYPE_DUMMY:
                self.db.put_frame_metrics(self.iq_header, channel_powers=ch_powers,
                                          snr=0.0, cal_quality=self._cal_quality)

            #############################################
            #         SEND PROCESSED DATA BLOCK         #
            #############################################
            # -> Update header fields (flags, sync state, reserved-slot telemetry)
            self._finalize_header(sample_sync_flag, iq_sync_flag, sync_state)

            # -> Send IQ frame toward the iq server
            header_uint8 = np.frombuffer(self.iq_header.encode_header(), dtype=np.uint8)
            if active_buffer_index_iq in (0, 1):
                (self.out_shmem_iface_iq.buffers[active_buffer_index_iq])[0:1024] = header_uint8
                self.out_shmem_iface_iq.send_ctr_buff_ready(active_buffer_index_iq)
            else:
                if not self.ignore_frame_drop_warning: self.logger.warning("Dropping frame - IQ server, Total: {:d}".format(self.out_shmem_iface_iq.dropped_frame_cntr))
                if self.event_bus is not None:
                    from daq_events import DAQEvent, EVT_FRAME_DROP
                    self.event_bus.emit(DAQEvent(severity="warning", module="delay_sync",
                        event_type=EVT_FRAME_DROP, payload={"target": "iq_server",
                            "total": self.out_shmem_iface_iq.dropped_frame_cntr}))

            # -> Send IQ frame toward the hwc module
            if active_buffer_index_hwc in (0, 1):
                (self.out_shmem_iface_hwc.buffers[active_buffer_index_hwc])[0:1024] = header_uint8
                # TODO: For ADPIS control HWC module should get informed about the power levels from the header
                self.out_shmem_iface_hwc.send_ctr_buff_ready(active_buffer_index_hwc)
            else:
                if not self.ignore_frame_drop_warning: self.logger.warning("Dropping frame - HWC, Total: {:d}".format(self.out_shmem_iface_hwc.dropped_frame_cntr))
                if self.event_bus is not None:
                    from daq_events import DAQEvent, EVT_FRAME_DROP
                    self.event_bus.emit(DAQEvent(severity="warning", module="delay_sync",
                        event_type=EVT_FRAME_DROP, payload={"target": "hwc",
                            "total": self.out_shmem_iface_hwc.dropped_frame_cntr}))

            # -> Performance metrics recording
            if self.metrics is not None:
                _t_now = time.monotonic()
                self.metrics.record("frame_processing_latency_ms", (_t_now - _t_frame_start) * 1000)
                if self._last_frame_time > 0:
                    self.metrics.record("frame_throughput_fps", 1.0 / (_t_now - self._last_frame_time))
                self._last_frame_time = _t_now
                # Record per-frame drop DELTAS so METRICS min/avg/p95 describe
                # the drop rate; the raw cumulative counters remain available
                # in the STATUS snapshot's "counters" dict below.
                _drops_iq = self.out_shmem_iface_iq.dropped_frame_cntr
                _drops_hwc = self.out_shmem_iface_hwc.dropped_frame_cntr
                self.metrics.record("dropped_frames_iq", float(max(0, _drops_iq - self._prev_dropped_iq)))
                self.metrics.record("dropped_frames_hwc", float(max(0, _drops_hwc - self._prev_dropped_hwc)))
                self._prev_dropped_iq = _drops_iq
                self._prev_dropped_hwc = _drops_hwc

            # -> Periodic heartbeat and status update
            self._heartbeat_and_status(sync_state)

            # -> Inform the preceeding block that we have finished the processing
            self.in_shmem_iface.send_ctr_buff_ready(active_buff_index_dec)

    def _heartbeat_and_status(self, sync_state):
        """Heartbeat event every heartbeat_interval frames and STATUS
        snapshot refresh (spec G.2).

        status_update_interval = 0 (default): the snapshot is refreshed with
        the heartbeat, as before. k > 0: every k frames, independently of the
        heartbeat. With status_extended = 1 it is also refreshed whenever
        sync_state, frame_type or rf_center_freq changes.
        """
        heartbeat_fired = False
        if self._heartbeat_interval > 0:
            self._heartbeat_counter += 1
            if self._heartbeat_counter >= self._heartbeat_interval:
                self._heartbeat_counter = 0
                heartbeat_fired = True
                if self.event_bus is not None:
                    from daq_events import DAQEvent, EVT_HEARTBEAT
                    self.event_bus.emit(DAQEvent(severity="info", module="delay_sync",
                        event_type=EVT_HEARTBEAT, payload={"cpi_index": self.iq_header.cpi_index,
                            "sync_state": sync_state}))
        if self.status_server is None:
            return
        if self._status_update_interval > 0:
            self._status_counter += 1
            refresh = self._status_counter >= self._status_update_interval
            if refresh:
                self._status_counter = 0
        else:
            refresh = heartbeat_fired
        if self._status_extended:
            key = (sync_state, self.iq_header.frame_type, self.iq_header.rf_center_freq)
            if key != self._last_status_key:
                self._last_status_key = key
                refresh = True
        if refresh:
            self.status_server.update_status(self._build_status_snapshot(sync_state))

    def _build_status_snapshot(self, sync_state):
        """The STATUS snapshot; additive extended keys only with status_extended = 1."""
        snapshot = {
            "sync_state": sync_state,
            "current_frequency_hz": self.iq_header.rf_center_freq,
            "frame_count": self.iq_header.cpi_index,
            "aggregate_power_db": self._agg_power_mdb / 1000.0,
            "calibration_status": "locked" if sync_state >= 5 else ("calibrating" if sync_state >= 2 else "lost"),
            "counters": {
                "dropped_frames_iq": self.out_shmem_iface_iq.dropped_frame_cntr,
                "dropped_frames_hwc": self.out_shmem_iface_hwc.dropped_frame_cntr,
                "sync_fails_total": self.sync_failed_cntr_total,
                "sample_compensations": self.sample_compensation_cntr,
                "iq_compensations": self.iq_compensation_cntr,
            },
        }
        if self._status_extended:
            snapshot["counters"].update(self._extended_counters())
            snapshot.update(self._build_extended_status())
        return snapshot

    def _preprocess_frame(self, iq_samples_in, active_buffer_index_iq):
        """IQ preprocessing of a non-dummy frame with a payload.

        Copies (or IQ-corrects) the input payload into the outbound frame (or
        the drop scratch / the input itself when the outbound buffer was
        dropped), truncates it for the FSM into ``self._iq_samples`` and
        returns the per-channel powers for the metrics DB (None without DB).
        """
        ch_powers = None
        if active_buffer_index_iq in (0, 1):
            iq_frame_buffer_out = (self.out_shmem_iface_iq.buffers[active_buffer_index_iq]).view(dtype=np.complex64)
            # IQ header offset:1 sample -> 8 byte, 1024 byte length header -> 128 "sample"
            iq_samples_out = iq_frame_buffer_out[128:128+self.iq_header.cpi_length*self.iq_header.active_ant_chs].reshape(self.iq_header.active_ant_chs, self.iq_header.cpi_length)

            if self.en_iq_cal:
                iq_samples_out = correct_iq(iq_samples_in, iq_samples_out, self.iq_corrections, self.M)
            else:
                iq_samples_out = copy_iq(iq_samples_in, iq_samples_out, self.M)
        elif self._skip_copy_on_drop and not self.en_iq_cal:
            # IQ output buffer dropped and nothing to correct: process the input
            # frame in place ([offload] skip_copy_on_drop = 1; copy_iq is a pure
            # element copy, so every downstream result is identical)
            iq_samples_out = iq_samples_in
        else:
            # IQ output buffer dropped - process into a reusable
            # scratch frame instead of allocating one per drop
            scratch_shape = (self.iq_header.active_ant_chs, self.iq_header.cpi_length)
            if self._drop_scratch is None or self._drop_scratch.shape != scratch_shape:
                self._drop_scratch = np.empty(scratch_shape, dtype=np.complex64)
            iq_samples_out = self._drop_scratch
            if self.en_iq_cal:
                iq_samples_out = correct_iq(iq_samples_in, iq_samples_out, self.iq_corrections, self.M)
            else:
                iq_samples_out = copy_iq(iq_samples_in, iq_samples_out, self.M)

        # Truncate IQ sample matrix for further processing
        if self.iq_header.frame_type == IQHeader.FRAME_TYPE_CAL:
            iq_samples = iq_samples_out[:,:] # payload size must be N_proc
        elif self.N >= self.N_proc: iq_samples = iq_samples_out[:,0:self.N_proc] # Normal truncation
        else: iq_samples = iq_samples_out[:,:] # Only N sample is availabe in data frames

        # -> Aggregate received power for the scan-and-peak objective (v8 telemetry)
        if self._en_frontend_telemetry and self.iq_header.frame_type == IQHeader.FRAME_TYPE_DATA:
            p_lin = float(np.real(np.vdot(iq_samples, iq_samples))) / max(iq_samples.size, 1)
            self._agg_power_mdb = int(round(10.0 * np.log10(p_lin + 1e-20) * 1000.0))

        # -> Per-channel power estimates for the frame-metrics
        #    database record (only computed when the DB is enabled)
        if self.db is not None:
            ch_powers = np.einsum('ij,ij->i', iq_samples,
                                  iq_samples.conj()).real / max(iq_samples.shape[1], 1)

        self._iq_samples = iq_samples
        return ch_powers

    def _fsm_step(self):
        """Run the delay-synchronizer FSM on the current frame.

        Reads ``self.iq_header`` and ``self._iq_samples`` (the truncated
        sample matrix of the last frame with a payload) and returns
        ``(sample_sync_flag, iq_sync_flag, sync_state)`` for the outbound
        header.
        """
        sample_sync_flag = False
        iq_sync_flag     = False
        sync_state       = 0
        iq_samples       = self._iq_samples
        state_before     = self.current_state

        if (self.iq_header.frame_type != IQHeader.FRAME_TYPE_DUMMY):  # Check frame type
            #
            #------------------------------------------>
            #            
            if self.current_state == "STATE_INIT": 
                sync_state = 1
                # Recalculate IQ adjustment for the RF center frequency
                daq_rf           = self.iq_header.rf_center_freq # Read RF center frequency for phase offset calculation
                if self.iq_adjust_source == "explicit-time-delay":
                    iq_adjust_phase  = self.iq_adjust_time*daq_rf*2*np.pi  # Convert time delay to phase         

                    self.iq_adjust = self.iq_adjust_amplitude * np.exp(1j*iq_adjust_phase) # Assemble IQ adjustment vector
                    self.iq_adjust = np.insert(self.iq_adjust, self.std_ch_ind, 1+0j)
                elif self.iq_adjust_source == "touchstone":
                    self.iq_adjust = self.iq_adjust_table[np.argmin(abs(self.iq_adjust_table[:,0]-daq_rf)),1::]

                    self.logger.debug(f"IQ adjustment vector: abs:{abs(self.iq_adjust)}")
                    self.logger.debug(f"IQ adjustment vector: phase:{np.rad2deg(np.angle(self.iq_adjust))}")

                self.iq_adjust /= self.iq_adjust[self.std_ch_ind]
                # Reset IQ corrections
                self.iq_corrections    = np.ones(self.M, dtype=np.complex64) 
                self.iq_corrections[:] = self.iq_adjust[:]
                # Calibration frame
                if self.iq_header.frame_type == IQHeader.FRAME_TYPE_CAL:
                    self.current_state = "STATE_SAMPLE_CAL"
                    self._cal_start_frame = self.iq_header.cpi_index
                    if self.db is not None:
                        self.db.put_cal_event(CAL_EVENT_CAL_START, self.iq_header,
                                              sync_state_before=1, sync_state_after=2)
                    if self.event_bus is not None:
                        from daq_events import DAQEvent, EVT_CAL_START
                        self.event_bus.emit(DAQEvent(severity="info", module="delay_sync",
                            event_type=EVT_CAL_START, payload={"freq": self.iq_header.rf_center_freq}))

            #
            #------------------------------------------>
            #
            elif self.current_state == "STATE_SAMPLE_CAL":
                sync_state        = 2
                sample_sync_flag  = True
                delay_update_flag = 0
                fs_ppm_offsets=[0]*self.M 

                # ->  Calculate correlation functions
                self._compute_corr_functions(iq_samples)
                # ->  Calculate sample delays, check dynamic range
                # WARNING: This dynamic range checking assumes dirac like coorelation peak                    
                for m in self.channel_list:
                    peak_index = np.argmax(self.corr_functions[m, :])

                    # Check dynamic range. The sidelobe sample is taken with a
                    # wrapped index so a peak within corr_peak_offset samples of
                    # the end (badly desynced channel) cannot over-index, and
                    # degenerate (all-zero) correlations are treated as
                    # insufficient dynamic range instead of dividing by zero.
                    peak_val     = self.corr_functions[m, peak_index]
                    sidelobe_val = self.corr_functions[m, (peak_index+self.corr_peak_offset) % (2*self.N_proc)]
                    if peak_val > 0 and sidelobe_val > 0:
                        dyn_range = 10*np.log10(peak_val / sidelobe_val)
                    else:
                        dyn_range = 0.0
                    if dyn_range < self.min_corr_peak_dyn_range:
                        self.logger.warning("Correlation peak dynamic range is insufficient to perform calibration")
                        self.logger.warning("Real value: {:.2f}, minimum: {:.2f}".format(dyn_range, self.min_corr_peak_dyn_range))
                        delay_update_flag = 0
                        sample_sync_flag = False # Sync can not be checked properly
                        break

                    # Calculate sample offset
                    self.delays[m] = (self.N_proc - peak_index)

                    if np.abs(self.delays[m]) >= 1:
                        sample_sync_flag = False # Misalling detected
                        delay_update_flag=1
                else:
                    # Every channel had a usable correlation peak: derive the
                    # sampling-rate tune values from the measured delays
                    fs_ppm_offsets = int_fs_ppm_offsets(self.delays, self.INT_FS_TUNE_GAIN,
                                                        self.MIN_FS_PPM_OFFSET, self.MAX_FS_PPM_OFFSET)
                    if self.logger.isEnabledFor(logging.DEBUG):
                        for m in self.channel_list:
                            self.logger.debug("Channel {:d}, delay: {:d}, tune gain: {:d} ppm-offset: {:.7f}, ".format(
                                m, self.delays[m], int_fs_tune_gain(self.delays[m], self.INT_FS_TUNE_GAIN), fs_ppm_offsets[m]))

                # Set time delay
                if delay_update_flag:
                    msg_byte_array = inter_module_messages.pack_msg_sample_freq_tune(self.module_identifier, fs_ppm_offsets)
                    if self._rtl_daq_transaction(msg_byte_array) is not None:
                        self.last_update_ind=self.iq_header.cpi_index
                        self.current_state = "STATE_SYNC_WAIT"
                    # On a failed transaction stay in this state and retry

                if sample_sync_flag:
                    self.sample_compensation_cntr+=1 # Used to track how many succesfull compenssation have been performed so far
                    self.current_state = "STATE_FRAC_SAMPLE_CAL"
                    if self.db is not None:
                        self.db.put_cal_event(CAL_EVENT_SAMPLE_CAL_DONE, self.iq_header,
                                              delays=self.delays,
                                              sync_state_before=2, sync_state_after=3)
            #
            #------------------------------------------>
            #
            elif self.current_state == "STATE_SYNC_WAIT":
                sync_state = 3                    

                # Hold this state until the first summy frame arrives
                if self.last_update_ind+1 == self.iq_header.cpi_index:
                    self.last_update_ind += 1
            #
            #------------------------------------------>
            #                
            elif self.current_state == "STATE_FRAC_SAMPLE_CAL":
                sync_state          = 3
                # TODO: Change sync state -> changes have to take effect in the HWC module as well
                # Calculate fractional delays (truncate the SAMPLE axis to N_proc)
                taus = self.estimate_frac_delays(iq_samples[:, 0:self.N_proc])
                self.logger.debug(f"Fractional delays: {taus}")

                # Determine and set tune values
                # taus are ordered as self.channel_list (all channels
                # except the reference channel self.std_ch_ind)
                fs_ppm_offsets = frac_fs_ppm_offsets(taus, self.channel_list, self.M,
                                                     self.frac_delay_tolerance, self.FRAC_FS_TUNE_GAIN,
                                                     self.MIN_FS_PPM_OFFSET)
                frac_delay_update_flag = any(abs(taus[i]) > self.frac_delay_tolerance
                                             for i in range(len(self.channel_list)))

                if frac_delay_update_flag:
                    self.logger.debug(f"Sending ppm offsets: {fs_ppm_offsets}")
                    msg_byte_array = inter_module_messages.pack_msg_sample_freq_tune(self.module_identifier, fs_ppm_offsets)
                    if self._rtl_daq_transaction(msg_byte_array) is not None:
                        self.last_update_ind=self.iq_header.cpi_index
                        self.current_state = "STATE_FRAC_SYNC_WAIT"
                    # On a failed transaction stay in this state and retry
                else:
                    self.current_state = "STATE_IQ_CAL"
            #
            #------------------------------------------>
            #
            elif self.current_state == "STATE_FRAC_SYNC_WAIT":
                sync_state          = 3

                # Hold this state until the first summy frame arrives
                if self.last_update_ind+1 == self.iq_header.cpi_index:
                    self.last_update_ind += 1                           
            #
            #------------------------------------------>
            #
            elif self.current_state == "STATE_IQ_CAL":
                sync_state          = 4
                iq_corr_update_flag = False
                sample_sync_flag    = True
                iq_sync_flag        = True                

                if self.en_iq_cal:
                    dyn_ranges, iq_diffs = self.calc_iq_sync(iq_samples)
                    iq_diffs *= self.iq_adjust[:]
                    if dyn_ranges.size:
                        self._cal_quality = float(np.min(dyn_ranges))

                    if (dyn_ranges < self.min_corr_peak_dyn_range).any():
                        self.logger.warning("Correlation peak dynamic range is insufficient to perform calibration")
                        for m in range(self.M-1):                        
                            self.logger.warning("Real value: {:.2f}, minimum: {:.2f}".format(dyn_ranges[m], self.min_corr_peak_dyn_range))                        

                        sample_sync_flag = False # It seems that the sample sync has lost
                        iq_sync_flag = False
                        iq_corr_update_flag = False                            

                    # Check IQ calibration necessity (over ALL channels)
                    elif (abs(np.rad2deg(np.angle(iq_diffs))) > self.phase_diff_tolerance).any() or \
                         self._amplitude_out_of_tolerance(iq_diffs):
                        iq_corr_update_flag = True
                        self.logger.debug("Amplitude or phase differenceas are out of tolerance")

                    # Update correction values if needed                
                    if iq_corr_update_flag:
                        iq_sync_flag = False
                        self.iq_compensation_cntr+=1  # Used to track how many iq compensations have we issued so far
                        self.logger.info("Updating IQ correction values")                                        
                        self.iq_corrections *= iq_diffs
                        self.logger.info("Amplitude differences: {0}".format(20*np.log10(np.abs(iq_diffs))))
                        self.logger.info("Phase differernces: {0}".format(np.rad2deg(np.angle(iq_diffs))))

                if not sample_sync_flag:
                    self.current_state = "STATE_SAMPLE_CAL"

                if sample_sync_flag and iq_sync_flag:
                    self.current_state = "STATE_TRACK_LOCK"
                    if self.db is not None:
                        self.db.put_cal_event(CAL_EVENT_IQ_CAL_DONE, self.iq_header,
                                              iq_corrections=self.iq_corrections,
                                              sync_state_before=4, sync_state_after=5)
                        self.db.put_cal_event(CAL_EVENT_TRACK_LOCK, self.iq_header,
                                              iq_corrections=self.iq_corrections,
                                              sync_state_before=4, sync_state_after=5)
                        # Per-frequency scan aggregate: successful calibration
                        self.db.update_freq_scan(self.iq_header.rf_center_freq,
                                                 cal_success=True,
                                                 cal_quality=self._cal_quality,
                                                 iq_corrections=self.iq_corrections,
                                                 delays=self.delays,
                                                 num_channels=self.M)
                    if self.event_bus is not None:
                        from daq_events import DAQEvent, EVT_SYNC_LOCK
                        self.event_bus.emit(DAQEvent(severity="info", module="delay_sync",
                            event_type=EVT_SYNC_LOCK, payload={"freq": self.iq_header.rf_center_freq}))
                    if self.metrics is not None:
                        convergence = self.iq_header.cpi_index - self._cal_start_frame
                        self.metrics.record("cal_convergence_frames", float(convergence))
                    if self.cal_track_mode == 2 and self.en_iq_cal:
                        self.iq_diff_ref[:] = iq_diffs[:]
            #
            #------------------------------------------>
            #
            elif self.current_state == "STATE_TRACK_LOCK":
                sync_state       = 5
                sample_sync_flag = True
                iq_sync_flag     = True
                # Wait here until the calibration frame is turned off
                if self.iq_header.frame_type == IQHeader.FRAME_TYPE_DATA: # Normal data frame
                    dyn_ranges, iq_diffs = self.calc_iq_sync(iq_samples)
                    iq_diffs *= self.iq_adjust[:]
                    if dyn_ranges.size:
                        self._cal_quality = float(np.min(dyn_ranges))

                    if self.cal_track_mode == 1:
                        self.iq_diff_ref[:] = iq_diffs[:]
                    self.current_state = "STATE_TRACK"
                    self.last_rf = self.iq_header.rf_center_freq
                    # [calibration] track_decimation: the first tracking frame is checked
                    self._track_check_counter = 0
                    self._track_last_flags = (True, True)

            #
            #------------------------------------------>
            #
            elif self.current_state == "STATE_TRACK":
                sync_state = 6
                # Caltrack mode 0: Calibration tracking is disabled
                # Caltrack mode 1: Normal continous tracking
                # Caltrack mode 2: Track only on calibration frames

                track_check = (self.cal_track_mode == 1 or
                               (self.cal_track_mode == 2 and self.iq_header.frame_type == IQHeader.FRAME_TYPE_CAL))
                run_check = track_check
                if (track_check and self.cal_track_mode == 1 and self._track_decimation > 1
                        and self.iq_header.frame_type == IQHeader.FRAME_TYPE_DATA):
                    # [calibration] track_decimation = k: check every k-th DATA frame
                    # and reuse the last check's flags in between; other frame
                    # types (a CAL burst while locked) are always checked
                    run_check = (self._track_check_counter % self._track_decimation) == 0
                    self._track_check_counter += 1

                if run_check:

                    dyn_ranges, iq_diffs = self.calc_iq_sync(iq_samples)
                    iq_diffs *= self.iq_adjust[:]
                    if dyn_ranges.size:
                        self._cal_quality = float(np.min(dyn_ranges))
                    # Check sample sync loss
                    if (dyn_ranges < self.min_corr_peak_dyn_range).any():
                        self.logger.warning("Sample sync may lost")
                        sample_sync_flag = False
                    else:
                        sample_sync_flag = True

                    if self.en_iq_cal:
                        # Check IQ sync loss
                        if (abs(np.rad2deg(np.angle(iq_diffs/self.iq_diff_ref))) > self.phase_diff_tolerance).any() or \
                           self._amplitude_out_of_tolerance(iq_diffs/self.iq_diff_ref):
                               iq_sync_flag = False
                               self.logger.warning("IQ sync may lost")
                               for m in range(self.M):
                                   self.logger.debug("Differences: Amplitude {:.2f}, Phase: {:.2f}".format(
                                           20*np.log10((abs(iq_diffs[m]/self.iq_diff_ref[m]))), 
                                           (abs(np.rad2deg(np.angle(iq_diffs[m]/self.iq_diff_ref[m]))))))
                        else:
                            iq_sync_flag = True

                    # Track loss control
                    if (not sample_sync_flag) or (self.en_iq_cal and (not iq_sync_flag)):
                        self.sync_failed_cntr +=1
                        self.sync_failed_cntr_total+=1
                    else:
                        self.sync_failed_cntr -=1                       
                    if self.sync_failed_cntr == self.max_sync_fails:
                        self.current_state = "STATE_INIT"
                        self.sync_failed_cntr = 0
                        if self.db is not None:
                            self.db.put_cal_event(CAL_EVENT_TRACK_LOST, self.iq_header,
                                                  iq_corrections=self.iq_corrections,
                                                  sync_state_before=6, sync_state_after=1)
                            # Per-frequency scan aggregate: calibration lost
                            self.db.update_freq_scan(self.iq_header.rf_center_freq,
                                                     cal_success=False,
                                                     cal_quality=self._cal_quality,
                                                     num_channels=self.M)
                        if self.event_bus is not None:
                            from daq_events import DAQEvent, EVT_SYNC_LOST
                            self.event_bus.emit(DAQEvent(severity="warning", module="delay_sync",
                                event_type=EVT_SYNC_LOST, payload={"total_fails": self.sync_failed_cntr_total}))
                    elif self.sync_failed_cntr < 0: # Sync tracking holds
                        self.sync_failed_cntr = 0
                    self._track_last_flags = (sample_sync_flag, iq_sync_flag)

                elif track_check:
                    # Decimated tracking: no check on this frame, keep the last flags
                    sample_sync_flag, iq_sync_flag = self._track_last_flags

                else:
                    self.logger.debug("Sync flags are set")
                    sample_sync_flag = True
                    iq_sync_flag = True

                # Has the RF center frequency changed?
                if self.last_rf != self.iq_header.rf_center_freq:
                    self.logger.info("Center frequency changed, initiating recalibration")
                    sample_sync_flag = False
                    iq_sync_flag = False
                    self.sync_failed_cntr = 0
                    self.current_state = "STATE_INIT"
                    if self.db is not None:
                        self.db.put_cal_event(CAL_EVENT_FREQ_CHANGE, self.iq_header,
                                              iq_corrections=self.iq_corrections,
                                              sync_state_before=6, sync_state_after=1)
                    if self.event_bus is not None:
                        from daq_events import DAQEvent, EVT_FREQ_CHANGE
                        self.event_bus.emit(DAQEvent(severity="info", module="delay_sync",
                            event_type=EVT_FREQ_CHANGE, payload={
                                "old_freq": self.last_rf,
                                "new_freq": self.iq_header.rf_center_freq}))

            # Per-frame statistic - DEBUG level to keep string formatting
            # and log I/O out of the hot loop at the default INFO level
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("Delay track statistic [sync fails ,sample, iq, total][{:d},{:d},{:d}/{:d}]".format(
                                 self.sync_failed_cntr_total,
                                 self.sample_compensation_cntr,
                                 self.iq_compensation_cntr,
                                 self.iq_header.daq_block_index))

        elif (self.iq_header.frame_type == IQHeader.FRAME_TYPE_DUMMY): 
            # Reset instantaneous sync failed counter (New noise burst will start)
            self.sync_failed_cntr = 0

            # To speed up the calibration, this first call frame will be checked immediatly
            if self.current_state == "STATE_SYNC_WAIT":
                self.current_state = "STATE_SAMPLE_CAL"
            elif self.current_state == "STATE_FRAC_SYNC_WAIT":
                self.current_state = "STATE_FRAC_SAMPLE_CAL"

        if self._cal_timeout_frames > 0:
            self._track_cal_timeout(state_before, sync_state)

        return sample_sync_flag, iq_sync_flag, sync_state

@njit(fastmath=True, cache=True)
def correct_iq(iq_samples_in, iq_samples_out, iq_corrections, M):
    for m in range(M):
        iq_samples_out[m,:] = (iq_samples_in[m,:]-np.mean(iq_samples_in[m,:]))*iq_corrections[m]

    return iq_samples_out

@njit(fastmath=True, cache=True)
def copy_iq(iq_samples_in, iq_samples_out, M):
    for m in range(M):
        iq_samples_out[m,:] = iq_samples_in[m,:]

    return iq_samples_out


if __name__ == '__main__':
    import signal

    def _sig_handler(signum, frame):
        # Raise SystemExit so the finally block below runs the normal
        # clean-shutdown path (close event bus/db, propagate TERMINATE on the
        # outbound FIFOs via close_interfaces). daq_stop.sh sends SIGTERM.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sig_handler)

    # Defence in depth (HEIMDALL_INSTANCE_GUARD=refuse only): refuse to run
    # without the inherited instance lock, before any interface is opened
    _lock_rc = enforce_instance_lock("daq_chain_config.ini")
    if _lock_rc:
        sys.exit(_lock_rc)

    delay_synchronizer_inst0 = delaySynchronizer()
    try:
        if delay_synchronizer_inst0.open_interfaces() == 0:
            if delay_synchronizer_inst0.status_server is not None:
                delay_synchronizer_inst0.status_server.start()
            if delay_synchronizer_inst0.federation_health is not None:
                delay_synchronizer_inst0.federation_health.start()
            if delay_synchronizer_inst0.event_bus is not None:
                from daq_events import DAQEvent, EVT_PROCESS_START
                delay_synchronizer_inst0.event_bus.emit(DAQEvent(severity="info",
                    module="delay_sync", event_type=EVT_PROCESS_START, payload={}))
            delay_synchronizer_inst0.start()
    finally:
        if delay_synchronizer_inst0.event_bus is not None:
            from daq_events import DAQEvent, EVT_PROCESS_STOP
            delay_synchronizer_inst0.event_bus.emit(DAQEvent(severity="info",
                module="delay_sync", event_type=EVT_PROCESS_STOP, payload={}))
            delay_synchronizer_inst0.event_bus.close()
        if delay_synchronizer_inst0.federation_health is not None:
            delay_synchronizer_inst0.federation_health.close()
        if delay_synchronizer_inst0.status_server is not None:
            delay_synchronizer_inst0.status_server.close()
        if delay_synchronizer_inst0.db is not None:
            delay_synchronizer_inst0.db.close()
        delay_synchronizer_inst0.close_interfaces()
