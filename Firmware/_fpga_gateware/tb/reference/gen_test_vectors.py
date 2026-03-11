#!/usr/bin/env python3
"""
gen_test_vectors.py — Generate test vectors for HeIMDALL FPGA gateware simulation.

Produces:
  - test_iq_data.hex        : Interleaved U8 IQ data for top-level testbench
  - fir_coeffs.hex          : FIR filter coefficients (fixed-point Q1.17)
  - fir_input_i.hex         : FIR filter input I channel (fixed-point)
  - fir_input_q.hex         : FIR filter input Q channel (fixed-point)
  - fir_output_i.hex        : Expected FIR filter output I channel
  - fir_output_q.hex        : Expected FIR filter output Q channel
  - xcorr_ref_re.hex        : Cross-correlation reference (real)
  - xcorr_ref_im.hex        : Cross-correlation reference (imaginary)
  - xcorr_test_re.hex       : Cross-correlation test signal (real)
  - xcorr_test_im.hex       : Cross-correlation test signal (imaginary)
  - xcorr_expected.hex      : Expected cross-correlation output (magnitude^2)
  - twiddle_re.hex          : FFT twiddle factors (cosine, fixed-point)
  - twiddle_im.hex          : FFT twiddle factors (-sine, fixed-point)

All .hex files are formatted for Verilog $readmemh (one value per line, hex).
"""

import numpy as np
from pathlib import Path

try:
    from scipy.signal import firwin, lfilter
    from scipy.fft import fft, ifft
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    print("WARNING: scipy not found. Using numpy fallback (reduced precision).")


# =============================================================================
# Configuration
# =============================================================================
OUTPUT_DIR     = Path(__file__).parent
SAMPLE_RATE    = 2.4e6      # Hz
SIGNAL_FREQ    = 100e3      # Hz (CW signal)
NOISE_LEVEL    = 0.05       # Relative to signal
NUM_SAMPLES    = 256        # Number of IQ sample pairs
NUM_TAPS       = 16         # FIR filter tap count
DECIM_RATIO    = 4          # Decimation ratio
FFT_N          = 64         # FFT size for cross-correlation test
FFT_N_LARGE    = 1024       # FFT size for twiddle factor generation
XCORR_DELAY    = 5          # Sample delay for cross-correlation test
DATA_W         = 18         # Fixed-point data width (signed)
COEFF_W        = 18         # Fixed-point coefficient width (signed)
TWIDDLE_W      = 16         # Twiddle factor width (signed)
FRAC_BITS_DATA = DATA_W - 1
FRAC_BITS_COEFF = COEFF_W - 1
FRAC_BITS_TW    = TWIDDLE_W - 1


# =============================================================================
# Helpers
# =============================================================================
def float_to_fixedpoint(values, total_bits, frac_bits):
    """Convert float array to signed fixed-point integers."""
    scale = 2**frac_bits
    max_val = 2**(total_bits - 1) - 1
    min_val = -(2**(total_bits - 1))
    fp = np.round(values * scale).astype(np.int64)
    fp = np.clip(fp, min_val, max_val)
    return fp


def to_twos_complement_hex(values, total_bits):
    """Convert signed integers to two's complement hex strings."""
    mask = (1 << total_bits) - 1
    hex_width = (total_bits + 3) // 4
    lines = []
    for v in values:
        v_int = int(v)
        if v_int < 0:
            v_int = v_int + (1 << total_bits)
        v_int = v_int & mask
        lines.append(f"{v_int:0{hex_width}x}")
    return lines


def write_hex_file(filepath, hex_lines):
    """Write hex lines to a file."""
    with open(filepath, 'w') as f:
        for line in hex_lines:
            f.write(line + '\n')
    print(f"  Wrote {len(hex_lines)} values to {filepath.name}")


# =============================================================================
# Generate IQ test data (U8 interleaved)
# =============================================================================
def gen_iq_data():
    """Generate CW signal + noise as interleaved U8 IQ data."""
    t = np.arange(NUM_SAMPLES) / SAMPLE_RATE
    # Complex CW signal
    signal = np.exp(2j * np.pi * SIGNAL_FREQ * t)
    # Add noise
    noise = NOISE_LEVEL * (np.random.randn(NUM_SAMPLES) +
                           1j * np.random.randn(NUM_SAMPLES))
    iq = signal + noise
    # Normalize to [0, 255] for U8
    i_data = np.real(iq)
    q_data = np.imag(iq)
    # Scale to fit U8 range with some headroom
    i_u8 = np.clip(np.round((i_data * 0.4 + 0.5) * 255), 0, 255).astype(np.uint8)
    q_u8 = np.clip(np.round((q_data * 0.4 + 0.5) * 255), 0, 255).astype(np.uint8)
    # Interleave
    interleaved = np.empty(2 * NUM_SAMPLES, dtype=np.uint8)
    interleaved[0::2] = i_u8
    interleaved[1::2] = q_u8
    return interleaved, i_u8, q_u8


