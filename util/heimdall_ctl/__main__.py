"""heimdall-ctl: command-line interface for HeIMDALL DAQ pipeline control."""
import argparse
import json
import sys
import time

from . import __version__
from .config import find_config, load_config, resolve_ports, get_num_channels
from .formatting import (
    parse_freq, format_freq, parse_duration, duration_since_ms,
    print_json, print_table, colorize, severity_color,
)
from .client.ctl import CtlClient
from .client.status import StatusClient


def _make_clients(args):
    cfg_path = find_config(args.config)
    cfg = load_config(cfg_path) if cfg_path else None
    instance = args.instance
    ports = resolve_ports(cfg, instance) if cfg else {
        "ctl": 5001, "status": 5002, "events": 5003}

    host = args.host or "127.0.0.1"
    ctl_port = args.port_ctl or ports["ctl"]
    status_port = args.port_status or ports["status"]
    events_port = args.port_events or ports["events"]
    timeout = args.timeout

    ctl = CtlClient(host, ctl_port, timeout)
    status = StatusClient(host, status_port, timeout)
    num_ch = get_num_channels(cfg) if cfg else 5
    return ctl, status, events_port, host, num_ch


def cmd_status(args):
    _, status, _, _, _ = _make_clients(args)
    if args.json:
        print_json(status.status())
        return
    data = status.status()
    if args.watch:
        try:
            while True:
                data = status.status()
                sys.stdout.write("\033[2J\033[H")
                _print_status(data, args.color)
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    else:
        _print_status(data, args.color)


def _print_status(data, color_mode):
    health = data.get("pipeline_health", "unknown")
    hc = {"ok": "green", "degraded": "yellow", "error": "red"}.get(health, "reset")
    # The status server publishes 'current_frequency_hz'; accept the legacy
    # 'rf_center_freq' key too so older/newer combinations keep working.
    freq = data.get("current_frequency_hz", data.get("rf_center_freq", 0))
    print(f"Pipeline: {colorize(health, hc, color_mode)}  "
          f"Sync: {data.get('sync_state', '?')}  "
          f"Freq: {format_freq(freq)}  "
          f"Uptime: {data.get('uptime_sec', 0):.0f}s")
    cal = data.get("calibration_status")
    if cal:
        print(f"Calibration: {cal}  "
              f"Aggregate power: {data.get('aggregate_power_db', 0):.1f} dB")
    lat = data.get("latency")
    if lat:
        print(f"Latency: avg={lat['avg_ms']:.1f}ms  p95={lat['p95_ms']:.1f}ms  max={lat['max_ms']:.1f}ms")
    tp = data.get("throughput")
    if tp:
        print(f"Throughput: avg={tp['avg_fps']:.1f}fps  max={tp['max_fps']:.1f}fps")
    counters = data.get("counters")
    if counters:
        drops = (counters.get("dropped_frames_iq", 0) +
                 counters.get("dropped_frames_hwc", 0))
        recent = data.get("recent_drops")
        line = f"Dropped frames (total): {drops}"
        if recent is not None:
            line += f"  (recent: {recent})"
        print(line)
    gains = data.get("if_gains")
    if gains:
        print(f"Gains: {gains}")


def _print_ctl_reply(reply, json_mode, ok_message="OK"):
    """Render a parsed 128-byte control reply (contract: 'FNSD' + optional
    NUL-padded JSON payload)."""
    parsed = CtlClient.parse_reply(reply)
    if json_mode:
        print_json(parsed)
        return
    if not parsed.get("ok"):
        print(f"Error: {parsed.get('error', 'bad reply')}")
        return
    data = parsed.get("data")
    if data is not None:
        print_json(data)
    elif parsed.get("raw"):
        print(parsed["raw"])
    else:
        print(ok_message)


def cmd_tune(args):
    ctl, _, _, _, _ = _make_clients(args)
    hz = parse_freq(args.frequency)
    ctl.freq(hz)
    print(f"Tuned to {format_freq(hz)}")


def cmd_gain(args):
    ctl, _, _, _, num_ch = _make_clients(args)
    if args.unified is not None:
        ctl.gain_unified(int(args.unified), num_ch)
        print(f"Gain set to {args.unified} on all {num_ch} channels")
    else:
        gains = [int(g) for g in args.values.split(",")]
        ctl.gain(gains)
        print(f"Gains set: {gains}")


def cmd_agc(args):
    ctl, _, _, _, _ = _make_clients(args)
    ctl.agc()
    print("AGC enabled")


def cmd_recal(args):
    ctl, _, _, _, _ = _make_clients(args)
    ctl.recal()
    print("Recalibration requested")


