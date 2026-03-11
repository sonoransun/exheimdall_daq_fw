/*
 * Transport Abstraction Layer - Factory and utilities
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Copyright (C) 2018-2026 Tamás Pető
 */

#include <stdlib.h>
#include <string.h>
#include "transport.h"
#include "log.h"

transport_type_t transport_type_from_string(const char* str)
{
    if (str == NULL || strcmp(str, "shm") == 0 || strcmp(str, "auto") == 0)
        return TRANSPORT_SHM;
    if (strcmp(str, "spi") == 0)
        return TRANSPORT_SPI;
    if (strcmp(str, "pcie") == 0)
        return TRANSPORT_PCIE;
    if (strcmp(str, "usb3") == 0)
        return TRANSPORT_USB3;
    if (strcmp(str, "net") == 0 || strcmp(str, "eth") == 0)
        return TRANSPORT_NET;

    log_warn("Unknown transport type '%s', falling back to shm", str);
    return TRANSPORT_SHM;
}

const struct transport_ops* transport_get_ops(transport_type_t type)
{
    switch (type) {
    case TRANSPORT_SHM:
        return transport_shm_get_ops();

#ifdef HAS_SPI_TRANSPORT
    case TRANSPORT_SPI:
        return transport_spi_get_ops();
#endif

#ifdef HAS_PCIE_TRANSPORT
    case TRANSPORT_PCIE:
        return transport_pcie_get_ops();
#endif

#ifdef HAS_USB3_TRANSPORT
    case TRANSPORT_USB3:
        return transport_usb3_get_ops();
#endif

#ifdef HAS_NET_TRANSPORT
    case TRANSPORT_NET:
        return transport_net_get_ops();
#endif

    default:
        log_warn("Transport type %d not available, falling back to shm", type);
        return transport_shm_get_ops();
    }
}

struct transport_handle* transport_create(
    const char* channel_name,
    size_t buffer_size,
    bool is_producer,
    flow_control_t flow_control,
    int instance_id,
    transport_type_t type)
{
    const struct transport_ops* ops = transport_get_ops(type);
    if (ops == NULL) {
        log_error("No transport ops available for type %d", type);
        return NULL;
    }

    struct transport_handle* th = calloc(1, sizeof(struct transport_handle));
    if (th == NULL) {
        log_error("Failed to allocate transport handle");
        return NULL;
    }

    th->type = type;
    th->flow_control = flow_control;
    th->is_producer = is_producer;
    th->buffer_size = buffer_size;
    th->num_buffers = 2;  /* double-buffering */
    th->instance_id = instance_id;
    th->ops = ops;
    th->priv = NULL;
    th->total_bytes = 0;
    th->total_frames = 0;
    th->dropped_frames = 0;

    strncpy(th->channel_name, channel_name, sizeof(th->channel_name) - 1);
    th->channel_name[sizeof(th->channel_name) - 1] = '\0';

    return th;
}
