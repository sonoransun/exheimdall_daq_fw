`default_nettype none
// =============================================================================
// fft_radix2.v — Pipelined radix-2 DIT FFT butterfly engine
//
// Memory-based architecture with in-place computation using ping-pong buffers.
// Log2(N) stages, each with N/2 butterflies.
// Twiddle ROM: BRAM with pre-computed sin/cos values.
// Fixed-point: configurable bit width (default 16+16 complex).
// =============================================================================
module fft_radix2 #(
    parameter FFT_N       = 1024,       // FFT size (power of 2)
    parameter DATA_W      = 16,         // Real/Imag data width
    parameter LOG2_N      = $clog2(FFT_N),
    parameter TWIDDLE_W   = 16          // Twiddle factor width
) (
    input  wire                    clk,
    input  wire                    rst,

    // Control
    input  wire                    start,
    input  wire                    inverse,     // 1=IFFT, 0=FFT
    output reg                     busy,
    output reg                     done,

    // Input data write port
    input  wire [LOG2_N-1:0]       wr_addr,
    input  wire signed [DATA_W-1:0] wr_re,
    input  wire signed [DATA_W-1:0] wr_im,
    input  wire                    wr_en,

    // Output data read port
    input  wire [LOG2_N-1:0]       rd_addr,
    output wire signed [DATA_W-1:0] rd_re,
    output wire signed [DATA_W-1:0] rd_im
);

    // =========================================================================
    // Ping-pong data buffers
    // =========================================================================
    reg signed [DATA_W-1:0] buf0_re [0:FFT_N-1];
    reg signed [DATA_W-1:0] buf0_im [0:FFT_N-1];
    reg signed [DATA_W-1:0] buf1_re [0:FFT_N-1];
    reg signed [DATA_W-1:0] buf1_im [0:FFT_N-1];

    reg ping; // 0=read from buf0/write to buf1, 1=read from buf1/write to buf0

    // =========================================================================
    // Twiddle factor ROM
    // Pre-computed: W_N^k = cos(2*pi*k/N) - j*sin(2*pi*k/N)
    // Stored as fixed-point Q1.(TWIDDLE_W-1)
    // Only N/2 entries needed (half-period symmetry)
    // =========================================================================
    reg signed [TWIDDLE_W-1:0] twiddle_re [0:FFT_N/2-1];
    reg signed [TWIDDLE_W-1:0] twiddle_im [0:FFT_N/2-1];

    // Initialize twiddle factors from hex file (loaded during synthesis/sim)
    initial begin
        $readmemh("twiddle_re.hex", twiddle_re);
        $readmemh("twiddle_im.hex", twiddle_im);
    end

    // =========================================================================
    // Input write / output read
    // =========================================================================
    // Input is written to buf0 with bit-reversed addressing
    // Output is read from the current "read" buffer

    // Bit-reversal function
    function [LOG2_N-1:0] bit_reverse;
        input [LOG2_N-1:0] addr;
        integer k;
        begin
            for (k = 0; k < LOG2_N; k = k + 1)
                bit_reverse[k] = addr[LOG2_N-1-k];
        end
    endfunction

    // Write port (bit-reversed into buf0)
    always @(posedge clk) begin
        if (wr_en && !busy) begin
            buf0_re[bit_reverse(wr_addr)] <= wr_re;
            buf0_im[bit_reverse(wr_addr)] <= wr_im;
        end
    end

    // Read port (from final output buffer)
    wire read_from_buf1 = ping; // After all stages, result is in the last-written buffer
    assign rd_re = read_from_buf1 ? buf1_re[rd_addr] : buf0_re[rd_addr];
    assign rd_im = read_from_buf1 ? buf1_im[rd_addr] : buf0_im[rd_addr];

    // =========================================================================
    // FFT computation state machine
    // =========================================================================
    localparam S_IDLE     = 3'd0;
    localparam S_STAGE    = 3'd1;
    localparam S_BFLY     = 3'd2;
    localparam S_BFLY_MUL = 3'd3;
    localparam S_BFLY_WR  = 3'd4;
    localparam S_DONE     = 3'd5;

    reg [2:0]          state;
    reg [LOG2_N-1:0]   stage;         // Current stage (0..LOG2_N-1)
    reg [LOG2_N-1:0]   bfly_idx;      // Butterfly index within stage
    reg [LOG2_N-1:0]   group;         // Group counter
    reg [LOG2_N-1:0]   pair;          // Pair counter within group

    // Butterfly operands
    reg signed [DATA_W-1:0]   a_re, a_im;    // Top input
    reg signed [DATA_W-1:0]   b_re, b_im;    // Bottom input
    reg signed [TWIDDLE_W-1:0] w_re, w_im;   // Twiddle factor

    // Butterfly products (extended width for multiplication)
    localparam PROD_W = DATA_W + TWIDDLE_W;
    reg signed [PROD_W-1:0] wb_re, wb_im;

    // Butterfly outputs
    reg signed [DATA_W-1:0] out_a_re, out_a_im;
    reg signed [DATA_W-1:0] out_b_re, out_b_im;

    // Addressing
    reg [LOG2_N-1:0] addr_top, addr_bot;
    reg [LOG2_N-1:0] twiddle_idx;

    // Stage parameters
    reg [LOG2_N-1:0] half_size;    // 2^stage
    reg [LOG2_N-1:0] full_size;    // 2^(stage+1)
    reg [LOG2_N-1:0] num_groups;

    always @(posedge clk) begin
        if (rst) begin
            state    <= S_IDLE;
            busy     <= 1'b0;
            done     <= 1'b0;
            ping     <= 1'b0;
            stage    <= {LOG2_N{1'b0}};
            group    <= {LOG2_N{1'b0}};
            pair     <= {LOG2_N{1'b0}};
        end else begin
            done <= 1'b0;

            case (state)
                S_IDLE: begin
                    if (start) begin
                        busy      <= 1'b1;
                        stage     <= {LOG2_N{1'b0}};
                        ping      <= 1'b0;
                        state     <= S_STAGE;
                    end
                end

                S_STAGE: begin
                    // Compute stage parameters
                    half_size  <= (1 << stage);
                    full_size  <= (1 << (stage + 1));
                    num_groups <= (FFT_N >> (stage + 1));
                    group      <= {LOG2_N{1'b0}};
                    pair       <= {LOG2_N{1'b0}};
                    state      <= S_BFLY;
                end

                S_BFLY: begin
                    // Calculate addresses for this butterfly
                    addr_top    <= group * full_size + pair;
                    addr_bot    <= group * full_size + pair + half_size;
                    twiddle_idx <= pair * num_groups;

                    // Read operands from current read buffer
                    if (!ping) begin
                        a_re <= buf0_re[group * full_size + pair];
                        a_im <= buf0_im[group * full_size + pair];
                        b_re <= buf0_re[group * full_size + pair + half_size];
                        b_im <= buf0_im[group * full_size + pair + half_size];
                    end else begin
                        a_re <= buf1_re[group * full_size + pair];
                        a_im <= buf1_im[group * full_size + pair];
                        b_re <= buf1_re[group * full_size + pair + half_size];
                        b_im <= buf1_im[group * full_size + pair + half_size];
                    end

                    w_re <= inverse ? twiddle_re[pair * num_groups]
                                    : twiddle_re[pair * num_groups];
                    w_im <= inverse ? (-twiddle_im[pair * num_groups])
                                    : twiddle_im[pair * num_groups];

                    state <= S_BFLY_MUL;
                end

                S_BFLY_MUL: begin
                    // Complex multiply: W * b = (w_re*b_re - w_im*b_im) + j(w_re*b_im + w_im*b_re)
                    wb_re <= (w_re * b_re - w_im * b_im) >>> (TWIDDLE_W - 1);
                    wb_im <= (w_re * b_im + w_im * b_re) >>> (TWIDDLE_W - 1);

                    state <= S_BFLY_WR;
                end

                S_BFLY_WR: begin
                    // Butterfly: a' = a + Wb, b' = a - Wb
                    // Truncate to DATA_W with scaling (>>1 to prevent overflow)
                    out_a_re = (a_re + wb_re[DATA_W-1:0]) >>> 1;
                    out_a_im = (a_im + wb_im[DATA_W-1:0]) >>> 1;
                    out_b_re = (a_re - wb_re[DATA_W-1:0]) >>> 1;
                    out_b_im = (a_im - wb_im[DATA_W-1:0]) >>> 1;

                    // Write to alternate buffer
                    if (ping) begin
                        buf0_re[addr_top] <= out_a_re;
                        buf0_im[addr_top] <= out_a_im;
                        buf0_re[addr_bot] <= out_b_re;
                        buf0_im[addr_bot] <= out_b_im;
                    end else begin
                        buf1_re[addr_top] <= out_a_re;
                        buf1_im[addr_top] <= out_a_im;
                        buf1_re[addr_bot] <= out_b_re;
                        buf1_im[addr_bot] <= out_b_im;
                    end

                    // Advance to next butterfly
                    if (pair + 1 < half_size) begin
                        pair  <= pair + 1;
                        state <= S_BFLY;
                    end else if (group + 1 < num_groups) begin
                        group <= group + 1;
                        pair  <= {LOG2_N{1'b0}};
                        state <= S_BFLY;
                    end else begin
                        // Stage complete
                        ping <= ~ping;
                        if (stage + 1 < LOG2_N) begin
                            stage <= stage + 1;
                            state <= S_STAGE;
                        end else begin
                            state <= S_DONE;
                        end
                    end
                end

                S_DONE: begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    state <= S_IDLE;
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
`default_nettype wire
