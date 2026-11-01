"""
    HeIMDALL "active" capability: shared foundation.

    Leaf module (stdlib + numpy only) used by every other ``_active`` module:

      - repository paths and per-instance runtime directories;
      - CLI exit codes and the exception hierarchy;
      - ``Issue`` (validation finding) helpers;
      - ``DevTime``: exact device time (integer seconds + integer ticks at an
        integer tick rate). Floats are never used on scheduling paths: at a
        device epoch of ~1.8e9 s a float64 resolves only ~238 ns, coarser than
        one 5 ns tick at 200 MHz;
      - ``ChannelRef``, ``AsyncEvent`` and ``TxResult`` value types;
      - unit / angle helpers (dB, wavelength, azimuth wrapping);
      - INI list parsers that broadcast or reject (never silently truncate);
      - ``rng_stream``: per-purpose deterministic random streams (zlib.crc32
        keyed, never the salted built-in ``hash()``);
      - atomic text/JSON writes and sha256 helpers;
      - ``EventLog``: JSONL event journal in the ``daq_events.DAQEvent`` shape.

    No hardware access, no third-party hardware libraries.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import math
import operator
import os
import sys
import tempfile
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass, field, is_dataclass, fields as dc_fields
from fractions import Fraction

import numpy as np

logger = logging.getLogger("heimdall.active")

# ---------------------------------------------------------------------------
# Physical constants and paths
# ---------------------------------------------------------------------------
C0 = 299_792_458.0

ACTIVE_DIR = os.path.dirname(os.path.realpath(__file__))
FIRMWARE_DIR = os.path.dirname(ACTIVE_DIR)
REPO_ROOT = os.path.dirname(FIRMWARE_DIR)
DAQ_CORE_DIR = os.path.join(FIRMWARE_DIR, "_daq_core")
CONFIG_FILES_DIR = os.path.join(REPO_ROOT, "config_files")
DEFAULT_CONFIG = CONFIG_FILES_DIR + "/active_sim_ula8/active_chain_config.ini"


def ensure_daq_core_path() -> str:
    """Idempotently put ``Firmware/_daq_core`` first on ``sys.path``.

    Returns the directory. Needed before importing the reused read-only
    modules (iq_header, gain_budget, antenna_profile, daq_events).
    """
    if DAQ_CORE_DIR not in sys.path:
        sys.path.insert(0, DAQ_CORE_DIR)
    return DAQ_CORE_DIR


def _runtime_dir(base: str, instance_id: int, override: str) -> str:
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    else:
        iid = int(instance_id)
        if iid < 0:
            raise ValueError("instance_id must be >= 0")
        path = os.path.join(base, "active")
        if iid > 0:
            path = os.path.join(path, "inst%d" % iid)
    os.makedirs(path, exist_ok=True)
    return path


def state_dir(instance_id: int = 0, override: str = "") -> str:
    """Runtime state directory: ``Firmware/_data_control/active[/instN]``.

    ``override`` (absolute, or relative to the cwd) replaces the default. The
    directory is created when missing.
    """
    return _runtime_dir(os.path.join(FIRMWARE_DIR, "_data_control"), instance_id, override)


def log_dir(instance_id: int = 0, override: str = "") -> str:
    """Log directory: ``Firmware/_logs/active[/instN]`` (created when missing)."""
    return _runtime_dir(os.path.join(FIRMWARE_DIR, "_logs"), instance_id, override)


# ---------------------------------------------------------------------------
# Exit codes and exceptions
# ---------------------------------------------------------------------------
EXIT_OK, EXIT_INVALID, EXIT_USAGE, EXIT_BACKEND, EXIT_SAFETY, EXIT_CAL = 0, 1, 2, 3, 4, 5


class ActiveError(Exception):
    """Base class of every error raised by the active capability."""


class ConfigError(ActiveError):
    """Configuration could not be parsed or is invalid.

    ``issues`` holds the ``Issue`` records that caused the failure.
    """

    def __init__(self, msg, issues=()):
        super().__init__(msg)
        self.issues = tuple(issues)


class GeometryError(ActiveError):
    """Array geometry is malformed or inconsistent."""


class BackendError(ActiveError):
    """A radio backend failed."""


class BackendUnavailable(BackendError):
    """The library a backend needs is missing; ``hint`` names the package."""

    def __init__(self, msg, hint=""):
        super().__init__(msg if not hint else "%s (hint: %s)" % (msg, hint))
        self.hint = hint


class NotSupported(BackendError):
    """The requested feature does not exist on this device family."""


class HardwareFault(BackendError):
    """The hardware reported or exhibited a fault."""


class _CodedError(ActiveError):
    """Error carrying a stable machine-readable ``code`` and a ``detail``."""

    def __init__(self, code, detail=""):
        self.code = str(code)
        self.detail = str(detail)
        super().__init__("%s: %s" % (self.code, self.detail) if self.detail else self.code)


class CoherenceError(_CodedError):
    """Timing / coherence could not be established or verified."""


class CalibrationError(_CodedError):
    """A calibration failed its acceptance criteria."""


class SafetyRefusal(_CodedError):
    """A TX request was refused. No state change, no RF emitted."""


class InterlockTrip(_CodedError):
    """A TX safety trip: the controller is latched in FAULT."""


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------
ISSUE_LEVELS = ("error", "warning", "info")


@dataclass(frozen=True)
class Issue:
    """One validation finding with a stable ``code``."""
    level: str
    code: str
    message: str
    where: str = ""

    def __post_init__(self):
        if self.level not in ISSUE_LEVELS:
            raise ValueError("Issue level must be one of %s, got %r" % (ISSUE_LEVELS, self.level))

    def to_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message, "where": self.where}

    def __str__(self):
        loc = " (%s)" % self.where if self.where else ""
        return "%s %s: %s%s" % (self.level.upper(), self.code, self.message, loc)


def has_errors(issues) -> bool:
    return any(i.level == "error" for i in issues)


def format_issues(issues) -> str:
    """Human-readable, one issue per line, errors first (stable order otherwise)."""
    order = {lvl: k for k, lvl in enumerate(ISSUE_LEVELS)}
    ranked = sorted(enumerate(issues), key=lambda p: (order.get(p[1].level, 9), p[0]))
    return "\n".join(str(i) for _, i in ranked)


def issues_to_json(issues) -> list:
    return [i.to_dict() for i in issues]


# ---------------------------------------------------------------------------
# Exact device time
# ---------------------------------------------------------------------------
def _as_int(x, name):
    if isinstance(x, bool):
        raise TypeError("%s must be an integer, not bool" % name)
    try:
        return operator.index(x)
    except TypeError:
        raise TypeError("%s must be an integer, got %r" % (name, x)) from None


def as_fraction(x) -> Fraction:
    """Exact rational value of ``x`` (int, Fraction, float, Decimal or str).

    A float that is the double nearest to a short rational (denominator up to
    1e6, e.g. ``325e6 / 3``) maps to that rational, so sample-rate arithmetic
    on configured rates stays exact. Any other float maps to its exact binary
    value.
    """
    if isinstance(x, Fraction):
        return x
    if isinstance(x, bool):
        raise TypeError("expected a number, got bool")
    if isinstance(x, (int, np.integer)):
        return Fraction(int(x))
    if isinstance(x, (float, np.floating)):
        xf = float(x)
        if not math.isfinite(xf):
            raise ValueError("non-finite value %r" % x)
        exact = Fraction(xf)
        if exact.denominator == 1:
            return exact
        approx = exact.limit_denominator(1_000_000)
        if float(approx) == xf:
            return approx
        return exact
    return Fraction(x)


@dataclass(frozen=True, eq=False)
class DevTime:
    """Exact device time: ``secs + ticks / tick_rate`` seconds.

    Construction normalises ``0 <= ticks < tick_rate`` (carrying into
    ``secs``). Comparisons and hashing are exact across tick rates (via
    ``to_fraction``); ``diff_ticks`` requires equal tick rates. ``DevTime(k)``
    is the whole second ``k``.
    """
    secs: int
    ticks: int = 0
    tick_rate: int = 1

    def __post_init__(self):
        secs = _as_int(self.secs, "secs")
        ticks = _as_int(self.ticks, "ticks")
        rate = _as_int(self.tick_rate, "tick_rate")
        if rate <= 0:
            raise ValueError("tick_rate must be > 0, got %d" % rate)
        carry, ticks = divmod(ticks, rate)
        object.__setattr__(self, "secs", secs + carry)
        object.__setattr__(self, "ticks", ticks)
        object.__setattr__(self, "tick_rate", rate)

    # --- construction -----------------------------------------------------
    @staticmethod
    def from_seconds(t, tick_rate: int) -> "DevTime":
        """Nearest tick to ``t`` seconds (round half to even)."""
        rate = _as_int(tick_rate, "tick_rate")
        if rate <= 0:
            raise ValueError("tick_rate must be > 0")
        total = round(as_fraction(t) * rate)
        return DevTime(0, int(total), rate)

    @staticmethod
    def from_timespec(full_secs: int, frac_secs: float, tick_rate: int) -> "DevTime":
        """From a ``uhd.types.TimeSpec`` (full seconds + fractional seconds)."""
        rate = _as_int(tick_rate, "tick_rate")
        if rate <= 0:
            raise ValueError("tick_rate must be > 0")
        if isinstance(full_secs, (float, np.floating)) and float(full_secs).is_integer():
            full_secs = int(full_secs)
        frac = Fraction(float(frac_secs))
        return DevTime(_as_int(full_secs, "full_secs"), int(round(frac * rate)), rate)

    # --- conversion -------------------------------------------------------
    def to_timespec(self) -> tuple:
        """``(int full_secs, float frac_secs)`` for ``uhd.types.TimeSpec``."""
        return (self.secs, self.ticks / self.tick_rate)

    def to_fraction(self) -> Fraction:
        return self.secs + Fraction(self.ticks, self.tick_rate)

    def to_seconds(self) -> float:
        """Float seconds. Display only: never schedule with this value."""
        return float(self.to_fraction())

    def to_unix_ms(self, epoch_offset_s: float) -> int:
        """Unix milliseconds (floor), given unix = device time + epoch_offset_s."""
        return math.floor((self.to_fraction() + as_fraction(epoch_offset_s)) * 1000)

    # --- arithmetic -------------------------------------------------------
    def add_seconds(self, dt) -> "DevTime":
        """Add ``dt`` seconds (float/Fraction/int), rounded to the nearest tick."""
        return self.add_ticks(int(round(as_fraction(dt) * self.tick_rate)))

    def add_ticks(self, n: int) -> "DevTime":
        return DevTime(self.secs, self.ticks + _as_int(n, "n"), self.tick_rate)

    def with_tick_rate(self, tick_rate: int) -> "DevTime":
        """The same instant on another tick grid (rounded to the nearest tick)."""
        return DevTime.from_seconds(self.to_fraction(), tick_rate)

    def diff_ticks(self, other: "DevTime") -> int:
        """``self - other`` in ticks. ValueError when the tick rates differ."""
        if self.tick_rate != other.tick_rate:
            raise ValueError("diff_ticks needs equal tick rates (%d != %d)"
                             % (self.tick_rate, other.tick_rate))
        return (self.secs - other.secs) * self.tick_rate + (self.ticks - other.ticks)

    def diff_seconds(self, other: "DevTime") -> Fraction:
        """``self - other`` in exact seconds (any tick rates)."""
        return self.to_fraction() - other.to_fraction()

    def sample_offset(self, t0: "DevTime", fs_hz: float) -> int:
        """Sample index of this time in a stream starting at ``t0`` at ``fs_hz``.

        ``round((self - t0) * fs)`` with exact rational arithmetic (half-even).
        """
        return int(round(self.diff_seconds(t0) * as_fraction(fs_hz)))

    # --- comparisons (exact) ----------------------------------------------
    def _cmp_key(self, other):
        if not isinstance(other, DevTime):
            return NotImplemented
        return other.to_fraction()

    def __eq__(self, other):
        o = self._cmp_key(other)
        return o if o is NotImplemented else self.to_fraction() == o

    def __ne__(self, other):
        o = self._cmp_key(other)
        return o if o is NotImplemented else self.to_fraction() != o

    def __lt__(self, other):
        o = self._cmp_key(other)
        return o if o is NotImplemented else self.to_fraction() < o

    def __le__(self, other):
        o = self._cmp_key(other)
        return o if o is NotImplemented else self.to_fraction() <= o

    def __gt__(self, other):
        o = self._cmp_key(other)
        return o if o is NotImplemented else self.to_fraction() > o

    def __ge__(self, other):
        o = self._cmp_key(other)
        return o if o is NotImplemented else self.to_fraction() >= o

    def __hash__(self):
        return hash(self.to_fraction())

    def to_dict(self) -> dict:
        return {"secs": self.secs, "ticks": self.ticks, "tick_rate": self.tick_rate}

    @staticmethod
    def from_dict(d) -> "DevTime":
        return DevTime(int(d["secs"]), int(d.get("ticks", 0)), int(d.get("tick_rate", 1)))

    def __repr__(self):
        return "DevTime(%d, %d, %d)" % (self.secs, self.ticks, self.tick_rate)


# ---------------------------------------------------------------------------
# Value types shared by backends and safety
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChannelRef:
    """A physical channel: ``[active_device:<device>]`` local channel index."""
    device: str
    channel: int

    def __str__(self):
        return "%s:%d" % (self.device, self.channel)

    def to_dict(self) -> dict:
        return {"device": self.device, "channel": self.channel}


ASYNC_EVENT_KINDS = ("underflow", "underflow_in_packet", "late", "seq_error", "burst_ack",
                     "overflow", "ref_unlock", "lo_unlock", "time_error")


@dataclass
class AsyncEvent:
    """Asynchronous device event (kind in ``ASYNC_EVENT_KINDS``)."""
    kind: str
    chan: int = None
    time: DevTime = None
    detail: str = ""


@dataclass
class TxResult:
    """Outcome of one TX burst as reported by a backend."""
    ok: bool
    sent_samples: int
    underflows: int = 0
    late: int = 0
    seq_errors: int = 0
    acked: bool = False
    content_corrupt: bool = False
    events: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Units and angles
# ---------------------------------------------------------------------------
def _scalar_or_array(fn, x):
    arr = np.asarray(x, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        out = fn(arr)
    if np.ndim(out) == 0:
        return float(out)
    return out


def wavelength(f_hz) -> float:
    f = float(f_hz)
    if not f > 0.0 or not math.isfinite(f):
        raise ValueError("frequency must be finite and > 0, got %r" % f_hz)
    return C0 / f


def k0(f_hz) -> float:
    """Free-space wavenumber 2*pi/lambda (rad/m)."""
    return 2.0 * math.pi / wavelength(f_hz)


def db10(x):
    """10*log10(x) (power ratio); 0 -> -inf. Scalars return float."""
    return _scalar_or_array(lambda a: 10.0 * np.log10(a), x)


def db20(x):
    """20*log10(|x|) (amplitude ratio); 0 -> -inf."""
    return _scalar_or_array(lambda a: 20.0 * np.log10(np.abs(a)), np.abs(np.asarray(x)))


def from_db10(x_db):
    return _scalar_or_array(lambda a: 10.0 ** (a / 10.0), x_db)


def from_db20(x_db):
    return _scalar_or_array(lambda a: 10.0 ** (a / 20.0), x_db)


def dbm_to_w(p_dbm):
    return _scalar_or_array(lambda a: 10.0 ** ((a - 30.0) / 10.0), p_dbm)


def w_to_dbm(p_w):
    return _scalar_or_array(lambda a: 10.0 * np.log10(a) + 30.0, p_w)


def wrap_deg(x):
    """Wrap degrees into [-180, 180)."""
    if np.ndim(x) == 0:
        r = (float(x) + 180.0) % 360.0 - 180.0
        if r >= 180.0:
            r -= 360.0
        return r
    a = (np.asarray(x, dtype=float) + 180.0) % 360.0 - 180.0
    return np.where(a >= 180.0, a - 360.0, a)


def wrap_az(x):
    """Wrap a compass azimuth into [0, 360)."""
    if np.ndim(x) == 0:
        r = float(x) % 360.0
        if r >= 360.0:
            r -= 360.0
        return r
    a = np.asarray(x, dtype=float) % 360.0
    return np.where(a >= 360.0, a - 360.0, a)


# ---------------------------------------------------------------------------
# INI value parsers
# ---------------------------------------------------------------------------
_BOOL_STRICT = {"0": False, "1": True}
_BOOL_LOOSE = {"no": False, "false": False, "off": False, "yes": True, "true": True, "on": True}


def _err(code, msg, where):
    return ConfigError("%s%s" % (msg, " (%s)" % where if where else ""),
                       (Issue("error", code, msg, where),))


def parse_bool(s, where="", issues=None) -> bool:
    """Parse ``0``/``1``.

    ``yes/no/true/false/on/off`` are accepted; a ``BOOL_SPELLING`` warning
    ``Issue`` is appended to ``issues`` (when a list is given). Anything else
    raises ``ConfigError`` (code ``TYPE``).
    """
    if isinstance(s, bool):
        return s
    if isinstance(s, (int, np.integer)) and int(s) in (0, 1):
        return bool(int(s))
    t = str(s).strip().lower()
    if t in _BOOL_STRICT:
        return _BOOL_STRICT[t]
    if t in _BOOL_LOOSE:
        if issues is not None:
            issues.append(Issue("warning", "BOOL_SPELLING",
                                "boolean %r accepted; write 0 or 1" % str(s).strip(), where))
        return _BOOL_LOOSE[t]
    raise _err("TYPE", "expected a boolean 0/1, got %r" % s, where)


def _to_float(tok, where):
    try:
        v = float(tok)
    except (TypeError, ValueError):
        raise _err("TYPE", "expected a number, got %r" % tok, where) from None
    if not math.isfinite(v):
        raise _err("TYPE", "non-finite number %r" % tok, where)
    return v


def _to_int(tok, where):
    t = str(tok).strip()
    try:
        return int(t, 0)
    except ValueError:
        pass
    try:
        f = float(t)
    except ValueError:
        raise _err("TYPE", "expected an integer, got %r" % tok, where) from None
    if not math.isfinite(f) or f != int(f):
        raise _err("TYPE", "expected an integer, got %r" % tok, where)
    return int(f)


def _split(s, sep):
    return [t.strip() for t in str(s).split(sep)]


def parse_float_list(s, n, default, where="") -> list:
    """Per-chain list: ``""`` -> ``[default]*n``; one value -> broadcast; exactly
    ``n`` comma-separated values; any other length -> ``ConfigError`` (LIST_LEN).

    ``default`` may be None (for "not set" entries).
    """
    n = int(n)
    text = "" if s is None else str(s).strip()
    if text == "":
        return [default] * n
    toks = _split(text, ",")
    if any(t == "" for t in toks):
        raise _err("TYPE", "empty element in list %r" % text, where)
    vals = [_to_float(t, where) for t in toks]
    if len(vals) == 1:
        return vals * n
    if len(vals) != n:
        raise _err("LIST_LEN", "expected 1 or %d values, got %d" % (n, len(vals)), where)
    return vals


def parse_int_list(s, n=None, default=None, where="") -> list:
    """Comma-separated integers; ``a..b`` expands to an inclusive range.

    With ``n`` None any length is accepted (``""`` -> ``[]``). With ``n`` set the
    broadcast/length rules of ``parse_float_list`` apply.
    """
    text = "" if s is None else str(s).strip()
    if text == "":
        return [] if n is None else [default] * int(n)
    vals = []
    for t in _split(text, ","):
        if t == "":
            raise _err("TYPE", "empty element in list %r" % text, where)
        if ".." in t:
            a, _, b = t.partition("..")
            lo, hi = _to_int(a, where), _to_int(b, where)
            if hi < lo:
                raise _err("RANGE", "descending range %r" % t, where)
            vals.extend(range(lo, hi + 1))
        else:
            vals.append(_to_int(t, where))
    if n is None:
        return vals
    n = int(n)
    if len(vals) == 1:
        return vals * n
    if len(vals) != n:
        raise _err("LIST_LEN", "expected 1 or %d values, got %d" % (n, len(vals)), where)
    return vals


def parse_bands_hz(s, where="") -> list:
    """``"902e6:928e6; 2400e6:2483.5e6"`` -> ``[(lo, hi), ...]`` with lo < hi."""
    out = []
    text = "" if s is None else str(s).strip()
    if not text:
        return out
    for ent in _split(text, ";"):
        if not ent:
            continue
        parts = _split(ent, ":")
        if len(parts) != 2:
            raise _err("TYPE", "band %r must be lo:hi" % ent, where)
        lo, hi = _to_float(parts[0], where), _to_float(parts[1], where)
        if not lo < hi:
            raise _err("RANGE", "band %r needs lo < hi" % ent, where)
        if lo < 0:
            raise _err("RANGE", "band %r has a negative frequency" % ent, where)
        out.append((lo, hi))
    return out


def parse_az_el_list(s, where="") -> list:
    """``"75:0; 120:10"`` -> ``[(az_deg, el_deg), ...]``; el must be in [-90, 90]."""
    out = []
    text = "" if s is None else str(s).strip()
    if not text:
        return out
    for ent in _split(text, ";"):
        if not ent:
            continue
        parts = _split(ent, ":")
        if len(parts) != 2:
            raise _err("TYPE", "direction %r must be az:el" % ent, where)
        az, el = _to_float(parts[0], where), _to_float(parts[1], where)
        if not -90.0 <= el <= 90.0:
            raise _err("RANGE", "elevation %r outside [-90, 90]" % ent, where)
        out.append((az, el))
    return out


def parse_kv_list(s, where="") -> dict:
    """``"path=value; path=value"`` -> ordered dict (duplicate keys rejected)."""
    out = {}
    text = "" if s is None else str(s).strip()
    if not text:
        return out
    for ent in _split(text, ";"):
        if not ent:
            continue
        key, sep, val = ent.partition("=")
        key, val = key.strip(), val.strip()
        if not sep or not key:
            raise _err("TYPE", "entry %r must be key=value" % ent, where)
        if key in out:
            raise _err("TYPE", "duplicate key %r" % key, where)
        out[key] = val
    return out


# ---------------------------------------------------------------------------
# Deterministic random streams
# ---------------------------------------------------------------------------
def rng_stream(seed: int, *keys) -> np.random.Generator:
    """Independent deterministic stream for ``(seed, *keys)``.

    ``SeedSequence([seed] + [crc32(str(k)) for k in keys])``. Stable across
    runs, processes and platforms (never uses the salted ``hash()``).
    """
    seed = _as_int(seed, "seed")
    if seed < 0:
        raise ValueError("seed must be >= 0")
    entropy = [seed] + [zlib.crc32(str(k).encode("utf-8")) for k in keys]
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence(entropy)))


# ---------------------------------------------------------------------------
# JSON-friendly conversion, atomic writes, hashing
# ---------------------------------------------------------------------------
def to_jsonable(obj):
    """Recursively convert numpy / dataclass / complex / DevTime values to JSON types.

    Complex numbers become ``[re, im]``; Fractions become floats; enums their
    value; sets are sorted lists.
    """
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    if isinstance(obj, enum.Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, (complex, np.complexfloating)):
        return [float(obj.real), float(obj.imag)]
    if isinstance(obj, Fraction):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return to_jsonable(obj.tolist())
    if isinstance(obj, DevTime):
        return obj.to_dict()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dc_fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted((to_jsonable(v) for v in obj), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(obj, (list, tuple, deque)):
        return [to_jsonable(v) for v in obj]
    return str(obj)


def atomic_write_text(path, text, mode=0o644) -> None:
    """Write ``text`` to ``path`` atomically: temp file in the same directory,
    fsync, chmod, ``os.replace``, then a best-effort directory fsync.

    Readers never see a partial file, and the final file never exists with
    permissions wider than ``mode``.
    """
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def atomic_write_json(path, obj, mode=0o644) -> None:
    """``atomic_write_text`` of ``obj`` as sorted, indented JSON (``to_jsonable``)."""
    atomic_write_text(path, json.dumps(to_jsonable(obj), indent=2, sort_keys=True) + "\n", mode)


def sha256_bytes(b) -> str:
    return hashlib.sha256(bytes(b)).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
EVT_ACTIVE_OPEN = "active_open"
EVT_ACTIVE_FALLBACK = "active_fallback"
EVT_ACTIVE_SYNC_OK = "active_sync_ok"
EVT_ACTIVE_SYNC_FAIL = "active_sync_fail"
EVT_ACTIVE_TUNE = "active_tune"
EVT_ACTIVE_BAND_CHANGE = "active_band_change"
EVT_ACTIVE_COHERENCE_BUMP = "active_coherence_bump"
EVT_ACTIVE_COHERENCE_FAIL = "active_coherence_fail"
EVT_ACTIVE_CAL_START = "active_cal_start"
EVT_ACTIVE_CAL_DONE = "active_cal_done"
EVT_ACTIVE_CAL_FAIL = "active_cal_fail"
EVT_ACTIVE_CAL_INVALID = "active_cal_invalid"
EVT_ACTIVE_BEAM = "active_beam"
EVT_ACTIVE_TX_ARM = "active_tx_arm"
EVT_ACTIVE_TX_DISARM = "active_tx_disarm"
EVT_ACTIVE_TX_EXPIRE = "active_tx_expire"
EVT_ACTIVE_TX_BURST = "active_tx_burst"
EVT_ACTIVE_TX_REFUSED = "active_tx_refused"
EVT_ACTIVE_TX_TRIP = "active_tx_trip"
EVT_ACTIVE_TX_FAULT_RESET = "active_tx_fault_reset"
EVT_ACTIVE_SHUTDOWN = "active_shutdown"
EVT_ACTIVE_STATION_ID = "active_station_id"
EVT_ACTIVE_OVERFLOW = "active_overflow"
EVT_ACTIVE_UNDERFLOW = "active_underflow"
EVT_ACTIVE_CLIENT = "active_client"
EVT_ACTIVE_SIM_DAMAGE = "active_sim_damage"

EVENT_TYPES = tuple(v for k, v in sorted(globals().items()) if k.startswith("EVT_ACTIVE_"))

_SEVERITY_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
                    "error": logging.ERROR, "critical": logging.CRITICAL}


class EventLog:
    """Structured event journal.

    Each ``emit`` produces a dict in the ``daq_events.DAQEvent.to_dict()``
    shape (``timestamp, severity, module, event_type, payload``), keeps it in
    an in-memory ring, appends it as one JSON line to ``jsonl_path`` (when
    given), forwards it to an optional ``daq_events.EventBus`` and logs it.
    ``emit`` never raises: a write failure is counted in ``write_errors`` and
    logged once.
    """

    def __init__(self, jsonl_path: str = None, event_bus=None, module: str = "active",
                 ring: int = 1000, clock=time.time):
        self.jsonl_path = jsonl_path
        self.event_bus = event_bus
        self.module = module
        self._clock = clock
        self._ring = deque(maxlen=max(1, int(ring)))
        self._lock = threading.Lock()
        self.write_errors = 0
        self._warned = False
        self._daq_event_cls = None

    def _bus_event(self, d):
        if self._daq_event_cls is None:
            ensure_daq_core_path()
            from daq_events import DAQEvent  # lazy: optional dependency
            self._daq_event_cls = DAQEvent
        return self._daq_event_cls(timestamp=d["timestamp"], severity=d["severity"],
                                   module=d["module"], event_type=d["event_type"],
                                   payload=dict(d["payload"]))

    def emit(self, event_type: str, severity: str = "info", **payload) -> dict:
        try:
            try:
                ts = float(self._clock())
            except Exception:
                ts = time.time()
            d = {"timestamp": ts, "severity": str(severity), "module": self.module,
                 "event_type": str(event_type), "payload": to_jsonable(payload)}
        except Exception as exc:  # pragma: no cover - to_jsonable falls back to str()
            d = {"timestamp": time.time(), "severity": "error", "module": self.module,
                 "event_type": str(event_type), "payload": {"emit_error": repr(exc)}}
        with self._lock:
            self._ring.append(d)
            if self.jsonl_path:
                try:
                    parent = os.path.dirname(os.path.abspath(self.jsonl_path))
                    os.makedirs(parent, exist_ok=True)
                    with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(d, sort_keys=True) + "\n")
                except Exception as exc:
                    self.write_errors += 1
                    if not self._warned:
                        self._warned = True
                        try:
                            logger.warning("EventLog: cannot write %s: %s", self.jsonl_path, exc)
                        except Exception:
                            pass
        if self.event_bus is not None:
            try:
                self.event_bus.emit(self._bus_event(d))
            except Exception:
                pass
        try:
            logger.log(_SEVERITY_LEVELS.get(d["severity"], logging.INFO), "[%s] %s",
                       d["event_type"], json.dumps(d["payload"], sort_keys=True))
        except Exception:
            pass
        return d

    def recent(self, n: int = 100, event_type: str = None) -> list:
        """The last ``n`` events (oldest first), optionally of one type."""
        with self._lock:
            items = list(self._ring)
        if event_type is not None:
            items = [e for e in items if e["event_type"] == event_type]
        n = int(n)
        return items[-n:] if n > 0 else []

    def __len__(self):
        with self._lock:
            return len(self._ring)
