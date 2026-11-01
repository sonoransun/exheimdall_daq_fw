/*
 * 
 * Description :
 * Coherent multichannel receiver for the RTL chipset based software defined radios
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Author  : Tamas Peto, Carl Laufer
 * Compatible hardware: RTL-SDR v3, KerberosSDR, KrakenSDR
 *
 * Copyright (C) 2018-2022  Tamás Pető, Carl Laufer
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
 *
 */

/* Implementation note:
 *  The internally used buffer size variable denotes the number of downloaded byte values (I or Q)
 *  Eg: When the buffer_size has a value of 2**18, then 2**17 IQ sample is actually downloaded per channel
 */

#define _GNU_SOURCE /* F_SETPIPE_SZ */

#include <pthread.h>
#include <stdio.h>
#include <unistd.h>
#include <assert.h>
#include <math.h>

#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <signal.h>
#include <time.h>
#include <fcntl.h>
#include <sys/time.h>
#include <sys/mman.h>
#include <sys/uio.h>
#include <zmq.h>

#include "ini.h"
#include "log.h"
#include "rtl-sdr.h"
#include "rtl_daq.h"
#include "iq_header.h"
#include "sh_mem_util.h" /* compute_port() - NOTE: sh_mem_util.o must be on the rtl_daq link line */
#include "device_map.h"  /* [hw] serial_base / device_serials - device_map.o is on the link line */

#ifdef USEPIGPIO
#include <pigpio.h>
#endif

/* Depth of the circular, coherent read buffer ([daq] usb_ring_buffers) and the
 * number of librtlsdr asynchronous transfers ([daq] usb_async_transfers).
 * Assigned from the config before any allocation; the defaults are the former
 * NUM_BUFF / ASYNC_BUF_NUMBER literals. */
#define USB_DEPTH_MIN 2
#define USB_DEPTH_MAX 64
static int num_buff = 8;
static int async_buf_number = 12;
/* The 128-byte ZMQ message carries 31 four-byte per-channel parameters */
#define ZMQ_MAX_CH_PARAMS 31
#define FS_CORRECTION_KEEP_LIMIT 0.008 // Above this ppm offset the tuning is not terminated after 1 cal. frame
#define INI_FNAME "daq_chain_config.ini"

/*
 * ------> DUMMY FRAMES <------
 * If enabled, the module continues the acquisition,
 * but sends out dummy frames only, until NO_DUMMY_FRAMES number of
 * frames have not been sent out.
 */
#define NO_DUMMY_FRAMES 5
int en_dummy_frame = 0; 
int dummy_frame_cntr = 0;
/* ------> DUMMY FRAMES <------*/

struct rtl_rec_struct* rtl_receivers;
pthread_mutex_t buff_ind_mutex;
pthread_cond_t buff_ind_cond; // This signal is used to notice the main thread that a reader thread is finished
pthread_t fifo_read_thread;  
static pthread_barrier_t rtl_init_barrier;

int rtl_daq_zmq_port = 1130; // Set from config via federation instance_id
/* Bind host of the ZMQ REP socket ([daq] zmq_listen_address, default "*").
 * Written by the ini handler in main() before the ZMQ thread is created. */
static char rtl_daq_zmq_bind[64] = "*";
int reconfig_trigger=0;
volatile sig_atomic_t exit_flag = 0;
int noise_source_state = 0; // Noise source state is used also to track the calibration frame status!
int last_noise_source_state = 0;
int gain_change_flag;
int *new_gains;
int bias_change_flag = 0;
int *new_bias_states; // Runtime inline-LNA bias-tee state per channel (0/1)
float *new_fs_corrections;
int fs_correction_flag, fs_reset_cntr;
uint32_t new_center_freq;
int center_freq_change_flag;
int agc_change_flag = 0;
static uint32_t ch_no, buffer_size;
struct timeval frame_time_stamp;
static int ctr_channel_index;

int gpio_23 = 0;
int gpio_24 = 0;

#ifdef USEPIGPIO
/* Antenna-switch pins ([hw] antenna_switch_gpio); 23/24 when the key is absent.
 * ant_gpio_enabled = 0 for "none": pigpio is not initialised at all. */
static int ant_gpio_a = 23;
static int ant_gpio_b = 24;
static int ant_gpio_enabled = 1;
#endif

/*
 * This structure stores the configuration parameters, 
 * that are loaded from the ini file
 */ 
typedef struct
{
    int num_ch;
    char* en_bias_tee_str;    
    int daq_buffer_size;    
    int sample_rate;
    int center_freq;
    int gain;
    int en_noise_source_ctr;
    int ctr_channel_serial_no;
    int log_level;
    const char* hw_name;
    int hw_unit_id;
    int ioo_type;
    int instance_id;
    int port_stride;
    char* device_serials;     /* [hw] device_serials: comma list, wins over serial_base */
    int serial_base;          /* [hw] serial_base: channel i -> serial_base + i (default 1000) */
    int serial_keys_set;      /* serial_base or a non-empty device_serials is present */
    int hw_unit_id_auto;      /* [hw] unit_id = auto -> header unit_id = instance_id */
    int usb_ring_buffers;     /* [daq] usb_ring_buffers (default 8) */
    int usb_async_transfers;  /* [daq] usb_async_transfers (default 12) */
} configuration;

/* Strict decimal parse for the new integer keys; returns `bad` on garbage so
 * the range checks reject it instead of silently using atoi()'s 0. */
static int parse_int_or(const char* value, int bad)
{
    char *endp = NULL;
    errno = 0;
    long v = strtol(value, &endp, 10);
    if (errno != 0 || endp == value || *endp != '\0' || v < -2147483647L || v > 2147483647L)
        return bad;
    return (int) v;
}

/* Length (without the line ending) of the longest line of `fname` whose
 * first non-blank text is `key` followed by optional blanks and '=' or ':'.
 * Returns 0 when no such line exists or the file cannot be read. */
static long ini_key_line_length(const char* fname, const char* key)
{
    FILE* fp = fopen(fname, "r");
    if (fp == NULL)
        return 0;
    const size_t key_len = strlen(key);
    long longest = 0, len = 0;
    size_t matched = 0; /* characters of `key` matched after the leading blanks */
    enum { LEAD, KEY, AFTER_KEY, HIT, MISS } state = LEAD;
    int c;
    do
    {
        c = fgetc(fp);
        if (c == '\n' || c == EOF)
        {
            if (state == HIT && len > longest)
                longest = len;
            len = 0;
            matched = 0;
            state = LEAD;
            continue;
        }
        if (c != '\r')
            len++;
        if (state == LEAD && c != ' ' && c != '\t')
            state = KEY;
        if (state == KEY)
        {
            if (matched < key_len && c == key[matched])
                matched++;
            else
                state = (matched == key_len) ? AFTER_KEY : MISS;
        }
        if (state == AFTER_KEY && c != ' ' && c != '\t')
            state = (c == '=' || c == ':') ? HIT : MISS;
    } while (c != EOF);
    fclose(fp);
    return longest;
}

