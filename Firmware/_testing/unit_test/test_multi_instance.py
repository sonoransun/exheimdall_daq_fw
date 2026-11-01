"""Two synthetic instances on one host (spec J.1 WP-17b, B.1, B.3, B.5).

Sudo lane (``./unit_test.sh --sudo``, CI ``pipeline`` job): Linux, root or
passwordless sudo, built C binaries, numpy, scipy, pyzmq, flock.

Fixture (J.1): a private install root ``<tmp>`` (test_synthetic_chain.Sandbox:
``<tmp>/Firmware`` a real directory with copies of the scripts and the INI,
the code directories symlinked, ``<tmp>/util`` and ``<tmp>/config_files``
symlinked, ``HEIMDALL_RUN_DIR=<tmp>/run``). Instance 0 is provisioned in
place and instance 1 into ``<tmp>/instances/1`` with
``util/provision_instance.py`` (both get ``instance_registry = 1`` and
``instance_guard = refuse``); a second copy of instance 1 is provisioned into
``<tmp>/copies/1``. The repository's ``Firmware/daq_chain_config.ini`` is
hashed before and after: the lane must never write it.

TestRegistryInstances (registry on, refuse guard):
  both start with ``daq_synthetic_start.sh``; distinct ``/dev/shm`` segments;
  STATUS on 5002/5102 reports ``instance_id`` 0/1; both lock within 90 s;
  registry entries carry ``launcher``; ``daq_stop.sh 1`` leaves instance 0
  streaming; a start of the running instance 1 from the other copy exits 3;
  two concurrent starts of instance 1 -> exactly one exits 3;
  ``daq_stop.sh 1 --clean`` removes only ``inst1_*`` and keeps
  ``inst1_fence_term``; ``daq_stop.sh --all`` clears both.

TestLegacyInstances (no ``[host]`` keys, registry off):
  starting instance 1 from another copy while it runs prints no registry
  warning, leaves ``<tmp>/run`` empty and ends with the new chain's stages
  running (today's synthetic start: the port probe is advisory, nothing is
  stopped).

TestSoak (nightly, spec J.2 step 8, G39; runs only with
``HEIMDALL_SOAK_SEC`` > 0, e.g. ``HEIMDALL_SOAK_SEC=600``):
  instances 0 and 1 (registry on, ``cal_track_mode = 0``: one calibration,
  then the locked data path) start and lock; a real ``:5000``/``:5100``
  client per instance reads every frame; after the warm-up (the drop window
  empty on both) STATUS on 5002/5102 is sampled every SOAK_PERIOD s for
  HEIMDALL_SOAK_SEC s. On both instances: ``counters.cpi_gaps`` does not
  change from the post-lock baseline, ``counters.buffer_overrun_cnt == 0``,
  ``recent_drops == 0`` on every sample (and the dropped-frame counters do
  not move), ``sync_state >= 5`` on every non-DUMMY sample, ``frame_count``
  advances at the paced rate, and the client sees a continuous
  ``cpi_index`` with slot 102 zero in every header.

Every case stops every DAQ process on the host (``daq_stop.sh --all``), so
they skip when another chain is already running (fail under
``HEIMDALL_STRICT_MODULES=1``).
"""
import glob
import hashlib
import json
import os
import re
import shutil
import math
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_synthetic_chain as chain  # noqa: E402
from iq_header import IQHeader  # noqa: E402

REPO_INI = os.path.join(chain.FIRMWARE, "daq_chain_config.ini")
STAGES = ("synthesizer", "rebuffer", "decimate", "delay_sync")
SYNC_TIMEOUT = chain.sync_timeout(90)
#: lines the registry / lock / mutex helpers print (daq_common.sh)
REGISTRY_LINE = re.compile(r"registry|instance lock|startup mutex|already running|also running|"
                           r"another start of instance|taking over", re.IGNORECASE)


#: STATUS sampling period of the soak [s]; below the soak's drop window, so
#: every drop is still in ``recent_drops`` when the next sample is taken
SOAK_PERIOD = 2.0
SOAK_DROP_WINDOW = 10.0


