"""
    hw_controller -> delay_sync relay helpers (state document + recal request)

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Pure, stdlib-only helpers shared by the writer (hw_controller) and the
    reader (delay_sync) of two opt-in control files (design spec G.3, G.5):

    * ``_data_control/<P>hwc_state`` — one JSON document (``"v": 1``) with the
      hw_controller FSM/control snapshot and its last <= 32 events, written
      only under ``[monitoring] en_hwc_state_relay = 1``. Every event carries a
      per-process ``seq``; the document's ``epoch`` (``"<pid>-<start_ms>"``)
      changes on every hw_controller restart, so readers dedupe events by
      ``(epoch, seq)``.
    * ``_data_control/<P>recal_request`` — one line
      ``"<epoch> <gen> <unix_ms> <reason>\\n"`` written by the RECL verb under
      ``[calibration] en_wire_recal = 1``.

    Writes are atomic (unique temp file in the same directory + os.replace),
    so a concurrent reader sees the old or the new document, never a torn
    one. Readers take a baseline at start and never act on a leftover file.
"""
import json
import os
import tempfile
import time

#: Version of the hwc_state document layout written by write_hwc_state callers.
HWC_STATE_VERSION = 1

CONTROL_DIR = "_data_control"
HWC_STATE_NAME = "hwc_state"
RECAL_REQUEST_NAME = "recal_request"
RECAL_REASON_WIRE = "wire"


def hwc_state_path(prefix="", control_dir=CONTROL_DIR):
    """``_data_control/<P>hwc_state`` (``prefix`` = "" for instance 0, ``inst<N>_`` else)."""
    return os.path.join(control_dir, prefix + HWC_STATE_NAME)


def recal_request_path(prefix="", control_dir=CONTROL_DIR):
    """``_data_control/<P>recal_request``."""
    return os.path.join(control_dir, prefix + RECAL_REQUEST_NAME)


def make_epoch(pid=None, start_ms=None):
    """Process epoch ``"<pid>-<start_ms>"``; compute once at process start."""
    if pid is None:
        pid = os.getpid()
    if start_ms is None:
        start_ms = int(time.time() * 1000)
    return "{}-{}".format(int(pid), int(start_ms))


def _atomic_write_bytes(path, data, temp_prefix):
    """Write *data* to *path* via a unique temp file + os.replace.

    The temp file is created in the target directory (same filesystem), is
    mode 0644 and is removed on any error; OSError is re-raised.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=temp_prefix)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(data)
            f.flush()
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# hwc_state document (G.3)
# ---------------------------------------------------------------------------

def write_hwc_state(path, doc):
    """Atomically write the relay document *doc* (a dict) as compact JSON.

    Serialisation happens before any file is touched (TypeError/ValueError
    for a non-serialisable document). File errors are re-raised as OSError
    after the temp file is removed; the caller decides whether to log them.
    Safe to call from several threads/processes on the same path.
    """
    if not isinstance(doc, dict):
        raise TypeError("hwc_state document must be a dict")
    data = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    _atomic_write_bytes(path, data, ".hwc_state.")


def read_hwc_state(path):
    """Read the relay document; None when missing, torn, or not a valid document.

    A valid document is a JSON object with an integer ``v`` >= 1, a string
    ``epoch`` and, when present, a list ``events``.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    v = doc.get("v")
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        return None
    if not isinstance(doc.get("epoch"), str):
        return None
    if "events" in doc and not isinstance(doc["events"], list):
        return None
    return doc


def _event_seq(ev):
    if not isinstance(ev, dict):
        return None
    s = ev.get("seq")
    if isinstance(s, bool) or not isinstance(s, int):
        return None
    return s


def new_events_since(doc, last_epoch, last_seq):
    """Events of *doc* not seen yet -> ``(events, epoch, last_seq)``.

    An event is new iff the document's epoch differs from *last_epoch*
    (hw_controller restarted: every event is new) or its ``seq`` is greater
    than *last_seq*. The returned baseline is ``(doc epoch, max seq seen)``;
    after an epoch change without events it is ``(epoch, -1)``. Events
    without an integer ``seq`` are ignored. ``doc=None`` returns
    ``([], last_epoch, last_seq)`` unchanged.
    """
    if doc is None:
        return [], last_epoch, last_seq
    epoch = doc.get("epoch")
    events = [ev for ev in doc.get("events") or [] if _event_seq(ev) is not None]
    if epoch != last_epoch:
        fresh = events
        new_last = -1
    else:
        fresh = [ev for ev in events if _event_seq(ev) > last_seq]
        new_last = last_seq
    for ev in fresh:
        new_last = max(new_last, _event_seq(ev))
    return fresh, epoch, new_last


