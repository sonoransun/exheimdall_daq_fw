"""
    HeIMDALL "active" capability: timing, coherence plan and verification (spec 8).

      - ``plan_coherence(cfg)``: pure plan of a configuration - device groups,
        clock / time sources, the resolved sync method, timed tune, the LO plan,
        the phase behaviour after a retune and the coherence class
        (INCOHERENT / TIME_ALIGNED / SHARED_REF / SHARED_LO), with stable
        ``COHERENCE_*`` issue codes;
      - ``SyncProcedure``: trip an armed TX (``REF_UNLOCK``, spec 8.3) -> safe state ->
        references -> reference lock -> master clock rate ->
        LO sharing (``apply_lo_plan``) -> time alignment (none / unknown_pps / next_pps_host / gps_time) ->
        verification on two PPS edges -> epoch. TX is never live during sync;
      - ``retune``: disarms TX first, checks the new TX intervals against the
        frequency policy (a denied TX LO stays untuned), tunes every channel
        inside ONE command window when timed tune is available, waits for LO
        lock, applies the generation rule once and restarts RX at a common t0;
      - ``verify_coherence``: phase stability across timed injection captures
        (detects an unshared reference as a ramp - measured inside each capture as
        well, so a frequency offset at a multiple of 1/gap cannot alias to zero -,
        a PLL relock as a jump, and an LO share / repeatable tune that does not
        hold as a phase change across a same-frequency retune);
      - the coherence error budget (skew, position, calibration, drift, jitter,
        amplitude) and its report against the DoA / null verdicts.

    All scheduling uses exact ``DevTime`` arithmetic; waits go through
    ``backend.sleep`` (virtual on the simulator), never the wall clock.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import contextlib
import enum
import logging
import math
import time
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

import active_common as ac
from active_common import (C0, EVT_ACTIVE_BAND_CHANGE, EVT_ACTIVE_COHERENCE_FAIL, EVT_ACTIVE_SYNC_FAIL,
                           EVT_ACTIVE_SYNC_OK, EVT_ACTIVE_TUNE, BackendError, ChannelRef, CoherenceError,
                           ConfigError, DevTime, HardwareFault, Issue, NotSupported)
import active_devices as adev
import active_dsp as adsp
from active_backend import STABLE_PHASE_STATES, TuneReport

logger = logging.getLogger("heimdall.active.timing")

PPS_POLL_S = 0.05
PPS_TIMEOUT_S = 1.5
PPS_WINDOW_S = 0.5
PPS_RETRIES = 3
REF_POLL_S = 0.1
POST_PPS_SLEEP_S = 1.1
LO_LOCK_POLL_S = 0.01
LO_LOCK_TIMEOUT_S = 1.0
JUMP_FACTOR = 5.0
MAX_ALIGN_LAG = 16
DOA_VERDICT_DEG = 5.0
NULL_VERDICT_DEG = 2.0
ASSUMED_CAL_SNR_DB = 20.0
SYNC_METHODS = ("none", "unknown_pps", "next_pps_host", "gps_time")
# Families whose multi-device coherence is unproven but allowed (spec 0.3 D7): a warning only.
MULTI_DEVICE_VERIFY_FAMILIES = ("b210",)


class CoherenceClass(enum.IntEnum):
    INCOHERENT = 0
    TIME_ALIGNED = 1
    SHARED_REF = 2
    SHARED_LO = 3


@dataclass
class LoPlan:
    mode: str
    exporter: ChannelRef = None
    importers: list = field(default_factory=list)       # [(ChannelRef, direction)]


@dataclass
class CoherencePlan:
    groups: list
    sources: dict                     # device -> {"clock": .., "time": ..}
    master: str
    sync_method: str
    timed_tune: bool
    lo_plan: LoPlan
    coherence_class: CoherenceClass
    phase_after_tune: str             # repeatable|random|ambiguous_180|shared_lo|deterministic
    recal_on_tune: bool
    command_lead_s: float
    stream_lead_s: float
    issues: list

    def errors(self) -> list:
        return [i for i in self.issues if i.level == "error"]

    def to_dict(self) -> dict:
        d = ac.to_jsonable({k: v for k, v in self.__dict__.items() if k != "groups"})
        d["groups"] = [{"name": g.name, "family": g.family, "devices": g.device_names(),
                        "compound": g.compound} for g in self.groups]
        d["coherence_class"] = self.coherence_class.name
        return d


def _issue(out, level, code, msg, where=""):
    out.append(Issue(level, code, msg, where))


def _lo_plan(cfg, mode) -> LoPlan:
    if mode in ("none", "auto"):
        return LoPlan("none")
    if mode in ("n321_star", "twinrx"):
        mem = adev.lo_share_members(cfg, mode)
        exp = mem["exporters"][0] if mem["exporters"] else None
        exporter = None
        if exp is not None:
            d = cfg.device(exp)
            ch = (list(d.rx_channels) or list(d.tx_channels) or [0])[0]
            exporter = ChannelRef(exp, int(ch))
        imps = []
        for name in mem["importers"]:
            d = cfg.device(name)
            imps += [(ChannelRef(name, int(c)), "rx") for c in d.rx_channels]
            if mode == "n321_star":
                imps += [(ChannelRef(name, int(c)), "tx") for c in d.tx_channels]
        return LoPlan(mode, exporter, imps)
    if mode == "external_lo":
        imps = []
        for d in cfg.devices:
            if d.lo_source == "external":
                imps += [(ChannelRef(d.name, int(c)), "rx") for c in d.rx_channels]
                imps += [(ChannelRef(d.name, int(c)), "tx") for c in d.tx_channels]
        return LoPlan(mode, None, imps)
    if mode == "pervices_common":
        m = cfg.master_device()
        ch = (list(m.rx_channels) or list(m.tx_channels) or [0])[0]
        imps = [(ChannelRef(d.name, int(c)), "rx") for d in cfg.devices for c in d.rx_channels]
        return LoPlan(mode, ChannelRef(m.name, int(ch)), imps)
    return LoPlan(mode)


def plan_coherence(cfg) -> CoherencePlan:
    """Pure coherence plan of ``cfg`` (spec 8.2). Issues carry stable ``COHERENCE_*`` codes.

    ``phase_after_tune`` / ``recal_on_tune`` describe the CONFIGURED centre frequency
    only; every real tune is re-evaluated by the backend for the frequency it tuned and
    the LO share it programmed (``active_backend.phase_after_tune_at``), and that per-tune
    state drives the generation rule (an N321 star below 450 MHz is random)."""
    issues = []
    t = cfg.timing
    if not cfg.devices:
        _issue(issues, "error", "COHERENCE_DEVICES", "no devices configured", "[active_devices]")
        return CoherencePlan([], {}, "", "none", False, LoPlan("none"), CoherenceClass.INCOHERENT,
                             "random", True, t.command_lead_s, t.stream_lead_s, issues)
    groups = adev.device_groups(cfg)
    master = cfg.master_device().name
    sources = {}
    for d in cfg.devices:
        clk, tim = adev.effective_sources(cfg, d)
        sources[d.name] = {"clock": clk, "time": tim}
    bcaps = {d.name: adev.behaviour_caps(cfg, d) for d in cfg.devices}
    n_dev = len(cfg.devices)

    # 2. sync method
    if t.sync_method == "pc_clock":
        _issue(issues, "error", "COHERENCE_PC_CLOCK", "sync_method pc_clock cannot align device time "
               "(use unknown_pps, next_pps_host or gps_time)", "[active_timing] sync_method")
        sync = "none"
    else:
        sync = adev.resolve_sync_method(cfg)
    if sync == "none" and (len(groups) > 1 or any(g.compound for g in groups)):
        _issue(issues, "error", "COHERENCE_SYNC", "sync_method none with %d devices: their time cannot be "
               "aligned" % n_dev, "[active_timing] sync_method")
    if sync == "unknown_pps" and len(groups) > 1:
        _issue(issues, "error", "COHERENCE_SYNC", "unknown_pps aligns one compound MultiUSRP only; %d groups "
               "need next_pps_host (or gps_time)" % len(groups), "[active_timing] sync_method")
    if sync == "gps_time" and not all(s["time"] == "gpsdo" for s in sources.values()):
        _issue(issues, "error", "COHERENCE_GPS_TIME", "gps_time needs time_source gpsdo on every device",
               "[active_timing] sync_method")

    # 1. shared reference and PPS across mboards
    shared_ref = True
    if n_dev > 1:
        for name, s in sources.items():
            if s["clock"] == "internal" or s["time"] == "internal":
                shared_ref = False
                _issue(issues, "error", "COHERENCE_NO_SHARED_REF", "%d devices need a shared 10 MHz and PPS; "
                       "%s uses clock %s / time %s" % (n_dev, name, s["clock"], s["time"]),
                       "[active_device:%s]" % name)
    separate_gpsdo = n_dev > 1 and any(s["clock"] == "gpsdo" for s in sources.values())

    # 3. timed tune
    timed = adev.resolve_timed_tune(cfg)
    pervices = [n for n, c in bcaps.items() if c.vendor == "pervices"]
    if t.timed_tune == "on":
        bad = [n for n, c in bcaps.items() if not c.timed_tune]
        if bad:
            _issue(issues, "error", "COHERENCE_TIMED_TUNE", "timed_tune = on but %s cannot tune at a timed "
                   "command" % ", ".join(bad), "[active_timing] timed_tune")
    if pervices:
        timed = False

    # 4. phase after tune
    pat = adev.phase_after_tune(cfg)
    recal = not (pat in STABLE_PHASE_STATES and not cfg.calibration.recal_on_tune)

    # 5. LO plan
    mode = adev.resolve_lo_plan(cfg)
    lo = _lo_plan(cfg, mode)
    if mode in ("n321_star", "twinrx"):
        mem = adev.lo_share_members(cfg, mode)
        needs = mode == "n321_star" or any(cfg.device(n).lo_source == "external" for n in mem["importers"])
        if needs and len(mem["exporters"]) != 1:
            _issue(issues, "error", "COHERENCE_LO", "lo_plan %s needs exactly one LO exporter, got %d (%s)"
                   % (mode, len(mem["exporters"]), ", ".join(mem["exporters"]) or "none"),
                   "[active_timing] lo_plan")
        for name in mem["importers"]:
            if not bcaps[name].lo_import:
                _issue(issues, "error", "COHERENCE_LO", "%s (%s) cannot import an LO"
                       % (name, bcaps[name].family), "[active_device:%s] lo_source" % name)
        if mode == "n321_star" and n_dev > 16:
            _issue(issues, "error", "COHERENCE_LO", "n321_star distributes to at most 16 devices", "[active_devices]")
        if not mem["eligible"]:
            _issue(issues, "error", "COHERENCE_LO", "lo_plan %s but no device can take part" % mode,
                   "[active_timing] lo_plan")
    elif mode == "pervices_common":
        _issue(issues, "warning", "COHERENCE_PV_LO", "pervices_common: software selectability of the common "
               "LO is [verify]; calibrate after every tune", "[active_timing] lo_plan")
    if mode in ("n321_star", "twinrx") and t.lo_master:
        if t.lo_master not in cfg.device_names():
            _issue(issues, "error", "COHERENCE_LO", "lo_master %r is not a configured device" % t.lo_master,
                   "[active_timing] lo_master")

    # 6. Per Vices
    if pervices:
        if n_dev > 1:
            _issue(issues, "warning", "COHERENCE_PV_ALIGN", "Per Vices: the vendor alignment script fixes the "
                   "integer sample delay only; the host calibration is mandatory after every tune",
                   "[active_devices]")
        if t.timed_tune == "on":
            _issue(issues, "error", "COHERENCE_TIMED_TUNE", "Per Vices tunes are always untimed",
                   "[active_timing] timed_tune")

    # multi-device coherence capability
    fams = {c.family for c in bcaps.values() if not c.multi_device_coherent}
    verify_only = sorted(fams & set(MULTI_DEVICE_VERIFY_FAMILIES))
    not_coh = sorted(fams - set(MULTI_DEVICE_VERIFY_FAMILIES))
    if n_dev > 1 and not_coh:
        coherent_use = cfg.output.output_mode != "raw" or cfg.tx_enabled()
        _issue(issues, "error" if coherent_use else "warning", "COHERENCE_MULTI_DEVICE",
               "%s %s single-device coherent only: %d devices cannot be phase coherent"
               % (", ".join(not_coh), "is" if len(not_coh) == 1 else "are", n_dev), "[active_devices]")
    if n_dev > 1 and verify_only:
        _issue(issues, "warning", "COHERENCE_MULTI_DEVICE",
               "several %s: separate groups on a shared 10 MHz + PPS, aligned by host next_pps; "
               "coherent only after calibration, recalibrated after every tune [verify]"
               % ", ".join(verify_only), "[active_devices]")

    # leads and reference lock
    min_lead = max([float(c.min_tx_lead_s) for c in bcaps.values()] or [0.0])
    if t.stream_lead_s < t.command_lead_s + 0.05:
        _issue(issues, "error", "COHERENCE_LEAD", "stream_lead_s %.3g must be >= command_lead_s + 0.05"
               % t.stream_lead_s, "[active_timing] stream_lead_s")
    if t.stream_lead_s < min_lead:
        _issue(issues, "error", "COHERENCE_LEAD", "stream_lead_s %.3g below the device TX lead %.3g s"
               % (t.stream_lead_s, min_lead), "[active_timing] stream_lead_s")
    if not t.require_ref_lock:
        _issue(issues, "warning", "COHERENCE_REF_LOCK", "require_ref_lock = 0: an unlocked reference is "
               "not fatal (beamformed TX is refused)", "[active_timing] require_ref_lock")

    # 7. class
    if n_dev == 1:
        kind = bcaps[cfg.devices[0].name].lo_share_kind
        if kind in ("intra_device", "pervices_common") or pat == "shared_lo":
            cls = CoherenceClass.SHARED_LO
        else:
            cls = CoherenceClass.SHARED_REF
    elif sync == "none" or not shared_ref:
        cls = CoherenceClass.INCOHERENT
    elif separate_gpsdo or not_coh:
        cls = CoherenceClass.TIME_ALIGNED
    elif pat == "shared_lo":
        cls = CoherenceClass.SHARED_LO
    else:
        cls = CoherenceClass.SHARED_REF
    if cls == CoherenceClass.TIME_ALIGNED:
        _issue(issues, "warning", "COHERENCE_CLASS", "coherence class TIME_ALIGNED (%s): coherent element / "
               "beam output stays below sync_state 6 and beamformed TX is refused"
               % ("separate GPSDO references" if separate_gpsdo else "devices not phase coherent"),
               "[active_timing]")
    elif cls == CoherenceClass.INCOHERENT and n_dev > 1:
        _issue(issues, "warning", "COHERENCE_CLASS", "coherence class INCOHERENT: single-channel use only",
               "[active_timing]")
    return CoherencePlan(groups=groups, sources=sources, master=master, sync_method=sync, timed_tune=bool(timed),
                         lo_plan=lo, coherence_class=cls, phase_after_tune=pat, recal_on_tune=bool(recal),
                         command_lead_s=float(t.command_lead_s), stream_lead_s=float(t.stream_lead_s),
                         issues=issues)


def apply_lo_plan(backend, plan) -> list:
    """Program the LO sharing of ``plan.lo_plan`` on the backend -> ``[(call, ref, direction)]``.

    Exporters enable their LO export, importers select the shared source
    (``external`` for n321_star / external_lo, the configured ``companion`` /
    ``external`` for twinrx). A channel whose device reports no LO names
    (``lo_names == []``) is a ``ConfigError`` (spec 8.2 rule 5). ``pervices_common``
    is not software-selectable [verify]: nothing is programmed. Idempotent.
    """
    lp = plan.lo_plan
    done = []
    if lp.mode not in ("n321_star", "twinrx", "external_lo"):
        return done
    cfg = backend.cfg

    def names(direction, ref):
        n = backend.lo_names(direction, ref)
        if not n:
            raise ConfigError("lo_plan %s: %s reports no %s LO names (the device cannot share its LO)"
                              % (lp.mode, ref, direction.upper()),
                              (Issue("error", "COHERENCE_LO", "%s has no LO names" % ref, "[active_timing] lo_plan"),))
        return n
    if lp.exporter is not None:
        dirs = ("rx",) if lp.mode == "twinrx" else ("rx", "tx")
        for direction in dirs:
            d = cfg.device(lp.exporter.device)
            chans = d.rx_channels if direction == "rx" else d.tx_channels
            if not chans:
                continue
            ref = ChannelRef(d.name, int(chans[0]))
            names(direction, ref)
            backend.set_lo_export(direction, True, ref)
            done.append(("set_lo_export", ref, direction))
    for ref, direction in lp.importers:
        src = "external" if lp.mode != "twinrx" else (cfg.device(ref.device).lo_source or "external")
        names(direction, ref)
        backend.set_lo_source(direction, src, ref)
        done.append(("set_lo_source:%s" % src, ref, direction))
    return done


# ---------------------------------------------------------------------------
# Sync procedure
# ---------------------------------------------------------------------------
@dataclass
class SyncReport:
    ok: bool
    method: str
    steps: list
    ref_locked: dict
    pps_times: dict
    skew_ticks: int
    time_synchronized: bool
    epoch_offset_s: float
    issues: list

    def to_dict(self) -> dict:
        return ac.to_jsonable(self)


def _invalidate_sync(backend, reason) -> None:
    """A failed sync: the time loads it made (if any) are NOT an aligned device time.
    Drops the backend to REFS_SET (or below), clears the tune state and bumps, so
    streaming and TX refuse until a ``SyncProcedure`` succeeds. Never raises."""
    fn = getattr(backend, "invalidate_time", None)
    if not callable(fn):
        return
    try:
        fn(reason)
    except Exception as exc:  # noqa: BLE001  (the sync error is the one to report)
        logger.critical("SyncProcedure: invalidate_time(%s) failed: %r", reason, exc)


@contextlib.contextmanager
def _command_window(backend, t_cmd):
    """``backend.command_window`` (holds the TX lock across set / clear) when available,
    else set_command_time ... clear_command_time."""
    win = getattr(backend, "command_window", None)
    if callable(win):
        with win(t_cmd):
            yield
        return
    backend.set_command_time(t_cmd)
    try:
        yield
    finally:
        backend.clear_command_time()


def _ticks_between(a: DevTime, b: DevTime) -> int:
    """``a - b`` in ticks of the finer of the two tick rates (exact, rounded)."""
    rate = max(a.tick_rate, b.tick_rate)
    return int(round(a.diff_seconds(b) * rate))


class SyncProcedure:
    """Clock / time alignment of every device (spec 8.3). TX is never live during sync:
    an ARMED / TRANSMITTING safety controller (``safety``, default ``backend.safety``)
    is tripped (``REF_UNLOCK``) before anything is touched; a re-sync invalidates
    every calibration and every TX timing."""

    def __init__(self, backend, plan, timing_cfg, event_log=None, clock=time.monotonic, *,
                 rf_control=None, wallclock=None, safety=None):
        self.backend = backend
        self.plan = plan
        self.tcfg = timing_cfg
        self.event_log = event_log if event_log is not None else getattr(backend, "event_log", None)
        self._clock = clock
        self.rf = rf_control
        self.safety = safety if safety is not None else getattr(backend, "safety", None)
        if wallclock is None:
            wallclock = getattr(backend, "wallclock", None) or time.time
        self._wallclock = wallclock
        self.report = None
        self.epoch_offset_s = 0.0

    def _emit(self, etype, severity="info", **payload):
        if self.event_log is not None:
            try:
                self.event_log.emit(etype, severity, **payload)
            except Exception:
                pass

    def _devices_master_first(self) -> list:
        names = [d.name for g in self.plan.groups for d in g.devices]
        if self.plan.master in names:
            names.remove(self.plan.master)
            names.insert(0, self.plan.master)
        return names

    def _wait_edge(self, master, last) -> DevTime:
        """Poll the master's last-PPS time every 50 ms until it changes (1.5 s timeout)."""
        be = self.backend
        polls = int(math.ceil(PPS_TIMEOUT_S / PPS_POLL_S))
        for _ in range(polls):
            be.sleep(PPS_POLL_S)
            cur = be.time_last_pps(master)
            if cur != last:
                return cur
        raise CoherenceError("PPS_MISSING", "no PPS edge on %s within %.1f s" % (master, PPS_TIMEOUT_S))

    def run(self) -> SyncReport:
        """Run the procedure (inside ``backend.syncing()``: the time loads do not bump
        individually; the generation is bumped once, ``"sync"``, on success)."""
        # 0. a re-sync trips an armed TX (spec 8.3) - before the backend lock is involved
        s = self.safety
        if s is not None and _state_value(s) in ("armed", "transmitting"):
            try:
                s.trip("REF_UNLOCK", "re-sync (SyncProcedure) while TX is armed")
            except Exception as exc:  # noqa: BLE001  (trip never raises; defensive)
                logger.critical("SyncProcedure: trip failed: %r", exc)
        syncing = getattr(self.backend, "syncing", None)
        ctx = syncing() if callable(syncing) else contextlib.nullcontext()
        with ctx:
            return self._run()

    def _run(self) -> SyncReport:
        steps, issues = [], []
        be, plan, t = self.backend, self.plan, self.tcfg
        method = plan.sync_method
        ref_locked, pps_times = {}, {}
        skew = 0
        try:
            if method not in SYNC_METHODS:
                raise CoherenceError("SYNC_METHOD", "unsupported sync method %r" % method)
            hard = [i for i in plan.issues if i.level == "error"]
            if hard:
                raise CoherenceError(hard[0].code, "coherence plan invalid: %s" % hard[0].message)
            if self.safety is not None and _state_value(self.safety) in ("armed", "transmitting"):
                raise CoherenceError("TX_LIVE", "TX is still armed (the REF_UNLOCK trip did not take effect)")
            # 1. TX never live during sync
            be.safe_state()
            steps.append(("safe_state", ""))
            if self.rf is not None:
                self.rf.force_safe()
                steps.append(("rf_force_safe", ""))
            names = self._devices_master_first()
            # 2. references, master first
            for n in names:
                src = plan.sources[n]
                be.set_clock_source(src["clock"], device=n)
                be.set_time_source(src["time"], device=n)
            steps.append(("sources", {n: plan.sources[n] for n in names}))
            # 3. reference lock
            polls = int(math.ceil(float(t.ref_lock_timeout_s) / REF_POLL_S))
            pending = list(names)
            for _ in range(polls + 1):
                still = []
                for n in pending:
                    v = be.ref_locked(n)
                    ref_locked[n] = v
                    if v is False:
                        still.append(n)
                pending = still
                if not pending:
                    break
                be.sleep(REF_POLL_S)
            if pending:
                msg = "reference not locked on %s after %.1f s" % (", ".join(pending), t.ref_lock_timeout_s)
                if t.require_ref_lock:
                    raise CoherenceError("REF_LOCK", msg)
                issues.append(Issue("warning", "REF_LOCK", msg))
            for n in names:
                if ref_locked.get(n) is None:
                    issues.append(Issue("info", "REF_LOCK", "%s has no reference-lock sensor" % n))
            steps.append(("ref_lock", dict(ref_locked)))
            # 4. master clock rate (invalidates the time: always before the time is set)
            cfg = be.cfg
            for g in plan.groups:
                for d in g.devices:
                    mcr = adev.group_mcr_hz(g) if g.compound else adev.effective_mcr_hz(d, cfg)
                    if mcr and mcr > 0:
                        try:
                            be.set_master_clock_rate(mcr, device=d.name)
                        except NotSupported:
                            issues.append(Issue("info", "MCR", "%s: master clock rate managed by the device" % d.name))
            steps.append(("mcr", ""))
            # 4b. LO sharing (needs the shared reference; spec 8.2 rule 5)
            lo_calls = apply_lo_plan(be, plan)
            if lo_calls:
                steps.append(("lo_plan", [(c, str(r), d) for c, r, d in lo_calls]))
            # 5. time alignment
            master = names[0]
            if method == "unknown_pps":
                for g in plan.groups:
                    tick = be.time_now(g.devices[0].name).tick_rate
                    try:
                        be.set_time_unknown_pps(DevTime(0, 0, tick), group=g.name)
                    except HardwareFault as exc:
                        raise CoherenceError("PPS_MISSING", str(exc)) from exc
                steps.append(("time", "unknown_pps"))
            elif method in ("next_pps_host", "gps_time"):
                k = 0
                if method == "gps_time":
                    gt = be.sensors(master).get("gps_time")
                    if gt is None:
                        raise CoherenceError("GPS_TIME", "%s reports no gps_time sensor" % master)
                done = False
                for attempt in range(PPS_RETRIES):
                    last = be.time_last_pps(master)
                    edge = self._wait_edge(master, last)
                    if method == "gps_time":
                        k = int(be.sensors(master).get("gps_time")) + 1
                    if be.time_now(master).diff_seconds(edge) > Fraction(PPS_WINDOW_S):
                        continue
                    for n in names:
                        be.set_time_next_pps(DevTime(k, 0, edge.tick_rate), device=n)
                    if be.time_last_pps(master) == edge and \
                            be.time_now(master).diff_seconds(edge) <= Fraction(PPS_WINDOW_S):
                        done = True
                        break
                if not done:
                    raise CoherenceError("PPS_WINDOW", "could not set the time within %.1f s of a PPS edge "
                                         "(%d attempts)" % (PPS_WINDOW_S, PPS_RETRIES))
                be.sleep(POST_PPS_SLEEP_S)
                steps.append(("time", "%s k=%d" % (method, k)))
            else:
                steps.append(("time", "none"))
            # 6. verify
            if method != "none" and len(names) > 1:
                worst = 0
                last = be.time_last_pps(master)
                for _edge in range(2):
                    edge = self._wait_edge(master, last)
                    last = edge
                    times = {n: be.time_last_pps(n) for n in names}
                    pps_times = {n: v.to_dict() for n, v in times.items()}
                    for n in names[1:]:
                        d = _ticks_between(times[n], times[master])
                        if abs(d) > abs(worst):
                            worst = d
                        if abs(d) > int(t.max_pps_skew_ticks):
                            skew = abs(worst)
                            raise CoherenceError("PPS_SKEW", "%s last PPS %s vs master %s: %d ticks (max %d)"
                                                 % (n, times[n], times[master], d, t.max_pps_skew_ticks))
                skew = abs(worst)
            t1 = be.time_now(master)
            t2 = be.time_now(master)
            if t2 < t1:
                raise CoherenceError("TIME_MONOTONIC", "device time went backwards (%s -> %s)" % (t1, t2))
            tsync = True
            if any(g.compound for g in plan.groups):
                tsync = bool(be.time_synchronized())
                ettus = all(be.caps[d.name].vendor in ("ettus", "sim") for g in plan.groups for d in g.devices)
                if not tsync and ettus and method != "none":
                    raise CoherenceError("TIME_SYNC", "compound MultiUSRP reports its mboard times unsynchronized")
            steps.append(("verify", {"skew_ticks": skew, "time_synchronized": tsync}))
            # 7. epoch
            if t.epoch == "host":
                self.epoch_offset_s = float(self._wallclock()) - be.time_now(master).to_seconds()
            else:
                self.epoch_offset_s = 0.0
            steps.append(("epoch", self.epoch_offset_s))
        except CoherenceError as exc:
            _invalidate_sync(be, "sync_fail")
            self._emit(EVT_ACTIVE_SYNC_FAIL, "error", code=exc.code, detail=exc.detail, method=method)
            self.report = SyncReport(False, method, steps, ref_locked, pps_times, skew, False,
                                     self.epoch_offset_s, issues + [Issue("error", exc.code, exc.detail)])
            raise
        except (BackendError, HardwareFault) as exc:
            _invalidate_sync(be, "sync_fail")
            self._emit(EVT_ACTIVE_SYNC_FAIL, "error", code="BACKEND", detail=repr(exc), method=method)
            self.report = SyncReport(False, method, steps, ref_locked, pps_times, skew, False,
                                     self.epoch_offset_s, issues + [Issue("error", "BACKEND", repr(exc))])
            raise CoherenceError("BACKEND", "sync failed: %r" % (exc,)) from exc
        except BaseException as exc:          # ConfigError (LO plan), SafetyRefusal, ...: re-raised as is
            _invalidate_sync(be, "sync_fail")
            self._emit(EVT_ACTIVE_SYNC_FAIL, "error", code=type(exc).__name__, detail=repr(exc), method=method)
            self.report = SyncReport(False, method, steps, ref_locked, pps_times, skew, False,
                                     self.epoch_offset_s, issues + [Issue("error", "SYNC", repr(exc))])
            raise
        gen = be.coherence.bump("sync")
        self._emit(EVT_ACTIVE_SYNC_OK, "info", method=method, skew_ticks=skew, generation=gen,
                   epoch_offset_s=self.epoch_offset_s)
        self.report = SyncReport(True, method, steps, ref_locked, pps_times, skew, tsync,
                                 self.epoch_offset_s, issues)
        return self.report

    def stream_start_time(self) -> DevTime:
        """Master ``time_now + stream_lead_s`` on the master tick grid (exact)."""
        now = self.backend.time_now(self.plan.master or None)
        return now.add_seconds(Fraction(self.tcfg.stream_lead_s).limit_denominator(10 ** 9))


