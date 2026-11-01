"""
    Unit tests for the federation IQ router (federation_iq_router.py, spec E).

    Hardware-free and CI-safe: every source is a FakeIQServer (iq_server's
    ``streaming`` + per-frame ``IQDownload`` protocol) or another router on
    127.0.0.1, ports come from bind(0), no sudo.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import configparser
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

_HERE = os.path.dirname(os.path.realpath(__file__))
_CORE = os.path.realpath(os.path.join(_HERE, "..", "..", "_daq_core"))
sys.path.insert(0, _CORE)

from iq_header import IQHeader  # noqa: E402
import federation_common as fc  # noqa: E402
import federation_iq_router as fir  # noqa: E402
from federation_iq_router import FederationIQRouter  # noqa: E402

ROUTER_SCRIPT = os.path.join(_CORE, "federation_iq_router.py")
LOGGER = "federation_iq_router"


def setUpModule():
    logging.getLogger(LOGGER).setLevel(logging.CRITICAL)


def tearDownModule():
    logging.getLogger(LOGGER).setLevel(logging.NOTSET)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(cond, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return bool(cond())


def build_frame(cpi_index, ts, *, unit_id=7, ch=2, cpi=16, fs=1000000, overrun=0,
                slots=None, payload=None):
    h = IQHeader()
    h.hardware_id = "FAKE"
    h.unit_id = unit_id
    h.active_ant_chs = ch
    h.cpi_length = cpi
    h.sampling_freq = fs
    h.adc_sampling_freq = fs
    h.sample_bit_depth = 32
    h.cpi_index = cpi_index
    h.time_stamp = ts
    h.header_version = 8
    h.set_buffer_overrun_cnt(overrun)
    for k, v in (slots or {}).items():
        h.reserved[k] = v
    n = ch * cpi * 2 * 4
    if payload is None:
        payload = bytes((i * 7 + cpi_index) & 0xFF for i in range(n))
    assert len(payload) == n
    return h.encode_header() + payload


def decode(header_bytes):
    h = IQHeader()
    h.decode_header(bytes(header_bytes[:1024]))
    return h


class FakeIQServer(object):
    """iq_server stand-in: one client at a time, ``streaming`` then frames,
    one ``IQDownload`` expected per frame.

    frame_fn(i) -> frame bytes, or None to stop sending and hold the
    connection silent. accept_mode: ``normal`` | ``hold`` (read
    ``streaming``, never send) | ``close`` (close at once).
    frames_per_conn / after: send that many frames per connection, then
    ``silent`` or ``close``. frame_conns: only the first N connections get
    frames, later ones are closed at once.
    """

    def __init__(self, frame_fn=None, *, period=0.0, initial_delay=0.0, accept_mode="normal",
                 frames_per_conn=None, after="silent", frame_conns=None, delays=None):
        self.frame_fn = frame_fn or (lambda i: build_frame(i, 1000 + i))
        self.period = period
        self.initial_delay = initial_delay
        self.accept_mode = accept_mode
        self.frames_per_conn = frames_per_conn
        self.after = after
        self.frame_conns = frame_conns
        self.delays = delays or {}
        self.accepts = 0
        self.accept_times = []
        self.ended = 0
        self.sent = 0
        self.downloads = 0
        self.got_streaming = 0
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self._stop.set()
        self.thread.join(timeout=5)
        self.sock.close()

    def _recv_exact(self, conn, n):
        buf = b""
        while len(buf) < n:
            if self._stop.is_set():
                return None
            try:
                chunk = conn.recv(n - len(buf))
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _hold(self, conn):
        while not self._stop.is_set():
            try:
                if not conn.recv(4096):
                    return
            except socket.timeout:
                continue
            except OSError:
                return

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            idx = self.accepts
            self.accepts += 1
            self.accept_times.append(time.monotonic())
            try:
                conn.settimeout(0.1)
                self._handle(conn, idx)
            except OSError:
                pass
            finally:
                conn.close()
                self.ended += 1

    def _handle(self, conn, idx):
        if self.accept_mode == "close" or (self.frame_conns is not None
                                           and idx >= self.frame_conns):
            return
        if self._recv_exact(conn, len(b"streaming")) != b"streaming":
            return
        self.got_streaming += 1
        if self.accept_mode == "hold":
            self._hold(conn)
            return
        if self.initial_delay and self._stop.wait(self.initial_delay):
            return
        n = 0
        while not self._stop.is_set():
            if self.frames_per_conn is not None and n >= self.frames_per_conn:
                if self.after == "silent":
                    self._hold(conn)
                return
            frame = self.frame_fn(self.sent)
            if frame is None:
                self._hold(conn)
                return
            conn.settimeout(10.0)
            conn.sendall(frame)
            conn.settimeout(0.1)
            self.sent += 1
            n += 1
            if self._recv_exact(conn, len(b"IQDownload")) != b"IQDownload":
                return
            self.downloads += 1
            pause = self.delays.get(self.sent, self.period)
            if pause and self._stop.wait(pause):
                return


class OutClient(object):
    """A consumer of the router's output port."""

    def __init__(self, port, rcvbuf=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.connect(("127.0.0.1", port))
        self.sock.settimeout(10.0)

    def recv_exact(self, n):
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            k = self.sock.recv_into(view[got:])
            if k == 0:
                raise ConnectionError("router closed the output connection")
            got += k
        return bytes(buf)

    def read_frame(self):
        header = self.recv_exact(1024)
        h = decode(header)
        payload = self.recv_exact(fir.frame_payload_bytes(h))
        return header, payload, h

    def read_frames(self, n):
        return [self.read_frame() for _ in range(n)]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def src(port, iid=0, host="127.0.0.1", node_key=None):
    """An iqserver source dict for a FakeIQServer port."""
    return {"kind": "iqserver", "host": host, "connect_host": "127.0.0.1",
            "instance_id": iid, "node_key": node_key or fc.node_key(host, iid), "port": port}


def router_src(port):
    return {"kind": "router", "host": "127.0.0.1", "connect_host": "127.0.0.1", "port": port,
            "via": "127.0.0.1:{}".format(port)}


def stats_query(port, line, timeout=5.0):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(line.encode() + b"\n")
        data = b""
        while True:
            try:
                chunk = s.recv(65536)
            except ConnectionResetError:     # closed unread (allow_cidrs refusal)
                break
            if not chunk:
                break
            data += chunk
    return data


class RouterTestCase(unittest.TestCase):
    def make_server(self, *a, **kw):
        s = FakeIQServer(*a, **kw)
        self.addCleanup(s.close)
        return s

    def make_router(self, sources, **kw):
        kw.setdefault("output_port", 0)
        kw.setdefault("listen_address", "127.0.0.1")
        r = FederationIQRouter(sources, **kw)
        r.start()
        self.addCleanup(r.close)
        return r

    def make_client(self, router, **kw):
        c = OutClient(router.output_port, **kw)
        self.addCleanup(c.close)
        self.assertTrue(wait_until(lambda: len(router.get_clients()) >= 1))
        return c

    def diff_offsets(self, a, b):
        return [i for i in range(min(len(a), len(b))) if a[i] != b[i]]


# ---------------------------------------------------------------------------
# Tagging (E.3)
# ---------------------------------------------------------------------------

class TestTagging(RouterTestCase):

    def test_instance_mode_rewrites_unit_id_exactly(self):
        server = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 3 else None)
        router = self.make_router([src(server.port, iid=3)], connect_on_demand=True)
        client = self.make_client(router)
        for i, (header, payload, h) in enumerate(client.read_frames(3)):
            original = build_frame(i, 1000 + i)
            got = header + payload
            self.assertEqual(len(got), len(original))
            self.assertEqual(self.diff_offsets(got, original), [24])   # 7 -> 3, low byte only
            self.assertEqual(h.unit_id, 3)
            self.assertEqual(got[:24], original[:24])
            self.assertEqual(got[28:], original[28:])

    def test_unit_mode_leaves_frame_untouched(self):
        server = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 3 else None)
        router = self.make_router([src(server.port, iid=3)], tag_mode="unit",
                                  connect_on_demand=True)
        client = self.make_client(router)
        for i, (header, payload, h) in enumerate(client.read_frames(3)):
            self.assertEqual(header + payload, build_frame(i, 1000 + i))
            self.assertEqual(h.unit_id, 7)

    def test_node_mode_stamps_106_107_only(self):
        server = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 2 else None)
        router = self.make_router([src(server.port, iid=2, host="hosta")], tag_mode="node",
                                  connect_on_demand=True)
        client = self.make_client(router)
        crc = zlib.crc32(b"hosta:2") & 0xFFFFFFFF
        allowed = set(range(fir.slot_offset(106), fir.slot_offset(108)))
        for i, (header, payload, h) in enumerate(client.read_frames(2)):
            original = build_frame(i, 1000 + i)
            diffs = self.diff_offsets(header + payload, original)
            self.assertTrue(diffs and set(diffs) <= allowed, diffs)
            self.assertEqual(h.get_source_instance(), 3)        # instance_id + 1
            self.assertEqual(h.get_source_key_crc32(), crc)
            self.assertEqual(h.unit_id, 7)
            self.assertEqual(h.get_router_seq(), 0)
            self.assertEqual(h.get_router_rx_ts_ms(), 0)

    def test_node_crc_stable_when_a_source_is_added(self):
        two = FederationIQRouter([src(1, 0, "hosta"), src(2, 1, "hostb")], output_port=0)
        three = FederationIQRouter([src(3, 5, "hostc"), src(1, 0, "hosta"), src(2, 1, "hostb")],
                                   output_port=0)
        crcs2 = {r.source.key: r.source.node_key_crc32 for r in two._readers}
        crcs3 = {r.source.key: r.source.node_key_crc32 for r in three._readers}
        self.assertEqual(crcs2["hosta:0"], crcs3["hosta:0"])
        self.assertEqual(crcs2["hostb:1"], crcs3["hostb:1"])
        self.assertEqual(crcs2["hosta:0"], fir.source_key_crc32("hosta:0"))
        self.assertEqual(crcs2["hosta:0"], zlib.crc32(b"hosta:0") & 0xFFFFFFFF)

    def test_stamp_seq_and_rx_ts(self):
        server = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 3 else None)
        t0 = int(time.time() * 1000)
        router = self.make_router([src(server.port, iid=1)], tag_mode="unit", stamp_seq=True,
                                  stamp_rx_ts=True, connect_on_demand=True)
        client = self.make_client(router)
        frames = client.read_frames(3)
        t1 = int(time.time() * 1000)
        allowed = set(range(fir.slot_offset(103), fir.slot_offset(106)))
        for i, (header, payload, h) in enumerate(frames):
            self.assertEqual(h.get_router_seq(), i + 1)
            self.assertTrue(t0 <= h.get_router_rx_ts_ms() <= t1)
            diffs = self.diff_offsets(header + payload, build_frame(i, 1000 + i))
            self.assertTrue(set(diffs) <= allowed, diffs)

    def test_slot_offsets_match_iq_header(self):
        h = IQHeader()
        h.reserved[106] = 0x01020304
        raw = h.encode_header()
        self.assertEqual(raw[fir.slot_offset(106):fir.slot_offset(106) + 4],
                         (0x01020304).to_bytes(4, sys.byteorder))
        h.unit_id = 0x0A0B0C0D
        raw = h.encode_header()
        self.assertEqual(raw[24:28], (0x0A0B0C0D).to_bytes(4, sys.byteorder))

    def test_bad_tag_mode_and_policy(self):
        with self.assertRaises(ValueError):
            FederationIQRouter([], tag_mode="bogus")
        with self.assertRaises(ValueError):
            FederationIQRouter([], drop_policy="bogus")
        with self.assertRaisesRegex(ValueError, "duplicate router source 'hosta:0'"):
            FederationIQRouter([src(1, 0, "hosta"), src(2, 0, "HOSTA")])


