/*
 * 
 * Description :
 * IQ frame Ethernet server
 * 
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 * Author  : Tamas Peto
 * 
 * Copyright (C) 2018-2020  Tamás Pető
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
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <signal.h>
#include <time.h>
#include <netinet/tcp.h>
#include <sys/uio.h>
#include <sys/mman.h>

#include "eth_server.h"
#include "ini.h"
#include "log.h"
#include "sh_mem_util.h"
#include "iq_header.h"
#include "rtl_daq.h"
#include "transport.h"
#define INI_FNAME "daq_chain_config.ini"

static volatile sig_atomic_t sig_exit_flag = 0;

static void shutdown_handler(int sig)
{
    (void)sig;
    sig_exit_flag = 1;
}

static void install_signal_handlers(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = shutdown_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT,  &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(64,      &sa, NULL);
}

#define FATAL_ERR(l) log_fatal(l); return -1;

/*
 * This structure stores the configuration parameters, 
 * that are loaded from the ini file
 */ 
typedef struct
{
    int num_ch;
    int cpi_size;
    int log_level;
    int instance_id;
    int port_stride;
    char listen_address[64];
} configuration;

/*
 * Ini configuration parser callback function  
*/
static int handler(void* conf_struct, const char* section, const char* name,
                   const char* value)

{
    configuration* pconfig = (configuration*) conf_struct;

    #define MATCH(s, n) strcmp(section, s) == 0 && strcmp(name, n) == 0
    if (MATCH("hw", "num_ch")) 
    {
        pconfig->num_ch = atoi(value);
    } 
    else if (MATCH("pre_processing", "cpi_size")) 
    {
        pconfig->cpi_size = atoi(value);
    }
    else if (MATCH("daq", "log_level"))
    {
        pconfig->log_level = atoi(value);
    }
    else if (MATCH("federation", "instance_id"))
    {
        pconfig->instance_id = atoi(value);
    }
    else if (MATCH("federation", "port_stride"))
    {
        pconfig->port_stride = atoi(value);
    }
    else if (MATCH("daq", "listen_address"))
    {
        strncpy(pconfig->listen_address, value, sizeof(pconfig->listen_address)-1);
        pconfig->listen_address[sizeof(pconfig->listen_address)-1] = '\0';
    }
    else {
        return 1;  /* unknown section/name: accepted, non-fatal */
    }
    return 1;
}

#define SEND_FRAME_DEADLINE_S 10 /* Upper bound on the total send time of one IQ frame */

static int send_iq_frame(struct iq_frame_struct_32* iq_frame, int socket)
{
    /* Header and payload are coalesced into a single sendmsg call, and short
     * writes / EINTR are resumed instead of tearing down the connection.
     * A send that stops making progress entirely still fails the session via
     * SO_SNDTIMEO, and because that timeout restarts on every partial send,
     * a trickle-reading consumer is additionally bounded by a per-frame
     * deadline (SEND_FRAME_DEADLINE_S) so it cannot hold the single client
     * slot indefinitely. The bytes on the wire are identical to the legacy
     * two-send implementation. */
    struct iovec iov[2];
    iov[0].iov_base = iq_frame->header;
    iov[0].iov_len  = IQ_HEADER_LENGTH;
    iov[1].iov_base = iq_frame->payload;
    iov[1].iov_len  = (size_t)iq_frame->payload_size * sizeof(*iq_frame->payload) * 2;

    struct iovec* cur = iov;
    int iov_cnt = (iov[1].iov_len != 0) ? 2 : 1;

    struct timespec send_deadline;
    clock_gettime(CLOCK_MONOTONIC, &send_deadline);
    send_deadline.tv_sec += SEND_FRAME_DEADLINE_S;

    while (iov_cnt > 0)
    {
        struct msghdr msg;
        memset(&msg, 0, sizeof(msg));
        msg.msg_iov = cur;
        msg.msg_iovlen = iov_cnt;

        ssize_t sent = sendmsg(socket, &msg, MSG_NOSIGNAL);
        int send_errno = errno; /* preserved across the clock_gettime below */

        /* Bound the total per-frame send time even when the consumer keeps
         * making (slow) progress. */
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec > send_deadline.tv_sec ||
            (now.tv_sec == send_deadline.tv_sec && now.tv_nsec >= send_deadline.tv_nsec))
        {
            log_error("IQ frame send exceeded %d s deadline: dropping slow consumer", SEND_FRAME_DEADLINE_S);
            return -1;
        }

        if (sent < 0)
        {
            if (send_errno == EINTR)
                continue;
            log_error("IQ frame send failed: %s", strerror(send_errno));
            return -1;
        }
        while (sent > 0 && iov_cnt > 0)
        {
            if ((size_t)sent >= cur->iov_len)
            {
                sent -= (ssize_t)cur->iov_len;
                cur++;
                iov_cnt--;
            }
            else
            {
                cur->iov_base = (uint8_t*)cur->iov_base + sent;
                cur->iov_len -= (size_t)sent;
                sent = 0;
            }
        }
    }
    return 0;
}

