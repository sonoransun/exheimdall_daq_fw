/*
 *
 * Description :
 * GPU offload engine implementing fir_engine_ops.
 * Uses the VideoCore VI (V3D) GPU on Raspberry Pi 4 via the mailbox
 * interface (/dev/vcio) for memory allocation and QPU shader execution.
 * Provides a working framework with loadable QPU shader binary support
 * and a selftest function for validation.
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
#include <math.h>
#include <sys/mman.h>
#include <sys/ioctl.h>

#include "log.h"
#include "offload.h"

/*
 *-------------------------------------
 *  RPi VideoCore mailbox interface
 *-------------------------------------
 */
#define VCIO_DEV            "/dev/vcio"
#define GPU_MEM_FLAG_DIRECT 0x04    /* Direct uncached */
#define GPU_MEM_FLAG_ZERO   0x10    /* Zero on allocate */

/* Mailbox property tags */
#define TAG_ALLOCATE_MEMORY 0x3000C
#define TAG_LOCK_MEMORY     0x3000D
#define TAG_UNLOCK_MEMORY   0x3000E
#define TAG_RELEASE_MEMORY  0x3000F
#define TAG_EXECUTE_QPU     0x30011

/* Mailbox request/response */
#define MB_PROCESS_REQUEST  0x00000000
#define MB_SUCCESS          0x80000000

/* ARM physical to bus address conversion for VideoCore */
#define BUS_TO_PHYS(x)     ((x) & ~0xC0000000)

/*
 * ioctl for VideoCore mailbox
 * Defined as _IOWR(100, 0, char *) in the kernel
 */
#define IOCTL_MBOX_PROPERTY _IOWR(100, 0, char *)

/*
 *-------------------------------------
 *  GPU shader path
 *-------------------------------------
 */
#define GPU_SHADER_PATH     "_data_control/fir_shader.bin"

/*
 *-------------------------------------
 *  Private context
 *-------------------------------------
 */

struct gpu_fir_context {
    int mailbox_fd;          /* /dev/vcio */

    /* GPU memory for data buffers */
    unsigned int data_handle;
    unsigned int data_bus_addr;
    void *data_mem;
    size_t data_mem_size;

    /* GPU memory for shader code */
    unsigned int shader_handle;
    unsigned int shader_bus_addr;
    void *shader_mem;
    size_t shader_mem_size;

    /* GPU memory for uniform parameters */
    unsigned int uniform_handle;
    unsigned int uniform_bus_addr;
    void *uniform_mem;
    size_t uniform_mem_size;

    /* Filter configuration */
    float *coeffs;
    size_t tap_size;
    int dec_ratio;
    size_t block_size;
    int num_channels;

    bool available;
    bool initialized;
};

/*
 *-------------------------------------
 *  Mailbox interface
 *-------------------------------------
 */

static int mbox_open(void)
{
    int fd = open(VCIO_DEV, O_NONBLOCK);
    if (fd < 0)
        log_warn("GPU offload: cannot open %s: %s", VCIO_DEV, strerror(errno));
    return fd;
}

static int mbox_property(int fd, void *buf)
{
#ifdef __linux__
    int ret = ioctl(fd, IOCTL_MBOX_PROPERTY, buf);
    if (ret < 0) {
        log_error("GPU mailbox ioctl failed: %s", strerror(errno));
        return -1;
    }
    return 0;
#else
    (void)fd; (void)buf;
    return -1;
#endif
}

static unsigned int gpu_mem_alloc(int fd, unsigned int size, unsigned int align,
                                   unsigned int flags)
{
    /* Mailbox buffer: must be 16-byte aligned */
    uint32_t buf[32] __attribute__((aligned(16)));
    memset(buf, 0, sizeof(buf));

    buf[0] = 9 * 4;             /* Total buffer size */
    buf[1] = MB_PROCESS_REQUEST;
    buf[2] = TAG_ALLOCATE_MEMORY;
    buf[3] = 12;                /* Value buffer size */
    buf[4] = 12;                /* Request size */
    buf[5] = size;              /* Size */
    buf[6] = align;             /* Alignment */
    buf[7] = flags;             /* Flags */
    buf[8] = 0;                 /* End tag */

    if (mbox_property(fd, buf) < 0)
        return 0;
    return buf[5];  /* Handle returned */
}

