"""
	Description : Unit test for the IQ server module (iq_server.out)

	Pipeline lane (Linux, built C binaries; run by `unit_test.sh --sudo`).
	Each test runs iq_server.out in a private temp cwd with a COPY of the
	config: a Python producer plays delay_sync (it creates the delay_sync_iq
	segments and FIFOs) and Python clients speak the port-5000 protocol
	("streaming" handshake, one "IQDownload" ack per frame).

	The server port is a free ephemeral port P: instance_id = P - 5000 with
	port_stride = 1, so the listening port is compute_port(5000, id, 1) = P
	and every FIFO / shm name carries a unique inst<id>_ prefix.

	Covered (spec I.4 #6/#7, ledger L4):
	  - default single-client loop: frames byte-identical to the producer's,
	    delay_sync_iq mapped with the 2^23-sample worst case;
	  - [pre_processing] size_segments_from_config = 1 maps exactly the
	    delay_sync size 1024 + max(cpi_size, corr_size) * num_ch * 8;
	  - [data_interface] iq_server_max_clients > 1: one client byte-identical,
	    two clients both receive every frame, a stalled client is dropped at
	    the frame deadline while the other continues, a client beyond the cap
	    waits for a free slot, SIGTERM stops the server in every wait state
	    (also while waiting for acks), an input FIFO error exits nonzero.

	Project : HeIMDALL DAQ Firmware
	License : GNU GPL V3
	Author  : Tamas Peto
	Copyright (C) 2018-2020  Tamás Pető

	This program is free software: you can redistribute it and/or modify
	it under the terms of the GNU General Public License as published by
	the Free Software Foundation, either version 3 of the License, or
	any later version.

	This program is distributed in the hope that it will be useful,
	but WITHOUT ANY WARRANTY; without even the implied warranty of
	MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
	GNU General Public License for more details.

	You should have received a copy of the GNU General Public License
	along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import logging
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from configparser import ConfigParser
from os.path import join, dirname, realpath

import numpy as np

current_path  = dirname(realpath(__file__))
root_path     = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
live_config_filename = join(root_path, "daq_chain_config.ini")
IQ_SERVER_BIN = join(daq_core_path, "iq_server.out")

sys.path.insert(0, daq_core_path)
sys.path.insert(0, current_path)
from iq_header import IQHeader
from shmemIface import outShmemIface
from gen_utils import wait_consumer_done

# The producer outlives iq_server in the SIGTERM cases; its broken-pipe
# warnings are expected there
logging.getLogger("shmemIface").setLevel(logging.ERROR)

MAX_IQFRAME_PAYLOAD_SIZE = 2**23  # rtl_daq.h
SHM_DIR = "/dev/shm"
TIMEOUT = 30.0


def _free_port():
    """A free TCP port above 5000 (needed for instance_id = port - 5000)."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port > 5000:
            return port
    raise RuntimeError("no free port above 5000")


def _page_round(nbytes):
    page = os.sysconf("SC_PAGE_SIZE")
    return (nbytes + page - 1) // page * page


def _mapping_len(pid, shm_name):
    with open("/proc/{:d}/maps".format(pid)) as maps:
        for line in maps:
            fields = line.split()
            if len(fields) >= 6 and fields[5] == join(SHM_DIR, shm_name):
                start, end = (int(x, 16) for x in fields[0].split("-"))
                return end - start
    return None


def _make_frames(count, N, M, seed=3):
    """Reference frames: encoded header + cf32 payload, as delay_sync writes."""
    rng = np.random.default_rng(seed)
    frames = []
    header = IQHeader()
    header.sync_word = IQHeader.SYNC_WORD
    header.frame_type = IQHeader.FRAME_TYPE_DATA
    header.active_ant_chs = M
    header.cpi_length = N
    header.sample_bit_depth = 32
    header.data_type = 3
    header.sampling_freq = 2400000
    header.delay_sync_flag = 1
    header.iq_sync_flag = 1
    for i in range(count):
        header.cpi_index = i
        header.daq_block_index = i
        header.time_stamp = 1700000000000 + i
        payload = rng.standard_normal(2 * N * M).astype(np.float32)
        frames.append(header.encode_header() + payload.tobytes())
    return frames


