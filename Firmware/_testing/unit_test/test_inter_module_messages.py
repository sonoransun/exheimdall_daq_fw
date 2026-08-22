"""
    Description :
    Wire-format round-trip tests for the inter-module ZMQ control messages
    (inter_module_messages.py).

    These are the 128-byte tuner-control frames parsed by rtl_daq.c
    (struct hdaq_im_msg_struct):
        byte 0      : source_module_identifier (int8)
        byte 1      : command char ('r'/'c'/'g'/'a'/'s'/'n'/'b')
        bytes 2..127: parameters (rtl_daq.c casts &parameters[0] directly, so
                      multi-byte fields start at byte offset 2, little-endian
                      on every supported target)

    Every pack_msg_* is asserted to produce exactly 128 bytes with fields
    recoverable at the same offsets rtl_daq.c reads them from, and all-zero
    padding after the payload.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import sys
import unittest
from os.path import join, dirname, realpath
from struct import unpack

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

import inter_module_messages as imm  # noqa: E402

MSG_LEN = 128
PARAMS_OFFSET = 2  # parameters[] starts right after id + command char


class TesterInterModuleMessages(unittest.TestCase):

    def _common_checks(self, msg, module_id, command_char, payload_len):
        """Length, module id, command char, and zero padding after payload."""
        self.assertIsInstance(msg, (bytes, bytearray))
        self.assertEqual(len(msg), MSG_LEN)
        self.assertEqual(unpack("b", msg[0:1])[0], module_id)
        self.assertEqual(msg[1:2], command_char.encode("ascii"))
        pad = msg[PARAMS_OFFSET + payload_len:]
        self.assertEqual(pad, bytes(len(pad)),
                         "padding after the payload must be all zeros")

    def test_reconfiguration(self):
        """'r': uint32 center_freq, uint32 sample_rate, uint32 gain at
        parameters[0..2] (rtl_daq.c casts to uint32_t*)."""
        msg = imm.pack_msg_reconfiguration(4, 433920000, 2400000, 158)
        self._common_checks(msg, 4, "r", 12)
        freq, fs, gain = unpack("<III", msg[2:14])
        self.assertEqual(freq, 433920000)
        self.assertEqual(fs, 2400000)
        self.assertEqual(gain, 158)

    def test_rf_tune(self):
        """'c': uint32 center frequency at parameters[0]."""
        msg = imm.pack_msg_rf_tune(6, 868000000)
        self._common_checks(msg, 6, "c", 4)
        self.assertEqual(unpack("<I", msg[2:6])[0], 868000000)

    def test_rf_tune_max_uint32(self):
        """The 'c' payload is uint32: the maximum representable frequency
        must round-trip (larger values are rejected upstream by
        hw_controller before packing)."""
        msg = imm.pack_msg_rf_tune(6, 2**32 - 1)
        self.assertEqual(unpack("<I", msg[2:6])[0], 2**32 - 1)

    def test_set_gain(self):
        """'g': M x uint32 tenth-dB gains at parameters[0..M-1]."""
        gains = [0, 87, 297, 496, 144]
        msg = imm.pack_msg_set_gain(6, gains)
        self._common_checks(msg, 6, "g", 4 * len(gains))
        got = unpack("<" + "I" * len(gains), msg[2:2 + 4 * len(gains)])
        self.assertEqual(list(got), gains)

    def test_enable_agc(self):
        """'a': no payload."""
        msg = imm.pack_msg_enable_agc(6)
        self._common_checks(msg, 6, "a", 0)

    def test_noise_source_on_off(self):
        """'n': single byte at parameters[0] (rtl_daq.c reads
        msg->parameters[0] directly)."""
        msg_on = imm.pack_msg_noise_source_ctr(6, True)
        self._common_checks(msg_on, 6, "n", 1)
        self.assertEqual(msg_on[2], 1)

        msg_off = imm.pack_msg_noise_source_ctr(6, False)
        self._common_checks(msg_off, 6, "n", 1)
        self.assertEqual(msg_off[2], 0)

    def test_sample_freq_tune(self):
        """'s': M x float32 ppm offsets at parameters[0..M-1]."""
        offsets = [0.0, -1.25, 3.5, 0.0078125]  # exactly representable
        msg = imm.pack_msg_sample_freq_tune(2, offsets)
        self._common_checks(msg, 2, "s", 4 * len(offsets))
        got = unpack("<" + "f" * len(offsets), msg[2:2 + 4 * len(offsets)])
        for expected, actual in zip(offsets, got):
            self.assertEqual(actual, expected)

    def test_set_bias_tee(self):
        """'b': M x uint32 states (normalized to 0/1) at parameters[0..M-1]."""
        msg = imm.pack_msg_set_bias_tee(6, [1, 0, 7, True, False])
        self._common_checks(msg, 6, "b", 20)
        got = unpack("<5I", msg[2:22])
        # Truthy states are normalized to exactly 1 for the C side
        self.assertEqual(list(got), [1, 0, 1, 1, 0])

    def test_negative_module_identifier(self):
        """Module identifier is a signed byte on the wire."""
        msg = imm.pack_msg_enable_agc(-1)
        self.assertEqual(unpack("b", msg[0:1])[0], -1)

    def test_all_messages_are_128_bytes(self):
        """Every exported pack_msg_* returns exactly 128 bytes (frozen ZMQ
        wire contract with rtl_daq.c)."""
        samples = [
            imm.pack_msg_reconfiguration(1, 100000000, 2400000, 0),
            imm.pack_msg_rf_tune(1, 100000000),
            imm.pack_msg_set_gain(1, [0] * 31),  # parameters[126] fits 31 u32
            imm.pack_msg_enable_agc(1),
            imm.pack_msg_noise_source_ctr(1, True),
            imm.pack_msg_sample_freq_tune(1, [0.0] * 31),
            imm.pack_msg_set_bias_tee(1, [0] * 31),
        ]
        for msg in samples:
            self.assertEqual(len(msg), MSG_LEN)


if __name__ == "__main__":
    unittest.main()
