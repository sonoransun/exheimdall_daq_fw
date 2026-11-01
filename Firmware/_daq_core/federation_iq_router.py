"""
    Federation IQ Router — Multi-Instance IQ Stream Aggregation

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Connects to several IQ sources and serves their frames as one unified
    raw ``[1024-byte header][payload]`` TCP stream to any number of clients.

    Sources (one reader thread each):

    * ``iqserver``: a DAQ instance's ``iq_server`` (``host:5000 + iid*stride``),
      ``streaming`` handshake + one ``IQDownload`` per frame (stop-and-wait).
    * ``router``: an upstream router's output port (``router://host:port``),
      raw frame stream with no handshake (chaining). Frames are demultiplexed
      into logical sources by header slot 107 (``SOURCE_KEY_CRC32``), else 108
      (``NODE_ID_CRC32``), and are never re-stamped.

    Output: every client has a bounded queue and its own sender thread with a
    progress timeout, so a slow or stalled client never delays a source reader
    or another client. Frames are tagged per ``tag_mode`` (``instance`` =
    rewrite the header ``unit_id`` with the source instance id, the historical
    behaviour; ``unit`` = untouched; ``node`` = slots 106/107), optionally
    stamped with a per-source sequence number (slot 103) and the router receive
    time (slots 104/105), and optionally aligned on the header time stamp.

    An optional line-protocol stats port (default 7001 from the CLI) answers
    PING, VERSION, STATS, SOURCES and CLIENTS with one JSON object + ``\\n``.

    CLI (all flags win over ``--config``)::

        python3 _daq_core/federation_iq_router.py [--config federation.ini]
            [--sources host:iid[@node_id]|router://host:port,...]
            [--output-port 7000] [--listen-address 0.0.0.0] [--stats-port 7001]
            [--queue-frames 4] [--drop-policy oldest|newest|disconnect]
            [--client-stall-limit 50] [--send-timeout-sec 2.0] [--max-clients 8]
            [--tag-mode instance|unit|node] [--stamp-seq] [--stamp-rx-ts]
            [--align-window-ms 0] [--align-max-frames 2] [--max-skew-ms 500]
            [--connect-on-demand | --always-connect] [--connect-timeout-sec 2.0]
            [--read-timeout-sec auto] [--first-frame-timeout-sec 0]
            [--reconnect-sec 3.0] [--upstream-stats host:port,...]
            [--auth-token T] [--allow-cidrs CIDR,...]
            [--pid-file PATH] [--log-file PATH] [--log-level INFO]

    Exit codes: 0 normal stop, 2 output or stats port bind failure,
    3 bad arguments/config.
"""
import argparse
import collections
import configparser
import ipaddress
import json
import logging
import os
import select
import signal
import socket
import statistics
import struct
import sys
import threading
import time
import zlib

from iq_header import IQHeader
import federation_common as fc
from version import FW_VERSION

IQ_HEADER_LENGTH = 1024
SYNC_WORD = 0x2bf7b95a

# Byte offset of the unit_id field in the IQ header (native alignment:
# sync_word[4] + frame_type[4] + hardware_id[16] = 24)
_UNIT_ID_OFFSET = 24
# Byte offset of reserved[0] (iq_header.h: slot k lives at 252 + 4*k)
_RESERVED_OFFSET = 252

TAG_MODES = ("instance", "unit", "node")
DROP_POLICIES = ("oldest", "newest", "disconnect")
SOURCE_STATES = ("idle", "connecting", "waiting_for_slot", "streaming", "backoff", "stale")
#: ``stale`` is reported only for a chained (router://) logical source whose
#: upstream connection is streaming but which has delivered no frame within
#: its own read timeout (E.5b liveness per logical source).
#: Eviction reasons counted in STATS "evicted".
EVICT_REASONS = ("slow_consumer", "partial_frame", "send_timeout", "queue_full")

DEFAULT_READ_TIMEOUT_SEC = 5.0
MAX_BACKOFF_SEC = 30.0
MIN_BACKOFF_SEC = 0.1
GAP_LOG_INTERVAL_SEC = 10.0
SKEW_LOG_INTERVAL_SEC = 60.0
CLOCK_OFFSET_WINDOW = 64
ALIGN_LAG_WINDOW = 1024
UPSTREAM_STATS_INTERVAL_SEC = 30.0
#: Socket wait slice: every blocking wait re-checks stop / idle conditions.
POLL_SLICE_SEC = 0.25
#: close() joins every thread within this total budget.
JOIN_BUDGET_SEC = 3.0
LISTEN_BACKLOG = 16
STATS_VERBS = ("PING", "VERSION", "STATS", "SOURCES", "CLIENTS")
STATS_MAX_HANDLERS = 16
STATS_PROTOCOL = 1
#: Largest payload a header may announce (32 ch x 2^23 samples x cf32).
MAX_PAYLOAD_BYTES = 32 * (1 << 23) * 8

EXIT_OK = 0
EXIT_BIND = 2
EXIT_CONFIG = 3

_U32 = 0xFFFFFFFF


def slot_offset(slot):
    """Byte offset of reserved slot *slot* inside the 1024-byte header."""
    return _RESERVED_OFFSET + 4 * int(slot)


def _put_u32(buf, offset, value):
    struct.pack_into("=I", buf, offset, int(value) & _U32)


def source_key_crc32(node_key):
    """Slot-107 value of a node key: ``zlib.crc32(node_key) & 0xFFFFFFFF``."""
    return zlib.crc32(str(node_key).encode("utf-8")) & _U32


def frame_payload_bytes(hdr):
    """Payload size announced by a decoded header (today's rule).

    ``cpi_length * active_ant_chs * 2 (I+Q) * sample_bit_depth / 8``;
    a depth outside 8/16/32/64 is read as 32 (cf32 decimated DATA).
    """
    bit_depth = hdr.sample_bit_depth
    if bit_depth not in (8, 16, 32, 64):
        bit_depth = 32
    return (hdr.active_ant_chs * hdr.cpi_length * 2 * bit_depth) // 8


def memory_bound_frames(queue_frames, max_clients, align_max_frames, sources,
                        align_sources=None):
    """Frames the router can hold at most (E.2): client queues, alignment
    FIFOs and one in-flight read buffer per source.

    *sources* is the number of source connections (one in-flight buffer
    each); *align_sources* the number of alignment FIFOs, i.e. logical
    sources, which exceeds *sources* when router:// upstreams carry several
    instances (default: *sources*).
    """
    fifos = int(sources) if align_sources is None else max(int(align_sources), int(sources))
    return (int(queue_frames) * int(max_clients) + int(align_max_frames) * fifos
            + int(sources))


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 1)


def _fmt_endpoint(host, port):
    return "[{}]:{}".format(host, port) if ":" in str(host) else "{}:{}".format(host, port)


def _peer_str(addr):
    try:
        return _fmt_endpoint(addr[0], addr[1])
    except (IndexError, TypeError):
        return str(addr)


def _ip_allowed(addr, networks):
    """True when *networks* is empty or the peer address lies in one of them."""
    if not networks:
        return True
    try:
        ip = ipaddress.ip_address(str(addr[0]).split("%", 1)[0])
    except (ValueError, IndexError, TypeError):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in networks)


def _set_keepalive(sock):
    """SO_KEEPALIVE + 10 s idle / 5 s interval / 3 probes where supported."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except OSError:
        pass


def _shutdown(sock):
    """Wake any thread blocked on *sock* (the owning thread closes it)."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _close(sock):
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _bind_listener(address, port):
    family = socket.AF_INET6 if ":" in str(address) else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
        sock.listen(LISTEN_BACKLOG)
        sock.settimeout(POLL_SLICE_SEC)
    except OSError:
        sock.close()
        raise
    return sock


# ---------------------------------------------------------------------------
# Frames and per-client output sinks (E.2)
# ---------------------------------------------------------------------------

class _Frame(object):
    """One frame ready to forward. ``header`` and ``payload`` are never
    mutated after construction, so every client queue shares them."""
    __slots__ = ("header", "payload", "nbytes", "ts", "source")

    def __init__(self, header, payload, ts, source):
        self.header = header
        self.payload = payload
        self.nbytes = len(header) + len(payload)
        self.ts = ts
        self.source = source


