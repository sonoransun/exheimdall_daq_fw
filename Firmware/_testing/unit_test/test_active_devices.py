"""
    Unit tests for device capabilities, grouping and plan rules
    (Firmware/_active/active_devices.py).

    No sudo, no hardware. Configs come from default_config.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import dataclasses
import json
import math
import sys
import unittest
from os.path import dirname, join, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_config as C  # noqa: E402
import active_devices as D  # noqa: E402
from active_common import ChannelRef, ConfigError  # noqa: E402


def cfg_of(devices, backend="uhd", **sections):
    sections.setdefault("active", {})
    sections["active"] = dict({"backend": backend}, **sections["active"])
    return C.default_config(devices=devices, **sections)


def plan(cfg):
    return D.validate_device_plan(cfg)


def codes(issues, level=None):
    return [i.code for i in issues if level is None or i.level == level]


EXT = {"clock_source": "external", "time_source": "external"}


class TestCaps(unittest.TestCase):
    def test_invariants_every_family(self):
        self.assertEqual(set(D.families()), set(D.CAPS))
        for fam, c in D.CAPS.items():
            with self.subTest(fam=fam):
                self.assertEqual(c.family, fam)
                self.assertIn(c.vendor, ("ettus", "pervices", "sim"))
                self.assertGreaterEqual(c.n_rx, 1)
                self.assertGreaterEqual(c.n_tx, 0)
                self.assertGreater(c.freq_min_hz, 0)
                self.assertLess(c.freq_min_hz, c.freq_max_hz)
                self.assertGreater(c.max_bw_hz, 0)
                for rng in (c.rx_gain_range_db, c.tx_gain_range_db):
                    self.assertEqual(len(rng), 3)
                    self.assertLessEqual(rng[0], rng[1])
                    self.assertGreater(rng[2], 0)
                if c.mcr_range_hz:
                    self.assertEqual(len(c.mcr_range_hz), 2)
                    self.assertLess(c.mcr_range_hz[0], c.mcr_range_hz[1])
                for m in c.mcr_choices_hz:
                    self.assertGreater(m, 0)
                self.assertGreaterEqual(c.tick_rate_hz, 0)
                self.assertIsInstance(c.tick_rate_hz, int)
                self.assertIn(c.phase_ambiguity, ("random", "180", "deterministic"))
                self.assertIn(c.lo_share_kind, ("none", "intra_device", "n321_star", "twinrx",
                                                "external_lo", "pervices_common", "sim"))
                self.assertTrue(c.clock_sources and c.time_sources)
                self.assertIn("internal", c.clock_sources)
                self.assertTrue(math.isfinite(c.tx_max_dbm_upper))
                self.assertTrue(math.isfinite(c.rx_max_input_dbm))
                self.assertGreaterEqual(c.min_tx_lead_s, 0)
                for edge in c.band_edges_hz:
                    self.assertTrue(c.freq_min_hz < edge < c.freq_max_hz)
                for ch in c.tx_reduced_rate_chans:
                    self.assertLess(ch, c.n_tx)
                if c.vendor == "pervices":
                    self.assertGreater(c.rate_base_hz, 0)
                    self.assertGreater(c.tick_rate_hz, 0)
                    self.assertTrue(c.integer_delay_ctl)
                    self.assertEqual(c.gpio_banks, ())
                    self.assertEqual(c.min_tx_lead_s, 0.0002)
                else:
                    self.assertEqual(c.min_tx_lead_s, 0.0)
                    self.assertFalse(c.integer_delay_ctl)
                    self.assertTrue(c.mcr_choices_hz or c.mcr_range_hz)
                json.dumps(D.caps_to_dict(c))

    def test_named_facts(self):
        self.assertEqual(D.get_caps("crimson_tng").tick_rate_hz, 162_500_000)
        self.assertEqual(D.get_caps("cyan").tick_rate_hz, 250_000_000)
        for fam in ("crimson_tng", "cyan", "cyan_hb", "chestnut", "calamine"):
            c = D.get_caps(fam)
            self.assertTrue(c.single_mboard_only, fam)
            self.assertFalse(c.timed_tune, fam)
            self.assertFalse(c.gpsdo_option, fam)
            self.assertNotIn("gpsdo", c.clock_sources + c.time_sources, fam)
        x410 = D.get_caps("x410")
        self.assertFalse(x410.timed_tune)
        self.assertFalse(x410.multi_device_coherent)
        b210 = D.get_caps("b210")
        self.assertEqual(b210.dual_chan_max_rate_hz, 30.72e6)
        self.assertFalse(b210.compound_multi_usrp)
        self.assertTrue(D.get_caps("n321").lo_export)
        self.assertEqual(D.phase_ambiguity("n310", "external"), "180")
        self.assertEqual(D.phase_ambiguity("n310", "internal"), "random")
        self.assertTrue(D.get_caps("x310_ubx").phase_repeatable_timed_tune)
        self.assertTrue(D.get_caps("x310_sbx").phase_repeatable_timed_tune)
        self.assertEqual(D.get_caps("calamine").uhd_type, "crimson_tng")
        self.assertEqual(D.get_caps("crimson_tng").band_edges_hz, (120e6,))
        self.assertEqual(D.get_caps("cyan").band_edges_hz, (250e6, 400e6, 5.8e9))
        self.assertEqual(D.get_caps("crimson_tng").tx_reduced_rate_chans, (2, 3))
        self.assertTrue(D.get_caps("x440").phase_repeatable_reinit)
        self.assertFalse(D.get_caps("n320").phase_repeatable_reinit)
        self.assertEqual(D.get_caps("x410").gpio_banks, ("GPIO0", "GPIO1"))
        self.assertEqual(D.get_caps("n320").gpio_banks, ("FP0",))
        self.assertEqual(D.get_caps("sim").gpio_banks, ("FP0",))
        self.assertEqual(D.get_caps("x310_twinrx").n_tx, 0)
        self.assertTrue(any("[verify]" in n for n in D.get_caps("n321").notes))

    def test_unknown_family(self):
        with self.assertRaises(ConfigError) as cm:
            D.get_caps("hackrf")
        self.assertIn("b210", str(cm.exception))

    def test_tick_rates(self):
        cfg = cfg_of({"x": {"family": "n320", "master_clock_rate_hz": 245.76e6}})
        self.assertEqual(D.tick_rate_hz(cfg.device("x")), 245_760_000)
        cfg = cfg_of({"x": {"family": "x310_ubx"}})
        self.assertEqual(D.tick_rate_hz(cfg.device("x")), 200_000_000)
        cfg = cfg_of({"x": {"family": "crimson_tng"}}, backend="pervices",
                     rf={"sample_rate_hz": 325e6 / 4})
        self.assertEqual(D.tick_rate_hz(cfg.device("x")), 162_500_000)


class TestGrouping(unittest.TestCase):
    STAR = {
        "n321a": {"family": "n321", "addr": "192.168.20.2", "group": "star", "rx_channels": "0,1",
                  "lo_export": 1, **EXT},
        "n320b": {"family": "n320", "addr": "192.168.20.3", "group": "star", "rx_channels": "0,1",
                  "lo_source": "external", **EXT},
        "n320c": {"family": "n320", "addr": "192.168.20.4", "group": "star", "rx_channels": "0,1",
                  "lo_source": "external", **EXT},
    }

    def test_n321_star_one_compound_group(self):
        cfg = cfg_of(self.STAR, rf={"center_freq_hz": 2.4e9},
                     timing={"lo_plan": "n321_star", "lo_master": "n321a"})
        groups = D.device_groups(cfg)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertTrue(g.compound)
        self.assertEqual(g.device_names(), ["n321a", "n320b", "n320c"])
        args = D.parse_device_args(D.build_uhd_args(g, cfg))
        self.assertEqual((args["addr0"], args["addr1"], args["addr2"]),
                         ("192.168.20.2", "192.168.20.3", "192.168.20.4"))
        self.assertNotIn("addr", args)
        # N320 + N321 are one compound class: no mixed-group error, a clean plan
        self.assertEqual(codes(plan(cfg), "error"), [], [str(i) for i in plan(cfg)])
        self.assertEqual(D.resolve_sync_method(cfg), "unknown_pps")
        self.assertEqual(D.phase_after_tune(cfg), "shared_lo")
        # without the explicit group, auto grouping is per family -> two groups
        auto = cfg_of({k: dict(v, group="") for k, v in self.STAR.items()})
        self.assertEqual([g.device_names() for g in D.device_groups(auto)],
                         [["n321a"], ["n320b", "n320c"]])
        self.assertEqual(D.resolve_sync_method(auto), "next_pps_host")

    def test_auto_group_same_family(self):
        devs = {"a": {"family": "n320", "addr": "10.0.0.2", "master_clock_rate_hz": 245.76e6, **EXT},
                "b": {"family": "n320", "addr": "10.0.0.3", "rx_channels": "0,1", **EXT}}
        cfg = cfg_of(devs)
        groups = D.device_groups(cfg)
        self.assertEqual(len(groups), 1)
        self.assertEqual(D.build_uhd_args(groups[0], cfg),
                         "addr0=10.0.0.2,addr1=10.0.0.3,master_clock_rate=245760000")
        self.assertNotIn("DEVICE_MIXED_GROUP", codes(plan(cfg)))
        # UHD applies the one master_clock_rate to EVERY mboard: b runs at 245.76 MHz too
        a, b = cfg.device("a"), cfg.device("b")
        self.assertEqual(D.tick_rate_hz(a, cfg), 245_760_000)
        self.assertEqual(D.tick_rate_hz(b, cfg), 245_760_000)
        self.assertEqual(D.effective_mcr_hz(b, cfg), 245.76e6)
        self.assertEqual(D.group_mcr_hz(groups[0]), 245.76e6)
        # ... and pinning on one member only is a DEVICE_MCR error (pin on all or none)
        self.assertIn("DEVICE_MCR", codes(plan(cfg), "error"))
        # no false DEVICE_RATE against b's family-default MCR (61.44e6 = 245.76e6 / 4)
        fs = cfg_of(devs, rf={"sample_rate_hz": 61.44e6})
        self.assertNotIn("DEVICE_RATE", codes(plan(fs)), [str(i) for i in plan(fs)])
        both = dict(devs, b=dict(devs["b"], master_clock_rate_hz=245.76e6))
        both_cfg = cfg_of(both, rf={"sample_rate_hz": 61.44e6})
        self.assertNotIn("DEVICE_MCR", codes(plan(both_cfg)))
        self.assertNotIn("DEVICE_RATE", codes(plan(both_cfg)))
        none = {k: {kk: vv for kk, vv in v.items() if kk != "master_clock_rate_hz"}
                for k, v in devs.items()}
        none_cfg = cfg_of(none)
        self.assertNotIn("DEVICE_MCR", codes(plan(none_cfg)))
        self.assertEqual(D.tick_rate_hz(none_cfg.device("a"), none_cfg), 200_000_000)
        # channel map: mboard a has 2 channels (caps), b's channels follow
        cmap = groups[0].channel_map(cfg, "rx")
        self.assertEqual(cmap, [(0, 0, ChannelRef("a", 0)), (2, 1, ChannelRef("b", 0)),
                                (3, 2, ChannelRef("b", 1))])

    def test_two_b210_two_groups(self):
        devs = {"a": {"family": "b210", "serial": "3000001", "master_clock_rate_hz": 30.72e6, **EXT},
                "b": {"family": "b210", "serial": "3000002", "master_clock_rate_hz": 30.72e6, **EXT}}
        cfg = cfg_of(devs)
        groups = D.device_groups(cfg)
        self.assertEqual([g.device_names() for g in groups], [["a"], ["b"]])
        self.assertFalse(any(g.compound for g in groups))
        self.assertEqual(D.build_uhd_args(groups[0], cfg), "serial=3000001,master_clock_rate=30720000")
        self.assertEqual(D.resolve_sync_method(cfg), "next_pps_host")

    def test_compound_class(self):
        self.assertEqual(D.compound_class("n321"), D.compound_class("n320"))
        self.assertEqual(D.compound_class("x310_ubx"), D.compound_class("x310_twinrx"))
        self.assertNotEqual(D.compound_class("n310"), D.compound_class("n320"))
        devs = {"a": {"family": "n310", "addr": "1", "group": "g", **EXT},
                "b": {"family": "n320", "addr": "2", "group": "g", **EXT}}
        self.assertIn("DEVICE_MIXED_GROUP", codes(plan(cfg_of(devs)), "error"))

    def test_explicit_mixed_group_error(self):
        devs = {"a": {"family": "n320", "addr": "10.0.0.2", "group": "g", **EXT},
                "b": {"family": "x310_ubx", "addr": "10.0.0.3", "group": "g", **EXT}}
        self.assertIn("DEVICE_MIXED_GROUP", codes(plan(cfg_of(devs)), "error"))
        devs = {"a": {"family": "n320", "addr": "10.0.0.2", "group": "g", **EXT},
                "b": {"family": "b210", "serial": "1", "group": "g", "master_clock_rate_hz": 30.72e6,
                      **EXT}}
        self.assertIn("DEVICE_MIXED_GROUP", codes(plan(cfg_of(devs)), "error"))

    def test_sim_grouping(self):
        devs = {"s0": {"family": "sim", "rx_channels": "0,1"},
                "s1": {"family": "sim", "rx_channels": "0,1"}}
        cfg = cfg_of(devs, backend="sim")
        groups = D.device_groups(cfg)
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0].compound)
        self.assertEqual(D.build_uhd_args(groups[0], cfg), "type=sim,addr0=s0,addr1=s1")
        cfg = cfg_of(devs, backend="sim", sim={"emulate_family": "crimson_tng"})
        self.assertEqual(len(D.device_groups(cfg)), 2)

    def test_compound_needs_addr_and_args_conflicts(self):
        devs = {"a": {"family": "x310_ubx", **EXT}, "b": {"family": "x310_ubx", "addr": "x", **EXT}}
        cfg = cfg_of(devs)
        with self.assertRaises(ConfigError):
            D.build_uhd_args(D.device_groups(cfg)[0], cfg)
        cfg = cfg_of({"a": {"family": "n320", "addr": "1.2.3.4", "master_clock_rate_hz": 200e6,
                            "args": "master_clock_rate=250e6"}})
        with self.assertRaises(ConfigError):
            D.build_uhd_args(D.device_groups(cfg)[0], cfg)
        cfg = cfg_of({"a": {"family": "n320", "addr": "1.2.3.4", "master_clock_rate_hz": 250e6,
                            "args": "master_clock_rate=250e6,use_dpdk=1"}})
        self.assertEqual(D.build_uhd_args(D.device_groups(cfg)[0], cfg),
                         "addr=1.2.3.4,master_clock_rate=250000000,use_dpdk=1")
        cfg = cfg_of({"a": {"family": "b210"}})
        self.assertEqual(D.build_uhd_args(D.device_groups(cfg)[0], cfg), "type=b200")


class TestPervicesArgs(unittest.TestCase):
    def dev(self, family, **kw):
        return C.DeviceCfg(name="pv", family=family, **kw)

    def test_types(self):
        self.assertEqual(D.build_pervices_args(self.dev("crimson_tng", addr="192.168.10.2")),
                         "type=crimson_tng,addr=192.168.10.2")
        self.assertEqual(D.build_pervices_args(self.dev("cyan", addr="10.0.0.5")),
                         "type=cyan_nrnt,addr=10.0.0.5")
        self.assertEqual(D.build_pervices_args(self.dev("cyan_hb")), "type=cyan_nrnt")
        self.assertEqual(D.build_pervices_args(self.dev("chestnut", addr="a")), "type=chestnut,addr=a")
        self.assertEqual(D.build_pervices_args(self.dev("calamine", addr="a")), "type=crimson_tng,addr=a")

    def test_tree_passthrough_and_args(self):
        d = self.dev("crimson_tng", addr="192.168.10.2", args="type=crimson_tng,flag",
                     pervices_tree=(("/mboards/0/rx_dsps/0/delay_iq", "3 3"),
                                    ("fpga/link/sfpa/mtu", "9000")))
        self.assertEqual(D.build_pervices_args(d),
                         "type=crimson_tng,addr=192.168.10.2,flag,"
                         "crimson:/mboards/0/rx_dsps/0/delay_iq=3 3,crimson:fpga/link/sfpa/mtu=9000")
        ok = self.dev("crimson_tng", args="crimson:rx/a/rf/gain/val=10",
                      pervices_tree=(("time/source/ref", "external"), ("rx/b/dsp/rate", "1e6"),
                                     ("/mboards/0/clock_source/value", "external")))
        self.assertIn("crimson:rx/a/rf/gain/val=10", D.build_pervices_args(ok))
        d = self.dev("cyan", args="addr=10.0.0.9")
        self.assertEqual(D.build_pervices_args(d), "type=cyan_nrnt,addr=10.0.0.9")

    def test_rejections(self):
        def code(d):
            with self.assertRaises(ConfigError) as cm:
                D.build_pervices_args(d)
            return cm.exception.issues[0].code
        self.assertEqual(code(self.dev("crimson_tng", pervices_tree=(("tx/a/sob", "1"),))),
                         "DEVICE_PERVICES_TREE")
        self.assertEqual(code(self.dev("crimson_tng", args="crimson:/fpga/SOB=1")), "DEVICE_PERVICES_TREE")
        self.assertEqual(code(self.dev("cyan", args="type=crimson_tng")), "DEVICE_PERVICES_TYPE")
        self.assertEqual(code(self.dev("calamine", args="type=calamine")), "DEVICE_PERVICES_TYPE")
        self.assertEqual(code(self.dev("crimson_tng", args="addr0=1.1.1.1,addr1=1.1.1.2")),
                         "DEVICE_PERVICES_COMPOUND")
        self.assertEqual(code(self.dev("crimson_tng", addr="a", args="addr=b")), "DEVICE_ARGS")
        self.assertEqual(code(self.dev("crimson_tng", pervices_tree=(("a", "1,2"),))),
                         "DEVICE_PERVICES_TREE")
        self.assertEqual(code(self.dev("n320")), "DEVICE_BACKEND")

    def test_tx_and_trigger_tree_paths_rejected(self):
        """The driver writes crimson:<path> at construction, outside every TX limit."""
        def code(d):
            with self.assertRaises(ConfigError) as cm:
                D.build_pervices_args(d)
            return cm.exception.issues[0].code
        bad = ("tx/a/rf/gain/val", "tx/a/pwr", "tx/a/rf/atten/val", "tx/b/dsp/rstreq",
               "tx/a/trigger/trig_sel", "fpga/trigger/sma_mode", "/TX/A/PWR", "tx//a/pwr",
               "/mboards/0/tx_dsps/0/rate", "rx/a/trigger/sma_mode", "fpga/board/sys_rstreq",
               "fpga/x", "a")
        for path in bad:
            self.assertEqual(code(self.dev("crimson_tng", pervices_tree=((path, "1"),))),
                             "DEVICE_PERVICES_TREE", path)
            self.assertEqual(code(self.dev("cyan", args="crimson:%s=1" % path)),
                             "DEVICE_PERVICES_TREE", path)
        # rejected even on an RX-only device (no tx_channels)
        self.assertEqual(code(self.dev("crimson_tng", rx_channels=(0,), tx_channels=(),
                                       pervices_tree=(("tx/a/pwr", "1"),))), "DEVICE_PERVICES_TREE")

    def test_tx_tree_paths_reported_by_plan(self):
        cfg = cfg_of({"c": {"family": "crimson_tng", "addr": "192.168.10.2", "tx_channels": "0",
                            "pervices_tree": "tx/a/rf/gain/val=31.75; tx/a/pwr=1",
                            "args": "crimson:tx/a/rf/atten/val=0"}},
                     backend="pervices", rf={"sample_rate_hz": 325e6 / 4}, tx={"max_tx_gain_db": 0})
        self.assertIn("DEVICE_PERVICES_TREE", codes(plan(cfg), "error"))


class TestPlanRules(unittest.TestCase):
    def assertRule(self, cfg, code, level="error"):
        iss = plan(cfg)
        self.assertIn(code, codes(iss, level), [str(i) for i in iss])
        return iss

    def test_clean_plans(self):
        cfgs = [
            cfg_of({"b": {"family": "b210", "serial": "1", "rx_channels": "0,1"}},
                   rf={"sample_rate_hz": 20e6}),
            cfg_of({"x": {"family": "x310_ubx", "addr": "192.168.40.2", "rx_channels": "0,1"}},
                   rf={"sample_rate_hz": 10e6}),
            cfg_of({"c": {"family": "crimson_tng", "addr": "192.168.10.2", "rx_channels": "0..3",
                          "tx_channels": "0..3"}}, backend="pervices", rf={"sample_rate_hz": 325e6 / 4}),
            C.default_config(),
        ]
        for cfg in cfgs:
            self.assertEqual(codes(plan(cfg), "error"), [], [str(i) for i in plan(cfg)])

    def test_channel_rules(self):
        self.assertRule(cfg_of({"b": {"family": "b210", "rx_channels": "0,2"}}), "DEVICE_CHANNEL")
        self.assertRule(cfg_of({"t": {"family": "x310_twinrx", "addr": "a", "tx_channels": "0"}}),
                        "DEVICE_CHANNEL")
        self.assertRule(cfg_of({"b": {"family": "b210", "rx_channels": "0,0"}}), "DEVICE_CHANNEL")

    def test_freq(self):
        self.assertRule(cfg_of({"b": {"family": "b210"}}, rf={"center_freq_hz": 50e6}), "DEVICE_FREQ")
        self.assertRule(cfg_of({"c": {"family": "crimson_tng"}}, backend="pervices",
                               rf={"center_freq_hz": 120e6, "sample_rate_hz": 325e6 / 64}),
                        "DEVICE_PERVICES_BAND_EDGE", "warning")

    def test_x410(self):
        devs = {"a": {"family": "x410", "addr": "1", **EXT}, "b": {"family": "x410", "addr": "2", **EXT}}
        self.assertRule(cfg_of(devs), "DEVICE_X410_MULTI")
        self.assertRule(cfg_of(devs, output={"output_mode": "raw"}), "DEVICE_X410_MULTI", "warning")
        self.assertRule(cfg_of({"a": {"family": "x410", "addr": "1"}}, timing={"timed_tune": "on"}),
                        "DEVICE_X410_TIMED_TUNE")
        iss = self.assertRule(cfg_of({"a": {"family": "x410", "addr": "1"}}), "DEVICE_TIMED_TUNE", "info")
        self.assertFalse(D.resolve_timed_tune(cfg_of({"a": {"family": "x410", "addr": "1"}})))
        self.assertNotIn("DEVICE_X410_TIMED_TUNE", codes(iss))

    def test_n3xx_ext_gpsdo(self):
        self.assertRule(cfg_of({"n": {"family": "n320", "addr": "1", "clock_source": "external",
                                      "time_source": "gpsdo"}}), "DEVICE_N3XX_EXT_GPSDO")

    def test_b2xx_rules(self):
        self.assertRule(cfg_of({"b": {"family": "b210", "clock_source": "external", "gpsdo_fitted": 1}}),
                        "DEVICE_B2XX_GPSDO_EXT")
        self.assertRule(cfg_of({"b": {"family": "b210", "clock_source": "gpsdo"}}), "DEVICE_SOURCE")
        self.assertRule(cfg_of({"b": {"family": "b210", "rx_channels": "0,1"}},
                               rf={"sample_rate_hz": 40e6}), "DEVICE_B2XX_RATE")
        self.assertRule(cfg_of({"b": {"family": "b210", "rx_channels": "0,1",
                                      "master_clock_rate_hz": 40e6}}, rf={"sample_rate_hz": 10e6}),
                        "DEVICE_B2XX_RATE")
        ok = cfg_of({"b": {"family": "b210", "rx_channels": "0"}}, rf={"sample_rate_hz": 40e6})
        self.assertNotIn("DEVICE_B2XX_RATE", codes(plan(ok)))
        pinned = {"a": {"family": "b210", "serial": "1", "master_clock_rate_hz": 30.72e6, **EXT},
                  "b": {"family": "b210", "serial": "2", "master_clock_rate_hz": 30.72e6, **EXT}}
        iss = self.assertRule(cfg_of(pinned), "DEVICE_B2XX_MULTI", "warning")
        self.assertNotIn("DEVICE_B2XX_MULTI", codes(iss, "error"))
        unpinned = {"a": {"family": "b210", "serial": "1", **EXT}, "b": {"family": "b210", "serial": "2",
                                                                       **EXT}}
        self.assertRule(cfg_of(unpinned), "DEVICE_B2XX_MULTI", "error")

    def test_no_shared_ref(self):
        devs = {"a": {"family": "b210", "serial": "1", "master_clock_rate_hz": 30.72e6, **EXT},
                "b": {"family": "b210", "serial": "2", "master_clock_rate_hz": 30.72e6, **EXT}}
        self.assertRule(cfg_of(devs, timing={"sync_method": "none"}), "DEVICE_NO_SHARED_REF")
        internal = {k: dict(v, clock_source="internal") for k, v in devs.items()}
        self.assertRule(cfg_of(internal), "DEVICE_NO_SHARED_REF")
        self.assertNotIn("DEVICE_NO_SHARED_REF", codes(plan(cfg_of(devs))))

    def test_pervices_rules(self):
        pv = {"backend": "pervices", "rf": {"sample_rate_hz": 325e6 / 4}}

        def crimson(**kw):
            d = {"family": "crimson_tng", "addr": "192.168.10.2"}
            d.update(kw)
            return d
        self.assertRule(cfg_of({"c": crimson(clock_source="gpsdo")}, **pv), "DEVICE_PERVICES_GPSDO")
        self.assertRule(cfg_of({"c": crimson()}, timing={"timed_tune": "on"}, **pv),
                        "DEVICE_PERVICES_TIMED_TUNE")
        self.assertRule(cfg_of({"c": crimson(args="addr0=1,addr1=2")}, **pv), "DEVICE_PERVICES_COMPOUND")
        two = {"c1": crimson(group="g", **EXT), "c2": dict(crimson(group="g", **EXT), addr="192.168.11.2")}
        iss = self.assertRule(cfg_of(two, **pv), "DEVICE_PERVICES_COMPOUND")
        self.assertEqual(len(D.device_groups(cfg_of(two, **pv))), 2)
        self.assertRule(cfg_of({"c": crimson()}, backend="pervices", rf={"sample_rate_hz": 100e6}),
                        "DEVICE_PERVICES_RATE")
        self.assertNotIn("DEVICE_PERVICES_RATE",
                         codes(plan(cfg_of({"c": crimson()}, backend="pervices",
                                           rf={"sample_rate_hz": 325e6 / 3}))))
        half = {"backend": "pervices", "rf": {"sample_rate_hz": 325e6 / 2}}
        self.assertRule(cfg_of({"c": crimson(tx_channels="0,2")}, **half), "DEVICE_PERVICES_TX_RATE")
        self.assertNotIn("DEVICE_PERVICES_TX_RATE",
                         codes(plan(cfg_of({"c": crimson(tx_channels="0,2", pervices_full_tx_image=1)},
                                           **half))))
        self.assertNotIn("DEVICE_PERVICES_TX_RATE", codes(plan(cfg_of({"c": crimson(tx_channels="0,1")},
                                                                      **half))))
        self.assertRule(cfg_of({"c": crimson(pervices_tree="a/sob=1")}, **pv), "DEVICE_PERVICES_TREE")
        self.assertRule(cfg_of({"c": crimson(args="type=cyan_nrnt")}, **pv), "DEVICE_PERVICES_TYPE")
        del iss

    def test_recal_rule(self):
        pv = cfg_of({"c": {"family": "crimson_tng"}}, backend="pervices",
                    rf={"sample_rate_hz": 325e6 / 4}, calibration={"recal_on_tune": 0})
        self.assertRule(pv, "DEVICE_RECAL")
        ubx = cfg_of({"x": {"family": "x310_ubx", "addr": "1"}}, calibration={"recal_on_tune": 0})
        self.assertEqual(D.phase_after_tune(ubx), "repeatable")
        self.assertNotIn("DEVICE_RECAL", codes(plan(ubx)))
        ubx_off = cfg_of({"x": {"family": "x310_ubx", "addr": "1"}}, calibration={"recal_on_tune": 0},
                         timing={"timed_tune": "off"})
        self.assertRule(ubx_off, "DEVICE_RECAL")

    def test_info_rules(self):
        self.assertRule(cfg_of({"n": {"family": "n310", "addr": "1", "lo_source": "external"}}),
                        "DEVICE_N310_180", "info")
        self.assertRule(cfg_of({"x": {"family": "x440", "addr": "1", "master_clock_rate_hz": 500e6}},
                               rf={"sample_rate_hz": 50e6}), "DEVICE_X440", "info")

    def test_misc_rules(self):
        self.assertRule(cfg_of({"b": {"family": "b210"}}, rf={"rx_gain_db": 80}), "DEVICE_GAIN")
        self.assertRule(cfg_of({"b": {"family": "b210", "tx_channels": "0"}},
                               tx={"max_tx_gain_db": 95}), "DEVICE_GAIN")
        self.assertRule(cfg_of({"b": {"family": "b210", "max_tx_dbm_override": 25}}), "DEVICE_TX_OVERRIDE")
        self.assertRule(cfg_of({"x": {"family": "x310_ubx", "master_clock_rate_hz": 100e6}}),
                        "DEVICE_MCR")
        self.assertRule(cfg_of({"b": {"family": "b210", "master_clock_rate_hz": 70e6}}), "DEVICE_MCR")
        self.assertRule(cfg_of({"n": {"family": "n300", "lo_source": "external"}}), "DEVICE_LO")
        self.assertRule(cfg_of({"n": {"family": "n320", "lo_export": 1}}), "DEVICE_LO")
        self.assertRule(cfg_of({"c": {"family": "crimson_tng"}}, backend="uhd",
                               rf={"sample_rate_hz": 325e6 / 4}), "DEVICE_BACKEND")
        self.assertRule(cfg_of({"b": {"family": "b210"}}, backend="pervices"), "DEVICE_BACKEND")
        self.assertRule(cfg_of({"s": {"family": "sim"}}, backend="gnuradio"), "DEVICE_BACKEND")
        self.assertRule(cfg_of({"b": {"family": "b210"}}, rf={"sample_rate_hz": 80e6}), "DEVICE_RATE")
        self.assertRule(cfg_of({"b": {"family": "b210", "rx_channels": "0,1"}},
                               rf={"sample_rate_hz": 40e6}), "DEVICE_B2XX_RATE")
        self.assertRule(cfg_of({"x": {"family": "x310_ubx"}}, rf={"sample_rate_hz": 3e6}),
                        "DEVICE_RATE", "warning")
        two = {"a": {"family": "sim", "role": "master"}, "b": {"family": "sim", "role": "master"}}
        self.assertRule(cfg_of(two, backend="sim"), "DEVICE_ROLE")

    def test_lo_plans_and_phase(self):
        star = TestGrouping.STAR
        cfg = cfg_of({k: dict(v, group="") for k, v in star.items()}, rf={"center_freq_hz": 2.4e9})
        self.assertEqual(D.resolve_lo_plan(cfg), "n321_star")
        self.assertEqual(D.phase_after_tune(cfg), "shared_lo")
        low = cfg_of({k: dict(v, group="") for k, v in star.items()}, rf={"center_freq_hz": 300e6})
        self.assertEqual(D.phase_after_tune(low), "random")
        self.assertIn("DEVICE_LO", codes(plan(low), "warning"))
        b = cfg_of({"b": {"family": "b210", "rx_channels": "0,1"}})
        self.assertEqual(D.phase_after_tune(b), "shared_lo")
        n310 = cfg_of({"n": {"family": "n310", "addr": "1", "lo_source": "external"}})
        self.assertEqual(D.resolve_lo_plan(n310), "external_lo")
        self.assertEqual(D.phase_after_tune(n310), "ambiguous_180")
        x440 = cfg_of({"x": {"family": "x440", "addr": "1"}})
        self.assertEqual(D.phase_after_tune(x440), "deterministic")
        tw = cfg_of({"t": {"family": "x310_twinrx", "addr": "1", "rx_channels": "0..3",
                           "lo_source": "companion"}})
        self.assertEqual(D.resolve_lo_plan(tw), "twinrx")
        self.assertEqual(D.phase_after_tune(tw), "shared_lo")
        sim_pv = C.default_config(sim={"emulate_family": "crimson_tng"})
        self.assertEqual(D.phase_after_tune(sim_pv), "random")
        self.assertEqual(D.phase_after_tune(C.default_config()), "repeatable")

    def test_effective_sources_and_gains(self):
        one = cfg_of({"a": {"family": "n320", "addr": "1"}})
        self.assertEqual(D.effective_sources(one, one.device("a")), ("internal", "internal"))
        two = cfg_of({"a": {"family": "n320", "addr": "1"}, "b": {"family": "n320", "addr": "2",
                                                                 "time_source": "gpsdo"}})
        self.assertEqual(D.effective_sources(two, two.device("a")), ("external", "external"))
        self.assertEqual(D.effective_sources(two, two.device("b")), ("external", "gpsdo"))
        cfg = cfg_of({"a": {"family": "n320", "addr": "1", "tx_channels": "0,1"}},
                     tx={"max_tx_gain_db": ""})
        self.assertEqual(D.resolve_max_tx_gain_db(cfg), [0.0, 0.0])
        cfg = cfg_of({"a": {"family": "n320", "addr": "1", "tx_channels": "0,1"}},
                     tx={"max_tx_gain_db": "10,20"})
        self.assertEqual(D.resolve_max_tx_gain_db(cfg), [10.0, 20.0])


class TestFullRate(unittest.TestCase):
    """fs = MCR is valid on UHD: the bound is the largest sample rate, not max_bw."""

    def check(self, family, mcr, fs, bw_warn):
        cfg = cfg_of({"d": {"family": family, "addr": "1", "master_clock_rate_hz": mcr}},
                     rf={"sample_rate_hz": fs})
        iss = plan(cfg)
        self.assertNotIn("DEVICE_RATE", codes(iss, "error"), [str(i) for i in iss])
        self.assertEqual("DEVICE_RATE" in codes(iss, "warning"), bw_warn, [str(i) for i in iss])

    def test_full_rate_setups(self):
        self.check("n310", 125e6, 125e6, True)        # analog BW 100 MHz -> roll-off warning
        self.check("n300", 153.6e6, 153.6e6, True)
        self.check("x310_ubx", 200e6, 200e6, True)
        self.check("x310_sbx", 200e6, 200e6, True)
        self.check("n320", 245.76e6, 245.76e6, True)
        self.check("n321", 250e6, 250e6, True)
        self.check("x410", 491.52e6, 491.52e6, True)
        self.check("x410", 500e6, 500e6, True)
        self.check("n310", 125e6, 62.5e6, False)

    def test_bounds(self):
        self.assertEqual(D.max_sample_rate_hz(C.DeviceCfg(name="n", family="n310")), 153.6e6)
        self.assertEqual(D.max_sample_rate_hz(C.DeviceCfg(name="n", family="n310",
                                                          master_clock_rate_hz=125e6)), 125e6)
        self.assertEqual(D.max_sample_rate_hz(C.DeviceCfg(name="b", family="b210")), 61.44e6)
        self.assertEqual(D.max_sample_rate_hz(C.DeviceCfg(name="c", family="crimson_tng")), 325e6)
        # above the (pinned) MCR is still an error
        cfg = cfg_of({"d": {"family": "n310", "addr": "1", "master_clock_rate_hz": 125e6}},
                     rf={"sample_rate_hz": 153.6e6})
        self.assertIn("DEVICE_RATE", codes(plan(cfg), "error"))
        cfg = cfg_of({"d": {"family": "x310_ubx", "addr": "1"}}, rf={"sample_rate_hz": 250e6})
        self.assertIn("DEVICE_RATE", codes(plan(cfg), "error"))
        # bandwidth above the analog bandwidth warns even at a modest fs
        cfg = cfg_of({"d": {"family": "b210", "serial": "1"}},
                     rf={"sample_rate_hz": 50e6, "bandwidth_hz": 60e6})
        self.assertIn("DEVICE_RATE", codes(plan(cfg), "warning"))


class TestRateBudget(unittest.TestCase):
    def test_bytes_per_sample(self):
        self.assertEqual(D.bytes_per_sample("sc16", "n320", "rx"), 4.0)
        self.assertEqual(D.bytes_per_sample("sc8", "n320", "tx"), 2.0)
        self.assertEqual(D.bytes_per_sample("sc12", "x410", "rx"), 3.0)
        self.assertEqual(D.bytes_per_sample("sc16", "cyan_hb", "rx"), 3.0)
        self.assertEqual(D.bytes_per_sample("sc16", "cyan_hb", "tx"), 4.0)
        with self.assertRaises(ValueError):
            D.bytes_per_sample("fc32", "n320", "rx")
        with self.assertRaises(ValueError):
            D.bytes_per_sample("sc16", "n320", "up")

    def test_crimson_one_port(self):
        full = cfg_of({"c": {"family": "crimson_tng", "rx_channels": "0"}}, backend="pervices",
                      rf={"sample_rate_hz": 325e6})
        iss = D.rate_budget(full)
        self.assertIn("RATE", codes(iss, "error"))
        self.assertIn("SFP+A", iss[0].message)
        half = cfg_of({"c": {"family": "crimson_tng", "rx_channels": "0"}}, backend="pervices",
                      rf={"sample_rate_hz": 162.5e6})
        self.assertEqual(D.rate_budget(half), [])
        # A and C share SFP+A: 2 x 162.5 MS/s = 10.4 Gbps -> error; A and B are on separate ports
        ac_ = cfg_of({"c": {"family": "crimson_tng", "rx_channels": "0,2"}}, backend="pervices",
                     rf={"sample_rate_hz": 162.5e6})
        self.assertIn("RATE", codes(D.rate_budget(ac_), "error"))
        ab = cfg_of({"c": {"family": "crimson_tng", "rx_channels": "0,1"}}, backend="pervices",
                    rf={"sample_rate_hz": 162.5e6})
        self.assertEqual(D.rate_budget(ab), [])

    def test_cyan_ports(self):
        one = cfg_of({"y": {"family": "cyan", "rx_channels": "0,1", "link_ports": 1, "link_gbps": 40}},
                     backend="pervices", rf={"sample_rate_hz": 1e9})
        self.assertIn("RATE", codes(D.rate_budget(one), "error"))
        spread = cfg_of({"y": {"family": "cyan", "rx_channels": "0,2", "link_ports": 2,
                               "link_gbps": 40}}, backend="pervices", rf={"sample_rate_hz": 1e9})
        iss = D.rate_budget(spread)                        # 32 Gbps per 39.2 Gbps port: > 80 %
        self.assertEqual(codes(iss), ["RATE", "RATE"])
        self.assertEqual({i.level for i in iss}, {"warning"})
        hundred = cfg_of({"y": {"family": "cyan", "rx_channels": "0,1", "link_ports": 1,
                                "link_gbps": 100}}, backend="pervices", rf={"sample_rate_hz": 1e9})
        self.assertNotIn("error", [i.level for i in D.rate_budget(hundred)])

    def test_ettus_link_and_sim(self):
        cfg = cfg_of({"x": {"family": "x410", "addr": "1", "rx_channels": "0..3",
                            "master_clock_rate_hz": 500e6}}, rf={"sample_rate_hz": 250e6})
        self.assertIn("RATE", codes(D.rate_budget(cfg), "error"))       # 32 Gbps on one 10G link
        cfg = cfg_of({"x": {"family": "x410", "addr": "1", "rx_channels": "0..3", "link_ports": 2,
                            "link_gbps": 100, "master_clock_rate_hz": 500e6}},
                     rf={"sample_rate_hz": 250e6})
        self.assertEqual(D.rate_budget(cfg), [])
        self.assertEqual(D.rate_budget(C.default_config()), [])
        rows = D.port_traffic(cfg)
        self.assertEqual(rows[0][:3], ("x", "link", "rx"))
        self.assertAlmostEqual(rows[0][3], 32.0)


class TestPlanHardening(unittest.TestCase):
    """Regression tests: channel aliasing, LO-share membership, compound refs,
    per-device compound args, Per Vices wire format."""

    def test_subdev_entry_count_bounds_channels(self):
        devs = {"a": {"family": "n310", "addr": "10.0.0.1", "subdev_tx": "A:0", "tx_channels": "0,1",
                      **EXT},
                "b": {"family": "n310", "addr": "10.0.0.2", "tx_channels": "0", **EXT}}
        cfg = cfg_of(devs)
        g = D.device_groups(cfg)[0]
        self.assertTrue(g.compound)
        with self.assertRaises(ConfigError) as cm:
            g.channel_map(cfg, "tx")
        self.assertEqual(cm.exception.issues[0].code, "DEVICE_CHANNEL")
        iss = plan(cfg)
        self.assertTrue(any(i.code == "DEVICE_CHANNEL" and "subdev_tx" in i.message for i in iss),
                        [str(i) for i in iss])
        # a subdev spec with enough entries: no aliasing, distinct MultiUSRP channels
        ok = cfg_of({"a": dict(devs["a"], subdev_tx="A:0 A:1"), "b": devs["b"]})
        cmap = D.device_groups(ok)[0].channel_map(ok, "tx")
        self.assertEqual([m for m, _g, _r in cmap], [0, 1, 2])
        self.assertNotIn("DEVICE_CHANNEL", codes(plan(ok)))

    STAR = {
        "m": {"family": "n321", "addr": "10.0.0.1", "group": "star", "lo_export": 1, **EXT},
        "i": {"family": "n320", "addr": "10.0.0.2", "group": "star", "lo_source": "external", **EXT},
    }

    def star(self, extra=None, lo_export=1, **timing):
        devs = {k: dict(v) for k, v in self.STAR.items()}
        devs["m"]["lo_export"] = lo_export
        devs.update(extra or {})
        return cfg_of(devs, rf={"center_freq_hz": 2.4e9}, calibration={"recal_on_tune": 0},
                      timing=dict({"lo_plan": "n321_star"}, **timing))

    def test_lo_star_membership(self):
        good = self.star()
        self.assertEqual(D.phase_after_tune(good), "shared_lo")
        self.assertNotIn("DEVICE_LO", codes(plan(good), "error"))
        self.assertNotIn("DEVICE_RECAL", codes(plan(good)))
        # a member that neither exports nor imports the LO
        outsider = {"x": {"family": "n320", "addr": "10.0.0.3", "group": "star", **EXT}}
        cfg = self.star(outsider)
        self.assertNotEqual(D.phase_after_tune(cfg), "shared_lo")
        iss = plan(cfg)
        self.assertIn("DEVICE_RECAL", codes(iss, "error"))
        self.assertTrue(any(i.code == "DEVICE_LO" and i.where.startswith("[active_device:x]")
                            for i in iss), [str(i) for i in iss])
        mem = D.lo_share_members(cfg)
        self.assertEqual((mem["exporters"], mem["importers"], mem["outside"]), (["m"], ["i"], ["x"]))
        # explicit plan without an exporter
        cfg = self.star(lo_export=0)
        self.assertNotEqual(D.phase_after_tune(cfg), "shared_lo")
        iss = plan(cfg)
        self.assertIn("DEVICE_RECAL", codes(iss, "error"))
        self.assertTrue(any(i.code == "DEVICE_LO" and "exporter" in i.message for i in iss))
        # lo_master counts as the exporter
        cfg = self.star(lo_export=0, lo_master="m")
        self.assertEqual(D.phase_after_tune(cfg), "shared_lo")

    def test_twinrx_membership(self):
        tw = {"a": {"family": "x310_twinrx", "addr": "10.0.0.1", "rx_channels": "0..3", **EXT},
              "b": {"family": "x310_twinrx", "addr": "10.0.0.2", "rx_channels": "0..3", **EXT}}
        internal = cfg_of(tw, timing={"lo_plan": "twinrx"}, calibration={"recal_on_tune": 0})
        self.assertNotEqual(D.phase_after_tune(internal), "shared_lo")
        self.assertIn("DEVICE_LO", codes(plan(internal), "error"))
        shared = cfg_of({"a": dict(tw["a"], lo_export=1), "b": dict(tw["b"], lo_source="external")},
                        timing={"lo_plan": "twinrx"}, calibration={"recal_on_tune": 0})
        self.assertEqual(D.phase_after_tune(shared), "shared_lo")
        self.assertNotIn("DEVICE_LO", codes(plan(shared), "error"))
        no_exp = cfg_of({"a": dict(tw["a"], lo_source="external"), "b": dict(tw["b"], lo_source="external")},
                        timing={"lo_plan": "twinrx"}, calibration={"recal_on_tune": 0})
        self.assertNotEqual(D.phase_after_tune(no_exp), "shared_lo")
        self.assertTrue(any(i.code == "DEVICE_LO" and "exporter" in i.message for i in plan(no_exp)))

    def test_compound_group_needs_shared_ref(self):
        devs = {"a": {"family": "n310", "addr": "10.0.0.1"}, "b": {"family": "n310", "addr": "10.0.0.2"}}
        cfg = cfg_of(devs, timing={"clock_source": "internal", "time_source": "internal"})
        self.assertEqual(len(D.device_groups(cfg)), 1)
        self.assertIn("DEVICE_NO_SHARED_REF", codes(plan(cfg), "error"))
        cfg = cfg_of(devs, timing={"sync_method": "none"})
        self.assertIn("DEVICE_NO_SHARED_REF", codes(plan(cfg), "error"))
        ok = cfg_of(devs)                                        # auto -> external for several
        self.assertNotIn("DEVICE_NO_SHARED_REF", codes(plan(ok)))
        gps = cfg_of(devs, timing={"clock_source": "gpsdo", "time_source": "gpsdo"})
        self.assertNotIn("DEVICE_NO_SHARED_REF", codes(plan(gps)))
        one = cfg_of({"a": devs["a"]}, timing={"clock_source": "internal", "time_source": "internal"})
        self.assertNotIn("DEVICE_NO_SHARED_REF", codes(plan(one)))

    def test_compound_per_device_args_indexed(self):
        devs = {"a": {"family": "n320", "addr": "10.0.0.1", "args": "mgmt_addr=192.168.1.1", **EXT},
                "b": {"family": "n320", "addr": "10.0.0.2", "args": "mgmt_addr=192.168.1.2,"
                                                                   "second_addr=10.0.1.2", **EXT}}
        cfg = cfg_of(devs)
        args = D.parse_device_args(D.build_uhd_args(D.device_groups(cfg)[0], cfg))
        self.assertEqual(args["mgmt_addr0"], "192.168.1.1")
        self.assertEqual(args["mgmt_addr1"], "192.168.1.2")
        self.assertEqual(args["second_addr1"], "10.0.1.2")
        self.assertNotIn("mgmt_addr", args)
        self.assertNotIn("second_addr0", args)
        # common keys stay unindexed and conflict-checked
        same = {k: dict(v, args="num_recv_frames=256") for k, v in devs.items()}
        cfg = cfg_of(same)
        args = D.parse_device_args(D.build_uhd_args(D.device_groups(cfg)[0], cfg))
        self.assertEqual(args["num_recv_frames"], "256")
        clash = {"a": dict(devs["a"], args="num_recv_frames=256"),
                 "b": dict(devs["b"], args="num_recv_frames=128")}
        cfg = cfg_of(clash)
        with self.assertRaises(ConfigError):
            D.build_uhd_args(D.device_groups(cfg)[0], cfg)
        # a single device keeps its keys as written
        one = cfg_of({"a": devs["a"]})
        self.assertIn("mgmt_addr=192.168.1.1", D.build_uhd_args(D.device_groups(one)[0], one))

    def test_pervices_otw_sc16_only(self):
        pv = dict(backend="pervices", rf={"sample_rate_hz": 325e6, "otw_format": "sc8"})
        cfg = cfg_of({"c": {"family": "crimson_tng", "addr": "1", "rx_channels": "0"}}, **pv)
        self.assertIn("DEVICE_PERVICES_OTW", codes(plan(cfg), "error"))
        self.assertIn("RATE", codes(D.rate_budget(cfg), "error"))    # still 4 B/S: 106 %
        self.assertEqual(D.bytes_per_sample("sc8", "crimson_tng", "rx"), 4.0)
        self.assertEqual(D.bytes_per_sample("sc12", "cyan", "tx"), 4.0)
        self.assertEqual(D.bytes_per_sample("sc8", "cyan_hb", "rx"), 3.0)
        self.assertEqual(D.bytes_per_sample("sc8", "n320", "rx"), 2.0)
        ok = cfg_of({"c": {"family": "crimson_tng", "addr": "1", "rx_channels": "0"}},
                    backend="pervices", rf={"sample_rate_hz": 325e6 / 4})
        self.assertNotIn("DEVICE_PERVICES_OTW", codes(plan(ok)))


class TestFrozen(unittest.TestCase):
    def test_caps_and_group_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            D.get_caps("b210").n_rx = 4
        g = D.device_groups(C.default_config())[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            g.name = "x"


if __name__ == "__main__":
    unittest.main()
