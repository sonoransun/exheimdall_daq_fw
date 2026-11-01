"""
    Structured Event System for DAQ Pipeline Monitoring

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Provides a non-blocking EventBus with pluggable handlers (logging,
    syslog, ZMQ PUB). When disabled, all methods are zero-cost no-ops.
"""
import time
import json
import logging
import logging.handlers
import threading
import queue
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------
EVT_PROCESS_START = "process_start"
EVT_PROCESS_STOP = "process_stop"
EVT_SYNC_LOCK = "sync_lock"
EVT_SYNC_LOST = "sync_lost"
EVT_FREQ_CHANGE = "freq_change"
EVT_GAIN_CHANGE = "gain_change"
EVT_OVERDRIVE = "overdrive"
EVT_CAL_START = "cal_start"
EVT_CAL_SAMPLE_DONE = "cal_sample_done"
EVT_CAL_IQ_DONE = "cal_iq_done"
EVT_CAL_TIMEOUT = "cal_timeout"
EVT_NOISE_SOURCE_ON = "noise_source_on"
EVT_NOISE_SOURCE_OFF = "noise_source_off"
EVT_SCHEDULE_LOADED = "schedule_loaded"
EVT_SCHEDULE_TRANSITION = "schedule_transition"
EVT_SCHEDULE_COMPLETE = "schedule_complete"
EVT_DB_ERROR = "db_error"
EVT_DB_QUEUE_FULL = "db_queue_full"
EVT_FRAME_DROP = "frame_drop"
EVT_HEARTBEAT = "heartbeat"
EVT_PEER_UP = "peer_up"
EVT_PEER_DOWN = "peer_down"
EVT_PEER_DEGRADED = "peer_degraded"
EVT_COORDINATOR_ELECTED = "coordinator_elected"
EVT_EVENT_QUEUE_FULL = "event_queue_full"
# Amplified-receiver / RF front-end events
EVT_EXT_GAIN_CHANGE = "ext_gain_change"
EVT_COMPRESSION = "compression"
EVT_BIAS_TEE_CHANGE = "bias_tee_change"
# Antenna orientation events
EVT_ORIENTATION_SLEW = "orientation_slew"
EVT_ORIENTATION_SETTLED = "orientation_settled"
EVT_ORIENTATION_SCAN_START = "orientation_scan_start"
EVT_ORIENTATION_SCAN_PEAK = "orientation_scan_peak"
EVT_ORIENTATION_PARK = "orientation_park"
EVT_ORIENTATION_LIMIT = "orientation_limit"
# Cluster / calibration-robustness events (design spec G.7b). Emitters in
# other modules use getattr(daq_events, "EVT_...", "<literal>") so they keep
# working against an older daq_events.
EVT_PEER_RECOVERED = "peer_recovered"
EVT_PEER_FLAPPING = "peer_flapping"
EVT_RECAL_REQUESTED = "recal_requested"
EVT_CAL_STALLED = "cal_stalled"
EVT_COORDINATOR_COMMAND = "coordinator_command"
EVT_NODE_KEY_COLLISION = "node_key_collision"
EVT_SCHEDULE_RECONCILE_FAILED = "schedule_reconcile_failed"

