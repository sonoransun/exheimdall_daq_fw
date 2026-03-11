/*
 * Transport Abstraction Layer for HeIMDALL DAQ Firmware
 *
 * Provides a vtable-dispatched interface for pipeline inter-process
 * data transport. Supports shared memory (default), SPI+DMA, PCIe,
 * USB 3.0, and network transports.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 *
 * Copyright (C) 2018-2026 Tamás Pető
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * any later version.
 */

#ifndef TRANSPORT_H
#define TRANSPORT_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

/*
 *-------------------------------------
 *       Transport Types
 *-------------------------------------
 */
typedef enum {
    TRANSPORT_SHM   = 0,  /* POSIX shared memory (default) */
    TRANSPORT_SPI   = 1,  /* SPI + DMA (FPGA HAT) */
    TRANSPORT_PCIE  = 2,  /* PCIe (CM4 accelerator) */
    TRANSPORT_USB3  = 3,  /* USB 3.0 (USB accelerator) */
    TRANSPORT_NET   = 4,  /* Network/Ethernet (distributed) */
} transport_type_t;

/* Flow control modes */
typedef enum {
    FLOW_BACKPRESSURE = 0,  /* Block until buffer available */
    FLOW_DROP         = 1,  /* Drop frame if no buffer available */
} flow_control_t;

/* Forward declaration */
struct transport_handle;

/*
 *-------------------------------------
 *   Transport Operations (vtable)
 *-------------------------------------
 *
 * Each transport driver implements these function pointers.
 * Semantics mirror the existing shmem protocol:
 *   init_producer   -> init_out_sm_buffer
 *   init_consumer   -> init_in_sm_buffer
 *   destroy         -> destory_sm_buffer
 *   get_write_buf   -> wait_buff_free (producer side)
 *   submit_write    -> send_ctr_buff_ready
 *   get_read_buf    -> wait_buff_ready (consumer side)
 *   release_read    -> send_ctr_buff_free
 *   send_terminate  -> send_ctr_terminate
 */
struct transport_ops {
    int  (*init_producer)(struct transport_handle* th);
    int  (*init_consumer)(struct transport_handle* th);
    void (*destroy)(struct transport_handle* th);

    /* Producer: acquire writable buffer, returns buffer index (0/1) or negative on error, 3=dropped */
    int  (*get_write_buf)(struct transport_handle* th, void** buf_ptr);
    /* Producer: mark buffer as ready for consumer */
    int  (*submit_write)(struct transport_handle* th, int buf_index);

    /* Consumer: wait for buffer ready, returns buffer index (0/1) or TERMINATE(255) */
    int  (*get_read_buf)(struct transport_handle* th, void** buf_ptr);
    /* Consumer: release buffer back to producer */
    int  (*release_read)(struct transport_handle* th, int buf_index);

    /* Terminate signal */
    void (*send_terminate)(struct transport_handle* th);
};

/*
 *-------------------------------------
 *       Transport Handle
 *-------------------------------------
 */
struct transport_handle {
    transport_type_t  type;
    flow_control_t    flow_control;
    bool              is_producer;     /* true=output, false=input */
    size_t            buffer_size;     /* total buffer size in bytes (header + payload) */
    int               num_buffers;     /* number of buffers (2 for double-buffering) */
    int               instance_id;     /* federation instance id */

    char              channel_name[512]; /* logical channel name, e.g. "decimator_in" */

    const struct transport_ops* ops;   /* vtable */
    void*             priv;            /* transport-specific private state */

    /* Performance counters */
    uint64_t          total_bytes;
    uint64_t          total_frames;
    uint64_t          dropped_frames;
};

/*
 *-------------------------------------
 *       Factory Functions
 *-------------------------------------
 */

/* Create a transport handle. Returns NULL on failure. */
struct transport_handle* transport_create(
    const char* channel_name,
    size_t buffer_size,
    bool is_producer,
    flow_control_t flow_control,
    int instance_id,
    transport_type_t type
);

/* Parse transport type from config string */
transport_type_t transport_type_from_string(const char* str);

/* Get registered ops for a transport type (used internally) */
const struct transport_ops* transport_get_ops(transport_type_t type);

/*
 *-------------------------------------
 *   Convenience Dispatch Wrappers
 *-------------------------------------
 */

static inline int transport_init(struct transport_handle* th)
{
    if (th->is_producer)
        return th->ops->init_producer(th);
    else
        return th->ops->init_consumer(th);
}

static inline void transport_destroy(struct transport_handle* th)
{
    if (th && th->ops && th->ops->destroy)
        th->ops->destroy(th);
}

static inline int transport_get_write_buf(struct transport_handle* th, void** buf_ptr)
{
    return th->ops->get_write_buf(th, buf_ptr);
}

static inline int transport_submit_write(struct transport_handle* th, int buf_index)
{
    th->total_frames++;
    return th->ops->submit_write(th, buf_index);
}

static inline int transport_get_read_buf(struct transport_handle* th, void** buf_ptr)
{
    return th->ops->get_read_buf(th, buf_ptr);
}

static inline int transport_release_read(struct transport_handle* th, int buf_index)
{
    return th->ops->release_read(th, buf_index);
}

static inline void transport_send_terminate(struct transport_handle* th)
{
    if (th && th->ops && th->ops->send_terminate)
        th->ops->send_terminate(th);
}

/*
 *-------------------------------------
 *   Transport Driver Registration
 *-------------------------------------
 * Each transport_*.c file provides a get_ops function.
 * These are declared here and resolved at link time.
 */
extern const struct transport_ops* transport_shm_get_ops(void);

/* Optional drivers -- may not be linked */
#ifdef HAS_SPI_TRANSPORT
extern const struct transport_ops* transport_spi_get_ops(void);
#endif
#ifdef HAS_PCIE_TRANSPORT
extern const struct transport_ops* transport_pcie_get_ops(void);
#endif
#ifdef HAS_USB3_TRANSPORT
extern const struct transport_ops* transport_usb3_get_ops(void);
#endif
#ifdef HAS_NET_TRANSPORT
extern const struct transport_ops* transport_net_get_ops(void);
#endif

#endif /* TRANSPORT_H */
