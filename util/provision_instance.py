#!/usr/bin/env python3
"""
    Provision a HeIMDALL DAQ instance directory (design spec B.1, H.1).

    One install root (e.g. /opt/heimdall, holding Firmware/, util/ and
    config_files/) serves every instance of a host. Instance 0 runs in
    <root>/Firmware itself; every instance N > 0 runs in its own directory
    <root>/instances/<N> whose code is symlinked into the root and whose
    daq_chain_config.ini, _data_control/, _logs/ and _db/ are private. Every
    pipeline stage reads its config and control files relative to its cwd, so
    this isolates instances without any config-path option.

        sudo python3 util/provision_instance.py --instance 1            # dry run
        sudo python3 util/provision_instance.py --instance 1 --write
        sudo python3 util/provision_instance.py --instance 0 --in-place --write
        sudo python3 util/provision_instance.py --instance 1 --refresh --write
        # after repointing /opt/heimdall to a new tree (H.8 upgrade):
        sudo python3 util/provision_instance.py --instance 0 --refresh --from-root /opt/heimdall-<old> --write
        sudo python3 util/provision_instance.py --instance 1 --refresh --from-root /opt/heimdall-<old> --write

    Default is a dry run (directory plan, INI diff, port table). Exit codes:
    0 ok (or dry run ok), 1 validation or write failed, 2 usage, 3 destination
    exists (without --force/--refresh), 4 instance live (--refresh without
    --force).

    Standard library only. The INI is produced by a line-based key rewriter
    (IniDocument) that keeps comments, order and unknown keys.

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import argparse
import configparser
import difflib
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

EXIT_OK, EXIT_INVALID, EXIT_USAGE, EXIT_EXISTS, EXIT_LIVE = 0, 1, 2, 3, 4

INI_NAME = "daq_chain_config.ini"
DEFAULT_INSTALL_ROOT = "/opt/heimdall"
DEFAULT_RUN_DIR = "/run/heimdall"
DEFAULT_STRIDE = 100
DEFAULT_SERIAL_BASE = 1000
SERIAL_UNIT_STRIDE = 100
#: Firmware entries that stay private (real files/directories) in an instance dir
PRIVATE_DIRS = ("_data_control", "_logs", "_db")
#: Firmware entries never linked or copied into an instance dir
SKIP_ENTRIES = frozenset(PRIVATE_DIRS + (INI_NAME, "__pycache__"))
#: Private Firmware entries --from-root carries into a new tree (the logs stay behind:
#: they are history, and their pids/ files belong to the old tree's processes)
MIGRATED_DIRS = ("_data_control", "_db")
#: Entries an install root must have to be provisioned from (B.1)
REQUIRED_FIRMWARE = ("_daq_core", "ini_checker.py", "daq_start_sm.sh", "daq_stop.sh")
#: Root-level directories linked next to Firmware/ (so ../util/... resolves)
ROOT_LINKS = ("util", "config_files")
#: Host names shipped by common fleet images (C.3)
COMMON_IMAGE_HOSTNAMES = ("raspberrypi", "krakensdr", "kraken", "ubuntu", "debian",
                          "dietpi", "orangepi", "armbian", "localhost")
#: packaging/default/heimdall-daq (H.5) when the template is not installed
FALLBACK_ENV_TEMPLATE = ("PATH=/opt/miniforge3/envs/kraken/bin:/usr/local/sbin:/usr/local/bin:"
                         "/usr/sbin:/usr/bin:/sbin:/bin\n"
                         "HEIMDALL_RUN_DIR=/run/heimdall\n")
#: Flags that rewrite INI keys (refused with --from-config and --refresh)
KEY_FLAGS = (("serial_base", "--serial-base"), ("device_serials", "--device-serials"),
             ("unit_id", "--unit-id"), ("cores", "--cores"),
             ("delay_sync_cores", "--delay-sync-cores"), ("rt_offset", "--rt-offset"),
             ("stride", "--stride"), ("antenna_switch_gpio", "--antenna-switch-gpio"),
             ("cluster_members", "--cluster-members"), ("node_id", "--node-id"),
             ("cluster_id", "--cluster-id"), ("coordinator", "--coordinator"),
             ("advertise_host", "--advertise-host"), ("register", "--register"),
             ("profile", "--profile"))
PROFILES = ("minimal", "balanced", "maximum")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class UsageError(Exception):
    """Bad arguments or inputs: exit 2."""


class ProvisionError(Exception):
    """A refusal or failure with its own exit code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Line-based INI rewriter
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY_RE = re.compile(r"^(?P<key>[^\s=:;#\[][^=:]*?)(?P<sep>\s*[=:]\s*)(?P<value>.*)$")


def _is_comment(line):
    s = line.strip()
    return s.startswith(";") or s.startswith("#")


