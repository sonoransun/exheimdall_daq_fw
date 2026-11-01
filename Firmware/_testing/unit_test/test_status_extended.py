"""
    Description :
    delay_sync extension helpers driven with synthetic headers and a real
    delaySynchronizer built from a temporary daq_chain_config.ini (no FIFOs,
    no shared memory, no C binaries, no sudo):

      * input-header continuity counters, slot 102/109 pickup
      * the extended STATUS snapshot ([monitoring] status_extended) and the
        snapshot refresh cadence ([monitoring] status_update_interval)
      * the hw_controller relays: hwc_state merge, event re-emission deduped
        by (epoch, seq), and a leftover file / leftover recal generation that
        must never be replayed; a new epoch with gen = 1 restarts the
        calibration exactly once ([calibration] en_wire_recal)
      * the FederationHealth construction gate, mesh-peer rule, registrar
        wiring and the legacy-constructor fallback
      * header stamping (slots 108, 110-113) and slot-109 carry-through
      * the inherited instance-lock check

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import json
import logging
import os
import sys
import tempfile
import time
import types
import unittest
import zlib
from os.path import join, dirname, realpath


current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)


def _install_numba_stub_if_missing():
    try:
        import numba  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("numba")

    def _decorator(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def wrap(func):
            return func
        return wrap

    stub.jit = _decorator
    stub.njit = _decorator
    sys.modules["numba"] = stub


def _install_zmq_stub_if_missing():
    try:
        import zmq  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("zmq")
    stub.REQ = 3
    stub.LINGER = 17
    stub.RCVTIMEO = 27
    stub.SNDTIMEO = 28
    stub.ZMQError = type("ZMQError", (Exception,), {})
    stub.Context = type("Context", (), {"socket": lambda self, *a: None,
                                        "term": lambda self: None})
    sys.modules["zmq"] = stub


_install_numba_stub_if_missing()
_install_zmq_stub_if_missing()

import delay_sync  # noqa: E402
import hwc_relay  # noqa: E402
from iq_header import IQHeader  # noqa: E402
from daq_db_records import CAL_EVENT_CAL_START  # noqa: E402

BASE_INI = {
    "meta": {"ini_version": "8"},
    "hw": {"num_ch": "4"},
    "daq": {"log_level": "4", "center_freq": "433000000", "sample_rate": "2400000"},
    "pre_processing": {"cpi_size": "4096", "decimation_ratio": "1"},
    "calibration": {"corr_size": "4096", "std_ch_ind": "0", "amplitude_tolerance": "2",
                    "phase_tolerance": "2", "cal_track_mode": "1",
                    "maximum_sync_fails": "3", "amplitude_cal_mode": "channel_power",
                    "en_iq_cal": "0", "iq_adjust_source": "explicit-time-delay",
                    "iq_adjust_amplitude": "0,0,0", "iq_adjust_time_delay_ns": "0,0,0"},
}


def ini_text(overrides=None):
    sections = {k: dict(v) for k, v in BASE_INI.items()}
    for section, keys in (overrides or {}).items():
        sections.setdefault(section, {}).update(keys)
    out = []
    for section, keys in sections.items():
        out.append("[{}]".format(section))
        out.extend("{} = {}".format(k, v) for k, v in keys.items())
        out.append("")
    return "\n".join(out)


class FakeOut(object):
    """Stand-in for an output shm interface (only the drop counter is read)."""
    dropped_frame_cntr = 0


class FakeBus(object):
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def of_type(self, event_type):
        return [e for e in self.events if e.event_type == event_type]


class FakeDB(object):
    def __init__(self):
        self.cal_events = []

    def put_cal_event(self, event_type, iq_header, **kwargs):
        self.cal_events.append((event_type, kwargs))


class DSCase(unittest.TestCase):
    """Runs each test in a temp cwd holding the INI and _data_control/."""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="heimdall_status_ext_")
        os.chdir(self.tmp)
        os.mkdir("_data_control")

    def tearDown(self):
        os.chdir(self._old_cwd)

    def build(self, overrides=None):
        with open("daq_chain_config.ini", "w") as f:
            f.write(ini_text(overrides))
        logging.disable(logging.CRITICAL)
        try:
            ds = delay_sync.delaySynchronizer()
        finally:
            logging.disable(logging.NOTSET)
        if ds.event_bus is not None:
            self.addCleanup(ds.event_bus.close)
        ds.out_shmem_iface_iq = FakeOut()
        ds.out_shmem_iface_hwc = FakeOut()
        return ds


def header(cpi=0, block=0, overruns=0, decimator_drops=0, frame_type=0):
    h = IQHeader()
    h.frame_type = frame_type
    h.cpi_index = cpi
    h.daq_block_index = block
    h.set_buffer_overrun_cnt(overruns)
    h.set_decimator_drop_cnt(decimator_drops)
    h.header_version = 8
    return h


# ----------------------------------------------------------------------
# Continuity counters
# ----------------------------------------------------------------------

class TestContinuity(DSCase):

    def test_cpi_and_block_gap_counting(self):
        ds = self.build({"monitoring": {"status_extended": "1"}})
        self.assertTrue(ds._track_continuity)
        for cpi, block in ((0, 0), (1, 3), (2, 6), (5, 9), (6, 4), (7, 12), (7, 12)):
            ds._update_continuity(header(cpi, block))
        # gaps: 2->5 and 7->7 (cpi); 9->4 regression (block)
        self.assertEqual(ds._cpi_gaps, 2)
        self.assertEqual(ds._daq_block_gaps, 1)

    def test_slot_102_and_109_pickup(self):
        ds = self.build({"monitoring": {"status_extended": "1",
                                        "stamp_decimator_drops": "1"}})
        ds.iq_header = header(3, 9, overruns=4, decimator_drops=11)
        counters = ds._extended_counters()
        self.assertEqual(counters["buffer_overrun_cnt"], 4)
        self.assertEqual(counters["decimator_drops"], 11)
        self.assertEqual(counters["overrun_invalidated_frames"], 0)

    def test_decimator_drops_omitted_unless_stamped(self):
        ds = self.build({"monitoring": {"status_extended": "1"}})
        ds.iq_header = header(decimator_drops=11)
        self.assertNotIn("decimator_drops", ds._extended_counters())

    def test_continuity_off_by_default(self):
        ds = self.build()
        self.assertFalse(ds._track_continuity)


# ----------------------------------------------------------------------
# Extended STATUS snapshot and refresh cadence
# ----------------------------------------------------------------------

_LEGACY_SNAPSHOT_KEYS = {"sync_state", "current_frequency_hz", "frame_count",
                         "aggregate_power_db", "calibration_status", "counters"}
_LEGACY_COUNTER_KEYS = {"dropped_frames_iq", "dropped_frames_hwc", "sync_fails_total",
                        "sample_compensations", "iq_compensations"}


class RecordingStatusServer(object):
    def __init__(self):
        self.snapshots = []

    def update_status(self, snapshot):
        self.snapshots.append(snapshot)

    def get_pipeline_health(self):
        return "ok"


class TestExtendedSnapshot(DSCase):

    def test_default_snapshot_is_todays(self):
        ds = self.build()
        snap = ds._build_status_snapshot(5)
        self.assertEqual(set(snap), _LEGACY_SNAPSHOT_KEYS)
        self.assertEqual(set(snap["counters"]), _LEGACY_COUNTER_KEYS)
        self.assertEqual(list(snap["counters"]), ["dropped_frames_iq", "dropped_frames_hwc",
                                                  "sync_fails_total", "sample_compensations",
                                                  "iq_compensations"])

    def test_extended_snapshot_keys(self):
        ds = self.build({"monitoring": {"status_extended": "1"},
                         "federation": {"node_id": "lab-node:0", "cluster_id": "lab"}})
        ds.iq_header = header(cpi=42, block=7)
        ds.iq_header.time_stamp = 1761900000123
        ds._cal_quality = 31.25
        snap = ds._build_status_snapshot(2)
        for key in ("node_id", "cluster_id", "hostname", "pid", "fw_version", "ini_version",
                    "config_sha256", "num_channels", "sampling_freq_hz", "cpi_length",
                    "decimation_ratio", "frame_period_sec", "frame_type", "cpi_index",
                    "last_frame_ts_ms", "fsm_state", "cal"):
            self.assertIn(key, snap)
        self.assertTrue(_LEGACY_SNAPSHOT_KEYS <= set(snap))
        self.assertEqual(snap["node_id"], "lab-node:0")
        self.assertEqual(snap["cluster_id"], "lab")
        self.assertEqual(snap["pid"], os.getpid())
        self.assertEqual(snap["ini_version"], 8)
        self.assertEqual(len(snap["config_sha256"]), 64)
        self.assertEqual(snap["num_channels"], 4)
        self.assertEqual(snap["sampling_freq_hz"], 2400000)
        self.assertAlmostEqual(snap["frame_period_sec"], 4096 / 2.4e6, places=6)
        self.assertEqual(snap["cpi_index"], 42)
        self.assertEqual(snap["last_frame_ts_ms"], 1761900000123)
        self.assertEqual(snap["fsm_state"], "INIT")
        self.assertEqual(snap["cal"], {"quality_db": 31.25, "iq_compensations": 0,
                                       "sample_compensations": 0, "track_mode": 1,
                                       "recal_generation": 0})
        self.assertTrue(_LEGACY_COUNTER_KEYS <= set(snap["counters"]))
        for key in ("buffer_overrun_cnt", "cpi_gaps", "daq_block_gaps",
                    "overrun_invalidated_frames"):
            self.assertIn(key, snap["counters"])
        self.assertNotIn("federation", snap)
        json.dumps(snap)   # JSON-serialisable

    def test_default_cadence_follows_heartbeat(self):
        ds = self.build({"monitoring": {"heartbeat_interval": "4"}})
        ds.status_server = RecordingStatusServer()
        ds.iq_header = header()
        for _ in range(12):
            ds._heartbeat_and_status(0)
        self.assertEqual(len(ds.status_server.snapshots), 3)

    def test_status_update_interval_independent_of_heartbeat(self):
        ds = self.build({"monitoring": {"heartbeat_interval": "0",
                                        "status_update_interval": "3"}})
        ds.status_server = RecordingStatusServer()
        ds.iq_header = header()
        for _ in range(9):
            ds._heartbeat_and_status(0)
        self.assertEqual(len(ds.status_server.snapshots), 3)

    def test_extended_refresh_on_change(self):
        ds = self.build({"monitoring": {"heartbeat_interval": "100",
                                        "status_extended": "1"}})
        ds.status_server = RecordingStatusServer()
        ds.iq_header = header()
        ds.iq_header.rf_center_freq = 433000000
        ds._heartbeat_and_status(1)          # first frame: key changed
        ds._heartbeat_and_status(1)
        ds._heartbeat_and_status(2)          # sync_state change
        ds.iq_header.frame_type = IQHeader.FRAME_TYPE_CAL
        ds._heartbeat_and_status(2)          # frame_type change
        ds.iq_header.rf_center_freq = 868000000
        ds._heartbeat_and_status(2)          # frequency change
        ds._heartbeat_and_status(2)
        self.assertEqual([s["sync_state"] for s in ds.status_server.snapshots], [1, 2, 2, 2])

    def test_stale_threshold_passed_to_server(self):
        ds = self.build({"monitoring": {"en_monitoring": "0", "en_status_server": "1",
                                        "status_server_port": "0", "status_extended": "1",
                                        "status_update_interval": "10"}})
        # 3 * 10 * (4096 / 2.4e6) + 1
        self.assertAlmostEqual(ds.status_server.stale_after_sec, 30 * 4096 / 2.4e6 + 1.0)


# ----------------------------------------------------------------------
# hw_controller relays
# ----------------------------------------------------------------------

def hwc_doc(epoch, seqs, fsm="IQ_CAL", ts=None):
    return {"v": 1, "epoch": epoch, "seq": max(seqs) if seqs else 0,
            "ts": time.time() if ts is None else ts, "pid": 1, "fsm": fsm,
            "events": [{"seq": s, "timestamp": 1000.0 + s, "severity": "info",
                        "module": "hw_controller", "event_type": "freq_change",
                        "payload": {"n": s}} for s in seqs]}


class TestHwcRelay(DSCase):

    OVERRIDES = {"monitoring": {"status_extended": "1", "en_hwc_state_relay": "1"}}

    def _build(self):
        ds = self.build(self.OVERRIDES)
        ds.event_bus = FakeBus()
        return ds

    def test_leftover_file_is_not_replayed(self):
        hwc_relay.write_hwc_state("_data_control/hwc_state", hwc_doc("1-100", [1, 2, 3]))
        ds = self._build()
        ds._poll_relays()
        self.assertEqual(ds.event_bus.events, [])
        view, error = ds._hwc_status()
        self.assertIsNone(error)
        self.assertEqual(view["fsm"], "IQ_CAL")
        self.assertNotIn("events", view)
        self.assertIn("age_sec", view)

    def test_new_events_reemitted_once(self):
        hwc_relay.write_hwc_state("_data_control/hwc_state", hwc_doc("1-100", [1, 2]))
        ds = self._build()
        hwc_relay.write_hwc_state("_data_control/hwc_state", hwc_doc("1-100", [1, 2, 3, 4]))
        ds._poll_relays()
        ds._poll_relays()                    # same document: nothing new
        self.assertEqual([e.payload["n"] for e in ds.event_bus.events], [3, 4])
        ev = ds.event_bus.events[0]
        self.assertEqual(ev.module, "hw_controller")
        self.assertEqual(ev.event_type, "freq_change")
        self.assertTrue(ev.payload["relayed"])
        self.assertEqual(ev.timestamp, 1003.0)

    def test_epoch_change_reemits_everything(self):
        hwc_relay.write_hwc_state("_data_control/hwc_state", hwc_doc("1-100", [5, 6]))
        ds = self._build()
        hwc_relay.write_hwc_state("_data_control/hwc_state", hwc_doc("2-200", [0, 1]))
        ds._poll_relays()
        self.assertEqual([e.payload["n"] for e in ds.event_bus.events], [0, 1])

    def test_torn_file_counts_error(self):
        ds = self._build()
        with open("_data_control/hwc_state", "w") as f:
            f.write('{"v": 1, "epoch": "1-1", "events": [')
        ds._poll_relays()
        view, error = ds._hwc_status()
        self.assertIsNone(view)
        self.assertIn("unreadable", error)
        ds.iq_header = header()
        self.assertGreaterEqual(ds._extended_counters()["hwc_relay_errors"], 1)

    def test_tick_polls_every_fifth_frame(self):
        ds = self._build()
        polls = []
        ds._poll_relays = lambda: polls.append(1)
        for _ in range(10):
            ds._tick_relays()
        self.assertEqual(len(polls), 2)

    def test_instance_prefix(self):
        ds = self.build({"monitoring": {"en_hwc_state_relay": "1"},
                         "federation": {"instance_id": "2"}})
        self.assertEqual(ds._hwc_watcher.path, os.path.join("_data_control", "inst2_hwc_state"))

    def test_relay_disabled_by_default(self):
        ds = self.build()
        self.assertIsNone(ds._hwc_watcher)
        self.assertIsNone(ds._recal_watcher)


class TestWireRecal(DSCase):

    def _build(self):
        ds = self.build({"calibration": {"en_wire_recal": "1"}})
        ds.event_bus = FakeBus()
        ds.db = FakeDB()
        ds.current_state = "STATE_TRACK"
        ds.sync_failed_cntr = 2
        ds._last_sync_state = 6
        return ds

    def test_leftover_generation_is_ignored(self):
        hwc_relay.write_recal_request("_data_control/recal_request", "1-100", 5)
        ds = self._build()
        ds._poll_relays()
        self.assertEqual(ds.current_state, "STATE_TRACK")
        self.assertEqual(ds.event_bus.events, [])

    def test_new_epoch_triggers_init_once(self):
        hwc_relay.write_recal_request("_data_control/recal_request", "1-100", 5)
        ds = self._build()
        hwc_relay.write_recal_request("_data_control/recal_request", "2-200", 1)
        ds._poll_relays()
        self.assertEqual(ds.current_state, "STATE_INIT")
        self.assertEqual(ds.sync_failed_cntr, 0)
        self.assertEqual(ds._recal_generation, 1)
        starts = ds.event_bus.of_type("cal_start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].payload, {"reason": "wire", "generation": 1})
        self.assertEqual(ds.db.cal_events,
                         [(CAL_EVENT_CAL_START, {"sync_state_before": 6, "sync_state_after": 1})])
        # The same request again: no second restart
        ds.current_state = "STATE_TRACK"
        ds._poll_relays()
        self.assertEqual(ds.current_state, "STATE_TRACK")
        self.assertEqual(len(ds.event_bus.of_type("cal_start")), 1)
        # A higher generation in the same epoch acts again
        hwc_relay.write_recal_request("_data_control/recal_request", "2-200", 2)
        ds._poll_relays()
        self.assertEqual(ds.current_state, "STATE_INIT")
        self.assertEqual(ds._recal_generation, 2)

    def test_no_file_at_start_then_request(self):
        ds = self._build()
        ds._poll_relays()
        self.assertEqual(ds.current_state, "STATE_TRACK")
        hwc_relay.write_recal_request("_data_control/recal_request", "3-300", 0)
        ds._poll_relays()
        self.assertEqual(ds.current_state, "STATE_INIT")


# ----------------------------------------------------------------------
# FederationHealth gate and wiring
# ----------------------------------------------------------------------

class RecordingFH(object):
    """FederationHealth with the extended (keyword) constructor."""
    created = []

    def __init__(self, instance_id, peer_addresses, poll_interval=5.0, event_bus=None,
                 status_base_port=5002, port_stride=100, **kwargs):
        self.args = dict(instance_id=instance_id, peer_addresses=peer_addresses,
                         poll_interval=poll_interval, event_bus=event_bus,
                         status_base_port=status_base_port, port_stride=port_stride)
        self.kwargs = kwargs
        self.call = None
        RecordingFH.created.append(self)

    def get_peer_table(self):
        return {"hostb:0": {"alive": True, "instance_id": 0}}

    def get_coordinator_id(self):
        return 0

    def get_coordinator_status(self):
        return {"endpoint": "hostc:6000", "role": "leader", "term": 7, "registered": True,
                "last_register_ok_age_sec": 1.5, "last_error": None, "ignored": 1,
                "endpoints": {"hostc:6000": {"role": "leader"}}}


class LegacyFH(RecordingFH):
    """Today's FederationHealth signature: no new keyword arguments."""

    def __init__(self, instance_id, peer_addresses, poll_interval=5.0, event_bus=None,
                 status_base_port=5002, port_stride=100):
        RecordingFH.__init__(self, instance_id, peer_addresses, poll_interval, event_bus,
                             status_base_port, port_stride)


