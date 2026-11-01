#!/usr/bin/env python3
"""
    Federation Coordinator — Unified Control Plane for Multi-Instance DAQ

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Standalone process that provides a single control endpoint for the entire
    federation. Fans out commands (FREQ, GAIN, etc.) to all instance HW
    controllers and aggregates status from all StatusServers.

    Protocol v2 (design spec C and D):

    * ``:6000`` framing by ``federation_common.read_request``: legacy verbs
      (PING STATUS FREQ GAIN INSTANCE REBALANCE) keep today's reply framing
      (one JSON object, no trailing newline) and only gain additive keys;
      new verbs reply one JSON object + ``\\n`` with ``"ok"``.
    * Per-member commands always open with ``XACK`` (extended or legacy
      ack mode), honour the request deadline fixed at receipt, a circuit
      breaker, per-member serialisation and leadership guards.
    * Optional lease election between ``coordinator_candidates``
      (``federation_election``), SCHEDULE SYNC replication with pull repair,
      persistence under ``state_dir``, REGISTER-based dynamic membership,
      and schedule reconciliation (``federation_scheduler``).

    Without new keys the coordinator is ``static`` (term 0), uses today's
    membership (``--instances`` else ``peer_list``), persists nothing and
    serves STATUS live; the remaining legacy differences are ledger row L13.
"""
import argparse
import ipaddress
import json
import logging
import math
import os
import queue
import selectors
import signal
import socket
import struct
import sys
import threading
import time
import zlib
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, wait
from configparser import ConfigParser, Error as ConfigParserError

from federation_common import (
    FanOutBusy, FanOutRegistry, RequestError, compute_ports, default_node_id,
    fan_out, is_local_host, is_loopback, is_valid_cluster_id, is_valid_node_id,
    json_reply, node_key as make_node_key, parse_member, parse_member_list,
    read_request, request_json, self_host, self_member, _verb_of,
)
from federation_election import TERM_U32_MAX
from federation_health import (DnsUnavailable, FederationHealth, classify_error,
                               recv_until_close)
from federation_scheduler import (
    FederationScheduler, INLINE_LIMIT, SCHB_LIMIT, STRATEGIES, compute_gen,
    validate_schedule_doc,
)

try:
    from version import FW_VERSION
except ImportError:  # pragma: no cover - version.py ships beside this module
    FW_VERSION = "unknown"

PROTOCOL_VERSION = 2

#: D.4 mutating verb set (leader-only gating for ALL/NODE/FANOUT/INSTANCE).
MUTATING_VERBS = frozenset(
    "FREQ GAIN AGC INIT SCHD SCHS SCHN EGAN BIAS ORNT PARK SCAN OSTP RECL TLKA "
    "SCHB SCHC SCHE STHU".split())
#: Query verbs (answered on followers as well).
QUERY_VERBS = frozenset({"RFQ", "OQRY", "SCHQ", "HWSQ", "TLCK", "SCHG"})
#: Verbs only an extended (XACK-answering) member understands.
EXTENDED_ONLY_VERBS = frozenset({"RECL", "HWSQ", "TLCK", "TLKA", "SCHG"})
#: Per-channel value verbs guarded by the XACK channel count (G48).
CHANNEL_VERBS = frozenset({"GAIN", "EGAN", "BIAS"})
#: Served through the reserved pool when the general handler pool is full.
PRIORITY_VERBS = frozenset({"LEADER", "PREVOTE", "LEASE", "REGISTER"})

SCHC_CHUNK = 116
CHANNEL_MAX = 31

NEW_VERBS = ("VERSION", "LEADER", "PREVOTE", "LEASE", "HEALTH", "NODES", "MEMBERS", "MEMBER",
             "REGISTER", "PEERS", "ALL", "NODE", "FANOUT", "RECAL", "SCHEDULE", "ASSIGNMENTS",
             "EVENTS", "DB", "ROUTER")
LEGACY_VERB_LIST = ("PING", "STATUS", "FREQ", "GAIN", "INSTANCE", "REBALANCE")

BUSY_LEGACY = {"error": "busy", "retry_in_sec": 1}
RECONCILE_BACKOFF_SEC = (5, 10, 20, 40, 80)
COLLISION_EVENT_SEC = 600.0
#: Most REGISTER-ed members held at once; past it a new node_key is refused busy.
REGISTER_MAX_MEMBERS = 256
#: New node_keys one source address may register per REGISTER_RATE_WINDOW_SEC.
REGISTER_NEW_PER_SOURCE = 16
REGISTER_RATE_WINDOW_SEC = 60.0
#: Overflow connections the priority probe watches at once, and how long one
#: may stay silent before it gets the busy reply.
PROBE_MAX_PENDING = 256
PROBE_IDLE_SEC = 1.0
EVENTS_DEFAULT = 100
EVENTS_MAX = 1000


class ConfigError(Exception):
    """Bad coordinator configuration (CLI exit code 3)."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTINGS = OrderedDict([
    # name: (kind, default)
    ("coordinator_port", ("int", 6000)),
    ("listen_address", ("str", "0.0.0.0")),
    ("instance_id", ("int", 0)),
    ("port_stride", ("int", 100)),
    ("peer_list", ("str", "")),
    ("cluster_members", ("str", "")),
    ("coordinator_include_self", ("bool", False)),
    ("advertise_host", ("str", "")),
    ("cluster_id", ("str", "default")),
    ("node_id", ("str", "")),
    ("auth_token", ("str", "")),
    ("allow_cidrs", ("str", "")),
    ("coordinator_candidates", ("str", "")),
    ("coordinator_self", ("str", "")),
    ("election_interval_sec", ("float", 2.0)),
    ("election_missed_polls", ("int", 3)),
    ("election_settle_polls", ("int", 5)),
    ("election_clock_margin", ("float", 0.05)),
    ("election_failback_sec", ("float", 60.0)),
    ("require_quorum", ("bool", False)),
    ("health_mode", ("str", "mesh")),
    ("health_poll_interval", ("float", 5.0)),
    ("peer_timeout", ("float", 2.0)),
    ("peer_up_successes", ("int", 1)),
    ("peer_down_misses", ("opt_int", None)),
    ("peer_flap_window_sec", ("float", 0.0)),
    ("peer_flap_threshold", ("int", 0)),
    ("fanout_workers", ("int", 32)),
    ("peer_connect_timeout_sec", ("float", 2.0)),
    ("xack_timeout_sec", ("float", 3.0)),
    ("hwc_request_timeout_sec", ("auto_float", "auto")),
    ("max_member_rto_sec", ("float", 30.0)),
    ("max_handlers", ("int", 64)),
    ("reserved_handlers", ("int", 8)),
    ("sync_push_timeout_sec", ("float", 5.0)),
    ("schc_rtt_budget_ms", ("float", 50.0)),
    ("dns_ttl_sec", ("float", 60.0)),
    ("reconcile_override", ("bool", False)),
    ("status_timeout_sec", ("float", 3.0)),
    ("status_cache_sec", ("float", 0.0)),
    ("mutation_wait_sec", ("float", 2.0)),
    ("breaker_failures", ("int", 3)),
    ("breaker_open_sec", ("float", 10.0)),
    ("xack_cache_sec", ("float", 60.0)),
    ("schc_window", ("int", 16)),
    ("member_stall_after_sec", ("auto_float", "auto")),
    ("reconcile_assignments", ("auto_bool", "auto")),
    ("reconcile_interval_sec", ("float", 0.0)),
    ("reconcile_max_attempts", ("int", 5)),
    ("router_stats_endpoint", ("str", "")),
    ("state_dir", ("str", "")),
    ("register_expiry_factor", ("float", 3.0)),
    ("register_retain_sec", ("float", 600.0)),
])


def _convert(name, kind, raw):
    text = raw.strip() if isinstance(raw, str) else raw
    try:
        if kind == "str":
            return "" if text is None else str(text)
        if kind == "int":
            return int(text)
        if kind == "float":
            return float(text)
        if kind == "bool":
            if isinstance(text, bool):
                return text
            if str(text).lower() in ("1", "true", "yes", "on"):
                return True
            if str(text).lower() in ("0", "false", "no", "off"):
                return False
            raise ValueError(text)
        if kind == "opt_int":
            return None if text in (None, "") else int(text)
        if kind == "auto_float":
            if str(text).lower() == "auto":
                return "auto"
            return float(text)
        if kind == "auto_bool":
            if str(text).lower() == "auto":
                return "auto"
            return _convert(name, "bool", text)
    except (TypeError, ValueError):
        raise ConfigError("[federation] {} = {!r} is not a valid {}".format(
            name, raw, kind.replace("_", " ")))
    raise ConfigError("unknown setting kind {}".format(kind))


class CoordinatorSettings(object):
    """All coordinator keys (D.3.3, C.2, C.4.1) with their defaults.

    ``CoordinatorSettings(**overrides)``; unknown names raise TypeError.
    Every timeout and interval is a plain attribute, so tests can shrink them.
    """

    def __init__(self, **overrides):
        for name, (kind, default) in _SETTINGS.items():
            setattr(self, name, default)
        for name, value in overrides.items():
            if name not in _SETTINGS:
                raise TypeError("unknown coordinator setting '{}'".format(name))
            setattr(self, name, value)

    @classmethod
    def ini_values(cls, cfg):
        """{name: value} for every coordinator key present in ``[federation]``
        (plus ``[daq] listen_address``) of a ConfigParser. Raises ConfigError."""
        values = {}
        if cfg.has_section("federation"):
            for name, (kind, _default) in _SETTINGS.items():
                if name == "listen_address":
                    continue
                if cfg.has_option("federation", name):
                    values[name] = _convert(name, kind, cfg.get("federation", name))
        if cfg.has_option("daq", "listen_address"):
            addr = cfg.get("daq", "listen_address").strip()
            if addr:
                values["listen_address"] = addr
        return values

    def validate(self):
        """Raise ConfigError for values the coordinator cannot run with."""
        if self.cluster_id and not is_valid_cluster_id(self.cluster_id):
            raise ConfigError("bad cluster_id '{}'".format(self.cluster_id))
        if self.node_id and not is_valid_node_id(self.node_id):
            raise ConfigError("bad node_id '{}'".format(self.node_id))
        if self.health_mode not in ("mesh", "hub", "off"):
            raise ConfigError("health_mode must be mesh, hub or off")
        for name in ("fanout_workers", "max_handlers", "reserved_handlers", "schc_window",
                     "breaker_failures", "reconcile_max_attempts"):
            if int(getattr(self, name)) < 1:
                raise ConfigError("{} must be >= 1".format(name))
        for name in ("peer_connect_timeout_sec", "xack_timeout_sec", "max_member_rto_sec",
                     "status_timeout_sec", "health_poll_interval", "sync_push_timeout_sec"):
            if float(getattr(self, name)) <= 0:
                raise ConfigError("{} must be > 0".format(name))
        if not 0 <= int(self.coordinator_port) <= 65535:
            raise ConfigError("coordinator_port {} out of range (0-65535)".format(
                self.coordinator_port))
        if self.auth_token:
            from federation_common import is_valid_auth_token
            if not is_valid_auth_token(self.auth_token):
                raise ConfigError("bad auth_token (printable, no spaces, <= 128 chars)")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _frame(verb, payload=b""):
    """One 128-byte :5001 frame: 4-byte verb + 124-byte payload."""
    return verb.encode("ascii")[:4].ljust(4, b" ") + payload[:124].ljust(124, b"\x00")


def _xack_frame(term, cluster_id):
    cid = (cluster_id or "").encode("utf-8")[:31]
    payload = struct.pack("<I", int(term) & 0xFFFFFFFF) + cid.ljust(32, b"\x00") + \
        struct.pack("<I", 0)
    return _frame("XACK", payload)


def _recv_exact(sock, nbytes):
    data = b""
    while len(data) < nbytes:
        chunk = sock.recv(nbytes - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _reply_json(reply):
    """(kind, obj): kind 'empty' (FNSD + zeros), 'json', 'raw' or 'bad'."""
    if len(reply) < 4 or reply[:4] != b"FNSD":
        return "bad", None
    data = reply[4:].rstrip(b"\x00")
    if not data:
        return "empty", None
    text = data.decode("utf-8", errors="replace")
    try:
        return "json", json.loads(text)
    except ValueError:
        return "raw", text


def _ms(seconds):
    return int(round(seconds * 1000.0))


class _Breaker(object):
    __slots__ = ("failures", "open_until", "probing")

    def __init__(self):
        self.failures = 0
        self.open_until = 0.0
        self.probing = False


class _Resolver(object):
    """Background DNS cache (D.3.4): handlers only read cached addresses."""

    def __init__(self, ttl):
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._cache = {}          # host -> {"addrs": [...], "ok": mono|None, "tried": mono|None}
        self._done = {}           # host -> threading.Event (first attempt finished)

    @staticmethod
    def _literal(host):
        h = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        try:
            ipaddress.ip_address(h.split("%", 1)[0])
            return h
        except ValueError:
            return None

    def addresses(self, host):
        """Cached addresses of *host* (an IP literal is its own address)."""
        lit = self._literal(host)
        if lit is not None:
            return [lit]
        with self._lock:
            entry = self._cache.get(host)
            if entry is None:
                return []
            return list(entry["addrs"])

    def wait(self, host, timeout):
        if self._literal(host) is not None:
            return
        with self._lock:
            ev = self._done.setdefault(host, threading.Event())
        ev.wait(max(0.0, timeout))

    def refresh(self, hosts, force=False):
        now = time.monotonic()
        for host in set(hosts):
            if self._literal(host) is not None:
                continue
            with self._lock:
                entry = self._cache.get(host)
                due = (force or entry is None or entry["ok"] is None
                       or now - entry["ok"] >= self.ttl)
                ev = self._done.setdefault(host, threading.Event())
            if not due:
                continue
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                addrs = []
                for info in infos:
                    if info[4][0] not in addrs:
                        addrs.append(info[4][0])
                with self._lock:
                    self._cache[host] = {"addrs": addrs, "ok": time.monotonic(),
                                         "tried": time.monotonic()}
            except (OSError, UnicodeError):
                with self._lock:
                    entry = self._cache.get(host)
                    if entry is not None and entry["ok"] is not None and \
                            time.monotonic() - entry["ok"] < 10 * self.ttl:
                        entry["tried"] = time.monotonic()
                    else:
                        self._cache[host] = {"addrs": [], "ok": None,
                                             "tried": time.monotonic()}
            ev.set()


class _SingleFlight(object):
    """Concurrent callers of the same key share one in-flight computation (D.1)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._inflight = {}
        self.runs = 0

    def run(self, key, fn, timeout):
        with self._lock:
            slot = self._inflight.get(key)
            leader = slot is None
            if leader:
                slot = {"event": threading.Event(), "result": None, "error": None}
                self._inflight[key] = slot
                self.runs += 1
        if not leader:
            if not slot["event"].wait(max(0.0, timeout)):
                raise socket.timeout("shared poll did not finish in time")
            if slot["error"] is not None:
                raise slot["error"]
            return slot["result"]
        try:
            slot["result"] = fn()
            return slot["result"]
        except Exception as e:
            slot["error"] = e
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            slot["event"].set()


class _PrefixedSocket(object):
    """A socket whose first recv() calls return bytes already read by the probe."""

    def __init__(self, sock, prefix):
        self._sock = sock
        self._prefix = prefix

    def recv(self, n):
        if self._prefix:
            out, self._prefix = self._prefix[:n], self._prefix[n:]
            return out
        return self._sock.recv(n)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class Member(object):
    """One coordinator member (static, CLI, runtime or registered)."""

    _uid_lock = threading.Lock()
    _next_uid = 0

    def __init__(self, inst, source):
        with Member._uid_lock:
            Member._next_uid += 1
            self.uid = Member._next_uid
        self.inst = inst
        self.source = source
        iid = int(inst["instance_id"])
        host = str(inst["host"])
        self.host = host
        self.connect_host = inst.get("connect_host", host)
        self.instance_id = iid
        self.node_key = inst.get("node_key") or make_node_key(host, iid)
        self.node_id = inst.get("node_id")
        if inst.get("ports"):
            self.ports = dict(inst["ports"])
        else:
            self.ports = compute_ports(iid, inst.get("port_stride", 100))
        self.advertised_host = inst.get("advertised_host")
        self.addr = None
        self.nonce = None
        self.last_rx_mono = None
        self.register_interval = 5.0
        self.static = source in ("config", "cli")

    @property
    def legacy_key(self):
        return str(self.instance_id)

    def poll_dict(self, status_port):
        return {"node_key": self.node_key, "node_id": self.node_id, "host": self.host,
                "connect_host": self.connect_host, "instance_id": self.instance_id,
                "ports": {"status": status_port}}


class _Req(object):
    """Per-request bookkeeping: verb, kind, deadline fixed at receipt, leadership snapshot."""

    def __init__(self, verb, deadline_sec, legacy):
        self.verb = verb
        self.received = time.monotonic()
        self.deadline_sec = float(deadline_sec)
        self.deadline = self.received + self.deadline_sec
        self.legacy = legacy
        self.role = None
        self.term = 0
        self.version = None

    def remaining(self):
        return self.deadline - time.monotonic()

    @property
    def deadline_ms(self):
        return _ms(self.deadline_sec)


# ---------------------------------------------------------------------------
# :5001 session (XACK first, D.4)
# ---------------------------------------------------------------------------

