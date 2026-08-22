/*
 * CPU Generic Offload Engine - portable, dependency-free FIR decimation
 *
 * Plain C99 direct-form FIR decimator and U8->F32 converter with no
 * external DSP library. Last-resort fallback engine and the engine used
 * by dependency-light/CI builds (see OFFLOAD_GENERIC_ONLY in offload.h).
 *
 * Output contract (matches the platform engines): per-channel stateful
 * FIR with zero initial state, y[n] = sum_k coeffs[k] * x[n*dec_ratio - k],
 * output length = input_len / dec_ratio.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Copyright (C) 2018-2026 Tamás Pető
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
 */

#include <stdlib.h>
#include <string.h>
#include "offload.h"
#include "log.h"

#define DC 127.5f

struct generic_fir_ctx {
    float* coeffs;
    float** history;      /* 2 per channel (I,Q): last tap_size-1 input samples */
    float* work;          /* history prefix + one full-rate input block */
    size_t work_len;      /* (tap_size-1) + max_input_len */
    size_t max_input_len; /* block_size * dec_ratio */
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

static int generic_fir_init(struct fir_engine* eng, const float* coeffs,
                            size_t tap_size, int dec_ratio, size_t block_size,
                            int num_channels)
{
    struct generic_fir_ctx* ctx = (struct generic_fir_ctx*)eng->ctx;

    if (num_channels < 1 || dec_ratio < 1 || tap_size < 1) {
        log_error("Invalid generic FIR engine parameters");
        return -1;
    }

    ctx->num_channels = num_channels;
    ctx->dec_ratio = dec_ratio;
    ctx->block_size = block_size;
    ctx->tap_size = tap_size;
    ctx->max_input_len = block_size * (size_t)dec_ratio;
    ctx->work_len = (tap_size - 1) + ctx->max_input_len;

    ctx->coeffs = malloc(tap_size * sizeof(float));
    if (!ctx->coeffs) return -1;
    memcpy(ctx->coeffs, coeffs, tap_size * sizeof(float));

    ctx->work = malloc(ctx->work_len * sizeof(float));
    if (!ctx->work) return -1;

    ctx->history = calloc((size_t)num_channels * 2, sizeof(float*));
    if (!ctx->history) return -1;

    size_t hist_alloc = (tap_size > 1) ? (tap_size - 1) : 1;
    for (int m = 0; m < num_channels * 2; m++) {
        ctx->history[m] = calloc(hist_alloc, sizeof(float));
        if (!ctx->history[m]) {
            log_error("Failed to allocate FIR history vector %d", m);
            return -1;
        }
    }

    ctx->initialized = true;
    log_info("Generic FIR engine initialized: %d channels, %zu taps, %dx decimation",
             num_channels, tap_size, dec_ratio);
    return 0;
}

static void generic_fir_destroy(struct fir_engine* eng)
{
    struct generic_fir_ctx* ctx = (struct generic_fir_ctx*)eng->ctx;
    if (ctx) {
        if (ctx->history) {
            for (int m = 0; m < ctx->num_channels * 2; m++)
                free(ctx->history[m]);
            free(ctx->history);
        }
        free(ctx->work);
        free(ctx->coeffs);
        free(ctx);
    }
    free(eng);
}

static void generic_fir_run(struct generic_fir_ctx* ctx, float* history,
                            const float* input, float* output, size_t input_len)
{
    const size_t hist_len = ctx->tap_size - 1;
    const size_t dec = (size_t)ctx->dec_ratio;
    const size_t output_len = input_len / dec;
    const float* restrict coeffs = ctx->coeffs;
    float* restrict work = ctx->work;

    /* Assemble [previous tap_size-1 samples | new input] so every FIR
     * evaluation reads from one contiguous buffer. */
    memcpy(work, history, hist_len * sizeof(float));
    memcpy(work + hist_len, input, input_len * sizeof(float));

    for (size_t n = 0; n < output_len; n++) {
        const float* x = work + hist_len + n * dec; /* newest sample of output n */
        float acc = 0.0f;
        for (size_t k = 0; k < ctx->tap_size; k++)
            acc += coeffs[k] * x[-(ptrdiff_t)k];
        output[n] = acc;
    }

    /* Next call's history: the last tap_size-1 samples of the stream */
    memcpy(history, work + input_len, hist_len * sizeof(float));
}

static int generic_fir_decimate(struct fir_engine* eng, int ch_index,
                                const float* input_i, const float* input_q,
                                float* output_i, float* output_q,
                                size_t input_len)
{
    struct generic_fir_ctx* ctx = (struct generic_fir_ctx*)eng->ctx;

    if (!ctx->initialized || ch_index < 0 || ch_index >= ctx->num_channels)
        return -1;
    if (input_len > ctx->max_input_len)
        return -1;

    generic_fir_run(ctx, ctx->history[2 * ch_index],     input_i, output_i, input_len);
    generic_fir_run(ctx, ctx->history[2 * ch_index + 1], input_q, output_q, input_len);
    return 0;
}

static void generic_fir_reset(struct fir_engine* eng, int ch_index)
{
    struct generic_fir_ctx* ctx = (struct generic_fir_ctx*)eng->ctx;
    if (!ctx->initialized || ch_index < 0 || ch_index >= ctx->num_channels)
        return;
    memset(ctx->history[2 * ch_index], 0, (ctx->tap_size - 1) * sizeof(float));
    memset(ctx->history[2 * ch_index + 1], 0, (ctx->tap_size - 1) * sizeof(float));
}

struct fir_engine* fir_engine_generic_create(void)
{
    struct fir_engine* eng = calloc(1, sizeof(struct fir_engine));
    if (!eng) return NULL;

    struct generic_fir_ctx* ctx = calloc(1, sizeof(struct generic_fir_ctx));
    if (!ctx) { free(eng); return NULL; }

    eng->init = generic_fir_init;
    eng->destroy = generic_fir_destroy;
    eng->decimate = generic_fir_decimate;
    eng->reset = generic_fir_reset;
    eng->type = OFFLOAD_CPU_GENERIC;
    eng->ctx = ctx;

    return eng;
}

/*
 *-------------------------------------
 *   Convert Engine Implementation
 *-------------------------------------
 */

static int generic_u8_to_f32_deinterleave(struct convert_engine* eng,
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

static int generic_u8_to_f32_interleaved(struct convert_engine* eng,
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

struct convert_engine* convert_engine_generic_create(void)
{
    struct convert_engine* eng = calloc(1, sizeof(struct convert_engine));
    if (!eng) return NULL;

    eng->u8_to_f32_deinterleave = generic_u8_to_f32_deinterleave;
    eng->u8_to_f32_interleaved = generic_u8_to_f32_interleaved;
    eng->type = OFFLOAD_CPU_GENERIC;
    eng->ctx = NULL;

    return eng;
}
