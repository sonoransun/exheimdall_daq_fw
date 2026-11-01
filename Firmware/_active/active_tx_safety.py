"""
    HeIMDALL "active" capability: TX safety controller (SAFETY-CRITICAL).

    Every transmission of the active capability passes through this module:

      - ``TxPolicy``: the ``[active_tx]`` limits, resolved against the device caps
        (an empty ``max_tx_gain_db`` means the caps MINIMUM, fail-safe);
      - the arming state machine ``DISABLED / SAFE / ARMED / TRANSMITTING /
        FAULT`` (spec 12.2). DISABLED (``en_tx = 0`` or ``mode = rx``) is terminal
        for the process; FAULT is latched until ``reset_fault`` with the exact
        phrase, and a reset returns to SAFE, never ARMED;
      - the ordered arming preflight A1..A15 (spec 12.3): typed confirmation
        phrase (radiate only from an interactive TTY), frequency policy, ref
        lock / coherence, interlock, dummy-load sense, telemetry, static power
        feasibility, RX protection, EIRP / MPE, power calibration;
      - per-burst authorization B1..B12 (spec 12.4) on the FINAL buffer, which
        is frozen into a read-only ``TxPermit``: the backend sends
        ``permit.samples`` only (no checked/sent mismatch). Permits live in an
        identity registry (``registry[id] is permit``), are single use, bound to
        the arm session and carry a deadline; violations at ``consume`` time are
        trips;
      - duty: every issued permit reserves its duty at the burst's REAL start
        (timed: ``now + lead``; untimed: the consume time - ``at_offset_s`` is
        accepted only for flowgraph bursts of an adopted controller) until
        consumed / revoked / expired; ``consume`` re-checks it. Untimed bursts
        are refused once a hardware (non-simulation) backend is attached. The
        tuned frequency (``tx_freq``) is authoritative and is re-read at
        ``consume`` and by ``poll`` (retune: ARMED -> SAFE, a trip while
        TRANSMITTING); ``fs_hz`` must equal the TX rate; a declared
        ``occupied_bw_hz`` is verified on the samples (99 % power + peak bin)
        and an emission mask keeps the energy on bins the frequency policy
        forbids (LO images included) below -50 dBc; ``consume`` verifies that
        the front end is in the state the RX protection credited (LNA off, T/R
        tx);
      - PEP from the reconstructed envelope: every PEP-based limit uses the
        peak of an 8x band-limited interpolation of each chain (inter-sample
        peaks, burst edges), and an envelope above DAC full scale is refused;
      - active reflection (B10) is evaluated on the burst samples
        (``b(t) = S a(t)``) whenever an S-matrix is configured, whatever the
        declared weights; radiate with several chains always needs a valid TX
        calibration (``CAL_REQUIRED``);
      - the front-end model, power-cal table, isolation, S-matrix and geometry
        are tied to the fingerprint they were built from: the arm record
        carries THAT fingerprint, and an arm after a change rebuilds the model
        (or refuses ``CONFIG_CHANGED`` for an injected one);
      - trip generation: every trip / ``safe_shutdown`` bumps a counter, and
        ``arm`` / ``consume`` / ``burst_complete`` / ``refresh`` /
        ``authorize_burst`` re-check it right before and right after their
        state commit, so a signal handler that runs inside the critical section
        (the RLock is re-entrant) can never be overwritten by the commit;
      - PA idle rule: the PA may be on only for a burst in flight, an issued
        unexpired permit or the hold session. A refused ``consume``, an
        abandoned or expired permit and ``poll`` bring it back to RX;
      - trips (spec 12.5), evaluated deterministically by ``poll(now)`` (optional
        watchdog thread; sensor I/O outside the lock; ``trip`` never blocks on
        a busy lock), by event hooks and by signal / atexit handlers;
      - ``safe_shutdown``: the fixed, idempotent, never-raising fail-safe
        sequence (spec 12.8);
      - the arm record (``<state_dir>/tx_arm.json``, mode 0600) and
        ``flowgraph_guard`` for generated GNU Radio TX scripts (spec 13.5). An
        adopted (flowgraph) controller re-reads the record on every poll and
        stops when the arming process disarms, trips or re-arms.

    Refusals (``SafetyRefusal``) leave the state unchanged and emit no RF. Trips
    latch FAULT and run ``safe_shutdown`` before ``InterlockTrip`` is raised.

    What this does NOT replace: hardware interlocks. The software checks are
    millisecond-class; an idle DAC leaks LO carrier ("no samples" is not "RF
    off"); PA disable is what removes RF. An E-stop loop wired to PA enable and
    PA foldback are REQUIRED for amplified systems.

    Ordering note: within the arming preflight the static RX-protection check
    (A12) is evaluated after the dummy-load / cabled rating checks (A13) so the
    more specific code wins; both refuse.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import atexit
import contextlib
import dataclasses
import enum
import json
import logging
import math
import os
import signal
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

import numpy as np

import active_common as ac
from active_common import (EVT_ACTIVE_SHUTDOWN, EVT_ACTIVE_STATION_ID, EVT_ACTIVE_TX_ARM,
                           EVT_ACTIVE_TX_BURST, EVT_ACTIVE_TX_DISARM, EVT_ACTIVE_TX_EXPIRE,
                           EVT_ACTIVE_TX_FAULT_RESET, EVT_ACTIVE_TX_REFUSED, EVT_ACTIVE_TX_TRIP,
                           DevTime, EventLog, InterlockTrip, Issue, SafetyRefusal, TxResult)
import active_config as acfg
import active_devices as adev
import active_dsp as adsp
import active_frontend as afe
import active_regulatory as areg

logger = logging.getLogger("heimdall.active.tx_safety")


class TxState(str, enum.Enum):
    DISABLED = "disabled"
    SAFE = "safe"
    ARMED = "armed"
    TRANSMITTING = "transmitting"
    FAULT = "fault"


TX_MODES = ("dummy_load", "cabled", "radiate")
CONDUCTED_MODES = ("dummy_load", "cabled")

REFUSAL_CODES = (
    "TX_DISABLED", "FAULT_LATCHED", "ALREADY_ARMED", "MODE_NOT_ALLOWED", "CONFIRMATION",
    "CONFIRMATION_TTY_REQUIRED", "NO_OPERATOR", "BACKEND", "CONFIG_INVALID", "CONFIG_CHANGED",
    "FREQ_NOT_ALLOWED", "FREQ_DENIED", "FREQ_CHANGED", "REF_UNLOCK", "NO_SYNC", "COHERENCE_CLASS",
    "INTERLOCK_OPEN", "INTERLOCK_REQUIRED", "DUMMY_LOAD", "TELEMETRY_STALE", "NO_POWER_MONITOR",
    "REVERSE_POWER", "VSWR", "OVER_TEMP", "CHAIN_POWER", "TOTAL_POWER", "PA_COMPRESSION",
    "RX_PROTECTION", "DUMMY_LOAD_RATING", "CABLED_RX_OVERLOAD", "NO_POWER_CAL", "NO_AUTHORIZATION",
    "EIRP", "MPE", "PASSIVE_CHAIN", "CAL_REQUIRED", "CAL_MODE", "NOT_ARMED", "ARM_EXPIRED",
    "ARM_TOTAL", "WATCHDOG", "SHAPE", "NONFINITE", "AMPLITUDE", "GAIN", "BURST_LENGTH", "DUTY",
    "ACTIVE_REFLECTION", "START_TIME", "PERMIT_EXPIRED", "SEQUENCE", "TX_ARMED",
)
TRIP_CODES = (
    "HEARTBEAT", "TX_HANG", "UNDERFLOW_STORM", "REF_UNLOCK", "LO_UNLOCK", "REVERSE_POWER", "VSWR",
    "ACTIVE_REFLECTION", "OVER_TEMP", "INTERLOCK_OPEN", "DUMMY_LOAD_LOST", "TELEMETRY_STALE",
    "PERMIT_FORGED", "PERMIT_REUSED", "DIGEST", "EXCEPTION", "SIGNAL", "EXIT_WHILE_ARMED", "SEQUENCE",
    "FREQ_CHANGED",                    # the tuned TX frequency moved while TRANSMITTING
)

# Burst purposes whose energy is routed to the loopback reference RX (TX calibration).
LOOPBACK_PURPOSES = ("tx_cal", "loopback", "loopback_tdm", "loopback_hadamard")
ARM_RECORD_NAME = "tx_arm.json"
VSWR_MIN_FWD_DBM = -20.0           # VSWR is evaluated only above this forward power
FINGERPRINT_POLL_S = 1.0           # poll() re-hashes the config at most this often
FREQ_TOL_HZ = 1.0
FS_REL_TOL = 1e-6                  # burst fs_hz must equal the configured / device TX rate
F32_REL_TOL = 1e-6                 # complex64 rounding allowance on the frozen permit samples
# Occupied-bandwidth verification: at most this fraction of a chain's burst energy may lie
# outside the armed band (the 99 % power definition of occupied bandwidth, ITU-R SM.328),
# and the strongest spectral bin must lie inside it.
OCC_OOB_MAX_FRACTION = 0.01
# Emission mask: at most this fraction of a chain's burst energy (-50 dBc in total) may fall on
# spectral bins (or their LO images) that the frequency policy forbids (outside every allow band,
# in a configured or built-in deny band). The 99 % rule above only proves that the declared band
# is honest; this proves that nothing significant is emitted where transmitting is not allowed.
FORBIDDEN_MAX_FRACTION = 1e-5
SPEC_MIN_BINS_PER_BAND = 8         # spectral resolution of the B12 sample checks
SPEC_MAX_PAD = 1 << 20             # cap of the zero-padded FFT length
# Inter-sample peak: the analog envelope after DAC reconstruction is estimated on an
# ISP_OVERSAMPLE x band-limited (FFT) interpolation of each chain, with the idle (zero) DAC
# before and after the burst. Every PEP-based limit uses max(sample peak, reconstructed peak).
ISP_OVERSAMPLE = 8
ISP_BLOCK = 1 << 14                # interpolation core block (samples)
ISP_GUARD = 256                    # overlap on each side of a block (circular-wrap guard)
DAC_FULL_SCALE = 1.0               # a reconstructed envelope above this clips in the DUC / DAC
TRIP_LOCK_TIMEOUT_S = 1.0          # trip() proceeds without the lock after this wait
_REL_TOL = 1e-9

# Preflight codes evaluated after A6 (their static issues are not CONFIG_INVALID).
_LATER_CODES = frozenset((
    "FREQ_NOT_ALLOWED", "FREQ_DENIED", "CHAIN_POWER", "PA_COMPRESSION", "RX_PROTECTION",
    "DUMMY_LOAD", "DUMMY_LOAD_RATING", "CABLED_RX_OVERLOAD", "NO_POWER_CAL", "NO_AUTHORIZATION",
    "EIRP", "MPE", "PASSIVE_CHAIN", "TOTAL_POWER"))
_ARM_ORDER = ("CHAIN_POWER", "PA_COMPRESSION", "DUMMY_LOAD_RATING", "CABLED_RX_OVERLOAD",
              "RX_PROTECTION", "NO_POWER_CAL", "NO_AUTHORIZATION", "EIRP", "MPE", "PASSIVE_CHAIN")
_BURST_ORDER = ("CHAIN_POWER", "PA_COMPRESSION", "TOTAL_POWER", "DUMMY_LOAD_RATING",
                "CABLED_RX_OVERLOAD", "NO_POWER_CAL", "EIRP", "MPE", "RX_PROTECTION", "PASSIVE_CHAIN")


def ack_phrase(mode: str, fc_hz: float) -> str:
    """The exact arming confirmation, e.g. ``"ARM DUMMY_LOAD 915.000000 MHz"``."""
    return "ARM %s %.6f MHz" % (str(mode).upper(), float(fc_hz) / 1e6)


def reset_phrase(code: str) -> str:
    """The exact fault-reset confirmation ``"RESET FAULT <CODE>"``."""
    return "RESET FAULT %s" % str(code).upper()


def vswr_from_powers(fwd_dbm, rev_dbm) -> float:
    """VSWR from forward / reverse power: ``|G| = sqrt(P_rev/P_fwd)``, ``(1+|G|)/(1-|G|)``."""
    g = math.sqrt(10.0 ** ((float(rev_dbm) - float(fwd_dbm)) / 10.0))
    return math.inf if g >= 1.0 else (1.0 + g) / (1.0 - g)


def gamma_from_vswr(vswr) -> float:
    v = float(vswr)
    return (v - 1.0) / (v + 1.0) if math.isfinite(v) else 1.0


def reconstructed_peak(x, oversample=ISP_OVERSAMPLE, block=ISP_BLOCK, guard=ISP_GUARD) -> np.ndarray:
    """Per-row peak of the band-limited reconstruction of ``x`` (K, N).

    Each row is embedded in ``guard`` zeros on both sides (the idle DAC), then
    interpolated ``oversample`` x by zero-padding its spectrum, block by block
    (cores of ``block`` samples with ``guard`` samples of overlap whose output
    is discarded). Returns ``max(sample peak, reconstructed peak)`` per row:
    the inter-sample peak of the analog envelope that the DAC produces, which
    can exceed the largest sample by several dB (e.g. two tones at +/- fs/4).
    """
    a = np.atleast_2d(np.asarray(x, dtype=np.complex128))
    k, n = a.shape
    os_ = max(1, int(oversample))
    g = max(1, int(guard))
    out = np.max(np.abs(a), axis=1) if n else np.zeros(k)
    if os_ == 1 or n == 0:
        return out
    blk = max(1, int(block))
    for r in range(k):
        y = np.concatenate((np.zeros(g, complex), a[r], np.zeros(g, complex)))
        L = y.size
        best = out[r]
        for c0 in range(0, L, blk):
            c1 = min(L, c0 + blk)
            s0, s1 = max(0, c0 - g), min(L, c1 + g)
            seg = y[s0:s1]
            m = seg.size
            X = np.fft.fft(seg)
            h = (m + 1) // 2                     # bins 0..h-1 positive, the rest negative
            Xu = np.zeros(m * os_, complex)
            Xu[:h] = X[:h]
            if m % 2 == 0:                       # split the Nyquist bin between +/- fs/2
                Xu[h] = 0.5 * X[h]
                Xu[-h] = 0.5 * X[h]
                if m - h - 1 > 0:
                    Xu[m * os_ - (m - h - 1):] = X[h + 1:]
            else:
                Xu[m * os_ - (m - h):] = X[h:]
            yu = np.fft.ifft(Xu) * os_
            lo = (c0 - s0) * os_
            hi = lo + (c1 - c0) * os_
            pk = float(np.max(np.abs(yu[lo:hi]))) if hi > lo else 0.0
            if pk > best:
                best = pk
        out[r] = best
    return out


def _finite_or_none(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TxPolicy:
    """``[active_tx]`` plus the context the safety checks need (all resolved)."""
    enabled: bool
    mode_cfg: str
    backend: str
    operator_cfg: str
    fc_hz: float
    fs_hz: float
    lo_offset_hz: float
    n_tx: int
    n_rx: int
    allowed_modes: tuple
    authorization: str
    authorization_kind: str
    callsign: str
    station_id_interval_s: float
    allow_bands: tuple
    deny_bands: tuple
    conducted_ignore_builtin_deny: bool
    occupied_bw_cfg_hz: float           # 0 = the sample rate
    arm_timeout_s: float
    max_arm_total_s: float
    heartbeat_timeout_s: float
    max_digital_amplitude: float
    max_tx_gain_db: tuple               # resolved: "" -> caps minimum
    tx_gain_min_db: tuple               # caps range per chain
    tx_gain_max_db: tuple
    max_chain_power_dbm: tuple
    max_total_power_dbm: float
    max_eirp_dbm: float
    mpe_exclusion_m: float
    max_burst_s: float
    duty_max: float
    duty_window_s: float
    papr_margin_db: float
    pa_backoff_db: float
    dummy_load_rating_w: tuple
    dummy_load_pep_w: tuple             # effective (0 -> rating)
    cabled_rx_path_loss_db: tuple
    loopback_path_loss_db: tuple
    rx_safe_margin_db: float
    max_reverse_power_dbm: float        # None = not set
    max_vswr: float
    max_temp_c: float
    require_power_monitor_for_radiate: bool
    underflow_trip_count: int
    underflow_window_s: float
    mute_rx_during_tx: bool
    lna_off_during_tx: bool
    protect_passive_chain: bool
    passive_isolation_db: float
    passive_rx_damage_dbm: float
    min_start_lead_s: float
    min_tx_lead_s: float                # max caps min_tx_lead_s over the TX devices
    require_ref_lock: bool
    pa_hold_between_bursts: bool
    use_atr: bool
    pa_settle_us: float
    tr_guard_us: float
    telemetry_max_age_s: float
    interlock: str
    dummy_load_sense: str
    power_monitor: str
    lna_bias_line: str
    tr_switch_line: str
    pa_enable_line: str
    loopback_rx_chan: int
    tx_cal_method: str

    @staticmethod
    def from_config(cfg) -> "TxPolicy":
        t = cfg.tx
        n_tx, n_rx = cfg.num_tx(), cfg.num_rx()
        refs = cfg.tx_refs()
        caps = [adev.get_caps(cfg.device(r.device).family) for r in refs]
        gmin = tuple(float(c.tx_gain_range_db[0]) for c in caps)
        gmax = tuple(float(c.tx_gain_range_db[1]) for c in caps)
        max_gain = tuple(adev.resolve_max_tx_gain_db(cfg)) if n_tx else ()

        def per(v, default=0.0):
            v = tuple(float(x) for x in v)
            return v if len(v) == n_tx else (v[:1] * n_tx if len(v) == 1 else (float(default),) * n_tx)

        rating = per(t.dummy_load_rating_w)
        pep = per(t.dummy_load_pep_w)
        rc = cfg.rf_control
        return TxPolicy(
            enabled=cfg.tx_enabled(), mode_cfg=cfg.active.mode, backend=cfg.active.backend,
            operator_cfg=cfg.active.operator, fc_hz=float(cfg.rf.center_freq_hz),
            fs_hz=float(cfg.rf.sample_rate_hz), lo_offset_hz=float(cfg.rf.lo_offset_hz), n_tx=n_tx,
            n_rx=n_rx, allowed_modes=tuple(t.allowed_modes), authorization=t.authorization,
            authorization_kind=t.authorization_kind, callsign=t.callsign,
            station_id_interval_s=float(t.station_id_interval_s),
            allow_bands=tuple(tuple(b) for b in t.allow_bands),
            deny_bands=tuple(tuple(b) for b in t.deny_bands),
            conducted_ignore_builtin_deny=bool(t.conducted_ignore_builtin_deny),
            occupied_bw_cfg_hz=float(t.occupied_bw_hz), arm_timeout_s=float(t.arm_timeout_s),
            max_arm_total_s=float(t.max_arm_total_s), heartbeat_timeout_s=float(t.heartbeat_timeout_s),
            max_digital_amplitude=float(t.max_digital_amplitude), max_tx_gain_db=max_gain,
            tx_gain_min_db=gmin, tx_gain_max_db=gmax, max_chain_power_dbm=per(t.max_chain_power_dbm),
            max_total_power_dbm=float(t.max_total_power_dbm), max_eirp_dbm=float(t.max_eirp_dbm),
            mpe_exclusion_m=float(t.mpe_exclusion_m), max_burst_s=float(t.max_burst_s),
            duty_max=float(t.duty_max), duty_window_s=float(t.duty_window_s),
            papr_margin_db=float(t.papr_margin_db), pa_backoff_db=float(t.pa_backoff_db),
            dummy_load_rating_w=rating,
            dummy_load_pep_w=tuple(p if p > 0 else r for p, r in zip(pep, rating)),
            cabled_rx_path_loss_db=per(t.cabled_rx_path_loss_db),
            loopback_path_loss_db=per(t.loopback_path_loss_db),
            rx_safe_margin_db=float(t.rx_safe_margin_db),
            max_reverse_power_dbm=_finite_or_none(t.max_reverse_power_dbm), max_vswr=float(t.max_vswr),
            max_temp_c=float(t.max_temp_c),
            require_power_monitor_for_radiate=bool(t.require_power_monitor_for_radiate),
            underflow_trip_count=int(t.underflow_trip_count),
            underflow_window_s=float(t.underflow_window_s), mute_rx_during_tx=bool(t.mute_rx_during_tx),
            lna_off_during_tx=bool(t.lna_off_during_tx), protect_passive_chain=bool(t.protect_passive_chain),
            passive_isolation_db=float(t.passive_isolation_db),
            passive_rx_damage_dbm=float(t.passive_rx_damage_dbm),
            min_start_lead_s=float(t.min_start_lead_s),
            min_tx_lead_s=max([float(c.min_tx_lead_s) for c in caps] or [0.0]),
            require_ref_lock=bool(cfg.timing.require_ref_lock),
            pa_hold_between_bursts=bool(rc.pa_hold_between_bursts), use_atr=bool(rc.use_atr),
            pa_settle_us=float(rc.pa_settle_us), tr_guard_us=float(rc.tr_guard_us),
            telemetry_max_age_s=float(rc.telemetry_max_age_s), interlock=rc.interlock.strip(),
            dummy_load_sense=rc.dummy_load_sense.strip(), power_monitor=rc.power_monitor.strip(),
            lna_bias_line=rc.lna_bias_line.strip(), tr_switch_line=rc.tr_switch_line.strip(),
            pa_enable_line=rc.pa_enable_line.strip(), loopback_rx_chan=int(cfg.calibration.loopback_rx_chan),
            tx_cal_method=cfg.calibration.tx_method)

    def occupied_bw_hz(self, fs_hz=None) -> float:
        return self.occupied_bw_cfg_hz if self.occupied_bw_cfg_hz > 0 else float(
            self.fs_hz if fs_hz is None else fs_hz)

    def to_dict(self) -> dict:
        return ac.to_jsonable(self)


# ---------------------------------------------------------------------------
# Arm record and permits
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ArmRecord:
    session_id: str
    mode: str
    operator: str
    fc_hz: float
    occupied_bw_hz: float
    armed_unix: float
    expires_unix: float
    fingerprint: str
    flowgraph_sha256: str = ""
    waveform_sha256: str = ""
    max_bursts: int = 0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d) -> "ArmRecord":
        if not isinstance(d, dict):
            raise ValueError("arm record must be a JSON object")
        kw = {}
        for f in dataclasses.fields(ArmRecord):
            if f.name not in d:
                if f.default is dataclasses.MISSING:
                    raise ValueError("arm record lacks %r" % f.name)
                continue
            v = d[f.name]
            if f.type in ("str",):
                if not isinstance(v, str):
                    raise ValueError("arm record %s must be a string" % f.name)
            elif f.type in ("float",):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                    raise ValueError("arm record %s must be a number" % f.name)
                v = float(v)
            elif f.type in ("int",):
                if isinstance(v, bool) or not isinstance(v, int):
                    raise ValueError("arm record %s must be an integer" % f.name)
            kw[f.name] = v
        if kw["mode"] not in TX_MODES:
            raise ValueError("arm record mode %r unknown" % kw["mode"])
        return ArmRecord(**kw)


@dataclass(frozen=True, eq=False)
class TxPermit:
    """Authorization for exactly one burst of exactly these (read-only) samples."""
    permit_id: str
    session_id: str
    samples: np.ndarray                 # read-only complex64 copy, (K, N)
    chains: tuple
    fc_hz: float
    fs_hz: float
    tx_gains_db: tuple
    start: DevTime                      # None only for sim/test immediate sends
    duration_s: float
    digest: str                         # sha256 hex of samples.tobytes()
    issued_mono: float
    deadline_mono: float
    purpose: str
    prediction: tuple                   # TxPrediction per chain (order of ``chains``)


class DutyCycleMeter:
    """Sliding-window transmit duty cycle (busy time / window).

    ``record`` holds bursts that were sent (or committed to a start time);
    ``reserve`` holds the duty of issued, not yet consumed permits so that
    several outstanding permits cannot each see an empty meter. ``would_exceed``
    counts both and evaluates EVERY window that overlaps the candidate burst
    (the busy time of a sliding window is piecewise linear, so its maximum is
    at a breakpoint), not only the window ending at the burst end: future
    (timed) bursts are therefore accounted at their real start.
    """

    def __init__(self, window_s, duty_max, clock=time.monotonic):
        self.window_s = float(window_s)
        self.duty_max = float(duty_max)
        if not self.window_s > 0 or not 0 < self.duty_max <= 1:
            raise ValueError("duty window must be > 0 and duty_max in (0, 1]")
        self._clock = clock
        self._bursts = deque()
        self._reserved = {}

    def _now(self, now):
        return float(self._clock() if now is None else now)

    def _busy(self, t0, t1) -> float:
        return sum(max(0.0, min(s + d, t1) - max(s, t0)) for s, d in self._bursts)

    def _prune(self, now):
        horizon = now - self.window_s
        self._bursts = deque(b for b in sorted(self._bursts) if b[0] + b[1] >= horizon)

    def _intervals(self, exclude=None):
        out = list(self._bursts)
        out += [v for k, v in self._reserved.items() if k != exclude]
        return out

    def max_busy(self, start, dur_s, exclude=None) -> float:
        """Largest busy time of any window that overlaps a burst ``[start, start+dur]``
        (recorded + reserved bursts plus the candidate)."""
        s0 = float(start)
        d0 = max(0.0, float(dur_s))
        e0 = s0 + d0
        w = self.window_s
        iv = [(s, d) for s, d in self._intervals(exclude) if s + d > s0 - w and s < e0 + w]
        iv.append((s0, d0))
        S = np.array([s for s, _ in iv], dtype=float)
        E = S + np.array([d for _, d in iv], dtype=float)
        cand = np.concatenate([S, E, S + w, E + w, [s0, e0, e0 + w]])
        cand = cand[(cand >= s0 - 1e-12) & (cand <= e0 + w + 1e-12)]
        if cand.size == 0:
            return d0
        lo = cand[:, None] - w
        ov = np.clip(np.minimum(E[None, :], cand[:, None]) - np.maximum(S[None, :], lo), 0.0, None)
        return float(np.max(ov.sum(axis=1)))

    def would_exceed(self, dur_s, now=None, exclude=None) -> bool:
        """True when a burst of ``dur_s`` starting at ``now`` would push the busy
        time of any sliding window above ``duty_max * window`` (sent, committed
        and reserved bursts all count; ``exclude`` names a reservation to ignore)."""
        start = self._now(now)
        busy = self.max_busy(start, dur_s, exclude)
        return busy > self.duty_max * self.window_s * (1.0 + _REL_TOL) + 1e-12

    def reserve(self, key, start_mono, dur_s) -> None:
        self._reserved[key] = (float(start_mono), float(dur_s))

    def release(self, key) -> None:
        self._reserved.pop(key, None)

    def release_all(self) -> None:
        self._reserved.clear()

    def reserved(self) -> int:
        return len(self._reserved)

    def record(self, start_mono, dur_s, key=None) -> None:
        """Record a burst (and drop its reservation ``key``)."""
        if key is not None:
            self._reserved.pop(key, None)
        self._bursts.append((float(start_mono), float(dur_s)))
        try:
            now = float(self._clock())
        except Exception:
            now = float(start_mono)
        self._prune(min(float(start_mono), now))

    def duty(self, now=None) -> float:
        now = self._now(now)
        return self._busy(now - self.window_s, now) / self.window_s

    def clear(self) -> None:
        self._bursts.clear()
        self._reserved.clear()


class _Entry:
    __slots__ = ("permit", "samples", "digest", "session", "consumed", "completed", "duty_at", "at_offset",
                 "lna_credit", "tr_credit")

    def __init__(self, permit, duty_at, at_offset=0.0, lna_credit=False, tr_credit=False):
        self.permit = permit
        self.samples = permit.samples
        self.digest = permit.digest
        self.session = permit.session_id
        self.consumed = False
        self.completed = False
        self.duty_at = duty_at              # start of the burst on the monotonic clock (reserved duty)
        self.at_offset = at_offset          # untimed bursts: offset of the start from consume()
        self.lna_credit = lna_credit        # B9 assumed the LNA bias off
        self.tr_credit = tr_credit          # B9 assumed the T/R switch in tx


def _arm_path(state_dir) -> str:
    return os.path.join(state_dir, ARM_RECORD_NAME)


def write_arm_record(state_dir, rec: ArmRecord) -> str:
    """Write ``<state_dir>/tx_arm.json`` atomically with mode 0600; returns the path."""
    path = _arm_path(state_dir)
    ac.atomic_write_json(path, rec.to_dict(), mode=0o600)
    return path


def read_arm_record(state_dir):
    """The arm record, or None when missing or corrupt."""
    try:
        with open(_arm_path(state_dir), "r", encoding="utf-8") as fh:
            return ArmRecord.from_dict(json.load(fh))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def clear_arm_record(state_dir) -> None:
    """Delete the arm record (never raises)."""
    try:
        os.unlink(_arm_path(state_dir))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
def _obj_configured(obj) -> bool:
    if obj is None:
        return False
    fn = getattr(obj, "configured", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return True
    return True


def _obj_closed(obj):
    """InterlockInput-like (``closed()``) or a callable returning bool/None."""
    try:
        fn = getattr(obj, "closed", None)
        v = fn() if callable(fn) else obj()
    except Exception:
        return False
    return None if v is None else bool(v)


class TxSafetyController:
    """The TX arming state machine, per-burst authorization and fail-safe shutdown."""

    def __init__(self, cfg, frontend, geometry, event_log, *, clock=time.monotonic, wallclock=time.time,
                 sleep=time.sleep, state_dir=None, is_simulation=False,
                 sync_ok=lambda: False, ref_locked=lambda: None, coherence_class=lambda: 0,
                 tx_cal_valid=lambda: False, telemetry=None, interlock=None, dummy_load_sense=None,
                 time_now=None, tx_freq=None, tx_rate=None, adopt_record: ArmRecord = None):
        self.cfg = cfg
        self.policy = TxPolicy.from_config(cfg)
        self._clock = clock
        self._wallclock = wallclock
        self._sleep = sleep
        self.event_log = event_log if event_log is not None else EventLog()
        self.is_simulation = bool(is_simulation)
        self._sync_ok = sync_ok
        self._ref_locked = ref_locked
        self._coherence_class = coherence_class
        self._tx_cal_valid = tx_cal_valid
        self._telemetry = telemetry
        self._interlock = interlock
        self._dl_sense = dummy_load_sense
        self._time_now = time_now
        self._tx_freq = tx_freq            # () -> tuned TX centre (Hz): authoritative when given
        self._tx_rate = tx_rate            # () -> actual TX sample rate (Hz): else the configured rate
        self._adopted = False
        # Bumped by every trip / safe_shutdown (also from a signal handler running inside a
        # critical section): consume / arm / burst_complete / refresh never commit a state
        # across a bump.
        self._trip_gen = 0
        # The front-end model / geometry and the fingerprint of the config + auxiliary files
        # they were built from (``_model_fp``): the arm record carries THIS fingerprint, so a
        # table edited after the model was built is a CONFIG_CHANGED, never a silent mismatch.
        self._geometry_in = geometry
        self._frontend_in = frontend
        self._owns_model = frontend is None and geometry is None
        self.geometry = None
        self.frontend = None
        self._model_fp = None
        self._load_model()
        self.state_dir = state_dir if state_dir is not None else cfg.resolved_state_dir()
        self.backend = None
        self.rf = None
        self._lock = threading.RLock()
        self.state = TxState.SAFE if self.policy.enabled else TxState.DISABLED
        self.arm_record = None
        self._mode = None
        self._beamformed = False
        self._armed_mono = None
        self._expiry_mono = None
        self._last_hb = None
        self._registry = {}
        self._revoked = {}
        self._completed = deque(maxlen=4096)
        self._completed_set = set()
        self.last_banner = ""
        self._inflight = None
        self._underflows = deque()
        self.duty = DutyCycleMeter(self.policy.duty_window_s, self.policy.duty_max, clock)
        self.fault_code = ""
        self.fault_detail = ""
        self.fault_history = []
        self.last_shutdown = []
        self.last_arm_checks = []
        self._fp_checked = None
        self._station_next = None
        self._listeners = []
        self._guard_stop_fns = []
        self._watchdog = None
        self._watchdog_stop = threading.Event()
        self._hooks = None
        if adopt_record is not None:
            self._adopt(adopt_record)
        else:
            stale = read_arm_record(self.state_dir) if os.path.exists(_arm_path(self.state_dir)) else None
            if os.path.exists(_arm_path(self.state_dir)):
                clear_arm_record(self.state_dir)
                logger.warning("TX safety: deleted a stale arm record in %s", self.state_dir)
                self._event(EVT_ACTIVE_TX_DISARM, "warning", reason="stale_arm_record",
                            session=getattr(stale, "session_id", ""), path=_arm_path(self.state_dir))

    # --- model ---------------------------------------------------------------------------
    def _load_model(self) -> None:
        """Build (or rebuild) the geometry and front-end model from the config and stamp
        ``_model_fp`` with the fingerprint they were built from. The fingerprint is taken
        before and after the build; an auxiliary file that changes during the build is
        retried, and a model that cannot be tied to one fingerprint gets ``_model_fp =
        None`` (arming then refuses CONFIG_CHANGED). Injected models are kept as given and
        tied to the fingerprint at construction."""
        cfg = self.cfg
        fp = None
        geometry, frontend = self._geometry_in, self._frontend_in
        for _ in range(3):
            fp0 = self._fingerprint()
            geometry = self._geometry_in
            if geometry is None:
                try:
                    import active_geometry as ageo
                    geometry = ageo.geometry_from_config(cfg)
                except Exception as exc:  # geometry problems are reported by validate_static
                    logger.warning("TX safety: geometry unavailable: %s", exc)
                    geometry = None
            frontend = self._frontend_in if self._frontend_in is not None else \
                afe.FrontendModel.from_config(cfg, geometry)
            fp1 = self._fingerprint()
            if fp0 == fp1:
                fp = fp1
                break
            if not self._owns_model:
                break
        self.geometry = geometry
        self.frontend = frontend
        self._model_fp = fp

    def _model_current(self):
        """None when the model in use matches the config / aux files on disk; else why.
        A model this controller built is rebuilt first (a rebuild that raises is
        reported as the reason)."""
        try:
            cur = self._fingerprint()
        except Exception as exc:
            return "cannot fingerprint the configuration: %r" % (exc,)
        if self._model_fp is not None and cur == self._model_fp:
            return None
        if not self._owns_model:
            return ("the injected front-end model / geometry predates a change of the configuration or of "
                    "an auxiliary table: build a new controller")
        try:
            self._load_model()
            self.policy = TxPolicy.from_config(self.cfg)
            self.duty.window_s = float(self.policy.duty_window_s)
            self.duty.duty_max = float(self.policy.duty_max)
        except Exception as exc:
            self._model_fp = None
            return "rebuilding the front-end model failed: %r" % (exc,)
        if self._model_fp is None:
            return "an auxiliary table kept changing while the front-end model was rebuilt"
        logger.warning("TX safety: configuration / auxiliary tables changed since the model was built: rebuilt")
        return None

    # --- small helpers --------------------------------------------------------------
    def _event(self, etype, severity="info", **payload):
        try:
            self.event_log.emit(etype, severity, **payload)
        except Exception:
            pass

    def _refuse(self, code, detail, **payload):
        self._event(EVT_ACTIVE_TX_REFUSED, "warning", code=code, detail=detail,
                    state=self.state.value, session=getattr(self.arm_record, "session_id", ""), **payload)
        raise SafetyRefusal(code, detail)

    def _first_error(self, issues):
        for i in issues:
            if i.level == "error":
                return i
        return None

    @property
    def mode(self):
        return self._mode

    def add_status_listener(self, fn) -> None:
        """``fn(status_dict)`` is called after every ``safe_shutdown`` (sidecar update)."""
        self._listeners.append(fn)

    def attach(self, backend, rf_control) -> None:
        """Register the shutdown targets and build missing safety inputs from the config."""
        import active_rf_control as arf
        with self._lock:
            self.backend = backend
            self.rf = rf_control
            if backend is not None and bool(getattr(backend, "is_simulation", False)):
                self.is_simulation = True
            base = getattr(self.cfg, "base_dir", "")
            clk = self._clock
            if self._interlock is None and self.policy.interlock.lower() != "none":
                self._interlock = arf.InterlockInput(arf.resolve_line(arf.parse_line(self.policy.interlock), base),
                                                     backend, clk, self.policy.telemetry_max_age_s,
                                                     self._wallclock)
            if self._dl_sense is None and self.policy.dummy_load_sense.lower() != "none":
                self._dl_sense = arf.InterlockInput(
                    arf.resolve_line(arf.parse_line(self.policy.dummy_load_sense), base), backend, clk,
                    self.policy.telemetry_max_age_s, self._wallclock)
            if self._telemetry is None and self.policy.power_monitor.lower() != "none":
                self._telemetry = arf.PowerMonitor(
                    arf.resolve_line(arf.parse_line(self.policy.power_monitor), base), backend, clk,
                    self._wallclock)
            if rf_control is not None and getattr(rf_control, "safety", None) is None:
                fn = getattr(rf_control, "attach_safety", None)
                if callable(fn):
                    fn(self)

    def _atr(self) -> bool:
        return bool(getattr(self.rf, "atr_active", False)) if self.rf is not None else False

    # --- inputs ------------------------------------------------------------------------
    def _interlock_state(self):
        """None = not configured; else True only when positively closed."""
        cfgd = self.policy.interlock.lower() != "none"
        if self._interlock is None:
            return False if cfgd else None
        if not _obj_configured(self._interlock) and not cfgd:
            return None
        v = _obj_closed(self._interlock)
        return bool(v) if v is not None else False

    def _dl_state(self):
        cfgd = self.policy.dummy_load_sense.lower() != "none"
        if self._dl_sense is None:
            return False if cfgd else None
        if not _obj_configured(self._dl_sense) and not cfgd:
            return None
        v = _obj_closed(self._dl_sense)
        return bool(v) if v is not None else False

    def _monitor_configured(self) -> bool:
        return self.policy.power_monitor.lower() != "none" or _obj_configured(self._telemetry)

    def _read_telemetry(self):
        import active_rf_control as arf
        obj = self._telemetry
        try:
            if obj is None:
                return arf.Telemetry.empty()
            fn = getattr(obj, "read", None)
            tel = fn() if callable(fn) else obj()
            return arf.PowerMonitor._coerce(tel) if tel is not None else arf.Telemetry.empty()
        except Exception as exc:
            logger.error("telemetry read failed: %r", exc)
            return arf.Telemetry.empty()

    def _telemetry_violation(self, tel=None):
        """(code, detail) for the first telemetry limit violation, or None.

        With a monitor configured, a reading must be COMPLETE: a finite reverse
        power for every TX chain (reverse-power or VSWR limit), a finite forward
        power for every TX chain (VSWR limit) and at least one temperature with
        every entry finite (temperature limit). Missing / None entries are
        ``TELEMETRY_STALE`` (fail safe: a monitor that measures nothing is no
        monitor)."""
        if not self._monitor_configured():
            return None
        p = self.policy
        tel = self._read_telemetry() if tel is None else tel
        if not (tel.age_s <= p.telemetry_max_age_s):
            return "TELEMETRY_STALE", "telemetry age %s s > %g s" % (tel.age_s, p.telemetry_max_age_s)
        n_tx = p.n_tx
        vswr_on = math.isfinite(p.max_vswr) and p.max_vswr > 1.0
        rev_on = p.max_reverse_power_dbm is not None or vswr_on
        temp_on = math.isfinite(p.max_temp_c)

        def missing(vals, what):
            bad = [k for k in range(n_tx) if k >= len(vals) or _finite_or_none(vals[k]) is None]
            if bad:
                return "TELEMETRY_STALE", "power monitor reports no %s for TX chain(s) %s" % (what, bad)
            return None
        if rev_on:
            m = missing(tel.rev_dbm, "reverse power")
            if m:
                return m
        if vswr_on:
            m = missing(tel.fwd_dbm, "forward power")
            if m:
                return m
        if temp_on and (not tel.temp_c or any(_finite_or_none(t) is None for t in tel.temp_c)):
            return "TELEMETRY_STALE", "power monitor reports no (or a non-finite) temperature"
        n = max(len(tel.fwd_dbm), len(tel.rev_dbm))
        for k in range(len(tel.rev_dbm)):
            rev = _finite_or_none(tel.rev_dbm[k])
            if rev is not None and p.max_reverse_power_dbm is not None and rev > p.max_reverse_power_dbm:
                return "REVERSE_POWER", "chain %d reverse power %.2f dBm > %.2f dBm" % (
                    k, rev, p.max_reverse_power_dbm)
        for k in range(n):
            fwd = _finite_or_none(tel.fwd_dbm[k]) if k < len(tel.fwd_dbm) else None
            rev = _finite_or_none(tel.rev_dbm[k]) if k < len(tel.rev_dbm) else None
            if fwd is not None and rev is not None and fwd >= VSWR_MIN_FWD_DBM:
                v = vswr_from_powers(fwd, rev)
                if v > p.max_vswr:
                    return "VSWR", "chain %d VSWR %.2f > %.2f (fwd %.2f dBm, rev %.2f dBm)" % (
                        k, v, p.max_vswr, fwd, rev)
        for k, tc in enumerate(tel.temp_c):
            tcv = _finite_or_none(tc)
            if tcv is not None and tcv > p.max_temp_c:
                return "OVER_TEMP", "sensor %d at %.1f C > %.1f C" % (k, tcv, p.max_temp_c)
        return None

    def _ref_state(self):
        try:
            v = self._ref_locked()
        except Exception as exc:
            return False, "ref lock query failed: %r" % (exc,)
        if isinstance(v, dict):
            if any(x is False for x in v.values()):
                return False, "unlocked: %s" % ", ".join(sorted(k for k, x in v.items() if x is False))
            if not v or any(x is None for x in v.values()):
                return None, "no ref sensor on %s" % ", ".join(sorted(k for k, x in v.items() if x is None))
            return True, ""
        if v is None:
            return None, "ref lock unknown (no sensor)"
        return bool(v), "" if v else "reference not locked"

    def _fingerprint(self) -> str:
        return acfg.config_fingerprint(self.cfg)

    # --- static checks -------------------------------------------------------------------
    def _intervals(self, fc_hz, occ_hz, fs_hz):
        return areg.tx_frequency_intervals(fc_hz, occ_hz, self.policy.lo_offset_hz, fs_hz)

    def _freq_issues(self, mode, fc_hz, occ_hz, fs_hz):
        p = self.policy
        return areg.band_check(self._intervals(fc_hz, occ_hz, fs_hz), mode, p.allow_bands, p.deny_bands,
                               ignore_builtin=p.conducted_ignore_builtin_deny)

    def _rx_lna_on_during_tx(self) -> bool:
        return not self.policy.lna_off_during_tx

    def _power_issues(self, mode, fc_hz, occ_hz, preds, chains, purpose, static, lna_on=None, tr_state="tx"):
        """Mode power checks for chain predictions ``preds`` (dict chain -> TxPrediction).

        ``lna_on`` / ``tr_state``: the RX LNA bias and T/R position assumed during
        the burst (default: the post-sequencing state of the policy)."""
        p = self.policy
        if lna_on is None:
            lna_on = self._rx_lna_on_during_tx()
        fe = self.frontend
        out = []
        n_tx = p.n_tx
        peps = [afe.NEG_INF] * n_tx
        avgs = [afe.NEG_INF] * n_tx
        for n in chains:
            peps[n] = preds[n].out_pep_dbm
            avgs[n] = preds[n].out_avg_dbm
        where = "[active_tx] %s" % mode
        for n in chains:
            pr = preds[n]
            if pr.max_in_exceeded:
                out.append(Issue("error", "CHAIN_POWER", "TX %d: PEP at the input of stage %r exceeds its "
                                 "max_in" % (n, pr.max_in_exceeded), where))
            if pr.out_pep_dbm > p.max_chain_power_dbm[n] + 1e-9:
                out.append(Issue("error", "CHAIN_POWER", "TX %d: predicted PEP %.2f dBm > max_chain_power_dbm "
                                 "%.2f dBm" % (n, pr.out_pep_dbm, p.max_chain_power_dbm[n]), where))
            if pr.headroom_db < p.pa_backoff_db - 1e-9:
                out.append(Issue("error", "PA_COMPRESSION", "TX %d: headroom %.2f dB to P1dB of %r < "
                                 "pa_backoff_db %.2f dB" % (n, pr.headroom_db, pr.limiting_stage,
                                                            p.pa_backoff_db), where))
            if pr.unmodelled:
                out.append(Issue("error", "PA_COMPRESSION", "TX %d: amplifier(s) %s have no p1db/psat: "
                                 "compression cannot be predicted" % (n, ", ".join(pr.unmodelled)), where))
        if not static:
            tot = afe.power_sum_dbm(avgs)
            if tot > p.max_total_power_dbm + 1e-9:
                out.append(Issue("error", "TOTAL_POWER", "sum of chain averages %.2f dBm > "
                                 "max_total_power_dbm %.2f dBm" % (tot, p.max_total_power_dbm), where))
        if mode == "dummy_load":
            for n in chains:
                rating = p.dummy_load_rating_w[n]
                avg_w = afe._w(avgs[n])
                pep_w = afe._w(peps[n])
                if not rating > 0:
                    out.append(Issue("error", "DUMMY_LOAD_RATING", "TX %d: dummy_load_rating_w not set" % n, where))
                    continue
                if avg_w * p.duty_max > 0.5 * rating * (1 + _REL_TOL):
                    out.append(Issue("error", "DUMMY_LOAD_RATING", "TX %d: duty-weighted average %.4g W "
                                     "(%.4g W x duty %.3g) > 50%% of the %.4g W rating"
                                     % (n, avg_w * p.duty_max, avg_w, p.duty_max, rating), where))
                if pep_w > p.dummy_load_pep_w[n] * (1 + _REL_TOL):
                    out.append(Issue("error", "DUMMY_LOAD_RATING", "TX %d: PEP %.4g W > dummy-load PEP rating "
                                     "%.4g W" % (n, pep_w, p.dummy_load_pep_w[n]), where))
        lim = fe.rx_limits_dbm(lna_on) if fe.n_rx else []
        if mode == "cabled":
            for n in chains:
                loss = p.cabled_rx_path_loss_db[n]
                if not loss > 0:
                    out.append(Issue("error", "CABLED_RX_OVERLOAD", "TX %d: cabled_rx_path_loss_db not set" % n,
                                     where))
                    continue
                if n >= fe.n_rx:
                    out.append(Issue("error", "CABLED_RX_OVERLOAD", "TX %d: no RX %d to receive the cabled "
                                     "path" % (n, n), where))
                    continue
                lv = peps[n] - loss
                if lv > lim[n] + 1e-9:
                    out.append(Issue("error", "CABLED_RX_OVERLOAD", "TX %d -> RX %d: %.2f dBm after %.1f dB > "
                                     "safe %.2f dBm; add %.1f dB of attenuation"
                                     % (n, n, lv, loss, lim[n],
                                        afe.required_attenuation_db(lv, lim[n] + p.rx_safe_margin_db,
                                                                    p.rx_safe_margin_db)), where))
        loop = purpose in LOOPBACK_PURPOSES or (static and p.tx_cal_method.startswith("loopback")
                                                 and mode in CONDUCTED_MODES)
        lb = p.loopback_rx_chan
        if loop:
            if not 0 <= lb < fe.n_rx:
                out.append(Issue("error", "CABLED_RX_OVERLOAD", "loopback TX cal needs loopback_rx_chan "
                                 "(got %d)" % lb, "[active_calibration] loopback_rx_chan"))
            else:
                bad = [n for n in chains if not p.loopback_path_loss_db[n] > 0]
                if bad:
                    out.append(Issue("error", "CABLED_RX_OVERLOAD", "loopback_path_loss_db not set for TX %s"
                                     % bad, where))
                else:
                    lv = afe.coherent_sum_dbm([peps[n] - p.loopback_path_loss_db[n] for n in chains])
                    if lv > lim[lb] + 1e-9:
                        out.append(Issue("error", "CABLED_RX_OVERLOAD", "loopback RX %d: coherent sum %.2f dBm "
                                         "> safe %.2f dBm" % (lb, lv, lim[lb]), where))
        if fe.n_rx:
            prot_mode = "loopback" if (loop and mode == "dummy_load") else mode
            f_lo = max(1.0, fc_hz - occ_hz / 2.0)
            out.extend(fe.rx_protection(peps, prot_mode, lna_on, tr_state, f_lo)[0])
        elif mode == "radiate" and p.protect_passive_chain:
            out.extend(i for i in fe.rx_protection(peps, mode, True, "tx", fc_hz)[0])
        if mode == "radiate":
            pcal = fe.tx[chains[0]].power_cal if chains else None
            for n in chains:
                if static:
                    ok = pcal is not None and pcal.calibrated and pcal.covers(
                        n, fc_hz, p.tx_gain_min_db[n], p.max_tx_gain_db[n])
                else:
                    ok = preds[n].power_calibrated
                if not ok:
                    out.append(Issue("error", "NO_POWER_CAL", "TX %d: no measured power calibration at "
                                     "%.6f MHz over the gain range" % (n, fc_hz / 1e6),
                                     "[active_frontend] tx_power_cal_file"))
            if static:
                if not p.authorization:
                    out.append(Issue("error", "NO_AUTHORIZATION", "radiate requires an authorization "
                                     "reference", "[active_tx] authorization"))
                elif p.authorization_kind == "amateur" and not p.callsign:
                    out.append(Issue("error", "NO_AUTHORIZATION", "amateur authorization requires a callsign",
                                     "[active_tx] callsign"))
                if self.geometry is None:
                    out.append(Issue("error", "EIRP", "radiate requires a valid array geometry (element "
                                     "gains for the EIRP bound)", "[active_geometry]"))
            eirp = fe.eirp_upper_bound_dbm(peps)
            if eirp > p.max_eirp_dbm + 1e-9:
                out.append(Issue("error", "EIRP", "worst-case coherent EIRP %.2f dBm > max_eirp_dbm %.2f dBm"
                                 % (eirp, p.max_eirp_dbm), "[active_tx] max_eirp_dbm"))
            try:
                eirp_avg = fe.eirp_upper_bound_dbm(avgs)
                s_lim = areg.mpe_limit_band_w_m2(fc_hz - occ_hz / 2.0, fc_hz + occ_hz / 2.0)
                r = 0.0 if eirp_avg == afe.NEG_INF else math.sqrt(
                    afe._w(eirp_avg) * p.duty_max / (4.0 * math.pi * s_lim))
                if not p.mpe_exclusion_m > 0:
                    out.append(Issue("error", "MPE", "radiate requires mpe_exclusion_m > 0 (computed MPE "
                                     "distance %.2f m)" % r, "[active_tx] mpe_exclusion_m"))
                elif r > p.mpe_exclusion_m + 1e-9:
                    out.append(Issue("error", "MPE", "MPE distance %.2f m > mpe_exclusion_m %.2f m"
                                     % (r, p.mpe_exclusion_m), "[active_tx] mpe_exclusion_m"))
            except ValueError as exc:
                out.append(Issue("error", "MPE", str(exc), "[active_tx]"))
        order = _ARM_ORDER if static else _BURST_ORDER
        rank = {c: k for k, c in enumerate(order)}
        return sorted(out, key=lambda i: rank.get(i.code, len(order)))

    def _static_predictions(self, fc_hz):
        p = self.policy
        a = p.max_digital_amplitude
        preds = {}
        for n in range(p.n_tx):
            preds[n] = self.frontend.tx[n].predict(fc_hz, p.max_tx_gain_db[n], a, a, p.papr_margin_db,
                                                   p.pa_backoff_db)
        return preds

    def validate_static(self) -> list:
        """Configuration feasibility: schema, front end, frequency policy and the
        static power / protection / EIRP / MPE limits at the maximum settings for
        every allowed mode (spec 12.3 A7 / A12-A14). [] when TX is disabled."""
        p = self.policy
        if not p.enabled:
            return []
        issues = [i for i in acfg.validate_config(self.cfg) if i.level in ("error", "warning")]
        issues += self.frontend.validate(self.cfg)
        if self.frontend.n_tx != p.n_tx or len(p.max_tx_gain_db) != p.n_tx:
            issues.append(Issue("error", "SAFETY_STATIC", "front-end model has %d TX chains, config %d"
                                % (self.frontend.n_tx, p.n_tx)))
            return issues
        for n in range(p.n_tx):
            if p.max_tx_gain_db[n] > p.tx_gain_max_db[n] + 1e-9:
                issues.append(Issue("error", "GAIN", "TX %d: max_tx_gain_db %.2f > caps maximum %.2f"
                                    % (n, p.max_tx_gain_db[n], p.tx_gain_max_db[n]), "[active_tx] max_tx_gain_db"))
        if p.n_tx == 0:
            return issues
        occ = p.occupied_bw_hz()
        preds = self._static_predictions(p.fc_hz)
        for mode in p.allowed_modes:
            if mode not in TX_MODES:
                continue
            for i in self._freq_issues(mode, p.fc_hz, occ, p.fs_hz):
                issues.append(dataclasses.replace(i, where="%s [%s]" % (i.where, mode)))
            for i in self._power_issues(mode, p.fc_hz, occ, preds, list(range(p.n_tx)), "static", True):
                issues.append(dataclasses.replace(i, where="%s [%s]" % (i.where, mode)))
        seen = set()
        out = []
        for i in issues:
            k = (i.level, i.code, i.message, i.where)
            if k not in seen:
                seen.add(k)
                out.append(i)
        return out

    def required_confirmation(self, mode, fc_hz) -> str:
        return ack_phrase(mode, fc_hz)

    # --- arming ------------------------------------------------------------------------------
    def arm(self, mode, fc_hz, confirmation, operator="", confirmation_source="arg",
            occupied_bw_hz=None, duration_s=None, beamformed=False, flowgraph_sha256="",
            waveform_sha256="", max_bursts=0) -> ArmRecord:
        """Run the preflight A1..A15 and arm (spec 12.3). ``SafetyRefusal`` on the
        first failure; the state is unchanged and no RF is emitted."""
        with self._lock:
            gen0 = self._trip_gen
            p = self.policy
            checks = []
            self.last_arm_checks = checks
            fc = float(fc_hz)

            def ok(name):
                checks.append((name, "ok"))

            # A1
            if self.state == TxState.DISABLED:
                self._refuse("TX_DISABLED", "TX is disabled for this process (en_tx = 0 or mode = rx)")
            if self.state == TxState.FAULT:
                self._refuse("FAULT_LATCHED", "fault %s latched: reset with %r" % (
                    self.fault_code, reset_phrase(self.fault_code)))
            if self.state in (TxState.ARMED, TxState.TRANSMITTING):
                self._refuse("ALREADY_ARMED", "already armed (session %s)" % self.arm_record.session_id)
            ok("A1")
            # A2
            if mode not in TX_MODES or mode not in p.allowed_modes:
                self._refuse("MODE_NOT_ALLOWED", "mode %r not in allowed_modes %s" % (mode, list(p.allowed_modes)))
            ok("A2")
            # A3
            if mode == "radiate" and confirmation_source != "tty":
                self._refuse("CONFIRMATION_TTY_REQUIRED", "radiate must be confirmed by typing the phrase on "
                             "an interactive terminal (--confirm is rejected for radiate)")
            want = ack_phrase(mode, fc)
            if not isinstance(confirmation, str) or confirmation != want:
                self._refuse("CONFIRMATION", "confirmation does not match %r" % want)
            ok("A3")
            # A4
            op = (operator or p.operator_cfg or "").strip()
            if not op:
                self._refuse("NO_OPERATOR", "an operator name is required to arm")
            ok("A4")
            # A5
            sim = self.is_simulation or bool(getattr(self.backend, "is_simulation", False))
            if sim and p.backend != "sim":
                self._refuse("BACKEND", "simulation fallback for a %s configuration: TX is refused" % p.backend)
            ok("A5")
            # A6: the model the checks use must be the one the config / aux files describe
            why = self._model_current()
            if why is not None:
                self._refuse("CONFIG_CHANGED", why)
            p = self.policy
            static = self.validate_static()
            deferred_radiate = [i for i in static if i.level == "error" and i.code == "TX_RADIATE"
                                and mode == "radiate"]
            bad = [i for i in static if i.level == "error" and i.code not in _LATER_CODES
                   and i not in deferred_radiate]
            if bad:
                self._refuse("CONFIG_INVALID", "configuration invalid: %s" % "; ".join(str(i) for i in bad[:5]),
                             issues=ac.issues_to_json(bad))
            ok("A6")
            occ = float(occupied_bw_hz) if occupied_bw_hz else p.occupied_bw_hz()
            # A7
            if self._tx_freq is not None:
                try:
                    cur = float(self._tx_freq())
                except Exception as exc:
                    self._refuse("FREQ_CHANGED", "cannot read the TX frequency: %r" % (exc,))
                if abs(cur - fc) > FREQ_TOL_HZ:
                    self._refuse("FREQ_CHANGED", "arm frequency %.6f MHz != tuned TX %.6f MHz"
                                 % (fc / 1e6, cur / 1e6))
            fiss = self._freq_issues(mode, fc, occ, p.fs_hz)
            e = self._first_error(fiss)
            if e is not None:
                self._refuse(e.code, e.message)
            for w in fiss:
                logger.warning("TX arm: %s", w.message)
            ok("A7")
            # A8
            if p.require_ref_lock:
                st, why = self._ref_state()
                if st is not True:
                    self._refuse("REF_UNLOCK", "require_ref_lock = 1: %s" % (why or "not locked"))
            if beamformed or p.n_tx > 1:
                try:
                    synced = bool(self._sync_ok())
                except Exception:
                    synced = False
                if not synced:
                    self._refuse("NO_SYNC", "multi-chain / beamformed TX requires a successful sync")
                try:
                    cc = int(self._coherence_class())
                except Exception:
                    cc = 0
                if cc < 2:
                    self._refuse("COHERENCE_CLASS", "coherence class %d < SHARED_REF (2)" % cc)
            ok("A8")
            # A9
            il = self._interlock_state()
            if il is None and mode == "radiate":
                self._refuse("INTERLOCK_REQUIRED", "radiate requires a hardware interlock input")
            if il is False:
                self._refuse("INTERLOCK_OPEN", "the interlock loop is open")
            ok("A9")
            # A10
            if mode == "dummy_load":
                dl = self._dl_state()
                if dl is False:
                    self._refuse("DUMMY_LOAD", "the dummy-load sense reports no load")
                if any(not r > 0 for r in p.dummy_load_rating_w):
                    self._refuse("DUMMY_LOAD", "dummy_load_rating_w must be > 0 on every chain")
            ok("A10")
            # A11
            if mode == "radiate" and p.require_power_monitor_for_radiate and not self._monitor_configured():
                self._refuse("NO_POWER_MONITOR", "radiate requires a power monitor")
            tv = self._telemetry_violation()
            if tv is not None:
                self._refuse(tv[0], tv[1])
            ok("A11")
            # A12-A14
            preds = self._static_predictions(fc)
            piss = self._power_issues(mode, fc, occ, preds, list(range(p.n_tx)), "static", True)
            e = self._first_error(piss)
            if e is not None:
                self._refuse(e.code, e.message)
            ok("A12-A14")
            # A15
            if mode == "radiate" and beamformed:
                try:
                    cal_ok = bool(self._tx_cal_valid())
                except Exception:
                    cal_ok = False
                if not cal_ok:
                    self._refuse("CAL_REQUIRED", "beamformed radiate requires a valid TX calibration")
            ok("A15")
            if deferred_radiate:
                self._refuse("CONFIG_INVALID", "radiate configuration incomplete: %s"
                             % "; ".join(i.message for i in deferred_radiate))
            # success
            now = self._clock()
            wall = self._wallclock()
            dur = p.arm_timeout_s if not duration_s else min(float(duration_s), p.arm_timeout_s)
            if not dur > 0:
                self._refuse("ARM_TOTAL", "arm duration must be > 0")
            peps = [preds[n].out_pep_dbm for n in range(p.n_tx)]
            avgs = [preds[n].out_avg_dbm for n in range(p.n_tx)]
            eirp = self.frontend.eirp_upper_bound_dbm(peps) if p.n_tx else afe.NEG_INF
            try:
                mpe = areg.mpe_distance_m(self.frontend.eirp_upper_bound_dbm(avgs), fc, p.duty_max) \
                    if p.n_tx else 0.0
            except ValueError:
                mpe = None
            # the record certifies the model the checks above used (not the files as they are now)
            if self._fingerprint() != self._model_fp:
                self._refuse("CONFIG_CHANGED", "the configuration / an auxiliary table changed during arming")
            rec = ArmRecord(session_id=uuid.uuid4().hex, mode=mode, operator=op, fc_hz=fc, occupied_bw_hz=occ,
                            armed_unix=wall, expires_unix=wall + dur, fingerprint=self._model_fp,
                            flowgraph_sha256=str(flowgraph_sha256 or ""),
                            waveform_sha256=str(waveform_sha256 or ""), max_bursts=int(max_bursts or 0))
            self._check_not_interrupted(gen0, "arming")
            try:
                write_arm_record(self.state_dir, rec)
            except OSError as exc:
                self._refuse("CONFIG_INVALID", "cannot write the arm record in %s: %s" % (self.state_dir, exc))
            self.arm_record = rec
            self._mode = mode
            self._beamformed = bool(beamformed)
            self._armed_mono = now
            self._expiry_mono = now + dur
            self._last_hb = now
            self._fp_checked = now
            self._registry.clear()
            self.duty.release_all()
            self._underflows.clear()
            self._adopted = False
            self.state = TxState.ARMED
            if self._trip_gen != gen0:
                # a trip / shutdown (e.g. a signal handler) ran inside this commit
                self._undo_interrupted(rec.session_id, "arming")
            self._station_next = (now + p.station_id_interval_s) if p.authorization_kind == "amateur" else None
            banner = areg.regulatory_banner(mode, fc, p.authorization, p.authorization_kind, p.callsign, fiss,
                                            eirp if mode == "radiate" else None,
                                            p.max_eirp_dbm if mode == "radiate" else None,
                                            mpe if mode == "radiate" else None,
                                            p.mpe_exclusion_m if mode == "radiate" else None,
                                            p.station_id_interval_s, sim)
            self.last_banner = banner
            logger.warning("%s", banner)
            if fiss:
                logger.warning("TX arm: built-in deny override in conducted mode %s", mode)
            self._event(EVT_ACTIVE_TX_ARM, "warning", session=rec.session_id, mode=mode, operator=op,
                        fc_hz=fc, occupied_bw_hz=occ, authorization=p.authorization,
                        authorization_kind=p.authorization_kind, simulated=sim, beamformed=bool(beamformed),
                        expires_unix=rec.expires_unix, checks=checks, eirp_upper_dbm=eirp, mpe_m=mpe,
                        limits={"max_chain_power_dbm": list(p.max_chain_power_dbm),
                                "max_tx_gain_db": list(p.max_tx_gain_db),
                                "max_digital_amplitude": p.max_digital_amplitude})
            if p.authorization_kind == "amateur":
                self._event(EVT_ACTIVE_STATION_ID, "info", callsign=p.callsign, session=rec.session_id)
            if p.pa_hold_between_bursts and self.rf is not None and not self._atr():
                gen1 = self._trip_gen
                try:
                    self.rf.sequence_to_tx(rec)
                except Exception as exc:
                    self.trip("SEQUENCE", "PA hold sequencing failed: %r" % (exc,))
                    raise InterlockTrip("SEQUENCE", "PA hold sequencing failed: %r" % (exc,))
                if self._trip_gen != gen1:
                    # a trip ran while the PA was being sequenced on: re-assert the safe state
                    self.safe_shutdown("trip_during_sequencing", fault=self.state == TxState.FAULT)
                    self._raise_interrupted("arming")
            return rec

    # --- trip-generation guards (signal handlers run inside critical sections) ----------
    def _raise_interrupted(self, what):
        if self.state == TxState.FAULT:
            raise InterlockTrip(self.fault_code or "SIGNAL", "%s interrupted by a trip: %s"
                                % (what, self.fault_detail))
        self._refuse("SEQUENCE", "%s interrupted by a shutdown (signal / trip): nothing committed" % what)

    def _check_not_interrupted(self, gen0, what):
        """Raise when a trip / safe_shutdown ran since ``gen0`` (nothing committed yet)."""
        if self._trip_gen != gen0:
            self._raise_interrupted(what)

    def _undo_interrupted(self, session, what, entry=None):
        """A trip / shutdown ran between the last check and a state commit: restore the
        state it left (FAULT when a fault is latched, else SAFE), drop the committed
        burst / record, then raise."""
        if self.state in (TxState.ARMED, TxState.TRANSMITTING):
            self.state = TxState.FAULT if self.fault_code else TxState.SAFE
        if entry is not None and self._inflight is entry:
            self._inflight = None
        if self.state != TxState.FAULT:
            self.arm_record = None
            self._mode = None
        try:
            disk = read_arm_record(self.state_dir)
            if disk is not None and disk.session_id == session and not self._adopted:
                clear_arm_record(self.state_dir)
        except Exception:  # pragma: no cover
            pass
        self._raise_interrupted(what)

    def _adopt(self, rec: ArmRecord) -> None:
        """Adopt an arm record written by the arming process (flowgraph_guard only)."""
        if not self.policy.enabled:
            raise SafetyRefusal("TX_DISABLED", "TX is disabled in this configuration")
        if self._model_fp is None or self._model_fp != rec.fingerprint:
            raise SafetyRefusal("CONFIG_CHANGED", "the front-end model was built from a configuration / "
                                "auxiliary tables that differ from the arm record")
        p = self.policy
        now, wall = self._clock(), self._wallclock()
        span = float(rec.expires_unix) - float(rec.armed_unix)
        if not span > 0 or span > p.max_arm_total_s + 1e-6:
            raise SafetyRefusal("ARM_TOTAL", "the arm record spans %.1f s (max_arm_total_s %.0f s)"
                                % (span, p.max_arm_total_s))
        # wall-clock based, capped so that a backward wall-clock step cannot lengthen the window
        remaining = min(float(rec.expires_unix) - wall, p.arm_timeout_s,
                        float(rec.armed_unix) + p.max_arm_total_s - wall)
        if not remaining > 0:
            raise SafetyRefusal("ARM_EXPIRED", "the arm record expired")
        self.arm_record = rec
        self._mode = rec.mode
        self._armed_mono = now - max(0.0, wall - float(rec.armed_unix))
        self._expiry_mono = now + remaining
        self._last_hb = now
        self._fp_checked = now
        self._adopted = True
        self.state = TxState.ARMED

    def _adopted_record_problem(self):
        """Adopted controllers only: why the arming process's record no longer covers
        this session (missing / other session / changed / shortened), or None."""
        if not self._adopted or self.arm_record is None:
            return None
        own = self.arm_record
        disk = read_arm_record(self.state_dir)
        if disk is None:
            return "the arm record was removed (disarm, expiry or trip in the arming process)"
        if disk.session_id != own.session_id:
            return "the arm record belongs to another session"
        if disk.fingerprint != own.fingerprint or disk.mode != own.mode or abs(disk.fc_hz - own.fc_hz) > FREQ_TOL_HZ:
            return "the arm record changed"
        if float(disk.expires_unix) < float(own.expires_unix) - 1e-6:
            return "the arm record expiry was shortened"
        return None

    def refresh(self) -> ArmRecord:
        """Extend the arm expiry by ``arm_timeout_s``, capped at ``max_arm_total_s`` from
        the original arm (``ARM_TOTAL`` once the cap is reached)."""
        with self._lock:
            if self.state == TxState.DISABLED:
                self._refuse("TX_DISABLED", "TX is disabled")
            if self.state not in (TxState.ARMED, TxState.TRANSMITTING):
                self._refuse("NOT_ARMED", "not armed")
            now = self._clock()
            self._check_expiry_heartbeat(now)
            cap = self._armed_mono + self.policy.max_arm_total_s
            if now >= cap - 1e-9 or self._expiry_mono >= cap - 1e-9:
                self._refuse("ARM_TOTAL", "max_arm_total_s %.0f s reached: disarm and arm again"
                             % self.policy.max_arm_total_s)
            new = min(now + self.policy.arm_timeout_s, cap)
            self._expiry_mono = max(self._expiry_mono, new)
            rec = dataclasses.replace(self.arm_record,
                                      expires_unix=self._wallclock() + (self._expiry_mono - now))
            gen0 = self._trip_gen
            write_arm_record(self.state_dir, rec)
            if self._trip_gen != gen0 or self.state not in (TxState.ARMED, TxState.TRANSMITTING):
                # a trip / shutdown cleared the record meanwhile: never leave a refreshed one behind
                self._clear_own_record(rec.session_id)
                self._raise_interrupted("refresh")
            self.arm_record = rec
            self._last_hb = now
            return rec

    def heartbeat(self) -> None:
        with self._lock:
            self._last_hb = self._clock()

    def disarm(self, reason: str = "operator") -> None:
        """ARMED/TRANSMITTING -> SAFE through ``safe_shutdown``; never raises."""
        try:
            with self._lock:
                if self.state in (TxState.ARMED, TxState.TRANSMITTING):
                    sess = self.arm_record.session_id if self.arm_record else ""
                    self.safe_shutdown(reason)
                    self._event(EVT_ACTIVE_TX_DISARM, "info", reason=reason, session=sess)
                elif self.state == TxState.SAFE:
                    clear_arm_record(self.state_dir)
        except Exception as exc:  # pragma: no cover - safe_shutdown never raises
            logger.critical("disarm failed: %r", exc)

    def _check_expiry_heartbeat(self, now):
        """B1-style expiry / heartbeat evaluation; raises a refusal after the transition."""
        p = self.policy
        if self.state == TxState.ARMED:
            if now > self._expiry_mono:
                self._event(EVT_ACTIVE_TX_EXPIRE, "info", session=self.arm_record.session_id)
                self.disarm("expiry")
                self._refuse("ARM_EXPIRED", "the arm expired")
            if self._last_hb is not None and now - self._last_hb > p.heartbeat_timeout_s:
                self.disarm("watchdog")
                self._refuse("WATCHDOG", "heartbeat older than %.2f s" % p.heartbeat_timeout_s)

    # --- per-burst authorization ------------------------------------------------------------
    def _expected_fs(self) -> float:
        """The TX sample rate a burst must declare: the ``tx_rate`` provider (actual
        device rate) when given, else the configured ``sample_rate_hz``."""
        if self._tx_rate is not None:
            try:
                v = float(self._tx_rate())
            except Exception as exc:
                self._refuse("SHAPE", "cannot read the TX sample rate: %r" % (exc,))
            if not (math.isfinite(v) and v > 0):
                self._refuse("SHAPE", "the device reports a TX sample rate of %r" % (v,))
            return v
        return float(self.policy.fs_hz)

    def _read_tx_freq(self):
        """``(hz, "")`` from the ``tx_freq`` provider, ``(None, why)`` on failure."""
        try:
            v = float(self._tx_freq())
        except Exception as exc:
            return None, "cannot read the TX frequency: %r" % (exc,)
        if not math.isfinite(v):
            return None, "the TX frequency reads %r" % (v,)
        return v, ""

    def _retune_problem(self, reading):
        """Detail when the tuned TX frequency differs from the armed one, else None."""
        if reading is None or self.arm_record is None:
            return None
        val, why = reading
        if val is None:
            return why
        if abs(val - self.arm_record.fc_hz) > FREQ_TOL_HZ:
            return "TX retuned to %.6f MHz (armed %.6f MHz)" % (val / 1e6, self.arm_record.fc_hz / 1e6)
        return None

    def _b9_state(self):
        """``(lna_on, tr_state, lna_credit, tr_credit)`` for the per-burst RX protection.

        With an RF control attached the post-sequencing state is assumed (LNA off
        when ``lna_off_during_tx``, T/R in tx) and ``consume`` verifies that the
        front end is really there. Without one nothing can switch or verify the
        lines, so the worst case is used: LNA biased on, T/R in rx."""
        p = self.policy
        fe = self.frontend
        if self.rf is None:
            return True, ("rx" if fe.has_tr_switch else "tx"), False, False
        lna_on = not p.lna_off_during_tx
        return lna_on, "tx", (not lna_on) and bool(fe.lna_controllable), bool(fe.has_tr_switch)

    def _spectrum_problem(self, x, fs, fc_b, a_lo, a_hi, mode=None):
        """``(code, detail)`` when a chain's burst energy is not contained in ``[a_lo, a_hi]``
        (RF Hz), else None. Skipped when the band covers the whole baseband ``fc +/- fs/2``
        (the frequency policy then already covers every bin).

        Three tests per chain: the strongest bin lies in the band and at most
        ``OCC_OOB_MAX_FRACTION`` of the energy lies outside it (the declared band is
        honest: ``FREQ_CHANGED``), and at most ``FORBIDDEN_MAX_FRACTION`` (-50 dBc) lies
        on bins outside it that the frequency policy forbids, LO images included
        (``FREQ_NOT_ALLOWED`` / ``FREQ_DENIED``: the emission mask)."""
        lo, hi = a_lo - fc_b, a_hi - fc_b
        if lo <= -fs / 2.0 + FREQ_TOL_HZ and hi >= fs / 2.0 - FREQ_TOL_HZ:
            return None
        p = self.policy
        mode = mode or self._mode
        # resolution: at least SPEC_MIN_BINS_PER_BAND bins across the band (a burst shorter
        # than that is zero-padded: its gated spectrum, not a coarse bin straddling the edge)
        n = max(int(x.shape[1]), min(SPEC_MAX_PAD, int(math.ceil(
            SPEC_MIN_BINS_PER_BAND * fs / max(hi - lo, FREQ_TOL_HZ)))))
        f = np.fft.fftfreq(n, 1.0 / fs)
        bin_hz = fs / n
        half_bin = bin_hz / 2.0
        inband = (f >= lo - half_bin) & (f <= hi + half_bin)
        forb = areg.forbidden_mask(fc_b + f, bin_hz, mode, p.allow_bands, p.deny_bands,
                                   p.conducted_ignore_builtin_deny, p.lo_offset_hz, fc_b) & ~inband
        for k in range(x.shape[0]):                     # one chain at a time (memory)
            spec = np.abs(np.fft.fft(np.asarray(x[k], dtype=np.complex128), n)) ** 2
            tot = float(spec.sum())
            if not tot > 0:
                continue
            pk = int(np.argmax(spec))
            if not inband[pk]:
                return "FREQ_CHANGED", ("burst chain %d: spectral peak at %.6f MHz is outside the armed band "
                                        "%.6f..%.6f MHz (occupied_bw_hz does not describe this waveform)"
                                        % (k, (fc_b + f[pk]) / 1e6, a_lo / 1e6, a_hi / 1e6))
            if forb.any():
                fe = float(spec[forb].sum()) / tot
                if fe > FORBIDDEN_MAX_FRACTION:
                    j = int(np.flatnonzero(forb)[np.argmax(spec[forb])])
                    fj = fc_b + f[j]
                    iv = [(fj - half_bin, fj + half_bin, "emission")]
                    if p.lo_offset_hz:
                        for lo_c in (fc_b - p.lo_offset_hz, fc_b + p.lo_offset_hz):
                            iv.append((2 * lo_c - fj - half_bin, 2 * lo_c - fj + half_bin, "emission image"))
                    errs = [i for i in areg.band_check(iv, mode, p.allow_bands, p.deny_bands,
                                                       ignore_builtin=p.conducted_ignore_builtin_deny)
                            if i.level == "error"]
                    errs.sort(key=lambda i: i.code != "FREQ_DENIED")      # a denial is the stronger code
                    code = errs[0].code if errs else "FREQ_DENIED"
                    return code, ("burst chain %d: %.1f dBc of the energy falls where transmitting is not "
                                  "allowed (limit %.0f dBc); strongest at %.6f MHz (%.1f dBc): %s"
                                  % (k, 10 * math.log10(fe), 10 * math.log10(FORBIDDEN_MAX_FRACTION), fj / 1e6,
                                     10 * math.log10(max(float(spec[j]) / tot, 1e-300)),
                                     errs[0].message if errs else "forbidden by the frequency policy"))
            oob = float(spec[~inband].sum()) / tot
            if oob > OCC_OOB_MAX_FRACTION:
                return "FREQ_CHANGED", ("burst chain %d: %.2f %% of the energy lies outside the armed band "
                                        "%.6f..%.6f MHz (limit %.2f %%: widen occupied_bw_hz or shape the burst)"
                                        % (k, 100.0 * oob, a_lo / 1e6, a_hi / 1e6, 100.0 * OCC_OOB_MAX_FRACTION))
        return None

    def authorize_burst(self, samples, chains, tx_gains_db, fs_hz, start=None, weights=None,
                        purpose="operate", fc_hz=None, timeout_s=1.0, at_offset_s=0.0) -> TxPermit:
        """Run B1..B12 on the FINAL buffer and issue a single-use permit (spec 12.4).

        ``samples`` (K, N) are never modified or clipped; the permit carries a
        read-only complex64 copy. The tuned TX frequency (``tx_freq`` provider) is
        authoritative; ``fc_hz`` is only a cross-check against it (without a
        provider it is the burst centre, default the configured one). ``fs_hz``
        must equal the device / configured TX rate. ``at_offset_s`` places an
        untimed burst at ``now + offset`` in the duty window; it is accepted only
        for ``purpose="flowgraph"`` on an adopted (flowgraph_guard) controller
        (``SHAPE`` otherwise). The burst's duty is reserved at its real start
        until it is consumed, revoked or expires.
        """
        with self._lock:
            gen0 = self._trip_gen
            p = self.policy
            now = self._clock()
            self._expire_permits(now)
            # B1
            if self.state == TxState.DISABLED:
                self._refuse("TX_DISABLED", "TX is disabled")
            if self.state == TxState.FAULT:
                self._refuse("FAULT_LATCHED", "fault %s latched" % self.fault_code)
            if self.state not in (TxState.ARMED, TxState.TRANSMITTING):
                self._refuse("NOT_ARMED", "not armed")
            if now > self._expiry_mono:
                if self.state == TxState.ARMED:
                    self._event(EVT_ACTIVE_TX_EXPIRE, "info", session=self.arm_record.session_id)
                    self.disarm("expiry")
                self._refuse("ARM_EXPIRED", "the arm expired")
            if self._last_hb is None or now - self._last_hb > p.heartbeat_timeout_s:
                if self.state == TxState.ARMED:
                    self.disarm("watchdog")
                self._refuse("WATCHDOG", "heartbeat older than %.2f s" % p.heartbeat_timeout_s)
            why = self._adopted_record_problem()
            if why is not None:
                self._revoke_adopted(why)
                self._refuse("NOT_ARMED", why)
            fp = self._fingerprint()
            if fp != self.arm_record.fingerprint:
                if self.state == TxState.ARMED:
                    self.disarm("config_changed")
                self._refuse("CONFIG_CHANGED", "the configuration (or an auxiliary table) changed since arming")
            # B2
            try:
                chains = tuple(int(c) for c in chains)
            except (TypeError, ValueError):
                self._refuse("SHAPE", "chains must be a sequence of global TX indices")
            if not chains or len(set(chains)) != len(chains) or any(not 0 <= c < p.n_tx for c in chains):
                self._refuse("SHAPE", "chains %s must be unique configured TX indices (0..%d)" % (
                    list(chains), p.n_tx - 1))
            x = np.asarray(samples)
            if x.dtype.kind not in "iufc" or x.ndim != 2 or x.shape[0] != len(chains) or x.shape[1] < 1:
                self._refuse("SHAPE", "samples must be a numeric (K=%d, N>=1) array, got %s %s"
                             % (len(chains), x.dtype, x.shape))
            try:
                fs = float(fs_hz)
            except (TypeError, ValueError):
                fs = float("nan")
            if not (math.isfinite(fs) and fs > 0):
                self._refuse("SHAPE", "fs_hz must be finite and > 0")
            want_fs = self._expected_fs()
            if abs(fs - want_fs) > FS_REL_TOL * want_fs:
                self._refuse("SHAPE", "fs_hz %.6f S/s != the TX sample rate %.6f S/s (the burst duration, duty "
                             "and deadline are computed from it)" % (fs, want_fs))
            try:
                tmo = float(timeout_s)
            except (TypeError, ValueError):
                tmo = float("nan")
            if not (math.isfinite(tmo) and tmo >= 0):
                self._refuse("SHAPE", "timeout_s must be finite and >= 0")
            tmo = min(tmo, p.heartbeat_timeout_s)
            try:
                at_off = float(at_offset_s or 0.0)
            except (TypeError, ValueError):
                at_off = float("nan")
            if not math.isfinite(at_off):
                self._refuse("SHAPE", "at_offset_s must be finite")
            at_off = max(0.0, at_off)
            flowgraph = str(purpose) == "flowgraph" and self._adopted
            if at_off > 0 and not flowgraph:
                # an untimed send starts at consume(): a declared offset would place its duty in a
                # window it does not occupy. Timed bursts use ``start``.
                self._refuse("SHAPE", "at_offset_s is accepted only for flowgraph bursts of an adopted "
                             "(flowgraph_guard) controller; use a timed start")
            x128 = np.array(x, dtype=np.complex128, copy=True)
            if not np.all(np.isfinite(x128)):
                self._refuse("NONFINITE", "samples contain NaN or inf")
            frozen = x128.astype(np.complex64)
            # B3 (complex magnitude bounds |I| and |Q|): exact on complex128, and the frozen
            # complex64 copy (what is sent) within float32 rounding (~1e-7 relative)
            pk128 = np.max(np.abs(x128), axis=1)
            f64 = frozen.astype(np.complex128)
            pk64 = np.max(np.abs(f64), axis=1)
            for k, c in enumerate(chains):
                pk = (pk128[k], pk64[k])
                if (pk128[k] > p.max_digital_amplitude * (1.0 + 1e-12)
                        or pk64[k] > p.max_digital_amplitude * (1.0 + F32_REL_TOL)):
                    self._refuse("AMPLITUDE", "chain %d peak |s| %.9f > max_digital_amplitude %.6f (never "
                                 "clipped: scale the waveform)" % (c, max(pk), p.max_digital_amplitude))
            # B3 (analog): the reconstructed envelope (inter-sample peaks, burst edges) must not
            # clip the DUC / DAC; its peak drives every PEP-based limit below
            isp = reconstructed_peak(f64)
            for k, c in enumerate(chains):
                if isp[k] > DAC_FULL_SCALE:
                    self._refuse("AMPLITUDE", "chain %d reconstructed (inter-sample) envelope peak %.4f > DAC "
                                 "full scale %.1f (sample peak %.4f): reduce the amplitude or band-limit / ramp "
                                 "the waveform" % (c, isp[k], DAC_FULL_SCALE, pk64[k]))
            # B4
            try:
                gains = tuple(float(g) for g in (tx_gains_db if np.ndim(tx_gains_db) else
                                                 [tx_gains_db] * len(chains)))
            except (TypeError, ValueError):
                self._refuse("GAIN", "tx_gains_db must be numeric")
            if len(gains) != len(chains):
                self._refuse("GAIN", "tx_gains_db has %d entries for %d chains" % (len(gains), len(chains)))
            for g, c in zip(gains, chains):
                if not math.isfinite(g):
                    self._refuse("GAIN", "chain %d gain is not finite" % c)
                if g > p.max_tx_gain_db[c] + 1e-9:
                    self._refuse("GAIN", "chain %d gain %.2f dB > max_tx_gain_db %.2f dB" % (c, g, p.max_tx_gain_db[c]))
                if g < p.tx_gain_min_db[c] - 1e-9 or g > p.tx_gain_max_db[c] + 1e-9:
                    self._refuse("GAIN", "chain %d gain %.2f dB outside caps %.2f..%.2f dB"
                                 % (c, g, p.tx_gain_min_db[c], p.tx_gain_max_db[c]))
            # B5
            n_samp = int(x.shape[1])
            dur = n_samp / fs
            if dur > p.max_burst_s * (1.0 + 1e-12):
                self._refuse("BURST_LENGTH", "burst %.6f s > max_burst_s %.6f s" % (dur, p.max_burst_s))
            # B11 (the start is needed by the duty check)
            lead = 0.0
            if start is not None:
                if not isinstance(start, DevTime):
                    self._refuse("START_TIME", "start must be a DevTime")
                if self._time_now is None:
                    self._refuse("START_TIME", "no device clock to verify the start time")
                try:
                    lead = float(start.diff_seconds(self._time_now()))
                except Exception as exc:
                    self._refuse("START_TIME", "cannot read the device time: %r" % (exc,))
                min_lead = max(p.min_start_lead_s, p.min_tx_lead_s)
                if not math.isfinite(lead) or lead < min_lead - 1e-12:
                    self._refuse("START_TIME", "start only %.6f s ahead (need >= %.6f s)" % (lead, min_lead))
                if now + lead + dur > self._expiry_mono:
                    self._refuse("START_TIME", "the burst would end after the arm expiry")
            else:
                sim = self.is_simulation or bool(getattr(self.backend, "is_simulation", False))
                if not sim and not flowgraph and (self._time_now is not None or self.backend is not None):
                    self._refuse("START_TIME", "hardware bursts must be timed (start required)")
                if now + at_off + dur > self._expiry_mono:
                    self._refuse("START_TIME", "the burst would end after the arm expiry")
            # B6: at the burst's real start, counting every outstanding permit
            duty_at = now + (lead if start is not None else at_off)
            if self.duty.would_exceed(dur, duty_at):
                self._refuse("DUTY", "a %.6f s burst would exceed duty_max %.3g over %.3g s (current %.4f, "
                             "%d permit(s) outstanding)" % (dur, p.duty_max, p.duty_window_s, self.duty.duty(now),
                                                             self.duty.reserved()))
            # B12 inputs (needed by B7..B9): the tuned frequency is authoritative
            if fc_hz is not None:
                try:
                    fc_arg = float(fc_hz)
                except (TypeError, ValueError):
                    fc_arg = float("nan")
                if not math.isfinite(fc_arg):
                    self._refuse("FREQ_CHANGED", "fc_hz must be finite")
            if self._tx_freq is not None:
                cur, why = self._read_tx_freq()
                if cur is None:
                    self._refuse("FREQ_CHANGED", why)
                if fc_hz is not None and abs(fc_arg - cur) > FREQ_TOL_HZ:
                    self._refuse("FREQ_CHANGED", "burst fc %.6f MHz != tuned TX %.6f MHz"
                                 % (fc_arg / 1e6, cur / 1e6))
                fc_b = cur
            elif fc_hz is not None:
                fc_b = fc_arg
            else:
                fc_b = p.fc_hz
            if abs(fc_b - self.arm_record.fc_hz) > FREQ_TOL_HZ:          # B12 (before the power model)
                self._refuse("FREQ_CHANGED", "TX frequency %.6f MHz != armed %.6f MHz"
                             % (fc_b / 1e6, self.arm_record.fc_hz / 1e6))
            occ_b = p.occupied_bw_hz(fs)
            # B7..B9 on the final buffer (PEP from the reconstructed envelope peak)
            rms = adsp.rms(f64, axis=1)
            peak = np.maximum(adsp.peak(f64, axis=1), isp)
            preds = {}
            for k, c in enumerate(chains):
                preds[c] = self.frontend.tx[c].predict(fc_b, gains[k], float(rms[k]), float(peak[k]),
                                                       p.papr_margin_db, p.pa_backoff_db)
            mode = self._mode
            lna_on, tr_state, lna_credit, tr_credit = self._b9_state()
            e = self._first_error(self._power_issues(mode, fc_b, occ_b, preds, list(chains), purpose, False,
                                                     lna_on=lna_on, tr_state=tr_state))
            if e is not None:
                self._refuse(e.code, e.message)
            # B10 on the actual samples (whatever the declared weights say): b(t) = S a(t)
            if self.frontend.s_matrix is not None:
                gmax = gamma_from_vswr(p.max_vswr)
                scale = []
                for k, c in enumerate(chains):
                    pep_w = afe._w(preds[c].out_pep_dbm) if preds[c].out_pep_dbm != afe.NEG_INF else 0.0
                    scale.append(math.sqrt(pep_w) / float(peak[k]) if peak[k] > 0 else 0.0)
                # the waves are sampled at the sample instants: scale the reverse peak by the
                # largest inter-sample excess of the burst (conservative)
                exc = max([float(isp[k] / pk64[k]) for k in range(len(chains)) if pk64[k] > 0] or [1.0])
                rev_s, gam_s = self.frontend.active_waves_from_samples(f64, chains, scale)
                for n in range(p.n_tx):
                    rv = float(rev_s[n]) + 20.0 * math.log10(exc)
                    if p.max_reverse_power_dbm is not None and rv > p.max_reverse_power_dbm + 1e-9:
                        self._refuse("ACTIVE_REFLECTION", "chain %d predicted active reverse power %.2f dBm > "
                                     "%.2f dBm (from the burst samples)" % (n, rv, p.max_reverse_power_dbm))
                for c in chains:
                    if math.isfinite(gam_s[c]) and gam_s[c] > gmax + 1e-9:
                        self._refuse("ACTIVE_REFLECTION", "chain %d active reflection |G| %.3f > %.3f (VSWR %.2f), "
                                     "from the burst samples" % (c, gam_s[c], gmax, p.max_vswr))
            # B10 on the declared weights (an additional, never a replacing, check)
            if weights is not None and self.frontend.s_matrix is not None:
                w = np.asarray(weights, dtype=complex).reshape(-1)
                t = np.zeros(p.n_tx, dtype=complex)
                if w.size == len(chains):
                    for k, c in enumerate(chains):
                        t[c] = w[k]
                elif w.size == p.n_tx:
                    t[list(chains)] = w[list(chains)]
                else:
                    self._refuse("SHAPE", "weights have %d entries (expected %d or %d)" % (w.size, len(chains), p.n_tx))
                peps = [preds[c].out_pep_dbm if c in preds else None for c in range(p.n_tx)]
                rev = self.frontend.active_reverse_dbm(t, peps)
                gam = self.frontend.active_reflection(t)
                gmax = gamma_from_vswr(p.max_vswr)
                for c in chains:
                    if p.max_reverse_power_dbm is not None and rev[c] > p.max_reverse_power_dbm + 1e-9:
                        self._refuse("ACTIVE_REFLECTION", "chain %d predicted active reverse power %.2f dBm > "
                                     "%.2f dBm" % (c, rev[c], p.max_reverse_power_dbm))
                    if abs(t[c]) > 0 and gam[c] > gmax + 1e-12:
                        self._refuse("ACTIVE_REFLECTION", "chain %d active reflection |G| %.3f > %.3f (VSWR %.2f)"
                                     % (c, gam[c], gmax, p.max_vswr))
            # radiate with several chains is beamformed whatever the weights argument says
            if mode == "radiate" and (weights is not None or len(chains) > 1):
                try:
                    cal_ok = bool(self._tx_cal_valid())
                except Exception:
                    cal_ok = False
                if not cal_ok:
                    self._refuse("CAL_REQUIRED", "beamformed / multi-chain radiate burst without a valid TX "
                                 "calibration")
            # B12: the occupied band inside the armed band
            a_lo = self.arm_record.fc_hz - self.arm_record.occupied_bw_hz / 2.0
            a_hi = self.arm_record.fc_hz + self.arm_record.occupied_bw_hz / 2.0
            if fc_b - occ_b / 2.0 < a_lo - FREQ_TOL_HZ or fc_b + occ_b / 2.0 > a_hi + FREQ_TOL_HZ:
                self._refuse("FREQ_CHANGED", "occupied band %.6f..%.6f MHz outside the armed band %.6f..%.6f MHz"
                             % ((fc_b - occ_b / 2) / 1e6, (fc_b + occ_b / 2) / 1e6, a_lo / 1e6, a_hi / 1e6))
            # B12: the declared band must describe the actual samples
            why = self._spectrum_problem(frozen, fs, fc_b, max(a_lo, fc_b - occ_b / 2.0),
                                         min(a_hi, fc_b + occ_b / 2.0), mode)
            if why is not None:
                self._refuse(why[0], why[1])
            # issue (never across a trip / shutdown that ran during the checks)
            if self._trip_gen != gen0 or self.state not in (TxState.ARMED, TxState.TRANSMITTING):
                self._raise_interrupted("authorize_burst")
            frozen.flags.writeable = False
            deadline = now + lead + max(1.0, 2.0 * dur + tmo)
            permit = TxPermit(permit_id=uuid.uuid4().hex, session_id=self.arm_record.session_id, samples=frozen,
                              chains=chains, fc_hz=fc_b, fs_hz=fs, tx_gains_db=gains, start=start,
                              duration_s=dur, digest=ac.sha256_bytes(frozen.tobytes()), issued_mono=now,
                              deadline_mono=deadline, purpose=str(purpose),
                              prediction=tuple(preds[c] for c in chains))
            self._registry[permit.permit_id] = _Entry(permit, duty_at, at_off, lna_credit, tr_credit)
            self.duty.reserve(permit.permit_id, duty_at, dur)
            if self._trip_gen != gen0:
                self._drop_permit(permit.permit_id, "interrupted")
                self._raise_interrupted("authorize_burst")
            return permit

    # --- consume / complete ---------------------------------------------------------------------
    def _trip_raise(self, code, detail):
        self.trip(code, detail)
        raise InterlockTrip(code, detail)

    def _drop_permit(self, pid, reason) -> None:
        """Remove an issued permit: registry, duty reservation; remembered as revoked."""
        self._registry.pop(pid, None)
        self.duty.release(pid)
        self._revoke(pid, reason)

    def _expire_permits(self, now) -> None:
        """Revoke issued (not consumed) permits past their deadline."""
        for pid, e in list(self._registry.items()):
            if not e.consumed and now > e.permit.deadline_mono:
                self._drop_permit(pid, "expired")

    def _live_issued(self, now) -> bool:
        return any(not e.consumed and now <= e.permit.deadline_mono for e in self._registry.values())

    def _pa_idle_check(self, now, unknown_on=False) -> None:
        """PA enabled while nothing may transmit -> ``rf.sequence_to_rx`` (never raises).

        The PA may be on only for a burst in flight, an issued unexpired permit
        (pre-sequenced by the runner) or the armed hold session; anything else
        amplifies the idle DAC's LO leakage. ``unknown_on``: treat an RF control
        that cannot report its PA state as on."""
        rf = self.rf
        if rf is None or self._atr():
            return
        armed = self.state in (TxState.ARMED, TxState.TRANSMITTING)
        if armed and (self._inflight is not None or self.policy.pa_hold_between_bursts):
            return
        fn = getattr(rf, "pa_any_on", None)
        if callable(fn):
            try:
                on = bool(fn())
            except Exception:
                on = True
        else:
            on = bool(unknown_on)
        if not on:
            return
        if armed and self._live_issued(now):
            return
        logger.error("TX safety: PA enabled with no burst in flight or live permit: sequencing to RX")
        self._event(EVT_ACTIVE_TX_REFUSED, "warning", code="SEQUENCE", detail="PA on while idle: forced to RX",
                    state=self.state.value, session=getattr(self.arm_record, "session_id", ""))
        try:
            rf.sequence_to_rx()
        except Exception as exc:  # pragma: no cover - sequence_to_rx never raises
            logger.critical("sequence_to_rx failed: %r", exc)

    def _refuse_consume(self, pid, code, detail, drop=True):
        """A consume refusal: drop the permit and bring an idle PA back to RX, then refuse."""
        if drop and pid is not None:
            self._drop_permit(pid, "refused:%s" % code)
        self._pa_idle_check(self._clock(), unknown_on=True)
        self._refuse(code, detail)

    def _frontend_state_problem(self, entry):
        """Detail when the RF front end is not in the state B9 assumed, else None."""
        rf = self.rf
        if rf is None or self._atr() or not (entry.lna_credit or entry.tr_credit):
            return None
        st = getattr(rf, "state", None)
        if not isinstance(st, dict):
            return "the RF control cannot report its line states (B9 assumed LNA off / T/R tx)"
        if entry.lna_credit and st.get("lna") is not False:
            return "RX protection assumed the LNA bias off but it is on (run rf.sequence_to_tx first)"
        if entry.tr_credit and st.get("tr") != "tx":
            return "RX protection assumed the T/R switch in tx but it is in %r (run rf.sequence_to_tx first)" % (
                st.get("tr"),)
        return None

    def consume(self, permit) -> None:
        """Called by the backend immediately before the first send (spec 12.4)."""
        with self._lock:
            gen0 = self._trip_gen
            now = self._clock()
            if self.state == TxState.DISABLED:
                self._refuse("TX_DISABLED", "TX is disabled")
            pid = getattr(permit, "permit_id", None)
            entry = self._registry.get(pid) if isinstance(pid, str) else None
            if entry is None or entry.permit is not permit:
                if entry is None and isinstance(pid, str) and pid in self._completed_set:
                    self._trip_raise("PERMIT_REUSED", "permit %s was already used" % pid)
                if entry is None and isinstance(pid, str) and pid in self._revoked:
                    why = self._revoked[pid]
                    self._refuse_consume(None, "PERMIT_EXPIRED" if why == "expired" else "NOT_ARMED",
                                         "permit revoked (%s)" % why)
                self._trip_raise("PERMIT_FORGED", "permit %r is not a registered permit" % (pid,))
            if entry.consumed:
                self._trip_raise("PERMIT_REUSED", "permit %s was already consumed" % pid)
            sess = self.arm_record.session_id if self.arm_record else None
            if permit.session_id != entry.session or permit.session_id != sess:
                self._trip_raise("PERMIT_FORGED", "permit %s belongs to another session" % pid)
            if (permit.samples is not entry.samples or permit.digest != entry.digest
                    or ac.sha256_bytes(np.ascontiguousarray(permit.samples).tobytes()) != entry.digest):
                self._trip_raise("DIGEST", "permit %s samples changed after authorization" % pid)
            if now > permit.deadline_mono:
                self._refuse_consume(pid, "PERMIT_EXPIRED", "permit %s passed its deadline" % pid)
            if self.state == TxState.TRANSMITTING:
                # the PA belongs to the burst in flight: leave it; the permit stays usable
                self._refuse("SEQUENCE", "another burst is in flight")
            if self.state != TxState.ARMED:
                self._refuse_consume(pid, "NOT_ARMED", "not armed")
            # the tuned frequency, re-read right before the send (retune -> SAFE)
            if self._tx_freq is not None:
                why = self._retune_problem(self._read_tx_freq())
                if why is not None:
                    self.disarm("retune")
                    self._refuse_consume(pid, "FREQ_CHANGED", why)
            self.poll(now)
            if self.state == TxState.FAULT:
                raise InterlockTrip(self.fault_code, self.fault_detail)
            if self.state != TxState.ARMED or self._registry.get(pid) is not entry:
                self._refuse_consume(pid, "NOT_ARMED", "disarmed by the watchdog")
            # duty at the real start (untimed bursts start now), outstanding permits counted
            start_mono = entry.duty_at if permit.start is not None else now + entry.at_offset
            if self.duty.would_exceed(permit.duration_s, start_mono, exclude=pid):
                self._refuse_consume(pid, "DUTY", "a %.6f s burst would exceed duty_max %.3g over %.3g s"
                                     % (permit.duration_s, self.policy.duty_max, self.policy.duty_window_s))
            why = self._frontend_state_problem(entry)
            if why is not None:
                self._refuse_consume(pid, "SEQUENCE", why)
            # last step before the commit: a trip / shutdown since entry (signal handler, a
            # trip from another thread) wins; never write TRANSMITTING over it
            if self._trip_gen != gen0 or self.state != TxState.ARMED or self._registry.get(pid) is not entry:
                self._registry.pop(pid, None)
                self.duty.release(pid)
                self._revoke(pid, "interrupted")
                self._raise_interrupted("consume")
            entry.consumed = True
            entry.duty_at = start_mono
            self._inflight = entry
            self.state = TxState.TRANSMITTING
            if self._trip_gen != gen0:
                self._registry.pop(pid, None)
                self.duty.release(pid)
                self._revoke(pid, "interrupted")
                self._undo_interrupted(sess, "consume", entry)
            self.duty.record(start_mono, permit.duration_s, key=pid)
            self._event(EVT_ACTIVE_TX_BURST, "info", phase="start", permit=pid, session=sess,
                        chains=list(permit.chains), duration_s=permit.duration_s, purpose=permit.purpose,
                        out_pep_dbm=[pr.out_pep_dbm for pr in permit.prediction])

    def permit_active(self, permit) -> bool:
        """True for a registered, unexpired, not yet completed permit of the current
        session (issued or in flight), or for the current ``ArmRecord`` when the PA
        is held between bursts (``pa_hold_between_bursts = 1``)."""
        with self._lock:
            if self.state not in (TxState.ARMED, TxState.TRANSMITTING) or self.arm_record is None:
                return False
            if isinstance(permit, ArmRecord):
                return permit is self.arm_record and self.policy.pa_hold_between_bursts
            pid = getattr(permit, "permit_id", None)
            entry = self._registry.get(pid) if isinstance(pid, str) else None
            if entry is None or entry.permit is not permit or entry.completed:
                return False
            if entry.session != self.arm_record.session_id:
                return False
            return self._clock() <= permit.deadline_mono

    def handle_async_events(self, events) -> None:
        """Map backend async events onto trips / counters (ref/LO unlock trip while armed)."""
        with self._lock:
            now = self._clock()
            for ev in events or ():
                kind = getattr(ev, "kind", "")
                if kind in ("underflow", "underflow_in_packet", "late", "seq_error"):
                    if kind != "seq_error":
                        self._underflows.append(now)
                elif kind == "ref_unlock" and self.state in (TxState.ARMED, TxState.TRANSMITTING):
                    self.trip("REF_UNLOCK", getattr(ev, "detail", "") or "reference unlocked")
                elif kind == "lo_unlock" and self.state in (TxState.ARMED, TxState.TRANSMITTING):
                    self.trip("LO_UNLOCK", getattr(ev, "detail", "") or "LO unlocked")
            self._check_underflows(now)

    def _remember_completed(self, pid):
        if len(self._completed) == self._completed.maxlen:
            self._completed_set.discard(self._completed[0])
        self._completed.append(pid)
        self._completed_set.add(pid)

    def _revoke(self, pid, reason):
        self._revoked[pid] = reason
        if len(self._revoked) > 4096:
            for k in list(self._revoked)[:1024]:
                self._revoked.pop(k, None)

    def _check_underflows(self, now):
        p = self.policy
        while self._underflows and self._underflows[0] < now - p.underflow_window_s:
            self._underflows.popleft()
        if len(self._underflows) >= p.underflow_trip_count and self.state in (TxState.ARMED, TxState.TRANSMITTING):
            n = len(self._underflows)
            self._underflows.clear()
            self.trip("UNDERFLOW_STORM", "%d underflow/late events within %.3g s" % (n, p.underflow_window_s))

    def burst_complete(self, permit, result: TxResult) -> None:
        """Account the burst outcome; return to ARMED unless a trip occurred."""
        with self._lock:
            gen0 = self._trip_gen
            now = self._clock()
            pid = getattr(permit, "permit_id", None)
            entry = self._registry.get(pid) if isinstance(pid, str) else None
            if entry is None or entry.permit is not permit:
                logger.warning("burst_complete for an unknown/revoked permit %r (state %s)", pid, self.state.value)
                return
            if not entry.consumed:
                # never sent: revoke it; the state and the burst in flight (if any) are untouched
                logger.warning("burst_complete for the unconsumed permit %s: revoked", pid)
                self._drop_permit(pid, "completed_unconsumed")
                self._pa_idle_check(now, unknown_on=True)
                return
            if entry is not self._inflight:
                logger.error("burst_complete for the consumed permit %s which is not in flight: ignored", pid)
                self._registry.pop(pid, None)
                entry.completed = True
                self._remember_completed(pid)
                return
            self._registry.pop(pid, None)
            entry.completed = True
            self._remember_completed(pid)
            self._inflight = None
            n_bad = 0
            if result is not None:
                n_bad = int(getattr(result, "underflows", 0) or 0) + int(getattr(result, "late", 0) or 0)
                if getattr(result, "content_corrupt", False) and n_bad == 0:
                    n_bad = 1
            for _ in range(n_bad):
                self._underflows.append(now)
            self._event(EVT_ACTIVE_TX_BURST, "info", phase="done", permit=pid,
                        ok=bool(getattr(result, "ok", False)), sent=int(getattr(result, "sent_samples", 0) or 0),
                        underflows=int(getattr(result, "underflows", 0) or 0), late=int(getattr(result, "late", 0) or 0))
            if result is not None and getattr(result, "events", None):
                self.handle_async_events(result.events)
            self._check_underflows(now)
            if self.state == TxState.TRANSMITTING and self._trip_gen == gen0:
                self.state = TxState.ARMED
                if self._trip_gen != gen0 and self.state == TxState.ARMED:
                    self.state = TxState.FAULT if self.fault_code else TxState.SAFE
            if self.state == TxState.ARMED:
                if not self.policy.pa_hold_between_bursts and self.rf is not None and not self._atr():
                    try:
                        self.rf.sequence_to_rx()
                    except Exception as exc:  # pragma: no cover - sequence_to_rx never raises
                        logger.error("sequence_to_rx failed: %r", exc)
                if now > self._expiry_mono:
                    self._event(EVT_ACTIVE_TX_EXPIRE, "info", session=self.arm_record.session_id)
                    self.disarm("expiry")

    # --- watchdog -------------------------------------------------------------------------------
    def _gather_inputs(self) -> dict:
        """Read the external safety inputs (sensor / backend I/O) for ``poll``. Runs
        OUTSIDE the controller lock so that a hanging sensor cannot block a trip."""
        p = self.policy
        return {
            "il": self._interlock_state(),
            "dl": self._dl_state() if self._mode == "dummy_load" else None,
            "tv": self._telemetry_violation(),
            "ref": self._ref_state() if p.require_ref_lock else (None, ""),
            "freq": self._read_tx_freq() if self._tx_freq is not None else None,
            "adopt": self._adopted_record_problem() if self._adopted else None,
        }

    def _revoke_adopted(self, why) -> None:
        """The arming process withdrew the record this (flowgraph) controller adopted."""
        sess = self.arm_record.session_id if self.arm_record else ""
        logger.critical("TX safety: %s: stopping", why)
        self.safe_shutdown("arm_record_revoked")
        self._event(EVT_ACTIVE_TX_DISARM, "warning", reason="arm_record_revoked", detail=why, session=sess)

    def poll(self, now=None) -> None:
        """Deterministic watchdog / telemetry / interlock evaluation (spec 12.5)."""
        if self.state not in (TxState.ARMED, TxState.TRANSMITTING):
            with self._lock:
                self._pa_idle_check(float(self._clock() if now is None else now))
            return
        inp = self._gather_inputs()
        with self._lock:
            now = float(self._clock() if now is None else now)
            p = self.policy
            if self.state not in (TxState.ARMED, TxState.TRANSMITTING):
                self._pa_idle_check(now)
                return
            tx = self.state == TxState.TRANSMITTING
            # deadline / expiry / heartbeat
            if tx and self._inflight is not None and now > self._inflight.permit.deadline_mono:
                self.trip("TX_HANG", "burst %s ran past its deadline" % self._inflight.permit.permit_id)
                return
            if now > self._expiry_mono:
                if tx:
                    self.trip("TX_HANG", "arm expired while transmitting")
                else:
                    self._event(EVT_ACTIVE_TX_EXPIRE, "info", session=self.arm_record.session_id)
                    self.disarm("expiry")
                return
            if self._last_hb is not None and now - self._last_hb > p.heartbeat_timeout_s:
                if tx:
                    self.trip("HEARTBEAT", "no heartbeat for %.3f s while transmitting" % (now - self._last_hb))
                else:
                    self.disarm("watchdog")
                return
            # the arming process's record (adopted flowgraph controllers)
            if inp["adopt"] is not None:
                self._revoke_adopted(inp["adopt"])
                return
            # hardware inputs
            if inp["il"] is False:
                self.trip("INTERLOCK_OPEN", "interlock loop opened")
                return
            if self._mode == "dummy_load" and inp["dl"] is False:
                self.trip("DUMMY_LOAD_LOST", "dummy-load sense lost")
                return
            tv = inp["tv"]
            if tv is not None:
                self.trip(tv[0], tv[1])
                return
            if p.require_ref_lock:
                st, why = inp["ref"]
                if st is False:
                    self.trip("REF_UNLOCK", why)
                    return
            # retune (spec 12.2: ARMED -> SAFE; while TRANSMITTING a trip)
            why = self._retune_problem(inp["freq"])
            if why is not None:
                if tx:
                    self.trip("FREQ_CHANGED", why)
                else:
                    logger.warning("TX safety: %s: disarming", why)
                    self.disarm("retune")
                return
            self._check_underflows(now)
            if self.state not in (TxState.ARMED, TxState.TRANSMITTING):
                return
            # permits past their deadline; a PA left on with nothing to send
            self._expire_permits(now)
            self._pa_idle_check(now)
            # configuration fingerprint (throttled)
            if self._fp_checked is None or now - self._fp_checked >= FINGERPRINT_POLL_S:
                self._fp_checked = now
                try:
                    changed = self._fingerprint() != self.arm_record.fingerprint
                except Exception:
                    changed = True
                if changed:
                    self.disarm("config_changed")
                    return
            if self._station_next is not None and now >= self._station_next:
                self._station_next = now + p.station_id_interval_s
                self._event(EVT_ACTIVE_STATION_ID, "warning", callsign=p.callsign,
                            session=self.arm_record.session_id, reminder="identify your station")

    def start_watchdog(self, period_s=0.05, heartbeat=False) -> None:
        """Background ``poll()`` every ``period_s`` (``heartbeat=True``: the thread also
        heartbeats - process liveness only, used by ``flowgraph_guard``)."""
        with self._lock:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._watchdog_stop.clear()
            period = max(0.001, float(period_s))

            def loop():
                while not self._watchdog_stop.wait(period):
                    try:
                        if heartbeat:
                            self.heartbeat()
                        self.poll()
                    except Exception as exc:
                        self.trip("EXCEPTION", "watchdog: %r" % (exc,))
            self._watchdog = threading.Thread(target=loop, name="active-tx-watchdog", daemon=True)
            self._watchdog.start()

    def stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        t = self._watchdog
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._watchdog = None

    # --- trips, reset, shutdown -----------------------------------------------------------------
    def trip(self, code: str, detail: str = "") -> None:
        """Latch FAULT and run ``safe_shutdown``; never raises.

        The lock is taken with a timeout (``TRIP_LOCK_TIMEOUT_S``): a thread stuck
        in sensor / backend I/O while holding it cannot block a trip (e.g. from a
        signal handler); without the lock the fault is still latched and the
        shutdown sequence still runs."""
        self._trip_gen += 1             # first: an interrupted critical section must not commit
        got = False
        try:
            try:
                got = self._lock.acquire(timeout=TRIP_LOCK_TIMEOUT_S)
            except Exception:  # pragma: no cover
                got = False
            if not got:
                logger.critical("TX trip %s: controller lock busy; shutting down without it", code)
            code = str(code)
            self.fault_history.append((code, str(detail), self._wallclock()))
            if self.state == TxState.DISABLED:
                logger.error("TX trip %s while DISABLED: %s", code, detail)
                self._event(EVT_ACTIVE_TX_TRIP, "error", code=code, detail=detail, state="disabled")
                self.safe_shutdown("trip:%s" % code, lock_timeout=None if got else 0.0)
                return
            first = self.state != TxState.FAULT
            if first:
                self.fault_code = code
                self.fault_detail = str(detail)
            sess = self.arm_record.session_id if self.arm_record else ""
            self.state = TxState.FAULT
            logger.critical("TX TRIP %s: %s", code, detail)
            self._event(EVT_ACTIVE_TX_TRIP, "critical", code=code, detail=detail, session=sess,
                        operator=getattr(self.arm_record, "operator", ""), mode=self._mode or "",
                        latched=self.fault_code)
            self.safe_shutdown("trip:%s" % code, fault=True, lock_timeout=None if got else 0.0)
        except Exception as exc:  # pragma: no cover
            logger.critical("trip(%s) failed: %r", code, exc)
        finally:
            if got:
                self._lock.release()

    def reset_fault(self, operator: str, confirmation: str) -> None:
        """FAULT -> SAFE (never ARMED) with ``reset_phrase(code)``; the cause must be clear."""
        with self._lock:
            if self.state == TxState.DISABLED:
                self._refuse("TX_DISABLED", "TX is disabled")
            if self.state != TxState.FAULT:
                self._refuse("SEQUENCE", "no fault is latched (state %s)" % self.state.value)
            if not (operator or "").strip():
                self._refuse("NO_OPERATOR", "an operator name is required to reset a fault")
            want = reset_phrase(self.fault_code)
            if confirmation != want:
                self._refuse("CONFIRMATION", "confirmation does not match %r" % want)
            if self._interlock_state() is False:
                self._refuse("INTERLOCK_OPEN", "the interlock is still open")
            tv = self._telemetry_violation()
            if tv is not None and tv[0] != "TELEMETRY_STALE":
                self._refuse(tv[0], "cause not cleared: %s" % tv[1])
            if self.policy.require_ref_lock and self._ref_state()[0] is False:
                self._refuse("REF_UNLOCK", "the reference is still unlocked")
            code = self.fault_code
            self.safe_shutdown("fault_reset")
            self.state = TxState.SAFE
            self.arm_record = None
            self._mode = None
            self.fault_code = ""
            self.fault_detail = ""
            self._event(EVT_ACTIVE_TX_FAULT_RESET, "warning", operator=operator.strip(), code=code)

    def _clear_own_record(self, session) -> None:
        """Delete the arm record; an adopted (flowgraph) controller deletes it only
        while it still belongs to its own session (never the arming process's next one)."""
        if self._adopted:
            disk = read_arm_record(self.state_dir)
            if disk is None or disk.session_id != session:
                return
        clear_arm_record(self.state_dir)

    def safe_shutdown(self, reason: str, fault: bool = None, lock_timeout=None) -> list:
        """The fail-safe sequence (spec 12.8): fixed order, every step in its own try,
        idempotent, never raises. Returns ``[(step, ok, error)]``.

        ``lock_timeout``: how long to wait for the controller lock (default 5 s);
        the sequence runs without it after that."""
        self._trip_gen += 1
        steps = []
        session = getattr(self.arm_record, "session_id", "")

        def run(name, fn, target=True):
            if target is None:
                steps.append((name, True, "skipped: not attached"))
                return True
            try:
                fn()
                steps.append((name, True, ""))
                return True
            except Exception as exc:
                logger.critical("safe_shutdown step %s failed: %r", name, exc)
                steps.append((name, False, repr(exc)))
                return False

        try:
            wait = 5.0 if lock_timeout is None else max(0.0, float(lock_timeout))
            lock_ok = self._lock.acquire(timeout=wait) if wait > 0 else self._lock.acquire(blocking=False)
        except Exception:  # pragma: no cover
            lock_ok = False
        try:
            # 1. state + permits + sessions
            def mark():
                if fault or self.state == TxState.FAULT:
                    if self.state != TxState.DISABLED:
                        self.state = TxState.FAULT
                elif self.state != TxState.DISABLED:
                    self.state = TxState.SAFE
                for pid in list(self._registry):
                    self._revoke(pid, "shutdown:%s" % reason)
                self._registry.clear()
                self.duty.release_all()
                self._inflight = None
                self.arm_record = None if self.state != TxState.FAULT else self.arm_record
                self._mode = None if self.state != TxState.FAULT else self._mode
                for fn in list(self._guard_stop_fns):
                    try:
                        fn()
                    except Exception as exc:
                        logger.error("guard stop failed: %r", exc)
            run("mark_state", mark)
            be, rf = self.backend, self.rf
            # 2-3. backend
            run("tx_abort", lambda: be.tx_abort(), be)
            run("tx_gain_min", lambda: be.set_tx_gain_min(), be)
            # 4. PA off
            run("pa_off", lambda: rf.pa_enable(None, False), rf)
            # 5. settle, T/R rx
            run("pa_settle", lambda: self._sleep(self.policy.pa_settle_us / 1e6), rf)
            run("tr_rx", lambda: rf.tr_switch("rx"), rf)
            # 6. guard, LNA + RX gain restore
            run("tr_guard", lambda: self._sleep(self.policy.tr_guard_us / 1e6), rf)

            def lna():
                if callable(getattr(rf, "restore_lna", None)):
                    rf.restore_lna()            # the pre-TX state (an operator's LNA-off is kept)
                    return
                restore = rf.lna_restore_value() if hasattr(rf, "lna_restore_value") else True
                rf.lna_bias(restore)
            run("lna_restore", lna, rf)
            run("rx_gain_restore", lambda: rf.restore_rx_gain() if hasattr(rf, "restore_rx_gain") else None, rf)
            # 7. record, event, sidecar
            run("clear_arm_record", lambda: self._clear_own_record(session))
            self.last_shutdown = steps
            run("event", lambda: self.event_log.emit(EVT_ACTIVE_SHUTDOWN, "warning", reason=reason,
                                                     state=self.state.value, steps=list(steps)))
            if self._listeners:
                st = None
                try:
                    st = self.status()
                except Exception as exc:
                    logger.error("status() failed during shutdown: %r", exc)
                for fn in list(self._listeners):
                    run("sidecar", lambda fn=fn: fn(st))
            self.last_shutdown = steps
        except Exception as exc:  # pragma: no cover - every step is wrapped
            logger.critical("safe_shutdown internal error: %r", exc)
        finally:
            if lock_ok:
                self._lock.release()
        return steps

    # --- process hooks -------------------------------------------------------------------------
    def _on_signal(self, signum, frame):
        try:
            name = signal.Signals(signum).name
        except (ValueError, TypeError):
            name = str(signum)
        try:
            if self.state in (TxState.ARMED, TxState.TRANSMITTING):
                self.trip("SIGNAL", name)
            else:
                self.safe_shutdown("signal:%s" % name)
        finally:
            prev = (self._hooks or {}).get("signals", {}).get(signum)
            if callable(prev):
                prev(signum, frame)
            elif prev == signal.SIG_DFL:
                if signum == signal.SIGINT:
                    raise KeyboardInterrupt
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

    def _on_exit(self):
        try:
            if self.state in (TxState.ARMED, TxState.TRANSMITTING):
                self.trip("EXIT_WHILE_ARMED", "interpreter exit while armed")
            elif self.backend is not None or self.rf is not None:
                self.safe_shutdown("exit")
        except Exception:  # pragma: no cover
            pass

    def install_process_hooks(self) -> None:
        """atexit + SIGINT/SIGTERM/SIGHUP handlers that run ``safe_shutdown`` (or trip
        ``SIGNAL`` while armed), then chain to the previous handler. Main thread only."""
        if threading.current_thread() is not threading.main_thread():
            logger.warning("install_process_hooks: not on the main thread; no hooks installed")
            return
        if self._hooks is not None:
            return
        hooks = {"signals": {}}
        for nm in ("SIGINT", "SIGTERM", "SIGHUP"):
            sig = getattr(signal, nm, None)
            if sig is None:
                continue
            try:
                hooks["signals"][sig] = signal.getsignal(sig)
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError) as exc:
                logger.warning("cannot hook %s: %s", nm, exc)
        atexit.register(self._on_exit)
        self._hooks = hooks

    def uninstall_process_hooks(self) -> None:
        """Restore the previous signal handlers and unregister the atexit hook."""
        if self._hooks is None:
            return
        for sig, prev in self._hooks["signals"].items():
            try:
                signal.signal(sig, prev if prev is not None else signal.SIG_DFL)
            except (ValueError, OSError, TypeError):
                pass
        try:
            atexit.unregister(self._on_exit)
        except Exception:  # pragma: no cover
            pass
        self._hooks = None

    @contextlib.contextmanager
    def session(self, mode, fc_hz, confirmation, operator="", **kw):
        """``with ctl.session(...) as rec:`` arm ... finally ``safe_shutdown``."""
        rec = self.arm(mode, fc_hz, confirmation, operator, **kw)
        try:
            yield rec
        finally:
            if self.state == TxState.FAULT:
                self.safe_shutdown("session_end")
            else:
                self.disarm("session_end")
                if self.state != TxState.SAFE:
                    self.safe_shutdown("session_end")

    # --- status ------------------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            p = self.policy
            rec = self.arm_record
            return {
                "state": self.state.value, "enabled": p.enabled, "mode": self._mode,
                "session": rec.session_id if rec else "", "operator": rec.operator if rec else "",
                "fc_hz": rec.fc_hz if rec else None,
                "expires_in_s": (self._expiry_mono - now) if (rec and self._expiry_mono is not None
                                                             and self.state != TxState.FAULT) else None,
                "fault_code": self.fault_code, "fault_detail": self.fault_detail,
                "reset_phrase": reset_phrase(self.fault_code) if self.fault_code else "",
                "fault_history": [list(x) for x in self.fault_history[-20:]],
                "duty": self.duty.duty(now), "permits_outstanding": len(self._registry),
                "inflight": self._inflight.permit.permit_id if self._inflight else "",
                "simulation": bool(self.is_simulation), "allowed_modes": list(p.allowed_modes),
                "limits": {"max_digital_amplitude": p.max_digital_amplitude,
                           "max_tx_gain_db": list(p.max_tx_gain_db),
                           "max_chain_power_dbm": list(p.max_chain_power_dbm),
                           "max_total_power_dbm": p.max_total_power_dbm, "max_eirp_dbm": p.max_eirp_dbm,
                           "max_burst_s": p.max_burst_s, "duty_max": p.duty_max,
                           "duty_window_s": p.duty_window_s},
                "last_shutdown": [list(s) for s in self.last_shutdown],
                "interlock": self._interlock_state(),
            }


