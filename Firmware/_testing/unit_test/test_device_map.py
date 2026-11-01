"""
    Tests for the rtl_daq ingress device map (WP-03, design spec A.2/A.3/A.7).

    Part 1 - device_map.c probe (any OS with cc/gcc/clang):
        device_map.c is compiled with -DDEVICE_MAP_PROBE_MAIN and driven as
        `device_map_probe "<list>" <base> <num_ch>` and
        `device_map_probe --count S s1,s2,...`. Asserts the default map, the
        serial_base offset, list parsing with blanks, every error string and
        device_map_count with 0/1/2 matches. The Makefile wiring (device_map.o,
        the probe target and the PIGPIO switch, via `make -n`) is checked too.

    Part 2 - rtl_daq.c against a fake librtlsdr (Linux + C compiler + libzmq
        headers; skipped otherwise):
        rtl_daq.c is compiled unchanged together with device_map.c, log.c,
        ini.c, iq_header.c and sh_mem_util.c against a stub rtl-sdr.h and an
        in-test fake librtlsdr whose attached devices are given by the
        FAKE_RTLSDR_SERIALS environment variable. The process runs in a temp
        directory on a free ZMQ port. Asserts: identical "Device serial:%s,
        index: %d" lines and header unit_id with no new key, serial_base /
        device_serials mapping, the duplicate-serial warning (keys absent) and
        fatal error (keys set), the expanded -3 message, device-map and ring
        depth validation, the ring depth (default 8, and a configured one)
        reported by a forced overrun, usb_async_transfers reaching
        rtlsdr_read_async, the refusal of a device_serials line longer than inih's 199 bytes,
        unit_id = auto, the ZMQ bind address (Linux /proc/net/tcp) and the
        once-per-process 'r' deprecation warning (needs pyzmq).

    Hardware-free, no sudo. Run:
        python3 -m unittest -v _testing/unit_test/test_device_map.py

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
FIRMWARE = os.path.abspath(os.path.join(HERE, "..", ".."))
DAQ_CORE = os.path.join(FIRMWARE, "_daq_core")
DEVICE_MAP_C = os.path.join(DAQ_CORE, "device_map.c")

sys.path.insert(0, DAQ_CORE)
from iq_header import IQHeader  # noqa: E402

IQ_HEADER_LENGTH = 1024
FRAME_TYPE_DUMMY = 1


def find_compiler():
    for cand in ("cc", "gcc", "clang"):
        path = shutil.which(cand)
        if path:
            return path
    return None


# ---------------------------------------------------------------------------
# Part 1: device_map.c probe
# ---------------------------------------------------------------------------

class TestDeviceMapProbe(unittest.TestCase):
    tmpdir = None
    probe = None
    compiler = None

    @classmethod
    def setUpClass(cls):
        cls.compiler = find_compiler()
        if cls.compiler is None:
            return
        cls.tmpdir = tempfile.mkdtemp(prefix="hd_devmap_")
        cls.probe = os.path.join(cls.tmpdir, "device_map_probe")
        res = subprocess.run(
            [cls.compiler, "-std=gnu99", "-Wall", "-Wextra", "-Werror",
             "-DDEVICE_MAP_PROBE_MAIN", "-I", DAQ_CORE, DEVICE_MAP_C,
             "-o", cls.probe],
            capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise AssertionError("device_map.c probe failed to compile "
                                 "warning-free:\n" + res.stderr)

    @classmethod
    def tearDownClass(cls):
        if cls.tmpdir:
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        if self.compiler is None:
            self.skipTest("SKIP reason: no C compiler (cc/gcc/clang) on PATH")

    def resolve(self, lst, base, num_ch):
        res = subprocess.run([self.probe, lst, str(base), str(num_ch)],
                             capture_output=True, text=True, timeout=30)
        return res.returncode, res.stdout.splitlines()

    def assert_ok(self, lst, base, num_ch, expected):
        rc, lines = self.resolve(lst, base, num_ch)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines, expected)

    def assert_err(self, lst, base, num_ch, reason):
        rc, lines = self.resolve(lst, base, num_ch)
        self.assertEqual(rc, 1, lines)
        self.assertEqual(lines, ["ERR " + reason])

    def count(self, serial, present):
        res = subprocess.run([self.probe, "--count", serial, present],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        return int(res.stdout.strip())

    # -- serial_base path ---------------------------------------------------
    def test_default_map_is_1000_plus_i(self):
        self.assert_ok("", 1000, 5, ["1000", "1001", "1002", "1003", "1004"])

    def test_serial_base_offset(self):
        self.assert_ok("", 1100, 5, ["1100", "1101", "1102", "1103", "1104"])

    def test_blank_list_uses_serial_base(self):
        self.assert_ok("  \t ", 1200, 2, ["1200", "1201"])

    def test_single_channel(self):
        self.assert_ok("", 0, 1, ["0"])

    def test_serial_base_upper_bound(self):
        # 99999999 - 31: channel 30 still fits an 8-digit serial
        rc, lines = self.resolve("", 99999968, 31)
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], "99999968")
        self.assertEqual(lines[-1], "99999998")
        self.assertEqual(len(lines), 31)

    def test_serial_base_out_of_range(self):
        self.assert_err("", -1, 5, "serial_base out of range")
        self.assert_err("", 99999969, 1, "serial_base out of range")

    # -- device_serials path ------------------------------------------------
    def test_list_parsing_with_spaces(self):
        self.assert_ok(" 2000 , 2001,\t2002 ", 1000, 3,
                       ["2000", "2001", "2002"])

    def test_list_wins_over_serial_base(self):
        self.assert_ok("5003,5001,5002", 1100, 3, ["5003", "5001", "5002"])

    def test_list_ignores_serial_base_range(self):
        self.assert_ok("7", -1, 1, ["7"])

    def test_list_keeps_leading_zeros_and_8_digits(self):
        self.assert_ok("0001,12345678", 1000, 2, ["0001", "12345678"])

    def test_list_entry_count_mismatch(self):
        self.assert_err("1000,1001", 1000, 3,
                        "device_serials has 2 entries but num_ch is 3")
        self.assert_err("1000,1001,1002,", 1000, 3,
                        "device_serials has 4 entries but num_ch is 3")

    def test_list_bad_entries(self):
        reason = "device_serials entry %d is not a decimal serial of 1-8 digits"
        self.assert_err("1000,10a0,1002", 1000, 3, reason % 1)
        self.assert_err("123456789", 1000, 1, reason % 0)
        self.assert_err("1000,,1002", 1000, 3, reason % 1)
        self.assert_err("1000,1001,", 1000, 3, reason % 2)
        self.assert_err("-100", 1000, 1, reason % 0)
        self.assert_err("10 00", 1000, 1, reason % 0)

    def test_list_duplicates(self):
        self.assert_err("1000,1001,1000", 1000, 3,
                        "device_serials entry 2 duplicates entry 0")
        self.assert_err("1000, 1001 ,1001", 1000, 3,
                        "device_serials entry 2 duplicates entry 1")

    def test_num_ch_range(self):
        self.assert_err("", 1000, 0, "num_ch out of range 1..31")
        self.assert_err("", 1000, 32, "num_ch out of range 1..31")
        self.assert_err("1,2", 1000, 32, "num_ch out of range 1..31")
        rc, lines = self.resolve("", 1000, 31)
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 31)

    def test_usage_error_exit_2(self):
        res = subprocess.run([self.probe, "", "x", "5"], capture_output=True,
                             timeout=30)
        self.assertEqual(res.returncode, 2)
        res = subprocess.run([self.probe], capture_output=True, timeout=30)
        self.assertEqual(res.returncode, 2)

    # -- device_map_count ---------------------------------------------------
    def test_count_matches(self):
        self.assertEqual(self.count("1000", "1001,1002"), 0)
        self.assertEqual(self.count("1000", "1001,1000,1002"), 1)
        self.assertEqual(self.count("1000", "1000,1001,1000"), 2)
        self.assertEqual(self.count("1000", ""), 0)
        # string equality, not numeric equality
        self.assertEqual(self.count("1000", "01000,1000"), 1)


class TestMakefileWiring(unittest.TestCase):
    """device_map.o is linked into rtl_daq.out, the probe target exists and
    iq_server.out links -lpthread (K.1 WP-03 contract)."""

    def setUp(self):
        with open(os.path.join(DAQ_CORE, "Makefile")) as fh:
            self.mk = fh.read()

    def rule(self, target):
        m = re.search(r"^%s:(.*)\n\t(.*)$" % re.escape(target), self.mk,
                      re.MULTILINE)
        self.assertIsNotNone(m, "no rule for " + target)
        return m.group(1), m.group(2)

    def test_rtl_daq_links_device_map(self):
        deps, _ = self.rule("rtl_daq.out")
        self.assertIn("device_map.o", deps.split())

    def test_probe_target(self):
        deps, recipe = self.rule("device_map_probe")
        self.assertIn("device_map.c", deps.split())
        self.assertIn("-DDEVICE_MAP_PROBE_MAIN", recipe)

    def test_iq_server_links_pthread(self):
        _, recipe = self.rule("iq_server.out")
        self.assertIn("-lpthread", recipe)

    def make_dry_run(self, target, *variables):
        make = shutil.which("make")
        if make is None:
            self.skipTest("SKIP reason: make not on PATH")
        res = subprocess.run([make, "-n", "-B", target, "ENGINE=generic",
                              "MARCH=portable"] + list(variables),
                             cwd=DAQ_CORE, capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    @staticmethod
    def command_for(output, needle):
        lines = [ln for ln in output.splitlines() if needle in ln]
        if not lines:
            raise AssertionError("no command containing %r in:\n%s" % (needle, output))
        return lines[0]

    def test_pigpio_defines_reach_rtl_daq_compile(self):
        # The documented PIGPIO switch must compile the #ifdef USEPIGPIO code
        # (compile line) and link -lpigpio (link line, without the -D).
        pig = "PIGPIO=-lpigpio -DUSEPIGPIO"
        compile_cmd = self.command_for(self.make_dry_run("rtl_daq.o", pig),
                                       "-c rtl_daq.c")
        self.assertIn("-DUSEPIGPIO", compile_cmd.split())
        out = self.make_dry_run("rtl_daq.out", pig)
        link_cmd = self.command_for(out, "-o rtl_daq.out")
        self.assertIn("-lpigpio", link_cmd.split())
        self.assertNotIn("-DUSEPIGPIO", link_cmd.split())
        # only rtl_daq.o gets the define
        self.assertNotIn("-DUSEPIGPIO", self.command_for(out, "-c log.c").split())

    def test_default_build_has_no_pigpio(self):
        out = self.make_dry_run("rtl_daq.out")
        self.assertNotIn("USEPIGPIO", out)
        self.assertNotIn("-lpigpio", out)


# ---------------------------------------------------------------------------
# Part 2: rtl_daq.c against a fake librtlsdr
# ---------------------------------------------------------------------------

STUB_RTLSDR_H = r"""
/* Minimal rtl-sdr.h for the test build of rtl_daq.c (fake librtlsdr). */
#ifndef FAKE_RTL_SDR_H
#define FAKE_RTL_SDR_H
#include <stdint.h>
typedef struct rtlsdr_dev rtlsdr_dev_t;
typedef void (*rtlsdr_read_async_cb_t)(unsigned char *buf, uint32_t len, void *ctx);
uint32_t rtlsdr_get_device_count(void);
int rtlsdr_get_device_usb_strings(uint32_t index, char *manufact, char *product, char *serial);
int rtlsdr_get_index_by_serial(const char *serial);
int rtlsdr_open(rtlsdr_dev_t **dev, uint32_t index);
int rtlsdr_close(rtlsdr_dev_t *dev);
int rtlsdr_set_center_freq(rtlsdr_dev_t *dev, uint32_t freq);
uint32_t rtlsdr_get_center_freq(rtlsdr_dev_t *dev);
int rtlsdr_set_sample_freq_correction_f(rtlsdr_dev_t *dev, float ppm);
int rtlsdr_set_tuner_gain(rtlsdr_dev_t *dev, int gain);
int rtlsdr_set_tuner_gain_mode(rtlsdr_dev_t *dev, int manual);
int rtlsdr_set_sample_rate(rtlsdr_dev_t *dev, uint32_t rate);
uint32_t rtlsdr_get_sample_rate(rtlsdr_dev_t *dev);
int rtlsdr_set_dithering(rtlsdr_dev_t *dev, int dither);
int rtlsdr_reset_buffer(rtlsdr_dev_t *dev);
int rtlsdr_read_async(rtlsdr_dev_t *dev, rtlsdr_read_async_cb_t cb, void *ctx,
                      uint32_t buf_num, uint32_t buf_len);
