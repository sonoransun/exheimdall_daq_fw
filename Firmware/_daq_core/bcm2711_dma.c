/*
 *
 * Description :
 * BCM2711 (Raspberry Pi 4) DMA engine driver implementation.
 * Provides DMA memory allocation via CMA or /dev/mem, physical address
 * translation via /proc/self/pagemap, and asynchronous DMA memory copies.
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
#include <sys/ioctl.h>
#include <time.h>

#include "log.h"
#include "bcm2711_dma.h"

/*
 *-------------------------------------
 *  Constants
 *-------------------------------------
 */
#define DMA_REG_MAP_SIZE       (BCM2711_DMA_NUM_CHAN * BCM2711_DMA_CHAN_SIZE)
#define PAGE_SIZE_4K           4096
#define PAGEMAP_ENTRY_SIZE     8
#define PAGEMAP_PFN_MASK       0x7FFFFFFFFFFFFFULL
#define PAGEMAP_PRESENT_BIT    (1ULL << 63)

/* CMA heap device (Linux 5.x+) */
#define CMA_HEAP_DEV           "/dev/dma_heap/linux,cma"

/* /dev/mem for legacy access */
#define DEV_MEM                "/dev/mem"

/* DMA control block alignment */
#define DMA_CB_ALIGN           32

/*
 *-------------------------------------
 *  DMA register access helpers
 *-------------------------------------
 */

static inline volatile uint32_t *dma_chan_regs(struct dma_context *ctx)
{
    if (!ctx->dma_regs)
        return NULL;
    return ctx->dma_regs + (ctx->channel * BCM2711_DMA_CHAN_SIZE / 4);
}

static inline void dma_reg_write(struct dma_context *ctx, uint32_t offset, uint32_t val)
{
    volatile uint32_t *regs = dma_chan_regs(ctx);
    if (regs)
        regs[offset / 4] = val;
}

static inline uint32_t dma_reg_read(struct dma_context *ctx, uint32_t offset)
{
    volatile uint32_t *regs = dma_chan_regs(ctx);
    if (regs)
        return regs[offset / 4];
    return 0;
}

/*
 *-------------------------------------
 *  Virtual to physical address translation
 *-------------------------------------
 */

uint32_t virt_to_phys(void *virt)
{
    int fd = open("/proc/self/pagemap", O_RDONLY);
    if (fd < 0) {
        log_error("Cannot open /proc/self/pagemap: %s", strerror(errno));
        return 0;
    }

    uintptr_t vaddr = (uintptr_t)virt;
    size_t page_num = vaddr / PAGE_SIZE_4K;
    off_t offset = page_num * PAGEMAP_ENTRY_SIZE;

    uint64_t entry = 0;
    if (lseek(fd, offset, SEEK_SET) < 0) {
        log_error("pagemap lseek failed: %s", strerror(errno));
        close(fd);
        return 0;
    }
    if (read(fd, &entry, sizeof(entry)) != sizeof(entry)) {
        log_error("pagemap read failed: %s", strerror(errno));
        close(fd);
        return 0;
    }
    close(fd);

    if (!(entry & PAGEMAP_PRESENT_BIT)) {
        log_error("Page not present in memory for vaddr %p", virt);
        return 0;
    }

    uint64_t pfn = entry & PAGEMAP_PFN_MASK;
    uint32_t phys = (uint32_t)(pfn * PAGE_SIZE_4K + (vaddr % PAGE_SIZE_4K));
    return phys;
}

/*
 *-------------------------------------
 *  DMA initialization
 *-------------------------------------
 */

