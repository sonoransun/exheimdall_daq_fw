/*
 *
 * Description :
 * USB 3.0 transport driver using libusb-1.0 async bulk transfers.
 * Implements the transport_ops interface for high-speed USB data acquisition
 * from FPGA front-end devices.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Author  : HeIMDALL DAQ Contributors
 *
 * Copyright (C) 2018-2024  HeIMDALL DAQ Contributors
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
 *
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <errno.h>
#include <pthread.h>

#ifdef USELIBUSB
#include <libusb-1.0/libusb.h>
#endif

#include "log.h"
#include "transport.h"

/*
 *-------------------------------------
 *  Constants
 *-------------------------------------
 */
#define USB3_DEFAULT_VID     0x1D50   /* OpenMoko -- placeholder VID */
#define USB3_DEFAULT_PID     0x6099   /* Placeholder PID */
#define USB3_DEFAULT_EP_IN   0x81     /* Bulk IN endpoint */
#define USB3_DEFAULT_EP_OUT  0x01     /* Bulk OUT endpoint */
#define USB3_INTERFACE       0
#define USB3_TIMEOUT_MS      5000
#define USB3_NUM_TRANSFERS   32
#define USB3_EVENT_TIMEOUT_S 1

/*
 *-------------------------------------
 *  Private state
 *-------------------------------------
 */

struct usb3_transport_priv {
#ifdef USELIBUSB
    libusb_context *usb_ctx;
    libusb_device_handle *dev_handle;
#else
    void *usb_ctx;
    void *dev_handle;
#endif
    uint16_t vid, pid;
    uint8_t ep_in, ep_out;

    /* Async transfer ring */
#ifdef USELIBUSB
    struct libusb_transfer *transfers[USB3_NUM_TRANSFERS];
#else
    void *transfers[USB3_NUM_TRANSFERS];
#endif
    void *transfer_bufs[USB3_NUM_TRANSFERS];
    size_t transfer_size;
    int num_transfers;

    /* Ring buffer management */
    volatile int head;   /* Next buffer to be filled by callback */
    volatile int tail;   /* Next buffer to be read by consumer */
    pthread_mutex_t mutex;
    pthread_cond_t data_ready;
    pthread_t event_thread;
    bool event_thread_running;
    bool terminated;

    /* Write side (producer) */
    void *tx_buf[2];
    int active_tx;
};

/*
 *-------------------------------------
 *  Event handling thread
 *-------------------------------------
 */

#ifdef USELIBUSB
static void *usb3_event_thread_func(void *arg)
{
    struct usb3_transport_priv *priv = (struct usb3_transport_priv *)arg;
    struct timeval tv;

    log_info("USB3 event thread started");

    while (!priv->terminated) {
        tv.tv_sec = USB3_EVENT_TIMEOUT_S;
        tv.tv_usec = 0;
        int ret = libusb_handle_events_timeout_completed(
            priv->usb_ctx, &tv, NULL);
        if (ret != 0 && ret != LIBUSB_ERROR_TIMEOUT && !priv->terminated) {
            log_error("USB3 event handling error: %s", libusb_error_name(ret));
        }
    }

    log_info("USB3 event thread exiting");
    return NULL;
}

/*
 *-------------------------------------
 *  Async bulk IN callback
 *  (follows rtlsdrCallback pattern from rtl_daq.c)
 *-------------------------------------
 */