int main(int argc, char* argv[])
{
    log_set_level(LOG_TRACE);
    install_signal_handlers();
    configuration config;
    memset(&config, 0, sizeof(config));
    config.instance_id = 0;
    config.port_stride = 100;
    strcpy(config.listen_address, "0.0.0.0");
    int ret = 0;
    int active_buff_ind;
    char eth_cmd[1024];

    /* Set parameters from the config file*/
    int ini_status = ini_parse(INI_FNAME, handler, &config);
    if (ini_status < 0)
    {FATAL_ERR("Configuration could not be loaded, exiting ..")}
    if (ini_status > 0)
    {log_warn("Config file %s has a parse error at line %d", INI_FNAME, ini_status);}

    log_set_level(config.log_level);

    /* Best effort: lock pages to avoid faults in the streaming path.
     * MCL_ONFAULT is required with MCL_FUTURE: the input transport below maps
     * worst-case-sized (MAX_IQFRAME_PAYLOAD_SIZE) shm segments, and a plain
     * MCL_FUTURE would fault-populate and pin every page of them at mmap
     * time (hundreds of MiB of tmpfs, fatal on small-RAM targets). With
     * MCL_ONFAULT only pages actually touched get locked. */
#ifdef MCL_ONFAULT
    if (mlockall(MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT) != 0)
        log_warn("mlockall failed: %s", strerror(errno));
#else
    /* No MCL_ONFAULT on this libc: never use bare MCL_FUTURE (it would
     * pre-fault the worst-case shm segments); lock current pages only. */
    if (mlockall(MCL_CURRENT) != 0)
        log_warn("mlockall failed: %s", strerror(errno));
#endif

    struct iq_frame_struct_32* iq_frame = calloc(1, sizeof(struct iq_frame_struct_32));

    /* Initializing input transport interface */
    size_t input_buf_size = MAX_IQFRAME_PAYLOAD_SIZE*config.num_ch*4*2+IQ_HEADER_LENGTH;
    struct transport_handle* input_transport = transport_create(
        "delay_sync_iq", input_buf_size, false,
        FLOW_BACKPRESSURE, config.instance_id, TRANSPORT_SHM);
    if (!input_transport) {FATAL_ERR("Failed to create input transport")}

    ret = transport_init(input_transport);
    if (ret != 0) {FATAL_ERR("Failed to init transport interface")}
    else {log_info("Transport interface succesfully initialized");}

    /* Starting IQ ethernet server. The listening socket is created once and
     * survives across client sessions, so clients reconnecting between
     * sessions are not refused. */
    int listen_sock = iq_stream_listen(
        compute_port(5000, config.instance_id, config.port_stride),
        config.listen_address);
    if (listen_sock < 0) {FATAL_ERR("Failed to open the IQ server listening socket")}

    int term_flag = 0;
    while(!sig_exit_flag && !term_flag)
    {
        /* This function blocks until a client connects to the server */
        int client_sock = iq_stream_accept(listen_sock);
        if (client_sock < 0)
        {
            if (sig_exit_flag) break;
            log_error("Client connection failed, retrying..");
            sleep(1);
            continue;
        }

        /* Set send timeout so slow consumers don't block the server */
        struct timeval snd_tv = { .tv_sec = 2, .tv_usec = 0 };
        setsockopt(client_sock, SOL_SOCKET, SO_SNDTIMEO, &snd_tv, sizeof(snd_tv));
        struct timeval rcv_tv = { .tv_sec = 5, .tv_usec = 0 };
        setsockopt(client_sock, SOL_SOCKET, SO_RCVTIMEO, &rcv_tv, sizeof(rcv_tv));
        /* The port 5000 protocol is stop-and-wait (per-frame "IQDownload"
         * ack), so disable Nagle to avoid delayed-ACK stalls */
        int nodelay = 1;
        setsockopt(client_sock, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

        int exit_flag = 0;
        while(!exit_flag && !sig_exit_flag)
        {
            void* buf_ptr;
            active_buff_ind = transport_get_read_buf(input_transport, &buf_ptr);
            if (active_buff_ind < 0) { exit_flag = active_buff_ind; break; }
            if (active_buff_ind == TERMINATE || buf_ptr == NULL)
            {
                log_info("Terminate signal received from the DAQ chain");
                term_flag = 1;
                break;
            }
            iq_frame->header = (struct iq_header_struct*) buf_ptr;
            iq_frame->payload = ((float *) buf_ptr) + IQ_HEADER_LENGTH/sizeof(float);
            CHK_SYNC_WORD(check_sync_word(iq_frame->header));
            iq_frame->payload_size = iq_frame->header->cpi_length * iq_frame->header->active_ant_chs;

            ret = send_iq_frame(iq_frame, client_sock);
            transport_release_read(input_transport, active_buff_ind);
            if (ret != 0) { log_error("Closing connection"); break; }

            int bytes_recieved = recv(client_sock, eth_cmd, sizeof(eth_cmd)-1, 0);
            if (bytes_recieved <= 0) { exit_flag = 1; break; }
            eth_cmd[bytes_recieved] = '\0';
            if (strcmp(eth_cmd, "IQDownload") != 0) { exit_flag = 1; }
        }
        close(client_sock);
    }
    close(listen_sock);
    if (sig_exit_flag)
        log_info("Received shutdown signal");
    transport_destroy(input_transport);
    free(input_transport);
    free(iq_frame);
    log_info("DAQ chain IQ server has exited.");
    return 0;
}