# ---------------------------------------------------------------------------
# Output path (E.2)
# ---------------------------------------------------------------------------

BIG_PAYLOAD = bytes(4 * 16384 * 8)   # 512 KiB frames


def big_frame(i):
    return build_frame(i, 1000 + i, ch=4, cpi=16384, payload=BIG_PAYLOAD)


class TestOutputPath(RouterTestCase):

    def test_non_reading_client_does_not_delay_reader(self):
        total = 80
        server = self.make_server(lambda i: big_frame(i) if i < total else None, period=0.01,
                                  initial_delay=0.3)
        router = self.make_router([src(server.port)], queue_frames=8, client_stall_limit=0,
                                  send_timeout_sec=30.0, connect_on_demand=True)
        stuck = OutClient(router.output_port, rcvbuf=16384)
        self.addCleanup(stuck.close)
        reader = self.make_client(router)
        self.assertTrue(wait_until(lambda: len(router.get_clients()) == 2))
        got = 0
        deadline = time.monotonic() + 20
        reader.sock.settimeout(3.0)
        try:
            while got < total and time.monotonic() < deadline:
                reader.read_frame()
                got += 1
        except socket.timeout:
            pass
        self.assertGreaterEqual(got, int(0.9 * total))
        clients = sorted(router.get_clients(), key=lambda c: c["sent_frames"])
        self.assertEqual(len(clients), 2)
        self.assertGreater(clients[0]["dropped_frames"], 0)
        self.assertGreater(clients[0]["overflows"], 0)
        self.assertLessEqual(clients[0]["queued"], 8)

    def test_disconnect_policy_evicts_stuck_client(self):
        total = 80
        server = self.make_server(lambda i: big_frame(i) if i < total else None, period=0.01,
                                  initial_delay=0.3)
        router = self.make_router([src(server.port)], queue_frames=8, drop_policy="disconnect",
                                  send_timeout_sec=30.0, connect_on_demand=True)
        stuck = OutClient(router.output_port, rcvbuf=16384)
        self.addCleanup(stuck.close)
        reader = self.make_client(router)
        self.assertTrue(wait_until(lambda: len(router.get_clients()) == 2))
        reader.read_frames(total - 10)
        self.assertTrue(wait_until(lambda: router.get_stats()["evicted"]["queue_full"] == 1))
        self.assertEqual(len(router.get_clients()), 1)

    def test_slow_steady_client_not_evicted(self):
        frame = build_frame(0, 1000, ch=4, cpi=8192, payload=bytes(4 * 8192 * 8))  # 256 KiB
        server = self.make_server(lambda i: frame, period=0.05)
        router = self.make_router([src(server.port)], send_timeout_sec=1.0,
                                  connect_on_demand=True)
        client = self.make_client(router, rcvbuf=32768)
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            client.sock.recv(32768)
            time.sleep(0.02)
        stats = router.get_stats()
        self.assertEqual(stats["evicted"], {r: 0 for r in fir.EVICT_REASONS})
        self.assertEqual(len(stats["clients"]), 1)
        self.assertGreaterEqual(stats["clients"][0]["sent_frames"], 2)

    def test_client_stopping_mid_frame_is_evicted_partial(self):
        payload = bytes(4 * 262144 * 8)   # 8 MiB: larger than any socket buffer pair
        frame = build_frame(0, 1000, ch=4, cpi=262144, payload=payload)
        server = self.make_server(lambda i: frame if i < 2 else None)
        router = self.make_router([src(server.port)], send_timeout_sec=0.5,
                                  connect_on_demand=True)
        client = self.make_client(router, rcvbuf=65536)
        client.recv_exact(100000)
        self.assertTrue(wait_until(lambda: router.get_stats()["evicted"]["partial_frame"] == 1,
                                   timeout=15))
        self.assertEqual(router.get_clients(), [])

    def test_max_clients_rejects_extra(self):
        router = self.make_router([], max_clients=1)
        self.make_client(router)
        extra = OutClient(router.output_port)
        self.addCleanup(extra.close)
        extra.sock.settimeout(5.0)
        self.assertEqual(extra.sock.recv(10), b"")
        self.assertTrue(wait_until(lambda: router.get_stats()["rejected_clients"] == 1))
        self.assertEqual(len(router.get_clients()), 1)

    def test_close_fast_with_stuck_client(self):
        payload = bytes(4 * 262144 * 8)
        frame = build_frame(0, 1000, ch=4, cpi=262144, payload=payload)
        server = self.make_server(lambda i: frame if i < 3 else None)
        router = FederationIQRouter([src(server.port)], output_port=0, listen_address="127.0.0.1",
                                    send_timeout_sec=60.0, connect_on_demand=True)
        router.start()
        stuck = OutClient(router.output_port, rcvbuf=16384)
        self.addCleanup(stuck.close)
        self.assertTrue(wait_until(lambda: server.sent >= 2))
        t0 = time.monotonic()
        router.close()
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_client_accepted_during_close_is_not_served(self):
        router = FederationIQRouter([src(1)], output_port=0, listen_address="127.0.0.1")
        lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lst.bind(("127.0.0.1", 0))
        lst.listen(1)
        peer = socket.create_connection(lst.getsockname(), timeout=5)
        self.addCleanup(peer.close)
        accepted = lst.accept()
        lst.close()

        class _Listener(object):        # accept() returns while close() runs
            def accept(self):
                router._stop_event.set()
                return accepted
        router._out_sock = _Listener()
        router._output_server()
        self.assertEqual(router.get_clients(), [])
        self.assertEqual(peer.recv(1), b"")       # the router closed the client

    def test_memory_bound(self):
        self.assertEqual(fir.memory_bound_frames(4, 8, 2, 2), 38)
        self.assertEqual(40 * 1024 * 1024 * fir.memory_bound_frames(4, 8, 2, 2), 1593835520)
        server = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 1 else None)
        router = self.make_router([src(server.port)])
        self.assertEqual(router.get_stats()["memory_bound_bytes"], 0)
        self.assertTrue(wait_until(lambda: server.sent == 1))
        self.assertTrue(wait_until(lambda: router.get_stats()["memory_bound_bytes"] ==
                                   (4 * 8 + 2 + 1) * len(build_frame(0, 0))))
        hinted = FederationIQRouter([src(1)], frame_bytes_hint=1000)
        self.assertEqual(hinted.memory_bound(), (35, 35000))


