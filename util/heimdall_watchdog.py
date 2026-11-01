#!/usr/bin/env python3
"""HeIMDALL DAQ watchdog for systemd-supervised chains (spec H.4).

Run once per invocation, normally by ``heimdall-daq-watchdog.timer``
(every 30 s, starting 120 s after boot) through
``heimdall-daq-watchdog.service``:

    heimdall_watchdog.py [--run-dir /run/heimdall] [--fails 3] [--grace auto|SEC]
                         [--stall-sec auto|SEC] [--timeout 2] [--dry-run] [--verbose]

Each run reads the host instance registry (``<run-dir>/inst<N>.json``,
written by the start scripts with ``[host] instance_registry = 1``) and, for
every live entry started by ``daq_supervise.sh`` under systemd
(``launcher == "systemd-supervised"``) whose status server is enabled
(``ports.status`` not null), sends ``STATUS`` to that port. A check fails
when the server is unreachable, when ``pipeline_health`` is ``error`` or
``stale``, or when ``frame_count`` has not changed for the stall threshold.
After ``--fails`` consecutive failing runs the entry's unit is restarted:
``systemctl reset-failed <unit>`` (clears the unit's start-rate counter, so
systemd does not refuse this restart) and then ``systemctl restart <unit>``.

Only chains that are still running are considered. A supervised unit that
hit its start limit has stopped its chain and removed its registry entry
(``ExecStopPost=daq_stop.sh N --clean``), so the watchdog never sees it:
recover it by hand with ``systemctl reset-failed <unit>`` and
``systemctl start <unit>``.

Every other entry (started by hand, by the oneshot units, by the supervisor
outside systemd, or with the status server disabled) is reported once and
never restarted. Counters live in ``<run-dir>/watchdog.json``, keyed by
``"<instance_id>@<started_at>"``: a restarted chain starts from zero, and
keys of chains that are gone are dropped.

Exit status: 0 the run completed (whether or not it restarted anything);
1 a systemctl command failed or the state file could not be written;
2 usage error. Standard library only.
"""
import argparse
import configparser
import errno
import glob
import json
import math
import os
import re
import socket
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

PROG = "heimdall_watchdog"
DEFAULT_RUN_DIR = "/run/heimdall"
STATE_FILE = "watchdog.json"
STATE_SCHEMA = 1
WATCHED_LAUNCHER = "systemd-supervised"
FAILING_HEALTH = ("error", "stale")
# Grace: max(GRACE_FLOOR_SEC, 3 x stale_after_sec), stale_after_sec from the
# extended STATUS when reported, else the G.2 default (k = 100, 0.437 s).
GRACE_FLOOR_SEC = 120.0
DEFAULT_STALE_AFTER_SEC = 132.0
SYSTEMCTL_TIMEOUT_SEC = 90
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@_.:\\-]*\.service$")


def log(msg):
    print("{}: {}".format(PROG, msg), flush=True)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def ini_stall_threshold(ini_path):
    """The supervisor's auto stall threshold (spec H.3) from an instance INI:
    ``3 x k_eff x T_fp + 30`` s with ``k_eff = status_update_interval`` when
    > 0, else ``heartbeat_interval`` (default 100), and
    ``T_fp = cpi_size x decimation_ratio / sample_rate``.

    Returns None when the INI is unreadable, the frame period is unknown or
    ``k_eff`` is 0 (the snapshot is never refreshed): no stall check."""
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with open(ini_path, "r", encoding="utf-8", errors="replace") as f:
            cfg.read_file(f)
    except (OSError, configparser.Error):
        return None

    def get(section, key, fallback=""):
        try:
            return cfg.get(section, key, fallback=fallback)
        except configparser.Error:
            return fallback

    sui = _num(get("monitoring", "status_update_interval", "0")) or 0.0
    hbi = _num(get("monitoring", "heartbeat_interval", "100"))
    k_eff = sui if sui > 0 else (hbi if hbi is not None else 100.0)
    cpi = _num(get("pre_processing", "cpi_size"))
    dec = _num(get("pre_processing", "decimation_ratio"))
    fs = _num(get("daq", "sample_rate"))
    if not (cpi and dec and fs) or cpi <= 0 or dec <= 0 or fs <= 0 or k_eff <= 0:
        return None
    return 3.0 * k_eff * (cpi * dec / fs) + 30.0


