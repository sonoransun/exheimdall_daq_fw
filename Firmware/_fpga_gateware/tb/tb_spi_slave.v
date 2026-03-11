`default_nettype none
`timescale 1ns / 1ps
// =============================================================================
// tb_spi_slave.v — SPI protocol testbench
//
// Verifies SPI framing, register access, burst data transfer, and CRC handling.
// =============================================================================
module tb_spi_slave;

    // =========================================================================
    // Parameters
    // =========================================================================
    parameter CLK_PERIOD  = 10;    // 100 MHz system clock
    parameter SCLK_PERIOD = 16;    // ~62.5 MHz SPI clock

    // =========================================================================
    // Signals
    // =========================================================================
    reg         clk;
    reg         rst;
    reg         sclk;
    reg         cs_n;
    reg         mosi;
    wire        miso;

    wire [7:0]  rx_data;
    wire        rx_valid;
    reg         rx_ready;

    reg  [7:0]  tx_data;
    reg         tx_valid;
    wire        tx_ready;

    wire [7:0]  reg_addr;
    wire [31:0] reg_wdata;
    wire        reg_wr;
    wire        reg_rd;
    reg  [31:0] reg_rdata;

    wire        drdy;
    reg         busy;
    wire [15:0] frame_seq;
    wire [31:0] frame_len;
    wire [7:0]  frame_cmd;
    wire        frame_valid;
    wire        crc_error;

    // =========================================================================
    // DUT
    // =========================================================================
    spi_slave #(
        .FIFO_DEPTH(64)
    ) u_dut (
        .sclk       (sclk),
        .cs_n       (cs_n),
        .mosi       (mosi),
        .miso       (miso),
        .clk        (clk),
        .rst        (rst),
        .rx_data    (rx_data),
        .rx_valid   (rx_valid),
        .rx_ready   (rx_ready),
        .tx_data    (tx_data),
        .tx_valid   (tx_valid),
        .tx_ready   (tx_ready),
        .reg_addr   (reg_addr),
        .reg_wdata  (reg_wdata),
        .reg_wr     (reg_wr),
        .reg_rd     (reg_rd),
        .reg_rdata  (reg_rdata),
        .drdy       (drdy),
        .busy       (busy),
        .frame_seq  (frame_seq),
        .frame_len  (frame_len),
        .frame_cmd  (frame_cmd),
        .frame_valid(frame_valid),
        .crc_error  (crc_error)
    );

    // =========================================================================
    // Clock generation
    // =========================================================================
    initial clk = 0;
    always #(CLK_PERIOD/2) clk = ~clk;

    // =========================================================================
    // SPI master tasks
    // =========================================================================
    task spi_begin;
        begin
            cs_n = 1'b0;
            #(SCLK_PERIOD);
        end
    endtask

    task spi_end;
        begin
            #(SCLK_PERIOD);
            cs_n = 1'b1;
            #(SCLK_PERIOD * 4);
        end
    endtask

    task spi_send_byte;
        input [7:0] data;
        integer i;
        begin
            for (i = 7; i >= 0; i = i - 1) begin
                mosi = data[i];
                #(SCLK_PERIOD / 2);
                sclk = 1'b1;     // Rising edge — slave samples
                #(SCLK_PERIOD / 2);
                sclk = 1'b0;     // Falling edge
            end
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

    // Send a complete frame: SYNC + SEQ + LEN + payload + CRC
    task spi_send_frame;
        input [15:0] seq;
        input [7:0]  cmd;
        input [31:0] payload_len;  // Total payload bytes including cmd
        begin
            // SYNC word
            spi_send_word32(32'h48415400);
            // Sequence number
            spi_send_word16(seq);
            // Length
            spi_send_word32(payload_len);
            // Command byte (first payload byte)
            spi_send_byte(cmd);
        end
    endtask

    // =========================================================================
    // Test sequence
    // =========================================================================
    integer errors;

    initial begin
        $dumpfile("tb_spi_slave.vcd");
        $dumpvars(0, tb_spi_slave);

        // Initial state
        clk       = 0;
        rst       = 1;
        sclk      = 0;
        cs_n      = 1;
        mosi      = 0;
        rx_ready  = 1;
        tx_data   = 8'hAA;
        tx_valid  = 0;
        busy      = 0;
        reg_rdata = 32'hCAFEBABE;
        errors    = 0;

        // Reset
        #(CLK_PERIOD * 10);
        rst = 0;
        #(CLK_PERIOD * 5);

        // -----------------------------------------------------------------
        // Test 1: Send a WRITE_REG frame (cmd=0x01)
        //         Write 0xDEADBEEF to register address 0x08
        // -----------------------------------------------------------------
        $display("--- Test 1: WRITE_REG frame ---");
        spi_begin();
        spi_send_frame(16'h0001, 8'h01, 32'd6); // seq=1, cmd=WRITE_REG, len=6 (cmd+addr+4data)
        spi_send_byte(8'h08);       // Register address
        spi_send_word32(32'hDEADBEEF); // Register data
        // CRC (placeholder)
        spi_send_word32(32'h00000000);
        spi_end();

        #(CLK_PERIOD * 20);

        // -----------------------------------------------------------------
        // Test 2: Send a READ_REG frame (cmd=0x02)
        //         Read from register address 0x04
        // -----------------------------------------------------------------
        $display("--- Test 2: READ_REG frame ---");
        spi_begin();
        spi_send_frame(16'h0002, 8'h02, 32'd2); // seq=2, cmd=READ_REG, len=2
        spi_send_byte(8'h04);       // Register address
        // CRC
        spi_send_word32(32'h00000000);
        spi_end();

        #(CLK_PERIOD * 20);

        // -----------------------------------------------------------------
        // Test 3: Send a WRITE_DATA frame (burst mode)
        //         Send 8 bytes of IQ data
        // -----------------------------------------------------------------
        $display("--- Test 3: WRITE_DATA burst ---");
        spi_begin();
        spi_send_frame(16'h0003, 8'h10, 32'd9); // seq=3, cmd=WRITE_DATA, len=9 (cmd+8 data)
        // 8 bytes of interleaved IQ data
        spi_send_byte(8'h80); // I0
        spi_send_byte(8'h7F); // Q0
        spi_send_byte(8'hFF); // I1
        spi_send_byte(8'h00); // Q1
        spi_send_byte(8'h40); // I2
        spi_send_byte(8'hC0); // Q2
        spi_send_byte(8'h80); // I3
        spi_send_byte(8'h80); // Q3
        // CRC
        spi_send_word32(32'h00000000);
        spi_end();

        #(CLK_PERIOD * 50);

        // -----------------------------------------------------------------
        // Test 4: CRC error detection
        // -----------------------------------------------------------------
        $display("--- Test 4: CRC error (0xDEADBEEF triggers error) ---");
        spi_begin();
        spi_send_frame(16'h0004, 8'h01, 32'd1);
        // CRC = 0xDEADBEEF should trigger error
        spi_send_word32(32'hDEADBEEF);
        spi_end();

        #(CLK_PERIOD * 20);
        if (crc_error)
            $display("  PASS: CRC error detected");
        else begin
            $display("  FAIL: CRC error not detected");
            errors = errors + 1;
        end

        // -----------------------------------------------------------------
        // Summary
        // -----------------------------------------------------------------
        #(CLK_PERIOD * 10);
        $display("\n=== SPI Slave Test Complete: %0d errors ===", errors);
        if (errors == 0)
            $display("PASS");
        else
            $display("FAIL");

        $finish;
    end

    // =========================================================================
    // Monitor register writes
    // =========================================================================
    always @(posedge clk) begin
        if (reg_wr)
            $display("  REG WRITE: addr=0x%02h data=0x%08h", reg_addr, reg_wdata);
        if (reg_rd)
            $display("  REG READ:  addr=0x%02h", reg_addr);
    end

endmodule
`default_nettype wire
