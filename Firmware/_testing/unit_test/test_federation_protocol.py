"""
    Coordinator wire protocol v2 tests (design spec D.1-D.5, D.7)

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Hardware-free: FakeHWCServer speaks the :5001 frames (XACK, extended
    acks, HWSQ/SCHG/SCHQ, SCHB/SCHC/SCHE) or the legacy FNSD+zeros dialect;
    FakeStatusServer answers the :5002 line verbs. Every port is ephemeral and
    every timeout is shrunk through CoordinatorSettings.
"""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                "_daq_core"))

from federation_coordinator import (CoordinatorSettings, FederationCoordinator,  # noqa: E402
                                    build_from_args, ConfigError)
from federation_scheduler import FederationScheduler  # noqa: E402


def _zero_reply():
    return b"FNSD" + b"\x00" * 124


def _json_reply(obj):
    data = json.dumps(obj, separators=(",", ":")).encode()
    assert len(data) <= 124, data
    return b"FNSD" + data.ljust(124, b"\x00")


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


class FakeHWCServer:
    """Stand-in for hw_controller's CtrIfaceServer (extends test_federation's fixture).

    mode "extended" answers XACK; mode "legacy" answers FNSD+zeros to
    everything (including XACK). Every frame is recorded as (conn, verb, frame).
    """

    def __init__(self, mode="extended", nch=5, caps="hwsq,schb,schg,tlck", term=0,
                 rto=500, hmx=500, fp=437):
        self.mode = mode
        self.nch = nch
        self.caps = caps
        self.term = term
        self.rto = rto
        self.hmx = hmx
        self.fp = fp
        self.xack_mode = "ok"         # ok | silent | reset | cluster_mismatch | delay
        self.xack_delay = 0.0
        self.delays = {}              # verb -> seconds before replying
        self.replies = {}             # verb -> reply bytes (overrides)
        self.schg_override = None     # dict -> SCHG reply; "silent" -> never answer
        self.frames = []
        self.lock = threading.Lock()
        self.schedule = None          # last loaded document (dict)
        self.gen = None
        self.active = False
        self.sessions = {}
        self.schc_seen = 0
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self._stop = threading.Event()
        self._conn_id = 0
        threading.Thread(target=self._run, daemon=True).start()

    def verbs(self):
        with self.lock:
            return [v for _c, v, _f in self.frames]

    def command_verbs(self):
        return [v for v in self.verbs() if v != "XACK"]

    def _run(self):
        self.srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._conn_id += 1
            threading.Thread(target=self._serve, args=(conn, self._conn_id), daemon=True).start()

    def _serve(self, conn, cid):
        try:
            conn.settimeout(10.0)
            while not self._stop.is_set():
                frame = _recv_exact(conn, 128)
                if len(frame) < 128:
                    return
                verb = frame[:4].decode("ascii", errors="replace")
                with self.lock:
                    self.frames.append((cid, verb, frame))
                reply = self._handle(verb, frame)
                if reply is None:
                    if verb == "XACK" and self.xack_mode == "reset":
                        return
                    time.sleep(30)
                    return
                if verb != "XACK" and verb in self.delays:
                    time.sleep(self.delays[verb])
                conn.sendall(reply)
        except OSError:
            pass
        finally:
            conn.close()

    def _ack(self, applied=True, reason="ok", **extra):
        body = {"applied": applied, "reason": reason, "fsm": "IQ_CAL", "seq": len(self.frames)}
        body.update(extra)
        return _json_reply(body)

    def _handle(self, verb, frame):
        if verb in self.replies:
            return self.replies[verb]
        if self.mode == "legacy":
            if verb == "SCHD":
                self._load(frame[4:].rstrip(b"\x00").decode())
            elif verb == "SCHS":
                self.active = False
            elif verb == "SCHQ":
                if self.schedule is None or not self.active:
                    return _json_reply({"state": "IDLE", "active": False})
                return _json_reply({"state": "ACTIVE_DWELLING", "active": True, "idx": 0,
                                    "total": len(self.schedule["entries"]), "freq": 1,
                                    "frames": 0, "dwell": 1})
            return _zero_reply()
        if verb == "XACK":
            if self.xack_mode in ("silent", "reset"):
                return None
            if self.xack_mode == "delay":
                time.sleep(self.xack_delay)
            if self.xack_mode == "cluster_mismatch":
                return _json_reply({"xack": False, "error": "cluster_mismatch",
                                    "term": self.term})
            return _json_reply({"xack": True, "v": 1, "nch": self.nch, "term": self.term,
                                "caps": self.caps, "rto": self.rto, "fp": self.fp,
                                "hmx": self.hmx})
        if verb == "SCHD":
            try:
                self._load(frame[4:].rstrip(b"\x00").decode())
            except (OSError, ValueError):
                return self._ack(False, "parse_error")
            return self._ack()
        if verb == "SCHS":
            self.active = False
            self.gen = None
            return self._ack()
        if verb == "SCHG":
            if self.schg_override == "silent":
                return None
            if isinstance(self.schg_override, dict):
                return _json_reply(self.schg_override)
            return _json_reply({"gen": self.gen, "loaded": self.schedule is not None,
                                "active": self.active,
                                "st": "ACTIVE_DWELLING" if self.active else "IDLE", "idx": 0,
                                "total": len(self.schedule["entries"]) if self.schedule else 0})
        if verb == "SCHQ":
            if self.schedule is None or not self.active:
                return _json_reply({"state": "IDLE", "active": False})
            return _json_reply({"state": "ACTIVE_DWELLING", "active": True, "idx": 0,
                                "total": len(self.schedule["entries"]), "freq": 1,
                                "frames": 0, "dwell": 1})
        if verb == "HWSQ":
            return _json_reply({"fsm": "IQ_CAL", "pend": 0, "ns": 0, "agc": 0, "nch": self.nch,
                                "f": 433000000, "fage": 100, "fr": 1, "fc": 7, "t": self.term})
        if verb == "SCHB":
            total, crc, nchunks, _z = struct.unpack("<IIHH", frame[4:16])
            sess = len(self.sessions) + 1
            self.sessions[sess] = {"total": total, "crc": crc, "n": nchunks, "data": b"",
                                   "seq": 0}
            return _json_reply({"ok": True, "sess": sess})
        if verb == "SCHC":
            sess, seq, ln = struct.unpack("<HHH", frame[4:10])
            st = self.sessions.get(sess)
            if st is None:
                return _json_reply({"ok": False, "reason": "bad_session", "seq": seq})
            if seq != st["seq"]:
                return _json_reply({"ok": False, "reason": "out_of_order", "seq": seq})
            st["data"] += frame[10:10 + ln]
            st["seq"] += 1
            self.schc_seen += 1
            return _json_reply({"ok": True, "seq": seq})
        if verb == "SCHE":
            sess = struct.unpack("<H", frame[4:6])[0]
            st = self.sessions.pop(sess, None)
            if st is None or len(st["data"]) != st["total"]:
                return self._ack(False, "incomplete", entries=0)
            if zlib.crc32(st["data"]) & 0xFFFFFFFF != st["crc"]:
                return self._ack(False, "crc_mismatch", entries=0)
            self._load(st["data"].decode())
            return self._ack(entries=len(self.schedule["entries"]))
        if verb == "RECL":
            if "recl" not in self.caps:
                return self._ack(False, "disabled")
            return self._ack()
        return self._ack()

    def _load(self, text):
        text = text.strip()
        if not text.startswith("{"):
            with open(text) as f:
                text = f.read()
        doc = json.loads(text)
        self.schedule = doc
        self.gen = doc.get("gen")
        self.active = True

    def close(self):
        self._stop.set()
        try:
            self.srv.close()
        except OSError:
            pass


class FakeStatusServer:
    """:5002 line server: STATUS / PEERS / EVENTS / DB_STATS, one request per connection."""

    def __init__(self, status=None, delay=0.0):
        self.status = dict(status or {"sync_state": 6, "pipeline_health": "ok",
                                      "frame_count": 1, "instance_id": 0})
        self.delay = delay
        self.busy = False
        self.polls = 0
        self.lines = []
        self.events = []
        self.lock = threading.Lock()
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(32)
        self.port = self.srv.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(5.0)
            line = conn.recv(1024).decode().strip()
            cmd = line.split()[0].upper() if line else ""
            with self.lock:
                self.lines.append(line)
                if cmd == "STATUS":
                    self.polls += 1
            if self.delay:
                time.sleep(self.delay)
            if self.busy:
                resp = {"error": "busy"}
            elif cmd == "STATUS":
                resp = dict(self.status)
            elif cmd == "EVENTS":
                resp = {"events": list(self.events)}
            elif cmd == "DB_STATS":
                resp = {"error": "database not enabled"}
            elif cmd == "PEERS":
                resp = {"instance_id": self.status.get("instance_id", 0), "peers": {}}
            else:
                resp = {"error": "unknown command"}
            conn.sendall((json.dumps(resp) + "\n").encode())
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self._stop.set()
        try:
            self.srv.close()
        except OSError:
            pass


