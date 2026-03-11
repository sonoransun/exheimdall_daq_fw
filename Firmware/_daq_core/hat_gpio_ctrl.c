/*
 *
 * Description :
 * GPIO HAT controller implementation with pluggable backends.
 * Supports sysfs (portable), pigpio (RPi-optimized), and libgpiod (modern).
 * Falls back gracefully when preferred backends are not available.
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
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <poll.h>
#include <time.h>

#include "log.h"
#include "hat_gpio_ctrl.h"

#ifdef USEPIGPIO
#include <pigpio.h>
#endif

#ifdef USELIBGPIOD
#include <gpiod.h>
#endif

/*
 *-------------------------------------
 *  Maximum tracked pins for sysfs
 *-------------------------------------
 */
#define MAX_GPIO_PINS 64

/*
 *=============================================
 *  SYSFS backend implementation
 *=============================================
 */

struct sysfs_ctx {
    int value_fds[MAX_GPIO_PINS];   /* Cached value file descriptors */
    bool exported[MAX_GPIO_PINS];
};

static int sysfs_export(int pin)
{
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d", pin);
    if (access(path, F_OK) == 0)
        return 0;

    int fd = open("/sys/class/gpio/export", O_WRONLY);
    if (fd < 0) {
        log_error("sysfs: export open failed: %s", strerror(errno));
        return GPIO_ERR_IO;
    }
    char buf[16];
    int len = snprintf(buf, sizeof(buf), "%d", pin);
    int ret = (write(fd, buf, len) == len) ? GPIO_OK : GPIO_ERR_IO;
    close(fd);
    if (ret != GPIO_OK)
        log_error("sysfs: export write failed for pin %d: %s", pin, strerror(errno));
    /* Allow udev to create device nodes */
    usleep(50000);
    return ret;
}

static int sysfs_set_direction(int pin, const char *dir)
{
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/direction", pin);
    int fd = open(path, O_WRONLY);
    if (fd < 0) {
        log_error("sysfs: direction open failed for pin %d: %s", pin, strerror(errno));
        return GPIO_ERR_IO;
    }
    int ret = (write(fd, dir, strlen(dir)) > 0) ? GPIO_OK : GPIO_ERR_IO;
    close(fd);
    return ret;
}

static int sysfs_set_edge(int pin, const char *edge)
{
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/edge", pin);
    int fd = open(path, O_WRONLY);
    if (fd < 0) {
        log_error("sysfs: edge open failed for pin %d: %s", pin, strerror(errno));
        return GPIO_ERR_IO;
    }
    int ret = (write(fd, edge, strlen(edge)) > 0) ? GPIO_OK : GPIO_ERR_IO;
    close(fd);
    return ret;
}

static int sysfs_open_value(int pin)
{
    char path[64];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/value", pin);
    return open(path, O_RDWR | O_NONBLOCK);
}

static void sysfs_unexport(int pin)
{
    int fd = open("/sys/class/gpio/unexport", O_WRONLY);
    if (fd < 0) return;
    char buf[16];
    int len = snprintf(buf, sizeof(buf), "%d", pin);
    (void)write(fd, buf, len);
    close(fd);
}

static int sysfs_init(struct gpio_handle *gh)
{
    struct sysfs_ctx *ctx = calloc(1, sizeof(struct sysfs_ctx));
    if (!ctx)
        return GPIO_ERR_INIT;

    for (int i = 0; i < MAX_GPIO_PINS; i++) {
        ctx->value_fds[i] = -1;
        ctx->exported[i] = false;
    }

    gh->backend_ctx = ctx;
    log_info("GPIO sysfs backend initialized");
    return GPIO_OK;
}

static void sysfs_close(struct gpio_handle *gh)
{
    struct sysfs_ctx *ctx = (struct sysfs_ctx *)gh->backend_ctx;
    if (!ctx) return;

    for (int i = 0; i < MAX_GPIO_PINS; i++) {
        if (ctx->value_fds[i] >= 0)
            close(ctx->value_fds[i]);
        if (ctx->exported[i])
            sysfs_unexport(i);
    }

    free(ctx);
    gh->backend_ctx = NULL;
}

static int sysfs_ensure_exported(struct sysfs_ctx *ctx, int pin)
{
    if (pin < 0 || pin >= MAX_GPIO_PINS)
        return GPIO_ERR_PIN;

    if (!ctx->exported[pin]) {
        int ret = sysfs_export(pin);
        if (ret != GPIO_OK)
            return ret;
        ctx->exported[pin] = true;
    }
    return GPIO_OK;
}

