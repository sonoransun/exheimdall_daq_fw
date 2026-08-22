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
#                             _daq_core/rebuffer.out and decimate.out, and
#                             the unit_test_k4 config values in
#                             daq_chain_config.ini (the tests copy the
#                             config to a temp dir - the live file is never
#                             modified). NOT run in CI.
#   ./unit_test.sh --help     This text.
#
# Removed suites: test_squelch.py + gen_burst.py were deleted - squelch.out
# has no source and no Makefile target, no config carries a [squelch]
# section, and delay_sync has no squelch support (suite failed in setUp).
# ---------------------------------------------------------------------------

set -u
cd "$(dirname "$0")" || exit 1

PYTHON="${PYTHON:-python3}"

# The hardware-free suite: every module below runs without sudo, without SDR
# hardware, and without the C binaries. (test_iq_header_abi compiles a probe
# with cc/gcc/clang when available and skips otherwise; test_daq_db skips
# its integration cases without berkeleydb; test_control_iface and
# test_delay_sync_dsp stub zmq/numba when missing.)
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
"

# Pipeline suites (Linux + sudo + built C binaries). test_sys additionally
# needs the full chain startable (daq_start_sm.sh) and is therefore listed
# last; test_iq_server contains only a not-implemented placeholder.
SUDO_MODULES="
test_rebuffer
test_decimator
test_delay_sync
"

run_modules() {
    # $1: whitespace-separated module list
    # Runs each module in its own interpreter (stubbed optional modules and
    # cwd changes cannot leak between suites), prints a per-module summary,
    # returns nonzero if any module failed.
    modules="$1"
    total=0
    failed=0
    summary=""
    logfile="$(mktemp)" || exit 1

    for module in $modules; do
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
    printf '===== %d/%d modules passed =====\n' "$((total - failed))" "$total"
    [ "$failed" -eq 0 ]
}

prepare_logs() {
    mkdir -p _testing/test_logs _logs
    rm -f _testing/test_logs/*.log 2> /dev/null
    rm -f _testing/test_logs/*.html 2> /dev/null
}

case "${1:-}" in
    --ci)
        echo "HeIMDALL DAQ - hardware-free unit test suite (no sudo)"
        echo "Python: $("$PYTHON" --version 2>&1)  Platform: $(uname -sm)"
        echo "Skips print their reason; missing optional deps are not failures."
        echo
        prepare_logs
        run_modules "$CI_MODULES"
        exit $?
        ;;
    --sudo)
        echo "HeIMDALL DAQ - pipeline unit tests (sudo + C binaries required)"
        if [ "$(uname -s)" != "Linux" ]; then
            echo "ERROR: the pipeline suites require Linux (C binaries, FIFOs)." >&2
            exit 1
        fi
        if [ "$(id -u)" -ne 0 ]; then
            echo "ERROR: run as root (sudo ./unit_test.sh --sudo)." >&2
            exit 1
        fi
        for binary in _daq_core/rebuffer.out _daq_core/decimate.out; do
            if [ ! -x "$binary" ]; then
                echo "ERROR: $binary not built (cd _daq_core && make)." >&2
                exit 1
            fi
        done
        prepare_logs
        run_modules "$SUDO_MODULES"
        status=$?
        echo
        echo "NOTE: test_sys.py (full-chain end-to-end via daq_start_sm.sh) and"
        echo "      test_iq_server.py (placeholder) are not part of this run:"
        echo "        sudo $PYTHON -W ignore -m unittest -v _testing/unit_test/test_sys.py"
        exit $status
        ;;
    --help|-h)
        sed -n '2,30p' "$0"
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
