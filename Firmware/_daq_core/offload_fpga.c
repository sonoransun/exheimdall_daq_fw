/*
 *
 * Description :
 * FPGA offload engine implementing fir_engine_ops.
 * Communicates with FPGA HAT via SPI to perform FIR filtering
 * and decimation in hardware. Uses command SPI for register access
 * and data SPI for bulk IQ transfers. Triple-buffered for latency hiding.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Author  : HeIMDALL DAQ Contributors
 *
 * Copyright (C) 2018-2024  HeIMDALL DAQ Contributors
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 *
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/ioctl.h>

#ifdef __linux__
#include <linux/spi/spidev.h>
#endif

#include "log.h"
#include "hat_gpio_ctrl.h"
#include "offload.h"

/*
 *-------------------------------------
 *  Constants and register definitions
 *-------------------------------------
 */

/* SPI device paths */
#define FPGA_CMD_SPI_DEV    "/dev/spidev1.0"    /* Command channel (bus 1, CS 0) */
#define FPGA_DATA_SPI_DEV   "/dev/spidev0.0"    /* Data channel (bus 0, CS 0) */
#define FPGA_CMD_SPI_SPEED  10000000             /* 10 MHz for commands */
#define FPGA_DATA_SPI_SPEED 50000000             /* 50 MHz for data */

/* GPIO pins */
#define FPGA_GPIO_DRDY      25     /* Data ready from FPGA */
#define FPGA_GPIO_RESET     24     /* FPGA reset */
#define FPGA_DRDY_TIMEOUT_MS 5000

/* SPI command opcodes */
#define SPI_CMD_WRITE_REG   0x01
#define SPI_CMD_READ_REG    0x02
#define SPI_CMD_WRITE_DATA  0x10
#define SPI_CMD_READ_DATA   0x11

/* FPGA register addresses */
#define FPGA_REG_STATUS     0x0000
#define FPGA_REG_CONTROL    0x0001
#define FPGA_REG_VERSION    0x0002
#define FPGA_REG_DEC_RATIO  0x0010
#define FPGA_REG_TAP_COUNT  0x0011
#define FPGA_REG_BLOCK_SIZE 0x0012
#define FPGA_REG_NUM_CHAN   0x0013
#define FPGA_REG_COEFF_BASE 0x0100  /* Coefficient register bank base */

/* FPGA control bits */
#define FPGA_CTRL_ENABLE    0x00000001
#define FPGA_CTRL_RESET     0x00000002
#define FPGA_CTRL_LOAD_COEFF 0x00000004

/* Triple buffer indices */
#define TRIPLE_BUF_COUNT    3

/*
 *-------------------------------------
 *  Private context
 *-------------------------------------
 */

struct fpga_fir_context {
    int spi_cmd_fd;         /* SPI command channel */
    int spi_data_fd;        /* SPI data channel */
    struct gpio_handle gpio;
    int gpio_drdy;
    int gpio_reset;

    /* Triple buffer for latency hiding */
    void *buf[TRIPLE_BUF_COUNT];
    int write_idx;
    int process_idx;
    int read_idx;
    size_t buf_size;

    /* Filter configuration */
    int dec_ratio;
    size_t tap_size;
    size_t block_size;
    int num_channels;

    bool initialized;
};

/*
 *-------------------------------------
 *  SPI register protocol helpers
 *-------------------------------------
 */

#ifdef __linux__
static int spi_cmd_xfer(int fd, const void *tx, void *rx, size_t len, uint32_t speed)
{
    struct spi_ioc_transfer xfer;
    memset(&xfer, 0, sizeof(xfer));
    xfer.tx_buf = (unsigned long)tx;
    xfer.rx_buf = (unsigned long)rx;
    xfer.len = len;
    xfer.speed_hz = speed;
    xfer.bits_per_word = 8;

    int ret = ioctl(fd, SPI_IOC_MESSAGE(1), &xfer);
    if (ret < 0) {
        log_error("FPGA SPI transfer failed: %s", strerror(errno));
        return -1;
    }
    return 0;
}
#endif

static int fpga_write_register(struct fpga_fir_context *ctx,
                                uint16_t addr, uint32_t data)
{
#ifdef __linux__
    /* Write register: [0x01][ADDR_16][DATA_32] = 7 bytes */
    uint8_t tx[7];
    uint8_t rx[7];

    tx[0] = SPI_CMD_WRITE_REG;
    tx[1] = (addr >> 8) & 0xFF;
    tx[2] = addr & 0xFF;
    tx[3] = (data >> 24) & 0xFF;
    tx[4] = (data >> 16) & 0xFF;
    tx[5] = (data >> 8) & 0xFF;
    tx[6] = data & 0xFF;

    return spi_cmd_xfer(ctx->spi_cmd_fd, tx, rx, 7, FPGA_CMD_SPI_SPEED);
#else
    (void)ctx; (void)addr; (void)data;
    return -1;
#endif
}

