`default_nettype none
// =============================================================================
// fir_mac.v — Single multiply-accumulate unit for FIR filter
//
// Maps to ECP5 MULT18X18D DSP block.
// 18x18 signed multiply with 48-bit accumulator.
// Parameterized data width, pipelined for timing closure.
// =============================================================================
module fir_mac #(
    parameter DATA_W  = 18,  // Input data width (signed)
    parameter COEFF_W = 18,  // Coefficient width (signed)
    parameter ACC_W   = 48   // Accumulator width
) (
    input  wire                    clk,
    input  wire                    rst,

    // Control
    input  wire                    clear,    // Clear accumulator
    input  wire                    en,       // Enable MAC operation

    // Data inputs
    input  wire signed [DATA_W-1:0]  data_in,
    input  wire signed [COEFF_W-1:0] coeff_in,

    // Accumulated result
    output wire signed [ACC_W-1:0]   acc_out,
    output wire                      acc_valid
);

    // Pipeline stage 1: register inputs
    reg signed [DATA_W-1:0]  data_r;
    reg signed [COEFF_W-1:0] coeff_r;
    reg                      en_r1;
    reg                      clear_r1;

    always @(posedge clk) begin
        if (rst) begin
            data_r   <= {DATA_W{1'b0}};
            coeff_r  <= {COEFF_W{1'b0}};
            en_r1    <= 1'b0;
            clear_r1 <= 1'b0;
        end else begin
            data_r   <= data_in;
            coeff_r  <= coeff_in;
            en_r1    <= en;
            clear_r1 <= clear;
        end
    end

    // Pipeline stage 2: multiply
    // Synthesis tool should infer MULT18X18D for ECP5
    reg signed [DATA_W+COEFF_W-1:0] product;
    reg                              en_r2;
    reg                              clear_r2;

    always @(posedge clk) begin
        if (rst) begin
            product  <= {(DATA_W+COEFF_W){1'b0}};
            en_r2    <= 1'b0;
            clear_r2 <= 1'b0;
        end else begin
            product  <= data_r * coeff_r;
            en_r2    <= en_r1;
            clear_r2 <= clear_r1;
        end
    end

    // Pipeline stage 3: accumulate
    reg signed [ACC_W-1:0] accumulator;
    reg                    valid_r3;

    always @(posedge clk) begin
        if (rst) begin
            accumulator <= {ACC_W{1'b0}};
            valid_r3    <= 1'b0;
        end else begin
            valid_r3 <= en_r2;
            if (clear_r2) begin
                accumulator <= {{(ACC_W-DATA_W-COEFF_W){product[DATA_W+COEFF_W-1]}}, product};
            end else if (en_r2) begin
                accumulator <= accumulator + {{(ACC_W-DATA_W-COEFF_W){product[DATA_W+COEFF_W-1]}}, product};
            end
        end
    end

    assign acc_out   = accumulator;
    assign acc_valid = valid_r3;

endmodule
`default_nettype wire