class ClientSink(object):
    """One output client: bounded queue + sender thread with a progress timeout.

    ``offer()`` is O(1) and never touches the socket. The sender writes on a
    non-blocking socket; the client is evicted when no byte is accepted for
    ``send_timeout_sec``, and always when a frame is left partially written.
    """

    def __init__(self, router, sock, peer, queue_frames, drop_policy, stall_limit,
                 send_timeout_sec):
        self.router = router
        self.sock = sock
        self.peer = peer
        self.queue_frames = int(queue_frames)
        self.drop_policy = drop_policy
        self.stall_limit = int(stall_limit)
        self.send_timeout_sec = float(send_timeout_sec)
        self.cond = threading.Condition()
        self.queue = collections.deque()
        self.sent_frames = 0
        self.sent_bytes = 0
        self.dropped_frames = 0
        self.overflows = 0
        self.consecutive_drops = 0
        self.connected_since = time.monotonic()
        self.last_error = None
        self.evict_reason = None
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="iq_client_{}".format(peer))

    # -- producer side (reader / aligner threads) --------------------------
    def offer(self, frame):
        with self.cond:
            if self.closed:
                return
            if len(self.queue) >= self.queue_frames:
                self.overflows += 1
                if self.drop_policy == "disconnect":
                    self._evict_locked("queue_full")
                    return
                if self.drop_policy == "oldest":
                    self.queue.popleft()
                    self.queue.append(frame)
                self.dropped_frames += 1
                self.consecutive_drops += 1
                if self.stall_limit > 0 and self.consecutive_drops >= self.stall_limit:
                    self._evict_locked("slow_consumer")
                    return
            else:
                self.queue.append(frame)
            self.cond.notify()

    def _evict_locked(self, reason):
        if not self.closed:
            self.closed = True
            self.evict_reason = reason
            self.dropped_frames += len(self.queue)
            self.queue.clear()
            self.cond.notify()

    def stop(self):
        """Router shutdown: stop the sender and wake any blocked socket call."""
        with self.cond:
            self.closed = True
            self.queue.clear()
            self.cond.notify()
        _shutdown(self.sock)

    def snapshot(self, now):
        with self.cond:
            return {"peer": self.peer,
                    "connected_sec": round(now - self.connected_since, 1),
                    "queued": len(self.queue),
                    "sent_frames": self.sent_frames,
                    "sent_bytes": self.sent_bytes,
                    "dropped_frames": self.dropped_frames,
                    "overflows": self.overflows,
                    "consecutive_drops": self.consecutive_drops,
                    "last_error": self.last_error}

    # -- sender thread -----------------------------------------------------
    def _run(self):
        try:
            while True:
                frame = None
                with self.cond:
                    if self.closed:
                        break
                    if self.queue:
                        frame = self.queue.popleft()
                    else:
                        self.cond.wait(POLL_SLICE_SEC)
                if frame is None:
                    if not self._peer_alive():
                        with self.cond:
                            self.closed = True
                        break
                    continue
                if not self._send_frame(frame):
                    break
                with self.cond:
                    self.sent_frames += 1
                    self.sent_bytes += frame.nbytes
                    self.consecutive_drops = 0
        finally:
            _close(self.sock)
            self.router._remove_sink(self)

    def _peer_alive(self):
        """Drain bytes the client sent; False on EOF or a socket error."""
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
            if not readable:
                return True
            data = self.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return True
        except (OSError, ValueError) as e:
            self.last_error = str(e)
            return False
        if not data:
            self.last_error = "peer closed"
            return False
        return True

    def _fail(self, reason, sent, error):
        """End the session after a send problem; partial frames always evict."""
        with self.cond:
            if error:
                self.last_error = error
            if self.closed:
                return False
            if sent > 0:
                self._evict_locked("partial_frame")
            elif reason is not None:
                self._evict_locked(reason)
            else:
                self.closed = True
        return False

    def _send_frame(self, frame):
        sent = 0
        last_progress = time.monotonic()
        for part in (frame.header, frame.payload):
            view = memoryview(part)
            off = 0
            while off < len(view):
                if self.closed:
                    return False
                try:
                    n = self.sock.send(view[off:])
                except (BlockingIOError, InterruptedError):
                    n = 0
                except (OSError, ValueError) as e:
                    return self._fail(None, sent, str(e))
                if n > 0:
                    off += n
                    sent += n
                    last_progress = time.monotonic()
                    continue
                remaining = self.send_timeout_sec - (time.monotonic() - last_progress)
                if remaining <= 0:
                    return self._fail("send_timeout", sent,
                                      "no progress for {:g} s".format(self.send_timeout_sec))
                try:
                    select.select([], [self.sock], [], min(remaining, POLL_SLICE_SEC))
                except (OSError, ValueError) as e:
                    return self._fail(None, sent, str(e))
        return True


# ---------------------------------------------------------------------------
# Sources: readers (one per configured source) and logical sources (stats)
# ---------------------------------------------------------------------------

class _SourceDone(Exception):
    """End of one source connection. outcome: 'failure', 'idle' or 'stop'."""

    def __init__(self, outcome, reason):
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


class _Reader(object):
    """Connection lifecycle of one configured source (E.5b)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.kind = cfg["kind"]
        self.host = cfg["host"]
        self.connect_host = cfg.get("connect_host") or cfg["host"]
        self.port = int(cfg["port"])
        self.endpoint = _fmt_endpoint(self.connect_host, self.port)
        self.via = cfg.get("via") or _fmt_endpoint(self.host, self.port)
        self.state = "connecting"
        self.state_since = time.monotonic()
        self.read_timeout_sec = None       # effective, set per connection
        self.backoff_sec = 0.0
        self.consecutive_failures = 0
        self.connections = 0
        self.reconnects = 0
        self.frames_received = 0
        self.bytes_received = 0
        self.last_error = None
        self.sock = None
        self.thread = None
        self.source = None                 # direct (iqserver) logical source
        self.logical = {}                  # router kind: crc (or None) -> _Source
        self.untagged_logged = False
        self.stats_endpoints = []          # router kind: "h:p" of --upstream-stats


class _Source(object):
    """Per logical source state: continuity, loss, skew and identity."""

    def __init__(self, reader, kind, key, node_key, instance_id, node_id=None, crc=None):
        self.reader = reader
        self.kind = kind
        self.key = key
        self.node_key = node_key
        self.instance_id = instance_id
        self.node_id = node_id
        self.crc = crc
        self.node_key_crc32 = source_key_crc32(node_key) if kind == "iqserver" else 0
        self.node_id_crc32 = 0
        self.frames_received = 0
        self.bytes_received = 0
        self.frames_lost = 0
        self.cpi_gaps = 0
        self.restarts = 0
        self.overruns = 0
        self.last_cpi_index = None
        #: False after a connect-on-demand idle period: the next forward step
        #: is a new gap baseline (iq_server skipped those frames for nobody),
        #: while a backwards step still counts as a restart.
        self.gap_baseline_valid = False
        self.last_overrun_cnt = None
        self.last_rx_mono = None
        #: router kind: no frame for this long -> "stale" (None = never)
        self.stale_after_sec = None
        self.last_frame_time = 0.0
        self.unit_id_seen = None
        self.offsets = collections.deque(maxlen=CLOCK_OFFSET_WINDOW)
        self.clock_offset_ms = None
        self.skew_warning = False
        self.last_gap_log = None
        self.last_skew_log = None
        self.fwd_seq = 0

    @property
    def tracks_continuity(self):
        """False for the untagged pseudo-source ``via:<h:p>``: it merges the
        interleaved cpi_index / overrun counters of every instance behind an
        untagging upstream, so continuity would be meaningless."""
        return not (self.kind == "router" and self.crc is None)

    def is_stale(self, now=None):
        """A chained logical source that stopped delivering frames while its
        upstream connection keeps streaming (always False for direct sources)."""
        if self.kind != "router" or self.stale_after_sec is None:
            return False                   # stale_after_sec is set with last_rx_mono
        now = time.monotonic() if now is None else now
        return now - self.last_rx_mono > self.stale_after_sec

    def is_streaming(self, now=None):
        return self.reader.state == "streaming" and not self.is_stale(now)

    def state(self, now=None):
        """Effective E.5b state: the reader's, or ``stale`` (logical source)."""
        if self.reader.state == "streaming" and self.is_stale(now):
            return "stale"
        return self.reader.state


# ---------------------------------------------------------------------------
# Bounded watermark alignment (E.5)
# ---------------------------------------------------------------------------