class IniDocument:
    """An INI file as lines, edited key by key.

    Keys are matched case-insensitively within their section (ConfigParser
    lower-cases option names). ``set()`` rewrites the value of an existing
    key in place (keeping the key spelling and delimiter, dropping indented
    continuation lines), adds a missing key after the last key line of its
    section, and adds a missing section at the end of the file. Comments,
    blank lines, order and unknown keys are preserved byte for byte.
    """

    def __init__(self, text):
        self.lines = text.splitlines()
        self.trailing_newline = text.endswith("\n") or not text

    def text(self):
        out = "\n".join(self.lines)
        return out + "\n" if self.lines and self.trailing_newline else out

    def _sections(self):
        """[(name, header_index, end_index)] where end is exclusive."""
        found = []
        for i, line in enumerate(self.lines):
            m = _SECTION_RE.match(line)
            if m and not _is_comment(line):
                found.append([m.group(1).strip(), i, None])
        for k, entry in enumerate(found):
            entry[2] = found[k + 1][1] if k + 1 < len(found) else len(self.lines)
        return [tuple(e) for e in found]

    def _find_section(self, section):
        for name, start, end in self._sections():
            if name == section:
                return start, end
        return None

    def _key_lines(self, start, end):
        """[(key_lower, index, last_index)] of the key lines of a section."""
        out = []
        i = start + 1
        while i < end:
            line = self.lines[i]
            if line.strip() and not _is_comment(line) and not line[0].isspace():
                m = _KEY_RE.match(line)
                if m:
                    last = i
                    j = i + 1
                    while j < end and self.lines[j].strip() and self.lines[j][0].isspace() \
                            and not _is_comment(self.lines[j]):
                        last = j
                        j += 1
                    out.append((m.group("key").strip().lower(), i, last))
                    i = last + 1
                    continue
            i += 1
        return out

    def sections(self):
        return [name for name, _, _ in self._sections()]

    def has_section(self, section):
        return self._find_section(section) is not None

    def get(self, section, key):
        """Value of *key* (single-line part, stripped) or None when absent."""
        span = self._find_section(section)
        if span is None:
            return None
        for k, i, _ in self._key_lines(*span):
            if k == key.lower():
                return _KEY_RE.match(self.lines[i]).group("value").strip()
        return None

    def keys(self, section):
        span = self._find_section(section)
        return [] if span is None else [k for k, _, _ in self._key_lines(*span)]

    def set(self, section, key, value):
        value = "" if value is None else str(value)
        if "\n" in value or "\r" in value:
            raise ValueError("value of [{0}] {1} must be a single line".format(section, key))
        span = self._find_section(section)
        if span is None:
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines.append("[{0}]".format(section))
            self.lines.append(_format_line(key, value))
            self.trailing_newline = True
            return
        start, end = span
        key_lines = self._key_lines(start, end)
        for k, i, last in key_lines:
            if k == key.lower():
                m = _KEY_RE.match(self.lines[i])
                sep = m.group("sep")
                if not value:
                    sep = sep.rstrip()
                elif sep[0].isspace() and not sep[-1].isspace():
                    sep += " "  # "key =" -> "key = value"
                self.lines[i:last + 1] = [m.group("key") + sep + value]
                return
        insert_at = (key_lines[-1][2] + 1) if key_lines else start + 1
        self.lines.insert(insert_at, _format_line(key, value))


def _format_line(key, value):
    return "{0} = {1}".format(key, value) if value else "{0} =".format(key)


def read_ini_params(path):
    """ConfigParser sections of *path* as {section: {key: value}}; UsageError when unreadable."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, "r") as f:
            parser.read_file(f)
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError("cannot read {0}: {1}".format(path, e))
    except configparser.Error as e:
        raise UsageError("cannot parse {0}: {1}".format(path, e))
    return {s: dict(parser.items(s, raw=True)) for s in parser.sections()}


# ---------------------------------------------------------------------------
# Helpers shared with util/cluster_cfg_gen.py
# ---------------------------------------------------------------------------

def strict_int(text):
    s = str(text).strip()
    return int(s) if re.match(r"^[+-]?[0-9]+$", s) else None


def resolve_serials(hw):
    """Per-channel serials of a [hw] dict (A.1 rule), None when not resolvable."""
    num_ch = strict_int(hw.get("num_ch", ""))
    if num_ch is None or num_ch < 1:
        return None
    listed = [x.strip() for x in hw.get("device_serials", "").split(",") if x.strip()]
    if listed:
        return listed
    base = strict_int(hw.get("serial_base", DEFAULT_SERIAL_BASE))
    if base is None:
        return None
    return [str(base + i) for i in range(num_ch)]


def control_channel_index(params):
    """Channel index of [daq] ctr_channel_serial_no in the template's serials (0 when not found)."""
    serials = resolve_serials(params.get("hw", {})) or []
    ctr = strict_int(params.get("daq", {}).get("ctr_channel_serial_no", ""))
    if ctr is not None and str(ctr) in serials:
        return serials.index(str(ctr))
    return 0


def parse_endpoint(text):
    """``host:port`` / ``[v6]:port`` -> (host, port); ValueError otherwise."""
    t = text.strip()
    if t.startswith("["):
        close = t.find("]")
        host, rest = t[1:close], t[close + 1:]
        if close <= 1 or not rest.startswith(":"):
            raise ValueError("bad endpoint '{0}' (expected host:port)".format(text))
        port = rest[1:]
    else:
        host, sep, port = t.rpartition(":")
        if not sep or not host or ":" in host:
            raise ValueError("bad endpoint '{0}' (expected host:port)".format(text))
    p = strict_int(port)
    if not port.isdigit() or p is None or not 1 <= p <= 65535:
        raise ValueError("bad endpoint '{0}' (port must be 1..65535)".format(text))
    if any(c.isspace() or c in "/@,[]" for c in host):
        raise ValueError("bad endpoint '{0}' (bad host)".format(text))
    return host, p


def format_endpoint(host, port):
    return "[{0}]:{1}".format(host, port) if ":" in host else "{0}:{1}".format(host, port)


def default_node_id(instance_id):
    """``<short lower-case hostname>:<N>`` (federation_common.default_node_id)."""
    fc = _federation_common()
    if fc is not None:
        return fc.default_node_id(instance_id)
    host = re.sub(r"[^A-Za-z0-9_.-]", "-", socket.gethostname().split(".", 1)[0].lower()) or "host"
    if not host[0].isalnum():
        host = "h" + host
    suffix = ":{0}".format(int(instance_id))
    return host[:48 - len(suffix)] + suffix


def _federation_common():
    core = os.path.join(os.path.dirname(_SCRIPT_DIR), "Firmware", "_daq_core")
    if core not in sys.path:
        sys.path.append(core)
    try:
        import federation_common
    except ImportError:
        return None
    return federation_common


def instance_ports(params, instance_id, stride):
    """[(name, port, enabled)] of an instance (I.3 enabled-port rule)."""
    off = instance_id * stride
    mon = params.get("monitoring", {})
    en_mon = mon.get("en_monitoring", "0").strip() == "1"
    status_base = strict_int(mon.get("status_server_port", "5002")) or 5002
    events_base = strict_int(mon.get("zmq_pub_port", "5003")) or 5003
    return [("iq", 5000 + off, True),
            ("ctl", 5001 + off, True),
            ("status", status_base + off, en_mon and mon.get("en_status_server", "0").strip() == "1"),
            ("events", events_base + off, en_mon and mon.get("en_zmq_pub", "0").strip() == "1"),
            ("zmq", 1130 + off, True)]


def port_table(params, instance_id, stride):
    lines = ["Ports of instance {0} (port_stride {1}):".format(instance_id, stride)]
    for name, port, enabled in instance_ports(params, instance_id, stride):
        lines.append("  {0:<7} {1:>5}{2}".format(name, port, "" if enabled else "  (disabled)"))
    return "\n".join(lines)


def params_of_text(text):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return {s: dict(parser.items(s, raw=True)) for s in parser.sections()}


