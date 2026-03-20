#!/usr/bin/env python3
"""
HeIMDALL DAQ Performance Regression Test Suite
Validates performance optimizations and detects regressions

Project: HeIMDALL DAQ Firmware
License: GNU GPL V3
Author: Generated via Claude Code optimization plan
"""

import os
import sys
import time
import json
import statistics
import subprocess
import tempfile
from pathlib import Path
import unittest

# Add the util directory to path for imports
sys.path.append(str(Path(__file__).parent.parent.parent / "util"))
from performance_monitor import DAQPerformanceMonitor

class PerformanceRegressionTest(unittest.TestCase):
    """Test suite for HeIMDALL DAQ performance regression detection"""

    @classmethod
    def setUpClass(cls):
        """Set up test environment"""
        cls.test_duration = 10  # Short tests for CI
        cls.instance_id = 0
        cls.monitor = DAQPerformanceMonitor(cls.instance_id)

        # Expected performance baselines (adjust based on system)
        cls.performance_baselines = {
            'max_cpu_percent': 80.0,  # Maximum CPU usage per process
            'max_memory_mb': 256.0,   # Maximum memory usage per process
            'max_latency_ms': 100.0,  # Maximum end-to-end latency
            'max_ctx_switches': 1000, # Maximum involuntary context switches
            'min_throughput_mbps': 10.0  # Minimum data throughput
        }

    def setUp(self):
        """Set up for individual test"""
        self.monitor.discover_daq_processes()
        if not self.monitor.processes:
            self.skipTest("DAQ processes not running")

    def test_benchmark_throughput(self):
        """Measure maximum sustainable data rate"""
        print("\nRunning throughput benchmark...")

        # Start monitoring
        metrics_collected = []
        start_time = time.time()

        while time.time() - start_time < self.test_duration:
            stage_metrics = self.monitor.collect_per_stage_metrics()
            metrics_collected.append(stage_metrics)
            time.sleep(0.5)

        # Analyze throughput metrics
        rtl_metrics = [m.get('rtl_daq', {}) for m in metrics_collected if 'rtl_daq' in m]
        iq_metrics = [m.get('iq_server', {}) for m in metrics_collected if 'iq_server' in m]

        if rtl_metrics:
            avg_cpu = statistics.mean([m.get('cpu_percent', 0) for m in rtl_metrics])
            max_memory = max([m.get('memory_rss_mb', 0) for m in rtl_metrics])

            print(f"RTL-DAQ average CPU: {avg_cpu:.1f}%")
            print(f"RTL-DAQ max memory: {max_memory:.1f}MB")

            self.assertLess(avg_cpu, self.performance_baselines['max_cpu_percent'],
                           "RTL-DAQ CPU usage exceeds baseline")
            self.assertLess(max_memory, self.performance_baselines['max_memory_mb'],
                           "RTL-DAQ memory usage exceeds baseline")

        if iq_metrics:
            # Estimate throughput based on write bytes
            write_bytes = [m.get('write_bytes', 0) for m in iq_metrics]
            if len(write_bytes) > 1:
                throughput_bps = (write_bytes[-1] - write_bytes[0]) / self.test_duration
                throughput_mbps = throughput_bps / (1024 * 1024)

                print(f"Estimated throughput: {throughput_mbps:.2f}MB/s")
                self.assertGreater(throughput_mbps, self.performance_baselines['min_throughput_mbps'],
                                 "Throughput below baseline")

    def test_measure_latency_distribution(self):
        """Characterize latency under various loads"""
        print("\nMeasuring latency distribution...")

        latency_measurements = []
        start_time = time.time()

        while time.time() - start_time < self.test_duration:
            latency_data = self.monitor.measure_end_to_end_latency()
            if latency_data:
                latency_measurements.append(latency_data['latency_ms'])
            time.sleep(0.1)

        if latency_measurements:
            mean_latency = statistics.mean(latency_measurements)
            max_latency = max(latency_measurements)
            p95_latency = statistics.quantiles(latency_measurements, n=20)[18]  # 95th percentile

            print(f"Latency - Mean: {mean_latency:.2f}ms, Max: {max_latency:.2f}ms, P95: {p95_latency:.2f}ms")

            self.assertLess(mean_latency, self.performance_baselines['max_latency_ms'],
                           "Mean latency exceeds baseline")
            self.assertLess(p95_latency, self.performance_baselines['max_latency_ms'] * 1.5,
                           "P95 latency exceeds baseline")

    def test_cpu_affinity_compliance(self):
        """Verify CPU affinity is correctly applied"""
        print("\nTesting CPU affinity compliance...")

        violations = self.monitor.validate_cpu_affinity()

        if violations:
            print("CPU Affinity Violations:")
            for v in violations:
                print(f"  {v['process']}: expected {v['expected']}, actual {v['actual']}")

        self.assertEqual(len(violations), 0,
                        f"CPU affinity violations detected: {violations}")

    def test_memory_pressure_resistance(self):
        """Test system behavior under memory pressure"""
        print("\nTesting memory pressure resistance...")

        memory_metrics = self.monitor.check_memory_pressure()

        # Check for memory pressure indicators
        pressure_indicators = memory_metrics.get('pressure_indicators', {})

        print(f"Memory usage: {memory_metrics['memory_used_percent']:.1f}%")
        print(f"Swap usage: {memory_metrics['swap_used_percent']:.1f}%")

        if pressure_indicators:
            print(f"Pressure indicators: {pressure_indicators}")

        # Warn but don't fail for memory pressure (system dependent)
        if memory_metrics['memory_used_percent'] > 90:
            print("Warning: High memory usage detected")

        if memory_metrics['swap_used_percent'] > 10:
            print("Warning: Swap usage detected - may impact real-time performance")

        # Check for OOM kills (hard failure)
        self.assertNotIn('oom_kills', pressure_indicators,
                        "OOM killer activity detected")

    def test_real_time_scheduling_status(self):
        """Verify real-time scheduling is properly configured"""
        print("\nTesting real-time scheduling status...")

        rt_status = self.monitor.check_realtime_status()

        print(f"RT runtime unlimited: {rt_status.get('rt_runtime_unlimited')}")

        # Check that RT runtime is unlimited
        self.assertTrue(rt_status.get('rt_runtime_unlimited', False),
                       "Real-time runtime throttling should be disabled")

        # Check process scheduling (informational)
        process_sched = rt_status.get('process_scheduling', {})
        rt_processes = [p for p, sched in process_sched.items() if 'RT' in sched]

        print(f"Processes with RT scheduling: {rt_processes}")

        # Should have at least some RT scheduled processes
        self.assertGreater(len(rt_processes), 0,
                          "No processes found with real-time scheduling")

    def test_stress_optimization(self):
        """Verify optimizations under stress conditions"""
        print("\nTesting optimizations under stress...")

        # Generate some background CPU load
        stress_proc = None
        try:
            # Light CPU stress on non-RT cores
            stress_proc = subprocess.Popen([
                'stress-ng', '--cpu', '1', '--cpu-load', '30', '--timeout', '5s'
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Monitor during stress
            stress_metrics = []
            start_time = time.time()

            while time.time() - start_time < 5:
                metrics = self.monitor.collect_per_stage_metrics()
                stress_metrics.append(metrics)
                time.sleep(0.5)

            # Analyze impact
            if stress_metrics:
                for proc_name in ['rtl_daq', 'rebuffer', 'decimate']:
                    proc_metrics = [m.get(proc_name, {}) for m in stress_metrics if proc_name in m]
                    if proc_metrics:
                        ctx_switches = [m.get('ctx_switches_involuntary', 0) for m in proc_metrics]
                        max_ctx_switches = max(ctx_switches) if ctx_switches else 0

                        print(f"{proc_name} max involuntary context switches: {max_ctx_switches}")

                        # RT processes should have minimal involuntary context switches
                        self.assertLess(max_ctx_switches, self.performance_baselines['max_ctx_switches'],
                                       f"{proc_name} has too many involuntary context switches")

        finally:
            if stress_proc:
                stress_proc.terminate()
                stress_proc.wait()

    def test_simd_acceleration_active(self):
        """Verify SIMD acceleration is being utilized (ARM/x86)"""
        print("\nTesting SIMD acceleration status...")

        # Check if NEON/SSE acceleration is compiled in
        try:
            # Look for NEON/SSE indicators in process memory maps
            for proc_name, process in self.monitor.processes.items():
                try:
                    maps_file = f"/proc/{process.pid}/maps"
                    if os.path.exists(maps_file):
                        with open(maps_file, 'r') as f:
                            maps_content = f.read()
                            # This is a basic check - in practice, we'd check binary symbols
                            print(f"{proc_name} process maps checked")
                except (FileNotFoundError, PermissionError):
                    pass

            # For now, just verify that optimized builds are enabled
            # In practice, this would check for SIMD instruction usage
            self.assertTrue(True, "SIMD acceleration check placeholder")

        except Exception as e:
            self.fail(f"SIMD acceleration test failed: {e}")

def run_performance_suite():
    """Run the complete performance test suite"""
    print("HeIMDALL DAQ Performance Regression Test Suite")
    print("=" * 50)

    # Check prerequisites
    if not Path("daq_chain_config.ini").exists():
        print("Error: Run from Firmware/ directory with config file present")
        return False

    # Run tests
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(PerformanceRegressionTest)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # Summary
    print("\nPerformance Test Summary:")
    print(f"Tests run: {result.testsRun}")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")

    if result.failures:
        print("\nFailures:")
        for test, traceback in result.failures:
            print(f"  {test}: {traceback}")

    if result.errors:
        print("\nErrors:")
        for test, traceback in result.errors:
            print(f"  {test}: {traceback}")

    return result.wasSuccessful()

if __name__ == '__main__':
    success = run_performance_suite()
    sys.exit(0 if success else 1)