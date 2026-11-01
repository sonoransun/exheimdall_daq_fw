"""End-to-end bit identity of the safe throughput levers (spec I.4, J.1 WP-17b).

Sudo lane (``./unit_test.sh --sudo``, CI ``pipeline`` job): Linux, root or
passwordless sudo, built ``rebuffer.out``/``decimate.out``, numpy, scipy,
pyzmq, taskset.

Deterministic input. Every run feeds the same byte stream: the importable
synthesizer (WP-10) with ``--seed 0 --zero-delays --blocks 400`` and its
noise source driven by a fixed CPI schedule instead of ZMQ ``n`` commands
(``ScheduledNoiseSource``): CAL frames for the first ``CAL_LEAD`` CPIs, then
one CAL frame every ``CAL_PERIOD`` CPIs. Each run records the sha256 of its
input stream (``time_stamp`` zeroed) and the test requires it to equal the
reference's. No command reaches the synthesizer, so no dummy frame and no
sample slip depends on real-time timing.

Calibration path. With that stream delay_sync leaves STATE_INIT on the first
CAL frame, calibrates (STATE_SAMPLE_CAL / STATE_FRAC_SAMPLE_CAL: the xcorr
and FFT engines), locks on the first DATA frame (calc_iq_sync) and, with
``cal_track_mode = 2``, runs calc_iq_sync on every later CAL frame in
STATE_TRACK. ``[monitoring] stamp_cal_telemetry = 1`` stamps the result of
the last check (header slot 110, cal quality in mdB) on every outbound frame,
so the frame digests cover the DSP results and not only the copied payload.
``en_iq_cal = 0`` because ``skip_copy_on_drop`` only applies without IQ
correction; ``[daq] en_noise_source_ctr = 0``. The reference must show the
calibration states, tracked CAL frames and changing cal-quality values
(test_0), so a stream that never left STATE_INIT fails instead of passing
vacuously.

The pipeline is the data path of ``daq_synthetic_start.sh`` composed by the
test so that nothing else is timing dependent:

    scheduled synthesizer | rebuffer.out 0 -> decimate.out 0 ->
    [taskset -c CORES] delay_sync.py -> capture_shmem_stream.py (delay_sync_iq)
                                     -> capture_shmem_stream.py (delay_sync_hwc, drained)

``decimate.out 0`` runs the decimator with back-pressure (the start scripts
use drop mode): every CPI reaches delay_sync, so delay_sync's FSM sees the
same frame sequence in every run and frames are only ever lost at its own
drop-mode output. hw_controller is not started (it would send ``g``/``n``
commands at timing-dependent frames); its delay_sync_hwc stream is drained by
a second capture. ``[pre_processing] en_filter_reset = 1`` keeps a decimating
FIR stateless across frames.

The input sizes are those of the sudo lane (``config_files/unit_test_k4``,
one CAL or DATA frame per DAQ block); test_0 also checks every reference
frame type against a model of rebuffer.out's frame assembly for the
configured sizes.

Frames are captured from ``delay_sync_iq`` with ``capture_shmem_stream.py``
(each frame is hashed instead of stored), matched by ``cpi_index`` and
compared byte for byte except ``time_stamp`` for: levers off vs
``fft_workers = 1``, ``delay_sync_cores`` widened, ``skip_copy_on_drop = 1``
and ``size_segments_from_config = 1``.

``delay_sync_cores`` is applied the way the launchers apply it
(``taskset -c <delay_sync_cores>``): the reference run pins delay_sync to one
of the CPUs this process may use (the launchers' default core 2 when
allowed), the widened run to all of them. The ``skip_copy_on_drop`` run uses
a capture that stalls periodically, so delay_sync really drops output
buffers; the test requires a delivered frame whose cal quality was computed
on a dropped, tracked CAL frame, i.e. by calc_iq_sync on the aliased input
of the no-copy path.
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
#: noise-source schedule of the input stream, in CPIs: CAL for the first
#: CAL_LEAD CPIs (calibration and lock), then one CAL CPI every CAL_PERIOD
#: (checked by calc_iq_sync in STATE_TRACK with cal_track_mode = 2)
CAL_LEAD = 8
CAL_PERIOD = 3
#: a stalling consumer holds every STALL_EVERY-th frame for STALL_FACTOR x the
#: mean frame interval (at least STALL_MIN_SEC), so delay_sync fills its other
#: buffer and then finds none free: an output drop on any host speed
STALL_EVERY = 10
STALL_FACTOR = 5.0
STALL_MIN_SEC = 0.3
RUN_TIMEOUT = 900
#: share of the CPIs a run with a non-stalling capture must deliver: the
#: capture hashes every frame and can fall behind delay_sync's drop-mode
#: output now and then on a loaded host
MIN_DELIVERED = 0.9
#: STATUS / header sync_state of STATE_SAMPLE_CAL and STATE_TRACK
SYNC_SAMPLE_CAL = 2
SYNC_TRACK = 6
FIFOS = tuple("%s_%s" % (d, s) for s in chain.SHM_STREAMS for d in ("fw", "bw"))

#: the synthesizer with its noise source on a fixed per-block schedule (argv:
#: "1"/"0" per DAQ block); writes the frames to stdout and the sha256 of the
#: stream (time_stamp zeroed) to a file
SOURCE = r"""
import hashlib, os, struct, sys
sys.path.insert(0, "_testing")
import test_data_synthesizer as synth
schedule, ts_off, digest_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
blocks = len(schedule)
SYNC = struct.pack("I", synth.IQHeader.SYNC_WORD)

