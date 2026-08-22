# =============================================================================
# Makefile for compiling the HeIMDALL DAQ firmware core
# Authors: Tamás Pető, Sándor Bajusz
#
# Usage:
#   make [-jN]                  Build the core binaries:
#                               rtl_daq.out rebuffer.out decimate.out iq_server.out
#   make ENGINE=<engine>        FIR/convert engine: auto|kfr|ne10|generic
#                                 auto (default): x86_64 -> kfr, aarch64/arm64 -> kfr,
#                                                 arm (32-bit) -> ne10, unknown -> generic
#                                 generic: dependency-free plain-C engine
#                                          (no KFR / Ne10 library needed)
#   make BUILD=<profile>        release (default) | debug (-Og -g)
#                               | asan (-O1 -g -fsanitize=address,undefined)
#   make MARCH=<flags>          Architecture flags override. Default is the native
#                               tuning of the old Makefile (-march=native / -mcpu=native).
#                               MARCH=portable  -> no arch-specific flags (CI /
#                                                  distributable binaries)
#                               MARCH='-mcpu=cortex-a72' -> explicit flags
#   make all-offload            Additionally build all optional transport/offload objs
#   make libs                   Shared libraries for Python ctypes access
#   make pgo-profile            Instrumented build; run a workload, then:
#   make pgo-optimize           Rebuild using the recorded profile
#   make clean                  Remove build artifacts (keeps PGO .gcda profiles)
#   make distclean              clean + remove PGO profile data
#
# Dependency layout (unchanged): external libraries are copied INTO this
# directory before building and resolved via -I. -L. :
#   librtlsdr.a, rtl-sdr.h, rtl-sdr_export.h   (krakenrf fork - always required
#                                               for rtl_daq.out)
#   libkfr_capi.so + kfr/ headers              (ENGINE=kfr)
#   libNE10.a (NE10*.h are vendored)           (ENGINE=ne10, 32-bit ARM only)
# ENGINE=generic needs none of the DSP libraries.
# =============================================================================

# Plain assignment (not ?=) so the default stays gcc as before; override on
# the command line with `make CC=...` if needed.
CC = gcc
RM = rm -f

HOST_ARCH := $(shell uname -m)

BUILD  ?= release
ENGINE ?= auto

# Uncomment if you are using third party switches from ckoval on a Rpi device
# and have pigpio installed
#PIGPIO=-lpigpio -DUSEPIGPIO

# Uncomment if you installed GCC 11.2
#CC=/usr/local/bin/gcc

# ===== Engine selection (ENGINE=auto|kfr|ne10|generic) =====
# Note: Ne10 is 32-bit-ARM only. aarch64 defaults to KFR (the old aarch64
# Ne10 branch never linked - libNE10.a is armv7-only).
ifeq ($(ENGINE),auto)
  ifeq ($(HOST_ARCH),x86_64)
    ENGINE_RESOLVED := kfr
  else ifeq ($(HOST_ARCH),aarch64)
    ENGINE_RESOLVED := kfr
  else ifeq ($(HOST_ARCH),arm64)
    ENGINE_RESOLVED := kfr
  else ifneq (,$(findstring arm,$(HOST_ARCH)))
    ENGINE_RESOLVED := ne10
  else
    ENGINE_RESOLVED := generic
  endif
else
  ENGINE_RESOLVED := $(ENGINE)
endif

# ===== Architecture flags (MARCH override) =====
ifeq ($(origin MARCH),undefined)
  ifeq ($(HOST_ARCH),x86_64)
    ARCH_FLAGS := -march=native
  else ifeq ($(HOST_ARCH),aarch64)
    ARCH_FLAGS := -mcpu=native
  else ifeq ($(HOST_ARCH),arm64)
    ARCH_FLAGS := -mcpu=native
  else ifneq (,$(findstring arm,$(HOST_ARCH)))
    ARCH_FLAGS := -mcpu=cortex-a72 -mtune=cortex-a72
  else
    ARCH_FLAGS :=
  endif
else ifeq ($(MARCH),portable)
  ARCH_FLAGS :=
else
  ARCH_FLAGS := $(MARCH)
endif

# ===== Build profile (BUILD=release|debug|asan) =====
WARN_FLAGS := -Wall
BASE_FLAGS := -std=gnu99 -I.
LDFLAGS ?=

