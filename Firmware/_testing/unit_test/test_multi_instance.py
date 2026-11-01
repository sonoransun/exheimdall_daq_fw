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

Both cases stop every DAQ process on the host (``daq_stop.sh --all``), so they
skip when another chain is already running.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_synthetic_chain as chain  # noqa: E402

REPO_INI = os.path.join(chain.FIRMWARE, "daq_chain_config.ini")
STAGES = ("synthesizer", "rebuffer", "decimate", "delay_sync")
SYNC_TIMEOUT = chain.sync_timeout(90)
#: lines the registry / lock / mutex helpers print (daq_common.sh)
REGISTRY_LINE = re.compile(r"registry|instance lock|startup mutex|already running|also running|"
                           r"another start of instance|taking over", re.IGNORECASE)


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
        if (os.cpu_count() or 1) < 4:
            raise unittest.SkipTest("two instances need at least 4 CPUs")
        if chain.daq_pids():
            raise unittest.SkipTest("another DAQ chain is running on this host (the lane stops every chain)")

    def setUp(self):
        self.repo_ini_sha = sha256(REPO_INI)
        self.box = chain.Sandbox(self.TAG)
        # The repository INI with the monitoring keys every check below reads
        chain.set_ini(self.box.ini, {"monitoring": chain.MONITORING,
                                     "data_interface": {"out_data_iface_type": "eth"},
                                     "federation": {"instance_id": "0", "port_stride": "100"}})

    def tearDown(self):
        self.box.cleanup()
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
        """--cores for instance N > 0 on a host with fewer than 4(N+1) CPUs."""
        n = os.cpu_count() or 4
        if 4 * instance_id + 3 < n:
            return []
        return ["--cores", ",".join(str((4 * instance_id + k) % n) for k in range(4))]

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
                        "DAQ processes survived daq_stop.sh --all: %s" % sorted(chain.daq_pids()))
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
        self.assertFalse(os.path.exists(os.path.join(fwb, "_logs", "inst1", "pids", "run_dir")))
        self.assertEqual(os.listdir(box.run_dir), [])


if __name__ == "__main__":
    unittest.main()
