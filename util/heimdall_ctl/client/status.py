"""Line-based TCP client for daq_status_server.py (port 5002)."""
import json
import socket


class StatusClient:
    def __init__(self, host="127.0.0.1", port=5002, timeout=5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _query(self, command):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            s.sendall((command + "\n").encode())
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
        return json.loads(data.decode())

    def ping(self):
        return self._query("PING")

    def status(self):
        return self._query("STATUS")

    def metrics(self):
        return self._query("METRICS")

    def events(self):
        return self._query("EVENTS")

    def events_dropped(self):
        return self._query("EVENTS_DROPPED")
