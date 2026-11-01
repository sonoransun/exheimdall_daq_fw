#!/bin/bash
#
#   HeIMDALL DAQ Firmware installer: dependencies + build
#
#   Project : HeIMDALL DAQ Firmware
#   License : GNU GPL V3
#
#   Supported architectures:
#     x86_64  -> KFR capi built from source (FIR engine)
#     aarch64 -> KFR capi built from source (FIR engine; Ne10 is 32-bit only)
#     armv7l  -> Ne10 built from source (NEON FIR engine)
#
#   Usage: util/install.sh [--federation-user] [--udev] [--extras-only]
#     (no flag)          dependencies + librtlsdr + DSP library + python
#                        packages + kernel tuning file + C build
#     --federation-user  also create the unprivileged "heimdall" system user
#                        and group that heimdall-coordinator.service and
#                        heimdall-iq-router.service run as (idempotent)
#     --udev             also install packaging/udev/99-heimdall-rtlsdr.rules
#                        (RTL2832U access for the plugdev group) and reload udev
#     --extras-only      run only the steps the flags above select (no apt,
#                        no source builds), e.g. on an installed host:
#                        sudo util/install.sh --extras-only --federation-user
#
#   Environment overrides (unset = the default behaviour):
#     BUILD_ROOT     where librtlsdr/kfr/Ne10 are cloned (default: repo/..)
#     LIBRTLSDR_REF  git ref of krakenrf/librtlsdr to build (default: the
#                    clone's checked-out branch). A branch, tag or commit sha;
#                    it is also checked out in an existing clone.
#     KFR_REF        git ref of kfrlib/kfr to build (default: 7.1.0). A branch,
#                    tag or commit sha; when set, it is also checked out in an
#                    existing git clone (unset: an existing kfr is used as is).
set -e

usage() {
    if [ "${1:-2}" -eq 0 ]; then
        sed -n '/^#   Usage:/,/^#     KFR_REF/s/^#//p' "$0"
    else
        sed -n '/^#   Usage:/,/^#     KFR_REF/s/^#//p' "$0" >&2
    fi
    exit "${1:-2}"
}

OPT_FED_USER=0
OPT_UDEV=0
OPT_EXTRAS_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --federation-user) OPT_FED_USER=1 ;;
        --udev) OPT_UDEV=1 ;;
        --extras-only) OPT_EXTRAS_ONLY=1 ;;
        -h|--help) usage 0 ;;
        *) echo "install.sh: unknown argument '$arg'" >&2; usage 2 ;;
    esac
done
if [ "$OPT_EXTRAS_ONLY" = 1 ] && [ "$OPT_FED_USER" = 0 ] && [ "$OPT_UDEV" = 0 ]; then
    echo "install.sh: --extras-only needs --federation-user and/or --udev" >&2
    exit 2
fi

# Resolve the repository root from this script's location instead of relying
# on a hardcoded checkout directory name.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DAQ_CORE="$REPO_ROOT/Firmware/_daq_core"
BUILD_ROOT="${BUILD_ROOT:-$REPO_ROOT/..}"
# KFR_REF_ENV remembers whether the caller chose a ref: only then is an
# existing kfr clone moved to it (unset keeps the existing tree as is).
KFR_REF_ENV="${KFR_REF:-}"
KFR_REF="${KFR_REF:-7.1.0}"
LIBRTLSDR_REF="${LIBRTLSDR_REF:-}"
FED_USER=heimdall
UDEV_RULE="$REPO_ROOT/packaging/udev/99-heimdall-rtlsdr.rules"

# as_root CMD...: run CMD as root (directly when already root, else sudo)
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Create the federation service user and group (spec H.5); idempotent.
install_federation_user() {
    if getent group "$FED_USER" >/dev/null 2>&1; then
        echo "group $FED_USER exists"
    else
        as_root groupadd --system "$FED_USER"
        echo "created system group $FED_USER"
    fi
    if getent passwd "$FED_USER" >/dev/null 2>&1; then
        echo "user $FED_USER exists"
    else
        as_root useradd --system --no-create-home --shell /usr/sbin/nologin \
            --gid "$FED_USER" "$FED_USER"
        echo "created system user $FED_USER (no home, no login shell)"
    fi
}

