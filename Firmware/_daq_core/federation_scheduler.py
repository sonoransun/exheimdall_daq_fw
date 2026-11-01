"""
    Federation Scheduler — Coordinated Frequency Schedule Partitioning

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Extends the DAQ frequency scheduling concept across multiple federated
    instances. Partitions a master frequency schedule among healthy instances
    using configurable strategies (round_robin, range).

    One canonical schedule format end to end (design spec D.5.1): the wire
    format of the coordinator's SCHEDULE SET/GET/SYNC, the persisted
    ``master_schedule.json`` and every per-target ``SCHD`` document use
    ``signal_scheduler.ScheduleParser.from_json``'s format::

        {"name": str, "repeat_mode": "loop"|..., "strategy": ... (master only),
         "gen": <u32> (per-target only),
         "entries": [{"frequency": int, "gains": [int,...], "dwell_frames": int,
                      "dwell_time_sec": float, "require_cal": bool}, ...]}

    Entry objects are stored and partitioned verbatim, so every key survives.
"""
import glob
import json
import logging
import os
import tempfile
import time
import zlib

try:
    from federation_common import is_local_host
except ImportError:  # pragma: no cover - federation_common ships beside this module
    is_local_host = None

#: Largest inline SCHD payload (128-byte frame minus the 4-byte verb).
INLINE_LIMIT = 124
#: Largest per-target schedule document a SCHB session accepts (D.6.2).
SCHB_LIMIT = 262144
#: Temp files of the path handover (rule 3) are named with this prefix.
TEMP_PREFIX = "heimdall_sched_"
#: Path-handover temp files older than this are removed by cleanup_temp_files().
TEMP_MAX_AGE_SEC = 3600.0

STRATEGIES = ("round_robin", "range")

def compute_gen(term, version, rebalance_seq, node_key):
    """Per-target generation id (D.5.2)."""
    text = "{}.{}.{}.{}".format(int(term), int(version), int(rebalance_seq), node_key)
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


def _int(value, what):
    if isinstance(value, bool):
        raise ValueError("{} must be an integer".format(what))
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("{} must be an integer (got {!r})".format(what, value))


def normalize_entry(entry, index=0):
    """Validate one canonical entry; returns a new dict with the keys as given.

    ``frequency`` is required (1 .. 2^32-1). ``gains`` is a list of ints (or
    absent/null), ``dwell_frames`` an int >= 1, ``dwell_time_sec`` a float > 0,
    ``require_cal`` a bool. Unknown keys are kept verbatim.
    """
    if not isinstance(entry, dict):
        raise ValueError("entries[{}] must be an object".format(index))
    if "frequency" not in entry:
        raise ValueError("entries[{}] has no frequency".format(index))
    out = dict(entry)
    freq = _int(entry["frequency"], "entries[{}].frequency".format(index))
    if not 0 < freq < 2 ** 32:
        raise ValueError("entries[{}].frequency {} is out of range".format(index, freq))
    out["frequency"] = freq
    if entry.get("gains") is not None:
        gains = entry["gains"]
        if not isinstance(gains, (list, tuple)) or not gains:
            raise ValueError("entries[{}].gains must be a per-channel list".format(index))
        out["gains"] = [_int(g, "entries[{}].gains".format(index)) for g in gains]
    elif "gains" in entry:
        out["gains"] = None
    if "dwell_frames" in entry:
        dwell = _int(entry["dwell_frames"], "entries[{}].dwell_frames".format(index))
        if dwell < 1:
            raise ValueError("entries[{}].dwell_frames must be >= 1".format(index))
        out["dwell_frames"] = dwell
    if entry.get("dwell_time_sec") is not None:
        try:
            sec = float(entry["dwell_time_sec"])
        except (TypeError, ValueError):
            raise ValueError("entries[{}].dwell_time_sec must be a number".format(index))
        if not sec > 0:
            raise ValueError("entries[{}].dwell_time_sec must be > 0".format(index))
        out["dwell_time_sec"] = sec
    if "require_cal" in entry and not isinstance(entry["require_cal"], bool):
        raise ValueError("entries[{}].require_cal must be true or false".format(index))
    return out