def hwc_status_view(doc, now=None):
    """The document minus ``events`` plus ``age_sec`` (now - ``ts``), for STATUS.hwc / HWC."""
    if doc is None:
        return None
    view = {k: v for k, v in doc.items() if k != "events"}
    ts = doc.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        if now is None:
            now = time.time()
        view["age_sec"] = round(max(0.0, now - ts), 3)
    else:
        view["age_sec"] = None
    return view


class HwcStateWatcher(object):
    """Reader-side state machine for the hwc_state relay (delay_sync, G.3).

    The constructor reads the file once and records ``(epoch, max seq)`` as
    the baseline without reporting anything, so a leftover document from a
    previous run is never replayed. ``poll()`` then returns
    ``(doc, new_events)``: the current document (None when missing/torn,
    counted in ``errors``) and the events not reported before.
    """

    def __init__(self, path, reader=read_hwc_state):
        self.path = path
        self._reader = reader
        self.errors = 0
        _events, self.last_epoch, self.last_seq = new_events_since(
            reader(path), None, -1)

    def poll(self):
        doc = self._reader(self.path)
        if doc is None:
            self.errors += 1
            return None, []
        events, self.last_epoch, self.last_seq = new_events_since(
            doc, self.last_epoch, self.last_seq)
        return doc, events


# ---------------------------------------------------------------------------
# recal_request (G.5)
# ---------------------------------------------------------------------------

def _check_token(name, value):
    value = str(value)
    if not value or any(c.isspace() for c in value):
        raise ValueError("{} must be a non-empty token without whitespace: {!r}".format(
            name, value))
    return value


def write_recal_request(path, epoch, gen, reason=RECAL_REASON_WIRE, ts_ms=None):
    """Atomically write ``"<epoch> <gen> <unix_ms> <reason>\\n"``; returns ts_ms.

    *gen* is the hw_controller ``recal_generation`` (int >= 0). OSError is
    re-raised after the temp file is removed.
    """
    epoch = _check_token("epoch", epoch)
    reason = _check_token("reason", reason)
    if isinstance(gen, bool) or not isinstance(gen, int) or gen < 0:
        raise ValueError("gen must be an int >= 0: {!r}".format(gen))
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    line = "{} {} {} {}\n".format(epoch, gen, int(ts_ms), reason)
    _atomic_write_bytes(path, line.encode("utf-8"), ".recal_request.")
    return int(ts_ms)


def read_recal_request(path):
    """Parse the recal request -> ``(epoch, gen, ts_ms, reason)``, or None.

    None when the file is missing or malformed (not exactly four tokens,
    non-integer or negative gen/ts).
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(4096)
    except OSError:
        return None
    try:
        parts = raw.decode("utf-8").split()
    except UnicodeDecodeError:
        return None
    if len(parts) != 4:
        return None
    epoch, gen_s, ts_s, reason = parts
    if not (gen_s.isdigit() and gen_s.isascii() and ts_s.isdigit() and ts_s.isascii()):
        return None
    return epoch, int(gen_s), int(ts_s), reason


class RecalWatcher(object):
    """delay_sync's wire-recalibration watcher (G.5).

    The constructor reads the request file (if any) and records
    ``(epoch, gen)`` as the baseline **without acting**. ``poll()`` (every 5
    frames) returns the request ``(epoch, gen, ts_ms, reason)`` to act on
    when the file's epoch differs from the baseline (a new hw_controller
    process issued it) or its gen is greater within the same epoch, and
    then moves the baseline; otherwise None.
    """

    def __init__(self, path, reader=read_recal_request):
        self.path = path
        self._reader = reader
        req = reader(path)
        if req is None:
            self.last_epoch, self.last_gen = None, -1
        else:
            self.last_epoch, self.last_gen = req[0], req[1]

    def poll(self):
        req = self._reader(self.path)
        if req is None:
            return None
        epoch, gen = req[0], req[1]
        if epoch != self.last_epoch or gen > self.last_gen:
            self.last_epoch, self.last_gen = epoch, gen
            return req
        return None