def write_atomic(path, text, mode=0o644):
    """Write *text* to *path* via a temp file in the same directory + os.replace.

    A symlinked *path* is resolved and its target rewritten (the link stays). An
    existing target keeps its permission bits and, when the caller may set it, its
    owner and group (a sudo run must not re-own the operator's INI); *mode* applies
    to a new file only.
    """
    path = os.path.realpath(path) if os.path.islink(path) else path
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".provision-", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if st is None:
            os.chmod(tmp, mode)
        else:
            if (st.st_uid, st.st_gid) != (os.geteuid(), os.getegid()):
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except PermissionError:
                    pass  # not root: the file becomes ours, as any rewrite by this user would
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Install root, liveness
# ---------------------------------------------------------------------------

def default_root():
    """The install root this script belongs to, without resolving symlinks.

    ``/opt/heimdall/util/provision_instance.py`` -> ``/opt/heimdall`` (so the
    links keep pointing through the /opt/heimdall symlink, H.8). Run through an
    instance dir's ``util`` link, the root that instance links to.
    """
    cand = os.path.dirname(_SCRIPT_DIR)
    core = os.path.join(cand, "Firmware", "_daq_core")
    if os.path.islink(core):
        target = os.readlink(core)
        if os.path.isabs(target):
            return os.path.dirname(os.path.dirname(target))
    return cand


def is_this_hosts_tree(root):
    return os.path.realpath(root) == os.path.realpath(default_root())


def run_dirs():
    dirs = [os.environ.get("HEIMDALL_RUN_DIR") or DEFAULT_RUN_DIR]
    if hasattr(os, "getuid"):
        dirs.append("/tmp/heimdall-run-{0}".format(os.getuid()))
    return list(dict.fromkeys(dirs))


def _process_args(pid):
    cmdline = "/proc/{0}/cmdline".format(pid)
    if os.path.exists(cmdline):
        try:
            with open(cmdline, "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return ""
    try:
        res = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def pid_role_alive(pid, role):
    """The hd_pid_role_alive rule of daq_common.sh: pid runs and its args contain *role*."""
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
    return role in _process_args(pid)


def registry_live(instance_id):
    """Path of a live ``$RUN/inst<N>.env`` (B.3.1), else None."""
    for d in run_dirs():
        path = os.path.join(d, "inst{0}.env".format(instance_id))
        try:
            with open(path, "r") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and key == "PIDS":
                for item in value.split(","):
                    role, _, pid = item.partition(":")
                    if role and pid_role_alive(pid, role):
                        return path
    return None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="provision_instance.py",
        description="Create or refresh a HeIMDALL DAQ instance directory (dry run unless --write).",
        epilog="Exit codes: 0 ok, 1 validation/write failed, 2 usage, 3 destination exists, "
               "4 instance live.")
    p.add_argument("--instance", type=int, required=True, metavar="N",
                   help="[federation] instance_id of the instance")
    p.add_argument("--root", help="install root (default: the tree holding this script, %s on an "
                                  "installed host)" % DEFAULT_INSTALL_ROOT)
    p.add_argument("--dest", help="instance directory (default <root>/instances/<N>)")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--template", help="base INI whose keys are rewritten "
                                        "(default <root>/Firmware/daq_chain_config.ini)")
    src.add_argument("--from-config", help="use this INI verbatim (no key is rewritten)")
    p.add_argument("--mode", choices=("link", "copy"), help="symlink the code (default) or copy it")
    ser = p.add_mutually_exclusive_group()
    ser.add_argument("--serial-base", type=int, metavar="B",
                     help="[hw] serial_base (default 1000 + 100*N)")
    ser.add_argument("--device-serials", metavar="LIST", help="[hw] device_serials (comma list)")
    p.add_argument("--unit-id", metavar="K|auto", help="[hw] unit_id (default auto)")
    p.add_argument("--cores", metavar="c0,c1,c2,c3", help="[host] cpu_cores (default 4N..4N+3)")
    p.add_argument("--delay-sync-cores", metavar="LIST", help="[host] delay_sync_cores")
    p.add_argument("--rt-offset", type=int, metavar="K", help="[host] rt_priority_offset (default 0)")
    p.add_argument("--stride", type=int, metavar="S", help="[federation] port_stride (default 100)")
    p.add_argument("--antenna-switch-gpio", metavar="none|A,B", help="[hw] antenna_switch_gpio")
    p.add_argument("--cluster-members", metavar="LIST",
                   help="[federation] cluster_members (also sets en_federation = 1)")
    p.add_argument("--node-id", metavar="NAME", help="[federation] node_id (default <host>:<N>)")
    p.add_argument("--cluster-id", metavar="ID", help="[federation] cluster_id (default 'default')")
    p.add_argument("--coordinator", metavar="H:P[,H:P]",
                   help="one endpoint: coordinator_host/port; two or more: coordinator_candidates")
    p.add_argument("--advertise-host", metavar="H", help="[federation] advertise_host")
    p.add_argument("--register", action="store_true",
                   help="[federation] register_with_coordinator = 1 (and en_federation = 1)")
    p.add_argument("--profile", choices=PROFILES,
                   help="merge config_files/performance/<NAME>.ini before the explicit flags")
    p.add_argument("--systemd", action="store_true",
                   help="write /etc/default/heimdall-daq@<N> and set [host] host_tuning = 0")
    p.add_argument("--env-file", metavar="PATH", help="environment file written with --systemd")
    p.add_argument("--python-bin", metavar="DIR", help="directory prepended to PATH in the env file")
    p.add_argument("--check-attached", action="store_true",
                   help="validate with 'no_hw attached' (run on the target host with the units plugged in)")
    p.add_argument("--in-place", action="store_true",
                   help="edit <root>/Firmware/daq_chain_config.ini (required for instance 0)")
    p.add_argument("--refresh", action="store_true",
                   help="re-create the code links against --root; never rewrites the INI")
    p.add_argument("--from-root", metavar="OLD",
                   help="with --refresh after repointing the install root to a new tree (H.8): "
                        "first copy the instance's INI, its backups, _data_control/ and _db/ from "
                        "the previous tree OLD (<OLD>/Firmware for instance 0, else "
                        "<OLD>/instances/<N>/Firmware)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing destination; refresh a live instance")
    p.add_argument("--write", action="store_true", help="execute the plan (default: dry run)")
    return p.parse_args(argv)


