"""
    Description :
    Pure models used by the synthetic IQ source (test_data_synthesizer.py):

    * FsSlipModel  - the sampling-clock response of rtl_daq to the 's'
                     (sample frequency correction) inter-module message,
                     expressed as integer sample slips per DAQ block.
    * channel_tables - the per-channel delay / phase / power tables of the
                       synthetic scene, for any channel count.
    * advance_delays - applies slips to the rendered per-channel delays.

    The module has no I/O, no threads and no third-party dependencies, so the
    closed-loop calibration test (test_sync_loop_model.py) and the synthesizer
    share exactly the same arithmetic.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""

# rtl_daq.c: FS_CORRECTION_KEEP_LIMIT - corrections whose magnitude is at or
# above this value are not reset after the first corrected block.
FS_CORRECTION_KEEP_LIMIT = 0.008

# Sign of the rendered-delay change per moved sample.
#
# Derivation (committed analytically; test_sync_loop_model.py asserts that
# -1 converges and +1 diverges):
#   1. The synthesizer renders channel m late by d = delays[m] samples:
#      raw_sig_m[n] = sig[n - d].
#   2. delay_sync correlates x_padd = [x, 0] (reference channel) with
#      y_padd = [0, y] (c = IFFT(conj(X) * Y)). For y[n] = x[n - d] the peak is
#      at peak_index = N_proc + d, so delay_sync's delays[m] = N_proc -
#      peak_index = -d.
#   3. fs_ppm_offsets[m] = -delays[m] * gain * MIN_FS_PPM_OFFSET
#      = +d * gain * 1e-7: a late channel receives a POSITIVE fractional rate
#      offset, which rtl_daq applies for one DAQ block.
#   4. The real loop converges on hardware, so a positive offset must reduce
#      the lag. The model moves k = int(acc) samples with the sign of the
#      offset (k > 0 for a late channel), hence the rendered delay changes by
#      -k: rendered[m] = delays[m] + FS_SLIP_SIGN * k with FS_SLIP_SIGN = -1.
FS_SLIP_SIGN = -1

# Synthetic scene for the first five channels (values of the historical
# 5-channel synthesizer, kept exactly).
_BASE_DELAYS = (0, 10, 0, 30, 0)                    # [sample]
_BASE_PHASE_DIFFS = (-10, 0, 20, 20, 40)            # [deg]
_BASE_CAL_NOISE_PHASE_DIFFS = (-10, 0, 20, 20, 40)  # [deg]
_BASE_POWER_DIFFS = (0, -3, 3, 2, 1)                # [dB]


def channel_tables(num_ch, zero_delays=False):
    """
        Per-channel synthetic scene tables.

        For m < 5 the historical values are returned exactly. For m >= 5:
        delays[m] = 10*m for odd m and 0 for even m; phase and power values
        repeat cyclically (index m % 5).

        Parameters:
        -----------
            :param: num_ch: Number of channels (>= 1)
            :param: zero_delays: When True every delay is 0 (identity tests)

        Return:
        -------
            (delays, phase_diffs, cal_noise_phase_diffs, power_diffs), lists of
            length num_ch
    """
    if num_ch < 1:
        raise ValueError("num_ch must be >= 1, got {0}".format(num_ch))
    delays = []
    for m in range(num_ch):
        if zero_delays:
            delays.append(0)
        elif m < len(_BASE_DELAYS):
            delays.append(_BASE_DELAYS[m])
        else:
            delays.append(10 * m if m % 2 else 0)
    phase_diffs = [_BASE_PHASE_DIFFS[m % 5] for m in range(num_ch)]
    cal_noise_phase_diffs = [_BASE_CAL_NOISE_PHASE_DIFFS[m % 5] for m in range(num_ch)]
    power_diffs = [_BASE_POWER_DIFFS[m % 5] for m in range(num_ch)]
    return delays, phase_diffs, cal_noise_phase_diffs, power_diffs


def advance_delays(rendered, slips, margin):
    """
        Apply per-channel integer slips to the rendered delays.

        The result is clamped to [0, margin]: a channel cannot be rendered
        ahead of the reference stream (that would need future samples) or
        further back than the generator keeps history for.

        Return:
        -------
            New list of rendered delays
    """
    return [min(max(int(r) + int(s), 0), int(margin)) for r, s in zip(rendered, slips)]


class FsSlipModel:
    """
        Sampling-clock model of rtl_daq's response to the 's' command
        (rtl_daq.c 's' handler and the fs_correction_flag block of the main
        loop).

        The M float32 values of an 's' message are fractional sample-rate
        offsets (delay_sync names them "ppm", but MIN_FS_PPM_OFFSET = 1e-7 and
        MAX_FS_PPM_OFFSET = 0.01 are fractions). rtl_daq applies them for the
        block following the command; on the next block it resets every channel
        whose |offset| < keep_limit to 0 and keeps the others running until the
        next 's' command.

        A channel running with fractional offset c for one block of B samples
        moves c*B samples relative to the reference. The model accumulates
        that per channel and hands out the integer part as slips; the
        fractional residual (< 1 sample) is carried over and is not rendered
        (documented limitation: the synthetic source has no fractional delay).
    """

    def __init__(self, M, B, keep_limit=FS_CORRECTION_KEEP_LIMIT, slip_sign=FS_SLIP_SIGN):
        """
            :param: M: Number of channels
            :param: B: DAQ block size (samples per channel per block)
            :param: keep_limit: rtl_daq FS_CORRECTION_KEEP_LIMIT
            :param: slip_sign: Sign of the rendered-delay change per moved sample
        """
        if M < 1:
            raise ValueError("M must be >= 1, got {0}".format(M))
        if B < 1:
            raise ValueError("B must be >= 1, got {0}".format(B))
        if slip_sign not in (-1, 1):
            raise ValueError("slip_sign must be -1 or +1, got {0}".format(slip_sign))
        self.M = int(M)
        self.B = int(B)
        self.keep_limit = float(keep_limit)
        self.slip_sign = int(slip_sign)
        self.values = [0.0] * self.M       # last commanded offsets (new_fs_corrections)
        self.corrections = [0.0] * self.M  # offsets currently applied to the tuners
        self.acc = [0.0] * self.M          # fractional sample accumulators
        self.reset_cntr = 0
        self.active = False                # rtl_daq fs_correction_flag

    def on_command(self, values):
        """
            Register an 's' command.

            :param: values: per-channel fractional offsets; channels beyond
                    len(values) get 0, extra values are ignored
        """
        v = [float(x) for x in list(values)[:self.M]]
        v += [0.0] * (self.M - len(v))
        self.values = v
        self.reset_cntr = 0
        self.active = True

    def on_block(self):
        """
            Advance the model by one generated DAQ block.

            Return:
            -------
                list of M ints: the slip (rendered-delay change) of each channel
                for this block
        """
        if self.active:
            if self.reset_cntr == 0:
                self.corrections = list(self.values)
            elif self.reset_cntr == 1:
                self.corrections = [v if abs(v) >= self.keep_limit else 0.0 for v in self.values]
                self.active = False
            self.reset_cntr += 1
        slips = [0] * self.M
        for m in range(self.M):
            c = self.corrections[m]
            if c != 0.0:
                self.acc[m] += c * self.B
            k = int(self.acc[m])  # truncation toward zero
            if k:
                self.acc[m] -= k
                slips[m] = self.slip_sign * k
        return slips
