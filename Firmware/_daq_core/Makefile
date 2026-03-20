# Makefile for compiling the DAQ firmware
# Author: Tamás Pető, Sándor Bajusz
CC=gcc

# Performance-optimized flags based on architecture
HOST_ARCH := $(shell uname -m)

ifeq ($(HOST_ARCH), x86_64)
# x86_64 aggressive optimization
CFLAGS=-Wall -std=gnu99 -march=native -Ofast -ffast-math -funroll-loops \
       -fomit-frame-pointer -DNDEBUG -flto -fno-signed-zeros \
       -fno-trapping-math -fassociative-math -freciprocal-math -I.
else ifeq ($(HOST_ARCH), aarch64)
# ARM64 optimization (Apple Silicon, newer ARM)
CFLAGS=-Wall -std=gnu99 -mcpu=native -Ofast -ffast-math -funroll-loops \
       -fomit-frame-pointer -DNDEBUG -flto -fno-signed-zeros \
       -fno-trapping-math -fassociative-math -freciprocal-math -I.
else ifeq ($(HOST_ARCH), arm64)
# ARM64 optimization (macOS naming)
CFLAGS=-Wall -std=gnu99 -mcpu=native -Ofast -ffast-math -funroll-loops \
       -fomit-frame-pointer -DNDEBUG -flto -fno-signed-zeros \
       -fno-trapping-math -fassociative-math -freciprocal-math -I.
else ifneq (,$(findstring arm,$(HOST_ARCH)))
# ARM optimization (Raspberry Pi 4 and similar)
CFLAGS=-Wall -std=gnu99 -mcpu=cortex-a72 -mtune=cortex-a72 -Ofast \
       -ffast-math -funroll-loops -fomit-frame-pointer -DNDEBUG \
       -fno-signed-zeros -fno-trapping-math -fassociative-math \
       -freciprocal-math -I.
else
# Generic fallback
CFLAGS=-Wall -std=gnu99 -O3 -ffast-math -funroll-loops \
       -fomit-frame-pointer -DNDEBUG -I.
endif
# Uncomment if you are using third party switches from ckoval on a Rpi device and have pigpio installed
#PIGPIO=-lpigpio -DUSEPIGPIO

# Uncomment if you installed GCC 11.2
#CC=/usr/local/bin/gcc
RM= rm -f

HOST_ARCH := $(shell uname -m)

# ===== Transport and Offload Object Groups =====
TRANSPORT_OBJS = transport.o transport_shm.o
OFFLOAD_OBJS = offload.o

# Platform-specific offload engines
ifeq ($(HOST_ARCH), x86_64)
OFFLOAD_OBJS += offload_cpu_kfr.o
PLATFORM_LIBS = -lkfr_capi
else
OFFLOAD_OBJS += offload_cpu_neon.o
PLATFORM_LIBS = -L. -lNE10 -lm
NEON_FLAG = -DARM_NEON
endif

# ===== Optional transport/offload drivers =====
# Uncomment to enable FPGA HAT support
#TRANSPORT_OBJS += transport_spi.o
#OFFLOAD_OBJS += offload_fpga.o
#CFLAGS += -DHAS_SPI_TRANSPORT -DHAS_FPGA_OFFLOAD

# Uncomment to enable GPU offload
#OFFLOAD_OBJS += offload_gpu.o
#CFLAGS += -DHAS_GPU_OFFLOAD

# Uncomment to enable PCIe transport
#TRANSPORT_OBJS += transport_pcie.o
#CFLAGS += -DHAS_PCIE_TRANSPORT

# Uncomment to enable USB3 transport
#TRANSPORT_OBJS += transport_usb3.o
#CFLAGS += -DHAS_USB3_TRANSPORT

# Uncomment to enable Network transport
#TRANSPORT_OBJS += transport_net.o
#CFLAGS += -DHAS_NET_TRANSPORT

# ===== Build Targets =====

all: daq_util transport_objs offload_objs rtl_daq rebuffer iq_server decimator

daq_util:
	$(CC) $(CFLAGS) -c -o ini.o ini.c
	$(CC) $(CFLAGS) -c -o log.o log.c
	$(CC) $(CFLAGS) -c -o iq_header.o iq_header.c
	$(CC) $(CFLAGS) -c -o sh_mem_util.o sh_mem_util.c

transport_objs: daq_util
	$(CC) $(CFLAGS) -c -o transport.o transport.c
	$(CC) $(CFLAGS) -c -o transport_shm.o transport_shm.c

offload_objs: daq_util
ifeq ($(HOST_ARCH), x86_64)
	$(CC) $(CFLAGS) -c -o offload.o offload.c
	$(CC) $(CFLAGS) -c -o offload_cpu_kfr.o offload_cpu_kfr.c
else
	$(CC) $(CFLAGS) $(NEON_FLAG) -c -o offload.o offload.c
	$(CC) $(CFLAGS) $(NEON_FLAG) -c -o offload_cpu_neon.o offload_cpu_neon.c
endif

rtl_daq: iq_header.c log.c ini.c rtl_daq.c rtl_daq.h
	$(CC) $(CFLAGS) log.o ini.o iq_header.o -o rtl_daq.out rtl_daq.c -lpthread -lzmq $(PIGPIO) -L. -lrtlsdr -lusb-1.0