static unsigned int gpu_mem_lock(int fd, unsigned int handle)
{
    uint32_t buf[32] __attribute__((aligned(16)));
    memset(buf, 0, sizeof(buf));

    buf[0] = 7 * 4;
    buf[1] = MB_PROCESS_REQUEST;
    buf[2] = TAG_LOCK_MEMORY;
    buf[3] = 4;
    buf[4] = 4;
    buf[5] = handle;
    buf[6] = 0;

    if (mbox_property(fd, buf) < 0)
        return 0;
    return buf[5];  /* Bus address returned */
}

static int gpu_mem_unlock(int fd, unsigned int handle)
{
    uint32_t buf[32] __attribute__((aligned(16)));
    memset(buf, 0, sizeof(buf));

    buf[0] = 7 * 4;
    buf[1] = MB_PROCESS_REQUEST;
    buf[2] = TAG_UNLOCK_MEMORY;
    buf[3] = 4;
    buf[4] = 4;
    buf[5] = handle;
    buf[6] = 0;

    return mbox_property(fd, buf);
}

static int gpu_mem_release(int fd, unsigned int handle)
{
    uint32_t buf[32] __attribute__((aligned(16)));
    memset(buf, 0, sizeof(buf));

    buf[0] = 7 * 4;
    buf[1] = MB_PROCESS_REQUEST;
    buf[2] = TAG_RELEASE_MEMORY;
    buf[3] = 4;
    buf[4] = 4;
    buf[5] = handle;
    buf[6] = 0;

    return mbox_property(fd, buf);
}

static int gpu_execute_qpu(int fd, unsigned int num_qpus,
                            unsigned int control_bus_addr, int noflush,
                            unsigned int timeout_ms)
{
    uint32_t buf[32] __attribute__((aligned(16)));
    memset(buf, 0, sizeof(buf));

    buf[0] = 10 * 4;
    buf[1] = MB_PROCESS_REQUEST;
    buf[2] = TAG_EXECUTE_QPU;
    buf[3] = 16;
    buf[4] = 16;
    buf[5] = num_qpus;
    buf[6] = control_bus_addr;
    buf[7] = noflush;
    buf[8] = timeout_ms;
    buf[9] = 0;

    if (mbox_property(fd, buf) < 0)
        return -1;
    return buf[5];  /* 0 on success */
}

/*
 *-------------------------------------
 *  GPU memory mapping helper
 *-------------------------------------
 */

static void *mapmem(unsigned int phys_addr, unsigned int size)
{
#ifdef __linux__
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        log_error("GPU mapmem: cannot open /dev/mem: %s", strerror(errno));
        return NULL;
    }

    unsigned int offset = phys_addr % 4096;
    unsigned int base = phys_addr - offset;
    void *mem = mmap(NULL, size + offset, PROT_READ | PROT_WRITE,
                     MAP_SHARED, fd, base);
    close(fd);

    if (mem == MAP_FAILED) {
        log_error("GPU mapmem: mmap failed: %s", strerror(errno));
        return NULL;
    }
    return (uint8_t *)mem + offset;
#else
    (void)phys_addr; (void)size;
    return NULL;
#endif
}

static void unmapmem(void *addr, unsigned int size)
{
    if (addr) {
        unsigned int offset = (unsigned int)((uintptr_t)addr % 4096);
        munmap((uint8_t *)addr - offset, size + offset);
    }
}

/*
 *-------------------------------------
 *  GPU memory allocation wrapper
 *-------------------------------------
 */

