#!/usr/bin/env python3
"""
   HeIMDALL DAQ pre-flight capacity check (spec I.2).

   Checks whether the instances described by the given INI files (plus the
   instances already running on this host and hypothetical extra ones) fit
   this host: cores, memory, /dev/shm, RLIMIT_MEMLOCK, USB bus bandwidth,
   NIC egress, ports, receiver serials, frame period, router memory and the
   hard size limits. Each resource gets need / have / verdict (ok, warn,
   fail, skip).

   python3 daq_preflight.py [--config INI ...] [--plus-live] [--add-instances N]
                            [--host-json FILE|-] [--strict] [--json]
   python3 daq_preflight.py --dump-host-json

   Exit status: 0 ok, 1 warnings, 2 failures (with --strict warnings count
   as failures), 3 usage, configuration or host-JSON error (also any
   internal error: a crash never exits 1, which means "warnings").

   The start scripts run it when [host] preflight = warn|strict
   (hd_preflight in daq_common.sh); heimdall-ctl capacity wraps it.

   Project: HeIMDALL DAQ Firmware
   License: GNU GPL V3
"""
import argparse
import configparser
import glob
import json
import os
import subprocess
import sys

FIRMWARE_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(FIRMWARE_DIR, "_daq_core"))

import capacity_model as cm  # noqa: E402

DEFAULT_CONFIG = "daq_chain_config.ini"
DEFAULT_RUN_DIR = "/run/heimdall"
DEFAULT_SYSFS_USB_ROOT = "/sys/bus/usb/devices"
DEFAULT_NET_ROOT = "/sys/class/net"
DEFAULT_PROC_ROOT = "/proc"
DEFAULT_SHM_PATH = "/dev/shm"
MODEL_PATHS = ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model")
HOST_SCHEMA = 1

EXIT_OK, EXIT_WARN, EXIT_FAIL, EXIT_USAGE = 0, 1, 2, 3


class UsageError(Exception):
    """Bad arguments, unreadable INI or invalid --host-json (exit 3)."""


# ---------------------------------------------------------------------------
# INI files
# ---------------------------------------------------------------------------

