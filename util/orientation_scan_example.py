#!/usr/bin/env python3
"""
    Example client: amplified receivers + antenna orientation over the DAQ
    control interface (TCP :5001) and status server (TCP :5002).

    Demonstrates the control commands added for the RF front-end / orientation
    expansion. Run it against a live DAQ chain started with a config that has
    [amplification] and [orientation] enabled (see config_files/directional_df).

        python3 util/orientation_scan_example.py --host localhost

    No package layout in this repo; imports are direct (sys.path.insert).

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import argparse
import json
import socket
import struct
import sys
import time


def _send_cmd(host, port, cmd4, payload=b""):
    """Send a 128-byte control frame (4-byte command + 124-byte payload)."""
    frame = cmd4.encode("ascii")[:4].ljust(4, b" ") + payload
    frame = frame.ljust(128, b"\x00")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(3.0)
        s.connect((host, port))
        s.send(frame)
        try:
            return s.recv(128)
        except socket.timeout:
            return b""


def set_external_gain(host, port, gains_db, num_ch):
    """EGAN: set per-channel external LNA gain (dB), sent as tenths of dB."""
    tenths = [int(round(g * 10)) for g in gains_db][:num_ch]
    payload = struct.pack("I" * len(tenths), *tenths)
    _send_cmd(host, port, "EGAN", payload)
    print("Set external LNA gains: {} dB".format(gains_db))


def set_bias_tee(host, port, states, num_ch):
    """BIAS: runtime inline-LNA bias-tee toggle (requires en_bias_tee_runtime=1)."""
    states = [1 if s else 0 for s in states][:num_ch]
    payload = struct.pack("I" * len(states), *states)
    _send_cmd(host, port, "BIAS", payload)
    print("Set bias-tee states: {}".format(states))


def set_bearing(host, port, az_deg, el_deg):
    """ORNT: slew the antenna to an absolute azimuth/elevation."""
    payload = struct.pack("ff", float(az_deg), float(el_deg))
    _send_cmd(host, port, "ORNT", payload)
    print("Commanded bearing: az={:.1f} el={:.1f}".format(az_deg, el_deg))


def query_status(host, status_port):
    """Query the JSON status server for the aggregate channel power / sync."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3.0)
            s.connect((host, status_port))
            s.send(b"STATUS")
            data = s.recv(4096)
        return json.loads(data.decode(errors="ignore"))
    except (OSError, ValueError):
        return {}


def external_scan_and_peak(host, ctrl_port, status_port, az_start=0, az_stop=350, az_step=10):
    """Client-side scan: step the antenna in azimuth, read aggregate power from
    the status server at each step, and settle on the peak. This is the
    least-invasive alternative to the firmware's autonomous SCAN command."""
    best_az, best_power = az_start, None
    az = az_start
    while az <= az_stop:
        set_bearing(host, ctrl_port, az, 0.0)
        time.sleep(1.0)  # allow the rotator to slew + settle
        status = query_status(host, status_port)
        power = status.get("aggregate_power_db")
        print("  az={:5.1f}  power={}".format(az, power))
        if power is not None and (best_power is None or power > best_power):
            best_power, best_az = power, az
        az += az_step
    print("Peak at az={:.1f} (power={})".format(best_az, best_power))
    set_bearing(host, ctrl_port, best_az, 0.0)


def main():
    ap = argparse.ArgumentParser(description="Amplified-receiver + orientation control example")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--ctrl-port", type=int, default=5001)
    ap.add_argument("--status-port", type=int, default=5002)
    ap.add_argument("--num-ch", type=int, default=5)
    ap.add_argument("--mode", choices=["demo", "scan", "autoscan"], default="demo")
    args = ap.parse_args()

    if args.mode == "demo":
        set_external_gain(args.host, args.ctrl_port, [20, 20, 20, 20, 20], args.num_ch)
        set_bias_tee(args.host, args.ctrl_port, [1, 1, 1, 1, 1], args.num_ch)
        set_bearing(args.host, args.ctrl_port, 137.5, 12.0)
    elif args.mode == "scan":
        external_scan_and_peak(args.host, args.ctrl_port, args.status_port)
    elif args.mode == "autoscan":
        # Trigger the firmware's autonomous scan-and-peak
        _send_cmd(args.host, args.ctrl_port, "SCAN")
        print("Autonomous scan-and-peak started (watch the event bus / status server)")


if __name__ == "__main__":
    sys.exit(main())
