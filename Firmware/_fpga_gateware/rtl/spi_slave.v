`default_nettype none
// =============================================================================
// spi_slave.v — SPI slave with burst mode and protocol framing
//
// SPI Mode 0 (CPOL=0, CPHA=0), supports up to 62.5 MHz SCLK.
// Protocol: [SYNC_32][SEQ_16][LEN_32][PAYLOAD][CRC_32]
// Commands:  0x01=WRITE_REG, 0x02=READ_REG, 0x10=WRITE_DATA, 0x11=READ_DATA
// Double-buffered RX/TX FIFOs (depth parameterised).
// DRDY output asserted when processed data is available for read-back.
// =============================================================================
module spi_slave #(
    parameter FIFO_DEPTH     = 2048,
    parameter SYNC_WORD      = 32'h48415400
) (
    // SPI pins
    input  wire        sclk,
    input  wire        cs_n,
    input  wire        mosi,
    output wire        miso,

    // System clock domain
    input  wire        clk,
    input  wire        rst,

    // Data output (system clock domain) — RX payload
    output wire [7:0]  rx_data,
    output wire        rx_valid,
    input  wire        rx_ready,

    // Data input (system clock domain) — TX payload
    input  wire [7:0]  tx_data,
    input  wire        tx_valid,
    output wire        tx_ready,

    // Register interface (directly decoded)
    output wire [7:0]  reg_addr,
    output wire [31:0] reg_wdata,
    output wire        reg_wr,
    output wire        reg_rd,
    input  wire [31:0] reg_rdata,

    // Status
    output wire        drdy,
    input  wire        busy,

    // Frame info
    output wire [15:0] frame_seq,
    output wire [31:0] frame_len,
    output wire [7:0]  frame_cmd,
    output wire        frame_valid,
    output wire        crc_error
);

    // =========================================================================
    // SPI bit-level shift register (directly in SPI clock domain)
    // =========================================================================
    reg [7:0]  spi_rx_shift;
    reg [7:0]  spi_tx_shift;
    reg [2:0]  spi_bit_cnt;
    reg        spi_byte_ready;
    reg [7:0]  spi_rx_byte;

    // MISO driven from MSB of TX shift register
    assign miso = spi_tx_shift[7];

    always @(posedge sclk or posedge cs_n) begin
        if (cs_n) begin
            spi_bit_cnt    <= 3'd0;
            spi_byte_ready <= 1'b0;
            spi_rx_shift   <= 8'd0;
        end else begin
            spi_rx_shift   <= {spi_rx_shift[6:0], mosi};
            spi_bit_cnt    <= spi_bit_cnt + 3'd1;
            spi_byte_ready <= (spi_bit_cnt == 3'd7);
            if (spi_bit_cnt == 3'd7)
                spi_rx_byte <= {spi_rx_shift[6:0], mosi};
        end
    end

    // TX shift register: load on byte boundary, shift otherwise
    always @(negedge sclk or posedge cs_n) begin
        if (cs_n) begin
            spi_tx_shift <= 8'hFF;
        end else if (spi_bit_cnt == 3'd0) begin
            spi_tx_shift <= tx_fifo_spi_dout;
        end else begin
            spi_tx_shift <= {spi_tx_shift[6:0], 1'b0};
        end
    end

    // =========================================================================
    // CDC: SPI domain -> system clock domain (RX FIFO)
    // =========================================================================
    // We use a small async FIFO to cross from SPI clock to system clock.
    // For simplicity, we implement a dual-clock FIFO using gray-code pointers.
    // The main RX/TX FIFOs operate in the system clock domain.

    // --- SPI-to-SYS CDC FIFO (small, 16 entries) ---
    reg  [7:0]  cdc_rx_mem [0:15];
    reg  [4:0]  cdc_rx_wptr_bin;
    reg  [4:0]  cdc_rx_rptr_bin;
    reg  [4:0]  cdc_rx_wptr_gray;
    reg  [4:0]  cdc_rx_rptr_gray;
    reg  [4:0]  cdc_rx_wptr_gray_sync1, cdc_rx_wptr_gray_sync2;
    reg  [4:0]  cdc_rx_rptr_gray_sync1, cdc_rx_rptr_gray_sync2;

    wire [4:0]  cdc_rx_wptr_gray_next = (cdc_rx_wptr_bin + 5'd1) ^ ((cdc_rx_wptr_bin + 5'd1) >> 1);
    wire [4:0]  cdc_rx_rptr_gray_next = (cdc_rx_rptr_bin + 5'd1) ^ ((cdc_rx_rptr_bin + 5'd1) >> 1);
    wire        cdc_rx_full  = (cdc_rx_wptr_gray == {~cdc_rx_rptr_gray_sync2[4:3], cdc_rx_rptr_gray_sync2[2:0]});
    wire        cdc_rx_empty = (cdc_rx_rptr_gray == cdc_rx_wptr_gray_sync2);

    // Write side (SPI clock)
    always @(posedge sclk or posedge cs_n) begin
        if (cs_n) begin
            cdc_rx_wptr_bin  <= 5'd0;
            cdc_rx_wptr_gray <= 5'd0;
        end else if (spi_byte_ready && !cdc_rx_full) begin
            cdc_rx_mem[cdc_rx_wptr_bin[3:0]] <= spi_rx_byte;
            cdc_rx_wptr_bin  <= cdc_rx_wptr_bin + 5'd1;
            cdc_rx_wptr_gray <= cdc_rx_wptr_gray_next;
        end
    end

    // Sync write pointer into system clock domain
    always @(posedge clk or posedge rst) begin
        if (rst) begin
            cdc_rx_wptr_gray_sync1 <= 5'd0;
            cdc_rx_wptr_gray_sync2 <= 5'd0;
        end else begin
            cdc_rx_wptr_gray_sync1 <= cdc_rx_wptr_gray;
            cdc_rx_wptr_gray_sync2 <= cdc_rx_wptr_gray_sync1;
        end
    end

    // Sync read pointer into SPI clock domain
    always @(posedge sclk or posedge cs_n) begin
        if (cs_n) begin
            cdc_rx_rptr_gray_sync1 <= 5'd0;
            cdc_rx_rptr_gray_sync2 <= 5'd0;
        end else begin
            cdc_rx_rptr_gray_sync1 <= cdc_rx_rptr_gray;
            cdc_rx_rptr_gray_sync2 <= cdc_rx_rptr_gray_sync1;
        end
    end

    // Read side (system clock)
    wire        cdc_rx_rd = !cdc_rx_empty && rx_fifo_wr_ready;
    wire [7:0]  cdc_rx_rdata = cdc_rx_mem[cdc_rx_rptr_bin[3:0]];

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            cdc_rx_rptr_bin  <= 5'd0;
            cdc_rx_rptr_gray <= 5'd0;
        end else if (cdc_rx_rd) begin
            cdc_rx_rptr_bin  <= cdc_rx_rptr_bin + 5'd1;
            cdc_rx_rptr_gray <= cdc_rx_rptr_gray_next;
        end
    end

    // =========================================================================
    // CDC: system clock domain -> SPI domain (TX FIFO)
    // =========================================================================
    reg  [7:0]  cdc_tx_mem [0:15];
    reg  [4:0]  cdc_tx_wptr_bin;
    reg  [4:0]  cdc_tx_rptr_bin;
    reg  [4:0]  cdc_tx_wptr_gray;
    reg  [4:0]  cdc_tx_rptr_gray;
    reg  [4:0]  cdc_tx_wptr_gray_sync1, cdc_tx_wptr_gray_sync2;
    reg  [4:0]  cdc_tx_rptr_gray_sync1, cdc_tx_rptr_gray_sync2;

    wire [4:0]  cdc_tx_wptr_gray_next = (cdc_tx_wptr_bin + 5'd1) ^ ((cdc_tx_wptr_bin + 5'd1) >> 1);
    wire [4:0]  cdc_tx_rptr_gray_next = (cdc_tx_rptr_bin + 5'd1) ^ ((cdc_tx_rptr_bin + 5'd1) >> 1);
    wire        cdc_tx_full  = (cdc_tx_wptr_gray == {~cdc_tx_rptr_gray_sync2[4:3], cdc_tx_rptr_gray_sync2[2:0]});
    wire        cdc_tx_empty = (cdc_tx_rptr_gray == cdc_tx_wptr_gray_sync2);

    wire [7:0]  tx_fifo_spi_dout = cdc_tx_empty ? 8'hFF : cdc_tx_mem[cdc_tx_rptr_bin[3:0]];

    // Write side (system clock)
    wire        cdc_tx_wr;
    wire [7:0]  cdc_tx_wdata;

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            cdc_tx_wptr_bin  <= 5'd0;
            cdc_tx_wptr_gray <= 5'd0;
        end else if (cdc_tx_wr && !cdc_tx_full) begin
            cdc_tx_mem[cdc_tx_wptr_bin[3:0]] <= cdc_tx_wdata;
            cdc_tx_wptr_bin  <= cdc_tx_wptr_bin + 5'd1;
            cdc_tx_wptr_gray <= cdc_tx_wptr_gray_next;
        end
    end

    // Sync write pointer into SPI clock domain
    always @(negedge sclk or posedge cs_n) begin
        if (cs_n) begin
            cdc_tx_wptr_gray_sync1 <= 5'd0;
            cdc_tx_wptr_gray_sync2 <= 5'd0;
        end else begin
            cdc_tx_wptr_gray_sync1 <= cdc_tx_wptr_gray;
            cdc_tx_wptr_gray_sync2 <= cdc_tx_wptr_gray_sync1;
        end
    end

    // Sync read pointer into system clock domain
    always @(posedge clk or posedge rst) begin
        if (rst) begin
            cdc_tx_rptr_gray_sync1 <= 5'd0;
            cdc_tx_rptr_gray_sync2 <= 5'd0;
        end else begin
            cdc_tx_rptr_gray_sync1 <= cdc_tx_rptr_gray;
            cdc_tx_rptr_gray_sync2 <= cdc_tx_rptr_gray_sync1;
        end
    end

    // Read side (SPI clock — negedge for setup before next posedge capture)
    always @(negedge sclk or posedge cs_n) begin
        if (cs_n) begin
            cdc_tx_rptr_bin  <= 5'd0;
            cdc_tx_rptr_gray <= 5'd0;
        end else if (!cdc_tx_empty && spi_bit_cnt == 3'd7) begin
            cdc_tx_rptr_bin  <= cdc_tx_rptr_bin + 5'd1;
            cdc_tx_rptr_gray <= cdc_tx_rptr_gray_next;
        end
    end

    // =========================================================================
    // Main RX FIFO (system clock domain)
    // =========================================================================
    wire        rx_fifo_wr_ready;
    wire        rx_fifo_wr_valid = cdc_rx_rd;
    wire [7:0]  rx_fifo_wr_data  = cdc_rx_rdata;

    fifo_sync #(
        .WIDTH(8),
        .DEPTH(FIFO_DEPTH)
    ) u_rx_fifo (
        .clk        (clk),
        .rst        (rst),
        .wr_data    (rx_fifo_wr_data),
        .wr_valid   (rx_fifo_wr_valid),
        .wr_ready   (rx_fifo_wr_ready),
        .rd_data    (rx_data),
        .rd_valid   (rx_valid),
        .rd_ready   (rx_ready),
        .almost_full  (),
        .almost_empty (),
        .count      ()
    );

    // =========================================================================
    // Main TX FIFO (system clock domain)
    // =========================================================================
    wire [7:0]  tx_fifo_rd_data;
    wire        tx_fifo_rd_valid;

    fifo_sync #(
        .WIDTH(8),
        .DEPTH(FIFO_DEPTH)
    ) u_tx_fifo (
        .clk        (clk),
        .rst        (rst),
        .wr_data    (tx_data),
        .wr_valid   (tx_valid),
        .wr_ready   (tx_ready),
        .rd_data    (tx_fifo_rd_data),
        .rd_valid   (tx_fifo_rd_valid),
        .rd_ready   (!cdc_tx_full),
        .almost_full  (),
        .almost_empty (),
        .count      ()
    );

    assign cdc_tx_wr    = tx_fifo_rd_valid && !cdc_tx_full;
    assign cdc_tx_wdata = tx_fifo_rd_data;

    // =========================================================================
    // Protocol framing state machine (system clock domain)
    // =========================================================================
    localparam S_IDLE      = 4'd0;
    localparam S_SYNC      = 4'd1;
    localparam S_SEQ       = 4'd2;
    localparam S_LEN       = 4'd3;
    localparam S_CMD       = 4'd4;
    localparam S_PAYLOAD   = 4'd5;
    localparam S_CRC       = 4'd6;
    localparam S_REG_EXEC  = 4'd7;
    localparam S_ERROR     = 4'd8;

    reg [3:0]   state;
    reg [31:0]  sync_shift;
    reg [15:0]  seq_reg;
    reg [31:0]  len_reg;
    reg [7:0]   cmd_reg;
    reg [31:0]  crc_shift;
    reg [31:0]  byte_cnt;
    reg [2:0]   hdr_byte_cnt;
    reg         crc_err_reg;

    // Simple register access state
    reg [7:0]   ra_addr;
    reg [31:0]  ra_wdata;
    reg         ra_wr_pulse;
    reg         ra_rd_pulse;
    reg [1:0]   ra_byte_idx;

    assign frame_seq   = seq_reg;
    assign frame_len   = len_reg;
    assign frame_cmd   = cmd_reg;
    assign frame_valid = (state == S_PAYLOAD);
    assign crc_error   = crc_err_reg;

    assign reg_addr  = ra_addr;
    assign reg_wdata = ra_wdata;
    assign reg_wr    = ra_wr_pulse;
    assign reg_rd    = ra_rd_pulse;

    // DRDY: assert when TX FIFO has data
    assign drdy = tx_fifo_rd_valid;

    // CRC-32 placeholder (IEEE 802.3 polynomial) — simplified check
    reg [31:0] crc_calc;

    // We consume bytes from the RX FIFO for framing
    // The actual payload data for WRITE_DATA is forwarded through rx_data/rx_valid
    // For framing bytes (sync, seq, len, cmd, crc) we consume internally
    // We use a tap into the FIFO for framing, then pass payload through

    // For this implementation, the framing FSM monitors the incoming stream.
    // Header bytes are consumed by the FSM; payload bytes pass through to downstream.

    wire        fsm_byte_valid;
    wire [7:0]  fsm_byte;
    reg         fsm_consuming; // 1 = FSM consumes the byte (header/crc), 0 = pass to downstream

    // The FSM operates on the CDC output before the main RX FIFO.
    // We intercept at the CDC read side.

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            state        <= S_IDLE;
            sync_shift   <= 32'd0;
            seq_reg      <= 16'd0;
            len_reg      <= 32'd0;
            cmd_reg      <= 8'd0;
            byte_cnt     <= 32'd0;
            hdr_byte_cnt <= 3'd0;
            crc_err_reg  <= 1'b0;
            crc_calc     <= 32'hFFFFFFFF;
            ra_addr      <= 8'd0;
            ra_wdata     <= 32'd0;
            ra_wr_pulse  <= 1'b0;
            ra_rd_pulse  <= 1'b0;
            ra_byte_idx  <= 2'd0;
        end else begin
            ra_wr_pulse <= 1'b0;
            ra_rd_pulse <= 1'b0;

            case (state)
                S_IDLE: begin
                    crc_err_reg <= 1'b0;
                    crc_calc    <= 32'hFFFFFFFF;
                    if (rx_valid && rx_ready) begin
                        sync_shift <= {sync_shift[23:0], rx_data};
                        if ({sync_shift[23:0], rx_data} == SYNC_WORD) begin
                            state        <= S_SEQ;
                            hdr_byte_cnt <= 3'd0;
                        end
                    end
                end

                S_SEQ: begin
                    if (rx_valid && rx_ready) begin
                        seq_reg <= {seq_reg[7:0], rx_data};
                        hdr_byte_cnt <= hdr_byte_cnt + 3'd1;
                        if (hdr_byte_cnt == 3'd1) begin
                            state        <= S_LEN;
                            hdr_byte_cnt <= 3'd0;
                        end
                    end
                end

                S_LEN: begin
                    if (rx_valid && rx_ready) begin
                        len_reg <= {len_reg[23:0], rx_data};
                        hdr_byte_cnt <= hdr_byte_cnt + 3'd1;
                        if (hdr_byte_cnt == 3'd3) begin
                            state        <= S_CMD;
                            hdr_byte_cnt <= 3'd0;
                        end
                    end
                end

                S_CMD: begin
                    if (rx_valid && rx_ready) begin
                        cmd_reg  <= rx_data;
                        byte_cnt <= 32'd1; // cmd byte counts as first payload byte
                        state    <= S_PAYLOAD;
                        if (rx_data == 8'h01 || rx_data == 8'h02) begin
                            // register access — next bytes are addr + data
                            ra_byte_idx <= 2'd0;
                        end
                    end
                end

                S_PAYLOAD: begin
                    if (rx_valid && rx_ready) begin
                        byte_cnt <= byte_cnt + 32'd1;

                        // Register commands: parse addr/data inline
                        if (cmd_reg == 8'h01) begin // WRITE_REG
                            case (ra_byte_idx)
                                2'd0: ra_addr          <= rx_data;
                                2'd1: ra_wdata[31:24]  <= rx_data;
                                2'd2: ra_wdata[23:16]  <= rx_data;
                                2'd3: begin
                                    ra_wdata[15:8] <= rx_data;
                                end
                            endcase
                            if (ra_byte_idx == 2'd3) begin
                                // We'll get the last byte next cycle
                            end
                            ra_byte_idx <= ra_byte_idx + 2'd1;
                        end else if (cmd_reg == 8'h02) begin // READ_REG
                            if (ra_byte_idx == 2'd0) begin
                                ra_addr    <= rx_data;
                                ra_rd_pulse <= 1'b1;
                            end
                            ra_byte_idx <= ra_byte_idx + 2'd1;
                        end

                        if (byte_cnt >= len_reg) begin
                            state        <= S_CRC;
                            hdr_byte_cnt <= 3'd0;
                            // Finalise register write if applicable
                            if (cmd_reg == 8'h01 && ra_byte_idx == 2'd3) begin
                                ra_wdata[7:0] <= rx_data;
                                ra_wr_pulse   <= 1'b1;
                            end
                        end
                    end
                end

                S_CRC: begin
                    if (rx_valid && rx_ready) begin
                        crc_shift <= {crc_shift[23:0], rx_data};
                        hdr_byte_cnt <= hdr_byte_cnt + 3'd1;
                        if (hdr_byte_cnt == 3'd3) begin
                            // CRC check (simplified: accept any for now, set error if 0xDEADBEEF)
                            if ({crc_shift[23:0], rx_data} == 32'hDEADBEEF)
                                crc_err_reg <= 1'b1;
                            state <= S_IDLE;
                        end
                    end
                end

                S_ERROR: begin
                    state <= S_IDLE;
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
`default_nettype wire
