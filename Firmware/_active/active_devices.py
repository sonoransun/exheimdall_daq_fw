"""
    HeIMDALL "active" capability: device capabilities, grouping and plan rules.

    - ``CAPS``: per-family capability table for Ettus/NI USRP (B210, X310 with
      UBX/SBX/TwinRX, N300/N310/N320/N321, X410, X440), Per Vices (Crimson TNG,
      Cyan, Cyan 3G, Chestnut, Calamine) and the simulator. Numbers marked
      [verify] in ``notes`` are conservative placeholders until checked against
      datasheets or measured (``tx_power_cal_file``); code treats them as
      configuration, never as hard facts.
    - ``device_groups``: which devices share one ``uhd.usrp.MultiUSRP``.
    - ``build_uhd_args`` / ``build_pervices_args``: device-args strings.
    - ``validate_device_plan``: hardware plan rules (codes ``DEVICE_*``).
    - ``rate_budget``: per-port network throughput budget (code ``RATE``).

    Pure Python: no hardware library is imported here.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from active_common import ChannelRef, ConfigError, Issue

# ---------------------------------------------------------------------------
# Capability table
# ---------------------------------------------------------------------------
_ETTUS_SRC = ("internal", "external", "gpsdo")
_N3XX_TIME = ("internal", "external", "gpsdo", "sfp0")
_X4XX_CLK = ("internal", "external", "gpsdo", "mboard")
_X4XX_TIME = ("internal", "external", "gpsdo", "qsfp0")
_PV_SRC = ("internal", "external")


@dataclass(frozen=True)
class DeviceCaps:
    family: str
    vendor: str                       # ettus | pervices | sim
    uhd_type: str                     # args "type=" value, or ""
    n_rx: int
    n_tx: int
    freq_min_hz: float
    freq_max_hz: float
    max_bw_hz: float
    dual_chan_max_rate_hz: float      # 0 = n/a
    mcr_choices_hz: tuple             # () = continuous/managed; the first entry is the UHD default
    mcr_range_hz: tuple               # (lo, hi) or ()
    rate_base_hz: float               # Per Vices: rate = rate_base/N ; 0 = n/a
    tick_rate_hz: int                 # 0 = equals the master clock rate
    compound_multi_usrp: bool
    single_mboard_only: bool
    timed_tune: bool
    phase_repeatable_timed_tune: bool
    phase_repeatable_reinit: bool
    lo_export: bool
    lo_import: bool
    lo_share_kind: str                # none|intra_device|n321_star|twinrx|external_lo|pervices_common|sim
    phase_ambiguity: str              # random | 180 | deterministic
    multi_device_coherent: bool
    clock_sources: tuple
    time_sources: tuple
    gpsdo_option: bool
    tx_max_dbm_upper: float           # conservative UPPER bound of conducted TX port power
    rx_max_input_dbm: float           # conservative damage-safe max at the RX port
    tx_gain_range_db: tuple           # (min, max, step)
    rx_gain_range_db: tuple
    integer_delay_ctl: bool           # Per Vices delay_iq
    gpio_banks: tuple
    band_edges_hz: tuple              # analog band switch points
    tx_reduced_rate_chans: tuple      # Crimson (2, 3) limited to rate_base/4
    min_tx_lead_s: float
    notes: tuple
    ext_lo_phase_ambiguity: str = ""  # phase ambiguity when lo_source = external ("" = same)
    lo_share_min_hz: float = 0.0      # LO sharing works only above this frequency


def _caps(**kw):
    base = dict(dual_chan_max_rate_hz=0.0, mcr_choices_hz=(), mcr_range_hz=(), rate_base_hz=0.0,
                tick_rate_hz=0, compound_multi_usrp=True, single_mboard_only=False,
                timed_tune=True, phase_repeatable_timed_tune=False, phase_repeatable_reinit=False,
                lo_export=False, lo_import=False, lo_share_kind="none", phase_ambiguity="random",
                multi_device_coherent=True, clock_sources=_ETTUS_SRC, time_sources=_ETTUS_SRC,
                gpsdo_option=True, tx_max_dbm_upper=20.0, rx_max_input_dbm=-15.0,
                integer_delay_ctl=False, gpio_banks=("FP0",), band_edges_hz=(),
                tx_reduced_rate_chans=(), min_tx_lead_s=0.0, notes=())
    base.update(kw)
    return DeviceCaps(**base)


_V = "[verify]"
_PV_COMMON = dict(vendor="pervices", compound_multi_usrp=False, single_mboard_only=True,
                  timed_tune=False, lo_share_kind="pervices_common", clock_sources=_PV_SRC,
                  time_sources=_PV_SRC, gpsdo_option=False, tx_max_dbm_upper=18.0,
                  rx_max_input_dbm=-10.0, integer_delay_ctl=True, gpio_banks=(),
                  min_tx_lead_s=0.0002, tx_gain_range_db=(0.0, 31.75, 0.25),
                  rx_gain_range_db=(0.0, 83.25, 0.25))

CAPS = {
    "b210": _caps(
        family="b210", vendor="ettus", uhd_type="b200", n_rx=2, n_tx=2, freq_min_hz=70e6,
        freq_max_hz=6e9, max_bw_hz=56e6, dual_chan_max_rate_hz=30.72e6,
        mcr_range_hz=(5e6, 61.44e6), compound_multi_usrp=False, lo_share_kind="intra_device",
        multi_device_coherent=False, tx_gain_range_db=(0.0, 89.75, 0.25),
        rx_gain_range_db=(0.0, 76.0, 1.0),
        notes=("two channels share one AD9361 LO (intra-device coherent)",
               "multiple B2xx only as separate groups with external ref + PPS " + _V,
               "TX/RX power figures are placeholders " + _V)),
    "x310_ubx": _caps(
        family="x310_ubx", vendor="ettus", uhd_type="x300", n_rx=2, n_tx=2, freq_min_hz=10e6,
        freq_max_hz=6e9, max_bw_hz=160e6, mcr_choices_hz=(200e6, 184.32e6),
        phase_repeatable_timed_tune=True, tx_gain_range_db=(0.0, 31.5, 0.5),
        rx_gain_range_db=(0.0, 31.5, 0.5),
        notes=("UBX phase is repeatable after a timed tune",
               "TX/RX power figures are placeholders " + _V)),
    "x310_sbx": _caps(
        family="x310_sbx", vendor="ettus", uhd_type="x300", n_rx=2, n_tx=2, freq_min_hz=400e6,
        freq_max_hz=4.4e9, max_bw_hz=120e6, mcr_choices_hz=(200e6, 184.32e6),
        phase_repeatable_timed_tune=True, tx_gain_range_db=(0.0, 31.5, 0.5),
        rx_gain_range_db=(0.0, 31.5, 0.5),
        notes=("SBX phase is repeatable after a timed tune",
               "TX/RX power figures are placeholders " + _V)),
    "x310_twinrx": _caps(
        family="x310_twinrx", vendor="ettus", uhd_type="x300", n_rx=4, n_tx=0,
        freq_min_hz=10e6, freq_max_hz=6e9, max_bw_hz=80e6, mcr_choices_hz=(200e6,),
        phase_repeatable_timed_tune=True, lo_export=True, lo_import=True, lo_share_kind="twinrx",
        tx_max_dbm_upper=0.0, tx_gain_range_db=(0.0, 0.0, 1.0), rx_gain_range_db=(0.0, 93.0, 1.0),
        notes=("RX-only (no TX); LOs shared via companion/external LO ports",)),
    "n300": _caps(
        family="n300", vendor="ettus", uhd_type="n3xx", n_rx=2, n_tx=2, freq_min_hz=10e6,
        freq_max_hz=6e9, max_bw_hz=100e6, mcr_choices_hz=(125e6, 122.88e6, 153.6e6),
        time_sources=_N3XX_TIME, tx_max_dbm_upper=18.0, tx_gain_range_db=(0.0, 65.0, 0.5),
        rx_gain_range_db=(0.0, 75.0, 0.5),
        notes=("external clock + gpsdo time is not supported",
               "TX/RX power figures are placeholders " + _V)),
    "n310": _caps(
        family="n310", vendor="ettus", uhd_type="n3xx", n_rx=4, n_tx=4, freq_min_hz=10e6,
        freq_max_hz=6e9, max_bw_hz=100e6, mcr_choices_hz=(125e6, 122.88e6, 153.6e6),
        time_sources=_N3XX_TIME, lo_import=True, lo_share_kind="external_lo",
        ext_lo_phase_ambiguity="180", tx_max_dbm_upper=18.0, tx_gain_range_db=(0.0, 65.0, 0.5),
        rx_gain_range_db=(0.0, 75.0, 0.5),
        notes=("external LO import leaves a 180 degree ambiguity: calibrate after every init/retune",
               "TX/RX power figures are placeholders " + _V)),
    "n320": _caps(
        family="n320", vendor="ettus", uhd_type="n3xx", n_rx=2, n_tx=2, freq_min_hz=3e6,
        freq_max_hz=6e9, max_bw_hz=200e6, mcr_choices_hz=(200e6, 245.76e6, 250e6),
        time_sources=_N3XX_TIME, lo_import=True, lo_share_kind="n321_star", lo_share_min_hz=450e6,
        tx_max_dbm_upper=18.0, tx_gain_range_db=(0.0, 60.0, 1.0), rx_gain_range_db=(0.0, 60.0, 1.0),
        notes=("imports the N321 LO star above 450 MHz",
               "TX/RX power figures are placeholders " + _V)),
    "n321": _caps(
        family="n321", vendor="ettus", uhd_type="n3xx", n_rx=2, n_tx=2, freq_min_hz=3e6,
        freq_max_hz=6e9, max_bw_hz=200e6, mcr_choices_hz=(200e6, 245.76e6, 250e6),
        time_sources=_N3XX_TIME, lo_export=True, lo_import=True, lo_share_kind="n321_star",
        lo_share_min_hz=450e6, tx_max_dbm_upper=18.0, tx_gain_range_db=(0.0, 60.0, 1.0),
        rx_gain_range_db=(0.0, 60.0, 1.0),
        notes=("exports its LO to 4 outputs (LO star) above 450 MHz",
               "Python access to the LO_OUT splitter/terminator " + _V,
               "TX/RX power figures are placeholders " + _V)),
    "x410": _caps(
        family="x410", vendor="ettus", uhd_type="x4xx", n_rx=4, n_tx=4, freq_min_hz=1e6,
        freq_max_hz=7.2e9, max_bw_hz=400e6, mcr_choices_hz=(245.76e6, 250e6, 491.52e6, 500e6),
        timed_tune=False, multi_device_coherent=False, clock_sources=_X4XX_CLK,
        time_sources=_X4XX_TIME, tx_max_dbm_upper=23.0, tx_gain_range_db=(0.0, 60.0, 1.0),
        rx_gain_range_db=(0.0, 60.0, 1.0), gpio_banks=("GPIO0", "GPIO1"),
        notes=("ZBX: no timed tune; phase is random after every tune",
               "single-device coherent only",
               "GPIO/ATR source setup " + _V, "TX/RX power figures are placeholders " + _V)),
    "x440": _caps(
        family="x440", vendor="ettus", uhd_type="x4xx", n_rx=8, n_tx=8, freq_min_hz=30e6,
        freq_max_hz=4e9, max_bw_hz=1.6e9, mcr_range_hz=(125e6, 2.048e9), timed_tune=False,
        phase_repeatable_reinit=True, phase_ambiguity="deterministic", clock_sources=_X4XX_CLK,
        time_sources=_X4XX_TIME, tx_max_dbm_upper=10.0, tx_gain_range_db=(0.0, 0.0, 1.0),
        rx_gain_range_db=(0.0, 0.0, 1.0), gpio_banks=("GPIO0", "GPIO1"),
        notes=("direct-sampling (NCO tuning); deterministic phase with the default image " + _V,
               "sample-based coherence, about 2 degrees rms device-to-device " + _V,
               "gain ranges and TX power are placeholders " + _V)),
    "crimson_tng": _caps(
        family="crimson_tng", uhd_type="crimson_tng", n_rx=4, n_tx=4, freq_min_hz=1e6,
        freq_max_hz=6e9, max_bw_hz=325e6, rate_base_hz=325e6, tick_rate_hz=162_500_000,
        band_edges_hz=(120e6,), tx_reduced_rate_chans=(2, 3),
        notes=("one MultiUSRP per unit; host next-PPS alignment across units",
               "tune is always untimed (set_command_time affects GPIO only)",
               "TX channels C/D limited to rate_base/4 unless the full TX image is loaded",
               "common LO software selectability " + _V, "TX/RX power figures are placeholders " + _V),
        **_PV_COMMON),
    "cyan": _caps(
        family="cyan", uhd_type="cyan_nrnt", n_rx=16, n_tx=16, freq_min_hz=1e6, freq_max_hz=18e9,
        max_bw_hz=1e9, rate_base_hz=1e9, tick_rate_hz=250_000_000,
        band_edges_hz=(250e6, 400e6, 5.8e9),
        **dict(_PV_COMMON, rx_gain_range_db=(-6.0, 33.0, 1.0)),
        notes=("up to 16 RX/16 TX; the default unit is 8/8",
               "gain ranges: the server-reported range is authoritative",
               "set_time_next_pps on Cyan " + _V, "TX/RX power figures are placeholders " + _V)),
    "cyan_hb": _caps(
        family="cyan_hb", uhd_type="cyan_nrnt", n_rx=4, n_tx=4, freq_min_hz=1e6, freq_max_hz=18e9,
        max_bw_hz=3e9, rate_base_hz=3e9, tick_rate_hz=250_000_000,
        band_edges_hz=(250e6, 400e6, 5.8e9),
        **dict(_PV_COMMON, rx_gain_range_db=(-6.0, 33.0, 1.0)),
        notes=("3 GSPS variant (no decimation); RX 3 bytes/sample on the wire",
               "TX/RX power figures are placeholders " + _V)),
    "chestnut": _caps(
        family="chestnut", uhd_type="chestnut", n_rx=4, n_tx=4, freq_min_hz=1e6, freq_max_hz=9e9,
        max_bw_hz=500e6, rate_base_hz=500e6, tick_rate_hz=250_000_000,
        notes=("rate_base, tick rate, gains and TX power are placeholders " + _V,),
        **_PV_COMMON),
    "calamine": _caps(
        family="calamine", uhd_type="crimson_tng", n_rx=4, n_tx=4, freq_min_hz=1e6,
        freq_max_hz=40e9, max_bw_hz=325e6, rate_base_hz=325e6, tick_rate_hz=162_500_000,
        notes=("uses the crimson_tng driver", "channel counts (TX count) " + _V,
               "gains and TX power are placeholders " + _V),
        **_PV_COMMON),
    "sim": _caps(
        family="sim", vendor="sim", uhd_type="", n_rx=32, n_tx=32, freq_min_hz=1e6,
        freq_max_hz=20e9, max_bw_hz=100e6, mcr_choices_hz=(100e6,), tick_rate_hz=100_000_000,
        phase_repeatable_timed_tune=True, lo_export=True, lo_import=True, lo_share_kind="sim",
        tx_max_dbm_upper=10.0, rx_max_input_dbm=-10.0, tx_gain_range_db=(0.0, 31.75, 0.25),
        rx_gain_range_db=(0.0, 76.0, 1.0),
        notes=("deterministic simulator; coherence behaviour follows [active_sim] emulate_family",)),
}

_PHASE_RANK = {"random": 0, "ambiguous_180": 1, "deterministic": 2, "repeatable": 2, "shared_lo": 3}


def families() -> list:
    return list(CAPS.keys())


def get_caps(family: str) -> DeviceCaps:
    try:
        return CAPS[str(family).lower()]
    except KeyError:
        raise ConfigError("unknown device family %r (valid: %s)" % (family, ", ".join(CAPS)),
                          (Issue("error", "DEVICE_CHANNEL", "unknown family %r" % family),)) from None


# Families that may share one compound MultiUSRP (same motherboard type / clocking).
_COMPOUND_CLASS = {"n300": "n30x", "n310": "n30x", "n320": "n32x", "n321": "n32x",
                   "x310_ubx": "x300", "x310_sbx": "x300", "x310_twinrx": "x300"}


def compound_class(family: str) -> str:
    """Compatibility class for compound groups: N320 + N321 (the LO star) or
    X310 with different daughterboards may share a MultiUSRP; anything else
    only with its own family."""
    return _COMPOUND_CLASS.get(family, family)


def phase_ambiguity(family: str, lo_source: str = "internal") -> str:
    """Phase ambiguity class of ``family`` for the given ``lo_source``."""
    caps = get_caps(family)
    if lo_source == "external" and caps.ext_lo_phase_ambiguity:
        return caps.ext_lo_phase_ambiguity
    return caps.phase_ambiguity


def behaviour_caps(cfg, dev) -> DeviceCaps:
    """Caps governing coherence behaviour: the sim emulates ``emulate_family``."""
    if dev.family == "sim" and cfg.sim.emulate_family not in ("", "sim"):
        return get_caps(cfg.sim.emulate_family)
    return get_caps(dev.family)


def _family_default_mcr(family) -> float:
    caps = get_caps(family)
    return float(caps.mcr_choices_hz[0]) if caps.mcr_choices_hz else 0.0


def _group_of(cfg, dev):
    """The DeviceGroup holding ``dev`` (by name), else None."""
    if cfg is None:
        return None
    try:
        groups = device_groups(cfg)
    except Exception:  # noqa: BLE001  (a broken plan is reported by validate_device_plan)
        return None
    for g in groups:
        if any(d.name == dev.name for d in g.devices):
            return g
    return None


def group_mcr_hz(group) -> float:
    """Master clock rate UHD applies to EVERY mboard of ``group``'s MultiUSRP.

    ``build_uhd_args`` passes one unindexed ``master_clock_rate`` for the whole
    compound device, so a value pinned on any member governs all of them; with
    no pin it is the (first member's) family default. Members pinning different
    values are a DEVICE_MCR error (the first pin is returned here).
    """
    for d in group.devices:
        if d.master_clock_rate_hz:
            return float(d.master_clock_rate_hz)
    return _family_default_mcr(group.devices[0].family)


def effective_mcr_hz(dev, cfg=None) -> float:
    """Master clock rate ``dev`` actually runs at (0 = managed by the driver).

    With ``cfg`` the device's group is resolved: in a compound MultiUSRP one MCR
    applies to every mboard (``group_mcr_hz``). Without ``cfg``: the pinned
    value, else the family default. ``validate_device_plan`` requires every
    member of a compound group to pin the same value or none (DEVICE_MCR), so
    both answers agree on a valid plan.
    """
    g = _group_of(cfg, dev)
    if g is not None and g.compound:
        return group_mcr_hz(g)
    if dev.master_clock_rate_hz:
        return float(dev.master_clock_rate_hz)
    return _family_default_mcr(dev.family)


def tick_rate_hz(dev, cfg=None) -> int:
    """Device time tick rate (Hz, int). 0 when the MCR is driver-managed and unknown.

    Pass ``cfg`` so a compound group member gets the group's MCR (see
    ``effective_mcr_hz``)."""
    caps = get_caps(dev.family)
    if caps.tick_rate_hz:
        return int(caps.tick_rate_hz)
    return int(round(effective_mcr_hz(dev, cfg)))


def max_sample_rate_hz(dev, cfg=None) -> float:
    """Largest sample rate the device can stream: fs = MCR is valid on UHD.

    The effective MCR when known (pinned / group), else the largest MCR choice,
    the top of the MCR range, the Per Vices rate base, or (last resort) the
    analog bandwidth. B2xx dual-channel limits are a separate rule.
    """
    caps = get_caps(dev.family)
    if caps.rate_base_hz:
        return float(caps.rate_base_hz)
    g = _group_of(cfg, dev)
    pinned = (g is not None and g.compound and any(d.master_clock_rate_hz for d in g.devices)) \
        or bool(dev.master_clock_rate_hz)
    if pinned:
        return float(effective_mcr_hz(dev, cfg))
    if caps.mcr_choices_hz:
        return float(max(caps.mcr_choices_hz))
    if caps.mcr_range_hz:
        return float(caps.mcr_range_hz[1])
    return float(caps.max_bw_hz)


def effective_sources(cfg, dev) -> tuple:
    """``(clock_source, time_source)`` for ``dev``: per-device key, else
    ``[active_timing]``; ``auto`` = internal for one device, external for several."""
    several = len(cfg.devices) > 1
    out = []
    for dkey, tkey in (("clock_source", "clock_source"), ("time_source", "time_source")):
        val = getattr(dev, dkey) or getattr(cfg.timing, tkey)
        if val in ("", "auto"):
            val = "external" if several else "internal"
        out.append(val)
    return tuple(out)


def resolve_timed_tune(cfg) -> bool:
    """``timed_tune``: on/off as configured; auto = every (behaviour) family supports it."""
    tt = cfg.timing.timed_tune
    if tt == "on":
        return True
    if tt == "off":
        return False
    return all(behaviour_caps(cfg, d).timed_tune for d in cfg.devices) if cfg.devices else False


def resolve_lo_plan(cfg) -> str:
    """The LO plan; ``auto`` infers one from the device lo_source/lo_export keys."""
    plan = cfg.timing.lo_plan
    if plan != "auto":
        return plan
    fams = {d.family for d in cfg.devices}
    if any(d.family == "x310_twinrx" and d.lo_source in ("companion", "external") for d in cfg.devices):
        return "twinrx"
    exporters = [d for d in cfg.devices if d.lo_export and get_caps(d.family).lo_share_kind == "n321_star"]
    importers = [d for d in cfg.devices
                 if d.lo_source == "external" and get_caps(d.family).lo_share_kind == "n321_star"]
    if exporters and importers:
        return "n321_star"
    if "n310" in fams and any(d.family == "n310" and d.lo_source == "external" for d in cfg.devices):
        return "external_lo"
    return "none"


def lo_share_members(cfg, plan: str = None) -> dict:
    """LO-sharing roles under ``plan`` (default: the resolved plan).

    Returns ``{"eligible", "exporters", "importers", "shared", "outside"}``, each a
    list of device names. For ``n321_star`` an exporter is an LO-export-capable
    member with ``lo_export = 1`` or named ``lo_master``; an importer has
    ``lo_source = external``. For ``twinrx`` an exporter has ``lo_export = 1`` or
    is the ``lo_master``; an importer has ``lo_source`` companion or external.
    ``shared`` = members that really run from the shared LO (companion members
    always; exporters/external importers only when an exporter exists);
    ``outside`` = eligible members that neither export nor import.
    """
    plan = resolve_lo_plan(cfg) if plan is None else plan
    out = {"eligible": [], "exporters": [], "importers": [], "shared": [], "outside": []}
    if plan not in ("n321_star", "twinrx"):
        return out
    master = cfg.timing.lo_master
    for d in cfg.devices:
        caps = behaviour_caps(cfg, d)
        if caps.lo_share_kind != plan:
            continue
        out["eligible"].append(d.name)
        exporter = caps.lo_export and (d.lo_export or (master and d.name == master))
        if plan == "n321_star":
            importer = d.lo_source == "external"
        else:
            importer = d.lo_source in ("companion", "external")
        if exporter:
            out["exporters"].append(d.name)
        if importer:
            out["importers"].append(d.name)
        if not exporter and not importer:
            out["outside"].append(d.name)
    for d in cfg.devices:
        if d.name not in out["eligible"]:
            continue
        if plan == "twinrx" and d.lo_source == "companion":
            out["shared"].append(d.name)
        elif out["exporters"] and (d.name in out["exporters"] or d.name in out["importers"]):
            out["shared"].append(d.name)
    return out


def phase_after_tune(cfg) -> str:
    """Worst phase behaviour after a retune across all devices (§8.2 rule 4):
    ``shared_lo``, ``repeatable``, ``deterministic``, ``ambiguous_180`` or ``random``.

    Only devices that really take part in an LO share (``lo_share_members``)
    count as ``shared_lo``; the others fall through to the timed-tune/random rules.
    """
    if not cfg.devices:
        return "random"
    plan = resolve_lo_plan(cfg)
    shared = set(lo_share_members(cfg, plan)["shared"])
    timed = resolve_timed_tune(cfg)
    fc = cfg.rf.center_freq_hz
    single = len(cfg.devices) == 1
    worst = None
    for d in cfg.devices:
        caps = behaviour_caps(cfg, d)
        cls = "random"
        if plan == "n321_star" and d.name in shared and fc >= caps.lo_share_min_hz:
            cls = "shared_lo"
        elif plan == "twinrx" and d.name in shared:
            cls = "shared_lo"
        elif single and caps.lo_share_kind == "intra_device":
            cls = "shared_lo"
        elif plan == "external_lo" and caps.ext_lo_phase_ambiguity == "180" and d.lo_source == "external":
            cls = "ambiguous_180"
        elif caps.phase_ambiguity == "deterministic":
            cls = "deterministic"
        elif timed and caps.timed_tune and caps.phase_repeatable_timed_tune:
            cls = "repeatable"
        if worst is None or _PHASE_RANK[cls] < _PHASE_RANK[worst]:
            worst = cls
    return worst


def resolve_max_tx_gain_db(cfg) -> list:
    """Per global TX: ``max_tx_gain_db``, with "" (None) resolved to the caps MINIMUM."""
    out = []
    refs = cfg.tx_refs()
    vals = list(cfg.tx.max_tx_gain_db) or [None] * len(refs)
    for ref, val in zip(refs, vals):
        caps = get_caps(cfg.device(ref.device).family)
        out.append(float(caps.tx_gain_range_db[0]) if val is None else float(val))
    return out


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeviceGroup:
    """Devices driven by one ``MultiUSRP`` (``compound`` = several mboards)."""
    name: str
    family: str
    devices: tuple                    # DeviceCfg
    compound: bool

    def device_names(self) -> list:
        return [d.name for d in self.devices]

    def channel_map(self, cfg, direction: str = "rx") -> list:
        """``[(multiusrp_channel, global_index, ChannelRef)]`` for this group.

        MultiUSRP channels number the mboards' channels consecutively; each
        mboard contributes its subdev-spec entry count (else caps n_rx/n_tx).
        """
        if direction not in ("rx", "tx"):
            raise ValueError("direction must be rx or tx")
        refs = cfg.rx_refs() if direction == "rx" else cfg.tx_refs()
        gindex = {r: i for i, r in enumerate(refs)}
        out = []
        offset = 0
        for d in self.devices:
            caps = get_caps(d.family)
            spec = d.subdev_rx if direction == "rx" else d.subdev_tx
            per_mb = len(spec.split()) if spec.strip() else (caps.n_rx if direction == "rx" else caps.n_tx)
            chans = d.rx_channels if direction == "rx" else d.tx_channels
            for c in chans:
                if not 0 <= int(c) < per_mb:
                    where = "[active_device:%s] %s_channels" % (d.name, direction)
                    msg = ("%s channel %d of %s is outside the %d MultiUSRP channel(s) its "
                           "%s gives" % (direction.upper(), int(c), d.name, per_mb,
                                         ("subdev_%s" % direction) if spec.strip() else "family"))
                    raise ConfigError(msg, (Issue("error", "DEVICE_CHANNEL", msg, where),))
                ref = ChannelRef(d.name, int(c))
                out.append((offset + int(c), gindex[ref], ref))
            offset += per_mb
        return out


def _groups_compound_capable(cfg, dev) -> bool:
    caps = get_caps(dev.family)
    if caps.vendor == "sim":
        b = behaviour_caps(cfg, dev)
        return b.compound_multi_usrp and not b.single_mboard_only
    return caps.vendor == "ettus" and caps.compound_multi_usrp


def device_groups(cfg) -> list:
    """Group devices per §7.2 (group order = first-device order).

    Compound-capable Ettus devices sharing a group (explicit ``group`` key or,
    when "", the same family) form one compound MultiUSRP; B2xx and Per Vices
    get one group per device; sim devices form one compound group unless the
    emulated family cannot. An explicit group may combine families of one
    ``compound_class`` (N320 + N321, X310 daughterboards); other mixes are
    reported by ``validate_device_plan`` (DEVICE_MIXED_GROUP), not here.
    """
    buckets = {}
    order = []
    for d in cfg.devices:
        if _groups_compound_capable(cfg, d):
            key = ("g", d.group) if d.group else ("auto", "sim" if d.family == "sim" else d.family)
        else:
            key = ("dev", d.name)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(d)
    groups = []
    used = set()
    for key in order:
        devs = tuple(buckets[key])
        name = key[1] if key[0] != "dev" else devs[0].name
        base, k = name, 2
        while name in used:
            name = "%s_%d" % (base, k)
            k += 1
        used.add(name)
        groups.append(DeviceGroup(name=name, family=devs[0].family, devices=devs,
                                  compound=len(devs) > 1))
    return groups


def resolve_sync_method(cfg) -> str:
    """``sync_method``; ``auto``: one device -> none, one compound group ->
    unknown_pps, several groups -> next_pps_host."""
    sm = cfg.timing.sync_method
    if sm != "auto":
        return sm
    groups = device_groups(cfg)
    if len(groups) > 1:
        return "next_pps_host"
    if groups and groups[0].compound:
        return "unknown_pps"
    return "none"


# ---------------------------------------------------------------------------
# Device args
# ---------------------------------------------------------------------------
def parse_device_args(s) -> dict:
    """``"k=v,k2=v2,flag"`` -> ordered dict (a bare flag maps to "")."""
    out = {}
    for tok in (t.strip() for t in str(s or "").split(",")):
        if not tok:
            continue
        k, _, v = tok.partition("=")
        k, v = k.strip(), v.strip()
        if not k:
            raise ConfigError("malformed device args token %r" % tok,
                              (Issue("error", "DEVICE_ARGS", "malformed args token %r" % tok),))
        if k in out and out[k] != v:
            raise ConfigError("device args repeat %r with different values" % k,
                              (Issue("error", "DEVICE_ARGS", "conflicting args key %r" % k),))
        out[k] = v
    return out


def format_device_args(d: dict) -> str:
    return ",".join(k if v == "" else "%s=%s" % (k, v) for k, v in d.items())


def _fmt_num(x) -> str:
    x = float(x)
    if x == int(x):
        return str(int(x))
    return repr(x)


def _merge(base: dict, extra: dict, where: str) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if k in out and out[k] != v:
            same_num = False
            try:
                same_num = float(out[k]) == float(v)
            except ValueError:
                pass
            if not same_num:
                raise ConfigError("%s: args %s=%s conflicts with %s=%s" % (where, k, v, k, out[k]),
                                  (Issue("error", "DEVICE_ARGS", "args %s=%s conflicts with %s"
                                         % (k, v, out[k]), where),))
            continue
        out[k] = v
    return out


# Device-args keys shared by every mboard of a compound MultiUSRP (kept unindexed,
# conflict-checked); any other key is per-device and renamed <key><i>.
_COMPOUND_COMMON_ARGS = frozenset((
    "type", "master_clock_rate", "recv_frame_size", "send_frame_size", "num_recv_frames",
    "num_send_frames", "recv_buff_size", "send_buff_size", "use_dpdk"))
_INDEXED_KEY_RE = re.compile(r"^[A-Za-z_]+[0-9]+$")


def build_uhd_args(group: DeviceGroup, cfg) -> str:
    """Device-args string for one group's MultiUSRP.

    Compound: ``addr0=..,addr1=..,master_clock_rate=..[,<args>]`` (per-device
    args keys such as mgmt_addr become ``mgmt_addr<i>``). Single:
    ``serial=..`` / ``addr=..`` (``type=<uhd_type>`` when neither is given).
    Per Vices groups delegate to ``build_pervices_args``. Sim:
    ``type=sim,addr[N]=<device name>``.
    """
    devs = group.devices
    caps0 = get_caps(devs[0].family)
    where = "group %s" % group.name
    if caps0.vendor == "pervices":
        if len(devs) != 1:
            raise ConfigError("%s: Per Vices units are one MultiUSRP each" % where,
                              (Issue("error", "DEVICE_PERVICES_COMPOUND",
                                     "several Per Vices units in one group", where),))
        return build_pervices_args(devs[0])
    if caps0.vendor == "sim":
        args = {"type": "sim"}
        if len(devs) == 1:
            args["addr"] = devs[0].name
        else:
            for i, d in enumerate(devs):
                args["addr%d" % i] = d.name
        return format_device_args(args)

    args = {}
    if len(devs) > 1:
        for i, d in enumerate(devs):
            extra = parse_device_args(d.args)
            addr = d.addr or extra.get("addr", "")
            if not addr:
                raise ConfigError("%s: device %s needs addr for a compound MultiUSRP" % (where, d.name),
                                  (Issue("error", "DEVICE_ARGS", "compound group member %s has no "
                                         "addr" % d.name, where),))
            args["addr%d" % i] = addr
    else:
        d = devs[0]
        if d.serial:
            args["serial"] = d.serial
        if d.addr:
            args["addr"] = d.addr
    mcrs = {float(d.master_clock_rate_hz) for d in devs if d.master_clock_rate_hz}
    if len(mcrs) > 1:
        raise ConfigError("%s: members disagree on master_clock_rate_hz" % where,
                          (Issue("error", "DEVICE_MCR", "compound members disagree on the MCR", where),))
    if mcrs:
        args["master_clock_rate"] = _fmt_num(mcrs.pop())
    for i, d in enumerate(devs):
        extra = parse_device_args(d.args)
        if len(devs) > 1:
            # UHD applies an unindexed key to EVERY mboard of a compound device:
            # per-device keys (mgmt_addr, second_addr, ...) become <key><i>.
            extra.pop("addr", None)
            extra = dict((k if k in _COMPOUND_COMMON_ARGS or _INDEXED_KEY_RE.match(k)
                          else "%s%d" % (k, i), v) for k, v in extra.items())
        args = _merge(args, extra, "[active_device:%s] args" % d.name)
    if len(devs) == 1 and not any(k in args for k in ("serial", "addr", "resource", "name")) \
            and "type" not in args and caps0.uhd_type:
        args = dict([("type", caps0.uhd_type)] + list(args.items()))
    return format_device_args(args)


# Per Vices state-tree paths a config may write at construction (``crimson:<path>``).
# The driver writes them before any HeIMDALL code runs, so a TX path (tx/<ch>/pwr,
# tx/<ch>/rf/gain/val, tx/<ch>/rf/atten, tx/<ch>/dsp/*, tx/<ch>/trigger/*) or a
# trigger path would set RF state outside max_tx_gain_db / the power prediction.
# Allow-list: RX chains, timing/reference and data-link configuration only.
_PV_TREE_ALLOW = (
    re.compile(r"^rx/[a-z](/[^/]+)+$"),                     # server tree: RX chain <ch>
    re.compile(r"^time(/[^/]+)+$"),                         # server tree: clock / reference / PPS
    re.compile(r"^fpga/link(/[^/]+)+$"),                    # server tree: SFP data links
    re.compile(r"^mboards/\d+/(rx_dsps|rx_frontends|rx_codecs|rx_link|rx_dboards)(/[^/]+)*$"),
    re.compile(r"^mboards/\d+/(time|time_source|clock_source|link|link_max_rate)(/[^/]+)*$"),
)
_PV_TREE_TX_RE = re.compile(r"(^|/)(tx|tx_[a-z_]+|txrx|trigger|trig_[a-z_]+|gating)(/|$)")


def _pv_norm_path(path: str) -> str:
    p = re.sub(r"/+", "/", str(path).strip().lower())
    return p.strip("/")


def check_pervices_tree_path(path: str, where: str = "") -> str:
    """Raise ConfigError(DEVICE_PERVICES_TREE) unless ``path`` is an allowed
    RX / timing / link tree path; returns the normalised path."""
    p = _pv_norm_path(path)

    def bad(reason):
        msg = "tree path %r is not allowed: %s" % (path, reason)
        raise ConfigError("%s: %s" % (where, msg),
                          (Issue("error", "DEVICE_PERVICES_TREE", msg, where),))

    if "sob" in p:
        bad("sob is blacklisted by the driver")
    if p.startswith("fpga/trigger") or _PV_TREE_TX_RE.search(p):
        bad("TX RF state and trigger paths written at construction bypass the [active_tx] limits "
            "(max_tx_gain_db, power prediction); TX gain/power is set only by the armed TX path")
    if not any(rx.match(p) for rx in _PV_TREE_ALLOW):
        bad("only RX (rx/<ch>/..., mboards/<n>/rx_*), timing (time/..., mboards/<n>/time*, "
            "*_source) and link (fpga/link/..., mboards/<n>/link*) paths may be written")
    return p


def build_pervices_args(dev) -> str:
    """``type=<uhd_type>,addr=<mgmt>[,<args>][,crimson:<path>=<value>...]``.

    Calamine uses ``type=crimson_tng``. A compound ``addrN=`` and a disagreeing
    explicit ``type`` are rejected. Tree paths (``pervices_tree`` entries and
    ``crimson:`` keys in ``args``) must pass ``check_pervices_tree_path``: an
    allow-list of RX / timing / link paths; TX RF state, triggers and ``sob``
    are rejected (the driver writes them at construction, outside every
    [active_tx] limit).
    """
    caps = get_caps(dev.family)
    where = "[active_device:%s]" % dev.name
    if caps.vendor != "pervices":
        raise ConfigError("%s: family %s is not a Per Vices device" % (where, dev.family),
                          (Issue("error", "DEVICE_BACKEND", "not a Per Vices family", where),))
    extra = parse_device_args(dev.args)
    for k in extra:
        if k.startswith("addr") and k != "addr":
            raise ConfigError("%s: compound %s= is not supported on Per Vices (one MultiUSRP per "
                              "unit)" % (where, k),
                              (Issue("error", "DEVICE_PERVICES_COMPOUND",
                                     "compound %s= rejected" % k, where),))
    if "type" in extra and extra["type"] != caps.uhd_type:
        raise ConfigError("%s: args type=%s disagrees with family %s (type=%s)"
                          % (where, extra["type"], dev.family, caps.uhd_type),
                          (Issue("error", "DEVICE_PERVICES_TYPE", "type=%s disagrees with %s"
                                 % (extra["type"], caps.uhd_type), where),))
    if dev.addr and extra.get("addr") and extra["addr"] != dev.addr:
        raise ConfigError("%s: addr key and args addr= disagree" % where,
                          (Issue("error", "DEVICE_ARGS", "addr disagrees with args addr=", where),))
    args = {"type": caps.uhd_type}
    addr = dev.addr or extra.get("addr", "")
    if addr:
        args["addr"] = addr
    for k, v in extra.items():
        if k in ("type", "addr"):
            continue
        if k.lower().startswith("crimson:"):
            check_pervices_tree_path(k[len("crimson:"):], where + " args")
        args[k] = v
    for path, value in dev.pervices_tree:
        check_pervices_tree_path(path, where + " pervices_tree")
        if any(ch in path for ch in ",=") or any(ch in value for ch in ",="):
            raise ConfigError("%s: pervices_tree entry %r=%r contains ',' or '='" % (where, path, value),
                              (Issue("error", "DEVICE_PERVICES_TREE", "tree entry contains , or =",
                                     where),))
        key = "crimson:" + path
        if key in args and args[key] != value:
            raise ConfigError("%s: tree path %s set twice" % (where, path),
                              (Issue("error", "DEVICE_ARGS", "tree path set twice", where),))
        args[key] = value
    return format_device_args(args)


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------
def _pervices_rate_n(rate_base, fs):
    """Integer N with rate_base/N == fs within 1 ppm (N in 1..65534), else None."""
    if fs <= 0:
        return None
    n = int(round(rate_base / fs))
    if not 1 <= n <= 65534:
        return None
    if abs(rate_base / n - fs) <= 1e-6 * fs:
        return n
    return None


def _coherent_use(cfg) -> bool:
    return cfg.output.output_mode != "raw" or cfg.tx_enabled()


def validate_device_plan(cfg) -> list:
    """Hardware plan rules (§7.3) -> ``[Issue]``.

    The network budget rule (``RATE``) is evaluated by ``rate_budget``; call
    both (``check_all`` does).
    """
    out = []

    def add(level, code, msg, where=""):
        out.append(Issue(level, code, msg, where))

    if not cfg.devices:
        add("error", "DEVICE_CHANNEL", "no devices configured", "[active_devices]")
        return out
    backend = cfg.active.backend
    fs = float(cfg.rf.sample_rate_hz)
    fc = float(cfg.rf.center_freq_hz)
    bw = cfg.rf.effective_bandwidth_hz()
    timed_cfg = cfg.timing.timed_tune
    groups = device_groups(cfg)
    rx_refs, tx_refs = cfg.rx_refs(), cfg.tx_refs()

    caps_by = {}
    for d in cfg.devices:
        w = "[active_device:%s]" % d.name
        try:
            caps_by[d.name] = get_caps(d.family)
        except ConfigError:
            add("error", "DEVICE_CHANNEL", "unknown family %r" % d.family, w + " family")
    if len(caps_by) != len(cfg.devices):
        return out

    # backend / vendor compatibility
    for d in cfg.devices:
        vendor = caps_by[d.name].vendor
        w = "[active_device:%s] family" % d.name
        if backend == "uhd" and vendor != "ettus":
            add("error", "DEVICE_BACKEND", "backend uhd drives Ettus/NI families only, not %s" % d.family, w)
        elif backend == "pervices" and vendor != "pervices":
            add("error", "DEVICE_BACKEND", "backend pervices drives Per Vices families only, not %s"
                % d.family, w)
        elif backend == "gnuradio" and vendor == "sim":
            add("error", "DEVICE_BACKEND", "backend gnuradio needs a hardware family, not sim", w)
    if backend == "gnuradio" and len({caps_by[d.name].vendor for d in cfg.devices}) > 1:
        add("error", "DEVICE_BACKEND", "gnuradio cannot mix Ettus and Per Vices devices in one "
                                       "flowgraph (gnuradiopv is a separate build)", "[active_devices]")

    # roles
    masters = [d.name for d in cfg.devices if d.role == "master"]
    if len(masters) > 1:
        add("error", "DEVICE_ROLE", "exactly one master allowed, got %s" % ", ".join(masters))

    # channels, frequency, gains, sources, MCR
    for d in cfg.devices:
        caps = caps_by[d.name]
        w = "[active_device:%s]" % d.name
        for key, n_max in (("rx_channels", caps.n_rx), ("tx_channels", caps.n_tx)):
            chans = list(getattr(d, key))
            bad = [c for c in chans if not 0 <= c < n_max]
            if bad:
                add("error", "DEVICE_CHANNEL", "%s %s outside 0..%d for %s"
                    % (key, bad, n_max - 1, d.family), w + " " + key)
            if len(set(chans)) != len(chans):
                add("error", "DEVICE_CHANNEL", "duplicate channel in %s" % key, w + " " + key)
            spec = d.subdev_rx if key == "rx_channels" else d.subdev_tx
            if spec.strip():
                n_spec = len(spec.split())
                over = [c for c in chans if c >= n_spec and 0 <= c < n_max]
                if over:
                    add("error", "DEVICE_CHANNEL", "%s %s outside the %d entr%s of %s %r (the "
                        "MultiUSRP channel would land on another port or mboard)"
                        % (key, over, n_spec, "y" if n_spec == 1 else "ies",
                           "subdev_rx" if key == "rx_channels" else "subdev_tx", spec),
                        w + " " + key)
            if d.family == "cyan" and any(c >= 8 for c in chans):
                add("info", "DEVICE_CHANNEL", "channels >= 8 need a 16-channel Cyan unit",
                    w + " " + key)
        if not d.rx_channels and not d.tx_channels:
            add("warning", "DEVICE_CHANNEL", "device has no RX or TX channels", w)
        if not caps.freq_min_hz <= fc <= caps.freq_max_hz:
            add("error", "DEVICE_FREQ", "center_freq %.6g Hz outside %s range %.6g..%.6g Hz"
                % (fc, d.family, caps.freq_min_hz, caps.freq_max_hz), "[active_rf] center_freq_hz")
        elif not caps.freq_min_hz <= fc - bw / 2 or not fc + bw / 2 <= caps.freq_max_hz:
            add("warning", "DEVICE_FREQ", "occupied band extends outside the %s range" % d.family,
                "[active_rf] bandwidth_hz")
        for edge in caps.band_edges_hz:
            if fc - bw / 2 < edge < fc + bw / 2:
                add("warning", "DEVICE_PERVICES_BAND_EDGE" if caps.vendor == "pervices" else "DEVICE_FREQ",
                    "occupied band straddles the %s analog band edge at %.6g Hz" % (d.family, edge),
                    "[active_rf] center_freq_hz")
        clk, tim = effective_sources(cfg, d)
        if clk not in caps.clock_sources:
            code = "DEVICE_PERVICES_GPSDO" if caps.vendor == "pervices" and clk == "gpsdo" else "DEVICE_SOURCE"
            add("error", code, "clock_source %s not supported by %s (valid: %s)"
                % (clk, d.family, ", ".join(caps.clock_sources)), w + " clock_source")
        if tim not in caps.time_sources:
            code = "DEVICE_PERVICES_GPSDO" if caps.vendor == "pervices" and tim == "gpsdo" else "DEVICE_SOURCE"
            add("error", code, "time_source %s not supported by %s (valid: %s)"
                % (tim, d.family, ", ".join(caps.time_sources)), w + " time_source")
        mcr = float(d.master_clock_rate_hz or 0.0)
        if mcr:
            if caps.mcr_choices_hz:
                if not any(abs(mcr - c) <= 1.0 for c in caps.mcr_choices_hz):
                    add("error", "DEVICE_MCR", "master_clock_rate_hz %.6g not in %s"
                        % (mcr, ", ".join("%.6g" % c for c in caps.mcr_choices_hz)),
                        w + " master_clock_rate_hz")
            elif caps.mcr_range_hz:
                if not caps.mcr_range_hz[0] <= mcr <= caps.mcr_range_hz[1]:
                    add("error", "DEVICE_MCR", "master_clock_rate_hz %.6g outside %.6g..%.6g"
                        % (mcr, caps.mcr_range_hz[0], caps.mcr_range_hz[1]), w + " master_clock_rate_hz")
            else:
                add("warning", "DEVICE_MCR", "master_clock_rate_hz is ignored for %s (fixed rate "
                                             "base)" % d.family, w + " master_clock_rate_hz")
        # LO keys
        if d.lo_source == "external" and not caps.lo_import:
            add("error", "DEVICE_LO", "lo_source external: %s cannot import an LO" % d.family,
                w + " lo_source")
        if d.lo_source == "companion" and caps.lo_share_kind not in ("twinrx", "sim"):
            add("error", "DEVICE_LO", "lo_source companion is TwinRX-only", w + " lo_source")
        if d.lo_export and not caps.lo_export:
            add("error", "DEVICE_LO", "lo_export: %s cannot export its LO" % d.family, w + " lo_export")
        if d.max_tx_dbm_override is not None and d.max_tx_dbm_override > caps.tx_max_dbm_upper:
            add("error", "DEVICE_TX_OVERRIDE", "max_tx_dbm_override %.4g exceeds the %s bound %.4g dBm "
                "(it may only be stricter)" % (d.max_tx_dbm_override, d.family, caps.tx_max_dbm_upper),
                w + " max_tx_dbm_override")
        if cfg.timing.stream_lead_s < caps.min_tx_lead_s:
            add("error", "DEVICE_LEAD", "stream_lead_s below the %s minimum TX lead %.4g s"
                % (d.family, caps.min_tx_lead_s), "[active_timing] stream_lead_s")
        if d.pervices_tree and caps.vendor != "pervices":
            add("warning", "DEVICE_ARGS", "pervices_tree is ignored for %s" % d.family,
                w + " pervices_tree")

    # gains (per global channel)
    for i, ref in enumerate(rx_refs):
        if i >= len(cfg.rf.rx_gain_db):
            break
        lo, hi, _st = caps_by[ref.device].rx_gain_range_db
        g = cfg.rf.rx_gain_db[i]
        if not lo <= g <= hi:
            add("error", "DEVICE_GAIN", "RX %d (%s) gain %.4g dB outside %.4g..%.4g"
                % (i, ref, g, lo, hi), "[active_rf] rx_gain_db")
    for i, ref in enumerate(tx_refs):
        if i >= len(cfg.tx.max_tx_gain_db):
            break
        g = cfg.tx.max_tx_gain_db[i]
        if g is None:
            continue
        lo, hi, _st = caps_by[ref.device].tx_gain_range_db
        if not lo <= g <= hi:
            add("error", "DEVICE_GAIN", "TX %d (%s) max_tx_gain_db %.4g outside %.4g..%.4g"
                % (i, ref, g, lo, hi), "[active_tx] max_tx_gain_db")

    # sample rate
    # fs is bounded by the largest achievable sample rate (fs = MCR is valid on
    # UHD; Per Vices rate_base); above the ANALOG bandwidth (max_bw_hz) the band
    # edges roll off, which is a warning only.
    max_fs_by = {d.name: max_sample_rate_hz(d, cfg) for d in cfg.devices}
    lim_name = min(max_fs_by, key=lambda n: max_fs_by[n])
    max_fs = max_fs_by[lim_name]
    if not 0 < fs <= max_fs * (1 + 1e-9):
        add("error", "DEVICE_RATE", "sample_rate_hz %.6g outside (0, %.6g] (largest sample rate of "
            "%s, %s)" % (fs, max_fs, lim_name, cfg.device(lim_name).family),
            "[active_rf] sample_rate_hz")
    else:
        min_bw = min(c.max_bw_hz for c in caps_by.values())
        for what, val, key in (("sample_rate_hz", fs, "sample_rate_hz"),
                               ("bandwidth", bw, "bandwidth_hz")):
            if val > min_bw * (1 + 1e-9):
                add("warning", "DEVICE_RATE", "%s %.6g exceeds the smallest device analog bandwidth "
                    "%.6g Hz: the band edges roll off" % (what, val, min_bw),
                    "[active_rf] %s" % key)
                break
    for d in cfg.devices:
        caps = caps_by[d.name]
        w = "[active_device:%s]" % d.name
        if caps.vendor == "pervices":
            n = _pervices_rate_n(caps.rate_base_hz, fs)
            if n is None:
                add("error", "DEVICE_PERVICES_RATE", "sample_rate_hz %.9g is not %s rate_base %.6g / N "
                    "(N integer 1..65534, 1 ppm)" % (fs, d.family, caps.rate_base_hz),
                    "[active_rf] sample_rate_hz")
            reduced = [c for c in d.tx_channels if c in caps.tx_reduced_rate_chans]
            if reduced and fs > caps.rate_base_hz / 4 * (1 + 1e-9) and not d.pervices_full_tx_image:
                add("error", "DEVICE_PERVICES_TX_RATE", "TX channels %s are limited to rate_base/4 = "
                    "%.6g S/s without pervices_full_tx_image = 1" % (reduced, caps.rate_base_hz / 4),
                    w + " tx_channels")
        elif caps.vendor == "ettus" and fs > 0:
            mcr = effective_mcr_hz(d, cfg)
            if mcr:
                ratio = mcr / fs
                if abs(ratio - round(ratio)) > 1e-6 * ratio:
                    add("warning", "DEVICE_RATE", "sample_rate_hz %.9g is not MCR %.9g / integer: UHD "
                        "will coerce the rate" % (fs, mcr), "[active_rf] sample_rate_hz")

    # family-specific rules
    ettus_b2 = [d for d in cfg.devices if d.family == "b210"]
    for d in ettus_b2:
        w = "[active_device:%s]" % d.name
        clk, tim = effective_sources(cfg, d)
        if clk == "external" and d.gpsdo_fitted:
            add("error", "DEVICE_B2XX_GPSDO_EXT", "B2xx with a fitted GPSDO cannot also take an external "
                "10 MHz", w + " clock_source")
        if "gpsdo" in (clk, tim) and not d.gpsdo_fitted:
            add("error", "DEVICE_SOURCE", "gpsdo source needs gpsdo_fitted = 1", w + " gpsdo_fitted")
        nch = max(len(d.rx_channels), len(d.tx_channels))
        mcr = float(d.master_clock_rate_hz or 0.0)
        if nch >= 2 and (fs > 30.72e6 * (1 + 1e-9) or mcr > 30.72e6 * (1 + 1e-9)):
            add("error", "DEVICE_B2XX_RATE", "B2xx with two channels is limited to 30.72 MS/s "
                "(sample rate and MCR)", "[active_rf] sample_rate_hz")
    if len(ettus_b2) > 1:
        add("warning", "DEVICE_B2XX_MULTI", "several B2xx: separate groups aligned by host next_pps "
            "[verify one-process multi-B2xx]", "[active_devices]")
        for d in ettus_b2:
            clk, tim = effective_sources(cfg, d)
            if clk != "external" or tim != "external" or not d.master_clock_rate_hz:
                add("error", "DEVICE_B2XX_MULTI", "multi-B2xx requires external ref + PPS and a pinned "
                    "master_clock_rate_hz", "[active_device:%s]" % d.name)

    x410 = [d for d in cfg.devices if d.family == "x410"]
    if x410 and len(cfg.devices) > 1:
        level = "error" if _coherent_use(cfg) else "warning"
        add(level, "DEVICE_X410_MULTI", "X410 is single-device coherent only: %d devices configured"
            % len(cfg.devices), "[active_devices]")
    if x410 and timed_cfg == "on":
        add("error", "DEVICE_X410_TIMED_TUNE", "X410 (ZBX) has no timed tune", "[active_timing] timed_tune")

    for d in cfg.devices:
        if caps_by[d.name].vendor == "ettus" and d.family.startswith("n3"):
            clk, tim = effective_sources(cfg, d)
            if clk == "external" and tim == "gpsdo":
                add("error", "DEVICE_N3XX_EXT_GPSDO", "N3xx cannot combine an external clock with gpsdo "
                    "time", "[active_device:%s]" % d.name)

    # timed tune support (behaviour caps; the sim follows emulate_family)
    if timed_cfg == "on":
        for d in cfg.devices:
            b = behaviour_caps(cfg, d)
            if not b.timed_tune:
                code = ("DEVICE_PERVICES_TIMED_TUNE" if b.vendor == "pervices" else
                        "DEVICE_X410_TIMED_TUNE" if b.family == "x410" else "DEVICE_TIMED_TUNE")
                if code == "DEVICE_X410_TIMED_TUNE" and x410:
                    continue  # already reported
                add("error", code, "timed_tune = on but %s cannot tune at a timed command"
                    % b.family, "[active_timing] timed_tune")
    elif timed_cfg == "auto" and not resolve_timed_tune(cfg):
        add("info", "DEVICE_TIMED_TUNE", "timed tune unavailable: every retune needs a recalibration",
            "[active_timing] timed_tune")

    # groups
    for g in groups:
        fams = {d.family for d in g.devices}
        if len({compound_class(f) for f in fams}) > 1:
            add("error", "DEVICE_MIXED_GROUP", "group %s mixes incompatible families %s"
                % (g.name, ", ".join(sorted(fams))), "[active_device:*] group")
        if g.compound:
            mcrs = {float(d.master_clock_rate_hz) for d in g.devices if d.master_clock_rate_hz}
            if len(mcrs) > 1:
                add("error", "DEVICE_MCR", "group %s members disagree on master_clock_rate_hz" % g.name,
                    "[active_device:*] master_clock_rate_hz")
            unpinned = [d.name for d in g.devices if not d.master_clock_rate_hz]
            if mcrs and unpinned:
                add("error", "DEVICE_MCR", "group %s: UHD applies one master_clock_rate (%.9g) to "
                    "every mboard of the compound MultiUSRP, but %s do%s not pin it; pin the same "
                    "master_clock_rate_hz on every member or on none"
                    % (g.name, next(iter(mcrs)), ", ".join(unpinned),
                       "es" if len(unpinned) == 1 else ""),
                    "[active_device:%s] master_clock_rate_hz" % unpinned[0])
    explicit = {}
    for d in cfg.devices:
        if d.group:
            explicit.setdefault(d.group, []).append(d)
    for gname, members in explicit.items():
        fams = {m.family for m in members}
        pv = [m for m in members if caps_by[m.name].vendor == "pervices"]
        incompatible = len({compound_class(f) for f in fams}) > 1
        already = any(len({compound_class(x.family) for x in g.devices}) > 1 for g in groups
                      if g.name == gname)
        if incompatible and not already:
            add("error", "DEVICE_MIXED_GROUP", "explicit group %s mixes incompatible families %s"
                % (gname, ", ".join(sorted(fams))), "[active_device:*] group")
        if len(pv) > 1:
            add("error", "DEVICE_PERVICES_COMPOUND", "group %s puts several Per Vices units in one "
                "MultiUSRP (one per unit is required)" % gname, "[active_device:*] group")
        others = [m for m in members if not _groups_compound_capable(cfg, m)
                  and caps_by[m.name].vendor != "pervices"]
        if len(others) > 1:
            add("warning", "DEVICE_GROUP", "group %s ignored for non-compound family %s"
                % (gname, others[0].family), "[active_device:*] group")

    sync = resolve_sync_method(cfg)
    if len(cfg.devices) > 1:
        # Several mboards (separate groups OR one compound MultiUSRP) each need the
        # shared 10 MHz + PPS; a free-running internal reference is incoherent.
        what = ("%d groups" % len(groups) if len(groups) > 1 else
                "a compound group of %d mboards" % len(cfg.devices))
        if sync == "none":
            add("error", "DEVICE_NO_SHARED_REF", "%s with sync_method none cannot be "
                "time-aligned" % what, "[active_timing] sync_method")
        for d in cfg.devices:
            clk, tim = effective_sources(cfg, d)
            if clk == "internal" or tim == "internal":
                add("error", "DEVICE_NO_SHARED_REF", "%s need a shared ref and PPS; %s uses "
                    "clock %s / time %s" % (what, d.name, clk, tim),
                    "[active_device:%s]" % d.name)

    # Per Vices wire format: 16-bit I/Q only (research_pervices §2.3/§5, PVAN-11)
    otw = cfg.rf.otw_format
    for d in cfg.devices:
        if caps_by[d.name].vendor == "pervices" and otw != "sc16":
            add("error", "DEVICE_PERVICES_OTW", "otw_format %s: Per Vices units carry 16-bit I/Q "
                "(sc16) only on the wire [verify]" % otw, "[active_rf] otw_format")

    # Per Vices args (type / tree / compound)
    for d in cfg.devices:
        if caps_by[d.name].vendor == "pervices":
            try:
                build_pervices_args(d)
            except ConfigError as exc:
                out.extend(exc.issues)

    # LO plan sanity
    plan = resolve_lo_plan(cfg)
    if plan in ("n321_star", "twinrx") and cfg.timing.lo_master:
        try:
            m = cfg.device(cfg.timing.lo_master)
        except ConfigError:
            m = None
        if m is not None and not caps_by[m.name].lo_export:
            add("error", "DEVICE_LO", "lo_master %s (%s) cannot export an LO" % (m.name, m.family),
                "[active_timing] lo_master")
    if plan in ("n321_star", "twinrx"):
        mem = lo_share_members(cfg, plan)
        needs_exporter = (plan == "n321_star" or
                          any(cfg.device(n).lo_source == "external" for n in mem["importers"]))
        if mem["eligible"] and needs_exporter and not mem["exporters"]:
            add("error", "DEVICE_LO", "lo_plan %s has no LO exporter (set lo_export = 1 or "
                "lo_master on an LO-export-capable device)" % plan, "[active_timing] lo_plan")
        for n in mem["outside"]:
            add("error", "DEVICE_LO", "%s is not part of the %s LO share (neither exports nor "
                "imports the LO): its phase is random after every retune; set lo_source = %s"
                % (n, plan, "external" if plan == "n321_star" else "companion or external"),
                "[active_device:%s] lo_source" % n)
        if mem["eligible"] and not mem["importers"]:
            add("warning", "DEVICE_LO", "lo_plan %s has no LO importer (lo_source = %s)"
                % (plan, "external" if plan == "n321_star" else "companion/external"),
                "[active_timing] lo_plan")
        if not mem["eligible"]:
            add("error", "DEVICE_LO", "lo_plan %s but no device can take part in it" % plan,
                "[active_timing] lo_plan")
    if plan == "n321_star":
        lo_min = max((caps_by[d.name].lo_share_min_hz for d in cfg.devices), default=0.0)
        if fc < lo_min:
            add("warning", "DEVICE_LO", "N321 LO sharing works only above %.6g Hz: phase is random "
                "after every tune here" % lo_min, "[active_rf] center_freq_hz")
        if len(cfg.devices) > 16:
            add("error", "DEVICE_LO", "n321_star supports at most 16 devices", "[active_devices]")

    # N310 external LO, X440
    if any(d.family == "n310" and d.lo_source == "external" for d in cfg.devices):
        add("info", "DEVICE_N310_180", "N310 external LO: 180 degree ambiguity; calibration is "
            "mandatory after every init/retune", "[active_device:*] lo_source")
    if any(d.family == "x440" for d in cfg.devices):
        add("info", "DEVICE_X440", "X440: sample-based coherence, about 2 degrees rms device-to-device "
            "[verify]", "[active_devices]")

    # recal_on_tune
    pat = phase_after_tune(cfg)
    if not cfg.calibration.recal_on_tune and pat not in ("repeatable", "shared_lo", "deterministic"):
        add("error", "DEVICE_RECAL", "recal_on_tune = 0 but the phase after a retune is %s" % pat,
            "[active_calibration] recal_on_tune")
    return out


# ---------------------------------------------------------------------------
# Rate / network budget
# ---------------------------------------------------------------------------
_OTW_BYTES = {"sc16": 4.0, "sc8": 2.0, "sc12": 3.0}
_LINK_EFFICIENCY = 0.98


def bytes_per_sample(otw: str, family: str, direction: str) -> float:
    """Wire bytes per complex sample (Cyan 3G: RX 3, TX 4 regardless of otw;
    other Per Vices families: 4 (sc16) regardless of otw, so the budget stays
    conservative)."""
    if direction not in ("rx", "tx"):
        raise ValueError("direction must be rx or tx")
    if family == "cyan_hb":
        return 3.0 if direction == "rx" else 4.0
    fcaps = CAPS.get(str(family).lower())
    if fcaps is not None and fcaps.vendor == "pervices":
        return 4.0
    try:
        return _OTW_BYTES[otw]
    except KeyError:
        raise ValueError("unknown otw_format %r" % otw) from None


def port_traffic(cfg) -> list:
    """``[(device, port_label, direction, gbps, capacity_gbps)]`` per network port."""
    rows = []
    fs = float(cfg.rf.sample_rate_hz)
    otw = cfg.rf.otw_format
    for d in cfg.devices:
        caps = get_caps(d.family)
        if caps.vendor == "sim":
            continue
        for direction, chans in (("rx", d.rx_channels), ("tx", d.tx_channels)):
            if not chans:
                continue
            try:
                bps = bytes_per_sample(otw, d.family, direction)
            except ValueError:
                continue
            per_chan = bps * fs * 8.0 / 1e9
            if d.family in ("crimson_tng", "calamine"):
                ports = {"SFP+A": 0.0, "SFP+B": 0.0}
                for c in chans:
                    ports["SFP+A" if c % 2 == 0 else "SFP+B"] += per_chan
                cap = float(d.link_gbps) * _LINK_EFFICIENCY
                for label, g in ports.items():
                    if g > 0:
                        rows.append((d.name, label, direction, g, cap))
            elif d.family in ("cyan", "cyan_hb"):
                nports = max(1, int(d.link_ports))
                ports = [0.0] * nports
                for c in chans:
                    ports[(int(c) // 2) % nports] += per_chan
                cap = float(d.link_gbps) * _LINK_EFFICIENCY
                for k, g in enumerate(ports):
                    if g > 0:
                        rows.append((d.name, "port%d" % k, direction, g, cap))
            else:
                cap = float(d.link_ports) * float(d.link_gbps) * _LINK_EFFICIENCY
                rows.append((d.name, "link", direction, per_chan * len(chans), cap))
    return rows


def rate_budget(cfg) -> list:
    """Network budget (§7.4) -> ``[Issue]``: > 100 % of usable capacity is an
    error ``RATE``, > 80 % a warning."""
    out = []
    for dev, port, direction, gbps, cap in port_traffic(cfg):
        if cap <= 0:
            out.append(Issue("error", "RATE", "%s %s has no capacity" % (dev, port),
                             "[active_device:%s] link_gbps" % dev))
            continue
        load = gbps / cap
        msg = ("%s %s %s: %.2f Gbps of %.2f usable (%.0f %%)"
               % (dev, port, direction.upper(), gbps, cap, 100.0 * load))
        if load > 1.0:
            out.append(Issue("error", "RATE", msg + ": exceeds the link (reduce the rate, channels "
                             "or otw_format; MTU 9000 required)", "[active_device:%s]" % dev))
        elif load > 0.8:
            out.append(Issue("warning", "RATE", msg + ": above 80 %%", "[active_device:%s]" % dev))
    return out


def caps_to_dict(caps: DeviceCaps) -> dict:
    """JSON-ready dict of one caps entry (``heimdall_active caps --json``)."""
    from dataclasses import asdict
    d = asdict(caps)
    for k, v in d.items():
        if isinstance(v, float) and not math.isfinite(v):
            d[k] = None
    return d
