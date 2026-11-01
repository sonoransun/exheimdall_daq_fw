#!/bin/bash
#
#   DAQ chain start srcipt
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#   Authors: Tamas Peto, Carl Laufer
#
#   Usage: ./daq_start_sm.sh [--takeover] [--no-host-tuning]
#     --takeover        with [host] instance_guard = refuse: stop this
#                       instance when it runs from another copy, then start
#     --no-host-tuning  same as [host] host_tuning = 0
#   Unknown arguments are ignored.
#
#   Exit codes: 0 started; 1 config/FIR-designer failure or ports still busy;
#   3 guard refusal (instance_guard = refuse, allow_core_sharing = refuse);
#   4 preflight failure with [host] preflight = strict.

HD_TAKEOVER=0
HD_NO_HOST_TUNING=0
for arg in "$@"; do
    case "$arg" in
        --takeover) HD_TAKEOVER=1 ;;
        --no-host-tuning) HD_NO_HOST_TUNING=1 ;;
    esac
done

if [ ! -f ./daq_common.sh ]; then
    echo -e "\e[91mdaq_common.sh not found next to $0 - DAQ chain not started!\e[39m"
    exit 1
fi
# shellcheck source-path=SCRIPTDIR
# shellcheck source=daq_common.sh
. ./daq_common.sh

# Check config file (fast path: no hardware probing; exit code 0 = valid).
# 'attached' adds the sysfs serial and host-registry checks.
if python3 ini_checker.py no_hw attached; then
    echo -e "\e[92mConfig file check [ OK ]\e[39m"
else
    echo -e "\e[91mConfig file check [ FAIL ]\e[39m"
    exit 1
fi

# Read config ini file (ConfigParser: immune to comments/substring matches)
hd_read_config daq_chain_config.ini
out_data_iface_type="$HD_OUT_IFACE"
instance_id="$HD_INSTANCE_ID"

# sysctl kernel.sched_rt_runtime_us + IRQ tuning ([host] host_tuning)
hd_host_tuning_early

# Compute FIFO name prefix based on instance_id
if [ "$instance_id" -eq 0 ]; then
    FIFO_PREFIX=""
else
    FIFO_PREFIX="inst${instance_id}_"
fi

# Host registry, startup mutex and instance lock ([host] instance_registry = 1
# only; the default opens no fd, exports nothing and writes no file)
if [ "$HD_INSTANCE_REGISTRY" = 1 ]; then
    hd_registry_begin "$instance_id"
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
# paths (external tooling reads them); instance N logs to _logs/instN/.
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

# usbfs_memory_mb + drop_caches ([host] host_tuning)
hd_host_tuning_late

# Check ports (IQ server, Hardware controller, Status server): listener-only;
# busy ports are freed with ./daq_stop.sh, 10 attempts, then exit 1
hd_port_check hw
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

# Detect number of CPU cores for affinity assignment
NUM_CORES=$(nproc 2>/dev/null || echo 4)
case "$NUM_CORES" in
    ''|*[!0-9]*|0) NUM_CORES=4 ;;
esac
HOST_ARCH=$(uname -m)

if [ -n "$HD_CPU_CORES" ]; then
    # [host] cpu_cores: tiers T0 rtl_daq, T1 rebuffer+decimate,
    # T2 delay_sync+iq_server, T3 hw_controller
    IFS=, read -r CORE0 CORE1 CORE2 CORE3 <<EOF
$HD_CPU_CORES
EOF
else
    # Per-instance CPU affinity: instance N is offset by N*4 cores (mod NUM_CORES)
    # so two federation instances on an 8-core host do not stack their SCHED_FIFO
    # 99/94/92 processes on the same cores. Instance 0 resolves to cores 0/1/2/3 -
    # identical to the historical fixed assignment.
    CORE0=$(( (instance_id * 4 + 0) % NUM_CORES ))
    CORE1=$(( (instance_id * 4 + 1) % NUM_CORES ))
    CORE2=$(( (instance_id * 4 + 2) % NUM_CORES ))
    CORE3=$(( (instance_id * 4 + 3) % NUM_CORES ))
fi
hd_core_guard "$CORE0" "$CORE1" "$CORE2" "$CORE3"

# [host] rt_priority_offset is subtracted from every tier (default 0)
RT_OFF="$HD_RT_PRIORITY_OFFSET"
PRIO_RTL=$((99 - RT_OFF))
PRIO_REB=$((94 - RT_OFF))
PRIO_DEC=$((92 - RT_OFF))
PRIO_DS=$((90 - RT_OFF))
PRIO_IQS=$((88 - RT_OFF))
PRIO_HWC=$((82 - RT_OFF))

