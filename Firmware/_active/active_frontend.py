"""
    HeIMDALL "active" capability: RF front-end amplification model.

    Pure model of the external RF chain around each SDR channel, scaling from a
    bare device port up to amplified systems (driver + PA + isolator + coupler +
    T/R switch on TX, limiter + LNA on RX):

      - ``parse_chain``: the stage grammar
        ``kind:name:gain_db[:nf_db[:p1db_out_dbm[:psat_dbm[:max_in_dbm[:am_pm_deg]]]]]``
        (stages separated by ``;``, ordered from the antenna port to the device
        RX port for RX, from the device TX port to the antenna port for TX);
      - ``RappPA``: Rapp AM/AM + quadratic AM/PM amplifier model, with P1dB
        solved numerically (never assumed);
      - ``RxChain``: gain / Friis noise figure / compression headroom / damage
        input level, reusing ``gain_budget`` from ``_daq_core`` (read-only);
      - ``TxPowerCal``: the conducted-power calibration table (bilinear,
        never extrapolated; outside the table a conservative caps bound);
      - ``TxChain.predict``: per-stage average / PEP propagation with headroom,
        compression and saturation flags;
      - EIRP (worst-case coherent N^2 bound and actual-weight pattern), required
        attenuation, free-space isolation;
      - ``FrontendModel``: every chain of a configuration, the TX->RX isolation
        matrix, RX-protection evaluation (spec 12.6), active reflection from an
        S-matrix and the IQ-header stamp values.

    Rapp P1dB. With the Rapp law ``|out| = G|in| / (1 + (G|in|/A_sat)^(2p))^(1/(2p))``
    the 1 dB compression point is ``Psat + 20 log10(r) - 1 dB`` with
    ``r = (10^(p/10) - 1)^(1/(2p))``: -2.17 dB below Psat at p = 2 (the spec's
    "~1.5 dB" is an approximation of this value). When a stage gives only a
    P1dB, its Psat is derived with this exact offset so the model reproduces the
    datasheet P1dB (and saturates later than a "+1.5 dB" guess would, which is
    the conservative direction for output-power limits). Headroom is measured
    against the lower of the datasheet and the modelled P1dB.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import csv
import dataclasses
import logging
import math
from dataclasses import dataclass

import numpy as np

import active_common as ac
from active_common import ConfigError, GeometryError, Issue

ac.ensure_daq_core_path()
from antenna_profile import AntennaProfile  # noqa: E402  (_daq_core, read-only reuse)
from gain_budget import (P1DB_NOT_MODELLED, friis_nf_db, lookup_lna_response,  # noqa: E402
                         p1db_headroom_db, parse_lna_freq_table, total_system_gain_db)

logger = logging.getLogger("heimdall.active.frontend")

STAGE_KINDS_TX = ("amp", "pa", "passive", "cable", "pad", "coupler", "switch", "isolator", "filter")
STAGE_KINDS_RX = ("limiter", "lna", "amp", "passive", "cable", "pad", "switch", "coupler", "filter")
PASSIVE_KINDS = ("passive", "cable", "pad", "coupler", "switch", "isolator", "filter", "limiter")
AMP_KINDS = ("amp", "pa", "lna")
MAX_STAGE_FIELDS = 8
RAPP_DEFAULT_P = 2.0
ASSUMED_ACTIVE_NF_DB = 6.0       # NF used for an amplifier stage without nf_db (flagged nf_assumed)
SATURATION_TOL_DB = 0.1          # out_pep >= psat - this -> saturated
MAX_HEADER_CHANNELS = 32
NEG_INF = float("-inf")
POS_INF = float("inf")


def _cerr(code, msg, where=""):
    return ConfigError(msg + (" (%s)" % where if where else ""), (Issue("error", code, msg, where),))


def _db(p_w):
    """Watts -> dBm, 0 -> -inf."""
    return NEG_INF if p_w <= 0.0 else 10.0 * math.log10(p_w) + 30.0


def _w(p_dbm):
    """dBm -> watts, -inf -> 0."""
    if p_dbm is None or p_dbm == NEG_INF:
        return 0.0
    return 10.0 ** ((float(p_dbm) - 30.0) / 10.0)


def _lvl(p_dbm):
    """dBm (None / -inf = no signal) -> float dBm."""
    if p_dbm is None:
        return NEG_INF
    v = float(p_dbm)
    if math.isnan(v):
        raise ValueError("NaN power level")
    return v


def coherent_sum_dbm(levels_dbm) -> float:
    """Worst-case (in-phase) sum of levels: ``20 log10 sum 10^(L/20)`` (dBm in, dBm out).

    ``None`` / -inf entries contribute nothing; an empty sum is -inf.
    """
    s = 0.0
    for lv in levels_dbm:
        v = _lvl(lv)
        if v == NEG_INF:
            continue
        if v == POS_INF:
            return POS_INF
        s += 10.0 ** (v / 20.0)
    return NEG_INF if s <= 0.0 else 20.0 * math.log10(s)


def power_sum_dbm(levels_dbm) -> float:
    """Incoherent (power) sum of levels in dBm."""
    return _db(sum(_w(_lvl(v)) for v in levels_dbm))


# ---------------------------------------------------------------------------
# Stages and the chain grammar
# ---------------------------------------------------------------------------
def rapp_p1db_offset_db(p: float = RAPP_DEFAULT_P) -> float:
    """``P1dB_out - Psat`` (dB, negative) of the Rapp law with smoothness ``p``."""
    p = float(p)
    if not p > 0:
        raise ValueError("Rapp p must be > 0")
    r = (10.0 ** (p / 10.0) - 1.0) ** (1.0 / (2.0 * p))
    return 20.0 * math.log10(r) - 1.0


@dataclass(frozen=True)
class Stage:
    """One front-end stage. Passive kinds have ``gain_db <= 0`` and ``nf_db`` = loss."""
    kind: str
    name: str
    gain_db: float
    nf_db: float = None
    p1db_out_dbm: float = None
    psat_dbm: float = None
    max_in_dbm: float = None
    am_pm_deg_at_p1db: float = 0.0
    rapp_p: float = RAPP_DEFAULT_P

    @property
    def is_amp(self) -> bool:
        return self.kind in AMP_KINDS

    @property
    def modelled(self) -> bool:
        """True when the compression of an amplifier is modelled (p1db or psat given)."""
        return self.is_amp and (self.p1db_out_dbm is not None or self.psat_dbm is not None)

    def effective_nf_db(self) -> float:
        if self.nf_db is not None:
            return float(self.nf_db)
        if self.kind in PASSIVE_KINDS:
            return max(0.0, -float(self.gain_db))
        return ASSUMED_ACTIVE_NF_DB

    def effective_psat_dbm(self):
        """Psat of the Rapp model: given, else derived from P1dB (exact Rapp offset), else None."""
        if self.psat_dbm is not None:
            return float(self.psat_dbm)
        if self.p1db_out_dbm is not None:
            return float(self.p1db_out_dbm) - rapp_p1db_offset_db(self.rapp_p)
        return None

    def model(self):
        """``RappPA`` of an amplifier stage with a compression model, else None."""
        psat = self.effective_psat_dbm()
        if not self.is_amp or psat is None:
            return None
        return RappPA(self.gain_db, psat, self.rapp_p, self.am_pm_deg_at_p1db)

    def effective_p1db_dbm(self):
        """P1dB used for headroom: min(datasheet, modelled) (None if not modelled)."""
        m = self.model()
        if m is None:
            return None if self.p1db_out_dbm is None else float(self.p1db_out_dbm)
        mod = m.p1db_out_dbm()
        return mod if self.p1db_out_dbm is None else min(float(self.p1db_out_dbm), mod)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def parse_chain(spec: str, direction: str) -> tuple:
    """Parse a ``;``-separated chain spec (spec 11.1) into a tuple of ``Stage``.

    ``""`` -> ``()`` (bare device). ``ConfigError`` (code ``CHAIN``) on an unknown
    kind for the direction, a missing/non-numeric gain, a positive gain on a
    passive kind, ``psat < p1db``, more than 8 fields (trailing empty fields are
    ignored) or a duplicate stage name.
    """
    if direction not in ("rx", "tx"):
        raise ValueError("direction must be rx or tx, got %r" % (direction,))
    kinds = STAGE_KINDS_TX if direction == "tx" else STAGE_KINDS_RX
    where = "%s_chain" % direction
    stages = []
    names = set()
    for k, ent in enumerate(e.strip() for e in str(spec or "").split(";")):
        if not ent:
            continue
        parts = [p.strip() for p in ent.split(":")]
        while len(parts) > MAX_STAGE_FIELDS and parts[-1] == "":
            parts.pop()
        if len(parts) > MAX_STAGE_FIELDS:
            raise _cerr("CHAIN", "stage %r has %d fields; the grammar is kind:name:gain_db[:nf_db"
                                 "[:p1db_out_dbm[:psat_dbm[:max_in_dbm[:am_pm_deg]]]]]" % (ent, len(parts)),
                        where)
        if len(parts) < 3:
            raise _cerr("CHAIN", "stage %r must be kind:name:gain_db[...]" % ent, where)
        kind = parts[0].lower()
        if kind not in kinds:
            raise _cerr("CHAIN", "stage kind %r is not valid for %s (valid: %s)"
                        % (kind, direction.upper(), ", ".join(kinds)), where)
        name = parts[1] or "%s%d" % (kind, k)
        if name in names:
            raise _cerr("CHAIN", "duplicate stage name %r" % name, where)
        names.add(name)
        nums = []
        for p in parts[2:]:
            if p == "":
                nums.append(None)
                continue
            try:
                v = float(p)
            except ValueError:
                raise _cerr("CHAIN", "stage %r: field %r is not a number" % (ent, p), where) from None
            if not math.isfinite(v):
                raise _cerr("CHAIN", "stage %r: field %r is not finite" % (ent, p), where)
            nums.append(v)
        nums += [None] * (6 - len(nums))
        gain, nf, p1db, psat, max_in, ampm = nums
        if gain is None:
            raise _cerr("CHAIN", "stage %r needs gain_db" % ent, where)
        if kind in PASSIVE_KINDS:
            if gain > 0:
                raise _cerr("CHAIN", "passive stage %r must have gain_db <= 0" % ent, where)
            if p1db is not None or psat is not None:
                raise _cerr("CHAIN", "passive stage %r cannot have p1db/psat" % ent, where)
            if nf is None:
                nf = -gain
        if nf is not None and nf < 0:
            raise _cerr("CHAIN", "stage %r: nf_db must be >= 0" % ent, where)
        if p1db is not None and psat is not None and psat < p1db:
            raise _cerr("CHAIN", "stage %r: psat (%g) < p1db (%g)" % (ent, psat, p1db), where)
        if ampm is not None and kind not in AMP_KINDS:
            raise _cerr("CHAIN", "stage %r: am_pm only applies to amplifiers" % ent, where)
        stages.append(Stage(kind=kind, name=name, gain_db=gain, nf_db=nf, p1db_out_dbm=p1db,
                            psat_dbm=psat, max_in_dbm=max_in,
                            am_pm_deg_at_p1db=0.0 if ampm is None else ampm))
    return tuple(stages)


def chain_to_spec(stages) -> str:
    """Inverse of ``parse_chain`` (round-trips every field)."""
    out = []
    for s in stages:
        f = [s.kind, s.name, repr(float(s.gain_db))]
        tail = [s.nf_db, s.p1db_out_dbm, s.psat_dbm, s.max_in_dbm,
                s.am_pm_deg_at_p1db if s.am_pm_deg_at_p1db else None]
        while tail and tail[-1] is None:
            tail.pop()
        f += ["" if v is None else repr(float(v)) for v in tail]
        out.append(":".join(f))
    return ";".join(out)


# ---------------------------------------------------------------------------
# Rapp amplifier
# ---------------------------------------------------------------------------
class RappPA:
    """Rapp AM/AM with quadratic AM/PM.

    Signals are complex amplitudes in sqrt(W) units (``|v|^2`` = power in W).
    ``|out| = G|v| / (1 + (G|v|/A_sat)^(2p))^(1/(2p))``,
    ``phase += am_pm_deg_at_p1db * (|out| / A_p1db)^2`` (degrees).
    """

    def __init__(self, gain_db, psat_dbm, p=RAPP_DEFAULT_P, am_pm_deg_at_p1db=0.0):
        self.gain_db = float(gain_db)
        self.psat_dbm = float(psat_dbm)
        self.p = float(p)
        self.am_pm_deg_at_p1db = float(am_pm_deg_at_p1db)
        for v, what in ((self.gain_db, "gain_db"), (self.psat_dbm, "psat_dbm"), (self.p, "p"),
                        (self.am_pm_deg_at_p1db, "am_pm_deg_at_p1db")):
            if not math.isfinite(v):
                raise ValueError("RappPA %s must be finite" % what)
        if not self.p > 0:
            raise ValueError("RappPA p must be > 0")
        self._g = 10.0 ** (self.gain_db / 20.0)
        self._a_sat = math.sqrt(_w(self.psat_dbm))
        self._p1db = None
        self._a_p1db = math.sqrt(_w(self.p1db_out_dbm()))

    def _out_amp(self, a):
        a = np.asarray(a, dtype=float)
        x = self._g * a
        with np.errstate(over="ignore"):
            r = (x / self._a_sat) ** (2.0 * self.p)
            return x / (1.0 + r) ** (1.0 / (2.0 * self.p))

    def apply(self, v) -> np.ndarray:
        """Amplify complex amplitudes ``v`` (sqrt(W)); returns complex128 of the same shape."""
        v = np.asarray(v, dtype=complex)
        a = np.abs(v)
        out_a = self._out_amp(a)
        phase = np.angle(v) + np.radians(self.am_pm_deg_at_p1db) * (out_a / self._a_p1db) ** 2
        return out_a * np.exp(1j * phase)

    def output_dbm(self, in_dbm) -> float:
        """Output power (dBm) for a CW input power ``in_dbm`` (-inf -> -inf)."""
        x = _lvl(in_dbm)
        if x == NEG_INF:
            return NEG_INF
        return _db(float(self._out_amp(math.sqrt(_w(x)))) ** 2)

    def am_pm_deg(self, in_dbm) -> float:
        """AM/PM phase shift (deg) at a CW input of ``in_dbm``."""
        x = _lvl(in_dbm)
        if x == NEG_INF:
            return 0.0
        out_a = float(self._out_amp(math.sqrt(_w(x))))
        return self.am_pm_deg_at_p1db * (out_a / self._a_p1db) ** 2

    def compression_db(self, in_dbm) -> float:
        """Gain compression (dB, >= 0) at ``in_dbm``."""
        x = _lvl(in_dbm)
        if x == NEG_INF:
            return 0.0
        return x + self.gain_db - self.output_dbm(x)

    def p1db_in_dbm(self) -> float:
        """Input power at 1 dB gain compression (solved by bisection)."""
        lo = self.psat_dbm - self.gain_db - 80.0
        hi = self.psat_dbm - self.gain_db + 60.0
        for _ in range(200):
            mid = 0.5 * (lo + hi)
            if self.compression_db(mid) < 1.0:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-10:
                break
        return 0.5 * (lo + hi)

    def p1db_out_dbm(self) -> float:
        """Output-referred P1dB (computed numerically from the model)."""
        if self._p1db is None:
            self._p1db = self.output_dbm(self.p1db_in_dbm())
        return self._p1db


# ---------------------------------------------------------------------------
# RX chain
# ---------------------------------------------------------------------------
class RxChain:
    """One receive chain: antenna port -> stages -> device RX port."""

    def __init__(self, stages, device_nf_db, device_max_in_dbm, antenna: AntennaProfile = None,
                 lna_freq_table=None):
        self.stages = tuple(stages)
        for s in self.stages:
            if not isinstance(s, Stage):
                raise TypeError("RxChain stages must be Stage instances")
            if s.kind not in STAGE_KINDS_RX:
                raise _cerr("CHAIN", "stage kind %r is not valid for RX" % s.kind)
        self.device_nf_db = float(device_nf_db)
        self.device_max_in_dbm = None if device_max_in_dbm is None else float(device_max_in_dbm)
        self.antenna = antenna
        self.lna_freq_table = list(lna_freq_table or [])

    @property
    def antenna_gain_dbi(self) -> float:
        return float(self.antenna.element_gain_dbi) if self.antenna is not None else 0.0

    @property
    def feed_loss_db(self) -> float:
        return float(self.antenna.feed_loss_db) if self.antenna is not None else 0.0

    def first_active_stage(self):
        for s in self.stages:
            if s.kind in ("lna", "amp"):
                return s
        return None

    def _effective(self, f_hz, lna_on, lna_off_isolation_db):
        """[(stage, gain_db, nf_db)] with the LNA table and LNA-off substitution applied."""
        out = []
        table_used = False
        iso = float(lna_off_isolation_db or 0.0)
        for s in self.stages:
            g, nf = float(s.gain_db), s.effective_nf_db()
            if s.kind == "lna":
                if self.lna_freq_table and not table_used and f_hz:
                    g, nf = lookup_lna_response(self.lna_freq_table, float(f_hz))
                    table_used = True
                if not lna_on and iso > 0:
                    g, nf = -iso, iso
            out.append((s, g, nf))
        return out

    def ext_gain_db(self, f_hz=0.0, lna_on=True, lna_off_isolation_db=0.0) -> float:
        return float(sum(g for _, g, _ in self._effective(f_hz, lna_on, lna_off_isolation_db)))

    def budget(self, f_hz, input_dbm, rx_gain_db, lna_on=True, lna_off_isolation_db=0.0,
               p1db_margin_db=0.0) -> dict:
        """Link budget at ``f_hz`` for ``input_dbm`` at the chain input (antenna port).

        ``total_gain_db`` = ``gain_budget.total_system_gain_db`` (antenna gain -
        feed loss + external gain + RX gain); ``nf_db`` = ``gain_budget.friis_nf_db``
        over [feed loss] + stages + (rx_gain, device NF); ``headroom_db`` per
        amplifier (None when not modelled); compression is advisory.
        """
        eff = self._effective(f_hz, lna_on, lna_off_isolation_db)
        ext = float(sum(g for _, g, _ in eff))
        rx_gain = float(rx_gain_db)
        total = total_system_gain_db(self.antenna_gain_dbi, self.feed_loss_db, ext, rx_gain)
        cascade = []
        if self.feed_loss_db > 0:
            cascade.append((-self.feed_loss_db, self.feed_loss_db))
        cascade += [(g, nf) for _, g, nf in eff]
        cascade.append((rx_gain, self.device_nf_db))
        nf_total = friis_nf_db(cascade)
        nf_assumed = any(s.is_amp and s.nf_db is None and not (s.kind == "lna" and self.lna_freq_table)
                         for s in self.stages)
        headroom = []
        stages_out = []
        gain_upto = 0.0
        x = _lvl(input_dbm)
        for s, g, nf in eff:
            gain_upto += g
            h = None
            if s.is_amp:
                p1 = s.effective_p1db_dbm()
                if p1 is not None and not (s.kind == "lna" and not lna_on and lna_off_isolation_db > 0):
                    h = p1db_headroom_db(x, gain_upto, p1)
                headroom.append(h)
            stages_out.append({"name": s.name, "kind": s.kind, "gain_db": g, "nf_db": nf,
                               "out_dbm": x + gain_upto, "headroom_db": h})
        margin = float(p1db_margin_db or 0.0)
        compressed = any(h is not None and h < margin for h in headroom)
        dmg = self.damage_input_dbm(lna_on, lna_off_isolation_db)
        return {"total_gain_db": total, "ext_gain_db": ext, "nf_db": nf_total, "nf_assumed": nf_assumed,
                "headroom_db": headroom, "compressed": compressed, "damage_input_dbm": dmg,
                "damage_margin_db": dmg - x, "stages": stages_out,
                "p1db_not_modelled": P1DB_NOT_MODELLED}

    def damage_input_dbm(self, lna_on=True, lna_off_isolation_db=0.0) -> float:
        """Damage level referred to the chain input: min over stages (and the device
        port) of ``max_in - sum(gain before the stage)``. With the LNA off its gain
        is replaced by ``-lna_off_isolation_db`` (0 = no credit: the gain is kept);
        its own ``max_in`` still applies."""
        iso = float(lna_off_isolation_db or 0.0)
        before = 0.0
        best = POS_INF
        for s in self.stages:
            if s.max_in_dbm is not None:
                best = min(best, float(s.max_in_dbm) - before)
            g = float(s.gain_db)
            if s.kind == "lna":
                if not lna_on and iso > 0:
                    g = -iso
                elif self.lna_freq_table:
                    # frequency independent bound: the highest gain the table can give
                    g = max(g, max(t[1] for t in self.lna_freq_table))
            before += g
        if self.device_max_in_dbm is not None:
            best = min(best, self.device_max_in_dbm - before)
        return best

    def safe_input_dbm(self, margin_db, lna_on=True, lna_off_isolation_db=0.0) -> float:
        return self.damage_input_dbm(lna_on, lna_off_isolation_db) - float(margin_db)


# ---------------------------------------------------------------------------
# TX power calibration table
# ---------------------------------------------------------------------------
def _per_chan(value, chan):
    if isinstance(value, dict):
        return value.get(int(chan))
    if isinstance(value, (list, tuple)):
        c = int(chan)
        return value[c] if 0 <= c < len(value) else None
    return value


class TxPowerCal:
    """Conducted TX power at the device port for full-scale CW (``|s| = 1``).

    ``rows``: ``(chan, freq_hz, tx_gain_db, p_fs_dbm)`` with ``chan`` the global
    TX index. Interpolated bilinearly over (freq, gain) and NEVER extrapolated.
    Outside the table ``fs_dbm`` returns the conservative bound
    ``upper - (gain_max - g)`` (``upper`` = ``max_dbm_upper`` or
    ``caps.tx_max_dbm_upper``; ``g`` clamped into the caps gain range; never
    above ``upper``). ``caps`` / ``max_dbm_upper`` may be a single value, a
    list indexed by chan or a ``{chan: value}`` dict.
    """

    def __init__(self, rows=None, caps=None, max_dbm_upper=None):
        self.caps = caps
        self.max_dbm_upper = max_dbm_upper
        norm = []
        seen = set()
        for r in rows or ():
            if len(r) != 4:
                raise _cerr("POWER_CAL", "power-cal rows are (chan, freq_hz, tx_gain_db, p_fs_dbm): %r" % (r,))
            c = int(r[0])
            f, g, p = float(r[1]), float(r[2]), float(r[3])
            if c < 0 or not all(math.isfinite(v) for v in (f, g, p)) or f <= 0:
                raise _cerr("POWER_CAL", "invalid power-cal row %r" % (r,))
            key = (c, f, g)
            if key in seen:
                raise _cerr("POWER_CAL", "duplicate power-cal row for chan %d, %g Hz, %g dB" % key)
            seen.add(key)
            norm.append((c, f, g, p))
        self.rows = tuple(sorted(norm))
        self.calibrated = bool(self.rows)
        self._tab = {}
        for c, f, g, p in self.rows:
            self._tab.setdefault(c, {}).setdefault(f, []).append((g, p))
        for c in self._tab:
            for f in self._tab[c]:
                self._tab[c][f].sort()

    # --- conservative bound ------------------------------------------------------
    def upper_dbm(self, chan) -> float:
        caps = _per_chan(self.caps, chan)
        ov = _per_chan(self.max_dbm_upper, chan)
        up = None if caps is None else float(caps.tx_max_dbm_upper)
        if ov is not None:
            up = float(ov) if up is None else min(up, float(ov))
        if up is None:
            raise ValueError("no conservative TX power bound for chan %d (no caps)" % int(chan))
        return up

    def conservative_dbm(self, chan, tx_gain_db) -> float:
        up = self.upper_dbm(chan)
        caps = _per_chan(self.caps, chan)
        if caps is None:
            return up
        gmin, gmax = float(caps.tx_gain_range_db[0]), float(caps.tx_gain_range_db[1])
        g = min(max(float(tx_gain_db), gmin), gmax)
        return min(up, up - (gmax - g))

    # --- table -------------------------------------------------------------------
    def _gain_interp(self, curve, g):
        gs = [c[0] for c in curve]
        if g < gs[0] - 1e-9 or g > gs[-1] + 1e-9:
            return None
        if len(curve) == 1:
            return curve[0][1]
        g = min(max(g, gs[0]), gs[-1])
        for (g0, p0), (g1, p1) in zip(curve[:-1], curve[1:]):
            if g0 <= g <= g1:
                t = 0.0 if g1 == g0 else (g - g0) / (g1 - g0)
                return p0 + t * (p1 - p0)
        return None

    def _lookup(self, chan, f_hz, g):
        tab = self._tab.get(int(chan))
        if not tab:
            return None
        freqs = sorted(tab)
        f = float(f_hz)
        tol = 1e-6 * max(1.0, abs(f))
        if f < freqs[0] - tol or f > freqs[-1] + tol:
            return None
        for fk in freqs:
            if abs(fk - f) <= tol:
                return self._gain_interp(tab[fk], g)
        for f0, f1 in zip(freqs[:-1], freqs[1:]):
            if f0 < f < f1:
                p0 = self._gain_interp(tab[f0], g)
                p1 = self._gain_interp(tab[f1], g)
                if p0 is None or p1 is None:
                    return None
                t = (f - f0) / (f1 - f0)
                return p0 + t * (p1 - p0)
        return None

    def in_range(self, chan, f_hz, tx_gain_db) -> bool:
        return self._lookup(chan, f_hz, float(tx_gain_db)) is not None

    def covers(self, chan, f_hz, g_lo, g_hi) -> bool:
        """True when every gain in ``[g_lo, g_hi]`` at ``f_hz`` is inside the table."""
        return self.in_range(chan, f_hz, g_lo) and self.in_range(chan, f_hz, g_hi)

    def fs_dbm(self, chan, f_hz, tx_gain_db) -> float:
        v = self._lookup(chan, f_hz, float(tx_gain_db))
        if v is not None:
            return float(v)
        return self.conservative_dbm(chan, tx_gain_db)

    def chans(self) -> list:
        return sorted(self._tab)

    @staticmethod
    def load(path, caps=None, max_dbm_upper=None) -> "TxPowerCal":
        return TxPowerCal(load_tx_power_cal_rows(path), caps, max_dbm_upper)


def _csv_rows(path):
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError as exc:
        raise _cerr("FILE_MISSING", "cannot read %s: %s" % (path, exc), path) from None
    return list(csv.reader(lines))


def load_tx_power_cal_rows(path) -> list:
    """Read a ``chan,freq_hz,tx_gain_db,p_fs_dbm`` CSV (spec 6.4)."""
    rows = _csv_rows(path)
    if not rows:
        raise _cerr("POWER_CAL", "empty power-cal table %s" % path, path)
    header = [h.strip().lower() for h in rows[0]]
    want = ["chan", "freq_hz", "tx_gain_db", "p_fs_dbm"]
    if header != want:
        raise _cerr("POWER_CAL", "power-cal header must be %s, got %s" % (",".join(want), ",".join(header)),
                    path)
    out = []
    for k, r in enumerate(rows[1:], start=2):
        if len(r) != 4:
            raise _cerr("POWER_CAL", "%s line %d: expected 4 columns" % (path, k), path)
        try:
            out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3])))
        except ValueError:
            raise _cerr("POWER_CAL", "%s line %d: non-numeric value" % (path, k), path) from None
    return out


# ---------------------------------------------------------------------------
# TX chain prediction
# ---------------------------------------------------------------------------
@dataclass
class TxPrediction:
    device_avg_dbm: float
    device_pep_dbm: float
    out_avg_dbm: float
    out_pep_dbm: float
    headroom_db: float
    limiting_stage: str
    compressed: bool
    saturated: bool
    stage_levels: list
    am_pm_deg: float = 0.0               # AM/PM phase at PEP summed over the amplifiers
    p_fs_dbm: float = 0.0                # full-scale CW power used at the device port
    power_calibrated: bool = False       # p_fs from the measured table (not the bound)
    max_in_exceeded: str = ""            # name of a stage whose max_in the PEP exceeds
    unmodelled: tuple = ()               # amplifier stages without p1db/psat (linear)

    def to_dict(self) -> dict:
        return ac.to_jsonable(self)


class TxChain:
    """One transmit chain: device TX port -> stages -> antenna port."""

    def __init__(self, chan: int, stages, power_cal: TxPowerCal):
        self.chan = int(chan)
        self.stages = tuple(stages)
        for s in self.stages:
            if not isinstance(s, Stage):
                raise TypeError("TxChain stages must be Stage instances")
            if s.kind not in STAGE_KINDS_TX:
                raise _cerr("CHAIN", "stage kind %r is not valid for TX" % s.kind)
        self.power_cal = power_cal if power_cal is not None else TxPowerCal()
        self._models = {s.name: s.model() for s in self.stages}

    @property
    def has_pa(self) -> bool:
        return any(s.kind == "pa" for s in self.stages)

    @property
    def gain_db(self) -> float:
        return float(sum(s.gain_db for s in self.stages))

    def predict(self, f_hz, tx_gain_db, dig_rms, dig_peak, papr_margin_db=0.0,
                pa_backoff_db=1.0) -> TxPrediction:
        """Average and PEP at every stage (spec 11.2).

        Device port: ``P_fs + 20 log10(dig_rms)`` / ``+ 20 log10(dig_peak)``.
        ``papr_margin_db`` raises the PEP (tracked through the chain) and drives
        each amplifier's average-power compression (subtracted again).
        ``headroom_db`` = min over modelled amplifiers of ``P1dB - out_pep``;
        ``compressed`` = headroom < ``pa_backoff_db``; ``saturated`` = some
        amplifier's out_pep >= psat - 0.1 dB.
        """
        rms = float(dig_rms)
        pk = float(dig_peak)
        if not (math.isfinite(rms) and math.isfinite(pk)) or rms < 0 or pk < 0:
            raise ValueError("dig_rms / dig_peak must be finite and >= 0")
        margin = float(papr_margin_db or 0.0)
        p_fs = self.power_cal.fs_dbm(self.chan, f_hz, tx_gain_db)
        cal = self.power_cal.in_range(self.chan, f_hz, tx_gain_db)
        avg = NEG_INF if rms == 0 else p_fs + 20.0 * math.log10(rms)
        pep = NEG_INF if pk == 0 else p_fs + 20.0 * math.log10(pk) + margin
        dev_avg, dev_pep = avg, pep
        headroom, limiting = POS_INF, ""
        saturated = False
        am_pm = 0.0
        over = ""
        unmodelled = []
        levels = []
        for s in self.stages:
            in_avg, in_pep = avg, pep
            h = None
            if s.max_in_dbm is not None and in_pep > float(s.max_in_dbm) + 1e-9 and not over:
                over = s.name
            if s.is_amp:
                m = self._models.get(s.name)
                if m is None:
                    unmodelled.append(s.name)
                    avg = in_avg + s.gain_db
                    pep = in_pep + s.gain_db
                else:
                    avg = NEG_INF if in_avg == NEG_INF else m.output_dbm(in_avg + margin) - margin
                    pep = m.output_dbm(in_pep)
                    am_pm += m.am_pm_deg(in_pep)
                    p1 = s.effective_p1db_dbm()
                    h = p1 - pep if pep != NEG_INF else POS_INF
                    if h < headroom:
                        headroom, limiting = h, s.name
                    if pep != NEG_INF and pep >= m.psat_dbm - SATURATION_TOL_DB:
                        saturated = True
            else:
                avg = in_avg + s.gain_db
                pep = in_pep + s.gain_db
            levels.append({"name": s.name, "kind": s.kind, "in_avg_dbm": in_avg, "in_pep_dbm": in_pep,
                           "out_avg_dbm": avg, "out_pep_dbm": pep, "headroom_db": h})
        return TxPrediction(device_avg_dbm=dev_avg, device_pep_dbm=dev_pep, out_avg_dbm=avg,
                            out_pep_dbm=pep, headroom_db=headroom, limiting_stage=limiting,
                            compressed=headroom < float(pa_backoff_db), saturated=saturated,
                            stage_levels=levels, am_pm_deg=am_pm, p_fs_dbm=p_fs,
                            power_calibrated=cal, max_in_exceeded=over,
                            unmodelled=tuple(unmodelled))

    def drive_backoff_db(self, f_hz, tx_gain_db, dig_rms, dig_peak, papr_margin_db=0.0) -> float:
        """PA backoff (headroom, dB) at a drive level: the TX-cal ``drive_level_db``."""
        return self.predict(f_hz, tx_gain_db, dig_rms, dig_peak, papr_margin_db).headroom_db


# ---------------------------------------------------------------------------
# EIRP, attenuation, isolation
# ---------------------------------------------------------------------------
def eirp_upper_bound_dbm(p_dbm, g_dbi) -> float:
    """Worst-case coherent EIRP ``20 log10 sum_n sqrt(P_n[mW] G_n)`` (dBm).

    ``p_dbm``: per-chain power (None / -inf = idle); ``g_dbi``: scalar or per
    chain (net element gain including feed loss). N equal chains give
    ``P + G + 20 log10 N`` (the N^2 rule).
    """
    p = list(p_dbm) if np.ndim(p_dbm) else [p_dbm]
    g = list(g_dbi) if np.ndim(g_dbi) else [g_dbi] * len(p)
    if len(g) != len(p):
        raise ValueError("p_dbm and g_dbi lengths differ")
    return coherent_sum_dbm([NEG_INF if _lvl(pi) == NEG_INF else _lvl(pi) + float(gi)
                             for pi, gi in zip(p, g)])


def eirp_pattern_dbm(pos, t, p_dbm, g_dbi, f_hz, U) -> np.ndarray:
    """EIRP (dBm) toward ENU direction(s) ``U`` for TX weights ``t`` with the chain
    powers ``p_dbm`` (isotropic element pattern of gain ``g_dbi``).

    Element n radiates ``sqrt(P_n G_n) exp(j arg t_n)``; the field is
    ``sum_n a_n(u) ...`` with ``a_n = exp(+j k0 u.r_n)`` (``AF_tx = a^T t``).
    """
    pos = np.asarray(pos, dtype=float).reshape(-1, 3)
    t = np.asarray(t, dtype=complex).reshape(-1)
    n = pos.shape[0]
    if t.size != n:
        raise ValueError("weights and positions differ in length")
    p = np.array([_w(_lvl(v)) * 1e3 for v in (p_dbm if np.ndim(p_dbm) else [p_dbm] * n)])
    g = np.asarray(g_dbi, dtype=float)
    g = np.full(n, float(g)) if g.ndim == 0 else g.reshape(-1)
    amp = np.sqrt(p * 10.0 ** (g / 10.0))
    amp = np.where(np.abs(t) > 0, amp, 0.0)
    ph = np.exp(1j * np.angle(t))
    U = np.asarray(U, dtype=float)
    A = np.exp(1j * ac.k0(f_hz) * (U @ pos.T))
    field_ = A @ (amp * ph)
    with np.errstate(divide="ignore"):
        out = 20.0 * np.log10(np.abs(field_))
    return float(out) if np.ndim(out) == 0 else out


def required_attenuation_db(p_src_dbm, p_safe_dbm, margin_db) -> float:
    """Attenuation needed so ``p_src`` arrives ``margin_db`` below ``p_safe`` (>= 0)."""
    return max(0.0, float(p_src_dbm) - float(p_safe_dbm) + float(margin_db))


def free_space_isolation_db(d_m, f_hz, g_t_dbi=0.0, g_r_dbi=0.0) -> float:
    """Friis port-to-port isolation ``20 log10(4 pi d / lambda) - G_t - G_r`` (>= 0 dB)."""
    d = float(d_m)
    if not d > 0:
        raise ValueError("distance must be > 0")
    fspl = 20.0 * math.log10(4.0 * math.pi * d / ac.wavelength(f_hz))
    return max(0.0, fspl - float(g_t_dbi) - float(g_r_dbi))


# ---------------------------------------------------------------------------
# Auxiliary file loaders
# ---------------------------------------------------------------------------
def load_isolation_file(path, n_rx, n_tx) -> np.ndarray:
    """(n_rx x n_tx) dB isolation CSV (``#`` comments, optional non-numeric header row)."""
    rows = _csv_rows(path)
    vals = []
    for k, r in enumerate(rows):
        try:
            vals.append([float(c) for c in r if c.strip() != ""])
        except ValueError:
            if k == 0:
                continue
            raise _cerr("ISOLATION", "%s: non-numeric isolation value in row %d" % (path, k + 1),
                        path) from None
    M = np.array(vals, dtype=float) if vals else np.zeros((0, 0))
    if M.shape != (int(n_rx), int(n_tx)):
        raise _cerr("ISOLATION", "%s: isolation matrix is %s, expected %d x %d (n_rx x n_tx)"
                    % (path, "x".join(str(s) for s in M.shape), n_rx, n_tx), path)
    if not np.all(np.isfinite(M)) or np.any(M < 0):
        raise _cerr("ISOLATION", "%s: isolation values must be finite and >= 0 dB" % path, path)
    return M


def load_s_matrix(path, n_tx) -> np.ndarray:
    """(n_tx x n_tx) complex S-matrix from ``.npy`` or a ``m,n,re,im`` CSV (spec 6.5)."""
    n_tx = int(n_tx)
    if str(path).lower().endswith(".npy"):
        try:
            S = np.load(path, allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise _cerr("S_MATRIX", "cannot read %s: %s" % (path, exc), path) from None
        S = np.asarray(S, dtype=complex)
    else:
        S = np.zeros((n_tx, n_tx), dtype=complex)
        seen = set()
        for k, r in enumerate(_csv_rows(path)):
            if len(r) != 4:
                raise _cerr("S_MATRIX", "%s: rows are m,n,re,im" % path, path)
            try:
                m, n, re_, im_ = int(r[0]), int(r[1]), float(r[2]), float(r[3])
            except ValueError:
                if k == 0:
                    continue
                raise _cerr("S_MATRIX", "%s: non-numeric row %d" % (path, k + 1), path) from None
            if not (0 <= m < n_tx and 0 <= n < n_tx):
                raise _cerr("S_MATRIX", "%s: index (%d, %d) outside %d x %d" % (path, m, n, n_tx, n_tx),
                            path)
            if (m, n) in seen:
                raise _cerr("S_MATRIX", "%s: duplicate entry (%d, %d)" % (path, m, n), path)
            seen.add((m, n))
            S[m, n] = complex(re_, im_)
    if S.shape != (n_tx, n_tx):
        raise _cerr("S_MATRIX", "%s: S-matrix is %s, expected %d x %d" % (path, S.shape, n_tx, n_tx), path)
    if not np.all(np.isfinite(S)):
        raise _cerr("S_MATRIX", "%s: non-finite S-matrix entry" % path, path)
    return S


# ---------------------------------------------------------------------------
# Whole front end
# ---------------------------------------------------------------------------
def _mask_bits(mask, n):
    if mask is None:
        return [True] * n
    if isinstance(mask, (int, np.integer)) and not isinstance(mask, bool):
        return [bool((int(mask) >> m) & 1) for m in range(n)]
    vals = list(mask)
    if len(vals) == 1:
        return [bool(vals[0])] * n
    if len(vals) != n:
        raise ValueError("lna_on_mask has %d entries for %d channels" % (len(vals), n))
    return [bool(v) for v in vals]


class FrontendModel:
    """Every RX/TX chain of a configuration plus the coupling model.

    ``isolation_db`` is the (n_rx, n_tx) TX->RX isolation in the operating
    (antenna) wiring at ``design_freq_hz``: same element -> ``tr_isolation_db``;
    other pairs -> the isolation file, else ``tx_rx_isolation_db``, else
    free-space from the geometry, else 0 dB (unknown = worst case). The
    loopback reference RX (``loopback_rx_chan``) couples through the coupler
    path (``loopback_path_loss_db``).
    """

    def __init__(self, rx, tx, isolation_db=None, s_matrix=None, *, same_element=None,
                 rx_pos=None, tx_pos=None, rx_gain_dbi=None, tx_gain_dbi=None, feed_loss_db=0.0,
                 tr_isolation_db=0.0, has_tr_switch=False, tx_rx_isolation_db=0.0,
                 isolation_file=None, cabled_rx_path_loss_db=(), loopback_path_loss_db=(),
                 loopback_rx_chan=-1, rx_safe_margin_db=10.0, lna_off_isolation_db=0.0,
                 lna_controllable=False, protect_passive_chain=True, passive_isolation_db=0.0,
                 passive_rx_damage_dbm=10.0, expected_input_dbm=-60.0, p1db_margin_db=3.0,
                 design_freq_hz=915e6, build_issues=()):
        self.rx = list(rx)
        self.tx = list(tx)
        n_rx, n_tx = len(self.rx), len(self.tx)
        self.n_rx, self.n_tx = n_rx, n_tx
        self.same_element = (np.zeros((n_rx, n_tx), dtype=bool) if same_element is None
                             else np.asarray(same_element, dtype=bool).reshape(n_rx, n_tx))
        self.rx_pos = None if rx_pos is None else [None if p is None else np.asarray(p, float) for p in rx_pos]
        self.tx_pos = None if tx_pos is None else [None if p is None else np.asarray(p, float) for p in tx_pos]
        self.rx_gain_dbi = [0.0] * n_rx if rx_gain_dbi is None else [float(g) for g in rx_gain_dbi]
        self.tx_gain_dbi = [0.0] * n_tx if tx_gain_dbi is None else [float(g) for g in tx_gain_dbi]
        self.feed_loss_db = float(feed_loss_db)
        self.tr_isolation_db = float(tr_isolation_db)
        self.has_tr_switch = bool(has_tr_switch)
        self.tx_rx_isolation_db = float(tx_rx_isolation_db)
        self.isolation_file = None if isolation_file is None else np.asarray(isolation_file, float)
        self.cabled_rx_path_loss_db = list(cabled_rx_path_loss_db) or [0.0] * n_tx
        self.loopback_path_loss_db = list(loopback_path_loss_db) or [0.0] * n_tx
        self.loopback_rx_chan = int(loopback_rx_chan)
        self.rx_safe_margin_db = float(rx_safe_margin_db)
        self.lna_off_isolation_db = float(lna_off_isolation_db)
        self.lna_controllable = bool(lna_controllable)
        self.protect_passive_chain = bool(protect_passive_chain)
        self.passive_isolation_db = float(passive_isolation_db)
        self.passive_rx_damage_dbm = float(passive_rx_damage_dbm)
        self.expected_input_dbm = float(expected_input_dbm)
        self.p1db_margin_db = float(p1db_margin_db)
        self.design_freq_hz = float(design_freq_hz)
        self.build_issues = list(build_issues)
        self.geometry = None
        self.s_matrix = None if s_matrix is None else np.asarray(s_matrix, dtype=complex)
        if self.s_matrix is not None and self.s_matrix.shape != (n_tx, n_tx):
            raise _cerr("S_MATRIX", "S-matrix must be %d x %d" % (n_tx, n_tx))
        self.isolation_db = (np.asarray(isolation_db, dtype=float).reshape(n_rx, n_tx)
                             if isolation_db is not None else self.isolation_at(self.design_freq_hz))

    # --- construction from a config ------------------------------------------------
    @staticmethod
    def from_config(cfg, geometry=None) -> "FrontendModel":
        import active_devices as adev  # local: keeps the module import light
        fe = cfg.frontend
        rx_refs, tx_refs = cfg.rx_refs(), cfg.tx_refs()
        n_rx, n_tx = len(rx_refs), len(tx_refs)
        issues = []
        if geometry is None:
            try:
                import active_geometry as ageo
                geometry = ageo.geometry_from_config(cfg)
            except (GeometryError, ConfigError, ValueError) as exc:
                issues.append(Issue("warning", "FRONTEND_GEOMETRY",
                                    "geometry unavailable (%s): TX->RX coupling between elements "
                                    "falls back to tx_rx_isolation_db (0 = worst case) and EIRP "
                                    "uses the antenna-profile gain" % exc))
        prof = cfg.antenna_profile
        feed = float(prof.feed_loss_db) if prof is not None else 0.0
        base_gain = float(prof.element_gain_dbi) if prof is not None else 0.0
        rx_pos, tx_pos = [None] * n_rx, [None] * n_tx
        rx_gain, tx_gain = [base_gain] * n_rx, [base_gain] * n_tx
        rx_elem, tx_elem = [None] * n_rx, [None] * n_tx
        if geometry is not None:
            for which, refs, pos_out, gain_out, el_out in (("rx", rx_refs, rx_pos, rx_gain, rx_elem),
                                                          ("tx", tx_refs, tx_pos, tx_gain, tx_elem)):
                els = geometry.elements_for(which)
                P = geometry.positions(which)
                for k, e in enumerate(els):
                    ref = getattr(e, which)
                    if ref in refs:
                        i = refs.index(ref)
                        pos_out[i] = P[k]
                        gain_out[i] = float(e.pattern.effective_gain_dbi())
                        el_out[i] = e.id
            # unmapped TX chains radiate through an unknown antenna: assume the best one
            mapped = [g for g, e in zip(tx_gain, tx_elem) if e is not None]
            if mapped:
                tx_gain = [g if e is not None else max(mapped) for g, e in zip(tx_gain, tx_elem)]
        same = np.zeros((n_rx, n_tx), dtype=bool)
        for m in range(n_rx):
            for n in range(n_tx):
                same[m, n] = rx_elem[m] is not None and rx_elem[m] == tx_elem[n]
        lna_table = parse_lna_freq_table(fe.lna_freq_table) if fe.en_frontend else []
        rx = []
        for i, ref in enumerate(rx_refs):
            caps = adev.get_caps(cfg.device(ref.device).family)
            ant = AntennaProfile(name=getattr(prof, "name", "geometry") if prof else "geometry",
                                 element_gain_dbi=rx_gain[i],
                                 cable_loss_db=float(prof.cable_loss_db) if prof else 0.0,
                                 connector_loss_db=float(prof.connector_loss_db) if prof else 0.0)
            rx.append(RxChain(parse_chain(fe.rx_chain_spec(i), "rx"), fe.device_nf_db,
                              caps.rx_max_input_dbm, ant, lna_table))
        caps_map = {n: adev.get_caps(cfg.device(r.device).family) for n, r in enumerate(tx_refs)}
        upper_map = {n: cfg.device(r.device).max_tx_dbm_override for n, r in enumerate(tx_refs)
                     if cfg.device(r.device).max_tx_dbm_override is not None}
        rows = load_tx_power_cal_rows(cfg.resolve_path(fe.tx_power_cal_file)) if fe.tx_power_cal_file else ()
        pcal = TxPowerCal(rows, caps_map, upper_map or None)
        tx = [TxChain(n, parse_chain(fe.tx_chain_spec(n), "tx"), pcal) for n in range(n_tx)]
        iso_file = None
        if fe.tx_rx_isolation_file:
            iso_file = load_isolation_file(cfg.resolve_path(fe.tx_rx_isolation_file), n_rx, n_tx)
        S = load_s_matrix(cfg.resolve_path(fe.s_matrix_file), n_tx) if fe.s_matrix_file else None
        rc = cfg.rf_control
        tx_cfg = cfg.tx
        model = FrontendModel(
            rx, tx, None, S, same_element=same, rx_pos=rx_pos, tx_pos=tx_pos,
            rx_gain_dbi=[g - feed for g in rx_gain], tx_gain_dbi=[g - feed for g in tx_gain],
            feed_loss_db=feed, tr_isolation_db=fe.tr_isolation_db,
            has_tr_switch=rc.tr_switch_line.strip().lower() != "none",
            tx_rx_isolation_db=fe.tx_rx_isolation_db, isolation_file=iso_file,
            cabled_rx_path_loss_db=list(tx_cfg.cabled_rx_path_loss_db) or [0.0] * n_tx,
            loopback_path_loss_db=list(tx_cfg.loopback_path_loss_db) or [0.0] * n_tx,
            loopback_rx_chan=cfg.calibration.loopback_rx_chan,
            rx_safe_margin_db=tx_cfg.rx_safe_margin_db, lna_off_isolation_db=fe.lna_off_isolation_db,
            lna_controllable=rc.lna_bias_line.strip().lower() != "none",
            protect_passive_chain=tx_cfg.protect_passive_chain,
            passive_isolation_db=tx_cfg.passive_isolation_db,
            passive_rx_damage_dbm=tx_cfg.passive_rx_damage_dbm,
            expected_input_dbm=fe.expected_input_dbm, p1db_margin_db=fe.p1db_margin_db,
            design_freq_hz=cfg.rf.center_freq_hz, build_issues=issues)
        model.geometry = geometry
        return model

    # --- isolation ---------------------------------------------------------------
    def isolation_at(self, f_hz) -> np.ndarray:
        """(n_rx, n_tx) operating-wiring isolation (dB) at ``f_hz``."""
        n_rx, n_tx = self.n_rx, self.n_tx
        if self.isolation_file is not None:
            iso = self.isolation_file.copy()
        else:
            iso = np.zeros((n_rx, n_tx))
            for m in range(n_rx):
                for n in range(n_tx):
                    if self.same_element[m, n]:
                        iso[m, n] = self.tr_isolation_db
                    elif self.tx_rx_isolation_db > 0:
                        iso[m, n] = self.tx_rx_isolation_db
                    elif (self.rx_pos is not None and self.tx_pos is not None
                          and self.rx_pos[m] is not None and self.tx_pos[n] is not None):
                        d = float(np.linalg.norm(self.rx_pos[m] - self.tx_pos[n]))
                        iso[m, n] = (free_space_isolation_db(d, f_hz, self.tx_gain_dbi[n],
                                                             self.rx_gain_dbi[m]) if d > 0 else 0.0)
                    else:
                        iso[m, n] = 0.0
        lb = self.loopback_rx_chan
        if 0 <= lb < n_rx:
            for n in range(n_tx):
                if self.loopback_path_loss_db[n] > 0:
                    iso[lb, n] = float(self.loopback_path_loss_db[n])
        return iso

    def rx_limits_dbm(self, lna_on=True) -> list:
        """Per RX port: damage level (chain input) minus ``rx_safe_margin_db``."""
        lna_eff = bool(lna_on) or not self.lna_controllable
        return [c.damage_input_dbm(lna_eff, self.lna_off_isolation_db) - self.rx_safe_margin_db
                for c in self.rx]

    def rx_protection(self, tx_pep_dbm: list, mode: str, lna_on: bool, tr_state: str,
                      f_hz=None) -> tuple:
        """RX protection (spec 12.6): ``(issues, worst level per RX dBm, limit per RX dBm)``.

        ``tx_pep_dbm``: PEP at each global TX chain output (None / -inf = idle).
        ``mode``: radiate | dummy_load | cabled | loopback. ``tr_state``: the T/R
        position during the transmission (``rx`` removes the same-element T/R
        isolation when a T/R switch is fitted). ``lna_on`` is ignored (treated as
        on) when the LNA bias is not controllable.
        """
        peps = [(_lvl(p) if p is not None else NEG_INF) for p in tx_pep_dbm]
        if len(peps) != self.n_tx:
            raise ValueError("tx_pep_dbm has %d entries for %d TX chains" % (len(peps), self.n_tx))
        if mode not in ("radiate", "dummy_load", "cabled", "loopback"):
            raise ValueError("unknown RX-protection mode %r" % (mode,))
        iso = self.isolation_at(self.design_freq_hz if f_hz is None else f_hz)
        if self.has_tr_switch and tr_state == "rx":
            iso = np.where(self.same_element, 0.0, iso)
        if mode == "cabled":
            for n in range(min(self.n_tx, self.n_rx)):
                if self.cabled_rx_path_loss_db[n] > 0:
                    iso[n, n] = float(self.cabled_rx_path_loss_db[n])
        limits = self.rx_limits_dbm(lna_on)
        worst = []
        issues = []
        for m in range(self.n_rx):
            lv = coherent_sum_dbm([p - iso[m, n] if p != NEG_INF else NEG_INF for n, p in enumerate(peps)])
            worst.append(lv)
            if lv > limits[m] + 1e-9:
                need = required_attenuation_db(lv, limits[m] + self.rx_safe_margin_db, self.rx_safe_margin_db)
                issues.append(Issue("error", "RX_PROTECTION",
                                    "RX %d: worst-case coupled level %.2f dBm exceeds the safe level "
                                    "%.2f dBm (damage %.2f dBm - margin %.1f dB) in mode %s; add %.1f dB "
                                    "of isolation/attenuation" % (m, lv, limits[m],
                                                                  limits[m] + self.rx_safe_margin_db,
                                                                  self.rx_safe_margin_db, mode, need),
                                    "rx%d" % m))
        if mode == "radiate" and self.protect_passive_chain and any(p != NEG_INF for p in peps):
            if not self.passive_isolation_db > 0:
                issues.append(Issue("error", "PASSIVE_CHAIN", "protect_passive_chain = 1 but "
                                    "passive_isolation_db is not set: the co-located passive receiver "
                                    "is unprotected", "[active_tx] passive_isolation_db"))
            else:
                lv = coherent_sum_dbm([p - self.passive_isolation_db for p in peps if p != NEG_INF])
                lim = self.passive_rx_damage_dbm - self.rx_safe_margin_db
                if lv > lim + 1e-9:
                    issues.append(Issue("error", "PASSIVE_CHAIN",
                                        "co-located passive receiver: %.2f dBm exceeds %.2f dBm (damage "
                                        "%.2f dBm - margin %.1f dB); add %.1f dB of isolation"
                                        % (lv, lim, self.passive_rx_damage_dbm, self.rx_safe_margin_db,
                                           required_attenuation_db(lv, self.passive_rx_damage_dbm,
                                                                   self.rx_safe_margin_db)),
                                        "[active_tx] passive_isolation_db"))
        return issues, worst, limits

    # --- EIRP ----------------------------------------------------------------------
    def eirp_upper_bound_dbm(self, tx_pep_dbm) -> float:
        """Worst-case coherent EIRP of the given chain powers (net element gains)."""
        return eirp_upper_bound_dbm(list(tx_pep_dbm), self.tx_gain_dbi)

    # --- active reflection -----------------------------------------------------------
    def active_reflection(self, t) -> np.ndarray:
        """``|Gamma_n^act| = |sum_m S_nm t_m / t_n|``; None without an S-matrix.

        An undriven chain (``t_n = 0``) with incident coupled power reports inf.
        """
        if self.s_matrix is None:
            return None
        t = np.asarray(t, dtype=complex).reshape(-1)
        if t.size != self.n_tx:
            raise ValueError("weights have %d entries for %d TX chains" % (t.size, self.n_tx))
        b = self.s_matrix @ t
        out = np.zeros(self.n_tx)
        for n in range(self.n_tx):
            if abs(t[n]) > 0:
                out[n] = abs(b[n]) / abs(t[n])
            else:
                out[n] = 0.0 if abs(b[n]) == 0 else np.inf
        return out

    def active_reverse_dbm(self, t, p_fwd_dbm) -> np.ndarray:
        """Reverse power (dBm) at every chain output: ``b = S a`` with
        ``a_n = sqrt(P_fwd,n) exp(j arg t_n)``; None without an S-matrix."""
        if self.s_matrix is None:
            return None
        t = np.asarray(t, dtype=complex).reshape(-1)
        p = [(_lvl(v) if v is not None else NEG_INF) for v in p_fwd_dbm]
        if t.size != self.n_tx or len(p) != self.n_tx:
            raise ValueError("weights / powers must have n_tx entries")
        a = np.array([math.sqrt(_w(pi)) if abs(ti) > 0 else 0.0 for pi, ti in zip(p, t)]) * np.exp(1j * np.angle(t))
        b = self.s_matrix @ a
        with np.errstate(divide="ignore"):
            return 10.0 * np.log10(np.abs(b) ** 2) + 30.0

    # --- predictions -------------------------------------------------------------------
    def predict_all(self, f_hz, tx_gains_db, dig_rms, dig_peak, papr_margin_db=0.0,
                    pa_backoff_db=1.0) -> list:
        """``TxChain.predict`` for every TX chain (scalars broadcast)."""
        def bc(v):
            return list(v) if np.ndim(v) else [v] * self.n_tx
        g, r, p = bc(tx_gains_db), bc(dig_rms), bc(dig_peak)
        return [c.predict(f_hz, g[n], r[n], p[n], papr_margin_db, pa_backoff_db)
                for n, c in enumerate(self.tx)]

    # --- validation ----------------------------------------------------------------------
    def validate(self, cfg) -> list:
        """Front-end findings: build warnings, TX_LOCK stage requirements, power-cal sanity."""
        issues = list(self.build_issues)
        tx_lock = bool(cfg.tx.en_tx)
        for n, c in enumerate(self.tx):
            for s in c.stages:
                if tx_lock and s.is_amp and not s.modelled:
                    issues.append(Issue("error", "TX_LOCK", "TX %d stage %r: every amp/pa needs p1db or "
                                        "psat with TX enabled" % (n, s.name), "tx_chain.%d" % n))
        if tx_lock:
            for m, c in enumerate(self.rx):
                s = c.first_active_stage()
                if s is not None and s.max_in_dbm is None:
                    issues.append(Issue("error", "TX_LOCK", "RX %d stage %r: the first active RX stage "
                                        "needs max_in_dbm with TX enabled" % (m, s.name), "rx_chain.%d" % m))
        pcal = self.tx[0].power_cal if self.tx else None
        if pcal is not None and pcal.calibrated:
            for c in pcal.chans():
                if c >= self.n_tx:
                    issues.append(Issue("error", "POWER_CAL", "tx_power_cal_file chan %d: no such global "
                                        "TX (n_tx = %d)" % (c, self.n_tx),
                                        "[active_frontend] tx_power_cal_file"))
            for c, f, g, p in pcal.rows:
                if c < self.n_tx:
                    try:
                        up = pcal.upper_dbm(c)
                    except ValueError:
                        continue
                    if p > up + 1e-9:
                        issues.append(Issue("warning", "POWER_CAL",
                                            "chan %d %.3f MHz gain %g dB: measured %.2f dBm exceeds the "
                                            "caps bound %.2f dBm (caps values are placeholders [verify])"
                                            % (c, f / 1e6, g, p, up), "[active_frontend] tx_power_cal_file"))
        return issues

    # --- header stamps -------------------------------------------------------------------
    def header_stamps(self, f_hz, rx_gains_db, lna_on_mask) -> dict:
        """IQ-header v8 stamp values for the first <= 32 RX channels:
        ``ext_lna_tenths`` / ``total_gain_tenths`` (dB x10), ``nf_mdb`` (milli-dB),
        ``compression_mask`` and ``lna_enable_mask`` (bit m = channel m)."""
        n = min(self.n_rx, MAX_HEADER_CHANNELS)
        gains = list(rx_gains_db) if np.ndim(rx_gains_db) else [rx_gains_db] * self.n_rx
        if len(gains) < n:
            raise ValueError("rx_gains_db has %d entries for %d channels" % (len(gains), self.n_rx))
        lna = _mask_bits(lna_on_mask, self.n_rx)
        ext, tot, nf = [], [], []
        comp = 0
        lmask = 0
        for m in range(n):
            b = self.rx[m].budget(f_hz, self.expected_input_dbm, float(gains[m]), lna[m],
                                  self.lna_off_isolation_db, self.p1db_margin_db)
            ext.append(int(round(b["ext_gain_db"] * 10.0)))
            tot.append(int(round(b["total_gain_db"] * 10.0)))
            nf.append(int(round(b["nf_db"] * 1000.0)))
            if b["compressed"]:
                comp |= 1 << m
            if lna[m]:
                lmask |= 1 << m
        return {"ext_lna_tenths": ext, "total_gain_tenths": tot, "nf_mdb": nf,
                "compression_mask": comp, "lna_enable_mask": lmask}

    def to_dict(self) -> dict:
        return {"n_rx": self.n_rx, "n_tx": self.n_tx,
                "rx_chains": [[s.to_dict() for s in c.stages] for c in self.rx],
                "tx_chains": [[s.to_dict() for s in c.stages] for c in self.tx],
                "isolation_db": self.isolation_db.tolist(), "tx_gain_dbi": list(self.tx_gain_dbi),
                "rx_gain_dbi": list(self.rx_gain_dbi), "power_calibrated":
                    bool(self.tx and self.tx[0].power_cal.calibrated),
                "s_matrix": self.s_matrix is not None}