# ---------------------------------------------------------------------------
# GNU Radio flowgraph guard
# ---------------------------------------------------------------------------
class _GuardTargets:
    """Shutdown targets of a generated flowgraph (``tx_abort`` = stop the graph)."""
    is_simulation = False

    def __init__(self, stop_fn, gain_min_fn):
        self._stop = stop_fn
        self._gain_min = gain_min_fn

    def tx_abort(self):
        if self._stop is not None:
            self._stop()

    def set_tx_gain_min(self):
        if self._gain_min is not None:
            self._gain_min()


class FlowgraphGuard:
    """Safety guard inside a generated GNU Radio TX script (spec 13.5)."""

    def __init__(self, controller: TxSafetyController, record: ArmRecord, chains, tx_gains_db, fs_hz):
        self.controller = controller
        self.record = record
        self.chains = tuple(int(c) for c in chains)
        self.tx_gains_db = tuple(float(g) for g in (tx_gains_db if np.ndim(tx_gains_db)
                                                    else [tx_gains_db] * len(self.chains)))
        self.fs_hz = float(fs_hz)
        self.bursts_checked = 0

    def check_waveform(self, samples, offset_s=0.0) -> TxPermit:
        """Re-run the per-burst limits (B2-B9) on the actual file samples of one burst.

        ``offset_s``: the burst's scheduled start relative to now (duty window).
        Refuses (``ARM_TOTAL``) beyond the record's ``max_bursts``.
        """
        rec = self.record
        if rec.max_bursts <= 0 or self.bursts_checked >= rec.max_bursts:
            raise SafetyRefusal("ARM_TOTAL", "the arm record authorizes %d flowgraph burst(s)" % rec.max_bursts)
        c = self.controller
        c.heartbeat()
        c.poll()                         # re-reads the arming process's record (disarm / trip there)
        permit = c.authorize_burst(samples, self.chains, self.tx_gains_db, self.fs_hz, start=None,
                                   purpose="flowgraph", fc_hz=rec.fc_hz, at_offset_s=offset_s)
        c.consume(permit)
        c.burst_complete(permit, TxResult(ok=True, sent_samples=int(permit.samples.shape[1])))
        self.bursts_checked += 1
        return permit

    def start_watchdog(self, stop_fn, gain_min_fn) -> None:
        """Stop the graph and set the TX gain to minimum at arm expiry, on a trip or on
        SIGTERM/SIGINT/SIGHUP (main thread)."""
        c = self.controller
        c.attach(_GuardTargets(stop_fn, gain_min_fn), None)
        c.install_process_hooks()
        c.start_watchdog(0.05, heartbeat=True)

    def shutdown(self, reason) -> list:
        c = self.controller
        c.stop_watchdog()
        steps = c.safe_shutdown(reason)
        c.uninstall_process_hooks()
        return steps