def _used_key_flags(args):
    used = []
    for attr, flag in KEY_FLAGS:
        v = getattr(args, attr)
        if v not in (None, False):
            used.append(flag)
    return used


def _cores_list(text, flag, exact=None):
    items = [x.strip() for x in text.split(",")]
    vals = [strict_int(x) for x in items]
    if not items or None in vals or any(v < 0 for v in vals):
        raise UsageError("{0} must be a comma list of non-negative integers, got '{1}'".format(flag, text))
    if exact is not None and (len(vals) != exact or len(set(vals)) != exact):
        raise UsageError("{0} must list exactly {1} distinct cores, got '{2}'".format(flag, exact, text))
    return vals


def validate_args(args):
    if args.instance < 0:
        raise UsageError("--instance must be >= 0")
    if args.from_config:
        used = _used_key_flags(args)
        if used:
            raise UsageError("flag {0} conflicts with --from-config".format(used[0]))
    if args.from_root and not args.refresh:
        raise UsageError("--from-root needs --refresh")
    if args.from_root and args.dest:
        raise UsageError("--from-root conflicts with --dest (an instance directory outside the install "
                         "root survives a root repoint: refresh it without --from-root)")
    if args.refresh:
        conflicts = _used_key_flags(args) + (["--from-config"] if args.from_config else []) + \
            (["--in-place"] if args.in_place else [])
        if conflicts:
            raise UsageError("flag {0} conflicts with --refresh (it never touches the INI)".format(conflicts[0]))
        if args.instance == 0 and not args.from_root:
            raise UsageError("--refresh is for instance directories (N > 0); instance 0 runs in "
                             "<root>/Firmware itself (after an upgrade, carry its INI over with "
                             "--refresh --from-root <previous tree>)")
        if args.instance == 0 and args.mode:
            raise UsageError("--mode conflicts with --instance 0 (it runs in <root>/Firmware)")
    elif args.instance == 0 and not args.in_place:
        raise UsageError("--instance 0 requires --in-place (instance 0 runs in <root>/Firmware)")
    if args.in_place and args.dest:
        raise UsageError("--dest conflicts with --in-place")
    if args.in_place and args.mode:
        raise UsageError("--mode conflicts with --in-place")
    if (args.env_file or args.python_bin) and not args.systemd:
        raise UsageError("--env-file/--python-bin need --systemd")
    if args.stride is not None and args.stride < 1:
        raise UsageError("--stride must be >= 1")
    if args.rt_offset is not None and not 0 <= args.rt_offset <= 81:
        raise UsageError("--rt-offset must be 0..81")
    if args.serial_base is not None and args.serial_base < 0:
        raise UsageError("--serial-base must be >= 0")
    if args.unit_id is not None and args.unit_id != "auto" and \
            (strict_int(args.unit_id) is None or strict_int(args.unit_id) < 0):
        raise UsageError("--unit-id must be a non-negative integer or 'auto'")
    if args.cores:
        _cores_list(args.cores, "--cores", exact=4)
    if args.delay_sync_cores:
        _cores_list(args.delay_sync_cores, "--delay-sync-cores")
    if args.coordinator:
        for item in args.coordinator.split(","):
            try:
                parse_endpoint(item)
            except ValueError as e:
                raise UsageError("--coordinator: {0}".format(e))
    for flag, value in (("--node-id", args.node_id), ("--cluster-id", args.cluster_id),
                        ("--advertise-host", args.advertise_host),
                        ("--cluster-members", args.cluster_members),
                        ("--device-serials", args.device_serials),
                        ("--antenna-switch-gpio", args.antenna_switch_gpio)):
        if value is not None and (not value.strip() or "\n" in value):
            raise UsageError("{0} must be a non-empty single-line value".format(flag))


class Plan:
    """Everything a run decides before touching the filesystem."""

    def __init__(self):
        self.root = None
        self.firmware_src = None
        self.dest = None          # instance dir (None with --in-place)
        self.firmware_dir = None  # where the INI lives and ini_checker runs
        self.ini_path = None
        self.check_ini = None     # the INI --refresh validates (the old tree's with --from-root)
        self.migrate = []         # [(src, dst)] private entries --from-root copies (INI last)
        self.mode = "link"
        self.base_text = ""       # what the INI diff is computed against
        self.new_text = None      # None with --refresh
        self.template_path = None
        self.entries = []         # [(name, src, dst)] code entries
        self.private_dirs = []
        self.exists = False
        self.env_file = None
        self.env_text = None
        self.warnings = []
        self.cores_error = None


def cores_within(text, cores):
    """True when the comma core list *text* names only cores of *cores*."""
    vals = [strict_int(x) for x in text.split(",") if x.strip()]
    return bool(vals) and None not in vals and set(vals) <= set(cores)


def _profile_params(root, name):
    path = os.path.join(root, "config_files", "performance", name + ".ini")
    if not os.path.isfile(path):
        raise UsageError("profile '{0}' not found: {1}".format(name, path))
    return read_ini_params(path)