# Severity string → logging level
_SEVERITY_MAP = {
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


@dataclass
class DAQEvent:
    """A single structured event emitted by the DAQ pipeline.

    ``source`` is the emitter identity (``{"instance_id", "node_id", "host",
    "cluster_id"}``) set by an EventBus built with ``source=`` ([monitoring]
    event_tag_identity = 1). It is omitted from ``to_dict()``/``to_json()``
    when None, so untagged events serialise exactly as before the field
    existed.
    """
    timestamp: float = 0.0
    severity: str = "info"
    module: str = ""
    event_type: str = ""
    payload: dict = field(default_factory=dict)
    source: dict = None

    def to_dict(self):
        d = asdict(self)
        if d["source"] is None:
            del d["source"]
        return d

    def to_json(self):
        return json.dumps(self.to_dict())


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

class LoggingHandler:
    """Forwards events to Python logging (always registered)."""

    def __init__(self):
        self._logger = logging.getLogger("heimdall.events")

    def __call__(self, event):
        level = _SEVERITY_MAP.get(event.severity, logging.INFO)
        payload_summary = ", ".join(f"{k}={v}" for k, v in event.payload.items())
        msg = f"[{event.event_type}] {payload_summary}" if payload_summary else f"[{event.event_type}]"
        self._logger.log(level, msg)


class SysLogEventHandler:
    """Forwards events to syslog via Python logging.handlers.SysLogHandler."""

    # Facility name → numeric constant
    _FACILITY_MAP = {
        "daemon": logging.handlers.SysLogHandler.LOG_DAEMON,
        "local0": logging.handlers.SysLogHandler.LOG_LOCAL0,
        "local1": logging.handlers.SysLogHandler.LOG_LOCAL1,
        "local2": logging.handlers.SysLogHandler.LOG_LOCAL2,
        "local3": logging.handlers.SysLogHandler.LOG_LOCAL3,
        "local4": logging.handlers.SysLogHandler.LOG_LOCAL4,
        "local5": logging.handlers.SysLogHandler.LOG_LOCAL5,
        "local6": logging.handlers.SysLogHandler.LOG_LOCAL6,
        "local7": logging.handlers.SysLogHandler.LOG_LOCAL7,
    }

    def __init__(self, address="/dev/log", facility="daemon", min_severity="warning"):
        self._min_level = _SEVERITY_MAP.get(min_severity, logging.WARNING)
        fac = self._FACILITY_MAP.get(facility, logging.handlers.SysLogHandler.LOG_DAEMON)
        self._syslog_logger = logging.getLogger("heimdall.syslog")
        self._syslog_logger.propagate = False
        # Remove existing handlers to avoid duplicates on re-init
        self._syslog_logger.handlers.clear()
        try:
            handler = logging.handlers.SysLogHandler(address=address, facility=fac)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._syslog_logger.addHandler(handler)
            self._available = True
        except Exception:
            self._available = False

    def __call__(self, event):
        if not self._available:
            return
        level = _SEVERITY_MAP.get(event.severity, logging.INFO)
        if level < self._min_level:
            return
        payload_json = json.dumps(event.payload) if event.payload else "{}"
        msg = f"heimdall.{event.module}.{event.event_type}: {payload_json}"
        self._syslog_logger.log(level, msg)


class ZMQPubHandler:
    """Publishes events on a ZMQ PUB socket for external subscribers.

    The multipart topic is ``topic_prefix + event_type``; the prefix is empty
    by default ([monitoring] event_topic_prefix = 1 makes delay_sync pass
    ``"inst<N>."``).
    """

    def __init__(self, port=5003, listen_address="*", topic_prefix=""):
        self._topic_prefix = topic_prefix or ""
        self._socket = None
        try:
            import zmq
            ctx = zmq.Context.instance()
            self._socket = ctx.socket(zmq.PUB)
            self._socket.bind(f"tcp://{listen_address}:{port}")
        except Exception:
            logging.getLogger("heimdall.events").warning(
                "ZMQ PUB handler unavailable", exc_info=True)
            self._socket = None

    def __call__(self, event):
        if self._socket is None:
            return
        try:
            topic = (self._topic_prefix + event.event_type).encode()
            data = event.to_json().encode()
            self._socket.send_multipart([topic, data], flags=1)  # NOBLOCK
        except Exception:
            pass

    def close(self):
        if self._socket is not None:
            try:
                self._socket.close(linger=0)
            except Exception:
                pass
            self._socket = None


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """Non-blocking event dispatcher with background delivery thread.

    When ``enabled=False`` all methods are zero-cost no-ops.

    ``source`` (a dict, default None) is attached to every emitted event
    that does not already carry one ([monitoring] event_tag_identity = 1).
    """

    def __init__(self, enabled=False, ring_size=500, queue_size=2000, source=None):
        self._enabled = enabled
        self._source = dict(source) if source is not None else None
        self._handlers = []
        self._ring = []
        self._ring_size = ring_size
        self._ring_idx = 0
        self.dropped_events = 0
        self._last_drop_notify = 0.0
        if not enabled:
            return
        self._queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._dispatch_loop, daemon=True,
                                        name="EventBus")
        self._thread.start()

    def register_handler(self, handler):
        """Add a callable handler that receives DAQEvent instances."""
        if self._enabled:
            self._handlers.append(handler)

    def emit(self, event):
        """Enqueue an event for background delivery. Non-blocking."""
        if not self._enabled:
            return
        if event.timestamp == 0.0:
            event.timestamp = time.time()
        if self._source is not None and event.source is None:
            event.source = self._source
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped_events += 1
            now = time.monotonic()
            if now - self._last_drop_notify >= 1.0:
                self._last_drop_notify = now
                drop_event = DAQEvent(
                    severity="warning",
                    module="event_bus",
                    event_type=EVT_EVENT_QUEUE_FULL,
                    payload={"dropped_total": self.dropped_events},
                )
                drop_event.timestamp = time.time()
                if self._source is not None:
                    drop_event.source = self._source
                self._store_ring(drop_event)
                for handler in self._handlers:
                    try:
                        handler(drop_event)
                    except Exception:
                        pass

    def get_recent_events(self, n=100):
        """Return up to *n* most recent events from the ring buffer."""
        if not self._enabled:
            return []
        total = min(n, len(self._ring))
        if total == 0:
            return []
        # Ring may not be full yet
        if len(self._ring) < self._ring_size:
            return list(self._ring[-total:])
        # Full ring: read backwards from write pointer
        result = []
        for i in range(total):
            idx = (self._ring_idx - 1 - i) % self._ring_size
            result.append(self._ring[idx])
        result.reverse()
        return result

    def close(self):
        """Signal the dispatch thread to drain and exit."""
        if not self._enabled:
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        # Close any closeable handlers
        for h in self._handlers:
            if hasattr(h, "close"):
                h.close()

    # ---- internal ----

    def _dispatch_loop(self):
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._store_ring(event)
            for handler in self._handlers:
                try:
                    handler(event)
                except Exception:
                    pass
        # Drain remaining
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                self._store_ring(event)
                for handler in self._handlers:
                    try:
                        handler(event)
                    except Exception:
                        pass
            except queue.Empty:
                break

    def _store_ring(self, event):
        if len(self._ring) < self._ring_size:
            self._ring.append(event)
        else:
            self._ring[self._ring_idx % self._ring_size] = event
        self._ring_idx += 1
