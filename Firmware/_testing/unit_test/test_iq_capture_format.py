"""
    Description :
    Unit tests of the .hcap capture container (_testing/iq_capture_format.py)
    and of its offline users: iq_analyzer.py (.hcap / .iqf loading, header
    sample rate) and capture_shmem_stream.py (.hcap output over a real
    FIFO + shared-memory loopback).

    Hardware-free, no sudo, no C binaries; Linux and macOS (CI module).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from os.path import join, dirname, realpath

current_path = dirname(realpath(__file__))
testing_path = dirname(current_path)
root_path = dirname(testing_path)
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)
sys.path.insert(0, testing_path)
sys.path.insert(0, current_path)

import iq_capture_format as hcap  # noqa: E402
from iq_header import IQHeader  # noqa: E402
from version import FW_VERSION  # noqa: E402

try:
    import numpy as np
except ImportError:  # the analyzer / shmem tests need numpy
    np = None

CAPTURE_TOOL = join(testing_path, "iq_capture_format.py")
TIMEOUT = 30


def make_frame(frame_type=IQHeader.FRAME_TYPE_DATA, channels=2, samples=16, bits=32,
               cpi_index=0, time_stamp=0, sampling_freq=1000000, overruns=0,
               delay_sync=1, iq_sync=1, block_index=None, seed=None):
    """(header_bytes, payload) of a consistent frame; DUMMY frames carry
    cpi_length 0 and no payload, as the chain emits them."""
    h = IQHeader()
    h.frame_type = frame_type
    h.hardware_id = "K4"
    h.active_ant_chs = channels
    h.cpi_length = 0 if frame_type == IQHeader.FRAME_TYPE_DUMMY else samples
    h.sample_bit_depth = bits
    h.sampling_freq = sampling_freq
    h.adc_sampling_freq = sampling_freq
    h.rf_center_freq = 433000000
    h.cpi_index = cpi_index
    h.daq_block_index = cpi_index if block_index is None else block_index
    h.time_stamp = time_stamp
    h.delay_sync_flag = delay_sync
    h.iq_sync_flag = iq_sync
    h.header_version = 8
    h.set_buffer_overrun_cnt(overruns)
    size = hcap.payload_length(h)
    rnd = (seed if seed is not None else cpi_index) & 0xFF
    payload = bytes((rnd + i) & 0xFF for i in range(size))
    return h.encode_header(), payload


def write_capture(path, frames, kind=hcap.KIND_POST_SYNC_CF32, meta=None):
    with hcap.HcapWriter(path, kind, meta) as writer:
        for header_bytes, payload in frames:
            writer.write(header_bytes, payload)
    return path


def mixed_frames(bits=32, samples=16):
    return [make_frame(IQHeader.FRAME_TYPE_DUMMY, bits=bits, cpi_index=0),
            make_frame(IQHeader.FRAME_TYPE_CAL, bits=bits, samples=samples, cpi_index=1, time_stamp=1000),
            make_frame(IQHeader.FRAME_TYPE_DATA, bits=bits, samples=samples, cpi_index=2, time_stamp=1100),
            make_frame(IQHeader.FRAME_TYPE_DATA, bits=bits, samples=samples, cpi_index=3, time_stamp=1200)]


class _TmpDirCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hcap_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, name):
        return join(self.tmpdir, name)


class TestWriterReader(_TmpDirCase):

    def test_round_trip_with_dummy_frames(self):
        frames = mixed_frames()
        path = write_capture(self.path("a.hcap"), frames,
                             meta={"instance_id": 2, "config": "[hw]\nnum_ch = 2\n", "notes": "unit"})
        # body = exact concatenation, no padding
        with open(path, "rb") as f:
            body = f.read()
        self.assertEqual(body, b"".join(h + p for h, p in frames))
        self.assertEqual(len(frames[0][1]), 0)

        reader = hcap.HcapReader(path)
        got = list(reader)
        self.assertEqual(len(got), 4)
        for (header, payload), (hb, pb) in zip(got, frames):
            self.assertIsInstance(header, IQHeader)
            self.assertEqual(header.encode_header(), hb)
            self.assertEqual(payload, pb)
        self.assertEqual(got[0][0].frame_type, IQHeader.FRAME_TYPE_DUMMY)
        self.assertEqual(got[0][1], b"")
        # raw_frames hands back the stored header bytes
        self.assertEqual([r[0] for r in reader.raw_frames()], [h for h, _ in frames])
        # header-only pass
        self.assertEqual([p for _, p in reader.frames(with_payload=False)], [None] * 4)
        # iteration restarts
        self.assertEqual(len(list(reader)), 4)

    def test_sidecar_contents(self):
        path = write_capture(self.path("s.hcap"), mixed_frames(),
                             meta={"instance_id": 1, "config": "INI TEXT", "notes": "n", "source": "x"})
        with open(path + ".json") as f:
            doc = json.load(f)
        self.assertEqual(doc["format"], "hcap-1")
        self.assertEqual(doc["kind"], "post_sync_cf32")
        self.assertEqual(doc["frames"], 4)
        self.assertEqual(doc["instance_id"], 1)
        self.assertEqual(doc["config"], "INI TEXT")
        self.assertEqual(doc["notes"], "n")
        self.assertEqual(doc["source"], "x")
        self.assertEqual(doc["fw_version"], FW_VERSION)
        self.assertTrue(doc["host"])
        self.assertRegex(doc["created"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(hcap.HcapReader(path).meta, doc)
        self.assertEqual(hcap.HcapReader(path).kind, "post_sync_cf32")

    def test_sidecar_defaults_and_open_state(self):
        path = self.path("d.hcap")
        writer = hcap.HcapWriter(path, hcap.KIND_RAW_U8)
        # sidecar exists while recording (frames 0)
        with open(path + ".json") as f:
            doc = json.load(f)
        self.assertEqual(doc["frames"], 0)
        self.assertIsNone(doc["config"])
        self.assertIsNone(doc["instance_id"])
        self.assertIsNone(doc["notes"])
        writer.write(*make_frame(bits=8))
        self.assertFalse(writer.closed)
        writer.close()
        writer.close()  # idempotent
        self.assertTrue(writer.closed)
        self.assertEqual(hcap.read_meta(path)["frames"], 1)
        with self.assertRaises(hcap.HcapError):
            writer.write(*make_frame(bits=8))

    def test_raw_u8_round_trip(self):
        frames = mixed_frames(bits=8, samples=32)
        path = write_capture(self.path("r.hcap"), frames, kind=hcap.KIND_RAW_U8)
        got = list(hcap.HcapReader(path))
        self.assertEqual([p for _, p in got], [p for _, p in frames])
        self.assertEqual(len(got[1][1]), 2 * 32 * 2)

    def test_payload_length_rule(self):
        h = IQHeader()
        h.active_ant_chs, h.cpi_length, h.sample_bit_depth = 5, 100, 32
        self.assertEqual(hcap.payload_length(h), 5 * 100 * 8)
        h.sample_bit_depth = 8
        self.assertEqual(hcap.payload_length(h), 5 * 100 * 2)
        h.frame_type = IQHeader.FRAME_TYPE_DUMMY
        self.assertEqual(hcap.payload_length(h), 0)
        self.assertEqual(hcap.wire_payload_length(h), 5 * 100 * 2)
        h.sample_bit_depth = 12
        with self.assertRaises(hcap.HcapError):
            hcap.wire_payload_length(h)

    def test_writer_rejects_inconsistent_frames(self):
        with hcap.HcapWriter(self.path("w.hcap"), hcap.KIND_POST_SYNC_CF32) as writer:
            self._check_rejections(writer)
        self.assertEqual(hcap.read_meta(self.path("w.hcap"))["frames"], 0)

    def _check_rejections(self, writer):
        hb, pb = make_frame()
        with self.assertRaisesRegex(hcap.HcapError, "payload is"):
            writer.write(hb, pb[:-1])
        with self.assertRaisesRegex(hcap.HcapError, "sample_bit_depth"):
            writer.write(*make_frame(bits=8))
        bad = bytearray(hb)
        bad[0] ^= 0xFF
        with self.assertRaisesRegex(hcap.HcapError, "sync word"):
            writer.write(bytes(bad), pb)
        with self.assertRaisesRegex(hcap.HcapError, "1024"):
            writer.write(hb[:1000], pb)
        dummy_hb, _ = make_frame(IQHeader.FRAME_TYPE_DUMMY)
        with self.assertRaisesRegex(hcap.HcapError, "payload is"):
            writer.write(dummy_hb, b"\0" * 8)
        self.assertEqual(writer.frames, 0)

    def test_writer_zeroes_dummy_cpi_length(self):
        # a nonstandard source's DUMMY frame keeps its cpi_length on the wire
        h = IQHeader()
        h.decode_header(make_frame(cpi_index=7)[0])
        h.frame_type = IQHeader.FRAME_TYPE_DUMMY
        wire = h.encode_header()
        path = self.path("dummy.hcap")
        with hcap.HcapWriter(path, hcap.KIND_POST_SYNC_CF32) as writer:
            writer.write(wire, b"")
            with self.assertRaisesRegex(hcap.HcapError, "payload is"):
                writer.write(wire, bytes(hcap.wire_payload_length(h)))
        (stored, header, payload), = list(hcap.HcapReader(path).raw_frames())
        self.assertEqual((header.frame_type, header.cpi_length, payload), (IQHeader.FRAME_TYPE_DUMMY, 0, b""))
        h.cpi_length = 0
        self.assertEqual(stored, h.encode_header())  # only cpi_length changed
        # wrap of such a stream reads the wire payload and stays in sync
        h2 = IQHeader()
        h2.decode_header(wire)
        stream = wire + bytes(hcap.wire_payload_length(h2)) + b"".join(make_frame(cpi_index=8))
        n = hcap.wrap_stream(io.BytesIO(stream), self.path("w.hcap"), hcap.KIND_POST_SYNC_CF32)
        self.assertEqual(n, 2)
        got = [hd.cpi_length for hd, _ in hcap.HcapReader(self.path("w.hcap"))]
        self.assertEqual(got, [0, 16])

    def test_writer_rejects_bad_kind_and_meta(self):
        with self.assertRaises(ValueError):
            hcap.HcapWriter(self.path("k.hcap"), "cf64")
        with self.assertRaises(ValueError):
            hcap.HcapWriter(self.path("k.hcap"), hcap.KIND_RAW_U8, {"bogus": 1})
        with self.assertRaises(ValueError):
            hcap.HcapWriter(self.path("k.hcap"), hcap.KIND_RAW_U8, {"frames": 3})

    def test_reader_bad_sync_word(self):
        frames = mixed_frames()
        path = write_capture(self.path("b.hcap"), frames)
        offset = len(frames[0][0]) + len(frames[0][1]) + len(frames[1][0]) + len(frames[1][1])
        with open(path, "r+b") as f:
            f.seek(offset)
            f.write(b"\x00\x00\x00\x00")
        reader = hcap.HcapReader(path)
        it = iter(reader)
        next(it)
        next(it)
        with self.assertRaisesRegex(hcap.HcapError, r"frame 2 at byte offset {:d}".format(offset)):
            next(it)

    def test_reader_truncated(self):
        frames = mixed_frames()
        path = write_capture(self.path("t.hcap"), frames)
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.truncate(size - 5)
        with self.assertRaisesRegex(hcap.HcapError, "frame 3 .*truncated payload"):
            list(hcap.HcapReader(path))
        with self.assertRaisesRegex(hcap.HcapError, "truncated payload"):
            list(hcap.HcapReader(path).frames(with_payload=False))
        with open(path, "r+b") as f:
            f.truncate(1024 + 100)
        with self.assertRaisesRegex(hcap.HcapError, "frame 1 .*truncated header"):
            list(hcap.HcapReader(path))

    def test_reader_without_or_with_bad_sidecar(self):
        frames = mixed_frames()
        path = self.path("n.hcap")
        with open(path, "wb") as f:
            f.write(b"".join(h + p for h, p in frames))
        reader = hcap.HcapReader(path)
        self.assertIsNone(reader.meta)
        self.assertIsNone(reader.kind)
        self.assertEqual(len(list(reader)), 4)
        with open(path + ".json", "w") as f:
            json.dump({"format": "hcap-0", "kind": "raw_u8"}, f)
        with self.assertRaisesRegex(hcap.HcapError, "not an hcap-1 sidecar"):
            hcap.HcapReader(path)
        with open(path + ".json", "w") as f:
            f.write("{not json")
        with self.assertRaisesRegex(hcap.HcapError, "unreadable sidecar"):
            hcap.HcapReader(path)
        with self.assertRaisesRegex(hcap.HcapError, "not found"):
            hcap.HcapReader(self.path("missing.hcap"))

    def test_stream_helpers(self):
        frames = mixed_frames()
        stream = io.BytesIO(b"".join(h + p for h, p in frames))
        for hb, pb in frames:
            got = hcap.read_stream_frame(stream)
            self.assertEqual((got[0], got[2]), (hb, pb))
        self.assertIsNone(hcap.read_stream_frame(stream))
        with self.assertRaisesRegex(hcap.HcapError, "stream ended"):
            hcap.read_stream_frame(io.BytesIO(frames[1][0] + frames[1][1][:3]))

    def test_recv_exact_raises_connection_error(self):
        import socket
        a, b = socket.socketpair()
        with a, b:
            hb, pb = make_frame()
            a.sendall(hb + pb)
            got = hcap.recv_wire_frame(b)
            self.assertEqual((got[0], got[2]), (hb, pb))
            a.sendall(hb[:100])
            a.shutdown(socket.SHUT_WR)
            with self.assertRaises(ConnectionError):
                hcap.recv_wire_frame(b)


class TestCheckInfoCli(_TmpDirCase):

    def write_ini(self, num_ch=2, buffer_size=32):
        path = self.path("cfg.ini")
        with open(path, "w") as f:
            f.write("[hw]\nnum_ch = {:d}\n[daq]\ndaq_buffer_size = {:d}\n".format(num_ch, buffer_size))
        return path

    def run_tool(self, *args, stdin=None):
        return subprocess.run([sys.executable, CAPTURE_TOOL] + list(args), input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)

    def test_check_capture(self):
        raw = write_capture(self.path("raw.hcap"), mixed_frames(bits=8, samples=32), kind=hcap.KIND_RAW_U8)
        post = write_capture(self.path("post.hcap"), mixed_frames())
        self.assertEqual(hcap.check_capture(raw, hcap.KIND_RAW_U8), [])
        self.assertEqual(hcap.check_capture(raw, hcap.KIND_RAW_U8, self.write_ini(2, 32)), [])
        self.assertRegex(hcap.check_capture(post, hcap.KIND_RAW_U8)[0], "post_sync_cf32 capture, raw_u8 is required")
        self.assertRegex(hcap.check_capture(raw, hcap.KIND_RAW_U8, self.write_ini(4, 32))[0], "num_ch = 4")
        self.assertRegex(hcap.check_capture(raw, hcap.KIND_RAW_U8, self.write_ini(2, 64))[0],
                         "daq_buffer_size = 64")
        self.assertRegex(hcap.check_capture(raw, hcap.KIND_RAW_U8, self.path("nope.ini"))[0], "cannot read")
        os.remove(raw + ".json")
        self.assertRegex(hcap.check_capture(raw, hcap.KIND_RAW_U8)[0], "no sidecar")
        empty = write_capture(self.path("empty.hcap"), [], kind=hcap.KIND_RAW_U8)
        self.assertRegex(hcap.check_capture(empty, hcap.KIND_RAW_U8)[0], "no frames")

    def test_check_capture_non_integer_config(self):
        raw = write_capture(self.path("raw.hcap"), mixed_frames(bits=8, samples=32), kind=hcap.KIND_RAW_U8)
        ini = self.path("bad.ini")
        with open(ini, "w") as f:
            f.write("[hw]\nnum_ch = five\n[daq]\ndaq_buffer_size = 32\n")
        self.assertEqual(hcap.check_capture(raw, hcap.KIND_RAW_U8, ini),
                         ["config {}: [hw] num_ch is not an integer".format(ini)])
        with open(ini, "w") as f:
            f.write("[hw]\nnum_ch = 2\n[daq]\ndaq_buffer_size = 32k\n")
        self.assertRegex(hcap.check_capture(raw, hcap.KIND_RAW_U8, ini)[0],
                         r"\[daq\] daq_buffer_size is not an integer")
        # post_sync_cf32 does not use daq_buffer_size
        post = write_capture(self.path("post.hcap"), mixed_frames())
        self.assertEqual(hcap.check_capture(post, hcap.KIND_POST_SYNC_CF32, ini), [])
        res = self.run_tool("check", raw, "--kind", "raw_u8", "--config", ini)
        self.assertEqual(res.returncode, 2)
        self.assertNotIn(b"Traceback", res.stderr)

    def test_cli_wrap_tee_reader_gone(self):
        frames = [make_frame(bits=8, samples=16384, cpi_index=i) for i in range(4)]
        stream = b"".join(h + p for h, p in frames)
        out = self.path("tee.hcap")
        proc = subprocess.Popen([sys.executable, CAPTURE_TOOL, "wrap", "--kind", "raw_u8", "--tee", "-", out],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def feed():
            try:
                proc.stdin.write(stream)
            except BrokenPipeError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        self.assertEqual(len(proc.stdout.read(3000)), 3000)
        proc.stdout.close()
        err = proc.stderr.read()
        proc.stderr.close()
        self.assertEqual(proc.wait(TIMEOUT), 0, err)
        feeder.join(TIMEOUT)
        self.assertNotIn(b"BrokenPipeError", err)
        self.assertNotIn(b"Exception ignored", err)
        meta = hcap.read_meta(out)  # closed: the final frame count is written
        self.assertEqual(meta["frames"], len(list(hcap.HcapReader(out))))

    def test_cli_check_exit_codes(self):
        raw = write_capture(self.path("raw.hcap"), mixed_frames(bits=8, samples=32), kind=hcap.KIND_RAW_U8)
        post = write_capture(self.path("post.hcap"), mixed_frames())
        self.assertEqual(self.run_tool("check", raw, "--kind", "raw_u8").returncode, 0)
        res = self.run_tool("check", post, "--kind", "raw_u8")
        self.assertEqual(res.returncode, 2)
        self.assertIn(b"ERROR: ", res.stderr)
        self.assertEqual(self.run_tool("check", raw, "--kind", "raw_u8", "--config",
                                       self.write_ini(3, 32)).returncode, 2)

    def test_cli_info(self):
        post = write_capture(self.path("post.hcap"), mixed_frames(), meta={"config": "x" * 10})
        res = self.run_tool("info", post, "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        info = json.loads(res.stdout)
        self.assertEqual(info["meta"]["kind"], "post_sync_cf32")
        self.assertEqual(info["meta"]["config"], "<10 bytes>")
        st = info["stats"]
        self.assertEqual((st["frames"], st["data"], st["cal"], st["dummy"]), (4, 2, 1, 1))
        self.assertEqual((st["first_cpi_index"], st["last_cpi_index"]), (0, 3))
        self.assertEqual((st["channels"], st["cpi_length"], st["sampling_freq"]), (2, 16, 1000000))
        res = self.run_tool("info", post)
        self.assertEqual(res.returncode, 0)
        self.assertIn(b"kind    : post_sync_cf32", res.stdout)
        self.assertEqual(self.run_tool("info", self.path("missing.hcap")).returncode, 1)

    def test_cli_wrap_and_tee(self):
        frames = mixed_frames(bits=8, samples=32)
        stream = b"".join(h + p for h, p in frames)
        ini = self.write_ini(2, 32)
        out = self.path("wrapped.hcap")
        res = self.run_tool("wrap", "--kind", "raw_u8", "--tee", "--frames", "2",
                            "--instance-id", "3", "--config", ini, "--notes", "synth", "-", out,
                            stdin=stream)
        self.assertEqual(res.returncode, 0, res.stderr)
        # the tee passes the whole stream; the capture holds the first 2 frames
        self.assertEqual(res.stdout, stream)
        got = list(hcap.HcapReader(out).raw_frames())
        self.assertEqual([(h, p) for h, _, p in got], frames[:2])
        meta = hcap.read_meta(out)
        self.assertEqual((meta["kind"], meta["frames"], meta["instance_id"], meta["notes"]),
                         ("raw_u8", 2, 3, "synth"))
        with open(ini) as f:
            self.assertEqual(meta["config"], f.read())
        self.assertEqual(meta["source"], "stdin")

        # from a file, all frames, no tee
        src = self.path("stream.bin")
        with open(src, "wb") as f:
            f.write(stream)
        out2 = self.path("all.hcap")
        res = self.run_tool("wrap", "--kind", "raw_u8", src, out2)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, b"")
        with open(out2, "rb") as f:
            self.assertEqual(f.read(), stream)
        # wrong kind for the stream's bit depth
        res = self.run_tool("wrap", "--kind", "post_sync_cf32", src, self.path("bad.hcap"))
        self.assertEqual(res.returncode, 1)
        self.assertIn(b"sample_bit_depth", res.stderr)

    def test_wrap_stream_api(self):
        frames = mixed_frames()
        tee = io.BytesIO()
        n = hcap.wrap_stream(io.BytesIO(b"".join(h + p for h, p in frames)), self.path("x.hcap"),
                             hcap.KIND_POST_SYNC_CF32, tee=tee, max_frames=1)
        self.assertEqual(n, 1)
        self.assertEqual(tee.getvalue(), b"".join(h + p for h, p in frames))
        n = hcap.wrap_stream(io.BytesIO(b"".join(h + p for h, p in frames)), self.path("y.hcap"),
                             hcap.KIND_POST_SYNC_CF32, max_frames=2)
        self.assertEqual(n, 2)
        self.assertEqual(hcap.read_meta(self.path("y.hcap"))["frames"], 2)


@unittest.skipIf(np is None, "numpy not installed")
class TestAnalyzer(_TmpDirCase):

    def setUp(self):
        super().setUp()
        import iq_analyzer
        self.analyzer = iq_analyzer

    def test_load_hcap_cf32(self):
        samples = (np.arange(2 * 16) + 1j * np.arange(2 * 16)).astype(np.complex64).reshape(2, 16)
        hb, _ = make_frame(samples=16, sampling_freq=2000000, cpi_index=5)
        dummy = make_frame(IQHeader.FRAME_TYPE_DUMMY)
        path = write_capture(self.path("a.hcap"), [dummy, (hb, samples.tobytes())])
        header, iq = self.analyzer.load_frame(path)
        self.assertEqual(header.cpi_index, 5)
        np.testing.assert_array_equal(iq, samples)
        header, iq = self.analyzer.load_frame(path, 1)
        np.testing.assert_array_equal(iq, samples)
        with self.assertRaisesRegex(ValueError, "no samples"):
            self.analyzer.load_frame(path, 0)
        with self.assertRaisesRegex(ValueError, "no frame 7"):
            self.analyzer.load_frame(path, 7)
        self.assertEqual(self.analyzer.resolve_sample_rate(None, header), 2000000.0)
        self.assertEqual(self.analyzer.resolve_sample_rate(1e6, header), 1e6)
        header.sampling_freq = 0
        self.assertEqual(self.analyzer.resolve_sample_rate(None, header), self.analyzer.fs)

    def test_load_hcap_raw_u8(self):
        hb, pb = make_frame(bits=8, samples=4, channels=2)
        path = write_capture(self.path("u.hcap"), [(hb, pb)], kind=hcap.KIND_RAW_U8)
        _, iq = self.analyzer.load_frame(path)
        raw = (np.frombuffer(pb, dtype=np.uint8).reshape(2, 4, 2).astype(np.float32) - 127.5) / 127.5
        np.testing.assert_allclose(iq, raw[:, :, 0] + 1j * raw[:, :, 1], rtol=1e-6)
        self.assertEqual(iq.dtype, np.complex64)

    def test_load_legacy_iqf(self):
        samples = np.ones((2, 16), dtype=np.complex64)
        hb, _ = make_frame(samples=16)
        path = self.path("VEGAM_0.iqf")
        with open(path, "wb") as f:
            f.write(hb + samples.tobytes() + bytes(2 ** 18))
        header, iq = self.analyzer.load_frame(path)
        np.testing.assert_array_equal(iq, samples)

    def test_cli_fs_default_is_header(self):
        captured = {}

        def fake_analyze(**kwargs):
            captured.update(kwargs)
        orig = self.analyzer.analyze
        self.analyzer.analyze = fake_analyze
        try:
            self.analyzer.main(["x.hcap", "--frame", "3"])
        finally:
            self.analyzer.analyze = orig
        self.assertIsNone(captured["sample_rate"])
        self.assertEqual(captured["frame_index"], 3)


@unittest.skipIf(np is None, "numpy not installed")
class TestCaptureShmemStream(_TmpDirCase):
    """capture_shmem_stream.IQFrameRecorder over a real FIFO + shm loopback."""

    def setUp(self):
        super().setUp()
        os.makedirs(self.path("_data_control"))
        self.prev_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.addCleanup(os.chdir, self.prev_cwd)
        self.name = "utcap_{:s}".format(uuid.uuid4().hex[:12])
        os.mkfifo(join("_data_control", "fw_" + self.name))
        os.mkfifo(join("_data_control", "bw_" + self.name))

    def _record(self, out_name, frames, kind):
        from shmemIface import outShmemIface
        from capture_shmem_stream import IQFrameRecorder
        holder = {}

        def build():
            holder["prod"] = outShmemIface(self.name, 1024 + 4096)
        t = threading.Thread(target=build)
        t.start()
        recorder = IQFrameRecorder(self.name, self.path(out_name), "1", kind)
        t.join(TIMEOUT)
        prod = holder["prod"]
        self.addCleanup(prod.destory_sm_buffer)
        self.assertTrue(recorder.in_shmem_iface.init_ok)
        recorder.start()
        for hb, pb in frames:
            idx = prod.wait_buff_free()
            self.assertIn(idx, (0, 1))
            buf = prod.buffers[idx]
            buf[:1024] = np.frombuffer(hb, dtype=np.uint8)
            buf[1024:1024 + len(pb)] = np.frombuffer(pb, dtype=np.uint8)
            prod.send_ctr_buff_ready(idx)
        # TERMINATE is queued behind the last buffer-ready byte (FIFO order)
        prod.send_ctr_terminate()
        recorder.join(TIMEOUT)
        self.assertFalse(recorder.is_alive())
        return self.path(out_name)

    def test_hcap_output(self):
        # odd u8 payload (not a whole number of float32) is captured exactly
        frames = [make_frame(IQHeader.FRAME_TYPE_DUMMY, bits=8),
                  make_frame(bits=8, samples=3, channels=1, cpi_index=1),
                  make_frame(bits=8, samples=3, channels=1, cpi_index=2)]
        path = self._record("cap.hcap", frames, hcap.KIND_RAW_U8)
        got = list(hcap.HcapReader(path).raw_frames())
        self.assertEqual([(h, p) for h, _, p in got], frames)
        meta = hcap.read_meta(path)
        self.assertEqual((meta["kind"], meta["frames"], meta["source"]), ("raw_u8", 3, "shmem:" + self.name))

    def test_plain_output_unchanged(self):
        frames = [make_frame(cpi_index=1), make_frame(cpi_index=2)]
        path = self._record("cap.dat", frames, None)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"".join(h + p for h, p in frames))
        self.assertFalse(os.path.exists(path + ".json"))

    def test_hcap_wrong_kind_exits_and_closes(self):
        from shmemIface import outShmemIface
        from capture_shmem_stream import IQFrameRecorder
        holder = {}
        t = threading.Thread(target=lambda: holder.setdefault("prod", outShmemIface(self.name, 1024 + 4096)))
        t.start()
        path = self.path("wrong.hcap")
        recorder = IQFrameRecorder(self.name, path, "1", hcap.KIND_POST_SYNC_CF32)
        t.join(TIMEOUT)
        prod = holder["prod"]
        self.addCleanup(prod.destory_sm_buffer)
        recorder.start()
        hb, pb = make_frame(bits=8, samples=3, channels=1, cpi_index=1)  # u8 into a cf32 capture
        idx = prod.wait_buff_free()
        buf = prod.buffers[idx]
        buf[:1024] = np.frombuffer(hb, dtype=np.uint8)
        buf[1024:1024 + len(pb)] = np.frombuffer(pb, dtype=np.uint8)
        prod.send_ctr_buff_ready(idx)
        recorder.join(TIMEOUT)
        self.assertFalse(recorder.is_alive())  # no TERMINATE needed
        self.assertTrue(recorder.hcap_writer.closed)
        self.assertEqual(hcap.read_meta(path)["frames"], 0)

    def test_hcap_requires_kind(self):
        from capture_shmem_stream import IQFrameRecorder
        with self.assertRaisesRegex(ValueError, "capture kind"):
            IQFrameRecorder(self.name, self.path("x.hcap"), "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
