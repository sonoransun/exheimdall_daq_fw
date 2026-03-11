/*
 * Offload Engine Abstraction for HeIMDALL DAQ Firmware
 *
 * Provides runtime-dispatched compute engines for signal processing
 * operations. Replaces compile-time #ifdef ARM_NEON selection with
 * vtable-based dispatch supporting CPU (NEON/KFR), FPGA, and GPU.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Copyright (C) 2018-2026 Tamás Pető
 */

#ifndef OFFLOAD_H
#define OFFLOAD_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

/*
 *-------------------------------------
 *       Offload Engine Types
 *-------------------------------------
 */
typedef enum {
    OFFLOAD_CPU_NEON  = 0,
    OFFLOAD_CPU_KFR   = 1,
    OFFLOAD_FPGA      = 2,
    OFFLOAD_GPU       = 3,
    OFFLOAD_AUTO      = 99,
} offload_engine_t;

/*
 *-------------------------------------
 *   FIR Decimation Engine Interface
 *-------------------------------------
 */
struct fir_engine {
    /* Initialize the engine with filter configuration.
     * coeffs:      FIR filter coefficients
     * tap_size:    number of taps
     * dec_ratio:   decimation ratio
     * block_size:  CPI size (output samples per channel)
     * num_channels: number of antenna channels
     * Returns 0 on success, negative on error.
     */
    int  (*init)(struct fir_engine* eng, const float* coeffs, size_t tap_size,
                 int dec_ratio, size_t block_size, int num_channels);

    /* Destroy engine and free resources */
    void (*destroy)(struct fir_engine* eng);

    /* Process one channel: apply FIR decimation.
     * ch_index:   channel index (0..num_channels-1)
     * input_i:    input I samples (length = block_size * dec_ratio)
     * input_q:    input Q samples (length = block_size * dec_ratio)
     * output_i:   output I samples (length = block_size)
     * output_q:   output Q samples (length = block_size)
     * input_len:  number of input samples (= block_size * dec_ratio)
     * Returns 0 on success.
     */
    int  (*decimate)(struct fir_engine* eng, int ch_index,
                     const float* input_i, const float* input_q,
                     float* output_i, float* output_q,
                     size_t input_len);

    /* Reset filter state for a channel (between frames if en_filter_reset) */
    void (*reset)(struct fir_engine* eng, int ch_index);

    /* Engine type identifier */
    offload_engine_t type;

    /* Engine-specific private context */
    void* ctx;
};

/*
 *-------------------------------------
 *   Data Conversion Engine Interface
 *-------------------------------------
 */
struct convert_engine {
    /* Convert U8 interleaved IQ to separate float I/Q streams.
     * Applies: out = (in - DC) / DC where DC = 127.5
     * in:     input U8 interleaved [I0,Q0,I1,Q1,...] (length = num_samples * 2)
     * out_i:  output float I samples
     * out_q:  output float Q samples
     * num_samples: number of IQ sample pairs
     */
    int (*u8_to_f32_deinterleave)(struct convert_engine* eng,
                                   const uint8_t* in,
                                   float* out_i, float* out_q,
                                   size_t num_samples);

    /* Convert U8 interleaved IQ to float interleaved [I0,Q0,I1,Q1,...].
     * No decimation, just format conversion.
     */
    int (*u8_to_f32_interleaved)(struct convert_engine* eng,
                                  const uint8_t* in, float* out,
                                  size_t num_samples);

    offload_engine_t type;
    void* ctx;
};

/*
 *-------------------------------------
 *       Factory Functions
 *-------------------------------------
 */

/* Create a FIR engine of the specified type. Returns NULL if unavailable. */
struct fir_engine* fir_engine_create(offload_engine_t type);

/* Create a data conversion engine. Returns NULL if unavailable. */
struct convert_engine* convert_engine_create(offload_engine_t type);

/* Parse engine type from config string */
offload_engine_t offload_engine_from_string(const char* str);

/* Auto-detect best available engine */
offload_engine_t offload_auto_detect(void);

/*
 *-------------------------------------
 *   Engine Registration
 *-------------------------------------
 */
#ifdef ARM_NEON
extern struct fir_engine* fir_engine_neon_create(void);
extern struct convert_engine* convert_engine_neon_create(void);
#endif

#ifndef ARM_NEON
extern struct fir_engine* fir_engine_kfr_create(void);
extern struct convert_engine* convert_engine_kfr_create(void);
#endif

#ifdef HAS_FPGA_OFFLOAD
extern struct fir_engine* fir_engine_fpga_create(void);
#endif

#ifdef HAS_GPU_OFFLOAD
extern struct fir_engine* fir_engine_gpu_create(void);
#endif

#endif /* OFFLOAD_H */