def member(iid, hwc=None, status=None, key=None, host="127.0.0.1"):
    """A legacy-membership instance dict whose ports point at fakes."""
    return {"host": host, "instance_id": iid,
            "node_key": key or "node{}:{}".format(iid, iid),
            "ports": {"ctl": hwc.port if hwc else 1, "status": status.port if status else 1,
                      "iq": None, "events": None, "zmq": None}}


def fast_settings(**kw):
    base = dict(xack_timeout_sec=0.5, hwc_request_timeout_sec=0.5, peer_connect_timeout_sec=0.5,
                status_timeout_sec=0.5, mutation_wait_sec=0.3, max_member_rto_sec=5.0,
                health_poll_interval=0.2, sync_push_timeout_sec=1.0, breaker_open_sec=1.0)
    base.update(kw)
    return CoordinatorSettings(**base)


def raw_request(port, data, timeout=10.0, shutdown=False):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        if shutdown:
            s.shutdown(socket.SHUT_WR)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    return out


def rj(port, line, body=None, token=None, timeout=15.0):
    prefix = "AUTH {} ".format(token) if token else ""
    if body is None:
        data = (prefix + line + "\n").encode()
    else:
        payload = json.dumps(body).encode() if not isinstance(body, bytes) else body
        data = "{}{} @{}\n".format(prefix, line, len(payload)).encode() + payload
    return json.loads(raw_request(port, data, timeout).decode())


