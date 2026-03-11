/*
 *
 * Description :
 * BCM2711 (Raspberry Pi 4) DMA engine driver.
 * Provides DMA memory allocation, physical address translation,
 * and asynchronous DMA memory copy operations.
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

#ifndef BCM2711_DMA_H
#define BCM2711_DMA_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

/*
 *-------------------------------------
 *  Error codes
 *-------------------------------------
 */
#define DMA_OK             0
#define DMA_NOT_AVAILABLE -1
#define DMA_ALLOC_FAIL    -2
#define DMA_TRANSFER_FAIL -3
#define DMA_TIMEOUT       -4

/*
 *-------------------------------------
 *  BCM2711 DMA register definitions
 *-------------------------------------
 */
#define BCM2711_PERI_BASE      0xFE000000
#define BCM2711_DMA_BASE       0xFE007000
#define BCM2711_DMA_CHAN_SIZE  0x100         /* Spacing between channel regs */
#define BCM2711_DMA_NUM_CHAN   15

/* DMA channel register offsets */
#define DMA_CS          0x00   /* Control & Status */
#define DMA_CONBLK_AD   0x04   /* Control Block Address */
#define DMA_TI          0x08   /* Transfer Information (read-only alias) */
#define DMA_SOURCE_AD   0x0C   /* Source Address */
#define DMA_DEST_AD     0x10   /* Destination Address */
#define DMA_TXFR_LEN    0x14   /* Transfer Length */
#define DMA_STRIDE      0x18   /* 2D Stride */
#define DMA_NEXTCONBK   0x1C   /* Next Control Block Address */
#define DMA_DEBUG       0x20   /* Debug */

/* DMA CS register bits */
#define DMA_CS_ACTIVE       (1 << 0)
#define DMA_CS_END          (1 << 1)
#define DMA_CS_INT          (1 << 2)
#define DMA_CS_ERROR        (1 << 8)
#define DMA_CS_ABORT        (1 << 30)
#define DMA_CS_RESET        (1U << 31)

/* DMA TI register bits */
#define DMA_TI_SRC_INC      (1 << 8)
#define DMA_TI_DEST_INC     (1 << 4)
#define DMA_TI_WAIT_RESP    (1 << 3)
#define DMA_TI_NO_WIDE_BURSTS (1 << 26)

/*
 *-------------------------------------
 *  DMA Control Block (must be 32-byte aligned)
 *-------------------------------------
 */
struct dma_cb {
    uint32_t ti;            /* Transfer Information */
    uint32_t source_ad;     /* Source Address (physical) */
    uint32_t dest_ad;       /* Destination Address (physical) */
    uint32_t txfr_len;      /* Transfer Length */
    uint32_t stride;        /* 2D Stride */
    uint32_t nextconbk;     /* Next Control Block Address (physical) */
    uint32_t reserved[2];   /* Padding to 32 bytes */
} __attribute__((aligned(32)));

/*
 *-------------------------------------
 *  DMA context
 *-------------------------------------
 */
struct dma_context {
    int mem_fd;                     /* /dev/mem */
    volatile uint32_t *dma_regs;    /* mmap'd DMA register base */
    int channel;
    bool available;

    /* DMA control block memory */
    struct dma_cb *cb_virt;         /* Virtual address of CB */
    uint32_t cb_phys;               /* Physical address of CB */
};

/*
 *-------------------------------------
 *  API functions
 *-------------------------------------
 */

/* Initialize DMA engine for a specific channel */
int  dma_init(struct dma_context *ctx, int channel);
void dma_close(struct dma_context *ctx);

/* Allocate DMA-capable (physically contiguous) buffer */
void *dma_alloc(struct dma_context *ctx, size_t size, uint32_t *phys_addr);
void  dma_free(struct dma_context *ctx, void *virt, size_t size);

/* DMA memory copy (async, returns immediately) */
int dma_memcpy_start(struct dma_context *ctx,
                     uint32_t dst_phys, uint32_t src_phys, size_t len);
int dma_memcpy_wait(struct dma_context *ctx, int timeout_ms);

/* Synchronous convenience wrapper */
int dma_memcpy(struct dma_context *ctx,
               uint32_t dst_phys, uint32_t src_phys, size_t len);

/* Virtual to physical address translation via /proc/self/pagemap */
uint32_t virt_to_phys(void *virt);

/*
 * Smart memcpy: uses DMA if available and size > threshold,
 * else falls back to regular memcpy.
 */
void smart_memcpy(struct dma_context *ctx, void *dst, const void *src,
                  size_t len, size_t min_dma_size);

#endif /* BCM2711_DMA_H */