# ---------------------------------------------------------------------------
# Loss detection, skew (E.5)
# ---------------------------------------------------------------------------

class TestLossDetection(RouterTestCase):

    def _run_sequence(self, cpis, overruns=None, server_kw=None, router_kw=None):
        overruns = overruns or [0] * len(cpis)

        def frame_fn(i):
            if i >= len(cpis):
                return None
            return build_frame(cpis[i], 1000 + i, overrun=overruns[i])
        server = self.make_server(frame_fn, **(server_kw or {}))
        router = self.make_router([src(server.port, 0, "hosta")], **(router_kw or {}))
        self.assertTrue(wait_until(
            lambda: router.get_sources()["hosta:0"]["frames_received"] == len(cpis)))
        return router.get_sources()["hosta:0"]

    def test_gap_counts_lost_frames(self):
        s = self._run_sequence([0, 1, 2, 5, 6, 9])
        self.assertEqual(s["cpi_gaps"], 2)
        self.assertEqual(s["frames_lost"], 4)
        self.assertEqual(s["restarts"], 0)
        self.assertEqual(s["last_cpi_index"], 9)

    def test_decrease_counts_restart(self):
        s = self._run_sequence([10, 11, 12, 0, 1])
        self.assertEqual(s["restarts"], 1)
        self.assertEqual(s["cpi_gaps"], 0)
        self.assertEqual(s["frames_lost"], 0)

    # A source chain restart always drops the router's connection (iq_server
    # dies), so continuity must survive a reconnect of an always-connect reader.
    _RECONNECT = {"server_kw": {"frames_per_conn": 3, "after": "close"},
                  "router_kw": {"reconnect_sec": 0.1}}

    def test_restart_across_reconnect(self):
        s = self._run_sequence([10, 11, 12, 0, 1, 2], **self._RECONNECT)
        self.assertGreaterEqual(s["reconnects"], 1)
        self.assertEqual((s["restarts"], s["cpi_gaps"], s["frames_lost"]), (1, 0, 0))

    def test_loss_across_reconnect(self):
        s = self._run_sequence([0, 1, 2, 7, 8, 9], **self._RECONNECT)
        self.assertGreaterEqual(s["reconnects"], 1)
        self.assertEqual((s["restarts"], s["cpi_gaps"], s["frames_lost"]), (0, 1, 4))

    def _on_demand_cycle(self, step):
        """Two client sessions on a connect-on-demand router; the source's
        cpi_index moves by *step* while the router is idle."""
        holder = {}

        def frame_fn(i):
            n = holder["server"].accepts - 1
            return build_frame(1000 + step * n + i, 1000 + i)
        server = holder["server"] = self.make_server(frame_fn, period=0.03)
        router = self.make_router([src(server.port, 0, "hosta")], connect_on_demand=True)
        for _ in range(2):
            client = OutClient(router.output_port)
            client.read_frames(3)
            client.close()
            self.assertTrue(wait_until(
                lambda: router.get_sources()["hosta:0"]["state"] == "idle"))
        self.assertEqual(server.accepts, 2)
        return router.get_sources()["hosta:0"]

    def test_on_demand_idle_cycle_counts_no_gap(self):
        s = self._on_demand_cycle(100)
        self.assertEqual((s["restarts"], s["cpi_gaps"], s["frames_lost"]), (0, 0, 0))
        self.assertGreater(s["last_cpi_index"], 1100)

    def test_on_demand_idle_cycle_still_counts_restart(self):
        s = self._on_demand_cycle(-500)
        self.assertEqual((s["restarts"], s["cpi_gaps"], s["frames_lost"]), (1, 0, 0))

    def test_cpi_wrap_is_continuous(self):
        s = self._run_sequence([0xFFFFFFFE, 0xFFFFFFFF, 0, 1])
        self.assertEqual((s["restarts"], s["cpi_gaps"], s["frames_lost"]), (0, 0, 0))

    def test_overruns_from_slot_102(self):
        s = self._run_sequence([0, 1, 2, 3, 4], overruns=[5, 5, 7, 2, 4])
        self.assertEqual(s["overruns"], 4)        # +2, rebase at 2, +2

    def test_gap_logged_once_per_interval(self):
        logging.getLogger(LOGGER).setLevel(logging.WARNING)
        self.addCleanup(logging.getLogger(LOGGER).setLevel, logging.CRITICAL)
        with self.assertLogs(LOGGER, level="WARNING") as cm:
            s = self._run_sequence([0, 2, 4, 6])
        self.assertEqual(s["cpi_gaps"], 3)
        self.assertEqual(sum("cpi_index gap" in m for m in cm.output), 1)

    def test_clock_offset_and_skew_warning(self):
        def live(lag_ms):
            return lambda i: build_frame(i, int(time.time() * 1000) - lag_ms) if i < 5 else None
        a = self.make_server(live(0))
        b = self.make_server(live(0))
        c = self.make_server(live(2000))
        router = self.make_router([src(a.port, 0, "hosta"), src(b.port, 0, "hostb"),
                                   src(c.port, 0, "hostc")])
        self.assertTrue(wait_until(lambda: all(
            s["frames_received"] == 5 for s in router.get_sources().values())))
        s = router.get_sources()
        self.assertLess(abs(s["hosta:0"]["clock_offset_ms"]), 400)
        self.assertGreater(s["hostc:0"]["clock_offset_ms"], 1600)
        self.assertTrue(s["hostc:0"]["skew_warning"])
        self.assertFalse(s["hosta:0"]["skew_warning"])
        self.assertFalse(s["hostb:0"]["skew_warning"])


# ---------------------------------------------------------------------------
# Alignment (E.5)
# ---------------------------------------------------------------------------