static void usb3_bulk_in_callback(struct libusb_transfer *transfer)
{
    struct usb3_transport_priv *priv = (struct usb3_transport_priv *)transfer->user_data;

    if (priv->terminated) {
        return;
    }

    if (transfer->status == LIBUSB_TRANSFER_COMPLETED) {
        pthread_mutex_lock(&priv->mutex);

        int next_head = (priv->head + 1) % priv->num_transfers;
        if (next_head == priv->tail) {
            /* Ring full -- drop oldest */
            log_warn("USB3 ring buffer overrun, dropping oldest frame");
            priv->tail = (priv->tail + 1) % priv->num_transfers;
        }
        priv->head = next_head;

        pthread_cond_signal(&priv->data_ready);
        pthread_mutex_unlock(&priv->mutex);

        /* Resubmit transfer for continuous streaming */
        if (!priv->terminated) {
            int ret = libusb_submit_transfer(transfer);
            if (ret != 0) {
                log_error("USB3 resubmit failed: %s", libusb_error_name(ret));
            }
        }
    } else if (transfer->status == LIBUSB_TRANSFER_CANCELLED) {
        log_info("USB3 transfer cancelled");
    } else {
        log_error("USB3 transfer error: status=%d", transfer->status);
        /* Try to resubmit on transient errors */
        if (!priv->terminated) {
            int ret = libusb_submit_transfer(transfer);
            if (ret != 0) {
                log_error("USB3 error resubmit failed: %s", libusb_error_name(ret));
            }
        }
    }
}
#endif /* USELIBUSB */

/*
 *-------------------------------------
 *  VID:PID parsing from channel_name
 *  Format: "VID:PID" e.g. "1d50:6099"
 *-------------------------------------
 */

static void parse_vid_pid(const char *name, uint16_t *vid, uint16_t *pid)
{
    *vid = USB3_DEFAULT_VID;
    *pid = USB3_DEFAULT_PID;

    if (name && name[0] != '\0') {
        unsigned int v, p;
        if (sscanf(name, "%x:%x", &v, &p) == 2) {
            *vid = (uint16_t)v;
            *pid = (uint16_t)p;
        }
    }
}

/*
 *-------------------------------------
 *  transport_ops implementation
 *-------------------------------------
 */

static int usb3_init_common(struct transport_handle *th)
{
    struct usb3_transport_priv *priv = calloc(1, sizeof(struct usb3_transport_priv));
    if (!priv) {
        log_fatal("USB3 transport: allocation failed");
        return -1;
    }

    priv->terminated = false;
    priv->head = 0;
    priv->tail = 0;
    priv->active_tx = 0;
    priv->event_thread_running = false;
    priv->transfer_size = th->buffer_size;
    priv->num_transfers = (th->num_buffers > 0 && th->num_buffers <= USB3_NUM_TRANSFERS)
                          ? th->num_buffers : USB3_NUM_TRANSFERS;
    priv->ep_in = USB3_DEFAULT_EP_IN;
    priv->ep_out = USB3_DEFAULT_EP_OUT;

    parse_vid_pid(th->channel_name, &priv->vid, &priv->pid);

    pthread_mutex_init(&priv->mutex, NULL);
    pthread_cond_init(&priv->data_ready, NULL);

    th->priv = priv;

#ifdef USELIBUSB
    int ret = libusb_init(&priv->usb_ctx);
    if (ret != 0) {
        log_error("USB3 libusb_init failed: %s", libusb_error_name(ret));
        free(priv);
        th->priv = NULL;
        return -1;
    }

    /* Find and open device by VID:PID */
    priv->dev_handle = libusb_open_device_with_vid_pid(
        priv->usb_ctx, priv->vid, priv->pid);
    if (!priv->dev_handle) {
        log_error("USB3 device %04x:%04x not found", priv->vid, priv->pid);
        libusb_exit(priv->usb_ctx);
        free(priv);
        th->priv = NULL;
        return -1;
    }

    /* Detach kernel driver if attached */
    if (libusb_kernel_driver_active(priv->dev_handle, USB3_INTERFACE) == 1) {
        ret = libusb_detach_kernel_driver(priv->dev_handle, USB3_INTERFACE);
        if (ret != 0) {
            log_warn("USB3 kernel driver detach failed: %s", libusb_error_name(ret));
        }
    }

    /* Claim interface */
    ret = libusb_claim_interface(priv->dev_handle, USB3_INTERFACE);
    if (ret != 0) {
        log_error("USB3 claim interface failed: %s", libusb_error_name(ret));
        libusb_close(priv->dev_handle);
        libusb_exit(priv->usb_ctx);
        free(priv);
        th->priv = NULL;
        return -1;
    }

    log_info("USB3 device %04x:%04x opened, interface %d claimed",
             priv->vid, priv->pid, USB3_INTERFACE);

    /* Allocate transfer buffers */
    for (int i = 0; i < priv->num_transfers; i++) {
        if (posix_memalign(&priv->transfer_bufs[i], 4096, priv->transfer_size) != 0) {
            log_fatal("USB3 transfer buffer allocation failed");
            return -1;
        }
        memset(priv->transfer_bufs[i], 0, priv->transfer_size);

        priv->transfers[i] = libusb_alloc_transfer(0);
        if (!priv->transfers[i]) {
            log_fatal("USB3 libusb_alloc_transfer failed");
            return -1;
        }
    }

    /* Start event thread */
    ret = pthread_create(&priv->event_thread, NULL, usb3_event_thread_func, priv);
    if (ret != 0) {
        log_error("USB3 event thread creation failed: %s", strerror(ret));
        return -1;
    }
    priv->event_thread_running = true;

#else
    log_warn("USB3 transport: libusb not available (USELIBUSB not defined)");

    /* Allocate transfer buffers anyway for non-libusb builds */
    for (int i = 0; i < priv->num_transfers; i++) {
        if (posix_memalign(&priv->transfer_bufs[i], 4096, priv->transfer_size) != 0) {
            log_fatal("USB3 transfer buffer allocation failed");
            return -1;
        }
        memset(priv->transfer_bufs[i], 0, priv->transfer_size);
    }
#endif

    /* Allocate TX double buffers */
    for (int i = 0; i < 2; i++) {
        if (posix_memalign(&priv->tx_buf[i], 4096, priv->transfer_size) != 0) {
            log_fatal("USB3 TX buffer allocation failed");
            return -1;
        }
        memset(priv->tx_buf[i], 0, priv->transfer_size);
    }

    return 0;
}