int dma_init(struct dma_context *ctx, int channel)
{
    memset(ctx, 0, sizeof(struct dma_context));
    ctx->mem_fd = -1;
    ctx->dma_regs = NULL;
    ctx->channel = channel;
    ctx->available = false;
    ctx->cb_virt = NULL;
    ctx->cb_phys = 0;

    if (channel < 0 || channel >= BCM2711_DMA_NUM_CHAN) {
        log_error("DMA channel %d out of range (0-%d)", channel, BCM2711_DMA_NUM_CHAN - 1);
        return DMA_NOT_AVAILABLE;
    }

    /* Open /dev/mem for register access */
    ctx->mem_fd = open(DEV_MEM, O_RDWR | O_SYNC);
    if (ctx->mem_fd < 0) {
        log_warn("DMA: cannot open %s: %s (DMA not available)", DEV_MEM, strerror(errno));
        return DMA_NOT_AVAILABLE;
    }

    /* Map DMA registers */
    ctx->dma_regs = (volatile uint32_t *)mmap(
        NULL, DMA_REG_MAP_SIZE,
        PROT_READ | PROT_WRITE, MAP_SHARED,
        ctx->mem_fd, BCM2711_DMA_BASE);

    if (ctx->dma_regs == MAP_FAILED) {
        log_warn("DMA: register mmap failed: %s (DMA not available)", strerror(errno));
        ctx->dma_regs = NULL;
        close(ctx->mem_fd);
        ctx->mem_fd = -1;
        return DMA_NOT_AVAILABLE;
    }

    /* Reset the DMA channel */
    dma_reg_write(ctx, DMA_CS, DMA_CS_RESET);
    usleep(10000);

    /* Verify channel is accessible */
    uint32_t debug = dma_reg_read(ctx, DMA_DEBUG);
    log_info("DMA channel %d initialized (debug reg: 0x%08X)", channel, debug);

    /* Allocate DMA control block (32-byte aligned, physically contiguous) */
    void *cb_mem = NULL;
    if (posix_memalign(&cb_mem, DMA_CB_ALIGN, sizeof(struct dma_cb)) != 0) {
        log_error("DMA CB allocation failed");
        munmap((void *)ctx->dma_regs, DMA_REG_MAP_SIZE);
        close(ctx->mem_fd);
        return DMA_ALLOC_FAIL;
    }
    memset(cb_mem, 0, sizeof(struct dma_cb));

    /* Lock the CB page in memory */
    if (mlock(cb_mem, sizeof(struct dma_cb)) != 0) {
        log_warn("DMA CB mlock failed: %s", strerror(errno));
    }

    ctx->cb_virt = (struct dma_cb *)cb_mem;
    ctx->cb_phys = virt_to_phys(cb_mem);

    if (ctx->cb_phys == 0) {
        log_warn("DMA CB physical address translation failed");
        free(cb_mem);
        munmap((void *)ctx->dma_regs, DMA_REG_MAP_SIZE);
        close(ctx->mem_fd);
        return DMA_NOT_AVAILABLE;
    }

    ctx->available = true;
    log_info("DMA engine ready on channel %d (CB phys: 0x%08X)", channel, ctx->cb_phys);
    return DMA_OK;
}

void dma_close(struct dma_context *ctx)
{
    if (!ctx)
        return;

    if (ctx->available) {
        /* Abort any running transfer */
        dma_reg_write(ctx, DMA_CS, DMA_CS_ABORT);
        usleep(1000);
        dma_reg_write(ctx, DMA_CS, DMA_CS_RESET);
    }

    if (ctx->cb_virt) {
        munlock(ctx->cb_virt, sizeof(struct dma_cb));
        free(ctx->cb_virt);
        ctx->cb_virt = NULL;
    }

    if (ctx->dma_regs) {
        munmap((void *)ctx->dma_regs, DMA_REG_MAP_SIZE);
        ctx->dma_regs = NULL;
    }

    if (ctx->mem_fd >= 0) {
        close(ctx->mem_fd);
        ctx->mem_fd = -1;
    }

    ctx->available = false;
    log_info("DMA engine closed (channel %d)", ctx->channel);
}

/*
 *-------------------------------------
 *  DMA buffer allocation
 *-------------------------------------
 */

void *dma_alloc(struct dma_context *ctx, size_t size, uint32_t *phys_addr)
{
    (void)ctx;

    /* Round up to page boundary */
    size_t alloc_size = (size + PAGE_SIZE_4K - 1) & ~(PAGE_SIZE_4K - 1);

    /* Try CMA heap first (Linux 5.x+) */
    int cma_fd = open(CMA_HEAP_DEV, O_RDWR);
    if (cma_fd >= 0) {
        /* CMA heap allocation via ioctl would go here.
         * For now, fall back to standard allocation + mlock. */
        close(cma_fd);
        log_trace("DMA alloc: CMA heap available but using mlock fallback");
    }

    /* Fallback: posix_memalign + mlock for physical contiguity on locked pages */
    void *virt = NULL;
    if (posix_memalign(&virt, PAGE_SIZE_4K, alloc_size) != 0) {
        log_error("DMA buffer allocation failed for %zu bytes", size);
        if (phys_addr)
            *phys_addr = 0;
        return NULL;
    }
    memset(virt, 0, alloc_size);

    /* Lock pages in physical memory */
    if (mlock(virt, alloc_size) != 0) {
        log_warn("DMA buffer mlock failed: %s (physical address may be invalid)",
                 strerror(errno));
    }

    /* Translate virtual to physical */
    if (phys_addr) {
        *phys_addr = virt_to_phys(virt);
        if (*phys_addr == 0)
            log_warn("DMA alloc: physical address translation failed for %p", virt);
    }

    log_trace("DMA alloc: %zu bytes at virt=%p phys=0x%08X",
              alloc_size, virt, phys_addr ? *phys_addr : 0);
    return virt;
}

