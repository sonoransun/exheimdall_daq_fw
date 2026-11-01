"""
    HeIMDALL "active" capability: regulatory frequency policy and RF exposure.

    Pure functions (stdlib math; ``forbidden_mask`` imports numpy lazily) used by
    the TX safety controller:

      - ``BUILTIN_DENY_BANDS_HZ``: an immutable, conservative and NON-EXHAUSTIVE
        list of safety-of-life / navigation / radio-astronomy bands. It is
        applied in every mode and cannot be removed by configuration (only the
        conducted modes may ignore it, through ``conducted_ignore_builtin_deny``);
      - ``tx_frequency_intervals``: every frequency interval a transmission
        touches - the occupied band, the LO leakage carrier and, with an LO
        offset, the image band;
      - ``band_check``: allow-list / deny-list evaluation (codes
        ``FREQ_NOT_ALLOWED`` / ``FREQ_DENIED``); ``forbidden_mask`` applies the
        same rule to spectral bins (the per-burst emission mask);
      - ``mpe_limit_w_m2`` / ``mpe_distance_m``: general-population maximum
        permissible exposure (FCC 47 CFR 1.1310 style) and the far-field
        exclusion distance;
      - ``REGULATORY_WARNING`` / ``regulatory_banner``: the text printed on every
        arm.

    The deny list and the MPE table are [verify] items: check them against the
    current rules of the operator's jurisdiction. This software is not legal
    advice.

    LO convention. UHD's ``tune_request(target, lo_off)`` places the LO at
    ``target + lo_off``; the active spec writes it ``fc - lo_offset``. Because
    the convention differs between drivers (and the Per Vices fork is
    unverified), BOTH candidate LO positions and both image bands are checked:
    the result is a superset of the true intervals, which can only make the
    policy stricter.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import math

from active_common import Issue

LO_LEAK_HALF_WIDTH_HZ = 1e3          # the LO carrier is checked as LO +- 1 kHz

CONDUCTED_MODES = ("dummy_load", "cabled")

# (lo_hz, hi_hz, label). Conservative, non-exhaustive [verify]. Applied in all modes.
BUILTIN_DENY_BANDS_HZ = (
    (74.8e6, 75.2e6, "aeronautical marker beacons"),
    (108.0e6, 137.0e6, "aeronautical navigation / communication (incl. 121.5 MHz distress)"),
    (156.7625e6, 156.8375e6, "marine VHF channel 16 (distress / calling)"),
    (242.95e6, 243.05e6, "military aeronautical distress (243 MHz)"),
    (328.6e6, 335.4e6, "ILS glideslope"),
    (399.9e6, 400.05e6, "radionavigation-satellite"),
    (406.0e6, 406.1e6, "COSPAS-SARSAT distress beacons"),
    (960.0e6, 1215.0e6, "DME, SSR, ADS-B, GNSS L5/E5"),
    (1215.0e6, 1300.0e6, "GNSS L2/E6, radiolocation radar"),
    (1400.0e6, 1427.0e6, "radio astronomy (passive, all emissions prohibited)"),
    (1525.0e6, 1660.5e6, "MSS downlinks, SAR, GNSS L1/E1/B1/G1, radio astronomy 1610.6-1613.8 MHz"),
    (2690.0e6, 2900.0e6, "radio astronomy + ATC radar"),
    (4200.0e6, 4400.0e6, "radio altimeters"),
    (5030.0e6, 5150.0e6, "aeronautical MLS"),
    (5350.0e6, 5470.0e6, "aeronautical radar"),
    (9000.0e6, 9500.0e6, "radar"),
    (10600.0e6, 10700.0e6, "radio astronomy"),
    (15350.0e6, 15400.0e6, "radio astronomy"),
)

REGULATORY_WARNING = (
    "REGULATORY WARNING: transmitting requires a licence or other authorization valid for this "
    "frequency, power, emission and location. Array gain counts toward EIRP: N coherent chains "
    "raise the main-beam EIRP by up to 20*log10(N) dB over one chain. An uncertified SDR + PA "
    "is generally not a compliant intentional radiator (spurious and out-of-band emissions are "
    "not verified by this software). Keep people outside the computed RF-exposure (MPE) "
    "distance. Hardware interlocks (an E-stop wired to PA enable, PA foldback, isolators) are "
    "required for amplified systems. Verify current rules in your jurisdiction; this software "
    "is not legal advice."
)


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------
def _finite(x, what) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError("%s must be finite, got %r" % (what, x))
    return v


def tx_frequency_intervals(fc_hz, occupied_bw_hz, lo_offset_hz, fs_hz) -> list:
    """Every RF interval a transmission at ``fc_hz`` touches: ``[(lo, hi, label)]``.

    - ``occupied``: ``fc +- occupied_bw/2`` (``occupied_bw <= 0`` -> the sample
      rate, the conservative full DAC band);
    - ``lo``: the LO leakage carrier ``+- 1 kHz``. With ``lo_offset == 0`` the LO
      sits at ``fc``; otherwise both conventions ``fc - lo_offset`` (spec) and
      ``fc + lo_offset`` (UHD tune_request) are listed (``lo`` / ``lo_alt``);
    - ``image`` / ``image_alt`` (``lo_offset != 0`` only): the occupied band
      mirrored about each LO candidate (``2*LO - fc``), the IQ-imbalance image.
    """
    fc = _finite(fc_hz, "fc_hz")
    fs = _finite(fs_hz, "fs_hz")
    off = _finite(lo_offset_hz, "lo_offset_hz")
    occ = _finite(occupied_bw_hz, "occupied_bw_hz")
    if fc <= 0:
        raise ValueError("fc_hz must be > 0")
    if occ <= 0:
        occ = fs
    if occ <= 0:
        raise ValueError("occupied bandwidth and sample rate are both <= 0")
    half = occ / 2.0
    out = [(fc - half, fc + half, "occupied")]
    if off == 0.0:
        out.append((fc - LO_LEAK_HALF_WIDTH_HZ, fc + LO_LEAK_HALF_WIDTH_HZ, "lo"))
        return out
    for label, lo in (("lo", fc - off), ("lo_alt", fc + off)):
        out.append((lo - LO_LEAK_HALF_WIDTH_HZ, lo + LO_LEAK_HALF_WIDTH_HZ, label))
    for label, lo in (("image", fc - off), ("image_alt", fc + off)):
        img = 2.0 * lo - fc
        out.append((img - half, img + half, label))
    return out


def _band(b):
    lo, hi = float(b[0]), float(b[1])
    label = str(b[2]) if len(b) > 2 else ""
    return lo, hi, label


def _overlaps(a_lo, a_hi, b_lo, b_hi) -> bool:
    """Open-interval overlap (touching edges do not overlap)."""
    return a_lo < b_hi and a_hi > b_lo


def _fmt_mhz(x) -> str:
    return "%.6f" % (x / 1e6)


def band_check(intervals, mode, allow_bands, deny_bands, ignore_builtin=False) -> list:
    """Evaluate TX ``intervals`` (``[(lo, hi[, label])]``) against the policy.

    - every interval must lie inside ONE allow band (all modes; an empty allow
      list allows nothing) -> error ``FREQ_NOT_ALLOWED``;
    - any overlap with a configured deny band -> error ``FREQ_DENIED``;
    - any overlap with ``BUILTIN_DENY_BANDS_HZ`` -> error ``FREQ_DENIED``, unless
      ``ignore_builtin`` and ``mode`` is conducted (dummy_load / cabled), which
      gives a warning ``FREQ_DENIED`` instead (the override is logged at every
      arm). ``ignore_builtin`` never applies to radiate.
    """
    issues = []
    allow = [_band(b) for b in (allow_bands or ())]
    deny = [_band(b) for b in (deny_bands or ())]
    conducted = mode in CONDUCTED_MODES
    for iv in intervals:
        lo, hi, label = _band(iv)
        what = "%s %s..%s MHz" % (label or "interval", _fmt_mhz(lo), _fmt_mhz(hi))
        if not any(alo <= lo and hi <= ahi for alo, ahi, _ in allow):
            if not allow:
                msg = "%s: allow_bands is empty (nothing may be transmitted)" % what
            else:
                msg = "%s is not inside any allow band (%s)" % (
                    what, "; ".join("%s..%s MHz" % (_fmt_mhz(a), _fmt_mhz(b)) for a, b, _ in allow))
            issues.append(Issue("error", "FREQ_NOT_ALLOWED", msg, "[active_tx] allow_bands"))
        for dlo, dhi, dlabel in deny:
            if _overlaps(lo, hi, dlo, dhi):
                issues.append(Issue("error", "FREQ_DENIED", "%s overlaps the configured deny band "
                                    "%s..%s MHz%s" % (what, _fmt_mhz(dlo), _fmt_mhz(dhi),
                                                      " (%s)" % dlabel if dlabel else ""),
                                    "[active_tx] deny_bands"))
        for dlo, dhi, dlabel in BUILTIN_DENY_BANDS_HZ:
            if not _overlaps(lo, hi, dlo, dhi):
                continue
            msg = "%s overlaps the built-in deny band %s..%s MHz (%s)" % (
                what, _fmt_mhz(dlo), _fmt_mhz(dhi), dlabel)
            if ignore_builtin and conducted:
                issues.append(Issue("warning", "FREQ_DENIED", msg + "; ignored for the conducted "
                                    "mode %s (conducted_ignore_builtin_deny = 1)" % mode,
                                    "[active_tx] conducted_ignore_builtin_deny"))
            else:
                issues.append(Issue("error", "FREQ_DENIED", msg, "BUILTIN_DENY_BANDS_HZ"))
    return issues


def forbidden_mask(freqs_hz, bin_hz, mode, allow_bands, deny_bands, ignore_builtin=False,
                   lo_offset_hz=0.0, fc_hz=None):
    """Vectorized policy test for spectral bins (numpy, imported lazily).

    Bin ``k`` covers ``[f_k - bin/2, f_k + bin/2]``; it is forbidden (True) when
    that interval is not inside one allow band, or overlaps a configured deny
    band, or overlaps a built-in deny band (unless ``ignore_builtin`` and a
    conducted ``mode``: the same rule as ``band_check``). With ``lo_offset_hz
    != 0`` the bin's IQ image about either LO candidate (``2*LO - f``, LO =
    ``fc -/+ lo_offset``) must pass too (image rejection is not credited).
    """
    import numpy as np
    f = np.asarray(freqs_hz, dtype=float).reshape(-1)
    half = abs(float(bin_hz)) / 2.0
    allow = [_band(b) for b in (allow_bands or ())]
    deny = [_band(b) for b in (deny_bands or ())]
    builtin = () if (ignore_builtin and mode in CONDUCTED_MODES) else BUILTIN_DENY_BANDS_HZ

    def test(fr):
        lo, hi = fr - half, fr + half
        ok = np.zeros(fr.shape, dtype=bool)
        for alo, ahi, _ in allow:
            ok |= (alo <= lo) & (hi <= ahi)
        bad = ~ok
        for dlo, dhi, _ in list(deny) + [_band(b) for b in builtin]:
            bad |= (lo < dhi) & (hi > dlo)
        return bad

    out = test(f)
    off = float(lo_offset_hz or 0.0)
    if off != 0.0 and fc_hz is not None:
        fc = float(fc_hz)
        for lo_c in (fc - off, fc + off):
            out |= test(2.0 * lo_c - f)
    return out


def builtin_deny_hits(lo_hz, hi_hz) -> list:
    """Built-in deny bands overlapping ``[lo_hz, hi_hz]``."""
    return [b for b in BUILTIN_DENY_BANDS_HZ if _overlaps(float(lo_hz), float(hi_hz), b[0], b[1])]


# ---------------------------------------------------------------------------
# RF exposure
# ---------------------------------------------------------------------------
def mpe_limit_w_m2(f_hz) -> float:
    """General-population MPE power density limit (W/m^2) [verify jurisdiction].

    =============  ==============
    3-30 MHz       1800 / f_MHz^2
    30-300 MHz     2
    300-1500 MHz   f_MHz / 150
    1.5-100 GHz    10
    =============  ==============

    ``ValueError`` below 3 MHz (near-field / induced-current regime: radiate is
    refused) and above 100 GHz (outside the table).
    """
    f = _finite(f_hz, "f_hz")
    f_mhz = f / 1e6
    if f_mhz < 3.0:
        raise ValueError("MPE table not defined below 3 MHz (got %.6g MHz): radiate refused" % f_mhz)
    if f_mhz < 30.0:
        return 1800.0 / (f_mhz * f_mhz)
    if f_mhz < 300.0:
        return 2.0
    if f_mhz < 1500.0:
        return f_mhz / 150.0
    if f_mhz <= 100e3:
        return 10.0
    raise ValueError("MPE table not defined above 100 GHz (got %.6g MHz)" % f_mhz)


def mpe_limit_band_w_m2(f_lo_hz, f_hi_hz) -> float:
    """Most stringent (smallest) MPE limit over ``[f_lo, f_hi]``.

    The limit is monotone on each table segment, so the minimum is at an edge or
    on the flat 30-300 MHz segment.
    """
    lo, hi = sorted((float(f_lo_hz), float(f_hi_hz)))
    cands = [lo, hi] + [x for x in (30e6, 300e6) if lo <= x <= hi]
    return min(mpe_limit_w_m2(f) for f in cands)


def mpe_distance_m(eirp_dbm, f_hz, duty=1.0) -> float:
    """Far-field distance where the power density falls to the MPE limit:
    ``R = sqrt(EIRP_W * duty / (4 pi S_lim))``."""
    eirp = float(eirp_dbm)
    d = float(duty)
    if not 0.0 <= d <= 1.0:
        raise ValueError("duty must be within [0, 1], got %r" % duty)
    if eirp == float("-inf") or d == 0.0:
        return 0.0
    if not math.isfinite(eirp):
        raise ValueError("eirp_dbm must be finite or -inf")
    p_w = 10.0 ** ((eirp - 30.0) / 10.0)
    return math.sqrt(p_w * d / (4.0 * math.pi * mpe_limit_w_m2(f_hz)))


def power_density_w_m2(eirp_dbm, distance_m, duty=1.0) -> float:
    """Far-field power density ``EIRP_W * duty / (4 pi R^2)``."""
    r = float(distance_m)
    if not r > 0:
        raise ValueError("distance must be > 0")
    return 10.0 ** ((float(eirp_dbm) - 30.0) / 10.0) * float(duty) / (4.0 * math.pi * r * r)


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
def regulatory_banner(mode, fc_hz, authorization="", authorization_kind="none", callsign="",
                      band_issues=(), eirp_dbm=None, eirp_limit_dbm=None, mpe_m=None,
                      mpe_exclusion_m=None, station_id_interval_s=None, simulated=False) -> str:
    """The full text logged (and printed by the CLI) on every arm."""
    lines = [REGULATORY_WARNING, ""]
    lines.append("mode: %s%s   fc: %s MHz" % (mode, " (SIMULATED)" if simulated else "",
                                               _fmt_mhz(float(fc_hz))))
    auth = authorization or "(none)"
    lines.append("authorization: %s [%s]" % (auth, authorization_kind or "none"))
    if authorization_kind == "amateur":
        lines.append("callsign: %s - identify your station; a reminder is emitted every %s s while "
                     "armed" % (callsign or "(missing)", "%g" % station_id_interval_s
                                if station_id_interval_s else "?"))
    errs = [i for i in band_issues if i.level == "error"]
    warns = [i for i in band_issues if i.level == "warning"]
    if errs:
        lines.append("frequency evaluation: REFUSED - %s" % "; ".join(i.message for i in errs))
    else:
        lines.append("frequency evaluation: every TX interval inside an allow band, outside the "
                     "deny bands%s" % (" (built-in deny override in a conducted mode)" if warns else ""))
    if eirp_dbm is not None:
        lim = "" if eirp_limit_dbm is None else " (limit %.2f dBm)" % float(eirp_limit_dbm)
        lines.append("worst-case EIRP (coherent, array gain included): %.2f dBm%s"
                     % (float(eirp_dbm), lim))
    if mpe_m is not None:
        ex = "" if mpe_exclusion_m is None else " (exclusion zone %.2f m)" % float(mpe_exclusion_m)
        lines.append("MPE distance (duty-averaged worst case): %.2f m%s" % (float(mpe_m), ex))
    if mode in CONDUCTED_MODES:
        lines.append("conducted mode: the TX outputs must be terminated in rated dummy loads / "
                     "attenuators; nothing may be connected to an antenna")
    return "\n".join(lines)
