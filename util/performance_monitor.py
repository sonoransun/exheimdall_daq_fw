#!/usr/bin/env python3
"""
HeIMDALL DAQ performance monitor (spec I.6).

* End-to-end latency is measured by a real IQ client: it performs the
  iq_server `streaming` handshake and the per-frame `IQDownload` ack on
  port 5000 + offset (--iq-port) and records, per received frame,
  latency = receive time (ms) - header.time_stamp (ms, stamped by rtl_daq or
  the synthesizer when the block was acquired).
  With [data_interface] iq_server_max_clients = 1 the monitor occupies the
  only client slot while it streams.
* Expected CPU affinity comes from the instance's host-registry entry
  (`cores`, spec B.3.1); without one from [host] cpu_cores, else from the
  start script's default layout (N*4 + k) % nproc.
* Real-time scheduling is read from /proc/<pid>/sched (`policy`:
  1 = SCHED_FIFO, 2 = SCHED_RR are real-time).
* Per-stage CPU / memory / context-switch metrics use psutil.

Project: HeIMDALL DAQ Firmware
License: GNU GPL V3
"""

import argparse
import configparser
import json
import os
import socket
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

try:
    import psutil
except ImportError:  # metrics need it; latency, affinity and RT checks do not
    psutil = None

UTIL_DIR = os.path.dirname(os.path.realpath(__file__))
FIRMWARE_DIR = os.path.join(os.path.dirname(UTIL_DIR), "Firmware")
for _p in (os.path.join(FIRMWARE_DIR, "_daq_core"), FIRMWARE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from iq_header import IQHeader  # noqa: E402
import capacity_model  # noqa: E402

#: Stage pid files written by the start scripts into _logs/inst<N>/pids/
STAGES = ("rtl_daq", "synthesizer", "rebuffer", "decimate", "delay_sync", "hw_controller", "iq_server")
#: Core tier of each stage (daq_start_sm.sh: T0 rtl_daq, T1 rebuffer+decimate,
#: T2 delay_sync+iq_server, T3 hw_controller)
STAGE_TIER = {"rtl_daq": 0, "synthesizer": 0, "rebuffer": 1, "decimate": 1, "delay_sync": 2,
              "iq_server": 2, "hw_controller": 3}
SCHED_POLICIES = {0: "SCHED_OTHER", 1: "SCHED_FIFO", 2: "SCHED_RR", 3: "SCHED_BATCH",
                  5: "SCHED_IDLE", 6: "SCHED_DEADLINE"}
RT_POLICIES = (1, 2)
HISTORY_LEN = 1000
DEFAULT_PORT_STRIDE = 100


def percentile(values, q):
    """Nearest-rank percentile (q in 0..100) of a non-empty sequence."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of an empty sequence")
    rank = max(1, int(-(-q * len(ordered) // 100)))
    return ordered[min(rank, len(ordered)) - 1]


def parse_sched(text):
    """{'policy', 'policy_name', 'rt', 'prio'} from /proc/<pid>/sched text;
    None when it carries no policy line."""
    fields = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if "policy" not in fields or not fields["policy"].lstrip("-").isdigit():
        return None
    policy = int(fields["policy"])
    prio = fields.get("prio")
    return {"policy": policy, "policy_name": SCHED_POLICIES.get(policy, "policy {0}".format(policy)),
            "rt": policy in RT_POLICIES,
            "prio": int(prio) if prio and prio.lstrip("-").isdigit() else None}


def iq_payload_bytes(header):
    """Payload length iq_server sends after a header (cf32: 2 x 32 bit)."""
    return header.cpi_length * header.active_ant_chs * 2 * header.sample_bit_depth // 8


class IQStreamClient(object):
    """A real iq_server client: `streaming` handshake, then one `IQDownload`
    ack per received frame (the stop-and-wait protocol of port 5000)."""

    RECV_CHUNK = 1 << 18

    def __init__(self, host="127.0.0.1", port=5000, timeout=10.0):
        self.host, self.port, self.timeout = host, int(port), float(timeout)
        self.sock = None
        self._buf = bytearray(self.RECV_CHUNK)
        self._started = False

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.sendall(b"streaming")
        self._started = False
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def _recv_exact(self, n, keep):
        out = bytearray(n) if keep else None
        view = memoryview(out) if keep else None
        got = 0
        while got < n:
            want = min(n - got, self.RECV_CHUNK)
            if keep:
                r = self.sock.recv_into(view[got:], want)
            else:
                r = self.sock.recv_into(self._buf, want)
            if r == 0:
                raise ConnectionError("IQ server closed the connection")
            got += r
        return out

    def next_frame(self):
        """Ack the previous frame (if any) and read the next one.

        Returns {'header': IQHeader, 'bytes': total frame bytes,
        'recv_ms': wall clock ms when the frame was complete}.
        """
        if self._started:
            self.sock.sendall(b"IQDownload")
        self._started = True
        header = IQHeader()
        header.decode_header(bytes(self._recv_exact(header.header_size, True)))
        if header.check_sync_word() != 0:
            raise ConnectionError("bad IQ header sync word 0x{0:08x}".format(header.sync_word))
        payload = iq_payload_bytes(header)
        self._recv_exact(payload, False)
        return {"header": header, "bytes": header.header_size + payload, "recv_ms": time.time() * 1000.0}


def frame_sample(frame):
    """One latency/throughput sample of a received frame."""
    h = frame["header"]
    return {"recv_ms": frame["recv_ms"], "cpi_index": h.cpi_index, "frame_type": h.frame_type,
            "bytes": frame["bytes"],
            "latency_ms": frame["recv_ms"] - h.time_stamp if h.time_stamp > 0 else None}


def summarize_samples(samples, elapsed_s):
    """frames, bytes, throughput (frames x bytes / elapsed), latency stats
    and cpi_index gaps of a list of frame_sample() dicts."""
    total = sum(s["bytes"] for s in samples)
    lat = [s["latency_ms"] for s in samples if s["latency_ms"] is not None]
    gaps = 0
    for prev, cur in zip(samples, samples[1:]):
        if cur["cpi_index"] > prev["cpi_index"] + 1:
            gaps += cur["cpi_index"] - prev["cpi_index"] - 1
    out = {"frames": len(samples), "bytes": total, "elapsed_s": elapsed_s,
           "throughput_MBps": total / elapsed_s / 1e6 if elapsed_s > 0 else None,
           "fps": len(samples) / elapsed_s if elapsed_s > 0 else None,
           "cpi_gaps": gaps, "latency_samples": len(lat)}
    if lat:
        out.update({"latency_p50_ms": percentile(lat, 50), "latency_p95_ms": percentile(lat, 95),
                    "latency_max_ms": max(lat), "latency_mean_ms": sum(lat) / len(lat)})
    return out


def measure_stream(host, port, frames=None, duration=None, timeout=10.0, stop_event=None, sink=None):
    """Stream through a real client until *frames* frames or *duration*
    seconds (or *stop_event*); returns summarize_samples() + 'samples'.
    *sink* (callable) receives every sample as it arrives."""
    if frames is None and duration is None and stop_event is None:
        raise ValueError("give frames, duration or stop_event")
    samples = []
    with IQStreamClient(host, port, timeout) as client:
        start = time.monotonic()
        while True:
            if frames is not None and len(samples) >= frames:
                break
            if duration is not None and time.monotonic() - start >= duration:
                break
            if stop_event is not None and stop_event.is_set():
                break
            sample = frame_sample(client.next_frame())
            samples.append(sample)
            if sink is not None:
                sink(sample)
        elapsed = time.monotonic() - start
    out = summarize_samples(samples, elapsed)
    out["samples"] = samples
    return out


def _read(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


class DAQPerformanceMonitor(object):
    def __init__(self, instance_id=0, firmware_dir=None, run_dirs=None, proc_root="/proc"):
        self.instance_id = int(instance_id)
        self.firmware_dir = firmware_dir or os.getcwd()
        self.pid_dir = os.path.join(self.firmware_dir, "_logs", "inst{0}".format(self.instance_id), "pids")
        self.run_dirs = run_dirs
        self.proc_root = proc_root
        self.metrics_history = defaultdict(lambda: deque(maxlen=HISTORY_LEN))
        self.running = False
        self.start_time = None
        self.processes = {}      # stage -> pid
        self._psutil_procs = {}  # stage -> psutil.Process
        self._latency_thread = None
        self._latency_stop = threading.Event()
        self._latency_error = None

    # -- discovery ---------------------------------------------------------

    def discover_daq_processes(self, quiet=False):
        """Read the stage pid files of this instance (live pids only)."""
        self.processes = {}
        for name in STAGES:
            text = _read(os.path.join(self.pid_dir, name + ".pid"))
            if text is None:
                continue
            try:
                pid = int(text.strip())
                os.kill(pid, 0)
            except ValueError:
                print("Bad pid file for {0}".format(name))
                continue
            except ProcessLookupError:
                if not quiet:
                    print("Warning: PID {0} of {1} is not running".format(text.strip(), name))
                continue
            except PermissionError:
                pass
            self.processes[name] = pid
            if not quiet:
                print("Found {0}: PID {1}".format(name, pid))
        return self.processes

    def _config(self):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(os.path.join(self.firmware_dir, "daq_chain_config.ini"))
        except configparser.Error:
            return configparser.ConfigParser(interpolation=None)
        return parser

    def registry_entry(self):
        """This instance's live host-registry entry, or None."""
        try:
            import daq_preflight
        except ImportError:
            return None
        dirs = self.run_dirs if self.run_dirs is not None else daq_preflight.run_dirs(self.firmware_dir)
        for entry in daq_preflight.read_live_entries(dirs, self.proc_root):
            if entry.get("instance_id") == self.instance_id:
                return entry
        return None

    def expected_layout(self):
        """{'source', 'cores', 'delay_sync_cores', 'rt_priority_offset',
        'nproc', 'cpu_cores_set'}: the registry entry wins, then [host]
        cpu_cores, then the default."""
        cfg = self._config()
        ds = [int(x) for x in cfg.get("host", "delay_sync_cores", fallback="").split(",") if x.strip().isdigit()]
        off_text = cfg.get("host", "rt_priority_offset", fallback="0").strip()
        offset = int(off_text) if off_text.isdigit() else 0
        nproc = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
        items = [x.strip() for x in cfg.get("host", "cpu_cores", fallback="").split(",") if x.strip()]
        cpu_cores_set = len(items) == 4 and all(x.isdigit() for x in items) and len(set(items)) == 4
        base = {"delay_sync_cores": ds, "rt_priority_offset": offset, "nproc": nproc,
                "cpu_cores_set": cpu_cores_set}
        entry = self.registry_entry()
        if entry and isinstance(entry.get("cores"), list) and len(entry["cores"]) == 4:
            return dict(base, source="registry", cores=[int(c) for c in entry["cores"]],
                        rt_priority_offset=int(entry.get("rt_priority_offset", offset)))
        if cpu_cores_set:
            return dict(base, source="config", cores=[int(x) for x in items])
        return dict(base, source="default", cores=capacity_model.default_cores(self.instance_id, nproc))

    def expected_affinity(self, layout=None):
        """{stage: [cores]} as the start script pins them; None for a stage it
        leaves unpinned. Without [host] cpu_cores, daq_start_sm.sh runs
        delay_sync on CORE1 below 3 CPUs and leaves hw_controller and
        iq_server unpinned below 4 (daq_synthetic_start.sh --rt always pins
        the four tiers)."""
        layout = layout or self.expected_layout()
        nproc = layout.get("nproc") or capacity_model.CORES_PER_HW_INSTANCE
        hw_fallback = not layout.get("cpu_cores_set") and "synthesizer" not in self.processes \
            and nproc < capacity_model.CORES_PER_HW_INSTANCE
        out = {}
        for stage, tier in STAGE_TIER.items():
            if stage == "delay_sync" and layout["delay_sync_cores"]:
                out[stage] = sorted(set(layout["delay_sync_cores"]))
            elif hw_fallback and stage in ("hw_controller", "iq_server"):
                out[stage] = None
            elif hw_fallback and stage == "delay_sync" and nproc < 3:
                out[stage] = [layout["cores"][1]]
            else:
                out[stage] = [layout["cores"][tier]]
        return out

    # -- checks ------------------------------------------------------------

    def chain_pinned(self):
        """False for a synthetic chain started without --rt: daq_synthetic_start.sh
        then neither pins nor RT-schedules its stages, so there is no
        expected layout."""
        if "synthesizer" not in self.processes or "rtl_daq" in self.processes:
            return True
        info = self.read_sched(self.processes["synthesizer"])
        return bool(info and info.get("rt"))

    def validate_cpu_affinity(self):
        """[{'process', 'expected', 'actual'}] for stages pinned elsewhere
        (none for an unpinned synthetic chain)."""
        if not hasattr(os, "sched_getaffinity") or not self.chain_pinned():
            return []
        layout = self.expected_layout()
        expected = self.expected_affinity(layout)
        violations = []
        for name, pid in self.processes.items():
            try:
                actual = sorted(os.sched_getaffinity(pid))
            except OSError:
                continue
            if expected[name] is not None and actual != expected[name]:
                violations.append({"process": name, "expected": expected[name], "actual": actual,
                                   "source": layout["source"]})
        return violations

    def read_sched(self, pid):
        text = _read(os.path.join(self.proc_root, str(pid), "sched"))
        info = parse_sched(text) if text else None
        if info is None and hasattr(os, "sched_getscheduler"):
            try:
                policy = os.sched_getscheduler(pid)
            except OSError:
                return None
            info = {"policy": policy, "policy_name": SCHED_POLICIES.get(policy, str(policy)),
                    "rt": policy in RT_POLICIES, "prio": None}
        if info is not None and hasattr(os, "sched_getparam"):
            try:
                info["rt_priority"] = os.sched_getparam(pid).sched_priority
            except OSError:
                info["rt_priority"] = None
        return info

    def check_realtime_status(self):
        """RT throttling and per-stage policy/priority vs the expected tiers."""
        runtime = _read(os.path.join(self.proc_root, "sys", "kernel", "sched_rt_runtime_us"))
        status = {"rt_runtime_unlimited": None if runtime is None else runtime.strip() == "-1"}
        expected = capacity_model.rt_priorities(self.expected_layout()["rt_priority_offset"])
        per_stage = {}
        for name, pid in self.processes.items():
            info = self.read_sched(pid)
            if info is None:
                per_stage[name] = {"policy_name": "UNKNOWN", "rt": None}
                continue
            info["expected_rt_priority"] = expected.get(name)
            per_stage[name] = info
        status["process_scheduling"] = per_stage
        return status

    def measure_end_to_end_latency(self, host, port, frames=20, timeout=10.0):
        """Stream *frames* frames through a real client; summary dict."""
        out = measure_stream(host, port, frames=frames, timeout=timeout)
        out.pop("samples")
        return out

    def collect_per_stage_metrics(self):
        if psutil is None:
            raise RuntimeError("psutil is required for per-stage metrics (pip install psutil)")
        metrics = {}
        for name, pid in self.processes.items():
            try:
                proc = self._psutil_procs.get(name)
                if proc is None or proc.pid != pid:
                    proc = self._psutil_procs[name] = psutil.Process(pid)
                mem = proc.memory_info()
                ctx = proc.num_ctx_switches()
                m = {"cpu_percent": proc.cpu_percent(), "memory_rss_mb": mem.rss / (1024 * 1024),
                     "memory_vms_mb": mem.vms / (1024 * 1024), "num_threads": proc.num_threads(),
                     "ctx_switches_voluntary": ctx.voluntary, "ctx_switches_involuntary": ctx.involuntary}
                if hasattr(os, "sched_getaffinity"):
                    try:
                        m["cpu_affinity"] = sorted(os.sched_getaffinity(pid))
                    except OSError:
                        pass
                metrics[name] = m
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                metrics[name] = {"error": str(e)}
        return metrics

    def check_memory_pressure(self):
        if psutil is None:
            raise RuntimeError("psutil is required for the memory check (pip install psutil)")
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        indicators = {}
        if memory.percent > 80:
            indicators["high_memory_usage"] = memory.percent
        if swap.percent > 10:
            indicators["swap_usage"] = swap.percent
        text = _read("/var/log/kern.log")
        if text:
            oom = [line for line in text.splitlines()[-100:] if "Out of memory" in line]
            if oom:
                indicators["oom_kills"] = len(oom)
        return {"memory_total_gb": memory.total / (1024 ** 3), "memory_used_percent": memory.percent,
                "swap_used_percent": swap.percent, "pressure_indicators": indicators}

    # -- continuous monitoring ---------------------------------------------

    def _latency_worker(self, host, port):
        try:
            measure_stream(host, port, stop_event=self._latency_stop,
                           sink=lambda s: self.metrics_history["latency"].append((s["recv_ms"] / 1000.0, s)))
        except (OSError, ConnectionError) as e:
            self._latency_error = str(e)

    def latency_summary(self):
        samples = [s for _, s in self.metrics_history["latency"]]
        if not samples:
            return None
        elapsed = (samples[-1]["recv_ms"] - samples[0]["recv_ms"]) / 1000.0 if len(samples) > 1 else 0.0
        return summarize_samples(samples, elapsed)

    def start_monitoring(self, interval=1.0, duration=None, iq_host=None, iq_port=None, summary_every=10.0):
        print("Starting HeIMDALL performance monitoring (instance {0})".format(self.instance_id))
        print("Monitoring interval: {0}s".format(interval))
        if duration:
            print("Duration: {0}s".format(duration))
        self.discover_daq_processes()
        if not self.processes:
            print("No DAQ processes found! Make sure the DAQ is running.")
            return
        if iq_port:
            print("Measuring latency through an IQ client on {0}:{1}".format(iq_host, iq_port))
            self._latency_stop.clear()
            self._latency_thread = threading.Thread(target=self._latency_worker, args=(iq_host, iq_port),
                                                    daemon=True)
            self._latency_thread.start()
        self.running = True
        self.start_time = time.time()
        next_summary = self.start_time + summary_every
        try:
            while self.running:
                now = time.time()
                self.metrics_history["stage_metrics"].append((now, self.collect_per_stage_metrics()))
                self.metrics_history["memory"].append((now, self.check_memory_pressure()))
                self.metrics_history["rt_status"].append((now, self.check_realtime_status()))
                if now >= next_summary:
                    self.print_summary()
                    next_summary = now + summary_every
                if duration and now - self.start_time >= duration:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nMonitoring stopped by user")
        finally:
            self.running = False
            self._latency_stop.set()
            if self._latency_thread is not None:
                self._latency_thread.join(timeout=15.0)
            self.print_summary()

    def print_summary(self):
        if not self.metrics_history["stage_metrics"]:
            return
        print("\n{0}\nPerformance Summary - {1}\n{0}".format("=" * 60, datetime.now().strftime("%H:%M:%S")))
        _, latest = self.metrics_history["stage_metrics"][-1]
        for name, m in latest.items():
            if "error" in m:
                continue
            print("\n{0}:".format(name.upper()))
            print("  CPU: {0:.1f}%".format(m["cpu_percent"]))
            print("  Memory: {0:.1f}MB RSS".format(m["memory_rss_mb"]))
            print("  Threads: {0}".format(m["num_threads"]))
            if "cpu_affinity" in m:
                print("  CPU Affinity: {0}".format(m["cpu_affinity"]))
        if self.metrics_history["memory"]:
            _, mem = self.metrics_history["memory"][-1]
            print("\nSYSTEM MEMORY:")
            print("  Used: {0:.1f}%".format(mem["memory_used_percent"]))
            print("  Swap: {0:.1f}%".format(mem["swap_used_percent"]))
            if mem["pressure_indicators"]:
                print("  Pressure indicators: {0}".format(mem["pressure_indicators"]))
        lat = self.latency_summary()
        if lat:
            print("\nIQ CLIENT ({0} frames):".format(lat["frames"]))
            if lat.get("latency_samples"):
                print("  Latency p50/p95/max: {0:.1f} / {1:.1f} / {2:.1f} ms".format(
                    lat["latency_p50_ms"], lat["latency_p95_ms"], lat["latency_max_ms"]))
            if lat["throughput_MBps"] is not None:
                print("  Throughput: {0:.2f} MB/s, {1:.2f} frames/s, cpi gaps {2}".format(
                    lat["throughput_MBps"], lat["fps"], lat["cpi_gaps"]))
        elif self._latency_error:
            print("\nIQ CLIENT: {0}".format(self._latency_error))
        for v in self.validate_cpu_affinity():
            print("CPU AFFINITY VIOLATION: {0}: expected {1}, actual {2} ({3})".format(
                v["process"], v["expected"], v["actual"], v["source"]))

    def export_metrics(self, output_file):
        data = {
            "metadata": {"instance_id": self.instance_id, "start_time": self.start_time,
                         "duration": time.time() - self.start_time if self.start_time else 0,
                         "export_time": time.time()},
            "metrics": {key: list(self.metrics_history[key])
                        for key in ("stage_metrics", "latency", "memory", "rt_status")},
            "latency_summary": self.latency_summary(),
        }
        with open(output_file, "w") as f:
            json.dump(data, f, indent=2)
        print("Metrics exported to {0}".format(output_file))


def _iq_port_for(firmware_dir, instance_id):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(os.path.join(firmware_dir, "daq_chain_config.ini"))
    except configparser.Error:
        pass
    stride = parser.get("federation", "port_stride", fallback=str(DEFAULT_PORT_STRIDE)).strip()
    return 5000 + int(instance_id) * (int(stride) if stride.isdigit() else DEFAULT_PORT_STRIDE)


def main(argv=None):
    p = argparse.ArgumentParser(description="HeIMDALL DAQ performance monitor")
    p.add_argument("--instance-id", type=int, default=0, help="DAQ instance ID to monitor")
    p.add_argument("--firmware-dir", default=None,
                   help="directory with daq_chain_config.ini and _logs/ (default: current directory)")
    p.add_argument("--run-dir", default=None, help="host registry directory (default: $HEIMDALL_RUN_DIR, "
                                                   "/run/heimdall, /tmp/heimdall-run-<uid>)")
    p.add_argument("--interval", type=float, default=1.0, help="monitoring interval in seconds")
    p.add_argument("--duration", type=float, help="monitoring duration in seconds")
    p.add_argument("--export", help="export metrics to a JSON file")
    p.add_argument("--iq-host", default="127.0.0.1", help="iq_server host for latency (default 127.0.0.1)")
    p.add_argument("--iq-port", default=None,
                   help="stream through a real IQ client on this port ('auto' = 5000 + N * port_stride) "
                        "and record latency = receive time - header time_stamp")
    p.add_argument("--latency-frames", type=int, metavar="N",
                   help="only measure N frames through the IQ client, print the summary and exit")
    p.add_argument("--check-affinity", action="store_true", help="check CPU affinity only")
    p.add_argument("--check-rt", action="store_true", help="check real-time scheduling only")
    p.add_argument("--check-memory", action="store_true", help="check memory pressure only")
    p.add_argument("--json", action="store_true", help="JSON output for the --check-*/--latency-frames modes")
    args = p.parse_args(argv)

    firmware_dir = args.firmware_dir or os.getcwd()
    monitor = DAQPerformanceMonitor(args.instance_id, firmware_dir,
                                    [args.run_dir] if args.run_dir else None)
    iq_port = None
    if args.iq_port is not None:
        iq_port = _iq_port_for(firmware_dir, args.instance_id) if args.iq_port == "auto" else int(args.iq_port)

    def emit(obj, text):
        print(json.dumps(obj, indent=2) if args.json else text)

    if args.latency_frames is not None:
        if iq_port is None:
            iq_port = _iq_port_for(firmware_dir, args.instance_id)
        try:
            summary = monitor.measure_end_to_end_latency(args.iq_host, iq_port, args.latency_frames)
        except (OSError, ConnectionError) as e:
            print("Error: IQ client on {0}:{1}: {2}".format(args.iq_host, iq_port, e), file=sys.stderr)
            return 1
        lines = ["{0}: {1}".format(k, v) for k, v in summary.items()]
        emit(summary, "\n".join(lines))
        return 0
    if args.check_affinity or args.check_rt:
        monitor.discover_daq_processes(quiet=args.json)
        result = {}
        if args.check_affinity:
            result["affinity_violations"] = monitor.validate_cpu_affinity()
            result["expected_layout"] = monitor.expected_layout()
        if args.check_rt:
            result["rt_status"] = monitor.check_realtime_status()
        lines = []
        if args.check_affinity:
            v = result["affinity_violations"]
            result["pinned"] = monitor.chain_pinned()
            if result["pinned"]:
                lines.append("Expected cores ({0}): {1}".format(result["expected_layout"]["source"],
                                                                result["expected_layout"]["cores"]))
            else:
                lines.append("Synthetic chain without --rt: stages are not pinned")
            lines += ["  {0}: expected {1}, actual {2}".format(x["process"], x["expected"], x["actual"]) for x in v]
            lines.append("CPU affinity violations: {0}".format(len(v)) if v
                         else "All processes have correct CPU affinity")
        if args.check_rt:
            rt = result["rt_status"]
            lines.append("RT runtime unlimited: {0}".format(rt["rt_runtime_unlimited"]))
            for name, info in rt["process_scheduling"].items():
                lines.append("  {0}: {1} rt_priority {2} (expected {3})".format(
                    name, info.get("policy_name"), info.get("rt_priority"), info.get("expected_rt_priority")))
        emit(result, "\n".join(lines))
        return 1 if result.get("affinity_violations") else 0
    if args.check_memory:
        mem = monitor.check_memory_pressure()
        emit(mem, "Memory Status:\n  Total: {0:.1f}GB\n  Used: {1:.1f}%\n  Swap: {2:.1f}%{3}".format(
            mem["memory_total_gb"], mem["memory_used_percent"], mem["swap_used_percent"],
            "\n  Pressure: {0}".format(mem["pressure_indicators"]) if mem["pressure_indicators"] else ""))
        return 0
    if psutil is None:
        print("Error: psutil is required for continuous monitoring (pip install psutil)", file=sys.stderr)
        return 1
    try:
        monitor.start_monitoring(args.interval, args.duration, args.iq_host, iq_port)
    finally:
        if args.export:
            monitor.export_metrics(args.export)
    return 0


if __name__ == "__main__":
    sys.exit(main())
