"""
    HeIMDALL "active" capability: RF front-end control lines and sequencing.

    Drives the discrete control lines of an amplified front end - PA enable,
    T/R switch, LNA bias, calibration switch matrix, noise source - through the
    backend GPIO interface, and reads the safety inputs (hardware interlock,
    dummy-load sense, forward/reverse power monitor).

    Line specs (``parse_line``)::

        none                        no line
        <device>:<bank>:<mask>[:low] USRP GPIO bank bits (``low`` = active low)
        sim:<name>                  a simulated line (sim backend)
        file:<path>                 a state file written by an external daemon
                                    (interlock / dummy-load sense / power monitor)

    Backend GPIO contract (all backends): ``gpio_write(line: LineSpec, value)``
    and ``gpio_read(line) -> bool`` take the LOGICAL state (True = asserted:
    PA enabled, T/R in tx, LNA biased, interlock loop closed ...); the backend
    maps it to electrical levels with ``line.mask`` and ``line.active_high``.
    ``gpio_atr(device, bank, ctrl_mask, idle, rx, tx, duplex)`` takes ELECTRICAL
    bank values (this module applies the polarity).

    Sequencing (software, when ATR is not in use)::

        to TX: RX gain min (mute) -> LNA off -> settle -> T/R tx -> tr_guard -> PA on -> pa_settle
        to RX: PA off -> pa_settle -> T/R rx -> tr_guard -> LNA restore -> RX gain restore

    Safety rules enforced here: ``pa_enable(on)`` requires an active permit (or
    the armed hold session) from the TX safety controller, otherwise it trips
    ``SEQUENCE``; the T/R switch and the cal switch matrix never move while any
    PA is (or may be) enabled; a PA line whose "off" write failed is treated as
    still on. Software interlocks are millisecond-class: an E-stop wired to PA
    enable and PA foldback are required in hardware for amplified systems.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass

import active_common as ac
from active_common import ConfigError, HardwareFault, InterlockTrip, Issue, SafetyRefusal

logger = logging.getLogger("heimdall.active.rf_control")

LINE_KINDS = ("none", "gpio", "sim", "file")
TR_MODES = ("rx", "tx")
CAL_INPUTS = ("antenna", "injection", "loopback")
_TRUE_WORDS = {"1", "closed", "close", "ok", "true", "yes", "on", "present", "connected"}
_FALSE_WORDS = {"0", "open", "false", "no", "off", "absent", "fault", "tripped", "disconnected"}
# A safety-input timestamp further than this in the FUTURE is invalid (a daemon writing
# milliseconds, or a clock running ahead, would otherwise produce a reading that never ages).
FUTURE_SKEW_S = 1.0


def _cerr(code, msg, where=""):
    return ConfigError(msg + (" (%s)" % where if where else ""), (Issue("error", code, msg, where),))


@dataclass(frozen=True)
class LineSpec:
    """A control/sense line (kind none | gpio | sim | file)."""
    kind: str
    device: str = ""
    bank: str = ""
    mask: int = 0
    active_high: bool = True
    path: str = ""
    name: str = ""

    def configured(self) -> bool:
        return self.kind != "none"

    def level_bits(self, asserted: bool) -> int:
        """Electrical bank bits of this line for a logical state (gpio lines)."""
        on = bool(asserted) == bool(self.active_high)
        return int(self.mask) if on else 0

    def __str__(self):
        if self.kind == "gpio":
            return "%s:%s:0x%x%s" % (self.device, self.bank, self.mask, "" if self.active_high else ":low")
        if self.kind == "sim":
            return "sim:%s" % self.name if self.name else "sim"
        if self.kind == "file":
            return "file:%s" % self.path
        return "none"


NONE_LINE = LineSpec("none")


def parse_line(spec: str) -> LineSpec:
    """``none`` | ``<device>:<bank>:<mask>[:low]`` | ``sim[:<name>]`` | ``file:<path>``.

    ``ConfigError`` (code ``LINE_SPEC``) otherwise; the mask is an int (``0x`` ok)
    in 1..2^32-1.
    """
    t = "" if spec is None else str(spec).strip()
    low = t.lower()
    if low in ("", "none"):
        return NONE_LINE
    if low == "sim":
        return LineSpec("sim")
    if low.startswith("sim:"):
        name = t[4:].strip()
        if not name:
            raise _cerr("LINE_SPEC", "sim:<name> needs a name", t)
        return LineSpec("sim", name=name)
    if low.startswith("file:"):
        path = t[5:].strip()
        if not path:
            raise _cerr("LINE_SPEC", "file:<path> needs a path", t)
        return LineSpec("file", path=os.path.expanduser(path))
    parts = [p.strip() for p in t.split(":")]
    if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3].lower() not in ("low", "high")):
        raise _cerr("LINE_SPEC", "line spec %r must be none | <device>:<bank>:<mask>[:low] | sim:<name> | "
                                 "file:<path>" % t, t)
    dev, bank, mask_s = parts[0], parts[1], parts[2]
    if not dev or not bank:
        raise _cerr("LINE_SPEC", "line spec %r needs a device and a bank" % t, t)
    try:
        mask = int(mask_s, 0)
    except ValueError:
        raise _cerr("LINE_SPEC", "line spec %r: mask %r is not an integer" % (t, mask_s), t) from None
    if not 0 < mask < (1 << 32):
        raise _cerr("LINE_SPEC", "line spec %r: mask must be in 1..2^32-1" % t, t)
    active_high = not (len(parts) == 4 and parts[3].lower() == "low")
    return LineSpec("gpio", device=dev, bank=bank, mask=mask, active_high=active_high)


def parse_line_list(spec: str, n: int) -> list:
    """One shared spec (broadcast to ``n``) or exactly ``n`` comma-separated specs."""
    n = int(n)
    text = "" if spec is None else str(spec).strip()
    if n <= 0:
        return []
    if text == "" or text.lower() == "none":
        return [NONE_LINE] * n
    toks = [t.strip() for t in text.split(",")]
    lines = [parse_line(t) for t in toks]
    if len(lines) == 1:
        return lines * n
    if len(lines) != n:
        raise _cerr("LIST_LEN", "expected 1 or %d line specs, got %d" % (n, len(lines)), text)
    return lines


def resolve_line(line: LineSpec, base_dir: str) -> LineSpec:
    """A ``file:`` line with a relative path resolved against ``base_dir``."""
    if line.kind == "file" and not os.path.isabs(line.path):
        return LineSpec("file", path=os.path.normpath(os.path.join(base_dir, line.path)))
    return line


# ---------------------------------------------------------------------------
# Safety inputs
# ---------------------------------------------------------------------------
@dataclass
class Telemetry:
    """Power-monitor reading. List entries (per TX chain) may be None; ``age_s``
    is the age of the reading (inf = no data)."""
    fwd_dbm: list
    rev_dbm: list
    temp_c: list
    age_s: float

    @staticmethod
    def empty() -> "Telemetry":
        return Telemetry([], [], [], math.inf)

    def to_dict(self) -> dict:
        return ac.to_jsonable(self)


def _word_state(text: str):
    t = str(text).strip().lower()
    if not t:
        return None
    tok = t.split()[0]
    if tok in _TRUE_WORDS:
        return True
    if tok in _FALSE_WORDS:
        return False
    return None


class InterlockInput:
    """A closed/open safety loop (hardware interlock or dummy-load sense).

    ``closed()``: None when not configured; True only on a positive closed
    reading; any read error, missing/stale file or unknown content is False
    (fail safe). File lines accept ``1/0``, ``closed/open`` (first word) or JSON
    ``{"closed": bool, "unix": t}``; with ``max_age_s`` a reading older than
    that (JSON ``unix`` or the file mtime) is stale -> False, and a timestamp
    more than ``FUTURE_SKEW_S`` in the future is invalid -> False.
    """

    def __init__(self, spec: LineSpec, backend=None, clock=time.monotonic, max_age_s=None,
                 wallclock=time.time):
        if isinstance(spec, str):
            spec = parse_line(spec)
        self.spec = spec
        self.backend = backend
        self._clock = clock
        self._wallclock = wallclock
        self.max_age_s = None if max_age_s is None else float(max_age_s)
        self.last_error = ""

    def configured(self) -> bool:
        return self.spec.kind != "none"

    def closed(self):
        if not self.configured():
            return None
        try:
            if self.spec.kind in ("gpio", "sim"):
                if self.backend is None:
                    self.last_error = "no backend to read %s" % self.spec
                    return False
                return bool(self.backend.gpio_read(self.spec))
            return self._read_file()
        except Exception as exc:
            self.last_error = repr(exc)
            return False

    def _read_file(self) -> bool:
        path = self.spec.path
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            mtime = os.path.getmtime(path)
        except OSError as exc:
            self.last_error = "cannot read %s: %s" % (path, exc)
            return False
        state, ts = None, None
        s = text.strip()
        if s.startswith("{"):
            try:
                d = json.loads(s)
                v = d.get("closed")
                state = v if isinstance(v, bool) else None
                ts = d.get("unix")
            except (ValueError, AttributeError):
                state = None
        else:
            state = _word_state(s)
        if state is None:
            self.last_error = "unrecognised content in %s" % path
            return False
        if self.max_age_s is not None:
            t_ref = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else mtime
            wall = float(self._wallclock())
            if not math.isfinite(t_ref) or t_ref > wall + FUTURE_SKEW_S:
                self.last_error = "timestamp in the future in %s" % path
                return False
            if wall - t_ref > self.max_age_s:
                self.last_error = "stale reading in %s" % path
                return False
        self.last_error = ""
        return bool(state)


class PowerMonitor:
    """Forward/reverse power + temperature telemetry.

    ``sim``: ``backend.telemetry()`` (a ``Telemetry`` or a dict with the same
    keys); ``file:<path>``: JSON ``{"unix": t, "fwd_dbm": [...], "rev_dbm":
    [...], "temp_c": [...]}`` written by a detector daemon (``age_s`` from
    ``unix``, else the file mtime). Missing / corrupt data, or a timestamp more
    than ``FUTURE_SKEW_S`` in the future -> ``age_s = inf``.
    """

    def __init__(self, spec: LineSpec, backend=None, clock=time.monotonic, wallclock=time.time):
        if isinstance(spec, str):
            spec = parse_line(spec)
        if spec.kind == "gpio":
            raise _cerr("ENUM", "power_monitor must be none, sim or file:<path>", str(spec))
        self.spec = spec
        self.backend = backend
        self._clock = clock
        self._wallclock = wallclock
        self.last_error = ""

    def configured(self) -> bool:
        return self.spec.kind != "none"

    @staticmethod
    def _coerce(d) -> Telemetry:
        if isinstance(d, Telemetry):
            return Telemetry(list(d.fwd_dbm or []), list(d.rev_dbm or []), list(d.temp_c or []),
                             float(d.age_s))
        if isinstance(d, dict):
            def lst(k):
                v = d.get(k, [])
                return [None if x is None else float(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
            age = d.get("age_s", 0.0)
            return Telemetry(lst("fwd_dbm"), lst("rev_dbm"), lst("temp_c"),
                             math.inf if age is None else float(age))
        raise TypeError("telemetry must be Telemetry or dict, got %r" % type(d))

    def read(self) -> Telemetry:
        if not self.configured():
            return Telemetry.empty()
        try:
            if self.spec.kind == "sim":
                if self.backend is None or not hasattr(self.backend, "telemetry"):
                    self.last_error = "no sim backend telemetry"
                    return Telemetry.empty()
                return self._coerce(self.backend.telemetry())
            return self._read_file()
        except Exception as exc:
            self.last_error = repr(exc)
            return Telemetry.empty()

    def _read_file(self) -> Telemetry:
        path = self.spec.path
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            mtime = os.path.getmtime(path)
        except (OSError, ValueError) as exc:
            self.last_error = "cannot read %s: %s" % (path, exc)
            return Telemetry.empty()
        if not isinstance(d, dict):
            self.last_error = "%s is not a JSON object" % path
            return Telemetry.empty()
        ts = d.get("unix")
        t_ref = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else mtime
        tel = self._coerce({k: d.get(k, []) for k in ("fwd_dbm", "rev_dbm", "temp_c")})
        wall = float(self._wallclock())
        if not math.isfinite(t_ref) or t_ref > wall + FUTURE_SKEW_S:
            self.last_error = "timestamp in the future in %s" % path
            tel.age_s = math.inf
            return tel
        tel.age_s = max(0.0, wall - t_ref)
        self.last_error = ""
        return tel


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------
class RfControl:
    """Front-end line control with safety-gated sequencing.

    ``state`` holds the COMMANDED line states: ``pa`` (list per TX chain),
    ``tr``, ``lna``, ``cal_input``, ``noise``, ``atr``. A failed "off" write
    leaves ``pa`` True (unknown is treated as on). A software PA-off (also
    ``force_safe``) overrides ATR into manual mode, so ``atr`` becomes False
    until ``configure_atr`` runs again.
    """

    def __init__(self, rf_cfg, n_tx: int, backend, event_log, safety=None, sleep=time.sleep,
                 base_dir=""):
        self.cfg = rf_cfg
        self.n_tx = int(n_tx)
        self.backend = backend
        self.event_log = event_log
        self.safety = safety
        self._sleep = sleep
        self.pa_lines = [resolve_line(x, base_dir) for x in parse_line_list(rf_cfg.pa_enable_line, self.n_tx)]
        self.tr_line = parse_line(rf_cfg.tr_switch_line)
        self.lna_line = parse_line(rf_cfg.lna_bias_line)
        self.cal_injection_line = parse_line(rf_cfg.cal_injection_line)
        self.cal_loopback_line = parse_line(rf_cfg.cal_loopback_line)
        self.noise_line = parse_line(rf_cfg.noise_source_line)
        for name, ln in (("tr_switch_line", self.tr_line), ("lna_bias_line", self.lna_line),
                         ("cal_injection_line", self.cal_injection_line),
                         ("cal_loopback_line", self.cal_loopback_line),
                         ("noise_source_line", self.noise_line)):
            if ln.kind == "file":
                raise _cerr("LINE_SPEC", "%s cannot be a file: line (output lines are gpio/sim)" % name, name)
        for ln in self.pa_lines:
            if ln.kind == "file":
                raise _cerr("LINE_SPEC", "pa_enable_line cannot be a file: line", "pa_enable_line")
        self.tr_guard_s = max(0.0, float(rf_cfg.tr_guard_us)) / 1e6
        self.pa_settle_s = max(0.0, float(rf_cfg.pa_settle_us)) / 1e6
        self.state = {"pa": [False] * self.n_tx, "tr": "rx", "lna": True, "cal_input": "antenna",
                      "noise": False, "atr": False}
        self._saved_rx_gain = None
        self._lna_before_tx = None
        self.history = []                     # (action, detail) of every line command

    # --- wiring --------------------------------------------------------------------
    def attach_safety(self, safety) -> None:
        self.safety = safety

    @property
    def atr_active(self) -> bool:
        return bool(self.state.get("atr"))

    def pa_any_on(self) -> bool:
        return any(self.state["pa"])

    def _policy(self, name, default=True):
        pol = getattr(self.safety, "policy", None) if self.safety is not None else None
        return bool(getattr(pol, name, default)) if pol is not None else default

    def _emit(self, kind, **payload):
        if self.event_log is not None:
            try:
                self.event_log.emit("active_rf_control", "debug", action=kind, **payload)
            except Exception:
                pass

    def _write(self, line: LineSpec, value: bool, what: str) -> None:
        self.history.append((what, bool(value)))
        if line.kind == "none":
            return
        if self.backend is None:
            raise HardwareFault("%s: no backend to drive %s" % (what, line))
        self.backend.gpio_write(line, bool(value))

    def _resolve_chans(self, chans):
        if chans is None or (isinstance(chans, str) and chans == "all"):
            return list(range(self.n_tx))
        out = []
        for c in ([chans] if isinstance(chans, int) else chans):
            c = int(c)
            if not 0 <= c < self.n_tx:
                raise ValueError("TX chain %d out of range (n_tx = %d)" % (c, self.n_tx))
            out.append(c)
        return sorted(set(out))

    # --- PA ------------------------------------------------------------------------
    def _authorized(self, chans, permit) -> tuple:
        s = self.safety
        if s is None:
            return False, "no TX safety controller attached"
        try:
            if permit is not None and s.permit_active(permit):
                pchains = getattr(permit, "chains", None)
                if pchains is not None and not set(chans) <= set(int(c) for c in pchains):
                    return False, "chains %s are not covered by the permit %s" % (chans, list(pchains))
                return True, ""
            rec = getattr(s, "arm_record", None)
            if permit is None and rec is not None and s.permit_active(rec):
                return True, ""
        except Exception as exc:
            return False, "permit check failed: %r" % (exc,)
        return False, "no active TX permit (or armed hold session)"

    def pa_enable(self, chans, on: bool, permit=None) -> None:
        """Enable/disable the PA lines of ``chans`` (list, int, None/"all").

        On requires ``safety.permit_active(permit)`` (or the armed hold session
        when ``permit`` is None); otherwise the safety controller trips
        ``SEQUENCE`` and ``InterlockTrip("SEQUENCE")`` is raised. Off is always
        allowed; a failed off write raises ``HardwareFault`` after every line was
        attempted and leaves that chain marked on.
        """
        chans = self._resolve_chans(chans)
        if on:
            ok, why = self._authorized(chans, permit)
            if not ok:
                detail = "pa_enable(on) refused for chains %s: %s" % (chans, why)
                logger.error(detail)
                if self.safety is not None:
                    try:
                        self.safety.trip("SEQUENCE", detail)
                    except Exception:
                        pass
                raise InterlockTrip("SEQUENCE", detail)
            if self.atr_active:
                for c in chans:
                    self.state["pa"][c] = True
                return
            groups = {}
            for c in chans:
                groups.setdefault(self.pa_lines[c], []).append(c)
            try:
                for line, cs in groups.items():
                    self._write(line, True, "pa_on:%s" % ",".join(map(str, cs)))
                    for c in cs:
                        self.state["pa"][c] = True
            except Exception:
                self.force_safe()
                raise
            self._emit("pa_on", chains=chans)
            return
        # off: a shared line goes low only when no chain on it stays enabled
        errors = []
        target = set(chans)
        by_line = {}
        for c in range(self.n_tx):
            by_line.setdefault(self.pa_lines[c], []).append(c)
        for line, cs in by_line.items():
            mine = [c for c in cs if c in target]
            if not mine:
                continue
            others_on = [c for c in cs if c not in target and self.state["pa"][c]]
            if others_on:
                for c in mine:
                    self.state["pa"][c] = False
                continue
            try:
                self._write(line, False, "pa_off:%s" % ",".join(map(str, mine)))
                for c in mine:
                    self.state["pa"][c] = False
            except Exception as exc:
                errors.append("%s: %r" % (line, exc))
                for c in mine:
                    self.state["pa"][c] = True
        if self.atr_active:
            # a software PA-off puts the bank in manual mode: ATR must be configured again
            self.state["atr"] = False
        self._emit("pa_off", chains=chans, errors=errors)
        if errors:
            raise HardwareFault("PA disable failed on %s" % "; ".join(errors))

    # --- T/R, LNA, cal, noise --------------------------------------------------------
    def tr_switch(self, mode: str) -> None:
        """Move the T/R switch (``rx`` | ``tx``); refused (SafetyRefusal SEQUENCE)
        while any PA is enabled."""
        if mode not in TR_MODES:
            raise ValueError("T/R mode must be rx or tx, got %r" % (mode,))
        if self.pa_any_on():
            raise SafetyRefusal("SEQUENCE", "T/R switch to %s refused: PA enabled on chains %s"
                                % (mode, [c for c, v in enumerate(self.state["pa"]) if v]))
        self._write(self.tr_line, mode == "tx", "tr_%s" % mode)
        self.state["tr"] = mode

    def lna_bias(self, on: bool) -> None:
        """LNA bias on/off; turning it on is refused while a PA is enabled and the
        policy keeps the LNA off during TX."""
        if on and self.pa_any_on() and self._policy("lna_off_during_tx", True):
            raise SafetyRefusal("SEQUENCE", "LNA bias on refused while a PA is enabled")
        self._write(self.lna_line, bool(on), "lna_%s" % ("on" if on else "off"))
        self.state["lna"] = bool(on)

    def cal_input(self, mode: str) -> None:
        """Route the RX inputs (``antenna`` | ``injection`` | ``loopback``);
        refused while any PA is enabled. Break before make."""
        if mode not in CAL_INPUTS:
            raise ValueError("cal input must be one of %s, got %r" % (CAL_INPUTS, mode))
        if self.pa_any_on():
            raise SafetyRefusal("SEQUENCE", "cal switch to %s refused: a PA is enabled" % mode)
        self._write(self.cal_injection_line, False, "cal_injection_off")
        self._write(self.cal_loopback_line, False, "cal_loopback_off")
        self.state["cal_input"] = "antenna"
        if mode == "injection":
            self._write(self.cal_injection_line, True, "cal_injection_on")
        elif mode == "loopback":
            self._write(self.cal_loopback_line, True, "cal_loopback_on")
        self.state["cal_input"] = mode

    def noise_source(self, on: bool) -> None:
        self._write(self.noise_line, bool(on), "noise_%s" % ("on" if on else "off"))
        self.state["noise"] = bool(on)

    # --- RX gain mute / restore ---------------------------------------------------------
    def _rx_gain_min(self, n):
        mins = []
        try:
            caps = getattr(self.backend, "caps", None) or {}
            for ref in self.backend.rx_refs():
                c = caps.get(ref.device)
                mins.append(float(c.rx_gain_range_db[0]) if c is not None else 0.0)
        except Exception:
            mins = []
        if len(mins) != n:
            mins = [0.0] * n
        return mins

    def mute_rx(self) -> None:
        """Save the RX gains and set the minimum (before TX)."""
        if self.backend is None:
            return
        cur = list(self.backend.rx_gain_db())
        if self._saved_rx_gain is None:
            self._saved_rx_gain = cur
        self.backend.set_rx_gain(self._rx_gain_min(len(cur)))
        self.history.append(("rx_mute", True))

    def restore_rx_gain(self) -> None:
        """Restore the RX gains saved by ``mute_rx`` (no-op when nothing is saved)."""
        if self._saved_rx_gain is None or self.backend is None:
            return
        saved = self._saved_rx_gain
        self.backend.set_rx_gain(saved)
        self._saved_rx_gain = None
        self.history.append(("rx_restore", True))

    def lna_restore_value(self) -> bool:
        """The LNA state to restore after TX: the state before ``sequence_to_tx``,
        else the current commanded state (an LNA the operator switched off stays off)."""
        if self._lna_before_tx is not None:
            return bool(self._lna_before_tx)
        return bool(self.state["lna"])

    def restore_lna(self) -> None:
        """Restore the pre-TX LNA bias (``lna_restore_value``) and forget the saved state."""
        self.lna_bias(self.lna_restore_value())
        self._lna_before_tx = None

    # --- sequencing ------------------------------------------------------------------------
    def sequence_to_tx(self, permit) -> list:
        """RX gain min (mute) -> LNA off -> settle -> T/R tx -> tr_guard -> PA on -> pa_settle.

        ``permit`` is a ``TxPermit`` (its chains) or the safety controller's
        ``ArmRecord`` (hold session: every chain). With ATR active the hardware
        sequences T/R, LNA and PA; only the RX mute runs here. Any failure runs
        ``force_safe`` and re-raises. Returns ``[(step, detail)]``.
        """
        steps = []
        chains = getattr(permit, "chains", None)
        chans = self._resolve_chans(None if chains is None else list(chains))
        ok, why = self._authorized(chans, permit)
        if not ok:
            detail = "sequence_to_tx refused: %s" % why
            if self.safety is not None:
                try:
                    self.safety.trip("SEQUENCE", detail)
                except Exception:
                    pass
            raise InterlockTrip("SEQUENCE", detail)
        try:
            if self._policy("mute_rx_during_tx", True):
                self.mute_rx()
                steps.append(("rx_mute", ""))
            if self.atr_active:
                for c in chans:
                    self.state["pa"][c] = True
                steps.append(("atr", "hardware sequenced"))
                return steps
            if self._lna_before_tx is None:
                self._lna_before_tx = self.state["lna"]      # restored by sequence_to_rx
            if self._policy("lna_off_during_tx", True):
                self.lna_bias(False)
                steps.append(("lna_off", ""))
            self._sleep(self.tr_guard_s)
            steps.append(("settle", self.tr_guard_s))
            self.tr_switch("tx")
            steps.append(("tr_tx", ""))
            self._sleep(self.tr_guard_s)
            steps.append(("tr_guard", self.tr_guard_s))
            self.pa_enable(chans, True, permit)
            steps.append(("pa_on", chans))
            self._sleep(self.pa_settle_s)
            steps.append(("pa_settle", self.pa_settle_s))
        except BaseException:
            self.force_safe()
            raise
        return steps

    def sequence_to_rx(self) -> list:
        """PA off -> pa_settle -> T/R rx -> tr_guard -> LNA restore -> RX gain restore.

        Never raises; returns ``[(step, ok, error)]``. The T/R switch is not
        moved (and the LNA stays off) when a PA may still be on.
        """
        steps = []

        def run(name, fn):
            try:
                fn()
                steps.append((name, True, ""))
                return True
            except Exception as exc:
                logger.error("sequence_to_rx %s failed: %r", name, exc)
                steps.append((name, False, repr(exc)))
                return False

        if self.atr_active:
            # ATR returns the lines to idle by itself after the burst: no override
            self.state["pa"] = [False] * self.n_tx
            steps.append(("atr_idle", True, ""))
            run("rx_gain_restore", self.restore_rx_gain)
            return steps
        pa_ok = run("pa_off", lambda: self.pa_enable(None, False))
        run("pa_settle", lambda: self._sleep(self.pa_settle_s))
        if pa_ok and not self.pa_any_on():
            run("tr_rx", lambda: self.tr_switch("rx"))
            run("tr_guard", lambda: self._sleep(self.tr_guard_s))
            restore = self.lna_restore_value()
            if run("lna_restore", lambda: self.lna_bias(restore)):
                self._lna_before_tx = None
        else:
            steps.append(("tr_rx", False, "skipped: PA state unknown"))
        run("rx_gain_restore", self.restore_rx_gain)
        return steps

    def force_safe(self) -> list:
        """Every PA line off, then T/R rx (only if every PA line is off). Never raises."""
        steps = []
        errors = []
        for line in sorted(set(self.pa_lines), key=str):
            try:
                self._write(line, False, "force_pa_off")
            except Exception as exc:
                errors.append(repr(exc))
        for c in range(self.n_tx):
            self.state["pa"][c] = bool(errors)
        self.state["atr"] = False
        steps.append(("pa_off", not errors, "; ".join(errors)))
        if errors:
            steps.append(("tr_rx", False, "skipped: PA state unknown"))
            logger.critical("force_safe: PA disable failed: %s", errors)
        else:
            try:
                self._write(self.tr_line, False, "force_tr_rx")
                self.state["tr"] = "rx"
                steps.append(("tr_rx", True, ""))
            except Exception as exc:
                steps.append(("tr_rx", False, repr(exc)))
        self._emit("force_safe", steps=steps)
        return steps

    # --- ATR ----------------------------------------------------------------------------------
    def configure_atr(self) -> bool:
        """Program USRP ATR on the GPIO banks of the PA / T/R / LNA lines.

        Only when ``use_atr = 1`` and every configured PA / T/R / LNA line is a
        gpio line and the backend accepts every bank: idle = rx = (PA off, T/R rx,
        LNA on); tx = duplex = (PA on, T/R tx, LNA off when lna_off_during_tx).
        Returns True when ATR is active.
        """
        self.state["atr"] = False
        if not bool(getattr(self.cfg, "use_atr", False)) or self.backend is None:
            return False
        lines = [("pa", ln) for ln in set(self.pa_lines) if ln.configured()]
        lines += [(k, ln) for k, ln in (("tr", self.tr_line), ("lna", self.lna_line)) if ln.configured()]
        if not lines or any(ln.kind != "gpio" for _, ln in lines):
            return False
        lna_off = self._policy("lna_off_during_tx", True)
        banks = {}
        for kind, ln in lines:
            banks.setdefault((ln.device, ln.bank), []).append((kind, ln))
        done = []
        try:
            for (dev, bank), members in sorted(banks.items()):
                ctrl = idle = tx = 0
                for kind, ln in members:
                    ctrl |= ln.mask
                    if kind == "pa":
                        idle |= ln.level_bits(False)
                        tx |= ln.level_bits(True)
                    elif kind == "tr":
                        idle |= ln.level_bits(False)
                        tx |= ln.level_bits(True)
                    else:
                        idle |= ln.level_bits(True)
                        tx |= ln.level_bits(not lna_off)
                done.append((dev, bank, idle))
                if not self.backend.gpio_atr(dev, bank, ctrl, idle, idle, tx, tx):
                    self._atr_rollback(done, "bank %s:%s refused ATR" % (dev, bank))
                    return False
                self.history.append(("atr", "%s:%s ctrl=0x%x idle=0x%x tx=0x%x" % (dev, bank, ctrl, idle, tx)))
        except Exception as exc:
            self._atr_rollback(done, "ATR configuration failed: %r" % (exc,))
            return False
        self.state["atr"] = True
        return True

    def _atr_rollback(self, banks, why) -> None:
        """Return every bank touched by ``configure_atr`` to manual control (ctrl mask
        0, idle levels: PA off, T/R rx), then ``force_safe``. Never raises."""
        logger.error("%s: rolling back %d bank(s) to manual control", why, len(banks))
        for dev, bank, idle in banks:
            try:
                self.backend.gpio_atr(dev, bank, 0, idle, idle, idle, idle)
                self.history.append(("atr_rollback", "%s:%s" % (dev, bank)))
            except Exception as exc:
                logger.critical("ATR rollback of %s:%s failed: %r", dev, bank, exc)
        self.force_safe()
        self.state["atr"] = False

    def status(self) -> dict:
        return {"state": ac.to_jsonable(self.state), "pa_lines": [str(x) for x in self.pa_lines],
                "tr_line": str(self.tr_line), "lna_line": str(self.lna_line),
                "atr": self.atr_active}
