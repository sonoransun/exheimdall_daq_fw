"""
    Unit tests for the active beamforming (Firmware/_active/active_beamforming.py).

    Hardware-free and seeded.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import json
import logging
import math
import sys
import unittest
from os.path import dirname, join, realpath

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_beamforming as BF  # noqa: E402
import active_config as C  # noqa: E402
import active_dsp as D  # noqa: E402
import active_geometry as G  # noqa: E402
import active_pattern as P  # noqa: E402
from active_common import C0, ChannelRef, GeometryError, rng_stream  # noqa: E402

logging.getLogger("antenna_profile").setLevel(logging.ERROR)
logging.getLogger("heimdall.active.beamforming").setLevel(logging.ERROR)

F = 1e9
LAM = C0 / F


def ula8():
    return G.preset_ula(8, LAM / 2)


def _cn(rng, *shape):
    return (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) / np.sqrt(2.0)


class TestSpec(unittest.TestCase):
    def test_validation_and_dict(self):
        s = BF.BeamSpec(10, 5, method="lcmv", taper="taylor", nulls=[[40, 0]], nbar=5)
        self.assertEqual(s.nulls, ((40.0, 0.0),))
        self.assertEqual(BF.BeamSpec.from_dict(json.loads(json.dumps(s.to_dict()))), s)
        for bad in (dict(method="music"), dict(taper="kaiser"), dict(steering="tdl"), dict(normalize="x"),
                    dict(el_deg=95.0), dict(nbar=1), dict(diag_loading=-1.0), dict(az_deg=float("nan"))):
            with self.assertRaises(ValueError):
                BF.BeamSpec(**dict(dict(az_deg=0.0), **bad))

    def test_spec_from_config(self):
        cfg = C.default_config(beam={"look_az_deg": 20, "look_el_deg": 3, "rx_method": "projection",
                                     "taper": "chebyshev", "nulls": "50:0", "tx_look_az_deg": -10,
                                     "tx_taper": "hamming", "tx_nulls": "", "steering": "ttd"})
        rx = BF.spec_from_config(cfg)
        self.assertEqual((rx.az_deg, rx.el_deg, rx.method, rx.taper, rx.nulls, rx.steering),
                         (20.0, 3.0, "projection", "chebyshev", ((50.0, 0.0),), "ttd"))
        tx = BF.spec_from_config(cfg.beam, "tx")
        self.assertEqual((tx.az_deg, tx.el_deg, tx.method, tx.taper, tx.nulls), (-10.0, 3.0, "das", "hamming", ()))
        self.assertIsNone(BF.spec_from_config(C.default_config(beam={"rx_method": "off"})))
        with self.assertRaises(ValueError):
            BF.spec_from_config(cfg, "both")


class TestTapers(unittest.TestCase):
    def test_ula_windows(self):
        g = ula8()
        np.testing.assert_array_equal(BF.taper_weights(g, "uniform", 30, 4), 1.0)
        cheb = BF.taper_weights(g, "chebyshev", 30, 4)
        self.assertAlmostEqual(cheb.max(), 1.0)
        np.testing.assert_allclose(cheb, cheb[::-1], atol=1e-12)
        hann = BF.taper_weights(g, "hann", 30, 4)
        self.assertGreater(hann.min(), 0.0)                       # no zero-weight edge elements
        for kind in ("hamming", "hann", "chebyshev", "taylor"):
            w = BF.taper_weights(g, kind, 30, 4)
            self.assertTrue(np.all(w > 0) and abs(w.max() - 1.0) < 1e-12, kind)

    def test_chebyshev_and_taylor_metrics(self):
        g = ula8()
        sol = BF.solve_beam(g, BF.BeamSpec(0, 0, taper="chebyshev", sll_db=30), F)
        self.assertAlmostEqual(sol.metrics.psll_db, -30.0, delta=0.5)
        self.assertAlmostEqual(sol.metrics.taper_eff, 0.842, delta=0.01)
        sol = BF.solve_beam(g, BF.BeamSpec(0, 0, taper="taylor", sll_db=30, nbar=4), F)
        self.assertAlmostEqual(sol.metrics.psll_db, -28.3, delta=0.7)
        uni = BF.solve_beam(g, BF.BeamSpec(0, 0), F)
        self.assertGreater(sol.metrics.hpbw_az_deg, uni.metrics.hpbw_az_deg)
        ham = BF.solve_beam(g, BF.BeamSpec(0, 0, taper="hamming"), F)
        self.assertLess(ham.metrics.psll_db, -30.0)

    def test_ura_separable_and_uca_uniform(self):
        g = G.preset_ura(4, 3, LAM / 2, LAM / 2)
        w = BF.taper_weights(g, "chebyshev", 25, 4)
        wx = BF.taper_weights(G.preset_ula(4, LAM / 2), "chebyshev", 25, 4)
        wy = BF.taper_weights(G.preset_ula(3, LAM / 2), "chebyshev", 25, 4)
        np.testing.assert_allclose(w.reshape(3, 4), np.outer(wy, wx), atol=1e-12)
        issues = []
        wu = BF.taper_weights(G.preset_uca(6, radius_m=0.3), "taylor", 30, 4, issues=issues)
        np.testing.assert_array_equal(wu, 1.0)
        self.assertEqual([i.code for i in issues], ["TAPER"])

    def test_irregular_and_thinned(self):
        rng = np.random.default_rng(4)
        r = np.sqrt(rng.uniform(0, 1, 30)) * 0.8
        th = rng.uniform(0, 2 * np.pi, 30)
        els = [G.Element(i, (float(x), 0.0, float(z)), rx=ChannelRef("s", i))
               for i, (x, z) in enumerate(zip(r * np.cos(th), r * np.sin(th)))]
        g = G.ArrayGeometry(els)
        w = BF.taper_weights(g, "taylor", 30, 4)
        rho = np.linalg.norm(g.positions() - g.positions().mean(axis=0), axis=1)
        order = np.argsort(rho)
        self.assertGreater(w[order[0]], w[order[-1]])              # radial: centre heavier than edge
        self.assertAlmostEqual(w.max(), 1.0)
        thin = G.ArrayGeometry([G.Element(i, (LAM / 2 * k, 0, 0), rx=ChannelRef("s", i))
                                for i, k in enumerate((0, 1, 2, 4, 5, 7, 8, 9))])
        wt = BF.taper_weights(thin, "chebyshev", 30, 4)
        np.testing.assert_allclose(wt, wt[::-1], atol=1e-9)        # symmetric about the aperture centre
        self.assertLess(wt[0], wt[3])


class TestRxWeights(unittest.TestCase):
    def test_das_distortionless(self):
        g = ula8()
        for az, el, taper in ((0, 0, "uniform"), (35, 10, "chebyshev"), (-50, -5, "taylor")):
            spec = BF.BeamSpec(az, el, taper=taper)
            w = BF.rx_weights(g, spec, F)
            a0 = P.array_manifold(g, G.direction_vector(az, el), F)
            self.assertAlmostEqual(abs(np.vdot(w, a0) - 1.0), 0.0, places=12)
        els = [G.Element(e.id, e.pos_body_m, rx=e.rx, pattern=G.ElementPattern("cos_q", q=2)) for e in g.elements]
        gp = G.ArrayGeometry(els)
        w = BF.rx_weights(gp, BF.BeamSpec(20, 0), F)
        a0 = P.array_manifold(gp, G.direction_vector(20, 0), F)
        self.assertAlmostEqual(abs(np.vdot(w, a0) - 1.0), 0.0, places=12)
        wn = BF.rx_weights(g, BF.BeamSpec(20, 0, normalize="unit_norm"), F)
        self.assertAlmostEqual(np.linalg.norm(wn), 1.0, places=12)
        s = np.vdot(wn, P.steering_ff(g.positions(), G.direction_vector(20, 0), F))
        self.assertAlmostEqual(s.imag, 0.0, places=12)
        self.assertGreater(s.real, 0.0)

    def test_near_field_focus(self):
        g = ula8()
        spec = BF.BeamSpec(10, 0, focus_range_m=2.0)
        w = BF.rx_weights(g, spec, F)
        p = 2.0 * G.direction_vector(10, 0)
        a_nf = P.steering_nf(g.positions(), p, F)
        self.assertAlmostEqual(abs(np.vdot(w, a_nf) - 1.0), 0.0, places=12)
        w_ff = BF.rx_weights(g, BF.BeamSpec(10, 0), F)
        self.assertGreater(abs(np.vdot(w, a_nf)), abs(np.vdot(w_ff, a_nf)) * 1.0001)

    def test_projection_nulls(self):
        g = ula8()
        spec = BF.BeamSpec(10, 0, method="projection", nulls=((40, 0), (-35, 0)))
        sol = BF.solve_beam(g, spec, F)
        w = sol.weights
        a0 = P.steering_ff(g.positions(), G.direction_vector(10, 0), F)
        self.assertAlmostEqual(abs(np.vdot(w, a0) - 1.0), 0.0, places=12)
        self.assertTrue(all(d < -100.0 for d in sol.metrics.null_depths_db))
        ref = BF.solve_beam(g, BF.BeamSpec(10, 0), F)
        self.assertLess(sol.metrics.pointing_error_deg, 0.5)
        self.assertAlmostEqual(sol.metrics.hpbw_az_deg, ref.metrics.hpbw_az_deg, delta=1.5)
        # DAS ignores nulls (and says so)
        issues = []
        BF.rx_weights(g, BF.BeamSpec(10, 0, nulls=((40, 0),)), F, issues=issues)
        self.assertEqual([i.code for i in issues], ["NULLS"])

    def test_null_errors(self):
        g = ula8()
        with self.assertRaises(GeometryError) as cm:
            BF.rx_weights(g, BF.BeamSpec(10, 0, method="projection", nulls=((13, 0),)), F)
        self.assertEqual(cm.exception.code, "NULL_IN_MAINLOBE")
        self.assertTrue(str(cm.exception).startswith("NULL_IN_MAINLOBE"))
        many = tuple((a, 0) for a in (40, 50, 60, -40, -50, -60, 70))
        with self.assertRaises(GeometryError) as cm:
            BF.rx_weights(g, BF.BeamSpec(0, 0, method="projection", nulls=many), F)
        self.assertEqual(cm.exception.code, "NULLS")

    def _scene(self, g, k, inr_db=30.0, seed=7):
        rng = rng_stream(seed, "mvdr", k)
        ai = P.steering_ff(g.positions(), G.direction_vector(40, 0), F)
        n = len(ai)
        X = math.sqrt(10 ** (inr_db / 10)) * np.outer(ai, _cn(rng, k)) + _cn(rng, n, k)
        return X, ai

    def test_mvdr(self):
        g = ula8()
        n = 8
        X, ai = self._scene(g, 4 * n)
        w = BF.rx_weights(g, BF.BeamSpec(0, 0, method="mvdr"), F, X=X)
        a0 = P.steering_ff(g.positions(), G.direction_vector(0, 0), F)
        self.assertAlmostEqual(abs(np.vdot(w, a0) - 1.0), 0.0, places=12)
        wd = BF.rx_weights(g, BF.BeamSpec(0, 0), F)
        out_i = abs(np.vdot(w, ai)) ** 2 * 1000.0                   # interferer power after the beam
        out_d = abs(np.vdot(wd, ai)) ** 2 * 1000.0
        self.assertLess(10 * math.log10(out_i / 1000.0), -30.0)     # > 30 dB below its element power
        self.assertGreater(10 * math.log10(out_d / out_i), 30.0)    # and > 30 dB better than DAS
        R = BF.sample_covariance(X)
        np.testing.assert_allclose(BF.rx_weights(g, BF.BeamSpec(0, 0, method="mvdr"), F, R=R, n_snapshots=32), w)
        w_np = BF.rx_weights(g, BF.BeamSpec(0, 0, method="mvdr"), F, R=R, noise_power=1.0)
        self.assertLess(abs(np.vdot(w_np, ai)) ** 2 * 1000.0, 1e-3 * 1000.0)
        with self.assertRaises(GeometryError) as cm:
            BF.rx_weights(g, BF.BeamSpec(0, 0, method="mvdr"), F, X=X[:, :2 * n - 1])
        self.assertEqual(cm.exception.code, "SNAPSHOTS")
        with self.assertRaises(GeometryError) as cm:
            BF.rx_weights(g, BF.BeamSpec(0, 0, method="mvdr"), F)
        self.assertEqual(cm.exception.code, "SNAPSHOTS")
        BF.rx_weights(g, BF.BeamSpec(0, 0, method="mvdr"), F, X=X[:, :2 * n])      # 2N is enough

    def test_lcmv_constraints_exact(self):
        g = ula8()
        X, _ = self._scene(g, 64, seed=8)
        spec = BF.BeamSpec(5, 0, method="lcmv", nulls=((40, 0), (-50, 0)))
        w = BF.rx_weights(g, spec, F, X=X)
        Cm = np.stack([P.steering_ff(g.positions(), G.direction_vector(a, 0), F) for a in (5, 40, -50)], axis=1)
        self.assertLess(np.abs(Cm.conj().T @ w - np.array([1, 0, 0])).max(), 1e-9)
        f = np.array([1.0, 0.5j])
        wl = BF.lcmv(BF.sample_covariance(X), Cm[:, :2], f, 0.1)
        self.assertLess(np.abs(Cm[:, :2].conj().T @ wl - f).max(), 1e-9)

    def test_beams_matrix_and_apply(self):
        g = ula8()
        specs = [BF.BeamSpec(a, 0) for a in (-30, 0, 30)]
        W = BF.beams_matrix(g, specs, F)
        self.assertEqual(W.shape, (3, 8))
        np.testing.assert_allclose(W[2], np.conj(BF.rx_weights(g, specs[2], F)))
        rng = rng_stream(3, "apply")
        s = _cn(rng, 512)
        X = np.outer(P.steering_ff(g.positions(), G.direction_vector(30, 0), F), s)
        np.testing.assert_allclose(BF.apply_rx(X, BF.rx_weights(g, specs[2], F)), s, atol=1e-10)
        np.testing.assert_allclose((W @ X)[2], s, atol=1e-10)
        with self.assertRaises(ValueError):
            BF.apply_rx(X[:4], BF.rx_weights(g, specs[2], F))
        with self.assertRaises(ValueError):
            BF.apply_rx(X, BF.rx_weights(g, specs[2], F), ttd_delays=np.zeros(8))

    def test_apply_rx_ttd_wideband(self):
        fs = 1e9
        g = ula8()
        az = 60.0
        u0 = G.direction_vector(az, 0)
        pos = g.positions()
        s = np.concatenate([np.zeros(256), D.lfm(2048, fs, 0.5 * fs), np.zeros(256)])
        t_arr = -(pos @ u0) / C0
        X = np.stack([D.frac_delay_fft(s, t * fs) * np.exp(-2j * np.pi * F * t) for t in t_arr])
        spec = BF.BeamSpec(az, 0, steering="ttd")
        sol = BF.solve_beam(g, spec, F, bw_hz=0.5 * fs)
        self.assertTrue(sol.metrics.squint_ok)
        self.assertGreaterEqual(sol.ttd_delays_s.min(), 0.0)
        y_ttd = BF.apply_rx(X, sol.weights, fs, sol.ttd_delays_s)
        y_ph = BF.apply_rx(X, sol.weights)
        e = np.vdot(s, s).real
        g_ttd = abs(np.vdot(s, y_ttd)) ** 2 / (e * np.vdot(y_ttd, y_ttd).real)
        g_ph = abs(np.vdot(s, y_ph)) ** 2 / (e * np.vdot(y_ph, y_ph).real)
        self.assertGreater(g_ttd, 0.999)                            # TTD output is a (delayed) copy of s
        self.assertLess(g_ph, g_ttd)
        self.assertFalse(BF.solve_beam(g, BF.BeamSpec(az, 0), F, bw_hz=0.5 * fs).metrics.squint_ok)


class TestTx(unittest.TestCase):
    def test_reciprocity_and_normalization(self):
        g = ula8()
        for spec in (BF.BeamSpec(25, 0, taper="hamming"), BF.BeamSpec(-40, 0, method="projection", nulls=((10, 0),))):
            t = BF.tx_weights(g, spec, F)
            self.assertAlmostEqual(float(np.abs(t).max()), 1.0, places=12)
            w = BF.rx_weights(g, BF.BeamSpec(spec.az_deg, spec.el_deg, method=spec.method, taper=spec.taper,
                                             nulls=spec.nulls), F)
            U = G.direction_vector(np.linspace(0, 359, 360), 0)
            A = P.steering_ff(g.positions(), U, F)
            af_tx = A @ t                                             # a^T t
            af_rx = A @ np.conj(w)                                    # w^H a
            np.testing.assert_allclose(af_tx / np.abs(af_tx).max(), af_rx / np.abs(af_rx).max(), atol=1e-12)
        sol = BF.solve_beam(g, BF.BeamSpec(-40, 0, method="projection", nulls=((10, 0),)), F, direction="tx")
        self.assertLess(sol.metrics.null_depths_db[0], -100.0)
        self.assertLess(sol.metrics.pointing_error_deg, 0.5)

    def test_tx_set_differs_from_rx(self):
        els = [G.Element(i, (LAM / 2 * i, 0, 0), rx=ChannelRef("s", i), tx=ChannelRef("s", i) if i < 4 else None)
               for i in range(8)]
        g = G.ArrayGeometry(els)
        t = BF.tx_weights(g, BF.BeamSpec(0, 0), F)
        self.assertEqual(t.shape, (4,))
        with self.assertRaises(GeometryError):
            BF.tx_weights(G.preset_ula(4, 0.1, map_tx=False), BF.BeamSpec(0, 0), F)

    def test_adaptive_tx_rejected(self):
        for m in ("mvdr", "lcmv"):
            with self.assertRaises(GeometryError) as cm:
                BF.tx_weights(ula8(), BF.BeamSpec(0, 0, method=m), F)
            self.assertEqual(cm.exception.code, "TX_METHOD")

    def test_synthesize_tx_applies_cal_and_ttd(self):
        fs = 50e6
        g = ula8()
        n_tx = 8
        rng = rng_stream(21, "txcal")
        gains = 10 ** (rng.normal(0, 1, n_tx) / 20) * np.exp(1j * rng.uniform(-np.pi, np.pi, n_tx))
        dly = rng.uniform(-3, 3, n_tx) / fs                          # chain delays (positive = late)
        spec = BF.BeamSpec(30, 0, steering="ttd")
        t = BF.tx_weights(g, spec, F)
        ttd = P.ttd_delays_s(g.positions("tx"), G.direction_vector(30, 0))
        burst = D.ramp_window(D.bandlimited_noise(2048, fs, 0.5 * fs, rng_stream(21, "burst")), 64)
        s = np.concatenate([np.zeros(512), burst, np.zeros(512)])          # guard zeros >> max delay
        cal = {"gains": gains, "delays_s": dly}
        out = BF.synthesize_tx(s, t, fs, tx_cal=cal, ttd_delays=ttd)
        self.assertEqual(out.shape, (n_tx, s.size))
        self.assertEqual(out.dtype, np.complex128)
        # pass through the (true) chains: y_n = g_n * x_n(t - dtau_n)
        y = np.stack([gains[k] * D.frac_delay_fft(out[k], dly[k] * fs) for k in range(n_tx)])
        common = (ttd - dly) * fs
        expect = np.stack([t[k] * D.frac_delay_fft(s, ttd[k] * fs - common.min()) for k in range(n_tx)])
        np.testing.assert_allclose(y, expect, atol=1e-6 * np.abs(expect).max())
        # the same through a CalResult-like object and a (correction, delays) tuple
        class _Cal:
            delays_s = dly
            def correction(self):
                return 1.0 / gains
        np.testing.assert_allclose(BF.synthesize_tx(s, t, fs, tx_cal=_Cal(), ttd_delays=ttd), out)
        np.testing.assert_allclose(BF.synthesize_tx(s, t, fs, tx_cal=(1.0 / gains, dly), ttd_delays=ttd), out)
        with self.assertRaises(ValueError):
            BF.synthesize_tx(s, t, fs, tx_cal=(np.ones(3), np.zeros(3)))
        with self.assertRaises(TypeError):
            BF.synthesize_tx(s, t, fs, tx_cal=42)

    def test_synthesize_tx_peak_scaling_exact(self):
        g = ula8()
        t = BF.tx_weights(g, BF.BeamSpec(20, 0, taper="chebyshev"), F)
        s = D.ramp_window(D.bandlimited_noise(4096, 10e6, 5e6, rng_stream(5, "s")), 32)
        cal = (np.exp(1j * np.linspace(0, 2, 8)) * np.linspace(0.8, 1.3, 8), np.linspace(0, 2e-7, 8))
        out = BF.synthesize_tx(s, t, 10e6, tx_cal=cal, backoff_db=6.0, peak_target=0.5)
        self.assertAlmostEqual(float(np.abs(out).max()), 0.5 * 10 ** (-6 / 20), places=15)
        self.assertLessEqual(float(np.abs(out).max()), 0.5 * 10 ** (-6 / 20))
        plain = BF.synthesize_tx(s, t, 10e6)
        np.testing.assert_allclose(plain, t[:, None] * s[None, :], atol=1e-15)
        half = BF.synthesize_tx(s, t, 10e6, backoff_db=20 * math.log10(2))
        np.testing.assert_allclose(half, plain / 2, atol=1e-15)
        bad = s.copy()
        bad[5] = np.nan
        with self.assertRaises(ValueError):
            BF.synthesize_tx(bad, t, 10e6)


class TestSolution(unittest.TestCase):
    def test_json_round_trip(self):
        g = ula8()
        sol = BF.solve_beam(g, BF.BeamSpec(15, 0, method="projection", nulls=((50, 0),), steering="ttd"), F,
                            cal_phase_rms_deg=1.0, cal_amp_rms_db=0.1)
        self.assertAlmostEqual(sol.metrics.cal_limited_null_floor_db, P.cal_limited_null_floor_db(1.0, 0.1, sol.weights))
        s = sol.to_json()
        back = BF.BeamSolution.from_json(s)
        self.assertEqual(back.spec, sol.spec)
        self.assertEqual(back.direction, "rx")
        np.testing.assert_array_equal(back.weights, sol.weights)
        np.testing.assert_array_equal(back.ttd_delays_s, sol.ttd_delays_s)
        self.assertEqual(back.metrics, sol.metrics)
        self.assertEqual(back.to_json(), s)
        with self.assertRaises(ValueError):
            BF.BeamSolution.from_json(json.dumps({"format": "other"}))
        tx = BF.solve_beam(g, BF.BeamSpec(-20, 0), F, direction="tx")
        self.assertAlmostEqual(tx.metrics.peak_az_deg, 340.0, delta=0.01)
        self.assertEqual(BF.BeamSolution.from_json(tx.to_json()).direction, "tx")
        with self.assertRaises(ValueError):
            BF.solve_beam(g, BF.BeamSpec(0, 0), F, direction="up")


if __name__ == "__main__":
    unittest.main()
