#!/bin/bash
#
# Host-global tuning for the HeIMDALL DAQ chain (spec B.7)
#
# Project: HeIMDALL DAQ Firmware
# License: GNU GPL V3
#
# Usage: sudo util/host_tuning.sh [--apply | --check] [--usb-core N]
#                                 [--controller PATTERN] [--eth-core N]
#                                 [--no-governor] [--no-drop-caches]
#   --apply (default)  run, in this order, the commands the start script ran
#                      before the tuning moved here: sysctl
#                      kernel.sched_rt_runtime_us=-1, irq_tuning.sh (next to
#                      this script; --usb-core/--controller/--eth-core/
#                      --no-governor forwarded only when given),
#                      usbfs_memory_mb = 0, drop_caches = 3
#                      (--no-drop-caches skips the last). Exit 1 when a step
#                      failed.
#   --check            print every setting; exit 0 when all are applied,
#                      1 otherwise (used by preflight and systemd).
#   Started once per host by heimdall-host-tuning.service, or per start by
#   daq_start_sm.sh with [host] host_tuning = 1|auto.
#   Test hook: HEIMDALL_TUNING_ROOT prefixes /proc and /sys (default: none).

MODE=apply
USB_CORE=""
CONTROLLER=""
ETH_CORE=""
NO_GOVERNOR=0
NO_DROP_CACHES=0

usage() {
    cat <<'EOF_USAGE'
Usage: sudo util/host_tuning.sh [--apply | --check] [--usb-core N]
                                [--controller PATTERN] [--eth-core N]
                                [--no-governor] [--no-drop-caches]
  --apply   (default) sysctl kernel.sched_rt_runtime_us=-1, irq_tuning.sh,
            usbfs_memory_mb = 0, drop_caches = 3; exit 1 when a step failed
  --check   print every setting; exit 0 when all are applied, 1 otherwise
EOF_USAGE
    exit "${1:-2}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) MODE=apply ;;
        --check) MODE=check ;;
        --usb-core|--eth-core|--controller)
            if [ $# -lt 2 ] || [ -z "$2" ]; then
                echo "host_tuning.sh: $1 needs a value" >&2
                exit 2
            fi
            case "$1" in
                --controller) CONTROLLER="$2" ;;
                *)
                    case "$2" in
                        *[!0-9]*)
                            echo "host_tuning.sh: $1 needs a non-negative integer (got '$2')" >&2
                            exit 2
                            ;;
                    esac
                    if [ "$1" = --usb-core ]; then USB_CORE="$2"; else ETH_CORE="$2"; fi
                    ;;
            esac
            shift
            ;;
        --no-governor) NO_GOVERNOR=1 ;;
        --no-drop-caches) NO_DROP_CACHES=1 ;;
        -h|--help) usage 0 ;;
        *)
            echo "host_tuning.sh: unknown argument '$1'" >&2
            usage 2
            ;;
    esac
    shift
done

HERE=$(cd "$(dirname "$0")" && pwd)
PROC="${HEIMDALL_TUNING_ROOT:-}/proc"
SYS="${HEIMDALL_TUNING_ROOT:-}/sys"
USBFS="$SYS/module/usbcore/parameters/usbfs_memory_mb"

irq_flags=()
[ -n "$USB_CORE" ] && irq_flags+=(--usb-core "$USB_CORE")
[ -n "$CONTROLLER" ] && irq_flags+=(--controller "$CONTROLLER")
[ -n "$ETH_CORE" ] && irq_flags+=(--eth-core "$ETH_CORE")
[ "$NO_GOVERNOR" = 1 ] && irq_flags+=(--no-governor)

if [ "$MODE" = apply ]; then
    failed=0
    sysctl -w kernel.sched_rt_runtime_us=-1 || failed=$((failed + 1))

    if [ -x "$HERE/irq_tuning.sh" ]; then
        "$HERE/irq_tuning.sh" "${irq_flags[@]}" || failed=$((failed + 1))
    else
        echo "WARN: $HERE/irq_tuning.sh not found, skipping IRQ tuning"
    fi

    if [ -e "$USBFS" ]; then
        echo 0 > "$USBFS" || failed=$((failed + 1))
    else
        echo "WARN: /sys/module/usbcore/parameters/usbfs_memory_mb not present, skipping"
    fi

    if [ "$NO_DROP_CACHES" = 0 ]; then
        echo '3' > "$PROC/sys/vm/drop_caches" || failed=$((failed + 1))
    fi

    if [ "$failed" -gt 0 ]; then
        echo "host tuning: $failed step(s) failed"
        exit 1
    fi
    echo "host tuning applied"
    exit 0
