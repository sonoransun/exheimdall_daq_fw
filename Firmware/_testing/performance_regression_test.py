#!/usr/bin/env python3
"""
HeIMDALL DAQ performance regression checks against a RUNNING chain (spec I.6).

Throughput and latency are measured through a real IQ client
(util/performance_monitor.py: `streaming` handshake + per-frame
`IQDownload`), so they need [data_interface] out_data_iface_type = eth:
  throughput = frames x bytes / elapsed      (bytes as received)
  latency    = receive time - header.time_stamp
Expected CPU affinity comes from the instance's registry entry (cores), else
from [host] cpu_cores, else the default (N*4 + k) % nproc layout; RT status
from /proc/<pid>/sched.

Run from Firmware/ while the chain runs: python3 _testing/performance_regression_test.py
Every live check skips (with a reason) when the chain, the IQ server or an
optional tool is not available, so it never fails on a CI box. Not part of
unit_test.sh --ci.

Project: HeIMDALL DAQ Firmware
License: GNU GPL V3
"""

import configparser
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

FIRMWARE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FIRMWARE_DIR.parent / "util"))
try:
    from performance_monitor import DAQPerformanceMonitor, measure_stream, psutil, _iq_port_for
    _MONITOR_IMPORT_ERROR = None
except Exception as _e:  # Linux-only interfaces, missing modules, ...
    DAQPerformanceMonitor = None
    _MONITOR_IMPORT_ERROR = _e

#: The latency budget: a frame is stamped when its CPI is acquired, so its
#: end-to-end latency is at least one frame period plus processing.
LATENCY_FRAME_PERIODS = 2.0
LATENCY_SLACK_MS = 500.0
#: Delivered frame rate must reach this share of 1 / frame period.
MIN_FPS_FRACTION = 0.5


def _frame_period_s():
    p = configparser.ConfigParser(interpolation=None)
    p.read(str(FIRMWARE_DIR / "daq_chain_config.ini"))
    try:
        return (p.getint("pre_processing", "cpi_size") * p.getint("pre_processing", "decimation_ratio")
                / p.getfloat("daq", "sample_rate"))
    except (configparser.Error, ValueError, ZeroDivisionError):
        return None


class PerformanceRegressionTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if DAQPerformanceMonitor is None:
            raise unittest.SkipTest("SKIP reason: performance_monitor unavailable ({0})".format(
                _MONITOR_IMPORT_ERROR))
        if not sys.platform.startswith("linux"):
            raise unittest.SkipTest("SKIP reason: live performance checks are Linux-only (/proc, RT scheduling)")
        cls.test_duration = 10.0
        cls.instance_id = 0
        cls.monitor = DAQPerformanceMonitor(cls.instance_id, str(FIRMWARE_DIR))
        cls.frame_period = _frame_period_s()
        cls.baselines = {"max_cpu_percent": 80.0, "max_memory_mb": 256.0, "max_ctx_switches": 1000}
        cls._stream = None

    def setUp(self):
        self.monitor.discover_daq_processes(quiet=True)
        if not self.monitor.processes:
            self.skipTest("SKIP reason: DAQ processes not running")

    def _stream_result(self):
        """One streaming run shared by the throughput and latency tests."""
        if "iq_server" not in self.monitor.processes:
            self.skipTest("SKIP reason: iq_server not running (out_data_iface_type is not eth)")
        if PerformanceRegressionTest._stream is None:
            port = _iq_port_for(str(FIRMWARE_DIR), self.instance_id)
            try:
                PerformanceRegressionTest._stream = measure_stream("127.0.0.1", port,
                                                                   duration=self.test_duration)
            except (OSError, ConnectionError) as e:
                self.skipTest("SKIP reason: IQ client on port {0} failed: {1}".format(port, e))
        return PerformanceRegressionTest._stream

    def test_benchmark_throughput(self):
        """Delivered throughput = frames x bytes / elapsed through a real client."""
        res = self._stream_result()
        print("\nIQ client: {0} frames, {1} bytes in {2:.2f} s -> {3:.2f} MB/s, {4:.2f} frames/s, "
              "cpi gaps {5}".format(res["frames"], res["bytes"], res["elapsed_s"], res["throughput_MBps"] or 0,
                                    res["fps"] or 0, res["cpi_gaps"]))
        self.assertGreater(res["frames"], 0, "no frame received through the IQ client")
        self.assertAlmostEqual(res["throughput_MBps"], res["bytes"] / res["elapsed_s"] / 1e6)
        if self.frame_period:
            self.assertGreaterEqual(res["fps"], MIN_FPS_FRACTION / self.frame_period,
                                    "delivered frame rate below {0:.0%} of 1/frame period".format(MIN_FPS_FRACTION))

    def test_measure_latency_distribution(self):
        """latency = receive time - header time_stamp, p95 within budget."""
        res = self._stream_result()
        if not res.get("latency_samples"):
            self.skipTest("SKIP reason: no frame carried a time_stamp")
        print("\nLatency p50 {0:.1f} ms, p95 {1:.1f} ms, max {2:.1f} ms over {3} frames".format(
            res["latency_p50_ms"], res["latency_p95_ms"], res["latency_max_ms"], res["latency_samples"]))
        if self.frame_period:
            budget = LATENCY_FRAME_PERIODS * self.frame_period * 1000.0 + LATENCY_SLACK_MS
            self.assertLess(res["latency_p95_ms"], budget, "p95 latency above {0:.0f} ms".format(budget))

    def test_cpu_affinity_compliance(self):
        if not self.monitor.chain_pinned():
            self.skipTest("SKIP reason: synthetic chain without --rt (stages are not pinned)")
        violations = self.monitor.validate_cpu_affinity()
        for v in violations:
            print("  {0}: expected {1}, actual {2} ({3})".format(v["process"], v["expected"], v["actual"], v["source"]))
        self.assertEqual(violations, [], "CPU affinity violations detected")

    def test_memory_pressure_resistance(self):
        if psutil is None:
            self.skipTest("SKIP reason: psutil not installed")
        mem = self.monitor.check_memory_pressure()
        print("\nMemory usage: {0:.1f}%, swap {1:.1f}%".format(mem["memory_used_percent"], mem["swap_used_percent"]))
        self.assertNotIn("oom_kills", mem["pressure_indicators"], "OOM killer activity detected")

    def test_real_time_scheduling_status(self):
        rt = self.monitor.check_realtime_status()
        print("\nRT runtime unlimited: {0}".format(rt["rt_runtime_unlimited"]))
        sched = rt["process_scheduling"]
        for name, info in sched.items():
            print("  {0}: {1} rt_priority {2} (expected {3})".format(
                name, info.get("policy_name"), info.get("rt_priority"), info.get("expected_rt_priority")))
        if "rtl_daq" not in sched:
            self.skipTest("SKIP reason: synthetic chain (stages run RT only with daq_synthetic_start.sh --rt)")
        self.assertTrue(rt["rt_runtime_unlimited"], "real-time runtime throttling should be disabled")
        for name in ("rtl_daq", "rebuffer", "decimate", "delay_sync"):
            if name in sched and sched[name].get("rt") is not None:
                self.assertTrue(sched[name]["rt"], "{0} does not run SCHED_FIFO/RR".format(name))
                if sched[name].get("rt_priority") is not None:
                    self.assertEqual(sched[name]["rt_priority"], sched[name]["expected_rt_priority"],
                                     "{0} RT priority differs from its tier".format(name))

    @unittest.skipUnless(shutil.which("stress-ng"), "SKIP reason: stress-ng not installed")
    def test_stress_optimization(self):
        if psutil is None:
            self.skipTest("SKIP reason: psutil not installed")
        stress = subprocess.Popen(["stress-ng", "--cpu", "1", "--cpu-load", "30", "--timeout", "5s"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            before = self.monitor.collect_per_stage_metrics()
            time.sleep(5)
            after = self.monitor.collect_per_stage_metrics()
        finally:
            stress.terminate()
            stress.wait()
        for name in ("rtl_daq", "rebuffer", "decimate"):
            if "ctx_switches_involuntary" in before.get(name, {}) and "ctx_switches_involuntary" in after.get(name, {}):
                delta = after[name]["ctx_switches_involuntary"] - before[name]["ctx_switches_involuntary"]
                print("{0}: {1} involuntary context switches under stress".format(name, delta))
                self.assertLess(delta, self.baselines["max_ctx_switches"],
                                "{0} has too many involuntary context switches".format(name))

    def test_simd_acceleration_active(self):
        """decimate.out carries a real FIR offload engine (KFR/NEON/Ne10 or generic)."""
        decimate_bin = FIRMWARE_DIR / "_daq_core" / "decimate.out"
        if not decimate_bin.exists():
            self.skipTest("SKIP reason: decimate.out not built")
        blob = decimate_bin.read_bytes()
        found_simd = [m.decode() for m in (b"kfr", b"KFR", b"neon", b"NEON", b"Ne10", b"ne10") if m in blob]
        found_generic = [m.decode() for m in (b"generic", b"cpu_generic") if m in blob]
        print("\nEngine markers in decimate.out - SIMD: {0}, generic: {1}".format(found_simd, found_generic))
        self.assertTrue(found_simd or found_generic, "decimate.out contains no known FIR engine marker")


def run_performance_suite():
    print("HeIMDALL DAQ Performance Regression Test Suite\n" + "=" * 50)
    if not (FIRMWARE_DIR / "daq_chain_config.ini").exists():
        print("Error: daq_chain_config.ini not found in {0}".format(FIRMWARE_DIR))
        return False
    suite = unittest.TestLoader().loadTestsFromTestCase(PerformanceRegressionTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\nTests run: {0}, failures: {1}, errors: {2}, skipped: {3}".format(
        result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))
    return result.wasSuccessful()


if __name__ == "__main__":
    sys.exit(0 if run_performance_suite() else 1)
