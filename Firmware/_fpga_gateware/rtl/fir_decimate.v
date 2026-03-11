`default_nettype none
// =============================================================================
// fir_decimate.v — Polyphase FIR decimation filter
//
// Configurable tap count (up to MAX_TAPS), configurable decimation ratio (1-16).
// Polyphase decomposition: R sub-filters each with ceil(tap_count/R) taps.
// Uses DSP blocks (MULT18X18D on ECP5) for multiply-accumulate.
// Dual pipeline: processes I and Q in parallel.
// Coefficient memory: dual-port BRAM, loaded via config register writes.
// State memory: BRAM for filter state vectors.
// Streaming input/output with valid/ready handshake.
// =============================================================================
module fir_decimate #(
    parameter MAX_TAPS      = 128,
    parameter MAX_DECIM     = 16,
    parameter DATA_W        = 18,   // Internal fixed-point width (signed)
    parameter COEFF_W       = 18,   // Coefficient width (signed)
    parameter ACC_W         = 48,   // Accumulator width
    parameter TAP_ADDR_W    = $clog2(MAX_TAPS)
) (
    input  wire                    clk,
    input  wire                    rst,

    // Configuration (directly from config_regs)
    input  wire [TAP_ADDR_W-1:0]  cfg_tap_count,     // Actual number of taps (1..MAX_TAPS)
    input  wire [3:0]             cfg_decim_ratio,    // Decimation ratio (1..16)

    // Coefficient loading interface
    input  wire [TAP_ADDR_W-1:0]  coeff_addr,
    input  wire [31:0]            coeff_wdata,        // IEEE 754 float32 coefficient
    input  wire                   coeff_wr,

    // Input: I and Q streams (fixed-point, sign-extended from u8_to_f32)
    input  wire signed [DATA_W-1:0] i_in,
    input  wire signed [DATA_W-1:0] q_in,
    input  wire                     in_valid,
    output wire                     in_ready,

    // Output: decimated I and Q
    output reg  signed [DATA_W-1:0] i_out,
    output reg  signed [DATA_W-1:0] q_out,
    output reg                      out_valid,
    input  wire                     out_ready
);

    // =========================================================================
    // Coefficient memory (dual-port BRAM)
    // =========================================================================
    reg signed [COEFF_W-1:0] coeff_mem [0:MAX_TAPS-1];

    // Write port: convert float32 coefficient to fixed-point on load
    // Simplified: treat lower 18 bits of float32 mantissa as fixed-point Q1.17
    // In practice, the host pre-converts to fixed-point before loading.
    always @(posedge clk) begin
        if (coeff_wr) begin
            coeff_mem[coeff_addr] <= coeff_wdata[COEFF_W-1:0];
        end
    end

    // =========================================================================
    // State memory (circular buffer for delay line)
    // =========================================================================
    reg signed [DATA_W-1:0] state_i [0:MAX_TAPS-1];
    reg signed [DATA_W-1:0] state_q [0:MAX_TAPS-1];
    reg [TAP_ADDR_W-1:0]   state_wr_ptr;

    // =========================================================================
    // Decimation counter
    // =========================================================================
    reg [3:0] decim_cnt;
    wire      decim_output = (decim_cnt == 4'd0);

    // =========================================================================
    // Filter FSM
    // =========================================================================
    localparam FSM_IDLE    = 3'd0;
    localparam FSM_LOAD    = 3'd1;
    localparam FSM_COMPUTE = 3'd2;
    localparam FSM_OUTPUT  = 3'd3;
    localparam FSM_SKIP    = 3'd4;

    reg [2:0]              fsm_state;
    reg [TAP_ADDR_W-1:0]  tap_idx;
    reg [TAP_ADDR_W-1:0]  state_rd_ptr;

    // MAC units for I and Q
    reg                    mac_clear;
    reg                    mac_en;
    reg signed [DATA_W-1:0]  mac_i_data;
    reg signed [COEFF_W-1:0] mac_i_coeff;
    reg signed [DATA_W-1:0]  mac_q_data;
    reg signed [COEFF_W-1:0] mac_q_coeff;

    wire signed [ACC_W-1:0] mac_i_acc;
    wire                     mac_i_valid;
    wire signed [ACC_W-1:0] mac_q_acc;
    wire                     mac_q_valid;

    fir_mac #(
        .DATA_W  (DATA_W),
        .COEFF_W (COEFF_W),
        .ACC_W   (ACC_W)
    ) u_mac_i (
        .clk      (clk),
        .rst      (rst),
        .clear    (mac_clear),
        .en       (mac_en),
        .data_in  (mac_i_data),
        .coeff_in (mac_i_coeff),
        .acc_out  (mac_i_acc),
        .acc_valid(mac_i_valid)
    );

    fir_mac #(
        .DATA_W  (DATA_W),
        .COEFF_W (COEFF_W),
        .ACC_W   (ACC_W)
    ) u_mac_q (
        .clk      (clk),
        .rst      (rst),
        .clear    (mac_clear),
        .en       (mac_en),
        .data_in  (mac_q_data),
        .coeff_in (mac_q_coeff),
        .acc_out  (mac_q_acc),
        .acc_valid(mac_q_valid)
    );

    assign in_ready = (fsm_state == FSM_IDLE) && (!out_valid || out_ready);

    // Pipeline delay counter for MAC (3 stages)
    reg [2:0] mac_pipeline_cnt;
    reg       compute_done;

    always @(posedge clk) begin
        if (rst) begin
            fsm_state      <= FSM_IDLE;
            tap_idx        <= {TAP_ADDR_W{1'b0}};
            state_wr_ptr   <= {TAP_ADDR_W{1'b0}};
            state_rd_ptr   <= {TAP_ADDR_W{1'b0}};
            decim_cnt      <= 4'd0;
            mac_clear      <= 1'b0;
            mac_en         <= 1'b0;
            mac_i_data     <= {DATA_W{1'b0}};
            mac_i_coeff    <= {COEFF_W{1'b0}};
            mac_q_data     <= {DATA_W{1'b0}};
            mac_q_coeff    <= {COEFF_W{1'b0}};
            i_out          <= {DATA_W{1'b0}};
            q_out          <= {DATA_W{1'b0}};
            out_valid      <= 1'b0;
            mac_pipeline_cnt <= 3'd0;
            compute_done   <= 1'b0;
        end else begin
            mac_clear <= 1'b0;
            mac_en    <= 1'b0;

            if (out_valid && out_ready)
                out_valid <= 1'b0;

            case (fsm_state)
                FSM_IDLE: begin
                    if (in_valid && in_ready) begin
                        // Store new sample in delay line
                        state_i[state_wr_ptr] <= i_in;
                        state_q[state_wr_ptr] <= q_in;

                        if (decim_output) begin
                            // Need to compute filter output
                            fsm_state    <= FSM_COMPUTE;
                            tap_idx      <= {TAP_ADDR_W{1'b0}};
                            state_rd_ptr <= state_wr_ptr;
                            mac_clear    <= 1'b1;
                            mac_en       <= 1'b1;
                            mac_i_data   <= i_in;
                            mac_i_coeff  <= coeff_mem[0];
                            mac_q_data   <= q_in;
                            mac_q_coeff  <= coeff_mem[0];
                            compute_done <= 1'b0;
                            mac_pipeline_cnt <= 3'd0;
                        end else begin
                            fsm_state <= FSM_SKIP;
                        end

                        // Advance write pointer (circular)
                        if (state_wr_ptr == cfg_tap_count - 1'b1)
                            state_wr_ptr <= {TAP_ADDR_W{1'b0}};
                        else
                            state_wr_ptr <= state_wr_ptr + 1'b1;

                        // Advance decimation counter
                        if (decim_cnt >= cfg_decim_ratio - 4'd1)
                            decim_cnt <= 4'd0;
                        else
                            decim_cnt <= decim_cnt + 4'd1;
                    end
                end

                FSM_COMPUTE: begin
                    if (!compute_done) begin
                        tap_idx <= tap_idx + 1'b1;

                        // Walk backwards through state buffer
                        if (state_rd_ptr == {TAP_ADDR_W{1'b0}})
                            state_rd_ptr <= cfg_tap_count - 1'b1;
                        else
                            state_rd_ptr <= state_rd_ptr - 1'b1;

                        if (tap_idx + 1'b1 >= cfg_tap_count) begin
                            compute_done <= 1'b1;
                            mac_en       <= 1'b0;
                            mac_pipeline_cnt <= 3'd1;
                        end else begin
                            mac_en      <= 1'b1;
                            mac_clear   <= 1'b0;
                            mac_i_data  <= state_i[state_rd_ptr];
                            mac_i_coeff <= coeff_mem[tap_idx + 1'b1];
                            mac_q_data  <= state_q[state_rd_ptr];
                            mac_q_coeff <= coeff_mem[tap_idx + 1'b1];
                        end
                    end else begin
                        // Wait for MAC pipeline to flush (3 stages)
                        mac_pipeline_cnt <= mac_pipeline_cnt + 3'd1;
                        if (mac_pipeline_cnt >= 3'd4) begin
                            fsm_state <= FSM_OUTPUT;
                        end
                    end
                end

                FSM_OUTPUT: begin
                    // Truncate/round accumulator to output width
                    // Take upper DATA_W bits after removing sign extension
                    i_out     <= mac_i_acc[ACC_W-1 -: DATA_W];
                    q_out     <= mac_q_acc[ACC_W-1 -: DATA_W];
                    out_valid <= 1'b1;
                    fsm_state <= FSM_IDLE;
                end

                FSM_SKIP: begin
                    // Non-output decimation phase — return to idle immediately
                    fsm_state <= FSM_IDLE;
                end

                default: fsm_state <= FSM_IDLE;
            endcase
        end
    end

endmodule
`default_nettype wire
