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
#     ./daq_stop.sh --legacy  # Use legacy SIGRT(64) instead of SIGTERM
#
#   Opt-in forms (combinable with --legacy):
#     ./daq_stop.sh <id> --clean        after the stop, remove instance <id>'s
#                                       FIFOs, /dev/shm segments and relay files
#                                       (iq_track_lock, fir_coeffs, fence_term kept)
#     ./daq_stop.sh <id> --reset-fence  remove _data_control/<P>fence_term of a
#                                       stopped instance (refused, nothing
#                                       stopped, while a stage of <id> runs)
#     ./daq_stop.sh --this-copy         stop only the DAQ processes whose
#                                       working directory is this copy
#     ./daq_stop.sh --all               global kill + force-kill of the python
#                                       stages + removal of every registry entry
#     ./daq_stop.sh [...] --federation  then run ./federation_stop.sh all
#
#   Exit codes: 0 done; 2 conflicting flags; 3 --clean/--reset-fence refused
#   because a process of the instance is still alive. The no-argument global
#   kill keeps the exit status it always had.

GRACE_PERIOD=2
USE_SIGTERM=true
OPT_CLEAN=false
OPT_RESET_FENCE=false
OPT_THIS_COPY=false
OPT_ALL=false
OPT_FEDERATION=false

for arg in "$@"; do
    case "$arg" in
        --legacy) USE_SIGTERM=false ;;
        --clean) OPT_CLEAN=true ;;
        --reset-fence) OPT_RESET_FENCE=true ;;
        --this-copy) OPT_THIS_COPY=true ;;
        --all) OPT_ALL=true ;;
        --federation) OPT_FEDERATION=true ;;
    esac
done

# Filter out flags to get instance ID
INSTANCE_ID=""
for arg in "$@"; do
    case "$arg" in
        --*) ;;
        *) INSTANCE_ID="$arg" ;;
    esac
done

usage_error() {
    echo "daq_stop.sh: $1" >&2
    exit 2
}

if [ "$OPT_THIS_COPY" = true ] && [ -n "$INSTANCE_ID" ]; then
    usage_error "--this-copy cannot be combined with an instance id"
fi
if [ "$OPT_ALL" = true ] && { [ -n "$INSTANCE_ID" ] || [ "$OPT_THIS_COPY" = true ]; }; then
    usage_error "--all cannot be combined with an instance id or --this-copy"
fi
if [ "$OPT_RESET_FENCE" = true ] && [ -z "$INSTANCE_ID" ]; then
    usage_error "--reset-fence needs an instance id: ./daq_stop.sh <id> --reset-fence"
fi
if [ "$OPT_CLEAN" = true ] && [ -z "$INSTANCE_ID" ]; then
    usage_error "--clean needs an instance id: ./daq_stop.sh <id> --clean"
fi
if { [ "$OPT_CLEAN" = true ] || [ "$OPT_RESET_FENCE" = true ]; }; then
    case "$INSTANCE_ID" in
        ''|*[!0-9]*) usage_error "instance id must be a non-negative integer (got '$INSTANCE_ID')" ;;
    esac
fi

# Registry helpers (host registry, process checks). An old copy without
# daq_common.sh skips every registry action.
HD_COMMON=false
if [ -f ./daq_common.sh ]; then
    # shellcheck source=daq_common.sh
    . ./daq_common.sh
    HD_COMMON=true
fi

send_signal() {
    local pid="$1"
    if [ "$USE_SIGTERM" = true ]; then
        sudo kill -TERM "$pid" 2>/dev/null
    else
        sudo kill -64 "$pid" 2>/dev/null
    fi
}

# Return 0 when the PID's current command line (ps -o args=, portable) still
# matches the DAQ component named by its PID file (e.g. rtl_daq.pid ->
# "rtl_daq"). Guards against stale PID files whose PIDs were recycled by
# unrelated processes after a crash, OOM-kill or unclean reboot.
pid_matches_pidfile() {
    local pid="$1"
    local pidfile="$2"
    local name args
    name="${pidfile##*/}"
    name="${name%.pid}"
    [ -n "$name" ] || return 1
    args=$(ps -o args= -p "$pid" 2>/dev/null)
    case "$args" in
        *"$name"*) return 0 ;;
        *) return 1 ;;
    esac
}

