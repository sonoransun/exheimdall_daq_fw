"""
    HeIMDALL "active" capability: deterministic RF system simulator (``SimBackend``).

    The test vehicle of the active capability (spec 13.2). It implements the
    full ``ActiveBackend`` interface on a virtual true clock - nothing ever
    sleeps on the wall clock - and models, from independent seeded random
    streams (adding one impairment never changes another):

      - per-device clocks ``t_dev = anchor_t + (T - anchor_T)(1 + ppm 1e-6)``
        with exact ``Fraction`` arithmetic; ppm = 0 on a shared external
        reference, ~N(0, ref_ppm_sigma) on an internal one (1e-3 of that on a
        GPSDO); PPS edges at integer true seconds (``pps_missing`` suppresses
        them); ``set_time_next_pps`` / ``set_time_unknown_pps`` latch the time
        at the next edge; ``set_time_now`` on a multi-device configuration
        applies a host latency ~U(0, 10 ms) per device and per call (one call on
        all devices or one call per device: the same hazard). ``pps_skew_ticks``: a slave loads a
        PPS-latched time that many ticks after the edge (an FPGA time-load
        latency), so its clock lags the master and the skew is visible in
        ``time_last_pps`` and in the sample alignment;
      - unshared references: a baseband phase ramp ``2 pi fc dppm 1e-6 t`` and
        a sample-clock slip ``fs dppm``;
      - LO phase per (device, chain, direction), redrawn on each tune by the
        family behaviour (``[active_sim] emulate_family``) and the plan:
        ``shared_lo`` (common phase + static path), ``repeatable`` (timed tune on
        a repeatable family), ``ambiguous_180``, ``deterministic``, ``random``;
        LO sharing is physical only once the importer's ``set_lo_source`` and
        the exporter's ``set_lo_export`` were called;
      - static RX/TX chain delays (integer + fraction, applied by
        ``frac_delay_fft`` with guard samples), log-normal gains, a
        gain-dependent and a temperature-dependent phase;
      - the scene: ``SimEmitter`` waveforms arriving per element with the TRUE
        delay (far field: plane wave; near field: spherical with 1/r), element
        patterns, nearest-neighbour mutual coupling; built WITHOUT
        ``steering_ff`` so the convention tests stay independent;
      - levels: port dBm + element gain + front-end gain -> dBFS with
        ``rx_fullscale_dbm``; AWGN at ``noise_dbfs``; clipping sets the
        overdrive bit;
      - calibration paths: injection (``h_inj``), TX loopback coupler
        (``h_cpl``) into ``loopback_rx_chan``, cabled TX n -> RX n;
      - TX: sc16 DAC, hidden true full-scale power (the conservative bound
        minus ``tx_true_offset_db``), TX impairments, Rapp PA AM/AM + AM/PM per
        amplifier stage, PA enable lines (and USRP ATR), the TX load wiring
        (antenna / dummy load / cabled), reverse waves (antenna: ``b = S a`` on
        EVERY port, idle ports included; dummy load: Gamma(load_vswr) per driven
        port), TX->RX leakage ``turnaround_samples`` later. A burst is modelled at
        the PHYSICAL TX rate (``set_rate``), whatever the permit declared; TX gains
        are coerced DOWN to the gain step (never above the request);
      - ``damage_log``: ANY RX port above its damage level (front-end
        ``max_in`` or caps) appends an entry and emits EVT_ACTIVE_SIM_DAMAGE;
      - timing faults (late, underflow storms with Ettus / Per Vices
        semantics, TX hang), telemetry with heating, virtual GPIO lines,
        injected faults, a call log and the ground truth (``truth``);
      - UHD timing semantics: ``strict_rx_timing`` (default True) makes a timed
        capture whose start has passed fail with ``late_command`` (no samples:
        a device cannot return the past). A TX-cal loopback is captured the
        realistic way: ``rx_start(t0)`` before ``tx_burst``, then ``rx_read``.

    Production code never reads ``truth``; tests do.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

import active_common as ac
from active_common import (C0, EVT_ACTIVE_OVERFLOW, EVT_ACTIVE_SIM_DAMAGE, EVT_ACTIVE_UNDERFLOW,
                           AsyncEvent, BackendError, ConfigError, DevTime, GeometryError, HardwareFault,
                           NotSupported, TxResult, as_fraction, rng_stream)
from active_backend import ActiveBackend, RxCapture, TuneReport
import active_devices as adev
import active_dsp as adsp
import active_frontend as afe
import active_geometry as ageo

logger = logging.getLogger("heimdall.active.sim")

GUARD = 64                       # block guard G (samples) for FFT fractional delays
INTERNAL_OVERSAMPLE = 1          # delays are applied by FFT phase ramps (no oversampling)
LO_LEAK_DBC = -50.0              # PA disabled: only LO leakage at -50 dBc of full scale
REF_LOCK_SETTLE_S = Fraction(3, 10)
SET_TIME_NOW_LATENCY_S = 0.010   # host latency ~U(0, 10 ms) per device
BOOT_OFFSET_MAX_S = 100.0        # device clocks boot at U(0, 100) s
T0 = Fraction(41, 4)             # virtual true time at construction (s)
SIM_EPOCH_UNIX = 1_800_000_000   # wallclock() = SIM_EPOCH_UNIX + T
GPS_EPOCH_UNIX = 1_800_000_000   # the GPSDO 'gps_time' sensor = this + floor(T)
RX_BUFFER_S = 2.0                # host RX buffer: a reader further behind overflows
TEMP_TAU_S = 60.0
DETECTOR_HOLD_S = 1.0            # power detector holds the burst reading this long
MIN_TX_LEAD_S = 1e-3             # a burst starting closer than this to "now" is late
CMD_MIN_LEAD_S = Fraction(1, 10_000)  # a timed tune needs its command time this far ahead (else late)
ZC_LEN = 4093                    # prime: periodic ZC emitter / injection waveform
LFM_PERIOD = 4096
NOISE_SEG = 4096
NOISE_HALF_TAPS = 64
UNDERFLOW_GAP = 64               # zero-fill length of one underflow (samples)
TX_HISTORY_S = 60.0
TX_HISTORY_MAX = 256
INJ_TONE_FRAC = 0.125            # tone injection at fs/8
FAULT_KINDS = ("ref_unlock", "lo_unlock", "pps_missing", "overflow", "underflow_storm", "late",
               "reverse_power", "interlock_open", "dummy_load_lost", "over_temp", "tx_hang",
               "telemetry_stale")
SIM_FLAVOURS = ("ettus", "pervices")
_WIRING = {"dummy_load": "dummy_load", "cabled": "cabled", "radiate": "antenna"}


@dataclass
class SimEmitter:
    """A simulated emitter: power at an isotropic element at the array origin."""
    az_deg: float
    el_deg: float
    range_m: float = 0.0              # 0 = far field
    power_dbm: float = -60.0
    waveform: str = "noise"           # noise | tone | zc | lfm
    f_offset_hz: float = 0.0
    bw_frac: float = 0.8              # noise / lfm bandwidth as a fraction of fs


@dataclass
class SimTruth:
    """Ground truth (tests only; never read by production code).

    ``rx_gain`` / ``tx_gain``: complex chain gain WITHOUT the LO phase
    (amplitude, static path phase, gain- and temperature-dependent phase);
    the chain response is ``gain * exp(j lo_phase)``. Delays in samples
    (positive = late), after any device integer-delay correction.
    """
    rx_delay_samples: np.ndarray
    rx_gain: np.ndarray
    tx_delay_samples: np.ndarray
    tx_gain: np.ndarray
    lo_phase_rx: np.ndarray
    lo_phase_tx: np.ndarray
    iq_conjugate: dict
    ref_ppm: dict
    pps_skew_ticks: dict
    injection_paths: np.ndarray
    coupler_paths: np.ndarray
    turnaround_samples: int
    p_fs_true: object                 # callable (chan, f_hz, tx_gain_db) -> dBm


def _u(seed, *keys) -> float:
    return float(rng_stream(seed, *keys).uniform(-math.pi, math.pi))


def _frac_ppm(ppm: float) -> Fraction:
    return Fraction(float(ppm)) / 1_000_000


def load_path_table(path, n, f_hz):
    """``chan,freq_hz,mag_db,phase_deg,delay_s`` (spec 6.3) at ``f_hz`` ->
    ``(h (n,) complex incl. exp(-j 2 pi f delay), delay_s (n,))``; ideal (1, 0) for
    channels without rows; mag / phase / delay interpolated linearly in f (clamped)."""
    h = np.ones(n, dtype=np.complex128)
    dl = np.zeros(n)
    if not path:
        return h, dl
    rows = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for ln, line in enumerate(fh):
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = [p.strip() for p in s.split(",")]
                if parts[0].lower() == "chan":
                    continue
                if len(parts) != 5:
                    raise ValueError("line %d: expected 5 fields" % (ln + 1))
                c = int(parts[0])
                rows.setdefault(c, []).append(tuple(float(p) for p in parts[1:]))
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read path table %s: %s" % (path, exc)) from exc
    f = float(f_hz)
    for c, rs in rows.items():
        if not 0 <= c < n:
            continue
        rs.sort()
        fr = np.array([r[0] for r in rs])
        mag = float(np.interp(f, fr, [r[1] for r in rs]))
        ph = float(np.interp(f, fr, [r[2] for r in rs]))
        d = float(np.interp(f, fr, [r[3] for r in rs]))
        h[c] = 10.0 ** (mag / 20.0) * np.exp(1j * math.radians(ph)) * np.exp(-2j * math.pi * f * d)
        dl[c] = d
    return h, dl


class SimBackend(ActiveBackend):
    """Deterministic physics simulation of the full RF system (spec 4.13 / 13.2)."""

    name = "sim"
    is_simulation = True

    def __init__(self, cfg, event_log=None, geometry=None, frontend=None, seed=None, flavour=None):
        super().__init__(cfg, event_log)
        sc = cfg.sim
        self.sc = sc
        self.seed = int(sc.seed if seed is None else seed)
        self.bcaps = {d.name: adev.behaviour_caps(cfg, d) for d in cfg.devices}
        if flavour is None:
            flavour = "pervices" if any(c.vendor == "pervices" for c in self.bcaps.values()) else "ettus"
        if flavour not in SIM_FLAVOURS:
            raise ValueError("flavour must be one of %s" % (SIM_FLAVOURS,))
        self.flavour = flavour
        self.calls = []
        self.damage_log = []
        self.strict_rx_timing = True         # a capture starting in the past fails "late_command" (UHD)
        self.rx_buffer_s = RX_BUFFER_S
        self._T = T0
        self._master = cfg.master_device().name if cfg.devices else ""
        self.scene = [SimEmitter(e.az_deg, e.el_deg, e.range_m, e.power_dbm, e.waveform, e.f_offset_hz)
                      for e in sc.emitters]
        # --- geometry / front end -----------------------------------------------------------------
        if geometry is None:
            try:
                geometry = ageo.geometry_from_config(cfg)
            except (GeometryError, ConfigError, ValueError) as exc:
                logger.warning("sim: geometry unavailable (%s): all elements co-located, isotropic", exc)
                geometry = None
        self.geometry = geometry
        self.frontend = frontend if frontend is not None else afe.FrontendModel.from_config(cfg, geometry)
        M, K = self.num_rx(), self.num_tx()
        self._rx_pos, self._rx_pat, self._rx_mapped = self._element_map("rx", self._rx_refs)
        self._tx_pos, self._tx_pat, self._tx_mapped = self._element_map("tx", self._tx_refs)
        # --- per-device state ------------------------------------------------------------------------
        self._tick = {}
        self._mcr = {}
        self._clk = {}
        self._clock_src = {}
        self._time_src = {}
        self._src_changed = {}
        self._ppm_int = {}
        self._ppm_gps = {}
        self._time_loaded = {}
        self._pending = []                    # [(device, req_T, value Fraction, latency Fraction)]
        self._cmd_time = {}
        self._lo_src = {}
        self._lo_exp = {}
        self._tune_count = {}
        for d in cfg.devices:
            tick = adev.tick_rate_hz(d, cfg) or 100_000_000
            self._tick[d.name] = int(tick)
            self._mcr[d.name] = adev.effective_mcr_hz(d, cfg)
            boot = as_fraction(round(float(rng_stream(self.seed, "boot", d.name).uniform(0, BOOT_OFFSET_MAX_S)), 6))
            self._ppm_int[d.name] = float(rng_stream(self.seed, "ppm", d.name).normal(0.0, sc.ref_ppm_sigma))
            self._ppm_gps[d.name] = float(rng_stream(self.seed, "ppm_gps", d.name).normal(0.0, sc.ref_ppm_sigma * 1e-3))
            self._clock_src[d.name] = "internal"
            self._time_src[d.name] = "internal"
            self._src_changed[d.name] = Fraction(-10)
            self._clk[d.name] = [(Fraction(0), boot, _frac_ppm(self._ppm_int[d.name]))]
            self._time_loaded[d.name] = False
            self._cmd_time[d.name] = None
            for direction in ("rx", "tx"):
                self._lo_src[(d.name, direction)] = "internal"
                self._lo_exp[(d.name, direction)] = False
                self._tune_count[(d.name, direction)] = 0
        self._shared_count = {"rx": 0, "tx": 0}
        # --- RF state ------------------------------------------------------------------------------
        fc = float(cfg.rf.center_freq_hz)
        fs = float(cfg.rf.sample_rate_hz)
        self._fc = {"rx": fc, "tx": fc}
        self._lo_off = {"rx": float(cfg.rf.lo_offset_hz), "tx": float(cfg.rf.lo_offset_hz)}
        self._fs = {"rx": fs, "tx": fs}
        gains = list(cfg.rf.rx_gain_db) or [30.0] * M
        self._rx_gain = [self._clip_gain(self._rx_refs[m].device, "rx", gains[m]) for m in range(M)]
        self._tx_gain = [self._gain_min(self._tx_refs[k].device, "tx") for k in range(K)]
        self._int_delay = {"rx": np.zeros(M), "tx": np.zeros(K)}
        self._lo_phase = {"rx": np.zeros(M), "tx": np.zeros(K)}
        self._lo_jumps = []                    # [(T, direction, index, dphi)]
        # static impairments (independent streams)
        self._rx_delay = np.array([self._draw_delay("rx_delay", r, sc.rx_delay_spread_samples) for r in self._rx_refs])
        self._tx_delay = np.array([self._draw_delay("tx_delay", r, sc.tx_delay_spread_samples) for r in self._tx_refs])
        self._rx_amp_db = np.array([float(rng_stream(self.seed, "rx_gain", r.device, r.channel).normal(0.0, sc.rx_gain_sigma_db))
                                    for r in self._rx_refs])
        cap = float(max(0.0, sc.tx_true_offset_db))
        self._tx_amp_db = np.array([float(np.clip(rng_stream(self.seed, "tx_gain", r.device, r.channel)
                                                  .normal(0.0, sc.tx_gain_sigma_db),
                                                  -3.0 * sc.tx_gain_sigma_db, min(3.0 * sc.tx_gain_sigma_db, cap)))
                                    for r in self._tx_refs])
        self._rx_path = np.array([_u(self.seed, "rx_path", r.device, r.channel) for r in self._rx_refs])
        self._tx_path = np.array([_u(self.seed, "tx_path", r.device, r.channel) for r in self._tx_refs])
        self._rx_kt = np.array([float(rng_stream(self.seed, "rx_temp", r.device, r.channel).uniform(0.5, 1.5))
                                for r in self._rx_refs])
        self._tx_kt = np.array([float(rng_stream(self.seed, "tx_temp", r.device, r.channel).uniform(0.5, 1.5))
                                for r in self._tx_refs])
        self._inj_resid = np.array([math.radians(float(rng_stream(self.seed, "inj_path", r.device, r.channel)
                                                       .normal(0.0, sc.injection_path_sigma_deg)))
                                    for r in self._rx_refs])
        self._cpl_resid = np.array([math.radians(float(rng_stream(self.seed, "cpl_path", r.device, r.channel)
                                                       .normal(0.0, sc.coupler_path_sigma_deg)))
                                    for r in self._tx_refs])
        self._truth_conj = {d.name: d.name in tuple(sc.iq_conjugate_devices) for d in cfg.devices}
        # temperatures (per TX chain; a device without TX stays at ambient)
        self.ambient_c = float(sc.temperature_c)
        self._temp0 = float(sc.temperature_c)
        self._tx_temp = np.full(K, float(sc.temperature_c))
        # GPIO
        self._gpio = {}
        self._atr = {}
        self._roles = self._role_lines()
        self.cal_input_override = None        # sim-only: cal input when no cal lines are configured
        # streaming / TX / faults / telemetry
        self._block = 0
        self._stream = None
        self._events = []
        self._bursts = []
        self._last_radiated = None
        self._faults = {}
        self._pps_gaps = []                   # [[start_T, end_T or None]]
        self._tel = {"fwd": [None] * K, "rev": [None] * K, "end": None}
        self._duty = []
        self._warned = set()
        for d in cfg.devices:                 # initial (pre-tune) LO phases
            for direction in ("rx", "tx"):
                self._draw_lo(d.name, direction, timed=False)

    # =========================================================================================
    # helpers: identity, time, caps
    # =========================================================================================
    def _trace(self, name, *args) -> None:
        self.calls.append((name, args, float(self._T)))

    def mono(self) -> float:
        """Virtual monotonic clock (seconds): inject as ``clock=`` into controllers."""
        return float(self._T)

    def wallclock(self) -> float:
        """Virtual wall clock (Unix seconds)."""
        return SIM_EPOCH_UNIX + float(self._T)

    @property
    def true_time(self) -> Fraction:
        return self._T

    def _element_map(self, which, refs):
        n = len(refs)
        pos = np.zeros((n, 3))
        pats = [ageo.ElementPattern() for _ in range(n)]
        mapped = np.zeros(n, dtype=bool)
        if self.geometry is None:
            mapped[:] = True
            return pos, pats, mapped
        P = self.geometry.positions(which)
        for k, e in enumerate(self.geometry.elements_for(which)):
            ref = getattr(e, which)
            if ref in refs:
                i = refs.index(ref)
                pos[i] = P[k]
                pats[i] = e.pattern
                mapped[i] = True
        return pos, pats, mapped

    def _draw_delay(self, purpose, ref, spread):
        s = abs(float(spread))
        return float(rng_stream(self.seed, purpose, ref.device, ref.channel).uniform(-s, s)) if s > 0 else 0.0

    def _range(self, dev, direction):
        c = self.caps[dev]
        return c.rx_gain_range_db if direction == "rx" else c.tx_gain_range_db

    def _gain_min(self, dev, direction):
        return float(self._range(dev, direction)[0])

    def _clip_gain(self, dev, direction, g):
        """Clip to the caps range and coerce to the gain step: RX to the nearest step,
        TX DOWN to the step (an applied TX gain never exceeds the authorized one)."""
        lo, hi, step = self._range(dev, direction)
        g = min(max(float(g), float(lo)), float(hi))
        if step and step > 0:
            q = (g - float(lo)) / float(step)
            n = math.floor(q + 1e-9) if direction == "tx" else round(q)
            g = float(lo) + n * float(step)
            g = min(max(g, float(lo)), float(hi))
        return g

    def _rate(self, dev):
        return self._clk[dev][-1][2]

    def _current_ppm(self, dev) -> float:
        if dev in self._faults.get("ref_unlock", {}).get("devices", ()):
            return self._ppm_int[dev]
        src = self._clock_src[dev]
        if src in ("external", "mboard"):
            return 0.0
        if src == "gpsdo":
            return self._ppm_gps[dev]
        return self._ppm_int[dev]

    def _reanchor(self, dev) -> None:
        """Keep the device clock continuous while its rate changes (ref switch / unlock);
        anchors already scheduled in the future (time loads) keep their value."""
        T = self._T
        r = _frac_ppm(self._current_ppm(dev))
        t = self._t_dev(dev, T)
        past = [a for a in self._clk[dev] if a[0] <= T]
        future = [(a[0], a[1], r) for a in self._clk[dev] if a[0] > T]
        self._clk[dev] = past + [(T, t, r)] + future

    def _t_dev(self, dev, T) -> Fraction:
        anchors = self._clk[dev]
        a = anchors[0]
        for x in anchors:
            if x[0] <= T:
                a = x
            else:
                break
        return a[1] + (T - a[0]) * (1 + a[2])

    def _true_of(self, dev, t) -> Fraction:
        """True time at which device ``dev`` reads ``t`` (Fraction or DevTime)."""
        if isinstance(t, DevTime):
            t = t.to_fraction()
        anchors = self._clk[dev]
        for i in range(len(anchors) - 1, -1, -1):
            Ta, ta, r = anchors[i]
            T = Ta + (t - ta) / (1 + r)
            nxt = anchors[i + 1][0] if i + 1 < len(anchors) else None
            if (T >= Ta or i == 0) and (nxt is None or T < nxt):
                return T
        Ta, ta, r = anchors[-1]
        return Ta + (t - ta) / (1 + r)

    def _devtime(self, dev, t: Fraction, floor=True) -> DevTime:
        tick = self._tick[dev]
        v = t * tick
        n = math.floor(v) if floor else round(v)
        return DevTime(0, int(n), tick)

    def _latency(self, dev) -> Fraction:
        if dev == self._master or not self.sc.pps_skew_ticks:
            return Fraction(0)
        return Fraction(int(self.sc.pps_skew_ticks), self._tick[dev])

    # --- PPS ----------------------------------------------------------------------------------
    def _suppressed(self, n) -> bool:
        for s, e in self._pps_gaps:
            if n >= s and (e is None or n < e):
                return True
        return False

    def _gap_at(self, n):
        for s, e in self._pps_gaps:
            if n >= s and (e is None or n < e):
                return (s, e)
        return None

    def _edge_after(self, T, until):
        """First unsuppressed integer edge E with T < E <= until (None if none)."""
        n = math.floor(T) + 1
        while n <= until:
            gap = self._gap_at(n)
            if gap is None:
                return Fraction(n)
            if gap[1] is None:
                return None
            n = max(n + 1, math.ceil(gap[1]))
        return None

    def _last_edge(self, T):
        """Last unsuppressed integer edge E <= T (None if none)."""
        n = math.floor(T)
        while n >= 0:
            gap = self._gap_at(n)
            if gap is None:
                return Fraction(n)
            n = math.ceil(gap[0]) - 1
        return None

    def _advance_to(self, T_new) -> None:
        T_new = as_fraction(T_new)
        if T_new <= self._T:
            return
        # commit pending PPS latches whose edge lies in (req_T, T_new]
        keep = []
        for dev, req_T, value, lat in self._pending:
            E = self._edge_after(req_T, T_new)
            if E is None:
                keep.append((dev, req_T, value, lat))
                continue
            Ta = E + lat
            self._clk[dev] = [a for a in self._clk[dev] if a[0] <= Ta] + [(Ta, value, _frac_ppm(self._current_ppm(dev)))]
            self._time_loaded[dev] = True
        self._pending = keep
        dt = float(T_new - self._T)
        decay = math.exp(-dt / TEMP_TAU_S)
        self._tx_temp = self.ambient_c + (self._tx_temp - self.ambient_c) * decay
        self._T = T_new

    def advance(self, seconds) -> None:
        """Advance the virtual true time (``sleep`` is the same)."""
        s = as_fraction(seconds)
        if s < 0:
            raise ValueError("cannot advance by a negative time")
        with self._lock:
            self._trace("advance", float(s))
            self._advance_to(self._T + s)

    def sleep(self, seconds: float) -> None:
        with self._lock:
            s = as_fraction(max(0.0, float(seconds)))
            self._trace("sleep", float(s))
            self._advance_to(self._T + s)

    # =========================================================================================
    # GPIO lines and RF front-end state
    # =========================================================================================
    def _role_lines(self) -> dict:
        import active_rf_control as arf
        rc = self.cfg.rf_control
        base = getattr(self.cfg, "base_dir", "")
        out = {"pa": [arf.NONE_LINE] * self.num_tx()}
        try:
            if self.num_tx():
                out["pa"] = [arf.resolve_line(x, base) for x in arf.parse_line_list(rc.pa_enable_line, self.num_tx())]
        except ConfigError as exc:
            logger.warning("sim: pa_enable_line unusable: %s", exc)
        for role, key in (("tr", "tr_switch_line"), ("lna", "lna_bias_line"),
                          ("cal_injection", "cal_injection_line"), ("cal_loopback", "cal_loopback_line"),
                          ("noise", "noise_source_line"), ("interlock", "interlock"),
                          ("dummy_load_sense", "dummy_load_sense")):
            try:
                out[role] = arf.parse_line(getattr(rc, key))
            except ConfigError as exc:
                logger.warning("sim: %s unusable: %s", key, exc)
                out[role] = arf.NONE_LINE
        return out

    @staticmethod
    def _key(line):
        kind = getattr(line, "kind", "none")
        if kind == "gpio":
            return ("gpio", line.device, line.bank, int(line.mask))
        if kind == "sim":
            return ("sim", line.name)
        return (kind, getattr(line, "path", ""))

    def _same(self, a, b) -> bool:
        return getattr(a, "kind", "none") != "none" and self._key(a) == self._key(b)

    def _line_state(self, line, default=False, burst=False) -> bool:
        """Logical state of a configured output line (ATR-driven while ``burst``)."""
        if getattr(line, "kind", "none") == "gpio":
            atr = self._atr.get((line.device, line.bank))
            if atr is not None and atr["ctrl"] & int(line.mask):
                level = atr["tx"] if burst else atr["idle"]
                on = bool(level & int(line.mask))
                return on == bool(line.active_high)
        return bool(self._gpio.get(self._key(line), default))

    def _pa_on(self, k, burst=True) -> bool:
        ln = self._roles["pa"][k] if k < len(self._roles["pa"]) else None
        if ln is None or ln.kind == "none":
            return True
        return self._line_state(ln, False, burst)

    def _tr_state(self, burst=False):
        ln = self._roles.get("tr")
        if ln is None or ln.kind == "none":
            return None
        return "tx" if self._line_state(ln, False, burst) else "rx"

    def _lna_on(self, burst=False) -> bool:
        ln = self._roles.get("lna")
        if ln is None or ln.kind == "none":
            return True
        return self._line_state(ln, True, burst)

    def cal_input(self) -> str:
        """Current RX input routing: antenna | injection | loopback (from the cal lines)."""
        inj, lb = self._roles.get("cal_injection"), self._roles.get("cal_loopback")
        if inj is not None and inj.kind != "none" and self._line_state(inj):
            return "injection"
        if lb is not None and lb.kind != "none" and self._line_state(lb):
            return "loopback"
        configured = any(x is not None and x.kind != "none" for x in (inj, lb))
        if not configured and self.cal_input_override in ("injection", "loopback"):
            return self.cal_input_override
        return "antenna"

    def set_cal_input(self, mode) -> None:
        """Sim-only: the cal routing when the config has no cal lines (``none``)."""
        if mode not in ("antenna", "injection", "loopback"):
            raise ValueError("cal input must be antenna, injection or loopback")
        self._trace("set_cal_input", mode)
        self.cal_input_override = None if mode == "antenna" else mode

    def _noise_on(self) -> bool:
        ln = self._roles.get("noise")
        if ln is None or ln.kind == "none":
            return True
        return self._line_state(ln)

    def wiring(self) -> str:
        """Physical TX load wiring (``[active_sim] tx_load``; auto follows the armed mode)."""
        w = self.sc.tx_load
        if w != "auto":
            return w
        mode = getattr(self.safety, "mode", None) if self.safety is not None else None
        if mode in _WIRING:
            return _WIRING[mode]
        allowed = tuple(self.cfg.tx.allowed_modes)
        if "dummy_load" in allowed:
            return "dummy_load"
        if "cabled" in allowed:
            return "cabled"
        return "antenna"

    def _do_gpio_write(self, line, value) -> None:
        if getattr(line, "kind", "none") in ("none", "file"):
            return
        self._gpio[self._key(line)] = bool(value)
        if line.kind == "gpio":
            atr = self._atr.get((line.device, line.bank))
            if atr is not None and atr["ctrl"] & int(line.mask) and not value:
                atr["ctrl"] &= ~int(line.mask) & 0xFFFFFFFF      # software override: manual mode

    def _do_gpio_read(self, line) -> bool:
        if self._same(line, self._roles.get("interlock")):
            return "interlock_open" not in self._faults
        if self._same(line, self._roles.get("dummy_load_sense")):
            return self.wiring() == "dummy_load" and "dummy_load_lost" not in self._faults
        if getattr(line, "kind", "none") == "sim" and line.name in ("interlock", "il"):
            return "interlock_open" not in self._faults
        return bool(self._gpio.get(self._key(line), False))

    def _do_gpio_atr(self, device, bank, ctrl_mask, idle, rx, tx, duplex) -> bool:
        if device not in self._devices:
            return False
        if bank not in tuple(self.caps[device].gpio_banks) + tuple(self.bcaps[device].gpio_banks):
            return False                       # no such bank (Per Vices: no ATR)
        self._atr[(device, bank)] = {"ctrl": int(ctrl_mask), "idle": int(idle), "rx": int(rx),
                                     "tx": int(tx), "duplex": int(duplex)}
        return True

    # =========================================================================================
    # lifecycle, references, time
    # =========================================================================================
    def _do_open(self) -> None:
        # a (re)opened device is re-initialized: no command time, no LO export / import,
        # no device-side integer delay (the LO phases stay until the next tune)
        self._stream = None
        for d in self._devices:
            self._cmd_time[d] = None
            for direction in ("rx", "tx"):
                self._lo_src[(d, direction)] = "internal"
                self._lo_exp[(d, direction)] = False
        for direction in ("rx", "tx"):
            self._int_delay[direction][:] = 0.0

    def _do_close(self) -> None:
        self._stream = None

    def _do_set_clock_source(self, src, device) -> None:
        for dev in self._devs(device):
            if src not in self.caps[dev].clock_sources and self.caps[dev].vendor != "sim":
                raise BackendError("%s: clock source %r not supported (valid: %s)"
                                   % (dev, src, ", ".join(self.caps[dev].clock_sources)))
            if src not in ("internal", "external", "gpsdo", "mboard"):
                raise BackendError("%s: clock source %r not supported by the simulator" % (dev, src))
            if self._clock_src[dev] != src:
                self._clock_src[dev] = src
                self._src_changed[dev] = self._T
                self._reanchor(dev)

    def _do_set_time_source(self, src, device) -> None:
        for dev in self._devs(device):
            if src not in self.caps[dev].time_sources and self.caps[dev].vendor != "sim":
                raise BackendError("%s: time source %r not supported (valid: %s)"
                                   % (dev, src, ", ".join(self.caps[dev].time_sources)))
            if src not in ("internal", "external", "gpsdo", "sfp0", "qsfp0"):
                raise BackendError("%s: time source %r not supported by the simulator" % (dev, src))
            self._time_src[dev] = src

    def _do_ref_locked(self, device):
        if device in self._faults.get("ref_unlock", {}).get("devices", ()):
            return False
        if self._clock_src[device] == "internal":
            return True
        return self._T >= self._src_changed[device] + REF_LOCK_SETTLE_S

    def _do_set_master_clock_rate(self, hz, device) -> None:
        for dev in self._devs(device):
            c = self.caps[dev]
            if c.mcr_choices_hz:
                if not any(abs(hz - x) <= 1.0 for x in c.mcr_choices_hz):
                    raise BackendError("%s: master clock rate %.9g not in %s" % (dev, hz, c.mcr_choices_hz))
            elif c.mcr_range_hz:
                if not c.mcr_range_hz[0] <= hz <= c.mcr_range_hz[1]:
                    raise BackendError("%s: master clock rate %.9g outside %s" % (dev, hz, c.mcr_range_hz))
            else:
                raise NotSupported("%s: the master clock rate is managed by the device" % dev)
            self._mcr[dev] = float(hz)
            if not c.tick_rate_hz:
                self._tick[dev] = int(round(hz))
            self._time_loaded[dev] = False

    def _do_time_now(self, device) -> DevTime:
        return self._devtime(device, self._t_dev(device, self._T), floor=True)

    def _do_time_last_pps(self, device) -> DevTime:
        E = self._last_edge(self._T)
        if E is None:
            return DevTime(0, 0, self._tick[device])
        return self._devtime(device, self._t_dev(device, E), floor=False)

    def _do_set_time_unknown_pps(self, t, group) -> None:
        devs = self._group_devs(group)
        horizon = self._T + 3
        E1 = self._edge_after(self._T, horizon)
        if E1 is None:
            raise HardwareFault("set_time_unknown_pps: no PPS edge detected (pps_missing?)")
        value = t.to_fraction()
        for dev in devs:
            self._pending = [p for p in self._pending if p[0] != dev]
            self._pending.append((dev, E1, value, self._latency(dev)))
        # UHD: wait for an edge, set the time at the next one, then sleep 1 s
        self._advance_to(E1 + 1 + Fraction(1, 100))

    def _do_set_time_next_pps(self, t, device) -> None:
        value = t.to_fraction()
        for dev in self._devs(device):
            self._pending = [p for p in self._pending if p[0] != dev]
            self._pending.append((dev, self._T, value, self._latency(dev)))

    def _do_set_time_now(self, t, device) -> None:
        devs = self._devs(device)
        value = t.to_fraction()
        n = sum(1 for c in self.calls if c[0] == "set_time_now")
        for i, dev in enumerate(devs):
            lat = Fraction(0)
            if len(self._devices) > 1:                 # per device AND per call (spec 13.2)
                lat = as_fraction(round(float(rng_stream(self.seed, "set_time_now", dev, n)
                                              .uniform(0.0, SET_TIME_NOW_LATENCY_S)), 9))
            Ta = self._T + lat
            self._pending = [p for p in self._pending if p[0] != dev]
            self._clk[dev] = [a for a in self._clk[dev] if a[0] <= Ta] + [(Ta, value, _frac_ppm(self._current_ppm(dev)))]
            self._time_loaded[dev] = True

    def _do_time_synchronized(self) -> bool:
        for g in self._groups:
            names = g.device_names()
            if len(names) < 2:
                continue
            if not all(self._time_loaded[n] for n in names):
                return False
            ts = [self._t_dev(n, self._T) for n in names]
            tol = Fraction(1, min(self._tick[n] for n in names))
            if max(ts) - min(ts) > tol:
                return False
        return True

    def _do_set_command_time(self, t, device) -> None:
        for dev in self._devs(device):
            self._cmd_time[dev] = t

    def _do_clear_command_time(self, device) -> None:
        for dev in self._devs(device):
            self._cmd_time[dev] = None

    # =========================================================================================
    # RF control
    # =========================================================================================
    def _do_set_rate(self, direction, hz) -> float:
        actual = hz
        for d in self.cfg.devices:
            c = self.caps[d.name]
            if c.vendor == "pervices" and c.rate_base_hz:
                n = min(max(1, int(round(c.rate_base_hz / hz))), 65534)
                actual = c.rate_base_hz / n
            elif c.vendor == "ettus" and self._mcr.get(d.name):
                mcr = self._mcr[d.name]
                actual = mcr / max(1, int(round(mcr / hz)))
        self._fs[direction] = float(actual)      # the base class bumps on a change
        return float(actual)

    def _lo_class(self, dev, direction, timed) -> str:
        cfg = self.cfg
        d = cfg.device(dev)
        caps = self.bcaps[dev]
        plan = adev.resolve_lo_plan(cfg)
        mem = adev.lo_share_members(cfg, plan) if plan in ("n321_star", "twinrx") else None
        fc = self._fc[direction]
        exporters_on = mem is not None and any(self._lo_exp[(e, direction)] for e in mem["exporters"])

        def active(name):
            dd = cfg.device(name)
            if mem is None or name not in mem["shared"]:
                return False
            if name in mem["exporters"]:
                return self._lo_exp[(name, direction)]
            if plan == "twinrx" and dd.lo_source == "companion":
                return self._lo_src[(name, direction)] == "companion"
            return self._lo_src[(name, direction)] == dd.lo_source and exporters_on
        if plan == "n321_star" and active(dev) and fc >= caps.lo_share_min_hz:
            return "shared_lo"
        if plan == "twinrx" and active(dev):
            return "shared_lo"
        if len(cfg.devices) == 1 and caps.lo_share_kind == "intra_device":
            return "shared_lo"
        if (caps.ext_lo_phase_ambiguity == "180" and d.lo_source == "external"
                and self._lo_src[(dev, direction)] == "external"):
            return "ambiguous_180"
        if caps.phase_ambiguity == "deterministic":
            return "deterministic"
        if timed and caps.timed_tune and caps.phase_repeatable_timed_tune:
            return "repeatable"
        return "random"

    def _draw_lo(self, dev, direction, timed) -> None:
        refs = self._rx_refs if direction == "rx" else self._tx_refs
        idx = [i for i, r in enumerate(refs) if r.device == dev]
        if not idx:
            return
        self._tune_count[(dev, direction)] += 1
        count = self._tune_count[(dev, direction)]
        cls = self._lo_class(dev, direction, timed)
        f_key = int(round(self._fc[direction]))
        for i in idx:
            ch = refs[i].channel
            if cls == "shared_lo":
                ph = _u(self.seed, "lo_shared", direction, self._shared_count[direction]) + \
                    _u(self.seed, "lo_path", dev, ch, direction)
            elif cls == "repeatable":
                ph = _u(self.seed, "lo_rep", dev, ch, direction, f_key)
            elif cls == "ambiguous_180":
                bit = int(rng_stream(self.seed, "lo_180bit", dev, ch, direction, count).integers(0, 2))
                ph = _u(self.seed, "lo_180base", dev, ch, direction, f_key) + math.pi * bit
            elif cls == "deterministic":
                ph = _u(self.seed, "lo_det", dev, ch, direction)
            else:
                ph = _u(self.seed, "lo_rand", dev, ch, direction, count)
            self._lo_phase[direction][i] = (ph + math.pi) % (2 * math.pi) - math.pi
        self._lo_jumps = [j for j in self._lo_jumps if not (j[1] == direction and j[2] in idx)]

    def _lo_phase_at(self, direction, i, T) -> float:
        ph = float(self._lo_phase[direction][i])
        for Tj, dj, ij, dphi in self._lo_jumps:
            if dj == direction and ij == i and Tj <= T:
                ph += dphi
        return ph

    def _lo_locked(self, dev) -> bool:
        f = self._faults.get("lo_unlock")
        return not (f is not None and dev in f["devices"] and self._T >= f["T"])

    def _do_tune(self, direction, freq_hz, lo_offset_hz, at) -> TuneReport:
        refs = self._rx_refs if direction == "rx" else self._tx_refs
        devs = [d for d in self._devices if any(r.device == d for r in refs)]
        old = self._fc[direction]
        lo_c = [c.freq_min_hz for c in (self.caps[d] for d in devs)]
        hi_c = [c.freq_max_hz for c in (self.caps[d] for d in devs)]
        f = freq_hz
        issues = []
        if devs:
            f = min(max(freq_hz, max(lo_c)), min(hi_c))
            if f != freq_hz:
                issues.append(ac.Issue("warning", "DEVICE_FREQ", "%s tune %.6g Hz coerced to %.6g Hz"
                                       % (direction, freq_hz, f)))
        band = False
        for d in devs:
            for edge in self.bcaps[d].band_edges_hz:
                if (old < edge) != (f < edge):
                    band = True
        timed_all = bool(devs)
        self._fc[direction] = float(f)
        self._lo_off[direction] = float(lo_offset_hz)
        self._shared_count[direction] += 1
        for d in devs:
            ct = self._cmd_time.get(d) if at is not None else None
            timed = ct is not None and self.bcaps[d].timed_tune and self.flavour != "pervices"
            if timed and ct.to_fraction() <= self._t_dev(d, self._T) + CMD_MIN_LEAD_S:
                # UHD: a command whose time has passed executes at once (untimed)
                timed = False
                self._events.append(AsyncEvent("late_command", time=ct,
                                               detail="%s %s tune: command time has passed" % (d, direction)))
            timed_all = timed_all and timed
            self._draw_lo(d, direction, timed)
        # UHD TuneRequest(target, lo_off): RF LO at target + lo_off; actual_dsp_freq carries
        # UHD's direction sign (centre = rf - sign * dsp, RX_SIGN = +1, TX_SIGN = -1)
        sign = 1.0 if direction == "rx" else -1.0
        rf = [float(f + lo_offset_hz)] * len(refs)
        dsp = [sign * float(lo_offset_hz)] * len(refs)
        locked = [self._lo_locked(r.device) for r in refs]
        return TuneReport(direction=direction, refs=tuple(refs), requested_hz=float(freq_hz), actual_rf_hz=rf,
                          actual_dsp_hz=dsp, timed=timed_all, phase_state="random", lo_locked=locked,
                          generation=self.coherence.generation, band_change=band, issues=issues,
                          actual_center_hz=[float(f)] * len(refs))

    def tuned_freq(self, direction="tx") -> float:
        """Current tuned centre frequency (Hz): ``tx_freq`` provider for the safety controller."""
        return float(self._fc[direction])

    def rate(self, direction="tx") -> float:
        """Current (physical) sample rate (Hz): ``tx_rate`` provider for the safety controller."""
        return float(self._fs[direction])

    def tuned_lo_offset_hz(self, direction="tx") -> float:
        """Current (physical) LO offset (Hz): the RF LO is at ``tuned_freq + tuned_lo_offset_hz``
        (UHD ``TuneRequest``)."""
        return float(self._lo_off[direction])

    def _do_set_rx_gain(self, gains_db) -> list:
        self._rx_gain = [self._clip_gain(self._rx_refs[m].device, "rx", g) for m, g in enumerate(gains_db)]
        return list(self._rx_gain)

    def _do_rx_gain(self) -> list:
        return list(self._rx_gain)

    def _do_set_tx_gains(self, chains, gains_db) -> None:
        for c, g in zip(chains, gains_db):
            self._tx_gain[int(c)] = self._clip_gain(self._tx_refs[int(c)].device, "tx", g)

    def _do_set_tx_gain_min(self) -> None:
        self._tx_gain = [self._gain_min(self._tx_refs[k].device, "tx") for k in range(self.num_tx())]

    def _do_tx_gain_readback(self, chains):
        return [float(self._tx_gain[int(c)]) for c in chains]

    def _ref_index(self, direction, ref) -> int:
        refs = self._rx_refs if direction == "rx" else self._tx_refs
        try:
            return refs.index(ref)
        except ValueError:
            raise BackendError("%s is not a configured %s channel" % (ref, direction.upper())) from None

    def _do_lo_names(self, direction, ref) -> list:
        self._ref_index(direction, ref)
        caps = self.bcaps[ref.device]
        if caps.lo_share_kind == "twinrx":
            return ["lo1", "lo2"]
        return ["lo1"] if (caps.lo_import or caps.lo_export) else []

    def _do_set_lo_source(self, direction, src, ref, name) -> None:
        self._ref_index(direction, ref)
        if src not in ("internal", "external", "companion"):
            raise BackendError("LO source %r unknown (internal | external | companion)" % src)
        if src != "internal" and not self.bcaps[ref.device].lo_import:
            raise NotSupported("%s (%s) cannot import an LO" % (ref.device, self.bcaps[ref.device].family))
        self._lo_src[(ref.device, direction)] = src

    def _do_set_lo_export(self, direction, enabled, ref, name) -> None:
        self._ref_index(direction, ref)
        if enabled and not self.bcaps[ref.device].lo_export:
            raise NotSupported("%s (%s) cannot export its LO" % (ref.device, self.bcaps[ref.device].family))
        self._lo_exp[(ref.device, direction)] = bool(enabled)

    def _do_set_integer_delay(self, direction, ref, samples) -> bool:
        i = self._ref_index(direction, ref)
        if not self.bcaps[ref.device].integer_delay_ctl:
            return False
        self._int_delay[direction][i] = float(samples)
        return True

    def _device_temp(self, dev) -> float:
        idx = [k for k, r in enumerate(self._tx_refs) if r.device == dev]
        return float(np.mean(self._tx_temp[idx])) if idx else self.ambient_c

    def _do_sensors(self, device) -> dict:
        rx = [self._lo_locked(device) for r in self._rx_refs if r.device == device]
        tx = [self._lo_locked(device) for r in self._tx_refs if r.device == device]
        out = {"ref_locked": bool(self._do_ref_locked(device)), "lo_locked": self._lo_locked(device),
               "rx_lo_locked": rx, "tx_lo_locked": tx, "temp_c": self._device_temp(device),
               "clock_source": self._clock_src[device], "time_source": self._time_src[device]}
        d = self.cfg.device(device)
        if "gpsdo" in (self._clock_src[device], self._time_src[device]) or d.gpsdo_fitted:
            out["gps_time"] = int(GPS_EPOCH_UNIX + math.floor(self._T))
            out["gps_locked"] = True
        return out

    # =========================================================================================
    # truth
    # =========================================================================================
    def _p_fs_true(self, chan, f_hz, tx_gain_db) -> float:
        return float(self.frontend.tx[int(chan)].power_cal.conservative_dbm(int(chan), float(tx_gain_db))
                     - self.sc.tx_true_offset_db)

    def _rx_gain_c(self) -> np.ndarray:
        gp = math.radians(self.sc.gain_phase_deg_per_db)
        tdr = math.radians(self.sc.temp_drift_deg_per_c)
        out = np.empty(self.num_rx(), dtype=np.complex128)
        for m, r in enumerate(self._rx_refs):
            ph = self._rx_path[m] + gp * (self._rx_gain[m] - 30.0) + tdr * self._rx_kt[m] * (
                self._device_temp(r.device) - self._temp0)
            out[m] = 10.0 ** (self._rx_amp_db[m] / 20.0) * np.exp(1j * ph)
        return out

    def _tx_gain_c(self) -> np.ndarray:
        gp = math.radians(self.sc.gain_phase_deg_per_db)
        tdr = math.radians(self.sc.temp_drift_deg_per_c)
        out = np.empty(self.num_tx(), dtype=np.complex128)
        for k in range(self.num_tx()):
            ph = self._tx_path[k] + gp * (self._tx_gain[k] - 30.0) + tdr * self._tx_kt[k] * (
                self._tx_temp[k] - self._temp0)
            out[k] = 10.0 ** (self._tx_amp_db[k] / 20.0) * np.exp(1j * ph)
        return out

    def _inj_paths(self):
        h, d = load_path_table(self.cfg.resolve_path(self.cfg.calibration.injection_paths_file),
                               self.num_rx(), self._fc["rx"])
        return h * np.exp(1j * self._inj_resid), d

    def _cpl_paths(self):
        h, d = load_path_table(self.cfg.resolve_path(self.cfg.calibration.coupler_paths_file),
                               self.num_tx(), self._fc["tx"])
        return h * np.exp(1j * self._cpl_resid), d

    @property
    def truth(self) -> SimTruth:
        T = self._T
        return SimTruth(
            rx_delay_samples=self._rx_delay - self._int_delay["rx"],
            rx_gain=self._rx_gain_c(),
            tx_delay_samples=self._tx_delay - self._int_delay["tx"],
            tx_gain=self._tx_gain_c(),
            lo_phase_rx=np.array([self._lo_phase_at("rx", i, T) for i in range(self.num_rx())]),
            lo_phase_tx=np.array([self._lo_phase_at("tx", i, T) for i in range(self.num_tx())]),
            iq_conjugate=dict(self._truth_conj),
            ref_ppm={d: self._current_ppm(d) for d in self._devices},
            pps_skew_ticks={d: (0 if d == self._master else int(self.sc.pps_skew_ticks)) for d in self._devices},
            injection_paths=self._inj_paths()[0],
            coupler_paths=self._cpl_paths()[0],
            turnaround_samples=int(self.sc.turnaround_samples),
            p_fs_true=self._p_fs_true)

    def true_rx_response(self) -> np.ndarray:
        """(M,) complex chain response (gain x exp(j LO phase)) at the current time."""
        t = self.truth
        return t.rx_gain * np.exp(1j * t.lo_phase_rx)

    def true_tx_response(self) -> np.ndarray:
        t = self.truth
        return t.tx_gain * np.exp(1j * t.lo_phase_tx)

    # =========================================================================================
    # waveforms on the absolute sample grid
    # =========================================================================================
    def _zc(self):
        if not hasattr(self, "_zc_tab"):
            self._zc_tab = adsp.zadoff_chu(ZC_LEN, 1)
        return self._zc_tab

    def _lfm(self, bw_frac):
        key = round(float(bw_frac), 6)
        cache = self.__dict__.setdefault("_lfm_tab", {})
        if key not in cache:
            cache[key] = adsp.lfm(LFM_PERIOD, 1.0, min(1.0, max(1e-3, key)))
        return cache[key]

    def _noise(self, key, k0, L, bw_frac) -> np.ndarray:
        """Unit-rms band-limited complex noise at absolute samples k0..k0+L-1 (continuous)."""
        H = NOISE_HALF_TAPS if bw_frac < 1.0 else 0
        a, b = k0 - H, k0 + L + H
        s0, s1 = a // NOISE_SEG, (b - 1) // NOISE_SEG
        segs = []
        for s in range(s0, s1 + 1):
            g = rng_stream(self.seed, *key, int(s)).standard_normal((NOISE_SEG, 2))
            segs.append((g[:, 0] + 1j * g[:, 1]) / math.sqrt(2.0))
        w = np.concatenate(segs)[a - s0 * NOISE_SEG: b - s0 * NOISE_SEG]
        if not H:
            return w
        tap = np.arange(-H, H + 1)
        h = np.sinc(float(bw_frac) * tap) * np.hamming(2 * H + 1)
        h /= math.sqrt(float(np.sum(h * h)))
        return np.convolve(w, h, mode="valid")

    def _wave(self, kind, key, k0, L, fs, f_off, bw_frac) -> np.ndarray:
        k = np.arange(L, dtype=np.int64) + int(k0)
        if kind == "tone":
            s = np.ones(L, dtype=np.complex128)
        elif kind == "zc":
            s = self._zc()[np.mod(k, ZC_LEN)]
        elif kind == "lfm":
            s = self._lfm(bw_frac)[np.mod(k, LFM_PERIOD)]
        else:
            s = self._noise(key, int(k0), L, min(1.0, max(1e-3, float(bw_frac))))
        if f_off:
            cyc = float(f_off) / float(fs)
            s = s * np.exp(2j * math.pi * np.mod(cyc * (k.astype(np.float64)), 1.0))
        return s

    # =========================================================================================
    # RX synthesis
    # =========================================================================================
    def _coupling_matrix(self, f_hz):
        if self.sc.coupling_db <= -99.0:
            return None
        idx = np.flatnonzero(self._rx_mapped)
        if idx.size < 2:
            return None
        P = self._rx_pos[idx]
        D = np.linalg.norm(P[:, None, :] - P[None, :, :], axis=-1)
        dmin = float(np.min(D[D > 0]))
        c = 10.0 ** (self.sc.coupling_db / 20.0)
        k0 = 2 * math.pi * f_hz / C0
        C = np.eye(self.num_rx(), dtype=np.complex128)
        for a, i in enumerate(idx):
            for b, j in enumerate(idx):
                if a != b and D[a, b] <= dmin * 1.01:
                    C[i, j] = c * np.exp(-1j * k0 * D[a, b])
        return C

    def _scene_field(self, T_d, n, fs, G):
        """(M, n+2G) field (sqrt(mW)) at the element terminals on the grid starting at
        true time ``T_d - G/fs``; unmapped RX rows stay zero."""
        M = self.num_rx()
        L = n + 2 * G
        out = np.zeros((M, L), dtype=np.complex128)
        if not self.scene:
            return out
        P = T_d * as_fraction(fs)
        k_int = math.floor(P)
        mu = float(P - k_int)
        k0 = k_int - G
        f_lo = self._fc["rx"]
        rows = np.flatnonzero(self._rx_mapped)
        for e_idx, em in enumerate(self.scene):
            s = self._wave(em.waveform, ("emitter", e_idx), k0, L, fs, em.f_offset_hz, em.bw_frac)
            amp = math.sqrt(10.0 ** (float(em.power_dbm) / 10.0))
            u0 = ageo.direction_vector(em.az_deg, em.el_deg)
            for m in rows:
                r = self._rx_pos[m]
                if em.range_m and em.range_m > 0:
                    p = float(em.range_m) * u0
                    dvec = p - r
                    dn = float(np.linalg.norm(dvec))
                    dr = dn - float(np.linalg.norm(p))
                    adv = -dr / C0 * fs
                    phase = -2 * math.pi * f_lo * dr / C0
                    gain = float(np.linalg.norm(p)) / dn
                    u_el = dvec / dn
                else:
                    tau = float(u0 @ r) / C0
                    adv = tau * fs
                    phase = 2 * math.pi * f_lo * tau
                    gain = 1.0
                    u_el = u0
                ge = self._element_field(self._rx_pat[m], u_el)
                out[m] += amp * gain * ge * np.exp(1j * phase) * adsp.frac_delay_fft(s, -(mu + adv))
        C = self._coupling_matrix(f_lo)
        if C is not None:
            out = C @ out
        return out

    def _element_field(self, pattern, u_enu) -> complex:
        import active_pattern as apat
        ub = self.geometry.enu_to_body(u_enu) if self.geometry is not None else np.asarray(u_enu, float)
        return complex(apat.element_gain(pattern, ub))

    def _injection_field(self, T_d, n, fs, G):
        M = self.num_rx()
        L = n + 2 * G
        P = T_d * as_fraction(fs)
        k_int = math.floor(P)
        mu = float(P - k_int)
        kind = self.cfg.calibration.cal_waveform
        if kind == "tone":
            s = self._wave("tone", ("inj",), k_int - G, L, fs, INJ_TONE_FRAC * fs, 1.0)
        else:
            s = self._wave("zc" if kind == "zc" else "noise", ("inj",), k_int - G, L, fs, 0.0, 0.8)
        amp = math.sqrt(10.0 ** (self.sc.injection_dbm / 10.0))
        h, dl = self._inj_paths()
        out = np.empty((M, L), dtype=np.complex128)
        for m in range(M):
            out[m] = amp * h[m] * adsp.frac_delay_fft(s, -(mu - dl[m] * fs))
        return out

    def _leak_db(self, wiring, tr_state, f_hz) -> np.ndarray:
        """(M, K) TX->RX coupling (dB) at the chain inputs for a burst."""
        fe = self.frontend
        iso = fe.isolation_at(f_hz)
        if fe.has_tr_switch and tr_state == "rx":
            iso = np.where(fe.same_element, 0.0, iso)
        leak = float(self.sc.tx_leak_dbc)
        out = np.empty_like(iso)
        for m in range(iso.shape[0]):
            for k in range(iso.shape[1]):
                v = iso[m, k]
                if wiring == "antenna":
                    if v > 0:
                        out[m, k] = -v
                    elif fe.has_tr_switch and tr_state == "rx" and fe.same_element[m, k]:
                        out[m, k] = 0.0
                    else:
                        out[m, k] = leak
                else:
                    out[m, k] = min(leak, -v) if v > 0 else leak
        return out

    def _burst_paths(self, b):
        """(M, K_b) complex coupling and (M, K_b) extra delay (samples) of burst ``b``."""
        M = self.num_rx()
        chains = b["chains"]
        Kb = len(chains)
        H = np.zeros((M, Kb), dtype=np.complex128)
        D = np.zeros((M, Kb))
        L = b["leak_db"]
        lb = int(self.cfg.calibration.loopback_rx_chan)
        tx = self.cfg.tx
        for j, c in enumerate(chains):
            for m in range(M):
                if b["cal_input"] == "loopback" and m == lb:
                    continue
                H[m, j] += 10.0 ** (L[m, c] / 20.0) * np.exp(1j * _u(self.seed, "leak_phase", m, c))
            if b["wiring"] == "cabled" and c < M:
                loss = float(tx.cabled_rx_path_loss_db[c]) if c < len(tx.cabled_rx_path_loss_db) else 0.0
                H[c, j] += 10.0 ** (-loss / 20.0)
            if b["cal_input"] == "loopback" and 0 <= lb < M:
                h, dl = b["cpl"]
                loss = float(tx.loopback_path_loss_db[c]) if c < len(tx.loopback_path_loss_db) else 0.0
                H[lb, j] += h[c] * 10.0 ** (-loss / 20.0)
                D[lb, j] = dl[c] * b["fs"]
            k_inj = self.cfg.calibration.injection_tx_index()
            if b["cal_input"] == "injection" and k_inj == c:
                h, dl = b["inj"]
                loss = float(tx.cabled_rx_path_loss_db[c]) if c < len(tx.cabled_rx_path_loss_db) else 0.0
                for m in range(M):
                    H[m, j] += h[m] * 10.0 ** (-loss / 20.0)
                    D[m, j] = dl[m] * b["fs"]
        return H, D

    def _tx_contrib(self, rows, T_d, n, fs, G):
        """TX bursts coupled into the chain inputs of ``rows`` (sqrt(mW)), (R, n+2G)."""
        L = n + 2 * G
        out = np.zeros((len(rows), L), dtype=np.complex128)
        fsf = as_fraction(fs)
        T_end = T_d + Fraction(n + G) / fsf
        T_beg = T_d - Fraction(G) / fsf
        f_rx = self._fc["rx"]
        turn = int(self.sc.turnaround_samples)
        pad = 16
        for b in self._bursts:
            if abs(b["fs"] - fs) > 1e-9 * fs:
                if "fs_mismatch" not in self._warned:
                    self._warned.add("fs_mismatch")
                    logger.warning("sim: TX burst at %.6g S/s ignored by an RX window at %.6g S/s", b["fs"], fs)
                continue
            span = Fraction(b["y"].shape[1] + turn + 2 * pad + 64) / fsf
            if min(b["T0"]) > T_end or max(b["T0"]) + span < T_beg:
                continue
            H, D = b["paths"]
            acc = np.zeros_like(out)
            for j in range(len(b["chains"])):
                yy = np.concatenate((np.zeros(pad), b["y"][j], np.zeros(pad)))
                base = float((b["T0"][j] - T_d) * fsf) + G + turn
                for r_i, m in enumerate(rows):
                    hc = H[m, j]
                    if hc == 0:
                        continue
                    Dm = base + D[m, j]
                    n0 = math.floor(Dm)
                    yd = adsp.frac_delay_fft(yy, Dm - n0) * hc
                    a = n0 - pad
                    lo, hi = max(0, a), min(L, a + yd.size)
                    if lo < hi:
                        acc[r_i, lo:hi] += yd[lo - a: hi - a]
            df = b["fc"] - f_rx
            if df:
                t_abs = float(T_d) + (np.arange(L) - G) / fs
                acc *= np.exp(2j * math.pi * np.mod(df * t_abs, 1.0))[None, :]
            out += acc
        return out

    def _check_damage(self, m, peak_mw, source) -> None:
        if not peak_mw > 0:
            return
        lvl = 10.0 * math.log10(peak_mw)
        lim = self.frontend.rx[m].damage_input_dbm(self._lna_on(burst=(source != "scene")),
                                                   self.frontend.lna_off_isolation_db)
        if lvl > lim + 1e-9:
            entry = {"t": float(self._T), "rx": int(m), "ref": str(self._rx_refs[m]), "level_dbm": lvl,
                     "limit_dbm": float(lim), "source": source}
            self.damage_log.append(entry)
            logger.error("SIM DAMAGE: RX %d at %.2f dBm > damage %.2f dBm (%s)", m, lvl, lim, source)
            self._emit(EVT_ACTIVE_SIM_DAMAGE, "critical", **entry)

    def _synth(self, n, start: DevTime):
        """(M, n) complex128 FS samples, overdrive mask."""
        M = self.num_rx()
        fs = self._fs["rx"]
        out = np.zeros((M, n), dtype=np.complex128)
        if M == 0:
            return out.astype(np.complex64), 0
        block = self._block
        self._block += 1
        cal = self.cal_input()
        inj_on = cal == "injection" and self._noise_on() and self.cfg.calibration.injection_tx_index() is None
        lb = int(self.cfg.calibration.loopback_rx_chan)
        r_max = float(np.max(np.linalg.norm(self._rx_pos, axis=1), initial=0.0))
        spread = float(np.max(np.abs(self._rx_delay - self._int_delay["rx"]), initial=0.0))
        G = int(max(GUARD, math.ceil(2.0 * r_max / C0 * fs + spread) + 32))
        fe = self.frontend
        gc = self._rx_gain_c()
        for dev in self._devices:
            rows = [m for m, r in enumerate(self._rx_refs) if r.device == dev]
            if not rows:
                continue
            T_d = self._true_of(dev, start)
            if cal == "injection" and inj_on:
                U = self._injection_field(T_d, n, fs, G)[rows]
                src = "injection"
            elif cal == "injection":
                U = np.zeros((len(rows), n + 2 * G), dtype=np.complex128)
                src = "injection"
            else:
                Fld = self._scene_field(T_d, n, fs, G)
                U = Fld[rows]
                src = "scene"
                for r_i, m in enumerate(rows):
                    feed = fe.rx[m].feed_loss_db
                    if feed:
                        U[r_i] *= 10.0 ** (-feed / 20.0)
                    if cal == "loopback" and m == lb:
                        U[r_i] = 0.0
            for r_i, m in enumerate(rows):
                pk = float(np.max(np.abs(U[r_i]) ** 2)) if U.shape[1] else 0.0
                self._check_damage(m, pk, src)
            U = U + self._tx_contrib(rows, T_d, n, fs, G)
            ppm = self._current_ppm(dev)
            f_err = self._fc["rx"] * ppm * 1e-6
            t_rel = float(T_d) + np.arange(n) / fs
            ramp = np.exp(-2j * math.pi * np.mod(f_err * t_rel, 1.0))
            slip = n / 2.0 * ppm * 1e-6
            lna = self._lna_on()
            for r_i, m in enumerate(rows):
                ext = fe.rx[m].ext_gain_db(self._fc["rx"], lna, fe.lna_off_isolation_db)
                scale = 10.0 ** ((ext + self._rx_gain[m] - self.sc.rx_fullscale_dbm) / 20.0)
                resp = gc[m] * np.exp(1j * self._lo_phase_at("rx", m, T_d))
                x = adsp.frac_delay_fft(U[r_i] * (scale * resp),
                                        self._rx_delay[m] - self._int_delay["rx"][m] + slip)
                out[m] = x[G:G + n] * ramp
        sig = math.sqrt(10.0 ** (self.sc.noise_dbfs / 10.0) / 2.0)
        g = rng_stream(self.seed, "noise", block).standard_normal((M, n, 2))
        out += sig * (g[..., 0] + 1j * g[..., 1])
        re = np.clip(out.real, -1.0, 1.0)
        im = np.clip(out.imag, -1.0, 1.0)
        out = re + 1j * im
        for m, r in enumerate(self._rx_refs):
            if self._truth_conj.get(r.device):
                out[m] = np.conj(out[m])
        x = out.astype(np.complex64)
        return x, adsp.overdrive_mask(x)

    def _master_true(self, t: DevTime) -> Fraction:
        return self._true_of(self._master, t)

    def _do_rx_capture(self, nsamps, start) -> RxCapture:
        errors = []
        if self.strict_rx_timing and start < self._do_time_now(self._master):
            # UHD: ERROR_CODE_LATE_COMMAND, no samples (the past cannot be streamed)
            self._events.append(AsyncEvent("late_command", time=start, detail="rx_capture start has passed"))
            return RxCapture(data=np.zeros((self.num_rx(), nsamps), dtype=np.complex64), start=start,
                             fs_hz=self._fs["rx"], fc_hz=self._fc["rx"], overflow=False,
                             errors=["late_command"], overdrive_mask=0, dropped=int(nsamps))
        overflow = False
        dropped = 0
        f = self._faults.get("overflow")
        data, mask = self._synth(nsamps, start)
        if f is not None and f["count"] > 0:
            f["count"] -= 1
            if f["count"] <= 0:
                self._faults.pop("overflow", None)
            overflow, dropped = True, nsamps // 2
            data[:, nsamps // 2:] = 0
            errors.append("overflow")
            self._events.append(AsyncEvent("overflow", detail="injected"))
            self._emit(EVT_ACTIVE_OVERFLOW, "warning", dropped=dropped)
        end = self._master_true(start) + Fraction(nsamps) / as_fraction(self._fs["rx"])
        self._advance_to(end)
        return RxCapture(data=data, start=start, fs_hz=self._fs["rx"], fc_hz=self._fc["rx"],
                         overflow=overflow, errors=errors, overdrive_mask=mask, dropped=dropped)

    def _do_rx_start(self, start) -> None:
        self._stream = {"t0": start, "pos": 0}

    def _stream_time(self, pos) -> DevTime:
        t0 = self._stream["t0"]
        return DevTime.from_seconds(t0.to_fraction() + Fraction(pos) / as_fraction(self._fs["rx"]), t0.tick_rate)

    def _do_rx_read(self, nsamps, timeout_s) -> RxCapture:
        fs = as_fraction(self._fs["rx"])
        start = self._stream_time(self._stream["pos"])
        overflow = False
        dropped = 0
        errors = []
        behind = self._T - self._master_true(start)
        if behind > as_fraction(self.rx_buffer_s):
            skip = math.ceil(behind * fs)
            self._stream["pos"] += skip
            dropped += skip
            overflow = True
            start = self._stream_time(self._stream["pos"])
        f = self._faults.get("overflow")
        if f is not None and f["count"] > 0:
            f["count"] -= 1
            if f["count"] <= 0:
                self._faults.pop("overflow", None)
            self._stream["pos"] += nsamps
            dropped += nsamps
            overflow = True
            start = self._stream_time(self._stream["pos"])
        if overflow:
            errors.append("overflow")
            self._events.append(AsyncEvent("overflow", detail="%d samples dropped" % dropped))
            self._emit(EVT_ACTIVE_OVERFLOW, "warning", dropped=dropped)
        data, mask = self._synth(nsamps, start)
        self._stream["pos"] += nsamps
        self._advance_to(self._master_true(start) + Fraction(nsamps) / fs)
        return RxCapture(data=data, start=start, fs_hz=self._fs["rx"], fc_hz=self._fc["rx"],
                         overflow=overflow, errors=errors, overdrive_mask=mask, dropped=dropped)

    def _do_rx_stop(self) -> None:
        self._stream = None

    def _do_async_events(self) -> list:
        evs, self._events = self._events, []
        return evs

    # =========================================================================================
    # TX
    # =========================================================================================
    def _do_create_tx_streamer(self) -> None:
        if not self._safety_enabled():                       # defence in depth (spec 4.12)
            raise BackendError("refusing to create a TX streamer while TX is disabled")
        self._trace("create_tx_streamer")

    def last_tx_output(self):
        """Test introspection of the last burst: ``(chains, y, fs_hz, fc_hz, wiring)`` with
        ``y`` (K, N) the antenna-port amplitude in sqrt(mW) (guard samples removed)."""
        if not self._bursts:
            return None
        b = self._bursts[-1]
        return list(b["chains"]), b["y"][:, b["g"]:b["g"] + b["n"]].copy(), b["fs"], b["fc"], b["wiring"]

    def _do_tx_abort(self) -> None:
        pass

    def _do_tx_send(self, permit, samples, timeout_s) -> TxResult:
        # the PHYSICAL TX rate, not the rate the permit declared (a mismatch must show)
        with self._lock:
            return self._transmit(samples, list(permit.chains), permit.start, self._fs["tx"], float(timeout_s))

    def physical_tx_unchecked(self, samples, start=None, chains=None, tx_gains_db=None) -> TxResult:
        """TEST-ONLY raw transmit path that BYPASSES the TX safety controller.

        Exists to prove that ``damage_log`` catches an over-power transmission, and
        for ``active_fake_uhd`` (whose ``UhdBackend.tx_burst`` already consumed the
        permit). Samples beyond full scale clip in the DAC.
        """
        with self._lock:
            x = np.atleast_2d(np.asarray(samples, dtype=np.complex128))
            chains = list(range(x.shape[0])) if chains is None else [int(c) for c in chains]
            if len(chains) != x.shape[0] or any(not 0 <= c < self.num_tx() for c in chains):
                raise BackendError("physical_tx_unchecked: chains %s do not match %d rows" % (chains, x.shape[0]))
            if tx_gains_db is not None:
                g = [float(tx_gains_db)] * len(chains) if np.ndim(tx_gains_db) == 0 else list(tx_gains_db)
                self._do_set_tx_gains(chains, g)
            self._trace("physical_tx_unchecked", tuple(chains), x.shape[1], start)
            return self._transmit(x, chains, start, self._fs["tx"], 1.0)

    def _tx_chain(self, k, v_mw, burst_on=True):
        """Device-port amplitude (sqrt(mW)) -> antenna-port amplitude through the TX stages."""
        out = v_mw
        for s in self.frontend.tx[k].stages:
            m = s.model()
            if m is not None:
                out = m.apply(out * math.sqrt(1e-3)) / math.sqrt(1e-3)
            else:
                out = out * 10.0 ** (float(s.gain_db) / 20.0)
        return out

    def _transmit(self, samples, chains, start, fs, timeout_s) -> TxResult:
        x = np.array(samples, dtype=np.complex128, copy=True)
        K, N = x.shape
        fs = float(fs)
        events = []
        devs = sorted({self._tx_refs[c].device for c in chains})
        # timing: a start too close to (or before) the device time is late -> dropped
        late = False
        if self._take_fault("late"):
            late = True
        T_start = {}
        for d in devs:
            if start is None:
                T_start[d] = self._T
            else:
                lead = max(MIN_TX_LEAD_S, float(self.bcaps[d].min_tx_lead_s))
                if start.to_fraction() < self._t_dev(d, self._T) + as_fraction(lead):
                    late = True
                T_start[d] = self._true_of(d, start)
        if late:
            events.append(AsyncEvent("late", time=start, detail="burst start not ahead of the device time"))
            self._emit(EVT_ACTIVE_UNDERFLOW, "warning", kind="late")
            return TxResult(ok=False, sent_samples=N, late=1, acked=False, events=events)
        # DAC (sc16): |I|, |Q| > 1 clip (reachable only through physical_tx_unchecked)
        for j, c in enumerate(chains):
            if self._truth_conj.get(self._tx_refs[c].device):
                x[j] = np.conj(x[j])
        x = adsp.sc16_to_cf32(adsp.cf32_to_sc16(x, 32767), 32767.0).astype(np.complex128)
        # underflow storm
        underflows = 0
        corrupt = False
        st = self._faults.get("underflow_storm")
        if st is not None and st["bursts"] > 0:
            st["bursts"] -= 1
            if st["bursts"] <= 0:
                self._faults.pop("underflow_storm", None)
            underflows = int(st["count"])
            pos = np.linspace(0, max(0, N - UNDERFLOW_GAP), underflows + 2)[1:-1].astype(int)
            for p in pos:
                if self.flavour == "pervices":
                    x = np.concatenate((x[:, :p], np.zeros((K, UNDERFLOW_GAP)), x[:, p:]), axis=1)[:, :N]
                    corrupt = True
                else:
                    x[:, p:p + UNDERFLOW_GAP] = 0.0
                events.append(AsyncEvent("underflow", detail="injected"))
            self._emit(EVT_ACTIVE_UNDERFLOW, "warning", count=underflows, flavour=self.flavour)
        spread = float(np.max(np.abs(self._tx_delay - self._int_delay["tx"]), initial=0.0))
        G = int(max(GUARD, math.ceil(spread) + 32))
        xp = np.concatenate((np.zeros((K, G)), x, np.zeros((K, G))), axis=1)
        gc = self._tx_gain_c()
        y = np.zeros_like(xp)
        T0s = []
        for j, c in enumerate(chains):
            d = self._tx_refs[c].device
            Ts = T_start[d]
            T0s.append(Ts - Fraction(G) / as_fraction(fs))
            p_fs = self._p_fs_true(c, self._fc["tx"], self._tx_gain[c])
            a = math.sqrt(10.0 ** (p_fs / 10.0))
            resp = gc[c] * np.exp(1j * self._lo_phase_at("tx", c, Ts))
            v = adsp.frac_delay_fft(xp[j] * (a * resp), self._tx_delay[c] - self._int_delay["tx"][c])
            f_err = self._fc["tx"] * self._current_ppm(d) * 1e-6
            t_rel = float(Ts) + (np.arange(xp.shape[1]) - G) / fs
            v = v * np.exp(2j * math.pi * np.mod(f_err * t_rel, 1.0))
            if self._pa_on(c, burst=True):
                y[j] = self._tx_chain(c, v)
            else:
                leak = math.sqrt(10.0 ** ((p_fs + LO_LEAK_DBC) / 10.0))
                y[j, G:G + N] = leak
        wiring = self.wiring()
        tr = self._tr_state(burst=True)
        rec = {"T0": T0s, "fs": fs, "y": y, "chains": list(chains), "fc": self._fc["tx"], "wiring": wiring,
               "cal_input": self.cal_input(), "tr": tr, "n": N, "g": G,
               "leak_db": self._leak_db(wiring, tr, self._fc["tx"]),
               "cpl": self._cpl_paths(), "inj": self._inj_paths()}
        rec["paths"] = self._burst_paths(rec)
        # RX damage (chain inputs; LNA / T/R state during the burst)
        H, _D = rec["paths"]
        field = H @ y
        for m in range(self.num_rx()):
            self._check_damage(m, float(np.max(np.abs(field[m]) ** 2)) if field.shape[1] else 0.0, "tx")
        # reverse waves, telemetry, heating
        fwd_mw = np.mean(np.abs(y[:, G:G + N]) ** 2, axis=1)
        S = self.frontend.s_matrix
        K_all = self.num_tx()
        rev_all = [None] * K_all
        if S is not None and wiring == "antenna":
            # active reflection: b = S a over EVERY port (idle ports receive coupled power)
            a_full = np.zeros((K_all, y.shape[1]), dtype=np.complex128)
            a_full[chains] = y
            b = S @ a_full
            rev_full = np.mean(np.abs(b[:, G:G + N]) ** 2, axis=1)
            rev_all = [float(v) for v in rev_full]
        else:
            # dummy load / cabled (or no S-matrix): each driven port sees its own load,
            # Gamma(load_vswr), no inter-port coupling
            gam = (self.sc.load_vswr - 1.0) / (self.sc.load_vswr + 1.0)
            for j, c in enumerate(chains):
                rev_all[c] = float(fwd_mw[j] * gam * gam)
        dur = N / fs
        for c in range(K_all):
            self._tel["fwd"][c] = None
            self._tel["rev"][c] = 10.0 * math.log10(rev_all[c]) if rev_all[c] is not None and rev_all[c] > 0 else None
        for j, c in enumerate(chains):
            self._tel["fwd"][c] = 10.0 * math.log10(fwd_mw[j]) if fwd_mw[j] > 0 else None
            self._tx_temp[c] += float(self.sc.heat_c_per_w) * float(fwd_mw[j]) * 1e-3 * dur / TEMP_TAU_S
        T_end = max(T0s) + Fraction(N + 2 * G) / as_fraction(fs) - Fraction(G) / as_fraction(fs)
        self._tel["end"] = T_end
        self._bursts.append(rec)
        horizon = self._T - Fraction(int(TX_HISTORY_S))
        self._bursts = [b for b in self._bursts if max(b["T0"]) > horizon][-TX_HISTORY_MAX:]
        if wiring == "antenna":
            self._last_radiated = rec
        hang = self._take_fault("tx_hang")
        self._advance_to(T_end + (as_fraction(timeout_s) if hang else 0))
        if not hang:
            events.append(AsyncEvent("burst_ack", time=start, detail=""))
        ok = not hang and underflows == 0 and not corrupt
        return TxResult(ok=ok, sent_samples=N, underflows=underflows, late=0, seq_errors=0,
                        acked=not hang, content_corrupt=corrupt, events=events)

    def _take_fault(self, kind) -> bool:
        f = self._faults.get(kind)
        if f is None:
            return False
        f["bursts"] -= 1
        if f["bursts"] <= 0:
            self._faults.pop(kind, None)
        return True

    # =========================================================================================
    # faults, telemetry, probe
    # =========================================================================================
    def inject_fault(self, kind: str, **kw) -> None:
        """Inject a fault (``FAULT_KINDS``). Keyword options:

        ref_unlock(device=None) | lo_unlock(device=None, delay_s=0) | pps_missing |
        overflow(count=1) | underflow_storm(count=8, bursts=1) | late(bursts=1) |
        reverse_power(dbm=40, chains=None) | interlock_open | dummy_load_lost |
        over_temp(temp_c=150) | tx_hang(bursts=1) | telemetry_stale
        """
        if kind not in FAULT_KINDS:
            raise ValueError("unknown fault %r (valid: %s)" % (kind, ", ".join(FAULT_KINDS)))
        with self._lock:
            self._trace("inject_fault", kind, dict(kw))
            dev = kw.get("device")
            devs = self._devs(dev) if dev is not None else list(self._devices)
            if kind == "ref_unlock":
                self._faults[kind] = {"devices": tuple(devs)}
                for d in devs:
                    self._reanchor(d)
                    self._events.append(AsyncEvent("ref_unlock", detail=d))
            elif kind == "lo_unlock":
                Tj = self._T + as_fraction(float(kw.get("delay_s", 0.0)))
                self._faults[kind] = {"devices": tuple(devs), "T": Tj}
                n = len(self._lo_jumps)
                for direction, refs in (("rx", self._rx_refs), ("tx", self._tx_refs)):
                    for i, r in enumerate(refs):
                        if r.device in devs:
                            g = rng_stream(self.seed, "lo_jump", r.device, r.channel, direction, n)
                            dphi = float(g.uniform(math.radians(60), math.radians(300)))
                            self._lo_jumps.append((Tj, direction, i, dphi))
                for d in devs:
                    self._events.append(AsyncEvent("lo_unlock", detail=d))
            elif kind == "pps_missing":
                if not any(e is None for _s, e in self._pps_gaps):
                    self._pps_gaps.append([self._T, None])
            elif kind == "overflow":
                self._faults[kind] = {"count": int(kw.get("count", 1))}
            elif kind == "underflow_storm":
                self._faults[kind] = {"count": int(kw.get("count", 8)), "bursts": int(kw.get("bursts", 1))}
            elif kind in ("late", "tx_hang"):
                self._faults[kind] = {"bursts": int(kw.get("bursts", 1))}
            elif kind == "reverse_power":
                self._faults[kind] = {"dbm": float(kw.get("dbm", 40.0)), "chains": kw.get("chains")}
            elif kind == "over_temp":
                self._faults[kind] = {"temp_c": float(kw.get("temp_c", 150.0))}
            else:
                self._faults[kind] = dict(kw)
            logger.info("sim: injected fault %s %s", kind, kw)

    def clear_faults(self) -> None:
        with self._lock:
            self._trace("clear_faults")
            had_ref = self._faults.get("ref_unlock", {}).get("devices", ())
            self._faults.clear()
            for g in self._pps_gaps:
                if g[1] is None:
                    g[1] = self._T
            for d in had_ref:
                self._reanchor(d)

    def faults(self) -> dict:
        return {k: dict(v) for k, v in self._faults.items()}

    def telemetry(self):
        """Forward / reverse power (detector hold ``DETECTOR_HOLD_S``) and temperature per TX chain."""
        import active_rf_control as arf
        K = self.num_tx()
        hold = self._tel["end"] is not None and self._T <= self._tel["end"] + as_fraction(DETECTOR_HOLD_S)
        fwd = list(self._tel["fwd"]) if hold else [None] * K
        rev = list(self._tel["rev"]) if hold else [None] * K
        temp = [float(t) for t in self._tx_temp]
        rp = self._faults.get("reverse_power")
        if rp is not None:
            for c in (range(K) if rp["chains"] is None else rp["chains"]):
                rev[int(c)] = rp["dbm"]
                if fwd[int(c)] is None:
                    fwd[int(c)] = rp["dbm"]
        ot = self._faults.get("over_temp")
        if ot is not None:
            temp = [ot["temp_c"]] * K
        age = math.inf if "telemetry_stale" in self._faults else 0.0
        return arf.Telemetry(fwd, rev, temp, age)

    def far_field_probe(self, az_deg, el_deg) -> np.ndarray:
        """Field (sqrt(mW) x element field gain) of the last radiated burst toward (az, el):
        ``sum_n y_n(t + u.r_n/c) exp(+j k0 u.r_n) g_e(u)``; empty when nothing was radiated."""
        with self._lock:
            b = self._last_radiated
            if b is None:
                return np.zeros(0, dtype=np.complex128)
            u = ageo.direction_vector(az_deg, el_deg)
            fs, fc = b["fs"], b["fc"]
            acc = np.zeros(b["y"].shape[1], dtype=np.complex128)
            for j, c in enumerate(b["chains"]):
                if not self._tx_mapped[c]:
                    continue
                tau = float(u @ self._tx_pos[c]) / C0
                ge = self._element_field(self._tx_pat[c], u)
                acc += adsp.frac_delay_fft(b["y"][j], -tau * fs) * np.exp(2j * math.pi * fc * tau) * ge
            return acc[b["g"]:b["g"] + b["n"]]

    def status(self) -> dict:
        st = super().status()
        st.update({"flavour": self.flavour, "seed": self.seed, "true_time_s": float(self._T),
                   "wiring": self.wiring(), "cal_input": self.cal_input(), "faults": sorted(self._faults),
                   "damage_events": len(self.damage_log)})
        return st