# ---------------------------------------------------------------------------
# Retune
# ---------------------------------------------------------------------------
def _tx_band_issues(cfg, safety, freq_hz, lo_offset_hz) -> list:
    import active_regulatory as areg      # lazy: only the TX-direction check needs it
    pol = getattr(safety, "policy", None)
    allowed = tuple(getattr(pol, "allowed_modes", None) or cfg.tx.allowed_modes)
    allow = getattr(pol, "allow_bands", None)
    deny = getattr(pol, "deny_bands", None)
    allow = cfg.tx.allow_bands if allow is None else allow
    deny = cfg.tx.deny_bands if deny is None else deny
    ignore = bool(getattr(pol, "conducted_ignore_builtin_deny", cfg.tx.conducted_ignore_builtin_deny))
    fs = float(cfg.rf.sample_rate_hz)
    occ = cfg.tx.effective_occupied_bw_hz(fs)
    mode = "radiate" if ("radiate" in allowed or not allowed) else allowed[0]
    iv = areg.tx_frequency_intervals(freq_hz, occ, lo_offset_hz, fs)
    return areg.band_check(iv, mode, allow, deny, ignore_builtin=ignore and mode != "radiate")


def _state_value(safety):
    st = getattr(safety, "state", None)
    return getattr(st, "value", st)


