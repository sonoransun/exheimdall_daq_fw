"""Default-path start / re-run / stop identity (spec B.9, K.2 WP-12 gate).

Sudo lane (``./unit_test.sh --sudo``, CI ``pipeline`` job): Linux, root or
passwordless sudo, built ``_daq_core/{rebuffer,decimate,iq_server}.out`` and
the python stage dependencies (numpy, scipy, pyzmq).

A real synthetic chain runs from a private copy of the Firmware directory
(scripts and INI copied; ``_daq_core``/``_testing``/``_calibration``/
``_fpga_gateware`` symlinked; private ``_data_control``/``_logs``/``_db``)
with the repository's INI as is, i.e. **no new key**:

1. start -> the stages of today's synthetic launch run, with today's ports
   and today's unprefixed ``/dev/shm`` names; the registry directory stays
   empty; no stage holds fd 8/9 or a HEIMDALL_* variable;
2. re-run while running -> exit 0, the busy ports are only reported
   (advisory probe, L9) and the running chain is not stopped (today's
   synthetic start had no port check);
3. the no-argument ``daq_stop.sh`` -> every DAQ process on the host is gone
   (it is the documented host-wide stop), the ports are free;
4. a fresh start reproduces exactly the process roles, ports and shm names
   of step 1; the final no-argument stop clears the host again.

The no-argument stop is host-wide by design, so the test refuses to run
(skip) when another DAQ chain is already running on the host. When this
host has no ``sudo`` binary but the test runs as root (build containers), a
``sudo`` shim that runs the command directly is put first on PATH; when
SCHED_FIFO is not permitted (containers without CAP_SYS_NICE), a ``chrt``
shim that drops the policy is added. Both are reported on stderr.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.abspath(os.path.join(HERE, "..", ".."))

PATTERNS = ["rtl_daq.out", "decimate.out", "rebuffer.out", "iq_server.out",
            "_testing/test_data_synthesizer.py", "_daq_core/delay_sync.py", "_daq_core/hw_controller.py"]
LEGACY_SHM = {"%s_%s" % (seg, ab) for seg in ("decimator_in", "decimator_out", "delay_sync_iq", "delay_sync_hwc")
              for ab in ("A", "B")}
LEGACY_PORTS = (5000, 5001, 5002, 5003, 1130)


def _have_root():
    if not sys.platform.startswith("linux"):
        return False, "Linux only"
    if os.geteuid() == 0:
        return True, ""
    r = subprocess.run(["sudo", "-n", "true"], capture_output=True)
    return (r.returncode == 0), "needs root or passwordless sudo"


def _binaries():
    missing = [b for b in ("rebuffer.out", "decimate.out", "iq_server.out")
               if not os.access(os.path.join(FIRMWARE, "_daq_core", b), os.X_OK)]
    return missing


def _daq_pids():
    pids = set()
    for pat in PATTERNS:
        r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
        pids.update(int(p) for p in r.stdout.split() if int(p) != os.getpid())
    return pids


def _listening(ports):
    r = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True)
    found = set()
    for line in r.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            try:
                port = int(fields[3].rsplit(":", 1)[1])
            except (IndexError, ValueError):
                continue
            if port in ports:
                found.add(port)
    return found


def _wait_for(cond, timeout, step=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


OK_ROOT, WHY_ROOT = _have_root()


@unittest.skipUnless(OK_ROOT, WHY_ROOT)
@unittest.skipIf(shutil.which("ss") is None or shutil.which("pgrep") is None, "needs ss and pgrep")
@unittest.skipIf(_binaries(), "C binaries not built: %s" % ", ".join(_binaries()))
class TestStartStopLegacy(unittest.TestCase):

    def setUp(self):
        if _daq_pids():
            self.skipTest("another DAQ chain is running on this host (the no-argument stop is host-wide)")
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="hd_legacy_"))
        os.chmod(self.tmp, 0o755)
        self.fw = os.path.join(self.tmp, "Firmware")
        os.makedirs(os.path.join(self.fw, "_data_control"))
        for name in os.listdir(FIRMWARE):
            src = os.path.join(FIRMWARE, name)
            if name.endswith((".sh", ".py")) and os.path.isfile(src):
                shutil.copy2(src, os.path.join(self.fw, name))
        shutil.copy2(os.path.join(FIRMWARE, "daq_chain_config.ini"), os.path.join(self.fw, "daq_chain_config.ini"))
        for name in ("_daq_core", "_testing", "_calibration", "_fpga_gateware"):
            if os.path.exists(os.path.join(FIRMWARE, name)):
                os.symlink(os.path.join(FIRMWARE, name), os.path.join(self.fw, name))
        self.run_dir = os.path.join(self.tmp, "run")
        os.makedirs(self.run_dir)
        self.env = dict(os.environ)
        for k in list(self.env):
            if k.startswith("HEIMDALL_"):
                del self.env[k]
        self.env["HEIMDALL_RUN_DIR"] = self.run_dir
        self._shims()
        from configparser import ConfigParser
        cfg = ConfigParser()
        cfg.read(os.path.join(self.fw, "daq_chain_config.ini"))
        self.assertEqual(cfg.get("federation", "instance_id", fallback="0"), "0",
                         "the legacy identity lane needs an instance-0 INI")
        self.assertFalse(cfg.has_section("host") and any(cfg.items("host")), "INI must not carry [host] keys")
        self.eth = cfg.get("data_interface", "out_data_iface_type", fallback="eth") == "eth"
        mon = cfg.get("monitoring", "en_monitoring", fallback="0") == "1"
        self.status = mon and cfg.get("monitoring", "en_status_server", fallback="0") == "1"
        self.pub = mon and cfg.get("monitoring", "en_zmq_pub", fallback="0") == "1"

    def _shims(self):
        shim = os.path.join(self.tmp, "shim")
        notes = []
        if shutil.which("sudo") is None and os.geteuid() == 0:
            os.makedirs(shim, exist_ok=True)
            with open(os.path.join(shim, "sudo"), "w") as f:
                f.write('#!/bin/sh\nexec "$@"\n')
            os.chmod(os.path.join(shim, "sudo"), 0o755)
            notes.append("sudo shim (root, no sudo binary)")
        probe = ["chrt", "-f", "1", "true"] if os.geteuid() == 0 else ["sudo", "-n", "chrt", "-f", "1", "true"]
        if subprocess.run(probe, capture_output=True).returncode != 0:
            os.makedirs(shim, exist_ok=True)
            with open(os.path.join(shim, "chrt"), "w") as f:
                f.write('#!/bin/sh\n# SCHED_FIFO not permitted here: drop "-f PRIO"\nshift 2\nexec "$@"\n')
            os.chmod(os.path.join(shim, "chrt"), 0o755)
            notes.append("chrt shim (SCHED_FIFO not permitted)")
        if notes:
            self.env["PATH"] = shim + os.pathsep + self.env.get("PATH", "")
            sys.stderr.write("[test_start_stop_legacy] using %s\n" % ", ".join(notes))

    def tearDown(self):
        if hasattr(self, "fw"):
            subprocess.run(["./daq_stop.sh"], cwd=self.fw, env=self.env, capture_output=True, timeout=120)
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def sh(self, *argv, timeout=300):
        log = os.path.join(self.tmp, "out.%d.log" % time.monotonic_ns())
        with open(log, "w") as out:
            rc = subprocess.run(list(argv), cwd=self.fw, env=self.env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, timeout=timeout).returncode
        with open(log) as f:
            return rc, f.read()

    def expected_roles(self):
        roles = {"synthesizer", "rebuffer", "decimate", "delay_sync", "hw_controller"}
        if self.eth:
            roles.add("iq_server")
        return roles

    def expected_ports(self):
        # 1130: the synthesizer's ZMQ control responder (L9), like rtl_daq's
        ports = {5001, 1130}
        if self.eth:
            ports.add(5000)
        if self.status:
            ports.add(5002)
        if self.pub:
            ports.add(5003)
        return ports

    def logs(self):
        out = []
        for name in sorted(os.listdir(os.path.join(self.fw, "_logs"))):
            path = os.path.join(self.fw, "_logs", name)
            if name.endswith(".log"):
                with open(path, errors="replace") as f:
                    out.append("==> %s <==\n%s" % (name, f.read()[-1500:]))
        return "\n".join(out)

    def pid_roles(self):
        piddir = os.path.join(self.fw, "_logs", "inst0", "pids")
        roles = {}
        for name in os.listdir(piddir):
            if name.endswith(".pid"):
                with open(os.path.join(piddir, name)) as f:
                    roles[name[:-4]] = int(f.read().split()[0])
        return roles

    def snapshot(self):
        """(roles alive, listening legacy ports, /dev/shm names) once the chain is up."""
        want_ports = self.expected_ports()
        up = _wait_for(lambda: _listening(LEGACY_PORTS) >= want_ports, timeout=180)
        self.assertTrue(up, "chain did not come up: listening %s, want %s\n%s"
                        % (sorted(_listening(LEGACY_PORTS)), sorted(want_ports), self.logs()))
        time.sleep(2)
        roles = self.pid_roles()
        alive = set()
        for role, pid in roles.items():
            r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True)
            if role in r.stdout:
                alive.add(role)
        shm = {n for n in os.listdir("/dev/shm")
               if any(n.startswith(seg) or n.startswith("inst") and seg in n
                      for seg in ("decimator_in", "decimator_out", "delay_sync_iq", "delay_sync_hwc"))}
        return alive, _listening(LEGACY_PORTS), shm, roles

    def assert_stage_fds_and_env(self, roles):
        for role in ("decimate", "rebuffer", "delay_sync"):
            pid = roles[role]
            # The stages open their own FIFOs/shm on low fds; what must not be
            # there is an inherited registry lock/mutex file
            targets = []
            for fd in os.listdir("/proc/%d/fd" % pid):
                try:
                    targets.append(os.readlink("/proc/%d/fd/%s" % (pid, fd)))
                except OSError:
                    pass
            bad = [t for t in targets if t.startswith((self.run_dir, "/run/heimdall", "/tmp/heimdall-run-"))
                   or t.endswith((".lock", ".start"))]
            self.assertEqual(bad, [], "%s inherited registry descriptors on the default path" % role)
            with open("/proc/%d/environ" % pid, "rb") as f:
                env = [e.split(b"=", 1)[0].decode() for e in f.read().split(b"\0") if e]
            self.assertEqual([e for e in env if e.startswith("HEIMDALL_") and e != "HEIMDALL_RUN_DIR"], [],
                             "%s carries new HEIMDALL_* variables" % role)

    # -- the lane --------------------------------------------------------

    def test_start_rerun_stop_identity(self):
        rc, out = self.sh("./daq_synthetic_start.sh")
        self.assertEqual(rc, 0, out)
        alive1, ports1, shm1, roles1 = self.snapshot()
        self.assertEqual(alive1, self.expected_roles(), self.logs())
        self.assertEqual(ports1, self.expected_ports())
        self.assertTrue(shm1, "no shared-memory segment created")
        self.assertTrue(shm1 <= LEGACY_SHM, "unexpected shm names: %s" % sorted(shm1 - LEGACY_SHM))
        self.assertEqual(os.listdir(self.run_dir), [], "registry written on the default path")
        self.assertFalse(os.path.exists(os.path.join(self.fw, "_logs", "inst0", "pids", "run_dir")))
        self.assert_stage_fds_and_env(roles1)
        with open(os.path.join(self.fw, "_data_control", "iq_track_lock")) as f:
            self.assertEqual(f.read(), "0")

        # Re-run while running: advisory port probe only, nothing stopped
        rc, out = self.sh("./daq_synthetic_start.sh")
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: port 5001 is in use by", out)
        self.assertNotIn("not free", out)
        for role in ("decimate", "delay_sync", "hw_controller"):
            r = subprocess.run(["ps", "-o", "args=", "-p", str(roles1[role])], capture_output=True, text=True)
            self.assertIn(role, r.stdout, "re-run stopped %s of the running chain" % role)

        # The no-argument stop clears every DAQ process on the host
        rc, out = self.sh("./daq_stop.sh")
        self.assertIn("Shut down DAQ chain ..", out)
        self.assertTrue(_wait_for(lambda: not _daq_pids(), timeout=30),
                        "DAQ processes survived the global stop: %s" % sorted(_daq_pids()))
        self.assertTrue(_wait_for(lambda: not (_listening(LEGACY_PORTS) & self.expected_ports()), timeout=30))
        self.assertEqual(os.listdir(self.run_dir), [])

        # A fresh start reproduces the first one
        rc, out = self.sh("./daq_synthetic_start.sh")
        self.assertEqual(rc, 0, out)
        alive3, ports3, shm3, _ = self.snapshot()
        self.assertEqual((alive3, ports3, shm3), (alive1, ports1, shm1))
        rc, out = self.sh("./daq_stop.sh")
        self.assertTrue(_wait_for(lambda: not _daq_pids(), timeout=30))
        self.assertEqual(os.listdir(self.run_dir), [])


if __name__ == "__main__":
    unittest.main()
