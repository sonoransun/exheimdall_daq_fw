"""
    HeIMDALL "active" capability: backend interface (``ActiveBackend``).

    Every RF backend (deterministic simulation, Ettus/NI UHD, Per Vices UHD
    fork, GNU Radio subprocess) implements this interface. The base class owns
    the rules that must hold for EVERY backend, so a hardware driver cannot
    forget them:

      - phase order ``CLOSED -> OPEN -> REFS_SET -> TIME_SET -> TUNED ->
        STREAMING`` (spec 4.12): tuning before the clock and time sources are
        set, or streaming before the device time is aligned (more than one
        device, or ``sync_method != none``) raises ``BackendError``. A clock /
        time source or master-clock-rate change after the time was set drops
        the phase back to ``REFS_SET`` and bumps the coherence generation; so
        does a sample-rate change after a tune and any time load outside
        ``SyncProcedure`` (``syncing()``);
      - TX interlocks on the RF state: while the attached TX safety controller
        is ARMED / TRANSMITTING a TX tune, a TX rate change, a time load, an LO
        source / export change or a TX integer-delay change disarms it first, and
        a clock / time source or master-clock-rate change trips it
        (``REF_UNLOCK``, spec 8.3). The check and the change are ONE critical
        section (``_tx_change``: ``_tx_lock`` then ``_lock``, TX re-checked inside),
        so no burst can be authorized and sent between them;
      - command time: tracked per device; ``tx_burst`` refuses ``SEQUENCE`` while
        one is set (UHD would queue the TX gain for it); ``command_window`` holds
        ``_tx_lock`` across set / clear. A timed tune whose command time is not
        ahead of the device time ran untimed (``TUNE_LATE``);
      - ``open()`` forgets every API-tracked RF setting (a re-opened device is
        re-initialized) and a re-open bumps the generation (``reopen``);
      - tune frequencies follow UHD ``tune_result_t`` (``TuneReport``);
      - the phase state after a tune is evaluated PER TUNE from the frequency
        actually tuned and the LO-share state actually programmed through
        ``set_lo_source`` / ``set_lo_export`` (``phase_after_tune_at``); a tune
        that changes the phase class or crosses an LO-share floor bumps, and so
        does a change of the programmed LO share or integer delay;
      - ``open()`` always ends with the TX gain at its minimum and every
        configured PA line off (even with ``en_tx = 0``); ``close()`` and
        ``__del__`` run ``safe_state()`` first;
      - the ONLY transmit entry point is ``tx_burst(permit)``: it refuses
        without an enabled TX safety controller, never creates a TX streamer
        while TX is disabled, checks the device TX rate / frequency / LO offset
        against the permit, lets the controller ``consume`` the permit, then
        re-checks (trip generation, controller state, the device clock against
        the authorized start, the gain the device really applied) right before
        the send, sends ``permit.samples`` only (no checked/sent mismatch), ends
        every burst with an end-of-burst and reports the outcome back with
        ``burst_complete``. A send that raises, or a burst that is never
        acknowledged, aborts the streamer and trips the controller
        (``EXCEPTION`` / ``TX_HANG``);
      - lock order: ``_tx_lock`` -> safety controller lock -> backend ``_lock``.
        The backend NEVER calls into the safety controller while holding
        ``_lock``, and the blocking send runs without ``_lock``. GPIO goes
        through its own short ``_gpio_lock``, so a trip from another thread
        drives the PA lines off while a burst is in flight;
      - ``safe_state``, ``tx_abort`` and ``set_tx_gain_min`` are always
        allowed and never raise;
      - spectral inversion (``[active_device:*] iq_conjugate``) is undone
        host-side: RX rows are conjugated after the read, TX rows before the send;
      - ``CoherenceTracker``: the generation number that invalidates
        calibrations whenever the relative channel phases may have changed.

    Concrete backends implement the protected ``_do_*`` hooks only. Hardware
    libraries are imported lazily by the concrete modules; importing this
    module needs only the stdlib and numpy.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import abc
import contextlib
import importlib
import logging
import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np

import active_common as ac
from active_common import (EVT_ACTIVE_COHERENCE_BUMP, EVT_ACTIVE_FALLBACK, EVT_ACTIVE_OPEN,
                           AsyncEvent, BackendError, BackendUnavailable, ConfigError,
                           DevTime, HardwareFault, InterlockTrip, Issue, NotSupported, SafetyRefusal,
                           TxResult)
import active_devices as adev

logger = logging.getLogger("heimdall.active.backend")

PHASES = ("CLOSED", "OPEN", "REFS_SET", "TIME_SET", "TUNED", "STREAMING")
_RANK = {p: i for i, p in enumerate(PHASES)}
DIRECTIONS = ("rx", "tx")
# Phase states after a tune whose channel-to-channel phase is stable (no recal needed
# after a TIMED tune): spec 8.4 step 6.
STABLE_PHASE_STATES = ("repeatable", "shared_lo", "deterministic")
PHASE_STATES = ("random", "ambiguous_180", "deterministic", "repeatable", "shared_lo")
_PHASE_RANK = {"random": 0, "ambiguous_180": 1, "deterministic": 2, "repeatable": 2, "shared_lo": 3}
LIVE_TX_STATES = ("armed", "transmitting")
TX_FREQ_TOL_HZ = 1.0             # permit fc vs the tuned TX centre (= the safety controller's FREQ_TOL_HZ)
TX_RATE_RTOL = 1e-9              # permit fs vs the device TX rate (relative)
TX_START_TOL_S = 0.05            # device clock vs the authorized start, on the controller's clock
TX_GAIN_TOL_DB = 1e-9            # applied (coerced) TX gain may never exceed the permit gain
TUNE_MIN_LEAD_S = 1e-4           # a timed tune must be at least this far ahead of the device time
# Backends by [active] backend -> (module, class, install hint)
BACKEND_MODULES = {
    "uhd": ("active_backend_uhd", "UhdBackend",
            "install the Ettus UHD Python bindings (python3-uhd / 'import uhd')"),
    "pervices": ("active_backend_pervices", "PerVicesBackend",
                 "install the Per Vices UHD fork bindings (python3-uhdpv / libuhdpv)"),
    "gnuradio": ("active_gnuradio", "GnuradioBackend",
                 "install GNU Radio (gnuradio, or gnuradiopv for Per Vices) and pyzmq"),
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class TuneReport:
    """Outcome of one ``tune(direction, ...)`` call over every channel of that direction.

    Frequency contract (UHD ``tune_result_t`` / ``multi_usrp`` semantics, so a UHD
    backend passes its values through unchanged):

      - ``actual_rf_hz``: UHD ``actual_rf_freq`` (the RF front-end LO). A
        ``TuneRequest(target, lo_off)`` puts it at ``target + lo_off``;
      - ``actual_dsp_hz``: UHD ``actual_dsp_freq`` (the DUC / DDC CORDIC shift,
        with UHD's direction-dependent sign: RX_SIGN = +1, TX_SIGN = -1);
      - ``actual_center_hz`` (optional, preferred): the actual centre per channel
        (``get_rx_freq`` / ``get_tx_freq``). When it is absent the base class
        derives the centre the way UHD does: ``rx: rf - dsp``, ``tx: rf + dsp``.
    """
    direction: str
    refs: tuple                         # ChannelRef per channel, global order
    requested_hz: float
    actual_rf_hz: list                  # per channel: RF LO frequency (UHD actual_rf_freq)
    actual_dsp_hz: list                 # per channel: DSP (CORDIC) shift (UHD actual_dsp_freq, signed)
    timed: bool                         # the tune really executed at a timed command
    phase_state: str                    # PHASE_STATES (plan + caps + timing of THIS tune)
    lo_locked: list                     # per channel
    generation: int                     # coherence generation after the tune
    band_change: bool                   # an analog band edge (or an LO-share floor) was crossed
    issues: list = field(default_factory=list)
    phase_class_changed: bool = False   # the phase class differs from the previous tune's
    actual_center_hz: list = None       # per channel: actual tuned centre (get_xx_freq), optional

    def to_dict(self) -> dict:
        return ac.to_jsonable(self)


# UHD multi_usrp tune_xx_subdev_and_dsp: centre = rf - sign * dsp
UHD_DSP_SIGN = {"rx": +1.0, "tx": -1.0}


def tuned_center_hz(direction, report, requested_hz) -> float:
    """The actual tuned centre of channel 0 of ``report`` (UHD semantics, see
    ``TuneReport``); ``requested_hz`` when the report carries no frequencies."""
    cen = getattr(report, "actual_center_hz", None)
    if cen:
        return float(cen[0])
    if report.actual_rf_hz and report.actual_dsp_hz:
        return float(report.actual_rf_hz[0]) - UHD_DSP_SIGN[direction] * float(report.actual_dsp_hz[0])
    return float(requested_hz)


def phase_after_tune_at(cfg, fc_hz, timed, direction="rx", share_live=None, bcaps=None) -> str:
    """Phase state after ONE tune at ``fc_hz`` (spec 8.2 rule 4, evaluated per tune).

    Same rules as ``active_devices.phase_after_tune`` (the plan at the configured
    centre), but with the frequency actually tuned, whether this tune really ran
    at a timed command (``timed``) and, when ``share_live(device, direction)`` is
    given, whether that device's LO share is really programmed (export enabled on
    the exporter, the shared source selected on the importer). Only devices with
    channels in ``direction`` count (all devices when none has). The worst class
    over those devices is returned.
    """
    if not cfg.devices:
        return "random"
    plan = adev.resolve_lo_plan(cfg)
    shared = set(adev.lo_share_members(cfg, plan)["shared"])
    single = len(cfg.devices) == 1
    devs = [d for d in cfg.devices if (d.rx_channels if direction == "rx" else d.tx_channels)] or list(cfg.devices)
    f = float(fc_hz)
    worst = None
    for d in devs:
        caps = bcaps[d.name] if bcaps is not None and d.name in bcaps else adev.behaviour_caps(cfg, d)
        live = True if share_live is None else bool(share_live(d.name, direction))
        cls = "random"
        if plan == "n321_star" and d.name in shared and live and f >= caps.lo_share_min_hz:
            cls = "shared_lo"
        elif plan == "twinrx" and d.name in shared and live:
            cls = "shared_lo"
        elif single and caps.lo_share_kind == "intra_device":
            cls = "shared_lo"
        elif (plan == "external_lo" and caps.ext_lo_phase_ambiguity == "180" and d.lo_source == "external"
              and live):
            cls = "ambiguous_180"
        elif caps.phase_ambiguity == "deterministic":
            cls = "deterministic"
        elif timed and caps.timed_tune and caps.phase_repeatable_timed_tune:
            cls = "repeatable"
        if worst is None or _PHASE_RANK[cls] < _PHASE_RANK[worst]:
            worst = cls
    return worst


def lo_share_floor_crossed(cfg, f_old_hz, f_new_hz, direction="rx", bcaps=None) -> bool:
    """True when a retune ``f_old -> f_new`` crosses the LO-sharing floor
    (``caps.lo_share_min_hz``) of a device taking part in the LO share: the relative
    phases are redrawn exactly like at an analog band edge (counts as a band change)."""
    plan = adev.resolve_lo_plan(cfg)
    if plan not in ("n321_star", "twinrx"):
        return False
    shared = set(adev.lo_share_members(cfg, plan)["shared"])
    for d in cfg.devices:
        if d.name not in shared or not (d.rx_channels if direction == "rx" else d.tx_channels):
            continue
        caps = bcaps[d.name] if bcaps is not None and d.name in bcaps else adev.behaviour_caps(cfg, d)
        m = float(caps.lo_share_min_hz or 0.0)
        if m > 0 and (float(f_old_hz) >= m) != (float(f_new_hz) >= m):
            return True
    return False


@dataclass
class RxCapture:
    """A block of RX samples: ``data`` (M, n) complex64 in global RX order."""
    data: np.ndarray
    start: DevTime                      # device time of sample 0
    fs_hz: float
    fc_hz: float
    overflow: bool
    errors: list
    overdrive_mask: int
    dropped: int = 0


class CoherenceTracker:
    """Coherence generation: bumped whenever relative channel phases may have changed.

    A calibration is valid only for the generation it was measured in (spec 9.7).
    ``reasons`` lists ``(generation, reason)`` (bounded). Thread safe.
    """

    MAX_REASONS = 256

    def __init__(self, event_log=None, generation: int = 0):
        self.generation = int(generation)
        self.reasons = []
        self.event_log = event_log
        self._lock = threading.Lock()

    def bump(self, reason: str) -> int:
        with self._lock:
            self.generation += 1
            self.reasons.append((self.generation, str(reason)))
            if len(self.reasons) > self.MAX_REASONS:
                del self.reasons[:len(self.reasons) - self.MAX_REASONS]
            gen = self.generation
        if self.event_log is not None:
            try:
                self.event_log.emit(EVT_ACTIVE_COHERENCE_BUMP, "info", generation=gen, reason=str(reason))
            except Exception:
                pass
        return gen

    def snapshot(self) -> dict:
        with self._lock:
            return {"generation": self.generation,
                    "last_reason": self.reasons[-1][1] if self.reasons else "",
                    "reasons": [list(r) for r in self.reasons[-16:]]}


class _TxAbort(Exception):
    """Internal: abort a consumed burst before the send (``kind`` refuse | late | trip |
    interrupted); ``exc`` is the SafetyRefusal / InterlockTrip raised to the caller."""

    def __init__(self, kind, exc):
        super().__init__(kind, exc)
        self.kind = kind
        self.exc = exc


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------
class ActiveBackend(abc.ABC):
    """The backend interface (spec 4.12 / 13.1). Subclasses implement ``_do_*``."""

    name = "abstract"
    is_simulation = False

    def __init__(self, cfg, event_log=None):
        self.cfg = cfg
        self.event_log = event_log if event_log is not None else ac.EventLog()
        self.coherence = CoherenceTracker(self.event_log)
        self.safety = None
        self.fallback = False                    # True when this sim replaced a real backend
        self.caps = {d.name: adev.get_caps(d.family) for d in cfg.devices}
        self._devices = [d.name for d in cfg.devices]
        self._groups = adev.device_groups(cfg) if cfg.devices else []
        self._rx_refs = list(cfg.rx_refs())
        self._tx_refs = list(cfg.tx_refs())
        try:
            self._sync_method = adev.resolve_sync_method(cfg) if cfg.devices else "none"
        except Exception:  # noqa: BLE001  (a broken plan is reported by validate_device_plan)
            self._sync_method = cfg.timing.sync_method
        try:
            self._plan_phase = adev.phase_after_tune(cfg)      # the plan at the configured fc (status only)
        except Exception:  # noqa: BLE001
            self._plan_phase = "random"
        try:
            self._bcaps = {d.name: adev.behaviour_caps(cfg, d) for d in cfg.devices}
        except Exception:  # noqa: BLE001
            self._bcaps = None
        self._lock = threading.RLock()           # backend state; never held while calling the safety controller
        self._tx_lock = threading.RLock()        # one tx_burst at a time (taken before the safety lock)
        self._gpio_lock = threading.RLock()      # GPIO only: a trip reaches the PA lines during a send
        self._open = False
        self._ever_opened = False
        self._clock_set = set()
        self._time_src_set = set()
        self._time_set = set()
        self._tuned = False
        self._tuned_dirs = set()
        self._streaming = False
        self._tx_streamer = False
        self._sync_depth = 0
        self._iq_conj = {d.name: bool(d.iq_conjugate) for d in cfg.devices}
        self._pa_lines_cache = None
        # RF state as programmed through this API (per direction)
        self._rate_set = {}                      # direction -> actual rate (Hz)
        self._center = {}                        # direction -> tuned centre (Hz)
        self._lo_off_set = {}                    # direction -> tuned LO offset (Hz)
        self._last_tune = {}                     # direction -> (fc, lo, timed, phase_state)
        self._lo_src_state = {}                  # (device, direction) -> LO source
        self._lo_exp_state = {}                  # (device, direction) -> LO export enabled
        self._int_delay_set = {}                 # (direction, ChannelRef) -> device-side integer delay
        self._cmd_time_set = {}                  # device -> DevTime while a command time is set

    def _reset_rf_state(self) -> None:
        """Forget every RF setting programmed through this API (a (re)opened device is
        re-initialized: default LO, no LO export / import, no command time)."""
        self._tuned = False
        self._tuned_dirs.clear()
        self._streaming = False
        self._tx_streamer = False
        self._rate_set.clear()
        self._center.clear()
        self._lo_off_set.clear()
        self._last_tune.clear()
        self._lo_src_state.clear()
        self._lo_exp_state.clear()
        self._int_delay_set.clear()
        self._cmd_time_set.clear()

    # --- identity --------------------------------------------------------------------
    @property
    def hardware_id(self) -> str:
        return self.cfg.resolved_hardware_id(simulation=bool(self.is_simulation))

    def attach_safety(self, safety) -> None:
        self.safety = safety

    # --- TX interlocks (NEVER called with self._lock held: lock order safety -> backend) -------
    def tx_live(self, safety=None) -> bool:
        """True while the (attached) TX safety controller is ARMED or TRANSMITTING."""
        s = self.safety if safety is None else safety
        if s is None:
            return False
        st = getattr(s, "state", None)
        return getattr(st, "value", st) in LIVE_TX_STATES

    def _disarm_tx(self, reason: str) -> None:
        """Disarm a live TX before an RF change; refuses (``SEQUENCE``) when it stays live."""
        s = self.safety
        if not self.tx_live(s):
            return
        try:
            s.disarm(reason)
        except Exception as exc:  # noqa: BLE001  (disarm never raises; defensive)
            logger.critical("disarm(%s) failed: %r", reason, exc)
        if self.tx_live(s):
            raise SafetyRefusal("SEQUENCE", "%s: TX is still armed (disarm did not complete)" % reason)

    def _trip_tx(self, code: str, detail: str) -> None:
        """Trip a live TX (a reference / time-base change invalidates every TX timing)."""
        s = self.safety
        if not self.tx_live(s):
            return
        try:
            s.trip(code, detail)
        except Exception as exc:  # noqa: BLE001  (trip never raises; defensive)
            logger.critical("trip(%s) failed: %r", code, exc)

    @contextlib.contextmanager
    def _tx_change(self, what: str, trip=None):
        """Context of an RF change that invalidates TX (tune / rate / time / references).

        Disarms (or, with ``trip=(code, detail)``, trips) a live TX once right away -
        so a trip reaches the PA lines even while a burst is in flight -, then takes
        ``_tx_lock`` (no ``tx_burst`` can be between its last check and its send),
        disarms / trips again (TX may have been armed meanwhile) and finally takes
        ``_lock`` and re-checks: TX still live there raises ``SafetyRefusal
        ("SEQUENCE")`` and nothing is changed. The body runs under ``_tx_lock`` AND
        ``_lock`` (lock order ``_tx_lock`` -> safety -> ``_lock``)."""
        def quiesce():
            if trip is not None:
                self._trip_tx(trip[0], trip[1])
                if self.tx_live():
                    raise SafetyRefusal("SEQUENCE", "%s: TX is still armed (the trip did not complete)" % what)
            else:
                self._disarm_tx(what)
        quiesce()
        with self._tx_lock:
            quiesce()
            with self._lock:
                if self.tx_live():
                    raise SafetyRefusal("SEQUENCE", "%s: TX was armed during the change; nothing changed" % what)
                yield

    @contextlib.contextmanager
    def command_window(self, t: DevTime, device=None):
        """``set_command_time(t)`` ... ``clear_command_time()`` as ONE window, holding
        ``_tx_lock`` throughout: on UHD a command time applies to EVERY later control
        command of that motherboard (a TX gain included), so no burst may run inside it."""
        with self._tx_lock:
            self.set_command_time(t, device)
            try:
                yield self
            finally:
                self.clear_command_time(device)

    def command_time_set(self, device=None) -> bool:
        """True while a command time is set (``set_command_time`` without a clear) on
        ``device`` (any device when None)."""
        with self._lock:
            devs = self._devices if device is None else [device]
            return any(self._cmd_time_set.get(d) is not None for d in devs)

    def invalidate_time(self, reason: str) -> None:
        """Forget the device time alignment (e.g. a failed ``SyncProcedure``): stop RX,
        drop to REFS_SET (or below), clear the tune state and bump the generation.
        A live TX is tripped first (``REF_UNLOCK``)."""
        self._trip_tx("REF_UNLOCK", "device time invalidated (%s)" % reason)
        with self._lock:
            if self._streaming:
                try:
                    self._do_rx_stop()
                except Exception as exc:  # noqa: BLE001
                    logger.error("rx_stop during %s failed: %r", reason, exc)
                self._streaming = False
            self._time_set.clear()
            self._tuned = False
            self._tuned_dirs.clear()
            self.coherence.bump(reason)

    @contextlib.contextmanager
    def syncing(self):
        """Context of ``SyncProcedure``: time loads inside do not bump individually (the
        procedure bumps once, ``"sync"``, when it completes)."""
        with self._lock:
            self._sync_depth += 1
        try:
            yield self
        finally:
            with self._lock:
                self._sync_depth -= 1

    def _after_time_load(self, devs, reason) -> None:
        """Bookkeeping after a time load (under ``_lock``): TIME_SET, and a generation bump
        outside ``SyncProcedure`` (a time load is a re-sync trigger, spec 8.3)."""
        self._time_set.update(devs)
        if self._sync_depth <= 0:
            self.coherence.bump(reason)

    # --- RF state as programmed ------------------------------------------------------------
    def rate(self, direction="tx") -> float:
        """Current sample rate (Hz): the last ``set_rate`` result, else the configured rate.
        Also the ``tx_rate`` provider for the safety controller."""
        return float(self._rate_set.get(direction, self.cfg.rf.sample_rate_hz))

    def tuned_freq(self, direction="tx") -> float:
        """Current tuned centre (Hz): the ``tx_freq`` provider for the safety controller."""
        return float(self._center.get(direction, self.cfg.rf.center_freq_hz))

    def tuned_lo_offset_hz(self, direction="tx") -> float:
        """Current tuned LO offset (Hz): the RF LO sits at ``tuned_freq + tuned_lo_offset_hz``
        (UHD ``TuneRequest(target, lo_off)``: ``rf_freq = target + lo_off``)."""
        return float(self._lo_off_set.get(direction, self.cfg.rf.lo_offset_hz))

    def last_phase_state(self, direction="rx"):
        """Phase state of the last tune of ``direction`` (None before the first tune)."""
        lt = self._last_tune.get(direction)
        return lt[3] if lt else None

    def last_tune(self, direction="rx"):
        """``(fc_hz, lo_offset_hz, timed, phase_state)`` of the last tune, or None."""
        return self._last_tune.get(direction)

    def _share_live(self, dev, direction) -> bool:
        """Is ``dev``'s LO share (plan role) really programmed through this API?"""
        cfg = self.cfg
        plan = adev.resolve_lo_plan(cfg)
        if plan in ("n321_star", "twinrx"):
            mem = adev.lo_share_members(cfg, plan)
            if dev not in mem["shared"]:
                return False
            if dev in mem["exporters"]:
                return bool(self._lo_exp_state.get((dev, direction)))
            d = cfg.device(dev)
            src = self._lo_src_state.get((dev, direction))
            if plan == "twinrx" and d.lo_source == "companion":
                return src == "companion"
            exp_on = any(self._lo_exp_state.get((e, direction)) for e in mem["exporters"])
            return src == d.lo_source and exp_on
        if plan == "external_lo":
            return self._lo_src_state.get((dev, direction)) == "external"
        return True

    def rx_refs(self) -> list:
        return list(self._rx_refs)

    def tx_refs(self) -> list:
        return list(self._tx_refs)

    def device_names(self) -> list:
        return list(self._devices)

    def groups(self) -> list:
        return list(self._groups)

    def master_device(self) -> str:
        return self.cfg.master_device().name if self.cfg.devices else ""

    def num_rx(self) -> int:
        return len(self._rx_refs)

    def num_tx(self) -> int:
        return len(self._tx_refs)

    # --- phase ---------------------------------------------------------------------------
    @property
    def phase(self) -> str:
        if not self._open:
            return "CLOSED"
        if self._streaming:
            return "STREAMING"
        refs = self._refs_ok()
        if refs and self._tuned:
            return "TUNED"
        if refs and self._time_ok():
            return "TIME_SET"
        if refs:
            return "REFS_SET"
        return "OPEN"

    def _refs_ok(self) -> bool:
        devs = set(self._devices)
        return devs <= self._clock_set and devs <= self._time_src_set

    def _time_ok(self) -> bool:
        return bool(self._devices) and set(self._devices) <= self._time_set

    def time_required(self) -> bool:
        """Streaming needs an aligned device time: several devices, or ``sync_method != none``."""
        return len(self._devices) > 1 or self._sync_method != "none"

    def _require(self, min_phase: str, what: str) -> None:
        if _RANK[self.phase] < _RANK[min_phase]:
            raise BackendError("%s: backend phase is %s, needs %s or later (phase order %s)"
                               % (what, self.phase, min_phase, " -> ".join(PHASES)))

    def _require_stream_ok(self, what: str) -> None:
        self._require("REFS_SET", what)
        if self.time_required() and not self._time_ok():
            raise BackendError("%s: streaming before TIME_SET (%d device(s), sync_method %s): align "
                               "the device time first (SyncProcedure)" % (what, len(self._devices),
                                                                         self._sync_method))

    def _devs(self, device) -> list:
        if device is None:
            return list(self._devices)
        if device not in self._devices:
            raise BackendError("unknown device %r (configured: %s)" % (device, ", ".join(self._devices)))
        return [device]

    def _group_devs(self, group) -> list:
        if group is None:
            return list(self._devices)
        for g in self._groups:
            if g.name == group:
                return g.device_names()
        raise BackendError("unknown device group %r (groups: %s)"
                           % (group, ", ".join(g.name for g in self._groups)))

    def _invalidate_time(self, reason: str) -> None:
        """A reference / rate change after the time was set: drop to REFS_SET and bump."""
        if _RANK[self.phase] >= _RANK["TIME_SET"]:
            if self._streaming:
                try:
                    self._do_rx_stop()
                except Exception as exc:  # noqa: BLE001
                    logger.error("rx_stop during %s failed: %r", reason, exc)
                self._streaming = False
            self._time_set.clear()
            self._tuned = False
            self._tuned_dirs.clear()
            self.coherence.bump(reason)

    def _trace(self, name, *args) -> None:
        """Hook: called by every public API method (the simulator records the calls)."""

    def _emit(self, etype, severity="info", **payload) -> None:
        try:
            self.event_log.emit(etype, severity, backend=self.name, **payload)
        except Exception:
            pass

    # --- lifecycle -----------------------------------------------------------------------
    def open(self) -> None:
        """Open the devices; ends with the TX gain at min and every PA line off."""
        with self._lock:
            if self._open:
                raise BackendError("backend already open")
            self._trace("open")
            self._do_open()
            reopen = self._ever_opened
            self._ever_opened = True
            self._open = True
            self._clock_set.clear()
            self._time_src_set.clear()
            self._time_set.clear()
            self._reset_rf_state()
            if reopen:
                self.coherence.bump("reopen")
            self.set_tx_gain_min()
            failed = [s for s in self._pa_lines_off() if not s[1]]
            if failed:
                self.close()
                raise HardwareFault("open: cannot drive the PA lines off: %s"
                                    % "; ".join("%s %s" % (s[0], s[2]) for s in failed))
            self._emit(EVT_ACTIVE_OPEN, "info", simulation=bool(self.is_simulation),
                       hardware_id=self.hardware_id, devices=list(self._devices))

    def close(self) -> None:
        """``safe_state()`` first, then release the devices. Never raises."""
        try:
            with self._lock:
                if not self._open:
                    return
                self._trace("close")
                self.safe_state()
                if self._streaming:
                    try:
                        self._do_rx_stop()
                    except Exception as exc:  # noqa: BLE001
                        logger.error("close: rx_stop failed: %r", exc)
                    self._streaming = False
                try:
                    self._do_close()
                except Exception as exc:  # noqa: BLE001
                    logger.error("close failed: %r", exc)
                self._open = False
                self._tx_streamer = False
        except Exception as exc:  # pragma: no cover - defensive
            logger.critical("close: unexpected error %r", exc)

    def __del__(self):
        try:
            if getattr(self, "_open", False):
                self.safe_state()
        except Exception:
            pass

    def sleep(self, seconds: float) -> None:
        """Real backends sleep; the simulator advances its virtual time."""
        time.sleep(max(0.0, float(seconds)))

    def status(self) -> dict:
        return {"backend": self.name, "simulation": bool(self.is_simulation),
                "fallback": bool(self.fallback), "hardware_id": self.hardware_id,
                "phase": self.phase, "coherence": self.coherence.snapshot(),
                "devices": list(self._devices), "num_rx": self.num_rx(), "num_tx": self.num_tx(),
                "sync_method": self._sync_method, "tx_streamer": bool(self._tx_streamer)}

    # --- references and time ----------------------------------------------------------------
    def set_clock_source(self, src, device=None) -> None:
        with self._tx_change("set_clock_source",
                             trip=("REF_UNLOCK", "clock source change to %r while TX is armed" % (src,))):
            self._require("OPEN", "set_clock_source")
            devs = self._devs(device)
            self._trace("set_clock_source", src, device)
            self._invalidate_time("clock_source")
            self._do_set_clock_source(str(src), device)
            self._clock_set.update(devs)

    def set_time_source(self, src, device=None) -> None:
        with self._tx_change("set_time_source",
                             trip=("REF_UNLOCK", "time source change to %r while TX is armed" % (src,))):
            self._require("OPEN", "set_time_source")
            devs = self._devs(device)
            self._trace("set_time_source", src, device)
            self._invalidate_time("time_source")
            self._do_set_time_source(str(src), device)
            self._time_src_set.update(devs)

    def ref_locked(self, device):
        """True / False, or None when the device has no reference-lock sensor."""
        with self._lock:
            self._require("OPEN", "ref_locked")
            self._devs(device)
            self._trace("ref_locked", device)
            return self._do_ref_locked(device)

    def set_master_clock_rate(self, hz, device=None) -> None:
        with self._tx_change("set_master_clock_rate",
                             trip=("REF_UNLOCK", "master clock rate change while TX is armed")):
            self._require("OPEN", "set_master_clock_rate")
            self._devs(device)
            hz = float(hz)
            if not (math.isfinite(hz) and hz > 0):
                raise BackendError("master clock rate must be finite and > 0")
            self._trace("set_master_clock_rate", hz, device)
            self._invalidate_time("master_clock_rate")
            self._do_set_master_clock_rate(hz, device)

    def time_now(self, device=None) -> DevTime:
        with self._lock:
            self._require("OPEN", "time_now")
            dev = self.master_device() if device is None else self._devs(device)[0]
            self._trace("time_now", dev)
            return self._do_time_now(dev)

    def time_last_pps(self, device=None) -> DevTime:
        with self._lock:
            self._require("OPEN", "time_last_pps")
            dev = self.master_device() if device is None else self._devs(device)[0]
            self._trace("time_last_pps", dev)
            return self._do_time_last_pps(dev)

    def set_time_unknown_pps(self, t: DevTime, group=None) -> None:
        with self._tx_change("time_set"):
            self._require("REFS_SET", "set_time_unknown_pps")
            if not isinstance(t, DevTime):
                raise BackendError("set_time_unknown_pps needs a DevTime")
            devs = self._group_devs(group)
            self._trace("set_time_unknown_pps", t, group)
            self._do_set_time_unknown_pps(t, group)
            self._after_time_load(devs, "time_unknown_pps")

    def set_time_next_pps(self, t: DevTime, device=None) -> None:
        with self._tx_change("time_set"):
            self._require("REFS_SET", "set_time_next_pps")
            if not isinstance(t, DevTime):
                raise BackendError("set_time_next_pps needs a DevTime")
            devs = self._devs(device)
            self._trace("set_time_next_pps", t, device)
            self._do_set_time_next_pps(t, device)
            self._after_time_load(devs, "time_next_pps")

    def set_time_now(self, t: DevTime, device=None) -> None:
        """Set the device time immediately. NOT a multi-device alignment method: the host
        latency differs per device (milliseconds); SyncProcedure never uses it. On a
        multi-device configuration every call warns (calling it once per device is
        the same hazard as one call on all of them)."""
        with self._tx_change("time_set"):
            self._require("REFS_SET", "set_time_now")
            if not isinstance(t, DevTime):
                raise BackendError("set_time_now needs a DevTime")
            devs = self._devs(device)
            if len(self._devices) > 1:
                logger.warning("set_time_now on %s of %d devices: the host latency misaligns their time "
                               "(align with PPS: SyncProcedure)", "all" if device is None else device,
                               len(self._devices))
            self._trace("set_time_now", t, device)
            self._do_set_time_now(t, device)
            self._after_time_load(devs, "time_now")

    def time_synchronized(self) -> bool:
        with self._lock:
            self._require("OPEN", "time_synchronized")
            self._trace("time_synchronized")
            return bool(self._do_time_synchronized())

    def set_command_time(self, t: DevTime, device=None) -> None:
        """Timed commands from now on (UHD: EVERY later control command of that
        motherboard, a TX gain included, until ``clear_command_time``). ``tx_burst``
        refuses ``SEQUENCE`` while one is set; prefer ``command_window``."""
        with self._lock:
            self._require("OPEN", "set_command_time")
            if not isinstance(t, DevTime):
                raise BackendError("set_command_time needs a DevTime")
            devs = self._devs(device)
            self._trace("set_command_time", t, device)
            for d in devs:                       # recorded first: a failing hook leaves it "set"
                self._cmd_time_set[d] = t
            self._do_set_command_time(t, device)

    def clear_command_time(self, device=None) -> None:
        with self._lock:
            self._require("OPEN", "clear_command_time")
            devs = self._devs(device)
            self._trace("clear_command_time", device)
            self._do_clear_command_time(device)
            for d in devs:
                self._cmd_time_set.pop(d, None)

    # --- RF --------------------------------------------------------------------------------------
    def set_rate(self, direction, hz) -> float:
        """Set the sample rate of ``direction``; returns the actual (coerced) rate.

        A TX rate change disarms a live TX first (the burst length, duty and occupied
        bandwidth the permits were checked for change with the rate). A rate change
        after a tune bumps the coherence generation (spec 8.3 re-sync trigger) and
        stops a running RX stream (restart it at a timed t0)."""
        if direction not in DIRECTIONS:
            raise BackendError("direction must be rx or tx")
        with self._rf_guard(direction == "tx", "rate_change"):
            self._require("OPEN", "set_rate")
            hz = float(hz)
            if not (math.isfinite(hz) and hz > 0):
                raise BackendError("sample rate must be finite and > 0")
            self._trace("set_rate", direction, hz)
            old = self.rate(direction)
            actual = float(self._do_set_rate(direction, hz))
            self._rate_set[direction] = actual
            if abs(old - actual) > 1e-9 * max(abs(actual), 1.0):
                if direction == "rx" and self._streaming:
                    try:
                        self._do_rx_stop()
                    except Exception as exc:  # noqa: BLE001
                        logger.error("rx_stop on a rate change failed: %r", exc)
                    self._streaming = False
                if self._tuned:
                    self.coherence.bump("rate_%s" % direction)
            return actual

    def tune(self, direction, freq_hz, lo_offset_hz=0.0, at: DevTime = None, bump: bool = True) -> TuneReport:
        """Tune every channel of ``direction``. ``at`` = the command time the caller set
        (timed tune). ``bump=True`` applies the spec 8.4 generation rule to this single
        tune; ``active_timing.retune`` passes False and applies it once per retune.

        A TX tune disarms a live TX first (it moves the TX LO, its leakage and image).
        The phase state is evaluated for THIS tune (``phase_after_tune_at``: the
        frequency tuned, timed or not, the LO share as programmed); crossing an
        LO-share floor counts as a band change, and a phase class different from the
        previous tune's sets ``phase_class_changed``: both always bump."""
        if direction not in DIRECTIONS:
            raise BackendError("direction must be rx or tx")
        with self._rf_guard(direction == "tx", "retune"):
            self._require("REFS_SET", "tune")
            f = float(freq_hz)
            lo = float(lo_offset_hz)
            if not (math.isfinite(f) and f > 0 and math.isfinite(lo)):
                raise BackendError("tune: frequency must be finite and > 0, lo offset finite")
            if at is not None and not isinstance(at, DevTime):
                raise BackendError("tune: at must be a DevTime")
            self._trace("tune", direction, f, lo, at)
            f_old = self.tuned_freq(direction)
            late = self._late_command(direction, at) if at is not None else None
            rep = self._do_tune(direction, f, lo, at)
            f_new = tuned_center_hz(direction, rep, f)
            if late and rep.timed:
                # UHD executes a command whose time has passed at once: an untimed tune
                rep.timed = False
            if late:
                rep.issues.append(Issue("warning", "TUNE_LATE", "%s tune: %s (executed untimed)" % (direction, late)))
                logger.warning("%s tune: %s: the tune executed untimed", direction, late)
            rep.phase_state = self.phase_state_for(bool(rep.timed), direction, f_new)
            if lo_share_floor_crossed(self.cfg, f_old, f_new, direction, self._bcaps):
                rep.band_change = True
            prev = self._last_tune.get(direction)
            rep.phase_class_changed = prev is not None and prev[3] != rep.phase_state
            if bump and self.tune_needs_bump(rep):
                self.coherence.bump("band_change_%s" % direction if rep.band_change else "tune_%s" % direction)
            rep.generation = self.coherence.generation
            self._center[direction] = f_new
            self._lo_off_set[direction] = lo
            self._last_tune[direction] = (f_new, lo, bool(rep.timed), rep.phase_state)
            self._tuned = True
            self._tuned_dirs.add(direction)
            return rep

    def _rf_guard(self, tx: bool, what: str):
        """``_tx_change(what)`` for a change that affects TX, else just ``_lock``."""
        return self._tx_change(what) if tx else self._lock

    def _late_command(self, direction, at):
        """Why a tune ``at`` cannot be a timed command (under ``_lock``), else None: no
        command time set on a device of ``direction``, or ``at`` not after its device time."""
        refs = self._rx_refs if direction == "rx" else self._tx_refs
        devs = sorted({r.device for r in refs}) or list(self._devices)
        for d in devs:
            ct = self._cmd_time_set.get(d)
            if ct is None:
                return "no command time set on %s" % d
            try:
                lead = float(ct.diff_seconds(self._do_time_now(d)))
            except Exception as exc:  # noqa: BLE001  (unknown device time: not provably timed)
                return "cannot read the %s device time (%r)" % (d, exc)
            if not (math.isfinite(lead) and lead > TUNE_MIN_LEAD_S):
                return "command time %s is %.6f s from the %s device time (late)" % (ct, lead, d)
        return None

    def phase_state_for(self, timed: bool, direction: str = "rx", freq_hz=None) -> str:
        """Phase state after a tune of ``direction`` at ``freq_hz`` (default: the current
        tuned centre): caps + plan + the LO share as programmed + whether the tune was timed
        (a repeatable family tuned untimed is random)."""
        f = self.tuned_freq(direction) if freq_hz is None else float(freq_hz)
        try:
            return phase_after_tune_at(self.cfg, f, bool(timed), direction, self._share_live, self._bcaps)
        except Exception as exc:  # noqa: BLE001  (fail safe: an unknown state is random -> bump)
            logger.error("phase_after_tune_at failed: %r", exc)
            return "random"

    @staticmethod
    def tune_needs_bump(report) -> bool:
        """Spec 8.4 step 6: bump unless the phase state is stable AND the tune was timed;
        a band change (analog band edge or LO-share floor) and a phase class change
        always bump."""
        if report.band_change or getattr(report, "phase_class_changed", False):
            return True
        return not (report.phase_state in STABLE_PHASE_STATES and report.timed)

    def set_rx_gain(self, gains_db) -> list:
        with self._lock:
            self._require("OPEN", "set_rx_gain")
            n = self.num_rx()
            g = [float(gains_db)] * n if np.ndim(gains_db) == 0 else [float(x) for x in gains_db]
            if len(g) != n:
                raise BackendError("set_rx_gain: %d values for %d RX channels" % (len(g), n))
            if not all(math.isfinite(x) for x in g):
                raise BackendError("set_rx_gain: gains must be finite")
            self._trace("set_rx_gain", tuple(g))
            return list(self._do_set_rx_gain(g))

    def rx_gain_db(self) -> list:
        with self._lock:
            self._require("OPEN", "rx_gain_db")
            return list(self._do_rx_gain())

    def set_tx_gain_min(self) -> None:
        """Every TX channel to its minimum gain. Always allowed; never raises."""
        try:
            self._trace("set_tx_gain_min")
            if self._open or self._tx_refs:
                self._do_set_tx_gain_min()
        except Exception as exc:  # noqa: BLE001
            logger.critical("set_tx_gain_min failed: %r", exc)

    def lo_names(self, direction, ref) -> list:
        with self._lock:
            self._require("OPEN", "lo_names")
            self._trace("lo_names", direction, ref)
            return list(self._do_lo_names(direction, ref))

    def _bump_outside_sync(self, reason) -> None:
        """Bump for an RF-state change (under ``_lock``); inside ``SyncProcedure`` the
        procedure bumps once itself."""
        if self._sync_depth <= 0:
            self.coherence.bump(reason)

    def set_lo_source(self, direction, src, ref, name="all") -> None:
        """Select the LO source. An LO change moves the relative channel phases at once
        (LO shares feed both directions on an N321 star): a live TX is disarmed first and
        a change of the programmed source bumps the coherence generation."""
        if direction not in DIRECTIONS:
            raise BackendError("direction must be rx or tx")
        with self._tx_change("lo_source"):
            self._require("OPEN", "set_lo_source")
            self._trace("set_lo_source", direction, src, ref, name)
            key = (ref.device, direction)
            old = self._lo_src_state.get(key, "internal")
            try:
                self._do_set_lo_source(direction, str(src), ref, name)
            except (NotSupported, BackendError):
                raise                                       # refused: nothing was changed
            except Exception:
                self._lo_src_state[key] = "unknown"         # a half-done change: not shared
                self._bump_outside_sync("lo_source_%s" % direction)
                raise
            self._lo_src_state[key] = str(src)
            if old != str(src):
                self._bump_outside_sync("lo_source_%s" % direction)

    def set_lo_export(self, direction, enabled, ref, name="all") -> None:
        """Enable / disable an LO export (disarms a live TX first; a change bumps)."""
        if direction not in DIRECTIONS:
            raise BackendError("direction must be rx or tx")
        with self._tx_change("lo_export"):
            self._require("OPEN", "set_lo_export")
            self._trace("set_lo_export", direction, bool(enabled), ref, name)
            key = (ref.device, direction)
            old = bool(self._lo_exp_state.get(key, False))
            try:
                self._do_set_lo_export(direction, bool(enabled), ref, name)
            except (NotSupported, BackendError):
                raise                                       # refused: nothing was changed
            except Exception:
                self._lo_exp_state[key] = False             # a half-done change: not shared
                self._bump_outside_sync("lo_export_%s" % direction)
                raise
            self._lo_exp_state[key] = bool(enabled)
            if old != bool(enabled):
                self._bump_outside_sync("lo_export_%s" % direction)

    def set_integer_delay(self, direction, ref, samples: int, bump: bool = True) -> bool:
        """Device-side integer delay correction; False = unsupported (the host shifts).

        A TX delay change disarms a live TX first (beamformed bursts were authorized
        for the old delays). A change of the programmed delay bumps the coherence
        generation (``integer_delay_<dir>``) unless ``bump=False``: a calibration
        that programs its OWN correction passes False and records ``integer_delays()``
        in its result."""
        if direction not in DIRECTIONS:
            raise BackendError("direction must be rx or tx")
        with self._rf_guard(direction == "tx", "integer_delay"):
            self._require("OPEN", "set_integer_delay")
            n = int(samples)
            self._trace("set_integer_delay", direction, ref, n)
            key = (direction, ref)
            old = int(self._int_delay_set.get(key, 0))
            ok = bool(self._do_set_integer_delay(direction, ref, n))
            if ok:
                self._int_delay_set[key] = n
                if bump and old != n:
                    self._bump_outside_sync("integer_delay_%s" % direction)
            return ok

    def integer_delays(self, direction="rx") -> dict:
        """``{ChannelRef: samples}`` as programmed through ``set_integer_delay``."""
        with self._lock:
            return {r: n for (d, r), n in self._int_delay_set.items() if d == direction}

    def sensors(self, device) -> dict:
        with self._lock:
            self._require("OPEN", "sensors")
            dev = self._devs(device)[0] if device is not None else self.master_device()
            self._trace("sensors", dev)
            return dict(self._do_sensors(dev))

    def gpio_write(self, line, value: bool) -> None:
        """Logical line state (True = asserted); the backend applies mask / polarity.

        Uses the short GPIO lock only (never ``_lock``): a trip from the watchdog drives
        the PA lines off even while a burst is being sent."""
        with self._gpio_lock:
            self._require("OPEN", "gpio_write")
            self._trace("gpio_write", str(line), bool(value))
            self._do_gpio_write(line, bool(value))

    def gpio_read(self, line) -> bool:
        with self._gpio_lock:
            self._require("OPEN", "gpio_read")
            self._trace("gpio_read", str(line))
            return bool(self._do_gpio_read(line))

    def gpio_atr(self, device, bank, ctrl_mask, idle, rx, tx, duplex) -> bool:
        """USRP ATR on ``bank`` (ELECTRICAL values); False = no ATR on this backend."""
        with self._gpio_lock:
            self._require("OPEN", "gpio_atr")
            self._trace("gpio_atr", device, bank, int(ctrl_mask), int(idle), int(rx), int(tx), int(duplex))
            return bool(self._do_gpio_atr(device, bank, int(ctrl_mask), int(idle), int(rx), int(tx),
                                          int(duplex)))

    # --- streaming ----------------------------------------------------------------------------
    def _conj_rx(self, data: np.ndarray) -> np.ndarray:
        rows = [m for m, r in enumerate(self._rx_refs) if self._iq_conj.get(r.device)]
        if rows:
            data[rows] = np.conj(data[rows])
        return data

    def rx_capture(self, nsamps: int, start: DevTime) -> RxCapture:
        """One timed capture of ``nsamps`` per channel starting at device time ``start``."""
        with self._lock:
            self._require_stream_ok("rx_capture")
            if not isinstance(start, DevTime):
                raise BackendError("rx_capture needs a DevTime start (stream_now is never used)")
            n = int(nsamps)
            if n <= 0:
                raise BackendError("rx_capture: nsamps must be > 0")
            self._trace("rx_capture", n, start)
            cap = self._do_rx_capture(n, start)
            cap.data = self._conj_rx(cap.data)
            return cap

    def rx_start(self, start: DevTime) -> None:
        with self._lock:
            self._require_stream_ok("rx_start")
            if not isinstance(start, DevTime):
                raise BackendError("rx_start needs a DevTime start (stream_now is never used)")
            if self._streaming:
                raise BackendError("rx_start: already streaming (rx_stop first)")
            self._trace("rx_start", start)
            self._do_rx_start(start)
            self._streaming = True

    def rx_read(self, nsamps: int, timeout_s=1.0) -> RxCapture:
        with self._lock:
            if not self._streaming:
                raise BackendError("rx_read: not streaming (rx_start first)")
            n = int(nsamps)
            if n <= 0:
                raise BackendError("rx_read: nsamps must be > 0")
            self._trace("rx_read", n)
            cap = self._do_rx_read(n, float(timeout_s))
            cap.data = self._conj_rx(cap.data)
            return cap

    def rx_stop(self) -> None:
        with self._lock:
            if not self._streaming:
                return
            self._trace("rx_stop")
            try:
                self._do_rx_stop()
            finally:
                self._streaming = False

    def async_events(self) -> list:
        """Drain the asynchronous device events. Reference / LO unlocks are also passed to
        the attached TX safety controller (a trip while armed)."""
        with self._lock:
            evs = list(self._do_async_events()) if self._open else []
        if evs and self.safety is not None:
            try:
                self.safety.handle_async_events([e for e in evs if e.kind in ("ref_unlock", "lo_unlock")])
            except Exception as exc:  # noqa: BLE001
                logger.error("safety.handle_async_events failed: %r", exc)
        return evs

    # --- TX ------------------------------------------------------------------------------------
    def _safety_enabled(self) -> bool:
        s = self.safety
        if s is None:
            return False
        st = getattr(s, "state", None)
        return getattr(st, "value", st) != "disabled"

    def _host_tx_samples(self, permit) -> np.ndarray:
        x = np.array(permit.samples, dtype=np.complex64, copy=True)
        for k, c in enumerate(permit.chains):
            if self._iq_conj.get(self._tx_refs[int(c)].device):
                x[k] = np.conj(x[k])
        return x

    def _tx_rf_problem(self, permit):
        """``(code, detail)`` when the device TX state (rate, tuned centre, LO offset)
        differs from what ``permit`` was authorized for, else None. Read under ``_lock``."""
        fs = self.rate("tx")
        try:
            pfs = float(permit.fs_hz)
        except (AttributeError, TypeError, ValueError):
            pfs = float("nan")
        if not (math.isfinite(pfs) and abs(fs - pfs) <= TX_RATE_RTOL * max(abs(fs), 1.0)):
            return "SHAPE", "device TX rate %.9g S/s != permit fs %.9g S/s" % (fs, pfs)
        if "tx" not in self._tuned_dirs:
            return "BACKEND", "TX is not tuned (phase %s)" % self.phase
        fc = self.tuned_freq("tx")
        try:
            pfc = float(permit.fc_hz)
        except (AttributeError, TypeError, ValueError):
            pfc = float("nan")
        if not (math.isfinite(pfc) and abs(fc - pfc) <= TX_FREQ_TOL_HZ):
            return "FREQ_CHANGED", "tuned TX %.6f MHz != permit fc %.6f MHz" % (fc / 1e6, pfc / 1e6)
        pol = getattr(self.safety, "policy", None)
        lo_pol = getattr(pol, "lo_offset_hz", None)
        if lo_pol is not None:
            lo = self.tuned_lo_offset_hz("tx")
            if not abs(lo - float(lo_pol)) <= TX_FREQ_TOL_HZ:
                return "FREQ_CHANGED", ("TX LO offset %.6f MHz != authorized %.6f MHz (TX LO at %.6f MHz)"
                                        % (lo / 1e6, float(lo_pol) / 1e6, (fc + lo) / 1e6))
        return None

    def _tx_cmd_time_problem(self, permit):
        """``("SEQUENCE", detail)`` while a command time is set on a device of ``permit``
        (UHD would queue the TX gain change for that time: the burst would go out at the
        previous gain while the readback already reports the new one). Under ``_lock``."""
        devs = sorted({self._tx_refs[int(c)].device for c in permit.chains})
        held = [d for d in devs if self._cmd_time_set.get(d) is not None]
        if held:
            return "SEQUENCE", ("a command time is set on %s (timed command window open): the TX gain "
                                "would not apply before the burst" % ", ".join(held))
        return None

    def _tx_min_lead_s(self, safety, devs) -> float:
        pol = getattr(safety, "policy", None)
        lead = max(float(getattr(pol, "min_start_lead_s", 0.0) or 0.0),
                   float(getattr(pol, "min_tx_lead_s", 0.0) or 0.0))
        for d in devs:
            c = (self._bcaps or {}).get(d) or self.caps.get(d)
            lead = max(lead, float(getattr(c, "min_tx_lead_s", 0.0) or 0.0))
        return lead

    def _tx_start_problem(self, safety, permit):
        """``(code, detail, trip)`` when the device clock no longer agrees with the start
        the permit was authorized for (read under ``_lock`` right before the send).

        Every device of the permit: ``start - now`` must be >= the minimum TX lead
        (else a late refusal), and - on the controller's clock - the burst must still
        start within ``TX_START_TOL_S`` of the authorized start and end before the arm
        expiry (else the device time moved after the authorization: trip)."""
        start = getattr(permit, "start", None)
        if start is None:
            return None
        devs = sorted({self._tx_refs[int(c)].device for c in permit.chains})
        clock = getattr(safety, "_clock", None)
        try:
            mono = float(clock()) if callable(clock) else None
        except Exception:  # noqa: BLE001
            mono = None
        start_mono = None
        inflight = getattr(safety, "_inflight", None)
        if inflight is not None and getattr(inflight, "permit", None) is permit:
            start_mono = getattr(inflight, "duty_at", None)
        expiry = getattr(safety, "_expiry_mono", None)
        dur = float(getattr(permit, "duration_s", 0.0) or 0.0)
        min_lead = self._tx_min_lead_s(safety, devs)
        for d in devs:
            try:
                lead = float(start.diff_seconds(self._do_time_now(d)))
            except Exception as exc:  # noqa: BLE001
                return "START_TIME", "cannot read the %s device time: %r" % (d, exc), True
            if not math.isfinite(lead):
                return "START_TIME", "%s: non-finite start lead" % d, True
            if mono is not None and start_mono is not None and abs(mono + lead - float(start_mono)) > TX_START_TOL_S:
                return ("START_TIME", "%s device time moved after the authorization: the burst would start "
                        "%.6f s from now, authorized for %.6f s" % (d, lead, float(start_mono) - mono), True)
            if mono is not None and expiry is not None and mono + max(lead, 0.0) + dur > float(expiry) + 1e-9:
                return "START_TIME", "%s: the burst would end after the arm expiry" % d, True
            if lead < min_lead - 1e-12:
                return "START_TIME", "start only %.6f s ahead of %s (need >= %.6f s)" % (lead, d, min_lead), False
        return None

    @staticmethod
    def _tx_interruption(safety, gen0):
        """The exception to raise when a trip / shutdown / disarm landed since ``consume``
        (trip generation moved, or the controller is no longer TRANSMITTING), else None."""
        gen = getattr(safety, "_trip_gen", None)
        st = getattr(safety, "state", None)
        st = getattr(st, "value", st)
        if (gen0 is None or gen == gen0) and st == "transmitting":
            return None
        if st == "fault":
            return InterlockTrip(getattr(safety, "fault_code", "") or "FAULT",
                                 getattr(safety, "fault_detail", "") or "tripped during tx_burst")
        return SafetyRefusal("SEQUENCE", "TX state changed during tx_burst (state %s)" % st)

    def tx_burst(self, permit, timeout_s: float = 1.0) -> TxResult:
        """The ONLY transmit path (spec 4.12):

        1) refuse ``TX_DISABLED`` without an enabled safety controller; refuse when the
           device TX rate / tuned centre / LO offset differ from the permit;
        2) ``safety.consume(permit)`` (forged / reused / tampered permits trip) - with
           NO backend lock held (lock order: safety -> backend);
        3) under ``_lock``: re-check that no trip / disarm landed (trip generation,
           TRANSMITTING), the RF state and the device clock against the authorized
           start (a moved device time trips ``START_TIME``); apply
           ``permit.tx_gains_db`` and refuse ``GAIN`` when the device coerced a gain
           above the permit; re-check the trip generation last;
        4) timed start-of-burst at ``permit.start``, send ``permit.samples`` (conjugated
           per ``iq_conjugate``), zero-length end-of-burst - WITHOUT ``_lock``, so a
           trip on another thread reaches the PA lines at once;
        5) a trip during the send: abort, TX gain min, raise it;
        6) async events -> ``TxResult``; ``safety.burst_complete(permit, result)``.
        """
        if not self._safety_enabled():
            raise SafetyRefusal("TX_DISABLED", "no enabled TX safety controller attached to the backend")
        safety = self.safety
        with self._tx_lock:
            with self._lock:
                if not self._open:
                    raise SafetyRefusal("BACKEND", "tx_burst: backend is not open")
                if not self._refs_ok() or not self._tuned:
                    raise SafetyRefusal("BACKEND", "tx_burst: backend phase %s (needs TUNED)" % self.phase)
                if self.time_required() and not self._time_ok():
                    raise SafetyRefusal("BACKEND", "tx_burst: device time not aligned")
                chains = getattr(permit, "chains", None)
                try:
                    bad = [int(c) for c in chains if not 0 <= int(c) < len(self._tx_refs)]
                except Exception:  # noqa: BLE001
                    bad = ["?"]
                if chains is None or bad:
                    raise SafetyRefusal("SHAPE", "tx_burst: permit chains %r are not configured TX chains"
                                        % (chains,))
                start = getattr(permit, "start", None)
                if start is None and not self.is_simulation:
                    raise SafetyRefusal("START_TIME", "tx_burst: a hardware burst needs a timed start")
                if start is not None and not isinstance(start, DevTime):
                    raise SafetyRefusal("START_TIME", "tx_burst: permit start must be a DevTime")
                prob = self._tx_rf_problem(permit) or self._tx_cmd_time_problem(permit)
                if prob is not None:
                    raise SafetyRefusal(prob[0], "tx_burst: " + prob[1])
                self._trace("tx_burst", getattr(permit, "permit_id", None))
                if not self._tx_streamer:
                    self._do_create_tx_streamer()
                    self._tx_streamer = True
            gen0 = getattr(safety, "_trip_gen", None)
            safety.consume(permit)            # raises SafetyRefusal / InterlockTrip
            try:
                with self._lock:
                    stop = self._tx_interruption(safety, gen0)
                    if stop is not None:
                        raise _TxAbort("interrupted", stop)
                    prob = self._tx_rf_problem(permit) or self._tx_cmd_time_problem(permit)
                    if prob is not None:
                        raise _TxAbort("refuse", SafetyRefusal(prob[0], "tx_burst: " + prob[1]))
                    sp = self._tx_start_problem(safety, permit)
                    if sp is not None:
                        code, detail, trip = sp
                        raise _TxAbort("trip" if trip else "late",
                                       (InterlockTrip if trip else SafetyRefusal)(code, "tx_burst: " + detail))
                    gains = [float(g) for g in permit.tx_gains_db]
                    self._do_set_tx_gains(list(permit.chains), gains)
                    applied = self._do_tx_gain_readback(list(permit.chains))
                    if applied is not None:
                        for c, g, a in zip(permit.chains, gains, applied):
                            if a is not None and float(a) > g + TX_GAIN_TOL_DB:
                                raise _TxAbort("refuse", SafetyRefusal(
                                    "GAIN", "tx_burst: chain %d gain coerced to %.4f dB > authorized %.4f dB"
                                    % (int(c), float(a), g)))
                    samples = self._host_tx_samples(permit)
                    stop = self._tx_interruption(safety, gen0)        # the last check before the send
                    if stop is not None:
                        raise _TxAbort("interrupted", stop)
                result = self._do_tx_send(permit, samples, float(timeout_s))
            except _TxAbort as ab:
                self.tx_abort()
                self.set_tx_gain_min()
                if ab.kind == "trip":
                    try:
                        safety.trip(ab.exc.code, ab.exc.detail)
                    except Exception as exc2:  # noqa: BLE001
                        logger.critical("trip after a start-time mismatch failed: %r", exc2)
                elif ab.kind in ("refuse", "late"):
                    not_sent = TxResult(ok=False, sent_samples=0, late=1 if ab.kind == "late" else 0)
                    try:
                        safety.burst_complete(permit, not_sent)
                    except Exception as exc2:  # noqa: BLE001
                        logger.error("burst_complete after a refusal: %r", exc2)
                raise ab.exc from None
            except BaseException as exc:
                self.tx_abort()
                self.set_tx_gain_min()
                failed = TxResult(ok=False, sent_samples=0, events=[AsyncEvent("time_error", detail=repr(exc))])
                try:
                    safety.burst_complete(permit, failed)
                except Exception as exc2:  # noqa: BLE001
                    logger.error("burst_complete after a send failure: %r", exc2)
                try:
                    safety.trip("EXCEPTION", "TX send failed: %r" % (exc,))
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(exc, (ac.ActiveError, KeyboardInterrupt, SystemExit)):
                    raise
                raise HardwareFault("TX send failed: %r" % (exc,)) from exc
            stop = self._tx_interruption(safety, gen0)
            if stop is not None:
                # a trip / disarm landed while the burst was in flight: its shutdown already
                # drove the PA off; make sure nothing re-applied gain afterwards
                self.tx_abort()
                self.set_tx_gain_min()
                result.ok = False
                try:
                    safety.burst_complete(permit, result)
                except Exception as exc2:  # noqa: BLE001
                    logger.error("burst_complete after an interrupted send: %r", exc2)
                raise stop
            hang = (not result.acked) and result.late == 0 and result.sent_samples > 0
            if hang:
                self.tx_abort()
            try:
                safety.burst_complete(permit, result)
            finally:
                if hang:
                    try:
                        safety.trip("TX_HANG", "burst %s not acknowledged within %.3f s"
                                    % (getattr(permit, "permit_id", "?"), float(timeout_s)))
                    except Exception:  # noqa: BLE001
                        pass
            return result

    def tx_abort(self) -> None:
        """Zero-length end-of-burst on every TX streamer. Never raises."""
        try:
            self._trace("tx_abort")
            self._do_tx_abort()
        except Exception as exc:  # noqa: BLE001
            logger.critical("tx_abort failed: %r", exc)

    def _pa_lines(self) -> list:
        if self._pa_lines_cache is None:
            lines = []
            try:
                import active_rf_control as arf   # lazy: only the line parser is used
                spec = self.cfg.rf_control.pa_enable_line
                for ln in arf.parse_line_list(spec, max(1, len(self._tx_refs))):
                    if ln.kind in ("gpio", "sim") and ln not in lines:
                        lines.append(ln)
            except Exception as exc:  # noqa: BLE001
                logger.error("cannot parse pa_enable_line: %r", exc)
            self._pa_lines_cache = lines
        return self._pa_lines_cache

    def _pa_lines_off(self) -> list:
        steps = []
        for ln in self._pa_lines():
            try:
                self._do_gpio_write(ln, False)
                steps.append(("pa_off:%s" % ln, True, ""))
            except Exception as exc:  # noqa: BLE001
                logger.critical("PA line %s off failed: %r", ln, exc)
                steps.append(("pa_off:%s" % ln, False, repr(exc)))
        return steps

    def safe_state(self) -> list:
        """tx_abort, TX gain min, every PA line off. Never raises; ``[(step, ok, error)]``."""
        steps = []
        try:
            self._trace("safe_state")
        except Exception:  # noqa: BLE001
            pass
        try:
            self._do_tx_abort()
            steps.append(("tx_abort", True, ""))
        except Exception as exc:  # noqa: BLE001
            steps.append(("tx_abort", False, repr(exc)))
        try:
            self._do_set_tx_gain_min()
            steps.append(("tx_gain_min", True, ""))
        except Exception as exc:  # noqa: BLE001
            steps.append(("tx_gain_min", False, repr(exc)))
        if self._open:
            try:
                steps.extend(self._pa_lines_off())
            except Exception as exc:  # noqa: BLE001  (defensive: _pa_lines_off never raises)
                steps.append(("pa_off", False, repr(exc)))
        else:
            steps.append(("pa_off", True, "skipped: backend closed"))
        for name, ok, err in steps:
            if not ok:
                logger.critical("safe_state step %s failed: %s", name, err)
        return steps

    # --- hooks (concrete backends) ------------------------------------------------------------
    @abc.abstractmethod
    def _do_open(self) -> None: ...

    @abc.abstractmethod
    def _do_close(self) -> None: ...

    @abc.abstractmethod
    def _do_set_clock_source(self, src, device) -> None: ...

    @abc.abstractmethod
    def _do_set_time_source(self, src, device) -> None: ...

    @abc.abstractmethod
    def _do_ref_locked(self, device): ...

    @abc.abstractmethod
    def _do_set_master_clock_rate(self, hz, device) -> None: ...

    @abc.abstractmethod
    def _do_time_now(self, device) -> DevTime: ...

    @abc.abstractmethod
    def _do_time_last_pps(self, device) -> DevTime: ...

    @abc.abstractmethod
    def _do_set_time_unknown_pps(self, t, group) -> None: ...

    @abc.abstractmethod
    def _do_set_time_next_pps(self, t, device) -> None: ...

    def _do_set_time_now(self, t, device) -> None:
        raise NotSupported("%s: set_time_now is not supported" % self.name)

    @abc.abstractmethod
    def _do_time_synchronized(self) -> bool: ...

    @abc.abstractmethod
    def _do_set_command_time(self, t, device) -> None: ...

    @abc.abstractmethod
    def _do_clear_command_time(self, device) -> None: ...

    @abc.abstractmethod
    def _do_set_rate(self, direction, hz) -> float: ...

    @abc.abstractmethod
    def _do_tune(self, direction, freq_hz, lo_offset_hz, at) -> TuneReport: ...

    @abc.abstractmethod
    def _do_set_rx_gain(self, gains_db) -> list: ...

    @abc.abstractmethod
    def _do_rx_gain(self) -> list: ...

    @abc.abstractmethod
    def _do_set_tx_gains(self, chains, gains_db) -> None: ...

    @abc.abstractmethod
    def _do_set_tx_gain_min(self) -> None: ...

    def _do_tx_gain_readback(self, chains):
        """The TX gain (dB) the device really applied per chain (after coercion to its
        gain steps), or None when the device cannot report it."""
        return None

    def _do_lo_names(self, direction, ref) -> list:
        return []

    def _do_set_lo_source(self, direction, src, ref, name) -> None:
        raise NotSupported("%s: LO source selection is not supported" % self.name)

    def _do_set_lo_export(self, direction, enabled, ref, name) -> None:
        raise NotSupported("%s: LO export is not supported" % self.name)

    def _do_set_integer_delay(self, direction, ref, samples) -> bool:
        return False

    def _do_sensors(self, device) -> dict:
        return {}

    @abc.abstractmethod
    def _do_gpio_write(self, line, value) -> None: ...

    @abc.abstractmethod
    def _do_gpio_read(self, line) -> bool: ...

    def _do_gpio_atr(self, device, bank, ctrl_mask, idle, rx, tx, duplex) -> bool:
        return False

    @abc.abstractmethod
    def _do_rx_capture(self, nsamps, start) -> RxCapture: ...

    @abc.abstractmethod
    def _do_rx_start(self, start) -> None: ...

    @abc.abstractmethod
    def _do_rx_read(self, nsamps, timeout_s) -> RxCapture: ...

    @abc.abstractmethod
    def _do_rx_stop(self) -> None: ...

    @abc.abstractmethod
    def _do_async_events(self) -> list: ...

    @abc.abstractmethod
    def _do_create_tx_streamer(self) -> None: ...

    @abc.abstractmethod
    def _do_tx_send(self, permit, samples, timeout_s) -> TxResult: ...

    @abc.abstractmethod
    def _do_tx_abort(self) -> None: ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def _import_backend_class(backend: str):
    mod_name, cls_name, hint = BACKEND_MODULES[backend]
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as exc:
        raise BackendUnavailable("backend %s is not available (%s)" % (backend, exc), hint) from exc
    try:
        return getattr(mod, cls_name)
    except AttributeError as exc:
        raise BackendUnavailable("backend module %s has no %s" % (mod_name, cls_name), hint) from exc


