/*
 *
 * Description :
 * SPI+DMA transport driver for FPGA HAT communication.
 * Implements the transport_ops interface for SPI bus transfers with
 * framing, CRC-32, and GPIO data-ready signaling.
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
#include <poll.h>

#ifdef __linux__
#include <linux/spi/spidev.h>
#endif

#include "log.h"
#include "transport.h"

/*
 *-------------------------------------
 *  SPI framing constants
 *-------------------------------------
 */
#define SPI_SYNC_WORD      0x48415400
#define SPI_MAX_SPEED_HZ   50000000   /* 50 MHz default */
#define SPI_DEFAULT_MODE   0          /* SPI mode 0 (CPOL=0, CPHA=0) */
#define SPI_BITS_PER_WORD  8
#define SPI_FRAME_OVERHEAD 14         /* sync(4) + seq(2) + len(4) + crc(4) */
#define GPIO_DRDY_PIN      25         /* Default data-ready GPIO */
#define GPIO_BUSY_PIN      8          /* Default busy GPIO */
#define GPIO_POLL_TIMEOUT_MS 5000

/*
 *-------------------------------------
 *  CRC-32 (ISO-HDLC / ITU-T V.42)
 *-------------------------------------
 */

static uint32_t crc32_table[256];
static bool crc32_table_init = false;

static void crc32_generate_table(void)
{
    uint32_t poly = 0xEDB88320;
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t crc = i;
        for (int j = 0; j < 8; j++) {
            if (crc & 1)
                crc = (crc >> 1) ^ poly;
            else
                crc >>= 1;
        }
        crc32_table[i] = crc;
    }
    crc32_table_init = true;
}

static uint32_t crc32_calc(const void *data, size_t len)
{
    if (!crc32_table_init)
        crc32_generate_table();

    const uint8_t *p = (const uint8_t *)data;
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < len; i++)
        crc = (crc >> 8) ^ crc32_table[(crc ^ p[i]) & 0xFF];
    return crc ^ 0xFFFFFFFF;
}

/*
 *-------------------------------------
 *  Private state
 *-------------------------------------
 */

struct spi_transport_priv {
    int spi_fd;             /* /dev/spidevX.Y */
    int gpio_drdy_fd;       /* GPIO value fd for poll() */
    int gpio_busy_fd;       /* GPIO value fd for busy */
    uint32_t spi_speed_hz;
    void *tx_buf[2];        /* Double-buffered TX (page-aligned) */
    void *rx_buf[2];        /* Double-buffered RX */
    int active_tx;
    int active_rx;
    bool terminated;
    uint16_t seq_num;
    int gpio_drdy_pin;
    int gpio_busy_pin;
    size_t buffer_size;
};

/*
 *-------------------------------------
 *  GPIO sysfs helpers
 *-------------------------------------
 */

static int gpio_export(int pin)
{
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d", pin);
    if (access(path, F_OK) == 0)
        return 0;  /* already exported */

    int fd = open("/sys/class/gpio/export", O_WRONLY);
    if (fd < 0) {
        log_error("GPIO export open failed: %s", strerror(errno));
        return -1;
    }
    char buf[16];
    int len = snprintf(buf, sizeof(buf), "%d", pin);
    int ret = (write(fd, buf, len) == len) ? 0 : -1;
    close(fd);
    if (ret != 0)
        log_error("GPIO export write failed for pin %d: %s", pin, strerror(errno));
    return ret;
}

static int gpio_set_direction(int pin, const char *dir)
{
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/direction", pin);
    int fd = open(path, O_WRONLY);
    if (fd < 0) {
        log_error("GPIO direction open failed for pin %d: %s", pin, strerror(errno));
        return -1;
    }
    int ret = (write(fd, dir, strlen(dir)) > 0) ? 0 : -1;
    close(fd);
    return ret;
}