/*
 * Ini configuration parser callback function  
*/
static int handler(void* conf_struct, const char* section, const char* name,
                   const char* value)
{
    configuration* pconfig = (configuration*) conf_struct;

    #define MATCH(s, n) strcmp(section, s) == 0 && strcmp(name, n) == 0
    if (MATCH("hw", "num_ch")) 
        {pconfig->num_ch = atoi(value);}
    else if (MATCH("hw","name"))
        {pconfig->hw_name = strdup(value);}
    else if (MATCH("hw","unit_id"))
    {
        pconfig->hw_unit_id_auto = (strcmp(value, "auto") == 0);
        pconfig->hw_unit_id = pconfig->hw_unit_id_auto ? 0 : atoi(value);
    }
    else if (MATCH("hw","serial_base"))
    {
        pconfig->serial_base = parse_int_or(value, -1);
        pconfig->serial_keys_set = 1;
    }
    else if (MATCH("hw","device_serials"))
    {
        free(pconfig->device_serials);
        pconfig->device_serials = strdup(value);
        if (value[0] != '\0')
            pconfig->serial_keys_set = 1;
    }
    else if (MATCH("daq","usb_ring_buffers"))
        {pconfig->usb_ring_buffers = parse_int_or(value, -1);}
    else if (MATCH("daq","usb_async_transfers"))
        {pconfig->usb_async_transfers = parse_int_or(value, -1);}
    else if (MATCH("daq","zmq_listen_address"))
    {
        /* Empty value = key absent (keep "*"); "localhost" is not accepted by
         * zmq_bind, so it maps to the loopback address. */
        if (strcmp(value, "localhost") == 0)
            snprintf(rtl_daq_zmq_bind, sizeof(rtl_daq_zmq_bind), "%s", "127.0.0.1");
        else if (value[0] != '\0')
            snprintf(rtl_daq_zmq_bind, sizeof(rtl_daq_zmq_bind), "%.63s", value);
    }
    else if (MATCH("hw", "ioo_type"))
        {pconfig->ioo_type = atoi(value);}
    else if (MATCH("hw","en_bias_tee"))
        {pconfig->en_bias_tee_str = strdup(value);}
    else if (MATCH("daq", "daq_buffer_size"))
        {pconfig->daq_buffer_size = atoi(value);} 
    else if (MATCH("daq", "sample_rate")) 
        {pconfig->sample_rate = atoi(value);} 
    else if (MATCH("daq", "center_freq")) 
        {pconfig->center_freq = atoi(value);} 
    else if (MATCH("daq", "gain")) 
        {pconfig->gain = atoi(value);} 
    else if (MATCH("daq", "en_noise_source_ctr")) 
        {pconfig->en_noise_source_ctr = atoi(value);}
    else if (MATCH("daq", "ctr_channel_serial_no"))
        {pconfig->ctr_channel_serial_no = atoi(value);}
    else if (MATCH("daq", "log_level"))
        {pconfig->log_level = atoi(value);}
    else if (MATCH("federation", "instance_id"))
        {pconfig->instance_id = atoi(value);}
    else if (MATCH("federation", "port_stride"))
        {pconfig->port_stride = atoi(value);}
    /* Unknown sections/keys are accepted (non-fatal): always return 1 so
     * ini_parse() reports 0 on success and callers can treat a positive
     * return value (malformed line) as a warning only. */
    return 1;
}

static void shutdown_handler(int sig)
{
    (void)sig;
    exit_flag = 1;
}