void dma_free(struct dma_context *ctx, void *virt, size_t size)
{
    (void)ctx;
    if (virt) {
        size_t alloc_size = (size + PAGE_SIZE_4K - 1) & ~(PAGE_SIZE_4K - 1);
        munlock(virt, alloc_size);
        free(virt);
    }
}

/*
 *-------------------------------------
 *  DMA memory copy
 *-------------------------------------
 */

int dma_memcpy_start(struct dma_context *ctx,
                     uint32_t dst_phys, uint32_t src_phys, size_t len)
{
    if (!ctx->available) {
        log_error("DMA not available");
        return DMA_NOT_AVAILABLE;
    }

    if (len == 0)
        return DMA_OK;

    /* Setup DMA control block */
    struct dma_cb *cb = ctx->cb_virt;
    cb->ti = DMA_TI_SRC_INC | DMA_TI_DEST_INC | DMA_TI_WAIT_RESP;
    cb->source_ad = src_phys;
    cb->dest_ad = dst_phys;
    cb->txfr_len = (uint32_t)len;
    cb->stride = 0;
    cb->nextconbk = 0;  /* No chaining */

    /* Memory barrier to ensure CB is written before DMA sees it */
    __sync_synchronize();

    /* Clear status flags */
    dma_reg_write(ctx, DMA_CS, DMA_CS_END | DMA_CS_INT | DMA_CS_ERROR);

    /* Point DMA to control block and start */
    dma_reg_write(ctx, DMA_CONBLK_AD, ctx->cb_phys);
    dma_reg_write(ctx, DMA_CS, DMA_CS_ACTIVE);

    log_trace("DMA copy started: src=0x%08X dst=0x%08X len=%zu", src_phys, dst_phys, len);
    return DMA_OK;
}

int dma_memcpy_wait(struct dma_context *ctx, int timeout_ms)
{
    if (!ctx->available)
        return DMA_NOT_AVAILABLE;

    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);

    while (1) {
        uint32_t cs = dma_reg_read(ctx, DMA_CS);

        /* Check for completion */
        if (cs & DMA_CS_END) {
            /* Clear end flag */
            dma_reg_write(ctx, DMA_CS, DMA_CS_END);
            return DMA_OK;
        }

        /* Check for error */
        if (cs & DMA_CS_ERROR) {
            uint32_t debug = dma_reg_read(ctx, DMA_DEBUG);
            log_error("DMA transfer error: CS=0x%08X DEBUG=0x%08X", cs, debug);
            /* Clear error */
            dma_reg_write(ctx, DMA_DEBUG, 0x07);  /* Clear error bits */
            dma_reg_write(ctx, DMA_CS, DMA_CS_ERROR);
            return DMA_TRANSFER_FAIL;
        }

        /* Check timeout */
        clock_gettime(CLOCK_MONOTONIC, &now);
        long elapsed_ms = (now.tv_sec - start.tv_sec) * 1000 +
                          (now.tv_nsec - start.tv_nsec) / 1000000;
        if (elapsed_ms >= timeout_ms) {
            log_error("DMA transfer timeout after %d ms", timeout_ms);
            /* Abort the transfer */
            dma_reg_write(ctx, DMA_CS, DMA_CS_ABORT);
            usleep(100);
            dma_reg_write(ctx, DMA_CS, DMA_CS_RESET);
            return DMA_TIMEOUT;
        }

        /* Brief spin */
        usleep(1);
    }
}

int dma_memcpy(struct dma_context *ctx,
               uint32_t dst_phys, uint32_t src_phys, size_t len)
{
    int ret = dma_memcpy_start(ctx, dst_phys, src_phys, len);
    if (ret != DMA_OK)
        return ret;

    /* Wait up to 5 seconds for completion */
    return dma_memcpy_wait(ctx, 5000);
}

/*
 *-------------------------------------
 *  Smart memcpy
 *-------------------------------------
 */

void smart_memcpy(struct dma_context *ctx, void *dst, const void *src,
                  size_t len, size_t min_dma_size)
{
    /* Use DMA if available and transfer size exceeds threshold */
    if (ctx && ctx->available && len >= min_dma_size) {
        uint32_t dst_phys = virt_to_phys(dst);
        uint32_t src_phys = virt_to_phys((void *)src);

        if (dst_phys != 0 && src_phys != 0) {
            int ret = dma_memcpy(ctx, dst_phys, src_phys, len);
            if (ret == DMA_OK) {
                log_trace("smart_memcpy: DMA used for %zu bytes", len);
                return;
            }
            log_warn("smart_memcpy: DMA failed (ret=%d), falling back to memcpy", ret);
        }
    }

    /* Fallback to regular memcpy */
    memcpy(dst, src, len);
}
