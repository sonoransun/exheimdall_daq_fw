"""
    Description :
    C-vs-Python IQ header ABI cross-check (the ABI lock for the refactor).

    Compiles a tiny C program that includes _daq_core/iq_header.h and prints
    sizeof(struct iq_header_struct), offsetof() for every field, and the
    IQH_RSV_* / frame-type / version #defines. The Python side computes the
    same offsets from iq_header.py's native struct format and RSV_* constants
    and asserts byte-for-byte agreement. A binary golden-frame round-trip
    (C-written struct -> IQHeader.decode_header) locks the actual layout,
    including padding, not just the computed offsets.

    Gracefully skips when no C compiler is present (pure-Python consistency
    checks still run).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from os.path import join, dirname, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

from iq_header import IQHeader, HEADER_FORMAT  # noqa: E402

# (field name, struct format) in C declaration order — must mirror
# struct iq_header_struct in iq_header.h AND the HEADER_FORMAT in iq_header.py.
FIELDS = [
    ("sync_word", "I"),
    ("frame_type", "I"),
    ("hardware_id", "16s"),
    ("unit_id", "I"),
    ("active_ant_chs", "I"),
    ("ioo_type", "I"),
    ("rf_center_freq", "Q"),
    ("adc_sampling_freq", "Q"),
    ("sampling_freq", "Q"),
    ("cpi_length", "I"),
    ("time_stamp", "Q"),
    ("daq_block_index", "I"),
    ("cpi_index", "I"),
    ("ext_integration_cntr", "Q"),
    ("data_type", "I"),
    ("sample_bit_depth", "I"),
    ("adc_overdrive_flags", "I"),
    ("if_gains", "32I"),
    ("delay_sync_flag", "I"),
    ("iq_sync_flag", "I"),
    ("sync_state", "I"),
    ("noise_source_state", "I"),
    ("reserved", "192I"),
    ("header_version", "I"),
]

# v8 reserved-slot names shared between iq_header.h (IQH_RSV_*) and
# iq_header.py (RSV_*)
RSV_SLOTS = [
    "EXT_LNA_GAINS", "TOTAL_GAINS", "SYSTEM_NF_MDB", "COMPRESSION_FLAGS",
    "BIAS_TEE_STATE", "ANTENNA_AZ_CDEG", "ANTENNA_EL_CDEG", "ROTATOR_STATE",
    "AGG_POWER_MDB", "BUFFER_OVERRUN_CNT",
]


def python_field_offsets():
    """Field byte offsets implied by the native-alignment Python format."""
    offsets = {}
    prefix = ""
    for name, fmt in FIELDS:
        # '0<code>' appends zero items of the type, forcing the struct module
        # to insert the native alignment padding the next field would get.
        base_char = fmt[-1]
        offsets[name] = struct.calcsize(prefix + "0" + base_char)
        prefix += fmt
    return offsets, struct.calcsize(prefix)


def find_compiler():
    for cand in ("cc", "gcc", "clang"):
        path = shutil.which(cand)
        if path:
            return path
    return None


C_PROGRAM = r"""
#include <stdio.h>
#include <stddef.h>
#include <string.h>
#include "iq_header.h"

