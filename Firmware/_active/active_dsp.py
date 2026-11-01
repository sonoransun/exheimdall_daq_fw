"""
    HeIMDALL "active" capability: array DSP primitives.

    Pure numpy/scipy helpers shared by beamforming, calibration, the simulation
    backend and the TX safety layer:

      - fractional / integer delays (FFT phase ramp, Kaiser-windowed sinc FIR);
      - delay estimators (cross-correlation with parabolic refinement, and a
        coherence-weighted cross-spectrum slope fit);
      - complex-gain estimators (least squares, principal eigenvector);
      - waveforms: tone, Zadoff-Chu, LFM, band-limited noise, Hadamard codes;
      - burst ramps, peak / rms / PAPR, peak scaling;
      - sc16 <-> cf32 conversion and the ADC overdrive mask.

    Conventions (frozen, spec section 2):
      - a positive delay means "later": ``frac_delay_fft(x, d)`` returns
        ``x(t - d)`` (d in samples);
      - a measured chain delay (positive = late) is corrected by delaying by
        its negative;
      - complex128 / float64 internally; complex64 / int16 only at the device
        and wire boundaries.

    No hardware access.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import math

import numpy as np
from scipy.linalg import hadamard as _sylvester_hadamard
from scipy.special import i0 as _bessel_i0

__all__ = [
    "frac_delay", "frac_delay_fft", "frac_delay_fir", "integer_shift",
    "xcorr_delay", "xspec_delay", "complex_gain_ls", "complex_gains_eig",
    "tone", "zadoff_chu", "lfm", "bandlimited_noise", "hadamard_codes",
    "ramp_window", "scale_to_peak", "peak", "rms", "papr_db",
    "cf32_to_sc16", "sc16_to_cf32", "overdrive_mask",
]

FIR_KAISER_BETA = 8.0          # ~3e-5 relative error up to 0.4 cycles/sample at 63 taps


# ---------------------------------------------------------------------------
# Delays
# ---------------------------------------------------------------------------
def _delay_vector(delay_samples, x: np.ndarray, axis: int) -> np.ndarray:
    """Broadcast scalar or per-row delays against ``x`` with ``axis`` kept as size 1."""
    d = np.asarray(delay_samples, dtype=float)
    if not np.all(np.isfinite(d)):
        raise ValueError("delays must be finite")
    if d.ndim == 0:
        return d
    other = list(x.shape)
    del other[axis]
    if tuple(d.shape) != tuple(other):
        raise ValueError("per-row delays have shape %s, expected %s (x.shape without axis %d)"
                         % (d.shape, tuple(other), axis))
    return np.expand_dims(d, axis)


def frac_delay_fft(x, delay_samples, axis=-1) -> np.ndarray:
    """Circular fractional delay by an exact FFT phase ramp: returns ``x(t - d)``.

    ``delay_samples`` is a scalar or one delay per row (shape of ``x`` without
    ``axis``). Exact for periodic (circular) blocks; integer delays reduce to
    an exact circular shift. Returns complex128.
    """
    x = np.asarray(x)
    if x.ndim == 0:
        raise ValueError("x must be at least 1-D")
    axis = axis % x.ndim
    n = x.shape[axis]
    if n == 0:
        return x.astype(np.complex128)
    d = _delay_vector(delay_samples, x, axis)
    shape = [1] * x.ndim
    shape[axis] = n
    f = np.fft.fftfreq(n).reshape(shape)          # cycles / sample
    X = np.fft.fft(x.astype(np.complex128, copy=False), axis=axis)
    return np.fft.ifft(X * np.exp(-2j * np.pi * f * d), axis=axis)


def _kaiser(t, half_len, beta):
    r = np.clip(1.0 - (t / half_len) ** 2, 0.0, None)
    return _bessel_i0(beta * np.sqrt(r)) / _bessel_i0(beta)


def _fir_taps(frac: float, ntaps: int, beta: float) -> np.ndarray:
    k = np.arange(ntaps) - (ntaps - 1) // 2
    t = k - frac
    h = np.sinc(t) * _kaiser(t, (ntaps - 1) / 2.0 + 1.0, beta)
    return h / h.sum()                            # unit DC gain


def frac_delay_fir(x, delay_samples: float, ntaps: int = 63, beta: float = FIR_KAISER_BETA) -> np.ndarray:
    """Linear (non-circular) fractional delay with a Kaiser-windowed sinc FIR.

    Returns ``x(t - d)`` with the same length along the last axis. Samples
    shifted in from outside the block are zero, so the first/last
    ``ntaps // 2 + |d|`` samples carry edge transients. ``delay_samples`` is a
    scalar or one delay per row. Streaming-friendly: the taps depend only on
    the fractional part of the delay.
    """
    ntaps = int(ntaps)
    if ntaps < 3 or ntaps % 2 == 0:
        raise ValueError("ntaps must be odd and >= 3, got %d" % ntaps)
    x = np.asarray(x)
    if x.ndim == 0:
        raise ValueError("x must be at least 1-D")
    xc = x.astype(np.complex128, copy=False)
    d = np.asarray(delay_samples, dtype=float)
    if not np.all(np.isfinite(d)):
        raise ValueError("delays must be finite")
    rows = xc.reshape(-1, xc.shape[-1])
    if d.ndim == 0:
        dl = np.full(rows.shape[0], float(d))
    else:
        if tuple(d.shape) != tuple(xc.shape[:-1]):
            raise ValueError("per-row delays have shape %s, expected %s" % (d.shape, xc.shape[:-1]))
        dl = d.reshape(-1)
    n = rows.shape[1]
    half = (ntaps - 1) // 2
    out = np.zeros_like(rows)
    for i in range(rows.shape[0]):
        di = int(np.round(dl[i]))
        h = _fir_taps(dl[i] - di, ntaps, beta)
        c = np.convolve(rows[i], h)               # c[j] = sum_k h[k] x[j-k]
        idx = np.arange(n) + half - di            # y[m] = c[m - D + half]
        ok = (idx >= 0) & (idx < c.size)
        out[i, ok] = c[idx[ok]]
    return out.reshape(xc.shape)


def frac_delay(x, delay_samples, axis=-1, method: str = "fft", ntaps: int = 63) -> np.ndarray:
    """``x(t - d)`` with ``method`` "fft" (circular, exact) or "fir" (linear)."""
    if method == "fft":
        return frac_delay_fft(x, delay_samples, axis=axis)
    if method == "fir":
        if axis not in (-1, np.asarray(x).ndim - 1):
            raise ValueError("frac_delay_fir works along the last axis only")
        return frac_delay_fir(x, delay_samples, ntaps=ntaps)
    raise ValueError("method must be 'fft' or 'fir', got %r" % (method,))


def integer_shift(x, n: int, fill=0) -> np.ndarray:
    """Shift along the last axis by ``n`` samples (positive = later), filling with ``fill``."""
    x = np.asarray(x)
    n = int(n)
    out = np.full(x.shape, fill, dtype=np.result_type(x.dtype, np.asarray(fill).dtype))
    m = x.shape[-1]
    if abs(n) >= m:
        return out
    if n >= 0:
        out[..., n:] = x[..., :m - n]
    else:
        out[..., :m + n] = x[..., -n:]
    return out


# ---------------------------------------------------------------------------
# Delay and gain estimation
# ---------------------------------------------------------------------------
def _xcorr_full(x_ref, x, max_lag):
    """r[l] = sum_n x[n + l] conj(x_ref[n]) for l in -max_lag..max_lag (linear)."""
    n = max(x_ref.size, x.size)
    nfft = 1 << int(math.ceil(math.log2(max(2, n + max_lag + 1))))
    R = np.fft.ifft(np.fft.fft(x, nfft) * np.conj(np.fft.fft(x_ref, nfft)))
    lags = np.arange(-max_lag, max_lag + 1)
    return lags, R[lags % nfft]


def xcorr_delay(x_ref, x, max_lag: int) -> tuple:
    """Delay of ``x`` relative to ``x_ref`` (positive = ``x`` is late).

    Returns ``(int_lag, frac, peak_norm)``: the integer lag of the
    cross-correlation magnitude peak within ``+-max_lag``, a parabolic
    refinement ``frac`` in [-0.5, 0.5] and the normalized peak
    ``|r| / (||x|| ||x_ref||)`` in [0, 1].
    """
    x_ref = np.asarray(x_ref, dtype=np.complex128).ravel()
    x = np.asarray(x, dtype=np.complex128).ravel()
    if x_ref.size == 0 or x.size == 0:
        raise ValueError("empty input")
    max_lag = int(max_lag)
    if max_lag < 0:
        raise ValueError("max_lag must be >= 0")
    max_lag = min(max_lag, max(x_ref.size, x.size) - 1)
    lags, r = _xcorr_full(x_ref, x, max_lag)
    mag = np.abs(r)
    i = int(np.argmax(mag))
    frac = 0.0
    if 0 < i < mag.size - 1:
        a, b, c = mag[i - 1], mag[i], mag[i + 1]
        den = a - 2.0 * b + c
        if den < 0.0:
            frac = float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))
    nrm = float(np.linalg.norm(x) * np.linalg.norm(x_ref))
    peak_norm = float(mag[i] / nrm) if nrm > 0 else 0.0
    return int(lags[i]), frac, peak_norm


def _default_nfft(n: int) -> int:
    nfft = 1 << int(math.floor(math.log2(max(64, n // 8))))
    return int(min(4096, max(64, nfft)))


def xspec_delay(x_ref, x, fs_hz, nfft=None, min_coherence=0.5) -> tuple:
    """Coherence-weighted least-squares cross-spectrum slope delay estimate.

    Model: ``x(t) = g * x_ref(t - tau)``. The integer part of tau comes from
    ``xcorr_delay``; the overlap of the two records is then split into Welch
    segments (Hann, 50 % overlap) and the phase of the averaged cross
    spectrum is fitted with a straight line over the bins whose
    magnitude-squared coherence is >= ``min_coherence`` (weights
    ``gamma^2 / (1 - gamma^2)``, the inverse phase variance).

    Returns ``(delay_s, phase0_rad, mean_coherence)``: ``phase0_rad`` is
    ``arg(g)`` and ``mean_coherence`` the mean coherence of the bins used. If
    fewer than 3 bins qualify, the cross-correlation estimate is returned
    with the mean coherence over all bins (which is then < ``min_coherence``:
    the slope fit was not used).
    """
    fs = float(fs_hz)
    if not fs > 0.0:
        raise ValueError("fs_hz must be > 0")
    x_ref = np.asarray(x_ref, dtype=np.complex128).ravel()
    x = np.asarray(x, dtype=np.complex128).ravel()
    n = min(x_ref.size, x.size)
    if n < 16:
        raise ValueError("records too short for a cross-spectrum estimate (%d samples)" % n)
    x_ref, x = x_ref[:n], x[:n]
    lag, frac, _ = xcorr_delay(x_ref, x, n // 2)
    # overlap: x[m + lag] ~ g * x_ref[m]  (x is late by lag samples)
    if lag >= 0:
        xa, ra = x[lag:], x_ref[:n - lag]
    else:
        xa, ra = x[:n + lag], x_ref[-lag:]
    m = xa.size
    seg = int(nfft) if nfft else _default_nfft(m)
    seg = max(16, min(seg, m))
    hop = max(1, seg // 2)
    win = np.hanning(seg + 2)[1:-1]
    starts = range(0, m - seg + 1, hop)
    Sxy = np.zeros(seg, dtype=np.complex128)
    Sxx = np.zeros(seg)
    Syy = np.zeros(seg)
    nseg = 0
    for s in starts:
        A = np.fft.fft(win * xa[s:s + seg])
        Bf = np.fft.fft(win * ra[s:s + seg])
        Sxy += A * np.conj(Bf)
        Sxx += np.abs(A) ** 2
        Syy += np.abs(Bf) ** 2
        nseg += 1
    f = np.fft.fftfreq(seg)                       # cycles / sample
    with np.errstate(divide="ignore", invalid="ignore"):
        coh = np.where((Sxx > 0) & (Syy > 0), np.abs(Sxy) ** 2 / (Sxx * Syy), 0.0)
    coh = np.clip(np.nan_to_num(coh), 0.0, 1.0)
    if nseg < 2:
        # single segment: coherence is identically 1 -> weight by cross power instead
        coh_w = np.abs(Sxy) / max(np.abs(Sxy).max(), 1e-300)
        use = coh_w >= 1e-3
        weights = coh_w ** 2
    else:
        use = coh >= float(min_coherence)
        weights = coh / np.maximum(1.0 - coh, 1e-6)
    if np.count_nonzero(use) < 3:
        return ((lag + frac) / fs, float(np.angle(Sxy.sum())), float(coh.mean()))
    fu, wu, su = f[use], weights[use], Sxy[use]
    if not wu.sum() > 0.0:
        return ((lag + frac) / fs, float(np.angle(Sxy.sum())), float(coh.mean()))
    # remove the weighted mean phase, then fit the residual (no wrapping for |frac| <= 0.5)
    slope = 0.0
    phi0 = float(np.angle(np.sum(wu * su / np.maximum(np.abs(su), 1e-300))))
    for _ in range(3):
        model = phi0 + slope * fu
        resid = np.angle(su * np.exp(-1j * model))
        W = wu.sum()
        fm = np.sum(wu * fu) / W
        rm = np.sum(wu * resid) / W
        var = np.sum(wu * (fu - fm) ** 2)
        ds = np.sum(wu * (fu - fm) * (resid - rm)) / var if var > 0 else 0.0
        slope += ds
        phi0 += rm - ds * fm
    tau_frac = -slope / (2.0 * np.pi)              # samples
    mean_coh = float(coh[use].mean()) if nseg >= 2 else 1.0
    phase0 = float(np.angle(np.exp(1j * phi0)))
    return ((lag + tau_frac) / fs, phase0, mean_coh)


def complex_gain_ls(x_ref, x) -> complex:
    """Least-squares complex gain g in ``x ~ g * x_ref``: (x_ref^H x)/(x_ref^H x_ref)."""
    x_ref = np.asarray(x_ref, dtype=np.complex128).ravel()
    x = np.asarray(x, dtype=np.complex128).ravel()
    if x_ref.size != x.size:
        raise ValueError("length mismatch %d != %d" % (x_ref.size, x.size))
    den = np.vdot(x_ref, x_ref).real
    if not den > 0.0:
        raise ValueError("reference has zero energy")
    return complex(np.vdot(x_ref, x) / den)


def complex_gains_eig(X, ref: int = 0) -> tuple:
    """Relative complex gains from the principal eigenvector of X X^H.

    ``X`` (M, N) holds M channels observing one common signal. Returns
    ``(g, residual_db)`` with ``g[ref] == 1`` and
    ``residual_db = 10 log10(||X - v1 v1^H X||^2 / ||X||^2)`` (rank-1 misfit).
    """
    X = np.asarray(X, dtype=np.complex128)
    if X.ndim != 2 or X.shape[0] < 1:
        raise ValueError("X must be (M, N)")
    ref = int(ref)
    if not 0 <= ref < X.shape[0]:
        raise ValueError("ref %d out of range" % ref)
    R = X @ X.conj().T
    _, vecs = np.linalg.eigh(R)
    v1 = vecs[:, -1]
    if abs(v1[ref]) < 1e-12 * np.abs(v1).max():
        raise ValueError("reference channel %d carries no signal" % ref)
    g = v1 / v1[ref]
    resid = X - np.outer(v1, v1.conj() @ X)
    tot = float(np.vdot(X, X).real)
    if tot <= 0.0:
        raise ValueError("X has zero energy")
    rr = float(np.vdot(resid, resid).real)
    residual_db = 10.0 * math.log10(max(rr / tot, 1e-30))
    return g, residual_db


# ---------------------------------------------------------------------------
# Waveforms
# ---------------------------------------------------------------------------
def tone(n, fs_hz, f_hz, amp=1.0, phase=0.0) -> np.ndarray:
    """Complex tone ``amp * exp(j(2 pi f t + phase))``, t = k/fs."""
    t = np.arange(int(n)) / float(fs_hz)
    return amp * np.exp(1j * (2.0 * np.pi * float(f_hz) * t + float(phase)))


def zadoff_chu(length: int, root: int = 1) -> np.ndarray:
    """Zadoff-Chu sequence ``exp(-j pi u k (k+1) / N)`` (odd N, gcd(u, N) = 1).

    Unit modulus with an ideal (zero off-peak) periodic autocorrelation.
    """
    n = int(length)
    u = int(root)
    if n < 1 or n % 2 == 0:
        raise ValueError("Zadoff-Chu length must be odd and >= 1, got %d" % n)
    if n > 1 and math.gcd(u, n) != 1:
        raise ValueError("root %d must be coprime with length %d" % (u, n))
    k = np.arange(n, dtype=np.int64)
    # exact integer phase index u k (k+1) mod 2N (no float rounding for long sequences)
    q = ((k * (k + 1)) % (2 * n)) * (u % (2 * n)) % (2 * n)
    return np.exp(-1j * np.pi * q.astype(np.float64) / n)


def lfm(n, fs_hz, bw_hz) -> np.ndarray:
    """Unit-amplitude linear FM chirp sweeping -bw/2 .. +bw/2 over ``n`` samples."""
    n = int(n)
    fs = float(fs_hz)
    bw = float(bw_hz)
    if n < 1:
        raise ValueError("n must be >= 1")
    if not 0.0 < bw <= fs:
        raise ValueError("need 0 < bw_hz <= fs_hz")
    t = np.arange(n) / fs
    T = n / fs
    k = bw / T
    return np.exp(1j * np.pi * (k * t * t - bw * t))


def bandlimited_noise(n, fs_hz, bw_hz, rng) -> np.ndarray:
    """Complex Gaussian noise confined to |f| <= bw/2 (circular), unit rms."""
    n = int(n)
    fs = float(fs_hz)
    bw = float(bw_hz)
    if n < 2:
        raise ValueError("n must be >= 2")
    if not 0.0 < bw <= fs:
        raise ValueError("need 0 < bw_hz <= fs_hz")
    if not isinstance(rng, np.random.Generator):
        raise TypeError("rng must be a numpy.random.Generator (use active_common.rng_stream)")
    w = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    W = np.fft.fft(w)
    f = np.fft.fftfreq(n, d=1.0 / fs)
    W[np.abs(f) > bw / 2.0] = 0.0
    x = np.fft.ifft(W)
    r = np.sqrt(np.mean(np.abs(x) ** 2))
    if not r > 0.0:
        raise ValueError("bandwidth too small for n=%d" % n)
    return x / r


def hadamard_codes(n_tx: int) -> np.ndarray:
    """Sylvester Hadamard codes (L, n_tx) of +-1, L = next power of two >= n_tx.

    Columns are orthogonal: ``H.T @ H == L * I``.
    """
    n_tx = int(n_tx)
    if n_tx < 1:
        raise ValueError("n_tx must be >= 1")
    L = 1 << int(math.ceil(math.log2(n_tx))) if n_tx > 1 else 1
    return _sylvester_hadamard(L).astype(np.float64)[:, :n_tx]


def ramp_window(x, ramp_samples: int) -> np.ndarray:
    """Raised-cosine on/off ramps of ``ramp_samples`` along the last axis.

    The first and last samples become exactly 0. Returns a new array.
    """
    x = np.asarray(x)
    r = int(ramp_samples)
    n = x.shape[-1]
    if r < 0:
        raise ValueError("ramp_samples must be >= 0")
    if 2 * r > n:
        raise ValueError("2 * ramp_samples (%d) exceeds the burst length %d" % (2 * r, n))
    out = np.array(x, dtype=np.result_type(x.dtype, np.float64), copy=True)
    if r == 0:
        return out
    k = np.arange(r)
    w = 0.5 * (1.0 - np.cos(np.pi * k / r))        # w[0] = 0
    out[..., :r] *= w
    out[..., n - r:] *= w[::-1]
    return out


def peak(x, axis=-1):
    """Maximum complex magnitude along ``axis`` (None = over everything)."""
    return np.max(np.abs(np.asarray(x)), axis=axis)


def rms(x, axis=-1):
    """Root-mean-square magnitude along ``axis`` (None = over everything)."""
    return np.sqrt(np.mean(np.abs(np.asarray(x)) ** 2, axis=axis))


def papr_db(x, axis=-1):
    """Peak-to-average power ratio in dB along ``axis``; a constant envelope gives 0 dB."""
    p = np.asarray(peak(x, axis=axis), dtype=float)
    r = np.asarray(rms(x, axis=axis), dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 20.0 * np.log10(p / r)
    out = np.where(r > 0, out, 0.0)
    return float(out) if np.ndim(out) == 0 else out


def scale_to_peak(x, peak_fs: float) -> np.ndarray:
    """Scale so that the maximum complex magnitude over the whole array is ``peak_fs``."""
    x = np.asarray(x)
    pk = float(np.max(np.abs(x))) if x.size else 0.0
    target = float(peak_fs)
    if not (math.isfinite(target) and target >= 0.0):
        raise ValueError("peak_fs must be finite and >= 0")
    if pk == 0.0 or not math.isfinite(pk):
        if not math.isfinite(pk):
            raise ValueError("input contains non-finite samples")
        return np.zeros_like(x, dtype=np.result_type(x.dtype, np.float64))
    y = x * (target / pk)
    # correct a possible 1-ulp overshoot so the bound is exact
    pk2 = float(np.max(np.abs(y)))
    if pk2 > target:
        y = y * (target / pk2)
    return y


# ---------------------------------------------------------------------------
# Sample formats
# ---------------------------------------------------------------------------
def cf32_to_sc16(x, scale=32767) -> np.ndarray:
    """Complex samples -> interleaved int16 I/Q (round half to even, saturating).

    Output shape is ``x.shape[:-1] + (2 * x.shape[-1],)``. NaN maps to 0.
    """
    x = np.asarray(x)
    if not np.iscomplexobj(x):
        x = x.astype(np.complex128)
    out = np.empty(x.shape[:-1] + (2 * x.shape[-1],), dtype=np.float64)
    out[..., 0::2] = x.real
    out[..., 1::2] = x.imag
    out = np.nan_to_num(out * float(scale), nan=0.0, posinf=32767.0, neginf=-32768.0)
    return np.clip(np.rint(out), -32768, 32767).astype(np.int16)


def sc16_to_cf32(v, scale=32768.0) -> np.ndarray:
    """Interleaved int16 I/Q -> complex64 ``(I + jQ) / scale``."""
    v = np.asarray(v)
    if v.shape[-1] % 2:
        raise ValueError("interleaved I/Q needs an even length")
    f = v.astype(np.float32) / np.float32(scale)
    return (f[..., 0::2] + 1j * f[..., 1::2]).astype(np.complex64)


def overdrive_mask(X, thresh: float = 0.99) -> int:
    """Bit m set when any |I| or |Q| >= ``thresh`` on row m (rows >= 32 ignored)."""
    X = np.asarray(X)
    if X.ndim == 1:
        X = X[np.newaxis, :]
    mask = 0
    for m in range(min(32, X.shape[0])):
        row = X[m]
        if row.size and (np.max(np.abs(row.real)) >= thresh or np.max(np.abs(row.imag)) >= thresh):
            mask |= 1 << m
    return mask