class TestAlignment(RouterTestCase):

    def test_watermark_orders_skewed_sources_and_counts_late(self):
        # Warm-up frame (ts 100) from both so both are streaming; then A's
        # frame i (ts 1000+100i) arrives ~30 ms BEFORE B's (ts 950+100i), so
        # arrival order != time order. B finally sends one stale frame (ts 500).
        def fa(i):
            return build_frame(i, 100 if i == 0 else 1000 + 100 * (i - 1)) if i <= 10 else None

        def fb(i):
            if i == 0:
                return build_frame(i, 100)
            if i <= 10:
                return build_frame(i, 950 + 100 * (i - 1))
            return build_frame(i, 500) if i == 11 else None
        a = self.make_server(fa, delays={1: 0.2}, period=0.05)
        b = self.make_server(fb, delays={1: 0.23}, period=0.05)
        router = self.make_router([src(a.port, 0, "hosta"), src(b.port, 0, "hostb")],
                                  align_window_ms=1000, align_max_frames=8, tag_mode="unit",
                                  queue_frames=64, connect_on_demand=True)
        client = self.make_client(router)
        ts = [h.time_stamp for _, _, h in client.read_frames(23)]
        self.assertIn(500, ts)
        rest = [t for t in ts if t != 500]
        self.assertEqual(rest, sorted(rest))
        stats = router.get_stats()
        self.assertEqual(stats["late_frames"], 1)
        self.assertEqual(stats["forced_releases"], 0)
        self.assertGreater(stats["alignment_lag_ms"]["p95"], 0)

    def test_silent_source_bounded_by_max_frames(self):
        b = self.make_server(lambda i: build_frame(i, 0) if i == 0 else None)
        a = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 12 else None,
                             initial_delay=0.3, period=0.02)
        router = self.make_router([src(a.port, 0, "hosta"), src(b.port, 0, "hostb")],
                                  align_window_ms=5000, align_max_frames=2, queue_frames=64,
                                  connect_on_demand=True)
        client = self.make_client(router)
        t0 = time.monotonic()
        held = []
        stop = threading.Event()

        def sample():
            while not stop.is_set():
                held.append(router.get_stats()["held_frames"])
                time.sleep(0.01)
        th = threading.Thread(target=sample)
        th.start()
        try:
            frames = client.read_frames(11)     # B's frame + 10 of A's 12
        finally:
            stop.set()
            th.join()
        self.assertLess(time.monotonic() - t0, 4.5)
        self.assertEqual(frames[0][2].time_stamp, 0)
        self.assertLessEqual(max(held), 2 + 1)   # B's ts-0 frame may be held for a moment
        stats = router.get_stats()
        self.assertGreaterEqual(stats["forced_releases"], 8)
        self.assertEqual(router.get_sources()["hostb:0"]["state"], "streaming")

    def test_source_leaving_streaming_is_excluded(self):
        b = self.make_server(lambda i: build_frame(i, 0) if i == 0 else None, frame_conns=1)
        a = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 30 else None,
                             initial_delay=0.2, period=0.03)
        router = self.make_router([src(a.port, 0, "hosta"), src(b.port, 0, "hostb")],
                                  align_window_ms=5000, align_max_frames=64,
                                  read_timeout_sec=0.5, reconnect_sec=0.2, queue_frames=64,
                                  connect_on_demand=True)
        client = self.make_client(router)
        t0 = time.monotonic()
        frames = client.read_frames(31)
        self.assertLess(time.monotonic() - t0, 4.0)       # never waited the 5 s window
        self.assertEqual(sorted(h.time_stamp for _, _, h in frames)[1:],
                         [1000 + i for i in range(30)])
        s = router.get_sources()["hostb:0"]
        self.assertNotEqual(s["state"], "streaming")
        self.assertGreaterEqual(s["consecutive_failures"], 1)


    def test_stale_logical_source_is_excluded(self):
        a = self.make_server(lambda i: build_frame(i, 1000 + 50 * i), period=0.05)
        b = self.make_server(lambda i: build_frame(i, 1000 + 50 * i), period=0.05,
                             frames_per_conn=3, after="silent", frame_conns=1)
        up = self.make_router([src(a.port, 0, "hosta"), src(b.port, 1, "hostb")],
                              tag_mode="node", queue_frames=64)
        down = self.make_router([router_src(up.output_port)], align_window_ms=5000,
                                align_max_frames=2, read_timeout_sec=1.0, queue_frames=64,
                                connect_on_demand=True)
        client = self.make_client(down)
        via = "127.0.0.1:{}".format(up.output_port)
        key_b = "via:{}/crc:{:08x}".format(via, fir.source_key_crc32("hostb:1"))
        key_a = "via:{}/crc:{:08x}".format(via, fir.source_key_crc32("hosta:0"))
        stop = threading.Event()
        lags = []

        def drain():
            while not stop.is_set():
                try:
                    _, _, h = client.read_frame()
                except (OSError, ConnectionError):
                    return
                lags.append((time.monotonic(), h))
        th = threading.Thread(target=drain, daemon=True)
        th.start()
        self.addCleanup(lambda: (stop.set(), client.close(), th.join(5)))
        self.assertTrue(wait_until(
            lambda: down.get_sources().get(key_b, {}).get("state") == "stale", timeout=10))
        sb = down.get_sources()[key_b]
        self.assertFalse(sb["connected"])
        self.assertEqual(sb["read_timeout_sec"], 1.0)
        self.assertEqual(down.get_sources()[key_a]["state"], "streaming")
        self.assertEqual(down.get_stats()["upstreams"][via]["state"], "streaming")
        forced = down.get_stats()["forced_releases"]
        n0 = down.get_sources()[key_a]["frames_received"]
        self.assertTrue(wait_until(
            lambda: down.get_sources()[key_a]["frames_received"] >= n0 + 15, timeout=10))
        self.assertEqual(down.get_stats()["forced_releases"], forced)
        self.assertLessEqual(down.get_stats()["held_frames"], 1)
        self.assertIn("stale", fir.SOURCE_STATES)


# ---------------------------------------------------------------------------
# Reader lifecycle (E.5b) and connect-on-demand (E.7)
# ---------------------------------------------------------------------------

class TestReaderLifecycle(RouterTestCase):

    def test_waiting_for_slot_without_reconnect(self):
        server = self.make_server(accept_mode="hold")
        router = self.make_router([src(server.port, 0, "hosta")])
        self.assertTrue(wait_until(
            lambda: router.get_sources()["hosta:0"]["state"] == "waiting_for_slot"))
        time.sleep(1.5)
        self.assertEqual(server.accepts, 1)
        s = router.get_sources()["hosta:0"]
        self.assertEqual(s["state"], "waiting_for_slot")
        self.assertFalse(s["connected"])
        self.assertEqual(s["reconnects"], 0)
        self.assertGreaterEqual(s["state_since_sec"], 1.0)

    def test_first_frame_timeout_reconnects(self):
        server = self.make_server(accept_mode="hold")
        router = self.make_router([src(server.port, 0, "hosta")], first_frame_timeout_sec=0.3,
                                  reconnect_sec=0.1)
        self.assertTrue(wait_until(lambda: server.accepts >= 2))
        self.assertGreaterEqual(router.get_sources()["hosta:0"]["reconnects"], 1)

    def test_long_frame_period_not_disconnected(self):
        # T_fp = cpi_length / sampling_freq = 8 s -> auto read timeout 24 s
        server = self.make_server(lambda i: build_frame(i, 1000 + i, ch=1, cpi=8, fs=1)
                                  if i < 2 else None, delays={1: 5.6})
        router = self.make_router([src(server.port, 0, "hosta")])
        self.assertTrue(wait_until(
            lambda: router.get_sources()["hosta:0"]["frames_received"] == 1))
        self.assertEqual(router.get_sources()["hosta:0"]["read_timeout_sec"], 24.0)
        self.assertTrue(wait_until(
            lambda: router.get_sources()["hosta:0"]["frames_received"] == 2, timeout=12))
        s = router.get_sources()["hosta:0"]
        self.assertEqual(s["reconnects"], 0)
        self.assertEqual(s["state"], "streaming")
        self.assertEqual(server.accepts, 1)

    def test_auto_read_timeout_floor(self):
        server = self.make_server(lambda i: build_frame(i, 1000) if i < 1 else None)
        router = self.make_router([src(server.port, 0, "hosta")])
        self.assertTrue(wait_until(
            lambda: router.get_sources()["hosta:0"]["frames_received"] == 1))
        self.assertEqual(router.get_sources()["hosta:0"]["read_timeout_sec"], 5.0)

    def test_backoff_doubles(self):
        server = self.make_server(accept_mode="close")
        router = self.make_router([src(server.port, 0, "hosta")], reconnect_sec=0.2)
        self.assertTrue(wait_until(lambda: server.accepts >= 4, timeout=10))
        t = server.accept_times
        gaps = [t[i + 1] - t[i] for i in range(3)]
        self.assertGreater(gaps[1], gaps[0] * 1.5)
        self.assertGreater(gaps[2], gaps[1] * 1.5)
        s = router.get_sources()["hosta:0"]
        self.assertGreaterEqual(s["consecutive_failures"], 3)
        self.assertAlmostEqual(s["backoff_sec"], 0.2 * 2 ** (s["consecutive_failures"] - 1))

    def test_backoff_reset_after_frame(self):
        server = self.make_server(lambda i: build_frame(i, 1000 + i), frames_per_conn=1,
                                  after="close")
        router = self.make_router([src(server.port, 0, "hosta")], reconnect_sec=0.1)
        self.assertTrue(wait_until(lambda: server.accepts >= 4))
        self.assertLessEqual(router.get_sources()["hosta:0"]["consecutive_failures"], 1)

    def test_connect_on_demand(self):
        server = self.make_server(lambda i: build_frame(i, 1000 + i), period=0.05)
        router = self.make_router([src(server.port, 0, "hosta")], connect_on_demand=True)
        time.sleep(0.8)
        self.assertEqual(server.accepts, 0)
        self.assertEqual(router.get_sources()["hosta:0"]["state"], "idle")
        client = OutClient(router.output_port)
        client.read_frames(3)
        self.assertEqual(server.accepts, 1)
        client.close()
        self.assertTrue(wait_until(lambda: server.ended == 1, timeout=5))
        self.assertTrue(wait_until(
            lambda: router.get_sources()["hosta:0"]["state"] == "idle"))
        sent = server.sent
        time.sleep(0.5)
        self.assertEqual(server.sent, sent)
        self.assertEqual(server.accepts, 1)

    def test_always_connect_is_class_default(self):
        server = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 2 else None)
        router = self.make_router([src(server.port, 0, "hosta")])
        self.assertFalse(router.connect_on_demand)
        self.assertTrue(wait_until(lambda: server.sent == 2))
        self.assertEqual(router.get_stream_stats()[0]["frames_received"], 2)