def wait_for(pred, timeout=5.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


class CoordinatorCase(unittest.TestCase):
    """Starts coordinators and fakes and stops them after each test."""

    def setUp(self):
        self._cleanup = []

    def tearDown(self):
        for fn in reversed(self._cleanup):
            try:
                fn()
            except Exception:
                pass

    def hwc(self, **kw):
        f = FakeHWCServer(**kw)
        self._cleanup.append(f.close)
        return f

    def status(self, **kw):
        f = FakeStatusServer(**kw)
        self._cleanup.append(f.close)
        return f

    def coord(self, instances, settings=None, scheduler=True, start=True, **kw):
        c = FederationCoordinator(port=0, instances=instances, listen_address="127.0.0.1",
                                  settings=settings or fast_settings(), **kw)
        if scheduler and c.scheduler is None:
            c.scheduler = FederationScheduler(coordinator=c)
        if start:
            c.start()
            self._cleanup.append(c.close)
        return c

    def warm(self, c, timeout=5.0):
        c._ensure_poller()
        t0 = time.monotonic()
        self.assertTrue(wait_for(lambda: c._health_warm(), timeout),
                        "coordinator health cache did not warm up")
        return time.monotonic() - t0


SCHED = {"name": "site-scan", "repeat_mode": "loop",
         "entries": [{"frequency": 433000000, "gains": [297] * 5, "dwell_frames": 100},
                     {"frequency": 868000000, "dwell_time_sec": 2.0, "require_cal": False},
                     {"frequency": 915000000, "dwell_frames": 50},
                     {"frequency": 1090000000, "dwell_frames": 50}]}


class TestFraming(CoordinatorCase):

    def test_legacy_verbs_keep_framing(self):
        st = self.status()
        c = self.coord([member(0, status=st), member(1, status=st, key="node1:1")])
        ping = raw_request(c.port, b"PING")
        self.assertFalse(ping.endswith(b"\n"))
        self.assertTrue(json.loads(ping.decode())["ok"])
        status = raw_request(c.port, b"STATUS")          # no newline: answered at once
        self.assertFalse(status.endswith(b"\n"))
        doc = json.loads(status.decode())
        for key in ("federation_health", "instance_count", "healthy_count", "instances",
                    "timestamp", "unreachable_count", "role", "term", "node_keys",
                    "deadline_ms", "poll_duration_ms", "cached"):
            self.assertIn(key, doc)
        self.assertEqual(sorted(doc["instances"]), ["0", "1"])
        self.assertEqual(doc["node_keys"], {"0": "node0:0", "1": "node1:1"})
        self.assertEqual(doc["role"], "static")
        unknown = raw_request(c.port, b"FOOBAR\n")
        self.assertEqual(unknown, b'{"error": "Unknown command"}')

    def test_legacy_status_without_newline_is_immediate(self):
        c = self.coord([])
        t0 = time.monotonic()
        out = raw_request(c.port, b"STATUS")
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(json.loads(out.decode())["federation_health"], "unknown")

    def test_new_verbs_newline_and_ok(self):
        c = self.coord([])
        out = raw_request(c.port, b"VERSION\n")
        self.assertTrue(out.endswith(b"\n"))
        doc = json.loads(out.decode())
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["protocol"], 2)
        self.assertEqual(doc["role"], "static")
        self.assertEqual(doc["deadlines"]["mutation_ms"], int(round(
            (0.3 + 0.5 + 0.5 + 5.0 + 3.0) * 1000)))
        for verb in ("VERSION", "SCHEDULE", "ALL", "REGISTER", "STATUS"):
            self.assertIn(verb, doc["verbs"])
        bad = rj(c.port, "NODES FOO")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad_request")
        self.assertIn("usage", bad)

    def test_split_request_parses(self):
        c = self.coord([])
        with socket.create_connection(("127.0.0.1", c.port), timeout=5) as s:
            s.sendall(b"VERS")
            time.sleep(0.1)
            s.sendall(b"ION\n")
            data = s.recv(65536)
        self.assertTrue(json.loads(data.decode())["ok"])

    def test_inline_body_too_large(self):
        c = self.coord([])
        body = json.dumps({"entries": [{"frequency": 1000000 + i} for i in range(150)]})
        self.assertGreater(len(body), 3072)
        self.assertLess(len(body), 4000)
        out = json.loads(raw_request(c.port, ("SCHEDULE SET " + body + "\n").encode()).decode())
        self.assertEqual(out["error"], "body_too_large_use_@n")

    def test_body_framing_and_short_body(self):
        c = self.coord([])
        c._read_kw = {"body_timeout": 0.3}
        ok = rj(c.port, "SCHEDULE SET", body=SCHED)
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["entries"], 4)
        # announced 50 bytes, sends 10 and keeps the connection open
        with socket.create_connection(("127.0.0.1", c.port), timeout=5) as s:
            s.sendall(b"SCHEDULE SET @50\n" + b"{" * 10)
            t0 = time.monotonic()
            data = s.recv(65536)
        self.assertEqual(json.loads(data.decode())["error"], "timeout")
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_auth_required(self):
        st = self.status()
        c = self.coord([member(0, status=st)], settings=fast_settings(auth_token="s3cret"))
        self.assertEqual(rj(c.port, "VERSION")["error"], "auth_required")
        legacy = raw_request(c.port, b"STATUS")
        self.assertEqual(json.loads(legacy.decode())["error"], "auth_required")
        self.assertFalse(legacy.endswith(b"\n"))
        self.assertTrue(rj(c.port, "VERSION", token="s3cret")["ok"])
        self.assertIn("instances", rj(c.port, "STATUS", token="s3cret"))

    def test_instance_usage_reply(self):
        c = self.coord([])
        out = raw_request(c.port, b"INSTANCE abc FREQ 1")
        self.assertEqual(json.loads(out.decode()), {"error": "Usage: INSTANCE <id> <command>"})
        out = raw_request(c.port, b"INSTANCE 7 FREQ 1")
        self.assertEqual(json.loads(out.decode()), {"error": "Instance 7 not found"})

    def test_cli_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            ini = os.path.join(d, "federation.ini")
            with open(ini, "w") as f:
                f.write("[federation]\ncoordinator_port = 6100\npeer_list = hosta:1\n"
                        "status_timeout_sec = 1.5\n")
            c, _args = build_from_args(["--config", ini, "--port", "6200"])
            self.assertEqual(c.port, 6200)
            self.assertEqual(c.settings.status_timeout_sec, 1.5)
            self.assertEqual([m.node_key for m in c._members()], ["hosta:1"])
            c2, _ = build_from_args(["--config", ini])
            self.assertEqual(c2.port, 6100)
            c3, _ = build_from_args(["--config", ini, "--instances", "hostb:2,hostb:2"])
            self.assertEqual([m.node_key for m in c3._members()], ["hostb:2", "hostb:2"])
            with open(ini, "w") as f:
                f.write("[federation]\ncluster_members = hosta:0,hosta:0\n")
            with self.assertRaises(ConfigError):
                build_from_args(["--config", ini])
            with open(ini, "w") as f:
                f.write("[federation]\nfanout_workers = many\n")
            with self.assertRaises(ConfigError):
                build_from_args(["--config", ini])

    def test_status_live_vs_cached(self):
        st = self.status()
        c = self.coord([member(0, status=st)])
        rj(c.port, "STATUS")
        rj(c.port, "STATUS")
        self.assertEqual(st.polls, 2)
        c2 = self.coord([member(0, status=st)], settings=fast_settings(status_cache_sec=30.0))
        st.polls = 0
        first = rj(c2.port, "STATUS")
        second = rj(c2.port, "STATUS")
        self.assertEqual(st.polls, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        rj(c2.port, "STATUS FRESH")
        self.assertEqual(st.polls, 2)


class TestFanOut(CoordinatorCase):

    def test_xack_before_every_command_and_legacy_ack(self):
        legacy = self.hwc(mode="legacy")
        c = self.coord([member(0, legacy)])
        wait_for(lambda: c._ack_cache_get("node0:0") is not None, 3.0)
        legacy.frames.clear()
        for _ in range(2):
            out = rj(c.port, "FREQ 433000000")
            r = out["results"]["0"]
            self.assertTrue(r["ok"])
            self.assertIsNone(r["applied"])
            self.assertTrue(r["legacy_ack"])
            self.assertEqual(out["summary"]["unconfirmed"], 1)
            self.assertIn("by_node", out)
            self.assertIn("deadline_ms", out)
        self.assertEqual(legacy.verbs(), ["XACK", "FREQ", "XACK", "FREQ"])
        self.assertEqual(c._ack_cache_get("node0:0")["mode"], "legacy")

    def test_extended_ack_and_all_node_fanout(self):
        a, b = self.hwc(), self.hwc()
        c = self.coord([member(0, a), member(1, b)])
        out = rj(c.port, "ALL FREQ 433000000")
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(out["results"]), ["node0:0", "node1:1"])
        self.assertTrue(out["results"]["node0:0"]["applied"])
        self.assertEqual(out["results"]["node0:0"]["reason"], "ok")
        self.assertEqual(out["summary"]["applied"], 2)
        node = rj(c.port, "NODE node1:1 HWSQ")
        self.assertEqual(node["node"], "node1:1")
        self.assertEqual(node["result"]["data"]["fsm"], "IQ_CAL")
        fan = rj(c.port, "FANOUT node1:1 AGC")
        self.assertEqual(list(fan["results"]), ["node1:1"])
        self.assertEqual(rj(c.port, "NODE nope:9 AGC")["error"], "unknown_member")
        self.assertEqual(rj(c.port, "FANOUT node0:0,nope:9 AGC")["error"], "unknown_member")
        self.assertEqual(rj(c.port, "ALL BOGUS")["error"], "unknown_hwc_command")
        self.assertEqual(rj(c.port, "ALL SCHD {}")["error"], "use_schedule_set")

    def test_channel_count_guard(self):
        a = self.hwc(nch=5)
        c = self.coord([member(0, a)])
        out = rj(c.port, "GAIN 100,200,300")
        r = out["results"]["0"]
        self.assertFalse(r["ok"])
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "channel_count_mismatch")
        self.assertEqual((r["expected"], r["got"]), (5, 3))
        self.assertNotIn("GAIN", a.verbs())

    def test_xack_timeout_not_sent_not_cached_breaker(self):
        a = self.hwc()
        a.xack_mode = "silent"
        c = self.coord([member(0, a)], start=False)
        out = c._fan_out_command("FREQ", "433000000")
        r = out["results"]["0"]
        self.assertEqual((r["error"], r["phase"], r["applied"]), ("timeout", "xack", False))
        self.assertNotIn("FREQ", a.verbs())
        self.assertIsNone(c._ack_cache_get("node0:0"))
        self.assertEqual(c.breaker_failures("node0:0"), 1)

    def test_xack_reset(self):
        a = self.hwc()
        a.xack_mode = "reset"
        c = self.coord([member(0, a)], start=False)
        r = c._fan_out_command("FREQ", "1")["results"]["0"]
        self.assertEqual((r["error"], r["phase"]), ("reset", "xack"))

    def test_xack_false_never_sends(self):
        a = self.hwc()
        a.xack_mode = "cluster_mismatch"
        c = self.coord([member(0, a)], start=False)
        r = c._fan_out_command("FREQ", "1")["results"]["0"]
        self.assertEqual((r["reason"], r["phase"], r["applied"]),
                         ("cluster_mismatch", "xack", False))
        self.assertEqual(a.command_verbs(), [])

    def test_static_coordinator_ignores_member_term(self):
        a = self.hwc(term=9)
        c = self.coord([member(0, a)], start=False)
        r = c._fan_out_command("FREQ", "1")["results"]["0"]
        self.assertTrue(r["applied"])
        self.assertEqual(a.command_verbs(), ["FREQ"])

    def test_elected_coordinator_steps_down_on_member_term(self):
        a = self.hwc()
        with tempfile.TemporaryDirectory() as d:
            port = _free_port()
            s = fast_settings(coordinator_candidates="127.0.0.1:{}".format(port),
                              coordinator_self="127.0.0.1:{}".format(port), state_dir=d,
                              election_interval_sec=0.5, election_missed_polls=2,
                              election_settle_polls=4)
            c = FederationCoordinator(port=port, instances=[member(0, a)],
                                      listen_address="127.0.0.1", settings=s)
            c.start()
            self._cleanup.append(c.close)
            self.assertTrue(wait_for(lambda: c._role_term()[0] == "leader", 8.0))
            a.term = c._role_term()[1] + 50
            a.frames.clear()
            out = rj(c.port, "FREQ 1")
            r = out["results"]["0"]
            self.assertEqual((r["reason"], r["phase"], r["applied"]),
                             ("stale_term", "xack", False))
            self.assertEqual(a.command_verbs(), [])
            self.assertEqual(c._role_term(), ("follower", a.term))
            self.assertEqual(rj(c.port, "FREQ 1")["error"], "not_leader")
            # the next election self-heals above the member's stored term
            self.assertTrue(wait_for(lambda: c._role_term()[0] == "leader", 8.0))
            self.assertGreater(c._role_term()[1], a.term)

    def test_slow_target_and_mutation_lock_free(self):
        fast, slow = self.hwc(), self.hwc()
        slow.delays["FREQ"] = 3.0
        c = self.coord([member(0, fast), member(1, slow)])
        result = {}

        def first():
            result["a"] = rj(c.port, "FREQ 1")
        t = threading.Thread(target=first)
        t0 = time.monotonic()
        t.start()
        time.sleep(0.2)
        second = rj(c.port, "GAIN 1,2,3,4,5")   # accepted while the slow member is busy
        self.assertNotIn("busy", json.dumps(second.get("error", "")))
        t.join(10)
        r = result["a"]["results"]["1"]
        self.assertEqual((r["error"], r["phase"]), ("timeout", "command"))
        self.assertIsNone(r["applied"])
        self.assertTrue(result["a"]["results"]["0"]["applied"])
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_rto_exceeds_deadline(self):
        a = self.hwc(rto=60000, hmx=12000)
        c = self.coord([member(0, a)], settings=fast_settings(hwc_request_timeout_sec="auto",
                                                              max_member_rto_sec=30.0))
        version = rj(c.port, "VERSION")
        client_timeout = version["deadlines"]["mutation_ms"] / 1000.0 + 3.0
        t0 = time.monotonic()
        out = rj(c.port, "FREQ 1", timeout=client_timeout)
        self.assertLess(time.monotonic() - t0, client_timeout)
        r = out["results"]["0"]
        self.assertEqual(r["reason"], "rto_exceeds_deadline")
        self.assertFalse(r["applied"])
        self.assertEqual(a.command_verbs(), [])

    def test_xack_after_deadline_never_gets_command(self):
        a = self.hwc()
        a.xack_mode = "delay"
        a.xack_delay = 1.2
        s = fast_settings(xack_timeout_sec=5.0, max_member_rto_sec=5.0, mutation_wait_sec=0.0,
                          peer_connect_timeout_sec=0.1)
        c = self.coord([member(0, a)], settings=s, start=False)
        from federation_coordinator import _Req
        req = _Req("FREQ", 1.0, True)
        req.role, req.term = "static", 0
        out = c._fan_out_command("FREQ", "1", req=req)
        r = out["results"]["0"]
        self.assertEqual(r["error"], "timeout")
        self.assertFalse(r["applied"])
        time.sleep(1.5)
        self.assertEqual(a.command_verbs(), [])
        self.assertTrue(wait_for(lambda: c._registry.abandoned_workers() == 0, 5.0))

    def test_abandoned_worker_gauge_and_busy(self):
        from federation_common import fan_out
        a = self.hwc()
        c = self.coord([member(0, a)], settings=fast_settings(fanout_workers=1))
        release = threading.Event()
        for _ in range(3):
            fan_out([1], lambda t, ctx: release.wait(10), max_workers=1, deadline_sec=0.05,
                    key=lambda t: t, registry=c._registry)
        self.assertEqual(rj(c.port, "VERSION")["abandoned_workers"], 3)
        self.assertEqual(json.loads(raw_request(c.port, b"STATUS").decode()),
                         {"error": "busy", "retry_in_sec": 5})
        self.assertEqual(rj(c.port, "ALL AGC"), {"ok": False, "error": "busy", "retry_in_sec": 5})
        busy = json.loads(raw_request(c.port, b"FREQ 1").decode())
        self.assertEqual(busy["error"], "busy")
        self.assertEqual(a.command_verbs(), [])
        release.set()
        self.assertTrue(wait_for(lambda: c._registry.abandoned_workers() == 0, 5.0))

    def test_breaker_counts_read_timeouts(self):
        a = self.hwc()
        a.delays["FREQ"] = 1.5
        s = fast_settings(breaker_failures=2, breaker_open_sec=30.0)
        c = self.coord([member(0, a)], settings=s, start=False)
        for _ in range(2):
            r = c._fan_out_command("FREQ", "1")["results"]["0"]
            self.assertEqual(r["phase"], "command")
        r = c._fan_out_command("FREQ", "1")["results"]["0"]
        self.assertEqual(r["reason"], "breaker_open")
        self.assertFalse(r["applied"])

    def test_dns_failure(self):
        a = self.hwc()
        inst = member(0, a, host="no-such-host.invalid")
        c = self.coord([inst])
        t0 = time.monotonic()
        out = rj(c.port, "FREQ 1")
        r = out["results"]["0"]
        self.assertEqual((r["error_kind"], r["phase"]), ("dns", "connect"))
        self.assertLess(time.monotonic() - t0, out["deadline_ms"] / 1000.0)

    def test_recal_fallback(self):
        ext = self.hwc(caps="hwsq,schb,schg,tlck,recl")
        nore = self.hwc()
        legacy = self.hwc(mode="legacy")
        c = self.coord([member(0, ext), member(1, nore), member(2, legacy)])
        out = rj(c.port, "RECAL")
        self.assertTrue(out["results"]["node0:0"]["applied"])
        self.assertNotIn("fallback", out["results"]["node0:0"])
        self.assertEqual(out["results"]["node1:1"]["fallback"], "INIT")
        self.assertEqual(out["results"]["node2:2"]["fallback"], "INIT")
        self.assertIn("INIT", nore.command_verbs())
        self.assertNotIn("RECL", nore.command_verbs())
        single = rj(c.port, "RECAL node0:0")
        self.assertEqual(single["node"], "node0:0")

    def test_handler_pool_overflow(self):
        c = self.coord([], settings=fast_settings(max_handlers=2, reserved_handlers=2))
        holders = [socket.create_connection(("127.0.0.1", c.port)) for _ in range(2)]
        try:
            time.sleep(0.3)
            out = raw_request(c.port, b"STATUS")
            self.assertEqual(json.loads(out.decode()), {"error": "busy", "retry_in_sec": 1})
            leader = rj(c.port, "LEADER")
            self.assertTrue(leader["ok"])
            self.assertEqual(leader["role"], "static")
        finally:
            for h in holders:
                h.close()

    def test_single_flight_status(self):
        st = self.status(delay=0.5)
        c = self.coord([member(0, status=st)])
        outs = []
        threads = [threading.Thread(target=lambda: outs.append(rj(c.port, "STATUS FRESH")))
                   for _ in range(2)]
        for t in threads:
            t.start()
            time.sleep(0.05)
        for t in threads:
            t.join(10)
        self.assertEqual(len(outs), 2)
        self.assertEqual(st.polls, 1)

    def test_status_aggregation_and_last_wins(self):
        good = self.status(status={"sync_state": 6, "pipeline_health": "ok", "frame_count": 3,
                                   "instance_id": 0, "tag": "second"})
        first = self.status(status={"sync_state": 6, "pipeline_health": "ok", "frame_count": 3,
                                    "instance_id": 0, "tag": "first"})
        closed = _free_port()
        insts = [member(0, status=first, key="hosta:0"), member(0, status=good, key="hostb:0"),
                 {"host": "127.0.0.1", "instance_id": 1, "node_key": "hostc:1",
                  "ports": {"ctl": closed, "status": closed}}]
        c = self.coord(insts)
        out = rj(c.port, "STATUS")
        self.assertEqual(out["federation_health"], "error")
        self.assertEqual(out["unreachable_count"], 1)
        self.assertEqual(out["unreachable"], ["1"])
        self.assertEqual(out["instances"]["0"]["tag"], "second")   # last member per id wins
        self.assertEqual(out["instance_count"], 3)
        self.assertEqual(out["key_collisions"][0]["node_keys"], ["hosta:0", "hostb:0"])

    def test_ambiguous_instance_only_with_cluster_members(self):
        c = self.coord([], settings=fast_settings(cluster_members="hosta:0,hostb:0"),
                       start=False)
        out = c._dispatch(_raw("INSTANCE 0 FREQ 1"), None)
        self.assertEqual(out, {"error": "ambiguous instance_id",
                               "candidates": ["hosta:0", "hostb:0"]})
        a = self.hwc()
        b = self.hwc()
        legacy = self.coord([member(0, a, key="hosta:0"), member(0, b, key="hostb:0")],
                            start=False)
        r = legacy._dispatch(_raw("INSTANCE 0 FREQ 1"), None)
        self.assertTrue(r["applied"])
        self.assertEqual(r["node_key"], "hosta:0")
        self.assertEqual(a.command_verbs(), ["FREQ"])
        self.assertEqual(b.command_verbs(), [])


