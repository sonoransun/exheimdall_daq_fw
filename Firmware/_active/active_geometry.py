"""
    HeIMDALL "active" capability: array geometry.

    Element model, coordinate frames, presets, geometry files and the
    analytic (closed-form) geometry checks.

    Frozen conventions (spec section 2):
      - world frame: local ENU in metres (x = East, y = North, z = Up); the
        origin is the array phase reference (element 0 or the centroid);
      - body frame: x = right, y = forward (boresight), z = up;
        ``r_enu = R_body . r_body`` with ``R_body = Rz(-heading) Rx(pitch) Ry(roll)``
        (heading clockwise from North, pitch nose-up +, roll right-side-down +);
      - az/el: compass azimuth (deg, clockwise from North, [0, 360)), elevation
        above the horizon; ``u(az, el) = [cos el sin az, cos el cos az, sin el]``
        points FROM the array TOWARD the source / target.

    The scan region of every check is the cone of half-angle ``scan_limit_deg``
    around the platform boresight (body +y in ENU). Planar presets (URA,
    triangular) lie in the body x-z plane, i.e. a panel facing forward; the UCA
    lies in the horizontal body x-y plane. JSON presets may choose
    ``"plane": "horizontal"`` for URA/triangular arrays.

    Frames: in the ``body`` frame the platform attitude rotates the element
    positions and directions; ``enu`` / ``lla`` positions are already ENU and the
    attitude orients only the boresight and the element patterns.

    Geometry files: canonical JSON (spec 6.1) and a convenience CSV (6.2; the
    header line also records ``frame``). ``units = lambda`` needs the file's own
    ``design_freq_hz``: physical positions never follow the configured operating
    frequency. A geometry carries the TX/RX ``scan_limit_deg`` of its config and
    any ``provenance_issues`` (e.g. a preset pitch derived from center_freq_hz),
    which every ``validate_geometry`` reports.

    No hardware access; stdlib + numpy only.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import csv
import io
import itertools
import json
import logging
import math
import os
from dataclasses import dataclass, replace

import numpy as np

import active_common as ac
from active_common import C0, ChannelRef, GeometryError, Issue

logger = logging.getLogger("heimdall.active.geometry")

GEOMETRY_SCHEMA = "heimdall-active-geometry/1"
PATTERN_KINDS = ("isotropic", "cos_q", "dipole_v", "dipole_h", "table")
TABLE_EXTEND = ("taper", "constant")
PHASE_REFERENCES = ("element0", "centroid")
FRAMES = ("body", "enu", "lla")
UNITS = ("m", "mm", "lambda")
PRESET_TYPES = ("ula", "ura", "uca", "tri")
PLANES = ("vertical", "horizontal")
WHICH = ("rx", "tx", "all")
LATTICE_KINDS = ("ula", "ura", "triangular", "uca", "irregular")
AMBIGUITY_LABELS = ("cone", "mirror_elevation", "none", "degenerate")
PLANE_KINDS = ("horizontal", "vertical", "tilted")
MIN_SEPARATION_M = 1e-4
DIPOLE_GAIN_DBI = 2.15
CSV_COLUMNS = ("id", "x_m", "y_m", "z_m", "rx_device", "rx_channel", "tx_device", "tx_channel",
               "enabled", "label", "pattern", "pattern_q", "gain_dbi", "pos_sigma_m")
_FREE_TEXT_KEYS = ("comment", "note", "notes", "description")


def _geom_error(code: str, msg: str, issues=()) -> GeometryError:
    """GeometryError whose message starts with a stable ``code`` (also ``.code``)."""
    e = GeometryError("%s: %s" % (code, msg))
    e.code = code
    e.issues = tuple(issues) if issues else (Issue("error", code, msg),)
    return e


def _finite(x, what) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise _geom_error("GEOMETRY", "%s: not a number: %r" % (what, x)) from None
    if not math.isfinite(v):
        raise _geom_error("GEOMETRY", "%s must be finite, got %r" % (what, x))
    return v


def _vec3(v, what) -> tuple:
    if not isinstance(v, (list, tuple, np.ndarray)) or len(v) != 3:
        raise _geom_error("GEOMETRY", "%s must be a list of 3 numbers, got %r" % (what, v))
    return tuple(_finite(c, what) for c in v)


# ---------------------------------------------------------------------------
# Element model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ElementPattern:
    """Element radiation pattern (body frame; boresight 0/0 = body +y).

    ``gain_dbi = None`` derives the peak gain from the shape: cos^q power
    pattern -> 10 log10(2(q+1)), dipole -> 2.15 dBi, isotropic -> 0 dBi,
    table -> the table values as given (absolute dBi).
    A ``table`` is ``((az_deg, el_deg, gain_db, phase_deg), ...)`` in body-frame
    az/el; it must be a full az x el grid or a single cut (one el or one az).
    Gain is interpolated bilinearly in dB and phase as a unit phasor (no error
    across +-180 deg). Outside the tabulated coverage (``table_extend``):

      - ``taper`` (default): an azimuth span with a gap > 90 deg falls linearly
        (dB) to ``table_floor_db`` within 10 deg of its edges; elevations outside
        a grid's span do the same toward the poles; a single elevation cut tapers
        linearly to the floor at both poles and a single azimuth cut tapers to
        the floor at the back (az0 + 180);
      - ``constant``: a cut applies unchanged at every other angle, azimuth is
        periodic across any gap and elevation is clamped (explicit opt-in for
        elevation / azimuth independent elements).

    ``table_floor_db`` (absolute, dBi before ``gain_dbi`` rescaling) defaults to
    the lowest tabulated gain minus 30 dB. A one-row table is constant.
    """
    kind: str = "isotropic"
    q: float = 1.0
    gain_dbi: float = None
    boresight_az_deg: float = 0.0
    boresight_el_deg: float = 0.0
    table: tuple = ()
    table_floor_db: float = None
    table_extend: str = "taper"

    def __post_init__(self):
        if self.kind not in PATTERN_KINDS:
            raise _geom_error("GEOMETRY", "pattern kind %r not in %s" % (self.kind, ", ".join(PATTERN_KINDS)))
        if self.kind == "cos_q" and not (math.isfinite(float(self.q)) and float(self.q) > 0):
            raise _geom_error("GEOMETRY", "cos_q pattern needs q > 0, got %r" % (self.q,))
        if self.gain_dbi is not None and not math.isfinite(float(self.gain_dbi)):
            raise _geom_error("GEOMETRY", "pattern gain_dbi must be finite")
        if self.table_extend not in TABLE_EXTEND:
            raise _geom_error("GEOMETRY", "table_extend %r not in %s" % (self.table_extend, TABLE_EXTEND))
        if self.table_floor_db is not None:
            try:
                fl = float(self.table_floor_db)
            except (TypeError, ValueError):
                raise _geom_error("GEOMETRY", "table_floor_db must be a number") from None
            if not math.isfinite(fl):
                raise _geom_error("GEOMETRY", "table_floor_db must be finite")
            object.__setattr__(self, "table_floor_db", fl)
        if self.kind == "table":
            rows = tuple(tuple(float(c) for c in r) for r in self.table)
            if not rows:
                raise _geom_error("GEOMETRY", "table pattern with no rows")
            for r in rows:
                if len(r) != 4 or not all(math.isfinite(c) for c in r):
                    raise _geom_error("GEOMETRY", "table rows are [az_deg, el_deg, gain_db, phase_deg]: %r" % (r,))
                if not -90.0 <= r[1] <= 90.0:
                    raise _geom_error("GEOMETRY", "table elevation %g outside [-90, 90]" % r[1])
            azs = sorted({ac.wrap_az(r[0]) for r in rows})
            els = sorted({r[1] for r in rows})
            keys = {(ac.wrap_az(r[0]), r[1]) for r in rows}
            if len(keys) != len(rows):
                raise _geom_error("GEOMETRY", "table pattern has duplicate (az, el) rows")
            if len(azs) > 1 and len(els) > 1 and len(keys) != len(azs) * len(els):
                raise _geom_error("GEOMETRY", "table pattern must be a full az x el grid or a single cut "
                                              "(%d rows for %d az x %d el)" % (len(rows), len(azs), len(els)))
            object.__setattr__(self, "table", rows)
        elif self.table:
            raise _geom_error("GEOMETRY", "table given for a %r pattern" % self.kind)

    def derived_gain_dbi(self) -> float:
        if self.kind == "cos_q":
            return 10.0 * math.log10(2.0 * (float(self.q) + 1.0))
        if self.kind in ("dipole_v", "dipole_h"):
            return DIPOLE_GAIN_DBI
        if self.kind == "table":
            return max(r[2] for r in self.table)
        return 0.0

    def effective_gain_dbi(self) -> float:
        return self.derived_gain_dbi() if self.gain_dbi is None else float(self.gain_dbi)

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "q": float(self.q), "gain_dbi": self.gain_dbi,
             "boresight_az_deg": float(self.boresight_az_deg),
             "boresight_el_deg": float(self.boresight_el_deg)}
        if self.kind == "table":
            d["table"] = [list(r) for r in self.table]
            d["table_floor_db"] = self.table_floor_db
            d["table_extend"] = self.table_extend
        return d


@dataclass(frozen=True)
class Apriori:
    """Known chain error (e.g. a VNA cable survey): delay (positive = late), phase, gain."""
    delay_s: float = 0.0
    phase_deg: float = 0.0
    gain_db: float = 0.0

    def to_dict(self) -> dict:
        return {"delay_s": float(self.delay_s), "phase_deg": float(self.phase_deg),
                "gain_db": float(self.gain_db)}


@dataclass(frozen=True)
class Element:
    id: int
    pos_body_m: tuple
    rx: ChannelRef = None
    tx: ChannelRef = None
    pattern: ElementPattern = ElementPattern()
    enabled: bool = True
    label: str = ""
    pos_sigma_m: float = 0.0
    polarization: str = "vertical"
    apriori_rx: Apriori = None
    apriori_tx: Apriori = None

    def __post_init__(self):
        if isinstance(self.id, bool) or not isinstance(self.id, (int, np.integer)) or int(self.id) < 0:
            raise _geom_error("GEOMETRY", "element id must be an int >= 0, got %r" % (self.id,))
        object.__setattr__(self, "id", int(self.id))
        object.__setattr__(self, "pos_body_m", _vec3(self.pos_body_m, "element %s position" % self.id))
        sig = _finite(self.pos_sigma_m, "element %s pos_sigma_m" % self.id)
        if sig < 0:
            raise _geom_error("GEOMETRY", "element %s pos_sigma_m must be >= 0" % self.id)
        for ref in (self.rx, self.tx):
            if ref is not None and not isinstance(ref, ChannelRef):
                raise _geom_error("GEOM_MAP", "element %s channel refs must be ChannelRef" % self.id)


@dataclass(frozen=True)
class Orientation:
    """Platform attitude: body -> ENU rotation."""
    heading_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0

    def matrix(self) -> np.ndarray:
        """3x3 body->ENU rotation ``Rz(-heading) . Rx(pitch) . Ry(roll)``."""
        h = math.radians(-float(self.heading_deg))
        p = math.radians(float(self.pitch_deg))
        r = math.radians(float(self.roll_deg))
        ch, sh = math.cos(h), math.sin(h)
        cp, sp = math.cos(p), math.sin(p)
        cr, sr = math.cos(r), math.sin(r)
        rz = np.array([[ch, -sh, 0.0], [sh, ch, 0.0], [0.0, 0.0, 1.0]])
        rx = np.array([[1.0, 0.0, 0.0], [0.0, cp, -sp], [0.0, sp, cp]])
        ry = np.array([[cr, 0.0, sr], [0.0, 1.0, 0.0], [-sr, 0.0, cr]])
        return rz @ rx @ ry

    def to_dict(self) -> dict:
        return {"heading_deg": float(self.heading_deg), "pitch_deg": float(self.pitch_deg),
                "roll_deg": float(self.roll_deg)}


# ---------------------------------------------------------------------------
# Directions
# ---------------------------------------------------------------------------
def direction_vector(az_deg, el_deg) -> np.ndarray:
    """Unit vector(s) from the array toward (az, el); broadcasts to (..., 3)."""
    az = np.radians(np.asarray(az_deg, dtype=float))
    el = np.radians(np.asarray(el_deg, dtype=float))
    az, el = np.broadcast_arrays(az, el)
    ce = np.cos(el)
    return np.stack([ce * np.sin(az), ce * np.cos(az), np.sin(el)], axis=-1)


def direction_angles(u) -> tuple:
    """Inverse of ``direction_vector``: (az_deg in [0, 360), el_deg). Scalars for (3,)."""
    u = np.asarray(u, dtype=float)
    if u.shape[-1] != 3:
        raise ValueError("direction vectors must have a last axis of 3")
    x, y, z = u[..., 0], u[..., 1], u[..., 2]
    az = ac.wrap_az(np.degrees(np.arctan2(x, y)))
    el = np.degrees(np.arctan2(z, np.hypot(x, y)))
    if u.ndim == 1:
        return float(az), float(el)
    return az, el


def compass_to_math_deg(az_deg) -> float:
    """Compass azimuth (clockwise from North) -> math angle (counter-clockwise from East)."""
    if np.ndim(az_deg):
        return ac.wrap_az(90.0 - np.asarray(az_deg, dtype=float))
    return ac.wrap_az(90.0 - float(az_deg))


_WGS84_A = 6378137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)


def _lla_to_ecef(lat_deg, lon_deg, alt_m):
    lat = np.radians(np.asarray(lat_deg, dtype=float))
    lon = np.radians(np.asarray(lon_deg, dtype=float))
    h = np.asarray(alt_m, dtype=float)
    sl = np.sin(lat)
    n = _WGS84_A / np.sqrt(1.0 - _WGS84_E2 * sl * sl)
    x = (n + h) * np.cos(lat) * np.cos(lon)
    y = (n + h) * np.cos(lat) * np.sin(lon)
    z = (n * (1.0 - _WGS84_E2) + h) * sl
    return np.stack(np.broadcast_arrays(x, y, z), axis=-1)


def lla_to_enu(lat_deg, lon_deg, alt_m, lat0_deg, lon0_deg, alt0_m) -> np.ndarray:
    """WGS-84 geodetic -> local ENU (metres) about (lat0, lon0, alt0); broadcasts to (..., 3)."""
    p = _lla_to_ecef(lat_deg, lon_deg, alt_m)
    p0 = _lla_to_ecef(lat0_deg, lon0_deg, alt0_m)
    lat0 = math.radians(float(lat0_deg))
    lon0 = math.radians(float(lon0_deg))
    sl, cl = math.sin(lat0), math.cos(lat0)
    so, co = math.sin(lon0), math.cos(lon0)
    rot = np.array([[-so, co, 0.0],
                    [-sl * co, -sl * so, cl],
                    [cl * co, cl * so, sl]])
    return (p - p0) @ rot.T


# ---------------------------------------------------------------------------
# Subspace / lattice helpers (shared with active_pattern)
# ---------------------------------------------------------------------------
def _pairwise(P: np.ndarray) -> np.ndarray:
    d = P[:, None, :] - P[None, :, :]
    return np.sqrt(np.sum(d * d, axis=-1))


def position_subspace(P, rtol: float = 0.01) -> tuple:
    """(rank, B (3, r) orthonormal basis, complement (3, 3-r)) of centred positions.

    Singular values below ``rtol * max_baseline`` count as zero, so a surveyed
    planar array with millimetre height scatter stays planar.
    """
    P = np.asarray(P, dtype=float).reshape(-1, 3)
    if P.shape[0] < 2:
        return 0, np.zeros((3, 0)), np.eye(3)
    Q = P - P.mean(axis=0)
    scale = float(_pairwise(P).max())
    if scale <= 0.0:
        return 0, np.zeros((3, 0)), np.eye(3)
    _, s, vt = np.linalg.svd(Q, full_matrices=True)
    s = np.concatenate([s, np.zeros(3 - s.size)])
    # singular value ~ sqrt(N) * rms extent; compare the rms extent with the baseline
    r = int(np.sum(s / math.sqrt(P.shape[0]) > float(rtol) * scale))
    return r, vt[:r].T.copy(), vt[r:].T.copy()


def reciprocal_vectors(d1, d2=None) -> np.ndarray:
    """Reciprocal lattice vectors g_i with g_i . d_j = delta_ij (no 2 pi), as rows (r, 3)."""
    d1 = np.asarray(d1, dtype=float)
    if d2 is None:
        n2 = float(d1 @ d1)
        if n2 <= 0:
            raise ValueError("zero lattice vector")
        return (d1 / n2)[np.newaxis, :]
    D = np.stack([d1, np.asarray(d2, dtype=float)], axis=1)       # (3, 2)
    G = D @ np.linalg.inv(D.T @ D)                                 # (3, 2), G^T D = I
    return G.T


def min_reciprocal_norm(d1, d2=None) -> float:
    """min_{(p,q) != 0} |p g1 + q g2| (1/m)."""
    g = reciprocal_vectors(d1, d2)
    if g.shape[0] == 1:
        return float(np.linalg.norm(g[0]))
    best = math.inf
    for p, q in itertools.product(range(-4, 5), repeat=2):
        if p == 0 and q == 0:
            continue
        best = min(best, float(np.linalg.norm(p * g[0] + q * g[1])))
    return best


def detect_lattice(pos, rtol=0.01) -> dict:
    """Classify element positions (N, 3).

    Returns ``{"kind": ula|ura|triangular|uca|irregular, "d1", "d2", "spacing",
    "radius", "periodic", "indices", "complete", "rank"}``. ``periodic`` is True
    for any lattice (an oblique lattice is reported as ``irregular`` with
    ``periodic`` True and its ``d1``/``d2``), ``indices`` are integer lattice
    coordinates (N, 2) and ``complete`` says the lattice is a full line /
    rectangle with no missing sites. Tolerance ``rtol`` is relative to the
    spacing (1 % by default).
    """
    P = np.asarray(pos, dtype=float).reshape(-1, 3)
    n = P.shape[0]
    out = {"kind": "irregular", "d1": None, "d2": None, "spacing": None, "radius": None,
           "periodic": False, "indices": None, "complete": False, "rank": 0}
    if n < 2:
        return out
    r, B, comp = position_subspace(P, rtol)
    out["rank"] = r
    if r == 1:
        axis = B[:, 0]
        t = (P - P[0]) @ axis
        ts = np.sort(t)
        diffs = np.diff(ts)
        d = float(diffs[diffs > 0].min()) if np.any(diffs > 0) else 0.0
        if d > 0:
            t0 = ts[0]
            for _ in range(3):                       # least-squares refit of origin and spacing
                ii = np.round((t - t0) / d)
                A = np.stack([np.ones_like(ii), ii], axis=1)
                (t0, d), *_ = np.linalg.lstsq(A, t, rcond=None)
                if d <= 0:
                    break
            idx = (t - t0) / d if d > 0 else np.full_like(t, 0.5)
            if d > 0 and np.all(np.abs(idx - np.round(idx)) <= rtol):
                ii = np.round(idx).astype(int)
                ii -= ii.min()
                out.update(kind="ula", d1=axis * d, spacing=d, periodic=True,
                           indices=np.stack([ii, np.zeros_like(ii)], axis=1),
                           complete=len(set(ii.tolist())) == n and int(ii.max()) == n - 1)
        return out
    if r != 2:
        return out
    q = (P - P.mean(axis=0)) @ B                                   # (N, 2) plane coordinates
    if n >= 5 and _is_uca(q, rtol, out):
        out["d1"] = None
        return out
    if _plane_lattice(q, B, rtol, out):
        return out
    if n >= 3:
        _is_uca(q, rtol, out)
    return out


def _is_uca(q, rtol, out) -> bool:
    n = q.shape[0]
    if n < 3:
        return False
    rad = np.hypot(q[:, 0], q[:, 1])
    R = float(rad.mean())
    if R <= 0 or np.any(np.abs(rad - R) > rtol * R):
        return False
    ang = np.sort(np.mod(np.arctan2(q[:, 1], q[:, 0]), 2 * np.pi))
    steps = np.diff(np.concatenate([ang, [ang[0] + 2 * np.pi]]))
    step = 2 * np.pi / n
    if np.any(np.abs(steps - step) > rtol * step):
        return False
    out.update(kind="uca", radius=R, spacing=2.0 * R * math.sin(math.pi / n), periodic=False,
               indices=None, complete=True)
    return True


def _plane_lattice(q, B, rtol, out) -> bool:
    n = q.shape[0]
    diffs = (q[:, None, :] - q[None, :, :]).reshape(-1, 2)
    norms = np.hypot(diffs[:, 0], diffs[:, 1])
    ok = norms > 0
    diffs, norms = diffs[ok], norms[ok]
    order = np.argsort(norms, kind="stable")
    b1 = diffs[order[0]]
    b2 = None
    for i in order[1:]:
        v = diffs[i]
        s = abs(b1[0] * v[1] - b1[1] * v[0]) / (np.hypot(*b1) * np.hypot(*v))
        if s > 0.2:
            b2 = v
            break
    if b2 is None:
        return False
    # Lagrange-Gauss reduction
    for _ in range(64):
        if np.hypot(*b2) < np.hypot(*b1):
            b1, b2 = b2, b1
        mu = round(float(b1 @ b2) / float(b1 @ b1))
        if mu == 0:
            break
        b2 = b2 - mu * b1
    M = np.stack([b1, b2], axis=1)
    q0 = q[0].copy()
    for _ in range(3):                                              # least-squares basis refit
        c = np.round(np.linalg.solve(M, (q - q0).T).T)
        A = np.concatenate([np.ones((n, 1)), c], axis=1)            # q = q0 + c1 b1 + c2 b2
        sol, *_ = np.linalg.lstsq(A, q, rcond=None)
        if abs(np.linalg.det(sol[1:].T)) <= 1e-12 * float(b1 @ b1):
            break
        q0, M = sol[0], sol[1:].T
    b1, b2 = M[:, 0], M[:, 1]
    c = np.linalg.solve(M, (q - q0).T).T                            # (N, 2)
    resid = (c - np.round(c)) @ M.T
    if np.any(np.hypot(resid[:, 0], resid[:, 1]) > rtol * np.hypot(*b1)):
        return False
    ii = np.round(c).astype(int)
    ii -= ii.min(axis=0)
    l1, l2 = float(np.hypot(*b1)), float(np.hypot(*b2))
    cosang = abs(float(b1 @ b2)) / (l1 * l2)
    d1 = B @ b1
    d2 = B @ b2
    out.update(d1=d1, d2=d2, periodic=True, indices=ii, spacing=l1)
    uniq = len({tuple(x) for x in ii.tolist()})
    if cosang <= 2.0 * rtol:
        ext = ii.max(axis=0) + 1
        out.update(kind="ura", complete=(uniq == n and int(ext[0] * ext[1]) == n))
    elif abs(l2 / l1 - 1.0) <= rtol and abs(cosang - 0.5) <= 2.0 * rtol:
        out.update(kind="triangular", complete=False)
    else:
        out.update(kind="irregular", complete=False)
    return True


# ---------------------------------------------------------------------------
# ArrayGeometry
# ---------------------------------------------------------------------------
def _ref_key(order, ref):
    try:
        return (0, order.index(ref))
    except ValueError:
        return (1, 0)


class ArrayGeometry:
    """An immutable set of elements on a platform.

    ``rx_order`` / ``tx_order`` (lists of ChannelRef, normally ``cfg.rx_refs()``
    / ``cfg.tx_refs()``) define the global channel order used by
    ``elements_for``; without them elements are ordered by id.
    """

    def __init__(self, elements, orientation=Orientation(), phase_reference="element0", name="",
                 design_freq_hz=0.0, site_lla=None, element_size_m=0.0, survey_sigma_m=0.0,
                 frame="body", rx_order=None, tx_order=None, scan_limit_deg=None,
                 provenance_issues=(), preset_spacing_m=None):
        els = tuple(elements)
        if not els:
            raise _geom_error("GEOMETRY", "no elements")
        for e in els:
            if not isinstance(e, Element):
                raise _geom_error("GEOMETRY", "elements must be Element instances")
        ids = [e.id for e in els]
        if len(set(ids)) != len(ids):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise _geom_error("GEOMETRY", "duplicate element id(s) %s" % dup)
        if phase_reference not in PHASE_REFERENCES:
            raise _geom_error("GEOMETRY", "phase_reference %r not in %s" % (phase_reference, PHASE_REFERENCES))
        if frame not in ("body", "enu"):
            raise _geom_error("GEOMETRY", "frame must be body or enu internally, got %r" % (frame,))
        if not isinstance(orientation, Orientation):
            raise _geom_error("GEOMETRY", "orientation must be an Orientation")
        if scan_limit_deg is not None:
            scan_limit_deg = _finite(scan_limit_deg, "scan_limit_deg")
            if not 0.0 <= scan_limit_deg <= 90.0:
                raise _geom_error("GEOMETRY", "scan_limit_deg must be within [0, 90], got %g" % scan_limit_deg)
        for it in provenance_issues:
            if not isinstance(it, Issue):
                raise _geom_error("GEOMETRY", "provenance_issues must be Issue instances")
        els = tuple(sorted(els, key=lambda e: e.id))
        for which in ("rx", "tx"):
            refs = [getattr(e, which) for e in els if getattr(e, which) is not None]
            if len(set(refs)) != len(refs):
                dup = sorted({str(r) for r in refs if refs.count(r) > 1})
                raise _geom_error("GEOM_MAP", "duplicate %s channel ref(s) %s" % (which, ", ".join(dup)))
        enabled = [e for e in els if e.enabled]
        if len(enabled) < 2:
            raise _geom_error("GEOMETRY", "need at least 2 enabled elements, got %d" % len(enabled))
        body = np.array([e.pos_body_m for e in els], dtype=float)
        dist = _pairwise(body)
        np.fill_diagonal(dist, np.inf)
        if dist.min() <= MIN_SEPARATION_M:
            i, j = np.unravel_index(int(np.argmin(dist)), dist.shape)
            raise _geom_error("GEOMETRY", "elements %d and %d are coincident (%.3g m <= %.0e m)"
                              % (els[i].id, els[j].id, dist[i, j], MIN_SEPARATION_M))
        self.elements = els
        self.n = len(els)
        self.orientation = orientation
        self.phase_reference = phase_reference
        self.name = str(name or "")
        self.design_freq_hz = float(design_freq_hz or 0.0)
        self.site_lla = None if site_lla is None else tuple(float(c) for c in site_lla)
        self.element_size_m = float(element_size_m or 0.0)
        self.survey_sigma_m = float(survey_sigma_m or 0.0)
        self.frame = frame
        self.rx_order = None if rx_order is None else list(rx_order)
        self.tx_order = None if tx_order is None else list(tx_order)
        self.scan_limit_deg = scan_limit_deg
        self.provenance_issues = tuple(provenance_issues)
        self.preset_spacing_m = None if preset_spacing_m is None else float(preset_spacing_m)
        # attitude (body -> ENU, for directions: boresight and element patterns)
        self._R = orientation.matrix()
        # positions: rotated by the attitude in the body frame; already ENU otherwise
        self._Rpos = self._R if frame == "body" else np.eye(3)
        self._enu = body @ self._Rpos.T
        self._index = {e.id: k for k, e in enumerate(els)}
        en = np.array([e.enabled for e in els])
        if phase_reference == "centroid":
            self._ref = self._enu[en].mean(axis=0)
        else:
            self._ref = self._enu[int(np.argmax(en))].copy()   # lowest-id enabled element

    # --- frames ----------------------------------------------------------------
    # ``orientation`` is the platform attitude. In the body frame it rotates the
    # element positions AND directions; in the enu frame (also lla files) the
    # positions are stored in ENU already and the attitude only orients the
    # body-frame directions: boresight and element patterns.
    def rotation(self) -> np.ndarray:
        """3x3 body -> ENU rotation of directions (platform attitude)."""
        return self._R.copy()

    def body_to_enu(self, v) -> np.ndarray:
        """Body-frame direction(s) -> ENU."""
        return np.asarray(v, dtype=float) @ self._R.T

    def enu_to_body(self, v) -> np.ndarray:
        """ENU direction(s) -> body frame (element patterns)."""
        return np.asarray(v, dtype=float) @ self._R

    def boresight_enu(self) -> np.ndarray:
        """Platform boresight (body +y) in ENU; for enu / lla geometries the platform
        heading / pitch / roll still set it (positions are not rotated)."""
        return self._R[:, 1].copy()

    def angle_from_boresight_deg(self, u) -> float:
        """Angle (deg) between ENU direction ``u`` and the platform boresight."""
        u = np.asarray(u, dtype=float).reshape(3)
        nu = float(np.linalg.norm(u))
        if not nu > 0:
            raise ValueError("zero direction")
        c = float(np.clip(self.boresight_enu() @ u / nu, -1.0, 1.0))
        return math.degrees(math.acos(c))

    def reference_point_enu(self) -> np.ndarray:
        """Phase reference in the un-referenced ENU frame (rotated body coordinates)."""
        return self._ref.copy()

    # --- element sets ------------------------------------------------------------
    def _check_which(self, which):
        if which not in WHICH:
            raise ValueError("which must be one of %s, got %r" % (WHICH, which))

    def elements_for(self, which: str) -> tuple:
        """Enabled (and, for rx/tx, mapped) elements sorted by global channel order."""
        self._check_which(which)
        if which == "all":
            return tuple(e for e in self.elements if e.enabled)
        sel = [e for e in self.elements if e.enabled and getattr(e, which) is not None]
        order = self.rx_order if which == "rx" else self.tx_order
        if order is not None:
            sel.sort(key=lambda e: (_ref_key(order, getattr(e, which)), e.id))
        return tuple(sel)

    def positions(self, which: str = "rx") -> np.ndarray:
        """(N, 3) ENU positions relative to the phase reference, ``elements_for`` order."""
        els = self.elements_for(which)
        idx = [self._index[e.id] for e in els]
        if not idx:
            return np.zeros((0, 3))
        return self._enu[idx] - self._ref

    def channel_indices(self, which: str, refs: list) -> list:
        """For each element of ``elements_for(which)`` its index into ``refs``."""
        if which not in ("rx", "tx"):
            raise ValueError("which must be rx or tx")
        refs = list(refs)
        out = []
        for e in self.elements_for(which):
            ref = getattr(e, which)
            try:
                out.append(refs.index(ref))
            except ValueError:
                raise _geom_error("GEOM_MAP", "element %d %s ref %s is not a configured channel"
                                  % (e.id, which, ref)) from None
        return out

    # --- size ---------------------------------------------------------------------
    def max_baseline_m(self, which="rx") -> float:
        P = self.positions(which)
        return float(_pairwise(P).max()) if P.shape[0] >= 2 else 0.0

    def aperture_m(self, which="rx") -> float:
        """Largest dimension D (max baseline plus one element size)."""
        P = self.positions(which)
        if P.shape[0] == 0:
            return 0.0
        return self.max_baseline_m(which) + self.element_size_m

    def min_spacing_m(self, which="rx") -> float:
        P = self.positions(which)
        if P.shape[0] < 2:
            return 0.0
        d = _pairwise(P)
        np.fill_diagonal(d, np.inf)
        return float(d.min())

    def rank(self, which="rx", rtol: float = 0.01) -> int:
        return position_subspace(self.positions(which), rtol)[0]

    def plane_normal(self, which="rx", rtol: float = 0.01):
        """Unit normal of a planar (rank 2) set, else None."""
        r, _, comp = position_subspace(self.positions(which), rtol)
        return comp[:, 0].copy() if r == 2 else None

    def ambiguity(self, which="rx", rtol: float = 0.01) -> str:
        """Frozen label (spec 4.4 / 10.6): cone (rank 1), mirror_elevation (rank 2: directions
        mirrored about the array plane), none (3-D) or degenerate. ``plane_kind`` tells
        which plane (horizontal: +el / -el; vertical: front / back)."""
        r = position_subspace(self.positions(which), rtol)[0]
        if r == 1:
            return "cone"
        if r == 2:
            return "mirror_elevation"
        if r == 3:
            return "none"
        return "degenerate"

    def plane_kind(self, which="rx", rtol: float = 0.01):
        """For a planar (rank 2) set: horizontal (normal within 5 deg of Up), vertical
        (normal within 5 deg of the horizon) or tilted; None otherwise."""
        r, _, comp = position_subspace(self.positions(which), rtol)
        if r != 2:
            return None
        nz = abs(float(comp[2, 0]))
        if nz > math.cos(math.radians(5.0)):
            return "horizontal"
        if nz < math.sin(math.radians(5.0)):
            return "vertical"
        return "tilted"

    def fraunhofer_m(self, f_hz, which="rx") -> float:
        """Far-field distance 2 D^2 / lambda."""
        D = self.aperture_m(which)
        return 2.0 * D * D / ac.wavelength(f_hz)

    def lattice(self, which="rx", rtol: float = 0.01) -> dict:
        return detect_lattice(self.positions(which), rtol)

    def max_pos_sigma_m(self, which="rx") -> float:
        els = self.elements_for(which)
        s = max((e.pos_sigma_m for e in els), default=0.0)
        return max(float(s), self.survey_sigma_m)

    # --- derived geometries ----------------------------------------------------------
    def _clone(self, **kw):
        args = dict(elements=self.elements, orientation=self.orientation,
                    phase_reference=self.phase_reference, name=self.name,
                    design_freq_hz=self.design_freq_hz, site_lla=self.site_lla,
                    element_size_m=self.element_size_m, survey_sigma_m=self.survey_sigma_m,
                    frame=self.frame, rx_order=self.rx_order, tx_order=self.tx_order,
                    scan_limit_deg=self.scan_limit_deg, provenance_issues=self.provenance_issues,
                    preset_spacing_m=self.preset_spacing_m)
        args.update(kw)
        return ArrayGeometry(**args)

    def with_orientation(self, o) -> "ArrayGeometry":
        """New platform attitude. Body-frame geometries rotate their positions; enu
        geometries keep their (already ENU) positions and change only the boresight."""
        return self._clone(orientation=o)

    def with_scan_limit(self, scan_limit_deg) -> "ArrayGeometry":
        return self._clone(scan_limit_deg=scan_limit_deg)

    def with_channel_order(self, rx_order=None, tx_order=None) -> "ArrayGeometry":
        return self._clone(rx_order=rx_order, tx_order=tx_order)

    def to_dict(self) -> dict:
        """Canonical JSON form (spec 6.1); positions in metres in ``frame``."""
        plat = self.orientation.to_dict()
        plat["site_lla"] = list(self.site_lla) if self.site_lla is not None else None
        els = []
        for e in self.elements:
            d = {"id": e.id, "pos": [float(c) for c in e.pos_body_m],
                 "rx": e.rx.to_dict() if e.rx is not None else None,
                 "tx": e.tx.to_dict() if e.tx is not None else None,
                 "enabled": bool(e.enabled), "label": e.label,
                 "pattern": e.pattern.to_dict(), "polarization": e.polarization,
                 "pos_sigma_m": float(e.pos_sigma_m)}
            ap = {}
            if e.apriori_rx is not None:
                ap["rx"] = e.apriori_rx.to_dict()
            if e.apriori_tx is not None:
                ap["tx"] = e.apriori_tx.to_dict()
            if ap:
                d["apriori"] = ap
            els.append(d)
        return {"schema": GEOMETRY_SCHEMA, "name": self.name, "units": "m",
                "design_freq_hz": self.design_freq_hz, "frame": self.frame,
                "phase_reference": self.phase_reference, "platform": plat,
                "survey": {"sigma_m": self.survey_sigma_m},
                "element_size_m": self.element_size_m, "elements": els, "preset": None}

    def __repr__(self):
        return "ArrayGeometry(name=%r, n=%d, rx=%d, tx=%d)" % (
            self.name, self.n, len(self.elements_for("rx")), len(self.elements_for("tx")))


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
def _preset_elements(body, device, map_tx, pattern=ElementPattern()):
    return tuple(Element(id=i, pos_body_m=tuple(p), rx=ChannelRef(device, i),
                         tx=ChannelRef(device, i) if map_tx else None, pattern=pattern)
                 for i, p in enumerate(body))


def _positive(x, what):
    v = _finite(x, what)
    if v <= 0:
        raise _geom_error("GEOMETRY", "%s must be > 0, got %g" % (what, v))
    return v


def _count(n, what, lo=1):
    if isinstance(n, bool) or int(n) != n or int(n) < lo:
        raise _geom_error("GEOMETRY", "%s must be an integer >= %d, got %r" % (what, lo, n))
    return int(n)


def _ula_body(n, spacing_m, axis_az_deg):
    n = _count(n, "ULA n", 2)
    d = _positive(spacing_m, "ULA spacing")
    a = math.radians(float(axis_az_deg))
    axis = np.array([math.sin(a), math.cos(a), 0.0])
    return np.outer(np.arange(n), axis) * d


def _ura_body(nx, ny, dx, dy, plane):
    nx, ny = _count(nx, "URA nx"), _count(ny, "URA ny")
    if nx * ny < 2:
        raise _geom_error("GEOMETRY", "URA needs nx*ny >= 2")
    dx, dy = _positive(dx, "URA dx"), _positive(dy, "URA dy")
    if plane not in PLANES:
        raise _geom_error("GEOMETRY", "plane must be vertical or horizontal")
    out = []
    for iy in range(ny):
        for ix in range(nx):
            out.append((ix * dx, iy * dy, 0.0) if plane == "horizontal" else (ix * dx, 0.0, iy * dy))
    return np.array(out, dtype=float)


def _tri_body(nx, ny, d, plane):
    nx, ny = _count(nx, "tri nx"), _count(ny, "tri ny")
    if nx * ny < 2:
        raise _geom_error("GEOMETRY", "triangular lattice needs nx*ny >= 2")
    d = _positive(d, "triangular spacing")
    if plane not in PLANES:
        raise _geom_error("GEOMETRY", "plane must be vertical or horizontal")
    h = d * math.sqrt(3.0) / 2.0
    out = []
    for iy in range(ny):
        for ix in range(nx):
            x = ix * d + (iy % 2) * d / 2.0
            out.append((x, iy * h, 0.0) if plane == "horizontal" else (x, 0.0, iy * h))
    return np.array(out, dtype=float)


def _uca_body(n, radius_m, arc_spacing_m, gamma0_deg):
    n = _count(n, "UCA n", 3)
    if radius_m is not None and float(radius_m) > 0:
        R = _positive(radius_m, "UCA radius")
    elif arc_spacing_m is not None and float(arc_spacing_m) > 0:
        R = _positive(arc_spacing_m, "UCA arc spacing") / (2.0 * math.sin(math.pi / n))
    else:
        raise _geom_error("GEOMETRY", "UCA needs radius_m or arc_spacing_m > 0")
    g = np.radians(float(gamma0_deg) + 360.0 * np.arange(n) / n)
    return np.stack([R * np.sin(g), R * np.cos(g), np.zeros(n)], axis=1)


def preset_ula(n, spacing_m, axis_az_deg=90.0, device="sim0", map_tx=True) -> ArrayGeometry:
    """N-element ULA along the horizontal body axis at compass ``axis_az_deg`` (90 = body +x)."""
    body = _ula_body(n, spacing_m, axis_az_deg)
    return ArrayGeometry(_preset_elements(body, device, map_tx), name="ula%d" % len(body))


def preset_ura(nx, ny, dx_m, dy_m, device="sim0", map_tx=True, plane="vertical") -> ArrayGeometry:
    """nx x ny rectangular array; element i = iy*nx + ix. ``plane`` vertical = body x-z panel."""
    body = _ura_body(nx, ny, dx_m, dy_m, plane)
    return ArrayGeometry(_preset_elements(body, device, map_tx), name="ura%dx%d" % (nx, ny))


def preset_uca(n, radius_m=None, arc_spacing_m=None, gamma0_deg=0.0, device="sim0",
               map_tx=True) -> ArrayGeometry:
    """Horizontal UCA; element n at compass angle gamma0 + 360 n/N. Arc spacing d = 2 R sin(pi/N)."""
    body = _uca_body(n, radius_m, arc_spacing_m, gamma0_deg)
    return ArrayGeometry(_preset_elements(body, device, map_tx), name="uca%d" % len(body))


def preset_tri(nx, ny, d_m, device="sim0", map_tx=True, plane="vertical") -> ArrayGeometry:
    """Equilateral triangular lattice: ny rows of nx, odd rows offset by d/2, row pitch d sqrt(3)/2."""
    body = _tri_body(nx, ny, d_m, plane)
    return ArrayGeometry(_preset_elements(body, device, map_tx), name="tri%dx%d" % (nx, ny))


# ---------------------------------------------------------------------------
# Scan region and analytic checks
# ---------------------------------------------------------------------------
def scan_transverse_max(B: np.ndarray, comp: np.ndarray, boresight, scan_limit_deg: float) -> float:
    """max |P_S u| over the cone of half-angle ``scan_limit_deg`` around ``boresight``.

    ``B`` spans the position subspace S (rank 1 or 2), ``comp`` its complement.
    For a line, beta = angle(boresight, axis); for a plane beta = angle(boresight,
    plane); the maximum is cos(max(0, beta - scan_limit)).
    """
    b = np.asarray(boresight, dtype=float)
    b = b / np.linalg.norm(b)
    r = B.shape[1]
    if r == 1:
        beta = math.acos(min(1.0, abs(float(b @ B[:, 0]))))
    elif r == 2:
        beta = math.asin(min(1.0, abs(float(b @ comp[:, 0]))))
    else:
        return 1.0
    return min(1.0, math.cos(max(0.0, beta - math.radians(float(scan_limit_deg)))))


def _beta(B, comp, b):
    b = np.asarray(b, dtype=float) / np.linalg.norm(b)
    if B.shape[1] == 1:
        return math.acos(min(1.0, abs(float(b @ B[:, 0]))))
    return math.asin(min(1.0, abs(float(b @ comp[:, 0]))))


def grating_free_scan_deg(geom: ArrayGeometry, f_hz, which="rx"):
    """Largest cone half-angle around boresight that is grating-lobe free at ``f_hz``.

    None for non-periodic sets (UCA / irregular / 3-D); 0 when even boresight
    shows a grating lobe; 90 when the whole forward hemisphere is free.
    """
    P = geom.positions(which)
    lat = detect_lattice(P)
    if not lat["periodic"]:
        return None
    r, B, comp = position_subspace(P)
    gmin = min_reciprocal_norm(lat["d1"], lat["d2"])
    s_free = ac.wavelength(f_hz) * gmin - 1.0
    if s_free >= 1.0 - 1e-12:
        return 90.0
    beta = _beta(B, comp, geom.boresight_enu())
    if s_free < math.cos(beta) - 1e-12:
        return 0.0
    return max(0.0, math.degrees(beta - math.acos(max(-1.0, min(1.0, s_free)))))


def _freqs(f_min_hz, f_max_hz):
    f_max = float(f_max_hz)
    if not (math.isfinite(f_max) and f_max > 0):
        raise ValueError("f_max_hz must be > 0")
    f_min = float(f_min_hz) if f_min_hz and float(f_min_hz) > 0 else f_max
    if f_min > f_max:
        f_min, f_max = f_max, f_min
    return f_min, f_max


def validate_geometry(geom, f_min_hz, f_max_hz, scan_limit_deg, which="rx", look=None,
                      beacon_range_m=None) -> list:
    """Analytic geometry checks (spec 10.6) -> [Issue] (codes GEOMETRY, GRATING_LOBE).

    ``look`` (az, el) enables the coplanar-elevation warning; ``beacon_range_m``
    the Fraunhofer comparison. The numeric search lives in
    ``active_pattern.numeric_ambiguity_search``.
    """
    where = "geometry[%s]" % which
    issues = []

    def add(level, code, msg):
        issues.append(Issue(level, code, msg, where))

    f_min, f_max = _freqs(f_min_hz, f_max_hz)
    for it in geom.provenance_issues:          # how the geometry was built (e.g. derived pitch)
        issues.append(it)
    lam_min = C0 / f_max                      # shortest wavelength (grating lobes)
    lam_max = C0 / f_min                      # longest wavelength (coupling)
    els = geom.elements_for(which)
    n = len(els)
    if n == 0 and which == "tx":
        return issues
    if n < 2:
        if which == "tx":
            add("warning", "GEOMETRY", "tx set has 1 element: no TX beamforming (single-chain TX only)")
        else:
            add("error", "GEOMETRY", "%s set has %d element(s); need >= 2" % (which, n))
        return issues
    P = geom.positions(which)
    dmin = geom.min_spacing_m(which)
    if dmin < 0.05 * lam_min:
        add("error", "GEOMETRY", "minimum spacing %.4g m < 0.05 lambda (%.4g m) at %.6g Hz"
            % (dmin, 0.05 * lam_min, f_max))
    elif dmin < 0.3 * lam_max:
        add("warning", "GEOMETRY", "minimum spacing %.4g m = %.3f lambda at %.6g Hz (< 0.3 lambda): "
            "strong mutual coupling" % (dmin, dmin / lam_max, f_min))
    if geom.element_size_m > 0 and dmin < geom.element_size_m:
        add("warning", "GEOMETRY", "minimum spacing %.4g m < element size %.4g m (physical overlap)"
            % (dmin, geom.element_size_m))
    r, B, comp = position_subspace(P)
    amb = geom.ambiguity(which)
    plane = geom.plane_kind(which)
    if r == 0:
        add("error", "GEOMETRY", "degenerate geometry (rank 0)")
        return issues
    if r == 1:
        add("info", "GEOMETRY", "linear array: cone-of-confusion ambiguity about the array axis")
    elif r == 2:
        add("info", "GEOMETRY", "planar array (%s, %s plane): directions mirrored about the array plane "
            "are indistinguishable" % (amb, plane))
    if look is not None:
        lel = float(look[1])
        if r == 2 and plane == "horizontal" and abs(lel) > 0:
            add("warning", "GEOMETRY", "look elevation %g deg on a horizontal coplanar array: "
                "+el and -el are indistinguishable" % lel)
        if r == 1 and abs(lel) > 0:
            add("warning", "GEOMETRY", "look elevation %g deg on a linear array: only the angle to "
                "the axis is resolved (cone ambiguity)" % lel)
    lat = detect_lattice(P)
    scan = float(scan_limit_deg)
    if lat["periodic"] and r in (1, 2):
        s_max = scan_transverse_max(B, comp, geom.boresight_enu(), scan)
        gmin = min_reciprocal_norm(lat["d1"], lat["d2"])
        if lam_min * gmin < (1.0 + s_max) * (1.0 - 1e-9):
            free = grating_free_scan_deg(geom, f_max, which)
            add("error", "GRATING_LOBE", "%s lattice (spacing %.4g m = %.3f lambda) has grating lobes "
                "at %.6g Hz within the %.1f deg scan (needs lambda |G|min >= 1 + %.3f, got %.3f); "
                "maximum grating-free scan %.1f deg"
                % (lat["kind"], lat["spacing"], lat["spacing"] / lam_min, f_max, scan, s_max,
                   lam_min * gmin, free if free is not None else 0.0))
    elif lat["kind"] == "uca":
        arc = lat["spacing"]
        if arc > 0.5 * lam_min * (1.0 + 1e-9):
            add("error", "GRATING_LOBE", "UCA arc spacing %.4g m = %.3f lambda > lambda/2 at %.6g Hz"
                % (arc, arc / lam_min, f_max))
        k0r = 2.0 * math.pi * lat["radius"] / lam_min
        if n <= 2.0 * k0r + 1.0:
            add("warning", "GEOMETRY", "UCA N = %d <= 2 k0 R + 1 = %.2f at %.6g Hz: phase-mode "
                "aliasing (element-space beamforming is unaffected)" % (n, 2.0 * k0r + 1.0, f_max))
    else:
        add("info", "GEOMETRY", "%s geometry (rank %d): no closed-form grating rule; the numeric "
            "ambiguity search applies" % (lat["kind"], r))
    rff = geom.fraunhofer_m(f_max, which)
    if beacon_range_m is not None and float(beacon_range_m) > 0 and float(beacon_range_m) < rff:
        add("warning", "GEOMETRY", "beacon range %.3g m is inside the Fraunhofer distance %.3g m: "
            "use the near-field (spherical) model" % (float(beacon_range_m), rff))
    tol = lam_min / 360.0
    sig = geom.max_pos_sigma_m(which)
    if sig > tol:
        add("warning", "GEOMETRY", "position uncertainty %.3g m exceeds lambda/360 = %.3g m "
            "(%.2f deg rms phase at %.6g Hz)" % (sig, tol, 360.0 * sig / lam_min, f_max))
    return issues


# ---------------------------------------------------------------------------
# Geometry files
# ---------------------------------------------------------------------------
def _cfg_attr(cfg, path, default=None):
    obj = cfg
    for p in path.split("."):
        if obj is None:
            return default
        obj = getattr(obj, p, None)
    return default if obj is None else obj


def _cfg_default_pattern(cfg) -> ElementPattern:
    """Default element pattern from ``[active_geometry]`` + ``[active_antenna]``."""
    if cfg is None:
        return ElementPattern()
    kind = _cfg_attr(cfg, "geometry.element_pattern", "isotropic")
    q = float(_cfg_attr(cfg, "geometry.element_q", 1.0))
    prof = getattr(cfg, "antenna_profile", None)
    gain = float(prof.element_gain_dbi) if prof is not None else None
    return ElementPattern(kind=kind, q=q, gain_dbi=gain)


def _cfg_polarization(cfg, default="vertical"):
    prof = getattr(cfg, "antenna_profile", None) if cfg is not None else None
    return getattr(prof, "polarization", None) or default


_PATTERN_KEYS = ("kind", "q", "gain_dbi", "boresight_az_deg", "boresight_el_deg", "table", "table_file",
                 "table_floor_db", "table_extend")


def _read_table_file(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                if not row or row[0].strip().startswith("#"):
                    continue
                try:
                    rows.append(tuple(float(c) for c in row[:4]))
                except ValueError:
                    continue                        # header line
    except OSError as exc:
        raise _geom_error("GEOMETRY_FILE", "cannot read pattern table %s: %s" % (path, exc)) from None
    return tuple(rows)


def _pattern_from(d, base, base_dir, where) -> ElementPattern:
    if d is None:
        return base
    if not isinstance(d, dict):
        raise _geom_error("GEOMETRY", "%s pattern must be an object" % where)
    bad = set(d) - set(_PATTERN_KEYS) - set(_FREE_TEXT_KEYS)
    if bad:
        raise _geom_error("GEOMETRY", "%s pattern: unknown key(s) %s" % (where, sorted(bad)))
    kw = {k: d[k] for k in ("kind", "q", "gain_dbi", "boresight_az_deg", "boresight_el_deg",
                            "table_floor_db", "table_extend") if k in d}
    if "table_file" in d and d["table_file"]:
        p = d["table_file"]
        if not os.path.isabs(p):
            p = os.path.join(base_dir or ".", p)
        kw["table"] = _read_table_file(p)
        kw.setdefault("kind", "table")
    elif "table" in d and d["table"]:
        kw["table"] = tuple(tuple(r) for r in d["table"])
        kw.setdefault("kind", "table")
    elif kw.get("kind", base.kind) != "table":
        kw["table"] = ()
    try:
        return replace(base, **kw)
    except GeometryError as exc:
        raise _geom_error("GEOMETRY", "%s: %s" % (where, exc)) from None
    except (TypeError, ValueError) as exc:
        raise _geom_error("GEOMETRY", "%s pattern: %s" % (where, exc)) from None


def _ref_from(d, where):
    if d is None:
        return None
    if not isinstance(d, dict) or set(d) - {"device", "channel"} or "device" not in d or "channel" not in d:
        raise _geom_error("GEOM_MAP", "%s must be {\"device\": name, \"channel\": int} or null" % where)
    ch = d["channel"]
    if isinstance(ch, bool) or not isinstance(ch, int) or ch < 0:
        raise _geom_error("GEOM_MAP", "%s channel must be an int >= 0, got %r" % (where, ch))
    dev = d["device"]
    if not isinstance(dev, str) or not dev:
        raise _geom_error("GEOM_MAP", "%s device must be a non-empty string" % where)
    return ChannelRef(dev, ch)


def _apriori_from(d, where):
    if d is None:
        return None
    if not isinstance(d, dict) or set(d) - {"delay_s", "phase_deg", "gain_db"}:
        raise _geom_error("GEOMETRY", "%s apriori must be {delay_s, phase_deg, gain_db}" % where)
    return Apriori(**{k: _finite(v, "%s apriori %s" % (where, k)) for k, v in d.items()})


def _remap_preset(elements, cfg, map_tx=True):
    """Preset element i -> global RX i and TX i (when those indices exist)."""
    if cfg is None:
        return elements
    rx = list(cfg.rx_refs())
    tx = list(cfg.tx_refs())
    out = []
    for e in elements:
        out.append(replace(e, rx=rx[e.id] if e.id < len(rx) else None,
                           tx=(tx[e.id] if e.id < len(tx) else None) if map_tx else None))
    return tuple(out)


def _orders(cfg):
    if cfg is None:
        return None, None
    return list(cfg.rx_refs()), list(cfg.tx_refs())


def _unit_scale(units, fd, what) -> float:
    """Metres per file unit. ``lambda`` needs the file's own design frequency: the
    physical element positions never follow the configured operating frequency."""
    if units == "lambda":
        if not fd > 0:
            raise _geom_error("GEOMETRY", "%s: units = lambda requires design_freq_hz > 0 in the file "
                              "(positions are not scaled by [active_geometry] / center_freq_hz)" % what)
        return C0 / fd
    return 1e-3 if units == "mm" else 1.0


def _cfg_design_freq(cfg) -> float:
    """``[active_geometry] design_freq_hz`` when explicitly > 0 (label only), else 0."""
    v = float(_cfg_attr(cfg, "geometry.design_freq_hz", 0.0) or 0.0)
    return v if v > 0 else 0.0


def _cfg_scan_limit(cfg):
    if cfg is None:
        return None
    v = _cfg_attr(cfg, "geometry.scan_limit_deg", None)
    return None if v is None else float(v)


_TOP_KEYS = ("schema", "name", "units", "design_freq_hz", "frame", "phase_reference", "platform",
             "survey", "element_size_m", "defaults", "elements", "preset")
_ELEMENT_KEYS = ("id", "pos", "lla", "rx", "tx", "enabled", "label", "pattern", "polarization",
                 "pos_sigma_m", "apriori")


def geometry_from_dict(d: dict, base_dir: str = "", cfg=None) -> ArrayGeometry:
    """Build an ArrayGeometry from the canonical JSON object (spec 6.1).

    With ``cfg`` the channel map is validated against ``[active_devices]``,
    ``"map": "auto"`` presets map element i to global RX/TX i, and missing
    platform / phase-reference / pattern values come from the config.
    """
    if not isinstance(d, dict):
        raise _geom_error("GEOMETRY", "geometry must be a JSON object")
    bad = set(d) - set(_TOP_KEYS) - set(_FREE_TEXT_KEYS)
    if bad:
        raise _geom_error("GEOMETRY", "unknown top-level key(s) %s" % sorted(bad))
    if d.get("schema") != GEOMETRY_SCHEMA:
        raise _geom_error("GEOMETRY", "schema must be %r, got %r" % (GEOMETRY_SCHEMA, d.get("schema")))
    units = d.get("units", "m")
    if units not in UNITS:
        raise _geom_error("GEOMETRY", "units must be one of %s, got %r" % (UNITS, units))
    fd = d.get("design_freq_hz")
    fd = _finite(fd, "design_freq_hz") if fd is not None else 0.0
    scale = _unit_scale(units, fd, "geometry file")
    if fd <= 0:
        fd = _cfg_design_freq(cfg)             # informative only: never scales positions
    frame = d.get("frame", "body")
    if frame not in FRAMES:
        raise _geom_error("GEOMETRY", "frame must be one of %s, got %r" % (FRAMES, frame))
    plat = d.get("platform")
    if plat is not None and not isinstance(plat, dict):
        raise _geom_error("GEOMETRY", "platform must be an object")
    plat = plat or {}
    bad = set(plat) - {"heading_deg", "pitch_deg", "roll_deg", "site_lla"} - set(_FREE_TEXT_KEYS)
    if bad:
        raise _geom_error("GEOMETRY", "platform: unknown key(s) %s" % sorted(bad))

    def _att(key):
        if key in plat and plat[key] is not None:
            return _finite(plat[key], "platform." + key)
        return float(_cfg_attr(cfg, "geometry." + key, 0.0))

    site = plat.get("site_lla")
    site = _vec3(site, "platform.site_lla") if site is not None else None
    # body: the attitude rotates positions and directions; enu / lla: positions are
    # already ENU and the attitude orients only the boresight and element patterns
    orientation = Orientation(_att("heading_deg"), _att("pitch_deg"), _att("roll_deg"))
    if frame == "lla" and site is None:
        raise _geom_error("GEOMETRY", "frame = lla requires platform.site_lla")
    phase_ref = d.get("phase_reference") or _cfg_attr(cfg, "geometry.phase_reference", "element0")
    survey = d.get("survey") or {}
    if not isinstance(survey, dict):
        raise _geom_error("GEOMETRY", "survey must be an object")
    survey_sigma = _finite(survey.get("sigma_m", 0.0) or 0.0, "survey.sigma_m")
    esize = d.get("element_size_m")
    esize = _finite(esize, "element_size_m") if esize is not None else \
        float(_cfg_attr(cfg, "geometry.element_size_m", 0.0))
    defaults = d.get("defaults") or {}
    if not isinstance(defaults, dict) or set(defaults) - {"pattern", "polarization"} - set(_FREE_TEXT_KEYS):
        raise _geom_error("GEOMETRY", "defaults may hold only pattern and polarization")
    def_pat = _pattern_from(defaults.get("pattern"), _cfg_default_pattern(cfg), base_dir, "defaults")
    def_pol = defaults.get("polarization") or _cfg_polarization(cfg)
    has_el = d.get("elements") is not None
    has_pre = d.get("preset") is not None
    if has_el == has_pre:
        raise _geom_error("GEOMETRY", "exactly one of 'elements' / 'preset' must be present")
    name = str(d.get("name") or "")
    rx_order, tx_order = _orders(cfg)

    if has_pre:
        elements = _preset_from_dict(d["preset"], scale, def_pat, def_pol, cfg)
        geom = ArrayGeometry(elements, orientation=orientation, phase_reference=phase_ref, name=name,
                             design_freq_hz=fd, site_lla=site, element_size_m=esize,
                             survey_sigma_m=survey_sigma, frame="body" if frame == "body" else "enu",
                             rx_order=rx_order, tx_order=tx_order, scan_limit_deg=_cfg_scan_limit(cfg))
    else:
        raw = d["elements"]
        if not isinstance(raw, list):
            raise _geom_error("GEOMETRY", "elements must be a list")
        elements = []
        for k, ed in enumerate(raw):
            where = "elements[%d]" % k
            if not isinstance(ed, dict):
                raise _geom_error("GEOMETRY", "%s must be an object" % where)
            bad = set(ed) - set(_ELEMENT_KEYS) - set(_FREE_TEXT_KEYS)
            if bad:
                raise _geom_error("GEOMETRY", "%s: unknown key(s) %s" % (where, sorted(bad)))
            if "id" not in ed:
                raise _geom_error("GEOMETRY", "%s: missing id" % where)
            eid = ed["id"]
            if isinstance(eid, bool) or not isinstance(eid, int) or eid < 0:
                raise _geom_error("GEOMETRY", "%s: id must be an int >= 0" % where)
            if frame == "lla":
                if "lla" not in ed:
                    raise _geom_error("GEOMETRY", "%s: frame lla needs \"lla\": [lat, lon, alt]" % where)
                lat, lon, alt = _vec3(ed["lla"], where + ".lla")
                pos = tuple(float(c) for c in lla_to_enu(lat, lon, alt, *site))
            else:
                if "pos" not in ed:
                    raise _geom_error("GEOMETRY", "%s: missing pos" % where)
                pos = tuple(c * scale for c in _vec3(ed["pos"], where + ".pos"))
            enabled = ed.get("enabled", True)
            if not isinstance(enabled, bool):
                raise _geom_error("GEOMETRY", "%s: enabled must be true/false" % where)
            ap = ed.get("apriori") or {}
            if not isinstance(ap, dict) or set(ap) - {"rx", "tx"}:
                raise _geom_error("GEOMETRY", "%s: apriori must be {\"rx\": {...}, \"tx\": {...}}" % where)
            elements.append(Element(
                id=eid, pos_body_m=pos, rx=_ref_from(ed.get("rx"), where + ".rx"),
                tx=_ref_from(ed.get("tx"), where + ".tx"),
                pattern=_pattern_from(ed.get("pattern"), def_pat, base_dir, where),
                enabled=enabled, label=str(ed.get("label", "") or ""),
                pos_sigma_m=_finite(ed.get("pos_sigma_m", 0.0) or 0.0, where + ".pos_sigma_m"),
                polarization=str(ed.get("polarization") or def_pol),
                apriori_rx=_apriori_from(ap.get("rx"), where + ".rx"),
                apriori_tx=_apriori_from(ap.get("tx"), where + ".tx")))
        ids = [e.id for e in elements]
        if len(set(ids)) != len(ids):
            raise _geom_error("GEOMETRY", "duplicate element id(s) %s"
                              % sorted({i for i in ids if ids.count(i) > 1}))
        if sorted(ids) != list(range(len(ids))):
            raise _geom_error("GEOMETRY", "element ids must be consecutive from 0, got %s" % sorted(ids))
        geom = ArrayGeometry(elements, orientation=orientation, phase_reference=phase_ref, name=name,
                             design_freq_hz=fd, site_lla=site, element_size_m=esize,
                             survey_sigma_m=survey_sigma, frame="body" if frame == "body" else "enu",
                             rx_order=rx_order, tx_order=tx_order, scan_limit_deg=_cfg_scan_limit(cfg))
    if cfg is not None:
        issues = geometry_map_issues(geom, cfg)
        errs = [i for i in issues if i.level == "error"]
        if errs:
            raise _geom_error("GEOM_MAP", "; ".join(i.message for i in errs), issues)
    return geom


_PRESET_KEYS = {
    "ula": ("n", "num_elements", "spacing", "axis_az_deg"),
    "ura": ("nx", "ny", "dx", "dy", "spacing", "plane"),
    "uca": ("n", "num_elements", "radius", "arc_spacing", "spacing", "gamma0_deg"),
    "tri": ("nx", "ny", "d", "spacing", "plane"),
}


def _preset_from_dict(p, scale, pattern, polarization, cfg):
    if not isinstance(p, dict):
        raise _geom_error("GEOMETRY", "preset must be an object")
    kind = p.get("type")
    if kind not in PRESET_TYPES:
        raise _geom_error("GEOMETRY", "preset type must be one of %s, got %r" % (PRESET_TYPES, kind))
    bad = set(p) - set(_PRESET_KEYS[kind]) - {"type", "device", "map", "map_tx"} - set(_FREE_TEXT_KEYS)
    if bad:
        raise _geom_error("GEOMETRY", "preset %s: unknown key(s) %s" % (kind, sorted(bad)))
    mp = p.get("map", "auto")
    if mp != "auto":
        raise _geom_error("GEOMETRY", "preset map must be \"auto\", got %r" % (mp,))
    device = str(p.get("device") or "sim0")
    map_tx = bool(p.get("map_tx", True))

    def g(*keys, default=None):
        for k in keys:
            if p.get(k) is not None:
                return p[k]
        return default

    def length(*keys):
        v = g(*keys)
        return None if v is None else _finite(v, "preset " + keys[0]) * scale

    if kind == "ula":
        body = _ula_body(g("n", "num_elements"), length("spacing"), g("axis_az_deg", default=90.0))
    elif kind == "ura":
        sp = length("spacing")
        body = _ura_body(g("nx"), g("ny"), length("dx") or sp, length("dy") or sp,
                         g("plane", default="vertical"))
    elif kind == "uca":
        body = _uca_body(g("n", "num_elements"), length("radius"), length("arc_spacing", "spacing"),
                         g("gamma0_deg", default=0.0))
    else:
        body = _tri_body(g("nx"), g("ny"), length("d", "spacing"), g("plane", default="vertical"))
    els = tuple(Element(id=i, pos_body_m=tuple(b), rx=ChannelRef(device, i),
                        tx=ChannelRef(device, i) if map_tx else None, pattern=pattern,
                        polarization=polarization) for i, b in enumerate(body))
    return _remap_preset(els, cfg, map_tx)


def geometry_map_issues(geom: ArrayGeometry, cfg) -> list:
    """Channel-map checks against ``[active_devices]`` (code GEOM_MAP).

    Errors: an unknown device or a channel the device does not list.
    Warnings: a configured RX channel no enabled element uses (except
    ``loopback_rx_chan``), a TX channel no element uses (except an injection
    source ``tx:<k>``), an enabled element with neither RX nor TX.
    """
    issues = []
    devs = {d.name: d for d in cfg.devices}
    for e in geom.elements:
        for which in ("rx", "tx"):
            ref = getattr(e, which)
            if ref is None:
                continue
            dev = devs.get(ref.device)
            where = "geometry element %d %s" % (e.id, which)
            if dev is None:
                issues.append(Issue("error", "GEOM_MAP", "element %d %s device %r is not in [active_devices]"
                                    % (e.id, which, ref.device), where))
                continue
            chans = dev.rx_channels if which == "rx" else dev.tx_channels
            if ref.channel not in tuple(int(c) for c in chans):
                issues.append(Issue("error", "GEOM_MAP", "element %d %s channel %s:%d is not in the device's "
                                    "%s_channels %s" % (e.id, which, ref.device, ref.channel, which,
                                                        list(chans)), where))
        if e.enabled and e.rx is None and e.tx is None:
            issues.append(Issue("warning", "GEOM_MAP", "element %d is enabled but maps no RX or TX channel"
                                % e.id, "geometry element %d" % e.id))
    used_rx = {e.rx for e in geom.elements if e.enabled and e.rx is not None}
    used_tx = {e.tx for e in geom.elements if e.enabled and e.tx is not None}
    loop = int(_cfg_attr(cfg, "calibration.loopback_rx_chan", -1))
    inj = None
    cal = getattr(cfg, "calibration", None)
    if cal is not None and hasattr(cal, "injection_tx_index"):
        inj = cal.injection_tx_index()
    for k, ref in enumerate(cfg.rx_refs()):
        if ref not in used_rx and k != loop:
            issues.append(Issue("warning", "GEOM_MAP", "global RX %d (%s) is not mapped to any enabled element"
                                % (k, ref), "[active_geometry]"))
    for k, ref in enumerate(cfg.tx_refs()):
        if ref not in used_tx and k != inj:
            issues.append(Issue("warning", "GEOM_MAP", "global TX %d (%s) is not mapped to any enabled element"
                                % (k, ref), "[active_geometry]"))
    return issues


def _load_json(path, cfg):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise _geom_error("GEOMETRY_FILE", "cannot read geometry %s: %s" % (path, exc)) from None
    try:
        d = json.loads(text)
    except ValueError as exc:
        raise _geom_error("GEOMETRY_FILE", "invalid JSON in %s: %s" % (path, exc)) from None
    return geometry_from_dict(d, os.path.dirname(os.path.abspath(path)), cfg)


def _csv_cell(row, key):
    v = row.get(key)
    return "" if v is None else str(v).strip()


def _load_csv(path, cfg):
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise _geom_error("GEOMETRY_FILE", "cannot read geometry %s: %s" % (path, exc)) from None
    lines = text.splitlines()
    units, fd, frame = "m", 0.0, "body"
    if lines and lines[0].lstrip().startswith("#"):
        for tok in lines[0].lstrip()[1:].split():
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            if k == "units":
                units = v
            elif k == "design_freq_hz":
                fd = _finite(v, "design_freq_hz")
            elif k == "frame":
                frame = v
            else:
                raise _geom_error("GEOMETRY", "CSV header line: unknown key %r" % k)
        lines = lines[1:]
    if units not in UNITS:
        raise _geom_error("GEOMETRY", "CSV units must be one of %s, got %r" % (UNITS, units))
    if frame not in ("body", "enu"):
        raise _geom_error("GEOMETRY", "CSV frame must be body or enu, got %r" % (frame,))
    scale = _unit_scale(units, fd, "CSV geometry (header '# units=lambda design_freq_hz=...')")
    if fd <= 0:
        fd = _cfg_design_freq(cfg)
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    cols = [c.strip() for c in (reader.fieldnames or [])]
    missing = {"id", "x_m", "y_m", "z_m"} - set(cols)
    if missing:
        raise _geom_error("GEOMETRY", "CSV missing column(s) %s" % sorted(missing))
    bad = set(cols) - set(CSV_COLUMNS)
    if bad:
        raise _geom_error("GEOMETRY", "CSV unknown column(s) %s" % sorted(bad))
    def_pat = _cfg_default_pattern(cfg)
    def_pol = _cfg_polarization(cfg)
    elements = []
    for k, raw in enumerate(reader):
        row = {(kk or "").strip(): vv for kk, vv in raw.items()}
        where = "CSV row %d" % (k + 1)
        try:
            eid = int(_csv_cell(row, "id"))
        except ValueError:
            raise _geom_error("GEOMETRY", "%s: id must be an integer" % where) from None
        pos = tuple(_finite(_csv_cell(row, c), "%s %s" % (where, c)) * scale for c in ("x_m", "y_m", "z_m"))
        refs = {}
        for which in ("rx", "tx"):
            dev, ch = _csv_cell(row, which + "_device"), _csv_cell(row, which + "_channel")
            if not dev and not ch:
                refs[which] = None
                continue
            if not dev or not ch:
                raise _geom_error("GEOM_MAP", "%s: %s_device and %s_channel must both be set" % (where, which, which))
            try:
                chn = int(ch)
            except ValueError:
                raise _geom_error("GEOM_MAP", "%s: %s_channel must be an int" % (where, which)) from None
            refs[which] = ChannelRef(dev, chn)
        en = _csv_cell(row, "enabled")
        if en not in ("", "0", "1"):
            raise _geom_error("GEOMETRY", "%s: enabled must be 0 or 1" % where)
        pkw = {}
        if _csv_cell(row, "pattern"):
            pkw["kind"] = _csv_cell(row, "pattern")
        if _csv_cell(row, "pattern_q"):
            pkw["q"] = _finite(_csv_cell(row, "pattern_q"), where + " pattern_q")
        if _csv_cell(row, "gain_dbi"):
            pkw["gain_dbi"] = _finite(_csv_cell(row, "gain_dbi"), where + " gain_dbi")
        try:
            pat = replace(def_pat, **pkw) if pkw else def_pat
        except GeometryError as exc:
            raise _geom_error("GEOMETRY", "%s: %s" % (where, exc)) from None
        sig = _csv_cell(row, "pos_sigma_m")
        elements.append(Element(id=eid, pos_body_m=pos, rx=refs["rx"], tx=refs["tx"], pattern=pat,
                                enabled=en != "0", label=_csv_cell(row, "label"),
                                pos_sigma_m=_finite(sig, where + " pos_sigma_m") if sig else 0.0,
                                polarization=def_pol))
    ids = [e.id for e in elements]
    if len(set(ids)) != len(ids):
        raise _geom_error("GEOMETRY", "duplicate element id(s) %s" % sorted({i for i in ids if ids.count(i) > 1}))
    if sorted(ids) != list(range(len(ids))):
        raise _geom_error("GEOMETRY", "element ids must be consecutive from 0, got %s" % sorted(ids))
    rx_order, tx_order = _orders(cfg)
    orientation = Orientation(float(_cfg_attr(cfg, "geometry.heading_deg", 0.0)),
                              float(_cfg_attr(cfg, "geometry.pitch_deg", 0.0)),
                              float(_cfg_attr(cfg, "geometry.roll_deg", 0.0)))
    geom = ArrayGeometry(elements, orientation=orientation,
                         phase_reference=_cfg_attr(cfg, "geometry.phase_reference", "element0"),
                         name=os.path.splitext(os.path.basename(path))[0], design_freq_hz=fd,
                         element_size_m=float(_cfg_attr(cfg, "geometry.element_size_m", 0.0)),
                         frame=frame, rx_order=rx_order, tx_order=tx_order,
                         scan_limit_deg=_cfg_scan_limit(cfg))
    if cfg is not None:
        issues = geometry_map_issues(geom, cfg)
        errs = [i for i in issues if i.level == "error"]
        if errs:
            raise _geom_error("GEOM_MAP", "; ".join(i.message for i in errs), issues)
    return geom


def load_geometry(path: str, cfg=None) -> ArrayGeometry:
    """Load a ``.json`` (canonical) or ``.csv`` geometry file."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        return _load_json(path, cfg)
    if ext == ".csv":
        return _load_csv(path, cfg)
    raise _geom_error("GEOMETRY_FILE", "geometry file must be .json or .csv: %s" % path)