static int gpio_set_edge(int pin, const char *edge)
{
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/edge", pin);
    int fd = open(path, O_WRONLY);
    if (fd < 0) {
        log_error("GPIO edge open failed for pin %d: %s", pin, strerror(errno));
        return -1;
    }
    int ret = (write(fd, edge, strlen(edge)) > 0) ? 0 : -1;
    close(fd);
    return ret;
}

static int gpio_open_value(int pin)
{
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/value", pin);
    int fd = open(path, O_RDONLY | O_NONBLOCK);
    if (fd < 0)
        log_error("GPIO value open failed for pin %d: %s", pin, strerror(errno));
    return fd;
}

static void gpio_unexport(int pin)
{
    int fd = open("/sys/class/gpio/unexport", O_WRONLY);
    if (fd < 0)
        return;
    char buf[16];
    int len = snprintf(buf, sizeof(buf), "%d", pin);
    (void)write(fd, buf, len);
    close(fd);
}

static int gpio_wait_rising(int fd, int timeout_ms)
{
    /* Clear any pending interrupt */
    char val;
    lseek(fd, 0, SEEK_SET);
    (void)read(fd, &val, 1);

    struct pollfd pfd;
    pfd.fd = fd;
    pfd.events = POLLPRI | POLLERR;

    int ret = poll(&pfd, 1, timeout_ms);
    if (ret < 0) {
        log_error("GPIO poll error: %s", strerror(errno));
        return -1;
    }
    if (ret == 0) {
        log_warn("GPIO DRDY timeout after %d ms", timeout_ms);
        return -1;
    }
    /* Consume the event */
    lseek(fd, 0, SEEK_SET);
    (void)read(fd, &val, 1);
    return 0;
}

/*
 *-------------------------------------
 *  SPI transfer helper
 *-------------------------------------
 */

#ifdef __linux__
static int spi_xfer(int fd, const void *tx, void *rx, size_t len, uint32_t speed_hz)
{
    struct spi_ioc_transfer xfer;
    memset(&xfer, 0, sizeof(xfer));
    xfer.tx_buf = (unsigned long)tx;
    xfer.rx_buf = (unsigned long)rx;
    xfer.len = len;
    xfer.speed_hz = speed_hz;
    xfer.bits_per_word = SPI_BITS_PER_WORD;

    int ret = ioctl(fd, SPI_IOC_MESSAGE(1), &xfer);
    if (ret < 0) {
        log_error("SPI transfer failed: %s", strerror(errno));
        return -1;
    }
    return 0;
}
#endif

/*
 *-------------------------------------
 *  transport_ops implementation
 *-------------------------------------
 */

