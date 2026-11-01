"""
    Description :
    Server-side tests for the TCP port-5001 control frame handling in
    hw_controller.py — the parts exercisable without SDR hardware:

      * CtrIfaceServer.process_ctr_frame — exact 128-byte framing (4-byte
        ASCII verb + 124-byte payload), payload decode offsets, 'FNSD'
        reply framing, EXIT/unknown-command handling, malformed-payload
        containment. The HWC main loop is emulated by a drainer thread that
        services the module-global ctr_request_queue.
      * HWC._handle_query_request — the FNSD+JSON reply shapes for the
        RFQ /OQRY/SCHQ query verbs (cross-agent contract #2).
      * HWC._drain_control_requests — queries answered in any state,
        hardware mutations deferred until allow_mutations, order
        preservation, and handler-exception containment.
      * Spec D.6 additions (WP-05): XACK handshake and extended acks, the
        CtrRequest state machine (deferred_timeout / RUNNING across the
        request timeout), fencing at execution time and its persistence,
        the control snapshot verbs HWSQ/SCHG answered with the main loop
        stopped, TLCK/TLKA, chunked schedule upload SCHB/SCHC/SCHE, RECL,
        the ctl_* connection keys, defer_control_during_burst, the
        hw_controller state relay writer (G.3) and the reply size budgets.
        Legacy (no-XACK) reply bytes are byte-compared for every verb.

    zmq is stubbed only when the real module is unavailable (the tests never
    open a ZMQ socket: rtl_daq transactions are replaced by stubs).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import json
import logging
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from os.path import join, dirname, realpath
from struct import pack
from unittest import mock

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

# Keep the HWC's logging.basicConfig(level=DEBUG) from flooding the output
logging.basicConfig(level=logging.CRITICAL)


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

    class ZMQError(Exception):
        pass

    class _Socket:
        def setsockopt(self, *a, **k):
            pass

        def connect(self, *a, **k):
            pass

        def close(self, *a, **k):
            pass

        def send(self, *a, **k):
            raise ZMQError("stub zmq: no transport")

        def recv(self, *a, **k):
            raise ZMQError("stub zmq: no transport")

    class Context:
        def socket(self, *a, **k):
            return _Socket()

        def term(self):
            pass

    stub.ZMQError = ZMQError
    stub.Context = Context
    sys.modules["zmq"] = stub


_install_zmq_stub_if_missing()

import hw_controller  # noqa: E402
import hwc_relay  # noqa: E402
from hw_controller import (CtrIfaceServer, HWC, CtrRequest, ConnState,  # noqa: E402
                           ControlPlane, U32_MAX)

M = 4  # receiver channels used throughout
ZEROS = b"FNSD" + bytes(124)


class _QueueDrainer(threading.Thread):
    """Emulates the HWC main loop: services hw_controller.ctr_request_queue,
    recording every request and answering query verbs with a canned (or
    computed) payload."""

    def __init__(self, query_handler=None):
        super().__init__(daemon=True)
        self.requests = []
        self.query_handler = query_handler or (
            lambda command: b'{"stub":true}')
        self._stop_evt = threading.Event()  # do not shadow Thread._stop()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                request = hw_controller.ctr_request_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self.requests.append((request.command, list(request.params)))
            if not request.try_start():
                continue
            if request.command in HWC.QUERY_COMMANDS:
                request.reply_payload = self.query_handler(request.command)
            request.finish((True, "ok"), "IQ_CAL")

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=5)


class _MainLoop(threading.Thread):
    """Runs the real HWC._drain_control_requests + snapshot publish like the
    HWC main loop does once per frame (no shared memory involved)."""

    def __init__(self, hwc, allow_mutations=True, period=0.01):
        super().__init__(daemon=True)
        self.hwc = hwc
        self.allow_mutations = allow_mutations
        self.period = period
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            self.hwc._drain_control_requests(allow_mutations=self.allow_mutations)
            self.hwc._publish_ctl_snapshot(frame=True)
            time.sleep(self.period)

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=10)


def _frame(verb, payload=b""):
    """Build one 128-byte control frame: 4-byte ASCII verb + 124B payload."""
    verb_bytes = verb.encode("ascii")
    assert len(verb_bytes) == 4, "verb must be exactly 4 bytes"
    assert len(payload) <= 124
    return verb_bytes + payload + bytes(124 - len(payload))


def _xack_frame(term=0, cluster_id=b""):
    return _frame("XACK", pack("<I", term) + cluster_id.ljust(32, b"\x00") + pack("<I", 0))


def _body(reply):
    """JSON body of an FNSD reply."""
    assert len(reply) == 128 and reply[0:4] == b"FNSD", reply
    return json.loads(reply[4:].rstrip(b"\x00").decode("utf-8"))


def _drain_queue():
    while True:
        try:
            hw_controller.ctr_request_queue.get_nowait()
        except queue.Empty:
            return


_BASE_INI = {
    "hw": {"num_ch": str(M), "en_bias_tee": ",".join(["0"] * M)},
    "daq": {"log_level": "5", "center_freq": "433000000", "sample_rate": "2400000",
            "listen_address": "127.0.0.1"},
    "pre_processing": {"cpi_size": "1048576", "decimation_ratio": "1"},
    "calibration": {"cal_track_mode": "0", "maximum_sync_fails": "3",
                    "cal_frame_burst_size": "5", "cal_frame_interval": "50",
                    "gain_lock_interval": "0", "unified_gain_control": "1",
                    "en_gain_tune_init": "0", "require_track_lock_intervention": "0",
                    "en_iq_cal": "1"},
    "adpis": {"en_adpis": "0", "adpis_proc_size": "8192",
              "adpis_gains_init": ",".join(["0"] * M)},
    "federation": {"instance_id": "0", "port_stride": "100"},
}


def _write_ini(path, overrides=None):
    sections = {k: dict(v) for k, v in _BASE_INI.items()}
    for section, values in (overrides or {}).items():
        sections.setdefault(section, {}).update(values)
    with open(path, "w") as f:
        for section, values in sections.items():
            f.write("[{}]\n".format(section))
            for key, value in values.items():
                f.write("{} = {}\n".format(key, value))
            f.write("\n")


class _HwcCase(unittest.TestCase):
    """Builds a real HWC from an INI in a private temp cwd (the control
    server thread is not started; rtl_daq transactions are stubbed)."""

    def setUp(self):
        _drain_queue()
        self.addCleanup(_drain_queue)

    def make_hwc(self, overrides=None, workdir=None):
        if workdir is None:
            workdir = tempfile.mkdtemp(prefix="hwc_ctl_")
            os.makedirs(join(workdir, "_data_control"))
            self.addCleanup(shutil.rmtree, workdir, True)
        _write_ini(join(workdir, "daq_chain_config.ini"), overrides)
        old_cwd = os.getcwd()
        os.chdir(workdir)
        self.addCleanup(os.chdir, old_cwd)
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
        hwc.current_state = "STATE_IQ_CAL"
        self.workdir = workdir
        return hwc

    def run_main_loop(self, hwc, allow_mutations=True):
        loop = _MainLoop(hwc, allow_mutations=allow_mutations)
        loop.start()
        self.addCleanup(loop.stop)
        return loop

    def extended_conn(self, server, term=0, cluster_id=b""):
        conn = ConnState()
        _flag, reply = server.process_ctr_frame(_xack_frame(term, cluster_id), conn)
        self.assertTrue(_body(reply)["xack"])
        return conn


class TesterCtrFrameDecoding(unittest.TestCase):
    """process_ctr_frame: framing, payload offsets, reply format."""

    def setUp(self):
        _drain_queue()
        # port is never bound: run() is not called in these tests
        self.server = CtrIfaceServer(M, port=0)
        self.drainer = _QueueDrainer()
        self.drainer.start()

    def tearDown(self):
        self.drainer.stop()
        _drain_queue()
        try:
            self.server.ctr_iface_socket.close()
        except OSError:
            pass

    def _submit(self, verb, payload=b""):
        exit_flag, reply = self.server.process_ctr_frame(
            _frame(verb, payload))
        return exit_flag, reply

    def _assert_plain_ack(self, reply):
        self.assertEqual(len(reply), 128)
        self.assertEqual(reply[0:4], b"FNSD")
        self.assertEqual(reply[4:], bytes(124))

    def test_freq_uint64_at_offset_4(self):
        exit_flag, reply = self._submit("FREQ", pack("<Q", 433920000))
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)
        self.assertEqual(self.drainer.requests[-1], ("FREQ", [433920000]))

    def test_gain_m_uint32_at_offset_4(self):
        gains = [0, 87, 297, 496]
        exit_flag, reply = self._submit("GAIN", pack("<" + "I" * M, *gains))
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)
        self.assertEqual(self.drainer.requests[-1], ("GAIN", gains))

    def test_egan_m_uint32_at_offset_4(self):
        """External LNA gains: tenths-dB uint32 per channel (kept out of the
        R820T quantizer downstream — the wire just carries raw values)."""
        gains = [140, 140, 0, 275]
        exit_flag, reply = self._submit("EGAN", pack("<" + "I" * M, *gains))
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)
        self.assertEqual(self.drainer.requests[-1], ("EGAN", gains))

    def test_bias_m_uint32_at_offset_4(self):
        states = [1, 0, 1, 1]
        exit_flag, reply = self._submit("BIAS", pack("<" + "I" * M, *states))
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)
        self.assertEqual(self.drainer.requests[-1], ("BIAS", states))

    def test_ornt_two_float32(self):
        exit_flag, reply = self._submit("ORNT", pack("<ff", 123.5, -10.25))
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)
        command, params = self.drainer.requests[-1]
        self.assertEqual(command, "ORNT")
        self.assertAlmostEqual(params[0], 123.5, places=4)
        self.assertAlmostEqual(params[1], -10.25, places=4)

    def test_space_padded_verbs(self):
        """'AGC ' and 'RFQ ' are compared with the trailing space —
        the 4-byte verb field is not NUL-padded."""
        exit_flag, reply = self._submit("AGC ")
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)
        self.assertEqual(self.drainer.requests[-1][0], "AGC ")

        exit_flag, reply = self._submit("RFQ ")
        self.assertFalse(exit_flag)
        self.assertEqual(reply[0:4], b"FNSD")  # JSON reply checked elsewhere
        self.assertEqual(self.drainer.requests[-1][0], "RFQ ")

    def test_schd_payload_utf8(self):
        path = "/tmp/schedule.json"
        exit_flag, reply = self._submit("SCHD", path.encode("utf-8"))
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)
        self.assertEqual(self.drainer.requests[-1], ("SCHD", [path]))

    def test_no_payload_verbs(self):
        for verb in ("INIT", "SCHS", "SCHN", "PARK", "SCAN", "OSTP", "RECL"):
            exit_flag, reply = self._submit(verb)
            self.assertFalse(exit_flag, verb)
            self._assert_plain_ack(reply)
            self.assertEqual(self.drainer.requests[-1][0], verb)

    def test_exit_closes_connection_without_reply(self):
        before = len(self.drainer.requests)
        exit_flag, reply = self._submit("EXIT")
        self.assertTrue(exit_flag)
        self.assertIsNone(reply)
        self.assertEqual(len(self.drainer.requests), before,
                         "EXIT must not reach the main loop")

    def test_unknown_command_acked_not_queued(self):
        before = len(self.drainer.requests)
        exit_flag, reply = self._submit("ZZZZ")
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)
        self.assertEqual(len(self.drainer.requests), before)

    def test_sthu_decodable_noop(self):
        """STHU stays decodable (wire compat) but is not queued."""
        before = len(self.drainer.requests)
        exit_flag, reply = self._submit("STHU", pack("<f", 0.25))
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)
        self.assertEqual(len(self.drainer.requests), before)

    def test_malformed_payload_contained(self):
        """A frame whose payload cannot be decoded must produce a reply, not
        kill the server thread."""
        # FREQ with a frame deliberately truncated below the uint64 field
        broken = b"FREQ" + b"\x01"  # only 5 bytes total, not 128
        exit_flag, reply = self.server.process_ctr_frame(broken)
        self.assertFalse(exit_flag)
        self._assert_plain_ack(reply)

    def test_new_verbs_never_queue_server_thread_queries(self):
        """HWSQ/SCHG/TLCK are answered in the server thread (no queue)."""
        before = len(self.drainer.requests)
        for verb in ("HWSQ", "SCHG", "TLCK"):
            _flag, reply = self._submit(verb)
            self.assertIsInstance(_body(reply), dict)
        self.assertEqual(len(self.drainer.requests), before)


class TesterQueryReplies(unittest.TestCase):
    """HWC._handle_query_request reply shapes (contract #2) and the full
    FNSD+JSON path through process_ctr_frame."""

    def _make_hwc(self):
        """A bare HWC with only the state _handle_query_request touches —
        avoids the config-file/socket side effects of HWC.__init__."""
        hwc = object.__new__(HWC)
        hwc.logger = logging.getLogger("test_hwc")
        hwc.total_system_gains_db = [21.4, 21.4, 33.05, 0.0]
        hwc.system_nf_db = [1.234, 1.234, 0.897, 6.0]
        hwc.compression_flags = 5
        hwc.orientation = None
        hwc.scheduler = None
        return hwc

    def test_rfq_shape(self):
        hwc = self._make_hwc()
        payload = hwc._handle_query_request("RFQ ")
        self.assertLessEqual(len(payload), 124)
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(set(data.keys()), {"gains", "nf", "comp"})
        self.assertEqual(len(data["gains"]), 4)
        self.assertEqual(len(data["nf"]), 4)
        for got, expected in zip(data["gains"], [21.4, 21.4, 33.05, 0.0]):
            self.assertAlmostEqual(got, expected, delta=0.06)
        for got, expected in zip(data["nf"], [1.234, 1.234, 0.897, 6.0]):
            self.assertAlmostEqual(got, expected, delta=0.006)
        self.assertEqual(data["comp"], 5)

    def test_oqry_shape_orientation_disabled(self):
        hwc = self._make_hwc()
        payload = hwc._handle_query_request("OQRY")
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data, {"az": 0.0, "el": 0.0, "state": 0})

    def test_schq_shape_no_scheduler(self):
        hwc = self._make_hwc()
        payload = hwc._handle_query_request("SCHQ")
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data, {"state": "IDLE", "active": False})

    def test_rfq_worst_case_fits_124_bytes(self):
        hwc = self._make_hwc()
        hwc.total_system_gains_db = [-99.9] * 8
        hwc.system_nf_db = [99.99] * 8
        hwc.compression_flags = 0xFF
        payload = hwc._handle_query_request("RFQ ")
        self.assertLessEqual(len(payload), 124)
        json.loads(payload.decode("utf-8"))  # still valid JSON

    def test_query_reply_through_full_frame_path(self):
        """RFQ through process_ctr_frame: reply is 'FNSD' + NUL-padded UTF-8
        JSON in bytes 4..127."""
        _drain_queue()
        hwc = self._make_hwc()
        server = CtrIfaceServer(M, port=0)
        drainer = _QueueDrainer(query_handler=hwc._handle_query_request)
        drainer.start()
        try:
            exit_flag, reply = server.process_ctr_frame(_frame("RFQ "))
            self.assertFalse(exit_flag)
            self.assertEqual(len(reply), 128)
            self.assertEqual(reply[0:4], b"FNSD")
            body = reply[4:].rstrip(b"\x00")
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(set(data.keys()), {"gains", "nf", "comp"})
        finally:
            drainer.stop()
            try:
                server.ctr_iface_socket.close()
            except OSError:
                pass


class TesterDrainLogic(unittest.TestCase):
    """HWC._drain_control_requests: query-anytime / mutate-only-when-safe."""

    def _make_hwc(self):
        hwc = object.__new__(HWC)
        hwc.logger = logging.getLogger("test_hwc_drain")
        hwc.total_system_gains_db = [0.0] * M
        hwc.system_nf_db = [0.0] * M
        hwc.compression_flags = 0
        hwc.orientation = None
        hwc.scheduler = None
        hwc._pending_ctr_requests = []
        hwc.noise_source_state = False
        hwc.defer_control_during_burst = False
        hwc.current_state = "STATE_IQ_CAL"
        hwc.ctl = ControlPlane(M)
        hwc.handled = []

        def _record(command, params, from_wire=False):
            hwc.handled.append((command, params))
            return True, "ok"

        hwc._handle_control_reqest = _record
        return hwc

    def setUp(self):
        _drain_queue()

    def tearDown(self):
        _drain_queue()

    def test_query_served_while_mutation_deferred(self):
        hwc = self._make_hwc()
        mutation = CtrRequest("FREQ", [433000000])
        query = CtrRequest("RFQ ", [])
        hw_controller.ctr_request_queue.put(mutation)
        hw_controller.ctr_request_queue.put(query)

        hwc._drain_control_requests(allow_mutations=False)
        self.assertTrue(query.done.is_set())
        self.assertIsNotNone(query.reply_payload)
        self.assertFalse(mutation.done.is_set())
        self.assertEqual(hwc.handled, [])
        self.assertEqual(hwc._pending_ctr_requests, [mutation])

        hwc._drain_control_requests(allow_mutations=True)
        self.assertTrue(mutation.done.is_set())
        self.assertEqual(hwc.handled, [("FREQ", [433000000])])
        self.assertEqual(hwc._pending_ctr_requests, [])

    def test_deferred_mutation_order_preserved(self):
        hwc = self._make_hwc()
        first = CtrRequest("GAIN", [1])
        second = CtrRequest("BIAS", [2])
        hw_controller.ctr_request_queue.put(first)
        hw_controller.ctr_request_queue.put(second)
        hwc._drain_control_requests(allow_mutations=False)
        third = CtrRequest("FREQ", [3])
        hw_controller.ctr_request_queue.put(third)
        hwc._drain_control_requests(allow_mutations=True)
        self.assertEqual([c for c, _p in hwc.handled],
                         ["GAIN", "BIAS", "FREQ"])

    def test_handler_exception_contained_and_completed(self):
        hwc = self._make_hwc()

        def _boom(command, params, from_wire=False):
            raise RuntimeError("injected failure")

        hwc._handle_control_reqest = _boom
        request = CtrRequest("GAIN", [0] * M)
        hw_controller.ctr_request_queue.put(request)
        hwc._drain_control_requests(allow_mutations=True)  # must not raise
        self.assertTrue(request.done.is_set(),
                        "a failing handler must still complete the request")
        self.assertEqual(request.result, (False, "error:injected failure"))

    def test_cancelled_request_never_executes(self):
        hwc = self._make_hwc()
        request = CtrRequest("FREQ", [1])
        self.assertTrue(request.try_cancel())
        self.assertTrue(request.cancelled)
        hw_controller.ctr_request_queue.put(request)
        hwc._drain_control_requests(allow_mutations=True)
        self.assertEqual(hwc.handled, [])
        self.assertFalse(request.done.is_set())

    def test_running_request_cannot_be_cancelled(self):
        request = CtrRequest("FREQ", [1])
        self.assertTrue(request.try_start())
        self.assertFalse(request.try_cancel())
        self.assertFalse(request.cancelled)
        request.finish((True, "ok"), "IQ_CAL")
        self.assertEqual(request.state, CtrRequest.DONE)

    def test_init_and_recl_deferred_during_burst(self):
        hwc = self._make_hwc()
        hwc.noise_source_state = True
        for verb in ("INIT", "RECL", "FREQ"):
            hw_controller.ctr_request_queue.put(CtrRequest(verb, []))
        hwc._drain_control_requests(allow_mutations=True)
        self.assertEqual([c for c, _p in hwc.handled], ["FREQ"])
        self.assertEqual([r.command for r in hwc._pending_ctr_requests], ["INIT", "RECL"])
        hwc.noise_source_state = False
        hwc._drain_control_requests(allow_mutations=True)
        self.assertEqual([c for c, _p in hwc.handled], ["FREQ", "INIT", "RECL"])

    def test_defer_control_during_burst(self):
        """[calibration] defer_control_during_burst = 1 also defers
        FREQ/GAIN/EGAN/BIAS while the noise source is on (G62)."""
        hwc = self._make_hwc()
        hwc.noise_source_state = True
        hwc.defer_control_during_burst = True
        for verb in ("FREQ", "GAIN", "EGAN", "BIAS", "ORNT"):
            hw_controller.ctr_request_queue.put(CtrRequest(verb, []))
        hwc._drain_control_requests(allow_mutations=True)
        self.assertEqual([c for c, _p in hwc.handled], ["ORNT"])
        hwc.noise_source_state = False
        hwc._drain_control_requests(allow_mutations=True)
        self.assertEqual([c for c, _p in hwc.handled],
                         ["ORNT", "FREQ", "GAIN", "EGAN", "BIAS"])


class TesterXack(_HwcCase):
    """XACK handshake, caps, cluster/term trust, fencing (C.4.5, D.6.2)."""

    def test_xack_reply_bytes(self):
        hwc = self.make_hwc()
        conn = ConnState()
        _flag, reply = hwc.ctr_iface_server.process_ctr_frame(_xack_frame(), conn)
        expected = (b'{"xack":true,"v":1,"nch":4,"term":0,"caps":"hwsq,schb,schg,tlck",'
                    b'"rto":10000,"fp":437,"hmx":12000}')
        self.assertEqual(reply, b"FNSD" + expected + bytes(124 - len(expected)))
        self.assertTrue(conn.xack)
        self.assertFalse(conn.cluster_bad)

    def test_caps_recl_only_with_wire_recal(self):
        hwc = self.make_hwc({"calibration": {"en_wire_recal": "1"},
                             "daq": {"ctl_request_timeout_sec": "20",
                                     "ctl_handler_max_sec": "30"}})
        _flag, reply = hwc.ctr_iface_server.process_ctr_frame(_xack_frame(), ConnState())
        body = _body(reply)
        self.assertEqual(body["caps"], "hwsq,schb,schg,tlck,recl")
        self.assertEqual(body["rto"], 20000)
        self.assertEqual(body["hmx"], 30000)

    def test_cluster_mismatch_refuses_mutations(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        conn = ConnState()
        _flag, reply = server.process_ctr_frame(_xack_frame(5, b"otherlab"), conn)
        self.assertEqual(_body(reply), {"xack": False, "error": "cluster_mismatch", "term": 0})
        self.assertTrue(conn.cluster_bad)
        self.assertEqual(hwc.ctl.max_term_seen, 0)
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 100000000)), conn)
        self.assertEqual(reply, ZEROS)
        self.assertEqual(hw_controller.ctr_request_queue.qsize(), 0, "must not be executed")
        _flag, reply = server.process_ctr_frame(_frame("TLKA", pack("<I", 1)), conn)
        self.assertEqual(reply, ZEROS)
        # queries are still answered
        _flag, reply = server.process_ctr_frame(_frame("HWSQ"), conn)
        self.assertIn("fsm", _body(reply))

    def test_empty_cluster_id_never_raises_fence(self):
        hwc = self.make_hwc()
        conn = self.extended_conn(hwc.ctr_iface_server, term=9)
        self.assertEqual(conn.term, 9)
        self.assertEqual(hwc.ctl.max_term_seen, 0)
        self.assertFalse(os.path.exists("_data_control/fence_term"))

    def test_fence_raised_persisted_and_reloaded(self):
        hwc = self.make_hwc()
        _flag, reply = hwc.ctr_iface_server.process_ctr_frame(_xack_frame(7, b"default"), ConnState())
        self.assertEqual(_body(reply)["term"], 7)
        with open("_data_control/fence_term") as f:
            self.assertEqual(f.read(), "7\n")
        # a lower term from the same cluster does not lower the fence
        hwc.ctr_iface_server.process_ctr_frame(_xack_frame(3, b"default"), ConnState())
        self.assertEqual(hwc.ctl.max_term_seen, 7)
        self.assertEqual(os.listdir("_data_control"), ["fence_term"], "no temp file left")
        hwc2 = self.make_hwc(workdir=self.workdir)
        self.assertEqual(hwc2.ctl.max_term_seen, 7)
        _flag, reply = hwc2.ctr_iface_server.process_ctr_frame(_frame("HWSQ"), ConnState())
        self.assertEqual(_body(reply)["t"], 7)

    def test_unreadable_fence_file_means_zero(self):
        workdir = tempfile.mkdtemp(prefix="hwc_ctl_")
        self.addCleanup(shutil.rmtree, workdir, True)
        os.makedirs(join(workdir, "_data_control"))
        with open(join(workdir, "_data_control", "fence_term"), "w") as f:
            f.write("garbage\n")
        hwc = self.make_hwc(workdir=workdir)
        self.assertEqual(hwc.ctl.max_term_seen, 0)

    def test_term_jump_refused(self):
        hwc = self.make_hwc({"federation": {"fence_max_term_jump": "10"}})
        conn = ConnState()
        _flag, reply = hwc.ctr_iface_server.process_ctr_frame(_xack_frame(11, b"default"), conn)
        self.assertEqual(_body(reply), {"xack": False, "error": "term_jump", "term": 0})
        self.assertTrue(conn.cluster_bad)
        self.assertEqual(hwc.ctl.max_term_seen, 0)
        conn2 = ConnState()
        _flag, reply = hwc.ctr_iface_server.process_ctr_frame(_xack_frame(10, b"default"), conn2)
        self.assertTrue(_body(reply)["xack"])
        self.assertEqual(hwc.ctl.max_term_seen, 10)

    def test_stale_term_at_execution(self):
        """A request deferred (outside IQ_CAL) from a term-3 connection, then
        an XACK with term 4 on another connection, then the drain ->
        stale_term, not executed."""
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        server.request_timeout_s = 3.0
        old = self.extended_conn(server, term=3, cluster_id=b"default")
        result = {}

        def _client():
            _f, result["reply"] = server.process_ctr_frame(_frame("FREQ", pack("<Q", 101000000)), old)

        client = threading.Thread(target=_client)
        client.start()
        deadline = time.monotonic() + 2
        while hw_controller.ctr_request_queue.qsize() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        hwc._drain_control_requests(allow_mutations=False)   # deferred
        self.assertEqual(len(hwc._pending_ctr_requests), 1)
        self.extended_conn(server, term=4, cluster_id=b"default")
        hwc._drain_control_requests(allow_mutations=True)
        client.join(5)
        body = _body(result["reply"])
        self.assertEqual((body["applied"], body["reason"]), (False, "stale_term"))
        self.assertEqual(hwc.transactions, [], "the stale command must not reach rtl_daq")
        self.assertEqual(hwc.rf_center_frequency, 433000000)

    def test_term_zero_connections_never_fenced(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        self.extended_conn(server, term=8, cluster_id=b"default")
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 102000000)), conn)
        self.assertEqual(_body(reply)["reason"], "ok")


class TesterExtendedAcks(_HwcCase):
    """Extended acks and the request state machine (D.6.2)."""

    def test_applied_ok_and_legacy_bytes_for_same_frame(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        frame = _frame("FREQ", pack("<Q", 102000000))
        _flag, legacy = server.process_ctr_frame(frame)
        self.assertEqual(legacy, ZEROS)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(frame, conn)
        body = _body(reply)
        self.assertEqual(set(body), {"applied", "reason", "fsm", "seq"})
        self.assertEqual((body["applied"], body["reason"], body["fsm"]), (True, "ok", "IQ_CAL"))
        _flag, reply2 = server.process_ctr_frame(frame, conn)
        self.assertEqual(_body(reply2)["seq"], body["seq"] + 1)

    def test_not_confirmed_freq_keeps_frequency(self):
        hwc = self.make_hwc()
        hwc._rtl_daq_transaction = lambda msg: None   # ZMQ timeout stub
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 150000000)), conn)
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"]), (False, "not_confirmed"))
        self.assertEqual(hwc.rf_center_frequency, 433000000)
        # legacy bytes unchanged for the failing command
        _flag, legacy = server.process_ctr_frame(_frame("FREQ", pack("<Q", 150000000)))
        self.assertEqual(legacy, ZEROS)

    def test_range_and_gain_not_confirmed(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 2**33)), conn)
        self.assertEqual(_body(reply)["reason"], "range")
        _flag, reply = server.process_ctr_frame(_frame("GAIN", pack("<IIII", 1, 2, 3, 4)), conn)
        self.assertEqual(_body(reply)["reason"], "range")
        hwc._rtl_daq_transaction = lambda msg: None
        _flag, reply = server.process_ctr_frame(_frame("GAIN", pack("<IIII", 297, 297, 297, 297)), conn)
        self.assertEqual(_body(reply)["reason"], "not_confirmed")
        _flag, reply = server.process_ctr_frame(_frame("AGC "), conn)
        self.assertEqual(_body(reply)["reason"], "not_confirmed")

    def test_gain_held_for_burst(self):
        hwc = self.make_hwc()
        hwc.noise_source_state = True
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("GAIN", pack("<IIII", 297, 297, 297, 297)), conn)
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"]), (None, "held_for_burst"))
        self.assertEqual(hwc.last_gains, [16] * M)
        _flag, reply = server.process_ctr_frame(_frame("AGC "), conn)
        self.assertEqual(_body(reply)["reason"], "held_for_burst")
        self.assertEqual(hwc.transactions, [])

    def test_disabled_reasons(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        for verb, payload in (("RECL", b""), ("SCHD", b'{"entries":[]}'), ("SCHS", b""),
                              ("SCHN", b""), ("ORNT", pack("<ff", 1, 2)), ("PARK", b""),
                              ("SCAN", b""), ("OSTP", b""), ("BIAS", pack("<IIII", 1, 1, 1, 1)),
                              ("EGAN", pack("<IIII", 10, 10, 10, 10))):
            _flag, reply = server.process_ctr_frame(_frame(verb, payload), conn)
            body = _body(reply)
            self.assertEqual((body["applied"], body["reason"]), (False, "disabled"), verb)
        # RECL with en_wire_recal = 0 on a legacy connection: today's zeros
        _flag, reply = server.process_ctr_frame(_frame("RECL"))
        self.assertEqual(reply, ZEROS)
        self.assertEqual(hwc.current_state, "STATE_IQ_CAL", "disabled RECL must not re-init")

    def test_init_and_sthu(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        loop = self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("INIT"), conn)
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"], body["fsm"]), (True, "ok", "INIT"))
        loop.stop()
        _flag, reply = server.process_ctr_frame(_frame("STHU", pack("<f", 0.5)), conn)
        self.assertEqual(_body(reply)["reason"], "unsupported")

    def test_parse_error(self):
        hwc = self.make_hwc({"schedule": {"en_schedule": "1", "schedule_mode": "none"}})
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        for payload in (b"{not json", b"/nonexistent/schedule.json", b"", b'{"entries":[]}'):
            _flag, reply = server.process_ctr_frame(_frame("SCHD", payload), conn)
            self.assertEqual(_body(reply)["reason"], "parse_error", payload)

    def test_error_reason_truncated_to_fit(self):
        hwc = self.make_hwc()

        def _boom(command, params, from_wire=False):
            raise RuntimeError("x" * 300)

        hwc._handle_control_reqest = _boom
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 1)), conn)
        body = _body(reply)
        self.assertFalse(body["applied"])
        self.assertTrue(body["reason"].startswith("error:xxx"))
        self.assertEqual(body["fsm"], "IQ_CAL")

    def test_queue_full(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        conn = self.extended_conn(server)
        for _ in range(hw_controller.ctr_request_queue.maxsize):
            hw_controller.ctr_request_queue.put(CtrRequest("INIT", []))
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 1)), conn)
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"]), (False, "queue_full"))
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 1)))
        self.assertEqual(reply, ZEROS)

    def test_deferred_timeout_never_executes(self):
        """Main loop not draining: PENDING at T_srv -> deferred_timeout, and
        the request is provably not executed afterwards."""
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        server.request_timeout_s = 0.3
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 120000000)), conn)
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"]), (False, "deferred_timeout"))
        _flag, legacy = server.process_ctr_frame(_frame("FREQ", pack("<Q", 120000000)))
        self.assertEqual(legacy, ZEROS)
        hwc._drain_control_requests(allow_mutations=True)
        self.assertEqual(hwc.transactions, [])
        self.assertEqual(hwc.rf_center_frequency, 433000000)

    def test_running_across_request_timeout(self):
        """A handler that sleeps across T_srv yields the true result on an
        extended connection and FNSD+zeros at T_srv on a legacy one."""
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        # Generous margins for slow shared CI runners: the 10 ms main loop has
        # 0.6 s to pick the legacy request up (else it is cancelled), and the
        # legacy reply at T_srv (0.6 s) is told apart from one that waited for
        # the 1.5 s handler.
        server.request_timeout_s = 0.6

        def _slow(msg):
            time.sleep(1.5)
            hwc.transactions.append(bytes(msg))
            return b"OK"

        hwc._rtl_daq_transaction = _slow
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 140000000)), conn)
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"]), (True, "ok"))
        self.assertEqual(hwc.rf_center_frequency, 140000000)
        t0 = time.monotonic()
        _flag, legacy = server.process_ctr_frame(_frame("FREQ", pack("<Q", 141000000)))
        elapsed = time.monotonic() - t0
        self.assertEqual(legacy, ZEROS)
        self.assertLess(elapsed, 1.2, "legacy reply must come at T_srv")
        deadline = time.monotonic() + 10
        while hwc.rf_center_frequency != 141000000 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(hwc.rf_center_frequency, 141000000, "the running request completes")

    def test_handler_timeout(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        server.request_timeout_s = 0.2
        server.handler_max_s = 0.2
        release = threading.Event()

        def _stuck(msg):
            release.wait(5)
            return b"OK"

        hwc._rtl_daq_transaction = _stuck
        self.run_main_loop(hwc)
        self.addCleanup(release.set)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("FREQ", pack("<Q", 140000000)), conn)
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"]), (None, "handler_timeout"))

    def test_legacy_reply_bytes_unchanged_for_every_verb(self):
        """Byte-compare: without XACK every existing verb replies exactly as
        before (zeros for mutating verbs, unchanged JSON for queries)."""
        hwc = self.make_hwc({"schedule": {"en_schedule": "1", "schedule_mode": "none"}})
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        sched = json.dumps({"entries": [{"frequency": 100000000}], "gen": 5},
                           separators=(",", ":")).encode()
        frames = [("FREQ", pack("<Q", 100000000)), ("GAIN", pack("<IIII", 297, 297, 297, 297)),
                  ("AGC ", b""), ("INIT", b""), ("SCHD", sched), ("SCHN", b""), ("SCHS", b""),
                  ("EGAN", pack("<IIII", 1, 2, 3, 4)), ("BIAS", pack("<IIII", 1, 0, 1, 0)),
                  ("ORNT", pack("<ff", 10, 20)), ("PARK", b""), ("SCAN", b""), ("OSTP", b""),
                  ("STHU", pack("<f", 1.0)), ("ZZZZ", b""), ("FREQ", pack("<Q", 0))]
        for verb, payload in frames:
            hwc.current_state = "STATE_IQ_CAL"
            _flag, reply = server.process_ctr_frame(_frame(verb, payload))
            self.assertEqual(reply, ZEROS, verb)
        _flag, reply = server.process_ctr_frame(_frame("RFQ "))
        self.assertEqual(reply[4:].rstrip(b"\x00"), b'{"gains":[0.0,0.0,0.0,0.0],"nf":[0.0,0.0,0.0,0.0],"comp":0}')
        _flag, reply = server.process_ctr_frame(_frame("OQRY"))
        self.assertEqual(reply[4:].rstrip(b"\x00"), b'{"az":0.0,"el":0.0,"state":0}')
        _flag, reply = server.process_ctr_frame(_frame("SCHQ"))
        self.assertEqual(reply[4:].rstrip(b"\x00"), b'{"state":"IDLE","active":false}')

    def test_schq_unchanged_after_gen_load(self):
        hwc = self.make_hwc({"schedule": {"en_schedule": "1", "schedule_mode": "none"}})
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        doc = b'{"gen":77,"entries":[{"frequency":100000000,"dwell_frames":10}]}'
        server.process_ctr_frame(_frame("SCHD", doc))
        _flag, reply = server.process_ctr_frame(_frame("SCHQ"))
        self.assertEqual(reply[4:].rstrip(b"\x00"),
                         b'{"state":"ACTIVE_WAITING_CAL","active":true,"idx":0,"total":1,'
                         b'"freq":100000000,"frames":0,"dwell":10}')


class TesterSnapshotVerbs(_HwcCase):
    """HWSQ/SCHG/TLCK from the control snapshot, main loop stopped (R4)."""

    def test_hwsq_answered_while_main_loop_stopped(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        t0 = time.monotonic()
        _flag, reply = server.process_ctr_frame(_frame("HWSQ"))
        self.assertLess(time.monotonic() - t0, 0.1)
        body = _body(reply)
        self.assertEqual(list(body), ["fsm", "pend", "ns", "agc", "nch", "f", "fage", "fr", "fc", "t"])
        self.assertEqual((body["fsm"], body["nch"], body["f"], body["fage"], body["fr"]),
                         ("INIT", M, 433000000, -1, 0))
        hwc._frame_count = 3
        hwc._publish_ctl_snapshot(frame=True)
        _flag, reply = server.process_ctr_frame(_frame("HWSQ"))
        first = _body(reply)
        self.assertEqual((first["fr"], first["fc"], first["fsm"]), (1, 3, "IQ_CAL"))
        time.sleep(0.05)
        _flag, reply = server.process_ctr_frame(_frame("HWSQ"))
        self.assertGreater(_body(reply)["fage"], first["fage"])
        # frames stalled for long: fage grows (capped) and fr drops to 0
        with hwc.ctl._snap_lock:
            hwc.ctl._snap["last_frame_mono"] -= 500.0
        t0 = time.monotonic()
        _flag, reply = server.process_ctr_frame(_frame("HWSQ"))
        self.assertLess(time.monotonic() - t0, 0.1)
        stalled = _body(reply)
        self.assertEqual((stalled["fage"], stalled["fr"]), (99999, 0))

    def test_hwsq_pend_counts_queued_and_deferred(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        hw_controller.ctr_request_queue.put(CtrRequest("FREQ", [1]))
        hw_controller.ctr_request_queue.put(CtrRequest("GAIN", [1]))
        hwc._drain_control_requests(allow_mutations=False)
        hwc._publish_ctl_snapshot(frame=True)
        hw_controller.ctr_request_queue.put(CtrRequest("INIT", []))
        _flag, reply = server.process_ctr_frame(_frame("HWSQ"))
        self.assertEqual(_body(reply)["pend"], 3)

    def test_schg_load_clear_and_stall(self):
        hwc = self.make_hwc({"schedule": {"en_schedule": "1", "schedule_mode": "none"}})
        server = hwc.ctr_iface_server
        _flag, reply = server.process_ctr_frame(_frame("SCHG"))
        self.assertEqual(_body(reply), {"gen": None, "loaded": False, "active": False,
                                        "st": "IDLE", "idx": 0, "total": 0})
        loop = self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        doc = b'{"gen":4294967295,"entries":[{"frequency":100000000},{"frequency":200000000}]}'
        _flag, reply = server.process_ctr_frame(_frame("SCHD", doc), conn)
        self.assertEqual(_body(reply)["reason"], "ok")
        loop.stop()   # frames stall: SCHG is still answered from the snapshot
        _flag, reply = server.process_ctr_frame(_frame("SCHG"))
        self.assertEqual(_body(reply), {"gen": 4294967295, "loaded": True, "active": True,
                                        "st": "ACTIVE_WAITING_CAL", "idx": 0, "total": 2})
        self.run_main_loop(hwc)
        server.process_ctr_frame(_frame("SCHD", b'{"entries":[{"frequency":300000000}]}'), conn)
        _flag, reply = server.process_ctr_frame(_frame("SCHG"))
        self.assertIsNone(_body(reply)["gen"], "a document without gen clears it")
        server.process_ctr_frame(_frame("SCHD", doc), conn)
        server.process_ctr_frame(_frame("SCHS"), conn)
        _flag, reply = server.process_ctr_frame(_frame("SCHG"))
        self.assertEqual(_body(reply), {"gen": None, "loaded": False, "active": False,
                                        "st": "IDLE", "idx": 0, "total": 0})

    def test_schg_gen_survives_schedule_end(self):
        hwc = self.make_hwc({"schedule": {"en_schedule": "1", "schedule_mode": "none"}})
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        doc = b'{"gen":12,"repeat_mode":"once","entries":[{"frequency":100000000}]}'
        server.process_ctr_frame(_frame("SCHD", doc), self.extended_conn(server))
        hwc.scheduler.schedule.active = False
        hwc.scheduler.state = hwc.scheduler.STATE_IDLE
        hwc._publish_ctl_snapshot()   # what the main loop does every frame
        _flag, reply = server.process_ctr_frame(_frame("SCHG"))
        body = _body(reply)
        self.assertEqual((body["gen"], body["active"], body["st"]), (12, False, "IDLE"))

    def test_tlck_and_tlka_in_place(self):
        hwc = self.make_hwc({"calibration": {"require_track_lock_intervention": "1"}})
        server = hwc.ctr_iface_server
        path = "_data_control/iq_track_lock"
        _flag, reply = server.process_ctr_frame(_frame("TLCK"))
        self.assertEqual(_body(reply), {"req": 1, "appr": 0})   # missing file
        with open(path, "w") as f:
            f.write("0\n")
        inode = os.stat(path).st_ino
        _flag, reply = server.process_ctr_frame(_frame("TLKA", pack("<I", 1)))
        self.assertEqual(_body(reply), {"applied": True, "appr": 1})
        self.assertEqual(os.stat(path).st_ino, inode, "TLKA writes in place")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"1")
        self.assertTrue(hwc._track_lock_approved())
        _flag, reply = server.process_ctr_frame(_frame("TLCK"))
        self.assertEqual(_body(reply), {"req": 1, "appr": 1})
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("TLKA", pack("<I", 0)), conn)
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"], body["appr"]), (True, "ok", 0))
        self.assertFalse(hwc._track_lock_approved())
        _flag, reply = server.process_ctr_frame(_frame("TLKA", pack("<I", 7)))
        self.assertEqual(_body(reply), {"applied": False, "reason": "range"})

    def test_tlka_creates_missing_file_and_reports_errors(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        _flag, reply = server.process_ctr_frame(_frame("TLKA", pack("<I", 1)))
        self.assertEqual(_body(reply)["appr"], 1)
        with open("_data_control/iq_track_lock", "rb") as f:
            self.assertEqual(f.read(), b"1")
        hwc.ctl.track_lock_path = "/nonexistent-dir/" + "y" * 200 + "/iq_track_lock"
        _flag, reply = server.process_ctr_frame(_frame("TLKA", pack("<I", 1)))
        body = _body(reply)
        self.assertFalse(body["applied"])
        self.assertTrue(body["reason"].startswith("error:"))

    def test_tlka_fenced(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        stale = self.extended_conn(server, term=2, cluster_id=b"default")
        self.extended_conn(server, term=5, cluster_id=b"default")
        _flag, reply = server.process_ctr_frame(_frame("TLKA", pack("<I", 1)), stale)
        self.assertEqual(_body(reply)["reason"], "stale_term")
        self.assertFalse(os.path.exists("_data_control/iq_track_lock"))


class TesterChunkedSchedule(_HwcCase):
    """SCHB/SCHC/SCHE reassembly (D.6.2)."""

    @staticmethod
    def _doc(n_entries=8, gen=99):
        doc = {"name": "chunked", "gen": gen,
               "entries": [{"frequency": 100000000 + i * 1000000, "dwell_frames": 10,
                            "gains": [297, 297, 297, 297]} for i in range(n_entries)]}
        return json.dumps(doc, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _chunks(data, sess):
        out = []
        for seq, off in enumerate(range(0, len(data), 116)):
            part = data[off:off + 116]
            out.append(_frame("SCHC", pack("<HHH", sess, seq, len(part)) + part))
        return out

    def _schb(self, server, conn, data, crc=None, nchunks=None):
        crc = zlib.crc32(data) & U32_MAX if crc is None else crc
        nchunks = -(-len(data) // 116) if nchunks is None else nchunks
        _flag, reply = server.process_ctr_frame(
            _frame("SCHB", pack("<IIHH", len(data), crc, nchunks, 0)), conn)
        return _body(reply)

    def test_reassembly_over_pipelined_socket(self):
        hwc = self.make_hwc({"schedule": {"en_schedule": "1", "schedule_mode": "none"}})
        server = CtrIfaceServer(M, port=0, listen_address="127.0.0.1", control=hwc.ctl)
        server.start()
        self.addCleanup(server.shutdown)
        self.assertTrue(server.listening.wait(5))
        self.run_main_loop(hwc)
        port = server.ctr_iface_socket.getsockname()[1]
        data = self._doc()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            def _rx():
                return CtrIfaceServer._recv_exact(sock, 128)
            sock.sendall(_xack_frame())
            self.assertTrue(_body(_rx())["xack"])
            sock.sendall(_frame("SCHB", pack("<IIHH", len(data), zlib.crc32(data), -(-len(data) // 116), 0)))
            sess = _body(_rx())["sess"]
            chunks = self._chunks(data, sess)
            self.assertGreater(len(chunks), 4)
            sock.sendall(b"".join(chunks))          # pipelined: all before any reply
            for seq in range(len(chunks)):
                self.assertEqual(_body(_rx()), {"ok": True, "seq": seq})
            sock.sendall(_frame("SCHE", pack("<H", sess)))
            body = _body(_rx())
            self.assertEqual((body["applied"], body["reason"], body["entries"]), (True, "ok", 8))
            sock.sendall(_frame("SCHG"))
            self.assertEqual(_body(_rx())["gen"], 99)
        self.assertEqual(len(hwc.scheduler.schedule.entries), 8)

    def test_crc_mismatch_incomplete_and_bad_session(self):
        hwc = self.make_hwc({"schedule": {"en_schedule": "1", "schedule_mode": "none"}})
        server = hwc.ctr_iface_server
        conn = ConnState()
        data = self._doc()
        sess = self._schb(server, conn, data, crc=1234)["sess"]
        for frame in self._chunks(data, sess):
            server.process_ctr_frame(frame, conn)
        _flag, reply = server.process_ctr_frame(_frame("SCHE", pack("<H", sess)), conn)
        self.assertEqual(_body(reply)["reason"], "crc_mismatch")
        sess = self._schb(server, conn, data)["sess"]
        server.process_ctr_frame(self._chunks(data, sess)[0], conn)
        _flag, reply = server.process_ctr_frame(_frame("SCHE", pack("<H", sess)), conn)
        self.assertEqual(_body(reply)["reason"], "incomplete")
        _flag, reply = server.process_ctr_frame(_frame("SCHE", pack("<H", sess)), conn)
        self.assertEqual(_body(reply)["reason"], "bad_session")
        self.assertIsNone(hwc.scheduler.schedule)

    def test_out_of_order_and_overflow(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        conn = ConnState()
        data = self._doc()
        sess = self._schb(server, conn, data)["sess"]
        chunks = self._chunks(data, sess)
        _flag, reply = server.process_ctr_frame(chunks[1], conn)
        self.assertEqual(_body(reply), {"ok": False, "reason": "out_of_order", "seq": 1})
        _flag, reply = server.process_ctr_frame(chunks[0], conn)
        self.assertEqual(_body(reply)["reason"], "bad_session", "the session was aborted")
        sess = self._schb(server, conn, data)["sess"]
        _flag, reply = server.process_ctr_frame(
            _frame("SCHC", pack("<HHH", sess, 0, 117) + bytes(114)), conn)
        self.assertEqual(_body(reply)["reason"], "overflow")
        self.assertEqual(self._schb(server, conn, bytes(262145))["reason"], "too_large")
        self.assertEqual(self._schb(server, conn, data, nchunks=0)["reason"], "bad_request")

    def test_new_schb_replaces_session_and_expiry(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        conn = ConnState()
        data = self._doc()
        first = self._schb(server, conn, data)["sess"]
        second = self._schb(server, conn, data)["sess"]
        self.assertNotEqual(first, second)
        _flag, reply = server.process_ctr_frame(self._chunks(data, first)[0], conn)
        self.assertEqual(_body(reply)["reason"], "bad_session")
        server.SCHED_SESSION_IDLE_SEC = 0.05
        time.sleep(0.1)
        _flag, reply = server.process_ctr_frame(self._chunks(data, second)[0], conn)
        self.assertEqual(_body(reply)["reason"], "bad_session", "expired session")

    def test_sche_parse_error_and_disabled(self):
        hwc = self.make_hwc()
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        conn = ConnState()
        bad = b'["not","an","object"]'
        sess = self._schb(server, conn, bad)["sess"]
        for frame in self._chunks(bad, sess):
            server.process_ctr_frame(frame, conn)
        _flag, reply = server.process_ctr_frame(_frame("SCHE", pack("<H", sess)), conn)
        self.assertEqual(_body(reply)["reason"], "parse_error")
        data = self._doc(2)
        sess = self._schb(server, conn, data)["sess"]
        for frame in self._chunks(data, sess):
            server.process_ctr_frame(frame, conn)
        _flag, reply = server.process_ctr_frame(_frame("SCHE", pack("<H", sess)), conn)
        body = _body(reply)   # SCHE is always JSON, also without XACK
        self.assertEqual((body["applied"], body["reason"], body["entries"]), (False, "disabled", 2))


class TesterRecal(_HwcCase):
    """RECL (G.5) and recal_on_gain_change (I.8)."""

    class _Bus:
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

        def close(self):
            pass

    def test_recl_writes_relay_and_reinits(self):
        hwc = self.make_hwc({"calibration": {"en_wire_recal": "1"}})
        hwc.event_bus = self._Bus()
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("RECL"), conn)
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"], body["fsm"]), (True, "ok", "INIT"))
        req = hwc_relay.read_recal_request("_data_control/recal_request")
        self.assertEqual((req[0], req[1], req[3]), (hwc.epoch, 1, "wire"))
        self.assertEqual([(e.event_type, e.payload) for e in hwc.event_bus.events],
                         [("recal_requested", {"generation": 1})])
        hwc.current_state = "STATE_IQ_CAL"
        server.process_ctr_frame(_frame("RECL"))
        self.assertEqual(hwc_relay.read_recal_request("_data_control/recal_request")[1], 2)

    def test_recal_on_gain_change(self):
        hwc = self.make_hwc({"calibration": {"en_wire_recal": "1", "recal_on_gain_change": "1"}})
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        conn = self.extended_conn(server)
        _flag, reply = server.process_ctr_frame(_frame("GAIN", pack("<IIII", 297, 297, 297, 297)), conn)
        self.assertEqual(_body(reply)["fsm"], "INIT")
        self.assertEqual(hwc.recal_generation, 1)

    def test_recal_on_gain_change_requires_wire_recal(self):
        hwc = self.make_hwc({"calibration": {"recal_on_gain_change": "1"}})
        self.assertFalse(hwc.recal_on_gain_change)

    def test_scheduler_gain_does_not_recalibrate(self):
        """Only a wire GAIN recalibrates: the scheduler's per-hop gain
        command (_handle_control_reqest without from_wire) must not re-enter
        STATE_INIT on every hop."""
        hwc = self.make_hwc({"calibration": {"en_wire_recal": "1", "recal_on_gain_change": "1"}})
        self.assertEqual(hwc._handle_control_reqest("GAIN", [297] * M), (True, "ok"))
        self.assertEqual((hwc.recal_generation, hwc.current_state), (0, "STATE_IQ_CAL"))
        self.assertFalse(os.path.exists("_data_control/recal_request"))

    def test_recl_relay_write_failure_is_applied_null(self):
        """The hw_controller-side INIT happens but delay_sync never sees the
        request: applied:null with the error, never applied:false."""
        hwc = self.make_hwc({"calibration": {"en_wire_recal": "1"}})
        hwc.recal_request_fname = join(self.workdir, "missing_dir", "recal_request")
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        _flag, reply = server.process_ctr_frame(_frame("RECL"), self.extended_conn(server))
        body = _body(reply)
        self.assertIsNone(body["applied"])
        self.assertTrue(body["reason"].startswith("error:"), body["reason"])
        self.assertEqual((body["fsm"], hwc.recal_generation), ("INIT", 1))

    def test_gain_relay_write_failure_keeps_ok(self):
        """The GAIN was applied and confirmed; a failed recal relay write
        only logs and does not turn the ack into a failure."""
        hwc = self.make_hwc({"calibration": {"en_wire_recal": "1", "recal_on_gain_change": "1"}})
        hwc.recal_request_fname = join(self.workdir, "missing_dir", "recal_request")
        server = hwc.ctr_iface_server
        self.run_main_loop(hwc)
        _flag, reply = server.process_ctr_frame(_frame("GAIN", pack("<IIII", 297, 297, 297, 297)),
                                                self.extended_conn(server))
        body = _body(reply)
        self.assertEqual((body["applied"], body["reason"], body["fsm"]), (True, "ok", "INIT"))
        self.assertEqual(hwc.gains, [hwc.valid_gains.index(297)] * M)


class TesterHelperResults(_HwcCase):
    """D.6.2 helper contract: False when rtl_daq did not confirm."""

    def test_control_noise_source_reports_confirmation(self):
        hwc = self.make_hwc()
        self.assertTrue(hwc._control_noise_source(True))
        self.assertTrue(hwc._control_noise_source(False))
        hwc._rtl_daq_transaction = lambda msg: None
        self.assertFalse(hwc._control_noise_source(True))
        self.assertFalse(hwc._control_noise_source(False))
        self.assertFalse(hwc.noise_source_state, "FSM bookkeeping unchanged")


class TesterQuietExistingKeys(_HwcCase):
    """Existing keys hw_controller only reads for derived values never log a
    new WARNING on the default path."""

    def test_unparsable_existing_keys_fall_back_quietly(self):
        with self.assertLogs("hw_controller", level="DEBUG") as logs:
            hwc = self.make_hwc({"daq": {"sample_rate": "2.4e6"},
                                 "pre_processing": {"decimation_ratio": "x"},
                                 "hw": {"en_bias_tee": "1x, 0 ,junk,7"}})
        noisy = [r.getMessage() for r in logs.records if r.levelno >= logging.WARNING and
                 any(k in r.getMessage() for k in ("sample_rate", "decimation_ratio", "en_bias_tee"))]
        self.assertEqual(noisy, [])
        self.assertEqual((hwc.sample_rate, hwc.decimation_ratio), (2400000, 1))
        self.assertEqual(hwc.hw_bias_tee, [1, 0, 0, 1], "parsed like rtl_daq's atoi")


class TesterServerModes(_HwcCase):
    """ctl_max_connections, backlog and idle timeouts (D.6.1)."""

    class _SockMock:
        def __init__(self, server):
            self.server = server
            self.backlog = None

        def bind(self, addr):
            self.addr = addr

        def listen(self, backlog):
            self.backlog = backlog

        def accept(self):
            # What shutdown() does: set the flag, then accept() fails
            self.server._shutdown_requested = True
            raise OSError("closed")

        def close(self):
            pass

    def _backlog(self, max_connections):
        server = CtrIfaceServer(M, port=0, max_connections=max_connections)
        server.ctr_iface_socket.close()
        server.ctr_iface_socket = self._SockMock(server)
        server.run()
        return server.ctr_iface_socket.backlog

    def test_listen_backlog(self):
        self.assertEqual(self._backlog(1), 4)
        self.assertEqual(self._backlog(2), 16)

    class _ScriptedSock(_SockMock):
        """accept() follows a script of connections / exceptions; when the
        script runs out it requests shutdown like shutdown() does."""

        def __init__(self, server, script):
            super().__init__(server)
            self.script, self.accepts = list(script), 0

        def accept(self):
            self.accepts += 1
            if not self.script:
                return super().accept()
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item, ("127.0.0.1", 0)

    class _Conn:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def _run_scripted(self, script, thread_cls=None):
        server = CtrIfaceServer(M, port=0, max_connections=2)
        server.ctr_iface_socket.close()
        server.ctr_iface_socket = self._ScriptedSock(server, script)
        server.ACCEPT_BACKOFF_S = 0.01
        real_thread = threading.Thread
        runner = real_thread(target=server.run, daemon=True)
        if thread_cls is not None:
            # hw_controller and this module share the threading module, so
            # the patch (active only while run() works) must not hit runner
            with mock.patch("hw_controller.threading.Thread", thread_cls):
                runner.start()
                runner.join(10)
        else:
            runner.start()
            runner.join(10)
        self.assertFalse(runner.is_alive(), "the accept loop must end")
        return server

    def test_bounded_transient_accept_error_keeps_serving(self):
        import errno
        server = self._run_scripted([OSError(errno.ECONNABORTED, "aborted"),
                                     OSError(errno.EMFILE, "too many files")])
        self.assertEqual(server.ctr_iface_socket.accepts, 3)

    def test_bounded_worker_start_failure_releases_slot(self):
        conns = [self._Conn() for _ in range(3)]

        class _NoThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        # 3 failed starts with 2 slots: a leaked slot would block the 3rd accept
        server = self._run_scripted(conns, thread_cls=_NoThread)
        self.assertEqual(server.ctr_iface_socket.accepts, 4)
        self.assertTrue(all(c.closed for c in conns))

    def test_bounded_shutdown_while_all_workers_busy(self):
        server, port = self._server(max_connections=2)
        c1, c2 = self._client(port), self._client(port)
        for c in (c1, c2):
            c.sendall(_frame("HWSQ"))
            CtrIfaceServer._recv_exact(c, 128)
        server.shutdown()
        server.join(5)
        self.assertFalse(server.is_alive(), "shutdown ends the loop with every slot taken")

    def _server(self, **kw):
        server = CtrIfaceServer(M, port=0, listen_address="127.0.0.1", **kw)
        server.start()
        self.addCleanup(server.shutdown)
        self.assertTrue(server.listening.wait(5))
        return server, server.ctr_iface_socket.getsockname()[1]

    def _client(self, port):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def test_two_concurrent_clients_third_waits_in_backlog(self):
        _server, port = self._server(max_connections=2)
        c1, c2 = self._client(port), self._client(port)
        for c in (c1, c2):
            c.sendall(_frame("HWSQ"))
            self.assertIn("fsm", _body(CtrIfaceServer._recv_exact(c, 128)))
        c3 = self._client(port)
        c3.sendall(_frame("HWSQ"))
        c3.settimeout(0.5)
        with self.assertRaises(socket.timeout):
            c3.recv(128)
        c1.close()
        c3.settimeout(5)
        self.assertIn("fsm", _body(CtrIfaceServer._recv_exact(c3, 128)))
        c2.sendall(_frame("SCHG"))
        self.assertIn("gen", _body(CtrIfaceServer._recv_exact(c2, 128)))

    def test_single_connection_mode_is_sequential(self):
        _server, port = self._server()
        c1 = self._client(port)
        c1.sendall(_frame("HWSQ"))
        CtrIfaceServer._recv_exact(c1, 128)
        c2 = self._client(port)
        c2.sendall(_frame("HWSQ"))
        c2.settimeout(0.4)
        with self.assertRaises(socket.timeout):
            c2.recv(128)
        c1.close()
        c2.settimeout(5)
        self.assertIn("fsm", _body(CtrIfaceServer._recv_exact(c2, 128)))

    def test_xack_idle_closes_only_xack_connections(self):
        _server, port = self._server(max_connections=2, xack_idle_sec=0.3)
        ext, legacy = self._client(port), self._client(port)
        ext.sendall(_xack_frame())
        self.assertTrue(_body(CtrIfaceServer._recv_exact(ext, 128))["xack"])
        legacy.sendall(_frame("HWSQ"))
        CtrIfaceServer._recv_exact(legacy, 128)
        time.sleep(0.9)
        ext.settimeout(2)
        self.assertEqual(ext.recv(128), b"", "idle XACK connection closed by the server")
        legacy.sendall(_frame("HWSQ"))
        self.assertIn("fsm", _body(CtrIfaceServer._recv_exact(legacy, 128)))

    def test_idle_timeout_for_every_connection(self):
        _server, port = self._server(idle_timeout_sec=0.3)
        legacy = self._client(port)
        legacy.sendall(_frame("HWSQ"))
        CtrIfaceServer._recv_exact(legacy, 128)
        time.sleep(0.8)
        legacy.settimeout(2)
        self.assertEqual(legacy.recv(128), b"")

    def test_ctl_keys_parsed_and_invalid_values_default(self):
        hwc = self.make_hwc({"daq": {"ctl_max_connections": "4", "ctl_xack_idle_sec": "12",
                                     "ctl_idle_timeout_sec": "7", "ctl_request_timeout_sec": "0.1"}})
        server = hwc.ctr_iface_server
        self.assertEqual((server.max_connections, server.xack_idle_s, server.idle_timeout_s,
                          server.request_timeout_s), (4, 12.0, 7.0, 10.0))


class TesterSizeBudgets(unittest.TestCase):
    """Every new reply built at its maxima fits the 124-byte payload (D.6.2)."""

    def setUp(self):
        self.ctl = ControlPlane(31, request_timeout_sec=600.0, handler_max_sec=120.0,
                                frame_period_sec=5000.0, en_wire_recal=True,
                                fence_max_term_jump=U32_MAX)
        self.server = CtrIfaceServer(31, port=0, control=self.ctl)
        self.addCleanup(self.server.ctr_iface_socket.close)

    def _check(self, payload, expected_len=None):
        self.assertLessEqual(len(payload), 124)
        json.loads(payload.decode("utf-8"))
        if expected_len is not None:
            self.assertEqual(len(payload), expected_len)

    def test_xack_maxima(self):
        conn = ConnState()
        reply = self.ctl.handle_xack(U32_MAX, "default", conn)
        self._check(CtrIfaceServer._json_payload(reply), 118)
        self.ctl.cluster_id = "x" * 31
        reply = self.ctl.handle_xack(1, "y" * 31, ConnState())
        self._check(CtrIfaceServer._json_payload(reply), 59)

    def test_hwsq_and_schg_maxima(self):
        with self.ctl.fence_lock:
            self.ctl.max_term_seen = U32_MAX
        self.ctl.publish({"fsm": "NOISE_CTR_WAIT", "pend_deferred": 500, "ns": 1, "agc": 1,
                          "nch": 31, "f": 2**40, "fc": 123999999, "gen": U32_MAX,
                          "loaded": True, "active": True, "st": "ACTIVE_WAITING_CAL",
                          "idx": 10**7, "total": 10**7}, frame=True)
        with self.ctl._snap_lock:
            self.ctl._snap["last_frame_mono"] -= 10**6
        hwsq = self.ctl.hwsq(queued=8)
        self.assertEqual((hwsq["pend"], hwsq["f"], hwsq["fage"], hwsq["fc"]), (99, U32_MAX, 99999, 999999))
        hwsq["fr"] = 0
        self._check(CtrIfaceServer._json_payload(hwsq), 120)
        self._check(CtrIfaceServer._json_payload(self.ctl.schg()), 98)

    def test_ack_maxima(self):
        with self.ctl._seq_lock:
            self.ctl._seq = U32_MAX - 1
        self._check(self.server._ack_payload(None, "deferred_timeout", fsm="NOISE_CTR_WAIT"), 84)
        with self.ctl._seq_lock:
            self.ctl._seq = U32_MAX - 1
        self._check(self.server._ack_payload(False, "crc_mismatch", {"entries": 99999},
                                             fsm="NOISE_CTR_WAIT"), 97)
        self._check(self.server._ack_payload(False, "error:" + "é" * 200, fsm="NOISE_CTR_WAIT"))

    def test_error_truncation_keeps_as_much_as_fits(self):
        """Truncation keeps the longest reason prefix that fits, whatever
        the per-character JSON escape cost (astral = 12-byte surrogate pair)."""
        for ch in ("\U0001F600", "\u2603", '"', "\\", "a"):
            payload = self.server._ack_payload(False, "error:" + ch * 200, fsm="NOISE_CTR_WAIT")
            self._check(payload)
            reason = json.loads(payload.decode("utf-8"))["reason"]
            self.assertTrue(reason.startswith("error:" + ch), (ch, reason))
            longer = self.server._ack_payload(False, reason + ch, fsm="NOISE_CTR_WAIT")
            self.assertNotEqual(json.loads(longer.decode("utf-8"))["reason"], reason + ch,
                                "one more character would not have fit")
        self._check(CtrIfaceServer._json_payload({"ok": False, "reason": "out_of_order", "seq": 65535}), 48)


class TesterStateRelay(_HwcCase):
    """hw_controller state + events relay writer (G.3, G.8)."""

    class _Event:
        def __init__(self, i):
            self.timestamp = 1.0 + i
            self.severity = "info"
            self.module = "hw_controller"
            self.event_type = "freq_change"
            self.payload = {"frequency": i, "np": hw_controller.np.int64(i)}

    def test_document_written_every_five_frames_or_when_dirty(self):
        hwc = self.make_hwc({"monitoring": {"en_monitoring": "0", "en_hwc_state_relay": "1"}})
        writes = []
        with mock.patch.object(hwc_relay, "write_hwc_state",
                               side_effect=lambda path, doc: writes.append((path, doc))):
            for _ in range(4):
                hwc._relay_tick()
            self.assertEqual(writes, [])
            hwc._relay_tick()
            self.assertEqual(len(writes), 1)
            hwc._relay_event_handler(self._Event(1))
            hwc._relay_tick()
            self.assertEqual(len(writes), 2)
        path, doc = writes[-1]
        self.assertEqual(path, "_data_control/hwc_state")
        for key in ("v", "epoch", "seq", "ts", "pid", "fsm", "pending", "noise_source", "agc",
                    "gains_idx", "gains_tenths", "ext_lna_gains_db", "bias_tee", "schedule",
                    "orientation", "frame_age_ms", "max_term_seen", "cal_stalled", "events"):
            self.assertIn(key, doc)
        self.assertEqual(doc["epoch"], hwc.epoch)
        self.assertEqual(doc["seq"], 1)
        self.assertEqual(doc["events"][0]["seq"], 0)
        self.assertEqual(doc["events"][0]["payload"], {"frequency": 1, "np": 1})
        json.dumps(doc)

    def test_real_file_round_trip(self):
        hwc = self.make_hwc({"monitoring": {"en_monitoring": "0", "en_hwc_state_relay": "1"},
                             "federation": {"instance_id": "2"}})
        hwc._relay_event_handler(self._Event(3))
        hwc._relay_tick()
        doc = hwc_relay.read_hwc_state("_data_control/inst2_hwc_state")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["fsm"], "IQ_CAL")
        self.assertEqual([e["seq"] for e in doc["events"]], [0])

    def test_disabled_writes_nothing(self):
        hwc = self.make_hwc()
        for _ in range(10):
            hwc._relay_tick()
        self.assertFalse(os.path.exists("_data_control/hwc_state"))

    def test_concurrent_events_and_main_loop_writer(self):
        """Events from the EventBus thread while the main loop writes: no
        'deque mutated during iteration', and only the main thread writes."""
        hwc = self.make_hwc({"monitoring": {"en_monitoring": "1", "en_hwc_state_relay": "1"}})
        writer_threads = set()

        def _record(path, doc):
            writer_threads.add(threading.get_ident())
            json.dumps(doc)

        stop = threading.Event()
        errors = []

        def _emitter():
            from daq_events import DAQEvent
            i = 0
            while not stop.is_set():
                try:
                    hwc.event_bus.emit(DAQEvent(severity="info", module="hw_controller",
                                                event_type="freq_change", payload={"i": i}))
                    hwc._relay_event_handler(self._Event(i))
                except Exception as e:  # pragma: no cover - failure path
                    errors.append(e)
                i += 1

        threads = [threading.Thread(target=_emitter) for _ in range(2)]
        with mock.patch.object(hwc_relay, "write_hwc_state", side_effect=_record):
            for t in threads:
                t.start()
            try:
                for _ in range(2000):
                    hwc._relay_tick()
            finally:
                stop.set()
                for t in threads:
                    t.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(writer_threads, {threading.get_ident()})
        self.assertLessEqual(len(hwc._relay_events), 32)


class TesterCalStall(_HwcCase):
    """[calibration] cal_stall_frames (I.8, G63)."""

    def test_emits_once_and_clears(self):
        hwc = self.make_hwc({"calibration": {"cal_stall_frames": "3"}})
        bus = TesterRecal._Bus()
        hwc.event_bus = bus
        hwc.iq_header.noise_source_state = 1
        hwc.iq_header.sync_state = 2
        for _ in range(6):
            hwc._check_cal_stall()
        self.assertTrue(hwc.cal_stalled)
        self.assertEqual([(e.event_type, e.payload) for e in bus.events],
                         [("cal_stalled", {"frames": 3, "sync_state": 2})])
        hwc.iq_header.sync_state = 5
        hwc._check_cal_stall()
        self.assertFalse(hwc.cal_stalled)

    def test_off_by_default(self):
        hwc = self.make_hwc()
        hwc.iq_header.noise_source_state = 1
        for _ in range(1000):
            hwc._check_cal_stall()
        self.assertFalse(hwc.cal_stalled)


if __name__ == "__main__":
    unittest.main()
