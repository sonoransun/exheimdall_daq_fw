#!/bin/bash
#
#   DAQ chain start script - replay of a recorded raw_u8 capture
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#
#   Usage: ./daq_replay_start.sh [--realtime] [--loop] [--restamp]
#                                [--takeover] [--no-host-tuning] CAPTURE.hcap
#     CAPTURE.hcap      a raw_u8 .hcap capture (rtl_daq.out's output format,
#                       e.g. recorded with `iq_capture_format.py wrap --kind
#                       raw_u8`); its frames must match this instance's
#                       [hw] num_ch and [daq] daq_buffer_size
#     --realtime        keep the recorded frame timing (default: as fast as
#                       the chain consumes, like the synthesizer)
#     --loop            start over at the end of the capture
#     --restamp         rewrite the header time_stamp to the replay time
#     --takeover        with [host] instance_guard = refuse: stop this
#                       instance when it runs from another copy, then start
#     --no-host-tuning  accepted for symmetry (this script tunes nothing)
#
#   Runs the chain of daq_synthetic_start.sh with
#       python3 _testing/iq_replay.py --raw-to-stdout CAPTURE | rebuffer.out 0
#   in place of the synthesizer. iq_replay answers the hw_controller /
#   delay_sync control messages on port 1130 + instance_id * port_stride with
#   "ok" without applying them (recorded samples cannot be retuned); like
#   rtl_daq, each acknowledged command turns the next 5 frames into
#   header-only DUMMY frames, which delay_sync waits for after a retune.
#   The noise source cannot be switched on and recorded samples cannot be
#   re-delayed, so the replayed chain calibrates only from CAL frames that
#   are in the capture and reaches sync only if the capture is coherent. A
#   capture of a free-running synthesizer (no CAL frames) stays at
#   sync_state 1. Stop it with
#   ./daq_stop.sh (or ./daq_stop.sh N); PID files are rebuffer.pid and
#   iq_replay.pid in _logs/inst<N>/pids/. With [host] instance_registry = 1
#   the registry entry has mode "synthetic".
#
#   Exit codes: 0 started; 1 config/FIR-designer failure or ports still busy
#   (refuse guard); 2 usage error or capture refused (not raw_u8, no
#   sidecar, corrupt, or frames that do not match the config); 3 guard
#   refusal.

HD_TAKEOVER=0
HD_NO_HOST_TUNING=0
replay_args=()
CAPTURE=""
# HD_TAKEOVER / HD_NO_HOST_TUNING / HD_SERIALS are read by daq_common.sh
# shellcheck disable=SC2034
while [ $# -gt 0 ]; do
    case "$1" in
        --takeover) HD_TAKEOVER=1 ;;
        --no-host-tuning) HD_NO_HOST_TUNING=1 ;;
        --realtime|--loop|--restamp) replay_args+=("$1") ;;
        -h|--help)
            sed -n '8,21p' "$0" | sed 's/^#//'
            exit 0
            ;;
        -*)
            echo -e "\e[91mUnknown option $1 - DAQ chain not started!\e[39m"
            exit 2
            ;;
        *)
            if [ -n "$CAPTURE" ]; then
                echo -e "\e[91mOnly one capture can be replayed - DAQ chain not started!\e[39m"
                exit 2
            fi
            CAPTURE="$1"
            ;;
    esac
    shift
done
if [ -z "$CAPTURE" ]; then
    echo "Usage: $0 [--realtime] [--loop] [--restamp] [--takeover] [--no-host-tuning] CAPTURE.hcap"
    exit 2
fi

if [ ! -f ./daq_common.sh ]; then
    echo -e "\e[91mdaq_common.sh not found next to $0 - DAQ chain not started!\e[39m"
    exit 1
fi
# shellcheck source=daq_common.sh disable=SC1091
. ./daq_common.sh

# The capture must be raw_u8 and match this instance's channel count and
# block size: rebuffer.out reads [daq] daq_buffer_size samples per channel
# per frame whatever the header says
if ! python3 _testing/iq_capture_format.py check --kind raw_u8 --config daq_chain_config.ini "$CAPTURE"; then
    echo -e "\e[91mCapture $CAPTURE refused - DAQ chain not started!\e[39m"
    exit 2
fi

# Check config file (exit code 0 = valid; errors are printed on stderr)
if python3 ini_checker.py no_hw; then
      echo -e "\e[92mConfig file check [OK]\e[39m"
else
      echo -e "\e[91mConfig file check [FAIL]\e[39m"
      exit 1
fi

# Read config ini file (ConfigParser: immune to comments/substring matches)
hd_read_config daq_chain_config.ini
out_data_iface_type="$HD_OUT_IFACE"
instance_id="$HD_INSTANCE_ID"

# Compute FIFO name prefix based on instance_id
if [ "$instance_id" -eq 0 ]; then
    FIFO_PREFIX=""
else
    FIFO_PREFIX="inst${instance_id}_"
fi

# Host registry, startup mutex and instance lock ([host] instance_registry = 1
# only). A replay start follows the synthetic rules: it never stops a
# running chain.
if [ "$HD_INSTANCE_REGISTRY" = 1 ]; then
    hd_registry_begin "$instance_id" synthetic
