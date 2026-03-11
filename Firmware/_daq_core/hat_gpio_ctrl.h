/*
 *
 * Description :
 * GPIO HAT controller with pluggable backends (sysfs, pigpio, libgpiod).
 * Provides unified GPIO access for data-ready signals, noise source
 * control, and FPGA HAT communication.
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

#ifndef HAT_GPIO_CTRL_H
#define HAT_GPIO_CTRL_H

#include <stdint.h>
#include <stdbool.h>

/*
 *-------------------------------------
 *  GPIO backend selection
 *-------------------------------------
 */
typedef enum {
    GPIO_BACKEND_SYSFS  = 0,
    GPIO_BACKEND_PIGPIO = 1,
    GPIO_BACKEND_GPIOD  = 2,
} gpio_backend_t;

/*
 *-------------------------------------
 *  Edge types for interrupts
 *-------------------------------------
 */
#define GPIO_EDGE_NONE    0
#define GPIO_EDGE_RISING  1
#define GPIO_EDGE_FALLING 2
#define GPIO_EDGE_BOTH    3

/*
 *-------------------------------------
 *  Error codes
 *-------------------------------------
 */
#define GPIO_OK            0
#define GPIO_ERR_INIT     -1
#define GPIO_ERR_PIN      -2
#define GPIO_ERR_IO       -3
#define GPIO_ERR_TIMEOUT  -4
#define GPIO_ERR_BACKEND  -5

/*
 *-------------------------------------
 *  ISR callback type
 *-------------------------------------
 */
typedef void (*gpio_isr_func)(int pin, int level, void *userdata);

/*
 *-------------------------------------
 *  GPIO handle
 *-------------------------------------
 */
struct gpio_handle {
    gpio_backend_t backend;
    void *backend_ctx;    /* Backend-specific context */
    bool initialized;
};

/*
 *-------------------------------------
 *  API functions
 *-------------------------------------
 */

/* Initialize GPIO subsystem with specified backend.
 * If the preferred backend is not available, falls back to sysfs. */
int  gpio_init(struct gpio_handle *gh, gpio_backend_t backend);
void gpio_close(struct gpio_handle *gh);

/* Pin direction configuration */
int  gpio_set_output(struct gpio_handle *gh, int pin);
int  gpio_set_input(struct gpio_handle *gh, int pin);

/* Digital I/O */
int  gpio_write(struct gpio_handle *gh, int pin, int value);
int  gpio_read(struct gpio_handle *gh, int pin);

/* Interrupt support (for DRDY signals) */
int  gpio_set_isr(struct gpio_handle *gh, int pin, int edge,
                  gpio_isr_func callback, void *userdata);

/* Convenience: wait for pin to reach specified level with timeout */
int  gpio_wait_level(struct gpio_handle *gh, int pin, int level, int timeout_ms);

#endif /* HAT_GPIO_CTRL_H */