def build_ini(args, template_text, root):
    """The rewritten INI text and warnings for --template mode (H.1 key table)."""
    doc = IniDocument(template_text)
    try:
        tparams = params_of_text(template_text)
    except configparser.Error as e:
        raise UsageError("cannot parse the template: {0}".format(e))
    n = args.instance
    warnings = []
    ctr_index = control_channel_index(tparams)
    num_ch = strict_int(tparams.get("hw", {}).get("num_ch", "")) or 0

    # 1) defaults of the key table
    values = []  # [(section, key, value)] applied in order; later wins

    def put(section, key, value):
        values.append((section, key, value))

    stride = args.stride if args.stride is not None else DEFAULT_STRIDE
    put("federation", "instance_id", str(n))
    put("federation", "port_stride", str(stride))
    put("federation", "node_id", default_node_id(n))
    put("federation", "cluster_id", "default")
    put("hw", "serial_base", str(DEFAULT_SERIAL_BASE + SERIAL_UNIT_STRIDE * n))
    put("hw", "unit_id", "auto")
    put("daq", "ctl_max_connections", "4")
    put("host", "instance_registry", "1")
    put("host", "instance_guard", "refuse")
    put("host", "cpu_cores", ",".join(str(4 * n + k) for k in range(4)))
    put("host", "rt_priority_offset", "0")
    put("host", "allow_core_sharing", "refuse")
    put("host", "host_tuning", "auto")
    put("host", "preflight", "warn")
    put("host", "health_thread_sched_other", "1")
    for key, value in (("en_monitoring", "1"), ("en_status_server", "1"), ("status_extended", "1"),
                       ("status_update_interval", "10"), ("event_tag_identity", "1")):
        put("monitoring", key, value)
    # 2) the profile snippet
    if args.profile:
        for section, items in _profile_params(root, args.profile).items():
            for key, value in items.items():
                put(section, key, value)
    # 3) explicit flags
    if args.node_id:
        put("federation", "node_id", args.node_id.strip())
    if args.cluster_id:
        put("federation", "cluster_id", args.cluster_id.strip())
    if args.cluster_members:
        put("federation", "cluster_members", args.cluster_members.strip())
        put("federation", "en_federation", "1")
    if args.coordinator:
        eps = [parse_endpoint(x) for x in args.coordinator.split(",")]
        if len(eps) >= 2:
            put("federation", "coordinator_candidates", ",".join(format_endpoint(h, p) for h, p in eps))
        else:
            put("federation", "coordinator_host", eps[0][0])
            put("federation", "coordinator_port", str(eps[0][1]))
    if args.advertise_host:
        put("federation", "advertise_host", args.advertise_host.strip())
    if args.register:
        put("federation", "register_with_coordinator", "1")
        put("federation", "en_federation", "1")
    if args.serial_base is not None:
        put("hw", "serial_base", str(args.serial_base))
    if args.device_serials:
        put("hw", "device_serials", ",".join(x.strip() for x in args.device_serials.split(",")))
    if args.unit_id is not None:
        put("hw", "unit_id", args.unit_id)
    if args.antenna_switch_gpio:
        put("hw", "antenna_switch_gpio", args.antenna_switch_gpio.strip())
    if args.cores:
        put("host", "cpu_cores", ",".join(str(c) for c in _cores_list(args.cores, "--cores", 4)))
    if args.delay_sync_cores:
        put("host", "delay_sync_cores",
            ",".join(str(c) for c in _cores_list(args.delay_sync_cores, "--delay-sync-cores")))
    if args.rt_offset is not None:
        put("host", "rt_priority_offset", str(args.rt_offset))
    if args.systemd:
        put("host", "host_tuning", "0")

    final = {}
    for section, key, value in values:
        final[(section, key)] = value
    # serial_base is not written when device_serials is the explicit choice
    if args.device_serials:
        final.pop(("hw", "serial_base"), None)
    else:
        # A.1: a template's explicit list wins over serial_base, so an inherited list
        # (usually instance 0's unit) would make this instance open those receivers.
        inherited = tparams.get("hw", {}).get("device_serials", "").strip()
        if inherited:
            final[("hw", "device_serials")] = ""
            warnings.append("template [hw] device_serials = {0} dropped: it would override serial_base "
                            "{1} (pass --device-serials to pin this instance's serials)".format(
                                inherited, final[("hw", "serial_base")]))
    tfed = tparams.get("federation", {})
    if args.coordinator:
        # The flag alone names the coordinators: delay_sync prefers coordinator_candidates
        # over coordinator_host, so the template's other form would win or linger.
        multi = ("federation", "coordinator_candidates") in final
        drop = ["coordinator_host"] if multi else ["coordinator_candidates"]
        candidates = final.get(("federation", "coordinator_candidates"), "").split(",")
        if tfed.get("coordinator_self", "").strip() not in candidates:
            drop.append("coordinator_self")
        for key in drop:
            inherited = tfed.get(key, "").strip()
            if inherited:
                final[("federation", key)] = ""
                warnings.append("template [federation] {0} = {1} dropped: --coordinator {2} names "
                                "the coordinators".format(key, inherited, args.coordinator.strip()))
    if not args.advertise_host:
        inherited = tfed.get("advertise_host", "").strip()
        host = socket.gethostname().split(".", 1)[0].lower()
        if inherited and inherited.lower() != host:
            warnings.append("template [federation] advertise_host = {0} kept, but this host is '{1}' "
                            "(pass --advertise-host)".format(inherited, host))
    cores = [strict_int(c) for c in final[("host", "cpu_cores")].split(",")]
    inherited = tparams.get("host", {}).get("delay_sync_cores", "").strip()
    if ("host", "delay_sync_cores") not in final and inherited and not cores_within(inherited, cores):
        # an inherited pin (usually instance 0's) would run this instance's delay_sync at
        # SCHED_FIFO on another instance's cores, past the cpu_cores guard
        final[("host", "delay_sync_cores")] = ""
        warnings.append("template [host] delay_sync_cores = {0} dropped: outside this instance's "
                        "cpu_cores {1} (pass --delay-sync-cores to pin it)".format(
                            inherited, final[("host", "cpu_cores")]))
    if args.cluster_members:
        # C.2: the mesh peers are cluster_members minus self only while peer_list is empty
        inherited = tparams.get("federation", {}).get("peer_list", "").strip()
        if inherited:
            final[("federation", "peer_list")] = ""
            warnings.append("template [federation] peer_list = {0} dropped: it would override "
                            "--cluster-members for health polling".format(inherited))
    for (section, key), value in final.items():
        doc.set(section, key, value)
    # Keep the control channel on the same channel index (A.1: it must be one of the serials)
    new_params = params_of_text(doc.text())
    serials = resolve_serials(new_params.get("hw", {}))
    if serials and ctr_index < len(serials) and "daq" in new_params:
        doc.set("daq", "ctr_channel_serial_no", serials[ctr_index])

    if args.register and not args.advertise_host:
        host = socket.gethostname().split(".", 1)[0].lower()
        if host in COMMON_IMAGE_HOSTNAMES:
            warnings.append("--register on a host named '{0}' (a common image default) without "
                            "--advertise-host: node keys may collide (set --advertise-host or "
                            "--node-id)".format(host))
    if args.register and not args.coordinator and not new_params.get("federation", {}).get(
            "coordinator_host", "").strip() and not new_params.get("federation", {}).get(
            "coordinator_candidates", "").strip():
        warnings.append("--register without --coordinator: the INI names no coordinator to register with")
    if args.device_serials and num_ch and len([x for x in args.device_serials.split(",") if x.strip()]) != num_ch:
        warnings.append("--device-serials lists {0} serials but [hw] num_ch is {1}".format(
            len([x for x in args.device_serials.split(",") if x.strip()]), num_ch))
    return doc.text(), warnings


