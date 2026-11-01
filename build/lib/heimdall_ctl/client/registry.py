"""Read the host instance registry (``$RUN/inst<N>.json``, spec B.3.1).

Registry files exist only for chains started with ``[host] instance_registry
= 1``. ``$RUN`` is ``$HEIMDALL_RUN_DIR`` (default ``/run/heimdall``), with the
start scripts' fallback ``/tmp/heimdall-run-<uid>``; a chain also records
the directory it used in ``_logs/inst<N>/pids/run_dir``.
"""
import glob
import json
import os
import re

DEFAULT_RUN_DIR = "/run/heimdall"


def _fallback_run_dir():
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return "/tmp/heimdall-run-{:d}".format(uid)


def candidate_run_dirs(explicit=None, firmware_dir=None):
    """Directories to read, most specific first, without duplicates."""
    if explicit:
        return [explicit]
    dirs = [os.environ.get("HEIMDALL_RUN_DIR") or DEFAULT_RUN_DIR, _fallback_run_dir()]
    if firmware_dir:
        for hint in sorted(glob.glob(os.path.join(firmware_dir, "_logs", "inst*",
                                                  "pids", "run_dir"))):
            try:
                with open(hint, "r", encoding="utf-8") as f:
                    line = f.readline().strip()
            except OSError:
                continue
            if line:
                dirs.append(line)
    seen, out = set(), []
    for d in dirs:
        key = os.path.abspath(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _instance_key(path):
    m = re.search(r"inst(\d+)\.json$", path)
    return int(m.group(1)) if m else -1


def read_entries(run_dirs):
    """Return [(path, entry_dict)] for every readable inst<N>.json, ordered by
    instance id; unreadable/torn files are skipped."""
    entries = []
    seen = set()
    for d in run_dirs:
        for path in sorted(glob.glob(os.path.join(d, "inst*.json")), key=_instance_key):
            real = os.path.realpath(path)
            if real in seen or _instance_key(path) < 0:
                continue
            seen.add(real)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(entry, dict):
                entries.append((path, entry))
    entries.sort(key=lambda pe: (pe[1].get("instance_id", _instance_key(pe[0])), pe[0]))
    return entries


def pid_alive(pid, role=None):
    """True when pid exists (and, where /proc is readable, its command line
    contains role - the same test as the start scripts' hd_pid_role_alive)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except OSError:
        return False
    if role:
        try:
            with open("/proc/{:d}/cmdline".format(pid), "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        except OSError:
            return True  # no /proc (e.g. macOS): the existence check is all we have
        return role in cmdline
    return True


def pids_alive(entry):
    """(alive, total) over the entry's ``pids`` map."""
    pids = entry.get("pids") if isinstance(entry.get("pids"), dict) else {}
    alive = sum(1 for role, pid in pids.items() if pid_alive(pid, role))
    return alive, len(pids)
