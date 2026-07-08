"""
    Unit test for the RF front-end gain/noise-figure budget (gain_budget.py)
    and the antenna profile (antenna_profile.py).

    No sudo, no hardware.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import unittest
import sys
from os.path import join, dirname, realpath
from configparser import ConfigParser

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

import gain_budget as gb
from gain_budget import RfFrontendConfig, RfFrontendParser, P1DB_NOT_MODELLED
from antenna_profile import AntennaProfile, AntennaProfileParser


class TestDbHelpers(unittest.TestCase):
    def test_db_lin_roundtrip(self):
        for x in (-30.0, -3.0, 0.0, 6.0, 20.0):
            self.assertAlmostEqual(gb.lin_to_db(gb.db_to_lin(x)), x, places=6)

    def test_db_to_lin_known(self):
        self.assertAlmostEqual(gb.db_to_lin(0.0), 1.0, places=9)
        self.assertAlmostEqual(gb.db_to_lin(10.0), 10.0, places=6)

    def test_lin_to_db_nonpositive(self):
        self.assertEqual(gb.lin_to_db(0.0), float('-inf'))


class TestTotalGain(unittest.TestCase):
    def test_total_system_gain(self):
        # antenna 6 dBi - 2 dB feed + 20 dB LNA + 15 dB tuner = 39 dB
        self.assertAlmostEqual(
            gb.total_system_gain_db(6.0, 2.0, 20.0, 15.0), 39.0, places=6)

    def test_gain_after_unified_clamp(self):
        # Equal (clamped) tuner gains -> equal per-channel totals
        totals = [gb.total_system_gain_db(0.0, 0.0, 20.0, 12.0) for _ in range(5)]
        self.assertEqual(len(set(totals)), 1)


class TestFriisNf(unittest.TestCase):
    def test_lossy_first_stage_equals_loss(self):
        # A single passive loss stage yields F = loss (dB)
        self.assertAlmostEqual(gb.friis_nf_db([(-6.0, 6.0)]), 6.0, places=6)

    def test_high_gain_first_stage_dominates(self):
        # LNA (20 dB gain, 1 dB NF) then tuner (0 dB gain, 3.5 dB NF):
        # system NF is only slightly above the LNA NF.
        nf = gb.friis_nf_db([(20.0, 1.0), (0.0, 3.5)])
        self.assertLess(nf, 1.1)
        self.assertGreater(nf, 1.0)

    def test_lossy_feed_raises_nf(self):
        # 3 dB feed loss ahead of a 3.5 dB tuner -> ~6.5 dB
        nf = gb.system_nf_db(3.0, 0.0, 0.0, 0.0, 3.5)
        self.assertAlmostEqual(nf, 6.5, places=6)

    def test_system_nf_lna_dominant(self):
        # feed 1 dB, LNA (25 dB, 0.8 dB NF), tuner (10 dB, 3.5 dB NF)
        nf = gb.system_nf_db(1.0, 25.0, 0.8, 10.0, 3.5)
        # Feed loss adds ~1 dB in front of a ~0.8 dB LNA-dominated chain
        self.assertLess(nf, 2.0)
        self.assertGreater(nf, 1.5)

    def test_empty_stages(self):
        self.assertEqual(gb.friis_nf_db([]), 0.0)


class TestP1dbHeadroom(unittest.TestCase):
    def test_positive_headroom(self):
        # input -60 dBm, +20 dB gain -> -40 dBm output, P1dB +10 dBm -> 50 dB headroom
        self.assertAlmostEqual(
            gb.p1db_headroom_db(-60.0, 20.0, 10.0), 50.0, places=6)

    def test_compression_flag(self):
        cfg = RfFrontendConfig(
            enabled=True,
            ext_lna_gains_db=[70.0],
            ext_lna_nf_db=[1.0],
            ext_lna_p1db_dbm=[0.0],
            expected_input_dbm=-10.0,
            p1db_margin_db=3.0,
        )
        b = cfg.channel_budget(0, tuner_gain_db=0.0)
        # -10 dBm input + 70 dB gain = +60 dBm >> 0 dBm P1dB -> compressed
        self.assertTrue(b["compressed"])

    def test_p1db_not_modelled_never_compresses(self):
        cfg = RfFrontendConfig(
            enabled=True,
            ext_lna_gains_db=[20.0],
            ext_lna_nf_db=[1.0],
            ext_lna_p1db_dbm=[P1DB_NOT_MODELLED],
        )
        b = cfg.channel_budget(0, tuner_gain_db=0.0)
        self.assertFalse(b["compressed"])


class TestLnaFreqTable(unittest.TestCase):
    def test_empty_table(self):
        self.assertEqual(gb.lookup_lna_response([], 700e6), (0.0, 0.0))

    def test_parse_and_lookup(self):
        table = gb.parse_lna_freq_table("100:10:1;700:20:1.5;1700:15:2")
        self.assertEqual(len(table), 3)
        g, n = gb.lookup_lna_response(table, 710e6)
        self.assertEqual((g, n), (20.0, 1.5))

    def test_malformed_entries_skipped(self):
        table = gb.parse_lna_freq_table("100:10:1;garbage;700:20:1.5")
        self.assertEqual(len(table), 2)


class TestRfFrontendParser(unittest.TestCase):
    def _parser(self, extra=""):
        p = ConfigParser()
        p.read_string(
            "[amplification]\n"
            "en_amplification = 1\n"
            "ext_lna_gains_db = 20,20,20,20,20\n"
            "ext_lna_nf_db = 1,1,1,1,1\n"
            "ext_lna_p1db_dbm = 10,10,10,10,10\n"
            "tuner_nf_db = 3.5\n"
            "max_total_gain_db = 80\n" + extra)
        return p

    def test_parse_ok(self):
        cfg = RfFrontendParser.from_ini_section(self._parser(), num_ch=5)
        self.assertIsNotNone(cfg)
        self.assertTrue(cfg.enabled)
        self.assertEqual(len(cfg.ext_lna_gains_db), 5)
        self.assertEqual(cfg.ext_lna_gains_db[0], 20.0)

    def test_disabled_section_returns_none(self):
        p = ConfigParser()
        p.read_string("[amplification]\nen_amplification = 0\n")
        self.assertIsNone(RfFrontendParser.from_ini_section(p, num_ch=5))

    def test_missing_section_returns_none(self):
        p = ConfigParser()
        p.read_string("[daq]\ngain = 0\n")
        self.assertIsNone(RfFrontendParser.from_ini_section(p, num_ch=5))

    def test_length_mismatch_falls_back(self):
        # Only 3 values but num_ch=5 -> neutral fallback, no exception
        p = ConfigParser()
        p.read_string(
            "[amplification]\nen_amplification = 1\n"
            "ext_lna_gains_db = 20,20,20\n")
        cfg = RfFrontendParser.from_ini_section(p, num_ch=5)
        self.assertEqual(cfg.ext_lna_gains_db, [0.0] * 5)

    def test_over_max_gain_flag(self):
        cfg = RfFrontendConfig(
            enabled=True, ext_lna_gains_db=[60.0], ext_lna_nf_db=[1.0],
            ext_lna_p1db_dbm=[P1DB_NOT_MODELLED], max_total_gain_db=50.0)
        b = cfg.channel_budget(0, tuner_gain_db=30.0)  # 90 dB total > 50
        self.assertTrue(b["over_max_gain"])


class TestAntennaProfile(unittest.TestCase):
    def test_disabled_returns_none(self):
        p = ConfigParser()
        p.read_string("[antenna]\nen_antenna_profile = 0\n")
        self.assertIsNone(AntennaProfileParser.from_ini_section(p))

    def test_parse_and_feed_loss(self):
        p = ConfigParser()
        p.read_string(
            "[antenna]\nen_antenna_profile = 1\n"
            "profile_name = yagi\nelement_gain_dbi = 12\n"
            "beamwidth_az_deg = 40\npolarization = horizontal\n"
            "cable_loss_db = 2.5\nconnector_loss_db = 0.5\n"
            "boresight_az_offset_deg = 3\n")
        prof = AntennaProfileParser.from_ini_section(p)
        self.assertEqual(prof.element_gain_dbi, 12.0)
        self.assertEqual(prof.feed_loss_db, 3.0)
        self.assertEqual(prof.polarization, 'horizontal')

    def test_boresight_applied(self):
        prof = AntennaProfile(boresight_az_offset_deg=5.0, boresight_el_offset_deg=-2.0)
        az, el = prof.apply_boresight(100.0, 10.0)
        self.assertEqual((az, el), (105.0, 8.0))

    def test_invalid_polarization_defaults(self):
        p = ConfigParser()
        p.read_string(
            "[antenna]\nen_antenna_profile = 1\npolarization = circular_bogus\n")
        prof = AntennaProfileParser.from_ini_section(p)
        self.assertEqual(prof.polarization, 'vertical')

    def test_budget_with_antenna(self):
        cfg = RfFrontendConfig(
            enabled=True, ext_lna_gains_db=[20.0], ext_lna_nf_db=[1.0],
            ext_lna_p1db_dbm=[P1DB_NOT_MODELLED], tuner_nf_db=3.5)
        prof = AntennaProfile(element_gain_dbi=12.0, cable_loss_db=2.0)
        b = cfg.channel_budget(0, tuner_gain_db=15.0, antenna_profile=prof)
        # 12 - 2 + 20 + 15 = 45 dB
        self.assertAlmostEqual(b["total_gain_db"], 45.0, places=6)


if __name__ == "__main__":
    unittest.main()