# ---------------------------------------------------------------------------
# Chaining (E.7)
# ---------------------------------------------------------------------------

class TestChaining(RouterTestCase):

    def _upstream(self, sources, **kw):
        kw.setdefault("tag_mode", "node")
        return self.make_router(sources, **kw)

    def test_two_level_chain(self):
        a = self.make_server(lambda i: build_frame(i, 1000 + i), period=0.03)
        b = self.make_server(lambda i: build_frame(100 + i, 2000 + i), period=0.03)
        up = self._upstream([src(a.port, 0, "hosta"), src(b.port, 1, "hostb")], stamp_seq=True)
        down = self.make_router([router_src(up.output_port)], tag_mode="instance",
                                stamp_seq=True, stamp_rx_ts=True, connect_on_demand=True)
        client = self.make_client(down)
        frames = client.read_frames(20)
        crc_a = fir.source_key_crc32("hosta:0")
        crc_b = fir.source_key_crc32("hostb:1")
        seqs = {crc_a: [], crc_b: []}
        for header, payload, h in frames:
            crc = h.get_source_key_crc32()
            self.assertIn(crc, seqs)
            seqs[crc].append(h.get_router_seq())
            self.assertEqual(h.get_router_rx_ts_ms(), 0)       # downstream never re-stamps
            iid = 0 if crc == crc_a else 1
            self.assertEqual(h.get_source_instance(), iid + 1)
            self.assertEqual(h.unit_id, iid)                    # instance mode: slot 106 - 1
            orig = build_frame(h.cpi_index, h.time_stamp)
            diffs = self.diff_offsets(header + payload, orig)
            allowed = {24, 25, 26, 27} | set(range(fir.slot_offset(103), fir.slot_offset(108)))
            self.assertTrue(set(diffs) <= allowed, diffs)
        for crc, seq in seqs.items():
            self.assertTrue(seq, crc)
            self.assertEqual(seq, list(range(seq[0], seq[0] + len(seq))))  # upstream's seq
        sources = down.get_sources()
        self.assertEqual(len(sources), 2)
        via = "127.0.0.1:{}".format(up.output_port)
        for key, s in sources.items():
            self.assertTrue(key.startswith("via:{}/crc:".format(via)), key)
            self.assertEqual(s["via"], via)
            self.assertEqual(s["kind"], "router")
            self.assertIn("node_key", s)
            self.assertEqual(s["cpi_gaps"], 0)
            self.assertEqual(s["restarts"], 0)
            self.assertGreater(s["frames_received"], 0)
        self.assertEqual({s["instance_id"] for s in sources.values()}, {0, 1})
        self.assertEqual(down.get_stats()["unattributed_frames"], 0)
        self.assertEqual(down.get_stats()["upstreams"][via]["state"], "streaming")

    def test_untagged_upstream_counted_and_logged_once(self):
        a = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 6 else None,
                             period=0.02)
        up = self._upstream([src(a.port, 0, "hosta")], tag_mode="instance", queue_frames=64,
                            connect_on_demand=True)
        down = self.make_router([router_src(up.output_port)], queue_frames=64,
                                connect_on_demand=True)
        logging.getLogger(LOGGER).setLevel(logging.WARNING)
        self.addCleanup(logging.getLogger(LOGGER).setLevel, logging.CRITICAL)
        with self.assertLogs(LOGGER, level="WARNING") as cm:
            client = self.make_client(down)
            client.read_frames(6)
            self.assertTrue(wait_until(lambda: down.get_stats()["unattributed_frames"] == 6))
        msg = "upstream router 127.0.0.1:{} does not tag sources (run it with --tag-mode " \
              "node)".format(up.output_port)
        self.assertEqual(sum(msg in m for m in cm.output), 1)
        sources = down.get_sources()
        self.assertEqual(list(sources), ["via:127.0.0.1:{}".format(up.output_port)])

    def test_untagged_pseudo_source_skips_continuity(self):
        a = self.make_server(lambda i: build_frame(i, 1000 + i, overrun=i) if i < 8 else None,
                             period=0.02)
        b = self.make_server(lambda i: build_frame(500 + i, 2000 + i, overrun=50)
                             if i < 8 else None, period=0.02)
        up = self._upstream([src(a.port, 0, "hosta"), src(b.port, 1, "hosta")],
                            tag_mode="unit", queue_frames=64, connect_on_demand=True)
        down = self.make_router([router_src(up.output_port)], queue_frames=64,
                                connect_on_demand=True)
        logging.getLogger(LOGGER).setLevel(logging.WARNING)
        self.addCleanup(logging.getLogger(LOGGER).setLevel, logging.CRITICAL)
        with self.assertLogs(LOGGER, level="WARNING") as cm:
            client = self.make_client(down)
            client.read_frames(16)
            self.assertTrue(wait_until(lambda: down.get_stats()["unattributed_frames"] == 16))
        self.assertFalse(any("cpi_index gap" in m for m in cm.output), cm.output)
        s = down.get_sources()["via:127.0.0.1:{}".format(up.output_port)]
        self.assertEqual(s["frames_received"], 16)
        self.assertEqual((s["restarts"], s["cpi_gaps"], s["frames_lost"], s["overruns"]),
                         (0, 0, 0, 0))

    def test_same_node_key_under_two_crcs_of_one_upstream(self):
        router = FederationIQRouter([router_src(7999)], upstream_stats=[("127.0.0.1", 7998)])
        reader = router._readers[0]
        crcs = (0x11111111, 0x22222222)
        for crc in crcs:
            reader.logical[crc] = fir._Source(reader, "router", None, None, None, crc=crc)
        router._upstream_maps["127.0.0.1:7998"] = {c: "hosta:0" for c in crcs}
        router._refresh_keys_locked()
        keys = sorted(router.get_sources())
        self.assertEqual(keys, ["via:127.0.0.1:7999/crc:{:08x}".format(c) for c in crcs])
        for s in router.get_sources().values():
            self.assertEqual(s["node_key"], "hosta:0")
        del reader.logical[crcs[1]]
        router._refresh_keys_locked()
        self.assertEqual(list(router.get_sources()), ["hosta:0"])

    def test_memory_bound_counts_logical_sources(self):
        self.assertEqual(fir.memory_bound_frames(4, 8, 2, 1, 3), 4 * 8 + 2 * 3 + 1)
        self.assertEqual(fir.memory_bound_frames(4, 8, 2, 2, 1), 38)
        router = FederationIQRouter([router_src(7999)], frame_bytes_hint=100)
        self.assertEqual(router.memory_bound(), (35, 3500))
        reader = router._readers[0]
        for crc in (1, 2, 3):
            reader.logical[crc] = fir._Source(reader, "router", None, None, None, crc=crc)
        self.assertEqual(router.memory_bound(), (39, 3900))

    def test_router_read_timeout_covers_slowest_logical_source(self):
        fast = fir.IQHeader()
        fast.decode_header(build_frame(0, 0)[:fir.IQ_HEADER_LENGTH])            # T_fp 16 us
        slow = fir.IQHeader()
        slow.decode_header(build_frame(0, 0, cpi=8000, fs=1000)[:fir.IQ_HEADER_LENGTH])
        router = FederationIQRouter([router_src(7999)])
        reader = router._readers[0]
        self.assertEqual(router._router_read_timeout(reader, None), fir.DEFAULT_READ_TIMEOUT_SEC)
        for crc, hdr in ((1, slow), (2, fast)):
            s = fir._Source(reader, "router", None, None, None, crc=crc)
            reader.logical[crc] = s
            router._account(s, hdr, 1024, time.monotonic(), 0)
        self.assertEqual(reader.logical[1].stale_after_sec, 24.0)
        self.assertEqual(reader.logical[2].stale_after_sec, fir.DEFAULT_READ_TIMEOUT_SEC)
        # a fast frame never shrinks the connection timeout below the slow source's
        self.assertEqual(router._router_read_timeout(reader, fast), 24.0)
        self.assertEqual(router._router_read_timeout(reader, None), 24.0)
        # the untagged pseudo-source keeps the slowest frame period it has seen
        pseudo = fir._Source(reader, "router", None, None, None, crc=None)
        router._account(pseudo, slow, 1024, time.monotonic(), 0)
        router._account(pseudo, fast, 1024, time.monotonic(), 0)
        self.assertEqual(pseudo.stale_after_sec, 24.0)
        for setting, want in ((2.5, 2.5), (0, None)):
            r = FederationIQRouter([router_src(7999)], read_timeout_sec=setting)
            rd = r._readers[0]
            s = fir._Source(rd, "router", None, None, None, crc=1)
            rd.logical[1] = s
            r._account(s, slow, 1024, time.monotonic(), 0)
            self.assertEqual(r._router_read_timeout(rd, fast), want)

    def test_slow_source_keeps_upstream_after_fast_one_stops(self):
        # A: 40 fast frames (2 s) then silent; B: T_fp 8 s (stale after 24 s), one
        # frame every 5.5 s. The downstream connection starts on A's frames; the
        # B gap after A stopped exceeds the 5 s default and must not time it out.
        a = self.make_server(lambda i: build_frame(i, 1000 + i), period=0.05,
                             frames_per_conn=40, after="silent", frame_conns=1)
        b = self.make_server(lambda i: build_frame(i, 5000 + i, cpi=8000, fs=1000),
                             period=5.5, initial_delay=1.0)
        up = self._upstream([src(a.port, 0, "hosta"), src(b.port, 1, "hostb")],
                            queue_frames=64)
        down = self.make_router([router_src(up.output_port)], queue_frames=64)
        client = self.make_client(down)
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                try:
                    client.read_frame()
                except (OSError, ConnectionError):
                    return
        th = threading.Thread(target=drain, daemon=True)
        th.start()
        self.addCleanup(lambda: (stop.set(), client.close(), th.join(5)))
        via = "127.0.0.1:{}".format(up.output_port)
        key_b = "via:{}/crc:{:08x}".format(via, fir.source_key_crc32("hostb:1"))
        self.assertTrue(wait_until(
            lambda: down.get_sources().get(key_b, {}).get("frames_received", 0) >= 3,
            timeout=30))
        upstream = down.get_stats()["upstreams"][via]
        self.assertEqual((upstream["reconnects"], upstream["last_error"]), (0, None))
        self.assertEqual(upstream["read_timeout_sec"], 24.0)
        sb = down.get_sources()[key_b]
        self.assertEqual((sb["frames_lost"], sb["cpi_gaps"], sb["restarts"]), (0, 0, 0))

    def _two_upstreams(self, key_a, key_b, with_stats):
        a = self.make_server(lambda i: build_frame(i, 1000 + i), period=0.03)
        b = self.make_server(lambda i: build_frame(i, 1000 + i), period=0.03)
        sa = _free_port() if with_stats else 0
        sb = _free_port() if with_stats else 0
        up1 = self._upstream([key_a(a.port)], stats_port=sa)
        up2 = self._upstream([key_b(b.port)], stats_port=sb)
        stats = [("127.0.0.1", sa), ("127.0.0.1", sb)] if with_stats else None
        down = self.make_router([router_src(up1.output_port), router_src(up2.output_port)],
                                upstream_stats=stats, connect_on_demand=True)
        client = self.make_client(down)
        client.read_frames(10)
        self.assertTrue(wait_until(lambda: len(down.get_sources()) == 2))
        return up1, up2, down

    def test_self_entries_on_two_hosts_are_distinct(self):
        def self_src(host):
            def make(port):
                m = fc.self_member(0, advertise_host=host)
                s = fir._member_to_source(m)
                s["port"] = port
                return s
            return make
        up1, up2, down = self._two_upstreams(self_src("hosta"), self_src("hostb"), True)
        down.poll_upstream_stats()
        sources = down.get_sources()
        self.assertEqual(set(sources), {"hosta:0", "hostb:0"})
        self.assertNotEqual(sources["hosta:0"]["node_key_crc32"],
                            sources["hostb:0"]["node_key_crc32"])
        self.assertEqual(sources["hosta:0"]["node_key_crc32"], fir.source_key_crc32("hosta:0"))
        self.assertNotEqual(sources["hosta:0"]["via"], sources["hostb:0"]["via"])
        self.assertEqual(sources["hosta:0"]["node_key"], "hosta:0")

    def test_crc_collision_keeps_two_sources(self):
        same = lambda port: src(port, 0, "hostx")   # noqa: E731
        up1, up2, down = self._two_upstreams(same, same, True)
        keys = sorted(down.get_sources())
        self.assertEqual(len(keys), 2)
        down.poll_upstream_stats()
        keys = sorted(down.get_sources())
        self.assertEqual(keys, sorted(["via:127.0.0.1:{}/hostx:0".format(up1.output_port),
                                       "via:127.0.0.1:{}/hostx:0".format(up2.output_port)]))
        for s in down.get_sources().values():
            self.assertEqual(s["cpi_gaps"], 0)
            self.assertEqual(s["restarts"], 0)

    def test_unmapped_crc_key(self):
        up1, up2, down = self._two_upstreams(lambda p: src(p, 0, "hosta"),
                                             lambda p: src(p, 1, "hostb"), False)
        expect = {"via:127.0.0.1:{}/crc:{:08x}".format(up1.output_port,
                                                       fir.source_key_crc32("hosta:0")),
                  "via:127.0.0.1:{}/crc:{:08x}".format(up2.output_port,
                                                       fir.source_key_crc32("hostb:1"))}
        self.assertEqual(set(down.get_sources()), expect)

    def test_node_id_slot_108_demux(self):
        nid = fir.source_key_crc32("site-a:0")

        def fa(i):
            return build_frame(i, 1000 + i, slots={108: nid}) if i < 4 else None
        a = self.make_server(fa)
        up = self._upstream([src(a.port, 0, "hosta")], tag_mode="unit", connect_on_demand=True)
        down = self.make_router([router_src(up.output_port)], connect_on_demand=True)
        client = self.make_client(down)
        client.read_frames(4)
        key = "via:127.0.0.1:{}/crc:{:08x}".format(up.output_port, nid)
        self.assertTrue(wait_until(lambda: key in down.get_sources()))
        self.assertEqual(down.get_sources()[key]["node_id_crc32"], nid)
        self.assertEqual(down.get_stats()["unattributed_frames"], 0)

    def test_mixed_sources(self):
        direct = self.make_server(lambda i: build_frame(i, 1000 + i), period=0.03)
        far = self.make_server(lambda i: build_frame(i, 1000 + i), period=0.03)
        up = self._upstream([src(far.port, 0, "hostb")])
        down = self.make_router([src(direct.port, 0, "hosta"), router_src(up.output_port)],
                                tag_mode="node", connect_on_demand=True)
        client = self.make_client(down)
        crcs = {h.get_source_key_crc32() for _, _, h in client.read_frames(12)}
        self.assertEqual(crcs, {fir.source_key_crc32("hosta:0"),
                                fir.source_key_crc32("hostb:0")})
        kinds = {k: s["kind"] for k, s in down.get_sources().items()}
        self.assertEqual(kinds["hosta:0"], "iqserver")
        self.assertEqual(sorted(kinds.values()), ["iqserver", "router"])


