"""
    Unit tests for the active timing / coherence module (Firmware/_active/active_timing.py).

    Coherence plans per hardware scenario, the sync procedure on the deterministic
    simulator (call order and failure codes), timed retune, generation rules,
    coherence verification and the error-budget numbers. Hardware-free; the
    simulator's virtual clock replaces every wall-clock wait.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import copy
import logging
import math
import sys
import tempfile
import unittest
from os.path import dirname, join, realpath

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_backend_sim as SB  # noqa: E402
import active_config as C  # noqa: E402
import active_timing as T  # noqa: E402
from active_common import BackendError, CoherenceError, EventLog, HardwareFault  # noqa: E402

logging.getLogger("antenna_profile").setLevel(logging.ERROR)
logging.getLogger("heimdall.active").setLevel(logging.CRITICAL + 10)

FC = 915e6
EXT = {"clock_source": "external", "time_source": "external"}


def dev(family, rx="0", tx="", **kw):
    d = {"family": family, "rx_channels": rx}
    if tx:
        d["tx_channels"] = tx
    d.update(kw)
    return d


def cfg_of(td, devices, n=None, **over):
    n = n if n is not None else sum(len(C.parse_int_list(d["rx_channels"])) for d in devices.values())
    kw = {"devices": devices, "geometry": {"preset": "ula", "num_elements": max(2, n)}}
    kw.update(over)
    return C.default_config(base_dir=td, **kw)


def codes(issues, level=None):
    return {i.code for i in issues if level is None or i.level == level}


TWO_SIM = {"a": dev("sim", "0..3", group="ga", role="master", **EXT),
           "b": dev("sim", "0..3", group="gb", **EXT)}
COMPOUND_SIM = {"a": dev("sim", "0..3", role="master", **EXT), "b": dev("sim", "0..3", **EXT)}


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def sim(self, cfg, **kw):
        ev = EventLog()
        s = SB.SimBackend(cfg, ev, **kw)
        s.open()
        return s, T.plan_coherence(cfg), ev


# ---------------------------------------------------------------------------
class TestPlan(Base):
    def test_single_b210(self):
        cfg = cfg_of(self.td, {"b": dev("b210", "0..1")})
        p = T.plan_coherence(cfg)
        self.assertEqual(p.sync_method, "none")
        self.assertEqual(p.coherence_class, T.CoherenceClass.SHARED_LO)
        self.assertEqual(p.phase_after_tune, "shared_lo")
        self.assertTrue(p.timed_tune)
        self.assertEqual(p.sources["b"], {"clock": "internal", "time": "internal"})
        self.assertEqual(p.errors(), [])
        self.assertEqual(p.lo_plan.mode, "none")

    def test_n321_star_four_devices(self):
        devs = {"m": dev("n321", "0..1", "0..1", lo_export=1, role="master", group="star", **EXT)}
        for k in range(3):
            devs["s%d" % k] = dev("n320", "0..1", "0..1", lo_source="external", group="star", **EXT)
        cfg = cfg_of(self.td, devs)
        p = T.plan_coherence(cfg)
        self.assertEqual(len(p.groups), 1)
        self.assertTrue(p.groups[0].compound)
        self.assertEqual(p.sync_method, "unknown_pps")
        self.assertEqual(p.lo_plan.mode, "n321_star")
        self.assertEqual(p.lo_plan.exporter.device, "m")
        self.assertEqual(len(p.lo_plan.importers), 12)                     # 3 x (2 rx + 2 tx)
        self.assertEqual(p.phase_after_tune, "shared_lo")
        self.assertEqual(p.coherence_class, T.CoherenceClass.SHARED_LO)
        self.assertEqual(p.errors(), [])
        # below the LO-sharing floor (450 MHz) the phase is random after every tune
        low = T.plan_coherence(cfg_of(self.td, devs, rf={"center_freq_hz": 300e6}))
        self.assertNotEqual(low.phase_after_tune, "shared_lo")
        self.assertTrue(low.recal_on_tune)
        # two exporters is an error
        devs2 = copy.deepcopy(devs)
        devs2["s0"]["lo_export"] = 1
        devs2["s0"]["family"] = "n321"
        del devs2["s0"]["lo_source"]
        self.assertIn("COHERENCE_LO", codes(T.plan_coherence(cfg_of(self.td, devs2)).issues, "error"))

    def test_two_b210(self):
        devs = {"a": dev("b210", "0..1", master_clock_rate_hz=30.72e6, role="master", **EXT),
                "b": dev("b210", "0..1", master_clock_rate_hz=30.72e6, **EXT)}
        p = T.plan_coherence(cfg_of(self.td, devs))
        self.assertEqual(len(p.groups), 2)
        self.assertEqual(p.sync_method, "next_pps_host")
        self.assertEqual(p.coherence_class, T.CoherenceClass.SHARED_REF)
        self.assertEqual(p.errors(), [])
        self.assertIn("COHERENCE_MULTI_DEVICE", codes(p.issues, "warning"))      # [verify], spec D7
        self.assertTrue(p.recal_on_tune)
        # internal references on a multi-device setup
        bad = {"a": dev("b210", "0..1"), "b": dev("b210", "0..1", clock_source="internal")}
        p2 = T.plan_coherence(cfg_of(self.td, bad))
        self.assertIn("COHERENCE_NO_SHARED_REF", codes(p2.issues, "error"))
        self.assertEqual(p2.coherence_class, T.CoherenceClass.INCOHERENT)

    def test_two_x410_is_an_error(self):
        devs = {"a": dev("x410", "0..3", role="master", **EXT), "b": dev("x410", "0..3", **EXT)}
        p = T.plan_coherence(cfg_of(self.td, devs))
        self.assertIn("COHERENCE_MULTI_DEVICE", codes(p.issues, "error"))
        self.assertFalse(p.timed_tune)
        self.assertEqual(p.coherence_class, T.CoherenceClass.TIME_ALIGNED)
        raw = T.plan_coherence(cfg_of(self.td, devs, output={"output_mode": "raw"}))
        self.assertIn("COHERENCE_MULTI_DEVICE", codes(raw.issues, "warning"))
        self.assertNotIn("COHERENCE_MULTI_DEVICE", codes(raw.issues, "error"))

    def test_two_crimson(self):
        devs = {"a": dev("crimson_tng", "0..3", addr="192.168.10.2", role="master", **EXT),
                "b": dev("crimson_tng", "0..3", addr="192.168.10.3", **EXT)}
        p = T.plan_coherence(cfg_of(self.td, devs, rf={"sample_rate_hz": 325e6 / 128}))
        self.assertEqual(len(p.groups), 2)
        self.assertEqual(p.sync_method, "next_pps_host")
        self.assertFalse(p.timed_tune)
        self.assertTrue(p.recal_on_tune)
        self.assertEqual(p.phase_after_tune, "random")
        self.assertIn("COHERENCE_PV_ALIGN", codes(p.issues, "warning"))
        self.assertEqual(p.coherence_class, T.CoherenceClass.SHARED_REF)
        on = T.plan_coherence(cfg_of(self.td, devs, timing={"timed_tune": "on"}))
        self.assertIn("COHERENCE_TIMED_TUNE", codes(on.issues, "error"))

    def test_twinrx_shared_lo(self):
        devs = {"x": dev("x310_twinrx", "0..3", lo_source="companion")}
        p = T.plan_coherence(cfg_of(self.td, devs))
        self.assertEqual(p.lo_plan.mode, "twinrx")
        self.assertEqual(p.phase_after_tune, "shared_lo")
        self.assertEqual(p.coherence_class, T.CoherenceClass.SHARED_LO)
        self.assertEqual(len(p.lo_plan.importers), 4)
        # two X310 + TwinRX: one exports, the other imports through the external LO port
        devs2 = {"m": dev("x310_twinrx", "0..3", lo_export=1, role="master", group="x", **EXT),
                 "s": dev("x310_twinrx", "0..3", lo_source="external", group="x", **EXT)}
        p2 = T.plan_coherence(cfg_of(self.td, devs2))
        self.assertEqual(p2.sync_method, "unknown_pps")
        self.assertEqual(p2.lo_plan.exporter.device, "m")
        self.assertEqual(p2.coherence_class, T.CoherenceClass.SHARED_LO)
        self.assertEqual(p2.errors(), [])

    def test_lla_distributed_time_aligned(self):
        gps = {"clock_source": "gpsdo", "time_source": "gpsdo"}
        devs = {"site1": dev("n310", "0..3", group="g1", role="master", **gps),
                "site2": dev("n310", "0..3", group="g2", **gps)}
        p = T.plan_coherence(cfg_of(self.td, devs, timing={"sync_method": "gps_time"}))
        self.assertEqual(p.sync_method, "gps_time")
        self.assertEqual(p.coherence_class, T.CoherenceClass.TIME_ALIGNED)
        self.assertIn("COHERENCE_CLASS", codes(p.issues, "warning"))
        self.assertEqual(p.errors(), [])
        bad = T.plan_coherence(cfg_of(self.td, TWO_SIM, timing={"sync_method": "gps_time"}))
        self.assertIn("COHERENCE_GPS_TIME", codes(bad.issues, "error"))

    def test_sync_rules(self):
        p = T.plan_coherence(cfg_of(self.td, TWO_SIM, timing={"sync_method": "none"}))
        self.assertIn("COHERENCE_SYNC", codes(p.issues, "error"))
        p = T.plan_coherence(cfg_of(self.td, TWO_SIM, timing={"sync_method": "unknown_pps"}))
        self.assertIn("COHERENCE_SYNC", codes(p.issues, "error"))
        p = T.plan_coherence(cfg_of(self.td, COMPOUND_SIM))
        self.assertEqual(p.sync_method, "unknown_pps")
        self.assertEqual(p.errors(), [])
        p = T.plan_coherence(cfg_of(self.td, COMPOUND_SIM, timing={"stream_lead_s": 0.1}))
        self.assertIn("COHERENCE_LEAD", codes(p.issues, "error"))
        p = T.plan_coherence(cfg_of(self.td, COMPOUND_SIM, timing={"require_ref_lock": 0}))
        self.assertIn("COHERENCE_REF_LOCK", codes(p.issues, "warning"))
        d = T.plan_coherence(cfg_of(self.td, COMPOUND_SIM)).to_dict()
        self.assertEqual(d["coherence_class"], "SHARED_REF")
        self.assertEqual(d["groups"][0]["devices"], ["a", "b"])

    def test_recal_on_tune_rule(self):
        devs = {"x": dev("sim", "0..3")}
        rep = cfg_of(self.td, devs, sim={"emulate_family": "x310_ubx"}, calibration={"recal_on_tune": 0})
        p = T.plan_coherence(rep)
        self.assertEqual(p.phase_after_tune, "repeatable")
        self.assertFalse(p.recal_on_tune)
        self.assertTrue(T.plan_coherence(cfg_of(self.td, devs, sim={"emulate_family": "x310_ubx"})).recal_on_tune)


# ---------------------------------------------------------------------------
class TestSync(Base):
    def test_call_order_next_pps_host(self):
        cfg = cfg_of(self.td, TWO_SIM)
        sim, plan, ev = self.sim(cfg)
        g0 = sim.coherence.generation
        rep = T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        self.assertTrue(rep.ok)
        self.assertEqual(rep.method, "next_pps_host")
        self.assertEqual(rep.skew_ticks, 0)
        names = [c[0] for c in sim.calls]
        first = {n: names.index(n) for n in ("safe_state", "set_clock_source", "ref_locked",
                                              "set_master_clock_rate", "set_time_next_pps")}
        self.assertLess(first["safe_state"], first["set_clock_source"])
        self.assertLess(first["set_clock_source"], first["ref_locked"])
        self.assertLess(first["ref_locked"], first["set_master_clock_rate"])
        self.assertLess(first["set_master_clock_rate"], first["set_time_next_pps"])
        last_time = max(i for i, n in enumerate(names) if n == "set_time_next_pps")
        self.assertGreater(max(i for i, n in enumerate(names) if n == "time_last_pps"), last_time)  # verify
        srcs = [c[2] for c in sim.calls if c[0] == "set_clock_source"]
        self.assertEqual(sim.calls[names.index("set_clock_source")][1][1], "a")                 # master first
        self.assertEqual(len(srcs), 2)
        self.assertEqual({c[1][1] for c in sim.calls if c[0] == "set_time_next_pps"}, {"a", "b"})
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertEqual(ev.recent(5, "active_sync_ok")[-1]["payload"]["method"], "next_pps_host")
        self.assertEqual(sim.time_now("a"), sim.time_now("b"))
        self.assertLess(sim.time_now("a").to_seconds(), 3.0)                                   # time set to k = 0
        self.assertAlmostEqual(rep.epoch_offset_s, sim.wallclock() - sim.time_now("a").to_seconds(), places=3)
        self.assertEqual(sim.phase, "TIME_SET")
        self.assertEqual(set(rep.pps_times), {"a", "b"})

    def test_unknown_pps_compound(self):
        cfg = cfg_of(self.td, COMPOUND_SIM)
        sim, plan, ev = self.sim(cfg)
        rep = T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        self.assertTrue(rep.ok and rep.time_synchronized)
        self.assertIn("set_time_unknown_pps", [c[0] for c in sim.calls])
        self.assertNotIn("set_time_next_pps", [c[0] for c in sim.calls])

    def test_single_device_none(self):
        cfg = cfg_of(self.td, {"x": dev("sim", "0..3")})
        sim, plan, ev = self.sim(cfg)
        rep = T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        self.assertEqual(rep.method, "none")
        names = [c[0] for c in sim.calls]
        self.assertNotIn("set_time_next_pps", names)
        self.assertNotIn("set_time_unknown_pps", names)
        self.assertEqual(sim.phase, "REFS_SET")

    def test_gps_time(self):
        gps = {"clock_source": "external", "time_source": "gpsdo"}
        devs = {"a": dev("sim", "0..3", group="ga", role="master", **gps),
                "b": dev("sim", "0..3", group="gb", **gps)}
        cfg = cfg_of(self.td, devs, timing={"sync_method": "gps_time"})
        sim, plan, ev = self.sim(cfg)
        T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        gps_now = SB.GPS_EPOCH_UNIX + sim.mono()
        self.assertAlmostEqual(sim.time_now("a").to_seconds(), gps_now, delta=1e-6)

    def test_ref_lock_failure(self):
        cfg = cfg_of(self.td, TWO_SIM)
        sim, plan, ev = self.sim(cfg)
        sim.inject_fault("ref_unlock", device="b")
        with self.assertRaises(CoherenceError) as cm:
            T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        self.assertEqual(cm.exception.code, "REF_LOCK")
        self.assertEqual(ev.recent(5, "active_sync_fail")[-1]["payload"]["code"], "REF_LOCK")
        polls = [c for c in sim.calls if c[0] == "ref_locked" and c[1][0] == "b"]
        self.assertGreaterEqual(len(polls), 50)                                 # 5 s at 0.1 s
        lax = cfg_of(self.td, TWO_SIM, timing={"require_ref_lock": 0}, sim={"ref_ppm_sigma": 0.0})
        sim2, plan2, ev2 = self.sim(lax)
        sim2.inject_fault("ref_unlock", device="b")
        rep = T.SyncProcedure(sim2, plan2, lax.timing, ev2).run()
        self.assertTrue(rep.ok)
        self.assertIn("REF_LOCK", codes(rep.issues, "warning"))

    def test_pps_missing(self):
        for devs in (TWO_SIM, COMPOUND_SIM):
            cfg = cfg_of(self.td, devs)
            sim, plan, ev = self.sim(cfg)
            sim.inject_fault("pps_missing")
            sp = T.SyncProcedure(sim, plan, cfg.timing, ev)
            with self.assertRaises(CoherenceError) as cm:
                sp.run()
            self.assertEqual(cm.exception.code, "PPS_MISSING")
            self.assertFalse(sp.report.ok)

    def test_pps_skew(self):
        cfg = cfg_of(self.td, TWO_SIM, sim={"pps_skew_ticks": 4})
        sim, plan, ev = self.sim(cfg)
        with self.assertRaises(CoherenceError) as cm:
            T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        self.assertEqual(cm.exception.code, "PPS_SKEW")
        ok = cfg_of(self.td, TWO_SIM, sim={"pps_skew_ticks": 1})
        sim2, plan2, ev2 = self.sim(ok)
        rep = T.SyncProcedure(sim2, plan2, ok.timing, ev2).run()
        self.assertEqual(rep.skew_ticks, 1)

    def test_failed_sync_invalidates_the_time(self):
        # PPS_SKEW in the verify step: the time loads already ran on every device
        cfg = cfg_of(self.td, TWO_SIM, sim={"pps_skew_ticks": 500})
        sim, plan, ev = self.sim(cfg)
        self.assertEqual(sim.phase, "OPEN")
        sp = T.SyncProcedure(sim, plan, cfg.timing, ev)
        with self.assertRaises(CoherenceError) as cm:
            sp.run()
        self.assertEqual(cm.exception.code, "PPS_SKEW")
        self.assertFalse(sp.report.ok)
        self.assertEqual(sim.phase, "REFS_SET")                            # not TIME_SET
        self.assertEqual(sim.coherence.snapshot()["last_reason"], "sync_fail")
        T.retune(sim, plan, FC)                                            # tuning is allowed, streaming is not
        self.assertFalse(sim._time_ok())
        with self.assertRaises(BackendError):
            sim.rx_capture(256, sim.time_now().add_seconds(0.05))
        with self.assertRaises(BackendError):
            sim.rx_start(sim.time_now().add_seconds(0.05))
        # a failure on a backend error path (GPS time missing after the references) too
        cfg2 = cfg_of(self.td, TWO_SIM)
        sim2, plan2, ev2 = self.sim(cfg2)
        T.SyncProcedure(sim2, plan2, cfg2.timing, ev2).run()
        T.retune(sim2, plan2, FC)
        self.assertEqual(sim2.phase, "TUNED")
        orig = sim2._do_set_time_next_pps
        calls = []

        def flaky(t, device):
            calls.append(device)
            if len(calls) > 1:
                raise HardwareFault("time load failed on the second device")
            return orig(t, device)
        sim2._do_set_time_next_pps = flaky
        with self.assertRaises(CoherenceError) as cm:
            T.SyncProcedure(sim2, plan2, cfg2.timing, ev2).run()
        self.assertEqual(cm.exception.code, "BACKEND")
        self.assertEqual(sim2.phase, "REFS_SET")
        with self.assertRaises(BackendError):
            sim2.rx_capture(256, sim2.time_now().add_seconds(0.05))

    def test_plan_errors_refuse_sync(self):
        cfg = cfg_of(self.td, TWO_SIM, timing={"sync_method": "none"})
        sim, plan, ev = self.sim(cfg)
        with self.assertRaises(CoherenceError) as cm:
            T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        self.assertEqual(cm.exception.code, "COHERENCE_SYNC")

    def test_lo_plan_applied(self):
        devs = {"m": dev("sim", "0..1", "0..1", lo_export=1, role="master", group="s", **EXT),
                "s": dev("sim", "0..1", "0..1", lo_source="external", group="s", **EXT)}
        cfg = cfg_of(self.td, devs, sim={"emulate_family": "n321"}, timing={"lo_plan": "n321_star"},
                     tx={"allow_bands": "902e6:928e6"})
        sim, plan, ev = self.sim(cfg)
        rep = T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        names = [c[0] for c in sim.calls]
        self.assertLess(names.index("set_master_clock_rate"), names.index("set_lo_export"))
        self.assertLess(names.index("set_lo_source"), names.index("set_time_unknown_pps"))
        self.assertIn("lo_plan", [s[0] for s in rep.steps])
        self.assertEqual(len([c for c in sim.calls if c[0] == "set_lo_source"]), 4)   # 2 rx + 2 tx importers
        T.retune(sim, plan, FC)
        a = sim.truth.lo_phase_rx.copy()
        T.retune(sim, plan, FC)
        b = sim.truth.lo_phase_rx.copy()
        np.testing.assert_allclose(np.angle(np.exp(1j * (b - b[0]))), np.angle(np.exp(1j * (a - a[0]))), atol=1e-9)
        # a device without LO names cannot take part: runtime ConfigError
        sim2, plan2, _ = self.sim(cfg_of(self.td, devs, timing={"lo_plan": "n321_star"},
                                         sim={"emulate_family": "x410"}))
        plan2.lo_plan = T.LoPlan("n321_star", None, [(sim2.rx_refs()[2], "rx")])
        with self.assertRaises(C.ConfigError):
            T.apply_lo_plan(sim2, plan2)
        self.assertEqual(T.apply_lo_plan(sim2, T.CoherencePlan([], {}, "", "none", False, T.LoPlan("none"),
                                                                T.CoherenceClass.SHARED_REF, "random", True,
                                                                0.1, 0.3, [])), [])

    def test_resync_trips_armed_tx(self):
        cfg = cfg_of(self.td, TWO_SIM)
        sim, plan, ev = self.sim(cfg)
        armed = _Safety("armed")
        rep = T.SyncProcedure(sim, plan, cfg.timing, ev, safety=armed).run()
        self.assertTrue(rep.ok)
        self.assertEqual(armed.tripped, ["REF_UNLOCK"])                 # spec 8.3, before anything else
        attached = _Safety("transmitting")
        sim.attach_safety(attached)                                     # default: backend.safety
        T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        self.assertEqual(attached.tripped, ["REF_UNLOCK"])
        idle = _Safety("safe")
        T.SyncProcedure(sim, plan, cfg.timing, ev, safety=idle).run()
        self.assertFalse(hasattr(idle, "tripped"))

    def test_rf_force_safe_and_stream_start(self):
        class Rf:
            calls = 0

            def force_safe(self):
                Rf.calls += 1
                return []
        cfg = cfg_of(self.td, TWO_SIM)
        sim, plan, ev = self.sim(cfg)
        sp = T.SyncProcedure(sim, plan, cfg.timing, ev, rf_control=Rf())
        sp.run()
        self.assertEqual(Rf.calls, 1)
        now = sim.time_now("a")
        st = sp.stream_start_time()
        self.assertEqual(st.diff_ticks(now), 30_000_000)                         # 0.3 s at 100 MHz


# ---------------------------------------------------------------------------
class _Safety:
    """Records disarm; reports the configured state."""

    def __init__(self, state="armed"):
        self.state = state
        self.disarmed = []

    def disarm(self, reason="operator"):
        self.disarmed.append(reason)
        self.state = "safe"

    def trip(self, code, detail=""):
        self.tripped = getattr(self, "tripped", []) + [code]
        self.state = "fault"


TRX = {"a": dev("sim", "0..3", "0..3", group="ga", role="master", **EXT),
       "b": dev("sim", "0..3", group="gb", **EXT)}


class TestRetune(Base):
    def synced(self, cfg):
        sim, plan, ev = self.sim(cfg)
        T.SyncProcedure(sim, plan, cfg.timing, ev).run()
        return sim, plan, ev

    def test_timed_retune_one_command_window_and_disarm(self):
        cfg = cfg_of(self.td, TRX, sim={"emulate_family": "x310_ubx"}, tx={"allow_bands": "902e6:928e6"})
        sim, plan, ev = self.synced(cfg)
        self.assertTrue(plan.timed_tune)
        safety = _Safety("armed")
        n0 = len(sim.calls)
        g0 = sim.coherence.generation
        reps = T.retune(sim, plan, 920e6, safety=safety)
        self.assertEqual(safety.disarmed, ["retune"])
        names = [c[0] for c in sim.calls[n0:]]
        i_set, i_clr = names.index("set_command_time"), names.index("clear_command_time")
        tunes = [i for i, n in enumerate(names) if n == "tune"]
        self.assertEqual(len(tunes), 2)
        self.assertTrue(all(i_set < i < i_clr for i in tunes))
        self.assertEqual(names.count("set_command_time"), 1)
        t_cmd = sim.calls[n0 + i_set][1][0]
        self.assertTrue(all(sim.calls[n0 + i][1][3] == t_cmd for i in tunes))     # tune(..., at=t)
        self.assertEqual([r.direction for r in reps], ["rx", "tx"])
        self.assertTrue(all(r.timed for r in reps))
        self.assertEqual({r.phase_state for r in reps}, {"repeatable"})
        self.assertEqual(sim.coherence.generation, g0)                            # no bump
        self.assertEqual(ev.recent(5, "active_tune")[-1]["payload"]["generation"], g0)
        nxt = sim.calls[n0 + i_clr + 1]
        self.assertEqual(nxt[0], "sleep")                                         # past the command time
        self.assertAlmostEqual(nxt[1][0], cfg.timing.command_lead_s + 0.01)
        not_armed = _Safety("safe")
        T.retune(sim, plan, 921e6, safety=not_armed)
        self.assertEqual(not_armed.disarmed, [])
        attached = _Safety("armed")
        sim.attach_safety(attached)                                     # default: backend.safety
        T.retune(sim, plan, 922e6)
        self.assertEqual(attached.disarmed, ["retune"])

    def test_untimed_retune_bumps_once(self):
        cfg = cfg_of(self.td, TRX, sim={"emulate_family": "x310_ubx"}, timing={"timed_tune": "off"},
                     tx={"allow_bands": "902e6:928e6"})
        sim, plan, ev = self.synced(cfg)
        self.assertFalse(plan.timed_tune)
        g0 = sim.coherence.generation
        reps = T.retune(sim, plan, 915e6)
        self.assertNotIn("set_command_time", [c[0] for c in sim.calls])
        self.assertEqual({r.phase_state for r in reps}, {"random"})
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertEqual({r.generation for r in reps}, {g0 + 1})

    def test_band_change_event_and_bump(self):
        devs = {"a": dev("sim", "0..3", group="ga", role="master", **EXT), "b": dev("sim", "0..3", group="gb", **EXT)}
        cfg = cfg_of(self.td, devs, sim={"emulate_family": "crimson_tng"}, rf={"center_freq_hz": 150e6})
        sim, plan, ev = self.synced(cfg)
        T.retune(sim, plan, 150e6)
        g0 = sim.coherence.generation
        reps = T.retune(sim, plan, 100e6)
        self.assertTrue(reps[0].band_change)
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertEqual(sim.coherence.snapshot()["last_reason"], "band_change")
        self.assertEqual(ev.recent(5, "active_band_change")[-1]["payload"]["freq_hz"], 100e6)
        reps = T.retune(sim, plan, 101e6)                       # same band, Per Vices: random -> bump
        self.assertFalse(reps[0].band_change)
        self.assertEqual(sim.coherence.generation, g0 + 2)

    def test_tx_left_untuned_in_denied_band(self):
        cfg = cfg_of(self.td, TRX, tx={"allow_bands": "902e6:928e6"})
        sim, plan, ev = self.synced(cfg)
        T.retune(sim, plan, 915e6)
        reps = T.retune(sim, plan, 1575.42e6)                   # GPS L1: built-in deny band
        tx = [r for r in reps if r.direction == "tx"][0]
        self.assertEqual(tx.phase_state, "untuned")
        self.assertIn("FREQ_DENIED", codes(tx.issues))
        self.assertEqual(sim.tuned_freq("tx"), 915e6)
        self.assertEqual(sim.tuned_freq("rx"), 1575.42e6)
        self.assertTrue(ev.recent(5, "active_tune")[-1]["payload"]["tx_untuned"])
        # no TX policy at all: nothing may be transmitted, TX stays untuned
        bare = cfg_of(self.td, TRX)
        sim2, plan2, _ = self.synced(bare)
        reps = T.retune(sim2, plan2, 915e6)
        self.assertEqual([r for r in reps if r.direction == "tx"][0].phase_state, "untuned")
        self.assertEqual(len(T.retune(sim2, plan2, 915e6, direction="rx")), 1)

    def test_lo_lock_failure(self):
        cfg = cfg_of(self.td, TRX, tx={"allow_bands": "902e6:928e6"})
        sim, plan, ev = self.synced(cfg)
        sim.inject_fault("lo_unlock", device="b")
        with self.assertRaises(CoherenceError) as cm:
            T.retune(sim, plan, 915e6)
        self.assertEqual(cm.exception.code, "LO_LOCK")
        self.assertTrue(ev.recent(5, "active_coherence_fail"))

    def _star(self):
        devs = {"m": dev("n321", "0..1", "", lo_export=1, role="master", group="star", **EXT)}
        for k in range(3):
            devs["s%d" % k] = dev("n320", "0..1", "", lo_source="external", group="star", **EXT)
        return self.synced(cfg_of(self.td, devs))

    def test_retune_across_lo_share_floor_bumps(self):
        sim, plan, ev = self._star()
        self.assertEqual(plan.phase_after_tune, "shared_lo")            # the plan, at 915 MHz
        self.assertEqual({r.phase_state for r in T.retune(sim, plan, FC)}, {"shared_lo"})
        g0 = sim.coherence.generation
        T.retune(sim, plan, 1000e6)                                     # timed, shared: no bump
        self.assertEqual(sim.coherence.generation, g0)
        rel = lambda: np.angle(sim.true_rx_response() * np.conj(sim.true_rx_response()[0]))  # noqa: E731
        r = T.retune(sim, plan, 300e6)[0]                               # below lo_share_min_hz (450 MHz)
        self.assertEqual(r.phase_state, "random")
        self.assertTrue(r.band_change)
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertEqual(sim.coherence.snapshot()["last_reason"], "band_change")
        a = rel()
        T.retune(sim, plan, 310e6)                                      # random: every tune bumps
        self.assertEqual(sim.coherence.generation, g0 + 2)
        self.assertGreater(float(np.max(np.abs(np.angle(np.exp(1j * (rel() - a)))))), 0.1)
        r = T.retune(sim, plan, FC)[0]                                  # back above: band change again
        self.assertEqual(r.phase_state, "shared_lo")
        self.assertEqual(sim.coherence.generation, g0 + 3)

    def test_rx_restart_with_common_t0(self):
        cfg = cfg_of(self.td, TWO_SIM)
        sim, plan, ev = self.synced(cfg)
        T.retune(sim, plan, 915e6)
        sim.rx_start(sim.time_now().add_seconds(0.3))
        sim.rx_read(1000)
        n0 = len(sim.calls)
        before = sim.time_now()
        T.retune(sim, plan, 916e6)
        names = [c[0] for c in sim.calls[n0:]]
        self.assertLess(names.index("rx_stop"), names.index("rx_start"))
        self.assertEqual(sim.phase, "STREAMING")
        t0 = [c for c in sim.calls[n0:] if c[0] == "rx_start"][0][1][0]
        self.assertGreater(t0, before)
        self.assertEqual(sim.rx_read(100).start, t0)


# ---------------------------------------------------------------------------
class _Rf:
    def __init__(self, sim):
        self.sim = sim
        self.log = []

    def cal_input(self, mode):
        self.log.append(("cal_input", mode))
        self.sim.set_cal_input(mode)

    def noise_source(self, on):
        self.log.append(("noise", on))


class TestVerify(Base):
    def setup_sync(self, **sim_over):
        cfg = cfg_of(self.td, TWO_SIM, sim=dict({"emulate_family": "x310_ubx"}, **sim_over))
        sim, plan, ev = self.sim(cfg)
        sp = T.SyncProcedure(sim, plan, cfg.timing, ev)
        sp.run()
        T.retune(sim, plan, FC)
        return sim, sp, ev

    def test_clean(self):
        sim, sp, ev = self.setup_sync()
        rf = _Rf(sim)
        rep = T.verify_coherence(sim, rf, sp.stream_start_time)
        self.assertTrue(rep.ok, rep.reasons)
        self.assertEqual(rep.captures, 4)
        self.assertLess(float(np.max(rep.phase_std_deg)), 0.1)
        self.assertLess(float(np.max(np.abs(rep.freq_offset_hz))), 0.05)
        self.assertEqual(rf.log[0], ("cal_input", "injection"))
        self.assertEqual(rf.log[-1], ("cal_input", "antenna"))
        self.assertEqual(sim.cal_input(), "antenna")
        starts = [c[1][1] for c in sim.calls if c[0] == "rx_capture"]
        self.assertEqual([s.diff_ticks(starts[0]) for s in starts[:4]], [0, 5_000_000, 10_000_000, 15_000_000])
        # the same-frequency retune check ran (timed, repeatable family) and reproduced the phases
        self.assertEqual(len(starts), 5)
        self.assertEqual(rep.phase_state, "repeatable")
        self.assertLess(float(np.max(np.abs(rep.retune_step_deg))), 0.5)
        self.assertLess(float(np.max(np.abs(rep.intra_freq_hz))), 1.0)

    def test_ramp_from_an_unshared_reference(self):
        sim, sp, ev = self.setup_sync(ref_ppm_sigma=0.002)
        sim.inject_fault("ref_unlock", device="b")
        rep = T.verify_coherence(sim, _Rf(sim), sp.stream_start_time)
        self.assertFalse(rep.ok)
        f_true = -FC * sim.truth.ref_ppm["b"] * 1e-6
        self.assertGreater(abs(f_true), 0.5)
        np.testing.assert_allclose(rep.freq_offset_hz[4:], f_true, rtol=0.02)
        np.testing.assert_allclose(rep.freq_offset_hz[:4], 0.0, atol=0.01)
        self.assertTrue(any("frequency offset" in r for r in rep.reasons))
        self.assertTrue(ev.recent(5, "active_coherence_fail"))

    def test_jump_from_lo_unlock(self):
        sim, sp, ev = self.setup_sync()
        sim.inject_fault("lo_unlock", device="b", delay_s=0.4)        # between captures 1 and 2
        rep = T.verify_coherence(sim, _Rf(sim), sp.stream_start_time)
        self.assertFalse(rep.ok)
        chans = sorted({j[0] for j in rep.jumps})
        self.assertEqual(chans, [4, 5, 6, 7])
        self.assertEqual({j[1] for j in rep.jumps}, {2})

    def test_aliased_frequency_offset_detected(self):
        sim, sp, ev = self.setup_sync()
        gap = 0.05
        sim._ppm_int["b"] = -(1.0 / gap) / FC * 1e6                     # device b ramps at exactly 1/gap
        sim.inject_fault("ref_unlock", device="b")                       # (a 10 MHz cable left off)
        rep = T.verify_coherence(sim, _Rf(sim), sp.stream_start_time, gap_s=gap)
        self.assertFalse(rep.ok)
        np.testing.assert_allclose(rep.freq_offset_hz[4:], 1.0 / gap, rtol=0.01)
        np.testing.assert_allclose(rep.freq_offset_hz[:4], 0.0, atol=0.01)
        self.assertTrue(any("frequency offset" in r for r in rep.reasons))

    def test_inactive_lo_share_detected(self):
        devs = {"m": dev("n321", "0..1", "", lo_export=1, role="master", group="star", **EXT)}
        for k in range(3):
            devs["s%d" % k] = dev("n320", "0..1", "", lo_source="external", group="star", **EXT)
        cfg = cfg_of(self.td, devs)
        for active in (True, False):
            sim, plan, ev = self.sim(cfg)
            sp = T.SyncProcedure(sim, plan, cfg.timing, ev)
            sp.run()
            if not active:                                              # the device ignored the export
                for k in list(sim._lo_exp):
                    sim._lo_exp[k] = False
            T.retune(sim, plan, FC)
            g0 = sim.coherence.generation
            rep = T.verify_coherence(sim, _Rf(sim), sp.stream_start_time)
            self.assertEqual(rep.phase_state, "shared_lo")
            if active:
                self.assertTrue(rep.ok, rep.reasons)
                self.assertEqual(sim.coherence.generation, g0)
            else:
                self.assertFalse(rep.ok)
                self.assertTrue(any("retune" in r for r in rep.reasons))
                self.assertGreater(float(np.max(np.abs(rep.retune_step_deg))), 5.0)
                self.assertEqual(sim.coherence.generation, g0 + 1)
                self.assertEqual(sim.coherence.snapshot()["last_reason"], "verify_retune")
        # the check can be switched off
        rep = T.verify_coherence(sim, _Rf(sim), sp.stream_start_time, retune_check=False)
        self.assertIsNone(rep.retune_step_deg)

    def test_needs_two_captures(self):
        sim, sp, ev = self.setup_sync()
        with self.assertRaises(ValueError):
            T.verify_coherence(sim, None, sp.stream_start_time, n_caps=1)

    def test_passing_verify_keeps_the_generation_untimed_stable_state(self):
        # X440: deterministic phase, no timed tune -> the check retune is untimed; a PASSING
        # verification must not bump (it would invalidate the calibration it just verified)
        cfg = cfg_of(self.td, {"a": dev("x440", "0..1", master_clock_rate_hz=250e6)},
                     rf={"center_freq_hz": FC, "sample_rate_hz": 1e6})
        sim, plan, ev = self.sim(cfg)
        sp = T.SyncProcedure(sim, plan, cfg.timing, ev, clock=sim.mono)
        sp.run()
        T.retune(sim, plan, FC, direction="rx")
        self.assertEqual(sim.last_tune("rx")[2:], (False, "deterministic"))
        g0 = sim.coherence.generation
        rep = T.verify_coherence(sim, _Rf(sim), sp.stream_start_time)
        self.assertTrue(rep.ok, rep.reasons)
        self.assertEqual(rep.phase_state, "deterministic")
        self.assertIsNotNone(rep.retune_step_deg)                        # the check did run
        self.assertEqual(sim.coherence.generation, g0)
        # timed family (x310_ubx): unchanged as well
        sim2, sp2, _ev2 = self.setup_sync()
        g1 = sim2.coherence.generation
        self.assertTrue(T.verify_coherence(sim2, _Rf(sim2), sp2.stream_start_time).ok)
        self.assertEqual(sim2.coherence.generation, g1)

    def test_check_retune_that_changes_the_phase_class_fails_and_bumps(self):
        devs = {"m": dev("n321", "0..1", "", lo_export=1, role="master", group="star", **EXT),
                "s0": dev("n320", "0..1", "", lo_source="external", group="star", **EXT)}
        cfg = cfg_of(self.td, devs)
        sim, plan, ev = self.sim(cfg)
        sp = T.SyncProcedure(sim, plan, cfg.timing, ev, clock=sim.mono)
        sp.run()
        T.retune(sim, plan, FC)
        self.assertEqual(sim.last_tune("rx")[3], "shared_lo")
        g0 = sim.coherence.generation
        sim.set_lo_export("rx", False, sim.rx_refs()[0])                # through the API: bumps at once
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertEqual(sim.coherence.snapshot()["last_reason"], "lo_export_rx")
        rep = T.verify_coherence(sim, _Rf(sim), sp.stream_start_time)
        self.assertFalse(rep.ok)
        self.assertTrue(any("left phase state random" in r for r in rep.reasons), rep.reasons)
        self.assertEqual(sim.coherence.generation, g0 + 2)
        self.assertEqual(sim.coherence.snapshot()["last_reason"], "verify_retune")


# ---------------------------------------------------------------------------
class TestBudget(unittest.TestCase):
    def test_numbers(self):
        self.assertAlmostEqual(T.skew_to_phase_deg(2.4e9, 1e-12), 0.864, places=6)
        self.assertAlmostEqual(T.position_tolerance_m(1e9, 1.0) * 1e3, 0.833, places=3)
        self.assertAlmostEqual(T.coherent_gain_loss_db(10.0), 0.132, places=3)
        self.assertAlmostEqual(T.ula_bearing_sigma_deg(2.0, 8, C.ac.C0 / 1e9 / 2, 1e9), 0.10, delta=0.005)
        self.assertAlmostEqual(T.freq_offset_hz(1e9, 0.5), 500.0)
        self.assertAlmostEqual(T.null_floor_db(1.0, 0.1, 8), -42.6, delta=0.05)
        self.assertAlmostEqual(T.sidelobe_floor_db(10.0, 8), 10 * math.log10(math.radians(10) ** 2 / 8))
        self.assertEqual(T.sidelobe_floor_db(0.0, 8), float("-inf"))
        self.assertAlmostEqual(T.rss_deg({"a": 3, "b": 4}), 5.0)
        self.assertGreater(T.ula_bearing_sigma_deg(2.0, 8, 0.15, 1e9, 60.0),
                           T.ula_bearing_sigma_deg(2.0, 8, 0.15, 1e9, 0.0))
        with self.assertRaises(ValueError):
            T.ula_bearing_sigma_deg(1.0, 1, 0.1, 1e9)

    def test_budget_dict(self):
        b = T.coherence_budget(2.4e9, skew_s=1e-12, pos_sigma_m=0.0, cal_phase_deg=0.5, drift_deg=0.0, n=8)
        self.assertAlmostEqual(b["terms_deg"]["skew"], 0.864, places=6)
        self.assertAlmostEqual(b["phase_rss_deg"], math.hypot(0.864, 0.5), places=9)
        self.assertAlmostEqual(b["gain_loss_db"], T.coherent_gain_loss_db(b["phase_rss_deg"]))
        b2 = T.coherence_budget(1e9, pos_sigma_m=T.position_tolerance_m(1e9, 1.0))
        self.assertAlmostEqual(b2["terms_deg"]["position"], 1.0, places=9)

    def test_phase_budget_report(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = C.default_config(base_dir=td, geometry={"preset": "ula", "num_elements": 8},
                                   devices={"sim0": {"family": "sim", "rx_channels": "0..7"}})
            import active_geometry as G
            rep = T.phase_budget_report(cfg, G.geometry_from_config(cfg))
            for key in ("terms_deg", "phase_rss_deg", "null_floor_db", "ula_bearing_sigma_deg",
                        "verdict_doa_ok", "verdict_null_30db_ok", "cal_sigma_deg", "assumptions"):
                self.assertIn(key, rep)
            self.assertEqual(rep["f_hz"], 915e6 + 1e6)
            self.assertTrue(rep["verdict_doa_ok"])
            bad = T.phase_budget_report(cfg, None, cal_sigma_deg=6.0)
            self.assertFalse(bad["verdict_doa_ok"])
            self.assertFalse(bad["verdict_null_30db_ok"])


if __name__ == "__main__":
    unittest.main()
