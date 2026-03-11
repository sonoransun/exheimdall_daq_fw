# =============================================================================
# Makefile — HeIMDALL DAQ FPGA signal processing offload gateware
#
# Targets:
#   synth      — Synthesize with Yosys (ECP5)
#   pnr        — Place and route with nextpnr-ecp5
#   bitstream  — Generate bitstream (.bit) for programming
#   sim        — Run top-level simulation (Icarus Verilog)
#   sim_spi    — Run SPI slave simulation
#   sim_fir    — Run FIR decimation simulation
#   sim_xcorr  — Run cross-correlation simulation
#   verify     — Run Python verification against simulation output
#   gen_vectors— Generate test vectors with Python
#   clean      — Remove build artifacts
# =============================================================================

# --- Tool configuration ---
YOSYS       ?= yosys
NEXTPNR     ?= nextpnr-ecp5
ECPPACK     ?= ecppack
IVERILOG    ?= iverilog
VVP         ?= vvp
PYTHON      ?= python3

# --- Device configuration ---
ECP5_DEVICE ?= 85k
ECP5_PACKAGE ?= CABGA381
ECP5_SPEED  ?= 6

# --- Directories ---
RTL_DIR     = rtl
TB_DIR      = tb
REF_DIR     = tb/reference
CONS_DIR    = constraints
BUILD_DIR   = build

# --- Source files ---
RTL_SRCS    = $(RTL_DIR)/top.v \
              $(RTL_DIR)/spi_slave.v \
              $(RTL_DIR)/iq_deinterleave.v \
              $(RTL_DIR)/u8_to_f32.v \
              $(RTL_DIR)/fir_decimate.v \
              $(RTL_DIR)/fir_mac.v \
              $(RTL_DIR)/xcorr_engine.v \
              $(RTL_DIR)/fft_radix2.v \
              $(RTL_DIR)/output_reinterleave.v \
              $(RTL_DIR)/config_regs.v \
              $(RTL_DIR)/fifo_sync.v \
              $(RTL_DIR)/id_register.v

# --- Constraint files ---
LPF_FILE    = $(CONS_DIR)/ecp5_ulx3s.lpf

# --- Build artifacts ---
JSON_FILE   = $(BUILD_DIR)/heimdall_fpga.json
ASC_FILE    = $(BUILD_DIR)/heimdall_fpga.config
BIT_FILE    = $(BUILD_DIR)/heimdall_fpga.bit

# =============================================================================
# Default target
# =============================================================================
.PHONY: all
all: bitstream

# =============================================================================
# Synthesis (Yosys)
# =============================================================================
.PHONY: synth
synth: $(JSON_FILE)

$(JSON_FILE): $(RTL_SRCS) | $(BUILD_DIR)
	$(YOSYS) -p " \
		read_verilog -DSYNTHESIS $(RTL_SRCS); \
		synth_ecp5 -top top -json $@"

# =============================================================================
# Place and Route (nextpnr)
# =============================================================================
.PHONY: pnr
pnr: $(ASC_FILE)

$(ASC_FILE): $(JSON_FILE) $(LPF_FILE) | $(BUILD_DIR)
	$(NEXTPNR) \
		--$(ECP5_DEVICE) \
		--package $(ECP5_PACKAGE) \
		--speed $(ECP5_SPEED) \
		--json $(JSON_FILE) \
		--lpf $(LPF_FILE) \
		--textcfg $@ \
		--freq 100

# =============================================================================
# Bitstream generation (ecppack)
# =============================================================================
.PHONY: bitstream
bitstream: $(BIT_FILE)

$(BIT_FILE): $(ASC_FILE) | $(BUILD_DIR)
	$(ECPPACK) --input $< --bit $@
	@echo "Bitstream ready: $@"
	@ls -la $@

# =============================================================================
# Simulation — Top-level testbench
# =============================================================================
.PHONY: sim
sim: gen_vectors $(BUILD_DIR)/tb_top.vvp
	cd $(TB_DIR) && $(VVP) ../$(BUILD_DIR)/tb_top.vvp

$(BUILD_DIR)/tb_top.vvp: $(TB_DIR)/tb_top.v $(RTL_SRCS) | $(BUILD_DIR)
	$(IVERILOG) -DSIMULATION -DVCD_OUTPUT \
		-o $@ \
		-I $(RTL_DIR) \
		$(TB_DIR)/tb_top.v $(RTL_SRCS)

# =============================================================================
# Simulation — SPI slave testbench
# =============================================================================
.PHONY: sim_spi
sim_spi: $(BUILD_DIR)/tb_spi_slave.vvp
	cd $(TB_DIR) && $(VVP) ../$(BUILD_DIR)/tb_spi_slave.vvp

