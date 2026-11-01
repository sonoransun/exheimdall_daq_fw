"""
    Description :
    Tests for the provisioning tools (design spec B.1, H.1, H.7, H.9):

        util/provision_instance.py   instance directories (symlink working
                                     copies), the line-based INI rewriter, the
                                     H.1 key table, --refresh/--from-config/
                                     --in-place/--systemd/--profile, exit codes
        util/cluster_cfg_gen.py      cluster INIs, federation.ini, ports.md,
                                     provision.sh and their validation
        util/cfg_gen.py              ini_version 8, no [squelch], serial 1000
        config_files/performance/*.ini, multi_instance_x86, cluster_site presets

    Hardware-free and CI-safe (Linux and macOS): every install root is a temp
    copy of a minimal tree (the real ini_checker.py, federation_common.py and
    daq_chain_config.ini plus stub scripts); validation always runs ini_checker
    in no_hw mode, which reads nothing but the INI. Registry liveness uses a
    temp HEIMDALL_RUN_DIR and a sleeping child process.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import configparser
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from os.path import dirname, join, realpath

current_path = dirname(realpath(__file__))
FIRMWARE = dirname(dirname(current_path))
REPO = dirname(FIRMWARE)
UTIL = join(REPO, "util")
PROVISION = join(UTIL, "provision_instance.py")
CLUSTER_GEN = join(UTIL, "cluster_cfg_gen.py")
CFG_GEN = join(UTIL, "cfg_gen.py")
LIVE_INI = join(FIRMWARE, "daq_chain_config.ini")
PRESETS = join(REPO, "config_files")

sys.path.insert(0, UTIL)
sys.path.insert(0, FIRMWARE)
import provision_instance as pi  # noqa: E402
import ini_checker  # noqa: E402

_HOOKS = ("HEIMDALL_SYSFS_ROOT", "HEIMDALL_RUN_DIR", "HEIMDALL_PROC_ROOT")

#: Keys provision_instance.py may change in --template mode with no key flag (H.1 table)
DEFAULT_TABLE_KEYS = {
    ("federation", "instance_id"), ("federation", "port_stride"), ("federation", "node_id"),
    ("federation", "cluster_id"), ("hw", "serial_base"), ("hw", "unit_id"),
    ("daq", "ctl_max_connections"), ("daq", "ctr_channel_serial_no"),
    ("host", "instance_registry"), ("host", "instance_guard"), ("host", "cpu_cores"),
    ("host", "rt_priority_offset"), ("host", "allow_core_sharing"), ("host", "host_tuning"),
    ("host", "preflight"), ("host", "health_thread_sched_other"),
    ("monitoring", "en_monitoring"), ("monitoring", "en_status_server"),
    ("monitoring", "status_extended"), ("monitoring", "status_update_interval"),
    ("monitoring", "event_tag_identity"),
}


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if k not in _HOOKS}
    if extra:
        env.update(extra)
    return env


def params(path):
    p = configparser.ConfigParser(interpolation=None)
    with open(path) as f:
        p.read_file(f)
    return {s: dict(p.items(s, raw=True)) for s in p.sections()}


def read(path):
    with open(path) as f:
        return f.read()


def make_root(base, name="root"):
    """A minimal install root: what provision_instance.py and ini_checker need."""
    root = join(base, name)
    fw = join(root, "Firmware")
    os.makedirs(join(fw, "_daq_core"))
    shutil.copy2(join(FIRMWARE, "ini_checker.py"), fw)
    shutil.copy2(LIVE_INI, fw)
    shutil.copy2(join(FIRMWARE, "_daq_core", "federation_common.py"), join(fw, "_daq_core"))
    for script in ("daq_start_sm.sh", "daq_stop.sh", "daq_synthetic_start.sh"):
        with open(join(fw, script), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(join(fw, script), 0o755)
    with open(join(fw, "fir_filter_designer.py"), "w") as f:
        f.write("# stub\n")
    for d in ("_testing", "_calibration", "_fpga_gateware", "_data_control", "_logs", "_db"):
        os.makedirs(join(fw, d))
    with open(join(fw, "_fpga_gateware", "heimdall_fpga.bin"), "wb") as f:
        f.write(b"\0")
    os.makedirs(join(root, "util"))
    shutil.copy2(PROVISION, join(root, "util"))
    os.makedirs(join(root, "config_files", "performance"))
    for name in ("minimal", "balanced", "maximum"):
        shutil.copy2(join(PRESETS, "performance", name + ".ini"), join(root, "config_files", "performance"))
    return root


def run_provision(args, env_extra=None, script=PROVISION, cwd=None):
    res = subprocess.run([sys.executable, script] + list(args), capture_output=True, text=True,
                         timeout=120, env=clean_env(env_extra), cwd=cwd)
    return res.returncode, res.stdout, res.stderr


def changed_keys(before, after):
    keys = set()
    for section in set(before) | set(after):
        b, a = before.get(section, {}), after.get(section, {})
        for key in set(b) | set(a):
            if b.get(key) != a.get(key):
                keys.add((section, key))
    return keys


def comment_lines(text):
    return [l for l in text.splitlines() if l.strip().startswith((";", "#"))]


class LiveChild:
    """A sleeping child whose command line contains *role* (registry liveness)."""

    def __init__(self, tmp, role):
        stub = join(tmp, role + ".py")
        with open(stub, "w") as f:
            f.write("import time\ntime.sleep(120)\n")
        self.proc = subprocess.Popen([sys.executable, stub])
        deadline = time.time() + 10
        while time.time() < deadline:
            ps = subprocess.run(["ps", "-o", "args=", "-p", str(self.proc.pid)], capture_output=True, text=True)
            if role in ps.stdout:
                break
            time.sleep(0.05)

    def stop(self):
        self.proc.kill()
        self.proc.wait()


# ---------------------------------------------------------------------------
# The INI rewriter
# ---------------------------------------------------------------------------

class TesterIniDocument(unittest.TestCase):

    TEXT = ("; header comment\n[meta]\nini_version = 8\n\n[hw]\nname = k5\nunit_id = 0\n"
            "; trailing comment of hw\n;serial_base = 1000\n\n[daq]\nKey_Mixed=old\nempty =\n"
            "multi = first\n  second line\nafter = 1\n")

    def test_rewrites_in_place_and_keeps_everything_else(self):
        doc = pi.IniDocument(self.TEXT)
        doc.set("hw", "unit_id", "auto")
        self.assertEqual(doc.text(), self.TEXT.replace("unit_id = 0", "unit_id = auto"))

    def test_adds_missing_key_after_the_last_key_line_of_its_section(self):
        doc = pi.IniDocument(self.TEXT)
        doc.set("hw", "serial_base", "1100")
        self.assertIn("unit_id = 0\nserial_base = 1100\n; trailing comment of hw\n;serial_base = 1000\n", doc.text())
        self.assertEqual(doc.get("hw", "serial_base"), "1100")

    def test_adds_missing_section_at_the_end(self):
        doc = pi.IniDocument(self.TEXT)
        doc.set("host", "cpu_cores", "4,5,6,7")
        self.assertTrue(doc.text().endswith("after = 1\n\n[host]\ncpu_cores = 4,5,6,7\n"))
        self.assertEqual(params_of(doc.text())["host"]["cpu_cores"], "4,5,6,7")

    def test_case_insensitive_keys_delimiters_empty_values_and_continuations(self):
        doc = pi.IniDocument(self.TEXT)
        doc.set("daq", "key_mixed", "new")
        doc.set("daq", "empty", "v")
        doc.set("daq", "multi", "one")
        doc.set("hw", "name", "")
        text = doc.text()
        self.assertIn("Key_Mixed=new\n", text)
        self.assertIn("empty = v\n", text)
        self.assertIn("multi = one\nafter = 1\n", text)
        self.assertNotIn("second line", text)
        self.assertIn("name =\n", text)
        self.assertEqual(params_of(text)["daq"], {"key_mixed": "new", "empty": "v", "multi": "one", "after": "1"})

    def test_commented_section_header_is_not_a_section(self):
        doc = pi.IniDocument("[a]\nx = 1\n; [host]\n;cpu_cores =\n")
        self.assertEqual(doc.sections(), ["a"])
        doc.set("host", "cpu_cores", "0,1,2,3")
        self.assertTrue(doc.text().endswith(";cpu_cores =\n\n[host]\ncpu_cores = 0,1,2,3\n"))

    def test_rejects_multiline_values(self):
        with self.assertRaises(ValueError):
            pi.IniDocument(self.TEXT).set("hw", "name", "a\nb")


def params_of(text):
    return pi.params_of_text(text)


# ---------------------------------------------------------------------------
# provision_instance.py
# ---------------------------------------------------------------------------

class TesterProvisionInstance(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="provision_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = make_root(self.tmp)
        self.run_dir = join(self.tmp, "run")
        os.makedirs(self.run_dir)
        self.env = {"HEIMDALL_RUN_DIR": self.run_dir}
        self.dest = join(self.root, "instances", "1")

    def provision(self, *args):
        return run_provision(["--root", self.root] + list(args), self.env)

    def test_write_creates_the_layout(self):
        rc, out, err = self.provision("--instance", "1", "--write")
        self.assertEqual(rc, 0, out + err)
        fw = join(self.dest, "Firmware")
        for name, target in (("util", join(self.root, "util")),
                             ("config_files", join(self.root, "config_files"))):
            self.assertTrue(os.path.islink(join(self.dest, name)))
            self.assertEqual(os.readlink(join(self.dest, name)), target)
        for name in ("_daq_core", "_testing", "_calibration", "_fpga_gateware", "ini_checker.py",
                     "fir_filter_designer.py", "daq_start_sm.sh", "daq_synthetic_start.sh", "daq_stop.sh"):
            path = join(fw, name)
            self.assertTrue(os.path.islink(path), name)
            self.assertEqual(os.readlink(path), join(self.root, "Firmware", name))
            self.assertTrue(os.path.exists(path), name + " resolves")
        self.assertTrue(os.path.isfile(join(fw, "_fpga_gateware", "heimdall_fpga.bin")))
        for name in ("_data_control", "_logs", "_db"):
            self.assertTrue(os.path.isdir(join(fw, name)) and not os.path.islink(join(fw, name)), name)
        self.assertTrue(os.path.isfile(join(fw, pi.INI_NAME)) and not os.path.islink(join(fw, pi.INI_NAME)))
        self.assertFalse(os.path.lexists(join(fw, "__pycache__")))
        self.assertIn("ini_checker (no_hw): ok", out)
        # the real checker agrees, in the instance dir, without any hardware
        res = subprocess.run([sys.executable, "ini_checker.py", pi.INI_NAME, "no_hw"], cwd=fw,
                             capture_output=True, text=True, timeout=60, env=clean_env())
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_ini_diff_is_exactly_the_default_table(self):
        rc, out, err = self.provision("--instance", "1", "--write")
        self.assertEqual(rc, 0, out + err)
        template = read(join(self.root, "Firmware", pi.INI_NAME))
        written = read(join(self.dest, "Firmware", pi.INI_NAME))
        before, after = params_of(template), params_of(written)
        self.assertLessEqual(changed_keys(before, after), DEFAULT_TABLE_KEYS)
        expected = {("hw", "serial_base"): "1100", ("host", "cpu_cores"): "4,5,6,7",
                    ("host", "instance_registry"): "1", ("host", "instance_guard"): "refuse",
                    ("monitoring", "en_monitoring"): "1", ("monitoring", "en_status_server"): "1",
                    ("daq", "ctl_max_connections"): "4", ("federation", "instance_id"): "1",
                    ("federation", "port_stride"): "100", ("federation", "cluster_id"): "default",
                    ("hw", "unit_id"): "auto", ("daq", "ctr_channel_serial_no"): "1100",
                    ("host", "rt_priority_offset"): "0", ("host", "allow_core_sharing"): "refuse",
                    ("host", "host_tuning"): "auto", ("host", "preflight"): "warn",
                    ("host", "health_thread_sched_other"): "1", ("monitoring", "status_extended"): "1",
                    ("monitoring", "status_update_interval"): "10", ("monitoring", "event_tag_identity"): "1"}
        for (section, key), value in expected.items():
            self.assertEqual(after[section][key], value, (section, key))
        self.assertTrue(after["federation"]["node_id"].endswith(":1"))
        self.assertNotIn("cluster_members", after["federation"])
        self.assertNotIn("antenna_switch_gpio", after["hw"])
        self.assertNotIn("delay_sync_cores", after["host"])
        self.assertNotIn("database", {s for s, _ in changed_keys(before, after)})
        # comments preserved, in order
        self.assertEqual(comment_lines(template), comment_lines(written))
        # the dry run of the same plan printed that diff and the port table
        self.assertIn("+serial_base = 1100", out)
        self.assertIn("  iq       5100", out)
        self.assertIn("  zmq      1230", out)

    def test_dry_run_writes_nothing(self):
        rc, out, err = self.provision("--instance", "2")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(join(self.root, "instances")))
        self.assertIn("Dry run: nothing written", out)
        self.assertIn("+cpu_cores = 8,9,10,11", out)
        self.assertIn("+serial_base = 1200", out)
        self.assertIn("ini_checker (no_hw): ok", out)

    def test_destination_exists_and_force(self):
        self.assertEqual(self.provision("--instance", "1", "--write")[0], 0)
        ini = join(self.dest, "Firmware", pi.INI_NAME)
        with open(ini, "a") as f:
            f.write("; operator edit\n")
        rc, _, err = self.provision("--instance", "1", "--write")
        self.assertEqual(rc, 3, err)
        self.assertIn("--refresh", err)
        rc, _, err = self.provision("--instance", "1")  # the dry run predicts the refusal
        self.assertEqual(rc, 3, err)
        rc, out, err = self.provision("--instance", "1", "--write", "--force", "--rt-offset", "3")
        self.assertEqual(rc, 0, out + err)
        backups = [n for n in os.listdir(join(self.dest, "Firmware")) if n.startswith(pi.INI_NAME + ".bak.")]
        self.assertEqual(len(backups), 1)
        self.assertIn("; operator edit", read(join(self.dest, "Firmware", backups[0])))
        self.assertEqual(params(ini)["host"]["rt_priority_offset"], "3")

    def test_refresh_relinks_and_keeps_the_ini(self):
        self.assertEqual(self.provision("--instance", "1", "--write")[0], 0)
        ini = join(self.dest, "Firmware", pi.INI_NAME)
        with open(ini, "a") as f:
            f.write("; operator edit\n")
        before = read(ini)
        # an upgraded tree: a new module, a removed one, a new template key
        new_root = join(self.tmp, "root-v2")
        shutil.copytree(self.root, new_root, symlinks=True, ignore=shutil.ignore_patterns("instances"))
        os.makedirs(join(new_root, "Firmware", "_newmod"))
        os.unlink(join(new_root, "Firmware", "daq_synthetic_start.sh"))
        with open(join(new_root, "Firmware", pi.INI_NAME), "a") as f:
            f.write("\n[newsec]\nbrand_new_key = 7\n")
        # still present in the OLD root: the link into it must go anyway (no mixed-version tree)
        self.assertTrue(os.path.isfile(join(self.root, "Firmware", "daq_synthetic_start.sh")))
        rc, out, err = run_provision(["--root", new_root, "--dest", self.dest, "--instance", "1",
                                      "--refresh", "--write"], self.env)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(read(ini), before)
        self.assertEqual(os.readlink(join(self.dest, "Firmware", "_daq_core")),
                         join(new_root, "Firmware", "_daq_core"))
        self.assertTrue(os.path.islink(join(self.dest, "Firmware", "_newmod")))
        self.assertFalse(os.path.lexists(join(self.dest, "Firmware", "daq_synthetic_start.sh")))
        self.assertIn("Removed stale entry: " + join("Firmware", "daq_synthetic_start.sh"), out)
        # every link now points into the new root; private entries stayed real
        for directory in (self.dest, join(self.dest, "Firmware")):
            for name in os.listdir(directory):
                path = join(directory, name)
                if os.path.islink(path):
                    self.assertTrue(os.readlink(path).startswith(new_root + os.sep), path)
        for name in ("_data_control", "_logs", "_db"):
            self.assertTrue(os.path.isdir(join(self.dest, "Firmware", name)))
            self.assertFalse(os.path.islink(join(self.dest, "Firmware", name)))
        self.assertIn("[newsec] brand_new_key (template value: '7')", out)
        self.assertIn("ini_checker (no_hw): ok", out)
        # --refresh never touches the INI: key flags are refused
        rc, _, err = run_provision(["--root", new_root, "--dest", self.dest, "--instance", "1",
                                    "--refresh", "--serial-base", "5000"], self.env)
        self.assertEqual(rc, 2, err)
        self.assertIn("conflicts with --refresh", err)

    def test_refresh_refused_while_live(self):
        self.assertEqual(self.provision("--instance", "1", "--write")[0], 0)
        child = LiveChild(self.tmp, "delay_sync")
        self.addCleanup(child.stop)
        with open(join(self.run_dir, "inst1.env"), "w") as f:
            f.write("SCHEMA=1\nINSTANCE_ID=1\nPIDS=rtl_daq:999999,delay_sync:{0}\n".format(child.proc.pid))
        rc, _, err = self.provision("--instance", "1", "--refresh", "--write")
        self.assertEqual(rc, 4, err)
        self.assertIn("is live", err)
        rc, out, err = self.provision("--instance", "1", "--refresh")  # dry run: a warning
        self.assertEqual(rc, 0, out + err)
        self.assertIn("will be refused", err)
        rc, out, err = self.provision("--instance", "1", "--refresh", "--write", "--force")
        self.assertEqual(rc, 0, out + err)
        child.stop()
        rc, out, err = self.provision("--instance", "1", "--refresh", "--write")
        self.assertEqual(rc, 0, out + err)

    def test_from_config_is_verbatim(self):
        src = join(self.tmp, "planned.ini")
        text = read(LIVE_INI).replace("instance_id = 0", "instance_id = 3") + "; planned by hand\n"
        with open(src, "w") as f:
            f.write(text)
        rc, out, err = self.provision("--instance", "3", "--from-config", src, "--write")
        self.assertEqual(rc, 0, out + err)
        with open(join(self.root, "instances", "3", "Firmware", pi.INI_NAME), "rb") as f:
            self.assertEqual(f.read(), text.encode())

    def test_from_config_refusals(self):
        src = join(self.tmp, "planned.ini")
        with open(src, "w") as f:
            f.write(read(LIVE_INI).replace("instance_id = 0", "instance_id = 3"))
        rc, _, err = self.provision("--instance", "3", "--from-config", src, "--serial-base", "1300")
        self.assertEqual(rc, 2, err)
        self.assertIn("flag --serial-base conflicts with --from-config", err)
        rc, _, err = self.provision("--instance", "4", "--from-config", src, "--write")
        self.assertEqual(rc, 2, err)
        self.assertIn("instance_id in {0} is 3, expected 4".format(src), err)
        self.assertFalse(os.path.exists(join(self.root, "instances")))

    def test_instance_0_requires_in_place(self):
        rc, _, err = self.provision("--instance", "0", "--write")
        self.assertEqual(rc, 2, err)
        self.assertIn("--in-place", err)

    def test_instance_0_in_place(self):
        ini = join(self.root, "Firmware", pi.INI_NAME)
        original = read(ini)
        rc, out, err = self.provision("--instance", "0", "--in-place", "--write")
        self.assertEqual(rc, 0, out + err)
        backups = [n for n in os.listdir(join(self.root, "Firmware")) if n.startswith(pi.INI_NAME + ".bak.")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(read(join(self.root, "Firmware", backups[0])), original)
        p = params(ini)
        self.assertEqual(p["federation"]["instance_id"], "0")
        self.assertEqual(p["hw"]["serial_base"], "1000")
        self.assertEqual(p["daq"]["ctr_channel_serial_no"], "1000")
        self.assertEqual(p["host"]["cpu_cores"], "0,1,2,3")
        self.assertEqual(p["host"]["instance_registry"], "1")
        self.assertEqual(p["host"]["instance_guard"], "refuse")
        self.assertFalse(os.path.exists(join(self.root, "instances")))

    def test_copy_mode(self):
        rc, out, err = self.provision("--instance", "1", "--mode", "copy", "--write")
        self.assertEqual(rc, 0, out + err)
        core = join(self.dest, "Firmware", "_daq_core")
        self.assertTrue(os.path.isdir(core) and not os.path.islink(core))
        self.assertTrue(os.path.isfile(join(core, "federation_common.py")))
        self.assertFalse(os.path.islink(join(self.dest, "util")))

    def test_copy_mode_refresh_removes_entries_gone_from_the_root(self):
        self.assertEqual(self.provision("--instance", "1", "--mode", "copy", "--write")[0], 0)
        fw = join(self.dest, "Firmware")
        with open(join(fw, "_logs", "keep.log"), "w") as f:
            f.write("log\n")
        os.unlink(join(self.root, "Firmware", "daq_synthetic_start.sh"))
        shutil.rmtree(join(self.root, "Firmware", "_calibration"))
        rc, out, err = self.provision("--instance", "1", "--refresh", "--write")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.lexists(join(fw, "daq_synthetic_start.sh")))
        self.assertFalse(os.path.lexists(join(fw, "_calibration")))
        self.assertTrue(os.path.isfile(join(fw, "_logs", "keep.log")))
        self.assertTrue(os.path.isfile(join(fw, pi.INI_NAME)))
        self.assertTrue(os.path.isdir(join(fw, "_daq_core")) and not os.path.islink(join(fw, "_daq_core")))

    def test_write_atomic_keeps_mode_and_symlinks(self):
        target = join(self.tmp, "preset.ini")
        with open(target, "w") as f:
            f.write("old\n")
        os.chmod(target, 0o640)
        link = join(self.tmp, "linked.ini")
        os.symlink(target, link)
        pi.write_atomic(link, "new\n")
        self.assertTrue(os.path.islink(link))  # the link stays, its target is rewritten
        self.assertEqual(read(target), "new\n")
        st = os.stat(target)
        self.assertEqual(st.st_mode & 0o7777, 0o640)
        self.assertEqual((st.st_uid, st.st_gid), (os.geteuid(), os.getegid()))
        fresh = join(self.tmp, "fresh.ini")
        pi.write_atomic(fresh, "x\n", 0o600)
        self.assertEqual(os.stat(fresh).st_mode & 0o7777, 0o600)
        self.assertFalse([n for n in os.listdir(self.tmp) if n.startswith(".provision-")])

    def test_in_place_keeps_the_ini_mode(self):
        ini = join(self.root, "Firmware", pi.INI_NAME)
        os.chmod(ini, 0o660)
        rc, out, err = self.provision("--instance", "0", "--in-place", "--write")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(os.stat(ini).st_mode & 0o7777, 0o660)

    def test_template_device_serials_are_not_inherited(self):
        rc, out, err = self.provision("--instance", "0", "--in-place", "--device-serials",
                                      "1000,1001,1002,1003,1004", "--write")
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.provision("--instance", "1", "--write")
        self.assertEqual(rc, 0, out + err)
        hw_daq = params(join(self.dest, "Firmware", pi.INI_NAME))
        self.assertEqual(hw_daq["hw"]["device_serials"].strip(), "")
        self.assertEqual(hw_daq["hw"]["serial_base"], "1100")
        self.assertEqual(hw_daq["daq"]["ctr_channel_serial_no"], "1100")
        self.assertIn("template [hw] device_serials = 1000,1001,1002,1003,1004 dropped", err)
        # an explicit --device-serials still wins
        rc, out, err = self.provision("--instance", "2", "--device-serials", "3000,3001,3002,3003,3004", "--write")
        self.assertEqual(rc, 0, out + err)
        hw = params(join(self.root, "instances", "2", "Firmware", pi.INI_NAME))["hw"]
        self.assertEqual(hw["device_serials"], "3000,3001,3002,3003,3004")
        self.assertNotIn("dropped", err)

    def test_template_peer_list_dropped_with_cluster_members(self):
        ini = join(self.root, "Firmware", pi.INI_NAME)
        text = read(ini)
        self.assertIn("\npeer_list =\n", text)
        with open(ini, "w") as f:
            f.write(text.replace("\npeer_list =\n", "\npeer_list = hostx:1\n"))
        rc, out, err = self.provision("--instance", "1", "--write")  # no --cluster-members: kept
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(params(join(self.dest, "Firmware", pi.INI_NAME))["federation"]["peer_list"], "hostx:1")
        rc, out, err = self.provision("--instance", "2", "--cluster-members", "hosta:0,hosta:2", "--write")
        self.assertEqual(rc, 0, out + err)
        fed = params(join(self.root, "instances", "2", "Firmware", pi.INI_NAME))["federation"]
        self.assertEqual(fed["peer_list"].strip(), "")
        self.assertEqual(fed["cluster_members"], "hosta:0,hosta:2")
        self.assertIn("template [federation] peer_list = hostx:1 dropped", err)

    def set_root_keys(self, *items):
        """Rewrite (section, key, value) items of the root INI (the default template)."""
        ini = join(self.root, "Firmware", pi.INI_NAME)
        doc = pi.IniDocument(read(ini))
        for section, key, value in items:
            doc.set(section, key, value)
        with open(ini, "w") as f:
            f.write(doc.text())

    def test_coordinator_flag_replaces_the_template_coordinators(self):
        self.set_root_keys(("federation", "coordinator_candidates", "hosta:6000,hostb:6000,hostc:6000"),
                           ("federation", "advertise_host", "not-this-host-xyz"))
        rc, out, err = self.provision("--instance", "1", "--coordinator", "hostz:6000", "--register", "--write")
        self.assertEqual(rc, 0, out + err)
        fed = params(join(self.dest, "Firmware", pi.INI_NAME))["federation"]
        self.assertEqual((fed["coordinator_host"], fed["coordinator_port"]), ("hostz", "6000"))
        self.assertEqual(fed["coordinator_candidates"].strip(), "")
        self.assertIn("template [federation] coordinator_candidates = hosta:6000,hostb:6000,hostc:6000 "
                      "dropped: --coordinator hostz:6000 names the coordinators", err)
        self.assertIn("template [federation] advertise_host = not-this-host-xyz kept", err)
        # several coordinators: a template coordinator_host goes
        self.set_root_keys(("federation", "coordinator_host", "hosty"),
                           ("federation", "advertise_host", ""))
        rc, out, err = self.provision("--instance", "2", "--coordinator", "hostq:6000,hostr:6000", "--write")
        self.assertEqual(rc, 0, out + err)
        fed = params(join(self.root, "instances", "2", "Firmware", pi.INI_NAME))["federation"]
        self.assertEqual(fed["coordinator_candidates"], "hostq:6000,hostr:6000")
        self.assertEqual(fed["coordinator_host"].strip(), "")
        self.assertIn("template [federation] coordinator_host = hosty dropped", err)
        self.assertNotIn("advertise_host", err)

    def test_template_delay_sync_cores_outside_cpu_cores_dropped(self):
        rc, out, err = self.provision("--instance", "0", "--in-place", "--cores", "0,1,2,3",
                                      "--delay-sync-cores", "2", "--write")
        self.assertEqual(rc, 0, out + err)
        # instance 0 again: its own pin lies within its cores and stays
        rc, out, err = self.provision("--instance", "0", "--in-place", "--write")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(params(join(self.root, "Firmware", pi.INI_NAME))["host"]["delay_sync_cores"], "2")
        self.assertNotIn("delay_sync_cores", err)
        rc, out, err = self.provision("--instance", "1", "--write")
        self.assertEqual(rc, 0, out + err)
        host = params(join(self.dest, "Firmware", pi.INI_NAME))["host"]
        self.assertEqual(host["cpu_cores"], "4,5,6,7")
        self.assertEqual(host["delay_sync_cores"].strip(), "")
        self.assertIn("template [host] delay_sync_cores = 2 dropped: outside this instance's cpu_cores "
                      "4,5,6,7", err)
        rc, out, err = self.provision("--instance", "2", "--delay-sync-cores", "9", "--write")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(params(join(self.root, "instances", "2", "Firmware", pi.INI_NAME))["host"]
                         ["delay_sync_cores"], "9")
        self.assertNotIn("delay_sync_cores = 2 dropped", err)

    def test_upgrade_repoint_and_from_root(self):
        """H.8: instance dirs under a symlinked root survive the repoint with --from-root."""
        v1 = self.root
        link = join(self.tmp, "heimdall")
        os.symlink(v1, link)
        run = lambda *a: run_provision(["--root", link] + list(a), self.env)  # noqa: E731
        self.assertEqual(run("--instance", "0", "--in-place", "--write")[0], 0)
        self.assertEqual(run("--instance", "1", "--write")[0], 0)
        old_fw = join(v1, "instances", "1", "Firmware")
        with open(join(old_fw, pi.INI_NAME), "a") as f:
            f.write("; operator edit\n")
        with open(join(old_fw, "_db", "scan.db"), "w") as f:
            f.write("db")
        with open(join(old_fw, "_data_control", "iq_track_lock"), "w") as f:
            f.write("1\n")
        with open(join(old_fw, "_logs", "rtl_daq.log"), "w") as f:
            f.write("log")
        if hasattr(os, "mkfifo"):
            os.mkfifo(join(old_fw, "_data_control", "fw_decimator_in"))
        # install v2 (with its own shipped INI) and repoint the root
        v2 = join(self.tmp, "root-v2")
        shutil.copytree(v1, v2, symlinks=True, ignore=shutil.ignore_patterns("instances", "fw_*"))
        shutil.copy2(LIVE_INI, join(v2, "Firmware", pi.INI_NAME))
        for name in os.listdir(join(v2, "Firmware")):
            if name.startswith(pi.INI_NAME + ".bak."):
                os.unlink(join(v2, "Firmware", name))
        tmp_link = link + ".new"
        os.symlink(v2, tmp_link)
        os.replace(tmp_link, link)
        rc, _, err = run("--instance", "1", "--refresh", "--write")
        self.assertEqual(rc, 2, err)
        self.assertIn("pass --from-root <previous tree>", err)
        rc, out, err = run("--instance", "1", "--refresh", "--from-root", v1)  # dry run
        self.assertEqual(rc, 0, out + err)
        self.assertIn("carried over from " + join(old_fw, "_db"), out)
        self.assertFalse(os.path.exists(join(v2, "instances")))
        rc, out, err = run("--instance", "1", "--refresh", "--from-root", v1, "--write")
        self.assertEqual(rc, 0, out + err)
        new_fw = join(link, "instances", "1", "Firmware")
        self.assertEqual(read(join(new_fw, pi.INI_NAME)), read(join(old_fw, pi.INI_NAME)))
        self.assertEqual(read(join(new_fw, "_db", "scan.db")), "db")
        self.assertEqual(read(join(new_fw, "_data_control", "iq_track_lock")), "1\n")
        self.assertFalse(os.path.lexists(join(new_fw, "_data_control", "fw_decimator_in")))
        self.assertEqual(os.listdir(join(new_fw, "_logs")), [])
        self.assertEqual(os.readlink(join(new_fw, "_daq_core")), join(link, "Firmware", "_daq_core"))
        self.assertTrue(os.path.isfile(join(v2, "instances", "1", "Firmware", pi.INI_NAME)))
        self.assertTrue(os.path.isfile(join(old_fw, pi.INI_NAME)))  # the old tree stays for a rollback
        self.assertIn("ini_checker (no_hw): ok", out)
        rc, _, err = run("--instance", "1", "--refresh", "--from-root", v1, "--write")
        self.assertEqual(rc, 2, err)
        self.assertIn("refresh it without --from-root", err)
        self.assertEqual(run("--instance", "1", "--refresh", "--write")[0], 0)
        # instance 0: its in-place INI replaces the new tree's shipped one (backed up)
        rc, out, err = run("--instance", "0", "--refresh", "--from-root", v1, "--write")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(read(join(v2, "Firmware", pi.INI_NAME)), read(join(v1, "Firmware", pi.INI_NAME)))
        self.assertEqual(params(join(v2, "Firmware", pi.INI_NAME))["host"]["instance_guard"], "refuse")
        backups = [n for n in os.listdir(join(v2, "Firmware")) if n.startswith(pi.INI_NAME + ".bak.")]
        self.assertTrue(any(read(join(v2, "Firmware", b)) == read(LIVE_INI) for b in backups), backups)
        # usage errors
        for args, text in ((["--instance", "1", "--from-root", v1], "--from-root needs --refresh"),
                           (["--instance", "1", "--refresh", "--from-root", v2], "is the install root itself"),
                           (["--instance", "3", "--refresh", "--from-root", v1], "has no " + pi.INI_NAME),
                           (["--instance", "0", "--refresh"], "--from-root <previous tree>"),
                           (["--instance", "1", "--refresh", "--from-root", v1, "--dest", self.dest],
                            "--from-root conflicts with --dest")):
            rc, _, err = run(*args)
            self.assertEqual(rc, 2, (args, err))
            self.assertIn(text, err, args)

    def test_profile_and_explicit_flags(self):
        rc, out, err = self.provision("--instance", "1", "--profile", "minimal", "--write")
        self.assertEqual(rc, 0, out + err)
        p = params(join(self.dest, "Firmware", pi.INI_NAME))
        self.assertEqual(p["host"]["rt_priority_offset"], "4")
        self.assertEqual(p["host"]["allow_core_sharing"], "warn")
        self.assertEqual(p["offload"]["fft_workers"], "1")
        rc, out, err = self.provision("--instance", "2", "--profile", "minimal", "--rt-offset", "2",
                                      "--cores", "0,2,4,6", "--delay-sync-cores", "4,5", "--write")
        self.assertEqual(rc, 0, out + err)
        p = params(join(self.root, "instances", "2", "Firmware", pi.INI_NAME))
        self.assertEqual(p["host"]["rt_priority_offset"], "2")
        self.assertEqual(p["host"]["cpu_cores"], "0,2,4,6")
        self.assertEqual(p["host"]["delay_sync_cores"], "4,5")

    def test_federation_flags(self):
        rc, out, err = self.provision(
            "--instance", "1", "--cluster-members", "hosta:0,hosta:1,hostb:0", "--node-id", "lab-a:1",
            "--cluster-id", "lab", "--coordinator", "hosta:6000,hostb:6000", "--register",
            "--advertise-host", "hosta", "--stride", "200", "--unit-id", "7",
            "--antenna-switch-gpio", "none", "--device-serials", "2000,2001,2002,2003,2004", "--write")
        self.assertEqual(rc, 0, out + err)
        p = params(join(self.dest, "Firmware", pi.INI_NAME))
        fed = p["federation"]
        self.assertEqual(fed["cluster_members"], "hosta:0,hosta:1,hostb:0")
        self.assertEqual(fed["en_federation"], "1")
        self.assertEqual(fed["coordinator_candidates"], "hosta:6000,hostb:6000")
        self.assertEqual(fed["register_with_coordinator"], "1")
        self.assertEqual(fed["advertise_host"], "hosta")
        self.assertEqual(fed["node_id"], "lab-a:1")
        self.assertEqual(fed["cluster_id"], "lab")
        self.assertEqual(fed["port_stride"], "200")
        self.assertEqual(p["hw"]["unit_id"], "7")
        self.assertEqual(p["hw"]["antenna_switch_gpio"], "none")
        self.assertEqual(p["hw"]["device_serials"], "2000,2001,2002,2003,2004")
        self.assertNotIn("serial_base", p["hw"])
        self.assertEqual(p["daq"]["ctr_channel_serial_no"], "2000")
        self.assertIn("  iq       5200", out)
        rc, out, err = self.provision("--instance", "2", "--coordinator", "hostc:6100", "--write")
        self.assertEqual(rc, 0, out + err)
        fed = params(join(self.root, "instances", "2", "Firmware", pi.INI_NAME))["federation"]
        self.assertEqual((fed["coordinator_host"], fed["coordinator_port"]), ("hostc", "6100"))
        self.assertNotIn("coordinator_candidates", fed)

    def test_systemd_env_file(self):
        env_file = join(self.tmp, "etc", "heimdall-daq@1")
        rc, out, err = self.provision("--instance", "1", "--systemd", "--env-file", env_file,
                                      "--python-bin", "/opt/py/bin", "--write")
        self.assertEqual(rc, 0, out + err)
        lines = read(env_file).splitlines()
        self.assertTrue(lines[0].startswith("PATH=/opt/py/bin:/opt/miniforge3/envs/kraken/bin:"), lines)
        self.assertIn("HEIMDALL_RUN_DIR=/run/heimdall", lines)
        self.assertEqual(params(join(self.dest, "Firmware", pi.INI_NAME))["host"]["host_tuning"], "0")
        self.assertIn("systemctl enable --now heimdall-daq-supervised@1", out)
        # the installed packaging template wins over the built-in one
        os.makedirs(join(self.root, "packaging", "default"))
        with open(join(self.root, "packaging", "default", "heimdall-daq"), "w") as f:
            f.write("# site template\nPATH=/usr/bin:/bin\nHEIMDALL_RUN_DIR=/run/heimdall\n")
        rc, out, err = self.provision("--instance", "0", "--in-place", "--systemd", "--env-file",
                                      env_file, "--python-bin", "/opt/py/bin", "--write")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(read(env_file), "# site template\nPATH=/opt/py/bin:/usr/bin:/bin\n"
                                         "HEIMDALL_RUN_DIR=/run/heimdall\n")
        self.assertIn("systemctl enable --now heimdall-daq-supervised\n", out)

    def test_validation_failure_exit_1_keeps_files(self):
        rc, out, err = self.provision("--instance", "1", "--device-serials", "2000,2001", "--write")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("FAILED", out)
        self.assertIn("device_serials", out + err)
        self.assertTrue(os.path.isfile(join(self.dest, "Firmware", pi.INI_NAME)))

    def test_usage_errors(self):
        for args in (["--instance", "1", "--cores", "1,2,3"], ["--instance", "1", "--cores", "1,1,2,3"],
                     ["--instance", "1", "--rt-offset", "90"], ["--instance", "1", "--coordinator", "hosta"],
                     ["--instance", "1", "--env-file", "/tmp/x"], ["--instance", "1", "--unit-id", "x"],
                     ["--instance", "1", "--in-place", "--dest", "/tmp/x"], ["--instance", "1", "--profile", "turbo"],
                     ["--instance", "1", "--serial-base", "1", "--device-serials", "1,2,3,4,5"],
                     ["--instance", "1", "--refresh"]):
            rc, _, err = self.provision(*args)
            self.assertEqual(rc, 2, (args, err))
        rc, _, err = run_provision(["--root", join(self.tmp, "nowhere"), "--instance", "1"], self.env)
        self.assertEqual(rc, 2, err)
        self.assertIn("not a HeIMDALL install root", err)

    def test_cores_beyond_this_host(self):
        n = (os.cpu_count() or 1) // 4 + 1  # 4n+3 >= nproc
        # another host's tree (a temp root): a warning, and the plan is written
        rc, out, err = self.provision("--instance", str(n), "--write")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("exceed the 0..", err)
        # this host's own tree: --write refuses (exit 1); the dry run only warns
        script = join(self.root, "util", "provision_instance.py")
        rc, _, err = run_provision(["--instance", str(n + 1), "--write"], self.env, script=script)
        self.assertEqual(rc, 1, err)
        self.assertIn("pass --cores", err)
        rc, out, err = run_provision(["--instance", str(n + 1)], self.env, script=script)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("exceed the 0..", err)

    def test_check_attached_runs_the_sysfs_serial_check(self):
        sysfs = join(self.tmp, "sysfs")
        for i in range(5):
            d = join(sysfs, "1-1.{0}".format(i + 1))
            os.makedirs(d)
            for attr, value in (("idVendor", "0bda"), ("idProduct", "2838"), ("serial", str(1100 + i)),
                                ("busnum", "1"), ("devnum", str(10 + i))):
                with open(join(d, attr), "w") as f:
                    f.write(value + "\n")
        env = dict(self.env, HEIMDALL_SYSFS_ROOT=sysfs)
        rc, out, err = run_provision(["--root", self.root, "--instance", "1", "--check-attached"], env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ini_checker (no_hw attached): ok", out)
        # unit 2's serials are not attached: the attached check fails, plain no_hw does not
        rc, out, err = run_provision(["--root", self.root, "--instance", "2", "--check-attached", "--write"], env)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("1200", out)
        self.assertTrue(os.path.isfile(join(self.root, "instances", "2", "Firmware", pi.INI_NAME)))
        rc, out, err = run_provision(["--root", self.root, "--instance", "3"], env)
        self.assertEqual(rc, 0, out + err)

    def test_logrotate_stanza_for_other_roots(self):
        rc, out, _ = self.provision("--instance", "1")
        self.assertEqual(rc, 0)
        self.assertIn("{0}/instances/*/Firmware/_logs/*.log".format(self.root), out)
        self.assertIn("copytruncate", out)


# ---------------------------------------------------------------------------
# cluster_cfg_gen.py
# ---------------------------------------------------------------------------

class TesterClusterCfgGen(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cluster_gen_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = join(self.tmp, "out")

    def generate(self, *args):
        res = subprocess.run([sys.executable, CLUSTER_GEN, "--template", LIVE_INI, "--out", self.out] + list(args),
                             capture_output=True, text=True, timeout=120, env=clean_env())
        return res.returncode, res.stdout, res.stderr

    def instance_files(self):
        found = []
        for host in sorted(os.listdir(self.out)):
            hdir = join(self.out, host)
            if not os.path.isdir(hdir):
                continue
            for inst in sorted(os.listdir(hdir)):
                path = join(hdir, inst, pi.INI_NAME)
                if os.path.isfile(path):
                    found.append((host, int(inst[len("inst"):]), path))
        return found

    def test_round_trip(self):
        rc, out, err = self.generate("--hosts", "hosta:2", "hostb:1", "--coordinators",
                                     "hosta:6000,hostb:6000,hostc:6000", "--router", "hosta:7000",
                                     "--register", "--cluster-id", "lab-a", "--cores-per-host", "hosta=8,hostb=4")
        self.assertEqual(rc, 0, out + err)
        files = self.instance_files()
        self.assertEqual([(h, i) for h, i, _ in files], [("hosta", 0), ("hosta", 1), ("hostb", 0)])
        memberships, serials, ports_by_host = [], [], {}
        for host, iid, path in files:
            p = ini_checker.read_config_file(path)
            self.assertEqual(ini_checker.check_ini(p, en_hw_check=False), [], path)
            fed = p["federation"]
            self.assertEqual(fed["instance_id"], str(iid))
            self.assertEqual(fed["register_with_coordinator"], "1")
            self.assertEqual(fed["en_federation"], "1")
            self.assertEqual(fed["advertise_host"], host)
            self.assertEqual(fed["node_id"], "{0}:{1}".format(host, iid))
            self.assertEqual(fed["cluster_id"], "lab-a")
            self.assertEqual(fed["stamp_node_id"], "1")
            self.assertEqual(fed["coordinator_candidates"], "hosta:6000,hostb:6000,hostc:6000")
            self.assertEqual(fed.get("peer_list", "").strip(), "")
            self.assertEqual(p["hw"]["unit_id"], "auto")
            self.assertEqual(p["daq"]["ctl_max_connections"], "4")
            self.assertEqual(p["host"]["instance_registry"], "1")
            self.assertEqual(p["host"]["instance_guard"], "refuse")
            self.assertEqual(p["host"]["cpu_cores"], ",".join(str(4 * iid + k) for k in range(4)))
            for key, value in (("en_monitoring", "1"), ("en_status_server", "1"),
                               ("status_extended", "1"), ("status_update_interval", "10")):
                self.assertEqual(p["monitoring"][key], value)
            members = fed["cluster_members"].split(",")
            self.assertIn("{0}:{1}".format(host, iid), members)
            memberships.append(members)
            resolved = ini_checker.resolve_serials(p["hw"])
            self.assertIn(p["daq"]["ctr_channel_serial_no"], resolved)
            serials.append(set(resolved))
            ports = [port for _, port, on in pi.instance_ports(p, iid, 100) if on]
            ports_by_host.setdefault(host, []).extend(ports)
        self.assertTrue(all(m == memberships[0] for m in memberships))  # symmetric
        self.assertEqual(memberships[0], ["hosta:0", "hosta:1", "hostb:0"])
        for a in range(len(serials)):
            for b in range(a + 1, len(serials)):
                self.assertFalse(serials[a] & serials[b])
        self.assertEqual(sorted(serials[1]), ["1100", "1101", "1102", "1103", "1104"])
        for host, ports in ports_by_host.items():
            self.assertEqual(len(ports), len(set(ports)), host)
        # federation.ini files pass check_federation_ini
        for rel in ("federation.ini", join("hostb", "federation.ini")):
            fp = ini_checker.read_config_file(join(self.out, rel))
            errors, _ = ini_checker.check_federation_ini(fp)
            self.assertEqual(errors, [], rel)
        central = params(join(self.out, "federation.ini"))["federation"]
        self.assertEqual(central["router_tag_mode"], "node")
        self.assertEqual(central["router_sources"], "hosta:0,hosta:1,router://hostb:7000")
        self.assertEqual(central["cluster_members"], "hosta:0,hosta:1,hostb:0")
        self.assertEqual(params(join(self.out, "hostb", "federation.ini"))["federation"]["router_sources"], "hostb:0")
        # ports.md and provision.sh
        ports_md = read(join(self.out, "ports.md"))
        for port in ("1130", "5000", "5001", "5002", "5003", "6000", "7000", "7001"):
            self.assertIn(port, ports_md)
        sh = read(join(self.out, "provision.sh"))
        lines = [l for l in sh.splitlines() if not l.startswith("#") and "provision_instance.py" in l]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual("--in-place" in line, "--instance 0 " in line, line)
        self.assertEqual(subprocess.run(["sh", "-n", join(self.out, "provision.sh")]).returncode, 0)

    def test_template_peer_list_is_cleared(self):
        tpl = join(self.tmp, "tpl.ini")
        text = read(LIVE_INI)
        self.assertIn("\npeer_list =\n", text)
        with open(tpl, "w") as f:
            f.write(text.replace("\npeer_list =\n", "\npeer_list = hostx:1\n"))
        res = subprocess.run([sys.executable, CLUSTER_GEN, "--template", tpl, "--out", self.out,
                              "--hosts", "hosta:2"], capture_output=True, text=True, timeout=120, env=clean_env())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("peer_list overrides", res.stderr)
        for _, _, path in self.instance_files():
            fed = params(path)["federation"]
            self.assertEqual(fed["peer_list"].strip(), "", path)
            self.assertEqual(fed["cluster_members"], "hosta:0,hosta:1")

    def site_template(self, *items):
        """The cluster_site preset (candidates + register = 1) with extra (section, key, value) items."""
        doc = pi.IniDocument(read(join(PRESETS, "cluster_site", pi.INI_NAME)))
        for section, key, value in items:
            doc.set(section, key, value)
        path = join(self.tmp, "site_tpl.ini")
        with open(path, "w") as f:
            f.write(doc.text())
        return path

    def generate_from(self, template, *args):
        res = subprocess.run([sys.executable, CLUSTER_GEN, "--template", template, "--out", self.out]
                             + list(args), capture_output=True, text=True, timeout=120, env=clean_env())
        return res.returncode, res.stdout, res.stderr

    def test_template_coordinators_and_register_follow_the_plan(self):
        tpl = self.site_template()
        self.assertEqual(params(tpl)["federation"]["register_with_coordinator"], "1")
        rc, out, err = self.generate_from(tpl, "--hosts", "hostx:1", "hosty:1", "--coordinators",
                                          "hostz:6000", "--register")
        self.assertEqual(rc, 0, out + err)
        for _, _, path in self.instance_files():
            fed = params(path)["federation"]
            self.assertEqual(fed["coordinator_host"], "hostz", path)
            self.assertEqual(fed["coordinator_candidates"].strip(), "", path)
            self.assertEqual(fed["register_with_coordinator"], "1", path)
        shutil.rmtree(self.out)
        rc, out, err = self.generate_from(tpl, "--hosts", "hostx:1", "hosty:1")
        self.assertEqual(rc, 0, out + err)
        for _, _, path in self.instance_files():
            fed = params(path)["federation"]
            self.assertEqual(fed["coordinator_candidates"].strip(), "", path)
            self.assertEqual(fed["coordinator_host"].strip(), "", path)
            self.assertEqual(fed["register_with_coordinator"], "0", path)
        shutil.rmtree(self.out)
        tpl = self.site_template(("federation", "coordinator_host", "hostw"),
                                 ("federation", "coordinator_candidates", ""),
                                 ("federation", "coordinator_self", "hostw:6000"))
        rc, out, err = self.generate_from(tpl, "--hosts", "hostx:1", "--coordinators",
                                          "hostq:6000,hostr:6000,hosts:6000", "--register")
        self.assertEqual(rc, 0, out + err)
        fed = params(join(self.out, "hostx", "inst0", pi.INI_NAME))["federation"]
        self.assertEqual(fed["coordinator_candidates"], "hostq:6000,hostr:6000,hosts:6000")
        self.assertEqual(fed["coordinator_host"].strip(), "")
        self.assertEqual(fed["coordinator_self"].strip(), "")

    def test_template_delay_sync_cores_outside_the_instance_cores_dropped(self):
        tpl = self.site_template(("host", "delay_sync_cores", "2"))
        rc, out, err = self.generate_from(tpl, "--hosts", "hostx:2")
        self.assertEqual(rc, 0, out + err)
        inst0 = params(join(self.out, "hostx", "inst0", pi.INI_NAME))["host"]
        inst1 = params(join(self.out, "hostx", "inst1", pi.INI_NAME))["host"]
        self.assertEqual((inst0["cpu_cores"], inst0["delay_sync_cores"]), ("0,1,2,3", "2"))
        self.assertEqual((inst1["cpu_cores"], inst1["delay_sync_cores"].strip()), ("4,5,6,7", ""))
        self.assertIn(join("hostx", "inst1", pi.INI_NAME) + ": template [host] delay_sync_cores = 2 "
                      "dropped: outside this instance's cpu_cores 4,5,6,7", err)
        self.assertNotIn(join("hostx", "inst0", pi.INI_NAME) + ": template [host] delay_sync_cores", err)

    def test_install_headers_name_the_exact_hosts(self):
        rc, out, err = self.generate("--hosts", "hosta:2", "hostb:1", "--coordinators",
                                     "hosta:6000,hostb:6000,hostc:6000", "--register", "--router", "hosta:7000")
        self.assertEqual(rc, 0, out + err)
        central = "\n".join(comment_lines(read(join(self.out, "federation.ini"))))
        self.assertIn("Install as /etc/heimdall/federation.ini on hosta, hostc only.", central)
        self.assertNotIn("every coordinator host", central)
        self.assertIn("uses that file instead", central)
        self.assertIn("Install as /etc/heimdall/federation.ini on hostb.",
                      "\n".join(comment_lines(read(join(self.out, "hostb", "federation.ini")))))
        self.out = join(self.tmp, "out-no-router")
        rc, out, err = self.generate("--hosts", "hosta:1", "hostb:1", "--coordinators",
                                     "hosta:6000,hostb:6000,hostc:6000")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("on every coordinator host (hosta, hostb, hostc).",
                      "\n".join(comment_lines(read(join(self.out, "federation.ini")))))
        self.assertFalse(os.path.exists(join(self.out, "hostb", "federation.ini")))

    def test_provision_sh_drives_provision_instance(self):
        rc, out, err = self.generate("--hosts", "hosta:2", "hostb:1", "--coordinators", "hosta:6000")
        self.assertEqual(rc, 0, out + err)
        root = make_root(self.tmp)
        env = clean_env({"HEIMDALL_ROOT": root, "PYTHON": sys.executable,
                         "HEIMDALL_RUN_DIR": join(self.tmp, "run")})
        res = subprocess.run(["sh", join(self.out, "provision.sh"), "hosta"], env=env, capture_output=True,
                             text=True, timeout=180)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(read(join(root, "Firmware", pi.INI_NAME)), read(join(self.out, "hosta", "inst0", pi.INI_NAME)))
        self.assertEqual(read(join(root, "instances", "1", "Firmware", pi.INI_NAME)),
                         read(join(self.out, "hosta", "inst1", pi.INI_NAME)))
        fed = params(join(root, "Firmware", pi.INI_NAME))["federation"]
        self.assertEqual((fed["coordinator_host"], fed["coordinator_port"]), ("hosta", "6000"))
        res = subprocess.run(["sh", join(self.out, "provision.sh"), "hostz"], env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(res.returncode, 2)
        self.assertIn("planned: hosta hostb", res.stderr)

    def test_instance_10_collides_with_the_coordinator(self):
        rc, out, err = self.generate("--hosts", "hosta:11", "hostb:1", "--coordinators", "hosta:6000,hostb:6000,hostc:6000")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("hosta/inst10/daq_chain_config.ini: [federation] instance_id: iq port 6000", err)
        self.assertIn("coordinator port", err)
        self.assertIn("keep instance ids 0-9 on hosta", err)

    def test_core_plan_exceeded(self):
        rc, out, err = self.generate("--hosts", "hosta:3", "--cores-per-host", "hosta=8")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("[host] cpu_cores", err)
        self.assertIn("need 12 cores", err)

    def test_two_coordinators_quorum_warning_and_single_coordinator(self):
        rc, out, err = self.generate("--hosts", "hosta:1", "hostb:1", "--coordinators", "hosta:6000,hostb:6000")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("with 2 candidates, losing either host blocks all mutations", err)
        rc, out, err = self.generate("--hosts", "hosta:1", "--coordinators", "hostc:6000", "--health-mode", "mesh")
        self.assertEqual(rc, 0, out + err)
        fed = params(join(self.out, "hosta", "inst0", pi.INI_NAME))["federation"]
        self.assertEqual((fed["coordinator_host"], fed["coordinator_port"]), ("hostc", "6000"))
        self.assertNotIn("coordinator_candidates", fed)
        self.assertNotIn("register_with_coordinator", fed)
        self.assertEqual(fed["en_federation"], "1")

    def test_auth_token_and_serial_layout(self):
        token = join(self.tmp, "token")
        with open(token, "w") as f:
            f.write("s3cret-token\n")
        rc, out, err = self.generate("--hosts", "hosta:1", "hostb:2", "--serial-base", "5000",
                                     "--serial-stride", "10", "--auth-token-file", token,
                                     "--coordinators", "hosta:6000")
        self.assertEqual(rc, 0, out + err)
        bases = [params(path)["hw"]["serial_base"] for _, _, path in self.instance_files()]
        self.assertEqual(bases, ["5000", "5010", "5020"])
        self.assertEqual(params(join(self.out, "hostb", "inst1", pi.INI_NAME))["federation"]["auth_token"], "s3cret-token")
        self.assertEqual(params(join(self.out, "federation.ini"))["federation"]["auth_token"], "s3cret-token")

    def test_usage_errors(self):
        for args in (["--hosts", "localhost:1"], ["--hosts", "hosta:0"], ["--hosts", "hosta:1", "hosta:2"],
                     ["--hosts", "hosta:1", "--register"], ["--hosts", "hosta:1", "--health-mode", "hub"],
                     ["--hosts", "hosta:1", "--serial-stride", "4"],
                     ["--hosts", "hosta:1", "--coordinators", "hosta:6000,hostb:6100"],
                     ["--hosts", "hosta:1", "--cores-per-host", "hostz=4"],
                     ["--hosts", "hosta:1", "--cluster-id", "bad id"]):
            rc, _, err = self.generate(*args)
            self.assertEqual(rc, 2, (args, err))


# ---------------------------------------------------------------------------
# cfg_gen.py, profiles, presets, kerberos script
# ---------------------------------------------------------------------------

class TesterShippedFiles(unittest.TestCase):

    def test_cfg_gen_output(self):
        try:
            import numpy  # noqa: F401  (cfg_gen computes with numpy)
        except ImportError:
            self.skipTest("SKIP reason: numpy not installed")
        with tempfile.TemporaryDirectory(prefix="cfg_gen_") as tmp:
            res = subprocess.run([sys.executable, CFG_GEN, "--bri", "1000", "--burst_length", "500", "--bw", "100"],
                                 cwd=tmp, capture_output=True, text=True, timeout=120, env=clean_env())
            self.assertEqual(res.returncode, 0, res.stderr)
            path = join(tmp, "autogen.ini")
            p = ini_checker.read_config_file(path)
            self.assertEqual(p["meta"]["ini_version"], "8")
            self.assertNotIn("squelch", p)
            self.assertEqual(p["daq"]["ctr_channel_serial_no"], "1000")
            self.assertEqual(ini_checker.check_ini(p, en_hw_check=False), [])
            # an impossible CPI fails with exit 1 and writes nothing
            res = subprocess.run([sys.executable, CFG_GEN, "--bri", "100", "--burst_length", "10", "--bw", "100"],
                                 cwd=join(tmp), capture_output=True, text=True, timeout=120, env=clean_env())
            self.assertEqual(res.returncode, 1, res.stderr)

    def test_profiles_are_host_and_offload_snippets(self):
        for name in ("minimal", "balanced", "maximum"):
            p = params(join(PRESETS, "performance", name + ".ini"))
            self.assertEqual(set(p), {"host", "offload"}, name)
            self.assertIn("fft_workers", p["offload"])
            self.assertNotIn("cpu_cores", p["host"])  # instance-specific, never in a profile
            self.assertNotIn("host_tuning", p["host"])  # owned by --systemd
            merged = read(LIVE_INI) + "\n[host]\ninstance_registry = 1\n"
            doc = pi.IniDocument(merged)
            for section, items in p.items():
                for key, value in items.items():
                    doc.set(section, key, value)
            self.assertEqual(ini_checker.check_ini(pi.params_of_text(doc.text()), en_hw_check=False), [], name)
        readme = read(join(PRESETS, "performance", "README.md"))
        self.assertIn("documentation only", readme)
        for legacy in ("minimal.conf", "balanced.conf", "maximum.conf"):
            self.assertIn(legacy, readme)

    def test_new_presets_validate_without_warnings(self):
        for rel in ("multi_instance_x86", "cluster_site"):
            path = join(PRESETS, rel, pi.INI_NAME)
            res = subprocess.run([sys.executable, join(FIRMWARE, "ini_checker.py"), path, "no_hw"],
                                 capture_output=True, text=True, timeout=60, env=clean_env())
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertNotIn("WARNING:", res.stderr, rel)
        x86 = params(join(PRESETS, "multi_instance_x86", pi.INI_NAME))
        self.assertEqual(x86["federation"]["instance_id"], "1")
        self.assertEqual(x86["host"]["cpu_cores"], "4,5,6,7")
        self.assertEqual(x86["hw"]["serial_base"], "1100")
        site = params(join(PRESETS, "cluster_site", pi.INI_NAME))
        self.assertEqual(len(site["federation"]["cluster_members"].split(",")), 3)
        self.assertEqual(len({m.split(":")[0] for m in site["federation"]["cluster_members"].split(",")}), 2)
        res = subprocess.run([sys.executable, join(FIRMWARE, "ini_checker.py"),
                              join(PRESETS, "cluster_site", "federation.ini"), "--federation"],
                             capture_output=True, text=True, timeout=60, env=clean_env())
        self.assertEqual(res.returncode, 0, res.stderr)
        fed = params(join(PRESETS, "cluster_site", "federation.ini"))["federation"]
        self.assertEqual(len(fed["coordinator_candidates"].split(",")), 3)

    def test_kerberos_script_syntax(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("SKIP reason: bash not installed")
        res = subprocess.run([bash, "-n", join(UTIL, "kerberos_eeprom_init.sh")], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("set_unit_serials.sh", read(join(UTIL, "kerberos_eeprom_init.sh")))

    def test_help_exits_0(self):
        for script in (PROVISION, CLUSTER_GEN):
            res = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True, timeout=60)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("usage:", res.stdout)


if __name__ == "__main__":
    unittest.main()