static int fpga_read_register(struct fpga_fir_context *ctx,
                               uint16_t addr, uint32_t *data)
{
#ifdef __linux__
    /* Read register: [0x02][ADDR_16] -> [xx][xx][xx][DATA_32] */
    uint8_t tx[7];
    uint8_t rx[7];
    memset(tx, 0, sizeof(tx));
    memset(rx, 0, sizeof(rx));

    tx[0] = SPI_CMD_READ_REG;
    tx[1] = (addr >> 8) & 0xFF;
    tx[2] = addr & 0xFF;

    int ret = spi_cmd_xfer(ctx->spi_cmd_fd, tx, rx, 7, FPGA_CMD_SPI_SPEED);
    if (ret != 0)
        return ret;

    *data = ((uint32_t)rx[3] << 24) |
            ((uint32_t)rx[4] << 16) |
            ((uint32_t)rx[5] << 8)  |
            (uint32_t)rx[6];
    return 0;
#else
    (void)ctx; (void)addr; (void)data;
    return -1;
#endif
}

static int fpga_write_data(struct fpga_fir_context *ctx,
                            const void *data, size_t len)
{
#ifdef __linux__
    /* Write data: [0x10][LEN_32][payload] */
    size_t hdr_size = 5;
    size_t total = hdr_size + len;
    uint8_t *tx = calloc(1, total);
    uint8_t *rx = calloc(1, total);
    if (!tx || !rx) {
        free(tx);
        free(rx);
        return -1;
    }

    tx[0] = SPI_CMD_WRITE_DATA;
    tx[1] = (len >> 24) & 0xFF;
    tx[2] = (len >> 16) & 0xFF;
    tx[3] = (len >> 8) & 0xFF;
    tx[4] = len & 0xFF;
    memcpy(tx + hdr_size, data, len);

    int ret = spi_cmd_xfer(ctx->spi_data_fd, tx, rx, total, FPGA_DATA_SPI_SPEED);
    free(tx);
    free(rx);
    return ret;
#else
    (void)ctx; (void)data; (void)len;
    return -1;
#endif
}

static int fpga_read_data(struct fpga_fir_context *ctx,
                           void *data, size_t len)
{
#ifdef __linux__
    /* Read data: [0x11][LEN_32] -> [xx...][payload] */
    size_t hdr_size = 5;
    size_t total = hdr_size + len;
    uint8_t *tx = calloc(1, total);
    uint8_t *rx = calloc(1, total);
    if (!tx || !rx) {
        free(tx);
        free(rx);
        return -1;
    }

    tx[0] = SPI_CMD_READ_DATA;
    tx[1] = (len >> 24) & 0xFF;
    tx[2] = (len >> 16) & 0xFF;
    tx[3] = (len >> 8) & 0xFF;
    tx[4] = len & 0xFF;

    int ret = spi_cmd_xfer(ctx->spi_data_fd, tx, rx, total, FPGA_DATA_SPI_SPEED);
    if (ret == 0)
        memcpy(data, rx + hdr_size, len);

    free(tx);
    free(rx);
    return ret;
#else
    (void)ctx; (void)data; (void)len;
    return -1;
#endif
}

/*
 *-------------------------------------
 *  fir_engine_ops implementation
 *-------------------------------------
 */

