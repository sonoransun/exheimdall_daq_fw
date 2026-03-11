`default_nettype none
// =============================================================================
// output_reinterleave.v — Re-interleaves separate I/Q float32 streams
//
// Input:  Parallel F32 I and F32 Q streams
// Output: Interleaved [I0, Q0, I1, Q1, ...] as 32-bit words
// Serializes 32-bit floats to SPI TX FIFO one byte at a time.
// =============================================================================
module output_reinterleave (
    input  wire        clk,
    input  wire        rst,

    // Input: parallel I and Q (IEEE 754 float32)
    input  wire [31:0] i_in,
    input  wire [31:0] q_in,
    input  wire        in_valid,
    output wire        in_ready,

    // Output: interleaved bytes for SPI TX
    output reg  [7:0]  dout,
    output reg         dout_valid,
    input  wire        dout_ready
);

    // We need to serialize: I[31:24], I[23:16], I[15:8], I[7:0],
    //                       Q[31:24], Q[23:16], Q[15:8], Q[7:0]
    // Total 8 bytes per I/Q pair

    reg [31:0] i_hold;
    reg [31:0] q_hold;
    reg [2:0]  byte_cnt;   // 0..7
    reg        active;

    assign in_ready = !active && (!dout_valid || dout_ready);

    always @(posedge clk) begin
        if (rst) begin
            i_hold     <= 32'd0;
            q_hold     <= 32'd0;
            byte_cnt   <= 3'd0;
            active     <= 1'b0;
            dout       <= 8'd0;
            dout_valid <= 1'b0;
        end else begin
            if (dout_valid && dout_ready)
                dout_valid <= 1'b0;

            if (active) begin
                if (!dout_valid || dout_ready) begin
                    case (byte_cnt)
                        3'd0: dout <= i_hold[31:24];
                        3'd1: dout <= i_hold[23:16];
                        3'd2: dout <= i_hold[15:8];
                        3'd3: dout <= i_hold[7:0];
                        3'd4: dout <= q_hold[31:24];
                        3'd5: dout <= q_hold[23:16];
                        3'd6: dout <= q_hold[15:8];
                        3'd7: dout <= q_hold[7:0];
                    endcase
                    dout_valid <= 1'b1;
                    byte_cnt   <= byte_cnt + 3'd1;
                    if (byte_cnt == 3'd7)
                        active <= 1'b0;
                end
            end else if (in_valid && in_ready) begin
                i_hold   <= i_in;
                q_hold   <= q_in;
                active   <= 1'b1;
                byte_cnt <= 3'd0;
            end
        end
    end

endmodule
`default_nettype wire