def retune(backend, plan, freq_hz, safety=None, lo_offset_hz=0.0, direction="both", event_log=None) -> list:
    """Retune every channel (spec 8.4) -> ``[TuneReport]``.

    Disarms an armed TX first (``safety``, default ``backend.safety``); a
    TX-direction tune whose new TX intervals fail the frequency policy leaves TX
    untuned (its report has ``phase_state`` "untuned" and the issues). One command
    window covers every timed tune. The generation rule uses each tune's own phase
    state (evaluated for the frequency actually tuned and the LO share as
    programmed). Raises ``CoherenceError("LO_LOCK")`` when an LO does not lock.
    """
    if direction not in ("rx", "tx", "both"):
        raise ValueError("direction must be rx, tx or both")
    ev = event_log if event_log is not None else getattr(backend, "event_log", None)
    cfg = backend.cfg
    f = float(freq_hz)
    lo = float(lo_offset_hz)
    if safety is None:
        safety = getattr(backend, "safety", None)
    # 1. never retune under TX
    if safety is not None and _state_value(safety) in ("armed", "transmitting"):
        safety.disarm("retune")
    dirs = [d for d in ("rx", "tx") if direction in (d, "both")]
    if "tx" in dirs and not backend.tx_refs():
        dirs.remove("tx")
    if "rx" in dirs and not backend.rx_refs():
        dirs.remove("rx")
    reports = []
    # 3. TX intervals against the frequency policy
    tx_blocked = []
    if "tx" in dirs:
        tx_blocked = [i for i in _tx_band_issues(cfg, safety, f, lo) if i.level == "error"]
        if tx_blocked:
            dirs.remove("tx")
            logger.warning("retune: TX left untuned at %.6f MHz: %s", f / 1e6, tx_blocked[0].message)
            reports.append(TuneReport(direction="tx", refs=tuple(backend.tx_refs()), requested_hz=f,
                                      actual_rf_hz=[], actual_dsp_hz=[], timed=False, phase_state="untuned",
                                      lo_locked=[], generation=backend.coherence.generation, band_change=False,
                                      issues=list(tx_blocked)))
    gen0 = backend.coherence.generation
    # 4. tune (one command window when timed)
    tuned = []
    if dirs and plan.timed_tune:
        t_cmd = backend.time_now(plan.master or None).add_seconds(
            Fraction(plan.command_lead_s).limit_denominator(10 ** 9))
        with _command_window(backend, t_cmd):           # every device of every group, TX locked out
            for d in dirs:
                tuned.append(backend.tune(d, f, lo, at=t_cmd, bump=False))
        backend.sleep(plan.command_lead_s + 0.01)
    else:
        for d in dirs:
            tuned.append(backend.tune(d, f, lo, at=None, bump=False))
    reports.extend(tuned)
    # 5. LO lock
    if tuned and not all(all(r.lo_locked) for r in tuned):
        devs = sorted({ref.device for r in tuned for ref, ok in zip(r.refs, r.lo_locked) if not ok})
        for _ in range(int(math.ceil(LO_LOCK_TIMEOUT_S / LO_LOCK_POLL_S))):
            backend.sleep(LO_LOCK_POLL_S)
            devs = [n for n in devs if not backend.sensors(n).get("lo_locked", True)]
            if not devs:
                break
        if devs:
            if ev is not None:
                ev.emit(EVT_ACTIVE_COHERENCE_FAIL, "error", code="LO_LOCK", devices=devs, freq_hz=f)
            raise CoherenceError("LO_LOCK", "LO not locked on %s after %.1f s" % (", ".join(devs), LO_LOCK_TIMEOUT_S))
    # 6. generation (once per retune)
    band = any(r.band_change for r in tuned)
    need = any(backend.tune_needs_bump(r) for r in tuned)
    if (need or band) and backend.coherence.generation == gen0:
        backend.coherence.bump("band_change" if band else "retune")
    gen = backend.coherence.generation
    for r in tuned:
        r.generation = gen
    if ev is not None:
        try:
            ev.emit(EVT_ACTIVE_TUNE, "info", freq_hz=f, lo_offset_hz=lo, directions=[r.direction for r in tuned],
                    timed=all(r.timed for r in tuned) if tuned else False, generation=gen,
                    phase_state=[r.phase_state for r in tuned], tx_untuned=bool(tx_blocked))
            if band:
                ev.emit(EVT_ACTIVE_BAND_CHANGE, "warning", freq_hz=f, generation=gen)
        except Exception:
            pass
    # 7. restart RX with a common timed t0 (resets the DSP CORDIC phase deterministically)
    if getattr(backend, "phase", "") == "STREAMING" and any(r.direction == "rx" for r in tuned):
        backend.rx_stop()
        t0 = backend.time_now(plan.master or None).add_seconds(
            Fraction(plan.stream_lead_s).limit_denominator(10 ** 9))
        backend.rx_start(t0)
    return reports


