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
#include <errno.h>
#include <poll.h>
#include <pthread.h>
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
#include "frame_sizes.h"
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
    int corr_size;                 /* [calibration], for size_segments_from_config */
    int size_segments_from_config; /* [pre_processing], 0 = map the worst case */
    int iq_server_max_clients;     /* [data_interface], 1 = legacy single-client loop */
    int iq_server_frame_deadline_ms; /* [data_interface], 0 = auto (1.0 x frame period) */
    int decimation_ratio;          /* [pre_processing], auto deadline only */
    double sample_rate;            /* [daq], auto deadline only */
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
    else if (MATCH("calibration", "corr_size"))
    {
        pconfig->corr_size = atoi(value);
    }
    else if (MATCH("pre_processing", "size_segments_from_config"))
    {
        pconfig->size_segments_from_config = atoi(value);
    }
    else if (MATCH("data_interface", "iq_server_max_clients"))
    {
        pconfig->iq_server_max_clients = atoi(value);
    }
    else if (MATCH("data_interface", "iq_server_frame_deadline_ms"))
    {
        pconfig->iq_server_frame_deadline_ms = atoi(value);
    }
    else if (MATCH("pre_processing", "decimation_ratio"))
    {
        pconfig->decimation_ratio = atoi(value);
    }
    else if (MATCH("daq", "sample_rate"))
    {
        pconfig->sample_rate = strtod(value, NULL);
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

/*
 *-------------------------------------------------------------------------
 *  Multi-client server ([data_interface] iq_server_max_clients > 1)
 *-------------------------------------------------------------------------
 *
 * The default (iq_server_max_clients = 1) keeps the single-client loop in
 * main() unchanged. With a larger cap, serve_multi() runs instead:
 *   - an accept thread performs the "streaming" handshake and adds sessions
 *     up to the cap (further clients wait in the listen backlog, exactly as
 *     a second client waits behind the single-client loop);
 *   - the main thread reads a frame only while at least one session exists,
 *     hands it to every session's sender thread, waits until each one has
 *     sent the frame and read its "IQDownload" ack, or until the frame
 *     deadline, closes the stragglers, and only then releases the buffer;
 *   - each session receives byte-for-byte what the single-client loop sends
 *     (the same send_iq_frame(), socket options and ack check).
 */

#define IQ_MULTI_MAX_CLIENTS         64   /* upper clamp of iq_server_max_clients */
#define IQ_MULTI_MIN_AUTO_DEADLINE_MS 100 /* floor of the automatic frame deadline */
#define IQ_MULTI_HANDSHAKE_TIMEOUT_S 5    /* a connected client must send "streaming" within */
#define IQ_MULTI_POLL_MS             200  /* stop/exit flag polling period */

struct iq_session
{
    struct iq_multi_server* srv;
    pthread_t thread;
    int  used;        /* slot holds a session (thread created) */
    int  fd;
    int  busy;        /* a frame has been handed over and is not finished yet */
    int  failed;      /* session must be closed (error, bad ack or deadline) */
    char peer[64];
};

struct iq_multi_server
{
    pthread_mutex_t lock;
    pthread_cond_t  job_cv;   /* main -> senders: frame handed over / stop */
    pthread_cond_t  done_cv;  /* senders -> main: frame finished; accept -> main: new session */
    pthread_cond_t  slot_cv;  /* main -> accept: a slot was freed / stop */
    int stop;
    int max_clients;
    int n_used;               /* slots in use, including failed sessions not reaped yet */
    int listen_sock;
    struct iq_frame_struct_32 frame; /* frame currently handed to the senders */
    struct iq_session sessions[IQ_MULTI_MAX_CLIENTS];
};

/* Reads the per-frame ack exactly as the single-client loop does */
static int recv_iq_ack(int socket)
{
    char eth_cmd[1024];
    ssize_t bytes_recieved = recv(socket, eth_cmd, sizeof(eth_cmd)-1, 0);
    if (bytes_recieved <= 0) return -1;
    eth_cmd[bytes_recieved] = '\0';
    if (strcmp(eth_cmd, "IQDownload") != 0) return -1;
    return 0;
}

/* 1 when a is strictly earlier than b */
static int timespec_before(const struct timespec* a, const struct timespec* b)
{
    return a->tv_sec < b->tv_sec || (a->tv_sec == b->tv_sec && a->tv_nsec < b->tv_nsec);
}

static void deadline_after_ms(struct timespec* ts, long ms)
{
    clock_gettime(CLOCK_MONOTONIC, ts);
    ts->tv_sec  += ms / 1000;
    ts->tv_nsec += (ms % 1000) * 1000000L;
    if (ts->tv_nsec >= 1000000000L)
    {
        ts->tv_sec  += 1;
        ts->tv_nsec -= 1000000000L;
    }
}

static int multi_active_sessions(const struct iq_multi_server* srv)
{
    int n = 0;
    for (int i = 0; i < srv->max_clients; i++)
        if (srv->sessions[i].used && !srv->sessions[i].failed) n++;
    return n;
}

static int multi_busy_sessions(const struct iq_multi_server* srv)
{
    int n = 0;
    for (int i = 0; i < srv->max_clients; i++)
        if (srv->sessions[i].used && srv->sessions[i].busy) n++;
    return n;
}

static void* iq_session_thread(void* arg)
{
    struct iq_session* ses = (struct iq_session*) arg;
    struct iq_multi_server* srv = ses->srv;

    pthread_mutex_lock(&srv->lock);
    while (!srv->stop && !ses->failed)
    {
        if (!ses->busy)
        {
            pthread_cond_wait(&srv->job_cv, &srv->lock);
            continue;
        }
        struct iq_frame_struct_32 frame = srv->frame;
        pthread_mutex_unlock(&srv->lock);

        int ret = send_iq_frame(&frame, ses->fd);
        if (ret == 0) ret = recv_iq_ack(ses->fd);

        pthread_mutex_lock(&srv->lock);
        ses->busy = 0;
        if (ret != 0) ses->failed = 1;
        pthread_cond_broadcast(&srv->done_cv);
    }
    ses->busy = 0;
    pthread_cond_broadcast(&srv->done_cv);
    pthread_mutex_unlock(&srv->lock);
    return NULL;
}

/* Accepts one connection and performs the "streaming" handshake
 * (eth_server.h iq_stream_accept protocol, with a handshake timeout so a
 * silent client cannot block further accepts). Returns the client socket or -1. */
static int multi_accept_client(int listen_sock, char* peer, size_t peer_len)
{
    struct sockaddr_in client_addr;
    socklen_t sin_size = sizeof(client_addr);
    int fd = accept(listen_sock, (struct sockaddr *)&client_addr, &sin_size);
    if (fd < 0)
    {
        if (errno != EINTR && errno != EAGAIN && errno != ECONNABORTED)
            log_error("Accepting client connection failed: %s", strerror(errno));
        return -1;
    }
    snprintf(peer, peer_len, "%s:%d", inet_ntoa(client_addr.sin_addr), ntohs(client_addr.sin_port));
    log_info("New connection from %s", peer);

    struct timeval hs_tv = { .tv_sec = IQ_MULTI_HANDSHAKE_TIMEOUT_S, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &hs_tv, sizeof(hs_tv));
    char recv_data[1024];
    ssize_t bytes_recieved = recv(fd, recv_data, sizeof(recv_data)-1, 0);
    if (bytes_recieved <= 0)
    {
        log_error("Connection from %s closed before the streaming request", peer);
        close(fd);
        return -1;
    }
    recv_data[bytes_recieved] = '\0';
    if (strcmp(recv_data, "streaming") != 0)
    {
        log_error("Unexpected request from %s, closing connection", peer);
        close(fd);
        return -1;
    }
    log_info("Streaming request received from %s", peer);

    /* Same per-session socket options as the single-client loop */
    struct timeval snd_tv = { .tv_sec = 2, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &snd_tv, sizeof(snd_tv));
    struct timeval rcv_tv = { .tv_sec = 5, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &rcv_tv, sizeof(rcv_tv));
    int nodelay = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
    return fd;
}

static void* iq_accept_thread(void* arg)
{
    struct iq_multi_server* srv = (struct iq_multi_server*) arg;
    for (;;)
    {
        pthread_mutex_lock(&srv->lock);
        while (!srv->stop && srv->n_used >= srv->max_clients)
            pthread_cond_wait(&srv->slot_cv, &srv->lock);
        int stop = srv->stop;
        pthread_mutex_unlock(&srv->lock);
        if (stop) break;

        struct pollfd pfd = { .fd = srv->listen_sock, .events = POLLIN, .revents = 0 };
        int pr = poll(&pfd, 1, IQ_MULTI_POLL_MS);
        if (pr <= 0) continue; /* timeout or EINTR: re-check the stop flag */

        char peer[64];
        int fd = multi_accept_client(srv->listen_sock, peer, sizeof(peer));
        if (fd < 0) continue;

        pthread_mutex_lock(&srv->lock);
        struct iq_session* ses = NULL;
        for (int i = 0; i < srv->max_clients && ses == NULL; i++)
            if (!srv->sessions[i].used) ses = &srv->sessions[i];
        if (srv->stop || ses == NULL)
        {
            pthread_mutex_unlock(&srv->lock);
            close(fd);
            continue;
        }
        memset(ses, 0, sizeof(*ses));
        ses->srv = srv;
        ses->fd  = fd;
        snprintf(ses->peer, sizeof(ses->peer), "%s", peer);
        if (pthread_create(&ses->thread, NULL, iq_session_thread, ses) != 0)
        {
            pthread_mutex_unlock(&srv->lock);
            log_error("Failed to start the sender thread for %s", peer);
            close(fd);
            continue;
        }
        ses->used = 1;
        srv->n_used++;
        log_info("IQ client %s added (%d/%d sessions)", peer, srv->n_used, srv->max_clients);
        pthread_cond_broadcast(&srv->done_cv);
        pthread_mutex_unlock(&srv->lock);
    }
    return NULL;
}

/* Joins and frees every failed session. Called by the main thread with the
 * lock NOT held and no frame in flight. */
static void multi_reap_sessions(struct iq_multi_server* srv)
{
    for (int i = 0; i < srv->max_clients; i++)
    {
        struct iq_session* ses = &srv->sessions[i];
        pthread_mutex_lock(&srv->lock);
        int reap = ses->used && (ses->failed || srv->stop);
        if (reap)
        {
            ses->failed = 1;
            shutdown(ses->fd, SHUT_RDWR);
            pthread_cond_broadcast(&srv->job_cv);
        }
        pthread_mutex_unlock(&srv->lock);
        if (!reap) continue;

        pthread_join(ses->thread, NULL);
        close(ses->fd);
        log_info("IQ client %s closed", ses->peer);

        pthread_mutex_lock(&srv->lock);
        ses->used = 0;
        srv->n_used--;
        pthread_cond_broadcast(&srv->slot_cv);
        pthread_mutex_unlock(&srv->lock);
    }
}

/* Hands the frame to every live session and waits for all acks or the
 * deadline; stragglers are shut down. Returns with no sender touching the
 * frame, so the caller may release the buffer. */
static void multi_dispatch_frame(struct iq_multi_server* srv,
                                 const struct iq_frame_struct_32* frame,
                                 long deadline_ms)
{
    pthread_mutex_lock(&srv->lock);
    srv->frame = *frame;
    for (int i = 0; i < srv->max_clients; i++)
    {
        struct iq_session* ses = &srv->sessions[i];
        if (ses->used && !ses->failed) ses->busy = 1;
    }
    pthread_cond_broadcast(&srv->job_cv);

    /* Wait in IQ_MULTI_POLL_MS slices so a shutdown signal is honoured
     * without waiting out a long frame deadline */
    struct timespec deadline;
    deadline_after_ms(&deadline, deadline_ms);
    for (;;)
    {
        if (sig_exit_flag || multi_busy_sessions(srv) == 0) break;
        struct timespec now, slice;
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (!timespec_before(&now, &deadline)) break;
        deadline_after_ms(&slice, IQ_MULTI_POLL_MS);
        if (timespec_before(&deadline, &slice)) slice = deadline;
        pthread_cond_timedwait(&srv->done_cv, &srv->lock, &slice);
    }
    for (int i = 0; i < srv->max_clients; i++)
    {
        struct iq_session* ses = &srv->sessions[i];
        if (ses->used && ses->busy)
        {
            if (sig_exit_flag)
                log_info("IQ client %s: closing session on shutdown", ses->peer);
            else
                log_warn("IQ client %s missed the %ld ms frame deadline: closing session",
                         ses->peer, deadline_ms);
            ses->failed = 1;
            shutdown(ses->fd, SHUT_RDWR); /* unblocks its pending send/recv */
        }
    }
    /* After shutdown() every pending send/recv returns promptly (and is
     * bounded by SO_SNDTIMEO/SO_RCVTIMEO in any case) */
    while (multi_busy_sessions(srv) > 0)
        pthread_cond_wait(&srv->done_cv, &srv->lock);
    pthread_mutex_unlock(&srv->lock);
}

/* Frame deadline: explicit [data_interface] iq_server_frame_deadline_ms, or
 * 1.0 x the frame period cpi_size * decimation_ratio / sample_rate (floored
 * at IQ_MULTI_MIN_AUTO_DEADLINE_MS): a client that keeps up with the stream
 * rate stays connected (at defaults the frame period is ~437 ms, which a
 * GbE client can meet; a sub-period factor would drop every GbE client). Without a sample rate the single-client
 * per-frame send bound (SEND_FRAME_DEADLINE_S) is used. */
static long multi_frame_deadline_ms(const configuration* config)
{
    if (config->iq_server_frame_deadline_ms > 0)
        return config->iq_server_frame_deadline_ms;
    if (config->sample_rate <= 0.0 || config->cpi_size < 1)
        return SEND_FRAME_DEADLINE_S * 1000L;
    int dec = (config->decimation_ratio > 0) ? config->decimation_ratio : 1;
    double period_ms = (double)config->cpi_size * (double)dec * 1000.0 / config->sample_rate;
    long ms = (long)period_ms;
    return (ms < IQ_MULTI_MIN_AUTO_DEADLINE_MS) ? IQ_MULTI_MIN_AUTO_DEADLINE_MS : ms;
}

static int multi_cond_init_monotonic(pthread_cond_t* cv)
{
    pthread_condattr_t attr;
    if (pthread_condattr_init(&attr) != 0) return -1;
    int ret = pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
    if (ret == 0) ret = pthread_cond_init(cv, &attr);
    pthread_condattr_destroy(&attr);
    return ret;
}

/* serve_multi() results */
#define IQ_MULTI_DONE        0  /* TERMINATE from the chain or a shutdown signal */
#define IQ_MULTI_INIT_FAILED (-1)
#define IQ_MULTI_INPUT_ERROR (-2)

/* Runs the multi-client server until TERMINATE, a shutdown signal or an
 * input transport error. Returns one of the IQ_MULTI_* results. */
static int serve_multi(struct transport_handle* input_transport, int listen_sock,
                       const configuration* config)
{
    struct iq_multi_server* srv = calloc(1, sizeof(*srv));
    if (srv == NULL) {log_fatal("Malloc failed, exiting.."); return IQ_MULTI_INIT_FAILED;}
    log_use_default_lock(); /* several threads log from here on */
    srv->max_clients = config->iq_server_max_clients;
    srv->listen_sock = listen_sock;
    long deadline_ms = multi_frame_deadline_ms(config);

    /* Initialize the lock and condition variables in order; on a failure
     * destroy exactly the ones already initialized */
    int n_init = 0;
    if (pthread_mutex_init(&srv->lock, NULL) == 0) n_init++;
    if (n_init == 1 && multi_cond_init_monotonic(&srv->job_cv) == 0) n_init++;
    if (n_init == 2 && multi_cond_init_monotonic(&srv->done_cv) == 0) n_init++;
    if (n_init == 3 && multi_cond_init_monotonic(&srv->slot_cv) == 0) n_init++;
    if (n_init < 4)
    {
        log_fatal("Failed to initialize the multi-client server");
        if (n_init > 2) pthread_cond_destroy(&srv->done_cv);
        if (n_init > 1) pthread_cond_destroy(&srv->job_cv);
        if (n_init > 0) pthread_mutex_destroy(&srv->lock);
        free(srv);
        return IQ_MULTI_INIT_FAILED;
    }
    log_info("Multi-client IQ server: up to %d clients, frame deadline %ld ms",
             srv->max_clients, deadline_ms);

    /* Shutdown signals stay with the main thread, so they interrupt its
     * blocking FIFO read exactly as in the single-client loop */
    sigset_t block_set, old_set;
    sigemptyset(&block_set);
    sigaddset(&block_set, SIGINT);
    sigaddset(&block_set, SIGTERM);
    sigaddset(&block_set, 64);
    pthread_sigmask(SIG_BLOCK, &block_set, &old_set);
    pthread_t accept_thread;
    int accept_ok = pthread_create(&accept_thread, NULL, iq_accept_thread, srv) == 0;
    pthread_sigmask(SIG_SETMASK, &old_set, NULL);
    if (!accept_ok)
    {
        log_fatal("Failed to start the accept thread");
        pthread_cond_destroy(&srv->slot_cv);
        pthread_cond_destroy(&srv->done_cv);
        pthread_cond_destroy(&srv->job_cv);
        pthread_mutex_destroy(&srv->lock);
        free(srv);
        return IQ_MULTI_INIT_FAILED;
    }

    int ret = IQ_MULTI_DONE;
    struct iq_frame_struct_32 frame;
    memset(&frame, 0, sizeof(frame));
    while (!sig_exit_flag)
    {
        /* Read the next frame only while at least one session exists */
        pthread_mutex_lock(&srv->lock);
        while (!sig_exit_flag && multi_active_sessions(srv) == 0)
        {
            struct timespec wake;
            deadline_after_ms(&wake, IQ_MULTI_POLL_MS);
            pthread_cond_timedwait(&srv->done_cv, &srv->lock, &wake);
        }
        pthread_mutex_unlock(&srv->lock);
        if (sig_exit_flag) break;

        void* buf_ptr;
        int active_buff_ind = transport_get_read_buf(input_transport, &buf_ptr);
        if (active_buff_ind < 0)
        {
            /* A shutdown signal interrupts the blocking FIFO read */
            if (!sig_exit_flag)
            {
                log_error("Multi-client IQ server stopped on an input transport error (%d)",
                          active_buff_ind);
                ret = IQ_MULTI_INPUT_ERROR;
            }
            break;
        }
        if (active_buff_ind == TERMINATE || buf_ptr == NULL)
        {
            log_info("Terminate signal received from the DAQ chain");
            break;
        }
        frame.header  = (struct iq_header_struct*) buf_ptr;
        frame.payload = ((float *) buf_ptr) + IQ_HEADER_LENGTH/sizeof(float);
        if (check_sync_word(frame.header) != 0)
        {
            log_error("IQ frame sync word error: dropping the frame and closing every session");
            transport_release_read(input_transport, active_buff_ind);
            pthread_mutex_lock(&srv->lock);
            for (int i = 0; i < srv->max_clients; i++)
                if (srv->sessions[i].used) srv->sessions[i].failed = 1;
            pthread_mutex_unlock(&srv->lock);
            multi_reap_sessions(srv);
            continue;
        }
        frame.payload_size = frame.header->cpi_length * frame.header->active_ant_chs;

        multi_dispatch_frame(srv, &frame, deadline_ms);
        transport_release_read(input_transport, active_buff_ind);
        multi_reap_sessions(srv);
    }

    pthread_mutex_lock(&srv->lock);
    srv->stop = 1;
    pthread_cond_broadcast(&srv->job_cv);
    pthread_cond_broadcast(&srv->slot_cv);
    pthread_mutex_unlock(&srv->lock);
    pthread_join(accept_thread, NULL);
    multi_reap_sessions(srv);

    pthread_cond_destroy(&srv->job_cv);
    pthread_cond_destroy(&srv->done_cv);
    pthread_cond_destroy(&srv->slot_cv);
    pthread_mutex_destroy(&srv->lock);
    free(srv);
    return ret;
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

    /* Initializing input transport interface. The default maps the
     * 2^23-sample worst case; with [pre_processing] size_segments_from_config
     * = 1 it maps exactly the delay_sync_iq size delay_sync creates
     * (1024 + max(cpi_size, corr_size) * num_ch * 8). */
    size_t input_buf_size;
    if (config.size_segments_from_config)
    {
        if (config.num_ch < 1 || config.cpi_size < 1 || config.corr_size < 1)
        {FATAL_ERR("size_segments_from_config = 1 requires positive num_ch, cpi_size and corr_size")}
        input_buf_size = hd_iq_frame_bytes(config.num_ch, config.cpi_size, config.corr_size);
        log_info("delay_sync_iq mapping sized from config: %zu bytes", input_buf_size);
    }
    else
        input_buf_size = hd_worst_frame_bytes(config.num_ch);
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
    int exit_code = 0;
    if (config.iq_server_max_clients > 1)
    {
        /* Opt-in multi-client server; the single-client loop below is skipped */
        if (config.iq_server_max_clients > IQ_MULTI_MAX_CLIENTS)
        {
            log_warn("iq_server_max_clients = %d exceeds the limit, using %d",
                     config.iq_server_max_clients, IQ_MULTI_MAX_CLIENTS);
            config.iq_server_max_clients = IQ_MULTI_MAX_CLIENTS;
        }
        /* Nonzero exit status when the server could not start or stopped on
         * an input transport error; TERMINATE and shutdown signals exit 0 */
        if (serve_multi(input_transport, listen_sock, &config) != IQ_MULTI_DONE)
            exit_code = 1;
        term_flag = 1;
    }
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
    return exit_code;
}
