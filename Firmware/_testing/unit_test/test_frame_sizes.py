"""
   Lockstep check of the shared-memory frame sizes (spec I.1, I.7).

   Compiles a probe that includes _daq_core/frame_sizes.h (the size_t helpers
   rebuffer.out, decimate.out and iq_server.out use) and asserts, over the
   grid M in {1,4,5,16,31}, Ncpi in {2^16,2^20,2^22}, R in {1,2,4},
   Nc in {2^14,2^16,2^21}, that every key of capacity_model.frame_sizes()
   equals its C helper AND the literal delay_sync.py out_shmem_size
   expression evaluated here.

   Skipped (with a reason) when frame_sizes.h is absent or no C compiler is
   installed. CI-safe: no sudo, temp directory only.

   Project: HeIMDALL DAQ Firmware
   License: GNU GPL V3
"""
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.realpath(__file__))
_CORE = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "_daq_core")
sys.path.insert(0, _CORE)

import capacity_model as cm  # noqa: E402

HEADER = os.path.join(_CORE, "frame_sizes.h")
GRID_M = (1, 4, 5, 16, 31)
GRID_NCPI = (2 ** 16, 2 ** 20, 2 ** 22)
GRID_R = (1, 2, 4)
GRID_NC = (2 ** 14, 2 ** 16, 2 ** 21)

PROBE_SRC = r"""
#include <stdio.h>
#include "frame_sizes.h"

static const int gm[] = {%(m)s};
static const int gn[] = {%(n)s};
static const int gr[] = {%(r)s};
static const int gc[] = {%(c)s};
#define LEN(a) (sizeof(a) / sizeof((a)[0]))

int main(void)
{
    for (size_t a = 0; a < LEN(gm); a++)
    for (size_t b = 0; b < LEN(gn); b++)
    for (size_t c = 0; c < LEN(gr); c++)
    for (size_t d = 0; d < LEN(gc); d++)
    {
        int M = gm[a], N = gn[b], R = gr[c], Nc = gc[d];
        printf("%%d %%d %%d %%d %%zu %%zu %%zu %%zu\n", M, N, R, Nc,
               hd_iq_frame_bytes(M, N, Nc), hd_worst_frame_bytes(M),
               hd_decimator_in_object_bytes(M, N, R, Nc),
               hd_decimator_in_consumer_map_bytes(M, N, R, Nc));
    }
    printf("CONST %%u %%u %%u %%u\n", HD_IQ_HEADER_BYTES, HD_MAX_FRAME_SAMPLES,
           HD_CF32_SAMPLE_BYTES, HD_CU8_SAMPLE_BYTES);
    return 0;
}
"""