int rtlsdr_cancel_async(rtlsdr_dev_t *dev);
int rtlsdr_set_bias_tee_gpio(rtlsdr_dev_t *dev, int gpio, int on);
#endif
"""

FAKE_RTLSDR_C = r"""
/* Fake librtlsdr: attached devices = FAKE_RTLSDR_SERIALS (comma list). */
#define _GNU_SOURCE
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include "rtl-sdr.h"

extern volatile sig_atomic_t exit_flag; /* rtl_daq.c */

/* Interpose libc's mlockall (the executable's definition wins): under test
 * rtl_daq must neither depend on RLIMIT_MEMLOCK (a non-root 8 MiB limit makes
 * the call fail and log a WARN; a 64 MiB one lets MCL_FUTURE starve later
 * thread stacks, aborting libzmq) nor pin memory. Root and non-root runs then
 * take the same path. */
int mlockall(int flags) { (void) flags; return 0; }

struct rtlsdr_dev { int index; volatile int cancel; uint32_t freq, rate; };

static int n_dev = -1;
static char serials[64][32];

static void load(void)
{
    if (n_dev >= 0) return;
    n_dev = 0;
    const char *env = getenv("FAKE_RTLSDR_SERIALS");
    if (!env || !*env) return;
    char *copy = strdup(env), *save = NULL;
    for (char *t = strtok_r(copy, ",", &save); t && n_dev < 64; t = strtok_r(NULL, ",", &save))
        snprintf(serials[n_dev++], sizeof serials[0], "%s", t);
    free(copy);
}

