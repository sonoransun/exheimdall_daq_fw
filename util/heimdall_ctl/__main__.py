"""heimdall-ctl: command-line interface for HeIMDALL DAQ pipeline control.

Exit codes (every subcommand):
  0  success, applied, or sent to a legacy server that does not confirm
  1  the reply reported an error / applied:false; --verify not observed;
     (fleet) any target not applied, timed out or failed
  2  usage error
  3  connection failure / timeout to the addressed server
  4  not_leader after one redirect
  5  capacity: the pre-flight tool reported failures
"""
import argparse
import json
import struct
import sys
import time

from . import __version__
from .config import (
    find_config, load_config, resolve_ports, get_num_channels, config_instance_id,
    config_port_stride, coordinator_endpoints, router_endpoint, auth_token,
    firmware_dir, service_ports,
)
from .formatting import (
    parse_freq, format_freq, parse_duration,
    print_json, print_table, colorize, severity_color,
    format_ts, format_age, format_ack, ack_exit_code,
)
from .client.ctl import (
    CtlClient, EndpointError, XackRefused, ScheduleTransferError,
    LEGACY_ACK_WARNING, QUERY_TIMEOUT_SEC, PAYLOAD_SIZE, MUTATING_VERBS,
)
from .client.status import StatusClient
from .client.federation import (
    CoordinatorClient, CoordinatorError, NotLeader, format_result, summarize, summary_line,
    results_exit_code, VERSION_TIMEOUT_SEC,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONN = 3
EXIT_NOT_LEADER = 4
EXIT_CAPACITY = 5

VERIFY_POLL_SEC = 0.5
SYNC_LOCKED = 5

RECAL_FALLBACK_WARNING = (
    "warning: server has wire recalibration disabled or lacks RECL: INIT only "
    "re-enters the hw_controller FSM and does not recalibrate a locked chain")
NO_PEERS_TEXT = ("this instance does not serve PEERS "
                 "(set [monitoring] status_extended = 1)")
CAL_HISTORY_IGNORED = "ignored: server lacks the extended CAL_HISTORY grammar"


class Failed(Exception):
    """A command failed with a message and an exit code (nothing more to send)."""

    def __init__(self, message, code=EXIT_FAIL):
        self.code = code
        super().__init__(message)


def _warn(msg):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# Resolved context (config, addressing, clients)
# --------------------------------------------------------------------------

class Context:
    def __init__(self, args):
        self.args = args
        self.cfg_path = find_config(args.config)
        self.cfg = load_config(self.cfg_path) if self.cfg_path else None
        self.instance = args.instance if args.instance is not None \
            else config_instance_id(self.cfg)
        self.stride = args.port_stride if args.port_stride is not None \
            else config_port_stride(self.cfg)
        self.host = args.host or "127.0.0.1"
        ports = resolve_ports(self.cfg, self.instance, self.stride)
        self.ctl_port = args.port_ctl or ports["ctl"]
        self.status_port = args.port_status or ports["status"]
        self.events_port = args.port_events or ports["events"]
        self.log("config {}; instance {:d}, stride {:d}; ctl {}:{:d}, status {}:{:d}, "
                 "events {}:{:d}".format(self.cfg_path or "(none)", self.instance,
                                         self.stride, self.host, self.ctl_port, self.host,
                                         self.status_port, self.host, self.events_port))

    def log(self, msg):
        if self.args.verbose:
            print("heimdall-ctl: " + msg, file=sys.stderr)

    def ctl(self):
        return CtlClient(self.host, self.ctl_port, timeout=self.args.timeout,
                         ack=self.args.ack)

    def status(self, timeout=None):
        if timeout is None:
            timeout = self.args.timeout if self.args.timeout is not None else QUERY_TIMEOUT_SEC
        return StatusClient(self.host, self.status_port, timeout)

    def coordinator(self):
        endpoints = coordinator_endpoints(self.cfg, self.args.coordinator)
        token = auth_token(self.cfg, self.args.auth_token)
        self.log("coordinators {}; auth {}".format(
            ",".join("{}:{}".format(h, p) for h, p in endpoints), "on" if token else "off"))
        return CoordinatorClient(endpoints, token, timeout=self.args.timeout, log=self.log)

    def firmware_dir(self):
        return firmware_dir(self.cfg_path, self.args.firmware_dir)


# --------------------------------------------------------------------------
# Value parsing
# --------------------------------------------------------------------------

def _parse_numbers(parser, text, cast, what):
    try:
        values = [cast(v) for v in text.split(",") if v.strip() != ""]
    except ValueError:
        parser.error("bad {}: {}".format(what, text))
    if not values:
        parser.error("no {} given".format(what))
    if any(v < 0 for v in values):
        parser.error("{} must be >= 0 (wire format is unsigned): {}".format(what, text))
    return values


def _parse_targets(text):
    return [t.strip() for t in text.split(",") if t.strip()] if text else None


# --------------------------------------------------------------------------
# :5001 mutating commands (ack-aware)
# --------------------------------------------------------------------------

def _channel_count(ctx, session):
    """XACK nch, else extended STATUS num_channels, else config/5."""
    if session.nch is not None:
        ctx.log("channel count {:d} from XACK".format(session.nch))
        return session.nch
    try:
        st = ctx.status(timeout=min(2.0, ctx.args.timeout or 2.0)).status()
        if StatusClient.is_extended(st) and isinstance(st.get("num_channels"), int):
            ctx.log("channel count {:d} from extended STATUS".format(st["num_channels"]))
            return st["num_channels"]
    except EndpointError:
        pass
    m = get_num_channels(ctx.cfg)
    ctx.log("channel count {:d} from {}".format(m, "config" if ctx.cfg else "default"))
    return m


def _check_count(ctx, values, m):
    if len(values) == m:
        return
    if ctx.args.strict:
        raise Failed("expected {:d} values, got {:d}".format(m, len(values)))
    _warn("warning: expected {:d} values, got {:d} (the server uses the first {:d})".format(
        m, len(values), m))


def _open_mutating(ctx):
    client = ctx.ctl()
    if client.ack == "legacy":
        _warn(LEGACY_ACK_WARNING)
    session = client.open(mutating=True)
    ctx.log("ctl {}:{:d}: ack mode {} ({}), command timeout {:.1f} s".format(
        client.host, client.port, client.ack, session.mode,
        session.command_timeout("FREQ")))
    return session


def _send_and_report(ctx, session, verb, payload=b"", quiet=False):
    """Send one mutating frame on an open session; print the outcome line
    (unless quiet). Returns (ack, exit_code); ack["elapsed_ms"] is the
    round-trip time."""
    t0 = time.monotonic()
    reply = session.request(verb, payload)
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    ack = CtlClient.parse_ack(reply, session.extended)
    ack["elapsed_ms"] = elapsed_ms
    if not quiet:
        if ctx.args.json:
            parsed = CtlClient.parse_reply(reply)
            parsed["elapsed_ms"] = int(elapsed_ms)
            print_json(parsed)
        else:
            print(format_ack(ack, elapsed_ms))
    return ack, ack_exit_code(ack)


def _execute(ctx, build, verify=None):
    """Open a session, build (verb, payload) with it, send, report and
    optionally verify. build(session) may raise Failed (nothing sent)."""
    with _open_mutating(ctx) as session:
        verb, payload = build(session)
        ack, code = _send_and_report(ctx, session, verb, payload)
        if code == EXIT_OK and ctx.args.verify and verify is not None:
            code = verify(session, ack)
    return code


def _verify_timeout(ctx, session, verb):
    return ctx.args.timeout if ctx.args.timeout is not None else session.command_timeout(verb)


def _poll(predicate, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if predicate():
                return True
        except EndpointError:
            pass
        if time.monotonic() + VERIFY_POLL_SEC > deadline:
            return False
        time.sleep(VERIFY_POLL_SEC)


def _verified(ok, what, timeout):
    if ok:
        print("verified: {}".format(what))
        return EXIT_OK
    print("NOT verified: {} not observed within {:.1f} s".format(what, timeout))
    return EXIT_FAIL


def _query_session_json(session, verb):
    parsed = CtlClient.parse_reply(session.request(verb, b""))
    data = parsed.get("data")
    return data if isinstance(data, dict) else None


def _hwc_relay_verify(ctx, session, verb, what, check):
    """Verify through extended STATUS ``hwc`` (needs the hwc relay)."""
    try:
        first = ctx.status().status()
    except EndpointError:
        first = None
    if not StatusClient.is_extended(first) or not isinstance(first.get("hwc"), dict):
        print("cannot verify {} on this server (needs the hwc relay)".format(what))
        return EXIT_OK
    timeout = _verify_timeout(ctx, session, verb)

    def pred():
        hwc = ctx.status().status().get("hwc")
        return isinstance(hwc, dict) and check(hwc)
    return _verified(_poll(pred, timeout), what, timeout)


def _oqry_verify(ctx, session, verb, what, check):
    timeout = _verify_timeout(ctx, session, verb)

    def pred():
        data = _query_session_json(session, "OQRY")
        return data is not None and check(data)
    return _verified(_poll(pred, timeout), what, timeout)


def cmd_tune(args):
    ctx = Context(args)
    try:
        hz = parse_freq(args.frequency)
    except ValueError as e:
        args._parser.error(str(e))
    if not 0 < hz < 2 ** 32:
        args._parser.error("frequency out of range: {}".format(args.frequency))

    def verify(session, ack):
        timeout = _verify_timeout(ctx, session, "FREQ")
        what = "frequency {}".format(format_freq(hz))
        if "hwsq" in session.caps:
            def pred():
                data = _query_session_json(session, "HWSQ")
                return data is not None and data.get("f") == hz
        else:
            def pred():
                st = ctx.status().status()
                return st.get("current_frequency_hz", st.get("rf_center_freq")) == hz
        return _verified(_poll(pred, timeout), what, timeout)

    return _execute(ctx, lambda s: ("FREQ", struct.pack("<Q", hz)), verify)


def cmd_gain(args):
    ctx = Context(args)
    explicit = None if args.unified is not None else \
        _parse_numbers(args._parser, args.values, int, "gain values")
    if args.unified is not None and args.unified < 0:
        args._parser.error("value must be >= 0 (wire format is unsigned): {}".format(
            args.unified))
    sent = {}

    def build(session):
        m = _channel_count(ctx, session)
        values = [int(args.unified)] * m if explicit is None else explicit
        if explicit is not None:
            _check_count(ctx, values, m)
        sent["values"] = values
        return "GAIN", CtlClient.pack_u32_list(values)

    def verify(session, ack):
        values = sent["values"]
        return _hwc_relay_verify(
            ctx, session, "GAIN", "gains",
            lambda hwc: _prefix_equal(hwc.get("gains_tenths"), values))
    return _execute(ctx, build, verify)


def _prefix_equal(observed, wanted, tol=0.0):
    if not isinstance(observed, list) or not observed:
        return False
    n = min(len(observed), len(wanted))
    try:
        return all(abs(float(observed[i]) - float(wanted[i])) <= tol for i in range(n))
    except (TypeError, ValueError):
        return False


def cmd_agc(args):
    ctx = Context(args)
    return _execute(ctx, lambda s: ("AGC", b""),
                    lambda s, a: _hwc_relay_verify(ctx, s, "AGC ", "AGC",
                                                   lambda hwc: hwc.get("agc") == 1))


def cmd_recal(args):
    ctx = Context(args)
    with _open_mutating(ctx) as session:
        verb = "INIT"
        if session.extended and "recl" in session.caps:
            ack, code = _send_and_report(ctx, session, "RECL", quiet=True)
            if ack.get("applied") is False and ack.get("reason") == "disabled":
                ctx.log("RECL answered disabled; falling back to INIT")
            else:
                verb = "RECL"
                if ctx.args.json:
                    print_json(dict(ack, elapsed_ms=int(ack["elapsed_ms"])))
                else:
                    print(format_ack(ack, ack["elapsed_ms"]))
        if verb == "INIT":
            _warn(RECAL_FALLBACK_WARNING)
            ack, code = _send_and_report(ctx, session, "INIT")
        if code == EXIT_OK and ctx.args.verify:
            code = _verify_recal(ctx, session, verb)
    return code


def _verify_recal(ctx, session, verb):
    try:
        st = ctx.status().status()
    except EndpointError:
        st = None
    if not StatusClient.is_extended(st):
        print("cannot verify recalibration on this server "
              "(needs [monitoring] status_extended = 1)")
        return EXIT_OK
    timeout = _verify_timeout(ctx, session, verb)
    seen = {"dropped": False}

    def pred():
        sync = ctx.status().status().get("sync_state", 0)
        if sync < SYNC_LOCKED:
            seen["dropped"] = True
            return False
        return seen["dropped"]
    return _verified(_poll(pred, timeout), "recalibration (sync lost and re-locked)", timeout)


def cmd_lna_gain(args):
    ctx = Context(args)
    if args.unified is not None and args.unified < 0:
        args._parser.error("value must be >= 0 (wire format is unsigned): {}".format(
            args.unified))
    explicit = None if args.unified is not None else \
        _parse_numbers(args._parser, args.values, float, "gain values")
    sent = {}

    def build(session):
        m = _channel_count(ctx, session)
        db = [float(args.unified)] * m if explicit is None else explicit
        if explicit is not None:
            _check_count(ctx, db, m)
        sent["db"] = db
        return "EGAN", CtlClient.pack_u32_list([int(round(v * 10.0)) for v in db])

    def verify(session, ack):
        return _hwc_relay_verify(
            ctx, session, "EGAN", "external LNA gains",
            lambda hwc: _prefix_equal(hwc.get("ext_lna_gains_db"), sent["db"], tol=0.05))
    return _execute(ctx, build, verify)


def cmd_bias_tee(args):
    ctx = Context(args)
    explicit = None if args.all is not None else \
        _parse_numbers(args._parser, args.values, int, "states")
    sent = {}

    def build(session):
        m = _channel_count(ctx, session)
        states = [1 if args.all == "on" else 0] * m if explicit is None else explicit
        if explicit is not None:
            _check_count(ctx, states, m)
        states = [1 if s else 0 for s in states]
        sent["mask"] = sum(1 << i for i, s in enumerate(states[:m]) if s)
        return "BIAS", CtlClient.pack_u32_list(states)

    def verify(session, ack):
        return _hwc_relay_verify(ctx, session, "BIAS", "bias-tee states",
                                 lambda hwc: hwc.get("bias_tee") == sent["mask"])
    return _execute(ctx, build, verify)


def cmd_bearing(args):
    ctx = Context(args)
    az, el = args.azimuth, args.elevation
    return _execute(
        ctx, lambda s: ("ORNT", struct.pack("<ff", float(az), float(el))),
        lambda s, a: _oqry_verify(
            ctx, s, "ORNT", "bearing az={:.2f} el={:.2f}".format(az, el),
            lambda d: d.get("state") == 2 and abs(float(d.get("az", 1e9)) - az) <= 0.5
            and abs(float(d.get("el", 1e9)) - el) <= 0.5))


def cmd_park(args):
    ctx = Context(args)
    return _execute(ctx, lambda s: ("PARK", b""),
                    lambda s, a: _oqry_verify(ctx, s, "PARK", "antenna parked (SETTLED)",
                                              lambda d: d.get("state") == 2))


def cmd_scan(args):
    ctx = Context(args)
    if args.action == "start":
        return _execute(ctx, lambda s: ("SCAN", b""),
                        lambda s, a: _oqry_verify(ctx, s, "SCAN", "scan running (SCANNING)",
                                                  lambda d: d.get("state") == 3))
    return _execute(ctx, lambda s: ("OSTP", b""),
                    lambda s, a: _oqry_verify(ctx, s, "OSTP", "rotator stopped",
                                              lambda d: d.get("state") in (0, 2)))


def cmd_track_lock(args):
    ctx = Context(args)
    if args.action is None:
        parsed = CtlClient.parse_reply(ctx.ctl().track_lock_query())
        data = parsed.get("data")
        if args.json:
            print_json(parsed)
            return EXIT_OK if isinstance(data, dict) else EXIT_FAIL
        if not isinstance(data, dict):
            raise Failed("this server does not answer TLCK (legacy hw_controller)")
        print("track lock: required={} approved={}".format(
            "yes" if data.get("req") else "no", "yes" if data.get("appr") else "no"))
        return EXIT_OK
    approve = args.action == "approve"

    def build(session):
        # Legacy per a live XACK, or per the auto-mode cache (no XACK sent):
        # an old hw_controller does not know TLKA and would only log it.
        if not session.extended and (session.xack_sent or session.client.ack == "auto"):
            raise Failed("this server does not support TLKA (legacy hw_controller); "
                         "command not sent")
        return "TLKA", struct.pack("<I", 1 if approve else 0)
    return _execute(ctx, build)


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------

def _load_schedule_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Failed("cannot read schedule {}: {}".format(path, e))


def _schedule_entries(doc):
    if isinstance(doc, dict) and isinstance(doc.get("entries"), list):
        return len(doc["entries"])
    if isinstance(doc, list):
        return len(doc)
    return 0


def _print_schedule_outcome(ctx, ack, how, entries, elapsed_ms):
    if ctx.args.json:
        print_json(dict(ack, transfer=how, entries=entries, elapsed_ms=int(elapsed_ms)))
    elif ack.get("applied") is True:
        print("applied: {:d} entries ({})".format(entries, how))
    else:
        print(format_ack(ack, elapsed_ms))


def _verify_schedule(ctx, session, doc, entries):
    timeout = _verify_timeout(ctx, session, "SCHD")
    gen = doc.get("gen") if isinstance(doc, dict) else None
    if "schg" in session.caps:
        def pred():
            d = _query_session_json(session, "SCHG")
            if d is None or not d.get("loaded"):
                return False
            return d.get("gen") == gen if gen is not None else d.get("total") == entries
    else:
        def pred():
            d = _query_session_json(session, "SCHQ")
            return d is not None and bool(d.get("active")) and d.get("total") == entries
    return _verified(_poll(pred, timeout), "schedule loaded ({:d} entries)".format(entries),
                     timeout)


def _schedule_load(ctx, args):
    if args.as_path:
        path_bytes = args.as_path.encode("utf-8")
        if len(path_bytes) > PAYLOAD_SIZE:
            raise Failed("schedule path is {:d} bytes; the limit is {:d}".format(
                len(path_bytes), PAYLOAD_SIZE))
        return _execute(ctx, lambda s: ("SCHD", path_bytes))
    if not args.file:
        args._parser.error("schedule load needs a FILE (or --as-path PATH)")
    doc = _load_schedule_file(args.file)
    data = CtlClient.schedule_json(doc)
    entries = _schedule_entries(doc)
    with _open_mutating(ctx) as session:
        t0 = time.monotonic()
        if len(data) <= PAYLOAD_SIZE:
            reply = session.request("SCHD", data)
            how = "inline"
        elif args.inline_only or "schb" not in session.caps:
            raise Failed("schedule is {:d} bytes; this server accepts 124-byte inline "
                         "schedules only (use --as-path for a file on the DAQ host)".format(
                             len(data)))
        else:
            try:
                reply, nchunks = session.upload_schedule_chunked(data)
            except ScheduleTransferError as e:
                raise Failed(str(e))
            how = "chunked, {:d} chunks".format(nchunks)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        ack = CtlClient.parse_ack(reply, session.extended)
        parsed = CtlClient.parse_reply(reply)
        if isinstance(parsed.get("data"), dict) and isinstance(parsed["data"].get("entries"), int):
            entries = parsed["data"]["entries"]
        _print_schedule_outcome(ctx, ack, how, entries, elapsed_ms)
        code = ack_exit_code(ack)
        if code == EXIT_OK and args.verify:
            code = _verify_schedule(ctx, session, doc, entries)
    return code


def cmd_schedule(args):
    ctx = Context(args)
    action = args.action
    if action == "load":
        return _schedule_load(ctx, args)
    if action == "stop":
        return _execute(ctx, lambda s: ("SCHS", b""))
    if action == "next":
        return _execute(ctx, lambda s: ("SCHN", b""))
    return _print_ctl_reply(ctx.ctl().schedule_query(), args.json, "(no schedule data)")


# --------------------------------------------------------------------------
# :5001 queries
# --------------------------------------------------------------------------

def _print_ctl_reply(reply, json_mode, ok_message="OK"):
    """Render a parsed 128-byte control reply (contract: 'FNSD' + optional
    NUL-padded JSON payload). Returns the exit code."""
    parsed = CtlClient.parse_reply(reply)
    if json_mode:
        print_json(parsed)
        return EXIT_OK if parsed.get("ok") else EXIT_FAIL
    if not parsed.get("ok"):
        print(f"Error: {parsed.get('error', 'bad reply')}")
        return EXIT_FAIL
    data = parsed.get("data")
    if data is not None:
        print_json(data)
    elif parsed.get("raw"):
        print(parsed["raw"])
    else:
        print(ok_message)
    return EXIT_OK


def cmd_orientation(args):
    ctx = Context(args)
    return _print_ctl_reply(ctx.ctl().orientation_query(), args.json, "(no orientation data)")


def cmd_rf_budget(args):
    ctx = Context(args)
    return _print_ctl_reply(ctx.ctl().rf_query(), args.json, "(no link-budget data)")


HWSQ_HEADERS = ["FSM", "PEND", "NOISE", "AGC", "NCH", "FREQ", "FRAME-AGE", "FRAMES", "TERM"]


def _hwsq_row(d):
    fage = d.get("fage")
    if isinstance(fage, (int, float)) and fage >= 0:
        age = ">=99.999s" if fage >= 99999 else "{:.3f}s".format(fage / 1000.0)
        if d.get("fr") == 0:
            age += " (stalled)"
    else:
        age = "-"
    freq = d.get("f")
    return [d.get("fsm", "?"), d.get("pend", "-"), d.get("ns", "-"), d.get("agc", "-"),
            d.get("nch", "-"), format_freq(freq) if isinstance(freq, (int, float)) else "-",
            age, d.get("fc", "-"), d.get("t", "-")]


def cmd_hwc_status(args):
    ctx = Context(args)
    parsed = CtlClient.parse_reply(ctx.ctl().hwc_state_query())
    data = parsed.get("data")
    if args.json:
        print_json(parsed)
        return EXIT_OK if isinstance(data, dict) else EXIT_FAIL
    if not isinstance(data, dict):
        raise Failed("this server does not answer HWSQ (legacy hw_controller)")
    if "error" in data:
        raise Failed("HWSQ: {}".format(data["error"]))
    print_table([h.lower() for h in HWSQ_HEADERS], [_hwsq_row(data)], args.color,
                separator=False)
    return EXIT_OK


# --------------------------------------------------------------------------
# :5002 status-server commands
# --------------------------------------------------------------------------

def cmd_status(args):
    ctx = Context(args)
    status = ctx.status()
    if args.json:
        print_json(status.status())
        return EXIT_OK
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
    return EXIT_OK


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


def cmd_metrics(args):
    ctx = Context(args)
    data = ctx.status().metrics()
    if args.json:
        print_json(data)
        return EXIT_FAIL if "error" in data else EXIT_OK
    if "error" in data:
        print(f"Error: {data['error']}")
        return EXIT_FAIL
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
    return EXIT_OK


def _event_line(endpoint, event, recv_ts):
    source = event.get("source")
    label = source.get("node_id") if isinstance(source, dict) and source.get("node_id") \
        else endpoint
    sev = str(event.get("severity", "info"))
    line = "{} [{}] {:<8} {:<16} {}".format(
        format_ts(recv_ts), label, sev, str(event.get("event_type", "?")),
        json.dumps(event.get("payload", {}), default=str))
    ev_ts = event.get("timestamp")
    if isinstance(ev_ts, (int, float)) and abs(ev_ts - recv_ts) > 1.0:
        line += " (event time {})".format(format_ts(ev_ts))
    return line, sev


def _tail_endpoints(ctx, args):
    hosts = []
    for item in args.hosts or []:
        hosts += [h.strip() for h in item.split(",") if h.strip()]
    hosts = hosts or [ctx.host]
    if args.instances:
        try:
            instances = [int(i) for i in args.instances.split(",") if i.strip()]
        except ValueError:
            args._parser.error("bad --instances list: {}".format(args.instances))
    else:
        instances = [ctx.instance]
    if args.port_events and len(hosts) * len(instances) == 1:
        return [(hosts[0], args.port_events)], instances
    return [(h, resolve_ports(ctx.cfg, i, ctx.stride)["events"])
            for h in hosts for i in instances], instances


def _tail_topics(ctx, filters, instances):
    if not filters:
        return None
    topics = list(filters)
    prefixed = ctx.cfg is not None and \
        ctx.cfg.get("monitoring", "event_topic_prefix", fallback="0").strip() == "1"
    if prefixed:
        topics += ["inst{:d}.{}".format(n, t) for n in instances for t in filters]
    return topics


def cmd_events(args):
    ctx = Context(args)
    if args.tail:
        from .client.events_sub import EventSubscriber
        endpoints, instances = _tail_endpoints(ctx, args)
        filters = [f.strip() for f in args.filter.split(",") if f.strip()] \
            if args.filter else None
        topics = _tail_topics(ctx, filters, instances)
        ctx.log("subscribing to {} (topics: {})".format(
            ", ".join("{}:{}".format(h, p) for h, p in endpoints), topics or "all"))
        try:
            sub = EventSubscriber(endpoints, topics)
        except RuntimeError as e:
            raise Failed(str(e))
        received = 0
        try:
            for endpoint, evt in sub:
                if args.json:
                    print(json.dumps(evt, default=str))
                else:
                    line, sev = _event_line(endpoint, evt, time.time())
                    print(colorize(line, severity_color(sev), args.color))
                sys.stdout.flush()
                received += 1
                if args.count and received >= args.count:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            sub.close()
        return EXIT_OK
    data = ctx.status().events()
    if args.json:
        print_json(data)
        return EXIT_FAIL if "error" in data else EXIT_OK
    if "error" in data:
        raise Failed(str(data["error"]))
    for evt in data.get("events", []):
        sev = evt.get("severity", "info")
        etype = evt.get("event_type", "?")
        ts = evt.get("timestamp", 0)
        print(colorize(f"[{ts:.3f}] {etype}: {evt.get('payload', {})}",
                       severity_color(sev), args.color))
    return EXIT_OK


def _print_records(data, key, json_mode):
    if json_mode:
        print_json(data)
        return EXIT_FAIL if "error" in data else EXIT_OK
    if "error" in data:
        raise Failed(str(data["error"]))
    for record in data.get(key, []):
        print(json.dumps(record, sort_keys=True, default=str))
    return EXIT_OK


def cmd_cal_history(args):
    ctx = Context(args)
    freq = None
    if args.freq:
        try:
            freq = parse_freq(args.freq)
        except ValueError as e:
            args._parser.error(str(e))
    try:
        since_sec = parse_duration(args.since) if args.since else None
    except ValueError as e:
        args._parser.error(str(e))
    if args.remote:
        status = ctx.status()
        extended = StatusClient.is_extended(status.status())
        ctx.log("status server is {}".format("extended" if extended else "legacy"))
        if extended and (since_sec is not None or args.limit is not None):
            since_ms = 0
            if since_sec is not None:
                server_ts = float(status.ping().get("ts", time.time()))
                since_ms = int(round((server_ts - since_sec) * 1000.0))
            data = status.cal_history(freq, since_ms, args.limit)
        else:
            if since_sec is not None or args.limit is not None:
                _warn(CAL_HISTORY_IGNORED)
            data = status.cal_history(freq)
        return _print_records(data, "cal_history", args.json)

    from .client.db import open_db, cal_history, default_db_dir
    db_dir = args.db_dir or default_db_dir(ctx.cfg, ctx.cfg_path, ctx.instance)
    ctx.log("local database {}".format(db_dir))
    since_ms = int((time.time() - since_sec) * 1000) if since_sec is not None else None
    try:
        db = open_db(db_dir, ctx.firmware_dir())
    except RuntimeError as e:
        raise Failed(str(e))
    try:
        records = cal_history(db, since_ms=since_ms, freq=freq,
                              limit=args.limit if args.limit is not None else 50)
    finally:
        db.close()
    if args.json:
        print_json([r.__dict__ if hasattr(r, '__dict__') else r for r in records])
        return EXIT_OK
    for r in records:
        print(r)
    return EXIT_OK


def cmd_freq_scan(args):
    ctx = Context(args)
    freq = None
    if args.freq:
        try:
            freq = parse_freq(args.freq)
        except ValueError as e:
            args._parser.error(str(e))
    if args.remote:
        return _print_records(ctx.status().scan_summary(freq), "freq_scan", args.json)
    from .client.db import open_db, freq_scan_summary, default_db_dir
    db_dir = args.db_dir or default_db_dir(ctx.cfg, ctx.cfg_path, ctx.instance)
    ctx.log("local database {}".format(db_dir))
    try:
        db = open_db(db_dir, ctx.firmware_dir())
    except RuntimeError as e:
        raise Failed(str(e))
    try:
        summary = freq_scan_summary(db, freq)
    finally:
        db.close()
    if args.json:
        print_json([s.__dict__ if hasattr(s, '__dict__') else s for s in summary])
        return EXIT_OK
    for s in summary:
        print(s)
    return EXIT_OK


def cmd_db_stats(args):
    ctx = Context(args)
    data = ctx.status().db_stats()
    print_json(data)
    return EXIT_FAIL if "error" in data else EXIT_OK


def cmd_peers(args):
    ctx = Context(args)
    data = ctx.status().peers()
    if StatusClient.is_unknown_command(data):
        raise Failed(NO_PEERS_TEXT)
    if args.json:
        print_json(data)
        return EXIT_FAIL if "error" in data else EXIT_OK
    if "error" in data:
        raise Failed(str(data["error"]))
    now = time.time()
    rows = []
    for addr, row in sorted((data.get("peers") or {}).items()):
        if not isinstance(row, dict):
            continue
        age = row.get("seen_age_sec")
        if age is None and isinstance(row.get("last_seen"), (int, float)) and row["last_seen"]:
            age = now - row["last_seen"]
        freq = row.get("frequency_hz")
        state = row.get("state") or ("up" if row.get("alive") else "down")
        rows.append([row.get("node_key") or addr, row.get("health", "-"),
                     row.get("sync_state", "-"),
                     format_freq(freq) if isinstance(freq, (int, float)) and freq else "-",
                     format_age(age), state])
    print("instance {} ({})  advisory leader: {}".format(
        data.get("instance_id", "?"), data.get("node_id") or "-",
        data.get("advisory_leader", "-")))
    print_table(["node", "health", "sync", "freq", "seen-age", "state"], rows, args.color,
                separator=False)
    return EXIT_OK


# --------------------------------------------------------------------------
# Local subcommands
# --------------------------------------------------------------------------

def cmd_ports(args):
    ctx = Context(args)
    rows = [[svc, port, note] for svc, port, note in
            service_ports(ctx.cfg, ctx.instance, ctx.stride)]
    if args.json:
        print_json({"instance_id": ctx.instance, "port_stride": ctx.stride,
                    "ports": {r[0]: r[1] for r in rows}})
        return EXIT_OK
    print("instance {:d} (offset {:d})".format(ctx.instance, ctx.instance * ctx.stride))
    print_table(["service", "port", "note"], rows, args.color, separator=False)
    return EXIT_OK


def cmd_instances(args):
    from .client.registry import candidate_run_dirs, read_entries, pids_alive
    ctx = Context(args)
    dirs = candidate_run_dirs(args.run_dir, ctx.firmware_dir())
    entries = read_entries(dirs)
    if args.json:
        print_json([e for _, e in entries])
        return EXIT_OK
    if not entries:
        print("no registry entries in {} (only instances started with [host] "
              "instance_registry = 1 are listed)".format(", ".join(dirs)))
        return EXIT_OK
    headers = ["id", "mode", "launcher", "cwd", "ports.status", "pids-alive", "started"]
    if args.ping:
        headers.append("ping")
    rows = []
    for _, e in entries:
        alive, total = pids_alive(e)
        ports = e.get("ports") if isinstance(e.get("ports"), dict) else {}
        status_port = ports.get("status")
        row = [e.get("instance_id", "?"), e.get("mode", "-"), e.get("launcher", "-"),
               e.get("cwd", "-"), status_port if status_port else "-",
               "{:d}/{:d}".format(alive, total),
               format_ts(e.get("started_at")) if e.get("started_at") else "-"]
        if args.ping:
            row.append(_ping_cell(ctx, status_port))
        rows.append(row)
    print_table(headers, rows, args.color, separator=False)
    return EXIT_OK


def _ping_cell(ctx, port):
    if not port:
        return "-"
    t0 = time.monotonic()
    try:
        StatusClient("127.0.0.1", int(port), min(2.0, ctx.args.timeout or 2.0)).ping()
    except EndpointError as e:
        return "error ({})".format(e.kind)
    return "{:.1f} ms".format((time.monotonic() - t0) * 1000.0)


def cmd_capacity(args):
    from .client import capacity
    ctx = Context(args)
    fw = ctx.firmware_dir()
    tool = capacity.find_tool(fw)
    if tool is None:
        raise Failed("{} not found in {} (use --firmware-dir or HEIMDALL_FIRMWARE)".format(
            capacity.TOOL_NAME, fw or "(no firmware directory)"), EXIT_CONN)
    configs = args.capacity_configs or ([ctx.cfg_path] if ctx.cfg_path else None)
    cmd = capacity.build_command(tool, configs, args.add_instances, args.host_json,
                                 args.json)
    ctx.log("running {}".format(" ".join(cmd)))
    proc = capacity.run(cmd, fw)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    code = capacity.map_exit(proc.returncode, args.strict)
    if proc.returncode not in (0, 1, 2):
        _warn("{} exited with code {:d}".format(capacity.TOOL_NAME, proc.returncode))
    return code


def cmd_config_show(args):
    cfg_path = find_config(args.config)
    if not cfg_path:
        print("No config file found")
        return EXIT_OK
    cfg = load_config(cfg_path)
    print(f"Config: {cfg_path}")
    for section in cfg.sections():
        print(f"\n[{section}]")
        for key, val in cfg.items(section):
            print(f"  {key} = {val}")
    return EXIT_OK


# --------------------------------------------------------------------------
# fleet (coordinator :6000)
# --------------------------------------------------------------------------

def _reply_error_code(reply):
    """Print the error of a failed coordinator reply; returns its exit code."""
    data = reply.data
    err = data.get("error", "error")
    if err == "not_replicated":
        rep = data.get("replicated") or {}
        _warn("warning: the change is committed on the leader but was not replicated to "
              "a quorum (stored {} of {}); it is not durable - re-issue it".format(
                  rep.get("stored", "?"), rep.get("of", "?")))
        return EXIT_FAIL
    msg = "Error: {}".format(err)
    if data.get("message"):
        msg += ": {}".format(data["message"])
    if data.get("usage"):
        msg += " (usage: {})".format(data["usage"])
    if "retry_in_sec" in data:
        msg += " (retry in {} s)".format(data["retry_in_sec"])
    print(msg, file=sys.stderr)
    return EXIT_FAIL


def _durability_note(data):
    if data.get("durable") is False:
        rep = data.get("replicated") or {}
        _warn("note: stored on {} of {} coordinator candidates (durable: false)".format(
            rep.get("stored", "?"), rep.get("of", "?")))


def _fleet_simple(ctx, client, line, render, body=None, kind="query", timeout=None,
                 json_code=None):
    """One coordinator request. In --json mode the raw reply is printed and
    json_code(data) (when given) decides the exit code of an ok reply, so
    per-target results count there as they do in the rendered output."""
    reply = client.request(line, body, kind=kind, timeout=timeout)
    if ctx.args.json:
        print(reply.raw)
        if not reply.ok:
            return EXIT_FAIL
        return json_code(reply.data) if json_code is not None else EXIT_OK
    if not reply.ok:
        return _reply_error_code(reply)
    return render(reply.data)


def _results_of(data):
    if isinstance(data.get("by_node"), dict) and data["by_node"]:
        return data["by_node"]
    if isinstance(data.get("results"), dict):
        return data["results"]
    if "node" in data and "result" in data:
        return {data["node"]: data["result"]}
    return {}


def _results_code(data):
    """Exit code of a fan-out/recal/exec/rebalance reply's per-target results."""
    return results_exit_code(summarize(_results_of(data)))


def _render_results(ctx, results, title="RESULT"):
    rows = [[k, format_result(r)] for k, r in sorted(results.items())]
    print_table(["NODE", title], rows, ctx.args.color, separator=False)
    counts = summarize(results)
    print(summary_line(counts))
    return results_exit_code(counts)


def _fanout_prefix(args):
    targets = _parse_targets(getattr(args, "targets", None))
    return "FANOUT {} ".format(",".join(targets)) if targets else "ALL "


def _fleet_fanout(ctx, client, verb_line, kind="mutation"):
    line = _fanout_prefix(ctx.args) + verb_line
    if client.legacy:
        return _fleet_legacy(ctx, client, verb_line)
    return _fleet_simple(ctx, client, line,
                         lambda d: _render_results(ctx, _results_of(d)), kind=kind,
                         json_code=_results_code)


def _fleet_legacy(ctx, client, verb_line):
    """Protocol-1 coordinator: only FREQ/GAIN fan-outs exist."""
    verb = verb_line.split()[0]
    if verb not in ("FREQ", "GAIN") or _parse_targets(getattr(ctx.args, "targets", None)):
        raise Failed("the coordinator does not support this command (legacy coordinator "
                     "without VERSION; only fleet tune/gain to all members)")
    return _fleet_simple(ctx, client, verb_line,
                         lambda d: _render_results(ctx, _results_of(d)), kind="mutation",
                         json_code=_results_code)


def _fleet_hwsq_groups(ctx, client, fallback):
    """Group the targets by channel count via HWSQ (for --unified values)."""
    reply = client.request(_fanout_prefix(ctx.args) + "HWSQ", kind="query")
    if not reply.ok:
        raise Failed("cannot read the members' channel counts: {}".format(reply.error))
    groups = {}
    for key, r in _results_of(reply.data).items():
        data = r.get("data") if isinstance(r, dict) else None
        nch = data.get("nch") if isinstance(data, dict) else None
        if not isinstance(nch, int):
            _warn("warning: {}: channel count unknown, using {:d}".format(key, fallback))
            nch = fallback
        groups.setdefault(nch, []).append(key)
    return groups


def _fleet_per_channel(ctx, client, verb, explicit, unified_value):
    """explicit: list of wire ints for every target, or None to fan a unified
    value out per channel-count group."""
    if explicit is not None:
        return _fleet_fanout(ctx, client, "{} {}".format(
            verb, ",".join(str(v) for v in explicit)))
    if client.legacy:
        raise Failed("--unified needs a coordinator with protocol 2 (VERSION)")
    groups = _fleet_hwsq_groups(ctx, client, get_num_channels(ctx.cfg))
    merged, raws, ok = {}, [], True
    for nch, keys in sorted(groups.items()):
        line = "FANOUT {} {} {}".format(",".join(sorted(keys)), verb,
                                        ",".join([str(unified_value)] * nch))
        reply = client.request(line, kind="mutation")
        raws.append(reply.raw)
        if not reply.ok:
            ok = False
            if not ctx.args.json:
                _reply_error_code(reply)
            continue
        merged.update(_results_of(reply.data))
    if ctx.args.json:
        for raw in raws:
            print(raw)
        return results_exit_code(summarize(merged)) if ok else EXIT_FAIL
    code = _render_results(ctx, merged)
    return code if ok else EXIT_FAIL


def _hwsq_data(result):
    """The HWSQ JSON of one per-node result, or None when that node failed."""
    d = result.get("data") if isinstance(result, dict) else None
    return d if isinstance(d, dict) and "error" not in d else None


def _hwsq_code(data):
    """Exit code of a fleet HWSQ reply: 1 when any node returned no HWSQ data."""
    results = _results_of(data).values()
    return EXIT_FAIL if any(_hwsq_data(r) is None for r in results) else EXIT_OK


def _render_hwsq_table(ctx, data):
    rows = []
    for key, r in sorted(_results_of(data).items()):
        d = _hwsq_data(r)
        if d is not None:
            rows.append([key] + _hwsq_row(d))
        else:
            rows.append([key, format_result(r)] + [""] * (len(HWSQ_HEADERS) - 1))
    print_table(["NODE"] + HWSQ_HEADERS, rows, ctx.args.color, separator=False)
    return _hwsq_code(data)


def _fleet_status_render(ctx, client, version, nodes, health):
    members = health.get("members", {}) if isinstance(health, dict) else {}
    role = version.get("role")
    term = version.get("term")
    leader_ep = "{}:{}".format(*client.last_endpoint) if client.last_endpoint else "?"
    if role == "follower":
        try:
            lr = client.request("LEADER", kind="query")
            leader = lr.data.get("leader") if lr.ok else None
            leader_ep = leader.get("endpoint", "?") if isinstance(leader, dict) else "unknown"
            term = leader.get("term", term) if isinstance(leader, dict) else term
        except EndpointError:
            leader_ep = "unknown"
    node_map = nodes.get("nodes", {}) if isinstance(nodes, dict) else {}
    keys = sorted(set(node_map) | set(members))
    unreachable = health.get("unreachable") if isinstance(health, dict) else None
    if not isinstance(unreachable, list):
        unreachable = [k for k in keys if "error" in (node_map.get(k) or {})]
    print("leader {} (term {})   overall: {}   members: {:d}   unreachable: {:d}".format(
        leader_ep, term if term is not None else "-",
        nodes.get("overall") or health.get("overall") or "unknown", len(keys),
        len(unreachable)))
    rows = []
    for k in keys:
        st = node_map.get(k) or {}
        m = members.get(k) or {}
        reachable = "error" not in st
        health_txt = "down" if m.get("state") == "down" or not reachable else \
            (m.get("health") or st.get("pipeline_health") or "unknown")
        age = st.get("snapshot_age_sec", m.get("snapshot_age_sec"))
        if age is None:
            age = m.get("seen_age_sec")
        counters = st.get("counters") if isinstance(st.get("counters"), dict) else {}
        freq = st.get("current_frequency_hz", st.get("rf_center_freq"))
        if reachable:
            drops = counters.get("dropped_frames_iq", 0) + counters.get("dropped_frames_hwc", 0)
            rows.append([k, health_txt, st.get("sync_state", "-"),
                         format_freq(freq) if isinstance(freq, (int, float)) else "-",
                         format_age(age), counters.get("buffer_overrun_cnt", "-"), drops])
        else:
            rows.append([k, health_txt, "-", "-", format_age(age), "-", "-"])
    print_table(["NODE", "HEALTH", "SYNC", "FREQ", "AGE", "OVERRUNS", "DROPS"], rows,
                ctx.args.color, separator=False)
    return (leader_ep, term)


def _fleet_status(ctx, client, args):
    version = client.version()
    fresh = " FRESH" if args.fresh else ""
    if version is None:  # legacy coordinator: aggregated STATUS
        return _fleet_simple(ctx, client, "STATUS" + fresh, _print_json_ok)
    last = None
    try:
        while True:
            nodes = client.request("NODES STATUS" + fresh, kind="query")
            if ctx.args.json:
                print(nodes.raw)
                if not args.watch:
                    return EXIT_OK if nodes.ok else EXIT_FAIL
            else:
                if not nodes.ok:
                    return _reply_error_code(nodes)
                try:
                    health = client.request("HEALTH", kind="query")
                    health_data = health.data if health.ok else {}
                except EndpointError:
                    health_data = {}
                if args.watch:
                    sys.stdout.write("\033[2J\033[H")
                current = _fleet_status_render(ctx, client, version, nodes.data,
                                               health_data)
                if args.watch and last is not None and current != last:
                    print("leader changed: {} term {} -> {} term {}".format(
                        last[0], last[1], current[0], current[1]))
                last = current
                if not args.watch:
                    return EXIT_OK
            sys.stdout.flush()
            time.sleep(1.0)
            if args.watch:
                client.refresh_version()
                version = client.version() or version  # keeps the last good one on error
    except KeyboardInterrupt:
        return EXIT_OK


def _render_members(ctx, data):
    rows = []
    for m in data.get("members", []):
        if isinstance(m, dict):
            rows.append([m.get("node_key", "-"), m.get("node_id", "-"), m.get("host", "-"),
                         m.get("instance_id", "-"), m.get("source", "-"),
                         m.get("state", "-")])
    print_table(["NODE", "NODE-ID", "HOST", "IID", "SOURCE", "STATE"], rows, ctx.args.color,
                separator=False)
    for c in data.get("key_collisions") or []:
        _warn("warning: node key collision {}: holder {} rejected {}".format(
            c.get("node_key"), c.get("holder_addr"), c.get("rejected_addr")))
    if "changed" in data:
        print("changed: {}".format("yes" if data["changed"] else "no"))
        _durability_note(data)
    return EXIT_OK


def _render_health(ctx, data):
    rows = []
    for k, m in sorted((data.get("members") or {}).items()):
        freq = m.get("frequency_hz")
        rows.append([k, m.get("state", "-"), "yes" if m.get("reachable") else "no",
                     m.get("health", "-"), m.get("sync_state", "-"),
                     format_freq(freq) if isinstance(freq, (int, float)) and freq else "-",
                     format_age(m.get("seen_age_sec")), m.get("error_kind") or "-"])
    print("overall: {}".format(data.get("overall", "unknown")))
    print_table(["NODE", "STATE", "REACHABLE", "HEALTH", "SYNC", "FREQ", "SEEN-AGE",
                 "ERROR"], rows, ctx.args.color, separator=False)
    return EXIT_OK


def _render_leader(ctx, data):
    leader = data.get("leader")
    print("role {}  term {}  leader {}  quorum {}  schedule version {}".format(
        data.get("role", "-"), data.get("term", "-"),
        leader.get("endpoint") if isinstance(leader, dict) else "unknown",
        "yes" if data.get("quorum") else "no", data.get("schedule_version", "-")))
    rows = [[c.get("endpoint", "-"), "yes" if c.get("reachable") else "no",
             c.get("role", "-"), c.get("term", "-")]
            for c in data.get("candidates") or [] if isinstance(c, dict)]
    if rows:
        print_table(["CANDIDATE", "REACHABLE", "ROLE", "TERM"], rows, ctx.args.color,
                    separator=False)
    return EXIT_OK


def _render_assignments(ctx, data):
    print("strategy {}  rebalance {}".format(data.get("strategy", "-"),
                                              data.get("gen_seq", "-")))
    rows = []
    for k, a in sorted((data.get("assignments") or {}).items()):
        entries = a.get("entries")
        rows.append([k, a.get("gen", "-"),
                     len(entries) if isinstance(entries, list) else entries,
                     a.get("state", "-"), a.get("attempts", "-")])
    print_table(["NODE", "GEN", "ENTRIES", "STATE", "ATTEMPTS"], rows, ctx.args.color,
                separator=False)
    if data.get("empty"):
        print("empty: {}".format(", ".join(data["empty"])))
    return EXIT_OK


def _render_rebalance(ctx, data):
    results = _results_of(data)
    rows = [[k, s.get("entries", "-"), s.get("gen", "-"), s.get("transfer", "-"),
             {True: "yes", False: "no"}.get(s.get("verified"), "-"), format_result(s)]
            for k, s in sorted(results.items()) if isinstance(s, dict)]
    print("rebalanced {} instances (strategy {})".format(data.get("instances", "?"),
                                                        data.get("strategy", "-")))
    print_table(["NODE", "ENTRIES", "GEN", "TRANSFER", "VERIFIED", "RESULT"], rows,
                ctx.args.color, separator=False)
    for key in ("skipped", "empty", "inactive"):
        if data.get(key):
            print("{}: {}".format(key, ", ".join(data[key])))
    _durability_note(data)
    counts = summarize(results)
    return results_exit_code(counts)


def _render_events(ctx, data):
    for evt in data.get("events", []):
        if not isinstance(evt, dict):
            continue
        sev = str(evt.get("severity", "info"))
        line = "{} [{}] {:<8} {:<16} {}".format(
            format_ts(evt.get("rx_ts")), evt.get("node_key", "?"), sev,
            str(evt.get("event_type", "?")), json.dumps(evt.get("payload", {}), default=str))
        print(colorize(line, severity_color(sev), ctx.args.color))
    for err in data.get("errors") or []:
        _warn("warning: events unavailable from {}".format(err))
    return EXIT_OK


def _render_schedule_set(ctx, data):
    preview = ", ".join("{}={}".format(k, v) for k, v in
                        sorted((data.get("assignments") or {}).items()))
    print("stored: {} entries, strategy {}, version {} ({} bytes)".format(
        data.get("entries", "?"), data.get("strategy", "-"), data.get("version", "-"),
        data.get("bytes", "?")))
    if preview:
        print("partition preview: {}".format(preview))
    print("run 'heimdall-ctl fleet rebalance' to distribute it")
    _durability_note(data)
    return EXIT_OK


def _render_schedule_get(ctx, data):
    print("version {}  term {}  set at {}".format(
        data.get("version", "-"), data.get("term", "-"), format_ts(data.get("set_at"))))
    print_json(data.get("schedule"))
    return EXIT_OK


def _render_version(ctx, data):
    d = data.get("deadlines") or {}
    print("protocol {}  fw {}  node {}  cluster {}  role {}  term {}  members {}".format(
        data.get("protocol"), data.get("fw_version", "-"), data.get("node_id", "-"),
        data.get("cluster_id", "-"), data.get("role", "-"), data.get("term", "-"),
        data.get("members", "-")))
    print("deadlines: query {} ms, mutation {} ms, schedule {} ms; abandoned workers {}".format(
        d.get("query_ms", "-"), d.get("mutation_ms", "-"), d.get("schedule_ms", "-"),
        data.get("abandoned_workers", "-")))
    return EXIT_OK


def _print_json_ok(data):
    print_json(data)
    return EXIT_OK


def _per_node_json(key):
    def render(data):
        print_json(data.get(key, data))
        return EXIT_OK
    return render


def _fleet_values(args, cast, scale=1.0):
    """Explicit per-channel wire values, or None for --unified/--all."""
    raw = getattr(args, "values", None)
    if raw is None:
        return None
    values = _parse_numbers(args._parser, raw, cast, "values")
    return [int(round(v * scale)) for v in values]


def cmd_fleet(args):
    ctx = Context(args)
    client = ctx.coordinator()
    a = args.fleet_command
    if a == "ping":
        t0 = time.monotonic()

        def render_pong(d):
            print("pong from {}:{} in {:.1f} ms (ts {})".format(
                client.last_endpoint[0], client.last_endpoint[1],
                (time.monotonic() - t0) * 1000.0, format_ts(d.get("ts"))))
            return EXIT_OK
        return _fleet_simple(ctx, client, "PING", render_pong,
                             timeout=args.timeout or VERSION_TIMEOUT_SEC)
    if a == "version":
        return _fleet_simple(ctx, client, "VERSION", lambda d: _render_version(ctx, d),
                             timeout=args.timeout or VERSION_TIMEOUT_SEC)
    if a == "status":
        return _fleet_status(ctx, client, args)
    if a == "leader":
        return _fleet_simple(ctx, client, "LEADER", lambda d: _render_leader(ctx, d))
    if a == "health":
        return _fleet_simple(ctx, client, "HEALTH", lambda d: _render_health(ctx, d))
    if a == "members":
        if args.action is None:
            return _fleet_simple(ctx, client, "MEMBERS", lambda d: _render_members(ctx, d))
        if not args.member:
            args._parser.error("fleet members {} needs an argument".format(args.action))
        line = ("MEMBER ADD " if args.action == "add" else "MEMBER REMOVE ") + args.member
        return _fleet_simple(ctx, client, line, lambda d: _render_members(ctx, d),
                             kind="mutation")
    if a == "peers":
        return _fleet_simple(ctx, client, "PEERS", _per_node_json("peers"))
    if a == "assignments":
        return _fleet_simple(ctx, client, "ASSIGNMENTS", lambda d: _render_assignments(ctx, d))
    if a == "tune":
        try:
            hz = parse_freq(args.frequency)
        except ValueError as e:
            args._parser.error(str(e))
        return _fleet_fanout(ctx, client, "FREQ {:d}".format(hz))
    if a == "gain":
        return _fleet_per_channel(ctx, client, "GAIN", _fleet_values(args, int),
                                  args.unified)
    if a == "lna-gain":
        unified = int(round(args.unified * 10.0)) if args.unified is not None else None
        return _fleet_per_channel(ctx, client, "EGAN", _fleet_values(args, float, 10.0),
                                  unified)
    if a == "bias-tee":
        return _fleet_per_channel(ctx, client, "BIAS", _fleet_values(args, int),
                                  1 if args.all == "on" else 0)
    if a == "agc":
        return _fleet_fanout(ctx, client, "AGC")
    if a == "park":
        return _fleet_fanout(ctx, client, "PARK")
    if a == "bearing":
        return _fleet_fanout(ctx, client, "ORNT {:g} {:g}".format(args.azimuth, args.elevation))
    if a == "hwc-status":
        return _fleet_simple(ctx, client, _fanout_prefix(args) + "HWSQ",
                             lambda d: _render_hwsq_table(ctx, d), json_code=_hwsq_code)
    if a == "recal":
        return _fleet_recal(ctx, client, args)
    if a == "exec":
        return _fleet_exec(ctx, client, args)
    if a == "schedule":
        return _fleet_schedule(ctx, client, args)
    if a == "rebalance":
        line = "REBALANCE"
        if args.strategy:
            line += " " + args.strategy
        if args.include_degraded:
            line += " INCLUDE_DEGRADED"
        return _fleet_simple(ctx, client, line, lambda d: _render_rebalance(ctx, d),
                             kind="schedule", json_code=_results_code)
    if a == "events":
        line = "EVENTS {:d}".format(args.n)
        if args.since:
            try:
                line += " age:{:d}".format(parse_duration(args.since))
            except ValueError as e:
                args._parser.error(str(e))
        return _fleet_simple(ctx, client, line, lambda d: _render_events(ctx, d))
    if a == "db-stats":
        return _fleet_simple(ctx, client, "DB STATS", _per_node_json("nodes"))
    if a == "router-stats":
        return _fleet_simple(ctx, client, "ROUTER STATS",
                             lambda d: _render_router("stats", ctx, d.get("stats") or {}))
    raise Failed("unknown fleet command {}".format(a), EXIT_USAGE)


def _fleet_recal(ctx, client, args):
    targets = _parse_targets(args.targets)
    if not targets:
        return _fleet_simple(ctx, client, "RECAL",
                             lambda d: _render_results(ctx, _results_of(d)), kind="mutation",
                             json_code=_results_code)
    merged, ok = {}, True
    for key in targets:
        reply = client.request("RECAL " + key, kind="mutation")
        if ctx.args.json:
            print(reply.raw)
        if not reply.ok:
            ok = False
            if not ctx.args.json:
                _reply_error_code(reply)
            continue
        merged.update(_results_of(reply.data))
    if ctx.args.json:
        return results_exit_code(summarize(merged)) if ok else EXIT_FAIL
    code = _render_results(ctx, merged)
    return code if ok else EXIT_FAIL


def _fleet_exec(ctx, client, args):
    verb = args.verb.upper()
    rest = " ".join(args.args)
    verb_line = (verb + " " + rest).strip()
    kind = "mutation" if verb.ljust(4)[:4] in MUTATING_VERBS or verb == "RECAL" else "query"
    if args.node:
        line = "NODE {} {}".format(args.node, verb_line)
    else:
        line = _fanout_prefix(args) + verb_line
    return _fleet_simple(ctx, client, line, lambda d: _render_results(ctx, _results_of(d)),
                         kind=kind, json_code=_results_code)


def _fleet_schedule(ctx, client, args):
    action = args.action
    if action == "set":
        if not args.arg:
            args._parser.error("fleet schedule set needs a FILE")
        doc = _load_schedule_file(args.arg)
        body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        line = "SCHEDULE SET" + (" " + args.strategy if args.strategy else "")
        return _fleet_simple(ctx, client, line, lambda d: _render_schedule_set(ctx, d),
                             body=body, kind="mutation")
    if action == "get":
        return _fleet_simple(ctx, client, "SCHEDULE GET",
                             lambda d: _render_schedule_get(ctx, d))
    if action == "clear":
        def render(d):
            code = _render_results(ctx, d.get("stopped") or {})
            _durability_note(d)
            return code
        return _fleet_simple(ctx, client, "SCHEDULE CLEAR", render, kind="mutation",
                             json_code=lambda d: results_exit_code(
                                 summarize(d.get("stopped") or {})))
    strategy = args.arg or args.strategy
    if strategy not in ("round_robin", "range"):
        args._parser.error("fleet schedule strategy needs round_robin or range")

    def render_strategy(d):
        print("strategy: {}".format(d.get("strategy")))
        _durability_note(d)
        return EXIT_OK
    return _fleet_simple(ctx, client, "SCHEDULE STRATEGY " + strategy, render_strategy,
                         kind="mutation")


# --------------------------------------------------------------------------
# router (stats endpoint :7001)
# --------------------------------------------------------------------------

def _render_router(view, ctx, data):
    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    clients = data.get("clients") if isinstance(data.get("clients"), list) else []
    if view == "stats":
        print("uptime {}  output :{}  tag_mode {}  clients {:d}  late_frames {}  "
              "unattributed {}".format(
                  format_age(data.get("uptime_sec")), data.get("output_port", "-"),
                  data.get("tag_mode", "-"), len(clients), data.get("late_frames", "-"),
                  data.get("unattributed_frames", "-")))
    if view in ("stats", "sources"):
        rows = [[k, s.get("state", "-"), "yes" if s.get("connected") else "no",
                 s.get("frames_received", "-"), s.get("frames_lost", "-"),
                 s.get("cpi_gaps", "-"), s.get("restarts", "-"),
                 format_age(s.get("last_frame_age_sec")), s.get("clock_offset_ms", "-")]
                for k, s in sorted(sources.items()) if isinstance(s, dict)]
        print_table(["SOURCE", "STATE", "CONNECTED", "FRAMES", "LOST", "GAPS", "RESTARTS",
                     "LAST-AGE", "OFFSET-MS"], rows, ctx.args.color, separator=False)
    if view in ("stats", "clients"):
        rows = [[c.get("peer", "-"), format_age(c.get("connected_sec")), c.get("queued", "-"),
                 c.get("sent_frames", "-"), c.get("dropped_frames", "-")]
                for c in clients if isinstance(c, dict)]
        if view == "stats":
            print()
        print_table(["CLIENT", "CONNECTED", "QUEUED", "SENT", "DROPPED"], rows,
                    ctx.args.color, separator=False)
    return EXIT_OK


def cmd_router(args):
    from .client.router import RouterStatsClient
    ctx = Context(args)
    coordinators = coordinator_endpoints(ctx.cfg, args.coordinator)
    host, port = router_endpoint(ctx.cfg, coordinators, args.router)
    ctx.log("router stats endpoint {}:{}".format(host, port))
    client = RouterStatsClient(host, port, auth_token(ctx.cfg, args.auth_token),
                               args.timeout)
    verb = args.view.upper()
    try:
        while True:
            reply = client.request(verb)
            if args.json:
                print(reply.raw)
            elif not reply.ok:
                return _reply_error_code(reply)
            else:
                if args.watch:
                    sys.stdout.write("\033[2J\033[H")
                _render_router(args.view, ctx, reply.data)
            if not args.watch:
                return EXIT_OK if reply.ok else EXIT_FAIL
            sys.stdout.flush()
            time.sleep(1.0)
    except KeyboardInterrupt:
        return EXIT_OK


# --------------------------------------------------------------------------
# Argument parser
# --------------------------------------------------------------------------

def _common_parent():
    """Global flags also accepted after the subcommand (SUPPRESS keeps the
    value given before the subcommand when the flag is not repeated)."""
    p = argparse.ArgumentParser(add_help=False)
    S = argparse.SUPPRESS
    p.add_argument("--host", default=S)
    p.add_argument("--instance", type=int, default=S)
    p.add_argument("--port-stride", type=int, default=S)
    p.add_argument("--json", action="store_true", default=S)
    p.add_argument("--strict", action="store_true", default=S)
    p.add_argument("--verify", action="store_true", default=S)
    p.add_argument("--timeout", type=float, default=S)
    p.add_argument("--ack", choices=["auto", "xack", "legacy"], default=S)
    p.add_argument("--legacy-ack", dest="ack", action="store_const", const="legacy",
                   default=S)
    p.add_argument("-v", "--verbose", action="store_true", default=S)
    return p


def build_parser():
    parser = argparse.ArgumentParser(
        prog="heimdall-ctl",
        description="HeIMDALL DAQ pipeline controller",
        epilog="exit codes: 0 ok/applied/unconfirmed (legacy server), 1 error or not "
               "applied, 2 usage, 3 connection failure/timeout, 4 not_leader, "
               "5 capacity failures")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--host", default=None, help="DAQ host (default 127.0.0.1)")
    parser.add_argument("--port-ctl", type=int, default=None)
    parser.add_argument("--port-status", type=int, default=None)
    parser.add_argument("--port-events", type=int, default=None)
    parser.add_argument("--config", default=None,
                        help="daq_chain_config.ini to read instance/ports from")
    parser.add_argument("--instance", type=int, default=None,
                        help="instance id N: ports += N * stride")
    parser.add_argument("--port-stride", type=int, default=None,
                        help="federation port stride (config value, else 100)")
    parser.add_argument("--coordinator", default=None, metavar="H:P[,H:P...]",
                        help="coordinator endpoints, tried in order on connect failure")
    parser.add_argument("--router", default=None, metavar="H:P",
                        help="router stats endpoint")
    parser.add_argument("--auth-token", default=None,
                        help="AUTH token for the coordinator/router (or HEIMDALL_AUTH_TOKEN)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="override the read timeout (default: queries 5 s, mutating "
                             "commands the server's T_cmd, fleet VERSION deadline + 3 s)")
    parser.add_argument("--ack", choices=["auto", "xack", "legacy"], default="auto",
                        help="XACK handshake before mutating commands (default auto)")
    parser.add_argument("--legacy-ack", dest="ack", action="store_const", const="legacy",
                        help="alias of --ack legacy")
    parser.add_argument("--strict", action="store_true",
                        help="refuse per-channel lists of the wrong length")
    parser.add_argument("--verify", action="store_true",
                        help="poll until the requested value is observed")
    parser.add_argument("--firmware-dir", default=None,
                        help="Firmware/ directory (capacity tool, local DB mode)")
    parser.add_argument("--json", action="store_true", help="print the raw reply")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print resolved endpoints, timeouts and the ack mode on stderr")
    parser.add_argument("--color", default="auto", choices=["auto", "never", "always"])

    common = _common_parent()
    sub = parser.add_subparsers(dest="command")

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text, parents=[common])
        p.set_defaults(func=func)
        return p

    p = add("status", cmd_status, "Show pipeline status")
    p.add_argument("--watch", action="store_true")

    p = add("tune", cmd_tune, "Set center frequency")
    p.add_argument("frequency", help="e.g. 433M, 1.2G, 868000000")

    p = add("gain", cmd_gain, "Set IF gains")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("values", nargs="?", help="Comma-separated per-channel gains (dB x 10)")
    g.add_argument("--unified", type=int, help="Same gain for all channels")

    add("agc", cmd_agc, "Enable automatic gain control")
    add("recal", cmd_recal, "Force recalibration (RECL when supported, else INIT)")
    add("metrics", cmd_metrics, "Show performance metrics")

    p = add("events", cmd_events, "Show recent events")
    p.add_argument("--tail", action="store_true", help="Live event stream (ZMQ)")
    p.add_argument("--filter", default=None, help="Comma-separated event type prefixes")
    p.add_argument("--hosts", action="append", default=None,
                   help="--tail: comma-separated hosts (repeatable)")
    p.add_argument("--instances", default=None, help="--tail: comma-separated instance ids")
    p.add_argument("--count", type=int, default=0,
                   help="--tail: exit after N events (default: run until Ctrl-C)")

    p = add("cal-history", cmd_cal_history, "Query calibration history")
    p.add_argument("--since", default=None, help="Duration, e.g. 1h, 30m, 1d")
    p.add_argument("--freq", default=None, help="Filter by frequency")
    p.add_argument("--limit", type=int, default=None, help="max records (local default 50)")
    p.add_argument("--remote", action="store_true", help="query the :5002 DB proxy")
    p.add_argument("--db-dir", default=None,
                   help="local DB dir (default: config [database] db_dir)")

    p = add("freq-scan", cmd_freq_scan, "Show per-frequency scan summary")
    p.add_argument("--freq", default=None, help="Filter by frequency")
    p.add_argument("--remote", action="store_true", help="query the :5002 DB proxy")
    p.add_argument("--db-dir", default=None,
                   help="local DB dir (default: config [database] db_dir)")

    add("db-stats", cmd_db_stats, "Show database statistics (:5002 DB_STATS)")

    p = add("schedule", cmd_schedule, "Manage signal schedule")
    p.add_argument("action", choices=["load", "stop", "query", "next"])
    p.add_argument("file", nargs="?", help="JSON schedule file (for load)")
    p.add_argument("--inline-only", action="store_true",
                   help="refuse schedules that need the chunked transfer")
    p.add_argument("--as-path", default=None, metavar="PATH",
                   help="load a schedule file that exists on the DAQ host")

    p = add("lna-gain", cmd_lna_gain, "Set external LNA gains (dB)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("values", nargs="?",
                   help="Comma-separated per-channel gains in dB (e.g. 14.5,14.5,14.5,14.5,14.5)")
    g.add_argument("--unified", type=float, help="Same gain (dB) for all channels")

    p = add("bias-tee", cmd_bias_tee, "Set per-channel inline-LNA bias tees")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("values", nargs="?",
                   help="Comma-separated per-channel states (e.g. 1,0,1,1,1)")
    g.add_argument("--all", choices=["on", "off"], help="Same state for all channels")

    p = add("bearing", cmd_bearing, "Slew the antenna to azimuth/elevation")
    p.add_argument("azimuth", type=float, help="Azimuth in degrees")
    p.add_argument("elevation", type=float, nargs="?", default=0.0,
                   help="Elevation in degrees (default 0)")

    add("park", cmd_park, "Park the antenna at its configured bearing")

    p = add("scan", cmd_scan, "Autonomous scan-and-peak control")
    p.add_argument("action", choices=["start", "stop"])

    add("orientation", cmd_orientation, "Query rotator/orientation state")
    add("rf-budget", cmd_rf_budget, "Query the RF link budget")
    add("hwc-status", cmd_hwc_status, "Query the hw_controller state (HWSQ)")

    p = add("track-lock", cmd_track_lock, "Query (TLCK) or set (TLKA) the IQ track lock")
    p.add_argument("action", nargs="?", choices=["approve", "revoke"])

    add("peers", cmd_peers, "Show this instance's federation peer table (:5002 PEERS)")
    add("ports", cmd_ports, "Show the ports of an instance")

    p = add("instances", cmd_instances, "List the instances in the host registry")
    p.add_argument("--run-dir", default=None, help="registry directory ($HEIMDALL_RUN_DIR)")
    p.add_argument("--ping", action="store_true", help="PING each status port")

    p = add("capacity", cmd_capacity, "Run the capacity pre-flight tool")
    p.add_argument("--config", dest="capacity_configs", nargs="+", default=None,
                   metavar="INI", help="instance configs (default: the resolved config)")
    p.add_argument("--add-instances", type=int, default=None)
    p.add_argument("--host-json", default=None, metavar="FILE")

    add("config-show", cmd_config_show, "Display resolved configuration")

    _add_fleet(sub, common)

    p = add("router", cmd_router, "IQ router statistics")
    p.add_argument("view", choices=["stats", "sources", "clients"])
    p.add_argument("--watch", action="store_true")
    return parser


def _add_fleet(sub, common):
    fleet = sub.add_parser("fleet", help="Cluster commands via the coordinator",
                           parents=[common])
    fleet.set_defaults(func=cmd_fleet)
    fsub = fleet.add_subparsers(dest="fleet_command")
    fsub.required = True

    def add(name, help_text):
        return fsub.add_parser(name, help=help_text, parents=[common])

    p = add("status", "NODES STATUS + HEALTH table")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--fresh", action="store_true", help="force a live member poll")
    for name, text in (("ping", "PING the coordinator"), ("version", "coordinator VERSION"),
                       ("leader", "election state (LEADER)"), ("health", "member health"),
                       ("peers", "each instance's PEERS view"),
                       ("assignments", "schedule assignments"),
                       ("db-stats", "DB_STATS of every member"),
                       ("router-stats", "router statistics via the coordinator")):
        add(name, text)
    p = add("members", "list / add / remove runtime members")
    p.add_argument("action", nargs="?", choices=["add", "remove"])
    p.add_argument("member", nargs="?", metavar="HOST:IID[@NODE] | NODE_KEY")

    def targets(p):
        p.add_argument("--targets", default=None, metavar="K1,K2",
                       help="restrict to these node keys (FANOUT)")
        return p

    p = targets(add("tune", "FREQ on every member"))
    p.add_argument("frequency")
    p = targets(add("gain", "GAIN on every member"))
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("values", nargs="?", help="comma-separated per-channel gains (dB x 10)")
    g.add_argument("--unified", type=int)
    p = targets(add("lna-gain", "EGAN on every member"))
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("values", nargs="?", help="comma-separated per-channel gains in dB")
    g.add_argument("--unified", type=float)
    p = targets(add("bias-tee", "BIAS on every member"))
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("values", nargs="?", help="comma-separated per-channel states")
    g.add_argument("--all", choices=["on", "off"])
    targets(add("agc", "AGC on every member"))
    targets(add("recal", "RECAL (RECL, INIT fallback) on every member"))
    targets(add("park", "PARK on every member"))
    p = targets(add("bearing", "ORNT on every member"))
    p.add_argument("azimuth", type=float)
    p.add_argument("elevation", type=float, nargs="?", default=0.0)
    targets(add("hwc-status", "HWSQ of every member"))
    p = targets(add("exec", "generic ALL / NODE / FANOUT command"))
    p.add_argument("--node", default=None, metavar="K")
    p.add_argument("verb")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p = add("schedule", "master schedule")
    p.add_argument("action", choices=["set", "get", "clear", "strategy"])
    p.add_argument("arg", nargs="?", metavar="FILE|STRATEGY")
    p.add_argument("--strategy", choices=["round_robin", "range"], default=None)
    p = add("rebalance", "partition and distribute the master schedule")
    p.add_argument("--strategy", choices=["round_robin", "range"], default=None)
    p.add_argument("--include-degraded", action="store_true")
    p = add("events", "merged member events")
    p.add_argument("-n", type=int, default=100)
    p.add_argument("--since", default=None, help="e.g. 10m (coordinator clock)")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_OK
    args._parser = parser  # for usage errors raised inside command handlers
    try:
        code = args.func(args)
    except Failed as e:
        print("Error: {}".format(e), file=sys.stderr)
        code = e.code
    except XackRefused as e:
        print("Error: {}".format(e), file=sys.stderr)
        code = EXIT_FAIL
    except NotLeader as e:
        print("Error: {}".format(e), file=sys.stderr)
        code = EXIT_NOT_LEADER
    except CoordinatorError as e:
        if e.reply.data.get("error"):
            code = _reply_error_code(e.reply)
        else:
            print("Error: {}:{}: bad VERSION reply: {}".format(
                e.reply.endpoint[0], e.reply.endpoint[1], e.reply.raw), file=sys.stderr)
            code = EXIT_FAIL
    except EndpointError as e:
        print("Error: {}:{}: {}".format(e.host, e.port, e.reason), file=sys.stderr)
        code = EXIT_CONN
    except ValueError as e:
        print("Error: {}".format(e), file=sys.stderr)
        code = EXIT_FAIL
    return EXIT_OK if code is None else code


def _entry():
    sys.exit(main())


if __name__ == "__main__":
    _entry()