class ScheduledNoiseSource(synth.SynthControl):
    # SynthControl without the ZMQ responder; the generation loop calls
    # fs_slip_block() once per DAQ block before it reads noise_source_state
    def __init__(self, M, B):
        super().__init__(M, B)
        self.block = -1
    def fs_slip_block(self):
        self.block += 1
        self.noise_source_state = int(schedule[self.block])
        return super().fs_slip_block()

sources = []
def make_source(M, B, port, bind_addr="*"):
    sources.append(ScheduledNoiseSource(M, B))
    return sources[-1]
synth.ZmqCtrThread = make_source

class HashingOut(object):
    def __init__(self, raw):
        self.raw = raw
        self.sha = hashlib.sha256()
    def write(self, data):
        self.raw.write(data)
        if len(data) == 1024 and data[:4] == SYNC:
            data = bytearray(data)
            data[ts_off:ts_off + 8] = bytes(8)
        self.sha.update(data)
    def flush(self):
        self.raw.flush()

out = HashingOut(sys.stdout.buffer)
rc = synth.main(["--seed", "0", "--zero-delays", "--blocks", str(blocks)], out=out)
if rc != 0 or len(sources) != 1 or sources[0].block + 1 != blocks:
    sys.stderr.write("scheduled source failed: rc %d, %d control objects, %s blocks\n"
                     % (rc, len(sources), sources[0].block + 1 if sources else "no"))
    sys.exit(1)
with open(digest_path, "w") as f:
    f.write(out.sha.hexdigest())
