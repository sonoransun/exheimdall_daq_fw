"""
    Description :
    hw_controller construction and init() for a federation instance N > 0
    without SDR hardware (spec B.2, B.9, D.6.2, G.3, G.4, WP-05):

      * HWC() + init() in a private temp cwd with instance_id = 1, no
        iq_track_lock file and a mocked shared-memory interface -> 0 (today
        init() returned -1 without the file, G1).
      * Lazy track lock: read only with require_track_lock_intervention = 1,
        re-opened on every check, missing file = "not approved" (logged
        once), and both an in-place write and an atomic replacement approve.
      * The main loop's track-lock gate driven with synthetic frames.
      * Per-instance control file names (inst1_ prefix), fence term loaded at
        construction, the :5001 port offset, apply_bias_tee_at_init, the
        EventBus `source=` and DAQDatabase instance kwargs with their
        TypeError fallbacks, and the relay document written by init().

    Hardware-free and CI-safe: the control server thread is never started,
    rtl_daq transactions are stubbed, every file lives in a temp directory.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest
from os.path import join, dirname, realpath
from unittest import mock

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

logging.basicConfig(level=logging.CRITICAL)

try:
    import zmq  # noqa: F401
except ImportError:  # same minimal stub as test_control_iface.py
    _zmq = types.ModuleType("zmq")
    _zmq.REQ, _zmq.LINGER, _zmq.RCVTIMEO, _zmq.SNDTIMEO = 3, 17, 27, 28

    class _ZMQError(Exception):
        pass

    class _Sock:
        def setsockopt(self, *a, **k):
            pass

        def connect(self, *a, **k):
            pass

        def close(self, *a, **k):
            pass

    class _Ctx:
        def socket(self, *a, **k):
            return _Sock()

        def term(self):
            pass

    _zmq.ZMQError, _zmq.Context = _ZMQError, _Ctx
    sys.modules["zmq"] = _zmq

import hw_controller  # noqa: E402
import hwc_relay  # noqa: E402
import inter_module_messages  # noqa: E402
from hw_controller import HWC, CtrIfaceServer  # noqa: E402
from iq_header import IQHeader  # noqa: E402
from shmemIface import TERMINATE  # noqa: E402

M = 4


def _ini_text(instance_id=1, port_stride=100, extra=None):
    sections = {
        "hw": {"num_ch": str(M), "en_bias_tee": "0,0,0,0"},
        "daq": {"log_level": "5", "center_freq": "433000000", "sample_rate": "2400000",
                "listen_address": "127.0.0.1"},
        "pre_processing": {"cpi_size": "1048576", "decimation_ratio": "1"},
        "calibration": {"cal_track_mode": "0", "maximum_sync_fails": "3",
                        "cal_frame_burst_size": "5", "cal_frame_interval": "50",
                        "gain_lock_interval": "0", "unified_gain_control": "1",
                        "en_gain_tune_init": "0", "require_track_lock_intervention": "0",
                        "en_iq_cal": "1"},
        "adpis": {"en_adpis": "0", "adpis_proc_size": "8192", "adpis_gains_init": "0,0,0,0"},
        "federation": {"instance_id": str(instance_id), "port_stride": str(port_stride)},
    }
    for section, values in (extra or {}).items():
        sections.setdefault(section, {}).update(values)
    lines = []
    for section, values in sections.items():
        lines.append("[{}]".format(section))
        lines.extend("{} = {}".format(k, v) for k, v in values.items())
        lines.append("")
    return "\n".join(lines)


class _FakeShmIface:
    """Stands in for inShmemIface: feeds encoded IQ headers to HWC.start()."""

    def __init__(self, shmem_name, instance_id=0, **_kw):
        self.shmem_name = shmem_name
        self.instance_id = instance_id
        self.init_ok = True
        self.buffers = [np.zeros(1024, dtype=np.uint8), np.zeros(1024, dtype=np.uint8)]
        self.headers = []
        self.released = []
        self.destroyed = False
        self.before_frame = None

    def wait_buff_free(self):
        if not self.headers:
            return TERMINATE
        if self.before_frame is not None:
            self.before_frame(len(self.released))
        header = self.headers.pop(0)
        self.buffers[0][:] = np.frombuffer(header.encode_header(), dtype=np.uint8)
        return 0

    def send_ctr_buff_ready(self, index):
        self.released.append(index)

    def destory_sm_buffer(self):
        self.destroyed = True


class _InstanceCase(unittest.TestCase):

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="hwc_inst_")
        self.addCleanup(shutil.rmtree, self.workdir, True)
        os.makedirs(join(self.workdir, "_data_control"))
        old_cwd = os.getcwd()
        os.chdir(self.workdir)
        self.addCleanup(os.chdir, old_cwd)
        hw_controller.ctr_request_queue.queue.clear()

    def make_hwc(self, instance_id=1, extra=None):
        with open("daq_chain_config.ini", "w") as f:
            f.write(_ini_text(instance_id=instance_id, extra=extra))
        with mock.patch.object(CtrIfaceServer, "start"):
            hwc = HWC()
        self.addCleanup(hwc.ctr_iface_server.ctr_iface_socket.close)
        if hwc.event_bus is not None:
            self.addCleanup(hwc.event_bus.close)
        hwc.transactions = []

        def _ok(msg):
            hwc.transactions.append(bytes(msg))
            return b"OK"

        hwc._rtl_daq_transaction = _ok
        return hwc

    def init_hwc(self, hwc):
        with mock.patch.object(hw_controller, "inShmemIface", _FakeShmIface):
            rc = hwc.init()
        self.addCleanup(self._close_zmq, hwc)
        return rc

    @staticmethod
    def _close_zmq(hwc):
        try:
            hwc.rtl_daq_socket.close(linger=0)
            hwc.zmq_context.term()
        except Exception:
            pass


class TesterInitInstance(_InstanceCase):

    def test_init_without_track_lock_file(self):
        hwc = self.make_hwc()
        self.assertFalse(os.path.exists("_data_control/inst1_iq_track_lock"))
        self.assertEqual(self.init_hwc(hwc), 0)
        self.assertEqual(hwc.in_shmem_iface.shmem_name, "delay_sync_hwc")
        self.assertEqual(hwc.in_shmem_iface.instance_id, 1)
        self.assertEqual(hwc.zmq_port, 1130 + 100)
        self.assertEqual(hwc.ctr_iface_server.ctr_iface_port_no, 5001 + 100)
        hwc.close()
        self.assertTrue(hwc.in_shmem_iface.destroyed)

    def test_prefixed_control_paths(self):
        hwc = self.make_hwc()
        self.assertEqual(hwc.track_lock_ctr_fname, "_data_control/inst1_iq_track_lock")
        self.assertEqual(hwc._orientation_state_fname, "_data_control/inst1_orientation_state")
        self.assertEqual(hwc._bias_tee_state_fname, "_data_control/inst1_bias_tee_state")
        self.assertEqual(hwc.fence_term_fname, "_data_control/inst1_fence_term")
        self.assertEqual(hwc.hwc_state_fname, os.path.join("_data_control", "inst1_hwc_state"))
        self.assertEqual(hwc.recal_request_fname, os.path.join("_data_control", "inst1_recal_request"))

    def test_instance_zero_paths_unprefixed(self):
        hwc = self.make_hwc(instance_id=0)
        self.assertEqual(hwc.track_lock_ctr_fname, "_data_control/iq_track_lock")
        self.assertEqual(hwc.fence_term_fname, "_data_control/fence_term")
        self.assertEqual(hwc.ctr_iface_server.ctr_iface_port_no, 5001)

    def test_fence_term_loaded_at_construction(self):
        with open("_data_control/inst1_fence_term", "w") as f:
            f.write("42\n")
        with open("_data_control/fence_term", "w") as f:
            f.write("7\n")   # instance 0's file must not be read by instance 1
        hwc = self.make_hwc()
        self.assertEqual(hwc.ctl.max_term_seen, 42)

    def test_init_publishes_snapshot(self):
        hwc = self.make_hwc()
        self.assertEqual(self.init_hwc(hwc), 0)
        hwsq = hwc.ctl.hwsq()
        self.assertEqual((hwsq["fsm"], hwsq["nch"], hwsq["fage"]), ("INIT", M, -1))
        self.assertEqual(hwc.ctl.fp_ms, 437)


class TesterLazyTrackLock(_InstanceCase):

    def test_missing_file_not_approved_logged_once(self):
        hwc = self.make_hwc(extra={"calibration": {"require_track_lock_intervention": "1"}})
        self.assertEqual(self.init_hwc(hwc), 0)
        with self.assertLogs("hw_controller", level="ERROR") as logs:
            self.assertFalse(hwc._track_lock_approved())
            self.assertFalse(hwc._track_lock_approved())
        self.assertEqual(len([r for r in logs.records if "track lock" in r.getMessage()]), 1)

    def test_in_place_and_atomic_writes_both_approve(self):
        hwc = self.make_hwc(extra={"calibration": {"require_track_lock_intervention": "1"}})
        path = "_data_control/inst1_iq_track_lock"
        with open(path, "w") as f:
            f.write("0")
        self.assertFalse(hwc._track_lock_approved())
        with open(path, "r+") as f:          # operator: echo 1 > file (in place)
            f.write("1\n")
        self.assertTrue(hwc._track_lock_approved())
        tmp = path + ".new"
        with open(tmp, "w") as f:
            f.write("0\n")
        os.replace(tmp, path)                # atomic replacement
        self.assertFalse(hwc._track_lock_approved())
        with open(tmp, "w") as f:
            f.write("1\n")
        os.replace(tmp, path)
        self.assertTrue(hwc._track_lock_approved())

    def _locked_frame(self):
        header = IQHeader()
        header.frame_type = IQHeader.FRAME_TYPE_DATA
        header.sync_state = 5
        header.noise_source_state = 1
        header.if_gains = [0] * 32
        return header

    def test_main_loop_waits_for_approval(self):
        """Locked delay_sync with the noise source on: without approval the
        noise source stays on; writing 1 into the file releases it."""
        hwc = self.make_hwc(extra={"calibration": {"require_track_lock_intervention": "1"}})
        self.assertEqual(self.init_hwc(hwc), 0)
        hwc.current_state = "STATE_IQ_CAL"
        hwc.noise_source_state = True
        shm = hwc.in_shmem_iface
        shm.headers = [self._locked_frame() for _ in range(4)]
        noise_off = inter_module_messages.pack_msg_noise_source_ctr(hwc.module_identifier, False)

        def _before(frame_no):
            if frame_no == 2:
                with open("_data_control/inst1_iq_track_lock", "w") as f:
                    f.write("1\n")
            if frame_no == 1:
                self.assertTrue(hwc.noise_source_state, "not approved yet")

        shm.before_frame = _before
        hwc.start()
        self.assertFalse(hwc.noise_source_state)
        self.assertIn(bytes(noise_off), hwc.transactions)
        self.assertEqual(shm.released, [0, 0, 0, 0])
        self.assertEqual(hwc.ctl.hwsq()["fc"], 4)

    def test_file_not_read_without_intervention(self):
        hwc = self.make_hwc()
        self.assertEqual(self.init_hwc(hwc), 0)
        hwc.current_state = "STATE_IQ_CAL"
        hwc.noise_source_state = True
        hwc.in_shmem_iface.headers = [self._locked_frame()]
        with mock.patch.object(hwc.ctl, "read_track_lock", side_effect=AssertionError("read")):
            hwc.start()
        self.assertFalse(hwc.noise_source_state)


class TesterInitOptions(_InstanceCase):

    def _amp(self, apply):
        amp = {"en_amplification": "1", "en_bias_tee_runtime": "1", "bias_tee_state": "1,0,1,0"}
        if apply is not None:
            amp["apply_bias_tee_at_init"] = apply
        return {"amplification": amp}

    def test_apply_bias_tee_at_init_sends_once(self):
        hwc = self.make_hwc(extra=self._amp("1"))
        self.assertEqual(self.init_hwc(hwc), 0)
        expected = inter_module_messages.pack_msg_set_bias_tee(hwc.module_identifier, [1, 0, 1, 0])
        self.assertEqual(hwc.transactions, [bytes(expected)])
        with open("_data_control/inst1_bias_tee_state") as f:
            self.assertEqual(f.read(), "5")

    def test_bias_tee_compared_with_all_off_when_hw_key_empty(self):
        extra = self._amp("1")
        extra["hw"] = {"en_bias_tee": ""}
        hwc = self.make_hwc(extra=extra)
        self.assertEqual(hwc.hw_bias_tee, [0, 0, 0, 0])
        self.assertEqual(self.init_hwc(hwc), 0)
        self.assertEqual(len(hwc.transactions), 1)

    def test_bias_tee_not_sent_by_default_or_when_equal(self):
        hwc = self.make_hwc(extra=self._amp(None))
        self.assertEqual(self.init_hwc(hwc), 0)
        self.assertEqual(hwc.transactions, [])
        extra = self._amp("1")
        extra["hw"] = {"en_bias_tee": "1,0,1,0"}
        hwc = self.make_hwc(extra=extra)
        self.assertEqual(self.init_hwc(hwc), 0)
        self.assertEqual(hwc.transactions, [])

    def test_init_writes_relay_document(self):
        hwc = self.make_hwc(extra={"monitoring": {"en_monitoring": "0", "en_hwc_state_relay": "1"}})
        self.assertEqual(self.init_hwc(hwc), 0)
        doc = hwc_relay.read_hwc_state("_data_control/inst1_hwc_state")
        self.assertEqual((doc["v"], doc["epoch"], doc["fsm"], doc["events"]),
                         (1, hwc.epoch, "INIT", []))
        self.assertEqual(doc["pid"], os.getpid())

    def test_event_bus_source_identity(self):
        captured = {}

        class _Bus:
            def __init__(self, enabled=False, ring_size=500, queue_size=2000, source=None):
                captured["source"] = source

            def register_handler(self, handler):
                pass

            def close(self):
                pass

        import daq_events
        with mock.patch.object(daq_events, "EventBus", _Bus):
            self.make_hwc(extra={"monitoring": {"en_monitoring": "1", "event_tag_identity": "1"},
                                 "federation": {"node_id": "rx-a:1", "cluster_id": "lab"}})
        source = captured["source"]
        self.assertEqual((source["instance_id"], source["node_id"], source["cluster_id"]),
                         (1, "rx-a:1", "lab"))
        self.assertIn("host", source)

    def test_event_bus_old_signature_fallback(self):
        class _OldBus:
            def __init__(self, enabled=False, ring_size=500, queue_size=2000):
                pass

            def register_handler(self, handler):
                pass

            def close(self):
                pass

        import daq_events
        with mock.patch.object(daq_events, "EventBus", _OldBus):
            hwc = self.make_hwc(extra={"monitoring": {"en_monitoring": "1", "event_tag_identity": "1"}})
        self.assertIsInstance(hwc.event_bus, _OldBus)

    def _fake_db_module(self, accepts_new_kwargs):
        calls = []

        class DAQDatabase:
            def __init__(self, **kwargs):
                if not accepts_new_kwargs and "instance_id" in kwargs:
                    raise TypeError("unexpected keyword argument 'instance_id'")
                calls.append(kwargs)
                self.event_bus = None

            def close(self):
                pass

        module = types.ModuleType("daq_db")
        module.DAQDatabase = DAQDatabase
        return module, calls

    def test_database_instance_kwargs(self):
        module, calls = self._fake_db_module(True)
        with mock.patch.dict(sys.modules, {"daq_db": module}):
            self.make_hwc(extra={"database": {"en_db": "1", "instance_subdir": "1"}})
        self.assertEqual(calls[0]["instance_id"], 1)
        self.assertTrue(calls[0]["instance_subdir"])
        self.assertFalse(calls[0]["write_meta"])

    def test_database_old_signature_fallback(self):
        module, calls = self._fake_db_module(False)
        with mock.patch.dict(sys.modules, {"daq_db": module}):
            hwc = self.make_hwc(extra={"database": {"en_db": "1"}})
        self.assertNotIn("instance_id", calls[0])
        self.assertIsNotNone(hwc.db)


if __name__ == "__main__":
    unittest.main()
