#!/bin/bash
#
#   DAQ chain start srcipt
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#   Authors: Tamas Peto, Carl Laufer

# Check config file (fast path: no hardware probing; exit code 0 = valid)
if python3 ini_checker.py no_hw; then
    echo -e "\e[92mConfig file check [ OK ]\e[39m"
else
    echo -e "\e[91mConfig file check [ FAIL ]\e[39m"
    exit 1
fi

sudo sysctl -w kernel.sched_rt_runtime_us=-1

# Apply IRQ tuning for optimal performance
if [ -x "../util/irq_tuning.sh" ]; then
    sudo ../util/irq_tuning.sh
fi

# Read config ini file (ConfigParser: immune to comments/substring matches)
out_data_iface_type=$(python3 -c "
from configparser import ConfigParser
c = ConfigParser()
c.read('daq_chain_config.ini')
print(c.get('data_interface', 'out_data_iface_type', fallback='eth'))
" 2>/dev/null || echo "eth")

# Read federation instance_id
instance_id=$(python3 -c "
from configparser import ConfigParser
c = ConfigParser()
c.read('daq_chain_config.ini')
print(c.get('federation', 'instance_id', fallback='0'))
" 2>/dev/null || echo "0")
case "$instance_id" in
    ''|*[!0-9]*) instance_id=0 ;;
esac

# Compute FIFO name prefix based on instance_id
if [ "$instance_id" -eq 0 ]; then
    FIFO_PREFIX=""
else
    FIFO_PREFIX="inst${instance_id}_"
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

# The Kernel limits the maximum size of all buffers that libusb can allocate to 16MB by default.
# In order to disable the limit, you have to run the following command as root:
if [ -e /sys/module/usbcore/parameters/usbfs_memory_mb ]; then
    sudo sh -c "echo 0 > /sys/module/usbcore/parameters/usbfs_memory_mb"
else
    echo "WARN: /sys/module/usbcore/parameters/usbfs_memory_mb not present, skipping"
fi

# This command clear the caches
echo '3' | sudo tee /proc/sys/vm/drop_caches > /dev/null

# Compute ports based on instance_id (port_stride=100)
port_stride=$(python3 -c "
from configparser import ConfigParser
c = ConfigParser()
c.read('daq_chain_config.ini')
print(c.get('federation', 'port_stride', fallback='100'))
" 2>/dev/null || echo "100")
case "$port_stride" in
    ''|*[!0-9]*) port_stride=100 ;;
esac
port_offset=$((instance_id * port_stride))
iq_port=$((5000 + port_offset))
hwc_port=$((5001 + port_offset))
status_port=$((5002 + port_offset))

# Port-in-use test: lsof when available, ss as fallback. Returns 0 when the
# port has a listener. If neither tool exists the check is skipped entirely
# (previously a missing lsof made this loop spin forever).
port_in_use() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -i:"$1" >/dev/null 2>&1
    elif command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
    else
        return 1
    fi
}

# Check ports (IQ server, Hardware controller, Status server)
if command -v lsof >/dev/null 2>&1 || command -v ss >/dev/null 2>&1; then
    port_attempts=0
    while true; do
        port_ready=1
        for p in "$iq_port" "$hwc_port" "$status_port"; do
            if port_in_use "$p"; then
                port_ready=0
            fi
        done
        if test $port_ready -eq 1; then
            break
        fi
        port_attempts=$((port_attempts + 1))
        if [ "$port_attempts" -ge 10 ]; then
            echo -e "\e[91mERROR: Ports (${iq_port}, ${hwc_port} & ${status_port}) still busy after ${port_attempts} attempts - DAQ chain not started!\e[39m"
            exit 1
        fi
        echo "WARN:Ports used by the DAQ chain instance ${instance_id} are not free! (${iq_port}, ${hwc_port} & ${status_port})"
        ./daq_stop.sh
        sleep 1
    done
else
    echo "WARN: Neither lsof nor ss available - skipping port availability check"
fi

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
python3 _daq_core/hw_discover.py > _data_control/hw_caps.json 2>/dev/null || true
python3 _daq_core/auto_config.py _data_control/hw_caps.json daq_chain_config.ini 2>/dev/null || true

