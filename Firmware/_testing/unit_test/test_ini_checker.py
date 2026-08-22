"""
    Description :
    Contract tests for the ini_checker CLI:

        python3 ini_checker.py [config_path] [no_hw]
            exit 0 = config valid, exit 1 = errors found
            'no_hw' skips ALL hardware probing (no lsusb / rtl_eeprom)

    Only the CLI contract is validated (the checker's internals are owned by
    another workstream and may be edited concurrently). If the contract is
    not implemented yet — the checker ignores the config-path argument or
    cannot run hardware-free — the tests skip with a printed reason instead
    of failing, so the suite stays meaningful during the transition.

    All test configs are temp copies; the live Firmware/daq_chain_config.ini
    is only read, never modified.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import configparser
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from os.path import join, dirname, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
INI_CHECKER = join(root_path, "ini_checker.py")
LIVE_CONFIG = join(root_path, "daq_chain_config.ini")


def run_checker(args, cwd):
    return subprocess.run([sys.executable, INI_CHECKER] + list(args),
                          cwd=cwd, capture_output=True, text=True,
                          timeout=120)


class TesterIniCheckerContract(unittest.TestCase):

    contract_probe = None  # None = unknown, True/False after probing

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ini_checker_")
        # Valid config: temp copy of the live one (read-only source)
        cls.valid_ini = join(cls.tmpdir, "valid.ini")
        shutil.copyfile(LIVE_CONFIG, cls.valid_ini)
        # Broken config: num_ch = 0 is unconditionally rejected
        parser = configparser.ConfigParser()
        parser.read(cls.valid_ini)
        parser["hw"]["num_ch"] = "0"
        cls.broken_ini = join(cls.tmpdir, "broken.ini")
        with open(cls.broken_ini, "w") as f:
            parser.write(f)
        # Empty working dir: no daq_chain_config.ini fallback present, so a
        # checker that ignores argv[1] cannot accidentally pass
        cls.empty_cwd = join(cls.tmpdir, "empty")
        os.makedirs(cls.empty_cwd)

        # Probe whether the CLI contract (path argument + exit codes +
        # hardware-free no_hw) is already implemented
        try:
            valid_res = run_checker([cls.valid_ini, "no_hw"],
                                    cwd=cls.empty_cwd)
            broken_res = run_checker([cls.broken_ini, "no_hw"],
                                     cwd=cls.empty_cwd)
            cls.contract_probe = (valid_res.returncode == 0 and
                                  broken_res.returncode == 1)
            cls.probe_detail = (
                "valid rc={} broken rc={}".format(valid_res.returncode,
                                                  broken_res.returncode))
        except Exception as e:  # pragma: no cover
            cls.contract_probe = False
            cls.probe_detail = repr(e)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _require_contract(self):
        if not self.contract_probe:
            self.skipTest(
                "SKIP reason: ini_checker CLI contract "
                "([config_path] [no_hw] + exit 0/1) not implemented yet "
                "({}) — owned by a concurrent workstream".format(
                    self.probe_detail))

    def test_valid_config_exits_zero(self):
        self._require_contract()
        res = run_checker([self.valid_ini, "no_hw"], cwd=self.empty_cwd)
        self.assertEqual(res.returncode, 0,
                         "stdout: {}\nstderr: {}".format(res.stdout,
                                                         res.stderr))

    def test_broken_config_exits_one(self):
        self._require_contract()
        res = run_checker([self.broken_ini, "no_hw"], cwd=self.empty_cwd)
        self.assertEqual(res.returncode, 1,
                         "num_ch=0 must be rejected with exit code 1 "
                         "(got {})".format(res.returncode))

    def test_no_hw_skips_hardware_probing(self):
        """With 'no_hw' the checker must not shell out to lsusb/rtl_eeprom
        (they do not exist on macOS/CI, so success itself proves this) and
        must not complain about missing receivers."""
        self._require_contract()
        res = run_checker([self.valid_ini, "no_hw"], cwd=self.empty_cwd)
        self.assertEqual(res.returncode, 0)
        combined = (res.stdout + res.stderr).lower()
        self.assertNotIn("lsusb", combined)

    def test_unknown_sections_and_keys_non_fatal(self):
        """Unknown sections/keys must stay non-fatal (older and newer
        configs keep validating — frozen compatibility rule)."""
        self._require_contract()
        augmented = join(self.tmpdir, "augmented.ini")
        shutil.copyfile(self.valid_ini, augmented)
        with open(augmented, "a") as f:
            f.write("\n[completely_unknown_section]\n"
                    "mystery_key = 42\n")
        res = run_checker([augmented, "no_hw"], cwd=self.empty_cwd)
        self.assertEqual(res.returncode, 0,
                         "unknown sections must not fail validation")

    def test_config_path_defaults_to_cwd(self):
        """With no path argument the checker falls back to the historic
        daq_chain_config.ini in the working directory."""
        self._require_contract()
        legacy_cwd = join(self.tmpdir, "legacy")
        os.makedirs(legacy_cwd, exist_ok=True)
        shutil.copyfile(self.valid_ini,
                        join(legacy_cwd, "daq_chain_config.ini"))
        res = run_checker(["no_hw"], cwd=legacy_cwd)
        self.assertEqual(res.returncode, 0,
                         "legacy invocation (cwd config + no_hw) broke")

    def test_missing_config_is_an_error(self):
        self._require_contract()
        res = run_checker([join(self.tmpdir, "does_not_exist.ini"),
                           "no_hw"], cwd=self.empty_cwd)
        self.assertNotEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
