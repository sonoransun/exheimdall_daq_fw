"""
    Description :
    Hardware-free tests of the synthetic IQ source (_testing/test_data_synthesizer.py):

    * importing the module generates nothing (the generator runs from main());
    * the ZMQ control responder (ZmqCtrThread) answers "ok" to every
      inter-module message packer, sets the dummy-frame flag, toggles the noise
      source and feeds 's' to the sampling-clock model;
    * the generated stream: header-only DUMMY frames after a command, CAL
      frames while the noise source is on, a clean exit on 'h', sample slips
      visible in the cross-correlation, unit_id selection, --blocks / --pace;
    * 5-channel output identity: with --seed 0 the first three frames hash
      to the values produced by the pre-change synthesizer run with
      numpy.random.seed(0) applied externally (header time_stamp masked);
    * a closed calibration loop over ZMQ (delay_sync's delay estimate and
      tuning expressions driving the running synthesizer) reaches zero delay.

    ZMQ tests are skipped without pyzmq. Every port is ephemeral.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from configparser import ConfigParser
from os.path import join, dirname, realpath

import numpy as np

current_path = dirname(realpath(__file__))
testing_path = dirname(current_path)
root_path = dirname(testing_path)
sys.path.insert(0, join(root_path, "_daq_core"))
sys.path.insert(0, testing_path)

import inter_module_messages as imm  # noqa: E402
from iq_header import IQHeader  # noqa: E402

try:
    import zmq
except ImportError:
    zmq = None

SYNTH = join(testing_path, "test_data_synthesizer.py")
HEADER_SIZE = 1024

# SHA-256 of the first three frames of the PRE-CHANGE synthesizer with
# numpy.random.seed(0) applied externally:
#   python3 -c "import numpy, runpy; numpy.random.seed(0);
#               runpy.run_path('_testing/test_data_synthesizer.py', run_name='__main__')"
# run with REFERENCE_INI as daq_chain_config.ini; the 8-byte header
# time_stamp (wall clock) is zeroed before hashing.
REFERENCE_INI = """[hw]
num_ch = 5
unit_id = 0
[daq]
log_level = 5
daq_buffer_size = 262144
center_freq = 700000000
sample_rate = 2400000
[pre_processing]
cpi_size = 1048576
decimation_ratio = 1
"""
REFERENCE_FRAME_SHA256 = (
    "055dffd9ae4cad393c217c7c514e50f30df0436af83fa73d948e4f3be8fffe80",
    "cf1760caa14657905b161cb76fa86aed611a7ff050fdf775b1f4f56ab5e84add",
    "c4bf97b4d137df273c9621ea47c9448486e10f8085c53f028bf04ca4e7f6676b",
)

# delay_sync constants and expressions (delay_sync.py, STATE_SAMPLE_CAL)
MIN_FS_PPM_OFFSET = 1e-7
MAX_FS_PPM_OFFSET = 0.01
INT_FS_TUNE_GAIN = np.array([[100, 2, 0], [50, 25, 15]])


def _time_stamp_slice():
    """Byte range of the header time_stamp field (found by encoding)."""
    h = IQHeader()
    h.time_stamp = 0
    a = h.encode_header()
    h.time_stamp = 0x0102030405060708
    b = h.encode_header()
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return slice(diff[0], diff[-1] + 1)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_ini(directory, text, ctrl_port):
    """Write daq_chain_config.ini whose control port is ctrl_port
    (instance_id 1, port_stride = ctrl_port - 1130)."""
    with open(join(directory, "daq_chain_config.ini"), "w") as f:
        f.write(text)
        f.write("[federation]\ninstance_id = 1\nport_stride = {0:d}\n".format(ctrl_port - 1130))


def _small_ini(num_ch=4, daq_buffer_size=8192, sample_rate=2400000, hw_extra="", log_level=5):
    return ("[hw]\nnum_ch = {0}\nunit_id = 0\n{1}"
            "[daq]\nlog_level = {4:d}\ndaq_buffer_size = {2}\ncenter_freq = 700000000\n"
            "sample_rate = {3}\n"
            "[pre_processing]\ncpi_size = 1048576\ndecimation_ratio = 1\n").format(
                num_ch, hw_extra, daq_buffer_size, sample_rate, log_level)


class FrameReader:
    """Reads (IQHeader, payload) frames from the synthesizer's stdout."""

    def __init__(self, stream):
        self.stream = stream

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.stream.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def read(self):
        raw = self._read_exact(HEADER_SIZE)
        if raw is None:
            return None, None, None
        h = IQHeader()
        h.decode_header(raw)
        payload = b""
        if h.cpi_length > 0:
            payload = self._read_exact(h.cpi_length * h.active_ant_chs * 2)
        return h, payload, raw


