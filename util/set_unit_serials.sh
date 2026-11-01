#!/bin/bash
# Program the EEPROM serials of one KrakenSDR unit for a multi-unit host or cluster
# (design spec A.5).
#
#   sudo util/set_unit_serials.sh --list
#   sudo util/set_unit_serials.sh --unit K [--count M] [--base 1000] [--stride 100]
#                                 [--by-index] [--dry-run] [--yes] [--force]
#
# Convention: unit K is programmed to base + K*stride + i (i = factory channel index), so
# the instance that owns unit K sets [hw] serial_base = base + K*stride. Plug in ONE unit
# at a time: the tool refuses unless exactly --count RTL2832U receivers are attached.
#
# Default mapping: read every receiver's serial s_j (rtl_eeprom -d j), old_min = min(s_j),
# new serial = base + K*stride + (s_j - old_min). This keeps the factory channel order
# (1000..1004 -> 1100..1104 for K = 1). --by-index maps librtlsdr index j to
# base + K*stride + j instead.
#
# Without --yes (or with --dry-run) only the plan is printed. With --yes every receiver
# is written (rtl_eeprom -d j -s NEW, answering its y/n prompt), then all are re-read
# and verified.
#
# Exit codes: 0 ok / plan printed, 2 usage, 3 a DAQ chain or another program holds the
#   receivers, 4 receiver count != --count, 5 rtl_eeprom not on PATH,
#   6 old serials not contiguous (default mapping; --force overrides), 7 verify failed.
#
# Test hooks: HEIMDALL_SYSFS_ROOT (default /sys/bus/usb/devices), HEIMDALL_RUN_DIR
# (default /run/heimdall, plus /tmp/heimdall-run-<uid>), PATH-stubbed rtl_eeprom/pgrep.

SYSFS_ROOT=${HEIMDALL_SYSFS_ROOT:-/sys/bus/usb/devices}
MAX_SERIAL=99999999

USAGE="usage: set_unit_serials.sh --list
       set_unit_serials.sh --unit K [--count M] [--base 1000] [--stride 100]
                           [--by-index] [--dry-run] [--yes] [--force]"

usage() {
    echo "$USAGE" >&2
    [ -n "${1:-}" ] && echo "set_unit_serials.sh: $1" >&2
    exit 2
}

die() {
    local code=$1
    shift
    echo "ERROR: $*" >&2
    exit "$code"
}

is_uint() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

# --- arguments ---------------------------------------------------------------------
LIST=0
UNIT=""
COUNT=5
BASE=1000
STRIDE=100
BY_INDEX=0
DRY_RUN=0
YES=0
FORCE=0
[ $# -eq 0 ] && usage "no action given (--list or --unit K)"
while [ $# -gt 0 ]; do
    case "$1" in
        --list) LIST=1 ;;
        --unit) [ $# -ge 2 ] || usage "--unit needs a value"; UNIT=$2; shift ;;
        --count) [ $# -ge 2 ] || usage "--count needs a value"; COUNT=$2; shift ;;
        --base) [ $# -ge 2 ] || usage "--base needs a value"; BASE=$2; shift ;;
        --stride) [ $# -ge 2 ] || usage "--stride needs a value"; STRIDE=$2; shift ;;
        --by-index) BY_INDEX=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --yes) YES=1 ;;
        --force) FORCE=1 ;;
        -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
        *) usage "unknown argument '$1'" ;;
    esac
    shift
done

if [ "$LIST" -eq 1 ]; then
    [ -z "$UNIT" ] || usage "--list and --unit are exclusive"