ifeq ($(BUILD),release)
  ifeq ($(HOST_ARCH),x86_64)
    OPT_FLAGS := -Ofast -ffast-math -funroll-loops -fomit-frame-pointer -DNDEBUG \
                 -flto -fno-signed-zeros -fno-trapping-math -fassociative-math \
                 -freciprocal-math
  else ifeq ($(HOST_ARCH),aarch64)
    OPT_FLAGS := -Ofast -ffast-math -funroll-loops -fomit-frame-pointer -DNDEBUG \
                 -flto -fno-signed-zeros -fno-trapping-math -fassociative-math \
                 -freciprocal-math
  else ifeq ($(HOST_ARCH),arm64)
    OPT_FLAGS := -Ofast -ffast-math -funroll-loops -fomit-frame-pointer -DNDEBUG \
                 -flto -fno-signed-zeros -fno-trapping-math -fassociative-math \
                 -freciprocal-math
  else ifneq (,$(findstring arm,$(HOST_ARCH)))
    OPT_FLAGS := -Ofast -ffast-math -funroll-loops -fomit-frame-pointer -DNDEBUG \
                 -fno-signed-zeros -fno-trapping-math -fassociative-math \
                 -freciprocal-math
  else
    OPT_FLAGS := -O3 -ffast-math -funroll-loops -fomit-frame-pointer -DNDEBUG
  endif
else ifeq ($(BUILD),debug)
  OPT_FLAGS := -Og -g
else ifeq ($(BUILD),asan)
  OPT_FLAGS := -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer
  LDFLAGS  += -fsanitize=address,undefined
else
  $(error Unknown BUILD '$(BUILD)' (use release, debug or asan))
endif

# ===== PGO (used internally by pgo-profile / pgo-optimize) =====
ifeq ($(PGO),generate)
  OPT_FLAGS += -fprofile-generate
  LDFLAGS   += -fprofile-generate
else ifeq ($(PGO),use)
  OPT_FLAGS += -fprofile-use -fprofile-correction
  LDFLAGS   += -fprofile-use
endif

# ===== Engine objects / defines / libraries =====
# kfr / ne10 lanes also compile the dependency-free generic engine
# (-DHAS_GENERIC_OFFLOAD) so [offload] fir_engine=generic is selectable at
# runtime; auto-detection still picks the platform engine, so default runtime
# behavior is unchanged.
ifeq ($(ENGINE_RESOLVED),kfr)
  ENGINE_CFLAGS := -DHAS_GENERIC_OFFLOAD
  ENGINE_OBJS   := offload_cpu_kfr.o offload_cpu_generic.o
  # -rpath $$ORIGIN lets decimate.out find the copied-in libkfr_capi.so next
  # to the binary without LD_LIBRARY_PATH; system-installed KFR also works.
  ENGINE_LIBS   := -L. -lkfr_capi -Wl,-rpath,'$$ORIGIN' -lm
else ifeq ($(ENGINE_RESOLVED),ne10)
  ENGINE_CFLAGS := -DARM_NEON -DENABLE_NE10_FIR_DECIMATE_FLOAT_NEON -DHAS_GENERIC_OFFLOAD
  ENGINE_OBJS   := offload_cpu_neon.o offload_cpu_generic.o
  ENGINE_LIBS   := -L. -lNE10 -lm
else ifeq ($(ENGINE_RESOLVED),generic)
  # OFFLOAD_GENERIC_ONLY: offload_cpu_kfr.c / offload_cpu_neon.c self-exclude,
  # the generic engine becomes the auto-detect default. No DSP library needed.
  ENGINE_CFLAGS := -DOFFLOAD_GENERIC_ONLY
  ENGINE_OBJS   := offload_cpu_generic.o
  ENGINE_LIBS   := -lm
else
  $(error Unknown ENGINE '$(ENGINE)' (use auto, kfr, ne10 or generic))
endif

# ===== Object groups =====
COMMON_OBJS    := log.o ini.o iq_header.o sh_mem_util.o
TRANSPORT_OBJS := transport.o transport_shm.o
OFFLOAD_OBJS   := offload.o $(ENGINE_OBJS)

# ===== Optional transport/offload drivers =====
# Uncomment to enable FPGA HAT support
#TRANSPORT_OBJS += transport_spi.o
#OFFLOAD_OBJS += offload_fpga.o
#EXTRA_CFLAGS += -DHAS_SPI_TRANSPORT -DHAS_FPGA_OFFLOAD

# Uncomment to enable GPU offload
#OFFLOAD_OBJS += offload_gpu.o
#EXTRA_CFLAGS += -DHAS_GPU_OFFLOAD

# Uncomment to enable PCIe transport
#TRANSPORT_OBJS += transport_pcie.o
#EXTRA_CFLAGS += -DHAS_PCIE_TRANSPORT

# Uncomment to enable USB3 transport
#TRANSPORT_OBJS += transport_usb3.o
#EXTRA_CFLAGS += -DHAS_USB3_TRANSPORT

# Uncomment to enable Network transport
#TRANSPORT_OBJS += transport_net.o
#EXTRA_CFLAGS += -DHAS_NET_TRANSPORT

