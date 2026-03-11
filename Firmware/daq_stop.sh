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

INSTANCE_ID="${1:-}"

if [ -n "$INSTANCE_ID" ]; then
    echo "Shutting down DAQ instance $INSTANCE_ID .."
    PID_DIR="_logs/inst${INSTANCE_ID}/pids"
    if [ -d "$PID_DIR" ]; then
        for pidfile in "$PID_DIR"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                sudo kill -64 "$pid" 2>/dev/null
            fi
            rm -f "$pidfile"
        done
        echo "Instance $INSTANCE_ID stopped"
    else
        echo "Warning: No PID directory found for instance $INSTANCE_ID at $PID_DIR"
    fi
else
    echo "Shut down DAQ chain .."
    sudo pkill -64 rtl_daq.out
    sudo kill -64 $(ps ax | grep "[p]ython3 _testing/test_data_synthesizer.py" | awk '{print $1}') 2> /dev/null
    sudo pkill -64 sync.out
    sudo pkill -64 decimate.out
    sudo pkill -64 rebuffer.out
    sudo kill -64 $(ps ax | grep "[p]ython3 _daq_core/delay_sync.py" | awk '{print $1}') 2> /dev/null
    sudo kill -64 $(ps ax | grep "[p]ython3 _daq_core/hw_controller.py" | awk '{print $1}') 2> /dev/null
    sudo kill -64 $(ps ax | grep "[p]ython3 _daq_core/iq_eth_sink.py" | awk '{print $1}') 2> /dev/null
    sudo pkill -64 iq_server.out
    # Also clean up any PID directories
    find _logs/inst*/pids -name "*.pid" -exec rm -f {} \; 2>/dev/null
fi
