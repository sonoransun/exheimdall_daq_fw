`default_nettype none
// =============================================================================
// top.v — HeIMDALL DAQ FPGA signal processing offload HAT
//
// Top-level module connecting SPI slave to the processing pipeline.
// Target: Lattice ECP5 (ULX3S), open-source Yosys + nextpnr toolchain.
//
// Data path:
//   SPI RX -> iq_deinterleave -> u8_to_f32(I) + u8_to_f32(Q)
//          -> fir_decimate -> output_reinterleave -> SPI TX
//
// Optional cross-correlation path (selected via config register):
//   fir_decimate -> xcorr_engine -> SPI TX
//
// Clock domains:
//   - SPI SCLK: SPI interface (directly used by spi_slave)
//   - sys_clk:  internal PLL (100 MHz from 25 MHz oscillator) for processing
// =============================================================================
module top #(
    parameter MAX_CHANNELS = 5,
    parameter MAX_TAP_SIZE = 128,
    parameter MAX_CPI      = 1048576,
    parameter SPI_FIFO_DEPTH = 2048,
    parameter FFT_N        = 1024,
    parameter DATA_W       = 18,
    parameter COEFF_W      = 18,
    parameter ACC_W        = 48
) (
    // Crystal oscillator
    input  wire        clk_25m,

    // SPI interface (directly from Raspberry Pi)
    input  wire        spi_sclk,
    input  wire        spi_cs_n,
    input  wire        spi_mosi,
    output wire        spi_miso,

    // Handshake
    output wire        drdy,       // Data ready (FPGA -> Pi)
    input  wire        busy_in,    // Busy (Pi -> FPGA)

    // Active-low reset
    input  wire        reset_n,

    // Status LEDs
    output wire [7:0]  led
);

    // =========================================================================
    // Clock generation — PLL: 25 MHz -> 100 MHz system clock
    // =========================================================================
    // For ECP5, use the EHXPLLL primitive. For simulation, pass through.
    wire sys_clk;
    wire pll_lock;

    `ifdef SIMULATION
        assign sys_clk  = clk_25m;
        assign pll_lock = 1'b1;
    `else
        // ECP5 PLL instantiation (EHXPLLL)
        // Input: 25 MHz, Output: 100 MHz (multiply by 4)
        (* keep *)
        EHXPLLL #(
            .PLLRST_ENA       ("DISABLED"),
            .INTFB_WAKE       ("DISABLED"),
            .STDBY_ENABLE     ("DISABLED"),
            .DPHASE_SOURCE    ("DISABLED"),
            .CLKOP_FPHASE     (0),
            .CLKOP_CPHASE     (1),
            .OUTDIVIDER_MUXA  ("DIVA"),
            .CLKOP_ENABLE     ("ENABLED"),
            .CLKOP_DIV        (6),          // VCO / 6 = 100 MHz
            .CLKI_DIV         (1),          // Ref / 1 = 25 MHz
            .CLKFB_DIV        (4),          // Feedback * 4 => VCO = 600 MHz
            .FEEDBK_PATH      ("CLKOP")
        ) u_pll (
            .CLKI     (clk_25m),
            .CLKFB    (sys_clk),
            .RST      (1'b0),
            .STDBY    (1'b0),
            .PHASESEL0(1'b0),
            .PHASESEL1(1'b0),
            .PHASEDIR (1'b0),
            .PHASESTEP(1'b0),
            .PHASELOADREG(1'b0),
            .PLLWAKESYNC(1'b0),
            .ENCLKOP  (1'b1),
            .CLKOP    (sys_clk),
            .LOCK     (pll_lock)
        );
    `endif

    // Synchronous reset
    wire sys_rst;
    reg [3:0] rst_sync;
    always @(posedge sys_clk or negedge reset_n) begin
        if (!reset_n)
            rst_sync <= 4'hF;
        else
            rst_sync <= {rst_sync[2:0], ~pll_lock};
    end
    assign sys_rst = rst_sync[3];

    // =========================================================================
    // SPI Slave
    // =========================================================================
    wire [7:0]  spi_rx_data;
    wire        spi_rx_valid;
    wire        spi_rx_ready;

    wire [7:0]  spi_tx_data;
    wire        spi_tx_valid;
    wire        spi_tx_ready;

    wire [7:0]  spi_reg_addr;
    wire [31:0] spi_reg_wdata;
    wire        spi_reg_wr;
    wire        spi_reg_rd;
    wire [31:0] spi_reg_rdata;

    wire [15:0] spi_frame_seq;
    wire [31:0] spi_frame_len;
    wire [7:0]  spi_frame_cmd;
    wire        spi_frame_valid;
    wire        spi_crc_error;

    spi_slave #(
        .FIFO_DEPTH (SPI_FIFO_DEPTH)
    ) u_spi_slave (
        .sclk       (spi_sclk),
        .cs_n       (spi_cs_n),
        .mosi       (spi_mosi),
        .miso       (spi_miso),
        .clk        (sys_clk),
        .rst        (sys_rst),
        .rx_data    (spi_rx_data),
        .rx_valid   (spi_rx_valid),
        .rx_ready   (spi_rx_ready),
        .tx_data    (spi_tx_data),
        .tx_valid   (spi_tx_valid),
        .tx_ready   (spi_tx_ready),
        .reg_addr   (spi_reg_addr),
        .reg_wdata  (spi_reg_wdata),
        .reg_wr     (spi_reg_wr),
        .reg_rd     (spi_reg_rd),
        .reg_rdata  (spi_reg_rdata),
        .drdy       (drdy),
        .busy       (busy_in),
        .frame_seq  (spi_frame_seq),
        .frame_len  (spi_frame_len),
        .frame_cmd  (spi_frame_cmd),
        .frame_valid(spi_frame_valid),
        .crc_error  (spi_crc_error)
    );

    // =========================================================================
    // Configuration Registers
    // =========================================================================
    wire        ctrl_start, ctrl_stop, ctrl_reset;
    wire [3:0]  cfg_decim_ratio;
    wire [$clog2(MAX_TAP_SIZE)-1:0] cfg_tap_count;
    wire [2:0]  cfg_channel_count;
    wire [19:0] cfg_cpi_length;
    wire [1:0]  cfg_proc_mode;

    wire        sts_busy;
    wire        sts_done;
    wire        sts_error;

    wire [$clog2(MAX_TAP_SIZE)-1:0] coeff_addr;
    wire [31:0]                      coeff_wdata;
    wire                             coeff_wr;

    config_regs #(
        .MAX_TAPS    (MAX_TAP_SIZE),
        .MAX_CHANNELS(MAX_CHANNELS),
        .MAX_CPI     (MAX_CPI)
    ) u_config_regs (
        .clk              (sys_clk),
        .rst              (sys_rst),
        .reg_addr         (spi_reg_addr),
        .reg_wdata        (spi_reg_wdata),
        .reg_wr           (spi_reg_wr),
        .reg_rd           (spi_reg_rd),
        .reg_rdata        (spi_reg_rdata),
        .ctrl_start       (ctrl_start),
        .ctrl_stop        (ctrl_stop),
        .ctrl_reset       (ctrl_reset),
        .cfg_decim_ratio  (cfg_decim_ratio),
        .cfg_tap_count    (cfg_tap_count),
        .cfg_channel_count(cfg_channel_count),
        .cfg_cpi_length   (cfg_cpi_length),
        .cfg_proc_mode    (cfg_proc_mode),
        .sts_busy         (sts_busy),
        .sts_done         (sts_done),
        .sts_error        (sts_error),
        .coeff_addr       (coeff_addr),
        .coeff_wdata      (coeff_wdata),
        .coeff_wr         (coeff_wr)
    );

    // =========================================================================
    // ID Register
    // =========================================================================
    wire [31:0] id_rdata;

    id_register #(
        .MAX_TAPS (MAX_TAP_SIZE),
        .MAX_FFT  (FFT_N)
    ) u_id_register (
        .clk   (sys_clk),
        .rst   (sys_rst),
        .addr  (spi_reg_addr[4:0]),
        .rd    (spi_reg_rd && (spi_reg_addr[7:5] == 3'b111)), // High addr for ID
        .rdata (id_rdata)
    );

    // =========================================================================
    // Processing pipeline
    // =========================================================================

    // --- IQ Deinterleave ---
    wire [7:0]  deint_i, deint_q;
    wire        deint_valid;
    wire        deint_ready;

    // Only pass data-write commands to the processing path
    wire data_to_pipe = spi_frame_valid && (spi_frame_cmd == 8'h10);

    iq_deinterleave u_deinterleave (
        .clk       (sys_clk),
        .rst       (sys_rst || ctrl_reset),
        .din       (spi_rx_data),
        .din_valid (spi_rx_valid && data_to_pipe),
        .din_ready (spi_rx_ready),
        .i_out     (deint_i),
        .q_out     (deint_q),
        .out_valid (deint_valid),
        .out_ready (deint_ready)
    );

    // --- U8 to F32 conversion (I channel) ---
    wire [31:0] f32_i;
    wire        f32_i_valid;
    wire        f32_i_ready;

    u8_to_f32 u_u8f32_i (
        .clk       (sys_clk),
        .rst       (sys_rst || ctrl_reset),
        .din       (deint_i),
        .din_valid (deint_valid),
        .din_ready (deint_ready),
        .dout      (f32_i),
        .dout_valid(f32_i_valid),
        .dout_ready(f32_i_ready)
    );

    // --- U8 to F32 conversion (Q channel) ---
    wire [31:0] f32_q;
    wire        f32_q_valid;
    wire        f32_q_ready;

    u8_to_f32 u_u8f32_q (
        .clk       (sys_clk),
        .rst       (sys_rst || ctrl_reset),
        .din       (deint_q),
        .din_valid (deint_valid),  // Same valid as I (pair-wise)
        .din_ready (),             // Ready driven by I path (shared)
        .dout      (f32_q),
        .dout_valid(f32_q_valid),
        .dout_ready(f32_q_ready)
    );

    // --- FIR Decimation Filter ---
    // Convert F32 to fixed-point for FIR input (take upper bits of mantissa)
    // Simplified: use sign + 17 MSBs of the float as fixed-point approximation
    wire signed [DATA_W-1:0] fir_i_in = {f32_i[31], f32_i[22:6]};
    wire signed [DATA_W-1:0] fir_q_in = {f32_q[31], f32_q[22:6]};
    wire                     fir_in_valid = f32_i_valid && f32_q_valid;

    wire signed [DATA_W-1:0] fir_i_out, fir_q_out;
    wire                     fir_out_valid;
    wire                     fir_out_ready;
    wire                     fir_in_ready;

    assign f32_i_ready = fir_in_ready;
    assign f32_q_ready = fir_in_ready;

    fir_decimate #(
        .MAX_TAPS  (MAX_TAP_SIZE),
        .DATA_W    (DATA_W),
        .COEFF_W   (COEFF_W),
        .ACC_W     (ACC_W)
    ) u_fir_decimate (
        .clk            (sys_clk),
        .rst            (sys_rst || ctrl_reset),
        .cfg_tap_count  (cfg_tap_count),
        .cfg_decim_ratio(cfg_decim_ratio),
        .coeff_addr     (coeff_addr),
        .coeff_wdata    (coeff_wdata),
        .coeff_wr       (coeff_wr),
        .i_in           (fir_i_in),
        .q_in           (fir_q_in),
        .in_valid       (fir_in_valid),
        .in_ready       (fir_in_ready),
        .i_out          (fir_i_out),
        .q_out          (fir_q_out),
        .out_valid      (fir_out_valid),
        .out_ready      (fir_out_ready)
    );

    // --- Processing mode multiplexing ---
    // Mode 0: FIR only -> output_reinterleave -> SPI TX
    // Mode 1: FIR + FFT xcorr -> SPI TX
    // Mode 2: FFT xcorr only (bypass FIR) -> SPI TX

    // Convert FIR fixed-point output back to F32 for output
    // Simplified: construct F32 from sign + magnitude
    wire [31:0] fir_i_f32 = {fir_i_out[DATA_W-1], 8'd127, fir_i_out[DATA_W-2:0], {(23-DATA_W+1){1'b0}}};
    wire [31:0] fir_q_f32 = {fir_q_out[DATA_W-1], 8'd127, fir_q_out[DATA_W-2:0], {(23-DATA_W+1){1'b0}}};

    // --- Output Re-interleave ---
    wire [7:0]  reint_dout;
    wire        reint_valid;
    wire        reint_ready;

    output_reinterleave u_reinterleave (
        .clk       (sys_clk),
        .rst       (sys_rst || ctrl_reset),
        .i_in      (fir_i_f32),
        .q_in      (fir_q_f32),
        .in_valid  (fir_out_valid && (cfg_proc_mode == 2'd0)),
        .in_ready  (fir_out_ready),
        .dout      (reint_dout),
        .dout_valid(reint_valid),
        .dout_ready(reint_ready)
    );

    // Connect re-interleaved output to SPI TX
    assign spi_tx_data  = reint_dout;
    assign spi_tx_valid = reint_valid;
    assign reint_ready  = spi_tx_ready;

    // --- Cross-correlation engine (optional path) ---
    // Instantiated but only active in modes 1 and 2
    wire xcorr_busy, xcorr_done;

    xcorr_engine #(
        .FFT_N  (FFT_N),
        .DATA_W (16)
    ) u_xcorr (
        .clk         (sys_clk),
        .rst         (sys_rst || ctrl_reset),
        .start       (ctrl_start && (cfg_proc_mode != 2'd0)),
        .busy        (xcorr_busy),
        .done        (xcorr_done),
        .mode_xcorr  (cfg_proc_mode != 2'd0),
        .ref_re      (fir_i_out[DATA_W-1 -: 16]),
        .ref_im      (fir_q_out[DATA_W-1 -: 16]),
        .ref_valid   (fir_out_valid && (cfg_proc_mode != 2'd0)),
        .ref_ready   (),
        .test_re     (16'sd0),    // Second channel — connected in multi-channel config
        .test_im     (16'sd0),
        .test_valid  (1'b0),
        .test_ready  (),
        .xcorr_mag2  (),
        .xcorr_valid (),
        .xcorr_ready (1'b1),
        .xcorr_idx   ()
    );

    // =========================================================================
    // Status
    // =========================================================================
    assign sts_busy  = fir_out_valid || xcorr_busy;
    assign sts_done  = xcorr_done;
    assign sts_error = spi_crc_error;

    // =========================================================================
    // LED indicators
    // =========================================================================
    assign led[0] = pll_lock;
    assign led[1] = ~spi_cs_n;        // SPI active
    assign led[2] = spi_frame_valid;   // Frame being processed
    assign led[3] = sts_busy;          // Processing busy
    assign led[4] = sts_done;          // Processing done
    assign led[5] = sts_error;         // Error
    assign led[6] = drdy;              // Data ready
    assign led[7] = 1'b0;             // Reserved

endmodule
`default_nettype wire