"""

#: capture_shmem_stream.IQFrameRecorder with an output that records
#: {cpi, type, sync_state, cal (slot 110), sha256 of the frame with
#: time_stamp zeroed} per frame
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
        self.f.write(json.dumps({"cpi": hdr.cpi_index, "type": hdr.frame_type, "sync": hdr.sync_state,
                                 "cal": hdr.get_cal_quality_mdb(), "sha": digest}) + "\n")
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


def noise_schedule(blocks_per_cpi):
    """Noise-source state per DAQ block ("1" = CAL block): on for the first
    CAL_LEAD CPIs, then for every CAL_PERIOD-th CPI."""
    return "".join("1" if b // blocks_per_cpi < CAL_LEAD or (b // blocks_per_cpi) % CAL_PERIOD == 0 else "0"
                   for b in range(BLOCKS))


def rebuffer_frame_types(schedule, block, cpi, cal):
    """Frame type of each frame rebuffer.out emits for *schedule*: per input
    block at most one frame, a CAL frame of *cal* samples or a DATA frame of
    *cpi* samples once that much input is buffered (rebuffer.c)."""
    available, types = 0, []
    for state in schedule:
        available += block
        if state == "1" and available >= cal:
            types.append(chain.IQHeader.FRAME_TYPE_CAL)
            available -= cal
        elif state == "0" and available >= cpi:
            types.append(chain.IQHeader.FRAME_TYPE_DATA)
            available -= cpi
    return types


class TestBitIdentity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if chain.WHY_NOT:
            raise unittest.SkipTest(chain.WHY_NOT)
        if shutil.which("taskset") is None:
            raise unittest.SkipTest("pipeline lane: needs taskset")
        chain.refuse_running_chain()
        cls.box = chain.Sandbox("bitid")
        cls.base_ini = os.path.join(cls.box.root, "reference.ini")
        shutil.copy2(cls.box.ini, cls.base_ini)
        chain.set_ini(cls.base_ini, {"daq": {"en_noise_source_ctr": "0"},
                                     "calibration": {"en_iq_cal": "0", "cal_track_mode": "2"},
                                     "pre_processing": {"en_filter_reset": "1"},
                                     "monitoring": {"stamp_cal_telemetry": "1"},
                                     "federation": {"instance_id": "0", "port_stride": "100"}})
        cfg = chain.read_ini(cls.base_ini)
        block = cfg.getint("daq", "daq_buffer_size")
        cpi = cfg.getint("pre_processing", "cpi_size") * cfg.getint("pre_processing", "decimation_ratio")
        cls.schedule = noise_schedule(max(1, cpi // block))
        cls.expected_types = rebuffer_frame_types(cls.schedule, block, cpi,
                                                  cfg.getint("calibration", "corr_size"))
        cls.expected_frames = len(cls.expected_types)
        # the CPUs this process may run on (a cpuset-restricted runner or
        # container need not include CPU 0..n-1)
        cls.cpus = sorted(os.sched_getaffinity(0))
        cls.pin = str(cls.cpus[2 % len(cls.cpus)])
        cls.runs = 0
        try:
            cls.reference, cls.input_sha = cls.run_pipeline({}, cores=cls.pin)
        except BaseException:
            cls.box.cleanup(check=False)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.box.cleanup()

    # -- the pipeline ----------------------------------------------------

    @classmethod
    def run_pipeline(cls, keys, cores, stall=False):
        """Run the deterministic pipeline once with *keys* set on the reference
        INI. Returns ({cpi_index: frame record} of the delay_sync_iq frames,
        sha256 of the input stream)."""
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
        input_digest = os.path.join(logs, "input.sha256")
        source = subprocess.list2cmdline([sys.executable, "-c", SOURCE, cls.schedule,
                                          str(chain.TIME_STAMP_OFFSET), input_digest])
        procs = [
            launch(["sh", "-c", "%s 2>%s | _daq_core/rebuffer.out 0"
                    % (source, os.path.join(logs, "synthetic.log"))], "rebuffer.log"),
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
        if not os.path.isfile(input_digest):
            raise AssertionError("the scheduled source of run %s failed\n%s" % (tag, cls.tail_logs(logs)))
        with open(input_digest) as f:
            input_sha = f.read().strip()
        frames = {}
        with open(digests) as f:
            for line in f:
                rec = json.loads(line)
                frames[rec["cpi"]] = rec
        if not frames:
            raise AssertionError("no frame captured in run %s\n%s" % (tag, cls.tail_logs(logs)))
        return frames, input_sha

    @staticmethod
    def tail_logs(logs):
        out = []
        for name in sorted(os.listdir(logs)):
            if name.endswith(".log"):
                with open(os.path.join(logs, name), errors="replace") as f:
                    out.append("==> %s <==\n%s" % (name, f.read()[-1500:]))
        return "\n".join(out)

    # -- comparison --------------------------------------------------------

    def run_variant(self, keys, cores=None, stall=False):
        frames, input_sha = self.run_pipeline(keys, cores=cores or self.pin, stall=stall)
        self.assertEqual(input_sha, self.input_sha, "the input stream differs from the reference's")
        return frames

    def assert_identical(self, frames, min_overlap):
        ref = self.reference
        common = sorted(c for c in frames if c in ref)
        self.assertGreaterEqual(len(common), min_overlap,
                                "only %d frames in common with the reference (%d / %d captured)"
                                % (len(common), len(frames), len(ref)))
        differ = [c for c in common if frames[c]["sha"] != ref[c]["sha"]]
        self.assertEqual(differ, [], "%d of %d matched frames differ (cpi_index %s...)"
                         % (len(differ), len(common), differ[:10]))

    def full_overlap(self):
        """Frames two non-stalling runs must have in common (each may miss
        1 - MIN_DELIVERED of the CPIs)."""
        return int((2 * MIN_DELIVERED - 1) * self.expected_frames)

    def tracked_cal(self, cpi):
        """True when the reference shows *cpi* as a CAL frame checked in
        STATE_TRACK (calc_iq_sync), None when the reference lacks it."""
        rec = self.reference.get(cpi)
        if rec is None:
            return None
        return rec["type"] == chain.IQHeader.FRAME_TYPE_CAL and rec["sync"] == SYNC_TRACK

    def test_0_reference_is_complete(self):
        """The back-pressured reference run delivers the CPIs (bar a few
        output drops) with distinct contents, and its FSM ran the calibration
        and the tracking DSP: the identity runs compare computed results."""
        ref = self.reference
        self.assertTrue(set(ref) <= set(range(self.expected_frames)), sorted(ref))
        wrong = [c for c in sorted(ref) if ref[c]["type"] != self.expected_types[c]]
        self.assertEqual(wrong, [], "frame types differ from the noise-source schedule")
        self.assertGreaterEqual(len(ref), int(MIN_DELIVERED * self.expected_frames),
                                "the reference run lost %d of %d CPIs" % (self.expected_frames - len(ref),
                                                                          self.expected_frames))
        self.assertEqual(len({r["sha"] for r in ref.values()}), len(ref), "identical digests of distinct frames")
        states = sorted({r["sync"] for r in ref.values()})
        self.assertTrue({SYNC_SAMPLE_CAL, SYNC_TRACK} <= set(states),
                        "the FSM did not calibrate and track: sync states %s" % states)
        tracked = [c for c in ref if self.tracked_cal(c)]
        self.assertGreaterEqual(len(tracked), self.expected_frames // (4 * CAL_PERIOD),
                                "too few CAL frames checked in STATE_TRACK: %d" % len(tracked))
        quality = {ref[c]["cal"] for c in tracked}
        self.assertGreater(len(quality), 1, "slot 110 does not follow the tracking checks: %s" % sorted(quality))
        self.assertNotIn(0, quality, "slot 110 not stamped on tracked CAL frames")

    def test_fft_workers_1(self):
        frames = self.run_variant({"offload": {"fft_workers": "1", "xcorr_workers": "1"}})
        self.assert_identical(frames, min_overlap=self.full_overlap())

    def test_delay_sync_cores_widened(self):
        cores = ",".join(str(c) for c in self.cpus)
        frames = self.run_variant({"host": {"delay_sync_cores": cores}}, cores=cores)
        self.assert_identical(frames, min_overlap=self.full_overlap())

    def test_skip_copy_on_drop(self):
        frames = self.run_variant({"offload": {"skip_copy_on_drop": "1"}}, stall=True)
        cpis = sorted(frames)
        dropped = (cpis[-1] - cpis[0] + 1) - len(cpis)
        self.assertGreater(dropped, 0, "the stalling capture caused no delay_sync output drop; "
                                       "the no-copy path was not exercised")
        dropped_tracked = [c for c in range(cpis[0], cpis[-1]) if c not in frames and self.tracked_cal(c)]
        self.assertTrue(dropped_tracked, "no dropped frame was a CAL frame checked in STATE_TRACK; "
                                         "calc_iq_sync never ran on the no-copy input")
        # A tracked DATA frame whose latest CAL frame was dropped carries (slot
        # 110) the result calc_iq_sync computed on that frame's aliased input
        def frame_type(c):
            rec = self.reference.get(c) or frames.get(c)
            return None if rec is None else rec["type"]

        witnesses = []
        for d in cpis:
            if frames[d]["type"] != chain.IQHeader.FRAME_TYPE_DATA or frames[d]["sync"] != SYNC_TRACK:
                continue
            c = d - 1
            while c >= 0 and frame_type(c) == chain.IQHeader.FRAME_TYPE_DATA:
                c -= 1
            if c >= 0 and c not in frames and self.tracked_cal(c):
                witnesses.append((c, d))
        self.assertTrue(witnesses, "no delivered frame carries a tracking result computed on a dropped "
                                   "frame; the no-copy input was not consumed by the DSP")
        self.assert_identical(frames, min_overlap=min(20, self.expected_frames // 4))

    def test_size_segments_from_config(self):
        frames = self.run_variant({"pre_processing": {"size_segments_from_config": "1"}})
        self.assert_identical(frames, min_overlap=self.full_overlap())


if __name__ == "__main__":
    unittest.main()