class _Aligner(object):
    """Per-source FIFOs released in time_stamp order (ties by STATS key)."""

    def __init__(self, router, window_ms, max_frames):
        self.router = router
        self.window_sec = float(window_ms) / 1000.0
        self.max_frames = int(max_frames)
        self.cond = threading.Condition()
        self.fifos = {}             # _Source -> deque[(frame, enqueue_mono)]
        self.max_seen = {}          # _Source -> highest time_stamp queued or released
        self.last_released_ts = None
        self.late_frames = 0
        self.forced_releases = 0
        self.lags_ms = collections.deque(maxlen=ALIGN_LAG_WINDOW)

    def push(self, src, frame):
        with self.cond:
            now = time.monotonic()
            prev = self.max_seen.get(src)
            self.max_seen[src] = frame.ts if prev is None else max(prev, frame.ts)
            if self.last_released_ts is not None and frame.ts < self.last_released_ts:
                self.late_frames += 1
                self.router._forward_frame(frame)
                return
            q = self.fifos.setdefault(src, collections.deque())
            if len(q) >= self.max_frames:
                # release what is already due (window expired, a source went
                # stale since the last slice) before forcing anything out
                self._release(now)
            if len(q) >= self.max_frames:
                self.forced_releases += 1
                self._release(now, forced_ts=q[0][0].ts)
            q.append((frame, now))
            self._release(now)
            self.cond.notify()

    def kick(self):
        with self.cond:
            self.cond.notify()

    def clear(self):
        with self.cond:
            self.fifos.clear()

    def held_frames(self):
        with self.cond:
            return sum(len(q) for q in self.fifos.values())

    def _release(self, now, forced_ts=None):
        streaming = [s for s in self.router._all_sources() if s.is_streaming(now)]
        if not streaming:
            threshold = float("inf")       # nothing left to wait for
        else:
            seen = [self.max_seen.get(s) for s in streaming]
            threshold = float("-inf") if any(v is None for v in seen) else min(seen)
        if forced_ts is not None:
            threshold = max(threshold, forced_ts)
        for q in self.fifos.values():
            for frame, enq in q:
                if now - enq < self.window_sec:
                    break
                threshold = max(threshold, frame.ts)
        while True:
            best = None
            for src, q in self.fifos.items():
                if q and q[0][0].ts <= threshold:
                    cand = (q[0][0].ts, src.key)
                    if best is None or cand < best[0]:
                        best = (cand, q)
            if best is None:
                return
            frame, enq = best[1].popleft()
            self.lags_ms.append((now - enq) * 1000.0)
            if self.last_released_ts is None or frame.ts > self.last_released_ts:
                self.last_released_ts = frame.ts
            self.router._forward_frame(frame)

    def run(self, stop_event):
        while not stop_event.is_set():
            with self.cond:
                now = time.monotonic()
                self._release(now)
                heads = [q[0][1] for q in self.fifos.values() if q]
                if heads:
                    timeout = min(max(min(heads) + self.window_sec - now, 0.005),
                                  POLL_SLICE_SEC)
                else:
                    timeout = POLL_SLICE_SEC
                self.cond.wait(timeout)

    def stats(self):
        with self.cond:
            lags = sorted(self.lags_ms)
            return {"late_frames": self.late_frames,
                    "forced_releases": self.forced_releases,
                    "held_frames": sum(len(q) for q in self.fifos.values()),
                    "alignment_lag_ms": {"p50": _percentile(lags, 0.50),
                                         "p95": _percentile(lags, 0.95)}}


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

