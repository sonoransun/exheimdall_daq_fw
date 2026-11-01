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
    """Minimal stand-in for a *legacy* hw_controller CtrIfaceServer: serves
    128-byte frames on each connection until the client closes and replies
    'FNSD' + 124 zero bytes to every frame (including XACK, which a legacy
    server does not know)."""

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
            except OSError:
                break
            try:
                conn.settimeout(5.0)
                while True:
                    data = b""
                    while len(data) < 128:
                        chunk = conn.recv(128 - len(data))
                        if not chunk:
                            break
                        data += chunk
                    if len(data) < 128:
                        break
                    self.frames.append(data)
                    conn.sendall(b"FNSD" + b"\x00" * 124)
            except OSError:
                pass
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
            r = result["results"]["0"]
            # today's {"ok": true} plus the additive ack keys (legacy server)
            self.assertTrue(r["ok"])
            self.assertIsNone(r["applied"])
            self.assertTrue(r["legacy_ack"])
            self.assertEqual(result["by_node"]["127.0.0.1:0"], r)
            time.sleep(0.1)
            # XACK always opens the connection, then the command frame follows
            self.assertEqual([f[:4] for f in hwc.frames], [b"XACK", b"FREQ"])
            frame = hwc.frames[1]
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
        over as a JSON file path to a same-host legacy target and refused
        for a remote legacy target (no shared filesystem)."""
        from federation_scheduler import FederationScheduler
        sent = []

        class FakeCoordinator:
            instances = [{"host": "127.0.0.1", "instance_id": 0},
                         {"host": "192.0.2.10", "instance_id": 1}]

            def _send_to_instance(self, iid, cmd_str):
                sent.append((iid, cmd_str))
                return {"ok": True}

        fs = FederationScheduler(coordinator=FakeCoordinator())
        freqs = [400000000 + i * 1000000 for i in range(20)]
        fs.set_master_schedule(freqs, [None] * 20, [100] * 20)
        fs.partition_schedule(instance_ids=[0, 1])
        results = fs.distribute()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 0)
        payload = sent[0][1][5:]
        self.assertFalse(payload.startswith("{"))  # a path, not inline JSON
        self.assertIn("heimdall_sched_", os.path.basename(payload))
        try:
            with open(payload) as f:
                sched = json.load(f)
            self.assertEqual(len(sched["entries"]), 10)
            self.assertEqual(sched["name"], "federation_inst0")
            self.assertNotIn("gen", sched)
        finally:
            os.unlink(payload)
        self.assertEqual(results[1], {"ok": False, "applied": False,
                                      "reason": "schedule_too_large_for_legacy_server",
                                      "bytes": results[1]["bytes"]})
        self.assertGreater(results[1]["bytes"], 124)


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


class _SimClock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _FakeSock(object):
    """Connected socket stand-in returning one STATUS reply."""

    def __init__(self, reply):
        self._data = json.dumps(reply).encode() + b"\n"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendall(self, data):
        pass

    def recv(self, n):
        out, self._data = self._data[:n], self._data[n:]
        return out


class _Bus(object):
    def __init__(self):
        self.events = []

    def emit(self, evt):
        self.events.append(evt.event_type)


def _status(**kw):
    base = {"sync_state": 6, "pipeline_health": "ok", "current_frequency_hz": 1,
            "frame_count": 1}
    base.update(kw)
    return base


class TestHealthStateMachine(unittest.TestCase):
    """FederationHealth C.5 state machine with injected clocks and connects."""

    def _today_down_poll_index(self, n_peers, interval, connect_cost, rounds):
        """Replay of today's _poll_peer rule (federation_health.py:106-147) on the
        simulated timeline: round 0 succeeds instantly, every later connect
        costs connect_cost seconds and fails; down iff now - last_seen > 3*interval."""
        t = 0.0
        last_seen = {i: 0.0 for i in range(n_peers)}
        down_at = {}
        for rnd in range(1, rounds):
            t += interval                      # stop_event.wait(poll_interval)
            for i in range(n_peers):
                t += connect_cost
                if i not in down_at and t - last_seen[i] > 3 * interval:
                    down_at[i] = rnd
        return down_at

    def _dead_after_first(self, clock, cost=2.0):
        state = {"round": 0}

        def connect(addr, timeout=None):
            if state["round"] == 0:
                return _FakeSock(_status())
            clock.t += cost
            raise socket.timeout("timed out")
        return connect, state

    def test_legacy_down_rule_matches_today(self):
        from federation_health import FederationHealth
        for misses in (None, 3):
            clock = _SimClock(0.0)
            connect, state = self._dead_after_first(clock)
            bus = _Bus()
            peers = ["hostx:{}".format(i) for i in range(1, 6)]
            kw = {} if misses is None else {"down_misses": misses}
            fh = FederationHealth(0, peers, poll_interval=5.0, event_bus=bus,
                                  _clock=clock, _wall=clock, _connect=connect, **kw)
            down_round = {}
            for rnd in range(6):
                state["round"] = rnd
                if rnd:
                    clock.t += 5.0
                fh.poll_round()
                for i, p in enumerate(peers):
                    if not fh.get_peer_table()[p]["alive"] and i not in down_round and rnd:
                        down_round[i] = rnd
            if misses is None:
                self.assertEqual(down_round, self._today_down_poll_index(5, 5.0, 2.0, 6))
                self.assertEqual(set(down_round.values()), {2})
            else:
                self.assertEqual(set(down_round.values()), {3})
            self.assertEqual(bus.events.count("peer_down"), 5)
            self.assertEqual(bus.events.count("peer_up"), 5)
            fh.close()

    def test_hysteresis_and_health_events(self):
        from federation_health import FederationHealth
        clock = _SimClock()
        reply = {"v": _status()}
        for emit in (False, True):
            bus = _Bus()
            fh = FederationHealth(0, ["hostx:1"], event_bus=bus, up_successes=2,
                                  emit_health_events=emit, _clock=clock, _wall=clock,
                                  _connect=lambda a, timeout=None: _FakeSock(reply["v"]))
            reply["v"] = _status()
            fh.poll_round()
            row = fh.get_member_table()["hostx:1"]
            self.assertFalse(row["alive"])
            self.assertEqual(row["state"], "unknown")
            fh.poll_round()
            self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "up")
            self.assertEqual(bus.events, ["peer_up"])
            reply["v"] = _status(sync_state=3, pipeline_health="degraded")
            fh.poll_round()
            self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "degraded")
            self.assertEqual(fh.get_healthy_peers(), ["hostx:1"])
            self.assertEqual(fh.get_healthy_peers(exclude_degraded=True), [])
            reply["v"] = _status()
            fh.poll_round()
            self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "up")
            expected = ["peer_up", "peer_degraded", "peer_recovered"] if emit else ["peer_up"]
            self.assertEqual(bus.events, expected)
            fh.close()

    def test_down_misses_real_sockets(self):
        """peer_down after down_misses short intervals with the key set."""
        from federation_health import FederationHealth
        from daq_status_server import StatusServer
        port = _free_port()
        server = StatusServer(port=port, instance_id=1)
        server.start()
        self.assertTrue(_wait_listening(port))
        server.update_status({"sync_state": 6})
        bus = _Bus()
        fh = FederationHealth(0, ["127.0.0.1:{}".format(port)], poll_interval=0.05,
                              event_bus=bus, down_misses=2, peer_timeout=0.3)
        addr = "127.0.0.1:{}".format(port)
        fh._poll_peer(addr)
        self.assertTrue(fh.get_peer_table()[addr]["alive"])
        server.close()
        time.sleep(1.2)
        fh._poll_peer(addr)
        self.assertTrue(fh.get_peer_table()[addr]["alive"])     # one miss is not enough
        time.sleep(0.1)
        fh._poll_peer(addr)
        row = fh.get_peer_table()[addr]
        self.assertFalse(row["alive"])
        self.assertEqual(row["state"], "down")
        self.assertEqual(row["error_kind"], "refused")
        self.assertEqual(bus.events, ["peer_up", "peer_down"])
        fh.close()

    def test_flapping(self):
        from federation_health import FederationHealth
        clock = _SimClock()
        ok = {"v": True}

        def connect(addr, timeout=None):
            if ok["v"]:
                return _FakeSock(_status())
            raise ConnectionRefusedError()
        bus = _Bus()
        fh = FederationHealth(0, ["hostx:1"], poll_interval=1.0, event_bus=bus,
                              down_misses=1, flap_window_sec=100.0, flap_threshold=2,
                              emit_health_events=True, _clock=clock, _wall=clock,
                              _connect=connect)
        for i in range(6):
            ok["v"] = i % 2 == 0
            clock.t += 2.0
            fh.poll_round()
        row = fh.get_member_table()["hostx:1"]
        self.assertTrue(row["flapping"])
        self.assertIn("peer_flapping", bus.events)
        ok["v"] = True
        for _ in range(3):
            clock.t += 60.0
            fh.poll_round()
        row = fh.get_member_table()["hostx:1"]
        self.assertFalse(row["flapping"])
        self.assertEqual(row["state"], "up")
        fh.close()

    def test_flapping_peer_that_stays_down_reports_down(self):
        from federation_health import FederationHealth
        clock = _SimClock()
        ok = {"v": True}

        def connect(addr, timeout=None):
            if ok["v"]:
                return _FakeSock(_status())
            raise ConnectionRefusedError()
        fh = FederationHealth(0, ["hostx:1"], poll_interval=1.0, down_misses=1,
                              flap_window_sec=100.0, flap_threshold=2, _clock=clock,
                              _wall=clock, _connect=connect)
        for i in range(6):                       # ends on a failed poll: down + flapping
            ok["v"] = i % 2 == 0
            clock.t += 2.0
            fh.poll_round()
        row = fh.get_member_table()["hostx:1"]
        self.assertFalse(row["alive"])
        self.assertTrue(row["flapping"])
        self.assertEqual(row["state"], "flapping")
        clock.t += 50.0
        fh.poll_round()
        self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "flapping")
        clock.t += 60.0                          # no transition for flap_window_sec
        fh.poll_round()
        row = fh.get_member_table()["hostx:1"]
        self.assertFalse(row["flapping"])
        self.assertEqual(row["state"], "down")
        fh.close()

    def test_busy_body(self):
        from federation_health import FederationHealth
        clock = _SimClock()
        reply = {"v": _status()}
        fh = FederationHealth(0, ["hostx:1"], down_misses=2, _clock=clock, _wall=clock,
                              _connect=lambda a, timeout=None: _FakeSock(reply["v"]))
        fh.poll_round()
        reply["v"] = {"error": "busy"}
        fh.poll_round()
        row = fh.get_member_table()["hostx:1"]
        self.assertEqual((row["busy_streak"], row["error_kind"], row["state"]),
                         (1, "busy", "up"))
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertTrue(row["reachable"])
        fh.poll_round()
        self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "degraded")
        reply["v"] = _status()
        fh.poll_round()
        row = fh.get_member_table()["hostx:1"]
        self.assertEqual((row["busy_streak"], row["state"]), (0, "up"))
        fh.close()

    def test_stall_detection_auto(self):
        from federation_health import FederationHealth
        clock = _SimClock()
        reply = {"v": _status(frame_count=1)}
        fh = FederationHealth(0, ["hostx:1"], stall_after_sec="auto", _clock=clock,
                              _wall=clock,
                              _connect=lambda a, timeout=None: _FakeSock(reply["v"]))
        fh.poll_round()
        clock.t += 30
        reply["v"] = _status(frame_count=2)        # first change: not stall-checked yet
        fh.poll_round()
        clock.t += 500
        fh.poll_round()
        self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "up")
        reply["v"] = _status(frame_count=3)        # second change: interval 500 s learned
        fh.poll_round()
        row = fh._peer_table["hostx:1"]
        self.assertEqual(fh.stall_threshold(row), 3 * 500.0)
        clock.t += 1000
        fh.poll_round()
        self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "up")
        clock.t += 600
        fh.poll_round()
        row = fh.get_member_table()["hostx:1"]
        self.assertEqual(row["state"], "stale")
        self.assertTrue(row["stale"])
        reply["v"] = _status(frame_count=4)
        fh.poll_round()
        self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "up")
        fh.close()

    def test_stall_numeric_uses_reported_stale_after(self):
        from federation_health import FederationHealth
        clock = _SimClock()
        reply = {"v": _status(frame_count=1, stale_after_sec=40.0)}
        fh = FederationHealth(0, ["hostx:1"], stall_after_sec=10.0, _clock=clock, _wall=clock,
                              _connect=lambda a, timeout=None: _FakeSock(reply["v"]))
        fh.poll_round()
        clock.t += 100
        fh.poll_round()
        self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "up")
        clock.t += 30
        fh.poll_round()
        self.assertEqual(fh.get_member_table()["hostx:1"]["state"], "stale")
        fh.close()

    def test_members_keyed_by_node_key(self):
        from federation_health import FederationHealth
        from federation_common import parse_member
        members = [parse_member("hosta:1@site-a"), parse_member("hostb:2")]
        fh = FederationHealth(0, [], members=members)
        self.assertEqual(sorted(fh.get_member_table()), ["hosta:1", "hostb:2"])
        self.assertEqual(fh._peer_endpoints["hosta:1"], ("hosta", 5102))
        self.assertEqual(fh.get_member_table()["hosta:1"]["node_id"], "site-a")
        fh.set_members([members[1]])
        self.assertEqual(list(fh.get_member_table()), ["hostb:2"])
        fh.close()

    def test_legacy_rows_keep_todays_keys(self):
        from federation_health import FederationHealth
        fh = FederationHealth(0, ["localhost:5102"])
        row = fh.get_peer_table()["localhost:5102"]
        for key in ("alive", "last_seen", "sync_state", "frequency_hz", "health", "instance_id"):
            self.assertIn(key, row)
        self.assertFalse(any(k.startswith("_") for k in row))
        fh.close()


class TestPeerListParsing(unittest.TestCase):

    def test_legacy_status_port_form_same_everywhere(self):
        from federation_health import FederationHealth
        from federation_coordinator import _parse_peer_list, FederationCoordinator
        from federation_coordinator import CoordinatorSettings, ConfigError
        fh = FederationHealth(0, ["hostb:5102"])
        self.assertEqual(fh.get_peer_table()["hostb:5102"]["instance_id"], 1)
        self.assertEqual(fh._peer_endpoints["hostb:5102"], ("hostb", 5102))
        coord = FederationCoordinator(port=0, instances=_parse_peer_list("hostb:5102"))
        m = coord._members()[0]
        self.assertEqual((m.instance_id, m.ports["status"], m.ports["ctl"]), (1, 5102, 5101))
        self.assertEqual(m.node_key, "hostb:1")
        with self.assertRaises(ConfigError):
            FederationCoordinator(port=0, settings=CoordinatorSettings(
                cluster_members="hostb:5102"))
        fh.close()

    def test_coordinator_membership_rules(self):
        from federation_coordinator import (_parse_peer_list, FederationCoordinator,
                                            CoordinatorSettings)
        from federation_common import self_host
        peers = _parse_peer_list("hostb:1,hosta:0,hostb:1")
        c = FederationCoordinator(port=0, instances=peers)
        self.assertEqual([m.node_key for m in c._members()], ["hostb:1", "hosta:0", "hostb:1"])
        me = self_host()
        c2 = FederationCoordinator(port=0, instances=_parse_peer_list("hostb:1"),
                                   settings=CoordinatorSettings(coordinator_include_self=True,
                                                                instance_id=2))
        keys = [m.node_key for m in c2._members()]
        self.assertEqual(keys, ["hostb:1", "{}:2".format(me)])
        self.assertEqual(c2._members()[1].connect_host, "127.0.0.1")
        c3 = FederationCoordinator(port=0, instances=_parse_peer_list("127.0.0.1:2"),
                                   settings=CoordinatorSettings(coordinator_include_self=True,
                                                                instance_id=2))
        self.assertEqual([m.node_key for m in c3._members()], ["127.0.0.1:2"])
        c4 = FederationCoordinator(port=0, settings=CoordinatorSettings(
            cluster_members="hosta:0,hostb:0", coordinator_include_self=True))
        self.assertEqual([m.node_key for m in c4._members()], ["hosta:0", "hostb:0"])


class _FakeRegistry(object):
    """Line server recording REGISTER arrival times."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self.times = []
        self.bodies = []
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        from federation_common import read_request
        self.srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                req = read_request(conn)
                self.times.append(time.monotonic())
                self.bodies.append(req.body_json())
                conn.sendall(json.dumps({"ok": True, "term": 0, "role": "static",
                                         "peers": {"hostq:9": {"state": "up"}}}).encode()
                             + b"\n")
            except Exception:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self.srv.close()


