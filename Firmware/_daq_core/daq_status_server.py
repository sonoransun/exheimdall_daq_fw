"""
    Lightweight TCP Status/Health Endpoint for DAQ Pipeline

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Runs as a daemon thread inside delay_sync.py, accepting simple
    line-based commands (PING, STATUS, METRICS, EVENTS) and returning
    JSON responses. Modelled after the CtrIfaceServer pattern in
    hw_controller.py but read-only.

    ``extended=True`` ([monitoring] status_extended = 1) enables the
    extended grammar of design spec G.1/G.2/G.7: snapshot freshness
    (``snapshot_age_sec``/``stale_after_sec`` and ``pipeline_health =
    "stale"``), the ``status_protocol`` capability key, the live ``hwc`` and
    ``federation`` blocks, an explicit ``{"error": "busy"}`` reply at the
    client cap, and the verbs PEERS, HWC, HW_SNAPSHOTS plus the 2/3-argument
    CAL_HISTORY form. With ``extended=False`` (default) every reply byte,
    the client cap, the listen backlog and the health derivation are the
    pre-extension ones.
"""
import json
import time
import socket
import logging
import threading
from collections import deque

#: Value of the extended STATUS ``status_protocol`` key (capability probe).
STATUS_PROTOCOL = 2

#: Largest ``limit`` accepted by the extended CAL_HISTORY / HW_SNAPSHOTS forms.
MAX_QUERY_LIMIT = 1000
DEFAULT_QUERY_LIMIT = 100

_LEGACY_VALID = ["PING", "STATUS", "METRICS", "EVENTS", "EVENTS_DROPPED",
                 "DB_STATS", "SCAN_SUMMARY", "CAL_HISTORY"]
_EXTENDED_VALID = _LEGACY_VALID + ["PEERS", "HWC", "HW_SNAPSHOTS"]

CAL_HISTORY_USAGE = "usage: CAL_HISTORY [rf_center_freq_hz]"
CAL_HISTORY_EXT_USAGE = "usage: CAL_HISTORY [rf_center_freq_hz|-] [since_ms] [limit]"
HW_SNAPSHOTS_USAGE = "usage: HW_SNAPSHOTS [since_ms] [limit]"


def auto_stale_after_sec(update_interval_frames, frame_period_sec):
    """Automatic staleness threshold (spec G.2).

    ``3 * max(1, k_eff) * frame_period_sec + 1.0`` where ``k_eff`` is the
    effective snapshot update interval in frames. Returns None when the
    frame period is unknown (staleness is then never reported).
    """
    if frame_period_sec is None or frame_period_sec <= 0:
        return None
    k_eff = max(1, int(update_interval_frames or 0))
    return 3.0 * k_eff * float(frame_period_sec) + 1.0


def _parse_nonneg_int(token):
    if not token.isdigit() or not token.isascii():
        raise ValueError(token)
    return int(token)


def _parse_limit(token):
    limit = _parse_nonneg_int(token)
    if limit < 1 or limit > MAX_QUERY_LIMIT:
        raise ValueError(token)
    return limit