$(BUILD_DIR)/tb_spi_slave.vvp: $(TB_DIR)/tb_spi_slave.v $(RTL_DIR)/spi_slave.v $(RTL_DIR)/fifo_sync.v | $(BUILD_DIR)
	$(IVERILOG) -DSIMULATION \
		-o $@ \
		-I $(RTL_DIR) \
		$(TB_DIR)/tb_spi_slave.v \
		$(RTL_DIR)/spi_slave.v \
		$(RTL_DIR)/fifo_sync.v

# =============================================================================
# Simulation — FIR decimation testbench
# =============================================================================
.PHONY: sim_fir
sim_fir: gen_vectors $(BUILD_DIR)/tb_fir_decimate.vvp
	cd $(TB_DIR) && $(VVP) ../$(BUILD_DIR)/tb_fir_decimate.vvp

$(BUILD_DIR)/tb_fir_decimate.vvp: $(TB_DIR)/tb_fir_decimate.v $(RTL_DIR)/fir_decimate.v $(RTL_DIR)/fir_mac.v | $(BUILD_DIR)
	$(IVERILOG) -DSIMULATION \
		-o $@ \
		-I $(RTL_DIR) \
		$(TB_DIR)/tb_fir_decimate.v \
		$(RTL_DIR)/fir_decimate.v \
		$(RTL_DIR)/fir_mac.v

# =============================================================================
# Simulation — Cross-correlation testbench
# =============================================================================
.PHONY: sim_xcorr
sim_xcorr: gen_vectors $(BUILD_DIR)/tb_xcorr.vvp
	cd $(TB_DIR) && $(VVP) ../$(BUILD_DIR)/tb_xcorr.vvp

$(BUILD_DIR)/tb_xcorr.vvp: $(TB_DIR)/tb_xcorr.v $(RTL_DIR)/xcorr_engine.v $(RTL_DIR)/fft_radix2.v | $(BUILD_DIR)
	$(IVERILOG) -DSIMULATION \
		-o $@ \
		-I $(RTL_DIR) \
		$(TB_DIR)/tb_xcorr.v \
		$(RTL_DIR)/xcorr_engine.v \
		$(RTL_DIR)/fft_radix2.v

# =============================================================================
# Test vector generation
# =============================================================================
.PHONY: gen_vectors
gen_vectors: $(REF_DIR)/test_iq_data.hex

$(REF_DIR)/test_iq_data.hex: $(REF_DIR)/gen_test_vectors.py
	cd $(REF_DIR) && $(PYTHON) gen_test_vectors.py

# =============================================================================
# Verification
# =============================================================================
.PHONY: verify
verify:
	cd $(REF_DIR) && $(PYTHON) verify_output.py

.PHONY: verify_fir
verify_fir:
	cd $(REF_DIR) && $(PYTHON) verify_output.py --fir

.PHONY: verify_xcorr
verify_xcorr:
	cd $(REF_DIR) && $(PYTHON) verify_output.py --xcorr

# =============================================================================
# Lint (Verilator)
# =============================================================================
.PHONY: lint
lint:
	verilator --lint-only -DSIMULATION -Wall \
		-I$(RTL_DIR) \
		$(RTL_DIR)/top.v

# =============================================================================
# Build directory
# =============================================================================
$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# =============================================================================
# Clean
# =============================================================================
.PHONY: clean
clean:
	rm -rf $(BUILD_DIR)/*
	rm -f $(TB_DIR)/*.vcd
	rm -f $(TB_DIR)/tb_top_output.hex
	rm -f $(REF_DIR)/*.hex

.PHONY: clean_sim
clean_sim:
	rm -f $(BUILD_DIR)/*.vvp
	rm -f $(TB_DIR)/*.vcd

# =============================================================================
# Help
# =============================================================================
.PHONY: help
help:
	@echo "HeIMDALL DAQ FPGA Gateware Build System"
	@echo ""
	@echo "Targets:"
	@echo "  synth       — Synthesize with Yosys (ECP5)"
	@echo "  pnr         — Place and route with nextpnr-ecp5"
	@echo "  bitstream   — Generate bitstream (.bit)"
	@echo "  sim         — Run top-level simulation"
	@echo "  sim_spi     — Run SPI slave simulation"
	@echo "  sim_fir     — Run FIR decimation simulation"
	@echo "  sim_xcorr   — Run cross-correlation simulation"
	@echo "  gen_vectors — Generate Python test vectors"
	@echo "  verify      — Verify simulation output vs reference"
	@echo "  lint        — Lint with Verilator"
	@echo "  clean       — Remove all build artifacts"
	@echo "  help        — Show this message"
	@echo ""
	@echo "Requirements:"
	@echo "  Synthesis:  yosys, nextpnr-ecp5, ecppack (prjtrellis)"
	@echo "  Simulation: iverilog, vvp"
	@echo "  Vectors:    python3 with numpy, scipy"