# ---------------------------------------------------------------------------
# Membership / source parsing (C.2, E.1)
# ---------------------------------------------------------------------------

def _cp(text):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text)
    return cp


class TestSourceResolution(unittest.TestCase):

    def test_mixed_sources_list(self):
        sources, origin = fir.resolve_sources(None, "hosta:0,router://hostb:7000", "instance")
        self.assertEqual(origin, "--sources")
        self.assertEqual([s["kind"] for s in sources], ["iqserver", "router"])
        self.assertEqual(sources[0]["port"], 5000)
        self.assertEqual(sources[1]["via"], "hostb:7000")

    def test_parser_error_strings(self):
        with self.assertRaisesRegex(ValueError, r"^bad router source 'hosta' \(expected "
                                    r"host:instance_id\[@node_id\] or router://host:port\)$"):
            fir.resolve_sources(None, "hosta", "instance")
        with self.assertRaisesRegex(ValueError, r"^router source '127\.0\.0\.1:0' uses a "
                                    r"loopback host; with tag_mode=node use the host's "
                                    r"advertised name$"):
            fir.resolve_sources(None, "127.0.0.1:0", "node")
        # allowed without node tagging
        fir.resolve_sources(None, "127.0.0.1:0", "instance")

    def test_peer_list_uses_legacy_port_form(self):
        cp = _cp("[federation]\npeer_list = hostb:5102, hostc:1\nrouter_include_self = 0\n")
        with mock.patch.object(fc, "parse_member_list", wraps=fc.parse_member_list) as pml:
            sources, origin = fir.resolve_sources(cp, None, "instance")
        self.assertEqual(origin, "peer_list")
        self.assertTrue(pml.call_args.kwargs.get("legacy_port_form"))
        self.assertTrue(pml.call_args.kwargs.get("allow_duplicates"))
        self.assertEqual([(s["node_key"], s["instance_id"], s["port"]) for s in sources],
                         [("hostb:1", 1, 5100), ("hostc:1", 1, 5100)])

    def test_peer_list_loopback_rejected_in_node_mode(self):
        cp = _cp("[federation]\npeer_list = 127.0.0.1:5102\n")
        with self.assertRaisesRegex(ValueError, "uses a loopback host; with tag_mode=node"):
            fir.resolve_sources(cp, None, "node")

    def test_peer_list_plus_self(self):
        cp = _cp("[federation]\ninstance_id = 2\nadvertise_host = hostz\npeer_list = hostb:1\n")
        sources, _ = fir.resolve_sources(cp, None, "node")
        me = sources[-1]
        self.assertEqual((me["node_key"], me["connect_host"], me["port"]),
                         ("hostz:2", "127.0.0.1", 5200))
        self.assertTrue(me["self"])
        cp = _cp("[federation]\npeer_list = 127.0.0.1:5002\nadvertise_host = hostz\n")
        sources, _ = fir.resolve_sources(cp, None, "instance")
        self.assertEqual([s["node_key"] for s in sources], ["127.0.0.1:0"])   # self present

    def test_precedence_router_sources_cluster_members(self):
        cp = _cp("[federation]\nrouter_sources = hosta:1\ncluster_members = hostb:0,hostc:0\n"
                 "peer_list = hostd:0\n")
        sources, origin = fir.resolve_sources(cp, None, "instance")
        self.assertEqual((origin, [s["node_key"] for s in sources]), ("router_sources",
                                                                       ["hosta:1"]))
        sources, origin = fir.resolve_sources(cp, "hoste:3", "instance")
        self.assertEqual((origin, sources[0]["port"]), ("--sources", 5300))
        cp.remove_option("federation", "router_sources")
        sources, origin = fir.resolve_sources(cp, None, "instance")
        self.assertEqual((origin, [s["node_key"] for s in sources]),
                         ("cluster_members", ["hostb:0", "hostc:0"]))
        cp.set("federation", "cluster_members", "localhost:0")
        with self.assertRaisesRegex(ValueError, "uses a loopback host"):
            fir.resolve_sources(cp, None, "instance")

    def test_port_stride(self):
        cp = _cp("[federation]\nport_stride = 10\n")
        sources, _ = fir.resolve_sources(cp, "hosta:3", "instance")
        self.assertEqual(sources[0]["port"], 5030)