static int sysfs_ensure_value_fd(struct sysfs_ctx *ctx, int pin)
{
    if (ctx->value_fds[pin] < 0) {
        ctx->value_fds[pin] = sysfs_open_value(pin);
        if (ctx->value_fds[pin] < 0)
            return GPIO_ERR_IO;
    }
    return GPIO_OK;
}

static int sysfs_gpio_set_output(struct gpio_handle *gh, int pin)
{
    struct sysfs_ctx *ctx = (struct sysfs_ctx *)gh->backend_ctx;
    int ret = sysfs_ensure_exported(ctx, pin);
    if (ret != GPIO_OK) return ret;
    ret = sysfs_set_direction(pin, "out");
    if (ret != GPIO_OK) return ret;
    return sysfs_ensure_value_fd(ctx, pin);
}

static int sysfs_gpio_set_input(struct gpio_handle *gh, int pin)
{
    struct sysfs_ctx *ctx = (struct sysfs_ctx *)gh->backend_ctx;
    int ret = sysfs_ensure_exported(ctx, pin);
    if (ret != GPIO_OK) return ret;
    ret = sysfs_set_direction(pin, "in");
    if (ret != GPIO_OK) return ret;
    return sysfs_ensure_value_fd(ctx, pin);
}

static int sysfs_gpio_write(struct gpio_handle *gh, int pin, int value)
{
    struct sysfs_ctx *ctx = (struct sysfs_ctx *)gh->backend_ctx;
    if (pin < 0 || pin >= MAX_GPIO_PINS || ctx->value_fds[pin] < 0)
        return GPIO_ERR_PIN;

    const char *val_str = value ? "1" : "0";
    lseek(ctx->value_fds[pin], 0, SEEK_SET);
    if (write(ctx->value_fds[pin], val_str, 1) != 1) {
        log_error("sysfs: write pin %d failed: %s", pin, strerror(errno));
        return GPIO_ERR_IO;
    }
    return GPIO_OK;
}

static int sysfs_gpio_read(struct gpio_handle *gh, int pin)
{
    struct sysfs_ctx *ctx = (struct sysfs_ctx *)gh->backend_ctx;
    if (pin < 0 || pin >= MAX_GPIO_PINS || ctx->value_fds[pin] < 0)
        return GPIO_ERR_PIN;

    char val;
    lseek(ctx->value_fds[pin], 0, SEEK_SET);
    if (read(ctx->value_fds[pin], &val, 1) != 1) {
        log_error("sysfs: read pin %d failed: %s", pin, strerror(errno));
        return GPIO_ERR_IO;
    }
    return (val == '1') ? 1 : 0;
}

static int sysfs_gpio_wait_level(struct gpio_handle *gh, int pin, int level, int timeout_ms)
{
    struct sysfs_ctx *ctx = (struct sysfs_ctx *)gh->backend_ctx;
    if (pin < 0 || pin >= MAX_GPIO_PINS || ctx->value_fds[pin] < 0)
        return GPIO_ERR_PIN;

    /* Set edge detection */
    const char *edge = (level == 1) ? "rising" : "falling";
    sysfs_set_edge(pin, edge);

    /* Clear any pending interrupt */
    char val;
    lseek(ctx->value_fds[pin], 0, SEEK_SET);
    (void)read(ctx->value_fds[pin], &val, 1);

    /* Check current level first */
    int current = sysfs_gpio_read(gh, pin);
    if (current == level)
        return GPIO_OK;

    /* Poll for edge event */
    struct pollfd pfd;
    pfd.fd = ctx->value_fds[pin];
    pfd.events = POLLPRI | POLLERR;

    int ret = poll(&pfd, 1, timeout_ms);
    if (ret < 0) {
        log_error("sysfs: poll pin %d failed: %s", pin, strerror(errno));
        return GPIO_ERR_IO;
    }
    if (ret == 0)
        return GPIO_ERR_TIMEOUT;

    /* Consume the event */
    lseek(ctx->value_fds[pin], 0, SEEK_SET);
    (void)read(ctx->value_fds[pin], &val, 1);

    return GPIO_OK;
}

/*
 *=============================================
 *  PIGPIO backend implementation
 *=============================================
 */

#ifdef USEPIGPIO

static int pigpio_init_backend(struct gpio_handle *gh)
{
    if (gpioInitialise() < 0) {
        log_warn("pigpio init failed, falling back to sysfs");
        return GPIO_ERR_INIT;
    }
    gh->backend_ctx = NULL;  /* pigpio uses global state */
    log_info("GPIO pigpio backend initialized");
    return GPIO_OK;
}

static void pigpio_close_backend(struct gpio_handle *gh)
{
    (void)gh;
    gpioTerminate();
    log_info("GPIO pigpio backend closed");
}