static int gpu_alloc_buffer(struct gpu_fir_context *ctx,
                             unsigned int *handle, unsigned int *bus_addr,
                             void **mem, size_t size)
{
    *handle = gpu_mem_alloc(ctx->mailbox_fd, (unsigned int)size, 4096,
                            GPU_MEM_FLAG_DIRECT | GPU_MEM_FLAG_ZERO);
    if (*handle == 0) {
        log_error("GPU memory allocation failed for %zu bytes", size);
        return -1;
    }

    *bus_addr = gpu_mem_lock(ctx->mailbox_fd, *handle);
    if (*bus_addr == 0) {
        log_error("GPU memory lock failed");
        gpu_mem_release(ctx->mailbox_fd, *handle);
        return -1;
    }

    *mem = mapmem(BUS_TO_PHYS(*bus_addr), (unsigned int)size);
    if (!*mem) {
        log_error("GPU memory mapping failed");
        gpu_mem_unlock(ctx->mailbox_fd, *handle);
        gpu_mem_release(ctx->mailbox_fd, *handle);
        return -1;
    }

    return 0;
}

static void gpu_free_buffer(struct gpu_fir_context *ctx,
                             unsigned int handle, unsigned int bus_addr,
                             void *mem, size_t size)
{
    (void)bus_addr;
    if (mem)
        unmapmem(mem, (unsigned int)size);
    if (handle) {
        gpu_mem_unlock(ctx->mailbox_fd, handle);
        gpu_mem_release(ctx->mailbox_fd, handle);
    }
}

/*
 *-------------------------------------
 *  Load QPU shader binary
 *-------------------------------------
 */

static int load_shader(struct gpu_fir_context *ctx, const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        log_warn("GPU shader file %s not found: %s", path, strerror(errno));
        return -1;
    }

    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);

    if (size <= 0 || size > 1024 * 1024) {
        log_error("GPU shader file invalid size: %ld", size);
        fclose(f);
        return -1;
    }

    ctx->shader_mem_size = (size_t)size;

    int ret = gpu_alloc_buffer(ctx, &ctx->shader_handle, &ctx->shader_bus_addr,
                                &ctx->shader_mem, ctx->shader_mem_size);
    if (ret != 0) {
        fclose(f);
        return -1;
    }

    size_t read = fread(ctx->shader_mem, 1, ctx->shader_mem_size, f);
    fclose(f);

    if (read != ctx->shader_mem_size) {
        log_error("GPU shader read incomplete: %zu of %zu", read, ctx->shader_mem_size);
        return -1;
    }

    log_info("GPU shader loaded: %s (%zu bytes)", path, ctx->shader_mem_size);
    return 0;
}

/*
 *-------------------------------------
 *  Selftest
 *-------------------------------------
 */

static int gpu_selftest(struct gpu_fir_context *ctx)
{
    if (!ctx->available) {
        log_warn("GPU selftest: GPU not available");
        return -1;
    }

    /* Allocate a small test buffer */
    unsigned int test_handle, test_bus;
    void *test_mem;
    size_t test_size = 4096;

    int ret = gpu_alloc_buffer(ctx, &test_handle, &test_bus, &test_mem, test_size);
    if (ret != 0) {
        log_error("GPU selftest: allocation failed");
        return -1;
    }

    /* Write a test pattern */
    uint32_t *ptr = (uint32_t *)test_mem;
    for (int i = 0; i < 256; i++)
        ptr[i] = (uint32_t)i;

    /* Memory barrier */
    __sync_synchronize();

    /* Read back and verify */
    bool pass = true;
    for (int i = 0; i < 256; i++) {
        if (ptr[i] != (uint32_t)i) {
            log_error("GPU selftest: mismatch at index %d: got %u expected %u",
                      i, ptr[i], (uint32_t)i);
            pass = false;
            break;
        }
    }

    gpu_free_buffer(ctx, test_handle, test_bus, test_mem, test_size);

    if (pass) {
        log_info("GPU selftest: PASSED (memory alloc/map/verify)");
        return 0;
    } else {
        log_error("GPU selftest: FAILED");
        return -1;
    }
}

/*
 *-------------------------------------
 *  fir_engine_ops implementation
 *-------------------------------------
 */