def validate_schedule_doc(doc):
    """Validate a canonical master document; returns the normalised copy.

    Raises ValueError with an operator-readable message.
    """
    if not isinstance(doc, dict):
        raise ValueError("schedule must be a JSON object")
    entries = doc.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("schedule needs a non-empty 'entries' list")
    out = dict(doc)
    out["entries"] = [normalize_entry(e, i) for i, e in enumerate(entries)]
    if "name" in doc and not isinstance(doc["name"], str):
        raise ValueError("schedule name must be a string")
    if "repeat_mode" in doc and not isinstance(doc["repeat_mode"], str):
        raise ValueError("repeat_mode must be a string")
    strategy = doc.get("strategy")
    if strategy is not None and strategy not in STRATEGIES:
        raise ValueError("unknown strategy '{}' (expected round_robin or range)".format(strategy))
    out.pop("gen", None)
    # The member-side parser must accept it exactly as members will load it
    from signal_scheduler import ScheduleParser
    try:
        ScheduleParser.from_json(json.dumps(out))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("schedule rejected by ScheduleParser: {}".format(e))
    return out


def _entry_out(entry):
    """Entry as emitted in a per-target document (D.5.1)."""
    out = {}
    for key, value in entry.items():
        if key == "require_cal":
            if value is False:
                out[key] = False
            continue
        if key == "dwell_time_sec" and value is None:
            continue
        if key == "gains" and value is None:
            continue
        out[key] = value
    return out


