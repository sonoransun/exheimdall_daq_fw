"""
    Description :
    Tests for util/set_unit_serials.sh (design spec A.5, A.7), the KrakenSDR
    multi-unit EEPROM serial programming tool:

        set_unit_serials.sh --list
        set_unit_serials.sh --unit K [--count M] [--base 1000] [--stride 100]
                            [--by-index] [--dry-run] [--yes] [--force]

    exit 0 ok / plan, 2 usage, 3 a chain or another program holds the
    receivers, 4 receiver count != --count, 5 no rtl_eeprom, 6 old serials not
    contiguous, 7 verify mismatch.

    Hardware-free and CI-safe: a fake sysfs tree (HEIMDALL_SYSFS_ROOT), a fake
    registry directory (HEIMDALL_RUN_DIR) and PATH-stubbed rtl_eeprom/pgrep.
    The rtl_eeprom stub reproduces the stderr of rtl_eeprom.c ("Found N
    device(s):", "Using device j: ...", "Serial number:\t\t<s>", the busy and
    out-of-range failures) and keeps each receiver's EEPROM serial in a state
    file, so writes and the verify step are exercised end to end.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from os.path import dirname, join, realpath

current_path = dirname(realpath(__file__))
REPO = dirname(dirname(dirname(current_path)))
TOOL = join(REPO, "util", "set_unit_serials.sh")
BASH = shutil.which("bash")

_HOOKS = ("HEIMDALL_SYSFS_ROOT", "HEIMDALL_RUN_DIR", "HEIMDALL_PROC_ROOT")

RTL_EEPROM_STUB = r"""#!/bin/sh
# rtl_eeprom stub: receivers = lines of $STATE/serials (index order), busy indices in
# $STATE/busy, writes ignored when $STATE/ignore_writes exists. Every call is logged.
STATE="__STATE__"
echo "$*" >> "$STATE/calls.log"
n=$(wc -l < "$STATE/serials" | tr -d ' ')
if [ "$n" -eq 0 ]; then
    echo "No supported devices found." >&2
    exit 1
fi
dev=0; new=""
while [ $# -gt 0 ]; do
    case "$1" in
        -d) dev=$2; shift ;;
        -s) new=$2; shift ;;
    esac
    shift
done
echo "Found $n device(s):" >&2
i=0
while [ "$i" -lt "$n" ]; do echo "  $i:  Generic RTL2832U OEM" >&2; i=$((i + 1)); done
echo "" >&2
if [ "$dev" -ge "$n" ]; then
    echo "Using device $dev: " >&2
    echo "Failed to open rtlsdr device #$dev." >&2
    exit 1
fi
echo "Using device $dev: Generic RTL2832U OEM" >&2
if grep -qx "$dev" "$STATE/busy" 2>/dev/null; then
    echo "usb_claim_interface error -6" >&2
    echo "Failed to open rtlsdr device #$dev." >&2
    exit 1
fi
cur=$(sed -n "$((dev + 1))p" "$STATE/serials")
echo "Current configuration:" >&2
echo "__________________________________________" >&2
printf 'Vendor ID:\t\t0x0bda\nProduct ID:\t\t0x2838\nManufacturer:\t\tRealtek\n' >&2
printf 'Product:\t\tRTL2838UHIDIR\nSerial number:\t\t%s\n' "$cur" >&2
echo "__________________________________________" >&2
if [ -n "$new" ]; then
    echo "" >&2
    echo "New configuration:" >&2
    printf 'Serial number:\t\t%s\n' "$new" >&2
    printf 'Write new configuration to device [y/n]? ' >&2
    read -r answer
    [ "$answer" = "y" ] || exit 0
    if [ ! -e "$STATE/ignore_writes" ]; then
        awk -v line=$((dev + 1)) -v s="$new" 'NR == line { print s; next } { print }' \
            "$STATE/serials" > "$STATE/serials.tmp" && mv "$STATE/serials.tmp" "$STATE/serials"
    fi
    echo "" >&2
    echo "Configuration successfully written." >&2
