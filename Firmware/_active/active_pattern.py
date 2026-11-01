"""
    HeIMDALL "active" capability: steering vectors, patterns and array metrics.

      - steering vectors: far field, near field (spherical), wideband;
      - element patterns (isotropic, cos^q, dipoles, measured tables);
      - array manifold, true-time-delay (TTD) delays, array factor, patterns;
      - directivity (closed form for isotropic elements, numeric otherwise),
        taper efficiency, calibration-limited null floor, squint;
      - beam metrics (peak, HPBW, PSLL, ISLR, null depths, lobes);
      - grating lobes: the analytic reciprocal-lattice test and a geometry
        agnostic numeric ambiguity search;
      - ``geometry_report`` combining the analytic and numeric checks.

    Conventions (spec section 2): ``a_n(u, f) = exp(+j 2 pi f u.r_n / c)``,
    ``AF(u) = w^H a(u)``. TTD (spec 0.3 D1): ``tau_n = (u0.r_n - min_m u0.r_m)/c
    >= 0`` applied as a delay on RX and on TX; at RF this is
    ``exp(-j 2 pi f_c tau_n) = conj(a_n(u0))`` up to a common phase.

    No hardware access; numpy + scipy only.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy import ndimage, optimize

import active_common as ac
from active_common import C0, Issue
import active_geometry as ag
from active_geometry import ElementPattern, direction_angles, direction_vector

FLOOR_DB = -300.0                   # finite stand-in for -inf (JSON-safe)
HALF_POWER_DB = 10.0 * math.log10(0.5)
_MAIN_LOBE_TOL_DB = 0.3             # plateau tolerance of the main-lobe flood fill
_PEAK_PENALTY_GRID = 1e-3           # dB / deg^2 pull toward the look on the coarse grid
_EDGE_WARN_MAX = 4                  # edge-of-visible-space warnings listed per report
_EDGE_TX_EVAL = 6                   # TX edge lobes re-measured with the element patterns per report
TX_EDGE_LOBE_DB = -3.0              # TX set: an edge lobe at or above this is an error (solve_beam refuses it)
_PEAK_PENALTY_FINE = 1e-6           # dB / deg^2 during the exact refinement (bias ~1e-5 deg/deg)
_GRID_MAX_POINTS = 1_300_000
_PATCH_TRIGGER = 6.0                # local re-grid when the sphere grid step > HPBW / this
_PATCH_HPBW_STEP = 10.0             # local grid step = HPBW / this
_PATCH_HALF_HPBW = 10.0             # local patch half-width in beamwidths ...
_PATCH_MAX_HALF_DEG = 30.0          # ... capped at this
_PATCH_MAX_SIDE = 401               # points per patch axis
_SEEDS_MAX = 32                     # sidelobe local maxima refined exactly
_SEED_WINDOW_DB = 6.0               # ... within this of the strongest grid sample
_CHUNK = 4_000_000                  # complex elements per evaluation chunk


def _db10(p):
    p = np.asarray(p, dtype=float)
    with np.errstate(divide="ignore"):
        out = 10.0 * np.log10(np.maximum(p, 1e-30))
    out = np.maximum(out, FLOOR_DB)
    return float(out) if out.ndim == 0 else out


# ---------------------------------------------------------------------------
# Steering vectors
# ---------------------------------------------------------------------------
def steering_ff(pos, U, f_hz) -> np.ndarray:
    """Far-field steering ``exp(+j k0 U . r^T)``: pos (N,3), U (3,) or (..., 3) -> (N,) or (..., N)."""
    pos = np.asarray(pos, dtype=float).reshape(-1, 3)
    U = np.asarray(U, dtype=float)
    return np.exp(1j * ac.k0(f_hz) * (U @ pos.T))


def steering_nf(pos, p_src_m, f_hz) -> np.ndarray:
    """Spherical-wave steering for a source at ``p_src_m`` (ENU, relative to the phase reference):
    ``a_n = (|p| / |p - r_n|) exp(-j k0 (|p - r_n| - |p|))``."""
    pos = np.asarray(pos, dtype=float).reshape(-1, 3)
    p = np.asarray(p_src_m, dtype=float).reshape(3)
    rp = float(np.linalg.norm(p))
    rn = np.linalg.norm(p[np.newaxis, :] - pos, axis=1)
    if rp <= 0.0 or np.any(rn <= 0.0):
        raise ValueError("near-field source coincides with the phase reference or an element")
    return (rp / rn) * np.exp(-1j * ac.k0(f_hz) * (rn - rp))


def steering_wb(pos, u, freqs_hz) -> np.ndarray:
    """Per-frequency far-field steering (F, N) for RF frequencies ``freqs_hz``."""
    pos = np.asarray(pos, dtype=float).reshape(-1, 3)
    f = np.asarray(freqs_hz, dtype=float).reshape(-1)
    if np.any(f <= 0):
        raise ValueError("frequencies must be > 0")
    proj = pos @ np.asarray(u, dtype=float).reshape(3)
    return np.exp(2j * np.pi * f[:, np.newaxis] * proj[np.newaxis, :] / C0)


def ttd_delays_s(pos, u0) -> np.ndarray:
    """True-time delays ``tau_n = (u0.r_n - min_m u0.r_m)/c >= 0`` (spec 0.3 D1)."""
    proj = np.asarray(pos, dtype=float).reshape(-1, 3) @ np.asarray(u0, dtype=float).reshape(3)
    if proj.size == 0:
        return proj
    return (proj - proj.min()) / C0


def ttd_delays_nf_s(pos, p_focus_m) -> np.ndarray:
    """Near-field true-time delays toward a focus point ``p_focus_m`` (ENU, relative to the
    phase reference): ``tau_n = (max_m |p - r_m| - |p - r_n|)/c >= 0``; the same sign
    as ``steering_nf`` and equal to ``ttd_delays_s`` in the far-field limit."""
    pos = np.asarray(pos, dtype=float).reshape(-1, 3)
    if pos.shape[0] == 0:
        return np.zeros(0)
    p = np.asarray(p_focus_m, dtype=float).reshape(3)
    rn = np.linalg.norm(p[np.newaxis, :] - pos, axis=1)
    if np.any(rn <= 0.0):
        raise ValueError("near-field focus coincides with an element")
    return (rn.max() - rn) / C0


# ---------------------------------------------------------------------------
# Element patterns
# ---------------------------------------------------------------------------
TABLE_MAX_AZ_GAP_DEG = 90.0         # a wider azimuth gap is "not tabulated"
TABLE_EDGE_TAPER_DEG = 10.0         # dB ramp to the floor beyond a tabulated edge
TABLE_FLOOR_REL_DB = 30.0           # default floor: lowest tabulated gain minus this


def _table_grid(table, floor_db=None, extend="taper"):
    """(els (E,), azs (A,), grid (E, A, 3) of [gain_db, cos phase, sin phase]) with the
    coverage extension of ``ElementPattern`` (virtual floor rows / columns)."""
    rows = np.asarray(table, dtype=float)
    raz = np.asarray(ac.wrap_az(rows[:, 0]), dtype=float).reshape(-1)
    rel = rows[:, 1]
    azs = np.unique(raz)
    els = np.unique(rel)
    ph = np.radians(rows[:, 3])
    grid = np.empty((els.size, azs.size, 3))
    grid[np.searchsorted(els, rel), np.searchsorted(azs, raz)] = np.stack([rows[:, 2], np.cos(ph), np.sin(ph)], -1)
    if extend != "taper" or rows.shape[0] == 1:
        return els, azs, grid
    floor = float(rows[:, 2].min()) - TABLE_FLOOR_REL_DB if floor_db is None else float(floor_db)

    def virtual(src):                               # floor gain, the edge sample's phase
        v = src.copy()
        v[..., 0] = floor
        return v

    # azimuth coverage
    if azs.size == 1:
        add_az = [(ac.wrap_az(azs[0] + 180.0), virtual(grid[:, 0]))]
    else:
        gaps = np.diff(np.concatenate([azs, [azs[0] + 360.0]]))
        k = int(np.argmax(gaps))
        add_az = []
        if gaps[k] > TABLE_MAX_AZ_GAP_DEG:
            k1 = (k + 1) % azs.size
            add_az = [(ac.wrap_az(azs[k] + TABLE_EDGE_TAPER_DEG), virtual(grid[:, k])),
                      (ac.wrap_az(azs[k1] - TABLE_EDGE_TAPER_DEG), virtual(grid[:, k1]))]
    if add_az:
        azs = np.concatenate([azs, [a for a, _ in add_az]])
        grid = np.concatenate([grid, np.stack([c for _, c in add_az], axis=1)], axis=1)
        o = np.argsort(azs, kind="stable")
        azs, grid = azs[o], grid[:, o]
    # elevation coverage
    lo, hi = [], []
    if els.size == 1:
        if els[0] > -90.0:
            lo = [(-90.0, virtual(grid[0]))]
        if els[0] < 90.0:
            hi = [(90.0, virtual(grid[-1]))]
    else:
        if els[0] > -90.0:
            lo = [(max(-90.0, els[0] - TABLE_EDGE_TAPER_DEG), virtual(grid[0]))]
            if lo[0][0] > -90.0:
                lo.insert(0, (-90.0, virtual(grid[0])))
        if els[-1] < 90.0:
            hi = [(min(90.0, els[-1] + TABLE_EDGE_TAPER_DEG), virtual(grid[-1]))]
            if hi[0][0] < 90.0:
                hi.append((90.0, virtual(grid[-1])))
    if lo or hi:
        els = np.concatenate([[e for e, _ in lo], els, [e for e, _ in hi]])
        grid = np.concatenate([np.stack([r for _, r in lo]) if lo else grid[:0], grid,
                               np.stack([r for _, r in hi]) if hi else grid[:0]], axis=0)
    return els, azs, grid


def _table_eval(table, az, el, floor_db=None, extend="taper"):
    """Interpolated (gain_db, phase_deg) of a pattern table at body az/el -> (..., 2).

    Bilinear in (el, az): periodic in azimuth, clamped in elevation; the gain in
    dB and the phase as a unit phasor (renormalized), so a +-180 deg wrap
    between samples interpolates correctly.
    """
    els, azs, grid = _table_grid(table, floor_db, extend)
    az = np.asarray(az, dtype=float)
    el = np.asarray(el, dtype=float)
    if els.size == 1:                                   # constant along elevation
        els = np.array([els[0], els[0] + 1.0])
        grid = np.concatenate([grid, grid], axis=0)
    azx = np.concatenate([azs, [azs[0] + 360.0]])
    gx = np.concatenate([grid, grid[:, :1]], axis=1)
    a = np.mod(az - azs[0], 360.0) + azs[0]
    j = np.clip(np.searchsorted(azx, a, side="right") - 1, 0, azs.size - 1)
    ta = (a - azx[j]) / (azx[j + 1] - azx[j])
    e = np.clip(el, els[0], els[-1])
    i = np.clip(np.searchsorted(els, e, side="right") - 1, 0, els.size - 2)
    te = (e - els[i]) / (els[i + 1] - els[i])
    ta, te = ta[..., None], te[..., None]
    v = ((1 - te) * ((1 - ta) * gx[i, j] + ta * gx[i, j + 1])
         + te * ((1 - ta) * gx[i + 1, j] + ta * gx[i + 1, j + 1]))
    return np.stack([v[..., 0], np.degrees(np.arctan2(v[..., 2], v[..., 1]))], axis=-1)


def element_gain(pattern: ElementPattern, u_body) -> np.ndarray:
    """Complex field gain toward body-frame unit direction(s) ``u_body`` (..., 3) -> (...).

    ``|g|^2`` is the element power gain (linear, peak = 10^(gain_dbi/10)); only
    a table pattern carries phase.
    """
    u = np.asarray(u_body, dtype=float)
    shape = u.shape[:-1]
    kind = pattern.kind
    if kind == "table":
        az, el = direction_angles(u.reshape(-1, 3))
        ge = _table_eval(pattern.table, np.atleast_1d(az), np.atleast_1d(el),
                         pattern.table_floor_db, pattern.table_extend)
        g_db = ge[..., 0]
        if pattern.gain_dbi is not None:
            g_db = g_db + (float(pattern.gain_dbi) - max(r[2] for r in pattern.table))
        out = 10.0 ** (g_db / 20.0) * np.exp(1j * np.radians(ge[..., 1]))
        return out.reshape(shape)
    amp = 10.0 ** (pattern.effective_gain_dbi() / 20.0)
    if kind == "isotropic":
        return np.full(shape, amp, dtype=np.complex128)
    b = direction_vector(pattern.boresight_az_deg, pattern.boresight_el_deg)
    if kind == "cos_q":
        c = u @ b
        with np.errstate(invalid="ignore"):
            f = np.where(c > 0.0, np.power(np.clip(c, 0.0, 1.0), float(pattern.q) / 2.0), 0.0)
        return (amp * f).astype(np.complex128)
    if kind == "dipole_v":
        axis = np.array([0.0, 0.0, 1.0])
    else:                                                       # dipole_h: horizontal, across boresight
        axis = np.cross(b, [0.0, 0.0, 1.0])
        nrm = np.linalg.norm(axis)
        axis = axis / nrm if nrm > 1e-9 else np.array([1.0, 0.0, 0.0])
    cp = np.clip(u @ axis, -1.0, 1.0)
    sp = np.sqrt(1.0 - cp * cp)
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(sp > 1e-9, np.cos(0.5 * np.pi * cp) / sp, 0.0)
    return (amp * f).astype(np.complex128)


def _all_isotropic(geom, which) -> bool:
    return all(e.pattern.kind == "isotropic" for e in geom.elements_for(which))


def _element_gains(geom, U, which, n_out):
    """Element field gains (..., N) (or (..., 1) for one shared pattern) toward ENU directions U (..., 3)."""
    els = geom.elements_for(which)
    if not els:
        return None
    ub = geom.enu_to_body(np.asarray(U, dtype=float))
    groups = {}
    for n, e in enumerate(els):
        groups.setdefault(e.pattern, []).append(n)
    if len(groups) == 1:
        return element_gain(next(iter(groups)), ub)[..., np.newaxis]
    G = np.empty(tuple(ub.shape[:-1]) + (n_out,), dtype=np.complex128)
    for pat, idx in groups.items():
        G[..., idx] = element_gain(pat, ub)[..., np.newaxis]
    return G


def array_manifold(geom, U_enu, f_hz, which="rx", include_pattern=True) -> np.ndarray:
    """Steering vectors including element patterns: (P, N) for U (P, 3) (or (N,) for U (3,))."""
    els = geom.elements_for(which)
    pos = geom.positions(which)
    U = np.asarray(U_enu, dtype=float)
    A = steering_ff(pos, U, f_hz)
    if not include_pattern or not els:
        return A
    G = _element_gains(geom, U, which, A.shape[-1])
    return A if G is None else A * G


def az_el_grid(az_step=0.25, el_step=0.25, az_range=(0, 360), el_range=(-90, 90)) -> tuple:
    """(az (A,), el (E,), U (E, A, 3)). A full 360 deg az range excludes the duplicate end."""
    a0, a1 = float(az_range[0]), float(az_range[1])
    e0, e1 = float(el_range[0]), float(el_range[1])
    if not (az_step > 0 and el_step > 0 and a1 > a0 and e1 >= e0):
        raise ValueError("invalid grid")
    na = int(round((a1 - a0) / az_step))
    if abs(a1 - a0 - 360.0) < 1e-9:
        az = a0 + az_step * np.arange(na)
    else:
        az = a0 + az_step * np.arange(na + 1)
        az = az[az <= a1 + 1e-9]
    ne = int(round((e1 - e0) / el_step))
    el = e0 + el_step * np.arange(ne + 1)
    el = el[el <= e1 + 1e-9]
    AZ, EL = np.meshgrid(az, el)
    return az, el, direction_vector(AZ, EL)


def array_factor(pos, w, f_hz, U) -> np.ndarray:
    """``w^H a(U)`` (isotropic elements)."""
    return steering_ff(pos, U, f_hz) @ np.conj(np.asarray(w, dtype=np.complex128))


def _field(geom, w, f_hz, U, which, include_pattern):
    """E(U) = w^H a(U) with element patterns, evaluated in chunks."""
    w = np.conj(np.asarray(w, dtype=np.complex128))
    U = np.asarray(U, dtype=float)
    flat = U.reshape(-1, 3)
    n = max(1, len(w))
    out = np.empty(flat.shape[0], dtype=np.complex128)
    step = max(1, _CHUNK // n)
    for s in range(0, flat.shape[0], step):
        out[s:s + step] = array_manifold(geom, flat[s:s + step], f_hz, which, include_pattern) @ w
    return out.reshape(U.shape[:-1])


def pattern_db(geom, w, f_hz, U, which="rx", normalize=True, include_pattern=True) -> np.ndarray:
    """Power pattern ``20 log10 |w^H a(U)|`` (dB, floored at -300); normalized to its max over U."""
    p = np.abs(_field(geom, w, f_hz, U, which, include_pattern)) ** 2
    if normalize:
        m = float(p.max()) if p.size else 0.0
        if m > 0:
            p = p / m
    return _db10(p)


# ---------------------------------------------------------------------------
# Directivity, efficiency, floors, squint
# ---------------------------------------------------------------------------
def directivity_isotropic_dbi(pos, w, f_hz, u0) -> float:
    """Closed form ``|w^H a0|^2 / (w^H S w)``, ``S_mn = sinc(k0 |r_m - r_n|)`` (isotropic elements)."""
    pos = np.asarray(pos, dtype=float).reshape(-1, 3)
    w = np.asarray(w, dtype=np.complex128)
    a0 = steering_ff(pos, u0, f_hz)
    dist = np.linalg.norm(pos[:, None, :] - pos[None, :, :], axis=-1)
    S = np.sinc(ac.k0(f_hz) * dist / np.pi)
    den = float(np.real(np.conj(w) @ S @ w))
    num = float(abs(np.vdot(w, a0)) ** 2)
    if den <= 0:
        return FLOOR_DB
    return _db10(num / den)


def _widest_flood(L, seed, shifts, tol, max_iter):
    """Cells reachable from ``seed`` by a path that never rises more than ``tol`` above the
    lowest level met so far (maximum-bottleneck relaxation, vectorized)."""
    rm = np.where(seed, L, -np.inf)
    for _ in range(int(max_iter)):
        best = rm.copy()
        for nb in shifts(rm):
            cand = np.where(L <= nb + tol, np.minimum(nb, L), -np.inf)
            np.maximum(best, cand, out=best)
        if np.array_equal(best, rm):
            break
        rm = best
    return rm > -np.inf


class _Grid:
    """Cell-centre az/el grid over the sphere (even az count, pole-aware adjacency)."""

    def __init__(self, step_deg):
        step = float(step_deg)
        E = int(math.ceil(180.0 / step))
        A = 2 * int(math.ceil(180.0 / step))
        while E * A > _GRID_MAX_POINTS:
            E = int(E * 0.9)
            A = 2 * int(A * 0.45)
        self.E, self.A = E, A
        self.d_el = 180.0 / E
        self.d_az = 360.0 / A
        self.el = -90.0 + self.d_el * (np.arange(E) + 0.5)
        self.az = self.d_az * (np.arange(A) + 0.5)
        AZ, EL = np.meshgrid(self.az, self.el)
        self.U = direction_vector(AZ, EL)
        self.w = np.cos(np.radians(EL)) * math.radians(self.d_az) * math.radians(self.d_el)

    def cell(self, u):
        az, el = direction_angles(np.asarray(u, dtype=float))
        i = int(np.clip(np.floor((el + 90.0) / self.d_el), 0, self.E - 1))
        j = int(np.floor(az / self.d_az)) % self.A
        return i, j

    def cells(self, U):
        az, el = direction_angles(np.asarray(U, dtype=float))
        i = np.clip(np.floor((np.asarray(el) + 90.0) / self.d_el).astype(int), 0, self.E - 1)
        j = np.floor(np.asarray(az) / self.d_az).astype(int) % self.A
        return i, j

    def shifts(self, X):
        """Yield the 8 neighbour views of X (wrap in az, cross the poles)."""
        h = self.A // 2
        top = np.roll(X[-1:], h, axis=1)
        bot = np.roll(X[:1], h, axis=1)
        P = np.concatenate([bot, X, top], axis=0)               # (E+2, A)
        for di in (-1, 0, 1):
            rows = P[1 + di:1 + di + self.E]
            for dj in (-1, 0, 1):
                if di == 0 and dj == 0:
                    continue
                yield np.roll(rows, -dj, axis=1)

    def label(self, mask):
        """Connected components with az wrap and pole merging -> (labels, count)."""
        lab, n = ndimage.label(mask, structure=np.ones((3, 3), dtype=int))
        if n == 0:
            return lab, 0
        parent = list(range(n + 1))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            if a and b:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

        for i in range(self.E):
            for di in (-1, 0, 1):
                k = i + di
                if 0 <= k < self.E:
                    union(lab[i, 0], lab[k, -1])
        h = self.A // 2
        for row in (0, self.E - 1):
            for j in range(self.A):
                for dj in (-1, 0, 1):
                    union(lab[row, j], lab[row, (j + h + dj) % self.A])
        root = np.array([find(a) for a in range(n + 1)])
        lab = root[lab]
        uniq = np.unique(lab[lab > 0])
        remap = np.zeros(n + 1, dtype=int)
        remap[uniq] = np.arange(1, uniq.size + 1)
        return remap[lab], int(uniq.size)


def _default_step(geom, f_hz, which):
    D = max(geom.aperture_m(which), 1e-6)
    return float(np.clip(math.degrees(ac.wavelength(f_hz) / D) / 10.0, 0.05, 1.0))


def directivity_numeric_dbi(geom, w, f_hz, u0, which="rx", include_pattern=True, grid_step_deg=None) -> float:
    """``4 pi |E(u0)|^2 / integral |E|^2 dOmega`` on a cos(el)-weighted grid."""
    step = grid_step_deg or _default_step(geom, f_hz, which)
    g = _Grid(step)
    p = np.abs(_field(geom, w, f_hz, g.U, which, include_pattern)) ** 2
    tot = float(np.sum(p * g.w))
    p0 = float(abs(_field(geom, w, f_hz, np.asarray(u0, dtype=float), which, include_pattern)) ** 2)
    return _db10(4.0 * math.pi * p0 / tot) if tot > 0 else FLOOR_DB


def taper_efficiency(w, a0=None) -> float:
    """``|sum w|^2 / (N sum |w|^2)``; with ``a0`` (unit-modulus steering) ``|w^H a0|^2/(N ||w||^2)``."""
    w = np.asarray(w, dtype=np.complex128).reshape(-1)
    n = w.size
    den = n * float(np.sum(np.abs(w) ** 2))
    if den <= 0:
        return 0.0
    if a0 is None:
        return float(abs(np.sum(w)) ** 2 / den)
    return float(abs(np.vdot(w, np.asarray(a0, dtype=np.complex128))) ** 2 / den)


def cal_limited_null_floor_db(phase_rms_deg, amp_rms_db, w) -> float:
    """Expected null residual relative to the beam peak: ``eps^2 / (eta_t N)`` with
    ``eps^2 = sigma_phi^2 + (10^(A/20) - 1)^2`` (eta_t from |w|). -300 dB when eps = 0."""
    aw = np.abs(np.asarray(w, dtype=np.complex128).reshape(-1))
    n = aw.size
    if n == 0 or not np.any(aw > 0):
        return FLOOR_DB
    eta = float(np.sum(aw) ** 2 / (n * np.sum(aw ** 2)))
    eps2 = math.radians(float(phase_rms_deg)) ** 2 + (10.0 ** (float(amp_rms_db) / 20.0) - 1.0) ** 2
    return _db10(eps2 / (eta * n)) if eps2 > 0 else FLOOR_DB


def squint_deg(theta0_deg, f0_hz, f_hz) -> float:
    """Phase-steering squint ``asin((f0/f) sin theta0) - theta0`` (clamped at +-90 deg)."""
    s = (float(f0_hz) / float(f_hz)) * math.sin(math.radians(float(theta0_deg)))
    return math.degrees(math.asin(max(-1.0, min(1.0, s)))) - float(theta0_deg)


def narrowband_ok(aperture_m, bw_hz, theta0_deg, limit=0.1) -> bool:
    """Phase steering is acceptable when ``B D |sin theta0| / c <= limit``."""
    return float(bw_hz) * float(aperture_m) * abs(math.sin(math.radians(float(theta0_deg)))) / C0 <= float(limit)


# ---------------------------------------------------------------------------
# Beam metrics
# ---------------------------------------------------------------------------
@dataclass
class Lobe:
    az_deg: float
    el_deg: float
    level_db: float
    kind: str                       # grating | ambiguity | sidelobe
    scan_az_deg: float = None       # numeric search: the look direction that exposes the lobe
    scan_el_deg: float = None
    intrinsic: bool = False         # the cone / mirror image of the main lobe (linear / planar set)
    edge: bool = False              # partial lobe at the edge of visible space (endfire / in-plane horizon)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BeamMetrics:
    peak_az_deg: float
    peak_el_deg: float
    pointing_error_deg: float
    hpbw_az_deg: float              # 360 = no -3 dB crossing in that cut
    hpbw_el_deg: float
    psll_db: float                  # -300 = no sidelobe outside the main lobe
    islr_db: float
    directivity_dbi: float
    taper_eff: float
    null_depths_db: list
    cal_limited_null_floor_db: float
    squint_ok: bool
    fill_time_bw_product: float
    lobes: list = field(default_factory=list)
    grid_step_deg: float = 0.0      # sphere grid step actually used
    local_refine: bool = False      # near-in sidelobes re-gridded at HPBW/10 (large apertures)
    near_field_m: float = 0.0       # > 0: measured on the focal sphere of this radius (focused beam)
    look_level_db: float = 0.0      # level toward the commanded look u0 relative to the peak

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lobes"] = [lb.to_dict() if isinstance(lb, Lobe) else dict(lb) for lb in self.lobes]
        return d

    @staticmethod
    def from_dict(d) -> "BeamMetrics":
        d = dict(d)
        d["lobes"] = [Lobe(**lb) for lb in d.get("lobes", [])]
        d["null_depths_db"] = list(d.get("null_depths_db", []))
        return BeamMetrics(**d)


def _angle_deg(u, v) -> float:
    c = float(np.clip(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v)), -1.0, 1.0))
    return math.degrees(math.acos(c))


def _tangents(u):
    z = np.array([0.0, 0.0, 1.0])
    e = np.cross(z, u)
    if np.linalg.norm(e) < 1e-6:
        e = np.array([1.0, 0.0, 0.0])
    e = e / np.linalg.norm(e)
    up = np.cross(u, e)
    return e, up / np.linalg.norm(up)


class _LocalPatch:
    """Gnomonic (tangent-plane) grid around the beam peak at HPBW/10, for apertures whose
    near-in sidelobes the sphere grid cannot resolve. The main lobe (and, for linear /
    planar sets, its cone / mirror image) is flooded on this grid; the strongest local
    maxima outside it are refined exactly."""

    def __init__(self, P, u_pk, e_az, e_el, hp_az, hp_el, p_pk, rank, B, comp):
        self.P, self.u, self.e1, self.e2, self.p_pk = P, u_pk, e_az, e_el, p_pk
        fin = [h for h in (hp_az, hp_el) if h < 360.0]
        # isotropic step (an anisotropic grid lets the flood skip tilted nulls diagonally)
        st = min(fin) / _PATCH_HPBW_STEP
        half, stp = [], []
        for h in (hp_az, hp_el):
            hh = min(_PATCH_MAX_HALF_DEG, _PATCH_HALF_HPBW * (h if h < 360.0 else max(fin)),
                     0.5 * (_PATCH_MAX_SIDE - 1) * st)
            n = max(1, int(math.ceil(hh / st)))
            half.append(n * st)
            stp.append(st)
        self.half, self.step = half, stp
        self.x = np.linspace(-half[0], half[0], 2 * int(round(half[0] / stp[0])) + 1)
        self.y = np.linspace(-half[1], half[1], 2 * int(round(half[1] / stp[1])) + 1)
        X, Y = np.meshgrid(self.x, self.y)                      # (ny, nx)
        self.U = self._dir(X, Y)
        self.L = _db10(P(self.U) / p_pk)
        c = (self.y.size // 2, self.x.size // 2)
        seed = np.zeros(self.L.shape, dtype=bool)
        seed[c] = True
        main = _widest_flood(self.L, seed, _box_shifts(self.L.shape), _MAIN_LOBE_TOL_DB, self.L.size)
        if rank == 2:
            n_ = comp[:, 0]
            Um = self.U - 2.0 * (self.U @ n_)[..., None] * n_
            ok, iy, ix = self._index(Um)
            img = np.zeros_like(main)
            img[ok] = main[iy[ok], ix[ok]]
            main = main | img
        elif rank == 1:
            v = self.U @ B[:, 0]
            vm = v[main]
            main = main | ((v >= vm.min() - 1e-12) & (v <= vm.max() + 1e-12))
        self.main = main
        self._side = None

    def _dir(self, X, Y):
        U = (self.u + np.tan(np.radians(X))[..., None] * self.e1 + np.tan(np.radians(Y))[..., None] * self.e2)
        return U / np.linalg.norm(U, axis=-1, keepdims=True)

    def _coords(self, U):
        U = np.asarray(U, dtype=float).reshape(-1, 3)
        c = U @ self.u
        with np.errstate(divide="ignore", invalid="ignore"):
            x = np.degrees(np.arctan((U @ self.e1) / c))
            y = np.degrees(np.arctan((U @ self.e2) / c))
        front = c > 1e-9
        return front, x, y

    def contains(self, U):
        front, x, y = self._coords(U)
        return front & (np.abs(x) <= self.half[0]) & (np.abs(y) <= self.half[1])

    def _index(self, U):
        shape = np.asarray(U).shape[:-1]
        front, x, y = self._coords(U)
        ok = front & (np.abs(x) <= self.half[0]) & (np.abs(y) <= self.half[1])
        ix = np.clip(np.rint((np.nan_to_num(x) + self.half[0]) / self.step[0]).astype(int), 0, self.x.size - 1)
        iy = np.clip(np.rint((np.nan_to_num(y) + self.half[1]) / self.step[1]).astype(int), 0, self.y.size - 1)
        return ok.reshape(shape), iy.reshape(shape), ix.reshape(shape)

    def sidelobes(self, k=8):
        """[(level_db, az, el)] of the ``k`` strongest local maxima outside the main lobe."""
        if self._side is not None:
            return self._side
        out = []
        Lo = np.where(self.main, -np.inf, self.L)
        if np.isfinite(Lo).any():
            nb = np.full(Lo.shape, -np.inf)
            for v in _box_shifts(Lo.shape)(Lo):
                np.maximum(nb, v, out=nb)
            pk = np.argwhere(np.isfinite(Lo) & (Lo >= nb))
            pk = pk[np.argsort(-Lo[pk[:, 0], pk[:, 1]], kind="stable")[:k]]
            for iy, ix in pk:
                x0 = np.array([self.x[ix], self.y[iy]])
                best = (float(self.L[iy, ix]), x0)

                def obj(z):
                    return -float(_db10(self.P(self._dir(np.array(z[0]), np.array(z[1]))) / self.p_pk))

                st = [self.step[0] / 2.0, self.step[1] / 2.0]
                r = optimize.minimize(obj, x0, method="Nelder-Mead",
                                      options={"xatol": 1e-5, "maxiter": 300,
                                               "initial_simplex": [x0, x0 + [st[0], 0], x0 + [0, st[1]]]})
                if -r.fun > best[0]:
                    ok, jy, jx = self._index(self._dir(np.array(r.x[0]), np.array(r.x[1]))[np.newaxis, :])
                    if ok[0] and not self.main[jy[0], jx[0]]:
                        best = (-float(r.fun), r.x)
                az, el = direction_angles(self._dir(np.array(best[1][0]), np.array(best[1][1])))
                out.append((best[0], float(az), float(el)))
        self._side = out
        return out


def _hpbw_cut(P, u_pk, p_pk, e, dth) -> float:
    """-3 dB width (deg) of the great-circle cut through ``u_pk`` along tangent ``e``; 360 = no crossing."""
    def lev(th):
        t = math.radians(th)
        return _db10(P(math.cos(t) * u_pk + math.sin(t) * e) / p_pk) - HALF_POWER_DB

    sides = []
    for sgn in (1.0, -1.0):
        prev, found = 0.0, None
        th = dth
        while th <= 180.0:
            if lev(sgn * th) < 0.0:
                found = optimize.brentq(lambda t: lev(sgn * t), prev, th, xtol=1e-6)
                break
            prev, th = th, th + dth
        if found is None:
            return 360.0
        sides.append(found)
    return float(sum(sides))


def _edge_of_visible(u, rank, B, tol_deg) -> bool:
    """True when ``u`` lies within ``tol_deg`` of the visible-space boundary of a linear /
    planar set (|B^T u| = 1: endfire for a line, the in-plane horizon for a plane)."""
    if rank not in (1, 2):
        return False
    return float(np.linalg.norm(B.T @ np.asarray(u, dtype=float).reshape(3))) >= math.cos(math.radians(tol_deg))


def _cone_ridge_lobe(P, u_pk, p_pk, axis, n=720):
    """Strongest point of the intrinsic cone ridge ``{u : u.axis = u_pk.axis}`` of a linear set
    outside the ridge's own main lobe, as an intrinsic ``ambiguity`` Lobe (None when the ridge
    degenerates, is all main lobe or stays below -6 dB). The array factor is constant along
    the ridge, so only the element patterns shape it. The ridge main lobe is the stretch
    reached from the peak by descending (``_MAIN_LOBE_TOL_DB`` plateau tolerance, as the
    flood fill): a flat ridge (isotropic elements) is all main lobe; a ridge rising away from
    the peak exposes its stronger part (level > 0 dB)."""
    e = np.asarray(axis, dtype=float).reshape(3)
    e = e / np.linalg.norm(e)
    c = float(u_pk @ e)
    s_ = math.sqrt(max(0.0, 1.0 - c * c))
    if s_ < 1e-6:
        return None
    m1 = u_pk - c * e
    m1 = m1 / np.linalg.norm(m1)
    m2 = np.cross(e, m1)
    phi = 2.0 * math.pi * np.arange(n) / n                   # phi = 0 is the peak
    U = c * e + s_ * (np.cos(phi)[:, None] * m1 + np.sin(phi)[:, None] * m2)
    lv = _db10(np.asarray(P(U), dtype=float) / p_pk)
    main = np.zeros(n, dtype=bool)
    main[0] = True
    for sgn in (1, -1):
        low = float(lv[0])
        for k in range(1, n):
            j = (sgn * k) % n
            if lv[j] > low + _MAIN_LOBE_TOL_DB:
                break
            main[j] = True
            low = min(low, float(lv[j]))
    if main.all():
        return None
    best = float(np.max(lv[~main]))
    if best <= -6.0:
        return None
    k = int(np.flatnonzero(~main)[np.argmax(lv[~main])])
    az, el = direction_angles(U[k])
    return Lobe(float(az), float(el), best, "ambiguity", intrinsic=True)


def _nf_field_fn(geom, w, f_hz, r_m, which, include_pattern):
    """E(U) on the sphere of radius ``r_m`` around the phase reference: spherical waves
    ``(r/R_n) exp(-j k0 (R_n - r))`` (``steering_nf``) times the element gains toward U, and
    the distance (..., ) from each point to its nearest element."""
    pos = geom.positions(which)
    wc = np.conj(np.asarray(w, dtype=np.complex128).reshape(-1))
    k = ac.k0(f_hz)
    r = float(r_m)

    def E(U):
        U = np.asarray(U, dtype=float)
        flat = U.reshape(-1, 3)
        out = np.empty(flat.shape[0], dtype=np.complex128)
        dmin = np.empty(flat.shape[0])
        step = max(1, _CHUNK // max(1, pos.shape[0]))
        for s0 in range(0, flat.shape[0], step):
            Uc = flat[s0:s0 + step]
            R = np.linalg.norm((r * Uc)[:, None, :] - pos[None, :, :], axis=2)
            dmin[s0:s0 + step] = R.min(axis=1)
            R = np.maximum(R, 1e-12)
            a = (r / R) * np.exp(-1j * k * (R - r))
            if include_pattern:
                g = _element_gains(geom, Uc, which, pos.shape[0])
                if g is not None:
                    a = a * g
            out[s0:s0 + step] = a @ wc
        return out.reshape(U.shape[:-1]), dmin.reshape(U.shape[:-1])
    return E


def _near_field_metrics(geom, w, f_hz, u0, r_m, nulls, which, bw_hz, cal_phase_rms_deg, cal_amp_rms_db,
                        include_pattern) -> BeamMetrics:
    """``beam_metrics`` of a beam focused at ``r_m u0`` (see there): evaluated on the focal
    sphere of radius ``r_m`` around the phase reference, locally around the focus."""
    pos = geom.positions(which)
    lam = ac.wavelength(f_hz)
    E = _nf_field_fn(geom, w, f_hz, r_m, which, include_pattern)

    def P(U):
        return np.abs(E(U)[0]) ** 2

    D_ap = max(geom.aperture_m(which), 1e-6)
    bw_est = math.degrees(min(math.pi, lam / D_ap))
    az0, el0 = direction_angles(u0)

    def obj(x):
        el = float(np.clip(x[1], -90.0, 90.0))
        u = direction_vector(x[0], el)
        th = math.degrees(math.acos(max(-1.0, min(1.0, float(u @ u0)))))
        return -(_db10(P(u)) - _PEAK_PENALTY_FINE * th * th)

    x0 = np.array([float(az0), float(el0)])
    st = max(0.05, bw_est / 8.0)
    res = optimize.minimize(obj, x0, method="Nelder-Mead",
                            options={"xatol": 1e-6, "fatol": 1e-9, "maxiter": 800,
                                     "initial_simplex": [x0, x0 + [st, 0], x0 + [0, st]]})
    xp = res.x if res.fun <= obj(x0) else x0
    pk_el = float(np.clip(xp[1], -90.0, 90.0))
    pk_az = float(ac.wrap_az(xp[0]))
    u_pk = direction_vector(pk_az, pk_el)
    p_pk = float(P(u_pk))
    if p_pk <= 0:
        raise ValueError("beam has no response (all-zero weights?)")
    dth = max(1e-3, min(0.5, math.degrees(0.886 * lam / D_ap) / 4.0))
    e_az, e_el = _tangents(u_pk)
    hpbw_az = _hpbw_cut(P, u_pk, p_pk, e_az, dth)
    hpbw_el = _hpbw_cut(P, u_pk, p_pk, e_el, dth)
    finite = [h for h in (hpbw_az, hpbw_el) if h < 360.0]
    # PSLL along the two principal cuts out to 6 beamwidths (90 deg at most), stopping where
    # the focal sphere comes closer to an element than half the focus-to-nearest-element
    # distance (beyond that point the 1/R field of that single element dominates: a
    # proximity effect, not a beam lobe)
    th_max = min(90.0, 6.0 * max(finite)) if finite else 90.0
    th = np.arange(dth, th_max + 0.5 * dth, dth)
    d_stop = 0.5 * float(np.min(np.linalg.norm(r_m * u0[None, :] - pos, axis=1)))
    rank, B, comp = ag.position_subspace(pos)
    # main lobe and its intrinsic cone / mirror images: within ~ the first null of the peak in
    # the position subspace (|B^T (u - u_pk)| < (1 - 1/N) lambda / D); never a sidelobe
    delta = (1.0 - 1.0 / max(pos.shape[0], 2)) * lam / D_ap
    psll = FLOOR_DB
    for e in (e_az, e_el):
        for sgn in (1.0, -1.0):
            t = np.radians(sgn * th)
            U = np.cos(t)[:, None] * u_pk + np.sin(t)[:, None] * e
            f, dmin = E(U)
            bad = np.flatnonzero(dmin < d_stop)
            m = int(bad[0]) if bad.size else th.size
            L = _db10(np.abs(f[:m]) ** 2 / p_pk)
            if L.size < 3:
                continue
            mins = np.flatnonzero((L[1:-1] < L[:-2]) & (L[1:-1] <= L[2:])) + 1
            if not mins.size:
                continue
            # true lobes only: interior local maxima past the first null, plus the last sample
            # when the walk ended at th_max (not when it rises into an element's proximity zone)
            Lp = np.concatenate([[np.inf], L, [np.inf if m < th.size else -np.inf]])
            pk = np.flatnonzero((L >= Lp[:-2]) & (L >= Lp[2:]))
            pk = pk[pk > mins[0]]
            if rank in (1, 2) and pk.size:
                pk = pk[np.linalg.norm((U[:m][pk] - u_pk) @ B, axis=1) >= delta]
            if pk.size:
                psll = max(psll, float(L[pk].max()))
    lobes = []
    excl = 0.5 * max(finite) if finite else 90.0
    if rank == 1:
        lb = _cone_ridge_lobe(P, u_pk, p_pk, B[:, 0])
        if lb is not None:
            lobes.append(lb)
    elif rank == 2:
        n_ = comp[:, 0]
        um = u_pk - 2.0 * float(u_pk @ n_) * n_
        if _angle_deg(um, u_pk) > excl:
            lv = _db10(P(um) / p_pk)
            if lv > -6.0:
                az_m, el_m = direction_angles(um)
                lobes.append(Lobe(float(az_m), float(el_m), float(lv), "ambiguity", intrinsic=True))
    a_nf = steering_nf(pos, r_m * u0, f_hz)
    wv = np.asarray(w, dtype=np.complex128).reshape(-1)
    den = float(np.sum(np.abs(wv) ** 2) * np.sum(np.abs(a_nf) ** 2))
    gain = _db10(pos.shape[0] * float(abs(np.vdot(wv, a_nf)) ** 2) / den) if den > 0 else FLOOR_DB
    nd = [float(_db10(np.abs(_field(geom, wv, f_hz, direction_vector(float(az), float(el)), which,
                                     include_pattern)) ** 2 / p_pk)) for az, el in nulls]
    rn = np.linalg.norm(r_m * u0[None, :] - pos, axis=1)
    fill = float(bw_hz) * float(np.ptp(rn)) / C0 if rn.size else 0.0
    return BeamMetrics(
        peak_az_deg=pk_az, peak_el_deg=pk_el, pointing_error_deg=_angle_deg(u_pk, u0),
        hpbw_az_deg=hpbw_az, hpbw_el_deg=hpbw_el, psll_db=float(psll), islr_db=FLOOR_DB,
        directivity_dbi=float(gain), taper_eff=taper_efficiency(wv, np.exp(1j * np.angle(a_nf))),
        null_depths_db=nd,
        cal_limited_null_floor_db=cal_limited_null_floor_db(cal_phase_rms_deg, cal_amp_rms_db, wv),
        squint_ok=bool(fill <= 0.1), fill_time_bw_product=fill, lobes=lobes,
        grid_step_deg=float(dth), local_refine=False, near_field_m=float(r_m),
        look_level_db=float(_db10(P(u0) / p_pk)))


def beam_metrics(geom, w, f_hz, u0, nulls=(), which="rx", bw_hz=0.0, cal_phase_rms_deg=0.0,
                 cal_amp_rms_db=0.0, grid_step_deg=None, include_pattern=True, focus_range_m=0.0) -> BeamMetrics:
    """Metrics of the beam ``E(u) = w^H a(u)`` steered toward ``u0`` (ENU unit vector).

    For TX weights ``t`` pass ``w = conj(t)`` (reciprocity). The main lobe is the
    region reached from the peak by descending (0.3 dB plateau tolerance); for
    linear / planar sets its intrinsic image (cone / mirror) is also part of the
    main lobe and reported as an intrinsic ``ambiguity`` lobe when separate (for a
    line: the strongest point of the cone ridge away from the peak). Lobes whose
    maximum sits on the visible-space boundary carry ``edge = True``.

    ``focus_range_m > 0`` (weights focused at ``focus_range_m u0``): the far-field
    pattern of focused weights is defocused and meaningless inside the Fraunhofer
    distance, so the beam is measured on the focal sphere of that radius around the
    phase reference with spherical-wave steering: peak / pointing / HPBW near the
    focus, PSLL along the two principal cuts (out to 6 HPBW, stopping where a point is
    closer to an element than half the focus-to-nearest-element distance), the intrinsic cone / mirror image only in ``lobes``, ``islr_db``
    -300 (not evaluated), ``directivity_dbi`` the coherent array gain at the focus
    (dB over one element), null depths in the far field (nulls are far-field
    constraints) relative to the focal peak. ``near_field_m`` records the range.
    """
    w = np.asarray(w, dtype=np.complex128).reshape(-1)
    u0 = np.asarray(u0, dtype=float).reshape(3)
    u0 = u0 / np.linalg.norm(u0)
    pos = geom.positions(which)
    if pos.shape[0] != w.size:
        raise ValueError("weights have %d entries but the %s set has %d elements" % (w.size, which, pos.shape[0]))
    fr = float(focus_range_m or 0.0)
    if not (math.isfinite(fr) and fr >= 0.0):
        raise ValueError("focus_range_m must be finite and >= 0")
    if fr > 0.0:
        return _near_field_metrics(geom, w, f_hz, u0, fr, nulls, which, bw_hz, cal_phase_rms_deg,
                                   cal_amp_rms_db, include_pattern)
    step = float(grid_step_deg) if grid_step_deg else _default_step(geom, f_hz, which)
    g = _Grid(step)

    def P(U):
        return np.abs(_field(geom, w, f_hz, U, which, include_pattern)) ** 2

    def P_ae(x):
        el = float(np.clip(x[1], -90.0, 90.0))
        return float(P(direction_vector(x[0], el)))

    Pg = P(g.U)
    # --- peak: hill-climb from u0 on the grid, then refine. A tiny penalty on the
    # angle from u0 picks the point nearest the look on a flat ridge (cone / mirror).
    th_grid = np.degrees(np.arccos(np.clip(g.U @ u0, -1.0, 1.0)))
    Lpen = _db10(Pg) - _PEAK_PENALTY_GRID * th_grid ** 2
    i, j = g.cell(u0)
    for _ in range(g.E * g.A):
        best = (Lpen[i, j], i, j)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                ii, jj = i + di, (j + dj) % g.A
                if ii < 0 or ii >= g.E:
                    ii, jj = (0 if ii < 0 else g.E - 1), (jj + g.A // 2) % g.A
                if Lpen[ii, jj] > best[0]:
                    best = (Lpen[ii, jj], ii, jj)
        if best[1:] == (i, j):
            break
        i, j = best[1], best[2]

    def obj(x):
        el = float(np.clip(x[1], -90.0, 90.0))
        u = direction_vector(x[0], el)
        th = math.degrees(math.acos(max(-1.0, min(1.0, float(u @ u0)))))
        return -(_db10(P(u)) - _PEAK_PENALTY_FINE * th * th)

    x0 = np.array([g.az[j], g.el[i]])
    res = optimize.minimize(obj, x0, method="Nelder-Mead",
                            options={"xatol": 1e-6, "fatol": 1e-9, "maxiter": 800,
                                     "initial_simplex": [x0, x0 + [step / 2, 0], x0 + [0, step / 2]]})
    xp = res.x if res.fun <= obj(x0) else x0
    pk_el = float(np.clip(xp[1], -90.0, 90.0))
    pk_az = float(ac.wrap_az(xp[0]))
    u_pk = direction_vector(pk_az, pk_el)
    p_pk = float(P(u_pk))
    if p_pk <= 0:
        raise ValueError("beam has no response (all-zero weights?)")
    L = _db10(Pg / p_pk)

    # --- HPBW in two orthogonal great-circle cuts through the peak
    D_ap = max(geom.aperture_m(which), 1e-6)
    dth = max(1e-3, min(step / 2.0, math.degrees(0.886 * ac.wavelength(f_hz) / D_ap) / 4.0))

    e_az, e_el = _tangents(u_pk)
    hpbw_az, hpbw_el = _hpbw_cut(P, u_pk, p_pk, e_az, dth), _hpbw_cut(P, u_pk, p_pk, e_el, dth)

    # --- main lobe: widest-path flood from the peak cell (a path never climbs more than
    # the tolerance above its lowest point, so it stops at the minima around the lobe).
    # The path length is limited to ~2.5 HPBW; the intrinsic image below supplies
    # the rest of a cone / mirror ridge.
    i, j = g.cell(u_pk)
    seed = np.zeros(Pg.shape, dtype=bool)
    seed[i, j] = True
    finite = [h for h in (hpbw_az, hpbw_el) if h < 360.0]
    cell_deg = min(g.d_el, g.d_az * max(math.cos(math.radians(pk_el)), 0.05))
    limit = int(math.ceil(2.5 * max(finite) / cell_deg)) + 5 if finite else g.E * g.A
    M = _widest_flood(L, seed, g.shifts, _MAIN_LOBE_TOL_DB, limit)
    # intrinsic image of the main lobe (cone for linear, mirror for planar sets)
    rank, B, comp = ag.position_subspace(pos)
    M_img = np.zeros_like(M)
    if rank == 2:
        n_ = comp[:, 0]
        Um = g.U - 2.0 * (g.U @ n_)[..., None] * n_
        ii, jj = g.cells(Um.reshape(-1, 3))
        M_img = M[ii, jj].reshape(M.shape)
    elif rank == 1:
        v = g.U @ B[:, 0]
        vm = v[M]
        M_img = (v >= vm.min() - 1e-12) & (v <= vm.max() + 1e-12)
    main = M | M_img
    outside = ~main
    lattice = ag.detect_lattice(pos)
    lobes = []

    def refine(ii, jj):
        x = np.array([g.az[jj], g.el[ii]])
        r = optimize.minimize(lambda y: -P_ae(y), x, method="Nelder-Mead",
                              options={"xatol": 1e-4, "maxiter": 200,
                                       "initial_simplex": [x, x + [step / 2, 0], x + [0, step / 2]]})
        if -r.fun > Pg[ii, jj]:
            ci, cj = g.cell(direction_vector(r.x[0], np.clip(r.x[1], -90, 90)))
            if outside[ci, cj]:
                return float(ac.wrap_az(r.x[0])), float(np.clip(r.x[1], -90, 90)), _db10(-r.fun / p_pk)
        return float(g.az[jj]), float(g.el[ii]), float(L[ii, jj])

    # --- local re-grid of the near-in sidelobes when the sphere grid cannot resolve them
    cell_pk = max(g.d_el, g.d_az * max(math.cos(math.radians(pk_el)), 0.05))
    patch = None
    if finite and cell_pk > min(finite) / _PATCH_TRIGGER:
        patch = _LocalPatch(P, u_pk, e_az, e_el, hpbw_az, hpbw_el, p_pk, rank, B, comp)

    # --- PSLL: exact refinement seeded from every strong local maximum outside the main lobe
    cands = []                                              # (level_db, az, el)
    kind_hi = "grating" if lattice["periodic"] else "ambiguity"
    if patch is not None:
        cands.extend(patch.sidelobes())
    psll = FLOOR_DB
    if outside.any():
        flat = np.where(outside, L, -np.inf)
        nb = np.full(flat.shape, -np.inf)
        for v in g.shifts(flat):
            np.maximum(nb, v, out=nb)
        seeds = np.argwhere(outside & (flat >= nb) & (flat >= float(flat.max()) - _SEED_WINDOW_DB))
        if patch is not None and seeds.size:
            keep = ~patch.contains(g.U[seeds[:, 0], seeds[:, 1]])
            seeds = seeds[keep]
        if seeds.size:
            order = np.argsort(-flat[seeds[:, 0], seeds[:, 1]], kind="stable")[:_SEEDS_MAX]
            for ii, jj in seeds[order]:
                az_s, el_s, lv = refine(int(ii), int(jj))
                cands.append((lv, az_s, el_s))
        lab, nlab = g.label(outside & (L > -6.0))
        for k in range(1, nlab + 1):
            sel = np.where(lab == k, L, -np.inf)
            a, b = np.unravel_index(int(np.argmax(sel)), sel.shape)
            if patch is not None and patch.contains(g.U[a, b][np.newaxis, :])[0]:
                continue                                    # the patch is authoritative there
            az_k, el_k, lv = refine(a, b)
            lobes.append(Lobe(az_k, el_k, lv, kind_hi))
            cands.append((lv, az_k, el_k))
    if patch is not None:
        for lv, az_k, el_k in patch.sidelobes():
            if lv > -6.0:
                lobes.append(Lobe(az_k, el_k, lv, kind_hi))
    if cands:
        psll, az_s, el_s = max(cands)
        if psll <= -6.0:
            lobes.append(Lobe(az_s, el_s, psll, "sidelobe"))
    edge_tol = max(1.0, 2.0 * step)
    for lb in lobes:
        lb.edge = _edge_of_visible(direction_vector(lb.az_deg, lb.el_deg), rank, B, edge_tol)
    img_only = M_img & ~M
    if rank == 2 and img_only.any():
        lab, nlab = g.label(img_only & (L > -6.0))
        for k in range(1, nlab + 1):
            sel = np.where(lab == k, L, -np.inf)
            a, b = np.unravel_index(int(np.argmax(sel)), sel.shape)
            lobes.append(Lobe(float(g.az[b]), float(g.el[a]), float(L[a, b]), "ambiguity", intrinsic=True))
    elif rank == 1:
        lb = _cone_ridge_lobe(P, u_pk, p_pk, B[:, 0])
        if lb is not None:
            lobes.append(lb)

    side = float(np.sum((Pg * g.w)[outside]))
    mainp = float(np.sum((Pg * g.w)[main]))
    islr = _db10(side / mainp) if mainp > 0 and side > 0 else FLOOR_DB
    if not include_pattern or _all_isotropic(geom, which):
        direct = directivity_isotropic_dbi(pos, w, f_hz, u0)
    else:
        tot = float(np.sum(Pg * g.w))
        direct = _db10(4.0 * math.pi * float(P(u0)) / tot) if tot > 0 else FLOOR_DB
    a0 = steering_ff(pos, u0, f_hz)
    nd = [float(_db10(P(direction_vector(float(az), float(el))) / p_pk)) for az, el in nulls]
    proj = pos @ u0
    fill = float(bw_hz) * float(proj.max() - proj.min()) / C0 if proj.size else 0.0
    return BeamMetrics(
        peak_az_deg=pk_az, peak_el_deg=pk_el, pointing_error_deg=_angle_deg(u_pk, u0),
        hpbw_az_deg=hpbw_az, hpbw_el_deg=hpbw_el, psll_db=float(psll), islr_db=float(islr),
        directivity_dbi=float(direct), taper_eff=taper_efficiency(w, a0), null_depths_db=nd,
        cal_limited_null_floor_db=cal_limited_null_floor_db(cal_phase_rms_deg, cal_amp_rms_db, w),
        squint_ok=bool(fill <= 0.1), fill_time_bw_product=fill, lobes=lobes,
        grid_step_deg=float(max(g.d_el, g.d_az)), local_refine=patch is not None,
        look_level_db=float(_db10(P(u0) / p_pk)))


# ---------------------------------------------------------------------------
# Grating lobes and ambiguity
# ---------------------------------------------------------------------------
def _lift(v, B, comp, u_hint):
    """Unit vector u with B^T u = v on the same side of the complement as ``u_hint``."""
    v = np.asarray(v, dtype=float)
    t = math.sqrt(max(0.0, 1.0 - float(v @ v)))
    base = B @ v
    if comp.shape[1] == 0:
        return base / max(np.linalg.norm(base), 1e-300)
    if comp.shape[1] == 1:
        m = comp[:, 0] * (1.0 if float(u_hint @ comp[:, 0]) >= 0 else -1.0)
    else:
        perp = u_hint - B @ (B.T @ u_hint)
        m = perp / np.linalg.norm(perp) if np.linalg.norm(perp) > 1e-9 else comp[:, 0]
    u = base + t * m
    return u / np.linalg.norm(u)


def lattice_grating_lobes(d1, d2, f_hz, u0_t) -> list:
    """Visible grating lobes of a periodic lattice (spec 10.6), as ``Lobe`` (level 0 dB).

    ``d1`` (and ``d2`` for planar lattices) are lattice vectors (3,) in ENU
    metres; ``u0_t`` is the look direction (a unit vector, or its projection
    onto the lattice subspace). Lobes sit at ``u_t = u0_t + lambda (p g1 + q g2)``
    and are visible when ``|u_t| <= 1``.
    """
    lam = ac.wavelength(f_hz)
    d1 = np.asarray(d1, dtype=float)
    vecs = [d1] if d2 is None else [d1, np.asarray(d2, dtype=float)]
    Bm = np.linalg.qr(np.stack(vecs, axis=1))[0]                      # (3, r)
    full = np.linalg.qr(np.concatenate([Bm, np.eye(3)], axis=1))[0]
    comp = full[:, Bm.shape[1]:3]
    G = ag.reciprocal_vectors(d1, None if d2 is None else d2)       # (r, 3)
    u0 = np.asarray(u0_t, dtype=float).reshape(3)
    v0 = Bm.T @ u0
    hint = u0 if np.linalg.norm(u0) > 0 else Bm[:, 0]
    K = int(math.ceil(2.0 * max(np.linalg.norm(v) for v in vecs) / lam)) + 1
    out = []
    rng = range(-K, K + 1)
    pairs = [(p, 0) for p in rng] if d2 is None else [(p, q) for p in rng for q in rng]
    for p, q in pairs:
        if p == 0 and q == 0:
            continue
        gv = p * G[0] + (q * G[1] if G.shape[0] > 1 else 0.0)
        vt = v0 + lam * (Bm.T @ gv)
        if float(vt @ vt) <= 1.0 + 1e-9:
            u = _lift(vt, Bm, comp, hint)
            az, el = direction_angles(u)
            out.append(Lobe(az, el, 0.0, "grating"))
    return out


def max_scan_free_deg(geom, f_hz, which="rx"):
    """Grating-free scan half-angle around boresight for periodic lattices; None otherwise."""
    return ag.grating_free_scan_deg(geom, f_hz, which)


def _box_shifts(shape):
    """Neighbour views (all 3^r - 1 offsets, no wrap; outside = -inf) for an r-D grid."""
    r = len(shape)

    def gen(X):
        Pd = np.pad(X, 1, mode="constant", constant_values=-np.inf)
        for off in np.ndindex(*(3,) * r):
            if all(o == 1 for o in off):
                continue
            yield Pd[tuple(slice(o, o + n) for o, n in zip(off, shape))]
    return gen


class _Coarray:
    """Uniform-weight ambiguity function of a position set in its own subspace:
    ``A(D) = |mean_n exp(j k0 D . rho_n)|^2`` for direction-cosine differences D in
    [-2, 2]^r. A beam steered to v0 shows A(v - v0) at visible v (|v| <= 1)."""

    def __init__(self, P, f_hz):
        self.r, self.B, self.comp = ag.position_subspace(P)
        if self.r == 0:
            return
        self.rho = (P - P.mean(axis=0)) @ self.B
        self.k = ac.k0(f_hz)
        lam = ac.wavelength(f_hz)
        self.D = max(float(ag._pairwise(P).max()), 1e-9)
        h = min(0.05, 0.25 * lam / self.D)
        cap = {1: 200000, 2: 999, 3: 79}[self.r]
        n = min(int(math.ceil(2.0 / h)), cap)
        self.n = n
        self.axis = np.linspace(-2.0, 2.0, 2 * n + 1)
        self.hg = float(self.axis[1] - self.axis[0])
        mesh = np.stack(np.meshgrid(*([self.axis] * self.r), indexing="ij"), axis=-1).reshape(-1, self.r)
        A = self.exact(mesh)
        self.A = A.reshape((2 * n + 1,) * self.r)
        # main lobe of A around D = 0 (widest-path flood inside a box of ~3 lambda/D)
        half = min(n, int(math.ceil(3.0 * lam / (self.D * self.hg))) + 2)
        sl = tuple(slice(n - half, n + half + 1) for _ in range(self.r))
        sub = _db10(self.A[sl])
        seed = np.zeros(sub.shape, dtype=bool)
        seed[(half,) * self.r] = True
        ml = _widest_flood(sub, seed, _box_shifts(sub.shape), _MAIN_LOBE_TOL_DB, sub.size)
        self.ML = np.zeros(self.A.shape, dtype=bool)
        self.ML[sl] = ml

    def exact(self, Dm):
        Dm = np.asarray(Dm, dtype=float)
        flat = Dm.reshape(-1, self.r)
        out = np.empty(flat.shape[0])
        step = max(1, _CHUNK // self.rho.shape[0])
        for s in range(0, flat.shape[0], step):
            out[s:s + step] = np.abs(np.mean(np.exp(1j * self.k * (flat[s:s + step] @ self.rho.T)), axis=1)) ** 2
        return out.reshape(Dm.shape[:-1])

    def interp(self, Dm):
        Dm = np.asarray(Dm, dtype=float)
        coords = ((Dm.reshape(-1, self.r) + 2.0) / self.hg).T
        out = ndimage.map_coordinates(self.A, coords, order=1, mode="constant", cval=0.0)
        return out.reshape(Dm.shape[:-1])

    def in_main(self, Dm):
        Dm = np.asarray(Dm, dtype=float)
        idx = np.clip(np.rint((Dm.reshape(-1, self.r) + 2.0) / self.hg).astype(int), 0, 2 * self.n)
        inside = np.all(np.abs(Dm.reshape(-1, self.r)) <= 2.0, axis=1)
        return (self.ML[tuple(idx.T)] & inside).reshape(Dm.shape[:-1])

    def peaks(self, min_level_db, polish=32):
        """Interior local maxima of A outside the main lobe: [(D, level_db)].

        Vectorized parabolic refinement for every grid maximum; the ``polish``
        strongest are then refined exactly (Nelder-Mead)."""
        pk = ndimage.maximum_filter(self.A, size=3, mode="constant", cval=-1.0) == self.A
        pk &= self.A >= 10.0 ** ((min_level_db - 1.0) / 10.0)
        pk &= ~self.ML
        idx = np.argwhere(pk)
        if idx.size == 0:
            return []
        last = 2 * self.n
        x = self.axis[idx].astype(float)
        a0 = self.A[tuple(idx.T)]
        for d in range(self.r):
            lo = idx.copy()
            hi = idx.copy()
            lo[:, d] = np.maximum(idx[:, d] - 1, 0)
            hi[:, d] = np.minimum(idx[:, d] + 1, last)
            am, ap_ = self.A[tuple(lo.T)], self.A[tuple(hi.T)]
            den = am - 2.0 * a0 + ap_
            with np.errstate(divide="ignore", invalid="ignore"):
                dl = np.where(den < 0, 0.5 * (am - ap_) / den, 0.0)
            x[:, d] += np.clip(np.nan_to_num(dl), -0.5, 0.5) * self.hg
        lev = self.exact(x)
        better = lev >= a0
        x = np.where(better[:, None], x, self.axis[idx])
        lev = np.where(better, lev, a0)
        order = np.argsort(-lev)
        x, lev = x[order], lev[order]
        for k in range(min(int(polish), x.shape[0])):
            res = optimize.minimize(lambda y: -float(self.exact(y)), x[k], method="Nelder-Mead",
                                    options={"xatol": 1e-7, "fatol": 1e-12, "maxiter": 400})
            if -res.fun > lev[k]:
                x[k], lev[k] = res.x, -res.fun
        keys = set()
        out = []
        for xi, li in zip(x, lev):
            key = tuple(np.rint((xi + 2.0) / (0.5 * self.hg)).astype(int))
            if key in keys:
                continue
            keys.add(key)
            lv = _db10(li)
            if lv >= min_level_db:
                out.append((xi, lv))
        return out

    def edge_candidates(self, V0, min_level_db):
        """Maxima on the visible boundary |v| = 1 (endfire / in-plane horizon lobes whose
        peak lies outside visible space): [(v_boundary (r,), level_lin, scan_index)]."""
        thr = 10.0 ** (min_level_db / 10.0)
        out = []
        if self.r == 1:
            for sgn in (1.0, -1.0):
                Db = sgn - V0[:, :1]
                Ab = self.exact(Db)
                Ai = self.exact(Db - sgn * self.hg)
                ok = (Ab >= Ai) & (Ab >= thr) & ~self.in_main(Db) & (np.abs(Db[:, 0]) <= 2.0)
                out.extend((np.array([sgn]), float(Ab[i]), int(i)) for i in np.flatnonzero(ok))
        elif self.r == 2:
            nc = max(90, int(math.ceil(2.0 * math.pi / self.hg)))
            phi = np.linspace(0.0, 2.0 * math.pi, nc, endpoint=False)
            c = np.stack([np.cos(phi), np.sin(phi)], axis=1)
            for s in range(0, V0.shape[0], 512):
                V = V0[s:s + 512]
                Db = c[np.newaxis, :, :] - V[:, np.newaxis, :]
                Ab = self.interp(Db)
                Ai = self.interp(c[np.newaxis, :, :] * (1.0 - self.hg) - V[:, np.newaxis, :])
                loc = (Ab >= np.roll(Ab, 1, axis=1)) & (Ab >= np.roll(Ab, -1, axis=1))
                ok = loc & (Ab >= Ai) & (Ab >= 0.8 * thr) & ~self.in_main(Db)
                idx = np.argwhere(ok)
                if idx.size == 0:
                    continue
                lev = self.exact(Db[idx[:, 0], idx[:, 1]])
                for (a, q), lv in zip(idx, lev):
                    if lv >= thr:
                        out.append((c[q], float(lv), int(s + a)))
        return out


def _visible_lobes(geom, f_hz, scan_limit_deg, which, step_deg, min_level_db, include_edge=False):
    """[(az, el, level_db, scan_az, scan_el, edge)] of secondary lobes visible for some look
    in the scan cone: interior maxima of the co-array ambiguity function (edge False)
    and, with ``include_edge``, maxima on the visible boundary |v| = 1 (edge True: the
    peak of the lobe lies outside visible space; endfire / in-plane horizon). One entry
    per 5 deg direction bin (interior) or 10 deg bin (edge), the worst level kept."""
    P = geom.positions(which)
    if P.shape[0] < 2:
        return []
    co = _Coarray(P, f_hz)
    if co.r == 0:
        return []
    U0 = ag.scan_directions(geom.boresight_enu(), scan_limit_deg, step_deg)
    lam = ac.wavelength(f_hz)
    B, comp, r = co.B, co.comp, co.r
    found = {}

    def add(u_l, level, i, edge=False):
        az, el = direction_angles(u_l)
        b = 10.0 if edge else 5.0
        key = (edge, int(round(az / b)) % int(360 / b), int(round(el / b)))
        if key not in found or level > found[key][2]:
            saz, sel = direction_angles(U0[i])
            found[key] = (az, el, float(level), saz, sel, edge)

    for dlt, lvl in co.peaks(min_level_db):
        if r < 3:
            V0 = U0 @ B
            vis = np.linalg.norm(V0 + dlt, axis=1) <= 1.0 + 1e-9
            if vis.any():
                i = int(np.argmax(vis))
                add(_lift(V0[i] + dlt, B, comp, U0[i]), lvl, i)
        else:
            W = U0 + dlt @ B.T
            nrm = np.linalg.norm(W, axis=1)
            cand = np.abs(nrm - 1.0) <= 0.443 * lam / co.D
            if cand.any():
                Us = W[cand] / nrm[cand, None]
                levels = co.exact((Us - U0[cand]) @ B)
                m = int(np.argmax(levels))
                level = _db10(levels[m])
                if level >= min_level_db:
                    add(Us[m], level, int(np.flatnonzero(cand)[m]))
    if include_edge and r < 3:
        for vb, lv, i in co.edge_candidates(U0 @ B, min_level_db):
            add(_lift(vb, B, comp, U0[i]), _db10(lv), i, True)
    return list(found.values())


def numeric_ambiguity_search(geom, f_hz, scan_limit_deg, which="rx", step_deg=2.0, threshold_db=-3.0) -> list:
    """Secondary lobes of uniform-weight beams steered across the scan cone (spec 10.6).

    Geometry agnostic: works in the direction-cosine difference space of the
    position subspace, so the intrinsic cone / mirror ambiguity of linear /
    planar sets is not reported. Returns ``Lobe`` objects at or above
    ``threshold_db - 3``: kind ``grating`` (periodic lattice) or ``ambiguity``
    when >= ``threshold_db``, ``sidelobe`` between ``threshold_db - 3`` and
    ``threshold_db``. ``scan_az/el_deg`` give the look that exposes the lobe.
    """
    lattice = ag.detect_lattice(geom.positions(which))
    hi = "grating" if lattice["periodic"] else "ambiguity"
    out = []
    for az, el, lv, saz, sel, _edge in _visible_lobes(geom, f_hz, scan_limit_deg, which, step_deg,
                                                      threshold_db - 3.0):
        if lv >= threshold_db - 3.0:
            out.append(Lobe(az, el, lv, hi if lv >= threshold_db else "sidelobe", saz, sel))
    out.sort(key=lambda lb: -lb.level_db)
    return out


@dataclass
class GeometryReport:
    which: str
    n: int
    rank: int
    ambiguity: str
    lattice: str
    min_spacing_m: float
    max_baseline_m: float
    fraunhofer_m: float
    grating_free_scan_deg: float     # None for non-periodic sets
    lobes: list
    worst_psll_db: float
    position_tolerance_1deg_m: float
    issues: list

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lobes"] = [lb.to_dict() for lb in self.lobes]
        d["issues"] = ac.issues_to_json(self.issues)
        return d


def _tx_lobe_with_patterns_db(geom, f_hz, saz, sel, which):
    """Strongest non-intrinsic grating / ambiguity lobe (dB) of the uniform beam steered to
    (saz, sel) with the element patterns: what ``solve_beam`` measures for that TX look."""
    u = direction_vector(saz, sel)
    m = beam_metrics(geom, array_manifold(geom, u, f_hz, which, True), f_hz, u, which=which)
    return max((lb.level_db for lb in m.lobes if lb.kind in ("grating", "ambiguity") and not lb.intrinsic),
               default=FLOOR_DB)


def geometry_report(geom, f_min_hz, f_max_hz, scan_limit_deg, which="rx", step_deg=2.0, look=None,
                    beacon_range_m=None) -> GeometryReport:
    """Analytic + numeric geometry validation for one element set (spec 10.6).

    Issues: ``validate_geometry`` (GEOMETRY / GRATING_LOBE) plus the numeric
    search at f_max (AMBIGUITY error >= -3 dB, warning -3..-6 dB) and an info
    line with the beamwidth at f_min. Partial lobes at the edge of visible space
    (endfire / in-plane horizon) >= -3 dB are warnings for the RX set; for the TX
    set they are AMBIGUITY errors, because ``solve_beam`` refuses such a TX beam
    (``TX_EDGE_LOBE_DB``). With directional elements the strongest TX edge lobes
    (up to ``_EDGE_TX_EVAL``) are re-measured with the element patterns, as
    ``solve_beam`` does, and only those still >= -3 dB are errors.
    """
    f_min, f_max = ag._freqs(f_min_hz, f_max_hz)
    els = geom.elements_for(which)
    lam_min = C0 / f_max
    if len(els) == 0:
        return GeometryReport(which, 0, 0, "degenerate", "irregular", 0.0, 0.0, 0.0, None, [], FLOOR_DB,
                              lam_min / 360.0, [])
    issues = list(ag.validate_geometry(geom, f_min, f_max, scan_limit_deg, which, look=look,
                                       beacon_range_m=beacon_range_m))
    where = "geometry[%s]" % which
    lat = geom.lattice(which)
    lobes, worst = [], FLOOR_DB
    if len(els) >= 2 and geom.rank(which) > 0:
        allv = _visible_lobes(geom, f_max, scan_limit_deg, which, step_deg, -40.0, include_edge=True)
        worst = max((t[2] for t in allv), default=FLOOR_DB)
        hi = "grating" if lat["periodic"] else "ambiguity"
        edge_warn, edge_err = [], []
        iso = _all_isotropic(geom, which)
        n_eval = 0
        for az, el, lv, saz, sel, edge in sorted(allv, key=lambda t: -t[2]):
            if edge:
                if lv >= -3.0:
                    if which == "tx" and lv >= TX_EDGE_LOBE_DB:
                        lvp = lv
                        if not iso:
                            if n_eval >= _EDGE_TX_EVAL:
                                lvp = FLOOR_DB              # the strongest ones were checked: warn only
                            else:
                                n_eval += 1
                                lvp = _tx_lobe_with_patterns_db(geom, f_max, saz, sel, which)
                        if lvp >= TX_EDGE_LOBE_DB:
                            edge_err.append((az, el, lv, saz, sel, lvp))
                            continue
                    edge_warn.append((az, el, lv, saz, sel))
                continue
            if lv >= -6.0:
                kind = hi if lv >= -3.0 else "sidelobe"
                lobes.append(Lobe(az, el, lv, kind, saz, sel))
                level = "error" if lv >= -3.0 else "warning"
                issues.append(Issue(level, "AMBIGUITY", "%s lobe %.1f dB at az %.1f el %.1f when steered to "
                                    "az %.1f el %.1f (%.6g Hz)" % (kind, lv, az, el, saz, sel, f_max), where))
        if len(edge_err) > _EDGE_WARN_MAX:
            issues.append(Issue("error", "AMBIGUITY", "TX set: %d more partial lobes >= %.0f dB at the edge of "
                                "visible space" % (len(edge_err) - _EDGE_WARN_MAX, TX_EDGE_LOBE_DB), where))
        for az, el, lv, saz, sel, lvp in edge_err[:_EDGE_WARN_MAX]:
            lobes.append(Lobe(az, el, lv, hi, saz, sel, edge=True))
            issues.append(Issue("error", "AMBIGUITY", "TX set: partial %s lobe %.1f dB at the edge of visible "
                                "space (az %.1f el %.1f, endfire / in-plane horizon) when steered to az %.1f el "
                                "%.1f (%.6g Hz)%s: solve_beam refuses such TX beams (>= %.0f dB); reduce "
                                "scan_limit_deg or use directional elements"
                                % (hi, lv, az, el, saz, sel, f_max,
                                   "" if iso else ", %.1f dB with the element patterns" % lvp, TX_EDGE_LOBE_DB),
                                where))
        for az, el, lv, saz, sel in edge_warn[:_EDGE_WARN_MAX]:
            lobes.append(Lobe(az, el, lv, "sidelobe", saz, sel, edge=True))
            issues.append(Issue("warning", "AMBIGUITY", "partial lobe %.1f dB at the edge of visible space "
                                "(az %.1f el %.1f, endfire / in-plane horizon) when steered to az %.1f el %.1f "
                                "(%.6g Hz); isotropic array factor: element patterns or a smaller scan_limit_deg "
                                "reduce it" % (lv, az, el, saz, sel, f_max), where))
        if len(edge_warn) > _EDGE_WARN_MAX:
            issues.append(Issue("warning", "AMBIGUITY", "%d more edge-of-visible-space lobes >= -3 dB"
                                % (len(edge_warn) - _EDGE_WARN_MAX), where))
        D = geom.aperture_m(which)
        if D > 0:
            issues.append(Issue("info", "GEOMETRY", "beamwidth ~ %.1f deg at f_min %.6g Hz (lambda/D)"
                                % (math.degrees(min(math.pi, (C0 / f_min) / D)), f_min), where))
    return GeometryReport(
        which=which, n=len(els), rank=geom.rank(which), ambiguity=geom.ambiguity(which), lattice=lat["kind"],
        min_spacing_m=geom.min_spacing_m(which), max_baseline_m=geom.max_baseline_m(which),
        fraunhofer_m=geom.fraunhofer_m(f_max, which), grating_free_scan_deg=ag.grating_free_scan_deg(geom, f_max, which),
        lobes=lobes, worst_psll_db=float(worst), position_tolerance_1deg_m=lam_min / 360.0, issues=issues)