def _iq(payload, h):
    u8 = np.frombuffer(payload, dtype=np.uint8).reshape(h.active_ant_chs, h.cpi_length * 2)
    x = u8.astype(np.float64) - 127.5
    return x[:, 0::2] + 1j * x[:, 1::2]


def measure_delays(iq, n_proc):
    """delay_sync's integer delay estimate (verbatim expressions): correlate
    x_padd = [x, 0] with y_padd = [0, y]; delays[m] = N_proc - peak_index."""
    zeros = np.zeros(n_proc, dtype=np.complex64)
    x_fft = np.fft.fft(np.concatenate([iq[0, 0:n_proc], zeros]))
    delays = [0] * iq.shape[0]
    for m in range(1, iq.shape[0]):
        y_fft = np.fft.fft(np.concatenate([zeros, iq[m, 0:n_proc]]))
        corr = np.abs(np.fft.ifft(x_fft.conj() * y_fft)) ** 2
        delays[m] = int(n_proc - np.argmax(corr))
    return delays


def int_fs_offsets(delays):
    """delay_sync's integer-stage tuning expression (verbatim)."""
    out = [0.0] * len(delays)
    for m, d in enumerate(delays):
        gain = (INT_FS_TUNE_GAIN[1, (INT_FS_TUNE_GAIN[0, :] <= abs(d))])[0]
        v = -1 * d * gain * MIN_FS_PPM_OFFSET
        if abs(v) > MAX_FS_PPM_OFFSET:
            v = np.sign(v) * MAX_FS_PPM_OFFSET
        out[m] = float(v)
    return out


class _SynthProcess:
    """Runs the synthesizer in a private temp cwd."""

    def __init__(self, ini_text, args=(), ctrl_port=None):
        self.dir = tempfile.mkdtemp(prefix="synth_ctl_")
        self.port = ctrl_port if ctrl_port is not None else _free_port()
        _write_ini(self.dir, ini_text, self.port)
        self.stderr = open(join(self.dir, "synthetic.log"), "wb")
        self.proc = subprocess.Popen([sys.executable, SYNTH] + list(args), cwd=self.dir,
                                     stdout=subprocess.PIPE, stderr=self.stderr)
        self.reader = FrameReader(self.proc.stdout)

    def log(self):
        self.stderr.flush()
        with open(join(self.dir, "synthetic.log"), "rb") as f:
            return f.read().decode(errors="replace")

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=30)
        self.proc.stdout.close()
        self.stderr.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    BIND_ATTEMPTS = 3

    @classmethod
    def with_responder(cls, ini_text, args=()):
        """
            Start a synthesizer whose ZMQ control responder is known to be bound.
            The free port is chosen by bind-and-close, so another process can take
            it before the child binds; the child then only logs an ERROR and keeps
            generating. The responder is created before the first frame is written,
            so once a frame arrives the bind outcome is in the log: on
            'unavailable' retry on a fresh port. ini_text must log at
            log_level <= 4 so the ERROR line is emitted.
        """
        for _ in range(cls.BIND_ATTEMPTS):
            sp = cls(ini_text, args)
            h, _, _ = sp.reader.read()
            if h is None:
                log = sp.log()
                sp.close()
                raise AssertionError("synthesizer produced no frame:\n" + log)
            if "unavailable" not in sp.log():
                return sp
            sp.close()
        raise AssertionError("control responder could not bind a free port in {0:d} attempts".format(
            cls.BIND_ATTEMPTS))


