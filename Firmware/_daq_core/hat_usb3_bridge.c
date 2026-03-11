/*
 *
 * Description :
 * USB3 bridge process for FPGA front-end devices.
 * Reads IQ data from a USB3-attached FPGA via libusb async bulk transfers
 * and outputs IQ frames (header + payload) to stdout, compatible with the
 * HeIMDALL DAQ pipeline (replaces rtl_daq.out in the chain).
 *
 * Follows the same patterns as rtl_daq.c:
 * - Async USB transfer callbacks with circular buffer
 * - IQ header (1024 bytes) construction for each frame
 * - ZMQ control thread for runtime commands
 * - Signal handling (SIGINT/SIGTERM)
 * - Configuration from daq_chain_config.ini
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

#include <pthread.h>
#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <errno.h>
#include <signal.h>
#include <sys/time.h>

#include <zmq.h>

#include "ini.h"
#include "log.h"
#include "iq_header.h"
#include "rtl_daq.h"

#ifdef USELIBUSB
#include <libusb-1.0/libusb.h>
#endif

/*
 *-------------------------------------
 *  Constants
 *-------------------------------------
 */
#define NUM_BUFF            8       /* Circular buffer depth */
#define ASYNC_BUF_NUMBER    12      /* Number of async USB transfers */
#define INI_FNAME           "daq_chain_config.ini"
#define NO_DUMMY_FRAMES     5
#define USB3_TIMEOUT_MS     5000
#define USB3_EVENT_TIMEOUT  1       /* Seconds */

/* Default USB device parameters */
#define USB3_DEFAULT_VID    0x1D50
#define USB3_DEFAULT_PID    0x6099
#define USB3_EP_IN          0x81
#define USB3_INTERFACE      0

/*
 *-------------------------------------
 *  Global state
 *-------------------------------------
 */

static pthread_mutex_t buff_mutex;
static pthread_cond_t  buff_cond;
static pthread_t ctrl_thread;

#ifdef USELIBUSB
static pthread_t event_thread;
static libusb_context *usb_ctx = NULL;
static libusb_device_handle *usb_dev = NULL;
static struct libusb_transfer *usb_transfers[ASYNC_BUF_NUMBER];
#endif

static int exit_flag = 0;
static int en_dummy_frame = 0;
static int dummy_frame_cntr = 0;
static int noise_source_state = 0;
static int last_noise_source_state = 0;
static int gain_change_flag = 0;
static int center_freq_change_flag = 0;
static uint32_t new_center_freq = 0;

static uint32_t ch_no = 0;
static uint32_t buffer_size = 0;
static int zmq_port = 1130;

/* Circular buffer */
static uint8_t **circ_buffers = NULL;
static volatile unsigned long long write_ind = 0;
static unsigned long long read_ind = 0;

/*
 *-------------------------------------
 *  Configuration
 *-------------------------------------
 */

typedef struct {
    int num_ch;
    int daq_buffer_size;
    int sample_rate;
    int center_freq;
    int gain;
    int en_noise_source_ctr;
    int log_level;
    const char *hw_name;
    int hw_unit_id;
    int ioo_type;
    int instance_id;
    int port_stride;
    uint16_t usb_vid;
    uint16_t usb_pid;
} configuration;

static int handler(void *conf_struct, const char *section, const char *name,
                   const char *value)
{
    configuration *pconfig = (configuration *)conf_struct;

    #define MATCH(s, n) strcmp(section, s) == 0 && strcmp(name, n) == 0
    if (MATCH("hw", "num_ch"))
        pconfig->num_ch = atoi(value);
    else if (MATCH("hw", "name"))
        pconfig->hw_name = strdup(value);
    else if (MATCH("hw", "unit_id"))
        pconfig->hw_unit_id = atoi(value);
    else if (MATCH("hw", "ioo_type"))
        pconfig->ioo_type = atoi(value);
    else if (MATCH("daq", "daq_buffer_size"))
        pconfig->daq_buffer_size = atoi(value);
    else if (MATCH("daq", "sample_rate"))
        pconfig->sample_rate = atoi(value);
    else if (MATCH("daq", "center_freq"))
        pconfig->center_freq = atoi(value);
    else if (MATCH("daq", "gain"))
        pconfig->gain = atoi(value);
    else if (MATCH("daq", "en_noise_source_ctr"))
        pconfig->en_noise_source_ctr = atoi(value);
    else if (MATCH("daq", "log_level"))
        pconfig->log_level = atoi(value);
    else if (MATCH("federation", "instance_id"))
        pconfig->instance_id = atoi(value);
    else if (MATCH("federation", "port_stride"))
        pconfig->port_stride = atoi(value);
    else if (MATCH("hw", "usb_vid")) {
        unsigned int v;
        if (sscanf(value, "%x", &v) == 1)
            pconfig->usb_vid = (uint16_t)v;
    }
    else if (MATCH("hw", "usb_pid")) {
        unsigned int p;
        if (sscanf(value, "%x", &p) == 1)
            pconfig->usb_pid = (uint16_t)p;
    }
    else
        return 0;
    return 0;
}

