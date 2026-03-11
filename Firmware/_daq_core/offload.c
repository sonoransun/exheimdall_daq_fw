/*
 * Offload Engine Abstraction - Factory and utilities
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Copyright (C) 2018-2026 Tamás Pető
 */

#include <stdlib.h>
#include <string.h>
#include "offload.h"
#include "log.h"

offload_engine_t offload_engine_from_string(const char* str)
{
    if (str == NULL || strcmp(str, "auto") == 0)
        return OFFLOAD_AUTO;
    if (strcmp(str, "cpu_neon") == 0 || strcmp(str, "neon") == 0)
        return OFFLOAD_CPU_NEON;
    if (strcmp(str, "cpu_kfr") == 0 || strcmp(str, "kfr") == 0)
        return OFFLOAD_CPU_KFR;
    if (strcmp(str, "fpga") == 0)
        return OFFLOAD_FPGA;
    if (strcmp(str, "gpu") == 0)
        return OFFLOAD_GPU;

    log_warn("Unknown offload engine '%s', using auto", str);
    return OFFLOAD_AUTO;
}

offload_engine_t offload_auto_detect(void)
{
    /* Priority: FPGA > GPU > CPU NEON > CPU KFR */
#ifdef HAS_FPGA_OFFLOAD
    /* TODO: probe FPGA hardware before selecting */
#endif

#ifdef HAS_GPU_OFFLOAD
    /* TODO: probe GPU before selecting */
#endif

#ifdef ARM_NEON
    return OFFLOAD_CPU_NEON;
#else
    return OFFLOAD_CPU_KFR;
#endif
}

struct fir_engine* fir_engine_create(offload_engine_t type)
{
    if (type == OFFLOAD_AUTO)
        type = offload_auto_detect();

    struct fir_engine* eng = NULL;

    switch (type) {
#ifdef ARM_NEON
    case OFFLOAD_CPU_NEON:
        eng = fir_engine_neon_create();
        break;
#endif

#ifndef ARM_NEON
    case OFFLOAD_CPU_KFR:
        eng = fir_engine_kfr_create();
        break;
#endif

#ifdef HAS_FPGA_OFFLOAD
    case OFFLOAD_FPGA:
        eng = fir_engine_fpga_create();
        break;
#endif

#ifdef HAS_GPU_OFFLOAD
    case OFFLOAD_GPU:
        eng = fir_engine_gpu_create();
        break;
#endif

    default:
        break;
    }

    if (eng == NULL) {
        log_warn("FIR engine type %d unavailable, falling back to platform default", type);
        /* Fall back to compile-time default */
#ifdef ARM_NEON
        eng = fir_engine_neon_create();
#else
        eng = fir_engine_kfr_create();
#endif
    }

    return eng;
}

struct convert_engine* convert_engine_create(offload_engine_t type)
{
    if (type == OFFLOAD_AUTO)
        type = offload_auto_detect();

    struct convert_engine* eng = NULL;

    switch (type) {
#ifdef ARM_NEON
    case OFFLOAD_CPU_NEON:
        eng = convert_engine_neon_create();
        break;
#endif

#ifndef ARM_NEON
    case OFFLOAD_CPU_KFR:
        eng = convert_engine_kfr_create();
        break;
#endif

    default:
        break;
    }

    if (eng == NULL) {
#ifdef ARM_NEON
        eng = convert_engine_neon_create();
#else
        eng = convert_engine_kfr_create();
#endif
    }

    return eng;
}