class FederationIQRouter:
    """Aggregates IQ streams from multiple DAQ instances (and upstream routers)."""

    def __init__(self, instance_configs, output_port=7000, listen_address="0.0.0.0", *,
                 queue_frames=4, drop_policy="oldest", client_stall_limit=50,
                 send_timeout_sec=2.0, max_clients=8, tag_mode="instance",
                 stamp_seq=False, stamp_rx_ts=False, align_window_ms=0,
                 align_max_frames=2, max_skew_ms=500, connect_on_demand=False,
                 connect_timeout_sec=2.0, read_timeout_sec="auto",
                 first_frame_timeout_sec=0.0, reconnect_sec=3.0, stats_port=0,
                 upstream_stats=None, upstream_stats_interval_sec=UPSTREAM_STATS_INTERVAL_SEC,
                 auth_token=None, allow_cidrs=None, frame_bytes_hint=0):
        """
        Parameters
        ----------
        instance_configs : list of dict
            Legacy form ``{"host": str, "instance_id": int, "iq_port": int}``,
            or the dicts of ``federation_common.parse_router_source`` /
            ``self_member``-derived sources: ``{"kind": "iqserver", "host",
            "connect_host", "instance_id", "node_key", "node_id", "port"}`` or
            ``{"kind": "router", "host", "connect_host", "port", "via"}``.
        output_port : int
            TCP port of the unified output stream (0 = ephemeral; the bound
            port is stored back after start()).
        listen_address : str
            Local address the output and stats servers bind to.
        Keyword-only parameters are the CLI settings of the same name
        (spec E.1); the defaults keep the historical class behaviour
        (``tag_mode="instance"``, ``connect_on_demand=False``, no stats port).
        """
        if tag_mode not in TAG_MODES:
            raise ValueError("bad tag_mode '{}' (expected instance, unit or node)".format(
                tag_mode))
        if drop_policy not in DROP_POLICIES:
            raise ValueError("bad drop_policy '{}' (expected oldest, newest or "
                             "disconnect)".format(drop_policy))
        if read_timeout_sec != "auto" and float(read_timeout_sec) < 0:
            raise ValueError("read_timeout_sec must be 'auto' or >= 0")
        if float(send_timeout_sec) <= 0:
            raise ValueError("send_timeout_sec must be > 0")
        self.logger = logging.getLogger(__name__)
        self.instance_configs = instance_configs
        self.output_port = int(output_port)
        self.listen_address = listen_address
        self.queue_frames = int(queue_frames)
        self.drop_policy = drop_policy
        self.client_stall_limit = int(client_stall_limit)
        self.send_timeout_sec = float(send_timeout_sec)
        self.max_clients = int(max_clients)
        self.tag_mode = tag_mode
        self.stamp_seq = bool(stamp_seq)
        self.stamp_rx_ts = bool(stamp_rx_ts)
        self.align_window_ms = int(align_window_ms)
        self.align_max_frames = int(align_max_frames)
        self.max_skew_ms = int(max_skew_ms)
        self.connect_on_demand = bool(connect_on_demand)
        self.connect_timeout_sec = float(connect_timeout_sec)
        self.read_timeout_sec = read_timeout_sec if read_timeout_sec == "auto" \
            else float(read_timeout_sec)
        self.first_frame_timeout_sec = float(first_frame_timeout_sec)
        self.reconnect_sec = float(reconnect_sec)
        self.stats_port = int(stats_port or 0)
        self.upstream_stats_interval_sec = float(upstream_stats_interval_sec)
        self.auth_token = auth_token or None
        self.allow_cidrs = [ipaddress.ip_network(c, strict=False) if isinstance(c, str) else c
                            for c in (allow_cidrs or [])]
        self.frame_bytes_hint = int(frame_bytes_hint or 0)

        self._stop_event = threading.Event()
        self._output_lock = threading.Condition()
        self._sinks = []
        self._stats_lock = threading.Lock()
        self._threads = []
        self._out_sock = None
        self._stats_sock = None
        self._stats_sem = threading.Semaphore(STATS_MAX_HANDLERS)
        self._start_mono = time.monotonic()
        self._evicted = {r: 0 for r in EVICT_REASONS}
        self._rejected_clients = 0
        self._unattributed_frames = 0
        self._max_frame_bytes = 0
        self._upstream_maps = {}           # "h:p" -> {crc: node_key}
        self._upstream_poll_failed = set()
        self._aligner = _Aligner(self, self.align_window_ms, self.align_max_frames) \
            if self.align_window_ms > 0 else None

        self._readers = []
        seen = set()
        for cfg in instance_configs:
            reader = _Reader(self._normalize_source(cfg))
            if reader.kind == "iqserver":
                key = reader.cfg["node_key"]
                if key in seen:
                    raise ValueError("duplicate router source '{}'".format(key))
                seen.add(key)
                reader.source = _Source(reader, "iqserver", key, key,
                                        reader.cfg["instance_id"], reader.cfg.get("node_id"))
            else:
                key = "router://{}".format(reader.via.lower())
                if key in seen:
                    raise ValueError("duplicate router source '{}'".format(key))
                seen.add(key)
            self._readers.append(reader)
        self._pair_upstream_stats(upstream_stats or [])

    # -- configuration helpers ---------------------------------------------
    @staticmethod
    def _normalize_source(cfg):
        kind = cfg.get("kind", "iqserver")
        if kind not in ("iqserver", "router"):
            raise ValueError("bad source kind '{}'".format(kind))
        out = dict(cfg)
        out["kind"] = kind
        out.setdefault("connect_host", cfg["host"])
        if "port" not in out:
            out["port"] = cfg["iq_port"]
        if kind == "iqserver":
            out["instance_id"] = int(cfg["instance_id"])
            if not out.get("node_key"):
                out["node_key"] = fc.node_key(cfg["host"], out["instance_id"])
        else:
            out.setdefault("via", _fmt_endpoint(cfg["host"], int(out["port"])))
        return out

    def _pair_upstream_stats(self, endpoints):
        """An ``--upstream-stats`` endpoint serves the router:// sources on the
        same host; an endpoint matching no source host serves all of them."""
        routers = [r for r in self._readers if r.kind == "router"]
        for host, port in endpoints:
            ep = _fmt_endpoint(host, int(port))
            self._upstream_maps[ep] = {}
            matched = [r for r in routers if r.host.lower() == str(host).lower()]
            for r in (matched or routers):
                r.stats_endpoints.append(ep)
        self._upstream_endpoints = [(h, int(p)) for h, p in endpoints]

    def memory_bound(self):
        """(frames, bytes) of the E.2 memory bound; bytes use the largest of
        the configured frame size hint and the largest frame seen so far."""
        with self._stats_lock:
            logical = len(self._all_sources_locked())
        frames = memory_bound_frames(self.queue_frames, self.max_clients,
                                     self.align_max_frames, len(self._readers), logical)
        return frames, frames * max(self.frame_bytes_hint, self._max_frame_bytes)

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        """Bind the output (and stats) port, then start every thread.

        Raises OSError when a port cannot be bound (nothing is left running).
        """
        self._out_sock = _bind_listener(self.listen_address, self.output_port)
        self.output_port = self._out_sock.getsockname()[1]
        if self.stats_port:
            try:
                self._stats_sock = _bind_listener(self.listen_address, self.stats_port)
            except OSError:
                _close(self._out_sock)
                self._out_sock = None
                raise
            self.stats_port = self._stats_sock.getsockname()[1]
        self._start_mono = time.monotonic()

        self._spawn(self._output_server, "iq_router_server")
        if self._stats_sock is not None:
            self._spawn(self._stats_server, "iq_router_stats")
        if self._aligner is not None:
            self._spawn(self._aligner.run, "iq_router_align", self._stop_event)
        if self._upstream_endpoints:
            self._spawn(self._upstream_poller, "iq_router_upstream")
        for reader in self._readers:
            reader.thread = self._spawn(self._reader_loop, "iq_reader_{}".format(
                reader.source.key if reader.source else reader.via), reader)

        frames, nbytes = self.memory_bound()
        if nbytes:
            bound = "{} frames x {} bytes = {} bytes".format(
                frames, max(self.frame_bytes_hint, self._max_frame_bytes), nbytes)
        else:
            bound = "{} frames x frame size (learned from the first frame)".format(frames)
        self.logger.info("IQ Router started: %d sources, output on port %d, stats port %s, "
                         "tag_mode %s, memory bound %s", len(self._readers), self.output_port,
                         self.stats_port or "off", self.tag_mode, bound)

    def _spawn(self, target, name, *args):
        t = threading.Thread(target=target, args=args, daemon=True, name=name)
        t.start()
        self._threads.append(t)
        return t

    def close(self):
        """Stop all threads and close connections (returns within ~3 s)."""
        self._stop_event.set()
        _shutdown(self._out_sock)
        _shutdown(self._stats_sock)
        with self._output_lock:
            sinks = list(self._sinks)
            self._output_lock.notify_all()
        for sink in sinks:
            sink.stop()
        for reader in self._readers:
            _shutdown(reader.sock)
        if self._aligner is not None:
            self._aligner.kick()
        deadline = time.monotonic() + JOIN_BUDGET_SEC
        for t in self._threads + [s.thread for s in sinks]:
            if t.is_alive() and t is not threading.current_thread():
                t.join(timeout=max(0.0, deadline - time.monotonic()))
        _close(self._out_sock)
        _close(self._stats_sock)
        if self._aligner is not None:
            self._aligner.clear()
        self.logger.info("IQ Router stopped")

    # -- output server -------------------------------------------------------
    def _output_server(self):
        """Accept clients on the unified output port."""
        while not self._stop_event.is_set():
            try:
                client, addr = self._out_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            peer = _peer_str(addr)
            if not _ip_allowed(addr, self.allow_cidrs):
                _close(client)
                continue
            with self._output_lock:
                full = len(self._sinks) >= self.max_clients
            if full:
                with self._stats_lock:
                    self._rejected_clients += 1
                self.logger.warning("IQ Router: client %s refused (max_clients %d reached)",
                                    peer, self.max_clients)
                _close(client)
                continue
            try:
                client.setblocking(False)
            except OSError:
                _close(client)
                continue
            sink = ClientSink(self, client, peer, self.queue_frames, self.drop_policy,
                              self.client_stall_limit, self.send_timeout_sec)
            with self._output_lock:
                if self._stop_event.is_set():   # close() already took its sink snapshot
                    _close(client)
                    break
                self._sinks.append(sink)
                self._output_lock.notify_all()
            sink.thread.start()
            self.logger.info("IQ Router: client connected from %s", peer)

    def _remove_sink(self, sink):
        with self._output_lock:
            if sink in self._sinks:
                self._sinks.remove(sink)
            self._output_lock.notify_all()
        if self._stop_event.is_set():
            return
        if sink.evict_reason is not None:
            with self._stats_lock:
                self._evicted[sink.evict_reason] = self._evicted.get(sink.evict_reason, 0) + 1
            self.logger.warning("IQ Router: client %s evicted (%s)", sink.peer,
                                sink.evict_reason)
        else:
            self.logger.info("IQ Router: client %s disconnected (%s)", sink.peer,
                             sink.last_error or "closed")

    def _has_clients(self):
        return bool(self._sinks)

    def _forward_frame(self, frame):
        """Enqueue a frame on every client (O(1) each; no socket I/O here)."""
        with self._output_lock:
            sinks = list(self._sinks)
        for sink in sinks:
            sink.offer(frame)

    # -- source readers ------------------------------------------------------
    def _set_state(self, reader, state):
        with self._stats_lock:
            old = reader.state
            if old == state:
                return
            reader.state = state
            reader.state_since = time.monotonic()
        if self._aligner is not None and "streaming" in (old, state):
            self._aligner.kick()

    def _wait_for_client(self):
        with self._output_lock:
            while not self._sinks and not self._stop_event.is_set():
                self._output_lock.wait(POLL_SLICE_SEC * 2)

    def _reader_loop(self, reader):
        while not self._stop_event.is_set():
            if self.connect_on_demand and not self._has_clients():
                with self._stats_lock:
                    for src in self._reader_sources_locked(reader):
                        src.gap_baseline_valid = False
                self._set_state(reader, "idle")
                self._wait_for_client()
                continue
            self._set_state(reader, "connecting")
            try:
                sock = socket.create_connection((reader.connect_host, reader.port),
                                                timeout=self.connect_timeout_sec)
            except OSError as e:
                self._source_failed(reader, "connect to {} failed: {}".format(
                    reader.endpoint, e))
                continue
            _set_keepalive(sock)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            sock.settimeout(POLL_SLICE_SEC)
            with self._stats_lock:
                if reader.connections > 0:
                    reader.reconnects += 1
                reader.connections += 1
                reader.sock = sock
            if self._stop_event.is_set():       # close() ran before sock was published
                _close(sock)
                break
            self.logger.info("IQ Router: connected to %s source %s", reader.kind,
                             reader.endpoint)
            try:
                self._run_connection(reader, sock)      # returns only by raising
            except _SourceDone as e:
                outcome, reason = e.outcome, e.reason
            except OSError as e:
                outcome, reason = "failure", str(e)
            with self._stats_lock:
                reader.sock = None
            _close(sock)
            if outcome == "stop":
                break
            if outcome == "idle":
                self.logger.info("IQ Router: no output clients, closed source %s",
                                 reader.endpoint)
                continue
            self._source_failed(reader, reason)
        self._set_state(reader, "idle")

    def _source_failed(self, reader, reason):
        if self._stop_event.is_set():
            return
        with self._stats_lock:
            reader.consecutive_failures += 1
            base = max(self.reconnect_sec, MIN_BACKOFF_SEC)
            delay = min(base * (2 ** (reader.consecutive_failures - 1)),
                        max(MAX_BACKOFF_SEC, base))
            reader.backoff_sec = delay
            reader.last_error = reason
        self._set_state(reader, "backoff")
        self.logger.warning("IQ Router: source %s: %s (retry in %.1f s)", reader.endpoint,
                            reason, delay)
        self._stop_event.wait(delay)

    def _recv_into(self, reader, sock, view, timeout, what):
        """Fill *view*; *timeout* (s, None = none) bounds the time without progress."""
        got = 0
        last = time.monotonic()
        while got < len(view):
            if self._stop_event.is_set():
                raise _SourceDone("stop", "router stopping")
            if self.connect_on_demand and not self._has_clients():
                raise _SourceDone("idle", "no output clients")
            try:
                n = sock.recv_into(view[got:])
            except socket.timeout:
                if timeout is not None and time.monotonic() - last >= timeout:
                    raise _SourceDone("failure", "{} timeout ({:g} s without data)".format(
                        what, timeout))
                continue
            except InterruptedError:
                continue
            except OSError as e:
                raise _SourceDone("failure", "receive error: {}".format(e))
            if n == 0:
                raise _SourceDone("failure", "connection closed by source")
            got += n
            last = time.monotonic()

    def _connection_read_timeout(self, hdr):
        if self.read_timeout_sec != "auto":
            return self.read_timeout_sec or None
        if hdr is not None and hdr.cpi_length and hdr.sampling_freq:
            return max(DEFAULT_READ_TIMEOUT_SEC,
                       3.0 * float(hdr.cpi_length) / float(hdr.sampling_freq))
        return DEFAULT_READ_TIMEOUT_SEC

    def _router_read_timeout(self, reader, hdr):
        """Streaming read timeout of a router:// connection (stats lock held).

        One upstream connection multiplexes logical sources with different
        frame periods, so the auto timeout is the largest of this frame's and
        every known logical source's own stale threshold: a slow source that
        keeps streaming after a fast one stopped must not time out a healthy
        upstream connection.
        """
        timeout = self._connection_read_timeout(hdr)
        if self.read_timeout_sec != "auto":
            return timeout
        for src in reader.logical.values():
            if src.stale_after_sec is not None and src.stale_after_sec > timeout:
                timeout = src.stale_after_sec
        return timeout

    def _run_connection(self, reader, sock):
        hdr = IQHeader()
        first_timeout = self.first_frame_timeout_sec or None
        with self._stats_lock:
            if reader.kind == "router":
                reader.read_timeout_sec = self._router_read_timeout(reader, None)
            else:
                reader.read_timeout_sec = self._connection_read_timeout(None)
        if reader.kind == "iqserver":
            sock.sendall(b"streaming")
            self._set_state(reader, "waiting_for_slot")
        header = bytearray(IQ_HEADER_LENGTH)
        view = memoryview(header)
        first = True
        while True:
            if first and reader.kind == "router":
                self._recv_into(reader, sock, view[:1], first_timeout, "first byte")
                self._set_state(reader, "streaming")
                self._recv_into(reader, sock, view[1:], reader.read_timeout_sec, "read")
            elif first:
                self._recv_into(reader, sock, view, first_timeout, "first frame")
            else:
                self._recv_into(reader, sock, view, reader.read_timeout_sec, "read")
            try:
                hdr.decode_header(bytes(header))
            except (struct.error, UnicodeDecodeError):
                raise _SourceDone("failure", "undecodable header")
            if hdr.check_sync_word() != 0:
                raise _SourceDone("failure", "bad sync word")
            if reader.kind == "router":
                with self._stats_lock:
                    reader.read_timeout_sec = self._router_read_timeout(reader, hdr)
            elif first:
                with self._stats_lock:
                    reader.read_timeout_sec = self._connection_read_timeout(hdr)
                self._set_state(reader, "streaming")
            size = frame_payload_bytes(hdr)
            if size > MAX_PAYLOAD_BYTES:
                raise _SourceDone("failure", "header announces a {} byte payload".format(size))
            payload = bytearray(size)
            if size:
                self._recv_into(reader, sock, memoryview(payload), reader.read_timeout_sec,
                                "read")
            self._handle_frame(reader, header, hdr, payload)
            first = False
            with self._stats_lock:
                reader.consecutive_failures = 0
                reader.backoff_sec = 0.0
            if self.connect_on_demand and not self._has_clients():
                raise _SourceDone("idle", "no output clients")
            if reader.kind == "iqserver":
                sock.sendall(b"IQDownload")

    # -- per-frame processing ------------------------------------------------
    def _logical_source(self, reader, hdr):
        """The logical source of a frame read from an upstream router (E.7)."""
        crc = hdr.reserved[IQHeader.RSV_SOURCE_KEY_CRC32] or \
            hdr.reserved[IQHeader.RSV_NODE_ID_CRC32] or None
        src = reader.logical.get(crc)
        if src is None:
            src = _Source(reader, "router", None, None, None, crc=crc)
            reader.logical[crc] = src
            self._refresh_keys_locked()
        return src

    def _handle_frame(self, reader, header, hdr, payload):
        rx_mono = time.monotonic()
        rx_wall_ms = int(time.time() * 1000)
        nbytes = IQ_HEADER_LENGTH + len(payload)
        log_untagged = False
        with self._stats_lock:
            reader.frames_received += 1
            reader.bytes_received += nbytes
            if nbytes > self._max_frame_bytes:
                self._max_frame_bytes = nbytes
            if reader.kind == "iqserver":
                src = reader.source
            else:
                src = self._logical_source(reader, hdr)
                if src.crc is None:
                    self._unattributed_frames += 1
                    log_untagged = not reader.untagged_logged
                    reader.untagged_logged = True
                raw_iid = hdr.reserved[IQHeader.RSV_SOURCE_INSTANCE]
                src.instance_id = raw_iid - 1 if raw_iid else None
            gap = self._account(src, hdr, nbytes, rx_mono, rx_wall_ms)
            skew = self._check_skew_locked(src, rx_mono)
        if log_untagged:
            self.logger.warning("upstream router %s does not tag sources (run it with "
                                "--tag-mode node)", reader.via)
        if gap is not None:
            self.logger.warning("IQ Router: source %s cpi_index gap %d -> %d (%d frames "
                                "lost)", src.key, gap[0], gap[1],
                                ((gap[1] - gap[0]) & _U32) - 1)
        if skew is not None:
            self.logger.warning("IQ Router: source %s clock offset %d ms deviates from the "
                                "source median %d ms by more than %d ms (check NTP/PTP)",
                                src.key, skew[0], skew[1], self.max_skew_ms)

        if reader.kind == "iqserver":
            if self.tag_mode == "instance":
                _put_u32(header, _UNIT_ID_OFFSET, src.instance_id)
            elif self.tag_mode == "node":
                _put_u32(header, slot_offset(IQHeader.RSV_SOURCE_INSTANCE), src.instance_id + 1)
                _put_u32(header, slot_offset(IQHeader.RSV_SOURCE_KEY_CRC32), src.node_key_crc32)
            if self.stamp_seq:
                src.fwd_seq = (src.fwd_seq + 1) & _U32
                _put_u32(header, slot_offset(IQHeader.RSV_ROUTER_SEQ), src.fwd_seq)
            if self.stamp_rx_ts:
                ts = rx_wall_ms & 0xFFFFFFFFFFFFFFFF
                _put_u32(header, slot_offset(IQHeader.RSV_ROUTER_RX_TS_LO), ts & _U32)
                _put_u32(header, slot_offset(IQHeader.RSV_ROUTER_RX_TS_HI), ts >> 32)
        elif self.tag_mode == "instance" and src.instance_id is not None:
            _put_u32(header, _UNIT_ID_OFFSET, src.instance_id)

        frame = _Frame(bytes(header), payload, hdr.time_stamp, src)
        if self._aligner is not None:
            self._aligner.push(src, frame)
        else:
            self._forward_frame(frame)

    def _account(self, src, hdr, nbytes, rx_mono, rx_wall_ms):
        """Update one logical source's counters (stats lock held).

        The cpi_index baseline survives reconnects, so a source chain restart
        (iq_server killed, connection lost, cpi_index starts again) counts in
        ``restarts`` and frames produced while an always-connect reader was
        reconnecting count in ``frames_lost``. Only a connect-on-demand idle
        period invalidates the gap baseline (see ``gap_baseline_valid``).

        Returns ``(last, new)`` cpi indices when a gap should be logged.
        """
        src.frames_received += 1
        src.bytes_received += nbytes
        src.last_rx_mono = rx_mono
        src.last_frame_time = time.time()
        src.unit_id_seen = hdr.unit_id
        src.node_id_crc32 = hdr.reserved[IQHeader.RSV_NODE_ID_CRC32]
        if src.kind == "router":
            src.node_key_crc32 = hdr.reserved[IQHeader.RSV_SOURCE_KEY_CRC32]
            stale_after = self._connection_read_timeout(hdr)
            if (src.crc is None and stale_after is not None
                    and src.stale_after_sec is not None):
                # the untagged pseudo-source interleaves every upstream
                # instance: keep the slowest frame period seen
                stale_after = max(stale_after, src.stale_after_sec)
            src.stale_after_sec = stale_after
        cpi = hdr.cpi_index & _U32
        gap = None
        if src.tracks_continuity:
            if src.last_cpi_index is not None:
                delta = (cpi - src.last_cpi_index) & _U32
                if delta >= 0x80000000:
                    src.restarts += 1
                elif delta > 1 and src.gap_baseline_valid:
                    src.frames_lost += delta - 1
                    src.cpi_gaps += 1
                    if (src.last_gap_log is None
                            or rx_mono - src.last_gap_log >= GAP_LOG_INTERVAL_SEC):
                        src.last_gap_log = rx_mono
                        gap = (src.last_cpi_index, cpi)
            src.last_cpi_index = cpi
            src.gap_baseline_valid = True
            overrun = hdr.reserved[IQHeader.RSV_BUFFER_OVERRUN_CNT] & _U32
            if src.last_overrun_cnt is not None and overrun > src.last_overrun_cnt:
                src.overruns += overrun - src.last_overrun_cnt
            src.last_overrun_cnt = overrun
        else:
            src.last_cpi_index = cpi
        if hdr.time_stamp:
            src.offsets.append(rx_wall_ms - hdr.time_stamp)
            src.clock_offset_ms = int(round(statistics.median(src.offsets)))
        return gap

    def _all_sources(self):
        with self._stats_lock:
            return self._all_sources_locked()

    def _all_sources_locked(self):
        out = []
        for r in self._readers:
            out.extend(self._reader_sources_locked(r))
        return out

    @staticmethod
    def _reader_sources_locked(reader):
        if reader.source is not None:
            return [reader.source]
        return list(reader.logical.values())

    def _skew_reference_locked(self):
        offsets = [s.clock_offset_ms for s in self._all_sources_locked()
                   if s.clock_offset_ms is not None]
        if len(offsets) < 2:
            return None
        return statistics.median(offsets)

    def _check_skew_locked(self, src, now):
        """Refresh *src*'s skew flag; returns (offset, median) when a warning is due."""
        ref = self._skew_reference_locked()
        flagged = (self.max_skew_ms > 0 and ref is not None and src.clock_offset_ms is not None
                   and abs(src.clock_offset_ms - ref) > self.max_skew_ms)
        src.skew_warning = flagged
        if flagged and (src.last_skew_log is None
                        or now - src.last_skew_log >= SKEW_LOG_INTERVAL_SEC):
            src.last_skew_log = now
            return src.clock_offset_ms, int(round(ref))
        return None

    # -- chained identity (E.7) ----------------------------------------------
    def _lookup_node_key_locked(self, reader, crc):
        for ep in reader.stats_endpoints:
            nk = self._upstream_maps.get(ep, {}).get(crc)
            if nk:
                return nk
        return None

    def _refresh_keys_locked(self):
        """Recompute the STATS keys of every logical (chained) source."""
        counts = collections.Counter(r.source.key for r in self._readers if r.source)
        per_via = collections.Counter()
        mapped = []
        for r in self._readers:
            for crc, src in r.logical.items():
                nk = self._lookup_node_key_locked(r, crc) if crc is not None else None
                src.node_key = nk
                mapped.append((r, crc, src, nk))
                if nk:
                    counts[nk] += 1
                    per_via[(r.via, nk)] += 1
        for r, crc, src, nk in mapped:
            if crc is None:
                src.key = "via:{}".format(r.via)
            elif nk and counts[nk] == 1:
                src.key = nk
            elif nk and per_via[(r.via, nk)] == 1:
                src.key = "via:{}/{}".format(r.via, nk)
            else:      # unmapped, or one upstream maps two CRCs to the same node key
                src.key = "via:{}/crc:{:08x}".format(r.via, crc)

    def poll_upstream_stats(self):
        """Query every ``--upstream-stats`` endpoint once (SOURCES) and refresh
        the ``crc -> node_key`` tables used to name chained sources."""
        for host, port in self._upstream_endpoints:
            ep = _fmt_endpoint(host, port)
            try:
                reply = fc.request_json(host, port, "SOURCES", auth_token=self.auth_token,
                                        connect_timeout=2.0, timeout=5.0)
                if not reply.get("ok") or not isinstance(reply.get("sources"), dict):
                    raise ValueError("unexpected reply {}".format(
                        json.dumps(reply)[:200]))
            except (OSError, ValueError) as e:
                if ep not in self._upstream_poll_failed:
                    self._upstream_poll_failed.add(ep)
                    self.logger.warning("IQ Router: upstream stats %s unavailable: %s", ep, e)
                continue
            if ep in self._upstream_poll_failed:
                self._upstream_poll_failed.discard(ep)
                self.logger.info("IQ Router: upstream stats %s reachable again", ep)
            table = {}
            for key, entry in reply["sources"].items():
                if not isinstance(entry, dict):
                    continue
                nk = entry.get("node_key") or (key if not str(key).startswith("via:") else None)
                if not nk:
                    continue
                for field in ("node_key_crc32", "node_id_crc32"):
                    crc = entry.get(field)
                    if isinstance(crc, int) and crc:
                        table.setdefault(crc & _U32, str(nk))
            with self._stats_lock:
                self._upstream_maps[ep] = table
                self._refresh_keys_locked()

    def _upstream_poller(self):
        while not self._stop_event.is_set():
            self.poll_upstream_stats()
            self._stop_event.wait(self.upstream_stats_interval_sec)

    # -- statistics ----------------------------------------------------------
    def _source_doc(self, src, now):
        r = src.reader
        state = src.state(now)
        if state == "stale":
            since = src.last_rx_mono + src.stale_after_sec
        else:
            since = r.state_since
        if src.kind == "router":
            read_timeout = src.stale_after_sec or 0
        else:
            read_timeout = r.read_timeout_sec or 0
        return {"instance_id": src.instance_id,
                "node_key": src.node_key,
                "node_id": src.node_id,
                "node_key_crc32": src.node_key_crc32,
                "node_id_crc32": src.node_id_crc32,
                "connected": state == "streaming",
                "state": state,
                "state_since_sec": round(now - since, 1),
                "read_timeout_sec": read_timeout,
                "backoff_sec": r.backoff_sec,
                "consecutive_failures": r.consecutive_failures,
                "kind": src.kind,
                "via": r.via if src.kind == "router" else None,
                "endpoint": r.endpoint,
                "frames_received": src.frames_received,
                "bytes_received": src.bytes_received,
                "frames_lost": src.frames_lost,
                "cpi_gaps": src.cpi_gaps,
                "restarts": src.restarts,
                "overruns": src.overruns,
                "last_cpi_index": src.last_cpi_index,
                "last_frame_age_sec": (round(now - src.last_rx_mono, 2)
                                       if src.last_rx_mono is not None else None),
                "clock_offset_ms": src.clock_offset_ms,
                "skew_warning": src.skew_warning,
                "reconnects": r.reconnects,
                "unit_id_seen": src.unit_id_seen}

    def _sources_doc_locked(self, now):
        ref = self._skew_reference_locked()
        docs = {}
        for src in self._all_sources_locked():
            src.skew_warning = (self.max_skew_ms > 0 and ref is not None
                                and src.clock_offset_ms is not None
                                and abs(src.clock_offset_ms - ref) > self.max_skew_ms)
            docs[src.key] = self._source_doc(src, now)
        return dict(sorted(docs.items()))

    def _upstreams_doc_locked(self, now):
        return {r.via: {"endpoint": r.endpoint, "state": r.state,
                        "state_since_sec": round(now - r.state_since, 1),
                        "connected": r.state == "streaming",
                        "read_timeout_sec": r.read_timeout_sec or 0,
                        "backoff_sec": r.backoff_sec,
                        "consecutive_failures": r.consecutive_failures,
                        "reconnects": r.reconnects,
                        "frames_received": r.frames_received,
                        "bytes_received": r.bytes_received,
                        "logical_sources": len(r.logical),
                        "stats_endpoints": list(r.stats_endpoints),
                        "last_error": r.last_error}
                for r in self._readers if r.kind == "router"}

    def get_sources(self):
        """``{stats_key: source document}`` (the SOURCES reply body)."""
        now = time.monotonic()
        with self._stats_lock:
            return self._sources_doc_locked(now)

    def get_clients(self):
        now = time.monotonic()
        with self._output_lock:
            sinks = list(self._sinks)
        return [s.snapshot(now) for s in sinks]

    def get_stats(self):
        """The STATS document (spec E.6)."""
        now = time.monotonic()
        frames, nbytes = self.memory_bound()
        with self._stats_lock:
            sources = self._sources_doc_locked(now)
            upstreams = self._upstreams_doc_locked(now)
            evicted = dict(self._evicted)
            unattributed = self._unattributed_frames
            rejected = self._rejected_clients
        if self._aligner is not None:
            align = self._aligner.stats()
        else:
            align = {"late_frames": 0, "forced_releases": 0, "held_frames": 0,
                     "alignment_lag_ms": {"p50": 0, "p95": 0}}
        return {"ok": True,
                "uptime_sec": round(now - self._start_mono, 1),
                "output_port": self.output_port,
                "stats_port": self.stats_port,
                "tag_mode": self.tag_mode,
                "align_window_ms": self.align_window_ms,
                "align_max_frames": self.align_max_frames,
                "connect_on_demand": self.connect_on_demand,
                "drop_policy": self.drop_policy,
                "queue_frames": self.queue_frames,
                "max_clients": self.max_clients,
                "memory_bound_bytes": nbytes,
                "memory_bound_frames": frames,
                "sources": sources,
                "upstreams": upstreams,
                "clients": self.get_clients(),
                "evicted": evicted,
                "rejected_clients": rejected,
                "late_frames": align["late_frames"],
                "forced_releases": align["forced_releases"],
                "held_frames": align["held_frames"],
                "unattributed_frames": unattributed,
                "alignment_lag_ms": align["alignment_lag_ms"]}

    def get_stream_stats(self):
        """Legacy per-instance statistics keyed by ``instance_id`` (deep copy).

        ``{iid: {"frames_received", "bytes_received", "last_frame_time",
        "connected"}}`` for every iq_server source; sources sharing an
        instance id (different hosts) are summed.
        """
        out = {}
        with self._stats_lock:
            for r in self._readers:
                src = r.source
                if src is None:
                    continue
                d = out.setdefault(src.instance_id, {"frames_received": 0,
                                                     "bytes_received": 0,
                                                     "last_frame_time": 0.0,
                                                     "connected": False})
                d["frames_received"] += src.frames_received
                d["bytes_received"] += src.bytes_received
                d["last_frame_time"] = max(d["last_frame_time"], src.last_frame_time)
                d["connected"] = d["connected"] or r.state == "streaming"
        return out

    # -- stats server (E.6) --------------------------------------------------
    def _stats_server(self):
        while not self._stop_event.is_set():
            try:
                conn, addr = self._stats_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not _ip_allowed(addr, self.allow_cidrs):
                _close(conn)
                continue
            if not self._stats_sem.acquire(blocking=False):
                try:
                    conn.settimeout(1.0)
                    conn.sendall(fc.json_reply({"ok": False, "error": "busy",
                                                "retry_in_sec": 1}, True))
                except OSError:
                    pass
                _close(conn)
                continue
            threading.Thread(target=self._stats_handler, args=(conn,), daemon=True,
                             name="iq_router_stats_conn").start()

    def _stats_handler(self, conn):
        try:
            try:
                req = fc.read_request(conn, self.auth_token, legacy_verbs=STATS_VERBS,
                                      json_verbs=frozenset())
            except fc.RequestError as e:
                reply = e.reply
            else:
                if req is None:
                    return
                reply = self.handle_stats_command(req.verb)
            conn.settimeout(5.0)
            conn.sendall(fc.json_reply(reply, True))
        except OSError:
            pass
        finally:
            _close(conn)
            self._stats_sem.release()

    def handle_stats_command(self, verb):
        """Reply object for one stats-port verb."""
        verb = str(verb).upper()
        if verb == "PING":
            return {"ok": True, "ts": time.time()}
        if verb == "VERSION":
            return {"ok": True, "component": "iq_router", "fw_version": FW_VERSION,
                    "protocol": STATS_PROTOCOL, "verbs": list(STATS_VERBS)}
        if verb == "STATS":
            return self.get_stats()
        if verb == "SOURCES":
            return {"ok": True, "sources": self.get_sources()}
        if verb == "CLIENTS":
            return {"ok": True, "clients": self.get_clients()}
        return {"ok": False, "error": "unknown_command", "valid": list(STATS_VERBS)}


