"""Tests for heimdall-ctl fleet/router subcommands against fake line servers."""
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

UTIL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "util"))
sys.path.insert(0, UTIL_DIR)

from heimdall_ctl import __main__ as cli  # noqa: E402
from heimdall_ctl.client.federation import (  # noqa: E402
    CoordinatorClient, CoordinatorError, classify, summarize, format_result,
)

LEGACY_VERBS = ("PING", "STATUS", "FREQ", "GAIN", "INSTANCE", "REBALANCE")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeCoordinator:
    """Line server like the coordinator (D.1): one request per connection,
    optional ``AUTH <token>`` prefix, ``@n`` bodies; legacy verbs reply
    without a trailing newline, new verbs with one.

    handlers: {request-line prefix: dict | callable(line, body) -> dict};
    the longest matching prefix wins. ``silent`` accepts and reads but never
    replies. Every request is recorded as (line, body, raw bytes)."""

    def __init__(self, handlers=None, token=None, silent=False, legacy=False):
        self.handlers = dict(handlers or {})
        self.token = token
        self.silent = silent
        self.legacy = legacy
        self.requests = []
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self.endpoint = "127.0.0.1:{}".format(self.port)
        self._stop = threading.Event()
        self._held = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def lines(self):
        return [r[0] for r in self.requests]

    def _read_request(self, conn):
        conn.settimeout(5.0)
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        line, _, rest = data.partition(b"\n")
        text = line.decode()
        raw = data
        body = None
        token_ok = True
        if self.token is not None:
            prefix = "AUTH {} ".format(self.token)
            if text.startswith(prefix):
                text = text[len(prefix):]
            else:
                token_ok = False
        parts = text.split()
        if parts and parts[-1].startswith("@") and parts[-1][1:].isdigit():
            n = int(parts[-1][1:])
            text = " ".join(parts[:-1])
            body = rest
            while len(body) < n:
                chunk = conn.recv(n - len(body))
                if not chunk:
                    break
                body += chunk
            raw += body[len(rest):]
        return text, body, raw, token_ok

    def _reply_for(self, line, body):
        best = None
        for prefix in self.handlers:
            if line == prefix or line.startswith(prefix + " ") or line.startswith(prefix):
                if best is None or len(prefix) > len(best):
                    best = prefix
        if best is None:
            return {"error": "Unknown command"} if self.legacy else \
                {"ok": False, "error": "unknown_command"}
        handler = self.handlers[best]
        return handler(line, body) if callable(handler) else handler

    def _run(self):
        self.srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                line, body, raw, token_ok = self._read_request(conn)
                self.requests.append((line, body, raw))
                if self.silent:
                    self._held.append(conn)
                    continue
                if not token_ok:
                    reply = {"ok": False, "error": "auth_required"}
                else:
                    reply = self._reply_for(line, body)
                text = json.dumps(reply)
                verb = line.split()[0].upper() if line.split() else ""
                if not (self.legacy or verb in LEGACY_VERBS):
                    text += "\n"
                conn.sendall(text.encode())
            except OSError:
                pass
            if not self.silent:
                conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        for c in self._held:
            c.close()
        self.srv.close()


def version_reply(mutation_ms=40000, query_ms=6000, schedule_ms=45000, role="static",
                  term=0):
    return {"ok": True, "protocol": 2, "fw_version": "1.1.0-dev", "node_id": "hosta:c",
            "cluster_id": "default", "role": role, "term": term, "members": 3,
            "verbs": ["VERSION"], "abandoned_workers": 0,
            "deadlines": {"query_ms": query_ms, "mutation_ms": mutation_ms,
                          "schedule_ms": schedule_ms}}


def R(applied, reason=None, **kw):
    out = {"ok": True, "applied": applied}
    if reason:
        out["reason"] = reason
    out.update(kw)
    return out


class FleetTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hctl_fleet_")
        self._env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp})
        self._env.start()
        for key in ("HEIMDALL_CONFIG", "HEIMDALL_COORDINATOR", "HEIMDALL_AUTH_TOKEN"):
            os.environ.pop(key, None)
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.servers = []

    def tearDown(self):
        os.chdir(self._cwd)
        self._env.stop()
        for s in self.servers:
            s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def coord(self, handlers=None, **kw):
        h = {"VERSION": version_reply()}
        h.update(handlers or {})
        c = FakeCoordinator(h, **kw)
        self.servers.append(c)
        return c

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def fleet(self, coord, *argv):
        return self.run_cli("--coordinator", coord.endpoint, "--color", "never", "fleet",
                            *argv)


