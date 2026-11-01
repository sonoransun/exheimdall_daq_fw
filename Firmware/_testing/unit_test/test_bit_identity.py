"""End-to-end bit identity of the safe throughput levers (spec I.4, J.1 WP-17b).

Sudo lane (``./unit_test.sh --sudo``, CI ``pipeline`` job): Linux, root or
passwordless sudo, built ``rebuffer.out``/``decimate.out``, numpy, scipy,
pyzmq, taskset.

Deterministic settings (I.4 "How bit-identity is proven"): ``en_iq_cal = 0``,
``cal_track_mode = 0``, ``[daq] en_noise_source_ctr = 0`` and the synthesizer
run with ``--seed 0 --zero-delays --blocks 400``, so no calibration burst, no
``s``/``n`` command and no dummy frame depends on real-time timing. The
pipeline is the data path of ``daq_synthetic_start.sh`` composed by the test
so that nothing else is timing dependent:

    test_data_synthesizer.py | rebuffer.out 0 -> decimate.out 0 ->
    [taskset -c CORES] delay_sync.py -> capture_shmem_stream.py (delay_sync_iq)
                                     -> capture_shmem_stream.py (delay_sync_hwc, drained)

``decimate.out 0`` runs the decimator with back-pressure (the start scripts
use drop mode): every CPI reaches delay_sync, so frames are only ever lost at
delay_sync's own drop-mode output. hw_controller is not started (it would
send ``g``/``n`` commands at timing-dependent frames); its delay_sync_hwc
stream is drained by a second capture. ``[pre_processing] en_filter_reset =
1`` keeps a decimating FIR stateless across frames.

Frames are captured from ``delay_sync_iq`` with ``capture_shmem_stream.py``
(each frame is hashed instead of stored), matched by ``cpi_index`` from the
first DATA frame on, and compared byte for byte except ``time_stamp`` for:
levers off vs ``fft_workers = 1``, ``delay_sync_cores`` widened,
``skip_copy_on_drop = 1`` and ``size_segments_from_config = 1``.

``delay_sync_cores`` is applied the way the launchers apply it
(``taskset -c <delay_sync_cores>``): the reference run pins delay_sync to one
core (the launchers' default core 2), the widened run to every core. The
``skip_copy_on_drop`` run uses a capture that stalls periodically, so
delay_sync really drops output buffers and takes the no-copy path.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_synthetic_chain as chain  # noqa: E402

BLOCKS = 400
#: a stalling consumer holds every STALL_EVERY-th frame for STALL_FACTOR x the
#: mean frame interval (at least STALL_MIN_SEC), so delay_sync fills its other
#: buffer and then finds none free: an output drop on any host speed
STALL_EVERY = 10
STALL_FACTOR = 5.0
STALL_MIN_SEC = 0.3
RUN_TIMEOUT = 900
FIFOS = tuple("%s_%s" % (d, s) for s in chain.SHM_STREAMS for d in ("fw", "bw"))

#: capture_shmem_stream.IQFrameRecorder with an output that records
#: {cpi, type, sha256 of the frame with time_stamp zeroed} per frame
CAPTURE = r"""
import hashlib, json, os, sys, time
sys.path.insert(0, os.path.join("_testing", "unit_test"))
import capture_shmem_stream as css
name, out, en_write, ts_off, stall_every, stall_factor, stall_min = sys.argv[1:8]
ts_off, stall_every = int(ts_off), int(stall_every)
stall_factor, stall_min = float(stall_factor), float(stall_min)

class FrameDigests(object):
    def __init__(self, path):
        self.f = open(path, "w")
        self.n = 0
        self.last = None
        self.intervals = []
    def write(self, frame):
        header = bytearray(frame[:1024])
        hdr = css.IQHeader()
        hdr.decode_header(bytes(header))
        header[ts_off:ts_off + 8] = bytes(8)
        digest = hashlib.sha256(bytes(header) + frame[1024:]).hexdigest()
        self.f.write(json.dumps({"cpi": hdr.cpi_index, "type": hdr.frame_type, "sha": digest}) + "\n")
        self.f.flush()
        self.n += 1
        now = time.monotonic()
        if self.last is not None:
            self.intervals.append(now - self.last)
        self.last = now
        if stall_every and self.n % stall_every == 0:
            mean = sum(self.intervals) / len(self.intervals) if self.intervals else 0.0
            time.sleep(max(stall_min, stall_factor * mean))
            self.last = None  # the interval across a stall is not the frame rate
    def close(self):
        self.f.close()