# =============================================================================
# Generate FIR test vectors
# =============================================================================
def gen_fir_vectors(i_u8, q_u8):
    """Generate FIR filter coefficients, input, and expected output."""
    # Design lowpass FIR filter
    cutoff = 0.4 / DECIM_RATIO  # Normalized cutoff
    if HAS_SCIPY:
        coeffs = firwin(NUM_TAPS, cutoff)
    else:
        # Simple rectangular window lowpass
        n = np.arange(NUM_TAPS) - (NUM_TAPS - 1) / 2.0
        coeffs = np.sinc(2 * cutoff * n) * np.hamming(NUM_TAPS)
        coeffs = coeffs / np.sum(coeffs)

    # Convert U8 to normalized float [-1, 1]
    i_float = (i_u8.astype(np.float64) - 127.5) / 127.5
    q_float = (q_u8.astype(np.float64) - 127.5) / 127.5

    # Apply FIR filter + decimation
    if HAS_SCIPY:
        i_filtered = lfilter(coeffs, 1.0, i_float)
        q_filtered = lfilter(coeffs, 1.0, q_float)
    else:
        i_filtered = np.convolve(i_float, coeffs, mode='full')[:NUM_SAMPLES]
        q_filtered = np.convolve(q_float, coeffs, mode='full')[:NUM_SAMPLES]

    # Decimate
    i_decimated = i_filtered[::DECIM_RATIO]
    q_decimated = q_filtered[::DECIM_RATIO]
    num_output = len(i_decimated)

    # Convert to fixed-point
    coeffs_fp = float_to_fixedpoint(coeffs, COEFF_W, FRAC_BITS_COEFF)
    i_in_fp   = float_to_fixedpoint(i_float, DATA_W, FRAC_BITS_DATA)
    q_in_fp   = float_to_fixedpoint(q_float, DATA_W, FRAC_BITS_DATA)
    i_out_fp  = float_to_fixedpoint(i_decimated, DATA_W, FRAC_BITS_DATA)
    q_out_fp  = float_to_fixedpoint(q_decimated, DATA_W, FRAC_BITS_DATA)

    return coeffs_fp, i_in_fp, q_in_fp, i_out_fp, q_out_fp, num_output


# =============================================================================
# Generate cross-correlation test vectors
# =============================================================================
def gen_xcorr_vectors():
    """Generate reference/test signals and expected cross-correlation."""
    # Reference: impulse at t=0
    ref = np.zeros(FFT_N, dtype=complex)
    ref[0] = 1.0

    # Test: delayed impulse
    test = np.zeros(FFT_N, dtype=complex)
    test[XCORR_DELAY] = 1.0

    # FFT-based cross-correlation: IFFT(conj(FFT(ref)) * FFT(test))
    if HAS_SCIPY:
        ref_fft = fft(ref)
        test_fft = fft(test)
        xcorr_freq = np.conj(ref_fft) * test_fft
        xcorr_time = ifft(xcorr_freq)
    else:
        ref_fft = np.fft.fft(ref)
        test_fft = np.fft.fft(test)
        xcorr_freq = np.conj(ref_fft) * test_fft
        xcorr_time = np.fft.ifft(xcorr_freq)

    # Magnitude squared
    mag2 = np.abs(xcorr_time)**2

    # Convert to fixed-point
    scale = 0.99  # Keep within range
    ref_re_fp  = float_to_fixedpoint(np.real(ref) * scale, TWIDDLE_W, FRAC_BITS_TW)
    ref_im_fp  = float_to_fixedpoint(np.imag(ref) * scale, TWIDDLE_W, FRAC_BITS_TW)
    test_re_fp = float_to_fixedpoint(np.real(test) * scale, TWIDDLE_W, FRAC_BITS_TW)
    test_im_fp = float_to_fixedpoint(np.imag(test) * scale, TWIDDLE_W, FRAC_BITS_TW)

    # Magnitude^2 in 32-bit unsigned
    mag2_scaled = mag2 * (2**FRAC_BITS_TW)**2
    mag2_int = np.clip(np.round(mag2_scaled), 0, 2**32 - 1).astype(np.uint64)

    return ref_re_fp, ref_im_fp, test_re_fp, test_im_fp, mag2_int


