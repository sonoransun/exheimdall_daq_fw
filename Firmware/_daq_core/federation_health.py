"""
    Federation Health Monitor — Peer Discovery and Health Tracking

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Polls peer DAQ instances via their StatusServer endpoints to maintain
    a health table. Emits federation events (peer_up, peer_down, peer_degraded)
    through the EventBus. Supports coordinator election via lowest healthy
    instance_id.
"""
import json
import socket
import time
import logging
import threading


class FederationHealth:
    """Lightweight peer health monitor using StatusServer TCP endpoints."""

    def __init__(self, instance_id, peer_addresses, poll_interval=5.0, event_bus=None):
        """
        Parameters
        ----------
        instance_id : int
            This instance's ID.
        peer_addresses : list of str
            List of "host:status_port" strings for peer instances.
        poll_interval : float
            Seconds between health polls.
        event_bus : EventBus or None
            Optional event bus for emitting federation events.
        """
        self.logger = logging.getLogger(__name__)
        self.instance_id = instance_id
        self.poll_interval = poll_interval
        self.event_bus = event_bus
        self._peer_addresses = peer_addresses
        self._peer_table = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._coordinator_id = instance_id  # assume self until proven otherwise

        # Initialize peer table
        for addr in peer_addresses:
            self._peer_table[addr] = {
                "alive": False,
                "last_seen": 0.0,
                "sync_state": 0,
                "frequency_hz": 0,
                "health": "unknown",
                "instance_id": -1,
            }

    def start(self):
        """Start the background polling thread."""
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="federation_health")
        self._thread.start()
        self.logger.info("Federation health monitor started, polling %d peers every %.1fs",
                         len(self._peer_addresses), self.poll_interval)

    def _poll_loop(self):
        while not self._stop_event.is_set():
            for addr in self._peer_addresses:
                self._poll_peer(addr)
            self._check_coordinator()
            self._stop_event.wait(self.poll_interval)

    def _poll_peer(self, addr):
        """Send PING and optionally STATUS to a single peer."""
        host, port_str = addr.rsplit(":", 1)
        port = int(port_str)
        try:
            with socket.create_connection((host, port), timeout=2.0) as sock:
                sock.sendall(b"STATUS\n")
                data = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            response = json.loads(data.decode("utf-8", errors="replace"))
            was_alive = self._peer_table[addr]["alive"]
            with self._lock:
                self._peer_table[addr].update({
                    "alive": True,
                    "last_seen": time.time(),
                    "sync_state": response.get("sync_state", 0),
                    "frequency_hz": response.get("current_frequency_hz", 0),
                    "health": response.get("pipeline_health", "unknown"),
                    "instance_id": response.get("instance_id", -1),
                })
            if not was_alive:
                self._emit_event("peer_up", addr)
        except Exception:
            was_alive = self._peer_table[addr]["alive"]
            now = time.time()
            with self._lock:
                # Mark as down if 3 intervals missed
                if now - self._peer_table[addr]["last_seen"] > 3 * self.poll_interval:
                    if was_alive:
                        self._peer_table[addr]["alive"] = False
                        self._peer_table[addr]["health"] = "error"
                        self._emit_event("peer_down", addr)

    def _check_coordinator(self):
        """Elect coordinator as lowest instance_id among healthy peers + self."""
        candidates = [self.instance_id]
        with self._lock:
            for addr, info in self._peer_table.items():
                if info["alive"] and info["health"] != "error" and info["instance_id"] >= 0:
                    candidates.append(info["instance_id"])
        new_coordinator = min(candidates)
        if new_coordinator != self._coordinator_id:
            self._coordinator_id = new_coordinator
            self._emit_event("coordinator_elected", None,
                             payload={"coordinator_id": new_coordinator})

    def _emit_event(self, event_type, addr, payload=None):
        if self.event_bus is None:
            return
        try:
            from daq_events import DAQEvent, EVT_PEER_UP, EVT_PEER_DOWN, EVT_COORDINATOR_ELECTED
            evt_map = {
                "peer_up": EVT_PEER_UP,
                "peer_down": EVT_PEER_DOWN,
                "peer_degraded": "peer_degraded",
                "coordinator_elected": EVT_COORDINATOR_ELECTED,
            }
            p = payload or {}
            if addr is not None:
                p["peer_address"] = addr
            self.event_bus.emit(DAQEvent(
                timestamp=time.time(),
                severity="info" if "up" in event_type or "elected" in event_type else "warning",
                module="federation",
                event_type=evt_map.get(event_type, event_type),
                payload=p,
            ))
        except Exception:
            pass

    def get_peer_table(self):
        """Return a copy of the current peer health table."""
        with self._lock:
            return dict(self._peer_table)

    def get_healthy_peers(self):
        """Return list of peer addresses that are alive and not in error state."""
        with self._lock:
            return [addr for addr, info in self._peer_table.items()
                    if info["alive"] and info["health"] != "error"]

    def get_coordinator_id(self):
        """Return the current coordinator instance_id."""
        return self._coordinator_id

    def close(self):
        """Stop the polling thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval + 1)
        self.logger.info("Federation health monitor stopped")
