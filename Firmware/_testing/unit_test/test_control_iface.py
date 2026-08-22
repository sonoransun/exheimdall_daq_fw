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

    zmq is stubbed only when the real module is unavailable (the tests never
    open a ZMQ socket).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import json
import logging
import queue
import sys
import threading
import types
import unittest
from os.path import join, dirname, realpath
from struct import pack

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)


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
from hw_controller import CtrIfaceServer, HWC, CtrRequest  # noqa: E402

M = 4  # receiver channels used throughout


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
            if request.command in HWC.QUERY_COMMANDS:
                request.reply_payload = self.query_handler(request.command)
            request.done.set()

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=5)


def _frame(verb, payload=b""):
    """Build one 128-byte control frame: 4-byte ASCII verb + 124B payload."""
    verb_bytes = verb.encode("ascii")
    assert len(verb_bytes) == 4, "verb must be exactly 4 bytes"
    assert len(payload) <= 124
    return verb_bytes + payload + bytes(124 - len(payload))


def _drain_queue():
    while True:
        try:
            hw_controller.ctr_request_queue.get_nowait()
        except queue.Empty:
            return


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
        for verb in ("INIT", "SCHS", "SCHN", "PARK", "SCAN", "OSTP"):
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
        hwc.handled = []
        hwc._handle_control_reqest = (
            lambda command, params: hwc.handled.append((command, params)))
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

        def _boom(command, params):
            raise RuntimeError("injected failure")

        hwc._handle_control_reqest = _boom
        request = CtrRequest("GAIN", [0] * M)
        hw_controller.ctr_request_queue.put(request)
        hwc._drain_control_requests(allow_mutations=True)  # must not raise
        self.assertTrue(request.done.is_set(),
                        "a failing handler must still complete the request")


if __name__ == "__main__":
    unittest.main()