class TestRegistrar(unittest.TestCase):

    def test_registrar_cadence_with_dead_peers(self):
        from federation_health import FederationHealth
        reg = _FakeRegistry()

        def dead(addr, timeout=None):
            time.sleep(2.0)
            raise socket.timeout("timed out")
        body = {"node_key": "hosta:1", "node_id": "hosta:1", "cluster_id": "default",
                "instance_id": 1, "nonce": "1-2-3"}
        fh = FederationHealth(1, ["hostx:{}".format(i) for i in range(2, 10)],
                              register={"endpoints": [("127.0.0.1", reg.port)], "interval": 1.0,
                                        "body_fn": lambda: body, "want_peers": True},
                              _connect=dead)
        try:
            fh.start()
            time.sleep(4.6)
        finally:
            fh.close()
            reg.close()
        self.assertGreaterEqual(len(reg.times), 4)
        gaps = [b - a for a, b in zip(reg.times, reg.times[1:])]
        for g in gaps:
            self.assertAlmostEqual(g, 1.0, delta=0.1)
        self.assertEqual(reg.bodies[0]["register_interval_sec"], 1.0)
        self.assertTrue(reg.bodies[0]["want_peers"])
        self.assertIn("hostq:9", fh.get_member_table())     # hub-mode peer table
        summary = fh.get_coordinator_status()
        self.assertTrue(summary["registered"])
        self.assertEqual(summary["role"], "static")

    def test_duplicate_logged_every_tenth(self):
        from federation_health import CoordinatorRegistrar
        replies = {"v": {"ok": False, "error": "duplicate_node_key", "holder": {"addr": "x"}}}
        reg = CoordinatorRegistrar([("h", 1)], 5.0, lambda: {"node_key": "a:1"},
                                   _request=lambda *a, **k: replies["v"])
        with self.assertLogs("federation_health.registrar", level="ERROR") as logs:
            for _ in range(11):
                reg.register_once()
        self.assertEqual(len(logs.output), 2)
        self.assertIn("advertise_host or node_id", logs.output[0])
        self.assertEqual(reg.summary()["last_error"], "duplicate_node_key")


