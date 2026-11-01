/*
 * Description :
 * Shared-memory frame size helpers of the C data plane (rebuffer.out,
 * decimate.out, iq_server.out). Every expression is evaluated in size_t, so
 * no product of config values can overflow int (num_ch = 32 used to wrap the
 * worst-case segment size, G23).
 *
 * The helpers are the single C definition of the per-segment sizes that the
 * Python capacity model (capacity_model.frame_sizes) mirrors key for key and
 * test_frame_sizes.py cross-checks:
 *
 *   hd_iq_frame_bytes(M, Ncpi, Nc)       = 1024 + max(Ncpi, Nc) * M * 8
 *       Exactly the delay_sync_iq segment delay_sync.py creates
 *       (1024 + max(N, N_proc) * 2 * M * 4) and the decimator_out object
 *       decimate.out creates under [pre_processing] size_segments_from_config = 1.
 *   hd_worst_frame_bytes(M)              = 1024 + 2^23 * M * 8
 *       The worst-case cf32 segment (MAX_IQFRAME_PAYLOAD_SIZE samples per
 *       channel) used when size_segments_from_config = 0 (default).
 *   hd_decimator_in_object_bytes(M, Ncpi, R, Nc)
 *                                        = 1024 + max(Ncpi * R, Nc) * M * 2
 *       The decimator_in object rebuffer.out creates (u8 I/Q).
 *   hd_decimator_in_consumer_map_bytes(M, Ncpi, R, Nc)
 *                                        = 1024 + max(Ncpi * R, Nc) * M * 8
 *       What decimate.out maps of decimator_in (8 bytes per complex sample,
 *       a 4x over-map of the object; pages past the object end are never
 *       touched).
 *
 * The header is self-contained (only <stddef.h>) so a test probe can include
 * it alone. When it is included after iq_header.h / rtl_daq.h, its constants
 * are checked against IQ_HEADER_LENGTH / MAX_IQFRAME_PAYLOAD_SIZE.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 */
#ifndef HEIMDALL_FRAME_SIZES_H
#define HEIMDALL_FRAME_SIZES_H

#include <stddef.h>

#define HD_IQ_HEADER_BYTES       1024u
#define HD_MAX_FRAME_SAMPLES     8388608u  /* 2^23 samples per channel */
#define HD_CF32_SAMPLE_BYTES     8u        /* complex float32 */
#define HD_CU8_SAMPLE_BYTES      2u        /* complex uint8 */

#ifdef IQ_HEADER_LENGTH
_Static_assert(HD_IQ_HEADER_BYTES == IQ_HEADER_LENGTH,
               "frame_sizes.h: HD_IQ_HEADER_BYTES must equal IQ_HEADER_LENGTH");
#endif
#ifdef MAX_IQFRAME_PAYLOAD_SIZE
_Static_assert(HD_MAX_FRAME_SAMPLES == MAX_IQFRAME_PAYLOAD_SIZE,
               "frame_sizes.h: HD_MAX_FRAME_SAMPLES must equal MAX_IQFRAME_PAYLOAD_SIZE");
#endif

static inline size_t hd_max_size(size_t a, size_t b)
{
    return (a >= b) ? a : b;
}

/* cf32 IQ frame: header + max(cpi_size, corr_size) samples per channel */
static inline size_t hd_iq_frame_bytes(int M, int Ncpi, int Nc)
{
    return HD_IQ_HEADER_BYTES
         + hd_max_size((size_t)Ncpi, (size_t)Nc) * (size_t)M * HD_CF32_SAMPLE_BYTES;
}

/* cf32 IQ frame sized for the 2^23-sample worst case */
static inline size_t hd_worst_frame_bytes(int M)
{
    return HD_IQ_HEADER_BYTES
         + (size_t)HD_MAX_FRAME_SAMPLES * (size_t)M * HD_CF32_SAMPLE_BYTES;
}

/* Samples per channel of the largest frame rebuffer.out emits */
static inline size_t hd_decimator_in_samples(int Ncpi, int R, int Nc)
{
    return hd_max_size((size_t)Ncpi * (size_t)R, (size_t)Nc);
}

/* decimator_in object as created by rebuffer.out (u8 I/Q) */
static inline size_t hd_decimator_in_object_bytes(int M, int Ncpi, int R, int Nc)
{
    return HD_IQ_HEADER_BYTES
         + hd_decimator_in_samples(Ncpi, R, Nc) * (size_t)M * HD_CU8_SAMPLE_BYTES;
}

/* decimator_in mapping length used by decimate.out (virtual only) */
static inline size_t hd_decimator_in_consumer_map_bytes(int M, int Ncpi, int R, int Nc)
{
    return HD_IQ_HEADER_BYTES
         + hd_decimator_in_samples(Ncpi, R, Nc) * (size_t)M * HD_CF32_SAMPLE_BYTES;
}

#endif /* HEIMDALL_FRAME_SIZES_H */
