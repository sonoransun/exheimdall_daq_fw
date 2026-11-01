#!/bin/sh
#
#   Federation service launcher (spec H.2)
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#
#   Usage:
#     ./federation_start.sh coordinator|router|all [--config PATH] [--foreground]
#                           [--state-dir DIR] [--log-dir DIR] [--pid-dir DIR]
#
#   coordinator   _daq_core/federation_coordinator.py (port 6000)
#   router        _daq_core/federation_iq_router.py   (ports 7000/7001)
#   --config      default /etc/heimdall/federation.ini when present, else
#                 ./daq_chain_config.ini
#   --log-dir     default ${HEIMDALL_FED_LOG_DIR:-_logs/federation}
#                 (<dir>/coordinator.log, <dir>/router.log)
#   --pid-dir     default ${HEIMDALL_FED_PID_DIR:-_logs/federation/pids}
#                 (<dir>/coordinator.pid, <dir>/router.pid)
#   --state-dir   default ${HEIMDALL_FED_STATE_DIR:-} (empty: not passed);
#                 coordinator only
#   --foreground  exec the service in this process (systemd); one service only
#
#   No sudo, no RT priority, no CPU pinning (unprivileged ports).
#   Exit codes: 0 started; 1 the service exited immediately; 2 usage error;
#   3 the service is already running (its PID file is live).

usage() {
    cat >&2 <<'EOF_USAGE'
Usage: ./federation_start.sh coordinator|router|all [--config PATH] [--foreground]
                             [--state-dir DIR] [--log-dir DIR] [--pid-dir DIR]
EOF_USAGE
    exit 2
}

SERVICE=""
CONFIG=""
FOREGROUND=false
STATE_DIR="${HEIMDALL_FED_STATE_DIR:-}"
LOG_DIR="${HEIMDALL_FED_LOG_DIR:-_logs/federation}"
PID_DIR="${HEIMDALL_FED_PID_DIR:-_logs/federation/pids}"

while [ $# -gt 0 ]; do
    case "$1" in
        coordinator|router|all)
            [ -z "$SERVICE" ] || usage
            SERVICE="$1"
            ;;
        --config|--state-dir|--log-dir|--pid-dir)
            [ $# -ge 2 ] && [ -n "$2" ] || { echo "federation_start.sh: $1 needs a value" >&2; usage; }
            case "$1" in
                --config) CONFIG="$2" ;;
                --state-dir) STATE_DIR="$2" ;;
                --log-dir) LOG_DIR="$2" ;;
                --pid-dir) PID_DIR="$2" ;;
            esac
            shift
            ;;
        --foreground) FOREGROUND=true ;;
        -h|--help) usage ;;
        *) echo "federation_start.sh: unknown argument '$1'" >&2; usage ;;
    esac
    shift
done
[ -n "$SERVICE" ] || usage
if [ "$FOREGROUND" = true ] && [ "$SERVICE" = all ]; then
    echo "federation_start.sh: --foreground starts one service (coordinator or router)" >&2
    exit 2
fi
if [ -z "$CONFIG" ]; then
    if [ -f /etc/heimdall/federation.ini ]; then
        CONFIG=/etc/heimdall/federation.ini
    else
        CONFIG=./daq_chain_config.ini
    fi
fi

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source-path=SCRIPTDIR
# shellcheck source=daq_common.sh
. "$HERE/daq_common.sh"

script_of() {
    case "$1" in
        coordinator) echo "$HERE/_daq_core/federation_coordinator.py" ;;
        router) echo "$HERE/_daq_core/federation_iq_router.py" ;;
    esac
}

role_of() {
    case "$1" in
        coordinator) echo federation_coordinator ;;
        router) echo federation_iq_router ;;
    esac
}

# start_service NAME: 0 started, 1 exited immediately, 3 already running
start_service() {
    local name script pidfile logfile pid
    name="$1"
    script=$(script_of "$name")
    pidfile="$PID_DIR/$name.pid"
    logfile="$LOG_DIR/$name.log"
    if [ -f "$pidfile" ]; then
        pid=$(head -n 1 "$pidfile" 2>/dev/null)
        if hd_pid_role_alive "$pid" "$(role_of "$name")"; then
            echo "federation $name is already running (pid $pid, $pidfile)" >&2
            return 3
        fi
        rm -f "$pidfile"
    fi
    if ! mkdir -p "$LOG_DIR" "$PID_DIR"; then
        echo "federation_start.sh: cannot create $LOG_DIR or $PID_DIR" >&2
        return 1
    fi
    set -- python3 "$script" --config "$CONFIG" --log-file "$logfile" --pid-file "$pidfile"
    if [ "$name" = coordinator ] && [ -n "$STATE_DIR" ]; then
        set -- "$@" --state-dir "$STATE_DIR"
    fi
    if [ "$FOREGROUND" = true ]; then
        exec "$@"
    fi
    nohup "$@" < /dev/null >> "$logfile" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$pidfile"
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "federation $name exited immediately - see $logfile" >&2
        tail -n 5 "$logfile" >&2 2>/dev/null
        rm -f "$pidfile"
        return 1
    fi
    echo "federation $name started (pid $pid, log $logfile)"
    return 0
}

if [ "$SERVICE" = all ]; then
    start_service coordinator
    rc1=$?
    start_service router
    rc2=$?
    [ "$rc1" -ne 0 ] && exit "$rc1"
    exit "$rc2"
fi
start_service "$SERVICE"
exit $?