class TestImport(unittest.TestCase):

    def test_import_generates_nothing(self):
        """Importing the synthesizer (no INI, no args) writes nothing, reads
        no config and exits cleanly."""
        d = tempfile.mkdtemp(prefix="synth_import_")
        try:
            code = ("import sys; sys.path.insert(0, {0!r}); import test_data_synthesizer as t; "
                    "assert callable(t.main); assert callable(t.ZmqCtrThread)").format(testing_path)
            res = subprocess.run([sys.executable, "-c", code], cwd=d, capture_output=True, timeout=60)
            self.assertEqual(res.returncode, 0, res.stderr.decode(errors="replace"))
            self.assertEqual(res.stdout, b"")
            self.assertEqual(os.listdir(d), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_missing_config_exits_1(self):
        d = tempfile.mkdtemp(prefix="synth_noini_")
        try:
            res = subprocess.run([sys.executable, SYNTH], cwd=d, capture_output=True, timeout=60)
            self.assertEqual(res.returncode, 1)
            self.assertEqual(res.stdout, b"")
            self.assertIn(b"configuration file not found", res.stderr)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_negative_blocks_is_usage_error(self):
        res = subprocess.run([sys.executable, SYNTH, "--blocks", "-1"], capture_output=True, timeout=60)
        self.assertEqual(res.returncode, 2)
        self.assertEqual(res.stdout, b"")

    def test_out_of_range_seed_is_usage_error(self):
        """numpy accepts seeds in [0, 2**32 - 1]; anything else is rejected by
        argparse (exit 2) before any INI read or socket bind."""
        for seed in ("-1", str(2**32)):
            res = subprocess.run([sys.executable, SYNTH, "--seed", seed], capture_output=True, timeout=60)
            self.assertEqual(res.returncode, 2, seed)
            self.assertEqual(res.stdout, b"")
            self.assertIn(b"--seed", res.stderr)
            self.assertNotIn(b"Traceback", res.stderr)

    def test_seed_bounds_accepted(self):
        import test_data_synthesizer as synth
        ap = synth.build_arg_parser()
        self.assertEqual(ap.parse_args(["--seed", "0"]).seed, 0)
        self.assertEqual(ap.parse_args(["--seed", str(2**32 - 1)]).seed, 2**32 - 1)


class TestConfigHelpers(unittest.TestCase):

    def setUp(self):
        import test_data_synthesizer as synth
        self.synth = synth

    def _parser(self, text):
        p = ConfigParser()
        p.read_string(text)
        return p

    def test_zmq_endpoint(self):
        ep = self.synth.zmq_endpoint
        self.assertEqual(ep(self._parser("[daq]\n")), ("*", 1130))
        self.assertEqual(ep(self._parser("[daq]\nzmq_listen_address =\n[federation]\ninstance_id = 2\n")),
                         ("*", 1330))
        self.assertEqual(ep(self._parser("[daq]\nzmq_listen_address = 127.0.0.1\n"
                                         "[federation]\ninstance_id = 3\nport_stride = 10\n")),
                         ("127.0.0.1", 1160))

    def test_resolve_unit_id(self):
        r = self.synth.resolve_unit_id
        self.assertEqual(r(self._parser("[hw]\nunit_id = 7\n"), False), 0)
        self.assertEqual(r(self._parser("[hw]\nunit_id = 7\n"), True), 7)
        auto = self._parser("[hw]\nunit_id = auto\n[federation]\ninstance_id = 4\n")
        self.assertEqual(r(auto, False), 4)
        self.assertEqual(r(auto, True), 4)
        self.assertEqual(r(self._parser("[hw]\n"), True), 0)
        with self.assertRaises(ValueError):
            r(self._parser("[hw]\nunit_id = x\n"), True)


class TestSynthControlMessages(unittest.TestCase):
    """SynthControl.handle_message applies every command (no sockets)."""

    def setUp(self):
        import test_data_synthesizer as synth
        self.ctl = synth.SynthControl(5, 2**18)

    def test_every_command_requests_dummy_frames(self):
        msgs = [
            imm.pack_msg_reconfiguration(2, 100000000, 2400000, 0),
            imm.pack_msg_rf_tune(2, 433000000),
            imm.pack_msg_set_gain(2, [1, 2, 3, 4, 5]),
            imm.pack_msg_enable_agc(2),
            imm.pack_msg_noise_source_ctr(2, True),
            imm.pack_msg_sample_freq_tune(2, [0.0] * 5),
            imm.pack_msg_set_bias_tee(2, [1] * 5),
            bytes([2]) + b"x" + bytes(126),  # unknown command: still a dummy burst (rtl_daq)
        ]
        for msg in msgs:
            self.assertEqual(self.ctl.handle_message(msg), b"ok")
            self.assertTrue(self.ctl.take_dummy_request())
            self.assertFalse(self.ctl.take_dummy_request(), "request is consumed once")

    def test_state_updates(self):
        c = self.ctl
        c.handle_message(imm.pack_msg_rf_tune(2, 433920000))
        self.assertEqual(c.rf_center_freq, 433920000)
        c.handle_message(imm.pack_msg_set_gain(2, [0, 87, 297, 496, 144]))
        self.assertEqual(list(c.gains), [0, 87, 297, 496, 144])
        c.handle_message(imm.pack_msg_enable_agc(2))
        self.assertEqual(c.agc, 1)
        c.handle_message(imm.pack_msg_noise_source_ctr(2, True))
        self.assertEqual(c.noise_source_state, 1)
        c.handle_message(imm.pack_msg_noise_source_ctr(2, False))
        self.assertEqual(c.noise_source_state, 0)
        self.assertIsNone(c.bias_tee_flags)
        c.handle_message(imm.pack_msg_set_bias_tee(2, [1, 0, 1, 0, 1]))
        self.assertEqual(c.bias_tee_flags, 0b10101)
        c.handle_message(imm.pack_msg_sample_freq_tune(2, [0.0, 2.5e-5, 0.0, 0.0, 0.0]))
        self.assertTrue(c.fs_model.active)
        self.assertAlmostEqual(c.fs_model.values[1], 2.5e-5, places=10)
        # 2.5e-5 * 2^18 = 6.55 samples -> slip -6 for the late channel
        self.assertEqual(c.fs_slip_block(), [0, -6, 0, 0, 0])

    def test_reconfiguration_is_ignored(self):
        c = self.ctl
        before = (c.rf_center_freq, list(c.gains))
        with self.assertLogs("test_data_synthesizer", level="WARNING") as cm:
            self.assertEqual(c.handle_message(imm.pack_msg_reconfiguration(2, 1, 2, 3)), b"ok")
        self.assertIn("deprecated, ignored", "\n".join(cm.output))
        self.assertEqual((c.rf_center_freq, list(c.gains)), before)

    def test_halt_sets_exit_request(self):
        self.assertEqual(self.ctl.handle_message(bytes([2]) + b"h" + bytes(126)), b"ok")
        self.assertTrue(self.ctl.exit_request)

    def test_malformed_message(self):
        with self.assertLogs("test_data_synthesizer", level="ERROR"):
            self.assertEqual(self.ctl.handle_message(b"c" * 10), b"err")
        self.assertFalse(self.ctl.take_dummy_request())


@unittest.skipIf(zmq is None, "pyzmq not installed")
class TestZmqCtrThread(unittest.TestCase):

    def setUp(self):
        import test_data_synthesizer as synth
        self.synth = synth
        self.ctr = synth.ZmqCtrThread(5, 2**18, 0, "127.0.0.1")
        self.ctr.start()
        self.ctx = zmq.Context()
        self.req = self.ctx.socket(zmq.REQ)
        self.req.setsockopt(zmq.RCVTIMEO, 5000)
        self.req.setsockopt(zmq.LINGER, 0)
        self.req.connect("tcp://127.0.0.1:{0:d}".format(self.ctr.port))

    def tearDown(self):
        self.req.close(linger=0)
        self.ctx.term()
        self.ctr.stop(timeout=5)

    def _txn(self, msg):
        self.req.send(msg)
        return self.req.recv()

    def test_answers_ok_for_every_packer(self):
        msgs = [
            imm.pack_msg_reconfiguration(2, 100000000, 2400000, 0),
            imm.pack_msg_rf_tune(2, 433000000),
            imm.pack_msg_set_gain(2, [10] * 5),
            imm.pack_msg_enable_agc(2),
            imm.pack_msg_noise_source_ctr(2, True),
            imm.pack_msg_noise_source_ctr(2, False),
            imm.pack_msg_sample_freq_tune(2, [0.0, 1e-5, 0.0, 3e-5, 0.0]),
            imm.pack_msg_set_bias_tee(2, [0, 1, 0, 0, 0]),
        ]
        for msg in msgs:
            self.assertEqual(self._txn(msg), b"ok")
            self.assertTrue(self.ctr.take_dummy_request())
        self.assertEqual(self.ctr.rf_center_freq, 433000000)
        self.assertEqual(list(self.ctr.gains), [10] * 5)
        self.assertEqual(self.ctr.noise_source_state, 0)
        self.assertEqual(self.ctr.bias_tee_flags, 0b00010)
        self.assertTrue(self.ctr.fs_model.active)

    def test_noise_toggle_and_fs_model(self):
        self.assertEqual(self._txn(imm.pack_msg_noise_source_ctr(2, True)), b"ok")
        self.assertEqual(self.ctr.noise_source_state, 1)
        self.assertEqual(self._txn(imm.pack_msg_noise_source_ctr(2, False)), b"ok")
        self.assertEqual(self.ctr.noise_source_state, 0)
        self.assertEqual(self._txn(imm.pack_msg_sample_freq_tune(2, [0.0, 2.5e-5, 0, 0, 0])), b"ok")
        self.assertEqual(self.ctr.fs_slip_block(), [0, -6, 0, 0, 0])
        self.assertEqual(self.ctr.fs_slip_block(), [0, 0, 0, 0, 0])  # reset after one block

    def test_malformed_reply_keeps_lockstep(self):
        self.assertEqual(self._txn(b"short"), b"err")
        self.assertEqual(self._txn(imm.pack_msg_enable_agc(2)), b"ok")

    def test_halt_ends_thread(self):
        self.assertEqual(self._txn(bytes([2]) + b"h" + bytes(126)), b"ok")
        self.ctr.join(timeout=5)
        self.assertFalse(self.ctr.is_alive())
        self.assertTrue(self.ctr.exit_request)

    def test_bind_conflict_raises(self):
        with self.assertRaises(zmq.ZMQError):
            self.synth.ZmqCtrThread(5, 1024, self.ctr.port, "127.0.0.1")

    def test_stop_unstarted_releases_port(self):
        other = self.synth.ZmqCtrThread(2, 1024, 0, "localhost")
        port = other.port
        other.stop()
        again = self.synth.ZmqCtrThread(2, 1024, port, "127.0.0.1")
        again.stop()


class TestGeneratedStream(unittest.TestCase):
    """The synthesizer as a process (as daq_synthetic_start.sh runs it)."""

    def test_reference_identity_seed0(self):
        """--seed 0 at otherwise default flags reproduces the pre-change
        synthesizer's first three frames byte for byte (time_stamp masked)."""
        sp = _SynthProcess(REFERENCE_INI, ["--seed", "0"])
        try:
            ts = _time_stamp_slice()
            frame_len = HEADER_SIZE + 5 * 262144 * 2
            for k in range(3):
                buf = sp.reader._read_exact(frame_len)
                self.assertIsNotNone(buf, sp.log())
                masked = bytearray(buf)
                masked[ts] = bytes(ts.stop - ts.start)
                self.assertEqual(hashlib.sha256(bytes(masked)).hexdigest(), REFERENCE_FRAME_SHA256[k],
                                 "frame {0} differs from the pre-change synthesizer".format(k))
        finally:
            sp.close()

    def test_blocks_and_default_header(self):
        """--blocks N writes exactly N frames and exits 0; header fields as
        before (unit_id 0 even with an integer [hw] unit_id, no bias slot)."""
        sp = _SynthProcess(_small_ini(num_ch=2, hw_extra="").replace("unit_id = 0", "unit_id = 7"),
                           ["--blocks", "3", "--zero-delays"])
        try:
            frames = []
            while True:
                h, payload, _ = sp.reader.read()
                if h is None:
                    break
                frames.append((h, payload))
            self.assertEqual(sp.proc.wait(timeout=60), 0, sp.log())
            self.assertEqual(len(frames), 3)
            for b, (h, payload) in enumerate(frames):
                self.assertEqual(h.sync_word, IQHeader.SYNC_WORD)
                self.assertEqual(h.daq_block_index, b)
                self.assertEqual(h.frame_type, IQHeader.FRAME_TYPE_DATA)
                self.assertEqual(h.cpi_length, 8192)
                self.assertEqual(h.unit_id, 0)
                self.assertEqual(h.hardware_id.rstrip("\x00"), "K2")
                self.assertEqual(h.get_bias_tee_state(), 0)
                self.assertEqual(len(payload), 2 * 8192 * 2)
        finally:
            sp.close()

    def test_any_channel_count(self):
        """Channel tables exist for every M up to 31 (the historical script
        indexed 5-entry tables and failed for M > 5)."""
        for M in (1, 8, 31):
            sp = _SynthProcess(_small_ini(num_ch=M, daq_buffer_size=1024), ["--blocks", "2"])
            try:
                out = sp.proc.stdout.read()
                self.assertEqual(sp.proc.wait(timeout=60), 0, sp.log())
                self.assertEqual(len(out), 2 * (HEADER_SIZE + M * 1024 * 2), "M={0}".format(M))
            finally:
                sp.close()

    def _first_header(self, ini, args):
        sp = _SynthProcess(ini, list(args) + ["--blocks", "1"])
        try:
            h, _, _ = sp.reader.read()
            self.assertIsNotNone(h, sp.log())
            return h
        finally:
            sp.close()

    def test_unit_id_selection(self):
        ini7 = _small_ini(num_ch=2).replace("unit_id = 0", "unit_id = 7")
        self.assertEqual(self._first_header(ini7, ["--unit-id-from-config"]).unit_id, 7)
        ini_auto = _small_ini(num_ch=2).replace("unit_id = 0", "unit_id = auto")
        self.assertEqual(self._first_header(ini_auto, []).unit_id, 1)  # instance_id 1

    def test_pace(self):
        """--pace emits blocks at daq_buffer_size / sample_rate (0.1 s here)."""
        sp = _SynthProcess(_small_ini(num_ch=2, daq_buffer_size=4096, sample_rate=40960),
                           ["--pace", "--blocks", "5"])
        try:
            t0 = None
            for _ in range(5):
                h, _, _ = sp.reader.read()
                self.assertIsNotNone(h, sp.log())
                if t0 is None:
                    t0 = time.monotonic()
            elapsed = time.monotonic() - t0
            self.assertEqual(sp.proc.wait(timeout=60), 0)
            self.assertGreaterEqual(elapsed, 0.3)
        finally:
            sp.close()

    @unittest.skipIf(zmq is None, "pyzmq not installed")
    def test_control_dummy_cal_slip_and_halt(self):
        """Over ZMQ: 'n' on -> 5 header-only DUMMY frames then CAL frames;
        's' slips channel 1 by the modelled number of samples; 'h' -> exit 0."""
        B = 8192
        sp = _SynthProcess.with_responder(_small_ini(num_ch=2, daq_buffer_size=B, log_level=4), ["--blocks", "0"])
        ctx = zmq.Context()
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 20000)
        req.setsockopt(zmq.LINGER, 0)
        req.connect("tcp://127.0.0.1:{0:d}".format(sp.port))
        try:
            h, _, _ = sp.reader.read()
            self.assertIsNotNone(h, sp.log())
            req.send(imm.pack_msg_noise_source_ctr(2, True))
            self.assertEqual(req.recv(), b"ok")

            def next_cal_after_dummies():
                seen_dummy = 0
                while True:
                    h, payload, _ = sp.reader.read()
                    self.assertIsNotNone(h, sp.log())
                    if h.frame_type == IQHeader.FRAME_TYPE_DUMMY:
                        self.assertEqual(h.cpi_length, 0)
                        self.assertEqual(payload, b"")
                        self.assertEqual(h.data_type, 0)
                        self.assertEqual(h.noise_source_state, 1)  # stamped on dummies, as rtl_daq
                        seen_dummy += 1
                    elif seen_dummy:
                        return h, payload, seen_dummy

            h, payload, n_dummy = next_cal_after_dummies()
            self.assertEqual(n_dummy, 5)
            self.assertEqual(h.frame_type, IQHeader.FRAME_TYPE_CAL)
            self.assertEqual(h.noise_source_state, 1)
            self.assertEqual(h.cpi_length, B)
            self.assertEqual(measure_delays(_iq(payload, h), B), [0, -10])

            # 4.5 samples in one block (below the keep limit): slip -4, residual 0.5
            v = 4.5 / B
            req.send(imm.pack_msg_sample_freq_tune(2, [0.0, v]))
            self.assertEqual(req.recv(), b"ok")
            h, payload, n_dummy = next_cal_after_dummies()
            self.assertEqual(n_dummy, 5)
            self.assertEqual(measure_delays(_iq(payload, h), B), [0, -6])

            req.send(bytes([2]) + b"h" + bytes(126))
            self.assertEqual(req.recv(), b"ok")
            while sp.reader.read()[0] is not None:
                pass
            self.assertEqual(sp.proc.wait(timeout=30), 0, sp.log())
        finally:
            req.close(linger=0)
            ctx.term()
            sp.close()

    @unittest.skipIf(zmq is None, "pyzmq not installed")
    def test_closed_loop_over_zmq_converges(self):
        """delay_sync's measurement + integer tuning expressions driving the
        running synthesizer through the real ZMQ port, with the
        STATE_SYNC_WAIT hold (wait for a dummy, measure the next CAL frame):
        every channel reaches delay 0 (delays[1]=10, delays[3]=30)."""
        B = 2**18
        n_proc = 2**16
        sp = _SynthProcess.with_responder(_small_ini(num_ch=4, daq_buffer_size=B, log_level=4),
                                          ["--blocks", "0", "--seed", "1"])
        ctx = zmq.Context()
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 20000)
        req.setsockopt(zmq.LINGER, 0)
        req.connect("tcp://127.0.0.1:{0:d}".format(sp.port))
        try:
            req.send(imm.pack_msg_noise_source_ctr(2, True))
            self.assertEqual(req.recv(), b"ok")
            wait_dummy = True
            history = []
            for _ in range(200):  # frames, dummies included
                h, payload, _ = sp.reader.read()
                self.assertIsNotNone(h, sp.log())
                if h.frame_type == IQHeader.FRAME_TYPE_DUMMY:
                    wait_dummy = False
                    continue
                if wait_dummy or h.frame_type != IQHeader.FRAME_TYPE_CAL:
                    continue
                delays = measure_delays(_iq(payload, h), n_proc)
                history.append(delays)
                if all(abs(d) < 1 for d in delays):
                    break
                req.send(imm.pack_msg_sample_freq_tune(2, int_fs_offsets(delays)))
                self.assertEqual(req.recv(), b"ok")
                wait_dummy = True
            self.assertEqual(history[0], [0, -10, 0, -30])
            self.assertEqual(history[-1], [0, 0, 0, 0], "no convergence: {0}".format(history))
        finally:
            req.close(linger=0)
            ctx.term()
            sp.close()


if __name__ == "__main__":
    unittest.main()
