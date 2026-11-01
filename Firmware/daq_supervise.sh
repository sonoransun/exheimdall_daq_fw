#!/bin/bash
#
#   DAQ chain supervisor (spec H.3)
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#
#   Usage:
#     ./daq_supervise.sh [--instance N] [--synthetic] [--interval 2]
#                        [--healthcheck auto|on|off] [--grace 60]
#                        [--stall-sec auto|SECONDS] [--allow-legacy-guard]
#                        [-- START_SCRIPT_ARGS...]
#
#   Starts the chain of this copy (./daq_start_sm.sh, or
#   ./daq_synthetic_start.sh with --synthetic; arguments after -- are passed
#   to it), then watches every stage whose PID file the start script wrote
#   (_logs/inst<N>/pids/*.pid) and, optionally, the status server. The first
#   failure stops the instance with ./daq_stop.sh N (never the host-wide
#   stop: HEIMDALL_SCOPED_STOP=1 is exported to the start script) and exits:
#     10 rtl_daq/synthesizer  11 rebuffer  12 decimate  13 delay_sync
#     14 hw_controller        15 iq_server 16 healthcheck failed
#   A failed start exits with the start script's code (1/3/4); 2 = usage or
#   misconfiguration. SIGTERM/SIGINT stop the instance and exit 0.

usage() {
    cat >&2 <<'EOF_USAGE'
Usage: ./daq_supervise.sh [--instance N] [--synthetic] [--interval 2]
                          [--healthcheck auto|on|off] [--grace 60]
                          [--stall-sec auto|SECONDS] [--allow-legacy-guard]
                          [-- START_SCRIPT_ARGS...]
EOF_USAGE
    exit 2
}

log() {
    echo "daq_supervise: $*"
}

die_usage() {
    echo "daq_supervise: $*" >&2
    exit 2
}

OPT_INSTANCE=""
SYNTHETIC=0
INTERVAL=2
HEALTHCHECK=off
GRACE=60
STALL_SEC=auto
ALLOW_LEGACY=0
start_args=()

need_uint() {  # FLAG VALUE
    case "$2" in
        ''|*[!0-9]*) die_usage "$1 needs a non-negative integer (got '$2')" ;;
    esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --instance|--interval|--healthcheck|--grace|--stall-sec)
            [ $# -ge 2 ] || die_usage "$1 needs a value"
            case "$1" in
                --instance) need_uint "$1" "$2"; OPT_INSTANCE="$2" ;;
                --interval)
                    need_uint "$1" "$2"
                    [ "$2" -ge 1 ] || die_usage "--interval must be at least 1"
                    INTERVAL="$2"
                    ;;
                --healthcheck)
                    case "$2" in auto|on|off) HEALTHCHECK="$2" ;; *) die_usage "--healthcheck must be auto, on or off" ;; esac
                    ;;
                --grace) need_uint "$1" "$2"; GRACE="$2" ;;
                --stall-sec)
                    if [ "$2" != auto ]; then need_uint "$1" "$2"; fi
                    STALL_SEC="$2"
                    ;;
            esac
            shift
            ;;
        --synthetic) SYNTHETIC=1 ;;
        --allow-legacy-guard) ALLOW_LEGACY=1 ;;
        --) shift; start_args=("$@"); break ;;
        -h|--help) usage ;;
        *) echo "daq_supervise: unknown argument '$1'" >&2; usage ;;
    esac
    shift
done

[ -f ./daq_common.sh ] || die_usage "./daq_common.sh not found (run from the Firmware directory)"
# shellcheck source=daq_common.sh
. ./daq_common.sh

if [ ! -f daq_chain_config.ini ]; then
    echo "daq_supervise: daq_chain_config.ini not found in $(pwd)" >&2
    exit 1
fi
hd_read_config daq_chain_config.ini
N="$HD_INSTANCE_ID"
if [ -n "$OPT_INSTANCE" ] && [ "$OPT_INSTANCE" != "$N" ]; then
    die_usage "--instance $OPT_INSTANCE does not match [federation] instance_id = $N of $(pwd -P)/daq_chain_config.ini"
fi

# Origin of the chain for the registry, and the scoped busy-port stop
if [ -n "${INVOCATION_ID:-}" ]; then
    export HEIMDALL_LAUNCHER=systemd-supervised
    export HEIMDALL_UNIT="${UNIT_NAME:-}"
else
    export HEIMDALL_LAUNCHER=supervised
fi
export HEIMDALL_SCOPED_STOP=1

