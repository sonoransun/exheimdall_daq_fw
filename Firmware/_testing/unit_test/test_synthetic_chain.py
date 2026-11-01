"""One synthetic DAQ chain end to end (spec J.1 WP-17b, B.4, G.5, R25).

Sudo lane (``./unit_test.sh --sudo``, CI ``pipeline`` job): Linux, root or
passwordless sudo, built ``_daq_core/{rebuffer,decimate,iq_server}.out``,
numpy, scipy and pyzmq (the synthesizer's ZMQ control responder lets
hw_controller and delay_sync calibrate the simulated chain).

A real chain is started with ``daq_synthetic_start.sh --pace --blocks 0``
from a private install root (the J.1 fixture: ``<tmp>/Firmware`` holds
copies of the scripts and the INI; the code directories are symlinked; the
registry directory is ``<tmp>/run``). The INI is the repository's with the
monitoring keys, ``status_extended = 1`` and ``en_wire_recal = 1`` added.

1. the chain reaches ``sync_state >= 5``; a real ``streaming``/``IQDownload``
   client on ``:5000`` sees a continuous ``cpi_index``; slot 102
   (``buffer_overrun_cnt``) is 0 in every header and in STATUS;
2. ``heimdall-ctl recal`` (RECL, en_wire_recal = 1) drops the lock and the
   chain re-locks; the recalibration generation advances;
3. the chain is stopped (``daq_stop.sh 0``) with hw_controller's
   ``recal_request`` relay file left behind; a fresh start re-locks and does
   not recalibrate (R25: the watcher never replays a leftover request).

The paced synthesizer is used so that the decimator (launched in drop mode
by the start script) is never overrun: an unpaced source makes the
decimator drop frames, which shows up as ``cpi_index`` gaps by design.

This module also provides the chain fixture (``Sandbox``, ``IQClient`` and
the probe helpers) that test_multi_instance, test_bit_identity and
test_throughput_lane import.

Environment: ``HEIMDALL_TEST_SYNC_TIMEOUT`` (seconds) overrides the wait for
the first lock on slow hosts.
"""
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
from configparser import ConfigParser

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.path.dirname(FIRMWARE)
sys.path.insert(0, os.path.join(FIRMWARE, "_daq_core"))
from iq_header import IQHeader  # noqa: E402

#: process-name patterns of every DAQ stage (daq_stop.sh's global sweep list)
DAQ_PATTERNS = ("rtl_daq.out", "decimate.out", "rebuffer.out", "iq_server.out",
                "_testing/test_data_synthesizer.py", "_daq_core/delay_sync.py",
                "_daq_core/hw_controller.py")
#: code directories an instance copy links (B.1)
CODE_DIRS = ("_daq_core", "_testing", "_calibration", "_fpga_gateware")
#: shared-memory streams of one instance
SHM_STREAMS = ("decimator_in", "decimator_out", "delay_sync_iq", "delay_sync_hwc")
SYNC_LOCKED = 5
#: frames a new :5000 client discards: buffers filled before it connected
STALE_FRAMES = 5
HEADER_SIZE = 1024
#: byte offset of time_stamp (uint64, field 10) in the native header layout
TIME_STAMP_OFFSET = struct.calcsize("II16sIIIQQQIQ") - struct.calcsize("Q")


def sync_timeout(default):
    """The first-lock timeout: HEIMDALL_TEST_SYNC_TIMEOUT or *default* seconds."""
    try:
        return max(1.0, float(os.environ.get("HEIMDALL_TEST_SYNC_TIMEOUT", default)))
    except ValueError:
        return float(default)


# ---------------------------------------------------------------------------
# Lane requirements
# ---------------------------------------------------------------------------

def lane_unavailable():
    """None when the pipeline lane can run here, else the skip reason."""
    if not sys.platform.startswith("linux"):
        return "pipeline lane: Linux only"
    if os.geteuid() != 0 and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        return "pipeline lane: needs root or passwordless sudo"
    for tool in ("ss", "pgrep"):
        if shutil.which(tool) is None:
            return "pipeline lane: needs %s" % tool
    missing = [b for b in ("rebuffer.out", "decimate.out", "iq_server.out")
               if not os.access(os.path.join(FIRMWARE, "_daq_core", b), os.X_OK)]
    if missing:
        return "C binaries not built: %s (cd _daq_core && make)" % ", ".join(missing)
    for mod in ("numpy", "scipy", "zmq"):
        try:
            __import__(mod)
        except ImportError:
            return "pipeline lane: python module %s not installed" % mod
    return None


