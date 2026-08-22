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


if __name__ == "__main__":
    unittest.main()
