"""
    Description :
    Tests for configuration validation and discovery (design spec A.1, A.4,
    A.6, A.7, B.6, C.2, C.4.1, D.6.1, G.2, I.3, J.4):

        python3 ini_checker.py [config_path] [no_hw] [attached] [--federation]
            exit 0 = config valid, exit 1 = errors found
            'no_hw'    skips ALL hardware probing (no lsusb / rtl_eeprom)
                       and reads nothing but the INI
            'attached' adds the sysfs serial + host-registry checks
            warnings go to stderr as 'WARNING: <text>' (exit code unchanged)

    plus hw_discover.discover_rtlsdr() / 'hw_discover.py --sdr' and the
    atomic INI rewrite of auto_config.py.

    Hardware-free and CI-safe: fake sysfs trees, fake registry directories,
    PATH-stubbed lsusb/rtl_eeprom, temp configs. The live
    Firmware/daq_chain_config.ini is only read, never modified.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import builtins
import configparser
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from os.path import join, dirname, realpath
from unittest import mock

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
INI_CHECKER = join(root_path, "ini_checker.py")
HW_DISCOVER = join(daq_core_path, "hw_discover.py")
LIVE_CONFIG = join(root_path, "daq_chain_config.ini")
PRESET_DIR = join(dirname(root_path), "config_files")

sys.path.insert(0, root_path)
sys.path.insert(0, daq_core_path)
import ini_checker  # noqa: E402
import hw_discover  # noqa: E402
import auto_config  # noqa: E402

# Environment of every CLI run: no inherited test hooks
_HOOKS = ("HEIMDALL_SYSFS_ROOT", "HEIMDALL_RUN_DIR", "HEIMDALL_PROC_ROOT")


def run_checker(args, cwd, env_extra=None, path_prefix=None):
    env = {k: v for k, v in os.environ.items() if k not in _HOOKS}
    if env_extra:
        env.update(env_extra)
    if path_prefix:
        env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
    return subprocess.run([sys.executable, INI_CHECKER] + list(args),
                          cwd=cwd, capture_output=True, text=True,
                          timeout=120, env=env)


def live_params():
    """Parsed copy of the shipped INI (the same dict read_config_file returns)."""
    params = ini_checker.read_config_file(LIVE_CONFIG)
    return {s: dict(v) for s, v in params.items()}


def warning_lines(stderr):
    return [l for l in stderr.splitlines() if l.startswith("WARNING: ")]


def write_ini(path, params):
    parser = configparser.ConfigParser()
    for section, values in params.items():
        parser[section] = values
    with open(path, "w") as f:
        parser.write(f)


def make_sysfs(root, devices):
    """Fake /sys/bus/usb/devices: devices = [(name, vendor, product, serial|None)]."""
    os.makedirs(root, exist_ok=True)
    for i, (name, vendor, product, serial) in enumerate(devices):
        d = join(root, name)
        os.makedirs(d)
        attrs = {"idVendor": vendor, "idProduct": product,
                 "busnum": "1", "devnum": str(10 + i)}
        if serial is not None:
            attrs["serial"] = serial
        for k, v in attrs.items():
            with open(join(d, k), "w") as f:
                f.write(v + "\n")
    # An interface directory without device attributes is ignored
    os.makedirs(join(root, "1-1:1.0"), exist_ok=True)
    return root


def kraken_devices(serials, prefix="1-1."):
    return [("{}{}".format(prefix, i + 1), "0bda", "2838", s) for i, s in enumerate(serials)]


# ---------------------------------------------------------------------------
# CLI contract (unchanged legacy behaviour)
# ---------------------------------------------------------------------------

class TesterIniCheckerContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ini_checker_")
        cls.valid_ini = join(cls.tmpdir, "valid.ini")
        shutil.copyfile(LIVE_CONFIG, cls.valid_ini)
        parser = configparser.ConfigParser()
        parser.read(cls.valid_ini)
        parser["hw"]["num_ch"] = "0"
        cls.broken_ini = join(cls.tmpdir, "broken.ini")
        with open(cls.broken_ini, "w") as f:
            parser.write(f)
        # Empty working dir: no daq_chain_config.ini fallback present
        cls.empty_cwd = join(cls.tmpdir, "empty")
        os.makedirs(cls.empty_cwd)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_valid_config_exits_zero_silently(self):
        res = run_checker([self.valid_ini, "no_hw"], cwd=self.empty_cwd)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout + res.stderr, "")

    def test_broken_config_exits_one(self):
        res = run_checker([self.broken_ini, "no_hw"], cwd=self.empty_cwd)
        self.assertEqual(res.returncode, 1)

    def test_no_hw_skips_hardware_probing(self):
        res = run_checker([self.valid_ini, "no_hw"], cwd=self.empty_cwd)
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("lsusb", (res.stdout + res.stderr).lower())

    def test_unknown_sections_and_keys_non_fatal(self):
        augmented = join(self.tmpdir, "augmented.ini")
        shutil.copyfile(self.valid_ini, augmented)
        with open(augmented, "a") as f:
            f.write("\n[completely_unknown_section]\nmystery_key = 42\n")
        res = run_checker([augmented, "no_hw"], cwd=self.empty_cwd)
        self.assertEqual(res.returncode, 0, "unknown sections must not fail validation")

    def test_config_path_defaults_to_cwd(self):
        legacy_cwd = join(self.tmpdir, "legacy")
        os.makedirs(legacy_cwd, exist_ok=True)
        shutil.copyfile(self.valid_ini, join(legacy_cwd, "daq_chain_config.ini"))
        res = run_checker(["no_hw"], cwd=legacy_cwd)
        self.assertEqual(res.returncode, 0, "legacy invocation (cwd config + no_hw) broke")
        res = run_checker(["no_hw", "attached"], cwd=legacy_cwd)
        self.assertEqual(res.returncode, 0, "tokens in any order, path from the cwd")

    def test_missing_config_is_an_error(self):
        res = run_checker([join(self.tmpdir, "does_not_exist.ini"), "no_hw"], cwd=self.empty_cwd)
        self.assertNotEqual(res.returncode, 0)

    def test_shipped_presets_no_error_no_warning(self):
        """WP-11 acceptance: every preset passes no_hw without error or WARNING."""
        presets = sorted(join(PRESET_DIR, d, "daq_chain_config.ini") for d in os.listdir(PRESET_DIR)
                         if os.path.isfile(join(PRESET_DIR, d, "daq_chain_config.ini")))
        self.assertTrue(presets)
        for preset in presets + [LIVE_CONFIG]:
            res = run_checker([preset, "no_hw"], cwd=self.empty_cwd)
            self.assertEqual(res.returncode, 0, "{}: {}".format(preset, res.stderr))
            self.assertEqual(warning_lines(res.stderr), [], preset)

    def test_shipped_ini_documents_keys_only_in_comments(self):
        """The shipped INI gains commented documentation, no active new key."""
        params = live_params()
        self.assertNotIn("host", params)
        for key in ("serial_base", "device_serials", "antenna_switch_gpio"):
            self.assertNotIn(key, params["hw"])
        for key in ("zmq_listen_address", "ctl_max_connections", "usb_ring_buffers"):
            self.assertNotIn(key, params["daq"])
        with open(LIVE_CONFIG) as f:
            text = f.read()
        for key in ("serial_base", "device_serials", "zmq_listen_address", "ctl_max_connections",
                    "status_update_interval", "cluster_members", "instance_registry", "cpu_cores"):
            self.assertIn(";" + key + " =", text)


# ---------------------------------------------------------------------------
# Grammar of the new keys and the new limits (errors)
# ---------------------------------------------------------------------------

class TesterNewKeyErrors(unittest.TestCase):

    def check(self, mutate, hw=False):
        params = live_params()
        mutate(params)
        return ini_checker.check_ini(params, hw)

    def assertClean(self, mutate):
        self.assertEqual(self.check(mutate), [])

    def assertError(self, mutate, needle):
        errors = self.check(mutate)
        self.assertTrue(any(needle in e for e in errors), "expected '{}' in {}".format(needle, errors))

    def test_live_config_clean(self):
        self.assertEqual(ini_checker.check_ini(live_params(), False), [])

    def test_num_ch_limit(self):
        def set_ch(n):
            def m(p):
                p["hw"]["num_ch"] = str(n)
                del p["amplification"]  # per-channel lists of length 5
            return m
        self.assertClean(set_ch(31))
        self.assertError(set_ch(32), "the 128-byte :1130 control message holds 31 per-channel values")

    def test_cpi_and_corr_limits(self):
        def pre(k, v):
            return lambda p: p["pre_processing" if k == "cpi_size" else "calibration"].__setitem__(k, v)
        self.assertClean(pre("cpi_size", str(2 ** 23)))
        self.assertError(pre("cpi_size", str(2 ** 23 + 1)), "CPI size must be at most 8388608")
        self.assertClean(pre("corr_size", str(2 ** 23)))
        self.assertError(pre("corr_size", str(2 ** 24)), "correlation size must be at most 8388608")

    def test_unit_id_auto(self):
        self.assertClean(lambda p: p["hw"].__setitem__("unit_id", "auto"))
        self.assertError(lambda p: p["hw"].__setitem__("unit_id", "automatic"), "Unit ID must be integer")

    def test_serial_base(self):
        def base(v, ctr=None):
            def m(p):
                p["hw"]["serial_base"] = v
                if ctr is not None:
                    p["daq"]["ctr_channel_serial_no"] = ctr
            return m
        self.assertClean(base("1100", "1100"))
        self.assertClean(base("1000"))
        self.assertClean(base(str(ini_checker.SERIAL_BASE_MAX), str(ini_checker.SERIAL_BASE_MAX)))
        self.assertError(base(str(ini_checker.SERIAL_BASE_MAX + 1)), "[hw] serial_base must be an integer")
        self.assertError(base("-1"), "[hw] serial_base")
        self.assertError(base(""), "[hw] serial_base")
        self.assertError(base("1100"), "ctr_channel_serial_no 1000 is not one of the configured serials")

    def test_device_serials(self):
        def serials(v, ctr="1000"):
            def m(p):
                p["hw"]["device_serials"] = v
                p["daq"]["ctr_channel_serial_no"] = ctr
            return m
        self.assertClean(serials("1000, 1001,1002,1003,1004"))
        self.assertClean(serials("42,43,44,45,46", "42"))
        self.assertClean(serials(""))  # empty = absent (rtl_daq rule)
        self.assertError(serials("1000,1001,1002,1003"), "device_serials has 4 entries but num_ch is 5")
        self.assertError(serials("1000,1001,1002,1003,1001"), "device_serials entry 4 duplicates entry 1")
        self.assertError(serials("1000,1001,10x2,1003,1004"), "entry 2 is not a decimal serial of 1-8 digits")
        self.assertError(serials("1000,1001,1002,1003,123456789"), "entry 4 is not a decimal serial")
        self.assertError(serials("1,2,3,4,5"), "ctr_channel_serial_no 1000 is not one of the configured serials")
        self.assertError(lambda p: (p["hw"].__setitem__("num_ch", "31"), p.pop("amplification"),
                                    p["hw"].__setitem__("device_serials", ", ".join(
                                        "{:08d}".format(10000000 + i) for i in range(31))),
                                    p["daq"].__setitem__("ctr_channel_serial_no", "10000000")),
                         "device_serials line is longer than the 199 bytes")

    def test_antenna_switch_gpio(self):
        def gpio(v):
            return lambda p: p["hw"].__setitem__("antenna_switch_gpio", v)
        for ok in ("none", "23,24", "0, 27", ""):
            self.assertClean(gpio(ok))
        for bad in ("23,23", "28,1", "a,b", "5", "1,2,3", "None"):
            self.assertError(gpio(bad), "[hw] antenna_switch_gpio must be 'none'")

    def test_zmq_listen_address(self):
        for ok in ("*", "0.0.0.0", "127.0.0.1", "localhost", ""):
            self.assertClean(lambda p, v=ok: p["daq"].__setitem__("zmq_listen_address", v))
        self.assertError(lambda p: p["daq"].__setitem__("zmq_listen_address", "10.0.0.5"),
                         "binding another address would cut the local chain")

    def test_daq_numeric_keys(self):
        cases = {"usb_ring_buffers": ("2", "64", "1", "65"),
                 "usb_async_transfers": ("12", "2", "0", "x"),
                 "ctl_max_connections": ("1", "4", "0", "1.5"),
                 "ctl_request_timeout_sec": ("10.0", "600", "0.5", "nan"),
                 "ctl_handler_max_sec": ("12", "120", "121", ""),
                 "ctl_xack_idle_sec": ("0", "86400", "-1", "86401"),  # hw_controller: 0 = off
                 "ctl_idle_timeout_sec": ("0", "300", "-1", "inf")}
        for key, (ok1, ok2, bad1, bad2) in cases.items():
            for ok in (ok1, ok2):
                self.assertClean(lambda p, k=key, v=ok: p["daq"].__setitem__(k, v))
            for bad in (bad1, bad2):
                self.assertError(lambda p, k=key, v=bad: p["daq"].__setitem__(k, v), "[daq] " + key)

    def test_offload_worker_keys_accept_auto(self):
        """[offload] fft_workers/xcorr_workers: 1..64, auto (any case) or empty, like resolve_workers()."""
        for key in ("fft_workers", "xcorr_workers"):
            for ok in ("auto", "AUTO", " Auto ", "", "1", "64"):
                self.assertClean(lambda p, k=key, v=ok: p["offload"].__setitem__(k, v))
            for bad in ("0", "abc", "65", "-2", "4.5", "automatic"):
                self.assertError(lambda p, k=key, v=bad: p["offload"].__setitem__(k, v),
                                 "[offload] {} must be 'auto' or an integer in 1..64".format(key))

    def test_ctr_serial_compared_as_string(self):
        """rtl_daq looks the control channel up by sprintf("%d"): zero-padded serials never match."""
        def padded(p):
            p["hw"]["device_serials"] = "01100,01101,01102,01103,01104"
            p["daq"]["ctr_channel_serial_no"] = "1100"
        self.assertError(padded, "write them without leading zeros")
        self.assertClean(lambda p: (p["hw"].__setitem__("device_serials", "1100,1101,1102,1103,1104"),
                                    p["daq"].__setitem__("ctr_channel_serial_no", "1100")))

    def test_peer_down_misses_empty_is_absent(self):
        """An uncommented ';peer_down_misses =' keeps the time rule (opt_int, empty = absent)."""
        self.assertClean(lambda p: p["federation"].__setitem__("peer_down_misses", ""))
        self.assertClean(lambda p: p["federation"].__setitem__("peer_down_misses", "3"))
        self.assertError(lambda p: p["federation"].__setitem__("peer_down_misses", "0"),
                         "[federation] peer_down_misses")
        self.assertError(lambda p: p["federation"].__setitem__("peer_down_misses", "x"),
                         "[federation] peer_down_misses")

    def test_other_section_keys(self):
        cases = [("pre_processing", "size_segments_from_config", "1", "2"),
                 ("calibration", "amplitude_tolerance_mode", "symmetric", "loose"),
                 ("calibration", "track_decimation", "4", "0"),
                 ("calibration", "cal_timeout_frames", "0", "-3"),
                 ("calibration", "invalidate_on_overrun", "1", "yes"),
                 ("calibration", "frames_after_overrun", "1", "x"),
                 ("amplification", "apply_bias_tee_at_init", "1", "true"),
                 ("offload", "fft_workers", "8", "0"),
                 ("data_interface", "iq_server_max_clients", "4", "0"),
                 ("data_interface", "iq_server_frame_deadline_ms", "250", "-1"),
                 ("database", "instance_subdir", "1", "2"),
                 ("monitoring", "status_extended", "1", "on"),
                 ("monitoring", "status_update_interval", "10", "-1"),
                 ("monitoring", "stale_after_sec", "14.1", "-2"),
                 ("monitoring", "status_max_clients", "8", "0")]
        for section, key, ok, bad in cases:
            self.assertClean(lambda p, s=section, k=key, v=ok: p[s].__setitem__(k, v))
            self.assertError(lambda p, s=section, k=key, v=bad: p[s].__setitem__(k, v),
                             "[{}] {}".format(section, key))

    def test_host_section(self):
        def host(**kw):
            return lambda p: p.__setitem__("host", dict(kw))
        self.assertClean(host(cpu_cores="4,5,6,7", rt_priority_offset="2", allow_core_sharing="allow",
                              host_tuning="auto", preflight="warn", instance_registry="1",
                              instance_guard="refuse", delay_sync_cores="2,3", usb_irq_core="3",
                              usb_controller="xhci_hcd:usb3", health_thread_sched_other="1"))
        self.assertClean(host(cpu_cores="", delay_sync_cores="", usb_irq_core="", usb_controller=""))
        self.assertError(host(cpu_cores="0,1,2"), "exactly 4 distinct core numbers")
        self.assertError(host(cpu_cores="0,1,1,2"), "exactly 4 distinct core numbers")
        self.assertError(host(cpu_cores="0,1,x,2"), "comma list of non-negative integers")
        self.assertError(host(rt_priority_offset="82"), "[host] rt_priority_offset")
        self.assertError(host(allow_core_sharing="maybe"), "[host] allow_core_sharing")
        self.assertError(host(host_tuning="yes"), "[host] host_tuning")
        self.assertError(host(preflight="on"), "[host] preflight")
        self.assertError(host(instance_guard="refuse"), "instance_guard = refuse requires [host] instance_registry = 1")
        self.assertError(host(usb_irq_core="-1"), "[host] usb_irq_core")

    def test_cpu_cores_vs_nproc_hardware_mode_only(self):
        params = live_params()
        params["host"] = {"cpu_cores": "1000,1001,1002,1003"}
        self.assertEqual(ini_checker._check_scaling_keys(params, False), [])
        errors = ini_checker._check_scaling_keys(params, True)
        self.assertTrue(any("exceed the" in e for e in errors), errors)

    def test_federation_instance_keys(self):
        def fed(**kw):
            return lambda p: p["federation"].update(kw)
        self.assertClean(fed(node_id="hosta:1", cluster_id="lab-a", health_mode="mesh",
                             health_poll_interval="1.0", peer_timeout="0.5", peer_up_successes="2",
                             peer_down_misses="3", health_workers="4", stamp_node_id="1",
                             auth_token="s3cr3t!", allow_cidrs="10.0.0.0/8, fd00::/8",
                             fence_max_term_jump="1000"))
        self.assertError(fed(node_id="-bad"), "node_id '-bad' does not match")
        self.assertError(fed(cluster_id="x" * 32), "cluster_id")
        self.assertError(fed(auth_token="has space"), "auth_token must be printable")
        self.assertError(fed(allow_cidrs="10.0.0.0/8,banana"), "allow_cidrs entry 'banana'")
        self.assertError(fed(peer_timeout="31"), "[federation] peer_timeout")
        self.assertError(fed(health_workers="0"), "[federation] health_workers")
        self.assertError(fed(register_with_coordinator="1"), "requires en_federation = 1")
        self.assertError(fed(health_mode="hub", en_federation="1"), "health_mode = hub requires")
        self.assertError(fed(advertise_host="localhost"), "loopback")

    def test_cluster_members_grammar(self):
        def members(v):
            return lambda p: p["federation"].update(cluster_members=v, en_federation="1")
        self.assertError(members("hosta:0,hosta:0"), "duplicate member 'hosta:0'")
        self.assertError(members("localhost:0"), "uses a loopback host")
        self.assertError(members("hosta"), "bad member entry 'hosta'")
        self.assertError(members("hosta:0@-x"), "bad node_id '-x'")

    def test_coordinator_router_keys_in_instance_ini(self):
        def fed(**kw):
            return lambda p: p["federation"].update(kw)
        # Existing key alone keeps today's validation (no new pairwise rule)
        self.assertClean(fed(coordinator_port="7000"))
        self.assertError(fed(coordinator_port="7000", router_queue_frames="4"),
                         "coordinator_port and router_port are both 7000")
        self.assertError(fed(election_missed_polls="1"), "[federation] election_missed_polls")
        self.assertError(fed(router_drop_policy="random"), "[federation] router_drop_policy")
        self.assertError(fed(coordinator_candidates="hosta:6000,hosta:6000"), "lists 'hosta:6000' twice")
        self.assertError(fed(coordinator_candidates="hosta"), "is not host:port")

    def test_peer_list_problems_escalate_with_cluster_keys(self):
        def fed(**kw):
            return lambda p: p["federation"].update(kw)
        self.assertClean(fed(peer_list="hostb:x"))  # a warning only
        self.assertError(fed(peer_list="hostb:x", cluster_members="hosta:0,hostb:0"), "peer_list: bad member entry")

    def test_check_ini_reads_nothing_but_parameters(self):
        """check_ini/check_ini_warnings in no_hw mode touch no file, process or
        sysfs, even with every new key set."""
        params = live_params()
        params["hw"].update(serial_base="1100")
        params["daq"]["ctr_channel_serial_no"] = "1100"
        params["host"] = {"instance_registry": "1", "instance_guard": "refuse"}
        params["federation"].update(en_federation="1", cluster_members="hosta:0,hostb:0",
                                    peer_list="hostb:5002", coordinator_host="hosta")
        boom = AssertionError("filesystem/process access from check_ini")
        with mock.patch.object(builtins, "open", side_effect=boom), \
                mock.patch.object(os, "listdir", side_effect=boom), \
                mock.patch.object(os, "scandir", side_effect=boom), \
                mock.patch.object(subprocess, "run", side_effect=boom), \
                mock.patch.object(ini_checker, "enumerate_sdr_sysfs", side_effect=boom), \
                mock.patch.object(ini_checker, "read_live_registry", side_effect=boom):
            ini_checker.check_ini(params, False)
            ini_checker.check_ini_warnings(params, False)


# ---------------------------------------------------------------------------
# Warnings (stderr only, exit code unchanged)
# ---------------------------------------------------------------------------

class TesterWarnings(unittest.TestCase):

    def warns(self, mutate):
        params = live_params()
        mutate(params)
        self.assertEqual(ini_checker.check_ini(params, False), [], "warning cases must not error")
        return ini_checker.check_ini_warnings(params, False)

    def assertWarn(self, mutate, needle):
        w = self.warns(mutate)
        self.assertTrue(any(needle in x for x in w), "expected '{}' in {}".format(needle, w))

    def test_live_config_has_no_warning(self):
        self.assertEqual(ini_checker.check_ini_warnings(live_params(), False), [])

    def test_corr_size_multiple_of_1024(self):
        self.assertWarn(lambda p: p["calibration"].__setitem__("corr_size", "65000"),
                        "corr_size = 65000 is not a multiple of 1024")

    def test_burst_below_max_sync_fails(self):
        self.assertWarn(lambda p: p["calibration"].__setitem__("cal_frame_burst_size", "5"),
                        "cal_frame_burst_size = 5 is below maximum_sync_fails = 10")
        self.assertEqual(self.warns(lambda p: p["calibration"].update(cal_frame_burst_size="5",
                                                                        cal_track_mode="1")), [])

    def test_unit_id_zero_on_federated_instance(self):
        self.assertWarn(lambda p: p["federation"].update(instance_id="1", en_federation="1"),
                        "[hw] unit_id = 0 on instance 1 with federation enabled")
        w = self.warns(lambda p: (p["federation"].update(instance_id="1", en_federation="1"),
                                  p["hw"].__setitem__("unit_id", "auto")))
        self.assertFalse(any("unit_id" in x for x in w), w)

    def test_frame_period_and_ctl_timeout(self):
        w = self.warns(lambda p: (p["pre_processing"].__setitem__("cpi_size", str(2 ** 23)),
                                  p["daq"].__setitem__("sample_rate", "1000000")))
        self.assertTrue(any("the frame period is 8.4 s" in x for x in w), w)
        self.assertTrue(any("ctl_request_timeout_sec = 10 is below 2 x the frame period (8.4 s): control "
                            "verbs will time out on a healthy chain" in x for x in w), w)
        w = self.warns(lambda p: (p["pre_processing"].__setitem__("cpi_size", str(2 ** 22)),
                                  p["daq"].update(sample_rate="1000000", ctl_request_timeout_sec="20")))
        self.assertEqual(w, [])

    def test_ctl_max_connections_with_federation(self):
        needle = "a resident DoA client blocks every coordinator and heimdall-ctl command on this member"
        self.assertWarn(lambda p: p["federation"].__setitem__("en_federation", "1"), needle)
        w = self.warns(lambda p: (p["federation"].__setitem__("en_federation", "1"),
                                  p["daq"].__setitem__("ctl_max_connections", "4")))
        self.assertFalse(any(needle in x for x in w), w)

    def test_status_snapshot_freshness(self):
        def mon(**kw):
            return lambda p: p["monitoring"].update(en_monitoring="1", en_status_server="1", **kw)
        self.assertWarn(mon(heartbeat_interval="0"),
                        "the STATUS snapshot is never refreshed (set [monitoring] status_update_interval)")
        self.assertEqual(self.warns(mon(heartbeat_interval="0", status_update_interval="10")), [])
        self.assertEqual(self.warns(mon()), [])  # 100 x 0.437 s = 43.7 s
        self.assertWarn(mon(heartbeat_interval="200"), "the STATUS snapshot refreshes every 87.4 s")
        # Status server not running: nothing to warn about
        self.assertEqual(self.warns(lambda p: p["monitoring"].update(en_status_server="1",
                                                                       heartbeat_interval="0")), [])

    def test_inert_worker_keys(self):
        self.assertWarn(lambda p: p["pre_processing"].__setitem__("fft_workers", "4"),
                        "[pre_processing] fft_workers is not read by any process; use [offload] fft_workers")
        self.assertWarn(lambda p: p["calibration"].__setitem__("xcorr_workers", "4"),
                        "[calibration] xcorr_workers is not read by any process; use [offload] xcorr_workers")

    def test_port_stride_rules(self):
        self.assertWarn(lambda p: p["federation"].update(instance_id="1", port_stride="3"),
                        "port_stride = 3 is below 4 on instance 1")
        self.assertWarn(lambda p: p["federation"].update(port_stride="3"),
                        "port_stride = 3 is below 4: instances 0 and 1")
        # Errors only in new-key mode
        params = live_params()
        params["federation"].update(instance_id="1", port_stride="3")
        params["host"] = {"instance_registry": "1"}
        errors = ini_checker.check_ini(params, False)
        self.assertTrue(any("port_stride = 3 is below 4 on instance 1" in e for e in errors), errors)
        self.assertFalse(any("below 4 on instance" in w for w in ini_checker.check_ini_warnings(params, False)))

    def test_port_above_65535_and_duplicate_ports(self):
        self.assertWarn(lambda p: p["federation"].update(instance_id="700", port_stride="100"),
                        "iq port 75000 of instance 700 is above 65535")
        self.assertWarn(lambda p: p["monitoring"].update(en_monitoring="1", en_status_server="1",
                                                         status_server_port="5000"),
                        "iq port and status port of instance 0 are both 5000")

    def test_service_port_collision(self):
        text = "port 6000 of instance 10 collides with the coordinator port; with stride 100 use instance ids 0-9"
        self.assertWarn(lambda p: p["federation"].update(instance_id="10", coordinator_host="localhost"), text)
        # Remote coordinator: no collision on this host
        w = self.warns(lambda p: p["federation"].update(instance_id="10", coordinator_host="192.0.2.10"))
        self.assertFalse(any("collides" in x for x in w), w)
        # New-key mode: an error
        params = live_params()
        params["federation"].update(instance_id="10", coordinator_host="localhost", router_port="7000")
        errors = ini_checker.check_ini(params, False)
        self.assertTrue(any(text in e for e in errors), errors)

    def test_zmq_port_inside_another_window(self):
        self.assertWarn(lambda p: p["federation"].update(port_stride="1290"),
                        "with port_stride 1290 the ZMQ port 5000 of instance 3 falls inside instance 0's 5000-5003 window")

    def test_membership_warnings(self):
        def fed(**kw):
            return lambda p: p["federation"].update(en_federation="1", **kw)
        w = self.warns(fed(cluster_members="hosta.invalid:0,hostb.invalid:0"))
        self.assertTrue(any("cluster_members does not contain this instance (expected" in x for x in w), w)
        local = socket.gethostname().split(".", 1)[0].lower()
        if local and local != "localhost":
            w = self.warns(fed(cluster_members="{}:0,hostb.invalid:0".format(local)))
            self.assertFalse(any("does not contain" in x for x in w), w)
        self.assertWarn(fed(cluster_members="hosta.invalid:0", peer_list="hostb.invalid:0"),
                        "peer_list overrides cluster_members for health polling")
        self.assertWarn(fed(peer_list="hostb:x"), "peer_list: bad member entry 'hostb:x'")
        self.assertWarn(fed(peer_list="hostb:1,hostb:1"), "peer_list lists 'hostb:1' twice")
        self.assertWarn(fed(peer_list="127.0.0.1:5002"), "peer_list entry '127.0.0.1:5002' is this instance")

    def test_common_image_hostname_with_registration(self):
        with mock.patch.object(socket, "gethostname", return_value="raspberrypi"):
            w = self.warns(lambda p: p["federation"].update(en_federation="1", register_with_coordinator="1",
                                                            coordinator_host="hosta.invalid",
                                                            ctl_max_connections="4"))
            self.assertTrue(any("on a host named 'raspberrypi'" in x for x in w), w)
            w = self.warns(lambda p: p["federation"].update(en_federation="1", register_with_coordinator="1",
                                                            advertise_host="kraken-07"))
            self.assertFalse(any("raspberrypi" in x for x in w), w)

    def test_cli_warnings_on_stderr_exit_zero(self):
        tmp = tempfile.mkdtemp(prefix="ini_warn_")
        self.addCleanup(shutil.rmtree, tmp, True)
        params = live_params()
        params["calibration"]["corr_size"] = "65000"
        params["pre_processing"]["fft_workers"] = "4"
        ini = join(tmp, "warn.ini")
        write_ini(ini, params)
        for args in ([ini, "no_hw"], [ini, "no_hw", "attached"], ["no_hw", ini]):
            res = run_checker(args, cwd=tmp, env_extra={"HEIMDALL_SYSFS_ROOT": join(tmp, "nosysfs"),
                                                        "HEIMDALL_RUN_DIR": join(tmp, "norun")})
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stdout, "")
            lines = warning_lines(res.stderr)
            self.assertEqual(len(lines), 2, res.stderr)
            self.assertIn("WARNING: [calibration] corr_size = 65000 is not a multiple of 1024", res.stderr)
            self.assertIn("WARNING: [pre_processing] fft_workers is not read by any process", res.stderr)

    def test_cli_errors_and_warnings_together(self):
        tmp = tempfile.mkdtemp(prefix="ini_warn_")
        self.addCleanup(shutil.rmtree, tmp, True)
        params = live_params()
        params["calibration"]["corr_size"] = "65000"
        params["daq"]["zmq_listen_address"] = "10.1.2.3"
        ini = join(tmp, "bad.ini")
        write_ini(ini, params)
        res = run_checker([ini, "no_hw"], cwd=tmp)
        self.assertEqual(res.returncode, 1)
        self.assertIn("zmq_listen_address must be one of", res.stderr)
        self.assertEqual(len(warning_lines(res.stderr)), 1, res.stderr)


# ---------------------------------------------------------------------------
# Attached-device and registry checks (only with the 'attached' token)
# ---------------------------------------------------------------------------

class TesterAttached(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ini_attached_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sysfs = join(self.tmp, "sys")
        self.run_dir = join(self.tmp, "run")
        os.makedirs(self.run_dir)
        self.children = []

    def tearDown(self):
        for child in self.children:
            child.kill()
            child.wait(timeout=10)

    def params(self, keys=True, base="1100"):
        p = live_params()
        if keys:
            p["hw"]["serial_base"] = base
            p["daq"]["ctr_channel_serial_no"] = base
        return p

    def ini(self, params):
        path = join(self.tmp, "cfg.ini")
        write_ini(path, params)
        return path

    def cli(self, ini, *tokens):
        return run_checker([ini, "no_hw"] + list(tokens), cwd=self.tmp,
                           env_extra={"HEIMDALL_SYSFS_ROOT": self.sysfs, "HEIMDALL_RUN_DIR": self.run_dir})

    def start_stub(self, role):
        """A sleeping process whose command line contains *role*."""
        script = join(self.tmp, role + ".py")
        with open(script, "w") as f:
            f.write("import time\ntime.sleep(120)\n")
        child = subprocess.Popen([sys.executable, script])
        self.children.append(child)
        return child.pid

    def write_registry(self, iid, pids, serials=(), ports=None, db_dir=None, cwd="/opt/heimdall/instances/1/Firmware"):
        entry = {"schema": 1, "instance_id": iid, "mode": "hw", "cwd": cwd, "pids": pids,
                 "serials": list(serials), "ports": ports or {}, "db_dir": db_dir}
        with open(join(self.run_dir, "inst{}.json".format(iid)), "w") as f:
            json.dump(entry, f)

    def test_enumerate_sdr_sysfs(self):
        make_sysfs(self.sysfs, kraken_devices(["1000", "1001"]) +
                   [("2-1", "0bda", "2832", None), ("2-2", "046d", "c52b", "XYZ"), ("2-3", "0bda", "8153", "1")])
        devices = ini_checker.enumerate_sdr_sysfs(self.sysfs)
        self.assertEqual([d["serial"] for d in devices], ["1000", "1001", None])
        self.assertEqual(devices[0]["path"], join(self.sysfs, "1-1.1"))
        self.assertEqual((devices[0]["busnum"], devices[0]["devnum"]), (1, 10))
        self.assertIsNone(ini_checker.enumerate_sdr_sysfs(join(self.tmp, "missing")))
        with mock.patch.dict(os.environ, {"HEIMDALL_SYSFS_ROOT": self.sysfs}):
            self.assertEqual(len(ini_checker.enumerate_sdr_sysfs()), 3)

    def test_a_all_serials_present(self):
        make_sysfs(self.sysfs, kraken_devices(["1100", "1101", "1102", "1103", "1104"]))
        self.assertEqual(ini_checker.check_attached_serials(self.params(), self.sysfs, self.run_dir), ([], []))
        res = self.cli(self.ini(self.params()), "attached")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_b_missing_serial(self):
        make_sysfs(self.sysfs, kraken_devices(["1100", "1101", "1102", "1104"]))
        errors, warnings = ini_checker.check_attached_serials(self.params(), self.sysfs, self.run_dir)
        self.assertEqual(warnings, [])
        self.assertEqual(errors, ["configured serial 1103 (channel 3) is not attached (sysfs); plug in the "
                                  "unit or fix [hw] serial_base/device_serials"])
        res = self.cli(self.ini(self.params()), "attached")
        self.assertEqual(res.returncode, 1)
        self.assertIn("configured serial 1103 (channel 3) is not attached", res.stderr)
        # Without serial keys a missing receiver is rtl_daq's business (today's -3 fatal)
        make_sysfs(join(self.tmp, "sys2"), kraken_devices(["1000", "1001"]))
        self.assertEqual(ini_checker.check_attached_serials(self.params(keys=False), join(self.tmp, "sys2"),
                                                            self.run_dir), ([], []))
        # The plain no_hw run never looks at sysfs
        res = self.cli(self.ini(self.params()))
        self.assertEqual((res.returncode, res.stderr), (0, ""))

    def test_c_duplicate_serials(self):
        # Two factory units: serials 1000..1004 twice
        make_sysfs(self.sysfs, kraken_devices(["1000", "1001", "1002", "1003", "1004"]) +
                   kraken_devices(["1000", "1001", "1002", "1003", "1004"], prefix="3-1."))
        errors, warnings = ini_checker.check_attached_serials(self.params(base="1000"), self.sysfs, self.run_dir)
        self.assertEqual(len(errors), 5)
        self.assertIn("serial 1000 (channel 0) is present on 2 devices ({}, {})".format(
            join(self.sysfs, "1-1.1"), join(self.sysfs, "3-1.1")), errors[0])
        self.assertIn("util/set_unit_serials.sh --unit 1", errors[0])
        # Serial keys absent: the same condition is a warning on stderr, exit 0
        res = self.cli(self.ini(self.params(keys=False)), "attached")
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = warning_lines(res.stderr)
        self.assertEqual(len(lines), 5, res.stderr)
        self.assertTrue(lines[0].startswith("WARNING: serial 1000 (channel 0) is present on 2 devices"))
        res = self.cli(self.ini(self.params(base="1000")), "attached")
        self.assertEqual(res.returncode, 1)

    def test_d_no_receivers_no_keys_is_silent(self):
        make_sysfs(self.sysfs, [])
        res = self.cli(self.ini(self.params(keys=False)), "attached")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        # No sysfs at all (macOS, containers): silent without keys ...
        shutil.rmtree(self.sysfs)
        res = self.cli(self.ini(self.params(keys=False)), "attached")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        # ... and exactly one warning with keys
        res = self.cli(self.ini(self.params()), "attached")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stderr.strip(), "WARNING: sysfs not available; attached-serial checks skipped")

    def test_e_serial_used_by_live_instance(self):
        make_sysfs(self.sysfs, kraken_devices(["1100", "1101", "1102", "1103", "1104"]))
        pid = self.start_stub("delay_sync")
        self.write_registry(1, {"delay_sync": pid}, serials=["1100", "1101", "1102", "1103", "1104"])
        errors, _ = ini_checker.check_attached_serials(self.params(), self.sysfs, self.run_dir)
        self.assertIn("serial 1100 is used by instance 1 running from /opt/heimdall/instances/1/Firmware", errors)
        res = self.cli(self.ini(self.params()), "attached")
        self.assertEqual(res.returncode, 1)
        self.assertIn("serial 1100 is used by instance 1 running from", res.stderr)
        # Without keys: the default serials 1000.. held by a live instance -> warning
        sys2 = make_sysfs(join(self.tmp, "sys2"), kraken_devices(["1000", "1001", "1002", "1003", "1004"]))
        self.write_registry(2, {"delay_sync": pid}, serials=["1000"], cwd="/srv/other")
        errors, warnings = ini_checker.check_attached_serials(self.params(keys=False), sys2, self.run_dir)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, ["serial 1000 is used by instance 2 running from /srv/other"])
        # The no_hw run ignores the registry entirely
        res = self.cli(self.ini(self.params()))
        self.assertEqual((res.returncode, res.stderr), (0, ""))

    def test_registry_liveness_rules(self):
        pid = self.start_stub("delay_sync")
        self.write_registry(1, {"delay_sync": pid})
        self.write_registry(2, {"rtl_daq": pid})            # pid alive but wrong role
        self.write_registry(3, {"delay_sync": 2 ** 22 + 12345})  # dead pid
        with open(join(self.run_dir, "inst4.json"), "w") as f:
            f.write("{not json")
        live = ini_checker.read_live_registry(self.run_dir)
        self.assertEqual([e["instance_id"] for e in live], [1])
        self.assertEqual(ini_checker.read_live_registry(self.run_dir, exclude_instance=1), [])
        # The own instance's entry never conflicts
        make_sysfs(self.sysfs, kraken_devices(["1100", "1101", "1102", "1103", "1104"]))
        params = self.params()
        params["federation"]["instance_id"] = "1"
        self.assertEqual(ini_checker.check_attached_serials(params, self.sysfs, self.run_dir), ([], []))

    def test_live_registry_ports_and_db_dir(self):
        pid = self.start_stub("hw_controller")
        db_dir = join(self.tmp, "_db")
        self.write_registry(1, {"hw_controller": pid}, ports={"iq": 5000, "ctl": 5001, "status": None},
                            db_dir=db_dir)
        params = self.params(keys=False)
        params["database"]["en_db"] = "1"
        errors, warnings = ini_checker.check_live_registry(params, self.run_dir, config_dir=self.tmp)
        self.assertEqual(errors, [])
        self.assertIn("port 5000 of instance 0 is also used by instance 1 running from "
                      "/opt/heimdall/instances/1/Firmware", warnings)
        self.assertTrue(any("[database] db_dir {} is also used by instance 1".format(realpath(db_dir)) in w
                            for w in warnings), warnings)
        params["database"]["instance_subdir"] = "1"
        params["host"] = {"instance_registry": "1"}
        errors, warnings = ini_checker.check_live_registry(params, self.run_dir, config_dir=self.tmp)
        self.assertEqual(len(errors), 2, errors)  # iq + ctl ports, new-key mode
        self.assertFalse(any("db_dir" in w for w in warnings))
        # CLI: only with 'attached'
        ini = self.ini(params)
        self.assertEqual(self.cli(ini).returncode, 0)
        res = self.cli(ini, "attached")
        self.assertEqual(res.returncode, 1)
        self.assertIn("port 5001 of instance 0 is also used by instance 1", res.stderr)


# ---------------------------------------------------------------------------
# Hardware mode: rtl_eeprom enumeration (PATH-stubbed lsusb / rtl_eeprom)
# ---------------------------------------------------------------------------

class TesterHardwareMode(unittest.TestCase):

    def setUp(self):
        if os.name != "posix":
            self.skipTest("SKIP reason: shell stubs need a POSIX shell")
        self.tmp = tempfile.mkdtemp(prefix="ini_hw_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = join(self.tmp, "eeprom.log")

    def stubs(self, n_receivers, serials, busy=(), other_realtek=0):
        """lsusb lists n_receivers RTL2838 + other_realtek Realtek NICs; the
        rtl_eeprom stub reproduces the real tool's stderr (rtl_eeprom.c):
        librtlsdr sees len(serials) RTL2832U devices, index i prints
        serials[i], a busy index fails the USB claim, an index past the count
        prints an empty device name and 'Failed to open'."""
        lsusb = "#!/bin/sh\n" + "".join(
            "echo 'Bus 001 Device 0{0:02d}: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T'\n".format(i)
            for i in range(n_receivers)) + "".join(
            "echo 'Bus 002 Device 0{0:02d}: ID 0bda:8153 Realtek Semiconductor Corp. RTL8153 Gigabit "
            "Ethernet Adapter'\n".format(i) for i in range(other_realtek))
        n_rtl = len(serials)
        listing = "".join("  echo '  {0}:  Generic, RTL2832U, SN: {1}' >&2\n".format(i, s)
                          for i, s in enumerate(serials))
        cases = "".join(
            "  {0}) echo 'Using device {0}: Generic RTL2832U OEM' >&2\n"
            "     echo 'usb_claim_interface error -6' >&2\n"
            "     echo 'Failed to open rtlsdr device #{0}.' >&2; exit 1 ;;\n".format(i)
            if i in busy else
            "  {0}) echo 'Using device {0}: Generic RTL2832U OEM' >&2\n"
            "     echo 'Current configuration:' >&2\n"
            "     echo 'Serial number:\t\t{1}' >&2 ;;\n".format(i, s)
            for i, s in enumerate(serials))
        if n_rtl:
            body = ("echo 'Found {0} device(s):' >&2\n{1}echo '' >&2\ncase \"$2\" in\n{2}"
                    "  *) echo \"Using device $2: \" >&2\n"
                    "     echo \"Failed to open rtlsdr device #$2.\" >&2; exit 1 ;;\n"
                    "esac\n").format(n_rtl, listing, cases)
        else:
            body = "echo 'No supported devices found.' >&2\nexit 1\n"
        eeprom = "#!/bin/sh\necho \"$2\" >> '{0}'\n{1}exit 0\n".format(self.log, body)
        for name, body in (("lsusb", lsusb), ("rtl_eeprom", eeprom)):
            path = join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)

    def indices(self):
        with open(self.log) as f:
            return [int(x) for x in f.read().split()]

    def run_hw(self, params):
        ini = join(self.tmp, "cfg.ini")
        write_ini(ini, params)
        return run_checker([ini], cwd=self.tmp, path_prefix=self.bin,
                           env_extra={"HEIMDALL_SYSFS_ROOT": join(self.tmp, "nosys"),
                                      "HEIMDALL_RUN_DIR": join(self.tmp, "norun")})

    def test_range5_kept_without_serial_keys(self):
        self.stubs(5, ["1000", "1001", "1002", "1003", "1004"])
        res = self.run_hw(live_params())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.indices(), [0, 1, 2, 3, 4])

    def test_serial_keys_enumerate_every_receiver(self):
        self.stubs(7, ["1000", "1001", "1002", "1003", "1004", "1100", "1101"])
        params = live_params()
        params["hw"]["serial_base"] = "1100"
        params["daq"]["ctr_channel_serial_no"] = "1100"
        res = self.run_hw(params)
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.indices(), [0, 1, 2, 3, 4, 5, 6])  # one probe per device, shared
        self.assertIn("Only 2 of the 5 configured serials are attached; missing: 1102, 1103, 1104", res.stderr)

    def test_extra_realtek_device_is_not_busy(self):
        """lsusb counts every Realtek device; an index past rtl_eeprom's 'Found N
        device(s)' count prints 'Failed to open' but is absent, not busy."""
        self.stubs(5, ["1100", "1101", "1102", "1103", "1104"], other_realtek=1)
        params = live_params()
        params["hw"]["serial_base"] = "1000"
        params["daq"]["ctr_channel_serial_no"] = "1000"
        res = self.run_hw(params)
        self.assertEqual(warning_lines(res.stderr), [], res.stderr)
        self.assertNotIn("busy", res.stderr)
        self.assertEqual(self.indices(), [0, 1, 2, 3, 4, 5])
        # the serials really are missing; only the phantom 'busy' index is gone
        self.assertEqual(res.returncode, 1)
        self.assertIn("missing: 1000, 1001, 1002, 1003, 1004", res.stderr)

    def test_extra_realtek_device_all_serials_found(self):
        self.stubs(5, ["1100", "1101", "1102", "1103", "1104"], other_realtek=2)
        params = live_params()
        params["hw"]["serial_base"] = "1100"
        params["daq"]["ctr_channel_serial_no"] = "1100"
        res = self.run_hw(params)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(warning_lines(res.stderr), [], res.stderr)
        self.assertEqual(self.indices(), [0, 1, 2, 3, 4, 5, 6])

    def test_only_non_sdr_realtek_device_is_not_busy(self):
        """No RTL2832U at all: rtl_eeprom says 'No supported devices found.'."""
        self.stubs(0, [], other_realtek=1)
        params = live_params()
        params["hw"]["serial_base"] = "1100"
        params["daq"]["ctr_channel_serial_no"] = "1100"
        res = self.run_hw(params)
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("busy", res.stderr)
        self.assertEqual(warning_lines(res.stderr), [], res.stderr)
        self.assertEqual(self.indices(), [0])

    def test_serial_keys_all_found_and_busy_device(self):
        self.stubs(6, ["1100", "1101", "1102", "1103", "1104", "1000"], busy=(5,))
        params = live_params()
        params["hw"]["serial_base"] = "1100"
        params["daq"]["ctr_channel_serial_no"] = "1100"
        res = self.run_hw(params)
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = warning_lines(res.stderr)
        self.assertEqual(len(lines), 1, res.stderr)
        self.assertIn("device index(es) 5 busy (claimed by another process", lines[0])