def stale_after_from_status(doc):
    """``stale_after_sec`` of an extended STATUS reply, or None."""
    if not isinstance(doc, dict):
        return None
    value = doc.get("stale_after_sec")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def auto_stall_threshold(doc, ini_path):
    """``3 x stale_after_sec + 30`` when the extended STATUS reports
    ``stale_after_sec``, else the H.3 formula from the INI (None = no stall
    check)."""
    stale = stale_after_from_status(doc)
    if stale is not None:
        return 3.0 * stale + 30.0
    return ini_stall_threshold(ini_path)


def auto_grace(doc):
    """``max(120, 3 x stale_after_sec)``; stale_after_sec defaults to 132."""
    stale = stale_after_from_status(doc)
    if stale is None:
        stale = DEFAULT_STALE_AFTER_SEC
    return max(GRACE_FLOOR_SEC, 3.0 * stale)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _proc_state_args(pid):
    """(state letter, args) of pid, or (None, None) when unknown."""
    proc = "/proc/{:d}".format(pid)
    if os.path.isdir("/proc/self"):
        try:
            with open(proc + "/stat", "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            with open(proc + "/cmdline", "rb") as f:
                args = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            return None, None
        tail = stat.rsplit(")", 1)[-1].split()
        return (tail[0] if tail else None), args
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-o", "args=", "-p", str(pid)],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=5, check=False).stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return None, None
    if not out:
        return None, None
    parts = out.split(None, 1)
    return parts[0][:1], (parts[1] if len(parts) > 1 else "")


def pid_role_alive(pid, role):
    """True when pid runs, is not a zombie and its command line contains role
    (the start scripts' ``hd_pid_role_alive``)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not role:
        return False
    try:
        os.kill(pid, 0)
    except OverflowError:
        return False
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    state, args = _proc_state_args(pid)
    if state is None or state == "Z":
        return False
    return role in args


def entry_live(entry):
    pids = entry.get("pids")
    if not isinstance(pids, dict):
        return False
    return any(pid_role_alive(pid, role) for role, pid in pids.items())


def _registry_index(path):
    m = re.search(r"inst(\d+)\.json$", path)
    return int(m.group(1)) if m else -1


def read_registry(run_dir):
    """[(path, entry)] of every readable ``inst<N>.json``, by instance id."""
    out = []
    for path in glob.glob(os.path.join(run_dir, "inst*.json")):
        if _registry_index(path) < 0:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError) as e:
            log("{}: unreadable registry entry ({}); skipped".format(path, e))
            continue
        if isinstance(entry, dict):
            out.append((path, entry))
    out.sort(key=lambda pe: (_registry_index(pe[0]), pe[0]))
    return out


def entry_key(entry):
    return "{}@{}".format(entry.get("instance_id"), entry.get("started_at"))


def entry_label(entry):
    unit = entry.get("unit") or ""
    return "instance {}{}".format(entry.get("instance_id"), " ({})".format(unit) if unit else "")


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------

def status_host(cwd):
    """Host to query: the instance's ``[daq] listen_address`` (wildcards map
    to loopback), like the supervisor."""
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    addr = ""
    try:
        with open(os.path.join(cwd, "daq_chain_config.ini"), "r", encoding="utf-8",
                  errors="replace") as f:
            cfg.read_file(f)
        addr = cfg.get("daq", "listen_address", fallback="").strip()
    except (OSError, configparser.Error):
        addr = ""
    if addr in ("", "0.0.0.0", "*"):
        return "127.0.0.1"
    if addr == "::":
        return "::1"
    return addr


def query_status(host, port, timeout):
    """Send ``STATUS`` and return the reply object; raises OSError/ValueError."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(b"STATUS\n")
        buf = b""
        deadline = time.monotonic() + timeout
        while b"\n" not in buf:
            if time.monotonic() > deadline:
                raise socket.timeout("no complete STATUS reply within {:g} s".format(timeout))
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    doc = json.loads(buf.decode("utf-8", "replace").strip() or "null")
    if not isinstance(doc, dict):
        raise ValueError("STATUS reply is not a JSON object")
    return doc


def _describe_exc(e):
    text = str(e)
    return e.__class__.__name__ + (": " + text if text else "")


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log("{}: unreadable state ({}); counters restart from 0".format(path, e))
        return {}
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA \
            or not isinstance(doc.get("entries"), dict):
        return {}
    return {k: v for k, v in doc["entries"].items() if isinstance(v, dict)}


def save_state(path, entries):
    tmp = "{}.{}.tmp".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"schema": STATE_SCHEMA, "entries": entries}, f, indent=1, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# systemctl
# ---------------------------------------------------------------------------

