"""
    Description :
    Unit tests of the replay and IQ-client tools:
      - _testing/iq_replay.py: --serve-port against clients doing the real
        :5000 "streaming" / per-frame "IQDownload" handshake (byte-identical
        frames, iq_server's close rules, sequential clients, --loop,
        --restamp, --realtime), --raw-to-stdout, exit codes, the ZMQ control
        acknowledger;
      - _testing/iq_latency_probe.py against the replay server (frames,
        throughput, latency, cpi_index gaps/resets, slot-102 overrun delta);
      - _testing/iq_recorder.py (--hcap, legacy .iqf, --frames in every
        trigger mode, --instance/--port-stride, ConnectionError on close);
      - _daq_core/iq_eth_sink.py (ConnectionError instead of a busy loop);
      - daq_replay_start.sh refusals (usage, wrong kind, config mismatch:
        exit 2 before anything is touched), bash -n and shellcheck.

    Hardware-free, no sudo, no C binaries; servers bind free ports on
    127.0.0.1. Linux and macOS (CI module).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from os.path import join, dirname, realpath

current_path = dirname(realpath(__file__))
testing_path = dirname(current_path)
root_path = dirname(testing_path)
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)
sys.path.insert(0, testing_path)
sys.path.insert(0, current_path)

import iq_capture_format as hcap  # noqa: E402
import iq_latency_probe  # noqa: E402
import iq_replay  # noqa: E402
import iq_recorder  # noqa: E402
import iq_eth_sink  # noqa: E402
from iq_header import IQHeader  # noqa: E402
from test_iq_capture_format import make_frame, write_capture  # noqa: E402

try:
    import zmq
except ImportError:
    zmq = None

REPLAY_TOOL = join(testing_path, "iq_replay.py")
PROBE_TOOL = join(testing_path, "iq_latency_probe.py")
REPLAY_START = join(root_path, "daq_replay_start.sh")
TIMEOUT = 30
logging.getLogger("iq_replay").setLevel(logging.WARNING)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post_frames(n=4, start=0, ts0=0, ts_step=0):
    """DUMMY first, then CAL and DATA frames with consecutive cpi_index."""
    frames = [make_frame(IQHeader.FRAME_TYPE_DUMMY, cpi_index=start)]
    for i in range(1, n):
        ftype = IQHeader.FRAME_TYPE_CAL if i == 1 else IQHeader.FRAME_TYPE_DATA
        frames.append(make_frame(ftype, cpi_index=start + i,
                                 time_stamp=(ts0 + i * ts_step) if ts0 else 0))
    return frames


class ReplayServer:
    """iq_replay's server loop on a free 127.0.0.1 port, in a thread."""

    def __init__(self, frames, **source_kwargs):
        self.frames = frames
        self.source = iq_replay.FrameSource(
            [(hb, hcap.decode_header(hb), pb) for hb, pb in frames], **source_kwargs)
        self.sock = iq_replay.bind_server(0, "127.0.0.1")
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=iq_replay.serve, args=(self.sock, self.source), daemon=True)
        self.thread.start()

    def stop(self):
        self.source.stop()
        self.thread.join(TIMEOUT)
        self.sock.close()

    def finished(self, timeout=TIMEOUT):
        self.thread.join(timeout)
        return not self.thread.is_alive()


def connect(port, handshake=b"streaming"):
    sock = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT)
    sock.sendall(handshake)
    return sock


def closed_by_peer(sock):
    try:
        return sock.recv(1) == b""
    except ConnectionResetError:
        return True


class _ServerCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="replay_test_")
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, name):
        return join(self.tmpdir, name)

    def server(self, frames, **kwargs):
        srv = ReplayServer(frames, **kwargs)
        self.servers.append(srv)
        return srv