def read_ini(path):
    """{section: {key: value}} of *path*; UsageError when unreadable."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        found = parser.read([path])
    except configparser.Error as e:
        raise UsageError("cannot parse {0}: {1}".format(path, e))
    if not found:
        raise UsageError("configuration file not found: {0}".format(path))
    return {s: dict(parser.items(s)) for s in parser.sections()}


# ---------------------------------------------------------------------------
# Host probes (Linux; every probe that is unavailable leaves its key out)
# ---------------------------------------------------------------------------

def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace").replace("\x00", "").strip()
    except OSError:
        return None


def _nproc():
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count()


def _mem_available(proc_root):
    text = _read(os.path.join(proc_root, "meminfo"))
    for line in (text or "").splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def _memlock():
    try:
        import resource
    except ImportError:
        return None
    soft = resource.getrlimit(resource.RLIMIT_MEMLOCK)[0]
    return -1 if soft == resource.RLIM_INFINITY else int(soft)


def _shm(shm_path):
    if not os.path.isdir(shm_path):
        return None
    try:
        st = os.statvfs(shm_path)
    except OSError:
        return None
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def _speed(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _usb_buses(sysfs_root):
    """[{bus, speed_mbps, devices: [{serial, devnum}]}] from sysfs, None when
    the USB device tree is absent. Receivers come from hw_discover's
    discover_rtlsdr() (the source of `hw_discover.py --sdr`)."""
    if not os.path.isdir(sysfs_root):
        return None
    try:
        from hw_discover import discover_rtlsdr
    except ImportError:
        return None
    buses = {}
    for path in sorted(glob.glob(os.path.join(sysfs_root, "usb[0-9]*"))):
        name = os.path.basename(path)
        bus = _read(os.path.join(path, "busnum"))
        bus = int(bus) if bus and bus.isdigit() else int(name[3:]) if name[3:].isdigit() else None
        if bus is None:
            continue
        speed = _speed(_read(os.path.join(path, "speed")))
        entry = {"bus": bus}
        if speed is not None:
            entry["speed_mbps"] = int(speed) if speed == int(speed) else speed
        entry["devices"] = []
        buses[bus] = entry
    for dev in discover_rtlsdr(sysfs_root)["devices"]:
        bus = dev.get("busnum")
        if bus is None:
            continue
        entry = buses.setdefault(bus, {"bus": bus, "speed_mbps": cm.HIGH_SPEED_MBPS, "devices": []})
        entry["devices"].append({"serial": dev.get("serial"), "devnum": dev.get("devnum")})
    return [buses[b] for b in sorted(buses)]


def _nics(net_root):
    if not os.path.isdir(net_root):
        return None
    out = []
    for name in sorted(os.listdir(net_root)):
        # lo and tun/tap devices (OpenVPN, Tailscale: tun drivers report a
        # fixed 10 Mbit/s) cannot carry the IQ stream at a meaningful speed.
        if name == "lo" or os.path.exists(os.path.join(net_root, name, "tun_flags")):
            continue
        speed = _speed(_read(os.path.join(net_root, name, "speed")))
        if speed is not None and speed > 0:
            out.append({"name": name, "speed_mbps": int(speed)})
    return out


def _model():
    for path in MODEL_PATHS:
        text = _read(path)
        if text:
            return text
    return None


def run_dirs(firmware_dir=None):
    """Registry directories: $HEIMDALL_RUN_DIR (default /run/heimdall), the
    /tmp/heimdall-run-<uid> fallback and the run_dir hints of this copy."""
    dirs = [os.environ.get("HEIMDALL_RUN_DIR") or DEFAULT_RUN_DIR]
    if hasattr(os, "getuid"):
        dirs.append("/tmp/heimdall-run-{0}".format(os.getuid()))
    for hint in sorted(glob.glob(os.path.join(firmware_dir or ".", "_logs", "inst*", "pids", "run_dir"))):
        line = (_read(hint) or "").splitlines()
        if line and line[0].strip():
            dirs.append(line[0].strip())
    return list(dict.fromkeys(os.path.abspath(d) for d in dirs))


def _process_args(pid, proc_root):
    text = _read(os.path.join(proc_root, str(pid), "cmdline"))
    if text is not None or os.path.isdir(proc_root):
        return text or ""
    try:
        res = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def pid_role_alive(pid, role, proc_root=DEFAULT_PROC_ROOT):
    """The hd_pid_role_alive rule: pid runs and its command line contains role."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    return str(role) in _process_args(pid, proc_root)


def read_live_entries(dirs, proc_root=DEFAULT_PROC_ROOT):
    """Live registry entries ($RUN/inst<N>.json whose pids run, B.3.1)."""
    live, seen = [], set()
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "inst*.json"))):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("pids"), dict):
                continue
            if any(pid_role_alive(pid, role, proc_root) for role, pid in entry["pids"].items()):
                live.append(entry)
    live.sort(key=lambda e: (e.get("instance_id", 0) if isinstance(e.get("instance_id"), int) else 0))
    return live


def host_probe(sysfs_usb_root=None, net_root=DEFAULT_NET_ROOT, proc_root=DEFAULT_PROC_ROOT,
               shm_path=DEFAULT_SHM_PATH, registry_dirs=None, firmware_dir=None):
    """This host's values in the --host-json schema (spec I.2)."""
    host = {"schema": HOST_SCHEMA}
    nproc = _nproc()
    if nproc:
        host["nproc"] = nproc
    model = _model()
    if model:
        host["model"] = model
    mem = _mem_available(proc_root)
    if mem is not None:
        host["mem_available"] = mem
    shm = _shm(shm_path)
    if shm is not None:
        host["shm_size"], host["shm_free"] = shm
    memlock = _memlock()
    if memlock is not None:
        host["memlock"] = memlock
    sysfs_usb_root = sysfs_usb_root or os.environ.get("HEIMDALL_SYSFS_ROOT") or DEFAULT_SYSFS_USB_ROOT
    buses = _usb_buses(sysfs_usb_root)
    if buses is not None:
        host["usb_buses"] = buses
    nics = _nics(net_root)
    if nics is not None:
        host["nics"] = nics
    dirs = registry_dirs if registry_dirs is not None else run_dirs(firmware_dir)
    host["live"] = read_live_entries(dirs, proc_root)
    return host