static void install_signal_handlers(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = shutdown_handler;
    sigemptyset(&sa.sa_mask);
    /* No SA_RESTART so blocked syscalls (zmq_recv, pthread_cond_wait) return */
    sa.sa_flags = 0;

    sigaction(SIGINT,  &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(64,      &sa, NULL);  /* SIGRT used by legacy daq_stop.sh */
}

void * fifo_read_tf(void* arg)
/*
 *  Control FIFO read thread function
 *
 *  This thread function handles the external requests using a ZMQ socket.
 *  Upon receipt of a command, this thread infroms the main thread on the requested operation.
 *  
 *  Return values:
 *  --------------
 *       NULL
 */    
{
    (void)arg; 

    // Initialize ZMQ Socket
    void *context   = zmq_ctx_new ();
    void *responder = zmq_socket (context, ZMQ_REP);
    char zmq_addr[96];
    snprintf(zmq_addr, sizeof(zmq_addr), "tcp://%s:%d", rtl_daq_zmq_bind, rtl_daq_zmq_port);
    int rc          = zmq_bind (responder, zmq_addr);
    if(rc!=0 && strcmp(rtl_daq_zmq_bind, "*") != 0)
        log_fatal("ZMQ bind to %s failed ([daq] zmq_listen_address): %s", zmq_addr, zmq_strerror(zmq_errno()));
    assert (rc == 0);
    if(rc!=0)
    {        
        log_fatal("Failed to open ZMQ socket"); 
        pthread_mutex_lock(&buff_ind_mutex);
        exit_flag = 1;
        pthread_cond_signal(&buff_ind_cond);
        pthread_mutex_unlock(&buff_ind_mutex); 
        return NULL;
    }
    int zmq_rcv_timeout_ms = 500;
    zmq_setsockopt(responder, ZMQ_RCVTIMEO, &zmq_rcv_timeout_ms, sizeof(zmq_rcv_timeout_ms));

    /* Per-channel parameter loops never read past the 31 values a message holds */
    const int n_ch_params = ch_no < ZMQ_MAX_CH_PARAMS ? (int) ch_no : ZMQ_MAX_CH_PARAMS;
    int r_deprecation_logged = 0;

    // Initialize message structure
    struct hdaq_im_msg_struct* msg;
    msg = (struct hdaq_im_msg_struct*) malloc(sizeof(struct hdaq_im_msg_struct));

    /* Main thread loop*/
    while(!exit_flag){

        int nbytes = zmq_recv(responder, msg, 128, 0);
        if (nbytes < 0) {
            if (errno == EAGAIN || errno == EINTR)
                continue;
            log_error("ZMQ recv failed: %s", strerror(errno));
            break;
        }
        log_info("IM Request from: %d",msg->source_module_identifier);
        log_info("Command id: %c",msg->command_identifier);

        /* The parameters start at byte offset 2 of the message: copy the 31
         * four-byte values into aligned storage instead of dereferencing a
         * misaligned uint32_t / float pointer (undefined behaviour). */
        uint32_t param_u32[ZMQ_MAX_CH_PARAMS];
        float param_f32[ZMQ_MAX_CH_PARAMS];
        memcpy(param_u32, msg->parameters, sizeof(param_u32));
        memcpy(param_f32, msg->parameters, sizeof(param_f32));
        
        /* Log the request BEFORE taking buff_ind_mutex: log_log() performs
         * blocking file-backed I/O (stderr is redirected to a log file by the
         * start script) and every SCHED_FIFO USB completion callback contends
         * on this mutex, so no I/O may run while it is held. msg is private
         * to this thread, so it is safe to inspect outside the mutex. */
        if( msg->command_identifier == 'r')
        {
            const uint32_t * parameters = param_u32;
            if (!r_deprecation_logged)
            {
                log_warn("ZMQ 'r' reconfiguration is deprecated");
                r_deprecation_logged = 1;
            }
            log_info("Signal 'r': Reconfiguring the tuner");
            log_info("Center freq: %u MHz", ((unsigned int) parameters[0]/1000000));
            log_info("Sample rate: %u MSps", ((unsigned int) parameters[1]/1000000));
            log_info("Gain: %d dB",(parameters[2]/10));
        }
        else if (msg->command_identifier == 'c')
        {
            const uint32_t * parameters = param_u32;
            log_info("Signal 'c': Center frequency tuning request");
            log_info("New center frequency: %u MHz", ((unsigned int) parameters[0]/1000000));
        }
        else if( msg->command_identifier == 'g')
        {
            const uint32_t * parameters = param_u32;
            log_info("Signal 'g': Gain tuning request");
            for(int i=0;i<n_ch_params;i++){
                log_info("Channel: %d, Gain: %f dB",i, (float) parameters[i]/10);
            }
        }
        else if( msg->command_identifier == 'a')
        {
            log_info("Signal 'a': enable AGC request");
        }
        else if( msg->command_identifier == 's')
        {
            const float * parameters = param_f32;
            log_info("Signal 's': Sampling frequency correction");
            for(int i=0;i<n_ch_params;i++){
                log_info("Channel: %d, fs ppm offset: %.8f",i, parameters[i]);
            }
        }
        else if (msg->command_identifier == 'n')
        {
            //log_warn("Control noise source feature is implemented only for KerberosSDR/KrakenSDR");
            if(msg->parameters[0] == 0)
                {log_info("Turn off noise source");}
            else
                {log_info("Turn on noise source");}
        }
        else if (msg->command_identifier == 'b')
        {
            const uint32_t * parameters = param_u32;
            log_info("Signal 'b': runtime bias-tee switch request");
            for(int i=0;i<n_ch_params;i++){
                log_info("Channel: %d, bias-tee: %d", i, parameters[i] ? 1 : 0);
            }
        }
        else if(msg->command_identifier == 'h')
        {
            log_info("Signal 2: FIFO read thread exiting \n");
        }

        /* Publish the request to the main thread. This critical section is
         * kept free of any blocking I/O (no logging, no zmq_send). */
        pthread_mutex_lock(&buff_ind_mutex);

        /* Tuner reconfiguration request */
        if( msg->command_identifier == 'r')
        {
            const uint32_t * parameters = param_u32;
            for(int i=0; i<ch_no; i++)
            {
              rtl_receivers[i].gain = (int) parameters[2];
              rtl_receivers[i].center_freq = parameters[0];
              rtl_receivers[i].sample_rate = parameters[1];
            }
            reconfig_trigger=1;
        }
        /* Center Frequency Tuning */
        else if (msg->command_identifier == 'c')
        {
            const uint32_t * parameters = param_u32;
            new_center_freq = parameters[0];
            center_freq_change_flag = 1;
        }
        /* Gain tuning*/
        else if( msg->command_identifier == 'g')
        {
            const uint32_t * parameters = param_u32;
            for(int i=0;i<n_ch_params;i++){
                new_gains[i] = (int) parameters[i];
            }
            gain_change_flag=1;
        }
        /* Enable AGC */
        else if( msg->command_identifier == 'a')
        {
            agc_change_flag = 1;
        }
        /* Sampling Freq Correction - Used for sampling clock delay tuning*/
        else if( msg->command_identifier == 's')
        {
            const float * parameters = param_f32;
            for(int i=0;i<n_ch_params;i++){
                new_fs_corrections[i] = parameters[i];
            }
            fs_reset_cntr = 0;
            fs_correction_flag=1;
        }
        /* Noise source switch requests */
        else if (msg->command_identifier == 'n')
        {
            noise_source_state = (msg->parameters[0] == 0) ? 0 : 1;
        }
        /* Runtime inline-LNA bias-tee switch request */
        else if (msg->command_identifier == 'b')
        {
            const uint32_t * parameters = param_u32;
            for(int i=0;i<n_ch_params;i++){
                new_bias_states[i] = parameters[i] ? 1 : 0;
            }
            bias_change_flag = 1;
        }
        /* System halt request */
        else if(msg->command_identifier == 'h')
        {
            exit_flag = 1;
        }
        /* Send out dummy frames while the changes takes effect*/
        en_dummy_frame = 1;
        dummy_frame_cntr = 0;

        pthread_cond_signal(&buff_ind_cond);
        pthread_mutex_unlock(&buff_ind_mutex);

        /* Reply after releasing the mutex: the REQ/REP pair is in lockstep on
         * this thread (recv follows this send), so replying after the signal
         * cannot reorder commands, and a blocking zmq_send no longer stalls
         * the USB callbacks contending on buff_ind_mutex. */
        zmq_send (responder, "ok", 2, 0);
    }
    free(msg);
    zmq_close(responder);
    zmq_ctx_destroy(context);
    return NULL;
}


void rtlsdrCallback(unsigned char *buf, uint32_t len, void *ctx)
/*   
 *                RTL-SDR Async read callback function
 *
 *  Description:
 *  ------------
 * 
 *       Reads samples from the device asynchronously. This function will block until
 *       it is being canceled using rtlsdr_cancel_async() [rtl-sdr.h]
 * 
 *  Arguments:
 *  ----------
 *       *buf: Sample buffer
 *        len: Length of the buffer
 *       *ctx: Descriptor structure of the current rtl_sdr 
 * 
 *       
 */
{
    struct rtl_rec_struct *rtl_rec = (struct rtl_rec_struct *) ctx;// Set the receiver's structure

    int wr_buff_ind = (int)(rtl_rec->buff_ind % (unsigned long long) num_buff); // Calculate current buffer index in the circular buffer

    /* Guard against short (or oversized) USB transfers: publishing a partially
     * filled slot would silently break multichannel coherence. Copy what
     * arrived, zero-fill the stale tail and warn. */
    if (len != buffer_size)
    {
        uint32_t copy_len = (len < buffer_size) ? len : buffer_size;
        memcpy(rtl_rec->buffer + (size_t)buffer_size * wr_buff_ind, buf, copy_len);
        if (copy_len < buffer_size)
            memset(rtl_rec->buffer + (size_t)buffer_size * wr_buff_ind + copy_len, 0, buffer_size - copy_len);
        log_warn("Unexpected USB transfer length on device %d: %u bytes instead of %u, tail zero-filled",
                 rtl_rec->dev_ind, len, buffer_size);
    }
    else
    {
        memcpy(rtl_rec->buffer + (size_t)buffer_size * wr_buff_ind, buf, buffer_size);
    }

#ifndef NDEBUG
    log_debug("Read at device:%d, buff index:%llu, write index:%d",rtl_rec->dev_ind, rtl_rec->buff_ind, wr_buff_ind);
#endif

    /* Publish the buffer and signal the main thread that new data is ready.
     * The mutex makes the 64-bit index update visible/atomic for the main
     * thread and guarantees the wakeup is not lost; keep this section short. */
    pthread_mutex_lock(&buff_ind_mutex);
    rtl_rec->buff_ind++;
    pthread_cond_signal(&buff_ind_cond);
    pthread_mutex_unlock(&buff_ind_mutex);
}

static int write_full_iov(int fd, struct iovec *iov, int iovcnt)
/*
 * Writes all bytes described by the iovec array to fd, handling partial
 * writes and EINTR (signal handlers are installed without SA_RESTART).
 * The iovec array is modified in place. Returns 0 on success, -1 on error
 * or when exit_flag is raised while interrupted.
 */
{
    size_t remaining = 0;
    for (int k = 0; k < iovcnt; k++)
        remaining += iov[k].iov_len;
    while (remaining > 0)
    {
        ssize_t written = writev(fd, iov, iovcnt);
        if (written < 0)
        {
            if (errno == EINTR)
            {
                if (exit_flag) {return -1;}
                continue;
            }
            return -1;
        }
        remaining -= (size_t)written;
        while (written > 0 && iovcnt > 0)
        {
            if ((size_t)written >= iov[0].iov_len)
            {
                written -= (ssize_t)iov[0].iov_len;
                iov++;
                iovcnt--;
            }
            else
            {
                iov[0].iov_base = (char*)iov[0].iov_base + written;
                iov[0].iov_len -= (size_t)written;
                written = 0;
            }
        }
    }
    return 0;
}

void *read_thread_entry(void *arg)
/*
 *                 Tuner read and configuration thread
 *
 *  Description:
 *  ------------
 *       Initializes the RTL-SDR devices with the given paramers, and then start the async read.
 * 
 *       Initialization procedures includes:
 *           - Opening the device
 *           - Disable dithering (To avoid phase drift)
 *           - Disable automatic gain control (For amplitude and phase calibration this is mandatory)
 *           - Set center frequency
 *           - Set tuner gain value
 *           - Set sampling frequency
 *           - Reset buffers
 * 
 *       In case the control FIFO receives the reconfiguration command ('r'), the main thread stops the asynchronous read
 *       and this thread re-
 * 
 *  Arguments:
 *  ----------
 *       *arg: Descriptor structure of the current rtl_sdr 
 * 
 *  Return values:
 *  --------------
 *       NULL
 */
{   
    struct rtl_rec_struct *rtl_rec = (struct rtl_rec_struct *) arg;// Set the thread's own receiver structure   
    log_info("Initializing RTL-SDR device, index:%d", rtl_rec->dev_ind);   
   
    rtlsdr_dev_t *dev = NULL;
    
    dev = rtl_rec->dev; // Set rtl-sdr device descriptor

    /* Disable dithering */
    if (rtlsdr_set_dithering(dev, 0) !=0) // Only in keenerd's driver
    {
        log_error("Failed to disable dithering: %s", strerror(errno));
    }
    /* Set gain control mode */
    if (rtlsdr_set_tuner_gain_mode(dev, 1) !=0)
    {
        log_error("Failed to disbale AGC: %s", strerror(errno));
    }
   
    while(!exit_flag)
    {
   
        //(rtlsdr_set_testmode(dev, 1)); // Set this to enable test mode
        
        /* Set center frequency */
        if (rtlsdr_set_center_freq(dev, rtl_rec->center_freq) !=0)
        {
            log_error("Failed to set center frequency: %s", strerror(errno));
        }
        rtl_rec->center_freq = rtlsdr_get_center_freq(rtl_rec->dev);

        /* Set tuner gain */
        if (rtlsdr_set_tuner_gain(dev, rtl_rec->gain) !=0)
        {
            log_error("Failed to set gain value: %s", strerror(errno));
        }
        /* Set sampling frequency */
        if (rtlsdr_set_sample_rate(dev, rtl_rec->sample_rate) !=0)
        {
            log_error("Failed to set sample rate: %s", strerror(errno));
        }
        
        /*Set all gpios into the default off state*/
        //rtlsdr_set_gpio(dev, 0, 0);
        //rtlsdr_set_gpio(dev, 0, 1);
        //rtlsdr_set_gpio(dev, 0, 2);
        //rtlsdr_set_gpio(dev, 0, 3);
        //rtlsdr_set_gpio(dev, 0, 4);
        //rtlsdr_set_gpio(dev, 0, 5);
        //rtlsdr_set_gpio(dev, 0, 6);
	    
	//rtlsdr_set_bias_tee_gpio(dev, 0, 0);
        //rtlsdr_set_bias_tee_gpio(dev, 1, 0);
        //rtlsdr_set_bias_tee_gpio(dev, 2, 0);
        //rtlsdr_set_bias_tee_gpio(dev, 3, 0);
        //rtlsdr_set_bias_tee_gpio(dev, 4, 0);
        //rtlsdr_set_bias_tee_gpio(dev, 5, 0);
        //rtlsdr_set_bias_tee_gpio(dev, 6, 0);

        /* Reset buffers */
        if (rtlsdr_reset_buffer(dev) !=0)
        {
            log_error("Failed to reset receiver buffer: %s", strerror(errno));
        }
        log_info("Device is initialized %d", rtl_rec->dev_ind);
        if (rtl_rec->dev_ind == 0)
        {
            log_info("Exact sample rate: %d Hz", rtlsdr_get_sample_rate(dev));
            log_info("Exact center frequency: %d Hz",rtlsdr_get_center_freq(dev));
        }

        /* Starting Asychronous read*/
        pthread_barrier_wait(&rtl_init_barrier);
        rtlsdr_read_async(dev, rtlsdrCallback, rtl_rec, (uint32_t) async_buf_number, buffer_size);
    }    
return NULL;
}


#ifdef USEPIGPIO
/* [hw] antenna_switch_gpio pre-pass: read before pigpio is initialised (which
 * happens before the main ini_parse, as it always has). Reads that one key
 * only and never logs; a parse error is reported by the main parse. */
typedef struct
{
    int present;
    char value[64];
} gpio_prepass_cfg;

static int gpio_prepass_handler(void* conf_struct, const char* section, const char* name,
                                const char* value)
{
    gpio_prepass_cfg* pcfg = (gpio_prepass_cfg*) conf_struct;
    if (strcmp(section, "hw") == 0 && strcmp(name, "antenna_switch_gpio") == 0)
    {
        pcfg->present = (value[0] != '\0'); /* empty value = key absent (pins 23/24) */
        snprintf(pcfg->value, sizeof(pcfg->value), "%s", value);
    }
    return 1;
}

/* "A,B": two distinct BCM GPIO numbers 0..27. Returns 0 on success. */
static int parse_gpio_pair(const char* value, int* a, int* b)
{
    char tail;
    if (sscanf(value, " %d , %d %c", a, b, &tail) != 2)
        return -1;
    if (*a < 0 || *a > 27 || *b < 0 || *b > 27 || *a == *b)
        return -1;
    return 0;
}
#endif

// Memory locking for real-time performance
int init_rt_memory(void) {
    // Lock all current and future memory
    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0) {
        log_error("mlockall failed: %s", strerror(errno));
        return -1;
    }

    // Pre-fault stack pages to avoid page faults during RT operation
    char dummy[64 * 1024];
    memset(dummy, 0, sizeof(dummy));

    log_info("Real-time memory locking initialized");
    return 0;
}