class TestReplayServer(_ServerCase):

    def test_streaming_handshake_byte_identical(self):
        frames = post_frames(4)
        srv = self.server(frames)
        with connect(srv.port) as c:
            for i, (hb, pb) in enumerate(frames):
                got_hb, header, got_pb = hcap.recv_wire_frame(c)
                self.assertEqual(got_hb, hb, "frame {:d} header".format(i))
                self.assertEqual(got_pb, pb, "frame {:d} payload".format(i))
                c.sendall(b"IQDownload")
            # end of the capture: the session is closed, the server ends
            self.assertTrue(closed_by_peer(c))
        self.assertTrue(srv.finished())
        self.assertEqual(srv.source.sent, 4)

    def test_bad_handshake_and_sequential_clients(self):
        frames = post_frames(4)
        srv = self.server(frames)
        with connect(srv.port, b"hello") as c:
            self.assertTrue(closed_by_peer(c))
        # "streaming" coalesced with an ack is not the exact request either
        with connect(srv.port, b"streamingIQDownload") as c:
            self.assertTrue(closed_by_peer(c))
        with connect(srv.port) as c:
            self.assertEqual(hcap.recv_wire_frame(c)[0], frames[0][0])
            c.sendall(b"q")  # any non-ack closes the session
            self.assertTrue(closed_by_peer(c))
        # the next client continues with the next frame
        with connect(srv.port) as c:
            self.assertEqual(hcap.recv_wire_frame(c)[0], frames[1][0])
            c.sendall(b"IQDownload")
            self.assertEqual(hcap.recv_wire_frame(c)[0], frames[2][0])

    def test_loop_and_restamp(self):
        frames = post_frames(3)
        srv = self.server(frames, loop=True, restamp=True)
        cpis = []
        with connect(srv.port) as c:
            for i in range(7):
                before = int(time.time() * 1000)
                got_hb, header, got_pb = hcap.recv_wire_frame(c)
                after = int(time.time() * 1000)
                cpis.append(header.cpi_index)
                hb, pb = frames[i % 3]
                self.assertEqual(got_pb, pb)
                self.assertLessEqual(before - 1000, header.time_stamp)
                self.assertLessEqual(header.time_stamp, after)
                off = iq_replay.TIME_STAMP_OFFSET
                self.assertEqual(got_hb[:off], hb[:off])
                self.assertEqual(got_hb[off + 8:], hb[off + 8:])
                c.sendall(b"IQDownload")
        self.assertEqual(cpis, [0, 1, 2, 0, 1, 2, 0])
        self.assertEqual(iq_replay.TIME_STAMP_OFFSET, 72)

    def test_realtime_pacing(self):
        frames = [make_frame(cpi_index=i, time_stamp=1000 + 150 * i) for i in range(3)]
        srv = self.server(frames, realtime=True)
        with connect(srv.port) as c:
            hcap.recv_wire_frame(c)
            t0 = time.monotonic()
            for _ in range(2):
                c.sendall(b"IQDownload")
                hcap.recv_wire_frame(c)
            elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 5.0)

    def test_frame_delay(self):
        def hdr(ts, samples=16, fs=1000000):
            return hcap.decode_header(make_frame(time_stamp=ts, samples=samples, sampling_freq=fs)[0])
        period = 16 / 1e6
        self.assertAlmostEqual(iq_replay.frame_delay(hdr(1000), hdr(1100)), 0.1)   # time stamp delta
        self.assertAlmostEqual(iq_replay.frame_delay(hdr(1100), hdr(0)), period)   # missing time stamp
        self.assertAlmostEqual(iq_replay.frame_delay(hdr(0), hdr(1050)), period)   # previous one missing
        self.assertAlmostEqual(iq_replay.frame_delay(hdr(1050), hdr(1040)), period)  # not increasing / wrap
        self.assertEqual(iq_replay.frame_period_s(hdr(0, fs=0)), 0.0)

    def test_lazy_reader_source_loops(self):
        frames = post_frames(3)
        path = write_capture(self.path("lazy.hcap"), frames)
        source = iq_replay.FrameSource(iq_replay.open_capture(path, hcap.KIND_POST_SYNC_CF32), loop=True)
        got = [source.next_frame() for _ in range(7)]
        self.assertEqual(got, [frames[i % 3] for i in range(7)])
        once = iq_replay.FrameSource(iq_replay.open_capture(path, hcap.KIND_POST_SYNC_CF32))
        self.assertEqual([once.next_frame() for _ in range(4)], frames + [None])
        with self.assertRaises(iq_replay.CaptureRefused):
            iq_replay.open_capture(path, hcap.KIND_RAW_U8)