# ---------------------------------------------------------------------------
# federation.ini (--federation)
# ---------------------------------------------------------------------------

class TesterFederationIni(unittest.TestCase):

    def fed(self, **kw):
        return {"federation": dict(kw)}

    def test_minimal_and_full_valid(self):
        self.assertEqual(ini_checker.check_federation_ini(self.fed(auth_token="tok")), ([], []))
        errors, warnings = ini_checker.check_federation_ini(self.fed(
            coordinator_port="6000", router_port="7000", router_stats_port="0",
            coordinator_candidates="hosta.invalid:6000,hostb.invalid:6000,hostc.invalid:6000",
            coordinator_self="hosta.invalid:6000", election_interval_sec="2.0",
            election_missed_polls="3", election_settle_polls="5", election_clock_margin="0.05",
            election_failback_sec="60", require_quorum="1", reconcile_assignments="auto",
            fanout_workers="32", max_handlers="64", reserved_handlers="8", xack_timeout_sec="3",
            hwc_request_timeout_sec="auto", max_member_rto_sec="30", schc_window="16",
            router_queue_frames="4", router_drop_policy="oldest", router_tag_mode="node",
            router_sources="hosta.invalid:0,router://hostd.invalid:7000",
            cluster_members="hosta.invalid:0,hostb.invalid:0@b0", cluster_id="lab-a",
            router_stats_endpoint="hosta.invalid:7001", state_dir="/var/lib/heimdall/coordinator",
            auth_token="tok", allow_cidrs="10.0.0.0/8"))
        self.assertEqual((errors, warnings), ([], []))

    def test_grammar_errors(self):
        cases = [(dict(coordinator_port="70000"), "[federation] coordinator_port"),
                 (dict(router_port="6000"), "coordinator_port and router_port are both 6000"),
                 (dict(election_interval_sec="0.01"), "election_interval_sec"),
                 (dict(election_settle_polls="4"), "election_settle_polls must be an integer >= 5"),
                 (dict(election_missed_polls="4", election_settle_polls="5"), ">= 6"),
                 # settle absent: the coordinator's default 5 is checked against the bound
                 (dict(election_missed_polls="4"),
                  "the default election_settle_polls = 5 is below the safe minimum 6 (ceil("),
                 (dict(election_missed_polls="4"), "; set election_settle_polls to at least 6"),
                 (dict(election_clock_margin="0.4"), "default election_settle_polls = 5 is below the safe minimum 6"),
                 (dict(coordinator_candidates="a.invalid:6000", coordinator_self="b.invalid:6000"),
                  "coordinator_self 'b.invalid:6000' is not one of coordinator_candidates"),
                 (dict(router_sources="localhost:0", router_tag_mode="node"), "uses a loopback host"),
                 (dict(router_sources="hosta:0,hosta:0"), "duplicate router source"),
                 (dict(cluster_members="127.0.0.1:0"), "uses a loopback host"),
                 (dict(reconcile_assignments="yes"), "reconcile_assignments"),
                 (dict(member_stall_after_sec="soon"), "'auto' or a number"),
                 (dict(router_stats_endpoint="nope"), "router_stats_endpoint 'nope' is not host:port"),
                 (dict(allow_cidrs="300.0.0.0/8"), "allow_cidrs entry"),
                 (dict(port_stride="0"), "port_stride must be a positive integer")]
        for kw, needle in cases:
            kw.setdefault("auth_token", "tok")
            errors, _ = ini_checker.check_federation_ini(self.fed(**kw))
            self.assertTrue(any(needle in e for e in errors), "{}: {} not in {}".format(kw, needle, errors))
        self.assertEqual(ini_checker.check_federation_ini({"hw": {}})[0],
                         ["federation.ini has no [federation] section"])

    def test_default_settle_within_bound(self):
        for kw in (dict(election_missed_polls="3"), dict(election_clock_margin="0.3"),
                   dict(election_missed_polls="4", election_settle_polls="6")):
            errors, _ = ini_checker.check_federation_ini(self.fed(auth_token="tok", **kw))
            self.assertEqual(errors, [], kw)

    def test_federation_common_missing(self):
        """Without _daq_core/federation_common.py: one warning for the instance INI, an error for --federation."""
        params = live_params()
        params["federation"]["cluster_members"] = "hosta.invalid:0,hostb.invalid:0"
        with mock.patch.object(ini_checker, "_fc", None):
            self.assertEqual(ini_checker.check_ini(params, False), [])
            warnings = ini_checker.check_ini_warnings(params, False)
            self.assertEqual([w for w in warnings if "not importable" in w],
                             ["_daq_core/federation_common.py is not importable; [federation] membership "
                              "and identity checks skipped"])
            errors, _ = ini_checker.check_federation_ini(self.fed(auth_token="tok",
                                                                  cluster_members="hosta.invalid:0"))
            self.assertIn("_daq_core/federation_common.py is not importable; federation.ini cannot be "
                          "validated", errors)
            # No membership key: nothing to skip, no warning
            self.assertEqual(ini_checker.check_ini_warnings(live_params(), False), [])

    def test_shipped_federation_ini_presets(self):
        """J.1: every shipped config_files/*/federation.ini passes --federation without an error."""
        presets = sorted(join(PRESET_DIR, d, "federation.ini") for d in os.listdir(PRESET_DIR)
                         if os.path.isfile(join(PRESET_DIR, d, "federation.ini")))
        if not presets:
            self.skipTest("SKIP reason: no config_files/*/federation.ini shipped yet")
        tmp = tempfile.mkdtemp(prefix="ini_fed_presets_")
        self.addCleanup(shutil.rmtree, tmp, True)
        for preset in presets:
            res = run_checker([preset, "--federation"], cwd=tmp)
            self.assertEqual(res.returncode, 0, "{}: {}".format(preset, res.stderr))
            self.assertNotIn("ERROR", res.stderr, preset)

    @unittest.skipUnless(os.path.isfile(join(PRESET_DIR, "cluster_site", "federation.ini")),
                         "SKIP reason: config_files/cluster_site/federation.ini not present yet")
    def test_cluster_site_federation_ini(self):
        """J.1: the cluster_site coordinator config validates cleanly."""
        tmp = tempfile.mkdtemp(prefix="ini_fed_site_")
        self.addCleanup(shutil.rmtree, tmp, True)
        res = run_checker([join(PRESET_DIR, "cluster_site", "federation.ini"), "--federation"], cwd=tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("ERROR", res.stderr)

    def test_warnings(self):
        _, w = ini_checker.check_federation_ini(self.fed(
            auth_token="tok", require_quorum="1", coordinator_candidates="a.invalid:6000,b.invalid:6000"))
        self.assertIn("with 2 candidates, losing either host blocks all mutations", w[0])
        _, w = ini_checker.check_federation_ini(self.fed())
        self.assertEqual(w, ["auth_token and allow_cidrs are empty and the listen address 0.0.0.0 is not "
                             "loopback: the coordinator accepts mutating commands from any host"])
        _, w = ini_checker.check_federation_ini({"federation": {}, "daq": {"listen_address": "127.0.0.1"}})
        self.assertEqual(w, [])

    def test_local_member_ports(self):
        errors, _ = ini_checker.check_federation_ini(self.fed(
            auth_token="tok", cluster_members="hosta.invalid:10,hostb.invalid:0",
            coordinator_candidates="hosta.invalid:6000", coordinator_self="hosta.invalid:6000"))
        self.assertIn("port 6000 of instance 10 collides with the coordinator port; with stride 100 use "
                      "instance ids 0-9", errors)
        errors, _ = ini_checker.check_federation_ini(self.fed(
            auth_token="tok", cluster_members="hosta.invalid:1,hosta.invalid:2", port_stride="2",
            coordinator_self="hosta.invalid:6000", coordinator_candidates="hosta.invalid:6000"))
        self.assertTrue(any("port_stride = 2 is below 4" in e for e in errors), errors)
        self.assertTrue(any("is also used by member hosta.invalid:1" in e for e in errors), errors)
        # Members of other hosts are not checked here
        errors, _ = ini_checker.check_federation_ini(self.fed(auth_token="tok",
                                                              cluster_members="hostz.invalid:10"))
        self.assertEqual(errors, [])

    def test_cli(self):
        tmp = tempfile.mkdtemp(prefix="ini_fed_")
        self.addCleanup(shutil.rmtree, tmp, True)
        good = join(tmp, "federation.ini")
        with open(good, "w") as f:
            f.write("[federation]\ncoordinator_port = 6000\ncluster_members = hosta.invalid:0\n")
        res = run_checker([good, "--federation"], cwd=tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(warning_lines(res.stderr)), 1)  # no auth_token
        bad = join(tmp, "bad.ini")
        with open(bad, "w") as f:
            f.write("[federation]\nauth_token = t\nelection_missed_polls = 50\n")
        res = run_checker(["--federation", bad], cwd=tmp)
        self.assertEqual(res.returncode, 1)
        self.assertIn("election_missed_polls", res.stderr)
        # Reviewer case f6.ini: missed_polls raised, settle left at the coordinator default 5
        f6 = join(tmp, "f6.ini")
        with open(f6, "w") as f:
            f.write("[federation]\nauth_token = t\ncoordinator_candidates = a.invalid:6000,b.invalid:6000,"
                    "c.invalid:6000\nelection_missed_polls = 4\n")
        res = run_checker([f6, "--federation"], cwd=tmp)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("default election_settle_polls = 5 is below the safe minimum 6", res.stderr)
        # Without --federation such a file is not a valid instance INI (missing [hw] ...)
        self.assertEqual(run_checker([good, "no_hw"], cwd=tmp).returncode, 1)


# ---------------------------------------------------------------------------
# hw_discover --sdr
# ---------------------------------------------------------------------------

class TesterHwDiscoverSdr(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hw_discover_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sysfs = make_sysfs(join(self.tmp, "sys"), kraken_devices(["1000", "1001"]) +
                                [("2-1", "0BDA", "2832", None), ("2-2", "046d", "c52b", "1")])

    def test_discover_rtlsdr(self):
        res = hw_discover.discover_rtlsdr(self.sysfs)
        self.assertEqual(res["count"], 3)
        self.assertTrue(res["sysfs"])
        self.assertEqual([d["serial"] for d in res["devices"]], ["1000", "1001", None])
        self.assertEqual(set(res["devices"][0]), {"busnum", "devnum", "path", "serial", "product_id"})
        # Same view as ini_checker's copy of the logic
        self.assertEqual(res["devices"], ini_checker.enumerate_sdr_sysfs(self.sysfs))
        self.assertEqual(hw_discover.discover_rtlsdr(join(self.tmp, "none")),
                         {"count": 0, "devices": [], "sysfs": False})

    def test_cli_sdr_flag(self):
        env = dict(os.environ, HEIMDALL_SYSFS_ROOT=self.sysfs)
        res = subprocess.run([sys.executable, HW_DISCOVER, "--sdr"], capture_output=True, text=True,
                             timeout=120, env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(res.stdout)
        self.assertEqual(list(doc), ["rtlsdr"])
        self.assertEqual(doc["rtlsdr"]["count"], 3)
        self.assertEqual(doc["rtlsdr"]["devices"][1]["path"], join(self.sysfs, "1-1.2"))

    def test_default_output_has_no_sdr_block(self):
        """Without --sdr (what the start scripts run) hw_caps.json keeps its keys."""
        res = subprocess.run([sys.executable, HW_DISCOVER], capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(res.stdout)
        self.assertEqual(sorted(doc), sorted(["hat", "gpu", "pcie", "usb3", "cpu", "dma", "rotator",
                                              "recommended"]))


# ---------------------------------------------------------------------------
# auto_config atomic rewrite
# ---------------------------------------------------------------------------

class TesterAutoConfigAtomic(unittest.TestCase):

    INI = textwrap.dedent("""\
        [pre_processing]
        ; operator comment kept verbatim
        fft_engine = auto
        cpi_size = 1048576

        [calibration]
        correlation_engine = cpu_numpy
        """)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="auto_config_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.caps = join(self.tmp, "hw_caps.json")
        with open(self.caps, "w") as f:
            json.dump({"recommended": {"fft_engine": "cpu_scipy", "correlation_engine": "gpu",
                                       "transport": "shm", "workers": 4}}, f)
        self.cfg = join(self.tmp, "daq_chain_config.ini")
        with open(self.cfg, "w") as f:
            f.write(self.INI)
        os.chmod(self.cfg, 0o640)

    def test_targeted_rewrite_is_atomic_and_keeps_mode(self):
        inode = os.stat(self.cfg).st_ino
        changes = auto_config.auto_configure(self.caps, self.cfg)
        self.assertEqual(changes, {("pre_processing", "fft_engine"): "cpu_scipy"})
        with open(self.cfg) as f:
            self.assertEqual(f.read(), self.INI.replace("fft_engine = auto", "fft_engine = cpu_scipy"))
        st = os.stat(self.cfg)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)
        self.assertNotEqual(st.st_ino, inode, "the file must be replaced, not rewritten in place")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["daq_chain_config.ini", "hw_caps.json"])

    def test_no_auto_values_leaves_file_untouched(self):
        with open(self.cfg, "w") as f:
            f.write(self.INI.replace("auto", "cpu_scipy"))
        before = os.stat(self.cfg)
        self.assertEqual(auto_config.auto_configure(self.caps, self.cfg), {})
        after = os.stat(self.cfg)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_symlink_target_replaced_link_kept(self):
        real_dir = join(self.tmp, "real")
        os.makedirs(real_dir)
        real = join(real_dir, "daq_chain_config.ini")
        shutil.move(self.cfg, real)
        os.symlink(real, self.cfg)
        auto_config.auto_configure(self.caps, self.cfg)
        self.assertTrue(os.path.islink(self.cfg))
        with open(real) as f:
            self.assertIn("fft_engine = cpu_scipy", f.read())
        self.assertEqual(os.listdir(real_dir), ["daq_chain_config.ini"])

    def test_write_failure_keeps_original_and_cleans_up(self):
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                auto_config.write_file_atomic(self.cfg, "garbage")
        with open(self.cfg) as f:
            self.assertEqual(f.read(), self.INI)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["daq_chain_config.ini", "hw_caps.json"])

    def test_unwritable_directory_falls_back_to_in_place_rewrite(self):
        """mkstemp refused (directory not writable, INI writable): in-place rewrite plus a warning."""
        inode = os.stat(self.cfg).st_ino
        with mock.patch.object(auto_config.tempfile, "mkstemp",
                               side_effect=PermissionError(13, "Permission denied")), \
                self.assertLogs(auto_config.logger, level="WARNING") as logs:
            changes = auto_config.auto_configure(self.caps, self.cfg)
        self.assertEqual(changes, {("pre_processing", "fft_engine"): "cpu_scipy"})
        with open(self.cfg) as f:
            self.assertEqual(f.read(), self.INI.replace("fft_engine = auto", "fft_engine = cpu_scipy"))
        self.assertEqual(os.stat(self.cfg).st_ino, inode)
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o640)
        self.assertTrue(any("Cannot create a temporary file in {}".format(realpath(self.tmp)) in m
                            and "not atomic" in m for m in logs.output), logs.output)

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0,
                     "SKIP reason: root ignores directory write permission")
    def test_read_only_directory_real(self):
        ro = join(self.tmp, "ro")
        os.makedirs(ro)
        cfg = join(ro, "daq_chain_config.ini")
        shutil.copy(self.cfg, cfg)
        os.chmod(cfg, 0o644)
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        with self.assertLogs(auto_config.logger, level="WARNING"):
            auto_config.auto_configure(self.caps, cfg)
        with open(cfg) as f:
            self.assertIn("fft_engine = cpu_scipy", f.read())
        self.assertEqual(os.listdir(ro), ["daq_chain_config.ini"])

    def test_full_rewrite_fallback_is_atomic(self):
        inode = os.stat(self.cfg).st_ino
        with mock.patch.object(auto_config, "_apply_targeted_updates", return_value=0):
            auto_config.auto_configure(self.caps, self.cfg)
        parser = configparser.ConfigParser()
        parser.read(self.cfg)
        self.assertEqual(parser["pre_processing"]["fft_engine"], "cpu_scipy")
        self.assertNotEqual(os.stat(self.cfg).st_ino, inode)
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
