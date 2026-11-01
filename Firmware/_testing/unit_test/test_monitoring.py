"""
    Unit tests for the DAQ Monitoring modules

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import unittest
import sys
import json
import time
import socket
import threading
from os.path import join, dirname, realpath
from unittest.mock import MagicMock, patch

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

from daq_metrics import MetricsCollector
from daq_events import (
    DAQEvent, EventBus, LoggingHandler, SysLogEventHandler, ZMQPubHandler,
    EVT_PROCESS_START, EVT_SYNC_LOCK, EVT_SYNC_LOST, EVT_HEARTBEAT, EVT_FRAME_DROP,
)
from daq_status_server import StatusServer



def _free_port():
    """Grab an ephemeral TCP port (the servers under test cannot report a
    kernel-assigned port back, so reserve one and hand it over)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port, timeout=5.0):
    """Deterministically wait until the server accepts connections
    (replaces fixed post-start sleeps)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.02)
    return False


# ======================================================================
# MetricsCollector tests
# ======================================================================

class TestMetricsCollector(unittest.TestCase):

    def test_record_and_stats_roundtrip(self):
        """Record values and retrieve stats."""
        mc = MetricsCollector(window_size=100)
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            mc.record("latency", v)
        s = mc.get_stats("latency")
        self.assertEqual(s["count"], 5)
        self.assertAlmostEqual(s["min"], 1.0)
        self.assertAlmostEqual(s["max"], 5.0)
        self.assertAlmostEqual(s["avg"], 3.0)
        self.assertAlmostEqual(s["last"], 5.0)

    def test_circular_overflow(self):
        """Buffer wraps correctly when window is exceeded."""
        mc = MetricsCollector(window_size=10)
        for i in range(25):
            mc.record("x", float(i))
        s = mc.get_stats("x")
        self.assertEqual(s["count"], 10)
        # Last 10 values: 15..24
        self.assertAlmostEqual(s["min"], 15.0)
        self.assertAlmostEqual(s["max"], 24.0)
        self.assertAlmostEqual(s["last"], 24.0)

    def test_empty_stats(self):
        """Stats for unknown metric returns zeros."""
        mc = MetricsCollector()
        s = mc.get_stats("nonexistent")
        self.assertEqual(s["count"], 0)
        self.assertAlmostEqual(s["min"], 0.0)

    def test_reset(self):
        """Reset clears a metric."""
        mc = MetricsCollector(window_size=100)
        mc.record("test", 42.0)
        mc.reset("test")
        s = mc.get_stats("test")
        self.assertEqual(s["count"], 0)

    def test_p95_accuracy(self):
        """P95 on a uniform distribution is approximately correct."""
        mc = MetricsCollector(window_size=1000)
        for i in range(1000):
            mc.record("uniform", float(i))
        s = mc.get_stats("uniform")
        # P95 of 0..999 ≈ 949.05
        self.assertGreater(s["p95"], 940.0)
        self.assertLess(s["p95"], 960.0)

    def test_multiple_metrics(self):
        """Multiple named metrics are independent."""
        mc = MetricsCollector(window_size=100)
        mc.record("a", 10.0)
        mc.record("b", 20.0)
        all_stats = mc.get_all_stats()
        self.assertIn("a", all_stats)
        self.assertIn("b", all_stats)
        self.assertAlmostEqual(all_stats["a"]["last"], 10.0)
        self.assertAlmostEqual(all_stats["b"]["last"], 20.0)


# ======================================================================
# DAQEvent tests
# ======================================================================

class TestDAQEvent(unittest.TestCase):

    def test_creation(self):
        """DAQEvent dataclass creation."""
        evt = DAQEvent(
            timestamp=1000.0,
            severity="warning",
            module="delay_sync",
            event_type=EVT_SYNC_LOCK,
            payload={"sync_state": 6},
        )
        self.assertEqual(evt.severity, "warning")
        self.assertEqual(evt.event_type, EVT_SYNC_LOCK)
        self.assertEqual(evt.payload["sync_state"], 6)

    def test_json_serialization(self):
        """DAQEvent serializes to valid JSON."""
        evt = DAQEvent(
            timestamp=2000.0,
            severity="info",
            module="hw_controller",
            event_type=EVT_PROCESS_START,
            payload={"pid": 1234},
        )
        j = evt.to_json()
        d = json.loads(j)
        self.assertEqual(d["event_type"], "process_start")
        self.assertEqual(d["payload"]["pid"], 1234)


# ======================================================================
# EventBus tests
# ======================================================================

class TestEventBus(unittest.TestCase):

    def test_handler_receives_event(self):
        """A registered handler receives emitted events."""
        bus = EventBus(enabled=True, ring_size=50)
        received = []
        bus.register_handler(lambda e: received.append(e))
        bus.emit(DAQEvent(severity="info", module="test",
                          event_type=EVT_HEARTBEAT, payload={"n": 1}))
        bus.close()  # close() joins the dispatch thread and drains the queue
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].event_type, EVT_HEARTBEAT)

    def test_multiple_handlers(self):
        """Multiple handlers each receive the event."""
        bus = EventBus(enabled=True, ring_size=50)
        r1, r2 = [], []
        bus.register_handler(lambda e: r1.append(e))
        bus.register_handler(lambda e: r2.append(e))
        bus.emit(DAQEvent(severity="info", module="test",
                          event_type=EVT_HEARTBEAT))
        bus.close()  # deterministic drain
        self.assertEqual(len(r1), 1)
        self.assertEqual(len(r2), 1)

    def test_queue_overflow_graceful(self):
        """Overflow drops events without raising."""
        bus = EventBus(enabled=True, ring_size=10, queue_size=5)
        # Block the dispatch thread so the queue fills
        block = threading.Event()
        bus.register_handler(lambda e: block.wait(timeout=2))
        for i in range(20):
            bus.emit(DAQEvent(severity="info", module="test",
                              event_type=EVT_HEARTBEAT, payload={"i": i}))
        block.set()
        bus.close()  # deterministic drain
        # Some events were dropped — just verify no exception and some were processed

    def test_disabled_noop(self):
        """Disabled bus is a complete no-op."""
        bus = EventBus(enabled=False)
        bus.register_handler(lambda e: None)
        bus.emit(DAQEvent(severity="info", module="test",
                          event_type=EVT_HEARTBEAT))
        events = bus.get_recent_events()
        self.assertEqual(len(events), 0)
        bus.close()  # Should not raise

    def test_recent_events_ring(self):
        """Recent events ring buffer works correctly."""
        bus = EventBus(enabled=True, ring_size=5)
        received = []
        bus.register_handler(lambda e: received.append(e))
        for i in range(10):
            bus.emit(DAQEvent(severity="info", module="test",
                              event_type=EVT_HEARTBEAT, payload={"i": i}))
        bus.close()  # deterministic drain
        recent = bus.get_recent_events(5)
        # Should have at most 5 events, the most recent ones
        self.assertLessEqual(len(recent), 5)
        if len(recent) == 5:
            # Verify they are the last 5 emitted
            indices = [e.payload["i"] for e in recent]
            self.assertEqual(indices, [5, 6, 7, 8, 9])


# ======================================================================
# SysLogEventHandler tests (mock-based)
# ======================================================================

class TestSysLogEventHandler(unittest.TestCase):

    @patch("logging.handlers.SysLogHandler")
    def test_severity_filtering(self, mock_syslog_cls):
        """Events below min_severity are dropped."""
        # Make the handler init succeed
        mock_syslog_cls.LOG_DAEMON = 3
        handler = SysLogEventHandler(address="/dev/log", facility="daemon",
                                     min_severity="warning")
        handler._available = True
        handler._syslog_logger = MagicMock()

        # Info event should be filtered out
        evt_info = DAQEvent(severity="info", module="test",
                            event_type=EVT_HEARTBEAT, payload={})
        handler(evt_info)
        handler._syslog_logger.log.assert_not_called()

        # Warning event should pass
        evt_warn = DAQEvent(severity="warning", module="test",
                            event_type=EVT_SYNC_LOST, payload={"reason": "test"})
        handler(evt_warn)
        handler._syslog_logger.log.assert_called_once()

    @patch("logging.handlers.SysLogHandler")
    def test_message_format(self, mock_syslog_cls):
        """Syslog message has expected format."""
        mock_syslog_cls.LOG_DAEMON = 3
        handler = SysLogEventHandler(address="/dev/log", facility="daemon",
                                     min_severity="info")
        handler._available = True
        handler._syslog_logger = MagicMock()

        evt = DAQEvent(severity="error", module="delay_sync",
                       event_type=EVT_FRAME_DROP, payload={"count": 5})
        handler(evt)
        args = handler._syslog_logger.log.call_args
        msg = args[0][1]
        self.assertIn("heimdall.delay_sync.frame_drop:", msg)
        self.assertIn('"count": 5', msg)


# ======================================================================
# StatusServer tests
# ======================================================================

class TestStatusServer(unittest.TestCase):

    def setUp(self):
        self.metrics = MetricsCollector(window_size=100)
        self.bus = EventBus(enabled=True, ring_size=50)
        # Use a random high port to avoid conflicts
        self.port = _free_port()
        self.server = StatusServer(port=self.port, metrics=self.metrics,
                                   event_bus=self.bus)
        self.server.start()
        assert _wait_listening(self.port), "StatusServer did not come up"

    def tearDown(self):
        self.server.close()
        self.bus.close()

    def _query(self, cmd):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("127.0.0.1", self.port))
        s.sendall((cmd + "\n").encode())
        data = b""
        # Replies are one JSON object terminated by '\n' — stop there
        # instead of waiting out the 2 s socket timeout on every query
        while b"\n" not in data:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                break
        s.close()
        return json.loads(data.decode())

    def test_ping(self):
        """PING returns ok and timestamp."""
        resp = self._query("PING")
        self.assertTrue(resp["ok"])
        self.assertIn("ts", resp)

    def test_status_structure(self):
        """STATUS returns expected keys."""
        self.server.update_status({
            "sync_state": 6,
            "frame_count": 100,
            "counters": {"dropped_frames_iq": 0, "dropped_frames_hwc": 0},
        })
        self.metrics.record("frame_processing_latency_ms", 2.5)
        self.metrics.record("frame_throughput_fps", 5.0)
        resp = self._query("STATUS")
        self.assertIn("pipeline_health", resp)
        self.assertIn("uptime_sec", resp)
        self.assertIn("latency", resp)
        self.assertEqual(resp["pipeline_health"], "ok")

    def test_metrics(self):
        """METRICS returns all metric stats."""
        self.metrics.record("test_metric", 42.0)
        resp = self._query("METRICS")
        self.assertIn("test_metric", resp)
        self.assertAlmostEqual(resp["test_metric"]["last"], 42.0)

    def test_db_commands_without_db(self):
        """DB proxy commands report 'database not enabled' when no db is wired."""
        for cmd in ("DB_STATS", "SCAN_SUMMARY", "CAL_HISTORY"):
            resp = self._query(cmd)
            self.assertIn("error", resp)

    def test_concurrent_clients(self):
        """A slow client must not block other status consumers (threaded
        accept loop)."""
        self.server.update_status({"sync_state": 6, "counters": {}})
        # Open a connection and leave it idle (would stall a serial loop for
        # its full 5 s recv timeout)
        stalled = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        stalled.connect(("127.0.0.1", self.port))
        try:
            t0 = time.time()
            resp = self._query("PING")
            elapsed = time.time() - t0
            self.assertTrue(resp["ok"])
            self.assertLess(elapsed, 3.0)
        finally:
            stalled.close()


class TestStatusServerHealthWindow(unittest.TestCase):
    """Health derivation must use windowed drop deltas, not the cumulative
    totals (a single historical drop must not pin health at 'degraded')."""

    def setUp(self):
        self.port = _free_port()
        self.server = StatusServer(port=self.port, drop_window_sec=0.3)
        self.server.start()
        assert _wait_listening(self.port), "StatusServer did not come up"

    def tearDown(self):
        self.server.close()

    def _query_status(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("127.0.0.1", self.port))
        s.sendall(b"STATUS\n")
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        return json.loads(data.decode())

    def _snapshot(self, drops_iq):
        return {
            "sync_state": 6,
            "counters": {"dropped_frames_iq": drops_iq, "dropped_frames_hwc": 0},
        }

    def test_recent_drop_degrades_health(self):
        self.server.update_status(self._snapshot(0))
        self.server.update_status(self._snapshot(3))  # 3 new drops
        resp = self._query_status()
        self.assertEqual(resp["pipeline_health"], "degraded")
        self.assertEqual(resp["recent_drops"], 3)
        # Cumulative totals stay visible for operators
        self.assertEqual(resp["counters"]["dropped_frames_iq"], 3)

    def test_health_recovers_after_window(self):
        self.server.update_status(self._snapshot(0))
        self.server.update_status(self._snapshot(3))
        time.sleep(0.4)  # window (0.3 s) elapses
        self.server.update_status(self._snapshot(3))  # no NEW drops
        resp = self._query_status()
        self.assertEqual(resp["pipeline_health"], "ok")
        self.assertEqual(resp["recent_drops"], 0)
        self.assertEqual(resp["counters"]["dropped_frames_iq"], 3)

    def test_first_snapshot_total_not_counted_as_recent(self):
        # Counters carry a historical total at the first heartbeat
        self.server.update_status(self._snapshot(42))
        resp = self._query_status()
        self.assertEqual(resp["recent_drops"], 0)
        self.assertEqual(resp["pipeline_health"], "ok")


class TestStatusServerInstanceId(unittest.TestCase):

    def test_instance_id_injected(self):
        port = _free_port()
        server = StatusServer(port=port, instance_id=3)
        server.start()
        assert _wait_listening(port), "StatusServer did not come up"
        try:
            server.update_status({"sync_state": 6, "counters": {}})
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(("127.0.0.1", port))
            s.sendall(b"STATUS\n")
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            s.close()
            resp = json.loads(data.decode())
            self.assertEqual(resp["instance_id"], 3)
        finally:
            server.close()


class TestStatusServerDBProxy(unittest.TestCase):
    """The DB read API is reachable through the status server when a db
    object is wired in."""

    class FakeDB:
        def get_db_stats(self):
            return {"frame_metrics.db": {"size_bytes": 123}, "read_errors": 0}

        def get_freq_scan_summary(self, freq=None):
            class R:
                pass
            r = R()
            r.rf_center_freq = freq if freq else 433000000
            r.total_frames = 10
            return [r]

        def get_cal_history(self, rf_center_freq=None, limit=100):
            return []

    def setUp(self):
        self.port = _free_port()
        self.server = StatusServer(port=self.port, db=self.FakeDB())
        self.server.start()
        assert _wait_listening(self.port), "StatusServer did not come up"

    def tearDown(self):
        self.server.close()

    def _query(self, cmd):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("127.0.0.1", self.port))
        s.sendall((cmd + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        return json.loads(data.decode())

    def test_db_stats(self):
        resp = self._query("DB_STATS")
        self.assertIn("frame_metrics.db", resp)

    def test_scan_summary(self):
        resp = self._query("SCAN_SUMMARY")
        self.assertEqual(resp["freq_scan"][0]["total_frames"], 10)

    def test_scan_summary_with_freq(self):
        resp = self._query("SCAN_SUMMARY 868000000")
        self.assertEqual(resp["freq_scan"][0]["rf_center_freq"], 868000000)

    def test_cal_history(self):
        resp = self._query("CAL_HISTORY")
        self.assertEqual(resp["cal_history"], [])

    def test_scan_summary_bad_arg(self):
        resp = self._query("SCAN_SUMMARY notanumber")
        self.assertIn("error", resp)



# ======================================================================
# Extended status grammar ([monitoring] status_extended, spec G.1/G.2/G.7)
# and event identity (G.4) — WP-06
# ======================================================================

import daq_events  # noqa: E402
import daq_status_server  # noqa: E402

# Replies recorded from the pre-extension status server (byte for byte):
# with status_extended = 0 every one of them must stay identical.
_LEGACY_UNKNOWN = (b'{"error": "unknown command", "valid": ["PING", "STATUS", "METRICS", '
                   b'"EVENTS", "EVENTS_DROPPED", "DB_STATS", "SCAN_SUMMARY", "CAL_HISTORY"]}\n')
_LEGACY_FIXTURES_NO_DB = {
    b"PEERS": _LEGACY_UNKNOWN,
    b"HWC": _LEGACY_UNKNOWN,
    b"HW_SNAPSHOTS": _LEGACY_UNKNOWN,
    b"HW_SNAPSHOTS 0 5": _LEGACY_UNKNOWN,
    b"FOO": _LEGACY_UNKNOWN,
    b"CAL_HISTORY 1 2 3": b'{"error": "database not enabled"}\n',
    b"CAL_HISTORY - 0 10": b'{"error": "database not enabled"}\n',
    b"CAL_HISTORY x": b'{"error": "database not enabled"}\n',
    b"DB_STATS": b'{"error": "database not enabled"}\n',
    b"EVENTS": b'{"error": "event bus not enabled"}\n',
    b"METRICS": b'{"error": "metrics not enabled"}\n',
}
_LEGACY_FIXTURES_DB = {
    b"PEERS": _LEGACY_UNKNOWN,
    b"HWC": _LEGACY_UNKNOWN,
    b"HW_SNAPSHOTS": _LEGACY_UNKNOWN,
    b"HW_SNAPSHOTS 0 5": _LEGACY_UNKNOWN,
    b"FOO": _LEGACY_UNKNOWN,
    b"CAL_HISTORY 1 2 3": b'{"error": "usage: CAL_HISTORY [rf_center_freq_hz]"}\n',
    b"CAL_HISTORY - 0 10": b'{"error": "usage: CAL_HISTORY [rf_center_freq_hz]"}\n',
    b"CAL_HISTORY x": b'{"error": "usage: CAL_HISTORY [rf_center_freq_hz]"}\n',
    b"DB_STATS": b'{"frame_metrics.db": {"size_bytes": 123}, "read_errors": 0}\n',
}
# STATUS keys the server adds to the delay_sync snapshot (pre-extension)
_LEGACY_STATUS_ADDED = {"timestamp", "uptime_sec", "recent_drops", "drop_window_sec",
                        "pipeline_health"}


class _FakeDB:
    """DB stand-in recording the read-API calls."""

    def __init__(self):
        self.calls = []

    def get_db_stats(self):
        return {"frame_metrics.db": {"size_bytes": 123}, "read_errors": 0}

    def get_instance_info(self):
        return {"db_dir": "_db/inst2", "instance_id": 2}

    def get_freq_scan_summary(self, freq=None):
        return []

    def get_cal_history(self, start_ts_ms=0, end_ts_ms=None, rf_center_freq=None, limit=1000):
        self.calls.append(("cal_history", start_ts_ms, rf_center_freq, limit))

        class R:
            pass
        r = R()
        r.timestamp_ms = start_ts_ms + 1
        r.rf_center_freq = rf_center_freq or 0
        return [r]

    def get_hw_snapshots(self, start_ts_ms=0, end_ts_ms=None, limit=1000):
        self.calls.append(("hw_snapshots", start_ts_ms, limit))

        class R:
            pass
        r = R()
        r.timestamp_ms = start_ts_ms + 2
        r.overdrive_events = 0
        return [r]


def _raw_query(port, cmd, timeout=3.0):
    """Send one command line, return the raw reply bytes (up to '\n' or EOF)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(cmd + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        return data
    finally:
        s.close()


class _ServerCase(unittest.TestCase):
    """Starts a StatusServer per test (free port, torn down afterwards)."""

    def start_server(self, **kwargs):
        port = _free_port()
        server = StatusServer(port=port, **kwargs)
        server.start()
        self.assertTrue(_wait_listening(port), "StatusServer did not come up")
        self.addCleanup(server.close)
        return server, port

    def query(self, port, cmd):
        return json.loads(_raw_query(port, cmd.encode()).decode())

    def start_capped_server(self, **kwargs):
        """start_server, then wait until the _wait_listening probe is fully
        handled: one answered PING (the accept loop is FIFO, so the probe
        was accepted before it), then every client and busy-replier slot
        free again. Otherwise the probe can be accepted late and count as
        a rejected client in the cap tests."""
        server, port = self.start_server(**kwargs)
        deadline = time.monotonic() + 10.0
        while True:
            try:
                if "ok" in self.query(port, "PING"):
                    break
            except (OSError, ValueError):
                pass
            self.assertLess(time.monotonic(), deadline, "StatusServer never answered PING")
            time.sleep(0.02)
        sems = [server._client_sem, server._busy_sem]
        for sem in sems:
            for _ in range(server._max_clients):
                self.assertTrue(sem.acquire(timeout=10.0))
            for _ in range(server._max_clients):
                sem.release()
        return server, port


class TestStatusLegacyBytes(_ServerCase):
    """status_extended = 0: every reply is today's, byte for byte."""

    def test_legacy_replies_without_db(self):
        _server, port = self.start_server()
        for cmd, want in _LEGACY_FIXTURES_NO_DB.items():
            self.assertEqual(_raw_query(port, cmd), want, cmd)

    def test_legacy_replies_with_db(self):
        _server, port = self.start_server(db=_FakeDB())
        for cmd, want in _LEGACY_FIXTURES_DB.items():
            self.assertEqual(_raw_query(port, cmd), want, cmd)

    def test_legacy_status_key_set(self):
        server, port = self.start_server()
        snap = {"sync_state": 6, "current_frequency_hz": 433000000, "frame_count": 7,
                "aggregate_power_db": 0.0, "calibration_status": "locked",
                "counters": {"dropped_frames_iq": 0, "dropped_frames_hwc": 0}}
        server.update_status(snap)
        resp = self.query(port, "STATUS")
        self.assertEqual(set(resp), set(snap) | _LEGACY_STATUS_ADDED)
        self.assertEqual(resp["counters"], snap["counters"])

    def test_legacy_never_stale_and_ignores_extended_options(self):
        """Extended-only options are inert without extended=True."""
        clock = [100.0]
        server, port = self.start_server(stale_after_sec=1.0, max_clients=1,
                                         _clock=lambda: clock[0])
        server.update_status({"sync_state": 6, "counters": {}})
        clock[0] += 1000.0
        resp = self.query(port, "STATUS")
        self.assertEqual(resp["pipeline_health"], "ok")
        self.assertNotIn("snapshot_age_sec", resp)
        self.assertNotIn("status_protocol", resp)
        self.assertEqual(server._max_clients, StatusServer.MAX_CLIENTS)

    def test_legacy_client_cap_closes_silently(self):
        server, port = self.start_capped_server()
        for _ in range(StatusServer.MAX_CLIENTS):
            self.assertTrue(server._client_sem.acquire(timeout=5.0))
        try:
            try:
                reply = _raw_query(port, b"PING")
            except ConnectionResetError:   # closed without reading the request
                reply = b""
            self.assertEqual(reply, b"")
        finally:
            for _ in range(StatusServer.MAX_CLIENTS):
                server._client_sem.release()


class TestStatusExtended(_ServerCase):

    def _snapshot(self, sync_state=6):
        return {"sync_state": sync_state, "counters": {"dropped_frames_iq": 0,
                                                       "dropped_frames_hwc": 0}}

    def test_documented_keys(self):
        server, port = self.start_server(extended=True, frame_period_sec=0.437,
                                         update_interval_frames=100)
        server.update_status(self._snapshot())
        resp = self.query(port, "STATUS")
        self.assertEqual(resp["status_protocol"], 2)
        self.assertIn("snapshot_age_sec", resp)
        # auto threshold: 3 * 100 * 0.437 + 1 = 132.1 s
        self.assertAlmostEqual(resp["stale_after_sec"], 132.1, places=3)
        self.assertEqual(resp["pipeline_health"], "ok")
        self.assertNotIn("federation", resp)   # no provider -> absent
        self.assertNotIn("hwc", resp)

    def test_auto_stale_formula(self):
        f = daq_status_server.auto_stale_after_sec
        self.assertAlmostEqual(f(100, 0.437), 132.1)
        self.assertAlmostEqual(f(10, 0.437), 14.11)
        self.assertAlmostEqual(f(10, 2**22 / 2.4e6), 3 * 10 * 2**22 / 2.4e6 + 1.0)
        self.assertAlmostEqual(f(0, 1.0), 4.0)     # k_eff = max(1, k)
        self.assertIsNone(f(10, None))

    def test_snapshot_age_and_stale(self):
        clock = [1000.0]
        server, port = self.start_server(extended=True, frame_period_sec=0.5,
                                         update_interval_frames=2, _clock=lambda: clock[0])
        server.update_status(self._snapshot())
        self.assertAlmostEqual(server.stale_after_sec, 4.0)  # 3*2*0.5 + 1
        clock[0] += 1.5
        resp = self.query(port, "STATUS")
        self.assertAlmostEqual(resp["snapshot_age_sec"], 1.5, places=3)
        self.assertEqual(resp["pipeline_health"], "ok")
        clock[0] += 3.0                                      # age 4.5 > 4.0
        resp = self.query(port, "STATUS")
        self.assertAlmostEqual(resp["snapshot_age_sec"], 4.5, places=3)
        self.assertEqual(resp["pipeline_health"], "stale")
        self.assertEqual(server.get_pipeline_health(), "stale")
        server.update_status(self._snapshot())               # fresh again
        self.assertEqual(self.query(port, "STATUS")["pipeline_health"], "ok")

    def test_explicit_stale_after_overrides_auto(self):
        server, _port = self.start_server(extended=True, stale_after_sec=7.5,
                                          frame_period_sec=0.437, update_interval_frames=100)
        self.assertEqual(server.stale_after_sec, 7.5)

    def test_busy_reply_at_client_cap(self):
        server, port = self.start_capped_server(extended=True, max_clients=2)
        for _ in range(2):
            self.assertTrue(server._client_sem.acquire(timeout=5.0))
        try:
            self.assertEqual(json.loads(_raw_query(port, b"PING").decode()),
                             {"error": "busy"})
        finally:
            for _ in range(2):
                server._client_sem.release()
        self.assertTrue(self.query(port, "PING")["ok"])

    def test_busy_repliers_are_bounded(self):
        """With every busy-replier slot in use, a rejected client is closed
        silently instead of getting one more thread."""
        server, port = self.start_capped_server(extended=True, max_clients=2)
        for sem in (server._client_sem, server._busy_sem):
            for _ in range(2):
                self.assertTrue(sem.acquire(timeout=5.0))
        try:
            try:
                reply = _raw_query(port, b"PING")
            except ConnectionResetError:   # closed without reading the request
                reply = b""
            self.assertEqual(reply, b"")    # no busy reply: no replier thread ran
        finally:
            for sem in (server._busy_sem, server._client_sem):
                for _ in range(2):
                    sem.release()
        self.assertTrue(self.query(port, "PING")["ok"])

    def test_busy_replier_slot_released(self):
        server, port = self.start_capped_server(extended=True, max_clients=1)
        self.assertTrue(server._client_sem.acquire(timeout=5.0))
        try:
            for _ in range(3):      # more rejects than busy slots: each slot is returned
                self.assertEqual(json.loads(_raw_query(port, b"PING").decode()),
                                 {"error": "busy"})
                deadline = time.monotonic() + 5.0
                while not server._busy_sem.acquire(blocking=False):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                server._busy_sem.release()
        finally:
            server._client_sem.release()

    def test_thread_exhaustion_keeps_accept_loop(self):
        server, port = self.start_capped_server(extended=True, max_clients=2)
        real_start = threading.Thread.start
        failing = {"on": True}

        def start(thread):
            if failing["on"] and thread.name == "StatusClient":
                raise RuntimeError("can't start new thread")
            return real_start(thread)
        with patch.object(threading.Thread, "start", start):
            try:
                reply = _raw_query(port, b"PING")
            except ConnectionResetError:
                reply = b""
            self.assertEqual(reply, b"")
            failing["on"] = False
            self.assertTrue(self.query(port, "PING")["ok"])
        # The failed start returned its client slot
        for _ in range(2):
            self.assertTrue(server._client_sem.acquire(timeout=5.0))
        for _ in range(2):
            server._client_sem.release()

    def test_unknown_verb_lists_new_verbs(self):
        _server, port = self.start_server(extended=True)
        resp = self.query(port, "FOO")
        self.assertEqual(resp["error"], "unknown command")
        self.assertEqual(resp["valid"][:8], ["PING", "STATUS", "METRICS", "EVENTS",
                                             "EVENTS_DROPPED", "DB_STATS", "SCAN_SUMMARY",
                                             "CAL_HISTORY"])
        self.assertEqual(resp["valid"][8:], ["PEERS", "HWC", "HW_SNAPSHOTS"])

    def test_peers(self):
        _server, port = self.start_server(extended=True)
        self.assertEqual(self.query(port, "PEERS"), {"error": "federation not enabled"})
        block = {"instance_id": 1, "node_id": "hosta:1", "cluster_id": "default",
                 "health_mode": "mesh", "coordinator": {"endpoint": None},
                 "advisory_leader": 0, "peers": {"hostb:0": {"alive": True}}}
        server2, port2 = self.start_server(extended=True, federation_provider=lambda: block)
        self.assertEqual(self.query(port2, "PEERS"),
                         {"instance_id": 1, "node_id": "hosta:1", "advisory_leader": 0,
                          "peers": {"hostb:0": {"alive": True}}})
        server2.update_status(self._snapshot())
        self.assertEqual(self.query(port2, "STATUS")["federation"], block)

    def test_hwc(self):
        _server, port = self.start_server(extended=True)
        self.assertEqual(self.query(port, "HWC"), {"error": "hwc relay not enabled"})
        state = {"view": None, "error": "hwc state unreadable"}
        server2, port2 = self.start_server(
            extended=True, hwc_provider=lambda: (state["view"], state["error"]))
        server2.update_status(self._snapshot())
        self.assertEqual(self.query(port2, "HWC"), {"error": "hwc state unreadable"})
        resp = self.query(port2, "STATUS")
        self.assertIsNone(resp["hwc"])
        self.assertEqual(resp["hwc_error"], "hwc state unreadable")
        state["view"] = {"v": 1, "fsm": "IQ_CAL", "age_sec": 0.2}
        self.assertEqual(self.query(port2, "HWC"), state["view"])
        resp = self.query(port2, "STATUS")
        self.assertEqual(resp["hwc"], state["view"])
        self.assertNotIn("hwc_error", resp)

    def test_cal_history_forms(self):
        db = _FakeDB()
        _server, port = self.start_server(extended=True, db=db)
        # bare and one-argument forms are today's, incl. the old usage text
        self.assertEqual(len(self.query(port, "CAL_HISTORY")["cal_history"]), 1)
        self.assertEqual(self.query(port, "CAL_HISTORY x"),
                         {"error": "usage: CAL_HISTORY [rf_center_freq_hz]"})
        # new 2/3-argument forms
        resp = self.query(port, "CAL_HISTORY - 1000 5")
        self.assertEqual(resp["cal_history"][0]["timestamp_ms"], 1001)
        self.assertEqual(db.calls[-1], ("cal_history", 1000, None, 5))
        self.query(port, "CAL_HISTORY 433000000 7")
        self.assertEqual(db.calls[-1], ("cal_history", 7, 433000000, 100))
        usage = {"error": "usage: CAL_HISTORY [rf_center_freq_hz|-] [since_ms] [limit]"}
        for bad in ("CAL_HISTORY - x", "CAL_HISTORY - 0 0", "CAL_HISTORY - 0 1001",
                    "CAL_HISTORY a 0", "CAL_HISTORY - 0 5 9", "CAL_HISTORY - -1"):
            self.assertEqual(self.query(port, bad), usage, bad)

    def test_hw_snapshots(self):
        _server, port = self.start_server(extended=True)
        self.assertEqual(self.query(port, "HW_SNAPSHOTS"), {"error": "database not enabled"})
        db = _FakeDB()
        _server2, port2 = self.start_server(extended=True, db=db)
        resp = self.query(port2, "HW_SNAPSHOTS")
        self.assertEqual(resp["hw_snapshots"][0]["timestamp_ms"], 2)
        self.assertEqual(db.calls[-1], ("hw_snapshots", 0, 100))
        self.query(port2, "HW_SNAPSHOTS 500 3")
        self.assertEqual(db.calls[-1], ("hw_snapshots", 500, 3))
        usage = {"error": "usage: HW_SNAPSHOTS [since_ms] [limit]"}
        for bad in ("HW_SNAPSHOTS x", "HW_SNAPSHOTS 0 0", "HW_SNAPSHOTS 0 2000",
                    "HW_SNAPSHOTS 1 2 3"):
            self.assertEqual(self.query(port2, bad), usage, bad)

    def test_db_stats_additive_keys(self):
        _server, port = self.start_server(extended=True, db=_FakeDB())
        resp = self.query(port, "DB_STATS")
        self.assertEqual(resp["frame_metrics.db"], {"size_bytes": 123})
        self.assertEqual(resp["read_errors"], 0)
        self.assertEqual(resp["db_dir"], "_db/inst2")
        self.assertEqual(resp["instance_id"], 2)


class TestEventIdentity(unittest.TestCase):

    def test_untagged_json_is_todays_asdict(self):
        evt = DAQEvent(timestamp=1.5, severity="info", module="delay_sync",
                       event_type=EVT_SYNC_LOCK, payload={"freq": 1})
        want = {"timestamp": 1.5, "severity": "info", "module": "delay_sync",
                "event_type": "sync_lock", "payload": {"freq": 1}}
        self.assertEqual(evt.to_dict(), want)
        self.assertEqual(list(evt.to_dict()), list(want))
        self.assertEqual(evt.to_json(), json.dumps(want))

    def test_tagged_bus_sets_source(self):
        source = {"instance_id": 2, "node_id": "hosta:2", "host": "hosta",
                  "cluster_id": "lab"}
        bus = EventBus(enabled=True, ring_size=10, source=source)
        received = []
        bus.register_handler(received.append)
        bus.emit(DAQEvent(severity="info", module="t", event_type=EVT_HEARTBEAT))
        bus.close()
        self.assertEqual(received[0].source, source)
        d = received[0].to_dict()
        self.assertEqual(d["source"], source)
        self.assertEqual(list(d)[-1], "source")

    def test_untagged_bus_leaves_source_none(self):
        bus = EventBus(enabled=True, ring_size=10)
        received = []
        bus.register_handler(received.append)
        bus.emit(DAQEvent(severity="info", module="t", event_type=EVT_HEARTBEAT))
        bus.close()
        self.assertIsNone(received[0].source)
        self.assertNotIn("source", received[0].to_dict())

    def test_new_event_constants(self):
        self.assertEqual(daq_events.EVT_PEER_RECOVERED, "peer_recovered")
        self.assertEqual(daq_events.EVT_PEER_FLAPPING, "peer_flapping")
        self.assertEqual(daq_events.EVT_RECAL_REQUESTED, "recal_requested")
        self.assertEqual(daq_events.EVT_CAL_STALLED, "cal_stalled")
        self.assertEqual(daq_events.EVT_COORDINATOR_COMMAND, "coordinator_command")
        self.assertEqual(daq_events.EVT_NODE_KEY_COLLISION, "node_key_collision")
        self.assertEqual(daq_events.EVT_SCHEDULE_RECONCILE_FAILED, "schedule_reconcile_failed")
        self.assertEqual(daq_events.EVT_CAL_TIMEOUT, "cal_timeout")


class TestZMQTopicPrefix(unittest.TestCase):

    def _publish_and_receive(self, **handler_kwargs):
        try:
            import zmq
        except ImportError:
            self.skipTest("pyzmq not installed")
        port = _free_port()
        handler = ZMQPubHandler(port=port, listen_address="127.0.0.1", **handler_kwargs)
        self.addCleanup(handler.close)
        ctx = zmq.Context.instance()
        sub = ctx.socket(zmq.SUB)
        self.addCleanup(sub.close, 0)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.setsockopt(zmq.RCVTIMEO, 200)
        sub.connect("tcp://127.0.0.1:{}".format(port))
        evt = DAQEvent(timestamp=1.0, severity="info", module="t", event_type=EVT_HEARTBEAT)
        deadline = time.time() + 10.0
        while time.time() < deadline:      # PUB/SUB slow joiner: resend until received
            handler(evt)
            try:
                return sub.recv_multipart()
            except zmq.Again:
                continue
        self.fail("no event received on the PUB socket")

    def test_default_topic_is_event_type(self):
        topic, data = self._publish_and_receive()
        self.assertEqual(topic, b"heartbeat")
        self.assertNotIn("source", json.loads(data.decode()))

    def test_prefixed_topic(self):
        topic, _data = self._publish_and_receive(topic_prefix="inst3.")
        self.assertEqual(topic, b"inst3.heartbeat")


if __name__ == "__main__":
    unittest.main()
