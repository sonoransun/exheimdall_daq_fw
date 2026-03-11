#!/usr/bin/env python3
"""
    Federation Coordinator — Unified Control Plane for Multi-Instance DAQ

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    Standalone process that provides a single control endpoint for the entire
    federation. Fans out commands (FREQ, GAIN, etc.) to all instance HW
    controllers and aggregates status from all StatusServers.
"""
import json
import socket
import logging
import threading
import time
import argparse
from configparser import ConfigParser


class FederationCoordinator:
    """Unified control plane for a federation of DAQ instances."""

    def __init__(self, port=6000, instances=None):
        """
        Parameters
        ----------
        port : int
            TCP port for the coordinator's control interface.
        instances : list of dict
            Each dict: {"host": str, "instance_id": int, "port_stride": int}
            Ports are computed as base_port + instance_id * port_stride.
        """
        self.logger = logging.getLogger(__name__)
        self.port = port
        self.instances = instances or []
        self._stop_event = threading.Event()
        self._server_thread = None

    def start(self):
        """Start the coordinator TCP server in a background thread."""
        self._server_thread = threading.Thread(target=self._serve, daemon=True,
                                                name="federation_coordinator")
        self._server_thread.start()
        self.logger.info("Federation coordinator started on port %d with %d instances",
                         self.port, len(self.instances))

    def _serve(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.settimeout(1.0)
        try:
            server_sock.bind(("", self.port))
            server_sock.listen(5)
        except socket.error as e:
            self.logger.error("Failed to bind coordinator on port %d: %s", self.port, e)
            return

        while not self._stop_event.is_set():
            try:
                client, addr = server_sock.accept()
            except socket.timeout:
                continue
            except socket.error:
                break
            threading.Thread(target=self._handle_client, args=(client,),
                            daemon=True).start()
        server_sock.close()

    def _handle_client(self, client_sock):
        try:
            client_sock.settimeout(5.0)
            data = client_sock.recv(4096).decode("utf-8", errors="replace").strip()
            if not data:
                return

            parts = data.split(None, 1)
            cmd = parts[0].upper()
            args = parts[1] if len(parts) > 1 else ""

            if cmd == "STATUS":
                result = self._aggregate_status()
                client_sock.sendall(json.dumps(result).encode("utf-8"))
            elif cmd == "FREQ":
                result = self._fan_out_command("FREQ", args)
                client_sock.sendall(json.dumps(result).encode("utf-8"))
            elif cmd == "GAIN":
                result = self._fan_out_command("GAIN", args)
                client_sock.sendall(json.dumps(result).encode("utf-8"))
            elif cmd == "INSTANCE":
                # INSTANCE <id> <subcmd>
                sub_parts = args.split(None, 1)
                if len(sub_parts) >= 2:
                    target_id = int(sub_parts[0])
                    subcmd = sub_parts[1]
                    result = self._send_to_instance(target_id, subcmd)
                else:
                    result = {"error": "Usage: INSTANCE <id> <command>"}
                client_sock.sendall(json.dumps(result).encode("utf-8"))
            elif cmd == "REBALANCE":
                result = {"status": "rebalance_requested", "instances": len(self.instances)}
                client_sock.sendall(json.dumps(result).encode("utf-8"))
            elif cmd == "PING":
                client_sock.sendall(json.dumps({"ok": True, "ts": time.time()}).encode("utf-8"))
            else:
                client_sock.sendall(json.dumps({"error": "Unknown command"}).encode("utf-8"))
        except Exception as e:
            self.logger.error("Error handling coordinator client: %s", e)
        finally:
            client_sock.close()

    def _compute_instance_ports(self, inst):
        """Compute ports for an instance dict."""
        iid = inst["instance_id"]
        stride = inst.get("port_stride", 100)
        return {
            "hwc_port": 5001 + iid * stride,
            "status_port": 5002 + iid * stride,
            "iq_port": 5000 + iid * stride,
        }

    def _fan_out_command(self, cmd, args, targets=None):
        """Send a command to all (or specified) instances' HW controllers."""
        results = {}
        target_instances = targets if targets is not None else self.instances
        for inst in target_instances:
            host = inst["host"]
            ports = self._compute_instance_ports(inst)
            hwc_port = ports["hwc_port"]
            try:
                result = self._send_hwc_command(host, hwc_port, "{} {}".format(cmd, args))
                results[str(inst["instance_id"])] = result
            except Exception as e:
                results[str(inst["instance_id"])] = {"error": str(e)}
        return {"command": cmd, "results": results}

    def _send_hwc_command(self, host, port, cmd_str):
        """Send a command to a single HW controller instance."""
        with socket.create_connection((host, port), timeout=5.0) as sock:
            # HW controller expects 128-byte frames
            frame = cmd_str.encode("utf-8").ljust(128, b"\x00")
            sock.sendall(frame)
            response = sock.recv(128)
            return {"response": response.decode("utf-8", errors="replace").strip("\x00")}

    def _send_to_instance(self, instance_id, cmd_str):
        """Send a command to a specific instance by ID."""
        for inst in self.instances:
            if inst["instance_id"] == instance_id:
                host = inst["host"]
                ports = self._compute_instance_ports(inst)
                return self._send_hwc_command(host, ports["hwc_port"], cmd_str)
        return {"error": "Instance {} not found".format(instance_id)}

    def _aggregate_status(self):
        """Query each instance's StatusServer and merge results."""
        statuses = {}
        for inst in self.instances:
            host = inst["host"]
            ports = self._compute_instance_ports(inst)
            status_port = ports["status_port"]
            try:
                with socket.create_connection((host, status_port), timeout=3.0) as sock:
                    sock.sendall(b"STATUS\n")
                    data = b""
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                status = json.loads(data.decode("utf-8", errors="replace"))
                statuses[str(inst["instance_id"])] = status
            except Exception as e:
                statuses[str(inst["instance_id"])] = {"error": str(e)}

        # Derive overall health
        healths = [s.get("pipeline_health", "error") for s in statuses.values()
                   if isinstance(s, dict) and "error" not in s]
        if all(h == "ok" for h in healths) and healths:
            overall = "ok"
        elif any(h == "error" for h in healths):
            overall = "error"
        elif healths:
            overall = "degraded"
        else:
            overall = "unknown"

        return {
            "federation_health": overall,
            "instance_count": len(self.instances),
            "healthy_count": sum(1 for h in healths if h in ("ok", "degraded")),
            "instances": statuses,
            "timestamp": time.time(),
        }

    def close(self):
        """Stop the coordinator."""
        self._stop_event.set()
        if self._server_thread is not None:
            self._server_thread.join(timeout=3)
        self.logger.info("Federation coordinator stopped")


def _parse_instances(instance_str, default_stride=100):
    """Parse instance list string like 'localhost:0,192.168.1.10:1'."""
    instances = []
    for entry in instance_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host, iid_str = entry.rsplit(":", 1)
        instances.append({
            "host": host,
            "instance_id": int(iid_str),
            "port_stride": default_stride,
        })
    return instances


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="HeIMDALL DAQ Federation Coordinator")
    parser.add_argument("--port", type=int, default=6000, help="Coordinator port")
    parser.add_argument("--instances", type=str, default="",
                        help="Comma-separated list of host:instance_id pairs")
    parser.add_argument("--config", type=str, default="",
                        help="Path to federation config INI file")
    args = parser.parse_args()

    instances = []
    port = args.port

    if args.config:
        cfg = ConfigParser()
        cfg.read(args.config)
        if cfg.has_section("federation"):
            port = cfg.getint("federation", "coordinator_port", fallback=6000)
            stride = cfg.getint("federation", "port_stride", fallback=100)
            inst_str = cfg.get("federation", "peer_list", fallback="")
            instances = _parse_instances(inst_str, stride)
    elif args.instances:
        instances = _parse_instances(args.instances)

    coordinator = FederationCoordinator(port=port, instances=instances)
    coordinator.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        coordinator.close()