class TestResultClassification(unittest.TestCase):
    def test_classify_and_summary(self):
        results = {
            "a:0": R(True, "ok", elapsed_ms=412),
            "a:1": R(False, "deferred_timeout", fsm="NOISE_CTR_WAIT"),
            "b:0": {"ok": False, "applied": None, "error": "timeout", "phase": "command"},
            "b:1": {"ok": True, "applied": None, "legacy_ack": True},
            "c:0": R(None, "held_for_burst", fsm="NOISE_CTR_WAIT"),
            "c:1": {"ok": False, "applied": False, "error": "refused",
                    "error_kind": "refused", "phase": "connect"},
            "d:0": {"ok": True},
            "d:1": {"ok": True, "data": {"fsm": "IQ_CAL"}},
        }
        self.assertEqual(summarize(results), {"applied": 1, "not_applied": 2,
                                              "unconfirmed": 3, "timeout": 1, "error": 0,
                                              "ok": 1})
        self.assertEqual(format_result(results["a:0"]), "applied (412 ms)")
        self.assertEqual(format_result(results["a:1"]),
                         "NOT applied: deferred_timeout (fsm=NOISE_CTR_WAIT)")
        self.assertEqual(format_result(results["b:0"]),
                         "timeout during command (outcome unknown)")
        self.assertEqual(format_result({"ok": False, "applied": False, "error": "timeout",
                                        "phase": "xack"}), "timeout during xack (not sent)")
        self.assertEqual(format_result(results["b:1"]),
                         "sent; server does not confirm application (legacy server)")
        self.assertEqual(format_result(results["c:0"]),
                         "held until the calibration burst ends (fsm=NOISE_CTR_WAIT)")
        self.assertEqual(classify({"error": "Instance 3 not found"}), "error")


