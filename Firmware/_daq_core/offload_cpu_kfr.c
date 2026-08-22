/*
 * CPU KFR Offload Engine (x86) - FIR Decimation via KFR library
 *
 * Extracted from fir_decimate.c x86 path for the offload abstraction.
 *
 * KFR's FIR filter is a 1:1 stateful convolution (input_len samples in,
 * input_len samples out), so decimation is performed here by filtering
 * at full rate into a scratch buffer and keeping every dec_ratio-th
 * sample: y[n] = y_full[n*dec_ratio], the same output instants as the
 * Ne10/CMSIS decimator used on ARM. One plan per channel per I/Q
 * component keeps the FIR delay lines isolated between channels.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Copyright (C) 2018-2026 Tamás Pető
 */

#if !defined(ARM_NEON) && !defined(OFFLOAD_GENERIC_ONLY)

#include <stdlib.h>
#include <string.h>
#include "offload.h"
#include "log.h"
#include <kfr/capi.h>

#define DC 127.5f

struct kfr_fir_ctx {
    KFR_FILTER_F32** filter_plans;  /* 2 per channel: [2*ch]=I, [2*ch+1]=Q */
    kfr_f32* coeffs;
    kfr_f32* scratch;               /* full-rate filter output before subsampling */
    kfr_f32* zeros;                 /* tap_size zeros used to flush filter state */
    size_t scratch_len;
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

    if (num_channels < 1 || dec_ratio < 1 || tap_size < 1) {
        log_error("Invalid KFR FIR engine parameters");
        return -1;
    }

    ctx->num_channels = num_channels;
    ctx->dec_ratio = dec_ratio;
    ctx->block_size = block_size;
    ctx->tap_size = tap_size;
    ctx->scratch_len = block_size * (size_t)dec_ratio;
    if (ctx->scratch_len < tap_size)
        ctx->scratch_len = tap_size;

    /* Copy coefficients into KFR-aligned buffer */
    ctx->coeffs = kfr_allocate(tap_size * sizeof(kfr_f32));
    if (!ctx->coeffs) return -1;
    memcpy(ctx->coeffs, coeffs, tap_size * sizeof(float));

    ctx->scratch = kfr_allocate(ctx->scratch_len * sizeof(kfr_f32));
    if (!ctx->scratch) return -1;

    ctx->zeros = kfr_allocate(tap_size * sizeof(kfr_f32));
    if (!ctx->zeros) return -1;
    memset(ctx->zeros, 0, tap_size * sizeof(kfr_f32));

    /* One stateful plan per channel per I/Q component, so delay lines
     * never leak samples between channels or components */
    ctx->filter_plans = calloc((size_t)num_channels * 2, sizeof(KFR_FILTER_F32*));
    if (!ctx->filter_plans) return -1;

    for (int m = 0; m < num_channels * 2; m++) {
        ctx->filter_plans[m] = kfr_filter_create_fir_plan_f32(ctx->coeffs, tap_size);
        if (!ctx->filter_plans[m]) {
            log_error("Failed to create KFR FIR filter plan %d", m);
            return -1;
        }
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
        if (ctx->filter_plans) {
            for (int m = 0; m < ctx->num_channels * 2; m++) {
                if (ctx->filter_plans[m])
                    kfr_filter_delete_plan_f32(ctx->filter_plans[m]);
            }
            free(ctx->filter_plans);
        }
        if (ctx->coeffs)
            kfr_deallocate(ctx->coeffs);
        if (ctx->scratch)
            kfr_deallocate(ctx->scratch);
        if (ctx->zeros)
            kfr_deallocate(ctx->zeros);
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
    const size_t dec = (size_t)ctx->dec_ratio;
    const size_t output_len = input_len / dec;

    if (!ctx->initialized || ch_index < 0 || ch_index >= ctx->num_channels)
        return -1;
    if (input_len > ctx->scratch_len)
        return -1;

    kfr_filter_process_f32(ctx->filter_plans[2 * ch_index],
                           ctx->scratch,
                           (const kfr_f32*)input_i,
                           input_len);
    for (size_t n = 0; n < output_len; n++)
        output_i[n] = ctx->scratch[n * dec];

    kfr_filter_process_f32(ctx->filter_plans[2 * ch_index + 1],
                           ctx->scratch,
                           (const kfr_f32*)input_q,
                           input_len);
    for (size_t n = 0; n < output_len; n++)
        output_q[n] = ctx->scratch[n * dec];

    return 0;
}

static void kfr_fir_reset(struct fir_engine* eng, int ch_index)
{
    struct kfr_fir_ctx* ctx = (struct kfr_fir_ctx*)eng->ctx;
    if (!ctx->initialized || ch_index < 0 || ch_index >= ctx->num_channels)
        return;

    /* A FIR filter's state is its last tap_size-1 input samples: flushing
     * tap_size zeros through the plan clears it without rebuilding plans */
    kfr_filter_process_f32(ctx->filter_plans[2 * ch_index],
                           ctx->scratch, ctx->zeros, ctx->tap_size);
    kfr_filter_process_f32(ctx->filter_plans[2 * ch_index + 1],
                           ctx->scratch, ctx->zeros, ctx->tap_size);
    log_trace("KFR FIR state reset, channel %d", ch_index);
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
    const uint8_t* restrict src = in;
    float* restrict di = out_i;
    float* restrict dq = out_q;
    for (size_t s = 0; s < num_samples; s++) {
        di[s] = ((float)src[2 * s]     - DC) / DC;
        dq[s] = ((float)src[2 * s + 1] - DC) / DC;
    }
    return 0;
}

static int kfr_u8_to_f32_interleaved(struct convert_engine* eng,
                                      const uint8_t* in, float* out,
                                      size_t num_samples)
{
    (void)eng;
    const uint8_t* restrict src = in;
    float* restrict dst = out;
    const size_t total = num_samples * 2;
    for (size_t n = 0; n < total; n++)
        dst[n] = ((float)src[n] - DC) / DC;
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

#endif /* !ARM_NEON && !OFFLOAD_GENERIC_ONLY */
