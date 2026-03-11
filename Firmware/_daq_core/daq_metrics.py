"""
    Rolling Statistics Engine for DAQ Performance Metrics

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Provides O(1) recording and windowed statistics (min, max, avg, p95)
    using pre-allocated circular numpy buffers. Single-writer safe —
    reads from the status server thread tolerate stale data.
"""
import numpy as np


class MetricsCollector:
    """Collects rolling performance metrics using circular buffers."""

    def __init__(self, window_size=1000):
        self._window_size = window_size
        self._metrics = {}  # name -> {"buf": np.array, "idx": int, "count": int}

    def _ensure_metric(self, name):
        if name not in self._metrics:
            self._metrics[name] = {
                "buf": np.zeros(self._window_size, dtype=np.float64),
                "idx": 0,
                "count": 0,
            }

    def record(self, name, value):
        """Record a single metric value. O(1)."""
        self._ensure_metric(name)
        m = self._metrics[name]
        m["buf"][m["idx"] % self._window_size] = value
        m["idx"] += 1
        if m["count"] < self._window_size:
            m["count"] += 1

    def get_stats(self, name):
        """Return {min, max, avg, p95, count, last} for a named metric."""
        if name not in self._metrics:
            return {"min": 0.0, "max": 0.0, "avg": 0.0, "p95": 0.0,
                    "count": 0, "last": 0.0}
        m = self._metrics[name]
        n = m["count"]
        if n == 0:
            return {"min": 0.0, "max": 0.0, "avg": 0.0, "p95": 0.0,
                    "count": 0, "last": 0.0}
        data = m["buf"][:n] if n < self._window_size else m["buf"]
        last_idx = (m["idx"] - 1) % self._window_size
        return {
            "min": float(np.min(data)),
            "max": float(np.max(data)),
            "avg": float(np.mean(data)),
            "p95": float(np.percentile(data, 95)),
            "count": n,
            "last": float(m["buf"][last_idx]),
        }

    def get_all_stats(self):
        """Return stats dict for every registered metric."""
        return {name: self.get_stats(name) for name in self._metrics}

    def reset(self, name):
        """Clear a single metric."""
        if name in self._metrics:
            m = self._metrics[name]
            m["buf"][:] = 0.0
            m["idx"] = 0
            m["count"] = 0
