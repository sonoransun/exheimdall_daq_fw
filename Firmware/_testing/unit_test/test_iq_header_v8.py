"""
    Unit test for IQ header v8: the reserved-region telemetry carve.

    Verifies the header is still exactly 1024 bytes, that reserved slots
    round-trip through encode/decode, and that the named accessors work.

    No sudo, no hardware.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import unittest
import sys
from os.path import join, dirname, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

from iq_header import IQHeader


class TestIQHeaderV8(unittest.TestCase):

    def test_header_is_1024_bytes(self):
        h = IQHeader()
        self.assertEqual(len(h.encode_header()), 1024)

    def test_reserved_roundtrip_default_zero(self):
        # A freshly encoded header decodes back with all-zero reserved slots.
        h = IQHeader()
        h.hardware_id = "kraken5"
        dec = IQHeader()
        dec.decode_header(h.encode_header())
        self.assertEqual(len(dec.reserved), 192)
        self.assertTrue(all(v == 0 for v in dec.reserved))

    def test_gain_telemetry_roundtrip(self):
        h = IQHeader()
        h.hardware_id = "kraken5"
        h.header_version = IQHeader.HEADER_VERSION
        h.set_ext_lna_gains([200, 201, 202, 203, 204])       # dB x10
        h.set_total_system_gains([350, 351, 352, 353, 354])  # dB x10
        h.set_system_nf_mdb([1500, 1510, 1520, 1530, 1540])  # milli-dB
        h.set_compression_flags(0b101)

        dec = IQHeader()
        dec.decode_header(h.encode_header())
        self.assertEqual(dec.header_version, 8)
        self.assertEqual(dec.reserved[IQHeader.RSV_EXT_LNA_GAINS:IQHeader.RSV_EXT_LNA_GAINS + 5],
                         [200, 201, 202, 203, 204])
        self.assertEqual(dec.reserved[IQHeader.RSV_TOTAL_GAINS:IQHeader.RSV_TOTAL_GAINS + 5],
                         [350, 351, 352, 353, 354])
        self.assertEqual(dec.reserved[IQHeader.RSV_SYSTEM_NF_MDB:IQHeader.RSV_SYSTEM_NF_MDB + 5],
                         [1500, 1510, 1520, 1530, 1540])
        self.assertEqual(dec.reserved[IQHeader.RSV_COMPRESSION_FLAGS], 0b101)

    def test_bearing_roundtrip(self):
        h = IQHeader()
        h.set_antenna_bearing(137.25, -12.5)
        h.set_rotator_state(2)
        dec = IQHeader()
        dec.decode_header(h.encode_header())
        az, el = dec.get_antenna_bearing()
        self.assertAlmostEqual(az, 137.25, places=2)
        self.assertAlmostEqual(el, -12.5, places=2)
        self.assertEqual(dec.get_rotator_state(), 2)

    def test_aggregate_power_roundtrip(self):
        h = IQHeader()
        h.set_aggregate_power_mdb(-42000)  # -42.0 dB in milli-dB (stored as uint32 wrap)
        dec = IQHeader()
        dec.decode_header(h.encode_header())
        # The getter sign-extends the two's-complement uint32 slot itself
        self.assertEqual(dec.get_aggregate_power_mdb(), -42000)
        # The wire encoding is unchanged: the raw slot still holds the wrap
        self.assertEqual(dec.reserved[IQHeader.RSV_AGG_POWER_MDB],
                         -42000 & 0xFFFFFFFF)

    def test_signed_getters_sign_extend(self):
        # Negative telemetry (net loss, negative NF corrections, negative az)
        # must round-trip through the uint32 slots via the getters.
        h = IQHeader()
        h.set_ext_lna_gains([-50, 200, -1])           # dB x10, ch0 = -5 dB loss
        h.set_total_system_gains([-120, 350, 0])
        h.set_system_nf_mdb([-500, 1500, 0])
        h.set_antenna_bearing(-10.5, -12.25)
        dec = IQHeader()
        dec.decode_header(h.encode_header())
        self.assertEqual(dec.get_ext_lna_gains(3), [-50, 200, -1])
        self.assertEqual(dec.get_total_system_gains(3), [-120, 350, 0])
        self.assertEqual(dec.get_system_nf_mdb(3), [-500, 1500, 0])
        az, el = dec.get_antenna_bearing()
        self.assertAlmostEqual(az, -10.5, places=2)
        self.assertAlmostEqual(el, -12.25, places=2)

    def test_unsigned_getters(self):
        h = IQHeader()
        h.set_compression_flags(0b1010)
        h.set_bias_tee_state(0b0110)
        dec = IQHeader()
        dec.decode_header(h.encode_header())
        self.assertEqual(dec.get_compression_flags(), 0b1010)
        self.assertEqual(dec.get_bias_tee_state(), 0b0110)

    def test_buffer_overrun_cnt_slot(self):
        # v8 carve extension: slot 102 = cumulative USB ring-buffer overrun
        # count stamped by rtl_daq (unsigned; 0 = none/unsupported).
        self.assertEqual(IQHeader.RSV_BUFFER_OVERRUN_CNT, 102)
        h = IQHeader()
        self.assertEqual(h.get_buffer_overrun_cnt(), 0)
        h.set_buffer_overrun_cnt(4000000000)  # > 2^31: stays unsigned
        dec = IQHeader()
        dec.decode_header(h.encode_header())
        self.assertEqual(dec.get_buffer_overrun_cnt(), 4000000000)
        self.assertEqual(dec.reserved[IQHeader.RSV_BUFFER_OVERRUN_CNT], 4000000000)

    def test_existing_fields_preserved(self):
        # Sanity: the carve must not disturb the classic header fields.
        h = IQHeader()
        h.hardware_id = "kraken5"
        h.rf_center_freq = 700000000
        h.cpi_index = 1234
        h.sync_state = 6
        h.if_gains = list(range(32))
        h.set_total_system_gains([100, 100, 100, 100, 100])
        dec = IQHeader()
        dec.decode_header(h.encode_header())
        self.assertEqual(dec.rf_center_freq, 700000000)
        self.assertEqual(dec.cpi_index, 1234)
        self.assertEqual(dec.sync_state, 6)
        self.assertEqual(list(dec.if_gains), list(range(32)))
        self.assertEqual(dec.check_sync_word(), 0)


if __name__ == "__main__":
    unittest.main()