def soak_seconds():
    """(seconds, error) from HEIMDALL_SOAK_SEC: (0, None) when unset or 0
    (the soak case skips), (0, message) when malformed (the case fails)."""
    raw = os.environ.get("HEIMDALL_SOAK_SEC", "").strip()
    if not raw:
        return 0.0, None
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value < 0:
        return 0.0, "HEIMDALL_SOAK_SEC = %r is not a non-negative number of seconds" % raw
    return value, None


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def shm_names(prefix=""):
    """/dev/shm objects of one instance's four streams."""
    found = set()
    for seg in chain.SHM_STREAMS:
        for ab in ("A", "B"):
            name = "%s%s_%s" % (prefix, seg, ab)
            if os.path.exists(os.path.join("/dev/shm", name)):
                found.add(name)
    return found


def ini_value(path, section, key):
    cfg = chain.read_ini(path)
    return cfg.get(section, key, fallback=None)


class _TwoInstanceBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if chain.WHY_NOT:
            raise unittest.SkipTest(chain.WHY_NOT)
        if shutil.which("flock") is None:
            raise unittest.SkipTest("pipeline lane: needs flock (util-linux)")
        chain.refuse_running_chain()

    def setUp(self):
        self.repo_ini_sha = sha256(REPO_INI)
        self.box = chain.Sandbox(self.TAG)
        # The repository INI with the monitoring keys every check below reads
        chain.set_ini(self.box.ini, {"monitoring": chain.MONITORING,
                                     "data_interface": {"out_data_iface_type": "eth"},
                                     "federation": {"instance_id": "0", "port_stride": "100"}})

    def tearDown(self):
        try:
            self.box.cleanup()
        finally:
            self.assertEqual(sha256(REPO_INI), self.repo_ini_sha, "the repository's daq_chain_config.ini changed")

    # -- helpers ---------------------------------------------------------

    def provision(self, *args):
        rc, out = self.box.sh(self.box.root, sys.executable,
                              os.path.join(self.box.root, "util", "provision_instance.py"),
                              "--root", self.box.root, *args)
        self.assertEqual(rc, 0, "provision_instance.py %s failed:\n%s" % (" ".join(args), out))
        return out

    @staticmethod
    def cores_for(instance_id):
        """--cores for instance N > 0 on a host with fewer than 4(N+1) CPUs.

        Only keeps provision_instance's default cpu_cores (4N..4N+3) inside
        this host's CPU range, so ini_checker accepts the instance INI. Core
        overlap with instance 0 does not matter here: daq_synthetic_start.sh
        without --rt pins no stage and runs no core guard."""
        n = os.cpu_count() or 4
        if 4 * instance_id + 3 < n:
            return []
        return ["--cores", ",".join(str(c) for c in sorted({(4 * instance_id + k) % n for k in range(4)}))]

    def start(self, cwd):
        return self.box.sh(cwd, "./daq_synthetic_start.sh", "--pace", "--blocks", "0")

    def assert_started(self, cwd, instance_id=0):
        rc, out = self.start(cwd)
        self.assertEqual(rc, 0, "start of %s failed:\n%s\n%s" % (cwd, out, self.box.logs(cwd, instance_id)))
        return out

    def wait_status(self, port, cwd, instance_id):
        up = chain.wait_for(lambda: chain.status(port) is not None, timeout=120)
        self.assertTrue(up, "no STATUS on :%d\n%s" % (port, self.box.logs(cwd, instance_id)))
        return chain.status(port)

    def wait_locked(self, port, cwd, instance_id, timeout=SYNC_TIMEOUT):
        ok = chain.wait_for(lambda: (chain.sync_state(port) or 0) >= chain.SYNC_LOCKED, timeout=timeout)
        self.assertTrue(ok, "instance %d did not lock within %.0f s: %s\n%s"
                        % (instance_id, timeout, chain.status(port), self.box.logs(cwd, instance_id)))

    def assert_streaming(self, port, what):
        """frame_count on STATUS *port* advances within 15 s."""
        first = (chain.status(port) or {}).get("frame_count")
        self.assertIsNotNone(first, "%s: no STATUS on :%d" % (what, port))
        moved = chain.wait_for(lambda: ((chain.status(port) or {}).get("frame_count") or -1) > first, timeout=15)
        self.assertTrue(moved, "%s stopped streaming (frame_count stuck at %s)" % (what, first))

    def alive_roles(self, cwd, instance_id, roles=STAGES):
        pids = self.box.pid_roles(cwd, instance_id)
        return {r for r in roles if r in pids and chain.role_alive(pids[r], r)}