# ---------------------------------------------------------------------------
# Coherence verification
# ---------------------------------------------------------------------------
@dataclass
class CoherenceReport:
    ok: bool
    phase_std_deg: np.ndarray
    freq_offset_hz: np.ndarray
    jumps: list                       # [(channel, capture_index, step_deg)]
    captures: int
    reasons: list = field(default_factory=list)
    intra_freq_hz: np.ndarray = None  # per channel: median in-capture frequency offset (Hz)
    retune_step_deg: np.ndarray = None  # per channel: phase change across the check retune (None: not run)
    phase_state: str = ""             # RX phase state the retune check relied on

    def to_dict(self) -> dict:
        return ac.to_jsonable(self)


def _lagged_product(ref, x, lag) -> np.ndarray:
    """``conj(ref[t]) x[t + lag]`` over the overlap (the integer xcorr peak alignment)."""
    n = ref.size
    if lag >= 0:
        return np.conj(ref[:n - lag]) * x[lag:]
    return np.conj(ref[-lag:]) * x[:n + lag]


def _lagged_phase(ref, x, lag) -> float:
    """``arg(sum_t conj(ref[t]) x[t + lag])`` (the inner product at the integer xcorr peak)."""
    n = ref.size
    if lag >= 0:
        return float(np.angle(np.vdot(ref[:n - lag], x[lag:])))
    return float(np.angle(np.vdot(ref[-lag:], x[:n + lag])))