wait_or_kill() {
    local pid="$1"
    local pidfile="$2"
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$GRACE_PERIOD" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        if pid_matches_pidfile "$pid" "$pidfile"; then
            sudo kill -KILL "$pid" 2>/dev/null
        else
            echo "Warning: stale PID file $pidfile - PID $pid belongs to an unrelated process, skipping force-kill"
        fi
    fi
}

# Signal every PID recorded under the given pids directory
signal_pid_dir() {
    local dir="$1"
    for pidfile in "$dir"/*.pid; do
        [ -f "$pidfile" ] || continue
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            if pid_matches_pidfile "$pid" "$pidfile"; then
                send_signal "$pid"
            else
                echo "Warning: stale PID file $pidfile - PID $pid belongs to an unrelated process, skipping"
            fi
        fi
    done
}

# Today's global orphan sweep by process name (TERM list, grace, KILL list)
global_sweep() {
    echo "Shut down DAQ chain .."

    # Primary path: the PID files the start scripts write for every instance
    # (including instance 0). This also catches processes launched via
    # non-default python interpreters that name-matching would miss.
    for pid_dir in _logs/inst*/pids; do
        [ -d "$pid_dir" ] || continue
        signal_pid_dir "$pid_dir"
    done

    # Fallback orphan sweep by process name (covers processes whose PID files
    # are missing/stale). Only the process names the start scripts actually
    # launch are listed here.
    if [ "$USE_SIGTERM" = true ]; then
        sudo pkill -TERM rtl_daq.out 2>/dev/null
        sudo pkill -TERM decimate.out 2>/dev/null
        sudo pkill -TERM rebuffer.out 2>/dev/null
        sudo pkill -TERM iq_server.out 2>/dev/null
        sudo pkill -TERM -f "python.*_testing/test_data_synthesizer.py" 2>/dev/null
        sudo pkill -TERM -f "python.*_daq_core/delay_sync.py" 2>/dev/null
        sudo pkill -TERM -f "python.*_daq_core/hw_controller.py" 2>/dev/null
    else
        sudo pkill -64 rtl_daq.out 2>/dev/null
        sudo pkill -64 decimate.out 2>/dev/null
        sudo pkill -64 rebuffer.out 2>/dev/null
        sudo pkill -64 iq_server.out 2>/dev/null
        sudo pkill -64 -f "python.*_testing/test_data_synthesizer.py" 2>/dev/null
        sudo pkill -64 -f "python.*_daq_core/delay_sync.py" 2>/dev/null
        sudo pkill -64 -f "python.*_daq_core/hw_controller.py" 2>/dev/null
    fi

    # Grace period then force-kill any survivors
    sleep "$GRACE_PERIOD"
    for pid_dir in _logs/inst*/pids; do
        [ -d "$pid_dir" ] || continue
        for pidfile in "$pid_dir"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                if pid_matches_pidfile "$pid" "$pidfile"; then
                    sudo kill -KILL "$pid" 2>/dev/null
                else
                    echo "Warning: stale PID file $pidfile - PID $pid belongs to an unrelated process, skipping force-kill"
                fi
            fi
        done
    done
    sudo pkill -KILL rtl_daq.out 2>/dev/null
    sudo pkill -KILL decimate.out 2>/dev/null
    sudo pkill -KILL rebuffer.out 2>/dev/null
    sudo pkill -KILL iq_server.out 2>/dev/null
    sudo pkill -KILL -f "python.*_testing/test_data_synthesizer.py" 2>/dev/null
    if [ "$OPT_ALL" = true ]; then
        sudo pkill -KILL -f "python.*_daq_core/delay_sync.py" 2>/dev/null
        sudo pkill -KILL -f "python.*_daq_core/hw_controller.py" 2>/dev/null
    fi

    # Clean up PID files
    find _logs/inst*/pids -name "*.pid" -exec rm -f {} \; 2>/dev/null
}

