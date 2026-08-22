"""Tests for the heimdall-ctl CLI against fake servers."""
import json
import socket
import struct
import sys
import threading
import time
import unittest
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "util"))

from heimdall_ctl.client.ctl import CtlClient
from heimdall_ctl.client.status import StatusClient
from heimdall_ctl.formatting import parse_freq, format_freq, parse_duration


class FakeStatusServer:
    """Minimal TCP server that responds to PING/STATUS/METRICS/EVENTS."""

    def __init__(self, port=0):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            try:
                data = conn.recv(1024).decode().strip().upper()
                if data == "PING":
                    resp = {"ok": True, "ts": time.time()}
                elif data == "STATUS":
                    resp = {
                        "sync_state": 6,
                        "rf_center_freq": 433000000,
                        "pipeline_health": "ok",
                        "uptime_sec": 100.0,
                    }
                elif data == "METRICS":
                    resp = {
                        "frame_processing_latency_ms": {
                            "min": 1.0, "max": 5.0, "avg": 2.5,
                            "p95": 4.0, "count": 100,
                        }
                    }
                elif data == "EVENTS":
                    resp = {"events": []}
                elif data == "EVENTS_DROPPED":
                    resp = {"dropped_events": 42}
                else:
                    resp = {"error": "unknown"}
                conn.sendall((json.dumps(resp) + "\n").encode())
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.srv.close()


class FakeCtlServer:
    """Minimal TCP server that accepts 128-byte frames and echoes back.

    Mirrors hw_controller's CtrIfaceServer framing: 4-byte ASCII verb
    (short verbs are space-padded, e.g. "AGC ") + 124-byte payload, reply
    'FNSD' + 124 bytes. Query verbs registered in `json_replies` answer with
    a NUL-padded UTF-8 JSON payload per the reply contract.
    """

    def __init__(self, port=0, json_replies=None):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self._stop = threading.Event()
        self.last_verb = None
        self.last_payload = None
        self.json_replies = json_replies or {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            try:
                data = conn.recv(128)
                if len(data) == 128:
                    raw_verb = data[:4].decode()
                    # Strip both NUL and space padding for assertions
                    self.last_verb = raw_verb.rstrip("\x00 ")
                    self.last_payload = data[4:]
                    if raw_verb in self.json_replies:
                        body = json.dumps(self.json_replies[raw_verb]).encode()
                        reply = b"FNSD" + body[:124].ljust(124, b"\x00")
                    else:
                        reply = b"FNSD" + b"\x00" * 124
                    conn.sendall(reply)
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.srv.close()


class TestFormatting(unittest.TestCase):
    def test_parse_freq(self):
        self.assertEqual(parse_freq("433M"), 433000000)
        self.assertEqual(parse_freq("1.2G"), 1200000000)
        self.assertEqual(parse_freq("868000000"), 868000000)
        self.assertEqual(parse_freq("433.92MHz"), 433920000)

    def test_format_freq(self):
        self.assertEqual(format_freq(433000000), "433.000 MHz")
        self.assertEqual(format_freq(1200000000), "1.200 GHz")

    def test_parse_duration(self):
        self.assertEqual(parse_duration("5m"), 300)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("1d"), 86400)


class TestStatusClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeStatusServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_ping(self):
        client = StatusClient("127.0.0.1", self.server.port)
        resp = client.ping()
        self.assertTrue(resp["ok"])

    def test_status(self):
        client = StatusClient("127.0.0.1", self.server.port)
        resp = client.status()
        self.assertEqual(resp["sync_state"], 6)
        self.assertEqual(resp["pipeline_health"], "ok")

    def test_metrics(self):
        client = StatusClient("127.0.0.1", self.server.port)
        resp = client.metrics()
        self.assertIn("frame_processing_latency_ms", resp)

    def test_events(self):
        client = StatusClient("127.0.0.1", self.server.port)
        resp = client.events()
        self.assertIn("events", resp)

    def test_events_dropped(self):
        client = StatusClient("127.0.0.1", self.server.port)
        resp = client.events_dropped()
        self.assertEqual(resp["dropped_events"], 42)


class TestCtlClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeCtlServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_freq(self):
        client = CtlClient("127.0.0.1", self.server.port)
        reply = client.freq(433000000)
        self.assertEqual(reply[:4], b"FNSD")
        time.sleep(0.1)
        self.assertEqual(self.server.last_verb, "FREQ")
        freq_val = struct.unpack("<Q", self.server.last_payload[:8])[0]
        self.assertEqual(freq_val, 433000000)

    def test_gain(self):
        client = CtlClient("127.0.0.1", self.server.port)
        client.gain([100, 200, 300, 400, 500])
        time.sleep(0.1)
        self.assertEqual(self.server.last_verb, "GAIN")
        gains = struct.unpack("<5I", self.server.last_payload[:20])
        self.assertEqual(gains, (100, 200, 300, 400, 500))

    def test_agc(self):
        client = CtlClient("127.0.0.1", self.server.port)
        client.agc()
        time.sleep(0.1)
        self.assertEqual(self.server.last_verb, "AGC")

    def test_schedule_stop(self):
        client = CtlClient("127.0.0.1", self.server.port)
        client.schedule_stop()
        time.sleep(0.1)
        self.assertEqual(self.server.last_verb, "SCHS")

    def test_agc_verb_is_space_padded(self):
        """hw_controller compares the literal 4-char string 'AGC '."""
        client = CtlClient("127.0.0.1", self.server.port)
        client.agc()
        time.sleep(0.1)
        self.assertEqual(self.server.last_payload, b"\x00" * 124)

    def test_recal_sends_init(self):
        """There is no RECL verb server-side; recal re-runs INIT."""
        client = CtlClient("127.0.0.1", self.server.port)
        reply = client.recal()
        self.assertEqual(reply[:4], b"FNSD")
        time.sleep(0.1)
        self.assertEqual(self.server.last_verb, "INIT")

    def test_ext_gain(self):
        client = CtlClient("127.0.0.1", self.server.port)
        client.ext_gain([145, 145, 145, 145, 145])
        time.sleep(0.1)
        self.assertEqual(self.server.last_verb, "EGAN")
        gains = struct.unpack("<5I", self.server.last_payload[:20])
        self.assertEqual(gains, (145, 145, 145, 145, 145))

    def test_bias_tee(self):
        client = CtlClient("127.0.0.1", self.server.port)
        client.bias_tee([1, 0, 1, 1, 0])
        time.sleep(0.1)
        self.assertEqual(self.server.last_verb, "BIAS")
        states = struct.unpack("<5I", self.server.last_payload[:20])
        self.assertEqual(states, (1, 0, 1, 1, 0))

    def test_bearing(self):
        client = CtlClient("127.0.0.1", self.server.port)
        client.bearing(123.5, 45.25)
        time.sleep(0.1)
        self.assertEqual(self.server.last_verb, "ORNT")
        az, el = struct.unpack("<ff", self.server.last_payload[:8])
        self.assertAlmostEqual(az, 123.5, places=3)
        self.assertAlmostEqual(el, 45.25, places=3)

    def test_orientation_verbs(self):
        client = CtlClient("127.0.0.1", self.server.port)
        for method, verb in ((client.park, "PARK"), (client.scan_start, "SCAN"),
                             (client.orientation_stop, "OSTP"),
                             (client.orientation_query, "OQRY")):
            method()
            time.sleep(0.1)
            self.assertEqual(self.server.last_verb, verb)

    def test_rf_query_verb(self):
        client = CtlClient("127.0.0.1", self.server.port)
        client.rf_query()
        time.sleep(0.1)
        self.assertEqual(self.server.last_verb, "RFQ")


class TestCtlReplyParsing(unittest.TestCase):
    """Contract: replies are 'FNSD' + NUL-padded UTF-8 JSON for query verbs,
    all-zeros for plain acks."""

    def test_parse_ack(self):
        reply = b"FNSD" + b"\x00" * 124
        parsed = CtlClient.parse_reply(reply)
        self.assertTrue(parsed["ok"])
        self.assertIsNone(parsed["data"])

    def test_parse_json_payload(self):
        body = json.dumps({"state": "SETTLED", "bearing_az_deg": 45.0}).encode()
        reply = b"FNSD" + body.ljust(124, b"\x00")
        parsed = CtlClient.parse_reply(reply)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["data"]["state"], "SETTLED")
        self.assertAlmostEqual(parsed["data"]["bearing_az_deg"], 45.0)

    def test_parse_non_json_payload_falls_back_to_raw(self):
        reply = b"FNSD" + b"hello world".ljust(124, b"\x00")
        parsed = CtlClient.parse_reply(reply)
        self.assertTrue(parsed["ok"])
        self.assertIsNone(parsed["data"])
        self.assertEqual(parsed["raw"], "hello world")

    def test_parse_bad_reply(self):
        parsed = CtlClient.parse_reply(b"NOPE" + b"\x00" * 124)
        self.assertFalse(parsed["ok"])
        parsed = CtlClient.parse_reply(b"")
        self.assertFalse(parsed["ok"])

    def test_query_roundtrip_with_json_reply(self):
        server = FakeCtlServer(json_replies={
            "OQRY": {"state": "IDLE", "bearing_az_deg": 0.0}})
        try:
            client = CtlClient("127.0.0.1", server.port)
            parsed = CtlClient.parse_reply(client.orientation_query())
            self.assertTrue(parsed["ok"])
            self.assertEqual(parsed["data"]["state"], "IDLE")
        finally:
            server.close()


class TestEventDropCounter(unittest.TestCase):
    def test_drop_counter_increments(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(__file__), "..", "..", "_daq_core"))
        from daq_events import EventBus, DAQEvent, EVT_HEARTBEAT, EVT_EVENT_QUEUE_FULL

        bus = EventBus(enabled=True, queue_size=2)
        time.sleep(0.1)

        # Fill the queue
        for _ in range(20):
            bus.emit(DAQEvent(severity="info", module="test",
                              event_type=EVT_HEARTBEAT, payload={}))

        time.sleep(0.5)
        self.assertGreater(bus.dropped_events, 0)

        # Check that the meta-event was stored in the ring
        recent = bus.get_recent_events(500)
        queue_full_events = [e for e in recent if e.event_type == EVT_EVENT_QUEUE_FULL]
        self.assertGreater(len(queue_full_events), 0)
        bus.close()


if __name__ == "__main__":
    unittest.main()
