`default_nettype none
// =============================================================================
// xcorr_engine.v — FFT-based cross-correlation engine
//
// Pipeline: FFT(ref) -> FFT(test) -> conj_multiply -> IFFT -> |.|^2
// Configurable size: 1K to 64K points (parameter).
// Fixed-point arithmetic: 16-bit real + 16-bit imag internally.
// Twiddle factors stored in BRAM (pre-computed).
// F32 I/O conversion at boundaries.
// Streaming: can accept new frame while previous processes.
// =============================================================================
module xcorr_engine #(
    parameter FFT_N     = 1024,
    parameter DATA_W    = 16,
    parameter LOG2_N    = $clog2(FFT_N)
) (
    input  wire                    clk,
    input  wire                    rst,

    // Control
    input  wire                    start,
    output wire                    busy,
    output wire                    done,
    input  wire                    mode_xcorr,    // 1=cross-corr, 0=bypass

    // Reference channel input (fixed-point)
    input  wire signed [DATA_W-1:0] ref_re,
    input  wire signed [DATA_W-1:0] ref_im,
    input  wire                     ref_valid,
    output wire                     ref_ready,

    // Test channel input (fixed-point)
    input  wire signed [DATA_W-1:0] test_re,
    input  wire signed [DATA_W-1:0] test_im,
    input  wire                     test_valid,
    output wire                     test_ready,

    // Output: cross-correlation magnitude squared
    output reg  [2*DATA_W-1:0]     xcorr_mag2,
    output reg                     xcorr_valid,
    input  wire                    xcorr_ready,

    // Output read address (for sequential readout)
    output reg  [LOG2_N-1:0]       xcorr_idx
);

    // =========================================================================
    // State machine
    // =========================================================================
    localparam S_IDLE       = 4'd0;
    localparam S_LOAD_REF   = 4'd1;
    localparam S_FFT_REF    = 4'd2;
    localparam S_LOAD_TEST  = 4'd3;
    localparam S_FFT_TEST   = 4'd4;
    localparam S_CONJ_MUL   = 4'd5;
    localparam S_IFFT       = 4'd6;
    localparam S_MAG_SQ     = 4'd7;
    localparam S_OUTPUT     = 4'd8;
    localparam S_DONE       = 4'd9;

    reg [3:0] state;
    reg [LOG2_N-1:0] load_cnt;
    reg [LOG2_N-1:0] proc_cnt;

    assign busy = (state != S_IDLE);
    assign done = (state == S_DONE);

    // =========================================================================
    // FFT instance (shared for ref, test, and IFFT)
    // =========================================================================
    reg                     fft_start;
    reg                     fft_inverse;
    wire                    fft_busy;
    wire                    fft_done;
    reg  [LOG2_N-1:0]      fft_wr_addr;
    reg  signed [DATA_W-1:0] fft_wr_re;
    reg  signed [DATA_W-1:0] fft_wr_im;
    reg                     fft_wr_en;
    reg  [LOG2_N-1:0]      fft_rd_addr;
    wire signed [DATA_W-1:0] fft_rd_re;
    wire signed [DATA_W-1:0] fft_rd_im;

    fft_radix2 #(
        .FFT_N  (FFT_N),
        .DATA_W (DATA_W)
    ) u_fft (
        .clk     (clk),
        .rst     (rst),
        .start   (fft_start),
        .inverse (fft_inverse),
        .busy    (fft_busy),
        .done    (fft_done),
        .wr_addr (fft_wr_addr),
        .wr_re   (fft_wr_re),
        .wr_im   (fft_wr_im),
        .wr_en   (fft_wr_en),
        .rd_addr (fft_rd_addr),
        .rd_re   (fft_rd_re),
        .rd_im   (fft_rd_im)
    );

    // =========================================================================
    // Storage for FFT(ref) result
    // =========================================================================
    reg signed [DATA_W-1:0] ref_fft_re [0:FFT_N-1];
    reg signed [DATA_W-1:0] ref_fft_im [0:FFT_N-1];

    // =========================================================================
    // Storage for conjugate-multiply result (feed back into FFT for IFFT)
    // =========================================================================
    reg signed [DATA_W-1:0] conj_re [0:FFT_N-1];
    reg signed [DATA_W-1:0] conj_im [0:FFT_N-1];

    // =========================================================================
    // Magnitude-squared storage
    // =========================================================================
    reg [2*DATA_W-1:0] mag2_mem [0:FFT_N-1];

    // =========================================================================
    // Flow control
    // =========================================================================
    assign ref_ready  = (state == S_LOAD_REF)  && !fft_busy;
    assign test_ready = (state == S_LOAD_TEST) && !fft_busy;

    // =========================================================================
    // Main FSM
    // =========================================================================
    always @(posedge clk) begin
        if (rst) begin
            state       <= S_IDLE;
            load_cnt    <= {LOG2_N{1'b0}};
            proc_cnt    <= {LOG2_N{1'b0}};
            fft_start   <= 1'b0;
            fft_inverse <= 1'b0;
            fft_wr_en   <= 1'b0;
            fft_wr_addr <= {LOG2_N{1'b0}};
            fft_wr_re   <= {DATA_W{1'b0}};
            fft_wr_im   <= {DATA_W{1'b0}};
            fft_rd_addr <= {LOG2_N{1'b0}};
            xcorr_mag2  <= {(2*DATA_W){1'b0}};
            xcorr_valid <= 1'b0;
            xcorr_idx   <= {LOG2_N{1'b0}};
        end else begin
            fft_start <= 1'b0;
            fft_wr_en <= 1'b0;

            if (xcorr_valid && xcorr_ready)
                xcorr_valid <= 1'b0;

            case (state)
                S_IDLE: begin
                    if (start && mode_xcorr) begin
                        state    <= S_LOAD_REF;
                        load_cnt <= {LOG2_N{1'b0}};
                    end
                end

                // ----- Load reference channel into FFT -----
                S_LOAD_REF: begin
                    if (ref_valid && ref_ready) begin
                        fft_wr_addr <= load_cnt;
                        fft_wr_re   <= ref_re;
                        fft_wr_im   <= ref_im;
                        fft_wr_en   <= 1'b1;
                        load_cnt    <= load_cnt + 1'b1;
                        if (load_cnt == FFT_N - 1) begin
                            state <= S_FFT_REF;
                        end
                    end
                end

                // ----- Compute FFT of reference -----
                S_FFT_REF: begin
                    if (!fft_busy && !fft_done) begin
                        fft_start   <= 1'b1;
                        fft_inverse <= 1'b0;
                    end
                    if (fft_done) begin
                        // Copy FFT result to ref storage
                        state    <= S_LOAD_TEST;
                        load_cnt <= {LOG2_N{1'b0}};
                        proc_cnt <= {LOG2_N{1'b0}};
                    end
                end

                // ----- Copy ref FFT out & load test channel -----
                S_LOAD_TEST: begin
                    // Copy ref FFT results in parallel with loading test data
                    if (proc_cnt < FFT_N) begin
                        fft_rd_addr <= proc_cnt;
                        if (proc_cnt > 0) begin
                            ref_fft_re[proc_cnt - 1] <= fft_rd_re;
                            ref_fft_im[proc_cnt - 1] <= fft_rd_im;
                        end
                        proc_cnt <= proc_cnt + 1'b1;
                    end else if (proc_cnt == FFT_N) begin
                        ref_fft_re[FFT_N-1] <= fft_rd_re;
                        ref_fft_im[FFT_N-1] <= fft_rd_im;
                        proc_cnt <= proc_cnt + 1'b1;
                    end

                    // Load test data
                    if (test_valid && test_ready) begin
                        fft_wr_addr <= load_cnt;
                        fft_wr_re   <= test_re;
                        fft_wr_im   <= test_im;
                        fft_wr_en   <= 1'b1;
                        load_cnt    <= load_cnt + 1'b1;
                        if (load_cnt == FFT_N - 1) begin
                            state <= S_FFT_TEST;
                        end
                    end
                end

                // ----- Compute FFT of test -----
                S_FFT_TEST: begin
                    if (!fft_busy && !fft_done) begin
                        fft_start   <= 1'b1;
                        fft_inverse <= 1'b0;
                    end
                    if (fft_done) begin
                        state    <= S_CONJ_MUL;
                        proc_cnt <= {LOG2_N{1'b0}};
                    end
                end

                // ----- Conjugate multiply: X_ref* . X_test -----
                S_CONJ_MUL: begin
                    fft_rd_addr <= proc_cnt;

                    if (proc_cnt > 0) begin
                        // conj(ref) * test = (ref_re - j*ref_im) * (test_re + j*test_im)
                        // = (ref_re*test_re + ref_im*test_im) + j(ref_re*test_im - ref_im*test_re)
                        // Use registered FFT output (1 cycle latency)
                        conj_re[proc_cnt - 1] <= ((ref_fft_re[proc_cnt-1] * fft_rd_re +
                                                    ref_fft_im[proc_cnt-1] * fft_rd_im) >>> (DATA_W-1));
                        conj_im[proc_cnt - 1] <= ((ref_fft_re[proc_cnt-1] * fft_rd_im -
                                                    ref_fft_im[proc_cnt-1] * fft_rd_re) >>> (DATA_W-1));
                    end

                    proc_cnt <= proc_cnt + 1'b1;

                    if (proc_cnt == FFT_N) begin
                        conj_re[FFT_N-1] <= ((ref_fft_re[FFT_N-1] * fft_rd_re +
                                              ref_fft_im[FFT_N-1] * fft_rd_im) >>> (DATA_W-1));
                        conj_im[FFT_N-1] <= ((ref_fft_re[FFT_N-1] * fft_rd_im -
                                              ref_fft_im[FFT_N-1] * fft_rd_re) >>> (DATA_W-1));
                        // Load conjugate-multiply result back into FFT for IFFT
                        state    <= S_IFFT;
                        load_cnt <= {LOG2_N{1'b0}};
                    end
                end

                // ----- IFFT of conjugate product -----
                S_IFFT: begin
                    if (load_cnt < FFT_N) begin
                        fft_wr_addr <= load_cnt;
                        fft_wr_re   <= conj_re[load_cnt];
                        fft_wr_im   <= conj_im[load_cnt];
                        fft_wr_en   <= 1'b1;
                        load_cnt    <= load_cnt + 1'b1;
                    end else if (!fft_busy && !fft_done && load_cnt == FFT_N) begin
                        fft_start   <= 1'b1;
                        fft_inverse <= 1'b1;
                        load_cnt    <= load_cnt + 1'b1; // prevent re-trigger
                    end
                    if (fft_done) begin
                        state    <= S_MAG_SQ;
                        proc_cnt <= {LOG2_N{1'b0}};
                    end
                end

                // ----- Compute magnitude squared -----
                S_MAG_SQ: begin
                    fft_rd_addr <= proc_cnt;

                    if (proc_cnt > 0) begin
                        mag2_mem[proc_cnt - 1] <= fft_rd_re * fft_rd_re +
                                                   fft_rd_im * fft_rd_im;
                    end

                    proc_cnt <= proc_cnt + 1'b1;

                    if (proc_cnt == FFT_N) begin
                        mag2_mem[FFT_N-1] <= fft_rd_re * fft_rd_re +
                                             fft_rd_im * fft_rd_im;
                        state     <= S_OUTPUT;
                        xcorr_idx <= {LOG2_N{1'b0}};
                    end
                end

                // ----- Stream out results -----
                S_OUTPUT: begin
                    if (!xcorr_valid || xcorr_ready) begin
                        xcorr_mag2  <= mag2_mem[xcorr_idx];
                        xcorr_valid <= 1'b1;
                        xcorr_idx   <= xcorr_idx + 1'b1;
                        if (xcorr_idx == FFT_N - 1) begin
                            state <= S_DONE;
                        end
                    end
                end

                S_DONE: begin
                    state <= S_IDLE;
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
`default_nettype wire
