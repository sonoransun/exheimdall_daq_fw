/*
 * Description :
 * Channel -> RTL-SDR EEPROM serial mapping for rtl_daq ([hw] serial_base,
 * [hw] device_serials) and duplicate-serial counting.
 *
 * Standalone translation unit: no librtlsdr / zmq / pigpio dependency, so it
 * can be compiled and tested without the copy-in libraries
 * (see _testing/unit_test/test_device_map.py and the device_map_probe target).
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 */
#ifndef DEVICE_MAP_H
#define DEVICE_MAP_H

#include <stddef.h>

/* The 128-byte :1130 control message carries at most 31 per-channel values */
#define DEVICE_MAP_MAX_CH      31
/* Storage size of one serial string (an 8-digit serial + NUL fits easily) */
#define DEVICE_MAP_SERIAL_LEN  16
/* Largest serial an 8-digit EEPROM serial string can hold */
#define DEVICE_MAP_SERIAL_MAX  99999999
/* Largest accepted [hw] serial_base: base + 30 must stay an 8-digit serial */
#define DEVICE_MAP_SERIAL_BASE_MAX (DEVICE_MAP_SERIAL_MAX - DEVICE_MAP_MAX_CH)

/*
 * Resolve the per-channel serial strings.
 *   device_serials: NULL, "" or whitespace only -> channel i uses the decimal
 *                   string of serial_base + i (serial_base must be
 *                   0..DEVICE_MAP_SERIAL_BASE_MAX);
 *                   otherwise a comma list with exactly num_ch distinct
 *                   entries of 1-8 decimal digits (surrounding blanks are
 *                   ignored); entry i (0-based) is channel i.
 * Returns 0 on success, -1 on error with a NUL-terminated reason in err.
 * Error reasons (exact strings):
 *   "num_ch out of range 1..31"
 *   "device_serials has %d entries but num_ch is %d"
 *   "device_serials entry %d is not a decimal serial of 1-8 digits"
 *   "device_serials entry %d duplicates entry %d"   (later, earlier; 0-based)
 *   "serial_base out of range"
 */
int device_map_resolve(const char *device_serials, int serial_base, int num_ch,
                       char out[][DEVICE_MAP_SERIAL_LEN], char *err, size_t errlen);

/* Count how many entries of `present` (n_present device serial strings) equal `serial`. */
int device_map_count(const char *serial, const char present[][DEVICE_MAP_SERIAL_LEN], int n_present);

#endif /* DEVICE_MAP_H */