class TestSettings(unittest.TestCase):

    def _settings(self, argv, ini=None):
        if ini is not None:
            fd, path = tempfile.mkstemp(suffix=".ini")
            with os.fdopen(fd, "w") as f:
                f.write(ini)
            self.addCleanup(os.unlink, path)
            argv = ["--config", path] + argv
        args = fir.build_arg_parser().parse_args(argv)
        return fir.resolve_settings(args)[0]

    def test_defaults(self):
        s = self._settings(["--sources", "hosta:0"])
        self.assertEqual((s["output_port"], s["stats_port"], s["queue_frames"],
                          s["drop_policy"], s["tag_mode"], s["align_window_ms"],
                          s["align_max_frames"], s["max_skew_ms"], s["max_clients"],
                          s["send_timeout_sec"], s["read_timeout_sec"],
                          s["first_frame_timeout_sec"], s["reconnect_sec"],
                          s["client_stall_limit"], s["connect_timeout_sec"],
                          s["listen_address"]),
                         (7000, 7001, 4, "oldest", "instance", 0, 2, 500, 8, 2.0, "auto", 0.0,
                          3.0, 50, 2.0, "0.0.0.0"))
        self.assertTrue(s["connect_on_demand"])      # CLI/INI default on
        self.assertFalse(s["stamp_seq"])

    def test_cli_wins_over_config(self):
        ini = ("[daq]\nlisten_address = 10.0.0.1\n[federation]\nrouter_port = 7100\n"
               "router_queue_frames = 9\nrouter_connect_on_demand = 0\n"
               "router_read_timeout_sec = 12\nrouter_sources = hosta:0\n")
        s = self._settings(["--output-port", "7200"], ini)
        self.assertEqual((s["output_port"], s["queue_frames"], s["connect_on_demand"],
                          s["read_timeout_sec"], s["listen_address"]),
                         (7200, 9, False, 12.0, "10.0.0.1"))
        s = self._settings(["--connect-on-demand", "--listen-address", "127.0.0.1"], ini)
        self.assertTrue(s["connect_on_demand"])
        self.assertEqual(s["listen_address"], "127.0.0.1")

    def test_bad_values(self):
        with self.assertRaisesRegex(fir.ConfigError, r"bad value for --queue-frames: '0' "
                                    r"\(expected an integer 1\.\.1024\)"):
            self._settings(["--sources", "hosta:0", "--queue-frames", "0"])
        with self.assertRaisesRegex(fir.ConfigError, r"bad value for \[federation\] "
                                    r"router_drop_policy: 'x'"):
            self._settings([], "[federation]\nrouter_sources = hosta:0\n"
                               "router_drop_policy = x\n")
        with self.assertRaisesRegex(fir.ConfigError, r"bad value for --send-timeout-sec: '0' "
                                    r"\(expected a number > 0\)"):
            self._settings(["--sources", "hosta:0", "--send-timeout-sec", "0"])
        with self.assertRaisesRegex(ValueError, "send_timeout_sec must be > 0"):
            FederationIQRouter([src(1)], send_timeout_sec=0)
        with self.assertRaisesRegex(fir.ConfigError, "stats port must differ"):
            self._settings(["--sources", "hosta:0", "--output-port", "7001"])
        with self.assertRaisesRegex(fir.ConfigError, "bad CIDR"):
            self._settings(["--sources", "hosta:0", "--allow-cidrs", "10.0.0.0/33"])
        with self.assertRaisesRegex(fir.ConfigError, "cannot read config file"):
            fir.resolve_settings(fir.build_arg_parser().parse_args(
                ["--config", "/nonexistent/federation.ini"]))

    def test_frame_bytes_hint_from_instance_ini(self):
        s = self._settings([], "[hw]\nnum_ch = 5\n[pre_processing]\ncpi_size = 1048576\n"
                               "[federation]\nrouter_sources = hosta:0\n")
        self.assertEqual(s["frame_bytes_hint"], 1024 + 5 * 1048576 * 8)


# ---------------------------------------------------------------------------
# Stats endpoint (E.6) and legacy API
# ---------------------------------------------------------------------------

