"""Line-based TCP client for daq_status_server.py (port 5002).

One command per connection, one JSON object terminated by "\\n" per reply.
Connection failures raise ``EndpointError`` (CLI exit 3)."""
import json
import socket

from .ctl import EndpointError, QUERY_TIMEOUT_SEC


class StatusClient:
    def __init__(self, host="127.0.0.1", port=5002, timeout=QUERY_TIMEOUT_SEC):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _query(self, command):
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.settimeout(self.timeout)
                s.sendall((command + "\n").encode())
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
        except socket.timeout:
            raise EndpointError(self.host, self.port,
                                "no reply within {:g} s".format(self.timeout), kind="timeout")
        except OSError as e:
            raise EndpointError(self.host, self.port, e.strerror or str(e), kind="connect")
        if not data.strip():
            raise EndpointError(self.host, self.port,
                                "connection closed without a reply", kind="reset")
        try:
            return json.loads(data.decode("utf-8", errors="replace"))
        except ValueError:
            raise EndpointError(self.host, self.port, "reply is not JSON", kind="bad_reply")

    def query(self, command):
        """Send an arbitrary status-server command line."""
        return self._query(command)

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

    # --- DB read proxies (served when [database] + [monitoring] are enabled) ---

    def db_stats(self):
        return self._query("DB_STATS")

    def scan_summary(self, freq_hz=None):
        return self._query("SCAN_SUMMARY" if freq_hz is None
                           else "SCAN_SUMMARY {:d}".format(int(freq_hz)))

    def cal_history(self, freq_hz=None, since_ms=None, limit=None):
        """Bare/one-argument form unless since_ms or limit is given; the 2/3
        argument form needs an extended server (status_protocol in STATUS)."""
        if since_ms is None and limit is None:
            return self._query("CAL_HISTORY" if freq_hz is None
                               else "CAL_HISTORY {:d}".format(int(freq_hz)))
        parts = ["CAL_HISTORY", "-" if freq_hz is None else str(int(freq_hz)),
                 str(int(since_ms or 0))]
        if limit is not None:
            parts.append(str(int(limit)))
        return self._query(" ".join(parts))

    # --- extended-only verbs ([monitoring] status_extended = 1) ---

    def peers(self):
        return self._query("PEERS")

    def hwc(self):
        return self._query("HWC")

    @staticmethod
    def is_extended(status_reply):
        """A server speaks the extended :5002 grammar iff STATUS carries
        the ``status_protocol`` key (G.1)."""
        return isinstance(status_reply, dict) and "status_protocol" in status_reply

    @staticmethod
    def is_unknown_command(reply):
        return isinstance(reply, dict) and reply.get("error") == "unknown command"