STATUS_ON=0
if [ "$HD_EN_MONITORING" = 1 ] && [ "$HD_EN_STATUS_SERVER" = 1 ]; then
    STATUS_ON=1
fi
case "$HEALTHCHECK" in
    on)
        [ "$STATUS_ON" = 1 ] || die_usage "healthcheck requires [monitoring] en_monitoring=1 and en_status_server=1"
        ;;
    auto)
        if [ "$STATUS_ON" = 1 ]; then
            HEALTHCHECK=on
        else
            log "healthcheck skipped: status server disabled"
            HEALTHCHECK=off
        fi
        ;;
esac

STATUS_PORT=$((HD_STATUS_SERVER_PORT + N * HD_PORT_STRIDE))
STATUS_HOST="$HD_LISTEN_ADDRESS"
case "$STATUS_HOST" in
    ''|0.0.0.0|'*') STATUS_HOST=127.0.0.1 ;;
    ::) STATUS_HOST=::1 ;;
esac
STALL_CHECK=1
if [ "$HEALTHCHECK" = on ]; then
    if [ "$STALL_SEC" = auto ]; then
        if [ -n "$HD_STALL_AUTO" ]; then
            STALL_SEC="$HD_STALL_AUTO"
            log "stall threshold ${STALL_SEC} s (auto: 3 x ${HD_K_EFF} frames x ${HD_FRAME_PERIOD} s + 30 s)"
        else
            STALL_CHECK=0
            log "WARN: stall check skipped: the STATUS snapshot is never refreshed (k_eff = ${HD_K_EFF:-0}) or the frame period is unknown; only reachability is checked"
        fi
    else
        log "stall threshold ${STALL_SEC} s"
    fi
fi

# Legacy guard on a multi-instance host: a supervised restart could not be
# told apart from a second copy of the same instance.
if [ "$HD_INSTANCE_GUARD" != refuse ]; then
    other=$(hd_registry_other_live "$N" | head -n 1)
    if [ -z "$other" ]; then
        for f in _logs/inst*/pids/*.pid; do
            [ -f "$f" ] || continue
            m="${f#_logs/inst}"
            m="${m%%/*}"
            [ "$m" = "$N" ] && continue
            role="${f##*/}"
            role="${role%.pid}"
            if hd_pid_role_alive "$(head -n 1 "$f")" "$role"; then
                other="$m $(pwd -P)"
                break
            fi
        done
    fi
    if [ -n "$other" ]; then
        msg="instance $N runs with [host] instance_guard = legacy on a multi-instance host; provision it with util/provision_instance.py --instance $N --in-place"
        if [ "$ALLOW_LEGACY" = 1 ]; then
            log "WARN: $msg (instance ${other%% *} is running; continuing: --allow-legacy-guard)"
        else
            echo "$msg" >&2
            exit 2
        fi
    fi
fi

if [ "$SYNTHETIC" = 1 ]; then
    START=./daq_synthetic_start.sh
else
    START=./daq_start_sm.sh
fi
log "starting instance $N with $START (launcher $HEIMDALL_LAUNCHER)"
"$START" "${start_args[@]}"
rc=$?
if [ "$rc" -ne 0 ]; then
    log "start of instance $N failed (exit $rc)"
    exit "$rc"
fi

stop_instance() {
    ./daq_stop.sh "$N"
}

on_signal() {
    trap '' TERM INT
    log "signal received - stopping instance $N"
    stop_instance
    exit 0
}
trap on_signal TERM INT

# Stages to supervise: the PID files the start script wrote, in pipeline
# order. A downstream stage dies right after its upstream one (EOF on its
# input), so the first dead stage of a check is the most upstream one and
# names the stage that failed.
PID_DIR="_logs/inst${N}/pids"
STAGE_ORDER="rtl_daq synthesizer rebuffer decimate delay_sync hw_controller iq_server"
for f in "$PID_DIR"/*.pid; do
    [ -f "$f" ] || continue
    role="${f##*/}"
    role="${role%.pid}"
    case " $STAGE_ORDER " in
        *" $role "*) ;;
        *) log "ignoring unknown PID file $f" ;;
    esac
done
roles=()
pids=()
for role in $STAGE_ORDER; do
    f="$PID_DIR/$role.pid"
    [ -f "$f" ] || continue
    pid=$(head -n 1 "$f")
    case "$pid" in ''|*[!0-9]*) log "ignoring malformed PID file $f"; continue ;; esac
    roles+=("$role")
    pids+=("$pid")