def _capture_freq_hz(z, fs, n_sub=2) -> float:
    """Frequency (Hz) of the phase ramp of the lagged product ``z`` INSIDE one capture:
    the LS phase slope over ``n_sub`` equal sub-blocks (default: the two halves).

    Long sub-blocks on purpose: a block phase ``arg(sum conj(x_ref) x_n)`` is as
    insensitive to the injection waveform (chirp / delay coupling, noise statistics)
    as the whole-capture phase only when the block is long; a lag-1 or short-block
    estimate is biased by the fractional inter-chain delays. Unambiguous for
    ``|f| < n_sub fs / (2 len(z))``; beyond that the blocks decorrelate and the phase
    std fails instead."""
    z = np.asarray(z, dtype=np.complex128)
    n_sub = max(2, int(n_sub))
    L = z.size // n_sub
    if L < 1 or not fs > 0:
        return 0.0
    ph = np.unwrap(np.angle(z[:n_sub * L].reshape(n_sub, L).sum(axis=1)))
    tb = (np.arange(n_sub) + 0.5) * L / float(fs)
    tc = tb - tb.mean()
    return float(tc @ (ph - ph.mean()) / (tc @ tc)) / (2 * math.pi)


def _retune_same_frequency(backend):
    """Re-tune RX to its current frequency the way it was last tuned (timed in a command
    window when the last tune was timed) WITHOUT the per-tune generation bump: the
    check measures whether the phases held, and ``verify_coherence`` bumps only when
    they did not (a passing verification must not invalidate the calibration it just
    verified). Returns ``(phase state relied on, TuneReport)``, or None when the check
    does not apply (no tune yet, unstable phase state, streaming, or a backend without
    the bookkeeping)."""
    last = getattr(backend, "last_tune", None)
    lt = last("rx") if callable(last) else None
    if not lt or lt[3] not in STABLE_PHASE_STATES or getattr(backend, "phase", "") == "STREAMING":
        return None
    fc, lo, timed, state = lt
    if timed:
        lead = float(getattr(getattr(backend.cfg, "timing", None), "command_lead_s", 0.1) or 0.1)
        t_cmd = backend.time_now().add_seconds(Fraction(lead).limit_denominator(10 ** 9))
        with _command_window(backend, t_cmd):
            rep = backend.tune("rx", fc, lo, at=t_cmd, bump=False)
        backend.sleep(lead + 0.01)
    else:
        rep = backend.tune("rx", fc, lo, at=None, bump=False)
    return state, rep


