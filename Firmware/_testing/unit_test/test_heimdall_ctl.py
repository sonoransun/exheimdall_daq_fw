"""Tests for the heimdall-ctl CLI against fake servers."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

UTIL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "util"))
DAQ_CORE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "_daq_core"))
sys.path.insert(0, UTIL_DIR)

from heimdall_ctl import __main__ as cli  # noqa: E402
from heimdall_ctl.client import ctl as ctl_mod  # noqa: E402
from heimdall_ctl.client.ctl import CtlClient, AckModeCache  # noqa: E402
from heimdall_ctl.client.status import StatusClient  # noqa: E402
from heimdall_ctl.client.db import default_db_dir  # noqa: E402
from heimdall_ctl.config import resolve_ports  # noqa: E402
from heimdall_ctl.formatting import (  # noqa: E402
    parse_freq, format_freq, parse_duration, format_ts)

HAVE_ZMQ = importlib.util.find_spec("zmq") is not None

FAKE_TIMEOUT = 10.0


def _recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


class FakeStatusServer:
    """Minimal TCP server that responds to PING/STATUS/METRICS/EVENTS.

    responses: optional {COMMAND: dict | callable(arg) -> dict} overriding
    the defaults; every received line is recorded in .commands."""

    def __init__(self, port=0, responses=None):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.responses = responses or {}
        self.commands = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _default(self, cmd):
        if cmd == "PING":
            return {"ok": True, "ts": time.time()}
        if cmd == "STATUS":
            return {"sync_state": 6, "rf_center_freq": 433000000,
                    "pipeline_health": "ok", "uptime_sec": 100.0}
        if cmd == "METRICS":
            return {"frame_processing_latency_ms": {
                "min": 1.0, "max": 5.0, "avg": 2.5, "p95": 4.0, "count": 100}}
        if cmd == "EVENTS":
            return {"events": []}
        if cmd == "EVENTS_DROPPED":
            return {"dropped_events": 42}
        return {"error": "unknown"}

    def _run(self):
        self.srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(FAKE_TIMEOUT)
                line = conn.recv(1024).decode().strip()
                self.commands.append(line)
                parts = line.split(None, 1)
                cmd = parts[0].upper() if parts else ""
                arg = parts[1] if len(parts) > 1 else ""
                handler = self.responses.get(cmd)
                if handler is None:
                    resp = self._default(cmd)
                elif callable(handler):
                    resp = handler(arg)
                else:
                    resp = handler
                conn.sendall((json.dumps(resp) + "\n").encode())
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.srv.close()


class FakeCtlServer:
    """TCP server speaking hw_controller's CtrIfaceServer framing.

    4-byte ASCII verb (short verbs are space-padded, e.g. "AGC ") + 124-byte
    payload, reply 'FNSD' + 124 bytes; several frames per connection.
    Query verbs registered in `json_replies` answer with a NUL-padded JSON
    payload per the reply contract.

    mode="legacy": XACK and every new verb answer FNSD + zeros (an old
    hw_controller). mode="extended": XACK answers {"xack":true,...}; on an
    XACK connection mutating verbs answer extended acks (``acks`` overrides
    per verb, a list is consumed in order); HWSQ/SCHG/TLCK answer JSON;
    SCHB/SCHC/SCHE reassemble a chunked schedule and check its CRC.
    """

    MUTATING = ("FREQ", "GAIN", "AGC ", "INIT", "SCHD", "SCHS", "SCHN", "EGAN", "BIAS",
                "ORNT", "PARK", "SCAN", "OSTP", "RECL", "TLKA", "SCHE")

    def __init__(self, port=0, json_replies=None, mode="legacy", caps="hwsq,schb,schg,tlck",
                 nch=5, rto=10000, hmx=12000, acks=None, xack_reply=None, hwsq=None,
                 schg=None):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self._stop = threading.Event()
        self.last_verb = None
        self.last_payload = None
        self.json_replies = json_replies or {}
        self.mode = mode
        self.caps = caps
        self.nch = nch
        self.rto = rto
        self.hmx = hmx
        self.acks = acks or {}
        self.xack_reply = xack_reply
        self.hwsq = hwsq or {"fsm": "IQ_CAL", "pend": 0, "ns": 0, "agc": 0, "nch": nch,
                             "f": 433000000, "fage": 437, "fr": 1, "fc": 1234, "t": 0}
        self.schg = schg or {"gen": None, "loaded": False, "active": False, "st": "IDLE",
                             "idx": 0, "total": 0}
        self.frames = []          # (connection index, raw verb, payload)
        self.xack_count = 0
        self.schedule = None      # assembled SCHB/SCHC document
        self.schc_burst = 0       # max SCHC frames received before a reply was read
        self._seq = 0
        self._conn_index = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def verbs(self):
        return [v for _, v, _ in self.frames]

    def _json(self, obj):
        body = json.dumps(obj, separators=(",", ":")).encode()
        return b"FNSD" + body[:124].ljust(124, b"\x00")

    def _ack(self, raw_verb):
        verb = raw_verb.rstrip()
        spec = self.acks.get(verb)
        if isinstance(spec, list):
            spec = spec.pop(0) if spec else None
        self._seq += 1
        if spec is None:
            spec = {"applied": True, "reason": "ok", "fsm": "IQ_CAL"}
        return dict(spec, seq=self._seq)

    def _reply(self, state, raw_verb, payload):
        zeros = b"FNSD" + b"\x00" * 124
        if raw_verb == "XACK":
            self.xack_count += 1
            if self.mode != "extended":
                return zeros
            state["xack"] = True
            return self._json(self.xack_reply or {
                "xack": True, "v": 1, "nch": self.nch, "term": 0, "caps": self.caps,
                "rto": self.rto, "fp": 437, "hmx": self.hmx})
        if raw_verb in self.json_replies:
            return self._json(self.json_replies[raw_verb])
        if self.mode != "extended":
            return zeros
        if raw_verb == "HWSQ":
            return self._json(self.hwsq)
        if raw_verb == "SCHG":
            return self._json(self.schg)
        if raw_verb == "TLCK":
            return self._json({"req": 1, "appr": 0})
        if raw_verb == "SCHB":
            total, crc, nchunks, _ = struct.unpack("<IIHH", payload[:12])
            state["sched"] = {"total": total, "crc": crc, "n": nchunks, "data": b"",
                              "seq": 0, "sess": 7}
            return self._json({"ok": True, "sess": 7})
        if raw_verb == "SCHC":
            sess, seq, length = struct.unpack("<HHH", payload[:6])
            sched = state.get("sched")
            if sched is None or sess != sched["sess"]:
                return self._json({"ok": False, "reason": "bad_session"})
            if seq != sched["seq"]:
                return self._json({"ok": False, "reason": "out_of_order", "seq": seq})
            sched["data"] += payload[6:6 + length]
            sched["seq"] += 1
            return self._json({"ok": True, "seq": seq})
        if raw_verb == "SCHE":
            sched = state.pop("sched", None)
            if sched is None:
                return self._json({"applied": False, "reason": "incomplete", "entries": 0})
            if zlib.crc32(sched["data"]) & 0xFFFFFFFF != sched["crc"]:
                return self._json({"applied": False, "reason": "crc_mismatch", "entries": 0})
            self.schedule = json.loads(sched["data"].decode())
            return self._json({"applied": True, "reason": "ok",
                               "entries": len(self.schedule.get("entries", [])),
                               "fsm": "IQ_CAL", "seq": 1})
        if raw_verb in self.MUTATING and state.get("xack"):
            return self._json(self._ack(raw_verb))
        return zeros

    def _serve(self, conn, index):
        state = {}
        conn.settimeout(FAKE_TIMEOUT)
        pending_schc = 0
        while True:
            try:
                data = _recv_exact(conn, 128)
            except OSError:
                return
            if data is None:
                return
            raw_verb = data[:4].decode("ascii", errors="replace")
            payload = data[4:]
            self.frames.append((index, raw_verb, payload))
            if raw_verb != "XACK":
                # Strip both NUL and space padding for assertions
                self.last_verb = raw_verb.rstrip("\x00 ")
                self.last_payload = payload
            if raw_verb == "SCHC":
                # Count how many chunks the client pipelined before waiting.
                pending_schc += 1
                conn.setblocking(False)
                try:
                    peek = conn.recv(1, socket.MSG_PEEK)
                except (BlockingIOError, OSError):
                    peek = b""
                conn.settimeout(FAKE_TIMEOUT)
                self.schc_burst = max(self.schc_burst, pending_schc)
                if not peek:
                    pending_schc = 0
            try:
                conn.sendall(self._reply(state, raw_verb, payload))
            except OSError:
                return

    def _run(self):
        self.srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._conn_index += 1
            try:
                self._serve(conn, self._conn_index)
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.srv.close()


class HoldingServer:
    """Accepts connections and never reads (a port held by another client)."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.conns = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
                self.conns.append(conn)
            except socket.timeout:
                continue
            except OSError:
                break

    def received(self):
        """Everything the clients sent (call after they closed)."""
        out = b""
        for conn in self.conns:
            conn.settimeout(2.0)
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        return out

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        for conn in self.conns:
            conn.close()
        self.srv.close()