def daq_pids():
    """Pids of every DAQ stage on this host (any copy)."""
    pids = set()
    for pat in DAQ_PATTERNS:
        r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
        pids.update(int(p) for p in r.stdout.split() if int(p) != os.getpid())
    return pids


def listening(ports):
    """The subset of *ports* with a TCP listener on this host."""
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


def wait_for(cond, timeout, step=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def role_alive(pid, role):
    """True when *pid* runs and its command line names *role* (not a zombie)."""
    r = subprocess.run(["ps", "-o", "stat=", "-o", "args=", "-p", str(pid)], capture_output=True, text=True)
    info = r.stdout.strip()
    return bool(info) and not info.startswith("Z") and role in info


def query(port, command, timeout=3.0, host="127.0.0.1"):
    """One ``:5002``-style line request; the decoded JSON reply, or None."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((command + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode())
    except (OSError, ValueError):
        return None


def status(port):
    reply = query(port, "STATUS")
    return reply if isinstance(reply, dict) and "error" not in reply else None


def sync_state(port):
    st = status(port)
    return None if st is None else st.get("sync_state")


class IQClient(object):
    """A real ``:5000`` client: ``streaming`` once, then ``IQDownload`` per frame."""

    def __init__(self, port, host="127.0.0.1", timeout=20.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.sock.sendall(b"streaming")
        self._first = True
        self._buf = bytearray(1 << 20)

    def _recv_exact(self, n, keep):
        """Receive *n* bytes; return them when *keep*, else discard (b'')."""
        if keep:
            out = bytearray(n)
            view = memoryview(out)
        got = 0
        while got < n:
            if keep:
                k = self.sock.recv_into(view[got:], n - got)
            else:
                k = self.sock.recv_into(self._buf, min(len(self._buf), n - got))
            if k == 0:
                raise ConnectionError("IQ server closed the connection")
            got += k
        return bytes(out) if keep else b""

    def frame(self, keep_payload=False):
        """(header bytes, IQHeader, payload length, payload or b'') of the next frame."""
        if not self._first:
            self.sock.sendall(b"IQDownload")
        self._first = False
        raw = self._recv_exact(HEADER_SIZE, True)
        hdr = IQHeader()
        hdr.decode_header(raw)
        if hdr.check_sync_word():
            raise ValueError("IQ header sync word mismatch")
        n = hdr.cpi_length * hdr.active_ant_chs * 2 * hdr.sample_bit_depth // 8
        payload = self._recv_exact(n, keep_payload)
        return raw, hdr, n, payload

    def close(self):
        try:
            self.sock.sendall(b"q")
        except OSError:
            pass
        self.sock.close()


def connect_iq(port, timeout=60.0):
    """IQClient on *port*, retried until the server answers or *timeout*."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return IQClient(port)
        except OSError as e:
            last = e
            time.sleep(0.5)
    raise AssertionError("no IQ server on :%d within %.0f s (%s)" % (port, timeout, last))


class StateSampler(threading.Thread):
    """Samples STATUS every *period* s: ``(monotonic, sync_state,
    recal_generation, frame_type)``. A DUMMY frame (frame type 1) reports
    ``sync_state`` 0 because delay_sync's FSM skips it, so lock checks use
    ``lost()``, which ignores those samples."""

    def __init__(self, port, period=0.2):
        super().__init__(daemon=True)
        self.port = port
        self.period = period
        self.samples = []  # (monotonic, sync_state, recal_generation, frame_type)
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            st = status(self.port)
            if st is not None:
                gen = (st.get("cal") or {}).get("recal_generation")
                self.samples.append((time.monotonic(), st.get("sync_state"), gen, st.get("frame_type")))
            self._stop_evt.wait(self.period)

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=10)

    def lost(self, since=0):
        """Samples from index *since* on that report a non-DUMMY frame below lock."""
        return [s for s in self.samples[since:]
                if s[1] is not None and s[1] < SYNC_LOCKED and s[3] != IQHeader.FRAME_TYPE_DUMMY]

    def relocked(self):
        """True once a sample after the first lost one reports the lock again."""
        samples = list(self.samples)
        lost = [i for i, s in enumerate(samples)
                if s[1] is not None and s[1] < SYNC_LOCKED and s[3] != IQHeader.FRAME_TYPE_DUMMY]
        return bool(lost) and any(s[1] is not None and s[1] >= SYNC_LOCKED for s in samples[lost[0]:])


# ---------------------------------------------------------------------------
# The J.1 fixture: a private install root
# ---------------------------------------------------------------------------

