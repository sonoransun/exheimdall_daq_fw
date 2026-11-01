"""
    Unit tests for steering vectors, patterns and array metrics
    (Firmware/_active/active_pattern.py), including the frozen sign conventions
    (spec section 2 and 0.3 D1): the convention test builds a true-delay plane
    wave locally, without steering_ff.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import json
import math
import sys
import unittest
from os.path import dirname, join, realpath

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_dsp as D  # noqa: E402
import active_geometry as G  # noqa: E402
import active_pattern as P  # noqa: E402
from active_common import C0, ChannelRef, rng_stream  # noqa: E402

F = 1e9
LAM = C0 / F


def _local_u(az_deg, el_deg):
    """Direction vector written out independently of active_geometry."""
    a, e = math.radians(az_deg), math.radians(el_deg)
    return np.array([math.cos(e) * math.sin(a), math.cos(e) * math.cos(a), math.sin(e)])


def _local_delay(x, d):
    """Independent circular FFT delay x(t - d), d in samples."""
    n = x.size
    k = np.fft.fftfreq(n)
    return np.fft.ifft(np.fft.fft(x) * np.exp(-2j * np.pi * k * d))


def _received(pos, u, fc, fs, s_bb):
    """Element signals of a far-field plane wave from u: the RF wavefront reaches element n
    at t_n = -u.r_n / c (earlier when closer to the source); after downconversion
    x_n(t) = s_bb(t - t_n) exp(-j 2 pi fc t_n)."""
    t_arr = -(pos @ u) / C0
    return np.stack([_local_delay(s_bb, t * fs) * np.exp(-2j * np.pi * fc * t) for t in t_arr])


def codes(issues, level=None):
    return [i.code for i in issues if level is None or i.level == level]


class TestConventions(unittest.TestCase):
    def test_plane_wave_matches_steering_ff_not_conj(self):
        g = G.preset_ura(3, 3, 0.4 * LAM, 0.45 * LAM)
        pos = g.positions()
        rng = rng_stream(1, "conv")
        s = D.bandlimited_noise(4096, 1e6, 1e4, rng)
        for az, el in ((37.0, 12.0), (-60.0, -25.0), (5.0, 70.0)):
            X = _received(pos, _local_u(az, el), F, 1e6, s)
            meas = np.array([D.complex_gain_ls(X[0], X[n]) for n in range(len(pos))])
            a = P.steering_ff(pos, G.direction_vector(az, el), F)
            err = np.degrees(np.abs(np.angle(meas / (a / a[0]))))
            self.assertLess(err.max(), 0.1)
            err_conj = np.degrees(np.abs(np.angle(meas / np.conj(a / a[0]))))
            self.assertGreater(err_conj.max(), 10.0)           # the conjugate model does not fit

    def test_near_field_converges_to_far_field(self):
        g = G.preset_ula(8, LAM / 2)
        pos = g.positions()
        u = G.direction_vector(25.0, 0.0)
        rff = g.fraunhofer_m(F)
        ff = P.steering_ff(pos, u, F)
        errs = []
        for k in (1.0, 10.0, 100.0):
            nf = P.steering_nf(pos, k * rff * u, F)
            errs.append(np.degrees(np.abs(np.angle(nf / ff))).max())
            if k >= 10.0:
                np.testing.assert_allclose(np.abs(nf), 1.0, atol=0.01)
        self.assertLess(errs[1], 10.0)                           # pi/20 rad bound at 10 R_ff
        self.assertLess(errs[2], 1.0)
        self.assertAlmostEqual(errs[1] / errs[2], 10.0, delta=0.5)   # ~ 1/R
        with self.assertRaises(ValueError):
            P.steering_nf(pos, pos[3], F)

    def test_ttd_sign_d1(self):
        g = G.preset_ula(8, LAM / 2)
        pos = g.positions()
        for az in (30.0, -50.0, 0.0):
            u0 = G.direction_vector(az, 0.0)
            tau = P.ttd_delays_s(pos, u0)
            self.assertGreaterEqual(tau.min(), 0.0)
            self.assertAlmostEqual(tau.min(), 0.0, places=18)
            r = np.exp(-2j * np.pi * F * tau) / np.conj(P.steering_ff(pos, u0, F))
            self.assertLess(np.abs(np.angle(r / r[0])).max(), 1e-9)      # common phase only

    def test_ttd_aligns_wideband_lfm(self):
        fs = 1e9                                                  # 3.5-sample aperture fill time
        g = G.preset_ula(8, LAM / 2)
        pos = g.positions()
        az = 60.0
        u0 = G.direction_vector(az, 0.0)
        s = np.concatenate([np.zeros(256), D.lfm(2048, fs, 0.5 * fs), np.zeros(256)])
        X = _received(pos, _local_u(az, 0.0), F, fs, s)
        tau = P.ttd_delays_s(pos, u0)
        Y = np.stack([D.frac_delay(X[n], tau[n] * fs) for n in range(len(pos))]) * \
            np.exp(-2j * np.pi * F * tau)[:, None]
        Yp = X * np.conj(P.steering_ff(pos, u0, F))[:, None]      # phase-only steering

        def worst_corr(Z):
            c = [abs(np.vdot(Z[0], Z[n])) / (np.linalg.norm(Z[0]) * np.linalg.norm(Z[n])) for n in range(1, len(Z))]
            return min(c)

        self.assertGreater(worst_corr(Y), 0.999)
        self.assertLess(worst_corr(Yp), 0.9)                       # without TTD the envelopes smear


class TestSteeringAndPatterns(unittest.TestCase):
    def test_steering_shapes_and_wideband(self):
        pos = G.preset_ura(2, 2, 0.1, 0.1).positions()
        az, el, U = P.az_el_grid(30, 30)
        self.assertEqual(U.shape, (7, 12, 3))
        self.assertEqual(P.steering_ff(pos, U, F).shape, (7, 12, 4))
        self.assertEqual(P.steering_ff(pos, U[0, 0], F).shape, (4,))
        freqs = np.array([0.9e9, 1e9, 1.1e9])
        W = P.steering_wb(pos, U[3, 4], freqs)
        for i, f in enumerate(freqs):
            np.testing.assert_allclose(W[i], P.steering_ff(pos, U[3, 4], f), atol=1e-12)
        az, el, _ = P.az_el_grid(10, 10, az_range=(-30, 30), el_range=(0, 20))
        self.assertEqual((az[0], az[-1], el[-1]), (-30.0, 30.0, 20.0))

    def _directivity_db(self, pat):
        _, _, U = P.az_el_grid(0.5, 0.5)
        az, el, _ = P.az_el_grid(0.5, 0.5)
        g2 = np.abs(P.element_gain(pat, U)) ** 2
        wts = np.cos(np.radians(el))[:, None] * math.radians(0.5) ** 2
        return 10 * math.log10(4 * math.pi * g2.max() / np.sum(g2 * wts))

    def test_element_gain_models(self):
        for q in (1.0, 2.0):
            pat = G.ElementPattern("cos_q", q=q)
            self.assertAlmostEqual(pat.effective_gain_dbi(), 10 * math.log10(2 * (q + 1)))
            self.assertAlmostEqual(self._directivity_db(pat), pat.effective_gain_dbi(), delta=0.05)
            self.assertAlmostEqual(abs(P.element_gain(pat, [0, 1, 0])) ** 2, 2 * (q + 1), places=9)
            self.assertEqual(abs(P.element_gain(pat, [0, -1, 0])), 0.0)
        dv = G.ElementPattern("dipole_v")
        self.assertAlmostEqual(self._directivity_db(dv), 2.15, delta=0.05)
        self.assertEqual(abs(P.element_gain(dv, [0, 0, 1])), 0.0)
        dh = G.ElementPattern("dipole_h")
        self.assertAlmostEqual(abs(P.element_gain(dh, [1, 0, 0])), 0.0, places=9)   # along its axis
        self.assertGreater(abs(P.element_gain(dh, [0, 0, 1])), 1.0)
        tilt = G.ElementPattern("cos_q", q=2, boresight_az_deg=90)
        self.assertAlmostEqual(abs(P.element_gain(tilt, [1, 0, 0])) ** 2, 6.0, places=9)
        iso = G.ElementPattern("isotropic", gain_dbi=3.0)
        self.assertAlmostEqual(abs(P.element_gain(iso, [0, 0, 1])), 10 ** (3 / 20))

    def test_table_pattern(self):
        cut = G.ElementPattern("table", table=((0, 0, 5.0, 0.0), (30, 0, 4.1, 2.0)))
        g = P.element_gain(cut, G.direction_vector([0, 15, 30, 195], [0, 0, 0, 0]))
        np.testing.assert_allclose(20 * np.log10(np.abs(g[:3])), [5.0, 4.55, 4.1], atol=1e-9)
        np.testing.assert_allclose(np.degrees(np.angle(g[:3])), [0.0, 1.0, 2.0], atol=1e-9)
        self.assertAlmostEqual(20 * math.log10(abs(g[3])), 4.55, places=9)   # periodic wrap 30 -> 360
        grid = G.ElementPattern("table", table=tuple((a, e, a / 10.0 + e / 5.0, 0.0)
                                                    for a in (0, 90, 180, 270) for e in (-30, 0, 30)))
        v = P.element_gain(grid, G.direction_vector(45, 15))
        self.assertAlmostEqual(20 * math.log10(abs(v)), 4.5 + 3.0, places=9)
        self.assertAlmostEqual(20 * math.log10(abs(P.element_gain(grid, G.direction_vector(45, 80)))), 4.5 + 6.0)
        shifted = G.ElementPattern("table", gain_dbi=2.0, table=((0, 0, 5.0, 0.0), (30, 0, 4.1, 2.0)))
        self.assertAlmostEqual(20 * math.log10(abs(P.element_gain(shifted, [0, 1, 0]))), 2.0)

    def test_manifold_uses_body_frame_patterns(self):
        els = [G.Element(i, (0.15 * i, 0, 0), rx=ChannelRef("s", i), pattern=G.ElementPattern("cos_q", q=2))
               for i in range(4)]
        g = G.ArrayGeometry(els, orientation=G.Orientation(heading_deg=90))
        A = P.array_manifold(g, G.direction_vector(90, 0), F)                   # body forward = East
        self.assertAlmostEqual(abs(A[0]) ** 2, 6.0, places=9)
        A0 = P.array_manifold(g, G.direction_vector(0, 0), F)                   # body left (-x)... edge
        self.assertAlmostEqual(abs(A0[0]), 0.0, places=9)
        np.testing.assert_allclose(P.array_manifold(g, G.direction_vector(90, 0), F, include_pattern=False),
                                   P.steering_ff(g.positions(), G.direction_vector(90, 0), F))

    def test_pattern_db_and_array_factor(self):
        g = G.preset_ula(8, LAM / 2)
        w = np.ones(8) / 8
        _, _, U = P.az_el_grid(1, 1)
        pdb = P.pattern_db(g, w, F, U)
        self.assertAlmostEqual(float(pdb.max()), 0.0, places=12)
        self.assertAlmostEqual(abs(P.array_factor(g.positions(), w, F, G.direction_vector(0, 0))), 1.0)
        self.assertLessEqual(float(pdb.min()), -60.0)


class TestMetrics(unittest.TestCase):
    def test_uniform_ula8_metrics(self):
        g = G.preset_ula(8, LAM / 2)
        u0 = G.direction_vector(0, 0)
        w = P.steering_ff(g.positions(), u0, F) / 8
        m = P.beam_metrics(g, w, F, u0)
        self.assertAlmostEqual(m.hpbw_az_deg, 12.8, delta=0.3)
        self.assertAlmostEqual(m.psll_db, -12.8, delta=0.3)
        self.assertAlmostEqual(m.directivity_dbi, 9.03, delta=0.1)
        self.assertAlmostEqual(m.pointing_error_deg, 0.0, delta=0.01)
        self.assertAlmostEqual(m.peak_az_deg, 0.0, delta=0.01)
        self.assertEqual(m.hpbw_el_deg, 360.0)                                  # cone: no el crossing
        self.assertAlmostEqual(m.taper_eff, 1.0, places=12)
        self.assertTrue(m.squint_ok)
        self.assertLess(m.islr_db, -5.0)
        num = P.directivity_numeric_dbi(g, w, F, u0, include_pattern=False)
        closed = P.directivity_isotropic_dbi(g.positions(), w, F, u0)
        self.assertAlmostEqual(closed, 10 * math.log10(8), places=6)
        self.assertLess(abs(10 ** (num / 10) / 10 ** (closed / 10) - 1.0), 0.01)
        d = json.loads(json.dumps(m.to_dict()))
        self.assertEqual(P.BeamMetrics.from_dict(d), m)

    def test_scanned_beam_and_cone_pointing(self):
        g = G.preset_ula(8, LAM / 2)
        for az in (30.0, -45.0):
            u0 = G.direction_vector(az, 0)
            w = P.steering_ff(g.positions(), u0, F) / 8
            m = P.beam_metrics(g, w, F, u0)
            self.assertLess(m.pointing_error_deg, 0.01)
            self.assertAlmostEqual(m.hpbw_az_deg, 12.8 / math.cos(math.radians(az)), delta=0.6)

    def test_planar_mirror_and_nulls(self):
        g = G.preset_ura(4, 4, LAM / 2, LAM / 2)                                # vertical panel facing North
        u0 = G.direction_vector(20, 10)
        a = P.steering_ff(g.positions(), u0, F)
        m = P.beam_metrics(g, a / 16, F, u0, nulls=((60, 0),))
        self.assertLess(m.pointing_error_deg, 0.01)
        self.assertTrue(any(lb.kind == "ambiguity" and lb.level_db > -0.1 and 150 < lb.az_deg < 190
                            for lb in m.lobes))                                  # back-side mirror image
        self.assertGreater(m.psll_db, -14.0)
        self.assertEqual(len(m.null_depths_db), 1)
        # cos^2 elements suppress the mirror: no ambiguity lobe, the directivity uses the pattern
        els = [G.Element(e.id, e.pos_body_m, rx=e.rx, pattern=G.ElementPattern("cos_q", q=2)) for e in g.elements]
        gp = G.ArrayGeometry(els)
        mp = P.beam_metrics(gp, a / 16, F, u0)
        self.assertFalse(any(lb.kind == "ambiguity" for lb in mp.lobes))
        self.assertGreater(mp.directivity_dbi, m.directivity_dbi + 2.0)

    def test_null_floor(self):
        self.assertAlmostEqual(P.cal_limited_null_floor_db(1.0, 0.1, np.ones(8)), -42.6, delta=0.5)
        self.assertEqual(P.cal_limited_null_floor_db(0.0, 0.0, np.ones(8)), P.FLOOR_DB)
        tapered = P.cal_limited_null_floor_db(1.0, 0.1, np.hamming(8))
        self.assertGreater(tapered, -42.6)                                      # eta_t < 1 raises the floor

    def test_taper_efficiency(self):
        self.assertAlmostEqual(P.taper_efficiency(np.ones(8)), 1.0)
        w = np.array([0.5, 1, 1, 0.5])
        self.assertAlmostEqual(P.taper_efficiency(w), 9.0 / (4 * 2.5))
        a0 = np.exp(1j * np.arange(4))
        self.assertAlmostEqual(P.taper_efficiency(w * a0, a0), 9.0 / (4 * 2.5))

    def test_squint_and_narrowband(self):
        self.assertAlmostEqual(P.squint_deg(30.0, F, F), 0.0)
        f = 1.05 * F
        expected = P.squint_deg(30.0, F, f)
        self.assertLess(expected, 0.0)                                          # higher f -> toward broadside
        g = G.preset_ula(16, LAM / 2)
        u0 = G.direction_vector(30, 0)
        w = P.steering_ff(g.positions(), u0, F) / 16                             # phase weights at f0
        m = P.beam_metrics(g, w, f, u0)
        self.assertAlmostEqual(m.peak_az_deg - 30.0, expected, delta=0.05)
        self.assertTrue(P.narrowband_ok(1.0, 10e6, 30.0))                       # 10e6 * 0.5 / c = 0.017
        self.assertFalse(P.narrowband_ok(1.0, 100e6, 60.0))                     # 0.29
        self.assertTrue(P.narrowband_ok(1.0, 100e6, 0.0))
        m2 = P.beam_metrics(G.preset_ula(8, 1.0), np.ones(8) / 8 * P.steering_ff(
            G.preset_ula(8, 1.0).positions(), G.direction_vector(30, 0), 1.5e8), 1.5e8,
            G.direction_vector(30, 0), bw_hz=20e6)
        self.assertAlmostEqual(m2.fill_time_bw_product, 20e6 * 7 * 0.5 / C0, places=9)
        self.assertFalse(m2.squint_ok)


class TestGratingAndAmbiguity(unittest.TestCase):
    def test_lattice_grating_lobes(self):
        d1 = np.array([LAM, 0, 0])
        lobes = P.lattice_grating_lobes(d1, None, F, G.direction_vector(0, 0))
        self.assertEqual(sorted(round(lb.az_deg) for lb in lobes), [90, 270])
        self.assertEqual(P.lattice_grating_lobes(d1 / 2, None, F, G.direction_vector(0, 0)), [])
        self.assertEqual(len(P.lattice_grating_lobes(d1 / 2, None, F, G.direction_vector(90, 0))), 1)
        lobes = P.lattice_grating_lobes([0.7 * LAM, 0, 0], [0, 0, 0.7 * LAM], F, G.direction_vector(40, 0))
        self.assertTrue(lobes)
        for lb in lobes:
            u = G.direction_vector(lb.az_deg, lb.el_deg)
            self.assertAlmostEqual(np.linalg.norm(u), 1.0)
            self.assertLess(u[1] * G.direction_vector(40, 0)[1] * -1, 1e-9)    # same (front) side
        self.assertAlmostEqual(P.max_scan_free_deg(G.preset_ula(8, 0.6 * LAM), F),
                               math.degrees(math.asin(1 / 0.6 - 1)), places=6)
        self.assertIsNone(P.max_scan_free_deg(G.preset_uca(5, radius_m=0.2), F))

    def test_numeric_search_ula(self):
        lobes = P.numeric_ambiguity_search(G.preset_ula(8, LAM), F, 0.0)
        self.assertTrue(lobes)
        self.assertTrue(all(lb.kind == "grating" and lb.level_db > -0.01 for lb in lobes))
        self.assertEqual(sorted(round(lb.az_deg) for lb in lobes), [90, 270])  # endfire at broadside
        self.assertEqual(P.numeric_ambiguity_search(G.preset_ula(8, LAM / 2), F, 60.0), [])
        lobes = P.numeric_ambiguity_search(G.preset_ula(8, 0.7 * LAM), F, 45.0)
        self.assertTrue(any(lb.level_db >= -3.0 for lb in lobes))
        self.assertTrue(all(lb.scan_az_deg is not None for lb in lobes))

    def test_numeric_search_uca(self):
        self.assertEqual(P.numeric_ambiguity_search(G.preset_uca(5, arc_spacing_m=LAM / 2), F, 90.0), [])
        lobes = P.numeric_ambiguity_search(G.preset_uca(5, arc_spacing_m=LAM), F, 90.0)
        self.assertTrue(any(lb.kind == "ambiguity" and lb.level_db >= -3.0 for lb in lobes))

    def test_numeric_search_3d(self):
        rng = np.random.default_rng(9)
        P3 = rng.uniform(-0.5, 0.5, (6, 3)) * LAM
        g = G.ArrayGeometry([G.Element(i, tuple(p), rx=ChannelRef("s", i)) for i, p in enumerate(P3)])
        self.assertEqual(g.rank(), 3)
        lobes = P.numeric_ambiguity_search(g, F, 60.0)
        self.assertTrue(all(lb.level_db < 0.0 for lb in lobes))
        sparse = G.ArrayGeometry([G.Element(i, tuple(p * 6), rx=ChannelRef("s", i)) for i, p in enumerate(P3)])
        self.assertTrue(P.numeric_ambiguity_search(sparse, F, 60.0))

    def test_geometry_report(self):
        rep = P.geometry_report(G.preset_ula(8, 0.6 * LAM), F, F, 60.0)
        self.assertIn("GRATING_LOBE", codes(rep.issues, "error"))
        self.assertIn("AMBIGUITY", codes(rep.issues, "error"))
        self.assertEqual((rep.lattice, rep.rank, rep.ambiguity, rep.n), ("ula", 1, "cone", 8))
        self.assertAlmostEqual(rep.grating_free_scan_deg, 41.81, delta=0.01)
        json.dumps(rep.to_dict())
        ok = P.geometry_report(G.preset_ula(8, LAM / 2), 0.95 * F, F, 45.0)
        self.assertEqual(codes(ok.issues, "error"), [])
        self.assertAlmostEqual(ok.position_tolerance_1deg_m, LAM / 360)
        self.assertLess(ok.worst_psll_db, -10.0)
        self.assertAlmostEqual(ok.fraunhofer_m, 2 * (3.5 * LAM) ** 2 / LAM, places=9)
        edge = P.geometry_report(G.preset_ura(4, 4, LAM / 2, LAM / 2), F, F, 60.0)
        self.assertEqual(codes(edge.issues, "error"), [])                    # endfire shoulder only warns
        self.assertTrue(any("edge of visible space" in i.message for i in edge.issues if i.level == "warning"))
        tx = P.geometry_report(G.preset_ula(4, 0.1, map_tx=False), F, F, 60.0, which="tx")
        self.assertEqual((tx.n, tx.issues), (0, []))


if __name__ == "__main__":
    unittest.main()
