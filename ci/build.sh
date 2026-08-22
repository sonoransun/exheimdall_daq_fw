#!/usr/bin/env bash
#
# CI build helper for the HeIMDALL DAQ C core - locally reproducible.
#
#   Usage: ci/build.sh <lane>
#
#   Lanes:
#     generic   Dependency-light build: krakenrf librtlsdr only, plain-C FIR
#               engine (make ENGINE=generic MARCH=portable). No KFR / Ne10.
#     kfr       Full engine build: librtlsdr + KFR capi built from source
#               (make ENGINE=kfr MARCH=portable).
#     asan      generic lane compiled with BUILD=asan
#               (-fsanitize=address,undefined).
#
#   Environment:
#     DEPS_DIR     Cache directory for cloned/built dependencies
#                  (default: <repo>/.ci-deps). Safe to cache in CI keyed on
#                  LIBRTLSDR_REF / KFR_REF.
#     LIBRTLSDR_REF  git ref of krakenrf/librtlsdr to build (default: master)
#     KFR_REF        git ref of kfrlib/kfr to build (default: 7.1.0)
#     MAKE_JOBS      parallelism (default: nproc)
#
#   Required system packages (ubuntu): build-essential cmake git
#     libusb-1.0-0-dev libzmq3-dev; the kfr lane additionally needs clang.
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

mkdir -p "$DEPS_DIR"

log() { echo "[ci/build.sh] $*"; }

# --- krakenrf librtlsdr (fork API: rtlsdr_set_dithering etc. - the stock
# --- apt librtlsdr does NOT work, see CLAUDE.md) --------------------------
build_librtlsdr() {
    local src="$DEPS_DIR/librtlsdr"
    if [ ! -f "$src/build/src/librtlsdr.a" ]; then
        log "building krakenrf librtlsdr ($LIBRTLSDR_REF)"
        rm -rf "$src"
        git clone --depth 1 --branch "$LIBRTLSDR_REF" \
            https://github.com/krakenrf/librtlsdr "$src"
        cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release \
              -DINSTALL_UDEV_RULES=OFF >/dev/null
        cmake --build "$src/build" -j "$MAKE_JOBS" >/dev/null
    else
        log "librtlsdr: using cached build"
    fi
    cp "$src/build/src/librtlsdr.a" "$CORE/"
    cp "$src/include/rtl-sdr.h" "$src/include/rtl-sdr_export.h" "$CORE/"
}

# --- KFR C API (needs clang; PIC is required on arm64) --------------------
build_kfr() {
    local src="$DEPS_DIR/kfr"
    if ! ls "$src"/build/lib/libkfr_capi.so* >/dev/null 2>&1; then
        log "building KFR capi ($KFR_REF) - this is the slow step, cache \$DEPS_DIR"
        rm -rf "$src"
        git clone --depth 1 --branch "$KFR_REF" \
            https://github.com/kfrlib/kfr "$src"
        cmake -S "$src" -B "$src/build" \
              -DCMAKE_BUILD_TYPE=Release \
              -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
              -DKFR_ENABLE_CAPI_BUILD=ON \
              -DKFR_ENABLE_DFT=ON \
              -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ >/dev/null
        cmake --build "$src/build" --target kfr_capi -j "$MAKE_JOBS" >/dev/null
    else
        log "KFR: using cached build"
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

case "$LANE" in
    generic)
        build_librtlsdr
        make -C "$CORE" clean
        make -C "$CORE" -j "$MAKE_JOBS" ENGINE=generic MARCH=portable
        check_outputs
        ;;
    kfr)
        build_librtlsdr
        build_kfr
        make -C "$CORE" clean
        make -C "$CORE" -j "$MAKE_JOBS" ENGINE=kfr MARCH=portable
        check_outputs
        ;;
    asan)
        build_librtlsdr
        make -C "$CORE" clean
        make -C "$CORE" -j "$MAKE_JOBS" ENGINE=generic MARCH=portable BUILD=asan
        check_outputs
        ;;
    *)
        echo "Usage: ci/build.sh generic|kfr|asan" >&2
        exit 2
        ;;
esac