else
    [ -n "$UNIT" ] || usage "--unit K is required (or --list)"
    is_uint "$UNIT" || usage "--unit must be a non-negative integer"
    { is_uint "$COUNT" && [ "$COUNT" -ge 1 ] && [ "$COUNT" -le 31 ]; } || usage "--count must be 1..31"
    is_uint "$BASE" || usage "--base must be a non-negative integer"
    { is_uint "$STRIDE" && [ "$STRIDE" -ge 1 ]; } || usage "--stride must be a positive integer"
    # at most 8 digits each (the EEPROM limit) keeps the arithmetic far from 64-bit overflow
    { [ ${#UNIT} -le 8 ] && [ ${#BASE} -le 8 ] && [ ${#STRIDE} -le 8 ]; } ||
        usage "--unit, --base and --stride take at most 8 digits (serials are 8-digit)"
    [ "$COUNT" -le "$STRIDE" ] || usage "--count $COUNT exceeds --stride $STRIDE: units would share serials"
    # 10# : strip leading zeros so bash does not read the values as octal
    UNIT=$((10#$UNIT)); COUNT=$((10#$COUNT)); BASE=$((10#$BASE)); STRIDE=$((10#$STRIDE))
    UNIT_BASE=$((BASE + UNIT * STRIDE))
    { [ "$UNIT_BASE" -ge 0 ] && [ $((UNIT_BASE + COUNT - 1)) -le $MAX_SERIAL ]; } ||
        usage "serials above $MAX_SERIAL do not fit the 8-digit EEPROM serial"
fi

command -v rtl_eeprom >/dev/null 2>&1 || die 5 "rtl_eeprom is not on PATH (install the krakenrf librtlsdr tools)"

# --- sysfs enumeration (opens no device) -------------------------------------------
# SYSFS_SERIAL[k] / SYSFS_BUSDEV[k] of every RTL2832U (idVendor 0bda, idProduct 2838/2832)
SYSFS_SERIAL=()
SYSFS_BUSDEV=()
read_attr() {
    [ -r "$1" ] && head -n 1 "$1" 2>/dev/null | tr -d '[:space:]'
}
enumerate_sysfs() {
    local dev vendor product
    [ -d "$SYSFS_ROOT" ] || return 0
    for dev in "$SYSFS_ROOT"/*; do
        [ -d "$dev" ] || continue
        vendor=$(read_attr "$dev/idVendor" | tr '[:upper:]' '[:lower:]')
        product=$(read_attr "$dev/idProduct" | tr '[:upper:]' '[:lower:]')
        [ "$vendor" = "0bda" ] || continue
        case "$product" in 2838|2832) ;; *) continue ;; esac
        SYSFS_SERIAL+=("$(read_attr "$dev/serial")")
        SYSFS_BUSDEV+=("$(read_attr "$dev/busnum"):$(read_attr "$dev/devnum")")
    done
}

# --- rtl_eeprom probing --------------------------------------------------------------
# probe J -> sets PROBE_STATE (ok|busy|absent|none), PROBE_SERIAL, PROBE_FOUND (count or "")
probe() {
    local j=$1 out found
    out=$(rtl_eeprom -d "$j" 2>&1 </dev/null)
    PROBE_SERIAL=""
    PROBE_FOUND=""
    if printf '%s\n' "$out" | grep -q "No supported devices found"; then
        PROBE_STATE=none
        PROBE_FOUND=0
        return
    fi
    found=$(printf '%s\n' "$out" | sed -n 's/.*Found \([0-9][0-9]*\) device(s).*/\1/p' | head -n 1)
    PROBE_FOUND=$found
    if [ -n "$found" ] && [ "$j" -ge "$found" ]; then
        PROBE_STATE=absent
        return
    fi
    PROBE_SERIAL=$(printf '%s\n' "$out" | sed -n 's/^Serial number:[[:space:]]*//p' | head -n 1 | tr -d '[:space:]')
    if printf '%s\n' "$out" | grep -q -e "usb_claim_interface error" -e "Failed to open"; then
        PROBE_STATE=busy
        PROBE_SERIAL=""
    elif [ -n "$PROBE_SERIAL" ]; then
        PROBE_STATE=ok
    else
        PROBE_STATE=absent
    fi
}

# bus:dev of the sysfs receiver whose serial is $1 (exactly one match), else "-"
busdev_of_serial() {
    local k match="-" hits=0
    for k in "${!SYSFS_SERIAL[@]}"; do
        if [ -n "$1" ] && [ "${SYSFS_SERIAL[$k]}" = "$1" ]; then
            match=${SYSFS_BUSDEV[$k]}
            hits=$((hits + 1))
        fi
    done
    if [ "$hits" -eq 1 ]; then printf '%s' "$match"; else printf '%s' "-"; fi
}

# --- liveness (exit 3) ---------------------------------------------------------------
pid_role_alive() {
    kill -0 "$1" 2>/dev/null || return 1
    ps -o args= -p "$1" 2>/dev/null | grep -q -F -- "$2"
}
live_registry() {
    local dir f pids item
    for dir in "${HEIMDALL_RUN_DIR:-/run/heimdall}" "/tmp/heimdall-run-$(id -u)"; do
        for f in "$dir"/inst*.env; do
            [ -f "$f" ] || continue
            pids=$(sed -n 's/^PIDS=//p' "$f" | head -n 1)
            for item in ${pids//,/ }; do
                if pid_role_alive "${item#*:}" "${item%%:*}"; then
                    printf '%s\n' "$f"
                    return 0
                fi
            done
        done
    done
    return 1
}
refuse_if_busy() {
    local entry
    if pgrep -x rtl_daq.out >/dev/null 2>&1; then
        die 3 "rtl_daq.out is running; stop every DAQ chain first (EEPROM writes to a claimed receiver fail or corrupt it)"
    fi
    if entry=$(live_registry); then
        die 3 "a DAQ instance is live ($entry); stop it first"
    fi
}

enumerate_sysfs

# --- --list ----------------------------------------------------------------------------
if [ "$LIST" -eq 1 ]; then
    probe 0
    total=${PROBE_FOUND:-0}
    [ "$PROBE_STATE" = "none" ] && total=0
    if [ -z "$PROBE_FOUND" ] && [ "$PROBE_STATE" != "none" ]; then
        total=1  # rtl_eeprom printed no count: list index 0 only
    fi
    printf '%-4s %-9s %-13s %-13s %s\n' idx bus:dev sysfs-serial eeprom-serial state
    matched=()
    for ((j = 0; j < total; j++)); do
        [ "$j" -gt 0 ] && probe "$j"
        case "$PROBE_STATE" in
            ok) state=free ;;
            busy) state=busy ;;
            *) continue ;;
        esac
        bd=$(busdev_of_serial "$PROBE_SERIAL")
        sys="-"
        [ "$bd" != "-" ] && { sys=$PROBE_SERIAL; matched+=("$bd"); }
        printf '%-4s %-9s %-13s %-13s %s\n' "$j" "$bd" "$sys" "${PROBE_SERIAL:--}" "$state"
    done
    for k in "${!SYSFS_SERIAL[@]}"; do
        seen=0
        for bd in "${matched[@]+"${matched[@]}"}"; do
            [ "$bd" = "${SYSFS_BUSDEV[$k]}" ] && seen=1
        done
        [ "$seen" -eq 1 ] && continue
        printf '%-4s %-9s %-13s %-13s %s\n' "-" "${SYSFS_BUSDEV[$k]}" "${SYSFS_SERIAL[$k]:--}" "-" "unmatched"
    done
    echo "${#SYSFS_SERIAL[@]} RTL2832U receiver(s) in sysfs, $total in librtlsdr."
    echo "unmatched: a sysfs receiver whose plug-in serial matches no EEPROM (busy, or written and not yet power-cycled)."
    exit 0
fi

# --- --unit K --------------------------------------------------------------------------
refuse_if_busy
n_sysfs=${#SYSFS_SERIAL[@]}
if [ "$n_sysfs" -ne "$COUNT" ]; then
    die 4 "found $n_sysfs RTL2832U receiver(s) in sysfs ($SYSFS_ROOT), expected --count $COUNT; plug in exactly one unit"
fi

OLD=()
for ((j = 0; j < COUNT; j++)); do
    probe "$j"
    if [ -n "$PROBE_FOUND" ] && [ "$PROBE_FOUND" -ne "$COUNT" ]; then
        die 4 "rtl_eeprom found $PROBE_FOUND receiver(s), expected --count $COUNT"
    fi
    case "$PROBE_STATE" in
        ok) OLD+=("$PROBE_SERIAL") ;;
        busy) die 3 "receiver index $j is busy (claimed by another program); close it and retry" ;;
        *) die 4 "receiver index $j could not be read with rtl_eeprom" ;;
    esac
done

NEW=()
if [ "$BY_INDEX" -eq 1 ]; then
    for ((j = 0; j < COUNT; j++)); do
        NEW+=($((UNIT_BASE + j)))
    done
else
    old_min=""
    for s in "${OLD[@]}"; do
        { is_uint "$s" && [ "${#s}" -le 8 ]; } || die 6 "receiver serial '$s' is not a decimal number; use --by-index"
        v=$((10#$s))
        if [ -z "$old_min" ] || [ "$v" -lt "$old_min" ]; then old_min=$v; fi
    done
    contiguous=1
    for ((d = 0; d < COUNT; d++)); do
        hit=0
        for s in "${OLD[@]}"; do
            [ $((10#$s)) -eq $((old_min + d)) ] && hit=1
        done
        [ "$hit" -eq 1 ] || contiguous=0
    done
    if [ "$contiguous" -ne 1 ]; then
        [ "$FORCE" -eq 1 ] || die 6 "old serials (${OLD[*]}) are not $old_min..$((old_min + COUNT - 1)); use --by-index, or --force to map them by offset from $old_min"
        echo "WARNING: old serials (${OLD[*]}) are not contiguous; mapping by offset from $old_min (--force)" >&2
    fi
    for s in "${OLD[@]}"; do
        off=$((10#$s - old_min))
        [ "$off" -lt "$STRIDE" ] || die 6 "serial $s is $off above $old_min, not below --stride $STRIDE; use --by-index"
        NEW+=($((UNIT_BASE + off)))
    done
    # distinct targets (duplicate old serials would collide)
    for ((a = 0; a < COUNT; a++)); do
        for ((b = a + 1; b < COUNT; b++)); do
            [ "${NEW[$a]}" != "${NEW[$b]}" ] || die 6 "receivers $a and $b both have serial ${OLD[$a]}; use --by-index"
        done
    done
fi

echo "Unit $UNIT: $COUNT receiver(s), serials -> $UNIT_BASE..$((UNIT_BASE + COUNT - 1)) ($([ "$BY_INDEX" -eq 1 ] && echo "by librtlsdr index" || echo "factory order kept"))"
printf '%-4s %-9s %-13s %s\n' idx bus:dev old-serial new-serial
changes=0
for ((j = 0; j < COUNT; j++)); do
    note=""
    if [ "${OLD[$j]}" = "${NEW[$j]}" ]; then
        note="  (unchanged)"
    else
        changes=$((changes + 1))
    fi
    printf '%-4s %-9s %-13s %s%s\n' "$j" "$(busdev_of_serial "${OLD[$j]}")" "${OLD[$j]}" "${NEW[$j]}" "$note"
done

config_hint() {
    echo "Power-cycle the unit, then set in daq_chain_config.ini: [hw] serial_base = $UNIT_BASE"
    echo "and [daq] ctr_channel_serial_no = $UNIT_BASE"
}

if [ "$YES" -ne 1 ] || [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run: nothing written. Re-run with --yes to program the EEPROMs."
    exit 0
fi

failed=0
for ((j = 0; j < COUNT; j++)); do
    [ "${OLD[$j]}" = "${NEW[$j]}" ] && continue
    echo "Writing receiver $j: ${OLD[$j]} -> ${NEW[$j]}"
    if ! out=$(printf 'y\n' | rtl_eeprom -d "$j" -s "${NEW[$j]}" 2>&1); then
        echo "ERROR: rtl_eeprom -d $j -s ${NEW[$j]} failed:" >&2
        printf '%s\n' "$out" | tail -n 3 >&2
        failed=1
    fi
done

mismatch=0
for ((j = 0; j < COUNT; j++)); do
    probe "$j"
    if [ "$PROBE_STATE" != "ok" ] || [ "$PROBE_SERIAL" != "${NEW[$j]}" ]; then
        echo "ERROR: verify: receiver $j reads '${PROBE_SERIAL:-?}' ($PROBE_STATE), expected ${NEW[$j]}" >&2
        mismatch=1
    fi
done
if [ "$mismatch" -ne 0 ]; then
    [ "$failed" -ne 0 ] && echo "ERROR: one or more writes failed (see above)" >&2
    exit 7
fi
echo "Verified: $changes receiver(s) written, all $COUNT read back as planned."
config_hint
exit 0
