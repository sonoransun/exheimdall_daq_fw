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

    # ---- scaling / clustering slots 103-113 ----

    NEW_SLOTS = {
        "RSV_ROUTER_SEQ": 103, "RSV_ROUTER_RX_TS_LO": 104,
        "RSV_ROUTER_RX_TS_HI": 105, "RSV_SOURCE_INSTANCE": 106,
        "RSV_SOURCE_KEY_CRC32": 107, "RSV_NODE_ID_CRC32": 108,
        "RSV_DECIMATOR_DROP_CNT": 109, "RSV_CAL_QUALITY_MDB": 110,
        "RSV_IQ_COMP_CNT": 111, "RSV_SAMPLE_COMP_CNT": 112,
        "RSV_RECAL_GEN": 113,
    }

    # (setter, getter, slot) for the plain unsigned u32 slots
    U32_ACCESSORS = [
        ("set_router_seq", "get_router_seq", 103),
        ("set_source_instance", "get_source_instance", 106),
        ("set_source_key_crc32", "get_source_key_crc32", 107),
        ("set_node_id_crc32", "get_node_id_crc32", 108),
        ("set_decimator_drop_cnt", "get_decimator_drop_cnt", 109),
        ("set_iq_comp_cnt", "get_iq_comp_cnt", 111),
        ("set_sample_comp_cnt", "get_sample_comp_cnt", 112),
        ("set_recal_gen", "get_recal_gen", 113),
    ]

    def test_new_slot_constants(self):
        for name, value in self.NEW_SLOTS.items():
            self.assertEqual(getattr(IQHeader, name), value, name)
        # header_version is NOT bumped by the new slots
        self.assertEqual(IQHeader.HEADER_VERSION, 8)

    def test_fresh_header_encodes_zero_in_new_slots(self):
        h = IQHeader()
        h.hardware_id = "kraken5"
        h.header_version = IQHeader.HEADER_VERSION
        raw = h.encode_header()
        # bytes 252 + 4*103 .. 252 + 4*114 are all zero on the wire
        self.assertEqual(raw[252 + 4 * 103:252 + 4 * 114], bytes(4 * 11))
        dec = IQHeader()
        dec.decode_header(raw)
        for k in range(103, 114):
            self.assertEqual(dec.reserved[k], 0, "slot {}".format(k))
        for _set, get, _slot in self.U32_ACCESSORS:
            self.assertEqual(getattr(dec, get)(), 0, get)
        self.assertEqual(dec.get_router_rx_ts_ms(), 0)
        self.assertEqual(dec.get_cal_quality_mdb(), 0)

    def test_u32_accessors_roundtrip(self):
        for i, (setter, getter, slot) in enumerate(self.U32_ACCESSORS):
            value = 0xF0000000 + 1000 * i + 7   # > 2^31: must stay unsigned
            h = IQHeader()
            getattr(h, setter)(value)
            dec = IQHeader()
            dec.decode_header(h.encode_header())
            self.assertEqual(getattr(dec, getter)(), value, getter)
            self.assertEqual(dec.reserved[slot], value, getter)
            # exactly one reserved word set
            self.assertEqual(sum(1 for v in dec.reserved if v), 1, setter)

    def test_u32_accessors_wrap(self):
        # Counters wrap modulo 2^32 rather than raising in the hot loop
        for setter, getter, _slot in self.U32_ACCESSORS:
            h = IQHeader()
            getattr(h, setter)((1 << 32) + 5)
            self.assertEqual(getattr(h, getter)(), 5, setter)
            getattr(h, setter)(-1)
            self.assertEqual(getattr(h, getter)(), 0xFFFFFFFF, setter)

    def test_router_rx_ts_u64_split(self):
        ts = 1761912345678  # 2025-10-31 in Unix ms, > 2^32
        h = IQHeader()
        h.set_router_rx_ts_ms(ts)
        self.assertEqual(h.reserved[IQHeader.RSV_ROUTER_RX_TS_LO], ts & 0xFFFFFFFF)
        self.assertEqual(h.reserved[IQHeader.RSV_ROUTER_RX_TS_HI], ts >> 32)
        dec = IQHeader()
        dec.decode_header(h.encode_header())
        self.assertEqual(dec.get_router_rx_ts_ms(), ts)
        # full u64 range
        h.set_router_rx_ts_ms(0xFFFFFFFFFFFFFFFF)
        dec.decode_header(h.encode_header())
        self.assertEqual(dec.get_router_rx_ts_ms(), 0xFFFFFFFFFFFFFFFF)
        # setting the timestamp touches only slots 104/105
        h = IQHeader()
        h.set_router_rx_ts_ms(ts)
        nonzero = [k for k, v in enumerate(h.reserved) if v]
        self.assertEqual(nonzero, [104, 105])

    def test_cal_quality_signed(self):
        for mdb in (0, 1, -1, 23456, -12345, 2147483647, -2147483648):
            h = IQHeader()
            h.set_cal_quality_mdb(mdb)
            dec = IQHeader()
            dec.decode_header(h.encode_header())
            self.assertEqual(dec.get_cal_quality_mdb(), mdb, mdb)
            # wire encoding is two's complement uint32
            self.assertEqual(dec.reserved[IQHeader.RSV_CAL_QUALITY_MDB],
                             mdb & 0xFFFFFFFF)

    def test_cal_quality_float_round_and_saturate(self):
        h = IQHeader()
        # delay_sync stamps round(_cal_quality * 1000) from a float dB value
        h.set_cal_quality_mdb(-3.2177 * 1000)
        self.assertEqual(h.get_cal_quality_mdb(), -3218)
        h.set_cal_quality_mdb(27.4994 * 1000)
        self.assertEqual(h.get_cal_quality_mdb(), 27499)
        # out of int32 range saturates instead of wrapping into a wrong sign
        h.set_cal_quality_mdb(float("inf"))
        self.assertEqual(h.get_cal_quality_mdb(), 2147483647)
        h.set_cal_quality_mdb(-float("inf"))
        self.assertEqual(h.get_cal_quality_mdb(), -2147483648)
        h.set_cal_quality_mdb(5e12)
        self.assertEqual(h.get_cal_quality_mdb(), 2147483647)
        h.set_cal_quality_mdb(1 << 40)
        self.assertEqual(h.get_cal_quality_mdb(), 2147483647)
        # NaN (no valid quality) is stored as 0
        h.set_cal_quality_mdb(float("nan"))
        self.assertEqual(h.get_cal_quality_mdb(), 0)

    def test_new_slots_do_not_disturb_v8_slots(self):
        h = IQHeader()
        h.set_buffer_overrun_cnt(9)
        h.set_aggregate_power_mdb(-42000)
        h.set_ext_lna_gains([200] * 32)
        h.set_router_seq(1)
        h.set_router_rx_ts_ms(2)
        h.set_source_instance(3)
        h.set_source_key_crc32(4)
        h.set_node_id_crc32(5)
        h.set_decimator_drop_cnt(6)
        h.set_cal_quality_mdb(-7)
        h.set_iq_comp_cnt(8)
        h.set_sample_comp_cnt(9)
        h.set_recal_gen(10)
        dec = IQHeader()
        dec.decode_header(h.encode_header())
        self.assertEqual(dec.get_buffer_overrun_cnt(), 9)
        self.assertEqual(dec.get_aggregate_power_mdb(), -42000)
        self.assertEqual(dec.get_ext_lna_gains(), [200] * 32)
        self.assertEqual(
            [dec.get_router_seq(), dec.get_router_rx_ts_ms(),
             dec.get_source_instance(), dec.get_source_key_crc32(),
             dec.get_node_id_crc32(), dec.get_decimator_drop_cnt(),
             dec.get_cal_quality_mdb(), dec.get_iq_comp_cnt(),
             dec.get_sample_comp_cnt(), dec.get_recal_gen()],
            [1, 2, 3, 4, 5, 6, -7, 8, 9, 10])
        # slots 114..191 stay zero
        self.assertTrue(all(v == 0 for v in dec.reserved[114:]))

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
