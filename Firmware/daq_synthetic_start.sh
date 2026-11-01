#!/bin/bash
#
#   DAQ chain start srcipt
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#   Authors: Tamas Peto, Carl Laufer
#
#   Usage: ./daq_synthetic_start.sh [--rt] [--pace] [--blocks N] [--seed S]
#                                   [--takeover] [--no-host-tuning]
#     --rt              launch with the hardware tiers of daq_start_sm.sh
#                       (taskset + SCHED_FIFO, [host] cpu_cores/rt_priority_offset,
#                       core guard and preflight); implies --pace
#     --pace            synthesizer emits blocks at the real sample rate
#     --blocks N        synthesizer block count (0 = unbounded; default 10000)
#     --seed S          seed the synthesizer's random generator
#     --takeover        with [host] instance_guard = refuse: stop this
#                       instance when it runs from another copy, then start
#     --no-host-tuning  accepted for symmetry (this script tunes nothing)
#   The synthesizer flags are passed on only when given here. Unknown
#   arguments are ignored.
#
#   Exit codes: 0 started; 1 config/FIR-designer failure or ports still busy
#   (refuse guard); 3 guard refusal; 4 preflight failure (--rt, strict).

HD_TAKEOVER=0
HD_NO_HOST_TUNING=0
RT_MODE=0
synth_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --takeover) HD_TAKEOVER=1 ;;
        --no-host-tuning) HD_NO_HOST_TUNING=1 ;;
        --rt) RT_MODE=1 ;;
        --pace) synth_args+=(--pace) ;;
        --blocks|--seed)
            if [ $# -lt 2 ]; then
                echo -e "\e[91m$1 needs a value - DAQ chain not started!\e[39m"
                exit 1
            fi
            synth_args+=("$1" "$2")
            shift
            ;;
    esac
    shift
done
if [ "$RT_MODE" = 1 ]; then
    case " ${synth_args[*]} " in
        *" --pace "*) ;;
        *) synth_args+=(--pace) ;;
    esac
fi

if [ ! -f ./daq_common.sh ]; then
    echo -e "\e[91mdaq_common.sh not found next to $0 - DAQ chain not started!\e[39m"
    exit 1
fi
# shellcheck source=daq_common.sh
. ./daq_common.sh

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
# only; the default opens no fd, exports nothing and writes no file)
if [ "$HD_INSTANCE_REGISTRY" = 1 ]; then
    hd_registry_begin "$instance_id" synthetic
fi

# (re) create control FIFOs
rm _data_control/${FIFO_PREFIX}fw_decimator_in 2> /dev/null
rm _data_control/${FIFO_PREFIX}bw_decimator_in 2> /dev/null

rm _data_control/${FIFO_PREFIX}fw_decimator_out 2> /dev/null
rm _data_control/${FIFO_PREFIX}bw_decimator_out 2> /dev/null

rm _data_control/${FIFO_PREFIX}fw_delay_sync_iq 2> /dev/null
rm _data_control/${FIFO_PREFIX}bw_delay_sync_iq 2> /dev/null

rm _data_control/${FIFO_PREFIX}fw_delay_sync_hwc 2> /dev/null
rm _data_control/${FIFO_PREFIX}bw_delay_sync_hwc 2> /dev/null

mkfifo _data_control/${FIFO_PREFIX}fw_decimator_in
mkfifo _data_control/${FIFO_PREFIX}bw_decimator_in

mkfifo _data_control/${FIFO_PREFIX}fw_decimator_out
mkfifo _data_control/${FIFO_PREFIX}bw_decimator_out

mkfifo _data_control/${FIFO_PREFIX}fw_delay_sync_iq
mkfifo _data_control/${FIFO_PREFIX}bw_delay_sync_iq

mkfifo _data_control/${FIFO_PREFIX}fw_delay_sync_hwc
mkfifo _data_control/${FIFO_PREFIX}bw_delay_sync_hwc

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

# Archive old log files instead of deleting - scoped to THIS instance's
# directory so starting instance N never touches another instance's live logs.
if ls "$LOG_DIR"/*.log 1>/dev/null 2>&1; then
    archive_dir="$LOG_DIR/archive/$(date -Iseconds)"
    mkdir -p "$archive_dir"
    mv "$LOG_DIR"/*.log "$archive_dir/" 2>/dev/null
    # Keep only the last 10 archives of this instance
    ls -1dt "$LOG_DIR"/archive/*/ 2>/dev/null | tail -n +11 | xargs rm -rf 2>/dev/null
