# shellcheck shell=sh
# HD_* variables are set here and consumed by the sourcing scripts:
# shellcheck disable=SC2034
#
#   Common helpers of the DAQ chain scripts (spec B.3.2)
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#
#   Sourced (never executed) by daq_start_sm.sh, daq_synthetic_start.sh,
#   daq_stop.sh, daq_supervise.sh, federation_start.sh and federation_stop.sh,
#   always from the Firmware directory (every path below is cwd-relative, like
#   the rest of the chain). POSIX sh; the only non-POSIX construct is `local`
#   (dash, busybox sh and bash support it). Sourcing defines functions only:
#   no command runs, no file is touched and no variable is set or exported.
#
#   With [host] instance_registry = 0 (the default) the start scripts call
#   none of the registry/lock/mutex helpers, so the chain's fd table,
#   environment and files are exactly those of the scripts before this file
#   existed. Registry files ($RUN/inst<N>.env|.json) are only ever read
#   through the .env sidecar; this file never parses JSON.

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# hd_read_config [INI]: set HD_* shell variables from the instance INI with
# one ConfigParser pass. The fallbacks equal the separate snippets the start
# scripts used before (instance_id 0, port_stride 100, out_data_iface_type
# eth, fpga/gpu enable 0); a missing or unparsable INI leaves every fallback.
hd_read_config() {
    local cfg out
    cfg="${1:-daq_chain_config.ini}"
    HD_INSTANCE_ID=0
    HD_PORT_STRIDE=100
    HD_OUT_IFACE=eth
    HD_FPGA_ENABLE=0
    HD_FPGA_BITSTREAM=
    HD_GPU_ENABLE=0
    HD_EN_MONITORING=0
    HD_EN_STATUS_SERVER=0
    HD_EN_ZMQ_PUB=0
    HD_STATUS_SERVER_PORT=5002
    HD_ZMQ_PUB_PORT=5003
    HD_LISTEN_ADDRESS=0.0.0.0
    HD_STALL_AUTO=
    HD_FRAME_PERIOD=
    HD_K_EFF=
    HD_CPU_CORES=
    HD_DELAY_SYNC_CORES=
    HD_RT_PRIORITY_OFFSET=0
    HD_ALLOW_CORE_SHARING=warn
    HD_HOST_TUNING=always
    HD_PREFLIGHT=off
    HD_INSTANCE_REGISTRY=0
    HD_INSTANCE_GUARD=legacy
    HD_USB_IRQ_CORE=
    HD_USB_CONTROLLER=
    HD_SERIALS=
    HD_NODE_ID=
    HD_DB_DIR=
    # The program text uses double quotes only (it is a single-quoted shell
    # string); -c keeps it out of a here-document inside $(...), which old
    # bash releases mis-parse.
    code='
import math
import os
import re
import shlex
import socket
import sys
from configparser import ConfigParser

path, cwd = sys.argv[1], sys.argv[2]
c = ConfigParser()
try:
    c.read(path)
except Exception:
    c = ConfigParser()


def g(sec, key, fb=""):
    try:
        return c.get(sec, key, fallback=fb)
    except Exception:
        return fb


def digits(v, fb):
    return v if re.fullmatch(r"[0-9]+", v or "") else fb


def choice(v, allowed, fb):
    v = (v or "").strip()
    return v if v in allowed else fb


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def guarded(fn, fb=""):
    # Optional/derived values: a bad one falls back alone and never empties
    # the whole output (instance id, ports, ... stay intact).
    try:
        return fn()
    except Exception:
        return fb


out = {}
iid = digits(g("federation", "instance_id", "0"), "0")
out["INSTANCE_ID"] = iid
out["PORT_STRIDE"] = digits(g("federation", "port_stride", "100"), "100")
out["OUT_IFACE"] = g("data_interface", "out_data_iface_type", "eth")
out["FPGA_ENABLE"] = g("fpga", "enable", "0")
out["FPGA_BITSTREAM"] = g("fpga", "bitstream", "")
out["GPU_ENABLE"] = g("gpu", "enable", "0")
out["EN_MONITORING"] = g("monitoring", "en_monitoring", "0").strip()
out["EN_STATUS_SERVER"] = g("monitoring", "en_status_server", "0").strip()
out["EN_ZMQ_PUB"] = g("monitoring", "en_zmq_pub", "0").strip()
out["STATUS_SERVER_PORT"] = digits(g("monitoring", "status_server_port", "5002").strip(), "5002")
out["ZMQ_PUB_PORT"] = digits(g("monitoring", "zmq_pub_port", "5003").strip(), "5003")
out["LISTEN_ADDRESS"] = g("daq", "listen_address", "0.0.0.0").strip() or "0.0.0.0"

# Supervisor stall threshold (H.3): 3 * k_eff * T_fp + 30 s, with
# k_eff = status_update_interval if > 0 else heartbeat_interval.
sui = num(g("monitoring", "status_update_interval", "0")) or 0
hbi = num(g("monitoring", "heartbeat_interval", "100"))
k_eff = sui if sui > 0 else (hbi if hbi is not None else 100)
cpi = num(g("pre_processing", "cpi_size", ""))
dec = num(g("pre_processing", "decimation_ratio", ""))
fs = num(g("daq", "sample_rate", ""))
t_fp = guarded(lambda: cpi * dec / fs if cpi and dec and fs else None, None)
if t_fp is not None and not (math.isfinite(t_fp) and t_fp > 0):
    t_fp = None
out["K_EFF"] = guarded(lambda: "%g" % k_eff)
out["FRAME_PERIOD"] = "%.6f" % t_fp if t_fp else ""
out["STALL_AUTO"] = guarded(lambda: str(int(round(3 * k_eff * t_fp + 30))) if (t_fp and k_eff > 0) else "")

cores = g("host", "cpu_cores", "").replace(" ", "")
items = cores.split(",") if cores else []
ok = len(items) == 4 and all(re.fullmatch(r"[0-9]+", x) for x in items) and len(set(items)) == 4
out["CPU_CORES"] = cores if ok else ""
ds = g("host", "delay_sync_cores", "").replace(" ", "")
out["DELAY_SYNC_CORES"] = ds if re.fullmatch(r"[0-9]+(,[0-9]+)*", ds) else ""
off = digits(g("host", "rt_priority_offset", "0").strip(), "0")
out["RT_PRIORITY_OFFSET"] = off if int(off) <= 81 else "0"
out["ALLOW_CORE_SHARING"] = choice(g("host", "allow_core_sharing", "warn"), ("warn", "refuse", "allow"), "warn")
out["HOST_TUNING"] = choice(g("host", "host_tuning", "always"), ("always", "auto", "1", "0"), "always")
out["PREFLIGHT"] = choice(g("host", "preflight", "off"), ("off", "warn", "strict"), "off")
out["INSTANCE_REGISTRY"] = "1" if g("host", "instance_registry", "0").strip() == "1" else "0"
out["INSTANCE_GUARD"] = choice(g("host", "instance_guard", "legacy"), ("legacy", "refuse"), "legacy")
out["USB_IRQ_CORE"] = digits(g("host", "usb_irq_core", "").strip(), "")
ctrl = g("host", "usb_controller", "").strip()
out["USB_CONTROLLER"] = "" if "\n" in ctrl else ctrl

# Resolved receiver serials (A.1): device_serials wins over serial_base.
def resolve_serials():
    serials = [s.strip() for s in g("hw", "device_serials", "").split(",") if s.strip()]
    if not serials:
        base = int(digits(g("hw", "serial_base", "1000").strip(), "1000"))
        n_ch = int(digits(g("hw", "num_ch", "0").strip(), "0"))
        serials = [str(base + i) for i in range(min(n_ch, 1024))]
    return ",".join(serials)


out["SERIALS"] = guarded(resolve_serials)

node_id = g("federation", "node_id", "").strip()
if not node_id:
    try:
        sys.path.insert(0, os.path.join(cwd, "_daq_core"))
        from federation_common import default_node_id
        node_id = default_node_id(int(iid))
    except Exception:
        host = socket.gethostname().split(".")[0].lower() or "host"
        node_id = "{}:{}".format(host, int(iid))
out["NODE_ID"] = node_id

def resolve_db_dir():
    if g("database", "en_db", "0").strip() != "1":
        return ""
    db = g("database", "db_dir", "_db").strip() or "_db"
    if g("database", "instance_subdir", "0").strip() == "1" and int(iid) > 0:
        db = os.path.join(db, "inst{}".format(int(iid)))
    return os.path.normpath(os.path.join(cwd, db))


out["DB_DIR"] = guarded(resolve_db_dir)

for k, v in out.items():
    print("HD_{}={}".format(k, shlex.quote(v)))
'
    out=$(python3 -I -c "$code" "$cfg" "$(pwd -P)" 2>/dev/null) || out=""
    [ -n "$out" ] && eval "$out"
    return 0
}