def verify_coherence(backend, rf_control, start_fn, n_caps=4, nsamps=8192, gap_s=0.05,
                     max_phase_std_deg=1.0, max_freq_offset_hz=0.5, retune_check=True,
                     n_sub=2, retune_tol_deg=None) -> CoherenceReport:
    """Phase stability over ``n_caps`` timed injection captures (spec 8.6).

    ``phi_nk = arg(x_ref^H x_n)`` evaluated at channel n's integer cross-correlation
    lag (found on the first capture, |lag| <= ``MAX_ALIGN_LAG``) so that chains whose
    delays differ by whole samples do not decorrelate a broadband injection.

    Frequency offset: the phase ramp is measured INSIDE every capture as well
    (``_capture_freq_hz``: the slope over ``n_sub`` long sub-blocks); its median over the captures
    resolves the ``1/gap_s`` ambiguity of the inter-capture phases (the phases are
    unwrapped around that ramp), so an unshared reference whose offset is a
    multiple of ``1/gap_s`` cannot alias to zero. Then the std over k (unwrapped)
    and the LS slope vs the capture time (-> Hz) per channel; a step that deviates
    from the median step by more than ``5 x`` the robust step spread (floor:
    ``5 x max_phase_std_deg``) is a jump.

    ``retune_check``: when the last RX tune left a stable phase state (shared_lo /
    repeatable / deterministic), RX is re-tuned to the same frequency (timed when
    the last tune was) and one more capture must reproduce the relative phases
    (prediction: the last capture + the measured ramp) within ``retune_tol_deg``
    (default ``5 x max(max_phase_std_deg, measured std)``): an LO import that is not
    active, or a family whose timed tune is not repeatable, fails here and bumps the
    coherence generation (``verify_retune``); so does a check retune that leaves a
    different phase state (e.g. it ran late, or the LO share is no longer
    programmed). The check retune itself never bumps: a PASSING verification keeps
    the generation, and so the calibration it verified.

    Pass iff every std and offset is within limits, there are no jumps and the
    retune check (when run) passed. The cal input is restored to the antenna in
    ``finally``.
    """
    K = int(n_caps)
    if K < 2:
        raise ValueError("n_caps must be >= 2")
    phis, times, freqs = [], [], []
    lags = None
    t0 = None
    retune = None
    try:
        if rf_control is not None:
            rf_control.cal_input("injection")
            rf_control.noise_source(True)
        t0 = start_fn()

        def measure(cap):
            nonlocal lags
            X = np.asarray(cap.data, dtype=np.complex128)
            if lags is None:
                lags = [0] + [int(adsp.xcorr_delay(X[0], X[m], MAX_ALIGN_LAG)[0]) for m in range(1, X.shape[0])]
            ph = np.array([_lagged_phase(X[0], X[m], lags[m]) for m in range(X.shape[0])])
            fr = np.array([0.0] + [_capture_freq_hz(_lagged_product(X[0], X[m], lags[m]), float(cap.fs_hz), n_sub)
                                   for m in range(1, X.shape[0])])
            return ph, fr

        for k in range(K):
            tk = t0.add_seconds(Fraction(gap_s).limit_denominator(10 ** 9) * k)
            if tk < backend.time_now():
                tk = start_fn()
            cap = backend.rx_capture(int(nsamps), tk)
            ph, fr = measure(cap)
            phis.append(ph)
            freqs.append(fr)
            times.append(float(tk.diff_seconds(t0)))
        if retune_check:
            rt = _retune_same_frequency(backend)
            if rt is not None:
                state, trep = rt
                tb = start_fn()
                ph, _fr = measure(backend.rx_capture(int(nsamps), tb))
                retune = (state, ph, float(tb.diff_seconds(t0)), trep)
    finally:
        if rf_control is not None:
            for fn, arg in ((rf_control.noise_source, False), (rf_control.cal_input, "antenna")):
                try:
                    fn(arg)
                except Exception as exc:  # noqa: BLE001
                    logger.error("verify_coherence restore failed: %r", exc)
    P = np.array(phis).T                                     # (M, K) radians
    M = P.shape[0]
    t = np.array(times)
    f_in = np.median(np.array(freqs).T, axis=1)             # (M,) in-capture ramp (Hz)
    pred = 2 * math.pi * f_in[:, None] * t[None, :]
    U = np.unwrap(np.angle(np.exp(1j * (P - pred))), axis=1) + pred
    std = np.degrees(np.std(U, axis=1))
    tc = t - t.mean()
    den = float(np.sum(tc * tc))
    slope = (U - U.mean(axis=1, keepdims=True)) @ tc / den if den > 0 else np.zeros(M)
    foff = slope / (2 * math.pi)
    jumps = []
    for m in range(1, M):
        steps = np.degrees(np.angle(np.exp(1j * np.diff(P[m]))))
        med = float(np.median(steps))
        mad = float(np.median(np.abs(steps - med))) * 1.4826
        thr = max(JUMP_FACTOR * mad, JUMP_FACTOR * float(max_phase_std_deg))
        for k, s in enumerate(steps):
            if abs(s - med) > thr:
                jumps.append((m, k + 1, float(s)))
    reasons = []
    if np.any(std > max_phase_std_deg):
        reasons.append("phase std %.3f deg > %.3f" % (float(np.max(std)), max_phase_std_deg))
    if np.any(np.abs(foff) > max_freq_offset_hz):
        reasons.append("frequency offset %.4g Hz > %.4g" % (float(np.max(np.abs(foff))), max_freq_offset_hz))
    if jumps:
        reasons.append("%d phase jump(s)" % len(jumps))
    step_deg = None
    state = ""
    if retune is not None:
        state, ph_b, t_b, trep = retune
        expect = U[:, -1] + 2 * math.pi * foff * (t_b - t[-1])
        step_deg = np.degrees(np.angle(np.exp(1j * (ph_b - expect))))
        tol = float(retune_tol_deg) if retune_tol_deg is not None else \
            JUMP_FACTOR * max(float(max_phase_std_deg), float(np.max(std)))
        worst = float(np.max(np.abs(step_deg[1:]))) if M > 1 else 0.0
        bump = False
        if worst > tol:
            reasons.append("relative phase changed by up to %.1f deg (> %.1f) across a same-frequency retune "
                           "(phase state %s): the LO share / timed-tune repeatability does not hold"
                           % (worst, tol, state))
            bump = True
        new_state = getattr(trep, "phase_state", state)
        if getattr(trep, "band_change", False) or new_state != state:
            # the check retune itself left a different phase class (e.g. the LO share is no
            # longer programmed, or the timed tune ran late): the relied-on state is gone
            reasons.append("the check retune left phase state %s (relied on %s)" % (new_state, state))
            bump = True
        if bump:
            try:
                backend.coherence.bump("verify_retune")
            except Exception as exc:  # noqa: BLE001
                logger.error("verify_coherence: generation bump failed: %r", exc)
    rep = CoherenceReport(ok=not reasons, phase_std_deg=std, freq_offset_hz=foff, jumps=jumps, captures=K,
                          reasons=reasons, intra_freq_hz=f_in, retune_step_deg=step_deg, phase_state=state)
    if not rep.ok:
        ev = getattr(backend, "event_log", None)
        if ev is not None:
            try:
                ev.emit(EVT_ACTIVE_COHERENCE_FAIL, "warning", reasons=reasons, jumps=jumps,
                        phase_std_deg=std, freq_offset_hz=foff)
            except Exception:
                pass
    return rep


