"""
    Federation Health Monitor — Peer Discovery and Health Tracking

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Polls peer DAQ instances via their StatusServer endpoints to maintain
    a health table. Emits federation events (peer_up, peer_down and, when
    enabled, peer_degraded / peer_recovered / peer_flapping) through the
    EventBus. Supports the advisory coordinator election via lowest healthy
    instance_id (telemetry only; nothing acts on it).

    Every constructor parameter added after the original six is
    keyword-only and defaults to today's behaviour (design spec C.5): with
    only ``peer_addresses`` the polling cadence, the 2 s connect timeout,
    the time-only down rule, the events and the advisory election are
    unchanged.

    ``CoordinatorRegistrar`` (C.3) runs on its own daemon thread and sends
    ``REGISTER`` to every coordinator endpoint in parallel, so slow or dead
    peers never delay registration.
"""
import json
import logging
import os
import random
import socket
import statistics
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait

from federation_common import parse_member, request_json

#: Member states (C.5).
STATE_UNKNOWN = "unknown"
STATE_UP = "up"
STATE_DEGRADED = "degraded"
STATE_DOWN = "down"
STATE_FLAPPING = "flapping"
STATE_STALE = "stale"

#: Reported pipeline_health values that make an up member degraded.
_DEGRADED_HEALTH = frozenset({"degraded", "error", "stale"})

#: Stall detection with ``stall_after_sec="auto"`` (C.6).
STALL_AUTO_FLOOR_SEC = 60.0
STALL_AUTO_HISTORY = 5

_ALIVE_STATES = frozenset({STATE_UP, STATE_DEGRADED, STATE_STALE, STATE_FLAPPING})


class DnsUnavailable(OSError):
    """Raised by a connect hook when a member host has no cached address."""
    error_kind = "dns"


def classify_error(exc):
    """error_kind of a failed poll: refused | timeout | bad_json | dns | reset."""
    kind = getattr(exc, "error_kind", None)
    if kind:
        return kind
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, (ValueError, UnicodeError)):
        return "bad_json"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return "reset"
    return "refused"


def _event_type(name):
    """Event type constant from daq_events, or the literal (G.7b)."""
    try:
        import daq_events
    except ImportError:
        return name
    return getattr(daq_events, "EVT_" + name.upper(), name)