class TestStatsEndpoint(RouterTestCase):

    SOURCE_KEYS = {"instance_id", "node_key_crc32", "node_id_crc32", "connected", "state",
                   "state_since_sec", "read_timeout_sec", "backoff_sec",
                   "consecutive_failures", "kind", "via", "frames_received", "bytes_received",
                   "frames_lost", "cpi_gaps", "restarts", "overruns", "last_cpi_index",
                   "last_frame_age_sec", "clock_offset_ms", "skew_warning", "reconnects",
                   "unit_id_seen", "node_key"}
    STATS_KEYS = {"ok", "uptime_sec", "output_port", "tag_mode", "align_window_ms",
                  "connect_on_demand", "memory_bound_bytes", "sources", "clients", "evicted",
                  "late_frames", "forced_releases", "unattributed_frames", "alignment_lag_ms"}
    CLIENT_KEYS = {"peer", "connected_sec", "queued", "sent_frames", "sent_bytes",
                   "dropped_frames", "overflows"}

    def test_stats_json_shape(self):
        server = self.make_server(lambda i: build_frame(i, int(time.time() * 1000)), period=0.05)
        router = self.make_router([src(server.port, 0, "hosta")], stats_port=_free_port(),
                                  connect_on_demand=True)
        client = self.make_client(router)
        client.read_frames(2)
        raw = stats_query(router.stats_port, "STATS")
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        doc = json.loads(raw)
        self.assertTrue(self.STATS_KEYS <= set(doc), self.STATS_KEYS - set(doc))
        self.assertTrue(set(fir.EVICT_REASONS) >= {"slow_consumer", "partial_frame",
                                                   "send_timeout"})
        self.assertEqual(set(doc["evicted"]), set(fir.EVICT_REASONS))
        self.assertEqual(set(doc["alignment_lag_ms"]), {"p50", "p95"})
        s = doc["sources"]["hosta:0"]
        self.assertTrue(self.SOURCE_KEYS <= set(s), self.SOURCE_KEYS - set(s))
        self.assertIn(s["state"], fir.SOURCE_STATES)
        self.assertEqual((s["kind"], s["via"], s["connected"]), ("iqserver", None, True))
        self.assertEqual(s["node_key_crc32"], fir.source_key_crc32("hosta:0"))
        self.assertEqual(s["unit_id_seen"], 7)
        self.assertTrue(self.CLIENT_KEYS <= set(doc["clients"][0]))
        self.assertEqual(doc["output_port"], router.output_port)
        self.assertEqual(doc["tag_mode"], "instance")
        self.assertTrue(doc["connect_on_demand"])

    def test_verbs(self):
        router = self.make_router([src(1, 0, "hosta")], stats_port=_free_port())
        port = router.stats_port
        ping = json.loads(stats_query(port, "PING"))
        self.assertTrue(ping["ok"])
        self.assertIsInstance(ping["ts"], float)
        ver = json.loads(stats_query(port, "version"))
        self.assertEqual((ver["ok"], ver["component"]), (True, "iq_router"))
        self.assertIn("fw_version", ver)
        self.assertEqual(ver["verbs"], list(fir.STATS_VERBS))
        src_doc = json.loads(stats_query(port, "SOURCES"))
        self.assertEqual(list(src_doc["sources"]), ["hosta:0"])
        self.assertEqual(json.loads(stats_query(port, "CLIENTS")), {"ok": True, "clients": []})
        self.assertEqual(json.loads(stats_query(port, "BOGUS")),
                         {"ok": False, "error": "unknown_command",
                          "valid": ["PING", "VERSION", "STATS", "SOURCES", "CLIENTS"]})
        # no trailing newline from the client still works (one chunk = one request)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"PING")
            self.assertTrue(json.loads(s.recv(65536))["ok"])

    def test_auth_token(self):
        router = self.make_router([], stats_port=_free_port(), auth_token="s3cret")
        port = router.stats_port
        self.assertEqual(json.loads(stats_query(port, "STATS")),
                         {"ok": False, "error": "auth_required"})
        self.assertEqual(json.loads(stats_query(port, "AUTH wrong PING")),
                         {"ok": False, "error": "auth_required"})
        self.assertTrue(json.loads(stats_query(port, "AUTH s3cret PING"))["ok"])

    def test_allow_cidrs(self):
        router = self.make_router([], stats_port=_free_port(), allow_cidrs=["10.0.0.0/8"])
        self.assertEqual(stats_query(router.stats_port, "PING"), b"")
        out = OutClient(router.output_port)
        self.addCleanup(out.close)
        out.sock.settimeout(5)
        try:
            self.assertEqual(out.sock.recv(10), b"")
        except ConnectionResetError:
            pass
        ok = self.make_router([], stats_port=_free_port(), allow_cidrs=["127.0.0.0/8"])
        self.assertTrue(json.loads(stats_query(ok.stats_port, "PING"))["ok"])

    def test_upstream_stats_tolerates_unreachable(self):
        down = FederationIQRouter([router_src(1)], upstream_stats=[("127.0.0.1", _free_port())])
        down.poll_upstream_stats()            # logs, never raises
        self.assertEqual(down._readers[0].stats_endpoints, ["127.0.0.1:{}".format(
            down._upstream_endpoints[0][1])])


class TestLegacyAPI(RouterTestCase):

    def test_get_stream_stats_shape_and_deep_copy(self):
        router = FederationIQRouter([{"host": "localhost", "instance_id": 0, "iq_port": 5000},
                                     {"host": "localhost", "instance_id": 1, "iq_port": 5100}],
                                    output_port=_free_port())
        stats = router.get_stream_stats()
        self.assertEqual(set(stats), {0, 1})
        for v in stats.values():
            self.assertEqual(v, {"frames_received": 0, "bytes_received": 0,
                                 "last_frame_time": 0.0, "connected": False})
        stats[0]["frames_received"] = 99
        self.assertEqual(router.get_stream_stats()[0]["frames_received"], 0)
        self.assertEqual(router._readers[1].port, 5100)
        self.assertEqual(router._readers[1].source.key, "localhost:1")

    def test_legacy_counts_header_plus_payload(self):
        server = self.make_server(lambda i: build_frame(i, 1000 + i) if i < 3 else None)
        router = self.make_router([{"host": "127.0.0.1", "instance_id": 4,
                                    "iq_port": server.port}])
        self.assertTrue(wait_until(lambda: router.get_stream_stats()[4]["frames_received"] == 3))
        st = router.get_stream_stats()[4]
        self.assertEqual(st["bytes_received"], 3 * len(build_frame(0, 0)))
        self.assertTrue(st["connected"])
        self.assertGreater(st["last_frame_time"], 0)

    def test_start_raises_on_busy_port(self):
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        router = FederationIQRouter([], output_port=holder.getsockname()[1],
                                    listen_address="127.0.0.1")
        with self.assertRaises(OSError):
            router.start()


# ---------------------------------------------------------------------------
# CLI process (E.1)
# ---------------------------------------------------------------------------

class TestCLI(unittest.TestCase):

    def _run(self, args, timeout=20):
        return subprocess.run([sys.executable, ROUTER_SCRIPT] + args, capture_output=True,
                              text=True, timeout=timeout, cwd=_CORE)

    def test_help_exits_0(self):
        r = self._run(["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("--tag-mode", r.stdout)

    def test_bad_arguments_exit_3(self):
        r = self._run(["--sources", "hosta"])
        self.assertEqual(r.returncode, 3)
        self.assertIn("bad router source 'hosta'", r.stderr)
        r = self._run(["--sources", "localhost:0", "--tag-mode", "node"])
        self.assertEqual(r.returncode, 3)
        self.assertIn("uses a loopback host; with tag_mode=node", r.stderr)
        r = self._run(["--no-such-flag"])
        self.assertEqual(r.returncode, 3)
        r = self._run(["--sources", "hosta:0", "--drop-policy", "random"])
        self.assertEqual(r.returncode, 3)

    def test_bind_failure_exit_2(self):
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        r = self._run(["--sources", "hosta:0", "--listen-address", "127.0.0.1",
                       "--output-port", str(holder.getsockname()[1]), "--stats-port", "0"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot bind", r.stderr)

    def test_run_stats_and_sigterm(self):
        tmp = tempfile.mkdtemp()
        pid_file = os.path.join(tmp, "router.pid")
        log_file = os.path.join(tmp, "router.log")
        out_port, stats_port = _free_port(), _free_port()
        proc = subprocess.Popen(
            [sys.executable, ROUTER_SCRIPT, "--sources", "router://127.0.0.1:{}".format(
                _free_port()), "--listen-address", "127.0.0.1", "--output-port", str(out_port),
             "--stats-port", str(stats_port), "--always-connect", "--reconnect-sec", "0.2",
             "--pid-file", pid_file, "--log-file", log_file],
            cwd=_CORE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            self.assertTrue(wait_until(lambda: os.path.exists(pid_file), timeout=15))
            with open(pid_file) as f:
                self.assertEqual(int(f.read()), proc.pid)
            doc = json.loads(stats_query(stats_port, "STATS"))
            self.assertEqual(doc["output_port"], out_port)
            self.assertFalse(doc["connect_on_demand"])
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stderr.close()
        self.assertFalse(os.path.exists(pid_file))
        with open(log_file) as f:
            log = f.read()
        self.assertIn("memory bound", log)
        self.assertIn("IQ Router stopped", log)


if __name__ == "__main__":
    unittest.main()