def _parse_per_channel(parser, values_str, num_ch, scale=1.0, unified=None):
    """Parse a comma-separated per-channel list, or fan out a unified value.

    Errors out (argparse usage error) when the value count does not match the
    channel count — the server unpacks a fixed num_ch values from the
    NUL-padded payload, so missing channels would silently be set to 0 — or
    when any value is negative (the wire format is unsigned uint32)."""
    if unified is not None:
        if float(unified) < 0:
            parser.error("value must be >= 0 (wire format is unsigned): {}".format(unified))
        return [int(round(float(unified) * scale))] * num_ch
    values = [float(v) for v in values_str.split(",")]
    if len(values) != num_ch:
        parser.error("expected {:d} per-channel values, got {:d}: {}".format(
            num_ch, len(values), values_str))
    if any(v < 0 for v in values):
        parser.error("values must be >= 0 (wire format is unsigned): {}".format(values_str))
    return [int(round(v * scale)) for v in values]


def cmd_lna_gain(args):
    ctl, _, _, _, num_ch = _make_clients(args)
    gains_tenths = _parse_per_channel(args._parser, args.values, num_ch, scale=10.0,
                                      unified=args.unified)
    reply = ctl.ext_gain(gains_tenths)
    _print_ctl_reply(reply, args.json,
                     "External LNA gains set: {} (dB x10)".format(gains_tenths))


def cmd_bias_tee(args):
    ctl, _, _, _, num_ch = _make_clients(args)
    if args.all is not None:
        states = [1 if args.all == "on" else 0] * num_ch
    else:
        states = [int(v) for v in args.values.split(",")]
        if len(states) != num_ch:
            args._parser.error("expected {:d} per-channel states, got {:d}: {}".format(
                num_ch, len(states), args.values))
        if any(s < 0 for s in states):
            args._parser.error("states must be >= 0 (wire format is unsigned): {}".format(
                args.values))
    reply = ctl.bias_tee(states)
    _print_ctl_reply(reply, args.json, "Bias-tee states set: {}".format(states))


def cmd_bearing(args):
    ctl, _, _, _, _ = _make_clients(args)
    reply = ctl.bearing(args.azimuth, args.elevation)
    _print_ctl_reply(reply, args.json,
                     f"Bearing commanded: az={args.azimuth:.2f} el={args.elevation:.2f}")


def cmd_park(args):
    ctl, _, _, _, _ = _make_clients(args)
    reply = ctl.park()
    _print_ctl_reply(reply, args.json, "Antenna parked")


def cmd_scan(args):
    ctl, _, _, _, _ = _make_clients(args)
    if args.action == "start":
        reply = ctl.scan_start()
        _print_ctl_reply(reply, args.json, "Scan-and-peak started")
    else:
        reply = ctl.orientation_stop()
        _print_ctl_reply(reply, args.json, "Rotator motion stopped")


def cmd_orientation(args):
    ctl, _, _, _, _ = _make_clients(args)
    reply = ctl.orientation_query()
    _print_ctl_reply(reply, args.json, "(no orientation data)")


def cmd_rf_budget(args):
    ctl, _, _, _, _ = _make_clients(args)
    reply = ctl.rf_query()
    _print_ctl_reply(reply, args.json, "(no link-budget data)")


def cmd_metrics(args):
    _, status, _, _, _ = _make_clients(args)
    data = status.metrics()
    if args.json:
        print_json(data)
        return
    if "error" in data:
        print(f"Error: {data['error']}")
        return
    rows = []
    for name, stats in data.items():
        if isinstance(stats, dict):
            rows.append([
                name,
                f"{stats.get('min', 0):.2f}",
                f"{stats.get('avg', 0):.2f}",
                f"{stats.get('p95', 0):.2f}",
                f"{stats.get('max', 0):.2f}",
                str(stats.get('count', 0)),
            ])
    print_table(["Metric", "Min", "Avg", "P95", "Max", "Count"], rows, args.color)


def cmd_events(args):
    if args.tail:
        from .client.events_sub import subscribe
        _, _, events_port, host, _ = _make_clients(args)
        topics = args.filter.split(",") if args.filter else None
        try:
            for evt in subscribe(host, events_port, topics):
                sev = evt.get("severity", "info")
                etype = evt.get("event_type", "?")
                ts = evt.get("timestamp", 0)
                payload = evt.get("payload", {})
                line = f"[{ts:.3f}] {etype}: {json.dumps(payload)}"
                print(colorize(line, severity_color(sev), args.color))
        except KeyboardInterrupt:
            pass
    else:
        _, status, _, _, _ = _make_clients(args)
        data = status.events()
        if args.json:
            print_json(data)
            return
        for evt in data.get("events", []):
            sev = evt.get("severity", "info")
            etype = evt.get("event_type", "?")
            ts = evt.get("timestamp", 0)
            print(colorize(f"[{ts:.3f}] {etype}: {evt.get('payload', {})}",
                           severity_color(sev), args.color))


def cmd_cal_history(args):
    from .client.db import open_db, cal_history
    since_ms = duration_since_ms(args.since) if args.since else None
    freq = parse_freq(args.freq) if args.freq else None
    db = open_db(args.db_dir)
    try:
        records = cal_history(db, since_ms=since_ms, freq=freq, limit=args.limit)
    finally:
        db.close()
    if args.json:
        print_json([r.__dict__ if hasattr(r, '__dict__') else r for r in records])
        return
    for r in records:
        print(r)