# Today's per-instance stop through the PID files. With --clean or
# --reset-fence, STILL_ALIVE=true records a process of the instance that
# survived the stop (e.g. it could not be signalled).
STILL_ALIVE=false
stop_instance() {
    echo "Shutting down DAQ instance $INSTANCE_ID .."
    PID_DIR="_logs/inst${INSTANCE_ID}/pids"
    if [ -d "$PID_DIR" ]; then
        # Send signal to all processes first
        signal_pid_dir "$PID_DIR"
        # Wait for graceful exit, then force-kill stragglers
        for pidfile in "$PID_DIR"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null && ! pid_matches_pidfile "$pid" "$pidfile"; then
                echo "Warning: stale PID file $pidfile - PID $pid belongs to an unrelated process, skipping"
            else
                wait_or_kill "$pid" "$pidfile"
                if [ "$OPT_CLEAN" = true ] || [ "$OPT_RESET_FENCE" = true ]; then
                    # A survivor keeps its PID file, so a retry is refused too
                    note_survivor "$pid" "$pidfile" || continue
                fi
            fi
            rm -f "$pidfile"
        done
        echo "Instance $INSTANCE_ID stopped"
    else
        echo "Warning: No PID directory found for instance $INSTANCE_ID at $PID_DIR"
    fi
}

# pid_file_alive PID PIDFILE: 0 when PID runs the role its PID file names.
# With daq_common.sh, a process this user cannot signal (kill -0 EPERM, e.g.
# the root-owned hw_controller for a non-root caller) still counts (ps -p).
pid_file_alive() {
    local role
    if [ "$HD_COMMON" = true ]; then
        role="${2##*/}"
        hd_pid_role_alive "$1" "${role%.pid}"
    else
        kill -0 "$1" 2>/dev/null && pid_matches_pidfile "$1" "$2"
    fi
}

# A force-killed process can take a moment to disappear; one second of grace
# before it counts as a survivor. Returns 1 for a survivor.
note_survivor() {
    pid_file_alive "$1" "$2" || return 0
    sleep 1
    if pid_file_alive "$1" "$2"; then
        STILL_ALIVE=true
        echo "Warning: $2 - PID $1 is still running"
        return 1
    fi
    return 0
}

