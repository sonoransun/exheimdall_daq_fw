"""
    Description :
    Real producer/consumer loopback tests for the Python shared-memory
    interface (shmemIface.py) over actual named FIFOs and POSIX shared
    memory in an isolated temporary working directory.

    These replace the old tautological "instance naming" checks (which
    re-implemented the naming rule inline and asserted on local variables):
    every assertion here goes through the real outShmemIface/inShmemIface
    code paths — INIT_READY handshake, A/B double-buffer alternation,
    payload integrity, TERMINATE propagation, drop-mode sentinel, EOF
    handling, and the federation instN_ prefixing of both the shared-memory
    segment names and the FIFO paths.

    No sudo and no C binaries required; runs on macOS and Linux.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from os.path import join, dirname, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

import numpy as np  # noqa: E402
from shmemIface import (outShmemIface, inShmemIface,  # noqa: E402
                        A_BUFF_READY, B_BUFF_READY, TERMINATE, BUFFER_DROPPED)

JOIN_TIMEOUT = 10.0


class _Holder:
    """Result slot for interfaces constructed on a helper thread (both FIFO
    open() calls block until the peer opens its end)."""

    def __init__(self):
        self.iface = None
        self.error = None


def _build_producer(holder, name, size, drop_mode=False, instance_id=0):
    try:
        holder.iface = outShmemIface(name, size, drop_mode=drop_mode,
                                     instance_id=instance_id)
    except Exception as e:  # pragma: no cover - surfaced via holder.error
        holder.error = e


class TesterShmemLoopback(unittest.TestCase):

    def setUp(self):
        # shmemIface uses the cwd-relative '_data_control/' FIFO prefix, so
        # each test runs inside its own temp working directory.
        self.tmpdir = tempfile.mkdtemp(prefix="shmem_loop_")
        os.makedirs(join(self.tmpdir, "_data_control"))
        self.prev_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        # Shared-memory names are a host-global namespace: make them unique
        # so parallel/concurrent test runs cannot collide.
        self.name = "utst_{:s}".format(uuid.uuid4().hex[:12])
        self.producer = None
        self.consumer = None

    def tearDown(self):
        if self.consumer is not None:
            self.consumer.destory_sm_buffer()
        if self.producer is not None:
            self.producer.destory_sm_buffer()
        os.chdir(self.prev_cwd)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mkfifos(self, prefix=""):
        os.mkfifo(join("_data_control", prefix + "fw_" + self.name))
        os.mkfifo(join("_data_control", prefix + "bw_" + self.name))

    def _connect(self, size=4096, drop_mode=False, instance_id=0):
        """Create a connected producer/consumer pair over real FIFOs."""
        prefix = "inst{:d}_".format(instance_id) if instance_id else ""
        self._mkfifos(prefix)
        holder = _Holder()
        t = threading.Thread(target=_build_producer,
                             args=(holder, self.name, size, drop_mode,
                                   instance_id))
        t.start()
        self.consumer = inShmemIface(self.name, instance_id=instance_id)
        t.join(timeout=JOIN_TIMEOUT)
        self.assertFalse(t.is_alive(), "producer construction deadlocked")
        self.assertIsNone(holder.error,
                          "producer init raised: {!r}".format(holder.error))
        self.producer = holder.iface
        return self.producer, self.consumer

    # ------------------------------------------------------------------

    def test_init_handshake(self):
        """INIT_READY is sent by the producer and consumed by the consumer;
        both ends report init_ok and see two attached buffers."""
        prod, cons = self._connect()
        self.assertTrue(prod.init_ok)
        self.assertTrue(cons.init_ok)
        self.assertEqual(len(prod.buffers), 2)
        self.assertEqual(len(cons.buffers), 2)
        # The kernel may round the segment up to a page multiple (macOS);
        # the consumer self-sizes from the real segment size.
        self.assertGreaterEqual(len(cons.buffers[0]), 4096)
        self.assertGreaterEqual(len(cons.buffers[1]), 4096)

    def test_ab_alternation_and_payload_integrity(self):
        """Frames alternate between the _A and _B segments and arrive
        byte-exact through the real shared memory."""
        size = 4096
        prod, cons = self._connect(size=size)
        rng = np.random.default_rng(seed=0xC0FFEE)
        received_indices = []
        for frame_no in range(6):
            active = prod.wait_buff_free()
            self.assertIn(active, (0, 1),
                          "producer had no free buffer at frame "
                          "{:d}".format(frame_no))
            payload = rng.integers(0, 256, size, dtype=np.uint8)
            payload[0] = frame_no  # frame tag
            prod.buffers[active][:] = payload
            prod.send_ctr_buff_ready(active)

            got = cons.wait_buff_free()
            self.assertIn(got, (0, 1))
            received_indices.append(got)
            self.assertEqual(got, active,
                             "consumer read a different buffer than the "
                             "producer filled")
            np.testing.assert_array_equal(cons.buffers[got][:size], payload)
            cons.send_ctr_buff_ready(got)
        # Strict A/B alternation in this lock-step pattern
        self.assertEqual(received_indices, [0, 1, 0, 1, 0, 1])

    def test_terminate_propagates(self):
        """TERMINATE (255) sent by the producer is returned verbatim by the
        consumer's wait_buff_free."""
        prod, cons = self._connect()
        prod.send_ctr_terminate()
        self.assertEqual(cons.wait_buff_free(), TERMINATE)

    def test_drop_mode_returns_sentinel(self):
        """With both buffers in flight and no consumer ack, drop mode returns
        BUFFER_DROPPED (3) instead of blocking, and counts the drop."""
        prod, cons = self._connect(drop_mode=True)
        self.assertEqual(prod.wait_buff_free(), 0)
        prod.send_ctr_buff_ready(0)
        self.assertEqual(prod.wait_buff_free(), 1)
        prod.send_ctr_buff_ready(1)
        # No consumer ack: the O_NONBLOCK backward FIFO raises
        # BlockingIOError inside wait_buff_free -> sentinel
        self.assertEqual(prod.wait_buff_free(), BUFFER_DROPPED)
        self.assertEqual(prod.dropped_frame_cntr, 1)
        # After the consumer frees buffer A the producer recovers
        self.assertEqual(cons.wait_buff_free(), 0)
        cons.send_ctr_buff_ready(0)
        self.assertEqual(prod.wait_buff_free(), 0)
        # Drain the pending B frame so teardown is clean
        self.assertEqual(cons.wait_buff_free(), 1)
        cons.send_ctr_buff_ready(1)

    def test_producer_death_yields_eof_not_crash(self):
        """If the producer disappears without TERMINATE the consumer gets -1
        (EOF), not an unhandled struct.error."""
        prod, cons = self._connect()
        prod.destory_sm_buffer()
        self.producer = None  # already destroyed
        self.assertEqual(cons.wait_buff_free(), -1)

    def test_instance_prefixing_real_paths(self):
        """instance_id=N prefixes BOTH the shared-memory segment names and
        the FIFO paths with instN_ — asserted against the real interface
        objects, not a re-implementation."""
        instance_id = 3
        prod, cons = self._connect(instance_id=instance_id)
        expected_shm = "inst3_" + self.name
        self.assertEqual(prod.shmem_name, expected_shm)
        self.assertEqual(cons.shmem_name, expected_shm)
        self.assertEqual(prod.memories[0].name.lstrip("/"),
                         expected_shm + "_A")
        self.assertEqual(prod.memories[1].name.lstrip("/"),
                         expected_shm + "_B")
        # The prefixed FIFOs are the ones actually opened (a frame passes)
        active = prod.wait_buff_free()
        prod.buffers[active][:4] = np.frombuffer(b"ping", dtype=np.uint8)
        prod.send_ctr_buff_ready(active)
        got = cons.wait_buff_free()
        self.assertEqual(got, active)
        self.assertEqual(cons.buffers[got][:4].tobytes(), b"ping")
        cons.send_ctr_buff_ready(got)

    def test_instance0_paths_unprefixed(self):
        """Instance 0 keeps the historic unprefixed names (backward
        compatibility with pre-federation deployments)."""
        prod, cons = self._connect(instance_id=0)
        self.assertEqual(prod.shmem_name, self.name)
        self.assertTrue(os.path.exists(join("_data_control",
                                            "fw_" + self.name)))
        self.assertTrue(os.path.exists(join("_data_control",
                                            "bw_" + self.name)))

    def test_signal_byte_values_on_the_wire(self):
        """The raw FIFO protocol byte values are wire ABI (lockstep with
        sh_mem_util.h) and must never change."""
        self.assertEqual(A_BUFF_READY, 1)
        self.assertEqual(B_BUFF_READY, 2)
        self.assertEqual(TERMINATE, 255)
        self.assertEqual(BUFFER_DROPPED, 3)
        from shmemIface import INIT_READY
        self.assertEqual(INIT_READY, 10)


if __name__ == "__main__":
    unittest.main()
