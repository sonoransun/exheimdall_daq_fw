"""TCP control client for hw_controller.py (port 5001).

Builds 128-byte frames: 4-byte verb + 124-byte payload.
"""
import json
import socket
import struct


class CtlClient:
    FRAME_SIZE = 128
    VERB_SIZE = 4
    PAYLOAD_SIZE = 124

    def __init__(self, host="127.0.0.1", port=5001, timeout=5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _send(self, verb, payload=b""):
        verb_bytes = verb.encode("ascii")[:self.VERB_SIZE].ljust(self.VERB_SIZE, b"\x00")
        payload = payload[:self.PAYLOAD_SIZE].ljust(self.PAYLOAD_SIZE, b"\x00")
        frame = verb_bytes + payload
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            s.sendall(frame)
            reply = s.recv(self.FRAME_SIZE)
        return reply

    def freq(self, hz):
        payload = struct.pack("<Q", int(hz))
        return self._send("FREQ", payload)

    def gain(self, gains):
        payload = struct.pack(f"<{len(gains)}I", *[int(g) for g in gains])
        return self._send("GAIN", payload)

    def gain_unified(self, value, num_channels):
        return self.gain([value] * num_channels)

    def agc(self):
        return self._send("AGC")

    def init(self):
        return self._send("INIT")

    def recal(self):
        try:
            return self._send("RECL")
        except Exception:
            return self._send("INIT")

    def schedule_load(self, schedule_dict):
        payload = json.dumps(schedule_dict).encode()
        return self._send("SCHD", payload)

    def schedule_stop(self):
        return self._send("SCHS")

    def schedule_query(self):
        return self._send("SCHQ")

    def schedule_next(self):
        return self._send("SCHN")