class _Producer(threading.Thread):
    """delay_sync's role: creates delay_sync_iq and writes the frames after
    `go` is set, then sends TERMINATE. bad_signal: sends this invalid
    forward-FIFO byte instead of the frames (an input transport error)."""

    def __init__(self, instance_id, size, frames, bad_signal=None):
        threading.Thread.__init__(self, daemon=True)
        self.instance_id, self.size, self.frames = instance_id, size, frames
        self.bad_signal = bad_signal
        self.go = threading.Event()
        self.ready = threading.Event()
        self.sent = 0

    def run(self):
        iface = outShmemIface("delay_sync_iq", self.size, drop_mode=False,
                              instance_id=self.instance_id)
        self.ready.set()
        if not iface.init_ok:
            iface.destory_sm_buffer()
            return
        self.go.wait(TIMEOUT)
        if self.bad_signal is not None:
            os.write(iface.fw_ctr_fifo, struct.pack("B", self.bad_signal))
            iface.destory_sm_buffer()
            return
        for frame in self.frames:
            idx = iface.wait_buff_free()
            if idx not in (0, 1):
                break
            iface.buffers[idx][0:len(frame)] = np.frombuffer(frame, dtype=np.uint8)
            iface.send_ctr_buff_ready(idx)
            self.sent += 1
        iface.send_ctr_terminate()
        wait_consumer_done(iface)
        iface.destory_sm_buffer()


def _recv_exact(sock, n):
    chunks = bytearray()
    while len(chunks) < n:
        data = sock.recv(min(n - len(chunks), 1 << 20))
        if not data:
            raise ConnectionError("server closed the connection")
        chunks += data
    return bytes(chunks)


class _Client(threading.Thread):
    """Port-5000 client. stall_after=k: stops acking after k frames (keeps the
    socket open); quit_after=k: closes the socket after k frames;
    ack_delay: seconds to wait before each ack (a slow but live client)."""

    def __init__(self, port, stall_after=None, quit_after=None, ack_delay=0.0):
        threading.Thread.__init__(self, daemon=True)
        self.port, self.stall_after, self.quit_after = port, stall_after, quit_after
        self.ack_delay = ack_delay
        self.frames = []
        self.closed_by_server = False
        self.connected = threading.Event()
        self.release = threading.Event()  # ends a stall
        self.error = None

    def run(self):
        deadline = time.time() + TIMEOUT
        sock = None
        while time.time() < deadline:
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=TIMEOUT)
                break
            except OSError:
                time.sleep(0.05)
        if sock is None:
            self.error = "could not connect"
            return
        try:
            sock.sendall(b"streaming")
            self.connected.set()
            header = IQHeader()
            while True:
                if self.quit_after is not None and len(self.frames) >= self.quit_after:
                    return
                try:
                    hdr = _recv_exact(sock, 1024)
                except ConnectionError:
                    self.closed_by_server = True
                    return
                header.decode_header(hdr)
                n = header.cpi_length * header.active_ant_chs * 2 * (header.sample_bit_depth // 8)
                self.frames.append(hdr + _recv_exact(sock, n))
                if self.stall_after is not None and len(self.frames) >= self.stall_after:
                    self.release.wait(TIMEOUT)
                    try:
                        self.closed_by_server = sock.recv(1) == b""
                    except OSError:
                        self.closed_by_server = True
                    return
                if self.ack_delay:
                    time.sleep(self.ack_delay)
                sock.sendall(b"IQDownload")
        except OSError as err:
            self.closed_by_server = True
            self.error = str(err)
        finally:
            sock.close()


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists(IQ_SERVER_BIN),
                     "needs Linux and a built iq_server.out")