/*
 *-------------------------------------
 *  ZMQ control thread
 *  (Same pattern as rtl_daq.c fifo_read_tf)
 *-------------------------------------
 */

static void *ctrl_thread_func(void *arg)
{
    (void)arg;

    void *context   = zmq_ctx_new();
    void *responder = zmq_socket(context, ZMQ_REP);
    char zmq_addr[64];
    sprintf(zmq_addr, "tcp://*:%d", zmq_port);
    int rc = zmq_bind(responder, zmq_addr);
    if (rc != 0) {
        log_fatal("USB3 bridge: failed to open ZMQ socket on %s", zmq_addr);
        pthread_mutex_lock(&buff_mutex);
        exit_flag = 1;
        pthread_cond_signal(&buff_cond);
        pthread_mutex_unlock(&buff_mutex);
        return NULL;
    }

    log_info("USB3 bridge: ZMQ control listening on %s", zmq_addr);

    struct hdaq_im_msg_struct *msg = malloc(sizeof(struct hdaq_im_msg_struct));

    while (!exit_flag) {
        zmq_recv(responder, msg, 128, 0);
        log_info("USB3 bridge: command '%c' from module %d",
                 msg->command_identifier, msg->source_module_identifier);

        pthread_mutex_lock(&buff_mutex);

        if (msg->command_identifier == 'c') {
            uint32_t *params = (uint32_t *)msg->parameters;
            new_center_freq = params[0];
            center_freq_change_flag = 1;
            log_info("USB3 bridge: center freq change to %u MHz", new_center_freq / 1000000);
        }
        else if (msg->command_identifier == 'g') {
            gain_change_flag = 1;
            log_info("USB3 bridge: gain change request");
        }
        else if (msg->command_identifier == 'n') {
            noise_source_state = (msg->parameters[0] == 0) ? 0 : 1;
            log_info("USB3 bridge: noise source %s", noise_source_state ? "ON" : "OFF");
        }
        else if (msg->command_identifier == 'h') {
            log_info("USB3 bridge: halt request");
            exit_flag = 1;
        }

        en_dummy_frame = 1;
        dummy_frame_cntr = 0;
        zmq_send(responder, "ok", 2, 0);

        pthread_cond_signal(&buff_cond);
        pthread_mutex_unlock(&buff_mutex);
    }

    free(msg);
    zmq_close(responder);
    zmq_ctx_destroy(context);
    return NULL;
}

/*
 *-------------------------------------
 *  USB async transfer callback
 *  (Same pattern as rtlsdrCallback)
 *-------------------------------------
 */

#ifdef USELIBUSB
static void usb3_callback(struct libusb_transfer *transfer)
{
    if (exit_flag)
        return;

    if (transfer->status == LIBUSB_TRANSFER_COMPLETED) {
        int wr_idx = (int)(write_ind % NUM_BUFF);

        /* Copy data into circular buffer (all channels interleaved) */
        if ((size_t)transfer->actual_length <= buffer_size * ch_no) {
            memcpy(circ_buffers[wr_idx], transfer->buffer, transfer->actual_length);
        }

        log_debug("USB3 callback: write_ind=%llu, wr_idx=%d, len=%d",
                  write_ind, wr_idx, transfer->actual_length);
        write_ind++;

        pthread_cond_signal(&buff_cond);
    }
    else if (transfer->status == LIBUSB_TRANSFER_CANCELLED) {
        log_info("USB3 transfer cancelled");
        return;
    }
    else {
        log_error("USB3 transfer error: status=%d", transfer->status);
    }

    /* Resubmit */
    if (!exit_flag) {
        int ret = libusb_submit_transfer(transfer);
        if (ret != 0)
            log_error("USB3 resubmit failed: %s", libusb_error_name(ret));
    }
}

static void *event_thread_func(void *arg)
{
    (void)arg;
    struct timeval tv;

    log_info("USB3 event thread started");
    while (!exit_flag) {
        tv.tv_sec = USB3_EVENT_TIMEOUT;
        tv.tv_usec = 0;
        libusb_handle_events_timeout_completed(usb_ctx, &tv, NULL);
    }
    log_info("USB3 event thread exiting");
    return NULL;
}
#endif /* USELIBUSB */

/*
 *-------------------------------------
 *  Signal handler
 *-------------------------------------
 */