class TestDummyInjection(unittest.TestCase):

    def frames(self, n=4):
        return [(hb, hcap.decode_header(hb), pb)
                for hb, pb in (make_frame(bits=8, samples=8, cpi_index=i, time_stamp=1000 + i) for i in range(n))]

    def test_dummy_frame_header(self):
        hb, header, _ = self.frames(1)[0]
        dummy = iq_replay.dummy_frame(hb)
        expected = IQHeader()
        expected.decode_header(hb)
        expected.frame_type = IQHeader.FRAME_TYPE_DUMMY
        expected.data_type = 0
        expected.cpi_length = 0
        self.assertEqual(dummy, expected.encode_header())

    def test_request_dummies_replaces_next_frames(self):
        frames = self.frames(4)
        source = iq_replay.FrameSource(frames)
        source.request_dummies(5)
        source.request_dummies(2)  # restarts the count, as rtl_daq's dummy_frame_cntr = 0
        got = [source.next_frame() for _ in range(4)]
        for (hb, payload), (orig_hb, orig_h, _) in zip(got[:2], frames[:2]):
            self.assertEqual((hb, payload), (iq_replay.dummy_frame(orig_hb), b""))
            h = hcap.decode_header(hb)
            self.assertEqual((h.daq_block_index, h.cpi_index, h.time_stamp),
                             (orig_h.daq_block_index, orig_h.cpi_index, orig_h.time_stamp))
        self.assertEqual(got[2:], [(hb, pb) for hb, _, pb in frames[2:]])
        self.assertIsNone(source.next_frame())

    @unittest.skipIf(zmq is None, "pyzmq not installed")
    def test_responder_requests_dummies(self):
        frames = self.frames(8)
        source = iq_replay.FrameSource(frames)
        port = free_port()
        responder = iq_replay.ControlResponder(port, source, dummy_frames=iq_replay.NO_DUMMY_FRAMES)
        responder.start()
        ctx = zmq.Context()
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 10000)
        req.setsockopt(zmq.LINGER, 0)
        req.connect("tcp://127.0.0.1:{:d}".format(port))
        try:
            req.send(b"\x02s" + bytes(126))
            self.assertEqual(req.recv(), b"ok")
            types = [hcap.decode_header(source.next_frame()[0]).frame_type for _ in range(7)]
            self.assertEqual(types, [IQHeader.FRAME_TYPE_DUMMY] * 5 + [IQHeader.FRAME_TYPE_DATA] * 2)
        finally:
            req.close()
            ctx.term()
            source.stop()
            responder.join(TIMEOUT)


