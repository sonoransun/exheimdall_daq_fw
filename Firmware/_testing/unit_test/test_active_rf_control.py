"""
    Unit tests for the active RF front-end line control (Firmware/_active/active_rf_control.py).

    Hardware-free: a recording fake backend and fake sleep; no real waits.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import json
import logging
import math
import os
import sys
import tempfile
import time
import unittest
from os.path import dirname, join, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_config as C  # noqa: E402
import active_devices as DEV  # noqa: E402
import active_rf_control as RF  # noqa: E402
from active_common import ChannelRef, ConfigError, HardwareFault, InterlockTrip, SafetyRefusal  # noqa: E402

logging.getLogger("heimdall.active").setLevel(logging.CRITICAL + 10)


class FakeBackend:
    """Records every GPIO / gain / ATR call in one ordered log shared with the fake sleep."""

    def __init__(self, log, n_rx=2, fail_write=None):
        self.log = log
        self.caps = {"sim0": DEV.get_caps("sim")}
        self._rx = [ChannelRef("sim0", k) for k in range(n_rx)]
        self.gains = [30.0] * n_rx
        self.fail_write = fail_write          # callable(line, value) -> bool
        self.inputs = {}
        self.atr_calls = []
        self.atr_result = True
        self.tel = None

    def rx_refs(self):
        return list(self._rx)

    def rx_gain_db(self):
        return list(self.gains)

    def set_rx_gain(self, g):
        self.log.append(("set_rx_gain", tuple(g)))
        self.gains = list(g)

    def gpio_write(self, line, value):
        if self.fail_write is not None and self.fail_write(line, value):
            self.log.append(("gpio_fail", str(line), value))
            raise HardwareFault("gpio write failed")
        self.log.append(("gpio", str(line), bool(value)))

    def gpio_read(self, line):
        v = self.inputs.get(str(line))
        if isinstance(v, Exception):
            raise v
        return bool(v)

    def gpio_atr(self, device, bank, ctrl, idle, rx, tx, duplex):
        self.atr_calls.append((device, bank, ctrl, idle, rx, tx, duplex))
        return self.atr_result

    def telemetry(self):
        return self.tel


class StubSafety:
    """Minimal safety double: permits are accepted when listed in ``active``."""

    class Policy:
        mute_rx_during_tx = True
        lna_off_during_tx = True

    def __init__(self):
        self.policy = self.Policy()
        self.active = set()
        self.trips = []
        self.arm_record = None

    def permit_active(self, p):
        return id(p) in self.active

    def trip(self, code, detail=""):
        self.trips.append(code)


class Permit:
    def __init__(self, chains):
        self.chains = tuple(chains)


def rf_cfg(**kw):
    base = dict(pa_enable_line="sim:pa", tr_switch_line="sim:tr", lna_bias_line="sim:lna",
                cal_injection_line="sim:inj", cal_loopback_line="sim:loop", noise_source_line="sim:ns",
                use_atr=False, tr_guard_us=50.0, pa_settle_us=100.0)
    base.update(kw)
    return C.RfControlCfg(**base)


def make(n_tx=2, fail_write=None, **kw):
    log = []
    be = FakeBackend(log, fail_write=fail_write)
    saf = StubSafety()
    rc = RF.RfControl(rf_cfg(**kw), n_tx, be, None, saf, sleep=lambda s: log.append(("sleep", s)))
    return rc, be, saf, log


class TestLineSpecs(unittest.TestCase):
    def test_grammar(self):
        self.assertEqual(RF.parse_line("none").kind, "none")
        self.assertEqual(RF.parse_line("").kind, "none")
        self.assertEqual(RF.parse_line(" NONE ").kind, "none")
        g = RF.parse_line("n321a:FP0:0x0c")
        self.assertEqual((g.kind, g.device, g.bank, g.mask, g.active_high), ("gpio", "n321a", "FP0", 12, True))
        lo = RF.parse_line("b210:FP0:3:low")
        self.assertFalse(lo.active_high)
        self.assertEqual(lo.level_bits(True), 0)
        self.assertEqual(lo.level_bits(False), 3)
        self.assertEqual(g.level_bits(True), 12)
        s = RF.parse_line("sim:pa0")
        self.assertEqual((s.kind, s.name), ("sim", "pa0"))
        self.assertEqual(RF.parse_line("sim").kind, "sim")
        f = RF.parse_line("file:/tmp/il.state")
        self.assertEqual((f.kind, f.path), ("file", "/tmp/il.state"))
        self.assertEqual(str(g), "n321a:FP0:0xc")
        self.assertEqual(str(lo), "b210:FP0:0x3:low")
        self.assertEqual(RF.resolve_line(RF.parse_line("file:x/il"), "/base").path, "/base/x/il")

    def test_errors(self):
        for bad in ("dev:FP0:0", "dev:FP0:0x100000000", "dev:FP0", "dev:FP0:3:inverted", "dev::3",
                    "dev:FP0:abc", "sim:", "file:", "a:b:c:d:e"):
            with self.assertRaises(ConfigError, msg=bad) as cm:
                RF.parse_line(bad)
            self.assertEqual(cm.exception.issues[0].code, "LINE_SPEC", bad)

    def test_list(self):
        self.assertEqual(len(RF.parse_line_list("sim:pa", 4)), 4)
        self.assertEqual(len(set(RF.parse_line_list("sim:pa", 4))), 1)
        lst = RF.parse_line_list("d:FP0:1, d:FP0:2, d:FP0:4", 3)
        self.assertEqual([x.mask for x in lst], [1, 2, 4])
        self.assertEqual(RF.parse_line_list("none", 3), [RF.NONE_LINE] * 3)
        self.assertEqual(RF.parse_line_list("sim:pa", 0), [])
        with self.assertRaises(ConfigError) as cm:
            RF.parse_line_list("d:FP0:1,d:FP0:2", 3)
        self.assertEqual(cm.exception.issues[0].code, "LIST_LEN")

    def test_output_lines_cannot_be_files(self):
        with self.assertRaises(ConfigError):
            make(pa_enable_line="file:/tmp/x")
        with self.assertRaises(ConfigError):
            make(tr_switch_line="file:/tmp/x")


class TestSequencing(unittest.TestCase):
    def test_sequence_to_tx_order_and_guards(self):
        rc, be, saf, log = make()
        p = Permit([0, 1])
        saf.active.add(id(p))
        steps = rc.sequence_to_tx(p)
        self.assertEqual([s[0] for s in steps],
                         ["rx_mute", "lna_off", "settle", "tr_tx", "tr_guard", "pa_on", "pa_settle"])
        self.assertEqual(log, [
            ("set_rx_gain", (0.0, 0.0)),
            ("gpio", "sim:lna", False),
            ("sleep", 50e-6),
            ("gpio", "sim:tr", True),
            ("sleep", 50e-6),
            ("gpio", "sim:pa", True),
            ("sleep", 100e-6),
        ])
        self.assertEqual(rc.state["pa"], [True, True])
        self.assertEqual(rc.state["tr"], "tx")
        self.assertFalse(rc.state["lna"])

    def test_sequence_to_rx_order_and_restore(self):
        rc, be, saf, log = make()
        p = Permit([0, 1])
        saf.active.add(id(p))
        rc.sequence_to_tx(p)
        del log[:]
        steps = rc.sequence_to_rx()
        self.assertTrue(all(ok for _, ok, _ in steps))
        self.assertEqual(log, [
            ("gpio", "sim:pa", False),
            ("sleep", 100e-6),
            ("gpio", "sim:tr", False),
            ("sleep", 50e-6),
            ("gpio", "sim:lna", True),
            ("set_rx_gain", (30.0, 30.0)),
        ])
        self.assertEqual(rc.state["pa"], [False, False])
        self.assertEqual(rc.state["tr"], "rx")
        self.assertTrue(rc.state["lna"])
        self.assertEqual(be.gains, [30.0, 30.0])
        # idempotent and harmless when nothing is saved
        self.assertTrue(all(ok for _, ok, _ in rc.sequence_to_rx()))

    def test_policy_flags_respected(self):
        rc, be, saf, log = make()
        saf.policy.mute_rx_during_tx = False
        saf.policy.lna_off_during_tx = False
        p = Permit([1])
        saf.active.add(id(p))
        rc.sequence_to_tx(p)
        self.assertNotIn(("set_rx_gain", (0.0, 0.0)), log)
        self.assertNotIn(("gpio", "sim:lna", False), log)
        self.assertEqual(rc.state["pa"], [False, True])

    def test_pa_on_without_permit_trips_sequence(self):
        rc, be, saf, log = make()
        with self.assertRaises(InterlockTrip) as cm:
            rc.pa_enable([0], True, Permit([0]))
        self.assertEqual(cm.exception.code, "SEQUENCE")
        self.assertEqual(saf.trips, ["SEQUENCE"])
        self.assertFalse(any(e[:2] == ("gpio", "sim:pa") and e[2] for e in log))
        with self.assertRaises(InterlockTrip):
            rc.pa_enable(None, True)
        with self.assertRaises(InterlockTrip):
            rc.sequence_to_tx(Permit([0]))
        no_safety = RF.RfControl(rf_cfg(), 2, be, None, None, sleep=lambda s: None)
        with self.assertRaises(InterlockTrip):
            no_safety.pa_enable([0], True, Permit([0]))

    def test_permit_must_cover_the_chains(self):
        rc, be, saf, log = make()
        p = Permit([0])
        saf.active.add(id(p))
        with self.assertRaises(InterlockTrip):
            rc.pa_enable([0, 1], True, p)
        rc.pa_enable([0], True, p)
        self.assertEqual(rc.state["pa"], [True, False])

    def test_hold_session_via_arm_record(self):
        rc, be, saf, log = make()
        rec = object()
        saf.arm_record = rec
        saf.active.add(id(rec))
        rc.sequence_to_tx(rec)
        self.assertEqual(rc.state["pa"], [True, True])
        rc.pa_enable(None, True)            # permit None: the armed hold session

    def test_tr_and_cal_refused_while_pa_on(self):
        rc, be, saf, log = make()
        p = Permit([0])
        saf.active.add(id(p))
        rc.pa_enable([0], True, p)
        for fn in (lambda: rc.tr_switch("rx"), lambda: rc.tr_switch("tx"), lambda: rc.cal_input("loopback"),
                   lambda: rc.cal_input("antenna"), lambda: rc.lna_bias(True)):
            with self.assertRaises(SafetyRefusal) as cm:
                fn()
            self.assertEqual(cm.exception.code, "SEQUENCE")
        rc.lna_bias(False)                  # turning the LNA off is always allowed
        rc.pa_enable([0], False)
        rc.tr_switch("rx")
        rc.cal_input("loopback")
        self.assertEqual(rc.state["cal_input"], "loopback")
        with self.assertRaises(ValueError):
            rc.tr_switch("duplex")
        with self.assertRaises(ValueError):
            rc.cal_input("sky")

    def test_cal_input_break_before_make(self):
        rc, be, saf, log = make()
        rc.cal_input("injection")
        self.assertEqual(log, [("gpio", "sim:inj", False), ("gpio", "sim:loop", False), ("gpio", "sim:inj", True)])
        del log[:]
        rc.cal_input("loopback")
        self.assertEqual(log, [("gpio", "sim:inj", False), ("gpio", "sim:loop", False), ("gpio", "sim:loop", True)])
        rc.noise_source(True)
        self.assertTrue(rc.state["noise"])

    def test_shared_line_stays_on_for_other_chain(self):
        rc, be, saf, log = make()
        p = Permit([0, 1])
        saf.active.add(id(p))
        rc.pa_enable([0, 1], True, p)
        del log[:]
        rc.pa_enable([0], False)
        self.assertEqual(log, [])           # the shared line still feeds chain 1
        self.assertEqual(rc.state["pa"], [False, True])
        rc.pa_enable([1], False)
        self.assertEqual(log, [("gpio", "sim:pa", False)])

    def test_per_chain_lines(self):
        rc, be, saf, log = make(pa_enable_line="sim:pa0,sim:pa1")
        p = Permit([1])
        saf.active.add(id(p))
        rc.pa_enable([1], True, p)
        self.assertEqual(log, [("gpio", "sim:pa1", True)])


class TestFailures(unittest.TestCase):
    def test_sequence_to_rx_never_raises(self):
        rc, be, saf, log = make()
        p = Permit([0, 1])
        saf.active.add(id(p))
        rc.sequence_to_tx(p)
        be.fail_write = lambda line, v: True
        steps = rc.sequence_to_rx()
        d = {s[0]: s[1] for s in steps}
        self.assertFalse(d["pa_off"])
        self.assertFalse(d["tr_rx"])        # never switch T/R while the PA may be on
        self.assertEqual(rc.state["pa"], [True, True])
        self.assertEqual(rc.state["tr"], "tx")

    def test_force_safe_never_raises(self):
        rc, be, saf, log = make()
        p = Permit([0, 1])
        saf.active.add(id(p))
        rc.sequence_to_tx(p)
        be.fail_write = lambda line, v: str(line) == "sim:pa"
        steps = rc.force_safe()
        self.assertEqual([s[:2] for s in steps], [("pa_off", False), ("tr_rx", False)])
        self.assertEqual(rc.state["pa"], [True, True])
        be.fail_write = lambda line, v: str(line) == "sim:tr"
        steps = rc.force_safe()
        self.assertEqual([s[:2] for s in steps], [("pa_off", True), ("tr_rx", False)])
        be.fail_write = None
        self.assertEqual([s[:2] for s in rc.force_safe()], [("pa_off", True), ("tr_rx", True)])
        self.assertEqual(rc.state["tr"], "rx")
        broken = RF.RfControl(rf_cfg(), 2, None, None, saf, sleep=lambda s: None)
        self.assertFalse(broken.force_safe()[0][1])
        self.assertFalse(broken.sequence_to_rx()[0][1])

    def test_pa_off_failure_raises_after_all_lines(self):
        rc, be, saf, log = make(pa_enable_line="sim:pa0,sim:pa1")
        p = Permit([0, 1])
        saf.active.add(id(p))
        rc.pa_enable([0, 1], True, p)
        be.fail_write = lambda line, v: str(line) == "sim:pa0" and not v
        del log[:]
        with self.assertRaises(HardwareFault):
            rc.pa_enable(None, False)
        self.assertIn(("gpio", "sim:pa1", False), log)
        self.assertEqual(rc.state["pa"], [True, False])

    def test_pa_on_failure_forces_safe(self):
        rc, be, saf, log = make()
        p = Permit([0, 1])
        saf.active.add(id(p))
        be.fail_write = lambda line, v: str(line) == "sim:pa" and v
        with self.assertRaises(HardwareFault):
            rc.sequence_to_tx(p)
        self.assertEqual(log[-2:], [("gpio", "sim:pa", False), ("gpio", "sim:tr", False)])
        self.assertEqual(rc.state["pa"], [False, False])
        self.assertEqual(rc.state["tr"], "rx")


class TestAtr(unittest.TestCase):
    def test_atr_configuration(self):
        log = []
        be = FakeBackend(log)
        cfg = rf_cfg(pa_enable_line="n321a:FP0:0x3", tr_switch_line="n321a:FP0:0x4:low",
                     lna_bias_line="n321a:FP0:0x8", use_atr=True)
        rc = RF.RfControl(cfg, 2, be, None, StubSafety(), sleep=lambda s: None)
        self.assertTrue(rc.configure_atr())
        self.assertTrue(rc.atr_active)
        self.assertEqual(len(be.atr_calls), 1)
        dev, bank, ctrl, idle, rx, tx, duplex = be.atr_calls[0]
        self.assertEqual((dev, bank, ctrl), ("n321a", "FP0", 0xF))
        self.assertEqual(idle, 0x4 | 0x8)        # PA off, T/R rx (active low -> high), LNA on
        self.assertEqual(rx, idle)
        self.assertEqual(tx, 0x3)                # PA on, T/R tx (low), LNA off
        self.assertEqual(duplex, tx)

    def test_atr_per_bank_and_refusals(self):
        log = []
        be = FakeBackend(log)
        cfg = rf_cfg(pa_enable_line="a:FP0:1,b:FP0:1", tr_switch_line="none", lna_bias_line="none",
                     use_atr=True)
        rc = RF.RfControl(cfg, 2, be, None, StubSafety(), sleep=lambda s: None)
        self.assertTrue(rc.configure_atr())
        self.assertEqual(sorted(c[0] for c in be.atr_calls), ["a", "b"])
        be.atr_result = False
        self.assertFalse(rc.configure_atr())
        self.assertFalse(rc.atr_active)
        sim = RF.RfControl(rf_cfg(use_atr=True), 2, be, None, StubSafety(), sleep=lambda s: None)
        self.assertFalse(sim.configure_atr())    # sim lines have no ATR bank
        off = RF.RfControl(rf_cfg(pa_enable_line="a:FP0:1", use_atr=False), 2, be, None, StubSafety(),
                           sleep=lambda s: None)
        self.assertFalse(off.configure_atr())

    def test_atr_sequencing_and_override(self):
        log = []
        be = FakeBackend(log)
        saf = StubSafety()
        rc = RF.RfControl(rf_cfg(pa_enable_line="a:FP0:1", tr_switch_line="a:FP0:2", lna_bias_line="none",
                                 use_atr=True), 2, be, None, saf, sleep=lambda s: log.append(("sleep", s)))
        self.assertTrue(rc.configure_atr())
        p = Permit([0, 1])
        saf.active.add(id(p))
        del log[:]
        steps = rc.sequence_to_tx(p)
        self.assertEqual([s[0] for s in steps], ["rx_mute", "atr"])
        self.assertEqual(log, [("set_rx_gain", (0.0, 0.0))])     # ATR drives T/R and PA in hardware
        rc.sequence_to_rx()
        self.assertTrue(rc.atr_active)
        rc.force_safe()                                          # software override -> manual mode
        self.assertFalse(rc.atr_active)


class TestInputs(unittest.TestCase):
    def test_interlock_none_and_sim(self):
        self.assertIsNone(RF.InterlockInput(RF.NONE_LINE).closed())
        self.assertFalse(RF.InterlockInput(RF.NONE_LINE).configured())
        be = FakeBackend([])
        il = RF.InterlockInput(RF.parse_line("sim:il"), be)
        be.inputs["sim:il"] = True
        self.assertTrue(il.closed())
        be.inputs["sim:il"] = False
        self.assertFalse(il.closed())
        be.inputs["sim:il"] = HardwareFault("bus")
        self.assertFalse(il.closed())              # read errors read as open
        self.assertFalse(RF.InterlockInput(RF.parse_line("sim:il")).closed())   # no backend
        g = RF.InterlockInput("dev:FP0:1", be)
        be.inputs["dev:FP0:0x1"] = True
        self.assertTrue(g.closed())

    def test_interlock_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "il.state")
            now = [1000.0]
            il = RF.InterlockInput(RF.parse_line("file:" + path), max_age_s=2.0, wallclock=lambda: now[0])
            self.assertFalse(il.closed())          # missing file
            for text, want in (("closed\n", True), ("1", True), ("open", False), ("0", False),
                               ("garbage", False), ("", False)):
                with open(path, "w") as fh:
                    fh.write(text)
                os.utime(path, (now[0], now[0]))
                self.assertEqual(il.closed(), want, text)
            with open(path, "w") as fh:
                json.dump({"closed": True, "unix": 999.5}, fh)
            self.assertTrue(il.closed())
            now[0] = 1005.0
            self.assertFalse(il.closed())          # stale JSON timestamp
            with open(path, "w") as fh:
                fh.write("closed")
            os.utime(path, (1000.0, 1000.0))
            self.assertFalse(il.closed())          # stale mtime
            os.utime(path, (1004.5, 1004.5))
            self.assertTrue(il.closed())
            with open(path, "w") as fh:
                json.dump({"closed": "yes"}, fh)
            self.assertFalse(il.closed())

    def test_power_monitor(self):
        self.assertEqual(RF.PowerMonitor(RF.NONE_LINE).read().age_s, math.inf)
        with self.assertRaises(ConfigError):
            RF.PowerMonitor(RF.parse_line("dev:FP0:1"))
        be = FakeBackend([])
        pm = RF.PowerMonitor(RF.parse_line("sim"), be)
        be.tel = {"fwd_dbm": [10.0, None], "rev_dbm": [-5.0, -6.0], "temp_c": [40.0], "age_s": 0.2}
        t = pm.read()
        self.assertEqual((t.fwd_dbm, t.rev_dbm, t.temp_c, t.age_s), ([10.0, None], [-5.0, -6.0], [40.0], 0.2))
        be.tel = RF.Telemetry([1.0], [2.0], [3.0], 0.0)
        self.assertEqual(pm.read().fwd_dbm, [1.0])
        be.tel = "bad"
        self.assertEqual(pm.read().age_s, math.inf)
        self.assertEqual(RF.PowerMonitor(RF.parse_line("sim")).read().age_s, math.inf)

    def test_power_monitor_file_and_staleness(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pm.json")
            now = [2000.0]
            pm = RF.PowerMonitor(RF.parse_line("file:" + path), wallclock=lambda: now[0])
            self.assertEqual(pm.read().age_s, math.inf)
            with open(path, "w") as fh:
                json.dump({"unix": 1999.5, "fwd_dbm": [20.0, 21.0], "rev_dbm": [0.0, 1.0], "temp_c": [45]}, fh)
            t = pm.read()
            self.assertEqual(t.fwd_dbm, [20.0, 21.0])
            self.assertAlmostEqual(t.age_s, 0.5)
            now[0] = 2010.0
            self.assertAlmostEqual(pm.read().age_s, 10.5)
            with open(path, "w") as fh:
                json.dump({"fwd_dbm": [1.0]}, fh)
            os.utime(path, (2009.0, 2009.0))
            self.assertAlmostEqual(pm.read().age_s, 1.0)
            with open(path, "w") as fh:
                fh.write("{not json")
            self.assertEqual(pm.read().age_s, math.inf)

    def test_status_and_timing_source(self):
        rc, be, saf, log = make()
        st = rc.status()
        self.assertEqual(st["tr_line"], "sim:tr")
        self.assertFalse(st["atr"])
        t0 = time.monotonic()
        rc.force_safe()
        self.assertLess(time.monotonic() - t0, 0.2)


class TestReviewRegressions(unittest.TestCase):
    def test_future_timestamps_are_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            pm_path, il_path = os.path.join(td, "pm.json"), os.path.join(td, "il.json")
            now = [1.7e9]
            with open(pm_path, "w") as fh:     # a daemon writing milliseconds, then dying
                json.dump({"unix": now[0] * 1000, "fwd_dbm": [30.0], "rev_dbm": [0.0], "temp_c": [40.0]}, fh)
            with open(il_path, "w") as fh:
                json.dump({"closed": True, "unix": now[0] + 3600}, fh)
            pm = RF.PowerMonitor(RF.parse_line("file:" + pm_path), wallclock=lambda: now[0])
            il = RF.InterlockInput(RF.parse_line("file:" + il_path), max_age_s=1.0, wallclock=lambda: now[0])
            self.assertEqual(pm.read().age_s, math.inf)
            self.assertIn("future", pm.last_error)
            self.assertFalse(il.closed())
            self.assertIn("future", il.last_error)
            with open(il_path, "w") as fh:     # within the allowed skew: accepted
                json.dump({"closed": True, "unix": now[0] + 0.5 * RF.FUTURE_SKEW_S}, fh)
            self.assertTrue(il.closed())
            with open(pm_path, "w") as fh:
                json.dump({"unix": now[0] + 0.5 * RF.FUTURE_SKEW_S, "fwd_dbm": [1.0]}, fh)
            self.assertEqual(pm.read().age_s, 0.0)

    def test_lna_restore_keeps_the_operator_state(self):
        rc, be, saf, log = make()
        self.assertTrue(rc.lna_restore_value())          # nothing saved: the current state (on)
        rc.lna_bias(False)                               # operator switched the LNA off
        self.assertFalse(rc.lna_restore_value())
        p = Permit([0, 1])
        saf.active.add(id(p))
        rc.sequence_to_tx(p)
        rc.sequence_to_rx()
        self.assertFalse(rc.state["lna"])
        saf.policy.lna_off_during_tx = False             # no LNA switching during TX
        rc.lna_bias(False)
        rc.sequence_to_tx(p)
        rc.sequence_to_rx()
        self.assertFalse(rc.state["lna"])
        rc.lna_bias(True)
        rc.sequence_to_tx(p)
        rc.pa_enable(None, False)
        rc.tr_switch("rx")
        rc.restore_lna()                                 # the safe_shutdown path
        self.assertTrue(rc.state["lna"])
        self.assertIsNone(rc._lna_before_tx)

    def test_atr_failure_rolls_back_programmed_banks(self):
        log = []

        class Be(FakeBackend):
            def gpio_atr(self, device, bank, ctrl, idle, rx, tx, duplex):
                self.atr_calls.append((device, bank, ctrl, idle, rx, tx, duplex))
                if device == "b" and ctrl:
                    return False
                return True
        be = Be(log)
        cfg = rf_cfg(pa_enable_line="a:FP0:1,b:FP0:1", tr_switch_line="a:FP0:2", lna_bias_line="none",
                     use_atr=True)
        rc = RF.RfControl(cfg, 2, be, None, StubSafety(), sleep=lambda s: None)
        self.assertFalse(rc.configure_atr())
        self.assertFalse(rc.atr_active)
        manual = [(c[0], c[1]) for c in be.atr_calls if c[2] == 0]
        self.assertEqual(sorted(manual), [("a", "FP0"), ("b", "FP0")])   # both banks back to manual
        self.assertIn(("gpio", "a:FP0:0x1", False), log)                 # PA lines driven off
        self.assertIn(("gpio", "b:FP0:0x1", False), log)
        self.assertEqual(rc.state["pa"], [False, False])

        class Raising(FakeBackend):
            def gpio_atr(self, device, bank, ctrl, idle, rx, tx, duplex):
                self.atr_calls.append((device, bank, ctrl))
                if device == "b" and ctrl:
                    raise HardwareFault("bank b")
                return True
        be2 = Raising([])
        rc2 = RF.RfControl(cfg, 2, be2, None, StubSafety(), sleep=lambda s: None)
        self.assertFalse(rc2.configure_atr())
        self.assertIn(("a", "FP0", 0), be2.atr_calls)

if __name__ == "__main__":
    unittest.main()