uint32_t rtlsdr_get_device_count(void) { load(); return (uint32_t) n_dev; }

int rtlsdr_get_device_usb_strings(uint32_t index, char *manufact, char *product, char *serial)
{
    load();
    if ((int) index >= n_dev) return -1;
    strcpy(manufact, "Fake");
    strcpy(product, "RTL2838UHIDIR");
    strcpy(serial, serials[index]);
    return 0;
}

int rtlsdr_get_index_by_serial(const char *serial)
{
    char m[256], p[256], s[256];
    if (!serial) return -2;
    uint32_t cnt = rtlsdr_get_device_count();
    if (!cnt) return -1;
    for (uint32_t i = 0; i < cnt; i++)
    {
        rtlsdr_get_device_usb_strings(i, m, p, s);
        if (!strcmp(serial, s)) return (int) i;
    }
    return -3;
}

int rtlsdr_open(rtlsdr_dev_t **dev, uint32_t index)
{
    load();
    if ((int) index >= n_dev) return -1;
    *dev = calloc(1, sizeof(**dev));
    (*dev)->index = (int) index;
    fprintf(stderr, "FAKE open index=%u serial=%s\n", index, serials[index]);
    return 0;
}

int rtlsdr_close(rtlsdr_dev_t *dev) { free(dev); return 0; }
int rtlsdr_set_center_freq(rtlsdr_dev_t *dev, uint32_t freq) { dev->freq = freq; return 0; }
uint32_t rtlsdr_get_center_freq(rtlsdr_dev_t *dev) { return dev->freq; }
int rtlsdr_set_sample_freq_correction_f(rtlsdr_dev_t *dev, float ppm) { (void) dev; (void) ppm; return 0; }
int rtlsdr_set_tuner_gain(rtlsdr_dev_t *dev, int gain) { (void) dev; (void) gain; return 0; }
int rtlsdr_set_tuner_gain_mode(rtlsdr_dev_t *dev, int manual) { (void) dev; (void) manual; return 0; }
int rtlsdr_set_sample_rate(rtlsdr_dev_t *dev, uint32_t rate) { dev->rate = rate; return 0; }
uint32_t rtlsdr_get_sample_rate(rtlsdr_dev_t *dev) { return dev->rate; }
int rtlsdr_set_dithering(rtlsdr_dev_t *dev, int dither) { (void) dev; (void) dither; return 0; }
int rtlsdr_reset_buffer(rtlsdr_dev_t *dev) { (void) dev; return 0; }
int rtlsdr_set_bias_tee_gpio(rtlsdr_dev_t *dev, int gpio, int on) { (void) dev; (void) gpio; (void) on; return 0; }

