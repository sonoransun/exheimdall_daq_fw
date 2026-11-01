"""ANSI formatting, SI units, duration parsing for CLI output."""
import json
import re
import sys
import time


def parse_freq(s):
    """Parse frequency string like '433M', '1.2G', '868000000' to Hz."""
    s = s.strip().upper()
    m = re.match(r'^([0-9.]+)\s*([KMGT]?)(?:HZ)?$', s)
    if not m:
        raise ValueError(f"Cannot parse frequency: {s}")
    val = float(m.group(1))
    suffix = m.group(2)
    mult = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    return int(round(val * mult.get(suffix, 1)))


def format_freq(hz):
    """Format Hz as human-readable."""
    if hz >= 1e9:
        return f"{hz / 1e9:.3f} GHz"
    elif hz >= 1e6:
        return f"{hz / 1e6:.3f} MHz"
    elif hz >= 1e3:
        return f"{hz / 1e3:.1f} kHz"
    return f"{hz} Hz"


def parse_duration(s):
    """Parse duration like '5m', '2h', '1d', '30s' to seconds."""
    m = re.match(r'^(\d+)\s*([smhd])$', s.strip().lower())
    if not m:
        raise ValueError(f"Cannot parse duration: {s}")
    val = int(m.group(1))
    unit = m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * mult[unit]


def duration_since_ms(since_str):
    """Convert a --since string to an epoch millisecond timestamp."""
    secs = parse_duration(since_str)
    return int((time.time() - secs) * 1000)


def format_ts(ts):
    """Unix seconds -> local ISO-8601 time with milliseconds."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "-"
    ms = int(round(ts * 1000))   # round once, so x.9996 carries into the next second
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ms // 1000))
    return "{}.{:03d}".format(base, ms % 1000)


def format_age(sec):
    if sec is None:
        return "-"
    try:
        return "{:.1f}s".format(float(sec))
    except (TypeError, ValueError):
        return "-"


def format_ack(ack, elapsed_ms=None):
    """One-line outcome of a mutating command (spec F.2).

    ack: {"applied": True|False|None, "reason", "fsm", "legacy"} (parse_ack)."""
    if ack.get("legacy"):
        return "sent; server does not confirm application (legacy server)"
    fsm = ack.get("fsm")
    fsm_part = "fsm={}".format(fsm) if fsm else ""
    applied = ack.get("applied")
    reason = ack.get("reason")
    if applied is True:
        parts = [p for p in (fsm_part, "{:d} ms".format(int(elapsed_ms))
                             if elapsed_ms is not None else "") if p]
        return "applied" + (" ({})".format(", ".join(parts)) if parts else "")
    suffix = " ({})".format(fsm_part) if fsm_part else ""
    if applied is False:
        return "NOT applied: {}{}".format(reason or "unknown", suffix)
    if reason == "held_for_burst":
        return "held until the calibration burst ends" + suffix
    return "outcome unknown: {}{}".format(reason or "no confirmation", suffix)


def ack_exit_code(ack):
    """0 applied / held_for_burst / unconfirmed legacy; 1 applied:false or an
    unknown outcome reported by an extended server (handler_timeout)."""
    if ack.get("legacy"):
        return 0
    if ack.get("applied") is True:
        return 0
    if ack.get("applied") is None and ack.get("reason") == "held_for_burst":
        return 0
    return 1


# ANSI color helpers
_COLORS = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _use_color(mode):
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty()


def colorize(text, color, mode="auto"):
    if not _use_color(mode):
        return text
    return _COLORS.get(color, "") + text + _COLORS["reset"]


def severity_color(severity):
    return {"info": "green", "warning": "yellow", "error": "red"}.get(
        severity, "reset")


def print_json(data):
    print(json.dumps(data, indent=2, default=str))


def print_table(headers, rows, color_mode="auto", separator=True):
    """Print a simple aligned table."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    header_line = fmt.format(*headers)
    if not separator:  # compact form used by the newer subcommands
        header_line = header_line.rstrip()
    if _use_color(color_mode):
        print(colorize(header_line, "bold", color_mode))
    else:
        print(header_line)
    if separator:
        print("-" * len(header_line))
    for row in rows:
        line = fmt.format(*[str(c) for c in row])
        print(line if separator else line.rstrip())
