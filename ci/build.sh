#!/usr/bin/env bash
#
# CI build + lint helper for the HeIMDALL DAQ firmware - locally reproducible.
#
#   Usage: ci/build.sh <lane>
#
#   Lanes:
#     generic      Dependency-light build: krakenrf librtlsdr only, plain-C FIR
#                  engine (make ENGINE=generic MARCH=portable). No KFR / Ne10.
#                  Afterwards also builds the optional targets hat_usb3_bridge,
#                  hat_pcie_bridge and (when the Makefile has it)
#                  device_map_probe - see "Changed-file rule" for when a
#                  failure there is fatal.
#     kfr          Full engine build: librtlsdr + KFR capi built from source
#                  (make ENGINE=kfr MARCH=portable).
#     asan         generic lane compiled with BUILD=asan
#                  (-fsanitize=address,undefined).
#     deploy-lint  Script/unit/config lint (needs no C build): bash -n and
#                  ShellCheck of Firmware/*.sh util/*.sh ci/build.sh, a
#                  dash-mode ShellCheck of the POSIX scripts, systemd-analyze verify of
#                  packaging/systemd units against a stub /opt/heimdall tree,
#                  the User= unit directory check, ini_checker on every
#                  preset, federation.ini / cluster_cfg_gen / provisioning
#                  dry runs, --help smoke tests and udevadm verify. Inputs
#                  that do not exist yet print "SKIP <path> (not present yet)".
#
#   Changed-file rule (deploy-lint findings, generic-lane optional targets):
#     HEIMDALL_STRICT_MODULES=1  every finding / optional-target failure is
#                                fatal.
#     CI_BASE_REF=<branch>       (pull requests) fatal only when the finding's
#                                file is in
#                                `git diff --name-only origin/<branch>...HEAD`;
#                                findings in untouched files are reported in
#                                the summary table but do not fail the lane.
#                                If the diff cannot be computed every finding
#                                is fatal (fail closed).
#     CI_BASE_SHA=<sha>          (pushes; used when CI_BASE_REF is empty) the
#                                same rule against
#                                `git diff --name-only <sha>...HEAD`, i.e. the
#                                commits the push added (github.event.before).
#                                An all-zero sha (new branch) or an
#                                unreachable sha is fail-closed (strict).
#     none of these              report-only (exit 0 despite findings), e.g.
#                                the nightly schedule / workflow_dispatch.
#   Under GitHub Actions every non-fatal finding is also emitted as a
#   ::warning:: annotation (and the deploy-lint table goes to the job
#   summary), so report-only regressions on main stay visible.
#
#   Environment:
#     DEPS_DIR       Cache directory for cloned/built dependencies
#                    (default: <repo>/.ci-deps). Safe to cache in CI keyed on
#                    LIBRTLSDR_REF / KFR_REF; a cached build made from a
#                    different ref is rebuilt.
#     LIBRTLSDR_REF  git ref of krakenrf/librtlsdr to build (default: master).
#                    A branch, a tag or a commit sha (7-40 hex digits; cloned
#                    without --branch, then checked out).
#     KFR_REF        git ref of kfrlib/kfr to build (default: 7.1.0); same
#                    forms as LIBRTLSDR_REF.
#     MAKE_JOBS      parallelism (default: nproc)
#     CI_OPTIONAL_TARGETS  1 (default) = the generic lane also builds the
#                    optional targets; 0 = core binaries only (pipeline job)
#     PYTHON         interpreter for deploy-lint (default: python3)
#     HEIMDALL_STRICT_MODULES, CI_BASE_REF, CI_BASE_SHA
#                    see "Changed-file rule"
#
#   Required system packages (ubuntu): build-essential cmake git
#     libusb-1.0-0-dev libzmq3-dev; the kfr lane additionally needs clang;
#     deploy-lint needs shellcheck and systemd (systemd-analyze, udevadm).
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
set -euo pipefail

