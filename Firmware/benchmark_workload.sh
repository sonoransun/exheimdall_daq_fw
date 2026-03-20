#!/bin/bash
#
# HeIMDALL DAQ Benchmark Workload Script
# Generates synthetic load for performance testing and optimization validation
#
# Project: HeIMDALL DAQ Firmware
# License: GNU GPL V3
# Author: Generated via Claude Code optimization plan

set -e

# Configuration
BENCHMARK_DURATION=${1:-30}  # Default 30 seconds
INSTANCE_ID=${2:-0}         # Default instance 0
OUTPUT_DIR="_testing/benchmark_results"

echo "HeIMDALL DAQ Performance Benchmark"
echo "==================================="
echo "Duration: ${BENCHMARK_DURATION}s"
echo "Instance ID: ${INSTANCE_ID}"

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Check if DAQ is running
if ! pgrep -f "rtl_daq.out" > /dev/null; then
    echo "Error: DAQ not running. Start with ./daq_synthetic_start.sh first."
    exit 1
fi

# Start performance monitoring in background
echo "Starting performance monitoring..."
python3 ../util/performance_monitor.py \
    --instance-id "$INSTANCE_ID" \
    --duration "$BENCHMARK_DURATION" \
    --export "$OUTPUT_DIR/benchmark_$(date +%Y%m%d_%H%M%S).json" &
MONITOR_PID=$!

# Generate synthetic network load to test iq_server
echo "Generating synthetic network load..."
python3 - <<EOF &
import socket
import time
import threading

def network_load_test():
    """Generate synthetic network connections to iq_server"""
    port = 5000 + ($INSTANCE_ID * 100)

    for i in range(10):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(('localhost', port))

            # Read some IQ data
            data = sock.recv(4096)
            sock.close()
            time.sleep(0.1)
        except Exception as e:
            pass

# Run network load test in background
threading.Thread(target=network_load_test).start()
time.sleep($BENCHMARK_DURATION)
EOF
NETWORK_PID=$!

# Generate CPU stress on non-RT cores
echo "Generating CPU stress on non-RT cores..."
stress_cores=""
num_cores=$(nproc)
if [ $num_cores -ge 4 ]; then
    # Use cores 0 and 3 (not used by main pipeline)
    stress_cores="0,3"
else
    # Use any available core
    stress_cores="0"
fi

stress-ng --cpu 2 --cpu-load 50 --timeout ${BENCHMARK_DURATION}s --taskset $stress_cores &
STRESS_PID=$!

# Generate I/O load
echo "Generating I/O load..."
dd if=/dev/zero of=/tmp/benchmark_io_test bs=1M count=100 &
IO_PID=$!

# Wait for benchmark duration
echo "Running benchmark for ${BENCHMARK_DURATION} seconds..."
sleep $BENCHMARK_DURATION

# Clean up background processes
echo "Cleaning up..."
kill $MONITOR_PID 2>/dev/null || true
kill $NETWORK_PID 2>/dev/null || true
kill $STRESS_PID 2>/dev/null || true
kill $IO_PID 2>/dev/null || true

# Remove temporary files
rm -f /tmp/benchmark_io_test

# Wait for monitoring to complete
wait $MONITOR_PID 2>/dev/null || true

echo "Benchmark completed. Results in $OUTPUT_DIR/"
echo "Performance summary:"

# Quick CPU affinity check
if command -v python3 > /dev/null; then
    python3 ../util/performance_monitor.py --instance-id "$INSTANCE_ID" --check-affinity
fi

# Quick memory check
echo ""
echo "Final memory status:"
free -h

echo ""
echo "Benchmark workload completed successfully!"