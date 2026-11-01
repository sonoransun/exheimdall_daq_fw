"""
    Unit tests for the active capability foundation (Firmware/_active/active_common.py).

    No sudo, no hardware, no network.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import json
import logging
import os
import stat
import sys
import tempfile
import unittest
from fractions import Fraction
from os.path import dirname, join, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import numpy as np  # noqa: E402

import active_common as ac  # noqa: E402
from active_common import (ConfigError, DevTime, EventLog, Issue, parse_az_el_list,  # noqa: E402
                           parse_bands_hz, parse_bool, parse_float_list, parse_int_list,
                           parse_kv_list)

logging.getLogger("heimdall.active").setLevel(logging.CRITICAL + 1)


class TestPaths(unittest.TestCase):
    def test_layout(self):
        self.assertEqual(ac.ACTIVE_DIR, join(root_path, "_active"))
        self.assertEqual(ac.FIRMWARE_DIR, root_path)
        self.assertEqual(ac.REPO_ROOT, dirname(root_path))
        self.assertEqual(ac.DAQ_CORE_DIR, join(root_path, "_daq_core"))
        self.assertTrue(ac.DEFAULT_CONFIG.endswith("config_files/active_sim_ula8/active_chain_config.ini"))

    def test_ensure_daq_core_path_idempotent(self):
        # Other test modules may insert the directory themselves when several run
        # in one process: check that the helper adds it once and never again.
        saved = list(sys.path)
        try:
            sys.path[:] = [p for p in sys.path if p != ac.DAQ_CORE_DIR]
            ac.ensure_daq_core_path()
            ac.ensure_daq_core_path()
            self.assertEqual(sys.path.count(ac.DAQ_CORE_DIR), 1)
            self.assertEqual(sys.path[0], ac.DAQ_CORE_DIR)
        finally:
            sys.path[:] = saved

    def test_state_and_log_dir_override(self):
        with tempfile.TemporaryDirectory() as td:
            p = ac.state_dir(0, join(td, "st"))
            self.assertTrue(os.path.isdir(p))
            q = ac.log_dir(3, join(td, "lg"))
            self.assertEqual(q, join(td, "lg"))
            self.assertTrue(os.path.isdir(q))

    def test_exit_codes(self):
        self.assertEqual((ac.EXIT_OK, ac.EXIT_INVALID, ac.EXIT_USAGE, ac.EXIT_BACKEND,
                          ac.EXIT_SAFETY, ac.EXIT_CAL), (0, 1, 2, 3, 4, 5))


class TestExceptions(unittest.TestCase):
    def test_hierarchy_and_fields(self):
        e = ac.SafetyRefusal("TX_DISABLED", "en_tx = 0")
        self.assertIsInstance(e, ac.ActiveError)
        self.assertEqual((e.code, e.detail), ("TX_DISABLED", "en_tx = 0"))
        self.assertIn("TX_DISABLED", str(e))
        t = ac.InterlockTrip("HEARTBEAT")
        self.assertEqual(t.code, "HEARTBEAT")
        self.assertEqual(ac.CoherenceError("PPS_MISSING").code, "PPS_MISSING")
        self.assertEqual(ac.CalibrationError("CAL_QUALITY", "x").code, "CAL_QUALITY")
        u = ac.BackendUnavailable("uhd missing", hint="python3-uhd")
        self.assertIsInstance(u, ac.BackendError)
        self.assertEqual(u.hint, "python3-uhd")
        self.assertIn("python3-uhd", str(u))
        self.assertTrue(issubclass(ac.NotSupported, ac.BackendError))
        self.assertTrue(issubclass(ac.HardwareFault, ac.BackendError))
        c = ConfigError("bad", [Issue("error", "RANGE", "m")])
        self.assertEqual(c.issues[0].code, "RANGE")
        self.assertIsInstance(c.issues, tuple)

    def test_issue_helpers(self):
        iss = [Issue("warning", "W", "w"), Issue("error", "E", "e", "[x] y")]
        self.assertTrue(ac.has_errors(iss))
        self.assertFalse(ac.has_errors(iss[:1]))
        text = ac.format_issues(iss)
        self.assertTrue(text.splitlines()[0].startswith("ERROR E"))
        js = ac.issues_to_json(iss)
        self.assertEqual(js[1], {"level": "error", "code": "E", "message": "e", "where": "[x] y"})
        json.dumps(js)
        with self.assertRaises(ValueError):
            Issue("fatal", "X", "bad level")


class TestDevTime(unittest.TestCase):
    def test_normalisation(self):
        t = DevTime(1, 250, 100)
        self.assertEqual((t.secs, t.ticks, t.tick_rate), (3, 50, 100))
        n = DevTime(0, -1, 100)
        self.assertEqual((n.secs, n.ticks), (-1, 99))
        self.assertEqual(DevTime(5), DevTime(5, 0, 200_000_000))
        with self.assertRaises(ValueError):
            DevTime(0, 0, 0)
        with self.assertRaises(TypeError):
            DevTime(0.5, 0, 10)

    def test_exact_add_diff_compare_across_rates(self):
        a = DevTime(10, 1, 3)              # 10 + 1/3 s
        b = DevTime(10, 2, 6)              # same instant, other grid
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(a.diff_seconds(b), 0)
        c = DevTime(10, 1, 2)              # 10.5 s
        self.assertLess(a, c)
        self.assertGreater(c, b)
        self.assertLessEqual(a, b)
        self.assertGreaterEqual(c, a)
        self.assertNotEqual(a, c)
        self.assertEqual(c.diff_seconds(a), Fraction(1, 6))
        with self.assertRaises(ValueError):
            a.diff_ticks(c)
        d = DevTime(10, 0, 3)
        self.assertEqual(a.diff_ticks(d), 1)
        self.assertEqual(d.add_ticks(7), DevTime(12, 1, 3))
        self.assertEqual(d.add_seconds(Fraction(4, 3)), DevTime(11, 1, 3))
        self.assertEqual(d.add_seconds(0.5).ticks, 2)      # 1.5 ticks -> half-even -> 2
        self.assertEqual(d.add_seconds(-1).secs, 9)

    def test_from_seconds_rounding(self):
        self.assertEqual(DevTime.from_seconds(Fraction(5, 2), 1), DevTime(2, 0, 1))   # half-even
        self.assertEqual(DevTime.from_seconds(Fraction(7, 2), 1), DevTime(4, 0, 1))
        t = DevTime.from_seconds(0.1, 200_000_000)
        self.assertEqual((t.secs, t.ticks), (0, 20_000_000))

    def test_timespec_roundtrip(self):
        rate = 250_000_000
        for secs, ticks in ((0, 0), (1, 1), (1_800_000_000, 249_999_999), (42, 123_456_789)):
            t = DevTime(secs, ticks, rate)
            full, frac = t.to_timespec()
            self.assertIsInstance(full, int)
            self.assertIsInstance(frac, float)
            self.assertEqual(DevTime.from_timespec(full, frac, rate), t)
        self.assertEqual(DevTime.from_timespec(7.0, 0.5, 4), DevTime(7, 2, 4))   # integral float ok
        with self.assertRaises(TypeError):
            DevTime.from_timespec(7.5, 0.0, 4)
        # a fractional part that rounds up to a full second carries
        self.assertEqual(DevTime.from_timespec(3, 0.9999999999999, 100), DevTime(4, 0, 100))

    def test_sample_offset_crimson(self):
        tick = 162_500_000
        fs = 325e6 / 3                      # 108.333... MS/s = 1.5 ticks per sample
        t0 = DevTime(1_800_000_000, 12345, tick)
        for m in (0, 1, 7, 1000, 10 ** 9):
            t = t0.add_ticks(3 * m)
            self.assertEqual(t.sample_offset(t0, fs), 2 * m)
        self.assertEqual(t0.add_ticks(-3).sample_offset(t0, fs), -2)
        # exact when fs is given as a Fraction too
        self.assertEqual(t0.add_ticks(3 * 10 ** 9).sample_offset(t0, Fraction(325_000_000, 3)),
                         2 * 10 ** 9)

    def test_precision_at_large_epoch(self):
        rate = 200_000_000
        t = DevTime(1_800_000_000, 0, rate)
        u = t.add_ticks(1)
        self.assertNotEqual(t, u)
        self.assertEqual(u.diff_ticks(t), 1)
        self.assertEqual(u.diff_seconds(t), Fraction(1, rate))
        # the float view cannot resolve the tick, the exact value can
        self.assertEqual(t.to_seconds(), u.to_seconds())

    def test_unix_ms_and_dict(self):
        t = DevTime(10, 25, 100)
        self.assertEqual(t.to_unix_ms(1_700_000_000.0), 1_700_000_010_250)
        self.assertEqual(DevTime.from_dict(t.to_dict()), t)
        self.assertEqual(DevTime(1, 2, 3).with_tick_rate(6), DevTime(1, 4, 6))


class TestUnits(unittest.TestCase):
    def test_db_helpers(self):
        self.assertAlmostEqual(ac.db10(100.0), 20.0)
        self.assertAlmostEqual(ac.db20(10.0), 20.0)
        self.assertAlmostEqual(ac.db20(-10.0), 20.0)
        self.assertAlmostEqual(ac.from_db10(3.0), 1.9952623, places=6)
        self.assertAlmostEqual(ac.from_db20(6.0), 1.9952623, places=6)
        self.assertAlmostEqual(ac.dbm_to_w(30.0), 1.0)
        self.assertAlmostEqual(ac.w_to_dbm(0.001), 0.0)
        self.assertEqual(ac.db10(0.0), float("-inf"))
        arr = ac.db10(np.array([1.0, 10.0]))
        np.testing.assert_allclose(arr, [0.0, 10.0])
        self.assertIsInstance(ac.db10(10), float)
        for x in (-30.0, 0.0, 17.5):
            self.assertAlmostEqual(ac.w_to_dbm(ac.dbm_to_w(x)), x)

    def test_wavelength_k0(self):
        self.assertAlmostEqual(ac.wavelength(1e9), 0.299792458)
        self.assertAlmostEqual(ac.k0(1e9), 2 * np.pi / 0.299792458)
        with self.assertRaises(ValueError):
            ac.wavelength(0.0)

    def test_wrap(self):
        self.assertEqual(ac.wrap_deg(180.0), -180.0)
        self.assertEqual(ac.wrap_deg(-180.0), -180.0)
        self.assertAlmostEqual(ac.wrap_deg(190.0), -170.0)
        self.assertAlmostEqual(ac.wrap_deg(-190.0), 170.0)
        self.assertEqual(ac.wrap_az(360.0), 0.0)
        self.assertAlmostEqual(ac.wrap_az(-90.0), 270.0)
        self.assertLess(ac.wrap_az(-1e-20), 360.0)
        np.testing.assert_allclose(ac.wrap_az(np.array([-10.0, 370.0])), [350.0, 10.0])


class TestParsers(unittest.TestCase):
    def test_parse_bool(self):
        self.assertTrue(parse_bool("1"))
        self.assertFalse(parse_bool(" 0 "))
        iss = []
        self.assertTrue(parse_bool("yes", "[s] k", iss))
        self.assertFalse(parse_bool("False", "[s] k", iss))
        self.assertEqual([i.code for i in iss], ["BOOL_SPELLING", "BOOL_SPELLING"])
        self.assertEqual(iss[0].level, "warning")
        with self.assertRaises(ConfigError) as cm:
            parse_bool("maybe", "[s] k")
        self.assertEqual(cm.exception.issues[0].code, "TYPE")

    def test_parse_float_list(self):
        self.assertEqual(parse_float_list("", 3, 1.5), [1.5, 1.5, 1.5])
        self.assertEqual(parse_float_list("2", 3, 0.0), [2.0, 2.0, 2.0])
        self.assertEqual(parse_float_list("1, 2,3", 3, 0.0), [1.0, 2.0, 3.0])
        self.assertEqual(parse_float_list("", 2, None), [None, None])
        with self.assertRaises(ConfigError) as cm:
            parse_float_list("1,2", 3, 0.0, "[active_tx] max_chain_power_dbm")
        self.assertEqual(cm.exception.issues[0].code, "LIST_LEN")
        for bad in ("a", "1,,2", "nan", "inf"):
            with self.assertRaises(ConfigError):
                parse_float_list(bad, 3, 0.0)

    def test_parse_int_list(self):
        self.assertEqual(parse_int_list("0,2, 3"), [0, 2, 3])
        self.assertEqual(parse_int_list("0..3"), [0, 1, 2, 3])
        self.assertEqual(parse_int_list(""), [])
        self.assertEqual(parse_int_list("4", 2), [4, 4])
        self.assertEqual(parse_int_list("0x10"), [16])
        with self.assertRaises(ConfigError) as cm:
            parse_int_list("1,2", 3)
        self.assertEqual(cm.exception.issues[0].code, "LIST_LEN")
        for bad in ("1.5", "x", "3..1"):
            with self.assertRaises(ConfigError):
                parse_int_list(bad)

    def test_parse_bands(self):
        self.assertEqual(parse_bands_hz("902e6:928e6; 2400e6:2483.5e6"),
                         [(902e6, 928e6), (2400e6, 2483.5e6)])
        self.assertEqual(parse_bands_hz(""), [])
        for bad in ("928e6:902e6", "1:2:3", "a:b", "-5:3"):
            with self.assertRaises(ConfigError):
                parse_bands_hz(bad)

    def test_parse_az_el(self):
        self.assertEqual(parse_az_el_list("75:0; 120:10"), [(75.0, 0.0), (120.0, 10.0)])
        with self.assertRaises(ConfigError):
            parse_az_el_list("10:95")
        with self.assertRaises(ConfigError):
            parse_az_el_list("10")

    def test_parse_kv(self):
        self.assertEqual(parse_kv_list("/a/b=1; c = two"), {"/a/b": "1", "c": "two"})
        self.assertEqual(list(parse_kv_list("z=1;a=2")), ["z", "a"])
        for bad in ("novalue", "=x", "a=1;a=2"):
            with self.assertRaises(ConfigError):
                parse_kv_list(bad)


class TestRng(unittest.TestCase):
    def test_pinned_values(self):
        # Pinned: any change in the seeding scheme breaks simulation determinism.
        self.assertEqual(ac.rng_stream(7).integers(0, 2 ** 31, size=3).tolist(),
                         [2029167941, 1342382292, 1469265226])
        vals = ac.rng_stream(12345, "lo_phase", "sim0", 3).random(2).tolist()
        self.assertAlmostEqual(vals[0], 0.3579666018168066, places=15)
        self.assertAlmostEqual(vals[1], 0.8864379021043077, places=15)

    def test_reproducible_and_keyed(self):
        a = ac.rng_stream(1, "noise", 0).random(8)
        b = ac.rng_stream(1, "noise", 0).random(8)
        np.testing.assert_array_equal(a, b)
        c = ac.rng_stream(1, "noise", 1).random(8)
        d = ac.rng_stream(2, "noise", 0).random(8)
        e = ac.rng_stream(1, "coupling", 0).random(8)
        for other in (c, d, e):
            self.assertFalse(np.array_equal(a, other))
        with self.assertRaises(ValueError):
            ac.rng_stream(-1)


class TestFiles(unittest.TestCase):
    def test_atomic_write_text_and_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = join(td, "sub", "f.txt")
            ac.atomic_write_text(p, "hello\n", mode=0o600)
            with open(p) as fh:
                self.assertEqual(fh.read(), "hello\n")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            ac.atomic_write_text(p, "again")
            with open(p) as fh:
                self.assertEqual(fh.read(), "again")
            self.assertEqual(sorted(os.listdir(join(td, "sub"))), ["f.txt"])   # no temp left
            j = join(td, "x.json")
            obj = {"a": np.float64(1.5), "b": np.arange(3), "c": 1 + 2j, "d": DevTime(1, 2, 4),
                   "e": (1, 2), "f": Fraction(1, 4), "g": np.int32(7), "h": {3}}
            ac.atomic_write_json(j, obj)
            with open(j) as fh:
                back = json.load(fh)
            self.assertEqual(back, {"a": 1.5, "b": [0, 1, 2], "c": [1.0, 2.0],
                                    "d": {"secs": 1, "ticks": 2, "tick_rate": 4}, "e": [1, 2],
                                    "f": 0.25, "g": 7, "h": [3]})

    def test_atomic_write_failure_cleans_up(self):
        with tempfile.TemporaryDirectory() as td:
            class Boom:
                def __str__(self):
                    raise RuntimeError("x")
            with self.assertRaises(TypeError):
                ac.atomic_write_text(join(td, "f"), Boom())
            self.assertEqual(os.listdir(td), [])

    def test_sha256(self):
        self.assertEqual(ac.sha256_bytes(b"abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        with tempfile.TemporaryDirectory() as td:
            p = join(td, "f")
            with open(p, "wb") as fh:
                fh.write(b"abc")
            self.assertEqual(ac.sha256_file(p), ac.sha256_bytes(b"abc"))


class TestEvents(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(ac.EVT_ACTIVE_TX_TRIP, "active_tx_trip")
        self.assertEqual(ac.EVT_ACTIVE_SIM_DAMAGE, "active_sim_damage")
        self.assertEqual(len(ac.EVENT_TYPES), 26)
        for v in ac.EVENT_TYPES:
            self.assertTrue(v.startswith("active_") and v == v.lower())

    def test_jsonl_daq_event_shape_and_ring(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as td:
            path = join(td, "logs", "active_events.jsonl")
            log = EventLog(path, ring=3, clock=lambda: now[0])
            for k in range(5):
                now[0] += 1
                d = log.emit(ac.EVT_ACTIVE_TUNE, "info", freq_hz=915e6 + k, arr=np.ones(2))
            self.assertEqual(set(d), {"timestamp", "severity", "module", "event_type", "payload"})
            self.assertEqual(d["timestamp"], 1005.0)
            self.assertEqual(d["module"], "active")
            self.assertEqual(d["payload"]["arr"], [1.0, 1.0])
            with open(path) as fh:
                lines = [json.loads(x) for x in fh]
            self.assertEqual(len(lines), 5)
            self.assertEqual(lines[0]["payload"]["freq_hz"], 915e6)
            self.assertEqual(len(log), 3)
            self.assertEqual([e["payload"]["freq_hz"] for e in log.recent()],
                             [915e6 + 2, 915e6 + 3, 915e6 + 4])
            log.emit(ac.EVT_ACTIVE_TX_TRIP, "critical", code="HEARTBEAT")
            self.assertEqual(len(log.recent(10, ac.EVT_ACTIVE_TX_TRIP)), 1)
            self.assertEqual(len(log.recent(1)), 1)
            self.assertEqual(log.recent(0), [])

    def test_matches_daq_event(self):
        from daq_events import DAQEvent
        log = EventLog(None, clock=lambda: 5.0)
        d = log.emit("active_beam", "info", az=1.0)
        self.assertEqual(set(d), set(DAQEvent(5.0, "info", "active", "active_beam", {"az": 1.0}).to_dict()))

    def test_unwritable_path_never_raises(self):
        with tempfile.TemporaryDirectory() as td:
            blocker = join(td, "file")
            with open(blocker, "w") as fh:
                fh.write("x")
            log = EventLog(join(blocker, "sub", "ev.jsonl"))   # parent is a regular file
            d = log.emit("active_open", backend="sim")
            self.assertEqual(d["event_type"], "active_open")
            log.emit("active_open")
            self.assertEqual(log.write_errors, 2)
            self.assertEqual(len(log.recent()), 2)

    def test_event_bus_forwarding_and_failure(self):
        got = []

        class Bus:
            def emit(self, ev):
                got.append(ev)

        class BadBus:
            def emit(self, ev):
                raise RuntimeError("bus down")

        EventLog(None, event_bus=Bus()).emit("active_cal_done", quality_db=-30)
        self.assertEqual(got[0].event_type, "active_cal_done")
        self.assertEqual(got[0].payload, {"quality_db": -30})
        EventLog(None, event_bus=BadBus()).emit("active_cal_done")   # must not raise

    def test_bad_clock_never_raises(self):
        def clock():
            raise RuntimeError("clock")
        d = EventLog(None, clock=clock).emit("active_open")
        self.assertIsInstance(d["timestamp"], float)


class TestValueTypes(unittest.TestCase):
    def test_channel_ref(self):
        r = ac.ChannelRef("n321a", 1)
        self.assertEqual(str(r), "n321a:1")
        self.assertEqual(r, ac.ChannelRef("n321a", 1))
        self.assertEqual(len({r, ac.ChannelRef("n321a", 1)}), 1)

    def test_tx_result_defaults(self):
        r = ac.TxResult(True, 100)
        self.assertEqual((r.underflows, r.late, r.acked, r.content_corrupt, r.events),
                         (0, 0, False, False, []))
        r.events.append(ac.AsyncEvent("burst_ack"))
        self.assertEqual(ac.TxResult(True, 1).events, [])
        self.assertIn("underflow", ac.ASYNC_EVENT_KINDS)


if __name__ == "__main__":
    unittest.main()
