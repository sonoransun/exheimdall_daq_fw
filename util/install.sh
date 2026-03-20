#!/bin/bash
echo "Installing dependencies and build HeIMDALL DAQ Firmware"
cd ..
cd ..
sudo apt install git
echo "6/1 Install build dependencies for the realtek driver"
sudo apt install cmake
sudo apt install libusb-1.0-0-dev
echo "6/2 Build and install rtl-sdr driver"
git clone https://github.com/krakenrf/librtlsdr

cd librtlsdr
mkdir build
cd build
cmake ../ -DINSTALL_UDEV_RULES=ON
make
sudo make install
sudo cp ../rtl-sdr.rules /etc/udev/rules.d/
sudo ldconfig
cd ..
cd ..

echo "6/3 Disable built-in rtl-sdr driver"
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee --append /etc/modprobe.d/blacklist-dvb_usb_rtl28xxu.conf
echo "6/4 Install SIMD FIR filter DSP library"

HOST_ARCH=$(uname -m)
if [ "$HOST_ARCH" = "x86_64" ]; then
    echo "X86 64 platform."
elif [ "$HOST_ARCH" = "armv7l" ]; then
    git clone https://github.com/projectNe10/Ne10
    cd Ne10
    mkdir build
    cd build
    export NE10_LINUX_TARGET_ARCH=armv7 
    cmake -DGNULINUX_PLATFORM=ON ..     
    make
    cp modules/libNE10.a ../../heimdall_daq_fw/Firmware/_daq_core
    cd ..
    cd ..    
else
    echo "Architecture not recognized!"
    exit
fi
echo "6/5 Install the required python3 packages"
sudo apt install python3-pip
sudo python3 -m pip install numpy
sudo python3 -m pip install configparser
sudo apt-get install libatlas-base-dev gfortran
sudo python3 -m pip install scipy
sudo python3 -m pip install pyzmq
sudo python3 -m pip install scikit-rf
# For testing
sudo python3 -m pip install plotly


sudo apt install libzmq3-dev -y

echo "6/6 Install performance optimization tools"
# Real-time performance tools
sudo apt install linux-tools-generic cpuset-tools numactl stress-ng -y

# Python packages for monitoring
sudo python3 -m pip install psutil
# Optional: Berkeley DB for advanced features
sudo python3 -m pip install berkeleydb || echo "Warning: berkeleydb not installed (optional)"

echo "6/7 Configure system for real-time performance"

# Configure system limits for real-time audio group
if ! grep -q "@audio.*rtprio" /etc/security/limits.conf; then
    echo "@audio - rtprio 95" | sudo tee -a /etc/security/limits.conf
    echo "@audio - memlock unlimited" | sudo tee -a /etc/security/limits.conf
    echo "@audio - nice -19" | sudo tee -a /etc/security/limits.conf
    echo "Added real-time limits configuration"
fi

# Copy kernel tuning parameters
if [ -f "kernel_tuning.conf" ]; then
    sudo cp kernel_tuning.conf /etc/sysctl.d/99-heimdall-rt.conf
    echo "Applied kernel tuning parameters (will take effect after reboot)"
fi

# Set up user in audio group for real-time privileges
sudo usermod -a -G audio $USER
echo "Added $USER to audio group for RT privileges"

echo "6/8 Build HeIMDALL DAQ Firmware with optimizations"
cd heimdall_daq_fw/Firmware/_daq_core
make clean  # Clean build with new optimization flags
make

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