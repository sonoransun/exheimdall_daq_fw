"""
    Unit tests for Federation modules

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import sys
import os
import json
import time
import socket
import struct
import threading
import unittest

# Add _daq_core to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '_daq_core'))



def _free_port():
    """Grab an ephemeral TCP port for servers that cannot report a
    kernel-assigned port back (fixed ports EADDRINUSE-flake under parallel
    or repeated CI runs)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port, timeout=5.0):
    """Deterministically wait until a server accepts connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.02)
    return False


class FakeHWCServer:
    """Minimal stand-in for hw_controller's CtrIfaceServer: accepts one
    128-byte frame per connection and replies 'FNSD' + 124 zero bytes."""

    def __init__(self, port=0):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self._stop = threading.Event()
        self.frames = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            try:
                data = conn.recv(128)
                if len(data) == 128:
                    self.frames.append(data)
                    conn.sendall(b"FNSD" + b"\x00" * 124)
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.srv.close()


class TestFederationHealth(unittest.TestCase):
    """Tests for federation_health.py"""

    def test_init_peer_table(self):
        from federation_health import FederationHealth
        fh = FederationHealth(instance_id=0, peer_addresses=["localhost:5102", "localhost:5202"])
        table = fh.get_peer_table()
        self.assertEqual(len(table), 2)
        self.assertIn("localhost:5102", table)
        self.assertIn("localhost:5202", table)
        self.assertFalse(table["localhost:5102"]["alive"])
        fh.close()

    def test_get_healthy_peers_empty(self):
        from federation_health import FederationHealth
        fh = FederationHealth(instance_id=0, peer_addresses=["localhost:9999"])
        self.assertEqual(fh.get_healthy_peers(), [])
        fh.close()

    def test_coordinator_election_self(self):
        from federation_health import FederationHealth
        fh = FederationHealth(instance_id=2, peer_addresses=[])
        self.assertEqual(fh.get_coordinator_id(), 2)
        fh.close()

    def test_poll_unreachable_peer(self):
        from federation_health import FederationHealth
        fh = FederationHealth(instance_id=0, peer_addresses=["localhost:19999"],
                              poll_interval=0.5)
        # Manually poll
        fh._poll_peer("localhost:19999")
        table = fh.get_peer_table()
        # Should not be alive (never was alive, so no peer_down event)
        self.assertFalse(table["localhost:19999"]["alive"])
        fh.close()

    def test_start_stop(self):
        from federation_health import FederationHealth
        fh = FederationHealth(instance_id=0, peer_addresses=[], poll_interval=0.2)
        fh.start()
        time.sleep(0.3)
        fh.close()
        # Should not raise

    def test_canonical_peer_form_derives_instance_id_and_port(self):
        """'host:instance_id' entries carry the peer's instance_id and derive
        the status port via base + id * stride."""
        from federation_health import FederationHealth
        fh = FederationHealth(instance_id=0,
                              peer_addresses=["localhost:1", "10.0.0.2:3"])
        table = fh.get_peer_table()
        self.assertEqual(table["localhost:1"]["instance_id"], 1)
        self.assertEqual(table["10.0.0.2:3"]["instance_id"], 3)
        self.assertEqual(fh._peer_endpoints["localhost:1"], ("localhost", 5102))
        self.assertEqual(fh._peer_endpoints["10.0.0.2:3"], ("10.0.0.2", 5302))
        fh.close()

    def test_legacy_port_form_derives_instance_id(self):
        """'host:status_port' entries still poll the given port; the
        instance_id is recovered from the port formula when it fits."""
        from federation_health import FederationHealth
        fh = FederationHealth(instance_id=0,
                              peer_addresses=["localhost:5102", "localhost:19999"])
        table = fh.get_peer_table()
        self.assertEqual(table["localhost:5102"]["instance_id"], 1)
        self.assertEqual(table["localhost:19999"]["instance_id"], -1)
        self.assertEqual(fh._peer_endpoints["localhost:5102"], ("localhost", 5102))
        self.assertEqual(fh._peer_endpoints["localhost:19999"], ("localhost", 19999))
        fh.close()

    def test_election_uses_config_derived_instance_ids(self):
        """A live peer with a lower config-derived instance_id wins the
        election even when its STATUS snapshot lacks instance_id."""
        from federation_health import FederationHealth
        fh = FederationHealth(instance_id=2, peer_addresses=["localhost:0"])
        with fh._lock:
            fh._peer_table["localhost:0"].update(
                {"alive": True, "health": "ok", "last_seen": time.time()})
        fh._check_coordinator()
        self.assertEqual(fh.get_coordinator_id(), 0)
        fh.close()

    def test_election_with_status_server(self):
        """End-to-end: poll a real StatusServer whose snapshot includes
        instance_id, then elect the lowest id."""
        from federation_health import FederationHealth
        from daq_status_server import StatusServer
        # Port chosen so it does NOT fit the base + id * stride formula:
        # the instance_id must come from the peer's STATUS snapshot.
        port = _free_port()
        server = StatusServer(port=port, instance_id=0)
        server.start()
        self.assertTrue(_wait_listening(port), "StatusServer did not come up")
        server.update_status({"sync_state": 6, "counters": {}})
        try:
            fh = FederationHealth(instance_id=4, peer_addresses=[
                "localhost:{:d}".format(port)])
            fh._poll_peer("localhost:{:d}".format(port))
            table = fh.get_peer_table()
            entry = table["localhost:{:d}".format(port)]
            self.assertTrue(entry["alive"])
            self.assertEqual(entry["instance_id"], 0)
            fh._check_coordinator()
            self.assertEqual(fh.get_coordinator_id(), 0)
            fh.close()
        finally:
            server.close()


class TestFederationCoordinator(unittest.TestCase):
    """Tests for federation_coordinator.py"""

    def test_coordinator_ping(self):
        from federation_coordinator import FederationCoordinator
        port = _free_port()
        coord = FederationCoordinator(port=port, instances=[])
        coord.start()
        self.assertTrue(_wait_listening(port), "coordinator did not come up")
        try:
            with socket.create_connection(("localhost", port), timeout=2) as sock:
                sock.sendall(b"PING\n")
                data = sock.recv(4096)
                response = json.loads(data.decode())
                self.assertTrue(response["ok"])
                self.assertIn("ts", response)
        finally:
            coord.close()

    def test_coordinator_status_no_instances(self):
        from federation_coordinator import FederationCoordinator
        port = _free_port()
        coord = FederationCoordinator(port=port, instances=[])
        coord.start()
        self.assertTrue(_wait_listening(port), "coordinator did not come up")
        try:
            with socket.create_connection(("localhost", port), timeout=2) as sock:
                sock.sendall(b"STATUS\n")
                data = sock.recv(4096)
                response = json.loads(data.decode())
                self.assertIn("federation_health", response)
                self.assertEqual(response["instance_count"], 0)
        finally:
            coord.close()

    def test_coordinator_unknown_command(self):
        from federation_coordinator import FederationCoordinator
        port = _free_port()
        coord = FederationCoordinator(port=port, instances=[])
        coord.start()
        self.assertTrue(_wait_listening(port), "coordinator did not come up")
        try:
            with socket.create_connection(("localhost", port), timeout=2) as sock:
                sock.sendall(b"FOOBAR\n")
                data = sock.recv(4096)
                response = json.loads(data.decode())
                self.assertIn("error", response)
        finally:
            coord.close()

    def test_parse_instances(self):
        from federation_coordinator import _parse_instances
        result = _parse_instances("localhost:0,192.168.1.10:1")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["host"], "localhost")
        self.assertEqual(result[0]["instance_id"], 0)
        self.assertEqual(result[1]["host"], "192.168.1.10")
        self.assertEqual(result[1]["instance_id"], 1)

    def test_encode_hwc_command_wire_format(self):
        """Commands must be encoded as the real 128-byte HWC frames:
        binary little-endian payloads, not ASCII text."""
        from federation_coordinator import FederationCoordinator
        enc = FederationCoordinator._encode_hwc_command

        verb, payload = enc("FREQ 433000000")
        self.assertEqual(verb, "FREQ")
        self.assertEqual(struct.unpack("<Q", payload)[0], 433000000)

        verb, payload = enc("GAIN 100,200,300,400,500")
        self.assertEqual(verb, "GAIN")
        self.assertEqual(struct.unpack("<5I", payload), (100, 200, 300, 400, 500))

        verb, payload = enc("EGAN 145 145 145 145 145")
        self.assertEqual(verb, "EGAN")
        self.assertEqual(struct.unpack("<5I", payload), (145,) * 5)

        verb, payload = enc("ORNT 123.5 45.0")
        self.assertEqual(verb, "ORNT")
        az, el = struct.unpack("<ff", payload)
        self.assertAlmostEqual(az, 123.5, places=3)
        self.assertAlmostEqual(el, 45.0, places=3)

        # Short verbs carry the trailing space hw_controller compares against
        self.assertEqual(enc("AGC"), ("AGC ", b""))
        self.assertEqual(enc("RFQ"), ("RFQ ", b""))
        self.assertEqual(enc("SCHS"), ("SCHS", b""))

        verb, payload = enc('SCHD {"entries":[{"frequency":433000000}]}')
        self.assertEqual(verb, "SCHD")
        self.assertEqual(json.loads(payload.decode())["entries"][0]["frequency"],
                         433000000)

        with self.assertRaises(ValueError):
            enc("BOGUS 1")
        with self.assertRaises(ValueError):
            enc("FREQ notanumber")

    def test_fan_out_sends_real_frames(self):
        """A FREQ fan-out arrives as a decodable 128-byte binary frame."""
        from federation_coordinator import FederationCoordinator
        hwc = FakeHWCServer()
        try:
            coord = FederationCoordinator(port=_free_port(), instances=[
                {"host": "127.0.0.1", "instance_id": 0, "port_stride": 100}])
            # Redirect the computed HWC port at the fake server
            coord._compute_instance_ports = lambda inst: {
                "hwc_port": hwc.port, "status_port": 0, "iq_port": 0}
            result = coord._fan_out_command("FREQ", "433000000")
            self.assertEqual(result["results"]["0"], {"ok": True})
            time.sleep(0.1)
            self.assertEqual(len(hwc.frames), 1)
            frame = hwc.frames[0]
            self.assertEqual(len(frame), 128)
            self.assertEqual(frame[:4], b"FREQ")
            self.assertEqual(struct.unpack("<Q", frame[4:12])[0], 433000000)
        finally:
            hwc.close()


class TestFederationScheduler(unittest.TestCase):
    """Tests for federation_scheduler.py"""

    def test_round_robin_partition(self):
        from federation_scheduler import FederationScheduler
        fs = FederationScheduler()
        fs.set_master_schedule(
            frequencies=[433000000, 868000000, 915000000, 1090000000],
            gains=[[40] * 5, [40] * 5, [40] * 5, [40] * 5],
            dwell_frames=[100, 100, 100, 100],
            strategy="round_robin"
        )
        assignments = fs.partition_schedule(instance_ids=[0, 1])
        self.assertIn(0, assignments)
        self.assertIn(1, assignments)
        # Round robin: instance 0 gets indices 0,2; instance 1 gets 1,3
        self.assertEqual(assignments[0]["frequencies"], [433000000, 915000000])
        self.assertEqual(assignments[1]["frequencies"], [868000000, 1090000000])

    def test_range_partition(self):
        from federation_scheduler import FederationScheduler
        fs = FederationScheduler()
        fs.set_master_schedule(
            frequencies=[100, 200, 300, 400],
            gains=[[1] * 5, [2] * 5, [3] * 5, [4] * 5],
            dwell_frames=[10, 20, 30, 40],
            strategy="range"
        )
        assignments = fs.partition_schedule(instance_ids=[0, 1], strategy="range")
        # Range: sorted, first half to 0, second half to 1
        self.assertEqual(assignments[0]["frequencies"], [100, 200])
        self.assertEqual(assignments[1]["frequencies"], [300, 400])

    def test_single_instance_gets_all(self):
        from federation_scheduler import FederationScheduler
        fs = FederationScheduler()
        fs.set_master_schedule(
            frequencies=[433000000, 868000000],
            gains=[[40] * 5, [40] * 5],
            dwell_frames=[100, 100]
        )
        assignments = fs.partition_schedule(instance_ids=[0])
        self.assertEqual(len(assignments[0]["frequencies"]), 2)

    def test_no_master_schedule(self):
        from federation_scheduler import FederationScheduler
        fs = FederationScheduler()
        assignments = fs.partition_schedule(instance_ids=[0])
        self.assertEqual(assignments, {})

    def test_get_assignments(self):
        from federation_scheduler import FederationScheduler
        fs = FederationScheduler()
        fs.set_master_schedule([100, 200], [[1] * 5, [2] * 5], [10, 20])
        fs.partition_schedule(instance_ids=[0])
        assignments = fs.get_assignments()
        self.assertIn(0, assignments)

    def test_distribute_sends_schd_json(self):
        """Distribution must use the SCHD verb with JSON that
        ScheduleParser.from_json can load — not an invented SCHEDULE verb."""
        from federation_scheduler import FederationScheduler
        sent = []

        class FakeCoordinator:
            def _send_to_instance(self, iid, cmd_str):
                sent.append((iid, cmd_str))
                return {"ok": True}

        fs = FederationScheduler(coordinator=FakeCoordinator())
        fs.set_master_schedule(
            frequencies=[433000000, 868000000],
            gains=[None, None],
            dwell_frames=[100, 200])
        fs.partition_schedule(instance_ids=[0, 1])
        results = fs.distribute()
        self.assertEqual(results[0], {"ok": True})
        self.assertEqual(len(sent), 2)
        for iid, cmd_str in sent:
            self.assertTrue(cmd_str.startswith("SCHD "))
            payload = cmd_str[5:]
            self.assertLessEqual(len(payload.encode()), 124)
            # Round-trip through the real schedule parser
            from signal_scheduler import ScheduleParser
            sched = ScheduleParser.from_json(payload)
            self.assertEqual(len(sched.entries), 1)
        # Round-robin: instance 0 got the first frequency
        sched0 = json.loads(sent[0][1][5:])
        self.assertEqual(sched0["entries"][0]["frequency"], 433000000)
        self.assertEqual(sched0["entries"][0]["dwell_frames"], 100)

    def test_distribute_large_schedule_falls_back_to_file(self):
        """Schedules too large for the 124-byte inline payload are handed
        over as a JSON file path."""
        from federation_scheduler import FederationScheduler
        sent = []

        class FakeCoordinator:
            def _send_to_instance(self, iid, cmd_str):
                sent.append((iid, cmd_str))
                return {"ok": True}

        fs = FederationScheduler(coordinator=FakeCoordinator())
        freqs = [400000000 + i * 1000000 for i in range(10)]
        fs.set_master_schedule(freqs, [None] * 10, [100] * 10)
        fs.partition_schedule(instance_ids=[0])
        fs.distribute()
        self.assertEqual(len(sent), 1)
        payload = sent[0][1][5:]
        self.assertFalse(payload.startswith("{"))  # a path, not inline JSON
        try:
            with open(payload) as f:
                sched = json.load(f)
            self.assertEqual(len(sched["entries"]), 10)
        finally:
            os.unlink(payload)


class TestFederationIQRouter(unittest.TestCase):
    """Tests for federation_iq_router.py"""

    def test_init_stats(self):
        from federation_iq_router import FederationIQRouter
        router = FederationIQRouter(
            instance_configs=[
                {"host": "localhost", "instance_id": 0, "iq_port": 5000},
                {"host": "localhost", "instance_id": 1, "iq_port": 5100},
            ],
            output_port=_free_port()
        )
        stats = router.get_stream_stats()
        self.assertEqual(len(stats), 2)
        self.assertEqual(stats[0]["frames_received"], 0)
        self.assertEqual(stats[1]["frames_received"], 0)

    def test_start_stop(self):
        from federation_iq_router import FederationIQRouter
        router = FederationIQRouter(
            instance_configs=[],
            output_port=_free_port()
        )
        router.start()
        time.sleep(0.3)  # router has no readiness hook; brief spin-up only
        router.close()


class TestInstanceNaming(unittest.TestCase):
    """Instance naming/port rules asserted against the REAL production code
    (the old version re-implemented the rules inline and asserted on its own
    local variables, so it passed regardless of the production behavior).

    The full FIFO/shared-memory loopback coverage — including instN_
    prefixing exercised end-to-end through outShmemIface/inShmemIface —
    lives in test_shmem_iface.py.
    """

    def test_compute_port_formula(self):
        """sh_mem_util.c's compute_port mirror: base + instance_id * stride
        is what every Python-side consumer implements."""
        from daq_status_server import StatusServer  # noqa: F401 (import check)
        # federation_health derives peer status ports with the same formula
        from federation_health import FederationHealth
        fh = FederationHealth(instance_id=0,
                              peer_addresses=["localhost:1", "localhost:3"],
                              status_base_port=5002, port_stride=100)
        try:
            self.assertEqual(fh._peer_endpoints["localhost:1"],
                             ("localhost", 5102))
            self.assertEqual(fh._peer_endpoints["localhost:3"],
                             ("localhost", 5302))
        finally:
            fh.close()


if __name__ == "__main__":
    unittest.main()
