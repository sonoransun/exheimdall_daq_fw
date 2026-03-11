/*
 * Shared Memory Transport Driver
 *
 * Wraps existing sh_mem_util.c to implement the transport_ops interface.
 * This is the backward-compatible default transport -- runtime behavior
 * is identical to the original direct shmem usage.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Copyright (C) 2018-2026 Tamás Pető
 */

#include <stdlib.h>
#include <string.h>
#include "transport.h"
#include "sh_mem_util.h"
#include "log.h"

/*
 * Shared memory name/FIFO path mapping.
 * Maps logical channel names to the existing #define'd constants.
 */
struct shm_channel_map {
    const char* channel_name;
    const char* sm_name_a;
    const char* sm_name_b;
    const char* fw_fifo;
    const char* bw_fifo;
};

static const struct shm_channel_map shm_channels[] = {
    { "gen_frame",
      GEN_FRAME_SM_NAME_A, GEN_FRAME_SM_NAME_B,
      GEN_FRAME_FW_FIFO, GEN_FRAME_BW_FIFO },
    { "decimator_in",
      DECIMATOR_IN_SM_NAME_A, DECIMATOR_IN_SM_NAME_B,
      DECIMATOR_IN_FW_FIFO, DECIMATOR_IN_BW_FIFO },
    { "decimator_out",
      DECIMATOR_OUT_SM_NAME_A, DECIMATOR_OUT_SM_NAME_B,
      DECIMATOR_OUT_FW_FIFO, DECIMATOR_OUT_BW_FIFO },
    { "delay_sync_iq",
      DELAY_SYNC_IQ_SM_NAME_A, DELAY_SYNC_IQ_SM_NAME_B,
      DELAY_SYNC_IQ_FW_FIFO, DELAY_SYNC_IQ_BW_FIFO },
    { NULL, NULL, NULL, NULL, NULL }
};

/* Private state: wraps the existing shmem_transfer_struct */
struct shm_priv {
    struct shmem_transfer_struct sm;
};

static const struct shm_channel_map* find_channel(const char* name)
{
    for (int i = 0; shm_channels[i].channel_name != NULL; i++) {
        if (strcmp(shm_channels[i].channel_name, name) == 0)
            return &shm_channels[i];
    }
    return NULL;
}

static int shm_init_producer(struct transport_handle* th)
{
    const struct shm_channel_map* ch = find_channel(th->channel_name);
    if (ch == NULL) {
        log_error("Unknown SHM channel: %s", th->channel_name);
        return -1;
    }

    struct shm_priv* priv = calloc(1, sizeof(struct shm_priv));
    if (priv == NULL) return -1;

    priv->sm.shared_memory_size = th->buffer_size;
    priv->sm.io_type = 0; /* Output */
    priv->sm.drop_mode = (th->flow_control == FLOW_DROP);

    build_shmem_name(priv->sm.shared_memory_names[0], th->instance_id, ch->sm_name_a);
    build_shmem_name(priv->sm.shared_memory_names[1], th->instance_id, ch->sm_name_b);
    build_fifo_path(priv->sm.fw_ctr_fifo_name, th->instance_id, ch->fw_fifo);
    build_fifo_path(priv->sm.bw_ctr_fifo_name, th->instance_id, ch->bw_fifo);

    th->priv = priv;

    int ret = init_out_sm_buffer(&priv->sm);
    if (ret != 0) {
        log_error("SHM producer init failed for channel %s", th->channel_name);
        free(priv);
        th->priv = NULL;
    }
    return ret;
}

static int shm_init_consumer(struct transport_handle* th)
{
    const struct shm_channel_map* ch = find_channel(th->channel_name);
    if (ch == NULL) {
        log_error("Unknown SHM channel: %s", th->channel_name);
        return -1;
    }

    struct shm_priv* priv = calloc(1, sizeof(struct shm_priv));
    if (priv == NULL) return -1;

    priv->sm.shared_memory_size = th->buffer_size;
    priv->sm.io_type = 1; /* Input */

    build_shmem_name(priv->sm.shared_memory_names[0], th->instance_id, ch->sm_name_a);
    build_shmem_name(priv->sm.shared_memory_names[1], th->instance_id, ch->sm_name_b);
    build_fifo_path(priv->sm.fw_ctr_fifo_name, th->instance_id, ch->fw_fifo);
    build_fifo_path(priv->sm.bw_ctr_fifo_name, th->instance_id, ch->bw_fifo);

    th->priv = priv;

    int ret = init_in_sm_buffer(&priv->sm);
    if (ret != 0) {
        log_error("SHM consumer init failed for channel %s", th->channel_name);
        free(priv);
        th->priv = NULL;
    }
    return ret;
}

static void shm_destroy(struct transport_handle* th)
{
    if (th->priv) {
        struct shm_priv* priv = (struct shm_priv*)th->priv;
        destory_sm_buffer(&priv->sm);
        free(priv);
        th->priv = NULL;
    }
}

static int shm_get_write_buf(struct transport_handle* th, void** buf_ptr)
{
    struct shm_priv* priv = (struct shm_priv*)th->priv;
    int idx = wait_buff_free(&priv->sm);
    if (idx == 0 || idx == 1) {
        *buf_ptr = priv->sm.shm_ptr[idx];
    } else if (idx == 3) {
        /* Frame dropped */
        th->dropped_frames++;
        *buf_ptr = NULL;
    } else {
        *buf_ptr = NULL;
    }
    return idx;
}

static int shm_submit_write(struct transport_handle* th, int buf_index)
{
    struct shm_priv* priv = (struct shm_priv*)th->priv;
    send_ctr_buff_ready(&priv->sm, buf_index);
    return 0;
}

static int shm_get_read_buf(struct transport_handle* th, void** buf_ptr)
{
    struct shm_priv* priv = (struct shm_priv*)th->priv;
    int idx = wait_buff_ready(&priv->sm);
    if (idx == 0 || idx == 1) {
        *buf_ptr = priv->sm.shm_ptr[idx];
    } else {
        *buf_ptr = NULL;
    }
    return idx;
}

static int shm_release_read(struct transport_handle* th, int buf_index)
{
    struct shm_priv* priv = (struct shm_priv*)th->priv;
    send_ctr_buff_free(&priv->sm, buf_index);
    return 0;
}

static void shm_send_terminate(struct transport_handle* th)
{
    struct shm_priv* priv = (struct shm_priv*)th->priv;
    send_ctr_terminate(&priv->sm);
}

/* Static ops table */
static const struct transport_ops shm_ops = {
    .init_producer = shm_init_producer,
    .init_consumer = shm_init_consumer,
    .destroy       = shm_destroy,
    .get_write_buf = shm_get_write_buf,
    .submit_write  = shm_submit_write,
    .get_read_buf  = shm_get_read_buf,
    .release_read  = shm_release_read,
    .send_terminate = shm_send_terminate,
};

const struct transport_ops* transport_shm_get_ops(void)
{
    return &shm_ops;
}