# ---------------------------------------------------------------------------
# Processes and the host registry
# ---------------------------------------------------------------------------

# hd_pid_role_alive PID ROLE: 0 when PID runs and its command line
# (ps -o args=) contains ROLE - the stale-PID-file test of daq_stop.sh. A
# zombie (exited, not yet reaped by a parent such as a container's PID 1
# that never waits) does not run.
hd_pid_role_alive() {
    local info
    [ -n "${1:-}" ] && [ -n "${2:-}" ] || return 1
    case "$1" in *[!0-9]*) return 1 ;; esac
    kill -0 "$1" 2>/dev/null || ps -p "$1" >/dev/null 2>&1 || return 1
    info=$(ps -o stat= -o args= -p "$1" 2>/dev/null) || return 1
    info="${info#"${info%%[! ]*}"}"
    case "$info" in
        Z*) return 1 ;;
        *"$2"*) return 0 ;;
    esac
    return 1
}

# hd_env_get FILE KEY: print the value of KEY in a KEY=VALUE file. The file
# is read, never sourced.
hd_env_get() {
    local k v
    [ -f "$1" ] || return 1
    while IFS='=' read -r k v; do
        if [ "$k" = "$2" ]; then
            printf '%s\n' "$v"
            return 0
        fi
    done < "$1"
    return 1
}