rebuffer: sh_mem_util.c iq_header.c log.c ini.c rebuffer.c rtl_daq.h transport.h
	$(CC) $(CFLAGS) $(TRANSPORT_OBJS) sh_mem_util.o log.o ini.o iq_header.o -o rebuffer.out rebuffer.c -lrt -lm

decimator: transport_objs offload_objs
ifeq ($(HOST_ARCH), x86_64)
	$(CC) $(CFLAGS) -c fir_decimate.c -o fir_decimate.o
	$(CC) $(CFLAGS) fir_decimate.o $(TRANSPORT_OBJS) $(OFFLOAD_OBJS) sh_mem_util.o log.o ini.o iq_header.o -o decimate.out -lrt $(PLATFORM_LIBS)
else
	$(CC) $(CFLAGS) $(NEON_FLAG) -c fir_decimate.c -o fir_decimate.o
	$(CC) $(CFLAGS) fir_decimate.o $(TRANSPORT_OBJS) $(OFFLOAD_OBJS) sh_mem_util.o log.o ini.o iq_header.o -o decimate.out -lrt $(PLATFORM_LIBS)
endif

iq_server: sh_mem_util.c iq_header.c log.c ini.c iq_server.c transport.h
	$(CC) $(CFLAGS) $(TRANSPORT_OBJS) sh_mem_util.o log.o ini.o iq_header.o -o iq_server.out iq_server.c -lrt

# ===== Optional Targets =====

# FPGA HAT SPI transport driver
transport_spi: transport_spi.c
	$(CC) $(CFLAGS) -DHAS_SPI_TRANSPORT -c transport_spi.c -o transport_spi.o

# USB3 transport driver
transport_usb3: transport_usb3.c
	$(CC) $(CFLAGS) -DHAS_USB3_TRANSPORT -c transport_usb3.c -o transport_usb3.o

# PCIe transport driver
transport_pcie: transport_pcie.c
	$(CC) $(CFLAGS) -DHAS_PCIE_TRANSPORT -c transport_pcie.c -o transport_pcie.o

# Network transport driver
transport_net: transport_net.c
	$(CC) $(CFLAGS) -DHAS_NET_TRANSPORT -c transport_net.c -o transport_net.o

# DMA engine driver
dma_engine: bcm2711_dma.c bcm2711_dma.h
	$(CC) $(CFLAGS) -c bcm2711_dma.c -o bcm2711_dma.o

# GPIO HAT controller
gpio_ctrl: hat_gpio_ctrl.c hat_gpio_ctrl.h
	$(CC) $(CFLAGS) -c hat_gpio_ctrl.c -o hat_gpio_ctrl.o

# FPGA offload engine
offload_fpga: offload_fpga.c
	$(CC) $(CFLAGS) $(NEON_FLAG) -DHAS_FPGA_OFFLOAD -c offload_fpga.c -o offload_fpga.o

# GPU offload engine
offload_gpu: offload_gpu.c
	$(CC) $(CFLAGS) $(NEON_FLAG) -DHAS_GPU_OFFLOAD -c offload_gpu.c -o offload_gpu.o

# USB3 bridge process
hat_usb3_bridge: hat_usb3_bridge.c
	$(CC) $(CFLAGS) log.o ini.o iq_header.o -o hat_usb3_bridge.out hat_usb3_bridge.c -lpthread -lzmq -lusb-1.0

# PCIe bridge process
hat_pcie_bridge: hat_pcie_bridge.c
	$(CC) $(CFLAGS) log.o ini.o iq_header.o -o hat_pcie_bridge.out hat_pcie_bridge.c -lpthread

# Shared libraries for Python ctypes access
libs: libfpga_hal.so libdma_hal.so

libfpga_hal.so: offload_fpga.c transport_spi.c hat_gpio_ctrl.c
	$(CC) $(CFLAGS) -shared -fPIC -o libfpga_hal.so offload_fpga.c transport_spi.c hat_gpio_ctrl.c log.c

libdma_hal.so: bcm2711_dma.c
	$(CC) $(CFLAGS) -shared -fPIC -o libdma_hal.so bcm2711_dma.c

# Build all offload/transport components
all-offload: all transport_spi transport_usb3 transport_pcie transport_net dma_engine gpio_ctrl offload_fpga offload_gpu

# Profile-Guided Optimization targets
pgo-profile:
	$(MAKE) CFLAGS="$(CFLAGS) -fprofile-generate" clean all
	@echo "Run benchmark workload now, then execute 'make pgo-optimize'"
	@echo "Example: cd ../.. && sudo ./benchmark_workload.sh"

pgo-optimize:
	$(MAKE) CFLAGS="$(CFLAGS) -fprofile-use" clean all
	@echo "PGO-optimized binaries built"

clean:
	$(RM) ini.o log.o iq_header.o sh_mem_util.o fir_decimate.o decimate.o \
	      transport.o transport_shm.o transport_spi.o transport_usb3.o \
	      transport_pcie.o transport_net.o offload.o offload_cpu_neon.o \
	      offload_cpu_kfr.o offload_fpga.o offload_gpu.o bcm2711_dma.o \
	      hat_gpio_ctrl.o \
	      rtl_daq.out rebuffer.out decimate.out iq_server.out \
	      hat_usb3_bridge.out hat_pcie_bridge.out \
	      libfpga_hal.so libdma_hal.so
