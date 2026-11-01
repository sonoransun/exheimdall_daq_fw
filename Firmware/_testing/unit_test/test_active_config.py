"""
    Unit tests for the active configuration schema (Firmware/_active/active_config.py).

    No sudo, no hardware. Configs come from parse_config_text / default_config.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import dataclasses
import json
import logging
import os
import sys
import tempfile
import textwrap
import unittest
from os.path import dirname, join, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_config as C  # noqa: E402
from active_common import ChannelRef, ConfigError, has_errors  # noqa: E402

logging.getLogger("antenna_profile").setLevel(logging.ERROR)


def parse(text, base_dir=None):
    return C.parse_config_text(textwrap.dedent(text), base_dir or tempfile.gettempdir())


def codes(issues, level=None):
    return [i.code for i in issues if level is None or i.level == level]


TWO_DEV = """
[active]
backend = uhd
[active_devices]
devices = n321a, n320b
[active_device:n321a]
family = n321
addr = 192.168.20.2
rx_channels = 0,1
tx_channels = 0,1
[active_device:n320b]
family = n320
addr = 192.168.20.3
rx_channels = 0,1
tx_channels = 1
"""


class TestDefaults(unittest.TestCase):
    def test_empty_file_defaults(self):
        cfg = parse("")
        self.assertFalse(cfg.tx.en_tx)
        self.assertEqual(cfg.active.mode, "rx")
        self.assertEqual(cfg.active.backend, "sim")
        self.assertFalse(cfg.active.en_active)
        self.assertFalse(cfg.tx_enabled())
        self.assertEqual(cfg.meta.active_ini_version, 1)
        self.assertEqual(cfg.rf.center_freq_hz, 915e6)
        self.assertEqual(cfg.rf.cpi_samples, 65536)
        self.assertEqual(cfg.tx.allowed_modes, ("dummy_load",))
        self.assertEqual(cfg.tx.max_digital_amplitude, 0.5)
        self.assertEqual(cfg.output.iq_port, 5050)
        self.assertEqual(cfg.sim.seed, 12345)
        self.assertEqual(cfg.sim.emitters[0], C.SimEmitter(30.0, 0.0, 0.0, -60.0, "noise", 0.0))
        self.assertIsNone(cfg.antenna_profile)
        self.assertEqual(cfg.parse_issues, ())
        # implicit sim device: one RX per preset element, no TX
        self.assertEqual(len(cfg.devices), 1)
        self.assertEqual(cfg.devices[0].name, "sim0")
        self.assertEqual(cfg.devices[0].rx_channels, (0, 1, 2, 3))
        self.assertEqual(cfg.devices[0].tx_channels, ())
        self.assertEqual(cfg.rf.rx_gain_db, (30.0,) * 4)
        self.assertEqual(cfg.num_tx(), 0)
        self.assertEqual(C.validate_config(cfg), [])

    def test_default_config_overrides(self):
        cfg = C.default_config(active={"mode": "trx"}, rf={"rx_gain_db": [10, 20]},
                               devices={"sim0": {"family": "sim", "rx_channels": "0,1",
                                                 "tx_channels": "0,1"}},
                               tx={"en_tx": True, "allow_bands": [(902e6, 928e6)]})
        self.assertEqual(cfg.active.mode, "trx")
        self.assertEqual(cfg.rf.rx_gain_db, (10.0, 20.0))
        self.assertEqual(cfg.tx.allow_bands, ((902e6, 928e6),))
        self.assertTrue(cfg.tx_enabled())
        with self.assertRaises(ConfigError):
            C.default_config(bogus={"x": 1})

    def test_ura_preset_sim_channels(self):
        cfg = parse("[active_geometry]\npreset = ura\nnx = 2\nny = 3\n")
        self.assertEqual(cfg.devices[0].rx_channels, tuple(range(6)))

    def test_geometry_file_needs_devices(self):
        with self.assertRaises(ConfigError) as cm:
            parse("[active_geometry]\ngeometry_file = g.json\n")
        self.assertIn("REQUIRED", codes(cm.exception.issues))

    def test_hardware_backend_needs_devices(self):
        with self.assertRaises(ConfigError):
            parse("[active]\nbackend = uhd\n")


class TestDevicesAndChannels(unittest.TestCase):
    def test_order_and_refs(self):
        cfg = parse(TWO_DEV)
        self.assertEqual(cfg.device_names(), ["n321a", "n320b"])
        self.assertEqual(cfg.rx_refs(), [ChannelRef("n321a", 0), ChannelRef("n321a", 1),
                                         ChannelRef("n320b", 0), ChannelRef("n320b", 1)])
        self.assertEqual(cfg.tx_refs(), [ChannelRef("n321a", 0), ChannelRef("n321a", 1),
                                         ChannelRef("n320b", 1)])
        self.assertEqual((cfg.num_rx(), cfg.num_tx()), (4, 3))
        self.assertEqual(cfg.device("n320b").addr, "192.168.20.3")
        self.assertEqual(cfg.master_device().name, "n321a")
        with self.assertRaises(ConfigError):
            cfg.device("nope")
        self.assertEqual(cfg.resolved_hardware_id(), "ACT-N321")
        self.assertEqual(cfg.resolved_hardware_id(simulation=True), "SIM-N321")

    def test_device_defaults_and_ranges(self):
        cfg = parse("""
        [active_devices]
        devices = a, b
        [active_device:a]
        family = sim
        [active_device:b]
        family = sim
        tx_channels = 0..2
        pervices_tree = /mboards/0/x=1; /y=2
        max_tx_dbm_override = 5
        """)
        self.assertEqual(cfg.device("a").rx_channels, (0,))
        self.assertEqual(cfg.device("b").rx_channels, ())
        self.assertEqual(cfg.device("b").tx_channels, (0, 1, 2))
        self.assertEqual(cfg.device("b").pervices_tree, (("/mboards/0/x", "1"), ("/y", "2")))
        self.assertEqual(cfg.device("b").max_tx_dbm_override, 5.0)
        self.assertIsNone(cfg.device("a").max_tx_dbm_override)

    def test_missing_family_and_unknown_family(self):
        with self.assertRaises(ConfigError) as cm:
            parse("[active_devices]\ndevices = a\n[active_device:a]\naddr = x\n")
        self.assertIn("REQUIRED", codes(cm.exception.issues))
        with self.assertRaises(ConfigError) as cm:
            parse("[active_devices]\ndevices = a\n[active_device:a]\nfamily = hackrf\n")
        self.assertIn("ENUM", codes(cm.exception.issues))
        with self.assertRaises(ConfigError):
            parse("[active_devices]\ndevices = a\n")      # section missing
        with self.assertRaises(ConfigError):
            parse("[active_devices]\ndevices = a, a\n[active_device:a]\nfamily = sim\n")

    def test_unknown_backend_is_parse_error(self):
        with self.assertRaises(ConfigError) as cm:
            parse("[active]\nbackend = soapy\n")
        self.assertIn("ENUM", codes(cm.exception.issues))

    def test_unknown_emulate_family_is_parse_error(self):
        with self.assertRaises(ConfigError):
            parse("[active_sim]\nemulate_family = nope\n")

    def test_unlisted_device_section_warns(self):
        cfg = parse("[active_devices]\ndevices = a\n[active_device:a]\nfamily = sim\n"
                    "[active_device:b]\nfamily = sim\n")
        self.assertIn("UNKNOWN_KEY", codes(cfg.parse_issues, "warning"))


class TestLists(unittest.TestCase):
    def test_broadcast(self):
        cfg = parse(TWO_DEV + "[active_rf]\nrx_gain_db = 20\n[active_tx]\nmax_chain_power_dbm = 10\n")
        self.assertEqual(cfg.rf.rx_gain_db, (20.0,) * 4)
        self.assertEqual(cfg.tx.max_chain_power_dbm, (10.0,) * 3)
        self.assertEqual(cfg.tx.max_tx_gain_db, (None,) * 3)     # "" -> caps MIN, resolved later

    def test_exact_length(self):
        cfg = parse(TWO_DEV + "[active_rf]\nrx_gain_db = 1,2,3,4\n[active_tx]\nmax_tx_gain_db = 5,6,7\n")
        self.assertEqual(cfg.rf.rx_gain_db, (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(cfg.tx.max_tx_gain_db, (5.0, 6.0, 7.0))

    def test_list_len_error(self):
        with self.assertRaises(ConfigError) as cm:
            parse(TWO_DEV + "[active_rf]\nrx_gain_db = 1,2\n")
        self.assertIn("LIST_LEN", codes(cm.exception.issues))
        with self.assertRaises(ConfigError) as cm:
            parse(TWO_DEV + "[active_tx]\ndummy_load_rating_w = 1,2,3,4\n")
        self.assertIn("LIST_LEN", codes(cm.exception.issues))

    def test_list_len_programmatic(self):
        cfg = parse(TWO_DEV)
        bad = dataclasses.replace(cfg, rf=dataclasses.replace(cfg.rf, rx_gain_db=(1.0,)))
        self.assertIn("LIST_LEN", codes(C.validate_config(bad), "error"))

    def test_bands_dirs_emitters(self):
        cfg = parse("""
        [active_tx]
        allow_bands = 902e6:928e6;2400e6:2483.5e6
        [active_beam]
        nulls = 75:0;120:10
        [active_sim]
        emitters = 30:0:0:-60:noise;75:5:100:-70:tone:25000
        iq_conjugate_devices = sim0
        """)
        self.assertEqual(cfg.tx.allow_bands, ((902e6, 928e6), (2400e6, 2483.5e6)))
        self.assertEqual(cfg.beam.nulls, ((75.0, 0.0), (120.0, 10.0)))
        self.assertEqual(cfg.sim.emitters[1], C.SimEmitter(75.0, 5.0, 100.0, -70.0, "tone", 25000.0))
        self.assertEqual(cfg.sim.iq_conjugate_devices, ("sim0",))
        with self.assertRaises(ConfigError):
            parse("[active_sim]\nemitters = 30:0:0\n")

    def test_inline_semicolon_truncation_detected(self):
        with self.assertRaises(ConfigError) as cm:
            parse("[active_tx]\ndeny_bands = 1e9:2e9 ; 3e9:4e9\n")
        self.assertIn("INLINE_COMMENT", codes(cm.exception.issues))
        cfg = parse("[active_beam]\nnulls = 75:0 ; 120:10\n")
        self.assertIn("INLINE_COMMENT", codes(cfg.parse_issues, "warning"))
        self.assertEqual(cfg.beam.nulls, ((75.0, 0.0),))        # what configparser kept
        # [active_tx] is strict: an inline comment on ANY key is an error ...
        with self.assertRaises(ConfigError) as cm:
            parse("[active_tx]\nen_tx = 0 ; disabled\n")
        self.assertIn("INLINE_COMMENT", codes(cm.exception.issues, "error"))
        # ... elsewhere a trailing comment on a plain (non-list) key is fine
        cfg = parse("[active_rf]\ncenter_freq_hz = 915e6 ; ISM\n")
        self.assertEqual(cfg.parse_issues, ())
        self.assertEqual(cfg.rf.center_freq_hz, 915e6)

    def test_inline_comment_on_per_chain_tx_list_is_fatal(self):
        # ';' (as in the band lists beside it) would truncate to the first value,
        # which would then be broadcast to every chain and loosen chain 1's limit.
        for line in ("dummy_load_rating_w = 100 ; 5", "max_chain_power_dbm = 30 ; 0",
                     "dummy_load_pep_w = 100 ; 5", "max_tx_gain_db = 20 # 0",
                     "cabled_rx_path_loss_db = 40 ; 30", "loopback_path_loss_db = 40 ; 30"):
            with self.assertRaises(ConfigError, msg=line) as cm:
                parse("[active_tx]\n%s\n" % line)
            self.assertIn("INLINE_COMMENT", codes(cm.exception.issues, "error"), line)
        with self.assertRaises(ConfigError) as cm:
            parse("[active_tx]\nmax_chain_power_dbm = 30,\n    ; 0\n")
        self.assertIn("INLINE_COMMENT", codes(cm.exception.issues, "error"))
        two_tx = ("[active_devices]\ndevices = s\n[active_device:s]\nfamily = sim\n"
                  "tx_channels = 0,1\n")
        cfg = parse(two_tx + "[active_tx]\nmax_chain_power_dbm = 30,0\n")   # correct spelling
        self.assertEqual(cfg.tx.max_chain_power_dbm, (30.0, 0.0))


class TestUnknownKeys(unittest.TestCase):
    def test_unknown_tx_key_is_fatal(self):
        with self.assertRaises(ConfigError) as cm:
            parse("[active_tx]\nmax_burst_sec = 5\n")
        self.assertIn("UNKNOWN_KEY", codes(cm.exception.issues, "error"))

    def test_unknown_key_elsewhere_warns(self):
        cfg = parse("[active_rf]\ncenter_freq = 1e9\n[hw]\nnum_ch = 5\n")
        self.assertEqual(codes(cfg.parse_issues), ["UNKNOWN_KEY", "UNKNOWN_KEY"])
        self.assertEqual(cfg.rf.center_freq_hz, 915e6)
        self.assertIn("UNKNOWN_KEY", codes(C.validate_config(cfg), "warning"))

    def test_bool_spelling_warns(self):
        cfg = parse("[active]\nen_active = yes\n")
        self.assertTrue(cfg.active.en_active)
        self.assertIn("BOOL_SPELLING", codes(cfg.parse_issues))

    def test_wrong_types(self):
        for text in ("[active_rf]\ncenter_freq_hz = abc\n", "[active_tx]\nen_tx = maybe\n",
                     "[active_rf]\ncpi_samples = 1.5\n", "[active]\ninstance_id = x\n",
                     "[active_tx]\nallow_bands = 928e6:902e6\n"):
            with self.assertRaises(ConfigError, msg=text):
                parse(text)

    def test_syntax_error(self):
        with self.assertRaises(ConfigError) as cm:
            parse("[active\nx = 1\n")
        self.assertEqual(cm.exception.issues[0].code, "SYNTAX")
        with self.assertRaises(ConfigError):
            parse("[active]\nmode = rx\nmode = tx\n")


class TestParserHardening(unittest.TestCase):
    """The strict [active_tx] section cannot be bypassed by configparser quirks."""

    def assertFatal(self, text, code):
        with self.assertRaises(ConfigError, msg=text) as cm:
            C.parse_config_text(text, tempfile.gettempdir())
        self.assertIn(code, codes(cm.exception.issues, "error"), cm.exception.issues)
        return cm.exception.issues

    def test_inline_comment_on_continuation_line(self):
        text = ("[active_tx]\ndeny_bands = 1000e6:1100e6;\n    1200e6:1300e6 ; 1400e6:1500e6\n")
        self.assertFatal(text, "INLINE_COMMENT")
        text = "[active_tx]\ndeny_bands = 1e9:2e9;\n    3e9:4e9 ; 5e9:6e9\n"
        self.assertFatal(text, "INLINE_COMMENT")
        # elsewhere: a warning, and the value configparser kept
        cfg = C.parse_config_text("[active_beam]\nnulls = 75:0;\n    120:10 ; 130:5\n", ".")
        self.assertIn("INLINE_COMMENT", codes(cfg.parse_issues, "warning"))
        # a correctly wrapped multi-line list parses cleanly
        cfg = C.parse_config_text("[active_tx]\ndeny_bands = 1e9:2e9;\n    3e9:4e9;\n"
                                  "    5e9:6e9\n", ".")
        self.assertEqual(cfg.tx.deny_bands, ((1e9, 2e9), (3e9, 4e9), (5e9, 6e9)))
        self.assertNotIn("INLINE_COMMENT", codes(cfg.parse_issues))

    def test_commented_section_header_still_strict(self):
        text = ("[active_device:s0]\nfamily = sim\n[active_devices]\ndevices = s0\n"
                "[active_tx]   ; safety section\ndeny_bands = 900e6:910e6 ; 915e6:916e6\n")
        iss = self.assertFatal(text, "INLINE_COMMENT")
        self.assertTrue(any(i.code == "INLINE_COMMENT" and i.where.startswith("[active_tx]")
                            for i in iss), iss)

    def test_indented_comment_line_inside_list(self):
        self.assertFatal("[active_tx]\ndeny_bands = 1e9:2e9\n    ;3e9:4e9\n", "INLINE_COMMENT")
        cfg = C.parse_config_text("[active_beam]\nnulls = 75:0\n    ;120:10\n", ".")
        self.assertIn("INLINE_COMMENT", codes(cfg.parse_issues, "warning"))
        # an unindented comment line is an ordinary comment
        cfg = C.parse_config_text("[active_tx]\ndeny_bands = 1e9:2e9\n; note\nen_tx = 0\n", ".")
        self.assertNotIn("INLINE_COMMENT", codes(cfg.parse_issues))

    def test_default_section_rejected(self):
        self.assertFatal("[DEFAULT]\nmax_digital_amplitude = 0.9\n[active_tx]\nen_tx = 0\n",
                         "SYNTAX")
        self.assertFatal("[DEFAULT]\nmax_chain_power_dbm = 40\nmax_burst_s = 10\nduty_max = 1\n"
                         "[active_tx]\nen_tx = 0\n", "SYNTAX")
        # nothing is inherited even if the error were ignored: the parser has no DEFAULT
        cp = C._new_parser()
        cp.read_string("[DEFAULT]\nduty_max = 1\n[active_tx]\nen_tx = 0\n")
        self.assertFalse(cp.has_option("active_tx", "duty_max"))

    def test_indented_safety_key_is_fatal(self):
        issues = self.assertFatal("[active_tx]\nauthorization = LIC-1\n   max_burst_s = 0.001\n",
                                  "SYNTAX")
        self.assertTrue(any("max_burst_s" in i.message for i in issues))
        self.assertFatal("[active_tx]\ncallsign = N0CALL\n   extra text\n", "SYNTAX")
        self.assertFatal("[active_tx]\ndeny_bands = 1e9:2e9;\n   duty_max = 1\n", "SYNTAX")
        # elsewhere a swallowed key warns
        cfg = C.parse_config_text("[active_output]\nsidecar_path = /tmp/x\n"
                                  "   record_path = /tmp/y\n", ".")
        self.assertIn("SYNTAX", codes(cfg.parse_issues, "warning"))

    def test_chain_field_count(self):
        base = TWO_DEV.replace("backend = uhd", "backend = uhd\nmode = trx\noperator = bob") + \
            "[active_tx]\nen_tx = 1\nallow_bands = 902e6:928e6\ndummy_load_rating_w = 10\n" \
            "[active_rf_control]\npa_enable_line = n321a:FP0:0x1\n" \
            "[active_frontend]\nen_frontend = 1\nrx_chain = lna:l1:20:0.9:15::10\n"
        # trailing empty fields: still checked -> no p1db/psat -> TX_LOCK
        iss = C.validate_config(parse(base + "tx_chain = pa:main:40:::::::\n"))
        self.assertIn("TX_LOCK", codes(iss, "error"))
        iss = C.validate_config(parse(base + "tx_chain = pa:main:30::::::\n"))
        self.assertIn("TX_LOCK", codes(iss, "error"))
        # non-empty 9th field with TX enabled: error
        iss = C.validate_config(parse(base + "tx_chain = pa:main:17::37:40::8:1\n"))
        self.assertIn("CHAIN", codes(iss, "error"))
        # the 8-field spec example is clean
        iss = C.validate_config(parse(base + "tx_chain = amp:drv:20:5:20:23;pa:main:17::37:40::8\n"))
        self.assertFalse(has_errors(iss), iss)
        # TX disabled: > 8 fields stay a warning
        iss = C.validate_config(parse("[active_frontend]\nen_frontend = 1\n"
                                      "tx_chain = pa:main:17::37:40::8:1\n"))
        self.assertIn("CHAIN", codes(iss, "warning"))
        self.assertNotIn("CHAIN", codes(iss, "error"))


class TestEnumRange(unittest.TestCase):
    def check(self, text, code, level="error"):
        cfg = parse(text)
        iss = C.validate_config(cfg)
        self.assertIn(code, codes(iss, level), "%s -> %s" % (text, iss))
        return iss

    def test_enum_errors(self):
        self.check("[active]\nmode = duplex\n", "ENUM")
        self.check("[active_geometry]\npreset = hex\n", "ENUM")
        self.check("[active_beam]\ntaper = kaiser\n", "ENUM")
        self.check("[active_tx]\nallowed_modes = dummy_load, broadcast\n", "ENUM")
        self.check("[active_output]\noutput_mode = fft\n", "ENUM")
        self.check("[active_calibration]\ninjection_source = gen\n", "ENUM")

    def test_range_errors(self):
        self.check("[meta]\nactive_ini_version = 2\n", "VERSION")
        self.check("[active]\ninstance_id = 16\n", "RANGE")
        self.check("[active_rf]\ncpi_samples = 100\n", "RANGE")
        self.check("[active_geometry]\nspacing_lambda = 20\n", "RANGE")
        self.check("[active_tx]\nmax_digital_amplitude = 0.95\n", "RANGE")
        self.check("[active_tx]\nduty_max = 0\n", "RANGE")
        self.check("[active_tx]\nmax_burst_s = 2\nduty_window_s = 10\n", "RANGE")
        self.check("[active_tx]\narm_timeout_s = 100\nmax_arm_total_s = 50\n", "RANGE")
        self.check("[active_timing]\ncommand_lead_s = 0.3\nstream_lead_s = 0.3\n", "RANGE")
        self.check("[active_calibration]\nrecal_interval_s = 5\n", "RANGE")
        self.check("[active_calibration]\ntx_cal_amplitude = 0.6\n", "RANGE")
        self.check("[active]\nhardware_id = THIS-ID-IS-WAY-TOO-LONG\n", "RANGE")
        self.check("[active_beam]\nsll_db = 5\n", "RANGE")
        cfg = parse("[active_calibration]\nrecal_interval_s = 0\n")
        self.assertFalse(has_errors(C.validate_config(cfg)))

    def test_pc_clock_error(self):
        self.check("[active_timing]\nsync_method = pc_clock\n", "COHERENCE_PC_CLOCK")

    def test_misc_warnings(self):
        self.check("[active_rf]\notw_format = sc12\n", "OTW_FORMAT", "warning")
        self.check("[active_beam]\ntaper = taylor\nnbar = 2\n", "NBAR", "warning")

    def test_required(self):
        self.check("[active_calibration]\nrx_method = beacon\n", "REQUIRED")
        self.check("[active_calibration]\ntx_method = loopback_tdm\n", "REQUIRED")
        self.check("[active_timing]\nlo_plan = n321_star\n", "REQUIRED")
        self.check("[active_timing]\nlo_master = ghost\n", "UNKNOWN_DEVICE")
        self.check("[active_tx]\nauthorization_kind = amateur\n", "REQUIRED")

    def test_aux_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = parse("[active_frontend]\ntx_power_cal_file = cal.csv\n", td)
            self.assertIn("FILE_MISSING", codes(C.validate_config(cfg)))
            with open(join(td, "cal.csv"), "w") as fh:
                fh.write("chan,freq_hz,tx_gain_db,p_fs_dbm\n")
            self.assertNotIn("FILE_MISSING", codes(C.validate_config(cfg)))
            self.assertEqual(cfg.resolve_path("cal.csv"), join(os.path.realpath(td), "cal.csv")
                             if os.path.realpath(td) == cfg.base_dir else join(td, "cal.csv"))
            self.assertEqual(cfg.resolve_path(""), "")
            self.assertEqual(cfg.resolve_path("/abs/x"), "/abs/x")


class TestPorts(unittest.TestCase):
    def port_issues(self, extra):
        return [i for i in C.validate_config(parse("[active_output]\n" + extra)) if i.code == "PORT"]

    def test_defaults_ok(self):
        self.assertEqual(self.port_issues(""), [])

    def test_forbidden_ports(self):
        for port in (5000, 5001, 5100, 6000, 1130):
            iss = self.port_issues("iq_port = %d\n" % port)
            self.assertTrue(any(i.level == "error" for i in iss), port)
        iss = self.port_issues("status_port = 1230\n")      # 1130 + 100 (instance 1 ZMQ)
        self.assertTrue(any(i.level == "error" for i in iss))

    def test_port_5000_allowed_warns(self):
        iss = self.port_issues("iq_port = 5000\nallow_port_5000 = 1\n")
        self.assertEqual([i.level for i in iss], ["warning"])
        iss = self.port_issues("control_port = 5001\nallow_port_5000 = 1\n")
        self.assertTrue(any(i.level == "error" for i in iss))

    def test_collision_and_offset(self):
        iss = self.port_issues("status_port = 5050\n")
        self.assertTrue(any("collides" in i.message for i in iss))
        cfg = parse("[active]\ninstance_id = 1\n")
        self.assertEqual(C.active_port_map(cfg)["iq"], 5150)
        self.assertEqual(C.active_port_map(cfg)["event"], 0)       # en_event_pub = 0
        # instance offset lands on a forbidden port: 4950 + 100 = 5050 fine, 4900+100=5000 not
        cfg = parse("[active]\ninstance_id = 1\n[active_output]\niq_port = 4900\n")
        self.assertIn("PORT", codes(C.validate_config(cfg), "error"))
        self.assertEqual(C.active_port_map(parse("[active_output]\ncontrol_port = 0\n"))["control"], 0)

    def test_gr_zmq_offset(self):
        cfg = parse("[active]\ninstance_id = 2\n")
        self.assertEqual(C.gr_zmq_address(cfg), "tcp://127.0.0.1:5255")
        cfg = parse("[active_gnuradio]\nzmq_address = garbage\n")
        self.assertIn("ENUM", codes(C.validate_config(cfg)))


class TestSafetySanity(unittest.TestCase):
    TX = TWO_DEV.replace("backend = uhd", "backend = uhd\nmode = trx\noperator = alice")

    def test_tx_lock(self):
        cfg = parse(self.TX + "[active_tx]\nen_tx = 1\n")
        iss = C.validate_config(cfg)
        self.assertIn("TX_LOCK", codes(iss, "error"))          # allow_bands missing
        self.assertIn("DUMMY_LOAD", codes(iss, "error"))       # rating 0
        cfg = parse(self.TX + "[active_tx]\nen_tx = 1\nallow_bands = 902e6:928e6\n"
                              "dummy_load_rating_w = 1\n")
        self.assertFalse(has_errors(C.validate_config(cfg)), C.validate_config(cfg))

    def test_tx_lock_operator_and_pa_line(self):
        text = TWO_DEV.replace("backend = uhd", "backend = uhd\nmode = trx") + textwrap.dedent("""
        [active_tx]
        en_tx = 1
        allow_bands = 902e6:928e6
        dummy_load_rating_w = 10
        [active_frontend]
        en_frontend = 1
        tx_chain = amp:drv:20:5:20:23;pa:main:17::37:40::8
        rx_chain = lna:l1:20:0.9:15
        """)
        iss = C.validate_config(parse(text))
        msgs = [i.message for i in iss if i.code == "TX_LOCK"]
        self.assertTrue(any("operator" in m for m in msgs))
        self.assertTrue(any("pa_enable_line" in m for m in msgs))
        self.assertTrue(any("max_in_dbm" in m for m in msgs))
        fixed = text.replace("mode = trx", "mode = trx\noperator = bob").replace(
            "rx_chain = lna:l1:20:0.9:15", "rx_chain = lna:l1:20:0.9:15::10") + \
            "[active_rf_control]\npa_enable_line = n321a:FP0:0x1\n"
        self.assertFalse(has_errors(C.validate_config(parse(fixed))), C.validate_config(parse(fixed)))

    def test_power_monitor_pairing(self):
        base = self.TX + ("[active_tx]\nen_tx = 1\nallow_bands = 902e6:928e6\ndummy_load_rating_w = 1\n")
        iss = C.validate_config(parse(base + "max_reverse_power_dbm = 30\n"))
        self.assertIn("TX_LOCK", codes(iss, "error"))
        iss = C.validate_config(parse(base + "[active_rf_control]\npower_monitor = sim\n"))
        self.assertIn("TX_LOCK", codes(iss, "error"))

    def test_radiate_requirements(self):
        cfg = parse("[active_tx]\nallowed_modes = dummy_load, radiate\n")
        iss = [i for i in C.validate_config(cfg) if i.code == "TX_RADIATE"]
        text = " ".join(i.message for i in iss)
        for word in ("authorization", "tx_power_cal_file", "interlock", "mpe_exclusion_m",
                     "power monitor", "passive_isolation_db"):
            self.assertIn(word, text)

    def test_cabled_and_loopback_losses(self):
        base = self.TX + "[active_tx]\nen_tx = 1\nallow_bands = 902e6:928e6\ndummy_load_rating_w = 1\n"
        iss = C.validate_config(parse(base + "allowed_modes = dummy_load,cabled\n"))
        self.assertIn("CABLED", codes(iss, "error"))
        iss = C.validate_config(parse(base + "[active_calibration]\ntx_method = loopback_tdm\n"
                                             "loopback_rx_chan = 3\n"))
        self.assertIn("LOOPBACK", codes(iss, "error"))

    def test_line_specs_and_chain_grammar(self):
        iss = C.validate_config(parse(TWO_DEV + "[active_rf_control]\npa_enable_line = ghost:FP0:1\n"
                                                "tr_switch_line = n321a:FP0\ninterlock = sim\n"))
        self.assertEqual(codes(iss, "error").count("LINE_SPEC"), 2)
        iss = C.validate_config(parse(TWO_DEV + "[active_rf_control]\n"
                                                "pa_enable_line = n321a:FP0:1,n321a:FP0:2\n"))
        self.assertIn("LIST_LEN", codes(iss, "error"))
        iss = C.validate_config(parse("[active_frontend]\nen_frontend = 1\n"
                                      "tx_chain = pa:x\nrx_chain = lna:l:20;cable:c:1.2;mixer:m:3\n"))
        self.assertGreaterEqual(codes(iss, "error").count("CHAIN"), 3)

    def test_chain_overrides(self):
        cfg = parse(TWO_DEV + "[active_frontend]\nen_frontend = 1\nrx_chain = lna:a:20\n"
                              "rx_chain.2 = lna:b:10\ntx_chain.9 = amp:x:1:::5\n")
        self.assertEqual(cfg.frontend.rx_chain_spec(2), "lna:b:10")
        self.assertEqual(cfg.frontend.rx_chain_spec(0), "lna:a:20")
        self.assertEqual(cfg.parse_issues, ())
        self.assertIn("RANGE", codes(C.validate_config(cfg), "error"))       # tx_chain.9 > n_tx
        off = parse("[active_frontend]\nrx_chain = lna:a:20\n")
        self.assertEqual(off.frontend.rx_chain_spec(0), "")                   # en_frontend = 0
        self.assertIn("CHAIN", codes(C.validate_config(off), "warning"))     # declared but ignored

    TX4 = textwrap.dedent("""
    [active]
    backend = sim
    mode = trx
    operator = alice
    [active_devices]
    devices = s
    [active_device:s]
    family = sim
    rx_channels = 0..3
    tx_channels = 0..3
    [active_tx]
    en_tx = 1
    allow_bands = 902e6:928e6
    dummy_load_rating_w = 5
    """)

    def test_disabled_frontend_with_declared_chain_is_tx_lock(self):
        for key in ("tx_chain = pa:main:40", "tx_chain = pa:main:40:5:37:40::8",
                    "rx_chain = lna:l:20:1:0::10", "tx_chain.2 = amp:d:20:5:20",
                    "rx_chain.1 = lna:l:20"):
            cfg = parse(self.TX4 + "[active_frontend]\nen_frontend = 0\n%s\n" % key)
            self.assertEqual(cfg.frontend.tx_chain_spec(0), "")
            iss = C.validate_config(cfg)
            self.assertTrue(any(i.code == "TX_LOCK" and "en_frontend = 0" in i.message
                                for i in iss if i.level == "error"), (key, iss))
        # a declared PA requires pa_enable_line even with en_frontend = 0
        iss = C.validate_config(parse(self.TX4 + "[active_frontend]\ntx_chain = pa:main:40\n"))
        self.assertTrue(any("pa_enable_line" in i.message for i in iss if i.level == "error"))
        # TX off: only a warning
        cfg = parse(self.TX4.replace("en_tx = 1", "en_tx = 0") +
                    "[active_frontend]\ntx_chain = pa:main:40\n")
        iss = C.validate_config(cfg)
        self.assertIn("CHAIN", codes(iss, "warning"))
        self.assertNotIn("TX_LOCK", codes(iss, "error"))
        # nothing declared: no finding
        iss = C.validate_config(parse(self.TX4))
        self.assertFalse(has_errors(iss), iss)
        self.assertFalse([i for i in iss if "en_frontend" in i.message])


class TestTxEnabled(unittest.TestCase):
    def test_truth_table(self):
        for en_tx in (0, 1):
            for mode in ("rx", "tx", "trx"):
                cfg = parse("[active]\nmode = %s\n[active_tx]\nen_tx = %d\n" % (mode, en_tx))
                self.assertEqual(cfg.tx_enabled(), bool(en_tx) and mode in ("tx", "trx"),
                                 (en_tx, mode))


class TestAntenna(unittest.TestCase):
    def test_profile_via_parser(self):
        cfg = parse("""
        [active_antenna]
        en_antenna_profile = 1
        profile_name = patch
        element_gain_dbi = 6.5
        cable_loss_db = 1.0
        connector_loss_db = 0.25
        polarization = rhcp
        """)
        ap = cfg.antenna_profile
        self.assertEqual(type(ap).__name__, "AntennaProfile")
        self.assertEqual(ap.name, "patch")
        self.assertEqual(ap.element_gain_dbi, 6.5)
        self.assertEqual(ap.feed_loss_db, 1.25)
        self.assertEqual(ap.polarization, "rhcp")
        self.assertIsNone(parse("[active_antenna]\nen_antenna_profile = 0\n").antenna_profile)

    def test_disabled_profile_with_values_is_reported(self):
        off = "[active_antenna]\nen_antenna_profile = 0\nelement_gain_dbi = 24\n"
        cfg = parse(off)
        self.assertIsNone(cfg.antenna_profile)
        self.assertEqual(cfg.antenna_ignored_keys, ("element_gain_dbi",))
        self.assertIn("ANTENNA_IGNORED", codes(C.validate_config(cfg), "warning"))
        cfg = parse(off + "[active_tx]\nallowed_modes = dummy_load, radiate\n")
        self.assertIn("ANTENNA_IGNORED", codes(C.validate_config(cfg), "error"))
        # default-valued keys (any spelling) are not reported
        cfg = parse("[active_antenna]\nen_antenna_profile = 0\nelement_gain_dbi = 0.0\n"
                    "polarization = Vertical\nbeamwidth_az_deg = 360\n"
                    "[active_tx]\nallowed_modes = radiate\n")
        self.assertEqual(cfg.antenna_ignored_keys, ())
        self.assertNotIn("ANTENNA_IGNORED", codes(C.validate_config(cfg)))
        cfg = parse("[active_antenna]\nen_antenna_profile = 1\nelement_gain_dbi = 24\n")
        self.assertEqual(cfg.antenna_ignored_keys, ())

    def test_bad_values(self):
        with self.assertRaises(ConfigError):
            parse("[active_antenna]\nen_antenna_profile = 1\nelement_gain_dbi = high\n")
        cfg = parse("[active_antenna]\nen_antenna_profile = 1\npolarization = weird\n")
        self.assertIn("ENUM", codes(cfg.parse_issues, "warning"))


class TestFingerprint(unittest.TestCase):
    BASE = TestSafetySanity.TX + "[active_tx]\nen_tx = 1\nallow_bands = 902e6:928e6\n" \
                                 "dummy_load_rating_w = 1\n"

    def test_stable(self):
        a = C.config_fingerprint(parse(self.BASE))
        b = C.config_fingerprint(parse(self.BASE))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_changed_by_every_tx_key(self):
        ref = C.config_fingerprint(parse(self.BASE))
        cfg = parse(self.BASE)
        seen = set()
        for f in dataclasses.fields(C.TxCfg):
            val = getattr(cfg.tx, f.name)
            if isinstance(val, bool):
                new = not val
            elif isinstance(val, (int, float)):
                new = val + 1
            elif isinstance(val, str):
                new = val + "x"
            elif f.name in ("allowed_modes",):
                new = val + ("cabled",)
            elif f.name in ("allow_bands", "deny_bands"):
                new = val + ((1e9, 2e9),)
            elif f.name == "max_tx_gain_db":
                new = (1.0,) * len(val)
            elif isinstance(val, tuple):
                new = tuple(x + 1 for x in val)
            elif val is None:
                new = 5.0
            else:
                self.fail("unhandled field %s" % f.name)
            mod = dataclasses.replace(cfg, tx=dataclasses.replace(cfg.tx, **{f.name: new}))
            fp = C.config_fingerprint(mod)
            self.assertNotEqual(fp, ref, f.name)
            seen.add(fp)
        self.assertEqual(len(seen), len(dataclasses.fields(C.TxCfg)))

    def test_changed_by_ini_value(self):
        a = C.config_fingerprint(parse(self.BASE))
        b = C.config_fingerprint(parse(self.BASE + "max_burst_s = 0.05\n"))
        self.assertNotEqual(a, b)

    def test_power_cal_file_contents(self):
        with tempfile.TemporaryDirectory() as td:
            p = join(td, "tx_power_cal.csv")
            with open(p, "w") as fh:
                fh.write("chan,freq_hz,tx_gain_db,p_fs_dbm\n0,915e6,10,0.0\n")
            text = self.BASE + "[active_frontend]\ntx_power_cal_file = tx_power_cal.csv\n"
            a = C.config_fingerprint(parse(text, td))
            self.assertEqual(a, C.config_fingerprint(parse(text, td)))
            with open(p, "w") as fh:
                fh.write("chan,freq_hz,tx_gain_db,p_fs_dbm\n0,915e6,10,0.5\n")
            b = C.config_fingerprint(parse(text, td))
            self.assertNotEqual(a, b)
            os.unlink(p)
            c = C.config_fingerprint(parse(text, td))
            self.assertNotIn(c, (a, b))
            self.assertTrue(C.aux_file_hashes(parse(text, td))["frontend.tx_power_cal_file"]
                            .startswith("missing:"))

    def test_unchanged_by_output_and_excluded(self):
        a = C.config_fingerprint(parse(self.BASE))
        for extra in ("[active_output]\niq_port = 6050\nqueue_len = 8\nlisten_address = 0.0.0.0\n",
                      "[active_gnuradio]\npython = /opt/gr/bin/python3\nstart_timeout_s = 30\n",
                      "[hw]\nwhatever = 1\n"):
            self.assertEqual(C.config_fingerprint(parse(self.BASE + extra)), a, extra)
        b = C.config_fingerprint(parse(self.BASE.replace("operator = alice",
                                                         "operator = alice\nlog_level = debug\n"
                                                         "state_dir = /tmp/x")))
        self.assertEqual(a, b)
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(C.config_fingerprint(parse(self.BASE, td)), a)   # location excluded
        c = C.config_fingerprint(parse(self.BASE + "[active_rf]\ncenter_freq_hz = 916e6\n"))
        self.assertNotEqual(a, c)


class TestLoadAndDict(unittest.TestCase):
    def test_load_config_file_and_env(self):
        with tempfile.TemporaryDirectory() as td:
            p = join(td, "active_chain_config.ini")
            with open(p, "w") as fh:
                fh.write("[meta]\nconfig_name = t\n[active_geometry]\nnum_elements = 3\n")
            cfg = C.load_config(p)
            self.assertEqual(cfg.meta.config_name, "t")
            self.assertEqual(cfg.base_dir, os.path.dirname(os.path.abspath(p)))
            self.assertEqual(cfg.path, os.path.abspath(p))
            old = os.environ.get("HEIMDALL_ACTIVE_CONFIG")
            os.environ["HEIMDALL_ACTIVE_CONFIG"] = p
            try:
                self.assertEqual(C.load_config().meta.config_name, "t")
            finally:
                if old is None:
                    del os.environ["HEIMDALL_ACTIVE_CONFIG"]
                else:
                    os.environ["HEIMDALL_ACTIVE_CONFIG"] = old
            with self.assertRaises(ConfigError) as cm:
                C.load_config(join(td, "missing.ini"))
            self.assertEqual(cm.exception.issues[0].code, "UNREADABLE")

    def test_config_to_dict_json(self):
        cfg = parse(TWO_DEV + "[active_antenna]\nen_antenna_profile = 1\n")
        d = C.config_to_dict(cfg)
        s = json.dumps(d)
        self.assertIn("n321a", s)
        self.assertEqual(d["devices"][0]["name"], "n321a")
        self.assertEqual(d["antenna_profile"]["name"], "isotropic")
        for key in ("meta", "active", "rf", "timing", "geometry", "beam", "calibration", "frontend",
                    "rf_control", "tx", "output", "gnuradio", "sim"):
            self.assertIn(key, d)

    def test_known_keys(self):
        self.assertIn("en_tx", C.KNOWN_KEYS["active_tx"])
        self.assertIn("family", C.KNOWN_KEYS["active_device:<name>"])
        self.assertIn("rx_chain.<i>", C.KNOWN_KEYS["active_frontend"])
        self.assertIn("element_gain_dbi", C.KNOWN_KEYS["active_antenna"])
        # every TxCfg field is a key of [active_tx] and vice versa
        self.assertEqual({f.name for f in dataclasses.fields(C.TxCfg)}, set(C.KNOWN_KEYS["active_tx"]))
        for cls, sec in ((C.ActiveCfg, "active"), (C.RfCfg, "active_rf"), (C.TimingCfg, "active_timing"),
                         (C.GeometryCfg, "active_geometry"), (C.BeamCfg, "active_beam"),
                         (C.CalibrationCfg, "active_calibration"), (C.RfControlCfg, "active_rf_control"),
                         (C.OutputCfg, "active_output"), (C.GnuradioCfg, "active_gnuradio"),
                         (C.SimCfg, "active_sim"), (C.MetaCfg, "meta")):
            self.assertEqual({f.name for f in dataclasses.fields(cls)}, set(C.KNOWN_KEYS[sec]), sec)
        dev_fields = {f.name for f in dataclasses.fields(C.DeviceCfg)} - {"name"}
        self.assertEqual(dev_fields, set(C.KNOWN_KEYS["active_device:<name>"]))

    def test_frozen(self):
        cfg = C.default_config()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.tx.en_tx = True


if __name__ == "__main__":
    unittest.main()