class TestFederationGate(DSCase):

    def setUp(self):
        DSCase.setUp(self)
        RecordingFH.created = []
        self._saved = sys.modules.get("federation_health")
        self.use(RecordingFH)

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("federation_health", None)
        else:
            sys.modules["federation_health"] = self._saved
        DSCase.tearDown(self)

    def use(self, cls):
        mod = types.ModuleType("federation_health")
        mod.FederationHealth = cls
        sys.modules["federation_health"] = mod

    def test_disabled_without_federation(self):
        self.assertIsNone(self.build().federation_health)
        self.assertIsNone(self.build({"federation": {"en_federation": "0",
                                                     "peer_list": "hostb:0"}}).federation_health)
        self.assertIsNone(self.build({"federation": {"en_federation": "1"}}).federation_health)
        self.assertEqual(RecordingFH.created, [])

    def test_peer_list_only_is_todays_call(self):
        ds = self.build({"federation": {"en_federation": "1",
                                        "peer_list": "hostb:0, hostc:5102"}})
        fh = ds.federation_health
        self.assertIsNotNone(fh)
        self.assertEqual(fh.args["peer_addresses"], ["hostb:0", "hostc:5102"])
        self.assertEqual(fh.args["poll_interval"], 5.0)
        self.assertEqual(fh.args["status_base_port"], 5002)
        self.assertEqual(fh.kwargs, {})

    def test_new_keys_become_kwargs(self):
        ds = self.build({"federation": {"en_federation": "1", "peer_list": "hostb:0",
                                        "health_poll_interval": "2.5", "peer_timeout": "1.0",
                                        "peer_down_misses": "3", "health_workers": "4",
                                        "emit_peer_health_events": "1",
                                        "auth_token": "s3cret", "node_id": "n1:0"},
                         "host": {"health_thread_sched_other": "1"}})
        fh = ds.federation_health
        self.assertEqual(fh.args["poll_interval"], 2.5)
        self.assertEqual(fh.kwargs, {"peer_timeout": 1.0, "down_misses": 3, "max_workers": 4,
                                     "emit_health_events": True, "auth_token": "s3cret",
                                     "node_id": "n1:0", "sched_other": True})

    def test_cluster_members_only_excludes_self(self):
        ds = self.build({"federation": {"en_federation": "1", "advertise_host": "hosta",
                                        "cluster_members": "hosta:0,hostb:0,hosta:1@a1"}})
        fh = ds.federation_health
        self.assertIsNotNone(fh)
        self.assertEqual(fh.args["peer_addresses"], [])
        self.assertEqual([m["node_key"] for m in fh.kwargs["members"]], ["hostb:0", "hosta:1"])

    def test_cluster_members_without_self_polls_everything(self):
        ds = self.build({"federation": {"en_federation": "1", "advertise_host": "hosta",
                                        "cluster_members": "hostb:0,hostc:0"}})
        self.assertEqual([m["node_key"] for m in ds.federation_health.kwargs["members"]],
                         ["hostb:0", "hostc:0"])

    def test_peer_list_wins_over_cluster_members(self):
        ds = self.build({"federation": {"en_federation": "1", "peer_list": "hostz:3",
                                        "cluster_members": "hostb:0,hostc:0"}})
        fh = ds.federation_health
        self.assertEqual(fh.args["peer_addresses"], ["hostz:3"])
        self.assertNotIn("members", fh.kwargs)

    def test_register_only(self):
        ds = self.build({"federation": {"en_federation": "1", "instance_id": "1",
                                        "advertise_host": "hosta", "cluster_id": "lab",
                                        "register_with_coordinator": "1",
                                        "coordinator_host": "hostc",
                                        "register_interval_sec": "2.0"},
                         "monitoring": {"zmq_pub_port": "5003"}})
        fh = ds.federation_health
        reg = fh.kwargs["register"]
        self.assertEqual(reg["endpoints"], [("hostc", 6000)])
        self.assertEqual(reg["interval"], 2.0)
        self.assertFalse(reg["want_peers"])
        body = reg["body_fn"]()
        self.assertEqual(body["node_key"], "hosta:1")
        self.assertEqual(body["instance_id"], 1)
        self.assertEqual(body["cluster_id"], "lab")
        self.assertEqual(body["advertise_host"], "hosta")
        self.assertEqual(body["ports"], {"iq": 5100, "ctl": 5101, "status": 5102,
                                         "events": 5103, "zmq": 1230})
        pid, start_ms, sha = body["nonce"].split("-")
        self.assertEqual(int(pid), os.getpid())
        self.assertEqual(sha, ds.config_sha256[:12])
        self.assertIsNone(body["health"])          # no status server
        self.assertFalse(body["want_peers"])
        json.dumps(body)

    def test_register_candidates_and_hub_mode(self):
        ds = self.build({"federation": {"en_federation": "1", "advertise_host": "hosta",
                                        "register_with_coordinator": "1",
                                        "coordinator_candidates": "c1:6000, c2:6100",
                                        "health_mode": "hub", "peer_list": "hostb:0"}})
        fh = ds.federation_health
        self.assertEqual(fh.kwargs["register"]["endpoints"], [("c1", 6000), ("c2", 6100)])
        self.assertTrue(fh.kwargs["register"]["want_peers"])
        self.assertEqual(fh.args["peer_addresses"], [])    # hub: no polling

    def test_register_without_endpoint_is_disabled(self):
        ds = self.build({"federation": {"en_federation": "1", "advertise_host": "hosta",
                                        "register_with_coordinator": "1"}})
        self.assertIsNotNone(ds.federation_health)
        self.assertNotIn("register", ds.federation_health.kwargs)

    def test_legacy_constructor_fallback(self):
        self.use(LegacyFH)
        ds = self.build({"federation": {"en_federation": "1", "advertise_host": "hosta",
                                        "cluster_members": "hosta:0,hostb:2",
                                        "register_with_coordinator": "1",
                                        "coordinator_host": "hostc"}})
        fh = ds.federation_health
        self.assertIsInstance(fh, LegacyFH)
        self.assertEqual(fh.args["peer_addresses"], ["hostb:2"])
        self.assertFalse(ds._fed_info["registering"])

    def test_federation_block_only_with_monitor(self):
        ds = self.build({"monitoring": {"status_extended": "1"}})
        self.assertIsNone(ds._federation_status())
        ds = self.build({"monitoring": {"status_extended": "1"},
                         "federation": {"en_federation": "1", "node_id": "hosta:0",
                                        "advertise_host": "hosta",
                                        "cluster_members": "hosta:0,hostb:0",
                                        "register_with_coordinator": "1",
                                        "coordinator_host": "hostc"}})
        block = ds._federation_status()
        self.assertEqual(block["instance_id"], 0)
        self.assertEqual(block["node_id"], "hosta:0")
        self.assertEqual(block["health_mode"], "mesh")
        self.assertEqual(block["advisory_leader"], 0)
        self.assertEqual(block["advisory_leader_node_key"], "hosta:0")
        self.assertEqual(block["peers"], {"hostb:0": {"alive": True, "instance_id": 0}})
        self.assertEqual(block["coordinator"], {"endpoint": "hostc:6000", "role": "leader",
                                                "term": 7, "registered": True,
                                                "last_register_ok_age_sec": 1.5,
                                                "last_error": None,
                                                "endpoints": {"hostc:6000": {"role": "leader"}}})

    def test_real_federation_health_accepts_the_wiring(self):
        """Against the in-tree FederationHealth (when it has the extended
        constructor): no fallback, registrar configured, block shape."""
        sys.modules.pop("federation_health", None)
        if self._saved is not None:
            sys.modules["federation_health"] = self._saved
        import federation_health
        import inspect
        params = inspect.signature(federation_health.FederationHealth.__init__).parameters
        if "register" not in params or "members" not in params:
            self.skipTest("federation_health without the extended constructor")
        ds = self.build({"monitoring": {"status_extended": "1"},
                         "federation": {"en_federation": "1", "advertise_host": "hosta",
                                        "cluster_members": "hosta:0,hostb:0",
                                        "register_with_coordinator": "1",
                                        "coordinator_host": "hostc", "peer_timeout": "0.5"}})
        fh = ds.federation_health
        self.assertIsInstance(fh, federation_health.FederationHealth)
        self.addCleanup(fh.close)
        self.assertTrue(ds._fed_info["registering"])
        block = ds._federation_status()
        self.assertEqual(set(block["coordinator"]) - {"endpoints"},
                         {"endpoint", "role", "term", "registered",
                          "last_register_ok_age_sec", "last_error"})
        self.assertIsInstance(block["peers"], dict)
        json.dumps(block)


