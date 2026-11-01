"""
    Description :
    delay_sync FSM harness: the real delaySynchronizer (built from a
    temporary daq_chain_config.ini) runs its own start() loop over scripted
    synthetic frames through fake shared-memory interfaces and a fake rtl_daq
    control link. No FIFOs, shared memory, C binaries or sudo.

    Covers the calibration-robustness keys (spec I.8, addendum WP-22) and
    the tracking-decimation lever (spec I.4 #3):
      * defaults (keys absent) and explicit default values give byte-identical
        outbound headers, payloads and rtl_daq messages; an overrun leaves
        the sync flags untouched unless invalidate_on_overrun = 1
      * [calibration] track_decimation: k = 1 is today's trajectory; k = 3
        checks every 3rd tracking frame and reuses the last flags in between
      * [calibration] amplitude_tolerance_mode = symmetric catches a gain
        imbalance the legacy one-sided test misses
      * [calibration] cal_timeout_frames: one event / WARNING / DB record per
        calibration run, no hardware action
      * [calibration] invalidate_on_overrun / frames_after_overrun
      * malformed values fall back to the default with a WARNING

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import logging
import os
import sys
import tempfile
import types
import unittest
from os.path import join, dirname, realpath

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)


def _install_numba_stub_if_missing():
    try:
        import numba  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("numba")

    def _decorator(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def wrap(func):
            return func
        return wrap

    stub.jit = _decorator
    stub.njit = _decorator
    sys.modules["numba"] = stub


def _install_zmq_stub_if_missing():
    try:
        import zmq  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("zmq")
    stub.REQ = 3
    stub.LINGER = 17
    stub.RCVTIMEO = 27
    stub.SNDTIMEO = 28
    stub.ZMQError = type("ZMQError", (Exception,), {})
    stub.Context = type("Context", (), {"socket": lambda self, *a: None,
                                        "term": lambda self: None})
    sys.modules["zmq"] = stub


_install_numba_stub_if_missing()
_install_zmq_stub_if_missing()

import delay_sync  # noqa: E402
from iq_header import IQHeader  # noqa: E402

M = 4
N = 4096
CAL = IQHeader.FRAME_TYPE_CAL
DATA = IQHeader.FRAME_TYPE_DATA
DUMMY = IQHeader.FRAME_TYPE_DUMMY

BASE_INI = {
    "meta": {"ini_version": "8"},
    "hw": {"num_ch": str(M)},
    "daq": {"log_level": "4", "center_freq": "433000000", "sample_rate": "2400000"},
    "pre_processing": {"cpi_size": str(N), "decimation_ratio": "1"},
    "calibration": {"corr_size": str(N), "std_ch_ind": "0", "amplitude_tolerance": "2",
                    "phase_tolerance": "2", "cal_track_mode": "1",
                    "maximum_sync_fails": "3", "amplitude_cal_mode": "channel_power",
                    "en_iq_cal": "0", "iq_adjust_source": "explicit-time-delay",
                    "iq_adjust_amplitude": "0,0,0", "iq_adjust_time_delay_ns": "0,0,0"},
}

# Every key of this package's FSM levers, at its documented default value
EXPLICIT_DEFAULTS = {
    "calibration": {"track_decimation": "1", "amplitude_tolerance_mode": "legacy",
                    "cal_timeout_frames": "0", "invalidate_on_overrun": "0",
                    "frames_after_overrun": "1", "en_wire_recal": "0"},
    "offload": {"fft_workers": "4", "xcorr_workers": "4", "skip_copy_on_drop": "0"},
    "monitoring": {"status_extended": "0", "status_update_interval": "0",
                   "stamp_cal_telemetry": "0", "en_hwc_state_relay": "0"},
    "federation": {"stamp_node_id": "0"},
}


def ini_text(overrides=None):
    sections = {k: dict(v) for k, v in BASE_INI.items()}
    for section, keys in (overrides or {}).items():
        sections.setdefault(section, {}).update(keys)
    lines = []
    for section, keys in sections.items():
        lines.append("[{}]".format(section))
        lines.extend("{} = {}".format(k, v) for k, v in keys.items())
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Synthetic frames
# ----------------------------------------------------------------------

class Signals(object):
    """Deterministic multichannel noise: correlated (aligned) or not."""

    def __init__(self, seed=7):
        self.rng = np.random.default_rng(seed)

    def noise(self, n):
        return self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)

    def frame(self, amps=None, phases_deg=None, correlated=True, delays=None):
        base = self.noise(N + 64)
        x = np.zeros((M, N), dtype=np.complex64)
        for m in range(M):
            b = base if correlated else self.noise(N + 64)
            d = delays[m] if delays else 0
            s = b[32 - d:32 - d + N]
            a = (amps[m] if amps else 1.0) * np.exp(1j * np.deg2rad(phases_deg[m] if phases_deg else 0.0))
            x[m] = (a * s).astype(np.complex64)
        return x


def encode_frame(frame_type, cpi, data=None, freq=433000000, overruns=0):
    h = IQHeader()
    h.frame_type = frame_type
    h.active_ant_chs = M
    h.cpi_length = 0 if frame_type == DUMMY else N
    h.sample_bit_depth = 32
    h.cpi_index = cpi
    h.daq_block_index = 3 * cpi
    h.rf_center_freq = freq
    h.time_stamp = 1000 + cpi
    h.header_version = 8
    h.set_buffer_overrun_cnt(overruns)
    payload = b"" if (data is None or frame_type == DUMMY) else data.tobytes()
    return h.encode_header() + payload


class FrameScript(object):
    def __init__(self, seed=7):
        self.sig = Signals(seed)
        self.frames = []

    def add(self, frame_type, count=1, overruns=0, freq=433000000, **signal_kwargs):
        for _ in range(count):
            data = None if frame_type == DUMMY else self.sig.frame(**signal_kwargs)
            self.frames.append(encode_frame(frame_type, len(self.frames), data, freq=freq,
                                            overruns=overruns))
        return self

    def to_lock(self):
        """INIT -> SAMPLE_CAL -> FRAC -> IQ_CAL -> TRACK_LOCK -> TRACK."""
        self.add(CAL, 4)
        self.add(DATA, 1)
        return self


# ----------------------------------------------------------------------
# Fake interfaces
# ----------------------------------------------------------------------

class FakeIn(object):
    def __init__(self, frames):
        size = max(len(f) for f in frames)
        self.buffers = [np.zeros(size, np.uint8), np.zeros(size, np.uint8)]
        self.frames = frames
        self.i = 0

    def wait_buff_free(self):
        if self.i >= len(self.frames):
            return delay_sync.TERMINATE
        idx = self.i % 2
        f = self.frames[self.i]
        self.buffers[idx][:] = 0
        self.buffers[idx][:len(f)] = np.frombuffer(f, np.uint8)
        self.i += 1
        return idx

    def send_ctr_buff_ready(self, idx):
        pass


class FakeOut(object):
    def __init__(self, size):
        self.buffers = [np.zeros(size, np.uint8), np.zeros(size, np.uint8)]
        self.sent = []
        self.dropped_frame_cntr = 0
        self.n = 0

    def wait_buff_free(self):
        self.n += 1
        return (self.n - 1) % 2

    def send_ctr_buff_ready(self, idx):
        self.sent.append(bytes(self.buffers[idx]))


class FakeBus(object):
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def of_type(self, event_type):
        return [e for e in self.events if e.event_type == event_type]


class FakeDB(object):
    def __init__(self):
        self.cal_events = []

    def put_cal_event(self, event_type, iq_header, **kwargs):
        self.cal_events.append((event_type, kwargs))

    def put_frame_metrics(self, *args, **kwargs):
        pass

    def update_freq_scan(self, *args, **kwargs):
        pass


class Run(object):
    """Outputs of one start() run."""

    def __init__(self, ds, msgs, trajectory):
        self.ds = ds
        self.msgs = msgs
        self.trajectory = trajectory          # (state after, sync_state, flags) per frame
        self.iq = ds.out_shmem_iface_iq.sent
        self.headers = []
        for raw in self.iq:
            h = IQHeader()
            h.decode_header(raw[:1024])
            self.headers.append(h)

    def flags(self):
        return [(h.delay_sync_flag, h.iq_sync_flag) for h in self.headers]

    def states(self):
        return [t[0] for t in self.trajectory]


class FSMCase(unittest.TestCase):

    def setUp(self):
        self._old_cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="heimdall_fsm_")
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._old_cwd)

    def build(self, overrides=None):
        with open("daq_chain_config.ini", "w") as f:
            f.write(ini_text(overrides))
        logging.disable(logging.CRITICAL)
        try:
            return delay_sync.delaySynchronizer()
        finally:
            logging.disable(logging.NOTSET)

    def run_script(self, script, overrides=None, bus=None, db=None, ds=None):
        if ds is None:
            ds = self.build(overrides)
        msgs = []
        ds._rtl_daq_transaction = lambda msg: (msgs.append(bytes(msg)), b"ok")[1]
        ds.in_shmem_iface = FakeIn(script.frames)
        ds.out_shmem_iface_iq = FakeOut(1024 + N * M * 8)
        ds.out_shmem_iface_hwc = FakeOut(1024)
        ds.event_bus = bus
        ds.db = db
        trajectory = []
        fsm_step = ds._fsm_step

        def recording_step():
            result = fsm_step()
            trajectory.append((ds.current_state, result[2], result[:2]))
            return result
        ds._fsm_step = recording_step
        ds.start()
        return Run(ds, msgs, trajectory)


# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------

def _mixed_script():
    s = FrameScript(seed=21)
    amps, ph = [1, 1.6, 0.7, 1.2], [0, 20, -35, 60]
    s.add(CAL, 1, amps=amps, phases_deg=ph)
    s.add(CAL, 2, amps=amps, phases_deg=ph, delays=[0, 0, 3, 0])   # integer delay -> 's'
    s.add(DUMMY, 1)
    s.add(CAL, 1, correlated=False)                                # weak correlation
    s.add(CAL, 6, amps=amps, phases_deg=ph)
    s.add(DATA, 4, amps=amps, phases_deg=ph, overruns=0)
    s.add(DATA, 3, amps=amps, phases_deg=ph, overruns=1)           # overrun counter step
    s.add(DATA, 4, amps=[1, 3, 0.2, 1], phases_deg=[0, 80, -90, 10])
    s.add(DATA, 2, correlated=False)
    s.add(DATA, 3, freq=868000000)
    s.add(CAL, 6, freq=868000000)
    return s


class TestDefaultsIdentical(FSMCase):

    def _compare(self, overrides_base, overrides_explicit):
        a = self.run_script(_mixed_script(), overrides_base)
        merged = {k: dict(v) for k, v in (overrides_base or {}).items()}
        for section, keys in overrides_explicit.items():
            merged.setdefault(section, {}).update(keys)
        b = self.run_script(_mixed_script(), merged)
        self.assertEqual(a.iq, b.iq)
        self.assertEqual(a.ds.out_shmem_iface_hwc.sent, b.ds.out_shmem_iface_hwc.sent)
        self.assertEqual(a.msgs, b.msgs)
        self.assertEqual(a.trajectory, b.trajectory)
        self.assertGreater(len(a.msgs), 0)
        return a

    def test_explicit_defaults_byte_identical(self):
        self._compare(None, EXPLICIT_DEFAULTS)

    def test_explicit_defaults_byte_identical_with_iq_cal(self):
        a = self._compare({"calibration": {"en_iq_cal": "1"}}, EXPLICIT_DEFAULTS)
        self.assertGreater(a.ds.iq_compensation_cntr, 0)

    def test_overrun_leaves_flags_by_default(self):
        s = FrameScript().to_lock()
        s.add(DATA, 3, overruns=0).add(DATA, 3, overruns=1)
        run = self.run_script(s, {"calibration": {"en_iq_cal": "1"}})
        self.assertEqual(run.flags()[-6:], [(1, 1)] * 6)
        self.assertEqual(run.ds._overrun_invalidated_frames, 0)


# ----------------------------------------------------------------------
# track_decimation
# ----------------------------------------------------------------------

class TestTrackDecimation(FSMCase):

    def _count_checks(self, ds):
        calls = []
        orig = ds.calc_iq_sync

        def counting(iq_samples):
            calls.append(ds.current_state)
            return orig(iq_samples)
        ds.calc_iq_sync = counting
        return calls

    def test_k1_is_todays_trajectory(self):
        s1 = FrameScript(seed=3).to_lock().add(DATA, 9)
        s2 = FrameScript(seed=3).to_lock().add(DATA, 9)
        a = self.run_script(s1)
        b = self.run_script(s2, {"calibration": {"track_decimation": "1"}})
        self.assertEqual(a.trajectory, b.trajectory)
        self.assertEqual(a.iq, b.iq)
        self.assertEqual(a.states()[-1], "STATE_TRACK")

    def test_k3_checks_every_third_frame(self):
        ds1 = self.build()
        calls1 = self._count_checks(ds1)
        a = self.run_script(FrameScript(seed=3).to_lock().add(DATA, 9), ds=ds1)
        ds3 = self.build({"calibration": {"track_decimation": "3"}})
        calls3 = self._count_checks(ds3)
        b = self.run_script(FrameScript(seed=3).to_lock().add(DATA, 9), ds=ds3)
        self.assertEqual(calls1.count("STATE_TRACK"), 9)
        self.assertEqual(calls3.count("STATE_TRACK"), 3)       # frames 1, 4, 7 of 9
        # With healthy frames the state/flag trajectory is identical to k = 1
        self.assertEqual(a.trajectory, b.trajectory)

    def test_k3_reuses_failed_flags_and_slows_track_loss(self):
        def script():
            return FrameScript(seed=4).to_lock().add(DATA, 9, correlated=False)
        a = self.run_script(script())
        b = self.run_script(script(), {"calibration": {"track_decimation": "3"}})
        track_a = a.trajectory[5:]
        track_b = b.trajectory[5:]
        # k = 1: three failed checks -> back to INIT on the 3rd bad frame
        self.assertEqual(track_a[2][0], "STATE_INIT")
        # k = 3: the failed check's flags are reused on the next two frames,
        # and the loss counter only moves on checked frames (1st, 4th, 7th)
        self.assertEqual([t[2] for t in track_b[:3]], [(False, False)] * 3)
        self.assertEqual([t[0] for t in track_b[:6]], ["STATE_TRACK"] * 6)
        self.assertEqual(track_b[6][0], "STATE_INIT")

    def test_k3_counts_only_data_frames(self):
        """CAL frames while locked are always checked and use no DATA slot."""
        ds = self.build({"calibration": {"track_decimation": "3"}})
        calls = self._count_checks(ds)
        s = FrameScript(seed=3).to_lock()
        s.add(DATA, 1).add(CAL, 2).add(DATA, 3)
        self.run_script(s, ds=ds)
        # TRACK frames: DATA(check) CAL(check) CAL(check) DATA DATA DATA(check)
        self.assertEqual(calls.count("STATE_TRACK"), 4)
        self.assertEqual(ds._track_check_counter, 4)


# ----------------------------------------------------------------------
# amplitude_tolerance_mode
# ----------------------------------------------------------------------

class TestAmplitudeToleranceMode(FSMCase):

    def test_pure_function(self):
        tol_db = 2.0
        tol_ratio = 10 ** (tol_db / 20)
        f = delay_sync.amplitude_out_of_tolerance
        half = np.array([1.0, 0.5, 1.0])     # -6 dB: missed by the one-sided test
        double = np.array([1.0, 2.0, 1.0])   # +6 dB
        near = np.array([1.0, 1.1, 0.9])     # within +-2 dB
        self.assertFalse(f(half, tol_ratio, tol_db, "legacy"))
        self.assertTrue(f(half, tol_ratio, tol_db, "symmetric"))
        self.assertTrue(f(double, tol_ratio, tol_db, "legacy"))
        self.assertTrue(f(double, tol_ratio, tol_db, "symmetric"))
        self.assertFalse(f(near, tol_ratio, tol_db, "legacy"))
        self.assertFalse(f(near, tol_ratio, tol_db, "symmetric"))
        self.assertTrue(f(np.array([1.0, 0.0]), tol_ratio, tol_db, "symmetric"))

    def _iq_cal_run(self, mode):
        overrides = {"calibration": {"en_iq_cal": "1"}}
        if mode is not None:
            overrides["calibration"]["amplitude_tolerance_mode"] = mode
        # Channel 2 is 6 dB hot: its correction is 0.5 (below 1, one-sided miss)
        s = FrameScript(seed=8).add(CAL, 4, amps=[1, 1, 2.0, 1])
        return self.run_script(s, overrides)

    def test_legacy_misses_low_correction(self):
        run = self._iq_cal_run(None)
        self.assertEqual(run.ds.iq_compensation_cntr, 0)
        self.assertEqual(run.states()[-1], "STATE_TRACK_LOCK")

    def test_symmetric_compensates(self):
        run = self._iq_cal_run("symmetric")
        self.assertEqual(run.ds.iq_compensation_cntr, 1)
        self.assertNotEqual(run.trajectory[3][0], "STATE_TRACK_LOCK")


# ----------------------------------------------------------------------
# cal_timeout_frames
# ----------------------------------------------------------------------

class TestCalTimeout(FSMCase):

    def _stuck_script(self, n_stuck):
        # INIT consumes the first CAL frame; the uncorrelated ones keep
        # STATE_SAMPLE_CAL failing its dynamic-range check
        return FrameScript(seed=9).add(CAL, 1).add(CAL, n_stuck, correlated=False)

    def test_one_event_per_run(self):
        bus, db = FakeBus(), FakeDB()
        with self.assertLogs("delay_sync", level="WARNING") as logs:
            run = self.run_script(self._stuck_script(10),
                                  {"calibration": {"cal_timeout_frames": "4"},
                                   "daq": {"log_level": "3"}}, bus=bus, db=db)
        self.assertEqual(run.states()[-1], "STATE_SAMPLE_CAL")
        events = bus.of_type("cal_timeout")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload, {"state": "SAMPLE_CAL", "frames": 4})
        self.assertEqual(events[0].severity, "warning")
        timeouts = [e for e in db.cal_events if e[0] == delay_sync.CAL_EVENT_CAL_TIMEOUT]
        self.assertEqual(timeouts, [(9, {"sync_state_before": 2, "sync_state_after": 2})])
        self.assertEqual(sum("Calibration timeout" in line for line in logs.output), 1)
        self.assertEqual(run.msgs, [])          # no hardware action

    def test_counter_resets_when_calibration_completes(self):
        bus = FakeBus()
        # 2 stuck + 3 calibrating CAL frames = 5 < 6, then the run completes
        s = self._stuck_script(2)
        s.add(CAL, 3).add(DATA, 2)              # completes -> TRACK_LOCK -> TRACK
        s.add(DATA, 1, freq=868000000)          # frequency change -> INIT
        # a second run of 5 stuck frames: 5 < 6 only if the count restarted
        s.add(CAL, 1, freq=868000000).add(CAL, 5, correlated=False, freq=868000000)
        run = self.run_script(s, {"calibration": {"cal_timeout_frames": "6"}}, bus=bus)
        self.assertIn("STATE_TRACK", run.states())
        self.assertEqual(bus.of_type("cal_timeout"), [])

    def test_disabled_by_default(self):
        bus = FakeBus()
        self.run_script(self._stuck_script(10), bus=bus)
        self.assertEqual(bus.of_type("cal_timeout"), [])

    def test_fires_again_after_wire_recal(self):
        """Timeout fires, RECL restarts calibration, the new stuck run fires again."""
        bus, db = FakeBus(), FakeDB()
        ds = self.build({"calibration": {"cal_timeout_frames": "3"}})
        first = self.run_script(self._stuck_script(5), bus=bus, db=db, ds=ds)
        self.assertEqual(first.states()[-1], "STATE_SAMPLE_CAL")
        self.assertEqual(len(bus.of_type("cal_timeout")), 1)
        ds._apply_wire_recal(("e1", 1, 0, "wire"))
        self.assertEqual(ds._cal_timeout_count, 0)
        second = self.run_script(FrameScript(seed=10).add(CAL, 1).add(CAL, 4, correlated=False),
                                 bus=bus, db=db, ds=ds)
        self.assertEqual(second.states()[-1], "STATE_SAMPLE_CAL")
        events = bus.of_type("cal_timeout")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].payload, {"state": "SAMPLE_CAL", "frames": 3})
        timeouts = [e for e in db.cal_events if e[0] == delay_sync.CAL_EVENT_CAL_TIMEOUT]
        self.assertEqual(len(timeouts), 2)

    def test_entry_from_init_starts_new_run(self):
        """Entering the calibration states from outside them resets the
        count even when the FSM goes INIT -> SAMPLE_CAL inside one step."""
        ds = self.build({"calibration": {"cal_timeout_frames": "3"}})
        ds.iq_header.frame_type = CAL
        ds.current_state = "STATE_SAMPLE_CAL"
        ds._cal_timeout_count = 7               # left over from a stuck run
        ds._track_cal_timeout("STATE_INIT", 1)
        self.assertEqual(ds._cal_timeout_count, 0)
        for _ in range(3):
            ds._track_cal_timeout("STATE_SAMPLE_CAL", 2)
        self.assertEqual(ds._cal_timeout_count, 3)


# ----------------------------------------------------------------------
# invalidate_on_overrun
# ----------------------------------------------------------------------

class TestInvalidateOnOverrun(FSMCase):

    def _script(self):
        s = FrameScript(seed=10).to_lock()            # 5 frames, overruns 0
        s.add(DATA, 2, overruns=0)                    # frames 5, 6
        s.add(DATA, 4, overruns=2)                    # step at frame 7
        s.add(DATA, 3, overruns=3)                    # step at frame 11
        return s

    def test_frame_and_following_are_invalidated(self):
        run = self.run_script(self._script(), {"calibration": {"invalidate_on_overrun": "1",
                                                               "en_iq_cal": "1"}})
        flags = run.flags()
        self.assertEqual(flags[5:7], [(1, 1)] * 2)
        self.assertEqual(flags[7:9], [(0, 0)] * 2)    # step frame + 1 following
        self.assertEqual(flags[9:11], [(1, 1)] * 2)
        self.assertEqual(flags[11:13], [(0, 0)] * 2)
        self.assertEqual(flags[13], (1, 1))
        self.assertEqual(run.ds._overrun_invalidated_frames, 4)
        # the FSM itself is unaffected (sync_state still 6)
        self.assertEqual([h.sync_state for h in run.headers[7:9]], [6, 6])

    def test_frames_after_overrun(self):
        run = self.run_script(self._script(), {"calibration": {"invalidate_on_overrun": "1",
                                                               "frames_after_overrun": "0",
                                                               "en_iq_cal": "1"}})
        flags = run.flags()
        self.assertEqual(flags[7:9], [(0, 0), (1, 1)])
        run = self.run_script(self._script(), {"calibration": {"invalidate_on_overrun": "1",
                                                               "frames_after_overrun": "3",
                                                               "en_iq_cal": "1"}})
        # 7..10 after the first step, 11..13 (end of the script) after the second
        self.assertEqual(run.flags()[5:], [(1, 1)] * 2 + [(0, 0)] * 7)
        self.assertEqual(run.ds._overrun_invalidated_frames, 7)


# ----------------------------------------------------------------------
# Defensive parsing
# ----------------------------------------------------------------------

class TestMalformedValues(FSMCase):

    def test_bad_values_fall_back_with_warning(self):
        with open("daq_chain_config.ini", "w") as f:
            f.write(ini_text({"calibration": {"amplitude_tolerance_mode": "both",
                                              "cal_timeout_frames": "-1",
                                              "invalidate_on_overrun": "yes",
                                              "frames_after_overrun": "x",
                                              "track_decimation": "0"},
                              "offload": {"fft_workers": "zero"}}))
        with self.assertLogs("delay_sync", level="WARNING") as logs:
            ds = delay_sync.delaySynchronizer()
        self.assertEqual(ds.amplitude_tolerance_mode, "legacy")
        self.assertEqual(ds._cal_timeout_frames, 0)
        self.assertFalse(ds._invalidate_on_overrun)
        self.assertEqual(ds._frames_after_overrun, 1)
        self.assertEqual(ds._track_decimation, 1)
        self.assertEqual(ds.fft_workers, 4)
        joined = "\n".join(logs.output)
        for key in ("amplitude_tolerance_mode", "cal_timeout_frames", "invalidate_on_overrun",
                    "frames_after_overrun", "track_decimation", "fft_workers"):
            self.assertIn(key, joined)


if __name__ == "__main__":
    unittest.main()