class TestScheduling(CoordinatorCase):

    def _two_extended(self, **kw):
        a, b = self.hwc(**kw), self.hwc(**kw)
        sa, sb = self.status(), self.status()
        c = self.coord([member(0, a, sa), member(1, b, sb)])
        return c, a, b

    def test_cold_rebalance(self):
        c = self.coord([])
        out = json.loads(raw_request(c.port, b"REBALANCE").decode())
        self.assertEqual(out, {"error": "no master schedule"})

    def test_set_rebalance_assignments_chunked(self):
        c, a, b = self._two_extended()
        self.assertTrue(rj(c.port, "SCHEDULE SET round_robin", body=SCHED)["ok"])
        warming = json.loads(raw_request(c.port, b"REBALANCE").decode())
        seq = 1
        if "error" in warming:
            self.assertEqual(warming["error"], "health_warming")
        else:
            seq = 2
        self.warm(c)
        out = json.loads(raw_request(c.port, b"REBALANCE").decode())
        self.assertEqual(out["status"], "rebalanced", out)
        self.assertEqual(out["instances"], 2)
        s0 = out["by_node"]["node0:0"]
        self.assertTrue(s0["verified"], s0)
        self.assertEqual(s0["transfer"], "chunked")
        self.assertEqual(s0["entries"], 2)
        self.assertEqual(a.gen, s0["gen"])
        self.assertGreater(a.schc_seen, 1)
        self.assertEqual(a.schedule["name"], "site-scan@node0:0")
        self.assertIn("SCHB", a.command_verbs())
        assignments = rj(c.port, "ASSIGNMENTS")
        rec = assignments["assignments"]["node0:0"]
        self.assertEqual(rec["state"], "applied")
        self.assertEqual(rec["gen"], s0["gen"])
        self.assertEqual([e["frequency"] for e in rec["entries"]], [433000000, 915000000])
        self.assertEqual(assignments["gen_seq"], seq)
        # SET/GET/per-target round trip keeps dwell_time_sec, require_cal, repeat_mode
        got = rj(c.port, "SCHEDULE GET")
        self.assertEqual(got["schedule"]["entries"][1],
                         {"frequency": 868000000, "dwell_time_sec": 2.0, "require_cal": False})
        self.assertEqual(b.schedule["entries"][0]["dwell_time_sec"], 2.0)
        self.assertIs(b.schedule["entries"][0]["require_cal"], False)

    def test_legacy_targets_inline_and_path(self):
        small = {"name": "s", "entries": [{"frequency": 433000000}, {"frequency": 868000000}]}
        a = self.hwc(mode="legacy")
        sa = self.status()
        c = self.coord([member(0, a, sa)])
        rj(c.port, "SCHEDULE SET", body=small)
        self.warm(c)
        out = json.loads(raw_request(c.port, b"REBALANCE").decode())
        s0 = out["by_node"]["node0:0"]
        self.assertEqual(s0["transfer"], "inline")
        self.assertTrue(s0["legacy_ack"])
        self.assertTrue(s0["verified"])
        self.assertEqual(a.schedule["name"], "federation_inst0")
        self.assertNotIn("gen", a.schedule)
        rj(c.port, "SCHEDULE SET", body=SCHED)
        out = json.loads(raw_request(c.port, b"REBALANCE").decode())
        s0 = out["by_node"]["node0:0"]
        self.assertEqual(s0["transfer"], "path")
        self.assertTrue(s0["verified"])
        path = [f[4:].rstrip(b"\x00").decode() for _c, v, f in a.frames if v == "SCHD"][-1]
        self.assertFalse(os.path.exists(path))      # deleted after a verified load

    def test_remote_legacy_target_refused(self):
        sched = FederationScheduler()
        text = "x" * 200
        self.assertIsNone(sched.choose_transfer(text, (), "192.0.2.10"))
        self.assertEqual(sched.choose_transfer(text, (), "127.0.0.1"), "path")
        self.assertEqual(sched.choose_transfer(text, ("schb",), "192.0.2.10"), "chunked")
        self.assertEqual(sched.choose_transfer("x" * 124, (), "192.0.2.10"), "inline")

    def test_empty_and_inactive_get_schs(self):
        a, b, d = self.hwc(), self.hwc(), self.hwc()
        sa, sb = self.status(), self.status()
        sd = self.status(status={"sync_state": 3, "pipeline_health": "degraded",
                                 "frame_count": 1})
        c = self.coord([member(0, a, sa), member(1, b, sb), member(2, d, sd)])
        rj(c.port, "SCHEDULE SET", body={"name": "one", "entries": [{"frequency": 433000000}]})
        self.warm(c)
        out = json.loads(raw_request(c.port, b"REBALANCE").decode())
        self.assertEqual(out["empty"], ["node1:1"])
        self.assertEqual(out["skipped"], ["node2:2"])
        self.assertEqual(sorted(out["inactive"]), ["node1:1", "node2:2"])
        self.assertIn("SCHS", b.command_verbs())
        self.assertIn("SCHS", d.command_verbs())
        self.assertNotIn("SCHS", a.command_verbs())
        incl = json.loads(raw_request(c.port, b"REBALANCE INCLUDE_DEGRADED").decode())
        self.assertEqual(incl["instances"], 3)

    def test_schedule_set_too_large(self):
        c = self.coord([])
        big = {"name": "big", "entries": [{"frequency": 100000000 + i, "dwell_frames": 100,
                                           "gains": [100] * 31} for i in range(2000)]}
        out = rj(c.port, "SCHEDULE SET", body=big)
        self.assertEqual(out["error"], "bad_schedule")
        self.assertIn("per-target limit is 262144", out["message"])
        self.assertEqual(rj(c.port, "SCHEDULE SET", body={"entries": []})["error"],
                         "bad_schedule")

    def test_concurrent_rebalance_one_busy(self):
        a = self.hwc()
        a.delays["SCHE"] = 1.0
        sa = self.status()
        s = fast_settings(hwc_request_timeout_sec=3.0, mutation_wait_sec=0.2)
        c = self.coord([member(0, a, sa)], settings=s)
        rj(c.port, "SCHEDULE SET", body=SCHED)
        self.warm(c)
        outs = []

        def go():
            outs.append(json.loads(raw_request(c.port, b"REBALANCE").decode()))
        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
            time.sleep(0.1)
        for t in threads:
            t.join(15)
        errors = [o for o in outs if o.get("error") == "busy"]
        done = [o for o in outs if o.get("status") == "rebalanced"]
        self.assertEqual((len(errors), len(done)), (1, 1), outs)
        gen = done[0]["by_node"]["node0:0"]["gen"]
        self.assertEqual(rj(c.port, "ASSIGNMENTS")["assignments"]["node0:0"]["gen"], gen)
        self.assertEqual(a.gen, gen)

    def test_schedule_clear(self):
        c, a, b = self._two_extended()
        rj(c.port, "SCHEDULE SET", body=SCHED)
        self.warm(c)
        raw_request(c.port, b"REBALANCE")
        out = rj(c.port, "SCHEDULE CLEAR")
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(out["stopped"]), ["node0:0", "node1:1"])
        self.assertEqual(out["replicated"], {"stored": 1, "of": 1})
        self.assertFalse(a.active)
        self.assertEqual(rj(c.port, "SCHEDULE GET")["error"], "no_master_schedule")
        rec = rj(c.port, "ASSIGNMENTS")["assignments"]["node0:0"]
        self.assertEqual(rec["desired"], "inactive")

    def test_reconciliation_rules(self):
        a, b = self.hwc(), self.hwc()
        sa, sb = self.status(), self.status()
        s = fast_settings(reconcile_assignments=True, reconcile_interval_sec=3600,
                          reconcile_max_attempts=2)
        c = self.coord([member(0, a, sa), member(1, b, sb)], settings=s)
        rj(c.port, "SCHEDULE SET", body=SCHED)
        self.warm(c)
        out = json.loads(raw_request(c.port, b"REBALANCE").decode())
        gen0 = out["by_node"]["node0:0"]["gen"]
        self.assertEqual(c.reconcile_round(), {"node0:0": "ok", "node1:1": "ok"})
        # SCHG timeout -> skip, no re-send
        a.schg_override = "silent"
        n_before = a.command_verbs().count("SCHE")
        self.assertEqual(c.reconcile_round()["node0:0"], "skip")
        self.assertEqual(a.command_verbs().count("SCHE"), n_before)
        # foreign gen -> overridden, no re-send
        a.schg_override = {"gen": 12345, "loaded": True, "active": True, "st": "IDLE",
                           "idx": 0, "total": 1}
        self.assertEqual(c.reconcile_round()["node0:0"], "overridden")
        self.assertEqual(a.command_verbs().count("SCHE"), n_before)
        self.assertEqual(rj(c.port, "ASSIGNMENTS")["assignments"]["node0:0"]["state"],
                         "overridden")
        self.assertNotIn("node0:0", c.reconcile_round())
        # reconcile_override = 1 -> re-send
        c.settings.reconcile_override = True
        a.schg_override = None
        a.gen = 12345
        self.assertTrue(c.reconcile_round()["node0:0"].startswith("resent"))
        self.assertEqual(a.gen, gen0)
        c.settings.reconcile_override = False
        # a stale gen this leader issued earlier -> re-send; persistent failure -> failed
        b.gen = gen0
        b.replies["SCHE"] = _json_reply({"applied": False, "reason": "parse_error"})
        b.replies["SCHD"] = _json_reply({"applied": False, "reason": "parse_error"})
        self.assertTrue(c.reconcile_round()["node1:1"].startswith("resend_failed"))
        with c._mutation_lock:
            c._assignments["node1:1"]["next_try_mono"] = 0.0
        c.reconcile_round()
        rec = rj(c.port, "ASSIGNMENTS")["assignments"]["node1:1"]
        self.assertEqual(rec["state"], "failed")
        self.assertEqual(rec["attempts"], 2)
        self.assertIn("schedule_reconcile_failed",
                      [e["event_type"] for e in c._local_events])

    def test_reconciliation_legacy_member(self):
        a = self.hwc(mode="legacy")
        sa = self.status()
        s = fast_settings(reconcile_assignments=True, reconcile_interval_sec=3600)
        c = self.coord([member(0, a, sa)], settings=s)
        rj(c.port, "SCHEDULE SET", body={"name": "one", "repeat_mode": "once",
                                         "entries": [{"frequency": 433000000}]})
        self.warm(c)
        raw_request(c.port, b"REBALANCE")
        sa.status["frame_count"] = 50
        c._poller.poll_round()
        c.reconcile_round()
        a.active = False            # a finished one-shot schedule reads as inactive
        loads = a.command_verbs().count("SCHD")
        sa.status["frame_count"] = 60
        c._poller.poll_round()
        self.assertEqual(c.reconcile_round()["node0:0"], "skip")
        self.assertEqual(a.command_verbs().count("SCHD"), loads)
        sa.status["frame_count"] = 2            # restarted member
        c._poller.poll_round()
        self.assertEqual(c.reconcile_round()["node0:0"], "resent:restart")
        self.assertEqual(a.command_verbs().count("SCHD"), loads + 1)

    def test_health_warming(self):
        sa = self.status(delay=0.6)
        c = self.coord([member(0, status=sa)],
                       settings=fast_settings(health_poll_interval=5.0, status_timeout_sec=2.0))
        rj(c.port, "SCHEDULE SET", body=SCHED)
        out = json.loads(raw_request(c.port, b"REBALANCE").decode())
        self.assertEqual(out["error"], "health_warming")
        self.assertIn("retry_in_sec", out)


