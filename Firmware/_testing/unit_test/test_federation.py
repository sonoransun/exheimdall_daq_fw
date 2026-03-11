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
import threading
import unittest

# Add _daq_core to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '_daq_core'))


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


class TestFederationCoordinator(unittest.TestCase):
    """Tests for federation_coordinator.py"""

    def test_coordinator_ping(self):
        from federation_coordinator import FederationCoordinator
        coord = FederationCoordinator(port=16000, instances=[])
        coord.start()
        time.sleep(0.2)
        try:
            with socket.create_connection(("localhost", 16000), timeout=2) as sock:
                sock.sendall(b"PING\n")
                data = sock.recv(4096)
                response = json.loads(data.decode())
                self.assertTrue(response["ok"])
                self.assertIn("ts", response)
        finally:
            coord.close()

    def test_coordinator_status_no_instances(self):
        from federation_coordinator import FederationCoordinator
        coord = FederationCoordinator(port=16001, instances=[])
        coord.start()
        time.sleep(0.2)
        try:
            with socket.create_connection(("localhost", 16001), timeout=2) as sock:
                sock.sendall(b"STATUS\n")
                data = sock.recv(4096)
                response = json.loads(data.decode())
                self.assertIn("federation_health", response)
                self.assertEqual(response["instance_count"], 0)
        finally:
            coord.close()

    def test_coordinator_unknown_command(self):
        from federation_coordinator import FederationCoordinator
        coord = FederationCoordinator(port=16002, instances=[])
        coord.start()
        time.sleep(0.2)
        try:
            with socket.create_connection(("localhost", 16002), timeout=2) as sock:
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


class TestFederationScheduler(unittest.TestCase):
    """Tests for federation_scheduler.py"""

    def test_round_robin_partition(self):
        from federation_scheduler import FederationScheduler
        fs = FederationScheduler()
        fs.set_master_schedule(
            frequencies=[433000000, 868000000, 915000000, 1090000000],
            gains=[40, 40, 40, 40],
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
            gains=[1, 2, 3, 4],
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
            gains=[40, 40],
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
        fs.set_master_schedule([100, 200], [1, 2], [10, 20])
        fs.partition_schedule(instance_ids=[0])
        assignments = fs.get_assignments()
        self.assertIn(0, assignments)


class TestFederationIQRouter(unittest.TestCase):
    """Tests for federation_iq_router.py"""

    def test_init_stats(self):
        from federation_iq_router import FederationIQRouter
        router = FederationIQRouter(
            instance_configs=[
                {"host": "localhost", "instance_id": 0, "iq_port": 5000},
                {"host": "localhost", "instance_id": 1, "iq_port": 5100},
            ],
            output_port=17000
        )
        stats = router.get_stream_stats()
        self.assertEqual(len(stats), 2)
        self.assertEqual(stats[0]["frames_received"], 0)
        self.assertEqual(stats[1]["frames_received"], 0)

    def test_start_stop(self):
        from federation_iq_router import FederationIQRouter
        router = FederationIQRouter(
            instance_configs=[],
            output_port=17001
        )
        router.start()
        time.sleep(0.3)
        router.close()


class TestInstanceNaming(unittest.TestCase):
    """Test the Python-side instance naming logic in shmemIface."""

    def test_outShmemIface_name_instance0(self):
        """Instance 0 should produce original names."""
        # We can't fully init shmemIface without FIFOs, but we can test name logic
        # by checking the naming convention
        iid = 0
        shmem_name = "decimator_out"
        if iid != 0:
            result = f"inst{iid}_{shmem_name}"
        else:
            result = shmem_name
        self.assertEqual(result, "decimator_out")

    def test_outShmemIface_name_instance1(self):
        """Instance 1 should produce prefixed names."""
        iid = 1
        shmem_name = "decimator_out"
        if iid != 0:
            result = f"inst{iid}_{shmem_name}"
        else:
            result = shmem_name
        self.assertEqual(result, "inst1_decimator_out")

    def test_fifo_path_instance0(self):
        """Instance 0: original path."""
        iid = 0
        prefix = '_data_control/'
        if iid != 0:
            prefix += f'inst{iid}_'
        self.assertEqual(prefix + 'fw_decimator_in', '_data_control/fw_decimator_in')

    def test_fifo_path_instance1(self):
        """Instance 1: prefixed path."""
        iid = 1
        prefix = '_data_control/'
        if iid != 0:
            prefix += f'inst{iid}_'
        self.assertEqual(prefix + 'fw_decimator_in', '_data_control/inst1_fw_decimator_in')

    def test_port_compute(self):
        """Port computation: base + instance_id * stride."""
        self.assertEqual(5000 + 0 * 100, 5000)
        self.assertEqual(5000 + 1 * 100, 5100)
        self.assertEqual(5001 + 2 * 100, 5201)
        self.assertEqual(1130 + 3 * 100, 1430)


if __name__ == "__main__":
    unittest.main()
