#!/bin/bash
#
# IRQ Affinity Tuning for HeIMDALL DAQ Performance
#
# Project: HeIMDALL DAQ Firmware
# License: GNU GPL V3
# Author: Generated via Claude Code optimization plan
#
# Usage: sudo ./irq_tuning.sh [--usb-core N] [--controller PATTERN]
#                             [--eth-core N] [--no-governor] [--keep-going]
#                             [--any-arch]
#   Without flags it does exactly what it always did. Each flag changes only
#   its own aspect:
#   --usb-core N          pin USB IRQs to core N (default core 0)
#   --controller PATTERN  pin only the USB IRQ lines of /proc/interrupts that
#                         contain PATTERN (e.g. xhci_hcd:usb3); default all
#   --eth-core N          pin network IRQs to core N
#   --no-governor         leave the CPU frequency governor alone
#   --keep-going          report and count failed writes instead of aborting;
#                         exit 1 at the end when any write failed
#   --any-arch            on architectures not listed below (e.g. armv7l) use
#                         nproc, core 0 for USB and the last core for network
#   Test hook: HEIMDALL_TUNING_ROOT prefixes /proc and /sys (default: none);
#   with it set, HEIMDALL_TUNING_FAKE_EUID overrides the root check so the
#   fake-tree tests run unprivileged (ignored without HEIMDALL_TUNING_ROOT).

set -e

USB_CORE=""
CONTROLLER=""
ETH_CORE=""
NO_GOVERNOR=0
KEEP_GOING=0
ANY_ARCH=0
while [ $# -gt 0 ]; do
    case "$1" in
        --usb-core|--eth-core|--controller)
            if [ $# -lt 2 ] || [ -z "$2" ]; then
                echo "irq_tuning.sh: $1 needs a value"
                exit 2
            fi
            case "$1" in
                --controller) CONTROLLER="$2" ;;
                *)
                    case "$2" in
                        *[!0-9]*)
                            echo "irq_tuning.sh: $1 needs a non-negative integer (got '$2')"
                            exit 2
                            ;;
                    esac
                    if [ "$1" = --usb-core ]; then USB_CORE="$2"; else ETH_CORE="$2"; fi
                    ;;
            esac
            shift
            ;;
        --no-governor) NO_GOVERNOR=1 ;;
        --keep-going) KEEP_GOING=1 ;;
        --any-arch) ANY_ARCH=1 ;;
        *) echo "irq_tuning.sh: ignoring unknown argument '$1'" ;;
    esac
    shift
done

PROC="${HEIMDALL_TUNING_ROOT:-}/proc"
SYS="${HEIMDALL_TUNING_ROOT:-}/sys"
FAILED=0

# Hex smp_affinity mask of one core (comma-separated 32-bit groups)
core_mask() {
    local groups mask
    groups=$(( $1 / 32 ))
    mask=$(printf '%x' $(( 1 << ($1 % 32) )))
    while [ "$groups" -gt 0 ]; do
        mask="$mask,00000000"
        groups=$((groups - 1))
    done
    echo "$mask"
}

# write_value VALUE FILE [LOG]: a failed write aborts the script (set -e) as
# it always did, unless --keep-going (then it is reported and counted). LOG
# is printed after a successful write. Must be called as a plain command (a
# condition context would suspend set -e).
write_value() {
    if [ "$KEEP_GOING" = 1 ]; then
        if ! { echo "$1" > "$2"; } 2>/dev/null; then
            echo "WARN: cannot write $1 to $2"
            FAILED=$((FAILED + 1))
            return 0
        fi
    else
        echo "$1" > "$2"
    fi
    if [ -n "${3:-}" ]; then
        echo "$3"
    fi
}

# Check if running as root
TUNING_EUID=$EUID
if [ -n "${HEIMDALL_TUNING_ROOT:-}" ] && [ -n "${HEIMDALL_TUNING_FAKE_EUID:-}" ]; then
    TUNING_EUID=$HEIMDALL_TUNING_FAKE_EUID
fi
if [ "$TUNING_EUID" -ne 0 ]; then
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
        if [ "$ANY_ARCH" = 1 ]; then
            NUM_CORES=$(nproc)
            USB_CORE_MASK=1
            ETH_CORE_MASK=$(core_mask $((NUM_CORES - 1)))
        else
            echo "Unsupported architecture: $HOST_ARCH"
            exit 1
        fi
        ;;
esac
if [ -n "$USB_CORE" ]; then
    USB_CORE_MASK=$(core_mask "$USB_CORE")
fi
if [ -n "$ETH_CORE" ]; then
    ETH_CORE_MASK=$(core_mask "$ETH_CORE")
fi

echo "Detected $NUM_CORES cores on $HOST_ARCH platform"

# Pin USB controller IRQs to core 0 (with rtl_daq)
USB_IRQS=$(grep -E "(xhci|ehci|usb|dwc)" "$PROC/interrupts" 2>/dev/null | { if [ -n "$CONTROLLER" ]; then grep -F -- "$CONTROLLER"; else cat; fi; } | cut -d: -f1 | tr -d ' ' || true)
for irq in $USB_IRQS; do
    if [ -w "$PROC/irq/$irq/smp_affinity" ]; then
        write_value "$USB_CORE_MASK" "$PROC/irq/$irq/smp_affinity" "USB IRQ $irq -> core mask $USB_CORE_MASK"
    fi
done

# Pin Ethernet IRQs to dedicated core
ETH_IRQS=$(grep -E "eth|enp|wlan|bcmgenet" "$PROC/interrupts" 2>/dev/null | cut -d: -f1 | tr -d ' ' || true)
for irq in $ETH_IRQS; do
    if [ -w "$PROC/irq/$irq/smp_affinity" ]; then
        write_value "$ETH_CORE_MASK" "$PROC/irq/$irq/smp_affinity" "Network IRQ $irq -> core mask $ETH_CORE_MASK"
    fi
done

# Disable RPS (Receive Packet Steering) to keep network processing on assigned cores
for iface in "$SYS"/class/net/*/queues/rx-*/rps_cpus; do
    if [ -w "$iface" ]; then
        write_value 0 "$iface"
    fi
done

# Set CPU governor to performance for real-time workload
if [ "$NO_GOVERNOR" = 0 ]; then
    for gov in "$SYS"/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        if [ -w "$gov" ]; then
            write_value performance "$gov"
        fi
    done
fi

if [ "$FAILED" -gt 0 ]; then
    echo "IRQ affinity tuning completed with $FAILED failed write(s)"
    exit 1
fi
echo "IRQ affinity tuning completed"
