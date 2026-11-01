"""Throughput lane: 300 paced synthetic frames through one chain (spec J.1 WP-17b, I.6).

Sudo lane (``./unit_test.sh --sudo``, CI ``pipeline`` job): Linux, root or
passwordless sudo, built C binaries, numpy, scipy, pyzmq.

One chain is started from a private install root (test_synthetic_chain's
Sandbox) with ``daq_synthetic_start.sh --pace --blocks 0``: the synthesizer
emits blocks at the configured sample rate, like real receivers. After the
chain has locked, a real ``streaming``/``IQDownload`` client on ``:5000``
receives FRAMES frames while delay_sync's ``frame_processing_latency_ms``
metric is sampled on ``:5002`` (METRICS, every 50 ms; each new per-frame
value is kept).

Asserts: ``cpi_index`` strictly increasing (no reset or repeat); zero USB
overruns (slot 102 is 0 in every header); bounded drops (frames missing from
the client's ``cpi_index`` sequence <= 1 %); ``frame_processing_latency_ms``
p95 < 0.5 x the frame period (``cpi_size * decimation_ratio / sample_rate``).

Writes ``Firmware/_testing/test_logs/throughput.json`` (a CI artefact; the
measured rows of the capacity guide come from it):
``{"date", "host", "nproc", "model", "frames", "p50_ms", "p95_ms", "drops",
"overruns", "fps"}`` - ``p50_ms``/``p95_ms`` are delay_sync's per-frame
processing latency, ``drops`` the frames missing on ``:5000``, ``overruns``
the slot-102 increase over the window, ``fps`` frames per second at the
client.

The INI is the repository's with the monitoring keys added and
``cal_track_mode = 0`` (one calibration, then the locked data path that the
capacity figures describe; periodic calibration bursts are a calibration
policy, not data-path throughput).
"""
import json
import os
import platform
import socket
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_synthetic_chain as chain  # noqa: E402

FRAMES = 300
METRIC = "frame_processing_latency_ms"
RESULT = os.path.join(chain.FIRMWARE, "_testing", "test_logs", "throughput.json")
#: large enough that the metric's sample count keeps increasing during the lane
METRICS_WINDOW = 100000


def percentile(values, q):
    """Linear-interpolation percentile (numpy's default) of a non-empty list."""
    s = sorted(values)
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def host_model():
    """Board model (device tree), else the CPU model name, else the machine type."""
    try:
        with open("/proc/device-tree/model", "rb") as f:
            model = f.read().rstrip(b"\0").decode(errors="replace").strip()
            if model:
                return model
    except OSError:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith(("model name", "hardware")):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.machine()


class LatencySampler(threading.Thread):
    """Polls METRICS; keeps (monotonic, value) whenever the metric's count advanced."""

    def __init__(self, port, period=0.05):
        super().__init__(daemon=True)
        self.port = port
        self.period = period
        self.samples = []
        self.missed = 0
        self._stop_evt = threading.Event()

    def run(self):
        last_count = None
        while not self._stop_evt.is_set():
            reply = chain.query(self.port, "METRICS", timeout=2.0)
            stats = reply.get(METRIC) if isinstance(reply, dict) else None
            if stats and stats.get("count"):
                count = stats["count"]
                if last_count is not None and count > last_count:
                    self.samples.append((time.monotonic(), float(stats["last"])))
                    self.missed += count - last_count - 1
                last_count = count
            self._stop_evt.wait(self.period)

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=10)


@unittest.skipIf(chain.WHY_NOT, chain.WHY_NOT)
class TestThroughputLane(unittest.TestCase):

    def setUp(self):
        chain.refuse_running_chain()
        self.box = chain.Sandbox("throughput")
        mon = dict(chain.MONITORING, en_metrics="1", metrics_window_size=str(METRICS_WINDOW))
        chain.set_ini(self.box.ini, {"monitoring": mon,
                                     "calibration": {"cal_track_mode": "0"},
                                     "data_interface": {"out_data_iface_type": "eth"},
                                     "federation": {"instance_id": "0", "port_stride": "100"}})
        cfg = chain.read_ini(self.box.ini)
        self.frame_period = (cfg.getint("pre_processing", "cpi_size") *
                             cfg.getint("pre_processing", "decimation_ratio") /
                             float(cfg.getint("daq", "sample_rate")))

    def tearDown(self):
        self.box.cleanup()

    def test_paced_frames(self):
        box = self.box
        rc, out = box.sh(box.fw, "./daq_synthetic_start.sh", "--pace", "--blocks", "0")
        self.assertEqual(rc, 0, out + box.logs())
        locked = chain.wait_for(lambda: (chain.sync_state(5002) or 0) >= chain.SYNC_LOCKED,
                                timeout=chain.sync_timeout(150))
        self.assertTrue(locked, "no lock: %s\n%s" % (chain.status(5002), box.logs()))
        self.assertTrue(chain.wait_steady(5002), "no steady locked DATA stream: %s" % chain.status(5002))

        sampler = LatencySampler(5002)
        sampler.start()
        client = chain.connect_iq(5000)
        try:
            for _ in range(chain.STALE_FRAMES):
                client.frame()
            headers = []
            t0 = time.monotonic()
            for _ in range(FRAMES):
                headers.append(client.frame()[1])
            t1 = time.monotonic()
        finally:
            client.close()
            sampler.stop()

        cpis = [h.cpi_index for h in headers]
        steps = [b - a for a, b in zip(cpis, cpis[1:])]
        drops = sum(s - 1 for s in steps if s > 1)
        overrun_cnts = [h.get_buffer_overrun_cnt() for h in headers]
        latencies = [v for t, v in sampler.samples if t0 <= t <= t1]
        self.assertGreaterEqual(len(latencies), FRAMES // 2,
                                "too few latency samples (%d, %d missed)" % (len(latencies), sampler.missed))
        result = {
            "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": socket.gethostname(),
            "nproc": os.cpu_count(),
            "model": host_model(),
            "frames": len(headers),
            "p50_ms": round(percentile(latencies, 0.50), 3),
            "p95_ms": round(percentile(latencies, 0.95), 3),
            "drops": drops,
            "overruns": overrun_cnts[-1] - overrun_cnts[0],
            "fps": round(len(headers) / (t1 - t0), 3),
        }
        os.makedirs(os.path.dirname(RESULT), exist_ok=True)
        tmp = RESULT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(result, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, RESULT)
        sys.stderr.write("[test_throughput_lane] %s (frame period %.1f ms)\n"
                         % (json.dumps(result, sort_keys=True), self.frame_period * 1000))

        self.assertTrue(all(s >= 1 for s in steps), "cpi_index not strictly increasing: %s" % cpis)
        self.assertEqual([c for c in overrun_cnts if c], [], "USB overrun counter (slot 102) non-zero")
        self.assertLessEqual(drops, max(1, len(headers) // 100), "too many frames dropped: %s" % result)
        self.assertLess(result["p95_ms"], 500.0 * self.frame_period,
                        "%s p95 %.1f ms >= 0.5 x frame period (%.1f ms)"
                        % (METRIC, result["p95_ms"], 1000.0 * self.frame_period))


if __name__ == "__main__":
    unittest.main()