# hd_entry_live ENVFILE: 0 when any role:pid of its PIDS line is alive.
hd_entry_live() {
    local pids item
    pids=$(hd_env_get "$1" PIDS) || return 1
    for item in $(printf '%s\n' "$pids" | tr ',' ' '); do
        if hd_pid_role_alive "${item#*:}" "${item%%:*}"; then
            return 0
        fi
    done
    return 1
}

# hd_run_dir: print the first usable registry directory: $HEIMDALL_RUN_DIR
# (default /run/heimdall), then /tmp/heimdall-run-<uid>. A directory is
# usable when it exists (created 0755 when missing) and is writable; the
# /tmp fallback must also be a real directory owned by this uid. Returns 1
# (and prints nothing on stdout) when neither is usable.
hd_run_dir() {
    local primary fallback uid owner
    primary="${HEIMDALL_RUN_DIR:-/run/heimdall}"
    uid=$(id -u)
    fallback="/tmp/heimdall-run-$uid"
    [ -d "$primary" ] || { mkdir -p "$primary" && chmod 0755 "$primary"; } 2>/dev/null
    if [ -d "$primary" ] && [ -w "$primary" ]; then
        printf '%s\n' "$primary"
        return 0
    fi
    if [ ! -L "$fallback" ] && [ ! -d "$fallback" ]; then
        { mkdir -p "$fallback" && chmod 0755 "$fallback"; } 2>/dev/null
    fi
    owner=$(ls -ldn "$fallback" 2>/dev/null | awk '{print $3}')
    if [ ! -L "$fallback" ] && [ -d "$fallback" ] && [ -w "$fallback" ] && [ "$owner" = "$uid" ]; then
        echo "WARN: run directory $primary is not usable; using $fallback" >&2
        printf '%s\n' "$fallback"
        return 0
    fi
    echo "WARN: no usable run directory ($primary, $fallback); registry, instance lock and startup mutex disabled for this start" >&2
    return 1
}

# hd_registry_dir_trusted DIR: 0 when DIR may be read as a registry
# directory. $HD_RUN, $HEIMDALL_RUN_DIR and /run/heimdall only need to exist;
# any other directory (the /tmp/heimdall-run-<uid> fallback, a run_dir hint)
# must be a real directory owned by this uid, the test hd_run_dir applies,
# so a directory another local user planted in /tmp is never read or pruned.
hd_registry_dir_trusted() {
    local owner
    [ -d "$1" ] || return 1
    case "$1" in
        "${HD_RUN:-/run/heimdall}" | "${HEIMDALL_RUN_DIR:-/run/heimdall}" | /run/heimdall) return 0 ;;
    esac
    [ ! -L "$1" ] || return 1
    owner=$(ls -ldn "$1" 2>/dev/null | awk '{print $3}')
    [ "$owner" = "$(id -u)" ]
}

# hd_registry_dirs: existing registry directories, one per line: $HD_RUN,
# $HEIMDALL_RUN_DIR (default /run/heimdall), /tmp/heimdall-run-<uid> and the
# run_dir hints this copy's chains recorded (the last two only when
# hd_registry_dir_trusted). Creates nothing.
hd_registry_dirs() {
    local f
    {
        [ -n "${HD_RUN:-}" ] && printf '%s\n' "$HD_RUN"
        printf '%s\n' "${HEIMDALL_RUN_DIR:-/run/heimdall}" "/tmp/heimdall-run-$(id -u)"
        for f in _logs/inst*/pids/run_dir; do
            [ -f "$f" ] && head -n 1 "$f"
        done
    } | awk 'NF && !seen[$0]++' | while IFS= read -r f; do
        hd_registry_dir_trusted "$f" && printf '%s\n' "$f"
    done
}

