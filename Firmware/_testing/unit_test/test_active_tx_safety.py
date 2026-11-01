"""
    Unit tests for the active TX safety controller (Firmware/_active/active_tx_safety.py).

    Hardware-free and deterministic: injected monotonic / wall clocks, fake
    sleep, recording fake backend + RF control, temp state directories.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import copy
import dataclasses
import json
import logging
import math
import os
import re
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from os.path import dirname, join, realpath

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_config as C  # noqa: E402
import active_rf_control as RF  # noqa: E402
import active_tx_safety as S  # noqa: E402
from active_common import (AsyncEvent, DevTime, EventLog, InterlockTrip, SafetyRefusal,  # noqa: E402
                           TxResult, sha256_file)

logging.getLogger("antenna_profile").setLevel(logging.ERROR)
logging.getLogger("heimdall.active").setLevel(logging.CRITICAL + 10)

FC = 915e6
FS = 2e6
WALL0 = 1.7e9


class Clock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)


class Loop:
    """Interlock / dummy-load sense double."""

    def __init__(self, closed=True):
        self.state = closed

    def configured(self):
        return True

    def closed(self):
        return self.state


class Tel:
    def __init__(self, n=4, fwd=-30.0, rev=-60.0, temp=30.0, age=0.1):
        self.t = RF.Telemetry([fwd] * n, [rev] * n, [temp] * n, age)

    def read(self):
        return copy.deepcopy(self.t)


class RecBackend:
    is_simulation = True

    def __init__(self, log, fail=()):
        self.log = log
        self.fail = set(fail)

    def _do(self, name):
        self.log.append(name)
        if name in self.fail:
            raise RuntimeError("%s failed" % name)

    def tx_abort(self):
        self._do("tx_abort")

    def set_tx_gain_min(self):
        self._do("set_tx_gain_min")


class RecRf:
    atr_active = False
    safety = None

    def __init__(self, log, fail=()):
        self.log = log
        self.fail = set(fail)

    def _do(self, name):
        self.log.append(name)
        if name.split(":")[0] in self.fail:
            raise RuntimeError("%s failed" % name)

    def attach_safety(self, s):
        self.safety = s

    def pa_enable(self, chans, on, permit=None):
        self._do("pa_enable:%s" % ("on" if on else "off"))

    def tr_switch(self, mode):
        self._do("tr_switch:%s" % mode)

    def lna_bias(self, on):
        self._do("lna_bias:%s" % ("on" if on else "off"))

    def lna_restore_value(self):
        return True

    def restore_rx_gain(self):
        self._do("restore_rx_gain")

    def sequence_to_tx(self, permit):
        self._do("sequence_to_tx")

    def sequence_to_rx(self):
        self._do("sequence_to_rx")
        return []


def merge(base, over):
    out = copy.deepcopy(base)
    for sec, keys in over.items():
        if sec == "devices":
            out["devices"] = copy.deepcopy(keys)
            continue
        out.setdefault(sec, {}).update(keys)
    return out


BASE = {
    "active": {"en_active": 1, "mode": "trx", "operator": "tester"},
    "devices": {"sim0": {"family": "sim", "rx_channels": "0..3", "tx_channels": "0..3"}},
    "geometry": {"preset": "ula", "num_elements": 4},
    "tx": {"en_tx": 1, "allowed_modes": "dummy_load", "allow_bands": "902e6:928e6",
           "dummy_load_rating_w": 1},
}


def cal_csv(td, chans=4):
    path = os.path.join(td, "tx_power_cal.csv")
    with open(path, "w") as fh:
        fh.write("chan,freq_hz,tx_gain_db,p_fs_dbm\n")
        for c in range(chans):
            for f in (900e6, 930e6):
                for g in (0.0, 10.0):
                    fh.write("%d,%g,%g,%g\n" % (c, f, g, -25.0 + g))
    return path


def radiate_over(td):
    cal_csv(td)
    return {"tx": {"allowed_modes": "dummy_load,radiate", "authorization": "TEST-EXP-1",
                   "authorization_kind": "experimental", "mpe_exclusion_m": 5,
                   "passive_isolation_db": 80, "max_reverse_power_dbm": 10},
            "frontend": {"tx_power_cal_file": "tx_power_cal.csv"},
            "rf_control": {"interlock": "sim:il", "power_monitor": "sim"}}


def make_cfg(td, **over):
    return C.default_config(base_dir=td, **merge(BASE, over))


def make_ctl(cfg, td, clock=None, **kw):
    clock = clock or Clock()
    kw.setdefault("ref_locked", lambda: True)
    kw.setdefault("sync_ok", lambda: True)
    kw.setdefault("coherence_class", lambda: 2)
    kw.setdefault("wallclock", lambda: WALL0 + clock.t)
    kw.setdefault("sleep", lambda s: None)
    return S.TxSafetyController(cfg, None, None, EventLog(), clock=clock, state_dir=td, **kw)


def wave(k=4, n=200, amp=0.4):
    t = np.arange(n)
    return (amp * np.exp(2j * np.pi * 0.05 * t))[None, :].repeat(k, axis=0)


def arm(ctl, mode="dummy_load", fc=FC, **kw):
    return ctl.arm(mode, fc, S.ack_phrase(mode, fc), kw.pop("operator", "tester"), **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = self._td.name
        self.clock = Clock()

    def tearDown(self):
        self._td.cleanup()

    def ctl(self, cfg=None, **kw):
        return make_ctl(cfg if cfg is not None else make_cfg(self.td), self.td, self.clock, **kw)

    def armed(self, cfg=None, mode="dummy_load", **kw):
        arm_kw = kw.pop("arm_kw", {})
        c = self.ctl(cfg, **kw)
        arm(c, mode, **arm_kw)
        return c

    def refused(self, code, fn, *a, **kw):
        with self.assertRaises(SafetyRefusal) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        return cm.exception

    def tripped(self, code, fn, *a, **kw):
        with self.assertRaises(InterlockTrip) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, code, str(cm.exception))


# ---------------------------------------------------------------------------
class TestBasics(Base):
    def test_phrases(self):
        self.assertEqual(S.ack_phrase("dummy_load", 915e6), "ARM DUMMY_LOAD 915.000000 MHz")
        self.assertEqual(S.reset_phrase("vswr"), "RESET FAULT VSWR")
        self.assertAlmostEqual(S.vswr_from_powers(0.0, -9.54), 2.0, places=2)
        self.assertEqual(S.vswr_from_powers(0.0, 0.0), math.inf)
        self.assertAlmostEqual(S.gamma_from_vswr(2.0), 1 / 3)

    def test_codes_used_are_listed(self):
        with open(S.__file__, encoding="utf-8") as fh:
            src = fh.read()
        used = set(re.findall(r'_refuse\("([A-Z_]+)"', src)) | set(re.findall(r'SafetyRefusal\("([A-Z_]+)"', src))
        trips = set(re.findall(r'trip\("([A-Z_]+)"', src)) | set(re.findall(r'_trip_raise\("([A-Z_]+)"', src))
        self.assertTrue(used <= set(S.REFUSAL_CODES) | set(S.TRIP_CODES), used - set(S.REFUSAL_CODES))
        self.assertTrue(trips <= set(S.TRIP_CODES), trips - set(S.TRIP_CODES))
        self.assertEqual(len(set(S.REFUSAL_CODES)), len(S.REFUSAL_CODES))
        self.assertIn("SEQUENCE", S.TRIP_CODES)

    def test_default_config_is_disabled(self):
        cfg = C.default_config(base_dir=self.td)
        c = make_ctl(cfg, self.td, self.clock)
        self.assertEqual(c.state, S.TxState.DISABLED)
        self.assertEqual(c.validate_static(), [])
        self.refused("TX_DISABLED", arm, c)
        self.refused("TX_DISABLED", c.refresh)
        self.refused("TX_DISABLED", c.authorize_burst, wave(1), [0], [0], FS)
        self.refused("TX_DISABLED", c.consume, object())
        self.refused("TX_DISABLED", c.reset_fault, "op", "RESET FAULT X")
        self.assertEqual(c.status()["state"], "disabled")
        c.disarm()
        self.assertEqual(c.state, S.TxState.DISABLED)

    def test_mode_rx_is_disabled(self):
        c = self.ctl(make_cfg(self.td, active={"mode": "rx"}))
        self.assertEqual(c.state, S.TxState.DISABLED)

    def test_policy(self):
        p = S.TxPolicy.from_config(make_cfg(self.td))
        self.assertTrue(p.enabled)
        self.assertEqual(p.max_tx_gain_db, (0.0,) * 4)          # "" -> caps minimum
        self.assertEqual(p.dummy_load_pep_w, (1.0,) * 4)        # 0 -> the rating
        self.assertEqual(p.occupied_bw_hz(), FS)
        self.assertIn("max_eirp_dbm", p.to_dict())

    def test_arm_success(self):
        c = self.ctl()
        self.assertEqual(c.state, S.TxState.SAFE)
        self.assertEqual([i for i in c.validate_static() if i.level == "error"], [])
        rec = arm(c)
        self.assertEqual(c.state, S.TxState.ARMED)
        self.assertEqual(rec.mode, "dummy_load")
        self.assertEqual(rec.operator, "tester")
        self.assertAlmostEqual(rec.expires_unix - rec.armed_unix, 60.0)
        on_disk = S.read_arm_record(self.td)
        self.assertEqual(on_disk, rec)
        mode = stat.S_IMODE(os.stat(os.path.join(self.td, S.ARM_RECORD_NAME)).st_mode)
        self.assertEqual(mode, 0o600)
        ev = c.event_log.recent(10, "active_tx_arm")
        self.assertEqual(ev[-1]["payload"]["session"], rec.session_id)
        self.assertEqual([x[0] for x in ev[-1]["payload"]["checks"]][-1], "A15")
        self.assertIn("not legal advice", c.last_banner)
        self.assertEqual(c.status()["state"], "armed")

    def test_arm_record_io(self):
        self.assertIsNone(S.read_arm_record(self.td))
        rec = S.ArmRecord("s", "cabled", "op", 915e6, 2e6, 1.0, 2.0, "f" * 64, max_bursts=3)
        S.write_arm_record(self.td, rec)
        self.assertEqual(S.read_arm_record(self.td), rec)
        with open(os.path.join(self.td, S.ARM_RECORD_NAME), "w") as fh:
            fh.write("{broken")
        self.assertIsNone(S.read_arm_record(self.td))
        with open(os.path.join(self.td, S.ARM_RECORD_NAME), "w") as fh:
            json.dump(dict(rec.to_dict(), mode="space"), fh)
        self.assertIsNone(S.read_arm_record(self.td))
        S.clear_arm_record(self.td)
        S.clear_arm_record(self.td)
        self.assertFalse(os.path.exists(os.path.join(self.td, S.ARM_RECORD_NAME)))

    def test_stale_arm_record_deleted_at_construction(self):
        S.write_arm_record(self.td, S.ArmRecord("old", "dummy_load", "op", FC, FS, 1.0, 9e9, "x"))
        c = self.ctl()
        self.assertFalse(os.path.exists(os.path.join(self.td, S.ARM_RECORD_NAME)))
        self.assertEqual(c.event_log.recent(5, "active_tx_disarm")[-1]["payload"]["reason"], "stale_arm_record")
        self.assertEqual(c.state, S.TxState.SAFE)

    def test_duty_meter(self):
        clk = Clock()
        d = S.DutyCycleMeter(10.0, 0.1, clk)
        for k in range(10):
            self.assertFalse(d.would_exceed(0.1))
            d.record(clk.t, 0.1)
            clk.advance(0.1)
        self.assertAlmostEqual(d.duty(), 0.1)
        self.assertTrue(d.would_exceed(0.1))
        self.assertFalse(d.would_exceed(0.1, now=10.05))
        clk.t = 25.0
        self.assertEqual(d.duty(), 0.0)
        with self.assertRaises(ValueError):
            S.DutyCycleMeter(0, 0.1)


# ---------------------------------------------------------------------------
class TestPreflight(Base):
    def test_a1_states(self):
        c = self.ctl()
        arm(c)
        self.refused("ALREADY_ARMED", arm, c)
        c.trip("EXCEPTION", "test")
        self.refused("FAULT_LATCHED", arm, c)

    def test_a2_mode(self):
        c = self.ctl()
        self.refused("MODE_NOT_ALLOWED", arm, c, "cabled")
        self.refused("MODE_NOT_ALLOWED", c.arm, "space", FC, "ARM SPACE 915.000000 MHz", "tester")
        self.assertEqual(c.state, S.TxState.SAFE)

    def test_a3_confirmation(self):
        c = self.ctl()
        self.refused("CONFIRMATION", c.arm, "dummy_load", FC, "ARM DUMMY_LOAD 915.000001 MHz", "tester")
        self.refused("CONFIRMATION", c.arm, "dummy_load", FC, "arm dummy_load 915.000000 MHz", "tester")
        c2 = self.ctl(make_cfg(self.td, **radiate_over(self.td)), interlock=Loop(), telemetry=Tel(),
                      is_simulation=True)
        e = self.refused("CONFIRMATION_TTY_REQUIRED", c2.arm, "radiate", FC, S.ack_phrase("radiate", FC),
                         "tester", confirmation_source="arg")
        self.assertIn("--confirm", e.detail)
        self.assertEqual(c2.state, S.TxState.SAFE)
        self.assertEqual(c2.event_log.recent(5, "active_tx_refused")[-1]["payload"]["code"],
                         "CONFIRMATION_TTY_REQUIRED")

    def test_a4_operator(self):
        c = self.ctl(make_cfg(self.td, active={"operator": ""}))
        self.refused("NO_OPERATOR", c.arm, "dummy_load", FC, S.ack_phrase("dummy_load", FC), "  ")
        # the config operator is accepted when the argument is empty
        self.assertEqual(arm(self.ctl(), operator="").operator, "tester")

    def test_a5_sim_fallback(self):
        cfg = make_cfg(self.td)
        cfg = dataclasses.replace(cfg, active=dataclasses.replace(cfg.active, backend="uhd"))
        c = self.ctl(cfg, is_simulation=True)
        self.refused("BACKEND", arm, c)

    def test_a6_config_invalid(self):
        cfg = make_cfg(self.td, frontend={"en_frontend": 1, "tx_chain": "pa:p:10::20"})
        c = self.ctl(cfg)
        self.assertIn("TX_LOCK", [i.code for i in c.validate_static() if i.level == "error"])
        e = self.refused("CONFIG_INVALID", arm, c)
        self.assertIn("pa_enable_line", e.detail)
        cfg2 = make_cfg(self.td, tx={"max_tx_gain_db": "40"})
        self.refused("CONFIG_INVALID", arm, self.ctl(cfg2))

    def test_a7_frequency(self):
        c = self.ctl()
        self.refused("FREQ_NOT_ALLOWED", arm, c, fc=930e6)
        self.refused("FREQ_NOT_ALLOWED", arm, c, fc=927.5e6)       # occupied band over the edge
        gps = self.ctl(make_cfg(self.td, tx={"allow_bands": "1500e6:1700e6"}))
        self.refused("FREQ_DENIED", arm, gps, fc=1575.42e6)
        over = self.ctl(make_cfg(self.td, tx={"allow_bands": "1500e6:1700e6",
                                              "conducted_ignore_builtin_deny": 1}))
        self.assertEqual(arm(over, fc=1575.42e6).fc_hz, 1575.42e6)   # conducted override
        tuned = self.ctl(tx_freq=lambda: 916e6)
        self.refused("FREQ_CHANGED", arm, tuned)

    def test_a8_ref_and_coherence(self):
        self.refused("REF_UNLOCK", arm, self.ctl(ref_locked=lambda: False))
        self.refused("REF_UNLOCK", arm, self.ctl(ref_locked=lambda: None))
        self.refused("REF_UNLOCK", arm, self.ctl(ref_locked=lambda: {"sim0": False}))
        self.refused("NO_SYNC", arm, self.ctl(sync_ok=lambda: False))
        self.refused("COHERENCE_CLASS", arm, self.ctl(coherence_class=lambda: 1))
        one = make_cfg(self.td, devices={"sim0": {"family": "sim", "rx_channels": "0..3", "tx_channels": "0"}})
        self.assertEqual(arm(self.ctl(one, sync_ok=lambda: False, coherence_class=lambda: 0)).mode, "dummy_load")
        c = self.ctl(one, sync_ok=lambda: False)
        self.refused("NO_SYNC", c.arm, "dummy_load", FC, S.ack_phrase("dummy_load", FC), "t", beamformed=True)
        nolock = make_cfg(self.td, timing={"require_ref_lock": 0})
        self.assertEqual(arm(self.ctl(nolock, ref_locked=lambda: False)).mode, "dummy_load")

    def test_a9_interlock(self):
        il = make_cfg(self.td, rf_control={"interlock": "sim:il"})
        self.refused("INTERLOCK_OPEN", arm, self.ctl(il))                       # configured, unreadable
        self.refused("INTERLOCK_OPEN", arm, self.ctl(il, interlock=Loop(False)))
        self.assertEqual(arm(self.ctl(il, interlock=Loop(True))).mode, "dummy_load")
        over = radiate_over(self.td)
        over["rf_control"]["interlock"] = "none"
        c = self.ctl(make_cfg(self.td, **over), telemetry=Tel(), is_simulation=True)
        self.refused("INTERLOCK_REQUIRED", c.arm, "radiate", FC, S.ack_phrase("radiate", FC), "t",
                     confirmation_source="tty")

    def test_a10_dummy_load(self):
        dl = make_cfg(self.td, rf_control={"dummy_load_sense": "sim:dl"})
        self.refused("DUMMY_LOAD", arm, self.ctl(dl, dummy_load_sense=Loop(False)))
        self.assertEqual(arm(self.ctl(dl, dummy_load_sense=Loop(True))).mode, "dummy_load")
        self.refused("DUMMY_LOAD", arm, self.ctl(make_cfg(self.td, tx={"dummy_load_rating_w": 0})))

    def test_a11_telemetry(self):
        pm = make_cfg(self.td, rf_control={"power_monitor": "sim"}, tx={"max_reverse_power_dbm": 10})
        self.refused("TELEMETRY_STALE", arm, self.ctl(pm))                       # no reading at all
        self.refused("TELEMETRY_STALE", arm, self.ctl(pm, telemetry=Tel(age=5.0)))
        self.refused("REVERSE_POWER", arm, self.ctl(pm, telemetry=Tel(rev=20.0, fwd=30.0)))
        self.refused("VSWR", arm, self.ctl(pm, telemetry=Tel(fwd=0.0, rev=-5.0)))
        self.refused("OVER_TEMP", arm, self.ctl(pm, telemetry=Tel(temp=80.0)))
        self.assertEqual(arm(self.ctl(pm, telemetry=Tel())).mode, "dummy_load")
        # low forward power: VSWR not evaluated (noise floor)
        self.assertEqual(arm(self.ctl(pm, telemetry=Tel(fwd=-50.0, rev=-52.0))).mode, "dummy_load")
        over = radiate_over(self.td)
        over["rf_control"]["power_monitor"] = "none"
        over["tx"].pop("max_reverse_power_dbm")
        c = self.ctl(make_cfg(self.td, **over), interlock=Loop(), is_simulation=True)
        self.refused("NO_POWER_MONITOR", c.arm, "radiate", FC, S.ack_phrase("radiate", FC), "t",
                     confirmation_source="tty")

    def test_a12_power_and_protection(self):
        self.refused("CHAIN_POWER", arm, self.ctl(make_cfg(self.td, tx={"max_chain_power_dbm": -40})))
        pa = make_cfg(self.td, frontend={"en_frontend": 1, "tx_chain": "amp:drv:20::-10"})
        self.refused("PA_COMPRESSION", arm, self.ctl(pa))
        hot = make_cfg(self.td, tx={"max_tx_gain_db": 20})
        e = self.refused("RX_PROTECTION", arm, self.ctl(hot))
        self.assertIn("isolation/attenuation", e.detail)
        cool = make_cfg(self.td, tx={"max_tx_gain_db": 20},
                        frontend={"tr_isolation_db": 40, "tx_rx_isolation_db": 40},
                        rf_control={"tr_switch_line": "sim:tr"})
        self.assertEqual(arm(self.ctl(cool)).mode, "dummy_load")

    def test_a13_ratings(self):
        tiny = make_cfg(self.td, tx={"dummy_load_rating_w": 1e-6})
        self.refused("DUMMY_LOAD_RATING", arm, self.ctl(tiny))
        cab = make_cfg(self.td, tx={"allowed_modes": "cabled", "cabled_rx_path_loss_db": 1, "max_tx_gain_db": 20})
        self.refused("CABLED_RX_OVERLOAD", arm, self.ctl(cab), "cabled")
        ok = make_cfg(self.td, tx={"allowed_modes": "cabled", "cabled_rx_path_loss_db": 30})
        self.assertEqual(arm(self.ctl(ok), "cabled").mode, "cabled")

    def radiate(self, ctl, **kw):
        return ctl.arm("radiate", FC, S.ack_phrase("radiate", FC), "tester", confirmation_source="tty", **kw)

    def rad_ctl(self, mutate=None, **kw):
        over = radiate_over(self.td)
        if mutate:
            mutate(over)
        kw.setdefault("interlock", Loop())
        kw.setdefault("telemetry", Tel())
        kw.setdefault("is_simulation", True)
        return self.ctl(make_cfg(self.td, **over), **kw)

    def test_a14_radiate(self):
        c = self.rad_ctl()
        self.assertEqual([i for i in c.validate_static() if i.level == "error"], [])
        rec = self.radiate(c)
        self.assertEqual(rec.mode, "radiate")
        self.assertTrue(c.event_log.recent(5, "active_tx_arm")[-1]["payload"]["simulated"])
        self.assertIn("SIMULATED", c.last_banner)
        self.assertIn("EIRP", c.last_banner)
        self.refused("NO_POWER_CAL", self.radiate,
                     self.rad_ctl(lambda o: o["frontend"].update(tx_power_cal_file="")))
        self.refused("NO_AUTHORIZATION", self.radiate,
                     self.rad_ctl(lambda o: o["tx"].update(authorization="")))
        self.refused("EIRP", self.radiate, self.rad_ctl(lambda o: o["tx"].update(max_eirp_dbm=-40)))
        self.refused("MPE", self.radiate, self.rad_ctl(lambda o: o["tx"].update(mpe_exclusion_m=1e-9)))
        self.refused("PASSIVE_CHAIN", self.radiate,
                     self.rad_ctl(lambda o: o["tx"].update(passive_isolation_db=0)))
        self.refused("PASSIVE_CHAIN", self.radiate,
                     self.rad_ctl(lambda o: o["tx"].update(passive_isolation_db=5, passive_rx_damage_dbm=-40)))
        amateur = self.rad_ctl(lambda o: o["tx"].update(authorization_kind="amateur", callsign="KA1ABC"))
        self.radiate(amateur)
        self.assertTrue(amateur.event_log.recent(5, "active_station_id"))

    def test_a15_beamformed_radiate_needs_tx_cal(self):
        self.refused("CAL_REQUIRED", self.radiate, self.rad_ctl(), beamformed=True)
        self.assertEqual(self.radiate(self.rad_ctl(tx_cal_valid=lambda: True), beamformed=True).mode, "radiate")

    def test_refusal_leaves_state_and_disk_unchanged(self):
        c = self.ctl(make_cfg(self.td, tx={"max_chain_power_dbm": -40}))
        self.refused("CHAIN_POWER", arm, c)
        self.assertEqual(c.state, S.TxState.SAFE)
        self.assertIsNone(c.arm_record)
        self.assertFalse(os.path.exists(os.path.join(self.td, S.ARM_RECORD_NAME)))


# ---------------------------------------------------------------------------
class TestBurst(Base):
    def test_happy_path_and_permit(self):
        c = self.armed()
        x = wave()
        x0 = x.copy()
        p = c.authorize_burst(x, [0, 1, 2, 3], [0, 0, 0, 0], FS)
        self.assertTrue(np.array_equal(x, x0))
        self.assertEqual(p.samples.dtype, np.complex64)
        self.assertFalse(p.samples.flags.writeable)
        with self.assertRaises(ValueError):
            p.samples[0, 0] = 1
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.samples = x
        self.assertEqual(p.digest, __import__("hashlib").sha256(p.samples.tobytes()).hexdigest())
        self.assertAlmostEqual(p.duration_s, 200 / FS)
        self.assertEqual(len(p.prediction), 4)
        self.assertTrue(c.permit_active(p))
        self.assertEqual(c.state, S.TxState.ARMED)               # issuing does not change state
        c.consume(p)
        self.assertEqual(c.state, S.TxState.TRANSMITTING)
        self.assertTrue(c.permit_active(p))
        c.burst_complete(p, TxResult(True, 200, acked=True))
        self.assertEqual(c.state, S.TxState.ARMED)
        self.assertFalse(c.permit_active(p))

    def test_b1(self):
        c = self.ctl()
        self.refused("NOT_ARMED", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        arm(c)
        self.clock.advance(61.0)
        c.heartbeat()
        self.refused("ARM_EXPIRED", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        self.assertEqual(c.state, S.TxState.SAFE)
        arm(c)
        self.clock.advance(3.0)
        self.refused("WATCHDOG", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        self.assertEqual(c.state, S.TxState.SAFE)

    def test_b1_config_changed_after_power_cal_edit(self):
        path = cal_csv(self.td)
        c = self.armed(make_cfg(self.td, frontend={"tx_power_cal_file": "tx_power_cal.csv"}))
        c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        with open(path, "a") as fh:
            fh.write("0,915e6,5,-20.5\n")
        self.refused("CONFIG_CHANGED", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        self.assertEqual(c.state, S.TxState.SAFE)

    def test_b2_shape(self):
        c = self.armed()
        self.refused("SHAPE", c.authorize_burst, wave(3), [0, 1, 2, 3], [0] * 4, FS)
        self.refused("SHAPE", c.authorize_burst, wave(1)[0], [0], [0], FS)
        self.refused("SHAPE", c.authorize_burst, wave(1), [4], [0], FS)
        self.refused("SHAPE", c.authorize_burst, wave(2), [1, 1], [0, 0], FS)
        self.refused("SHAPE", c.authorize_burst, np.array([["a", "b"]], dtype=object), [0], [0], FS)
        self.refused("SHAPE", c.authorize_burst, np.zeros((1, 0), complex), [0], [0], FS)
        self.refused("SHAPE", c.authorize_burst, wave(1), [0], [0], 0.0)
        x = wave(1)
        x[0, 5] = np.nan
        self.refused("NONFINITE", c.authorize_burst, x, [0], [0], FS)
        x = wave(1)
        x[0, 7] = np.inf
        self.refused("NONFINITE", c.authorize_burst, x, [0], [0], FS)
        self.assertEqual(c.state, S.TxState.ARMED)

    def test_b3_amplitude_never_clipped(self):
        c = self.armed()
        x = wave(4, amp=0.4)
        x[2, 10] = 0.51
        x0 = x.copy()
        e = self.refused("AMPLITUDE", c.authorize_burst, x, [0, 1, 2, 3], [0] * 4, FS)
        self.assertIn("chain 2", e.detail)
        self.assertTrue(np.array_equal(x, x0))
        # |s| is the complex magnitude: |I|, |Q| <= 0.36 but |s| = 0.509
        y = np.full((1, 100), 0.36 + 0.36j)
        self.refused("AMPLITUDE", c.authorize_burst, y, [0], [0], FS)
        c.authorize_burst(np.full((1, 100), 0.5 + 0j), [0], [0], FS)    # exactly the limit

    def test_b4_gain(self):
        c = self.armed()
        self.refused("GAIN", c.authorize_burst, wave(1), [0], [1.0], FS)
        self.refused("GAIN", c.authorize_burst, wave(1), [0], [-1.0], FS)
        self.refused("GAIN", c.authorize_burst, wave(2), [0, 1], [0.0], FS)
        self.refused("GAIN", c.authorize_burst, wave(1), [0], [float("nan")], FS)

    def test_b5_burst_length(self):
        c = self.armed()
        n = int(0.1 * FS)
        c.authorize_burst(wave(1, n), [0], [0], FS)
        self.refused("BURST_LENGTH", c.authorize_burst, wave(1, n + 1), [0], [0], FS)

    def test_b6_duty(self):
        c = self.armed()
        n = int(0.1 * FS)
        x = wave(1, n)
        for _ in range(10):
            c.heartbeat()
            p = c.authorize_burst(x, [0], [0], FS)
            c.consume(p)
            self.clock.advance(0.1)
            c.burst_complete(p, TxResult(True, n))
        c.heartbeat()
        e = self.refused("DUTY", c.authorize_burst, x, [0], [0], FS)
        self.assertIn("0.1", e.detail)
        self.clock.advance(1.0)
        c.heartbeat()
        self.refused("DUTY", c.authorize_burst, wave(1, n // 2), [0], [0], FS)   # all 10 still in the window
        self.clock.t = 10.1
        c.heartbeat()
        c.authorize_burst(wave(1, n // 2), [0], [0], FS)          # the first burst left the window

    def test_b7_power_checks(self):
        c = self.armed(make_cfg(self.td, tx={"max_total_power_dbm": -25}))
        self.refused("TOTAL_POWER", c.authorize_burst, wave(4, amp=0.5), [0, 1, 2, 3], [0] * 4, FS)
        c.authorize_burst(wave(1, amp=0.5), [0], [0], FS)           # one chain fits
        # defense in depth: the burst checks hold even if the static checks were bypassed
        c.policy = dataclasses.replace(c.policy, max_chain_power_dbm=(-40.0,) * 4)
        self.refused("CHAIN_POWER", c.authorize_burst, wave(1), [0], [0], FS)
        c.policy = dataclasses.replace(c.policy, max_chain_power_dbm=(0.0,) * 4, dummy_load_pep_w=(1e-7,) * 4)
        self.refused("DUMMY_LOAD_RATING", c.authorize_burst, wave(1), [0], [0], FS)   # PEP
        c.policy = dataclasses.replace(c.policy, dummy_load_pep_w=(1.0,) * 4, dummy_load_rating_w=(1e-7,) * 4)
        self.refused("DUMMY_LOAD_RATING", c.authorize_burst, wave(1), [0], [0], FS)   # average x duty
        amp = self.armed(make_cfg(self.td, frontend={"en_frontend": 1, "tx_chain": "amp:drv:20::30",
                                                     "tr_isolation_db": 40, "tx_rx_isolation_db": 40},
                                  rf_control={"tr_switch_line": "sim:tr"}))
        # no RF control attached: nothing can put the T/R switch in tx -> worst case (T/R rx)
        self.refused("RX_PROTECTION", amp.authorize_burst, wave(1), [0], [0], FS)
        amp.attach(RecBackend([]), RecRf([]))
        amp.authorize_burst(wave(1), [0], [0], FS)
        amp.policy = dataclasses.replace(amp.policy, pa_backoff_db=100.0)
        self.refused("PA_COMPRESSION", amp.authorize_burst, wave(1), [0], [0], FS)

    def test_b7_uses_actual_rms(self):
        c = self.armed()
        x = np.zeros((1, 1000), complex)
        x[0, ::10] = 0.5                                            # PAPR 10 dB
        p = c.authorize_burst(x, [0], [0], FS)
        pr = p.prediction[0]
        self.assertAlmostEqual(pr.device_pep_dbm - pr.device_avg_dbm, 10.0, places=6)

    def test_b8_cabled_and_loopback(self):
        cab = make_cfg(self.td, tx={"allowed_modes": "cabled", "cabled_rx_path_loss_db": 30})
        c = self.armed(cab, "cabled")
        c.authorize_burst(wave(1), [0], [0], FS)
        c.policy = dataclasses.replace(c.policy, cabled_rx_path_loss_db=(1.0,) * 4)
        c.frontend.rx_safe_margin_db = 25.0                          # safe level -35 dBm
        self.refused("CABLED_RX_OVERLOAD", c.authorize_burst, wave(1), [0], [0], FS)
        lb = make_cfg(self.td, devices={"sim0": {"family": "sim", "rx_channels": "0..4", "tx_channels": "0..3"}},
                      calibration={"tx_method": "loopback_tdm", "loopback_rx_chan": 4},
                      tx={"loopback_path_loss_db": 40})
        d = self.armed(lb)
        d.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS, purpose="tx_cal")
        d.policy = dataclasses.replace(d.policy, loopback_path_loss_db=(5.0,) * 4)
        d.frontend.rx_safe_margin_db = 25.0
        self.refused("CABLED_RX_OVERLOAD", d.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS, purpose="tx_cal")

    def test_b8_eirp_and_mpe(self):
        over = radiate_over(self.td)
        c = self.ctl(make_cfg(self.td, **over), interlock=Loop(), telemetry=Tel(), is_simulation=True)
        c.arm("radiate", FC, S.ack_phrase("radiate", FC), "t", confirmation_source="tty")
        c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.policy = dataclasses.replace(c.policy, max_eirp_dbm=-30.0)
        self.refused("EIRP", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.policy = dataclasses.replace(c.policy, max_eirp_dbm=10.0, mpe_exclusion_m=1e-9)
        self.refused("MPE", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.policy = dataclasses.replace(c.policy, mpe_exclusion_m=5.0)
        self.refused("CAL_REQUIRED", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS, weights=np.ones(4))

    def test_b9_rx_protection(self):
        c = self.armed()
        c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.frontend.rx_safe_margin_db = 40.0
        self.refused("RX_PROTECTION", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)

    def test_b10_active_reflection(self):
        path = os.path.join(self.td, "s.csv")
        with open(path, "w") as fh:
            fh.write("# freq_hz=915e6\nm,n,re,im\n")
            for a in range(4):
                for b in range(4):
                    fh.write("%d,%d,%g,0\n" % (a, b, 0.05 if a == b else 0.3))
        c = self.armed(make_cfg(self.td, frontend={"s_matrix_file": "s.csv"}))
        c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)                     # no weights: not evaluated
        c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS, weights=[1, 1j, -1, -1j])  # |G| = 0.25
        e = self.refused("ACTIVE_REFLECTION", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS,
                         weights=np.ones(4))                                     # |G| = 0.95
        self.assertIn("VSWR", e.detail)
        self.refused("SHAPE", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS, weights=np.ones(3))

    def test_b11_start_time(self):
        dev = [DevTime(100, 0, 100_000_000)]
        c = self.armed(time_now=lambda: dev[0], is_simulation=False)
        self.refused("START_TIME", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)       # untimed on hw
        self.refused("START_TIME", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS,
                     start=dev[0].add_seconds(0.01))
        self.refused("START_TIME", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS,
                     start=dev[0].add_seconds(59.99995))
        self.refused("START_TIME", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS, start=100.2)
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS, start=dev[0].add_seconds(0.2))
        self.assertGreater(p.deadline_mono, 0.2 + 1.0 - 1e-9)
        sim = self.armed(time_now=lambda: dev[0], is_simulation=True)
        sim.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)                                  # sim: immediate

    def test_b12_frequency(self):
        c = self.armed()
        self.refused("FREQ_CHANGED", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS, fc_hz=916e6)
        c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS, fc_hz=FC + 0.5)
        narrow = self.armed(arm_kw={"occupied_bw_hz": 1e6})
        self.refused("FREQ_CHANGED", narrow.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)  # wider band
        tuned = [FC]
        d = self.armed(tx_freq=lambda: tuned[0])
        tuned[0] = 920e6
        self.refused("FREQ_CHANGED", d.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)


# ---------------------------------------------------------------------------
class TestPermits(Base):
    def issue(self, c=None):
        c = c or self.armed()
        return c, c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)

    def test_single_use(self):
        c, p = self.issue()
        c.consume(p)
        self.tripped("PERMIT_REUSED", c.consume, p)
        self.assertEqual(c.state, S.TxState.FAULT)
        self.assertEqual(c.fault_code, "PERMIT_REUSED")

    def test_reuse_after_completion(self):
        c, p = self.issue()
        c.consume(p)
        c.burst_complete(p, TxResult(True, 200))
        self.tripped("PERMIT_REUSED", c.consume, p)

    def test_forged(self):
        c, p = self.issue()
        fake = S.TxPermit(**{f.name: getattr(p, f.name) for f in dataclasses.fields(p)})
        self.tripped("PERMIT_FORGED", c.consume, fake)
        self.assertEqual(c.state, S.TxState.FAULT)
        c2, _ = self.issue(self.armed())
        self.tripped("PERMIT_FORGED", c2.consume, object())

    def test_digest_tamper(self):
        c, p = self.issue()
        object.__setattr__(p, "digest", "0" * 64)
        self.tripped("DIGEST", c.consume, p)
        c2, p2 = self.issue(self.armed())
        other = np.zeros_like(p2.samples)
        object.__setattr__(p2, "samples", other)
        self.tripped("DIGEST", c2.consume, p2)
        c3, p3 = self.issue(self.armed())
        p3.samples.flags.writeable = True
        p3.samples[0, 0] = 0.49
        self.tripped("DIGEST", c3.consume, p3)

    def test_other_session(self):
        c, p = self.issue()
        c.disarm()
        arm(c)
        self.refused("NOT_ARMED", c.consume, p)                       # revoked at the disarm
        self.assertEqual(c.state, S.TxState.ARMED)

    def test_invalid_after_safe_shutdown(self):
        c, p = self.issue()
        steps = c.safe_shutdown("test")
        self.assertTrue(all(ok for _, ok, _ in steps))
        self.assertEqual(c.state, S.TxState.SAFE)
        self.assertFalse(c.permit_active(p))
        self.refused("NOT_ARMED", c.consume, p)
        self.assertEqual(c.state, S.TxState.SAFE)
        self.assertFalse(os.path.exists(os.path.join(self.td, S.ARM_RECORD_NAME)))

    def test_permit_expired(self):
        c, p = self.issue()
        self.clock.advance(1.5)
        c.heartbeat()
        self.refused("PERMIT_EXPIRED", c.consume, p)
        self.assertEqual(c.state, S.TxState.ARMED)

    def test_one_burst_in_flight(self):
        c, p = self.issue()
        q = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.consume(p)
        self.refused("SEQUENCE", c.consume, q)
        c.burst_complete(p, TxResult(True, 200))
        c.consume(q)

    def test_hold_session(self):
        c = self.armed(make_cfg(self.td, rf_control={"pa_hold_between_bursts": 1}))
        self.assertTrue(c.permit_active(c.arm_record))
        d = self.armed()
        self.assertFalse(d.permit_active(d.arm_record))


# ---------------------------------------------------------------------------
class TestTrips(Base):
    def test_expiry_is_safe_not_fault(self):
        c = self.armed()
        self.clock.advance(61.0)
        c.heartbeat()
        c.poll()
        self.assertEqual(c.state, S.TxState.SAFE)
        self.assertTrue(c.event_log.recent(5, "active_tx_expire"))
        self.assertFalse(os.path.exists(os.path.join(self.td, S.ARM_RECORD_NAME)))

    def test_heartbeat(self):
        c = self.armed(make_cfg(self.td, tx={"heartbeat_timeout_s": 0.5}))
        self.clock.advance(0.6)
        c.poll()
        self.assertEqual(c.state, S.TxState.SAFE)                    # ARMED -> disarm
        arm(c)
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.consume(p)
        self.clock.advance(0.3)
        c.poll()
        self.assertEqual(c.state, S.TxState.TRANSMITTING)
        self.clock.advance(0.3)                                      # before the permit deadline
        c.poll()
        self.assertEqual((c.state, c.fault_code), (S.TxState.FAULT, "HEARTBEAT"))

    def test_tx_hang(self):
        c = self.armed()
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.consume(p)
        self.clock.advance(1.2)
        c.heartbeat()
        c.poll()
        self.assertEqual(c.fault_code, "TX_HANG")
        c.burst_complete(p, TxResult(True, 200))                      # late completion is ignored
        self.assertEqual(c.state, S.TxState.FAULT)

    def test_underflow_storm(self):
        c = self.armed()
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.consume(p)
        c.burst_complete(p, TxResult(True, 200, underflows=2, late=1))
        self.assertEqual(c.state, S.TxState.ARMED)
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.consume(p)
        c.burst_complete(p, TxResult(False, 100, underflows=2))
        self.assertEqual((c.state, c.fault_code), (S.TxState.FAULT, "UNDERFLOW_STORM"))

    def test_underflows_outside_window_do_not_trip(self):
        c = self.armed()
        for _ in range(4):
            p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
            c.consume(p)
            c.burst_complete(p, TxResult(True, 200, underflows=2))
            self.clock.advance(1.1)
            c.heartbeat()
        self.assertEqual(c.state, S.TxState.ARMED)

    def test_async_unlock_events(self):
        c = self.armed()
        c.handle_async_events([AsyncEvent("lo_unlock", 2, detail="tx lo")])
        self.assertEqual(c.fault_code, "LO_UNLOCK")
        d = self.armed()
        d.handle_async_events([AsyncEvent("ref_unlock")])
        self.assertEqual(d.fault_code, "REF_UNLOCK")
        e = self.ctl()
        e.handle_async_events([AsyncEvent("ref_unlock")])            # not armed: no trip
        self.assertEqual(e.state, S.TxState.SAFE)

    def test_ref_unlock_via_poll(self):
        lock = [True]
        c = self.armed(ref_locked=lambda: lock[0])
        lock[0] = False
        c.poll()
        self.assertEqual(c.fault_code, "REF_UNLOCK")

    def test_telemetry_and_interlock_trips(self):
        pm = dict(rf_control={"power_monitor": "sim", "interlock": "sim:il", "dummy_load_sense": "sim:dl"},
                  tx={"max_reverse_power_dbm": 10})
        cases = [("REVERSE_POWER", lambda t, i, d: setattr(t.t, "rev_dbm", [20.0] * 4)),
                 ("VSWR", lambda t, i, d: (setattr(t.t, "fwd_dbm", [5.0] * 4), setattr(t.t, "rev_dbm", [0.0] * 4))),
                 ("OVER_TEMP", lambda t, i, d: setattr(t.t, "temp_c", [71.0] * 4)),
                 ("TELEMETRY_STALE", lambda t, i, d: setattr(t.t, "age_s", 3.0)),
                 ("INTERLOCK_OPEN", lambda t, i, d: setattr(i, "state", False)),
                 ("DUMMY_LOAD_LOST", lambda t, i, d: setattr(d, "state", False))]
        for code, fault in cases:
            tel, il, dl = Tel(), Loop(), Loop()
            c = self.armed(make_cfg(self.td, **pm), telemetry=tel, interlock=il, dummy_load_sense=dl)
            c.poll()
            self.assertEqual(c.state, S.TxState.ARMED, code)
            fault(tel, il, dl)
            c.poll()
            self.assertEqual((c.state, c.fault_code), (S.TxState.FAULT, code), code)
            ev = c.event_log.recent(5, "active_tx_trip")[-1]
            self.assertEqual(ev["payload"]["code"], code)
            self.assertEqual(ev["severity"], "critical")

    def test_trip_during_consume(self):
        il = Loop()
        c = self.armed(make_cfg(self.td, rf_control={"interlock": "sim:il"}), interlock=il)
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        il.state = False
        self.tripped("INTERLOCK_OPEN", c.consume, p)

    def test_fault_latched_and_reset(self):
        il = Loop()
        c = self.armed(make_cfg(self.td, rf_control={"interlock": "sim:il"}), interlock=il)
        il.state = False
        c.poll()
        self.assertEqual(c.state, S.TxState.FAULT)
        self.refused("FAULT_LATCHED", arm, c)
        self.refused("FAULT_LATCHED", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.trip("VSWR", "second")
        self.assertEqual(c.fault_code, "INTERLOCK_OPEN")             # the first cause stays latched
        self.refused("CONFIRMATION", c.reset_fault, "op", "RESET FAULT VSWR")
        self.refused("NO_OPERATOR", c.reset_fault, "", S.reset_phrase("INTERLOCK_OPEN"))
        self.refused("INTERLOCK_OPEN", c.reset_fault, "op", S.reset_phrase("INTERLOCK_OPEN"))
        il.state = True
        c.reset_fault("op", S.reset_phrase("INTERLOCK_OPEN"))
        self.assertEqual(c.state, S.TxState.SAFE)                    # SAFE, never ARMED
        self.assertIsNone(c.arm_record)
        self.assertTrue(c.event_log.recent(5, "active_tx_fault_reset"))
        self.refused("SEQUENCE", c.reset_fault, "op", "RESET FAULT X")
        arm(c)
        self.assertEqual(c.state, S.TxState.ARMED)

    def test_station_id_reminder(self):
        c = self.armed(make_cfg(self.td, tx={"authorization_kind": "amateur", "callsign": "KA1ABC",
                                             "station_id_interval_s": 10}))
        n0 = len(c.event_log.recent(50, "active_station_id"))
        for _ in range(11):
            self.clock.advance(1.0)
            c.heartbeat()
            c.poll()
        self.assertEqual(len(c.event_log.recent(50, "active_station_id")), n0 + 1)

    def test_watchdog_thread(self):
        c = self.armed()
        c.start_watchdog(0.005)
        try:
            self.clock.advance(61.0)
            c.heartbeat()
            t0 = time.monotonic()
            while c.state != S.TxState.SAFE and time.monotonic() - t0 < 0.2:
                time.sleep(0.005)
            self.assertEqual(c.state, S.TxState.SAFE)
        finally:
            c.stop_watchdog()

    def test_config_change_disarms_in_poll(self):
        path = cal_csv(self.td)
        c = self.armed(make_cfg(self.td, frontend={"tx_power_cal_file": "tx_power_cal.csv"}))
        with open(path, "a") as fh:
            fh.write("1,915e6,5,-20\n")
        self.clock.advance(1.0)
        c.heartbeat()
        c.poll()
        self.assertEqual(c.state, S.TxState.SAFE)


# ---------------------------------------------------------------------------
class TestShutdown(Base):
    EXPECT = ["tx_abort", "set_tx_gain_min", "pa_enable:off", ("sleep", 100e-6), "tr_switch:rx",
              ("sleep", 50e-6), "lna_bias:on", "restore_rx_gain"]

    def rig(self, be_fail=(), rf_fail=()):
        log = []
        c = self.ctl(sleep=lambda s: log.append(("sleep", s)))
        be, rf = RecBackend(log, be_fail), RecRf(log, rf_fail)
        c.attach(be, rf)
        self.assertIs(rf.safety, c)
        return c, log

    def test_exact_order_and_idempotent(self):
        c, log = self.rig()
        arm(c)
        del log[:]
        steps = c.safe_shutdown("test")
        self.assertEqual(log, self.EXPECT)
        self.assertEqual([s[0] for s in steps][:11],
                         ["mark_state", "tx_abort", "tx_gain_min", "pa_off", "pa_settle", "tr_rx", "tr_guard",
                          "lna_restore", "rx_gain_restore", "clear_arm_record", "event"])
        self.assertTrue(all(ok for _, ok, _ in steps))
        del log[:]
        steps2 = c.safe_shutdown("again")
        self.assertEqual(log, self.EXPECT)
        self.assertTrue(all(ok for _, ok, _ in steps2))
        self.assertEqual(c.state, S.TxState.SAFE)
        ev = c.event_log.recent(5, "active_shutdown")[-1]
        self.assertEqual(ev["payload"]["reason"], "again")

    def test_continues_when_a_step_raises(self):
        c, log = self.rig(be_fail=("tx_abort",), rf_fail=("tr_switch",))
        arm(c)
        del log[:]
        steps = c.safe_shutdown("test")
        self.assertEqual(log, self.EXPECT)
        d = {s[0]: s[1] for s in steps}
        self.assertFalse(d["tx_abort"])
        self.assertFalse(d["tr_rx"])
        self.assertTrue(d["tx_gain_min"] and d["pa_off"] and d["rx_gain_restore"] and d["clear_arm_record"])

    def test_trip_runs_shutdown_and_listeners(self):
        c, log = self.rig()
        seen = []
        c.add_status_listener(lambda st: seen.append(st["state"]))
        arm(c)
        del log[:]
        c.trip("OVER_TEMP", "80 C")
        self.assertEqual(log, self.EXPECT)
        self.assertEqual(seen, ["fault"])
        self.assertEqual(c.status()["fault_code"], "OVER_TEMP")
        self.assertEqual(c.status()["reset_phrase"], "RESET FAULT OVER_TEMP")

    def test_burst_complete_sequences_to_rx(self):
        c, log = self.rig()
        arm(c)
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.consume(p)
        c.burst_complete(p, TxResult(True, 200))
        self.assertEqual(log[-1], "sequence_to_rx")

    def test_pa_hold_sequences_at_arm(self):
        log = []
        c = self.ctl(make_cfg(self.td, rf_control={"pa_hold_between_bursts": 1}))
        c.attach(RecBackend(log), RecRf(log))
        arm(c)
        self.assertEqual(log, ["sequence_to_tx"])
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.consume(p)
        c.burst_complete(p, TxResult(True, 200))
        self.assertNotIn("sequence_to_rx", log)

    def test_session_context(self):
        c, log = self.rig()
        with c.session("dummy_load", FC, S.ack_phrase("dummy_load", FC), "op") as rec:
            self.assertEqual(c.state, S.TxState.ARMED)
            self.assertTrue(os.path.exists(os.path.join(self.td, S.ARM_RECORD_NAME)))
            del log[:]
        self.assertEqual(c.state, S.TxState.SAFE)
        self.assertEqual(log, self.EXPECT)
        self.assertFalse(os.path.exists(os.path.join(self.td, S.ARM_RECORD_NAME)))
        with self.assertRaises(RuntimeError):
            with c.session("dummy_load", FC, S.ack_phrase("dummy_load", FC), "op"):
                raise RuntimeError("boom")
        self.assertEqual(c.state, S.TxState.SAFE)
        self.assertTrue(rec.session_id)

    def test_refresh(self):
        c = self.armed(make_cfg(self.td, tx={"arm_timeout_s": 60, "max_arm_total_s": 100}))
        self.clock.advance(50)
        c.heartbeat()
        rec = c.refresh()
        self.assertAlmostEqual(rec.expires_unix - (WALL0 + 50), 50.0)      # capped at 100 s total
        self.assertEqual(S.read_arm_record(self.td), rec)
        self.clock.advance(40)
        c.heartbeat()
        self.refused("ARM_TOTAL", c.refresh)
        self.clock.advance(11)
        c.heartbeat()
        self.refused("ARM_EXPIRED", c.refresh)
        self.assertEqual(c.state, S.TxState.SAFE)
        self.refused("NOT_ARMED", c.refresh)

    def test_exit_while_armed(self):
        c, log = self.rig()
        arm(c)
        c._on_exit()
        self.assertEqual(c.fault_code, "EXIT_WHILE_ARMED")

    def test_sigterm_chain(self):
        called = []
        original = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda s, f: called.append(s))
        c, log = self.rig()
        try:
            c.install_process_hooks()
            arm(c)
            del log[:]
            os.kill(os.getpid(), signal.SIGTERM)
            t0 = time.monotonic()
            while not called and time.monotonic() - t0 < 0.2:
                time.sleep(0.001)
            self.assertEqual(called, [signal.SIGTERM])
            self.assertEqual((c.state, c.fault_code), (S.TxState.FAULT, "SIGNAL"))
            self.assertEqual(log, self.EXPECT)
            # unarmed: the handler still runs safe_shutdown, then chains
            c.reset_fault("op", S.reset_phrase("SIGNAL"))
            del log[:]
            os.kill(os.getpid(), signal.SIGTERM)
            t0 = time.monotonic()
            while len(called) < 2 and time.monotonic() - t0 < 0.2:
                time.sleep(0.001)
            self.assertEqual(len(called), 2)
            self.assertEqual(c.state, S.TxState.SAFE)
            self.assertEqual(log, self.EXPECT)
        finally:
            c.uninstall_process_hooks()
            signal.signal(signal.SIGTERM, original)
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_hooks_only_on_main_thread(self):
        c = self.ctl()
        th = threading.Thread(target=c.install_process_hooks)
        th.start()
        th.join()
        self.assertIsNone(c._hooks)


# ---------------------------------------------------------------------------
class TestRfIntegration(Base):
    def test_pa_gating_with_real_controller(self):
        log = []

        class Be(RecBackend):
            def gpio_write(self, line, value):
                log.append(("gpio", str(line), bool(value)))

            def rx_gain_db(self):
                return [30.0] * 4

            def set_rx_gain(self, g):
                log.append(("gain", tuple(g)))

            def rx_refs(self):
                return []

        cfg = make_cfg(self.td, rf_control={"pa_enable_line": "sim:pa", "tr_switch_line": "sim:tr"},
                       frontend={"tr_isolation_db": 30})
        c = self.ctl(cfg)
        be = Be(log)
        rf = RF.RfControl(cfg.rf_control, 4, be, None, None, sleep=lambda s: None)
        c.attach(be, rf)
        arm(c)
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        rf.sequence_to_tx(p)                         # allowed before consume (issued permit)
        self.assertEqual(rf.state["pa"], [True] * 4)
        c.consume(p)
        c.burst_complete(p, TxResult(True, 200))     # -> sequence_to_rx
        self.assertEqual(rf.state["pa"], [False] * 4)
        self.assertEqual(rf.state["tr"], "rx")
        with self.assertRaises(InterlockTrip):
            rf.pa_enable([0], True, p)               # completed permit
        self.assertEqual((c.state, c.fault_code), (S.TxState.FAULT, "SEQUENCE"))
        self.assertEqual(rf.state["pa"], [False] * 4)


# ---------------------------------------------------------------------------
class GpioBe:
    """Sim GPIO backend for a real RfControl (records the last level per line)."""
    is_simulation = True

    def __init__(self):
        self.lines = {}

    def gpio_write(self, line, v):
        self.lines[str(line)] = bool(v)

    def gpio_read(self, line):
        return True

    def tx_abort(self):
        pass

    def set_tx_gain_min(self):
        pass

    def rx_gain_db(self):
        return [30.0] * 4

    def set_rx_gain(self, g):
        pass

    def rx_refs(self):
        return []


LNA_FE = {"frontend": {"en_frontend": 1, "rx_chain": "lna:lna1:30:1:15::-20", "lna_off_isolation_db": 40,
                       "tx_rx_isolation_db": 20},
          "rf_control": {"lna_bias_line": "sim:lna", "use_atr": 0},
          "tx": {"lna_off_during_tx": 1, "rx_safe_margin_db": 0, "max_tx_gain_db": "10,10,10,10",
                 "max_digital_amplitude": 0.25}}


class TestReviewRegressions(Base):
    """Safety review findings (duty, PA idle, retune, fs, occupied band, B9 state,
    adopted record, telemetry completeness, completion bookkeeping, locking)."""

    def rf_rig(self, cfg, **kw):
        c = self.ctl(cfg, **kw)
        be = GpioBe()
        rf = RF.RfControl(cfg.rf_control, 4, be, EventLog(), sleep=lambda s: None)
        c.attach(be, rf)
        return c, be, rf

    # --- B6 duty -------------------------------------------------------------------
    def test_duty_counts_outstanding_permits(self):
        c = self.armed()
        n = int(0.1 * FS)
        ps = [c.authorize_burst(wave(1, n), [0], [0], FS) for _ in range(10)]   # 1.0 s budget reserved
        e = self.refused("DUTY", c.authorize_burst, wave(1, n), [0], [0], FS)
        self.assertIn("outstanding", e.detail)
        for p in ps:                                     # sent one after another: never above budget
            c.heartbeat()
            c.consume(p)
            self.clock.advance(0.1)
            c.burst_complete(p, TxResult(True, n))
        self.assertLessEqual(c.duty.duty(), 0.1 + 1e-9)
        # an expired, never-sent permit releases its reservation
        d = self.armed()
        q = d.authorize_burst(wave(1, n), [0], [0], FS)
        self.assertEqual(d.duty.reserved(), 1)
        self.clock.advance(1.5)
        d.heartbeat()
        d.poll()
        self.assertEqual(d.duty.reserved(), 0)
        self.refused("PERMIT_EXPIRED", d.consume, q)

    def test_duty_timed_bursts_counted_at_their_start(self):
        cfg = make_cfg(self.td, tx={"arm_timeout_s": 600, "max_arm_total_s": 900, "heartbeat_timeout_s": 30})
        c = self.armed(cfg, time_now=lambda: DevTime.from_seconds(self.clock.t, 100_000_000))
        n = int(0.1 * FS)
        ok = 0
        for k in range(20):                              # authorized 1 s apart, all start in [30, 32)
            st = DevTime.from_seconds(30.0 + 0.1 * k, 100_000_000)
            try:
                p = c.authorize_burst(wave(1, n), [0], [0], FS, start=st)
            except SafetyRefusal as e:
                self.assertEqual(e.code, "DUTY")
                break
            c.consume(p)
            c.burst_complete(p, TxResult(True, n))
            ok += 1
            self.clock.advance(1.0)
            c.heartbeat()
        self.assertEqual(ok, 10)                         # 1.0 s in one 10 s window, not 2.0 s

    def test_duty_rechecked_at_consume(self):
        c = self.armed()
        n = int(0.1 * FS)
        p = c.authorize_burst(wave(1, n), [0], [0], FS)
        c.duty.record(self.clock.t, 0.95)                # e.g. another path committed duty meanwhile
        self.refused("DUTY", c.consume, p)
        self.assertEqual(c.state, S.TxState.ARMED)
        self.refused("NOT_ARMED", c.consume, p)          # a refused consume revokes the permit
        self.assertEqual(c.duty.reserved(), 0)

    def test_duty_meter_reservations(self):
        clk = Clock()
        d = S.DutyCycleMeter(10.0, 0.1, clk)
        d.reserve("a", 5.0, 0.6)
        self.assertTrue(d.would_exceed(0.5, now=0.0))   # window [-..] covering 0..5.6 holds 1.1 s
        self.assertFalse(d.would_exceed(0.4, now=0.0))
        self.assertFalse(d.would_exceed(0.5, now=0.0, exclude="a"))
        d.release("a")
        self.assertFalse(d.would_exceed(0.9, now=0.0))
        d.record(20.0, 0.6)                              # a future burst limits an earlier one
        self.assertTrue(d.would_exceed(0.5, now=11.0))

    # --- PA idle ----------------------------------------------------------------------
    def test_pa_back_to_rx_when_consume_refused(self):
        cfg = make_cfg(self.td, rf_control={"pa_enable_line": "sim:pa", "tr_switch_line": "sim:tr",
                                            "lna_bias_line": "sim:lna", "use_atr": 0},
                       frontend={"tr_isolation_db": 30})
        c, be, rf = self.rf_rig(cfg)
        arm(c)
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        rf.sequence_to_tx(p)
        self.assertEqual(rf.state["pa"], [True] * 4)
        self.clock.advance(1.5)
        c.heartbeat()
        self.refused("PERMIT_EXPIRED", c.consume, p)
        self.assertEqual((rf.state["pa"], rf.state["tr"]), ([False] * 4, "rx"))
        self.assertFalse(be.lines["sim:pa"])
        self.assertEqual(c.state, S.TxState.ARMED)

    def test_pa_back_to_rx_when_permit_abandoned(self):
        cfg = make_cfg(self.td, rf_control={"pa_enable_line": "sim:pa", "use_atr": 0})
        c, be, rf = self.rf_rig(cfg)
        arm(c)
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        rf.sequence_to_tx(p)
        self.clock.advance(0.5)
        c.heartbeat()
        c.poll()
        self.assertEqual(rf.state["pa"], [True] * 4)     # live permit: pre-sequenced PA allowed
        self.clock.advance(0.6)                          # past the deadline, never consumed
        c.heartbeat()
        c.poll()
        self.assertEqual(rf.state["pa"], [False] * 4)
        self.assertFalse(c.permit_active(p))
        # an unconsumed permit "completed" is revoked and the PA goes off as well
        q = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        rf.sequence_to_tx(q)
        c.burst_complete(q, TxResult(True, 200))
        self.assertEqual(rf.state["pa"], [False] * 4)
        self.refused("NOT_ARMED", c.consume, q)

    # --- retune / fc authority ---------------------------------------------------------
    def test_tuned_frequency_is_authoritative(self):
        tuned = [FC]
        c = self.armed(tx_freq=lambda: tuned[0])
        tuned[0] = 121.5e6
        self.refused("FREQ_CHANGED", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        self.refused("FREQ_CHANGED", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS, fc_hz=FC)
        tuned[0] = FC
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS, fc_hz=FC)
        tuned[0] = 121.5e6                               # retune between authorize and consume
        self.refused("FREQ_CHANGED", c.consume, p)
        self.assertEqual(c.state, S.TxState.SAFE)        # retune -> SAFE (spec 12.2)
        tuned[0] = FC
        arm(c)
        tuned[0] = FC + 10.0
        c.poll()
        self.assertEqual(c.state, S.TxState.SAFE)
        tuned[0] = FC
        arm(c)
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        c.consume(p)
        tuned[0] = 121.5e6
        c.poll()
        self.assertEqual((c.state, c.fault_code), (S.TxState.FAULT, "FREQ_CHANGED"))
        fail = [False]

        def flaky():
            if fail[0]:
                raise RuntimeError("tuner gone")
            return FC
        d = self.armed(tx_freq=flaky)
        fail[0] = True
        self.refused("FREQ_CHANGED", d.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        d.poll()
        self.assertEqual(d.state, S.TxState.SAFE)        # an unreadable tuner disarms

    def test_burst_rate_must_match_device_rate(self):
        c = self.armed(make_cfg(self.td, tx={"max_burst_s": 0.01}))
        e = self.refused("SHAPE", c.authorize_burst, wave(4, 200000), [0, 1, 2, 3], [0] * 4, 2e9)
        self.assertIn("sample rate", e.detail)
        self.refused("SHAPE", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS * 1.001)
        c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS * (1 + 1e-9))
        d = self.armed(tx_rate=lambda: 1.92e6)
        self.refused("SHAPE", d.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS)
        self.assertAlmostEqual(d.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, 1.92e6).duration_s, 200 / 1.92e6)

    def test_occupied_band_verified_on_the_samples(self):
        c = self.armed(make_cfg(self.td, tx={"occupied_bw_hz": 100e3}))
        n = 2000
        t = np.arange(n)
        out = (0.4 * np.exp(2j * np.pi * (0.5e6 / FS) * t))[None, :]
        e = self.refused("FREQ_CHANGED", c.authorize_burst, out, [0], [0], FS)
        self.assertIn("spectral peak", e.detail)
        inb = (0.4 * np.exp(2j * np.pi * (10e3 / FS) * t))[None, :]
        c.authorize_burst(inb, [0], [0], FS)
        rng = np.random.default_rng(1)
        noise = rng.standard_normal((1, n)) + 1j * rng.standard_normal((1, n))
        noise *= 0.4 / np.max(np.abs(noise))
        spread = noise * 0.5 + 0.5 * inb                 # peak in band, most energy outside
        e = self.refused("FREQ_CHANGED", c.authorize_burst, spread * 0.8, [0], [0], FS)
        self.assertIn("energy", e.detail)
        c.authorize_burst(np.zeros((1, n)), [0], [0], FS)  # silence occupies nothing
        self.armed().authorize_burst(out, [0], [0], FS)    # occupied 0 = the full sample rate: no check

    # --- B9 actual state -----------------------------------------------------------------
    def test_b9_front_end_state_verified_at_consume(self):
        c, be, rf = self.rf_rig(make_cfg(self.td, **LNA_FE))
        arm(c)
        p = c.authorize_burst(wave(4, 200, 0.25), [0, 1, 2, 3], [10] * 4, FS)
        e = self.refused("SEQUENCE", c.consume, p)       # LNA still biased on
        self.assertIn("LNA", e.detail)
        self.assertEqual(c.state, S.TxState.ARMED)
        q = c.authorize_burst(wave(4, 200, 0.25), [0, 1, 2, 3], [10] * 4, FS)
        rf.sequence_to_tx(q)
        self.assertFalse(rf.state["lna"])
        c.consume(q)
        c.burst_complete(q, TxResult(True, 200))
        self.assertTrue(rf.state["lna"])                 # restored after the burst
        over = merge(LNA_FE, {"tx": {"lna_off_during_tx": 0}})
        self.refused("RX_PROTECTION", arm, self.ctl(make_cfg(self.td, **over)))   # no LNA-off credit
        # no RF control attached: nothing can switch the LNA off -> evaluated biased on
        g = self.armed(make_cfg(self.td, **LNA_FE))
        self.refused("RX_PROTECTION", g.authorize_burst, wave(4, 200, 0.25), [0, 1, 2, 3], [10] * 4, FS)
        # T/R switch fitted: the burst needs T/R in tx
        cfg = make_cfg(self.td, rf_control={"tr_switch_line": "sim:tr"}, frontend={"tr_isolation_db": 30})
        f, be2, rf2 = self.rf_rig(cfg)
        arm(f)
        r = f.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS)
        e = self.refused("SEQUENCE", f.consume, r)
        self.assertIn("T/R", e.detail)

    # --- telemetry completeness ------------------------------------------------------------
    def test_telemetry_must_cover_every_chain(self):
        pm = make_cfg(self.td, rf_control={"power_monitor": "sim"}, tx={"max_reverse_power_dbm": 10})
        for tel in (RF.Telemetry([], [], [], 0.0), RF.Telemetry([0.0], [-20.0], [30.0], 0.0),
                    RF.Telemetry([None] * 4, [None] * 4, [None] * 4, 0.0),
                    RF.Telemetry([-30.0] * 4, [-60.0] * 4, [], 0.0)):
            t = Tel()
            t.t = tel
            self.refused("TELEMETRY_STALE", arm, self.ctl(pm, telemetry=t))
        t = Tel()
        c = self.armed(pm, telemetry=t)
        t.t.rev_dbm[2] = None                            # one chain drops out of the reading
        c.poll()
        self.assertEqual((c.state, c.fault_code), (S.TxState.FAULT, "TELEMETRY_STALE"))

    # --- completion bookkeeping ----------------------------------------------------------------
    def test_burst_complete_of_another_permit_keeps_the_burst_in_flight(self):
        c = self.armed()
        a, b, d = (c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS) for _ in range(3))
        c.consume(a)
        c.burst_complete(b, TxResult(True, 200))         # b never sent
        self.assertEqual(c.state, S.TxState.TRANSMITTING)
        self.assertIs(c._inflight.permit, a)
        self.refused("SEQUENCE", c.consume, d)
        self.refused("NOT_ARMED", c.consume, b)          # revoked
        self.clock.advance(1.5)
        c.heartbeat()
        c.poll()
        self.assertEqual(c.fault_code, "TX_HANG")        # a's deadline is still tracked

    def test_timeout_is_bounded(self):
        c = self.armed()
        p = c.authorize_burst(wave(), [0, 1, 2, 3], [0] * 4, FS, timeout_s=1e6)
        self.assertLessEqual(p.deadline_mono - p.issued_mono, max(1.0, 2 * p.duration_s + 2.0) + 1e-9)
        for bad in (float("nan"), float("inf"), -1.0, "x"):
            self.refused("SHAPE", c.authorize_burst, wave(), [0, 1, 2, 3], [0] * 4, FS, timeout_s=bad)

    # --- locking --------------------------------------------------------------------------------
    def test_trip_proceeds_when_the_lock_is_held(self):
        c = self.armed()
        held, release = threading.Event(), threading.Event()

        def hog():
            with c._lock:
                held.set()
                release.wait(5.0)
        th = threading.Thread(target=hog)
        th.start()
        held.wait(1.0)
        old = S.TRIP_LOCK_TIMEOUT_S
        S.TRIP_LOCK_TIMEOUT_S = 0.05
        try:
            t0 = time.monotonic()
            c.trip("SIGNAL", "SIGTERM")
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual((c.state, c.fault_code), (S.TxState.FAULT, "SIGNAL"))
            self.assertFalse(os.path.exists(os.path.join(self.td, S.ARM_RECORD_NAME)))
        finally:
            S.TRIP_LOCK_TIMEOUT_S = old
            release.set()
            th.join()

    def test_poll_reads_sensors_outside_the_lock(self):
        entered, release = threading.Event(), threading.Event()

        class SlowLoop(Loop):
            def closed(self):
                entered.set()
                release.wait(5.0)
                return True
        c = self.armed(make_cfg(self.td, rf_control={"interlock": "sim:il"}), interlock=Loop())
        c._interlock = SlowLoop()
        th = threading.Thread(target=c.poll)
        th.start()
        try:
            self.assertTrue(entered.wait(1.0))
            self.assertTrue(c._lock.acquire(timeout=0.5))
            c._lock.release()
        finally:
            release.set()
            th.join()


# ---------------------------------------------------------------------------
class TestFlowgraphGuard(Base):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.td, "state")
        os.makedirs(self.state)
        sections = merge(BASE, {"active": {"state_dir": self.state}})
        self.ini = os.path.join(self.td, "active_chain_config.ini")
        with open(self.ini, "w") as fh:
            fh.write(C.render_ini(sections))
        self.cfg = C.load_config(self.ini)
        self.fg = os.path.join(self.td, "tx_flowgraph.py")
        with open(self.fg, "w") as fh:
            fh.write("# generated: do not edit\n")
        self.samples = wave(2, 400)
        self.wf = os.path.join(self.td, "burst.cf32")
        self.samples.astype(np.complex64).tofile(self.wf)

    def arm_for_guard(self, max_bursts=2):
        c = make_ctl(self.cfg, self.state, self.clock)
        arm(c, flowgraph_sha256=sha256_file(self.fg), waveform_sha256=sha256_file(self.wf),
            max_bursts=max_bursts)
        return c

    def guard(self, **kw):
        kw.setdefault("state_dir", self.state)
        kw.setdefault("clock", self.clock)
        kw.setdefault("wallclock", lambda: WALL0 + self.clock.t)
        return S.flowgraph_guard(self.ini, self.fg, self.wf, [0, 0], [0, 1], FS, **kw)

    def test_accepts_matching_record(self):
        self.arm_for_guard()
        g = self.guard(n_bursts=2)
        self.assertEqual(g.controller.state, S.TxState.ARMED)
        self.assertTrue(os.path.exists(os.path.join(self.state, S.ARM_RECORD_NAME)))   # adopted, not deleted
        p = g.check_waveform(self.samples, offset_s=0.0)
        self.assertEqual(p.samples.shape, (2, 400))
        g.check_waveform(self.samples, offset_s=1.0)
        self.refused("ARM_TOTAL", g.check_waveform, self.samples)
        bad = self.samples.copy()
        bad[0, 0] = 0.6
        g2 = (self.arm_for_guard(5), self.guard())[1]
        self.refused("AMPLITUDE", g2.check_waveform, bad)
        stops = []
        g2.start_watchdog(lambda: stops.append("stop"), lambda: stops.append("gain_min"))
        try:
            steps = g2.shutdown("exit")
        finally:
            g2.controller.uninstall_process_hooks()
        self.assertEqual(stops, ["stop", "gain_min"])
        self.assertTrue(all(ok for _, ok, _ in steps))
        self.assertFalse(os.path.exists(os.path.join(self.state, S.ARM_RECORD_NAME)))

    def test_refusals(self):
        self.refused("NOT_ARMED", self.guard)
        self.arm_for_guard()
        self.refused("ARM_TOTAL", self.guard, n_bursts=3)
        self.refused("ARM_EXPIRED", self.guard, wallclock=lambda: WALL0 + 61.0)
        with open(self.fg, "a") as fh:
            fh.write("tb.run()\n")
        self.refused("DIGEST", self.guard)
        self.arm_for_guard()
        np.zeros((2, 400), np.complex64).tofile(self.wf)
        self.refused("DIGEST", self.guard)
        self.samples.astype(np.complex64).tofile(self.wf)
        self.guard()
        with open(self.ini, "a") as fh:
            fh.write("\n[active_rf]\nlo_offset_hz = 1000\n")
        self.refused("CONFIG_CHANGED", self.guard)

    def test_zero_burst_record_refused(self):
        self.arm_for_guard(max_bursts=0)
        self.refused("ARM_TOTAL", self.guard)

    def test_watchdog_stops_graph_at_expiry(self):
        self.arm_for_guard()
        g = self.guard()
        stops = []
        g.start_watchdog(lambda: stops.append("stop"), lambda: stops.append("gain_min"))
        try:
            self.clock.advance(61.0)
            t0 = time.monotonic()
            while not stops and time.monotonic() - t0 < 0.2:
                time.sleep(0.005)
            self.assertEqual(stops[:2], ["stop", "gain_min"])
            self.assertEqual(g.controller.state, S.TxState.SAFE)
        finally:
            g.controller.stop_watchdog()
            g.controller.uninstall_process_hooks()


    def test_guard_stops_on_disarm_or_trip_in_arming_process(self):
        main = self.arm_for_guard(max_bursts=50)
        g = self.guard()
        g.check_waveform(self.samples)
        main.disarm("operator")
        self.clock.advance(0.1)
        g.controller.heartbeat()
        g.controller.poll()
        self.assertEqual(g.controller.state, S.TxState.SAFE)
        self.refused("NOT_ARMED", g.check_waveform, self.samples)
        main2 = self.arm_for_guard(max_bursts=50)
        g2 = self.guard()
        main2.trip("INTERLOCK_OPEN", "E-stop")
        self.refused("NOT_ARMED", g2.check_waveform, self.samples)
        # a guard of an old session never deletes the arming process's next record
        main3 = self.arm_for_guard(max_bursts=50)
        g3 = self.guard()
        main3.disarm()
        rec = arm(main3, flowgraph_sha256=sha256_file(self.fg), waveform_sha256=sha256_file(self.wf),
                  max_bursts=5)
        g3.controller.poll()
        self.assertEqual(g3.controller.state, S.TxState.SAFE)
        self.assertEqual(S.read_arm_record(self.state).session_id, rec.session_id)

    def test_adopted_expiry_is_capped(self):
        wall = WALL0 + self.clock.t
        rec = S.ArmRecord("s1", "dummy_load", "op", FC, FS, wall - 10.0, wall + 500.0, C.config_fingerprint(self.cfg))
        c = S.TxSafetyController(self.cfg, None, None, EventLog(), clock=self.clock, wallclock=lambda: wall,
                                 state_dir=self.state, adopt_record=rec)
        self.assertLessEqual(c._expiry_mono - self.clock.t, 60.0 + 1e-9)   # arm_timeout_s
        long = dataclasses.replace(rec, expires_unix=wall + 2000.0)
        with self.assertRaises(SafetyRefusal) as cm:
            S.TxSafetyController(self.cfg, None, None, EventLog(), clock=self.clock, wallclock=lambda: wall,
                                 state_dir=self.state, adopt_record=long)
        self.assertEqual(cm.exception.code, "ARM_TOTAL")

    def test_guard_fs_must_match_config(self):
        self.arm_for_guard()
        self.refused("SHAPE", S.flowgraph_guard, self.ini, self.fg, self.wf, [0, 0], [0, 1], 2 * FS,
                     state_dir=self.state, clock=self.clock, wallclock=lambda: WALL0 + self.clock.t)

if __name__ == "__main__":
    unittest.main()
