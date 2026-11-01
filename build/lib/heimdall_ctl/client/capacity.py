"""Run the capacity pre-flight tool (``<firmware>/daq_preflight.py``, spec I.2).

Exit mapping (spec F.1/F.4): tool exit 0 -> 0; 1 (warnings) -> 0, or 1 with
``--strict``; 2 (failures) -> 5; tool missing -> 3; anything else -> 1.
"""
import os
import subprocess
import sys

TOOL_NAME = "daq_preflight.py"


def find_tool(firmware_dir):
    if not firmware_dir:
        return None
    path = os.path.join(firmware_dir, TOOL_NAME)
    return path if os.path.isfile(path) else None


def build_command(tool, configs=None, add_instances=None, host_json=None, json_out=False,
                  python=None):
    cmd = [python or sys.executable, tool]
    if configs:
        cmd += ["--config"] + [os.path.abspath(c) for c in configs]
    if add_instances is not None:
        cmd += ["--add-instances", str(int(add_instances))]
    if host_json:
        cmd += ["--host-json", host_json if host_json == "-" else os.path.abspath(host_json)]
    if json_out:
        cmd.append("--json")
    return cmd


def map_exit(tool_exit, strict):
    if tool_exit == 0:
        return 0
    if tool_exit == 1:
        return 1 if strict else 0
    if tool_exit == 2:
        return 5
    return 1


def run(cmd, firmware_dir):
    """Run the tool in its firmware directory; returns CompletedProcess."""
    return subprocess.run(cmd, cwd=firmware_dir, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, check=False)