int main(int argc, char **argv)
{
    printf("sizeof=%zu\n", sizeof(struct iq_header_struct));
@OFFSET_LINES@
    printf("SYNC_WORD=%u\n", (unsigned) SYNC_WORD);
    printf("IQ_HEADER_LENGTH=%d\n", IQ_HEADER_LENGTH);
    printf("IQ_HEADER_VERSION=%d\n", IQ_HEADER_VERSION);
    printf("FRAME_TYPE_DATA=%d\n", FRAME_TYPE_DATA);
    printf("FRAME_TYPE_DUMMY=%d\n", FRAME_TYPE_DUMMY);
    printf("FRAME_TYPE_RAMP=%d\n", FRAME_TYPE_RAMP);
    printf("FRAME_TYPE_CAL=%d\n", FRAME_TYPE_CAL);
    printf("FRAME_TYPE_TRIGW=%d\n", FRAME_TYPE_TRIGW);
    printf("RSV_EXT_LNA_GAINS=%d\n", IQH_RSV_EXT_LNA_GAINS);
    printf("RSV_TOTAL_GAINS=%d\n", IQH_RSV_TOTAL_GAINS);
    printf("RSV_SYSTEM_NF_MDB=%d\n", IQH_RSV_SYSTEM_NF_MDB);
    printf("RSV_COMPRESSION_FLAGS=%d\n", IQH_RSV_COMPRESSION_FLAGS);
    printf("RSV_BIAS_TEE_STATE=%d\n", IQH_RSV_BIAS_TEE_STATE);
    printf("RSV_ANTENNA_AZ_CDEG=%d\n", IQH_RSV_ANTENNA_AZ_CDEG);
    printf("RSV_ANTENNA_EL_CDEG=%d\n", IQH_RSV_ANTENNA_EL_CDEG);
    printf("RSV_ROTATOR_STATE=%d\n", IQH_RSV_ROTATOR_STATE);
    printf("RSV_AGG_POWER_MDB=%d\n", IQH_RSV_AGG_POWER_MDB);
    printf("RSV_BUFFER_OVERRUN_CNT=%d\n", IQH_RSV_BUFFER_OVERRUN_CNT);

    if (argc > 1) {
        /* Golden frame: distinctive value in every field, written natively */
        struct iq_header_struct h;
        memset(&h, 0, sizeof(h));
        h.sync_word = SYNC_WORD;
        h.frame_type = FRAME_TYPE_CAL;
        strncpy(h.hardware_id, "ABI-CHECK", sizeof(h.hardware_id) - 1);
        h.unit_id = 7;
        h.active_ant_chs = 5;
        h.ioo_type = 2;
        h.rf_center_freq = 433000000ULL;
        h.adc_sampling_freq = 2400000ULL;
        h.sampling_freq = 1200000ULL;
        h.cpi_length = 1048576U;
        h.time_stamp = 1700000000123ULL;
        h.daq_block_index = 42;
        h.cpi_index = 43;
        h.ext_integration_cntr = 99887766554433ULL;
        h.data_type = 3;
        h.sample_bit_depth = 32;
        h.adc_overdrive_flags = 0x15;
        for (int i = 0; i < 32; i++) h.if_gains[i] = 100 + i;
        h.delay_sync_flag = 1;
        h.iq_sync_flag = 1;
        h.sync_state = 6;
        h.noise_source_state = 1;
        for (int i = 0; i < 192; i++) h.reserved[i] = 0x40000000U + (unsigned)i;
        h.header_version = IQ_HEADER_VERSION;
        FILE *f = fopen(argv[1], "wb");
        if (!f) return 1;
        if (fwrite(&h, sizeof(h), 1, f) != 1) return 1;
        fclose(f);
    }
    return 0;
}
"""


class TesterHeaderABI(unittest.TestCase):
    """Cross-checks iq_header.h against iq_header.py."""

    compiler = None
    c_report = None       # dict name->int from the compiled C program
    golden_bytes = None   # 1024-byte struct written by the C program
    build_error = None

    @classmethod
    def setUpClass(cls):
        cls.compiler = find_compiler()
        if cls.compiler is None:
            return
        tmpdir = tempfile.mkdtemp(prefix="iqh_abi_")
        try:
            offset_lines = "\n".join(
                '    printf("offsetof_{0}=%zu\\n", '
                'offsetof(struct iq_header_struct, {0}));'.format(name)
                for name, _fmt in FIELDS)
            src = join(tmpdir, "abi_check.c")
            with open(src, "w") as f:
                f.write(C_PROGRAM.replace("@OFFSET_LINES@", offset_lines))
            exe = join(tmpdir, "abi_check")
            compile_res = subprocess.run(
                [cls.compiler, "-std=gnu11", "-Wall",
                 "-I", daq_core_path, "-o", exe, src],
                capture_output=True, text=True, timeout=120)
            if compile_res.returncode != 0:
                cls.build_error = compile_res.stderr
                return
            golden = join(tmpdir, "golden.bin")
            run_res = subprocess.run([exe, golden], capture_output=True,
                                     text=True, timeout=60)
            if run_res.returncode != 0:
                cls.build_error = "abi_check runtime rc={} stderr={}".format(
                    run_res.returncode, run_res.stderr)
                return
            report = {}
            for line in run_res.stdout.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    report[k.strip()] = int(v.strip())
            cls.c_report = report
            with open(golden, "rb") as f:
                cls.golden_bytes = f.read()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _require_c(self):
        if self.compiler is None:
            self.skipTest("SKIP reason: no C compiler (cc/gcc/clang) on PATH")
        if self.build_error is not None:
            self.fail("iq_header.h failed to compile/run — C/Python lockstep "
                      "broken?\n" + str(self.build_error))

    # ---------------- pure-Python consistency (always runs) ----------------

    def test_python_format_is_1024_native(self):
        self.assertEqual(struct.calcsize(HEADER_FORMAT), 1024)
        # The FIELDS table above must describe exactly the same format
        offsets, total = python_field_offsets()
        self.assertEqual(total, 1024)
        self.assertEqual(offsets["reserved"], 252)
        self.assertEqual(offsets["header_version"], 1020)

    def test_python_rsv_constants(self):
        expected = {
            "EXT_LNA_GAINS": 0, "TOTAL_GAINS": 32, "SYSTEM_NF_MDB": 64,
            "COMPRESSION_FLAGS": 96, "BIAS_TEE_STATE": 97,
            "ANTENNA_AZ_CDEG": 98, "ANTENNA_EL_CDEG": 99,
            "ROTATOR_STATE": 100, "AGG_POWER_MDB": 101,
            "BUFFER_OVERRUN_CNT": 102,
        }
        for name, value in expected.items():
            self.assertEqual(getattr(IQHeader, "RSV_" + name), value,
                             "IQHeader.RSV_{} drifted".format(name))

    # ---------------- C cross-checks (skip without a compiler) --------------

    def test_struct_size_matches(self):
        self._require_c()
        self.assertEqual(self.c_report["sizeof"], 1024)
        self.assertEqual(self.c_report["IQ_HEADER_LENGTH"], 1024)

    def test_all_field_offsets_match(self):
        self._require_c()
        py_offsets, _total = python_field_offsets()
        for name, _fmt in FIELDS:
            self.assertEqual(
                self.c_report["offsetof_" + name], py_offsets[name],
                "offset mismatch for field '{}': C={} Python={}".format(
                    name, self.c_report["offsetof_" + name], py_offsets[name]))

    def test_constants_match(self):
        self._require_c()
        self.assertEqual(self.c_report["SYNC_WORD"], IQHeader.SYNC_WORD)
        self.assertEqual(self.c_report["IQ_HEADER_VERSION"],
                         IQHeader.HEADER_VERSION)
        for ft in ("DATA", "DUMMY", "RAMP", "CAL", "TRIGW"):
            self.assertEqual(self.c_report["FRAME_TYPE_" + ft],
                             getattr(IQHeader, "FRAME_TYPE_" + ft))

    def test_rsv_slot_defines_match(self):
        self._require_c()
        for name in RSV_SLOTS:
            self.assertEqual(
                self.c_report["RSV_" + name],
                getattr(IQHeader, "RSV_" + name),
                "IQH_RSV_{0} != IQHeader.RSV_{0}".format(name))

    def test_golden_frame_roundtrip(self):
        """A struct filled and written by the C compiler decodes to the same
        values through IQHeader.decode_header — locks padding, not just
        offsets."""
        self._require_c()
        self.assertEqual(len(self.golden_bytes), 1024)
        h = IQHeader()
        h.decode_header(self.golden_bytes)
        self.assertEqual(h.check_sync_word(), 0)
        self.assertEqual(h.frame_type, IQHeader.FRAME_TYPE_CAL)
        self.assertEqual(h.hardware_id.rstrip("\x00"), "ABI-CHECK")
        self.assertEqual(h.unit_id, 7)
        self.assertEqual(h.active_ant_chs, 5)
        self.assertEqual(h.ioo_type, 2)
        self.assertEqual(h.rf_center_freq, 433000000)
        self.assertEqual(h.adc_sampling_freq, 2400000)
        self.assertEqual(h.sampling_freq, 1200000)
        self.assertEqual(h.cpi_length, 1048576)
        self.assertEqual(h.time_stamp, 1700000000123)
        self.assertEqual(h.daq_block_index, 42)
        self.assertEqual(h.cpi_index, 43)
        self.assertEqual(h.ext_integration_cntr, 99887766554433)
        self.assertEqual(h.data_type, 3)
        self.assertEqual(h.sample_bit_depth, 32)
        self.assertEqual(h.adc_overdrive_flags, 0x15)
        self.assertEqual(list(h.if_gains), [100 + i for i in range(32)])
        self.assertEqual(h.delay_sync_flag, 1)
        self.assertEqual(h.iq_sync_flag, 1)
        self.assertEqual(h.sync_state, 6)
        self.assertEqual(h.noise_source_state, 1)
        self.assertEqual(list(h.reserved),
                         [0x40000000 + i for i in range(192)])
        self.assertEqual(h.header_version, IQHeader.HEADER_VERSION)

    def test_python_encode_matches_c_layout(self):
        """Encoding the golden values from Python must reproduce the exact
        bytes the C compiler wrote (bidirectional ABI lock)."""
        self._require_c()
        h = IQHeader()
        h.decode_header(self.golden_bytes)
        # decode->encode round trip must be byte-identical (padding bytes were
        # zeroed by memset on the C side, and struct.pack zeroes them too)
        self.assertEqual(h.encode_header(), self.golden_bytes)


if __name__ == "__main__":
    unittest.main()