class _HwcSession(object):
    """One :5001 connection to a member under its member lock."""

    def __init__(self, coord, member, ctx):
        self.c = coord
        self.m = member
        self.ctx = ctx
        self.sock = None
        self.mode = None
        self.info = {}
        self._lock = None
        self._transport_failed = False
        self._probe = False        # this session holds the breaker's half-open probe
        self._completed = False
        self.t0 = time.monotonic()

    # -- results --
    def _r(self, **kw):
        kw.setdefault("ok", False)
        kw["elapsed_ms"] = _ms(time.monotonic() - self.t0)
        return kw

    @property
    def extended(self):
        return self.mode == "extended"

    @property
    def caps(self):
        return self.info.get("caps", ()) if self.extended else ()

    def t_cmd(self):
        return self.c._t_cmd(self.info if self.extended else None)

    # -- open: breaker, dns, member lock, connect, XACK --
    def open(self, need=None):
        """Connect and XACK; None on success, else the not-sent result R.

        *need* is the budget that must remain after waiting for the member
        lock (default: the member's T_cmd from the ack-mode cache).
        """
        c, m, ctx = self.c, self.m, self.ctx
        ctx.phase = "connect"
        if ctx.cancel_event.is_set():
            return self._r(applied=False, error="cancelled", phase="connect")
        if not ctx.still_valid():
            return self._r(applied=False, error="leadership_lost", phase="connect")
        blocked, self._probe = c._breaker_check(m.node_key)
        if blocked is not None:
            return self._r(**blocked)
        port = c._compute_instance_ports(m.inst).get("hwc_port")
        if port is None:
            return self._r(applied=False, error="member has no control port (legacy "
                           "status-port peer_list entry)", error_kind="refused", phase="connect")
        addrs = c._resolve(m.connect_host, ctx.remaining())
        if not addrs:
            return self._r(applied=False, error="dns", error_kind="dns", phase="connect")
        if need is None:
            need = c._t_cmd(c._ack_cache_get(m.node_key))
            remaining = ctx.remaining()
            if need > remaining:
                return self._r(applied=False, reason="rto_exceeds_deadline", rto_ms=_ms(need),
                               remaining_ms=max(0, _ms(remaining)), phase="connect")
        lock = c._member_lock(m.node_key)
        budget = ctx.remaining() - need
        if budget <= 0 or not lock.acquire(timeout=budget):
            return self._r(applied=False, error="member_busy", phase="connect")
        self._lock = lock
        try:
            timeout = max(0.001, min(c.settings.peer_connect_timeout_sec, ctx.remaining()))
            self.sock = c._connect(m.connect_host, addrs, port, timeout)
        except OSError as e:
            self._transport_failed = True
            return self._r(applied=False, error=str(e) or e.__class__.__name__,
                           error_kind=classify_error(e), phase="connect")
        return self._xack()

    def _xack(self):
        c, m, ctx = self.c, self.m, self.ctx
        ctx.phase = "xack"
        role, term = c._role_term()
        try:
            self.sock.settimeout(max(0.001, min(c.settings.xack_timeout_sec, ctx.remaining())))
            self.sock.sendall(_xack_frame(term, c.settings.cluster_id))
            t_send = time.monotonic()
            reply = _recv_exact(self.sock, 128)
        except socket.timeout:
            self._transport_failed = True
            return self._r(applied=False, error="timeout", phase="xack")
        except OSError as e:
            self._transport_failed = True
            return self._r(applied=False, error="reset", error_kind="reset", phase="xack",
                           message=str(e))
        if len(reply) < 128:
            self._transport_failed = True
            return self._r(applied=False, error="reset", error_kind="reset", phase="xack")
        rtt = time.monotonic() - t_send
        kind, obj = _reply_json(reply)
        if kind == "empty":
            self.mode = "legacy"
            c._ack_cache_put(m.node_key, {"mode": "legacy", "rtt": rtt})
            return None
        if kind != "json" or not isinstance(obj, dict) or "xack" not in obj:
            return self._r(applied=False, error="bad_reply", error_kind="bad_reply", phase="xack")
        if obj.get("xack") is not True:
            return self._r(applied=False, reason=str(obj.get("error", "xack_refused")),
                           phase="xack")
        caps = tuple(x for x in str(obj.get("caps", "")).split(",") if x)
        info = {"mode": "extended", "nch": obj.get("nch"), "caps": caps,
                "term": obj.get("term", 0), "rto": obj.get("rto"), "hmx": obj.get("hmx"),
                "fp": obj.get("fp"), "rtt": rtt}
        self.mode = "extended"
        self.info = info
        c._ack_cache_put(m.node_key, info)
        c._check_member_rto(m, info)
        member_term = info["term"] if isinstance(info["term"], int) else 0
        if member_term > term:
            if c._observe_member_term(m, member_term):
                return self._r(applied=False, reason="stale_term", phase="xack")
        return None

    # -- one command frame --
    def command(self, verb, payload=b"", mutating=True, values=None, next_phase="command"):
        ctx = self.ctx
        if verb.strip() in EXTENDED_ONLY_VERBS and not self.extended:
            return self._r(applied=False, reason="unsupported_by_member", phase=ctx.phase)
        if verb in CHANNEL_VERBS and values is not None and self.extended:
            nch = self.info.get("nch")
            if isinstance(nch, int) and nch != len(values):
                return self._r(applied=False, reason="channel_count_mismatch",
                               expected=nch, got=len(values))
        t_cmd = self.t_cmd()
        blocked = ctx.check_before_send(t_cmd if mutating else None, next_phase=next_phase)
        if blocked is not None:
            blocked["elapsed_ms"] = _ms(time.monotonic() - self.t0)
            return blocked
        try:
            self.sock.settimeout(ctx.read_timeout(t_cmd))
            self.sock.sendall(_frame(verb, payload))
            reply = _recv_exact(self.sock, 128)
        except socket.timeout:
            self._transport_failed = True
            return self._r(applied=None, error="timeout", phase=ctx.phase)
        except OSError as e:
            return self._r(applied=None, error="reset", error_kind="reset", phase=ctx.phase,
                           message=str(e))
        if len(reply) < 128:
            return self._r(applied=None, error="reset", error_kind="reset", phase=ctx.phase)
        return self._map_reply(reply, mutating)

    def _map_reply(self, reply, mutating):
        kind, obj = _reply_json(reply)
        if kind == "bad":
            return self._r(applied=None, error="bad reply", error_kind="bad_reply",
                           raw=reply.hex())
        if not mutating:
            if kind == "empty":
                return self._r(ok=True)
            if kind == "raw":
                return self._r(ok=True, raw=obj)
            if isinstance(obj, dict) and obj.get("error") == "busy":
                return self._r(ok=False, error="busy", data=obj)
            return self._r(ok=True, data=obj)
        if kind == "empty":
            return self._r(ok=True, applied=None, legacy_ack=True)
        if kind == "json" and isinstance(obj, dict) and "applied" in obj:
            r = self._r(ok=True, applied=obj.get("applied"),
                        reason=obj.get("reason", "ok" if obj.get("applied") else None))
            for key in ("fsm", "seq", "entries"):
                if key in obj:
                    r[key] = obj[key]
            return r
        if kind == "raw":
            return self._r(ok=True, applied=None, raw=obj)
        return self._r(ok=True, applied=None, data=obj)

    # -- chunked schedule load (SCHB / SCHC pipelined / SCHE) --
    def send_chunked(self, data):
        c, ctx = self.c, self.ctx
        ctx.phase = "chunks"
        chunks = [data[i:i + SCHC_CHUNK] for i in range(0, len(data), SCHC_CHUNK)]
        crc = zlib.crc32(data) & 0xFFFFFFFF
        window = max(1, int(c.settings.schc_window))
        budget = c._t_chunks_max(len(chunks))
        chunk_deadline = time.monotonic() + budget

        def _timeout():
            return max(0.001, min(chunk_deadline - time.monotonic(), ctx.remaining()))

        try:
            self.sock.settimeout(_timeout())
            self.sock.sendall(_frame("SCHB", struct.pack("<IIHH", len(data), crc,
                                                         len(chunks), 0)))
            kind, obj = _reply_json(_recv_exact(self.sock, 128))
            if kind != "json" or not isinstance(obj, dict) or not obj.get("ok"):
                reason = obj.get("reason", "schb_refused") if isinstance(obj, dict) else "bad_reply"
                return self._r(applied=False, reason=reason, phase="chunks")
            sess = int(obj.get("sess", 0))
            sent = 0
            acked = 0
            while acked < len(chunks):
                while sent < len(chunks) and sent - acked < window:
                    if ctx.cancel_event.is_set():
                        return self._r(applied=False, error="cancelled", phase="chunks")
                    chunk = chunks[sent]
                    self.sock.settimeout(_timeout())
                    self.sock.sendall(_frame("SCHC", struct.pack("<HHH", sess, sent, len(chunk))
                                             + chunk))
                    sent += 1
                self.sock.settimeout(_timeout())
                kind, obj = _reply_json(_recv_exact(self.sock, 128))
                if kind != "json" or not isinstance(obj, dict) or not obj.get("ok") \
                        or obj.get("seq") != acked:
                    reason = obj.get("reason", "bad_reply") if isinstance(obj, dict) else "bad_reply"
                    return self._r(applied=False, reason=reason, phase="chunks")
                acked += 1
        except socket.timeout:
            self._transport_failed = True
            return self._r(applied=False, error="timeout", phase="chunks")
        except OSError as e:
            return self._r(applied=False, error="reset", error_kind="reset", phase="chunks",
                           message=str(e))
        return self.command("SCHE", struct.pack("<H", sess), mutating=True)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        if self.mode is not None or self._transport_failed:
            self.c._breaker_record(self.m.node_key, not self._transport_failed, self._probe)
        elif self._probe:
            self.c._breaker_unprobe(self.m.node_key)
        self._probe = False


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class FederationCoordinator:
    """Unified control plane for a federation of DAQ instances."""

    # Commands with no payload; values are the exact 4-byte verbs the
    # HW controller decodes (note the trailing space in "AGC "/"RFQ ").
    _NO_PAYLOAD_VERBS = {
        "AGC": "AGC ", "INIT": "INIT", "SCHS": "SCHS", "SCHQ": "SCHQ",
        "SCHN": "SCHN", "RFQ": "RFQ ", "PARK": "PARK", "SCAN": "SCAN",
        "OSTP": "OSTP", "OQRY": "OQRY",
    }
    #: Extended :5001 verbs (D.6) accepted by ALL/NODE/FANOUT/INSTANCE.
    _EXT_NO_PAYLOAD_VERBS = {"RECL": "RECL", "HWSQ": "HWSQ", "TLCK": "TLCK", "SCHG": "SCHG"}

    def __init__(self, port=6000, instances=None, scheduler=None,
                 listen_address="0.0.0.0", *, settings=None, event_bus=None):
        """
        Parameters
        ----------
        port : int
            TCP port for the coordinator's control interface (0 = ephemeral;
            the bound port is in ``self.port`` after start()).
        instances : list of dict
            Legacy membership: dicts {"host", "instance_id", "port_stride"}
            (or parse_member() dicts, optionally with explicit "ports").
            Used when ``settings.cluster_members`` is empty.
        scheduler : FederationScheduler or None
            When set, REBALANCE re-partitions and re-distributes its schedule.
        listen_address : str
            Local address the coordinator server binds to.
        settings : CoordinatorSettings or None
            Every coordinator key; defaults reproduce a static coordinator.
        event_bus : EventBus or None
            Optional bus for coordinator events (node_key_collision, ...).
        """
        self.logger = logging.getLogger(__name__)
        self.settings = settings if settings is not None else CoordinatorSettings()
        self.settings.validate()
        self.port = port
        self.instances = instances if instances is not None else []
        self.scheduler = scheduler
        self.listen_address = listen_address
        self.event_bus = event_bus
        self._stop_event = threading.Event()
        self._server_thread = None
        self._server_sock = None
        self._registry = FanOutRegistry()

        s = self.settings
        # -- membership --
        self._membership_lock = threading.Lock()
        self._static = []
        self._runtime_added = []        # Members added by MEMBER ADD
        self._runtime_removed = set()   # node keys removed by MEMBER REMOVE
        self._registered = OrderedDict()  # node_key -> Member
        self._collisions = OrderedDict()  # (nk, holder_addr, rejected_addr) -> since (wall)
        self._collision_events = OrderedDict()   # ckey -> mono of the last emitted event
        self._register_rate = OrderedDict()      # source addr -> [window start mono, new keys]
        self.cluster_mode = bool(str(s.cluster_members or "").strip())
        self._build_static_members()

        # -- per-member state --
        self._member_locks = {}
        self._member_locks_guard = threading.Lock()
        self._ack_cache = {}
        self._ack_lock = threading.Lock()
        self._legacy_logged = {}
        self._rto_logged = set()
        self._static_term_logged = set()
        self._breakers = {}
        self._breaker_lock = threading.Lock()
        self._resolver = _Resolver(s.dns_ttl_sec)
        self._addr_pref = {}               # host -> address that last connected
        self._addr_pref_lock = threading.Lock()
        self._resolver_running = False
        self._resolver_kick = threading.Event()

        # -- request handling --
        # re-entrant: state snapshots (_sync_doc) are taken under it, also from
        # code that already holds it for planning/commit; never held across I/O
        self._mutation_lock = threading.RLock()
        self._schedule_op = threading.Lock()
        self._reconcile_lock = threading.Lock()   # one reconciliation round at a time
        self._single_flight = _SingleFlight()
        self._status_cache = None
        self._general_pool = None
        self._reserved_pool = None
        self._general_free = None
        self._reserved_free = None
        self._probe_queue = queue.Queue(maxsize=256)
        self._read_kw = {}                   # read_request() overrides (tests shrink timeouts)
        self._allow = self._parse_cidrs(s.allow_cidrs)

        # -- replicated state --
        self._version = 0
        self._sync_term = 0
        self._set_at = None
        self._rebalance_seq = 0
        self._assignments = OrderedDict()   # node_key -> desired-state record
        self._issued_gens = deque(maxlen=4096)
        self._partitioned_at = None
        self._empty = []
        # bumped (mutation lock held) when REBALANCE plans new loads; a
        # reconciliation round planned before that is superseded at once,
        # not only when the REBALANCE commits its version (D.5.3)
        self._plan_epoch = 0
        self._repair_running = False
        self._last_pull = None               # (leader_ep, (term, version)) of the last pull
        self._push_hook = None               # test fault injection: callable(endpoint) -> drop?
        self._local_events = deque(maxlen=EVENTS_MAX)
        self._reconcile_kick = threading.Event()
        self._event_seen = {}
        self._event_seen_lock = threading.Lock()

        # -- persistence --
        candidates = str(s.coordinator_candidates or "").strip()
        self.persist_dir = None
        if s.state_dir:
            self.persist_dir = s.state_dir
        elif candidates:
            self.persist_dir = os.path.join("_data_control", "federation")

        # -- election --
        self.election = None
        self.election_runner = None
        self._leader_since = time.monotonic()
        self._first_reconcile_done = False
        if candidates:
            self._setup_election(candidates)

        self.reconcile_enabled = (s.reconcile_assignments == "auto"
                                  and (self.cluster_mode or bool(candidates))) \
            or s.reconcile_assignments is True
        self._poller = None
        self._poller_lock = threading.Lock()
        self._poller_started_mono = None
        if self.persist_dir is not None:
            self._load_state()

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_cidrs(text):
        nets = []
        for item in str(text or "").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                raise ConfigError("bad allow_cidrs entry '{}'".format(item))
        return nets

    def _build_static_members(self):
        s = self.settings
        static = []
        if self.cluster_mode:
            try:
                parsed = parse_member_list(s.cluster_members, default_stride=s.port_stride)
            except ValueError as e:
                raise ConfigError("cluster_members: {}".format(e))
            static = [Member(p, "config") for p in parsed]
        else:
            seen = set()
            for inst in self.instances:
                m = Member(inst, inst.get("source", "config") if isinstance(inst, dict) else "config")
                if m.node_key in seen:
                    self.logger.warning("membership lists '%s' more than once (kept, as today)",
                                        m.node_key)
                seen.add(m.node_key)
                static.append(m)
            if s.coordinator_include_self:
                try:
                    me = self_member(s.instance_id, s.port_stride, s.advertise_host or None)
                except ValueError as e:
                    raise ConfigError(str(e))
                dup = any(is_local_host(m.host) and m.ports.get("ctl") == me["ports"]["ctl"]
                          for m in static)
                if not dup:
                    static.append(Member(me, "config"))
        self._static = static

    def _setup_election(self, candidates):
        from federation_election import (Election, ElectionRunner, TermStore,
                                         parse_candidates)
        s = self.settings
        try:
            cands = parse_candidates(candidates)
            self_ep = s.coordinator_self.strip() if s.coordinator_self else ""
            if not self_ep:
                for ep in cands:
                    host, _, port = ep.rpartition(":")
                    if int(port) == int(self.port) and is_local_host(host):
                        self_ep = ep
                        break
            if not self_ep:
                raise ConfigError("cannot tell which coordinator_candidates entry this process "
                                  "is; set [federation] coordinator_self or --self")
            store = TermStore(self.persist_dir)
            self.election = Election(
                cands, self_ep, interval=s.election_interval_sec,
                missed_polls=s.election_missed_polls, settle_polls=s.election_settle_polls,
                clock_margin=s.election_clock_margin, failback_sec=s.election_failback_sec,
                require_quorum=s.require_quorum, store=store, cluster_id=s.cluster_id,
                on_change=self._on_role_change, schedule_version_fn=lambda: self._version,
                schedule_term_fn=lambda: self._sync_term,
                logger=logging.getLogger(__name__ + ".election"))
        except ValueError as e:
            raise ConfigError(str(e))
        for w in self.election.warnings:
            self.logger.warning("election: %s", w)
        self.election_runner = ElectionRunner(self.election, auth_token=s.auth_token or None,
                                              peer_timeout=s.peer_timeout,
                                              request=self._election_request)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Bind and start serving (raises OSError when the port cannot be bound)."""
        s = self.settings
        sock = socket.socket(socket.AF_INET6 if ":" in self.listen_address else socket.AF_INET,
                             socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.listen_address, self.port))
            sock.listen(32)
        except OSError as e:
            sock.close()
            self.logger.error("Failed to bind coordinator on port %d: %s", self.port, e)
            raise
        sock.settimeout(0.5)
        self._server_sock = sock
        self.port = sock.getsockname()[1]
        self._general_pool = ThreadPoolExecutor(max_workers=int(s.max_handlers),
                                                thread_name_prefix="coord")
        self._reserved_pool = ThreadPoolExecutor(max_workers=int(s.reserved_handlers),
                                                 thread_name_prefix="coord-prio")
        self._general_free = threading.BoundedSemaphore(int(s.max_handlers))
        self._reserved_free = threading.BoundedSemaphore(int(s.reserved_handlers))
        self._server_thread = threading.Thread(target=self._serve, daemon=True,
                                               name="federation_coordinator")
        self._server_thread.start()
        self._spawn(self._probe_loop, "coord-probe")
        self._resolver_running = True
        self._spawn(self._maintenance_loop, "coord-maint")
        if self.election_runner is not None:
            self.election_runner.start()
        if self.reconcile_enabled or self.election is not None or \
                (self.scheduler is not None and self.scheduler.has_master_schedule()):
            self._ensure_poller()
        if self.reconcile_enabled:
            self._spawn(self._reconcile_loop, "coord-reconcile")
        self._spawn(self._xack_probe_all, "coord-xack-probe")
        self.logger.info("Federation coordinator started on %s:%d with %d members (role %s)",
                         self.listen_address, self.port, len(self._members()),
                         self._role_term()[0])

    def _spawn(self, target, name, *args):
        t = threading.Thread(target=target, args=args, daemon=True, name=name)
        t.start()
        return t

    def close(self):
        """Stop the coordinator."""
        self._stop_event.set()
        self._reconcile_kick.set()
        self._resolver_kick.set()
        if self.election_runner is not None:
            self.election_runner.close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=3)
        if self._poller is not None:
            self._poller.close()
        self._registry.cancel_all()
        for pool in (self._general_pool, self._reserved_pool):
            if pool is not None:
                pool.shutdown(wait=False)
        self.logger.info("Federation coordinator stopped")

    # ------------------------------------------------------------------
    # server: accept, bounded pools, priority probe (D.1)
    # ------------------------------------------------------------------

    def _serve(self):
        server_sock = self._server_sock
        while not self._stop_event.is_set():
            try:
                client, addr = server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self._allow and not self._allowed(addr[0]):
                try:
                    client.close()
                except OSError:
                    pass
                continue
            if self._general_free.acquire(blocking=False):
                try:
                    self._general_pool.submit(self._serve_conn, client, addr, self._general_free)
                except RuntimeError:
                    self._general_free.release()
                    client.close()
                continue
            try:
                self._probe_queue.put_nowait((client, addr))
            except queue.Full:
                self._send_busy(client)
        server_sock.close()

    def _allowed(self, host):
        try:
            ip = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self._allow)

    @staticmethod
    def _send_busy(client):
        try:
            client.sendall(json_reply(BUSY_LEGACY, False))
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _probe_loop(self):
        """Serve overflow connections whose first bytes are a priority verb.

        All pending sockets are watched at once (selector), so idle overflow
        connections cannot delay a LEADER/LEASE that is already readable;
        one that stays silent for PROBE_IDLE_SEC gets the busy reply.
        """
        sel = selectors.DefaultSelector()
        pending = OrderedDict()             # socket -> (addr, accepted mono)
        try:
            while not self._stop_event.is_set():
                block = not pending
                while True:
                    try:
                        client, addr = (self._probe_queue.get(timeout=0.5) if block
                                        else self._probe_queue.get_nowait())
                    except queue.Empty:
                        break
                    block = False
                    if len(pending) >= PROBE_MAX_PENDING:
                        old, _ = pending.popitem(last=False)
                        self._probe_drop(sel, old)
                    try:
                        sel.register(client, selectors.EVENT_READ)
                    except (OSError, ValueError):
                        self._send_busy(client)
                        continue
                    pending[client] = (addr, time.monotonic())
                if not pending:
                    continue
                try:
                    ready = sel.select(timeout=0.05)
                except (OSError, ValueError):
                    ready = []
                for key, _ev in ready:
                    client = key.fileobj
                    entry = pending.pop(client, None)
                    try:
                        sel.unregister(client)
                    except (KeyError, ValueError, OSError):
                        pass
                    if entry is not None:
                        self._probe_serve(client, entry[0])
                now = time.monotonic()
                while pending:
                    client, (addr, since) = next(iter(pending.items()))
                    if now - since < PROBE_IDLE_SEC:
                        break
                    del pending[client]
                    self._probe_drop(sel, client)
        finally:
            for client in list(pending):
                self._probe_drop(sel, client)
            sel.close()

    def _probe_drop(self, sel, client):
        try:
            sel.unregister(client)
        except (KeyError, ValueError, OSError):
            pass
        self._send_busy(client)

    def _probe_serve(self, client, addr):
        try:
            client.settimeout(0.05)
            data = client.recv(4096)
        except OSError:
            data = b""
        if data and _verb_of(data) in PRIORITY_VERBS and \
                self._reserved_free.acquire(blocking=False):
            try:
                client.settimeout(None)
                self._reserved_pool.submit(self._serve_conn, _PrefixedSocket(client, data),
                                           addr, self._reserved_free)
                return
            except (RuntimeError, OSError):
                self._reserved_free.release()
        self._send_busy(client)

    def _serve_conn(self, client, addr, sem):
        try:
            self._handle_client(client, addr)
        finally:
            sem.release()

    def _handle_client(self, client_sock, addr=None):
        """Read one request, reply once, close (never closes without replying)."""
        token = self.settings.auth_token or None
        try:
            try:
                req = read_request(client_sock, token, **self._read_kw)
            except RequestError as e:
                self._send(client_sock, e.reply, e.newline)
                return
            if req is None:
                return
            client_sock.settimeout(30.0)
            legacy = req.legacy_verb or req.verb not in self._HANDLED
            try:
                reply = self._dispatch(req, addr)
            except Exception as e:
                self.logger.exception("Error handling coordinator request %r", req)
                reply = ({"error": str(e)} if legacy else
                         {"ok": False, "error": "internal_error", "message": str(e)})
            if isinstance(reply, dict) and req.verb in self._READ_VERBS and \
                    self.election is not None and self._role_term()[0] == "follower":
                reply.setdefault("from", "follower")
            self._send(client_sock, reply, not legacy if reply is not None else False)
        except Exception as e:
            self.logger.error("Error handling coordinator client: %s", e)
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    @staticmethod
    def _send(sock, reply, newline):
        if reply is None:
            reply = {"error": "Unknown command"}
        try:
            sock.sendall(json_reply(reply, newline))
        except OSError:
            pass

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    _HANDLED = frozenset(LEGACY_VERB_LIST + NEW_VERBS)
    #: Read verbs a follower serves from its own state (marked "from": "follower").
    _READ_VERBS = frozenset({"STATUS", "HEALTH", "NODES", "MEMBERS", "PEERS", "ASSIGNMENTS",
                             "EVENTS", "DB", "VERSION"})

    def _dispatch(self, req, addr):
        verb = req.verb
        handler = {
            "PING": self._verb_ping, "STATUS": self._verb_status, "FREQ": self._verb_freq_gain,
            "GAIN": self._verb_freq_gain, "INSTANCE": self._verb_instance,
            "REBALANCE": self._verb_rebalance, "VERSION": self._verb_version,
            "LEADER": self._verb_leader, "PREVOTE": self._verb_prevote,
            "LEASE": self._verb_lease, "HEALTH": self._verb_health, "NODES": self._verb_nodes,
            "MEMBERS": self._verb_members, "MEMBER": self._verb_member,
            "REGISTER": self._verb_register, "PEERS": self._verb_peers, "ALL": self._verb_all,
            "NODE": self._verb_node, "FANOUT": self._verb_fanout, "RECAL": self._verb_recal,
            "SCHEDULE": self._verb_schedule, "ASSIGNMENTS": self._verb_assignments,
            "EVENTS": self._verb_events, "DB": self._verb_db, "ROUTER": self._verb_router,
        }.get(verb)
        if handler is None:
            return {"error": "Unknown command"}
        return handler(req, addr)

    # -- deadlines (D.3.1) --
    def _deadline_query(self):
        s = self.settings
        return s.peer_connect_timeout_sec + s.status_timeout_sec + 1.0

    def _deadline_mutation(self):
        s = self.settings
        return (s.mutation_wait_sec + s.peer_connect_timeout_sec + s.xack_timeout_sec
                + s.max_member_rto_sec + 2.0 + 1.0)

    def _t_chunks_max(self, nchunks):
        s = self.settings
        return 1.0 + math.ceil(nchunks / float(max(1, s.schc_window))) * 2.0 * \
            s.schc_rtt_budget_ms / 1000.0

    def _deadline_schedule(self):
        s = self.settings
        nbytes = self.scheduler.whole_schedule_bytes() if self.scheduler is not None else 0
        nchunks = int(math.ceil(nbytes / float(SCHC_CHUNK))) if nbytes > INLINE_LIMIT else 0
        return (self._deadline_mutation() + self._t_chunks_max(nchunks)
                + s.max_member_rto_sec + 2.0)

    def _t_cmd(self, info):
        """Per-command read timeout T_cmd (D.3.1) from an ack-mode record."""
        value = self.settings.hwc_request_timeout_sec
        if value != "auto":
            return float(value)
        if info and info.get("mode") == "extended" and isinstance(info.get("rto"), (int, float)) \
                and isinstance(info.get("hmx"), (int, float)):
            return 2.0 + info["rto"] / 1000.0 + info["hmx"] / 1000.0 + 2.0
        return 14.0

    def _check_member_rto(self, member, info):
        rto, hmx = info.get("rto"), info.get("hmx")
        if not isinstance(rto, (int, float)) or not isinstance(hmx, (int, float)):
            return
        if 2.0 + rto / 1000.0 + hmx / 1000.0 > self.settings.max_member_rto_sec and \
                member.node_key not in self._rto_logged:
            self._rto_logged.add(member.node_key)
            self.logger.warning("member %s advertises rto %d ms + hmx %d ms beyond "
                                "max_member_rto_sec; raise [federation] max_member_rto_sec on "
                                "the coordinator", member.node_key, rto, hmx)

    # -- role / leadership --
    def _role_term(self):
        if self.election is None:
            return "static", 0
        return self.election.role_term()

    def _leader_payload(self):
        return self.election.leader_info() if self.election is not None else None

    def _on_role_change(self, role, term):
        n = self._registry.cancel_all()
        if n:
            self.logger.warning("leadership change (role %s, term %d): cancelled %d in-flight "
                                "fan-outs", role, term, n)
        if role == "leader":
            self._leader_since = time.monotonic()
            self._first_reconcile_done = False
            self._ensure_poller()
            self._reconcile_kick.set()

    def _gate_leader(self, req):
        """None when this coordinator may execute a leader-only verb, else the error reply."""
        role, term = self._role_term()
        if role == "follower":
            return {"ok": False, "error": "not_leader", "leader": self._leader_payload()}
        if role == "leader" and self.settings.require_quorum and not self.election.has_quorum():
            return {"ok": False, "error": "no_quorum"}
        req.role, req.term = role, term
        return None

    def _still_valid(self, req):
        return lambda: self._role_term() == (req.role, req.term)

    def _busy(self, req):
        return dict(BUSY_LEGACY) if req.legacy else {"ok": False, "error": "busy",
                                                     "retry_in_sec": 1}

    def _err(self, req, code, **extra):
        if req.legacy:
            out = {"error": code}
        else:
            out = {"ok": False, "error": code}
        out.update(extra)
        return out

    def _plan(self, req):
        """Acquire the mutation lock within mutation_wait_sec; False = busy."""
        wait_s = max(0.0, min(self.settings.mutation_wait_sec, req.remaining()))
        if not self._mutation_lock.acquire(timeout=wait_s):
            return False
        req.version = self._version
        return True

    def _observe_member_term(self, member, term):
        """An XACK reply carried a term above ours. True = do not send (stepped down)."""
        if self.election is None:
            if member.node_key not in self._static_term_logged:
                self._static_term_logged.add(member.node_key)
                self.logger.info("member %s has fence term %d from an elected coordinator; "
                                 "static mode ignores it", member.node_key, term)
            return False
        self.election.observe_term(term, source="XACK of {}".format(member.node_key))
        return True

    # ------------------------------------------------------------------
    # membership
    # ------------------------------------------------------------------

    def _members(self):
        """Snapshot of the effective membership (static, runtime, registered)."""
        with self._membership_lock:
            out = [m for m in self._static if m.node_key not in self._runtime_removed]
            out.extend(self._runtime_added)
            known = {m.node_key for m in out}
            out.extend(m for k, m in self._registered.items() if k not in known)
            return out

    def _membership_changed(self, new_members=()):
        if self._poller is not None:
            self._poller.set_members(self._poll_members())
        self._resolver_kick.set()
        if self._server_thread is not None and new_members:
            self._spawn(self._xack_probe_all, "coord-xack-probe", list(new_members))

    def _member_state(self, m, rows):
        if m.source == "registered" and self._registration_expired(m):
            return "expired"
        row = rows.get(m.node_key)
        return row["state"] if row else "unknown"

    def _registration_expired(self, m, now=None):
        if m.last_rx_mono is None:
            return False
        now = time.monotonic() if now is None else now
        return now - m.last_rx_mono > self.settings.register_expiry_factor * m.register_interval

    def _find_by_iid(self, iid, members=None):
        members = self._members() if members is None else members
        return [m for m in members if m.instance_id == iid]

    def _find_by_key(self, key, members=None):
        members = self._members() if members is None else members
        key = str(key).strip().lower()
        for m in members:
            if m.node_key == key:
                return m
        return None

    def _compute_instance_ports(self, inst):
        """Ports of a member's instance dict: explicit ``ports`` win, else the formula."""
        if inst.get("ports"):
            p = inst["ports"]
            return {"hwc_port": p.get("ctl"), "status_port": p.get("status"),
                    "iq_port": p.get("iq")}
        iid = inst["instance_id"]
        stride = inst.get("port_stride", 100)
        return {
            "hwc_port": 5001 + iid * stride,
            "status_port": 5002 + iid * stride,
            "iq_port": 5000 + iid * stride,
        }

    def _member_lock(self, key):
        with self._member_locks_guard:
            lock = self._member_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._member_locks[key] = lock
            return lock

    def _resolve(self, host, remaining):
        """Cached addresses of *host* (all of them, in resolver order); [] when none."""
        addrs = self._resolver.addresses(host)
        if addrs:
            return addrs
        if not self._resolver_running:
            self._resolver.refresh([host])
        else:
            self._resolver_kick.set()
            self._resolver.wait(host, min(self.settings.peer_connect_timeout_sec,
                                          max(0.0, remaining)))
        return self._resolver.addresses(host)

    def _connect(self, host, addrs, port, timeout):
        """Connect to *port* on the first reachable of *addrs* within *timeout* in total.

        Like socket.create_connection, every address is tried in order (the one
        that last succeeded for *host* first), so a name whose first address is
        unreachable (``localhost`` -> ``::1`` against an IPv4-only listener)
        still connects. Raises the last OSError when none answers.
        """
        with self._addr_pref_lock:
            pref = self._addr_pref.get(host)
        order = sorted(addrs, key=lambda a: a != pref)
        deadline = time.monotonic() + timeout
        err = None
        for i, addr in enumerate(order):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            # leave the later addresses a share of the budget, as a refused or
            # unroutable first address must not starve a reachable second one
            share = left if i == len(order) - 1 else max(min(left, 0.25), left / (len(order) - i))
            try:
                sock = socket.create_connection((addr, port), timeout=max(0.001, share))
            except OSError as e:
                err = e
                continue
            sock.settimeout(timeout)
            if addr != pref:
                with self._addr_pref_lock:
                    self._addr_pref[host] = addr
            return sock
        if err is None:
            err = socket.timeout("timed out")
        raise err

    # -- ack-mode cache --
    def _ack_cache_get(self, key):
        with self._ack_lock:
            entry = self._ack_cache.get(key)
            if entry is None or time.monotonic() - entry["ts"] > self.settings.xack_cache_sec:
                return None
            return entry

    def _ack_cache_put(self, key, info):
        info = dict(info)
        info["ts"] = time.monotonic()
        with self._ack_lock:
            self._ack_cache[key] = info
        if info.get("mode") == "legacy":
            last = self._legacy_logged.get(key)
            if last is None or time.monotonic() - last > self.settings.xack_cache_sec:
                self._legacy_logged[key] = time.monotonic()
                self.logger.info("member %s is a legacy hw_controller (no XACK); it logs "
                                 "'Unidentified control command: XACK' per coordinator command",
                                 key)

    # -- breaker --
    def _breaker_check(self, key):
        """(blocked R fields or None, probe): *probe* is True for the one caller
        granted the half-open probe (D.3.3); only it may clear the probe flag."""
        s = self.settings
        now = time.monotonic()
        with self._breaker_lock:
            b = self._breakers.get(key)
            if b is None or b.failures < s.breaker_failures:
                return None, False
            if now < b.open_until:
                return {"applied": False, "reason": "breaker_open",
                        "retry_in_sec": round(b.open_until - now, 1)}, False
            if b.probing:
                return {"applied": False, "reason": "breaker_open",
                        "retry_in_sec": round(s.breaker_open_sec, 1)}, False
            b.probing = True
            return None, True

    def _breaker_record(self, key, success, probe=False):
        s = self.settings
        with self._breaker_lock:
            b = self._breakers.setdefault(key, _Breaker())
            if probe:
                b.probing = False
            if success:
                b.failures = 0
                b.open_until = 0.0
            else:
                b.failures += 1
                if b.failures >= s.breaker_failures:
                    b.open_until = time.monotonic() + s.breaker_open_sec

    def _breaker_unprobe(self, key):
        with self._breaker_lock:
            b = self._breakers.get(key)
            if b is not None:
                b.probing = False

    def breaker_failures(self, key):
        with self._breaker_lock:
            b = self._breakers.get(key)
            return 0 if b is None else b.failures

    # ------------------------------------------------------------------
    # :5001 command encoding (unchanged for the existing verbs)
    # ------------------------------------------------------------------

    @classmethod
    def _encode_hwc_command(cls, cmd_str):
        """Translate a text command ('FREQ 433000000') into (verb, payload).

        Raises ValueError for unknown commands or malformed arguments,
        including integers outside the wire field's range (FREQ u64, channel
        values u32) and floats too large for ORNT's f32 fields.
        """
        try:
            return cls._pack_hwc_command(cmd_str)
        except (struct.error, OverflowError) as e:
            raise ValueError(str(e))

    @classmethod
    def _pack_hwc_command(cls, cmd_str):
        parts = cmd_str.split(None, 1)
        if not parts:
            raise ValueError("Empty command")
        cmd = parts[0].upper()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "FREQ":
            return "FREQ", struct.pack("<Q", int(args))
        if cmd in ("GAIN", "EGAN", "BIAS"):
            values = [int(v) for v in args.replace(",", " ").split()]
            if not values:
                raise ValueError("{:s} requires per-channel values".format(cmd))
            return cmd, struct.pack("<{:d}I".format(len(values)), *values)
        if cmd == "ORNT":
            fields = [float(v) for v in args.replace(",", " ").split()]
            if len(fields) < 2:
                raise ValueError("ORNT requires azimuth and elevation")
            return "ORNT", struct.pack("<ff", fields[0], fields[1])
        if cmd == "SCHD":
            payload = args.encode("utf-8")
            if len(payload) > 124:
                raise ValueError("SCHD payload exceeds 124 bytes")
            return "SCHD", payload
        if cmd in cls._NO_PAYLOAD_VERBS:
            return cls._NO_PAYLOAD_VERBS[cmd], b""
        raise ValueError("Unknown HWC command: {:s}".format(cmd))

    @classmethod
    def _encode_member_command(cls, cmd_str):
        """_encode_hwc_command plus the extended verbs RECL HWSQ TLCK TLKA SCHG.

        Returns (verb, payload, values) where values is the per-channel
        list of GAIN/EGAN/BIAS (else None).
        """
        parts = cmd_str.split(None, 1)
        cmd = parts[0].upper() if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""
        if cmd in cls._EXT_NO_PAYLOAD_VERBS:
            return cls._EXT_NO_PAYLOAD_VERBS[cmd], b"", None
        if cmd == "TLKA":
            try:
                value = int(args)
            except ValueError:
                raise ValueError("TLKA requires 1 (approve) or 0 (revoke)")
            if value not in (0, 1):
                raise ValueError("TLKA requires 1 (approve) or 0 (revoke)")
            return "TLKA", struct.pack("<I", value), None
        verb, payload = cls._encode_hwc_command(cmd_str)
        values = None
        if verb in CHANNEL_VERBS:
            values = [int(v) for v in args.replace(",", " ").split()]
            if len(values) > CHANNEL_MAX:
                raise ValueError("{} takes at most {} values".format(verb, CHANNEL_MAX))
        return verb, payload, values

    @staticmethod
    def _recv_exact(sock, nbytes):
        return _recv_exact(sock, nbytes)

    # ------------------------------------------------------------------
    # per-member execution
    # ------------------------------------------------------------------

    def _command_worker(self, verb, payload, values, mutating):
        def worker(member, ctx):
            session = _HwcSession(self, member, ctx)
            try:
                r = session.open()
                if r is not None:
                    return r
                if verb == "RECAL":
                    return self._recal_on(session)
                return session.command(verb, payload, mutating=mutating, values=values)
            finally:
                session.close()
        return worker

    def _recal_on(self, session):
        if session.extended and "recl" in session.caps:
            r = session.command("RECL", b"", mutating=True)
            if not (r.get("applied") is False and r.get("reason") == "disabled"):
                return r
        r = session.command("INIT", b"", mutating=True)
        r["fallback"] = "INIT"
        return r

    def _run_fanout(self, req, members, worker, still_valid=None):
        """fan_out keyed by member uid; returns {uid: R} or raises FanOutBusy."""
        deadline = max(0.0, req.remaining() - 0.25)
        return fan_out(members, worker, max_workers=self.settings.fanout_workers,
                       deadline_sec=deadline, still_valid=still_valid,
                       key=lambda m: m.uid, registry=self._registry)

    @staticmethod
    def _summary(results):
        out = {"applied": 0, "not_applied": 0, "unconfirmed": 0, "timeout": 0, "error": 0}
        for r in results:
            applied = r.get("applied", "absent")
            if r.get("error") == "timeout":
                out["timeout"] += 1
            elif applied is True or (applied == "absent" and r.get("ok")):
                out["applied"] += 1
            elif applied is False:
                out["not_applied"] += 1
            elif applied is None and (r.get("legacy_ack") or r.get("reason") == "held_for_burst"):
                out["unconfirmed"] += 1
            elif r.get("ok") and applied is None:
                out["unconfirmed"] += 1
            else:
                out["error"] += 1
        return out

    def _execute(self, req, members, cmd_str, mutating, still_valid=None):
        """Encode once, fan out, return (by_uid, error_reply)."""
        try:
            verb, payload, values = self._encode_member_command(cmd_str)
        except ValueError as e:
            msg = str(e)
            return {m.uid: {"ok": False, "applied": False, "error": msg, "phase": "connect"}
                    for m in members}, None
        try:
            res = self._run_fanout(req, members, self._command_worker(verb, payload, values,
                                                                      mutating),
                                   still_valid=still_valid)
        except FanOutBusy as e:
            return None, e.reply
        return res, None

    def _fan_out_command(self, cmd, args, targets=None, req=None):
        """Send a command to all (or specified) members' HW controllers (FREQ/GAIN)."""
        if req is None:
            req = _Req(cmd, self._deadline_mutation(), True)
            req.role, req.term = self._role_term()
        cmd_str = "{} {}".format(cmd, args).strip()
        members = targets if targets is not None else self._members()
        members = [m if isinstance(m, Member) else Member(m, "config") for m in members]
        mutating = cmd.upper() in MUTATING_VERBS
        res, busy = self._execute(req, members, cmd_str, mutating,
                                  self._still_valid(req) if mutating else None)
        if busy is not None:
            return dict(BUSY_LEGACY, retry_in_sec=busy.get("retry_in_sec", 5))
        results, by_node = {}, {}
        for m in members:
            r = res[m.uid]
            results[m.legacy_key] = r
            by_node[m.node_key] = r
        return {"command": cmd, "results": results, "by_node": by_node,
                "summary": self._summary([res[m.uid] for m in members]),
                "deadline_ms": req.deadline_ms}

    def _send_hwc_command(self, host, port, cmd_str):
        """Send one command (XACK first) to a HW controller at host:port."""
        inst = {"host": host, "instance_id": 0, "ports": {"ctl": port, "status": None,
                                                          "iq": None}}
        m = Member(inst, "cli")
        req = _Req("INSTANCE", self._deadline_mutation(), True)
        req.role, req.term = self._role_term()
        verb = cmd_str.split(None, 1)[0].upper() if cmd_str.strip() else ""
        res, busy = self._execute(req, [m], cmd_str, verb in MUTATING_VERBS)
        return busy if busy is not None else res[m.uid]

    def _send_to_instance(self, instance_id, cmd_str):
        """Send a command to a specific instance by ID (first match)."""
        matches = self._find_by_iid(instance_id)
        if not matches:
            return {"error": "Instance {} not found".format(instance_id)}
        req = _Req("INSTANCE", self._deadline_mutation(), True)
        req.role, req.term = self._role_term()
        verb = cmd_str.split(None, 1)[0].upper() if cmd_str.strip() else ""
        res, busy = self._execute(req, [matches[0]], cmd_str, verb in MUTATING_VERBS,
                                  self._still_valid(req))
        return busy if busy is not None else res[matches[0].uid]

    def send_schedule(self, target, json_bytes):
        """Load one schedule document on a member (library path of the scheduler).

        *target* is an instance id or a node key. Uses chunked transfer when
        the member advertises ``schb``, the path handover for a same-host
        legacy member, else refuses (``schedule_too_large_for_legacy_server``).
        """
        if isinstance(target, int):
            found = self._find_by_iid(target)
            member = found[0] if found else None
        else:
            member = self._find_by_key(target)
        if member is None:
            return {"error": "Instance {} not found".format(target)}
        req = _Req("SCHEDULE", self._deadline_schedule(), True)
        req.role, req.term = self._role_term()
        text = json_bytes.decode("utf-8") if isinstance(json_bytes, bytes) else json_bytes

        try:
            doc = json.loads(text)
            gen = doc.get("gen") if isinstance(doc, dict) else None
            entries = len(doc.get("entries") or []) if isinstance(doc, dict) else 0
        except ValueError:
            gen, entries = None, 0

        def worker(m, ctx):
            session = _HwcSession(self, m, ctx)
            try:
                r = session.open()
                if r is not None:
                    return r
                r, transfer = self._load_document(session, text, entries)
                path = r.pop("_path", None)
                if r.get("ok") and r.get("applied") is not False:
                    # SCHG proves the gen when the document carries one, else SCHQ
                    by_gen = gen is not None and session.extended and "schg" in session.caps
                    r["verified"] = self._verify_load(session, by_gen, gen, entries)
                    if path is not None and r["verified"]:
                        sched = self.scheduler if self.scheduler is not None \
                            else FederationScheduler()
                        sched.remove_temp_schedule(path)
                r["transfer"] = transfer
                return r
            finally:
                session.close()
        res = self._run_fanout(req, [member], worker, self._still_valid(req))
        return res[member.uid]

    def _load_document(self, session, text, entries):
        """Transfer one per-target document by the D.5.2 rules; returns (R, transfer)."""
        m = session.m
        data = text.encode("utf-8")
        transfer = FederationScheduler.choose_transfer(text, session.caps, m.connect_host)
        if transfer == "inline":
            return session.command("SCHD", data, mutating=True), transfer
        if transfer == "chunked":
            if len(data) > SCHB_LIMIT:
                return session._r(applied=False, reason="too_large", bytes=len(data),
                                  phase="chunks"), transfer
            return session.send_chunked(data), transfer
        if transfer == "path":
            sched = self.scheduler if self.scheduler is not None else FederationScheduler()
            path = sched.write_temp_schedule(text, max(0, m.instance_id))
            if len(path.encode("utf-8")) > INLINE_LIMIT:
                sched.remove_temp_schedule(path)
                return session._r(applied=False, reason="temp_path_too_long",
                                  phase=session.ctx.phase), transfer
            r = session.command("SCHD", path.encode("utf-8"), mutating=True)
            r["_path"] = path
            return r, transfer
        return session._r(applied=False, reason="schedule_too_large_for_legacy_server",
                          bytes=len(data), phase=session.ctx.phase), "none"

    # ------------------------------------------------------------------
    # :5002 polling (STATUS, PEERS, EVENTS, DB_STATS)
    # ------------------------------------------------------------------

    def _status_port(self, m):
        return self._compute_instance_ports(m.inst).get("status_port")

    def _line_worker(self, line):
        payload = (line.strip() + "\n").encode("utf-8")
        s = self.settings

        def worker(m, ctx):
            port = self._status_port(m)
            if port is None:
                return {"error": "member has no status port", "error_kind": "refused"}
            addrs = self._resolve(m.connect_host, ctx.remaining())
            if not addrs:
                return {"error": "dns", "error_kind": "dns"}
            try:
                timeout = max(0.001, min(s.peer_connect_timeout_sec, s.status_timeout_sec,
                                         ctx.remaining()))
                with self._connect(m.connect_host, addrs, port, timeout) as sock:
                    read_s = max(0.001, min(s.status_timeout_sec, ctx.remaining()))
                    sock.settimeout(read_s)
                    sock.sendall(payload)
                    data = recv_until_close(sock, max(0.001, min(read_s, ctx.remaining())))
                obj = json.loads(data.decode("utf-8", errors="replace"))
                if not isinstance(obj, dict):
                    raise ValueError("reply is not a JSON object")
                obj["_rx_wall"] = time.time()
                return obj
            except Exception as e:
                return {"error": str(e) or e.__class__.__name__, "error_kind": classify_error(e)}
        return worker

    def _poll_line(self, line, members, deadline_sec):
        """{uid: reply dict or {"error","error_kind"}} for one :5002 line per member."""
        if not members:
            return {}
        res = fan_out(members, self._line_worker(line),
                      max_workers=self.settings.fanout_workers,
                      deadline_sec=max(0.0, deadline_sec), key=lambda m: m.uid,
                      registry=self._registry)
        out = {}
        for m in members:
            r = res[m.uid]
            if r.get("phase") is not None and r.get("ok") is False:
                r = {"error": r.get("error", "timeout"), "error_kind": "timeout"}
            out[m.uid] = r
        return out

    def _live_status(self, req, members):
        """Single-flight live STATUS poll of *members* (D.1)."""
        key = ("STATUS",) + tuple(sorted(m.uid for m in members))
        deadline = max(0.0, req.remaining() - 0.25)
        t0 = time.monotonic()
        res = self._single_flight.run(key, lambda: self._poll_line("STATUS", members, deadline),
                                      deadline + 0.5)
        return res, _ms(time.monotonic() - t0)

    @staticmethod
    def _strip(obj):
        if isinstance(obj, dict) and "_rx_wall" in obj:
            obj = {k: v for k, v in obj.items() if k != "_rx_wall"}
        return obj

    # ------------------------------------------------------------------
    # background poller, resolver, xack probe, housekeeping
    # ------------------------------------------------------------------

    def _poll_members(self):
        return [m.poll_dict(self._status_port(m)) for m in self._members()
                if self._status_port(m) is not None]

    def _poll_connect(self, address, timeout=None):
        """Poller connect hook: only cached addresses (D.3.4)."""
        host, port = address
        addrs = self._resolve(host, self.settings.peer_connect_timeout_sec)
        if not addrs:
            raise DnsUnavailable("no cached address for {}".format(host))
        if timeout is None:
            timeout = self.settings.peer_connect_timeout_sec
        return self._connect(host, addrs, port, timeout)

    def _ensure_poller(self):
        """Start the background member poller (HEALTH, NODES, reconciliation, election,
        hub-mode REGISTER with want_peers)."""
        with self._poller_lock:
            if self._poller is not None:
                return self._poller
            s = self.settings
            self._resolver_kick.set()
            self._poller = FederationHealth(
                s.instance_id, [], poll_interval=s.health_poll_interval,
                members=self._poll_members(), peer_timeout=s.status_timeout_sec,
                up_successes=s.peer_up_successes, down_misses=s.peer_down_misses,
                flap_window_sec=s.peer_flap_window_sec, flap_threshold=s.peer_flap_threshold,
                max_workers=s.fanout_workers, cluster_id=s.cluster_id,
                stall_after_sec=s.member_stall_after_sec, _connect=self._poll_connect)
            self._poller_started_mono = time.monotonic()
            if self._server_thread is not None:
                self._poller.start()
            return self._poller

    def _health_rows(self):
        return self._poller.get_member_table() if self._poller is not None else {}

    def _health_warm(self):
        """Every member polled since this coordinator took over (D.5.3)."""
        if self._poller is None:
            return False
        since = max(self._leader_since, self._poller_started_mono or 0.0)
        if not self._poller.polled_since(since):
            return False
        if self.reconcile_enabled and self.election is not None and \
                not self._first_reconcile_done:
            return False
        return True

    def _maintenance_loop(self):
        last_dns = 0.0
        while not self._stop_event.is_set():
            self._expire_registrations()
            now = time.monotonic()
            interval = max(0.5, min(self.settings.dns_ttl_sec, self.settings.health_poll_interval))
            if self._resolver_kick.is_set() or now - last_dns >= interval:
                self._resolver_kick.clear()
                last_dns = now
                hosts = [m.connect_host for m in self._members()]
                for m in self._members():
                    if m.static:
                        hosts.append(m.host)
                try:
                    self._resolver.refresh(hosts)
                except Exception:
                    self.logger.exception("member name resolution failed")
            self._resolver_kick.wait(0.5)

    def _xack_probe_all(self, members=None):
        """One XACK (no command) to every member so rto/hmx/caps are cached (D.3.1)."""
        members = self._members() if members is None else members
        if not members:
            return

        def worker(member, ctx):
            session = _HwcSession(self, member, ctx)
            try:
                return session.open(need=0.0) or {"ok": True}
            finally:
                session.close()
        try:
            fan_out(members, worker, max_workers=self.settings.fanout_workers,
                    deadline_sec=self.settings.peer_connect_timeout_sec
                    + self.settings.xack_timeout_sec + 1.0,
                    key=lambda m: m.uid, registry=self._registry)
        except FanOutBusy:
            pass

    def _expire_registrations(self):
        s = self.settings
        now = time.monotonic()
        removed = []
        with self._membership_lock:
            for key, m in list(self._registered.items()):
                if m.last_rx_mono is not None and now - m.last_rx_mono > s.register_retain_sec:
                    del self._registered[key]
                    removed.append(key)
        if removed:
            self.logger.info("registered members removed after register_retain_sec: %s",
                             ", ".join(removed))
            self._membership_changed()

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def _emit(self, event_type, severity, payload):
        evt = {"timestamp": time.time(), "severity": severity, "module": "coordinator",
               "event_type": event_type, "payload": payload}
        self._local_events.append(evt)
        log = self.logger.error if severity == "error" else self.logger.warning
        log("event %s: %s", event_type, payload)
        if self.event_bus is not None:
            try:
                import daq_events
                self.event_bus.emit(daq_events.DAQEvent(
                    timestamp=evt["timestamp"], severity=severity, module="coordinator",
                    event_type=getattr(daq_events, "EVT_" + event_type.upper(), event_type),
                    payload=payload))
            except Exception:
                self.logger.debug("event bus emit failed", exc_info=True)

    # ------------------------------------------------------------------
    # legacy verbs
    # ------------------------------------------------------------------

    def _verb_ping(self, req, addr):
        return {"ok": True, "ts": time.time()}

    def _verb_status(self, req, addr):
        fresh = bool(req.tokens) and req.tokens[0].upper() == "FRESH"
        return self._aggregate_status(fresh=fresh)

    def _aggregate_status(self, fresh=False):
        """Query each member's StatusServer and merge results (C.6)."""
        s = self.settings
        req = _Req("STATUS", self._deadline_query(), True)
        members = self._members()
        cached = False
        poll_ms = 0
        now = time.monotonic()
        if not fresh and s.status_cache_sec > 0 and self._status_cache is not None and \
                now - self._status_cache[0] <= s.status_cache_sec and \
                set(self._status_cache[1]) >= {m.uid for m in members}:
            results = self._status_cache[1]
            poll_ms = self._status_cache[2]
            cached = True
        else:
            try:
                results, poll_ms = self._live_status(req, members)
            except socket.timeout:
                results = {m.uid: {"error": "timeout", "error_kind": "timeout"} for m in members}
            except FanOutBusy:
                return dict(BUSY_LEGACY, retry_in_sec=5)
            self._status_cache = (time.monotonic(), results, poll_ms)

        rows = self._health_rows()
        statuses, node_keys = {}, {}
        unreachable, degraded, stale = [], [], []
        error = False
        unreachable_count = 0
        by_iid = {}
        for m in members:
            r = self._strip(results.get(m.uid, {"error": "not polled"}))
            statuses[m.legacy_key] = r
            node_keys[m.legacy_key] = m.node_key
            by_iid.setdefault(m.instance_id, []).append(m.node_key)
            row = rows.get(m.node_key) or {}
            state = self._member_state(m, rows)
            busy = isinstance(r, dict) and r == {"error": "busy"}
            if isinstance(r, dict) and "error" in r and not busy:
                unreachable_count += 1
                if m.legacy_key not in unreachable:
                    unreachable.append(m.legacy_key)
                error = True
                continue
            if state == "expired":
                error = True
            health = r.get("pipeline_health", "unknown") if isinstance(r, dict) else "unknown"
            member_stale = row.get("stale") or FederationHealth._reported_stale(r)
            if member_stale and m.legacy_key not in stale:
                stale.append(m.legacy_key)
            if health == "error":
                error = True
            elif (health != "ok" or member_stale or row.get("flapping") or busy
                  or (s.peer_down_misses is not None
                      and row.get("busy_streak", 0) >= s.peer_down_misses)):
                if m.legacy_key not in degraded:
                    degraded.append(m.legacy_key)
        if not members:
            overall = "unknown"
        elif error:
            overall = "error"
        elif degraded or stale:
            overall = "degraded"
        else:
            overall = "ok"
        healths = [v.get("pipeline_health", "error") for v in statuses.values()
                   if isinstance(v, dict) and "error" not in v]
        role, term = self._role_term()
        out = {
            "federation_health": overall,
            "instance_count": len(members),
            "healthy_count": sum(1 for h in healths if h in ("ok", "degraded")),
            "instances": statuses,
            "timestamp": time.time(),
            "unreachable_count": unreachable_count,
            "unreachable": unreachable,
            "degraded": degraded,
            "stale": stale,
            "cached": cached,
            "poll_duration_ms": poll_ms,
            "role": role,
            "term": term,
            "node_keys": node_keys,
            "abandoned_workers": self._registry.abandoned_workers(),
            "deadline_ms": req.deadline_ms,
        }
        collisions = [{"instance_id": iid, "node_keys": keys}
                      for iid, keys in by_iid.items() if len(set(keys)) > 1]
        collisions.extend(self._collision_rows())
        if collisions:
            out["key_collisions"] = collisions
        return out

    def _verb_freq_gain(self, req, addr):
        req = self._make_req(req, "mutation")
        gate = self._gate_leader(req)
        if gate is not None:
            return gate
        if not self._plan(req):
            return self._busy(req)
        try:
            members = self._members()
        finally:
            self._mutation_lock.release()
        return self._fan_out_command(req.verb, req.args, targets=members, req=req)

    def _make_req(self, raw, kind):
        legacy = raw.legacy_verb
        if kind == "mutation":
            d = self._deadline_mutation()
        elif kind == "schedule":
            d = self._deadline_schedule()
        else:
            d = self._deadline_query()
        r = _Req(raw.verb, d, legacy)
        r.args = raw.args
        r.tokens = raw.tokens
        r.raw = raw
        return r

    def _verb_instance(self, raw, addr):
        sub = raw.args.split(None, 1)
        usage = {"error": "Usage: INSTANCE <id> <command>"}
        if len(sub) < 2:
            return usage
        try:
            iid = int(sub[0])
        except ValueError:
            return usage
        subcmd = sub[1]
        verb = subcmd.split(None, 1)[0].upper()
        mutating = verb in MUTATING_VERBS
        req = self._make_req(raw, "mutation")
        if mutating:
            gate = self._gate_leader(req)
            if gate is not None:
                return gate
            if not self._plan(req):
                return self._busy(req)
            try:
                members = self._members()
            finally:
                self._mutation_lock.release()
        else:
            req.role, req.term = self._role_term()
            members = self._members()
        matches = self._find_by_iid(iid, members)
        if not matches:
            return {"error": "Instance {} not found".format(iid)}
        if self.cluster_mode and len(matches) > 1:
            return {"error": "ambiguous instance_id",
                    "candidates": [m.node_key for m in matches]}
        target = matches[0]
        res, busy = self._execute(req, [target], subcmd, mutating,
                                  self._still_valid(req) if mutating else None)
        if busy is not None:
            return dict(BUSY_LEGACY, retry_in_sec=busy.get("retry_in_sec", 5))
        r = dict(res[target.uid])
        r.setdefault("node_key", target.node_key)
        return r

    # ------------------------------------------------------------------
    # new verbs: identity, election
    # ------------------------------------------------------------------

    def _coord_node_id(self):
        s = self.settings
        if s.node_id:
            return s.node_id
        try:
            return "{}:coordinator".format(self_host(s.advertise_host or None))
        except ValueError:
            return default_node_id(s.instance_id)

    def _verb_version(self, req, addr):
        role, term = self._role_term()
        return {"ok": True, "protocol": PROTOCOL_VERSION, "fw_version": FW_VERSION,
                "node_id": self._coord_node_id(), "cluster_id": self.settings.cluster_id,
                "role": role, "term": term, "members": len(self._members()),
                "verbs": list(LEGACY_VERB_LIST) + list(NEW_VERBS),
                "deadlines": {"query_ms": _ms(self._deadline_query()),
                              "mutation_ms": _ms(self._deadline_mutation()),
                              "schedule_ms": _ms(self._deadline_schedule())},
                "abandoned_workers": self._registry.abandoned_workers()}

    def _verb_leader(self, req, addr):
        cluster = req.tokens[0] if req.tokens else None
        if self.election is None:
            if cluster and cluster != self.settings.cluster_id:
                return {"ok": False, "error": "cluster_mismatch",
                        "expected": self.settings.cluster_id}
            return {"ok": True, "role": "static", "term": 0, "eligible": True, "reason": None,
                    "schedule_version": self._version, "leader": None,
                    "lease": {"holder": None, "term": 0, "expires_in_ms": 0},
                    "candidates": [], "quorum": True}
        return self.election.handle_leader(cluster)

    def _election_args(self, req):
        if len(req.tokens) < 2:
            return None, {"ok": False, "error": "bad_request",
                          "usage": "{} <term> <host:port> [<cluster_id>]".format(req.verb)}
        try:
            term = int(req.tokens[0])
            if term < 0 or term > TERM_U32_MAX:
                raise ValueError
        except ValueError:
            return None, {"ok": False, "error": "bad_request", "message": "bad term",
                          "usage": "{} <term> <host:port> [<cluster_id>]".format(req.verb)}
        cluster = req.tokens[2] if len(req.tokens) > 2 else None
        return (term, req.tokens[1], cluster), None

    def _verb_prevote(self, req, addr):
        args, err = self._election_args(req)
        if err:
            return err
        if self.election is None:
            return {"ok": True, "granted": False, "term": 0, "lease_holder": None,
                    "reason": "static"}
        try:
            return self.election.handle_prevote(*args)
        except ValueError as e:
            return {"ok": False, "error": "bad_request", "message": str(e)}

    def _verb_lease(self, req, addr):
        args, err = self._election_args(req)
        if err:
            return err
        if self.election is None:
            return {"ok": True, "granted": False, "term": 0, "lease_ms": 0, "reason": "static"}
        try:
            return self.election.handle_lease(*args)
        except ValueError as e:
            return {"ok": False, "error": "bad_request", "message": str(e)}

    def _election_request(self, host, port, line, auth_token=None, connect_timeout=2.0,
                          timeout=2.0):
        """Election transport; observes LEADER replies for pull repair (C.4.7)."""
        reply = request_json(host, port, line, auth_token=auth_token,
                             connect_timeout=connect_timeout, timeout=timeout)
        if line.startswith("LEADER") and isinstance(reply, dict) and \
                reply.get("role") == "leader":
            self._maybe_pull_repair("{}:{}".format(host, port), reply)
        return reply

    # ------------------------------------------------------------------
    # new verbs: health and status
    # ------------------------------------------------------------------

    def _rollup(self, members, rows):
        s = self.settings
        if not members:
            return "unknown", [], [], []
        unreachable, degraded, stale = [], [], []
        error = False
        for m in members:
            row = rows.get(m.node_key)
            state = self._member_state(m, rows)
            if row is None or state == "unknown":
                degraded.append(m.node_key)
                continue
            if state in ("down", "expired") or not row.get("reachable"):
                unreachable.append(m.node_key)
                error = True
                continue
            if row.get("health") == "error":
                error = True
            if row.get("stale"):
                stale.append(m.node_key)
            if (state in ("degraded", "stale", "flapping") or row.get("health") != "ok"
                    or (s.peer_down_misses is not None
                        and row.get("busy_streak", 0) >= s.peer_down_misses)):
                if m.node_key not in degraded:
                    degraded.append(m.node_key)
        if error:
            overall = "error"
        elif degraded or stale:
            overall = "degraded"
        else:
            overall = "ok"
        return overall, unreachable, degraded, stale

    def _verb_health(self, req, addr):
        self._ensure_poller()
        members = self._members()
        rows = self._health_rows()
        overall, unreachable, degraded, stale = self._rollup(members, rows)
        out = {}
        keys = ("reachable", "error_kind", "health", "sync_state", "frequency_hz",
                "seen_age_sec", "snapshot_age_sec", "frame_count_changed_age_sec", "stale",
                "flapping", "busy_streak", "last_seen")
        for m in members:
            row = rows.get(m.node_key, {})
            entry = {k: row.get(k) for k in keys}
            entry["state"] = self._member_state(m, rows)
            out[m.node_key] = entry
        return {"ok": True, "overall": overall, "members": out, "unreachable": unreachable,
                "degraded": degraded, "stale": stale}

    def _verb_nodes(self, raw, addr):
        toks = [t.upper() for t in raw.tokens]
        if not toks or toks[0] != "STATUS" or (len(toks) > 1 and toks[1:] != ["FRESH"]):
            return {"ok": False, "error": "bad_request", "usage": "NODES STATUS [FRESH]"}
        fresh = len(toks) > 1
        req = self._make_req(raw, "query")
        poller = self._ensure_poller()
        members = self._members()
        snaps = poller.get_member_snapshots()
        nodes = {}
        cached = not fresh and all(snaps.get(m.node_key, (None, None))[0] is not None
                                   for m in members)
        poll_ms = 0
        if cached:
            rows = self._health_rows()
            for m in members:
                row = rows.get(m.node_key, {})
                if row.get("reachable"):
                    nodes[m.node_key] = snaps[m.node_key][0]
                else:
                    nodes[m.node_key] = {"error": "unreachable",
                                         "error_kind": row.get("error_kind", "refused")}
        else:
            try:
                results, poll_ms = self._live_status(req, members)
            except socket.timeout:
                results = {m.uid: {"error": "timeout", "error_kind": "timeout"} for m in members}
            except FanOutBusy as e:
                return e.reply
            for m in members:
                r = self._strip(results.get(m.uid))
                if isinstance(r, dict) and "error" in r and "error_kind" not in r:
                    r = dict(r, error_kind="busy" if r.get("error") == "busy" else "refused")
                nodes[m.node_key] = r
        overall, _u, _d, _s = self._rollup(members, self._health_rows())
        if not cached:
            if any("error" in (v or {}) for v in nodes.values()):
                overall = "error"
        return {"ok": True, "overall": overall, "cached": cached, "poll_duration_ms": poll_ms,
                "nodes": nodes, "deadline_ms": req.deadline_ms}

    def _member_query(self, raw, line):
        req = self._make_req(raw, "query")
        members = self._members()
        try:
            res = self._single_flight.run(
                (line,) + tuple(sorted(m.uid for m in members)),
                lambda: self._poll_line(line, members, req.remaining() - 0.25),
                req.remaining())
        except socket.timeout:
            res = {m.uid: {"error": "timeout", "error_kind": "timeout"} for m in members}
        return req, members, res

    def _verb_peers(self, raw, addr):
        try:
            req, members, res = self._member_query(raw, "PEERS")
        except FanOutBusy as e:
            return e.reply
        return {"ok": True, "peers": {m.node_key: self._strip(res[m.uid]) for m in members},
                "deadline_ms": req.deadline_ms}

    def _verb_db(self, raw, addr):
        if [t.upper() for t in raw.tokens] != ["STATS"]:
            return {"ok": False, "error": "bad_request", "usage": "DB STATS"}
        try:
            req, members, res = self._member_query(raw, "DB_STATS")
        except FanOutBusy as e:
            return e.reply
        return {"ok": True, "nodes": {m.node_key: self._strip(res[m.uid]) for m in members},
                "deadline_ms": req.deadline_ms}

    def _verb_router(self, raw, addr):
        if [t.upper() for t in raw.tokens] != ["STATS"]:
            return {"ok": False, "error": "bad_request", "usage": "ROUTER STATS"}
        ep = (self.settings.router_stats_endpoint or "").strip()
        if not ep:
            return {"ok": False, "error": "router_not_configured"}
        host, _, port = ep.rpartition(":")
        try:
            stats = request_json(host, int(port), "STATS",
                                 auth_token=self.settings.auth_token or None,
                                 connect_timeout=self.settings.peer_connect_timeout_sec,
                                 timeout=self.settings.status_timeout_sec)
        except (OSError, ValueError) as e:
            return {"ok": False, "error": "router_unreachable", "message": str(e)}
        return {"ok": True, "router": ep, "stats": stats}

    def _event_rx_ts(self, nk, event, now):
        ident = json.dumps(event, sort_keys=True, default=str)
        with self._event_seen_lock:
            seen = self._event_seen.setdefault(nk, OrderedDict())
            ts = seen.get(ident)
            if ts is None:
                seen[ident] = now
                ts = now
                while len(seen) > 4 * EVENTS_MAX:
                    seen.popitem(last=False)
            return ts

    def _verb_events(self, raw, addr):
        n = EVENTS_DEFAULT
        bound_age = None
        bound_ts = None
        usage = "EVENTS [n] [age:<sec> | <since_ts>]"
        toks = list(raw.tokens)
        try:
            if toks and toks[0].isdigit():
                n = int(toks.pop(0))
                if not 1 <= n <= EVENTS_MAX:
                    raise ValueError("n must be 1..{}".format(EVENTS_MAX))
            if toks:
                t = toks.pop(0)
                if t.lower().startswith("age:"):
                    bound_age = float(t[4:])
                    if bound_age < 0:
                        raise ValueError("age must be >= 0")
                else:
                    bound_ts = float(t)
            if toks:
                raise ValueError("too many arguments")
        except ValueError as e:
            return {"ok": False, "error": "bad_request", "message": str(e), "usage": usage}
        try:
            req, members, res = self._member_query(raw, "EVENTS")
        except FanOutBusy as e:
            return e.reply
        now = time.time()
        merged = []
        errors = {}
        for order, m in enumerate(members):
            r = res[m.uid]
            evs = r.get("events") if isinstance(r, dict) else None
            if not isinstance(evs, list):
                errors[m.node_key] = (r or {}).get("error", "no events")
                continue
            for idx, ev in enumerate(evs):
                if not isinstance(ev, dict):
                    continue
                rx = self._event_rx_ts(m.node_key, ev, now)
                merged.append((rx, order, idx, dict(ev, node_key=m.node_key, rx_ts=rx)))
        for idx, ev in enumerate(self._local_events):
            rx = self._event_rx_ts("coordinator", ev, now)
            merged.append((rx, len(members), idx, dict(ev, node_key="coordinator", rx_ts=rx)))
        if bound_age is not None:
            merged = [e for e in merged if e[0] >= now - bound_age]
        elif bound_ts is not None:
            merged = [e for e in merged if e[0] >= bound_ts]
        merged.sort(key=lambda e: (e[0], e[1], e[2]))
        out = {"ok": True, "now": now, "events": [e[3] for e in merged[-n:]]}
        if errors:
            out["errors"] = errors
        return out

    # ------------------------------------------------------------------
    # new verbs: membership
    # ------------------------------------------------------------------

    def _member_row(self, m, rows, now):
        age = None if m.last_rx_mono is None else round(now - m.last_rx_mono, 1)
        return {"node_key": m.node_key, "node_id": m.node_id, "host": m.host,
                "advertised_host": m.advertised_host, "instance_id": m.instance_id,
                "ports": m.ports, "source": m.source, "state": self._member_state(m, rows),
                "addr": m.addr, "last_register_age_sec": age}

    def _collision_rows(self):
        with self._membership_lock:
            return [{"node_key": k[0], "holder_addr": k[1], "rejected_addr": k[2],
                     "since": since} for k, since in self._collisions.items()]

    def _members_reply(self):
        rows = self._health_rows()
        now = time.monotonic()
        return [self._member_row(m, rows, now) for m in self._members()]

    def _verb_members(self, req, addr):
        return {"ok": True, "members": self._members_reply(),
                "key_collisions": self._collision_rows()}

    def _verb_member(self, raw, addr):
        usage = "MEMBER ADD host:iid[@node_id] | MEMBER REMOVE <node_key>"
        if len(raw.tokens) != 2 or raw.tokens[0].upper() not in ("ADD", "REMOVE"):
            return {"ok": False, "error": "bad_request", "usage": usage}
        req = self._make_req(raw, "mutation")
        gate = self._gate_leader(req)
        if gate is not None:
            return gate
        action, arg = raw.tokens[0].upper(), raw.tokens[1]
        if action == "ADD":
            try:
                parsed = parse_member(arg, default_stride=self.settings.port_stride,
                                      allow_loopback=not self.cluster_mode)
            except ValueError as e:
                return {"ok": False, "error": "bad_request", "message": str(e)}
        if not self._plan(req):
            return self._busy(req)
        new_members = []
        try:
            with self._membership_lock:
                if action == "ADD":
                    key = parsed["node_key"]
                    existing = next((m for m in self._runtime_added if m.node_key == key), None)
                    static = next((m for m in self._static if m.node_key == key), None)
                    if existing is not None:
                        if existing.host == parsed["host"] and \
                                existing.node_id == parsed["node_id"]:
                            changed = False
                        else:
                            return {"ok": False, "error": "duplicate_member", "node_key": key}
                    elif static is not None and key not in self._runtime_removed:
                        if static.host == parsed["host"] and \
                                (parsed["node_id"] in (None, static.node_id)):
                            changed = False
                        else:
                            return {"ok": False, "error": "duplicate_member", "node_key": key}
                    elif static is not None:
                        self._runtime_removed.discard(key)
                        changed = True
                        new_members.append(static)
                    else:
                        m = Member(parsed, "runtime")
                        self._runtime_added.append(m)
                        new_members.append(m)
                        changed = True
                else:
                    key = arg.strip().lower()
                    if any(m.node_key == key for m in self._runtime_added):
                        self._runtime_added = [m for m in self._runtime_added
                                               if m.node_key != key]
                        changed = True
                    elif any(m.node_key == key for m in self._static) and \
                            key not in self._runtime_removed:
                        self._runtime_removed.add(key)
                        changed = True
                    elif key in self._registered:
                        del self._registered[key]
                        changed = True
                    else:
                        return {"ok": False, "error": "unknown_member", "node_key": key}
            if changed:
                self._commit_version()
        finally:
            self._mutation_lock.release()
        if changed:
            self._membership_changed(new_members)
        replicated, ok, durable = self._replicate()
        out = {"ok": True, "members": self._members_reply(), "changed": changed,
               "replicated": replicated}
        return self._replication_result(out, ok, durable)

    def _replication_result(self, out, ok, durable):
        if not ok:
            return {"ok": False, "error": "not_replicated", "replicated": out["replicated"],
                    "version": self._version}
        if self.election is not None and not self.settings.require_quorum:
            out["durable"] = durable
        elif self.election is None:
            out["durable"] = True
        return out

    def _verb_register(self, req, addr):
        try:
            body = req.body_json()
        except RequestError as e:
            return e.reply
        if not isinstance(body, dict):
            return {"ok": False, "error": "bad_request", "message": "REGISTER needs a JSON body"}
        s = self.settings
        try:
            nk = str(body["node_key"]).strip().lower()
            iid = int(body["instance_id"])
            nonce = str(body.get("nonce", ""))
            cluster = str(body.get("cluster_id", "default"))
            stride = int(body.get("port_stride", s.port_stride))
            interval = float(body.get("register_interval_sec", 5.0))
            if not 0.1 <= interval <= 3600:
                raise ValueError("register_interval_sec out of range")
            parsed_key = parse_member(nk, default_stride=stride, allow_loopback=True)
            if parsed_key["instance_id"] != iid:
                raise ValueError("node_key '{}' does not end in instance_id {}".format(nk, iid))
            ports = body.get("ports")
            if ports is not None:
                if not isinstance(ports, dict):
                    raise ValueError("ports must be an object")
                ports = {k: int(ports[k]) for k in ("iq", "ctl", "status", "events", "zmq")
                         if k in ports}
                if any(not 1 <= p <= 65535 for p in ports.values()):
                    raise ValueError("port out of range")
            else:
                ports = compute_ports(iid, stride)
            node_id = body.get("node_id")
            if node_id is not None and not is_valid_node_id(str(node_id)):
                raise ValueError("bad node_id '{}'".format(node_id))
            adv = str(body.get("advertise_host") or parsed_key["host"]).strip().lower()
        except (KeyError, TypeError, ValueError) as e:
            return {"ok": False, "error": "bad_request", "message": str(e)}
        if cluster != s.cluster_id:
            return {"ok": False, "error": "cluster_mismatch", "expected": s.cluster_id}
        src = addr[0] if addr else "127.0.0.1"
        host = adv if _Resolver._literal(adv) is not None else src
        now = time.monotonic()
        new_member = None
        moved = None
        collision = None
        refused = None
        with self._membership_lock:
            static = next((m for m in self._static if m.node_key == nk
                           and nk not in self._runtime_removed), None)
            holder = self._registered.get(nk)
            if static is not None:
                static_addrs = set(self._resolver.addresses(static.host))
                static_addrs.update(self._resolver.addresses(static.connect_host))
                local_ok = is_loopback(src) and is_local_host(static.connect_host)
                if src in static_addrs or local_ok:
                    static.nonce, static.addr, static.last_rx_mono = nonce, src, now
                    accepted_as = static
                else:
                    collision = {"addr": static.host, "node_id": static.node_id, "nonce": None}
            elif holder is None or holder.nonce == nonce or holder.addr == src or \
                    self._registration_expired(holder, now):
                if holder is not None and holder.nonce == nonce:
                    # same registrant (e.g. a DHCP address change): follow its
                    # new address/ports and re-point the poller when they moved
                    holder.addr = src
                    holder.last_rx_mono = now
                    holder.register_interval = interval
                    if (holder.connect_host, holder.ports) != (host, ports):
                        holder.host = holder.connect_host = host
                        holder.ports = dict(ports)
                        holder.inst["host"] = holder.inst["connect_host"] = host
                        holder.inst["ports"] = dict(ports)
                        moved = holder
                    accepted_as = holder
                elif not self._register_admit(nk, src, now):
                    refused = {"ok": False, "error": "busy",
                               "retry_in_sec": int(REGISTER_RATE_WINDOW_SEC)}
                else:
                    inst = {"host": host, "connect_host": host, "instance_id": iid,
                            "node_key": nk, "node_id": node_id, "ports": ports,
                            "port_stride": stride, "advertised_host": adv}
                    m = Member(inst, "registered")
                    m.advertised_host = adv
                    m.addr, m.nonce, m.last_rx_mono = src, nonce, now
                    m.register_interval = interval
                    self._registered[nk] = m
                    accepted_as = m
                    new_member = m
            else:
                collision = {"addr": holder.addr, "node_id": holder.node_id,
                             "nonce": holder.nonce}
            if collision is not None:
                ckey = (nk, collision["addr"], src)
                self._collisions.setdefault(ckey, time.time())
                while len(self._collisions) > 256:
                    self._collisions.popitem(last=False)
        if refused is not None:
            self.logger.warning("REGISTER of %s from %s refused: registration limit "
                                "(%d members, %d new keys per source per %.0f s)", nk, src,
                                REGISTER_MAX_MEMBERS, REGISTER_NEW_PER_SOURCE,
                                REGISTER_RATE_WINDOW_SEC)
            return refused
        if collision is not None:
            mono = time.monotonic()
            last = self._collision_events.get(ckey)
            if last is None or mono - last >= COLLISION_EVENT_SEC:
                self._collision_events.pop(ckey, None)
                self._collision_events[ckey] = mono
                while self._collision_events:
                    k0, t0 = next(iter(self._collision_events.items()))
                    if len(self._collision_events) <= 256 and mono - t0 < COLLISION_EVENT_SEC:
                        break
                    del self._collision_events[k0]
                self._emit("node_key_collision", "error",
                           {"node_key": nk, "holder_addr": collision["addr"],
                            "rejected_addr": src})
            return {"ok": False, "error": "duplicate_node_key", "holder": collision}
        if new_member is not None or moved is not None:
            self._membership_changed([new_member or moved])
        role, term = self._role_term()
        reply = {"ok": True, "term": term, "role": role, "leader": self._leader_payload(),
                 "next_register_sec": interval,
                 "registered_as": {"node_key": nk, "host": accepted_as.host}}
        if body.get("want_peers"):
            # hub mode (C.2/C.3) takes its peer table from this ack, so the
            # rows must come from a running poller even on a static coordinator
            self._ensure_poller()
            rows = self._health_rows()
            reply["peers"] = {m.node_key: rows.get(m.node_key, {"state": "unknown"})
                              for m in self._members() if m.node_key != nk}
        return reply

    def _register_admit(self, nk, src, now):
        """Admit a new registered node_key (membership lock held).

        Bounds the registered table (REGISTER_MAX_MEMBERS, expired entries
        are evicted first) and the new keys per source address per window.
        """
        rate = self._register_rate
        while rate:
            k0, (t0, _n) = next(iter(rate.items()))
            if now - t0 < REGISTER_RATE_WINDOW_SEC and len(rate) <= 4096:
                break
            del rate[k0]
        win = rate.get(src)
        if win is not None and win[1] >= REGISTER_NEW_PER_SOURCE:
            return False
        if nk not in self._registered and len(self._registered) >= REGISTER_MAX_MEMBERS:
            for key, m in list(self._registered.items()):
                if self._registration_expired(m, now):
                    del self._registered[key]
                    break
            else:
                return False
        if win is None:
            rate[src] = [now, 1]
        else:
            win[1] += 1
        return True

    # ------------------------------------------------------------------
    # new verbs: ALL / NODE / FANOUT / RECAL
    # ------------------------------------------------------------------

    def _parse_hwc_verb(self, text):
        verb = text.split(None, 1)[0].upper() if text.strip() else ""
        known = (set(self._NO_PAYLOAD_VERBS) | set(self._EXT_NO_PAYLOAD_VERBS)
                 | {"FREQ", "GAIN", "EGAN", "BIAS", "ORNT", "SCHD", "TLKA"})
        return verb, verb in known

    def _targeted(self, raw, members, cmd_text, refuse_schd=False, key_reply=None):
        verb, known = self._parse_hwc_verb(cmd_text)
        if not known:
            return {"ok": False, "error": "unknown_hwc_command", "verb": verb}
        if refuse_schd and verb == "SCHD":
            return {"ok": False, "error": "use_schedule_set"}
        mutating = verb in MUTATING_VERBS
        req = self._make_req(raw, "mutation")
        if mutating:
            gate = self._gate_leader(req)
            if gate is not None:
                return gate
            if not self._plan(req):
                return self._busy(req)
            try:
                members = members()
            finally:
                self._mutation_lock.release()
        else:
            req.role, req.term = self._role_term()
            members = members()
        if isinstance(members, dict):
            return members
        res, busy = self._execute(req, members, cmd_text, mutating,
                                  self._still_valid(req) if mutating else None)
        if busy is not None:
            return busy
        if key_reply is not None:
            return key_reply(req, members, res)
        results = {}
        for m in members:
            results[m.node_key] = res[m.uid]
        return {"ok": True, "command": verb, "results": results,
                "summary": self._summary([res[m.uid] for m in members]),
                "deadline_ms": req.deadline_ms}

    def _verb_all(self, raw, addr):
        if not raw.args:
            return {"ok": False, "error": "bad_request", "usage": "ALL <VERB> [args]"}
        return self._targeted(raw, self._members, raw.args, refuse_schd=True)

    def _verb_node(self, raw, addr):
        parts = raw.args.split(None, 1)
        if len(parts) < 2:
            return {"ok": False, "error": "bad_request", "usage": "NODE <node_key> <VERB> [args]"}
        key = parts[0].lower()

        def members():
            m = self._find_by_key(key)
            return [m] if m is not None else {"ok": False, "error": "unknown_member",
                                              "node_key": key}

        def reply(req, ms, res):
            return {"ok": True, "node": key, "result": res[ms[0].uid],
                    "deadline_ms": req.deadline_ms}
        return self._targeted(raw, members, parts[1], key_reply=reply)

    def _verb_fanout(self, raw, addr):
        parts = raw.args.split(None, 1)
        if len(parts) < 2:
            return {"ok": False, "error": "bad_request",
                    "usage": "FANOUT <k1,k2,...> <VERB> [args]"}
        keys = [k.strip().lower() for k in parts[0].split(",") if k.strip()]

        def members():
            all_m = self._members()
            out = []
            for k in keys:
                m = self._find_by_key(k, all_m)
                if m is None:
                    return {"ok": False, "error": "unknown_member", "node_key": k}
                if m not in out:
                    out.append(m)
            return out
        return self._targeted(raw, members, parts[1])

    def _verb_recal(self, raw, addr):
        req = self._make_req(raw, "mutation")
        gate = self._gate_leader(req)
        if gate is not None:
            return gate
        if len(raw.tokens) > 1:
            return {"ok": False, "error": "bad_request", "usage": "RECAL [node_key]"}
        if not self._plan(req):
            return self._busy(req)
        try:
            members = self._members()
        finally:
            self._mutation_lock.release()
        if raw.tokens:
            m = self._find_by_key(raw.tokens[0], members)
            if m is None:
                return {"ok": False, "error": "unknown_member", "node_key": raw.tokens[0].lower()}
            members = [m]
        try:
            res = self._run_fanout(req, members, self._command_worker("RECAL", b"", None, True),
                                   self._still_valid(req))
        except FanOutBusy as e:
            return e.reply
        results = {m.node_key: res[m.uid] for m in members}
        if raw.tokens:
            return {"ok": True, "node": members[0].node_key, "result": res[members[0].uid],
                    "deadline_ms": req.deadline_ms}
        return {"ok": True, "command": "RECL", "results": results,
                "summary": self._summary(list(results.values())),
                "deadline_ms": req.deadline_ms}

    # ------------------------------------------------------------------
    # scheduling: SCHEDULE *, REBALANCE, ASSIGNMENTS
    # ------------------------------------------------------------------

    def _need_scheduler(self):
        if self.scheduler is None:
            self.scheduler = FederationScheduler(coordinator=self)
        return self.scheduler

    def _commit_version(self):
        """Bump the replicated version (mutation lock held) and persist."""
        _role, term = self._role_term()
        self._version += 1
        self._sync_term = term
        self._persist()

    def _verb_schedule(self, raw, addr):
        toks = raw.tokens
        sub = toks[0].upper() if toks else ""
        if sub == "SET":
            return self._schedule_set(raw)
        if sub == "GET":
            return self._schedule_get(raw)
        if sub == "CLEAR":
            return self._schedule_clear(raw)
        if sub == "STRATEGY":
            return self._schedule_strategy(raw)
        if sub == "SYNC":
            return self._schedule_sync(raw)
        return {"ok": False, "error": "bad_request",
                "usage": "SCHEDULE SET [round_robin|range] @n | GET | CLEAR | "
                         "STRATEGY round_robin|range | SYNC @n"}

    def _schedule_set(self, raw):
        req = self._make_req(raw, "mutation")
        gate = self._gate_leader(req)
        if gate is not None:
            return gate
        strategy = None
        word = raw.tokens[1] if len(raw.tokens) > 1 else ""
        if word and word[0] not in "{[":
            if word not in STRATEGIES:
                return {"ok": False, "error": "bad_request",
                        "message": "unknown strategy '{}'".format(word),
                        "usage": "SCHEDULE SET [round_robin|range] @n"}
            strategy = word
        try:
            doc = raw.body_json()
        except RequestError as e:
            return e.reply
        if doc is None:
            return {"ok": False, "error": "bad_request", "message": "no schedule document",
                    "usage": "SCHEDULE SET [round_robin|range] @n"}
        probe = FederationScheduler()
        try:
            canon = probe.set_master_schedule_doc(doc, strategy)
        except ValueError as e:
            return {"ok": False, "error": "bad_schedule", "message": str(e)}
        nbytes = probe.whole_schedule_bytes()
        if nbytes > SCHB_LIMIT:
            return {"ok": False, "error": "bad_schedule",
                    "message": "schedule is {} bytes; the per-target limit is {} (SCHB)".format(
                        nbytes, SCHB_LIMIT)}
        sched = self._need_scheduler()
        if not self._plan(req):
            return self._busy(req)
        try:
            canon = sched.set_master_schedule_doc(canon)
            self._set_at = time.time()
            self._commit_version()
            version = self._version
        finally:
            self._mutation_lock.release()
        self._ensure_poller()
        rows = self._health_rows()
        targets = self._partition_targets(self._members(), rows, False) or \
            sorted(m.node_key for m in self._members())
        preview = probe.partition_schedule(targets=targets) if targets else {}
        replicated, ok, durable = self._replicate()
        out = {"ok": True, "entries": len(canon["entries"]), "strategy": canon["strategy"],
               "version": version, "bytes": len(json.dumps(canon, separators=(",", ":"))),
               "assignments": {k: len(v["entries"]) for k, v in preview.items()},
               "replicated": replicated}
        return self._replication_result(out, ok, durable)

    def _schedule_get(self, raw):
        if len(raw.tokens) > 1 and raw.tokens[1].upper() == "SYNC":
            return {"ok": True, "sync": self._sync_doc()}
        doc = self.scheduler.get_master_schedule_doc() if self.scheduler is not None else None
        if doc is None:
            return {"ok": False, "error": "no_master_schedule"}
        out = {"ok": True, "schedule": doc, "version": self._version,
               "term": self._sync_term, "set_at": self._set_at}
        if self._role_term()[0] == "follower":
            out["from"] = "follower"
        return out

    def _schedule_strategy(self, raw):
        req = self._make_req(raw, "mutation")
        gate = self._gate_leader(req)
        if gate is not None:
            return gate
        if len(raw.tokens) != 2 or raw.tokens[1] not in STRATEGIES:
            return {"ok": False, "error": "bad_request",
                    "usage": "SCHEDULE STRATEGY round_robin|range"}
        if self.scheduler is None or not self.scheduler.has_master_schedule():
            return {"ok": False, "error": "no_master_schedule"}
        if not self._plan(req):
            return self._busy(req)
        try:
            self.scheduler.set_strategy(raw.tokens[1])
            self._commit_version()
        finally:
            self._mutation_lock.release()
        replicated, ok, durable = self._replicate()
        return self._replication_result({"ok": True, "strategy": raw.tokens[1],
                                         "replicated": replicated}, ok, durable)

    def _schedule_clear(self, raw):
        req = self._make_req(raw, "mutation")
        gate = self._gate_leader(req)
        if gate is not None:
            return gate
        if not self._schedule_op.acquire(timeout=max(0.0, min(self.settings.mutation_wait_sec,
                                                               req.remaining()))):
            return self._busy(req)
        try:
            if not self._plan(req):
                return self._busy(req)
            try:
                members = self._members()
                rows = self._health_rows()
                if self.scheduler is not None:
                    self.scheduler.clear_master_schedule()
                for m in members:
                    rec = self._assignments.get(m.node_key) or {}
                    self._assignments[m.node_key] = self._desired_record(
                        None, "inactive", [], rec.get("last_frame_count"))
                self._empty = []
                self._commit_version()
                version = self._version
            finally:
                self._mutation_lock.release()
            reachable = [m for m in members if (rows.get(m.node_key) or {}).get("reachable")
                         or self._poller is None]
            try:
                res = self._run_fanout(req, reachable,
                                       self._command_worker("SCHS", b"", None, True),
                                       self._still_valid(req))
            except FanOutBusy as e:
                return e.reply
            with self._mutation_lock:
                if self._version == version:
                    for m in reachable:
                        rec = self._assignments.get(m.node_key)
                        if rec is not None:
                            r = res[m.uid]
                            rec["last_result"] = r
                            rec["state"] = "applied" if r.get("ok") and \
                                r.get("applied") is not False else "pending"
                    self._persist()
            stopped = {m.node_key: res[m.uid] for m in reachable}
        finally:
            self._schedule_op.release()
        replicated, ok, durable = self._replicate()
        return self._replication_result({"ok": True, "stopped": stopped,
                                         "replicated": replicated}, ok, durable)

    @staticmethod
    def _desired_record(gen, desired, partition, last_frame_count=None):
        return {"gen": gen, "desired": desired, "state": "pending", "attempts": 0,
                "next_try_mono": 0.0, "last_result": None, "last_frame_count": last_frame_count,
                "partition": [dict(e) for e in partition], "entries": len(partition)}

    def _partition_targets(self, members, rows, include_degraded):
        if self.settings.health_mode == "off":
            return sorted(m.node_key for m in members)
        states = ("up", "degraded") if include_degraded else ("up",)
        keys = sorted({m.node_key for m in members
                       if self._member_state(m, rows) in states})
        return keys

    def _verb_rebalance(self, raw, addr):
        req = self._make_req(raw, "schedule")
        gate = self._gate_leader(req)
        if gate is not None:
            return gate
        strategy = None
        include_degraded = False
        for tok in raw.tokens:
            if tok.upper() == "INCLUDE_DEGRADED":
                include_degraded = True
            elif tok in STRATEGIES:
                strategy = tok
            else:
                return {"error": "Usage: REBALANCE [round_robin|range] [INCLUDE_DEGRADED]"}
        if self.scheduler is None:
            return {"error": "no federation scheduler configured"}
        if not self.scheduler.has_master_schedule():
            return {"error": "no master schedule"}
        self._ensure_poller()
        if not self._health_warm():
            return {"error": "health_warming",
                    "retry_in_sec": int(math.ceil(self.settings.health_poll_interval))}
        if not self._schedule_op.acquire(timeout=max(0.0, min(self.settings.mutation_wait_sec,
                                                               req.remaining()))):
            return self._busy(req)
        try:
            return self._rebalance(req, strategy, include_degraded)
        finally:
            self._schedule_op.release()

    def _rebalance(self, req, strategy, include_degraded):
        if not self._plan(req):
            return self._busy(req)
        try:
            members = self._members()
            rows = self._health_rows()
            targets = self._partition_targets(members, rows, include_degraded)
            if not targets:
                return {"error": "no healthy instances"}
            strat = strategy or self.scheduler.get_master_schedule_doc()["strategy"]
            assignments = self.scheduler.partition_schedule(targets=targets, strategy=strat)
            self._rebalance_seq += 1
            seq = self._rebalance_seq
            term = req.term
            plans = OrderedDict()
            by_key = OrderedDict()
            for m in members:
                by_key.setdefault(m.node_key, m)
            empty = []
            for key, m in by_key.items():
                part = assignments.get(key)
                if part is not None and part["entries"]:
                    gen = compute_gen(term, req.version, seq, key)
                    self._issued_gens.append(gen)
                    plans[m.uid] = ("load", part, gen)
                else:
                    if part is not None:
                        empty.append(key)
                    plans[m.uid] = ("stop", None, None)
            self._plan_epoch += 1
            version_at_plan = self._version
        finally:
            self._mutation_lock.release()
        contacted = [m for m in by_key.values()
                     if plans[m.uid][0] == "load" or (rows.get(m.node_key) or {}).get("reachable")]

        def worker(member, ctx):
            action, part, gen = plans[member.uid]
            return self._schedule_target(member, ctx, action, part, gen)
        try:
            res = self._run_fanout(req, contacted, worker, self._still_valid(req))
        except FanOutBusy as e:
            return dict(BUSY_LEGACY, retry_in_sec=e.reply.get("retry_in_sec", 5))
        superseded = False
        with self._mutation_lock:
            if self._version != version_at_plan:
                superseded = True
            else:
                new = OrderedDict()
                for key, m in by_key.items():
                    action, part, gen = plans[m.uid]
                    old = self._assignments.get(key) or {}
                    rec = self._desired_record(gen, "schedule" if action == "load" else "inactive",
                                               part["entries"] if part else [],
                                               old.get("last_frame_count"))
                    r = res.get(m.uid)
                    rec["last_result"] = r
                    if r is not None:
                        rec["state"] = self._state_after_load(r)
                    new[key] = rec
                self._assignments = new
                self._empty = empty
                self._partitioned_at = time.time()
                self._commit_version()
        results, by_node = {}, {}
        for m in contacted:
            results[m.legacy_key] = res[m.uid]
            by_node[m.node_key] = res[m.uid]
        skipped = [k for k in by_key if k not in targets]
        inactive = [k for k in by_key if plans[by_key[k].uid][0] == "stop"]
        out = {"status": "rebalanced", "instances": len(targets), "strategy": strat,
               "gen_seq": seq, "results": results, "by_node": by_node, "skipped": skipped,
               "empty": empty, "inactive": inactive, "deadline_ms": req.deadline_ms}
        if superseded:
            out["superseded"] = True
            out["replicated"] = {"stored": 0, "of": 0, "stragglers": []}
            return out
        replicated, ok, durable = self._replicate()
        out["replicated"] = replicated
        if not ok:
            return {"error": "not_replicated", "replicated": replicated, "version": self._version}
        if self.election is not None and not self.settings.require_quorum:
            out["durable"] = durable
        return out

    @staticmethod
    def _state_after_load(r):
        if r.get("ok") and r.get("applied") is not False and r.get("verified") is not False:
            return "applied" if (r.get("verified") or r.get("applied")) else "pending"
        return "pending"

    def _schedule_target(self, member, ctx, action, part, gen):
        """Load (or stop) one target's partition; returns S (D.2 REBALANCE row)."""
        session = _HwcSession(self, member, ctx)
        entries = len(part["entries"]) if part else 0
        try:
            r = session.open()
            if r is not None:
                return self._s_from(r, entries, gen, None, None, ctx)
            if action == "stop":
                r = session.command("SCHS", b"", mutating=True)
                return self._s_from(r, 0, None, None, None, ctx)
            extended = session.extended and "schg" in session.caps
            text, gen_used = self.scheduler.target_document(part, member.node_key,
                                                            member.instance_id, extended, gen)
            r, transfer = self._load_document(session, text, entries)
            path = r.pop("_path", None)
            verified = None
            if r.get("ok") and r.get("applied") is not False:
                verified = self._verify_load(session, extended, gen_used, entries)
            if path is not None and verified:
                self.scheduler.remove_temp_schedule(path)
            return self._s_from(r, entries, gen_used, verified, transfer, ctx)
        finally:
            session.close()

    def _verify_load(self, session, extended, gen, entries):
        if extended:
            q = session.command("SCHG", b"", mutating=False, next_phase="verify")
            data = q.get("data") if q.get("ok") else None
            return None if not isinstance(data, dict) else data.get("gen") == gen
        q = session.command("SCHQ", b"", mutating=False, next_phase="verify")
        data = q.get("data") if q.get("ok") else None
        if not isinstance(data, dict):
            return None
        return bool(data.get("active")) and data.get("total") == entries

    @staticmethod
    def _s_from(r, entries, gen, verified, transfer, ctx):
        s = {"ok": bool(r.get("ok")), "applied": r.get("applied", False if not r.get("ok")
                                                       else None),
             "entries": entries, "gen": gen, "verified": verified, "transfer": transfer,
             "reason": r.get("reason") or r.get("error") or ("ok" if r.get("applied") else None),
             "phase": r.get("phase", ctx.phase)}
        for key in ("legacy_ack", "error", "error_kind", "bytes", "elapsed_ms", "retry_in_sec"):
            if key in r:
                s[key] = r[key]
        return s

    def _verb_assignments(self, req, addr):
        with self._mutation_lock:
            return self._assignments_reply()

    def _assignments_reply(self):
        recs = OrderedDict()
        for key, rec in self._assignments.items():
            recs[key] = {"gen": rec.get("gen"), "entries": rec.get("partition", []),
                         "desired": rec.get("desired"),
                         "state": rec.get("state", "unknown"),
                         "attempts": rec.get("attempts", 0),
                         "last_result": rec.get("last_result")}
        strategy = None
        if self.scheduler is not None and self.scheduler.has_master_schedule():
            strategy = self.scheduler.get_master_schedule_doc()["strategy"]
        out = {"ok": True, "strategy": strategy, "gen_seq": self._rebalance_seq,
               "assignments": recs, "empty": list(self._empty),
               "partitioned_at": self._partitioned_at}
        if self._role_term()[0] == "follower":
            out["from"] = "follower"
        return out

    # ------------------------------------------------------------------
    # reconciliation (D.5.3)
    # ------------------------------------------------------------------

    def _reconcile_loop(self):
        while not self._stop_event.is_set():
            interval = self.settings.reconcile_interval_sec or self.settings.health_poll_interval
            self._reconcile_kick.wait(interval)
            self._reconcile_kick.clear()
            if self._stop_event.is_set():
                break
            try:
                self.reconcile_round()
            except Exception:
                self.logger.exception("reconciliation round failed")

    def reconcile_round(self):
        """One reconciliation round; returns {node_key: action} for the members it touched.

        Plan and commit are zero-wait snapshots under the schedule-operation and
        mutation locks; the SCHG queries and re-sends run outside both, so an
        operator's REBALANCE / SCHEDULE CLEAR is never made busy by a slow
        member (C.4.6, D.5.3). A version bump while the round runs (an operator
        mutation) or a REBALANCE plan (``_plan_epoch``) invalidates it: no further
        frame is sent (still_valid), its results are discarded at commit, and
        its schedule_overridden / schedule_reconcile_failed events are emitted
        only when it commits.
        """
        role, term = self._role_term()
        if role == "follower":
            return {}
        if self.scheduler is not None:
            self.scheduler.cleanup_temp_files()
        if not self._reconcile_lock.acquire(blocking=False):
            return {}
        try:
            if not self._schedule_op.acquire(blocking=False):
                return {}
            try:
                if not self._mutation_lock.acquire(blocking=False):
                    return {}
                try:
                    stamp = (self._version, self._plan_epoch)
                    desired = {k: dict(v) for k, v in self._assignments.items()}
                    members = {m.node_key: m for m in self._members()}
                    rows = self._health_rows()
                    snaps = self._poller.get_member_snapshots() if self._poller else {}
                    issued = set(self._issued_gens)
                finally:
                    self._mutation_lock.release()
            finally:
                self._schedule_op.release()
            now = time.monotonic()
            todo = []
            for key, rec in desired.items():
                m = members.get(key)
                if m is None:
                    continue
                if self._member_state(m, rows) not in ("up", "degraded"):
                    continue
                if rec["state"] == "failed":
                    continue
                if rec["state"] == "overridden" and not self.settings.reconcile_override:
                    continue
                if rec.get("next_try_mono", 0.0) > now:
                    continue
                todo.append((m, rec))
            req = _Req("RECONCILE", self._deadline_schedule(), False)
            req.role, req.term = role, term
            leader_ok = self._still_valid(req)

            def unchanged():
                return (self._version, self._plan_epoch) == stamp

            def still_valid():
                return leader_ok() and unchanged()
            actions = {}
            updates = {}
            events = []
            if todo:
                def worker(member, ctx):
                    rec = next(r for mm, r in todo if mm is member)
                    fc = (snaps.get(member.node_key) or (None, None))[0]
                    fc = fc.get("frame_count") if isinstance(fc, dict) else None
                    return self._reconcile_member(member, rec, ctx, issued, fc)
                try:
                    res = self._run_fanout(req, [m for m, _ in todo], worker, still_valid)
                except FanOutBusy:
                    return {}
                for m, rec in todo:
                    action, upd = res[m.uid].get("action"), res[m.uid].get("update")
                    if action is None:
                        action, upd = "skip", None
                    actions[m.node_key] = action
                    if upd:
                        updates[m.node_key] = upd
                    events.extend(res[m.uid].get("events") or ())
            with self._mutation_lock:
                committed = unchanged()
                if committed:
                    for key, upd in updates.items():
                        if key in self._assignments:
                            self._assignments[key].update(upd)
                    if updates:
                        self._persist()
            # events describe this round's verdicts; a superseded round's
            # verdicts (e.g. a REBALANCE's fresh gen read as an override) are void
            if committed:
                for name, severity, data in events:
                    self._emit(name, severity, data)
            if role == "leader":
                self._first_reconcile_done = True
            return actions
        finally:
            self._reconcile_lock.release()

    def _reconcile_member(self, m, rec, ctx, issued, frame_count):
        """Decide and (maybe) re-send for one member; returns {"action", "update"}.

        A member restart is detected the same way for every dialect: its STATUS
        frame_count went down since the last round. A schedule that is absent
        (gen null) is re-sent after a restart, or while this coordinator's own
        load was never confirmed (state not ``applied``); absent from a member
        that did not restart after a confirmed load, it was stopped by an
        operator (SCHS) and becomes ``overridden`` like any other override.
        """
        cache = self._ack_cache_get(m.node_key)
        last = rec.get("last_frame_count")
        restarted = last is not None and frame_count is not None and frame_count < last
        update = {}
        skip_update = {}
        if frame_count is not None:
            update["last_frame_count"] = frame_count
            if not restarted:
                # a restart seen in a round that could not act stays detectable
                skip_update["last_frame_count"] = frame_count
        skip = {"action": "skip", "update": skip_update}
        if cache is not None and cache.get("mode") == "legacy":
            if not restarted:
                return {"action": "skip", "update": update}
            return self._restart_resend(m, rec, ctx, update)
        session = _HwcSession(self, m, ctx)
        try:
            r = session.open()
            if r is not None:
                return skip
            if not (session.extended and "schg" in session.caps):
                if not restarted:
                    return {"action": "skip", "update": update}
                return self._restart_resend(m, rec, ctx, update, session)
            q = session.command("SCHG", b"", mutating=False, next_phase="verify")
            data = q.get("data") if q.get("ok") else None
            if not isinstance(data, dict):
                return skip
            gen = data.get("gen")
            active = bool(data.get("active"))
            override = bool(self.settings.reconcile_override)
            if rec["desired"] == "schedule":
                if gen is not None and gen == rec.get("gen"):
                    if rec["state"] != "applied":
                        update["state"] = "applied"
                    return {"action": "ok", "update": update}
                if gen is None and restarted:
                    return self._restart_resend(m, rec, ctx, update, session)
                if gen is None and rec["state"] != "applied":
                    return self._resend(m, rec, ctx, update, "missing", session)
                resend = gen in issued
            else:
                if gen is None and not active:
                    if rec["state"] != "applied":
                        update["state"] = "applied"
                    return {"action": "ok", "update": update}
                resend = gen in issued
            if resend or override:
                return self._resend(m, rec, ctx, update, "stale_gen", session)
            out = {"action": "overridden", "update": update}
            if rec["state"] != "overridden":
                out["events"] = [("schedule_overridden", "warning",
                                  {"node_key": m.node_key, "gen_seen": gen})]
            update["state"] = "overridden"
            return out
        finally:
            session.close()

    def _restart_resend(self, m, rec, ctx, update, session=None):
        """Re-send after a restart; a failed attempt keeps the restart detectable
        (the pre-restart frame_count is kept, so the next round retries)."""
        out = self._resend(m, rec, ctx, update, "restart", session)
        if not out["action"].startswith("resent:"):
            out["update"].pop("last_frame_count", None)
        return out

    def _resend(self, m, rec, ctx, update, why, session=None):
        own = session is None
        if own:
            session = _HwcSession(self, m, ctx)
            r = session.open()
            if r is not None:
                session.close()
                return {"action": "skip", "update": update}
        try:
            attempts = rec.get("attempts", 0) + 1
            if rec["desired"] == "inactive":
                r = session.command("SCHS", b"", mutating=True)
                s_res = self._s_from(r, 0, None, None, None, ctx)
            else:
                extended = session.extended and "schg" in session.caps
                part = {"entries": rec.get("partition", [])}
                text, gen_used = self.scheduler.target_document(part, m.node_key, m.instance_id,
                                                                extended, rec.get("gen"))
                r, transfer = self._load_document(session, text, len(part["entries"]))
                path = r.pop("_path", None)
                verified = None
                if r.get("ok") and r.get("applied") is not False:
                    verified = self._verify_load(session, extended, gen_used,
                                                 len(part["entries"]))
                if path is not None and verified:
                    self.scheduler.remove_temp_schedule(path)
                s_res = self._s_from(r, len(part["entries"]), gen_used, verified, transfer, ctx)
        finally:
            if own:
                session.close()
        good = s_res["ok"] and s_res["applied"] is not False and s_res.get("verified") is not False
        update.update({"attempts": attempts, "last_result": s_res})
        if good:
            update.update({"state": "applied", "next_try_mono": 0.0})
            return {"action": "resent:" + why, "update": update}
        out = {"action": "resend_failed:" + why, "update": update}
        max_attempts = int(self.settings.reconcile_max_attempts)
        if attempts >= max_attempts:
            update["state"] = "failed"
            out["events"] = [("schedule_reconcile_failed", "error",
                              {"node_key": m.node_key, "attempts": attempts,
                               "last_result": s_res.get("reason")})]
        else:
            backoff = RECONCILE_BACKOFF_SEC[min(attempts - 1, len(RECONCILE_BACKOFF_SEC) - 1)]
            update.update({"state": "pending", "next_try_mono": time.monotonic() + backoff})
        return out

    # ------------------------------------------------------------------
    # replication (C.4.7)
    # ------------------------------------------------------------------

    def _sync_doc(self):
        with self._mutation_lock:
            return self._sync_doc_locked()

    def _sync_doc_locked(self):
        doc = self.scheduler.get_master_schedule_doc() if self.scheduler is not None else None
        with self._membership_lock:
            added = [m.inst.get("entry") or "{}:{}{}".format(
                m.host, m.instance_id, "@" + m.node_id if m.node_id else "")
                for m in self._runtime_added]
            removed = sorted(self._runtime_removed)
        assignments = OrderedDict()
        for key, rec in self._assignments.items():
            assignments[key] = {"gen": rec.get("gen"), "entries": rec.get("entries", 0),
                                "state": rec.get("state"), "last_result": rec.get("last_result"),
                                "desired": rec.get("desired"),
                                "partition": rec.get("partition", []),
                                "attempts": rec.get("attempts", 0),
                                "last_frame_count": rec.get("last_frame_count")}
        return {"term": self._sync_term,
                "version": self._version, "schedule": doc, "set_at": self._set_at,
                "members_runtime": {"added": added, "removed": removed},
                "rebalance_seq": self._rebalance_seq, "assignments": assignments,
                "issued_gens": list(self._issued_gens), "empty": list(self._empty),
                "partitioned_at": self._partitioned_at}

    def _apply_sync_doc(self, doc):
        """Install a replicated state document (followers, pull repair, load at start).

        Every field is parsed and validated into locals first; coordinator
        state changes only once the whole document is known good, so a bad
        document (ValueError) leaves the held (term, version) untouched.
        """
        if not isinstance(doc, dict):
            raise ValueError("sync document must be a JSON object")
        try:
            version = int(doc.get("version", 0))
            term = int(doc.get("term", 0))
            rebalance_seq = int(doc.get("rebalance_seq", 0))
            gens = doc.get("issued_gens", [])
            if not isinstance(gens, list):
                raise ValueError("issued_gens must be a list")
            issued = deque((int(g) for g in gens), maxlen=4096)
            empty = doc.get("empty", [])
            if not isinstance(empty, list):
                raise ValueError("empty must be a list")
            sched_doc = doc.get("schedule")
            if sched_doc:
                canon = validate_schedule_doc(sched_doc)
                strat = canon.get("strategy") or "round_robin"
                if strat not in STRATEGIES:
                    raise ValueError("unknown strategy '{}'".format(strat))
            assignments = doc.get("assignments") or {}
            if not isinstance(assignments, dict):
                raise ValueError("assignments must be an object")
            recs = OrderedDict()
            for key, a in assignments.items():
                if not isinstance(a, dict):
                    raise ValueError("assignment '{}' must be an object".format(key))
                partition = a.get("partition", [])
                if not isinstance(partition, list) or \
                        not all(isinstance(e, dict) for e in partition):
                    raise ValueError("assignment '{}' partition must be a list of "
                                     "objects".format(key))
                rec = self._desired_record(a.get("gen"), a.get("desired", "schedule"),
                                           partition, a.get("last_frame_count"))
                rec["state"] = a.get("state", "unknown")
                rec["attempts"] = a.get("attempts", 0)
                rec["last_result"] = a.get("last_result")
                recs[str(key)] = rec
            runtime = doc.get("members_runtime") or {}
            if not isinstance(runtime, dict):
                raise ValueError("members_runtime must be an object")
            added_entries = runtime.get("added", [])
            removed = runtime.get("removed", [])
            if not isinstance(added_entries, list) or not isinstance(removed, list):
                raise ValueError("members_runtime added/removed must be lists")
            removed = set(str(k) for k in removed)
        except (TypeError, AttributeError, KeyError) as e:
            raise ValueError("bad sync document: {}".format(e))
        added = []
        for entry in added_entries:
            try:
                added.append(Member(parse_member(entry, default_stride=self.settings.port_stride,
                                                 allow_loopback=not self.cluster_mode),
                                    "runtime"))
            except (TypeError, ValueError, AttributeError):
                self.logger.warning("ignoring bad replicated member '%s'", entry)
        # -- commit (nothing below raises for a validated document) --
        if sched_doc:
            self._need_scheduler().set_master_schedule_doc(sched_doc)
        elif self.scheduler is not None:
            self.scheduler.clear_master_schedule()
        self._version = version
        self._sync_term = term
        self._set_at = doc.get("set_at")
        self._rebalance_seq = rebalance_seq
        self._issued_gens = issued
        self._empty = list(empty)
        self._partitioned_at = doc.get("partitioned_at")
        self._assignments = recs
        with self._membership_lock:
            self._runtime_added = added
            self._runtime_removed = removed
        self._membership_changed()

    def _replicate(self):
        """Push SCHEDULE SYNC to every follower; returns (replicated, ok, durable)."""
        if self.election is None:
            return {"stored": 1, "of": 1}, True, True
        s = self.settings
        doc = self._sync_doc()
        # the sender's endpoint lets a follower refuse a SYNC from a non-candidate
        body = dict(doc, leader=self.election.self_endpoint)
        followers = [ep for ep in self.election.candidates if ep != self.election.self_endpoint]
        n = len(self.election.candidates)
        stored = 1
        stragglers = []
        if followers:
            def push(ep):
                if self._push_hook is not None and self._push_hook(ep):
                    return None
                host, _, port = ep.rpartition(":")
                try:
                    return request_json(host, int(port), "SCHEDULE SYNC", body=body,
                                        auth_token=s.auth_token or None,
                                        connect_timeout=min(2.0, s.sync_push_timeout_sec),
                                        timeout=s.sync_push_timeout_sec)
                except (OSError, ValueError):
                    return None
            pool = ThreadPoolExecutor(max_workers=len(followers), thread_name_prefix="sync-push")
            try:
                futures = {pool.submit(push, ep): ep for ep in followers}
                done, _ = wait(futures, timeout=s.sync_push_timeout_sec + 0.5)
                for fut, ep in futures.items():
                    r = fut.result() if fut in done else None
                    # a follower holds this version when it stored it now, or
                    # already held exactly this (term, version) from an earlier push
                    if isinstance(r, dict) and r.get("ok") and (
                            r.get("stored") is True or (r.get("term"), r.get("version"))
                            == (doc["term"], doc["version"])):
                        stored += 1
                    else:
                        stragglers.append(ep)
            finally:
                pool.shutdown(wait=False)
        replicated = {"stored": stored, "of": n, "stragglers": stragglers}
        ok = (stored > n / 2.0) if s.require_quorum else True
        return replicated, ok, stored == n

    def _schedule_sync(self, raw):
        if self.election is None:
            return {"ok": False, "error": "bad_request",
                    "message": "SCHEDULE SYNC is accepted by elected followers only"}
        try:
            doc = raw.body_json()
        except RequestError as e:
            return e.reply
        if not isinstance(doc, dict) or "version" not in doc or "term" not in doc:
            return {"ok": False, "error": "bad_request", "message": "bad sync document"}
        try:
            term, version = int(doc["term"]), int(doc["version"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_request", "message": "bad term/version"}
        from federation_election import normalize_endpoint
        el = self.election
        try:
            sender = normalize_endpoint(str(doc.get("leader") or ""))
        except (TypeError, ValueError):
            sender = None
        if sender is None or sender not in el.candidates or sender == el.self_endpoint:
            return {"ok": False, "error": "not_candidate",
                    "message": "SCHEDULE SYNC must name a coordinator candidate as 'leader'"}
        role, own_term = self._role_term()
        if role == "leader" and term <= own_term:
            return {"ok": True, "stored": False, "term": self._sync_term,
                    "version": self._version}
        known = el.leader_info()
        if term == own_term and known is not None and known["term"] == own_term and \
                known["endpoint"] != sender:
            # this follower knows a different leader for this term
            return {"ok": False, "error": "not_leader", "leader": known}
        if term > own_term:
            el.observe_term(term, source="SCHEDULE SYNC of {}".format(sender))
            if self._role_term()[1] < term:
                return {"ok": False, "error": "bad_request",
                        "message": "term {} not accepted (current term {})".format(
                            term, self._role_term()[1])}
        stored = False
        with self._mutation_lock:
            if (term, version) > (self._sync_term, self._version):
                try:
                    self._apply_sync_doc(doc)
                except (TypeError, ValueError, KeyError, AttributeError) as e:
                    return {"ok": False, "error": "bad_request", "message": str(e)}
                self._persist()
                stored = True
        return {"ok": True, "stored": stored, "term": self._sync_term, "version": self._version}

    def _maybe_pull_repair(self, leader_ep, reply):
        """Pull the leader's state when the (term, version) it holds is newer.

        Compares like with like: the LEADER reply's ``schedule_term`` is the
        term of the leader's held version, not its election term (``term``,
        used only as a fallback for a reply without the key). A pull of the
        same advertised pair from the same leader is not repeated once it
        returned a document, so a pair that turns out not newer (e.g. a torn
        read on the leader) cannot cause a pull per election tick.
        """
        try:
            lv = int(reply.get("schedule_version", 0))
            lt = int(reply.get("schedule_term", reply.get("term", 0)))
        except (TypeError, ValueError):
            return
        advertised = (lt, lv)
        if (self._sync_term, self._version) >= advertised or self._repair_running:
            return
        if self._last_pull == (leader_ep, advertised):
            return
        if self._role_term()[0] == "leader":
            return
        self._repair_running = True
        self._spawn(self._pull_repair, "coord-repair", leader_ep, advertised)

    def _pull_repair(self, leader_ep, advertised):
        try:
            host, _, port = leader_ep.rpartition(":")
            reply = request_json(host, int(port), "SCHEDULE GET SYNC",
                                 auth_token=self.settings.auth_token or None,
                                 connect_timeout=self.settings.peer_connect_timeout_sec,
                                 timeout=self.settings.sync_push_timeout_sec)
            doc = reply.get("sync") if isinstance(reply, dict) else None
            if isinstance(doc, dict):
                self._last_pull = (leader_ep, advertised)
                with self._mutation_lock:
                    if (int(doc.get("term", 0)), int(doc.get("version", 0))) > \
                            (self._sync_term, self._version):
                        self._apply_sync_doc(doc)
                        self._persist()
                        self.logger.info("pull repair from %s: version %d (term %d)",
                                         leader_ep, self._version, self._sync_term)
        except (OSError, ValueError, TypeError) as e:
            self.logger.info("pull repair from %s failed: %s", leader_ep, e)
        finally:
            self._repair_running = False

    # ------------------------------------------------------------------
    # persistence (C.4.7)
    # ------------------------------------------------------------------

    def _persist(self):
        if self.persist_dir is None:
            return
        from federation_election import atomic_write_json
        doc = self._sync_doc()
        try:
            atomic_write_json(os.path.join(self.persist_dir, "master_schedule.json"),
                              {"term": doc["term"], "version": doc["version"],
                               "set_at": doc["set_at"], "schedule": doc["schedule"]})
            atomic_write_json(os.path.join(self.persist_dir, "members_runtime.json"),
                              doc["members_runtime"])
            atomic_write_json(os.path.join(self.persist_dir, "assignments.json"),
                              {k: doc[k] for k in ("rebalance_seq", "assignments", "issued_gens",
                                                   "empty", "partitioned_at")})
        except OSError as e:
            self.logger.error("cannot persist coordinator state under %s: %s",
                              self.persist_dir, e)

    def _load_state(self):
        doc = {}
        for name in ("master_schedule.json", "members_runtime.json", "assignments.json"):
            path = os.path.join(self.persist_dir, name)
            try:
                with open(path) as f:
                    part = json.load(f)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                self.logger.error("ignoring unreadable %s: %s", path, e)
                continue
            if name == "members_runtime.json":
                doc["members_runtime"] = part
            else:
                doc.update(part)
        if not doc:
            return
        doc.setdefault("term", 0)
        doc.setdefault("version", 0)
        try:
            self._apply_sync_doc(doc)
        except (TypeError, ValueError, KeyError, AttributeError) as e:
            self.logger.error("ignoring persisted coordinator state: %s", e)
            return
        self.logger.info("loaded persisted state: version %d, %s master schedule",
                         self._version, "a" if doc.get("schedule") else "no")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_instances(instance_str, default_stride=100):
    """Parse a legacy instance list like 'localhost:0,192.168.1.10:1' (--instances).

    Kept in order with duplicates, as today; loopback hosts are allowed.
    """
    out = []
    for m in parse_member_list(instance_str, allow_duplicates=True,
                               default_stride=default_stride, allow_loopback=True):
        m["source"] = "cli"
        out.append(m)
    return out


def _parse_peer_list(peer_str, default_stride=100):
    """peer_list -> member dicts (legacy status-port form, duplicates kept)."""
    out = []
    for m in parse_member_list(peer_str, allow_duplicates=True, default_stride=default_stride,
                               legacy_port_form=True):
        m["source"] = "config"
        out.append(m)
    return out


class _ArgParser(argparse.ArgumentParser):
    """argparse whose usage errors are ConfigError (exit 3, D.1), never exit 2
    (which a supervisor reads as a bind failure)."""

    def error(self, message):
        raise ConfigError("{}: {}".format(self.prog, message))


def build_from_args(argv=None):
    """(coordinator, args) from the command line (CLI flag > --config > default)."""
    parser = _ArgParser(description="HeIMDALL DAQ Federation Coordinator")
    parser.add_argument("--config", type=str, default="",
                        help="Path to federation config INI file")
    parser.add_argument("--port", type=int, default=None, help="Coordinator port (6000)")
    parser.add_argument("--listen-address", type=str, default=None,
                        help="Bind address (default [daq] listen_address or 0.0.0.0)")
    parser.add_argument("--instances", type=str, default=None,
                        help="Comma-separated list of host:instance_id pairs")
    parser.add_argument("--self", dest="self_ep", type=str, default=None,
                        help="This process's host:port among coordinator_candidates")
    parser.add_argument("--state-dir", type=str, default=None, help="Persistence directory")
    parser.add_argument("--auth-token", type=str, default=None, help="AUTH token for :6000")
    parser.add_argument("--pid-file", type=str, default=None)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)

    values = {}
    if args.config:
        cfg = ConfigParser()
        try:
            if not cfg.read(args.config):
                raise ConfigError("cannot read config file {}".format(args.config))
        except ConfigParserError as e:
            raise ConfigError("cannot parse {}: {}".format(args.config, e))
        values = CoordinatorSettings.ini_values(cfg)
    if args.port is not None:
        values["coordinator_port"] = args.port
    if args.listen_address is not None:
        values["listen_address"] = args.listen_address
    if args.self_ep is not None:
        values["coordinator_self"] = args.self_ep
    if args.state_dir is not None:
        values["state_dir"] = args.state_dir
    if args.auth_token is not None:
        values["auth_token"] = args.auth_token
    settings = CoordinatorSettings(**values)
    try:
        if args.instances is not None:
            instances = _parse_instances(args.instances, settings.port_stride)
        else:
            instances = _parse_peer_list(settings.peer_list, settings.port_stride)
    except ValueError as e:
        raise ConfigError(str(e))
    coordinator = FederationCoordinator(port=settings.coordinator_port, instances=instances,
                                        listen_address=settings.listen_address,
                                        settings=settings)
    if coordinator.scheduler is None:
        coordinator.scheduler = FederationScheduler(coordinator=coordinator)
    return coordinator, args


def main(argv=None):
    pre = _ArgParser(add_help=False)
    pre.add_argument("--log-file", default=None)
    pre.add_argument("--log-level", default="INFO")
    try:
        known, _ = pre.parse_known_args(argv)
    except ConfigError as e:
        print("configuration error: {}".format(e), file=sys.stderr)
        return 3
    handlers = None
    if known.log_file:
        try:
            handlers = [logging.FileHandler(known.log_file)]
        except OSError as e:
            print("cannot open log file {}: {}".format(known.log_file, e), file=sys.stderr)
            return 3
    level = getattr(logging, str(known.log_level).upper(), None)
    if not isinstance(level, int):
        print("bad --log-level {}".format(known.log_level), file=sys.stderr)
        return 3
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    log = logging.getLogger("federation_coordinator")
    try:
        coordinator, args = build_from_args(argv)
    except ConfigError as e:
        log.error("configuration error: %s", e)
        print("configuration error: {}".format(e), file=sys.stderr)
        return 3
    s = coordinator.settings
    log.info("effective settings: port=%d listen=%s members=%d candidates=%s self=%s "
             "state_dir=%s auth=%s cluster_id=%s", coordinator.port, coordinator.listen_address,
             len(coordinator._members()), s.coordinator_candidates or "-",
             coordinator.election.self_endpoint if coordinator.election else "-",
             coordinator.persist_dir or "-", "on" if s.auth_token else "off", s.cluster_id)
    try:
        coordinator.start()
    except OSError:
        return 2
    if args.pid_file:
        try:
            with open(args.pid_file, "w") as f:
                f.write("{}\n".format(os.getpid()))
        except OSError as e:
            log.error("cannot write pid file %s: %s", args.pid_file, e)

    # daq_stop.sh / federation_stop.sh send SIGTERM — run the same cleanup as Ctrl-C
    def _on_term_signal(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_term_signal)
    signal.signal(signal.SIGINT, _on_term_signal)
    try:
        while True:
            time.sleep(1)
    except SystemExit:
        pass
    finally:
        coordinator.close()
        if args.pid_file:
            try:
                os.unlink(args.pid_file)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
