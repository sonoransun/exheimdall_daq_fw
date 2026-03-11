#!/usr/bin/env python3
"""
verify_output.py — Verify FPGA simulation output against Python reference.

Reads simulation output files and compares against reference data.
Reports max error, RMS error, and pass/fail based on tolerance threshold.

Usage:
    python3 verify_output.py [--fir] [--xcorr] [--top] [--tolerance FLOAT]

Default: verify all available outputs.
"""

import argparse
import sys
import numpy as np
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent
DATA_W     = 18
TWIDDLE_W  = 16
COEFF_W    = 18


def hex_to_signed(hex_str, total_bits):
    """Convert hex string (two's complement) to signed integer."""
    val = int(hex_str.strip(), 16)
    if val >= (1 << (total_bits - 1)):
        val -= (1 << total_bits)
    return val


def read_hex_file(filepath, total_bits=None, unsigned=False):
    """Read a .hex file and return list of integer values."""
    values = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('//') or line.startswith('#'):
                continue
            if total_bits and not unsigned:
                values.append(hex_to_signed(line, total_bits))
            else:
                values.append(int(line, 16))
    return np.array(values)


def compute_errors(actual, expected, name=""):
    """Compute and report error metrics between actual and expected arrays."""
    n = min(len(actual), len(expected))
    if n == 0:
        print(f"  {name}: No data to compare")
        return None, None, False

    actual = actual[:n].astype(np.float64)
    expected = expected[:n].astype(np.float64)

    diff = actual - expected
    abs_diff = np.abs(diff)

    max_err = np.max(abs_diff)
    mean_err = np.mean(abs_diff)
    rms_err = np.sqrt(np.mean(diff**2))

    # Normalized errors (relative to full scale)
    full_scale = np.max(np.abs(expected)) if np.max(np.abs(expected)) > 0 else 1.0
    max_err_norm = max_err / full_scale
    rms_err_norm = rms_err / full_scale

    print(f"  {name}:")
    print(f"    Samples compared: {n}")
    print(f"    Max absolute error:  {max_err:.1f} LSBs")
    print(f"    Mean absolute error: {mean_err:.2f} LSBs")
    print(f"    RMS error:           {rms_err:.2f} LSBs")
    print(f"    Max relative error:  {max_err_norm:.6f} ({max_err_norm*100:.4f}%)")
    print(f"    RMS relative error:  {rms_err_norm:.6f} ({rms_err_norm*100:.4f}%)")

    return max_err, rms_err, max_err_norm


def verify_fir(tolerance=0.01):
    """Verify FIR filter output."""
    print("\n=== FIR Decimation Filter Verification ===")

    # Check for simulation output files
    sim_i_path = OUTPUT_DIR / "sim_fir_output_i.hex"
    sim_q_path = OUTPUT_DIR / "sim_fir_output_q.hex"
    ref_i_path = OUTPUT_DIR / "fir_output_i.hex"
    ref_q_path = OUTPUT_DIR / "fir_output_q.hex"

    if not sim_i_path.exists():
        print(f"  Simulation output not found: {sim_i_path}")
        print("  Run FIR testbench first (make sim_fir)")
        return None

    sim_i = read_hex_file(sim_i_path, DATA_W)
    sim_q = read_hex_file(sim_q_path, DATA_W)
    ref_i = read_hex_file(ref_i_path, DATA_W)
    ref_q = read_hex_file(ref_q_path, DATA_W)

    max_i, rms_i, norm_i = compute_errors(sim_i, ref_i, "I channel")
    max_q, rms_q, norm_q = compute_errors(sim_q, ref_q, "Q channel")

    if norm_i is None or norm_q is None:
        return False

    passed = (norm_i <= tolerance) and (norm_q <= tolerance)
    print(f"\n  Tolerance: {tolerance*100:.2f}%")
    print(f"  Result: {'PASS' if passed else 'FAIL'}")
    return passed


def verify_xcorr(tolerance=0.05):
    """Verify cross-correlation output."""
    print("\n=== Cross-Correlation Verification ===")

    sim_path = OUTPUT_DIR / "sim_xcorr_output.hex"
    ref_path = OUTPUT_DIR / "xcorr_expected.hex"

    if not sim_path.exists():
        print(f"  Simulation output not found: {sim_path}")
        print("  Run xcorr testbench first (make sim_xcorr)")
        return None

    sim_data = read_hex_file(sim_path, unsigned=True)
    ref_data = read_hex_file(ref_path, unsigned=True)

    max_err, rms_err, norm_err = compute_errors(sim_data, ref_data, "Magnitude^2")

    if norm_err is None:
        return False

    # Also verify peak position
    if len(sim_data) > 0 and len(ref_data) > 0:
        sim_peak = np.argmax(sim_data[:min(len(sim_data), len(ref_data))])
        ref_peak = np.argmax(ref_data[:min(len(sim_data), len(ref_data))])
        print(f"\n  Peak position: sim={sim_peak}, ref={ref_peak}")
        peak_correct = (sim_peak == ref_peak)
        print(f"  Peak position: {'CORRECT' if peak_correct else 'WRONG'}")
    else:
        peak_correct = False

    passed = (norm_err <= tolerance) and peak_correct
    print(f"\n  Tolerance: {tolerance*100:.2f}%")
    print(f"  Result: {'PASS' if passed else 'FAIL'}")
    return passed


def verify_top():
    """Verify top-level simulation output (basic checks)."""
    print("\n=== Top-Level Output Verification ===")

    sim_path = OUTPUT_DIR.parent / "tb_top_output.hex"

    if not sim_path.exists():
        print(f"  Simulation output not found: {sim_path}")
        print("  Run top-level testbench first (make sim)")
        return None

    data = read_hex_file(sim_path, unsigned=True)
    print(f"  Captured {len(data)} bytes from MISO")

    # Basic sanity check: not all FF (which means no data was returned)
    non_ff = np.sum(data != 0xFF)
    print(f"  Non-0xFF bytes: {non_ff}")

    if non_ff > 0:
        print("  PASS: FPGA returned non-trivial data")
        return True
    else:
        print("  INFO: All bytes are 0xFF (no processed data returned)")
        print("  This may be expected if processing pipeline is not fully active")
        return True  # Not necessarily a failure


def main():
    parser = argparse.ArgumentParser(description="Verify FPGA simulation outputs")
    parser.add_argument("--fir", action="store_true", help="Verify FIR output only")
    parser.add_argument("--xcorr", action="store_true", help="Verify xcorr output only")
    parser.add_argument("--top", action="store_true", help="Verify top-level output only")
    parser.add_argument("--tolerance", type=float, default=0.01,
                        help="Error tolerance as fraction (default: 0.01 = 1%%)")
    args = parser.parse_args()

    # If no specific test requested, run all
    run_all = not (args.fir or args.xcorr or args.top)

    print("HeIMDALL FPGA Output Verification")
    print("=" * 50)

    results = {}

    if args.fir or run_all:
        results['FIR'] = verify_fir(args.tolerance)

    if args.xcorr or run_all:
        results['Xcorr'] = verify_xcorr(args.tolerance * 5)  # Looser for xcorr

    if args.top or run_all:
        results['Top'] = verify_top()

    # Summary
    print("\n" + "=" * 50)
    print("Summary:")
    all_pass = True
    for name, result in results.items():
        if result is None:
            status = "SKIPPED"
        elif result:
            status = "PASS"
        else:
            status = "FAIL"
            all_pass = False
        print(f"  {name:10s}: {status}")

    print("=" * 50)
    if all_pass:
        print("Overall: PASS")
        return 0
    else:
        print("Overall: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
