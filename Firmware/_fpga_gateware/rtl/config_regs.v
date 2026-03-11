`default_nettype none
// =============================================================================
// config_regs.v — SPI-accessible register bank
//
// Address space: 0x00-0xFF (256 bytes, 64 x 32-bit registers)
// Directly drives fir_decimate and xcorr_engine parameters.
//
// Register map:
//   0x00: Control      — bit0=start, bit1=stop, bit2=reset processing
//   0x04: Status       — bit0=busy, bit1=done, bit2=error (read-only)
//   0x08: Decimation ratio (1..16)
//   0x0C: Tap count (1..128)
//   0x10: Channel count (1..5)
//   0x14: CPI length
//   0x18: Reserved
//   0x1C: Reserved
//   0x20-0x9F: FIR coefficients (128 x 32-bit, byte-addressed)
//   0xA0: Processing mode — 0=FIR only, 1=FIR+FFT, 2=FFT only
// =============================================================================
module config_regs #(
    parameter MAX_TAPS    = 128,
    parameter MAX_CHANNELS = 5,
    parameter MAX_CPI     = 1048576,
    parameter COEFF_ADDR_W = $clog2(MAX_TAPS)
) (
    input  wire        clk,
    input  wire        rst,

    // Register access from SPI slave
    input  wire [7:0]  reg_addr,
    input  wire [31:0] reg_wdata,
    input  wire        reg_wr,
    input  wire        reg_rd,
    output reg  [31:0] reg_rdata,

    // Configuration outputs
    output wire        ctrl_start,
    output wire        ctrl_stop,
    output wire        ctrl_reset,

    output wire [3:0]  cfg_decim_ratio,
    output wire [COEFF_ADDR_W-1:0] cfg_tap_count,
    output wire [2:0]  cfg_channel_count,
    output wire [19:0] cfg_cpi_length,
    output wire [1:0]  cfg_proc_mode,

    // Status inputs
    input  wire        sts_busy,
    input  wire        sts_done,
    input  wire        sts_error,

    // Coefficient write port (directly to FIR)
    output reg  [COEFF_ADDR_W-1:0] coeff_addr,
    output reg  [31:0]             coeff_wdata,
    output reg                     coeff_wr
);

    // =========================================================================
    // Register storage
    // =========================================================================
    reg [31:0] r_control;       // 0x00
    // Status is read-only      // 0x04
    reg [31:0] r_decim_ratio;   // 0x08
    reg [31:0] r_tap_count;     // 0x0C
    reg [31:0] r_channel_count; // 0x10
    reg [31:0] r_cpi_length;    // 0x14
    reg [31:0] r_reserved0;     // 0x18
    reg [31:0] r_reserved1;     // 0x1C
    reg [31:0] r_proc_mode;     // 0xA0

    // Control pulses (active for one cycle only)
    reg ctrl_start_r, ctrl_stop_r, ctrl_reset_r;
    assign ctrl_start = ctrl_start_r;
    assign ctrl_stop  = ctrl_stop_r;
    assign ctrl_reset = ctrl_reset_r;

    // Configuration outputs
    assign cfg_decim_ratio   = r_decim_ratio[3:0];
    assign cfg_tap_count     = r_tap_count[COEFF_ADDR_W-1:0];
    assign cfg_channel_count = r_channel_count[2:0];
    assign cfg_cpi_length    = r_cpi_length[19:0];
    assign cfg_proc_mode     = r_proc_mode[1:0];

    // =========================================================================
    // Write logic
    // =========================================================================
    always @(posedge clk) begin
        if (rst) begin
            r_control       <= 32'd0;
            r_decim_ratio   <= 32'd1;       // Default: no decimation
            r_tap_count     <= 32'd16;      // Default: 16 taps
            r_channel_count <= 32'd5;       // Default: 5 channels (KrakenSDR)
            r_cpi_length    <= 32'd1024;    // Default: 1K CPI
            r_reserved0     <= 32'd0;
            r_reserved1     <= 32'd0;
            r_proc_mode     <= 32'd0;       // Default: FIR only
            ctrl_start_r    <= 1'b0;
            ctrl_stop_r     <= 1'b0;
            ctrl_reset_r    <= 1'b0;
            coeff_addr      <= {COEFF_ADDR_W{1'b0}};
            coeff_wdata     <= 32'd0;
            coeff_wr        <= 1'b0;
        end else begin
            // Self-clearing control pulses
            ctrl_start_r <= 1'b0;
            ctrl_stop_r  <= 1'b0;
            ctrl_reset_r <= 1'b0;
            coeff_wr     <= 1'b0;

            if (reg_wr) begin
                case (reg_addr)
                    8'h00: begin
                        r_control    <= reg_wdata;
                        ctrl_start_r <= reg_wdata[0];
                        ctrl_stop_r  <= reg_wdata[1];
                        ctrl_reset_r <= reg_wdata[2];
                    end
                    // 0x04 is read-only (status)
                    8'h08: r_decim_ratio   <= reg_wdata;
                    8'h0C: r_tap_count     <= reg_wdata;
                    8'h10: r_channel_count <= reg_wdata;
                    8'h14: r_cpi_length    <= reg_wdata;
                    8'h18: r_reserved0     <= reg_wdata;
                    8'h1C: r_reserved1     <= reg_wdata;
                    8'hA0: r_proc_mode     <= reg_wdata;
                    default: begin
                        // Coefficient space: 0x20..0x9F => coeff index 0..127
                        if (reg_addr >= 8'h20 && reg_addr <= 8'h9F) begin
                            // Each coefficient is 4 bytes apart, so index = (addr-0x20)/4
                            // But register addresses are already 4-byte aligned conceptually
                            // We accept byte-address and convert
                            coeff_addr  <= (reg_addr - 8'h20) >> 2;
                            coeff_wdata <= reg_wdata;
                            coeff_wr    <= 1'b1;
                        end
                    end
                endcase
            end
        end
    end

    // =========================================================================
    // Read logic
    // =========================================================================
    always @(posedge clk) begin
        if (rst) begin
            reg_rdata <= 32'd0;
        end else if (reg_rd) begin
            case (reg_addr)
                8'h00: reg_rdata <= r_control;
                8'h04: reg_rdata <= {29'd0, sts_error, sts_done, sts_busy};
                8'h08: reg_rdata <= r_decim_ratio;
                8'h0C: reg_rdata <= r_tap_count;
                8'h10: reg_rdata <= r_channel_count;
                8'h14: reg_rdata <= r_cpi_length;
                8'h18: reg_rdata <= r_reserved0;
                8'h1C: reg_rdata <= r_reserved1;
                8'hA0: reg_rdata <= r_proc_mode;
                default: reg_rdata <= 32'hDEAD_BEEF;
            endcase
        end
    end

endmodule
`default_nettype wire