def _is_private_name(name):
    """True for a Firmware entry that stays private to an instance dir (never linked)."""
    return name in SKIP_ENTRIES or name.startswith(".") or name.startswith(INI_NAME + ".")


def _code_entries(root, dest):
    fw_src = os.path.join(root, "Firmware")
    entries = [(name, os.path.join(root, name), os.path.join(dest, name))
               for name in ROOT_LINKS if os.path.exists(os.path.join(root, name))]
    for name in sorted(os.listdir(fw_src)):
        if _is_private_name(name):
            continue
        entries.append((os.path.join("Firmware", name), os.path.join(fw_src, name),
                        os.path.join(dest, "Firmware", name)))
    return entries


def _env_template(root):
    path = os.path.join(root, "packaging", "default", "heimdall-daq")
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return FALLBACK_ENV_TEMPLATE


def build_env_text(root, python_bin):
    text = _env_template(root)
    if not python_bin:
        return text
    python_bin = os.path.abspath(python_bin)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("PATH="):
            lines[i] = "PATH={0}:{1}".format(python_bin, line[len("PATH="):])
            break
    else:
        lines.insert(0, "PATH={0}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin".format(
            python_bin))
    return "\n".join(lines) + "\n"


def _migration_entries(old_fw, firmware_dir):
    """[(src, dst)] of what --from-root copies: the private dirs, the INI backups, the INI last."""
    names = [d for d in MIGRATED_DIRS if os.path.lexists(os.path.join(old_fw, d))]
    names += sorted(x for x in os.listdir(old_fw) if x.startswith(INI_NAME + "."))
    names.append(INI_NAME)
    return [(os.path.join(old_fw, x), os.path.join(firmware_dir, x)) for x in names]


def make_plan(args):
    plan = Plan()
    plan.root = os.path.abspath(args.root) if args.root else default_root()
    plan.firmware_src = os.path.join(plan.root, "Firmware")
    missing = [e for e in REQUIRED_FIRMWARE if not os.path.exists(os.path.join(plan.firmware_src, e))]
    if missing:
        raise UsageError("{0} is not a HeIMDALL install root (missing Firmware/{1})".format(
            plan.root, ", Firmware/".join(missing)))
    n = args.instance
    old_fw = None
    if args.from_root:
        old_root = os.path.abspath(args.from_root)
        if os.path.realpath(old_root) == os.path.realpath(plan.root):
            raise UsageError("--from-root {0} is the install root itself ({1}): repoint the root to "
                             "the new tree first".format(old_root, os.path.realpath(plan.root)))
        old_fw = os.path.join(old_root, "Firmware") if n == 0 else \
            os.path.join(old_root, "instances", str(n), "Firmware")
        if not os.path.isfile(os.path.join(old_fw, INI_NAME)):
            raise UsageError("--from-root: {0} has no {1} to carry over".format(old_fw, INI_NAME))
    if args.in_place or n == 0:
        plan.firmware_dir = plan.firmware_src
    else:
        plan.dest = os.path.abspath(args.dest) if args.dest else os.path.join(plan.root, "instances", str(n))
        if os.path.realpath(plan.dest) == os.path.realpath(plan.root) or \
                os.path.realpath(plan.dest) == os.path.realpath(plan.firmware_src):
            raise UsageError("--dest must not be the install root")
        plan.firmware_dir = os.path.join(plan.dest, "Firmware")
        plan.exists = os.path.lexists(plan.dest)
        if plan.exists and not os.path.isdir(plan.dest):
            raise UsageError("{0} exists and is not a directory".format(plan.dest))
        if args.refresh:
            has_ini = os.path.isfile(os.path.join(plan.firmware_dir, INI_NAME))
            if old_fw and has_ini:
                raise UsageError("{0} already has Firmware/{1} (carried over before?): refresh it "
                                 "without --from-root".format(plan.dest, INI_NAME))
            if not old_fw and not has_ini:
                raise UsageError("nothing to refresh: {0} has no Firmware/{1}. If the install root "
                                 "was repointed to a new tree (H.8 upgrade), pass --from-root "
                                 "<previous tree> to carry the instance over from it".format(
                                     plan.dest, INI_NAME))
            if args.mode:
                plan.mode = args.mode
            else:
                plan.mode = "link" if os.path.islink(os.path.join(old_fw or plan.firmware_dir,
                                                                  "_daq_core")) else "copy"
        else:
            plan.mode = args.mode or "link"
        plan.entries = _code_entries(plan.root, plan.dest)
        plan.private_dirs = [os.path.join(plan.firmware_dir, d) for d in PRIVATE_DIRS]
    plan.ini_path = os.path.join(plan.firmware_dir, INI_NAME)
    plan.check_ini = os.path.join(old_fw, INI_NAME) if old_fw else plan.ini_path
    if old_fw:
        plan.migrate = _migration_entries(old_fw, plan.firmware_dir)
    plan.template_path = os.path.abspath(args.template) if args.template else os.path.join(plan.firmware_src, INI_NAME)

    if not args.refresh:
        if args.from_config:
            try:
                with open(args.from_config, "r") as f:
                    plan.new_text = f.read()
            except (OSError, UnicodeDecodeError) as e:
                raise UsageError("cannot read {0}: {1}".format(args.from_config, e))
            try:
                params = params_of_text(plan.new_text)
            except configparser.Error as e:
                raise UsageError("cannot parse {0}: {1}".format(args.from_config, e))
            iid = params.get("federation", {}).get("instance_id")
            if strict_int(iid if iid is not None else "") != n:
                raise UsageError("instance_id in {0} is {1}, expected {2}".format(
                    args.from_config, "absent" if iid is None else iid.strip(), n))
        else:
            try:
                with open(plan.template_path, "r") as f:
                    template_text = f.read()
            except (OSError, UnicodeDecodeError) as e:
                raise UsageError("cannot read the template {0}: {1}".format(plan.template_path, e))
            plan.new_text, warns = build_ini(args, template_text, plan.root)
            plan.warnings.extend(warns)
        if os.path.isfile(plan.ini_path):
            with open(plan.ini_path, "r") as f:
                plan.base_text = f.read()
        elif not args.from_config:
            with open(plan.template_path, "r") as f:
                plan.base_text = f.read()
        else:
            plan.base_text = ""
        # cpu_cores vs this host's cores: an error only for the cores this tool chose
        # (--template mode, writing this host's own tree); a --from-config file was planned
        # by cluster_cfg_gen.py against --cores-per-host, so it is a warning there
        params = params_of_text(plan.new_text)
        cores = [strict_int(c) for c in params.get("host", {}).get("cpu_cores", "").split(",") if c.strip()]
        nproc = os.cpu_count() or 1
        if cores and None not in cores and max(cores) >= nproc:
            text = ("cpu_cores {0} exceed the 0..{1} cores of this host; pass --cores".format(
                ",".join(str(c) for c in cores), nproc - 1))
            if args.write and not args.from_config and is_this_hosts_tree(plan.root):
                plan.cores_error = text
            else:
                plan.warnings.append(text)
    if args.systemd:
        default_env = "/etc/default/heimdall-daq" if n == 0 else "/etc/default/heimdall-daq@{0}".format(n)
        plan.env_file = os.path.abspath(args.env_file) if args.env_file else default_env
        plan.env_text = build_env_text(plan.root, args.python_bin)
    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _replace_link(src, dst):
    tmp = "{0}.provision-{1}".format(dst, os.getpid())
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(src, tmp)
    os.replace(tmp, dst)