# FPGA bitstream loading (if enabled)
FPGA_ENABLE=$(python3 -c "
from configparser import ConfigParser
c = ConfigParser()
c.read('daq_chain_config.ini')
print(c.get('fpga', 'enable', fallback='0'))
" 2>/dev/null || echo "0")
if [ "$FPGA_ENABLE" = "1" ]; then
    FPGA_BITSTREAM=$(python3 -c "
from configparser import ConfigParser
c = ConfigParser()
c.read('daq_chain_config.ini')
print(c.get('fpga', 'bitstream', fallback=''))
" 2>/dev/null)
    if [ -n "$FPGA_BITSTREAM" ] && [ -f "$FPGA_BITSTREAM" ]; then
        echo "Loading FPGA bitstream: $FPGA_BITSTREAM"
        python3 _daq_core/fpga_loader.py "$FPGA_BITSTREAM" || echo "FPGA load failed, continuing with CPU-only mode"
    fi
fi

# GPU initialization (if enabled)
GPU_ENABLE=$(python3 -c "
from configparser import ConfigParser
c = ConfigParser()
c.read('daq_chain_config.ini')
print(c.get('gpu', 'enable', fallback='0'))
" 2>/dev/null || echo "0")
if [ "$GPU_ENABLE" = "1" ]; then
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

# Per-instance CPU affinity: instance N is offset by N*4 cores (mod NUM_CORES)
# so two federation instances on an 8-core host do not stack their SCHED_FIFO
# 99/94/92 processes on the same cores. Instance 0 resolves to cores 0/1/2/3 -
# identical to the historical fixed assignment.
CORE0=$(( (instance_id * 4 + 0) % NUM_CORES ))
CORE1=$(( (instance_id * 4 + 1) % NUM_CORES ))
CORE2=$(( (instance_id * 4 + 2) % NUM_CORES ))
CORE3=$(( (instance_id * 4 + 3) % NUM_CORES ))
if [ "$instance_id" -gt 0 ] && [ "$NUM_CORES" -lt $(( (instance_id + 1) * 4 )) ]; then
    echo "WARN: ${NUM_CORES} cores for $((instance_id + 1)) instances - RT processes of multiple instances will share cores (${CORE0},${CORE1},${CORE2},${CORE3})"
fi

# Differentiated Real-Time Priority & CPU Affinity Assignment
echo "Starting DAQ Subsystem (instance ${instance_id}) with RT optimization"
echo "Platform: $HOST_ARCH, Cores: $NUM_CORES (using ${CORE0},${CORE1},${CORE2},${CORE3})"

# Tier 1: Hardware I/O Critical (95-99) - Core 0 & 1
# RTL-SDR USB acquisition (highest priority) + Rebuffer pipeline
taskset -c $CORE0 chrt -f 99 _daq_core/rtl_daq.out 2> "$LOG_DIR/rtl_daq.log" | \
taskset -c $CORE1 chrt -f 94 _daq_core/rebuffer.out 0 2> "$LOG_DIR/rebuffer.log" &
echo $! > "$PID_DIR/rebuffer.pid"
# $! is the LAST process of the pipeline (rebuffer); the pipeline's process
# group leader is its FIRST process (rtl_daq) - record it too so per-instance
# daq_stop.sh can signal the USB acquisition process.
jobs -p %+ > "$PID_DIR/rtl_daq.pid" 2>/dev/null

# FIR Decimation (high priority) - Core 1 (shared with rebuffer for cache locality)
taskset -c $CORE1 chrt -f 92 _daq_core/decimate.out 2> "$LOG_DIR/decimator.log" &
echo $! > "$PID_DIR/decimate.pid"

# FFT Cross-correlation (high priority) - Core 2
if [ $NUM_CORES -ge 4 ]; then
    taskset -c $CORE2 chrt -f 90 python3 _daq_core/delay_sync.py 2> "$LOG_DIR/delay_sync.log" &
elif [ $NUM_CORES -ge 3 ]; then
    taskset -c $CORE2 chrt -f 90 python3 _daq_core/delay_sync.py 2> "$LOG_DIR/delay_sync.log" &
else
    # Fallback for dual-core systems
    taskset -c $CORE1 chrt -f 90 python3 _daq_core/delay_sync.py 2> "$LOG_DIR/delay_sync.log" &
fi
echo $! > "$PID_DIR/delay_sync.pid"

# Tier 2: Control Plane (75-84) - Core 3 (or available)
if [ $NUM_CORES -ge 4 ]; then
    taskset -c $CORE3 chrt -f 82 sudo env "PATH=$PATH" python3 _daq_core/hw_controller.py 2> "$LOG_DIR/hwc.log" &
else
    # Use any available core for systems with fewer cores
    chrt -f 82 sudo env "PATH=$PATH" python3 _daq_core/hw_controller.py 2> "$LOG_DIR/hwc.log" &
fi
echo $! > "$PID_DIR/hw_controller.pid"
# root priviliges are needed to drive the i2c master

if [ $out_data_iface_type = eth ]; then
    echo "Output data interface: IQ ethernet server"
    # IQ server (medium-high priority) - Core 2 or 3 depending on system
    if [ $NUM_CORES -ge 4 ]; then
        taskset -c $CORE2 chrt -f 88 _daq_core/iq_server.out 2>"$LOG_DIR/iq_server.log" &
    else
        chrt -f 88 _daq_core/iq_server.out 2>"$LOG_DIR/iq_server.log" &
    fi
    echo $! > "$PID_DIR/iq_server.pid"
elif [ $out_data_iface_type = shmem ]; then
    echo "Output data interface: Shared memory"
fi

# IQ Eth sink used for testing
#sleep 3
#python3 _daq_core/iq_eth_sink.py 2>"$LOG_DIR/iq_eth_sink.log" &

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