static int pigpio_gpio_set_output(struct gpio_handle *gh, int pin)
{
    (void)gh;
    return (gpioSetMode(pin, PI_OUTPUT) == 0) ? GPIO_OK : GPIO_ERR_IO;
}

static int pigpio_gpio_set_input(struct gpio_handle *gh, int pin)
{
    (void)gh;
    return (gpioSetMode(pin, PI_INPUT) == 0) ? GPIO_OK : GPIO_ERR_IO;
}

static int pigpio_gpio_write(struct gpio_handle *gh, int pin, int value)
{
    (void)gh;
    return (gpioWrite(pin, value) == 0) ? GPIO_OK : GPIO_ERR_IO;
}

static int pigpio_gpio_read(struct gpio_handle *gh, int pin)
{
    (void)gh;
    int val = gpioRead(pin);
    if (val < 0) return GPIO_ERR_IO;
    return val;
}

static int pigpio_gpio_set_isr(struct gpio_handle *gh, int pin, int edge,
                                gpio_isr_func callback, void *userdata)
{
    (void)gh;
    int pigpio_edge;
    switch (edge) {
        case GPIO_EDGE_RISING:  pigpio_edge = RISING_EDGE;  break;
        case GPIO_EDGE_FALLING: pigpio_edge = FALLING_EDGE; break;
        case GPIO_EDGE_BOTH:    pigpio_edge = EITHER_EDGE;  break;
        default: return GPIO_ERR_PIN;
    }
    /* pigpio's gpioSetISRFuncEx has a compatible signature */
    return (gpioSetISRFuncEx(pin, pigpio_edge, 0,
            (gpioISRFuncEx_t)callback, userdata) == 0) ? GPIO_OK : GPIO_ERR_IO;
}

static int pigpio_gpio_wait_level(struct gpio_handle *gh, int pin, int level, int timeout_ms)
{
    (void)gh;
    /* Poll with short sleep intervals */
    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);

    while (1) {
        int val = gpioRead(pin);
        if (val == level)
            return GPIO_OK;

        clock_gettime(CLOCK_MONOTONIC, &now);
        long elapsed_ms = (now.tv_sec - start.tv_sec) * 1000 +
                          (now.tv_nsec - start.tv_nsec) / 1000000;
        if (elapsed_ms >= timeout_ms)
            return GPIO_ERR_TIMEOUT;

        usleep(100);
    }
}

#endif /* USEPIGPIO */

/*
 *=============================================
 *  LIBGPIOD backend implementation
 *=============================================
 */

#ifdef USELIBGPIOD

struct gpiod_ctx {
    struct gpiod_chip *chip;
    struct gpiod_line *lines[MAX_GPIO_PINS];
};

static int gpiod_init_backend(struct gpio_handle *gh)
{
    struct gpiod_ctx *ctx = calloc(1, sizeof(struct gpiod_ctx));
    if (!ctx)
        return GPIO_ERR_INIT;

    ctx->chip = gpiod_chip_open("/dev/gpiochip0");
    if (!ctx->chip) {
        log_warn("libgpiod: cannot open /dev/gpiochip0, falling back to sysfs");
        free(ctx);
        return GPIO_ERR_INIT;
    }

    for (int i = 0; i < MAX_GPIO_PINS; i++)
        ctx->lines[i] = NULL;

    gh->backend_ctx = ctx;
    log_info("GPIO libgpiod backend initialized");
    return GPIO_OK;
}

static void gpiod_close_backend(struct gpio_handle *gh)
{
    struct gpiod_ctx *ctx = (struct gpiod_ctx *)gh->backend_ctx;
    if (!ctx) return;

    for (int i = 0; i < MAX_GPIO_PINS; i++) {
        if (ctx->lines[i])
            gpiod_line_release(ctx->lines[i]);
    }
    if (ctx->chip)
        gpiod_chip_close(ctx->chip);

    free(ctx);
    gh->backend_ctx = NULL;
    log_info("GPIO libgpiod backend closed");
}

static struct gpiod_line *gpiod_get_line(struct gpiod_ctx *ctx, int pin)
{
    if (pin < 0 || pin >= MAX_GPIO_PINS)
        return NULL;
    if (!ctx->lines[pin])
        ctx->lines[pin] = gpiod_chip_get_line(ctx->chip, pin);
    return ctx->lines[pin];
}

