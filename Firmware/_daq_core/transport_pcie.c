/*
 *
 * Description :
 * PCIe transport driver using memory-mapped BAR access and XDMA channels.
 * Implements the transport_ops interface for PCIe-attached FPGA cards.
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
#include <sys/mman.h>
#include <sys/stat.h>

#include "log.h"
#include "transport.h"

/*
 *-------------------------------------
 *  Constants
 *-------------------------------------
 */
#define PCIE_DEFAULT_BAR_DEV    "/dev/uio0"
#define PCIE_DEFAULT_BAR_SIZE   (64 * 1024)     /* 64 KB BAR0 */
#define PCIE_DEFAULT_H2C_DEV    "/dev/xdma0_h2c_0"
#define PCIE_DEFAULT_C2H_DEV    "/dev/xdma0_c2h_0"

/* Doorbell register offsets in BAR0 (device-specific, configurable) */
#define PCIE_REG_STATUS         0x0000
#define PCIE_REG_CONTROL        0x0004
#define PCIE_REG_DOORBELL       0x0008
#define PCIE_REG_DATA_READY     0x000C
#define PCIE_REG_DATA_SIZE      0x0010

#define PCIE_CTRL_START         0x00000001
#define PCIE_CTRL_STOP          0x00000002
#define PCIE_CTRL_RESET         0x00000004

#define PCIE_STATUS_READY       0x00000001
#define PCIE_STATUS_DATA_AVAIL  0x00000002

#define PCIE_POLL_INTERVAL_US   100
#define PCIE_POLL_TIMEOUT_MS    5000

/*
 *-------------------------------------
 *  Private state
 *-------------------------------------
 */

struct pcie_transport_priv {
    int bar_fd;             /* /dev/uio0 or similar */
    volatile uint32_t *bar_ptr;  /* mmap'd BAR0 */
    size_t bar_size;

    int dma_h2c_fd;         /* /dev/xdma0_h2c_0 (host to card) */
    int dma_c2h_fd;         /* /dev/xdma0_c2h_0 (card to host) */

    void *dma_buf[2];       /* DMA buffers (page-aligned) */
    size_t buf_size;
    int active_buf;
    bool has_xdma;          /* true if XDMA character devices are available */
    bool has_bar;            /* true if UIO BAR mapping succeeded */
    bool terminated;
};

/*
 *-------------------------------------
 *  BAR register access helpers
 *-------------------------------------
 */

static inline void bar_write32(struct pcie_transport_priv *priv, uint32_t offset, uint32_t val)
{
    if (priv->bar_ptr)
        priv->bar_ptr[offset / 4] = val;
}

static inline uint32_t bar_read32(struct pcie_transport_priv *priv, uint32_t offset)
{
    if (priv->bar_ptr)
        return priv->bar_ptr[offset / 4];
    return 0;
}

/*
 *-------------------------------------
 *  transport_ops implementation
 *-------------------------------------
 */