class StatusServer:
    """TCP server thread that exposes pipeline health as JSON."""

    # Maximum number of concurrently served clients (legacy mode; the
    # extended mode uses [monitoring] status_max_clients, default 8)
    MAX_CLIENTS = 8
    LEGACY_BACKLOG = 4
    EXTENDED_BACKLOG = 16

    def __init__(self, port=5002, metrics=None, event_bus=None, db=None,
                 instance_id=None, listen_address="0.0.0.0",
                 drop_window_sec=60.0, *, extended=False, stale_after_sec=0.0,
                 frame_period_sec=None, update_interval_frames=1,
                 max_clients=None, hwc_provider=None, federation_provider=None,
                 _clock=time.monotonic):
        """Keyword-only options (all inert unless ``extended``):

        stale_after_sec : float
            > 0: explicit staleness threshold [s]. 0 (default): automatic,
            ``auto_stale_after_sec(update_interval_frames, frame_period_sec)``.
        frame_period_sec, update_interval_frames
            Inputs of the automatic threshold.
        max_clients : int or None
            Concurrent client cap (None = MAX_CLIENTS).
        hwc_provider : callable or None
            ``() -> (view, error)``: the hwc relay document minus events
            (``view``, None when unreadable) and an error text. None = relay
            not enabled.
        federation_provider : callable or None
            ``() -> dict or None``: the STATUS ``federation`` block. None or
            a None result = federation not enabled.
        _clock : callable
            Monotonic clock (test hook).
        """
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
        self._extended = bool(extended)
        self._clock = _clock
        self._last_update_mono = _clock()
        if stale_after_sec and stale_after_sec > 0:
            self._stale_after_sec = float(stale_after_sec)
        else:
            self._stale_after_sec = auto_stale_after_sec(update_interval_frames,
                                                         frame_period_sec)
        self._hwc_provider = hwc_provider
        self._federation_provider = federation_provider
        if self._extended and max_clients is not None:
            self._max_clients = max(1, int(max_clients))
        else:
            self._max_clients = self.MAX_CLIENTS
        self._client_sem = threading.Semaphore(self._max_clients)
        # Bounds the extended-mode busy repliers; past it a rejected
        # connection is closed silently, as in legacy mode
        self._busy_sem = threading.Semaphore(self._max_clients)
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
            self._last_update_mono = self._clock()

    @property
    def stale_after_sec(self):
        """Resolved staleness threshold [s] (None = never stale)."""
        return self._stale_after_sec

    def get_pipeline_health(self):
        """Current pipeline_health as STATUS would report it (thread-safe)."""
        now = time.time()
        with self._status_lock:
            sync_state = self._status.get("sync_state", 0)
            self._prune_drop_events(now)
            recent_drops = sum(delta for _, delta in self._drop_events)
            age = self._clock() - self._last_update_mono
        return self._derive_health(sync_state, recent_drops, age)

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

    def _derive_health(self, sync_state, recent_drops, snapshot_age_sec):
        """Health rule: the pre-extension drop/sync rule, plus "stale" in
        extended mode when the snapshot is older than the threshold."""
        if (self._extended and self._stale_after_sec is not None
                and snapshot_age_sec > self._stale_after_sec):
            return "stale"
        if sync_state >= 5 and recent_drops == 0:
            return "ok"
        if sync_state >= 2:
            return "degraded"
        return "error"

    def _serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self._listen_address, self._port))
            srv.listen(self.EXTENDED_BACKLOG if self._extended else self.LEGACY_BACKLOG)
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
                if self._extended and self._busy_sem.acquire(blocking=False):
                    # Explicit busy reply on a short-lived, bounded thread, so
                    # a slow rejected client never stalls the accept loop
                    if self._start_thread(self._reject_busy, conn, "StatusBusy"):
                        continue
                    self._busy_sem.release()
                self._close_quietly(conn)
                continue
            if not self._start_thread(self._client_worker, conn, "StatusClient"):
                self._client_sem.release()
                self._close_quietly(conn)
        srv.close()

    def _start_thread(self, target, conn, name):
        """Start a per-connection thread; False when the thread cannot be
        created, so the accept loop survives thread exhaustion."""
        try:
            threading.Thread(target=target, args=(conn,), daemon=True, name=name).start()
        except RuntimeError as e:
            self._logger.warning("StatusServer: cannot start %s thread (%s); closing", name, e)
            return False
        return True

    @staticmethod
    def _close_quietly(conn):
        try:
            conn.close()
        except OSError:
            pass

    def _reject_busy(self, conn):
        """Reply ``{"error": "busy"}`` and close (extended mode, client cap).

        The request line is drained after the reply so the close is a FIN,
        not a reset that could discard the reply at the client.
        """
        try:
            conn.settimeout(1.0)
            conn.sendall((json.dumps({"error": "busy"}) + "\n").encode())
            conn.shutdown(socket.SHUT_WR)
            conn.recv(1024)
        except OSError:
            pass
        finally:
            self._close_quietly(conn)
            self._busy_sem.release()

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
            if self._extended and len(arg.split()) > 1:
                resp = self._build_cal_history_ext(arg.split())
            else:
                resp = self._build_cal_history(arg)
        elif self._extended and cmd == "PEERS":
            resp = self._build_peers()
        elif self._extended and cmd == "HWC":
            resp = self._build_hwc()
        elif self._extended and cmd == "HW_SNAPSHOTS":
            resp = self._build_hw_snapshots(arg.split())
        else:
            resp = {"error": "unknown command",
                    "valid": list(_EXTENDED_VALID if self._extended else _LEGACY_VALID)}
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
            snapshot_age = self._clock() - self._last_update_mono
        snapshot["timestamp"] = now
        snapshot["uptime_sec"] = round(now - self._start_time, 1)
        if self._instance_id is not None:
            snapshot.setdefault("instance_id", self._instance_id)
        # Derive pipeline health from the drop rate over the recent window
        # (cumulative totals stay available in snapshot["counters"])
        sync_state = snapshot.get("sync_state", 0)
        snapshot["recent_drops"] = recent_drops
        snapshot["drop_window_sec"] = self._drop_window_sec
        snapshot["pipeline_health"] = self._derive_health(sync_state, recent_drops,
                                                          snapshot_age)
        if self._extended:
            snapshot["status_protocol"] = STATUS_PROTOCOL
            snapshot["snapshot_age_sec"] = round(max(0.0, snapshot_age), 3)
            snapshot["stale_after_sec"] = (None if self._stale_after_sec is None
                                           else round(self._stale_after_sec, 3))
            if self._hwc_provider is not None:
                view, error = self._call_hwc_provider()
                snapshot["hwc"] = view
                if view is None:
                    snapshot["hwc_error"] = error
            federation = self._call_federation_provider()
            if federation is not None:
                snapshot["federation"] = federation
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

    def _call_hwc_provider(self):
        try:
            view, error = self._hwc_provider()
        except Exception as e:
            self._logger.exception("StatusServer: hwc provider failed")
            return None, str(e)
        return view, error

    def _call_federation_provider(self):
        if self._federation_provider is None:
            return None
        try:
            return self._federation_provider()
        except Exception:
            self._logger.exception("StatusServer: federation provider failed")
            return None

    def _build_peers(self):
        federation = self._call_federation_provider()
        if federation is None:
            return {"error": "federation not enabled"}
        return {key: federation.get(key) for key in
                ("instance_id", "node_id", "advisory_leader", "peers")}

    def _build_hwc(self):
        if self._hwc_provider is None:
            return {"error": "hwc relay not enabled"}
        view, error = self._call_hwc_provider()
        if view is None:
            return {"error": error or "hwc state unavailable"}
        return view

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
            stats = self._db.get_db_stats()
            if self._extended:
                info_fn = getattr(self._db, "get_instance_info", None)
                if info_fn is not None:
                    info = info_fn()
                    stats["db_dir"] = info.get("db_dir")
                    stats["instance_id"] = info.get("instance_id")
            return stats
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
            return {"error": CAL_HISTORY_USAGE}
        except Exception as e:
            self._logger.exception("CAL_HISTORY query failed")
            return {"error": str(e)}

    def _build_cal_history_ext(self, args):
        """Extended ``CAL_HISTORY <freq|-> <since_ms> [limit]`` (2/3 args)."""
        if self._db is None:
            return {"error": "database not enabled"}
        try:
            if len(args) > 3:
                raise ValueError(args)
            freq = None if args[0] == "-" else _parse_nonneg_int(args[0])
            since_ms = _parse_nonneg_int(args[1])
            limit = _parse_limit(args[2]) if len(args) == 3 else DEFAULT_QUERY_LIMIT
        except ValueError:
            return {"error": CAL_HISTORY_EXT_USAGE}
        try:
            records = self._db.get_cal_history(start_ts_ms=since_ms,
                                               rf_center_freq=freq, limit=limit)
            return {"cal_history": [vars(r) for r in records]}
        except Exception as e:
            self._logger.exception("CAL_HISTORY query failed")
            return {"error": str(e)}

    def _build_hw_snapshots(self, args):
        """Extended ``HW_SNAPSHOTS [since_ms] [limit]``."""
        if self._db is None:
            return {"error": "database not enabled"}
        try:
            if len(args) > 2:
                raise ValueError(args)
            since_ms = _parse_nonneg_int(args[0]) if args else 0
            limit = _parse_limit(args[1]) if len(args) == 2 else DEFAULT_QUERY_LIMIT
        except ValueError:
            return {"error": HW_SNAPSHOTS_USAGE}
        try:
            records = self._db.get_hw_snapshots(start_ts_ms=since_ms, limit=limit)
            return {"hw_snapshots": [vars(r) for r in records]}
        except Exception as e:
            self._logger.exception("HW_SNAPSHOTS query failed")
            return {"error": str(e)}
