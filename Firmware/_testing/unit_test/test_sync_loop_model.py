"""
    Description :
    Closed-loop model of the sample-delay calibration (delay_sync <-> rtl_daq)
    using the synthetic source's sampling-clock model (_testing/synth_model.py).

    delay_sync's tuning math is carried here as a verbatim copy of its
    expressions (delay_sync.py, STATE_SAMPLE_CAL and STATE_FRAC_SAMPLE_CAL):
        delays[m]         = N_proc - peak_index
        fs_ppm_offsets[m] = -delays[m] * gain(|delays[m]|) * MIN_FS_PPM_OFFSET,
                            clipped to +-MAX_FS_PPM_OFFSET
        frac:  fs_ppm_offsets[m] = sign(tau) * |tau * FRAC_FS_TUNE_GAIN| * MIN_FS_PPM_OFFSET
               for |tau| > frac_delay_tolerance
    When delay_sync exports int_fs_ppm_offsets / frac_fs_ppm_offsets as
    module-level functions, they are additionally cross-checked against the
    copies on a grid of inputs (skipped with a reason otherwise).

    The loop: one measurement per frame, one 's' command (through the real
    128-byte message encoding) per measured misalignment, then the
    STATE_SYNC_WAIT hold of NO_DUMMY_FRAMES dummy blocks before the next
    measurement. B = 2^18 as in the shipped configuration.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import sys
import unittest
from os.path import join, dirname, realpath

import numpy as np

current_path = dirname(realpath(__file__))
testing_path = dirname(current_path)
root_path = dirname(testing_path)
sys.path.insert(0, join(root_path, "_daq_core"))
sys.path.insert(0, testing_path)

import inter_module_messages as imm  # noqa: E402
import synth_model  # noqa: E402
from synth_model import FsSlipModel, FS_SLIP_SIGN, FS_CORRECTION_KEEP_LIMIT, advance_delays  # noqa: E402

B = 2**18
NO_DUMMY_FRAMES = 5
STD_CH = 0

# delay_sync.py constants (DelaySynchronizer.__init__)
MIN_FS_PPM_OFFSET = 0.0000001
MAX_FS_PPM_OFFSET = 0.01
INT_FS_TUNE_GAIN = np.array([[100, 2, 0], [50, 25, 15]])
FRAC_FS_TUNE_GAIN = 20
FRAC_DELAY_TOLERANCE = 0.03


# ---- verbatim copies of the delay_sync expressions ----

def delay_from_peak(n_proc, peak_index):
    """delay_sync.py: self.delays[m] = (self.N_proc - peak_index)"""
    return n_proc - peak_index


def int_fs_ppm_offsets_copy(delays, gain_table, min_ppm, max_ppm):
    """delay_sync.py STATE_SAMPLE_CAL tuning expression, for every channel."""
    fs_ppm_offsets = [0] * len(delays)
    for m in range(len(delays)):
        fs_tune_gain_m = (gain_table[1, (gain_table[0, :] <= abs(delays[m]))])[0]
        fs_ppm_offsets[m] = -1 * delays[m] * fs_tune_gain_m * min_ppm
        if abs(fs_ppm_offsets[m]) > max_ppm:
            fs_ppm_offsets[m] = np.sign(fs_ppm_offsets[m]) * max_ppm
    return fs_ppm_offsets


def frac_fs_ppm_offsets_copy(taus, channel_list, M, tol, gain, min_ppm):
    """delay_sync.py STATE_FRAC_SAMPLE_CAL tuning expression."""
    fs_ppm_offsets = [0] * M
    for i, m in enumerate(channel_list):
        if abs(taus[i]) > tol:
            fs_ppm_offsets[m] = np.sign(taus[i]) * np.abs(taus[i] * gain) * min_ppm
    return fs_ppm_offsets


def _wire(offsets):
    """Round-trip offsets through the real 's' message (float32 on the wire)."""
    _, cmd, params = imm.unpack_msg(imm.pack_msg_sample_freq_tune(2, offsets))
    assert cmd == "s"
    return imm.unpack_f32_params(params, len(offsets))


def run_loop(delays, slip_sign=FS_SLIP_SIGN, frames=40, margin=B):
    """
        Simulate the integer-delay calibration loop.

        Return:
        -------
            (frames_to_converge or None, history of rendered delays per frame)
    """
    M = len(delays)
    model = FsSlipModel(M, B, slip_sign=slip_sign)
    rendered = list(delays)
    history = []
    for frame in range(frames):
        # Measurement frame (one DAQ block)
        rendered = advance_delays(rendered, model.on_block(), margin)
        history.append(list(rendered))
        # The synthesizer renders channel m late by rendered[m]; the
        # correlation peak sits at N_proc + d, so delay_sync measures -d.
        n_proc = 2**16
        measured = [delay_from_peak(n_proc, n_proc + (rendered[m] - rendered[STD_CH])) for m in range(M)]
        if all(abs(d) < 1 for d in measured):
            return frame, history
        offsets = int_fs_ppm_offsets_copy(measured, INT_FS_TUNE_GAIN, MIN_FS_PPM_OFFSET, MAX_FS_PPM_OFFSET)
        model.on_command(_wire(offsets))
        # STATE_SYNC_WAIT: hold until the dummy frames of this command pass
        for _ in range(NO_DUMMY_FRAMES):
            rendered = advance_delays(rendered, model.on_block(), margin)
    return None, history


class TestSyncLoopModel(unittest.TestCase):

    def test_sign_constant(self):
        self.assertEqual(FS_SLIP_SIGN, -1)
        self.assertEqual(FS_CORRECTION_KEEP_LIMIT, 0.008)

    def test_converges_synthetic_table(self):
        """The 5-channel synthetic table (delays[1]=10, delays[3]=30)
        reaches zero delay on every channel within 40 frames."""
        delays = synth_model.channel_tables(5)[0]
        self.assertEqual(delays, [0, 10, 0, 30, 0])
        frame, history = run_loop(delays)
        self.assertIsNotNone(frame, "no convergence: {0}".format(history))
        self.assertLess(frame, 40)
        self.assertEqual(history[-1], [0] * 5)

    def test_converges_any_channel_count(self):
        """Channel tables for M up to 31 (delays 10*m for odd m >= 5) also
        converge within 40 frames."""
        for M in (1, 2, 4, 8, 16, 31):
            delays = synth_model.channel_tables(M)[0]
            frame, history = run_loop(delays)
            self.assertIsNotNone(frame, "M={0}: no convergence: {1}".format(M, history[-3:]))
            self.assertEqual(history[-1], [0] * M)

    def test_large_delay_uses_kept_correction(self):
        """A delay large enough to clip at MAX_FS_PPM_OFFSET (>= keep limit)
        keeps the correction running after the reset block, and still
        converges."""
        frame, history = run_loop([0, 2000])
        self.assertIsNotNone(frame, "no convergence: {0}".format(history))

    def test_wrong_sign_diverges(self):
        """FS_SLIP_SIGN = +1 grows the misalignment within 5 frames."""
        delays = [0, 10, 0, 30, 0]
        frame, history = run_loop(delays, slip_sign=+1, frames=5)
        self.assertIsNone(frame)
        self.assertGreater(history[-1][1], 10)
        self.assertGreater(history[-1][3], 30)

    def test_worked_numbers(self):
        """B.4 worked numbers: delay 10 -> v = +2.5e-5 -> 6.55 samples per
        held block; delay 1 -> v = +1.5e-6 -> 0.39 per block, crossing one
        sample after 3 corrections."""
        v = int_fs_ppm_offsets_copy([0, -10, -1], INT_FS_TUNE_GAIN, MIN_FS_PPM_OFFSET, MAX_FS_PPM_OFFSET)
        self.assertAlmostEqual(v[1], 2.5e-5, places=12)
        self.assertAlmostEqual(v[2], 1.5e-6, places=12)
        model = FsSlipModel(3, B)
        model.on_command(_wire(v))
        self.assertEqual(model.on_block(), [0, -6, 0])
        self.assertAlmostEqual(model.acc[1], 2.5e-5 * B - 6, places=3)
        model = FsSlipModel(2, B)
        slips = []
        for _ in range(3):
            model.on_command([0.0, 1.5e-6])
            slips.append(model.on_block()[1])
            slips.append(model.on_block()[1])
        self.assertEqual(slips, [0, 0, 0, 0, -1, 0])

    def test_keep_and_reset_matches_rtl_daq(self):
        """rtl_daq: the block after the command applies every value; the next
        block resets |v| < 0.008 to 0 and keeps the others until the next 's'."""
        model = FsSlipModel(3, 1000)
        model.on_command([0.0, 0.009, 0.001])
        self.assertTrue(model.active)
        self.assertEqual(model.on_block(), [0, -9, -1])     # reset_cntr 0: all applied
        self.assertEqual(model.corrections, [0.0, 0.009, 0.001])
        self.assertEqual(model.on_block(), [0, -9, 0])      # reset_cntr 1: small one reset
        self.assertFalse(model.active)
        self.assertEqual(model.corrections, [0.0, 0.009, 0.0])
        for _ in range(3):
            self.assertEqual(model.on_block(), [0, -9, 0])  # kept until the next command
        model.on_command([0.0, 0.0, 0.0])
        self.assertEqual(model.on_block(), [0, 0, 0])
        self.assertEqual(model.on_block(), [0, 0, 0])
        # exactly at the limit counts as kept (fabs(v) < limit resets)
        model = FsSlipModel(1, 1000)
        model.on_command([0.008])
        model.on_block()
        model.on_block()
        self.assertEqual(model.corrections, [0.008])

    def test_truncation_toward_zero_and_residual(self):
        model = FsSlipModel(2, 1000)
        model.on_command([0.0, -0.00155])   # -1.55 samples, below the keep limit
        self.assertEqual(model.on_block(), [0, 1])  # k = int(-1.55) = -1 -> slip +1
        self.assertAlmostEqual(model.acc[1], -0.55, places=9)
        self.assertEqual(model.on_block(), [0, 0])

    def test_command_padding_and_truncation(self):
        model = FsSlipModel(3, 1000)
        model.on_command([0.002])
        self.assertEqual(model.values, [0.002, 0.0, 0.0])
        model.on_command([0.001, 0.002, 0.003, 0.004])
        self.assertEqual(model.values, [0.001, 0.002, 0.003])

    def test_advance_delays_clamps(self):
        self.assertEqual(advance_delays([5, 5, 5], [-7, 2, 100], 50), [0, 7, 50])

    def test_model_argument_validation(self):
        with self.assertRaises(ValueError):
            FsSlipModel(0, B)
        with self.assertRaises(ValueError):
            FsSlipModel(2, 0)
        with self.assertRaises(ValueError):
            FsSlipModel(2, B, slip_sign=0)
        with self.assertRaises(ValueError):
            synth_model.channel_tables(0)

    def test_channel_tables(self):
        d, p, cp, pw = synth_model.channel_tables(5)
        self.assertEqual((d, p, cp, pw), ([0, 10, 0, 30, 0], [-10, 0, 20, 20, 40],
                                          [-10, 0, 20, 20, 40], [0, -3, 3, 2, 1]))
        d, p, cp, pw = synth_model.channel_tables(8)
        self.assertEqual(d, [0, 10, 0, 30, 0, 50, 0, 70])
        self.assertEqual(p[5:], [-10, 0, 20])
        self.assertEqual(pw[5:], [0, -3, 3])
        self.assertEqual(synth_model.channel_tables(3)[0], [0, 10, 0])
        self.assertEqual(synth_model.channel_tables(8, zero_delays=True)[0], [0] * 8)

    def test_delay_estimate_sign(self):
        """Derivation step 2 with real data: y[n] = x[n - d] correlated as
        delay_sync does (x_padd = [x, 0], y_padd = [0, y], IFFT(conj(X) Y))
        gives delays = N_proc - peak_index = -d."""
        rng = np.random.default_rng(3)
        n_proc = 4096
        base = rng.normal(size=n_proc + 64) + 1j * rng.normal(size=n_proc + 64)
        x = base[64:64 + n_proc]
        zeros = np.zeros(n_proc, dtype=np.complex64)
        x_fft = np.fft.fft(np.concatenate([x, zeros]))
        for d in (0, 1, 10, 30):
            y = base[64 - d:64 - d + n_proc]          # y[n] = x[n - d]
            y_fft = np.fft.fft(np.concatenate([zeros, y]))
            corr = np.abs(np.fft.ifft(x_fft.conj() * y_fft)) ** 2
            self.assertEqual(delay_from_peak(n_proc, int(np.argmax(corr))), -d)


class TestDelaySyncCrossCheck(unittest.TestCase):
    """When delay_sync exports its tuning math as module-level functions,
    they must equal the verbatim copies above."""

    @classmethod
    def setUpClass(cls):
        try:
            import delay_sync
        except Exception as err:  # optional deps (scipy, pyzmq) or mid-edit module
            raise unittest.SkipTest("delay_sync not importable: {0}".format(err))
        # staticmethod: a plain function stored on the class would otherwise
        # bind as a method and receive `self` as an extra first argument.
        int_fn = getattr(delay_sync, "int_fs_ppm_offsets", None)
        frac_fn = getattr(delay_sync, "frac_fs_ppm_offsets", None)
        cls.int_fn = staticmethod(int_fn) if int_fn is not None else None
        cls.frac_fn = staticmethod(frac_fn) if frac_fn is not None else None

    @staticmethod
    def _offsets(result, M):
        """Accept a bare offsets sequence or an (offsets, flag) pair."""
        if isinstance(result, tuple) and len(result) == 2 and np.ndim(result[0]) == 1 \
                and len(result[0]) == M:
            result = result[0]
        return [float(v) for v in result]

    def test_int_fs_ppm_offsets(self):
        if self.int_fn is None:
            self.skipTest("delay_sync.int_fs_ppm_offsets not exported")
        grid = [[0, d, -d, 2 * d + 1] for d in (0, 1, 2, 3, 10, 49, 50, 99, 100, 101, 5000, 10**6)]
        for delays in grid:
            got = self._offsets(self.int_fn(np.array(delays), INT_FS_TUNE_GAIN,
                                            MIN_FS_PPM_OFFSET, MAX_FS_PPM_OFFSET), len(delays))
            want = [float(v) for v in int_fs_ppm_offsets_copy(delays, INT_FS_TUNE_GAIN,
                                                              MIN_FS_PPM_OFFSET, MAX_FS_PPM_OFFSET)]
            self.assertEqual(len(got), len(want))
            for g, w in zip(got, want):
                self.assertAlmostEqual(g, w, places=15, msg="delays={0}".format(delays))

    def test_frac_fs_ppm_offsets(self):
        if self.frac_fn is None:
            self.skipTest("delay_sync.frac_fs_ppm_offsets not exported")
        M = 5
        channel_list = [1, 2, 3, 4]
        for taus in ([0.0, 0.0, 0.0, 0.0], [0.03, -0.03, 0.031, -0.5],
                     [0.2, -0.2, 0.01, 1.5], [-0.04, 0.0, 0.029, 0.3]):
            got = self._offsets(self.frac_fn(np.array(taus), channel_list, M, FRAC_DELAY_TOLERANCE,
                                             FRAC_FS_TUNE_GAIN, MIN_FS_PPM_OFFSET), M)
            want = [float(v) for v in frac_fs_ppm_offsets_copy(taus, channel_list, M, FRAC_DELAY_TOLERANCE,
                                                               FRAC_FS_TUNE_GAIN, MIN_FS_PPM_OFFSET)]
            for g, w in zip(got, want):
                self.assertAlmostEqual(g, w, places=15, msg="taus={0}".format(taus))


if __name__ == "__main__":
    unittest.main()