# hd_registry_env_files: every inst<N>.env of hd_registry_dirs, one per line.
hd_registry_env_files() {
    local d f
    hd_registry_dirs | while IFS= read -r d; do
        for f in "$d"/inst*.env; do
            case "$f" in
                */inst[0-9]*.env) [ -f "$f" ] && printf '%s\n' "$f" ;;
            esac
        done
    done
}

# hd_registry_live N: 0 when a live inst<N>.env exists in $HD_RUN (or, when
# HD_RUN is empty, in any registry directory); prints its CWD.
hd_registry_live() {
    local f cwd
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        case "$f" in */inst"$1".env) ;; *) continue ;; esac
        if [ -n "${HD_RUN:-}" ] && [ "${f%/*}" != "$HD_RUN" ]; then
            continue
        fi
        if hd_entry_live "$f"; then
            cwd=$(hd_env_get "$f" CWD) || cwd=unknown
            printf '%s\n' "${cwd:-unknown}"
            return 0
        fi
    done <<HD_EOF
$(hd_registry_env_files)
HD_EOF
    return 1
}

# hd_registry_other_live N: print "<id> <cwd>" for every live entry whose
# instance id differs from N.
hd_registry_other_live() {
    local f iid cwd
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        iid=$(hd_env_get "$f" INSTANCE_ID) || continue
        [ "$iid" = "$1" ] && continue
        if hd_entry_live "$f"; then
            cwd=$(hd_env_get "$f" CWD) || cwd=unknown
            printf '%s %s\n' "$iid" "${cwd:-unknown}"
        fi
    done <<HD_EOF
$(hd_registry_env_files)
HD_EOF
}

# hd_registry_prune: remove inst<N>.json/.env whose pids are all dead.
hd_registry_prune() {
    local f
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        if ! hd_entry_live "$f"; then
            if rm -f "$f" "${f%.env}.json" 2>/dev/null && [ ! -e "$f" ] && [ ! -e "${f%.env}.json" ]; then
                echo "Removed stale registry entry ${f%.env}"
            else
                echo "WARN: cannot remove stale registry entry ${f%.env}"
            fi
        fi
    done <<HD_EOF
$(hd_registry_env_files)
HD_EOF
}

# ---------------------------------------------------------------------------
# Startup mutex (fd 8) and instance lock (fd 9) - only with
# [host] instance_registry = 1 and a writable $HD_RUN (B.3.3, B.3.4)
# ---------------------------------------------------------------------------

# hd_open_fd 8|9 FILE: open FILE for writing on fd 8 or 9 of this shell;
# 1 (nothing opened) when FILE cannot be opened. The probe with the regular
# builtin `true` keeps a failing redirection from terminating a POSIX shell
# (a failed redirection on the special builtin `exec` does); `command exec`
# is avoided because bash 3.2 spins on exit after it.
hd_open_fd() {
    { true >> "$2"; } 2>/dev/null || return 1
    case "$1" in
        8) { exec 8>"$2"; } 2>/dev/null ;;
        9) { exec 9>"$2"; } 2>/dev/null ;;
        *) return 1 ;;
    esac
}

hd_startup_mutex() {
    [ -n "${HD_RUN:-}" ] && [ -w "$HD_RUN" ] || return 0
    if ! command -v flock >/dev/null 2>&1; then
        echo "WARN: flock not found; startup mutex skipped"
        return 0
    fi
    if ! hd_open_fd 8 "$HD_RUN/inst$1.start"; then
        echo "WARN: cannot open startup mutex; continuing without it"
        return 0
    fi
    if [ "$HD_INSTANCE_GUARD" = refuse ]; then
        flock -n 8 || { echo "ERROR: another start of instance $1 is in progress"; exit 3; }
    else
        flock -w 60 8 || echo "WARN: startup mutex for instance $1 not acquired after 60 s; continuing"
    fi
}