def set_ini(path, values):
    """Set ``{section: {key: value}}`` in the INI at *path* (sections created)."""
    cfg = ConfigParser(interpolation=None)
    cfg.optionxform = str
    cfg.read(path)
    for section, items in values.items():
        if not cfg.has_section(section):
            cfg.add_section(section)
        for key, value in items.items():
            cfg.set(section, key, str(value))
    with open(path, "w") as f:
        cfg.write(f)


def read_ini(path):
    cfg = ConfigParser(interpolation=None)
    cfg.read(path)
    return cfg


class Sandbox(object):
    """``<tmp>`` with ``<tmp>/Firmware`` a real directory: copies of
    ``Firmware/*.sh``, ``Firmware/*.py`` and ``daq_chain_config.ini``,
    symlinks for the code directories, ``<tmp>/util`` and
    ``<tmp>/config_files`` symlinked, ``HEIMDALL_RUN_DIR=<tmp>/run``.

    Runs as root without a ``sudo`` binary get a ``sudo`` shim that runs the
    command directly; hosts that do not permit SCHED_FIFO get a ``chrt`` shim
    that drops the policy (both reported on stderr). ``HOME`` is private so
    heimdall-ctl's ack-mode cache stays inside the sandbox.
    """

    def __init__(self, tag):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="hd_%s_" % tag))
        os.chmod(self.root, 0o755)
        self.tag = tag
        self.fw = os.path.join(self.root, "Firmware")
        os.makedirs(os.path.join(self.fw, "_data_control"))
        for name in os.listdir(FIRMWARE):
            src = os.path.join(FIRMWARE, name)
            if name.endswith((".sh", ".py")) and os.path.isfile(src):
                shutil.copy2(src, os.path.join(self.fw, name))
        shutil.copy2(os.path.join(FIRMWARE, "daq_chain_config.ini"), os.path.join(self.fw, "daq_chain_config.ini"))
        for name in CODE_DIRS:
            if os.path.exists(os.path.join(FIRMWARE, name)):
                os.symlink(os.path.join(FIRMWARE, name), os.path.join(self.fw, name))
        for name in ("util", "config_files"):
            os.symlink(os.path.join(REPO, name), os.path.join(self.root, name))
        self.run_dir = os.path.join(self.root, "run")
        os.makedirs(self.run_dir)
        home = os.path.join(self.root, "home")
        os.makedirs(home)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("HEIMDALL_")}
        self.env.update({"HEIMDALL_RUN_DIR": self.run_dir, "HOME": home,
                         "XDG_CACHE_HOME": os.path.join(home, ".cache")})
        self._shims()
        self._seq = 0

    def _shims(self):
        shim = os.path.join(self.root, "shim")
        notes = []
        if shutil.which("sudo") is None and os.geteuid() == 0:
            os.makedirs(shim, exist_ok=True)
            with open(os.path.join(shim, "sudo"), "w") as f:
                f.write('#!/bin/sh\nexec "$@"\n')
            os.chmod(os.path.join(shim, "sudo"), 0o755)
            notes.append("sudo shim (root, no sudo binary)")
        probe = ["chrt", "-f", "1", "true"] if os.geteuid() == 0 else ["sudo", "-n", "chrt", "-f", "1", "true"]
        if shutil.which("chrt") is None or subprocess.run(probe, capture_output=True).returncode != 0:
            os.makedirs(shim, exist_ok=True)
            with open(os.path.join(shim, "chrt"), "w") as f:
                f.write('#!/bin/sh\n# SCHED_FIFO not permitted here: drop "-f PRIO"\nshift 2\nexec "$@"\n')
            os.chmod(os.path.join(shim, "chrt"), 0o755)
            notes.append("chrt shim (SCHED_FIFO not permitted)")
        if notes:
            self.env["PATH"] = shim + os.pathsep + self.env.get("PATH", "")
            sys.stderr.write("[%s] using %s\n" % (self.tag, ", ".join(notes)))

    @property
    def ini(self):
        return os.path.join(self.fw, "daq_chain_config.ini")

    def _log_path(self):
        self._seq += 1
        return os.path.join(self.root, "out.%03d.log" % self._seq)

    def sh(self, cwd, *argv, timeout=300):
        """Run *argv* in *cwd*; (exit code, combined output). The output goes
        through a file: the stages a start script launches inherit its stdout."""
        log = self._log_path()
        with open(log, "w") as out:
            rc = subprocess.run(list(argv), cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, timeout=timeout).returncode
        with open(log, errors="replace") as f:
            return rc, f.read()

    def spawn(self, cwd, *argv):
        """Start *argv* in *cwd* without waiting; (Popen, output log path)."""
        log = self._log_path()
        with open(log, "w") as out:
            proc = subprocess.Popen(list(argv), cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT)
        return proc, log

    @staticmethod
    def log_dir(cwd, instance_id=0):
        return os.path.join(cwd, "_logs") if instance_id == 0 else os.path.join(cwd, "_logs", "inst%d" % instance_id)

    def logs(self, cwd=None, instance_id=0, tail=2000):
        """The tail of every stage log of an instance copy (assertion context)."""
        d = self.log_dir(cwd or self.fw, instance_id)
        out = []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith(".log"):
                    with open(os.path.join(d, name), errors="replace") as f:
                        out.append("==> %s <==\n%s" % (os.path.join(d, name), f.read()[-tail:]))
        return "\n".join(out)

    @staticmethod
    def pid_roles(cwd, instance_id=0):
        """{role: pid} from the PID files of an instance copy."""
        piddir = os.path.join(cwd, "_logs", "inst%d" % instance_id, "pids")
        roles = {}
        if os.path.isdir(piddir):
            for name in os.listdir(piddir):
                if name.endswith(".pid"):
                    try:
                        with open(os.path.join(piddir, name)) as f:
                            roles[name[:-4]] = int(f.read().split()[0])
                    except (OSError, ValueError, IndexError):
                        pass
        return roles

    def stop_all(self):
        """The host-wide ``daq_stop.sh --all`` (the lane refuses to run beside another chain)."""
        if os.path.isdir(self.fw):
            subprocess.run(["./daq_stop.sh", "--all"], cwd=self.fw, env=self.env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)

    def cleanup(self):
        self.stop_all()
        wait_for(lambda: not daq_pids(), timeout=30)
        shutil.rmtree(self.root, ignore_errors=True)