# [host] preflight = warn|strict (default off)
hd_preflight

# Differentiated Real-Time Priority & CPU Affinity Assignment
echo "Starting DAQ Subsystem (instance ${instance_id}) with RT optimization"
echo "Platform: $HOST_ARCH, Cores: $NUM_CORES (using ${CORE0},${CORE1},${CORE2},${CORE3})"

# Tier 1: Hardware I/O Critical (95-99) - Core 0 & 1
# RTL-SDR USB acquisition (highest priority) + Rebuffer pipeline
taskset -c "$CORE0" chrt -f "$PRIO_RTL" _daq_core/rtl_daq.out 2> "$LOG_DIR/rtl_daq.log" 8>&- | \
taskset -c "$CORE1" chrt -f "$PRIO_REB" _daq_core/rebuffer.out 0 2> "$LOG_DIR/rebuffer.log" 8>&- &
echo $! > "$PID_DIR/rebuffer.pid"
# $! is the LAST process of the pipeline (rebuffer); the pipeline's process
# group leader is its FIRST process (rtl_daq) - record it too so per-instance
# daq_stop.sh can signal the USB acquisition process.
jobs -p %+ > "$PID_DIR/rtl_daq.pid" 2>/dev/null
# From here on a failing start stops the stages it launched
hd_arm_abort "$instance_id"

# FIR Decimation (high priority) - Core 1 (shared with rebuffer for cache locality)
hd_launch taskset -c "$CORE1" chrt -f "$PRIO_DEC" _daq_core/decimate.out 2> "$LOG_DIR/decimator.log"
echo $! > "$PID_DIR/decimate.pid"

# FFT Cross-correlation (high priority) - Core 2 ([host] delay_sync_cores
# widens the list for delay_sync only)
if [ -n "$HD_CPU_CORES" ] || [ "$NUM_CORES" -ge 3 ]; then
    hd_launch taskset -c "${HD_DELAY_SYNC_CORES:-$CORE2}" chrt -f "$PRIO_DS" python3 _daq_core/delay_sync.py 2> "$LOG_DIR/delay_sync.log"
else
    # Fallback for dual-core systems
    hd_launch taskset -c "${HD_DELAY_SYNC_CORES:-$CORE1}" chrt -f "$PRIO_DS" python3 _daq_core/delay_sync.py 2> "$LOG_DIR/delay_sync.log"
fi
echo $! > "$PID_DIR/delay_sync.pid"

# Tier 2: Control Plane (75-84) - Core 3 (or available)
if [ -n "$HD_CPU_CORES" ] || [ "$NUM_CORES" -ge 4 ]; then
    hd_launch taskset -c "$CORE3" chrt -f "$PRIO_HWC" sudo env "PATH=$PATH" python3 _daq_core/hw_controller.py 2> "$LOG_DIR/hwc.log"
else
    # Use any available core for systems with fewer cores
    hd_launch chrt -f "$PRIO_HWC" sudo env "PATH=$PATH" python3 _daq_core/hw_controller.py 2> "$LOG_DIR/hwc.log"
fi
echo $! > "$PID_DIR/hw_controller.pid"
# root priviliges are needed to drive the i2c master

if [ "$out_data_iface_type" = eth ]; then
    echo "Output data interface: IQ ethernet server"
    # IQ server (medium-high priority) - Core 2 or 3 depending on system
    if [ -n "$HD_CPU_CORES" ] || [ "$NUM_CORES" -ge 4 ]; then
        hd_launch taskset -c "$CORE2" chrt -f "$PRIO_IQS" _daq_core/iq_server.out 2>"$LOG_DIR/iq_server.log"
    else
        hd_launch chrt -f "$PRIO_IQS" _daq_core/iq_server.out 2>"$LOG_DIR/iq_server.log"
    fi
    echo $! > "$PID_DIR/iq_server.pid"
elif [ "$out_data_iface_type" = shmem ]; then
    echo "Output data interface: Shared memory"
fi

# IQ Eth sink used for testing
#sleep 3
#python3 _daq_core/iq_eth_sink.py 2>"$LOG_DIR/iq_eth_sink.log" &

if [ "$HD_INSTANCE_REGISTRY" = 1 ]; then
    hd_registry_write hw "$PID_DIR" "$CORE0,$CORE1,$CORE2,$CORE3"
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