# hd_instance_lock N [hw|synthetic]: open $HD_RUN/inst<N>.lock on fd 9
# (inherited by every stage) and take it. Conflicts follow B.3.4;
# HD_TAKEOVER=1 is --takeover. A synthetic start under the legacy guard never
# stops the running chain (its port check is advisory, L9), so it continues
# unlocked at once instead of waiting for the lock.
hd_instance_lock() {
    local n mode here other
    n="$1"
    mode="${2:-hw}"
    HD_LOCKED=0
    HD_LOCK_PENDING=0
    [ -n "${HD_RUN:-}" ] && [ -w "$HD_RUN" ] || return 0
    if ! command -v flock >/dev/null 2>&1; then
        echo "WARN: flock not found; instance lock skipped"
        return 0
    fi
    if ! hd_open_fd 9 "$HD_RUN/inst$n.lock"; then
        echo "WARN: cannot open instance lock $HD_RUN/inst$n.lock; continuing without it"
        return 0
    fi
    export HEIMDALL_INSTANCE_LOCK="$HD_RUN/inst$n.lock"
    export HEIMDALL_INSTANCE_GUARD="$HD_INSTANCE_GUARD"
    if flock -n 9; then
        HD_LOCKED=1
        return 0
    fi
    here=$(pwd -P)
    other=$(hd_registry_live "$n") || other=unknown
    if [ "$HD_INSTANCE_GUARD" = refuse ]; then
        if [ "$other" = "$here" ]; then
            echo "instance $n is running from this copy - restarting"
            ./daq_stop.sh "$n"
        elif [ "${HD_TAKEOVER:-0}" = 1 ] && [ "$other" != unknown ]; then
            echo "instance $n is running from $other - taking over (--takeover)"
            (cd "$other" && ./daq_stop.sh "$n")
        else
            echo "ERROR: instance $n is already running from $other (use --takeover to stop it)"
            exit 3
        fi
        if ! flock -w 10 9; then
            echo "ERROR: instance lock of instance $n is still held after 10 s - DAQ chain not started!"
            exit 3
        fi
        HD_LOCKED=1
    elif [ "$mode" = synthetic ]; then
        if [ "$other" = "$here" ]; then
            echo "WARN: instance $n is already running from this copy; it is not stopped (today's synthetic start) - continuing without the instance lock"
        else
            echo "WARN: instance $n is also running from $other; it is not stopped (today's synthetic start) - continuing without the instance lock"
        fi
    else
        if [ "$other" = "$here" ]; then
            echo "WARN: instance $n is already running from this copy; the port check will stop it (today's behaviour)"
        else
            echo "WARN: instance $n is also running from $other; today's behaviour applies: the port check stops all DAQ processes on this host"
        fi
        HD_LOCK_PENDING=1
    fi
}

# hd_lock_deferred: after the port check stopped the old chain, wait for the
# instance lock a conflicting legacy-guard start left pending.
hd_lock_deferred() {
    [ "${HD_LOCK_PENDING:-0}" = 1 ] || return 0
    HD_LOCK_PENDING=0
    if flock -w 15 9; then
        HD_LOCKED=1
    else
        echo "WARN: instance lock still held; continuing unlocked"
    fi
}

# hd_registry_begin N [hw|synthetic]: step 4 of B.3.5 - only call with
# instance_registry=1. Stale entries (every stage dead) are pruned first.
hd_registry_begin() {
    HD_RUN=$(hd_run_dir) || HD_RUN=""
    if [ -n "$HD_RUN" ]; then
        export HEIMDALL_RUN_DIR="$HD_RUN"
        hd_registry_prune
    fi
    export HEIMDALL_LAUNCHER="${HEIMDALL_LAUNCHER:-manual}"
    hd_startup_mutex "$1"
    hd_instance_lock "$1" "${2:-hw}"
}

# ---------------------------------------------------------------------------
# Cores, ports, host tuning, preflight
# ---------------------------------------------------------------------------

# hd_core_guard C0 C1 C2 C3: today's core-sharing warning (cores derived from
# the instance id, i.e. [host] cpu_cores empty) plus, with
# instance_registry = 1, the overlap check against other live instances.
hd_core_guard() {
    local n mine iid cwd theirs c overlap f
    n="$HD_INSTANCE_ID"
    if [ -z "$HD_CPU_CORES" ] && [ "$n" -gt 0 ] && [ "$NUM_CORES" -lt $(( (n + 1) * 4 )) ]; then
        echo "WARN: ${NUM_CORES} cores for $((n + 1)) instances - RT processes of multiple instances will share cores (${1},${2},${3},${4})"
    fi
    [ "$HD_INSTANCE_REGISTRY" = 1 ] || return 0
    [ "$HD_ALLOW_CORE_SHARING" = allow ] && return 0
    mine=" $1 $2 $3 $4 "
    while read -r iid cwd; do
        [ -n "$iid" ] || continue
        theirs=""
        while IFS= read -r f; do
            case "$f" in */inst"$iid".env) theirs=$(hd_env_get "$f" CORES) && break ;; esac
        done <<HD_EOF
$(hd_registry_env_files)
HD_EOF
        overlap=""
        for c in $(printf '%s\n' "$theirs" | tr ',' ' '); do
            case "$mine" in *" $c "*) overlap="${overlap:+$overlap,}$c" ;; esac
        done
        [ -n "$overlap" ] || continue
        if [ "$HD_ALLOW_CORE_SHARING" = refuse ]; then
            echo "ERROR: cores $overlap of instance $n are used by instance $iid running from $cwd ([host] allow_core_sharing = refuse) - DAQ chain not started!"
            exit 3
        fi
        echo "WARN: cores $overlap of instance $n are also used by instance $iid running from $cwd"
    done <<HD_EOF
