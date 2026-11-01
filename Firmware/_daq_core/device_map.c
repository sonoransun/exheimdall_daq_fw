/*
 * Description :
 * Channel -> RTL-SDR EEPROM serial mapping for rtl_daq (see device_map.h).
 *
 * Built with -DDEVICE_MAP_PROBE_MAIN this file is also a small test probe
 * (make device_map_probe):
 *   device_map_probe "<list>" <serial_base> <num_ch>
 *       prints one resolved serial per line and exits 0,
 *       or prints "ERR <reason>" and exits 1.
 *   device_map_probe --count <serial> <s1,s2,...>
 *       prints how many list entries equal <serial> and exits 0.
 *   Usage errors exit 2.
 *
 * Project : HeIMDALL DAQ Firmware
 * License : GNU GPL V3
 */
#include "device_map.h"

#include <stdio.h>
#include <string.h>

/* Bounds of the blank-trimmed token [*begin, *end) */
static void trim_blanks(const char **begin, const char **end)
{
    while (*begin < *end && (**begin == ' ' || **begin == '\t'))
        (*begin)++;
    while (*end > *begin && ((*end)[-1] == ' ' || (*end)[-1] == '\t'))
        (*end)--;
}

static int is_blank_string(const char *s)
{
    for (; *s; s++)
        if (*s != ' ' && *s != '\t')
            return 0;
    return 1;
}

int device_map_resolve(const char *device_serials, int serial_base, int num_ch,
                       char out[][DEVICE_MAP_SERIAL_LEN], char *err, size_t errlen)
{
    if (errlen > 0)
        err[0] = '\0';

    if (num_ch < 1 || num_ch > DEVICE_MAP_MAX_CH)
    {
        snprintf(err, errlen, "num_ch out of range 1..%d", DEVICE_MAP_MAX_CH);
        return -1;
    }

    if (device_serials == NULL || is_blank_string(device_serials))
    {
        if (serial_base < 0 || serial_base > DEVICE_MAP_SERIAL_BASE_MAX)
        {
            snprintf(err, errlen, "serial_base out of range");
            return -1;
        }
        for (int i = 0; i < num_ch; i++)
            snprintf(out[i], DEVICE_MAP_SERIAL_LEN, "%d", serial_base + i);
        return 0;
    }

    /* Count the entries first so a length mismatch is reported as such */
    int n_entries = 1;
    for (const char *p = device_serials; *p; p++)
        if (*p == ',')
            n_entries++;
    if (n_entries != num_ch)
    {
        snprintf(err, errlen, "device_serials has %d entries but num_ch is %d", n_entries, num_ch);
        return -1;
    }

    const char *tok = device_serials;
    for (int i = 0; i < num_ch; i++)
    {
        const char *comma = strchr(tok, ',');
        const char *begin = tok;
        const char *end = comma ? comma : tok + strlen(tok);
        trim_blanks(&begin, &end);

        size_t len = (size_t)(end - begin);
        int valid = (len >= 1 && len <= 8);
        for (const char *c = begin; valid && c < end; c++)
            if (*c < '0' || *c > '9')
                valid = 0;
        if (!valid)
        {
            snprintf(err, errlen, "device_serials entry %d is not a decimal serial of 1-8 digits", i);
            return -1;
        }
        memcpy(out[i], begin, len);
        out[i][len] = '\0';

        for (int j = 0; j < i; j++)
        {
            if (strcmp(out[i], out[j]) == 0)
            {
                snprintf(err, errlen, "device_serials entry %d duplicates entry %d", i, j);
                return -1;
            }
        }
        tok = comma ? comma + 1 : end;
    }
    return 0;
}

int device_map_count(const char *serial, const char present[][DEVICE_MAP_SERIAL_LEN], int n_present)
{
    int n = 0;
    for (int j = 0; j < n_present; j++)
        if (strcmp(serial, present[j]) == 0)
            n++;
    return n;
}

#ifdef DEVICE_MAP_PROBE_MAIN
#include <stdlib.h>
#include <errno.h>
#include <limits.h>

#define PROBE_MAX_PRESENT 256

static int parse_int(const char *s, int *out)
{
    char *endp = NULL;
    errno = 0;
    long v = strtol(s, &endp, 10);
    if (errno != 0 || endp == s || *endp != '\0' || v < INT_MIN || v > INT_MAX)
        return -1;
    *out = (int)v;
    return 0;
}

static int usage(void)
{
    fprintf(stderr,
            "usage: device_map_probe \"<list>\" <serial_base> <num_ch>\n"
            "       device_map_probe --count <serial> <s1,s2,...>\n");
    return 2;
}

int main(int argc, char **argv)
{
    if (argc == 4 && strcmp(argv[1], "--count") == 0)
    {
        static char present[PROBE_MAX_PRESENT][DEVICE_MAP_SERIAL_LEN];
        int n_present = 0;
        const char *tok = argv[3];
        while (*tok != '\0')
        {
            if (n_present == PROBE_MAX_PRESENT)
                return usage();
            const char *comma = strchr(tok, ',');
            const char *begin = tok;
            const char *end = comma ? comma : tok + strlen(tok);
            trim_blanks(&begin, &end);
            size_t len = (size_t)(end - begin);
            if (len >= DEVICE_MAP_SERIAL_LEN)
                len = DEVICE_MAP_SERIAL_LEN - 1;
            memcpy(present[n_present], begin, len);
            present[n_present][len] = '\0';
            n_present++;
            if (!comma)
                break;
            tok = comma + 1;
        }
        printf("%d\n", device_map_count(argv[2], (const char (*)[DEVICE_MAP_SERIAL_LEN])present, n_present));
        return 0;
    }

    if (argc != 4)
        return usage();

    int serial_base, num_ch;
    if (parse_int(argv[2], &serial_base) != 0 || parse_int(argv[3], &num_ch) != 0)
        return usage();

    char serials[DEVICE_MAP_MAX_CH][DEVICE_MAP_SERIAL_LEN];
    char err[128];
    if (device_map_resolve(argv[1], serial_base, num_ch, serials, err, sizeof err) != 0)
    {
        printf("ERR %s\n", err);
        return 1;
    }
    for (int i = 0; i < num_ch; i++)
        printf("%s\n", serials[i]);
    return 0;
}
#endif /* DEVICE_MAP_PROBE_MAIN */