LANE="${1:-generic}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORE="$REPO_ROOT/Firmware/_daq_core"
DEPS_DIR="${DEPS_DIR:-$REPO_ROOT/.ci-deps}"
LIBRTLSDR_REF="${LIBRTLSDR_REF:-master}"
KFR_REF="${KFR_REF:-7.1.0}"
MAKE_JOBS="${MAKE_JOBS:-$(nproc 2>/dev/null || echo 2)}"
PYTHON="${PYTHON:-python3}"
STRICT="${HEIMDALL_STRICT_MODULES:-0}"
CI_BASE_REF="${CI_BASE_REF:-}"
CI_BASE_SHA="${CI_BASE_SHA:-}"
CI_OPTIONAL_TARGETS="${CI_OPTIONAL_TARGETS:-1}"

case "$STRICT" in
    0|1) ;;
    *) echo "[ci/build.sh] HEIMDALL_STRICT_MODULES must be 0 or 1 (got '$STRICT')" >&2
       exit 2 ;;
esac

log() { echo "[ci/build.sh] $*"; }

# --- dependency sources -----------------------------------------------------

is_sha() {
    printf '%s' "$1" | grep -Eqx '[0-9a-fA-F]{7,40}'
}

# clone_ref <url> <ref> <dest>: shallow branch/tag clone, or full clone +
# detached checkout for a commit sha (G68).
clone_ref() {
    local url="$1" ref="$2" dest="$3"
    rm -rf "$dest"
    if is_sha "$ref"; then
        git clone "$url" "$dest"
        git -C "$dest" checkout --detach "$ref"
    else
        git clone --depth 1 --branch "$ref" "$url" "$dest"
    fi
    printf '%s\n' "$ref" > "$dest/.heimdall-ci-ref"
}

# cached_ref_ok <dest> <ref>: the cached clone was made from <ref>.
cached_ref_ok() {
    [ -f "$1/.heimdall-ci-ref" ] && [ "$(cat "$1/.heimdall-ci-ref")" = "$2" ]
}

# --- krakenrf librtlsdr (fork API: rtlsdr_set_dithering etc. - the stock
# --- apt librtlsdr does NOT work, see CLAUDE.md) --------------------------
build_librtlsdr() {
    local src="$DEPS_DIR/librtlsdr"
    if [ -f "$src/build/src/librtlsdr.a" ] && cached_ref_ok "$src" "$LIBRTLSDR_REF"; then
        log "librtlsdr: using cached build ($LIBRTLSDR_REF)"
    else
        log "building krakenrf librtlsdr ($LIBRTLSDR_REF)"
        clone_ref https://github.com/krakenrf/librtlsdr "$LIBRTLSDR_REF" "$src"
        cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release \
              -DINSTALL_UDEV_RULES=OFF >/dev/null
        cmake --build "$src/build" -j "$MAKE_JOBS" >/dev/null
    fi
    cp "$src/build/src/librtlsdr.a" "$CORE/"
    cp "$src/include/rtl-sdr.h" "$src/include/rtl-sdr_export.h" "$CORE/"
}

# --- KFR C API (needs clang; PIC is required on arm64) --------------------
build_kfr() {
    local src="$DEPS_DIR/kfr"
    if ls "$src"/build/lib/libkfr_capi.so* >/dev/null 2>&1 && cached_ref_ok "$src" "$KFR_REF"; then
        log "KFR: using cached build ($KFR_REF)"
    else
        log "building KFR capi ($KFR_REF) - this is the slow step, cache \$DEPS_DIR"
        clone_ref https://github.com/kfrlib/kfr "$KFR_REF" "$src"
        cmake -S "$src" -B "$src/build" \
              -DCMAKE_BUILD_TYPE=Release \
              -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
              -DKFR_ENABLE_CAPI_BUILD=ON \
              -DKFR_ENABLE_DFT=ON \
              -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ >/dev/null
        cmake --build "$src/build" --target kfr_capi -j "$MAKE_JOBS" >/dev/null
    fi
    cp "$src"/build/lib/libkfr_capi.so* "$CORE/"
    rm -rf "$CORE/kfr"
    cp -r "$src/include/kfr" "$CORE/kfr"
}