def csv_loss_warnings(geom: ArrayGeometry) -> list:
    """What a CSV export drops (pattern tables, boresight, a-priori data, platform, polarization)."""
    out = []
    for e in geom.elements:
        if e.pattern.kind == "table":
            out.append("element %d: pattern table not representable in CSV" % e.id)
        if e.pattern.boresight_az_deg or e.pattern.boresight_el_deg:
            out.append("element %d: pattern boresight not representable in CSV" % e.id)
        if e.apriori_rx is not None or e.apriori_tx is not None:
            out.append("element %d: a-priori corrections not representable in CSV" % e.id)
    o = geom.orientation
    if o.heading_deg or o.pitch_deg or o.roll_deg or geom.site_lla is not None:
        what = "orientation" if geom.frame == "body" else "attitude (boresight)"
        out.append("platform %s / site not stored in CSV (comes from [active_geometry])" % what)
    if geom.phase_reference != "element0":
        out.append("phase_reference not stored in CSV (comes from [active_geometry])")
    if geom.survey_sigma_m or geom.element_size_m:
        out.append("survey sigma / element size not stored in CSV")
    return out


def save_geometry(geom: ArrayGeometry, path: str) -> None:
    """Write ``.json`` (lossless) or ``.csv`` (lossy: see ``csv_loss_warnings``) atomically."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        ac.atomic_write_text(path, json.dumps(geom.to_dict(), indent=2, sort_keys=False) + "\n")
        return
    if ext != ".csv":
        raise _geom_error("GEOMETRY_FILE", "geometry file must be .json or .csv: %s" % path)
    for w in csv_loss_warnings(geom):
        logger.warning("geometry CSV export is lossy: %s", w)
    buf = io.StringIO()
    buf.write("# units=m design_freq_hz=%r frame=%s\n" % (float(geom.design_freq_hz), geom.frame))
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(CSV_COLUMNS)
    for e in geom.elements:
        p = e.pattern
        wr.writerow([e.id, repr(e.pos_body_m[0]), repr(e.pos_body_m[1]), repr(e.pos_body_m[2]),
                     e.rx.device if e.rx else "", e.rx.channel if e.rx else "",
                     e.tx.device if e.tx else "", e.tx.channel if e.tx else "",
                     1 if e.enabled else 0, e.label,
                     "" if p.kind == "table" else p.kind, repr(float(p.q)),
                     "" if p.gain_dbi is None else repr(float(p.gain_dbi)), repr(float(e.pos_sigma_m))])
    ac.atomic_write_text(path, buf.getvalue())


def geometry_config_issues(cfg) -> list:
    """Provenance checks of the ``[active_geometry]`` preset keys (code GEOMETRY).

    With ``spacing_m = 0`` and ``design_freq_hz = 0`` the preset pitch is
    ``spacing_lambda`` x lambda(center_freq_hz): the physical model then follows
    the operating frequency and the grating check passes by construction. That
    is a warning, an error with ``en_tx = 1``. A UCA whose size is fixed by
    ``radius_m > 0`` is pinned (its pitch does not depend on the frequency).
    """
    g = cfg.geometry
    if g.geometry_file:
        return []
    if (g.spacing_m and g.spacing_m > 0) or (g.design_freq_hz and g.design_freq_hz > 0):
        return []
    if g.preset == "uca" and g.radius_m and g.radius_m > 0:
        return []
    tx_on = bool(_cfg_attr(cfg, "tx.en_tx", False))
    try:
        sp = "%.4g m" % (float(g.spacing_lambda) * ac.wavelength(float(cfg.rf.center_freq_hz)))
    except ValueError:
        sp = "undefined"
    return [Issue("error" if tx_on else "warning", "GEOMETRY",
                  "preset element pitch is derived from the operating frequency (spacing_lambda %g x "
                  "lambda(center_freq_hz) = %s): set spacing_m or design_freq_hz to pin the physical "
                  "geometry%s" % (float(g.spacing_lambda), sp, " (required with en_tx = 1)" if tx_on else ""),
                  "[active_geometry] spacing_m / design_freq_hz")]


def geometry_from_config(cfg, issues=None) -> ArrayGeometry:
    """Geometry from ``[active_geometry]``: the geometry file, else the preset keys.

    Presets map element i to global RX i and TX i (when those indices exist);
    the channel map is validated (GeometryError GEOM_MAP). Provenance findings
    (``geometry_config_issues``) are kept on ``geom.provenance_issues`` (reported
    again by every ``validate_geometry``) and appended to ``issues`` when given;
    the preset pitch actually used is ``geom.preset_spacing_m``.
    """
    g = cfg.geometry
    if g.geometry_file:
        geom = load_geometry(cfg.resolve_path(g.geometry_file), cfg)
        if issues is not None:
            issues.extend(geom.provenance_issues)
        return geom
    prov = geometry_config_issues(cfg)
    fd = float(g.design_freq_hz) if g.design_freq_hz and g.design_freq_hz > 0 else float(cfg.rf.center_freq_hz)
    spacing = float(g.spacing_m) if g.spacing_m and g.spacing_m > 0 else float(g.spacing_lambda) * ac.wavelength(fd)
    if g.preset == "ula":
        body = _ula_body(g.num_elements, spacing, g.axis_az_deg)
    elif g.preset == "ura":
        body = _ura_body(g.nx, g.ny, spacing, spacing, "vertical")
    elif g.preset == "uca":
        body = _uca_body(g.num_elements, g.radius_m if g.radius_m > 0 else None, spacing, 0.0)
        if g.radius_m > 0:                                     # pinned by the radius: the real arc pitch
            spacing = 2.0 * float(g.radius_m) * math.sin(math.pi / len(body))
    elif g.preset == "tri":
        body = _tri_body(g.nx, g.ny, spacing, "vertical")
    else:
        raise _geom_error("GEOMETRY", "unknown preset %r" % g.preset)
    pat = _cfg_default_pattern(cfg)
    pol = _cfg_polarization(cfg)
    els = tuple(Element(id=i, pos_body_m=tuple(b), pattern=pat, polarization=pol,
                        rx=ChannelRef("_", i), tx=ChannelRef("_", i)) for i, b in enumerate(body))
    els = _remap_preset(els, cfg)
    rx_order, tx_order = _orders(cfg)
    geom = ArrayGeometry(els, orientation=Orientation(g.heading_deg, g.pitch_deg, g.roll_deg),
                         phase_reference=g.phase_reference, name="preset-%s" % g.preset,
                         design_freq_hz=fd, element_size_m=g.element_size_m,
                         rx_order=rx_order, tx_order=tx_order, scan_limit_deg=float(g.scan_limit_deg),
                         provenance_issues=prov, preset_spacing_m=spacing)
    if issues is not None:
        issues.extend(prov)
    issues = geometry_map_issues(geom, cfg)
    errs = [i for i in issues if i.level == "error"]
    if errs:
        raise _geom_error("GEOM_MAP", "; ".join(i.message for i in errs), issues)
    return geom


def scan_directions(boresight, scan_limit_deg, step_deg=2.0) -> np.ndarray:
    """Unit directions (P, 3) sampling the cone of half-angle ``scan_limit_deg`` around
    ``boresight`` (boundary included), roughly ``step_deg`` apart."""
    b = np.asarray(boresight, dtype=float)
    b = b / np.linalg.norm(b)
    tmp = np.array([0.0, 0.0, 1.0]) if abs(b[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    e1 = np.cross(b, tmp)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(b, e1)
    step = max(1e-3, float(step_deg))
    lim = max(0.0, min(180.0, float(scan_limit_deg)))
    nth = int(math.ceil(lim / step))
    thetas = np.linspace(0.0, lim, nth + 1) if nth > 0 else np.array([0.0])
    out = []
    for th in thetas:
        t = math.radians(th)
        nphi = 1 if th == 0 else max(4, int(math.ceil(360.0 * math.sin(t) / step)))
        phi = np.linspace(0.0, 2 * np.pi, nphi, endpoint=False)
        out.append(math.cos(t) * b + math.sin(t) * (np.outer(np.cos(phi), e1) + np.outer(np.sin(phi), e2)))
    return np.vstack(out)