$(hd_registry_other_live "$n")
HD_EOF
    return 0
}

# hd_ss_listen [-p] PORT: the listener lines `ss -ltn[p] "sport = :PORT"`
# reports, without its header (no -H/--no-header, which iproute2 < 4.13 lacks).
# Non-zero when ss is missing or fails.
hd_ss_listen() {
    local fl out
    fl=-ltn
    if [ "$1" = -p ]; then
        fl=-ltnp
        shift
    fi
    command -v ss >/dev/null 2>&1 || return 1
    out=$(ss "$fl" "sport = :$1" 2>/dev/null) || return 1
    printf '%s\n' "$out" | awk 'NF && $1 != "State"'
}

# hd_lsof_listen PORT: lsof's listener lines of PORT without the header.
# Non-zero when lsof is missing; lsof's own status is not trusted (it is 1
# both for "nothing found" and for errors), so an empty answer means free.
hd_lsof_listen() {
    command -v lsof >/dev/null 2>&1 || return 1
    lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | tail -n +2
    return 0
}

# hd_port_in_use PORT: 0 = a TCP listener holds PORT, 1 = free, 2 = unknown
# (neither ss nor lsof gave an answer). Only listening sockets count (L5).
hd_port_in_use() {
    local rc out
    rc=2
    if out=$(hd_ss_listen "$1"); then
        rc=1
        [ -n "$out" ] && return 0
    fi
    if out=$(hd_lsof_listen "$1"); then
        rc=1
        [ -n "$out" ] && return 0
    fi
    return "$rc"
}

# hd_port_holder PORT: "name",pid=... of the listener (best effort).
hd_port_holder() {
    local h
    h=$(hd_ss_listen -p "$1" | sed -n 's/.*users:((\(.*\))).*/\1/p' | head -n 1)
    if [ -z "$h" ]; then
        h=$(hd_lsof_listen "$1" | awk 'NR == 1 {print $1 " (pid " $2 ")"}')
    fi
    printf '%s\n' "${h:-unknown}"
}

# hd_port_listeners PORT: the listener lines of PORT for the final error.
hd_port_listeners() {
    local out
    if out=$(hd_ss_listen -p "$1") && [ -n "$out" ]; then
        printf '%s\n' "$out"
    else
        hd_lsof_listen "$1"
    fi
}

# hd_port_check hw|synthetic (B.3.2 / L5). The busy loop: 10 attempts, the
# no-argument ./daq_stop.sh (or ./daq_stop.sh N with the refuse guard or
# HEIMDALL_SCOPED_STOP=1) between rounds, exit 1 when still busy.
hd_port_check() {
    local mode n off iq ctl st adv p busy attempts
    mode="$1"
    n="$HD_INSTANCE_ID"
    off=$((n * HD_PORT_STRIDE))
    iq=$((5000 + off))
    ctl=$((5001 + off))
    st=$((HD_STATUS_SERVER_PORT + off))
    adv=$((1130 + off))
    if [ "$HD_EN_MONITORING" = 1 ] && [ "$HD_EN_ZMQ_PUB" = 1 ]; then
        adv="$adv $((HD_ZMQ_PUB_PORT + off))"
    fi
    p=0
    hd_port_in_use "$iq" || p=$?
    if [ "$p" = 2 ]; then
        echo "WARN: Neither lsof nor ss available - skipping port availability check"
        return 0
    fi
    if [ "$mode" = synthetic ] && [ "$HD_INSTANCE_GUARD" != refuse ]; then
        for p in "$iq" "$ctl" "$st" $adv; do
            if hd_port_in_use "$p"; then
                echo "WARN: port $p is in use by $(hd_port_holder "$p")"
            fi
        done
        return 0
    fi
    for p in $adv; do
        if hd_port_in_use "$p"; then
            echo "WARN: port $p is in use by $(hd_port_holder "$p")"
        fi
    done
    attempts=0
    while true; do
        busy=""
        for p in "$iq" "$ctl" "$st"; do
            if hd_port_in_use "$p"; then
                busy="$busy $p"
            fi
        done
        [ -z "$busy" ] && break
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 10 ]; then
            printf '\033[91mERROR: Ports (%s, %s & %s) still busy after %s attempts - DAQ chain not started!\033[39m\n' \
                "$iq" "$ctl" "$st" "$attempts"
            for p in $busy; do
                hd_port_listeners "$p"
            done
            exit 1
        fi
        echo "WARN:Ports used by the DAQ chain instance ${n} are not free! (${iq}, ${ctl} & ${st})"
        if [ "$HD_INSTANCE_GUARD" = refuse ] || [ "${HEIMDALL_SCOPED_STOP:-}" = 1 ]; then
            ./daq_stop.sh "$n"
        else
            ./daq_stop.sh
        fi
        sleep 1
    done
}