def flowgraph_guard(config_path, flowgraph_path, waveform_path, tx_gains_db, chains, fs_hz,
                    n_bursts=None, state_dir=None, clock=time.monotonic, wallclock=time.time,
                    event_log=None) -> FlowgraphGuard:
    """Adopt the arm record for a generated TX flowgraph, or raise ``SafetyRefusal``.

    Requires an arm record whose fingerprint equals the config's, whose
    ``flowgraph_sha256`` / ``waveform_sha256`` equal the sha256 of these files,
    which has not expired and whose ``max_bursts`` covers ``n_bursts``.
    """
    cfg = acfg.load_config(config_path)
    if not cfg.tx_enabled():
        raise SafetyRefusal("TX_DISABLED", "TX is disabled in %s" % config_path)
    try:
        fs = float(fs_hz)
    except (TypeError, ValueError):
        fs = float("nan")
    want_fs = float(cfg.rf.sample_rate_hz)
    if not (math.isfinite(fs) and abs(fs - want_fs) <= FS_REL_TOL * want_fs):
        raise SafetyRefusal("SHAPE", "flowgraph fs %r != configured sample_rate_hz %.6f" % (fs_hz, want_fs))
    sd = state_dir if state_dir is not None else cfg.resolved_state_dir()
    rec = read_arm_record(sd)
    if rec is None:
        raise SafetyRefusal("NOT_ARMED", "no valid arm record in %s" % sd)
    if acfg.config_fingerprint(cfg) != rec.fingerprint:
        raise SafetyRefusal("CONFIG_CHANGED", "the configuration changed since arming")
    try:
        fg_sha = ac.sha256_file(flowgraph_path)
        wf_sha = ac.sha256_file(waveform_path)
    except OSError as exc:
        raise SafetyRefusal("DIGEST", "cannot hash the flowgraph / waveform: %s" % exc) from None
    if not rec.flowgraph_sha256 or rec.flowgraph_sha256 != fg_sha:
        raise SafetyRefusal("DIGEST", "flowgraph sha256 does not match the arm record")
    if not rec.waveform_sha256 or rec.waveform_sha256 != wf_sha:
        raise SafetyRefusal("DIGEST", "waveform sha256 does not match the arm record")
    if not float(rec.expires_unix) > float(wallclock()):
        raise SafetyRefusal("ARM_EXPIRED", "the arm record expired")
    if rec.max_bursts <= 0 or (n_bursts is not None and int(n_bursts) > rec.max_bursts):
        raise SafetyRefusal("ARM_TOTAL", "the arm record authorizes %d burst(s), the flowgraph needs %s"
                            % (rec.max_bursts, n_bursts))
    ctl = TxSafetyController(cfg, None, None, event_log, clock=clock, wallclock=wallclock, state_dir=sd,
                             adopt_record=rec)
    # Safety inputs readable without a radio handle (file: lines). gpio / sim inputs cannot be
    # read from the flowgraph process and therefore read as open (fail safe).
    ctl.attach(None, None)
    return FlowgraphGuard(ctl, rec, chains, tx_gains_db, fs_hz)
