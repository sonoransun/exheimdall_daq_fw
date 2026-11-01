"""
    Unit tests for the active RF front-end model (Firmware/_active/active_frontend.py).

    Hardware-free and deterministic.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import logging
import math
import os
import sys
import tempfile
import unittest
from os.path import dirname, join, realpath

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_config as C  # noqa: E402
import active_devices as DEV  # noqa: E402
import active_frontend as F  # noqa: E402
from active_common import C0, ConfigError  # noqa: E402
from antenna_profile import AntennaProfile  # noqa: E402
from gain_budget import friis_nf_db, total_system_gain_db  # noqa: E402

logging.getLogger("antenna_profile").setLevel(logging.ERROR)
logging.getLogger("heimdall.active").setLevel(logging.CRITICAL + 10)

TX_EXAMPLE = "amp:driver:20:5:20:23;pa:main:17::37:40::8;isolator:iso:-0.5;coupler:cpl40:-0.3;switch:tr:-0.8"
RX_EXAMPLE = "limiter:lim:-0.5:0.5:::30;switch:tr:-0.8;lna:lna1:20:0.9:15::10;cable:c1:-1.2"


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class TestGrammar(unittest.TestCase):
    def test_example_chains(self):
        tx = F.parse_chain(TX_EXAMPLE, "tx")
        self.assertEqual([s.kind for s in tx], ["amp", "pa", "isolator", "coupler", "switch"])
        pa = tx[1]
        self.assertEqual((pa.name, pa.gain_db, pa.nf_db, pa.p1db_out_dbm, pa.psat_dbm, pa.max_in_dbm,
                          pa.am_pm_deg_at_p1db), ("main", 17.0, None, 37.0, 40.0, None, 8.0))
        self.assertEqual(tx[2].nf_db, 0.5)                       # passive NF defaults to the loss
        rx = F.parse_chain(RX_EXAMPLE, "rx")
        self.assertEqual(rx[0].max_in_dbm, 30.0)
        self.assertEqual(rx[2].max_in_dbm, 10.0)
        self.assertEqual(rx[2].p1db_out_dbm, 15.0)

    def test_empty_fields_and_empty_spec(self):
        s = F.parse_chain("pa:main:30:::40:10:5", "tx")[0]
        self.assertEqual((s.nf_db, s.p1db_out_dbm, s.psat_dbm, s.max_in_dbm, s.am_pm_deg_at_p1db),
                         (None, None, 40.0, 10.0, 5.0))
        self.assertEqual(F.parse_chain("", "rx"), ())
        self.assertEqual(F.parse_chain(" ; ", "tx"), ())
        self.assertEqual(len(F.parse_chain("amp:a:10:3:::::", "tx")), 1)   # trailing empty fields
        self.assertEqual(F.parse_chain("cable:c:-1:1:::::", "rx")[0].nf_db, 1.0)

    def test_round_trip(self):
        st = F.parse_chain(TX_EXAMPLE, "tx")
        self.assertEqual(F.parse_chain(F.chain_to_spec(st), "tx"), st)

    def test_errors(self):
        bad = [("pa:x:10", "rx"), ("limiter:x:-1", "tx"), ("lna:x:20", "tx"), ("amp:x", "tx"),
               ("amp:x::3", "tx"), ("amp:x:abc", "tx"), ("cable:c:1.5", "tx"), ("amp:a:10:3:20:19", "tx"),
               ("amp:a:1:2:3:4:5:6:7", "tx"), ("amp:a:10;amp:a:12", "tx"), ("amp:a:10:-1", "tx"),
               ("pad:p:-3::10", "tx"), ("bogus:b:1", "rx"),
               ("amp:a:inf", "tx"), ("cable:c:-1::::: 3", "rx")]
        for spec, d in bad:
            with self.assertRaises(ConfigError, msg=spec) as cm:
                F.parse_chain(spec, d)
            self.assertEqual(cm.exception.issues[0].code, "CHAIN", spec)
        with self.assertRaises(ValueError):
            F.parse_chain("", "both")


class TestRapp(unittest.TestCase):
    def test_p1db_is_computed(self):
        for p in (1.0, 2.0, 3.0):
            pa = F.RappPA(30.0, 40.0, p)
            r = (10 ** (p / 10) - 1) ** (1 / (2 * p))
            self.assertAlmostEqual(pa.p1db_out_dbm(), 40.0 + 20 * math.log10(r) - 1.0, places=6)
            self.assertAlmostEqual(pa.compression_db(pa.p1db_in_dbm()), 1.0, places=6)
        pa = F.RappPA(30.0, 40.0, 2.0)
        # "approximately Psat - 1.5 dB": the exact Rapp p=2 value is Psat - 2.17 dB
        self.assertTrue(40.0 - 2.5 < pa.p1db_out_dbm() < 40.0 - 1.0)
        self.assertAlmostEqual(pa.p1db_out_dbm() - 40.0, F.rapp_p1db_offset_db(2.0), places=6)

    def test_small_signal_and_saturation(self):
        pa = F.RappPA(20.0, 30.0)
        self.assertAlmostEqual(pa.output_dbm(-40.0), -20.0, places=6)
        self.assertLess(pa.output_dbm(30.0), 30.0)
        self.assertGreater(pa.output_dbm(30.0), 29.9)
        self.assertEqual(pa.output_dbm(float("-inf")), float("-inf"))
        v = np.sqrt(F._w(np.array([-40.0, 0.0, 30.0])[0]))
        out = pa.apply(np.array([v, -v, 1j * v]))
        self.assertTrue(np.allclose(np.abs(out) ** 2, F._w(-20.0), rtol=1e-6))

    def test_am_pm_grows_with_drive(self):
        pa = F.RappPA(20.0, 30.0, 2.0, am_pm_deg_at_p1db=8.0)
        lv = [-30.0, -15.0, -10.0, -5.0, 0.0, 10.0]
        ph = [pa.am_pm_deg(x) for x in lv]
        self.assertTrue(all(b > a for a, b in zip(ph, ph[1:])))
        self.assertAlmostEqual(pa.am_pm_deg(pa.p1db_in_dbm()), 8.0, places=6)
        amps = np.sqrt([F._w(x) for x in lv])
        phases = np.degrees(np.angle(pa.apply(amps)))
        self.assertTrue(np.allclose(phases, ph, atol=1e-9))

    def test_p1db_only_stage_reproduces_datasheet(self):
        s = F.parse_chain("pa:p:20::30", "tx")[0]
        self.assertAlmostEqual(s.effective_psat_dbm(), 30.0 - F.rapp_p1db_offset_db(2.0))
        self.assertAlmostEqual(s.model().p1db_out_dbm(), 30.0, places=6)
        self.assertAlmostEqual(s.effective_p1db_dbm(), 30.0, places=6)
        both = F.parse_chain("pa:p:20::35:36", "tx")[0]          # datasheet above the model
        self.assertAlmostEqual(both.effective_p1db_dbm(), F.RappPA(20, 36).p1db_out_dbm(), places=6)


class TestRxChain(unittest.TestCase):
    def test_nf_reuses_gain_budget(self):
        st = F.parse_chain(RX_EXAMPLE, "rx")
        rx = F.RxChain(st, 6.0, -10.0)
        b = rx.budget(915e6, -60.0, 30.0)
        expect = friis_nf_db([(s.gain_db, s.effective_nf_db()) for s in st] + [(30.0, 6.0)])
        self.assertAlmostEqual(b["nf_db"], expect, places=12)
        self.assertAlmostEqual(b["ext_gain_db"], -0.5 - 0.8 + 20 - 1.2)
        self.assertAlmostEqual(b["total_gain_db"], total_system_gain_db(0.0, 0.0, 17.5, 30.0))
        self.assertFalse(b["compressed"])
        self.assertEqual(len(b["headroom_db"]), 1)
        self.assertAlmostEqual(b["headroom_db"][0], 15.0 - (-60.0 - 1.3 + 20.0), places=9)

    def test_antenna_profile_in_budget(self):
        ant = AntennaProfile(element_gain_dbi=5.0, cable_loss_db=1.0, connector_loss_db=0.5)
        rx = F.RxChain(F.parse_chain("lna:l:20:1:15::10", "rx"), 6.0, -10.0, ant)
        b = rx.budget(915e6, -60.0, 10.0)
        self.assertAlmostEqual(b["total_gain_db"], total_system_gain_db(5.0, 1.5, 20.0, 10.0))
        self.assertAlmostEqual(b["nf_db"], friis_nf_db([(-1.5, 1.5), (20.0, 1.0), (10.0, 6.0)]))

    def test_damage_input(self):
        rx = F.RxChain(F.parse_chain("lna:l1:20:1:15::10", "rx"), 6.0, 10.0)
        self.assertAlmostEqual(rx.damage_input_dbm(), -10.0)          # device port: 10 - 20
        rx2 = F.RxChain(F.parse_chain("limiter:lim:-0.5:0.5:::30;lna:l1:20:1:15::10", "rx"), 6.0, 10.0)
        self.assertAlmostEqual(rx2.damage_input_dbm(), 10.0 - 19.5)
        # LNA off: gain replaced by -isolation; its own max_in (10.5 at the antenna) still applies
        self.assertAlmostEqual(rx2.damage_input_dbm(lna_on=False, lna_off_isolation_db=20.0), 10.5)
        self.assertAlmostEqual(rx2.damage_input_dbm(lna_on=False, lna_off_isolation_db=0.0), -9.5)
        self.assertAlmostEqual(rx2.safe_input_dbm(10.0), -19.5)
        bare = F.RxChain((), 6.0, -10.0)
        self.assertAlmostEqual(bare.damage_input_dbm(), -10.0)
        # the example chain: device -10 dBm behind 17.5 dB of net gain
        self.assertAlmostEqual(F.RxChain(F.parse_chain(RX_EXAMPLE, "rx"), 6, -10).damage_input_dbm(), -27.5)

    def test_compression_flag(self):
        rx = F.RxChain(F.parse_chain("lna:l:20:1:15::10", "rx"), 6.0, 10.0)
        self.assertFalse(rx.budget(1e9, -60, 0, p1db_margin_db=3)["compressed"])
        self.assertTrue(rx.budget(1e9, -6, 0, p1db_margin_db=3)["compressed"])     # headroom 1 dB

    def test_lna_frequency_table(self):
        table = F.parse_lna_freq_table("900:18:1.2;1000:22:0.8")
        rx = F.RxChain(F.parse_chain("lna:l:20:1:15::10", "rx"), 6.0, 10.0, None, table)
        self.assertAlmostEqual(rx.budget(910e6, -60, 0)["ext_gain_db"], 18.0)
        self.assertAlmostEqual(rx.budget(990e6, -60, 0)["ext_gain_db"], 22.0)
        self.assertAlmostEqual(rx.damage_input_dbm(), 10.0 - 22.0)              # worst table gain


class TestTxPowerCal(unittest.TestCase):
    CAPS = DEV.get_caps("sim")    # tx_max 10 dBm, gain 0..31.75

    def rows(self):
        out = []
        for c in (0, 1):
            for f in (900e6, 930e6):
                for g in (0.0, 10.0, 20.0):
                    out.append((c, f, g, -20.0 + g + (f - 900e6) / 30e6))   # planar in (f, g)
        return out

    def test_bilinear_inside(self):
        cal = F.TxPowerCal(self.rows(), self.CAPS)
        self.assertTrue(cal.calibrated)
        self.assertAlmostEqual(cal.fs_dbm(0, 915e6, 5.0), -20.0 + 5.0 + 0.5, places=9)
        self.assertAlmostEqual(cal.fs_dbm(1, 900e6, 20.0), 0.0, places=9)
        self.assertTrue(cal.in_range(1, 930e6, 0.0))
        self.assertTrue(cal.covers(0, 915e6, 0.0, 20.0))

    def test_never_extrapolates(self):
        cal = F.TxPowerCal(self.rows(), self.CAPS)
        for chan, f, g in ((0, 940e6, 5.0), (0, 915e6, 25.0), (0, 899e6, 5.0), (3, 915e6, 5.0)):
            self.assertFalse(cal.in_range(chan, f, g))
            self.assertAlmostEqual(cal.fs_dbm(chan, f, g), cal.conservative_dbm(chan, g))
        self.assertFalse(cal.covers(0, 915e6, 0.0, 25.0))

    def test_conservative_bound(self):
        cal = F.TxPowerCal(None, self.CAPS)
        self.assertFalse(cal.calibrated)
        g_max = self.CAPS.tx_gain_range_db[1]
        self.assertAlmostEqual(cal.fs_dbm(0, 915e6, g_max), self.CAPS.tx_max_dbm_upper)
        self.assertAlmostEqual(cal.fs_dbm(0, 915e6, 0.0), self.CAPS.tx_max_dbm_upper - g_max)
        self.assertAlmostEqual(cal.fs_dbm(0, 915e6, 99.0), self.CAPS.tx_max_dbm_upper)   # never above
        self.assertAlmostEqual(cal.fs_dbm(0, 915e6, -5.0), cal.fs_dbm(0, 915e6, 0.0))    # clamps to min
        true_offset = C.default_config().sim.tx_true_offset_db
        for g in np.linspace(0, g_max, 9):
            true = cal.conservative_dbm(0, g) - true_offset          # the sim's hidden true P_fs
            self.assertGreaterEqual(cal.fs_dbm(0, 915e6, g), true)
        tight = F.TxPowerCal(None, self.CAPS, max_dbm_upper={0: 4.0})
        self.assertAlmostEqual(tight.fs_dbm(0, 915e6, g_max), 4.0)
        self.assertAlmostEqual(tight.fs_dbm(1, 915e6, g_max), 10.0)
        with self.assertRaises(ValueError):
            F.TxPowerCal(None, None).fs_dbm(0, 915e6, 0.0)

    def test_load_and_errors(self):
        with tempfile.TemporaryDirectory() as td:
            p = write(os.path.join(td, "cal.csv"), "# measured\nchan,freq_hz,tx_gain_db,p_fs_dbm\n"
                                                   "0,915e6,0,-21.5\n0,915e6,10,-11.0\n")
            cal = F.TxPowerCal.load(p, self.CAPS)
            self.assertAlmostEqual(cal.fs_dbm(0, 915e6, 5.0), -16.25)
            self.assertFalse(cal.in_range(0, 915.1e6, 5.0))
            write(p, "chan,freq,gain,p\n0,1,2,3\n")
            with self.assertRaises(ConfigError):
                F.TxPowerCal.load(p, self.CAPS)
            write(p, "chan,freq_hz,tx_gain_db,p_fs_dbm\n0,915e6,x,1\n")
            with self.assertRaises(ConfigError):
                F.TxPowerCal.load(p, self.CAPS)
        with self.assertRaises(ConfigError):
            F.TxPowerCal([(0, 915e6, 0, 1), (0, 915e6, 0, 2)])
        with self.assertRaises(ConfigError):
            F.TxPowerCal([(0, 915e6, float("nan"), 1)])


class TestTxPrediction(unittest.TestCase):
    def chain(self, spec=TX_EXAMPLE, p_fs=0.0):
        rows = [(0, f, g, p_fs + g) for f in (900e6, 930e6) for g in (0.0, 30.0)]
        return F.TxChain(0, F.parse_chain(spec, "tx"), F.TxPowerCal(rows, DEV.get_caps("sim")))

    def test_linear_region(self):
        ch = self.chain(p_fs=-40.0)
        pr = ch.predict(915e6, 0.0, 0.5, 0.5)
        dev = -40.0 + 20 * math.log10(0.5)
        self.assertAlmostEqual(pr.device_avg_dbm, dev)
        self.assertAlmostEqual(pr.device_pep_dbm, dev)
        self.assertAlmostEqual(pr.out_pep_dbm, dev + 20 + 17 - 0.5 - 0.3 - 0.8, places=3)
        self.assertFalse(pr.compressed)
        self.assertFalse(pr.saturated)
        self.assertTrue(pr.power_calibrated)
        self.assertEqual(len(pr.stage_levels), 5)
        self.assertGreater(pr.headroom_db, 20.0)

    def test_compression_and_saturation(self):
        ch = self.chain(p_fs=-20.0)
        lv = [ch.predict(915e6, g, 1.0, 1.0, pa_backoff_db=1.0) for g in (0.0, 15.0, 20.0, 30.0)]
        self.assertFalse(lv[0].compressed)
        self.assertTrue(lv[-1].compressed)
        self.assertTrue(lv[-1].saturated)
        self.assertEqual(lv[-1].limiting_stage, "driver")     # the example driver compresses first
        hr = [p.headroom_db for p in lv]
        self.assertTrue(all(b < a for a, b in zip(hr, hr[1:])))
        am = [p.am_pm_deg for p in lv]
        self.assertTrue(all(b > a for a, b in zip(am, am[1:])))    # AM/PM grows with drive
        self.assertLess(lv[-1].out_pep_dbm, 40.0 - 0.5 - 0.3 - 0.8 + 1e-9)

    def test_papr_margin_and_rms(self):
        ch = self.chain(p_fs=-40.0)
        a = ch.predict(915e6, 0.0, 0.25, 0.5)
        b = ch.predict(915e6, 0.0, 0.25, 0.5, papr_margin_db=3.0)
        self.assertAlmostEqual(a.device_pep_dbm - a.device_avg_dbm, 20 * math.log10(2.0))
        self.assertAlmostEqual(b.device_pep_dbm - a.device_pep_dbm, 3.0)
        self.assertAlmostEqual(b.out_avg_dbm, a.out_avg_dbm, places=3)     # linear region
        z = ch.predict(915e6, 0.0, 0.0, 0.0)
        self.assertEqual(z.out_pep_dbm, float("-inf"))
        self.assertEqual(z.headroom_db, float("inf"))

    def test_max_in_and_unmodelled(self):
        ch = F.TxChain(0, F.parse_chain("pa:p:30:::40:-20", "tx"), F.TxPowerCal(None, DEV.get_caps("sim")))
        self.assertEqual(ch.predict(915e6, 31.75, 0.5, 0.5).max_in_exceeded, "p")
        lin = F.TxChain(0, F.parse_chain("amp:a:10", "tx"), F.TxPowerCal(None, DEV.get_caps("sim")))
        pr = lin.predict(915e6, 0.0, 0.5, 0.5)
        self.assertEqual(pr.unmodelled, ("a",))


class TestEirpIsolation(unittest.TestCase):
    def test_eirp_upper_bound(self):
        self.assertAlmostEqual(F.eirp_upper_bound_dbm([30.0] * 8, 5.0), 53.06, places=2)
        self.assertAlmostEqual(F.eirp_upper_bound_dbm([30.0], [5.0]), 35.0)
        self.assertAlmostEqual(F.eirp_upper_bound_dbm([30.0, None, float("-inf")], 0.0), 30.0)
        self.assertEqual(F.coherent_sum_dbm([]), float("-inf"))
        self.assertAlmostEqual(F.coherent_sum_dbm([0.0, 0.0]), 20 * math.log10(2))
        self.assertAlmostEqual(F.power_sum_dbm([0.0, 0.0]), 10 * math.log10(2))

    def test_eirp_pattern_peaks_at_the_steered_bound(self):
        lam = C0 / 1e9
        pos = np.array([[k * lam / 2, 0.0, 0.0] for k in range(8)])
        u0 = np.array([math.sin(math.radians(30)), math.cos(math.radians(30)), 0.0])
        t = np.conj(np.exp(1j * 2 * np.pi / lam * (pos @ u0)))
        e0 = F.eirp_pattern_dbm(pos, t, [30.0] * 8, 5.0, 1e9, u0)
        self.assertAlmostEqual(e0, F.eirp_upper_bound_dbm([30.0] * 8, 5.0), places=9)
        off = F.eirp_pattern_dbm(pos, t, [30.0] * 8, 5.0, 1e9, np.array([[0, 1.0, 0], [1.0, 0, 0]]))
        self.assertTrue(np.all(off < e0 - 3.0))

    def test_required_attenuation(self):
        self.assertAlmostEqual(F.required_attenuation_db(30.0, -20.0, 10.0), 60.0)
        self.assertEqual(F.required_attenuation_db(-50.0, -20.0, 10.0), 0.0)

    def test_free_space_isolation(self):
        self.assertAlmostEqual(F.free_space_isolation_db(1.0, 1e9), 32.4, places=1)
        self.assertAlmostEqual(F.free_space_isolation_db(1.0, 1e9, 5.0, 5.0),
                               F.free_space_isolation_db(1.0, 1e9) - 10.0)
        self.assertEqual(F.free_space_isolation_db(0.01, 1e9, 10, 10), 0.0)   # never negative
        with self.assertRaises(ValueError):
            F.free_space_isolation_db(0.0, 1e9)


class TestFrontendModel(unittest.TestCase):
    def neighbour_model(self, **kw):
        rx = [F.RxChain(F.parse_chain("lna:l:20:1:15::10", "rx"), 6.0, -10.0)]
        tx = [F.TxChain(0, (), F.TxPowerCal(None, DEV.get_caps("sim")))]
        kw.setdefault("protect_passive_chain", False)
        return F.FrontendModel(rx, tx, rx_pos=[np.zeros(3)], tx_pos=[np.array([1.0, 0, 0])],
                               design_freq_hz=1e9, **kw)

    def test_rx_protection_flags_neighbour(self):
        m = self.neighbour_model()
        self.assertAlmostEqual(m.isolation_db[0, 0], 32.45, places=2)
        issues, worst, limits = m.rx_protection([30.0], "radiate", True, "tx")
        self.assertEqual([i.code for i in issues], ["RX_PROTECTION"])
        self.assertAlmostEqual(worst[0], 30.0 - 32.448, places=2)
        self.assertAlmostEqual(limits[0], -10.0 - 20.0 - 10.0)
        self.assertIn("add 37.", issues[0].message)
        ok, _, _ = m.rx_protection([-20.0], "radiate", True, "tx")
        self.assertEqual(ok, [])
        idle, w2, _ = m.rx_protection([None], "radiate", True, "tx")
        self.assertEqual(idle, [])
        self.assertEqual(w2[0], float("-inf"))

    def test_lna_off_credit_needs_a_line(self):
        m = self.neighbour_model(lna_off_isolation_db=30.0, lna_controllable=False)
        self.assertEqual(m.rx_limits_dbm(lna_on=False), m.rx_limits_dbm(lna_on=True))
        m2 = self.neighbour_model(lna_off_isolation_db=30.0, lna_controllable=True)
        # LNA off: device port -10 + 30 = 20 dBm, LNA max_in 10 -> 10 dBm at the antenna
        self.assertAlmostEqual(m2.rx_limits_dbm(lna_on=False)[0], 10.0 - 10.0)
        self.assertEqual(m2.rx_protection([10.0], "radiate", False, "tx")[0], [])

    def test_same_element_tr_and_modes(self):
        rx = [F.RxChain((), 6.0, -10.0) for _ in range(2)]
        tx = [F.TxChain(n, (), F.TxPowerCal(None, DEV.get_caps("sim"))) for n in range(2)]
        same = np.eye(2, dtype=bool)
        m = F.FrontendModel(rx, tx, same_element=same, tr_isolation_db=40.0, has_tr_switch=True,
                            tx_rx_isolation_db=50.0, cabled_rx_path_loss_db=[30.0, 30.0],
                            loopback_path_loss_db=[0.0, 0.0], design_freq_hz=1e9)
        self.assertTrue(np.allclose(m.isolation_db, [[40, 50], [50, 40]]))
        self.assertEqual(m.rx_protection([5.0, 5.0], "dummy_load", True, "tx")[0], [])
        # T/R left in rx while transmitting: the same-element isolation is gone
        self.assertTrue(m.rx_protection([5.0, 5.0], "dummy_load", True, "rx")[0])
        _, worst, _ = m.rx_protection([5.0, None], "cabled", True, "tx")
        self.assertAlmostEqual(worst[0], 5.0 - 30.0)
        with self.assertRaises(ValueError):
            m.rx_protection([5.0], "dummy_load", True, "tx")
        with self.assertRaises(ValueError):
            m.rx_protection([5.0, 5.0], "space", True, "tx")

    def test_loopback_rx_and_passive_chain(self):
        rx = [F.RxChain((), 6.0, -10.0) for _ in range(3)]
        tx = [F.TxChain(n, (), F.TxPowerCal(None, DEV.get_caps("sim"))) for n in range(2)]
        m = F.FrontendModel(rx, tx, tx_rx_isolation_db=60.0, loopback_rx_chan=2,
                            loopback_path_loss_db=[40.0, 40.0], passive_isolation_db=0.0,
                            design_freq_hz=1e9)
        self.assertTrue(np.allclose(m.isolation_db[2], [40.0, 40.0]))
        _, worst, _ = m.rx_protection([0.0, 0.0], "loopback", True, "tx")
        self.assertAlmostEqual(worst[2], 20 * math.log10(2) - 40.0)
        iss, _, _ = m.rx_protection([0.0, 0.0], "radiate", True, "tx")
        self.assertEqual([i.code for i in iss], ["PASSIVE_CHAIN"])
        m.passive_isolation_db = 5.0
        iss, _, _ = m.rx_protection([0.0, 0.0], "radiate", True, "tx")
        self.assertEqual([i.code for i in iss], ["PASSIVE_CHAIN"])   # 6.02 dBm - 5 > 10 - 10
        m.passive_isolation_db = 60.0
        self.assertEqual(m.rx_protection([0.0, 0.0], "radiate", True, "tx")[0], [])
        self.assertEqual(m.rx_protection([0.0, 0.0], "dummy_load", True, "tx")[0], [])

    def test_active_reflection(self):
        tx = [F.TxChain(n, (), F.TxPowerCal(None, DEV.get_caps("sim"))) for n in range(2)]
        S = np.array([[0.1, 0.2], [0.2, 0.1]], dtype=complex)
        m = F.FrontendModel([], tx, s_matrix=S, design_freq_hz=1e9)
        self.assertTrue(np.allclose(m.active_reflection([1, 1]), [0.3, 0.3]))
        self.assertTrue(np.allclose(m.active_reflection([1, -1]), [0.1, 0.1]))
        g = m.active_reflection([1, 0])
        self.assertAlmostEqual(g[0], 0.1)
        self.assertEqual(g[1], np.inf)
        rev = m.active_reverse_dbm([1, 1], [0.0, 0.0])
        self.assertTrue(np.allclose(rev, 20 * math.log10(0.3)))
        self.assertIsNone(F.FrontendModel([], tx, design_freq_hz=1e9).active_reflection([1, 1]))
        with self.assertRaises(ConfigError):
            F.FrontendModel([], tx, s_matrix=np.eye(3))

    def test_active_waves_from_samples(self):
        tx = [F.TxChain(n, (), F.TxPowerCal(None, DEV.get_caps("sim"))) for n in range(2)]
        S = np.array([[0.1, 0.2], [0.2, 0.1]], dtype=complex)
        m = F.FrontendModel([], tx, s_matrix=S, design_freq_hz=1e9)
        t = np.arange(512)
        s = np.exp(2j * np.pi * 0.01 * t)
        x = np.vstack([s, s])                                   # in phase, as weights [1, 1]
        rev, gam = m.active_waves_from_samples(x, [0, 1], [1.0, 1.0])
        self.assertTrue(np.allclose(gam, [0.3, 0.3]))
        self.assertTrue(np.allclose(rev, 20 * math.log10(0.3) + 30))
        rev, gam = m.active_waves_from_samples(np.vstack([s, -s]), [0, 1], [1.0, 1.0])
        self.assertTrue(np.allclose(gam, [0.1, 0.1]))
        # one driven chain: the idle chain's reverse power is reported, its |G| is nan
        rev, gam = m.active_waves_from_samples(s[None, :], [0], [2.0])
        self.assertAlmostEqual(gam[0], 0.1)
        self.assertTrue(math.isnan(gam[1]))
        self.assertAlmostEqual(rev[1], 10 * math.log10((0.2 * 2.0) ** 2) + 30)
        # segment-wise in phase / anti-phase (Hadamard-like): the worst window counts
        h = np.concatenate([np.ones(256), -np.ones(256)])
        rev, gam = m.active_waves_from_samples(np.vstack([s, s * h]), [0, 1], [1.0, 1.0])
        self.assertAlmostEqual(gam[0], 0.3, places=6)
        # blocks give the same answer
        r2, g2 = m.active_waves_from_samples(np.vstack([s, s * h]), [0, 1], [1.0, 1.0], block=100)
        self.assertTrue(np.allclose(r2, rev) and np.allclose(g2, gam))
        self.assertIsNone(F.FrontendModel([], tx, design_freq_hz=1e9).active_waves_from_samples(x, [0, 1], [1, 1]))
        with self.assertRaises(ValueError):
            m.active_waves_from_samples(x, [0], [1.0])

    def test_from_config(self):
        with tempfile.TemporaryDirectory() as td:
            write(os.path.join(td, "cal.csv"), "chan,freq_hz,tx_gain_db,p_fs_dbm\n" + "".join(
                "%d,%g,%g,%g\n" % (c, f, g, -25 + g) for c in range(4) for f in (900e6, 930e6) for g in (0, 20)))
            write(os.path.join(td, "s.csv"), "# freq_hz=915e6\nm,n,re,im\n" + "".join(
                "%d,%d,%g,0\n" % (a, b, 0.1 if a == b else 0.05) for a in range(4) for b in range(4)))
            cfg = C.default_config(
                base_dir=td,
                devices={"sim0": {"family": "sim", "rx_channels": "0..3", "tx_channels": "0..3"}},
                geometry={"preset": "ula", "num_elements": 4},
                frontend={"en_frontend": 1, "rx_chain": "lna:l:20:1:15::10", "tx_chain": "amp:d:10:3:20",
                          "tx_chain.2": "amp:d:12:3:20", "tr_isolation_db": 45,
                          "tx_power_cal_file": "cal.csv", "s_matrix_file": "s.csv"},
                rf_control={"tr_switch_line": "sim:tr"})
            m = F.FrontendModel.from_config(cfg)
            self.assertEqual((m.n_rx, m.n_tx), (4, 4))
            self.assertTrue(np.allclose(np.diag(m.isolation_db), 45.0))
            lam = C0 / 915e6
            self.assertAlmostEqual(m.isolation_db[0, 1], F.free_space_isolation_db(lam / 2, 915e6), places=6)
            self.assertAlmostEqual(m.isolation_db[0, 3], F.free_space_isolation_db(1.5 * lam, 915e6), places=6)
            self.assertTrue(m.has_tr_switch)
            self.assertEqual(m.tx[2].stages[0].gain_db, 12.0)
            self.assertTrue(m.tx[0].power_cal.in_range(3, 915e6, 10.0))
            self.assertEqual(m.s_matrix.shape, (4, 4))
            self.assertEqual(m.validate(cfg), [])
            # iso file overrides geometry and the T/R value
            write(os.path.join(td, "iso.csv"), "\n".join(",".join(["70"] * 4) for _ in range(4)) + "\n")
            cfg2 = C.default_config(
                base_dir=td,
                devices={"sim0": {"family": "sim", "rx_channels": "0..3", "tx_channels": "0..3"}},
                frontend={"tx_rx_isolation_file": "iso.csv"})
            self.assertTrue(np.allclose(F.FrontendModel.from_config(cfg2).isolation_db, 70.0))
            write(os.path.join(td, "iso.csv"), "70,70\n70,70\n")
            with self.assertRaises(ConfigError):
                F.FrontendModel.from_config(cfg2)

    def test_validate_tx_lock(self):
        cfg = C.default_config(
            active={"mode": "trx", "operator": "op"},
            devices={"sim0": {"family": "sim", "rx_channels": "0..1", "tx_channels": "0..1"}},
            geometry={"num_elements": 2},
            frontend={"en_frontend": 1, "tx_chain": "amp:d:10", "rx_chain": "lna:l:20:1"},
            tx={"en_tx": 1, "allow_bands": "902e6:928e6", "dummy_load_rating_w": 1})
        codes = [i.code for i in F.FrontendModel.from_config(cfg).validate(cfg)]
        self.assertEqual(codes.count("TX_LOCK"), 4)

    def test_header_stamps(self):
        cfg = C.default_config(
            devices={"sim0": {"family": "sim", "rx_channels": "0..3"}},
            frontend={"en_frontend": 1, "rx_chain": "lna:l:20:0.9:15::10;cable:c:-1.5",
                      "rx_chain.3": "lna:l:20:0.9:-45::10", "expected_input_dbm": -60})
        m = F.FrontendModel.from_config(cfg)
        st = m.header_stamps(915e6, [30, 30, 30, 10], [1, 1, 0, 1])
        for k in ("ext_lna_tenths", "total_gain_tenths", "nf_mdb"):
            self.assertEqual(len(st[k]), 4)
            self.assertTrue(all(isinstance(v, int) for v in st[k]))
        self.assertEqual(st["ext_lna_tenths"][0], 185)
        self.assertEqual(st["total_gain_tenths"][0], 485)
        b = m.rx[0].budget(915e6, -60, 30)
        self.assertEqual(st["nf_mdb"][0], int(round(b["nf_db"] * 1000)))
        self.assertEqual(st["compression_mask"], 1 << 3)        # p1db -45 dBm on RX 3
        self.assertEqual(st["lna_enable_mask"], 0b1011)
        self.assertEqual(m.header_stamps(915e6, 30, 0b0101)["lna_enable_mask"], 0b0101)
        self.assertEqual(m.header_stamps(915e6, 30, None)["lna_enable_mask"], 0b1111)
        big = C.default_config(devices={"sim0": {"family": "sim", "rx_channels": "0..31"},
                                        "sim1": {"family": "sim", "rx_channels": "0..7", "role": "slave"}},
                               geometry={"num_elements": 4})
        mb = F.FrontendModel.from_config(big)
        self.assertEqual(mb.n_rx, 40)
        self.assertEqual(len(mb.header_stamps(915e6, 30, None)["nf_mdb"]), 32)


if __name__ == "__main__":
    unittest.main()