# hd_host_tuning_early: position of the sysctl + irq_tuning commands (B.7).
# HD_NO_HOST_TUNING=1 is --no-host-tuning.
hd_host_tuning_early() {
    local reason other
    if [ "${HD_NO_HOST_TUNING:-0}" = 1 ] || [ "$HD_HOST_TUNING" = 0 ]; then
        echo "host tuning skipped (disabled)"
        return 0
    fi
    set --
    if [ -n "$HD_USB_IRQ_CORE" ]; then
        set -- "$@" --usb-core "$HD_USB_IRQ_CORE"
    fi
    if [ -n "$HD_USB_CONTROLLER" ]; then
        set -- "$@" --controller "$HD_USB_CONTROLLER"
    fi
    if [ "$HD_HOST_TUNING" = always ]; then
        sudo sysctl -w kernel.sched_rt_runtime_us=-1

        # Apply IRQ tuning for optimal performance
        if [ -x "../util/irq_tuning.sh" ]; then
            sudo ../util/irq_tuning.sh "$@"
        fi
        return 0
    fi
    if [ "$HD_HOST_TUNING" = auto ]; then
        reason=""
        if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet heimdall-host-tuning.service; then
            reason="heimdall-host-tuning.service is active"
        else
            other=$(hd_registry_other_live "$HD_INSTANCE_ID" | head -n 1)
            if [ -n "$other" ]; then
                reason="instance ${other%% *} is running"
            fi
        fi
        if [ -n "$reason" ]; then
            echo "host tuning skipped ($reason)"
            return 0
        fi
    fi
    sudo "$(dirname "$0")/../util/host_tuning.sh" --apply "$@"
}

# hd_host_tuning_late: position of the usbfs + drop_caches commands (B.7).
hd_host_tuning_late() {
    [ "${HD_NO_HOST_TUNING:-0}" = 1 ] && return 0
    [ "$HD_HOST_TUNING" = always ] || return 0
    # The Kernel limits the maximum size of all buffers that libusb can allocate to 16MB by default.
    # In order to disable the limit, you have to run the following command as root:
    if [ -e /sys/module/usbcore/parameters/usbfs_memory_mb ]; then
        sudo sh -c "echo 0 > /sys/module/usbcore/parameters/usbfs_memory_mb"
    else
        echo "WARN: /sys/module/usbcore/parameters/usbfs_memory_mb not present, skipping"
    fi

    # This command clear the caches
    echo '3' | sudo tee /proc/sys/vm/drop_caches > /dev/null
}

# hd_preflight: [host] preflight = warn|strict runs daq_preflight.py (I.2);
# strict + exit 2 stops the start with exit 4.
hd_preflight() {
    local rc
    case "$HD_PREFLIGHT" in
        warn|strict) ;;
        *) return 0 ;;
    esac
    if [ ! -f daq_preflight.py ]; then
        echo "WARN: daq_preflight.py not found; preflight skipped"
        return 0
    fi
    python3 daq_preflight.py --config daq_chain_config.ini --plus-live
    rc=$?
    if [ "$rc" -eq 2 ] && [ "$HD_PREFLIGHT" = strict ]; then
        printf '\033[91mERROR: preflight failed ([host] preflight = strict) - DAQ chain not started!\033[39m\n'
        exit 4
    fi
    if [ "$rc" -ne 0 ]; then
        echo "WARN: preflight reported problems (exit $rc); continuing ([host] preflight = $HD_PREFLIGHT)"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Launch helpers
# ---------------------------------------------------------------------------

# hd_launch CMD...: start one stage in the background. The stage never
# inherits the startup mutex (fd 8) and keeps the instance lock (fd 9).
# $! is the stage's pid. Pipelines carry the same `8>&-` inline.
hd_launch() {
    "$@" 8>&- &
}

# hd_abort_launch N RC: EXIT-trap body armed after the first stage started;
# a non-zero exit stops instance N so no stage is orphaned.
hd_abort_launch() {
    [ "${2:-0}" -eq 0 ] && return 0
    echo "Start of instance $1 failed after launching stages (exit $2) - stopping it"
    ./daq_stop.sh "$1"
}

hd_arm_abort() {
    # shellcheck disable=SC2064
    trap "hd_abort_launch '$1' \$?" EXIT
}

hd_disarm_abort() {
    trap - EXIT
}