check_outputs() {
    local missing=0
    for b in rtl_daq.out rebuffer.out decimate.out iq_server.out; do
        if [ ! -x "$CORE/$b" ]; then
            log "MISSING: $b"
            missing=1
        fi
    done
    [ "$missing" -eq 0 ] || { log "FAIL: core binaries missing"; exit 1; }
    log "OK: all four core binaries built ($LANE lane)"
}

# --- changed-file rule ------------------------------------------------------

CHANGED_FILES=""
CHANGED_MODE="report"   # report | changed | strict
CHANGED_BASE=""         # what the changed-file diff was taken against

init_changed_files() {
    local base
    if [ "$STRICT" = 1 ]; then
        CHANGED_MODE="strict"
        return 0
    fi
    if [ -n "$CI_BASE_REF" ]; then
        base="origin/$CI_BASE_REF"
    elif [ -n "$CI_BASE_SHA" ]; then
        if printf '%s' "$CI_BASE_SHA" | grep -Eqx '0+'; then
            log "WARNING: push has no base commit (new branch) - treating every finding as fatal"
            CHANGED_MODE="strict"
            return 0
        fi
        base="$CI_BASE_SHA"
    else
        return 0
    fi
    if CHANGED_FILES="$(git -C "$REPO_ROOT" diff --name-only "$base...HEAD")"; then
        CHANGED_MODE="changed"
        CHANGED_BASE="$base"
    else
        log "WARNING: cannot diff against $base - treating every finding as fatal"
        CHANGED_MODE="strict"
    fi
}

describe_mode() {
    case "$CHANGED_MODE" in
        strict)  echo "strict: every finding is fatal" ;;
        changed) echo "changed-file rule vs $CHANGED_BASE" ;;
        *)       echo "report-only: set CI_BASE_REF, CI_BASE_SHA or HEIMDALL_STRICT_MODULES=1 to gate" ;;
    esac
}

# gha_warning <title> <message>: GitHub Actions ::warning:: annotation for a
# non-fatal finding (no-op outside Actions).
gha_warning() {
    [ "${GITHUB_ACTIONS:-}" = "true" ] || return 0
    local msg
    # workflow-command escaping: % CR LF
    msg="$(printf '%s' "$2" | sed -e 's/%/%25/g' | tr '\n' ' ')"
    echo "::warning title=$1::$msg ($(describe_mode))"
}

# is_fatal <repo-relative path>...: a finding attributed to these files fails
# the lane under the current mode.
is_fatal() {
    local f
    case "$CHANGED_MODE" in
        strict) return 0 ;;
        changed)
            for f in "$@"; do
                if printf '%s\n' "$CHANGED_FILES" | grep -Fqx -- "$f"; then
                    return 0
                fi
            done
            return 1 ;;
        *) return 1 ;;
    esac
}

# --- generic lane: optional Makefile targets --------------------------------

build_optional_targets() {
    local t failed=0 targets=""
    for t in hat_usb3_bridge hat_pcie_bridge device_map_probe; do
        if grep -Eq "^${t}[[:space:]]*:" "$CORE/Makefile"; then
            targets="$targets $t"
        else
            log "SKIP make $t (no such Makefile target yet)"
        fi
    done
    [ -n "$targets" ] || return 0
    for t in $targets; do
        log "building optional target: $t"
        if ! make -C "$CORE" ENGINE=generic MARCH=portable "$t"; then
            log "optional target $t FAILED"
            failed=1
        fi
    done
    [ "$failed" -eq 1 ] || { log "OK: optional targets built:$targets"; return 0; }
    init_changed_files
    if is_fatal Firmware/_daq_core/Makefile \
                Firmware/_daq_core/hat_usb3_bridge.c \
                Firmware/_daq_core/hat_pcie_bridge.c \
                Firmware/_daq_core/device_map.c \
                Firmware/_daq_core/device_map.h; then
        log "FAIL: optional target build failed ($(describe_mode))"
        exit 1
    fi
    log "optional target failure does not fail the lane ($(describe_mode))"
    gha_warning "optional target build" "one or more optional Makefile targets failed to build (attempted:$targets)"
}