static int gpiod_gpio_set_output(struct gpio_handle *gh, int pin)
{
    struct gpiod_ctx *ctx = (struct gpiod_ctx *)gh->backend_ctx;
    struct gpiod_line *line = gpiod_get_line(ctx, pin);
    if (!line) return GPIO_ERR_PIN;
    return (gpiod_line_request_output(line, "heimdall", 0) == 0) ? GPIO_OK : GPIO_ERR_IO;
}

static int gpiod_gpio_set_input(struct gpio_handle *gh, int pin)
{
    struct gpiod_ctx *ctx = (struct gpiod_ctx *)gh->backend_ctx;
    struct gpiod_line *line = gpiod_get_line(ctx, pin);
    if (!line) return GPIO_ERR_PIN;
    return (gpiod_line_request_input(line, "heimdall") == 0) ? GPIO_OK : GPIO_ERR_IO;
}

static int gpiod_gpio_write(struct gpio_handle *gh, int pin, int value)
{
    struct gpiod_ctx *ctx = (struct gpiod_ctx *)gh->backend_ctx;
    struct gpiod_line *line = gpiod_get_line(ctx, pin);
    if (!line) return GPIO_ERR_PIN;
    return (gpiod_line_set_value(line, value) == 0) ? GPIO_OK : GPIO_ERR_IO;
}

static int gpiod_gpio_read(struct gpio_handle *gh, int pin)
{
    struct gpiod_ctx *ctx = (struct gpiod_ctx *)gh->backend_ctx;
    struct gpiod_line *line = gpiod_get_line(ctx, pin);
    if (!line) return GPIO_ERR_PIN;
    int val = gpiod_line_get_value(line);
    if (val < 0) return GPIO_ERR_IO;
    return val;
}

static int gpiod_gpio_wait_level(struct gpio_handle *gh, int pin, int level, int timeout_ms)
{
    struct gpiod_ctx *ctx = (struct gpiod_ctx *)gh->backend_ctx;
    struct gpiod_line *line = gpiod_get_line(ctx, pin);
    if (!line) return GPIO_ERR_PIN;

    /* Check current level first */
    int current = gpiod_line_get_value(line);
    if (current == level)
        return GPIO_OK;

    /* Request events */
    int event_type = (level == 1) ? GPIOD_LINE_REQUEST_EVENT_RISING_EDGE
                                  : GPIOD_LINE_REQUEST_EVENT_FALLING_EDGE;
    gpiod_line_release(line);
    if (gpiod_line_request_both_edges_events(line, "heimdall") != 0)
        return GPIO_ERR_IO;

    struct timespec ts;
    ts.tv_sec = timeout_ms / 1000;
    ts.tv_nsec = (timeout_ms % 1000) * 1000000L;

    int ret = gpiod_line_event_wait(line, &ts);
    if (ret == 0) return GPIO_ERR_TIMEOUT;
    if (ret < 0) return GPIO_ERR_IO;

    /* Consume event */
    struct gpiod_line_event event;
    gpiod_line_event_read(line, &event);

    return GPIO_OK;
}

#endif /* USELIBGPIOD */

/*
 *=============================================
 *  Public API -- dispatch to active backend
 *=============================================
 */

int gpio_init(struct gpio_handle *gh, gpio_backend_t backend)
{
    memset(gh, 0, sizeof(struct gpio_handle));
    gh->initialized = false;

    int ret = GPIO_ERR_BACKEND;

    /* Try requested backend, fall back to sysfs */
    switch (backend) {
    case GPIO_BACKEND_PIGPIO:
#ifdef USEPIGPIO
        ret = pigpio_init_backend(gh);
        if (ret == GPIO_OK) {
            gh->backend = GPIO_BACKEND_PIGPIO;
            gh->initialized = true;
            return GPIO_OK;
        }
#else
        log_warn("pigpio backend requested but USEPIGPIO not defined");
#endif
        /* Fall through to sysfs */
        log_info("Falling back to sysfs GPIO backend");
        /* fall through */

    case GPIO_BACKEND_GPIOD:
        if (backend == GPIO_BACKEND_GPIOD) {
#ifdef USELIBGPIOD
            ret = gpiod_init_backend(gh);
            if (ret == GPIO_OK) {
                gh->backend = GPIO_BACKEND_GPIOD;
                gh->initialized = true;
                return GPIO_OK;
            }
#else
            log_warn("libgpiod backend requested but USELIBGPIOD not defined");
#endif
            log_info("Falling back to sysfs GPIO backend");
        }
        /* fall through */

    case GPIO_BACKEND_SYSFS:
    default:
        ret = sysfs_init(gh);
        if (ret == GPIO_OK) {
            gh->backend = GPIO_BACKEND_SYSFS;
            gh->initialized = true;
            return GPIO_OK;
        }
        break;
    }

    log_error("GPIO initialization failed for all backends");
    return ret;
}

