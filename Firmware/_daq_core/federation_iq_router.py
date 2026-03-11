"""
    Federation IQ Router — Multi-Instance IQ Stream Aggregation

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Connects to multiple DAQ instance IQ servers and provides a unified
    TCP output stream. Each IQ frame is tagged with the source instance
    via the existing unit_id field in the IQ header.
"""
import socket
import logging
import threading
import time
import struct

IQ_HEADER_LENGTH = 1024
SYNC_WORD = 0x2bf7b95a


class FederationIQRouter:
    """Aggregates IQ streams from multiple DAQ instances."""

    def __init__(self, instance_configs, output_port=7000):
        """
        Parameters
        ----------
        instance_configs : list of dict
            Each dict: {"host": str, "instance_id": int, "iq_port": int}
        output_port : int
            TCP port for the unified output stream.
        """
        self.logger = logging.getLogger(__name__)
        self.instance_configs = instance_configs
        self.output_port = output_port
        self._stop_event = threading.Event()
        self._output_clients = []
        self._output_lock = threading.Lock()
        self._stats = {}
        self._stats_lock = threading.Lock()
        self._threads = []

        for cfg in instance_configs:
            iid = cfg["instance_id"]
            self._stats[iid] = {
                "frames_received": 0,
                "bytes_received": 0,
                "last_frame_time": 0.0,
                "connected": False,
            }

    def start(self):
        """Start the router: one reader thread per instance + output server."""
        # Start output server
        server_thread = threading.Thread(target=self._output_server, daemon=True,
                                         name="iq_router_server")
        server_thread.start()
        self._threads.append(server_thread)

        # Start one reader per instance
        for cfg in self.instance_configs:
            t = threading.Thread(target=self._instance_reader, args=(cfg,),
                                daemon=True,
                                name="iq_reader_{}".format(cfg["instance_id"]))
            t.start()
            self._threads.append(t)

        self.logger.info("IQ Router started: %d sources, output on port %d",
                         len(self.instance_configs), self.output_port)

    def _output_server(self):
        """Accept clients on the unified output port."""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.settimeout(1.0)
        try:
            server_sock.bind(("", self.output_port))
            server_sock.listen(5)
        except socket.error as e:
            self.logger.error("Failed to bind IQ router output on port %d: %s",
                              self.output_port, e)
            return

        while not self._stop_event.is_set():
            try:
                client, addr = server_sock.accept()
                self.logger.info("IQ Router: client connected from %s:%d", addr[0], addr[1])
                with self._output_lock:
                    self._output_clients.append(client)
            except socket.timeout:
                continue
            except socket.error:
                break
        server_sock.close()

    def _instance_reader(self, cfg):
        """Connect to one instance's IQ server and forward frames."""
        host = cfg["host"]
        port = cfg["iq_port"]
        iid = cfg["instance_id"]

        while not self._stop_event.is_set():
            try:
                sock = socket.create_connection((host, port), timeout=5.0)
                # Send streaming command (iq_server expects this)
                sock.sendall(b"streaming")
                with self._stats_lock:
                    self._stats[iid]["connected"] = True
                self.logger.info("IQ Router: connected to instance %d at %s:%d", iid, host, port)

                while not self._stop_event.is_set():
                    # Read IQ header
                    header = self._recv_exact(sock, IQ_HEADER_LENGTH)
                    if header is None:
                        break

                    # Validate sync word (first 4 bytes, little-endian)
                    sync = struct.unpack_from("<I", header, 0)[0]
                    if sync != SYNC_WORD:
                        self.logger.warning("IQ Router: bad sync word from instance %d", iid)
                        break

                    # Read payload size from header
                    # cpi_length is at offset 36 (uint32), active_ant_chs at offset 20 (uint32)
                    # payload = cpi_length * active_ant_chs * 2 (I+Q) * 4 (float32)
                    active_chs = struct.unpack_from("<I", header, 20)[0]
                    cpi_length = struct.unpack_from("<I", header, 36)[0]
                    payload_size = active_chs * cpi_length * 2 * 4  # float32 I/Q

                    payload = b""
                    if payload_size > 0:
                        payload = self._recv_exact(sock, payload_size)
                        if payload is None:
                            break

                    frame = header + payload

                    # Update stats
                    with self._stats_lock:
                        self._stats[iid]["frames_received"] += 1
                        self._stats[iid]["bytes_received"] += len(frame)
                        self._stats[iid]["last_frame_time"] = time.time()

                    # Forward to all output clients
                    self._forward_frame(frame)

                sock.close()
            except Exception as e:
                self.logger.warning("IQ Router: instance %d connection error: %s", iid, e)

            with self._stats_lock:
                self._stats[iid]["connected"] = False

            # Retry after delay
            if not self._stop_event.is_set():
                self._stop_event.wait(3.0)

    def _recv_exact(self, sock, nbytes):
        """Receive exactly nbytes from socket."""
        data = b""
        while len(data) < nbytes:
            try:
                chunk = sock.recv(nbytes - len(data))
                if not chunk:
                    return None
                data += chunk
            except socket.timeout:
                if self._stop_event.is_set():
                    return None
            except socket.error:
                return None
        return data

    def _forward_frame(self, frame):
        """Send a complete IQ frame to all connected output clients."""
        dead_clients = []
        with self._output_lock:
            for client in self._output_clients:
                try:
                    client.sendall(frame)
                except Exception:
                    dead_clients.append(client)
            for client in dead_clients:
                self._output_clients.remove(client)
                try:
                    client.close()
                except Exception:
                    pass

    def get_stream_stats(self):
        """Return per-instance stream statistics."""
        with self._stats_lock:
            return dict(self._stats)

    def close(self):
        """Stop all threads and close connections."""
        self._stop_event.set()
        with self._output_lock:
            for client in self._output_clients:
                try:
                    client.close()
                except Exception:
                    pass
            self._output_clients.clear()
        for t in self._threads:
            t.join(timeout=3)
        self.logger.info("IQ Router stopped")
