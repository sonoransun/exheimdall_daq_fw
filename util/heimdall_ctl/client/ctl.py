"""TCP control client for hw_controller.py (port 5001).

Builds 128-byte frames: 4-byte verb + 124-byte payload (little-endian
binary packing). Verbs shorter than 4 characters are space-padded to match
the server's decoder ("AGC " / "RFQ ").

Replies are 128 bytes starting with 'FNSD'. Query verbs (RFQ, OQRY, SCHQ,
...) may carry a NUL-padded UTF-8 JSON document in bytes 4-127; plain acks
carry all-zero payloads.
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
        # Space-pad short verbs: the server compares 4-char strings like "AGC "
        verb_bytes = verb.encode("ascii")[:self.VERB_SIZE].ljust(self.VERB_SIZE, b" ")
        payload = payload[:self.PAYLOAD_SIZE].ljust(self.PAYLOAD_SIZE, b"\x00")
        frame = verb_bytes + payload
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            s.sendall(frame)
            reply = b""
            while len(reply) < self.FRAME_SIZE:
                chunk = s.recv(self.FRAME_SIZE - len(reply))
                if not chunk:
                    break
                reply += chunk
        return reply

    @staticmethod
    def parse_reply(reply):
        """Parse a 128-byte control reply into a dict.

        Returns {"ok": bool, ...}. For query verbs the server may embed a
        NUL-padded UTF-8 JSON document in bytes 4-127 -> {"ok": True,
        "data": <parsed>}. An all-zeros payload (plain ack / no data) yields
        {"ok": True, "data": None}. Unparseable payloads are surfaced raw.
        """
        if not reply or len(reply) < 4 or reply[:4] != b"FNSD":
            return {"ok": False, "error": "bad reply",
                    "raw": reply.hex() if reply else ""}
        payload = reply[4:].rstrip(b"\x00")
        if not payload:
            return {"ok": True, "data": None}
        text = payload.decode("utf-8", errors="replace")
        try:
            return {"ok": True, "data": json.loads(text)}
        except ValueError:
            return {"ok": True, "data": None, "raw": text}

    # --- Tuner / calibration ---

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
        # Recalibration is triggered by re-running the INIT sequence; there
        # is no separate server-side verb for it.
        return self._send("INIT")

    # --- Schedule ---

    def schedule_load(self, schedule_dict):
        payload = json.dumps(schedule_dict).encode()
        return self._send("SCHD", payload)

    def schedule_stop(self):
        return self._send("SCHS")

    def schedule_query(self):
        return self._send("SCHQ")

    def schedule_next(self):
        return self._send("SCHN")

    # --- RF front-end (amplified receivers) ---

    def ext_gain(self, gains_tenths_db):
        """Set per-channel external LNA gains (tenths of dB)."""
        payload = struct.pack(f"<{len(gains_tenths_db)}I",
                              *[int(g) for g in gains_tenths_db])
        return self._send("EGAN", payload)

    def bias_tee(self, states):
        """Set per-channel bias-tee states (0/1)."""
        payload = struct.pack(f"<{len(states)}I",
                              *[1 if s else 0 for s in states])
        return self._send("BIAS", payload)

    def rf_query(self):
        """Query the RF link budget (total gain / NF / compression)."""
        return self._send("RFQ")

    # --- Antenna orientation ---

    def bearing(self, az_deg, el_deg):
        """Slew the antenna to an absolute azimuth/elevation."""
        payload = struct.pack("<ff", float(az_deg), float(el_deg))
        return self._send("ORNT", payload)

    def park(self):
        return self._send("PARK")

    def scan_start(self):
        return self._send("SCAN")

    def orientation_stop(self):
        return self._send("OSTP")

    def orientation_query(self):
        return self._send("OQRY")