def systemctl(*args):
    """Run systemctl; returns (returncode, stripped stdout+stderr)."""
    try:
        p = subprocess.run(["systemctl"] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=SYSTEMCTL_TIMEOUT_SEC, check=False)
    except FileNotFoundError:
        return 127, "systemctl not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out after {} s".format(SYSTEMCTL_TIMEOUT_SEC)
    except OSError as e:
        return 126, _describe_exc(e)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


def restart_unit(unit, label):
    """reset-failed (clears the start-rate counter, so systemd does not
    refuse the restart of a unit that has been restarted often), then
    restart; returns True on success."""
    rc, out = systemctl("reset-failed", unit)
    if rc != 0:
        log("{}: systemctl reset-failed {} failed (exit {}): {}".format(label, unit, rc, out))
    rc, out = systemctl("restart", unit)
    if rc != 0:
        log("{}: systemctl restart {} failed (exit {}): {}".format(label, unit, rc, out))
        return False
    log("{}: restarted {}".format(label, unit))
    return True


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

class Watchdog:
    def __init__(self, args, now=None):
        self.args = args
        self.now = time.time() if now is None else now
        self.ok = True

    def verbose(self, msg):
        if self.args.verbose:
            log(msg)

    def notice_once(self, st, label, reason):
        """Log why an entry is not watched, once per chain start."""
        if st.get("notice") != reason:
            log("{}: {}; not watched".format(label, reason))
            st["notice"] = reason
        else:
            self.verbose("{}: {}; not watched".format(label, reason))

    def check_entry(self, entry, st):
        """Update st (the entry's counters) for one live, watched entry."""
        label = entry_label(entry)
        args = self.args
        try:
            started = float(entry.get("started_at"))
        except (TypeError, ValueError):
            started = 0.0
        age = self.now - started
        if args.grace is not None and age < args.grace:
            self.verbose("{}: in its start grace ({:.0f} of {:g} s)".format(label, age, args.grace))
            return
        if args.grace is None and age < GRACE_FLOOR_SEC:
            self.verbose("{}: in its start grace ({:.0f} of at least {:g} s)"
                         .format(label, age, GRACE_FLOOR_SEC))
            return

        cwd = entry.get("cwd") or ""
        port = entry["ports"]["status"]
        doc, failure = None, None
        try:
            doc = query_status(status_host(cwd), int(port), args.timeout)
        except (OSError, ValueError) as e:
            failure = "STATUS on port {} unreachable ({})".format(port, _describe_exc(e))

        if args.grace is None:
            grace = auto_grace(doc)
            if age < grace:
                self.verbose("{}: in its start grace ({:.0f} of {:g} s)".format(label, age, grace))
                return

        if failure is None:
            health = doc.get("pipeline_health")
            if health in FAILING_HEALTH:
                failure = "pipeline_health is {}".format(health)
        if failure is None:
            failure = self.stall_check(doc, st, os.path.join(cwd, "daq_chain_config.ini"), label)

        if failure is None:
            if st.get("fails"):
                log("{}: healthy again after {} failed check(s)".format(label, st["fails"]))
            else:
                self.verbose("{}: healthy".format(label))
            st["fails"] = 0
            st.pop("last_failure", None)
            return

        st["fails"] = int(st.get("fails", 0)) + 1
        st["last_failure"] = failure
        log("{}: {} - failed check {}/{}".format(label, failure, st["fails"], args.fails))
        if st["fails"] >= args.fails:
            self.restart(entry, st, label)

    def stall_check(self, doc, st, ini_path, label):
        fc = doc.get("frame_count")
        if isinstance(fc, bool) or not isinstance(fc, int):
            if not st.get("stall_notice"):
                log("{}: STATUS has no frame_count; stall check skipped".format(label))
                st["stall_notice"] = True
            return None
        if self.args.stall_sec is not None:
            threshold = self.args.stall_sec
        else:
            threshold = auto_stall_threshold(doc, ini_path)
            if threshold is None:
                if not st.get("stall_notice"):
                    log("{}: stall threshold unknown (the STATUS snapshot is never refreshed "
                        "or the frame period is unknown); stall check skipped".format(label))
                    st["stall_notice"] = True
                return None
        if st.get("frame_count") != fc or not isinstance(st.get("frame_count_since"),
                                                         (int, float)):
            st["frame_count"] = fc
            st["frame_count_since"] = self.now
            return None
        frozen = self.now - st["frame_count_since"]
        if frozen >= threshold:
            return "frame_count {} unchanged for {:.0f} s (threshold {:.0f} s)".format(
                fc, frozen, threshold)
        return None

    def restart(self, entry, st, label):
        unit = entry["unit"]
        if self.args.dry_run:
            log("{}: would restart {} (dry run)".format(label, unit))
            return
        if not restart_unit(unit, label):
            self.ok = False
        st["fails"] = 0
        st["restarted_at"] = self.now

    def run(self):
        run_dir = self.args.run_dir
        if not os.path.isdir(run_dir):
            self.verbose("no registry directory {}; nothing to watch".format(run_dir))
            return 0
        lock_fd = self._lock(run_dir)
        if lock_fd is False:
            log("another watchdog run holds {}; skipped".format(run_dir))
            return 0
        try:
            return self._run_locked(run_dir)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

    @staticmethod
    def _lock(run_dir):
        """fd holding an exclusive flock on the run directory, None when
        locking is unavailable, False when another run holds it."""
        if fcntl is None:
            return None
        try:
            fd = os.open(run_dir, os.O_RDONLY)
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
            return None
        return fd

    def _run_locked(self, run_dir):
        state_path = os.path.join(run_dir, STATE_FILE)
        old = load_state(state_path)
        new = {}
        for path, entry in read_registry(run_dir):
            if not entry_live(entry):
                self.verbose("{}: {} has no running stage; skipped".format(
                    entry_label(entry), os.path.basename(path)))
                continue
            key = entry_key(entry)
            st = dict(old.get(key, {}))
            new[key] = st
            label = entry_label(entry)
            launcher = entry.get("launcher") or "manual"
            ports = entry.get("ports") if isinstance(entry.get("ports"), dict) else {}
            if launcher != WATCHED_LAUNCHER:
                self.notice_once(st, label, "started by launcher '{}' (only {} chains are "
                                 "restarted)".format(launcher, WATCHED_LAUNCHER))
                continue
            if ports.get("status") in (None, "", 0):
                self.notice_once(st, label, "status server disabled")
                continue
            unit = entry.get("unit") if isinstance(entry.get("unit"), str) else ""
            if not UNIT_RE.match(unit):
                self.notice_once(st, label, "registry entry has no valid unit name ({!r}), "
                                 "so it cannot be restarted".format(unit))
                continue
            st.pop("notice", None)
            self.check_entry(entry, st)
        for key in sorted(set(old) - set(new)):
            self.verbose("dropping counters of {} (chain gone)".format(key))
        if self.args.dry_run:
            return 0 if self.ok else 1
        try:
            save_state(state_path, new)
        except OSError as e:
            log("cannot write {}: {}".format(state_path, _describe_exc(e)))
            self.ok = False
        return 0 if self.ok else 1


