/*
 * CPU NEON Offload Engine (ARM) - FIR Decimation via Ne10 library
 *
 * Extracted from fir_decimate.c NEON path for the offload abstraction.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Copyright (C) 2018-2026 Tamás Pető
 */

#ifdef ARM_NEON

#include <stdlib.h>
#include <string.h>
#include "offload.h"
#include "log.h"
#include "NE10.h"

#define DC 127.5f

struct neon_fir_ctx {
    ne10_float32_t** state_vectors;      /* Per-channel state (2 per ch: I and Q) */
    ne10_fir_decimate_instance_f32_t* fir_cfgs; /* Per-channel FIR config */
    int num_channels;
    int dec_ratio;
    size_t block_size;                    /* CPI size (output) */
    size_t tap_size;
    ne10_uint32_t fir_blocksize;
    bool initialized;
};

struct neon_convert_ctx {
    int dummy; /* No state needed for simple conversion */
};

/*
 *-------------------------------------
 *   FIR Engine Implementation
 *-------------------------------------
 */

static int neon_fir_init(struct fir_engine* eng, const float* coeffs,
                         size_t tap_size, int dec_ratio, size_t block_size,
                         int num_channels)
{
    struct neon_fir_ctx* ctx = (struct neon_fir_ctx*)eng->ctx;

    if (ne10_init() != NE10_OK) {
        log_error("Ne10 initialization failed");
        return -1;
    }

    ctx->num_channels = num_channels;
    ctx->dec_ratio = dec_ratio;
    ctx->block_size = block_size;
    ctx->tap_size = tap_size;
    ctx->fir_blocksize = (ne10_uint32_t)(block_size * dec_ratio);

    /* Allocate per-channel FIR configurations (2 per channel: I and Q) */
    ctx->fir_cfgs = malloc(num_channels * 2 * sizeof(ne10_fir_decimate_instance_f32_t));
    if (!ctx->fir_cfgs) return -1;

    ctx->state_vectors = malloc(num_channels * 2 * sizeof(ne10_float32_t*));
    if (!ctx->state_vectors) return -1;

    ne10_uint16_t R = (ne10_uint16_t)dec_ratio;

    for (int m = 0; m < num_channels * 2; m++) {
        ctx->state_vectors[m] = malloc((tap_size + ctx->fir_blocksize - 1) * sizeof(ne10_float32_t));
        if (!ctx->state_vectors[m]) {
            log_error("Failed to allocate FIR state vector %d", m);
            return -1;
        }
        memset(ctx->state_vectors[m], 0, (tap_size + ctx->fir_blocksize - 1) * sizeof(ne10_float32_t));

        if (ne10_fir_decimate_init_float(&ctx->fir_cfgs[m], (ne10_uint16_t)tap_size,
                                         R, (ne10_float32_t*)coeffs,
                                         ctx->state_vectors[m],
                                         ctx->fir_blocksize) != NE10_OK) {
            log_error("Failed to initialize FIR instance %d", m);
            return -1;
        }
    }

    ctx->initialized = true;
    log_info("NEON FIR engine initialized: %d channels, %zu taps, %dx decimation",
             num_channels, tap_size, dec_ratio);
    return 0;
}

static void neon_fir_destroy(struct fir_engine* eng)
{
    struct neon_fir_ctx* ctx = (struct neon_fir_ctx*)eng->ctx;
    if (ctx) {
        if (ctx->state_vectors) {
            for (int m = 0; m < ctx->num_channels * 2; m++) {
                free(ctx->state_vectors[m]);
            }
            free(ctx->state_vectors);
        }
        free(ctx->fir_cfgs);
        free(ctx);
    }
    free(eng);
}

static int neon_fir_decimate(struct fir_engine* eng, int ch_index,
                             const float* input_i, const float* input_q,
                             float* output_i, float* output_q,
                             size_t input_len)
{
    struct neon_fir_ctx* ctx = (struct neon_fir_ctx*)eng->ctx;

    for (size_t b = 0; b < input_len / ctx->fir_blocksize; b++) {
        // Use NEON-accelerated decimation instead of C reference
        ne10_fir_decimate_float_neon(
            &ctx->fir_cfgs[2 * ch_index],
            (ne10_float32_t*)(input_i + b * ctx->fir_blocksize),
            (ne10_float32_t*)(output_i + b * ctx->block_size),
            ctx->fir_blocksize);

        ne10_fir_decimate_float_neon(
            &ctx->fir_cfgs[2 * ch_index + 1],
            (ne10_float32_t*)(input_q + b * ctx->fir_blocksize),
            (ne10_float32_t*)(output_q + b * ctx->block_size),
            ctx->fir_blocksize);
    }

    return 0;
}

static void neon_fir_reset(struct fir_engine* eng, int ch_index)
{
    struct neon_fir_ctx* ctx = (struct neon_fir_ctx*)eng->ctx;
    memset(ctx->state_vectors[2 * ch_index], 0,
           (ctx->tap_size + ctx->fir_blocksize - 1) * sizeof(ne10_float32_t));
    memset(ctx->state_vectors[2 * ch_index + 1], 0,
           (ctx->tap_size + ctx->fir_blocksize - 1) * sizeof(ne10_float32_t));
}

struct fir_engine* fir_engine_neon_create(void)
{
    struct fir_engine* eng = calloc(1, sizeof(struct fir_engine));
    if (!eng) return NULL;

    struct neon_fir_ctx* ctx = calloc(1, sizeof(struct neon_fir_ctx));
    if (!ctx) { free(eng); return NULL; }

    eng->init = neon_fir_init;
    eng->destroy = neon_fir_destroy;
    eng->decimate = neon_fir_decimate;
    eng->reset = neon_fir_reset;
    eng->type = OFFLOAD_CPU_NEON;
    eng->ctx = ctx;

    return eng;
}

/*
 *-------------------------------------
 *   Convert Engine Implementation
 *-------------------------------------
 */

static int neon_u8_to_f32_deinterleave(struct convert_engine* eng,
                                        const uint8_t* in,
                                        float* out_i, float* out_q,
                                        size_t num_samples)
{
    (void)eng;
    for (size_t s = 0; s < num_samples; s++) {
        out_i[s] = ((float)in[2 * s]     - DC) / DC;
        out_q[s] = ((float)in[2 * s + 1] - DC) / DC;
    }
    return 0;
}

static int neon_u8_to_f32_interleaved(struct convert_engine* eng,
                                       const uint8_t* in, float* out,
                                       size_t num_samples)
{
    (void)eng;
    for (size_t s = 0; s < num_samples; s++) {
        out[2 * s]     = ((float)in[2 * s]     - DC) / DC;
        out[2 * s + 1] = ((float)in[2 * s + 1] - DC) / DC;
    }
    return 0;
}

struct convert_engine* convert_engine_neon_create(void)
{
    struct convert_engine* eng = calloc(1, sizeof(struct convert_engine));
    if (!eng) return NULL;

    eng->u8_to_f32_deinterleave = neon_u8_to_f32_deinterleave;
    eng->u8_to_f32_interleaved = neon_u8_to_f32_interleaved;
    eng->type = OFFLOAD_CPU_NEON;
    eng->ctx = NULL;

    return eng;
}

#endif /* ARM_NEON */