def _register_req(body, token=None):
    from federation_common import Request
    data = json.dumps(body).encode()
    return Request("REGISTER", "", data, False, "REGISTER @{}".format(len(data)))


class TestRegister(unittest.TestCase):

    def _body(self, **kw):
        b = {"node_key": "hosta:1", "node_id": "hosta:1", "cluster_id": "default",
             "instance_id": 1, "advertise_host": "hosta", "port_stride": 100,
             "ports": {"iq": 5100, "ctl": 5101, "status": 5102, "events": 5103, "zmq": 1230},
             "nonce": "100-1-abc", "register_interval_sec": 5.0}
        b.update(kw)
        return b

    def test_duplicate_rules(self):
        from federation_coordinator import FederationCoordinator
        c = FederationCoordinator(port=0)
        r = c._verb_register(_register_req(self._body()), ("10.0.0.11", 4000))
        self.assertTrue(r["ok"])
        self.assertEqual(r["registered_as"], {"node_key": "hosta:1", "host": "10.0.0.11"})
        m = c._members()[0]
        self.assertEqual((m.host, m.advertised_host, m.source), ("10.0.0.11", "hosta",
                                                                 "registered"))
        # same nonce, new address (DHCP change): accepted, address updated
        r = c._verb_register(_register_req(self._body()), ("10.0.0.12", 4000))
        self.assertTrue(r["ok"])
        self.assertEqual(c._members()[0].host, "10.0.0.12")
        # different nonce, same address: restart
        r = c._verb_register(_register_req(self._body(nonce="200-2-abc")), ("10.0.0.12", 1))
        self.assertTrue(r["ok"])
        self.assertEqual(c._members()[0].nonce, "200-2-abc")
        # different nonce, different address, live holder: rejected and listed
        r = c._verb_register(_register_req(self._body(nonce="300-3-zzz", node_id="kraken:0")),
                             ("10.0.0.99", 1))
        self.assertEqual(r["error"], "duplicate_node_key")
        self.assertEqual(r["holder"]["addr"], "10.0.0.12")
        self.assertEqual(len(c._members()), 1)
        rows = c._verb_members(None, None)
        self.assertEqual(rows["key_collisions"][0]["rejected_addr"], "10.0.0.99")
        self.assertIn("node_key_collision", [e["event_type"] for e in c._local_events])
        # expired holder: replaced
        c._members()[0].last_rx_mono -= 16.0
        self.assertEqual(c._verb_members(None, None)["members"][0]["state"], "expired")
        r = c._verb_register(_register_req(self._body(nonce="300-3-zzz")), ("10.0.0.99", 1))
        self.assertTrue(r["ok"])
        self.assertEqual(c._members()[0].host, "10.0.0.99")
        # an IP-literal advertise_host is used as the connection host
        r = c._verb_register(_register_req(self._body(node_key="10.0.0.50:2", instance_id=2,
                                                      advertise_host="10.0.0.50")),
                             ("192.168.1.9", 1))
        self.assertEqual(r["registered_as"]["host"], "10.0.0.50")

    def test_cluster_mismatch_bad_request_static_entry(self):
        from federation_coordinator import FederationCoordinator, CoordinatorSettings
        c = FederationCoordinator(port=0, instances=[{"host": "127.0.0.1", "instance_id": 1,
                                                       "node_key": "hosta:1"}],
                                  settings=CoordinatorSettings(cluster_id="lab-a"))
        r = c._verb_register(_register_req(self._body()), ("127.0.0.1", 1))
        self.assertEqual(r, {"ok": False, "error": "cluster_mismatch", "expected": "lab-a"})
        r = c._verb_register(_register_req(self._body(cluster_id="lab-a", instance_id=3)),
                             ("127.0.0.1", 1))
        self.assertEqual(r["error"], "bad_request")
        # static entry: the static host's address is accepted, another address is not
        r = c._verb_register(_register_req(self._body(cluster_id="lab-a")), ("127.0.0.1", 1))
        self.assertTrue(r["ok"])
        self.assertEqual(r["registered_as"]["host"], "127.0.0.1")
        r = c._verb_register(_register_req(self._body(cluster_id="lab-a", nonce="x")),
                             ("10.9.9.9", 1))
        self.assertEqual(r["error"], "duplicate_node_key")

    def test_register_over_the_wire_with_auth_and_peers(self):
        from federation_coordinator import FederationCoordinator, CoordinatorSettings
        from federation_common import request_json
        c = FederationCoordinator(port=0, listen_address="127.0.0.1",
                                  settings=CoordinatorSettings(auth_token="tok"))
        c.start()
        try:
            r = request_json("127.0.0.1", c.port, "REGISTER", body=self._body())
            self.assertEqual(r["error"], "auth_required")
            r = request_json("127.0.0.1", c.port, "REGISTER", body=self._body(want_peers=True),
                             auth_token="tok")
            self.assertTrue(r["ok"])
            self.assertEqual(r["registered_as"]["host"], "127.0.0.1")
            self.assertEqual(r["peers"], {})
            self.assertEqual(r["role"], "static")
        finally:
            c.close()


