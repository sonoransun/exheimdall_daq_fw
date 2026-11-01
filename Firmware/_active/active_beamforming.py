"""
    HeIMDALL "active" capability: beamforming.

      - ``BeamSpec`` (look direction, method, taper, nulls, steering mode);
      - amplitude tapers: uniform, Hamming, Hann, Dolph-Chebyshev, Taylor
        (separable on ULA / URA lattices, sampled continuous distributions on
        irregular geometries, uniform for a UCA);
      - RX weights: delay-and-sum (DAS), projection nulls, MVDR, LCMV;
      - TX weights ``t = conj(w) / max|w|`` (reciprocity: ``a^T t = w^H a / max|w|``);
      - applying weights / TTD to sample blocks and synthesizing calibrated,
        steered TX buffers (peak and PAPR are measured on that final buffer);
      - ``BeamSolution`` (weights + metrics, JSON round-trip) and ``solve_beam``.

    Weights stay geometric: calibration is applied to the data (RX) or by
    ``synthesize_tx`` (TX), never folded into the weights (spec 9.5).

    No hardware access; numpy + scipy only.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import json
import logging
import math
import time
import warnings
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.signal import windows as _win

import active_common as ac
from active_common import GeometryError, Issue
import active_dsp as dsp
import active_geometry as ag
import active_pattern as ap
from active_geometry import direction_vector

logger = logging.getLogger("heimdall.active.beamforming")

RX_METHODS = ("das", "projection", "mvdr", "lcmv")
TX_METHODS = ("das", "projection")
TAPERS = ("uniform", "hamming", "hann", "chebyshev", "taylor")
STEERING = ("phase", "ttd")
NORMALIZE = ("unit_gain", "unit_norm")
BEAM_JSON_FORMAT = "heimdall-active-beam/1"
MAINLOBE_CORR = 0.7          # |a0^H a_k| / (|a0||a_k|) above this -> NULL_IN_MAINLOBE
TX_DEFAULT_SCAN_LIMIT_DEG = 90.0   # TX look limit when neither the caller nor the geometry sets one
TX_MAX_LOBE_DB = -3.0        # a TX grating / ambiguity lobe at or above this is refused
TX_GUARD_TOL = 1e-3          # synthesize_tx: |s| allowed in the cropped tail (x max|s|, -60 dBc)
TX_INTRINSIC_TOL_DB = 0.5    # a TX cone / mirror image this far above the beam peak is refused (POINTING)
TX_PEAK_CONE_TOL_DEG = 0.05  # numerical slack on "the radiated peak lies inside the scan cone"
TX_LOOK_MIN_DB = -3.0        # the commanded TX look must get at least this much of the radiated peak


def _err(code, msg):
    e = GeometryError("%s: %s" % (code, msg))
    e.code = code
    return e


@dataclass(frozen=True)
class BeamSpec:
    az_deg: float
    el_deg: float = 0.0
    method: str = "das"          # das|projection|mvdr|lcmv   (TX: das|projection only)
    taper: str = "uniform"       # uniform|hamming|hann|chebyshev|taylor
    sll_db: float = 30.0
    nbar: int = 4
    nulls: tuple = ()            # ((az, el), ...)
    diag_loading: float = 10.0   # x noise power estimate (MVDR/LCMV)
    steering: str = "phase"      # phase|ttd
    focus_range_m: float = 0.0   # > 0 -> near-field focus at that range along (az, el)
    normalize: str = "unit_gain" # unit_gain|unit_norm (RX only)

    def __post_init__(self):
        if self.method not in RX_METHODS:
            raise ValueError("method %r not in %s" % (self.method, RX_METHODS))
        if self.taper not in TAPERS:
            raise ValueError("taper %r not in %s" % (self.taper, TAPERS))
        if self.steering not in STEERING:
            raise ValueError("steering %r not in %s" % (self.steering, STEERING))
        if self.normalize not in NORMALIZE:
            raise ValueError("normalize %r not in %s" % (self.normalize, NORMALIZE))
        for name in ("az_deg", "el_deg", "sll_db", "diag_loading", "focus_range_m"):
            v = float(getattr(self, name))
            if not math.isfinite(v):
                raise ValueError("%s must be finite" % name)
            object.__setattr__(self, name, v)
        if not -90.0 <= self.el_deg <= 90.0:
            raise ValueError("el_deg must be within [-90, 90]")
        if self.diag_loading < 0 or self.focus_range_m < 0:
            raise ValueError("diag_loading and focus_range_m must be >= 0")
        if int(self.nbar) != self.nbar or int(self.nbar) < 2:
            raise ValueError("nbar must be an integer >= 2")
        object.__setattr__(self, "nbar", int(self.nbar))
        nl = tuple((float(a), float(e)) for a, e in self.nulls)
        object.__setattr__(self, "nulls", nl)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["nulls"] = [list(n) for n in self.nulls]
        return d

    @staticmethod
    def from_dict(d) -> "BeamSpec":
        d = dict(d)
        d["nulls"] = tuple(tuple(n) for n in d.get("nulls", ()))
        return BeamSpec(**d)


def spec_from_config(beam_cfg, direction="rx"):
    """BeamSpec from ``[active_beam]`` (a BeamCfg or a whole ActiveConfig).

    Returns None for ``rx_method = off`` (no RX beamforming).
    """
    b = getattr(beam_cfg, "beam", beam_cfg)
    if direction == "rx":
        if b.rx_method == "off":
            return None
        return BeamSpec(az_deg=b.look_az_deg, el_deg=b.look_el_deg, method=b.rx_method, taper=b.taper,
                        sll_db=b.sll_db, nbar=b.nbar, nulls=tuple(b.nulls), diag_loading=b.diag_loading,
                        steering=b.steering, focus_range_m=b.focus_range_m, normalize=b.normalize)
    if direction == "tx":
        az, el = b.tx_look()
        return BeamSpec(az_deg=az, el_deg=el, method=b.tx_method, taper=b.effective_tx_taper(),
                        sll_db=b.sll_db, nbar=b.nbar, nulls=tuple(b.tx_nulls), diag_loading=b.diag_loading,
                        steering=b.steering, focus_range_m=b.focus_range_m, normalize="unit_gain")
    raise ValueError("direction must be rx or tx")


# ---------------------------------------------------------------------------
# Tapers
# ---------------------------------------------------------------------------
def _taylor_fm(nbar, sll_db):
    B = 10.0 ** (float(sll_db) / 20.0)
    A = math.acosh(B) / math.pi
    s2 = nbar ** 2 / (A ** 2 + (nbar - 0.5) ** 2)
    ma = np.arange(1, nbar)
    m2 = ma.astype(float) ** 2
    Fm = np.empty(nbar - 1)
    for mi in range(nbar - 1):
        numer = (-1) ** (mi + 2) * np.prod(1.0 - m2[mi] / s2 / (A ** 2 + (ma - 0.5) ** 2))
        denom = 2.0 * np.prod(1.0 - m2[mi] / m2[:mi]) * np.prod(1.0 - m2[mi] / m2[mi + 1:])
        Fm[mi] = numer / denom
    return ma, Fm


def _continuous(kind, x, sll_db, nbar):
    """Continuous aperture distribution at normalized coordinate x in [-0.5, 0.5]."""
    x = np.asarray(x, dtype=float)
    if kind == "hamming":
        return 0.54 + 0.46 * np.cos(2 * np.pi * x)
    if kind == "hann":
        return 0.5 + 0.5 * np.cos(2 * np.pi * x)
    # taylor (also the off-lattice stand-in for chebyshev, which has no continuous form)
    ma, Fm = _taylor_fm(nbar, sll_db)
    return 1.0 + 2.0 * np.cos(2 * np.pi * np.multiply.outer(x, ma)) @ Fm


def _discrete(kind, n, sll_db, nbar):
    if n == 1:
        return np.ones(1)
    if kind == "hamming":
        return _win.hamming(n, sym=True)
    if kind == "hann":
        return _win.hann(n + 2, sym=True)[1:-1]          # no zero-weight edge elements
    if kind == "chebyshev":
        with warnings.catch_warnings():          # scipy's note concerns spectral analysis, not arrays
            warnings.simplefilter("ignore", UserWarning)
            return _win.chebwin(n, at=float(sll_db), sym=True)
    if kind == "taylor":
        return _win.taylor(n, nbar=int(nbar), sll=float(sll_db), norm=False, sym=True)
    return np.ones(n)


def _line_coord(t, n_eff):
    """Positions t along a line -> x in [-0.5, 0.5) of an aperture n_eff elements long."""
    span = float(t.max() - t.min())
    if span <= 0:
        return np.zeros_like(t)
    L = span * n_eff / max(n_eff - 1, 1)
    return (t - 0.5 * (t.max() + t.min())) / L


def taper_weights(geom, kind, sll_db, nbar, which="rx", issues=None) -> np.ndarray:
    """Real amplitude taper (max 1) in ``geom.elements_for(which)`` order.

    ULA / URA lattices with no missing sites use the discrete scipy windows
    (separable for a URA); thinned lattices, irregular and triangular sets
    sample the continuous distribution (projection onto the principal axis for
    elongated apertures, radial otherwise; Chebyshev falls back to Taylor off
    the lattice). A UCA gets a uniform taper (+ a warning appended to ``issues``).
    """
    if kind not in TAPERS:
        raise ValueError("taper %r not in %s" % (kind, TAPERS))
    P = geom.positions(which)
    n = P.shape[0]
    if n == 0:
        return np.zeros(0)
    if kind == "uniform" or n == 1:
        return np.ones(n)
    nbar = int(nbar)
    lat = ag.detect_lattice(P)
    r, B, _ = ag.position_subspace(P)
    if lat["kind"] == "uca":
        msg = "taper %r requested on a UCA: using uniform weights" % kind
        logger.warning(msg)
        if issues is not None:
            issues.append(Issue("warning", "TAPER", msg, "beam[%s]" % which))
        return np.ones(n)
    if lat["kind"] == "ula":
        idx = lat["indices"][:, 0]
        if lat["complete"]:
            w = _discrete(kind, n, sll_db, nbar)[idx]
        else:
            m = int(idx.max()) + 1
            w = _continuous(kind, (idx - (m - 1) / 2.0) / m, sll_db, nbar)
    elif lat["kind"] == "ura":
        idx = lat["indices"]
        ext = idx.max(axis=0) + 1
        if lat["complete"]:
            w = _discrete(kind, int(ext[0]), sll_db, nbar)[idx[:, 0]] * \
                _discrete(kind, int(ext[1]), sll_db, nbar)[idx[:, 1]]
        else:
            x1 = (idx[:, 0] - (ext[0] - 1) / 2.0) / ext[0]
            x2 = (idx[:, 1] - (ext[1] - 1) / 2.0) / ext[1]
            w = _continuous(kind, x1, sll_db, nbar) * _continuous(kind, x2, sll_db, nbar)
    elif r == 1:
        w = _continuous(kind, _line_coord(P @ B[:, 0], n), sll_db, nbar)
    else:
        Q = P - P.mean(axis=0)
        s = np.linalg.svd(Q, compute_uv=False)
        if s.size > 1 and s[1] > 0 and s[0] / s[1] > 3.0:
            vt = np.linalg.svd(Q, full_matrices=False)[2]
            w = _continuous(kind, _line_coord(Q @ vt[0], n), sll_db, nbar)
        else:
            rho = np.linalg.norm(Q, axis=1)
            ne = math.sqrt(n)
            x = 0.5 * rho / max(rho.max(), 1e-300) * (ne - 1.0) / ne if ne > 1 else np.zeros(n)
            w = _continuous(kind, x, sll_db, nbar)
    w = np.clip(np.asarray(w, dtype=float), 0.0, None)
    mx = float(w.max())
    return w / mx if mx > 0 else np.ones(n)


# ---------------------------------------------------------------------------
# Weight solvers
# ---------------------------------------------------------------------------
def sample_covariance(X) -> np.ndarray:
    """``X X^H / K`` for snapshots X (N, K)."""
    X = np.asarray(X, dtype=np.complex128)
    if X.ndim != 2 or X.shape[1] < 1:
        raise ValueError("X must be (N, K) with K >= 1")
    return X @ X.conj().T / X.shape[1]


def _unit_gain(w, a0):
    s = np.vdot(w, a0)                                   # w^H a0
    if abs(s) < 1e-12 * np.linalg.norm(w) * np.linalg.norm(a0):
        raise _err("NULL_IN_MAINLOBE", "the constrained weights have no response toward the look direction")
    return w / np.conj(s)


def projection_nulls(w_q, C, a0) -> np.ndarray:
    """``P_perp w_q`` with ``P_perp = I - C (C^H C)^-1 C^H``, renormalized to ``w^H a0 = 1``."""
    w_q = np.asarray(w_q, dtype=np.complex128).reshape(-1)
    C = np.asarray(C, dtype=np.complex128)
    if C.ndim == 1:
        C = C[:, np.newaxis]
    if C.shape[1] == 0:
        return _unit_gain(w_q, a0)
    w = w_q - C @ np.linalg.lstsq(C, w_q, rcond=None)[0]
    return _unit_gain(w, np.asarray(a0, dtype=np.complex128))


def mvdr(R, a0, loading) -> np.ndarray:
    """``(R + d I)^-1 a0 / (a0^H (R + d I)^-1 a0)`` with absolute loading ``d``."""
    R = np.asarray(R, dtype=np.complex128)
    a0 = np.asarray(a0, dtype=np.complex128).reshape(-1)
    Rl = R + float(loading) * np.eye(R.shape[0])
    v = np.linalg.solve(Rl, a0)
    return v / np.vdot(a0, v)


def lcmv(R, C, f, loading) -> np.ndarray:
    """``R~^-1 C (C^H R~^-1 C)^-1 f`` with ``R~ = R + loading I``; satisfies ``C^H w = f``."""
    R = np.asarray(R, dtype=np.complex128)
    C = np.asarray(C, dtype=np.complex128)
    if C.ndim == 1:
        C = C[:, np.newaxis]
    f = np.asarray(f, dtype=np.complex128).reshape(-1)
    Rl = R + float(loading) * np.eye(R.shape[0])
    RiC = np.linalg.solve(Rl, C)
    return RiC @ np.linalg.solve(C.conj().T @ RiC, f)


def _noise_estimate(R, n_snapshots):
    """Mean of the smallest N-K eigenvalues, K from the MDL criterion (Wax-Kailath)."""
    lam = np.sort(np.clip(np.linalg.eigvalsh(R).real, 1e-300, None))[::-1]
    n = lam.size
    ks = float(n_snapshots or 4 * n)
    best, kbest = math.inf, 0
    for k in range(n):
        tail = lam[k:]
        geo = math.exp(float(np.mean(np.log(tail))))
        ari = float(np.mean(tail))
        mdl = -ks * (n - k) * math.log(geo / ari) + 0.5 * k * (2 * n - k) * math.log(ks)
        if mdl < best:
            best, kbest = mdl, k
    kbest = min(kbest, n - 1)
    return float(np.mean(lam[kbest:]))


def _look_vector(geom, spec, f_hz, which):
    u0 = direction_vector(spec.az_deg, spec.el_deg)
    if spec.focus_range_m > 0:
        pos = geom.positions(which)
        a = ap.steering_nf(pos, spec.focus_range_m * u0, f_hz)
        g = ap.array_manifold(geom, u0, f_hz, which, True) / ap.steering_ff(pos, u0, f_hz)
        return u0, a * g
    return u0, ap.array_manifold(geom, u0, f_hz, which, True)


def _null_matrix(geom, spec, f_hz, which, a0):
    n = a0.size
    if not spec.nulls:
        return np.zeros((n, 0), dtype=np.complex128)
    if len(spec.nulls) > n - 2:
        raise _err("NULLS", "%d nulls need N >= K + 2 elements (N = %d)" % (len(spec.nulls), n))
    U = direction_vector([a for a, _ in spec.nulls], [e for _, e in spec.nulls])
    C = ap.array_manifold(geom, U, f_hz, which, True).T              # (N, K)
    for k, (az, el) in enumerate(spec.nulls):
        ck = C[:, k]
        rho = abs(np.vdot(a0, ck)) / (np.linalg.norm(a0) * np.linalg.norm(ck))
        if rho > MAINLOBE_CORR:
            raise _err("NULL_IN_MAINLOBE", "null at az %g el %g is inside the main lobe (|a0^H a_k| = %.2f > %.2f)"
                       % (az, el, rho, MAINLOBE_CORR))
    return C


def rx_weights(geom, spec, f_hz, R=None, noise_power=None, X=None, n_snapshots=None, issues=None) -> np.ndarray:
    """RX weights (N_rx,) in ``geom.elements_for("rx")`` order (spec 10.1).

    DAS ``w = tau . a0 / sum(tau |a0|^2)`` (unit_gain: ``w^H a0 = 1``), projection
    nulls, MVDR, LCMV. MVDR/LCMV need a covariance ``R`` (or snapshots ``X``)
    from at least 2N snapshots (``GeometryError`` SNAPSHOTS); the loading is
    ``diag_loading`` x ``noise_power`` (default: mean of the N-K smallest
    eigenvalues, K by MDL).
    """
    which = "rx"
    u0, a0 = _look_vector(geom, spec, f_hz, which)
    n = a0.size
    if n == 0:
        raise _err("GEOMETRY", "no RX elements")
    C = _null_matrix(geom, spec, f_hz, which, a0)
    tau = taper_weights(geom, spec.taper, spec.sll_db, spec.nbar, which, issues)
    m = spec.method
    if m in ("das", "projection"):
        if m == "das" and C.shape[1] and issues is not None:
            issues.append(Issue("warning", "NULLS", "nulls are ignored by the das method", "beam[rx]"))
        w = tau * a0
        w = projection_nulls(w, C, a0) if m == "projection" else _unit_gain(w, a0)
    else:
        if X is not None:
            X = np.asarray(X, dtype=np.complex128)
            if X.ndim != 2 or X.shape[0] != n:
                raise ValueError("X must be (%d, K)" % n)
            R = sample_covariance(X)
            n_snapshots = X.shape[1]
        if R is None:
            raise _err("SNAPSHOTS", "%s needs a covariance from >= 2N = %d snapshots" % (m, 2 * n))
        R = np.asarray(R, dtype=np.complex128)
        if R.shape != (n, n):
            raise ValueError("R must be (%d, %d)" % (n, n))
        if n_snapshots is not None and int(n_snapshots) < 2 * n:
            raise _err("SNAPSHOTS", "%d snapshots < 2N = %d" % (int(n_snapshots), 2 * n))
        noise = float(noise_power) if noise_power is not None else _noise_estimate(R, n_snapshots)
        delta = spec.diag_loading * noise
        if m == "mvdr":
            w = mvdr(R, a0, delta)
        else:
            Cf = np.concatenate([a0[:, np.newaxis], C], axis=1)
            fvec = np.zeros(Cf.shape[1], dtype=np.complex128)
            fvec[0] = 1.0
            w = lcmv(R, Cf, fvec, delta)
    if spec.normalize == "unit_norm":
        s = np.vdot(w, a0)
        w = w / np.linalg.norm(w) * (np.conj(s) / abs(s) if abs(s) > 0 else 1.0)
    return w


def tx_scan_limit_deg(geom, scan_limit_deg=None) -> float:
    """The TX look cone half-angle: the argument, else ``geom.scan_limit_deg`` (from
    ``[active_geometry] scan_limit_deg``), else 90 deg (the forward hemisphere)."""
    if scan_limit_deg is not None:
        v = float(scan_limit_deg)
    elif getattr(geom, "scan_limit_deg", None) is not None:
        v = float(geom.scan_limit_deg)
    else:
        v = TX_DEFAULT_SCAN_LIMIT_DEG
    if not (math.isfinite(v) and 0.0 <= v <= 90.0):
        raise ValueError("scan_limit_deg must be within [0, 90], got %r" % (scan_limit_deg,))
    return v


def _focus_local_looks(geom, spec, which="tx"):
    """Unit directions (N, 3) from each element of ``which`` to the focus point."""
    pos = geom.positions(which)
    d = spec.focus_range_m * direction_vector(spec.az_deg, spec.el_deg)[np.newaxis, :] - pos
    nrm = np.linalg.norm(d, axis=1)
    if np.any(nrm <= 0.0):
        raise _err("SCAN_LIMIT", "TX focus point coincides with an element")
    return d / nrm[:, np.newaxis]


def check_tx_look(geom, spec, scan_limit_deg=None) -> float:
    """Refuse (``GeometryError`` SCAN_LIMIT) a TX look / focus direction outside the scan
    cone around the platform boresight: the geometry and grating-lobe validation only
    certify that cone. For a near-field focus every TX element's own direction to the
    focus point must lie in the cone too (each element then works inside the validated,
    grating-free region). Returns the angle from boresight (deg) of the look."""
    lim = tx_scan_limit_deg(geom, scan_limit_deg)
    u0 = direction_vector(spec.az_deg, spec.el_deg)
    ang = geom.angle_from_boresight_deg(u0)
    if ang > lim + 1e-9:
        what = "focus point" if spec.focus_range_m > 0 else "look"
        raise _err("SCAN_LIMIT", "TX %s az %g el %g is %.1f deg from the boresight, outside the validated "
                   "%.1f deg scan cone (scan_limit_deg)" % (what, spec.az_deg, spec.el_deg, ang, lim))
    if spec.focus_range_m > 0 and geom.elements_for("tx"):
        loc = np.degrees(np.arccos(np.clip(_focus_local_looks(geom, spec) @ geom.boresight_enu(), -1.0, 1.0)))
        k = int(np.argmax(loc))
        if loc[k] > lim + 1e-9:
            raise _err("SCAN_LIMIT", "TX focus az %g el %g at %g m: TX element %d sees the focus %.1f deg from "
                       "the boresight, outside the validated %.1f deg scan cone (focus farther away or "
                       "closer to boresight)" % (spec.az_deg, spec.el_deg, spec.focus_range_m,
                                                  geom.elements_for("tx")[k].id, float(loc[k]), lim))
    return ang


def _tx_gates(geom, spec, f_hz, metrics, scan_limit_deg):
    """Refusals on the solved TX beam (``GeometryError``):

    - SCAN_LIMIT: the radiated main-lobe peak lies outside the scan cone;
    - POINTING: the commanded look lies outside the half-power contour of the radiated
      beam (``look_level_db`` < -3 dB: element patterns pulled the peak away, e.g. along
      the cone ridge of a line array), or an intrinsic cone / mirror image radiates more
      than ``TX_INTRINSIC_TOL_DB`` above the peak;
    - GRATING_LOBE: a non-intrinsic grating / ambiguity lobe (far field), a near-field
      sidelobe or, for a focused beam on a periodic lattice, an element whose direction
      to the focus is outside the grating-free scan, at or above ``TX_MAX_LOBE_DB``.
    """
    lim = tx_scan_limit_deg(geom, scan_limit_deg)
    m = metrics
    head = "TX beam az %g el %g%s" % (spec.az_deg, spec.el_deg,
                                     " (focus %g m)" % spec.focus_range_m if spec.focus_range_m > 0 else "")
    u_pk = direction_vector(m.peak_az_deg, m.peak_el_deg)
    ang_pk = geom.angle_from_boresight_deg(u_pk)
    if ang_pk > lim + TX_PEAK_CONE_TOL_DEG:
        raise _err("SCAN_LIMIT", "%s radiates its peak at az %.1f el %.1f, %.1f deg from the boresight, outside "
                   "the validated %.1f deg scan cone: refused" % (head, m.peak_az_deg, m.peak_el_deg, ang_pk, lim))
    if m.look_level_db < TX_LOOK_MIN_DB:
        raise _err("POINTING", "%s peaks at az %.1f el %.1f, %.1f deg from the commanded look, which only gets "
                   "%.1f dB of the peak (outside the half-power beam; element patterns pull the beam): refused"
                   % (head, m.peak_az_deg, m.peak_el_deg, m.pointing_error_deg, m.look_level_db))
    strong = [lb for lb in m.lobes if lb.intrinsic and lb.level_db > TX_INTRINSIC_TOL_DB]
    if strong:
        lb = max(strong, key=lambda x: x.level_db)
        raise _err("POINTING", "%s: its intrinsic %s image at az %.1f el %.1f radiates %+.1f dB above the "
                   "commanded beam: refused" % (head, "cone" if geom.rank("tx") == 1 else "mirror",
                                                lb.az_deg, lb.el_deg, lb.level_db))
    bad = [lb for lb in m.lobes if lb.kind in ("grating", "ambiguity") and not lb.intrinsic
           and lb.level_db >= TX_MAX_LOBE_DB]
    if bad:
        lb = max(bad, key=lambda x: x.level_db)
        where = (" (partial lobe at the edge of visible space, endfire / in-plane horizon; geometry_report "
                 "flags it for the TX set)" if lb.edge else "")
        raise _err("GRATING_LOBE", "%s has a %s lobe of %.1f dB at az %.1f el %.1f%s (%.6g Hz): refused"
                   % (head, lb.kind, lb.level_db, lb.az_deg, lb.el_deg, where, float(f_hz)))
    if spec.focus_range_m > 0:
        if m.psll_db >= TX_MAX_LOBE_DB:
            raise _err("GRATING_LOBE", "%s has a near-field sidelobe of %.1f dB on the focal sphere (%.6g Hz): "
                       "refused" % (head, m.psll_db, float(f_hz)))
        gf = ag.grating_free_scan_deg(geom, f_hz, "tx")
        if gf is not None:
            loc = np.degrees(np.arccos(np.clip(_focus_local_looks(geom, spec) @ geom.boresight_enu(), -1, 1)))
            if float(loc.max()) > gf + 1e-9:
                raise _err("GRATING_LOBE", "%s: a TX element sees the focus %.1f deg from the boresight, beyond "
                           "the %.1f deg grating-free scan of the lattice at %.6g Hz: refused"
                           % (head, float(loc.max()), gf, float(f_hz)))


def tx_weights(geom, spec, f_hz, issues=None, scan_limit_deg=None) -> np.ndarray:
    """TX weights ``t = conj(w_geometric) / max|w|`` over the TX elements (spec 10.2).

    Methods das / projection only (adaptive TX weights are refused). The
    radiated pattern is ``a^T(u) t = w^H a(u) / max|w|``. Refused
    (``GeometryError``): nulls with ``das`` (NULLS: they would not be applied)
    and a look outside the scan cone (SCAN_LIMIT, see ``check_tx_look``).
    """
    if spec.method not in TX_METHODS:
        raise _err("TX_METHOD", "TX beams support %s only, not %r" % (", ".join(TX_METHODS), spec.method))
    if spec.nulls and spec.method == "das":
        raise _err("NULLS", "tx nulls require tx_method = projection (das does not place nulls; "
                   "%d null(s) requested)" % len(spec.nulls))
    which = "tx"
    if not geom.elements_for(which):
        raise _err("GEOMETRY", "no TX elements")
    check_tx_look(geom, spec, scan_limit_deg)
    u0, a0 = _look_vector(geom, spec, f_hz, which)
    C = _null_matrix(geom, spec, f_hz, which, a0)
    tau = taper_weights(geom, spec.taper, spec.sll_db, spec.nbar, which, issues)
    w = tau * a0
    if spec.method == "projection":
        w = projection_nulls(w, C, a0)
    mx = float(np.abs(w).max())
    if not mx > 0:
        raise _err("GEOMETRY", "TX weights are all zero")
    return np.conj(w) / mx


# ---------------------------------------------------------------------------
# Applying weights
# ---------------------------------------------------------------------------
def apply_rx(X, w, fs_hz=None, ttd_delays=None) -> np.ndarray:
    """Beam output ``w^H X`` (N,) for calibrated element samples X (N_rx, N).

    With ``ttd_delays`` (seconds, per element) the rows are first delayed by
    ``tau_n`` (circular FFT delay over the block) so the envelope is aligned;
    the carrier part of TTD is the conjugated steering phase already in ``w``.
    """
    X = np.asarray(X, dtype=np.complex128)
    w = np.asarray(w, dtype=np.complex128).reshape(-1)
    if X.ndim != 2 or X.shape[0] != w.size:
        raise ValueError("X must be (%d, N)" % w.size)
    if ttd_delays is not None:
        if not fs_hz or float(fs_hz) <= 0:
            raise ValueError("fs_hz is required with ttd_delays")
        X = dsp.frac_delay_fft(X, np.asarray(ttd_delays, dtype=float) * float(fs_hz))
    return np.conj(w) @ X


def beams_matrix(geom, specs, f_hz, R=None, noise_power=None, n_snapshots=None) -> np.ndarray:
    """(B, N_rx) matrix whose rows are ``w_b^H`` (``Y = W @ X``)."""
    rows = [np.conj(rx_weights(geom, s, f_hz, R=R, noise_power=noise_power, n_snapshots=n_snapshots))
            for s in specs]
    if not rows:
        return np.zeros((0, len(geom.elements_for("rx"))), dtype=np.complex128)
    return np.vstack(rows)


def _tx_cal_terms(tx_cal, n):
    if tx_cal is None:
        return np.ones(n, dtype=np.complex128), np.zeros(n)
    if isinstance(tx_cal, dict):
        if "correction" in tx_cal:
            c = np.asarray(tx_cal["correction"], dtype=np.complex128)
        else:
            c = 1.0 / np.asarray(tx_cal["gains"], dtype=np.complex128)
        d = np.asarray(tx_cal.get("delays_s", np.zeros(n)), dtype=float)
    elif isinstance(tx_cal, (tuple, list)) and len(tx_cal) == 2:
        c = np.asarray(tx_cal[0], dtype=np.complex128)
        d = np.asarray(tx_cal[1], dtype=float)
    elif callable(getattr(tx_cal, "correction", None)):
        c = np.asarray(tx_cal.correction(), dtype=np.complex128)
        d = np.asarray(getattr(tx_cal, "delays_s"), dtype=float)
    elif hasattr(tx_cal, "gains"):
        c = 1.0 / np.asarray(tx_cal.gains, dtype=np.complex128)
        d = np.asarray(getattr(tx_cal, "delays_s"), dtype=float)
    else:
        raise TypeError("tx_cal must be None, (correction, delays_s), a dict or a CalResult-like object")
    c = c.reshape(-1)
    d = d.reshape(-1)
    if c.size != n or d.size != n:
        raise ValueError("tx_cal has %d/%d entries for %d TX chains" % (c.size, d.size, n))
    if not (np.all(np.isfinite(c)) and np.all(np.isfinite(d))):
        raise ValueError("tx_cal contains non-finite values")
    return c, d


def synthesize_tx(s, t, fs_hz, tx_cal=None, ttd_delays=None, backoff_db=0.0, peak_target=None) -> np.ndarray:
    """Per-chain TX buffers (n_tx, N), complex128 (spec 4.6 / 9.5).

    ``s_n = t_n c_n^tx frac_delay(s, tau_ttd,n - dtau_tx,n - min_m(.))`` where
    ``c^tx = 1/g^tx`` and ``dtau_tx`` (positive = late) come from ``tx_cal``
    (``(correction, delays_s)``, a dict, or a CalResult-like object with
    ``correction()`` / ``gains`` and ``delays_s``). Delays are linear (zero
    padded) and the block keeps its N samples, so ``s`` must end with guard
    zeros covering the largest relative delay ``D = ceil(max_n d_n)`` samples:
    when any of the last D samples of ``s`` exceeds ``TX_GUARD_TOL`` x max|s|
    (-60 dBc) a ValueError is raised, because cropping it would end that chain's
    burst abruptly (spectral splatter, a hard PA transient). The result is then
    scaled by ``10^(-backoff_db/20)``; with ``peak_target`` the maximum
    magnitude over all chains is exactly ``peak_target 10^(-backoff_db/20)``.
    ``backoff_db`` must be finite and >= 0, ``peak_target`` within (0, 1]
    (ValueError otherwise); without ``peak_target`` a peak above full scale is
    logged (``authorize_burst`` refuses it).
    """
    s = np.asarray(s, dtype=np.complex128).reshape(-1)
    t = np.asarray(t, dtype=np.complex128).reshape(-1)
    n_tx, N = t.size, s.size
    if n_tx == 0 or N == 0:
        raise ValueError("empty weights or waveform")
    if not (np.all(np.isfinite(s)) and np.all(np.isfinite(t))):
        raise ValueError("non-finite waveform or weights")
    fs = float(fs_hz)
    if not fs > 0:
        raise ValueError("fs_hz must be > 0")
    try:
        bo = float(backoff_db)
    except (TypeError, ValueError):
        raise ValueError("backoff_db must be a number") from None
    if not (math.isfinite(bo) and bo >= 0.0):
        raise ValueError("backoff_db must be finite and >= 0 (a backoff never amplifies), got %r" % (backoff_db,))
    if peak_target is not None:
        try:
            pt = float(peak_target)
        except (TypeError, ValueError):
            raise ValueError("peak_target must be a number") from None
        if not (math.isfinite(pt) and 0.0 < pt <= 1.0):
            raise ValueError("peak_target must be within (0, 1] of full scale, got %r" % (peak_target,))
    c, dtx = _tx_cal_terms(tx_cal, n_tx)
    ttd = np.zeros(n_tx) if ttd_delays is None else np.asarray(ttd_delays, dtype=float).reshape(-1)
    if ttd.size != n_tx:
        raise ValueError("ttd_delays has %d entries for %d TX chains" % (ttd.size, n_tx))
    d = (ttd - dtx) * fs
    d = d - d.min()
    if np.any(d != 0):
        D = int(math.ceil(float(d.max()) - 1e-9))
        smax = float(np.max(np.abs(s)))
        tail = s[max(0, N - D):]
        if D > 0 and smax > 0 and float(np.max(np.abs(tail))) > TX_GUARD_TOL * smax:
            k = int(np.argmax(d))
            raise ValueError("synthesize_tx: TX chain %d is delayed by %.2f samples (TTD - TX-cal "
                             "pre-advance) but the last %d samples of the waveform are not zero (max %.3g "
                             "of the peak): they would be cut, ending the burst abruptly; append at least "
                             "%d trailing guard zeros" % (k, float(d[k]), D, float(np.max(np.abs(tail))) / smax,
                                                          D))
        G = int(math.ceil(float(d.max()))) + 64
        sp = np.concatenate([np.zeros(G), s, np.zeros(G)])
        rows = dsp.frac_delay_fft(np.broadcast_to(sp, (n_tx, sp.size)), d)[:, G:G + N]
    else:
        rows = np.broadcast_to(s, (n_tx, N))
    out = (t * c)[:, np.newaxis] * rows
    scale = 10.0 ** (-bo / 20.0)
    if peak_target is not None:
        out = dsp.scale_to_peak(out, pt * scale)
    elif scale != 1.0:
        out = out * scale
    if not np.all(np.isfinite(out)):
        raise ValueError("synthesized TX buffer is not finite")
    pk = float(np.max(np.abs(out)))
    if peak_target is None and pk > 1.0 + 1e-9:
        logger.warning("synthesize_tx: peak %.3g exceeds full scale (no peak_target); authorize_burst "
                       "will refuse this buffer", pk)
    return np.ascontiguousarray(out, dtype=np.complex128)


# ---------------------------------------------------------------------------
# Solutions
# ---------------------------------------------------------------------------
@dataclass
class BeamSolution:
    spec: BeamSpec
    direction: str
    f_hz: float
    weights: np.ndarray          # RX: w (y = w^H x) ; TX: t (applied as t_n s)
    ttd_delays_s: np.ndarray
    metrics: ap.BeamMetrics
    created_unix: float
    issues: list = field(default_factory=list)   # warnings collected while solving

    def to_json(self) -> str:
        w = np.asarray(self.weights, dtype=np.complex128)
        return json.dumps({
            "format": BEAM_JSON_FORMAT, "spec": self.spec.to_dict(), "direction": self.direction,
            "f_hz": float(self.f_hz), "weights": [[float(z.real), float(z.imag)] for z in w],
            "ttd_delays_s": [float(x) for x in np.asarray(self.ttd_delays_s, dtype=float)],
            "metrics": self.metrics.to_dict(), "created_unix": float(self.created_unix),
            "issues": ac.issues_to_json(self.issues)}, sort_keys=True)

    @staticmethod
    def from_json(s) -> "BeamSolution":
        d = json.loads(s)
        if d.get("format") != BEAM_JSON_FORMAT:
            raise ValueError("not a %s document (format %r)" % (BEAM_JSON_FORMAT, d.get("format")))
        w = np.array([complex(re, im) for re, im in d["weights"]], dtype=np.complex128)
        return BeamSolution(spec=BeamSpec.from_dict(d["spec"]), direction=d["direction"], f_hz=float(d["f_hz"]),
                            weights=w, ttd_delays_s=np.asarray(d["ttd_delays_s"], dtype=float),
                            metrics=ap.BeamMetrics.from_dict(d["metrics"]), created_unix=float(d["created_unix"]),
                            issues=[Issue(**i) for i in d.get("issues", [])])


def solve_beam(geom, spec, f_hz, direction="rx", R=None, cal_phase_rms_deg=0.0, cal_amp_rms_db=0.0,
               bw_hz=0.0, X=None, n_snapshots=None, noise_power=None, grid_step_deg=None, issues=None,
               scan_limit_deg=None) -> BeamSolution:
    """Weights + TTD delays + metrics for one beam. TX metrics use ``conj(t)`` (reciprocity).

    Warnings (ignored nulls, UCA taper, RX look outside the scan cone) go to
    ``BeamSolution.issues`` and to ``issues`` when given. TX refusals
    (``GeometryError``): see ``tx_weights`` (NULLS, SCAN_LIMIT) and ``_tx_gates``
    on the solved beam: SCAN_LIMIT (radiated peak outside the cone), POINTING (the
    look outside the radiated half-power beam, or an intrinsic cone / mirror image
    stronger than the beam), GRATING_LOBE (a grating / ambiguity lobe, not the intrinsic image, at or
    above -3 dB). With ``focus_range_m > 0`` the TTD delays are the near-field ones
    toward the focus point and the metrics are measured on the focal sphere
    (``ap.beam_metrics``), where the far-field lobe search does not apply.
    """
    found = []
    where = "beam[%s]" % direction
    if direction == "rx":
        w = rx_weights(geom, spec, f_hz, R=R, noise_power=noise_power, X=X, n_snapshots=n_snapshots,
                       issues=found)
        w_pat = w
        lim = scan_limit_deg if scan_limit_deg is not None else getattr(geom, "scan_limit_deg", None)
        if lim is not None:
            ang = geom.angle_from_boresight_deg(direction_vector(spec.az_deg, spec.el_deg))
            if ang > float(lim) + 1e-9:
                found.append(Issue("warning", "SCAN_LIMIT", "RX look az %g el %g is %.1f deg from the boresight, "
                                   "outside the validated %.1f deg scan cone" % (spec.az_deg, spec.el_deg, ang,
                                                                                float(lim)), where))
    elif direction == "tx":
        w = tx_weights(geom, spec, f_hz, issues=found, scan_limit_deg=scan_limit_deg)
        w_pat = np.conj(w)
    else:
        raise ValueError("direction must be rx or tx")
    pos = geom.positions(direction)
    u0 = direction_vector(spec.az_deg, spec.el_deg)
    if spec.steering != "ttd":
        ttd = np.zeros(pos.shape[0])
    elif spec.focus_range_m > 0:
        ttd = ap.ttd_delays_nf_s(pos, spec.focus_range_m * u0)
    else:
        ttd = ap.ttd_delays_s(pos, u0)
    metrics = ap.beam_metrics(geom, w_pat, f_hz, u0, nulls=spec.nulls, which=direction, bw_hz=bw_hz,
                              cal_phase_rms_deg=cal_phase_rms_deg, cal_amp_rms_db=cal_amp_rms_db,
                              grid_step_deg=grid_step_deg, focus_range_m=spec.focus_range_m)
    if spec.steering == "ttd":
        metrics.squint_ok = True                      # true-time delay: no squint
    if direction == "tx":
        _tx_gates(geom, spec, f_hz, metrics, scan_limit_deg)
    if issues is not None:
        issues.extend(found)
    return BeamSolution(spec=spec, direction=direction, f_hz=float(f_hz), weights=w, ttd_delays_s=ttd,
                        metrics=metrics, created_unix=time.time(), issues=found)