# ---------------------------------------------------------------------------
# CLI (E.1)
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Bad argument or configuration (exit 3)."""


def _v_int(lo=None, hi=None):
    def conv(text):
        try:
            v = int(str(text).strip())
        except ValueError:
            v = None
        if v is None or (lo is not None and v < lo) or (hi is not None and v > hi):
            if hi is None:
                raise ValueError("expected an integer >= {}".format(lo))
            raise ValueError("expected an integer {}..{}".format(lo, hi))
        return v
    return conv


def _v_float(lo=0.0, exclusive=False):
    def conv(text):
        try:
            v = float(str(text).strip())
        except ValueError:
            v = None
        if v is None or v != v or v < lo or (exclusive and v == lo) or v == float("inf"):
            raise ValueError("expected a number {} {:g}".format(">" if exclusive else ">=", lo))
        return v
    return conv


def _v_auto_float(text):
    if str(text).strip().lower() == "auto":
        return "auto"
    try:
        return _v_float(0.0)(text)
    except ValueError:
        raise ValueError("expected 'auto' or a number >= 0")


def _v_choice(choices):
    def conv(text):
        v = str(text).strip().lower()
        if v not in choices:
            raise ValueError("expected one of {}".format(", ".join(choices)))
        return v
    return conv


def _v_bool01(text):
    v = str(text).strip()
    if v not in ("0", "1"):
        raise ValueError("expected 0 or 1")
    return v == "1"


def _v_token(text):
    v = str(text).strip()
    if not fc.is_valid_auth_token(v):
        raise ValueError("expected a printable token without spaces, <= {} chars".format(
            fc.AUTH_TOKEN_MAX_LEN))
    return v


def _v_cidrs(text):
    out = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError("bad CIDR '{}'".format(item))
    return out


def _v_endpoints(text):
    out = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("["):
            close = item.find("]")
            host, port = (item[1:close], item[close + 2:]) if close > 0 and \
                item[close + 1:close + 2] == ":" else ("", "")
        else:
            host, _, port = item.rpartition(":")
        if not host or ":" in host and not item.startswith("[") or not port.isdigit() \
                or not 1 <= int(port) <= 65535:
            raise ValueError("bad endpoint '{}' (expected host:port)".format(item))
        out.append((host, int(port)))
    return out


#: (setting, CLI flag, [federation] key, validator, default)
_SETTINGS = (
    ("output_port", "--output-port", "router_port", _v_int(1, 65535), 7000),
    ("stats_port", "--stats-port", "router_stats_port", _v_int(0, 65535), 7001),
    ("queue_frames", "--queue-frames", "router_queue_frames", _v_int(1, 1024), 4),
    ("drop_policy", "--drop-policy", "router_drop_policy", _v_choice(DROP_POLICIES), "oldest"),
    ("tag_mode", "--tag-mode", "router_tag_mode", _v_choice(TAG_MODES), "instance"),
    ("align_window_ms", "--align-window-ms", "router_align_window_ms", _v_int(0), 0),
    ("align_max_frames", "--align-max-frames", "router_align_max_frames", _v_int(1, 64), 2),
    ("max_skew_ms", "--max-skew-ms", "router_max_skew_ms", _v_int(0), 500),
    ("max_clients", "--max-clients", "router_max_clients", _v_int(1, 256), 8),
    ("send_timeout_sec", "--send-timeout-sec", "router_send_timeout_sec",
     _v_float(0.0, exclusive=True), 2.0),
    ("read_timeout_sec", "--read-timeout-sec", "router_read_timeout_sec", _v_auto_float,
     "auto"),
    ("first_frame_timeout_sec", "--first-frame-timeout-sec",
     "router_first_frame_timeout_sec", _v_float(0.0), 0.0),
    ("reconnect_sec", "--reconnect-sec", "router_reconnect_sec", _v_float(0.0), 3.0),
    ("auth_token", "--auth-token", "auth_token", _v_token, None),
    ("allow_cidrs", "--allow-cidrs", "allow_cidrs", _v_cidrs, []),
    # CLI-only settings (no INI key)
    ("client_stall_limit", "--client-stall-limit", None, _v_int(0), 50),
    ("connect_timeout_sec", "--connect-timeout-sec", None, _v_float(0.0, exclusive=True), 2.0),
    ("upstream_stats", "--upstream-stats", None, _v_endpoints, []),
)


class _ArgumentParser(argparse.ArgumentParser):
    """Usage errors exit 3 (bad arguments), not argparse's 2 (= bind failure)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_CONFIG, "{}: error: {}\n".format(self.prog, message))


