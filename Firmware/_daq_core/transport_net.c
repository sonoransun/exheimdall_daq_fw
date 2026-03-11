/*
 *
 * Description :
 * Network/TCP transport driver for remote IQ data streaming.
 * Implements the transport_ops interface over TCP sockets.
 * Producer operates as a TCP server; consumer connects as a client.
 * Frame protocol: [LEN_32][IQ_HEADER_1024][PAYLOAD]
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
#include <fcntl.h>
#include <errno.h>

#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <netdb.h>

#include "log.h"
#include "transport.h"

/*
 *-------------------------------------
 *  Constants
 *-------------------------------------
 */
#define NET_DEFAULT_PORT    5100
#define NET_BACKLOG         1
#define NET_RECV_TIMEOUT_S  10

/*
 *-------------------------------------
 *  Private state
 *-------------------------------------
 */

struct net_transport_priv {
    int sockfd;
    int clientfd;           /* For producer (server mode): accepted client */
    char host[256];
    int port;
    void *buf[2];
    int active_buf;
    bool is_server;         /* true for producer, false for consumer */
    bool terminated;
};

/*
 *-------------------------------------
 *  Socket helpers
 *-------------------------------------
 */

static int send_all(int fd, const void *data, size_t len)
{
    const uint8_t *p = (const uint8_t *)data;
    size_t sent = 0;
    while (sent < len) {
        ssize_t ret = send(fd, p + sent, len - sent, MSG_NOSIGNAL);
        if (ret < 0) {
            if (errno == EINTR)
                continue;
            log_error("NET send failed: %s", strerror(errno));
            return -1;
        }
        sent += ret;
    }
    return 0;
}

static int recv_all(int fd, void *data, size_t len)
{
    uint8_t *p = (uint8_t *)data;
    size_t received = 0;
    while (received < len) {
        ssize_t ret = recv(fd, p + received, len - received, 0);
        if (ret < 0) {
            if (errno == EINTR)
                continue;
            log_error("NET recv failed: %s", strerror(errno));
            return -1;
        }
        if (ret == 0) {
            log_info("NET peer disconnected");
            return -1;
        }
        received += ret;
    }
    return 0;
}

/*
 *-------------------------------------
 *  Parse host:port from channel_name
 *  Format: "host:port" or just "port"
 *-------------------------------------
 */

static void parse_host_port(const char *name, char *host, size_t host_size, int *port)
{
    strncpy(host, "0.0.0.0", host_size);
    *port = NET_DEFAULT_PORT;

    if (!name || name[0] == '\0')
        return;

    const char *colon = strchr(name, ':');
    if (colon) {
        size_t hlen = (size_t)(colon - name);
        if (hlen >= host_size)
            hlen = host_size - 1;
        strncpy(host, name, hlen);
        host[hlen] = '\0';
        *port = atoi(colon + 1);
    } else {
        *port = atoi(name);
    }

    if (*port <= 0)
        *port = NET_DEFAULT_PORT;
}

/*
 *-------------------------------------
 *  transport_ops implementation
 *-------------------------------------
 */

