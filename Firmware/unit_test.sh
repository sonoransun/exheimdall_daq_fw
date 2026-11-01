#!/bin/sh
# ---------------------------------------------------------------------------
# HeIMDALL DAQ Firmware - unit test runner
#
# Usage:
#   ./unit_test.sh            Legacy behavior: run the decimator pipeline
#                             suite (requires sudo + built C binaries),
#                             exactly what the historic script did.
#   ./unit_test.sh --ci       Run the ENTIRE hardware-free, no-sudo suite.
#                             Works on macOS and Ubuntu with only Python 3 +
#                             numpy/scipy. Optional extras (C compiler,
#                             berkeleydb, pyzmq, numba) enable more tests;
#                             without them the affected tests SKIP with a
#                             printed reason. Exit code: 0 = all green,
#                             1 = at least one module failed.
#   ./unit_test.sh --sudo     Run the pipeline/integration suites that spawn
#                             the C binaries over FIFOs + shared memory.
#                             Requires: Linux, sudo/root, built
#                             _daq_core/{rebuffer,decimate,iq_server}.out,
#                             and the unit_test_k4 config values in
#                             daq_chain_config.ini (the tests copy the
#                             config to a temp dir - the live file is never
#                             modified). CI runs it in the `pipeline` job.
#   ./unit_test.sh --list     Print every registered module per lane and
#                             whether its file is present or pending.
#   ./unit_test.sh --help     This text.
#
# Pending modules: a registered module whose file
# _testing/unit_test/<module>.py does not exist yet prints
#   PEND  <module> (not present yet)
# and is not counted as a failure. With HEIMDALL_STRICT_MODULES=1 a missing
# module is a FAIL (CI switches this on once every package has merged).
#
# Environment:
#   PYTHON                    interpreter (default: python3)
#   HEIMDALL_STRICT_MODULES   0 (default) or 1, see "Pending modules"
#
# Removed suites: test_squelch.py + gen_burst.py were deleted - squelch.out
# has no source and no Makefile target, no config carries a [squelch]
# section, and delay_sync has no squelch support (suite failed in setUp).
# ---------------------------------------------------------------------------

set -u
cd "$(dirname "$0")" || exit 1

PYTHON="${PYTHON:-python3}"

STRICT_MODULES="${HEIMDALL_STRICT_MODULES:-0}"

# Validated only by the lanes that use it (--ci, --sudo), so --list, --help
# and the no-argument legacy mode never depend on the variable.
check_strict_modules() {
    case "$STRICT_MODULES" in
        0|1) ;;
        *)
            echo "ERROR: HEIMDALL_STRICT_MODULES must be 0 or 1 (got '$STRICT_MODULES')." >&2
            exit 2
            ;;
    esac
}

# The hardware-free suite: every module below runs without sudo, without SDR
# hardware, and without the C binaries. (test_iq_header_abi compiles a probe
# with cc/gcc/clang when available and skips otherwise; test_daq_db skips
# its integration cases without berkeleydb; test_control_iface and
# test_delay_sync_dsp stub zmq/numba when missing.) One module per line;
# modules of packages that have not merged yet print PEND (see header).
CI_MODULES="
test_iq_header_v8
test_iq_header_abi
test_inter_module_messages
test_shmem_iface
test_control_iface
test_delay_sync_dsp
test_fir_designer
test_ini_checker
test_gain_budget
test_signal_scheduler
test_orientation_controller
test_daq_db
test_monitoring
test_federation
test_heimdall_ctl
test_federation_common
test_hwc_relay
test_device_map
test_hwc_init_instance
test_status_extended
test_delay_sync_fsm
test_federation_protocol
test_federation_election
test_iq_router
test_synthetic_control
test_sync_loop_model
test_launch_scripts
test_capacity_model
test_frame_sizes
test_provisioning
test_set_unit_serials
test_heimdall_ctl_fleet
test_watchdog
test_iq_capture_format
test_iq_replay
"

# Pipeline suites (Linux + sudo + built C binaries). test_sys (full chain
# via daq_start_sm.sh, manual lane) is deliberately not listed.
SUDO_MODULES="
test_rebuffer
test_decimator
test_delay_sync
test_iq_server
test_start_stop_legacy
test_multi_instance
test_synthetic_chain
test_bit_identity
test_throughput_lane
"