def cmd_freq_scan(args):
    from .client.db import open_db, freq_scan_summary
    db = open_db(args.db_dir)
    try:
        summary = freq_scan_summary(db)
    finally:
        db.close()
    if args.json:
        print_json([s.__dict__ if hasattr(s, '__dict__') else s for s in summary])
        return
    for s in summary:
        print(s)


def cmd_schedule(args):
    ctl, _, _, _, _ = _make_clients(args)
    action = args.action
    if action == "load":
        with open(args.file) as f:
            sched = json.load(f)
        ctl.schedule_load(sched)
        print("Schedule loaded")
    elif action == "stop":
        ctl.schedule_stop()
        print("Schedule stopped")
    elif action == "query":
        reply = ctl.schedule_query()
        _print_ctl_reply(reply, args.json, "(no schedule data)")
    elif action == "next":
        ctl.schedule_next()
        print("Skipped to next entry")


def cmd_config_show(args):
    cfg_path = find_config(args.config)
    if not cfg_path:
        print("No config file found")
        return
    cfg = load_config(cfg_path)
    print(f"Config: {cfg_path}")
    for section in cfg.sections():
        print(f"\n[{section}]")
        for key, val in cfg.items(section):
            print(f"  {key} = {val}")


def main():
    parser = argparse.ArgumentParser(
        prog="heimdall-ctl",
        description="HeIMDALL DAQ pipeline controller")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port-ctl", type=int, default=None)
    parser.add_argument("--port-status", type=int, default=None)
    parser.add_argument("--port-events", type=int, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--instance", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--color", default="auto", choices=["auto", "never", "always"])
    parser.add_argument("--timeout", type=float, default=5.0)

    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("status", help="Show pipeline status")
    p.add_argument("--watch", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("tune", help="Set center frequency")
    p.add_argument("frequency", help="e.g. 433M, 1.2G, 868000000")
    p.set_defaults(func=cmd_tune)

    p = sub.add_parser("gain", help="Set IF gains")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("values", nargs="?", help="Comma-separated per-channel gains")
    g.add_argument("--unified", type=int, help="Same gain for all channels")
    p.set_defaults(func=cmd_gain)

    p = sub.add_parser("agc", help="Enable automatic gain control")
    p.set_defaults(func=cmd_agc)

    p = sub.add_parser("recal", help="Force recalibration")
    p.set_defaults(func=cmd_recal)

    p = sub.add_parser("metrics", help="Show performance metrics")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("events", help="Show recent events")
    p.add_argument("--tail", action="store_true", help="Live event stream (ZMQ)")
    p.add_argument("--filter", default=None, help="Comma-separated event types")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("cal-history", help="Query calibration history from DB")
    p.add_argument("--since", default=None, help="Duration, e.g. 1h, 30m, 1d")
    p.add_argument("--freq", default=None, help="Filter by frequency")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--db-dir", default="_db")
    p.set_defaults(func=cmd_cal_history)

    p = sub.add_parser("freq-scan", help="Show per-frequency scan summary")
    p.add_argument("--db-dir", default="_db")
    p.set_defaults(func=cmd_freq_scan)

    p = sub.add_parser("schedule", help="Manage signal schedule")
    p.add_argument("action", choices=["load", "stop", "query", "next"])
    p.add_argument("file", nargs="?", help="JSON schedule file (for load)")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("lna-gain", help="Set external LNA gains (dB)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("values", nargs="?",
                   help="Comma-separated per-channel gains in dB (e.g. 14.5,14.5,14.5,14.5,14.5)")
    g.add_argument("--unified", type=float, help="Same gain (dB) for all channels")
    p.set_defaults(func=cmd_lna_gain)

    p = sub.add_parser("bias-tee", help="Set per-channel inline-LNA bias tees")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("values", nargs="?",
                   help="Comma-separated per-channel states (e.g. 1,0,1,1,1)")
    g.add_argument("--all", choices=["on", "off"], help="Same state for all channels")
    p.set_defaults(func=cmd_bias_tee)

    p = sub.add_parser("bearing", help="Slew the antenna to azimuth/elevation")
    p.add_argument("azimuth", type=float, help="Azimuth in degrees")
    p.add_argument("elevation", type=float, nargs="?", default=0.0,
                   help="Elevation in degrees (default 0)")
    p.set_defaults(func=cmd_bearing)

    p = sub.add_parser("park", help="Park the antenna at its configured bearing")
    p.set_defaults(func=cmd_park)

    p = sub.add_parser("scan", help="Autonomous scan-and-peak control")
    p.add_argument("action", choices=["start", "stop"])
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("orientation", help="Query rotator/orientation state")
    p.set_defaults(func=cmd_orientation)

    p = sub.add_parser("rf-budget", help="Query the RF link budget")
    p.set_defaults(func=cmd_rf_budget)

    p = sub.add_parser("config-show", help="Display resolved configuration")
    p.set_defaults(func=cmd_config_show)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args._parser = parser  # for usage errors raised inside command handlers
    args.func(args)


if __name__ == "__main__":
    main()