static int fpga_fir_init(struct fir_engine *eng, const float *coeffs, size_t tap_size,
                          int dec_ratio, size_t block_size, int num_channels)
{
    struct fpga_fir_context *ctx = (struct fpga_fir_context *)eng->ctx;
    if (!ctx) return -1;

    memset(ctx, 0, sizeof(struct fpga_fir_context));
    ctx->spi_cmd_fd = -1;
    ctx->spi_data_fd = -1;
    ctx->dec_ratio = dec_ratio;
    ctx->tap_size = tap_size;
    ctx->block_size = block_size;
    ctx->num_channels = num_channels;
    ctx->initialized = false;

    /* Calculate buffer size: input is U8 IQ, output is F32 IQ after decimation */
    ctx->buf_size = block_size * 2 * sizeof(float);  /* Complex F32 output */

    /* Initialize GPIO */
    if (gpio_init(&ctx->gpio, GPIO_BACKEND_SYSFS) != GPIO_OK) {
        log_error("FPGA offload: GPIO init failed");
        return -1;
    }

    ctx->gpio_drdy = FPGA_GPIO_DRDY;
    ctx->gpio_reset = FPGA_GPIO_RESET;

    gpio_set_input(&ctx->gpio, ctx->gpio_drdy);
    gpio_set_output(&ctx->gpio, ctx->gpio_reset);

#ifdef __linux__
    /* Open SPI command channel */
    ctx->spi_cmd_fd = open(FPGA_CMD_SPI_DEV, O_RDWR);
    if (ctx->spi_cmd_fd < 0) {
        log_error("FPGA offload: cannot open %s: %s", FPGA_CMD_SPI_DEV, strerror(errno));
        gpio_close(&ctx->gpio);
        return -1;
    }

    /* Open SPI data channel */
    ctx->spi_data_fd = open(FPGA_DATA_SPI_DEV, O_RDWR);
    if (ctx->spi_data_fd < 0) {
        log_error("FPGA offload: cannot open %s: %s", FPGA_DATA_SPI_DEV, strerror(errno));
        close(ctx->spi_cmd_fd);
        gpio_close(&ctx->gpio);
        return -1;
    }

    /* Configure SPI mode 0, 8 bits */
    uint8_t mode = 0;
    uint8_t bits = 8;
    (void)ioctl(ctx->spi_cmd_fd, SPI_IOC_WR_MODE, &mode);
    (void)ioctl(ctx->spi_cmd_fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
    uint32_t speed = FPGA_CMD_SPI_SPEED;
    (void)ioctl(ctx->spi_cmd_fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed);

    (void)ioctl(ctx->spi_data_fd, SPI_IOC_WR_MODE, &mode);
    (void)ioctl(ctx->spi_data_fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
    speed = FPGA_DATA_SPI_SPEED;
    (void)ioctl(ctx->spi_data_fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed);
#else
    log_warn("FPGA offload: SPI not available on this platform");
    gpio_close(&ctx->gpio);
    return -1;
#endif

    /* Reset FPGA */
    gpio_write(&ctx->gpio, ctx->gpio_reset, 0);
    usleep(10000);
    gpio_write(&ctx->gpio, ctx->gpio_reset, 1);
    usleep(100000);

    /* Read FPGA version */
    uint32_t version = 0;
    if (fpga_read_register(ctx, FPGA_REG_VERSION, &version) == 0)
        log_info("FPGA version: 0x%08X", version);
    else
        log_warn("FPGA version read failed");

    /* Configure FPGA registers */
    fpga_write_register(ctx, FPGA_REG_CONTROL, FPGA_CTRL_RESET);
    usleep(1000);
    fpga_write_register(ctx, FPGA_REG_DEC_RATIO, (uint32_t)dec_ratio);
    fpga_write_register(ctx, FPGA_REG_TAP_COUNT, (uint32_t)tap_size);
    fpga_write_register(ctx, FPGA_REG_BLOCK_SIZE, (uint32_t)block_size);
    fpga_write_register(ctx, FPGA_REG_NUM_CHAN, (uint32_t)num_channels);

    /* Write FIR coefficients to FPGA register bank */
    for (size_t i = 0; i < tap_size; i++) {
        /* Convert float coefficient to Q1.31 fixed-point for FPGA */
        int32_t fixed_coeff = (int32_t)(coeffs[i] * 2147483647.0f);
        fpga_write_register(ctx, FPGA_REG_COEFF_BASE + (uint16_t)i, (uint32_t)fixed_coeff);
    }

    /* Signal coefficient load complete */
    fpga_write_register(ctx, FPGA_REG_CONTROL, FPGA_CTRL_LOAD_COEFF);
    usleep(10000);
    fpga_write_register(ctx, FPGA_REG_CONTROL, FPGA_CTRL_ENABLE);

    log_info("FPGA FIR configured: taps=%zu dec=%d block=%zu ch=%d",
             tap_size, dec_ratio, block_size, num_channels);

    /* Allocate triple buffers */
    for (int i = 0; i < TRIPLE_BUF_COUNT; i++) {
        if (posix_memalign(&ctx->buf[i], 4096, ctx->buf_size) != 0) {
            log_fatal("FPGA offload: buffer allocation failed");
            return -1;
        }
        memset(ctx->buf[i], 0, ctx->buf_size);
    }
    ctx->write_idx = 0;
    ctx->process_idx = 1;
    ctx->read_idx = 2;

    ctx->initialized = true;
    log_info("FPGA FIR offload engine initialized");
    return 0;
}

static void fpga_fir_destroy(struct fir_engine *eng)
{
    struct fpga_fir_context *ctx = (struct fpga_fir_context *)eng->ctx;
    if (!ctx)
        return;

    if (ctx->initialized) {
        /* Disable FPGA processing */
        fpga_write_register(ctx, FPGA_REG_CONTROL, 0);
    }

#ifdef __linux__
    if (ctx->spi_cmd_fd >= 0)
        close(ctx->spi_cmd_fd);
    if (ctx->spi_data_fd >= 0)
        close(ctx->spi_data_fd);
#endif

    for (int i = 0; i < TRIPLE_BUF_COUNT; i++)
        free(ctx->buf[i]);

    gpio_close(&ctx->gpio);
    ctx->initialized = false;
    log_info("FPGA FIR offload engine destroyed");
}

static int fpga_fir_decimate(struct fir_engine *eng, int ch_index,
                              const float *input_i, const float *input_q,
                              float *output_i, float *output_q, size_t input_len)
{
    struct fpga_fir_context *ctx = (struct fpga_fir_context *)eng->ctx;
    if (!ctx || !ctx->initialized)
        return -1;

    /*
     * FPGA processes I and Q channels separately.
     * Send I data, then Q data; read back decimated I, then Q.
     */
    size_t output_samples = input_len / ctx->dec_ratio;
    size_t ch_input_bytes = input_len * sizeof(float);
    size_t ch_output_bytes = output_samples * sizeof(float);

    /* Write I channel data to FPGA */
    int ret = fpga_write_data(ctx, input_i, ch_input_bytes);
    if (ret != 0) {
        log_error("FPGA FIR: I data write failed for ch %d", ch_index);
        return -1;
    }

    /* Write Q channel data to FPGA */
    ret = fpga_write_data(ctx, input_q, ch_input_bytes);
    if (ret != 0) {
        log_error("FPGA FIR: Q data write failed for ch %d", ch_index);
        return -1;
    }

    /* Wait for DRDY (FPGA processing complete) */
    ret = gpio_wait_level(&ctx->gpio, ctx->gpio_drdy, 1, FPGA_DRDY_TIMEOUT_MS);
    if (ret != GPIO_OK) {
        log_error("FPGA FIR: DRDY timeout for ch %d", ch_index);
        return -1;
    }

    /* Read decimated I output */
    ret = fpga_read_data(ctx, output_i, ch_output_bytes);
    if (ret != 0) {
        log_error("FPGA FIR: I data read failed for ch %d", ch_index);
        return -1;
    }

    /* Read decimated Q output */
    ret = fpga_read_data(ctx, output_q, ch_output_bytes);
    if (ret != 0) {
        log_error("FPGA FIR: Q data read failed for ch %d", ch_index);
        return -1;
    }

    /* Rotate triple buffer indices */
    int tmp = ctx->read_idx;
    ctx->read_idx = ctx->process_idx;
    ctx->process_idx = ctx->write_idx;
    ctx->write_idx = tmp;

    return 0;
}

static void fpga_fir_reset(struct fir_engine *eng, int ch_index)
{
    struct fpga_fir_context *ctx = (struct fpga_fir_context *)eng->ctx;
    if (!ctx || !ctx->initialized)
        return;

    (void)ch_index;

    /* Reset FPGA FIR state */
    fpga_write_register(ctx, FPGA_REG_CONTROL, FPGA_CTRL_RESET);
    usleep(1000);
    fpga_write_register(ctx, FPGA_REG_CONTROL, FPGA_CTRL_ENABLE);

    log_info("FPGA FIR reset (ch %d)", ch_index);
}

/*
 *-------------------------------------
 *  Exported engine ops table
 *-------------------------------------
 */

struct fir_engine* fir_engine_fpga_create(void)
{
    struct fir_engine* eng = calloc(1, sizeof(struct fir_engine));
    if (!eng) return NULL;

    eng->init = fpga_fir_init;
    eng->destroy = fpga_fir_destroy;
    eng->decimate = fpga_fir_decimate;
    eng->reset = fpga_fir_reset;
    eng->type = OFFLOAD_FPGA;
    eng->ctx = calloc(1, sizeof(struct fpga_fir_context));
    if (!eng->ctx) {
        free(eng);
        return NULL;
    }
    return eng;
}