void gpio_close(struct gpio_handle *gh)
{
    if (!gh || !gh->initialized)
        return;

    switch (gh->backend) {
    case GPIO_BACKEND_SYSFS:
        sysfs_close(gh);
        break;
#ifdef USEPIGPIO
    case GPIO_BACKEND_PIGPIO:
        pigpio_close_backend(gh);
        break;
#endif
#ifdef USELIBGPIOD
    case GPIO_BACKEND_GPIOD:
        gpiod_close_backend(gh);
        break;
#endif
    default:
        break;
    }

    gh->initialized = false;
}

int gpio_set_output(struct gpio_handle *gh, int pin)
{
    if (!gh || !gh->initialized)
        return GPIO_ERR_INIT;

    switch (gh->backend) {
#ifdef USEPIGPIO
    case GPIO_BACKEND_PIGPIO:
        return pigpio_gpio_set_output(gh, pin);
#endif
#ifdef USELIBGPIOD
    case GPIO_BACKEND_GPIOD:
        return gpiod_gpio_set_output(gh, pin);
#endif
    case GPIO_BACKEND_SYSFS:
    default:
        return sysfs_gpio_set_output(gh, pin);
    }
}

int gpio_set_input(struct gpio_handle *gh, int pin)
{
    if (!gh || !gh->initialized)
        return GPIO_ERR_INIT;

    switch (gh->backend) {
#ifdef USEPIGPIO
    case GPIO_BACKEND_PIGPIO:
        return pigpio_gpio_set_input(gh, pin);
#endif
#ifdef USELIBGPIOD
    case GPIO_BACKEND_GPIOD:
        return gpiod_gpio_set_input(gh, pin);
#endif
    case GPIO_BACKEND_SYSFS:
    default:
        return sysfs_gpio_set_input(gh, pin);
    }
}

int gpio_write(struct gpio_handle *gh, int pin, int value)
{
    if (!gh || !gh->initialized)
        return GPIO_ERR_INIT;

    switch (gh->backend) {
#ifdef USEPIGPIO
    case GPIO_BACKEND_PIGPIO:
        return pigpio_gpio_write(gh, pin, value);
#endif
#ifdef USELIBGPIOD
    case GPIO_BACKEND_GPIOD:
        return gpiod_gpio_write(gh, pin, value);
#endif
    case GPIO_BACKEND_SYSFS:
    default:
        return sysfs_gpio_write(gh, pin, value);
    }
}

int gpio_read(struct gpio_handle *gh, int pin)
{
    if (!gh || !gh->initialized)
        return GPIO_ERR_INIT;

    switch (gh->backend) {
#ifdef USEPIGPIO
    case GPIO_BACKEND_PIGPIO:
        return pigpio_gpio_read(gh, pin);
#endif
#ifdef USELIBGPIOD
    case GPIO_BACKEND_GPIOD:
        return gpiod_gpio_read(gh, pin);
#endif
    case GPIO_BACKEND_SYSFS:
    default:
        return sysfs_gpio_read(gh, pin);
    }
}

int gpio_set_isr(struct gpio_handle *gh, int pin, int edge,
                 gpio_isr_func callback, void *userdata)
{
    if (!gh || !gh->initialized)
        return GPIO_ERR_INIT;

    switch (gh->backend) {
#ifdef USEPIGPIO
    case GPIO_BACKEND_PIGPIO:
        return pigpio_gpio_set_isr(gh, pin, edge, callback, userdata);
#endif
    case GPIO_BACKEND_SYSFS:
    default:
        /* sysfs does not support ISR callbacks directly.
         * Use gpio_wait_level() for polling instead. */
        log_warn("ISR callbacks not supported on sysfs backend; use gpio_wait_level()");
        (void)pin; (void)edge; (void)callback; (void)userdata;
        return GPIO_ERR_BACKEND;
    }
}

int gpio_wait_level(struct gpio_handle *gh, int pin, int level, int timeout_ms)
{
    if (!gh || !gh->initialized)
        return GPIO_ERR_INIT;

    switch (gh->backend) {
#ifdef USEPIGPIO
    case GPIO_BACKEND_PIGPIO:
        return pigpio_gpio_wait_level(gh, pin, level, timeout_ms);
#endif
#ifdef USELIBGPIOD
    case GPIO_BACKEND_GPIOD:
        return gpiod_gpio_wait_level(gh, pin, level, timeout_ms);
#endif
    case GPIO_BACKEND_SYSFS:
    default:
        return sysfs_gpio_wait_level(gh, pin, level, timeout_ms);
    }
}