static void signal_handler(int signum)
{
    (void)signum;
    log_info("USB3 bridge: signal received, shutting down");
    exit_flag = 1;
    pthread_cond_broadcast(&buff_cond);
}

/*
 *-------------------------------------
 *  Main
 *-------------------------------------
 */

int main(int argc, char **argv)
{
    (void)argc; (void)argv;

    log_set_level(LOG_TRACE);

    configuration config;
    memset(&config, 0, sizeof(config));
    config.instance_id = 0;
    config.port_stride = 100;
    config.usb_vid = USB3_DEFAULT_VID;
    config.usb_pid = USB3_DEFAULT_PID;

    /* Load configuration */
    if (ini_parse(INI_FNAME, handler, &config) < 0) {
        log_fatal("USB3 bridge: configuration load failed");
        return -1;
    }

    ch_no = config.num_ch;
    buffer_size = config.daq_buffer_size * 2;  /* Bytes: I+Q per sample */
    zmq_port = compute_port(1130, config.instance_id, config.port_stride);
    log_set_level(config.log_level);

    log_info("USB3 bridge: ch=%d, buffer_size=%d, VID:PID=%04x:%04x",
             ch_no, buffer_size, config.usb_vid, config.usb_pid);

    /* Install signal handlers */
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    /* Allocate IQ header */
    struct iq_header_struct *iq_header = calloc(1, sizeof(struct iq_header_struct));
    if (!iq_header) {
        log_fatal("USB3 bridge: IQ header allocation failed");
        return -1;
    }

    /* Fill static IQ header fields */
    iq_header->sync_word = SYNC_WORD;
    iq_header->header_version = 7;
    if (config.hw_name)
        strncpy(iq_header->hardware_id, config.hw_name, sizeof(iq_header->hardware_id) - 1);
    iq_header->unit_id = config.hw_unit_id;
    iq_header->active_ant_chs = ch_no;
    iq_header->ioo_type = config.ioo_type;
    iq_header->rf_center_freq = (uint64_t)config.center_freq;
    iq_header->adc_sampling_freq = (uint64_t)config.sample_rate;
    iq_header->sampling_freq = (uint64_t)config.sample_rate;
    iq_header->cpi_length = (uint32_t)config.daq_buffer_size;
    iq_header->data_type = 2;       /* IQ data */
    iq_header->sample_bit_depth = 8;
    iq_header->frame_type = FRAME_TYPE_DATA;
    for (uint32_t m = 0; m < ch_no; m++)
        iq_header->if_gains[m] = (uint32_t)config.gain;

    /* Allocate circular buffers */
    size_t frame_size = buffer_size * ch_no;
    circ_buffers = malloc(NUM_BUFF * sizeof(uint8_t *));
    for (int i = 0; i < NUM_BUFF; i++) {
        circ_buffers[i] = malloc(frame_size);
        if (!circ_buffers[i]) {
            log_fatal("USB3 bridge: circular buffer allocation failed");
            return -1;
        }
        memset(circ_buffers[i], 0, frame_size);
    }

    pthread_mutex_init(&buff_mutex, NULL);
    pthread_cond_init(&buff_cond, NULL);

    /* Spawn ZMQ control thread */
    pthread_create(&ctrl_thread, NULL, ctrl_thread_func, NULL);

#ifdef USELIBUSB
    /* Initialize libusb */
    int ret = libusb_init(&usb_ctx);
    if (ret != 0) {
        log_fatal("USB3 bridge: libusb init failed: %s", libusb_error_name(ret));
        return -1;
    }

    /* Open USB device */
    usb_dev = libusb_open_device_with_vid_pid(usb_ctx, config.usb_vid, config.usb_pid);
    if (!usb_dev) {
        log_fatal("USB3 bridge: device %04x:%04x not found", config.usb_vid, config.usb_pid);
        libusb_exit(usb_ctx);
        return -1;
    }

    /* Detach kernel driver if needed */
    if (libusb_kernel_driver_active(usb_dev, USB3_INTERFACE) == 1) {
        libusb_detach_kernel_driver(usb_dev, USB3_INTERFACE);
    }

    ret = libusb_claim_interface(usb_dev, USB3_INTERFACE);
    if (ret != 0) {
        log_fatal("USB3 bridge: claim interface failed: %s", libusb_error_name(ret));
        libusb_close(usb_dev);
        libusb_exit(usb_ctx);
        return -1;
    }

    log_info("USB3 bridge: device opened, interface claimed");

    /* Start event thread */
    pthread_create(&event_thread, NULL, event_thread_func, NULL);

    /* Allocate and submit async transfers */
    for (int i = 0; i < ASYNC_BUF_NUMBER; i++) {
        usb_transfers[i] = libusb_alloc_transfer(0);
        void *buf = malloc(frame_size);
        if (!usb_transfers[i] || !buf) {
            log_fatal("USB3 bridge: transfer allocation failed");
            return -1;
        }

        libusb_fill_bulk_transfer(
            usb_transfers[i], usb_dev, USB3_EP_IN,
            buf, frame_size,
            usb3_callback, NULL, USB3_TIMEOUT_MS);

        ret = libusb_submit_transfer(usb_transfers[i]);
        if (ret != 0) {
            log_error("USB3 bridge: submit transfer %d failed: %s",
                      i, libusb_error_name(ret));
        }
    }

    log_info("USB3 bridge: %d async transfers submitted", ASYNC_BUF_NUMBER);
#else
    log_fatal("USB3 bridge: libusb not available (USELIBUSB not defined)");
    exit_flag = 1;
#endif

    /* Main data acquisition loop (same pattern as rtl_daq.c) */
    struct timeval frame_time_stamp;

    while (!exit_flag) {
        pthread_mutex_lock(&buff_mutex);
        while (write_ind <= read_ind && !exit_flag)
            pthread_cond_wait(&buff_cond, &buff_mutex);
        pthread_mutex_unlock(&buff_mutex);

        if (exit_flag)
            break;

        /* Check for circular buffer overrun */
        if ((write_ind - read_ind) >= NUM_BUFF) {
            log_warn("USB3 bridge: circular buffer overrun (write=%llu, read=%llu)",
                     write_ind, read_ind);
        }

        int rd_idx = (int)(read_ind % NUM_BUFF);

        /* Complete IQ header */
        gettimeofday(&frame_time_stamp, NULL);
        uint64_t ts_ms = (uint64_t)(frame_time_stamp.tv_sec) * 1000 +
                         (uint64_t)(frame_time_stamp.tv_usec) / 1000;
        iq_header->time_stamp = ts_ms;
        iq_header->daq_block_index = (uint32_t)read_ind;
        iq_header->noise_source_state = (uint32_t)noise_source_state;

        if (en_dummy_frame) {
            iq_header->frame_type = FRAME_TYPE_DUMMY;
            iq_header->data_type = 0;
            iq_header->cpi_length = 0;
        } else {
            iq_header->cpi_length = (uint32_t)config.daq_buffer_size;
            iq_header->data_type = 1;
            if (noise_source_state == 1)
                iq_header->frame_type = FRAME_TYPE_CAL;
            else
                iq_header->frame_type = FRAME_TYPE_DATA;
        }

        /* Write IQ header to stdout */
        fwrite(iq_header, sizeof(struct iq_header_struct), 1, stdout);

        /* Write IQ data to stdout */
        if (!en_dummy_frame) {
            fwrite(circ_buffers[rd_idx], 1, frame_size, stdout);
        }

        fflush(stdout);
        read_ind++;

        if (en_dummy_frame) {
            dummy_frame_cntr++;
            if (dummy_frame_cntr >= NO_DUMMY_FRAMES)
                en_dummy_frame = 0;
        }

        /* Handle center frequency change */
        if (center_freq_change_flag) {
            iq_header->rf_center_freq = (uint64_t)new_center_freq;
            center_freq_change_flag = 0;
            log_info("USB3 bridge: center freq updated to %u Hz", new_center_freq);
        }

        last_noise_source_state = noise_source_state;

        log_debug("USB3 bridge: frame %llu written (type=%d)",
                  read_ind - 1, iq_header->frame_type);
    }

    /* Cleanup */
    log_info("USB3 bridge: shutting down");

#ifdef USELIBUSB
    for (int i = 0; i < ASYNC_BUF_NUMBER; i++) {
        if (usb_transfers[i]) {
            libusb_cancel_transfer(usb_transfers[i]);
        }
    }

    pthread_join(event_thread, NULL);

    for (int i = 0; i < ASYNC_BUF_NUMBER; i++) {
        if (usb_transfers[i]) {
            free(usb_transfers[i]->buffer);
            libusb_free_transfer(usb_transfers[i]);
        }
    }

    if (usb_dev) {
        libusb_release_interface(usb_dev, USB3_INTERFACE);
        libusb_close(usb_dev);
    }
    if (usb_ctx)
        libusb_exit(usb_ctx);
#endif

    pthread_join(ctrl_thread, NULL);

    for (int i = 0; i < NUM_BUFF; i++)
        free(circ_buffers[i]);
    free(circ_buffers);
    free(iq_header);

    pthread_mutex_destroy(&buff_mutex);
    pthread_cond_destroy(&buff_cond);

    log_info("USB3 bridge: all resources freed");
    return 0;
}