# Install the optional RTL2832U udev rule and reload udev.
install_udev_rule() {
    as_root install -m 0644 "$UDEV_RULE" /etc/udev/rules.d/99-heimdall-rtlsdr.rules
    echo "installed /etc/udev/rules.d/99-heimdall-rtlsdr.rules"
    if command -v udevadm >/dev/null 2>&1; then
        as_root udevadm control --reload-rules || echo "Warning: udevadm control --reload-rules failed"
        as_root udevadm trigger --subsystem-match=usb || echo "Warning: udevadm trigger failed"
    else
        echo "Warning: udevadm not found; the rule applies after the next boot or replug"
    fi
}

install_extras() {
    if [ "$OPT_FED_USER" = 1 ]; then
        echo "Create the federation service user"
        install_federation_user
    fi
    if [ "$OPT_UDEV" = 1 ]; then
        echo "Install the RTL2832U udev rule"
        install_udev_rule
    fi
}

if [ "$OPT_EXTRAS_ONLY" = 1 ]; then
    install_extras
    exit 0
fi

# is_sha REF: a commit sha (7-40 hex digits) - cannot be cloned with --branch
is_sha() {
    case "$1" in
        *[!0-9a-fA-F]*) return 1 ;;
    esac
    [ "${#1}" -ge 7 ] && [ "${#1}" -le 40 ]
}

# checkout_ref DIR REF: check REF out in the clone DIR (fetching it when the
# clone does not have it yet)
checkout_ref() {
    echo "Checking out $2 in $1"
    git -C "$1" -c advice.detachedHead=false checkout -q "$2" 2>/dev/null || {
        git -C "$1" fetch -q origin "$2"
        git -C "$1" -c advice.detachedHead=false checkout -q FETCH_HEAD
    }
}

echo "Installing dependencies and build HeIMDALL DAQ Firmware"

# pip on Debian/Ubuntu >= 23.04 refuses system-wide installs (PEP 668);
# fall back to --break-system-packages there.
pip_install() {
    sudo python3 -m pip install "$@" \
        || sudo python3 -m pip install --break-system-packages "$@"
}

echo "6/1 Install build dependencies"
sudo apt-get update
sudo apt-get install -y git cmake build-essential libusb-1.0-0-dev libzmq3-dev lsof

echo "6/2 Build and install rtl-sdr driver (krakenrf fork)"
cd "$BUILD_ROOT"
if [ ! -d librtlsdr ]; then
    git clone https://github.com/krakenrf/librtlsdr
fi
if [ -n "$LIBRTLSDR_REF" ]; then
    checkout_ref librtlsdr "$LIBRTLSDR_REF"
fi
cd librtlsdr
mkdir -p build
cd build
cmake ../ -DINSTALL_UDEV_RULES=ON
make -j"$(nproc)"
sudo make install
sudo cp ../rtl-sdr.rules /etc/udev/rules.d/
sudo ldconfig
# Copy the static lib + headers into _daq_core (the Makefile resolves them
# via -I. -L. per the copy-in dependency layout, see CLAUDE.md)
cp src/librtlsdr.a "$DAQ_CORE/"
cp ../include/rtl-sdr.h ../include/rtl-sdr_export.h "$DAQ_CORE/"
cd "$BUILD_ROOT"

echo "6/3 Disable built-in rtl-sdr driver"
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-dvb_usb_rtl28xxu.conf