class TestMembershipVerbs(CoordinatorCase):

    def test_members_add_remove_idempotent(self):
        c = self.coord([])
        add = rj(c.port, "MEMBER ADD hostz:3@site-z")
        self.assertTrue(add["ok"])
        self.assertTrue(add["changed"])
        self.assertEqual(add["replicated"], {"stored": 1, "of": 1})
        again = rj(c.port, "MEMBER ADD hostz:3@site-z")
        self.assertFalse(again["changed"])
        self.assertEqual(rj(c.port, "MEMBER ADD hostz:3@other")["error"], "duplicate_member")
        rows = rj(c.port, "MEMBERS")["members"]
        self.assertEqual([(r["node_key"], r["source"]) for r in rows], [("hostz:3", "runtime")])
        self.assertEqual(rj(c.port, "MEMBER REMOVE hostz:3")["changed"], True)
        self.assertEqual(rj(c.port, "MEMBER REMOVE hostz:3")["error"], "unknown_member")
        self.assertEqual(rj(c.port, "MEMBER ADD nonsense")["error"], "bad_request")

    def test_health_nodes_events_peers_db_router(self):
        sa = self.status()
        sa.events = [{"timestamp": 1.0, "event_type": "sync_lock", "severity": "info"}]
        c = self.coord([member(0, status=sa)])
        c._ensure_poller()
        wait_for(lambda: c._poller.rounds_completed() > 0, 5.0)
        health = rj(c.port, "HEALTH")
        self.assertEqual(health["overall"], "ok")
        self.assertEqual(health["members"]["node0:0"]["state"], "up")
        nodes = rj(c.port, "NODES STATUS")
        self.assertTrue(nodes["cached"])
        self.assertEqual(nodes["nodes"]["node0:0"]["pipeline_health"], "ok")
        fresh = rj(c.port, "NODES STATUS FRESH")
        self.assertFalse(fresh["cached"])
        events = rj(c.port, "EVENTS 10 age:60")
        self.assertEqual(events["events"][0]["node_key"], "node0:0")
        self.assertIn("rx_ts", events["events"][0])
        self.assertEqual(rj(c.port, "EVENTS age:x")["error"], "bad_request")
        self.assertIn("node0:0", rj(c.port, "PEERS")["peers"])
        self.assertEqual(rj(c.port, "DB STATS")["nodes"]["node0:0"],
                         {"error": "database not enabled"})
        self.assertEqual(rj(c.port, "ROUTER STATS")["error"], "router_not_configured")


