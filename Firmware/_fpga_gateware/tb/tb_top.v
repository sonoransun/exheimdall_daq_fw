`default_nettype none
`timescale 1ns / 1ps
// =============================================================================
// tb_top.v — Top-level system testbench
//
// SPI master model drives SCLK/MOSI/CS to the FPGA top module.
// Sends configuration registers, then IQ data.
// Captures MISO output and writes to file for verification.
// Uses $readmemh for test vectors.
// =============================================================================
module tb_top;

    // =========================================================================
    // Parameters
    // =========================================================================
    parameter CLK_PERIOD  = 40;    // 25 MHz oscillator
    parameter SCLK_PERIOD = 32;    // ~31.25 MHz SPI clock
    parameter NUM_IQ_BYTES = 64;   // Number of interleaved IQ bytes to send

    // =========================================================================
    // Signals
    // =========================================================================
    reg         clk_25m;
    reg         spi_sclk;
    reg         spi_cs_n;
    reg         spi_mosi;
    wire        spi_miso;
    wire        drdy;
    reg         busy_in;
    reg         reset_n;
    wire [7:0]  led;

    // =========================================================================
    // DUT
    // =========================================================================
    top #(
        .MAX_CHANNELS   (5),
        .MAX_TAP_SIZE   (128),
        .MAX_CPI        (1048576),
        .SPI_FIFO_DEPTH (64),
        .FFT_N          (64),
        .DATA_W         (18),
        .COEFF_W        (18),
        .ACC_W          (48)
    ) u_dut (
        .clk_25m   (clk_25m),
        .spi_sclk  (spi_sclk),
        .spi_cs_n  (spi_cs_n),
        .spi_mosi  (spi_mosi),
        .spi_miso  (spi_miso),
        .drdy      (drdy),
        .busy_in   (busy_in),
        .reset_n   (reset_n),
        .led       (led)
    );

    // =========================================================================
    // Clock generation (25 MHz)
    // =========================================================================
    initial clk_25m = 0;
    always #(CLK_PERIOD/2) clk_25m = ~clk_25m;

    // =========================================================================
    // Test data
    // =========================================================================
    reg [7:0] iq_data [0:NUM_IQ_BYTES-1];
    reg [7:0] miso_capture [0:4095];
    integer   miso_cnt;

    initial begin
        $readmemh("reference/test_iq_data.hex", iq_data);
    end

    // =========================================================================
    // SPI master tasks
    // =========================================================================
    task spi_begin;
        begin
            spi_cs_n = 1'b0;
            #(SCLK_PERIOD);
        end
    endtask

    task spi_end;
        begin
            #(SCLK_PERIOD);
            spi_cs_n = 1'b1;
            #(SCLK_PERIOD * 4);
        end
    endtask

    task spi_xfer_byte;
        input  [7:0] tx;
        output [7:0] rx;
        integer i;
        reg [7:0] rx_shift;
        begin
            rx_shift = 8'd0;
            for (i = 7; i >= 0; i = i - 1) begin
                spi_mosi = tx[i];
                #(SCLK_PERIOD / 2);
                spi_sclk = 1'b1;
                rx_shift = {rx_shift[6:0], spi_miso};
                #(SCLK_PERIOD / 2);
                spi_sclk = 1'b0;
            end
            rx = rx_shift;
        end
    endtask

    task spi_send_byte;
        input [7:0] tx;
        reg [7:0] dummy;
        begin
            spi_xfer_byte(tx, dummy);
        end
    endtask

    task spi_send_word32;
        input [31:0] data;
        begin
            spi_send_byte(data[31:24]);
            spi_send_byte(data[23:16]);
            spi_send_byte(data[15:8]);
            spi_send_byte(data[7:0]);
        end
    endtask

    task spi_send_word16;
        input [15:0] data;
        begin
            spi_send_byte(data[15:8]);
            spi_send_byte(data[7:0]);
        end
    endtask

    // Send a frame header
    task spi_frame_header;
        input [15:0] seq;
        input [7:0]  cmd;
        input [31:0] len;
        begin
            spi_send_word32(32'h48415400); // SYNC
            spi_send_word16(seq);          // SEQ
            spi_send_word32(len);          // LEN
            spi_send_byte(cmd);            // CMD
        end
    endtask

    // =========================================================================
    // Test sequence
    // =========================================================================
    integer i;
    integer fd;
    reg [7:0] rx_byte;

    initial begin
        `ifdef VCD_OUTPUT
            $dumpfile("tb_top.vcd");
            $dumpvars(0, tb_top);
        `endif

        // Initialize
        spi_sclk = 0;
        spi_cs_n = 1;
        spi_mosi = 0;
        busy_in  = 0;
        reset_n  = 0;
        miso_cnt = 0;

        // Hold reset for a while
        #(CLK_PERIOD * 20);
        reset_n = 1;
        #(CLK_PERIOD * 20);

        $display("=== HeIMDALL FPGA Top-Level Test ===");
        $display("LED state after reset: 0x%02h", led);
        $display("  PLL lock (LED0): %b", led[0]);

        // -----------------------------------------------------------------
        // Step 1: Configure decimation ratio
        // -----------------------------------------------------------------
        $display("\n--- Step 1: Set decimation ratio = 2 ---");
        spi_begin();
        spi_frame_header(16'h0001, 8'h01, 32'd6);
        spi_send_byte(8'h08);              // Reg addr 0x08 = decimation ratio
        spi_send_word32(32'h00000002);      // Ratio = 2
        spi_send_word32(32'h00000000);      // CRC
        spi_end();
        #(CLK_PERIOD * 20);

        // -----------------------------------------------------------------
        // Step 2: Configure tap count
        // -----------------------------------------------------------------
        $display("--- Step 2: Set tap count = 8 ---");
        spi_begin();
        spi_frame_header(16'h0002, 8'h01, 32'd6);
        spi_send_byte(8'h0C);              // Reg addr 0x0C = tap count
        spi_send_word32(32'h00000008);      // 8 taps
        spi_send_word32(32'h00000000);      // CRC
        spi_end();
        #(CLK_PERIOD * 20);

        // -----------------------------------------------------------------
        // Step 3: Load FIR coefficients (8 simple averaging coefficients)
        // -----------------------------------------------------------------
        $display("--- Step 3: Load FIR coefficients ---");
        for (i = 0; i < 8; i = i + 1) begin
            spi_begin();
            spi_frame_header(16'h0010 + i, 8'h01, 32'd6);
            spi_send_byte(8'h20 + (i * 4));     // Coeff register address
            spi_send_word32(32'h00002000);        // Fixed-point coefficient (~0.125)
            spi_send_word32(32'h00000000);        // CRC
            spi_end();
            #(CLK_PERIOD * 10);
        end

        // -----------------------------------------------------------------
        // Step 4: Send IQ data
        // -----------------------------------------------------------------
        $display("--- Step 4: Send %0d bytes of IQ data ---", NUM_IQ_BYTES);
        spi_begin();
        spi_frame_header(16'h0100, 8'h10, NUM_IQ_BYTES + 1);
        for (i = 0; i < NUM_IQ_BYTES; i = i + 1) begin
            spi_xfer_byte(iq_data[i], rx_byte);
            miso_capture[miso_cnt] = rx_byte;
            miso_cnt = miso_cnt + 1;
        end
        spi_send_word32(32'h00000000);      // CRC
        spi_end();

        // Wait for processing
        #(CLK_PERIOD * 500);

        // -----------------------------------------------------------------
        // Step 5: Read back processed data
        // -----------------------------------------------------------------
        $display("--- Step 5: Read back processed data ---");
        if (drdy) begin
            spi_begin();
            spi_frame_header(16'h0200, 8'h11, 32'd1);
            // Clock out some bytes to read TX FIFO
            for (i = 0; i < 32; i = i + 1) begin
                spi_xfer_byte(8'h00, rx_byte);
                miso_capture[miso_cnt] = rx_byte;
                miso_cnt = miso_cnt + 1;
                if (rx_byte != 8'hFF)
                    $display("  RX[%0d] = 0x%02h", i, rx_byte);
            end
            spi_send_word32(32'h00000000);  // CRC
            spi_end();
        end else begin
            $display("  No DRDY — no processed data available yet");
        end

        // -----------------------------------------------------------------
        // Step 6: Read status register
        // -----------------------------------------------------------------
        $display("--- Step 6: Read status register ---");
        spi_begin();
        spi_frame_header(16'h0300, 8'h02, 32'd2);
        spi_send_byte(8'h04);              // Reg addr 0x04 = status
        spi_send_word32(32'h00000000);      // CRC
        spi_end();
        #(CLK_PERIOD * 20);

        // -----------------------------------------------------------------
        // Write output file
        // -----------------------------------------------------------------
        $display("\n--- Writing captured MISO data to output file ---");
        fd = $fopen("tb_top_output.hex", "w");
        for (i = 0; i < miso_cnt; i = i + 1)
            $fwrite(fd, "%02h\n", miso_capture[i]);
        $fclose(fd);
        $display("Wrote %0d bytes to tb_top_output.hex", miso_cnt);

        // -----------------------------------------------------------------
        // Summary
        // -----------------------------------------------------------------
        $display("\nLED state at end: 0x%02h", led);
        $display("=== Top-Level Test Complete ===");

        #(CLK_PERIOD * 10);
        $finish;
    end

    // =========================================================================
    // Timeout
    // =========================================================================
    initial begin
        #(CLK_PERIOD * 500000);
        $display("ERROR: Simulation timeout");
        $finish;
    end

endmodule
`default_nettype wire