def ports_of(ini_path):
    """(iq, ctl, status) ports of the instance whose INI is *ini_path*."""
    cfg = read_ini(ini_path)
    off = cfg.getint("federation", "instance_id", fallback=0) * cfg.getint("federation", "port_stride",
                                                                            fallback=100)
    return 5000 + off, 5001 + off, cfg.getint("monitoring", "status_server_port", fallback=5002) + off


def heimdall_ctl(sandbox, *args, timeout=120):
    """Run ``python3 -m heimdall_ctl ARGS`` (the util/heimdall_ctl package)."""
    env = dict(sandbox.env)
    env["PYTHONPATH"] = os.path.join(sandbox.root, "util") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log = sandbox._log_path()
    with open(log, "w") as out:
        rc = subprocess.run([sys.executable, "-m", "heimdall_ctl"] + list(args), cwd=sandbox.root, env=env,
                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                            timeout=timeout).returncode
    with open(log, errors="replace") as f:
        return rc, f.read()


# ---------------------------------------------------------------------------
# The lane
# ---------------------------------------------------------------------------

WHY_NOT = lane_unavailable()
#: monitoring keys every chain test of this lane adds to the INI
MONITORING = {"en_monitoring": "1", "en_status_server": "1", "status_server_port": "5002",
              "status_extended": "1", "status_update_interval": "1", "heartbeat_interval": "100"}