class TestReplayCli(_ServerCase):

    def run_replay(self, *args, timeout=TIMEOUT):
        return subprocess.run([sys.executable, REPLAY_TOOL] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)

    def test_raw_to_stdout(self):
        frames = [make_frame(IQHeader.FRAME_TYPE_DUMMY, bits=8)] + \
                 [make_frame(bits=8, samples=32, cpi_index=i) for i in range(1, 4)]
        path = write_capture(self.path("raw.hcap"), frames, kind=hcap.KIND_RAW_U8)
        res = self.run_replay(path, "--raw-to-stdout")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, b"".join(h + p for h, p in frames))

    def test_raw_to_closed_pipe_exits_cleanly(self):
        frames = [make_frame(bits=8, samples=4096, channels=4, cpi_index=i) for i in range(4)]
        path = write_capture(self.path("raw.hcap"), frames, kind=hcap.KIND_RAW_U8)
        proc = subprocess.Popen([sys.executable, REPLAY_TOOL, path, "--raw-to-stdout", "--loop"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdout.read(4096)
        proc.stdout.close()
        _, err = proc.communicate(timeout=TIMEOUT)
        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn(b"Traceback", err)

    def test_refusals(self):
        post = write_capture(self.path("post.hcap"), post_frames(2))
        raw = write_capture(self.path("raw.hcap"), [make_frame(bits=8)], kind=hcap.KIND_RAW_U8)
        self.assertEqual(self.run_replay(post, "--raw-to-stdout").returncode, 2)
        self.assertEqual(self.run_replay(raw, "--serve-port", str(free_port())).returncode, 2)
        bare = self.path("bare.hcap")
        shutil.copy(post, bare)
        res = self.run_replay(bare, "--serve-port", str(free_port()))
        self.assertEqual(res.returncode, 2)
        self.assertIn(b"no sidecar", res.stderr)
        empty = write_capture(self.path("empty.hcap"), [])
        self.assertEqual(self.run_replay(empty, "--serve-port", str(free_port())).returncode, 2)
        # corrupt capture: runtime error
        with open(post, "r+b") as f:
            f.write(b"\0\0\0\0")
        self.assertEqual(self.run_replay(post, "--serve-port", str(free_port())).returncode, 1)
        # usage errors (argparse)
        self.assertEqual(self.run_replay(raw).returncode, 2)
        self.assertEqual(self.run_replay(raw, "--raw-to-stdout", "--serve-port", "1").returncode, 2)
        self.assertEqual(self.run_replay(post, "--serve-port", "70000").returncode, 2)

    def test_bind_failure(self):
        post = write_capture(self.path("post.hcap"), post_frames(2))
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            res = self.run_replay(post, "--serve-port", str(busy.getsockname()[1]),
                                  "--listen-address", "127.0.0.1")
        self.assertEqual(res.returncode, 1)
        self.assertIn(b"cannot listen", res.stderr)

    def test_serve_subprocess_with_probe_cli(self):
        frames = post_frames(5)
        path = write_capture(self.path("post.hcap"), frames)
        port = free_port()
        proc = subprocess.Popen([sys.executable, REPLAY_TOOL, path, "--serve-port", str(port),
                                 "--listen-address", "127.0.0.1", "--restamp"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + TIMEOUT
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    self.assertLess(time.monotonic(), deadline, "replay server did not start")
                    time.sleep(0.05)
            # that probe connection sent no handshake: the server closed it
            res = subprocess.run([sys.executable, PROBE_TOOL, "--port", str(port), "--duration", "20",
                                  "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=TIMEOUT)
            self.assertEqual(res.returncode, 0, res.stderr)
            rep = json.loads(res.stdout)
            self.assertEqual(rep["frames"], 5)
            self.assertEqual(rep["cpi_index_gaps"], 0)
            self.assertEqual(rep["latency_ms"]["count"], 5)
            self.assertIn("ConnectionError", rep["error"])  # end of the capture
            proc.wait(TIMEOUT)
            self.assertEqual(proc.returncode, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()


@unittest.skipIf(zmq is None, "pyzmq not installed")
class TestControlResponder(unittest.TestCase):

    def test_ack_and_halt(self):
        frames = [(hb, hcap.decode_header(hb), pb) for hb, pb in post_frames(2)]
        source = iq_replay.FrameSource(frames)
        port = free_port()
        responder = iq_replay.ControlResponder(port, source)
        responder.start()
        ctx = zmq.Context()
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 10000)
        req.setsockopt(zmq.LINGER, 0)
        req.connect("tcp://127.0.0.1:{:d}".format(port))
        try:
            req.send(b"\x02c" + bytes(126))
            self.assertEqual(req.recv(), b"ok")
            req.send(b"g" * 3)
            self.assertEqual(req.recv(), b"err")
            self.assertFalse(source.stopped)
            req.send(b"\x02h" + bytes(126))
            self.assertEqual(req.recv(), b"ok")
            responder.join(TIMEOUT)
            self.assertFalse(responder.is_alive())
            self.assertTrue(source.stopped)
            with self.assertRaises(iq_replay.ReplayStopped):
                source.next_frame()
        finally:
            req.close()
            ctx.term()
            source.stop()
            responder.join(TIMEOUT)


class TestLatencyProbe(_ServerCase):

    def test_report_against_replay(self):
        cpis = [0, 1, 2, 5, 6]
        overruns = [3, 3, 4, 7, 7]
        frames = [make_frame(cpi_index=c, overruns=o, time_stamp=0) for c, o in zip(cpis, overruns)]
        srv = self.server(frames, restamp=True)
        rep, error = iq_latency_probe.probe("127.0.0.1", srv.port, duration_s=20)
        self.assertIn("ConnectionError", error)  # replay ended
        self.assertEqual(rep["frames"], 5)
        self.assertEqual(rep["frame_types"]["data"], 5)
        self.assertEqual(rep["bytes"], 5 * (1024 + 2 * 16 * 8))
        self.assertGreater(rep["throughput_mbps"], 0)
        self.assertEqual((rep["cpi_index_first"], rep["cpi_index_last"]), (0, 6))
        self.assertEqual((rep["cpi_index_gaps"], rep["cpi_index_missing"], rep["cpi_index_resets"]),
                         (1, 2, 0))
        self.assertEqual((rep["overrun_first"], rep["overrun_last"], rep["overrun_delta"]), (3, 7, 4))
        lat = rep["latency_ms"]
        self.assertEqual(lat["count"], 5)
        self.assertGreaterEqual(lat["min"], -50)   # restamped at send time
        self.assertLess(lat["max"], 5000)
        exc = rep["latency_minus_frame_ms"]
        self.assertEqual(exc["count"], 5)
        self.assertAlmostEqual(exc["mean"], lat["mean"] - 1000.0 * 16 / 1e6, places=2)
        self.assertEqual(rep["last_frame"]["channels"], 2)

    def test_frame_limit_leaves_server_for_next_client(self):
        frames = post_frames(4)
        srv = self.server(frames, loop=True)
        rep, error = iq_latency_probe.probe("127.0.0.1", srv.port, duration_s=20, max_frames=2)
        self.assertIsNone(error)
        self.assertEqual(rep["frames"], 2)
        # no time_stamp in these frames
        self.assertEqual(rep["latency_ms"]["count"], 0)
        self.assertIsNone(rep["latency_ms"]["p50"])
        # the probe's 'q' closed its session; the next client gets frame 2
        with connect(srv.port) as c:
            self.assertEqual(hcap.recv_wire_frame(c)[1].cpi_index, 2)

    def test_resets_on_loop_and_duration(self):
        srv = self.server(post_frames(3), loop=True)
        rep, error = iq_latency_probe.probe("127.0.0.1", srv.port, duration_s=0.3)
        self.assertIsNone(error)
        self.assertGreater(rep["frames"], 6)
        self.assertGreaterEqual(rep["cpi_index_resets"], 2)
        self.assertEqual(rep["cpi_index_gaps"], 0)
        self.assertEqual(rep["frame_types"]["dummy"] + rep["frame_types"]["cal"] +
                         rep["frame_types"]["data"], rep["frames"])

    def test_stats_wrap(self):
        stats = iq_latency_probe.ProbeStats()
        for cpi, ovr in ((0xFFFFFFFE, 0xFFFFFFFF), (0xFFFFFFFF, 0xFFFFFFFF), (1, 2)):
            stats.add(hcap.decode_header(make_frame(cpi_index=cpi & 0xFFFFFFFF, overruns=ovr)[0]), 1, 0)
        rep = stats.report(1.0, "h", 1)
        self.assertEqual((rep["cpi_index_gaps"], rep["cpi_index_missing"], rep["cpi_index_resets"]), (1, 1, 0))
        self.assertEqual(rep["overrun_delta"], 3)

    def test_cli(self):
        self.assertEqual(iq_latency_probe.compute_port(5000, 3, 100), 5300)

        def run(*args):
            return subprocess.run([sys.executable, PROBE_TOOL] + list(args), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=TIMEOUT)
        self.assertEqual(run("--port", "1", "--instance", "1").returncode, 2)
        self.assertEqual(run("--duration", "0").returncode, 2)
        self.assertEqual(run("--port", "70000").returncode, 2)
        res = run("--port", str(free_port()), "--json", "--timeout", "2")
        self.assertEqual(res.returncode, 1)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["frames"], 0)
        self.assertIn("cannot connect", doc["error"])
        # --instance N --port-stride 1 addresses port 5000 + N
        srv = self.server(post_frames(3))
        res = run("--instance", str(srv.port - 5000), "--port-stride", "1", "--duration", "20")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(b"3 frames", res.stdout)
        self.assertIn(b"gaps 0", res.stdout)


class TestRecorder(_ServerCase):

    def recorder(self, port, **kwargs):
        kwargs.setdefault("ctrl_file", self.path("no_ctrl"))
        kwargs.setdefault("out_dir", self.tmpdir)
        kwargs.setdefault("min_block_index", 0)
        return iq_recorder.IQRecorder(host="127.0.0.1", port=port, **kwargs)

    def test_hcap_phase_cont(self):
        frames = post_frames(6)  # dummy, cal, data x4 (all delay_sync_flag 1)
        srv = self.server(frames)
        path = self.path("rec.hcap")
        rec = self.recorder(srv.port, frames=3, hcap_path=path, hcap_meta={"notes": "t", "instance_id": 0})
        self.assertEqual(rec.start_process(), 0)
        got = list(hcap.HcapReader(path).raw_frames())
        # frame 0 is consumed by the handshake, frame 1 is CAL (not triggered)
        self.assertEqual([(h, p) for h, _, p in got], frames[2:5])
        meta = hcap.read_meta(path)
        self.assertEqual((meta["kind"], meta["frames"], meta["notes"], meta["instance_id"]),
                         ("post_sync_cf32", 3, "t", 0))
        self.assertEqual(meta["source"], "tcp://127.0.0.1:{:d}".format(srv.port))

    def test_calibration_mode_honours_frames(self):
        frames = [make_frame(IQHeader.FRAME_TYPE_CAL, cpi_index=i) for i in range(8)]
        srv = self.server(frames)
        path = self.path("cal.hcap")
        rec = self.recorder(srv.port, frames=2, test_case="calibration", hcap_path=path)
        self.assertEqual(rec.start_process(), 0)
        self.assertEqual([h.cpi_index for h, _ in hcap.HcapReader(path)], [1, 2])
        self.assertEqual(srv.source.sent, 3)  # stopped after the budget

    def test_all_mode_records_every_frame(self):
        frames = post_frames(5) + [make_frame(cpi_index=5, delay_sync=0, iq_sync=0)]
        srv = self.server(frames)
        path = self.path("all.hcap")
        rec = self.recorder(srv.port, frames=5, test_case="all", hcap_path=path)
        self.assertEqual(rec.start_process(), 0)
        self.assertEqual([(h, p) for h, _, p in hcap.HcapReader(path).raw_frames()], frames[1:])

    def test_legacy_iqf_output(self):
        frames = post_frames(4)
        srv = self.server(frames)
        rec = self.recorder(srv.port, frames=1, prefix="T")
        self.assertEqual(rec.start_process(), 0)
        with open(self.path("T_0.iqf"), "rb") as f:
            data = f.read()
        hb, pb = frames[2]
        self.assertEqual(data, hb + pb + bytes(2 ** 18))
        self.assertFalse(os.path.exists(self.path("T_1.iqf")))

    def test_server_close_raises_not_spins(self):
        srv = self.server(post_frames(3))
        rec = self.recorder(srv.port, frames=50, hcap_path=self.path("short.hcap"))
        t0 = time.monotonic()
        self.assertEqual(rec.start_process(), -1)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(hcap.read_meta(self.path("short.hcap"))["frames"], 1)

    def test_ctrl_file(self):
        ctrl = self.path("rec_ctr")
        with open(ctrl, "w") as f:
            f.write("0")
        srv = self.server(post_frames(4))
        rec = self.recorder(srv.port, frames=1, ctrl_file=ctrl, hcap_path=self.path("paused.hcap"))
        self.assertEqual(rec.start_process(), -1)  # paused until the stream ended
        self.assertEqual(hcap.read_meta(self.path("paused.hcap"))["frames"], 0)
        with open(ctrl, "w") as f:
            f.write("2")
        srv = self.server(post_frames(4))
        rec = self.recorder(srv.port, frames=1, ctrl_file=ctrl, hcap_path=self.path("exit.hcap"))
        self.assertEqual(rec.start_process(), 0)
        self.assertEqual(hcap.read_meta(self.path("exit.hcap"))["frames"], 0)

    def test_unwritable_output_returns_error(self):
        srv = self.server(post_frames(4))
        rec = self.recorder(srv.port, frames=2, test_case="all", out_dir=self.path("missing_dir"))
        self.assertEqual(rec.start_process(), -1)
        srv = self.server(post_frames(4))
        rec = self.recorder(srv.port, frames=2, hcap_path=self.path(join("missing_dir", "x.hcap")))
        self.assertEqual(rec.start_process(), -1)
        self.assertIsNone(rec.rec_ctr_fd)

    def test_rejected_frame_returns_error(self):
        # an 8-bit frame cannot go into a post_sync_cf32 capture
        frames = [make_frame(cpi_index=0), make_frame(bits=8, cpi_index=1)]
        srv = self.server(frames)
        path = self.path("bad.hcap")
        rec = self.recorder(srv.port, frames=2, test_case="all", hcap_path=path)
        self.assertEqual(rec.start_process(), -1)
        self.assertEqual(hcap.read_meta(path)["frames"], 0)

    def test_connect_failure(self):
        rec = self.recorder(free_port(), frames=1)
        self.assertEqual(rec.start_process(), -1)

    def test_cli_instance_port(self):
        frames = post_frames(4)
        srv = self.server(frames)
        out = self.path("cli.hcap")
        ini = self.path("cfg.ini")
        with open(ini, "w") as f:
            f.write("[hw]\nnum_ch = 2\n")
        rc = iq_recorder.main(["--instance", str(srv.port - 5000), "--port-stride", "1",
                               "--frames", "1", "--min-block-index", "0", "--hcap", out,
                               "--config", ini, "--ctrl-file", self.path("none")])
        self.assertEqual(rc, 0)
        meta = hcap.read_meta(out)
        self.assertEqual(meta["instance_id"], srv.port - 5000)
        self.assertEqual(meta["config"], "[hw]\nnum_ch = 2\n")
        self.assertEqual(meta["frames"], 1)
        with self.assertRaises(SystemExit):
            iq_recorder.main(["--port", "1", "--instance", "1"])
        with self.assertRaises(SystemExit):
            iq_recorder.main(["--frames", "0"])


class TestEthSink(_ServerCase):

    def test_counts_dropped_frames(self):
        frames = [make_frame(IQHeader.FRAME_TYPE_DUMMY)] + \
                 [make_frame(cpi_index=c) for c in (1, 2, 4, 5)]
        srv = self.server(frames)
        sink = iq_eth_sink.IQRecorder(4, host="127.0.0.1", port=srv.port)
        self.addCleanup(lambda: sink.socket_inst.close())  # close_eth() leaves a fresh socket
        self.assertEqual(sink.listen(), 0)
        self.assertEqual(sink.dropped_frames, 1)

    def test_stream_end_raises_connection_error(self):
        srv = self.server(post_frames(3))
        sink = iq_eth_sink.IQRecorder(100, host="127.0.0.1", port=srv.port)
        t0 = time.monotonic()
        self.assertEqual(sink.listen(), -1)  # the old loop spun on recv_into() == 0
        self.assertLess(time.monotonic() - t0, 10)
        a, b = socket.socketpair()
        with a, b:
            sink.socket_inst.close()
            sink.socket_inst = b
            a.sendall(make_frame()[0][:10])
            a.close()
            with self.assertRaises(ConnectionError):
                sink.receive_iq_frame()

    def test_connect_attempts(self):
        sink = iq_eth_sink.IQRecorder(1, host="127.0.0.1", port=free_port(), connect_attempts=1)
        self.assertEqual(sink.listen(), -1)


@unittest.skipIf(shutil.which("bash") is None, "bash not available")
class TestReplayStartScript(unittest.TestCase):
    """daq_replay_start.sh refusal paths in a sandbox copy (exit 2 before
    the INI check, FIFOs, logs or any stage)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="replay_start_")
        for name in ("daq_replay_start.sh", "daq_common.sh"):
            shutil.copy(join(root_path, name), self.tmpdir)
        os.symlink(testing_path, join(self.tmpdir, "_testing"))
        os.symlink(daq_core_path, join(self.tmpdir, "_daq_core"))
        with open(join(self.tmpdir, "daq_chain_config.ini"), "w") as f:
            f.write("[hw]\nnum_ch = 2\n[daq]\ndaq_buffer_size = 32\n")
        bindir = join(self.tmpdir, "bin")
        os.mkdir(bindir)
        with open(join(bindir, "python3"), "w") as f:
            f.write("#!/bin/sh\nexec '{}' \"$@\"\n".format(sys.executable))
        os.chmod(join(bindir, "python3"), 0o755)
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_script(self, *args):
        return subprocess.run(["bash", "./daq_replay_start.sh"] + list(args), cwd=self.tmpdir,
                              env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=TIMEOUT)

    def assert_untouched(self):
        for name in ("_data_control", "_logs", "_db"):
            self.assertFalse(os.path.exists(join(self.tmpdir, name)), name)

    def test_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", REPLAY_START]).returncode, 0)
        self.assertTrue(os.access(REPLAY_START, os.X_OK))

    @unittest.skipIf(shutil.which("shellcheck") is None, "shellcheck not available")
    def test_shellcheck(self):
        res = subprocess.run(["shellcheck", "-x", "daq_replay_start.sh"], cwd=root_path,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=TIMEOUT)
        self.assertEqual(res.returncode, 0, res.stdout.decode())

    def test_registry_mode_is_synthetic(self):
        # spec B.3.1: a registry entry's mode is hw or synthetic (preflight
        # and capacity_model reject / remap anything else)
        with open(REPLAY_START) as f:
            calls = [line.split() for line in f if line.strip().startswith("hd_registry_")]
        modes = {c[0]: c[2] if c[0] == "hd_registry_begin" else c[1] for c in calls}
        self.assertEqual(modes, {"hd_registry_begin": "synthetic", "hd_registry_write": "synthetic"})

    def test_usage_errors(self):
        self.assertEqual(self.run_script().returncode, 2)
        self.assertEqual(self.run_script("--bogus", "x.hcap").returncode, 2)
        self.assertEqual(self.run_script("a.hcap", "b.hcap").returncode, 2)
        res = self.run_script("--help")
        self.assertEqual(res.returncode, 0)
        self.assertIn(b"CAPTURE.hcap", res.stdout)
        self.assert_untouched()

    def test_refuses_non_raw_capture(self):
        post = write_capture(join(self.tmpdir, "post.hcap"), post_frames(2))
        res = self.run_script("--realtime", post)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn(b"raw_u8 is required", res.stderr)
        self.assertIn(b"refused", res.stdout)
        self.assert_untouched()

    def test_refuses_mismatched_raw_capture(self):
        raw = write_capture(join(self.tmpdir, "raw.hcap"), [make_frame(bits=8, samples=64)],
                            kind=hcap.KIND_RAW_U8)
        res = self.run_script(raw)
        self.assertEqual(res.returncode, 2)
        self.assertIn(b"daq_buffer_size = 32", res.stderr)
        self.assertEqual(self.run_script(join(self.tmpdir, "missing.hcap")).returncode, 2)
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main(verbosity=2)