def _remove_entry(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def install_code(plan):
    """Create (or re-create) every code entry; returns the removed stale entries (dest-relative)."""
    os.makedirs(plan.firmware_dir, exist_ok=True)
    for _, src, dst in plan.entries:
        if plan.mode == "link":
            if os.path.lexists(dst) and not os.path.islink(dst):
                _remove_entry(dst)  # a copy-mode entry being converted to a link
            _replace_link(src, dst)
        else:
            if os.path.lexists(dst):
                _remove_entry(dst)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, dst)
    # Entries this tool owns that the root no longer has (removed by an upgrade, or
    # a link into a previous --root): every unwanted symlink, and in copy mode every
    # unwanted non-private entry. Private names (INI, its backups, private dirs,
    # hidden files) are never touched, even when an operator made them links.
    removed = []
    wanted = set(dst for _, _, dst in plan.entries)
    for directory in (plan.dest, plan.firmware_dir):
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if path in wanted or name.startswith("."):
                continue
            if directory == plan.dest:  # Firmware/ itself is a real directory: never stale
                stale = os.path.islink(path) or (plan.mode == "copy" and name in ROOT_LINKS)
            else:
                stale = not _is_private_name(name) and (os.path.islink(path) or plan.mode == "copy")
            if not stale:
                continue
            _remove_entry(path)
            removed.append(os.path.relpath(path, plan.dest))
    for d in plan.private_dirs:
        if os.path.islink(d):
            raise ProvisionError(EXIT_INVALID, "{0} is a symlink; private directories must be real "
                                               "directories".format(d))
        os.makedirs(d, exist_ok=True)
    return removed


def run_checker(plan, ini_path, cwd, attached):
    """Run the install root's ini_checker.py; (returncode, combined output)."""
    cmd = [sys.executable, os.path.join(plan.firmware_src, "ini_checker.py"), ini_path, "no_hw"]
    if attached:
        cmd.append("attached")
    try:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "cannot run ini_checker: {0}".format(e)
    return res.returncode, (res.stdout + res.stderr).strip()


def validate(plan, args, written):
    if written:
        rc, out = run_checker(plan, INI_NAME, plan.firmware_dir, args.check_attached)
    else:
        text = plan.new_text
        if text is None:  # --refresh: the instance INI as it is (or will be, with --from-root)
            with open(plan.check_ini, "r") as f:
                text = f.read()
        with tempfile.TemporaryDirectory(prefix="provision-check-") as tmp:
            path = os.path.join(tmp, INI_NAME)
            with open(path, "w") as f:
                f.write(text)
            rc, out = run_checker(plan, path, tmp, args.check_attached)
    mode = "no_hw attached" if args.check_attached else "no_hw"
    if out:
        print(out)
    if rc != 0:
        print("ini_checker ({0}) FAILED{1}".format(
            mode, "; the files stay for inspection" if written else ""))
        return False
    print("ini_checker ({0}): ok".format(mode))
    return True


def missing_template_keys(template_path, ini_path):
    template = read_ini_params(template_path)
    instance = read_ini_params(ini_path)
    out = []
    for section, items in template.items():
        have = instance.get(section, {})
        for key, value in items.items():
            if key not in have:
                out.append("[{0}] {1} (template value: '{2}')".format(section, key, value))
    return out


def print_plan(plan, args):
    action = "refresh" if args.refresh else ("in-place" if args.in_place else "create")
    print("Install root : {0}".format(plan.root))
    print("Instance     : {0} ({1}{2})".format(args.instance, action,
                                                "" if args.in_place else ", mode " + plan.mode))
    if plan.dest:
        print("Directory    : {0}{1}".format(plan.dest, " (exists)" if plan.exists else ""))
        for name, src, _ in plan.entries:
            print("  {0:<40} {1} {2}".format(name, "->" if plan.mode == "link" else "<= copy of", src))
        for d in plan.private_dirs:
            print("  {0:<40} private directory".format(os.path.relpath(d, plan.dest)))
        for src, dst in plan.migrate:
            print("  {0:<40} <= carried over from {1}".format(os.path.relpath(dst, plan.dest), src))
        if not args.refresh:
            print("  {0:<40} {1}".format(os.path.join("Firmware", INI_NAME),
                                         "copied verbatim from " + os.path.abspath(args.from_config)
                                         if args.from_config else "generated from " + plan.template_path))
    elif plan.migrate:
        print("INI          : {0} (backup {1}.bak.<unix ts> of the shipped one first)".format(
            plan.ini_path, INI_NAME))
        for src, dst in plan.migrate:
            print("  {0:<40} <= carried over from {1}".format(os.path.relpath(dst, plan.firmware_dir), src))
    else:
        print("INI          : {0} (backup {1}.bak.<unix ts> first)".format(plan.ini_path, INI_NAME))
    if plan.new_text is not None:
        diff = list(difflib.unified_diff(plan.base_text.splitlines(), plan.new_text.splitlines(),
                                         "current" if os.path.isfile(plan.ini_path) else "template",
                                         "instance {0}".format(args.instance), lineterm=""))
        print("\nINI diff:")
        print("\n".join(diff) if diff else "  (no change)")
        params = params_of_text(plan.new_text)
        stride = strict_int(params.get("federation", {}).get("port_stride", "100")) or DEFAULT_STRIDE
        print()
        print(port_table(params, args.instance, stride))
    if plan.env_file:
        print("\nEnvironment file {0}:".format(plan.env_file))
        print("".join("  " + line + "\n" for line in plan.env_text.splitlines()), end="")