int net_init_producer(struct transport_handle *th)
{
    log_info("NET transport: initializing producer (server mode)");

    struct net_transport_priv *priv = calloc(1, sizeof(struct net_transport_priv));
    if (!priv) {
        log_fatal("NET transport: allocation failed");
        return -1;
    }

    priv->sockfd = -1;
    priv->clientfd = -1;
    priv->active_buf = 0;
    priv->is_server = true;
    priv->terminated = false;

    parse_host_port(th->channel_name, priv->host, sizeof(priv->host), &priv->port);

    th->priv = priv;

    /* Allocate double buffers */
    for (int i = 0; i < 2; i++) {
        if (posix_memalign(&priv->buf[i], 4096, th->buffer_size) != 0) {
            log_fatal("NET buffer allocation failed");
            return -1;
        }
        memset(priv->buf[i], 0, th->buffer_size);
    }

    /* Create server socket (follows eth_server.h pattern) */
    priv->sockfd = socket(AF_INET, SOCK_STREAM, 0);
    if (priv->sockfd < 0) {
        log_fatal("NET server socket creation failed: %s", strerror(errno));
        return -1;
    }

    int opt = 1;
    if (setsockopt(priv->sockfd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
        log_error("NET setsockopt SO_REUSEADDR failed: %s", strerror(errno));
    }

    struct sockaddr_in server_addr;
    memset(&server_addr, 0, sizeof(server_addr));
    server_addr.sin_family = AF_INET;
    server_addr.sin_port = htons(priv->port);
    server_addr.sin_addr.s_addr = INADDR_ANY;

    if (bind(priv->sockfd, (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0) {
        log_fatal("NET bind failed on port %d: %s", priv->port, strerror(errno));
        close(priv->sockfd);
        return -1;
    }

    if (listen(priv->sockfd, NET_BACKLOG) < 0) {
        log_fatal("NET listen failed: %s", strerror(errno));
        close(priv->sockfd);
        return -1;
    }

    log_info("NET server listening on port %d", priv->port);

    /* Accept one connection (blocking) */
    struct sockaddr_in client_addr;
    socklen_t addr_len = sizeof(client_addr);
    priv->clientfd = accept(priv->sockfd, (struct sockaddr *)&client_addr, &addr_len);
    if (priv->clientfd < 0) {
        log_error("NET accept failed: %s", strerror(errno));
        close(priv->sockfd);
        return -1;
    }

    /* Set TCP_NODELAY for low latency */
    opt = 1;
    setsockopt(priv->clientfd, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));

    log_info("NET client connected from %s:%d",
             inet_ntoa(client_addr.sin_addr), ntohs(client_addr.sin_port));

    return 0;
}

int net_init_consumer(struct transport_handle *th)
{
    log_info("NET transport: initializing consumer (client mode)");

    struct net_transport_priv *priv = calloc(1, sizeof(struct net_transport_priv));
    if (!priv) {
        log_fatal("NET transport: allocation failed");
        return -1;
    }

    priv->sockfd = -1;
    priv->clientfd = -1;
    priv->active_buf = 0;
    priv->is_server = false;
    priv->terminated = false;

    parse_host_port(th->channel_name, priv->host, sizeof(priv->host), &priv->port);

    th->priv = priv;

    /* Allocate double buffers */
    for (int i = 0; i < 2; i++) {
        if (posix_memalign(&priv->buf[i], 4096, th->buffer_size) != 0) {
            log_fatal("NET buffer allocation failed");
            return -1;
        }
        memset(priv->buf[i], 0, th->buffer_size);
    }

    /* Create client socket and connect */
    priv->sockfd = socket(AF_INET, SOCK_STREAM, 0);
    if (priv->sockfd < 0) {
        log_fatal("NET client socket creation failed: %s", strerror(errno));
        return -1;
    }

    struct sockaddr_in server_addr;
    memset(&server_addr, 0, sizeof(server_addr));
    server_addr.sin_family = AF_INET;
    server_addr.sin_port = htons(priv->port);

    if (inet_pton(AF_INET, priv->host, &server_addr.sin_addr) <= 0) {
        /* Try hostname resolution */
        struct hostent *he = gethostbyname(priv->host);
        if (!he) {
            log_fatal("NET cannot resolve host: %s", priv->host);
            close(priv->sockfd);
            return -1;
        }
        memcpy(&server_addr.sin_addr, he->h_addr_list[0], he->h_length);
    }

    log_info("NET connecting to %s:%d", priv->host, priv->port);

    if (connect(priv->sockfd, (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0) {
        log_error("NET connect failed: %s", strerror(errno));
        close(priv->sockfd);
        return -1;
    }

    /* Set TCP_NODELAY */
    int opt = 1;
    setsockopt(priv->sockfd, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));

    /* Set receive timeout */
    struct timeval tv;
    tv.tv_sec = NET_RECV_TIMEOUT_S;
    tv.tv_usec = 0;
    setsockopt(priv->sockfd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    /* The consumer uses sockfd directly (no accept needed) */
    priv->clientfd = priv->sockfd;

    log_info("NET connected to %s:%d", priv->host, priv->port);
    return 0;
}

void net_destroy(struct transport_handle *th)
{
    if (!th || !th->priv)
        return;

    struct net_transport_priv *priv = (struct net_transport_priv *)th->priv;
    priv->terminated = true;

    if (priv->is_server) {
        if (priv->clientfd >= 0)
            close(priv->clientfd);
        if (priv->sockfd >= 0)
            close(priv->sockfd);
    } else {
        if (priv->sockfd >= 0)
            close(priv->sockfd);
    }

    for (int i = 0; i < 2; i++)
        free(priv->buf[i]);

    free(priv);
    th->priv = NULL;
    log_info("NET transport: destroyed");
}

int net_get_write_buf(struct transport_handle *th, void **buf_ptr)
{
    struct net_transport_priv *priv = (struct net_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

    int idx = priv->active_buf ^ 1;
    *buf_ptr = priv->buf[idx];
    return idx;
}

int net_submit_write(struct transport_handle *th, int buf_index)
{
    struct net_transport_priv *priv = (struct net_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

    int fd = priv->is_server ? priv->clientfd : priv->sockfd;
    uint32_t len = (uint32_t)th->buffer_size;

    /* Send frame: [LEN_32][PAYLOAD] */
    if (send_all(fd, &len, sizeof(len)) != 0) {
        log_error("NET send length failed");
        return -1;
    }
    if (send_all(fd, priv->buf[buf_index], len) != 0) {
        log_error("NET send payload failed");
        return -1;
    }

    priv->active_buf = buf_index;
    th->total_bytes += len;
    th->total_frames++;
    return 0;
}

int net_get_read_buf(struct transport_handle *th, void **buf_ptr)
{
    struct net_transport_priv *priv = (struct net_transport_priv *)th->priv;
    if (priv->terminated)
        return -1;

    int fd = priv->is_server ? priv->clientfd : priv->sockfd;
    int idx = priv->active_buf;

    /* Receive frame: [LEN_32][PAYLOAD] */
    uint32_t len;
    if (recv_all(fd, &len, sizeof(len)) != 0) {
        log_error("NET recv length failed");
        return -1;
    }

    if (len > th->buffer_size) {
        log_error("NET frame too large: %u > %zu", len, th->buffer_size);
        return -1;
    }

    if (recv_all(fd, priv->buf[idx], len) != 0) {
        log_error("NET recv payload failed");
        return -1;
    }

    *buf_ptr = priv->buf[idx];
    return idx;
}

int net_release_read(struct transport_handle *th, int buf_index)
{
    struct net_transport_priv *priv = (struct net_transport_priv *)th->priv;
    (void)buf_index;
    priv->active_buf ^= 1;
    return 0;
}

void net_send_terminate(struct transport_handle *th)
{
    struct net_transport_priv *priv = (struct net_transport_priv *)th->priv;
    priv->terminated = true;

    /* Send a zero-length frame to signal termination */
    int fd = priv->is_server ? priv->clientfd : priv->sockfd;
    uint32_t len = 0;
    (void)send_all(fd, &len, sizeof(len));

    log_info("NET transport: terminate signaled");
}

/*
 *-------------------------------------
 *  Exported ops table
 *-------------------------------------
 */

static const struct transport_ops net_transport_ops = {
    .init_producer  = net_init_producer,
    .init_consumer  = net_init_consumer,
    .destroy        = net_destroy,
    .get_write_buf  = net_get_write_buf,
    .submit_write   = net_submit_write,
    .get_read_buf   = net_get_read_buf,
    .release_read   = net_release_read,
    .send_terminate = net_send_terminate,
};

const struct transport_ops* transport_net_get_ops(void)
{
    return &net_transport_ops;
}