# --- deploy-lint ------------------------------------------------------------

LINT_SUMMARY=""
LINT_FATAL=0
LINT_FINDINGS=0
LINT_TMP=""

# finding <check> <file> [<more attributed files>...]
finding() {
    local check="$1" file="$2" status=REPORT
    LINT_FINDINGS=$((LINT_FINDINGS + 1))
    shift
    if is_fatal "$@"; then
        status=FATAL
        LINT_FATAL=1
    else
        gha_warning "deploy-lint: $check" "$file"
    fi
    LINT_SUMMARY="${LINT_SUMMARY}$(printf '%-6s  %-28s  %s' "$status" "$check" "$file")
"
    echo "  -> $status: $check: $file"
}

skip_missing() {
    echo "SKIP $1 (not present yet)"
}

# need_tool <tool> <check>: 0 when available. A missing tool is a lane
# failure on CI (CI=true) and a SKIP elsewhere.
need_tool() {
    if command -v "$1" >/dev/null 2>&1; then
        return 0
    fi
    if [ "${CI:-}" = "true" ]; then
        echo "ERROR: $1 is required for '$2' on CI" >&2
        LINT_FATAL=1
        LINT_FINDINGS=$((LINT_FINDINGS + 1))
        LINT_SUMMARY="${LINT_SUMMARY}$(printf '%-6s  %-28s  %s' FATAL "$2" "($1 not installed)")
"
    else
        echo "SKIP $2 ($1 not installed)"
    fi
    return 1
}

# run_limited <seconds> <cmd...>: run with a wall-clock limit (coreutils
# timeout, else perl alarm - macOS has no timeout(1)).
run_limited() {
    local secs="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
    else
        perl -e 'alarm shift; exec @ARGV or die "exec: $!"' "$secs" "$@"
    fi
}