class TestPersistence(CoordinatorCase):

    def test_static_persistence_only_with_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.coord([], settings=fast_settings(state_dir=d))
            self.assertTrue(rj(c.port, "SCHEDULE SET range", body=SCHED)["ok"])
            self.assertTrue(rj(c.port, "MEMBER ADD hostz:3")["ok"])
            self.assertTrue(rj(c.port, "SCHEDULE STRATEGY round_robin")["ok"])
            c.close()
            self.assertEqual(sorted(os.listdir(d)),
                             ["assignments.json", "master_schedule.json", "members_runtime.json"])
            c2 = self.coord([], settings=fast_settings(state_dir=d))
            got = rj(c2.port, "SCHEDULE GET")
            self.assertEqual(got["version"], 3)
            self.assertEqual(got["schedule"]["strategy"], "round_robin")
            self.assertEqual(got["schedule"]["entries"], SCHED["entries"])
            self.assertEqual([m["node_key"] for m in rj(c2.port, "MEMBERS")["members"]],
                             ["hostz:3"])
        c3 = self.coord([])
        self.assertTrue(rj(c3.port, "SCHEDULE SET", body=SCHED)["ok"])
        c3.close()
        c4 = self.coord([])
        self.assertEqual(rj(c4.port, "SCHEDULE GET")["error"], "no_master_schedule")
        self.assertEqual(json.loads(raw_request(c4.port, b"REBALANCE").decode()),
                         {"error": "no master schedule"})
        self.assertEqual(rj(c4.port, "SCHEDULE STRATEGY range")["error"], "no_master_schedule")