# ---------------------------------------------------------------------------
# --host-json validation
# ---------------------------------------------------------------------------

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def live_entry_problem(entry):
    """None when a registry entry (B.3.1) carries usable values for the
    fields the model reads, else a message naming the first bad field."""
    if not isinstance(entry, dict):
        return "an entry is not an object"
    if not _is_int(entry.get("instance_id")) or entry["instance_id"] < 0:
        return "'live[].instance_id' must be an integer >= 0"
    if "mode" in entry and entry["mode"] not in ("hw", "synthetic"):
        return "'live[].mode' must be hw or synthetic"
    cores = entry.get("cores")
    if cores is not None and not (isinstance(cores, list) and all(_is_int(c) and c >= 0 for c in cores)):
        return "'live[].cores' must be a list of integers >= 0"
    ports = entry.get("ports")
    if ports is not None and not (isinstance(ports, dict) and all(
            v is None or (_is_int(v) and v >= 0) for v in ports.values())):
        return "'live[].ports' must map names to integers >= 0 or null"
    serials = entry.get("serials")
    if serials is not None and not (isinstance(serials, list) and all(isinstance(x, str) for x in serials)):
        return "'live[].serials' must be a list of strings"
    if "rt_priority_offset" in entry and not (_is_int(entry["rt_priority_offset"])
                                              and entry["rt_priority_offset"] >= 0):
        return "'live[].rt_priority_offset' must be an integer >= 0"
    if "pids" in entry and not isinstance(entry["pids"], dict):
        return "'live[].pids' must be an object"
    if "cwd" in entry and not (entry["cwd"] is None or isinstance(entry["cwd"], str)):
        return "'live[].cwd' must be a string"
    return None


def validate_host_json(doc):
    """Raise UsageError unless *doc* follows the --host-json schema."""
    def bad(key, what):
        raise UsageError("host JSON: '{0}' must be {1}".format(key, what))

    if not isinstance(doc, dict):
        raise UsageError("host JSON: the document must be an object")
    if "schema" in doc and doc["schema"] != HOST_SCHEMA:
        bad("schema", "1")
    if "nproc" in doc and not (_is_int(doc["nproc"]) and doc["nproc"] >= 1):
        bad("nproc", "a positive integer")
    if "model" in doc and not isinstance(doc["model"], str):
        bad("model", "a string")
    for key in ("mem_available", "shm_size", "shm_free"):
        if key in doc and not (_is_int(doc[key]) and doc[key] >= 0):
            bad(key, "a byte count >= 0")
    if "memlock" in doc and not (_is_int(doc["memlock"]) and doc["memlock"] >= -1):
        bad("memlock", "a byte count or -1 (unlimited)")
    if "usb_buses" in doc:
        if not isinstance(doc["usb_buses"], list):
            bad("usb_buses", "a list")
        for b in doc["usb_buses"]:
            if not isinstance(b, dict) or not _is_int(b.get("bus")):
                bad("usb_buses[].bus", "an integer")
            if "speed_mbps" in b and not (_is_num(b["speed_mbps"]) and b["speed_mbps"] > 0):
                bad("usb_buses[].speed_mbps", "a positive number")
            if not isinstance(b.get("devices", []), list):
                bad("usb_buses[].devices", "a list")
            for d in b.get("devices", []):
                if not isinstance(d, dict) or not (d.get("serial") is None or isinstance(d["serial"], str)):
                    bad("usb_buses[].devices[].serial", "a string or null")
    if "nics" in doc:
        if not isinstance(doc["nics"], list):
            bad("nics", "a list")
        for n in doc["nics"]:
            if not isinstance(n, dict) or not isinstance(n.get("name"), str) \
                    or not _is_num(n.get("speed_mbps")):
                bad("nics[]", "objects with a name and a numeric speed_mbps")
    if "live" in doc:
        if not isinstance(doc["live"], list) or not all(isinstance(e, dict) for e in doc["live"]):
            bad("live", "a list of registry entries")
        for e in doc["live"]:
            problem = live_entry_problem(e)
            if problem:
                raise UsageError("host JSON: {0}".format(problem))
    return doc


