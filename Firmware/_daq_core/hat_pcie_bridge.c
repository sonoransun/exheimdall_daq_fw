/*
 *
 * Description :
 * PCIe bridge process for FPGA-attached data acquisition cards.
 * Opens a PCIe device (UIO or XDMA), maps BAR for register access,
 * uses DMA for bulk data transfer, and outputs IQ frames to stdout
 * or shared memory. Compatible with the HeIMDALL DAQ pipeline.
 *
 * Can optionally collapse FIR + decimation + correlation into a
 * single FPGA pass when the attached FPGA bitstream supports it.
 *
 * Parses the [pcie] section of daq_chain_config.ini for:
 *   device     - PCI BDF address (e.g. 0000:01:00.0)
 *   bar_index  - BAR number to mmap for register access
 *   driver     - kernel driver name (xdma | uio)
 *
 * Uses XDMA character devices (/dev/xdma0_h2c_0, /dev/xdma0_c2h_0)
 * for DMA transfers between host and FPGA card.
 *
 * Follows the same patterns as hat_usb3_bridge.c:
 * - DMA read with double buffering
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
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/time.h>

#include <zmq.h>

#include "transport.h"
#include "log.h"
#include "ini.h"
#include "iq_header.h"
#include "rtl_daq.h"
#include "sh_mem_util.h"

/*
 *-------------------------------------
 *  Constants
 *-------------------------------------
 */
#define INI_FNAME           "daq_chain_config.ini"
#define NO_DUMMY_FRAMES     5
#define NUM_DMA_BUFS        2       /* Double buffered DMA */

/* Default PCIe device paths (XDMA driver) */
#define PCIE_XDMA_H2C_FMT  "/dev/xdma0_h2c_%d"
#define PCIE_XDMA_C2H_FMT  "/dev/xdma0_c2h_%d"
#define PCIE_UIO_DEV_FMT    "/dev/uio%d"
#define PCIE_BAR_SIZE       (64 * 1024)

/* FPGA register offsets (BAR0) */
#define REG_STATUS          0x0000
#define REG_CONTROL         0x0004
#define REG_VERSION         0x0008
#define REG_NUM_CHANNELS    0x000C
#define REG_SAMPLE_RATE     0x0010
#define REG_CENTER_FREQ_LO  0x0014
#define REG_CENTER_FREQ_HI  0x0018
#define REG_BLOCK_SIZE      0x001C
#define REG_DEC_RATIO       0x0020
#define REG_GAIN            0x0024
#define REG_NOISE_SRC       0x0028
#define REG_DOORBELL        0x002C

/* Control register bits */
#define CTRL_ENABLE         (1 << 0)
#define CTRL_RESET          (1 << 1)
#define CTRL_DMA_START      (1 << 2)
#define CTRL_FPGA_DECIMATE  (1 << 3)  /* Enable FPGA-side FIR+decimation */

/* Status register bits */
#define STATUS_READY        (1 << 0)
#define STATUS_DATA_AVAIL   (1 << 1)
#define STATUS_DMA_DONE     (1 << 2)
#define STATUS_ERROR        (1 << 31)

#define POLL_INTERVAL_US    100
#define POLL_TIMEOUT_MS     5000

/* XDMA DMA channel index used for transfers */
#define XDMA_CHANNEL        0

/*
 *-------------------------------------
 *  Global state
 *-------------------------------------
 */

static int exit_flag = 0;
static int en_dummy_frame = 0;
static int dummy_frame_cntr = 0;
static int noise_source_state = 0;
static int gain_change_flag = 0;
static int center_freq_change_flag = 0;
static uint32_t new_center_freq = 0;

static pthread_mutex_t ctrl_mutex;
static pthread_cond_t  ctrl_cond;
static pthread_t ctrl_thread;

static int zmq_port = 1130;
static uint32_t ch_no = 0;
static uint32_t buffer_size = 0;