done
if [ "${#roles[@]}" -eq 0 ]; then
    log "no stage PID files in $PID_DIR after the start - stopping instance $N"
    stop_instance
    exit 1
fi
log "supervising instance $N: $(for i in "${!roles[@]}"; do printf '%s=%s ' "${roles[$i]}" "${pids[$i]}"; done)"
if [ "$HEALTHCHECK" = on ]; then
    log "healthcheck: STATUS on $STATUS_HOST:$STATUS_PORT every ${INTERVAL} s (grace ${GRACE} s)"
fi

exit_code_of() {
    case "$1" in
        rtl_daq|synthesizer) echo 10 ;;
        rebuffer) echo 11 ;;
        decimate) echo 12 ;;
        delay_sync) echo 13 ;;
        hw_controller) echo 14 ;;
        iq_server) echo 15 ;;
    esac
}

# One STATUS query: prints "<state> <frame_count|-> <health|-> <has_age 0/1>
# <monotonic s> <reason...>"
status_probe() {
    python3 -I -c '
import json, socket, sys, time
host, port = sys.argv[1], int(sys.argv[2])
fc, health, age, reason = "-", "-", "0", "-"
state = "ok"
try:
    with socket.create_connection((host, port), timeout=2.0) as s:
        s.settimeout(2.0)
        s.sendall(b"STATUS\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    doc = json.loads(buf.decode("utf-8", "replace").strip() or "null")
    if not isinstance(doc, dict):
        raise ValueError("STATUS reply is not a JSON object")
    if isinstance(doc.get("frame_count"), int):
        fc = str(doc["frame_count"])
    if isinstance(doc.get("pipeline_health"), str):
        health = doc["pipeline_health"] or "-"
    if "snapshot_age_sec" in doc:
        age = "1"
except Exception as e:
    state, reason = "fail", "unreachable ({})".format(e.__class__.__name__ + (": " + str(e) if str(e) else ""))
print(state, fc, health, age, int(time.monotonic()), reason)
' "$STATUS_HOST" "$STATUS_PORT" 2>/dev/null || echo "fail - - 0 0 status probe failed"
}

monotonic() {
    python3 -I -c 'import time; print(int(time.monotonic()))'
}

# first_dead_stage: print the index of the most upstream dead stage and
# return 1; return 0 when every stage is alive.
first_dead_stage() {
    local i
    for i in "${!roles[@]}"; do
        if ! hd_pid_role_alive "${pids[$i]}" "${roles[$i]}"; then
            printf '%s\n' "$i"
            return 1
        fi
    done
    return 0
}

T0=$(monotonic)
fails=0
last_fc=""
last_change=""
warned_no_fc=0
while :; do
    if ! first_dead=$(first_dead_stage); then
        # Rescan: an upstream stage checked as alive above may have died
        # (taking this one with it) while the scan ran.
        first_dead=$(first_dead_stage) || true
        i="$first_dead"
        log "stage ${roles[$i]} (pid ${pids[$i]}) died"
        stop_instance
        exit "$(exit_code_of "${roles[$i]}")"
    fi
    if [ "$HEALTHCHECK" = on ]; then
        read -r state fc health has_age now reason <<< "$(status_probe)"
        if [ "$now" = 0 ]; then
            now=$(monotonic)
        fi
        failure=""
        if [ "$state" != ok ]; then
            failure="$reason"
        elif [ "$health" = stale ]; then
            failure="pipeline_health is stale"
        elif [ "$has_age" = 0 ] && [ "$STALL_CHECK" = 1 ]; then
            if [ "$fc" = - ]; then
                if [ "$warned_no_fc" = 0 ]; then
                    log "WARN: stall check skipped: STATUS has no frame_count; only reachability is checked"
                    warned_no_fc=1
                fi
            elif [ "$fc" != "$last_fc" ]; then
                last_fc="$fc"
                last_change="$now"
            elif [ $((now - last_change)) -ge "$STALL_SEC" ]; then
                failure="frame_count $fc unchanged for $((now - last_change)) s (threshold ${STALL_SEC} s)"
            fi
        fi
        if [ -n "$failure" ]; then
            if [ $((now - T0)) -lt "$GRACE" ]; then
                fails=0
            else
                fails=$((fails + 1))
                log "healthcheck failure $fails/3: $failure"
                if [ "$fails" -ge 3 ]; then
                    log "healthcheck failed 3 consecutive times - stopping instance $N"
                    stop_instance
                    exit 16
                fi
            fi
        else
            fails=0
        fi
    fi
    sleep "$INTERVAL" &
    wait $!
done