def load_host_json(path):
    try:
        if path == "-":
            doc = json.load(sys.stdin)
        else:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
    except OSError as e:
        raise UsageError("cannot read host JSON {0}: {1}".format(path, e))
    except ValueError as e:
        raise UsageError("host JSON {0} is not valid JSON: {1}".format(path, e))
    return validate_host_json(doc)


# ---------------------------------------------------------------------------
# Plan assembly
# ---------------------------------------------------------------------------

def build_plan(configs, host, plus_live=False, add_instances=0, mode="hw", live_configs=True):
    """(instances, federations, notes) for cm.evaluate().

    live_configs: read a live entry's INI from its cwd (only for entries of
    this host's registry, not for --host-json entries of another machine).
    """
    nproc = host.get("nproc")
    instances, federations, notes = [], [], []
    for path in configs:
        params = read_ini(path)
        try:
            if cm.is_federation_ini(params):
                federations.append(cm.federation_from_params(params, os.path.abspath(path)))
            else:
                instances.append(cm.instance_from_params(params, os.path.abspath(path), mode, nproc))
        except ValueError as e:
            raise UsageError("{0}: {1}".format(path, e))
    given_ids = {i["instance_id"] for i in instances}
    if plus_live:
        for entry in host.get("live") or []:
            problem = live_entry_problem(entry)
            if problem:
                # only this host's registry gets here: --host-json is validated
                notes.append("live registry entry ignored: {0}".format(problem))
                continue
            iid = entry.get("instance_id")
            if iid in given_ids:
                notes.append("live instance {0} is replaced by its given configuration".format(iid))
                continue
            params = None
            cwd = entry.get("cwd")
            if live_configs and isinstance(cwd, str):
                path = os.path.join(cwd, DEFAULT_CONFIG)
                if os.path.isfile(path):
                    try:
                        params = read_ini(path)
                    except UsageError as e:
                        notes.append("live instance {0}: {1}".format(iid, e))
            try:
                inst = cm.instance_from_registry(entry, params, nproc)
            except (TypeError, ValueError) as e:
                notes.append("live instance {0}: {1}; using defaults".format(iid, e))
                inst = cm.instance_from_registry(entry, None, nproc)
            if not inst["params_known"]:
                notes.append("live instance {0}: configuration not readable; sizes assume a default "
                             "{1}-channel instance".format(iid, inst["num_ch"]))
            instances.append(inst)
    if add_instances:
        templates = [i for i in instances if i["source"] == "config"]
        if not templates:
            raise UsageError("--add-instances needs an instance configuration to copy")
        used = {i["instance_id"] for i in instances}
        next_id = max(used) + 1 if used else 0
        for _ in range(add_instances):
            while next_id in used:
                next_id += 1
            instances.append(cm.planned_instance(templates[-1], next_id, nproc))
            used.add(next_id)
        notes.append("{0} planned instance(s) copy {1} with the provisioning defaults "
                     "(serial_base 1000+100*N, cpu_cores 4N..4N+3)".format(
                         add_instances, templates[-1]["config"]))
    return instances, federations, notes


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt(value, unit):
    if value is None:
        return "-"
    if unit == "bytes":
        if value < 0:
            return "unlimited"
        if value >= 1 << 30:
            return "{0:.2f} GiB".format(value / float(1 << 30))
        return "{0:.1f} MiB".format(value / float(cm.MIB))
    if unit == "MB/s":
        return "{0:.1f} MB/s".format(value)
    if unit == "s":
        return "{0:.3f} s".format(value)
    return "{0} {1}".format(value, unit)


