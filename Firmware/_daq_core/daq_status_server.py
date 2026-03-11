"""
    Lightweight TCP Status/Health Endpoint for DAQ Pipeline

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Runs as a daemon thread inside delay_sync.py, accepting simple
    line-based commands (PING, STATUS, METRICS, EVENTS) and returning
    JSON responses. Modelled after the CtrIfaceServer pattern in
    hw_controller.py but read-only.
"""
import json
import time
import socket
import logging
import threading


class StatusServer:
    """TCP server thread that exposes pipeline health as JSON."""

    def __init__(self, port=5002, metrics=None, event_bus=None):
        self._port = port
        self._metrics = metrics
        self._event_bus = event_bus
        self._status = {}
        self._status_lock = threading.Lock()
        self._start_time = time.time()
        self._stop = threading.Event()
        self._logger = logging.getLogger("heimdall.status")
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="StatusServer")

    def start(self):
        self._thread.start()

    def update_status(self, snapshot):
        """Thread-safe setter called from the main frame loop."""
        with self._status_lock:
            self._status = snapshot

    def close(self):
        self._stop.set()
        # Connect to unblock accept()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(("127.0.0.1", self._port))
            s.close()
        except Exception:
            pass
        self._thread.join(timeout=2.0)

    # ---- internal ----

    def _serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", self._port))
            srv.listen(4)
        except socket.error as e:
            self._logger.error("StatusServer bind failed on port %d: %s", self._port, e)
            return
        srv.settimeout(1.0)
        self._logger.info("StatusServer listening on port %d", self._port)

        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_client(conn)
            except Exception:
                pass
            finally:
                conn.close()
        srv.close()

    def _handle_client(self, conn):
        conn.settimeout(5.0)
        try:
            data = conn.recv(1024)
        except socket.timeout:
            return
        if not data:
            return
        cmd = data.decode(errors="replace").strip().upper()
        if cmd == "PING":
            resp = {"ok": True, "ts": time.time()}
        elif cmd == "STATUS":
            resp = self._build_status()
        elif cmd == "METRICS":
            resp = self._build_metrics()
        elif cmd == "EVENTS":
            resp = self._build_events()
        else:
            resp = {"error": "unknown command", "valid": ["PING", "STATUS", "METRICS", "EVENTS"]}
        try:
            conn.sendall((json.dumps(resp) + "\n").encode())
        except Exception:
            pass

    def _build_status(self):
        with self._status_lock:
            snapshot = dict(self._status)
        snapshot["timestamp"] = time.time()
        snapshot["uptime_sec"] = round(time.time() - self._start_time, 1)
        # Derive pipeline health
        sync_state = snapshot.get("sync_state", 0)
        dropped = snapshot.get("counters", {})
        recent_drops = dropped.get("dropped_frames_iq", 0) + dropped.get("dropped_frames_hwc", 0)
        if sync_state >= 5 and recent_drops == 0:
            health = "ok"
        elif sync_state >= 2:
            health = "degraded"
        else:
            health = "error"
        snapshot["pipeline_health"] = health
        # Inline metrics if available
        if self._metrics is not None:
            latency = self._metrics.get_stats("frame_processing_latency_ms")
            throughput = self._metrics.get_stats("frame_throughput_fps")
            snapshot["latency"] = {
                "min_ms": round(latency["min"], 2),
                "max_ms": round(latency["max"], 2),
                "avg_ms": round(latency["avg"], 2),
                "p95_ms": round(latency["p95"], 2),
            }
            snapshot["throughput"] = {
                "min_fps": round(throughput["min"], 2),
                "max_fps": round(throughput["max"], 2),
                "avg_fps": round(throughput["avg"], 2),
            }
        return snapshot

    def _build_metrics(self):
        if self._metrics is None:
            return {"error": "metrics not enabled"}
        return self._metrics.get_all_stats()

    def _build_events(self):
        if self._event_bus is None:
            return {"error": "event bus not enabled"}
        events = self._event_bus.get_recent_events(100)
        return {"events": [e.to_dict() for e in events]}