int usb3_init_producer(struct transport_handle *th)
{
    log_info("USB3 transport: initializing producer");
    int ret = usb3_init_common(th);
    if (ret != 0) return ret;

    /* Producer does not submit IN transfers */
    return 0;
}

int usb3_init_consumer(struct transport_handle *th)
{
    log_info("USB3 transport: initializing consumer");
    int ret = usb3_init_common(th);
    if (ret != 0) return ret;

#ifdef USELIBUSB
    struct usb3_transport_priv *priv = (struct usb3_transport_priv *)th->priv;

    /* Submit initial async bulk IN transfers */
    for (int i = 0; i < priv->num_transfers; i++) {
        libusb_fill_bulk_transfer(
            priv->transfers[i],
            priv->dev_handle,
            priv->ep_in,
            priv->transfer_bufs[i],
            priv->transfer_size,
            usb3_bulk_in_callback,
            priv,
            USB3_TIMEOUT_MS);

        ret = libusb_submit_transfer(priv->transfers[i]);
        if (ret != 0) {
            log_error("USB3 initial submit failed for transfer %d: %s",
                      i, libusb_error_name(ret));
            return -1;
        }
    }
    log_info("USB3 consumer: %d async transfers submitted", priv->num_transfers);
#endif

    return 0;
}

void usb3_destroy(struct transport_handle *th)
{
    if (!th || !th->priv)
        return;

    struct usb3_transport_priv *priv = (struct usb3_transport_priv *)th->priv;
    priv->terminated = true;

    /* Wake up any waiting consumers */
    pthread_mutex_lock(&priv->mutex);
    pthread_cond_broadcast(&priv->data_ready);
    pthread_mutex_unlock(&priv->mutex);

#ifdef USELIBUSB
    /* Cancel all pending transfers */
    for (int i = 0; i < priv->num_transfers; i++) {
        if (priv->transfers[i])
            libusb_cancel_transfer(priv->transfers[i]);
    }

    /* Wait for event thread */
    if (priv->event_thread_running)
        pthread_join(priv->event_thread, NULL);

    /* Free transfers */
    for (int i = 0; i < priv->num_transfers; i++) {
        if (priv->transfers[i])
            libusb_free_transfer(priv->transfers[i]);
    }

    /* Release interface and close device */
    if (priv->dev_handle) {
        libusb_release_interface(priv->dev_handle, USB3_INTERFACE);
        libusb_close(priv->dev_handle);
    }
    if (priv->usb_ctx)
        libusb_exit(priv->usb_ctx);
#endif

    /* Free buffers */
    for (int i = 0; i < priv->num_transfers; i++)
        free(priv->transfer_bufs[i]);
    for (int i = 0; i < 2; i++)
        free(priv->tx_buf[i]);

    pthread_mutex_destroy(&priv->mutex);
    pthread_cond_destroy(&priv->data_ready);

    free(priv);
    th->priv = NULL;
    log_info("USB3 transport: destroyed");
}