def format_text(report, notes):
    host = report["host"]
    lines = ["HeIMDALL DAQ pre-flight - host {0}: nproc {1}, model {2}".format(
        "probed" if host["probed"] else "from --host-json",
        host["nproc"] if host["nproc"] is not None else "?", host["model"] or "-")]
    for i in report["instances"]:
        ports = " ".join("{0} {1}".format(k, v) for k, v in i["ports"].items() if v is not None)
        lines.append("  instance {0:<3} {1:<9} {2:<7} cores {3:<12} ports {4}  {5}".format(
            i["instance_id"], i["mode"], i["source"], ",".join(str(c) for c in i["cores"]),
            ports, i["config"] or ""))
    lines.append("{0:<16} {1:>14} {2:>14}  {3:<7} {4}".format("RESOURCE", "NEED", "HAVE", "VERDICT", "NOTE"))
    for name, r in report["resources"].items():
        lines.append("{0:<16} {1:>14} {2:>14}  {3:<7} {4}".format(
            name, _fmt(r["need"], r["unit"]), _fmt(r["have"], r["unit"]), r["verdict"], r["note"]))
    for n in notes:
        lines.append("note: " + n)
    lines.append("result: {0} (exit {1})".format(("ok", "warn", "fail")[report["exit"]], report["exit"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "{0}: error: {1}\n".format(self.prog, message))


def build_parser():
    p = _Parser(prog="daq_preflight.py",
                description="Check whether HeIMDALL DAQ instances fit this host (spec I.2). "
                            "Exit 0 ok, 1 warnings, 2 failures, 3 usage error.")
    p.add_argument("--config", nargs="+", action="extend", metavar="INI",
                   help="instance INI(s) and/or a federation.ini (default: ./daq_chain_config.ini)")
    p.add_argument("--plus-live", action="store_true",
                   help="also count the live instances of the host registry "
                        "(only chains started with [host] instance_registry = 1)")
    p.add_argument("--add-instances", type=int, default=0, metavar="N",
                   help="add N planned copies of the last instance INI "
                        "(provisioning defaults for serials, cores and ports)")
    p.add_argument("--host-json", metavar="FILE",
                   help="use host values from FILE ('-' = stdin) instead of probing this host")
    p.add_argument("--dump-host-json", action="store_true",
                   help="print this host's probed values in the --host-json schema and exit")
    p.add_argument("--mode", choices=("hw", "synthetic"), default="hw",
                   help="mode of the given instances (synthetic: no USB receivers)")
    p.add_argument("--usb-budget", type=float, default=cm.DEFAULT_USB2_BUS_BUDGET_MBPS, metavar="MBPS",
                   help="usable MB/s of one USB 2.0 high-speed bus (default %(default)g)")
    p.add_argument("--strict", action="store_true", help="count warnings as failures (exit 2)")
    p.add_argument("--json", action="store_true", help="print the JSON report")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except UsageError as e:
        sys.stderr.write("daq_preflight.py: error: {0}\n".format(e))
        return EXIT_USAGE
    except Exception as e:  # noqa: BLE001 - exit 1 means "warnings"; never report a crash as that
        sys.stderr.write("daq_preflight.py: internal error: {0}: {1}\n".format(type(e).__name__, e))
        return EXIT_USAGE


def _run(args):
    if args.add_instances < 0:
        raise UsageError("--add-instances must be >= 0")
    if args.usb_budget <= 0:
        raise UsageError("--usb-budget must be > 0")
    if args.dump_host_json:
        print(json.dumps(host_probe(firmware_dir=os.getcwd()), indent=2))
        return EXIT_OK
    if args.host_json:
        host = dict(load_host_json(args.host_json))
        host["probed"] = False
    else:
        host = host_probe(firmware_dir=os.getcwd())
        host["probed"] = True
    instances, federations, notes = build_plan(
        args.config or [DEFAULT_CONFIG], host, args.plus_live, args.add_instances,
        args.mode, live_configs=not args.host_json)
    report = cm.evaluate(instances, host, federations, args.usb_budget, args.strict)
    if notes:
        report["notes"] = notes
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_text(report, notes))
    return report["exit"]


if __name__ == "__main__":
    sys.exit(main())