def build_arg_parser():
    p = _ArgumentParser(
        prog="federation_iq_router.py",
        description="HeIMDALL federation IQ router: aggregates the IQ streams of several "
                    "DAQ instances (and upstream routers) into one multi-client TCP "
                    "stream. Flags override --config values.")
    p.add_argument("--config", metavar="PATH",
                   help="INI file; reads [federation] router_* / membership keys and "
                        "[daq] listen_address")
    p.add_argument("--sources", metavar="LIST",
                   help="comma-separated host:iid[@node_id] (iq_server) and "
                        "router://host:port (upstream router) entries")
    p.add_argument("--output-port", metavar="PORT", help="unified output port (7000)")
    p.add_argument("--listen-address", metavar="ADDR",
                   help="bind address of the output and stats ports (0.0.0.0)")
    p.add_argument("--stats-port", metavar="PORT", help="stats port, 0 = off (7001)")
    p.add_argument("--queue-frames", metavar="N", help="per-client queue depth (4)")
    p.add_argument("--drop-policy", metavar="P",
                   help="full client queue: oldest | newest | disconnect (oldest)")
    p.add_argument("--client-stall-limit", metavar="N",
                   help="evict a client after N consecutive drops, 0 = never (50)")
    p.add_argument("--send-timeout-sec", metavar="S",
                   help="evict a client that accepts no byte for S seconds (2.0)")
    p.add_argument("--max-clients", metavar="N", help="output clients served (8)")
    p.add_argument("--tag-mode", metavar="M",
                   help="instance (rewrite unit_id) | unit (untouched) | node (slots "
                        "106/107) (instance)")
    p.add_argument("--stamp-seq", action="store_true",
                   help="stamp slot 103 with a per-source forwarded-frame counter")
    p.add_argument("--stamp-rx-ts", action="store_true",
                   help="stamp slots 104/105 with the router receive time (Unix ms)")
    p.add_argument("--align-window-ms", metavar="W",
                   help="hold frames up to W ms to release them in time_stamp order, "
                        "0 = arrival order (0)")
    p.add_argument("--align-max-frames", metavar="N",
                   help="per-source alignment FIFO depth (2)")
    p.add_argument("--max-skew-ms", metavar="MS",
                   help="clock-offset deviation that raises skew_warning, 0 = off (500)")
    cod = p.add_mutually_exclusive_group()
    cod.add_argument("--connect-on-demand", dest="connect_on_demand", action="store_const",
                     const=True, help="connect to sources only while a client is attached "
                                      "(default)")
    cod.add_argument("--always-connect", dest="connect_on_demand", action="store_const",
                     const=False, help="keep every source connected")
    p.add_argument("--connect-timeout-sec", metavar="S", help="TCP connect timeout (2.0)")
    p.add_argument("--read-timeout-sec", metavar="S",
                   help="streaming read timeout; auto = max(5, 3 x frame period), "
                        "0 = none (auto)")
    p.add_argument("--first-frame-timeout-sec", metavar="S",
                   help="give up waiting for iq_server's client slot after S s, "
                        "0 = keep waiting (0)")
    p.add_argument("--reconnect-sec", metavar="S",
                   help="first reconnect delay; doubles per failure up to 30 s (3.0)")
    p.add_argument("--upstream-stats", metavar="LIST",
                   help="host:port stats endpoints of upstream routers (node key names)")
    p.add_argument("--auth-token", metavar="T", help="token required on the stats port")
    p.add_argument("--allow-cidrs", metavar="LIST",
                   help="only accept output/stats clients from these networks")
    p.add_argument("--pid-file", metavar="PATH", help="write the process id here")
    p.add_argument("--log-file", metavar="PATH", help="log here instead of stderr")
    p.add_argument("--log-level", metavar="LEVEL", default="INFO",
                   help="DEBUG | INFO | WARNING | ERROR (INFO)")
    return p