class TesterIQServer(unittest.TestCase):

    M = 4
    N = 2**12
    N_CAL = 2**10
    FRAMES = 8

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="iq_server_test_")
        self.prev_cwd = os.getcwd()
        shutil.copyfile(live_config_filename, join(self.workdir, "daq_chain_config.ini"))
        os.makedirs(join(self.workdir, "_data_control"))
        os.chdir(self.workdir)
        self.port = _free_port()
        self.instance_id = self.port - 5000
        prefix = "inst{:d}_".format(self.instance_id)
        os.mkfifo(join("_data_control", prefix + "fw_delay_sync_iq"))
        os.mkfifo(join("_data_control", prefix + "bw_delay_sync_iq"))
        self.shm_name = prefix + "delay_sync_iq_A"
        self.log_name = join(self.workdir, "iq_server.log")
        self.proc = None
        self.producer = None

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        os.chdir(self.prev_cwd)
        shutil.rmtree(self.workdir, ignore_errors=True)

    # ---------- helpers ----------
    def _configure(self, **keys):
        parser = ConfigParser()
        parser.read("daq_chain_config.ini")
        parser["hw"]["num_ch"] = str(self.M)
        parser["pre_processing"]["cpi_size"] = str(self.N)
        parser["pre_processing"]["decimation_ratio"] = "1"
        parser["calibration"]["corr_size"] = str(self.N_CAL)
        parser["daq"]["log_level"] = "2"
        parser["daq"]["listen_address"] = "127.0.0.1"
        if not parser.has_section("federation"):
            parser.add_section("federation")
        parser["federation"]["instance_id"] = str(self.instance_id)
        parser["federation"]["port_stride"] = "1"
        for name, value in keys.items():
            section, key = name.split("__")
            if not parser.has_section(section):
                parser.add_section(section)
            parser[section][key] = str(value)
        with open("daq_chain_config.ini", "w") as f:
            parser.write(f)

    def _start(self, frames, bad_signal=None):
        self.frames = frames
        size = 1024 + max(self.N, self.N_CAL) * self.M * 8  # delay_sync's out_shmem_size
        self.producer = _Producer(self.instance_id, size, frames, bad_signal)
        self.log = open(self.log_name, "w")
        self.addCleanup(self.log.close)
        self.proc = subprocess.Popen([IQ_SERVER_BIN], stdout=subprocess.DEVNULL, stderr=self.log)
        self.producer.start()
        self.assertTrue(self.producer.ready.wait(TIMEOUT))
        self._wait_log("IQ Data TCP Server waiting for client")

    def _read_log(self):
        with open(self.log_name) as f:
            return f.read()

    def _wait_log(self, text, timeout=TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if text in self._read_log():
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)
        self.fail("iq_server log never showed {!r}:\n{}".format(text, self._read_log()))

    def _finish(self, *clients):
        self.producer.join(TIMEOUT)
        for c in clients:
            c.release.set()
            c.join(TIMEOUT)
            self.assertFalse(c.is_alive())
        self.assertEqual(self.proc.wait(TIMEOUT), 0, self._read_log())

    def _single_client_run(self, **keys):
        self._configure(**keys)
        self._start(_make_frames(self.FRAMES, self.N, self.M))
        client = _Client(self.port)
        client.start()
        self.assertTrue(client.connected.wait(TIMEOUT))
        mapping = _mapping_len(self.proc.pid, self.shm_name)
        self.producer.go.set()
        self._finish(client)
        self.assertIsNone(client.error)
        self.assertEqual(client.frames, self.frames)
        return mapping

    # ---------- default single-client loop ----------
    def test_default_single_client_byte_identical(self):
        mapping = self._single_client_run()
        self.assertEqual(mapping, _page_round(1024 + MAX_IQFRAME_PAYLOAD_SIZE * self.M * 8))
        log = self._read_log()
        self.assertNotIn("Multi-client", log)
        self.assertNotIn("sized from config", log)

    def test_config_sized_mapping(self):
        mapping = self._single_client_run(pre_processing__size_segments_from_config=1)
        self.assertEqual(mapping, _page_round(1024 + max(self.N, self.N_CAL) * self.M * 8))
        self.assertIn("delay_sync_iq mapping sized from config: {:d} bytes"
                      .format(1024 + max(self.N, self.N_CAL) * self.M * 8), self._read_log())

    def test_config_sized_mapping_corr_size_dominates(self):
        self.N, self.N_CAL = 2**10, 2**12
        mapping = self._single_client_run(pre_processing__size_segments_from_config=1)
        self.assertEqual(mapping, _page_round(1024 + self.N_CAL * self.M * 8))

    def test_max_clients_1_is_the_legacy_loop(self):
        self._single_client_run(data_interface__iq_server_max_clients=1)
        self.assertNotIn("Multi-client", self._read_log())

    # ---------- multi-client server ----------
    def test_multi_single_client_byte_identical(self):
        self._single_client_run(data_interface__iq_server_max_clients=2)
        self.assertIn("Multi-client IQ server: up to 2 clients", self._read_log())

    def test_multi_two_clients_receive_every_frame(self):
        self._configure(data_interface__iq_server_max_clients=2)
        self._start(_make_frames(self.FRAMES, self.N, self.M))
        a, b = _Client(self.port), _Client(self.port)
        a.start()
        b.start()
        self._wait_log("(2/2 sessions)")
        self.producer.go.set()
        self._finish(a, b)
        self.assertEqual(a.frames, self.frames)
        self.assertEqual(b.frames, self.frames)

    def test_multi_stalled_client_dropped_at_deadline(self):
        self._configure(data_interface__iq_server_max_clients=2,
                        data_interface__iq_server_frame_deadline_ms=300)
        self._start(_make_frames(self.FRAMES, self.N, self.M))
        good, stalled = _Client(self.port), _Client(self.port, stall_after=2)
        good.start()
        stalled.start()
        self._wait_log("(2/2 sessions)")
        t0 = time.time()
        self.producer.go.set()
        self._wait_log("missed the 300 ms frame deadline")
        self.producer.join(TIMEOUT)
        elapsed = time.time() - t0
        self.assertEqual(good.frames, self.frames)
        self.assertEqual(stalled.frames, self.frames[:2])
        self._finish(good, stalled)
        self.assertTrue(stalled.closed_by_server)
        # One deadline, not one per frame
        self.assertLess(elapsed, 5.0)

    def test_multi_client_beyond_cap_waits_for_a_slot(self):
        self._configure(data_interface__iq_server_max_clients=2,
                        data_interface__iq_server_frame_deadline_ms=3000)
        self._start(_make_frames(self.FRAMES, self.N, self.M))
        # The stayer acks slowly, so the stream is still running when the
        # quitter's slot is freed
        quitter, stayer = _Client(self.port, quit_after=3), _Client(self.port, ack_delay=0.1)
        quitter.start()
        stayer.start()
        self._wait_log("(2/2 sessions)")
        late = _Client(self.port)
        late.start()
        self.assertTrue(late.connected.wait(TIMEOUT))
        self.producer.go.set()
        self._finish(quitter, stayer, late)
        self.assertEqual(stayer.frames, self.frames)
        self.assertEqual(quitter.frames, self.frames[:3])
        # The late client was admitted only after the quitter's slot was
        # freed, and from then on received a contiguous tail of the stream
        self.assertGreater(len(late.frames), 0)
        first = self.frames.index(late.frames[0])
        self.assertGreaterEqual(first, 3)
        self.assertEqual(late.frames, self.frames[first:])

    def test_multi_auto_deadline_has_a_floor(self):
        """Auto deadline = 1.0 x cpi_size*R/fs, floored at 100 ms: a tiny CPI
        does not drop every network client."""
        self._configure(data_interface__iq_server_max_clients=2, daq__sample_rate=1000000)
        self._start(_make_frames(1, self.N, self.M))
        self._wait_log("frame deadline 100 ms")
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(5), 0)
        self.producer.go.set()

    def test_multi_auto_deadline_from_frame_period(self):
        self._configure(data_interface__iq_server_max_clients=2, daq__sample_rate=10000,
                        pre_processing__decimation_ratio=2)
        self._start(_make_frames(1, self.N, self.M))
        # 1.0 x the frame period: 4096 * 2 / 10000 s = 819 ms
        self._wait_log("frame deadline 819 ms")
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(5), 0)
        self.producer.go.set()

    def test_multi_max_clients_clamped(self):
        self._configure(data_interface__iq_server_max_clients=1000)
        self._start(_make_frames(1, self.N, self.M))
        self._wait_log("Multi-client IQ server: up to 64 clients")
        self.assertIn("iq_server_max_clients = 1000 exceeds the limit, using 64", self._read_log())
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(5), 0)
        self.producer.go.set()

    def test_multi_sigterm_while_waiting_for_clients(self):
        self._configure(data_interface__iq_server_max_clients=4)
        self._start(_make_frames(1, self.N, self.M))
        self._wait_log("Multi-client IQ server")
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(5), 0)
        self.assertIn("Received shutdown signal", self._read_log())
        self.producer.go.set()

    def test_multi_sigterm_while_waiting_for_a_frame(self):
        self._configure(data_interface__iq_server_max_clients=4)
        self._start(_make_frames(1, self.N, self.M))
        client = _Client(self.port)
        client.start()
        self._wait_log("(1/4 sessions)")
        self.proc.send_signal(signal.SIGTERM)  # main thread blocked on the FIFO read
        self.assertEqual(self.proc.wait(5), 0)
        self.assertIn("Received shutdown signal", self._read_log())
        client.join(TIMEOUT)
        self.assertTrue(client.closed_by_server)
        self.producer.go.set()

    def test_multi_sigterm_while_waiting_for_acks(self):
        # A long explicit deadline must not delay shutdown: the dispatch wait
        # polls the exit flag (without it SO_RCVTIMEO bounds the exit at 5 s)
        self._configure(data_interface__iq_server_max_clients=2,
                        data_interface__iq_server_frame_deadline_ms=60000)
        self._start(_make_frames(self.FRAMES, self.N, self.M))
        stalled = _Client(self.port, stall_after=1)
        stalled.start()
        self._wait_log("(1/2 sessions)")
        self.producer.go.set()
        deadline = time.time() + TIMEOUT
        while len(stalled.frames) < 1 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(stalled.frames, self.frames[:1])
        time.sleep(0.3)  # the server is now waiting for the missing ack
        t0 = time.time()
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(TIMEOUT), 0, self._read_log())
        self.assertLess(time.time() - t0, 3.0)
        log = self._read_log()
        self.assertIn("closing session on shutdown", log)
        self.assertNotIn("missed the", log)
        self.assertIn("Received shutdown signal", log)
        stalled.release.set()
        stalled.join(TIMEOUT)
        self.assertTrue(stalled.closed_by_server)

    def test_multi_input_error_exits_nonzero(self):
        self._configure(data_interface__iq_server_max_clients=2)
        self._start([], bad_signal=7)
        client = _Client(self.port)
        client.start()
        self._wait_log("(1/2 sessions)")
        self.producer.go.set()
        self.assertEqual(self.proc.wait(TIMEOUT), 1, self._read_log())
        self.assertIn("Multi-client IQ server stopped on an input transport error",
                      self._read_log())
        client.join(TIMEOUT)
        self.assertTrue(client.closed_by_server)
        self.assertEqual(client.frames, [])

    def test_multi_rejects_bad_handshake(self):
        self._configure(data_interface__iq_server_max_clients=2)
        self._start(_make_frames(self.FRAMES, self.N, self.M))
        bad = socket.create_connection(("127.0.0.1", self.port), timeout=TIMEOUT)
        self.addCleanup(bad.close)
        bad.sendall(b"hello")
        self.assertEqual(bad.recv(1), b"")
        self._wait_log("Unexpected request from")
        client = _Client(self.port)
        client.start()
        self._wait_log("(1/2 sessions)")
        self.producer.go.set()
        self._finish(client)
        self.assertEqual(client.frames, self.frames)


if __name__ == "__main__":
    unittest.main()