lint_scripts() {
    local f out sc=1
    echo "===== bash -n / shellcheck: Firmware/*.sh util/*.sh ci/build.sh ====="
    need_tool shellcheck shellcheck || sc=0
    for f in Firmware/*.sh util/*.sh ci/build.sh; do
        [ -f "$f" ] || continue
        if ! out="$(bash -n "$f" 2>&1)"; then
            printf '%s\n' "$out"
            finding "bash -n" "$f"
        fi
        [ "$sc" -eq 1 ] || continue
        # Run from the script's directory so `# shellcheck source=daq_common.sh`
        # directives resolve with -x.
        if ! out="$(cd "$(dirname "$f")" && shellcheck -S warning -x -e SC3043 "$(basename "$f")" 2>&1)"; then
            printf '%s\n' "$out"
            finding "shellcheck" "$f"
        fi
    done
    [ "$sc" -eq 1 ] || return 0
    echo "===== shellcheck -s dash: POSIX scripts ====="
    for f in Firmware/daq_stop.sh Firmware/daq_common.sh; do
        if [ ! -f "$f" ]; then
            skip_missing "$f"
            continue
        fi
        if ! out="$(cd "$(dirname "$f")" && shellcheck -s dash -S warning -x -e SC3043 "$(basename "$f")" 2>&1)"; then
            printf '%s\n' "$out"
            finding "shellcheck -s dash" "$f"
        fi
    done
}

# unit_instance_name <file>: the name systemd-analyze gives a unit file
# (a template foo@.service verifies as foo@i.service).
unit_instance_name() {
    local b
    b="$(basename "$1")"
    case "$b" in
        *@.*) printf '%s@i.%s\n' "${b%%@.*}" "${b##*@.}" ;;
        *)    printf '%s\n' "$b" ;;
    esac
}

lint_systemd() {
    local units="" u stub prefix exe out rc matched attributed b
    echo "===== systemd-analyze verify: packaging/systemd ====="
    for u in packaging/systemd/*.service packaging/systemd/*.timer; do
        [ -f "$u" ] && units="$units $u"
    done
    if [ -z "$units" ]; then
        skip_missing "packaging/systemd/*.service"
        return 0
    fi
    need_tool systemd-analyze "systemd-analyze verify" || return 0
    # Stub install root: every /opt/heimdall path is rewritten into a temp
    # tree whose Exec* targets are executable stubs and whose working
    # directories exist (no root needed, the real /opt is never touched).
    stub="$LINT_TMP/systemd"
    prefix="$stub/root"
    mkdir -p "$stub/units" "$prefix"
    for u in $units; do
        sed "s#/opt/heimdall#$prefix/opt/heimdall#g" "$u" > "$stub/units/$(basename "$u")"
    done
    for u in "$stub"/units/*; do
        sed -n 's/^[[:space:]]*Exec[A-Za-z]*=[-@:+!|]*\([^[:space:]]*\).*/\1/p' "$u" |
            sed 's/%[iI]/i/g' | while IFS= read -r exe; do
                case "$exe" in
                    "$prefix"/*)
                        mkdir -p "$(dirname "$exe")"
                        printf '#!/bin/sh\nexit 0\n' > "$exe"
                        chmod +x "$exe" ;;
                esac
            done
        sed -n 's/^[[:space:]]*WorkingDirectory=[-]*\(.*\)$/\1/p' "$u" |
            sed 's/%[iI]/i/g' | while IFS= read -r exe; do
                case "$exe" in
                    "$prefix"/*) mkdir -p "$exe" ;;
                esac
            done
    done
    rc=0
    out="$(SYSTEMD_LOG_COLOR=0 systemd-analyze verify "$stub"/units/* 2>&1)" || rc=$?
    out="$(printf '%s\n' "$out" | sed "s#$prefix##g; s#$stub/units/##g")"
    [ -z "$out" ] || printf '%s\n' "$out"
    # Attribute each output line to the unit it names; anything else on a
    # failing run is attributed to every unit.
    attributed=0
    for u in $units; do
        b="$(basename "$u")"
        matched="$(printf '%s\n' "$out" | grep -F -e "$b:" -e "$(unit_instance_name "$u"):" || true)"
        if [ -n "$matched" ]; then
            finding "systemd-analyze" "$u"
            attributed=1
        fi
    done
    if [ "$rc" -ne 0 ] && [ "$attributed" -eq 0 ]; then
        for u in $units; do
            finding "systemd-analyze" "$u"
        done
    fi
}

# H.4: units running as an unprivileged User= declare the directories they
# write (LogsDirectory always; StateDirectory with --state-dir;
# RuntimeDirectory with --pid-dir), and no unit has an inline comment after
# a value (systemd does not support them). The inline-comment check is a
# heuristic: it flags ' # ' (or a trailing ' #') in an unquoted value and
# ignores a '#' that follows a quote, so Environment="X=a # b" or
# ExecStart=/bin/sh -c '... # ...' and values like "chain #2" pass.
# ExecStart= continuation lines (trailing backslash) are joined before the
# --state-dir / --pid-dir match.
lint_unit_directories() {
    local u user execline bad ws
    # literal space + tab: some greps (ugrep as macOS grep) mis-handle
    # [[:space:]] combined with a negated bracket in this pattern.
    ws="$(printf ' \t')"
    echo "===== unit check: User= directories, inline comments ====="
    for u in packaging/systemd/*.service packaging/systemd/*.timer; do
        [ -f "$u" ] || continue
        bad=0
        if grep -nE "^[$ws]*[A-Za-z][A-Za-z0-9]*=[^\"']*[$ws]#([$ws]|\$)" "$u"; then
            echo "$u: inline comment after a value (systemd keeps it as part of the value)"
            bad=1
        fi
        user="$(sed -n 's/^[[:space:]]*User=[[:space:]]*\([^[:space:]]*\).*/\1/p' "$u" | tail -n 1)"
        if [ -n "$user" ] && [ "$user" != "root" ] && [ "$user" != "0" ]; then
            execline="$(sed -e ':a' -e '/\\$/{N' -e 's/\\\n//' -e 'ba' -e '}' "$u" |
                grep -E '^[[:space:]]*ExecStart=' || true)"
            if ! grep -Eq '^[[:space:]]*LogsDirectory=' "$u"; then
                echo "$u: User=$user but no LogsDirectory="
                bad=1
            fi
            case "$execline" in
                *--state-dir*)
                    grep -Eq '^[[:space:]]*StateDirectory=' "$u" ||
                        { echo "$u: User=$user, ExecStart passes --state-dir but no StateDirectory="; bad=1; } ;;
            esac
            case "$execline" in
                *--pid-dir*)
                    grep -Eq '^[[:space:]]*RuntimeDirectory=' "$u" ||
                        { echo "$u: User=$user, ExecStart passes --pid-dir but no RuntimeDirectory="; bad=1; } ;;
            esac
        fi
        [ "$bad" -eq 0 ] || finding "unit directories" "$u"
    done
}

