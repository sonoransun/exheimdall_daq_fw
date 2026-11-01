"""Watchdog, systemd units and host packaging (spec H.4, H.5, H.6, H.9; WP-16).

CI lane (Linux and macOS; no sudo, no systemd, no hardware):

* ``util/heimdall_watchdog.py`` runs as a subprocess against a temporary
  registry directory, a FakeStatusServer on a free port and a recording
  ``systemctl`` stub placed first on PATH. Registry "stages" are real sleeping
  processes whose command line carries the role name; every one is killed in
  tearDown.
* The unit files, environment templates, udev rule, logrotate policy and
  kernel tuning file are checked statically against the spec and against the
  scripts they call (flags must exist, paths must map onto this tree).
* ``util/install.sh`` extras (--federation-user, --udev) run with recording
  stubs for sudo/getent/groupadd/useradd/install/udevadm, so nothing on the
  host changes.
* ``systemd-analyze verify`` runs only where it exists and ``--root`` is
  supported (stub /opt/heimdall tree inside a temp root); otherwise skipped.
"""
import configparser
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.path.dirname(FIRMWARE)
UTIL = os.path.join(REPO, "util")
PACKAGING = os.path.join(REPO, "packaging")
SYSTEMD_DIR = os.path.join(PACKAGING, "systemd")
WATCHDOG = os.path.join(UTIL, "heimdall_watchdog.py")
INSTALL_SH = os.path.join(UTIL, "install.sh")
BASH = shutil.which("bash")


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("heimdall_watchdog", WATCHDOG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wd = _load_watchdog()


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _read(path):
    with open(path) as f:
        return f.read()


def ini_text(cpi_size=1048576, decimation_ratio=1, sample_rate=2400000,
             heartbeat_interval=100, status_update_interval=None):
    sui = "" if status_update_interval is None else \
        "status_update_interval = {}\n".format(status_update_interval)
    return ("[daq]\nsample_rate = {fs}\n\n[pre_processing]\ncpi_size = {cpi}\n"
            "decimation_ratio = {dec}\n\n[monitoring]\nen_monitoring = 1\n"
            "en_status_server = 1\nheartbeat_interval = {hb}\n{sui}").format(
                fs=sample_rate, cpi=cpi_size, dec=decimation_ratio, hb=heartbeat_interval,
                sui=sui)


class FakeStatusServer(threading.Thread):
    """:5002-style server: one command per connection, JSON + newline."""

    def __init__(self, doc):
        super().__init__(daemon=True)
        self.doc = doc
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.queries = 0
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                try:
                    if conn.recv(1024).strip().upper() == b"STATUS":
                        self.queries += 1
                        conn.sendall(json.dumps(self.doc).encode() + b"\n")
                except OSError:
                    pass

    def stop(self):
        self._halt.set()
        self.sock.close()


def free_port():
    """A port nothing listens on (bound and released)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


SYSTEMCTL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$WD_SYSTEMCTL_LOG"
case "$1" in
    restart) exit "${WD_RESTART_RC:-0}" ;;
    *) exit 0 ;;
esac
"""


class WatchdogCase(unittest.TestCase):
    """Temp registry + stub systemctl + helpers to run the watchdog."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hd_watchdog_")
        self.run_dir = os.path.join(self.tmp, "run")
        os.makedirs(self.run_dir)
        self.stub_dir = os.path.join(self.tmp, "bin")
        self.syslog = os.path.join(self.tmp, "systemctl.log")
        open(self.syslog, "w").close()
        _write(os.path.join(self.stub_dir, "systemctl"), SYSTEMCTL_STUB, 0o755)
        self.procs = []
        self.servers = []

    def tearDown(self):
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=5)
            except OSError:
                pass
        for s in self.servers:
            s.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures -----------------------------------------------------------

    def stage(self, role):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", role],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p.pid

    def server(self, doc):
        s = FakeStatusServer(doc)
        s.start()
        self.servers.append(s)
        return s

    def entry(self, n, launcher="systemd-supervised", status_port=None, started_ago=1000,
              unit=None, ini=None, live=True, started_at=None):
        cwd = os.path.join(self.tmp, "inst{}".format(n), "Firmware")
        os.makedirs(cwd, exist_ok=True)
        _write(os.path.join(cwd, "daq_chain_config.ini"), ini if ini is not None else ini_text())
        if unit is None:
            unit = ("heimdall-daq-supervised.service" if n == 0
                    else "heimdall-daq-supervised@{}.service".format(n))
        pids = {"delay_sync": self.stage("delay_sync") if live else 999999,
                "rebuffer": self.stage("rebuffer") if live else 999998}
        doc = {"schema": 1, "instance_id": n, "mode": "hw", "cwd": cwd,
               "started_at": int(started_at if started_at is not None
                                 else time.time() - started_ago),
               "guard": "refuse", "launcher": launcher, "unit": unit,
               "ports": {"iq": 5000, "ctl": 5001, "status": status_port, "events": None,
                         "zmq": 1130},
               "pids": pids}
        _write(os.path.join(self.run_dir, "inst{}.json".format(n)), json.dumps(doc))
        return doc

    def run_wd(self, *args, **env):
        e = dict(os.environ)
        e["PATH"] = self.stub_dir + os.pathsep + e.get("PATH", "")
        e["WD_SYSTEMCTL_LOG"] = self.syslog
        e.update(env)
        p = subprocess.run([sys.executable, WATCHDOG, "--run-dir", self.run_dir] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e,
                           timeout=60)
        return p.returncode, p.stdout.decode()

    def calls(self):
        return [line for line in _read(self.syslog).splitlines() if line]

    def state(self):
        return json.loads(_read(os.path.join(self.run_dir, "watchdog.json")))["entries"]


class TestWatchdogRestart(WatchdogCase):

    def test_restart_after_exactly_three_failures(self):
        doc = self.entry(1, status_port=free_port())
        unit = doc["unit"]
        for i in (1, 2):
            rc, out = self.run_wd()
            self.assertEqual(rc, 0, out)
            self.assertIn("failed check {}/3".format(i), out)
            self.assertEqual(self.calls(), [], out)
        rc, out = self.run_wd()
        self.assertEqual(rc, 0, out)
        self.assertIn("failed check 3/3", out)
        self.assertEqual(self.calls(), ["reset-failed " + unit, "restart " + unit])
        self.assertIn("restarted " + unit, out)
        key = "1@{}".format(doc["started_at"])
        self.assertEqual(self.state()[key]["fails"], 0)

    def test_fails_flag(self):
        self.entry(2, status_port=free_port())
        rc, out = self.run_wd("--fails", "1")
        self.assertEqual(rc, 0, out)
        self.assertEqual([c.split()[0] for c in self.calls()],
                         ["reset-failed", "restart"])

    def test_stopped_chain_is_never_restarted(self):
        # A unit that hit its start limit has no running stage (its
        # ExecStopPost --clean removes the registry entry; a leftover entry
        # has only dead pids): the watchdog leaves it alone.
        self.entry(1, status_port=free_port())
        for p in self.procs:
            p.kill()
            p.wait(timeout=5)
        for _ in range(3):
            rc, out = self.run_wd("--fails", "1")
            self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])

    def test_restart_failure_exits_1(self):
        self.entry(1, status_port=free_port())
        rc, out = self.run_wd("--fails", "1", WD_RESTART_RC="5")
        self.assertEqual(rc, 1, out)
        self.assertIn("systemctl restart heimdall-daq-supervised@1.service failed (exit 5)", out)

    def test_manual_and_oneshot_never_restarted(self):
        dead = free_port()
        self.entry(1, launcher="manual", status_port=dead, unit="")
        self.entry(2, launcher="systemd-oneshot", status_port=dead,
                   unit="heimdall-daq@2.service")
        self.entry(3, launcher="supervised", status_port=dead, unit="")
        for _ in range(4):
            rc, out = self.run_wd("--fails", "1")
            self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])
        # reported once per chain start, not on every run
        rc, out = self.run_wd("--fails", "1")
        self.assertNotIn("not watched", out)
        rc, out = self.run_wd("--fails", "1", "--verbose")
        for n, launcher in ((1, "manual"), (2, "systemd-oneshot"), (3, "supervised")):
            self.assertIn("instance {}".format(n), out)
            self.assertIn("launcher '{}'".format(launcher), out)

    def test_first_run_reports_unwatched_entries(self):
        self.entry(1, launcher="manual", status_port=free_port(), unit="")
        rc, out = self.run_wd()
        self.assertIn("instance 1: started by launcher 'manual'", out)
        self.assertIn("not watched", out)

    def test_entry_without_status_port_skipped(self):
        srv = self.server({"frame_count": 1, "pipeline_health": "error"})
        self.entry(1, status_port=None)
        for _ in range(3):
            rc, out = self.run_wd("--fails", "1")
            self.assertEqual(rc, 0, out)
        self.assertEqual(srv.queries, 0)
        self.assertEqual(self.calls(), [])
        rc, out = self.run_wd("--verbose")
        self.assertIn("status server disabled; not watched", out)

    def test_dead_entry_ignored_and_key_dropped(self):
        doc = self.entry(1, status_port=free_port())
        self.run_wd()
        key = "1@{}".format(doc["started_at"])
        self.assertEqual(self.state()[key]["fails"], 1)
        for p in self.procs:
            p.kill()
            p.wait(timeout=5)
        rc, out = self.run_wd("--fails", "1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state(), {})

    def test_invalid_unit_never_passed_to_systemctl(self):
        self.entry(1, status_port=free_port(), unit="--now")
        rc, out = self.run_wd("--fails", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn("no valid unit name ('--now'), so it cannot be restarted; not watched",
                      out)
        self.assertEqual(self.calls(), [])
        for _ in range(3):
            rc, out = self.run_wd("--fails", "1")
            self.assertEqual(rc, 0, out)
            self.assertNotIn("no valid unit name", out, "reported once per chain start")
            self.assertNotIn("failed check", out)
        self.assertEqual(self.calls(), [])
        self.assertNotIn("fails", list(self.state().values())[0])

    def test_dry_run(self):
        self.entry(1, status_port=free_port())
        rc, out = self.run_wd("--fails", "1", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("would restart heimdall-daq-supervised@1.service (dry run)", out)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "watchdog.json")))

    def test_missing_run_dir(self):
        rc, out = self.run_wd("--run-dir", os.path.join(self.tmp, "absent"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])

    def test_usage_errors(self):
        for args in (["--fails", "0"], ["--grace", "soon"], ["--stall-sec", "-1"],
                     ["--timeout", "0"]):
            with self.subTest(args=args):
                rc, out = self.run_wd(*args)
                self.assertEqual(rc, 2, out)


class TestWatchdogHealth(WatchdogCase):

    def test_health_error_and_stale_count_ok_resets(self):
        srv = self.server({"frame_count": 1, "pipeline_health": "error"})
        doc = self.entry(1, status_port=srv.port)
        key = "1@{}".format(doc["started_at"])
        self.run_wd()
        srv.doc = {"frame_count": 2, "pipeline_health": "stale"}
        rc, out = self.run_wd()
        self.assertIn("pipeline_health is stale - failed check 2/3", out)
        srv.doc = {"frame_count": 3, "pipeline_health": "degraded"}
        rc, out = self.run_wd()
        self.assertIn("healthy again after 2 failed check(s)", out)
        self.assertEqual(self.state()[key]["fails"], 0)
        srv.doc = {"frame_count": 4, "pipeline_health": "error"}
        self.run_wd()
        self.run_wd()
        self.assertEqual(self.calls(), [])
        self.run_wd()
        self.assertEqual(len(self.calls()), 2)

    def test_grace_honoured(self):
        srv = self.server({"frame_count": 1, "pipeline_health": "error"})
        self.entry(1, status_port=srv.port, started_ago=10)
        for _ in range(4):
            rc, out = self.run_wd("--fails", "1")
            self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(srv.queries, 0, "STATUS is not queried inside the 120 s floor")

    def test_auto_grace_default_and_from_stale_after(self):
        # 200 s after the start: past the 120 s floor, inside the default
        # max(120, 3 x 132) = 396 s grace -> queried, not counted
        srv = self.server({"frame_count": 1, "pipeline_health": "error"})
        self.entry(1, status_port=srv.port, started_ago=200)
        rc, out = self.run_wd("--fails", "1")
        self.assertEqual(self.calls(), [], out)
        self.assertEqual(srv.queries, 1)
        # extended STATUS: stale_after_sec 14.1 -> grace max(120, 42.3) = 120 s
        srv.doc = {"frame_count": 1, "pipeline_health": "error", "stale_after_sec": 14.1}
        rc, out = self.run_wd("--fails", "1")
        self.assertEqual(len(self.calls()), 2, out)

    def test_explicit_grace(self):
        srv = self.server({"frame_count": 1, "pipeline_health": "error"})
        self.entry(1, status_port=srv.port, started_ago=30)
        rc, out = self.run_wd("--fails", "1", "--grace", "60")
        self.assertEqual(self.calls(), [], out)
        rc, out = self.run_wd("--fails", "1", "--grace", "10")
        self.assertEqual(len(self.calls()), 2, out)

    def test_new_started_at_resets_counter(self):
        port = free_port()
        old = self.entry(1, status_port=port, started_ago=2000)
        self.run_wd()
        self.run_wd()
        self.assertEqual(self.state()["1@{}".format(old["started_at"])]["fails"], 2)
        new = self.entry(1, status_port=port, started_ago=1000)
        rc, out = self.run_wd()
        self.assertIn("failed check 1/3", out)
        st = self.state()
        self.assertEqual(list(st), ["1@{}".format(new["started_at"])])
        self.run_wd()
        self.assertEqual(self.calls(), [])

    def test_listen_address_used_for_status(self):
        srv = self.server({"frame_count": 1, "pipeline_health": "ok"})
        ini = ini_text().replace("[daq]\n", "[daq]\nlisten_address = 127.0.0.1\n")
        self.entry(1, status_port=srv.port, ini=ini)
        rc, out = self.run_wd("--verbose")
        self.assertIn("instance 1 (heimdall-daq-supervised@1.service): healthy", out)
        self.assertEqual(srv.queries, 1)


class TestWatchdogStall(WatchdogCase):

    def backdate(self, doc, frame_count, ago):
        key = "{}@{}".format(doc["instance_id"], doc["started_at"])
        state = {"schema": 1, "entries": {key: {"fails": 0, "frame_count": frame_count,
                                                "frame_count_since": time.time() - ago}}}
        _write(os.path.join(self.run_dir, "watchdog.json"), json.dumps(state))

    def test_auto_threshold_large_cpi_is_at_least_525s(self):
        path = os.path.join(self.tmp, "big.ini")
        _write(path, ini_text(cpi_size=4200000, sample_rate=2400000))
        thr = wd.ini_stall_threshold(path)
        self.assertGreaterEqual(thr, 525.0)
        self.assertAlmostEqual(thr, 3 * 100 * 1.75 + 30)

    def test_auto_threshold_formula_variants(self):
        path = os.path.join(self.tmp, "a.ini")
        _write(path, ini_text())
        self.assertAlmostEqual(wd.ini_stall_threshold(path), 3 * 100 * 1048576 / 2.4e6 + 30)
        _write(path, ini_text(status_update_interval=10))
        self.assertAlmostEqual(wd.ini_stall_threshold(path), 3 * 10 * 1048576 / 2.4e6 + 30)
        _write(path, ini_text(heartbeat_interval=0))
        self.assertIsNone(wd.ini_stall_threshold(path))
        _write(path, "[daq]\nsample_rate = 2400000\n")
        self.assertIsNone(wd.ini_stall_threshold(path))
        self.assertIsNone(wd.ini_stall_threshold(os.path.join(self.tmp, "missing.ini")))
        self.assertAlmostEqual(wd.auto_stall_threshold({"stale_after_sec": 53.4}, path),
                               3 * 53.4 + 30)
        self.assertEqual(wd.auto_grace({}), 396.0)
        self.assertEqual(wd.auto_grace({"stale_after_sec": 14.1}), 120.0)
        self.assertEqual(wd.auto_grace({"stale_after_sec": 53.4}), 3 * 53.4)
        self.assertIsNone(wd.stale_after_from_status({"stale_after_sec": None}))
        self.assertIsNone(wd.stale_after_from_status({"stale_after_sec": True}))

    def test_slowly_refreshing_healthy_entry_not_restarted(self):
        # T_fp = 1.75 s, k = 100: threshold 555 s; frame_count frozen 400 s
        srv = self.server({"frame_count": 7, "pipeline_health": "ok"})
        doc = self.entry(1, status_port=srv.port,
                         ini=ini_text(cpi_size=4200000, sample_rate=2400000))
        self.backdate(doc, 7, 400)
        for _ in range(4):
            rc, out = self.run_wd("--fails", "1")
            self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])

    def test_frozen_frame_count_fails(self):
        # T_fp = 0.437 s, k = 100: threshold 161 s; frozen 400 s -> failure
        srv = self.server({"frame_count": 7, "pipeline_health": "ok"})
        doc = self.entry(1, status_port=srv.port)
        self.backdate(doc, 7, 400)
        rc, out = self.run_wd("--fails", "1")
        self.assertIn("frame_count 7 unchanged for 400 s (threshold 161 s)", out)
        self.assertEqual(len(self.calls()), 2)

    def test_advancing_frame_count_is_healthy(self):
        srv = self.server({"frame_count": 8, "pipeline_health": "ok"})
        doc = self.entry(1, status_port=srv.port)
        self.backdate(doc, 7, 400)
        rc, out = self.run_wd("--fails", "1")
        self.assertEqual(self.calls(), [], out)
        st = self.state()["1@{}".format(doc["started_at"])]
        self.assertEqual(st["frame_count"], 8)
        self.assertLess(time.time() - st["frame_count_since"], 30)

    def test_stall_threshold_from_extended_status(self):
        srv = self.server({"frame_count": 7, "pipeline_health": "ok", "stale_after_sec": 14.1})
        doc = self.entry(1, status_port=srv.port,
                         ini=ini_text(cpi_size=4200000, sample_rate=2400000))
        self.backdate(doc, 7, 100)
        rc, out = self.run_wd("--fails", "1")
        self.assertIn("(threshold 72 s)", out)

    def test_explicit_stall_sec(self):
        srv = self.server({"frame_count": 7, "pipeline_health": "ok"})
        doc = self.entry(1, status_port=srv.port)
        self.backdate(doc, 7, 100)
        rc, out = self.run_wd("--fails", "1", "--stall-sec", "1000")
        self.assertEqual(self.calls(), [], out)
        rc, out = self.run_wd("--fails", "1", "--stall-sec", "50")
        self.assertEqual(len(self.calls()), 2, out)

    def test_no_frame_count_or_never_refreshed_skips_stall(self):
        srv = self.server({"pipeline_health": "ok"})
        doc = self.entry(1, status_port=srv.port)
        self.backdate(doc, 7, 10000)
        rc, out = self.run_wd("--fails", "1")
        self.assertIn("STATUS has no frame_count; stall check skipped", out)
        self.assertEqual(self.calls(), [])
        srv.doc = {"frame_count": 7, "pipeline_health": "ok"}
        doc2 = self.entry(2, status_port=srv.port, ini=ini_text(heartbeat_interval=0))
        self.backdate(doc2, 7, 10000)
        rc, out = self.run_wd("--fails", "1")
        self.assertIn("stall threshold unknown", out)
        self.assertEqual(self.calls(), [])


class TestPidRoleAlive(unittest.TestCase):

    def test_role_match_and_dead(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "rebuffer"])
        try:
            self.assertTrue(wd.pid_role_alive(p.pid, "rebuffer"))
            self.assertFalse(wd.pid_role_alive(p.pid, "decimate"))
        finally:
            p.kill()
            p.wait(timeout=5)
        self.assertFalse(wd.pid_role_alive(p.pid, "rebuffer"))
        self.assertFalse(wd.pid_role_alive("x", "rebuffer"))
        self.assertFalse(wd.pid_role_alive(0, "rebuffer"))
        self.assertFalse(wd.pid_role_alive(2 ** 64, "rebuffer"))
        self.assertFalse(wd.pid_role_alive(str(2 ** 64), "rebuffer"))

    def test_zombie_is_dead(self):
        p = subprocess.Popen([sys.executable, "-c", "pass", "rebuffer"])
        deadline = time.time() + 10
        while time.time() < deadline:
            state, _ = wd._proc_state_args(p.pid)
            if state in ("Z", None):
                break
            time.sleep(0.05)
        try:
            self.assertFalse(wd.pid_role_alive(p.pid, "rebuffer"))
        finally:
            p.wait(timeout=5)


# ---------------------------------------------------------------------------
# Unit files and templates
# ---------------------------------------------------------------------------

def parse_unit(path):
    """{section: [(key, value)]} of a systemd unit (comments on own lines)."""
    out, section = {}, None
    with open(path) as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith(("#", ";")):
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                section = m.group(1)
                out.setdefault(section, [])
                continue
            key, _, value = line.partition("=")
            out[section].append((key.strip(), value.strip()))
    return out


def values(unit, section, key):
    return [v for k, v in unit.get(section, []) if k == key]


def value(unit, section, key):
    v = values(unit, section, key)
    return v[-1] if v else None


UNITS = ["heimdall-daq.service", "heimdall-daq@.service", "heimdall-daq-supervised.service",
         "heimdall-daq-supervised@.service", "heimdall-coordinator.service",
         "heimdall-iq-router.service", "heimdall-host-tuning.service",
         "heimdall-daq-watchdog.service", "heimdall-daq-watchdog.timer"]


def to_tree(path):
    """Map an /opt/heimdall path of a unit onto this source tree."""
    path = path.replace("/opt/heimdall/instances/%i/Firmware", "/opt/heimdall/Firmware")
    assert path.startswith("/opt/heimdall/"), path
    return os.path.join(REPO, path[len("/opt/heimdall/"):])


class TestUnitFiles(unittest.TestCase):

    def unit(self, name):
        return parse_unit(os.path.join(SYSTEMD_DIR, name))

    def test_all_units_present_and_no_inline_comments(self):
        for name in UNITS:
            with self.subTest(unit=name):
                path = os.path.join(SYSTEMD_DIR, name)
                self.assertTrue(os.path.isfile(path))
                for sec, items in parse_unit(path).items():
                    for k, v in items:
                        self.assertNotRegex(v, r"(^|\s)[#;]", "{}: inline comment in {}=".format(
                            name, k))
                        self.assertIn(sec, ("Unit", "Service", "Timer", "Install"))

    def test_exec_paths_exist_in_tree_and_flags_are_known(self):
        usage = {
            "daq_supervise.sh": ["--instance", "--healthcheck"],
            "daq_stop.sh": ["--clean"],
            "federation_start.sh": ["--foreground", "--config", "--state-dir", "--log-dir",
                                    "--pid-dir"],
            "host_tuning.sh": ["--apply"],
        }
        for name in UNITS:
            u = self.unit(name)
            for key in ("ExecStart", "ExecStop", "ExecStopPost"):
                for cmd in values(u, "Service", key):
                    with self.subTest(unit=name, key=key):
                        argv = shlex.split(cmd)
                        target = to_tree(argv[0])
                        self.assertTrue(os.path.isfile(target), target)
                        self.assertTrue(os.access(target, os.X_OK), target + " not executable")
                        script = os.path.basename(target)
                        text = _read(target)
                        for flag in [a for a in argv[1:] if a.startswith("--")]:
                            self.assertIn(flag, text, "{} does not know {}".format(script, flag))
                        for flag in usage.get(script, []):
                            if flag in argv:
                                self.assertIn(flag, text)

    def test_oneshot_units(self):
        u = self.unit("heimdall-daq.service")
        self.assertEqual(value(u, "Service", "Type"), "oneshot")
        self.assertEqual(value(u, "Service", "ExecStop"), "/opt/heimdall/Firmware/daq_stop.sh 0")
        self.assertEqual(values(u, "Service", "EnvironmentFile"), ["-/etc/default/heimdall-daq"])
        self.assertEqual(value(u, "Unit", "ConditionPathExists"),
                         "/opt/heimdall/Firmware/_daq_core/rtl_daq.out")
        self.assertEqual(value(u, "Service", "Environment"), "HEIMDALL_LAUNCHER=systemd-oneshot")
        self.assertEqual(value(u, "Unit", "Conflicts"), "heimdall-daq-supervised.service")
        self.assertEqual(value(u, "Service", "KillMode"), "control-group")
        t = self.unit("heimdall-daq@.service")
        self.assertEqual(value(t, "Service", "Type"), "oneshot")
        self.assertEqual(values(t, "Service", "EnvironmentFile"),
                         ["-/etc/default/heimdall-daq", "-/etc/default/heimdall-daq@%i"])
        self.assertEqual(value(t, "Unit", "ConditionPathExists"),
                         "/opt/heimdall/instances/%i/Firmware/daq_chain_config.ini")
        self.assertEqual(value(t, "Service", "Environment"), "HEIMDALL_LAUNCHER=systemd-oneshot")
        self.assertEqual(value(t, "Unit", "Conflicts"), "heimdall-daq-supervised@%i.service")
        self.assertEqual(value(t, "Service", "ExecStop"),
                         "/opt/heimdall/instances/%i/Firmware/daq_stop.sh %i")

    def test_supervised_units(self):
        for name, wd_dir, n, conflict in (
                ("heimdall-daq-supervised@.service", "/opt/heimdall/instances/%i/Firmware", "%i",
                 "heimdall-daq@%i.service"),
                ("heimdall-daq-supervised.service", "/opt/heimdall/Firmware", "0",
                 "heimdall-daq.service")):
            with self.subTest(unit=name):
                u = self.unit(name)
                self.assertEqual(value(u, "Service", "Type"), "simple")
                self.assertEqual(value(u, "Service", "WorkingDirectory"), wd_dir)
                self.assertEqual(value(u, "Service", "ExecStart"),
                                 "{}/daq_supervise.sh --instance {} --healthcheck auto".format(
                                     wd_dir, n))
                self.assertEqual(value(u, "Service", "ExecStopPost"),
                                 "{}/daq_stop.sh {} --clean".format(wd_dir, n))
                self.assertEqual(value(u, "Service", "Restart"), "on-failure")
                self.assertEqual(value(u, "Service", "RestartSec"), "5")
                self.assertEqual(value(u, "Service", "RestartPreventExitStatus"), "1 2 3 4")
                self.assertEqual(value(u, "Service", "Environment"), "UNIT_NAME=%n")
                self.assertEqual(value(u, "Service", "User"), "root")
                self.assertEqual(value(u, "Service", "LimitRTPRIO"), "99")
                self.assertEqual(value(u, "Service", "LimitMEMLOCK"), "infinity")
                self.assertEqual(value(u, "Service", "KillMode"), "control-group")
                self.assertEqual(value(u, "Unit", "StartLimitIntervalSec"), "300")
                self.assertEqual(value(u, "Unit", "StartLimitBurst"), "5")
                self.assertEqual(value(u, "Unit", "Conflicts"), conflict)
                self.assertIn("heimdall-host-tuning.service", value(u, "Unit", "After"))
                self.assertEqual(values(u, "Unit", "ConditionPathExists"),
                                 [wd_dir + "/daq_chain_config.ini",
                                  "/opt/heimdall/Firmware/_daq_core/rtl_daq.out"])
                self.assertIn("-/etc/default/heimdall-daq", values(u, "Service", "EnvironmentFile"))

    def test_conflicts_are_symmetric(self):
        pairs = {}
        for name in UNITS:
            for c in values(self.unit(name), "Unit", "Conflicts"):
                for other in c.split():
                    pairs.setdefault(name.replace("%i", ""), set()).add(other.replace("%i", ""))
        for a, others in pairs.items():
            for b in others:
                self.assertIn(a, pairs.get(b, set()), "{} conflicts with {} but not back".format(a, b))

    def test_federation_units_users_and_directories(self):
        for name, service, state in (("heimdall-coordinator.service", "coordinator", True),
                                     ("heimdall-iq-router.service", "router", False)):
            with self.subTest(unit=name):
                u = self.unit(name)
                self.assertEqual(value(u, "Service", "User"), "heimdall")
                self.assertEqual(value(u, "Service", "Group"), "heimdall")
                self.assertEqual(value(u, "Unit", "ConditionPathExists"),
                                 "/etc/heimdall/federation.ini")
                self.assertEqual(value(u, "Service", "Restart"), "always")
                self.assertEqual(value(u, "Service", "RestartSec"), "3")
                self.assertEqual(value(u, "Service", "RuntimeDirectoryPreserve"), "yes")
                self.assertEqual(values(u, "Service", "EnvironmentFile"),
                                 ["-/etc/default/heimdall-federation"])
                argv = shlex.split(value(u, "Service", "ExecStart"))
                self.assertEqual(argv[1:3], [service, "--foreground"])
                opts = dict(zip(argv[3::2], argv[4::2]))
                self.assertEqual(opts["--config"], "/etc/heimdall/federation.ini")
                logs = value(u, "Service", "LogsDirectory")
                self.assertTrue(logs, "User=heimdall units must declare LogsDirectory")
                self.assertEqual(opts["--log-dir"], "/var/log/" + logs)
                self.assertEqual(opts["--pid-dir"], "/run/" + value(u, "Service",
                                                                      "RuntimeDirectory"))
                if state:
                    sd = value(u, "Service", "StateDirectory")
                    self.assertTrue(sd, "the coordinator must declare StateDirectory")
                    self.assertEqual(opts["--state-dir"], "/var/lib/" + sd)
                else:
                    self.assertIsNone(value(u, "Service", "StateDirectory"))
                    self.assertNotIn("--state-dir", opts)

    def test_host_tuning_watchdog_and_timer(self):
        h = self.unit("heimdall-host-tuning.service")
        self.assertEqual(value(h, "Service", "Type"), "oneshot")
        self.assertEqual(value(h, "Service", "RemainAfterExit"), "yes")
        self.assertEqual(value(h, "Service", "ExecStart"), "/opt/heimdall/util/host_tuning.sh --apply")
        self.assertEqual(set(value(h, "Unit", "Before").split()),
                         {"heimdall-daq.service", "heimdall-daq-supervised.service"})
        self.assertEqual(value(h, "Install", "WantedBy"), "multi-user.target")
        w = self.unit("heimdall-daq-watchdog.service")
        self.assertEqual(value(w, "Service", "Type"), "oneshot")
        argv = shlex.split(value(w, "Service", "ExecStart"))
        self.assertEqual(argv, ["/opt/heimdall/util/heimdall_watchdog.py", "--run-dir",
                                "/run/heimdall", "--fails", "3"])
        args = wd.parse_args(argv[1:])
        self.assertEqual((args.run_dir, args.fails), ("/run/heimdall", 3))
        self.assertNotIn("Install", w, "the watchdog service is started by its timer only")
        self.assertFalse([k for k, _ in w.get("Unit", []) if k.startswith("Condition")],
                         "a skipped run never activates the oneshot, which would leave "
                         "OnUnitActiveSec= without a base and stop the timer")
        t = self.unit("heimdall-daq-watchdog.timer")
        self.assertEqual(value(t, "Timer", "OnBootSec"), "120")
        self.assertEqual(value(t, "Timer", "OnUnitActiveSec"), "30")
        self.assertEqual(value(t, "Timer", "Unit"), "heimdall-daq-watchdog.service")
        self.assertEqual(value(t, "Install", "WantedBy"), "timers.target")
        self.assertTrue(_read(WATCHDOG).startswith("#!/usr/bin/env python3\n"))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_systemd_analyze_verify(self):
        root = tempfile.mkdtemp(prefix="hd_units_")
        try:
            probe = subprocess.run(["systemd-analyze", "verify", "--root=" + root, "--help"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            if b"unrecognized option" in probe.stdout or b"Unknown option" in probe.stdout:
                self.skipTest("systemd-analyze verify has no --root")
            stub_exec = "#!/bin/sh\nexit 0\n"
            for rel in ("Firmware/daq_start_sm.sh", "Firmware/daq_stop.sh",
                        "Firmware/daq_supervise.sh", "Firmware/federation_start.sh",
                        "Firmware/_daq_core/rtl_daq.out", "util/host_tuning.sh",
                        "util/heimdall_watchdog.py", "instances/1/Firmware/daq_start_sm.sh",
                        "instances/1/Firmware/daq_stop.sh", "instances/1/Firmware/daq_supervise.sh"):
                _write(os.path.join(root, "opt/heimdall", rel), stub_exec, 0o755)
            # the stock units (targets the heimdall units order against)
            for sysdir in ("/usr/lib/systemd/system", "/lib/systemd/system"):
                if os.path.isdir(sysdir) and not os.path.islink(sysdir):
                    shutil.copytree(sysdir, os.path.join(root, sysdir.lstrip("/")),
                                    symlinks=True, dirs_exist_ok=True)
                    break
            unit_dir = os.path.join(root, "etc/systemd/system")
            os.makedirs(unit_dir)
            files = []
            for name in UNITS:
                dst_name = name.replace("@.", "@1.")
                shutil.copy(os.path.join(SYSTEMD_DIR, name), os.path.join(unit_dir, dst_name))
                if "@." in name:
                    shutil.copy(os.path.join(SYSTEMD_DIR, name), os.path.join(unit_dir, name))
                files.append(os.path.join(unit_dir, dst_name))
            p = subprocess.run(["systemd-analyze", "verify", "--root=" + root,
                                "--man=no"] + files,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            out = p.stdout.decode(errors="replace")
            relevant = [l for l in out.splitlines() if "heimdall" in l]
            self.assertEqual(p.returncode, 0, out)
            self.assertEqual(relevant, [], out)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestTemplatesAndHostFiles(unittest.TestCase):

    def env_lines(self, name):
        out = {}
        for line in _read(os.path.join(PACKAGING, "default", name)).splitlines():
            if not line or line.startswith("#"):
                continue
            self.assertRegex(line, r"^[A-Z_][A-Z0-9_]*=[^ \t\"'#]*$", line)
            k, v = line.split("=", 1)
            out[k] = v
        return out

    def test_env_templates(self):
        path = ("/opt/miniforge3/envs/kraken/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:"
                "/usr/bin:/sbin:/bin")
        self.assertEqual(self.env_lines("heimdall-daq"),
                         {"PATH": path, "HEIMDALL_RUN_DIR": "/run/heimdall"})
        self.assertEqual(self.env_lines("heimdall-federation"), {"PATH": path})
        fed = _read(os.path.join(PACKAGING, "default", "heimdall-federation"))
        for var in ("HEIMDALL_FED_LOG_DIR", "HEIMDALL_FED_PID_DIR", "HEIMDALL_FED_STATE_DIR"):
            self.assertRegex(fed, r"(?m)^#" + var + "=")
            self.assertIn(var, _read(os.path.join(FIRMWARE, "federation_start.sh")))

    def test_env_template_consumed_by_provisioning(self):
        prov = _read(os.path.join(UTIL, "provision_instance.py"))
        self.assertIn('"packaging", "default", "heimdall-daq"', prov)

    def test_udev_rule(self):
        lines = [l for l in _read(os.path.join(PACKAGING, "udev", "99-heimdall-rtlsdr.rules"))
                 .splitlines() if l and not l.startswith("#")]
        self.assertEqual(lines, ['SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", '
                                 'ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0664"'])

    @unittest.skipUnless(shutil.which("udevadm"), "udevadm not available")
    def test_udev_rule_verifies(self):
        rule = os.path.join(PACKAGING, "udev", "99-heimdall-rtlsdr.rules")
        p = subprocess.run(["udevadm", "verify", rule], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60)
        out = p.stdout.decode(errors="replace")
        if p.returncode != 0 and ("Unknown command" in out or "unknown command" in out.lower()):
            self.skipTest("udevadm has no verify command (systemd < 254)")
        self.assertEqual(p.returncode, 0, out)

    def test_logrotate(self):
        text = _read(os.path.join(PACKAGING, "logrotate", "heimdall"))
        body = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        brace = body.index(next(l for l in body if l.endswith("{")))
        globs = body[:brace] + [body[brace].rstrip("{").strip()]
        self.assertEqual(globs, ["/opt/heimdall/Firmware/_logs/*.log",
                                 "/opt/heimdall/Firmware/_logs/inst*/*.log",
                                 "/opt/heimdall/instances/*/Firmware/_logs/*.log",
                                 "/opt/heimdall/instances/*/Firmware/_logs/inst*/*.log",
                                 "/opt/heimdall/Firmware/_logs/federation/*.log",
                                 "/var/log/heimdall/*.log"])
        for directive in ("copytruncate", "missingok", "notifempty", "rotate 7", "size 50M"):
            self.assertIn(directive, body)
        self.assertIn("archive", text, "the header documents the per-start archive")

    @unittest.skipUnless(shutil.which("logrotate"), "logrotate not available")
    def test_logrotate_parses(self):
        tmp = tempfile.mkdtemp(prefix="hd_logrotate_")
        try:
            state = os.path.join(tmp, "state")
            p = subprocess.run(["logrotate", "-d", "-s", state,
                                os.path.join(PACKAGING, "logrotate", "heimdall")],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            out = p.stdout.decode(errors="replace")
            self.assertEqual(p.returncode, 0, out)
            self.assertNotIn("error:", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_kernel_tuning_has_no_inline_comments(self):
        active = {}
        for line in _read(os.path.join(UTIL, "kernel_tuning.conf")).splitlines():
            if not line.strip() or line.lstrip().startswith(("#", ";")):
                continue
            self.assertNotIn("#", line, "inline comment would be part of the value: " + line)
            key, sep, val = line.partition("=")
            self.assertEqual(sep, "=", line)
            self.assertRegex(key, r"^[a-z0-9_.]+$")
            active[key] = val
        self.assertEqual(active["kernel.sched_rt_runtime_us"], "-1")
        self.assertEqual(active["net.ipv4.tcp_rmem"], "4096 262144 16777216")
        # NUMA keys stay active (sysctl -e / systemd-sysctl skip them where absent)
        self.assertEqual(active["vm.zone_reclaim_mode"], "0")
        self.assertEqual(active["kernel.numa_balancing"], "0")
        for gone in ("kernel.sched_latency_ns", "kernel.sched_min_granularity_ns",
                     "kernel.sched_wakeup_granularity_ns", "kernel.sched_migration_cost_ns",
                     "net.ipv4.tcp_low_latency"):
            self.assertNotIn(gone, active)
            self.assertIn("#" + gone + "=", _read(os.path.join(UTIL, "kernel_tuning.conf")))

    def test_system_tuning_continues_past_unknown_keys(self):
        try:
            import psutil  # noqa: F401  (system_tuning imports it at module level)
        except ImportError:
            self.skipTest("psutil not installed")
        spec = importlib.util.spec_from_file_location("system_tuning",
                                                      os.path.join(UTIL, "system_tuning.py"))
        st = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(st)
        for rc, expect in ((0, "Applied kernel tuning parameters"),
                           (255, "sysctl rejected some settings")):
            with self.subTest(rc=rc), \
                    mock.patch.object(st.Path, "write_text") as wt, \
                    mock.patch.object(st.subprocess, "run",
                                      return_value=subprocess.CompletedProcess([], rc)) as run, \
                    mock.patch("builtins.print") as pr:
                st.apply_sysctl_config()
                wt.assert_called_once()
                run.assert_called_once_with(
                    ["sysctl", "-e", "-p", "/etc/sysctl.d/99-heimdall-rt.conf"], check=False)
                printed = " ".join(str(c.args[0]) for c in pr.call_args_list if c.args)
                self.assertIn(expect, printed)


# ---------------------------------------------------------------------------
# util/install.sh
# ---------------------------------------------------------------------------

STUB = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$INSTALL_LOG"
{body}
"""


@unittest.skipUnless(BASH, "bash not available")
class TestInstallScript(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hd_install_")
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "calls.log")
        open(self.log, "w").close()
        stubs = {
            # sudo runs the (stubbed) command without escalating
            "sudo": 'exec "$@"',
            "id": 'if [ "$1" = -u ]; then echo 1000; else echo "uid=1000"; fi',
            "getent": 'grep -qx "$1 $2" "$INSTALL_DB" 2>/dev/null',
            "groupadd": 'for a; do last=$a; done; echo "group $last" >> "$INSTALL_DB"',
            "useradd": 'for a; do last=$a; done; echo "passwd $last" >> "$INSTALL_DB"',
            "install": "exit 0",
            "udevadm": "exit 0",
        }
        for name, body in stubs.items():
            _write(os.path.join(self.bin, name), STUB.format(body=body), 0o755)
        self.db = os.path.join(self.tmp, "db")
        open(self.db, "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_install(self, *args):
        env = dict(os.environ, INSTALL_LOG=self.log, INSTALL_DB=self.db)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        p = subprocess.run([BASH, INSTALL_SH] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env=env, timeout=60)
        return p.returncode, p.stdout.decode()

    def calls(self, prog=None):
        lines = [l for l in _read(self.log).splitlines() if l]
        return [l for l in lines if prog is None or l.split()[0] == prog]

    def test_usage(self):
        rc, out = self.run_install("--help")
        self.assertEqual(rc, 0, out)
        for flag in ("--federation-user", "--udev", "--extras-only", "LIBRTLSDR_REF", "KFR_REF"):
            self.assertIn(flag, out)
        rc, out = self.run_install("--bogus")
        self.assertEqual(rc, 2, out)
        rc, out = self.run_install("--extras-only")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.calls(), [])

    def run_clone_step(self, existing, ref, explicit=True, git_tree=True):
        """Run install.sh's KFR clone/checkout step with a logging git stub."""
        text = _read(INSTALL_SH)
        funcs = re.search(r"^is_sha\(\) \{.*?^\}\n.*?^checkout_ref\(\) \{.*?^\}\n",
                          text, re.S | re.M).group(0)
        step = re.search(r"^    if \[ ! -d kfr \]; then\n.*?^    fi\n", text,
                         re.S | re.M).group(0)
        work = tempfile.mkdtemp(dir=self.tmp)
        if existing:
            os.mkdir(os.path.join(work, "kfr"))
            if git_tree:
                os.mkdir(os.path.join(work, "kfr", ".git"))
        bindir = tempfile.mkdtemp(dir=self.tmp)
        git_log = os.path.join(work, "git.log")
        _write(os.path.join(bindir, "git"),
               '#!/bin/sh\nprintf "%s\\n" "$*" >> "{}"\n'.format(git_log), 0o755)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"], KFR_REF=ref,
                   KFR_REF_ENV=ref if explicit else "")
        p = subprocess.run(["bash", "-c", "set -e\n" + funcs + step], cwd=work, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout.decode())
        return _read(git_log).splitlines() if os.path.exists(git_log) else []

    def test_kfr_ref_checked_out_in_existing_clone(self):
        self.assertEqual(self.run_clone_step(True, "v6.0.2"),
                         ["-C kfr -c advice.detachedHead=false checkout -q v6.0.2"])
        # KFR_REF unset: an existing tree is used as is (pre-program behaviour)
        self.assertEqual(self.run_clone_step(True, "7.1.0", explicit=False), [])
        # an existing kfr that is not a git clone is built as is, never aborts
        self.assertEqual(self.run_clone_step(True, "v6.0.2", git_tree=False), [])
        self.assertEqual(self.run_clone_step(False, "7.1.0"),
                         ["clone --depth 1 --branch 7.1.0 https://github.com/kfrlib/kfr"])
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(self.run_clone_step(False, sha),
                         ["clone https://github.com/kfrlib/kfr",
                          "-C kfr -c advice.detachedHead=false checkout -q " + sha])

    def test_federation_user_idempotent(self):
        rc, out = self.run_install("--extras-only", "--federation-user")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls("groupadd"), ["groupadd --system heimdall"])
        self.assertEqual(self.calls("useradd"),
                         ["useradd --system --no-create-home --shell /usr/sbin/nologin "
                          "--gid heimdall heimdall"])
        self.assertIn("created system user heimdall", out)
        open(self.log, "w").close()
        rc, out = self.run_install("--extras-only", "--federation-user")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls("groupadd") + self.calls("useradd"), [])
        self.assertIn("user heimdall exists", out)
        self.assertEqual(self.calls("udevadm") + self.calls("install"), [])

    def test_federation_user_with_existing_group(self):
        _write(self.db, "group heimdall\n")
        rc, out = self.run_install("--extras-only", "--federation-user")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls("groupadd"), [])
        self.assertEqual(len(self.calls("useradd")), 1)

    def test_udev(self):
        rc, out = self.run_install("--extras-only", "--udev")
        self.assertEqual(rc, 0, out)
        rule = os.path.join(PACKAGING, "udev", "99-heimdall-rtlsdr.rules")
        self.assertEqual(self.calls("install"),
                         ["install -m 0644 {} /etc/udev/rules.d/99-heimdall-rtlsdr.rules".format(
                             rule)])
        self.assertEqual(self.calls("udevadm"), ["udevadm control --reload-rules",
                                                 "udevadm trigger --subsystem-match=usb"])
        self.assertTrue(all(l.split()[0] != "useradd" for l in self.calls()))

    def test_inert_limits_dropped(self):
        text = _read(INSTALL_SH)
        self.assertNotIn("@audio", text)
        self.assertNotIn("tee -a /etc/security/limits.conf", text)
        self.assertNotIn("usermod", text)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not available")
    def test_shellcheck(self):
        p = subprocess.run(["shellcheck", "-S", "warning", INSTALL_SH], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout.decode())


if __name__ == "__main__":
    unittest.main()
