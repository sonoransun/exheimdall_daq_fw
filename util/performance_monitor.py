#!/usr/bin/env python3
"""
HeIMDALL DAQ Performance Monitoring Utility
Real-time performance metrics collection and analysis

Project: HeIMDALL DAQ Firmware
License: GNU GPL V3
Author: Generated via Claude Code optimization plan
"""

import os
import sys
import time
import json
import psutil
import argparse
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime, timedelta
import subprocess
import threading

class DAQPerformanceMonitor:
    def __init__(self, instance_id=0):
        self.instance_id = instance_id
        self.log_dir = Path(f"_logs/inst{instance_id}")
        self.pid_dir = Path(f"_logs/inst{instance_id}/pids")
        self.metrics_history = defaultdict(deque)
        self.running = False
        self.start_time = None
        self.processes = {}

    def discover_daq_processes(self):
        """Discover running DAQ processes via PID files"""
        process_names = ['rtl_daq', 'rebuffer', 'decimate', 'delay_sync', 'hw_controller', 'iq_server']

        for proc_name in process_names:
            pid_file = self.pid_dir / f"{proc_name}.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    if psutil.pid_exists(pid):
                        process = psutil.Process(pid)
                        self.processes[proc_name] = process
                        print(f"Found {proc_name}: PID {pid}")
                    else:
                        print(f"Warning: PID {pid} for {proc_name} not found")
                except (ValueError, psutil.NoSuchProcess) as e:
                    print(f"Error reading PID for {proc_name}: {e}")

    def measure_end_to_end_latency(self):
        """Measure sample acquisition to output latency"""
        # TODO: Implement using IQ header timestamps and output measurement
        # For now, estimate based on processing delays
        try:
            rtl_process = self.processes.get('rtl_daq')
            delay_process = self.processes.get('delay_sync')

            if not (rtl_process and delay_process):
                return None

            # Estimate latency based on context switches and processing time
            rtl_ctx_switches = rtl_process.num_ctx_switches()
            delay_ctx_switches = delay_process.num_ctx_switches()

            # Basic latency estimation (this would need actual timestamping in production)
            estimated_latency_ms = (rtl_ctx_switches.involuntary + delay_ctx_switches.involuntary) * 0.1

            return {
                'timestamp': time.time(),
                'latency_ms': estimated_latency_ms,
                'rtl_ctx_switches': rtl_ctx_switches.involuntary,
                'delay_ctx_switches': delay_ctx_switches.involuntary
            }
        except Exception as e:
            print(f"Latency measurement error: {e}")
            return None

    def collect_per_stage_metrics(self):
        """Monitor throughput and latency for each pipeline stage"""
        metrics = {}

        for proc_name, process in self.processes.items():
            try:
                cpu_percent = process.cpu_percent()
                memory_info = process.memory_info()
                io_counters = process.io_counters()
                num_threads = process.num_threads()
                ctx_switches = process.num_ctx_switches()

                metrics[proc_name] = {
                    'cpu_percent': cpu_percent,
                    'memory_rss_mb': memory_info.rss / (1024 * 1024),
                    'memory_vms_mb': memory_info.vms / (1024 * 1024),
                    'read_bytes': io_counters.read_bytes,
                    'write_bytes': io_counters.write_bytes,
                    'num_threads': num_threads,
                    'ctx_switches_voluntary': ctx_switches.voluntary,
                    'ctx_switches_involuntary': ctx_switches.involuntary
                }

                # Check CPU affinity if available
                try:
                    affinity = process.cpu_affinity()
                    metrics[proc_name]['cpu_affinity'] = affinity
                except (AttributeError, psutil.AccessDenied):
                    pass

            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                metrics[proc_name] = {'error': str(e)}

        return metrics

    def validate_cpu_affinity(self):
        """Verify processes are bound to correct cores"""
        affinity_violations = []

        expected_affinity = {
            'rtl_daq': [0],      # Core 0
            'rebuffer': [1],     # Core 1
            'decimate': [1],     # Core 1
            'delay_sync': [2],   # Core 2
            'hw_controller': [3], # Core 3
            'iq_server': [2]     # Core 2
        }

        for proc_name, process in self.processes.items():
            try:
                actual_affinity = process.cpu_affinity()
                expected = expected_affinity.get(proc_name, [])

                if expected and actual_affinity != expected:
                    affinity_violations.append({
                        'process': proc_name,
                        'expected': expected,
                        'actual': actual_affinity
                    })
            except (AttributeError, psutil.AccessDenied):
                pass

        return affinity_violations

    def check_memory_pressure(self):
        """Monitor memory allocation and swap usage"""
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()

        pressure_indicators = {}

        # Check for memory pressure indicators
        if memory.percent > 80:
            pressure_indicators['high_memory_usage'] = memory.percent

        if swap.percent > 10:
            pressure_indicators['swap_usage'] = swap.percent

        # Check for OOM killer activity
        try:
            with open('/var/log/kern.log', 'r') as f:
                recent_logs = f.readlines()[-100:]  # Last 100 lines
                oom_kills = [line for line in recent_logs if 'Out of memory' in line]
                if oom_kills:
                    pressure_indicators['oom_kills'] = len(oom_kills)
        except (FileNotFoundError, PermissionError):
            pass

        return {
            'memory_total_gb': memory.total / (1024**3),
            'memory_used_percent': memory.percent,
            'swap_used_percent': swap.percent,
            'pressure_indicators': pressure_indicators
        }

    def check_realtime_status(self):
        """Verify real-time scheduling configuration"""
        rt_status = {}

        # Check RT runtime setting
        try:
            with open('/proc/sys/kernel/sched_rt_runtime_us', 'r') as f:
                rt_runtime = f.read().strip()
                rt_status['rt_runtime_unlimited'] = (rt_runtime == '-1')
        except FileNotFoundError:
            rt_status['rt_runtime_unlimited'] = None

        # Check process scheduling policies
        process_sched = {}
        for proc_name, process in self.processes.items():
            try:
                # Get scheduling info (requires root on some systems)
                with open(f'/proc/{process.pid}/sched', 'r') as f:
                    sched_info = f.read()
                    if 'policy' in sched_info.lower():
                        process_sched[proc_name] = 'RT_SCHED'
                    else:
                        process_sched[proc_name] = 'NORMAL_SCHED'
            except (FileNotFoundError, PermissionError):
                process_sched[proc_name] = 'UNKNOWN'

        rt_status['process_scheduling'] = process_sched
        return rt_status

    def start_monitoring(self, interval=1.0, duration=None):
        """Start continuous performance monitoring"""
        print(f"Starting HeIMDALL performance monitoring (instance {self.instance_id})")
        print(f"Monitoring interval: {interval}s")
        if duration:
            print(f"Duration: {duration}s")

        self.discover_daq_processes()
        if not self.processes:
            print("No DAQ processes found! Make sure the DAQ is running.")
            return

        self.running = True
        self.start_time = time.time()

        try:
            while self.running:
                timestamp = time.time()

                # Collect metrics
                stage_metrics = self.collect_per_stage_metrics()
                latency_metrics = self.measure_end_to_end_latency()
                memory_metrics = self.check_memory_pressure()
                rt_status = self.check_realtime_status()

                # Store in history
                self.metrics_history['stage_metrics'].append((timestamp, stage_metrics))
                if latency_metrics:
                    self.metrics_history['latency'].append((timestamp, latency_metrics))
                self.metrics_history['memory'].append((timestamp, memory_metrics))
                self.metrics_history['rt_status'].append((timestamp, rt_status))

                # Trim history to last 1000 samples
                for key in self.metrics_history:
                    if len(self.metrics_history[key]) > 1000:
                        self.metrics_history[key].popleft()

                # Print periodic summary
                if int(timestamp - self.start_time) % 10 == 0:  # Every 10 seconds
                    self.print_summary()

                # Check duration
                if duration and (timestamp - self.start_time) >= duration:
                    break

                time.sleep(interval)

        except KeyboardInterrupt:
            print("\nMonitoring stopped by user")
        finally:
            self.running = False

    def print_summary(self):
        """Print current performance summary"""
        if not self.metrics_history['stage_metrics']:
            return

        print(f"\n{'='*60}")
        print(f"Performance Summary - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*60}")

        # Latest metrics
        _, latest_metrics = self.metrics_history['stage_metrics'][-1]

        for proc_name, metrics in latest_metrics.items():
            if 'error' in metrics:
                continue

            print(f"\n{proc_name.upper()}:")
            print(f"  CPU: {metrics['cpu_percent']:.1f}%")
            print(f"  Memory: {metrics['memory_rss_mb']:.1f}MB RSS")
            print(f"  Threads: {metrics['num_threads']}")
            if 'cpu_affinity' in metrics:
                print(f"  CPU Affinity: {metrics['cpu_affinity']}")

        # Memory pressure
        if self.metrics_history['memory']:
            _, memory_metrics = self.metrics_history['memory'][-1]
            print(f"\nSYSTEM MEMORY:")
            print(f"  Used: {memory_metrics['memory_used_percent']:.1f}%")
            print(f"  Swap: {memory_metrics['swap_used_percent']:.1f}%")
            if memory_metrics['pressure_indicators']:
                print(f"  Pressure indicators: {memory_metrics['pressure_indicators']}")

        # Affinity violations
        violations = self.validate_cpu_affinity()
        if violations:
            print(f"\nCPU AFFINITY VIOLATIONS:")
            for violation in violations:
                print(f"  {violation['process']}: expected {violation['expected']}, actual {violation['actual']}")

    def export_metrics(self, output_file):
        """Export collected metrics to JSON"""
        output_data = {
            'metadata': {
                'instance_id': self.instance_id,
                'start_time': self.start_time,
                'duration': time.time() - self.start_time if self.start_time else 0,
                'export_time': time.time()
            },
            'metrics': {
                'stage_metrics': list(self.metrics_history['stage_metrics']),
                'latency': list(self.metrics_history['latency']),
                'memory': list(self.metrics_history['memory']),
                'rt_status': list(self.metrics_history['rt_status'])
            }
        }

        with open(output_file, 'w') as f:
            json.dump(output_data, f, indent=2)

        print(f"Metrics exported to {output_file}")