@unittest.skipIf(WHY_NOT, WHY_NOT)
class TestSyntheticChain(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if daq_pids():
            raise unittest.SkipTest("another DAQ chain is running on this host (the lane stops every chain)")
        cls.box = Sandbox("synthchain")
        # cal_track_mode = 0: one calibration, then a locked data stream. In
        # mode 2 with long bursts (unit_test_k4) the noise source is nearly
        # always on, RECL is deferred during a burst (hw_controller) and the
        # calibration frames make the decimator drop frames (cpi_index gaps).
        set_ini(cls.box.ini, {"monitoring": MONITORING,
                              "calibration": {"en_wire_recal": "1", "cal_track_mode": "0"},
                              "data_interface": {"out_data_iface_type": "eth"},
                              "federation": {"instance_id": "0", "port_stride": "100"}})
        cls.iq_port, cls.ctl_port, cls.st_port = ports_of(cls.box.ini)
        cls.started = False

    @classmethod
    def tearDownClass(cls):
        cls.box.cleanup()

    def start_chain(self):
        rc, out = self.box.sh(self.box.fw, "./daq_synthetic_start.sh", "--pace", "--blocks", "0")
        self.assertEqual(rc, 0, out + self.box.logs())
        type(self).started = True

    def wait_locked(self, timeout):
        locked = wait_for(lambda: (sync_state(self.st_port) or 0) >= SYNC_LOCKED, timeout=timeout)
        self.assertTrue(locked, "no lock within %.0f s: STATUS %s\n%s"
                        % (timeout, status(self.st_port), self.box.logs()))

    def ensure_locked(self):
        if not self.started:
            self.start_chain()
        self.wait_locked(sync_timeout(150))

    def test_1_lock_and_continuous_stream(self):
        self.ensure_locked()
        client = connect_iq(self.iq_port)
        try:
            # The first frames of a new client are the buffers delay_sync
            # filled before anyone read them (older cpi_index values)
            for _ in range(STALE_FRAMES):
                client.frame()
            gaps_before = status(self.st_port)["counters"]["cpi_gaps"]
            frames = [client.frame()[1] for _ in range(40)]
            gaps_after = status(self.st_port)["counters"]["cpi_gaps"]
        finally:
            client.close()
        cpis = [h.cpi_index for h in frames]
        gaps = [(a, b) for a, b in zip(cpis, cpis[1:]) if b != a + 1]
        self.assertEqual(gaps, [], "cpi_index not continuous on :%d: %s" % (self.iq_port, cpis))
        self.assertEqual(gaps_after, gaps_before, "delay_sync saw input cpi_index gaps while streaming")
        self.assertEqual([h.get_buffer_overrun_cnt() for h in frames if h.get_buffer_overrun_cnt()], [],
                         "slot 102 (buffer_overrun_cnt) non-zero")
        self.assertTrue(any(h.frame_type == IQHeader.FRAME_TYPE_DATA for h in frames), "no DATA frame")
        st = status(self.st_port)
        self.assertIsNotNone(st)
        self.assertGreaterEqual(st["sync_state"], SYNC_LOCKED)
        self.assertEqual(st["counters"].get("buffer_overrun_cnt"), 0, st)
        self.assertEqual(st.get("instance_id"), 0, st)

    def test_2_wire_recal_then_restart_with_leftover_request(self):
        self.ensure_locked()
        recal_file = os.path.join(self.box.fw, "_data_control", "recal_request")
        gen0 = (status(self.st_port).get("cal") or {}).get("recal_generation")
        self.assertEqual(gen0, 0, "a fresh chain must not have recalibrated")

        sampler = StateSampler(self.st_port)
        sampler.start()
        try:
            rc, out = heimdall_ctl(self.box, "--timeout", "30", "recal")
            self.assertEqual(rc, 0, out)
            self.assertNotIn("falling back to INIT", out)
            relocked = wait_for(lambda: sampler.relocked(), timeout=sync_timeout(150))
        finally:
            sampler.stop()
        states = [(s[1], s[3]) for s in sampler.samples]
        self.assertTrue(relocked, "RECL did not drop and regain the lock: (sync_state, frame_type) samples %s\n%s"
                        % (states, self.box.logs()))
        drop = sampler.samples.index(sampler.lost()[0])
        self.assertFalse(any(s[1] < SYNC_LOCKED and s[3] != IQHeader.FRAME_TYPE_DUMMY
                             for s in sampler.samples[:1]), "not locked before RECL: %s" % states)
        self.assertTrue(any(s[1] >= SYNC_LOCKED for s in sampler.samples[drop:] if s[1] is not None), states)
        gen1 = (status(self.st_port).get("cal") or {}).get("recal_generation")
        self.assertGreaterEqual(gen1, 1, "recal generation did not advance")
        self.assertTrue(os.path.isfile(recal_file), "hw_controller wrote no recal_request relay file")

        # Stop: the relay file stays behind (R25)
        rc, out = self.box.sh(self.box.fw, "./daq_stop.sh", "0")
        self.assertEqual(rc, 0, out)
        self.assertTrue(wait_for(lambda: not daq_pids(), timeout=60), "stages survived daq_stop.sh 0")
        self.assertTrue(os.path.isfile(recal_file), "the recal_request of the previous run is gone after the stop")

        # Restart: the start script removes the leftover (L12); the chain
        # locks again and never replays the old request
        self.start_chain()
        self.assertFalse(os.path.exists(recal_file), "the start left the previous run's recal_request")
        self.wait_locked(sync_timeout(150))
        sampler = StateSampler(self.st_port)
        sampler.start()
        time.sleep(15)
        sampler.stop()
        self.assertEqual(sampler.lost(), [], "the restarted chain lost its lock (leftover recal replayed?)")
        gens = {s[2] for s in sampler.samples}
        self.assertEqual(gens, {0}, "recal generation after the restart: %s" % gens)


if __name__ == "__main__":
    unittest.main()