rec = css.IQFrameRecorder(name, os.devnull, en_write)
if not rec.in_shmem_iface.init_ok:
    sys.exit(1)
rec.output_file.close()
rec.output_file = FrameDigests(out)
rec.run()
"""


class TestBitIdentity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if chain.WHY_NOT:
            raise unittest.SkipTest(chain.WHY_NOT)
        if shutil.which("taskset") is None:
            raise unittest.SkipTest("pipeline lane: needs taskset")
        if chain.daq_pids():
            raise unittest.SkipTest("another DAQ chain is running on this host")
        cls.box = chain.Sandbox("bitid")
        cls.base_ini = os.path.join(cls.box.root, "reference.ini")
        shutil.copy2(cls.box.ini, cls.base_ini)
        chain.set_ini(cls.base_ini, {"daq": {"en_noise_source_ctr": "0"},
                                     "calibration": {"en_iq_cal": "0", "cal_track_mode": "0"},
                                     "pre_processing": {"en_filter_reset": "1"},
                                     "federation": {"instance_id": "0", "port_stride": "100"}})
        cfg = chain.read_ini(cls.base_ini)
        blocks_per_cpi = max(1, cfg.getint("pre_processing", "cpi_size") * cfg.getint(
            "pre_processing", "decimation_ratio") // cfg.getint("daq", "daq_buffer_size"))
        cls.expected_frames = BLOCKS // blocks_per_cpi
        cls.ncpu = os.cpu_count() or 1
        cls.pin = str(2 % cls.ncpu)
        cls.runs = 0
        cls.reference = cls.run_pipeline({}, cores=cls.pin)

    @classmethod
    def tearDownClass(cls):
        cls.box.cleanup()

    # -- the pipeline ----------------------------------------------------

    @classmethod
    def run_pipeline(cls, keys, cores, stall=False):
        """Run the deterministic pipeline once with *keys* set on the reference
        INI; {cpi_index: (frame_type, sha256)} of the delay_sync_iq frames."""
        box = cls.box
        cls.runs += 1
        tag = "run%d" % cls.runs
        fw = box.fw
        shutil.copy2(cls.base_ini, box.ini)
        if keys:
            chain.set_ini(box.ini, keys)
        logs = os.path.join(fw, "_logs", tag)
        os.makedirs(logs)

        def checked(*argv):
            rc, out = box.sh(fw, *argv)
            if rc != 0:
                raise AssertionError("%s failed (%d):\n%s" % (" ".join(argv), rc, out))

        checked(sys.executable, "ini_checker.py", "no_hw")
        dc = os.path.join(fw, "_data_control")
        for name in FIFOS:
            path = os.path.join(dc, name)
            if os.path.lexists(path):
                os.unlink(path)
            os.mkfifo(path)
        checked(sys.executable, "fir_filter_designer.py")

        def launch(argv, log):
            with open(os.path.join(logs, log), "w") as err:
                return subprocess.Popen(argv, cwd=fw, env=box.env, stdin=subprocess.DEVNULL,
                                        stdout=err, stderr=subprocess.STDOUT)

        digests = os.path.join(logs, "delay_sync_iq.jsonl")
        procs = [
            launch(["sh", "-c", "%s _testing/test_data_synthesizer.py --seed 0 --zero-delays --blocks %d "
                    "2>%s | _daq_core/rebuffer.out 0" % (sys.executable, BLOCKS,
                                                          os.path.join(logs, "synthetic.log"))],
                   "rebuffer.log"),
            launch(["_daq_core/decimate.out", "0"], "decimator.log"),
            launch(["taskset", "-c", cores, sys.executable, "_daq_core/delay_sync.py"], "delay_sync.log"),
        ]
        capture_iq = launch([sys.executable, "-c", CAPTURE, "delay_sync_iq", digests, "1",
                             str(chain.TIME_STAMP_OFFSET), str(STALL_EVERY if stall else 0), str(STALL_FACTOR),
                             str(STALL_MIN_SEC)],
                            "capture_iq.log")
        capture_hwc = launch([sys.executable, "-c", CAPTURE, "delay_sync_hwc", os.devnull, "0",
                              str(chain.TIME_STAMP_OFFSET), "0", "0", "0"], "capture_hwc.log")
        procs += [capture_iq, capture_hwc]
        try:
            capture_iq.wait(timeout=RUN_TIMEOUT)
            for p in procs:
                p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            raise AssertionError("pipeline run %s did not finish within %d s\n%s"
                                 % (tag, RUN_TIMEOUT, cls.tail_logs(logs)))
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()
        if capture_iq.returncode != 0:
            raise AssertionError("capture of delay_sync_iq exited %d\n%s"
                                 % (capture_iq.returncode, cls.tail_logs(logs)))
        frames = {}
        with open(digests) as f:
            for line in f:
                rec = json.loads(line)
                frames[rec["cpi"]] = (rec["type"], rec["sha"])
        if not frames:
            raise AssertionError("no frame captured in run %s\n%s" % (tag, cls.tail_logs(logs)))
        return frames

    @staticmethod
    def tail_logs(logs):
        out = []
        for name in sorted(os.listdir(logs)):
            if name.endswith(".log"):
                with open(os.path.join(logs, name), errors="replace") as f:
                    out.append("==> %s <==\n%s" % (name, f.read()[-1500:]))
        return "\n".join(out)

    # -- comparison --------------------------------------------------------

    def assert_identical(self, frames, min_overlap):
        ref = self.reference
        data_from = min(c for c, (t, _) in ref.items() if t == chain.IQHeader.FRAME_TYPE_DATA)
        common = sorted(c for c in frames if c in ref and c >= data_from)
        self.assertGreaterEqual(len(common), min_overlap,
                                "only %d frames in common with the reference (%d / %d captured)"
                                % (len(common), len(frames), len(ref)))
        differ = [c for c in common if frames[c] != ref[c]]
        self.assertEqual(differ, [], "%d of %d matched frames differ (cpi_index %s...)"
                         % (len(differ), len(common), differ[:10]))

    def test_0_reference_is_complete(self):
        """The back-pressured reference run delivers every CPI, all DATA frames."""
        ref = self.reference
        self.assertEqual(sorted(ref), list(range(self.expected_frames)),
                         "the reference run lost frames (a fast consumer must see every CPI)")
        self.assertEqual({t for t, _ in ref.values()}, {chain.IQHeader.FRAME_TYPE_DATA})
        self.assertEqual(len({s for _, s in ref.values()}), len(ref), "identical digests of distinct frames")

    def test_fft_workers_1(self):
        frames = self.run_pipeline({"offload": {"fft_workers": "1", "xcorr_workers": "1"}}, cores=self.pin)
        self.assert_identical(frames, min_overlap=self.expected_frames)

    def test_delay_sync_cores_widened(self):
        cores = ",".join(str(c) for c in range(self.ncpu))
        frames = self.run_pipeline({"host": {"delay_sync_cores": cores}}, cores=cores)
        self.assert_identical(frames, min_overlap=self.expected_frames)

    def test_skip_copy_on_drop(self):
        frames = self.run_pipeline({"offload": {"skip_copy_on_drop": "1"}}, cores=self.pin, stall=True)
        cpis = sorted(frames)
        dropped = (cpis[-1] - cpis[0] + 1) - len(cpis)
        self.assertGreater(dropped, 0, "the stalling capture caused no delay_sync output drop; "
                                       "the no-copy path was not exercised")
        self.assert_identical(frames, min_overlap=min(20, self.expected_frames // 4))

    def test_size_segments_from_config(self):
        frames = self.run_pipeline({"pre_processing": {"size_segments_from_config": "1"}}, cores=self.pin)
        self.assert_identical(frames, min_overlap=self.expected_frames)


if __name__ == "__main__":
    unittest.main()
