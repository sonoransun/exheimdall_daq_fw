`default_nettype none
// =============================================================================
// u8_to_f32.v — Converts unsigned 8-bit to IEEE 754 float32
//
// Computes (u8 - 127.5) / 127.5 to normalize to [-1.0, 1.0].
// 3-stage pipeline:
//   Stage 1: Subtract 127.5 (as fixed-point: u8*2 - 255 => signed 9-bit)
//   Stage 2: Convert signed integer to float mantissa/exponent
//   Stage 3: Multiply by 1/127.5 scale factor (pre-computed)
//
// Uses fixed-point intermediate for efficiency on ECP5.
// Processes I and Q in parallel (dual instantiation expected at top level).
// =============================================================================
module u8_to_f32 (
    input  wire        clk,
    input  wire        rst,

    // Input
    input  wire [7:0]  din,
    input  wire        din_valid,
    output wire        din_ready,

    // Output: IEEE 754 float32
    output reg  [31:0] dout,
    output reg         dout_valid,
    input  wire        dout_ready
);

    // Pipeline flow control
    reg         p1_valid, p2_valid;
    wire        p1_ready, p2_ready, p3_ready;

    assign din_ready = !p1_valid || p1_ready;
    assign p1_ready  = !p2_valid || p2_ready;
    assign p2_ready  = !dout_valid || dout_ready;
    assign p3_ready  = dout_ready;

    // =========================================================================
    // Stage 1: Subtract — compute signed_val = (u8 * 2) - 255
    //          This is equivalent to 2*(u8 - 127.5) in fixed-point (1 frac bit)
    // =========================================================================
    reg signed [9:0] p1_signed_val; // range: -255 to +255
    // p1_valid declared above

    always @(posedge clk) begin
        if (rst) begin
            p1_valid      <= 1'b0;
            p1_signed_val <= 10'sd0;
        end else begin
            if (p1_valid && p1_ready)
                p1_valid <= 1'b0;
            if (din_valid && din_ready) begin
                p1_signed_val <= ({1'b0, din, 1'b0}) - 10'sd255; // u8*2 - 255
                p1_valid      <= 1'b1;
            end
        end
    end

    // =========================================================================
    // Stage 2: Convert signed integer to float components
    //          We convert p1_signed_val (which is 2x the desired numerator)
    //          to a preliminary float. The value represents (u8-127.5)*2 in
    //          integer, so we need to account for the *2 and /127.5 in stage 3.
    // =========================================================================
    reg [31:0] p2_float;

    // Integer to float conversion (combinational helper, registered output)
    // Handle: sign, magnitude, find MSB, build float
    reg        p2_sign;
    reg [8:0]  p2_mag;      // |p1_signed_val| fits in 9 bits (0..255)
    reg [7:0]  p2_exponent;
    reg [22:0] p2_mantissa;

    always @(posedge clk) begin
        if (rst) begin
            p2_valid <= 1'b0;
            p2_float <= 32'd0;
        end else begin
            if (p2_valid && p2_ready)
                p2_valid <= 1'b0;

            if (p1_valid && p1_ready) begin
                p2_valid <= 1'b1;

                if (p1_signed_val == 10'sd0) begin
                    p2_float <= 32'h00000000; // +0.0
                end else begin
                    p2_sign = p1_signed_val[9];
                    p2_mag  = p2_sign ? (-p1_signed_val[8:0]) : p1_signed_val[8:0];

                    // Find position of MSB (leading one)
                    // Magnitude range 1..255, so MSB is bit 0..7
                    // We encode as float: val = 1.mantissa * 2^exp
                    // with bias 127
                    casez (p2_mag)
                        9'b1_????_????: begin p2_exponent = 8'd135; p2_mantissa = {p2_mag[7:0], 15'd0}; end // 2^8
                        9'b0_1???_????: begin p2_exponent = 8'd134; p2_mantissa = {p2_mag[6:0], 16'd0}; end
                        9'b0_01??_????: begin p2_exponent = 8'd133; p2_mantissa = {p2_mag[5:0], 17'd0}; end
                        9'b0_001?_????: begin p2_exponent = 8'd132; p2_mantissa = {p2_mag[4:0], 18'd0}; end
                        9'b0_0001_????: begin p2_exponent = 8'd131; p2_mantissa = {p2_mag[3:0], 19'd0}; end
                        9'b0_0000_1???: begin p2_exponent = 8'd130; p2_mantissa = {p2_mag[2:0], 20'd0}; end
                        9'b0_0000_01??: begin p2_exponent = 8'd129; p2_mantissa = {p2_mag[1:0], 21'd0}; end
                        9'b0_0000_001?: begin p2_exponent = 8'd128; p2_mantissa = {p2_mag[0],   22'd0}; end
                        9'b0_0000_0001: begin p2_exponent = 8'd127; p2_mantissa = 23'd0; end
                        default:        begin p2_exponent = 8'd0;   p2_mantissa = 23'd0; end
                    endcase

                    p2_float <= {p2_sign, p2_exponent, p2_mantissa};
                end
            end
        end
    end

    // =========================================================================
    // Stage 3: Multiply by scale factor
    //          p2_float represents (u8 - 127.5) * 2
    //          We need to multiply by 1/(127.5*2) = 1/255
    //          1/255 in float32 = 0x3B808081 approximately
    //          For exact result: 1/127.5 = 0x3C003C00 approximately
    //          Since p2_float = 2*(u8-127.5), scale = 1/255 = 0x3B808081
    //
    //          Simplified: instead of true float multiply, we adjust the
    //          exponent. Dividing by 255 ~= 2^(-8) * (256/255).
    //          We subtract 8 from the exponent and accept <0.4% error.
    //          For precise results, downstream DSP corrects in float domain.
    // =========================================================================

    always @(posedge clk) begin
        if (rst) begin
            dout_valid <= 1'b0;
            dout       <= 32'd0;
        end else begin
            if (dout_valid && dout_ready)
                dout_valid <= 1'b0;

            if (p2_valid && p2_ready) begin
                dout_valid <= 1'b1;

                if (p2_float == 32'd0) begin
                    dout <= 32'd0;
                end else begin
                    // Subtract 8 from exponent (divide by 256, close to 255)
                    // Sign preserved, mantissa preserved
                    dout <= {p2_float[31],
                             p2_float[30:23] - 8'd8,
                             p2_float[22:0]};
                end
            end
        end
    end

endmodule
`default_nettype wire