fi

# Useful to set this on low power ARM devices
#sudo cpufreq-set -g performance

# Set for Tinkerboard with heatsink/fan
#sudo cpufreq-set -d 1.8GHz

# Port probe: advisory only (prints, never stops anything) unless
# [host] instance_guard = refuse, which applies the hardware rules
hd_port_check synthetic
hd_lock_deferred

# Generating FIR filter coefficients
python3 fir_filter_designer.py
out=$?
if test $out -ne 0
    then
        echo -e "\e[91mDAQ chain not started!\e[39m"
        exit 1
fi

# --- Hardware Discovery & Initialization ---
echo "Discovering available hardware..."
python3 _daq_core/hw_discover.py > "_data_control/${FIFO_PREFIX}hw_caps.json" 2>/dev/null || true
python3 _daq_core/auto_config.py "_data_control/${FIFO_PREFIX}hw_caps.json" daq_chain_config.ini 2>/dev/null || true

# FPGA bitstream loading (if enabled)
if [ "$HD_FPGA_ENABLE" = "1" ]; then
    FPGA_BITSTREAM="$HD_FPGA_BITSTREAM"
    if [ -n "$FPGA_BITSTREAM" ] && [ -f "$FPGA_BITSTREAM" ]; then
        echo "Loading FPGA bitstream: $FPGA_BITSTREAM"
        python3 _daq_core/fpga_loader.py "$FPGA_BITSTREAM" || echo "FPGA load failed, continuing with CPU-only mode"
    fi
fi

# GPU initialization (if enabled)
if [ "$HD_GPU_ENABLE" = "1" ]; then
    echo "Initializing GPU offload..."
    python3 _daq_core/gpu_init.py || echo "GPU init failed, continuing with CPU-only mode"
fi