class TestCoordinatorClient(FleetTestBase):
    def test_version_error_reply_is_not_legacy(self):
        state = {"reply": {"ok": False, "error": "busy", "retry_in_sec": 2}}
        c = self.coord({"VERSION": lambda line, body: state["reply"]})
        client = CoordinatorClient([("127.0.0.1", c.port)])
        with self.assertRaises(CoordinatorError) as cm:
            client.version()
        self.assertEqual(cm.exception.reply.error, "busy")
        state["reply"] = {"error": "Unknown command"}   # the protocol-1 reply
        self.assertIsNone(client.version())
        self.assertTrue(client.legacy)

    def test_watch_refresh_keeps_last_good_version(self):
        state = {"reply": version_reply(term=4)}
        c = self.coord({"VERSION": lambda line, body: state["reply"]})
        client = CoordinatorClient([("127.0.0.1", c.port)])
        self.assertEqual(client.version()["term"], 4)
        state["reply"] = {"ok": False, "error": "busy", "retry_in_sec": 1}
        client.refresh_version()
        self.assertEqual(client.version()["term"], 4)
        state["reply"] = version_reply(term=5)
        client.refresh_version()
        self.assertEqual(client.version()["term"], 5)

    def test_timeout_from_version_deadlines(self):
        c = self.coord({"VERSION": version_reply(mutation_ms=2000, query_ms=500,
                                                 schedule_ms=9000)})
        client = CoordinatorClient([("127.0.0.1", c.port)])
        self.assertAlmostEqual(client.timeout_for("mutation"), 5.0)
        self.assertAlmostEqual(client.timeout_for("query"), 3.5)
        self.assertAlmostEqual(client.timeout_for("schedule"), 12.0)
        self.assertEqual(c.lines, ["VERSION"])  # fetched once
        fixed = CoordinatorClient([("127.0.0.1", c.port)], timeout=7.5)
        self.assertEqual(fixed.timeout_for("mutation"), 7.5)

    def test_slow_reply_times_out_at_version_deadline_plus_3(self):
        def slow(line, body):
            time.sleep(4.5)
            return {"ok": True, "command": "AGC", "results": {}}
        c = self.coord({"VERSION": version_reply(mutation_ms=500), "ALL AGC": slow})
        t0 = time.monotonic()
        code, _, err = self.fleet(c, "agc")
        elapsed = time.monotonic() - t0
        self.assertEqual(code, 3)
        self.assertIn("no reply within 3.5 s", err)
        self.assertLess(elapsed, 4.4)

    def test_failover_only_on_connect_errors(self):
        dead = _free_port()
        good = self.coord({"PING": {"ok": True, "ts": 1.0}})
        code, out, err = self.run_cli("--coordinator",
                                      "127.0.0.1:{},{}".format(dead, good.endpoint),
                                      "fleet", "ping")
        self.assertEqual(code, 0, err)
        self.assertIn("pong from {}".format(good.endpoint), out)
        # a coordinator that accepts and then never answers is not retried elsewhere
        silent = FakeCoordinator(silent=True)
        self.servers.append(silent)
        other = self.coord({"PING": {"ok": True, "ts": 1.0}})
        code, out, err = self.run_cli("--timeout", "1", "--coordinator",
                                      "{},{}".format(silent.endpoint, other.endpoint),
                                      "fleet", "ping")
        self.assertEqual(code, 3)
        self.assertEqual(silent.lines, ["PING"])
        self.assertEqual(other.lines, [])

    def test_all_unreachable_exit_3(self):
        code, _, err = self.run_cli("--coordinator", "127.0.0.1:{},127.0.0.1:{}".format(
            _free_port(), _free_port()), "fleet", "ping")
        self.assertEqual(code, 3)
        self.assertIn("all coordinators unreachable", err)

    def test_not_leader_redirect_followed_once(self):
        leader = self.coord({"ALL FREQ": {"ok": True, "command": "FREQ",
                                          "results": {"a:0": R(True, "ok", elapsed_ms=5)},
                                          "deadline_ms": 40000}})
        follower = self.coord({"ALL FREQ": {"ok": False, "error": "not_leader",
                                            "leader": {"endpoint": leader.endpoint,
                                                       "term": 7}}})
        code, out, err = self.fleet(follower, "tune", "433M")
        self.assertEqual(code, 0, err)
        self.assertIn("ALL FREQ 433000000", leader.lines)
        self.assertIn("applied (5 ms)", out)
        # a second not_leader -> exit 4
        loop_b = self.coord()
        loop_a = self.coord({"ALL FREQ": {"ok": False, "error": "not_leader",
                                          "leader": {"endpoint": loop_b.endpoint, "term": 8}}})
        loop_b.handlers["ALL FREQ"] = {"ok": False, "error": "not_leader",
                                       "leader": {"endpoint": loop_a.endpoint, "term": 8}}
        code, _, err = self.fleet(loop_a, "tune", "433M")
        self.assertEqual(code, 4)
        self.assertIn("not_leader", err)
        self.assertEqual(loop_a.lines.count("ALL FREQ 433000000"), 1)
        self.assertEqual(loop_b.lines.count("ALL FREQ 433000000"), 1)
        # unknown leader -> exit 4 at once
        lost = self.coord({"ALL AGC": {"ok": False, "error": "not_leader", "leader": None}})
        code, _, _ = self.fleet(lost, "agc")
        self.assertEqual(code, 4)

    def test_auth_prefix(self):
        c = self.coord({"PING": {"ok": True, "ts": 1.0}}, token="s3cret")
        code, _, _ = self.fleet(c, "ping")
        self.assertEqual(code, 1)  # auth_required
        code, _, err = self.run_cli("--auth-token", "s3cret", "--coordinator", c.endpoint,
                                    "fleet", "ping")
        self.assertEqual(code, 0, err)
        self.assertTrue(c.requests[-1][2].startswith(b"AUTH s3cret PING\n"))
        with mock.patch.dict(os.environ, {"HEIMDALL_AUTH_TOKEN": "s3cret",
                                          "HEIMDALL_COORDINATOR": c.endpoint}):
            code, _, _ = self.run_cli("fleet", "ping")
        self.assertEqual(code, 0)


