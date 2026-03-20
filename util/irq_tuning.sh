#!/bin/bash
#
# IRQ Affinity Tuning for HeIMDALL DAQ Performance
#
# Project: HeIMDALL DAQ Firmware
# License: GNU GPL V3
# Author: Generated via Claude Code optimization plan

set -e

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "IRQ tuning requires root privileges. Run with sudo."
    exit 1
fi

echo "Configuring IRQ affinity for optimal DAQ performance..."

# Detect platform and set core assignments
HOST_ARCH=$(uname -m)
case $HOST_ARCH in
    aarch64|arm64)
        # ARM64 systems (Raspberry Pi 4, Apple Silicon)
        USB_CORE_MASK=1    # Core 0 (rtl_daq affinity)
        ETH_CORE_MASK=8    # Core 3 (hw_controller affinity)
        NUM_CORES=4
        ;;
    x86_64)
        # x86_64 systems with 8+ cores
        USB_CORE_MASK=1    # Core 0
        ETH_CORE_MASK=128  # Core 7
        NUM_CORES=$(nproc)
        ;;
    *)
        echo "Unsupported architecture: $HOST_ARCH"
        exit 1
        ;;
esac

echo "Detected $NUM_CORES cores on $HOST_ARCH platform"

# Pin USB controller IRQs to core 0 (with rtl_daq)
USB_IRQS=$(grep -E "(xhci|ehci|usb|dwc)" /proc/interrupts 2>/dev/null | cut -d: -f1 | tr -d ' ' || true)
for irq in $USB_IRQS; do
    if [ -w /proc/irq/$irq/smp_affinity ]; then
        echo $USB_CORE_MASK > /proc/irq/$irq/smp_affinity
        echo "USB IRQ $irq -> core mask $USB_CORE_MASK"
    fi
done

# Pin Ethernet IRQs to dedicated core
ETH_IRQS=$(grep -E "eth|enp|wlan|bcmgenet" /proc/interrupts 2>/dev/null | cut -d: -f1 | tr -d ' ' || true)
for irq in $ETH_IRQS; do
    if [ -w /proc/irq/$irq/smp_affinity ]; then
        echo $ETH_CORE_MASK > /proc/irq/$irq/smp_affinity
        echo "Network IRQ $irq -> core mask $ETH_CORE_MASK"
    fi
done

# Disable RPS (Receive Packet Steering) to keep network processing on assigned cores
for iface in /sys/class/net/*/queues/rx-*/rps_cpus; do
    if [ -w "$iface" ]; then
        echo 0 > "$iface"
    fi
done

# Set CPU governor to performance for real-time workload
for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    if [ -w "$gov" ]; then
        echo performance > "$gov"
    fi
done

echo "IRQ affinity tuning completed"