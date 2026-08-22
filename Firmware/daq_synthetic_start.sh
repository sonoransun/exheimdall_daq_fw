#!/bin/bash
#
#   DAQ chain start srcipt
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#   Authors: Tamas Peto, Carl Laufer

# Check config file (exit code 0 = valid; errors are printed on stderr)
if python3 ini_checker.py no_hw; then
      echo -e "\e[92mConfig file check [OK]\e[39m"
else
      echo -e "\e[91mConfig file check [FAIL]\e[39m"
      exit 1
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

# Generating FIR filter coefficients
python3 fir_filter_designer.py
out=$?
if test $out -ne 0
    then
        echo -e "\e[91mDAQ chain not started!\e[39m"
        exit
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

# Start main program chain -Thread 0 Normal (non squelch mode)
echo "Starting DAQ Subsystem with synthetic data source (instance ${instance_id})"
python3 _testing/test_data_synthesizer.py 2>"$LOG_DIR/synthetic.log" | \
_daq_core/rebuffer.out 0 2> "$LOG_DIR/rebuffer.log" &
echo $! > "$PID_DIR/rebuffer.pid"
# $! is the LAST process of the pipeline (rebuffer); the pipeline's process
# group leader is its FIRST process (the synthesizer) - record it too so
# per-instance daq_stop.sh can signal it.
jobs -p %+ > "$PID_DIR/synthesizer.pid" 2>/dev/null

# Decimator - Thread 1
chrt -f 99 _daq_core/decimate.out 2> "$LOG_DIR/decimator.log" &
echo $! > "$PID_DIR/decimate.pid"

# Delay synchronizer - Thread 2
python3 _daq_core/delay_sync.py 2> "$LOG_DIR/delay_sync.log" &
echo $! > "$PID_DIR/delay_sync.pid"

# Hardware Controller data path - Thread 3
sudo python3 _daq_core/hw_controller.py 2> "$LOG_DIR/hwc.log" &
echo $! > "$PID_DIR/hw_controller.pid"
# root priviliges are needed to drive the i2c master

if [ $out_data_iface_type = eth ]; then
    echo "Output data interface: IQ ethernet server"
    _daq_core/iq_server.out 2>"$LOG_DIR/iq_server.log" &
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

