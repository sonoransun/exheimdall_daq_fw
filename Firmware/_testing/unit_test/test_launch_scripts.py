"""Launch, stop, supervise and host-tuning scripts (spec B.3-B.9, H.2, H.3).

CI lane (Linux and macOS, needs bash; no sudo, no hardware, no C binaries).
Every script runs in a temporary copy of the Firmware directory whose
pipeline stages, ini_checker, FIR designer and discovery tools are recording
stubs, with a sandbox PATH that holds only:

* symlinks to the harmless system tools the scripts use (sh, awk, ps, ...);
* recording stubs for ``sudo`` (never escalates: it records and runs the
  command, except the host-tuning commands sysctl/tee/sh/irq_tuning.sh/
  host_tuning.sh, which are only recorded), ``taskset``/``chrt``
  (pass-through), ``ss``/``lsof`` (report the ports listed in a file),
  ``pgrep``/``pkill`` (operate on a fake proc tree only; pkill records),
  ``systemctl``, ``nproc``, ``uname``, ``sleep`` (optionally instant) and
  ``python3`` (this interpreter);
* ``flock``: util-linux on Linux, a fcntl.flock stub on macOS.

"Stage" processes are real processes whose ``ps -o args=`` carry the role
names; ``HEIMDALL_PROC_ROOT`` points at a fake proc tree giving them a cwd,
``HEIMDALL_RUN_DIR`` at a temp directory. Every process the tests start is
killed in tearDown.
"""
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.path.dirname(FIRMWARE)
UTIL = os.path.join(REPO, "util")
BASH = shutil.which("bash")
IS_LINUX = sys.platform.startswith("linux")
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

OWN_SCRIPTS = ["daq_start_sm.sh", "daq_synthetic_start.sh", "daq_stop.sh", "daq_common.sh",
               "daq_supervise.sh", "federation_start.sh", "federation_stop.sh"]

# Real tools linked into the sandbox PATH (whichever exist on this host)
REAL_TOOLS = ["sh", "bash", "dash", "cat", "ls", "rm", "mkdir", "mkfifo", "ps", "kill", "head",
              "tail", "awk", "sed", "tr", "cut", "grep", "date", "id", "readlink", "dirname",
              "basename", "env", "find", "xargs", "mv", "chmod", "touch", "wc", "sort", "ln",
              "cp", "rmdir", "nohup", "true", "false", "expr", "stat", "uniq", "tee"]
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

TODAY_TERM = ["-TERM rtl_daq.out", "-TERM decimate.out", "-TERM rebuffer.out", "-TERM iq_server.out",
              "-TERM -f python.*_testing/test_data_synthesizer.py",
              "-TERM -f python.*_daq_core/delay_sync.py",
              "-TERM -f python.*_daq_core/hw_controller.py"]
TODAY_KILL = ["-KILL rtl_daq.out", "-KILL decimate.out", "-KILL rebuffer.out", "-KILL iq_server.out",
              "-KILL -f python.*_testing/test_data_synthesizer.py"]
ALL_EXTRA_KILL = ["-KILL -f python.*_daq_core/delay_sync.py",
                  "-KILL -f python.*_daq_core/hw_controller.py"]

BASE_INI = """[hw]
name = k4
num_ch = {num_ch}

[daq]
daq_buffer_size = 262144
sample_rate = 2400000
center_freq = 416588000

[pre_processing]
cpi_size = 1048576
decimation_ratio = 1

[data_interface]
out_data_iface_type = {iface}

[federation]
instance_id = {iid}
port_stride = {stride}

[monitoring]
en_monitoring = {en_mon}
en_status_server = {en_status}
status_server_port = {status_port}
en_zmq_pub = {en_pub}
zmq_pub_port = 5003
{extra}
"""

# --------------------------------------------------------------------------
# Stub programs
# --------------------------------------------------------------------------

SUDO_STUB = r"""#!/bin/sh
# Never escalates. Records the command; host-tuning commands are only
# recorded, everything else is executed as the test user.
printf '%s\n' "$*" >> "$FAKE_DIR/sudo.log"
case "$1" in
    tee) cat > /dev/null; exit 0 ;;
    sysctl|sh|*irq_tuning.sh|*host_tuning.sh) exit 0 ;;
    kill) [ "${FAKE_SUDO_NOKILL:-0}" = 1 ] && exit 1 ;;
esac
exec "$@"
"""

PASSTHROUGH_STUB = r"""#!/bin/sh
# {name} OPT VALUE CMD...: record, drop the two options, run CMD
printf '%s\n' "$*" >> "$FAKE_DIR/{name}.log"
shift 2
exec "$@"
"""

NPROC_STUB = "#!/bin/sh\necho \"${FAKE_NPROC:-4}\"\n"

UNAME_STUB = "#!/bin/sh\nif [ -n \"${FAKE_UNAME:-}\" ] && [ \"$1\" = -m ]; then echo \"$FAKE_UNAME\"; exit 0; fi\nexec %s \"$@\"\n"

SLEEP_STUB = "#!/bin/sh\n[ \"${FAKE_FAST_SLEEP:-0}\" = 1 ] && exit 0\nexec %s \"$@\"\n"

SYSTEMCTL_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DIR/systemctl.log"
case "$1" in
    is-active) [ "${FAKE_SYSTEMCTL_ACTIVE:-0}" = 1 ] && exit 0; exit 3 ;;