static int pcie_init_common(struct transport_handle *th)
{
    struct pcie_transport_priv *priv = calloc(1, sizeof(struct pcie_transport_priv));
    if (!priv) {
        log_fatal("PCIe transport: allocation failed");
        return -1;
    }

    priv->bar_fd = -1;
    priv->dma_h2c_fd = -1;
    priv->dma_c2h_fd = -1;
    priv->bar_ptr = NULL;
    priv->bar_size = PCIE_DEFAULT_BAR_SIZE;
    priv->buf_size = th->buffer_size;
    priv->active_buf = 0;
    priv->has_xdma = false;
    priv->has_bar = false;
    priv->terminated = false;

    th->priv = priv;

    /* Try to open UIO device for BAR access */
    const char *bar_dev = PCIE_DEFAULT_BAR_DEV;
    if (th->channel_name[0] != '\0')
        bar_dev = th->channel_name;

    priv->bar_fd = open(bar_dev, O_RDWR | O_SYNC);
    if (priv->bar_fd >= 0) {
        priv->bar_ptr = (volatile uint32_t *)mmap(
            NULL, priv->bar_size,
            PROT_READ | PROT_WRITE, MAP_SHARED,
            priv->bar_fd, 0);
        if (priv->bar_ptr == MAP_FAILED) {
            log_warn("PCIe BAR mmap failed: %s", strerror(errno));
            priv->bar_ptr = NULL;
            close(priv->bar_fd);
            priv->bar_fd = -1;
        } else {
            priv->has_bar = true;
            log_info("PCIe BAR mapped: %s, size %zu", bar_dev, priv->bar_size);
        }
    } else {
        log_warn("PCIe UIO device %s not available: %s", bar_dev, strerror(errno));
    }

    /* Try to open XDMA channels */
    priv->dma_h2c_fd = open(PCIE_DEFAULT_H2C_DEV, O_RDWR);
    priv->dma_c2h_fd = open(PCIE_DEFAULT_C2H_DEV, O_RDWR);
    if (priv->dma_h2c_fd >= 0 && priv->dma_c2h_fd >= 0) {
        priv->has_xdma = true;
        log_info("PCIe XDMA channels opened: h2c=%s, c2h=%s",
                 PCIE_DEFAULT_H2C_DEV, PCIE_DEFAULT_C2H_DEV);
    } else {
        if (priv->dma_h2c_fd >= 0) { close(priv->dma_h2c_fd); priv->dma_h2c_fd = -1; }
        if (priv->dma_c2h_fd >= 0) { close(priv->dma_c2h_fd); priv->dma_c2h_fd = -1; }
        log_warn("PCIe XDMA channels not available (will use BAR-only mode)");
    }

    if (!priv->has_bar && !priv->has_xdma) {
        log_error("PCIe transport: no BAR or XDMA access available");
        free(priv);
        th->priv = NULL;
        return -1;
    }

    /* Allocate page-aligned DMA buffers */
    for (int i = 0; i < 2; i++) {
        if (posix_memalign(&priv->dma_buf[i], 4096, priv->buf_size) != 0) {
            log_fatal("PCIe DMA buffer allocation failed");
            return -1;
        }
        memset(priv->dma_buf[i], 0, priv->buf_size);
    }

    /* Reset FPGA if BAR available */
    if (priv->has_bar) {
        bar_write32(priv, PCIE_REG_CONTROL, PCIE_CTRL_RESET);
        usleep(10000);
        bar_write32(priv, PCIE_REG_CONTROL, PCIE_CTRL_START);
    }

    return 0;
}

int pcie_init_producer(struct transport_handle *th)
{
    log_info("PCIe transport: initializing producer");
    return pcie_init_common(th);
}

int pcie_init_consumer(struct transport_handle *th)
{
    log_info("PCIe transport: initializing consumer");
    return pcie_init_common(th);
}

void pcie_destroy(struct transport_handle *th)
{
    if (!th || !th->priv)
        return;

    struct pcie_transport_priv *priv = (struct pcie_transport_priv *)th->priv;
    priv->terminated = true;

    /* Stop FPGA */
    if (priv->has_bar) {
        bar_write32(priv, PCIE_REG_CONTROL, PCIE_CTRL_STOP);
    }

    /* Unmap BAR */
    if (priv->bar_ptr) {
        munmap((void *)priv->bar_ptr, priv->bar_size);
    }
    if (priv->bar_fd >= 0)
        close(priv->bar_fd);

    /* Close XDMA channels */
    if (priv->dma_h2c_fd >= 0)
        close(priv->dma_h2c_fd);
    if (priv->dma_c2h_fd >= 0)
        close(priv->dma_c2h_fd);

    /* Free DMA buffers */
    for (int i = 0; i < 2; i++)
        free(priv->dma_buf[i]);

    free(priv);
    th->priv = NULL;
    log_info("PCIe transport: destroyed");
}