class FederationHealth:
    """Lightweight peer health monitor using StatusServer TCP endpoints."""

    def __init__(self, instance_id, peer_addresses, poll_interval=5.0, event_bus=None,
                 status_base_port=5002, port_stride=100, *,
                 members=None, peer_timeout=2.0, up_successes=1, down_misses=None,
                 flap_window_sec=0.0, flap_threshold=0, emit_health_events=False,
                 max_workers=1, node_id=None, cluster_id="default",
                 auth_token=None, sched_other=False, stall_after_sec=0.0,
                 register=None,
                 _clock=time.monotonic, _wall=time.time,
                 _connect=socket.create_connection):
        """
        Parameters
        ----------
        instance_id : int
            This instance's ID.
        peer_addresses : list of str
            Peer entries in the canonical "host:instance_id" form; the
            legacy "host:status_port" form (number >= 1024) is accepted as
            today. Parsed by federation_common.parse_member with
            legacy_port_form=True, so FederationHealth, the coordinator and
            the router read peer_list identically.
        poll_interval : float
            Seconds between health polls.
        event_bus : EventBus or None
            Optional event bus for emitting federation events.
        status_base_port, port_stride : int
            Port formula of the peers' status servers.
        members : list of parse_member() dicts or None
            Overrides peer_addresses; the table is then keyed by node key.
        peer_timeout : float
            Connect/read timeout of one STATUS poll (today's literal 2.0).
        up_successes : int
            Consecutive good polls before a peer is up (1 = today).
        down_misses : int or None
            None = today's time-only down rule; k = down after k
            consecutive failed polls and seen_age > k * poll_interval.
        flap_window_sec, flap_threshold : float, int
            More than flap_threshold state changes within flap_window_sec
            mark a peer flapping (0 = off).
        emit_health_events : bool
            Emit peer_degraded / peer_recovered / peer_flapping.
        max_workers : int
            Parallel polls (1 = today's sequential loop).
        node_id, cluster_id : str
            Identity used by the registrar.
        auth_token : str or None
            AUTH prefix for requests to :6000 (the registrar).
        sched_other : bool
            Polling and registrar threads switch themselves to SCHED_OTHER.
        stall_after_sec : float or "auto"
            Progress-based stale detection on frame_count (0 = off).
        register : dict or None
            {"endpoints": [(host, port), ...], "interval": 5.0,
             "body_fn": callable -> dict, "want_peers": bool} starts a
            CoordinatorRegistrar beside the polling thread.
        _clock, _wall, _connect : callables
            Test hooks (monotonic clock, wall clock, socket.create_connection).
        """
        self.logger = logging.getLogger(__name__)
        self.instance_id = instance_id
        self.poll_interval = poll_interval
        self.event_bus = event_bus
        self.status_base_port = status_base_port
        self.port_stride = port_stride
        self.peer_timeout = float(peer_timeout)
        self.up_successes = max(1, int(up_successes))
        self.down_misses = None if down_misses is None else max(1, int(down_misses))
        self.flap_window_sec = float(flap_window_sec)
        self.flap_threshold = int(flap_threshold)
        self.emit_health_events = bool(emit_health_events)
        self.max_workers = max(1, int(max_workers))
        self.node_id = node_id
        self.cluster_id = cluster_id
        self.auth_token = auth_token
        self.sched_other = bool(sched_other)
        self.stall_after_sec = stall_after_sec
        self._clock = _clock
        self._wall = _wall
        self._connect = _connect

        self._peer_addresses = []
        self._peer_table = {}
        self._peer_endpoints = {}  # addr -> (host, status_port)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._executor = None
        self._coordinator_id = instance_id  # assume self until proven otherwise
        self._hub_rows = {}
        self._rounds = 0
        self._round_lock = threading.Lock()
        self._sched_logged = False

        if members is not None:
            self.set_members(members)
        else:
            for addr in peer_addresses:
                m = parse_member(addr, default_stride=port_stride,
                                 status_base_port=status_base_port, legacy_port_form=True)
                self._peer_addresses.append(addr)
                self._peer_endpoints[addr] = (m["connect_host"], m["ports"]["status"])
                if addr not in self._peer_table:
                    self._peer_table[addr] = self._new_row(m)

        self.registrar = None
        if register:
            self.registrar = CoordinatorRegistrar(
                endpoints=register.get("endpoints") or [],
                interval=float(register.get("interval", 5.0)),
                body_fn=register["body_fn"],
                want_peers=bool(register.get("want_peers", False)),
                auth_token=auth_token, peer_timeout=self.peer_timeout,
                on_peers=self.apply_hub_peers, sched_other=self.sched_other,
                _clock=_clock)

    # ------------------------------------------------------------------
    # table construction
    # ------------------------------------------------------------------

    def _new_row(self, member):
        return {
            # today's keys
            "alive": False,
            "last_seen": 0.0,
            "sync_state": 0,
            "frequency_hz": 0,
            "health": "unknown",
            "instance_id": member["instance_id"],
            # additive keys (C.5)
            "node_key": member["node_key"],
            "node_id": member.get("node_id"),
            "state": STATE_UNKNOWN,
            "reachable": False,
            "consecutive_successes": 0,
            "consecutive_failures": 0,
            "busy_streak": 0,
            "error_kind": "none",
            "seen_age_sec": None,
            "snapshot_age_sec": None,
            "stale": False,
            "flapping": False,
            "transitions_in_window": 0,
            "frame_count": None,
            "frame_count_changed_age_sec": None,
            # private bookkeeping (stripped from the public copies)
            "_seen_mono": None,
            "_poll_mono": None,
            "_base_state": STATE_UNKNOWN,
            "_transitions": deque(),
            "_last_transition_mono": None,
            "_fc_change_mono": None,
            "_fc_intervals": deque(maxlen=STALL_AUTO_HISTORY),
            "_fc_changes": 0,
            "_stalled": False,
            "_last_status": None,
            "_last_status_mono": None,
        }

    def set_members(self, members):
        """Replace the polled set with parse_member() dicts (keyed by node key).

        Rows of members that stay keep their state; new members start
        unknown; removed members are dropped.
        """
        with self._lock:
            keys = []
            endpoints = {}
            table = {}
            for m in members:
                key = m["node_key"]
                if key in endpoints:
                    continue
                keys.append(key)
                endpoints[key] = (m.get("connect_host", m["host"]), m["ports"]["status"])
                row = self._peer_table.get(key)
                if row is None:
                    row = self._new_row(m)
                else:
                    row["instance_id"] = m["instance_id"]
                    row["node_id"] = m.get("node_id") or row["node_id"]
                table[key] = row
            self._peer_addresses = keys
            self._peer_endpoints = endpoints
            self._peer_table = table

    # ------------------------------------------------------------------
    # threads
    # ------------------------------------------------------------------

    def start(self):
        """Start the background polling thread (and the registrar, if configured)."""
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="federation_health")
        self._thread.start()
        self.logger.info("Federation health monitor started, polling %d peers every %.1fs",
                         len(self._peer_addresses), self.poll_interval)
        if self.registrar is not None:
            self.registrar.start()

    def _apply_sched_other(self):
        if not self.sched_other:
            return
        apply_sched_other(self.logger, "federation_health", once=not self._sched_logged)
        self._sched_logged = True

    def _poll_loop(self):
        self._apply_sched_other()
        while not self._stop_event.is_set():
            self.poll_round()
            wait_s = self.poll_interval
            if self.max_workers > 1:
                wait_s *= random.uniform(0.9, 1.1)
            self._stop_event.wait(wait_s)

    def poll_round(self):
        """Poll every peer once (sequentially, or in parallel with max_workers > 1),
        then run the advisory election. Returns the number of polled peers."""
        with self._round_lock:
            addrs = list(self._peer_addresses)
            if self.max_workers == 1 or len(addrs) <= 1:
                for addr in addrs:
                    if self._stop_event.is_set():
                        break
                    self._poll_peer(addr)
            else:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=self.max_workers,
                                                        thread_name_prefix="fedhealth")
                wait([self._executor.submit(self._poll_peer, addr) for addr in addrs])
            self._check_coordinator()
            self._rounds += 1
            return len(addrs)

    # ------------------------------------------------------------------
    # one poll
    # ------------------------------------------------------------------

    def _fetch_status(self, host, port):
        with self._connect((host, port), timeout=self.peer_timeout) as sock:
            sock.sendall(b"STATUS\n")
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        response = json.loads(data.decode("utf-8", errors="replace"))
        if not isinstance(response, dict):
            raise ValueError("STATUS reply is not a JSON object")
        return response

    def _poll_peer(self, addr):
        """Send STATUS to a single peer and update its row."""
        with self._lock:
            endpoint = self._peer_endpoints.get(addr)
        if endpoint is None:
            return
        host, port = endpoint
        try:
            response = self._fetch_status(host, port)
        except Exception as e:
            self._on_failure(addr, e)
            return
        if set(response) == {"error"} and response.get("error") == "busy":
            self._on_busy(addr)
        else:
            self._on_success(addr, response)

    def _on_success(self, addr, response):
        events = []
        now_mono = self._clock()
        with self._lock:
            row = self._peer_table.get(addr)
            if row is None:
                return
            was_alive = row["alive"]
            # Prefer the config-derived instance_id; take the reported one
            # when the config form did not carry it
            peer_iid = row["instance_id"]
            if peer_iid < 0:
                peer_iid = response.get("instance_id", -1)
            row.update({
                "sync_state": response.get("sync_state", 0),
                "frequency_hz": response.get("current_frequency_hz", 0),
                "health": response.get("pipeline_health", "unknown"),
                "instance_id": peer_iid,
            })
            row["reachable"] = True
            row["error_kind"] = "none"
            row["consecutive_failures"] = 0
            row["consecutive_successes"] += 1
            row["busy_streak"] = 0
            row["_seen_mono"] = now_mono
            row["_poll_mono"] = now_mono
            row["_last_status"] = response
            row["_last_status_mono"] = now_mono
            if response.get("node_id") and not row.get("node_id"):
                row["node_id"] = response.get("node_id")
            self._track_frame_count(row, response, now_mono)
            row["snapshot_age_sec"] = response.get("snapshot_age_sec")
            reported_stale = self._reported_stale(response)
            if was_alive or row["consecutive_successes"] >= self.up_successes:
                row["alive"] = True
                row["last_seen"] = self._wall()
            if not was_alive and row["alive"]:
                events.append(("peer_up", None))
            if row["alive"]:
                new_state = self._alive_state(row, reported_stale)
                events.extend(self._transition(row, new_state, now_mono))
            self._refresh_derived(row, now_mono)
        for evt, payload in events:
            self._emit_event(evt, addr, payload=payload)

    def _on_busy(self, addr):
        events = []
        now_mono = self._clock()
        with self._lock:
            row = self._peer_table.get(addr)
            if row is None:
                return
            row["reachable"] = True
            row["error_kind"] = "busy"
            row["busy_streak"] += 1
            row["_seen_mono"] = now_mono
            row["_poll_mono"] = now_mono
            if row["alive"]:
                row["last_seen"] = self._wall()
                new_state = self._alive_state(row, False)
                events.extend(self._transition(row, new_state, now_mono))
            self._refresh_derived(row, now_mono)
        for evt, payload in events:
            self._emit_event(evt, addr, payload=payload)

    def _on_failure(self, addr, exc):
        events = []
        now_mono = self._clock()
        with self._lock:
            row = self._peer_table.get(addr)
            if row is None:
                return
            row["reachable"] = False
            row["error_kind"] = classify_error(exc)
            row["consecutive_failures"] += 1
            row["consecutive_successes"] = 0
            row["_poll_mono"] = now_mono
            was_alive = row["alive"]
            if self.down_misses is None:
                # today's rule verbatim: mark as down if 3 intervals missed
                down = self._wall() - row["last_seen"] > 3 * self.poll_interval
            else:
                seen = row["_seen_mono"]
                seen_age = float("inf") if seen is None else now_mono - seen
                down = (row["consecutive_failures"] >= self.down_misses
                        and seen_age > self.down_misses * self.poll_interval)
            if down and was_alive:
                row["alive"] = False
                row["health"] = "error"
                events.append(("peer_down", None))
                events.extend(self._transition(row, STATE_DOWN, now_mono))
            self._refresh_derived(row, now_mono)
        for evt, payload in events:
            self._emit_event(evt, addr, payload=payload)

    # ------------------------------------------------------------------
    # state machine helpers (called with self._lock held)
    # ------------------------------------------------------------------

    @staticmethod
    def _reported_stale(response):
        age = response.get("snapshot_age_sec")
        limit = response.get("stale_after_sec")
        try:
            return age is not None and limit is not None and float(age) > float(limit)
        except (TypeError, ValueError):
            return False

    def _track_frame_count(self, row, response, now_mono):
        fc = response.get("frame_count")
        if fc is None:
            row["_stalled"] = False
            return
        prev = row["frame_count"]
        if prev is None:
            row["_fc_change_mono"] = now_mono
        elif fc != prev:
            if row["_fc_change_mono"] is not None and row["_fc_changes"] > 0:
                row["_fc_intervals"].append(now_mono - row["_fc_change_mono"])
            row["_fc_changes"] += 1
            row["_fc_change_mono"] = now_mono
        row["frame_count"] = fc
        threshold = self.stall_threshold(row, response)
        row["_stalled"] = (threshold is not None and row["_fc_change_mono"] is not None
                           and now_mono - row["_fc_change_mono"] > threshold)

    def stall_threshold(self, row, response=None):
        """Seconds without a frame_count change before a member is stale (C.6), or None."""
        setting = self.stall_after_sec
        if not setting:
            return None
        reported = 0.0
        if response is not None:
            try:
                reported = float(response.get("stale_after_sec") or 0.0)
            except (TypeError, ValueError):
                reported = 0.0
        if setting == "auto":
            if row["_fc_changes"] < 2:
                return None
            median = statistics.median(row["_fc_intervals"]) if row["_fc_intervals"] else 0.0
            return max(STALL_AUTO_FLOOR_SEC, 3.0 * median, 3.0 * reported)
        return max(float(setting), 3.0 * reported)

    def _alive_state(self, row, reported_stale):
        busy_limit = self.down_misses if self.down_misses is not None else 3
        if row["_stalled"] or reported_stale:
            return STATE_STALE
        if row["health"] in _DEGRADED_HEALTH or row["busy_streak"] >= busy_limit:
            return STATE_DEGRADED
        if row["busy_streak"] > 0 and row["_base_state"] in (STATE_DEGRADED, STATE_STALE):
            return row["_base_state"]
        return STATE_UP

    def _transition(self, row, new_state, now_mono):
        """Move the base state; returns the events to emit."""
        events = []
        old = row["_base_state"]
        flap_on = self.flap_threshold > 0 and self.flap_window_sec > 0
        if new_state != old:
            row["_base_state"] = new_state
            if flap_on:
                row["_transitions"].append(now_mono)
            row["_last_transition_mono"] = now_mono
            if self.emit_health_events:
                if old == STATE_UP and new_state in (STATE_DEGRADED, STATE_STALE):
                    events.append(("peer_degraded", {"state": new_state}))
                elif old in (STATE_DEGRADED, STATE_STALE) and new_state == STATE_UP:
                    events.append(("peer_recovered", {"state": new_state}))
        was_flapping = row["flapping"]
        if flap_on:
            window_start = now_mono - self.flap_window_sec
            while row["_transitions"] and row["_transitions"][0] < window_start:
                row["_transitions"].popleft()
            if len(row["_transitions"]) > self.flap_threshold:
                row["flapping"] = True
            elif (row["flapping"] and row["_last_transition_mono"] is not None
                  and now_mono - row["_last_transition_mono"] >= self.flap_window_sec):
                row["flapping"] = False
        if row["flapping"] and not was_flapping and self.emit_health_events:
            events.append(("peer_flapping", {"transitions": len(row["_transitions"])}))
        row["state"] = STATE_FLAPPING if row["flapping"] else row["_base_state"]
        return events

    def _refresh_derived(self, row, now_mono):
        if self.flap_threshold > 0 and self.flap_window_sec > 0:
            # C.5: flapping --(no transition for flap_window_sec)--> state of the
            # last poll, also for a peer that stays down (no further transitions)
            window_start = now_mono - self.flap_window_sec
            while row["_transitions"] and row["_transitions"][0] < window_start:
                row["_transitions"].popleft()
            if (row["flapping"] and row["_last_transition_mono"] is not None
                    and now_mono - row["_last_transition_mono"] >= self.flap_window_sec):
                row["flapping"] = False
        seen = row["_seen_mono"]
        row["seen_age_sec"] = None if seen is None else round(now_mono - seen, 3)
        fcm = row["_fc_change_mono"]
        row["frame_count_changed_age_sec"] = None if fcm is None else round(now_mono - fcm, 3)
        row["stale"] = row["_base_state"] == STATE_STALE
        row["transitions_in_window"] = len(row["_transitions"])
        row["state"] = STATE_FLAPPING if row["flapping"] else row["_base_state"]

    # ------------------------------------------------------------------
    # advisory election (unchanged rule and payload)
    # ------------------------------------------------------------------

    def _check_coordinator(self):
        """Elect coordinator as lowest instance_id among healthy peers + self."""
        candidates = [self.instance_id]
        with self._lock:
            for addr, info in self._peer_table.items():
                if info["alive"] and info["health"] != "error" and info["instance_id"] >= 0:
                    candidates.append(info["instance_id"])
        new_coordinator = min(candidates)
        if new_coordinator != self._coordinator_id:
            self._coordinator_id = new_coordinator
            self._emit_event("coordinator_elected", None,
                             payload={"coordinator_id": new_coordinator})

    def _emit_event(self, event_type, addr, payload=None):
        if self.event_bus is None:
            return
        try:
            from daq_events import DAQEvent, EVT_PEER_UP, EVT_PEER_DOWN, EVT_COORDINATOR_ELECTED
            evt_map = {
                "peer_up": EVT_PEER_UP,
                "peer_down": EVT_PEER_DOWN,
                "peer_degraded": _event_type("peer_degraded"),
                "peer_recovered": _event_type("peer_recovered"),
                "peer_flapping": _event_type("peer_flapping"),
                "coordinator_elected": EVT_COORDINATOR_ELECTED,
            }
            p = payload or {}
            if addr is not None:
                p["peer_address"] = addr
            if event_type == "peer_recovered":
                severity = "info"
            else:
                severity = ("info" if "up" in event_type or "elected" in event_type
                            else "warning")
            self.event_bus.emit(DAQEvent(
                timestamp=time.time(),
                severity=severity,
                module="federation",
                event_type=evt_map.get(event_type, event_type),
                payload=p,
            ))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # hub mode (peer table from the REGISTER ack)
    # ------------------------------------------------------------------

    def apply_hub_peers(self, peers):
        """Store the coordinator's member rows (``health_mode = hub``)."""
        if not isinstance(peers, dict):
            return
        with self._lock:
            self._hub_rows = {str(k): dict(v) for k, v in peers.items() if isinstance(v, dict)}

    # ------------------------------------------------------------------
    # accessors
    # ------------------------------------------------------------------

    @staticmethod
    def _public(row):
        return {k: v for k, v in row.items() if not k.startswith("_")}

    def get_peer_table(self):
        """Return a copy of the current peer health table (keyed by the configured
        address string; hub-mode rows keyed by node key)."""
        with self._lock:
            table = {addr: self._public(info) for addr, info in self._peer_table.items()}
            for key, row in self._hub_rows.items():
                table.setdefault(key, dict(row))
            return table

    def get_member_table(self):
        """Rows keyed by node key (C.5)."""
        with self._lock:
            table = {}
            for info in self._peer_table.values():
                table[info["node_key"]] = self._public(info)
            for key, row in self._hub_rows.items():
                table.setdefault(key, dict(row))
            return table

    def get_member_snapshots(self):
        """{node_key: (last STATUS reply or None, monotonic receive time or None)}."""
        with self._lock:
            return {info["node_key"]: (info["_last_status"], info["_last_status_mono"])
                    for info in self._peer_table.values()}

    def polled_since(self, t_mono):
        """True when every member has been polled at or after monotonic time t_mono."""
        with self._lock:
            return all(row["_poll_mono"] is not None and row["_poll_mono"] >= t_mono
                       for row in self._peer_table.values())

    def rounds_completed(self):
        return self._rounds

    def get_healthy_peers(self, exclude_degraded=False):
        """Return list of peer addresses that are alive and not in error state."""
        with self._lock:
            return [addr for addr, info in self._peer_table.items()
                    if info["alive"] and info["health"] != "error"
                    and (not exclude_degraded or info["state"] == STATE_UP)]

    def get_coordinator_id(self):
        """Return the current coordinator instance_id."""
        return self._coordinator_id

    def get_coordinator_status(self):
        """Registration summary for STATUS ``federation.coordinator`` (None without a registrar)."""
        if self.registrar is None:
            return None
        return self.registrar.summary()

    def close(self):
        """Stop the polling thread (and the registrar)."""
        self._stop_event.set()
        if self.registrar is not None:
            self.registrar.close()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval + 1)
        if self._executor is not None:
            self._executor.shutdown(wait=False)
        self.logger.info("Federation health monitor stopped")