class TestReviewRegressions(CoordinatorCase):
    """Default-path and concurrency regressions found in review."""

    def _pin_addresses(self, c, host, addrs):
        """Make the resolver cache return *addrs* for *host* (IPv6 first)."""
        with c._resolver._lock:
            c._resolver._cache[host] = {"addrs": list(addrs), "ok": time.monotonic() + 3600,
                                        "tried": time.monotonic()}

    def test_hostname_member_tries_every_address(self):
        # 'localhost' commonly resolves to ::1 first while hw_controller and the
        # status server listen on IPv4 only: every cached address must be tried
        a = self.hwc()
        sa = self.status()
        c = self.coord([member(0, a, sa, host="localhost")], start=False)
        self._pin_addresses(c, "localhost", ["::1", "127.0.0.1"])
        c.start()
        self._cleanup.append(c.close)
        r = rj(c.port, "FREQ 433000000")["results"]["0"]
        self.assertTrue(r["ok"], r)
        self.assertIn("FREQ", a.command_verbs())
        st = json.loads(raw_request(c.port, b"STATUS").decode())
        self.assertNotIn("error", st["instances"]["0"], st)
        self.assertEqual(c._addr_pref.get("localhost"), "127.0.0.1")
        c._ensure_poller()
        self.assertTrue(wait_for(lambda: (c._poller.get_member_table().get("node0:0") or {})
                                 .get("state") == "up", 5.0),
                        c._poller.get_member_table())

    def test_unpinned_localhost_member(self):
        a = self.hwc()
        c = self.coord([member(0, a, host="localhost")])
        r = rj(c.port, "FREQ 433000000")["results"]["0"]
        self.assertTrue(r["ok"], r)

    def test_breaker_single_probe(self):
        a = self.hwc()
        s = fast_settings(breaker_failures=1, breaker_open_sec=0.2)
        c = self.coord([member(0, a)], settings=s, start=False)
        c._breaker_record("node0:0", False)
        time.sleep(0.25)
        blocked, probe = c._breaker_check("node0:0")          # this caller is the probe
        self.assertEqual((blocked, probe), (None, True))
        for _ in range(2):
            r = c._fan_out_command("FREQ", "1")["results"]["0"]
            self.assertEqual(r["reason"], "breaker_open")    # a refused session never
        self.assertNotIn("FREQ", a.command_verbs())           # clears the probe flag
        c._breaker_record("node0:0", True, probe)
        r = c._fan_out_command("FREQ", "1")["results"]["0"]
        self.assertTrue(r["ok"], r)

    def _reconciled_pair(self, **kw):
        a, b = self.hwc(), self.hwc()
        sa, sb = self.status(), self.status()
        sa.status["frame_count"] = sb.status["frame_count"] = 50
        s = fast_settings(reconcile_assignments=True, reconcile_interval_sec=3600, **kw)
        c = self.coord([member(0, a, sa), member(1, b, sb)], settings=s)
        rj(c.port, "SCHEDULE SET", body=SCHED)
        self.warm(c)
        out = json.loads(raw_request(c.port, b"REBALANCE").decode())
        self.assertEqual(out.get("status"), "rebalanced", out)
        c._poller.poll_round()
        self.assertEqual(c.reconcile_round(), {"node0:0": "ok", "node1:1": "ok"})
        return c, a, b, sa, sb, out

    def test_reconcile_operator_schs_is_overridden(self):
        c, a, b, sa, sb, out = self._reconciled_pair()
        loads = a.command_verbs().count("SCHE") + a.command_verbs().count("SCHD")
        # heimdall-ctl SCHS: hw_controller clears the schedule (gen null, unloaded)
        a.schedule, a.gen, a.active = None, None, False
        sa.status["frame_count"] = 60
        c._poller.poll_round()
        self.assertEqual(c.reconcile_round()["node0:0"], "overridden")
        self.assertEqual(a.command_verbs().count("SCHE") + a.command_verbs().count("SCHD"),
                         loads)
        self.assertEqual(rj(c.port, "ASSIGNMENTS")["assignments"]["node0:0"]["state"],
                         "overridden")
        self.assertEqual([e["event_type"] for e in c._local_events].count(
            "schedule_overridden"), 1)
        self.assertNotIn("node0:0", c.reconcile_round())
        self.assertIsNone(a.gen)

    def test_reconcile_restart_and_unconfirmed_load_resent(self):
        c, a, b, sa, sb, out = self._reconciled_pair()
        gen_b = out["by_node"]["node1:1"]["gen"]
        # member restart: frame_count went down and the schedule is gone
        b.schedule, b.gen, b.active = None, None, False
        sb.status["frame_count"] = 3
        c._poller.poll_round()
        self.assertEqual(c.reconcile_round()["node1:1"], "resent:restart")
        self.assertEqual(b.gen, gen_b)
        # this coordinator's own load was never confirmed -> repaired
        gen_a = out["by_node"]["node0:0"]["gen"]
        a.schedule, a.gen, a.active = None, None, False
        with c._mutation_lock:
            c._assignments["node0:0"]["state"] = "pending"
        self.assertEqual(c.reconcile_round()["node0:0"], "resent:missing")
        self.assertEqual(a.gen, gen_a)

    def test_reconcile_round_never_makes_operators_busy(self):
        c, a, b, sa, sb, first = self._reconciled_pair(hwc_request_timeout_sec=5.0,
                                                       max_member_rto_sec=10.0)
        for verb, line in (("REBALANCE", None), ("CLEAR", "SCHEDULE CLEAR")):
            n_schg = a.verbs().count("SCHG")
            a.delays["SCHG"] = 1.5                 # a calibration stall on SCHG
            t = threading.Thread(target=c.reconcile_round)
            t.start()
            self.assertTrue(wait_for(lambda: a.verbs().count("SCHG") > n_schg, 5.0))
            a.delays.pop("SCHG")
            if line is None:
                out = json.loads(raw_request(c.port, b"REBALANCE").decode())
                self.assertEqual(out.get("status"), "rebalanced", out)
                new_gen = out["by_node"]["node0:0"]["gen"]
            else:
                out = rj(c.port, line)
                self.assertTrue(out.get("ok"), out)
            t.join(15)
            self.assertFalse(t.is_alive())
        # the stale round's results were discarded: the clear stands
        rec = rj(c.port, "ASSIGNMENTS")["assignments"]["node0:0"]
        self.assertEqual(rec["desired"], "inactive")
        self.assertNotEqual(new_gen, first["by_node"]["node0:0"]["gen"])

    def test_register_same_nonce_new_address_moves_poller(self):
        from federation_common import Request
        c = self.coord([], start=False)
        c._ensure_poller()
        self._cleanup.append(c.close)
        body = {"node_key": "hosta:1", "node_id": "hosta:1", "cluster_id": "default",
                "instance_id": 1, "advertise_host": "hosta", "port_stride": 100,
                "nonce": "n1"}

        def reg(src):
            req = Request("REGISTER", "", json.dumps(body).encode(), False, "REGISTER")
            return c._verb_register(req, (src, 40000))
        self.assertEqual(reg("10.0.0.11")["registered_as"]["host"], "10.0.0.11")
        self.assertEqual(c._poller._peer_endpoints["hosta:1"], ("10.0.0.11", 5102))
        self.assertEqual(reg("10.0.0.99")["registered_as"]["host"], "10.0.0.99")  # DHCP move
        self.assertEqual(c._members()[0].connect_host, "10.0.0.99")
        self.assertEqual(c._poller._peer_endpoints["hosta:1"], ("10.0.0.99", 5102))
        self.assertEqual(c._compute_instance_ports(c._members()[0].inst)["status_port"], 5102)

    def test_send_schedule_path_verified_and_deleted(self):
        a = self.hwc(mode="legacy")
        c = self.coord([member(0, a)])
        text = FederationScheduler._build_schedule_json(
            {"entries": SCHED["entries"]}, name="federation_inst0")
        self.assertGreater(len(text), 124)
        r = c.send_schedule(0, text.encode())
        self.assertNotIn("_path", r)
        self.assertEqual(r["transfer"], "path")
        self.assertTrue(r["verified"], r)
        path = [f[4:].rstrip(b"\x00").decode() for _c, v, f in a.frames if v == "SCHD"][-1]
        self.assertFalse(os.path.exists(path))
        self.assertEqual(a.schedule["name"], "federation_inst0")

    def test_election_terms_beyond_u32_refused(self):
        c = self.coord([], start=False)
        for handler, verb in ((c._verb_prevote, "PREVOTE"), (c._verb_lease, "LEASE")):
            r = handler(_raw(verb + " 4294967296 127.0.0.1:1"), None)
            self.assertEqual((r["ok"], r["error"]), (False, "bad_request"), r)
            self.assertNotEqual(handler(_raw(verb + " 4294967295 127.0.0.1:1"), None)
                                .get("error"), "bad_request")

    def test_cli_exit_codes(self):
        import contextlib
        import io
        from federation_coordinator import main
        for argv in (["--bogus"], ["--port", "abc"], ["--port", "99999"], ["--log-level"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main(argv), 3, argv)
            self.assertIn("configuration error", err.getvalue())
        with tempfile.TemporaryDirectory() as d:
            ini = os.path.join(d, "f.ini")
            with open(ini, "w") as f:
                f.write("[federation]\ncoordinator_port = 70000\n")
            with self.assertRaises(ConfigError):
                build_from_args(["--config", ini])

    def test_out_of_range_integers_are_usage_errors(self):
        # FREQ is a u64 and channel values are u32 on the wire: a value outside
        # the field is a per-member usage error, never a struct.error escaping
        a = self.hwc()
        c = self.coord([member(0, a)])
        cases = (("FREQ -1", "'Q' format"), ("FREQ 18446744073709551616", "'Q' format"),
                 ("GAIN -5", "'I' format"), ("EGAN 4294967296", "'I' format"),
                 ("ORNT 1e40 0", "float too large"))
        for line, msg in cases:
            out = rj(c.port, line) if line.split()[0] in ("FREQ", "GAIN") else None
            if out is not None:
                self.assertEqual(out["command"], line.split()[0], out)
                r = out["results"]["0"]
                self.assertIs(r["applied"], False, r)
                self.assertIn(msg, r["error"])
            out = rj(c.port, "ALL " + line)
            self.assertTrue(out["ok"], out)
            r = out["results"]["node0:0"]
            self.assertIs(r["applied"], False, r)
            self.assertIn(msg, r["error"])
            self.assertNotEqual(out.get("error"), "internal_error")
        self.assertEqual(a.command_verbs(), [])
        with self.assertRaises(ValueError):
            FederationCoordinator._encode_hwc_command("BIAS 1 -1")
        self.assertEqual(FederationCoordinator._encode_hwc_command(
            "FREQ 18446744073709551615")[1], b"\xff" * 8)

    def test_hub_register_starts_the_poller(self):
        # hub mode takes its peer table from the REGISTER ack: a static coordinator
        # with no HEALTH/NODES caller must still poll and report real peer states
        from federation_common import request_json
        sa, sb = self.status(), self.status()
        c = self.coord([], settings=fast_settings(health_poll_interval=0.1))
        self.assertIsNone(c._poller)

        def body(key, iid, sport, nonce, want):
            return {"node_key": key, "node_id": key, "cluster_id": "default",
                    "instance_id": iid, "advertise_host": "127.0.0.1", "port_stride": 100,
                    "ports": {"iq": 5000, "ctl": 5001, "status": sport, "events": 5003,
                              "zmq": 1130},
                    "nonce": nonce, "want_peers": want}
        r = request_json("127.0.0.1", c.port, "REGISTER",
                         body=body("hostb:1", 1, sb.port, "b", False))
        self.assertTrue(r["ok"], r)
        self.assertNotIn("peers", r)
        self.assertIsNone(c._poller)              # no want_peers: no poller
        state = {}

        def peer_up():
            ra = request_json("127.0.0.1", c.port, "REGISTER",
                              body=body("hosta:0", 0, sa.port, "a", True))
            state.update(ra.get("peers") or {})
            return (state.get("hostb:1") or {}).get("state") == "up"
        self.assertTrue(wait_for(peer_up, 5.0, step=0.1), state)
        self.assertIsNotNone(c._poller)

    def test_reconcile_superseded_by_rebalance_plan_emits_nothing(self):
        # a round planned before a REBALANCE must not read the REBALANCE's fresh
        # (not yet committed) gen as an operator override (D.5.3)
        c, a, b, sa, sb, first = self._reconciled_pair(hwc_request_timeout_sec=5.0,
                                                       max_member_rto_sec=10.0)
        old_gen = a.gen
        entered, gate = threading.Event(), threading.Event()
        real = c._reconcile_member

        def held(m, rec, ctx, issued, fc):
            if m.node_key != "node0:0":
                return {"action": "skip", "update": {}}
            entered.set()
            gate.wait(10)
            return real(m, rec, ctx, issued, fc)
        c._reconcile_member = held
        actions = {}
        t = threading.Thread(target=lambda: actions.update(c.reconcile_round()))
        t.start()
        self.assertTrue(entered.wait(5))
        b.delays["SCHG"] = 1.5          # keeps the REBALANCE uncommitted after a's load
        n0 = a.verbs().count("SCHG")
        out = {}
        r = threading.Thread(target=lambda: out.update(
            json.loads(raw_request(c.port, b"REBALANCE").decode())))
        r.start()
        # a's new partition is loaded and verified; the REBALANCE waits on b
        self.assertTrue(wait_for(lambda: a.gen not in (None, old_gen)
                                 and a.verbs().count("SCHG") > n0, 5.0))
        self.assertNotIn("status", out)
        n_schg = a.verbs().count("SCHG")
        gate.set()
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertEqual(actions.get("node0:0"), "skip", actions)
        self.assertEqual(a.verbs().count("SCHG"), n_schg)    # the stale round sent nothing
        r.join(15)
        self.assertFalse(r.is_alive())
        self.assertEqual(out.get("status"), "rebalanced", out)
        self.assertNotIn("schedule_overridden", [e["event_type"] for e in c._local_events])
        rec = rj(c.port, "ASSIGNMENTS")["assignments"]["node0:0"]
        self.assertEqual(rec["state"], "applied", rec)
        self.assertEqual(rec["gen"], out["by_node"]["node0:0"]["gen"])
        self.assertEqual(a.gen, rec["gen"])


class _TrickleServer(object):
    """Accepts connections and sends one byte every *period* seconds, forever."""

    def __init__(self, period=0.05):
        self.period = period
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._trickle, args=(c,), daemon=True).start()

    def _trickle(self, c):
        try:
            while not self.stop.is_set():
                c.sendall(b" ")
                time.sleep(self.period)
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.stop.set()
        self.sock.close()