class CliTestBase(unittest.TestCase):
    """Runs heimdall-ctl's main() in-process in an empty temp cwd (so no
    daq_chain_config.ini is found) with a private ack-mode cache."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hctl_test_")
        self.cache_dir = os.path.join(self.tmp, "cache")
        self._env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache_dir})
        self._env.start()
        for key in ("HEIMDALL_CONFIG", "HEIMDALL_COORDINATOR", "HEIMDALL_AUTH_TOKEN",
                    "HEIMDALL_FIRMWARE", "HEIMDALL_RUN_DIR"):
            os.environ.pop(key, None)
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.servers = []

    def tearDown(self):
        os.chdir(self._cwd)
        self._env.stop()
        for srv in self.servers:
            srv.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def server(self, srv):
        self.servers.append(srv)
        return srv

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path


class TestFormatting(unittest.TestCase):
    def test_parse_freq(self):
        self.assertEqual(parse_freq("433M"), 433000000)
        self.assertEqual(parse_freq("1.2G"), 1200000000)
        self.assertEqual(parse_freq("868000000"), 868000000)
        self.assertEqual(parse_freq("433.92MHz"), 433920000)
        # float products like 1024.07e6 = 1024069999.99... round, not truncate
        self.assertEqual(parse_freq("1024.07M"), 1024070000)
        bad = [v for v in range(100000, 200000)
               if parse_freq("{}.{:02d}M".format(v // 100, v % 100)) != v * 10000]
        self.assertEqual(bad, [])

    def test_format_ts_carries_rounded_milliseconds(self):
        base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(101))
        self.assertEqual(format_ts(100.9996), base + ".000")
        self.assertTrue(format_ts(100.25).endswith(".250"))
        self.assertEqual(format_ts(None), "-")

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
        """The library recal() re-runs INIT (the CLI prefers RECL when advertised)."""
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

    def test_library_default_sends_no_xack(self):
        before = self.server.xack_count
        CtlClient("127.0.0.1", self.server.port).freq(100000000)
        self.assertEqual(self.server.xack_count, before)

    def test_send_size_guard(self):
        client = CtlClient("127.0.0.1", self.server.port)
        with self.assertRaisesRegex(ValueError, "payload exceeds 124 bytes"):
            client._send("SCHD", b"x" * 125)
        with self.assertRaisesRegex(ValueError, "payload exceeds 124 bytes"):
            client.schedule_load({"entries": [{"frequency": 433000000 + i}
                                              for i in range(20)]})
        # exactly 124 bytes still fits
        client._send("SCHD", b"y" * 124)
        time.sleep(0.1)
        self.assertEqual(self.server.last_payload, b"y" * 124)


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

    # --- additive keys (F.2) ---

    def test_parse_reply_additive_keys(self):
        parsed = CtlClient.parse_reply(b"FNSD" + b"\x00" * 124)
        self.assertEqual(parsed, {"ok": True, "data": None, "legacy": True})
        ack = {"applied": False, "reason": "not_confirmed", "fsm": "IQ_CAL", "seq": 9}
        parsed = CtlClient.parse_reply(b"FNSD" + json.dumps(ack).encode().ljust(124, b"\x00"))
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["data"], ack)
        self.assertIs(parsed["applied"], False)
        self.assertEqual(parsed["reason"], "not_confirmed")
        self.assertEqual(parsed["fsm"], "IQ_CAL")
        self.assertEqual(parsed["seq"], 9)
        # a query document has no ack keys
        parsed = CtlClient.parse_reply(b"FNSD" + b'{"az":1}'.ljust(124, b"\x00"))
        self.assertNotIn("applied", parsed)
        self.assertNotIn("legacy", parsed)

    def test_parse_ack_mapping(self):
        zeros = b"FNSD" + b"\x00" * 124
        self.assertEqual(CtlClient.parse_ack(zeros, False),
                         {"applied": None, "reason": None, "fsm": None, "legacy": True})
        body = b'{"applied":null,"reason":"held_for_burst","fsm":"NOISE_CTR_WAIT","seq":1}'
        self.assertEqual(CtlClient.parse_ack(b"FNSD" + body.ljust(124, b"\x00"), True),
                         {"applied": None, "reason": "held_for_burst",
                          "fsm": "NOISE_CTR_WAIT", "legacy": False})
        self.assertEqual(CtlClient.parse_ack(b"JUNK" + b"\x00" * 124, True)["reason"],
                         "bad_reply")
        self.assertIs(CtlClient.parse_ack(
            b"FNSD" + b'{"error":"busy","frames":0}'.ljust(124, b"\x00"), True)["applied"],
            False)


class TestAckModeCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hctl_cache_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_path_follows_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp}):
            self.assertEqual(AckModeCache.default_path(),
                             os.path.join(self.tmp, "heimdall-ctl", "ack_modes.json"))
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "relative/dir"}):
            self.assertTrue(AckModeCache.default_path().endswith(
                os.path.join(".cache", "heimdall-ctl", "ack_modes.json")))

    def test_put_get_ttl_and_file_format(self):
        now = [1000.0]
        path = os.path.join(self.tmp, "c", "ack_modes.json")
        cache = AckModeCache(path, clock=lambda: now[0])
        self.assertIsNone(cache.get("h:1"))
        cache.put("h:1", "legacy")
        cache.put("h:2", "extended")
        self.assertEqual(cache.get("h:1"), "legacy")
        self.assertEqual(cache.get("h:2"), "extended")
        with open(path) as f:
            self.assertEqual(json.load(f)["h:1"], {"mode": "legacy", "ts": 1000.0})
        self.assertEqual([n for n in os.listdir(os.path.dirname(path))], ["ack_modes.json"])
        now[0] += 24 * 3600 + 1
        self.assertIsNone(cache.get("h:1"))

    def test_corrupt_and_unwritable_cache_are_ignored(self):
        path = os.path.join(self.tmp, "ack_modes.json")
        with open(path, "w") as f:
            f.write("{not json")
        cache = AckModeCache(path)
        self.assertIsNone(cache.get("h:1"))
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        cache = AckModeCache(os.path.join(blocker, "sub", "ack_modes.json"))
        cache.put("h:1", "legacy")  # must not raise
        self.assertIsNone(cache.get("h:1"))


class TestAddressing(CliTestBase):
    def test_instance_without_config_applies_stride(self):
        self.assertEqual(resolve_ports(None, 2)["ctl"], 5201)
        code, out, err = self.run_cli("-v", "--instance", "2", "ports")
        self.assertEqual(code, 0)
        self.assertIn("ctl 127.0.0.1:5201, status 127.0.0.1:5202, events 127.0.0.1:5203", err)
        for port in ("5200", "5201", "5202", "5203", "1330", "6000", "7000", "7001"):
            self.assertIn(port, out)

    def test_port_stride_flag_and_config(self):
        code, out, _ = self.run_cli("--instance", "3", "--port-stride", "10", "ports",
                                    "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["ports"]["ctl (hw_controller)"], 5031)
        cfg = self.write("daq_chain_config.ini",
                         "[federation]\ninstance_id = 1\nport_stride = 100\n"
                         "[monitoring]\nstatus_server_port = 6002\nzmq_pub_port = 6003\n")
        code, out, _ = self.run_cli("--config", cfg, "ports", "--json")
        ports = json.loads(out)["ports"]
        self.assertEqual(ports["ctl (hw_controller)"], 5101)
        self.assertEqual(ports["status"], 6102)
        self.assertEqual(ports["events"], 6103)
        # subcommand-level --instance overrides the config's instance_id
        code, out, _ = self.run_cli("--config", cfg, "ports", "--instance", "0", "--json")
        self.assertEqual(json.loads(out)["ports"]["ctl (hw_controller)"], 5001)

    def test_connection_error_exit_3_without_traceback(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        code, out, err = self.run_cli("--port-ctl", str(port), "tune", "433M")
        self.assertEqual(code, 3)
        self.assertIn("Error: 127.0.0.1:{}:".format(port), err)
        self.assertNotIn("Traceback", err)
        code, _, err = self.run_cli("--port-status", str(port), "status")
        self.assertEqual(code, 3)
        self.assertIn("Error: 127.0.0.1:{}:".format(port), err)

    def test_launcher_help(self):
        launcher = os.path.join(UTIL_DIR, "bin", "heimdall-ctl")
        if not os.path.isfile(launcher):
            repo = os.path.dirname(UTIL_DIR)
            if (os.path.exists(os.path.join(repo, ".git"))
                    or os.path.isfile(os.path.join(repo, "pyproject.toml"))):
                self.fail("util/bin/heimdall-ctl missing from a source checkout")
            self.skipTest("util/bin/heimdall-ctl not present (published tree)")
        self.assertTrue(os.access(launcher, os.X_OK), "launcher must be executable")
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run([launcher, "--help"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"heimdall-ctl", proc.stdout)
        self.assertIn(b"fleet", proc.stdout)


class TestAckModes(CliTestBase):
    def test_legacy_server_cached_after_first_xack(self):
        srv = self.server(FakeCtlServer(mode="legacy"))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "tune", "433M")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "sent; server does not confirm application (legacy server)")
        self.assertEqual(srv.xack_count, 1)
        self.assertEqual(srv.verbs, ["XACK", "FREQ"])
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "tune", "434M")
        self.assertEqual(code, 0)
        self.assertEqual(srv.xack_count, 1, "second command must not send XACK")
        self.assertEqual(srv.verbs[-1], "FREQ")
        with open(os.path.join(self.cache_dir, "heimdall-ctl", "ack_modes.json")) as f:
            entry = json.load(f)["127.0.0.1:{}".format(srv.port)]
        self.assertEqual(entry["mode"], "legacy")
        # --ack xack ignores the cache
        self.run_cli("--port-ctl", str(srv.port), "--ack", "xack", "agc")
        self.assertEqual(srv.xack_count, 2)

    def test_explicit_xack_refreshes_a_stale_legacy_entry(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        AckModeCache().put("127.0.0.1:{}".format(srv.port), "legacy")
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "agc")
        self.assertEqual(code, 0)
        self.assertEqual(srv.xack_count, 0, "a cached legacy entry skips XACK")
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--ack", "xack", "agc")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("applied"), out)
        self.assertEqual(AckModeCache().get("127.0.0.1:{}".format(srv.port)), "extended")
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "agc")
        self.assertEqual(code, 0)
        self.assertEqual(srv.xack_count, 2, "auto mode probes again after the refresh")
        self.assertTrue(out.startswith("applied"), out)

    def test_unwritable_cache_still_works(self):
        blocker = os.path.join(self.tmp, "notadir")
        open(blocker, "w").close()
        srv = self.server(FakeCtlServer(mode="legacy"))
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": blocker}):
            for _ in range(2):
                code, out, _ = self.run_cli("--port-ctl", str(srv.port), "agc")
                self.assertEqual(code, 0)
        self.assertEqual(srv.xack_count, 2)
        self.assertEqual(srv.verbs.count("AGC "), 2)

    def test_queries_never_send_xack(self):
        srv = self.server(FakeCtlServer(mode="extended",
                                        json_replies={"OQRY": {"az": 1, "el": 2, "state": 0}}))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "orientation")
        self.assertEqual(code, 0)
        self.assertEqual(srv.verbs, ["OQRY"])

    def test_ack_legacy_sends_no_xack_and_warns(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        for flag in (["--ack", "legacy"], ["--legacy-ack"]):
            code, out, err = self.run_cli("--port-ctl", str(srv.port), *flag, "agc")
            self.assertEqual(code, 0)
            self.assertIn(ctl_mod.LEGACY_ACK_WARNING, err)
            self.assertEqual(out.strip(),
                             "sent; server does not confirm application (legacy server)")
        self.assertEqual(srv.xack_count, 0)
        self.assertEqual(srv.verbs, ["AGC ", "AGC "])

    def test_xack_timeout_sends_nothing(self):
        holder = self.server(HoldingServer())
        t0 = time.monotonic()
        code, out, err = self.run_cli("--port-ctl", str(holder.port), "tune", "433M")
        elapsed = time.monotonic() - t0
        self.assertEqual(code, 3)
        self.assertIn("server did not answer XACK within 3 s (another client may hold the "
                      "control port); command not sent - retry, or use --ack legacy to "
                      "queue it behind that client", err)
        self.assertGreaterEqual(elapsed, 2.9)
        self.assertEqual(holder.received(), b"XACK" + b"\x00" * 124)
        # never cached as legacy
        self.assertIsNone(AckModeCache().get("127.0.0.1:{}".format(holder.port)))

    def test_xack_false_command_not_sent(self):
        srv = self.server(FakeCtlServer(mode="extended", xack_reply={
            "xack": False, "error": "cluster_mismatch", "term": 3}))
        code, out, err = self.run_cli("--port-ctl", str(srv.port), "tune", "433M")
        self.assertEqual(code, 1)
        self.assertIn("cluster mismatch", err)
        self.assertEqual(srv.verbs, ["XACK"])

    def test_bad_xack_reply_exit_3(self):
        srv = self.server(FakeCtlServer(mode="extended", xack_reply={"what": 1}))
        code, _, err = self.run_cli("--port-ctl", str(srv.port), "agc")
        self.assertEqual(code, 3)
        self.assertIn("bad XACK reply", err)
        self.assertEqual(srv.verbs, ["XACK"])

    def test_timeout_chain(self):
        srv = self.server(FakeCtlServer(mode="extended", rto=10000, hmx=12000))
        client = CtlClient("127.0.0.1", srv.port, timeout=None, ack="xack")
        with client.open(mutating=True) as s:
            self.assertTrue(s.extended)
            self.assertAlmostEqual(s.command_timeout("FREQ"), 26.0)
            self.assertAlmostEqual(s.command_timeout("OQRY"), 5.0)
        legacy = CtlClient("127.0.0.1", srv.port, timeout=None, ack="legacy")
        with legacy.open(mutating=True) as s:
            self.assertAlmostEqual(s.command_timeout("FREQ"), 14.0)
        fixed = CtlClient("127.0.0.1", srv.port, timeout=7.0, ack="xack")
        with fixed.open(mutating=True) as s:
            self.assertEqual(s.command_timeout("FREQ"), 7.0)

    def test_verbose_reports_ack_mode_and_timeout(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        code, _, err = self.run_cli("-v", "--port-ctl", str(srv.port), "agc")
        self.assertEqual(code, 0)
        self.assertIn("ack mode auto (extended), command timeout 26.0 s", err)


class TestMutatingVerbs(CliTestBase):
    def test_tune_outcomes(self):
        cases = [
            ({"applied": True, "reason": "ok", "fsm": "IQ_CAL"}, 0, r"^applied \(fsm=IQ_CAL, \d+ ms\)$"),
            ({"applied": False, "reason": "not_confirmed", "fsm": "IQ_CAL"}, 1,
             r"^NOT applied: not_confirmed \(fsm=IQ_CAL\)$"),
            ({"applied": None, "reason": "held_for_burst", "fsm": "NOISE_CTR_WAIT"}, 0,
             r"^held until the calibration burst ends \(fsm=NOISE_CTR_WAIT\)$"),
            ({"applied": False, "reason": "deferred_timeout", "fsm": "NOISE_CTR_WAIT"}, 1,
             r"^NOT applied: deferred_timeout \(fsm=NOISE_CTR_WAIT\)$"),
        ]
        for ack, want_code, pattern in cases:
            srv = self.server(FakeCtlServer(mode="extended", acks={"FREQ": ack}))
            code, out, _ = self.run_cli("--port-ctl", str(srv.port), "tune", "433M")
            self.assertEqual(code, want_code, out)
            self.assertRegex(out.strip(), pattern)
            self.assertEqual(srv.verbs, ["XACK", "FREQ"])
            self.assertEqual(struct.unpack("<Q", srv.frames[1][2][:8])[0], 433000000)
        legacy = self.server(FakeCtlServer(mode="legacy"))
        code, out, _ = self.run_cli("--port-ctl", str(legacy.port), "tune", "433M")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "sent; server does not confirm application (legacy server)")

    def test_json_output(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--json", "park")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertTrue(doc["applied"])
        self.assertEqual(doc["reason"], "ok")

    def test_all_mutating_verbs_send_their_frames(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        p = str(srv.port)
        runs = [(["agc"], "AGC "), (["bearing", "10", "5"], "ORNT"), (["park"], "PARK"),
                (["scan", "start"], "SCAN"), (["scan", "stop"], "OSTP"),
                (["schedule", "stop"], "SCHS"), (["schedule", "next"], "SCHN"),
                (["lna-gain", "--unified", "14.5"], "EGAN"),
                (["bias-tee", "--all", "on"], "BIAS"), (["track-lock", "approve"], "TLKA")]
        for argv, verb in runs:
            code, out, err = self.run_cli("--port-ctl", p, *argv)
            self.assertEqual(code, 0, (argv, out, err))
            self.assertTrue(out.startswith("applied"), (argv, out))
            self.assertEqual(srv.verbs[-1], verb)
        # the EGAN frame carries tenths of dB on all 5 channels (nch from XACK)
        egan_frames = [p for _, v, p in srv.frames if v == "EGAN"]
        self.assertEqual(struct.unpack("<5I", egan_frames[0][:20]), (145,) * 5)
        bias_frames = [p for _, v, p in srv.frames if v == "BIAS"]
        self.assertEqual(struct.unpack("<5I", bias_frames[0][:20]), (1,) * 5)
        tlka = [p for _, v, p in srv.frames if v == "TLKA"]
        self.assertEqual(struct.unpack("<I", tlka[0][:4])[0], 1)

    def test_unified_gain_uses_xack_channel_count(self):
        srv = self.server(FakeCtlServer(mode="extended", nch=3))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "gain", "--unified", "297")
        self.assertEqual(code, 0)
        payload = srv.frames[-1][2]
        self.assertEqual(struct.unpack("<3I", payload[:12]), (297, 297, 297))
        self.assertEqual(payload[12:], b"\x00" * 112)

    def test_channel_count_mismatch_warns_and_sends(self):
        srv = self.server(FakeCtlServer(mode="extended", nch=5))
        code, out, err = self.run_cli("--port-ctl", str(srv.port), "gain", "100,200")
        self.assertEqual(code, 0)
        self.assertIn("warning: expected 5 values, got 2 (the server uses the first 5)", err)
        self.assertEqual(srv.verbs, ["XACK", "GAIN"])
        for verb, argv in (("EGAN", ["lna-gain", "1,2,3"]), ("BIAS", ["bias-tee", "1,0"])):
            code, out, err = self.run_cli("--port-ctl", str(srv.port), *argv)
            self.assertEqual(code, 0)
            self.assertIn("warning: expected 5 values", err)
            self.assertEqual(srv.verbs[-1], verb)

    def test_channel_count_mismatch_strict_refuses(self):
        srv = self.server(FakeCtlServer(mode="extended", nch=5))
        for argv in (["--strict", "gain", "100,200"], ["gain", "100,200", "--strict"],
                     ["--strict", "lna-gain", "1,2"], ["--strict", "bias-tee", "1"]):
            code, out, err = self.run_cli("--port-ctl", str(srv.port), *argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("expected 5 values, got", err)
        self.assertNotIn("GAIN", srv.verbs)
        self.assertNotIn("EGAN", srv.verbs)
        self.assertNotIn("BIAS", srv.verbs)
        # the right count passes --strict
        code, _, _ = self.run_cli("--port-ctl", str(srv.port), "--strict", "gain",
                                  "1,2,3,4,5")
        self.assertEqual(code, 0)
        self.assertEqual(srv.verbs[-1], "GAIN")

    def test_channel_count_from_extended_status_for_legacy_ctl(self):
        st = self.server(FakeStatusServer(responses={"STATUS": {
            "status_protocol": 2, "num_channels": 4, "sync_state": 6}}))
        srv = self.server(FakeCtlServer(mode="legacy"))
        code, out, err = self.run_cli("--port-ctl", str(srv.port), "--port-status",
                                      str(st.port), "gain", "1,2,3,4")
        self.assertEqual(code, 0)
        self.assertNotIn("warning", err)

    def test_negative_values_are_usage_errors(self):
        code, _, err = self.run_cli("--port-ctl", "1", "lna-gain", "1,-2")
        self.assertEqual(code, 2)
        code, _, err = self.run_cli("--port-ctl", "1", "gain", "abc")
        self.assertEqual(code, 2)

    def test_verify_tune(self):
        srv = self.server(FakeCtlServer(mode="extended"))  # HWSQ f = 433 MHz
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--verify", "tune", "433M")
        self.assertEqual(code, 0)
        self.assertIn("verified: frequency 433.000 MHz", out)
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--verify", "--timeout",
                                    "1", "tune", "868M")
        self.assertEqual(code, 1)
        self.assertIn("NOT verified: frequency 868.000 MHz", out)

    def test_verify_tune_without_hwsq_uses_status(self):
        st = self.server(FakeStatusServer(responses={"STATUS": {
            "current_frequency_hz": 433000000, "sync_state": 6}}))
        srv = self.server(FakeCtlServer(mode="legacy"))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--port-status",
                                    str(st.port), "--verify", "tune", "433M")
        self.assertEqual(code, 0)
        self.assertIn("verified", out)

    def test_verify_gain_needs_relay(self):
        st = self.server(FakeStatusServer())
        srv = self.server(FakeCtlServer(mode="extended"))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--port-status",
                                    str(st.port), "--verify", "gain", "--unified", "297")
        self.assertEqual(code, 0)
        self.assertIn("cannot verify gains on this server (needs the hwc relay)", out)
        st2 = self.server(FakeStatusServer(responses={"STATUS": {
            "status_protocol": 2, "hwc": {"gains_tenths": [297] * 5, "agc": 0}}}))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--port-status",
                                    str(st2.port), "--verify", "gain", "--unified", "297")
        self.assertEqual(code, 0)
        self.assertIn("verified: gains", out)
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--port-status",
                                    str(st2.port), "--verify", "--timeout", "1", "agc")
        self.assertEqual(code, 1)
        self.assertIn("NOT verified: AGC", out)

    def test_verify_orientation(self):
        srv = self.server(FakeCtlServer(mode="extended", json_replies={
            "OQRY": {"az": 10.0, "el": 5.0, "state": 2}}))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--verify",
                                    "bearing", "10", "5")
        self.assertEqual(code, 0)
        self.assertIn("verified: bearing", out)
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--verify", "--timeout",
                                    "1", "scan", "start")
        self.assertEqual(code, 1)


class TestRecal(CliTestBase):
    def test_default_legacy_server_sends_init_exit_0(self):
        srv = self.server(FakeCtlServer(mode="legacy"))
        code, out, err = self.run_cli("--port-ctl", str(srv.port), "recal")
        self.assertEqual(code, 0)
        self.assertEqual(srv.verbs, ["XACK", "INIT"])
        self.assertIn("lacks RECL: INIT only re-enters the hw_controller FSM", err)

    def test_extended_without_recl_cap_sends_init(self):
        srv = self.server(FakeCtlServer(mode="extended", caps="hwsq,schb,schg,tlck"))
        code, out, err = self.run_cli("--port-ctl", str(srv.port), "recal")
        self.assertEqual(code, 0)
        self.assertEqual(srv.verbs, ["XACK", "INIT"])
        self.assertIn("server has wire recalibration disabled or lacks RECL", err)
        self.assertTrue(out.startswith("applied"))

    def test_recl_disabled_falls_back_to_init_on_same_connection(self):
        srv = self.server(FakeCtlServer(mode="extended", caps="hwsq,schb,schg,tlck,recl",
                                        acks={"RECL": {"applied": False, "reason": "disabled",
                                                       "fsm": "IQ_CAL"}}))
        code, out, err = self.run_cli("--port-ctl", str(srv.port), "recal")
        self.assertEqual(code, 0)
        self.assertEqual(srv.verbs, ["XACK", "RECL", "INIT"])
        self.assertEqual({c for c, _, _ in srv.frames}, {1})
        self.assertIn("server has wire recalibration disabled", err)

    def test_recl_used_when_advertised(self):
        srv = self.server(FakeCtlServer(mode="extended", caps="hwsq,schb,schg,tlck,recl"))
        code, out, err = self.run_cli("--port-ctl", str(srv.port), "recal")
        self.assertEqual(code, 0)
        self.assertEqual(srv.verbs, ["XACK", "RECL"])
        self.assertEqual(err, "")
        self.assertRegex(out.strip(), r"^applied \(fsm=IQ_CAL, \d+ ms\)$")

    def test_verify_recal_needs_extended_status(self):
        st = self.server(FakeStatusServer())
        srv = self.server(FakeCtlServer(mode="extended"))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--port-status",
                                    str(st.port), "--verify", "recal")
        self.assertEqual(code, 0)
        self.assertIn("cannot verify recalibration on this server", out)
        states = iter([6, 3, 3, 6])
        st2 = self.server(FakeStatusServer(responses={"STATUS": lambda arg: {
            "status_protocol": 2, "sync_state": next(states, 6)}}))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--port-status",
                                    str(st2.port), "--verify", "recal")
        self.assertEqual(code, 0)
        self.assertIn("verified: recalibration", out)


class TestSchedule(CliTestBase):
    def _doc(self, n):
        return {"name": "t", "entries": [{"frequency": 400000000 + i * 1000,
                                          "dwell_frames": 10} for i in range(n)]}

    def test_inline_schedule(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        path = self.write("s.json", json.dumps(self._doc(1)))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "schedule", "load", path)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "applied: 1 entries (inline)")
        sent = srv.frames[-1][2].rstrip(b"\x00")
        self.assertEqual(json.loads(sent), self._doc(1))
        self.assertNotIn(b" ", sent)  # compact JSON

    def test_chunked_schedule_pipelined_with_crc(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        doc = self._doc(40)
        path = self.write("s.json", json.dumps(doc, indent=2))
        code, out, err = self.run_cli("--port-ctl", str(srv.port), "schedule", "load", path)
        self.assertEqual(code, 0, err)
        nbytes = len(json.dumps(doc, separators=(",", ":")))
        nchunks = (nbytes + 115) // 116
        self.assertEqual(out.strip(), "applied: 40 entries (chunked, {:d} chunks)".format(
            nchunks))
        self.assertEqual(srv.schedule, doc)
        self.assertEqual(srv.verbs[:2], ["XACK", "SCHB"])
        self.assertEqual(srv.verbs.count("SCHC"), nchunks)
        self.assertEqual(srv.verbs[-1], "SCHE")
        self.assertGreater(srv.schc_burst, 1, "SCHC frames must be pipelined")
        self.assertLessEqual(srv.schc_burst, 16)

    def test_large_schedule_refused_by_legacy_or_inline_only(self):
        doc = self._doc(40)
        path = self.write("s.json", json.dumps(doc))
        nbytes = len(json.dumps(doc, separators=(",", ":")))
        msg = ("schedule is {:d} bytes; this server accepts 124-byte inline schedules only "
               "(use --as-path for a file on the DAQ host)").format(nbytes)
        legacy = self.server(FakeCtlServer(mode="legacy"))
        code, out, err = self.run_cli("--port-ctl", str(legacy.port), "schedule", "load", path)
        self.assertEqual(code, 1)
        self.assertIn(msg, err)
        self.assertEqual(legacy.verbs, ["XACK"])
        ext = self.server(FakeCtlServer(mode="extended"))
        code, out, err = self.run_cli("--port-ctl", str(ext.port), "schedule", "load", path,
                                      "--inline-only")
        self.assertEqual(code, 1)
        self.assertIn(msg, err)
        self.assertEqual(ext.verbs, ["XACK"])

    def test_as_path(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "schedule", "load",
                                    "--as-path", "/opt/heimdall/sched.json")
        self.assertEqual(code, 0)
        self.assertEqual(srv.frames[-1][1], "SCHD")
        self.assertEqual(srv.frames[-1][2].rstrip(b"\x00"), b"/opt/heimdall/sched.json")

    def test_verify_schedule_by_schg(self):
        srv = self.server(FakeCtlServer(mode="extended", schg={
            "gen": 77, "loaded": True, "active": True, "st": "ACTIVE_DWELLING",
            "idx": 0, "total": 1}))
        doc = dict(self._doc(1), gen=77)
        path = self.write("s.json", json.dumps(doc))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "--verify", "schedule",
                                    "load", path)
        self.assertEqual(code, 0)
        self.assertIn("verified: schedule loaded (1 entries)", out)
        self.assertEqual(srv.verbs[-1], "SCHG")

    def test_schedule_query_unchanged(self):
        srv = self.server(FakeCtlServer(json_replies={"SCHQ": {"state": "IDLE",
                                                               "active": False}}))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "schedule", "query")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"state": "IDLE", "active": False})
        self.assertEqual(srv.verbs, ["SCHQ"])


class TestHwcQueries(CliTestBase):
    def test_hwc_status(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "hwc-status")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0].split(), ["fsm", "pend", "noise", "agc", "nch", "freq",
                                            "frame-age", "frames", "term"])
        self.assertIn("IQ_CAL", lines[1])
        self.assertIn("433.000 MHz", lines[1])
        self.assertEqual(srv.verbs, ["HWSQ"])
        legacy = self.server(FakeCtlServer(mode="legacy"))
        code, _, err = self.run_cli("--port-ctl", str(legacy.port), "hwc-status")
        self.assertEqual(code, 1)
        self.assertIn("does not answer HWSQ", err)

    def test_track_lock(self):
        srv = self.server(FakeCtlServer(mode="extended"))
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "track-lock")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "track lock: required=yes approved=no")
        code, out, _ = self.run_cli("--port-ctl", str(srv.port), "track-lock", "revoke")
        self.assertEqual(code, 0)
        self.assertEqual(struct.unpack("<I", srv.frames[-1][2][:4])[0], 0)
        legacy = self.server(FakeCtlServer(mode="legacy"))
        code, _, err = self.run_cli("--port-ctl", str(legacy.port), "track-lock", "approve")
        self.assertEqual(code, 1)
        self.assertIn("does not support TLKA", err)
        self.assertEqual(legacy.verbs, ["XACK"])
        # cached as legacy now: no XACK is sent, and TLKA is still refused
        code, out, err = self.run_cli("--port-ctl", str(legacy.port), "track-lock", "approve")
        self.assertEqual(code, 1)
        self.assertIn("does not support TLKA", err)
        self.assertEqual(legacy.verbs, ["XACK"])


class TestStatusServerCommands(CliTestBase):
    def test_events_error_reply_exit_1_in_text_mode(self):
        st = self.server(FakeStatusServer(responses={
            "EVENTS": {"error": "event bus not enabled"}}))
        code, out, err = self.run_cli("--port-status", str(st.port), "events")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Error: event bus not enabled", err)
        code, _, _ = self.run_cli("--port-status", str(st.port), "--json", "events")
        self.assertEqual(code, 1)

    def test_connection_failures_are_oserrors_for_library_callers(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(OSError) as cm:
            StatusClient("127.0.0.1", port, timeout=2.0).ping()
        self.assertIsInstance(cm.exception, ctl_mod.EndpointError)
        self.assertIn("127.0.0.1:{}".format(port), str(cm.exception))
        with self.assertRaises(socket.error):
            CtlClient("127.0.0.1", port, timeout=2.0).agc()

    def test_db_stats(self):
        st = self.server(FakeStatusServer(responses={"DB_STATS": {"records": 3}}))
        code, out, _ = self.run_cli("--port-status", str(st.port), "db-stats")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"records": 3})
        st2 = self.server(FakeStatusServer(responses={
            "DB_STATS": {"error": "database not enabled"}}))
        code, out, _ = self.run_cli("--port-status", str(st2.port), "db-stats")
        self.assertEqual(code, 1)

    def test_cal_history_remote_legacy_server(self):
        st = self.server(FakeStatusServer(responses={
            "CAL_HISTORY": {"cal_history": [{"ts": 1}]}}))
        code, out, err = self.run_cli("--port-status", str(st.port), "cal-history",
                                      "--remote", "--freq", "433M", "--since", "1h",
                                      "--limit", "5")
        self.assertEqual(code, 0)
        self.assertIn("ignored: server lacks the extended CAL_HISTORY grammar", err)
        self.assertEqual(st.commands, ["STATUS", "CAL_HISTORY 433000000"])
        self.assertEqual(json.loads(out.strip()), {"ts": 1})

    def test_cal_history_remote_extended_uses_server_clock(self):
        server_ts = time.time() + 3600.0  # server clock one hour ahead
        st = self.server(FakeStatusServer(responses={
            "STATUS": {"status_protocol": 2, "sync_state": 6},
            "PING": {"ok": True, "ts": server_ts},
            "CAL_HISTORY": {"cal_history": []}}))
        code, out, err = self.run_cli("--port-status", str(st.port), "cal-history",
                                      "--remote", "--since", "1h", "--limit", "7")
        self.assertEqual(code, 0, err)
        self.assertEqual(st.commands[:2], ["STATUS", "PING"])
        parts = st.commands[2].split()
        self.assertEqual(parts[0], "CAL_HISTORY")
        self.assertEqual(parts[1], "-")
        self.assertEqual(int(parts[2]), int(round((server_ts - 3600) * 1000)))
        self.assertEqual(parts[3], "7")
        # bare form when neither --since nor --limit is given
        self.run_cli("--port-status", str(st.port), "cal-history", "--remote")
        self.assertEqual(st.commands[-1], "CAL_HISTORY")

    def test_freq_scan_remote(self):
        st = self.server(FakeStatusServer(responses={"SCAN_SUMMARY": {"freq_scan": []}}))
        code, _, _ = self.run_cli("--port-status", str(st.port), "freq-scan", "--remote",
                                  "--freq", "868M")
        self.assertEqual(code, 0)
        self.assertEqual(st.commands, ["SCAN_SUMMARY 868000000"])

    def test_peers(self):
        st = self.server(FakeStatusServer(responses={"PEERS": {
            "error": "unknown command", "valid": ["PING"]}}))
        code, _, err = self.run_cli("--port-status", str(st.port), "peers")
        self.assertEqual(code, 1)
        self.assertIn("this instance does not serve PEERS (set [monitoring] "
                      "status_extended = 1)", err)
        st2 = self.server(FakeStatusServer(responses={"PEERS": {
            "instance_id": 0, "node_id": "hosta:0", "advisory_leader": 0,
            "peers": {"hostb:5102": {"node_key": "hostb:1", "health": "ok",
                                     "sync_state": 6, "frequency_hz": 433000000,
                                     "seen_age_sec": 0.4, "state": "up"}}}}))
        code, out, _ = self.run_cli("--port-status", str(st2.port), "peers")
        self.assertEqual(code, 0)
        self.assertIn("hostb:1", out)
        self.assertIn("433.000 MHz", out)
        self.assertIn("0.4s", out)

    def test_existing_status_output_unchanged(self):
        st = self.server(FakeStatusServer())
        code, out, _ = self.run_cli("--port-status", str(st.port), "status")
        self.assertEqual(code, 0)
        self.assertEqual(out, "Pipeline: ok  Sync: 6  Freq: 433.000 MHz  Uptime: 100s\n")


class TestLocalCommands(CliTestBase):
    def test_db_dir_default_follows_instance_subdir(self):
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read_string("[database]\ndb_dir = _db\ninstance_subdir = 1\n")
        base = os.path.join(self.tmp, "Firmware")
        cfg_path = os.path.join(base, "daq_chain_config.ini")
        self.assertEqual(default_db_dir(cfg, cfg_path, 3), os.path.join(base, "_db", "inst3"))
        self.assertEqual(default_db_dir(cfg, cfg_path, 0), os.path.join(base, "_db"))
        cfg.set("database", "instance_subdir", "0")
        self.assertEqual(default_db_dir(cfg, cfg_path, 3), os.path.join(base, "_db"))
        cfg.set("database", "db_dir", "/var/db")
        self.assertEqual(default_db_dir(cfg, cfg_path, 3), "/var/db")
        self.assertEqual(default_db_dir(None, None, 3), "_db")
        ini = self.write("fw/daq_chain_config.ini",
                         "[federation]\ninstance_id = 2\n[database]\ndb_dir = _db\n"
                         "instance_subdir = 1\n")
        code, _, err = self.run_cli("-v", "--config", ini, "cal-history")
        self.assertIn("local database {}".format(
            os.path.join(self.tmp, "fw", "_db", "inst2")), err)

    def test_instances(self):
        run_dir = os.path.join(self.tmp, "run")
        os.makedirs(run_dir)
        code, out, _ = self.run_cli("instances", "--run-dir", run_dir)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "no registry entries in {} (only instances started "
                         "with [host] instance_registry = 1 are listed)".format(run_dir))
        st = self.server(FakeStatusServer())
        entry = {"schema": 1, "instance_id": 1, "mode": "synthetic", "launcher": "manual",
                 "cwd": "/opt/heimdall/instances/1/Firmware", "started_at": 1761900000,
                 "ports": {"status": st.port},
                 "pids": {"python": os.getpid(), "rtl_daq": 2 ** 22 + 12345}}
        with open(os.path.join(run_dir, "inst1.json"), "w") as f:
            json.dump(entry, f)
        with open(os.path.join(run_dir, "inst2.json"), "w") as f:
            f.write("{torn")
        code, out, _ = self.run_cli("instances", "--run-dir", run_dir, "--ping")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("ping", lines[0])
        self.assertIn("synthetic", lines[1])
        self.assertIn("1/2", lines[1])
        self.assertRegex(lines[1], r"\d+\.\d ms$")

    def test_capacity_exit_mapping(self):
        fw = os.path.join(self.tmp, "fw")
        os.makedirs(fw)
        with open(os.path.join(fw, "daq_preflight.py"), "w") as f:
            f.write("import json, os, sys\n"
                    "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n"
                    "sys.exit(int(open(os.path.join(os.path.dirname(os.path.abspath("
                    "__file__)), 'code')).read()))\n")
        ini = self.write("a.ini", "[hw]\nnum_ch = 5\n")
        for tool_code, strict, want in ((0, False, 0), (1, False, 0), (2, False, 5),
                                        (1, True, 1), (0, True, 0), (7, False, 1)):
            with open(os.path.join(fw, "code"), "w") as f:
                f.write(str(tool_code))
            argv = ["--firmware-dir", fw, "capacity", "--config", ini, "--add-instances", "2",
                    "--json"] + (["--strict"] if strict else [])
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, want, (tool_code, strict, err))
            doc = json.loads(out)
            self.assertEqual(doc["argv"], ["--config", ini, "--add-instances", "2", "--json"])
            self.assertEqual(os.path.realpath(doc["cwd"]), os.path.realpath(fw))
        code, _, err = self.run_cli("--firmware-dir", self.tmp, "capacity")
        self.assertEqual(code, 3)
        self.assertIn("daq_preflight.py not found", err)


@unittest.skipUnless(HAVE_ZMQ, "pyzmq not installed")
class TestEventsSubscriber(CliTestBase):
    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _publisher(self, port):
        sys.path.insert(0, DAQ_CORE_DIR)
        from daq_events import ZMQPubHandler, DAQEvent
        pub = ZMQPubHandler(port=port, listen_address="127.0.0.1")
        self.addCleanup(pub.close)
        return pub, DAQEvent

    def test_multipart_loopback_and_subscribe_wrapper(self):
        from heimdall_ctl.client.events_sub import EventSubscriber, subscribe
        port = self._free_port()
        pub, DAQEvent = self._publisher(port)
        evt = DAQEvent(severity="warning", module="delay_sync", event_type="sync_lost",
                       payload={"cpi_index": 1234})
        with EventSubscriber([("127.0.0.1", port)], ["sync"]) as sub:
            got = []
            deadline = time.monotonic() + 10
            while not got and time.monotonic() < deadline:
                pub(evt)
                got = sub.recv(200)
            self.assertTrue(got, "no event received")
            endpoint, event = got[0]
            self.assertEqual(endpoint, "127.0.0.1:{}".format(port))
            self.assertEqual(event["event_type"], "sync_lost")
            self.assertEqual(event["payload"], {"cpi_index": 1234})
        # module-level subscribe() keeps its signature and yields dicts
        gen = subscribe("127.0.0.1", port, None)
        stop = threading.Event()

        def pump():
            while not stop.is_set():
                pub(evt)
                time.sleep(0.05)
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            first = next(gen)
        finally:
            stop.set()
            t.join(2)
            gen.close()
        self.assertEqual(first["event_type"], "sync_lost")

    def test_single_frame_fallback(self):
        import zmq
        from heimdall_ctl.client.events_sub import EventSubscriber, decode_frames
        self.assertEqual(decode_frames([b'heartbeat {"a": 1}']), {"a": 1})
        self.assertEqual(decode_frames([b"garbage"]), {"raw": "garbage"})
        port = self._free_port()
        ctx = zmq.Context()
        pub = ctx.socket(zmq.PUB)
        pub.bind("tcp://127.0.0.1:{}".format(port))
        try:
            with EventSubscriber(["127.0.0.1:{}".format(port)]) as sub:
                got = []
                deadline = time.monotonic() + 10
                while not got and time.monotonic() < deadline:
                    pub.send_string('heartbeat {"event_type": "heartbeat", "payload": {}}')
                    got = sub.recv(200)
            self.assertEqual(got[0][1]["event_type"], "heartbeat")
        finally:
            pub.close(linger=0)
            ctx.term()

    def test_cli_tail_line_format(self):
        port = self._free_port()
        pub, DAQEvent = self._publisher(port)
        evt = DAQEvent(severity="warning", module="delay_sync", event_type="sync_lost",
                       payload={"cpi_index": 1234})
        evt.timestamp = time.time() - 30.0
        stop = threading.Event()

        def pump():
            while not stop.is_set():
                pub(evt)
                time.sleep(0.05)
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            code, out, err = self.run_cli("--port-events", str(port), "--color", "never",
                                          "events", "--tail", "--count", "1",
                                          "--filter", "sync")
        finally:
            stop.set()
            t.join(2)
        self.assertEqual(code, 0, err)
        self.assertRegex(
            out.strip(),
            r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} \[127\.0\.0\.1:%d\] warning  "
            r"sync_lost        \{\"cpi_index\": 1234\} \(event time .*\)$" % port)

    def test_tail_endpoints_for_hosts_and_instances(self):
        args = cli.build_parser().parse_args(
            ["events", "--tail", "--hosts", "a,b", "--hosts", "c", "--instances", "0,2"])
        args._parser = None
        ctx = cli.Context(args)
        endpoints, instances = cli._tail_endpoints(ctx, args)
        self.assertEqual(instances, [0, 2])
        self.assertEqual(endpoints, [("a", 5003), ("a", 5203), ("b", 5003), ("b", 5203),
                                     ("c", 5003), ("c", 5203)])
        cfg_path = self.write("daq_chain_config.ini",
                              "[monitoring]\nevent_topic_prefix = 1\n")
        args = cli.build_parser().parse_args(["--config", cfg_path, "events", "--tail"])
        ctx = cli.Context(args)
        self.assertEqual(cli._tail_topics(ctx, ["sync"], [0, 1]),
                         ["sync", "inst0.sync", "inst1.sync"])
        self.assertIsNone(cli._tail_topics(ctx, None, [0]))


class TestEventDropCounter(unittest.TestCase):
    def test_drop_counter_increments(self):
        sys.path.insert(0, DAQ_CORE_DIR)
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
