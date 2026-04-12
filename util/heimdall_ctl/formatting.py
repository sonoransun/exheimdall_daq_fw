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
    return int(val * mult.get(suffix, 1))


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


def print_table(headers, rows, color_mode="auto"):
    """Print a simple aligned table."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    header_line = fmt.format(*headers)
    if _use_color(color_mode):
        print(colorize(header_line, "bold", color_mode))
    else:
        print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))