class TestSchedulerFixes(unittest.TestCase):
    """D.5 scheduler rules (canonical format, partition, gen, transfer, temp files)."""

    def test_range_remainder_spread(self):
        from federation_scheduler import FederationScheduler
        fs = FederationScheduler()
        fs.set_master_schedule(list(range(100, 800, 100)), [None] * 7, [1] * 7, "range")
        a = fs.partition_schedule(instance_ids=[0, 1, 2])
        self.assertEqual([len(a[k]["entries"]) for k in (0, 1, 2)], [3, 2, 2])
        self.assertEqual(a[0]["frequencies"], [100, 200, 300])

    def test_early_returns_clear_assignments(self):
        from federation_scheduler import FederationScheduler
        fs = FederationScheduler()
        self.assertEqual(fs.rebalance(), {"error": "no master schedule"})
        fs.set_master_schedule([100], [None], [1])
        fs.partition_schedule(instance_ids=[0])
        self.assertTrue(fs.get_assignments())
        self.assertEqual(fs.partition_schedule(instance_ids=[]), {})
        self.assertEqual(fs.get_assignments(), {})

        class Health(object):
            instance_id = 0

            def get_healthy_peers(self):
                return []

            def get_peer_table(self):
                return {}
        fs2 = FederationScheduler(health_monitor=Health())
        fs2.set_master_schedule([100], [None], [1])
        self.assertEqual(list(fs2.partition_schedule()), [0])

    def test_canonical_doc_round_trip(self):
        from federation_scheduler import FederationScheduler
        from signal_scheduler import ScheduleParser
        fs = FederationScheduler()
        doc = {"name": "site", "repeat_mode": "pingpong", "strategy": "range",
               "entries": [{"frequency": 433000000, "gains": [297] * 5, "dwell_frames": 100},
                           {"frequency": 868000000, "dwell_time_sec": 2.0,
                            "require_cal": False}]}
        out = fs.set_master_schedule_doc(doc)
        self.assertEqual(out, doc)
        self.assertEqual(fs._master_schedule["frequencies"], [433000000, 868000000])
        a = fs.partition_schedule(targets=["hosta:0", "hostb:0"])
        text, gen = fs.target_document(a["hostb:0"], "hostb:0", 0, True, 77)
        self.assertEqual(json.loads(text), {"name": "site@hostb:0", "repeat_mode": "pingpong",
                                            "gen": 77, "entries": [doc["entries"][1]]})
        parsed = ScheduleParser.from_json(text)
        self.assertEqual(parsed.repeat_mode, "pingpong")
        self.assertFalse(parsed.entries[0].require_cal)
        text, gen = fs.target_document(a["hosta:0"], "hosta:0", 3, False, 77)
        self.assertIsNone(gen)
        self.assertEqual(json.loads(text)["name"], "federation_inst3")
        self.assertNotIn("gen", json.loads(text))
        with self.assertRaises(ValueError):
            fs.set_master_schedule_doc({"entries": [{"frequency": 0}]})
        with self.assertRaises(ValueError):
            fs.set_master_schedule_doc({"entries": [{"frequency": 1, "gains": 5}]})

    def test_legacy_document_bytes_unchanged(self):
        """Per-target documents for legacy targets are byte-identical to today's."""
        from federation_scheduler import FederationScheduler
        fs = FederationScheduler()
        fs.set_master_schedule([433000000, 868000000], [[40] * 5, None], [100, 200])
        a = fs.partition_schedule(instance_ids=[0])
        text = fs._build_schedule_json(a[0], name="federation_inst0")
        today = json.dumps({"name": "federation_inst0", "entries": [
            {"frequency": 433000000, "dwell_frames": 100, "gains": [40] * 5},
            {"frequency": 868000000, "dwell_frames": 200}]}, separators=(",", ":"))
        self.assertEqual(json.loads(text), json.loads(today))
        self.assertEqual(len(text), len(today))

    def test_gen_formula(self):
        import zlib
        from federation_scheduler import compute_gen
        self.assertEqual(compute_gen(7, 4, 2, "hosta:1"),
                         zlib.crc32(b"7.4.2.hosta:1") & 0xFFFFFFFF)

    def test_temp_file_cleanup(self):
        import shutil
        import tempfile
        from federation_scheduler import FederationScheduler
        private = tempfile.mkdtemp(prefix="schedtmp")
        saved, tempfile.tempdir = tempfile.tempdir, private   # isolate from parallel runs
        try:
            fs = FederationScheduler()
            old = fs.write_temp_schedule("{}", 0)
            new = fs.write_temp_schedule("{}", 1)
            os.utime(old, (time.time() - 7200, time.time() - 7200))
            self.assertEqual(fs.cleanup_temp_files(), [old])
            self.assertTrue(os.path.exists(new))
            self.assertTrue(new.startswith(private))
        finally:
            tempfile.tempdir = saved
            shutil.rmtree(private, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
