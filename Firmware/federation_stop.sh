#!/bin/sh
#
#   Federation service stop script (spec H.2)
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#
#   Usage:
#     ./federation_stop.sh coordinator|router|all [--pid-dir DIR]
#
#   --pid-dir   default ${HEIMDALL_FED_PID_DIR:-_logs/federation/pids}
#   SIGTERM, 5 s grace, SIGKILL (only when the pid still runs the service),
#   then the PID file is removed. A missing or stale PID file is not an error.
#   Exit codes: 0 done; 2 usage error.

usage() {
    echo "Usage: ./federation_stop.sh coordinator|router|all [--pid-dir DIR]" >&2
    exit 2
}

SERVICE=""
PID_DIR="${HEIMDALL_FED_PID_DIR:-_logs/federation/pids}"
GRACE=5

while [ $# -gt 0 ]; do
    case "$1" in
        coordinator|router|all)
            [ -z "$SERVICE" ] || usage
            SERVICE="$1"
            ;;
        --pid-dir)
            [ $# -ge 2 ] && [ -n "$2" ] || usage
            PID_DIR="$2"
            shift
            ;;
        -h|--help) usage ;;
        *) echo "federation_stop.sh: unknown argument '$1'" >&2; usage ;;
    esac
    shift
done
[ -n "$SERVICE" ] || usage

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source-path=SCRIPTDIR
# shellcheck source=daq_common.sh
. "$HERE/daq_common.sh"

stop_service() {
    local name role pidfile pid waited
    name="$1"
    case "$name" in
        coordinator) role=federation_coordinator ;;
        router) role=federation_iq_router ;;
    esac
    pidfile="$PID_DIR/$name.pid"
    if [ ! -f "$pidfile" ]; then
        echo "federation $name: no PID file ($pidfile), not running"
        return 0
    fi
    pid=$(head -n 1 "$pidfile" 2>/dev/null)
    if ! hd_pid_role_alive "$pid" "$role"; then
        echo "federation $name: stale PID file $pidfile removed"
        rm -f "$pidfile"
        return 0
    fi
    kill -TERM "$pid" 2>/dev/null
    waited=0
    while hd_pid_role_alive "$pid" "$role" && [ "$waited" -lt "$GRACE" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if hd_pid_role_alive "$pid" "$role"; then
        kill -KILL "$pid" 2>/dev/null
        echo "federation $name (pid $pid) killed after ${GRACE} s"
    else
        echo "federation $name (pid $pid) stopped"
    fi
    rm -f "$pidfile"
}

if [ "$SERVICE" = all ]; then
    stop_service router
    stop_service coordinator
else
    stop_service "$SERVICE"
fi
exit 0