def _compiler():
    for name in (os.environ.get("CC"), "cc", "gcc", "clang"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


def delay_sync_out_shmem_size(N, N_proc, M):
    """Verbatim delay_sync.py expression (create_shm_buffers, R61)."""
    if N >= N_proc:
        out_shmem_size = int(1024 + N * 2 * M * (32 / 8))
    else:
        out_shmem_size = int(1024 + N_proc * 2 * M * (32 / 8))
    return out_shmem_size


@unittest.skipUnless(os.path.isfile(HEADER), "SKIP reason: _daq_core/frame_sizes.h not present")
@unittest.skipUnless(_compiler(), "SKIP reason: no C compiler (cc/gcc/clang) on PATH")
class FrameSizesLockstepTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hd_frame_sizes_")
        src = os.path.join(cls.tmp, "probe.c")
        exe = os.path.join(cls.tmp, "probe")
        with open(src, "w") as f:
            f.write(PROBE_SRC % {"m": ",".join(map(str, GRID_M)), "n": ",".join(map(str, GRID_NCPI)),
                                 "r": ",".join(map(str, GRID_R)), "c": ",".join(map(str, GRID_NC))})
        build = subprocess.run([_compiler(), "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", _CORE,
                                "-o", exe, src], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True, timeout=120)
        if build.returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise AssertionError("frame_sizes.h probe does not compile warning-free:\n" + build.stdout)
        run = subprocess.run([exe], stdout=subprocess.PIPE, universal_newlines=True, timeout=60, check=True)
        cls.rows, cls.consts = {}, None
        for line in run.stdout.splitlines():
            parts = line.split()
            if parts[0] == "CONST":
                cls.consts = tuple(int(x) for x in parts[1:])
                continue
            vals = [int(x) for x in parts]
            cls.rows[tuple(vals[:4])] = vals[4:]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_grid_complete(self):
        self.assertEqual(len(self.rows), len(GRID_M) * len(GRID_NCPI) * len(GRID_R) * len(GRID_NC))

    def test_constants(self):
        self.assertEqual(self.consts, (cm.IQ_HEADER_BYTES, cm.MAX_FRAME_SAMPLES,
                                       cm.CF32_SAMPLE_BYTES, cm.CU8_SAMPLE_BYTES))

    def test_every_key_equals_its_c_helper(self):
        for M, N, R, Nc in itertools.product(GRID_M, GRID_NCPI, GRID_R, GRID_NC):
            iq, worst, din_obj, din_map = self.rows[(M, N, R, Nc)]
            for seg_cfg in (False, True):
                with self.subTest(M=M, Ncpi=N, R=R, Nc=Nc, size_segments_from_config=seg_cfg):
                    py = cm.frame_sizes(M, N, R, Nc, size_segments_from_config=seg_cfg)
                    self.assertEqual(py["iq_frame"], iq)
                    self.assertEqual(py["worst_frame"], worst)
                    self.assertEqual(py["decimator_in_object"], din_obj)
                    self.assertEqual(py["decimator_in_consumer_map"], din_map)
                    self.assertEqual(py["decimator_out_resident"], iq)
                    self.assertEqual(py["decimator_out_object"], iq if seg_cfg else worst)
                    self.assertEqual(py["delay_sync_iq"], iq)

    def test_delay_sync_expression(self):
        for M, N, R, Nc in itertools.product(GRID_M, GRID_NCPI, GRID_R, GRID_NC):
            with self.subTest(M=M, Ncpi=N, Nc=Nc):
                literal = delay_sync_out_shmem_size(N, Nc, M)
                self.assertEqual(self.rows[(M, N, R, Nc)][0], literal)
                self.assertEqual(cm.frame_sizes(M, N, R, Nc)["delay_sync_iq"], literal)

    def test_consumer_map_is_four_times_object_payload(self):
        for M, N, R, Nc in itertools.product(GRID_M, GRID_NCPI, GRID_R, GRID_NC):
            _, _, din_obj, din_map = self.rows[(M, N, R, Nc)]
            self.assertEqual(din_map - 1024, 4 * (din_obj - 1024))


class FrameSizesPythonTest(unittest.TestCase):
    """Pure-Python properties that need no compiler."""

    def test_keys(self):
        self.assertEqual(set(cm.frame_sizes(5, 2 ** 20, 1, 2 ** 16)),
                         {"iq_frame", "worst_frame", "decimator_in_object", "decimator_in_consumer_map",
                          "decimator_out_object", "decimator_out_resident", "delay_sync_iq"})

    def test_32_channels_do_not_wrap(self):
        # int arithmetic wrapped at M = 32 (G23); Python ints and size_t do not
        self.assertEqual(cm.frame_sizes(32, 2 ** 20, 1, 2 ** 16)["worst_frame"], 1024 + 2 ** 23 * 32 * 8)

    def test_rejects_bad_values(self):
        for args in ((0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 0, 1), (1, 1, 1, 0), (1.5, 1, 1, 1), (True, 1, 1, 1)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    cm.frame_sizes(*args)
        with self.assertRaises(ValueError):
            cm.frame_sizes(5, 2 ** 20, 1, 2 ** 16, B=0)


if __name__ == "__main__":
    unittest.main()
