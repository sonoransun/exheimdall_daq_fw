/*
 * CPU KFR Offload Engine (x86) - FIR Decimation via KFR library
 *
 * Extracted from fir_decimate.c x86 path for the offload abstraction.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Copyright (C) 2018-2026 Tamás Pető
 */

#ifndef ARM_NEON

#include <stdlib.h>
#include <string.h>
#include "offload.h"
#include "log.h"
#include <kfr/capi.h>

#define DC 127.5f

struct kfr_fir_ctx {
    KFR_FILTER_F32* filter_plan;
    kfr_f32* coeffs;
    int num_channels;
    int dec_ratio;
    size_t block_size;
    size_t tap_size;
    bool initialized;
};

/*
 *-------------------------------------
 *   FIR Engine Implementation
 *-------------------------------------
 */

static int kfr_fir_init(struct fir_engine* eng, const float* coeffs,
                        size_t tap_size, int dec_ratio, size_t block_size,
                        int num_channels)
{
    struct kfr_fir_ctx* ctx = (struct kfr_fir_ctx*)eng->ctx;

    ctx->num_channels = num_channels;
    ctx->dec_ratio = dec_ratio;
    ctx->block_size = block_size;
    ctx->tap_size = tap_size;

    /* Copy coefficients into KFR-aligned buffer */
    ctx->coeffs = kfr_allocate(tap_size * sizeof(kfr_f32));
    if (!ctx->coeffs) return -1;
    memcpy(ctx->coeffs, coeffs, tap_size * sizeof(float));

    /* Create KFR filter plan */
    ctx->filter_plan = kfr_filter_create_fir_plan_f32(ctx->coeffs, tap_size);
    if (!ctx->filter_plan) {
        log_error("Failed to create KFR FIR filter plan");
        return -1;
    }

    ctx->initialized = true;
    log_info("KFR FIR engine initialized: %d channels, %zu taps, %dx decimation",
             num_channels, tap_size, dec_ratio);
    return 0;
}

static void kfr_fir_destroy(struct fir_engine* eng)
{
    struct kfr_fir_ctx* ctx = (struct kfr_fir_ctx*)eng->ctx;
    if (ctx) {
        if (ctx->filter_plan)
            kfr_filter_destroy_plan_f32(ctx->filter_plan);
        if (ctx->coeffs)
            kfr_deallocate(ctx->coeffs);
        free(ctx);
    }
    free(eng);
}

static int kfr_fir_decimate(struct fir_engine* eng, int ch_index,
                            const float* input_i, const float* input_q,
                            float* output_i, float* output_q,
                            size_t input_len)
{
    struct kfr_fir_ctx* ctx = (struct kfr_fir_ctx*)eng->ctx;
    (void)ch_index;

    /* Apply FIR filter to I and Q channels */
    kfr_filter_process_f32(ctx->filter_plan,
                           (kfr_f32*)output_i,
                           (const kfr_f32*)input_i,
                           input_len);

    kfr_filter_process_f32(ctx->filter_plan,
                           (kfr_f32*)output_q,
                           (const kfr_f32*)input_q,
                           input_len);

    return 0;
}

static void kfr_fir_reset(struct fir_engine* eng, int ch_index)
{
    struct kfr_fir_ctx* ctx = (struct kfr_fir_ctx*)eng->ctx;
    (void)ch_index;

    /* KFR filter reset: destroy and recreate plan */
    if (ctx->filter_plan) {
        kfr_filter_destroy_plan_f32(ctx->filter_plan);
        ctx->filter_plan = kfr_filter_create_fir_plan_f32(ctx->coeffs, ctx->tap_size);
    }
    log_warn("KFR filter reset via plan recreation");
}

struct fir_engine* fir_engine_kfr_create(void)
{
    struct fir_engine* eng = calloc(1, sizeof(struct fir_engine));
    if (!eng) return NULL;

    struct kfr_fir_ctx* ctx = calloc(1, sizeof(struct kfr_fir_ctx));
    if (!ctx) { free(eng); return NULL; }

    eng->init = kfr_fir_init;
    eng->destroy = kfr_fir_destroy;
    eng->decimate = kfr_fir_decimate;
    eng->reset = kfr_fir_reset;
    eng->type = OFFLOAD_CPU_KFR;
    eng->ctx = ctx;

    return eng;
}

/*
 *-------------------------------------
 *   Convert Engine Implementation
 *-------------------------------------
 */

static int kfr_u8_to_f32_deinterleave(struct convert_engine* eng,
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

static int kfr_u8_to_f32_interleaved(struct convert_engine* eng,
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

struct convert_engine* convert_engine_kfr_create(void)
{
    struct convert_engine* eng = calloc(1, sizeof(struct convert_engine));
    if (!eng) return NULL;

    eng->u8_to_f32_deinterleave = kfr_u8_to_f32_deinterleave;
    eng->u8_to_f32_interleaved = kfr_u8_to_f32_interleaved;
    eng->type = OFFLOAD_CPU_KFR;
    eng->ctx = NULL;

    return eng;
}

#endif /* !ARM_NEON */
