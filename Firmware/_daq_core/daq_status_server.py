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
from collections import deque


class StatusServer:
    """TCP server thread that exposes pipeline health as JSON."""

    # Maximum number of concurrently served clients
    MAX_CLIENTS = 8

    def __init__(self, port=5002, metrics=None, event_bus=None, db=None,
                 instance_id=None, listen_address="0.0.0.0",
                 drop_window_sec=60.0):
        self._port = port
        self._metrics = metrics
        self._event_bus = event_bus
        self._db = db
        self._instance_id = instance_id
        self._listen_address = listen_address
        self._status = {}
        self._status_lock = threading.Lock()
        self._start_time = time.time()
        self._stop = threading.Event()
        self._logger = logging.getLogger("heimdall.status")
        # Windowed frame-drop tracking: health is judged on drops within the
        # recent window, while the raw cumulative counters remain exposed in
        # the snapshot's "counters" dict.
        self._drop_window_sec = drop_window_sec
        self._prev_drop_total = None
        self._drop_events = deque()  # (timestamp, delta) pairs
        self._client_sem = threading.Semaphore(self.MAX_CLIENTS)
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="StatusServer")

    def start(self):
        self._thread.start()

    def update_status(self, snapshot):
        """Thread-safe setter called from the main frame loop."""
        now = time.time()
        counters = snapshot.get("counters") or {}
        drop_total = (counters.get("dropped_frames_iq", 0) +
                      counters.get("dropped_frames_hwc", 0))
        with self._status_lock:
            if self._prev_drop_total is None or drop_total < self._prev_drop_total:
                delta = 0  # first snapshot / counter reset: no window entry
            else:
                delta = drop_total - self._prev_drop_total
            self._prev_drop_total = drop_total
            if delta > 0:
                self._drop_events.append((now, delta))
            self._prune_drop_events(now)
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

    def _prune_drop_events(self, now):
        """Drop window entries older than the window. Caller holds the lock."""
        cutoff = now - self._drop_window_sec
        while self._drop_events and self._drop_events[0][0] < cutoff:
            self._drop_events.popleft()

    def _serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self._listen_address, self._port))
            srv.listen(4)
        except socket.error as e:
            self._logger.error("StatusServer bind failed on port %d: %s", self._port, e)
            return
        srv.settimeout(1.0)
        self._logger.info("StatusServer listening on %s:%d",
                          self._listen_address, self._port)

        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # One thread per client so a stalled consumer cannot block the
            # other monitoring clients (federation peers poll this endpoint).
            if not self._client_sem.acquire(blocking=False):
                self._logger.warning("StatusServer: client limit reached, rejecting %s", addr)
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._client_worker, args=(conn,),
                             daemon=True, name="StatusClient").start()
        srv.close()

    def _client_worker(self, conn):
        try:
            self._handle_client(conn)
        except Exception:
            self._logger.exception("StatusServer client handler error")
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._client_sem.release()

    def _handle_client(self, conn):
        conn.settimeout(5.0)
        try:
            data = conn.recv(1024)
        except socket.timeout:
            return
        if not data:
            return
        line = data.decode(errors="replace").strip()
        parts = line.split(None, 1)
        cmd = parts[0].upper() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "PING":
            resp = {"ok": True, "ts": time.time()}
        elif cmd == "STATUS":
            resp = self._build_status()
        elif cmd == "METRICS":
            resp = self._build_metrics()
        elif cmd == "EVENTS":
            resp = self._build_events()
        elif cmd == "EVENTS_DROPPED":
            resp = self._build_events_dropped()
        elif cmd == "DB_STATS":
            resp = self._build_db_stats()
        elif cmd == "SCAN_SUMMARY":
            resp = self._build_scan_summary(arg)
        elif cmd == "CAL_HISTORY":
            resp = self._build_cal_history(arg)
        else:
            resp = {"error": "unknown command",
                    "valid": ["PING", "STATUS", "METRICS", "EVENTS",
                              "EVENTS_DROPPED", "DB_STATS", "SCAN_SUMMARY",
                              "CAL_HISTORY"]}
        try:
            conn.sendall((json.dumps(resp) + "\n").encode())
        except Exception:
            self._logger.debug("StatusServer: reply send failed", exc_info=True)

    def _build_status(self):
        now = time.time()
        with self._status_lock:
            snapshot = dict(self._status)
            self._prune_drop_events(now)
            recent_drops = sum(delta for _, delta in self._drop_events)
        snapshot["timestamp"] = now
        snapshot["uptime_sec"] = round(now - self._start_time, 1)
        if self._instance_id is not None:
            snapshot.setdefault("instance_id", self._instance_id)
        # Derive pipeline health from the drop rate over the recent window
        # (cumulative totals stay available in snapshot["counters"])
        sync_state = snapshot.get("sync_state", 0)
        snapshot["recent_drops"] = recent_drops
        snapshot["drop_window_sec"] = self._drop_window_sec
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

    def _build_events_dropped(self):
        if self._event_bus is None:
            return {"error": "event bus not enabled"}
        return {"dropped_events": self._event_bus.dropped_events}

    # ---- persistence read API proxies (active only when a db is wired) ----

    def _build_db_stats(self):
        if self._db is None:
            return {"error": "database not enabled"}
        try:
            return self._db.get_db_stats()
        except Exception as e:
            self._logger.exception("DB_STATS query failed")
            return {"error": str(e)}

    def _build_scan_summary(self, arg):
        if self._db is None:
            return {"error": "database not enabled"}
        try:
            freq = int(arg) if arg else None
            records = self._db.get_freq_scan_summary(freq)
            return {"freq_scan": [vars(r) for r in records]}
        except ValueError:
            return {"error": "usage: SCAN_SUMMARY [rf_center_freq_hz]"}
        except Exception as e:
            self._logger.exception("SCAN_SUMMARY query failed")
            return {"error": str(e)}

    def _build_cal_history(self, arg):
        if self._db is None:
            return {"error": "database not enabled"}
        try:
            freq = int(arg) if arg else None
            records = self._db.get_cal_history(rf_center_freq=freq, limit=100)
            return {"cal_history": [vars(r) for r in records]}
        except ValueError:
            return {"error": "usage: CAL_HISTORY [rf_center_freq_hz]"}
        except Exception as e:
            self._logger.exception("CAL_HISTORY query failed")
            return {"error": str(e)}
