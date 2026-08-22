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
set -e

echo "Installing dependencies and build HeIMDALL DAQ Firmware"

# Resolve the repository root from this script's location instead of relying
# on a hardcoded checkout directory name.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DAQ_CORE="$REPO_ROOT/Firmware/_daq_core"
BUILD_ROOT="${BUILD_ROOT:-$REPO_ROOT/..}"
KFR_REF="${KFR_REF:-7.1.0}"

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
        git clone --depth 1 --branch "$KFR_REF" https://github.com/kfrlib/kfr
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

# Configure system limits for real-time audio group
if ! grep -q "@audio.*rtprio" /etc/security/limits.conf; then
    echo "@audio - rtprio 95" | sudo tee -a /etc/security/limits.conf
    echo "@audio - memlock unlimited" | sudo tee -a /etc/security/limits.conf
    echo "@audio - nice -19" | sudo tee -a /etc/security/limits.conf
    echo "Added real-time limits configuration"
fi

# Copy kernel tuning parameters
if [ -f "$REPO_ROOT/util/kernel_tuning.conf" ]; then
    sudo cp "$REPO_ROOT/util/kernel_tuning.conf" /etc/sysctl.d/99-heimdall-rt.conf
    echo "Applied kernel tuning parameters (will take effect after reboot)"
fi

# Set up user in audio group for real-time privileges
sudo usermod -a -G audio "$USER"
echo "Added $USER to audio group for RT privileges"

echo "6/8 Build HeIMDALL DAQ Firmware with optimizations"
cd "$DAQ_CORE"
make clean  # Clean build with new optimization flags
if [ "$HOST_ARCH" = "x86_64" ] || [ "$HOST_ARCH" = "aarch64" ] || [ "$HOST_ARCH" = "armv7l" ]; then
    make -j"$(nproc)"
else
    make -j"$(nproc)" ENGINE=generic
fi

echo ""
echo "Installation complete!"
echo "===================="
echo ""
echo "Next steps:"
echo "1. Log out and back in to activate group membership"
echo "2. Reboot to apply kernel parameters: sudo reboot"
echo "3. Run system optimization: sudo python3 ../util/system_tuning.py --full"
echo "4. Test with: ./daq_synthetic_start.sh"
echo ""
echo "Performance monitoring:"
echo "- Check CPU affinity: python3 ../util/performance_monitor.py --check-affinity"
echo "- Monitor performance: python3 ../util/performance_monitor.py"
echo "- Run benchmarks: ./benchmark_workload.sh"

# TODO: Check installed versions:
# Scipy: 1.8 or later
