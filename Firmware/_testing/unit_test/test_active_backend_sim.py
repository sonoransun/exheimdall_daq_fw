"""
    Unit tests for the active backend interface (Firmware/_active/active_backend.py)
    and the deterministic RF simulator (Firmware/_active/active_backend_sim.py).

    Hardware-free and deterministic: seeded random streams, the simulator's
    virtual clock (no wall-clock sleeps), temp directories for every state dir.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import copy
import logging
import math
import os
import sys
import tempfile
import threading
import unittest
from os.path import dirname, join, realpath

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_backend as AB  # noqa: E402
import active_backend_sim as SB  # noqa: E402
import active_config as C  # noqa: E402
import active_frontend as FE  # noqa: E402
import active_geometry as G  # noqa: E402
import active_pattern as P  # noqa: E402
import active_rf_control as RF  # noqa: E402
import active_timing as T  # noqa: E402
import active_tx_safety as TS  # noqa: E402
from active_common import (BackendError, BackendUnavailable, ConfigError, DevTime, EventLog,  # noqa: E402
                           HardwareFault, InterlockTrip, SafetyRefusal)

logging.getLogger("antenna_profile").setLevel(logging.ERROR)
logging.getLogger("heimdall.active").setLevel(logging.CRITICAL + 10)

FC = 915e6
FS = 2e6


def merge(base, over):
    out = copy.deepcopy(base)
    for sec, keys in over.items():
        if sec == "devices":
            out["devices"] = copy.deepcopy(keys)
            continue
        out.setdefault(sec, {}).update(keys)
    return out


RX8 = {"geometry": {"preset": "ula", "num_elements": 8},
       "devices": {"sim0": {"family": "sim", "rx_channels": "0..7"}}}
TX4 = {"active": {"en_active": 1, "mode": "trx", "operator": "tester"},
       "devices": {"sim0": {"family": "sim", "rx_channels": "0..3", "tx_channels": "0..3"}},
       "geometry": {"preset": "ula", "num_elements": 4},
       "tx": {"en_tx": 1, "allowed_modes": "dummy_load", "allow_bands": "902e6:928e6",
              "dummy_load_rating_w": 1}}
EXT = {"clock_source": "external", "time_source": "external"}
TWO_DEV = {"devices": {"a": {"family": "sim", "rx_channels": "0..3", "group": "ga", "role": "master",
                             "clock_source": "external", "time_source": "external"},
                       "b": {"family": "sim", "rx_channels": "0..3", "group": "gb",
                             "clock_source": "external", "time_source": "external"}},
           "geometry": {"preset": "ula", "num_elements": 8}}


def cfg_of(td, base=None, **over):
    return C.default_config(base_dir=td, **merge(base or RX8, over))


def up(cfg, sync=True, tune=True, **kw):
    """An open simulator, synchronized (SyncProcedure) and tuned to the configured fc."""
    sim = SB.SimBackend(cfg, kw.pop("event_log", EventLog()), **kw)
    sim.open()
    plan = T.plan_coherence(cfg)
    if sync:
        T.SyncProcedure(sim, plan, cfg.timing).run()
    if tune:
        T.retune(sim, plan, cfg.rf.center_freq_hz)
    return sim, plan


def start_in(sim, dt=0.05, device=None):
    return sim.time_now(device).add_seconds(dt)


def capture(sim, n=4096, dt=0.05):
    return sim.rx_capture(n, start_in(sim, dt))


def make_safety(cfg, sim, td, ev=None):
    ev = ev or sim.event_log
    s = TS.TxSafetyController(cfg, sim.frontend, sim.geometry, ev, clock=sim.mono, wallclock=sim.wallclock,
                              sleep=sim.sleep, state_dir=td, sync_ok=lambda: True, ref_locked=lambda: True,
                              coherence_class=lambda: 2, time_now=sim.time_now,
                              tx_freq=lambda: sim.tuned_freq("tx"), tx_rate=lambda: sim.rate("tx"))
    rf = RF.RfControl(cfg.rf_control, cfg.num_tx(), sim, ev, sleep=sim.sleep)
    s.attach(sim, rf)
    sim.attach_safety(s)
    return s, rf


def arm(s, mode="dummy_load"):
    rec = s.arm(mode, FC, TS.ack_phrase(mode, FC), "tester")
    s.heartbeat()
    return rec


def burst(k=4, n=2000, amp=0.3):
    t = np.arange(n)
    x = amp * np.exp(2j * np.pi * 0.05 * t) * np.hanning(n)
    return x[None, :].repeat(k, axis=0)


def permit_for(s, sim, x, chains=None, gains=None, dt=0.1):
    chains = list(range(x.shape[0])) if chains is None else chains
    gains = [0.0] * len(chains) if gains is None else gains
    return s.authorize_burst(x, chains, gains, FS, start=start_in(sim, dt))


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = self._td.name

    def tearDown(self):
        self._td.cleanup()


# ---------------------------------------------------------------------------
class TestInterface(Base):
    def test_coherence_tracker(self):
        ev = EventLog()
        tr = AB.CoherenceTracker(ev)
        self.assertEqual(tr.generation, 0)
        self.assertEqual(tr.bump("sync"), 1)
        self.assertEqual(tr.bump("tune"), 2)
        snap = tr.snapshot()
        self.assertEqual(snap["generation"], 2)
        self.assertEqual(snap["last_reason"], "tune")
        self.assertEqual(ev.recent(5, "active_coherence_bump")[-1]["payload"]["generation"], 2)

    def test_tune_bump_rule(self):
        def rep(state, timed, band=False):
            return AB.TuneReport("rx", (), FC, [], [], timed, state, [], 0, band)
        self.assertFalse(AB.ActiveBackend.tune_needs_bump(rep("repeatable", True)))
        self.assertFalse(AB.ActiveBackend.tune_needs_bump(rep("shared_lo", True)))
        self.assertFalse(AB.ActiveBackend.tune_needs_bump(rep("deterministic", True)))
        self.assertTrue(AB.ActiveBackend.tune_needs_bump(rep("repeatable", False)))
        self.assertTrue(AB.ActiveBackend.tune_needs_bump(rep("random", True)))
        self.assertTrue(AB.ActiveBackend.tune_needs_bump(rep("ambiguous_180", True)))
        self.assertTrue(AB.ActiveBackend.tune_needs_bump(rep("repeatable", True, band=True)))

    def test_abstract(self):
        with self.assertRaises(TypeError):
            AB.ActiveBackend(cfg_of(self.td))
        self.assertEqual(AB.PHASES, ("CLOSED", "OPEN", "REFS_SET", "TIME_SET", "TUNED", "STREAMING"))

    def test_phase_order(self):
        cfg = cfg_of(self.td, TWO_DEV)
        sim = SB.SimBackend(cfg)
        self.assertEqual(sim.phase, "CLOSED")
        with self.assertRaises(BackendError):
            sim.tune("rx", FC)
        sim.open()
        self.assertEqual(sim.phase, "OPEN")
        with self.assertRaises(BackendError):                       # tuning before REFS_SET
            sim.tune("rx", FC)
        with self.assertRaises(BackendError):
            sim.set_time_next_pps(DevTime(0, 0, 100_000_000))
        for d in ("a", "b"):
            sim.set_clock_source("external", device=d)
            sim.set_time_source("external", device=d)
        self.assertEqual(sim.phase, "REFS_SET")
        sim.tune("rx", FC)
        with self.assertRaises(BackendError) as cm:                   # stream before TIME_SET (2 devices)
            sim.rx_capture(256, start_in(sim))
        self.assertIn("TIME_SET", str(cm.exception))
        with self.assertRaises(BackendError):
            sim.rx_start(start_in(sim))
        plan = T.plan_coherence(cfg)
        T.SyncProcedure(sim, plan, cfg.timing).run()
        T.retune(sim, plan, FC)
        self.assertEqual(sim.phase, "TUNED")
        sim.rx_capture(256, start_in(sim))
        sim.rx_start(start_in(sim))
        self.assertEqual(sim.phase, "STREAMING")
        with self.assertRaises(BackendError):
            sim.rx_start(start_in(sim))
        g0 = sim.coherence.generation
        sim.set_clock_source("external", device="a")                  # after a time set
        self.assertEqual(sim.phase, "REFS_SET")
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertEqual(sim.coherence.snapshot()["last_reason"], "clock_source")
        with self.assertRaises(BackendError):
            sim.rx_read(16)                                            # the stream was stopped
        with self.assertRaises(BackendError):
            sim.rx_capture(256, start_in(sim))
        T.SyncProcedure(sim, plan, cfg.timing).run()
        g1 = sim.coherence.generation
        sim.set_master_clock_rate(100e6)
        self.assertEqual(sim.phase, "REFS_SET")
        self.assertEqual(sim.coherence.generation, g1 + 1)
        with self.assertRaises(BackendError):
            sim.set_master_clock_rate(99e6)                            # not a sim MCR choice
        with self.assertRaises(BackendError):
            sim.rx_capture(256, None)

    def test_single_device_streams_without_time_set(self):
        sim, _ = up(cfg_of(self.td), sync=True)
        self.assertFalse(sim.time_required())
        self.assertEqual(sim.phase, "TUNED")
        cap = capture(sim, 512)
        self.assertEqual(cap.data.shape, (8, 512))
        self.assertEqual(cap.data.dtype, np.complex64)

    def test_open_close_safe_state(self):
        cfg = cfg_of(self.td, TX4, rf_control={"pa_enable_line": "sim:pa", "tr_switch_line": "sim:tr"})
        sim = SB.SimBackend(cfg)
        sim._tx_gain = [20.0] * 4
        sim._gpio[("sim", "pa")] = True
        sim.open()
        names = [c[0] for c in sim.calls]
        self.assertEqual(names[:2], ["open", "set_tx_gain_min"])
        self.assertEqual(sim.rx_gain_db(), [30.0] * 4)
        self.assertEqual(sim._tx_gain, [0.0] * 4)
        self.assertFalse(sim._gpio[("sim", "pa")])
        sim._tx_gain = [10.0] * 4
        sim._gpio[("sim", "pa")] = True
        sim.close()
        names = [c[0] for c in sim.calls]
        self.assertLess(names.index("safe_state", names.index("close")), len(names))
        self.assertEqual(sim._tx_gain, [0.0] * 4)
        self.assertFalse(sim._gpio[("sim", "pa")])
        self.assertEqual(sim.phase, "CLOSED")
        sim.close()                                                    # idempotent

    def test_safe_state_never_raises(self):
        sim, _ = up(cfg_of(self.td, TX4, rf_control={"pa_enable_line": "sim:pa"}))

        def boom(*a, **k):
            raise RuntimeError("device gone")
        sim._do_tx_abort = boom
        sim._do_set_tx_gain_min = boom
        steps = sim.safe_state()
        self.assertEqual([s[0] for s in steps][:2], ["tx_abort", "tx_gain_min"])
        self.assertFalse(steps[0][1])
        self.assertFalse(steps[1][1])
        self.assertTrue(steps[2][1])                                   # the PA line still went off
        sim.tx_abort()
        sim.set_tx_gain_min()
        sim.close()

    def test_tx_refused_without_safety_and_no_streamer_when_disabled(self):
        cfg = cfg_of(self.td, TX4, tx={"en_tx": 0})
        sim, _ = up(cfg)
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tx_burst(object())
        self.assertEqual(cm.exception.code, "TX_DISABLED")
        s, _rf = make_safety(cfg, sim, self.td)
        self.assertEqual(s.state, TS.TxState.DISABLED)
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tx_burst(object())
        self.assertEqual(cm.exception.code, "TX_DISABLED")
        self.assertNotIn("create_tx_streamer", [c[0] for c in sim.calls])
        with self.assertRaises(BackendError):                          # defence in depth
            sim._do_create_tx_streamer()

    def test_tx_burst_invalid_permit_trips(self):
        cfg = cfg_of(self.td, TX4)
        sim, _ = up(cfg)
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        p = permit_for(s, sim, burst())
        forged = TS.TxPermit(**{f: getattr(p, f) for f in p.__dataclass_fields__})
        with self.assertRaises(InterlockTrip) as cm:
            sim.tx_burst(forged)
        self.assertEqual(cm.exception.code, "PERMIT_FORGED")
        self.assertEqual(s.state, TS.TxState.FAULT)
        self.assertIn("create_tx_streamer", [c[0] for c in sim.calls])
        self.assertEqual(sim._tx_gain, [0.0] * 4)

    def test_tx_burst_safe_path_and_reuse(self):
        cfg = cfg_of(self.td, TX4)
        sim, _ = up(cfg)
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        p = permit_for(s, sim, burst())
        rf.sequence_to_tx(p)
        t_before = sim.mono()
        res = sim.tx_burst(p)
        self.assertTrue(res.ok and res.acked)
        self.assertEqual(res.sent_samples, 2000)
        self.assertEqual([e.kind for e in res.events], ["burst_ack"])
        self.assertGreater(sim.mono(), t_before + 0.1)                  # virtual time ran to the burst end
        self.assertEqual(s.state, TS.TxState.ARMED)
        self.assertEqual(sim.damage_log, [])
        self.assertEqual(sim.last_tx_output()[4], "dummy_load")
        with self.assertRaises(InterlockTrip) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "PERMIT_REUSED")

    def test_hardware_burst_needs_start(self):
        cfg = cfg_of(self.td, TX4)
        sim, _ = up(cfg)
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        p = permit_for(s, sim, burst())
        sim.is_simulation = False                                      # behave like hardware
        try:
            object.__setattr__(p, "start", None)
            with self.assertRaises(SafetyRefusal) as cm:
                sim.tx_burst(p)
            self.assertEqual(cm.exception.code, "START_TIME")
        finally:
            sim.is_simulation = True

    def test_make_backend(self):
        sim = AB.make_backend(cfg_of(self.td))
        self.assertIsInstance(sim, SB.SimBackend)
        self.assertTrue(sim.hardware_id.startswith("SIM-"))
        hw = {"active": {"backend": "uhd"}, "devices": {"b0": {"family": "b210", "rx_channels": "0..1"}},
              "geometry": {"preset": "ula", "num_elements": 2}}
        with self.assertRaises(BackendUnavailable) as cm:
            AB.make_backend(cfg_of(self.td, hw))
        self.assertTrue(cm.exception.hint)
        ev = EventLog()
        fb = AB.make_backend(cfg_of(self.td, hw, active={"allow_sim_fallback": 1}), ev)
        self.assertIsInstance(fb, SB.SimBackend)
        self.assertTrue(fb.fallback)
        self.assertTrue(fb.status()["simulation"])
        self.assertTrue(fb.hardware_id.startswith("SIM-"))
        self.assertEqual(ev.recent(5, "active_fallback")[-1]["payload"]["requested"], "uhd")
        hw_tx = merge(hw, {"active": {"allow_sim_fallback": 1, "mode": "trx", "operator": "x"},
                           "devices": {"b0": {"family": "b210", "rx_channels": "0..1", "tx_channels": "0"}},
                           "tx": {"en_tx": 1, "allow_bands": "902e6:928e6"}})
        with self.assertRaises(ConfigError):
            AB.make_backend(cfg_of(self.td, hw_tx))
        bad = {"devices": {"x": {"family": "sim", "rx_channels": "0..3"}},
               "rf": {"center_freq_hz": 50e9}}
        with self.assertRaises(ConfigError):
            AB.make_backend(cfg_of(self.td, bad))


# ---------------------------------------------------------------------------
class TestDeterminismAndScene(Base):
    def test_determinism(self):
        cfg = cfg_of(self.td)
        a, _ = up(cfg)
        b, _ = up(cfg)
        c, _ = up(cfg, seed=777)
        xa, xb, xc = capture(a).data, capture(b).data, capture(c).data
        self.assertTrue(np.array_equal(xa, xb))
        self.assertFalse(np.array_equal(xa, xc))
        self.assertEqual([x[0] for x in a.calls], [x[0] for x in b.calls])
        self.assertTrue(np.array_equal(capture(a).data, capture(b).data))

    def test_impairment_streams_are_independent(self):
        a, _ = up(cfg_of(self.td, sim={"coupling_db": -99}))
        b, _ = up(cfg_of(self.td, sim={"coupling_db": -15}))
        ta, tb = a.truth, b.truth
        np.testing.assert_array_equal(ta.lo_phase_rx, tb.lo_phase_rx)
        np.testing.assert_array_equal(ta.rx_delay_samples, tb.rx_delay_samples)
        np.testing.assert_array_equal(ta.rx_gain, tb.rx_gain)
        self.assertFalse(np.array_equal(capture(a).data, capture(b).data))
        # a different spread changes only the delays
        c, _ = up(cfg_of(self.td, sim={"rx_delay_spread_samples": 1.0}))
        np.testing.assert_array_equal(ta.lo_phase_rx, c.truth.lo_phase_rx)
        self.assertFalse(np.array_equal(ta.rx_delay_samples, c.truth.rx_delay_samples))

    def _amplitudes(self, sim, n=4096):
        x = capture(sim, n).data.astype(complex)
        return x.mean(axis=1) / sim.true_rx_response()

    def test_far_field_convention(self):
        cfg = cfg_of(self.td, sim={"emitters": "30:10:0:-40:tone", "noise_dbfs": -90}, timing=EXT)
        sim, _ = up(cfg)
        g = self._amplitudes(sim)
        a = P.steering_ff(sim.geometry.positions("rx"), G.direction_vector(30, 10), FC)
        err = np.degrees(np.angle((g / g[0]) / (a / a[0])))
        self.assertLess(np.max(np.abs(err)), 0.05)
        self.assertLess(np.max(np.abs(np.abs(g) / np.abs(g[0]) - 1)), 1e-3)
        # level: -40 dBm + 30 dB gain - 0 dBm full scale = -10 dBFS (|g| includes the amplitude only)
        self.assertAlmostEqual(20 * math.log10(abs(g[0])), -10.0, delta=0.05)

    def test_near_field_convention(self):
        cfg = cfg_of(self.td, sim={"emitters": "60:0:2.5:-40:tone", "noise_dbfs": -90}, timing=EXT)
        sim, _ = up(cfg)
        g = self._amplitudes(sim)
        pos = sim.geometry.positions("rx")
        a = P.steering_nf(pos, 2.5 * G.direction_vector(60, 0), FC)
        err = np.degrees(np.angle((g / g[0]) / (a / a[0])))
        self.assertLess(np.max(np.abs(err)), 0.05)
        np.testing.assert_allclose(np.abs(g) / abs(g[0]), np.abs(a) / abs(a[0]), rtol=1e-3)

    def test_true_delay_wideband(self):
        cfg = cfg_of(self.td, sim={"emitters": "40:0:0:-50:noise", "rx_delay_spread_samples": 0.0,
                                   "noise_dbfs": -100}, rf={"sample_rate_hz": 50e6}, timing=EXT)
        sim, _ = up(cfg)
        x = capture(sim, 8192, dt=0.01).data.astype(complex) / sim.true_rx_response()[:, None]
        pos = sim.geometry.positions("rx")
        u = G.direction_vector(40, 0)
        tau = pos @ u / C.ac.C0                     # arrival advance (s)
        y = np.fft.ifft(np.fft.fft(x, axis=1) * np.exp(2j * np.pi * np.fft.fftfreq(8192)[None, :] * (-tau * 50e6)[:, None]
                                                       ), axis=1)
        y *= np.exp(-2j * np.pi * FC * tau)[:, None]
        mid = slice(200, -200)
        corr = abs(np.vdot(y[0, mid], y[-1, mid])) / (np.linalg.norm(y[0, mid]) * np.linalg.norm(y[-1, mid]))
        self.assertGreater(corr, 0.999)

    def test_iq_conjugate_truth_and_host_undo(self):
        base = {"emitters": "30:0:0:-40:tone", "noise_dbfs": -90}
        ref, _ = up(cfg_of(self.td, sim=base, timing=EXT))
        inv, _ = up(cfg_of(self.td, sim=dict(base, iq_conjugate_devices="sim0"), timing=EXT))
        both, _ = up(cfg_of(self.td, sim=dict(base, iq_conjugate_devices="sim0"), timing=EXT,
                            devices={"sim0": {"family": "sim", "rx_channels": "0..7", "iq_conjugate": 1}}))
        xr, xi, xb = capture(ref).data, capture(inv).data, capture(both).data
        self.assertTrue(inv.truth.iq_conjugate["sim0"])
        np.testing.assert_allclose(xi, np.conj(xr), atol=1e-6)
        np.testing.assert_allclose(xb, xr, atol=1e-6)

    def test_levels_noise_and_overdrive(self):
        quiet, _ = up(cfg_of(self.td, sim={"emitters": "", "noise_dbfs": -40}))
        x = capture(quiet, 16384).data
        p = 10 * np.log10(np.mean(np.abs(x) ** 2, axis=1))
        np.testing.assert_allclose(p, -40.0, atol=0.2)
        self.assertEqual(capture(quiet).overdrive_mask, 0)
        loud, _ = up(cfg_of(self.td, sim={"emitters": "0:0:0:-20:tone"}, devices={
            "sim0": {"family": "sim", "rx_channels": "0..7"}}))
        cap = capture(loud)
        self.assertEqual(cap.overdrive_mask, 0xFF)
        self.assertLessEqual(float(np.max(np.abs(cap.data.real))), 1.0)

    def test_coupling_toeplitz(self):
        sim, _ = up(cfg_of(self.td, sim={"coupling_db": -20}))
        C_ = sim._coupling_matrix(FC)
        self.assertAlmostEqual(abs(C_[0, 1]), 0.1, places=9)
        self.assertEqual(C_[0, 2], 0)
        self.assertAlmostEqual(abs(C_[3, 2]), abs(C_[4, 5]))


# ---------------------------------------------------------------------------
class TestLoPhase(Base):
    def _sim(self, emulate, devices=None):
        dev = devices or {"sim0": {"family": "sim", "rx_channels": "0..3"}}
        cfg = cfg_of(self.td, {"devices": dev, "geometry": {"preset": "ula", "num_elements": 4}},
                     sim={"emulate_family": emulate})
        sim = SB.SimBackend(cfg)
        sim.open()
        sim.set_clock_source("internal")
        sim.set_time_source("internal")
        return sim

    def _timed_tune(self, sim, f):
        t = sim.time_now().add_seconds(0.1)
        sim.set_command_time(t)
        rep = sim.tune("rx", f, at=t)
        sim.clear_command_time()
        return rep

    def test_x310_ubx_timed_vs_untimed(self):
        sim = self._sim("x310_ubx")
        sim.tune("rx", FC)
        p1 = sim.truth.lo_phase_rx.copy()
        rep = sim.tune("rx", FC)
        self.assertFalse(rep.timed)
        self.assertEqual(rep.phase_state, "random")
        p2 = sim.truth.lo_phase_rx.copy()
        self.assertGreater(np.max(np.abs(np.angle(np.exp(1j * (p2 - p1))))), 0.1)       # redrawn
        rep = self._timed_tune(sim, FC)
        self.assertTrue(rep.timed)
        self.assertEqual(rep.phase_state, "repeatable")
        q1 = sim.truth.lo_phase_rx.copy()
        self._timed_tune(sim, 2.4e9)
        self._timed_tune(sim, FC)
        np.testing.assert_allclose(sim.truth.lo_phase_rx, q1)                            # stable
        g = sim.coherence.generation
        self._timed_tune(sim, FC)
        self.assertEqual(sim.coherence.generation, g)
        sim.tune("rx", FC)
        self.assertEqual(sim.coherence.generation, g + 1)

    def test_n310_external_lo_180(self):
        dev = {"sim0": {"family": "sim", "rx_channels": "0..3", "lo_source": "external"}}
        sim = self._sim("n310", dev)
        for r in sim.rx_refs():
            sim.set_lo_source("rx", "external", r)
        base = None
        seen = set()
        for _ in range(16):
            self._timed_tune(sim, FC)
            ph = sim.truth.lo_phase_rx.copy()
            base = ph if base is None else base
            d = np.degrees(np.angle(np.exp(1j * (ph - base))))
            for v in d:
                self.assertTrue(min(abs(v), abs(abs(v) - 180)) < 1e-6, v)
                seen.add(int(round(abs(v))))
        self.assertEqual(seen, {0, 180})
        # without the runtime LO import the phase is plainly random
        sim2 = self._sim("n310", dev)
        sim2.tune("rx", FC)
        a = sim2.truth.lo_phase_rx.copy()
        sim2.tune("rx", FC)
        d = np.degrees(np.angle(np.exp(1j * (sim2.truth.lo_phase_rx - a))))
        self.assertTrue(any(min(abs(v), abs(abs(v) - 180)) > 1 for v in d))

    def test_crimson_ignores_timed_tune(self):
        sim = self._sim("crimson_tng")
        self.assertEqual(sim.flavour, "pervices")
        rep = self._timed_tune(sim, FC)
        self.assertFalse(rep.timed)
        p1 = sim.truth.lo_phase_rx.copy()
        self._timed_tune(sim, FC)
        self.assertGreater(np.max(np.abs(np.angle(np.exp(1j * (sim.truth.lo_phase_rx - p1))))), 0.1)
        rep = sim.tune("rx", 100e6)
        self.assertTrue(rep.band_change)                     # 120 MHz analog band edge

    def test_shared_lo_n321_star(self):
        dev = {"m": {"family": "sim", "rx_channels": "0..1", "lo_export": 1, "role": "master", "group": "s",
                     "clock_source": "external", "time_source": "external"},
               "s": {"family": "sim", "rx_channels": "0..1", "lo_source": "external", "group": "s",
                     "clock_source": "external", "time_source": "external"}}
        cfg = cfg_of(self.td, {"devices": dev, "geometry": {"preset": "ula", "num_elements": 4}},
                     sim={"emulate_family": "n321"}, timing={"lo_plan": "n321_star"})
        sim = SB.SimBackend(cfg)
        sim.open()
        plan = T.plan_coherence(cfg)
        T.SyncProcedure(sim, plan, cfg.timing).run()
        sim.set_lo_export("rx", True, sim.rx_refs()[0])
        for r in sim.rx_refs()[2:]:
            sim.set_lo_source("rx", "external", r)
        sim.tune("rx", FC)
        a = sim.truth.lo_phase_rx.copy()
        sim.tune("rx", FC)
        b = sim.truth.lo_phase_rx.copy()
        rel_a, rel_b = np.angle(np.exp(1j * (a - a[0]))), np.angle(np.exp(1j * (b - b[0])))
        np.testing.assert_allclose(rel_a, rel_b, atol=1e-9)                     # relative phases stable
        self.assertGreater(abs(np.angle(np.exp(1j * (b[0] - a[0])))), 1e-3)     # common phase moved
        self.assertEqual(sim.lo_names("rx", sim.rx_refs()[0]), ["lo1"])

    def test_lo_unlock_fault(self):
        sim = self._sim("x310_ubx")
        sim.tune("rx", FC)
        p = sim.truth.lo_phase_rx.copy()
        sim.inject_fault("lo_unlock", delay_s=0.5)
        self.assertTrue(sim.sensors("sim0")["lo_locked"])
        sim.advance(0.6)
        self.assertFalse(sim.sensors("sim0")["lo_locked"])
        self.assertGreater(np.min(np.abs(np.angle(np.exp(1j * (sim.truth.lo_phase_rx - p))))), math.radians(50))
        self.assertIn("lo_unlock", [e.kind for e in sim.async_events()])
        sim.clear_faults()
        self.assertTrue(sim.sensors("sim0")["lo_locked"])


# ---------------------------------------------------------------------------
class TestClocks(Base):
    def test_virtual_time(self):
        sim = SB.SimBackend(cfg_of(self.td))
        sim.open()
        t0 = sim.time_now()
        self.assertEqual(t0.tick_rate, 100_000_000)
        m0 = sim.mono()
        sim.sleep(0.25)
        self.assertAlmostEqual(sim.mono() - m0, 0.25)
        self.assertEqual(sim.calls[-1][0], "sleep")
        self.assertGreater(sim.time_now(), t0)
        self.assertAlmostEqual(sim.wallclock() - sim.mono(), SB.SIM_EPOCH_UNIX)
        with self.assertRaises(ValueError):
            sim.advance(-1)

    def test_next_pps_latch(self):
        sim = SB.SimBackend(cfg_of(self.td, TWO_DEV))
        sim.open()
        for d in ("a", "b"):
            sim.set_clock_source("external", device=d)
            sim.set_time_source("external", device=d)
        tick = 100_000_000
        sim.set_time_next_pps(DevTime(7, 0, tick))
        frac = sim.mono() % 1.0
        sim.advance(1.0 - frac + 0.25)                  # 0.25 s after the edge
        for d in ("a", "b"):
            self.assertEqual(sim.time_now(d), DevTime(7, 25_000_000, tick))
        self.assertTrue(sim.time_synchronized())
        sim.advance(1.0)
        self.assertEqual(sim.time_last_pps("a"), DevTime(8, 0, tick))

    def test_pps_skew(self):
        sim, _ = up(cfg_of(self.td, TWO_DEV, sim={"pps_skew_ticks": 3}, timing={"max_pps_skew_ticks": 5}))
        self.assertEqual(sim.truth.pps_skew_ticks, {"a": 0, "b": 3})
        self.assertEqual(sim.time_last_pps("b").diff_ticks(sim.time_last_pps("a")), -3)
        self.assertEqual(sim.time_now("b").diff_ticks(sim.time_now("a")), -3)

    def test_set_time_now_multi_device_offset(self):
        sim = SB.SimBackend(cfg_of(self.td, TWO_DEV))
        sim.open()
        for d in ("a", "b"):
            sim.set_clock_source("external", device=d)
            sim.set_time_source("external", device=d)
        sim.set_time_now(DevTime(100, 0, 100_000_000))
        sim.advance(0.5)
        d = float(sim.time_now("a").diff_seconds(sim.time_now("b")))
        self.assertGreater(abs(d), 1e-6)
        self.assertLessEqual(abs(d), SB.SET_TIME_NOW_LATENCY_S)
        # the usual per-mboard loop (one call per device) is the same hazard
        for dv in ("a", "b"):
            sim.set_time_now(DevTime(200, 0, 100_000_000), device=dv)
        sim.advance(0.5)
        d2 = float(sim.time_now("a").diff_seconds(sim.time_now("b")))
        self.assertGreater(abs(d2), 1e-6)
        self.assertLessEqual(abs(d2), SB.SET_TIME_NOW_LATENCY_S)
        # one device: exact
        one = SB.SimBackend(cfg_of(self.td))
        one.open()
        one.set_clock_source("internal")
        one.set_time_source("internal")
        one.set_time_now(DevTime(5, 0, 100_000_000))
        self.assertEqual(one.time_now(), DevTime(5, 0, 100_000_000))

    def test_unshared_reference_phase_ramp(self):
        dev = copy.deepcopy(TWO_DEV["devices"])
        for d in dev.values():
            d["clock_source"] = "internal"
            d["time_source"] = "internal"
        cfg = cfg_of(self.td, {"devices": dev, "geometry": {"preset": "ula", "num_elements": 8}},
                     sim={"emitters": "0:0:0:-40:tone:100e3", "noise_dbfs": -90})
        sim = SB.SimBackend(cfg)
        sim.open()
        for d in ("a", "b"):
            sim.set_clock_source("internal", device=d)
            sim.set_time_source("internal", device=d)
        sim.set_time_next_pps(DevTime(0, 0, 100_000_000))
        sim.advance(1.1)
        sim.tune("rx", FC)
        x = capture(sim, 16384).data.astype(complex)
        z = x[4] * np.conj(x[0])                                   # device b ch0 vs device a ch0
        f_meas = np.angle(np.sum(z[1:] * np.conj(z[:-1]))) * FS / (2 * np.pi)
        ppm = sim.truth.ref_ppm
        f_true = -FC * (ppm["b"] - ppm["a"]) * 1e-6
        self.assertGreater(abs(f_true), 50.0)
        self.assertAlmostEqual(f_meas, f_true, delta=abs(f_true) * 0.01)
        # a shared external reference: no ramp
        sim2, _ = up(cfg_of(self.td, TWO_DEV, sim={"emitters": "0:0:0:-40:tone:100e3", "noise_dbfs": -90}))
        x = capture(sim2, 16384).data.astype(complex)
        z = x[4] * np.conj(x[0])
        self.assertLess(abs(np.angle(np.sum(z[1:] * np.conj(z[:-1]))) * FS / (2 * np.pi)), 0.01)

    def test_ref_lock_settle_and_unlock(self):
        sim = SB.SimBackend(cfg_of(self.td))
        sim.open()
        sim.set_clock_source("external")
        self.assertFalse(sim.ref_locked("sim0"))
        sim.advance(0.31)
        self.assertTrue(sim.ref_locked("sim0"))
        self.assertEqual(sim.truth.ref_ppm["sim0"], 0.0)
        sim.inject_fault("ref_unlock")
        self.assertFalse(sim.ref_locked("sim0"))
        self.assertNotEqual(sim.truth.ref_ppm["sim0"], 0.0)
        self.assertIn("ref_unlock", [e.kind for e in sim.async_events()])
        sim.clear_faults()
        self.assertTrue(sim.ref_locked("sim0"))
        with self.assertRaises(BackendError):
            sim.set_clock_source("rubidium")

    def test_pps_missing(self):
        sim = SB.SimBackend(cfg_of(self.td, TWO_DEV))
        sim.open()
        for d in ("a", "b"):
            sim.set_clock_source("external", device=d)
            sim.set_time_source("external", device=d)
        sim.inject_fault("pps_missing")
        last = sim.time_last_pps("a")
        sim.advance(3.0)
        self.assertEqual(sim.time_last_pps("a"), last)
        with self.assertRaises(HardwareFault):
            sim.set_time_unknown_pps(DevTime(0, 0, 100_000_000))
        sim.clear_faults()
        sim.advance(1.0)
        self.assertNotEqual(sim.time_last_pps("a"), last)
        sim.set_time_unknown_pps(DevTime(0, 0, 100_000_000))
        self.assertTrue(sim.time_synchronized())


# ---------------------------------------------------------------------------
class TestRxStreaming(Base):
    def test_contiguous_reads_and_overflow(self):
        sim, _ = up(cfg_of(self.td))
        t0 = start_in(sim, 0.1)
        sim.rx_start(t0)
        c1 = sim.rx_read(1000)
        c2 = sim.rx_read(1000)
        self.assertEqual(c1.start, t0)
        self.assertEqual(c2.start.sample_offset(t0, FS), 1000)
        self.assertFalse(c1.overflow or c2.overflow)
        sim.inject_fault("overflow")
        c3 = sim.rx_read(1000)
        self.assertTrue(c3.overflow)
        self.assertEqual(c3.dropped, 1000)
        self.assertEqual(c3.start.sample_offset(t0, FS), 3000)
        self.assertIn("overflow", [e.kind for e in sim.async_events()])
        sim.advance(5.0)                                    # the reader falls behind the host buffer
        c4 = sim.rx_read(1000)
        self.assertTrue(c4.overflow)
        self.assertGreater(c4.dropped, 0)
        self.assertFalse(sim.rx_read(1000).overflow)
        sim.rx_stop()
        self.assertEqual(sim.phase, "TUNED")


# ---------------------------------------------------------------------------
class TestCalPaths(Base):
    def test_injection_routing_and_paths(self):
        path = os.path.join(self.td, "inj.csv")
        with open(path, "w") as fh:
            fh.write("chan,freq_hz,mag_db,phase_deg,delay_s\n")
            for c in range(8):
                for f in (900e6, 930e6):
                    fh.write("%d,%g,%g,%g,%g\n" % (c, f, -0.5 * c, 10.0 * c, 0.0))
        cfg = cfg_of(self.td, sim={"rx_delay_spread_samples": 0.0, "noise_dbfs": -90,
                                   "injection_path_sigma_deg": 2.0},
                     calibration={"injection_paths_file": "inj.csv", "cal_waveform": "noise"},
                     rf_control={"cal_injection_line": "sim:inj"})
        sim, _ = up(cfg)
        rf = RF.RfControl(cfg.rf_control, 0, sim, sim.event_log, sleep=sim.sleep)
        rf.cal_input("injection")
        self.assertEqual(sim.cal_input(), "injection")
        x = capture(sim, 8192).data.astype(complex)
        est = (x @ np.conj(x[0])) / np.vdot(x[0], x[0])
        truth = sim.true_rx_response() * sim.truth.injection_paths
        rel = truth / truth[0]
        err = np.degrees(np.angle(est / rel))
        self.assertLess(np.max(np.abs(err)), 0.1)
        np.testing.assert_allclose(np.abs(est), np.abs(rel), rtol=2e-3)
        h = sim.truth.injection_paths
        self.assertAlmostEqual(20 * math.log10(abs(h[3])), -1.5, places=6)
        rf.cal_input("antenna")
        self.assertEqual(sim.cal_input(), "antenna")

    def test_loopback_routing_and_turnaround(self):
        cfg = cfg_of(self.td, TX4, sim={"rx_delay_spread_samples": 0.0, "tx_delay_spread_samples": 0.0,
                                        "emitters": "", "noise_dbfs": -100, "turnaround_samples": 37},
                     calibration={"loopback_rx_chan": 3}, tx={"loopback_path_loss_db": 10})
        sim, _ = up(cfg)
        sim.set_cal_input("loopback")
        self.assertEqual(sim.cal_input(), "loopback")
        n = 1021
        zc = np.exp(-1j * np.pi * np.arange(n) * (np.arange(n) + 1) / n) * 0.3
        t_start = start_in(sim, 0.05)
        # the realistic (UHD) pattern: start the RX stream BEFORE the burst, read after it
        sim.rx_start(t_start.add_ticks(-100_000))                        # 1 ms before the burst
        sim.physical_tx_unchecked(zc[None, :], start=t_start, chains=[1], tx_gains_db=10.0)
        cap = sim.rx_read(4096)
        sim.rx_stop()
        self.assertFalse(cap.overflow)
        x = cap.data.astype(complex)
        pw = np.mean(np.abs(x) ** 2, axis=1)
        self.assertGreater(10 * np.log10(pw[3] / pw[0]), 20.0)          # loopback >> leakage
        r = np.abs(np.correlate(x[3], zc, mode="valid"))
        lag = int(np.argmax(r))
        self.assertEqual(lag, 2000 + 37)                                 # 1 ms + turnaround
        self.assertEqual(sim.truth.turnaround_samples, 37)

    def test_cabled_wiring(self):
        cfg = cfg_of(self.td, TX4, sim={"tx_load": "cabled", "emitters": "", "noise_dbfs": -100},
                     tx={"cabled_rx_path_loss_db": 20})
        sim, _ = up(cfg)
        t_start = start_in(sim, 0.05)
        sim.rx_start(t_start)
        sim.physical_tx_unchecked(burst(1, 1000, 0.3), start=t_start, chains=[2], tx_gains_db=0.0)
        x = sim.rx_read(3000).data.astype(complex)
        sim.rx_stop()
        pw = 10 * np.log10(np.mean(np.abs(x) ** 2, axis=1))
        self.assertEqual(int(np.argmax(pw)), 2)
        self.assertGreater(pw[2] - np.max(np.delete(pw, 2)), 10.0)


# ---------------------------------------------------------------------------
class TestTxPhysics(Base):
    def test_rapp_pa_matches_model(self):
        cfg = cfg_of(self.td, TX4, frontend={"en_frontend": 1, "tx_chain": "pa:pa1:20:5:20:23::8"},
                     sim={"tx_gain_sigma_db": 0.0, "tx_delay_spread_samples": 0.0, "tx_load": "dummy_load"})
        sim, _ = up(cfg)
        sim.set_clock_source("external")
        sim.advance(0.5)
        T.retune(sim, T.plan_coherence(cfg), FC)
        n = 4000
        a = np.linspace(0.01, 0.99, n)
        sim.physical_tx_unchecked(a[None, :], chains=[0], tx_gains_db=31.75)
        chains, y, fs, fc, wiring = sim.last_tx_output()
        p_fs = sim.truth.p_fs_true(0, FC, 31.75)
        v = a * math.sqrt(10 ** (p_fs / 10)) * math.sqrt(1e-3) * abs(sim.true_tx_response()[0])
        pa = FE.RappPA(20.0, 23.0, 2.0, 8.0)
        ref = pa.apply(v) / math.sqrt(1e-3)
        sl = slice(100, n - 100)
        np.testing.assert_allclose(np.abs(y[0, sl]), np.abs(ref[sl]), rtol=2e-3)
        dphi = np.angle(y[0, sl] * np.conj(ref[sl]))
        self.assertLess(np.std(dphi), 1e-3)                                     # AM/PM reproduced
        pk_dbm = 10 * math.log10(np.max(np.abs(y[0]) ** 2))
        self.assertLess(pk_dbm, 23.0)                                           # never above Psat
        self.assertGreater(pk_dbm, pa.p1db_out_dbm() - 3.0)

    def test_true_power_below_conservative_bound(self):
        cfg = cfg_of(self.td, TX4)
        sim, _ = up(cfg)
        for g in (0.0, 10.0, 31.75):
            self.assertAlmostEqual(sim.truth.p_fs_true(1, FC, g),
                                   sim.frontend.tx[1].power_cal.conservative_dbm(1, g) - 3.0)

    def test_damage_log_unchecked_vs_safe_path(self):
        ev = EventLog()
        cfg = cfg_of(self.td, TX4, sim={"tx_load": "antenna"})
        sim, _ = up(cfg, event_log=ev)
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        p = permit_for(s, sim, burst(amp=0.3))
        rf.sequence_to_tx(p)
        sim.tx_burst(p)
        rf.sequence_to_rx()
        self.assertEqual(sim.damage_log, [])
        s.disarm()
        x = np.full((4, 1000), 0.99 + 0j)
        sim.physical_tx_unchecked(x, tx_gains_db=31.75)
        self.assertTrue(sim.damage_log)
        e = sim.damage_log[0]
        self.assertGreater(e["level_dbm"], e["limit_dbm"])
        self.assertEqual(e["source"], "tx")
        self.assertTrue(ev.recent(10, "active_sim_damage"))

    def test_dac_clip_only_through_unchecked(self):
        cfg = cfg_of(self.td, TX4, sim={"tx_gain_sigma_db": 0.0, "tx_delay_spread_samples": 0.0})
        sim, _ = up(cfg)
        sim.physical_tx_unchecked(np.full((1, 500), 1.5 + 1.5j), chains=[0], tx_gains_db=0.0)
        y = sim.last_tx_output()[1]
        lin = sim.physical_tx_unchecked(np.full((1, 500), 0.5 + 0.5j), chains=[0], tx_gains_db=0.0)
        self.assertTrue(lin.ok)
        y2 = sim.last_tx_output()[1]
        self.assertAlmostEqual(float(np.median(np.abs(y[0]))) / float(np.median(np.abs(y2[0]))), 2.0, places=3)

    def test_pa_line_off_gives_lo_leakage_only(self):
        cfg = cfg_of(self.td, TX4, rf_control={"pa_enable_line": "sim:pa"}, sim={"tx_gain_sigma_db": 0.0})
        sim, _ = up(cfg)
        sim.physical_tx_unchecked(burst(1, 500, 0.5), chains=[0], tx_gains_db=0.0)
        y = sim.last_tx_output()[1][0]
        p_fs = sim.truth.p_fs_true(0, FC, 0.0)
        np.testing.assert_allclose(10 * np.log10(np.abs(y) ** 2), p_fs - 50.0, atol=1e-9)
        sim.gpio_write(RF.parse_line("sim:pa"), True)
        sim.physical_tx_unchecked(burst(1, 500, 0.5), chains=[0], tx_gains_db=0.0)
        self.assertGreater(float(np.max(np.abs(sim.last_tx_output()[1][0]) ** 2)),
                           10 ** ((p_fs - 10) / 10))

    def test_atr_drives_pa_during_bursts_only(self):
        cfg = cfg_of(self.td, TX4, rf_control={"pa_enable_line": "sim0:FP0:0x1", "use_atr": 1})
        sim, _ = up(cfg)
        self.assertTrue(sim.gpio_atr("sim0", "FP0", 0x1, 0x0, 0x0, 0x1, 0x1))
        self.assertFalse(sim.gpio_atr("sim0", "GPIOX", 0x1, 0, 0, 1, 1))
        self.assertFalse(sim._pa_on(0, burst=False))
        self.assertTrue(sim._pa_on(0, burst=True))
        sim.gpio_write(RF.parse_line("sim0:FP0:0x1"), False)                    # software override
        self.assertFalse(sim._pa_on(0, burst=True))

    def test_late_and_hang(self):
        cfg = cfg_of(self.td, TX4)
        sim, _ = up(cfg)
        late = sim.physical_tx_unchecked(burst(1), start=sim.time_now(), chains=[0])
        self.assertEqual((late.late, late.ok, late.acked), (1, False, False))
        self.assertEqual(late.events[0].kind, "late")
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        sim.inject_fault("late")
        res = sim.tx_burst(permit_for(s, sim, burst()))
        self.assertEqual(res.late, 1)
        self.assertEqual(s.state, TS.TxState.ARMED)
        sim.inject_fault("tx_hang")
        t0 = sim.mono()
        res = sim.tx_burst(permit_for(s, sim, burst()), timeout_s=0.5)
        self.assertFalse(res.acked)
        self.assertGreater(sim.mono() - t0, 0.5)
        self.assertEqual(s.state, TS.TxState.FAULT)
        self.assertEqual(s.fault_code, "TX_HANG")

    def _underflow(self, flavour, count):
        cfg = cfg_of(self.td, TX4, sim={"tx_gain_sigma_db": 0.0, "tx_delay_spread_samples": 0.0})
        sim, _ = up(cfg, flavour=flavour)
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        sim.inject_fault("underflow_storm", count=count)
        x = burst(4, 4000, 0.3)
        res = sim.tx_burst(permit_for(s, sim, x))
        return sim, s, res, x

    def test_underflow_semantics_per_flavour(self):
        sim, s, res, x = self._underflow("ettus", 2)
        self.assertEqual(res.underflows, 2)
        self.assertFalse(res.content_corrupt)
        self.assertFalse(res.ok)
        y = sim.last_tx_output()[1][0]
        self.assertEqual(y.size, 4000)
        self.assertLess(float(np.min(np.abs(y[1300:1500]))), 1e-6)            # zero-filled gap
        self.assertEqual(s.state, TS.TxState.ARMED)
        sim, s, res, x = self._underflow("pervices", 2)
        self.assertTrue(res.content_corrupt)
        self.assertFalse(res.ok)
        self.assertEqual(sim.last_tx_output()[1].shape[1], 4000)               # same length: tail dropped
        sim, s, res, x = self._underflow("ettus", 6)                         # > underflow_trip_count (5)
        self.assertEqual(s.state, TS.TxState.FAULT)
        self.assertEqual(s.fault_code, "UNDERFLOW_STORM")

    def test_telemetry_and_temperature(self):
        cfg = cfg_of(self.td, TX4, sim={"heat_c_per_w": 5e6, "tx_gain_sigma_db": 0.0, "load_vswr": 2.0})
        sim, _ = up(cfg)
        tel = sim.telemetry()
        self.assertEqual(tel.fwd_dbm, [None] * 4)
        self.assertEqual(tel.age_s, 0.0)
        sim.physical_tx_unchecked(np.full((2, 20000), 0.5 + 0j), chains=[0, 1], tx_gains_db=20.0)
        tel = sim.telemetry()
        p_fs = sim.truth.p_fs_true(0, FC, 20.0)
        self.assertAlmostEqual(tel.fwd_dbm[0], p_fs + 20 * math.log10(0.5), delta=0.05)
        self.assertAlmostEqual(tel.rev_dbm[0] - tel.fwd_dbm[0], 20 * math.log10(1 / 3), delta=1e-6)
        self.assertIsNone(tel.fwd_dbm[2])
        rise = tel.temp_c[0] - 35.0
        self.assertGreater(rise, 0.01)
        self.assertEqual(tel.temp_c[2], 35.0)
        sim.advance(60.0)
        tel2 = sim.telemetry()
        self.assertIsNone(tel2.fwd_dbm[0])                                       # detector hold expired
        self.assertAlmostEqual(tel2.temp_c[0] - 35.0, rise * math.exp(-1.0), delta=rise * 0.01)
        sim.inject_fault("over_temp", temp_c=99.0)
        sim.inject_fault("reverse_power", dbm=33.0, chains=[1])
        sim.inject_fault("telemetry_stale")
        tel3 = sim.telemetry()
        self.assertEqual(tel3.temp_c, [99.0] * 4)
        self.assertEqual(tel3.rev_dbm[1], 33.0)
        self.assertEqual(tel3.age_s, math.inf)
        sim.clear_faults()
        self.assertEqual(sim.telemetry().age_s, 0.0)
        mon = RF.PowerMonitor(RF.parse_line("sim"), sim)
        self.assertEqual(mon.read().age_s, 0.0)

    def test_interlock_and_dummy_load_sense(self):
        cfg = cfg_of(self.td, TX4, rf_control={"interlock": "sim:il", "dummy_load_sense": "sim:dl"})
        sim, _ = up(cfg)
        il = RF.InterlockInput(RF.parse_line("sim:il"), sim)
        dl = RF.InterlockInput(RF.parse_line("sim:dl"), sim)
        self.assertTrue(il.closed())
        self.assertTrue(dl.closed())                                             # auto wiring = dummy_load
        sim.inject_fault("interlock_open")
        sim.inject_fault("dummy_load_lost")
        self.assertFalse(il.closed())
        self.assertFalse(dl.closed())
        sim.clear_faults()
        self.assertTrue(il.closed())
        ant = cfg_of(self.td, TX4, rf_control={"dummy_load_sense": "sim:dl"}, sim={"tx_load": "antenna"})
        sim2, _ = up(ant)
        self.assertFalse(RF.InterlockInput(RF.parse_line("sim:dl"), sim2).closed())

    def test_far_field_probe_peaks_at_tx_look(self):
        cfg = cfg_of(self.td, TX4, geometry={"preset": "ula", "num_elements": 4},
                     sim={"tx_load": "antenna", "tx_delay_spread_samples": 0.0})
        sim, _ = up(cfg)
        self.assertEqual(sim.far_field_probe(0, 0).size, 0)
        look = 25.0
        pos = sim.geometry.positions("tx")
        t = np.conj(P.steering_ff(pos, G.direction_vector(look, 0), FC))
        pre = t / sim.true_tx_response()
        pre = pre / np.max(np.abs(pre)) * 0.4
        x = pre[:, None] * np.exp(2j * np.pi * 0.01 * np.arange(2000))[None, :]
        sim.physical_tx_unchecked(x, tx_gains_db=0.0)
        az = np.arange(-89.0, 90.0, 0.5)
        pw = [float(np.mean(np.abs(sim.far_field_probe(a % 360, 0)) ** 2)) for a in az]
        self.assertLessEqual(abs(az[int(np.argmax(pw))] - look), 1.0)
        self.assertGreater(10 * math.log10(max(pw) / min(pw)), 15.0)

    def test_integer_delay_control(self):
        cfg = cfg_of(self.td, {"devices": {"sim0": {"family": "sim", "rx_channels": "0..3"}},
                               "geometry": {"preset": "ula", "num_elements": 4}},
                     sim={"emulate_family": "crimson_tng", "rx_delay_spread_samples": 3.0})
        sim, _ = up(cfg)
        d0 = sim.truth.rx_delay_samples.copy()
        ref = sim.rx_refs()[1]
        self.assertTrue(sim.set_integer_delay("rx", ref, 2))
        self.assertAlmostEqual(sim.truth.rx_delay_samples[1], d0[1] - 2)
        plain, _ = up(cfg_of(self.td))
        self.assertFalse(plain.set_integer_delay("rx", plain.rx_refs()[0], 2))

    def test_gains_quantized_and_clipped(self):
        sim, _ = up(cfg_of(self.td))
        out = sim.set_rx_gain([10.4, 200, -5, 30, 30, 30, 30, 30.6])
        self.assertEqual(out, [10.0, 76.0, 0.0, 30.0, 30.0, 30.0, 30.0, 31.0])
        with self.assertRaises(BackendError):
            sim.set_rx_gain([1, 2])
        self.assertEqual(sim.set_rate("rx", 2e6), 2e6)

    def test_status_and_calls(self):
        sim, _ = up(cfg_of(self.td))
        st = sim.status()
        self.assertTrue(st["simulation"])
        self.assertEqual(st["phase"], "TUNED")
        self.assertEqual(st["flavour"], "ettus")
        name, args, t = sim.calls[-1]
        self.assertIsInstance(args, tuple)
        self.assertIsInstance(t, float)
        with self.assertRaises(ValueError):
            sim.inject_fault("meteor")


# ---------------------------------------------------------------------------
def _fake_backend_class():
    """A minimal concrete ActiveBackend (every hook a no-op): base-class rules only."""
    def tune(self, direction, f, lo, at):
        refs = self._rx_refs if direction == "rx" else self._tx_refs
        n = len(refs)
        sign = 1.0 if direction == "rx" else -1.0             # UHD tune_result_t (no actual_center_hz)
        return AB.TuneReport(direction, tuple(refs), f, [f + lo] * n, [sign * lo] * n, False, "random",
                             [True] * n, 0, False)
    body = {m: (lambda self, *a, **k: None) for m in AB.ActiveBackend.__abstractmethods__}
    body.update({"name": "fake", "_do_set_rate": lambda self, d, hz: hz, "_do_tune": tune,
                 "_do_time_now": lambda self, dev: DevTime(1, 0, 100_000_000),
                 "_do_rx_gain": lambda self: [], "_do_async_events": lambda self: [],
                 "_do_ref_locked": lambda self, dev: True})
    return type("_FakeBackend", (AB.ActiveBackend,), body)


class TestBaseRules(Base):
    def test_rate_change_bump_and_stream_stop_in_base(self):
        be = _fake_backend_class()(cfg_of(self.td))
        be.open()
        be.set_clock_source("internal")
        be.set_time_source("internal")
        be.set_rate("rx", cfg_of(self.td).rf.sample_rate_hz)
        g0 = be.coherence.generation
        be.set_rate("rx", 1e6)                                          # not tuned yet: no bump
        self.assertEqual(be.coherence.generation, g0)
        be.tune("rx", FC)
        g1 = be.coherence.generation
        self.assertEqual(be.set_rate("rx", 1e6), 1e6)                   # unchanged: no bump
        self.assertEqual(be.coherence.generation, g1)
        be.rx_start(DevTime(2, 0, 100_000_000))
        be.set_rate("rx", 2e6)
        self.assertEqual(be.coherence.generation, g1 + 1)
        self.assertEqual(be.coherence.snapshot()["last_reason"], "rate_rx")
        self.assertEqual(be.phase, "TUNED")                             # the stream was stopped
        self.assertEqual(be.rate("rx"), 2e6)

    def test_time_load_outside_sync_bumps(self):
        be = _fake_backend_class()(cfg_of(self.td))
        be.open()
        be.set_clock_source("internal")
        be.set_time_source("internal")
        g0 = be.coherence.generation
        be.set_time_next_pps(DevTime(0, 0, 100_000_000))
        self.assertEqual(be.coherence.generation, g0 + 1)
        with be.syncing():
            be.set_time_next_pps(DevTime(0, 0, 100_000_000))
        self.assertEqual(be.coherence.generation, g0 + 1)              # SyncProcedure bumps once itself

    def test_phase_state_per_tune_frequency(self):
        # N321 LO star: shared below 450 MHz is impossible -> random, and the crossing bumps
        dev = {"m": {"family": "sim", "rx_channels": "0..1", "lo_export": 1, "role": "master", "group": "s",
                     "clock_source": "external", "time_source": "external"},
               "s": {"family": "sim", "rx_channels": "0..1", "lo_source": "external", "group": "s",
                     "clock_source": "external", "time_source": "external"}}
        cfg = cfg_of(self.td, {"devices": dev, "geometry": {"preset": "ula", "num_elements": 4}},
                     sim={"emulate_family": "n321"}, timing={"lo_plan": "n321_star"})
        sim, plan = up(cfg, tune=False)
        self.assertEqual(T.retune(sim, plan, FC)[0].phase_state, "shared_lo")
        g0 = sim.coherence.generation
        rep = T.retune(sim, plan, 300e6)[0]
        self.assertEqual(rep.phase_state, "random")
        self.assertTrue(rep.band_change)
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertEqual(sim._lo_class("s", "rx", True), "random")      # the sim agrees
        # an LO export switched off through the API: the share is not active
        T.retune(sim, plan, FC)
        sim.set_lo_export("rx", False, sim.rx_refs()[0])
        g1 = sim.coherence.generation
        rep = T.retune(sim, plan, FC)[0]
        self.assertEqual(rep.phase_state, "random")
        self.assertTrue(rep.phase_class_changed)
        self.assertEqual(sim.coherence.generation, g1 + 1)


class TestTxInterlocks(Base):
    def setUp(self):
        super().setUp()
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        super().tearDown()

    def armed(self, **over):
        cfg = cfg_of(self.td, TX4, **over)
        sim, plan = up(cfg)
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        return cfg, sim, plan, s, rf

    def test_tx_rate_change_disarms_and_mismatch_refused(self):
        cfg, sim, plan, s, rf = self.armed()
        sim.set_rate("tx", 250e3)
        self.assertEqual(s.state, TS.TxState.SAFE)
        sim.set_rate("tx", FS)
        arm(s)
        p = permit_for(s, sim, burst())
        sim._do_set_rate("tx", 250e3)                                   # behind the base class' back
        n0 = len(sim._bursts)
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "SHAPE")
        self.assertEqual(len(sim._bursts), n0)
        sim._do_set_rate("tx", FS)
        res = sim.tx_burst(permit_for(s, sim, burst()))
        self.assertTrue(res.ok)
        self.assertEqual(sim.last_tx_output()[2], sim.rate("tx"))       # modelled at the physical rate

    def test_tx_tune_disarms_and_lo_offset_checked(self):
        cfg, sim, plan, s, rf = self.armed()
        sim.tune("tx", FC, lo_offset_hz=20e6)                           # LO at 895 MHz, outside 902-928
        self.assertEqual(s.state, TS.TxState.SAFE)
        sim.tune("tx", FC)
        arm(s)
        p = permit_for(s, sim, burst())
        sim._do_tune("tx", FC, 20e6, None)                              # behind the base class' back
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "FREQ_CHANGED")
        self.assertIn("LO offset", cm.exception.detail)
        # retune() falls back to backend.safety
        sim._do_tune("tx", FC, 0.0, None)
        T.retune(sim, plan, 916e6)
        self.assertEqual(s.state, TS.TxState.SAFE)

    def test_time_load_disarms_and_bumps(self):
        cfg, sim, plan, s, rf = self.armed()
        g = sim.coherence.generation
        sim.set_time_now(DevTime(1000, 0, sim.time_now().tick_rate))
        self.assertEqual(s.state, TS.TxState.SAFE)
        self.assertEqual(sim.coherence.generation, g + 1)

    def test_device_time_moved_after_authorization_trips(self):
        cfg, sim, plan, s, rf = self.armed()
        sim.set_time_now(DevTime(1000, 0, sim.time_now().tick_rate))
        arm(s)
        p = permit_for(s, sim, burst(), dt=0.2)
        sim._do_set_time_now(DevTime(0, 0, sim.time_now().tick_rate), None)   # behind the base class' back
        n0 = len(sim._bursts)
        with self.assertRaises(InterlockTrip) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "START_TIME")
        self.assertEqual(s.state, TS.TxState.FAULT)
        self.assertEqual(len(sim._bursts), n0)
        self.assertEqual(sim._tx_gain, [0.0] * 4)

    def test_start_lead_rechecked_at_send(self):
        cfg, sim, plan, s, rf = self.armed()
        p = permit_for(s, sim, burst(), dt=0.1)
        sim.advance(0.1 - cfg.tx.min_start_lead_s / 2)                  # host too slow: now inside the lead
        n0 = len(sim._bursts)
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "START_TIME")
        self.assertEqual(s.state, TS.TxState.ARMED)                     # a late burst, not a fault
        self.assertEqual(len(sim._bursts), n0)

    def test_reference_change_and_resync_trip(self):
        cfg, sim, plan, s, rf = self.armed()
        sim.set_clock_source("external")
        self.assertEqual(s.state, TS.TxState.FAULT)
        self.assertEqual(s.fault_code, "REF_UNLOCK")
        cfg, sim, plan, s, rf = self.armed()
        T.SyncProcedure(sim, plan, cfg.timing).run()                    # spec 8.3: a re-sync trips TX
        self.assertEqual(s.state, TS.TxState.FAULT)
        self.assertEqual(s.fault_code, "REF_UNLOCK")
        self.assertIn("re-sync", s.fault_detail)
        T.retune(sim, plan, FC)
        with self.assertRaises((SafetyRefusal, InterlockTrip)):
            permit_for(s, sim, burst())

    def test_tx_gain_never_above_authorized(self):
        cfg, sim, plan, s, rf = self.armed(tx={"max_tx_gain_db": "2.13,2.13,2.13,2.13"})
        seen = []
        orig = sim._do_tx_send

        def send(*a):
            seen.append(list(sim._tx_gain))
            return orig(*a)
        sim._do_tx_send = send
        self.assertTrue(sim.tx_burst(permit_for(s, sim, burst(), gains=[2.13] * 4)).ok)
        self.assertEqual(seen[-1], [2.0] * 4)                           # coerced DOWN to the 0.25 dB step
        p = permit_for(s, sim, burst(), gains=[2.13] * 4)

        def up_coerce(chains, gains):                                   # a device that rounds up
            for c in chains:
                sim._tx_gain[int(c)] = 2.25
        sim._do_set_tx_gains = up_coerce
        n0 = len(sim._bursts)
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "GAIN")
        self.assertEqual(len(sim._bursts), n0)
        self.assertEqual(sim._tx_gain, [0.0] * 4)
        self.assertEqual(s.state, TS.TxState.ARMED)

    def test_trip_between_consume_and_send_aborts(self):
        cfg, sim, plan, s, rf = self.armed(tx={"max_tx_gain_db": "4"})
        p = permit_for(s, sim, burst(), gains=[4.0] * 4)
        orig = s.consume

        def consume(permit):
            orig(permit)
            s.trip("INTERLOCK_OPEN", "E-stop right after consume")
        s.consume = consume
        n0 = len(sim._bursts)
        with self.assertRaises(InterlockTrip) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "INTERLOCK_OPEN")
        self.assertEqual(len(sim._bursts), n0)                          # nothing went out
        self.assertEqual(sim._tx_gain, [0.0] * 4)

    def test_reverse_waves_idle_ports_and_dummy_load(self):
        S = np.diag([0.1] * 4).astype(complex)
        S[1, 0] = S[0, 1] = 0.3
        cfg = cfg_of(self.td, TX4, sim={"tx_load": "antenna", "load_vswr": 2.0, "tx_gain_sigma_db": 0.0})
        sim, _ = up(cfg)
        sim.frontend.s_matrix = S
        sim.physical_tx_unchecked(np.full((1, 4000), 0.5 + 0j), chains=[0], tx_gains_db=10.0)
        tel = sim.telemetry()
        self.assertIsNone(tel.fwd_dbm[1])
        self.assertAlmostEqual(tel.rev_dbm[1] - tel.fwd_dbm[0], 20 * math.log10(0.3), delta=0.05)  # idle port
        self.assertAlmostEqual(tel.rev_dbm[0] - tel.fwd_dbm[0], 20 * math.log10(0.1), delta=0.05)
        self.assertIsNone(tel.rev_dbm[3])
        dl = cfg_of(self.td, TX4, sim={"tx_load": "dummy_load", "load_vswr": 2.0, "tx_gain_sigma_db": 0.0})
        sim2, _ = up(dl)
        sim2.frontend.s_matrix = S                                      # the antenna's S: not the loads'
        sim2.physical_tx_unchecked(np.full((1, 4000), 0.5 + 0j), chains=[0], tx_gains_db=10.0)
        tel = sim2.telemetry()
        self.assertAlmostEqual(tel.rev_dbm[0] - tel.fwd_dbm[0], 20 * math.log10(1 / 3), delta=1e-6)
        self.assertIsNone(tel.rev_dbm[1])

    def test_late_rx_command(self):
        sim, _ = up(cfg_of(self.td))
        past = sim.time_now().add_seconds(-0.01)
        t = sim.mono()
        cap = sim.rx_capture(256, past)
        self.assertEqual(cap.errors, ["late_command"])
        self.assertEqual(cap.dropped, 256)
        self.assertFalse(np.any(cap.data))
        self.assertEqual(sim.mono(), t)
        self.assertIn("late_command", [e.kind for e in sim.async_events()])
        sim.strict_rx_timing = False                                    # legacy, explicit opt-out
        self.assertEqual(sim.rx_capture(256, past).errors, [])


TWO_TX = {"active": {"en_active": 1, "mode": "trx", "operator": "tester"},
          "devices": {"a": {"family": "sim", "rx_channels": "0..1", "tx_channels": "0..1", "group": "ga",
                            "role": "master", "clock_source": "external", "time_source": "external"},
                      "b": {"family": "sim", "rx_channels": "0..1", "tx_channels": "0..1", "group": "gb",
                            "clock_source": "external", "time_source": "external"}},
          "geometry": {"preset": "ula", "num_elements": 4},
          "tx": {"en_tx": 1, "allowed_modes": "dummy_load", "allow_bands": "902e6:928e6",
                 "dummy_load_rating_w": 1}}


class TestRfStateInterlocks(Base):
    """The RF-state rules added for the check-then-change races (tune / rate / time vs
    tx_burst), the re-open, the command time, late timed tunes, the UHD tune sign
    convention and the LO / integer-delay interlocks."""

    def setUp(self):
        super().setUp()
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        super().tearDown()

    def armed(self, base=TX4, **over):
        cfg = cfg_of(self.td, base, **over)
        sim, plan = up(cfg)
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        return cfg, sim, plan, s, rf

    # --- check-then-change race ----------------------------------------------------------
    def test_tx_tune_cannot_slip_between_the_check_and_the_send(self):
        cfg = cfg_of(self.td, TX4)
        sim, plan = up(cfg)
        s, rf = make_safety(cfg, sim, self.td)
        checked, go = threading.Event(), threading.Event()
        orig_dis = sim._disarm_tx

        def delayed(reason):                    # TX not live yet; then a scheduling delay
            orig_dis(reason)
            checked.set()
            go.wait(10.0)
        sim._disarm_tx = delayed
        out = {}

        def tuner():
            try:
                out["rep"] = sim.tune("tx", 920e6)
            except BaseException as exc:  # noqa: BLE001
                out["exc"] = exc
        th = threading.Thread(target=tuner, daemon=True)
        th.start()
        self.assertTrue(checked.wait(10.0))
        sim._disarm_tx = orig_dis
        arm(s)
        p = permit_for(s, sim, burst())
        orig_send = sim._do_tx_send
        seen = {}

        def send(*a):
            go.set()
            th.join(0.3)                        # the tune must wait for the burst
            seen["tuner_blocked"] = th.is_alive()
            seen["fc_at_send"] = sim.tuned_freq("tx")
            return orig_send(*a)
        sim._do_tx_send = send
        res = sim.tx_burst(p)
        self.assertTrue(res.ok)
        self.assertTrue(seen["tuner_blocked"])
        self.assertEqual(seen["fc_at_send"], FC)
        self.assertEqual(sim.last_tx_output()[3], FC)                   # emitted at the authorized fc
        th.join(10.0)
        self.assertFalse(th.is_alive())
        self.assertIn("rep", out, out)
        self.assertEqual(sim.tuned_freq("tx"), 920e6)
        self.assertEqual(s.state, TS.TxState.SAFE)                      # the delayed tune disarmed

    def test_arm_during_the_change_refuses_and_changes_nothing(self):
        cfg, sim, plan, s, rf = self.armed()
        orig_dis = sim._disarm_tx
        n = {"calls": 0}

        def rearm(reason):                      # disarmed, then re-armed before the lock is taken
            orig_dis(reason)
            n["calls"] += 1
            if n["calls"] == 2:
                arm(s)
        sim._disarm_tx = rearm
        g0 = sim.coherence.generation
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tune("tx", 920e6)
        self.assertEqual(cm.exception.code, "SEQUENCE")
        self.assertEqual(sim.tuned_freq("tx"), FC)
        self.assertEqual(sim.coherence.generation, g0)
        sim._disarm_tx = orig_dis
        # a reference change trips (REF_UNLOCK) and still applies
        sim.set_clock_source("internal")
        self.assertEqual(s.state, TS.TxState.FAULT)
        self.assertEqual(s.fault_code, "REF_UNLOCK")

    # --- reopen -----------------------------------------------------------------------------
    def test_reopen_forgets_the_rf_state(self):
        cfg = cfg_of(self.td, TX4)
        sim, plan = up(cfg)
        self.assertEqual(sim._tuned_dirs, {"rx", "tx"})
        g0 = sim.coherence.generation
        sim.close()
        sim.open()
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertEqual(sim.coherence.snapshot()["last_reason"], "reopen")
        self.assertEqual(sim._tuned_dirs, set())
        self.assertIsNone(sim.last_tune("tx"))
        self.assertEqual((sim._center, sim._rate_set, sim._lo_exp_state, sim._lo_src_state), ({}, {}, {}, {}))
        self.assertFalse(sim._tx_streamer)
        T.SyncProcedure(sim, plan, cfg.timing).run()
        sim.tune("rx", FC)                                              # RX only
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        p = permit_for(s, sim, burst())
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "BACKEND")
        self.assertIn("not tuned", cm.exception.detail)
        T.retune(sim, plan, FC, direction="tx")                         # disarms
        arm(s)
        self.assertTrue(sim.tx_burst(permit_for(s, sim, burst())).ok)

    def test_failed_sync_refuses_tx(self):
        cfg = cfg_of(self.td, TWO_TX)
        sim, plan = up(cfg, sync=False, tune=False)
        master = cfg.master_device().name
        sim._latency = lambda dev: SB.Fraction(0) if dev == master else SB.Fraction(500, sim._tick[dev])
        with self.assertRaises(SB.ac.CoherenceError) as cm:
            T.SyncProcedure(sim, plan, cfg.timing).run()
        self.assertEqual(cm.exception.code, "PPS_SKEW")
        T.retune(sim, plan, FC)
        s, rf = make_safety(cfg, sim, self.td)
        arm(s)
        p = permit_for(s, sim, burst())
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "BACKEND")
        self.assertIn("time not aligned", cm.exception.detail)

    # --- command time -----------------------------------------------------------------------
    def test_command_time_blocks_tx_burst(self):
        cfg, sim, plan, s, rf = self.armed()
        p = permit_for(s, sim, burst())
        sim.set_command_time(start_in(sim, 1.0))
        self.assertTrue(sim.command_time_set())
        n0 = len(sim._bursts)
        with self.assertRaises(SafetyRefusal) as cm:
            sim.tx_burst(p)
        self.assertEqual(cm.exception.code, "SEQUENCE")
        self.assertIn("command time", cm.exception.detail)
        self.assertEqual(len(sim._bursts), n0)
        sim.clear_command_time()
        self.assertFalse(sim.command_time_set())
        self.assertTrue(sim.tx_burst(p).ok)                             # refused before consume
        # command_window holds the TX lock: a burst waits for the window to close
        p2 = permit_for(s, sim, burst())
        out = {}
        with sim.command_window(start_in(sim, 1.0)):
            th = threading.Thread(target=lambda: out.setdefault("r", sim.tx_burst(p2)), daemon=True)
            th.start()
            th.join(0.3)
            self.assertTrue(th.is_alive())
        th.join(10.0)
        self.assertFalse(th.is_alive())
        self.assertTrue(out["r"].ok)

    # --- late timed tune ------------------------------------------------------------------
    def test_late_timed_tune_counts_as_untimed(self):
        cfg = cfg_of(self.td, TWO_DEV, sim={"emulate_family": "x310_ubx"})
        sim, plan = up(cfg)
        self.assertEqual(sim.last_tune("rx")[2:], (True, "repeatable"))
        g0 = sim.coherence.generation
        t_ok = start_in(sim, 0.1)
        with sim.command_window(t_ok):
            rep = sim.tune("rx", 920e6, at=t_ok)                        # a real timed tune: no bump
        self.assertTrue(rep.timed)
        self.assertEqual(rep.phase_state, "repeatable")
        self.assertEqual(sim.coherence.generation, g0)
        sim.async_events()
        past = start_in(sim, -5.0)
        with sim.command_window(past):
            rep = sim.tune("rx", 921e6, at=past)
        self.assertFalse(rep.timed)
        self.assertEqual(rep.phase_state, "random")
        self.assertIn("TUNE_LATE", {i.code for i in rep.issues})
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertIn("late_command", [e.kind for e in sim.async_events()])
        rep = sim.tune("rx", 922e6, at=start_in(sim, 0.1))             # 'at' without a command time
        self.assertFalse(rep.timed)

    # --- UHD tune sign --------------------------------------------------------------------
    def test_tune_report_follows_uhd_sign_convention(self):
        cfg = cfg_of(self.td, TX4)
        sim, plan = up(cfg)
        lo = 1e6
        rx = sim.tune("rx", FC, lo)
        tx = sim.tune("tx", FC, lo)
        self.assertEqual((rx.actual_rf_hz[0], rx.actual_dsp_hz[0]), (FC + lo, lo))     # rf = target + lo_off
        self.assertEqual((tx.actual_rf_hz[0], tx.actual_dsp_hz[0]), (FC + lo, -lo))    # TX_SIGN = -1
        self.assertEqual(AB.ActiveBackend.tuned_freq(sim, "tx"), FC)
        self.assertEqual(AB.ActiveBackend.tuned_freq(sim, "rx"), FC)
        # a backend reporting only UHD rf / dsp (no actual_center_hz): the base derives the centre
        be = _fake_backend_class()(cfg_of(self.td))
        be.open()
        be.set_clock_source("internal")
        be.set_time_source("internal")
        be.tune("rx", FC, lo)
        self.assertEqual(be.tuned_freq("rx"), FC)
        rep = AB.TuneReport("tx", (), FC, [FC + lo], [-lo], False, "random", [True], 0, False)
        self.assertEqual(AB.tuned_center_hz("tx", rep, 0.0), FC)
        rep.actual_center_hz = [FC + 7.0]
        self.assertEqual(AB.tuned_center_hz("tx", rep, 0.0), FC + 7.0)

    # --- LO / integer delay ---------------------------------------------------------------
    def test_integer_delay_change_disarms_and_bumps(self):
        cfg, sim, plan, s, rf = self.armed(sim={"emulate_family": "crimson_tng"})
        ref = sim.tx_refs()[1]
        g0 = sim.coherence.generation
        self.assertTrue(sim.set_integer_delay("tx", ref, 3))
        self.assertEqual(s.state, TS.TxState.SAFE)
        self.assertEqual(sim.coherence.generation, g0 + 1)
        self.assertEqual(sim.coherence.snapshot()["last_reason"], "integer_delay_tx")
        self.assertEqual(sim.integer_delays("tx"), {ref: 3})
        sim.set_integer_delay("tx", ref, 3)                             # unchanged: no bump
        self.assertEqual(sim.coherence.generation, g0 + 1)
        sim.set_integer_delay("tx", ref, 2, bump=False)                 # a calibration's own correction
        self.assertEqual(sim.coherence.generation, g0 + 1)
        arm(s)
        sim.set_integer_delay("rx", sim.rx_refs()[0], 1)                # RX: no disarm, bumps
        self.assertEqual(s.state, TS.TxState.ARMED)
        self.assertEqual(sim.coherence.generation, g0 + 2)

    def test_lo_change_disarms_and_share_change_bumps(self):
        cfg, sim, plan, s, rf = self.armed()
        g0 = sim.coherence.generation
        sim.set_lo_source("rx", "internal", sim.rx_refs()[0])           # unchanged source: disarm only
        self.assertEqual(s.state, TS.TxState.SAFE)
        self.assertEqual(sim.coherence.generation, g0)
        # N321 star: switching the export off bumps at once (not only at the next verify)
        dev = {"m": {"family": "sim", "rx_channels": "0..1", "lo_export": 1, "role": "master", "group": "s",
                     "clock_source": "external", "time_source": "external"},
               "s": {"family": "sim", "rx_channels": "0..1", "lo_source": "external", "group": "s",
                     "clock_source": "external", "time_source": "external"}}
        cfg2 = cfg_of(self.td, {"devices": dev, "geometry": {"preset": "ula", "num_elements": 4}},
                      sim={"emulate_family": "n321"}, timing={"lo_plan": "n321_star"})
        sim2, plan2 = up(cfg2)
        self.assertEqual(sim2.last_tune("rx")[3], "shared_lo")
        g1 = sim2.coherence.generation
        sim2.set_lo_export("rx", True, sim2.rx_refs()[0])               # already on: no bump
        self.assertEqual(sim2.coherence.generation, g1)
        sim2.set_lo_export("rx", False, sim2.rx_refs()[0])
        self.assertEqual(sim2.coherence.generation, g1 + 1)
        self.assertEqual(sim2.phase_state_for(True, "rx"), "random")
        sim2.set_lo_source("rx", "internal", sim2.rx_refs()[2])
        self.assertEqual(sim2.coherence.generation, g1 + 2)
        self.assertEqual(sim2.coherence.snapshot()["last_reason"], "lo_source_rx")


class TestTxThreading(Base):
    """Real threads (bounded waits on Events, no fixed sleeps): the fail-safe paths
    must not depend on the backend lock being free."""

    def setUp(self):
        super().setUp()
        logging.disable(logging.CRITICAL)
        cfg = cfg_of(self.td, TX4, rf_control={"pa_enable_line": "sim:pa"})
        self.sim, _ = up(cfg)
        self.s, self.rf = make_safety(cfg, self.sim, self.td)
        arm(self.s)
        self.p = permit_for(self.s, self.sim, burst())
        self.rf.sequence_to_tx(self.p)
        self.assertTrue(self.sim._gpio[("sim", "pa")])

    def tearDown(self):
        logging.disable(logging.NOTSET)
        super().tearDown()

    def run_burst(self):
        out = {}

        def target():
            try:
                out["result"] = self.sim.tx_burst(self.p)
            except BaseException as exc:  # noqa: BLE001
                out["exc"] = exc
        th = threading.Thread(target=target, daemon=True)
        th.start()
        return th, out

    def test_trip_reaches_pa_during_inflight_send(self):
        sim, s = self.sim, self.s
        in_send, release = threading.Event(), threading.Event()
        orig = sim._do_tx_send

        def blocking_send(*a):
            in_send.set()
            release.wait(10.0)                  # a hardware send blocked waiting for its ack
            return orig(*a)
        sim._do_tx_send = blocking_send
        th, out = self.run_burst()
        self.assertTrue(in_send.wait(10.0))
        tripper = threading.Thread(target=lambda: s.trip("INTERLOCK_OPEN", "watchdog"), daemon=True)
        tripper.start()
        tripper.join(10.0)
        self.assertFalse(tripper.is_alive(), "trip() blocked behind the in-flight send")
        self.assertFalse(sim._gpio[("sim", "pa")])                     # PA off BEFORE the send returned
        self.assertTrue(th.is_alive())
        release.set()
        th.join(10.0)
        self.assertFalse(th.is_alive())
        self.assertIsInstance(out.get("exc"), InterlockTrip)
        self.assertEqual(s.state, TS.TxState.FAULT)
        self.assertEqual(sim._tx_gain, [0.0] * 4)
        self.assertTrue(all(ok for _n, ok, _e in s.last_shutdown))

    def test_no_deadlock_between_backend_and_safety_locks(self):
        sim, s = self.sim, self.s
        held, in_consume = threading.Event(), threading.Event()
        orig = s.consume

        def consume(permit):
            in_consume.set()
            return orig(permit)                 # blocks on the safety lock held by the poller
        s.consume = consume

        def poller():                           # like poll(): safety lock held, then a trip
            with s._lock:
                held.set()
                in_consume.wait(10.0)
                s.trip("INTERLOCK_OPEN", "poll")
        pt = threading.Thread(target=poller, daemon=True)
        pt.start()
        self.assertTrue(held.wait(10.0))
        th, out = self.run_burst()
        pt.join(10.0)
        th.join(10.0)
        self.assertFalse(pt.is_alive(), "trip() deadlocked")
        self.assertFalse(th.is_alive(), "tx_burst deadlocked")
        self.assertIsInstance(out.get("exc"), (SafetyRefusal, InterlockTrip))
        self.assertFalse(sim._gpio[("sim", "pa")])
        self.assertTrue(dict((n, ok) for n, ok, _e in s.last_shutdown)["pa_off"])


if __name__ == "__main__":
    unittest.main()