def main():
    parser = argparse.ArgumentParser(description='HeIMDALL DAQ Performance Monitor')
    parser.add_argument('--instance-id', type=int, default=0, help='DAQ instance ID to monitor')
    parser.add_argument('--interval', type=float, default=1.0, help='Monitoring interval in seconds')
    parser.add_argument('--duration', type=int, help='Monitoring duration in seconds')
    parser.add_argument('--export', help='Export metrics to JSON file')
    parser.add_argument('--check-affinity', action='store_true', help='Check CPU affinity only')
    parser.add_argument('--check-memory', action='store_true', help='Check memory pressure only')

    args = parser.parse_args()

    monitor = DAQPerformanceMonitor(args.instance_id)

    if args.check_affinity:
        monitor.discover_daq_processes()
        violations = monitor.validate_cpu_affinity()
        if violations:
            print("CPU Affinity Violations:")
            for v in violations:
                print(f"  {v['process']}: expected {v['expected']}, actual {v['actual']}")
        else:
            print("All processes have correct CPU affinity")
        return

    if args.check_memory:
        memory_metrics = monitor.check_memory_pressure()
        print("Memory Status:")
        print(f"  Total: {memory_metrics['memory_total_gb']:.1f}GB")
        print(f"  Used: {memory_metrics['memory_used_percent']:.1f}%")
        print(f"  Swap: {memory_metrics['swap_used_percent']:.1f}%")
        if memory_metrics['pressure_indicators']:
            print(f"  Pressure: {memory_metrics['pressure_indicators']}")
        return

    # Start monitoring
    try:
        monitor.start_monitoring(args.interval, args.duration)
    finally:
        if args.export:
            monitor.export_metrics(args.export)

if __name__ == '__main__':
    main()