fi
exit 0
"""

PGREP_STUB = """#!/bin/sh
# pgrep stub: "running" when $STATE/rtl_daq_running exists
echo "$*" >> "__STATE__/pgrep.log"
[ -e "__STATE__/rtl_daq_running" ] && { echo 4242; exit 0; }
exit 1
"""


@unittest.skipIf(os.name != "posix" or BASH is None, "SKIP reason: needs bash")
class TesterSetUnitSerials(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="unit_serials_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = join(self.tmp, "state")
        self.bin = join(self.tmp, "bin")
        self.sysfs = join(self.tmp, "sysfs")
        self.run_dir = join(self.tmp, "run")
        for d in (self.state, self.bin, self.sysfs, self.run_dir):
            os.makedirs(d)
        for name, body in (("rtl_eeprom", RTL_EEPROM_STUB), ("pgrep", PGREP_STUB)):
            path = join(self.bin, name)
            with open(path, "w") as f:
                f.write(body.replace("__STATE__", self.state))
            os.chmod(path, 0o755)

    # -- fixtures ----------------------------------------------------------

    def receivers(self, serials, busy=(), sysfs_serials=None, extra_sysfs=()):
        """librtlsdr sees *serials* in index order; sysfs lists the same receivers
        (or *sysfs_serials*) plus *extra_sysfs* RTL2832Us and one Realtek NIC."""
        with open(join(self.state, "serials"), "w") as f:
            f.write("".join("{0}\n".format(s) for s in serials))
        with open(join(self.state, "busy"), "w") as f:
            f.write("".join("{0}\n".format(b) for b in busy))
        listed = list(serials if sysfs_serials is None else sysfs_serials) + list(extra_sysfs)
        for k, s in enumerate(listed):
            self.sysfs_device("1-1.{0}".format(k + 1), "0bda", "2838", s, busnum=1, devnum=10 + k)
        self.sysfs_device("2-1", "0bda", "8153", "000001", busnum=2, devnum=3)  # RTL8153 NIC
        self.sysfs_device("usb1", "1d6b", "0002", "0000:00:14.0", busnum=1, devnum=1)

    def sysfs_device(self, name, vendor, product, serial, busnum, devnum):
        d = join(self.sysfs, name)
        os.makedirs(d)
        for attr, value in (("idVendor", vendor), ("idProduct", product), ("serial", serial),
                            ("busnum", busnum), ("devnum", devnum)):
            with open(join(d, attr), "w") as f:
                f.write("{0}\n".format(value))

    def eeprom(self):
        with open(join(self.state, "serials")) as f:
            return f.read().split()

    def calls(self):
        try:
            with open(join(self.state, "calls.log")) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def run_tool(self, *args, path=None, env_extra=None):
        env = {k: v for k, v in os.environ.items() if k not in _HOOKS}
        env["PATH"] = path if path is not None else self.bin + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        env["HEIMDALL_SYSFS_ROOT"] = self.sysfs
        env["HEIMDALL_RUN_DIR"] = self.run_dir
        if env_extra:
            env.update(env_extra)
        res = subprocess.run([BASH, TOOL] + list(args), env=env, capture_output=True, text=True,
                             timeout=60, stdin=subprocess.DEVNULL)
        return res.returncode, res.stdout, res.stderr

    def plan_rows(self, stdout):
        """{idx: (old, new)} of the printed plan table."""
        rows = {}
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0].isdigit():
                rows[int(parts[0])] = (parts[2], parts[3])
        return rows

    # -- plan ---------------------------------------------------------------

    def test_plan_for_unit_1_keeps_factory_order(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"])
        rc, out, err = self.run_tool("--unit", "1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.plan_rows(out), {j: (str(1000 + j), str(1100 + j)) for j in range(5)})
        self.assertIn("Dry run: nothing written", out)
        self.assertEqual(self.eeprom(), ["1000", "1001", "1002", "1003", "1004"])
        self.assertFalse(any("-s" in c.split() for c in self.calls()), self.calls())
        # bus:dev of each receiver comes from the sysfs serial match
        self.assertIn("1:10", out)

    def test_plan_maps_by_offset_from_the_lowest_serial(self):
        # librtlsdr index order differs from the factory channel order
        self.receivers(["1003", "1001", "1000", "1004", "1002"])
        rc, out, err = self.run_tool("--unit", "2", "--base", "1000", "--stride", "100")
        self.assertEqual(rc, 0, err)
        rows = self.plan_rows(out)
        self.assertEqual([rows[j][1] for j in range(5)], ["1203", "1201", "1200", "1204", "1202"])

    def test_by_index_ignores_the_old_serials(self):
        self.receivers(["1003", "1001", "1000", "1004", "1002"])
        rc, out, err = self.run_tool("--unit", "1", "--by-index")
        self.assertEqual(rc, 0, err)
        self.assertEqual([self.plan_rows(out)[j][1] for j in range(5)], [str(1100 + j) for j in range(5)])

    def test_count_option_and_base(self):
        self.receivers(["7", "8", "9"])
        rc, out, err = self.run_tool("--unit", "3", "--count", "3", "--base", "2000", "--stride", "10")
        self.assertEqual(rc, 0, err)
        self.assertEqual([self.plan_rows(out)[j][1] for j in range(3)], ["2030", "2031", "2032"])

    # -- refusals -------------------------------------------------------------

    def test_exact_count_refusal(self):
        # two units plugged in: 10 RTL2832U receivers in sysfs
        self.receivers(["1000", "1001", "1002", "1003", "1004"],
                       extra_sysfs=["1000", "1001", "1002", "1003", "1004"])
        rc, out, err = self.run_tool("--unit", "1", "--yes")
        self.assertEqual(rc, 4, out + err)
        self.assertIn("found 10 RTL2832U receiver(s)", err)
        self.assertIn("plug in exactly one unit", err)
        self.assertEqual(self.calls(), [])

    def test_no_receiver_is_a_count_refusal(self):
        self.receivers([])
        rc, _, err = self.run_tool("--unit", "0")
        self.assertEqual(rc, 4, err)

    def test_live_registry_refusal(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"])
        stub = join(self.tmp, "delay_sync.py")
        with open(stub, "w") as f:
            f.write("import time\ntime.sleep(60)\n")
        child = subprocess.Popen([sys.executable, stub])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        with open(join(self.run_dir, "inst1.env"), "w") as f:
            f.write("SCHEMA=1\nINSTANCE_ID=1\nMODE=hw\nPIDS=rtl_daq:999999,delay_sync:{0}\n".format(child.pid))
        deadline = time.time() + 10
        while time.time() < deadline:  # the child's args must show delay_sync.py
            ps = subprocess.run(["ps", "-o", "args=", "-p", str(child.pid)], capture_output=True, text=True)
            if "delay_sync" in ps.stdout:
                break
            time.sleep(0.05)
        rc, out, err = self.run_tool("--unit", "1", "--yes")
        self.assertEqual(rc, 3, out + err)
        self.assertIn("inst1.env", err)
        self.assertEqual(self.calls(), [])

    def test_dead_registry_entry_does_not_refuse(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"])
        with open(join(self.run_dir, "inst1.env"), "w") as f:
            f.write("SCHEMA=1\nPIDS=rtl_daq:999999,delay_sync:999998\n")
        rc, _, err = self.run_tool("--unit", "1")
        self.assertEqual(rc, 0, err)

    def test_running_rtl_daq_refusal(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"])
        open(join(self.state, "rtl_daq_running"), "w").close()
        rc, _, err = self.run_tool("--unit", "1", "--yes")
        self.assertEqual(rc, 3, err)
        self.assertIn("rtl_daq.out is running", err)
        with open(join(self.state, "pgrep.log")) as f:
            self.assertEqual(f.read().split(), ["-x", "rtl_daq.out"])

    def test_busy_receiver_refusal(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"], busy=[2])
        rc, _, err = self.run_tool("--unit", "1")
        self.assertEqual(rc, 3, err)
        self.assertIn("index 2 is busy", err)

    def test_non_contiguous_refusal_and_force(self):
        self.receivers(["1000", "1001", "1002", "1003", "1010"])
        rc, out, err = self.run_tool("--unit", "1", "--yes")
        self.assertEqual(rc, 6, out + err)
        self.assertIn("not 1000..1004", err)
        self.assertEqual(self.eeprom(), ["1000", "1001", "1002", "1003", "1010"])
        rc, out, err = self.run_tool("--unit", "1", "--force")
        self.assertEqual(rc, 0, err)
        self.assertEqual([self.plan_rows(out)[j][1] for j in range(5)], ["1100", "1101", "1102", "1103", "1110"])

    def test_duplicate_or_non_numeric_serials_need_by_index(self):
        self.receivers(["1000", "1000", "1002", "1003", "1004"])
        rc, _, err = self.run_tool("--unit", "1", "--force")
        self.assertEqual(rc, 6, err)
        self.assertIn("--by-index", err)
        shutil.rmtree(self.sysfs)
        os.makedirs(self.sysfs)
        self.receivers(["00000001", "ABC", "x", "y", "z"])
        rc, _, err = self.run_tool("--unit", "1")
        self.assertEqual(rc, 6, err)
        self.assertIn("not a decimal number", err)

    def test_missing_rtl_eeprom(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"])
        if any(os.access(join(d, "rtl_eeprom"), os.X_OK) for d in ("/usr/bin", "/bin")):
            self.skipTest("SKIP reason: a real rtl_eeprom is installed in /usr/bin or /bin")
        os.unlink(join(self.bin, "rtl_eeprom"))
        rc, _, err = self.run_tool("--unit", "1", path=self.bin + os.pathsep + "/usr/bin:/bin")
        self.assertEqual(rc, 5, err)
        self.assertIn("rtl_eeprom is not on PATH", err)

    def test_usage_errors(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"])
        for args in ([], ["--unit"], ["--unit", "x"], ["--unit", "1", "--count", "0"],
                     ["--unit", "1", "--count", "32"], ["--unit", "1", "--stride", "4"],
                     ["--list", "--unit", "1"], ["--bogus"], ["--unit", "1", "--base", "99999990"],
                     # more than 8 digits: refused before 64-bit arithmetic could wrap negative
                     ["--unit", "92233720368547759"], ["--unit", "1", "--base", "123456789"],
                     ["--unit", "1", "--stride", "184467440737095516"]):
            rc, _, err = self.run_tool(*args)
            self.assertEqual(rc, 2, (args, err))
        self.assertEqual(self.calls(), [])
        rc, _, err = self.run_tool("--unit", "92233720368547759")
        self.assertIn("at most 8 digits", err)

    # -- writing and verify -----------------------------------------------------

    def test_write_and_verify(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"])
        rc, out, err = self.run_tool("--unit", "1", "--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.eeprom(), ["1100", "1101", "1102", "1103", "1104"])
        writes = [c for c in self.calls() if "-s" in c.split()]
        self.assertEqual(writes, ["-d {0} -s {1}".format(j, 1100 + j) for j in range(5)])
        lines = out.strip().splitlines()
        self.assertEqual(lines[-2], "Power-cycle the unit, then set in daq_chain_config.ini: "
                                    "[hw] serial_base = 1100")
        self.assertEqual(lines[-1], "and [daq] ctr_channel_serial_no = 1100")

    def test_dry_run_wins_over_yes(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"])
        rc, out, err = self.run_tool("--unit", "1", "--yes", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.eeprom(), ["1000", "1001", "1002", "1003", "1004"])
        self.assertIn("Dry run", out)

    def test_already_programmed_receivers_are_not_rewritten(self):
        self.receivers(["1100", "1101", "1002", "1103", "1104"], sysfs_serials=["1100", "1101", "1002", "1103", "1104"])
        rc, out, err = self.run_tool("--unit", "1", "--by-index", "--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([c for c in self.calls() if "-s" in c.split()], ["-d 2 -s 1102"])
        self.assertEqual(self.eeprom(), ["1100", "1101", "1102", "1103", "1104"])

    def test_verify_failure_when_writes_are_ignored(self):
        self.receivers(["1000", "1001", "1002", "1003", "1004"])
        open(join(self.state, "ignore_writes"), "w").close()
        rc, out, err = self.run_tool("--unit", "1", "--yes")
        self.assertEqual(rc, 7, out + err)
        self.assertIn("verify: receiver 0 reads '1000'", err)
        self.assertNotIn("Power-cycle", out)

    # -- --list -----------------------------------------------------------------

    def test_list_table(self):
        # receiver 3 was rewritten but not power-cycled: its sysfs serial is still 1003
        self.receivers(["1000", "1001", "1002", "1103", "1004"], busy=[1],
                       sysfs_serials=["1000", "1001", "1002", "1003", "1004"])
        rc, out, err = self.run_tool("--list")
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["idx", "bus:dev", "sysfs-serial", "eeprom-serial", "state"])
        rows = {l.split()[0]: l.split() for l in lines[1:] if len(l.split()) == 5 and l.split()[0].isdigit()}
        self.assertEqual(rows["0"], ["0", "1:10", "1000", "1000", "free"])
        self.assertEqual(rows["1"], ["1", "-", "-", "-", "busy"])
        self.assertEqual(rows["3"], ["3", "-", "-", "1103", "free"])
        self.assertEqual(sorted(rows), ["0", "1", "2", "3", "4"])
        unmatched = [l.split() for l in lines if l.split()[:1] == ["-"]]
        self.assertEqual(sorted(u[2] for u in unmatched), ["1001", "1003"])
        self.assertTrue(all(u[-1] == "unmatched" for u in unmatched))
        self.assertIn("5 RTL2832U receiver(s) in sysfs, 5 in librtlsdr.", out)
        # --list never writes and needs no idle host
        self.assertFalse(any("-s" in c.split() for c in self.calls()))

    def test_list_without_receivers(self):
        self.receivers([])
        rc, out, err = self.run_tool("--list")
        self.assertEqual(rc, 0, err)
        self.assertIn("0 RTL2832U receiver(s) in sysfs, 0 in librtlsdr.", out)

    # -- lint ---------------------------------------------------------------------

    def test_bash_syntax_and_shellcheck(self):
        for script in (TOOL, join(REPO, "util", "kerberos_eeprom_init.sh")):
            res = subprocess.run([BASH, "-n", script], capture_output=True, text=True, timeout=30)
            self.assertEqual(res.returncode, 0, res.stderr)
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            self.skipTest("SKIP reason: shellcheck not installed (bash -n passed)")
        # K.2: clean at shellcheck's default severity (style included), not only -S warning
        res = subprocess.run([shellcheck, "-x", TOOL, join(REPO, "util", "kerberos_eeprom_init.sh")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