class TestRegistryInstances(_TwoInstanceBase):
    TAG = "multi"

    def test_two_instances_registry_guard_and_scoped_stop(self):
        box = self.box
        self.provision("--instance", "0", "--in-place", "--write")
        self.provision("--instance", "1", "--write", *self.cores_for(1))
        other = os.path.join(box.root, "copies", "1")
        self.provision("--instance", "1", "--dest", other, "--write", *self.cores_for(1))
        fw0 = box.fw
        fw1 = os.path.join(box.root, "instances", "1", "Firmware")
        fw1b = os.path.join(other, "Firmware")
        for fw, n in ((fw0, 0), (fw1, 1), (fw1b, 1)):
            ini = os.path.join(fw, "daq_chain_config.ini")
            self.assertEqual(ini_value(ini, "federation", "instance_id"), str(n), ini)
            self.assertEqual(ini_value(ini, "host", "instance_registry"), "1", ini)
            self.assertEqual(ini_value(ini, "host", "instance_guard"), "refuse", ini)
        self.assertFalse(os.path.islink(os.path.join(fw1, "daq_chain_config.ini")), "instance INI must be private")
        self.assertTrue(os.path.islink(os.path.join(fw1, "_daq_core")), "instance code must be linked")

        # -- both instances start, with distinct resources -----------------
        self.assert_started(fw0, 0)
        self.assert_started(fw1, 1)
        st0 = self.wait_status(5002, fw0, 0)
        st1 = self.wait_status(5102, fw1, 1)
        self.assertEqual(st0.get("instance_id"), 0, st0)
        self.assertEqual(st1.get("instance_id"), 1, st1)
        self.assertTrue(chain.wait_for(lambda: "decimator_in_A" in shm_names() and
                                       "inst1_decimator_in_A" in shm_names("inst1_"), timeout=30),
                        "missing /dev/shm segments: %s %s" % (shm_names(), shm_names("inst1_")))
        self.assertEqual(shm_names() & shm_names("inst1_"), set())
        self.wait_locked(5002, fw0, 0)
        self.wait_locked(5102, fw1, 1)

        # -- registry entries ------------------------------------------------
        for n, fw in ((0, fw0), (1, fw1)):
            path = os.path.join(box.run_dir, "inst%d.json" % n)
            self.assertTrue(os.path.isfile(path), "no registry entry %s: %s" % (path, os.listdir(box.run_dir)))
            self.assertTrue(os.path.isfile(os.path.join(box.run_dir, "inst%d.env" % n)))
            with open(path) as f:
                entry = json.load(f)
            self.assertEqual(entry.get("launcher"), "manual", entry)
            self.assertEqual(entry["instance_id"], n)
            self.assertEqual(entry["mode"], "synthetic")
            self.assertEqual(entry["cwd"], os.path.realpath(fw))
            self.assertEqual(entry["ports"]["iq"], 5000 + 100 * n)
            self.assertEqual(entry["ports"]["status"], 5002 + 100 * n)
            for role in STAGES:
                self.assertIn(role, entry["pids"], entry)
                self.assertTrue(chain.role_alive(entry["pids"][role], role), "%s of instance %d" % (role, n))

        # -- the scoped stop leaves instance 0 streaming -------------------------
        pids1 = box.pid_roles(fw1, 1)
        rc, out = box.sh(fw1, "./daq_stop.sh", "1")
        self.assertEqual(rc, 0, out)
        self.assertTrue(chain.wait_for(lambda: not any(chain.role_alive(p, r) for r, p in pids1.items()),
                                       timeout=30), "instance 1 survived daq_stop.sh 1")
        self.assertFalse(os.path.exists(os.path.join(box.run_dir, "inst1.json")))
        self.assertTrue(os.path.exists(os.path.join(box.run_dir, "inst0.json")))
        self.assertEqual(self.alive_roles(fw0, 0), set(STAGES), "daq_stop.sh 1 touched instance 0")
        self.assert_streaming(5002, "instance 0")

        # -- the running instance 1 cannot be started from another copy ------
        self.assert_started(fw1, 1)
        self.wait_status(5102, fw1, 1)
        rc, out = self.start(fw1b)
        self.assertEqual(rc, 3, "second start of instance 1 from %s:\n%s" % (fw1b, out))
        self.assertIn("instance 1 is already running from %s" % os.path.realpath(fw1), out)
        self.assertEqual(self.alive_roles(fw1, 1), set(STAGES), "the refused start stopped instance 1")
        self.assertEqual(self.alive_roles(fw1b, 1), set(), "the refused start launched stages")

        # -- two concurrent starts: exactly one exits 3 ------------------------
        rc, out = box.sh(fw1, "./daq_stop.sh", "1")
        self.assertEqual(rc, 0, out)
        self.assertTrue(chain.wait_for(lambda: not self.alive_roles(fw1, 1), timeout=30))
        procs = [box.spawn(fw, "./daq_synthetic_start.sh", "--pace", "--blocks", "0") for fw in (fw1, fw1b)]
        codes = []
        for proc, log in procs:
            codes.append(proc.wait(timeout=300))
        outs = []
        for _, log in procs:
            with open(log, errors="replace") as f:
                outs.append(f.read())
        self.assertEqual(sorted(codes), [0, 3], "concurrent starts of instance 1: %s\n%s" % (codes, "\n".join(outs)))
        loser = codes.index(3)
        self.assertRegex(outs[loser], r"another start of instance 1 is in progress|"
                                      r"instance 1 is already running from")
        winner = (fw1, fw1b)[1 - loser]
        self.wait_status(5102, winner, 1)
        self.assertEqual(self.alive_roles(winner, 1), set(STAGES))
        self.assertEqual(self.alive_roles((fw1, fw1b)[loser], 1), set())
        self.assert_streaming(5002, "instance 0")

        # -- --clean removes only instance 1's resources, keeps the fence -------
        dc = os.path.join(winner, "_data_control")
        with open(os.path.join(dc, "inst1_fence_term"), "w") as f:
            f.write("7\n")
        shm0 = shm_names()
        rc, out = box.sh(winner, "./daq_stop.sh", "1", "--clean")
        self.assertEqual(rc, 0, out)
        self.assertEqual(shm_names("inst1_"), set(), "instance 1 shared memory left after --clean")
        self.assertEqual(shm_names(), shm0, "--clean of instance 1 touched instance 0's shared memory")
        left = sorted(n for n in os.listdir(dc) if n.startswith("inst1_") and ("fw_" in n or "bw_" in n))
        self.assertEqual(left, [], "instance 1 FIFOs left after --clean")
        self.assertTrue(os.path.isfile(os.path.join(dc, "inst1_fence_term")), "--clean removed inst1_fence_term")
        with open(os.path.join(dc, "inst1_fence_term")) as f:
            self.assertEqual(f.read(), "7\n")
        self.assertTrue(os.path.isfile(os.path.join(dc, "inst1_iq_track_lock")))
        self.assertTrue(os.path.exists(os.path.join(fw0, "_data_control", "fw_decimator_in")),
                        "--clean of instance 1 removed instance 0's FIFOs")
        self.assertEqual(self.alive_roles(fw0, 0), set(STAGES))
        self.assert_streaming(5002, "instance 0")

        # -- --all clears both -------------------------------------------------
        self.assert_started(winner, 1)
        self.wait_status(5102, winner, 1)
        rc, out = box.sh(fw0, "./daq_stop.sh", "--all")
        self.assertEqual(rc, 0, out)
        self.assertTrue(chain.wait_for(lambda: not chain.daq_pids(), timeout=60),
                        "DAQ processes survived daq_stop.sh --all:\n%s" % chain.ps_listing(chain.daq_pids()))
        self.assertEqual(glob.glob(os.path.join(box.run_dir, "inst*.json")) +
                         glob.glob(os.path.join(box.run_dir, "inst*.env")), [])
        self.assertTrue(chain.wait_for(lambda: not chain.listening({5000, 5001, 5002, 5100, 5101, 5102}),
                                       timeout=30))
        self.assertEqual(self.alive_roles(fw0, 0) | self.alive_roles(winner, 1), set())