/* PCIe resources */
static int bar_fd = -1;
static volatile uint32_t *bar_ptr = NULL;
static int c2h_fd = -1;
static int h2c_fd = -1;
static bool has_xdma = false;
static bool has_bar = false;

/* DMA buffers */
static void *dma_bufs[NUM_DMA_BUFS];
static int active_dma_buf = 0;

/*
 *-------------------------------------
 *  Configuration
 *-------------------------------------
 */

typedef struct {
    /* [hw] section */
    int num_ch;
    const char *hw_name;
    int hw_unit_id;
    int ioo_type;

    /* [daq] section */
    int daq_buffer_size;
    int sample_rate;
    int center_freq;
    int gain;
    int en_noise_source_ctr;
    int log_level;

    /* [pre_processing] section */
    int decimation_ratio;

    /* [federation] section */
    int instance_id;
    int port_stride;

    /* [pcie] section */
    int pcie_enable;
    char pcie_device[64];       /* PCI BDF, e.g. "0000:01:00.0" */
    int pcie_bar_index;         /* BAR number for register access */
    char pcie_driver[32];       /* "xdma" or "uio" */

    /* Derived */
    int en_fpga_decimate;       /* 1 = FPGA handles FIR+decimation */
} configuration;

static int handler(void *conf_struct, const char *section, const char *name,
                   const char *value)
{
    configuration *pconfig = (configuration *)conf_struct;

    #define MATCH(s, n) strcmp(section, s) == 0 && strcmp(name, n) == 0

    /* [hw] */
    if (MATCH("hw", "num_ch"))
        pconfig->num_ch = atoi(value);
    else if (MATCH("hw", "name"))
        pconfig->hw_name = strdup(value);
    else if (MATCH("hw", "unit_id"))
        pconfig->hw_unit_id = atoi(value);
    else if (MATCH("hw", "ioo_type"))
        pconfig->ioo_type = atoi(value);

    /* [daq] */
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

    /* [pre_processing] */
    else if (MATCH("pre_processing", "decimation_ratio"))
        pconfig->decimation_ratio = atoi(value);

    /* [federation] */
    else if (MATCH("federation", "instance_id"))
        pconfig->instance_id = atoi(value);
    else if (MATCH("federation", "port_stride"))
        pconfig->port_stride = atoi(value);

    /* [pcie] */
    else if (MATCH("pcie", "enable"))
        pconfig->pcie_enable = atoi(value);
    else if (MATCH("pcie", "device"))
        strncpy(pconfig->pcie_device, value, sizeof(pconfig->pcie_device) - 1);
    else if (MATCH("pcie", "bar_index"))
        pconfig->pcie_bar_index = atoi(value);
    else if (MATCH("pcie", "driver"))
        strncpy(pconfig->pcie_driver, value, sizeof(pconfig->pcie_driver) - 1);

    else
        return 0;
    return 0;
}

/*
 *-------------------------------------
 *  BAR register access
 *-------------------------------------
 */

static inline void bar_write(uint32_t offset, uint32_t val)
{
    if (bar_ptr)
        bar_ptr[offset / 4] = val;
}

static inline uint32_t bar_read(uint32_t offset)
{
    if (bar_ptr)
        return bar_ptr[offset / 4];
    return 0;
}

/*
 *-------------------------------------
 *  DMA read helper
 *  Reads exactly 'size' bytes from an XDMA C2H channel,
 *  handling partial reads and EINTR.
 *-------------------------------------
 */