def apply_sched_other(logger, who, once=True):
    """Best-effort: switch the calling thread to SCHED_OTHER (Linux)."""
    if not hasattr(os, "sched_setscheduler") or not hasattr(os, "SCHED_OTHER"):
        if once:
            logger.info("%s: SCHED_OTHER not available on this platform", who)
        return False
    try:
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    except OSError as e:
        if once:
            logger.warning("%s: could not switch to SCHED_OTHER: %s", who, e)
        return False
    if once:
        logger.info("%s thread runs SCHED_OTHER", who)
    return True


class CoordinatorRegistrar:
    """Periodic ``REGISTER`` to every coordinator endpoint (C.3).

    Runs on its own daemon thread at a fixed cadence (``interval``); each
    round sends to all endpoints in parallel and is bounded by
    ``min(peer_timeout, interval / 2)`` (connect + send + read).
    """

    LOG_EVERY_SEC = 60.0
    DUPLICATE_LOG_EVERY = 10

    def __init__(self, endpoints, interval, body_fn, want_peers=False, auth_token=None,
                 peer_timeout=2.0, on_peers=None, sched_other=False,
                 _clock=time.monotonic, _request=request_json):
        self.logger = logging.getLogger(__name__ + ".registrar")
        self.endpoints = [(str(h), int(p)) for h, p in endpoints]
        self.interval = float(interval)
        self.body_fn = body_fn
        self.want_peers = bool(want_peers)
        self.auth_token = auth_token
        self.round_timeout = max(0.05, min(float(peer_timeout), self.interval / 2.0))
        self.on_peers = on_peers
        self.sched_other = bool(sched_other)
        self._clock = _clock
        self._request = _request
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._state = {"{}:{}".format(h, p): {"ok": None, "last_ok_mono": None,
                                               "last_error": None, "role": None,
                                               "term": None, "registered_as": None,
                                               "leader": None, "_last_log": None,
                                               "_dup_count": 0}
                       for h, p in self.endpoints}
        self._executor = None
        self.rounds = 0

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="federation_registrar")
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.round_timeout + 1.0)
        if self._executor is not None:
            self._executor.shutdown(wait=False)

    def _loop(self):
        if self.sched_other:
            apply_sched_other(self.logger, "federation_registrar")
        next_at = self._clock()
        while not self._stop.is_set():
            self.register_once()
            next_at += self.interval
            now = self._clock()
            if next_at < now:
                next_at = now
            self._stop.wait(max(0.0, next_at - now))

    def _body(self):
        body = dict(self.body_fn())
        body.setdefault("want_peers", self.want_peers)
        body.setdefault("register_interval_sec", self.interval)
        return body

    def register_once(self):
        """One parallel REGISTER round; returns {endpoint: reply dict or None}."""
        if not self.endpoints:
            return {}
        try:
            body = self._body()
        except Exception as e:
            self.logger.error("REGISTER body could not be built: %s", e)
            return {}
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=len(self.endpoints),
                                                thread_name_prefix="registrar")
        futures = {self._executor.submit(self._send, h, p, body): "{}:{}".format(h, p)
                   for h, p in self.endpoints}
        done, _ = wait(futures, timeout=self.round_timeout + 0.25)
        results = {}
        for fut, ep in futures.items():
            if fut in done:
                reply, err = fut.result()
            else:
                reply, err = None, "timeout"
            results[ep] = reply
            self._record(ep, reply, err)
        self.rounds += 1
        return results

    def _send(self, host, port, body):
        try:
            reply = self._request(host, port, "REGISTER", body=body,
                                  auth_token=self.auth_token,
                                  connect_timeout=self.round_timeout,
                                  timeout=self.round_timeout)
            return reply, None
        except Exception as e:
            return None, str(e) or e.__class__.__name__

    def _record(self, ep, reply, err):
        now = self._clock()
        with self._lock:
            st = self._state[ep]
            if reply is not None and reply.get("ok"):
                st.update({"ok": True, "last_ok_mono": now, "last_error": None,
                           "role": reply.get("role"), "term": reply.get("term"),
                           "registered_as": reply.get("registered_as"),
                           "leader": reply.get("leader")})
                st["_dup_count"] = 0
                peers = reply.get("peers")
            else:
                error = err if reply is None else reply.get("error", "error")
                st["ok"] = False
                st["last_error"] = error
                peers = None
                if error == "duplicate_node_key":
                    st["_dup_count"] += 1
                    if st["_dup_count"] % self.DUPLICATE_LOG_EVERY == 1:
                        self.logger.error(
                            "REGISTER to %s rejected: duplicate_node_key (holder %s); set "
                            "[federation] advertise_host or node_id",
                            ep, (reply or {}).get("holder"))
                elif st["_last_log"] is None or now - st["_last_log"] >= self.LOG_EVERY_SEC:
                    st["_last_log"] = now
                    self.logger.warning("REGISTER to %s failed: %s", ep, error)
        if peers is not None and self.on_peers is not None:
            try:
                self.on_peers(peers)
            except Exception:
                self.logger.exception("hub peer table update failed")

    def summary(self):
        """{"endpoint","role","term","registered","last_register_ok_age_sec","last_error"}
        for the best endpoint (the leader when known), plus "endpoints"."""
        now = self._clock()
        with self._lock:
            rows = {}
            for ep, st in self._state.items():
                age = None if st["last_ok_mono"] is None else round(now - st["last_ok_mono"], 1)
                rows[ep] = {"role": st["role"], "term": st["term"], "registered": bool(st["ok"]),
                            "last_register_ok_age_sec": age, "last_error": st["last_error"]}
        best = None
        for ep, row in rows.items():
            if row["role"] in ("leader", "static"):
                best = ep
                break
        if best is None and rows:
            best = next(iter(rows))
        out = {"endpoint": best}
        out.update(rows.get(best, {"role": None, "term": None, "registered": False,
                                   "last_register_ok_age_sec": None, "last_error": None}))
        out["endpoints"] = rows
        return out