# --reset-fence without --clean: refused before any signal while a process
# of the instance recorded in this copy's PID dir is alive.
instance_has_live_pid() {
    local pidfile pid live
    live=1
    for pidfile in "_logs/inst${INSTANCE_ID}/pids"/*.pid; do
        [ -f "$pidfile" ] || continue
        pid=$(head -n 1 "$pidfile")
        case "$pid" in ''|*[!0-9]*) continue ;; esac
        if pid_file_alive "$pid" "$pidfile"; then
            echo "Warning: $pidfile - PID $pid is still running"
            live=0
        fi
    done
    return "$live"
}

# Registry entry of instance N: removed only when it belongs to this copy.
remove_registry_entry() {
    local here hint
    [ "$HD_COMMON" = true ] || return 0
    here=$(pwd -P)
    hint="_logs/inst${INSTANCE_ID}/pids/run_dir"
    {
        if [ -f "$hint" ]; then
            head -n 1 "$hint"
        fi
        printf '%s\n' "${HEIMDALL_RUN_DIR:-/run/heimdall}" "/tmp/heimdall-run-$(id -u)"
    } | awk 'NF && !seen[$0]++' | while IFS= read -r dir; do
        [ -f "$dir/inst${INSTANCE_ID}.env" ] || continue
        entry_cwd=$(hd_env_get "$dir/inst${INSTANCE_ID}.env" CWD)
        if [ "$entry_cwd" = "$here" ]; then
            rm -f "$dir/inst${INSTANCE_ID}.env" "$dir/inst${INSTANCE_ID}.json"
        else
            echo "WARN: registry entry for instance $INSTANCE_ID belongs to ${entry_cwd:-unknown}; not removed (use --all or stop it from that copy)"
        fi
    done
}

remove_all_registry_entries() {
    [ "$HD_COMMON" = true ] || return 0
    hd_registry_env_files | while IFS= read -r f; do
        [ -n "$f" ] && rm -f "$f" "${f%.env}.json"
    done
    echo "Registry entries removed"
}

# --clean: control files and shared memory of instance N (G77)
clean_instance() {
    local p name
    if [ "$INSTANCE_ID" -eq 0 ]; then p=""; else p="inst${INSTANCE_ID}_"; fi
    for name in decimator_in decimator_out delay_sync_iq delay_sync_hwc; do
        rm -f "_data_control/${p}fw_${name}" "_data_control/${p}bw_${name}"
        rm -f "/dev/shm/${p}${name}_A" "/dev/shm/${p}${name}_B"
    done
    for name in orientation_state bias_tee_state hwc_state recal_request; do
        rm -f "_data_control/${p}${name}"
    done
    echo "Instance $INSTANCE_ID control files and shared memory removed (iq_track_lock, fir_coeffs and fence_term kept)"
}

reset_fence() {
    local p
    if [ "$INSTANCE_ID" -eq 0 ]; then p=""; else p="inst${INSTANCE_ID}_"; fi
    rm -f "_data_control/${p}fence_term"
    echo "fencing state of instance $INSTANCE_ID reset; the next coordinator XACK sets it again"
}

# --this-copy: PID dirs of this copy, then the name patterns of the global
# sweep restricted to processes whose cwd is this copy.
this_copy_pids() {
    local proc here pattern pid cwd
    proc="${HEIMDALL_PROC_ROOT:-/proc}"
    here=$(pwd -P)
    for pattern in rtl_daq.out decimate.out rebuffer.out iq_server.out \
        "python.*_testing/test_data_synthesizer.py" \
        "python.*_daq_core/delay_sync.py" \
        "python.*_daq_core/hw_controller.py"; do
        for pid in $(pgrep -f "$pattern" 2>/dev/null); do
            [ "$pid" = "$$" ] && continue
            cwd=$(readlink "$proc/$pid/cwd" 2>/dev/null) || continue
            [ "$cwd" = "$here" ] && printf '%s\n' "$pid"
        done
    done
}

stop_this_copy() {
    local pids pid
    echo "Shut down the DAQ processes of this copy ($(pwd -P)) .."
    for pid_dir in _logs/inst*/pids; do
        [ -d "$pid_dir" ] || continue
        signal_pid_dir "$pid_dir"
    done
    if [ ! -d "${HEIMDALL_PROC_ROOT:-/proc}" ]; then
        echo "WARN: no proc filesystem; only PID files of this copy were used"
        pids=""
    else
        pids=$(this_copy_pids)
        for pid in $pids; do
            send_signal "$pid"
        done
    fi
    sleep "$GRACE_PERIOD"
    for pid_dir in _logs/inst*/pids; do
        [ -d "$pid_dir" ] || continue
        for pidfile in "$pid_dir"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null && pid_matches_pidfile "$pid" "$pidfile"; then
                sudo kill -KILL "$pid" 2>/dev/null
            fi
            rm -f "$pidfile"
        done
    done
    if [ -n "$pids" ]; then
        for pid in $(this_copy_pids); do
            sudo kill -KILL "$pid" 2>/dev/null
        done
    fi
    echo "DAQ processes of this copy stopped"
}

rc=0
refuse_live() {
    echo "ERROR: instance $INSTANCE_ID still has running processes; --clean/--reset-fence refused" >&2
    exit 3
}

if [ -n "$INSTANCE_ID" ] && [ "$OPT_RESET_FENCE" = true ] && [ "$OPT_CLEAN" = false ]; then
    # --reset-fence alone never stops the instance: it only resets the fence
    # of an instance that is already down.
    instance_has_live_pid && refuse_live
    reset_fence
elif [ -n "$INSTANCE_ID" ]; then
    stop_instance
    if [ "$OPT_CLEAN" = true ] && [ "$STILL_ALIVE" = true ]; then
        refuse_live
    fi
    remove_registry_entry
    if [ "$OPT_CLEAN" = true ]; then
        clean_instance
        [ "$OPT_RESET_FENCE" = true ] && reset_fence
    fi
elif [ "$OPT_THIS_COPY" = true ]; then
    stop_this_copy
else
    global_sweep
    rc=$?
    if [ "$OPT_ALL" = true ]; then
        remove_all_registry_entries
        rc=0
    elif [ "$HD_COMMON" = true ]; then
        hd_registry_prune
    fi
fi

if [ "$OPT_FEDERATION" = true ]; then
    if [ -x ./federation_stop.sh ]; then
        ./federation_stop.sh all
    else
        echo "WARN: ./federation_stop.sh not found; federation services not stopped"
    fi
fi
exit "$rc"