# ---------------------------------------------------------------------------
# Coherence budget (spec 8.7)
# ---------------------------------------------------------------------------
def skew_to_phase_deg(f_hz, dt_s) -> float:
    """Phase error (deg) of a timing skew ``dt_s`` at ``f_hz``: ``360 f dt``."""
    return 360.0 * float(f_hz) * float(dt_s)


def position_tolerance_m(f_hz, phase_deg) -> float:
    """Position error (m) giving ``phase_deg`` at ``f_hz``: ``phase/360 lambda``."""
    return float(phase_deg) / 360.0 * C0 / float(f_hz)


def freq_offset_hz(f_hz, ppm) -> float:
    return float(f_hz) * float(ppm) * 1e-6


def coherent_gain_loss_db(sigma_deg) -> float:
    """Coherent array-gain loss of a random phase error: ``10 log10(e) sigma^2`` (rad)."""
    s = math.radians(float(sigma_deg))
    return 10.0 * math.log10(math.e) * s * s


def sidelobe_floor_db(sigma_deg, n, eta=1.0) -> float:
    """Average sidelobe floor of a random phase error: ``sigma^2 / (eta N)``."""
    s = math.radians(float(sigma_deg))
    if s <= 0:
        return float("-inf")
    return 10.0 * math.log10(s * s / (float(eta) * int(n)))


