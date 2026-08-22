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

# Return 0 when the PID's current command line (ps -o args=, portable) still
# matches the DAQ component named by its PID file (e.g. rtl_daq.pid ->
# "rtl_daq"). Guards against stale PID files whose PIDs were recycled by
# unrelated processes after a crash, OOM-kill or unclean reboot.
pid_matches_pidfile() {
    local pid="$1"
    local pidfile="$2"
    local name args
    name="${pidfile##*/}"
    name="${name%.pid}"
    [ -n "$name" ] || return 1
    args=$(ps -o args= -p "$pid" 2>/dev/null)
    case "$args" in
        *"$name"*) return 0 ;;
        *) return 1 ;;
    esac
}

wait_or_kill() {
    local pid="$1"
    local pidfile="$2"
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$GRACE_PERIOD" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        if pid_matches_pidfile "$pid" "$pidfile"; then
            sudo kill -KILL "$pid" 2>/dev/null
        else
            echo "Warning: stale PID file $pidfile - PID $pid belongs to an unrelated process, skipping force-kill"
        fi
    fi
}

# Signal every PID recorded under the given pids directory
signal_pid_dir() {
    local dir="$1"
    for pidfile in "$dir"/*.pid; do
        [ -f "$pidfile" ] || continue
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            if pid_matches_pidfile "$pid" "$pidfile"; then
                send_signal "$pid"
            else
                echo "Warning: stale PID file $pidfile - PID $pid belongs to an unrelated process, skipping"
            fi
        fi
    done
}

if [ -n "$INSTANCE_ID" ]; then
    echo "Shutting down DAQ instance $INSTANCE_ID .."
    PID_DIR="_logs/inst${INSTANCE_ID}/pids"
    if [ -d "$PID_DIR" ]; then
        # Send signal to all processes first
        signal_pid_dir "$PID_DIR"
        # Wait for graceful exit, then force-kill stragglers
        for pidfile in "$PID_DIR"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null && ! pid_matches_pidfile "$pid" "$pidfile"; then
                echo "Warning: stale PID file $pidfile - PID $pid belongs to an unrelated process, skipping"
            else
                wait_or_kill "$pid" "$pidfile"
            fi
            rm -f "$pidfile"
        done
        echo "Instance $INSTANCE_ID stopped"
    else
        echo "Warning: No PID directory found for instance $INSTANCE_ID at $PID_DIR"
    fi
else
    echo "Shut down DAQ chain .."

    # Primary path: the PID files the start scripts write for every instance
    # (including instance 0). This also catches processes launched via
    # non-default python interpreters that name-matching would miss.
    for pid_dir in _logs/inst*/pids; do
        [ -d "$pid_dir" ] || continue
        signal_pid_dir "$pid_dir"
    done

    # Fallback orphan sweep by process name (covers processes whose PID files
    # are missing/stale). Only the process names the start scripts actually
    # launch are listed here.
    if [ "$USE_SIGTERM" = true ]; then
        sudo pkill -TERM rtl_daq.out 2>/dev/null
        sudo pkill -TERM decimate.out 2>/dev/null
        sudo pkill -TERM rebuffer.out 2>/dev/null
        sudo pkill -TERM iq_server.out 2>/dev/null
        sudo pkill -TERM -f "python.*_testing/test_data_synthesizer.py" 2>/dev/null
        sudo pkill -TERM -f "python.*_daq_core/delay_sync.py" 2>/dev/null
        sudo pkill -TERM -f "python.*_daq_core/hw_controller.py" 2>/dev/null
    else
        sudo pkill -64 rtl_daq.out 2>/dev/null
        sudo pkill -64 decimate.out 2>/dev/null
        sudo pkill -64 rebuffer.out 2>/dev/null
        sudo pkill -64 iq_server.out 2>/dev/null
        sudo pkill -64 -f "python.*_testing/test_data_synthesizer.py" 2>/dev/null
        sudo pkill -64 -f "python.*_daq_core/delay_sync.py" 2>/dev/null
        sudo pkill -64 -f "python.*_daq_core/hw_controller.py" 2>/dev/null
    fi

    # Grace period then force-kill any survivors
    sleep "$GRACE_PERIOD"
    for pid_dir in _logs/inst*/pids; do
        [ -d "$pid_dir" ] || continue
        for pidfile in "$pid_dir"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                if pid_matches_pidfile "$pid" "$pidfile"; then
                    sudo kill -KILL "$pid" 2>/dev/null
                else
                    echo "Warning: stale PID file $pidfile - PID $pid belongs to an unrelated process, skipping force-kill"
                fi
            fi
        done
    done
    sudo pkill -KILL rtl_daq.out 2>/dev/null
    sudo pkill -KILL decimate.out 2>/dev/null
    sudo pkill -KILL rebuffer.out 2>/dev/null
    sudo pkill -KILL iq_server.out 2>/dev/null
    sudo pkill -KILL -f "python.*_testing/test_data_synthesizer.py" 2>/dev/null

    # Clean up PID files
    find _logs/inst*/pids -name "*.pid" -exec rm -f {} \; 2>/dev/null
fi