fi

# --check -------------------------------------------------------------------

bad=0
report() {  # STATUS NAME VALUE [WANT]
    if [ "$1" = FAIL ]; then
        bad=$((bad + 1))
        printf '%-4s %s = %s (want %s)\n' "$1" "$2" "$3" "$4"
    else
        printf '%-4s %s = %s\n' "$1" "$2" "$3"
    fi
}

# Normalise an smp_affinity / rps_cpus bitmap: no commas, no leading zeros
norm_mask() {
    local m
    m=$(printf '%s' "$1" | tr -d ',\n' | tr 'A-F' 'a-f' | sed 's/^0*//')
    printf '%s\n' "${m:-0}"
}

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

rt=$(cat "$PROC/sys/kernel/sched_rt_runtime_us" 2>/dev/null)
if [ "$rt" = "-1" ]; then
    report OK kernel.sched_rt_runtime_us "$rt"
else
    report FAIL kernel.sched_rt_runtime_us "${rt:-unreadable}" -1
fi

if [ -e "$USBFS" ]; then
    v=$(cat "$USBFS" 2>/dev/null)
    if [ "$v" = 0 ]; then report OK usbfs_memory_mb "$v"; else report FAIL usbfs_memory_mb "${v:-unreadable}" 0; fi
else
    report N/A usbfs_memory_mb "not present"
fi

if [ "$NO_GOVERNOR" = 0 ]; then
    n_gov=0
    for gov in "$SYS"/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        [ -f "$gov" ] || continue
        n_gov=$((n_gov + 1))
        v=$(cat "$gov" 2>/dev/null)
        cpu=${gov%/cpufreq/*}
        cpu=${cpu##*/}
        if [ "$v" = performance ]; then report OK "$cpu governor" "$v"; else report FAIL "$cpu governor" "${v:-unreadable}" performance; fi
    done
    [ "$n_gov" -eq 0 ] && report N/A "cpu governor" "no cpufreq"
fi

# Expected IRQ masks: the values irq_tuning.sh writes
case $(uname -m) in
    aarch64|arm64) usb_mask=1; eth_mask=8 ;;
    x86_64) usb_mask=1; eth_mask=128 ;;
    *) usb_mask=""; eth_mask="" ;;
esac
[ -n "$USB_CORE" ] && usb_mask=$(core_mask "$USB_CORE")
[ -n "$ETH_CORE" ] && eth_mask=$(core_mask "$ETH_CORE")

check_irqs() {  # LABEL MASK IRQ...
    local label mask irq f v
    label="$1"
    mask="$2"
    shift 2
    for irq in "$@"; do
        f="$PROC/irq/$irq/smp_affinity"
        [ -f "$f" ] || continue
        v=$(cat "$f" 2>/dev/null)
        if [ "$(norm_mask "$v")" = "$(norm_mask "$mask")" ]; then
            report OK "$label IRQ $irq affinity" "$v"
        else
            report FAIL "$label IRQ $irq affinity" "${v:-unreadable}" "$mask"
        fi
    done
}

usb_irqs=$(grep -E "(xhci|ehci|usb|dwc)" "$PROC/interrupts" 2>/dev/null | { if [ -n "$CONTROLLER" ]; then grep -F -- "$CONTROLLER"; else cat; fi; } | cut -d: -f1 | tr -d ' ')
eth_irqs=$(grep -E "eth|enp|wlan|bcmgenet" "$PROC/interrupts" 2>/dev/null | cut -d: -f1 | tr -d ' ')
if [ -z "$usb_mask" ]; then
    report N/A "IRQ affinity" "unsupported architecture $(uname -m) (pass --usb-core/--eth-core)"
else
    # shellcheck disable=SC2086
    check_irqs USB "$usb_mask" $usb_irqs
fi
if [ -n "$eth_mask" ]; then
    # shellcheck disable=SC2086
    check_irqs Network "$eth_mask" $eth_irqs
fi

for rps in "$SYS"/class/net/*/queues/rx-*/rps_cpus; do
    [ -f "$rps" ] || continue
    v=$(cat "$rps" 2>/dev/null)
    name=${rps#"$SYS"/class/net/}
    if [ "$(norm_mask "$v")" = 0 ]; then report OK "rps $name" "$v"; else report FAIL "rps $name" "${v:-unreadable}" 0; fi
done

if [ "$bad" -gt 0 ]; then
    echo "host tuning: $bad setting(s) not applied"
    exit 1
fi
echo "host tuning: all settings applied"
exit 0