def make_backend(cfg, event_log=None, **kw) -> ActiveBackend:
    """Build the configured backend (spec 1.5 / 4.12).

    ``validate_device_plan`` errors raise ``ConfigError``. Hardware libraries are
    imported lazily by the backend constructors; a missing one raises
    ``BackendUnavailable`` (with an install hint) unless ``[active]
    allow_sim_fallback = 1``, in which case the simulator replaces it: logged at
    WARNING, ``EVT_ACTIVE_FALLBACK`` emitted, ``hardware_id`` forced to ``SIM-...``
    and the status marked ``simulation: true``. A fallback is refused
    (``ConfigError``) when TX is enabled on a hardware configuration.
    """
    issues = [i for i in adev.validate_device_plan(cfg) if i.level == "error"]
    if issues:
        raise ConfigError("device plan invalid:\n" + ac.format_issues(issues), tuple(issues))
    backend = cfg.active.backend
    if backend == "sim":
        import active_backend_sim as asim
        return asim.SimBackend(cfg, event_log, **kw)
    if backend not in BACKEND_MODULES:
        raise ConfigError("unknown backend %r" % backend,
                          (Issue("error", "ENUM", "unknown backend %r" % backend, "[active] backend"),))
    try:
        cls = _import_backend_class(backend)
        return cls(cfg, event_log, **kw)
    except BackendUnavailable as exc:
        if not cfg.active.allow_sim_fallback:
            raise
        if cfg.tx_enabled():
            raise ConfigError("backend %s unavailable (%s) and en_tx = 1: a simulation fallback is "
                              "refused for a transmit-enabled hardware configuration" % (backend, exc),
                              (Issue("error", "BACKEND", "sim fallback refused with en_tx = 1",
                                     "[active] allow_sim_fallback"),)) from exc
        logger.warning("backend %s unavailable (%s; %s): FALLING BACK TO THE SIMULATOR",
                       backend, exc, getattr(exc, "hint", ""))
        import active_backend_sim as asim
        sim_kw = {k: v for k, v in kw.items() if k in ("geometry", "frontend", "seed", "flavour")}
        be = asim.SimBackend(cfg, event_log, **sim_kw)
        be.fallback = True
        be.fallback_from = backend
        be._emit(EVT_ACTIVE_FALLBACK, "warning", requested=backend, reason=str(exc),
                 hint=getattr(exc, "hint", ""), hardware_id=be.hardware_id)
        return be
