"""
    Unit tests for hwc_relay (hw_controller -> delay_sync state relay + recal request)

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Hardware-free and CI-safe: every test works in its own temp directory.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '_daq_core'))

import hwc_relay  # noqa: E402


def _doc(epoch="1238-1761900000123", seqs=(), **extra):
    doc = {"v": hwc_relay.HWC_STATE_VERSION, "epoch": epoch, "seq": (max(seqs) + 1) if seqs else 0,
           "ts": 1761900000.12, "pid": 1238, "fsm": "IQ_CAL", "pending": 0,
           "noise_source": 0, "agc": 0, "gains_idx": [12] * 5, "gains_tenths": [297] * 5,
           "ext_lna_gains_db": [0] * 5, "bias_tee": 0,
           "schedule": {"state": "IDLE", "active": False, "gen": None},
           "orientation": {"az": 0.0, "el": 0.0, "state": 0},
           "frame_age_ms": 437, "max_term_seen": 0, "cal_stalled": False,
           "events": [{"seq": s, "timestamp": 1761900000.0 + s, "severity": "info",
                       "module": "hw_controller", "event_type": "freq_change",
                       "payload": {"n": s}} for s in seqs]}
    doc.update(extra)
    return doc


class _TmpDir(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hwc_relay_test_")
        self.state = os.path.join(self.dir, "hwc_state")
        self.recal = os.path.join(self.dir, "recal_request")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def leftovers(self):
        return [n for n in os.listdir(self.dir) if n.startswith(".")]


class TestPaths(unittest.TestCase):

    def test_paths(self):
        self.assertEqual(hwc_relay.hwc_state_path(), os.path.join("_data_control", "hwc_state"))
        self.assertEqual(hwc_relay.hwc_state_path("inst2_"),
                         os.path.join("_data_control", "inst2_hwc_state"))
        self.assertEqual(hwc_relay.recal_request_path("inst1_"),
                         os.path.join("_data_control", "inst1_recal_request"))
        self.assertEqual(hwc_relay.recal_request_path(control_dir="/x"),
                         os.path.join("/x", "recal_request"))

    def test_make_epoch(self):
        self.assertEqual(hwc_relay.make_epoch(1238, 1761900000123), "1238-1761900000123")
        e = hwc_relay.make_epoch()
        pid, ms = e.split("-")
        self.assertEqual(int(pid), os.getpid())
        self.assertLess(abs(int(ms) - time.time() * 1000), 60000)


class TestHwcState(_TmpDir):

    def test_round_trip(self):
        doc = _doc(seqs=(1040, 1041))
        hwc_relay.write_hwc_state(self.state, doc)
        self.assertEqual(hwc_relay.read_hwc_state(self.state), doc)
        with open(self.state) as f:
            raw = f.read()
        self.assertNotIn(" ", raw.replace("hw_controller", ""))   # compact JSON
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o644)

    def test_overwrite(self):
        hwc_relay.write_hwc_state(self.state, _doc(seqs=(1,)))
        hwc_relay.write_hwc_state(self.state, _doc(seqs=(1, 2)))
        self.assertEqual(len(hwc_relay.read_hwc_state(self.state)["events"]), 2)

    def test_missing_and_torn(self):
        self.assertIsNone(hwc_relay.read_hwc_state(self.state))
        full = json.dumps(_doc(seqs=(1, 2)))
        for torn in (full[:len(full) // 2], "", "\xff\xfe", "[1,2]", '{"v":1}',
                     '{"v":0,"epoch":"e"}', '{"v":true,"epoch":"e"}', '{"v":1,"epoch":5}',
                     '{"v":1,"epoch":"e","events":{}}'):
            with open(self.state, "w") as f:
                f.write(torn)
            self.assertIsNone(hwc_relay.read_hwc_state(self.state), repr(torn))
        with open(self.state, "wb") as f:
            f.write(b"\xff\xfe\x00")
        self.assertIsNone(hwc_relay.read_hwc_state(self.state))

    def test_future_version_with_additive_keys_accepted(self):
        hwc_relay.write_hwc_state(self.state, {"v": 2, "epoch": "e", "new_key": 1})
        self.assertEqual(hwc_relay.read_hwc_state(self.state)["new_key"], 1)

    def test_non_serialisable_doc_touches_nothing(self):
        with self.assertRaises(TypeError):
            hwc_relay.write_hwc_state(self.state, {"v": 1, "epoch": "e", "x": object()})
        with self.assertRaises(TypeError):
            hwc_relay.write_hwc_state(self.state, ["not", "a", "dict"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_unwritable_directory_raises_and_cleans(self):
        missing = os.path.join(self.dir, "nope", "hwc_state")
        with self.assertRaises(OSError):
            hwc_relay.write_hwc_state(missing, _doc())

    def test_replace_failure_removes_temp(self):
        # target is a directory -> os.replace fails after the temp file was written
        os.mkdir(self.state)
        with self.assertRaises(OSError):
            hwc_relay.write_hwc_state(self.state, _doc())
        self.assertEqual(self.leftovers(), [])

    def test_status_view(self):
        doc = _doc(seqs=(5,))
        view = hwc_relay.hwc_status_view(doc, now=doc["ts"] + 1.5)
        self.assertNotIn("events", view)
        self.assertEqual(view["age_sec"], 1.5)
        self.assertEqual(view["fsm"], "IQ_CAL")
        self.assertIn("events", doc)                  # input untouched
        self.assertIsNone(hwc_relay.hwc_status_view(None))
        self.assertIsNone(hwc_relay.hwc_status_view({"v": 1, "epoch": "e"})["age_sec"])
        self.assertEqual(hwc_relay.hwc_status_view(doc, now=doc["ts"] - 3)["age_sec"], 0.0)

    def test_concurrent_writers_and_reader(self):
        """Two writers x 1000 writes + a reader: every read is complete, no temp left."""
        n = 1000
        errors = []
        reads = {"ok": 0, "none": 0}
        stop = threading.Event()

        def writer(tag):
            try:
                for i in range(n):
                    hwc_relay.write_hwc_state(self.state, _doc(epoch=tag, seqs=(i,),
                                                               pad="x" * 2000))
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        def reader():
            while not stop.is_set():
                d = hwc_relay.read_hwc_state(self.state)
                if d is None:
                    reads["none"] += 1
                    continue
                if d["epoch"] not in ("w1", "w2") or len(d["pad"]) != 2000 \
                        or len(d["events"]) != 1:
                    errors.append(AssertionError("incomplete document read"))
                reads["ok"] += 1

        rt = threading.Thread(target=reader)
        w1 = threading.Thread(target=writer, args=("w1",))
        w2 = threading.Thread(target=writer, args=("w2",))
        rt.start()
        w1.start()
        w2.start()
        w1.join(120)
        w2.join(120)
        stop.set()
        rt.join(30)
        self.assertEqual(errors, [])
        self.assertGreater(reads["ok"], 0)
        self.assertEqual(self.leftovers(), [])
        final = hwc_relay.read_hwc_state(self.state)
        self.assertEqual(final["events"][0]["seq"], n - 1)


class TestEventDedupe(unittest.TestCase):

    def test_same_epoch_only_newer_seq(self):
        doc = _doc(epoch="A", seqs=(3, 4, 5))
        ev, epoch, last = hwc_relay.new_events_since(doc, "A", 4)
        self.assertEqual([e["seq"] for e in ev], [5])
        self.assertEqual((epoch, last), ("A", 5))
        ev, epoch, last = hwc_relay.new_events_since(doc, "A", 5)
        self.assertEqual((ev, epoch, last), ([], "A", 5))

    def test_epoch_change_all_new(self):
        doc = _doc(epoch="B", seqs=(0, 1))
        ev, epoch, last = hwc_relay.new_events_since(doc, "A", 1041)
        self.assertEqual([e["seq"] for e in ev], [0, 1])
        self.assertEqual((epoch, last), ("B", 1))

    def test_epoch_change_without_events(self):
        ev, epoch, last = hwc_relay.new_events_since(_doc(epoch="B"), "A", 7)
        self.assertEqual((ev, epoch, last), ([], "B", -1))
        # the first event of the new process (seq 0) is then new
        ev, _e, last = hwc_relay.new_events_since(_doc(epoch="B", seqs=(0,)), epoch, last)
        self.assertEqual(([e["seq"] for e in ev], last), ([0], 0))

    def test_none_doc_keeps_baseline(self):
        self.assertEqual(hwc_relay.new_events_since(None, "A", 3), ([], "A", 3))

    def test_bad_events_ignored(self):
        doc = _doc(epoch="A")
        doc["events"] = [{"seq": "x"}, 5, {"seq": True}, {"seq": 9, "event_type": "ok"}]
        ev, _e, last = hwc_relay.new_events_since(doc, "A", 0)
        self.assertEqual(ev, [{"seq": 9, "event_type": "ok"}])
        self.assertEqual(last, 9)


class TestHwcStateWatcher(_TmpDir):

    def test_baseline_suppresses_leftover(self):
        hwc_relay.write_hwc_state(self.state, _doc(epoch="old", seqs=(10, 11)))
        w = hwc_relay.HwcStateWatcher(self.state)
        self.assertEqual((w.last_epoch, w.last_seq), ("old", 11))
        doc, ev = w.poll()
        self.assertEqual(ev, [])
        self.assertEqual(doc["epoch"], "old")
        hwc_relay.write_hwc_state(self.state, _doc(epoch="old", seqs=(10, 11, 12)))
        _doc_, ev = w.poll()
        self.assertEqual([e["seq"] for e in ev], [12])
        _doc_, ev = w.poll()
        self.assertEqual(ev, [])

    def test_restart_new_epoch_replays_its_events_once(self):
        hwc_relay.write_hwc_state(self.state, _doc(epoch="old", seqs=(1041,)))
        w = hwc_relay.HwcStateWatcher(self.state)
        hwc_relay.write_hwc_state(self.state, _doc(epoch="new", seqs=(0, 1)))
        _d, ev = w.poll()
        self.assertEqual([e["seq"] for e in ev], [0, 1])
        _d, ev = w.poll()
        self.assertEqual(ev, [])

    def test_no_file_at_start_and_errors_counted(self):
        w = hwc_relay.HwcStateWatcher(self.state)
        self.assertEqual((w.last_epoch, w.last_seq), (None, -1))
        self.assertEqual(w.poll(), (None, []))
        self.assertEqual(w.errors, 1)
        hwc_relay.write_hwc_state(self.state, _doc(epoch="E", seqs=(0,)))
        _d, ev = w.poll()
        self.assertEqual([e["seq"] for e in ev], [0])
        with open(self.state, "w") as f:
            f.write("{torn")
        self.assertEqual(w.poll(), (None, []))
        self.assertEqual(w.errors, 2)
        self.assertEqual((w.last_epoch, w.last_seq), ("E", 0))   # baseline kept


class TestRecalRequest(_TmpDir):

    def test_write_read(self):
        ts = hwc_relay.write_recal_request(self.recal, "1238-1761900000123", 3, ts_ms=1761900001000)
        self.assertEqual(ts, 1761900001000)
        with open(self.recal) as f:
            self.assertEqual(f.read(), "1238-1761900000123 3 1761900001000 wire\n")
        self.assertEqual(hwc_relay.read_recal_request(self.recal),
                         ("1238-1761900000123", 3, 1761900001000, "wire"))
        self.assertEqual(self.leftovers(), [])

    def test_default_timestamp_and_reason(self):
        ts = hwc_relay.write_recal_request(self.recal, "e", 1)
        self.assertLess(abs(ts - time.time() * 1000), 60000)
        self.assertEqual(hwc_relay.read_recal_request(self.recal)[3], "wire")
        hwc_relay.write_recal_request(self.recal, "e", 2, reason="coordinator")
        self.assertEqual(hwc_relay.read_recal_request(self.recal)[1:4:2], (2, "coordinator"))

    def test_bad_arguments(self):
        for kw in ({"epoch": "a b", "gen": 1}, {"epoch": "", "gen": 1},
                   {"epoch": "e", "gen": -1}, {"epoch": "e", "gen": 1.5},
                   {"epoch": "e", "gen": True}, {"epoch": "e", "gen": 1, "reason": "two words"}):
            with self.assertRaises(ValueError, msg=kw):
                hwc_relay.write_recal_request(self.recal, **kw)
        self.assertFalse(os.path.exists(self.recal))

    def test_malformed_files(self):
        self.assertIsNone(hwc_relay.read_recal_request(self.recal))
        for bad in ("", "e 1 2", "e 1 2 wire extra", "e x 2 wire", "e 1 -2 wire", "e -1 2 wire",
                    "e 1٣ 2 wire"):
            with open(self.recal, "w", encoding="utf-8") as f:
                f.write(bad + "\n")
            self.assertIsNone(hwc_relay.read_recal_request(self.recal), repr(bad))
        with open(self.recal, "wb") as f:
            f.write(b"\xff 1 2 wire\n")
        self.assertIsNone(hwc_relay.read_recal_request(self.recal))


class TestRecalWatcher(_TmpDir):

    def test_leftover_at_start_is_baseline_not_acted(self):
        hwc_relay.write_recal_request(self.recal, "old", 7)
        w = hwc_relay.RecalWatcher(self.recal)
        self.assertEqual((w.last_epoch, w.last_gen), ("old", 7))
        self.assertIsNone(w.poll())
        self.assertIsNone(w.poll())

    def test_newer_gen_same_epoch_acts_once(self):
        hwc_relay.write_recal_request(self.recal, "E", 1)
        w = hwc_relay.RecalWatcher(self.recal)
        hwc_relay.write_recal_request(self.recal, "E", 2, ts_ms=5)
        self.assertEqual(w.poll(), ("E", 2, 5, "wire"))
        self.assertIsNone(w.poll())
        self.assertEqual((w.last_epoch, w.last_gen), ("E", 2))

    def test_older_or_equal_gen_ignored(self):
        hwc_relay.write_recal_request(self.recal, "E", 5)
        w = hwc_relay.RecalWatcher(self.recal)
        hwc_relay.write_recal_request(self.recal, "E", 4)
        self.assertIsNone(w.poll())
        hwc_relay.write_recal_request(self.recal, "E", 5)
        self.assertIsNone(w.poll())

    def test_new_epoch_gen_1_after_start_acts(self):
        # leftover from a previous run with a high generation; hw_controller restarts
        hwc_relay.write_recal_request(self.recal, "1000-1", 9)
        w = hwc_relay.RecalWatcher(self.recal)
        hwc_relay.write_recal_request(self.recal, "2000-2", 1, ts_ms=42)
        self.assertEqual(w.poll(), ("2000-2", 1, 42, "wire"))
        self.assertIsNone(w.poll())

    def test_no_file_at_start_then_request(self):
        w = hwc_relay.RecalWatcher(self.recal)
        self.assertEqual((w.last_epoch, w.last_gen), (None, -1))
        self.assertIsNone(w.poll())
        hwc_relay.write_recal_request(self.recal, "E", 1, ts_ms=1)
        self.assertEqual(w.poll(), ("E", 1, 1, "wire"))
        self.assertIsNone(w.poll())

    def test_file_removed_keeps_baseline(self):
        hwc_relay.write_recal_request(self.recal, "E", 1)
        w = hwc_relay.RecalWatcher(self.recal)
        os.unlink(self.recal)
        self.assertIsNone(w.poll())
        hwc_relay.write_recal_request(self.recal, "E", 1)
        self.assertIsNone(w.poll())                 # same (epoch, gen): already seen

    def test_injected_reader(self):
        seq = iter([None, ("E", 1, 0, "wire"), ("E", 1, 0, "wire")])
        w = hwc_relay.RecalWatcher("unused", reader=lambda p: next(seq))
        self.assertEqual(w.poll(), ("E", 1, 0, "wire"))
        self.assertIsNone(w.poll())


if __name__ == "__main__":
    unittest.main()