class TestHardening(CoordinatorCase):
    """Bounded reads, atomic sync apply, REGISTER limits, priority probe."""

    def test_apply_sync_doc_is_atomic(self):
        c = self.coord([], start=False)
        for bad in ({"term": 5, "version": 7, "issued_gens": ["x"]},
                    {"term": 6, "version": 9, "assignments": [1]},
                    {"term": 6, "version": 9, "assignments": {"k": 1}},
                    {"term": 6, "version": 9, "members_runtime": [1]},
                    {"term": 6, "version": 9, "schedule": {"entries": "x"}}):
            with self.assertRaises(ValueError):
                c._apply_sync_doc(bad)
            self.assertEqual((c._sync_term, c._version), (0, 0), bad)
            self.assertEqual(c._assignments, {})
        c._apply_sync_doc({"term": 2, "version": 3, "schedule": SCHED,
                           "issued_gens": [1, 2], "members_runtime": {"added": ["hostq:2"]}})
        self.assertEqual((c._sync_term, c._version), (2, 3))
        self.assertEqual([m.node_key for m in c._members()], ["hostq:2"])

    def test_load_state_discards_corrupt_assignments(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "master_schedule.json"), "w") as f:
                json.dump({"term": 3, "version": 4, "schedule": None}, f)
            with open(os.path.join(d, "assignments.json"), "w") as f:
                json.dump({"assignments": [1]}, f)
            with self.assertLogs("federation_coordinator", level="ERROR"):
                c = self.coord([], settings=fast_settings(state_dir=d), start=False)
            self.assertEqual((c._sync_term, c._version), (0, 0))

    def test_status_poll_bounded_against_trickling_member(self):
        tr = _TrickleServer()
        self._cleanup.append(tr.close)
        insts = [{"host": "127.0.0.1", "instance_id": 0, "node_key": "slow:0",
                  "ports": {"ctl": _free_port(), "status": tr.port}}]
        c = self.coord(insts, settings=fast_settings(status_timeout_sec=0.3), start=False)
        t0 = time.monotonic()
        out = c._line_worker("STATUS")(c._members()[0], _Ctx(5.0))
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIn("error", out)

    def test_health_fetch_bounded_against_trickling_peer(self):
        from federation_health import FederationHealth
        tr = _TrickleServer()
        self._cleanup.append(tr.close)
        fh = FederationHealth(0, [], peer_timeout=0.3)
        t0 = time.monotonic()
        with self.assertRaises(socket.timeout):
            fh._fetch_status("127.0.0.1", tr.port)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_register_rate_limited_per_source(self):
        import federation_coordinator as fcm
        from federation_common import Request
        c = self.coord([], start=False)

        def reg(i, src):
            body = {"node_key": "h{}:1".format(i), "instance_id": 1, "nonce": "n{}".format(i),
                    "advertise_host": "10.1.0.{}".format(i % 250 + 1)}
            req = Request("REGISTER", "", json.dumps(body).encode(), False, "REGISTER")
            return c._verb_register(req, (src, 40000))
        for i in range(fcm.REGISTER_NEW_PER_SOURCE):
            self.assertTrue(reg(i, "10.0.0.1")["ok"])
        r = reg(999, "10.0.0.1")
        self.assertEqual((r["ok"], r["error"]), (False, "busy"))
        self.assertTrue(reg(0, "10.0.0.1")["ok"])          # a re-registration still works
        self.assertTrue(reg(1000, "10.0.0.2")["ok"])       # another source is not affected
        old = fcm.REGISTER_MAX_MEMBERS
        fcm.REGISTER_MAX_MEMBERS = len(c._registered)
        try:
            r = reg(1001, "10.0.0.3")
            self.assertEqual((r["ok"], r["error"]), (False, "busy"))
        finally:
            fcm.REGISTER_MAX_MEMBERS = old

    def test_collision_events_pruned(self):
        import federation_coordinator as fcm
        c = self.coord([], start=False)
        mono = time.monotonic()
        for i in range(300):
            c._collision_events[("k{}".format(i), "a", "b")] = mono - fcm.COLLISION_EVENT_SEC - 1
        from federation_common import Request

        def reg(src, nonce):
            body = {"node_key": "dup:1", "instance_id": 1, "nonce": nonce}
            req = Request("REGISTER", "", json.dumps(body).encode(), False, "REGISTER")
            return c._verb_register(req, (src, 40000))
        self.assertTrue(reg("10.0.0.1", "a")["ok"])
        self.assertEqual(reg("10.0.0.2", "b")["error"], "duplicate_node_key")
        self.assertLessEqual(len(c._collision_events), 256)
        self.assertIn(("dup:1", "10.0.0.1", "10.0.0.2"), c._collision_events)

    def test_idle_overflow_does_not_starve_priority_probe(self):
        c = self.coord([], settings=fast_settings(max_handlers=2, reserved_handlers=2))
        holders = [socket.create_connection(("127.0.0.1", c.port)) for _ in range(2 + 40)]
        self._cleanup.extend(h.close for h in holders)
        time.sleep(0.3)
        t0 = time.monotonic()
        leader = rj(c.port, "LEADER", timeout=5.0)
        self.assertTrue(leader["ok"])
        self.assertLess(time.monotonic() - t0, 1.5)


class _Ctx(object):
    def __init__(self, sec):
        self.deadline = time.monotonic() + sec
        self.phase = None

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())


def _raw(line):
    from federation_common import Request
    parts = line.split(None, 1)
    return Request(parts[0].upper(), parts[1] if len(parts) > 1 else "", None, False, line)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if __name__ == "__main__":
    unittest.main()