echo "6/4 Install SIMD FIR filter DSP library"
HOST_ARCH=$(uname -m)
if [ "$HOST_ARCH" = "x86_64" ] || [ "$HOST_ARCH" = "aarch64" ]; then
    # KFR capi (requires clang). PIC is mandatory on arm64 and harmless on x86.
    echo "Building KFR $KFR_REF for $HOST_ARCH"
    sudo apt-get install -y clang
    if [ ! -d kfr ]; then
        if is_sha "$KFR_REF"; then
            git clone https://github.com/kfrlib/kfr
            checkout_ref kfr "$KFR_REF"
        else
            git clone --depth 1 --branch "$KFR_REF" https://github.com/kfrlib/kfr
        fi
    elif [ -n "${KFR_REF_ENV:-}" ]; then
        if [ -e kfr/.git ]; then
            checkout_ref kfr "$KFR_REF"
        else
            echo "Warning: kfr is not a git clone; building it as is (KFR_REF ignored)" >&2
        fi
    fi
    cmake -S kfr -B kfr/build \
          -DCMAKE_BUILD_TYPE=Release \
          -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
          -DKFR_ENABLE_CAPI_BUILD=ON \
          -DKFR_ENABLE_DFT=ON \
          -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
    cmake --build kfr/build --target kfr_capi -j"$(nproc)"
    cp kfr/build/lib/libkfr_capi.so* "$DAQ_CORE/"
    rm -rf "$DAQ_CORE/kfr"
    cp -r kfr/include/kfr "$DAQ_CORE/kfr"
elif [ "$HOST_ARCH" = "armv7l" ]; then
    if [ ! -d Ne10 ]; then
        git clone https://github.com/projectNe10/Ne10
    fi
    cd Ne10
    mkdir -p build
    cd build
    export NE10_LINUX_TARGET_ARCH=armv7
    cmake -DGNULINUX_PLATFORM=ON ..
    make -j"$(nproc)"
    cp modules/libNE10.a "$DAQ_CORE/"
    cd "$BUILD_ROOT"
else
    echo "Architecture '$HOST_ARCH' not recognized - building with the"
    echo "dependency-free generic FIR engine (make ENGINE=generic)."
fi

echo "6/5 Install the required python3 packages"
sudo apt-get install -y python3-pip libatlas-base-dev gfortran
pip_install numpy
pip_install configparser
pip_install scipy
pip_install pyzmq
pip_install scikit-rf
# For testing
pip_install plotly || echo "Warning: plotly not installed (optional, testing only)"

echo "6/6 Install performance optimization tools"
# Real-time performance tools
sudo apt-get install -y linux-tools-generic numactl stress-ng || \
    echo "Warning: some performance tools not installed (optional)"

# Python packages for monitoring
pip_install psutil
# Optional: Berkeley DB for advanced features
pip_install berkeleydb || echo "Warning: berkeleydb not installed (optional)"

echo "6/7 Configure system for real-time performance"

# No /etc/security/limits.conf entries: the chain runs as root (the start
# scripts use sudo, the systemd units set LimitRTPRIO/LimitMEMLOCK), so
# group rtprio/memlock limits never applied to it.

# Copy kernel tuning parameters
if [ -f "$REPO_ROOT/util/kernel_tuning.conf" ]; then
    sudo cp "$REPO_ROOT/util/kernel_tuning.conf" /etc/sysctl.d/99-heimdall-rt.conf
    echo "Applied kernel tuning parameters (will take effect after reboot)"
fi

echo "6/8 Build HeIMDALL DAQ Firmware with optimizations"
cd "$DAQ_CORE"
make clean  # Clean build with new optimization flags
if [ "$HOST_ARCH" = "x86_64" ] || [ "$HOST_ARCH" = "aarch64" ] || [ "$HOST_ARCH" = "armv7l" ]; then
    make -j"$(nproc)"
else
    make -j"$(nproc)" ENGINE=generic
fi

install_extras

echo ""
echo "Installation complete!"
echo "===================="
echo ""
echo "Next steps:"
echo "1. Reboot to apply kernel parameters: sudo reboot"
echo "2. Run system optimization: sudo python3 ../util/system_tuning.py --full"
echo "3. Test with: ./daq_synthetic_start.sh"
echo "4. Run the chain under systemd: see packaging/systemd/README.md"
echo ""
echo "Performance monitoring:"
echo "- Check CPU affinity: python3 ../util/performance_monitor.py --check-affinity"
echo "- Monitor performance: python3 ../util/performance_monitor.py"
echo "- Run benchmarks: ./benchmark_workload.sh"

# TODO: Check installed versions:
# Scipy: 1.8 or later