static int gpu_fir_init(struct fir_engine *eng, const float *coeffs, size_t tap_size,
                         int dec_ratio, size_t block_size, int num_channels)
{
    struct gpu_fir_context *ctx = (struct gpu_fir_context *)eng->ctx;
    if (!ctx) return -1;

    memset(ctx, 0, sizeof(struct gpu_fir_context));
    ctx->mailbox_fd = -1;
    ctx->available = false;
    ctx->initialized = false;
    ctx->tap_size = tap_size;
    ctx->dec_ratio = dec_ratio;
    ctx->block_size = block_size;
    ctx->num_channels = num_channels;

    /* Open mailbox */
    ctx->mailbox_fd = mbox_open();
    if (ctx->mailbox_fd < 0) {
        log_warn("GPU offload: mailbox not available, engine disabled");
        return -1;
    }

    ctx->available = true;

    /* Save coefficients */
    ctx->coeffs = malloc(tap_size * sizeof(float));
    if (!ctx->coeffs) {
        log_error("GPU offload: coefficient allocation failed");
        return -1;
    }
    memcpy(ctx->coeffs, coeffs, tap_size * sizeof(float));

    /* Allocate GPU data buffer for input + output */
    size_t input_size = block_size * 2 * sizeof(float) * num_channels;
    size_t output_size = (block_size / dec_ratio) * 2 * sizeof(float) * num_channels;
    ctx->data_mem_size = input_size + output_size;

    int ret = gpu_alloc_buffer(ctx, &ctx->data_handle, &ctx->data_bus_addr,
                                &ctx->data_mem, ctx->data_mem_size);
    if (ret != 0) {
        log_error("GPU data buffer allocation failed");
        free(ctx->coeffs);
        close(ctx->mailbox_fd);
        return -1;
    }

    /* Allocate GPU uniform buffer for filter parameters */
    ctx->uniform_mem_size = sizeof(float) * (tap_size + 16);
    ret = gpu_alloc_buffer(ctx, &ctx->uniform_handle, &ctx->uniform_bus_addr,
                            &ctx->uniform_mem, ctx->uniform_mem_size);
    if (ret != 0) {
        log_error("GPU uniform buffer allocation failed");
        gpu_free_buffer(ctx, ctx->data_handle, ctx->data_bus_addr,
                        ctx->data_mem, ctx->data_mem_size);
        free(ctx->coeffs);
        close(ctx->mailbox_fd);
        return -1;
    }

    /* Write coefficients and parameters into uniform buffer */
    float *uniforms = (float *)ctx->uniform_mem;
    /* Uniforms layout: [dec_ratio, tap_size, block_size, num_channels, coeffs...] */
    uniforms[0] = (float)dec_ratio;
    uniforms[1] = (float)tap_size;
    uniforms[2] = (float)block_size;
    uniforms[3] = (float)num_channels;
    memcpy(&uniforms[4], coeffs, tap_size * sizeof(float));

    /* Load QPU shader binary */
    ret = load_shader(ctx, GPU_SHADER_PATH);
    if (ret != 0) {
        log_warn("GPU shader not available; FIR will use software fallback");
        /* Not fatal: engine reports available=true but executes on CPU */
    }

    /* Run selftest */
    gpu_selftest(ctx);

    ctx->initialized = true;
    log_info("GPU FIR offload engine initialized (dec=%d, taps=%zu, block=%zu, ch=%d)",
             dec_ratio, tap_size, block_size, num_channels);
    return 0;
}

static void gpu_fir_destroy(struct fir_engine *eng)
{
    struct gpu_fir_context *ctx = (struct gpu_fir_context *)eng->ctx;
    if (!ctx)
        return;

    if (ctx->shader_mem)
        gpu_free_buffer(ctx, ctx->shader_handle, ctx->shader_bus_addr,
                        ctx->shader_mem, ctx->shader_mem_size);

    if (ctx->uniform_mem)
        gpu_free_buffer(ctx, ctx->uniform_handle, ctx->uniform_bus_addr,
                        ctx->uniform_mem, ctx->uniform_mem_size);

    if (ctx->data_mem)
        gpu_free_buffer(ctx, ctx->data_handle, ctx->data_bus_addr,
                        ctx->data_mem, ctx->data_mem_size);

    free(ctx->coeffs);

    if (ctx->mailbox_fd >= 0)
        close(ctx->mailbox_fd);

    ctx->available = false;
    ctx->initialized = false;
    log_info("GPU FIR offload engine destroyed");
}