def _positive_float(text):
    v = _num(text)
    if v is None or v <= 0:
        raise argparse.ArgumentTypeError("needs a positive number (got {!r})".format(text))
    return v


def _auto_or_seconds(text):
    if text == "auto":
        return None
    v = _num(text)
    if v is None or v < 0:
        raise argparse.ArgumentTypeError("needs 'auto' or a non-negative number of seconds "
                                         "(got {!r})".format(text))
    return v


def _fails(text):
    try:
        v = int(text)
    except ValueError:
        v = 0
    if v < 1:
        raise argparse.ArgumentTypeError("needs an integer >= 1 (got {!r})".format(text))
    return v


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="heimdall_watchdog.py",
        description="Restart systemd-supervised HeIMDALL DAQ chains whose status server "
                    "reports a failure for several consecutive runs (one run per invocation).")
    p.add_argument("--run-dir", default=os.environ.get("HEIMDALL_RUN_DIR") or DEFAULT_RUN_DIR,
                   help="host registry directory (default $HEIMDALL_RUN_DIR or /run/heimdall)")
    p.add_argument("--fails", type=_fails, default=3,
                   help="consecutive failing runs before a restart (default 3)")
    p.add_argument("--grace", type=_auto_or_seconds, default=None, metavar="auto|SEC",
                   help="seconds after started_at during which an entry is not checked "
                        "(default auto = max(120, 3 x stale_after_sec), stale_after_sec "
                        "from the extended STATUS, else 132)")
    p.add_argument("--stall-sec", type=_auto_or_seconds, default=None, metavar="auto|SEC",
                   help="frame_count stall threshold (default auto = 3 x stale_after_sec + 30 "
                        "when STATUS reports it, else 3 x k_eff x frame period + 30 from the "
                        "instance INI)")
    p.add_argument("--timeout", type=_positive_float, default=2.0,
                   help="STATUS connect/read timeout in seconds (default 2)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be restarted; never call systemctl, keep no state")
    p.add_argument("--verbose", action="store_true",
                   help="log every decision, including healthy entries")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    return Watchdog(args).run()


if __name__ == "__main__":
    sys.exit(main())