# Create PID directory for this instance
PID_DIR="_logs/inst${instance_id}/pids"
mkdir -p "$PID_DIR"
# Purge stale PID files from previous runs (a crash/reboot leaves them behind
# with PIDs the kernel may have recycled). Scoped to THIS instance's directory
# so starting instance N never touches another instance's PID files.
rm -f "$PID_DIR"/*.pid

CORES_USED=""
if [ "$RT_MODE" = 1 ]; then
    # --rt: the hardware tiers of daq_start_sm.sh, synthesizer in rtl_daq's slot
    NUM_CORES=$(nproc 2>/dev/null || echo 4)
    case "$NUM_CORES" in
        ''|*[!0-9]*|0) NUM_CORES=4 ;;
    esac
    if [ -n "$HD_CPU_CORES" ]; then
        IFS=, read -r CORE0 CORE1 CORE2 CORE3 <<EOF
$HD_CPU_CORES
EOF
    else
        CORE0=$(( (instance_id * 4 + 0) % NUM_CORES ))
        CORE1=$(( (instance_id * 4 + 1) % NUM_CORES ))
        CORE2=$(( (instance_id * 4 + 2) % NUM_CORES ))
        CORE3=$(( (instance_id * 4 + 3) % NUM_CORES ))
    fi
    hd_core_guard "$CORE0" "$CORE1" "$CORE2" "$CORE3"
    CORES_USED="$CORE0,$CORE1,$CORE2,$CORE3"
    RT_OFF="$HD_RT_PRIORITY_OFFSET"
    hd_preflight
fi

if [ "$RT_MODE" = 1 ]; then
    echo "Starting DAQ Subsystem with synthetic data source (instance ${instance_id}) with RT optimization"
    echo "Cores: $NUM_CORES (using ${CORE0},${CORE1},${CORE2},${CORE3})"
    taskset -c "$CORE0" chrt -f $((99 - RT_OFF)) python3 _testing/test_data_synthesizer.py "${synth_args[@]}" 2>"$LOG_DIR/synthetic.log" 8>&- | \
    taskset -c "$CORE1" chrt -f $((94 - RT_OFF)) _daq_core/rebuffer.out 0 2> "$LOG_DIR/rebuffer.log" 8>&- &
    echo $! > "$PID_DIR/rebuffer.pid"
    jobs -p %+ > "$PID_DIR/synthesizer.pid" 2>/dev/null
    hd_arm_abort "$instance_id"

    hd_launch taskset -c "$CORE1" chrt -f $((92 - RT_OFF)) _daq_core/decimate.out 2> "$LOG_DIR/decimator.log"
    echo $! > "$PID_DIR/decimate.pid"

    hd_launch taskset -c "${HD_DELAY_SYNC_CORES:-$CORE2}" chrt -f $((90 - RT_OFF)) python3 _daq_core/delay_sync.py 2> "$LOG_DIR/delay_sync.log"
    echo $! > "$PID_DIR/delay_sync.pid"

    hd_launch taskset -c "$CORE3" chrt -f $((82 - RT_OFF)) sudo env "PATH=$PATH" python3 _daq_core/hw_controller.py 2> "$LOG_DIR/hwc.log"
    echo $! > "$PID_DIR/hw_controller.pid"

    if [ "$out_data_iface_type" = eth ]; then
        echo "Output data interface: IQ ethernet server"
        hd_launch taskset -c "$CORE2" chrt -f $((88 - RT_OFF)) _daq_core/iq_server.out 2>"$LOG_DIR/iq_server.log"
        echo $! > "$PID_DIR/iq_server.pid"
    elif [ "$out_data_iface_type" = shmem ]; then
        echo "Output data interface: Shared memory"
    fi
else
    # Start main program chain -Thread 0 Normal (non squelch mode)
    echo "Starting DAQ Subsystem with synthetic data source (instance ${instance_id})"
    python3 _testing/test_data_synthesizer.py "${synth_args[@]}" 2>"$LOG_DIR/synthetic.log" 8>&- | \
    _daq_core/rebuffer.out 0 2> "$LOG_DIR/rebuffer.log" 8>&- &
    echo $! > "$PID_DIR/rebuffer.pid"
    # $! is the LAST process of the pipeline (rebuffer); the pipeline's process
    # group leader is its FIRST process (the synthesizer) - record it too so
    # per-instance daq_stop.sh can signal it.
    jobs -p %+ > "$PID_DIR/synthesizer.pid" 2>/dev/null
    # From here on a failing start stops the stages it launched
    hd_arm_abort "$instance_id"

    # Decimator - Thread 1
    hd_launch chrt -f 99 _daq_core/decimate.out 2> "$LOG_DIR/decimator.log"
    echo $! > "$PID_DIR/decimate.pid"

    # Delay synchronizer - Thread 2
    hd_launch python3 _daq_core/delay_sync.py 2> "$LOG_DIR/delay_sync.log"
    echo $! > "$PID_DIR/delay_sync.pid"

    # Hardware Controller data path - Thread 3
    hd_launch sudo env "PATH=$PATH" python3 _daq_core/hw_controller.py 2> "$LOG_DIR/hwc.log"
    echo $! > "$PID_DIR/hw_controller.pid"
    # root priviliges are needed to drive the i2c master

    if [ "$out_data_iface_type" = eth ]; then
        echo "Output data interface: IQ ethernet server"
        hd_launch _daq_core/iq_server.out 2>"$LOG_DIR/iq_server.log"
        echo $! > "$PID_DIR/iq_server.pid"
    elif [ "$out_data_iface_type" = shmem ]; then
        echo "Output data interface: Shared memory"
    fi
fi

# IQ Eth sink used for testing
#sleep 3
#python3 _daq_core/iq_eth_sink.py 2>"$LOG_DIR/iq_eth_sink.log" &

if [ "$HD_INSTANCE_REGISTRY" = 1 ]; then
    HD_SERIALS=""
    hd_registry_write synthetic "$PID_DIR" "$CORES_USED"
fi
hd_disarm_abort

echo -e "      )  (     "
echo -e "      (   ) )  "
echo -e "       ) ( (   "
echo -e "     _______)_ "
echo -e "  .-'---------|"
echo -e " (  |/\/\/\/\/|"
echo -e "  '-./\/\/\/\/|"
echo -e "    '_________'"
echo -e "     '-------' "
echo -e "               "
echo -e "Have a coffee watch radar"