static ssize_t dma_read_full(int fd, void *buf, size_t size)
{
    ssize_t total = 0;
    uint8_t *ptr = (uint8_t *)buf;

    while ((size_t)total < size) {
        ssize_t ret = read(fd, ptr + total, size - total);
        if (ret < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (ret == 0) return total;
        total += ret;
    }
    return total;
}

/*
 *-------------------------------------
 *  DMA write helper
 *  Writes exactly 'size' bytes to an XDMA H2C channel.
 *-------------------------------------
 */

static ssize_t dma_write_full(int fd, const void *buf, size_t size)
{
    ssize_t total = 0;
    const uint8_t *ptr = (const uint8_t *)buf;

    while ((size_t)total < size) {
        ssize_t ret = write(fd, ptr + total, size - total);
        if (ret < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (ret == 0) return total;
        total += ret;
    }
    return total;
}

/*
 *-------------------------------------
 *  Wait for FPGA data ready
 *  Polls the STATUS register for DATA_AVAIL bit.
 *-------------------------------------
 */

static int wait_data_ready(void)
{
    int elapsed_us = 0;

    while (!(bar_read(REG_STATUS) & STATUS_DATA_AVAIL)) {
        if (exit_flag)
            return -1;
        usleep(POLL_INTERVAL_US);
        elapsed_us += POLL_INTERVAL_US;
        if (elapsed_us >= POLL_TIMEOUT_MS * 1000) {
            log_warn("PCIe bridge: data ready timeout");
            return -1;
        }
    }
    return 0;
}

/*
 *-------------------------------------
 *  Open XDMA character devices
 *  Constructs device paths from the driver name and channel
 *  index, then opens both H2C and C2H file descriptors.
 *-------------------------------------
 */

static int open_xdma_channels(int channel)
{
    char c2h_path[64];
    char h2c_path[64];

    snprintf(c2h_path, sizeof(c2h_path), PCIE_XDMA_C2H_FMT, channel);
    snprintf(h2c_path, sizeof(h2c_path), PCIE_XDMA_H2C_FMT, channel);

    c2h_fd = open(c2h_path, O_RDWR);
    h2c_fd = open(h2c_path, O_RDWR);

    if (c2h_fd >= 0 && h2c_fd >= 0) {
        has_xdma = true;
        log_info("PCIe bridge: XDMA channels opened (%s, %s)", h2c_path, c2h_path);
        return 0;
    }

    log_error("PCIe bridge: failed to open XDMA channels (%s: %s, %s: %s)",
              h2c_path, (h2c_fd < 0) ? strerror(errno) : "ok",
              c2h_path, (c2h_fd < 0) ? strerror(errno) : "ok");

    if (c2h_fd >= 0) { close(c2h_fd); c2h_fd = -1; }
    if (h2c_fd >= 0) { close(h2c_fd); h2c_fd = -1; }
    return -1;
}

/*
 *-------------------------------------
 *  Open UIO device for BAR register access
 *  Maps the specified BAR via mmap.
 *-------------------------------------
 */

static int open_bar(int bar_index)
{
    char uio_path[64];
    snprintf(uio_path, sizeof(uio_path), PCIE_UIO_DEV_FMT, bar_index);

    bar_fd = open(uio_path, O_RDWR | O_SYNC);
    if (bar_fd < 0) {
        log_error("PCIe bridge: cannot open %s: %s", uio_path, strerror(errno));
        return -1;
    }

    bar_ptr = (volatile uint32_t *)mmap(NULL, PCIE_BAR_SIZE,
                                         PROT_READ | PROT_WRITE,
                                         MAP_SHARED, bar_fd, 0);
    if (bar_ptr == MAP_FAILED) {
        log_error("PCIe bridge: BAR mmap failed: %s", strerror(errno));
        bar_ptr = NULL;
        close(bar_fd);
        bar_fd = -1;
        return -1;
    }

    has_bar = true;
    uint32_t ver = bar_read(REG_VERSION);
    log_info("PCIe bridge: BAR%d mapped via %s, FPGA version 0x%08X",
             bar_index, uio_path, ver);
    return 0;
}

/*
 *-------------------------------------
 *  ZMQ control thread
 *  (Same pattern as rtl_daq.c fifo_read_tf and hat_usb3_bridge.c)
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
        log_fatal("PCIe bridge: ZMQ bind failed on %s", zmq_addr);
        pthread_mutex_lock(&ctrl_mutex);
        exit_flag = 1;
        pthread_cond_signal(&ctrl_cond);
        pthread_mutex_unlock(&ctrl_mutex);
        return NULL;
    }

    log_info("PCIe bridge: ZMQ control on %s", zmq_addr);

    struct hdaq_im_msg_struct *msg = malloc(sizeof(struct hdaq_im_msg_struct));

    while (!exit_flag) {
        zmq_recv(responder, msg, 128, 0);
        log_info("PCIe bridge: command '%c' from module %d",
                 msg->command_identifier, msg->source_module_identifier);

        pthread_mutex_lock(&ctrl_mutex);

        if (msg->command_identifier == 'c') {
            uint32_t *params = (uint32_t *)msg->parameters;
            new_center_freq = params[0];
            center_freq_change_flag = 1;
            log_info("PCIe bridge: center freq change to %u MHz",
                     new_center_freq / 1000000);
        }
        else if (msg->command_identifier == 'g') {
            gain_change_flag = 1;
            log_info("PCIe bridge: gain change request");
        }
        else if (msg->command_identifier == 'n') {
            noise_source_state = (msg->parameters[0] == 0) ? 0 : 1;
            log_info("PCIe bridge: noise source %s",
                     noise_source_state ? "ON" : "OFF");
        }
        else if (msg->command_identifier == 'h') {
            log_info("PCIe bridge: halt request");
            exit_flag = 1;
        }

        en_dummy_frame = 1;
        dummy_frame_cntr = 0;
        zmq_send(responder, "ok", 2, 0);

        pthread_cond_signal(&ctrl_cond);
        pthread_mutex_unlock(&ctrl_mutex);
    }

    free(msg);
    zmq_close(responder);
    zmq_ctx_destroy(context);
    return NULL;
}

/*
 *-------------------------------------
 *  Signal handler
 *-------------------------------------
 */

static void signal_handler(int signum)
{
    (void)signum;
    log_info("PCIe bridge: signal received, shutting down");
    exit_flag = 1;
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
    config.en_fpga_decimate = 0;
    config.decimation_ratio = 1;
    config.pcie_enable = 0;
    strncpy(config.pcie_device, "0000:01:00.0", sizeof(config.pcie_device) - 1);
    config.pcie_bar_index = 0;
    strncpy(config.pcie_driver, "xdma", sizeof(config.pcie_driver) - 1);

    /* Load configuration */
    if (ini_parse(INI_FNAME, handler, &config) < 0) {
        log_fatal("PCIe bridge: configuration load failed");
        return -1;
    }

    ch_no = config.num_ch;
    buffer_size = config.daq_buffer_size * 2;  /* Bytes: I+Q per sample */
    zmq_port = compute_port(1130, config.instance_id, config.port_stride);
    log_set_level(config.log_level);

    log_info("PCIe bridge: ch=%d, buffer_size=%d, pcie_device=%s, bar=%d, driver=%s",
             ch_no, buffer_size, config.pcie_device, config.pcie_bar_index,
             config.pcie_driver);

    /* Install signal handlers */
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    /* Open BAR for register access via UIO */
    if (open_bar(config.pcie_bar_index) != 0) {
        log_warn("PCIe bridge: BAR access not available, continuing without register control");
    }

    /* Open XDMA DMA channels */
    if (strcmp(config.pcie_driver, "xdma") == 0) {
        if (open_xdma_channels(XDMA_CHANNEL) != 0) {
            log_warn("PCIe bridge: XDMA channels not available");
        }
    } else {
        log_info("PCIe bridge: driver '%s' selected, skipping XDMA channel open",
                 config.pcie_driver);
    }

    if (!has_bar && !has_xdma) {
        log_fatal("PCIe bridge: no BAR or XDMA access, cannot proceed");
        return -1;
    }

    /* Allocate IQ header */
    struct iq_header_struct *iq_header = calloc(1, sizeof(struct iq_header_struct));
    if (!iq_header) {
        log_fatal("PCIe bridge: IQ header allocation failed");
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

    /* If FPGA does decimation, update header accordingly */
    if (config.en_fpga_decimate && config.decimation_ratio > 1) {
        iq_header->sampling_freq = (uint64_t)config.sample_rate / config.decimation_ratio;
        iq_header->cpi_length = (uint32_t)config.daq_buffer_size / config.decimation_ratio;
        iq_header->sample_bit_depth = 32;  /* FPGA outputs F32 */
        iq_header->data_type = 3;          /* Decimated IQ */
    }

    /* Allocate DMA buffers (page-aligned for XDMA) */
    size_t frame_size;
    if (config.en_fpga_decimate && config.decimation_ratio > 1) {
        /* FPGA outputs F32 after decimation */
        frame_size = (config.daq_buffer_size / config.decimation_ratio) *
                     ch_no * 2 * sizeof(float);
    } else {
        frame_size = buffer_size * ch_no;
    }

    for (int i = 0; i < NUM_DMA_BUFS; i++) {
        if (posix_memalign(&dma_bufs[i], 4096, frame_size) != 0) {
            log_fatal("PCIe bridge: DMA buffer allocation failed");
            return -1;
        }
        memset(dma_bufs[i], 0, frame_size);
    }

    /* Configure FPGA via BAR registers */
    if (has_bar) {
        bar_write(REG_CONTROL, CTRL_RESET);
        usleep(10000);
        bar_write(REG_NUM_CHANNELS, ch_no);
        bar_write(REG_SAMPLE_RATE, (uint32_t)config.sample_rate);
        bar_write(REG_CENTER_FREQ_LO, (uint32_t)(config.center_freq & 0xFFFFFFFF));
        bar_write(REG_CENTER_FREQ_HI, 0);
        bar_write(REG_BLOCK_SIZE, (uint32_t)config.daq_buffer_size);
        bar_write(REG_GAIN, (uint32_t)config.gain);

        uint32_t ctrl = CTRL_ENABLE;
        if (config.en_fpga_decimate) {
            bar_write(REG_DEC_RATIO, (uint32_t)config.decimation_ratio);
            ctrl |= CTRL_FPGA_DECIMATE;
        }
        bar_write(REG_CONTROL, ctrl);

        log_info("PCIe bridge: FPGA configured and enabled");
    }

    pthread_mutex_init(&ctrl_mutex, NULL);
    pthread_cond_init(&ctrl_cond, NULL);

    /* Spawn ZMQ control thread */
    pthread_create(&ctrl_thread, NULL, ctrl_thread_func, NULL);

    /*
     * Main data acquisition loop
     * Reads IQ frames from the FPGA via DMA, prepends an IQ header,
     * and writes the complete frame (header + payload) to stdout.
     */
    struct timeval frame_time_stamp;
    unsigned long long frame_index = 0;

    log_info("PCIe bridge: entering main acquisition loop (frame_size=%zu)", frame_size);

    while (!exit_flag) {
        /* Wait for data from FPGA */
        if (has_bar) {
            if (wait_data_ready() != 0) {
                if (exit_flag) break;
                continue;
            }
        }

        /* Read data via DMA (C2H: card-to-host) */
        int buf_idx = active_dma_buf;

        if (has_xdma) {
            /* Seek to offset 0 for each transfer on the XDMA C2H device */
            if (lseek(c2h_fd, 0, SEEK_SET) < 0) {
                log_error("PCIe bridge: C2H lseek failed: %s", strerror(errno));
            }

            ssize_t ret = dma_read_full(c2h_fd, dma_bufs[buf_idx], frame_size);
            if (ret < 0) {
                log_error("PCIe bridge: DMA read failed: %s", strerror(errno));
                if (exit_flag) break;
                continue;
            }
            if ((size_t)ret != frame_size) {
                log_warn("PCIe bridge: short DMA read: %zd of %zu", ret, frame_size);
            }
        }

        /* Acknowledge data to FPGA via doorbell register */
        if (has_bar)
            bar_write(REG_DOORBELL, 1);

        /* Complete IQ header for this frame */
        gettimeofday(&frame_time_stamp, NULL);
        uint64_t ts_ms = (uint64_t)(frame_time_stamp.tv_sec) * 1000 +
                         (uint64_t)(frame_time_stamp.tv_usec) / 1000;
        iq_header->time_stamp = ts_ms;
        iq_header->daq_block_index = (uint32_t)frame_index;
        iq_header->noise_source_state = (uint32_t)noise_source_state;

        if (en_dummy_frame) {
            iq_header->frame_type = FRAME_TYPE_DUMMY;
            iq_header->data_type = 0;
            iq_header->cpi_length = 0;
        } else {
            if (config.en_fpga_decimate && config.decimation_ratio > 1) {
                iq_header->cpi_length = (uint32_t)config.daq_buffer_size / config.decimation_ratio;
                iq_header->data_type = 3;
            } else {
                iq_header->cpi_length = (uint32_t)config.daq_buffer_size;
                iq_header->data_type = 1;
            }

            if (noise_source_state == 1)
                iq_header->frame_type = FRAME_TYPE_CAL;
            else
                iq_header->frame_type = FRAME_TYPE_DATA;
        }

        /* Write IQ header (1024 bytes) to stdout */
        fwrite(iq_header, sizeof(struct iq_header_struct), 1, stdout);

        /* Write raw IQ data payload to stdout (all channels interleaved) */
        if (!en_dummy_frame) {
            fwrite(dma_bufs[buf_idx], 1, frame_size, stdout);
        }

        fflush(stdout);

        /* Swap DMA buffer (double buffering) */
        active_dma_buf ^= 1;
        frame_index++;

        if (en_dummy_frame) {
            dummy_frame_cntr++;
            if (dummy_frame_cntr >= NO_DUMMY_FRAMES)
                en_dummy_frame = 0;
        }

        /* Handle runtime configuration changes via BAR registers */
        if (center_freq_change_flag && has_bar) {
            bar_write(REG_CENTER_FREQ_LO, new_center_freq);
            iq_header->rf_center_freq = (uint64_t)new_center_freq;
            center_freq_change_flag = 0;
            log_info("PCIe bridge: center freq -> %u Hz", new_center_freq);
        }

        if (gain_change_flag && has_bar) {
            bar_write(REG_GAIN, (uint32_t)config.gain);
            gain_change_flag = 0;
            log_info("PCIe bridge: gain updated");
        }

        if (has_bar && noise_source_state != (int)bar_read(REG_NOISE_SRC)) {
            bar_write(REG_NOISE_SRC, (uint32_t)noise_source_state);
        }

        log_debug("PCIe bridge: frame %llu written (type=%d)",
                  frame_index - 1, iq_header->frame_type);
    }

    /* Cleanup */
    log_info("PCIe bridge: shutting down");

    /* Reset FPGA and unmap BAR */
    if (has_bar) {
        bar_write(REG_CONTROL, CTRL_RESET);
        munmap((void *)bar_ptr, PCIE_BAR_SIZE);
    }
    if (bar_fd >= 0) close(bar_fd);

    /* Close XDMA character devices */
    if (c2h_fd >= 0) close(c2h_fd);
    if (h2c_fd >= 0) close(h2c_fd);

    /* Free DMA buffers */
    for (int i = 0; i < NUM_DMA_BUFS; i++)
        free(dma_bufs[i]);

    pthread_join(ctrl_thread, NULL);
    free(iq_header);

    pthread_mutex_destroy(&ctrl_mutex);
    pthread_cond_destroy(&ctrl_cond);

    log_info("PCIe bridge: all resources freed");
    return 0;
}