def _load_config(path):
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        if not cp.read(path):
            raise ConfigError("cannot read config file '{}'".format(path))
    except configparser.Error as e:
        raise ConfigError("bad config file '{}': {}".format(path, e))
    return cp


def _ini_get(cp, section, key):
    if cp is None or not cp.has_section(section) or not cp.has_option(section, key):
        return None
    value = cp.get(section, key).strip()
    return value if value else None


def _ini_value(cp, section, key, conv, default):
    raw = _ini_get(cp, section, key)
    if raw is None:
        return default
    try:
        return conv(raw)
    except ValueError as e:
        raise ConfigError("bad value for [{}] {}: '{}' ({})".format(section, key, raw, e))


def _member_to_source(m):
    src = {"kind": "iqserver", "entry": m["entry"], "host": m["host"],
           "connect_host": m["connect_host"], "instance_id": m["instance_id"],
           "node_key": m["node_key"], "node_id": m["node_id"], "port": m["ports"]["iq"]}
    if m.get("self"):
        src["self"] = True
    return src


def _check_node_mode_loopback(sources, tag_mode):
    if tag_mode != "node":
        return
    for s in sources:
        if s["kind"] == "iqserver" and not s.get("self") and fc.is_loopback(s["host"]):
            raise ValueError("router source '{}' uses a loopback host; with tag_mode=node "
                             "use the host's advertised name".format(s.get("entry", s["host"])))