# =============================================================================
# Generate twiddle factors for FFT
# =============================================================================
def gen_twiddle_factors(n):
    """Generate twiddle factors W_N^k = cos(2*pi*k/N) - j*sin(2*pi*k/N)."""
    k = np.arange(n // 2)
    angles = -2.0 * np.pi * k / n
    tw_re = np.cos(angles)
    tw_im = np.sin(angles)  # Note: W = cos - j*sin, so im = -sin
    # But convention: twiddle_im stores -sin(angle) = sin(-angle)
    # angles are already negative, so sin(angles) = -sin(2*pi*k/N) which is correct

    tw_re_fp = float_to_fixedpoint(tw_re, TWIDDLE_W, FRAC_BITS_TW)
    tw_im_fp = float_to_fixedpoint(tw_im, TWIDDLE_W, FRAC_BITS_TW)
    return tw_re_fp, tw_im_fp


# =============================================================================
# Main
# =============================================================================
def main():
    np.random.seed(42)  # Reproducible

    print("HeIMDALL FPGA Test Vector Generator")
    print("=" * 50)

    # --- IQ data ---
    print("\nGenerating interleaved U8 IQ data...")
    interleaved, i_u8, q_u8 = gen_iq_data()
    hex_lines = [f"{v:02x}" for v in interleaved]
    write_hex_file(OUTPUT_DIR / "test_iq_data.hex", hex_lines)

    # --- FIR vectors ---
    print("\nGenerating FIR filter test vectors...")
    coeffs_fp, i_in_fp, q_in_fp, i_out_fp, q_out_fp, num_out = gen_fir_vectors(i_u8, q_u8)

    write_hex_file(OUTPUT_DIR / "fir_coeffs.hex",
                   to_twos_complement_hex(coeffs_fp, COEFF_W))
    write_hex_file(OUTPUT_DIR / "fir_input_i.hex",
                   to_twos_complement_hex(i_in_fp, DATA_W))
    write_hex_file(OUTPUT_DIR / "fir_input_q.hex",
                   to_twos_complement_hex(q_in_fp, DATA_W))
    write_hex_file(OUTPUT_DIR / "fir_output_i.hex",
                   to_twos_complement_hex(i_out_fp, DATA_W))
    write_hex_file(OUTPUT_DIR / "fir_output_q.hex",
                   to_twos_complement_hex(q_out_fp, DATA_W))
    print(f"  FIR: {NUM_TAPS} taps, decim={DECIM_RATIO}, "
          f"{NUM_SAMPLES} in -> {num_out} out")

    # --- Cross-correlation vectors ---
    print("\nGenerating cross-correlation test vectors...")
    ref_re, ref_im, test_re, test_im, mag2 = gen_xcorr_vectors()

    write_hex_file(OUTPUT_DIR / "xcorr_ref_re.hex",
                   to_twos_complement_hex(ref_re, TWIDDLE_W))
    write_hex_file(OUTPUT_DIR / "xcorr_ref_im.hex",
                   to_twos_complement_hex(ref_im, TWIDDLE_W))
    write_hex_file(OUTPUT_DIR / "xcorr_test_re.hex",
                   to_twos_complement_hex(test_re, TWIDDLE_W))
    write_hex_file(OUTPUT_DIR / "xcorr_test_im.hex",
                   to_twos_complement_hex(test_im, TWIDDLE_W))
    write_hex_file(OUTPUT_DIR / "xcorr_expected.hex",
                   [f"{int(v):08x}" for v in mag2])
    print(f"  Xcorr: FFT_N={FFT_N}, delay={XCORR_DELAY} samples")

    # --- Twiddle factors ---
    print("\nGenerating twiddle factors...")
    for n in [FFT_N, FFT_N_LARGE]:
        tw_re, tw_im = gen_twiddle_factors(n)
        suffix = f"_{n}" if n != FFT_N_LARGE else ""
        write_hex_file(OUTPUT_DIR / f"twiddle_re{suffix}.hex",
                       to_twos_complement_hex(tw_re, TWIDDLE_W))
        write_hex_file(OUTPUT_DIR / f"twiddle_im{suffix}.hex",
                       to_twos_complement_hex(tw_im, TWIDDLE_W))
        print(f"  N={n}: {n//2} twiddle factors")

    # Also write the default twiddle files (for FFT_N_LARGE, used by fft_radix2)
    tw_re_default, tw_im_default = gen_twiddle_factors(FFT_N_LARGE)
    write_hex_file(OUTPUT_DIR / "twiddle_re.hex",
                   to_twos_complement_hex(tw_re_default, TWIDDLE_W))
    write_hex_file(OUTPUT_DIR / "twiddle_im.hex",
                   to_twos_complement_hex(tw_im_default, TWIDDLE_W))

    print("\nDone. All test vectors written to:", OUTPUT_DIR)


if __name__ == "__main__":
    main()