# ----------------------------------------------------------------------
# Header stamping
# ----------------------------------------------------------------------

def slot_value(header_bytes, slot):
    off = 252 + 4 * slot
    return int.from_bytes(header_bytes[off:off + 4], sys.byteorder)


class TestStamping(DSCase):

    def _finalized_bytes(self, ds, h):
        ds.iq_header = IQHeader()
        ds.iq_header.decode_header(h.encode_header())
        ds._finalize_header(True, True, 6)
        return ds.iq_header.encode_header()

    def test_defaults_leave_new_slots_zero(self):
        ds = self.build()
        ds._cal_quality = 25.0
        ds.iq_compensation_cntr = 3
        out = self._finalized_bytes(ds, header())
        for slot in (108, 110, 111, 112, 113):
            self.assertEqual(slot_value(out, slot), 0, slot)

    def test_node_id_slot_108(self):
        ds = self.build({"federation": {"node_id": "lab-a:0", "stamp_node_id": "1"}})
        out = self._finalized_bytes(ds, header())
        self.assertEqual(slot_value(out, 108), zlib.crc32(b"lab-a:0") & 0xFFFFFFFF)

    def test_cal_telemetry_slots(self):
        ds = self.build({"monitoring": {"stamp_cal_telemetry": "1"}})
        ds._cal_quality = -12.3456
        ds.iq_compensation_cntr = 3
        ds.sample_compensation_cntr = 2
        ds._recal_generation = 9
        ds.iq_header = header()
        ds._finalize_header(True, True, 6)
        self.assertEqual(ds.iq_header.get_cal_quality_mdb(), -12346)
        self.assertEqual(ds.iq_header.get_iq_comp_cnt(), 3)
        self.assertEqual(ds.iq_header.get_sample_comp_cnt(), 2)
        self.assertEqual(ds.iq_header.get_recal_gen(), 9)

    def test_slot_109_carried_through(self):
        ds = self.build({"monitoring": {"stamp_cal_telemetry": "1"},
                         "federation": {"stamp_node_id": "1"}})
        out = self._finalized_bytes(ds, header(decimator_drops=77, overruns=5))
        self.assertEqual(slot_value(out, 109), 77)
        self.assertEqual(slot_value(out, 102), 5)


