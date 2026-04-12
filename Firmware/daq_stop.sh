#!/bin/sh
#
#   DAQ chain stop script
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#
#   Usage:
#     ./daq_stop.sh           # Stop all DAQ instances (global kill)
#     ./daq_stop.sh <id>      # Stop only instance <id> using PID files
#     ./daq_stop.sh --legacy  # Use legacy SIGRT(64) instead of SIGTERM

GRACE_PERIOD=2
USE_SIGTERM=true

for arg in "$@"; do
    case "$arg" in
        --legacy) USE_SIGTERM=false ;;
    esac
done

# Filter out flags to get instance ID
INSTANCE_ID=""
for arg in "$@"; do
    case "$arg" in
        --*) ;;
        *) INSTANCE_ID="$arg" ;;
    esac
done

send_signal() {
    local pid="$1"
    if [ "$USE_SIGTERM" = true ]; then
        sudo kill -TERM "$pid" 2>/dev/null
    else
        sudo kill -64 "$pid" 2>/dev/null
    fi
}

wait_or_kill() {
    local pid="$1"
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$GRACE_PERIOD" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        sudo kill -KILL "$pid" 2>/dev/null
    fi
}

if [ -n "$INSTANCE_ID" ]; then
    echo "Shutting down DAQ instance $INSTANCE_ID .."
    PID_DIR="_logs/inst${INSTANCE_ID}/pids"
    if [ -d "$PID_DIR" ]; then
        # Send signal to all processes first
        for pidfile in "$PID_DIR"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                send_signal "$pid"
            fi
        done
        # Wait for graceful exit, then force-kill stragglers
        for pidfile in "$PID_DIR"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            wait_or_kill "$pid"
            rm -f "$pidfile"
        done
        echo "Instance $INSTANCE_ID stopped"
    else
        echo "Warning: No PID directory found for instance $INSTANCE_ID at $PID_DIR"
    fi
else
    echo "Shut down DAQ chain .."

    # Send SIGTERM (or legacy SIGRT) to all pipeline processes
    if [ "$USE_SIGTERM" = true ]; then
        sudo pkill -TERM rtl_daq.out 2>/dev/null
        sudo kill -TERM $(ps ax | grep "[p]ython3 _testing/test_data_synthesizer.py" | awk '{print $1}') 2>/dev/null
        sudo pkill -TERM sync.out 2>/dev/null
        sudo pkill -TERM decimate.out 2>/dev/null
        sudo pkill -TERM rebuffer.out 2>/dev/null
        sudo kill -TERM $(ps ax | grep "[p]ython3 _daq_core/delay_sync.py" | awk '{print $1}') 2>/dev/null
        sudo kill -TERM $(ps ax | grep "[p]ython3 _daq_core/hw_controller.py" | awk '{print $1}') 2>/dev/null
        sudo kill -TERM $(ps ax | grep "[p]ython3 _daq_core/iq_eth_sink.py" | awk '{print $1}') 2>/dev/null
        sudo pkill -TERM iq_server.out 2>/dev/null
    else
        sudo pkill -64 rtl_daq.out 2>/dev/null
        sudo kill -64 $(ps ax | grep "[p]ython3 _testing/test_data_synthesizer.py" | awk '{print $1}') 2>/dev/null
        sudo pkill -64 sync.out 2>/dev/null
        sudo pkill -64 decimate.out 2>/dev/null
        sudo pkill -64 rebuffer.out 2>/dev/null
        sudo kill -64 $(ps ax | grep "[p]ython3 _daq_core/delay_sync.py" | awk '{print $1}') 2>/dev/null
        sudo kill -64 $(ps ax | grep "[p]ython3 _daq_core/hw_controller.py" | awk '{print $1}') 2>/dev/null
        sudo kill -64 $(ps ax | grep "[p]ython3 _daq_core/iq_eth_sink.py" | awk '{print $1}') 2>/dev/null
        sudo pkill -64 iq_server.out 2>/dev/null
    fi

    # Grace period then force-kill any survivors
    sleep "$GRACE_PERIOD"
    sudo pkill -KILL rtl_daq.out 2>/dev/null
    sudo pkill -KILL decimate.out 2>/dev/null
    sudo pkill -KILL rebuffer.out 2>/dev/null
    sudo pkill -KILL iq_server.out 2>/dev/null
    sudo pkill -KILL sync.out 2>/dev/null

    # Clean up PID files
    find _logs/inst*/pids -name "*.pid" -exec rm -f {} \; 2>/dev/null
fi