int pcie_get_write_buf(struct transport_handle *th, void **buf_ptr)
{
    struct pcie_transport_priv *priv = (struct pcie_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

    int idx = priv->active_buf ^ 1;
    *buf_ptr = priv->dma_buf[idx];
    return idx;
}

int pcie_submit_write(struct transport_handle *th, int buf_index)
{
    struct pcie_transport_priv *priv = (struct pcie_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

    if (priv->has_xdma) {
        /* Write data to FPGA via XDMA H2C channel */
        ssize_t written = 0;
        size_t remaining = priv->buf_size;
        uint8_t *ptr = (uint8_t *)priv->dma_buf[buf_index];

        while (remaining > 0) {
            ssize_t ret = write(priv->dma_h2c_fd, ptr + written, remaining);
            if (ret < 0) {
                if (errno == EINTR)
                    continue;
                log_error("PCIe XDMA H2C write failed: %s", strerror(errno));
                return -1;
            }
            written += ret;
            remaining -= ret;
        }
    } else if (priv->has_bar) {
        /* BAR-only mode: write data through BAR (slow, for small transfers) */
        log_warn("PCIe BAR-only write mode: limited to register-level access");
        bar_write32(priv, PCIE_REG_DOORBELL, 1);
    }

    priv->active_buf = buf_index;
    th->total_bytes += priv->buf_size;
    th->total_frames++;
    return 0;
}

int pcie_get_read_buf(struct transport_handle *th, void **buf_ptr)
{
    struct pcie_transport_priv *priv = (struct pcie_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

    int idx = priv->active_buf;

    if (priv->has_xdma) {
        /* Read data from FPGA via XDMA C2H channel */
        if (priv->has_bar) {
            /* Poll doorbell register for data availability */
            int elapsed_us = 0;
            while (!(bar_read32(priv, PCIE_REG_STATUS) & PCIE_STATUS_DATA_AVAIL)) {
                if (priv->terminated)
                    return -1;
                usleep(PCIE_POLL_INTERVAL_US);
                elapsed_us += PCIE_POLL_INTERVAL_US;
                if (elapsed_us >= PCIE_POLL_TIMEOUT_MS * 1000) {
                    log_warn("PCIe data ready timeout");
                    return -1;
                }
            }
        }

        ssize_t total_read = 0;
        size_t remaining = priv->buf_size;
        uint8_t *ptr = (uint8_t *)priv->dma_buf[idx];

        while (remaining > 0) {
            ssize_t ret = read(priv->dma_c2h_fd, ptr + total_read, remaining);
            if (ret < 0) {
                if (errno == EINTR)
                    continue;
                log_error("PCIe XDMA C2H read failed: %s", strerror(errno));
                return -1;
            }
            if (ret == 0) {
                log_error("PCIe XDMA C2H unexpected EOF");
                return -1;
            }
            total_read += ret;
            remaining -= ret;
        }
    } else if (priv->has_bar) {
        /* BAR-only read -- poll status, then read size from register */
        int elapsed_us = 0;
        while (!(bar_read32(priv, PCIE_REG_STATUS) & PCIE_STATUS_DATA_AVAIL)) {
            if (priv->terminated)
                return -1;
            usleep(PCIE_POLL_INTERVAL_US);
            elapsed_us += PCIE_POLL_INTERVAL_US;
            if (elapsed_us >= PCIE_POLL_TIMEOUT_MS * 1000) {
                log_warn("PCIe BAR data ready timeout");
                return -1;
            }
        }
        log_warn("PCIe BAR-only read: register-level access only");
    }

    *buf_ptr = priv->dma_buf[idx];
    return idx;
}

int pcie_release_read(struct transport_handle *th, int buf_index)
{
    struct pcie_transport_priv *priv = (struct pcie_transport_priv *)th->priv;
    (void)buf_index;
    priv->active_buf ^= 1;

    /* Acknowledge read completion to FPGA */
    if (priv->has_bar)
        bar_write32(priv, PCIE_REG_DATA_READY, 0);

    return 0;
}

void pcie_send_terminate(struct transport_handle *th)
{
    struct pcie_transport_priv *priv = (struct pcie_transport_priv *)th->priv;
    priv->terminated = true;

    if (priv->has_bar)
        bar_write32(priv, PCIE_REG_CONTROL, PCIE_CTRL_STOP);

    log_info("PCIe transport: terminate signaled");
}

/*
 *-------------------------------------
 *  Exported ops table
 *-------------------------------------
 */

static const struct transport_ops pcie_transport_ops = {
    .init_producer  = pcie_init_producer,
    .init_consumer  = pcie_init_consumer,
    .destroy        = pcie_destroy,
    .get_write_buf  = pcie_get_write_buf,
    .submit_write   = pcie_submit_write,
    .get_read_buf   = pcie_get_read_buf,
    .release_read   = pcie_release_read,
    .send_terminate = pcie_send_terminate,
};

const struct transport_ops* transport_pcie_get_ops(void)
{
    return &pcie_transport_ops;
}