int usb3_get_write_buf(struct transport_handle *th, void **buf_ptr)
{
    struct usb3_transport_priv *priv = (struct usb3_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

    int idx = priv->active_tx ^ 1;
    *buf_ptr = priv->tx_buf[idx];
    return idx;
}

int usb3_submit_write(struct transport_handle *th, int buf_index)
{
    struct usb3_transport_priv *priv = (struct usb3_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

#ifdef USELIBUSB
    int transferred = 0;
    int ret = libusb_bulk_transfer(
        priv->dev_handle,
        priv->ep_out,
        priv->tx_buf[buf_index],
        (int)priv->transfer_size,
        &transferred,
        USB3_TIMEOUT_MS);
    if (ret != 0) {
        log_error("USB3 bulk OUT transfer failed: %s", libusb_error_name(ret));
        return -1;
    }
    if ((size_t)transferred != priv->transfer_size) {
        log_warn("USB3 short write: %d of %zu bytes", transferred, priv->transfer_size);
    }
#else
    (void)buf_index;
    log_warn("USB3 submit_write: no-op (libusb not available)");
#endif

    priv->active_tx = buf_index;
    th->total_bytes += priv->transfer_size;
    th->total_frames++;
    return 0;
}

int usb3_get_read_buf(struct transport_handle *th, void **buf_ptr)
{
    struct usb3_transport_priv *priv = (struct usb3_transport_priv *)th->priv;

    pthread_mutex_lock(&priv->mutex);

    /* Wait for data to be available in the ring */
    while (priv->head == priv->tail && !priv->terminated) {
        pthread_cond_wait(&priv->data_ready, &priv->mutex);
    }

    if (priv->terminated) {
        pthread_mutex_unlock(&priv->mutex);
        return -1;
    }

    int idx = priv->tail;
    *buf_ptr = priv->transfer_bufs[idx];
    pthread_mutex_unlock(&priv->mutex);

    return idx;
}

int usb3_release_read(struct transport_handle *th, int buf_index)
{
    struct usb3_transport_priv *priv = (struct usb3_transport_priv *)th->priv;
    (void)buf_index;

    pthread_mutex_lock(&priv->mutex);
    priv->tail = (priv->tail + 1) % priv->num_transfers;
    pthread_mutex_unlock(&priv->mutex);

    return 0;
}

void usb3_send_terminate(struct transport_handle *th)
{
    struct usb3_transport_priv *priv = (struct usb3_transport_priv *)th->priv;
    priv->terminated = true;

    pthread_mutex_lock(&priv->mutex);
    pthread_cond_broadcast(&priv->data_ready);
    pthread_mutex_unlock(&priv->mutex);

    log_info("USB3 transport: terminate signaled");
}

/*
 *-------------------------------------
 *  Exported ops table
 *-------------------------------------
 */

static const struct transport_ops usb3_transport_ops = {
    .init_producer  = usb3_init_producer,
    .init_consumer  = usb3_init_consumer,
    .destroy        = usb3_destroy,
    .get_write_buf  = usb3_get_write_buf,
    .submit_write   = usb3_submit_write,
    .get_read_buf   = usb3_get_read_buf,
    .release_read   = usb3_release_read,
    .send_terminate = usb3_send_terminate,
};

const struct transport_ops* transport_usb3_get_ops(void)
{
    return &usb3_transport_ops;
}