run_modules() {
    # $1: whitespace-separated module list
    # Runs each module in its own interpreter (stubbed optional modules and
    # cwd changes cannot leak between suites), prints a per-module summary,
    # returns nonzero if any module failed. A module whose file does not
    # exist is PEND (not a failure) unless HEIMDALL_STRICT_MODULES=1.
    modules="$1"
    total=0
    failed=0
    pending=0
    summary=""
    logfile="$(mktemp)" || exit 1

    for module in $modules; do
        if [ ! -f "_testing/unit_test/${module}.py" ]; then
            if [ "$STRICT_MODULES" = 1 ]; then
                total=$((total + 1))
                failed=$((failed + 1))
                printf 'FAIL  %s (not present; HEIMDALL_STRICT_MODULES=1)\n' "$module"
                summary="${summary}FAIL  ${module} (not present)
"
            else
                pending=$((pending + 1))
                printf 'PEND  %s (not present yet)\n' "$module"
                summary="${summary}PEND  ${module} (not present yet)
"
            fi
            continue
        fi
        total=$((total + 1))
        printf '===== %s =====\n' "$module"
        if "$PYTHON" -W ignore -m unittest -v "_testing/unit_test/${module}.py" >"$logfile" 2>&1; then
            result="$(tail -n 3 "$logfile" | tr '\n' ' ')"
            printf 'PASS  %s\n' "$result"
            summary="${summary}PASS  ${module}
"
        else
            failed=$((failed + 1))
            cat "$logfile"
            printf 'FAIL  %s\n' "$module"
            summary="${summary}FAIL  ${module}
"
        fi
    done
    rm -f "$logfile"

    printf '\n===== Per-module summary =====\n%s' "$summary"
    if [ "$pending" -eq 0 ]; then
        printf '===== %d/%d modules passed =====\n' "$((total - failed))" "$total"
    else
        printf '===== %d/%d modules passed, %d pending (not present yet) =====\n' \
            "$((total - failed))" "$total" "$pending"
    fi
    [ "$failed" -eq 0 ]
}

list_modules() {
    # $1: lane name, $2: module list
    for module in $2; do
        if [ -f "_testing/unit_test/${module}.py" ]; then
            printf '%-5s %-8s %s\n' "$1" present "$module"
        else
            printf '%-5s %-8s %s\n' "$1" pending "$module"
        fi
    done
}

prepare_logs() {
    mkdir -p _testing/test_logs _logs
    rm -f _testing/test_logs/*.log 2> /dev/null
    rm -f _testing/test_logs/*.html 2> /dev/null
}

case "${1:-}" in
    --ci)
        check_strict_modules
        echo "HeIMDALL DAQ - hardware-free unit test suite (no sudo)"
        echo "Python: $("$PYTHON" --version 2>&1)  Platform: $(uname -sm)"
        echo "Skips print their reason; missing optional deps are not failures."
        echo
        prepare_logs
        run_modules "$CI_MODULES"
        exit $?
        ;;
    --sudo)
        check_strict_modules
        echo "HeIMDALL DAQ - pipeline unit tests (sudo + C binaries required)"
        if [ "$(uname -s)" != "Linux" ]; then
            echo "ERROR: the pipeline suites require Linux (C binaries, FIFOs)." >&2
            exit 1
        fi
        if [ "$(id -u)" -ne 0 ]; then
            echo "ERROR: run as root (sudo ./unit_test.sh --sudo)." >&2
            exit 1
        fi
        for binary in _daq_core/rebuffer.out _daq_core/decimate.out _daq_core/iq_server.out; do
            if [ ! -x "$binary" ]; then
                echo "ERROR: $binary not built (cd _daq_core && make)." >&2
                exit 1
            fi
        done
        prepare_logs
        run_modules "$SUDO_MODULES"
        status=$?
        echo
        echo "NOTE: test_sys.py (full-chain end-to-end via daq_start_sm.sh) is not"
        echo "      part of this run:"
        echo "        sudo $PYTHON -W ignore -m unittest -v _testing/unit_test/test_sys.py"
        exit $status
        ;;
    --list)
        list_modules ci "$CI_MODULES"
        list_modules sudo "$SUDO_MODULES"
        exit 0
        ;;
    --help|-h)
        sed -n '2,/^# ------/p' "$0" | sed -n '2,$p' | sed '$d'
        exit 0
        ;;
    "")
        # Legacy behavior: the historic script ran (only) the decimator
        # pipeline suite under sudo.
        echo "Start unit testing.. (legacy mode: decimator pipeline suite)"
        echo "Hint: './unit_test.sh --ci' runs the full no-sudo suite,"
        echo "      './unit_test.sh --sudo' runs all pipeline suites."
        echo "Internal warnings are ignored"
        prepare_logs
        sudo "$PYTHON" -W ignore -m unittest -v _testing/unit_test/test_decimator.py
        exit $?
        ;;
    *)
        echo "Unknown option: $1 (try --help)" >&2
        exit 2
        ;;
esac