class TestFleetCommands(FleetTestBase):
    FANOUT = {"ok": True, "command": "X", "deadline_ms": 40000,
              "results": {"hosta:0": R(True, "ok", elapsed_ms=412, fsm="IQ_CAL"),
                          "hosta:1": R(False, "deferred_timeout", fsm="NOISE_CTR_WAIT"),
                          "hostb:0": {"ok": False, "applied": None, "error": "timeout",
                                      "phase": "command"}},
              "summary": {"applied": 1, "not_applied": 1, "unconfirmed": 0, "timeout": 1,
                          "error": 0}}

    def test_tune_table_and_exit_codes(self):
        c = self.coord({"ALL FREQ": self.FANOUT})
        code, out, _ = self.fleet(c, "tune", "433M")
        self.assertEqual(code, 1)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0].split(), ["NODE", "RESULT"])
        self.assertRegex(lines[1], r"^hosta:0\s+applied \(412 ms\)$")
        self.assertRegex(lines[2], r"^hosta:1\s+NOT applied: deferred_timeout "
                                   r"\(fsm=NOISE_CTR_WAIT\)$")
        self.assertRegex(lines[3], r"^hostb:0\s+timeout during command \(outcome unknown\)$")
        self.assertEqual(lines[4], "summary: applied 1, not applied 1, timeout 1")
        self.assertEqual(c.lines, ["VERSION", "ALL FREQ 433000000"])

    def test_unconfirmed_only_exit_0(self):
        c = self.coord({"ALL AGC": {"ok": True, "command": "AGC", "results": {
            "a:0": {"ok": True, "applied": None, "legacy_ack": True},
            "a:1": R(None, "held_for_burst")}}})
        code, out, _ = self.fleet(c, "agc")
        self.assertEqual(code, 0)
        self.assertIn("unconfirmed 2", out)

    def test_targets_use_fanout(self):
        c = self.coord({"FANOUT": {"ok": True, "results": {"a:0": R(True, "ok")}}})
        for argv, line in ((["park", "--targets", "a:0,b:1"], "FANOUT a:0,b:1 PARK"),
                           (["bearing", "120", "15", "--targets", "a:0"],
                            "FANOUT a:0 ORNT 120 15"),
                           (["agc", "--targets", "a:0"], "FANOUT a:0 AGC"),
                           (["gain", "297,297", "--targets", "a:0"],
                            "FANOUT a:0 GAIN 297,297"),
                           (["lna-gain", "14.5,0", "--targets", "a:0"],
                            "FANOUT a:0 EGAN 145,0"),
                           (["bias-tee", "1,0", "--targets", "a:0"],
                            "FANOUT a:0 BIAS 1,0")):
            code, out, err = self.fleet(c, *argv)
            self.assertEqual(code, 0, (argv, err))
            self.assertEqual(c.lines[-1], line)

    def test_unified_values_grouped_by_channel_count(self):
        hwsq = {"ok": True, "command": "HWSQ", "results": {
            "a:0": {"ok": True, "data": {"fsm": "IQ_CAL", "nch": 5}},
            "a:1": {"ok": True, "data": {"fsm": "IQ_CAL", "nch": 4}},
            "b:0": {"ok": False, "applied": False, "error": "refused", "phase": "connect"}}}

        def fanout(line, body):
            keys = line.split()[1].split(",")
            return {"ok": True, "results": {k: R(True, "ok") for k in keys}}
        c = self.coord({"ALL HWSQ": hwsq, "FANOUT": fanout})
        code, out, err = self.fleet(c, "gain", "--unified", "297")
        self.assertEqual(code, 0, err)
        self.assertIn("FANOUT a:0,b:0 GAIN 297,297,297,297,297", c.lines)
        self.assertIn("FANOUT a:1 GAIN 297,297,297,297", c.lines)
        self.assertIn("b:0: channel count unknown, using 5", err)
        self.assertIn("summary: applied 3, not applied 0", out)
        code, _, _ = self.fleet(c, "lna-gain", "--unified", "14.5")
        self.assertIn("FANOUT a:1 EGAN 145,145,145,145", c.lines)
        code, _, _ = self.fleet(c, "bias-tee", "--all", "on")
        self.assertIn("FANOUT a:1 BIAS 1,1,1,1", c.lines)

    def test_hwc_status_table(self):
        c = self.coord({"ALL HWSQ": {"ok": True, "results": {
            "a:0": {"ok": True, "data": {"fsm": "IQ_CAL", "pend": 0, "ns": 0, "agc": 0,
                                         "nch": 5, "f": 433000000, "fage": 437, "fr": 1,
                                         "fc": 99, "t": 0}},
            "b:0": {"ok": False, "applied": False, "error": "timeout", "phase": "xack"}}}})
        code, out, _ = self.fleet(c, "hwc-status")
        self.assertEqual(code, 1)
        self.assertIn("IQ_CAL", out)
        self.assertIn("timeout during xack (not sent)", out)

    def test_recal(self):
        c = self.coord({"RECAL": lambda line, body: {"ok": True, "results": {
            "a:0": R(True, "ok", fallback="INIT")}} if line == "RECAL" else
            {"ok": True, "node": line.split()[1],
             "result": R(True, "ok")}})
        code, out, _ = self.fleet(c, "recal")
        self.assertEqual(code, 0)
        self.assertIn("applied [INIT fallback]", out)
        code, out, _ = self.fleet(c, "recal", "--targets", "a:0,a:1")
        self.assertEqual(code, 0)
        self.assertEqual(c.lines[-2:], ["RECAL a:0", "RECAL a:1"])
        self.assertIn("summary: applied 2", out)

    def test_exec_all_node_fanout(self):
        c = self.coord({"ALL": {"ok": True, "results": {"a:0": {"ok": True,
                                                              "data": {"az": 1}}}},
                        "NODE": {"ok": True, "node": "a:0", "result": R(True, "ok")},
                        "FANOUT": {"ok": True, "results": {"a:0": R(True, "ok")}}})
        code, out, _ = self.fleet(c, "exec", "OQRY")
        self.assertEqual(code, 0)
        self.assertEqual(c.lines[-1], "ALL OQRY")
        self.assertIn('{"az":1}', out)
        code, _, _ = self.fleet(c, "exec", "--node", "a:0", "FREQ", "433000000")
        self.assertEqual(c.lines[-1], "NODE a:0 FREQ 433000000")
        code, _, _ = self.fleet(c, "exec", "--targets", "a:0,b:0", "SCHS")
        self.assertEqual(c.lines[-1], "FANOUT a:0,b:0 SCHS")

    def test_schedule_commands(self):
        doc = {"name": "s", "entries": [{"frequency": 433000000, "dwell_frames": 10}]}
        path = os.path.join(self.tmp, "s.json")
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
        c = self.coord({
            "SCHEDULE SET": {"ok": True, "entries": 1, "strategy": "range", "version": 4,
                             "bytes": 60, "assignments": {"a:0": 1},
                             "replicated": {"stored": 1, "of": 1}, "durable": True},
            "SCHEDULE GET": {"ok": True, "version": 4, "term": 7, "set_at": 1761900000.5,
                             "schedule": doc},
            "SCHEDULE CLEAR": {"ok": True, "stopped": {"a:0": R(True, "ok")},
                               "replicated": {"stored": 1, "of": 1}},
            "SCHEDULE STRATEGY": {"ok": True, "strategy": "range",
                                  "replicated": {"stored": 1, "of": 1}}})
        code, out, err = self.fleet(c, "schedule", "set", path, "--strategy", "range")
        self.assertEqual(code, 0, err)
        line, body, raw = c.requests[-1]
        self.assertEqual(line, "SCHEDULE SET range")
        compact = json.dumps(doc, separators=(",", ":")).encode()
        self.assertEqual(body, compact)
        self.assertTrue(raw.startswith("SCHEDULE SET range @{:d}\n".format(
            len(compact)).encode()))
        self.assertIn("stored: 1 entries, strategy range, version 4", out)
        code, out, _ = self.fleet(c, "schedule", "get")
        self.assertEqual(code, 0)
        self.assertIn("version 4  term 7", out)
        self.assertIn('"frequency": 433000000', out)
        code, out, _ = self.fleet(c, "schedule", "clear")
        self.assertEqual(code, 0)
        self.assertIn("a:0", out)
        code, out, _ = self.fleet(c, "schedule", "strategy", "range")
        self.assertEqual(code, 0)
        self.assertEqual(c.lines[-1], "SCHEDULE STRATEGY range")

    def test_not_replicated_exit_1_with_warning(self):
        c = self.coord({"SCHEDULE STRATEGY": {"ok": False, "error": "not_replicated",
                                              "replicated": {"stored": 1, "of": 3},
                                              "version": 5}})
        code, _, err = self.fleet(c, "schedule", "strategy", "round_robin")
        self.assertEqual(code, 1)
        self.assertIn("not replicated to a quorum (stored 1 of 3)", err)

    def test_rebalance(self):
        c = self.coord({"REBALANCE": {
            "status": "rebalanced", "instances": 2, "strategy": "round_robin", "gen_seq": 3,
            "results": {"0": {}}, "by_node": {
                "a:0": {"ok": True, "applied": True, "entries": 6, "gen": 11,
                        "verified": True, "transfer": "chunked", "reason": "ok"},
                "a:1": {"ok": True, "applied": False, "entries": 6, "gen": 12,
                        "verified": False, "transfer": "inline",
                        "reason": "schedule_too_large_for_legacy_server"}},
            "skipped": ["b:0"], "empty": [], "inactive": [], "deadline_ms": 45000}})
        code, out, _ = self.fleet(c, "rebalance", "--strategy", "range", "--include-degraded")
        self.assertEqual(code, 1)
        self.assertEqual(c.lines[-1], "REBALANCE range INCLUDE_DEGRADED")
        self.assertIn("chunked", out)
        self.assertIn("skipped: b:0", out)

    def test_events_since_sends_age(self):
        c = self.coord({"EVENTS": {"ok": True, "now": 1.0, "events": [
            {"rx_ts": 1761900000.1, "node_key": "a:0", "severity": "warning",
             "event_type": "sync_lost", "payload": {"cpi_index": 1}}]}})
        code, out, _ = self.fleet(c, "events", "-n", "50", "--since", "10m")
        self.assertEqual(code, 0)
        self.assertEqual(c.lines[-1], "EVENTS 50 age:600")
        self.assertRegex(out.strip(), r"\[a:0\] warning  sync_lost")
        self.fleet(c, "events")
        self.assertEqual(c.lines[-1], "EVENTS 100")

    def test_queries(self):
        c = self.coord({
            "PING": {"ok": True, "ts": 1761900000.0},
            "LEADER": {"ok": True, "role": "leader", "term": 7, "eligible": True,
                       "schedule_version": 3, "leader": {"endpoint": "hosta:6000",
                                                         "term": 7},
                       "candidates": [{"endpoint": "hosta:6000", "reachable": True,
                                       "role": "leader", "term": 7}], "quorum": True},
            "HEALTH": {"ok": True, "overall": "degraded", "members": {
                "a:0": {"state": "up", "reachable": True, "health": "ok", "sync_state": 6,
                        "frequency_hz": 433000000, "seen_age_sec": 0.4}},
                "unreachable": [], "degraded": [], "stale": []},
            "MEMBERS": {"ok": True, "members": [
                {"node_key": "a:0", "node_id": "a:0", "host": "a", "instance_id": 0,
                 "source": "config", "state": "up"}], "key_collisions": []},
            "MEMBER": {"ok": True, "members": [], "changed": True,
                       "replicated": {"stored": 1, "of": 2}, "durable": False},
            "PEERS": {"ok": True, "peers": {"a:0": {"peers": {}}}},
            "ASSIGNMENTS": {"ok": True, "strategy": "round_robin", "gen_seq": 2,
                            "assignments": {"a:0": {"gen": 4, "entries": [{}, {}],
                                                    "state": "applied", "attempts": 1}},
                            "empty": []},
            "DB STATS": {"ok": True, "nodes": {"a:0": {"records": 2}}},
            "ROUTER STATS": {"ok": True, "router": "a:7001", "stats": {
                "uptime_sec": 10.0, "output_port": 7000, "sources": {}, "clients": []}}})
        for argv, line, needle in (
                (["ping"], "PING", "pong from"),
                (["version"], "VERSION", "protocol 2"),
                (["leader"], "LEADER", "leader hosta:6000"),
                (["health"], "HEALTH", "overall: degraded"),
                (["members"], "MEMBERS", "config"),
                (["members", "add", "hostc:1@site-c"], "MEMBER ADD hostc:1@site-c",
                 "changed: yes"),
                (["members", "remove", "hostc:1"], "MEMBER REMOVE hostc:1", "changed: yes"),
                (["peers"], "PEERS", '"a:0"'),
                (["assignments"], "ASSIGNMENTS", "applied"),
                (["db-stats"], "DB STATS", '"records": 2'),
                (["router-stats"], "ROUTER STATS", "output :7000")):
            code, out, err = self.fleet(c, *argv)
            self.assertEqual(code, 0, (argv, err))
            self.assertEqual(c.lines[-1], line)
            self.assertIn(needle, out)
        code, _, err = self.fleet(c, "members", "add", "hostc:1")
        self.assertIn("durable: false", err)

    def test_status_table(self):
        c = self.coord({
            "NODES STATUS": {"ok": True, "overall": "degraded", "cached": True,
                             "nodes": {
                                 "hosta:0": {"sync_state": 6, "current_frequency_hz": 433000000,
                                             "snapshot_age_sec": 0.4,
                                             "counters": {"buffer_overrun_cnt": 0,
                                                          "dropped_frames_iq": 0,
                                                          "dropped_frames_hwc": 0}},
                                 "hostb:0": {"error": "timeout", "error_kind": "timeout"}}},
            "HEALTH": {"ok": True, "overall": "degraded", "members": {
                "hosta:0": {"state": "up", "health": "ok", "seen_age_sec": 0.3},
                "hostb:0": {"state": "down", "seen_age_sec": 41.0}},
                "unreachable": ["hostb:0"]}})
        code, out, err = self.fleet(c, "status", "--fresh")
        self.assertEqual(code, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "leader {} (term 0)   overall: degraded   members: 2   "
                                   "unreachable: 1".format(c.endpoint))
        self.assertEqual(lines[1].split(), ["NODE", "HEALTH", "SYNC", "FREQ", "AGE",
                                            "OVERRUNS", "DROPS"])
        self.assertEqual(lines[2].split(), ["hosta:0", "ok", "6", "433.000", "MHz", "0.4s",
                                            "0", "0"])
        self.assertEqual(lines[3].split(), ["hostb:0", "down", "-", "-", "41.0s", "-", "-"])
        self.assertIn("NODES STATUS FRESH", c.lines)

    def test_json_pass_through(self):
        reply = {"ok": True, "command": "FREQ", "results": {"a:0": R(True, "ok")},
                 "deadline_ms": 40000}
        c = self.coord({"ALL FREQ": reply})
        code, out, _ = self.fleet(c, "--json", "tune", "433M")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), reply)
        code, out, _ = self.run_cli("--json", "--coordinator", c.endpoint, "fleet", "version")
        self.assertEqual(json.loads(out)["protocol"], 2)

    def test_json_mode_counts_per_target_results(self):
        rebalance = {"ok": True, "instances": 1, "by_node": {
            "a:0": {"ok": True, "applied": False, "reason": "not_confirmed"}}}
        c = self.coord({"ALL FREQ": self.FANOUT, "ALL AGC": self.FANOUT,
                        "RECAL": self.FANOUT, "ALL OQRY": self.FANOUT,
                        "REBALANCE": rebalance,
                        "MEMBERS": {"ok": True, "members": []}})
        for argv in (["tune", "433M"], ["agc"], ["recal"], ["exec", "OQRY"],
                     ["rebalance"]):
            code, out, _ = self.fleet(c, "--json", *argv)
            self.assertEqual(code, 1, argv)
            json.loads(out)  # raw reply printed unchanged
        code, out, _ = self.fleet(c, "--json", "members")
        self.assertEqual(code, 0)

    def test_json_mode_schedule_clear_and_hwc_status_count_targets(self):
        timeout = {"ok": False, "applied": None, "error": "timeout", "phase": "command"}
        bad = self.coord({
            "SCHEDULE CLEAR": {"ok": True, "stopped": {"a:0": R(True, "ok"), "b:0": timeout}},
            "ALL HWSQ": {"ok": True, "results": {
                "a:0": {"ok": True, "data": {"fsm": "IQ_CAL"}},
                "b:0": {"ok": False, "error": "timeout", "phase": "command"}}}})
        good = self.coord({
            "SCHEDULE CLEAR": {"ok": True, "stopped": {"a:0": R(True, "ok")}},
            "ALL HWSQ": {"ok": True, "results": {
                "a:0": {"ok": True, "data": {"fsm": "IQ_CAL"}}}}})
        for c, want in ((bad, 1), (good, 0)):
            for argv in (["schedule", "clear"], ["hwc-status"]):
                text_code, _, _ = self.fleet(c, *argv)
                code, out, _ = self.fleet(c, "--json", *argv)
                self.assertEqual(text_code, want, argv)
                self.assertEqual(code, want, argv)
                json.loads(out)  # raw reply printed unchanged

    def test_version_auth_refusal_reported_not_treated_as_legacy(self):
        c = self.coord(token="s3cret")
        for argv in (["agc", "--targets", "a:0"], ["agc"], ["tune", "433M"],
                     ["gain", "297,297"]):
            code, _, err = self.fleet(c, *argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("Error: auth_required", err)
            self.assertNotIn("legacy", err)
        self.assertEqual(set(c.lines), {"VERSION"}, "no legacy FREQ/GAIN fallback is sent")
        self.assertEqual(len(c.lines), 4, "VERSION is sent once per command")

    def test_version_busy_reply_exit_1(self):
        c = self.coord({"VERSION": {"ok": False, "error": "busy", "retry_in_sec": 2}})
        code, _, err = self.fleet(c, "tune", "433M")
        self.assertEqual(code, 1)
        self.assertIn("Error: busy (retry in 2 s)", err)
        self.assertEqual(c.lines, ["VERSION"])
        bad = self.coord({"VERSION": {"ok": True}})
        code, _, err = self.fleet(bad, "agc")
        self.assertEqual(code, 1)
        self.assertIn("bad VERSION reply", err)

    def test_error_reply_exit_1(self):
        c = self.coord({"ALL FREQ": {"ok": False, "error": "busy", "retry_in_sec": 1}})
        code, _, err = self.fleet(c, "tune", "433M")
        self.assertEqual(code, 1)
        self.assertIn("Error: busy (retry in 1 s)", err)

    def test_legacy_coordinator(self):
        c = FakeCoordinator({"FREQ": {"command": "FREQ", "results": {
            "0": {"ok": True}, "1": {"error": "Connection refused"}}},
            "PING": {"ok": True, "ts": 1.0}}, legacy=True)
        self.servers.append(c)
        code, out, _ = self.fleet(c, "tune", "433M")
        self.assertEqual(c.lines, ["VERSION", "FREQ 433000000"])
        self.assertEqual(code, 1)
        self.assertIn("sent; server does not confirm application", out)
        self.assertIn("error: Connection refused", out)
        code, _, err = self.fleet(c, "park")
        self.assertEqual(code, 1)
        self.assertIn("legacy coordinator", err)


class FakeRouterStats(FakeCoordinator):
    pass


class TestRouter(FleetTestBase):
    STATS = {"ok": True, "uptime_sec": 812.4, "output_port": 7000, "tag_mode": "instance",
             "sources": {"hosta:0": {"instance_id": 0, "connected": True,
                                     "state": "streaming", "frames_received": 1862,
                                     "frames_lost": 0, "cpi_gaps": 0, "restarts": 0,
                                     "last_frame_age_sec": 0.41, "clock_offset_ms": 3}},
             "clients": [{"peer": "10.0.0.5:51234", "connected_sec": 300.2, "queued": 1,
                          "sent_frames": 690, "dropped_frames": 2}],
             "late_frames": 0, "unattributed_frames": 0}

    def test_router_views(self):
        r = FakeRouterStats({"STATS": self.STATS,
                             "SOURCES": {"ok": True, "sources": self.STATS["sources"]},
                             "CLIENTS": {"ok": True, "clients": self.STATS["clients"]}},
                            token="tok")
        self.servers.append(r)
        base = ["--router", r.endpoint, "--auth-token", "tok", "--color", "never", "router"]
        code, out, err = self.run_cli(*base, "stats")
        self.assertEqual(code, 0, err)
        self.assertIn("streaming", out)
        self.assertIn("10.0.0.5:51234", out)
        self.assertTrue(r.requests[-1][2].startswith(b"AUTH tok STATS\n"))
        code, out, _ = self.run_cli(*base, "sources")
        self.assertEqual(code, 0)
        self.assertIn("hosta:0", out)
        self.assertNotIn("10.0.0.5", out)
        code, out, _ = self.run_cli(*base, "clients")
        self.assertIn("10.0.0.5:51234", out)
        code, out, _ = self.run_cli("--json", *base, "stats")
        self.assertEqual(json.loads(out), self.STATS)

    def test_router_endpoint_defaults_to_coordinator_host(self):
        args = cli.build_parser().parse_args(["--coordinator", "hostx:6000", "router", "stats"])
        ctx = cli.Context(args)
        from heimdall_ctl.config import coordinator_endpoints, router_endpoint
        self.assertEqual(router_endpoint(ctx.cfg, coordinator_endpoints(None, "hostx:6000")),
                         ("hostx", 7001))
        cfg_path = os.path.join(self.tmp, "c.ini")
        with open(cfg_path, "w") as f:
            f.write("[federation]\nrouter_stats_endpoint = hosty:7101\n"
                    "coordinator_candidates = h1:6000,h2:6100\n")
        from heimdall_ctl.config import load_config
        cfg = load_config(cfg_path)
        self.assertEqual(coordinator_endpoints(cfg), [("h1", 6000), ("h2", 6100)])
        self.assertEqual(router_endpoint(cfg, coordinator_endpoints(cfg)), ("hosty", 7101))

    def test_router_unreachable_exit_3(self):
        code, _, err = self.run_cli("--router", "127.0.0.1:{}".format(_free_port()),
                                    "router", "stats")
        self.assertEqual(code, 3)
        self.assertIn("Error: 127.0.0.1:", err)


if __name__ == "__main__":
    unittest.main()