lint_presets() {
    local p rc
    echo "===== ini_checker: config_files/*/daq_chain_config.ini (no_hw) ====="
    for p in config_files/*/daq_chain_config.ini; do
        [ -f "$p" ] || continue
        echo "--- $p"
        rc=0
        (cd Firmware && "$PYTHON" ini_checker.py "../$p" no_hw) || rc=$?
        [ "$rc" -eq 0 ] || finding "ini_checker no_hw" "$p" Firmware/ini_checker.py
    done
    echo "===== ini_checker --federation ====="
    p=config_files/cluster_site/federation.ini
    if [ -f "$p" ]; then
        rc=0
        (cd Firmware && "$PYTHON" ini_checker.py "../$p" --federation) || rc=$?
        [ "$rc" -eq 0 ] || finding "ini_checker --federation" "$p" Firmware/ini_checker.py
    else
        skip_missing "$p"
    fi
}

lint_generators() {
    local gen=util/cluster_cfg_gen.py prov=util/provision_instance.py out f rc root
    echo "===== cluster_cfg_gen.py round trip ====="
    if [ -f "$gen" ]; then
        out="$LINT_TMP/cluster"
        rc=0
        "$PYTHON" "$gen" --template Firmware/daq_chain_config.ini \
            --hosts hosta:2 hostb:1 --cores-per-host hosta=8,hostb=4 \
            --coordinators hosta:6000,hostb:6000,hostc:6000 --router hosta:7000 \
            --out "$out" || rc=$?
        if [ "$rc" -ne 0 ]; then
            finding "cluster_cfg_gen" "$gen"
        else
            for f in "$out"/*/inst*/daq_chain_config.ini; do
                [ -f "$f" ] || { echo "$gen wrote no instance INI under $out"; finding "cluster_cfg_gen" "$gen"; break; }
                rc=0
                (cd Firmware && "$PYTHON" ini_checker.py "$f" no_hw) || rc=$?
                [ "$rc" -eq 0 ] || finding "cluster_cfg_gen output" "$gen" Firmware/ini_checker.py
            done
            rc=0
            (cd Firmware && "$PYTHON" ini_checker.py "$out/federation.ini" --federation) || rc=$?
            [ "$rc" -eq 0 ] || finding "cluster_cfg_gen federation.ini" "$gen" Firmware/ini_checker.py
        fi
    else
        skip_missing "$gen"
    fi
    echo "===== provision_instance.py dry run ====="
    if [ -f "$prov" ]; then
        root="$LINT_TMP/provroot"
        mkdir -p "$root"
        ln -s "$REPO_ROOT/Firmware" "$root/Firmware"
        ln -s "$REPO_ROOT/util" "$root/util"
        ln -s "$REPO_ROOT/config_files" "$root/config_files"
        rc=0
        "$PYTHON" "$prov" --instance 1 --root "$root" || rc=$?
        [ "$rc" -eq 0 ] || finding "provision dry run" "$prov"
    else
        skip_missing "$prov"
    fi
}