int rtlsdr_read_async(rtlsdr_dev_t *dev, rtlsdr_read_async_cb_t cb, void *ctx,
                      uint32_t buf_num, uint32_t buf_len)
{
    fprintf(stderr, "FAKE read_async index=%d buf_num=%u buf_len=%u\n", dev->index, buf_num, buf_len);
    unsigned char *buf = malloc(buf_len);
    memset(buf, 0x40 + dev->index, buf_len);
    dev->cancel = 0;
    while (!dev->cancel && !exit_flag)
    {
        cb(buf, buf_len, ctx);
        usleep(4000);
    }
    free(buf);
    return 0;
}

int rtlsdr_cancel_async(rtlsdr_dev_t *dev) { dev->cancel = 1; return 0; }
"""

RTL_DAQ_SOURCES = ("rtl_daq.c", "device_map.c", "log.c", "ini.c",
                   "iq_header.c", "sh_mem_util.c")
RTL_DAQ_HEADERS = ("rtl_daq.h", "device_map.h", "log.h", "ini.h",
                   "iq_header.h", "sh_mem_util.h")


def free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listen_addresses(port):
    """IPv4 listen addresses of `port` from /proc/net/tcp (Linux)."""
    found = []
    with open("/proc/net/tcp") as fh:
        next(fh)
        for line in fh:
            parts = line.split()
            local, state = parts[1], parts[3]
            addr_hex, port_hex = local.split(":")
            if state == "0A" and int(port_hex, 16) == port:
                found.append(socket.inet_ntoa(struct.pack("<I", int(addr_hex, 16))))
    return found


class RtlDaqRun:
    """One rtl_daq process in a private cwd; frames parsed from stdout."""

    def __init__(self, exe, cwd, ini_text, fake_serials, max_headers=40,
                 paused=False):
        with open(os.path.join(cwd, "daq_chain_config.ini"), "w") as fh:
            fh.write(ini_text)
        env = dict(os.environ, FAKE_RTLSDR_SERIALS=fake_serials)
        self.proc = subprocess.Popen([exe], cwd=cwd, env=env,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.headers = []
        self.max_headers = max_headers
        self.err_chunks = []
        self.frame_event = threading.Event()
        # cleared = the stdout reader stops draining the pipe (forces an overrun)
        self.reading = threading.Event()
        if not paused:
            self.reading.set()
        self._t_out = threading.Thread(target=self._read_stdout, daemon=True)
        self._t_err = threading.Thread(target=self._read_stderr, daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _read_stdout(self):
        while True:
            self.reading.wait()
            raw = self._read_exact(IQ_HEADER_LENGTH)
            if raw is None:
                return
            hdr = IQHeader()
            hdr.decode_header(raw)
            payload = 0 if hdr.frame_type == FRAME_TYPE_DUMMY else \
                hdr.active_ant_chs * hdr.cpi_length * 2
            if payload and self._read_exact(payload) is None:
                return
            if len(self.headers) < self.max_headers:
                self.headers.append(hdr)
                if len(self.headers) >= min(5, self.max_headers):
                    self.frame_event.set()

    def _read_stderr(self):
        for chunk in iter(lambda: self.proc.stderr.read1(65536), b""):
            self.err_chunks.append(chunk)

    @property
    def log(self):
        return b"".join(self.err_chunks).decode(errors="replace")

    def wait_frames(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.frame_event.wait(0.1):
                return True
            if self.proc.poll() is not None:
                self._t_out.join(5)
                return self.frame_event.is_set()
        return False

    def stop(self, timeout=20.0):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError("rtl_daq did not exit on SIGTERM:\n" + self.log)
        finally:
            self.close()
        return rc

    def close(self):
        self.reading.set()
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self._t_out.join(10)
        self._t_err.join(10)
        self.proc.stdout.close()
        self.proc.stderr.close()

    def wait_exit(self, timeout=20.0):
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError("rtl_daq did not exit:\n" + self.log)
        finally:
            self.close()
        return rc


def make_ini(num_ch, port, hw_extra="", daq_extra="", unit_id="7",
             instance_id=None):
    """Minimal rtl_daq config; ZMQ port = 1130 + instance_id * 1 = port."""
    if instance_id is None:
        instance_id = port - 1130
    return (
        "[hw]\n"
        "name = kraken\n"
        "unit_id = %s\n"
        "ioo_type = 0\n"
        "num_ch = %d\n"
        "en_bias_tee = 0\n"
        "%s"
        "[daq]\n"
        "log_level = 2\n"
        "daq_buffer_size = 1024\n"
        "sample_rate = 2400000\n"
        "center_freq = 416588000\n"
        "gain = 0\n"
        "en_noise_source_ctr = 1\n"
        "ctr_channel_serial_no = 1000\n"
        "%s"
        "[federation]\n"
        "instance_id = %d\n"
        "port_stride = 1\n"
    ) % (unit_id, num_ch, hw_extra, daq_extra, instance_id)


class TestRtlDaqIngress(unittest.TestCase):
    tmpdir = None
    exe = None
    skip_reason = None

    @classmethod
    def setUpClass(cls):
        if not sys.platform.startswith("linux"):
            cls.skip_reason = "rtl_daq.c needs Linux (pthread_barrier, F_SETPIPE_SZ)"
            return
        cc = find_compiler()
        if cc is None:
            cls.skip_reason = "no C compiler (cc/gcc/clang) on PATH"
            return
        cls.tmpdir = tempfile.mkdtemp(prefix="hd_rtldaq_")
        probe_c = os.path.join(cls.tmpdir, "zmq_probe.c")
        with open(probe_c, "w") as fh:
            fh.write("#include <zmq.h>\nint main(void){void *c = zmq_ctx_new();"
                     " zmq_ctx_destroy(c); return 0;}\n")
        res = subprocess.run([cc, probe_c, "-o", probe_c + ".out", "-lzmq"],
                             capture_output=True, timeout=120)
        if res.returncode != 0:
            cls.skip_reason = "libzmq development files not installed"
            return
        src = os.path.join(cls.tmpdir, "src")
        os.mkdir(src)
        for name in RTL_DAQ_SOURCES + RTL_DAQ_HEADERS:
            shutil.copy(os.path.join(DAQ_CORE, name), src)
        with open(os.path.join(src, "rtl-sdr.h"), "w") as fh:
            fh.write(STUB_RTLSDR_H)
        with open(os.path.join(src, "fake_rtlsdr.c"), "w") as fh:
            fh.write(FAKE_RTLSDR_C)
        cls.exe = os.path.join(cls.tmpdir, "rtl_daq_fake")
        cmd = [cc, "-std=gnu99", "-Wall", "-O1", "-I", src] + \
            [os.path.join(src, n) for n in RTL_DAQ_SOURCES + ("fake_rtlsdr.c",)] + \
            ["-o", cls.exe, "-lzmq", "-lpthread", "-lm", "-lrt"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise AssertionError("rtl_daq.c failed to build against the fake "
                                 "librtlsdr:\n" + res.stderr)

    @classmethod
    def tearDownClass(cls):
        if cls.tmpdir:
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        if self.skip_reason:
            self.skipTest("SKIP reason: " + self.skip_reason)
        self.cwd = tempfile.mkdtemp(prefix="run_", dir=self.tmpdir)
        self.port = free_tcp_port()

    def start(self, ini_text, fake_serials, **kw):
        run = RtlDaqRun(self.exe, self.cwd, ini_text, fake_serials, **kw)
        self.addCleanup(run.close)
        return run

    def run_ok(self, ini_text, fake_serials, **kw):
        run = self.start(ini_text, fake_serials, **kw)
        self.assertTrue(run.wait_frames(), "no IQ frames:\n" + run.log)
        return run

    def run_fatal(self, ini_text, fake_serials):
        run = self.start(ini_text, fake_serials)
        rc = run.wait_exit()
        self.assertNotEqual(rc, 0, run.log)
        self.assertEqual(run.headers, [])
        return run.log

    @staticmethod
    def serial_lines(log):
        return re.findall(r"Device serial:(\S+), index: (-?\d+)", log)

    # -- default path ---------------------------------------------------------
    def test_default_config_identical_serial_lines_and_header(self):
        run = self.run_ok(make_ini(4, self.port), "1000,1001,1002,1003")
        if sys.platform.startswith("linux"):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not listen_addresses(self.port):
                time.sleep(0.1)
            self.assertEqual(listen_addresses(self.port), ["0.0.0.0"])
        rc = run.stop()
        log = run.log
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.serial_lines(log),
                         [("1000", "0"), ("1001", "1"), ("1002", "2"), ("1003", "3")])
        self.assertNotIn("present on", log)
        self.assertNotIn("deprecated", log)
        self.assertIn("buf_num=12 buf_len=2048", log)
        hdr = run.headers[0]
        self.assertEqual(hdr.unit_id, 7)
        self.assertEqual(hdr.active_ant_chs, 4)
        blocks = [h.daq_block_index for h in run.headers]
        self.assertEqual(blocks, list(range(blocks[0], blocks[0] + len(blocks))))

    def test_unit_id_auto_stamps_instance_id(self):
        run = self.run_ok(make_ini(2, self.port, unit_id="auto"), "1000,1001")
        run.stop()
        self.assertEqual(run.headers[0].unit_id, self.port - 1130)

    def test_integer_unit_id_with_instance_id(self):
        run = self.run_ok(make_ini(2, self.port, unit_id="0"), "1000,1001")
        run.stop()
        self.assertEqual(run.headers[0].unit_id, 0)

    # -- serial keys ----------------------------------------------------------
    def test_serial_base_maps_channels(self):
        run = self.run_ok(make_ini(3, self.port, hw_extra="serial_base = 1100\n"),
                          "1102,1100,1101")
        run.stop()
        self.assertEqual(self.serial_lines(run.log),
                         [("1100", "1"), ("1101", "2"), ("1102", "0")])

    def test_device_serials_list_maps_channels(self):
        run = self.run_ok(
            make_ini(3, self.port, hw_extra="device_serials = 7003, 7001,7002\n"),
            "7001,7002,7003")
        run.stop()
        self.assertEqual(self.serial_lines(run.log),
                         [("7003", "2"), ("7001", "0"), ("7002", "1")])

    def test_empty_device_serials_is_absent(self):
        run = self.run_ok(make_ini(2, self.port, hw_extra="device_serials =\n"),
                          "1000,1000,1001")
        run.stop()
        # keys absent -> duplicate is only a warning
        self.assertIn("serial 1000 is present on 2 devices", run.log)

    def test_duplicate_serial_without_keys_warns_and_runs(self):
        run = self.run_ok(make_ini(2, self.port), "1000,1001,1000")
        rc = run.stop()
        log = run.log
        self.assertEqual(rc, 0, log)
        self.assertEqual(log.count(
            "serial 1000 is present on 2 devices; program distinct serials "
            "with util/set_unit_serials.sh"), 1)
        self.assertNotIn("serial 1001 is present", log)
        self.assertEqual(self.serial_lines(log), [("1000", "0"), ("1001", "1")])

    def test_duplicate_serial_with_keys_is_fatal(self):
        log = self.run_fatal(make_ini(2, self.port, hw_extra="serial_base = 1000\n"),
                             "1000,1001,1000")
        self.assertIn("FATAL", log)
        self.assertIn("serial 1000 is present on 2 devices; program distinct "
                      "serials with util/set_unit_serials.sh", log)
        self.assertNotIn("FAKE open", log)

    def test_duplicate_serial_with_device_serials_is_fatal(self):
        log = self.run_fatal(
            make_ini(2, self.port, hw_extra="device_serials = 1000,1001\n"),
            "1000,1001,1000")
        self.assertRegex(log, r"FATAL[^\n]*serial 1000 is present on 2 devices; "
                              r"program distinct serials with util/set_unit_serials\.sh")
        self.assertNotIn("WARN", log)
        self.assertNotIn("FAKE open", log)

    def test_overlong_device_serials_line_is_fatal(self):
        # 31 serials as '1100, 1101, ...' -> a 201-byte line; inih keeps 199
        serials = ", ".join(str(1100 + i) for i in range(31))
        line = "device_serials = %s\n" % serials
        self.assertGreater(len(line), 200)
        log = self.run_fatal(make_ini(31, self.port, hw_extra=line), "1000")
        self.assertRegex(log, r"FATAL[^\n]*\[hw\] device_serials line is %d bytes "
                              r"long; the INI reader keeps at most 199 bytes per line"
                              % (len(line) - 1))
        self.assertNotIn("Device serial:", log)
        # CRLF ending: measured without the line ending
        log = self.run_fatal(make_ini(31, self.port, hw_extra=line.replace("\n", "\r\n")),
                             "1000")
        self.assertIn("[hw] device_serials line is %d bytes long" % (len(line) - 1), log)
        # the same list without blanks fits on one line and resolves
        compact = "device_serials = %s\n" % serials.replace(" ", "")
        self.assertLess(len(compact), 200)
        log = self.run_fatal(make_ini(31, self.port, hw_extra=compact), "1000")
        self.assertNotIn("bytes long", log)
        self.assertIn("expected serial 1100 for channel 0", log)

    def test_missing_serial_expanded_message(self):
        log = self.run_fatal(
            make_ini(2, self.port, hw_extra="device_serials = 1000,1009\n"),
            "1000,1001")
        self.assertIn(
            "The serial numbers of the devices are not yet configured (expected "
            "serial 1009 for channel 1; see [hw] serial_base/device_serials and "
            "util/set_unit_serials.sh), exiting..", log)

    def test_device_map_errors_are_fatal(self):
        log = self.run_fatal(
            make_ini(3, self.port, hw_extra="device_serials = 1000,1001\n"),
            "1000,1001,1002")
        self.assertIn("device map: device_serials has 2 entries but num_ch is 3", log)
        log = self.run_fatal(
            make_ini(2, self.port, hw_extra="serial_base = abc\n"), "1000,1001")
        self.assertIn("device map: serial_base out of range", log)
        log = self.run_fatal(make_ini(32, self.port), "1000")
        self.assertIn("device map: num_ch out of range 1..31", log)

    # -- ring depth -------------------------------------------------------------
    def test_ring_depth_validation(self):
        for extra, text in (
                ("usb_ring_buffers = 1\n", "Invalid [daq] usb_ring_buffers: 1 (allowed 2..64)"),
                ("usb_ring_buffers = 65\n", "Invalid [daq] usb_ring_buffers: 65 (allowed 2..64)"),
                ("usb_ring_buffers = x\n", "Invalid [daq] usb_ring_buffers: -1 (allowed 2..64)"),
                ("usb_async_transfers = 0\n", "Invalid [daq] usb_async_transfers: 0 (allowed 2..64)")):
            with self.subTest(extra=extra):
                log = self.run_fatal(make_ini(2, self.port, daq_extra=extra), "1000,1001")
                self.assertIn(text, log)

    def overrun_ring_depth(self, daq_extra):
        """Stall the stdout reader until the USB readers lap the ring, then
        return the ring depth the overrun warning reports."""
        run = self.start(make_ini(2, self.port, daq_extra=daq_extra),
                         "1000,1001", paused=True)
        # the fake delivers one transfer per 4 ms per channel; the pipe holds
        # a few frames, so 2 s laps any ring of <= 64 slots
        time.sleep(2.0)
        self.assertIsNone(run.proc.poll(), run.log)
        run.reading.set()
        self.assertTrue(run.wait_frames(), "no IQ frames:\n" + run.log)
        pattern = re.compile(r"raise \[daq\] usb_ring_buffers \(now (\d+)\)")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not pattern.search(run.log):
            time.sleep(0.1)
        self.assertEqual(run.stop(), 0, run.log)
        m = pattern.search(run.log)
        self.assertIsNotNone(m, "no overrun warning:\n" + run.log)
        self.assertIn("reader lag reached %s transfers" % m.group(1), run.log)
        return int(m.group(1))

    def test_default_ring_depth_is_8(self):
        self.assertEqual(self.overrun_ring_depth(""), 8)

    def test_configured_ring_depth_reaches_ring(self):
        self.assertEqual(self.overrun_ring_depth("usb_ring_buffers = 3\n"), 3)

    def test_ring_depth_keys_applied(self):
        run = self.run_ok(make_ini(2, self.port,
                                   daq_extra="usb_ring_buffers = 3\nusb_async_transfers = 5\n"),
                          "1000,1001")
        self.assertEqual(run.stop(), 0, run.log)
        self.assertIn("buf_num=5 buf_len=2048", run.log)
        self.assertNotIn("buf_num=12", run.log)
        blocks = [h.daq_block_index for h in run.headers]
        self.assertEqual(blocks, list(range(blocks[0], blocks[0] + len(blocks))))

    # -- ZMQ ------------------------------------------------------------------
    def test_zmq_listen_address_localhost(self):
        run = self.run_ok(make_ini(2, self.port,
                                   daq_extra="zmq_listen_address = localhost\n"),
                          "1000,1001")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not listen_addresses(self.port):
            time.sleep(0.1)
        addrs = listen_addresses(self.port)
        run.stop()
        self.assertEqual(addrs, ["127.0.0.1"])

    def test_r_deprecation_logged_once_and_halt(self):
        try:
            import zmq
        except ImportError:
            self.skipTest("SKIP reason: pyzmq not installed")
        run = self.run_ok(make_ini(2, self.port), "1000,1001")
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 10000)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect("tcp://localhost:%d" % self.port)
        def exchange(command, params=b""):
            msg = bytes([0, ord(command)]) + params
            sock.send(msg + bytes(128 - len(msg)))
            try:
                return sock.recv()
            except zmq.Again:
                raise AssertionError("no reply to '%s':\n%s" % (command, run.log))

        try:
            params = struct.pack("<III", 416588000, 2400000, 0)
            for _ in range(2):
                self.assertEqual(exchange("r", params), b"ok")
            # 'g' with 2 channels: loop bound is min(num_ch, 31)
            self.assertEqual(exchange("g", struct.pack("<II", 10, 20)), b"ok")
            self.assertEqual(exchange("h"), b"ok")
        finally:
            sock.close()
            ctx.term()
        rc = run.wait_exit()
        log = run.log
        self.assertEqual(rc, 0, log)
        self.assertEqual(log.count("ZMQ 'r' reconfiguration is deprecated"), 1)
        self.assertEqual(log.count("Signal 'r': Reconfiguring the tuner"), 2)
        self.assertIn("Channel: 1, Gain: 2.000000 dB", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