class TestLegacyInstances(_TwoInstanceBase):
    TAG = "multileg"

    def test_second_copy_without_host_keys_is_today_s_start(self):
        box = self.box
        legacy = os.path.join(box.root, "legacy1.ini")
        shutil.copy2(box.ini, legacy)
        chain.set_ini(legacy, {"federation": {"instance_id": "1", "port_stride": "100"}})
        cfg = chain.read_ini(legacy)
        self.assertFalse(cfg.has_section("host") and cfg.items("host"), "the legacy INI must carry no [host] key")
        copy_a = os.path.join(box.root, "copies", "a")
        copy_b = os.path.join(box.root, "copies", "b")
        self.provision("--instance", "1", "--from-config", legacy, "--dest", copy_a, "--write")
        self.provision("--instance", "1", "--from-config", legacy, "--dest", copy_b, "--write")
        fwa = os.path.join(copy_a, "Firmware")
        fwb = os.path.join(copy_b, "Firmware")

        out_a = self.assert_started(fwa, 1)
        self.wait_status(5102, fwa, 1)
        out_b = self.assert_started(fwb, 1)
        for out in (out_a, out_b):
            self.assertEqual([l for l in out.splitlines() if REGISTRY_LINE.search(l)], [],
                             "registry/lock output on the default path:\n%s" % out)
        self.assertIn("WARN: port 5101 is in use by", out_b, "the advisory port probe did not report :5101")
        self.assertEqual(os.listdir(box.run_dir), [], "registry written with instance_registry absent")
        # Today's outcome: nothing stopped the first copy's chain and the
        # second copy's stages were launched and keep running
        time.sleep(5)
        self.assertEqual(self.alive_roles(fwb, 1), set(STAGES),
                         "the second copy's chain is not running\n%s" % box.logs(fwb, 1))
        self.assertEqual(os.listdir(box.run_dir), [])


