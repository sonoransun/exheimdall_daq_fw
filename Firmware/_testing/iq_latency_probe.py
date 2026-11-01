"""
    Description :
    IQ client probe: connects to a :5000 IQ stream (iq_server.out, an IQ
    router output or iq_replay.py --serve-port) exactly like a real client -
    the "streaming" handshake, then one "IQDownload" request per frame - and
    reports what that client experiences:

      frames, frames per second, bytes and throughput in MB/s (10^6 bytes/s);
      latency_ms: receive wall time minus the header time_stamp (Unix ms,
        stamped by rtl_daq at block capture), over frames with a time_stamp;
      latency_minus_frame_ms: the same minus the frame's CPI duration
        cpi_length / sampling_freq (the part of the latency not explained by
        acquiring the CPI itself);
      cpi_index continuity: gaps (forward jumps), the frames they skip, and
        resets (cpi_index not increasing, e.g. a chain restart or a looped
        replay);
      the slot-102 USB overrun counter (IQH_RSV_BUFFER_OVERRUN_CNT) delta
        between the first and the last frame;
      frame types and the sync flags of the last frame.

    Latency across hosts is only as good as the clock synchronisation
    between them (NTP/PTP/chrony).

    Usage (from Firmware/):
        python3 _testing/iq_latency_probe.py --host 127.0.0.1 --port 5000 --duration 60 --json
        python3 _testing/iq_latency_probe.py --instance 1 --port-stride 100 --duration 60

    Exit codes: 0 frames received, 1 connection/protocol error or no frame
    received, 2 usage error.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import argparse
import json
import math
import os
import socket
import sys
import time

currentPath = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, currentPath)
import iq_capture_format as hcap  # noqa: E402  (also puts _daq_core on sys.path)
from iq_header import IQHeader  # noqa: E402

BASE_PORT = 5000
U32 = 1 << 32


def compute_port(base, instance_id, stride):
    """base + instance_id * stride (sh_mem_util.c compute_port)."""
    return base + instance_id * stride


def _percentile(sorted_values, q):
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * q
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_values[int(k)]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def summarize(values):
    """min/mean/p50/p95/max/count of a list of numbers (None entries when empty)."""
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "min": None, "mean": None, "p50": None, "p95": None, "max": None}
    return {"count": len(ordered), "min": round(ordered[0], 3),
            "mean": round(sum(ordered) / len(ordered), 3),
            "p50": round(_percentile(ordered, 0.50), 3),
            "p95": round(_percentile(ordered, 0.95), 3),
            "max": round(ordered[-1], 3)}


class ProbeStats:
    """Accumulates the per-frame observations of one probe run."""

    def __init__(self):
        self.frames = 0
        self.bytes = 0
        self.types = {"data": 0, "cal": 0, "dummy": 0, "other": 0}
        self.latency_ms = []
        self.latency_minus_frame_ms = []
        self.cpi_gaps = 0
        self.cpi_missing = 0
        self.cpi_resets = 0
        self.first_cpi = None
        self.last_cpi = None
        self.overrun_first = None
        self.overrun_last = None
        self.last_header = None

    def add(self, header, nbytes, rx_wall_ms):
        self.frames += 1
        self.bytes += nbytes
        key = {IQHeader.FRAME_TYPE_DATA: "data", IQHeader.FRAME_TYPE_CAL: "cal",
               IQHeader.FRAME_TYPE_DUMMY: "dummy"}.get(header.frame_type, "other")
        self.types[key] += 1
        if header.time_stamp:
            latency = rx_wall_ms - header.time_stamp
            self.latency_ms.append(latency)
            if header.frame_type != IQHeader.FRAME_TYPE_DUMMY and header.cpi_length and header.sampling_freq:
                self.latency_minus_frame_ms.append(
                    latency - 1000.0 * header.cpi_length / header.sampling_freq)
        cpi = header.cpi_index
        if self.last_cpi is None:
            self.first_cpi = cpi
        else:
            step = (cpi - self.last_cpi) % U32
            if step == 0 or step >= U32 // 2:
                self.cpi_resets += 1
            elif step > 1:
                self.cpi_gaps += 1
                self.cpi_missing += step - 1
        self.last_cpi = cpi
        overruns = header.get_buffer_overrun_cnt()
        if self.overrun_first is None:
            self.overrun_first = overruns
        self.overrun_last = overruns
        self.last_header = header

    def report(self, elapsed_s, host, port):
        elapsed_s = max(elapsed_s, 1e-9)
        last = self.last_header
        return {
            "host": host, "port": port,
            "duration_s": round(elapsed_s, 3),
            "frames": self.frames,
            "frame_types": dict(self.types),
            "bytes": self.bytes,
            "fps": round(self.frames / elapsed_s, 3),
            "throughput_mbps": round(self.bytes / elapsed_s / 1e6, 3),
            "latency_ms": summarize(self.latency_ms),
            "latency_minus_frame_ms": summarize(self.latency_minus_frame_ms),
            "cpi_index_first": self.first_cpi,
            "cpi_index_last": self.last_cpi,
            "cpi_index_gaps": self.cpi_gaps,
            "cpi_index_missing": self.cpi_missing,
            "cpi_index_resets": self.cpi_resets,
            "overrun_first": self.overrun_first,
            "overrun_last": self.overrun_last,
            "overrun_delta": (None if self.overrun_first is None
                              else (self.overrun_last - self.overrun_first) % U32),
            "last_frame": None if last is None else {
                "frame_type": last.frame_type, "channels": last.active_ant_chs,
                "cpi_length": last.cpi_length, "sampling_freq": last.sampling_freq,
                "rf_center_freq": last.rf_center_freq, "sync_state": last.sync_state,
                "delay_sync_flag": last.delay_sync_flag, "iq_sync_flag": last.iq_sync_flag},
        }


def probe(host, port, duration_s=10.0, max_frames=0, timeout_s=10.0, clock=time.time):
    """Run one probe; returns (report dict, error string or None). The run
    ends after duration_s, after max_frames frames (> 0) or when the stream
    ends; the frame that crosses the duration is still counted."""
    stats = ProbeStats()
    error = None
    sock = socket.create_connection((host, port), timeout=timeout_s)
    t_start = time.monotonic()
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        request = b"streaming"
        while True:
            sock.sendall(request)
            request = b"IQDownload"
            _, header, payload = hcap.recv_wire_frame(sock)
            stats.add(header, hcap.HEADER_SIZE + len(payload), clock() * 1000.0)
            if max_frames and stats.frames >= max_frames:
                break
            if time.monotonic() - t_start >= duration_s:
                break
    except (OSError, hcap.HcapError) as err:  # ConnectionError is an OSError
        error = "{}: {}".format(type(err).__name__, err)
    finally:
        elapsed = time.monotonic() - t_start
        try:
            sock.sendall(b"q")  # iq_server closes a session on any non-ack request
        except OSError:
            pass
        sock.close()
    return stats.report(elapsed, host, port), error


def format_report(rep, error):
    lat = rep["latency_ms"]
    exc = rep["latency_minus_frame_ms"]
    lines = ["IQ probe {}:{:d}: {:d} frames in {:.1f} s ({:.2f} fps), {:.2f} MB/s".format(
        rep["host"], rep["port"], rep["frames"], rep["duration_s"], rep["fps"], rep["throughput_mbps"])]
    types = rep["frame_types"]
    lines.append("  frame types : data {data:d}, cal {cal:d}, dummy {dummy:d}, other {other:d}".format(**types))
    if lat["count"]:
        lines.append("  latency_ms  : p50 {p50:.1f}  p95 {p95:.1f}  max {max:.1f}  (n={count:d})".format(**lat))
    else:
        lines.append("  latency_ms  : n/a (no header time_stamp)")
    if exc["count"]:
        lines.append("  minus frame : p50 {p50:.1f}  p95 {p95:.1f}  max {max:.1f}".format(**exc))
    lines.append("  cpi_index   : {} .. {}, gaps {:d} ({:d} frames missing), resets {:d}".format(
        rep["cpi_index_first"], rep["cpi_index_last"], rep["cpi_index_gaps"],
        rep["cpi_index_missing"], rep["cpi_index_resets"]))
    lines.append("  overruns    : delta {}".format(rep["overrun_delta"]))
    if error:
        lines.append("  stopped     : {}".format(error))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measure a :5000 IQ stream as a real client")
    parser.add_argument("--host", default="127.0.0.1", help="server address (default: %(default)s)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--port", type=int, help="server TCP port (default: 5000)")
    target.add_argument("--instance", type=int, metavar="N",
                        help="federation instance: port 5000 + N * stride")
    parser.add_argument("--port-stride", type=int, default=100, metavar="S",
                        help="port stride for --instance (default: %(default)s)")
    parser.add_argument("--duration", type=float, default=10.0,
                        help="measurement duration in seconds (default: %(default)s)")
    parser.add_argument("--frames", type=int, default=0, help="stop after N frames (0 = no limit)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="connect / per-frame receive timeout in seconds (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)

    if args.instance is not None:
        if args.instance < 0 or args.port_stride < 1:
            parser.error("--instance must be >= 0 and --port-stride >= 1")
        port = compute_port(BASE_PORT, args.instance, args.port_stride)
    else:
        port = BASE_PORT if args.port is None else args.port
    if not 0 < port <= 65535:
        parser.error("port {:d} is out of range".format(port))
    if args.duration <= 0 or args.timeout <= 0 or args.frames < 0:
        parser.error("--duration and --timeout must be > 0, --frames >= 0")

    try:
        rep, error = probe(args.host, port, args.duration, args.frames, args.timeout)
    except OSError as err:
        rep, error = None, "cannot connect to {}:{:d}: {}".format(args.host, port, err)
    if rep is None:
        if args.json:
            print(json.dumps({"host": args.host, "port": port, "frames": 0, "error": error}))
        else:
            print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    rep["error"] = error
    if args.json:
        print(json.dumps(rep, sort_keys=True))
    else:
        print(format_report(rep, error))
    # A stream that ends after delivering frames (a replay without --loop)
    # still measured something; no frame at all is a failure
    return 0 if rep["frames"] else 1


if __name__ == "__main__":
    sys.exit(main())