# hd_registry_write MODE PID_DIR CORES: write $HD_RUN/inst<N>.env and
# inst<N>.json (each atomically), record $HD_RUN in PID_DIR/run_dir, then
# close fd 8/9 in the script (the stages keep their own fd 9).
hd_registry_write() {
    local mode piddir cores n off pids f role pid st ev tmp started
    mode="$1"
    piddir="$2"
    cores="$3"
    n="$HD_INSTANCE_ID"
    if [ -n "${HD_RUN:-}" ] && [ -w "$HD_RUN" ]; then
        off=$((n * HD_PORT_STRIDE))
        st=""
        ev=""
        if [ "$HD_EN_MONITORING" = 1 ] && [ "$HD_EN_STATUS_SERVER" = 1 ]; then
            st=$((HD_STATUS_SERVER_PORT + off))
        fi
        if [ "$HD_EN_MONITORING" = 1 ] && [ "$HD_EN_ZMQ_PUB" = 1 ]; then
            ev=$((HD_ZMQ_PUB_PORT + off))
        fi
        pids=""
        for f in "$piddir"/*.pid; do
            [ -f "$f" ] || continue
            role="${f##*/}"
            role="${role%.pid}"
            pid=$(head -n 1 "$f")
            case "$pid" in ''|*[!0-9]*) continue ;; esac
            pids="${pids:+$pids,}$role:$pid"
        done
        started=$(date +%s)
        tmp="$HD_RUN/.inst$n.env.$$"
        if {
            printf 'SCHEMA=1\nINSTANCE_ID=%s\nMODE=%s\nCWD=%s\nSTARTED_AT=%s\n' "$n" "$mode" "$(pwd -P)" "$started"
            printf 'GUARD=%s\nLAUNCHER=%s\nUNIT=%s\n' "$HD_INSTANCE_GUARD" "${HEIMDALL_LAUNCHER:-manual}" "${HEIMDALL_UNIT:-}"
            printf 'PORT_IQ=%s\nPORT_CTL=%s\nPORT_STATUS=%s\nPORT_EVENTS=%s\nPORT_ZMQ=%s\n' \
                $((5000 + off)) $((5001 + off)) "$st" "$ev" $((1130 + off))
            printf 'CORES=%s\nSERIALS=%s\nPIDS=%s\n' "$cores" "$HD_SERIALS" "$pids"
        } > "$tmp" 2>/dev/null && chmod 0644 "$tmp" && mv -f "$tmp" "$HD_RUN/inst$n.env"; then
            if ! python3 -I - "$HD_RUN/inst$n.json" "$n" "$mode" "$HD_NODE_ID" "$(pwd -P)" \
                "$started" "$HD_INSTANCE_GUARD" "${HEIMDALL_LAUNCHER:-manual}" "${HEIMDALL_UNIT:-}" \
                "$((5000 + off))" "$((5001 + off))" "$st" "$ev" "$((1130 + off))" "$cores" \
                "$HD_RT_PRIORITY_OFFSET" "$HD_SERIALS" "$HD_DB_DIR" "$pids" <<'HD_PY'
import hashlib
import json
import os
import sys

(path, n, mode, node_id, cwd, started, guard, launcher, unit, p_iq, p_ctl, p_st, p_ev,
 p_zmq, cores, rt_off, serials, db_dir, pids) = sys.argv[1:]


def port(v):
    return int(v) if v else None


try:
    with open("daq_chain_config.ini", "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
except OSError:
    sha = None
entry = {
    "schema": 1, "instance_id": int(n), "mode": mode, "node_id": node_id, "cwd": cwd,
    "install_root": os.path.dirname(os.path.dirname(os.path.realpath("_daq_core"))),
    "config_sha256": sha, "started_at": int(started), "guard": guard,
    "launcher": launcher, "unit": unit,
    "ports": {"iq": port(p_iq), "ctl": port(p_ctl), "status": port(p_st),
              "events": port(p_ev), "zmq": port(p_zmq)},
    "cores": [int(c) for c in cores.split(",") if c],
    "rt_priority_offset": int(rt_off),
    "serials": [s for s in serials.split(",") if s],
    "db_dir": db_dir or None,
    "pids": {r: int(p) for r, p in (i.split(":", 1) for i in pids.split(",") if i)},
}
tmp = "{}.{}.tmp".format(path, os.getpid())
with open(tmp, "w") as f:
    json.dump(entry, f, indent=1, sort_keys=True)
    f.write("\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
HD_PY
            then
                echo "WARN: cannot write registry entry $HD_RUN/inst$n.json"
            fi
        else
            rm -f "$tmp"
            echo "WARN: cannot write registry entry $HD_RUN/inst$n.env"
        fi
        if [ -d "$piddir" ]; then
            printf '%s\n' "$HD_RUN" > "$piddir/run_dir" 2>/dev/null || true
        fi
    fi
    exec 9>&- 8>&-
}