fi

# (re) create control FIFOs
for fifo in fw_decimator_in bw_decimator_in fw_decimator_out bw_decimator_out \
            fw_delay_sync_iq bw_delay_sync_iq fw_delay_sync_hwc bw_delay_sync_hwc; do
    rm "_data_control/${FIFO_PREFIX}${fifo}" 2> /dev/null
    mkfifo "_data_control/${FIFO_PREFIX}${fifo}"
done

# Relay files of the previous run (hw_controller state / recalibration request)
rm -f "_data_control/${FIFO_PREFIX}recal_request" "_data_control/${FIFO_PREFIX}hwc_state"

# hw_controller's track-lock approval file ('0' = not approved)
[ -e "_data_control/${FIFO_PREFIX}iq_track_lock" ] || printf '0' > "_data_control/${FIFO_PREFIX}iq_track_lock"

# Create database directory
mkdir -p _db

# Per-instance log directory. Instance 0 keeps the historical flat _logs/
# paths; instance N logs to _logs/instN/.
if [ "$instance_id" -eq 0 ]; then
    LOG_DIR="_logs"
else
    LOG_DIR="_logs/inst${instance_id}"
fi
mkdir -p "$LOG_DIR"

# Archive old log files instead of deleting - scoped to THIS instance's directory
if ls "$LOG_DIR"/*.log 1>/dev/null 2>&1; then
    archive_dir="$LOG_DIR/archive/$(date -Iseconds)"
    mkdir -p "$archive_dir"
    mv "$LOG_DIR"/*.log "$archive_dir/" 2>/dev/null
    # Keep only the last 10 archives of this instance
    # shellcheck disable=SC2012
    ls -1dt "$LOG_DIR"/archive/*/ 2>/dev/null | tail -n +11 | xargs rm -rf 2>/dev/null
fi

# Port probe: advisory only (prints, never stops anything) unless
# [host] instance_guard = refuse, which applies the hardware rules
hd_port_check synthetic
hd_lock_deferred

# Generating FIR filter coefficients
if ! python3 fir_filter_designer.py; then
    echo -e "\e[91mDAQ chain not started!\e[39m"
    exit 1
fi

# Hardware discovery and `= auto` INI values, as daq_synthetic_start.sh
python3 _daq_core/hw_discover.py > "_data_control/${FIFO_PREFIX}hw_caps.json" 2>/dev/null || true
python3 _daq_core/auto_config.py "_data_control/${FIFO_PREFIX}hw_caps.json" daq_chain_config.ini 2>/dev/null || true

# Create PID directory for this instance; purge stale PID files of THIS
# instance (a crash/reboot leaves them behind with recyclable PIDs)
PID_DIR="_logs/inst${instance_id}/pids"
mkdir -p "$PID_DIR"
rm -f "$PID_DIR"/*.pid

CTRL_PORT=$((1130 + instance_id * HD_PORT_STRIDE))

echo "Starting DAQ Subsystem replaying $CAPTURE (instance ${instance_id})"
python3 _testing/iq_replay.py --raw-to-stdout --control-port "$CTRL_PORT" "${replay_args[@]}" "$CAPTURE" \
    2>"$LOG_DIR/replay.log" 8>&- | \
_daq_core/rebuffer.out 0 2> "$LOG_DIR/rebuffer.log" 8>&- &
echo $! > "$PID_DIR/rebuffer.pid"
# $! is the LAST process of the pipeline (rebuffer); its FIRST process is
# the replay - record it too so per-instance daq_stop.sh can signal it
jobs -p %+ > "$PID_DIR/iq_replay.pid" 2>/dev/null
# From here on a failing start stops the stages it launched
hd_arm_abort "$instance_id"

# Decimator
hd_launch chrt -f 99 _daq_core/decimate.out 2> "$LOG_DIR/decimator.log"
echo $! > "$PID_DIR/decimate.pid"

# Delay synchronizer
hd_launch python3 _daq_core/delay_sync.py 2> "$LOG_DIR/delay_sync.log"
echo $! > "$PID_DIR/delay_sync.pid"

# Hardware Controller data path (root privileges drive the i2c master)
hd_launch sudo env "PATH=$PATH" python3 _daq_core/hw_controller.py 2> "$LOG_DIR/hwc.log"
echo $! > "$PID_DIR/hw_controller.pid"

if [ "$out_data_iface_type" = eth ]; then
    echo "Output data interface: IQ ethernet server"
    hd_launch _daq_core/iq_server.out 2>"$LOG_DIR/iq_server.log"
    echo $! > "$PID_DIR/iq_server.pid"
elif [ "$out_data_iface_type" = shmem ]; then
    echo "Output data interface: Shared memory"
fi

if [ "$HD_INSTANCE_REGISTRY" = 1 ]; then
    # shellcheck disable=SC2034
    HD_SERIALS=""
    # Registry mode "synthetic" (spec B.3.1: hw | synthetic), as
    # hd_registry_begin above: no USB hardware is driven, and the pids map
    # names iq_replay in place of synthesizer
    hd_registry_write synthetic "$PID_DIR" ""
fi
hd_disarm_abort

echo "DAQ chain started in replay mode (log: $LOG_DIR/replay.log)"
