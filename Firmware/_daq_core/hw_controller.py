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
import re
import logging
import json
import os
import tempfile
import time
import zlib
from collections import deque

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
import hwc_relay
import federation_common
from version import FW_VERSION
from daq_db_records import (CAL_EVENT_NOISE_ON, CAL_EVENT_NOISE_OFF,
                            CAL_EVENT_FREQ_CHANGE, CAL_EVENT_GAIN_CHANGE)

U32_MAX = 0xFFFFFFFF


def _fsm_wire_name(state):
    """FSM name on the wire: the hw_controller state without 'STATE_' (spec 0.1)."""
    return state[6:] if state.startswith("STATE_") else state


def _json_default(obj):
    """json.dumps fallback for numpy scalars/arrays inside event payloads."""
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


def _jsonable(obj):
    """A JSON-serialisable deep copy of *obj* (numpy values converted)."""
    try:
        return json.loads(json.dumps(obj, default=_json_default))
    except (TypeError, ValueError):
        return {"repr": str(obj)}


class CtrRequest:
    """One in-flight control request handed from the CtrIfaceServer thread to
    the HWC main loop.

    State machine (spec D.6.2): PENDING -> RUNNING -> DONE, or
    PENDING -> CANCELLED. Both transitions out of PENDING are taken under
    `lock`, so a request whose wait timed out and was cancelled by the server
    thread is provably never executed, and a request the main loop already
    started is never reported as cancelled. The server thread waits on `done`
    and then reads `reply_payload` (bytes, query replies only, None otherwise)
    and `result` = (applied, reason) with `fsm` = FSM name after the handler.
    `term` is the fencing term of the submitting connection, `extended` its
    XACK flag."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"

    def __init__(self, command, params, term=0, extended=False):
        self.command = command
        self.params = params
        self.done = threading.Event()
        self.reply_payload = None
        self.lock = threading.Lock()
        self.state = self.PENDING
        self.result = None
        self.fsm = None
        self.term = term
        self.extended = extended
        self.issued_at = time.monotonic()

    @property
    def cancelled(self):
        """True once the server thread withdrew the request (read-only)."""
        return self.state == self.CANCELLED

    def try_start(self):
        """Main loop: PENDING -> RUNNING. False when the request was cancelled."""
        with self.lock:
            if self.state != self.PENDING:
                return False
            self.state = self.RUNNING
            return True

    def try_cancel(self):
        """Server thread: PENDING -> CANCELLED. False when it already runs or ran."""
        with self.lock:
            if self.state != self.PENDING:
                return False
            self.state = self.CANCELLED
            return True

    def finish(self, result, fsm=None):
        """Main loop: record (applied, reason) and wake the server thread."""
        with self.lock:
            self.result = result
            self.fsm = fsm
            self.state = self.DONE
        self.done.set()


class ConnState:
    """Per-connection state of the :5001 control server (spec D.6).

    xack        : the connection completed an XACK handshake (extended acks)
    term        : fencing term the connection declared in XACK (0 = none)
    cluster_bad : the XACK was refused (cluster mismatch / term jump); mutating
                  verbs are answered FNSD+zeros and not executed
    sched_session : the SCHB/SCHC reassembly session (at most one)
    """

    def __init__(self, xack=False, term=0, cluster_bad=False, sched_session=None):
        self.xack = xack
        self.term = term
        self.cluster_bad = cluster_bad
        self.sched_session = sched_session
        self.sent_xack = False      # any XACK seen (drives ctl_xack_idle_sec)
        self.idle_applied = False


class SchedSession:
    """One SCHB/SCHC chunked schedule upload being reassembled."""

    def __init__(self, sess, total_len, crc32, nchunks):
        self.sess = sess
        self.total_len = total_len
        self.crc32 = crc32
        self.nchunks = nchunks
        self.buf = bytearray()
        self.next_seq = 0
        self.last_activity = time.monotonic()


class ControlPlane:
    """State shared by the :5001 server thread(s) and the HWC main loop.

    Holds the persisted fencing term (`max_term_seen`, `<P>fence_term`), the
    per-frame control snapshot answered by HWSQ/SCHG without touching the
    request queue, the ack sequence counter, the track-lock file accessors
    and the static values advertised in XACK. Every method is thread-safe.
    """

    BASE_CAPS = "hwsq,schb,schg,tlck"
    MAX_RTO_MS = 600000
    MAX_HMX_MS = 120000
    MAX_FP_MS = 999999

    def __init__(self, M, cluster_id="default", fence_path=None,
                 fence_max_term_jump=1000, en_wire_recal=False,
                 request_timeout_sec=10.0, handler_max_sec=12.0,
                 frame_period_sec=0.0, track_lock_path="_data_control/iq_track_lock",
                 require_track_lock=False):
        self.logger = logging.getLogger(__name__)
        self.M = M
        self.cluster_id = cluster_id
        self.fence_path = fence_path
        self.fence_max_term_jump = fence_max_term_jump
        self.en_wire_recal = bool(en_wire_recal)
        self.rto_ms = min(self.MAX_RTO_MS, int(round(request_timeout_sec * 1000)))
        self.hmx_ms = min(self.MAX_HMX_MS, int(round(handler_max_sec * 1000)))
        self.fp_ms = min(self.MAX_FP_MS, max(0, int(round(frame_period_sec * 1000))))
        self.track_lock_path = track_lock_path
        self.require_track_lock = bool(require_track_lock)
        self.fence_lock = threading.Lock()
        self.max_term_seen = 0
        self._snap_lock = threading.Lock()
        self._snap = {"fsm": "INIT", "pend_deferred": 0, "ns": 0, "agc": 0,
                      "nch": M, "f": 0, "fc": 0, "gen": None, "loaded": False,
                      "active": False, "st": "IDLE", "idx": 0, "total": 0,
                      "last_frame_mono": None}
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._sess_id = 0

    # ---- fencing (spec C.4.5) ----

    def load_fence(self):
        """Load max_term_seen from the fence file (0 when absent/unreadable)."""
        term = 0
        if self.fence_path:
            try:
                with open(self.fence_path, "rb") as f:
                    raw = f.read(64).decode("ascii").strip()
                term = int(raw)
                if not 0 <= term <= U32_MAX:
                    raise ValueError("term out of range")
            except FileNotFoundError:
                term = 0
            except (OSError, ValueError, UnicodeDecodeError) as e:
                self.logger.warning("Ignoring unreadable fence term file %s (%s); fence term 0",
                                    self.fence_path, e)
                term = 0
        with self.fence_lock:
            self.max_term_seen = term
        if term:
            self.logger.info("Fence term %d loaded from %s", term, self.fence_path)
        return term

    def _persist_fence(self, term):
        """Atomically write the fence term (unique temp + fsync + os.replace)."""
        if not self.fence_path:
            return
        directory = os.path.dirname(self.fence_path) or "."
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".fence_term.")
            with os.fdopen(fd, "wb") as f:
                f.write("{:d}\n".format(term).encode("ascii"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.fence_path)
            tmp = None
            try:
                dfd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        except OSError as e:
            self.logger.error("Failed to persist fence term %d to %s: %s",
                              term, self.fence_path, e)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def is_stale(self, term):
        """Fence check at execution time: 0 < term < max_term_seen."""
        with self.fence_lock:
            return 0 < term < self.max_term_seen

    def caps(self):
        return self.BASE_CAPS + (",recl" if self.en_wire_recal else "")

    def handle_xack(self, term, cluster_id, conn):
        """Evaluate one XACK for *conn*; returns the reply dict (spec D.6.2)."""
        conn.sent_xack = True
        with self.fence_lock:
            if cluster_id and cluster_id != self.cluster_id:
                conn.cluster_bad = True
                conn.xack = False
                self.logger.warning("XACK from cluster '%s' refused (own cluster_id '%s')",
                                    cluster_id, self.cluster_id)
                return {"xack": False, "error": "cluster_mismatch", "term": self.max_term_seen}
            if cluster_id and term > self.max_term_seen + self.fence_max_term_jump:
                conn.cluster_bad = True
                conn.xack = False
                self.logger.warning("XACK term %d refused: jump above fence term %d + %d",
                                    term, self.max_term_seen, self.fence_max_term_jump)
                return {"xack": False, "error": "term_jump", "term": self.max_term_seen}
            conn.cluster_bad = False
            conn.xack = True
            conn.term = term
            if cluster_id and term > self.max_term_seen:
                self.max_term_seen = term
                self._persist_fence(term)
                self.logger.info("Fence term raised to %d", term)
            max_term = self.max_term_seen
        return {"xack": True, "v": 1, "nch": self.M, "term": max_term,
                "caps": self.caps(), "rto": self.rto_ms, "fp": self.fp_ms,
                "hmx": self.hmx_ms}

    # ---- control snapshot (spec D.6.2, R4) ----

    def publish(self, snap, frame=False):
        """Replace the snapshot; frame=True stamps the frame arrival time."""
        with self._snap_lock:
            last = time.monotonic() if frame else self._snap.get("last_frame_mono")
            self._snap = dict(snap)
            self._snap["last_frame_mono"] = last

    def snapshot(self):
        with self._snap_lock:
            return dict(self._snap)

    def fsm(self):
        with self._snap_lock:
            return self._snap.get("fsm", "INIT")

    def hwsq(self, queued=0):
        """HWSQ reply dict; `queued` = requests waiting in ctr_request_queue."""
        s = self.snapshot()
        last = s.get("last_frame_mono")
        if last is None:
            age = -1
        else:
            age = int((time.monotonic() - last) * 1000)
        fresh = 1 if 0 <= age <= max(2000, 3 * self.fp_ms) else 0
        with self.fence_lock:
            term = self.max_term_seen
        return {"fsm": s["fsm"], "pend": min(99, s["pend_deferred"] + queued),
                "ns": s["ns"], "agc": s["agc"], "nch": s["nch"],
                "f": min(max(0, int(s["f"])), U32_MAX), "fage": min(age, 99999),
                "fr": fresh, "fc": s["fc"] % 1000000, "t": term}

    def schg(self):
        s = self.snapshot()
        return {"gen": s["gen"], "loaded": s["loaded"], "active": s["active"],
                "st": s["st"], "idx": min(s["idx"], 99999), "total": min(s["total"], 99999)}

    # ---- counters ----

    def next_seq(self):
        with self._seq_lock:
            self._seq = (self._seq + 1) & U32_MAX
            return self._seq

    def next_session_id(self):
        with self._seq_lock:
            self._sess_id = self._sess_id % 0xFFFF + 1
            return self._sess_id

    # ---- track lock file (spec B.2) ----

    def read_track_lock(self):
        """True/False = first byte is b'1'; raises OSError when unreadable.
        The file is re-opened on every call."""
        with open(self.track_lock_path, "rb") as f:
            return f.read(1) == b"1"

    def write_track_lock(self, approved):
        """Write the approval byte in place (r+b, or wb when missing), truncate,
        flush and fsync, like an operator's `echo 1 > file`."""
        try:
            f = open(self.track_lock_path, "r+b")
        except FileNotFoundError:
            f = open(self.track_lock_path, "wb")
        with f:
            f.seek(0)
            f.write(b"1" if approved else b"0")
            f.truncate()
            f.flush()
            os.fsync(f.fileno())


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
        self._track_lock_error_logged = False
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

        # :5001 control-plane keys (spec D.6.1; defaults reproduce today)
        self.ctl_request_timeout_sec = CtrIfaceServer.REQUEST_TIMEOUT_S
        self.ctl_handler_max_sec = 12.0
        self.ctl_max_connections = 1
        self.ctl_xack_idle_sec = 30.0
        self.ctl_idle_timeout_sec = 0.0
        self.frame_period_sec = 0.0
        self.sample_rate = 2400000
        self.decimation_ratio = 1

        # Identity ([federation] node_id / cluster_id) and member-side fencing
        self.node_id = None
        self.cluster_id = "default"
        self.fence_max_term_jump = 1000

        # Calibration / burst / bias-tee opt-in keys (D.6.2, G.5, I.8)
        self.en_wire_recal = False
        self.defer_control_during_burst = False
        self.cal_stall_frames = 0
        self.recal_on_gain_change = False
        self.apply_bias_tee_at_init = False
        self.hw_bias_tee = None          # [hw] en_bias_tee as a per-channel list
        self.recal_generation = 0
        self._cal_stall_cntr = 0
        self.cal_stalled = False

        # Schedule generation id of the last loaded SCHD document (SCHG)
        self.schedule_gen = None

        # hw_controller state + events relay ([monitoring] en_hwc_state_relay, G.3)
        self.epoch = hwc_relay.make_epoch()
        self.en_hwc_state_relay = False
        self.event_tag_identity = False
        self._relay_lock = threading.Lock()
        self._relay_events = deque(maxlen=32)
        self._relay_dirty = False
        self._relay_seq = 0
        self._relay_frame_cntr = 0
        self._relay_write_failed = False

        # Frame bookkeeping for the control snapshot
        self._frame_count = 0
        self._last_frame_mono = None
        self._frame_interval_ms = -1

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

        # Per-instance control file names (<P> = inst<N>_ for N > 0)
        self._resolve_control_paths()

        # Control plane shared with the :5001 server thread (fence, snapshot)
        self.ctl = ControlPlane(self.M, cluster_id=self.cluster_id,
                                fence_path=self.fence_term_fname,
                                fence_max_term_jump=self.fence_max_term_jump,
                                en_wire_recal=self.en_wire_recal,
                                request_timeout_sec=self.ctl_request_timeout_sec,
                                handler_max_sec=self.ctl_handler_max_sec,
                                frame_period_sec=self.frame_period_sec,
                                track_lock_path=self.track_lock_ctr_fname,
                                require_track_lock=self.require_track_lock_intervention)
        self.ctl.load_fence()
        self._publish_ctl_snapshot()

        # Control interface server
        hwc_port = 5001 + self.instance_id * self.port_stride
        self.ctr_iface_server = CtrIfaceServer(self.M, port=hwc_port,
                                               listen_address=self.listen_address,
                                               control=self.ctl,
                                               request_timeout_sec=self.ctl_request_timeout_sec,
                                               handler_max_sec=self.ctl_handler_max_sec,
                                               max_connections=self.ctl_max_connections,
                                               xack_idle_sec=self.ctl_xack_idle_sec,
                                               idle_timeout_sec=self.ctl_idle_timeout_sec)
        self.ctr_iface_server.start()

        self.logger.info("Antenna channles {:d}".format(self.M))
        self.logger.info("IQ samples per channel {:d}".format(self.N))        
        self.logger.info("Processing sample size: {:d}".format(self.N_proc))
        self.logger.info("ADPIS state: {:d}".format(self.en_adpis))
        self.logger.info("Hardware Controller initialized")

    def _resolve_control_paths(self):
        """Per-instance `_data_control` file names: unprefixed for instance 0,
        `inst<N>_`-prefixed for N > 0 (like the FIFOs and shm segments)."""
        prefix = "" if self.instance_id == 0 else "inst{:d}_".format(self.instance_id)
        self.track_lock_ctr_fname = "_data_control/{:s}iq_track_lock".format(prefix)
        self._orientation_state_fname = "_data_control/{:s}orientation_state".format(prefix)
        self._bias_tee_state_fname = "_data_control/{:s}bias_tee_state".format(prefix)
        self.fence_term_fname = "_data_control/{:s}fence_term".format(prefix)
        self.hwc_state_fname = hwc_relay.hwc_state_path(prefix)
        self.recal_request_fname = hwc_relay.recal_request_path(prefix)

    # ---- small config helpers (new keys parse defensively: bad -> default + WARNING) ----

    def _cfg_float(self, parser, section, key, default, lo, hi):
        raw = parser.get(section, key, fallback=None) if parser.has_section(section) else None
        if raw is None or not raw.strip():
            return default
        try:
            value = float(raw)
        except ValueError:
            value = None
        if value is None or not lo <= value <= hi:
            self.logger.warning("[{:s}] {:s} = {:s} is invalid (expected {} .. {}); using {}".format(
                section, key, raw.strip(), lo, hi, default))
            return default
        return value

    def _cfg_int(self, parser, section, key, default, lo, hi, quiet=False):
        """quiet=True is for EXISTING keys that hw_controller only reads for
        derived values: a value it cannot parse falls back silently (debug
        log only), so the default path's log text is unchanged."""
        raw = parser.get(section, key, fallback=None) if parser.has_section(section) else None
        if raw is None or not raw.strip():
            return default
        try:
            value = int(raw.strip())
        except ValueError:
            value = None
        if value is None or not lo <= value <= hi:
            log = self.logger.debug if quiet else self.logger.warning
            log("[{:s}] {:s} = {:s} is invalid (expected {} .. {}); using {}".format(
                section, key, raw.strip(), lo, hi, default))
            return default
        return value

    @staticmethod
    def _atoi(token):
        """C atoi(): optional sign + leading decimal digits, anything else 0."""
        m = re.match(r'\s*([+-]?\d+)', token)
        return int(m.group(1)) if m else 0

    def _cfg_flag(self, parser, section, key):
        return self._cfg_int(parser, section, key, 0, 0, 1) == 1
        
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

        # :5001 control-plane keys (D.6.1)
        self.ctl_request_timeout_sec = self._cfg_float(parser, 'daq', 'ctl_request_timeout_sec',
                                                       CtrIfaceServer.REQUEST_TIMEOUT_S, 1.0, 600.0)
        self.ctl_handler_max_sec = self._cfg_float(parser, 'daq', 'ctl_handler_max_sec', 12.0, 1.0, 120.0)
        self.ctl_max_connections = self._cfg_int(parser, 'daq', 'ctl_max_connections', 1, 1, 64)
        self.ctl_xack_idle_sec = self._cfg_float(parser, 'daq', 'ctl_xack_idle_sec', 30.0, 0.0, 86400.0)
        self.ctl_idle_timeout_sec = self._cfg_float(parser, 'daq', 'ctl_idle_timeout_sec', 0.0, 0.0, 86400.0)
        # Frame period Ncpi * R / fs (advertised in XACK as fp, drives HWSQ fr)
        self.sample_rate = self._cfg_int(parser, 'daq', 'sample_rate', 2400000, 1, 2**31 - 1,
                                         quiet=True)
        self.decimation_ratio = self._cfg_int(parser, 'pre_processing', 'decimation_ratio', 1, 1,
                                              2**31 - 1, quiet=True)
        self.frame_period_sec = self.N * self.decimation_ratio / float(self.sample_rate)

        # Calibration-loop opt-ins (G.5, D.6.2, I.8)
        self.en_wire_recal = self._cfg_flag(parser, 'calibration', 'en_wire_recal')
        self.defer_control_during_burst = self._cfg_flag(parser, 'calibration', 'defer_control_during_burst')
        self.cal_stall_frames = self._cfg_int(parser, 'calibration', 'cal_stall_frames', 0, 0, 2**31 - 1)
        self.recal_on_gain_change = self._cfg_flag(parser, 'calibration', 'recal_on_gain_change')
        if self.recal_on_gain_change and not self.en_wire_recal:
            self.logger.warning("[calibration] recal_on_gain_change = 1 requires en_wire_recal = 1; ignored")
            self.recal_on_gain_change = False
        # [hw] en_bias_tee as rtl_daq applies it at start (absent = all off).
        # Existing key, only consumed by apply_bias_tee_at_init: parsed
        # quietly, rtl_daq-style (a token that is not an integer counts as 0,
        # like atoi), so the default path never logs anything new.
        hw_bias = parser.get('hw', 'en_bias_tee', fallback='').strip()
        values = [1 if self._atoi(v) else 0 for v in hw_bias.split(',') if v.strip()]
        self.hw_bias_tee = (values + [0] * self.M)[:self.M]

        # Instance identity, needed by the database before [federation] is parsed below
        if parser.has_section('federation'):
            self.instance_id = parser.getint('federation', 'instance_id', fallback=0)
            self.cluster_id = parser.get('federation', 'cluster_id', fallback='').strip() or 'default'
            self.node_id = parser.get('federation', 'node_id', fallback='').strip() or None
            self.fence_max_term_jump = self._cfg_int(parser, 'federation', 'fence_max_term_jump',
                                                     1000, 1, U32_MAX)
        if self.node_id is None:
            self.node_id = federation_common.default_node_id(self.instance_id)

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
                    db_kwargs = dict(
                        db_dir=parser.get('database', 'db_dir', fallback='_db'),
                        max_db_size_mb=parser.getint('database', 'max_db_size_mb', fallback=500),
                        rotation_max_age_hours=parser.getint('database', 'rotation_max_age_hours', fallback=168),
                        write_batch_size=parser.getint('database', 'write_batch_size', fallback=50),
                        write_flush_interval_sec=parser.getfloat('database', 'write_flush_interval_sec', fallback=1.0),
                        num_channels=self.M
                    )
                    try:
                        # Instance-aware options (B.2); older daq_db lacks them
                        self.db = DAQDatabase(
                            instance_id=self.instance_id,
                            instance_subdir=self._cfg_flag(parser, 'database', 'instance_subdir'),
                            write_meta=self._cfg_flag(parser, 'database', 'write_instance_meta'),
                            node_id=self.node_id, **db_kwargs)
                    except TypeError:
                        self.db = DAQDatabase(**db_kwargs)
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
            self.en_hwc_state_relay = self._cfg_flag(parser, 'monitoring', 'en_hwc_state_relay')
            self.event_tag_identity = self._cfg_flag(parser, 'monitoring', 'event_tag_identity')
            if en_monitoring:
                try:
                    from daq_events import (EventBus, LoggingHandler, SysLogEventHandler, ZMQPubHandler)
                    ring_size = parser.getint('monitoring', 'event_ring_size', fallback=500)
                    if self.event_tag_identity:
                        source = {"instance_id": self.instance_id, "node_id": self.node_id,
                                  "host": socket.gethostname(), "cluster_id": self.cluster_id}
                        try:
                            self.event_bus = EventBus(enabled=True, ring_size=ring_size, source=source)
                        except TypeError:
                            # daq_events without event identity support (spec G.4, R53)
                            self.event_bus = EventBus(enabled=True, ring_size=ring_size)
                    else:
                        self.event_bus = EventBus(enabled=True, ring_size=ring_size)
                    self.event_bus.register_handler(LoggingHandler())
                    if self.en_hwc_state_relay:
                        self.event_bus.register_handler(self._relay_event_handler)

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
                    if self._cfg_flag(parser, 'amplification', 'apply_bias_tee_at_init'):
                        if self.rf_frontend.en_bias_tee_runtime:
                            self.apply_bias_tee_at_init = True
                        else:
                            self.logger.warning("[amplification] apply_bias_tee_at_init = 1 requires "
                                                "en_bias_tee_runtime = 1; ignored")
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

        # The track-lock approval file (_data_control/<P>iq_track_lock) is no
        # longer opened here: _track_lock_approved() re-opens it on every check
        # and only when require_track_lock_intervention = 1 (spec B.2, L3).

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

        # [amplification] apply_bias_tee_at_init: make the hardware match the
        # configured runtime bias-tee state (and so header slot 97) at start
        if self.apply_bias_tee_at_init and self.hw_bias_tee is not None and \
                list(self.bias_tee_state) != list(self.hw_bias_tee):
            self.logger.info("Applying bias-tee state {0} at init ([hw] en_bias_tee = {1})".format(
                self.bias_tee_state, self.hw_bias_tee))
            msg_byte_array = inter_module_messages.pack_msg_set_bias_tee(self.module_identifier,
                                                                         list(self.bias_tee_state))
            if self._rtl_daq_transaction(msg_byte_array) is None:
                self.logger.error("Bias-tee state at init not confirmed by rtl_daq")

        self._publish_ctl_snapshot()
        if self.en_hwc_state_relay:
            self._write_hwc_state()
        return 0

    # ------------------------------------------------------------------
    # Control snapshot, relay writer, track lock (spec B.2, D.6.2, G.3)
    # ------------------------------------------------------------------

    def _fsm_name(self):
        return _fsm_wire_name(self.current_state)

    def _schedule_view(self):
        """Scheduler fields of the control snapshot (SCHG)."""
        view = {"gen": self.schedule_gen, "loaded": False, "active": False,
                "st": "IDLE", "idx": 0, "total": 0}
        if self.scheduler is None:
            view["gen"] = None
            return view
        view["st"] = self.scheduler.state
        sched = self.scheduler.schedule
        if sched is not None:
            view.update({"loaded": True, "active": bool(sched.active),
                         "idx": int(sched.current_index), "total": len(sched.entries)})
        return view

    def _publish_ctl_snapshot(self, frame=False):
        """Publish the per-frame control snapshot for HWSQ/SCHG (server thread
        reads it under the snapshot lock; the request queue is never touched)."""
        snap = {"fsm": self._fsm_name(),
                "pend_deferred": len(self._pending_ctr_requests),
                "ns": 1 if self.noise_source_state else 0,
                "agc": 1 if self.agc else 0,
                "nch": self.M,
                "f": int(self.rf_center_frequency),
                "fc": self._frame_count % 1000000}
        snap.update(self._schedule_view())
        self.ctl.publish(snap, frame=frame)

    def _track_lock_approved(self):
        """Re-open the track-lock file and read its first byte (b'1' = approved).
        A missing/unreadable file means "not approved" (logged once per process)."""
        try:
            return self.ctl.read_track_lock()
        except OSError as e:
            if not self._track_lock_error_logged:
                self.logger.error("Cannot read track lock approval file {:s} ({:s}); "
                                  "treating it as not approved".format(self.track_lock_ctr_fname, str(e)))
                self._track_lock_error_logged = True
            return False

    def _relay_event_handler(self, event):
        """EventBus handler (runs on the bus dispatch thread): only appends the
        event to the bounded relay deque; the main loop is the only file writer."""
        item = {"seq": 0, "timestamp": event.timestamp, "severity": event.severity,
                "module": event.module, "event_type": event.event_type,
                "payload": _jsonable(event.payload)}
        with self._relay_lock:
            item["seq"] = self._relay_seq
            self._relay_seq += 1
            self._relay_events.append(item)
            self._relay_dirty = True

    def _bias_tee_mask(self):
        mask = 0
        for m, state in enumerate(self.bias_tee_state):
            if state:
                mask |= (1 << m)
        return mask

    def _build_hwc_state(self, events, seq):
        """The hwc_state relay document (spec G.3)."""
        sched = self._schedule_view()
        if self.orientation is not None:
            orientation = {"az": float(self.orientation.cmd_az), "el": float(self.orientation.cmd_el),
                           "state": int(self.orientation.rotator_state_enum())}
        else:
            orientation = {"az": 0.0, "el": 0.0, "state": 0}
        return {"v": hwc_relay.HWC_STATE_VERSION, "epoch": self.epoch, "seq": seq,
                "ts": time.time(), "pid": os.getpid(), "fw_version": FW_VERSION,
                "fsm": self._fsm_name(),
                "pending": len(self._pending_ctr_requests) + ctr_request_queue.qsize(),
                "noise_source": 1 if self.noise_source_state else 0,
                "agc": 1 if self.agc else 0,
                "gains_idx": [int(g) for g in self.gains],
                "gains_tenths": [int(self.valid_gains[int(g)]) for g in self.gains],
                "ext_lna_gains_db": [float(g) for g in self.ext_lna_gains_db],
                "bias_tee": self._bias_tee_mask(),
                "schedule": {"state": sched["st"], "active": sched["active"], "gen": sched["gen"]},
                "orientation": orientation,
                "frame_age_ms": self._frame_interval_ms,
                "max_term_seen": self.ctl.max_term_seen,
                "cal_stalled": bool(self.cal_stalled),
                "events": events}

    def _write_hwc_state(self):
        """Main loop only: copy the relay deque under the lock, build and write
        the document outside it (unique temp file + os.replace)."""
        with self._relay_lock:
            events = list(self._relay_events)
            seq = self._relay_seq
            self._relay_dirty = False
        doc = self._build_hwc_state(events, seq)
        try:
            hwc_relay.write_hwc_state(self.hwc_state_fname, doc)
        except (OSError, TypeError, ValueError) as e:
            if not self._relay_write_failed:
                self.logger.error("Failed to write hw_controller state relay {:s}: {:s}".format(
                    self.hwc_state_fname, str(e)))
                self._relay_write_failed = True
            return
        if self._relay_write_failed:
            self.logger.info("hw_controller state relay {:s} written again".format(self.hwc_state_fname))
            self._relay_write_failed = False

    def _relay_tick(self):
        """Per frame: write the relay every 5 frames, or at once when new events arrived."""
        if not self.en_hwc_state_relay:
            return
        self._relay_frame_cntr += 1
        with self._relay_lock:
            dirty = self._relay_dirty
        if self._relay_frame_cntr >= 5 or dirty:
            self._relay_frame_cntr = 0
            self._write_hwc_state()

    def _emit_event(self, severity, event_type, payload):
        if self.event_bus is None:
            return
        from daq_events import DAQEvent
        self.event_bus.emit(DAQEvent(severity=severity, module="hw_controller",
                                     event_type=event_type, payload=payload))

    def _check_cal_stall(self):
        """[calibration] cal_stall_frames (I.8, G63): flag a calibration that
        does not converge while the noise source is on. Advisory only."""
        if self.cal_stall_frames <= 0:
            return
        if self.iq_header.noise_source_state == 1 and self.iq_header.sync_state < 5:
            self._cal_stall_cntr += 1
            if self._cal_stall_cntr >= self.cal_stall_frames and not self.cal_stalled:
                self.cal_stalled = True
                self.logger.warning("Calibration stalled: {:d} frames with the noise source on, "
                                    "sync_state {:d}".format(self._cal_stall_cntr, self.iq_header.sync_state))
                import daq_events
                self._emit_event("warning", getattr(daq_events, "EVT_CAL_STALLED", "cal_stalled"),
                                 {"frames": self._cal_stall_cntr,
                                  "sync_state": int(self.iq_header.sync_state)})
        else:
            self._cal_stall_cntr = 0
            self.cal_stalled = False

    def _perform_recal(self, reason):
        """RECL actions (G.5): bump recal_generation, write the recal request
        relay for delay_sync, emit recal_requested and re-enter STATE_INIT.
        Returns (applied, reason): (True, "ok"), or (None, "error:<msg>") when
        the relay write failed - the hw_controller side (STATE_INIT, event)
        still happens, but delay_sync never sees the request, so the outcome
        is neither 'applied' nor 'never applied'."""
        self.recal_generation += 1
        result = (True, "ok")
        try:
            hwc_relay.write_recal_request(self.recal_request_fname, self.epoch,
                                          self.recal_generation, reason)
        except (OSError, ValueError) as e:
            self.logger.error("Failed to write recalibration request {:s}: {:s}".format(
                self.recal_request_fname, str(e)))
            result = (None, "error:" + str(e))
        self.logger.info("Recalibration requested ({:s}, generation {:d}), re-entering STATE_INIT".format(
            reason, self.recal_generation))
        if self.event_bus is not None:
            import daq_events
            self._emit_event("info", getattr(daq_events, "EVT_RECAL_REQUESTED", "recal_requested"),
                             {"generation": self.recal_generation})
        self.current_state = "STATE_INIT"
        return result

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
        self._atomic_write(self._bias_tee_state_fname, "{:d}".format(self._bias_tee_mask()))

    def close(self):
        """
            Close the communication and data interfaces that are opened during the start of the module
        """
        if self.in_shmem_iface is not None:
            self.in_shmem_iface.destory_sm_buffer()

        if self.db is not None:
            self.db.close()

        if self.orientation is not None:
            self.orientation.close()

        self.logger.info("Module interfaces are closed")


    def _enable_agc(self):
        """Send the AGC enable message when AGC is on. Returns False when the
        rtl_daq transaction was not confirmed, True otherwise."""
        if self.agc:
            msg_byte_array = inter_module_messages.pack_msg_enable_agc(self.module_identifier)
            return self._rtl_daq_transaction(msg_byte_array) is not None
        return True


    def _change_gains(self):
        """
            Sends gain tuning request to the receiver module through the 
            receiver configuration FIFO
            
            Used important object parameter:
            
            :param: self.gains: Gain index array
            :type: gains: list of integers [gain index0, gain index1 , ..]

            :return: False when rtl_daq did not confirm the gain message
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
        confirmed = self._rtl_daq_transaction(msg_byte_array) is not None
        # Recompute the external-LNA link budget (advisory; does not change tuner gains)
        self._update_link_budget(gains)
        return confirmed
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

    def _handle_control_reqest(self, command, params, from_wire=False):
        """
            Handles external control requests that has arrived thorugh the control Ethernet interface

            Parameters:
            -----------
            :param: command: String identifier of the requested configuration command 
            :param: params : List of parameters of the configuration command

            :type: command : string (4 character length)
            :type: params  : python list

            Return values:
            --------------
            :return: (applied, reason): applied is True / False / None
                     (None = stored, applied when the calibration burst ends);
                     reason is one of the extended-ack reasons of spec D.6.2
                     (ok, not_confirmed, held_for_burst, parse_error, range,
                     disabled, unsupported). Exceptions are mapped to
                     error:<msg> by _drain_control_requests. Legacy
                     connections ignore the result (reply bytes unchanged).

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
                return False, "range"
            msg_byte_array = inter_module_messages.pack_msg_rf_tune(self.module_identifier, frequency)
            if self._rtl_daq_transaction(msg_byte_array) is None:
                # Not confirmed by rtl_daq: the tuner may still be on the old
                # frequency, so do not record a retune that never happened.
                self.logger.error("FREQ change to {:d} Hz not confirmed by rtl_daq".format(frequency))
                return False, "not_confirmed"
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
            return True, "ok"
        elif command == "GAIN":
            result = (True, "ok")
            try:
                if self.noise_source_state: # The noise source is turned on, we are storing only the gains
                    for m in range(self.M):
                        self.last_gains[m] = self.valid_gains.index(params[m])
                    result = (None, "held_for_burst")
                else:
                    for m in range(self.M):
                        self.gains[m] = self.valid_gains.index(params[m])
                    if not self._change_gains():
                        result = (False, "not_confirmed")
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
                return False, "range"
            if result[0] is True and self.recal_on_gain_change and from_wire:
                # [calibration] recal_on_gain_change (I.8, G64): only a GAIN
                # applied from the :5001 wire. Scheduler hops (which carry
                # per-entry gains) never recalibrate - that would re-enter
                # STATE_INIT on every hop. The GAIN itself was applied, so a
                # failed relay write does not change its ack.
                self._perform_recal("gain_change")
            return result
        elif command == "AGC ":
            if self.noise_source_state: # The noise source is turned on, we are only storing the AGC state
                self.last_agc = True
                return None, "held_for_burst"
            self.agc = True
            if not self._enable_agc():
                return False, "not_confirmed"
            return True, "ok"
        elif command == "SCHD":
            # Load schedule from JSON payload or file path
            if self.scheduler is None:
                return False, "disabled"
            try:
                from signal_scheduler import ScheduleParser
                payload = params[0] if params else ""
                if not (isinstance(payload, str) and payload.strip()):
                    self.logger.error("Failed to load schedule: empty payload")
                    return False, "parse_error"
                if payload.strip().startswith('{'):
                    text = payload
                else:
                    with open(payload.strip(), 'r') as f:
                        text = f.read()
                sched = ScheduleParser.from_json(text)
                self.scheduler.load_schedule(sched)
                self.logger.info("Schedule loaded via control command")
                if not sched.entries:  # load_schedule ignores an empty schedule
                    return False, "parse_error"
                self.schedule_gen = self._schedule_doc_gen(text)
                return True, "ok"
            except Exception as e:
                self.logger.error("Failed to load schedule: {:s}".format(str(e)))
                return False, "parse_error"
        elif command == "SCHS":
            # Stop/clear schedule
            if self.scheduler is None:
                return False, "disabled"
            self.scheduler.clear_schedule()
            self.schedule_gen = None
            return True, "ok"
        elif command == "SCHQ":
            # Query status (logged for now)
            if self.scheduler is not None:
                status = self.scheduler.get_status()
                self.logger.info("Schedule status: {:s}".format(str(status)))
            return True, "ok"
        elif command == "SCHN":
            # Skip to next entry
            if self.scheduler is None:
                return False, "disabled"
            self.scheduler.skip_to_next()
            return True, "ok"
        elif command == "EGAN":
            # Set per-channel external LNA gain (received as tenths of dB).
            # The values are stored as before even without [amplification];
            # they have no effect then, which the extended ack reports.
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
                return False, "range"
            if not self._en_amplification:
                return False, "disabled"
            return True, "ok"
        elif command == "RFQ ":
            # Query the current link budget
            self.logger.info("Link budget: total gains {0} dB, NF {1} dB, compression flags {2}".format(
                [round(g, 1) for g in self.total_system_gains_db],
                [round(n, 2) for n in self.system_nf_db], self.compression_flags))
            return True, "ok"
        elif command == "BIAS":
            # Runtime inline-LNA bias-tee toggle (requires en_bias_tee_runtime=1)
            if self.rf_frontend is not None and self.rf_frontend.en_bias_tee_runtime:
                try:
                    states = [1 if params[m] else 0 for m in range(self.M)]
                    self.bias_tee_state = states
                    msg_byte_array = inter_module_messages.pack_msg_set_bias_tee(self.module_identifier, states)
                    confirmed = self._rtl_daq_transaction(msg_byte_array) is not None
                    self.logger.info("Bias-tee state set: {0}".format(states))
                    # Relay to delay_sync for v8 header stamping
                    self._write_bias_tee_state()
                    if self.event_bus is not None:
                        from daq_events import DAQEvent, EVT_BIAS_TEE_CHANGE
                        self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                            event_type=EVT_BIAS_TEE_CHANGE, payload={"bias_tee_state": list(states)}))
                except (IndexError, TypeError):
                    self.logger.error("Improper bias-tee states: {0}".format(params))
                    return False, "range"
                if not confirmed:
                    self.logger.error("Bias-tee change not confirmed by rtl_daq")
                    return False, "not_confirmed"
                return True, "ok"
            self.logger.warning("Bias-tee runtime control disabled (set en_bias_tee_runtime=1)")
            return False, "disabled"
        elif command == "ORNT":
            # Slew the antenna to a commanded azimuth/elevation
            if self.orientation is None:
                self.logger.warning("Orientation control disabled (set en_orientation=1)")
                return False, "disabled"
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
                return False, "range"
            return True, "ok"
        elif command == "PARK":
            if self.orientation is None:
                return False, "disabled"
            self.orientation.park()
            if self.event_bus is not None:
                from daq_events import DAQEvent, EVT_ORIENTATION_PARK
                self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                    event_type=EVT_ORIENTATION_PARK, payload={}))
            return True, "ok"
        elif command == "SCAN":
            if self.orientation is None:
                return False, "disabled"
            self.orientation.start_scan()
            if self.event_bus is not None:
                from daq_events import DAQEvent, EVT_ORIENTATION_SCAN_START
                self.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                    event_type=EVT_ORIENTATION_SCAN_START, payload={}))
            return True, "ok"
        elif command == "OSTP":
            if self.orientation is None:
                return False, "disabled"
            self.orientation.stop()
            return True, "ok"
        elif command == "OQRY":
            if self.orientation is not None:
                self.logger.info("Orientation status: {0}".format(self.orientation.get_status()))
            return True, "ok"
        elif command == "INIT":
            # Re-run the hardware initialization sequence: restore configured
            # gains and disable the noise source by re-entering STATE_INIT.
            # _drain_control_requests defers this command while a calibration
            # noise burst is active, so self.gains hold the user gains here.
            self.logger.info("Re-initialization requested, re-entering STATE_INIT")
            self.current_state = "STATE_INIT"
            return True, "ok"
        elif command == "RECL":
            # Wire recalibration that also works when delay_sync is locked (G.5);
            # deferred during a noise burst exactly like INIT
            if not self.en_wire_recal:
                self.logger.warning("RECL ignored: wire recalibration disabled (set [calibration] en_wire_recal = 1)")
                return False, "disabled"
            return self._perform_recal(hwc_relay.RECAL_REASON_WIRE)
        self.logger.error("Unsupported control request: {:s}".format(command))
        return False, "unsupported"

    @staticmethod
    def _schedule_doc_gen(text):
        """Top-level u32 `gen` of a schedule JSON document, else None."""
        try:
            doc = json.loads(text)
        except ValueError:
            return None
        gen = doc.get("gen") if isinstance(doc, dict) else None
        if isinstance(gen, int) and not isinstance(gen, bool) and 0 <= gen <= U32_MAX:
            return gen
        return None


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

        Each request passes the CtrRequest state machine (PENDING -> RUNNING
        under the request lock), so a request the server thread cancelled
        after its wait timed out is never executed. Mutating requests are
        fence-checked immediately before execution (spec C.4.5): a request
        whose connection declared 0 < term < max_term_seen is refused with
        stale_term, also when it was deferred across the term change.
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
                # arbitrary later frame.
                self.logger.warning("Dropping timed-out control request: {:s}".format(
                    request.command))
                continue
            if request.command in self.QUERY_COMMANDS:
                if not request.try_start():
                    continue
                request.reply_payload = self._handle_query_request(request.command)
                request.finish((True, "ok"), self._fsm_name())
            elif allow_mutations and not self._defer_during_burst(request.command):
                if not request.try_start():
                    self.logger.warning("Dropping timed-out control request: {:s}".format(
                        request.command))
                    continue
                if self.ctl.is_stale(request.term):
                    self.logger.warning("Refusing control request {:s} from stale term {:d} "
                                        "(fence term {:d})".format(request.command, request.term,
                                                                   self.ctl.max_term_seen))
                    request.finish((False, "stale_term"), self._fsm_name())
                    continue
                self.logger.info("Control request: {:s}".format(request.command))
                try:
                    result = self._handle_control_reqest(request.command, request.params,
                                                         from_wire=True)
                except Exception as e:
                    self.logger.error("Control request {:s} failed: {:s}".format(
                        request.command, str(e)))
                    result = (False, "error:" + str(e))
                if request.command in ("SCHD", "SCHS", "SCHN"):
                    self._publish_ctl_snapshot()
                request.finish(result, self._fsm_name())
            else:
                # Not safe to touch the hardware now - retry next frame
                # (relative order of deferred mutating commands is preserved)
                self._pending_ctr_requests.append(request)

    # Commands additionally deferred while a calibration noise burst is active
    # under [calibration] defer_control_during_burst = 1 (G62)
    BURST_DEFERRABLE_COMMANDS = ("FREQ", "GAIN", "EGAN", "BIAS")

    def _defer_during_burst(self, command):
        """INIT and RECL are always deferred while a calibration noise burst is
        active: re-entering STATE_INIT with the cal-table gains loaded would
        overwrite the saved user gains and leave noise_source_state stuck
        True. FREQ/GAIN/EGAN/BIAS too with defer_control_during_burst = 1."""
        if command in ("INIT", "RECL"):
            return self.noise_source_state
        if self.defer_control_during_burst and command in self.BURST_DEFERRABLE_COMMANDS:
            return self.noise_source_state
        return False

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

            Return value: True when rtl_daq confirmed the noise-source switch,
            False when the transaction returned None (FSM callers ignore it).
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
            confirmed = self._rtl_daq_transaction(msg_byte_array) is not None
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
            confirmed = self._rtl_daq_transaction(msg_byte_array) is not None
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
        return confirmed


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

            # Frame bookkeeping for the control snapshot (HWSQ fc) and the relay
            now_mono = time.monotonic()
            if self._last_frame_mono is not None:
                self._frame_interval_ms = int((now_mono - self._last_frame_mono) * 1000)
            self._last_frame_mono = now_mono
            self._frame_count += 1

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

                # -> Calibration stall detection ([calibration] cal_stall_frames)
                self._check_cal_stall()

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
                                   if self._track_lock_approved():
                                       logging.info("User has approved track lock state initialization")
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

            # --> Control snapshot for the server-thread verbs (HWSQ/SCHG)
            self._publish_ctl_snapshot(frame=True)

            self.in_shmem_iface.send_ctr_buff_ready(active_buff_index)

            # --> hw_controller state relay for delay_sync (en_hwc_state_relay)
            self._relay_tick()
            
                        

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

        Additions (spec D.6; replies are FNSD + NUL-padded compact JSON):
          XACK = u32 term @4, char cluster_id[32] @8, u32 flags @40. Switches
                 the connection to extended acks: every mutating verb then
                 replies {"applied","reason","fsm","seq"} instead of zeros.
          HWSQ / SCHG / TLCK = no payload; answered in the server thread from
                 the control snapshot, in any state, never queued.
          TLKA = u32 value @4 (1 approve, 0 revoke the track lock).
          RECL = no payload; queued and FSM-gated like INIT.
          SCHB = u32 total_len @4, u32 crc32 @8, u16 nchunks @12;
          SCHC = u16 sess @4, u16 seq @6, u16 len @8, data @10;
          SCHE = u16 sess @4 (the reassembled JSON is loaded like SCHD).
        A connection that has not sent XACK gets today's bytes for every
        existing verb.
    """

    # How long the server waits for the main loop to execute one request
    # (default of [daq] ctl_request_timeout_sec)
    REQUEST_TIMEOUT_S = 10.0

    # ctl_max_connections > 1: how often a loop blocked on a free worker slot
    # re-checks shutdown, and the back-off after a transient accept() error
    SLOT_POLL_S = 0.5
    ACCEPT_BACKOFF_S = 0.1

    # Mutating verbs (spec D.4): refused (FNSD + zeros, not executed) on a
    # connection whose XACK was refused
    MUTATING_COMMANDS = frozenset(("FREQ", "GAIN", "AGC ", "INIT", "SCHD", "SCHS", "SCHN",
                                   "EGAN", "BIAS", "ORNT", "PARK", "SCAN", "OSTP", "STHU",
                                   "RECL", "TLKA", "SCHB", "SCHC", "SCHE"))

    SCHB_MAX_BYTES = 262144
    SCHC_MAX_CHUNK = 116
    SCHED_SESSION_IDLE_SEC = 30.0

    def __init__(self, M, port=5001, listen_address="0.0.0.0", control=None,
                 request_timeout_sec=None, handler_max_sec=12.0, max_connections=1,
                 xack_idle_sec=30.0, idle_timeout_sec=0.0):
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
            :param: control: shared ControlPlane (fence, snapshot); a default
                    one (no persistence) when None
            :param: request_timeout_sec: [daq] ctl_request_timeout_sec (T_srv)
            :param: handler_max_sec: [daq] ctl_handler_max_sec (H)
            :param: max_connections: [daq] ctl_max_connections
            :param: xack_idle_sec: [daq] ctl_xack_idle_sec (0 = off)
            :param: idle_timeout_sec: [daq] ctl_idle_timeout_sec (0 = off)
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
        self.listening = threading.Event()  # set once bind() + listen() succeeded

        self.request_timeout_s = (self.REQUEST_TIMEOUT_S if request_timeout_sec is None
                                  else float(request_timeout_sec))
        self.handler_max_s = float(handler_max_sec)
        self.max_connections = max(1, int(max_connections))
        self.xack_idle_s = float(xack_idle_sec)
        self.idle_timeout_s = float(idle_timeout_sec)
        self.control = control if control is not None else ControlPlane(
            M, request_timeout_sec=self.request_timeout_s, handler_max_sec=self.handler_max_s)

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

    @staticmethod
    def _apply_idle_timeout(connection, timeout_s):
        """Read timeout + TCP keepalive (10 s idle, 5 s interval, 3 probes)."""
        connection.settimeout(timeout_s)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                try:
                    connection.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
                except OSError:
                    pass

    def run(self):
        """
            Starts the control server service thread
        """

        self.logger.info("Opening control interface server on ip address: {:s} and port: {:d}".format(
            self.ctr_iface_addr[0], self.ctr_iface_port_no))
        try:
            self.ctr_iface_socket.bind(self.ctr_iface_addr)
            # ctl_max_connections = 1 keeps today's backlog (L16)
            self.ctr_iface_socket.listen(4 if self.max_connections == 1 else 16)
        except socket.error:
            self.logger.error("Unable to open the Hardware configuration interface")
            self.status=False
            return -1
        self.listening.set()

        if self.max_connections == 1:
            while not self._shutdown_requested:
                # Wait for a connection
                self.logger.info("Waiting for new connection")
                try:
                    connection, client_address = self.ctr_iface_socket.accept()
                except OSError:
                    break  # Listening socket closed by shutdown()
                self._serve_connection(connection)
        else:
            self._run_bounded()

        self.logger.info("Control interface server stopped")

    def _run_bounded(self):
        """ctl_max_connections = n > 1: at most n connections are served
        concurrently. The semaphore is acquired BEFORE accept(), so while all
        workers are busy further clients wait in the kernel backlog and
        nothing queues in Python. Requests still serialise through
        ctr_request_queue (FSM gating and per-connection order unchanged)."""
        slots = threading.BoundedSemaphore(self.max_connections)
        while not self._shutdown_requested:
            # Bounded wait so shutdown() ends the loop while all workers are busy
            if not slots.acquire(timeout=self.SLOT_POLL_S):
                continue
            if self._shutdown_requested:
                slots.release()
                break
            self.logger.info("Waiting for new connection")
            try:
                connection, client_address = self.ctr_iface_socket.accept()
            except OSError as e:
                slots.release()
                if self._shutdown_requested:
                    break  # Listening socket closed by shutdown()
                # Transient (ECONNABORTED, EMFILE, ...): keep serving
                self.logger.error("Control interface accept failed: {:s}".format(str(e)))
                time.sleep(self.ACCEPT_BACKOFF_S)
                continue
            worker = threading.Thread(target=self._serve_and_release,
                                      args=(connection, slots), daemon=True,
                                      name="CtrIfaceConn")
            try:
                worker.start()
            except RuntimeError as e:
                # Thread limit reached: drop this client, keep the slot count right
                self.logger.error("Unable to start control connection worker: {:s}".format(str(e)))
                connection.close()
                slots.release()

    def _serve_and_release(self, connection, slots):
        try:
            self._serve_connection(connection)
        finally:
            slots.release()

    def _serve_connection(self, connection):
        """Serve one client until it disconnects, sends EXIT, or idles out."""
        conn = ConnState()
        try:
            self.logger.info("Conenction established ")
            if self.idle_timeout_s > 0:
                self._apply_idle_timeout(connection, self.idle_timeout_s)

            # Main server loop
            while True:
                try:
                    ctr_frame = self._recv_exact(connection, 128)
                except socket.timeout:
                    self.logger.info("Closing idle control connection")
                    break
                if ctr_frame is None:
                    self.logger.info("No more data from client, closing connection")
                    break
                exit_flag, reply = self.process_ctr_frame(ctr_frame, conn)
                if exit_flag:
                    break
                connection.sendall(reply)
                if conn.sent_xack and not conn.idle_applied and self.xack_idle_s > 0:
                    self._apply_idle_timeout(connection, self.xack_idle_s)
                    conn.idle_applied = True
        except Exception as e:
            # A misbehaving client must never take the control plane down
            self.logger.error("Control connection error: {:s}".format(str(e)))
        finally:
            # Clean up the connection
            conn.sched_session = None
            connection.close()

    # ------------------------------------------------------------------
    # Reply assembly
    # ------------------------------------------------------------------

    @staticmethod
    def _build_reply(payload):
        """Assemble the fixed 128-byte reply frame: 'FNSD' + 124 payload bytes
        (NUL-padded; all zeros when there is no payload)."""
        if payload:
            payload = payload[:124]
            return b"FNSD" + payload + bytes(124 - len(payload))
        return b"FNSD" + bytes(124)

    @staticmethod
    def _json_payload(doc):
        return json.dumps(doc, separators=(',', ':')).encode('utf-8')

    def _json_reply(self, doc):
        return self._build_reply(self._json_payload(doc))

    def _ack_payload(self, applied, reason, extra=None, fsm=None):
        """Extended ack {"applied","reason",<extra>,"fsm","seq"} (<= 124 bytes:
        an error:<msg> reason is truncated so the JSON always fits)."""
        if fsm is None:
            fsm = self.control.fsm()
        seq = self.control.next_seq()

        def _encode(text):
            doc = {"applied": applied, "reason": text}
            if extra:
                doc.update(extra)
            doc["fsm"] = fsm
            doc["seq"] = seq
            return self._json_payload(doc)

        payload = _encode(reason)
        if len(payload) <= 124:
            return payload
        # Longest prefix of the reason (never shorter than the 'error:' tag)
        # whose JSON encoding fits. JSON escaping is per character, so the
        # encoded length is monotonic in the prefix length: binary search.
        lo, hi = min(len("error:"), len(reason)), len(reason)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(_encode(reason[:mid])) <= 124:
                lo = mid
            else:
                hi = mid - 1
        return _encode(reason[:lo])

    def _ack_reply(self, applied, reason, extra=None, fsm=None):
        return self._build_reply(self._ack_payload(applied, reason, extra, fsm))

    # ------------------------------------------------------------------
    # Queued requests (main loop)
    # ------------------------------------------------------------------

    def _submit_request(self, command, params, conn=None, extra=None, always_json=False):
        """Enqueue one request for the HWC main loop and wait for completion.
        Returns the 128-byte reply frame.

        Legacy connections (no XACK) get today's bytes and timing. On an
        extended connection (or with always_json, used by SCHE) a mutating
        verb gets the extended ack with the true outcome; a request that was
        still PENDING when ctl_request_timeout_sec expired is cancelled and
        reported deferred_timeout (it provably never runs), and one that was
        already RUNNING is waited for up to ctl_handler_max_sec more."""
        if conn is None:
            conn = ConnState()
        extended = conn.xack or always_json
        is_query = command in HWC.QUERY_COMMANDS
        request = CtrRequest(command, params, term=conn.term, extended=conn.xack)
        try:
            ctr_request_queue.put(request, timeout=2.0)
        except queue.Full:
            self.logger.error("Control request queue full, rejecting {:s}".format(command))
            if is_query:
                return self._build_reply(b'{"error":"busy"}')
            if extended:
                return self._ack_reply(False, "queue_full", extra)
            return self._build_reply(None)
        if not request.done.wait(timeout=self.request_timeout_s):
            self.logger.warning("Control request {:s} timed out waiting for the main loop".format(command))
            # Withdraw the request so the main loop never executes it at an
            # arbitrary later frame. Fails only when the main loop already
            # started it (RUNNING): then the request completes on its own.
            cancelled = request.try_cancel()
            if is_query:
                return self._build_reply(b'{"error":"busy"}')
            if not extended:
                # Frozen legacy contract: the standard 'FNSD' ack
                return self._build_reply(None)
            if cancelled:
                return self._ack_reply(False, "deferred_timeout", extra)
            if not request.done.wait(timeout=self.handler_max_s):
                self.logger.error("Control request {:s} still running {:.1f} s after the request "
                                  "timeout; outcome unknown".format(command, self.handler_max_s))
                return self._ack_reply(None, "handler_timeout", extra)
        if is_query or not extended:
            return self._build_reply(request.reply_payload)
        applied, reason = request.result
        return self._ack_reply(applied, reason, extra, fsm=request.fsm)

    # ------------------------------------------------------------------
    # Server-thread verbs (spec D.6.2)
    # ------------------------------------------------------------------

    def _handle_xack(self, msg_bytes, conn):
        term = unpack('<I', msg_bytes[4:8])[0]
        cluster_id = msg_bytes[8:40].split(b'\x00', 1)[0].decode('utf-8', errors='replace')
        reply = self.control.handle_xack(term, cluster_id, conn)
        self.logger.info("XACK term={:d} cluster_id='{:s}' -> {:s}".format(
            term, cluster_id, "extended" if reply.get("xack") else reply.get("error", "")))
        return self._json_reply(reply)

    def _handle_tlka(self, msg_bytes, conn):
        value = unpack('<I', msg_bytes[4:8])[0]
        if self.control.is_stale(conn.term):
            applied, reason = False, "stale_term"
        elif value not in (0, 1):
            applied, reason = False, "range"
        else:
            try:
                self.control.write_track_lock(value == 1)
                applied, reason = True, "ok"
                self.logger.info("Track lock approval set to {:d} via TLKA".format(value))
            except OSError as e:
                self.logger.error("TLKA failed to write {:s}: {:s}".format(
                    str(self.control.track_lock_path), str(e)))
                applied, reason = False, "error:" + str(e)
        if conn.xack:
            return self._ack_reply(applied, reason, {"appr": value} if applied else None)
        if applied:
            return self._json_reply({"applied": True, "appr": value})
        doc = {"applied": False, "reason": reason}
        payload = self._json_payload(doc)
        while len(payload) > 124:
            doc["reason"] = doc["reason"][:-1]
            payload = self._json_payload(doc)
        return self._build_reply(payload)

    def _handle_tlck(self):
        try:
            approved = 1 if self.control.read_track_lock() else 0
        except OSError:
            approved = 0
        return self._json_reply({"req": 1 if self.control.require_track_lock else 0,
                                 "appr": approved})

    def _handle_schb(self, msg_bytes, conn):
        total_len, crc, nchunks = unpack('<IIH', msg_bytes[4:14])
        if total_len > self.SCHB_MAX_BYTES:
            conn.sched_session = None
            return self._json_reply({"ok": False, "reason": "too_large"})
        if total_len == 0 or nchunks == 0 or nchunks > total_len or \
                nchunks * self.SCHC_MAX_CHUNK < total_len:
            conn.sched_session = None
            return self._json_reply({"ok": False, "reason": "bad_request"})
        sess = self.control.next_session_id()
        conn.sched_session = SchedSession(sess, total_len, crc, nchunks)
        self.logger.info("Chunked schedule session {:d}: {:d} bytes in {:d} chunks".format(
            sess, total_len, nchunks))
        return self._json_reply({"ok": True, "sess": sess})

    def _live_session(self, conn, sess):
        """The connection's session when it matches `sess` and has not expired."""
        session = conn.sched_session
        if session is None or session.sess != sess:
            return None
        if time.monotonic() - session.last_activity > self.SCHED_SESSION_IDLE_SEC:
            self.logger.warning("Chunked schedule session {:d} expired".format(sess))
            conn.sched_session = None
            return None
        return session

    def _handle_schc(self, msg_bytes, conn):
        sess, seq, length = unpack('<HHH', msg_bytes[4:10])
        session = self._live_session(conn, sess)
        if session is None:
            return self._json_reply({"ok": False, "reason": "bad_session", "seq": seq})
        if seq != session.next_seq:
            conn.sched_session = None
            return self._json_reply({"ok": False, "reason": "out_of_order", "seq": seq})
        if not 1 <= length <= self.SCHC_MAX_CHUNK or \
                len(session.buf) + length > session.total_len or seq >= session.nchunks:
            conn.sched_session = None
            return self._json_reply({"ok": False, "reason": "overflow", "seq": seq})
        session.buf.extend(msg_bytes[10:10 + length])
        session.next_seq += 1
        session.last_activity = time.monotonic()
        return self._json_reply({"ok": True, "seq": seq})

    def _handle_sche(self, msg_bytes, conn):
        sess = unpack('<H', msg_bytes[4:6])[0]
        session = self._live_session(conn, sess)
        conn.sched_session = None
        if session is None:
            return self._ack_reply(False, "bad_session", {"entries": 0})
        data = bytes(session.buf)
        if len(data) != session.total_len or session.next_seq != session.nchunks:
            return self._ack_reply(False, "incomplete", {"entries": 0})
        if (zlib.crc32(data) & U32_MAX) != session.crc32:
            return self._ack_reply(False, "crc_mismatch", {"entries": 0})
        try:
            text = data.decode('utf-8')
            doc = json.loads(text)
            if not isinstance(doc, dict):
                raise ValueError("schedule document is not a JSON object")
        except ValueError as e:
            self.logger.error("Chunked schedule session {:d} is not valid JSON: {:s}".format(sess, str(e)))
            return self._ack_reply(False, "parse_error", {"entries": 0})
        entries = doc.get("entries")
        n_entries = min(99999, len(entries)) if isinstance(entries, list) else 0
        self.logger.info("Chunked schedule session {:d} complete ({:d} entries)".format(sess, n_entries))
        return self._submit_request("SCHD", [text], conn, extra={"entries": n_entries},
                                    always_json=True)

    def process_ctr_frame(self, msg_bytes, conn=None):
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
            :param: conn: per-connection ConnState (a fresh legacy state when None)

            Return values:
            --------------
            :return: (exit_flag, reply): exit_flag True closes the connection
                     (no reply is sent); otherwise reply is the 128-byte frame
                     to send back.
        """
        if conn is None:
            conn = ConnState()
        command = msg_bytes[0:4].decode('ascii', errors='replace')
        self.logger.warning("Got command %s", command)
        try:
            if command == "EXIT":
                return True, None

            if conn.cluster_bad and command in self.MUTATING_COMMANDS:
                self.logger.warning("Refusing {:s} on a connection whose XACK was refused".format(command))
                return False, self._build_reply(None)

            if command == "STHU":
                # Accepted for wire compatibility; no threshold consumer exists
                # in the current chain (there is no matching rtl_daq command).
                threshold = unpack('f',msg_bytes[4:8])[0]
                self.logger.warning("STHU threshold {:f} accepted but not implemented".format(threshold))
                if conn.xack:
                    return False, self._ack_reply(False, "unsupported")
                return False, self._build_reply(None)

            elif command == "FREQ":
                frequency = unpack('Q',msg_bytes[4:12])[0]
                self.logger.info("Received frequency value: {:f} Hz".format(frequency))
                return False, self._submit_request(command, [frequency], conn)

            elif command == "GAIN":
                gains = unpack('I'*self.M, msg_bytes[4:4+4*self.M])
                for m in range(self.M):
                    self.logger.info("Received gain values - CH{:d}: {:d} dB x 10".format(m, gains[m]))
                return False, self._submit_request(command, list(gains), conn)

            elif command == "AGC ":
                self.logger.info("Received AGC request")
                return False, self._submit_request(command, [], conn)

            elif command == "INIT":
                self.logger.info("Inititalization command received")
                return False, self._submit_request(command, [], conn)

            elif command == "SCHD":
                # Schedule load: payload is file path or compact JSON
                payload = msg_bytes[4:128].decode('utf-8', errors='ignore').rstrip('\x00').strip()
                self.logger.info("Received schedule load command")
                return False, self._submit_request(command, [payload], conn)

            elif command == "SCHS":
                self.logger.info("Received schedule stop command")
                return False, self._submit_request(command, [], conn)

            elif command == "SCHQ":
                self.logger.info("Received schedule query command")
                return False, self._submit_request(command, [], conn)

            elif command == "SCHN":
                self.logger.info("Received schedule next command")
                return False, self._submit_request(command, [], conn)

            elif command == "EGAN":
                gains = unpack('I'*self.M, msg_bytes[4:4+4*self.M])
                for m in range(self.M):
                    self.logger.info("Received external LNA gain - CH{:d}: {:d} dB x 10".format(m, gains[m]))
                return False, self._submit_request(command, list(gains), conn)

            elif command == "RFQ ":
                self.logger.info("Received link-budget query command")
                return False, self._submit_request(command, [], conn)

            elif command == "BIAS":
                states = unpack('I'*self.M, msg_bytes[4:4+4*self.M])
                for m in range(self.M):
                    self.logger.info("Received bias-tee state - CH{:d}: {:d}".format(m, states[m]))
                return False, self._submit_request(command, list(states), conn)

            elif command == "ORNT":
                az, el = unpack('ff', msg_bytes[4:12])
                self.logger.info("Received orientation bearing: az={:.2f} el={:.2f}".format(az, el))
                return False, self._submit_request(command, [az, el], conn)

            elif command in ("PARK", "SCAN", "OSTP", "OQRY"):
                self.logger.info("Received orientation command: {:s}".format(command))
                return False, self._submit_request(command, [], conn)

            elif command == "XACK":
                return False, self._handle_xack(msg_bytes, conn)

            elif command == "HWSQ":
                return False, self._json_reply(self.control.hwsq(ctr_request_queue.qsize()))

            elif command == "SCHG":
                return False, self._json_reply(self.control.schg())

            elif command == "TLCK":
                return False, self._handle_tlck()

            elif command == "TLKA":
                return False, self._handle_tlka(msg_bytes, conn)

            elif command == "RECL":
                self.logger.info("Received wire recalibration request")
                return False, self._submit_request(command, [], conn)

            elif command == "SCHB":
                return False, self._handle_schb(msg_bytes, conn)

            elif command == "SCHC":
                return False, self._handle_schc(msg_bytes, conn)

            elif command == "SCHE":
                return False, self._handle_sche(msg_bytes, conn)

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
