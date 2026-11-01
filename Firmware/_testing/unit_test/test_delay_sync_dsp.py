"""
    Description :
    Pure-function tests for the delay_sync DSP math on synthetic data —
    import-and-call only, no FIFOs, no shared memory, no C binaries:

      * estimate_frac_delays must recover known injected fractional sample
        delays (for the default reference channel and a non-zero one), and
        must not modify the input array (it may alias the outbound
        shared-memory frame).
      * calc_iq_sync must recover known amplitude/phase offsets (the
        returned iq_diffs equalize the channels) and report a healthy
        cross-correlation dynamic range for aligned channels.
      * int_fs_ppm_offsets / frac_fs_ppm_offsets equal the inline FSM
        expressions they replaced (down to the 's' message bytes).
      * throughput levers (spec I.4): results are bit-identical for 1, 2, 4
        FFT workers, and skip_copy_on_drop gives the same downstream results
        on the aliased input as on the copied frame.

    numba and zmq are stubbed only when the real modules are unavailable
    (delay_sync imports both at module scope; neither is exercised here).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import logging
import sys
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
from offload_engines import FFTEngine  # noqa: E402


def make_ds(M=4, std_ch_ind=0, N_proc=2**13, corr_peak_offset=200,
            amplitude_cal_mode="channel_power"):
    """Build a delaySynchronizer carrying only the state the two pure DSP
    methods read (bypasses __init__, which opens config/interfaces)."""
    ds = object.__new__(delay_sync.delaySynchronizer)
    ds.logger = logging.getLogger("test_delay_sync_dsp")
    ds.logger.setLevel(logging.WARNING)
    ds.M = M
    ds.std_ch_ind = std_ch_ind
    ds.channel_list = [m for m in range(M)]
    ds.channel_list.remove(std_ch_ind)
    ds.N_proc = N_proc
    ds.corr_peak_offset = corr_peak_offset
    ds.min_corr_peak_dyn_range = 10
    ds.amplitude_cal_mode = amplitude_cal_mode
    ds.fft_engine = FFTEngine(engine_type="cpu_scipy")
    return ds


def frac_delayed_channels(N, M, taus, seed=1234):
    """Multichannel complex noise where channel m is the reference delayed by
    taus[m] samples (exact fractional delay applied in the frequency
    domain)."""
    rng = np.random.default_rng(seed)
    base = (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    base_w = np.fft.fft(base)
    freqs = np.fft.fftfreq(N)  # cycles/sample
    iq_samples = np.zeros((M, N), dtype=np.complex64)
    for m in range(M):
        shifted_w = base_w * np.exp(-2j * np.pi * freqs * taus[m])
        iq_samples[m, :] = np.fft.ifft(shifted_w).astype(np.complex64)
    return iq_samples


class TesterEstimateFracDelays(unittest.TestCase):

    N = 2**14
    BLOCK = 2**10

    def _recovered(self, ds, iq_samples):
        taus = ds.estimate_frac_delays(iq_samples, block_size=self.BLOCK)
        self.assertEqual(len(taus), iq_samples.shape[0] - 1)
        return taus

    def test_recovers_known_fractional_delays_ref0(self):
        injected = [0.0, 0.25, -0.4, 0.1]
        ds = make_ds(M=4, std_ch_ind=0)
        iq_samples = frac_delayed_channels(self.N, 4, injected)
        taus = self._recovered(ds, iq_samples)
        # taus are ordered as ds.channel_list = [1, 2, 3]. Sign convention
        # (verified against the shipping implementation): a channel delayed
        # by +tau samples yields an estimate of +tau.
        for tau_est, ch in zip(taus, ds.channel_list):
            self.assertAlmostEqual(
                tau_est, injected[ch], delta=0.05,
                msg="channel {:d}: injected {:+.3f}, recovered {:+.3f}"
                    .format(ch, injected[ch], tau_est))

    def test_recovers_with_nonzero_reference_channel(self):
        """The reference is self.std_ch_ind, not hardcoded 0: delays must be
        recovered relative to channel 2."""
        ds = make_ds(M=4, std_ch_ind=2)
        # Absolute delays; relative to ch2 they are -0.3, -0.1, +0.2
        absolute = [0.0, 0.2, 0.3, 0.5]
        iq_samples = frac_delayed_channels(self.N, 4, absolute)
        taus = self._recovered(ds, iq_samples)
        relative = [absolute[ch] - absolute[2] for ch in ds.channel_list]
        for tau_est, tau_rel, ch in zip(taus, relative, ds.channel_list):
            self.assertAlmostEqual(
                tau_est, tau_rel, delta=0.05,
                msg="channel {:d}: relative {:+.3f}, recovered {:+.3f}"
                    .format(ch, tau_rel, tau_est))

    def test_zero_delay_estimates_near_zero(self):
        ds = make_ds(M=3, std_ch_ind=0)
        iq_samples = frac_delayed_channels(self.N, 3, [0.0, 0.0, 0.0])
        taus = self._recovered(ds, iq_samples)
        for tau_est in taus:
            self.assertLess(abs(tau_est), 0.02)

    def test_input_array_not_mutated(self):
        """The input may alias the outbound shared-memory frame — the phase
        correction must not be applied in place."""
        ds = make_ds(M=4, std_ch_ind=0)
        iq_samples = frac_delayed_channels(self.N, 4, [0.0, 0.3, -0.2, 0.15])
        snapshot = iq_samples.copy()
        ds.estimate_frac_delays(iq_samples, block_size=self.BLOCK)
        np.testing.assert_array_equal(iq_samples, snapshot)


class TesterCalcIQSync(unittest.TestCase):

    N_PROC = 2**13

    def _synthetic(self, amps, phases_deg, seed=99, noise_amp=1e-3):
        rng = np.random.default_rng(seed)
        M = len(amps)
        base = (rng.standard_normal(self.N_PROC) +
                1j * rng.standard_normal(self.N_PROC)).astype(np.complex64)
        iq_samples = np.zeros((M, self.N_PROC), dtype=np.complex64)
        for m in range(M):
            factor = amps[m] * np.exp(1j * np.deg2rad(phases_deg[m]))
            noise = noise_amp * (rng.standard_normal(self.N_PROC) +
                                 1j * rng.standard_normal(self.N_PROC))
            iq_samples[m, :] = (factor * base + noise).astype(np.complex64)
        return iq_samples

    def test_recovers_amplitude_and_phase_offsets(self):
        amps = [1.0, 0.5, 2.0, 1.25]
        phases = [0.0, 30.0, -45.0, 120.0]
        ds = make_ds(M=4, std_ch_ind=0, N_proc=self.N_PROC)
        iq_samples = self._synthetic(amps, phases)
        dyn_ranges, iq_diffs = ds.calc_iq_sync(iq_samples)

        # Reference channel correction is exactly 1
        self.assertAlmostEqual(iq_diffs[0].real, 1.0, places=5)
        self.assertAlmostEqual(iq_diffs[0].imag, 0.0, places=5)

        # Applying the corrections equalizes every channel to the reference
        corrected = iq_samples * iq_diffs[:, None]
        ref = corrected[0]
        for m in range(1, 4):
            err = np.max(np.abs(corrected[m] - ref)) / np.max(np.abs(ref))
            self.assertLess(
                err, 0.02,
                "channel {:d} not equalized (relative error {:.4f})"
                .format(m, err))

        # In channel_power mode the correction magnitude is the inverse
        # amplitude offset
        for m in range(1, 4):
            self.assertAlmostEqual(abs(iq_diffs[m]), 1.0 / amps[m], delta=0.02)
        # ... and the correction phase cancels the injected phase offset
        for m in range(1, 4):
            residual = np.angle(iq_diffs[m] *
                                np.exp(1j * np.deg2rad(phases[m])))
            self.assertLess(abs(np.rad2deg(residual)), 1.0)

    def test_dyn_range_healthy_for_aligned_channels(self):
        """Aligned (sample-synchronous) noise channels must show a large
        positive correlation-peak dynamic range on every channel."""
        ds = make_ds(M=4, std_ch_ind=0, N_proc=self.N_PROC)
        iq_samples = self._synthetic([1.0] * 4, [0.0] * 4)
        dyn_ranges, _iq_diffs = ds.calc_iq_sync(iq_samples)
        self.assertEqual(len(dyn_ranges), 3)  # channel_list entries
        for dr in dyn_ranges:
            self.assertGreater(dr, 15.0)

    def test_disabled_mode_returns_unit_magnitude(self):
        """amplitude_cal_mode='disabled': phase-only corrections."""
        ds = make_ds(M=3, std_ch_ind=0, N_proc=self.N_PROC,
                     amplitude_cal_mode="disabled")
        iq_samples = self._synthetic([1.0, 0.5, 2.0], [0.0, 60.0, -30.0])
        _dyn_ranges, iq_diffs = ds.calc_iq_sync(iq_samples)
        for m in range(3):
            self.assertAlmostEqual(abs(iq_diffs[m]), 1.0, places=3)

    def test_nonzero_reference_channel(self):
        ds = make_ds(M=3, std_ch_ind=1, N_proc=self.N_PROC)
        iq_samples = self._synthetic([0.8, 1.0, 1.6], [15.0, 0.0, -75.0])
        _dyn_ranges, iq_diffs = ds.calc_iq_sync(iq_samples)
        self.assertAlmostEqual(iq_diffs[1].real, 1.0, places=5)
        self.assertAlmostEqual(iq_diffs[1].imag, 0.0, places=5)
        corrected = iq_samples * iq_diffs[:, None]
        for m in (0, 2):
            err = (np.max(np.abs(corrected[m] - corrected[1])) /
                   np.max(np.abs(corrected[1])))
            self.assertLess(err, 0.02)



# ======================================================================
# Extracted tuning math and throughput levers (spec I.4, K.1) — WP-06
# ======================================================================

import inter_module_messages  # noqa: E402
from iq_header import IQHeader  # noqa: E402
from offload_engines import resolve_workers  # noqa: E402

MIN_FS_PPM_OFFSET = 0.0000001
MAX_FS_PPM_OFFSET = 0.01
INT_FS_TUNE_GAIN = np.array([[100, 2, 0], [50, 25, 15]])
FRAC_FS_TUNE_GAIN = 20
FRAC_DELAY_TOLERANCE = 0.03


def inline_int_offsets(delays, channel_list, M):
    """Verbatim copy of the pre-extraction STATE_SAMPLE_CAL loop body."""
    fs_ppm_offsets = [0] * M
    for m in channel_list:
        fs_tune_gain_m = (INT_FS_TUNE_GAIN[1, (INT_FS_TUNE_GAIN[0, :] <= abs(delays[m]))])[0]
        fs_ppm_offsets[m] = -1 * delays[m] * fs_tune_gain_m * MIN_FS_PPM_OFFSET
        if abs(fs_ppm_offsets[m]) > MAX_FS_PPM_OFFSET:
            fs_ppm_offsets[m] = np.sign(fs_ppm_offsets[m]) * MAX_FS_PPM_OFFSET
    return fs_ppm_offsets


def inline_frac_offsets(taus, channel_list, M):
    """Verbatim copy of the pre-extraction STATE_FRAC_SAMPLE_CAL loop."""
    frac_delay_update_flag = False
    fs_ppm_offsets = [0] * M
    for i, m in enumerate(channel_list):
        if abs(taus[i]) > FRAC_DELAY_TOLERANCE:
            fs_ppm_offsets[m] = np.sign(taus[i]) * np.abs(taus[i] * FRAC_FS_TUNE_GAIN) * MIN_FS_PPM_OFFSET
            frac_delay_update_flag = True
    return fs_ppm_offsets, frac_delay_update_flag


class TesterExtractedTuningMath(unittest.TestCase):
    """int_fs_ppm_offsets / frac_fs_ppm_offsets equal the inline expressions
    they replace, down to the bytes of the 's' message sent to rtl_daq."""

    def test_int_offsets_equal_inline(self):
        rng = np.random.default_rng(3)
        for ref in (0, 2):
            M = 5
            channel_list = [m for m in range(M) if m != ref]
            grid = [np.zeros(M, dtype=int)]
            for d in (1, 2, 3, 10, 49, 50, 99, 100, 101, 5000, 10**6):
                v = np.zeros(M, dtype=int)
                for k, m in enumerate(channel_list):
                    v[m] = (d, -d, 2 * d + 1, -3 * d)[k]
                grid.append(v)
            grid.extend(np.where(np.arange(M) == ref, 0, rng.integers(-300, 300, M))
                        for _ in range(50))
            for delays in grid:
                got = delay_sync.int_fs_ppm_offsets(delays, INT_FS_TUNE_GAIN,
                                                    MIN_FS_PPM_OFFSET, MAX_FS_PPM_OFFSET)
                want = inline_int_offsets(delays, channel_list, M)
                for m in channel_list:
                    self.assertEqual(got[m], want[m], (ref, list(delays)))
                self.assertEqual(
                    inter_module_messages.pack_msg_sample_freq_tune(5, got),
                    inter_module_messages.pack_msg_sample_freq_tune(5, want),
                    (ref, list(delays)))

    def test_frac_offsets_equal_inline(self):
        rng = np.random.default_rng(4)
        M = 5
        channel_list = [1, 2, 3, 4]
        cases = [[0.0] * 4, [0.03, -0.03, 0.031, -0.5], [0.2, -0.2, 0.01, 1.5]]
        cases.extend(list(rng.uniform(-0.6, 0.6, 4)) for _ in range(50))
        for taus in cases:
            got = delay_sync.frac_fs_ppm_offsets(taus, channel_list, M, FRAC_DELAY_TOLERANCE,
                                                 FRAC_FS_TUNE_GAIN, MIN_FS_PPM_OFFSET)
            want, _flag = inline_frac_offsets(taus, channel_list, M)
            self.assertEqual(list(got), list(want), taus)
            self.assertEqual(inter_module_messages.pack_msg_sample_freq_tune(5, got),
                             inter_module_messages.pack_msg_sample_freq_tune(5, want))

    def test_tune_gain_table(self):
        self.assertEqual(delay_sync.int_fs_tune_gain(0, INT_FS_TUNE_GAIN), 15)
        self.assertEqual(delay_sync.int_fs_tune_gain(-2, INT_FS_TUNE_GAIN), 25)
        self.assertEqual(delay_sync.int_fs_tune_gain(100, INT_FS_TUNE_GAIN), 50)


class TesterResolveWorkers(unittest.TestCase):

    def test_values(self):
        self.assertEqual(resolve_workers(None), 4)
        self.assertEqual(resolve_workers(""), 4)
        self.assertEqual(resolve_workers("1"), 1)
        self.assertEqual(resolve_workers(" 8 "), 8)
        self.assertEqual(resolve_workers("auto", cores=[2, 3, 3]), 2)
        self.assertGreaterEqual(resolve_workers("AUTO"), 1)
        for bad in ("0", "-2", "x", "1.5"):
            with self.assertRaises(ValueError):
                resolve_workers(bad)


def _ds_for_levers(M=4, N=2**13, workers=4):
    ds = make_ds(M=M, std_ch_ind=0, N_proc=N)
    ds.fft_engine = FFTEngine(engine_type="cpu_scipy", workers=workers)
    ds.xcorr_engine = FFTEngine(engine_type="cpu_scipy", workers=workers)
    ds.corr_functions = np.zeros((M, 2 * N))
    ds.N = N
    ds.M = M
    return ds


class TesterWorkerIdentity(unittest.TestCase):
    """[offload] fft_workers / xcorr_workers only split independent
    transforms: results are bit-identical for 1, 2 and 4 workers."""

    def test_bit_identical_for_worker_counts(self):
        iq_samples = frac_delayed_channels(2**13, 4, [0.0, 0.3, -0.2, 0.15], seed=11)
        iq_samples[2] = np.roll(iq_samples[2], 3)
        results = {}
        for workers in (1, 2, 4):
            ds = _ds_for_levers(workers=workers)
            taus = ds.estimate_frac_delays(iq_samples, block_size=2**10)
            ds._compute_corr_functions(iq_samples)
            dyn, diffs = ds.calc_iq_sync(iq_samples)
            results[workers] = (np.array(taus), ds.corr_functions.copy(), dyn, diffs)
        for workers in (2, 4):
            for a, b in zip(results[1], results[workers]):
                self.assertTrue(np.array_equal(a, b), "workers={}".format(workers))


class TesterSkipCopyOnDrop(unittest.TestCase):
    """[offload] skip_copy_on_drop: on a dropped output buffer the FSM works
    on the input frame itself; every downstream result is unchanged."""

    def _ds(self, skip, en_iq_cal=False):
        ds = _ds_for_levers()
        ds.iq_header = IQHeader()
        ds.iq_header.frame_type = IQHeader.FRAME_TYPE_CAL
        ds.iq_header.active_ant_chs = ds.M
        ds.iq_header.cpi_length = ds.N
        ds._skip_copy_on_drop = skip
        ds.en_iq_cal = en_iq_cal
        ds.iq_corrections = np.ones(ds.M, dtype=np.complex64)
        ds._drop_scratch = None
        ds._en_frontend_telemetry = False
        ds.db = None
        return ds

    def _downstream(self, ds):
        iq = ds._iq_samples
        ds._compute_corr_functions(iq)
        taus = ds.estimate_frac_delays(iq[:, 0:ds.N_proc])
        dyn, diffs = ds.calc_iq_sync(iq)
        return np.array(taus), ds.corr_functions.copy(), dyn, diffs

    def test_aliased_equals_copied(self):
        iq_in = frac_delayed_channels(2**13, 4, [0.0, 0.25, -0.1, 0.4], seed=5)
        snapshot = iq_in.copy()
        copied = self._ds(skip=False)
        copied._preprocess_frame(iq_in, 3)            # 3 = BUFFER_DROPPED
        self.assertFalse(np.shares_memory(copied._iq_samples, iq_in))
        aliased = self._ds(skip=True)
        aliased._preprocess_frame(iq_in, 3)
        self.assertTrue(np.shares_memory(aliased._iq_samples, iq_in))
        for a, b in zip(self._downstream(copied), self._downstream(aliased)):
            self.assertTrue(np.array_equal(a, b))
        np.testing.assert_array_equal(iq_in, snapshot)   # input never modified

    def test_not_applied_with_iq_cal(self):
        iq_in = frac_delayed_channels(2**13, 4, [0.0, 0.0, 0.0, 0.0], seed=6)
        ds = self._ds(skip=True, en_iq_cal=True)
        ds._preprocess_frame(iq_in, 3)
        self.assertFalse(np.shares_memory(ds._iq_samples, iq_in))

    def test_not_applied_when_buffer_available(self):
        iq_in = frac_delayed_channels(2**13, 4, [0.0, 0.0, 0.0, 0.0], seed=7)
        ds = self._ds(skip=True)

        class Out(object):
            buffers = [np.zeros(1024 + iq_in.nbytes, dtype=np.uint8)] * 2
        ds.out_shmem_iface_iq = Out()
        ds._preprocess_frame(iq_in, 0)
        self.assertFalse(np.shares_memory(ds._iq_samples, iq_in))
        np.testing.assert_array_equal(ds._iq_samples, iq_in)


if __name__ == "__main__":
    unittest.main()