static int spi_init_common(struct transport_handle *th)
{
    struct spi_transport_priv *priv = calloc(1, sizeof(struct spi_transport_priv));
    if (!priv) {
        log_fatal("SPI transport: allocation failed");
        return -1;
    }

    priv->spi_fd = -1;
    priv->gpio_drdy_fd = -1;
    priv->gpio_busy_fd = -1;
    priv->spi_speed_hz = SPI_MAX_SPEED_HZ;
    priv->active_tx = 0;
    priv->active_rx = 0;
    priv->terminated = false;
    priv->seq_num = 0;
    priv->gpio_drdy_pin = GPIO_DRDY_PIN;
    priv->gpio_busy_pin = GPIO_BUSY_PIN;
    priv->buffer_size = th->buffer_size + SPI_FRAME_OVERHEAD;

    th->priv = priv;

#ifdef __linux__
    /* Parse SPI device path from channel_name, default /dev/spidev0.0 */
    const char *spi_dev = "/dev/spidev0.0";
    if (th->channel_name[0] != '\0')
        spi_dev = th->channel_name;

    priv->spi_fd = open(spi_dev, O_RDWR);
    if (priv->spi_fd < 0) {
        log_error("SPI open %s failed: %s", spi_dev, strerror(errno));
        free(priv);
        th->priv = NULL;
        return -1;
    }

    /* Configure SPI mode */
    uint8_t mode = SPI_DEFAULT_MODE;
    if (ioctl(priv->spi_fd, SPI_IOC_WR_MODE, &mode) < 0) {
        log_error("SPI set mode failed: %s", strerror(errno));
        close(priv->spi_fd);
        free(priv);
        th->priv = NULL;
        return -1;
    }

    uint8_t bits = SPI_BITS_PER_WORD;
    (void)ioctl(priv->spi_fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
    (void)ioctl(priv->spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &priv->spi_speed_hz);

    log_info("SPI transport: opened %s, speed %u Hz, mode %u",
             spi_dev, priv->spi_speed_hz, mode);
#else
    log_warn("SPI transport: not available on this platform (non-Linux)");
#endif

    /* Allocate page-aligned double buffers */
    for (int i = 0; i < 2; i++) {
        if (posix_memalign(&priv->tx_buf[i], 4096, priv->buffer_size) != 0) {
            log_fatal("SPI transport: tx buffer allocation failed");
            return -1;
        }
        memset(priv->tx_buf[i], 0, priv->buffer_size);

        if (posix_memalign(&priv->rx_buf[i], 4096, priv->buffer_size) != 0) {
            log_fatal("SPI transport: rx buffer allocation failed");
            return -1;
        }
        memset(priv->rx_buf[i], 0, priv->buffer_size);
    }

    /* Setup GPIOs */
#ifdef __linux__
    if (gpio_export(priv->gpio_drdy_pin) == 0) {
        gpio_set_direction(priv->gpio_drdy_pin, "in");
        gpio_set_edge(priv->gpio_drdy_pin, "rising");
        priv->gpio_drdy_fd = gpio_open_value(priv->gpio_drdy_pin);
    }
    if (gpio_export(priv->gpio_busy_pin) == 0) {
        gpio_set_direction(priv->gpio_busy_pin, "out");
    }
#endif

    return 0;
}

int spi_init_producer(struct transport_handle *th)
{
    log_info("SPI transport: initializing producer");
    return spi_init_common(th);
}

int spi_init_consumer(struct transport_handle *th)
{
    log_info("SPI transport: initializing consumer");
    return spi_init_common(th);
}

void spi_destroy(struct transport_handle *th)
{
    if (!th || !th->priv)
        return;

    struct spi_transport_priv *priv = (struct spi_transport_priv *)th->priv;
    priv->terminated = true;

#ifdef __linux__
    if (priv->spi_fd >= 0)
        close(priv->spi_fd);
    if (priv->gpio_drdy_fd >= 0)
        close(priv->gpio_drdy_fd);
    if (priv->gpio_busy_fd >= 0)
        close(priv->gpio_busy_fd);

    gpio_unexport(priv->gpio_drdy_pin);
    gpio_unexport(priv->gpio_busy_pin);
#endif

    for (int i = 0; i < 2; i++) {
        free(priv->tx_buf[i]);
        free(priv->rx_buf[i]);
    }

    free(priv);
    th->priv = NULL;
    log_info("SPI transport: destroyed");
}

int spi_get_write_buf(struct transport_handle *th, void **buf_ptr)
{
    struct spi_transport_priv *priv = (struct spi_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

    /* Return pointer into the inactive TX buffer, past the frame header area */
    int idx = priv->active_tx ^ 1;
    uint8_t *frame = (uint8_t *)priv->tx_buf[idx];
    /* Payload starts after: sync(4) + seq(2) + len(4) = 10 bytes */
    *buf_ptr = frame + 10;
    return idx;
}

int spi_submit_write(struct transport_handle *th, int buf_index)
{
    struct spi_transport_priv *priv = (struct spi_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

    uint8_t *frame = (uint8_t *)priv->tx_buf[buf_index];
    uint32_t payload_len = (uint32_t)th->buffer_size;

    /* Build frame header: [SYNC_32][SEQ_16][LEN_32][payload][CRC_32] */
    uint32_t sync = SPI_SYNC_WORD;
    memcpy(frame + 0, &sync, 4);
    memcpy(frame + 4, &priv->seq_num, 2);
    memcpy(frame + 6, &payload_len, 4);

    /* Calculate CRC over header(10) + payload */
    uint32_t total = 10 + payload_len;
    uint32_t crc = crc32_calc(frame, total);
    memcpy(frame + total, &crc, 4);

    priv->seq_num++;

#ifdef __linux__
    /* Full-duplex SPI transfer */
    int ret = spi_xfer(priv->spi_fd, frame, priv->rx_buf[buf_index],
                       total + 4, priv->spi_speed_hz);
    if (ret != 0) {
        log_error("SPI submit write failed");
        return -1;
    }
#else
    log_warn("SPI submit_write: no-op on non-Linux platform");
#endif

    priv->active_tx = buf_index;
    th->total_bytes += payload_len;
    th->total_frames++;
    return 0;
}

int spi_get_read_buf(struct transport_handle *th, void **buf_ptr)
{
    struct spi_transport_priv *priv = (struct spi_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

#ifdef __linux__
    /* Wait for DRDY GPIO assertion */
    if (priv->gpio_drdy_fd >= 0) {
        if (gpio_wait_rising(priv->gpio_drdy_fd, GPIO_POLL_TIMEOUT_MS) != 0) {
            log_warn("SPI get_read_buf: DRDY wait failed");
            return -1;
        }
    }

    /* Issue SPI read into RX buffer */
    int idx = priv->active_rx;
    size_t xfer_len = th->buffer_size + SPI_FRAME_OVERHEAD;
    memset(priv->tx_buf[idx], 0, xfer_len);  /* TX zeros for read */
    int ret = spi_xfer(priv->spi_fd, priv->tx_buf[idx], priv->rx_buf[idx],
                       xfer_len, priv->spi_speed_hz);
    if (ret != 0) {
        log_error("SPI read transfer failed");
        return -1;
    }

    /* Validate received frame */
    uint8_t *frame = (uint8_t *)priv->rx_buf[idx];
    uint32_t rx_sync;
    memcpy(&rx_sync, frame, 4);
    if (rx_sync != SPI_SYNC_WORD) {
        log_error("SPI frame sync mismatch: 0x%08X", rx_sync);
        return -1;
    }

    uint32_t rx_len;
    memcpy(&rx_len, frame + 6, 4);
    uint32_t rx_crc;
    memcpy(&rx_crc, frame + 10 + rx_len, 4);
    uint32_t calc_crc = crc32_calc(frame, 10 + rx_len);
    if (rx_crc != calc_crc) {
        log_error("SPI CRC mismatch: got 0x%08X, expected 0x%08X", rx_crc, calc_crc);
        return -1;
    }

    /* Return pointer to payload portion */
    *buf_ptr = frame + 10;
    return idx;
#else
    (void)buf_ptr;
    log_warn("SPI get_read_buf: not available on non-Linux platform");
    return -1;
#endif
}

int spi_release_read(struct transport_handle *th, int buf_index)
{
    struct spi_transport_priv *priv = (struct spi_transport_priv *)th->priv;
    (void)buf_index;
    priv->active_rx ^= 1;
    return 0;
}

void spi_send_terminate(struct transport_handle *th)
{
    struct spi_transport_priv *priv = (struct spi_transport_priv *)th->priv;
    priv->terminated = true;
    log_info("SPI transport: terminate signaled");
}

/*
 *-------------------------------------
 *  Exported ops table
 *-------------------------------------
 */

static const struct transport_ops spi_transport_ops = {
    .init_producer  = spi_init_producer,
    .init_consumer  = spi_init_consumer,
    .destroy        = spi_destroy,
    .get_write_buf  = spi_get_write_buf,
    .submit_write   = spi_submit_write,
    .get_read_buf   = spi_get_read_buf,
    .release_read   = spi_release_read,
    .send_terminate = spi_send_terminate,
};

const struct transport_ops* transport_spi_get_ops(void)
{
    return &spi_transport_ops;
}