def resolve_sources(cp, cli_sources, tag_mode, logger=None):
    """Router membership (C.2): ``--sources`` > ``router_sources`` >
    ``cluster_members`` > ``peer_list`` (+ the self entry when
    ``router_include_self = 1``, default 1).

    Returns ``(sources, origin)``; raises ValueError / ConfigError with the
    C.2 error strings.
    """
    logger = logger or logging.getLogger(__name__)
    stride = _ini_value(cp, "federation", "port_stride", _v_int(0), fc.DEFAULT_PORT_STRIDE)
    if cli_sources is not None:
        return fc.parse_router_source_list(cli_sources, tag_mode=tag_mode,
                                           default_stride=stride), "--sources"
    router_sources = _ini_get(cp, "federation", "router_sources")
    if router_sources:
        return fc.parse_router_source_list(router_sources, tag_mode=tag_mode,
                                           default_stride=stride), "router_sources"
    cluster_members = _ini_get(cp, "federation", "cluster_members")
    if cluster_members:
        members = fc.parse_member_list(cluster_members, default_stride=stride)
        sources = [_member_to_source(m) for m in members]
        _check_node_mode_loopback(sources, tag_mode)
        return sources, "cluster_members"

    members = fc.parse_member_list(_ini_get(cp, "federation", "peer_list") or "",
                                   legacy_port_form=True, allow_duplicates=True,
                                   default_stride=stride)
    sources = []
    seen = set()
    for m in members:
        if m["instance_id"] < 0:
            logger.warning("peer_list entry '%s' names no instance (status port off the "
                           "port_stride grid); not used as an IQ source", m["entry"])
            continue
        if m["node_key"] in seen:
            logger.warning("peer_list entry '%s' duplicates '%s'; read once", m["entry"],
                           m["node_key"])
            continue
        seen.add(m["node_key"])
        sources.append(_member_to_source(m))
    _check_node_mode_loopback(sources, tag_mode)
    if _ini_value(cp, "federation", "router_include_self", _v_bool01, True):
        iid = _ini_value(cp, "federation", "instance_id", _v_int(0), 0)
        me = _member_to_source(fc.self_member(
            iid, port_stride=stride,
            advertise_host=_ini_get(cp, "federation", "advertise_host")))
        if not any(fc.is_local_host(s["host"]) and s["port"] == me["port"] for s in sources):
            sources.append(me)
    return sources, "peer_list"


def _frame_bytes_hint(cp):
    """Delay-sync output frame size from an instance INI ([hw] num_ch x
    [pre_processing] cpi_size, cf32), else 0 (learned from the first frame)."""
    num_ch = _ini_get(cp, "hw", "num_ch")
    cpi = _ini_get(cp, "pre_processing", "cpi_size")
    try:
        return IQ_HEADER_LENGTH + int(num_ch) * int(cpi) * 8 if num_ch and cpi else 0
    except ValueError:
        return 0


def resolve_settings(args):
    """CLI flag > ``--config`` value > default. Returns (settings, sources, origin, cp)."""
    cp = _load_config(args.config) if args.config else None
    settings = {}
    for name, flag, key, conv, default in _SETTINGS:
        raw = getattr(args, name)
        if raw is not None:
            try:
                settings[name] = conv(raw)
            except ValueError as e:
                raise ConfigError("bad value for {}: '{}' ({})".format(flag, raw, e))
        elif key is not None:
            settings[name] = _ini_value(cp, "federation", key, conv, default)
        else:
            settings[name] = default
    if args.connect_on_demand is not None:
        settings["connect_on_demand"] = args.connect_on_demand
    else:
        settings["connect_on_demand"] = _ini_value(cp, "federation", "router_connect_on_demand",
                                                   _v_bool01, True)
    settings["listen_address"] = args.listen_address or \
        _ini_get(cp, "daq", "listen_address") or "0.0.0.0"
    settings["stamp_seq"] = bool(args.stamp_seq)
    settings["stamp_rx_ts"] = bool(args.stamp_rx_ts)
    if settings["stats_port"] and settings["stats_port"] == settings["output_port"]:
        raise ConfigError("the stats port must differ from the output port ({})".format(
            settings["output_port"]))
    try:
        sources, origin = resolve_sources(cp, args.sources, settings["tag_mode"])
    except ValueError as e:
        raise ConfigError(str(e))
    if not sources:
        raise ConfigError("no IQ sources configured (use --sources, router_sources, "
                          "cluster_members or peer_list)")
    settings["frame_bytes_hint"] = _frame_bytes_hint(cp)
    return settings, sources, origin, cp


def _setup_logging(level_name, log_file):
    level = getattr(logging, str(level_name).upper(), None)
    if not isinstance(level, int):
        raise ConfigError("bad value for --log-level: '{}' (expected DEBUG, INFO, WARNING "
                          "or ERROR)".format(level_name))
    try:
        handler = logging.FileHandler(log_file) if log_file else logging.StreamHandler(
            sys.stderr)
    except OSError as e:
        raise ConfigError("cannot open log file '{}': {}".format(log_file, e))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def _write_pid_file(path):
    tmp = "{}.{}.tmp".format(path, os.getpid())
    with open(tmp, "w") as f:
        f.write("{}\n".format(os.getpid()))
    os.replace(tmp, path)


def _remove_pid_file(path):
    try:
        with open(path) as f:
            if f.read().strip() != str(os.getpid()):
                return
        os.unlink(path)
    except OSError:
        pass


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        _setup_logging(args.log_level, args.log_file)
        settings, sources, origin, _ = resolve_settings(args)
    except ConfigError as e:
        sys.stderr.write("error: {}\n".format(e))
        return EXIT_CONFIG
    logger = logging.getLogger("federation_iq_router")
    kwargs = dict(settings)
    output_port = kwargs.pop("output_port")
    listen_address = kwargs.pop("listen_address")
    try:
        router = FederationIQRouter(sources, output_port, listen_address, **kwargs)
    except ValueError as e:
        sys.stderr.write("error: {}\n".format(e))
        return EXIT_CONFIG
    logger.info("IQ Router %s: sources from %s: %s", FW_VERSION, origin,
                ", ".join(s.get("entry") or s.get("node_key") or s.get("via") for s in sources))
    logger.info("IQ Router settings: %s", ", ".join(
        "{}={}".format(k, "***" if k == "auth_token" and v else
                       ",".join(str(c) for c in v) if isinstance(v, list) else v)
        for k, v in sorted(settings.items())))
    try:
        router.start()
    except OSError as e:
        logger.error("IQ Router: cannot bind %s port %s/%s: %s", listen_address,
                     output_port, settings["stats_port"] or "-", e)
        sys.stderr.write("error: cannot bind output port {} or stats port {} on {}: {}\n".format(
            output_port, settings["stats_port"] or "-", listen_address, e))
        return EXIT_BIND

    stop = threading.Event()

    def _on_signal(signum, _frame):
        logger.info("IQ Router: signal %d received, stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    if args.pid_file:
        try:
            _write_pid_file(args.pid_file)
        except OSError as e:
            logger.error("IQ Router: cannot write pid file %s: %s", args.pid_file, e)
            router.close()
            sys.stderr.write("error: cannot write pid file '{}': {}\n".format(args.pid_file, e))
            return EXIT_CONFIG
    try:
        while not stop.wait(0.5):
            pass
    finally:
        router.close()
        if args.pid_file:
            _remove_pid_file(args.pid_file)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