class FederationScheduler:
    """Partition and distribute frequency schedules across federated instances."""

    def __init__(self, coordinator=None, health_monitor=None):
        """
        Parameters
        ----------
        coordinator : FederationCoordinator or None
            Used to send schedule commands to instances.
        health_monitor : FederationHealth or None
            Used to determine which instances are healthy.
        """
        self.logger = logging.getLogger(__name__)
        self.coordinator = coordinator
        self.health_monitor = health_monitor
        self._current_assignments = {}
        self._master_schedule = None
        self._temp_files = {}  # path -> creation time (path handovers of this process)

    # ------------------------------------------------------------------
    # master schedule
    # ------------------------------------------------------------------

    def set_master_schedule(self, frequencies, gains, dwell_frames, strategy="round_robin"):
        """
        Set the master schedule to be partitioned.

        Parameters
        ----------
        frequencies : list of int
            RF center frequencies in Hz.
        gains : list of (list of int or None)
            Per-channel gain values (R820T tenths of dB) for each frequency,
            or None for no gain change on that entry. Scalars are rejected:
            the channel count is not known here, so a scalar cannot be
            expanded to the per-channel list the SCHD consumers require.
        dwell_frames : list of int
            Number of frames to dwell on each frequency.
        strategy : str
            Partition strategy: "round_robin" or "range" (anything else
            partitions round-robin, as before).
        """
        for i, g in enumerate(gains):
            if g is not None and not isinstance(g, (list, tuple)):
                raise ValueError(
                    "gains[{:d}] is a scalar ({!r}): per-frequency gains must be "
                    "a per-channel list (R820T tenths of dB) or None".format(i, g))
        entries = []
        for f, g, d in zip(frequencies, gains, dwell_frames):
            entry = {"frequency": int(f), "dwell_frames": int(d)}
            if isinstance(g, (list, tuple)):
                entry["gains"] = [int(v) for v in g]
            entries.append(entry)
        self._store_master({"name": "federation", "entries": entries}, strategy)
        self.logger.info("Master schedule set: %d frequencies, strategy=%s",
                         len(entries), strategy)

    def set_master_schedule_doc(self, doc, strategy=None):
        """Set the master schedule from a canonical document (validated, entries verbatim).

        ``strategy`` overrides the document's ``strategy`` key. Returns the
        stored canonical document. Raises ValueError for an invalid document.
        """
        canon = validate_schedule_doc(doc)
        strat = strategy or canon.get("strategy") or "round_robin"
        if strat not in STRATEGIES:
            raise ValueError("unknown strategy '{}' (expected round_robin or range)".format(strat))
        self._store_master(canon, strat)
        self.logger.info("Master schedule '%s' set: %d entries, strategy=%s",
                         canon.get("name", "federation"), len(canon["entries"]), strat)
        return self.get_master_schedule_doc()

    def _store_master(self, doc, strategy):
        entries = [dict(e) for e in doc["entries"]]
        self._master_schedule = {
            "name": doc.get("name", "federation"),
            "repeat_mode": doc.get("repeat_mode", "loop"),
            "entries": entries,
            # derived parallel views (compatibility with the list-based API)
            "frequencies": [e["frequency"] for e in entries],
            "gains": [e.get("gains") for e in entries],
            "dwell_frames": [e.get("dwell_frames", 100) for e in entries],
            "strategy": strategy,
        }
        for key, value in doc.items():
            if key not in ("name", "repeat_mode", "entries", "strategy", "gen"):
                self._master_schedule.setdefault("extra", {})[key] = value

    def set_strategy(self, strategy):
        if strategy not in STRATEGIES:
            raise ValueError("unknown strategy '{}' (expected round_robin or range)".format(strategy))
        if self._master_schedule is not None:
            self._master_schedule["strategy"] = strategy

    def clear_master_schedule(self):
        self._master_schedule = None
        self._current_assignments = {}

    def has_master_schedule(self):
        return self._master_schedule is not None

    def get_master_schedule_doc(self):
        """The canonical master document (with ``strategy``), or None."""
        m = self._master_schedule
        if m is None:
            return None
        doc = {"name": m["name"], "repeat_mode": m["repeat_mode"]}
        doc["strategy"] = m["strategy"]
        doc.update(m.get("extra", {}))
        doc["entries"] = [dict(e) for e in m["entries"]]
        return doc

    def whole_schedule_bytes(self, doc=None):
        """Size of the whole schedule serialised as one per-target SCHD document."""
        if doc is None:
            m = self._master_schedule
            if m is None:
                return 0
            sched = {"entries": m["entries"]}
            name, repeat = m["name"], m["repeat_mode"]
        else:
            sched = {"entries": doc["entries"]}
            name, repeat = doc.get("name", "federation"), doc.get("repeat_mode", "loop")
        text = self._build_schedule_json(sched, name="{}@{}".format(name, "x" * 64),
                                         gen=0xFFFFFFFF, repeat_mode=repeat)
        return len(text.encode("utf-8"))

    # ------------------------------------------------------------------
    # partitioning
    # ------------------------------------------------------------------

    def partition_schedule(self, instance_ids=None, strategy=None, targets=None):
        """
        Partition the master schedule across given instances.

        Parameters
        ----------
        instance_ids : list of int or None
            Library path: assignments keyed by int instance id.
        strategy : str or None
            Override the strategy set in set_master_schedule.
        targets : list of str or None
            Node keys (the coordinator always passes them); assignments are
            then keyed by node key.

        When neither is given the targets come from ``health_monitor``
        (a per-instance monitor: its healthy peers plus its own id), else
        ``[0]`` (the historical fallback).

        Returns
        -------
        dict
            Mapping key -> {entries, frequencies, gains, dwell_frames};
            ``{}`` (and cleared assignments) when nothing can be partitioned.
        """
        if self._master_schedule is None:
            self.logger.warning("No master schedule set")
            self._current_assignments = {}
            return {}

        if targets is not None:
            keys = list(targets)
        elif instance_ids is not None:
            keys = list(instance_ids)
        elif self.health_monitor is not None and hasattr(self.health_monitor, "instance_id"):
            peers = self.health_monitor.get_healthy_peers()
            peer_table = self.health_monitor.get_peer_table()
            keys = [peer_table[p]["instance_id"] for p in peers
                    if peer_table[p]["instance_id"] >= 0]
            keys.append(self.health_monitor.instance_id)
            keys = sorted(set(keys))
        else:
            keys = [0]

        if not keys:
            self.logger.warning("No instances available for partition")
            self._current_assignments = {}
            return {}

        strat = strategy or self._master_schedule.get("strategy", "round_robin")
        entries = self._master_schedule["entries"]
        if strat == "range":
            parts = self._partition_range(keys, entries)
        else:
            parts = self._partition_round_robin(keys, entries)
        assignments = {k: self._assignment(v) for k, v in parts.items()}
        self._current_assignments = assignments
        self.logger.info("Schedule partitioned: %s",
                         {k: len(v["entries"]) for k, v in assignments.items()})
        return assignments

    @staticmethod
    def _assignment(entries):
        return {
            "entries": [dict(e) for e in entries],
            "frequencies": [e["frequency"] for e in entries],
            "gains": [e.get("gains") for e in entries],
            "dwell_frames": [e.get("dwell_frames", 100) for e in entries],
        }

    @staticmethod
    def _partition_round_robin(keys, entries):
        """Alternate entries across targets in round-robin order."""
        n = len(keys)
        parts = {k: [] for k in keys}
        for i, entry in enumerate(entries):
            parts[keys[i % n]].append(entry)
        return parts

    @staticmethod
    def _partition_range(keys, entries):
        """Contiguous frequency ranges; the first ``total % n`` targets get one extra."""
        n = len(keys)
        total = len(entries)
        ordered = sorted(range(total), key=lambda i: entries[i]["frequency"])
        base, extra = divmod(total, n)
        parts = {}
        start = 0
        for idx, key in enumerate(keys):
            size = base + (1 if idx < extra else 0)
            parts[key] = [entries[i] for i in ordered[start:start + size]]
            start += size
        return parts

    # ------------------------------------------------------------------
    # per-target documents
    # ------------------------------------------------------------------

    @staticmethod
    def _build_schedule_json(sched, name="federation", gen=None, repeat_mode=None):
        """Build the compact JSON document ScheduleParser.from_json accepts.

        ``sched`` carries ``entries`` (canonical, emitted with their keys as
        stored: ``require_cal`` only when false, ``dwell_time_sec`` only when
        present) or the legacy parallel lists.
        """
        if "entries" in sched:
            entries = [_entry_out(e) for e in sched["entries"]]
        else:
            entries = []
            for f, g, d in zip(sched["frequencies"], sched["gains"], sched["dwell_frames"]):
                entry = {"frequency": int(f), "dwell_frames": int(d)}
                if isinstance(g, (list, tuple)):
                    entry["gains"] = [int(v) for v in g]
                entries.append(entry)
        doc = {"name": name}
        if repeat_mode is not None and repeat_mode != "loop":
            doc["repeat_mode"] = repeat_mode
        if gen is not None:
            doc["gen"] = int(gen) & 0xFFFFFFFF
        doc["entries"] = entries
        return json.dumps(doc, separators=(",", ":"))

    def target_document(self, sched, node_key, instance_id, extended, gen):
        """(json_text, gen_or_None) for one target.

        Extended targets (caps contain ``schg``): name ``<master>@<node key>``
        and ``gen``. Legacy targets: today's ``federation_inst<iid>`` and no
        ``gen`` (byte-identical to today, so the transfer choice is today's).
        """
        m = self._master_schedule or {}
        repeat = m.get("repeat_mode", "loop")
        if extended:
            name = "{}@{}".format(m.get("name", "federation"), node_key)
            return self._build_schedule_json(sched, name=name, gen=gen, repeat_mode=repeat), gen
        name = "federation_inst{:d}".format(int(instance_id))
        return self._build_schedule_json(sched, name=name, repeat_mode=repeat), None

    # ------------------------------------------------------------------
    # transfer (D.5.2)
    # ------------------------------------------------------------------

    @staticmethod
    def choose_transfer(json_text, caps, host):
        """'inline' | 'chunked' | 'path' | None (remote legacy target, too large)."""
        n = len(json_text.encode("utf-8"))
        if n <= INLINE_LIMIT:
            return "inline"
        if caps and "schb" in caps:
            return "chunked"
        local = True if is_local_host is None or host is None else is_local_host(host)
        return "path" if local else None

    def write_temp_schedule(self, json_text, instance_id):
        """Path handover: write the document to a temp file; returns its path."""
        fd, path = tempfile.mkstemp(prefix="{}inst{:d}_".format(TEMP_PREFIX, int(instance_id)),
                                    suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(json_text)
        self._temp_files[path] = time.time()
        return path

    def remove_temp_schedule(self, path):
        self._temp_files.pop(path, None)
        try:
            os.unlink(path)
        except OSError:
            pass

    def cleanup_temp_files(self, max_age_sec=TEMP_MAX_AGE_SEC, now=None):
        """Delete this user's ``heimdall_sched_*.json`` temp files older than max_age_sec."""
        now = time.time() if now is None else now
        removed = []
        uid = os.getuid() if hasattr(os, "getuid") else None
        pattern = os.path.join(tempfile.gettempdir(), TEMP_PREFIX + "*.json")
        for path in glob.glob(pattern):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if uid is not None and st.st_uid != uid:
                continue
            if now - st.st_mtime > max_age_sec:
                try:
                    os.unlink(path)
                    removed.append(path)
                except OSError:
                    continue
                self._temp_files.pop(path, None)
        return removed

    def _host_of(self, key):
        """Host of a library-path target (first coordinator instance with that id)."""
        for inst in getattr(self.coordinator, "instances", None) or []:
            if isinstance(inst, dict) and inst.get("instance_id") == key:
                return inst.get("connect_host", inst.get("host"))
        return None

    def distribute(self):
        """
        Send partitioned schedules to each instance via the coordinator
        (library path; the coordinator's REBALANCE uses its own parallel,
        XACK-first execution with the same transfer rules).

        Per target: compact JSON <= 124 bytes goes inline in 'SCHD'; a
        larger document is handed over as a temp-file path when the target
        is on this host (or its host is unknown) and refused with
        ``schedule_too_large_for_legacy_server`` for a remote target; with a
        coordinator that provides ``send_schedule(key, json_bytes)`` large
        documents go through it (chunked SCHB/SCHC/SCHE).

        Returns
        -------
        dict
            Per-instance send results.
        """
        if not self._current_assignments:
            self.logger.warning("No assignments to distribute, call partition_schedule first")
            return {}

        if self.coordinator is None:
            self.logger.warning("No coordinator configured, cannot distribute")
            return {}

        results = {}
        for iid, sched in self._current_assignments.items():
            try:
                sched_json = self._build_schedule_json(
                    sched, name="federation_inst{:d}".format(iid),
                    repeat_mode=(self._master_schedule or {}).get("repeat_mode"))
                n = len(sched_json.encode("utf-8"))
                if n <= INLINE_LIMIT:
                    results[iid] = self.coordinator._send_to_instance(
                        iid, "SCHD {}".format(sched_json))
                    continue
                if hasattr(self.coordinator, "send_schedule"):
                    results[iid] = self.coordinator.send_schedule(iid, sched_json.encode("utf-8"))
                    continue
                host = self._host_of(iid)
                if host is not None and is_local_host is not None and not is_local_host(host):
                    results[iid] = {"ok": False, "applied": False,
                                    "reason": "schedule_too_large_for_legacy_server",
                                    "bytes": n}
                    continue
                path = self.write_temp_schedule(sched_json, iid)
                self.logger.info(
                    "Schedule for instance %d exceeds the inline frame size, "
                    "sending file path %s (requires shared filesystem)", iid, path)
                results[iid] = self.coordinator._send_to_instance(iid, "SCHD {}".format(path))
            except Exception as e:
                results[iid] = {"error": str(e)}
        return results

    def rebalance(self):
        """Re-partition and re-distribute the schedule based on current health.

        Returns the per-target results, or ``{"error": "no master schedule"}``
        / ``{"error": "no healthy instances"}``.
        """
        self.logger.info("Rebalancing schedule across federation")
        if self._master_schedule is None:
            return {"error": "no master schedule"}
        if not self.partition_schedule():
            return {"error": "no healthy instances"}
        return self.distribute()

    def get_assignments(self):
        """Return the current schedule assignments."""
        return dict(self._current_assignments)