CFLAGS = $(WARN_FLAGS) $(BASE_FLAGS) $(ARCH_FLAGS) $(OPT_FLAGS) $(ENGINE_CFLAGS) $(EXTRA_CFLAGS)

# ===== Build Targets =====

CORE_BINS := rtl_daq.out rebuffer.out decimate.out iq_server.out

all: $(CORE_BINS)

$(info [heimdall make] arch=$(HOST_ARCH) engine=$(ENGINE_RESOLVED) build=$(BUILD) archflags='$(ARCH_FLAGS)')

# Pattern rule with header dependency tracking (parallel-safe)
%.o: %.c
	$(CC) $(CFLAGS) -MMD -MP -c $< -o $@

-include $(wildcard *.d)

rtl_daq.out: rtl_daq.o $(COMMON_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ -o $@ -lpthread -lzmq $(PIGPIO) -L. -lrtlsdr -lusb-1.0 -lm -lrt

rebuffer.out: rebuffer.o $(TRANSPORT_OBJS) $(COMMON_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ -o $@ -lrt -lm

decimate.out: fir_decimate.o $(TRANSPORT_OBJS) $(OFFLOAD_OBJS) $(COMMON_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ -o $@ -lrt $(ENGINE_LIBS)

iq_server.out: iq_server.o $(TRANSPORT_OBJS) $(COMMON_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ -o $@ -lrt -lm

# ---- Legacy target-name aliases (kept for scripts/docs compatibility) ----
rtl_daq: rtl_daq.out
rebuffer: rebuffer.out
decimator: decimate.out
decimate: decimate.out
iq_server: iq_server.out
daq_util: $(COMMON_OBJS)
transport_objs: $(TRANSPORT_OBJS)
offload_objs: $(OFFLOAD_OBJS)

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
	$(CC) $(CFLAGS) -DHAS_FPGA_OFFLOAD -c offload_fpga.c -o offload_fpga.o

# GPU offload engine
offload_gpu: offload_gpu.c
	$(CC) $(CFLAGS) -DHAS_GPU_OFFLOAD -c offload_gpu.c -o offload_gpu.o

# USB3 bridge process
hat_usb3_bridge: hat_usb3_bridge.c log.o ini.o iq_header.o
	$(CC) $(CFLAGS) log.o ini.o iq_header.o -o hat_usb3_bridge.out hat_usb3_bridge.c -lpthread -lzmq -lusb-1.0

# PCIe bridge process
hat_pcie_bridge: hat_pcie_bridge.c log.o ini.o iq_header.o
	$(CC) $(CFLAGS) log.o ini.o iq_header.o -o hat_pcie_bridge.out hat_pcie_bridge.c -lpthread

# Shared libraries for Python ctypes access
libs: libfpga_hal.so libdma_hal.so

libfpga_hal.so: offload_fpga.c transport_spi.c hat_gpio_ctrl.c log.c
	$(CC) $(CFLAGS) -shared -fPIC -o libfpga_hal.so offload_fpga.c transport_spi.c hat_gpio_ctrl.c log.c

libdma_hal.so: bcm2711_dma.c
	$(CC) $(CFLAGS) -shared -fPIC -o libdma_hal.so bcm2711_dma.c

# Build all offload/transport components
all-offload: all transport_spi transport_usb3 transport_pcie transport_net dma_engine gpio_ctrl offload_fpga offload_gpu

# Profile-Guided Optimization targets
pgo-profile:
	$(MAKE) clean
	$(MAKE) PGO=generate all
	@echo "Run benchmark workload now, then execute 'make pgo-optimize'"
	@echo "Example: cd ../.. && sudo ./benchmark_workload.sh"

pgo-optimize:
	$(MAKE) clean
	$(MAKE) PGO=use all
	@echo "PGO-optimized binaries built"

# NOTE: clean intentionally keeps *.gcda (runtime PGO profiles) so the
# pgo-profile -> run -> pgo-optimize flow survives the intermediate clean.
# Globs are restricted so the copied-in vendor files (librtlsdr.a, libNE10.a,
# libkfr_capi.so, kfr/) are never removed.
clean:
	$(RM) *.o *.d \
	      rtl_daq.out rebuffer.out decimate.out iq_server.out \
	      hat_usb3_bridge.out hat_pcie_bridge.out \
	      libfpga_hal.so libdma_hal.so

distclean: clean
	$(RM) *.gcda *.gcno

.PHONY: all clean distclean libs all-offload pgo-profile pgo-optimize \
        rtl_daq rebuffer decimator decimate iq_server \
        daq_util transport_objs offload_objs \
        transport_spi transport_usb3 transport_pcie transport_net \
        dma_engine gpio_ctrl offload_fpga offload_gpu \
        hat_usb3_bridge hat_pcie_bridge