lint_help() {
    local f rc
    echo "===== --help smoke tests ====="
    for f in Firmware/_daq_core/federation_coordinator.py \
             Firmware/_daq_core/federation_iq_router.py \
             Firmware/daq_preflight.py \
             util/provision_instance.py \
             util/cluster_cfg_gen.py; do
        if [ ! -f "$f" ]; then
            skip_missing "$f"
            continue
        fi
        rc=0
        run_limited 60 "$PYTHON" "$f" --help >"$LINT_TMP/help.out" 2>&1 || rc=$?
        if [ "$rc" -eq 0 ]; then
            echo "OK   $f --help"
        else
            cat "$LINT_TMP/help.out"
            finding "--help (exit $rc)" "$f"
        fi
    done
}

lint_udev() {
    local r rules="" out
    echo "===== udevadm verify: packaging/udev ====="
    for r in packaging/udev/*.rules; do
        [ -f "$r" ] && rules="$rules $r"
    done
    if [ -z "$rules" ]; then
        skip_missing "packaging/udev/*.rules"
        return 0
    fi
    if ! udevadm verify --help >/dev/null 2>&1; then
        echo "SKIP udevadm verify (not available on this host)"
        return 0
    fi
    for r in $rules; do
        if ! out="$(udevadm verify "$r" 2>&1)"; then
            printf '%s\n' "$out"
            finding "udevadm verify" "$r"
        fi
    done
}

deploy_lint() {
    cd "$REPO_ROOT"
    init_changed_files
    LINT_TMP="$(mktemp -d)"
    trap 'rm -rf "$LINT_TMP"' EXIT
    log "deploy-lint ($(describe_mode))"
    lint_scripts
    lint_systemd
    lint_unit_directories
    lint_presets
    lint_generators
    lint_help
    lint_udev
    echo
    echo "===== deploy-lint summary ($(describe_mode)) ====="
    if [ -z "$LINT_SUMMARY" ]; then
        echo "no findings"
    else
        printf '%-6s  %-28s  %s\n' STATUS CHECK FILE
        printf '%s' "$LINT_SUMMARY"
    fi
    echo "===== $LINT_FINDINGS finding(s); lane $( [ "$LINT_FATAL" -eq 0 ] && echo passed || echo FAILED ) ====="
    if [ -n "$LINT_SUMMARY" ] && [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        {
            echo "### deploy-lint: $LINT_FINDINGS finding(s) ($(describe_mode))"
            echo '```'
            printf '%-6s  %-28s  %s\n' STATUS CHECK FILE
            printf '%s' "$LINT_SUMMARY"
            echo '```'
        } >> "$GITHUB_STEP_SUMMARY" 2>/dev/null || true
    fi
    [ "$LINT_FATAL" -eq 0 ]
}

case "$LANE" in
    generic)
        mkdir -p "$DEPS_DIR"
        build_librtlsdr
        make -C "$CORE" clean
        make -C "$CORE" -j "$MAKE_JOBS" ENGINE=generic MARCH=portable
        check_outputs
        if [ "$CI_OPTIONAL_TARGETS" = 1 ]; then
            build_optional_targets
        else
            log "optional targets skipped (CI_OPTIONAL_TARGETS=$CI_OPTIONAL_TARGETS)"
        fi
        ;;
    kfr)
        mkdir -p "$DEPS_DIR"
        build_librtlsdr
        build_kfr
        make -C "$CORE" clean
        make -C "$CORE" -j "$MAKE_JOBS" ENGINE=kfr MARCH=portable
        check_outputs
        ;;
    asan)
        mkdir -p "$DEPS_DIR"
        build_librtlsdr
        make -C "$CORE" clean
        make -C "$CORE" -j "$MAKE_JOBS" ENGINE=generic MARCH=portable BUILD=asan
        check_outputs
        ;;
    deploy-lint)
        deploy_lint
        ;;
    *)
        echo "Usage: ci/build.sh generic|kfr|asan|deploy-lint" >&2
        exit 2
        ;;
esac