def print_followups(plan, args):
    if plan.env_file:
        unit = "heimdall-daq-supervised" if args.instance == 0 else \
            "heimdall-daq-supervised@{0}".format(args.instance)
        print("\nEnable the supervised unit:\n  systemctl enable --now {0}".format(unit))
    if plan.root != DEFAULT_INSTALL_ROOT:
        print("\nThe install root is not {0}; add this logrotate stanza "
              "(/etc/logrotate.d/heimdall-local):".format(DEFAULT_INSTALL_ROOT))
        print("{0}/Firmware/_logs/*.log\n{0}/Firmware/_logs/inst*/*.log\n"
              "{0}/instances/*/Firmware/_logs/*.log\n{0}/instances/*/Firmware/_logs/inst*/*.log {{\n"
              "    size 50M\n    rotate 7\n    copytruncate\n    missingok\n    notifempty\n"
              "    compress\n    delaycompress\n}}".format(plan.root))


def _copy_file(src, dst):
    """copy2 that also keeps the owner when this process may set it (sudo)."""
    shutil.copy2(src, dst, follow_symlinks=False)
    try:
        st = os.lstat(src)
        if (st.st_uid, st.st_gid) != (os.geteuid(), os.getegid()):
            os.lchown(dst, st.st_uid, st.st_gid)
    except (AttributeError, PermissionError):
        pass
    return dst


def _special_entries(directory, names):
    """copytree ignore: FIFOs, sockets and devices (runtime objects; a FIFO copy would block)."""
    out = []
    for name in names:
        mode = os.lstat(os.path.join(directory, name)).st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            out.append(name)
    return out


def migrate_state(plan):
    """Copy the --from-root entries (the old tree stays intact for a rollback)."""
    os.makedirs(plan.firmware_dir, exist_ok=True)
    if plan.dest is None and os.path.isfile(plan.ini_path):  # instance 0: the new tree's shipped INI
        with open(plan.ini_path, "rb") as f:
            shipped = f.read()
        with open(plan.check_ini, "rb") as f:
            carried = f.read()
        if shipped != carried:
            backup = "{0}.bak.{1}".format(plan.ini_path, int(time.time()))
            shutil.copy2(plan.ini_path, backup)
            print("Backup written: {0}".format(backup))
    for src, dst in plan.migrate:
        if os.path.isdir(src) and not os.path.islink(src):
            if os.path.islink(dst):
                raise ProvisionError(EXIT_INVALID, "{0} is a symlink; private directories must be "
                                                   "real directories".format(dst))
            shutil.copytree(src, dst, symlinks=True, ignore=_special_entries,
                            copy_function=_copy_file, dirs_exist_ok=True)
        else:
            tmp = "{0}.provision-{1}".format(dst, os.getpid())
            if os.path.lexists(tmp):
                os.unlink(tmp)
            _copy_file(src, tmp)
            os.replace(tmp, dst)
        print("Carried over: {0} <= {1}".format(dst, src))


def execute(plan, args):
    if args.refresh and plan.dest is None:  # instance 0 --from-root
        migrate_state(plan)
    elif args.in_place:
        ts = int(time.time())
        backup = "{0}.bak.{1}".format(plan.ini_path, ts)
        if os.path.exists(plan.ini_path):
            shutil.copy2(plan.ini_path, backup)
            print("Backup written: {0}".format(backup))
        write_atomic(plan.ini_path, plan.new_text)
        print("Written: {0}".format(plan.ini_path))
    else:
        os.makedirs(plan.dest, exist_ok=True)
        removed = install_code(plan)
        for name in removed:
            print("Removed stale entry: {0}".format(name))
        if args.refresh:
            print("Re-linked {0} entries in {1}".format(len(plan.entries), plan.dest))
            if plan.migrate:  # after the links: an interrupted run leaves no INI and can be re-run
                migrate_state(plan)
        else:
            if os.path.exists(plan.ini_path):
                backup = "{0}.bak.{1}".format(plan.ini_path, int(time.time()))
                shutil.copy2(plan.ini_path, backup)
                print("Backup written: {0}".format(backup))
            write_atomic(plan.ini_path, plan.new_text)
            print("Written: {0}".format(plan.ini_path))
    if plan.env_file:
        os.makedirs(os.path.dirname(plan.env_file), exist_ok=True)
        write_atomic(plan.env_file, plan.env_text)
        print("Written: {0}".format(plan.env_file))


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code not in (0, None) else EXIT_OK
    try:
        validate_args(args)
        plan = make_plan(args)
        if plan.dest and plan.exists and not args.refresh and not args.force:
            raise ProvisionError(EXIT_EXISTS, "{0} exists; use --refresh to re-link it after an "
                                              "upgrade or --force to overwrite it".format(plan.dest))
        live = registry_live(args.instance) if args.refresh and not args.force else None
        if live and args.write:
            raise ProvisionError(EXIT_LIVE, "instance {0} is live ({1}); stop it first or pass "
                                            "--force".format(args.instance, live))
        if live:
            plan.warnings.append("instance {0} is live ({1}): --refresh --write will be refused "
                                 "until it is stopped (or pass --force)".format(args.instance, live))
        if plan.cores_error:
            raise ProvisionError(EXIT_INVALID, plan.cores_error)
        print_plan(plan, args)
        for w in plan.warnings:
            print("WARNING: " + w, file=sys.stderr)
        if args.refresh:
            missing = missing_template_keys(plan.template_path, plan.check_ini)
            print("\nTemplate keys missing from the instance INI ({0}):".format(len(missing)))
            print("\n".join("  " + m for m in missing) if missing else "  (none)")
        if not args.write:
            ok = validate(plan, args, written=False)
            print("\nDry run: nothing written (pass --write to execute).")
            print_followups(plan, args)
            return EXIT_OK if ok else EXIT_INVALID
        print()
        execute(plan, args)
        ok = validate(plan, args, written=True)
        print_followups(plan, args)
        return EXIT_OK if ok else EXIT_INVALID
    except UsageError as e:
        print("ERROR: {0}".format(e), file=sys.stderr)
        return EXIT_USAGE
    except ProvisionError as e:
        print("ERROR: {0}".format(e), file=sys.stderr)
        return e.code
    except OSError as e:
        print("ERROR: {0}".format(e), file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
