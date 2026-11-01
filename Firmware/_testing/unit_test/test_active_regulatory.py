"""
    Unit tests for the active regulatory policy (Firmware/_active/active_regulatory.py).

    Hardware-free and deterministic.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import math
import sys
import unittest
from os.path import dirname, join, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_regulatory as R  # noqa: E402

ALLOW_ISM = [(902e6, 928e6)]


def codes(issues, level="error"):
    return [i.code for i in issues if i.level == level]


class TestIntervals(unittest.TestCase):
    def test_occupied_and_lo_without_offset(self):
        iv = R.tx_frequency_intervals(915e6, 2e6, 0.0, 4e6)
        self.assertEqual([x[2] for x in iv], ["occupied", "lo"])
        self.assertEqual(iv[0][:2], (914e6, 916e6))
        self.assertEqual(iv[1][:2], (915e6 - 1e3, 915e6 + 1e3))

    def test_occupied_zero_means_sample_rate(self):
        iv = R.tx_frequency_intervals(915e6, 0.0, 0.0, 4e6)
        self.assertEqual(iv[0][:2], (913e6, 917e6))

    def test_offset_adds_both_lo_conventions_and_images(self):
        iv = {x[2]: x[:2] for x in R.tx_frequency_intervals(915e6, 2e6, 5e6, 4e6)}
        self.assertEqual(set(iv), {"occupied", "lo", "lo_alt", "image", "image_alt"})
        self.assertAlmostEqual(iv["lo"][0], 910e6 - 1e3)       # spec: fc - lo_offset
        self.assertAlmostEqual(iv["lo_alt"][1], 920e6 + 1e3)   # UHD tune_request: fc + lo_off
        self.assertEqual(iv["image"], (904e6, 906e6))          # 2*LO - fc, same width
        self.assertEqual(iv["image_alt"], (924e6, 926e6))

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            R.tx_frequency_intervals(-1.0, 1e6, 0, 1e6)
        with self.assertRaises(ValueError):
            R.tx_frequency_intervals(915e6, 0.0, 0.0, 0.0)
        with self.assertRaises(ValueError):
            R.tx_frequency_intervals(float("nan"), 1e6, 0, 1e6)


class TestBandCheck(unittest.TestCase):
    def test_ism_915_allowed(self):
        iv = R.tx_frequency_intervals(915e6, 2e6, 0.0, 2e6)
        for mode in ("dummy_load", "cabled", "radiate"):
            self.assertEqual(R.band_check(iv, mode, ALLOW_ISM, ()), [])

    def test_gps_l1_denied_in_every_mode(self):
        iv = R.tx_frequency_intervals(1575.42e6, 1e6, 0.0, 1e6)
        allow = [(1500e6, 1700e6)]
        for mode in ("dummy_load", "cabled", "radiate"):
            iss = R.band_check(iv, mode, allow, ())
            self.assertIn("FREQ_DENIED", codes(iss), mode)
            self.assertNotIn("FREQ_NOT_ALLOWED", codes(iss), mode)
            self.assertTrue(any("GNSS" in i.message for i in iss))

    def test_conducted_override_only_in_conducted_modes(self):
        iv = R.tx_frequency_intervals(1575.42e6, 1e6, 0.0, 1e6)
        allow = [(1500e6, 1700e6)]
        for mode in ("dummy_load", "cabled"):
            iss = R.band_check(iv, mode, allow, (), ignore_builtin=True)
            self.assertEqual(codes(iss), [], mode)
            self.assertIn("FREQ_DENIED", codes(iss, "warning"), mode)
        iss = R.band_check(iv, "radiate", allow, (), ignore_builtin=True)
        self.assertIn("FREQ_DENIED", codes(iss))

    def test_user_deny_band_never_overridable(self):
        iv = R.tx_frequency_intervals(915e6, 2e6, 0.0, 2e6)
        iss = R.band_check(iv, "dummy_load", ALLOW_ISM, [(915.5e6, 915.6e6)], ignore_builtin=True)
        self.assertEqual(codes(iss), ["FREQ_DENIED"])

    def test_occupied_band_crossing_allow_edge(self):
        iv = R.tx_frequency_intervals(927.5e6, 2e6, 0.0, 2e6)
        iss = R.band_check(iv, "dummy_load", ALLOW_ISM, ())
        self.assertEqual(codes(iss), ["FREQ_NOT_ALLOWED"])
        self.assertIn("occupied", iss[0].message)

    def test_band_exactly_at_allow_edges_is_inside(self):
        iv = R.tx_frequency_intervals(915e6, 26e6, 0.0, 26e6)    # 902..928 exactly
        self.assertEqual(codes(R.band_check(iv, "cabled", ALLOW_ISM, ())), [])

    def test_empty_allow_list_allows_nothing(self):
        iv = R.tx_frequency_intervals(915e6, 2e6, 0.0, 2e6)
        iss = R.band_check(iv, "dummy_load", (), ())
        self.assertIn("FREQ_NOT_ALLOWED", codes(iss))
        self.assertIn("empty", iss[0].message)

    def test_lo_offset_pushes_lo_into_deny_band(self):
        # occupied band clean, LO leakage carrier lands in a configured deny band
        deny = [(904e6, 906e6)]
        clean = R.tx_frequency_intervals(915e6, 2e6, 0.0, 2e6)
        self.assertEqual(codes(R.band_check(clean, "dummy_load", ALLOW_ISM, deny)), [])
        iv = R.tx_frequency_intervals(915e6, 2e6, 10e6, 2e6)     # LO at 905 MHz
        iss = R.band_check(iv, "dummy_load", ALLOW_ISM, deny)
        self.assertIn("FREQ_DENIED", codes(iss))
        self.assertTrue(any(i.code == "FREQ_DENIED" and i.message.startswith("lo ") for i in iss))

    def test_builtin_deny_reached_through_lo(self):
        # fc 1310 MHz is clean, LO = 1310 - 15 = 1295 MHz is in 1215-1300 (GNSS L2 / radar)
        iv = R.tx_frequency_intervals(1310e6, 2e6, 15e6, 2e6)
        iss = R.band_check(iv, "dummy_load", [(1200e6, 1400e6)], ())
        self.assertIn("FREQ_DENIED", codes(iss))
        self.assertEqual(codes(R.band_check(R.tx_frequency_intervals(1310e6, 2e6, 0, 2e6), "dummy_load",
                                            [(1300e6, 1400e6)], ())), [])

    def test_image_band_checked(self):
        ok = R.tx_frequency_intervals(915e6, 2e6, 6e6, 2e6)       # images 903 / 927 +- 1 MHz
        self.assertEqual(codes(R.band_check(ok, "cabled", ALLOW_ISM, ())), [])
        bad = R.tx_frequency_intervals(915e6, 2e6, 7e6, 2e6)      # image 901 +- 1 MHz
        iss = R.band_check(bad, "cabled", ALLOW_ISM, ())
        self.assertIn("FREQ_NOT_ALLOWED", codes(iss))
        self.assertTrue(any(i.message.startswith("image") for i in iss))

    def test_builtin_list_shape(self):
        self.assertIsInstance(R.BUILTIN_DENY_BANDS_HZ, tuple)
        for lo, hi, label in R.BUILTIN_DENY_BANDS_HZ:
            self.assertLess(lo, hi)
            self.assertTrue(label)
        self.assertTrue(R.builtin_deny_hits(121.4e6, 121.6e6))
        self.assertTrue(R.builtin_deny_hits(1575e6, 1576e6))
        self.assertEqual(R.builtin_deny_hits(902e6, 928e6), [])


class TestMpe(unittest.TestCase):
    def test_limits(self):
        self.assertAlmostEqual(R.mpe_limit_w_m2(10e6), 18.0)
        self.assertAlmostEqual(R.mpe_limit_w_m2(100e6), 2.0)
        self.assertAlmostEqual(R.mpe_limit_w_m2(900e6), 6.0)
        self.assertAlmostEqual(R.mpe_limit_w_m2(2400e6), 10.0)
        # continuous at the segment boundaries
        self.assertAlmostEqual(R.mpe_limit_w_m2(30e6), 2.0)
        self.assertAlmostEqual(R.mpe_limit_w_m2(300e6), 2.0)
        self.assertAlmostEqual(R.mpe_limit_w_m2(1500e6), 10.0)

    def test_below_3mhz_and_above_100ghz(self):
        with self.assertRaises(ValueError):
            R.mpe_limit_w_m2(2.9e6)
        with self.assertRaises(ValueError):
            R.mpe_distance_m(30.0, 1e6)
        with self.assertRaises(ValueError):
            R.mpe_limit_w_m2(101e9)

    def test_band_minimum(self):
        self.assertAlmostEqual(R.mpe_limit_band_w_m2(290e6, 310e6), 2.0)
        self.assertAlmostEqual(R.mpe_limit_band_w_m2(902e6, 928e6), 902.0 / 150.0)
        self.assertAlmostEqual(R.mpe_limit_band_w_m2(20e6, 40e6), 2.0)

    def test_distance_formula(self):
        # 60 dBm = 1 kW EIRP at 900 MHz: R = sqrt(1000 / (4 pi 6))
        r = R.mpe_distance_m(60.0, 900e6)
        self.assertAlmostEqual(r, math.sqrt(1000.0 / (4 * math.pi * 6.0)), places=9)
        self.assertAlmostEqual(R.mpe_distance_m(60.0, 900e6, duty=0.1), r * math.sqrt(0.1), places=9)
        self.assertEqual(R.mpe_distance_m(float("-inf"), 900e6), 0.0)
        self.assertAlmostEqual(R.power_density_w_m2(60.0, r), 6.0, places=9)
        with self.assertRaises(ValueError):
            R.mpe_distance_m(30.0, 900e6, duty=1.5)


class TestText(unittest.TestCase):
    def test_warning_text(self):
        w = R.REGULATORY_WARNING
        for phrase in ("not legal advice", "Array gain counts toward EIRP",
                       "not a compliant intentional radiator", "jurisdiction"):
            self.assertIn(phrase, w)

    def test_banner(self):
        iss = R.band_check(R.tx_frequency_intervals(915e6, 2e6, 0, 2e6), "radiate", ALLOW_ISM, ())
        b = R.regulatory_banner("radiate", 915e6, "KA1ABC licence", "amateur", "KA1ABC", iss,
                                eirp_dbm=33.0, eirp_limit_dbm=36.0, mpe_m=0.4, mpe_exclusion_m=2.0,
                                station_id_interval_s=600, simulated=True)
        for s in (R.REGULATORY_WARNING, "KA1ABC", "SIMULATED", "33.00 dBm", "36.00 dBm", "0.40 m",
                  "915.000000 MHz", "600"):
            self.assertIn(s, b)
        c = R.regulatory_banner("dummy_load", 915e6)
        self.assertIn("dummy loads", c)


if __name__ == "__main__":
    unittest.main()
