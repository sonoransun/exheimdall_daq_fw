"""
    Description :
    Unit tests for fir_filter_designer.py — the startup-critical script that
    regenerates _data_control/fir_coeffs.txt on every DAQ start.

    The script reads daq_chain_config.ini from its working directory, so each
    test builds a minimal config in an isolated temp directory and runs the
    designer there as a subprocess (the live Firmware/daq_chain_config.ini is
    never touched).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import os
import subprocess
import sys
import tempfile
import unittest
from os.path import join, dirname, realpath

import numpy as np
from scipy import signal

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
DESIGNER = join(root_path, "fir_filter_designer.py")


def write_config(tmpdir, decimation_ratio, fir_bw, tap_size, window):
    with open(join(tmpdir, "daq_chain_config.ini"), "w") as f:
        f.write(
            "[daq]\n"
            "sample_rate = 2400000\n"
            "center_freq = 433000000\n"
            "\n"
            "[pre_processing]\n"
            "decimation_ratio = {:d}\n"
            "fir_relative_bandwidth = {:f}\n"
            "fir_tap_size = {:d}\n"
            "fir_window = {:s}\n".format(
                decimation_ratio, fir_bw, tap_size, window))


class TesterFirFilterDesigner(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fir_designer_")
        os.makedirs(join(self.tmpdir, "_data_control"))
        os.makedirs(join(self.tmpdir, "_logs"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self):
        return subprocess.run([sys.executable, DESIGNER],
                              cwd=self.tmpdir, capture_output=True,
                              text=True, timeout=120)

    def _coeffs(self):
        path = join(self.tmpdir, "_data_control", "fir_coeffs.txt")
        self.assertTrue(os.path.exists(path),
                        "designer did not write fir_coeffs.txt")
        return np.atleast_1d(np.loadtxt(path))

    def test_tap_count_matches_config(self):
        """Coefficient count equals [pre_processing] fir_tap_size — the C
        decimator loads exactly tap_size coefficients (startup invariant)."""
        write_config(self.tmpdir, decimation_ratio=8, fir_bw=1.0,
                     tap_size=64, window="hann")
        res = self._run()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        coeffs = self._coeffs()
        self.assertEqual(len(coeffs), 64)

    def test_unity_dc_gain(self):
        """firwin(scale=True) normalizes the passband: DC gain must be 1."""
        write_config(self.tmpdir, decimation_ratio=4, fir_bw=0.8,
                     tap_size=48, window="hann")
        res = self._run()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        coeffs = self._coeffs()
        self.assertAlmostEqual(float(np.sum(coeffs)), 1.0, places=6)

    def test_stopband_attenuation(self):
        """The realized filter must attenuate the stopband (aliasing
        protection for the decimator)."""
        write_config(self.tmpdir, decimation_ratio=8, fir_bw=1.0,
                     tap_size=128, window="hann")
        res = self._run()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        coeffs = self._coeffs()
        w, h = signal.freqz(coeffs, worN=4096)
        # Well beyond the cutoff (1/8 Nyquist here): expect > 60 dB rejection
        stop = np.abs(h[w > 0.5 * np.pi])
        self.assertLess(20 * np.log10(np.max(stop) + 1e-12), -60.0)

    def test_passthrough_when_no_decimation(self):
        """decimation_ratio=1 with full bandwidth degenerates to a single
        unity coefficient (no filtering)."""
        write_config(self.tmpdir, decimation_ratio=1, fir_bw=1.0,
                     tap_size=1, window="hann")
        res = self._run()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        coeffs = self._coeffs()
        self.assertEqual(len(coeffs), 1)
        self.assertAlmostEqual(float(coeffs[0]), 1.0, places=12)

    def test_matches_reference_firwin(self):
        """The generated coefficients are exactly scipy.signal.firwin with
        cutoff = fir_relative_bandwidth / decimation_ratio (what the
        decimator unit tests' transfer checker assumes)."""
        R, bw, K, win = 5, 0.9, 40, "blackmanharris"
        write_config(self.tmpdir, decimation_ratio=R, fir_bw=bw,
                     tap_size=K, window=win)
        res = self._run()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        coeffs = self._coeffs()
        reference = signal.firwin(K, bw / R, window=win)
        np.testing.assert_allclose(coeffs, reference, rtol=0, atol=1e-12)

    def test_rejects_tap_size_not_above_decimation(self):
        """tap_size <= decimation_ratio (with R > 1) must fail with a
        nonzero exit code so startup aborts loudly."""
        write_config(self.tmpdir, decimation_ratio=8, fir_bw=1.0,
                     tap_size=8, window="hann")
        res = self._run()
        self.assertNotEqual(res.returncode, 0)

    def test_rejects_decimation_below_one(self):
        write_config(self.tmpdir, decimation_ratio=0, fir_bw=1.0,
                     tap_size=16, window="hann")
        res = self._run()
        self.assertNotEqual(res.returncode, 0)

    def test_missing_config_fails(self):
        # No daq_chain_config.ini written in the temp cwd
        res = self._run()
        self.assertNotEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
