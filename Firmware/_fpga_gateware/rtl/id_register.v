`default_nettype none
// =============================================================================
// id_register.v — FPGA identification register block (read-only)
//
// Address map (relative to ID base):
//   0x00: Magic word     — 0x48454944 ("HEID")
//   0x04: Capability flags — bit0=FIR, bit1=xcorr, bit2=U8->F32
//   0x08: Max tap count  — 128
//   0x0C: Max FFT size   — 65536
//   0x10: Version        — 0x00010000 (v1.0.0)
//
// Directly wired, no clock needed for reads (but registered for timing).
// =============================================================================
module id_register #(
    parameter MAGIC       = 32'h48454944,  // "HEID"
    parameter CAP_FIR     = 1,
    parameter CAP_XCORR   = 1,
    parameter CAP_U8F32   = 1,
    parameter MAX_TAPS    = 128,
    parameter MAX_FFT     = 65536,
    parameter VERSION     = 32'h00010000   // v1.0.0
) (
    input  wire        clk,
    input  wire        rst,

    // Read interface
    input  wire [4:0]  addr,       // Word address (0..4)
    input  wire        rd,
    output reg  [31:0] rdata
);

    wire [31:0] cap_flags = {29'd0, CAP_U8F32[0], CAP_XCORR[0], CAP_FIR[0]};

    always @(posedge clk) begin
        if (rst) begin
            rdata <= 32'd0;
        end else if (rd) begin
            case (addr[2:0])
                3'd0: rdata <= MAGIC;
                3'd1: rdata <= cap_flags;
                3'd2: rdata <= MAX_TAPS;
                3'd3: rdata <= MAX_FFT;
                3'd4: rdata <= VERSION;
                default: rdata <= 32'd0;
            endcase
        end
    end

endmodule
`default_nettype wire