esac
exit 0
"""

SYSCTL_STUB = "#!/bin/sh\nprintf 'sysctl %s\\n' \"$*\" >> \"$FAKE_DIR/calls.log\"\n"

SS_STUB = r"""#!/usr/bin/env python3
# ss -ltn[p]H "sport = :P": one LISTEN line when P is in $FAKE_DIR/busy_ports
import os, sys
fake = os.environ["FAKE_DIR"]
with open(os.path.join(fake, "ss.log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
try:
    busy = open(os.path.join(fake, "busy_ports")).read().split()
except OSError:
    busy = []
port = sys.argv[-1].rsplit(":", 1)[-1] if "sport" in sys.argv[-1] else None
flags = "".join(a for a in sys.argv[1:-1] if a.startswith("-"))
if port in busy:
    line = "LISTEN 0      128          0.0.0.0:%s        0.0.0.0:*" % port
    if "p" in flags:
        line += ' users:(("fakesrv",pid=4242,fd=3))'
    if "H" not in flags:
        print("State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process")
    print(line)
"""

LSOF_STUB = r"""#!/usr/bin/env python3
import os, sys
fake = os.environ["FAKE_DIR"]
with open(os.path.join(fake, "lsof.log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
try:
    busy = open(os.path.join(fake, "busy_ports")).read().split()
except OSError:
    busy = []
port = next((a.split(":", 1)[1] for a in sys.argv[1:] if a.startswith("-iTCP:")), None)
if port in busy:
    print("COMMAND  PID USER FD TYPE DEVICE SIZE/OFF NODE NAME")
    print("fakesrv 4242 user 3u IPv4 0t0 TCP *:%s (LISTEN)" % port)
    sys.exit(0)
sys.exit(1)
"""

PGREP_STUB = r"""#!/usr/bin/env python3
# pgrep -f PATTERN over the fake proc tree only
import os, re, sys
root = os.environ.get("HEIMDALL_PROC_ROOT", "/nonexistent")
pattern = sys.argv[-1]
hits = []
for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
    if not name.isdigit():
        continue
    try:
        cmd = open(os.path.join(root, name, "cmdline"), "rb").read().replace(b"\0", b" ").decode()
    except OSError:
        continue
    if re.search(pattern, cmd):
        hits.append(name)
print("\n".join(hits))
sys.exit(0 if hits else 1)
"""

PKILL_STUB = r"""#!/bin/sh
printf 'pkill %s\n' "$*" >> "$FAKE_DIR/calls.log"
exit 1
"""

FLOCK_STUB = r"""#!/usr/bin/env python3
# flock [-n | -w SECS] FD with fcntl.flock on the inherited descriptor
import fcntl, sys, time
args = sys.argv[1:]
nonblock, wait = False, None
while len(args) > 1:
    a = args.pop(0)
    if a == "-n":
        nonblock = True
    elif a == "-w":
        wait = float(args.pop(0))
fd = int(args[0])
deadline = None if wait is None else time.monotonic() + wait
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if nonblock or (deadline is not None and time.monotonic() >= deadline):
            sys.exit(1)
        time.sleep(0.05)
"""

PYTHON_TOOL_STUB = r"""#!/usr/bin/env python3
# {name}: record argv, exit with $FAKE_{env}_RC (default 0)
import json, os, sys
with open(os.path.join(os.environ["FAKE_DIR"], "calls.log"), "a") as f:
    f.write("{name} " + " ".join(sys.argv[1:]) + "\n")
{body}
sys.exit(int(os.environ.get("FAKE_{env}_RC", "0")))
"""

STAGE_STUB = r"""#!/usr/bin/env python3
# Fake pipeline stage: records argv, cwd, open fds and HEIMDALL_* env, then
# waits for a signal.
import json, os, signal, sys, time
role = os.path.basename(sys.argv[0]).split(".")[0]
if role == "test_data_synthesizer":
    role = "synthesizer"
rec = {"role": role, "pid": os.getpid(), "argv": sys.argv[1:], "cwd": os.getcwd(),
       "fds": sorted(int(x) for x in os.listdir("/dev/fd") if x.isdigit()),
       "env": {k: v for k, v in os.environ.items() if k.startswith("HEIMDALL_")}}
d = os.path.join(os.environ["FAKE_DIR"], "stages")
os.makedirs(d, exist_ok=True)
tmp = os.path.join(d, ".%s.%d" % (role, os.getpid()))
with open(tmp, "w") as f:
    json.dump(rec, f)
os.replace(tmp, os.path.join(d, role + ".json"))
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
for _ in range(1200):
    time.sleep(0.5)
"""

DAQ_STOP_STUB = r"""#!/bin/sh
# Recording daq_stop.sh: kills the pids listed in $FAKE_DIR/kill_on_stop,
# frees the fake ports with FAKE_STOP_FREES=1.
printf '%s|%s\n' "$(pwd -P)" "$*" >> "$FAKE_DIR/daq_stop.log"
if [ -f "$FAKE_DIR/kill_on_stop" ]; then
    for p in $(cat "$FAKE_DIR/kill_on_stop"); do kill -9 "$p" 2>/dev/null; done
fi
if [ "${FAKE_STOP_FREES:-0}" = 1 ]; then : > "$FAKE_DIR/busy_ports"; fi
exit 0
"""

FED_STOP_STUB = "#!/bin/sh\nprintf 'federation_stop %s\\n' \"$*\" >> \"$FAKE_DIR/calls.log\"\n"

IRQ_STUB = "#!/bin/sh\nprintf 'irq_tuning %s\\n' \"$*\" >> \"$FAKE_DIR/calls.log\"\n"

START_STUB = r"""#!/bin/bash
# Recording start script for the supervisor tests: launches fake stages and
# writes their PID files like the real start scripts.
env | grep '^HEIMDALL_' | sort > "$FAKE_DIR/start_env"
printf '%s %s\n' "$0" "$*" >> "$FAKE_DIR/start.log"
[ "${FAKE_START_RC:-0}" = 0 ] || exit "$FAKE_START_RC"
d="_logs/inst${FAKE_N:-0}/pids"
mkdir -p "$d"
rm -f "$d"/*.pid
for r in ${FAKE_ROLES:-rtl_daq rebuffer decimate delay_sync hw_controller iq_server}; do
    ( exec -a "$r.fake" "$FAKE_REAL_SLEEP" 300 ) < /dev/null > /dev/null 2>&1 &
    echo $! > "$d/$r.pid"
done
[ -n "${FAKE_EXTRA_PID:-}" ] && echo 1 > "$d/$FAKE_EXTRA_PID.pid"
exit 0
"""

FED_SERVICE_STUB = r"""#!/usr/bin/env python3
# Fake federation service: records argv, writes --pid-file, waits for TERM
import json, os, signal, sys, time
name = os.path.basename(sys.argv[0]).replace(".py", "")
args = sys.argv[1:]
with open(os.path.join(os.environ["FAKE_DIR"], name + ".argv.json"), "w") as f:
    json.dump({"argv": args, "pid": os.getpid()}, f)
if os.environ.get("FAKE_FED_EXIT") == "1":
    sys.exit(1)
if "--pid-file" in args:
    with open(args[args.index("--pid-file") + 1], "w") as f:
        f.write("%d\n" % os.getpid())
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
for _ in range(1200):
    time.sleep(0.5)
"""


def _write(path, text, mode=0o755):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A reaped-later zombie still answers kill -0; ps tells them apart
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return out.returncode == 0 and not out.stdout.strip().startswith("Z")


def _wait_for(cond, timeout=15.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


@unittest.skipIf(BASH is None, "bash is not installed")
class _Sandbox(unittest.TestCase):
    """A Firmware copy with stubbed stages and a sandbox PATH."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="hd_launch_"))
        self.root = os.path.join(self.tmp, "root")
        self.fw = os.path.join(self.root, "Firmware")
        self.fake = os.path.join(self.tmp, "fake")
        self.bin = os.path.join(self.tmp, "bin")
        self.run_dir = os.path.join(self.tmp, "run")
        self.proc = os.path.join(self.tmp, "proc")
        self.other = os.path.join(self.tmp, "other_copy", "Firmware")
        for d in (self.fw, self.fake, self.bin, self.proc, self.other,
                  os.path.join(self.fw, "_data_control"), os.path.join(self.fw, "_daq_core"),
                  os.path.join(self.fw, "_testing")):
            os.makedirs(d, exist_ok=True)
        self._pgids = []
        self._pids = []
        self._popens = []
        self._make_bin()
        self._make_tree()
        self.write_ini()
        # B.9: every script must reach the recording stub, never a real sudo
        which = subprocess.run(["/bin/sh", "-c", "command -v sudo"], env=self.env(),
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(which.stdout.strip(), os.path.join(self.bin, "sudo"), which.stderr)

    def tearDown(self):
        for pgid in self._pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        for pid in self._pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for p in self._popens:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        # Stages started by the scripts record their pids
        stages = os.path.join(self.fake, "stages")
        if os.path.isdir(stages):
            for name in os.listdir(stages):
                rec = json.loads(_read(os.path.join(stages, name), "{}") or "{}")
                if rec.get("pid"):
                    try:
                        os.kill(rec["pid"], signal.SIGKILL)
                    except OSError:
                        pass
        for dirpath, dirnames, _ in os.walk(self.tmp):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- construction -------------------------------------------------------

    def _make_bin(self):
        for tool in REAL_TOOLS:
            real = shutil.which(tool, path=SYSTEM_PATH)
            if real:
                os.symlink(real, os.path.join(self.bin, tool))
        _write(os.path.join(self.bin, "python3"), "#!/bin/sh\nexec %s \"$@\"\n" % sys.executable)
        _write(os.path.join(self.bin, "sudo"), SUDO_STUB)
        for name in ("taskset", "chrt"):
            _write(os.path.join(self.bin, name), PASSTHROUGH_STUB.replace("{name}", name))
        _write(os.path.join(self.bin, "nproc"), NPROC_STUB)
        _write(os.path.join(self.bin, "uname"), UNAME_STUB % shutil.which("uname", path=SYSTEM_PATH))
        _write(os.path.join(self.bin, "sleep"), SLEEP_STUB % shutil.which("sleep", path=SYSTEM_PATH))
        _write(os.path.join(self.bin, "systemctl"), SYSTEMCTL_STUB)
        _write(os.path.join(self.bin, "sysctl"), SYSCTL_STUB)
        _write(os.path.join(self.bin, "ss"), SS_STUB)
        _write(os.path.join(self.bin, "lsof"), LSOF_STUB)
        _write(os.path.join(self.bin, "pgrep"), PGREP_STUB)
        _write(os.path.join(self.bin, "pkill"), PKILL_STUB)
        real_flock = shutil.which("flock", path=SYSTEM_PATH) if IS_LINUX else None
        if real_flock:
            os.symlink(real_flock, os.path.join(self.bin, "flock"))
        else:
            _write(os.path.join(self.bin, "flock"), FLOCK_STUB)

    def _make_tree(self):
        for name in OWN_SCRIPTS:
            shutil.copy2(os.path.join(FIRMWARE, name), os.path.join(self.fw, name))
        _write(os.path.join(self.fw, "ini_checker.py"),
               PYTHON_TOOL_STUB.replace("{name}", "ini_checker").replace("{env}", "INI").replace("{body}", ""))
        _write(os.path.join(self.fw, "fir_filter_designer.py"),
               PYTHON_TOOL_STUB.replace("{name}", "fir_filter_designer").replace("{env}", "FIR").replace("{body}", ""))
        _write(os.path.join(self.fw, "_daq_core", "hw_discover.py"),
               PYTHON_TOOL_STUB.replace("{name}", "hw_discover").replace("{env}", "HWD").replace("{body}", "print('{}')"))
        _write(os.path.join(self.fw, "_daq_core", "auto_config.py"),
               PYTHON_TOOL_STUB.replace("{name}", "auto_config").replace("{env}", "AUTO").replace("{body}", ""))
        _write(os.path.join(self.fw, "daq_preflight.py"),
               PYTHON_TOOL_STUB.replace("{name}", "daq_preflight").replace("{env}", "PREFLIGHT").replace("{body}", ""))
        for stage in ("rtl_daq.out", "rebuffer.out", "decimate.out", "iq_server.out"):
            _write(os.path.join(self.fw, "_daq_core", stage), STAGE_STUB)
        for stage in ("delay_sync.py", "hw_controller.py"):
            _write(os.path.join(self.fw, "_daq_core", stage), STAGE_STUB)
        _write(os.path.join(self.fw, "_testing", "test_data_synthesizer.py"), STAGE_STUB)
        for svc in ("federation_coordinator.py", "federation_iq_router.py"):
            _write(os.path.join(self.fw, "_daq_core", svc), FED_SERVICE_STUB)
        _write(os.path.join(self.root, "util", "irq_tuning.sh"), IRQ_STUB)
        _write(os.path.join(self.root, "util", "host_tuning.sh"), IRQ_STUB.replace("irq_tuning", "host_tuning"))

    def write_ini(self, iid=0, stride=100, iface="eth", en_mon=0, en_status=0, status_port=5002,
                  en_pub=0, extra="", num_ch=5, path=None):
        text = BASE_INI.format(iid=iid, stride=stride, iface=iface, en_mon=en_mon,
                               en_status=en_status, status_port=status_port, en_pub=en_pub,
                               extra=extra, num_ch=num_ch)
        _write(path or os.path.join(self.fw, "daq_chain_config.ini"), text, 0o644)

    def stub_daq_stop(self, where=None):
        _write(os.path.join(where or self.fw, "daq_stop.sh"), DAQ_STOP_STUB)

    def env(self, **extra):
        env = {"PATH": self.bin, "HOME": self.tmp, "LC_ALL": "C", "LANG": "C",
               "FAKE_DIR": self.fake, "HEIMDALL_RUN_DIR": self.run_dir,
               "FAKE_REAL_SLEEP": shutil.which("sleep", path=SYSTEM_PATH),
               "HEIMDALL_PROC_ROOT": self.proc, "TMPDIR": self.tmp}
        env.update({k: str(v) for k, v in extra.items()})
        return env

    # -- running --------------------------------------------------------------

    def run_script(self, argv, env=None, cwd=None, timeout=90):
        """Run argv to completion; output goes to a file (stages inherit
        stdout and outlive the script). Returns (rc, output)."""
        out_path = os.path.join(self.fake, "out.%d" % len(self._pgids))
        with open(out_path, "w") as out:
            p = subprocess.Popen(argv, cwd=cwd or self.fw, env=env or self.env(), stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            self._pgids.append(p.pid)
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                raise
        return rc, _read(out_path)

    def popen_script(self, argv, env=None, cwd=None):
        out_path = os.path.join(self.fake, "bg.%d" % len(self._pgids))
        out = open(out_path, "w")
        p = subprocess.Popen(argv, cwd=cwd or self.fw, env=env or self.env(), stdin=subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        out.close()
        self._pgids.append(p.pid)
        self._popens.append(p)
        p.out_path = out_path
        return p

    def lib(self, body, env=None, shell="sh", cwd=None, timeout=60):
        """Run `. ./daq_common.sh; BODY` with the POSIX sh (or bash)."""
        return self.run_script([shell, "-c", ". ./daq_common.sh\n" + body], env=env, cwd=cwd,
                               timeout=timeout)

    def spawn(self, argv0, cwd=None, lock=None):
        """A detached process whose args start with argv0 (sleep under
        exec -a), registered in the fake proc tree. With lock=PATH it holds
        flock on PATH through fd 9. Returns its pid."""
        cwd = cwd or self.fw
        body = 'cd "$1" || exit 1\n'
        if lock:
            body += 'exec 9>"$3"; flock 9 || exit 1\n'
        body += '( exec -a "$2" "$FAKE_REAL_SLEEP" 300 ) < /dev/null > /dev/null 2>&1 &\necho $!\n'
        out = subprocess.run([BASH, "-c", body, "spawn", cwd, argv0, lock or ""], env=self.env(),
                             capture_output=True, text=True, timeout=30, start_new_session=True)
        pid = int(out.stdout.strip())
        self._pids.append(pid)
        pdir = os.path.join(self.proc, str(pid))
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "cmdline"), "wb") as f:
            f.write(argv0.replace(" ", "\0").encode() + b"\x00300\x00")
        os.symlink(os.path.realpath(cwd), os.path.join(pdir, "cwd"))
        self.assertTrue(_wait_for(lambda: argv0.split()[0] in subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout), argv0)
        return pid

    def write_entry(self, n, cwd, pids, cores="", run_dir=None):
        run_dir = run_dir or self.run_dir
        os.makedirs(run_dir, exist_ok=True)
        lines = ["SCHEMA=1", "INSTANCE_ID=%d" % n, "MODE=hw", "CWD=%s" % cwd, "STARTED_AT=1",
                 "GUARD=refuse", "LAUNCHER=manual", "UNIT=", "CORES=%s" % cores, "SERIALS=",
                 "PIDS=%s" % ",".join("%s:%d" % rp for rp in pids)]
        _write(os.path.join(run_dir, "inst%d.env" % n), "\n".join(lines) + "\n", 0o644)
        _write(os.path.join(run_dir, "inst%d.json" % n), json.dumps({"instance_id": n}) + "\n", 0o644)

    def own_env(self):
        """The HEIMDALL_* variables the test itself passes to every script."""
        return {k: v for k, v in self.env().items() if k.startswith("HEIMDALL_")}

    def log(self, name):
        return _read(os.path.join(self.fake, name))

    def wait_stages(self, roles=("rtl_daq", "rebuffer", "decimate", "delay_sync", "hw_controller", "iq_server")):
        """Background launches may still be running when the script exits."""
        for role in roles:
            self.stage(role)

    def stage(self, role):
        path = os.path.join(self.fake, "stages", role + ".json")
        self.assertTrue(_wait_for(lambda: os.path.exists(path)), "stage %s never started" % role)
        return json.loads(_read(path))


# ==========================================================================
# Syntax and lint
# ==========================================================================

class TestSyntax(unittest.TestCase):

    def _shell_files(self):
        files = [os.path.join(FIRMWARE, f) for f in sorted(os.listdir(FIRMWARE)) if f.endswith(".sh")]
        files += [os.path.join(UTIL, f) for f in sorted(os.listdir(UTIL)) if f.endswith(".sh")]
        return files

    @unittest.skipIf(BASH is None, "bash is not installed")
    def test_bash_n_every_script(self):
        for path in self._shell_files():
            with self.subTest(path=os.path.relpath(path, REPO)):
                r = subprocess.run([BASH, "-n", path], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_posix_sh_n(self):
        shells = [s for s in ("sh", "dash") if shutil.which(s)]
        for sh in shells:
            for name in ("daq_stop.sh", "daq_common.sh", "federation_start.sh", "federation_stop.sh"):
                with self.subTest(shell=sh, script=name):
                    r = subprocess.run([sh, "-n", os.path.join(FIRMWARE, name)], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipIf(shutil.which("shellcheck") is None, "shellcheck is not installed")
    def test_shellcheck_clean(self):
        own = [os.path.join(FIRMWARE, n) for n in OWN_SCRIPTS]
        own += [os.path.join(UTIL, "irq_tuning.sh"), os.path.join(UTIL, "host_tuning.sh")]
        r = subprocess.run(["shellcheck", "-S", "warning", "-x", "-e", "SC3043"] + own,
                           capture_output=True, text=True, cwd=FIRMWARE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        posix = [os.path.join(FIRMWARE, n) for n in ("daq_stop.sh", "daq_common.sh")]
        r = subprocess.run(["shellcheck", "-s", "dash", "-S", "warning", "-x", "-e", "SC3043"] + posix,
                           capture_output=True, text=True, cwd=FIRMWARE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_common_library_has_no_side_effects(self):
        """Sourcing daq_common.sh defines functions only."""
        with tempfile.TemporaryDirectory() as d:
            shutil.copy2(os.path.join(FIRMWARE, "daq_common.sh"), d)
            r = subprocess.run(["sh", "-c", 'before=$(set | wc -l); . ./daq_common.sh; ls -A; env | grep -E "^(HD_|HEIMDALL_)" || true'],
                               cwd=d, capture_output=True, text=True, env={"PATH": SYSTEM_PATH})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(), ["daq_common.sh"])
            self.assertEqual(r.stderr, "")


# ==========================================================================
# Full start scripts in the stub tree
# ==========================================================================

class TestStartDefault(_Sandbox):
    """registry off (no [host] keys): today's behaviour."""

    def _start(self, script="./daq_start_sm.sh", args=(), **env):
        rc, out = self.run_script([script] + list(args), env=self.env(**env))
        return rc, out

    def test_default_hw_start_opens_no_fd_exports_nothing_creates_no_run_dir(self):
        _write(os.path.join(self.fw, "_data_control", "recal_request"), "1", 0o644)
        _write(os.path.join(self.fw, "_data_control", "hwc_state"), "x", 0o644)
        rc, out = self._start()
        self.assertEqual(rc, 0, out)
        self.assertIn("Config file check [ OK ]", out)
        self.assertIn("Have a coffee watch radar", out)
        roles = {"rtl_daq", "rebuffer", "decimate", "delay_sync", "hw_controller", "iq_server"}
        for role in roles:
            rec = self.stage(role)
            with self.subTest(role=role):
                self.assertNotIn(8, rec["fds"])
                self.assertNotIn(9, rec["fds"])
                self.assertEqual(rec["env"], self.own_env())
        self.assertFalse(os.path.exists(self.run_dir), "registry dir created on the default path")
        piddir = os.path.join(self.fw, "_logs", "inst0", "pids")
        self.assertEqual({f[:-4] for f in os.listdir(piddir) if f.endswith(".pid")}, roles)
        self.assertFalse(os.path.exists(os.path.join(piddir, "run_dir")))
        for role in roles:
            pid = int(_read(os.path.join(piddir, role + ".pid")).strip())
            self.assertIn(role, subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                                               capture_output=True, text=True).stdout)
        calls = self.log("calls.log").splitlines()
        self.assertIn("ini_checker no_hw attached", calls)
        self.assertIn("auto_config _data_control/hw_caps.json daq_chain_config.ini", calls)
        self.assertNotIn("daq_preflight", "\n".join(calls))
        dc = os.path.join(self.fw, "_data_control")
        for name in ("fw_decimator_in", "bw_decimator_in", "fw_decimator_out", "bw_decimator_out",
                     "fw_delay_sync_iq", "bw_delay_sync_iq", "fw_delay_sync_hwc", "bw_delay_sync_hwc"):
            self.assertTrue(stat.S_ISFIFO(os.stat(os.path.join(dc, name)).st_mode), name)
        self.assertEqual(_read(os.path.join(dc, "iq_track_lock")), "0")
        self.assertFalse(os.path.exists(os.path.join(dc, "recal_request")))
        self.assertFalse(os.path.exists(os.path.join(dc, "hwc_state")))
        # Today's tiers: cores 0..3 and priorities 99/94/92/90/82/88
        self.assertEqual(sorted(self.log("taskset.log").split("\n")[:-1]),
                         sorted(["-c 0 chrt -f 99 _daq_core/rtl_daq.out", "-c 1 chrt -f 94 _daq_core/rebuffer.out 0",
                                 "-c 1 chrt -f 92 _daq_core/decimate.out",
                                 "-c 2 chrt -f 90 python3 _daq_core/delay_sync.py",
                                 "-c 3 chrt -f 82 sudo env PATH=%s python3 _daq_core/hw_controller.py" % self.bin,
                                 "-c 2 chrt -f 88 _daq_core/iq_server.out"]))
        # Host tuning: today's commands at today's positions
        sudo = self.log("sudo.log").splitlines()
        self.assertEqual(sudo[0], "sysctl -w kernel.sched_rt_runtime_us=-1")
        self.assertEqual(sudo[1], "../util/irq_tuning.sh")
        tee = sudo.index("tee /proc/sys/vm/drop_caches")
        hwc = [i for i, s in enumerate(sudo) if "hw_controller.py" in s][0]
        self.assertLess(tee, hwc)
        # The synthesizer would never get extra flags; the default port check
        # probed the three blocking ports with the listener-only ss
        ss = self.log("ss.log")
        for port in (5000, 5001, 5002):
            self.assertIn("-ltnH sport = :%d" % port, ss)
        self.assertNotIn("-ltnH sport = :5003", ss)

    def test_instance_1_today_core_warning_and_prefixed_files(self):
        self.write_ini(iid=1)
        rc, out = self._start(FAKE_NPROC=4)
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: 4 cores for 2 instances - RT processes of multiple instances will share cores (0,1,2,3)", out)
        self.assertIn("auto_config _data_control/inst1_hw_caps.json daq_chain_config.ini", self.log("calls.log"))
        self.assertTrue(stat.S_ISFIFO(os.stat(os.path.join(self.fw, "_data_control", "inst1_fw_decimator_in")).st_mode))
        self.assertEqual(_read(os.path.join(self.fw, "_data_control", "inst1_iq_track_lock")), "0")
        self.assertTrue(os.path.isdir(os.path.join(self.fw, "_logs", "inst1")))
        self.assertIn("-ltnH sport = :5102", self.log("ss.log"))

    def test_existing_track_lock_is_kept(self):
        _write(os.path.join(self.fw, "_data_control", "iq_track_lock"), "1", 0o644)
        rc, out = self._start()
        self.assertEqual(rc, 0, out)
        self.assertEqual(_read(os.path.join(self.fw, "_data_control", "iq_track_lock")), "1")

    def test_config_failure_exits_1_before_anything(self):
        rc, out = self._start(FAKE_INI_RC=1)
        self.assertEqual(rc, 1)
        self.assertIn("Config file check [ FAIL ]", out)
        self.assertEqual(self.log("sudo.log"), "")

    def test_fir_designer_failure_exits_1_in_both_scripts(self):
        for script in ("./daq_start_sm.sh", "./daq_synthetic_start.sh"):
            with self.subTest(script=script):
                rc, out = self._start(script, FAKE_FIR_RC=1)
                self.assertEqual(rc, 1, out)
                self.assertIn("DAQ chain not started!", out)
                self.assertFalse(os.path.exists(os.path.join(self.fake, "stages")))

    def test_default_synthetic_start(self):
        _write(os.path.join(self.fake, "busy_ports"), "5000 1130", 0o644)
        rc, out = self._start("./daq_synthetic_start.sh")
        self.assertEqual(rc, 0, out)
        self.assertIn("Config file check [OK]", out)
        self.assertIn("WARN: port 5000 is in use by", out)
        self.assertIn("WARN: port 1130 is in use by", out)
        self.assertNotIn("not free", out)
        self.assertEqual(self.log("daq_stop.log"), "")
        self.wait_stages(("synthesizer", "rebuffer", "decimate", "delay_sync", "hw_controller", "iq_server"))
        synth = self.stage("synthesizer")
        self.assertEqual(synth["argv"], [])
        for role in ("synthesizer", "rebuffer", "decimate", "delay_sync", "hw_controller", "iq_server"):
            rec = self.stage(role)
            self.assertNotIn(8, rec["fds"])
            self.assertNotIn(9, rec["fds"])
            self.assertEqual(rec["env"], self.own_env())
        self.assertEqual(self.log("taskset.log"), "")
        self.assertEqual(self.log("chrt.log").strip(), "-f 99 _daq_core/decimate.out")
        self.assertIn("env PATH=%s python3 _daq_core/hw_controller.py" % self.bin, self.log("sudo.log"))
        self.assertNotIn("sysctl", self.log("sudo.log"))
        self.assertIn("ini_checker no_hw\n", self.log("calls.log"))
        self.assertFalse(os.path.exists(self.run_dir))
        piddir = os.path.join(self.fw, "_logs", "inst0", "pids")
        self.assertIn("synthesizer.pid", os.listdir(piddir))

    def test_synthetic_flags_passed_only_when_given(self):
        rc, out = self._start("./daq_synthetic_start.sh", ["--pace", "--blocks", "0", "--seed", "7"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.stage("synthesizer")["argv"], ["--pace", "--blocks", "0", "--seed", "7"])

    def test_synthetic_rt_uses_hardware_tiers(self):
        self.write_ini(extra="[host]\ncpu_cores = 4,5,6,7\nrt_priority_offset = 2\n")
        rc, out = self._start("./daq_synthetic_start.sh", ["--rt"], FAKE_NPROC=8)
        self.assertEqual(rc, 0, out)
        self.wait_stages(("synthesizer", "rebuffer", "decimate", "delay_sync", "hw_controller", "iq_server"))
        self.assertEqual(self.stage("synthesizer")["argv"], ["--pace"])
        ts = self.log("taskset.log")
        self.assertIn("-c 4 chrt -f 97 python3 _testing/test_data_synthesizer.py --pace", ts)
        self.assertIn("-c 5 chrt -f 90 _daq_core/decimate.out", ts)
        self.assertIn("-c 7 chrt -f 80 sudo env", ts)

    def test_host_keys_core_layout(self):
        self.write_ini(extra="[host]\ncpu_cores = 4,5,6,7\ndelay_sync_cores = 6,7\nrt_priority_offset = 10\n")
        rc, out = self._start(FAKE_NPROC=8)
        self.assertEqual(rc, 0, out)
        self.wait_stages()
        ts = self.log("taskset.log")
        self.assertIn("-c 4 chrt -f 89 _daq_core/rtl_daq.out", ts)
        self.assertIn("-c 5 chrt -f 84 _daq_core/rebuffer.out 0", ts)
        self.assertIn("-c 5 chrt -f 82 _daq_core/decimate.out", ts)
        self.assertIn("-c 6,7 chrt -f 80 python3 _daq_core/delay_sync.py", ts)
        self.assertIn("-c 7 chrt -f 72 sudo env", ts)
        self.assertIn("-c 6 chrt -f 78 _daq_core/iq_server.out", ts)
        self.assertNotIn("share cores", out)

    def test_no_host_tuning_flag_and_key(self):
        rc, out = self._start(args=["--no-host-tuning"])
        self.assertEqual(rc, 0, out)
        self.wait_stages()
        self.assertIn("host tuning skipped (disabled)", out)
        sudo = self.log("sudo.log")
        self.assertNotIn("sysctl", sudo)
        self.assertNotIn("tee /proc/sys/vm/drop_caches", sudo)

    def test_preflight_strict_exit_4_and_warn_continues(self):
        self.write_ini(extra="[host]\npreflight = strict\n")
        rc, out = self._start(FAKE_PREFLIGHT_RC=2)
        self.assertEqual(rc, 4, out)
        self.assertIn("daq_preflight --config daq_chain_config.ini --plus-live", self.log("calls.log"))
        self.assertFalse(os.path.exists(os.path.join(self.fake, "stages")))
        self.write_ini(extra="[host]\npreflight = warn\n")
        rc, out = self._start(FAKE_PREFLIGHT_RC=2)
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: preflight reported problems (exit 2)", out)


class TestStartRegistry(_Sandbox):
    """[host] instance_registry = 1: registry files, lock fd 9, exports."""

    def test_registry_entry_shape_lock_and_exports(self):
        self.write_ini(en_mon=1, en_status=1, status_port=6002, extra="[host]\ninstance_registry = 1\n")
        rc, out = self.run_script(["./daq_start_sm.sh"], env=self.env(HEIMDALL_LAUNCHER="systemd-oneshot",
                                                                      HEIMDALL_UNIT="heimdall-daq.service"))
        self.assertEqual(rc, 0, out)
        env_path = os.path.join(self.run_dir, "inst0.env")
        json_path = os.path.join(self.run_dir, "inst0.json")
        self.assertTrue(os.path.exists(env_path) and os.path.exists(json_path), out)
        self.assertEqual(stat.S_IMODE(os.stat(json_path).st_mode), 0o644)
        kv = dict(line.split("=", 1) for line in _read(env_path).splitlines())
        self.assertEqual(list(kv), ["SCHEMA", "INSTANCE_ID", "MODE", "CWD", "STARTED_AT", "GUARD", "LAUNCHER",
                                    "UNIT", "PORT_IQ", "PORT_CTL", "PORT_STATUS", "PORT_EVENTS", "PORT_ZMQ",
                                    "CORES", "SERIALS", "PIDS"])
        self.assertEqual(kv["MODE"], "hw")
        self.assertEqual(kv["CWD"], self.fw)
        self.assertEqual(kv["LAUNCHER"], "systemd-oneshot")
        self.assertEqual(kv["PORT_STATUS"], "6002")
        self.assertEqual(kv["PORT_EVENTS"], "")
        self.assertEqual(kv["CORES"], "0,1,2,3")
        self.assertEqual(kv["SERIALS"], "1000,1001,1002,1003,1004")
        doc = json.loads(_read(json_path))
        self.assertEqual(set(doc), {"schema", "instance_id", "mode", "node_id", "cwd", "install_root",
                                    "config_sha256", "started_at", "guard", "launcher", "unit", "ports",
                                    "cores", "rt_priority_offset", "serials", "db_dir", "pids"})
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["instance_id"], 0)
        self.assertEqual(doc["launcher"], "systemd-oneshot")
        self.assertEqual(doc["unit"], "heimdall-daq.service")
        self.assertEqual(doc["guard"], "legacy")
        self.assertEqual(doc["install_root"], self.root)
        self.assertEqual(doc["ports"], {"iq": 5000, "ctl": 5001, "status": 6002, "events": None, "zmq": 1130})
        self.assertEqual(doc["cores"], [0, 1, 2, 3])
        self.assertEqual(doc["serials"], ["1000", "1001", "1002", "1003", "1004"])
        self.assertIsNone(doc["db_dir"])
        self.assertTrue(doc["node_id"].endswith(":0"))
        self.assertEqual(len(doc["config_sha256"]), 64)
        self.assertEqual(set(doc["pids"]), {"rtl_daq", "rebuffer", "decimate", "delay_sync", "hw_controller",
                                            "iq_server"})
        self.assertEqual(_read(os.path.join(self.fw, "_logs", "inst0", "pids", "run_dir")).strip(), self.run_dir)
        ds = self.stage("delay_sync")
        self.assertIn(9, ds["fds"])
        self.assertNotIn(8, ds["fds"])
        self.assertEqual(ds["env"]["HEIMDALL_INSTANCE_LOCK"], os.path.join(self.run_dir, "inst0.lock"))
        self.assertEqual(ds["env"]["HEIMDALL_INSTANCE_GUARD"], "legacy")
        self.assertEqual(ds["env"]["HEIMDALL_RUN_DIR"], self.run_dir)
        self.assertEqual(ds["env"]["HEIMDALL_LAUNCHER"], "systemd-oneshot")
        # The stages hold the instance lock
        r = subprocess.run([BASH, "-c", 'exec 9>"$1"; flock -n 9', "x", os.path.join(self.run_dir, "inst0.lock")],
                           env=self.env())
        self.assertNotEqual(r.returncode, 0, "instance lock not held by the stages")

    def test_manual_launcher_and_synthetic_mode(self):
        self.write_ini(extra="[host]\ninstance_registry = 1\n")
        rc, out = self.run_script(["./daq_synthetic_start.sh"])
        self.assertEqual(rc, 0, out)
        doc = json.loads(_read(os.path.join(self.run_dir, "inst0.json")))
        self.assertEqual(doc["mode"], "synthetic")
        self.assertEqual(doc["launcher"], "manual")
        self.assertEqual(doc["serials"], [])
        self.assertEqual(doc["cores"], [])
        self.assertIn("synthesizer", doc["pids"])
        self.assertNotIn("rtl_daq", doc["pids"])

    def test_refuse_guard_second_copy_exits_3_before_fifos(self):
        self.write_ini(extra="[host]\ninstance_registry = 1\ninstance_guard = refuse\n")
        os.makedirs(self.run_dir)
        holder = self.spawn("rtl_daq.out", cwd=self.other, lock=os.path.join(self.run_dir, "inst0.lock"))
        self.write_entry(0, self.other, [("rtl_daq", holder)])
        rc, out = self.run_script(["./daq_start_sm.sh"])
        self.assertEqual(rc, 3, out)
        self.assertIn("instance 0 is already running from %s (use --takeover to stop it)" % self.other, out)
        self.assertFalse(os.path.exists(os.path.join(self.fw, "_data_control", "fw_decimator_in")))
        self.assertTrue(_alive(holder))


# ==========================================================================
# daq_common.sh functions
# ==========================================================================

class TestRunDirAndLock(_Sandbox):

    def test_run_dir_created(self):
        rc, out = self.lib('hd_run_dir')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), self.run_dir)
        self.assertTrue(os.path.isdir(self.run_dir))

    @unittest.skipIf(IS_ROOT, "root can write any directory")
    def test_run_dir_unwritable_falls_back(self):
        os.makedirs(self.run_dir)
        os.chmod(self.run_dir, 0o555)
        fallback = "/tmp/heimdall-run-%d" % os.getuid()
        existed = os.path.exists(fallback)
        try:
            rc, out = self.lib('d=$(hd_run_dir); echo "RESULT=$d"')
            self.assertEqual(rc, 0, out)
            self.assertIn("RESULT=%s" % fallback, out)
            self.assertIn("WARN: run directory %s is not usable; using %s" % (self.run_dir, fallback), out)
        finally:
            if not existed:
                shutil.rmtree(fallback, ignore_errors=True)

    def test_lock_open_failure_warns_and_continues(self):
        os.makedirs(os.path.join(self.run_dir, "inst0.lock"))
        rc, out = self.lib('HD_RUN="$HEIMDALL_RUN_DIR"; HD_INSTANCE_GUARD=refuse; hd_instance_lock 0; '
                           'echo "LOCKED=$HD_LOCKED LOCKVAR=${HEIMDALL_INSTANCE_LOCK:-none}"; echo still-here')
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: cannot open instance lock", out)
        self.assertIn("LOCKED=0 LOCKVAR=none", out)
        self.assertIn("still-here", out)

    def test_lock_free_is_taken(self):
        os.makedirs(self.run_dir)
        rc, out = self.lib('HD_RUN="$HEIMDALL_RUN_DIR"; HD_INSTANCE_GUARD=refuse; hd_instance_lock 2; '
                           'echo "LOCKED=$HD_LOCKED PENDING=$HD_LOCK_PENDING GUARD=$HEIMDALL_INSTANCE_GUARD"')
        self.assertEqual(rc, 0, out)
        self.assertIn("LOCKED=1 PENDING=0 GUARD=refuse", out)

    def _held(self, n, cwd):
        os.makedirs(self.run_dir, exist_ok=True)
        holder = self.spawn("rtl_daq.out", cwd=cwd, lock=os.path.join(self.run_dir, "inst%d.lock" % n))
        self.write_entry(n, os.path.realpath(cwd), [("rtl_daq", holder)])
        return holder

    def _lock(self, guard, n=1, takeover=0):
        return self.lib('HD_RUN="$HEIMDALL_RUN_DIR"; HD_INSTANCE_GUARD=%s; HD_TAKEOVER=%d; hd_instance_lock %d; '
                        'echo "LOCKED=$HD_LOCKED PENDING=$HD_LOCK_PENDING"' % (guard, takeover, n))

    def test_legacy_same_copy_warns_and_defers(self):
        self._held(1, self.fw)
        rc, out = self._lock("legacy")
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: instance 1 is already running from this copy; the port check will stop it "
                      "(today's behaviour)", out)
        self.assertIn("LOCKED=0 PENDING=1", out)

    def test_legacy_other_copy_warns_and_defers(self):
        self._held(1, self.other)
        rc, out = self._lock("legacy")
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: instance 1 is also running from %s; today's behaviour applies: the port check "
                      "stops all DAQ processes on this host" % self.other, out)
        self.assertIn("LOCKED=0 PENDING=1", out)

    def test_synthetic_legacy_conflict_continues_without_wait(self):
        for cwd, text in ((self.fw, "WARN: instance 1 is already running from this copy; it is not stopped "
                                    "(today's synthetic start) - continuing without the instance lock"),
                          (self.other, "WARN: instance 1 is also running from %s; it is not stopped "
                                       "(today's synthetic start) - continuing without the instance lock"
                                       % self.other)):
            with self.subTest(cwd=cwd):
                holder = self._held(1, cwd)
                t0 = time.monotonic()
                rc, out = self.lib('HD_RUN="$HEIMDALL_RUN_DIR"; HD_INSTANCE_GUARD=legacy; '
                                   'hd_instance_lock 1 synthetic; hd_lock_deferred; '
                                   'echo "LOCKED=$HD_LOCKED PENDING=$HD_LOCK_PENDING"')
                self.assertEqual(rc, 0, out)
                self.assertIn(text, out)
                self.assertIn("LOCKED=0 PENDING=0", out)
                self.assertNotIn("still held", out)
                self.assertLess(time.monotonic() - t0, 10)
                os.kill(holder, signal.SIGKILL)
                self.assertTrue(_wait_for(lambda: not _alive(holder)))

    def test_registry_begin_prunes_stale_entries(self):
        dead = self.spawn("rtl_daq.out", cwd=self.other)
        os.kill(dead, signal.SIGKILL)
        self.assertTrue(_wait_for(lambda: not _alive(dead)))
        self.write_entry(4, self.other, [("rtl_daq", dead)])
        live = self.spawn("rtl_daq.out", cwd=self.other)
        self.write_entry(5, self.other, [("rtl_daq", live)])
        rc, out = self.lib('HD_INSTANCE_GUARD=legacy; hd_registry_begin 0; echo "LOCKED=$HD_LOCKED"')
        self.assertEqual(rc, 0, out)
        self.assertIn("Removed stale registry entry %s" % os.path.join(self.run_dir, "inst4"), out)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "inst4.env")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "inst4.json")))
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "inst5.env")))

    @unittest.skipIf(os.geteuid() == 0, "root can remove from a read-only directory")
    def test_prune_reports_failed_removal(self):
        self.write_entry(4, self.other, [("rtl_daq", 999999)])
        os.chmod(self.run_dir, 0o555)
        rc, out = self.lib("hd_registry_prune")
        self.assertIn("WARN: cannot remove stale registry entry %s" % os.path.join(self.run_dir, "inst4"), out)
        self.assertNotIn("Removed stale", out)

    def test_deferred_lock_after_old_chain_stopped(self):
        holder = self._held(1, self.other)
        rc, out = self.lib('HD_RUN="$HEIMDALL_RUN_DIR"; HD_INSTANCE_GUARD=legacy; hd_instance_lock 1; '
                           'kill -9 %d; hd_lock_deferred; echo "LOCKED=$HD_LOCKED"' % holder)
        self.assertEqual(rc, 0, out)
        self.assertIn("LOCKED=1", out)

    def test_refuse_same_copy_restarts(self):
        holder = self._held(1, self.fw)
        self.stub_daq_stop()
        _write(os.path.join(self.fake, "kill_on_stop"), str(holder), 0o644)
        rc, out = self._lock("refuse")
        self.assertEqual(rc, 0, out)
        self.assertIn("instance 1 is running from this copy - restarting", out)
        self.assertIn("LOCKED=1 PENDING=0", out)
        self.assertEqual(self.log("daq_stop.log").strip(), "%s|1" % self.fw)

    def test_refuse_other_copy_exits_3(self):
        self._held(1, self.other)
        rc, out = self._lock("refuse")
        self.assertEqual(rc, 3, out)
        self.assertIn("instance 1 is already running from %s (use --takeover to stop it)" % self.other, out)

    def test_refuse_other_copy_takeover(self):
        holder = self._held(1, self.other)
        self.stub_daq_stop(where=self.other)
        _write(os.path.join(self.fake, "kill_on_stop"), str(holder), 0o644)
        rc, out = self._lock("refuse", takeover=1)
        self.assertEqual(rc, 0, out)
        self.assertIn("LOCKED=1", out)
        self.assertEqual(self.log("daq_stop.log").strip(), "%s|1" % self.other)

    def _mutex_pair(self, guard):
        os.makedirs(self.run_dir)
        body = ('HD_RUN="$HEIMDALL_RUN_DIR"; HD_INSTANCE_GUARD=%s; hd_startup_mutex 0; '
                'echo "$(date +%%s) acquired" >> "$FAKE_DIR/mutex.$1"; sleep 2; echo released >> "$FAKE_DIR/mutex.$1"')
        env = self.env()
        a = subprocess.Popen(["sh", "-c", ". ./daq_common.sh\n" + body % guard, "x", "a"], cwd=self.fw, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self._pgids.append(a.pid)
        self.assertTrue(_wait_for(lambda: "acquired" in self.log("mutex.a")))
        t_b = time.monotonic()
        b = subprocess.Popen(["sh", "-c", ". ./daq_common.sh\n" + body % guard, "x", "b"], cwd=self.fw, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self._pgids.append(b.pid)
        out_b, _ = b.communicate(timeout=30)
        a.communicate(timeout=30)
        return a.returncode, b.returncode, out_b, time.monotonic() - t_b

    def test_parallel_starts_refuse(self):
        rc_a, rc_b, out_b, _ = self._mutex_pair("refuse")
        self.assertEqual(rc_a, 0)
        self.assertEqual(rc_b, 3)
        self.assertIn("ERROR: another start of instance 0 is in progress", out_b)
        self.assertEqual(self.log("mutex.b"), "")

    def test_parallel_starts_legacy_waits(self):
        rc_a, rc_b, _, elapsed = self._mutex_pair("legacy")
        self.assertEqual((rc_a, rc_b), (0, 0))
        self.assertIn("released", self.log("mutex.a"))
        self.assertIn("acquired", self.log("mutex.b"))
        self.assertGreater(elapsed, 1.0, "second start did not wait for the mutex")


class TestCoreGuard(_Sandbox):

    def _guard(self, policy, registry=1, cores="2,3,4,5", n=1, nproc=8):
        c = cores.split(",")
        return self.lib('HD_INSTANCE_ID=%d; HD_INSTANCE_REGISTRY=%d; HD_ALLOW_CORE_SHARING=%s; HD_CPU_CORES=%s; '
                        'NUM_CORES=%d; hd_core_guard %s; echo rc=$?' % (n, registry, policy, cores, nproc, " ".join(c)))

    def setUp(self):
        super().setUp()
        pid = self.spawn("rtl_daq.out", cwd=self.other)
        self.write_entry(0, self.other, [("rtl_daq", pid)], cores="0,1,2,3")

    def test_warn(self):
        rc, out = self._guard("warn")
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: cores 2,3 of instance 1 are also used by instance 0 running from %s" % self.other, out)

    def test_refuse(self):
        rc, out = self._guard("refuse")
        self.assertEqual(rc, 3, out)
        self.assertIn("cores 2,3 of instance 1 are used by instance 0", out)

    def test_allow_silent(self):
        rc, out = self._guard("allow")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), "rc=0")

    def test_no_overlap_silent(self):
        rc, out = self._guard("refuse", cores="4,5,6,7")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), "rc=0")

    def test_registry_off_only_today_line(self):
        rc, out = self._guard("refuse", registry=0)
        self.assertEqual(out.strip(), "rc=0")
        rc, out = self.lib('HD_INSTANCE_ID=1; HD_INSTANCE_REGISTRY=0; HD_CPU_CORES=; NUM_CORES=4; '
                           'hd_core_guard 0 1 2 3')
        self.assertEqual(out.strip(), "WARN: 4 cores for 2 instances - RT processes of multiple instances "
                                      "will share cores (0,1,2,3)")


class TestPortCheck(_Sandbox):

    def setUp(self):
        super().setUp()
        self.stub_daq_stop()

    def _check(self, mode="hw", **env):
        return self.lib('hd_read_config; hd_port_check %s; echo "passed rc=$?"' % mode, env=self.env(**env))

    def busy(self, *ports):
        _write(os.path.join(self.fake, "busy_ports"), " ".join(str(p) for p in ports), 0o644)

    def test_free_ports_listener_only_probes(self):
        rc, out = self._check()
        self.assertEqual(rc, 0, out)
        self.assertIn("passed", out)
        ss = self.log("ss.log")
        for p in (5000, 5001, 5002, 1130):
            self.assertIn("-ltnH sport = :%d" % p, ss)
        lsof = self.log("lsof.log")
        self.assertIn("-nP -iTCP:5000 -sTCP:LISTEN", lsof)
        self.assertEqual(self.log("daq_stop.log"), "")

    def test_lsof_only(self):
        os.remove(os.path.join(self.bin, "ss"))
        self.busy(5001)
        rc, out = self._check(FAKE_FAST_SLEEP=1, FAKE_STOP_FREES=1)
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN:Ports used by the DAQ chain instance 0 are not free! (5000, 5001 & 5002)", out)

    def test_no_tool_skips_check(self):
        os.remove(os.path.join(self.bin, "ss"))
        os.remove(os.path.join(self.bin, "lsof"))
        rc, out = self._check()
        self.assertIn("WARN: Neither lsof nor ss available - skipping port availability check", out)
        self.assertIn("passed", out)

    def test_configured_status_port_with_offset(self):
        self.write_ini(iid=1, status_port=6002)
        rc, out = self._check()
        self.assertEqual(rc, 0, out)
        ss = self.log("ss.log")
        self.assertIn("sport = :6102", ss)
        self.assertNotIn("sport = :5102", ss)
        self.assertIn("sport = :5100", ss)
        self.assertIn("sport = :1230", ss)

    def test_advisory_ports_never_block(self):
        self.write_ini(en_mon=1, en_pub=1)
        self.busy(1130, 5003)
        rc, out = self._check()
        self.assertEqual(rc, 0, out)
        self.assertIn('WARN: port 1130 is in use by "fakesrv",pid=4242,fd=3', out)
        self.assertIn("WARN: port 5003 is in use by", out)
        self.assertIn("passed", out)
        self.assertEqual(self.log("daq_stop.log"), "")

    def test_pub_port_not_probed_when_disabled(self):
        self.busy(5003)
        rc, out = self._check()
        self.assertNotIn("5003", out + self.log("ss.log"))

    def test_busy_ten_rounds_then_exit_1(self):
        self.busy(5000)
        rc, out = self._check(FAKE_FAST_SLEEP=1)
        self.assertEqual(rc, 1, out)
        self.assertNotIn("passed", out)
        self.assertIn("ERROR: Ports (5000, 5001 & 5002) still busy after 10 attempts - DAQ chain not started!", out)
        self.assertIn('users:(("fakesrv",pid=4242,fd=3))', out)
        stops = self.log("daq_stop.log").splitlines()
        self.assertEqual(stops, ["%s|" % self.fw] * 9)

    def test_legacy_round_uses_global_stop(self):
        self.busy(5001)
        rc, out = self._check(FAKE_FAST_SLEEP=1, FAKE_STOP_FREES=1)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log("daq_stop.log").splitlines(), ["%s|" % self.fw])

    def test_scoped_stop_env_under_legacy(self):
        self.write_ini(iid=2)
        self.busy(5200)
        rc, out = self._check(FAKE_FAST_SLEEP=1, FAKE_STOP_FREES=1, HEIMDALL_SCOPED_STOP=1)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log("daq_stop.log").splitlines(), ["%s|2" % self.fw])

    def test_refuse_guard_scoped_stop(self):
        self.write_ini(iid=2, extra="[host]\ninstance_registry = 1\ninstance_guard = refuse\n")
        self.busy(5201)
        rc, out = self._check(FAKE_FAST_SLEEP=1, FAKE_STOP_FREES=1)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log("daq_stop.log").splitlines(), ["%s|2" % self.fw])

    def test_synthetic_legacy_advisory_only(self):
        self.busy(5000, 5001, 5002)
        rc, out = self._check("synthetic")
        self.assertEqual(rc, 0, out)
        for p in (5000, 5001, 5002):
            self.assertIn("WARN: port %d is in use by" % p, out)
        self.assertIn("passed", out)
        self.assertEqual(self.log("daq_stop.log"), "")

    def test_synthetic_refuse_like_hw(self):
        self.write_ini(extra="[host]\ninstance_registry = 1\ninstance_guard = refuse\n")
        self.busy(5000)
        rc, out = self._check("synthetic", FAKE_FAST_SLEEP=1)
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(self.log("daq_stop.log").splitlines()), 9)


class TestHostTuningHooks(_Sandbox):

    def _tune(self, value, extra_keys="", **env):
        self.write_ini(extra="[host]\nhost_tuning = %s\n%s" % (value, extra_keys))
        return self.lib('hd_read_config; hd_host_tuning_early; echo "-- late"; hd_host_tuning_late',
                        env=self.env(**env))

    def test_always_runs_today_commands(self):
        rc, out = self._tune("always")
        self.assertEqual(rc, 0, out)
        sudo = self.log("sudo.log").splitlines()
        self.assertEqual(sudo[:2], ["sysctl -w kernel.sched_rt_runtime_us=-1", "../util/irq_tuning.sh"])
        self.assertEqual(sudo[-1], "tee /proc/sys/vm/drop_caches")
        self.assertNotIn("host_tuning.sh", "\n".join(sudo))

    def test_always_appends_irq_keys(self):
        rc, out = self._tune("always", "usb_irq_core = 2\nusb_controller = xhci_hcd:usb3\n")
        self.assertIn("../util/irq_tuning.sh --usb-core 2 --controller xhci_hcd:usb3", self.log("sudo.log"))

    def test_value_1_runs_host_tuning_apply_only(self):
        rc, out = self._tune("1", "usb_irq_core = 1\n")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log("sudo.log").splitlines(), ["./../util/host_tuning.sh --apply --usb-core 1"])

    def test_auto_skips_when_unit_active(self):
        rc, out = self._tune("auto", FAKE_SYSTEMCTL_ACTIVE=1)
        self.assertIn("host tuning skipped (heimdall-host-tuning.service is active)", out)
        self.assertEqual(self.log("sudo.log"), "")
        self.assertIn("is-active --quiet heimdall-host-tuning.service", self.log("systemctl.log"))

    def test_auto_skips_when_other_instance_live(self):
        pid = self.spawn("rtl_daq.out", cwd=self.other)
        self.write_entry(3, self.other, [("rtl_daq", pid)])
        rc, out = self._tune("auto")
        self.assertIn("host tuning skipped (instance 3 is running)", out)
        self.assertEqual(self.log("sudo.log"), "")

    def test_auto_applies_otherwise(self):
        rc, out = self._tune("auto")
        self.assertEqual(self.log("sudo.log").splitlines(), ["./../util/host_tuning.sh --apply"])

    def test_zero_and_flag_disable(self):
        rc, out = self._tune("0")
        self.assertIn("host tuning skipped (disabled)", out)
        self.assertEqual(self.log("sudo.log"), "")
        rc, out = self._tune("always", HD_NO_HOST_TUNING=1)
        self.assertIn("host tuning skipped (disabled)", out)
        self.assertEqual(self.log("sudo.log"), "")


class TestAbortLaunch(_Sandbox):

    def test_failure_after_launch_stops_instance_and_keeps_status(self):
        self.stub_daq_stop()
        rc, out = self.lib("hd_arm_abort 4; exit 1", shell="bash")
        self.assertEqual(rc, 1, out)
        self.assertIn("Start of instance 4 failed after launching stages (exit 1) - stopping it", out)
        self.assertEqual(self.log("daq_stop.log").strip(), "%s|4" % self.fw)

    def test_success_and_disarm_do_nothing(self):
        self.stub_daq_stop()
        rc, _ = self.lib("hd_arm_abort 4; exit 0", shell="bash")
        self.assertEqual(rc, 0)
        rc, _ = self.lib("hd_arm_abort 4; hd_disarm_abort; exit 1", shell="bash")
        self.assertEqual(rc, 1)
        self.assertEqual(self.log("daq_stop.log"), "")


class TestReadConfig(_Sandbox):

    def test_fallbacks_without_ini(self):
        os.remove(os.path.join(self.fw, "daq_chain_config.ini"))
        rc, out = self.lib('hd_read_config; echo "$HD_INSTANCE_ID $HD_PORT_STRIDE $HD_OUT_IFACE $HD_FPGA_ENABLE '
                           '$HD_GPU_ENABLE $HD_STATUS_SERVER_PORT $HD_INSTANCE_REGISTRY $HD_INSTANCE_GUARD '
                           '$HD_HOST_TUNING $HD_PREFLIGHT $HD_ALLOW_CORE_SHARING"')
        self.assertEqual(out.strip(), "0 100 eth 0 0 5002 0 legacy always off warn")

    def test_today_validation_of_ids(self):
        self.write_ini(iid="x1", stride="abc")
        rc, out = self.lib('hd_read_config; echo "$HD_INSTANCE_ID $HD_PORT_STRIDE"')
        self.assertEqual(out.strip(), "0 100")

    def test_bad_derived_value_keeps_basic_keys(self):
        for key, value in (("heartbeat_interval", "1e400"), ("heartbeat_interval", "1e308"),
                           ("status_update_interval", "nan"), ("cpi_size", "nan")):
            with self.subTest(key=key, value=value):
                self.write_ini(iid=2, stride=50)
                path = os.path.join(self.fw, "daq_chain_config.ini")
                text = _read(path)
                if key == "cpi_size":
                    text = text.replace("cpi_size = 1048576", "cpi_size = %s" % value)
                else:
                    text = text.replace("[monitoring]\n", "[monitoring]\n%s = %s\n" % (key, value))
                _write(path, text, 0o644)
                rc, out = self.lib('hd_read_config; echo "iid=$HD_INSTANCE_ID stride=$HD_PORT_STRIDE '
                                   'serials=$HD_SERIALS stall=$HD_STALL_AUTO"')
                self.assertEqual(rc, 0, out)
                self.assertIn("iid=2 stride=50 serials=1000,1001,1002,1003,1004", out)

    def test_stall_auto_default_161(self):
        rc, out = self.lib('hd_read_config; echo "$HD_STALL_AUTO $HD_K_EFF"')
        self.assertEqual(out.strip(), "161 100")

    def test_serial_resolution_and_db_dir(self):
        self.write_ini(iid=3, extra="[database]\nen_db = 1\ndb_dir = _db\ninstance_subdir = 1\n")
        with open(os.path.join(self.fw, "daq_chain_config.ini")) as f:
            text = f.read().replace("num_ch = 5", "num_ch = 3\nserial_base = 1300")
        _write(os.path.join(self.fw, "daq_chain_config.ini"), text, 0o644)
        rc, out = self.lib('hd_read_config; echo "$HD_SERIALS|$HD_DB_DIR"')
        self.assertEqual(out.strip(), "1300,1301,1302|%s" % os.path.join(self.fw, "_db", "inst3"))


# ==========================================================================
# daq_stop.sh
# ==========================================================================

class TestDaqStop(_Sandbox):

    def setUp(self):
        super().setUp()
        _write(os.path.join(self.fw, "federation_stop.sh"), FED_STOP_STUB)

    def stop(self, *args, **env):
        env.setdefault("FAKE_FAST_SLEEP", 1)
        return self.run_script(["./daq_stop.sh"] + list(args), env=self.env(**env))

    def pidfile(self, n, role, pid):
        _write(os.path.join(self.fw, "_logs", "inst%d" % n, "pids", role + ".pid"), "%d\n" % pid, 0o644)

    def pkill_lines(self):
        return [l[len("pkill "):] for l in self.log("calls.log").splitlines() if l.startswith("pkill ")]

    def test_no_arg_global_sweep_keeps_live_other_copy_entry(self):
        mine = self.spawn("rebuffer.out", cwd=self.fw)
        self.pidfile(0, "rebuffer", mine)
        other = self.spawn("rtl_daq.out", cwd=self.other)
        self.write_entry(1, self.other, [("rtl_daq", other)])
        dead = self.spawn("decimate.out", cwd=self.other)
        os.kill(dead, signal.SIGKILL)
        self.assertTrue(_wait_for(lambda: not _alive(dead)))
        self.write_entry(2, self.other, [("decimate", dead)])
        rc, out = self.stop()
        self.assertTrue(out.startswith("Shut down DAQ chain .."), out)
        self.assertEqual(self.pkill_lines(), TODAY_TERM + TODAY_KILL)
        self.assertTrue(_wait_for(lambda: not _alive(mine)))
        self.assertTrue(_alive(other))
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "inst1.env")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "inst2.env")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "inst2.json")))
        self.assertFalse(os.path.exists(os.path.join(self.fw, "_logs", "inst0", "pids", "rebuffer.pid")))

    def test_no_arg_without_registry_files_is_today(self):
        rc, out = self.stop()
        self.assertEqual(self.pkill_lines(), TODAY_TERM + TODAY_KILL)
        self.assertFalse(os.path.exists(self.run_dir))
        self.assertEqual(out.strip(), "Shut down DAQ chain ..")

    def test_legacy_signal(self):
        self.stop("--legacy")
        lines = self.pkill_lines()
        self.assertEqual(lines[:7], [l.replace("-TERM", "-64") for l in TODAY_TERM])

    def test_this_copy_signals_only_same_cwd(self):
        mine = self.spawn("rtl_daq.out", cwd=self.fw)
        mine_py = self.spawn("python3 _daq_core/delay_sync.py", cwd=self.fw)
        theirs = self.spawn("rtl_daq.out", cwd=self.other)
        rc, out = self.stop("--this-copy")
        self.assertEqual(rc, 0, out)
        self.assertTrue(_wait_for(lambda: not _alive(mine)))
        self.assertTrue(_wait_for(lambda: not _alive(mine_py)))
        self.assertTrue(_alive(theirs))
        self.assertEqual(self.pkill_lines(), [])

    def test_this_copy_without_proc_filesystem(self):
        mine = self.spawn("rtl_daq.out", cwd=self.fw)
        self.pidfile(0, "rtl_daq", mine)
        rc, out = self.stop("--this-copy", HEIMDALL_PROC_ROOT=os.path.join(self.tmp, "noproc"))
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: no proc filesystem; only PID files of this copy were used", out)
        self.assertTrue(_wait_for(lambda: not _alive(mine)))

    def test_instance_keeps_other_copy_entry(self):
        pid = self.spawn("rtl_daq.out", cwd=self.fw)
        self.pidfile(3, "rtl_daq", pid)
        self.write_entry(3, self.other, [("rtl_daq", 999999)])
        rc, out = self.stop("3")
        self.assertEqual(rc, 0, out)
        self.assertIn("Instance 3 stopped", out)
        self.assertIn("WARN: registry entry for instance 3 belongs to %s; not removed (use --all or stop it from "
                      "that copy)" % self.other, out)
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "inst3.env")))
        self.assertTrue(_wait_for(lambda: not _alive(pid)))
        self.assertEqual(self.pkill_lines(), [])

    def test_instance_removes_own_entry_via_run_dir_hint(self):
        hinted = os.path.join(self.tmp, "hinted_run")
        pid = self.spawn("rtl_daq.out", cwd=self.fw)
        self.pidfile(3, "rtl_daq", pid)
        _write(os.path.join(self.fw, "_logs", "inst3", "pids", "run_dir"), hinted + "\n", 0o644)
        self.write_entry(3, self.fw, [("rtl_daq", pid)], run_dir=hinted)
        rc, out = self.stop("3")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(hinted, "inst3.env")))
        self.assertFalse(os.path.exists(os.path.join(hinted, "inst3.json")))

    def _control_files(self, n):
        p = "inst%d_" % n if n else ""
        dc = os.path.join(self.fw, "_data_control")
        removed = [p + "%s_%s" % (d, s) for s in ("decimator_in", "decimator_out", "delay_sync_iq",
                                                  "delay_sync_hwc") for d in ("fw", "bw")]
        removed += [p + x for x in ("orientation_state", "bias_tee_state", "hwc_state", "recal_request")]
        kept = [p + x for x in ("iq_track_lock", "fir_coeffs.txt", "fence_term")]
        for name in removed + kept:
            _write(os.path.join(dc, name), "1", 0o644)
        shm = []
        if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
            for s in ("decimator_in", "decimator_out", "delay_sync_iq", "delay_sync_hwc"):
                for ab in ("A", "B"):
                    path = "/dev/shm/%s%s_%s" % (p, s, ab)
                    if not os.path.exists(path):
                        _write(path, "x", 0o644)
                        shm.append(path)
            self.addCleanup(lambda: [os.path.exists(x) and os.remove(x) for x in shm])
        return dc, removed, kept, shm

    def test_clean_removes_instance_files_keeps_fence(self):
        n = 87
        pid = self.spawn("decimate.out", cwd=self.fw)
        self.pidfile(n, "decimate", pid)
        dc, removed, kept, shm = self._control_files(n)
        rc, out = self.stop(str(n), "--clean")
        self.assertEqual(rc, 0, out)
        for name in removed:
            self.assertFalse(os.path.exists(os.path.join(dc, name)), name)
        for name in kept:
            self.assertTrue(os.path.exists(os.path.join(dc, name)), name)
        for path in shm:
            self.assertFalse(os.path.exists(path), path)

    def test_clean_and_reset_fence_refused_while_alive(self):
        n = 88
        pid = self.spawn("decimate.out", cwd=self.fw)
        dc, removed, kept, _ = self._control_files(n)
        self.write_entry(n, self.fw, [("decimate", pid)])
        pidfile = os.path.join(self.fw, "_logs", "inst%d" % n, "pids", "decimate.pid")
        for flags in (["--clean"], ["--reset-fence"], ["--clean", "--reset-fence"]):
            with self.subTest(flags=flags):
                self.pidfile(n, "decimate", pid)
                # A retry is refused as well: the survivor keeps its PID file
                for attempt in (1, 2):
                    rc, out = self.stop(str(n), *flags, FAKE_SUDO_NOKILL=1)
                    self.assertEqual(rc, 3, "attempt %d: %s" % (attempt, out))
                    self.assertIn("still has running processes", out)
                    self.assertTrue(os.path.exists(pidfile), "attempt %d" % attempt)
                    for name in removed + kept:
                        self.assertTrue(os.path.exists(os.path.join(dc, name)), name)
                    self.assertTrue(os.path.exists(os.path.join(self.run_dir, "inst%d.env" % n)))
        self.assertTrue(_alive(pid))

    def test_reset_fence_alone_never_stops_a_live_instance(self):
        n = 6
        pid = self.spawn("decimate.out", cwd=self.fw)
        self.pidfile(n, "decimate", pid)
        dc, removed, kept, _ = self._control_files(n)
        rc, out = self.stop(str(n), "--reset-fence")
        self.assertEqual(rc, 3, out)
        self.assertIn("ERROR: instance 6 still has running processes; --clean/--reset-fence refused", out)
        self.assertNotIn("Shutting down", out)
        self.assertTrue(_alive(pid))
        self.assertTrue(os.path.exists(os.path.join(dc, "inst6_fence_term")))
        self.assertTrue(os.path.exists(os.path.join(self.fw, "_logs", "inst6", "pids", "decimate.pid")))
        self.assertNotIn("kill", self.log("sudo.log"))

    @unittest.skipIf(os.geteuid() == 0, "needs a non-root caller (kill -0 on a root process is EPERM)")
    def test_unsignallable_root_stage_counts_as_survivor(self):
        # pid 1 runs as root: kill -0 fails with EPERM for this user, ps -p
        # still sees it. Its PID file names its real command.
        args = subprocess.run(["ps", "-o", "args=", "-p", "1"], capture_output=True, text=True).stdout.split()
        if not args:
            self.skipTest("pid 1 not visible")
        role = os.path.basename(args[0])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", role):
            self.skipTest("pid 1 command %r unusable as a role" % role)
        n = 9
        dc, removed, kept, _ = self._control_files(n)
        self.pidfile(n, role, 1)
        rc, out = self.stop(str(n), "--clean")
        self.assertEqual(rc, 3, out)
        for name in removed + kept:
            self.assertTrue(os.path.exists(os.path.join(dc, name)), name)

    def test_reset_fence(self):
        dead = self.spawn("decimate.out", cwd=self.fw)
        os.kill(dead, signal.SIGKILL)
        self.assertTrue(_wait_for(lambda: not _alive(dead)))
        self.pidfile(5, "decimate", dead)
        dc, removed, kept, _ = self._control_files(5)
        rc, out = self.stop("5", "--reset-fence")
        self.assertEqual(rc, 0, out)
        self.assertIn("fencing state of instance 5 reset; the next coordinator XACK sets it again", out)
        self.assertFalse(os.path.exists(os.path.join(dc, "inst5_fence_term")))
        self.assertTrue(os.path.exists(os.path.join(dc, "inst5_iq_track_lock")))
        self.assertTrue(os.path.exists(os.path.join(dc, "inst5_fw_decimator_in")))

    def test_usage_conflicts_exit_2(self):
        for args in (["--reset-fence"], ["--this-copy", "3"], ["--clean"], ["--all", "2"], ["x", "--clean"]):
            with self.subTest(args=args):
                rc, out = self.stop(*args)
                self.assertEqual(rc, 2, out)
        self.assertEqual(self.pkill_lines(), [])

    def test_all_adds_python_kill_and_removes_every_entry(self):
        live = self.spawn("rtl_daq.out", cwd=self.other)
        self.write_entry(1, self.other, [("rtl_daq", live)])
        self.write_entry(0, self.fw, [("rtl_daq", 999999)])
        _write(os.path.join(self.fw, "_data_control", "fence_term"), "7", 0o644)
        rc, out = self.stop("--all")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.pkill_lines(), TODAY_TERM + TODAY_KILL + ALL_EXTRA_KILL)
        self.assertEqual([f for f in os.listdir(self.run_dir) if f.startswith("inst")], [])
        self.assertTrue(os.path.exists(os.path.join(self.fw, "_data_control", "fence_term")))

    def test_federation_without_instance_runs_global_sweep_then_federation_stop(self):
        rc, out = self.stop("--federation")
        calls = self.log("calls.log").splitlines()
        self.assertEqual(calls[-1], "federation_stop all")
        self.assertEqual([c[6:] for c in calls[:-1]], TODAY_TERM + TODAY_KILL)

    def test_federation_with_instance(self):
        rc, out = self.stop("4", "--federation")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log("calls.log").splitlines(), ["federation_stop all"])

    def test_old_copy_without_common_library(self):
        os.remove(os.path.join(self.fw, "daq_common.sh"))
        self.write_entry(1, self.other, [("rtl_daq", 999999)])
        rc, out = self.stop()
        self.assertEqual(self.pkill_lines(), TODAY_TERM + TODAY_KILL)
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "inst1.env")))


# ==========================================================================
# daq_supervise.sh
# ==========================================================================

class FakeStatusServer(threading.Thread):
    """:5002-style server: one command per connection, JSON + newline."""

    def __init__(self, doc):
        super().__init__(daemon=True)
        self.doc = doc
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.queries = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                try:
                    if conn.recv(1024).strip().upper() == b"STATUS":
                        self.queries += 1
                        conn.sendall(json.dumps(self.doc).encode() + b"\n")
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        self.sock.close()


class TestSupervisor(_Sandbox):

    def setUp(self):
        super().setUp()
        self.stub_daq_stop()
        for script in ("daq_start_sm.sh", "daq_synthetic_start.sh"):
            _write(os.path.join(self.fw, script), START_STUB)

    def supervise(self, *args, **env):
        env.setdefault("HEIMDALL_RUN_DIR", "/nonexistent/heimdall-run")
        return self.popen_script(["./daq_supervise.sh"] + list(args), env=self.env(**env))

    def wait_out(self, p, text, timeout=20):
        self.assertTrue(_wait_for(lambda: text in _read(p.out_path), timeout=timeout),
                        "%r not in output:\n%s" % (text, _read(p.out_path)))

    def pid_of(self, role, n=0):
        return int(_read(os.path.join(self.fw, "_logs", "inst%d" % n, "pids", role + ".pid")).strip())

    def test_dead_stage_exit_codes(self):
        for role, code in (("decimate", 12), ("rtl_daq", 10), ("iq_server", 15), ("hw_controller", 14)):
            with self.subTest(role=role):
                open(os.path.join(self.fake, "daq_stop.log"), "w").close()
                p = self.supervise("--interval", "1")
                self.wait_out(p, "supervising instance 0")
                pid = self.pid_of(role)
                os.kill(pid, signal.SIGKILL)
                self.assertEqual(p.wait(timeout=30), code, _read(p.out_path))
                self.assertIn("stage %s (pid %d) died" % (role, pid), _read(p.out_path))
                self.assertEqual(self.log("daq_stop.log").strip(), "%s|0" % self.fw)
                for r in ("rtl_daq", "rebuffer", "decimate", "delay_sync", "hw_controller", "iq_server"):
                    try:
                        os.kill(self.pid_of(r), signal.SIGKILL)
                    except OSError:
                        pass

    def _kill_pair(self, upstream, downstream):
        os.kill(self.pid_of(upstream), signal.SIGKILL)
        time.sleep(0.05)
        os.kill(self.pid_of(downstream), signal.SIGKILL)

    def test_upstream_death_reported_before_downstream(self):
        p = self.supervise("--interval", "1")
        self.wait_out(p, "supervising instance 0")
        up = self.pid_of("rtl_daq")
        self._kill_pair("rtl_daq", "rebuffer")
        self.assertEqual(p.wait(timeout=30), 10, _read(p.out_path))
        self.assertIn("stage rtl_daq (pid %d) died" % up, _read(p.out_path))
        self.assertNotIn("stage rebuffer", _read(p.out_path))

    def test_synthesizer_death_reported_before_rebuffer(self):
        p = self.supervise("--synthetic", "--interval", "1", FAKE_ROLES="synthesizer rebuffer decimate")
        self.wait_out(p, "supervising instance 0")
        self.assertIn("synthesizer=%d rebuffer=" % self.pid_of("synthesizer"), _read(p.out_path))
        self._kill_pair("synthesizer", "rebuffer")
        self.assertEqual(p.wait(timeout=30), 10, _read(p.out_path))

    def test_unknown_pid_file_ignored(self):
        os.makedirs(os.path.join(self.fw, "_logs", "inst0", "pids"), exist_ok=True)
        p = self.supervise("--interval", "1", FAKE_EXTRA_PID="other")
        self.wait_out(p, "ignoring unknown PID file _logs/inst0/pids/other.pid")
        os.kill(p.pid, signal.SIGTERM)
        self.assertEqual(p.wait(timeout=30), 0)

    def test_synthetic_role_and_start_args(self):
        p = self.supervise("--synthetic", "--interval", "1", "--", "--blocks", "0",
                           FAKE_ROLES="synthesizer rebuffer decimate")
        self.wait_out(p, "supervising instance 0")
        self.assertIn("daq_synthetic_start.sh --blocks 0", self.log("start.log"))
        os.kill(self.pid_of("synthesizer"), signal.SIGKILL)
        self.assertEqual(p.wait(timeout=30), 10)

    def test_exports_scoped_stop_and_launcher(self):
        p = self.supervise("--interval", "1")
        self.wait_out(p, "supervising instance 0")
        env = self.log("start_env").splitlines()
        self.assertIn("HEIMDALL_SCOPED_STOP=1", env)
        self.assertIn("HEIMDALL_LAUNCHER=supervised", env)
        self.assertFalse([e for e in env if e.startswith("HEIMDALL_UNIT=")])
        os.kill(p.pid, signal.SIGTERM)
        self.assertEqual(p.wait(timeout=30), 0)
        self.assertEqual(self.log("daq_stop.log").strip(), "%s|0" % self.fw)

    def test_systemd_launcher(self):
        p = self.supervise("--interval", "1", INVOCATION_ID="abc",
                           UNIT_NAME="heimdall-daq-supervised.service")
        self.wait_out(p, "supervising instance 0")
        env = self.log("start_env").splitlines()
        self.assertIn("HEIMDALL_LAUNCHER=systemd-supervised", env)
        self.assertIn("HEIMDALL_UNIT=heimdall-daq-supervised.service", env)

    def test_instance_mismatch_exit_2(self):
        p = self.supervise("--instance", "3")
        self.assertEqual(p.wait(timeout=30), 2)
        self.assertIn("does not match [federation] instance_id = 0", _read(p.out_path))
        self.assertEqual(self.log("start.log"), "")

    def test_start_failure_propagates(self):
        for rc in (1, 3, 4):
            with self.subTest(rc=rc):
                p = self.supervise(FAKE_START_RC=rc)
                self.assertEqual(p.wait(timeout=30), rc)

    def test_legacy_guard_with_other_live_instance(self):
        other = self.spawn("rtl_daq.out", cwd=self.fw)
        _write(os.path.join(self.fw, "_logs", "inst1", "pids", "rtl_daq.pid"), "%d\n" % other, 0o644)
        p = self.supervise()
        self.assertEqual(p.wait(timeout=30), 2)
        self.assertIn("instance 0 runs with [host] instance_guard = legacy on a multi-instance host; provision it "
                      "with util/provision_instance.py --instance 0 --in-place", _read(p.out_path))
        self.assertEqual(self.log("start.log"), "")
        p = self.supervise("--allow-legacy-guard", "--interval", "1")
        self.wait_out(p, "supervising instance 0")
        self.assertIn("WARN: instance 0 runs with [host] instance_guard = legacy", _read(p.out_path))

    def test_legacy_guard_with_live_registry_entry(self):
        other = self.spawn("rtl_daq.out", cwd=self.other)
        self.write_entry(2, self.other, [("rtl_daq", other)])
        p = self.supervise(HEIMDALL_RUN_DIR=self.run_dir)
        self.assertEqual(p.wait(timeout=30), 2)

    def test_refuse_guard_skips_legacy_check(self):
        self.write_ini(extra="[host]\ninstance_registry = 1\ninstance_guard = refuse\n")
        other = self.spawn("rtl_daq.out", cwd=self.fw)
        _write(os.path.join(self.fw, "_logs", "inst1", "pids", "rtl_daq.pid"), "%d\n" % other, 0o644)
        p = self.supervise("--interval", "1")
        self.wait_out(p, "supervising instance 0")

    def test_healthcheck_auto_skip_and_on_refused(self):
        p = self.supervise("--healthcheck", "auto", "--interval", "1")
        self.wait_out(p, "healthcheck skipped: status server disabled")
        os.kill(p.pid, signal.SIGTERM)
        p.wait(timeout=30)
        p = self.supervise("--healthcheck", "on")
        self.assertEqual(p.wait(timeout=30), 2)
        self.assertIn("healthcheck requires [monitoring] en_monitoring=1 and en_status_server=1", _read(p.out_path))

    def test_stall_auto_value_and_frozen_frame_count(self):
        srv = FakeStatusServer({"frame_count": 42, "sync_state": 6})
        srv.start()
        self.addCleanup(srv.stop)
        self.write_ini(en_mon=1, en_status=1, status_port=srv.port)
        p = self.supervise("--healthcheck", "auto", "--interval", "1")
        self.wait_out(p, "stall threshold 161 s (auto")
        os.kill(p.pid, signal.SIGTERM)
        p.wait(timeout=30)
        p = self.supervise("--healthcheck", "on", "--interval", "1", "--grace", "0", "--stall-sec", "2")
        self.assertEqual(p.wait(timeout=60), 16, _read(p.out_path))
        out = _read(p.out_path)
        self.assertIn("frame_count 42 unchanged", out)
        self.assertIn("healthcheck failure 3/3", out)
        self.assertEqual(self.log("daq_stop.log").strip().splitlines()[-1], "%s|0" % self.fw)

    def test_moving_frame_count_is_healthy(self):
        srv = FakeStatusServer({"frame_count": 1})
        srv.start()
        self.addCleanup(srv.stop)

        def bump():
            while not srv._stop.is_set():
                srv.doc = {"frame_count": srv.doc["frame_count"] + 1}
                time.sleep(0.3)
        threading.Thread(target=bump, daemon=True).start()
        self.write_ini(en_mon=1, en_status=1, status_port=srv.port)
        p = self.supervise("--healthcheck", "on", "--interval", "1", "--grace", "0", "--stall-sec", "2")
        self.assertTrue(_wait_for(lambda: srv.queries >= 6, timeout=30))
        self.assertIsNone(p.poll(), _read(p.out_path))
        self.assertNotIn("healthcheck failure", _read(p.out_path))

    def test_unreachable_and_stale(self):
        srv = FakeStatusServer({"frame_count": 5, "snapshot_age_sec": 99.0, "pipeline_health": "stale"})
        srv.start()
        self.addCleanup(srv.stop)
        self.write_ini(en_mon=1, en_status=1, status_port=srv.port)
        p = self.supervise("--healthcheck", "on", "--interval", "1", "--grace", "0")
        self.assertEqual(p.wait(timeout=60), 16)
        self.assertIn("pipeline_health is stale", _read(p.out_path))
        srv.stop()
        p = self.supervise("--healthcheck", "on", "--interval", "1", "--grace", "0")
        self.assertEqual(p.wait(timeout=60), 16)
        self.assertIn("unreachable", _read(p.out_path))

    def test_grace_tolerates_failures(self):
        self.write_ini(en_mon=1, en_status=1, status_port=1)
        p = self.supervise("--healthcheck", "on", "--interval", "1", "--grace", "60")
        self.wait_out(p, "supervising instance 0")
        time.sleep(4)
        self.assertIsNone(p.poll(), _read(p.out_path))


# ==========================================================================
# federation_start.sh / federation_stop.sh
# ==========================================================================

class TestFederationLaunchers(_Sandbox):

    def fed(self, *args, **env):
        return self.run_script(["./federation_start.sh"] + list(args), env=self.env(**env), timeout=60)

    def argv(self, name):
        return json.loads(_read(os.path.join(self.fake, name + ".argv.json")))

    def test_start_flags_pid_log_and_refuse_live(self):
        L, P, S = (os.path.join(self.tmp, x) for x in ("logs", "pids", "state"))
        rc, out = self.fed("coordinator", "--config", "fed.ini", "--log-dir", L, "--pid-dir", P, "--state-dir", S)
        self.assertEqual(rc, 0, out)
        rec = self.argv("federation_coordinator")
        self.assertEqual(rec["argv"], ["--config", "fed.ini", "--log-file", L + "/coordinator.log",
                                       "--pid-file", P + "/coordinator.pid", "--state-dir", S])
        self.assertEqual(int(_read(os.path.join(P, "coordinator.pid"))), rec["pid"])
        self.assertTrue(os.path.exists(os.path.join(L, "coordinator.log")))
        self.assertTrue(_alive(rec["pid"]))
        rc, out = self.fed("coordinator", "--log-dir", L, "--pid-dir", P)
        self.assertEqual(rc, 3, out)
        self.assertIn("already running", out)
        rc, out = self.run_script(["./federation_stop.sh", "coordinator", "--pid-dir", P])
        self.assertEqual(rc, 0, out)
        self.assertTrue(_wait_for(lambda: not _alive(rec["pid"])))
        self.assertFalse(os.path.exists(os.path.join(P, "coordinator.pid")))

    def test_env_defaults_and_flags_override(self):
        L, P, S = (os.path.join(self.tmp, x) for x in ("elogs", "epids", "estate"))
        rc, out = self.fed("all", HEIMDALL_FED_LOG_DIR=L, HEIMDALL_FED_PID_DIR=P, HEIMDALL_FED_STATE_DIR=S)
        self.assertEqual(rc, 0, out)
        coord = self.argv("federation_coordinator")["argv"]
        router = self.argv("federation_iq_router")["argv"]
        self.assertIn(S, coord)
        self.assertNotIn("--state-dir", router)
        self.assertEqual(router[router.index("--log-file") + 1], L + "/router.log")
        self.assertEqual(router[router.index("--pid-file") + 1], P + "/router.pid")
        if not os.path.exists("/etc/heimdall/federation.ini"):
            self.assertEqual(coord[coord.index("--config") + 1], "./daq_chain_config.ini")
        rc, out = self.run_script(["./federation_stop.sh", "all"], env=self.env(HEIMDALL_FED_PID_DIR=P))
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.listdir(P), [])
        L2 = os.path.join(self.tmp, "flaglogs")
        rc, out = self.fed("router", "--log-dir", L2, HEIMDALL_FED_LOG_DIR=L, HEIMDALL_FED_PID_DIR=P)
        self.assertEqual(rc, 0, out)
        router = self.argv("federation_iq_router")["argv"]
        self.assertEqual(router[router.index("--log-file") + 1], L2 + "/router.log")
        self.run_script(["./federation_stop.sh", "router", "--pid-dir", P])

    def test_default_dirs_relative_to_cwd(self):
        rc, out = self.fed("router")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.fw, "_logs", "federation", "router.log")))
        self.assertTrue(os.path.exists(os.path.join(self.fw, "_logs", "federation", "pids", "router.pid")))
        rc, out = self.run_script(["./federation_stop.sh", "router"])
        self.assertIn("stopped", out)

    def test_immediate_exit_is_1(self):
        P = os.path.join(self.tmp, "pids")
        rc, out = self.fed("coordinator", "--pid-dir", P, FAKE_FED_EXIT=1)
        self.assertEqual(rc, 1, out)
        self.assertIn("exited immediately", out)
        self.assertFalse(os.path.exists(os.path.join(P, "coordinator.pid")))

    def test_foreground_execs(self):
        p = self.popen_script(["./federation_start.sh", "coordinator", "--foreground", "--pid-dir",
                               os.path.join(self.tmp, "p")])
        self.assertTrue(_wait_for(lambda: os.path.exists(os.path.join(self.fake, "federation_coordinator.argv.json"))))
        self.assertEqual(self.argv("federation_coordinator")["pid"], p.pid)
        p.terminate()
        p.wait(timeout=30)

    def test_usage_errors(self):
        for args in ([], ["bogus"], ["all", "--foreground"], ["coordinator", "--log-dir"], ["router", "router"]):
            with self.subTest(args=args):
                rc, out = self.fed(*args)
                self.assertEqual(rc, 2, out)
        rc, out = self.run_script(["./federation_stop.sh"])
        self.assertEqual(rc, 2)

    def test_stop_stale_and_missing_pid_files(self):
        P = os.path.join(self.tmp, "pids")
        _write(os.path.join(P, "router.pid"), "999999\n", 0o644)
        rc, out = self.run_script(["./federation_stop.sh", "all", "--pid-dir", P])
        self.assertEqual(rc, 0, out)
        self.assertIn("stale PID file", out)
        self.assertIn("no PID file", out)
        self.assertFalse(os.path.exists(os.path.join(P, "router.pid")))


# ==========================================================================
# util/host_tuning.sh and util/irq_tuning.sh
# ==========================================================================

class TestHostTuningScript(_Sandbox):

    def setUp(self):
        super().setUp()
        self.util = os.path.join(self.tmp, "util")
        os.makedirs(self.util)
        shutil.copy2(os.path.join(UTIL, "host_tuning.sh"), self.util)
        shutil.copy2(os.path.join(UTIL, "irq_tuning.sh"), os.path.join(self.util, "irq_tuning.real"))
        _write(os.path.join(self.util, "irq_tuning.sh"), IRQ_STUB)
        self.hroot = os.path.join(self.tmp, "hostroot")
        files = {
            "proc/sys/kernel/sched_rt_runtime_us": "-1\n",
            "proc/sys/vm/drop_caches": "0\n",
            "sys/module/usbcore/parameters/usbfs_memory_mb": "0\n",
            "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor": "performance\n",
            "sys/devices/system/cpu/cpu1/cpufreq/scaling_governor": "performance\n",
            "sys/class/net/eth0/queues/rx-0/rps_cpus": "0\n",
            "proc/interrupts": ("           CPU0       CPU1\n"
                                " 40:     1000          0     GICv2  65 Level     xhci_hcd:usb1\n"
                                " 41:      500          0     GICv2  66 Level     xhci_hcd:usb3\n"
                                " 42:      100          0     GICv2 157 Level     eth0\n"
                                " 43:        1          0     GICv2  12 Level     timer\n"),
            "proc/irq/40/smp_affinity": "1\n",
            "proc/irq/41/smp_affinity": "00000001\n",
            "proc/irq/42/smp_affinity": "8\n",
            "proc/irq/43/smp_affinity": "f\n",
        }
        for rel, text in files.items():
            _write(os.path.join(self.hroot, rel), text, 0o644)

    def tune(self, *args, **env):
        env.setdefault("HEIMDALL_TUNING_ROOT", self.hroot)
        env.setdefault("FAKE_UNAME", "aarch64")
        return self.run_script([os.path.join(self.util, "host_tuning.sh")] + list(args), env=self.env(**env))

    def test_apply_today_commands_in_order(self):
        _write(os.path.join(self.hroot, "sys/module/usbcore/parameters/usbfs_memory_mb"), "16\n", 0o644)
        rc, out = self.tune("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log("calls.log").splitlines(),
                         ["sysctl -w kernel.sched_rt_runtime_us=-1", "irq_tuning "])
        self.assertEqual(_read(os.path.join(self.hroot, "sys/module/usbcore/parameters/usbfs_memory_mb")), "0\n")
        self.assertEqual(_read(os.path.join(self.hroot, "proc/sys/vm/drop_caches")), "3\n")

    def test_apply_forwards_flags_only_when_given(self):
        rc, out = self.tune("--usb-core", "2", "--controller", "xhci_hcd:usb3", "--eth-core", "3",
                            "--no-governor", "--no-drop-caches")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log("calls.log").splitlines()[1],
                         "irq_tuning --usb-core 2 --controller xhci_hcd:usb3 --eth-core 3 --no-governor")
        self.assertEqual(_read(os.path.join(self.hroot, "proc/sys/vm/drop_caches")), "0\n")

    def test_apply_without_usbfs_warns(self):
        os.remove(os.path.join(self.hroot, "sys/module/usbcore/parameters/usbfs_memory_mb"))
        rc, out = self.tune("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: /sys/module/usbcore/parameters/usbfs_memory_mb not present, skipping", out)

    def test_check_all_applied(self):
        rc, out = self.tune("--check")
        self.assertEqual(rc, 0, out)
        self.assertIn("OK   kernel.sched_rt_runtime_us = -1", out)
        self.assertIn("OK   USB IRQ 41 affinity = 00000001", out)
        self.assertIn("OK   Network IRQ 42 affinity = 8", out)
        self.assertNotIn("IRQ 43", out)

    def test_check_reports_unapplied(self):
        _write(os.path.join(self.hroot, "proc/sys/kernel/sched_rt_runtime_us"), "950000\n", 0o644)
        _write(os.path.join(self.hroot, "sys/devices/system/cpu/cpu1/cpufreq/scaling_governor"), "ondemand\n", 0o644)
        rc, out = self.tune("--check")
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL kernel.sched_rt_runtime_us = 950000 (want -1)", out)
        self.assertIn("FAIL cpu1 governor = ondemand (want performance)", out)
        rc, out = self.tune("--check", "--no-governor")
        self.assertNotIn("governor", out)

    def test_check_usb_core_and_controller(self):
        rc, out = self.tune("--check", "--usb-core", "2", "--controller", "usb3")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL USB IRQ 41 affinity = 00000001 (want 4)", out)
        self.assertNotIn("IRQ 40", out)

    def test_usage(self):
        rc, out = self.tune("--bogus")
        self.assertEqual(rc, 2)
        rc, out = self.tune("--usb-core", "x")
        self.assertEqual(rc, 2)

    def irq(self, *args, **env):
        env.setdefault("HEIMDALL_TUNING_ROOT", self.hroot)
        env.setdefault("FAKE_UNAME", "aarch64")
        return self.run_script(["bash", os.path.join(self.util, "irq_tuning.real")] + list(args),
                               env=self.env(**env))

    @unittest.skipIf(IS_ROOT, "the non-root path needs an unprivileged user")
    def test_irq_tuning_requires_root(self):
        rc, out = self.irq()
        self.assertEqual(rc, 1)
        self.assertIn("IRQ tuning requires root privileges. Run with sudo.", out)

    @unittest.skipUnless(IS_ROOT, "writes the fake /proc as root (container lane)")
    def test_irq_tuning_default_and_flags(self):
        for rel in ("proc/irq/40/smp_affinity", "proc/irq/41/smp_affinity", "proc/irq/42/smp_affinity"):
            _write(os.path.join(self.hroot, rel), "f\n", 0o644)
        _write(os.path.join(self.hroot, "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"), "ondemand\n", 0o644)
        _write(os.path.join(self.hroot, "sys/class/net/eth0/queues/rx-0/rps_cpus"), "f\n", 0o644)
        rc, out = self.irq()
        self.assertEqual(rc, 0, out)
        self.assertIn("USB IRQ 40 -> core mask 1", out)
        self.assertIn("Network IRQ 42 -> core mask 8", out)
        self.assertEqual(_read(os.path.join(self.hroot, "proc/irq/41/smp_affinity")), "1\n")
        self.assertEqual(_read(os.path.join(self.hroot, "proc/irq/43/smp_affinity")), "f\n")
        self.assertEqual(_read(os.path.join(self.hroot, "sys/class/net/eth0/queues/rx-0/rps_cpus")), "0\n")
        self.assertEqual(_read(os.path.join(self.hroot, "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")),
                         "performance\n")
        self.assertIn("IRQ affinity tuning completed", out)
        _write(os.path.join(self.hroot, "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"), "ondemand\n", 0o644)
        rc, out = self.irq("--usb-core", "2", "--controller", "usb3", "--eth-core", "35", "--no-governor")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_read(os.path.join(self.hroot, "proc/irq/41/smp_affinity")), "4\n")
        self.assertEqual(_read(os.path.join(self.hroot, "proc/irq/40/smp_affinity")), "1\n")
        self.assertEqual(_read(os.path.join(self.hroot, "proc/irq/42/smp_affinity")), "8,00000000\n")
        self.assertEqual(_read(os.path.join(self.hroot, "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")),
                         "ondemand\n")

    @unittest.skipUnless(IS_ROOT, "writes the fake /proc as root (container lane)")
    def test_irq_tuning_failure_handling_and_arch(self):
        bad = os.path.join(self.hroot, "proc/irq/40/smp_affinity")
        os.remove(bad)
        os.makedirs(bad)
        _write(os.path.join(self.hroot, "proc/irq/41/smp_affinity"), "f\n", 0o644)
        rc, out = self.irq()
        self.assertEqual(rc, 1, out)
        self.assertEqual(_read(os.path.join(self.hroot, "proc/irq/41/smp_affinity")), "f\n", "set -e did not abort")
        rc, out = self.irq("--keep-going")
        self.assertEqual(rc, 1, out)
        self.assertIn("completed with 1 failed write(s)", out)
        self.assertEqual(_read(os.path.join(self.hroot, "proc/irq/41/smp_affinity")), "1\n")
        os.rmdir(bad)
        rc, out = self.irq(FAKE_UNAME="armv7l")
        self.assertEqual(rc, 1)
        self.assertIn("Unsupported architecture: armv7l", out)
        rc, out = self.irq("--any-arch", FAKE_UNAME="armv7l", FAKE_NPROC=4)
        self.assertEqual(rc, 0, out)
        self.assertIn("Network IRQ 42 -> core mask 8", out)


if __name__ == "__main__":
    unittest.main()