class _FrameReader(threading.Thread):
    """A real ``:5000``-protocol client reading every frame of one instance
    until stopped: cpi_index steps, slot-102 values, the first error."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.cpis = []
        self.overruns = []
        self.error = None
        self._stop_evt = threading.Event()

    def run(self):
        try:
            client = chain.connect_iq(self.port)
        except AssertionError as e:
            self.error = str(e)
            return
        try:
            # buffers delay_sync filled before this client connected
            for _ in range(chain.STALE_FRAMES):
                client.frame()
            while not self._stop_evt.is_set():
                hdr = client.frame()[1]
                self.cpis.append(hdr.cpi_index)
                self.overruns.append(hdr.get_buffer_overrun_cnt())
        except (OSError, ValueError) as e:
            if not self._stop_evt.is_set():
                self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            client.close()

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=30)

    def gaps(self):
        return [(a, b) for a, b in zip(self.cpis, self.cpis[1:]) if b != a + 1]


class TestSoak(_TwoInstanceBase):
    TAG = "soak"
    SOAK_SEC, SOAK_ERROR = soak_seconds()

    def setUp(self):
        if self.SOAK_ERROR:
            self.fail(self.SOAK_ERROR)
        if self.SOAK_SEC <= 0:
            self.skipTest("soak case: set HEIMDALL_SOAK_SEC=<seconds> (the nightly CI job runs 600)")
        super().setUp()
        chain.set_ini(self.box.ini, {"calibration": {"cal_track_mode": "0"},
                                     "monitoring": {"drop_window_sec": str(SOAK_DROP_WINDOW)}})
        cfg = chain.read_ini(self.box.ini)
        self.frame_period = (cfg.getint("pre_processing", "cpi_size") *
                             cfg.getint("pre_processing", "decimation_ratio") /
                             float(cfg.getint("daq", "sample_rate")))

    def test_two_instance_soak(self):
        box = self.box
        self.provision("--instance", "0", "--in-place", "--write")
        self.provision("--instance", "1", "--write", *self.cores_for(1))
        fws = {0: box.fw, 1: os.path.join(box.root, "instances", "1", "Firmware")}
        ports = {n: chain.ports_of(os.path.join(fw, "daq_chain_config.ini")) for n, fw in fws.items()}
        self.assertEqual({n: p[2] for n, p in ports.items()}, {0: 5002, 1: 5102})
        for n, fw in fws.items():
            ini = os.path.join(fw, "daq_chain_config.ini")
            self.assertEqual(ini_value(ini, "calibration", "cal_track_mode"), "0", ini)
        for n, fw in fws.items():
            self.assert_started(fw, n)
        for n, fw in fws.items():
            self.wait_status(ports[n][2], fw, n)
            self.wait_locked(ports[n][2], fw, n)
        for n, fw in fws.items():
            self.assertTrue(chain.wait_steady(ports[n][2]), "instance %d: no steady locked DATA stream: %s\n%s"
                            % (n, chain.status(ports[n][2]), box.logs(fw, n)))

        readers = {n: _FrameReader(ports[n][0]) for n in fws}
        for r in readers.values():
            r.start()
        try:
            self._soak(fws, ports, readers)
        finally:
            for r in readers.values():
                r.stop()
        for n, r in readers.items():
            self.assertIsNone(r.error, "instance %d :%d client: %s" % (n, r.port, r.error))
            self.assertGreater(len(r.cpis), 1, "instance %d: the :%d client got no frames" % (n, r.port))
            self.assertEqual(r.gaps(), [], "instance %d: cpi_index gaps on :%d" % (n, r.port))
            self.assertEqual([c for c in r.overruns if c], [], "instance %d: slot 102 non-zero" % n)

    def _soak(self, fws, ports, readers):
        box = self.box
        # Warm-up: drops from before the clients connected leave the window
        quiet = lambda n: (chain.status(ports[n][2]) or {}).get("recent_drops") == 0  # noqa: E731
        for n, fw in fws.items():
            self.assertTrue(chain.wait_for(lambda: quiet(n), timeout=SOAK_DROP_WINDOW + 60),
                            "instance %d: recent_drops did not settle to 0 with a client reading: %s\n%s"
                            % (n, chain.status(ports[n][2]), box.logs(fw, n)))
        base = {}
        for n in fws:
            st = chain.status(ports[n][2])
            self.assertIsNotNone(st, "instance %d: no STATUS at the soak start" % n)
            base[n] = st
        violations = {n: [] for n in fws}
        t_end = time.monotonic() + self.SOAK_SEC
        sys.stderr.write("[test_multi_instance] soak: %.0f s on instances %s\n" % (self.SOAK_SEC, sorted(fws)))
        last = dict(base)
        while time.monotonic() < t_end:
            time.sleep(min(SOAK_PERIOD, max(0.0, t_end - time.monotonic())))
            for n in fws:
                st = chain.status(ports[n][2])
                if st is None:
                    violations[n].append("no STATUS reply")
                    continue
                last[n] = st
                problems = self.soak_problems(base[n], st)
                if problems:
                    violations[n].append("; ".join(problems))
            dead = {n: r.error for n, r in readers.items() if r.error}
            if dead:
                self.fail("IQ client failed during the soak: %s" % dead)
        expected = self.SOAK_SEC / self.frame_period
        for n, fw in fws.items():
            self.assertEqual(violations[n][:20], [], "instance %d soak violations (%d samples):\n%s"
                             % (n, len(violations[n]), box.logs(fw, n)))
            advanced = last[n]["frame_count"] - base[n]["frame_count"]
            self.assertGreaterEqual(advanced, 0.5 * expected,
                                    "instance %d: frame_count advanced %d over %.0f s (paced: ~%.0f)"
                                    % (n, advanced, self.SOAK_SEC, expected))
            self.assertEqual(self.alive_roles(fw, n), set(STAGES), "instance %d lost a stage during the soak" % n)
        sys.stderr.write("[test_multi_instance] soak ok: %s\n" % json.dumps(
            {n: {"frames": last[n]["frame_count"] - base[n]["frame_count"],
                 "client_frames": len(readers[n].cpis),
                 "cpi_gaps": last[n]["counters"].get("cpi_gaps")} for n in fws}, sort_keys=True))

    @staticmethod
    def soak_problems(base, st):
        """The G39 soak criteria *st* violates against the post-lock *base*."""
        out = []
        counters, counters0 = st.get("counters") or {}, base.get("counters") or {}
        if counters.get("cpi_gaps") != counters0.get("cpi_gaps"):
            out.append("cpi_gaps %s -> %s" % (counters0.get("cpi_gaps"), counters.get("cpi_gaps")))
        if counters.get("buffer_overrun_cnt") != 0:
            out.append("buffer_overrun_cnt %s" % counters.get("buffer_overrun_cnt"))
        if st.get("recent_drops") != 0:
            out.append("recent_drops %s" % st.get("recent_drops"))
        for key in ("dropped_frames_iq", "dropped_frames_hwc"):
            if counters.get(key) != counters0.get(key):
                out.append("%s %s -> %s" % (key, counters0.get(key), counters.get(key)))
        if st.get("frame_type") != IQHeader.FRAME_TYPE_DUMMY and (st.get("sync_state") or 0) < chain.SYNC_LOCKED:
            out.append("sync_state %s (frame_type %s)" % (st.get("sync_state"), st.get("frame_type")))
        return out


if __name__ == "__main__":
    unittest.main()