int main( int argc, char** argv )
{
    log_set_level(LOG_TRACE);
    log_use_default_lock(); /* rtl_daq logs from the USB reader threads, the ZMQ thread and main */
    install_signal_handlers();

    // Initialize real-time memory management
    if (init_rt_memory() != 0) {
        log_warn("Failed to initialize RT memory locking - continuing anyway");
    }

    configuration config;
    memset(&config, 0, sizeof(config));
    config.port_stride = 100;
    config.serial_base = 1000;
    config.usb_ring_buffers = 8;
    config.usb_async_transfers = 12;

    #ifdef USEPIGPIO
    {
        gpio_prepass_cfg gpio_cfg;
        memset(&gpio_cfg, 0, sizeof(gpio_cfg));
        (void) ini_parse(INI_FNAME, gpio_prepass_handler, &gpio_cfg);
        if (gpio_cfg.present && strcmp(gpio_cfg.value, "none") == 0)
        {
            ant_gpio_enabled = 0;
            log_info("antenna switch GPIO: none (skipped)");
        }
        else if (gpio_cfg.present &&
                 parse_gpio_pair(gpio_cfg.value, &ant_gpio_a, &ant_gpio_b) != 0)
        {
            log_fatal("Invalid [hw] antenna_switch_gpio '%s': use 'none' or two distinct GPIO numbers 0..27 as 'A,B'",
                      gpio_cfg.value);
            return -1;
        }
    }
    if (ant_gpio_enabled)
    {
        // PIGPIO
        if (gpioInitialise() < 0)
        {
           log_fatal("pigpio init failed");
           return -1;
        }

        gpioSetMode(ant_gpio_a, PI_OUTPUT);
        gpioSetMode(ant_gpio_b, PI_OUTPUT);
        gpioWrite(ant_gpio_a, 1); /* enable antenna input 1 by default */
        gpioWrite(ant_gpio_b, 0); /* disable antenna input 2 */
    }
    #endif

    /* Set parameters from the config file*/
    int ini_status = ini_parse(INI_FNAME, handler, &config);
    if (ini_status < 0)
    {
        log_fatal("Configuration could not be loaded, exiting ..");
        return -1;
    }
    else if (ini_status > 0)
    {
        log_warn("Config file %s has a parse error at line %d, continuing with parsed values", INI_FNAME, ini_status);
    }
    if (config.num_ch < 1 || config.num_ch > 32)
    {
        log_fatal("Invalid or missing channel number in configuration: %d", config.num_ch);
        return -1;
    }
    if (config.daq_buffer_size < 1)
    {
        log_fatal("Invalid or missing daq_buffer_size in configuration: %d", config.daq_buffer_size);
        return -1;
    }
    if (config.usb_ring_buffers < USB_DEPTH_MIN || config.usb_ring_buffers > USB_DEPTH_MAX)
    {
        log_fatal("Invalid [daq] usb_ring_buffers: %d (allowed %d..%d)",
                  config.usb_ring_buffers, USB_DEPTH_MIN, USB_DEPTH_MAX);
        return -1;
    }
    if (config.usb_async_transfers < USB_DEPTH_MIN || config.usb_async_transfers > USB_DEPTH_MAX)
    {
        log_fatal("Invalid [daq] usb_async_transfers: %d (allowed %d..%d)",
                  config.usb_async_transfers, USB_DEPTH_MIN, USB_DEPTH_MAX);
        return -1;
    }
    num_buff = config.usb_ring_buffers;
    async_buf_number = config.usb_async_transfers;

    /* inih keeps INI_MAX_LINE-1 bytes per line: the rest of a longer
     * device_serials line becomes a bogus key or a continuation that replaces
     * the value, so the list would be silently cut. Refuse it by name. */
    if (config.device_serials != NULL && config.device_serials[0] != '\0')
    {
        long len = ini_key_line_length(INI_FNAME, "device_serials");
        if (len > INI_MAX_LINE - 1)
        {
            log_fatal("[hw] device_serials line is %ld bytes long; the INI reader keeps at most %d bytes"
                      " per line - shorten it (e.g. drop the spaces after the commas)",
                      len, INI_MAX_LINE - 1);
            return -1;
        }
    }

    /* Channel -> device serial map ([hw] device_serials, else serial_base + i) */
    char serials[DEVICE_MAP_MAX_CH][DEVICE_MAP_SERIAL_LEN];
    {
        char map_err[128];
        if (device_map_resolve(config.device_serials, config.serial_base, config.num_ch,
                               serials, map_err, sizeof map_err) != 0)
        {
            log_fatal("device map: %s", map_err);
            return -1;
        }
    }
    buffer_size = config.daq_buffer_size*2;
    ch_no = config.num_ch;
    rtl_daq_zmq_port = compute_port(1130, config.instance_id, config.port_stride);

    log_set_level(config.log_level);

#ifdef F_SETPIPE_SZ
    /* Best effort: enlarge the stdout pipe towards the rebuffer module so a
     * whole IQ frame fits without extra wakeups. Non-fatal on failure. */
    {
        long desired_pipe_size = (long)buffer_size * ch_no + IQ_HEADER_LENGTH;
        if (fcntl(STDOUT_FILENO, F_SETPIPE_SZ, (int)desired_pipe_size) < 0 &&
            fcntl(STDOUT_FILENO, F_SETPIPE_SZ, 1048576) < 0)
        {
            log_warn("Could not enlarge stdout pipe: %s - continuing with default pipe size", strerror(errno));
        }
    }
#endif

    /* -> Parse bias tee config (missing/short lists default to disabled) */
    int en_bias_tee[ch_no];
    memset(en_bias_tee, 0, sizeof(en_bias_tee));
    if (config.en_bias_tee_str != NULL)
    {
        char * en_bias_ch_i_str = strtok(config.en_bias_tee_str, ",");
        int i = 0;
        while( en_bias_ch_i_str != NULL && i < ch_no )
        {
            en_bias_tee[i] = atoi(en_bias_ch_i_str);
            en_bias_ch_i_str = strtok(NULL, ",");
            i++;
        }
    }
    log_info("Config succesfully loaded from %s",INI_FNAME);
    log_info("Channel number: %d", ch_no);
    log_info("Number of IQ samples per channel: %d", buffer_size/2);    
    log_info("Starting multichannel coherent RTL-SDR receiver");
    if (config.en_noise_source_ctr == 1)
        log_info("Noise source control: enabled");
    else
        log_info("Noise source control: disabled");    
    
    /* Get control channel device index */
    char dev_serial[16];
    sprintf(dev_serial, "%d", config.ctr_channel_serial_no);
    int ctr_channel_dev_index = rtlsdr_get_index_by_serial(dev_serial);    
    if(ctr_channel_dev_index==-3)
    {
        log_warn("Failed to identify control channel index based on its configured serial number:%s",dev_serial);
        log_warn("Set to default device index: 0");
        ctr_channel_dev_index=0;
    }

    /* Duplicate-serial defence: two attached devices carrying the same serial
     * make rtlsdr_get_index_by_serial() silently pick the first one. Fatal when
     * the serials were configured explicitly, a warning otherwise (today's
     * first-match behaviour is kept so a working config keeps working). */
    {
        int n_present = (int) rtlsdr_get_device_count();
        char (*present)[DEVICE_MAP_SERIAL_LEN] = NULL;
        if (n_present > 0)
        {
            present = calloc((size_t) n_present, sizeof(*present));
            if (!present)
            {
                log_fatal("Device serial list allocation failed. Exiting..");
                return -1;
            }
            for (int j = 0; j < n_present; j++)
            {
                char manufact[256], product[256], serial[256];
                serial[0] = '\0';
                if (rtlsdr_get_device_usb_strings((uint32_t) j, manufact, product, serial) == 0)
                    snprintf(present[j], DEVICE_MAP_SERIAL_LEN, "%.15s", serial);
            }
        }
        for (int i = 0; i < ch_no; i++)
        {
            int n = device_map_count(serials[i], (const char (*)[DEVICE_MAP_SERIAL_LEN]) present, n_present);
            if (n <= 1)
                continue;
            if (config.serial_keys_set)
            {
                log_fatal("serial %s is present on %d devices; program distinct serials with util/set_unit_serials.sh",
                          serials[i], n);
                free(present);
                return -1;
            }
            log_warn("serial %s is present on %d devices; program distinct serials with util/set_unit_serials.sh",
                     serials[i], n);
        }
        free(present);
    }

    /* Allocation */    
    struct iq_header_struct* iq_header = calloc(1, sizeof(struct iq_header_struct));
    
    new_gains          = calloc(ch_no, sizeof(*new_gains));
    new_fs_corrections = calloc(ch_no, sizeof(*new_fs_corrections));
    new_bias_states    = calloc(ch_no, sizeof(*new_bias_states));
    
    rtl_receivers = malloc(sizeof(struct rtl_rec_struct)*ch_no);    
    for(int i=0; i<ch_no; i++)
    {
        struct rtl_rec_struct *rtl_rec = &rtl_receivers[i];
        memset(rtl_rec, 0, sizeof(struct rtl_rec_struct));

        // Get device index by serial number
        strcpy(dev_serial, serials[i]);
        int dev_index = rtlsdr_get_index_by_serial(dev_serial);
        rtl_rec->dev_ind = dev_index;
        log_info("Device serial:%s, index: %d",dev_serial, dev_index);
        if(dev_index==-3)
        {
            log_fatal("The serial numbers of the devices are not yet configured (expected serial %s for channel %d;"
                      " see [hw] serial_base/device_serials and util/set_unit_serials.sh), exiting..",
                      dev_serial, i);
            return(-1);
        }

        // Set noise control channel        
        if(dev_index == ctr_channel_dev_index){ctr_channel_index=i;}
    }

   
    // Initialization
    for(int i=0; i<ch_no; i++)
    {
        struct rtl_rec_struct *rtl_rec = &rtl_receivers[i];
        rtl_rec->buff_ind=0;        
        rtl_rec->gain = config.gain;
        rtl_rec->agc = 0;
        rtl_rec->center_freq = config.center_freq;
        rtl_rec->sample_rate = config.sample_rate;
        rtl_rec->buffer = malloc((size_t) num_buff * buffer_size * sizeof(uint8_t));      
        if(! rtl_rec->buffer)
        {
            log_fatal("Data buffer allocation failed. Exiting..");   
            return -1;
        }
           
    }
    /* Fill up the static fields of the IQ header */    
	iq_header->sync_word = SYNC_WORD;
    iq_header->header_version = IQ_HEADER_VERSION;
	/* hardware_id is a 16-byte wire-ABI field: truncate and keep it NUL-padded
	 * (the header was calloc'd and strncpy pads the remainder with NULs) */
	strncpy(iq_header->hardware_id, config.hw_name ? config.hw_name : "",
	        sizeof(iq_header->hardware_id)-1);
	iq_header->unit_id = config.hw_unit_id_auto ? config.instance_id : config.hw_unit_id;
	iq_header->active_ant_chs=ch_no;
	iq_header->ioo_type=config.ioo_type;
	iq_header->rf_center_freq= (uint64_t) config.center_freq;
	iq_header->adc_sampling_freq = (uint64_t) config.sample_rate; 
	iq_header->sampling_freq=(uint64_t) config.sample_rate; // Overwriten by the decimator module 
	iq_header->cpi_length= (uint32_t) config.daq_buffer_size; // Overwriten by the decimator module 
	iq_header->time_stamp=0; // Unix Epoch time
	iq_header->daq_block_index=0; // DAQ buffer index
	iq_header->cpi_index=0; // Filled up by the decimator module 
	iq_header->ext_integration_cntr=0; // Extended integration counter is not used by RTL-DAQs
	iq_header->frame_type=FRAME_TYPE_DATA; // Normal data frame	
	iq_header->data_type=2; // IQ data
	iq_header->sample_bit_depth=8; // RTL2832U
	iq_header->adc_overdrive_flags=0;
	for(int m=0;m<ch_no;m++)
	{
	    iq_header->if_gains[m]=(uint32_t) config.gain;
	}
	iq_header->delay_sync_flag=0;
	iq_header->iq_sync_flag=0;
    iq_header->sync_state=0;
	iq_header->noise_source_state=0;

    pthread_mutex_init(&buff_ind_mutex, NULL);
    pthread_cond_init(&buff_ind_cond, NULL);     

    /* Spawn control thread */
    pthread_create(&fifo_read_thread, NULL, fifo_read_tf, NULL);

    /* Opening RTL-SDR devices*/
    for(int i=0; i<ch_no; i++)
    {
    	struct rtl_rec_struct *rtl_rec = &rtl_receivers[i]; 
    	rtlsdr_dev_t *dev = NULL;
    	if (rtlsdr_open(&dev, rtl_rec->dev_ind) !=0)
    	{
       		log_fatal("Failed to open RTL-SDR device: %s", strerror(errno));
            return -1;
    	}
    	rtl_rec->dev = dev;
    }
    /*-> Enable/Disable bias tees*/
    for(int m=0;m<ch_no;m++)
    {
        if(en_bias_tee[m])
            {log_info("Bias tee on channel: %d is enabled",m);}
        else
            {log_info("Bias tee on channel: %d is disabled",m);}

        struct rtl_rec_struct *rtl_rec = &rtl_receivers[ctr_channel_index];    
	rtlsdr_set_bias_tee_gpio(rtl_rec->dev, m+1, en_bias_tee[m]);
        //rtlsdr_set_gpio(rtl_rec->dev, en_bias_tee[m] , m+1);
    }

    pthread_barrier_init(&rtl_init_barrier, NULL, ch_no);
    /* Spawn reader threads */
    for(int i=0; i<ch_no; i++)
    {       
        pthread_create(&rtl_receivers[i].async_read_thread, NULL, read_thread_entry, &rtl_receivers[i]);
    }

    unsigned long long read_buff_ind = 0;
    int data_ready = 1;
    int rd_buff_ind = 0;
    uint8_t overdrive_flags=0;
    uint32_t overrun_cntr = 0; /* Cumulative USB ring-buffer overrun events, stamped into reserved[IQH_RSV_BUFFER_OVERRUN_CNT] */
    struct rtl_rec_struct *rtl_rec;
    /*
     *
     * ---> Main data acquistion loop <---
     *
     * buff_ind_mutex is held while evaluating the per-channel buffer indexes
     * (they are written by the USB reader threads under the same mutex) and is
     * released for the frame emission / tuner control work. The condvar wait
     * keeps the 250 ms timeout purely as a safety net against missed wakeups.
     */
    pthread_mutex_lock(&buff_ind_mutex);
    while( !exit_flag )
    {
        /* We are checking here the current buffer indexes of the reader threads.
         * All the reader threads should reach the same index before we could send out the data,
         * and we could coninue the acquisition.
        */
        {
            struct timespec ts;
            clock_gettime(CLOCK_REALTIME, &ts);
            ts.tv_nsec += 250000000L; /* 250 ms */
            if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
            pthread_cond_timedwait(&buff_ind_cond, &buff_ind_mutex, &ts);
        }
        if (exit_flag) break;
        data_ready = 1;
        int overrun_detected = 0;
        int race_detected = 0;
        unsigned long long race_buff_ind = 0;
        for(int i=0; i<ch_no; i++)
        {
            rtl_rec = &rtl_receivers[i];
            if (rtl_rec->buff_ind <= read_buff_ind)
            {data_ready = 0; break;}

            if((rtl_rec->buff_ind - read_buff_ind) >= (unsigned long long) num_buff)
            {overrun_detected = 1;}
            if(rtl_rec->buff_ind % (unsigned long long) num_buff == read_buff_ind % (unsigned long long) num_buff)
            {
                /* Record the fact only; logging is blocking file I/O and must
                 * not run under buff_ind_mutex (every SCHED_FIFO USB callback
                 * contends on it). */
                race_detected = 1;
                race_buff_ind = rtl_rec->buff_ind;
                data_ready = 0;
                break;
            }
        }
        if (race_detected)
        {
            /* Log outside the critical section. The reader indexes only grow
             * and are re-evaluated on every iteration, so a wakeup signalled
             * during the unlocked window is at worst deferred to the 250 ms
             * condvar timeout - the same bound as during frame emission. */
            pthread_mutex_unlock(&buff_ind_mutex);
            log_warn("Likely race condition. Skipping data aquicision. RTL Buff index: %llu, read_buff_ind: %llu", race_buff_ind, read_buff_ind);
            pthread_mutex_lock(&buff_ind_mutex);
        }

        if (data_ready == 1)
        {
            pthread_mutex_unlock(&buff_ind_mutex);
            if (overrun_detected)
            {
                /* The reader threads lapped the main loop in the num_buff-slot
                 * ring: unread transfers were overwritten, so channel data in
                 * this frame may come from different USB epochs (coherence not
                 * guaranteed). Detection only - the frame is still emitted with
                 * its regular daq_block_index; downstream can key off the
                 * header counter. Counted once per EMITTED frame while lagged,
                 * not per condvar wakeup, so the header delta reflects the
                 * number of frames actually affected. */
                overrun_cntr++;
                if (overrun_cntr <= 5 || (overrun_cntr % 100) == 0)
                    log_warn("USB ring-buffer overrun (affected frame #%u): reader lag reached %d transfers."
                             " Channel coherence is not guaranteed. Consider increasing the number of buffers."
                             " - raise [daq] usb_ring_buffers (now %d)",
                             overrun_cntr, num_buff, num_buff);
            }
            /*
             *---------------------
             *  Complete IQ header 
             *---------------------
            */
            // Acquire local time in ms (Unix EPOC) and set timestamp field
            gettimeofday(&frame_time_stamp, NULL);                    
            uint64_t time_stamp_ms = (uint64_t)(frame_time_stamp.tv_sec) * 1000 +
                                     (uint64_t)(frame_time_stamp.tv_usec) / 1000;
            log_debug("Timestamp: %llu", time_stamp_ms);
            iq_header->time_stamp = time_stamp_ms;
            iq_header->daq_block_index = (uint32_t) read_buff_ind;
            iq_header->reserved[IQH_RSV_BUFFER_OVERRUN_CNT] = overrun_cntr;
            /* Buffer slot of the frame that is being sent out in this iteration */
            rd_buff_ind = (int)(read_buff_ind % (unsigned long long) num_buff);
            for(int i=0; i<ch_no; i++)
            {
                rtl_rec = &rtl_receivers[i];
                // Set center frequncy value
                iq_header->rf_center_freq = (uint64_t) rtl_rec->center_freq;
                // Set gain value
                iq_header->if_gains[i] = (uint32_t) rtl_rec->gain;
                // Check overdrive (memchr stops at the first saturated sample)
                if (memchr(rtl_rec->buffer + (size_t)buffer_size * rd_buff_ind, 255, buffer_size) != NULL)
                    overdrive_flags |= 1<<i;
            }
            iq_header->adc_overdrive_flags = (uint32_t) overdrive_flags;
            iq_header->noise_source_state = (uint32_t) noise_source_state;
            // Set frame type in the header
            if(en_dummy_frame)
            {
                iq_header->frame_type = FRAME_TYPE_DUMMY; // Dummy frame
                iq_header->data_type  = 0; // Dummy data
                iq_header->cpi_length = 0;
            }
            else
            {
                iq_header->cpi_length= (uint32_t) config.daq_buffer_size;
                iq_header->data_type=1;
                if (noise_source_state ==1) // Calibration frame
                {
                    iq_header->frame_type=FRAME_TYPE_CAL;
                }
                else // Normal data frame
                {
                    iq_header->frame_type=FRAME_TYPE_DATA;                    
                }
            }
            /*
            *----------------------------------------------------
            *  Send IQ header + payload in a single coalesced write
            *----------------------------------------------------
            */
            {
                struct iovec frame_iov[33]; /* 1 header + max 32 channels (ch_no validated <= 32) */
                int iovcnt = 0;
                frame_iov[iovcnt].iov_base = iq_header;
                frame_iov[iovcnt].iov_len  = sizeof(struct iq_header_struct);
                iovcnt++;
                if(en_dummy_frame == 0) // DATA or CAL frame
                {
                    for(int i=0; i<ch_no; i++)
                    {
                        rtl_rec = &rtl_receivers[i];
                        frame_iov[iovcnt].iov_base = rtl_rec->buffer + (size_t)buffer_size * rd_buff_ind;
                        frame_iov[iovcnt].iov_len  = buffer_size;
                        iovcnt++;
                    }
                }
                if (write_full_iov(STDOUT_FILENO, frame_iov, iovcnt) != 0)
                {
                    if (!exit_flag)
                    {
                        log_fatal("IQ frame write failed: %s", strerror(errno));
                        exit_flag = 1;
                    }
                }
            }
            if(overdrive_flags !=0)
                log_warn("Overdrive detected, flags: 0x%02X", overdrive_flags);

            overdrive_flags=0;
            read_buff_ind ++;
            if (en_dummy_frame)
            {
                dummy_frame_cntr +=1;
                if (dummy_frame_cntr == NO_DUMMY_FRAMES)
                    en_dummy_frame = 0;
            }
            log_debug("IQ frame writen, block index: %d, type:%d",iq_header->daq_block_index, iq_header->frame_type);
            /*
            *-------------------
            *   Tuner control
            *-------------------
            */

            /* We need to reconfigure the tuner, so the async read must be stopped*/
            // This feature is deprecated !!!
            if(reconfig_trigger==1)
            {
                for(int i=0; i<ch_no; i++)
                {                
                    if(rtlsdr_cancel_async(rtl_receivers[i].dev) != 0)
                    {
                        log_error("Async read stop failed: %s", strerror(errno));
                    }                    
                }
                reconfig_trigger=0;
            }
            /* Center frequency tuning request*/
            if(center_freq_change_flag == 1)
            {
                for( int i=0; i<ch_no; i++)
                {
                    rtl_rec = &rtl_receivers[i];         
                    if (rtlsdr_set_center_freq(rtl_rec->dev, new_center_freq) !=0)
                    {
                        log_error("Failed to set center frequency: %s", strerror(errno));
                    }                    
                    else
                    {
                        rtl_rec->center_freq = rtlsdr_get_center_freq(rtl_rec->dev);
                        log_info("Center frequency changed at ch: %d, frequency: %d",i,rtl_rec->center_freq);
                    }
                }
                center_freq_change_flag=0;
            }
            
            /* Gain change request */
            if(gain_change_flag==1)
            {
                for( int i=0; i<ch_no; i++)
                {
                    rtl_rec = &rtl_receivers[i];
                    if (rtlsdr_set_tuner_gain(rtl_rec->dev, new_gains[i]) !=0){
                        log_error("Failed to set gain value: %s", strerror(errno));
                    }
                    else{
                        log_info("Gain change at ch: %d, gain %d",i, new_gains[i]);
                        rtl_rec->gain = new_gains[i];
                        rtl_rec->agc = 0;
                    }
                }
                gain_change_flag=0;
            }
            /* Runtime bias-tee switch request.
             * Per-channel inline-LNA bias uses GPIO m+1 on the control-channel
             * device; GPIO 0 is reserved for the noise source, so bias never
             * collides with it. Defer while a calibration noise burst is active
             * to avoid toggling the control device mid-burst. */
            if (bias_change_flag == 1)
            {
                if (noise_source_state == 1)
                {
                    log_warn("Bias-tee change deferred: noise-source calibration burst active");
                }
                else
                {
                    rtl_rec = &rtl_receivers[ctr_channel_index];
                    for (int m = 0; m < ch_no; m++)
                    {
                        rtlsdr_set_bias_tee_gpio(rtl_rec->dev, m+1, new_bias_states[m]);
                        log_info("Bias-tee ch %d (GPIO %d) set to %d", m, m+1, new_bias_states[m]);
                    }
                    bias_change_flag = 0;
                }
            }
            /* Enable AGC request */
            if (agc_change_flag == 1)
            {
                for (int i = 0; i < ch_no; i++)
                {
                    rtl_rec = &rtl_receivers[i];
                    if (rtlsdr_set_tuner_gain_mode(rtl_rec->dev, 0))
                    {
                        log_error("Failed to set AGC state: %s",
                                  strerror(errno));
                    }
                    else
                    {
                        log_info("Enabled AGC at ch: %d", i);
                        rtl_rec->agc = 1;
                    }
                }
                agc_change_flag = 0;
            }
            /* Sampling frequency correction flag */
            if(fs_correction_flag==1)
            {   
                if(fs_reset_cntr == 0) /* Set corrections*/
                {

                    for( int i=0; i<ch_no; i++)
                    {
                        rtl_rec = &rtl_receivers[i];
                        if (rtlsdr_set_sample_freq_correction_f(rtl_rec->dev, new_fs_corrections[i]) !=0)
                            {log_error("Failed to set new sampling frequency correction, value: %s", strerror(errno));}                        
                        else{log_info("Sampling frequency correction set at ch: %d, value %.8f",i, new_fs_corrections[i]);}
                    }

                }
                else if(fs_reset_cntr == 1)
                {
                    for( int i=0; i<ch_no; i++) /* Tuning stage has been completed - reset corrections*/
                    {
                        rtl_rec = &rtl_receivers[i];
                        if(fabs(new_fs_corrections[i]) < FS_CORRECTION_KEEP_LIMIT) // Keep it running if the correction required is huge
                        {
                           if (rtlsdr_set_sample_freq_correction_f(rtl_rec->dev, 0) !=0)
                               {log_error("Failed to set new sampling frequency correction, value: %s", strerror(errno));}                        
                           else{log_info("Sampling frequency correction set at ch: %d, value %.8f",i, 0);}
                        }
                    }
                    fs_correction_flag=0;
                }
                fs_reset_cntr ++;
            }      
            /* Noise source switch request */
            if (last_noise_source_state != noise_source_state && config.en_noise_source_ctr==1)
            {
                rtl_rec = &rtl_receivers[ctr_channel_index];                
                if (noise_source_state == 1){
	            rtlsdr_set_bias_tee_gpio(rtl_rec->dev, 0, 1);

                    // Use pigpio to set Pi GPIO for third party Kerberos CKOVAL switches
                    #ifdef USEPIGPIO
                    if (ant_gpio_enabled)
                    {
                        // Maintain GPIO state, as we may change to ant 2 in the Python code
                        gpio_23 = gpioRead(ant_gpio_a);
                        gpio_24 = gpioRead(ant_gpio_b);

                        // Disconnect antennas
                        gpioWrite(ant_gpio_a, 0); /* enable antenna input 1 by default */
                        gpioWrite(ant_gpio_b, 0); /* disable antenna input 2 */
                    }
                    #endif
 
                    //rtlsdr_set_gpio(rtl_rec->dev, 1, 0);
                    log_info("Noise source turned on ");
                }
                else if (noise_source_state == 0){
                    rtlsdr_set_bias_tee_gpio(rtl_rec->dev, 0, 0);
                    //rtlsdr_set_bias_tee_gpio(rtl_rec->dev, 0, 1);
                    //rtlsdr_set_gpio(rtl_rec->dev, 0, 0);
                    log_info("Noise source turned off ");
                    // Use pigpio to set Pi GPIO for third party Kerberos CKOVAL switches
                    #ifdef USEPIGPIO
                    // PIGPIO
                    if (ant_gpio_enabled)
                    {
                        gpioWrite(ant_gpio_a, gpio_23); /* enable antenna input 1 by default */
                        gpioWrite(ant_gpio_b, gpio_24); /* disable antenna input 2 */
                    }
                    #endif
                }
                /*
                Currently the bias tee (noise source) has to be enabled in all Kerberos SDRs
                if there are multiple in the system. This hardware issue will be resolved in later versions.
                If you are using "older" (version < 2.0) Kerberos SDRs, uncomment this section to properly 
                control the noise source.
                */
                /*
                if(ch_no>4)
                {
                    log_warn("Noise source is controlled on the second Kerberos SDR as well");
                    struct rtl_rec_struct* rtl_rec_aux=&rtl_receivers[7];
                    if(noise_source_state == 1)
                        rtlsdr_set_gpio(rtl_rec_aux->dev, 1, 0);
                    else if(noise_source_state == 0)
                        rtlsdr_set_gpio(rtl_rec_aux->dev, 0, 0);
                }
                */
            }
            last_noise_source_state = noise_source_state;
            pthread_mutex_lock(&buff_ind_mutex);
        }
    }
    /* Release the mutex before joining the reader threads - in-flight USB
     * callbacks block on it and would deadlock the joins otherwise. */
    pthread_mutex_unlock(&buff_ind_mutex);
    log_info("Exiting..");
    for(int i=0; i<ch_no; i++)
    {
        struct rtl_rec_struct *rtl_rec = &rtl_receivers[i];
        if(rtlsdr_cancel_async(rtl_rec->dev) != 0)
        {
            log_fatal("Async read stop failed: %s", strerror(errno));
            return -1;
        }
        pthread_join(rtl_rec->async_read_thread, NULL);
        free(rtl_rec->buffer);
        if(rtlsdr_close(rtl_rec->dev) != 0)
            {log_warn("Device close failed, channel: %d: %s", i, strerror(errno));}
        else
            {log_info("Device closed, channel: %d", i);}
    }
    pthread_join(fifo_read_thread, NULL);
    free(iq_header);
    free(new_gains);
    free(new_fs_corrections);
    free(new_bias_states);
    free(rtl_receivers);
    free((char*)config.hw_name);
    free(config.en_bias_tee_str);
    free(config.device_serials);
    pthread_mutex_destroy(&buff_ind_mutex);
    pthread_cond_destroy(&buff_ind_cond);
    log_info("All the resources are free now");

    #ifdef USEPIGPIO
    if (ant_gpio_enabled)
        gpioTerminate();
    #endif

    return 0;
}