static int gpu_fir_decimate(struct fir_engine *eng, int ch_index,
                             const float *input_i, const float *input_q,
                             float *output_i, float *output_q, size_t input_len)
{
    struct gpu_fir_context *ctx = (struct gpu_fir_context *)eng->ctx;
    if (!ctx || !ctx->initialized)
        return -1;

    size_t output_len = input_len / ctx->dec_ratio;

    /* If no shader loaded, fall back to CPU FIR */
    if (!ctx->shader_mem) {
        log_trace("GPU FIR: no shader, using CPU fallback for ch %d", ch_index);

        for (size_t n = 0; n < output_len; n++) {
            float acc_i = 0.0f;
            float acc_q = 0.0f;
            size_t base = n * ctx->dec_ratio;

            for (size_t k = 0; k < ctx->tap_size && (base + k) < input_len; k++) {
                acc_i += input_i[base + k] * ctx->coeffs[k];
                acc_q += input_q[base + k] * ctx->coeffs[k];
            }
            output_i[n] = acc_i;
            output_q[n] = acc_q;
        }
        return 0;
    }

    /* Interleave I/Q into GPU input buffer for QPU processing */
    size_t input_bytes = input_len * 2 * sizeof(float);
    size_t output_bytes = output_len * 2 * sizeof(float);
    float *gpu_input = (float *)ctx->data_mem;
    float *gpu_output = (float *)((uint8_t *)ctx->data_mem + input_bytes);

    for (size_t i = 0; i < input_len; i++) {
        gpu_input[2 * i]     = input_i[i];
        gpu_input[2 * i + 1] = input_q[i];
    }

    __sync_synchronize();

    uint32_t *control = (uint32_t *)ctx->uniform_mem;
    size_t ctrl_offset = ctx->tap_size + 16;
    control[ctrl_offset + 0] = ctx->uniform_bus_addr;
    control[ctrl_offset + 1] = ctx->shader_bus_addr;

    __sync_synchronize();

    int ret = gpu_execute_qpu(ctx->mailbox_fd, 1,
                               ctx->uniform_bus_addr + (uint32_t)(ctrl_offset * sizeof(uint32_t)),
                               0, 5000);
    if (ret != 0) {
        log_error("GPU QPU execution failed (ret=%d) for ch %d, falling back to CPU",
                  ret, ch_index);
        for (size_t n = 0; n < output_len; n++) {
            float acc_i = 0.0f, acc_q = 0.0f;
            size_t base = n * ctx->dec_ratio;
            for (size_t k = 0; k < ctx->tap_size && (base + k) < input_len; k++) {
                acc_i += input_i[base + k] * ctx->coeffs[k];
                acc_q += input_q[base + k] * ctx->coeffs[k];
            }
            output_i[n] = acc_i;
            output_q[n] = acc_q;
        }
        return 0;
    }

    __sync_synchronize();

    /* De-interleave GPU output back to separate I/Q */
    for (size_t n = 0; n < output_len; n++) {
        output_i[n] = gpu_output[2 * n];
        output_q[n] = gpu_output[2 * n + 1];
    }
    (void)output_bytes;

    return 0;
}

static void gpu_fir_reset(struct fir_engine *eng, int ch_index)
{
    struct gpu_fir_context *ctx = (struct gpu_fir_context *)eng->ctx;
    if (!ctx || !ctx->initialized)
        return;

    (void)ch_index;

    /* Clear GPU data buffer */
    if (ctx->data_mem)
        memset(ctx->data_mem, 0, ctx->data_mem_size);

    log_info("GPU FIR reset (ch %d)", ch_index);
}

/*
 *-------------------------------------
 *  Exported engine ops table
 *-------------------------------------
 */

struct fir_engine* fir_engine_gpu_create(void)
{
    struct fir_engine* eng = calloc(1, sizeof(struct fir_engine));
    if (!eng) return NULL;

    eng->init = gpu_fir_init;
    eng->destroy = gpu_fir_destroy;
    eng->decimate = gpu_fir_decimate;
    eng->reset = gpu_fir_reset;
    eng->type = OFFLOAD_GPU;
    eng->ctx = calloc(1, sizeof(struct gpu_fir_context));
    if (!eng->ctx) {
        free(eng);
        return NULL;
    }
    return eng;
}
