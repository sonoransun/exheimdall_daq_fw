"""
    Unit tests for the active array DSP primitives (Firmware/_active/active_dsp.py).

    Hardware-free, seeded, small sizes.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import sys
import unittest
from os.path import dirname, join, realpath

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_dsp as D  # noqa: E402
from active_common import rng_stream  # noqa: E402


def _tones(n, freqs, amps, t_offset=0.0):
    """Analytic sum of complex tones at (fractional) sample times n - t_offset."""
    t = np.arange(n)[:, None] - t_offset
    return (amps[None, :] * np.exp(2j * np.pi * freqs[None, :] * t)).sum(axis=1)


def _cn(rng, *shape):
    return (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) / np.sqrt(2.0)


class TestDelays(unittest.TestCase):
    def test_fft_delay_exact_on_periodic_bandlimited_signal(self):
        rng = rng_stream(1, "fft")
        n = 1024
        bins = rng.choice(np.arange(-400, 400), 24, replace=False)
        freqs = bins / n                                     # on-bin: periodic, band-limited
        amps = _cn(rng, 24)
        x = _tones(n, freqs, amps)
        for d in (3.37, -5.81, 0.5, 12.0):
            y = D.frac_delay_fft(x, d)
            ref = _tones(n, freqs, amps, t_offset=d)
            err = np.sqrt(np.mean(np.abs(y - ref) ** 2)) / np.sqrt(np.mean(np.abs(ref) ** 2))
            self.assertLess(err, 1e-3)
            self.assertLess(err, 1e-10)                      # exact phase ramp

    def test_fft_delay_sign_and_integer_shift(self):
        x = np.zeros(64, complex)
        x[10] = 1.0
        y = D.frac_delay_fft(x, 5)                           # positive = later
        self.assertEqual(int(np.argmax(np.abs(y))), 15)
        np.testing.assert_allclose(y, np.roll(x, 5), atol=1e-12)

    def test_fft_delay_per_row(self):
        rng = rng_stream(2, "rows")
        X = _cn(rng, 3, 256)
        d = np.array([0.25, -1.5, 7.0])
        Y = D.frac_delay_fft(X, d)
        for m in range(3):
            np.testing.assert_allclose(Y[m], D.frac_delay_fft(X[m], d[m]), atol=1e-12)
        with self.assertRaises(ValueError):
            D.frac_delay_fft(X, np.zeros(2))
        # axis=0
        Y0 = D.frac_delay_fft(X.T, d, axis=0)
        np.testing.assert_allclose(Y0.T, Y, atol=1e-12)

    def test_fir_delay_accuracy_away_from_edges(self):
        rng = rng_stream(3, "fir")
        n = 3000
        freqs = rng.uniform(-0.4, 0.4, 20)                   # off-bin, band-limited to 0.4 cyc/sample
        amps = _cn(rng, 20)
        x = _tones(n, freqs, amps)
        for d in (3.37, -2.6, 0.49, 10.0):
            y = D.frac_delay_fir(x, d)
            ref = _tones(n, freqs, amps, t_offset=d)
            sl = slice(100, n - 100)
            err = np.sqrt(np.mean(np.abs(y[sl] - ref[sl]) ** 2)) / np.sqrt(np.mean(np.abs(ref[sl]) ** 2))
            self.assertLess(err, 1e-3)
        self.assertEqual(D.frac_delay_fir(x, 1.5).shape, x.shape)

    def test_fir_delay_linear_and_per_row(self):
        x = np.zeros(64, complex)
        x[:4] = 1.0
        y = D.frac_delay_fir(x, -10)                         # advanced out of the block: zeros, no wrap
        self.assertTrue(np.allclose(y[-20:], 0))
        X = np.vstack([x, 2 * x])
        Y = D.frac_delay_fir(X, np.array([1.0, 3.0]))
        np.testing.assert_allclose(Y[1], 2 * D.frac_delay_fir(x, 3.0), atol=1e-12)
        with self.assertRaises(ValueError):
            D.frac_delay_fir(x, 0.2, ntaps=64)
        with self.assertRaises(ValueError):
            D.frac_delay_fir(x, float("nan"))

    def test_frac_delay_dispatch(self):
        x = _cn(rng_stream(4, "d"), 128)
        np.testing.assert_allclose(D.frac_delay(x, 1.3), D.frac_delay_fft(x, 1.3))
        np.testing.assert_allclose(D.frac_delay(x, 1.3, method="fir"), D.frac_delay_fir(x, 1.3))
        with self.assertRaises(ValueError):
            D.frac_delay(x, 1.0, method="cubic")

    def test_integer_shift(self):
        x = np.arange(1, 6)
        np.testing.assert_array_equal(D.integer_shift(x, 2), [0, 0, 1, 2, 3])
        np.testing.assert_array_equal(D.integer_shift(x, -2, fill=9), [3, 4, 5, 9, 9])
        np.testing.assert_array_equal(D.integer_shift(x, 7), [0] * 5)
        X = np.vstack([x, x])
        np.testing.assert_array_equal(D.integer_shift(X, 1)[1], [0, 1, 2, 3, 4])


class TestEstimators(unittest.TestCase):
    def test_xcorr_and_xspec_delay_at_20db(self):
        fs = 1e6
        worst_x, worst_s = 0.0, 0.0
        for trial in range(8):
            rng = rng_stream(10, "xspec", trial)
            s = D.bandlimited_noise(16384, fs, 0.8 * fs, rng)
            tau = rng.uniform(-6.0, 6.0)
            g = np.exp(1j * rng.uniform(-np.pi, np.pi))
            sig = 10 ** (-20 / 20)                           # 20 dB SNR
            xr = s + sig * _cn(rng, s.size)
            x = g * D.frac_delay_fft(s, tau) + sig * _cn(rng, s.size)
            lag, frac, pk = D.xcorr_delay(xr, x, 32)
            self.assertEqual(lag, int(np.round(tau)))
            self.assertTrue(-0.5 <= frac <= 0.5)
            self.assertGreater(pk, 0.6)                      # off-grid peak loses up to ~4 dB
            worst_x = max(worst_x, abs(lag + frac - tau))
            d, ph, coh = D.xspec_delay(xr, x, fs)
            worst_s = max(worst_s, abs(d * fs - tau))
            self.assertAlmostEqual(np.angle(np.exp(1j * (ph - np.angle(g)))), 0.0, delta=np.radians(1.0))
            self.assertGreater(coh, 0.9)
        self.assertLess(worst_x, 0.25)                       # parabolic refinement: coarse
        self.assertLess(worst_s, 0.01)                       # spec: < 0.01 samples at 20 dB

    def test_xspec_no_coherence_falls_back(self):
        rng = rng_stream(11, "nc")
        a, b = _cn(rng, 4096), _cn(rng, 4096)
        d, ph, coh = D.xspec_delay(a, b, 1e6)
        self.assertLess(coh, 0.5)
        with self.assertRaises(ValueError):
            D.xspec_delay(a[:8], b[:8], 1e6)
        with self.assertRaises(ValueError):
            D.xspec_delay(a, b, 0.0)

    def test_complex_gain_ls(self):
        rng = rng_stream(12, "ls")
        x = _cn(rng, 500)
        g = 0.7 * np.exp(1j * 0.4)
        self.assertAlmostEqual(abs(D.complex_gain_ls(x, g * x) - g), 0.0, places=12)
        with self.assertRaises(ValueError):
            D.complex_gain_ls(np.zeros(4), np.ones(4))

    def test_complex_gains_eig(self):
        rng = rng_stream(13, "eig")
        m, n = 8, 4096
        s = _cn(rng, n)
        g = 10 ** (rng.normal(0, 1, m) / 20) * np.exp(1j * rng.uniform(-np.pi, np.pi, m))
        sig = 10 ** (-20 / 20)
        X = g[:, None] * s[None, :] + sig * _cn(rng, m, n)
        est, resid = D.complex_gains_eig(X, ref=2)
        self.assertAlmostEqual(abs(est[2] - 1.0), 0.0, places=12)
        rel = g / g[2]
        perr = np.degrees(np.abs(np.angle(est / rel)))
        self.assertLess(perr.max(), 0.2)
        self.assertLess(np.abs(20 * np.log10(np.abs(est / rel))).max(), 0.05)
        # residual ~ noise / total power: about -20 dB minus the array of 8 coherent channels
        self.assertTrue(-25.0 < resid < -15.0, resid)
        _, clean = D.complex_gains_eig(g[:, None] * s[None, :])
        self.assertLess(clean, -100.0)
        with self.assertRaises(ValueError):
            D.complex_gains_eig(X, ref=9)


class TestWaveforms(unittest.TestCase):
    def test_zadoff_chu(self):
        for n, u in ((63, 1), (139, 5), (8191, 17)):
            z = D.zadoff_chu(n, u)
            np.testing.assert_allclose(np.abs(z), 1.0, atol=1e-12)
            r = np.fft.ifft(np.fft.fft(z) * np.conj(np.fft.fft(z)))      # periodic autocorrelation
            self.assertAlmostEqual(abs(r[0]), n, places=6)
            self.assertLess(np.abs(r[1:]).max(), 1e-8 * n)
        with self.assertRaises(ValueError):
            D.zadoff_chu(64)
        with self.assertRaises(ValueError):
            D.zadoff_chu(63, 3)                              # gcd(3, 63) = 3

    def test_hadamard_orthogonality(self):
        for n_tx, L in ((1, 1), (2, 2), (5, 8), (8, 8), (9, 16)):
            H = D.hadamard_codes(n_tx)
            self.assertEqual(H.shape, (L, n_tx))
            self.assertTrue(set(np.unique(H)) <= {-1.0, 1.0})
            np.testing.assert_array_equal(H.T @ H, L * np.eye(n_tx))
        with self.assertRaises(ValueError):
            D.hadamard_codes(0)

    def test_ramp_window(self):
        x = np.ones((2, 100), complex)
        y = D.ramp_window(x, 16)
        self.assertEqual(y[0, 0], 0.0)
        self.assertEqual(y[1, -1], 0.0)
        np.testing.assert_array_equal(y[:, 16:84], 1.0)
        self.assertTrue(np.all(np.diff(y[0, :16].real) > 0))
        np.testing.assert_array_equal(x, 1.0)                # input unchanged
        np.testing.assert_array_equal(D.ramp_window(x, 0), x)
        with self.assertRaises(ValueError):
            D.ramp_window(x, 51)

    def test_scale_to_peak_exact(self):
        rng = rng_stream(14, "pk")
        X = _cn(rng, 4, 1000) * 3.7
        for target in (0.5, 0.123456789, 1.0):
            Y = D.scale_to_peak(X, target)
            self.assertLessEqual(float(np.abs(Y).max()), target)
            self.assertAlmostEqual(float(np.abs(Y).max()), target, places=15)
        np.testing.assert_array_equal(D.scale_to_peak(np.zeros(4), 0.5), 0.0)
        with self.assertRaises(ValueError):
            D.scale_to_peak(X, -1.0)

    def test_peak_rms_papr(self):
        t = D.tone(4096, 1e6, 12345.0, amp=0.3, phase=0.2)
        self.assertAlmostEqual(D.papr_db(t), 0.0, places=9)
        self.assertAlmostEqual(float(D.peak(t)), 0.3, places=12)
        self.assertAlmostEqual(float(D.rms(t)), 0.3, places=12)
        n = D.bandlimited_noise(4096, 1e6, 0.5e6, rng_stream(15, "n"))
        self.assertGreater(D.papr_db(n), 6.0)
        X = np.vstack([t, n])
        self.assertEqual(np.shape(D.papr_db(X)), (2,))
        self.assertAlmostEqual(float(D.papr_db(X)[0]), 0.0, places=9)

    def test_lfm_and_noise(self):
        fs, bw, n = 1e6, 0.5e6, 2048
        c = D.lfm(n, fs, bw)
        np.testing.assert_allclose(np.abs(c), 1.0, atol=1e-12)
        finst = np.diff(np.unwrap(np.angle(c))) * fs / (2 * np.pi)
        self.assertAlmostEqual(finst[0], -bw / 2, delta=bw / n * 2)
        self.assertAlmostEqual(finst[-1], bw / 2, delta=bw / n * 2)
        x = D.bandlimited_noise(n, fs, bw, rng_stream(16, "bl"))
        self.assertAlmostEqual(float(D.rms(x)), 1.0, places=12)
        f = np.fft.fftfreq(n, 1 / fs)
        self.assertLess(np.abs(np.fft.fft(x)[np.abs(f) > bw / 2]).max(), 1e-9)
        x2 = D.bandlimited_noise(n, fs, bw, rng_stream(16, "bl"))
        np.testing.assert_array_equal(x, x2)                 # deterministic
        with self.assertRaises(TypeError):
            D.bandlimited_noise(n, fs, bw, 1234)
        with self.assertRaises(ValueError):
            D.lfm(n, fs, 2 * fs)


class TestFormats(unittest.TestCase):
    def test_sc16_round_trip_and_saturation(self):
        rng = rng_stream(17, "sc16")
        v = rng.integers(-32768, 32768, size=(3, 64), dtype=np.int16)
        x = D.sc16_to_cf32(v)
        self.assertEqual(x.dtype, np.complex64)
        self.assertEqual(x.shape, (3, 32))
        np.testing.assert_array_equal(D.cf32_to_sc16(x, scale=32768), v)
        z = (_cn(rng, 200) * 0.3).astype(np.complex64)
        back = D.sc16_to_cf32(D.cf32_to_sc16(z), scale=32767)
        self.assertLessEqual(float(np.abs(back - z).max()), 0.75 / 32767)
        s = D.cf32_to_sc16(np.array([1.5 - 1.5j, -2.0 + 0.0j, np.nan + 1j]))
        np.testing.assert_array_equal(s, [32767, -32768, -32768, 0, 0, 32767])
        # round half to even
        h = D.cf32_to_sc16(np.array([0.5 + 1.5j, 2.5 - 0.5j]), scale=1)
        np.testing.assert_array_equal(h, [0, 2, 2, 0])
        self.assertEqual(h.dtype, np.int16)
        with self.assertRaises(ValueError):
            D.sc16_to_cf32(np.zeros(3, np.int16))

    def test_overdrive_mask(self):
        X = np.zeros((40, 16), complex)
        X[0, 3] = 0.995
        X[3, 1] = -0.99j
        X[5, 2] = 0.98 + 0.98j                               # |I|,|Q| below 0.99
        X[35, 0] = 1.0                                       # row >= 32 ignored
        self.assertEqual(D.overdrive_mask(X), 0b1001)
        self.assertEqual(D.overdrive_mask(X, thresh=0.97), 0b101001)
        self.assertEqual(D.overdrive_mask(np.array([0.1, -1.0])), 1)


if __name__ == "__main__":
    unittest.main()
