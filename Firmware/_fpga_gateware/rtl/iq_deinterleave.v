`default_nettype none
// =============================================================================
// iq_deinterleave.v — Separates interleaved U8 IQ into I and Q streams
//
// Input:  8-bit interleaved [I0, Q0, I1, Q1, ...]
// Output: separate 8-bit I stream and 8-bit Q stream
// Streaming at 1 sample per clock cycle (I/Q pair every 2 clocks).
// =============================================================================
module iq_deinterleave (
    input  wire        clk,
    input  wire        rst,

    // Input: interleaved I/Q bytes
    input  wire [7:0]  din,
    input  wire        din_valid,
    output wire        din_ready,

    // Output: separate I and Q streams (one pair per output valid)
    output reg  [7:0]  i_out,
    output reg  [7:0]  q_out,
    output reg         out_valid,
    input  wire        out_ready
);

    // State: 0 = expecting I sample, 1 = expecting Q sample
    reg phase;

    // Hold the I sample while waiting for Q
    reg [7:0] i_hold;
    reg       i_held;

    // Backpressure: accept input when we can store it
    assign din_ready = !i_held || (phase == 1'b1 && out_ready);

    always @(posedge clk) begin
        if (rst) begin
            phase     <= 1'b0;
            i_hold    <= 8'd0;
            i_held    <= 1'b0;
            i_out     <= 8'd0;
            q_out     <= 8'd0;
            out_valid <= 1'b0;
        end else begin
            // Clear output valid once downstream accepts
            if (out_valid && out_ready) begin
                out_valid <= 1'b0;
                i_held    <= 1'b0;
            end

            if (din_valid && din_ready) begin
                if (phase == 1'b0) begin
                    // Capture I sample
                    i_hold <= din;
                    i_held <= 1'b1;
                    phase  <= 1'b1;
                end else begin
                    // Capture Q sample, output I/Q pair
                    i_out     <= i_hold;
                    q_out     <= din;
                    out_valid <= 1'b1;
                    phase     <= 1'b0;
                end
            end
        end
    end

endmodule
`default_nettype wire