# ----------------------------------------------------------------------
# Inherited instance lock
# ----------------------------------------------------------------------

def _closed_fd():
    """An fd number no thread of this process is using: the highest number
    the soft RLIMIT_NOFILE allows (capped), searched downwards. Unlike a
    just-closed low fd, it is not reused by a concurrent open()."""
    import errno
    import resource
    soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    if soft == resource.RLIM_INFINITY or soft > 65536:
        soft = 65536
    for fd in range(soft - 1, 2, -1):
        try:
            os.fstat(fd)
        except OSError as e:
            if e.errno == errno.EBADF:
                return fd
    raise RuntimeError("no free fd number")


class TestInheritedLock(DSCase):

    def test_ok_missing_mismatch(self):
        lock = os.path.join(self.tmp, "inst1.lock")
        other = os.path.join(self.tmp, "other.lock")
        open(lock, "w").close()
        open(other, "w").close()
        fd = os.open(lock, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assertEqual(delay_sync._check_inherited_lock(lock, fd), "ok")
        self.assertEqual(delay_sync._check_inherited_lock(other, fd), "mismatch")
        self.assertEqual(delay_sync._check_inherited_lock(os.path.join(self.tmp, "nope"), fd),
                         "mismatch")
        self.assertEqual(delay_sync._check_inherited_lock(lock, _closed_fd()), "missing")

    def test_enforce_only_under_refuse(self):
        with open("daq_chain_config.ini", "w") as f:
            f.write(ini_text({"federation": {"instance_id": "1"}}))
        closed = _closed_fd()
        logger = logging.getLogger("test_status_extended.lock")
        self.assertEqual(delay_sync.enforce_instance_lock(environ={}, fd=closed), 0)
        self.assertEqual(delay_sync.enforce_instance_lock(
            environ={"HEIMDALL_INSTANCE_GUARD": "legacy"}, fd=closed), 0)
        with self.assertLogs(logger, level="CRITICAL") as logs:
            rc = delay_sync.enforce_instance_lock(
                environ={"HEIMDALL_INSTANCE_GUARD": "refuse",
                         "HEIMDALL_INSTANCE_LOCK": os.path.join(self.tmp, "inst1.lock")},
                logger=logger, fd=closed)
        self.assertEqual(rc, 3)
        self.assertIn("instance 1 started without its instance lock; refusing", logs.output[0])
        lock = os.path.join(self.tmp, "inst1.lock")
        open(lock, "w").close()
        fd = os.open(lock, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assertEqual(delay_sync.enforce_instance_lock(
            environ={"HEIMDALL_INSTANCE_GUARD": "refuse", "HEIMDALL_INSTANCE_LOCK": lock},
            logger=logger, fd=fd), 0)


# ----------------------------------------------------------------------
# [database] instance keys
# ----------------------------------------------------------------------

class TestDatabaseInstanceKeys(DSCase):

    def _build_with_fake_db(self, db_keys):
        captured = []

        class FakeDAQDatabase(object):
            def __init__(self, **kwargs):
                captured.append(kwargs)

            def close(self):
                pass
        fake = types.ModuleType("daq_db")
        fake.DAQDatabase = FakeDAQDatabase
        saved = sys.modules.get("daq_db")
        sys.modules["daq_db"] = fake
        try:
            with open("daq_chain_config.ini", "w") as f:
                f.write(ini_text({"database": dict({"en_db": "1"}, **db_keys)}))
            with self.assertLogs("delay_sync", level="DEBUG") as logs:
                ds = delay_sync.delaySynchronizer()
        finally:
            if saved is None:
                del sys.modules["daq_db"]
            else:
                sys.modules["daq_db"] = saved
        if ds.event_bus is not None:
            self.addCleanup(ds.event_bus.close)
        return ds, captured, logs.output

    def test_malformed_values_fall_back_and_keep_db(self):
        ds, captured, logs = self._build_with_fake_db(
            {"instance_subdir": "yes", "write_instance_meta": "2"})
        self.assertIsNotNone(ds.db)
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0]["instance_subdir"])
        self.assertFalse(captured[0]["write_meta"])
        self.assertTrue(any("[database] instance_subdir = 'yes' is invalid" in line
                            for line in logs))
        self.assertTrue(any("[database] write_instance_meta = '2' is invalid" in line
                            for line in logs))
        self.assertFalse(any("Failed to initialize DAQ database" in line for line in logs))

    def test_valid_values_passed(self):
        _ds, captured, _logs = self._build_with_fake_db(
            {"instance_subdir": "1", "write_instance_meta": "1"})
        self.assertTrue(captured[0]["instance_subdir"])
        self.assertTrue(captured[0]["write_meta"])


# ----------------------------------------------------------------------
# Event identity on delay_sync's own bus
# ----------------------------------------------------------------------

class TestEventIdentityWiring(DSCase):

    def test_tagged_bus(self):
        ds = self.build({"monitoring": {"en_monitoring": "1", "event_tag_identity": "1"},
                         "federation": {"instance_id": "2", "node_id": "hosta:2",
                                        "cluster_id": "lab", "advertise_host": "hosta"}})
        self.assertEqual(ds.event_bus._source, {"instance_id": 2, "node_id": "hosta:2",
                                                "host": "hosta", "cluster_id": "lab"})

    def test_untagged_by_default(self):
        ds = self.build({"monitoring": {"en_monitoring": "1"}})
        self.assertIsNone(ds.event_bus._source)


if __name__ == "__main__":
    unittest.main()
