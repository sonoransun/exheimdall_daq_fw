#!/bin/bash
#
# HeIMDALL DAQ benchmark workload (spec I.6)
#
# Streams IQ frames through a real client (util/performance_monitor.py:
# `streaming` handshake + per-frame `IQDownload` on port 5000 + N*port_stride)
# while it records per-stage metrics, end-to-end latency
# (receive time - header time_stamp) and throughput, and loads the host with
# CPU stress (stress-ng, cores outside the instance when the host has them)
# and file I/O.
#
#   ./benchmark_workload.sh [DURATION_SEC] [INSTANCE_ID]
#
# The IQ client needs [data_interface] out_data_iface_type = eth; with
# iq_server_max_clients = 1 it occupies the only client slot for the run.
# Results: _testing/benchmark_results/benchmark_<date>.json (under Firmware/).
# Needs psutil (the monitor's continuous mode). Exit 2 on bad arguments,
# 1 when psutil is missing, the chain is not running or no result was written.
#
# Project: HeIMDALL DAQ Firmware
# License: GNU GPL V3

set -e
cd "$(dirname "$0")"

BENCHMARK_DURATION=${1:-30}
INSTANCE_ID=${2:-0}
OUTPUT_DIR="_testing/benchmark_results"
case "$BENCHMARK_DURATION" in ''|*[!0-9]*) echo "Error: DURATION_SEC must be an integer"; exit 2 ;; esac
case "$INSTANCE_ID" in ''|*[!0-9]*) echo "Error: INSTANCE_ID must be an integer"; exit 2 ;; esac

echo "HeIMDALL DAQ Performance Benchmark"
echo "==================================="
echo "Duration: ${BENCHMARK_DURATION}s"
echo "Instance ID: ${INSTANCE_ID}"

if ! python3 -c 'import psutil' 2> /dev/null; then
    echo "Error: the Python module psutil is required (pip install psutil)"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# The chain runs with real hardware (rtl_daq.out) or in synthetic mode
# (test_data_synthesizer.py; daq_synthetic_start.sh never launches rtl_daq.out)
if ! pgrep -f "rtl_daq.out" > /dev/null && \
   ! pgrep -f "test_data_synthesizer.py" > /dev/null; then
    echo "Error: DAQ not running. Start with ./daq_start_sm.sh or ./daq_synthetic_start.sh first."
    exit 1
fi

IQ_ARGS=()
if pgrep -f "iq_server.out" > /dev/null; then
    IQ_ARGS=(--iq-port auto)
    echo "Streaming through an IQ client (latency and throughput are recorded)"
else
    echo "Note: iq_server.out is not running (out_data_iface_type is not eth) - no IQ client latency"
fi

RESULT="$OUTPUT_DIR/benchmark_$(date +%Y%m%d_%H%M%S).json"
echo "Starting performance monitoring..."
python3 ../util/performance_monitor.py \
    --instance-id "$INSTANCE_ID" \
    --duration "$BENCHMARK_DURATION" \
    "${IQ_ARGS[@]}" \
    --export "$RESULT" &
MONITOR_PID=$!

# CPU stress on cores the instance does not use (its 4 RT tiers sit on
# (N*4 + k) % nproc by default); a host with <= 4 cores has none to spare.
num_cores=$(nproc)
first_free=$(( (INSTANCE_ID + 1) * 4 ))
STRESS_PID=""
if command -v stress-ng > /dev/null 2>&1; then
    if [ "$num_cores" -gt "$first_free" ]; then
        stress_cores="${first_free}-$((num_cores - 1))"
    else
        stress_cores="0-$((num_cores - 1))"
        echo "Note: no core outside the instance's RT cores - stress shares them"
    fi
    echo "Generating CPU stress on cores ${stress_cores}..."
    stress-ng --cpu 2 --cpu-load 50 --timeout "${BENCHMARK_DURATION}s" --taskset "$stress_cores" > /dev/null &
    STRESS_PID=$!
else
    echo "Note: stress-ng not installed - skipping CPU stress load"
fi

echo "Generating I/O load..."
IO_FILE=$(mktemp "${TMPDIR:-/tmp}/heimdall_benchmark_io.XXXXXX")
dd if=/dev/zero of="$IO_FILE" bs=1M count=100 2> /dev/null &
IO_PID=$!

echo "Running benchmark for ${BENCHMARK_DURATION} seconds..."
# The monitor exits by itself after --duration
wait "$MONITOR_PID" || echo "Warning: performance monitor exited with status $?"

echo "Cleaning up..."
if [ -n "$STRESS_PID" ]; then
    kill "$STRESS_PID" 2>/dev/null || true
fi
wait "$IO_PID" 2>/dev/null || true
rm -f "$IO_FILE"

if [ ! -f "$RESULT" ]; then
    echo "Error: the performance monitor wrote no results ($RESULT)"
    exit 1
fi
echo "Benchmark completed. Results in $RESULT"
echo "Performance summary:"
python3 ../util/performance_monitor.py --instance-id "$INSTANCE_ID" --check-affinity --check-rt || true

echo ""
echo "Final memory status:"
free -h 2>/dev/null || true

echo ""
echo "Benchmark workload completed."
