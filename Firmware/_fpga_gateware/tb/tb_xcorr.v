`default_nettype none
`timescale 1ns / 1ps
// =============================================================================
// tb_xcorr.v — Cross-correlation engine testbench
//
// Verifies that a known delayed signal produces a correlation peak at the
// correct lag position.
// =============================================================================
module tb_xcorr;

    // =========================================================================
    // Parameters
    // =========================================================================
    parameter CLK_PERIOD = 10;
    parameter FFT_N      = 64;      // Small FFT for fast simulation
    parameter DATA_W     = 16;
    parameter LOG2_N     = $clog2(FFT_N);
    parameter DELAY_SAMP = 5;       // Expected correlation peak at lag=5

    // =========================================================================
    // Signals
    // =========================================================================
    reg                     clk;
    reg                     rst;
    reg                     start;
    wire                    busy;
    wire                    done;

    reg  signed [DATA_W-1:0] ref_re, ref_im;
    reg                      ref_valid;
    wire                     ref_ready;

    reg  signed [DATA_W-1:0] test_re, test_im;
    reg                      test_valid;
    wire                     test_ready;

    wire [2*DATA_W-1:0]     xcorr_mag2;
    wire                    xcorr_valid;
    reg                     xcorr_ready;
    wire [LOG2_N-1:0]       xcorr_idx;

    // =========================================================================
    // DUT
    // =========================================================================
    xcorr_engine #(
        .FFT_N  (FFT_N),
        .DATA_W (DATA_W)
    ) u_dut (
        .clk         (clk),
        .rst         (rst),
        .start       (start),
        .busy        (busy),
        .done        (done),
        .mode_xcorr  (1'b1),
        .ref_re      (ref_re),
        .ref_im      (ref_im),
        .ref_valid   (ref_valid),
        .ref_ready   (ref_ready),
        .test_re     (test_re),
        .test_im     (test_im),
        .test_valid  (test_valid),
        .test_ready  (test_ready),
        .xcorr_mag2  (xcorr_mag2),
        .xcorr_valid (xcorr_valid),
        .xcorr_ready (xcorr_ready),
        .xcorr_idx   (xcorr_idx)
    );

    // =========================================================================
    // Clock generation
    // =========================================================================
    initial clk = 0;
    always #(CLK_PERIOD/2) clk = ~clk;

    // =========================================================================
    // Test data: reference and delayed copy
    // =========================================================================
    reg signed [DATA_W-1:0] ref_data_re [0:FFT_N-1];
    reg signed [DATA_W-1:0] ref_data_im [0:FFT_N-1];
    reg signed [DATA_W-1:0] test_data_re [0:FFT_N-1];
    reg signed [DATA_W-1:0] test_data_im [0:FFT_N-1];

    // Output capture
    reg [2*DATA_W-1:0] result_mag2 [0:FFT_N-1];
    integer result_cnt;

    // Generate test data: impulse at position 0 for ref, position DELAY_SAMP for test
    integer k;
    initial begin
        for (k = 0; k < FFT_N; k = k + 1) begin
            ref_data_re[k]  = (k == 0) ? 16'sd16384 : 16'sd0;
            ref_data_im[k]  = 16'sd0;
            test_data_re[k] = (k == DELAY_SAMP) ? 16'sd16384 : 16'sd0;
            test_data_im[k] = 16'sd0;
        end
    end

    // =========================================================================
    // Test sequence
    // =========================================================================
    integer i;
    integer errors;
    integer peak_idx;
    reg [2*DATA_W-1:0] peak_val;

    initial begin
        $dumpfile("tb_xcorr.vcd");
        $dumpvars(0, tb_xcorr);

        // Initialize
        rst         = 1;
        start       = 0;
        ref_re      = 0;
        ref_im      = 0;
        ref_valid   = 0;
        test_re     = 0;
        test_im     = 0;
        test_valid  = 0;
        xcorr_ready = 1;
        errors      = 0;
        result_cnt  = 0;
        peak_idx    = 0;
        peak_val    = 0;

        // Reset
        #(CLK_PERIOD * 10);
        rst = 0;
        #(CLK_PERIOD * 5);

        // Start cross-correlation
        $display("Starting cross-correlation (FFT_N=%0d, expected peak at lag=%0d)...",
                 FFT_N, DELAY_SAMP);
        @(posedge clk);
        start <= 1;
        @(posedge clk);
        start <= 0;

        // ----- Feed reference data -----
        $display("Loading reference data...");
        for (i = 0; i < FFT_N; i = i + 1) begin
            @(posedge clk);
            ref_re    <= ref_data_re[i];
            ref_im    <= ref_data_im[i];
            ref_valid <= 1;
            while (!ref_ready) @(posedge clk);
        end
        @(posedge clk);
        ref_valid <= 0;

        // Wait for FFT of reference to complete
        wait(u_dut.state == 4'd3); // S_LOAD_TEST
        $display("Reference FFT complete, loading test data...");

        // ----- Feed test data -----
        for (i = 0; i < FFT_N; i = i + 1) begin
            @(posedge clk);
            test_re    <= test_data_re[i];
            test_im    <= test_data_im[i];
            test_valid <= 1;
            while (!test_ready) @(posedge clk);
        end
        @(posedge clk);
        test_valid <= 0;

        // Wait for processing to complete
        $display("Waiting for xcorr processing...");
        wait(done);
        $display("Cross-correlation complete.");

        // Wait for all results to stream out
        #(CLK_PERIOD * FFT_N * 4);

        // ----- Find peak -----
        $display("Analyzing results (%0d samples captured)...", result_cnt);
        for (i = 0; i < result_cnt; i = i + 1) begin
            if (result_mag2[i] > peak_val) begin
                peak_val = result_mag2[i];
                peak_idx = i;
            end
        end

        $display("  Peak found at index %0d (magnitude^2 = %0d)", peak_idx, peak_val);
        $display("  Expected peak at index %0d", DELAY_SAMP);

        if (peak_idx == DELAY_SAMP)
            $display("  PASS: Peak at correct lag position");
        else begin
            $display("  FAIL: Peak at wrong position (expected %0d, got %0d)",
                     DELAY_SAMP, peak_idx);
            errors = errors + 1;
        end

        $display("\n=== Cross-Correlation Test: %0d errors ===", errors);
        if (errors == 0)
            $display("PASS");
        else
            $display("FAIL");

        $finish;
    end

    // =========================================================================
    // Output capture
    // =========================================================================
    always @(posedge clk) begin
        if (xcorr_valid && xcorr_ready) begin
            result_mag2[result_cnt] <= xcorr_mag2;
            result_cnt = result_cnt + 1;
        end
    end

    // =========================================================================
    // Timeout
    // =========================================================================
    initial begin
        #(CLK_PERIOD * 1000000);
        $display("ERROR: Simulation timeout");
        $finish;
    end

endmodule
`default_nettype wire