def null_floor_db(phase_deg, amp_db, n, eta=1.0) -> float:
    """Calibration-limited null depth: ``eps^2/(eta N)``, ``eps^2 = sigma_phi^2 + (10^(A/20) - 1)^2``."""
    e2 = math.radians(float(phase_deg)) ** 2 + (10.0 ** (float(amp_db) / 20.0) - 1.0) ** 2
    if e2 <= 0:
        return float("-inf")
    return 10.0 * math.log10(e2 / (float(eta) * int(n)))


def ula_bearing_sigma_deg(sigma_phi_deg, n, d_m, f_hz, theta_deg=0.0) -> float:
    """Bearing std (deg) of an N-element ULA from per-element phase noise (LS phase slope)."""
    n = int(n)
    if n < 2:
        raise ValueError("n must be >= 2")
    k = 2 * math.pi * float(f_hz) / C0
    sxx = float(d_m) ** 2 * n * (n * n - 1) / 12.0
    s_sin = math.radians(float(sigma_phi_deg)) / (k * math.sqrt(sxx))
    c = math.cos(math.radians(float(theta_deg)))
    if abs(c) < 1e-9:
        return float("inf")
    return math.degrees(s_sin / abs(c))


def rss_deg(contribs: dict) -> float:
    return math.sqrt(sum(float(v) ** 2 for v in contribs.values()))


def coherence_budget(f_hz, skew_s=0.0, pos_sigma_m=0.0, cal_phase_deg=0.0, drift_deg=0.0, lo_jitter_s=0.0,
                     amp_rms_db=0.0, n=8, taper_eff=1.0) -> dict:
    """Per-term phase errors (deg), their rss, the gain loss and the sidelobe / null floors."""
    f = float(f_hz)
    lam = C0 / f
    terms = {"skew": 360.0 * f * float(skew_s), "position": 360.0 * float(pos_sigma_m) / lam,
             "calibration": float(cal_phase_deg), "drift": float(drift_deg),
             "lo_jitter": 360.0 * f * float(lo_jitter_s)}
    phase = rss_deg(terms)
    amp_eps = 10.0 ** (float(amp_rms_db) / 20.0) - 1.0
    total_eps_deg = math.sqrt(phase ** 2 + math.degrees(amp_eps) ** 2)
    return {"f_hz": f, "terms_deg": terms, "amplitude_eps_deg": math.degrees(amp_eps),
            "phase_rss_deg": phase, "total_eps_deg": total_eps_deg,
            "gain_loss_db": coherent_gain_loss_db(phase),
            "sidelobe_floor_db": sidelobe_floor_db(total_eps_deg, n, taper_eff),
            "null_floor_db": null_floor_db(phase, amp_rms_db, n, taper_eff), "n": int(n),
            "taper_eff": float(taper_eff)}


def phase_budget_report(cfg, geometry, cal_sigma_deg=None) -> dict:
    """The coherence budget of a configuration at the top of its band, with verdicts:
    DoA / beam ok at <= 5 deg rss, -30 dB nulls ok at <= 2 deg."""
    rf = cfg.rf
    fc = float(rf.center_freq_hz)
    f_max = float(cfg.geometry.f_max_hz) or fc + rf.effective_bandwidth_hz() / 2.0
    n = cfg.num_rx()
    assumptions = []
    pos = 0.0
    d = C0 / f_max / 2.0
    if geometry is not None:
        try:
            pos = float(geometry.max_pos_sigma_m("rx"))
            d = float(geometry.min_spacing_m("rx"))
            n = len(geometry.elements_for("rx"))
        except Exception as exc:  # noqa: BLE001
            assumptions.append("geometry metrics unavailable (%r)" % (exc,))
    else:
        assumptions.append("no geometry: lambda/2 spacing and exact positions assumed")
    if cal_sigma_deg is None:
        K = int(cfg.calibration.cal_samples) * int(cfg.calibration.cal_repeats)
        if cfg.active.backend == "sim":
            gain = float(cfg.rf.rx_gain_db[0]) if cfg.rf.rx_gain_db else 30.0
            snr_db = cfg.sim.injection_dbm + gain - cfg.sim.rx_fullscale_dbm - cfg.sim.noise_dbfs
            assumptions.append("calibration SNR %.1f dB from [active_sim]" % snr_db)
        else:
            snr_db = ASSUMED_CAL_SNR_DB
            assumptions.append("calibration SNR assumed %.0f dB (no measurement)" % snr_db)
        snr = 10.0 ** (snr_db / 10.0)
        cal_sigma_deg = math.degrees(1.0 / math.sqrt(2.0 * K * snr))
    skew_s = 0.0
    if len(cfg.devices) > 1:
        ticks = []
        for dv in cfg.devices:
            tr = adev.tick_rate_hz(dv, cfg) or 100_000_000
            ticks.append(tr)
        skew_s = float(cfg.timing.max_pps_skew_ticks) / float(min(ticks))
    drift = float(cfg.calibration.drift_threshold_deg)
    b = coherence_budget(f_max, skew_s=skew_s, pos_sigma_m=pos, cal_phase_deg=cal_sigma_deg, drift_deg=drift,
                         n=max(2, n))
    try:
        bearing = ula_bearing_sigma_deg(b["phase_rss_deg"], max(2, n), d, f_max)
    except ValueError:
        bearing = float("nan")
    b.update({"ula_bearing_sigma_deg": bearing, "cal_sigma_deg": float(cal_sigma_deg),
              "verdict_doa_ok": b["phase_rss_deg"] <= DOA_VERDICT_DEG,
              "verdict_null_30db_ok": b["phase_rss_deg"] <= NULL_VERDICT_DEG,
              "assumptions": assumptions})
    return b
