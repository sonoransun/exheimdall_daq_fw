"""TCP control client for hw_controller.py (port 5001).

Builds 128-byte frames: 4-byte verb + 124-byte payload (little-endian
binary packing). Verbs shorter than 4 characters are space-padded to match
the server's decoder ("AGC " / "RFQ ").

Replies are 128 bytes starting with 'FNSD'. Query verbs (RFQ, OQRY, SCHQ,
HWSQ, TLCK, SCHG) carry a NUL-padded UTF-8 JSON document in bytes 4-127;
plain (legacy) acks carry all-zero payloads.

Ack handshake (spec F.2 / D.6): before a mutating command the client may
send ``XACK`` on the same connection. An extended server answers
``{"xack":true,...}`` and from then on replies to every mutating verb on that
connection with an extended ack ``{"applied":true|false|null,"reason":...,
"fsm":...,"seq":...}``. A legacy server answers ``FNSD`` + zeros (and logs
one ``Unidentified control command: XACK`` line); the outcome is remembered in
a per-user ack-mode cache so later commands to it are sent exactly as before.

Library default: ``CtlClient(host, port)`` keeps the historical behaviour
(no XACK, one connection per command, 5 s timeout). The CLI selects the ack
mode with ``--ack``.
"""
import json
import os
import socket
import struct
import tempfile
import time
import zlib

FRAME_SIZE = 128
VERB_SIZE = 4
PAYLOAD_SIZE = 124

# Client side of the D.3.1 timeout chain.
QUERY_TIMEOUT_SEC = 5.0          # query verbs and :5002 (today's literal)
XACK_TIMEOUT_SEC = 3.0           # T_xack
LEGACY_T_CMD_SEC = 14.0          # 2.0 + T_srv(10) + 2.0 for a legacy server
DEFAULT_RTO_MS = 10000           # hw_controller ctl_request_timeout_sec default
DEFAULT_HMX_MS = 12000           # hw_controller ctl_handler_max_sec default

ACK_CACHE_TTL_SEC = 24 * 3600
ACK_MODES = ("auto", "xack", "legacy")

# D.4 verb sets (4-byte wire verbs).
MUTATING_VERBS = frozenset([
    "FREQ", "GAIN", "AGC ", "INIT", "SCHD", "SCHS", "SCHN", "EGAN", "BIAS",
    "ORNT", "PARK", "SCAN", "OSTP", "RECL", "TLKA", "SCHB", "SCHC", "SCHE",
    "STHU"])
QUERY_VERBS = frozenset(["RFQ ", "OQRY", "SCHQ", "HWSQ", "TLCK", "SCHG"])

# Chunked schedule transfer (SCHB/SCHC/SCHE, D.6.2).
SCHC_DATA_MAX = 116
SCHC_WINDOW = 16
SCHB_MAX_TOTAL = 262144

LEGACY_ACK_WARNING = ("warning: without XACK a command that waits behind another "
                      "client may execute after this command reports a timeout")


def xack_timeout_message(timeout_sec):
    return ("server did not answer XACK within {:g} s (another client may hold the "
            "control port); command not sent - retry, or use --ack legacy to queue "
            "it behind that client".format(timeout_sec))


class EndpointError(OSError):
    """Connection failure, reset or timeout talking to host:port (CLI exit 3).

    An OSError subclass, so library callers' ``except OSError`` /
    ``except socket.error`` handlers keep catching connection failures."""

    def __init__(self, host, port, reason, sent=False, kind="error"):
        self.host = host
        self.port = port
        self.reason = reason
        self.sent = sent  # True when the command frame had already been sent
        self.kind = kind  # "connect" | "timeout" | "reset" | "bad_reply" | "error"
        super().__init__("{}:{}: {}".format(host, port, reason))


class XackTimeout(EndpointError):
    """XACK not answered within T_xack: the command was not sent."""


class XackRefused(Exception):
    """The server answered {"xack":false,...}: the command was not sent (exit 1)."""

    def __init__(self, host, port, reply):
        self.host = host
        self.port = port
        self.reply = reply
        error = reply.get("error", "refused")
        text = "cluster mismatch" if error == "cluster_mismatch" else error
        super().__init__("{}:{}: {} (XACK refused: {}); command not sent".format(
            host, port, text, error))


class ScheduleTransferError(Exception):
    """A chunked schedule transfer was refused by the server (exit 1)."""


def t_cmd_from_xack(xack):
    """Per-command read timeout T_cmd = T_member + 2 for an extended server.

    T_member = 2.0 + rto + hmx (D.3.1): 26 s at the server defaults."""
    rto = xack.get("rto", DEFAULT_RTO_MS) if xack else DEFAULT_RTO_MS
    hmx = xack.get("hmx", DEFAULT_HMX_MS) if xack else DEFAULT_HMX_MS
    try:
        return 2.0 + float(rto) / 1000.0 + float(hmx) / 1000.0 + 2.0
    except (TypeError, ValueError):
        return 2.0 + (DEFAULT_RTO_MS + DEFAULT_HMX_MS) / 1000.0 + 2.0


def build_frame(verb, payload=b""):
    """Return the 128-byte frame for a verb + payload.

    Raises ValueError when the payload does not fit the 124-byte frame body
    (it used to be truncated silently)."""
    if len(payload) > PAYLOAD_SIZE:
        raise ValueError("payload exceeds 124 bytes")
    verb_bytes = verb.encode("ascii")[:VERB_SIZE].ljust(VERB_SIZE, b" ")
    return verb_bytes + payload.ljust(PAYLOAD_SIZE, b"\x00")


def _wire_verb(verb):
    return verb[:VERB_SIZE].ljust(VERB_SIZE, " ")


def _payload_json(reply):
    """Return (state, value) for a reply: ('bad', None), ('zeros', None),
    ('json', obj) or ('raw', text)."""
    if not reply or len(reply) < 4 or reply[:4] != b"FNSD":
        return "bad", None
    payload = reply[4:].rstrip(b"\x00")
    if not payload:
        return "zeros", None
    text = payload.decode("utf-8", errors="replace")
    try:
        return "json", json.loads(text)
    except ValueError:
        return "raw", text


class AckModeCache:
    """Per-user cache of each control endpoint's ack mode.

    File: ``$XDG_CACHE_HOME/heimdall-ctl/ack_modes.json`` (default
    ``~/.cache/heimdall-ctl/ack_modes.json``); key ``host:port``; entries
    ``{"mode": "legacy"|"extended", "ts": <unix time>}``; TTL 24 h; written
    atomically. Every read/write failure is ignored ("unknown")."""

    def __init__(self, path=None, ttl=ACK_CACHE_TTL_SEC, clock=time.time):
        self.path = path or self.default_path()
        self.ttl = ttl
        self._clock = clock

    @staticmethod
    def default_path():
        base = os.environ.get("XDG_CACHE_HOME", "")
        if not base or not os.path.isabs(base):
            base = os.path.join(os.path.expanduser("~"), ".cache")
        return os.path.join(base, "heimdall-ctl", "ack_modes.json")

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key):
        entry = self._load().get(key)
        if not isinstance(entry, dict):
            return None
        mode = entry.get("mode")
        try:
            age = self._clock() - float(entry.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if mode not in ("legacy", "extended") or age < 0 or age > self.ttl:
            return None
        return mode

    def put(self, key, mode):
        now = self._clock()
        data = {}
        for k, v in self._load().items():
            try:
                if isinstance(v, dict) and 0 <= now - float(v.get("ts", 0)) <= self.ttl:
                    data[k] = v
            except (TypeError, ValueError):
                continue
        data[key] = {"mode": mode, "ts": now}
        tmp = None
        try:
            directory = os.path.dirname(self.path)
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ack_modes.")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True)
            os.replace(tmp, self.path)
            tmp = None
        except OSError:
            pass
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


class CtlSession:
    """One open :5001 connection (after the optional XACK handshake)."""

    def __init__(self, client, sock, mutating):
        self.client = client
        self.sock = sock
        self.mutating = mutating
        self.mode = "legacy"       # "extended" once XACK was answered true
        self.xack = None           # the XACK reply dict (extended only)
        self.xack_sent = False

    # -- properties derived from the XACK reply --
    @property
    def extended(self):
        return self.mode == "extended"

    @property
    def caps(self):
        if not self.xack:
            return set()
        return {c.strip() for c in str(self.xack.get("caps", "")).split(",") if c.strip()}

    @property
    def nch(self):
        if self.xack and isinstance(self.xack.get("nch"), int):
            return self.xack["nch"]
        return None

    def command_timeout(self, verb):
        """Read timeout for one reply on this connection (D.3.1)."""
        if self.client.timeout is not None:
            return self.client.timeout
        if _wire_verb(verb) not in MUTATING_VERBS:
            return QUERY_TIMEOUT_SEC
        if self.extended:
            return t_cmd_from_xack(self.xack)
        return LEGACY_T_CMD_SEC

    # -- I/O --
    def _fail(self, reason, sent, kind):
        return EndpointError(self.client.host, self.client.port, reason, sent=sent, kind=kind)

    def send_frame(self, frame):
        try:
            self.sock.sendall(frame)
        except OSError as e:
            raise self._fail("send failed: {}".format(e), sent=False, kind="reset")

    def read_reply(self, timeout, sent=True):
        self.sock.settimeout(timeout)
        reply = b""
        try:
            while len(reply) < FRAME_SIZE:
                chunk = self.sock.recv(FRAME_SIZE - len(reply))
                if not chunk:
                    break
                reply += chunk
        except socket.timeout:
            raise self._fail("no reply within {:g} s{}".format(
                timeout, " (the command was sent; outcome unknown)" if sent else ""),
                sent=sent, kind="timeout")
        except OSError as e:
            raise self._fail("connection error: {}".format(e), sent=sent, kind="reset")
        if len(reply) < FRAME_SIZE:
            raise self._fail("connection closed by the server{}".format(
                " (the command was sent; outcome unknown)" if sent else ""),
                sent=sent, kind="reset")
        return reply

    def request(self, verb, payload=b"", timeout=None):
        """Send one frame and return the 128-byte reply."""
        frame = build_frame(verb, payload)
        self.send_frame(frame)
        return self.read_reply(timeout if timeout is not None else self.command_timeout(verb))

    def handshake(self):
        """Send XACK (term 0, empty cluster_id) and classify the reply."""
        self.xack_sent = True
        self.send_frame(build_frame("XACK", b""))
        try:
            reply = self.read_reply(self.client.xack_timeout, sent=False)
        except EndpointError as e:
            if e.kind == "timeout":
                raise XackTimeout(self.client.host, self.client.port,
                                  xack_timeout_message(self.client.xack_timeout),
                                  kind="timeout")
            raise
        state, value = _payload_json(reply)
        if state == "zeros":
            self.mode = "legacy"
            self.client.remember_mode("legacy")
        elif state == "json" and isinstance(value, dict) and value.get("xack") is True:
            self.mode = "extended"
            self.xack = value
            self.client.remember_mode("extended")
        elif state == "json" and isinstance(value, dict) and value.get("xack") is False:
            raise XackRefused(self.client.host, self.client.port, value)
        else:
            raise EndpointError(self.client.host, self.client.port,
                                "bad XACK reply; command not sent", kind="bad_reply")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- chunked schedule transfer --
    def upload_schedule_chunked(self, data_bytes, window=SCHC_WINDOW):
        """SCHB + pipelined SCHC + SCHE. Returns the SCHE reply bytes and the
        number of chunks. Raises ScheduleTransferError on a refusal."""
        if not 0 < len(data_bytes) <= SCHB_MAX_TOTAL:
            raise ScheduleTransferError(
                "schedule is {:d} bytes; the chunked limit is {:d}".format(
                    len(data_bytes), SCHB_MAX_TOTAL))
        chunks = [data_bytes[i:i + SCHC_DATA_MAX]
                  for i in range(0, len(data_bytes), SCHC_DATA_MAX)]
        header = struct.pack("<IIHH", len(data_bytes),
                             zlib.crc32(data_bytes) & 0xFFFFFFFF, len(chunks), 0)
        state, value = _payload_json(self.request("SCHB", header,
                                                  timeout=self.command_timeout("HWSQ")))
        if state != "json" or not isinstance(value, dict) or not value.get("ok"):
            reason = value.get("reason", "refused") if isinstance(value, dict) else "bad reply"
            raise ScheduleTransferError("SCHB refused: {}".format(reason))
        sess = int(value.get("sess", 0))
        read_timeout = self.command_timeout("HWSQ")
        sent = acked = 0
        while acked < len(chunks):
            while sent < len(chunks) and sent - acked < window:
                chunk = chunks[sent]
                payload = struct.pack("<HHH", sess, sent, len(chunk)) + chunk
                self.send_frame(build_frame("SCHC", payload))
                sent += 1
            state, value = _payload_json(self.read_reply(read_timeout))
            if state != "json" or not isinstance(value, dict) or not value.get("ok"):
                reason = value.get("reason", "refused") if isinstance(value, dict) else "bad reply"
                raise ScheduleTransferError("SCHC chunk {:d} refused: {}".format(acked, reason))
            acked += 1
        reply = self.request("SCHE", struct.pack("<H", sess))
        return reply, len(chunks)


class CtlClient:
    FRAME_SIZE = FRAME_SIZE
    VERB_SIZE = VERB_SIZE
    PAYLOAD_SIZE = PAYLOAD_SIZE

    def __init__(self, host="127.0.0.1", port=5001, timeout=5.0, ack="legacy",
                 cache=None, xack_timeout=None):
        """timeout: fixed read timeout for every reply (the historical 5 s);
        None selects the D.3.1 chain (queries 5 s, mutating = server T_cmd).
        ack: "legacy" (default: never XACK), "xack" (always) or "auto"
        (XACK unless the ack-mode cache says the server is legacy).
        xack_timeout: T_xack (default XACK_TIMEOUT_SEC, 3 s)."""
        if ack not in ACK_MODES:
            raise ValueError("ack must be one of {}".format(", ".join(ACK_MODES)))
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ack = ack
        self.xack_timeout = xack_timeout if xack_timeout is not None else XACK_TIMEOUT_SEC
        # "xack" decides without the cache but records the handshake outcome,
        # so an explicit --ack xack re-probe also refreshes the auto-mode entry.
        self.cache = cache if cache is not None else \
            (AckModeCache() if ack in ("auto", "xack") else None)

    @property
    def cache_key(self):
        return "{}:{}".format(self.host, self.port)

    def remember_mode(self, mode):
        if self.cache is not None and self.ack in ("auto", "xack"):
            self.cache.put(self.cache_key, mode)

    def will_xack(self, mutating):
        if not mutating or self.ack == "legacy":
            return False
        if self.ack == "xack":
            return True
        return self.cache is None or self.cache.get(self.cache_key) != "legacy"

    def open(self, mutating=True):
        """Connect once and (per the ack mode) perform the XACK handshake."""
        connect_timeout = self.timeout if self.timeout is not None else QUERY_TIMEOUT_SEC
        try:
            sock = socket.create_connection((self.host, self.port), timeout=connect_timeout)
        except socket.timeout:
            raise EndpointError(self.host, self.port,
                                "connect timed out after {:g} s".format(connect_timeout),
                                kind="connect")
        except OSError as e:
            raise EndpointError(self.host, self.port, e.strerror or str(e), kind="connect")
        session = CtlSession(self, sock, mutating)
        if self.will_xack(mutating):
            try:
                session.handshake()
            except BaseException:
                session.close()
                raise
        return session

    def _send(self, verb, payload=b""):
        """One command on its own connection; returns the raw 128-byte reply."""
        if len(payload) > self.PAYLOAD_SIZE:
            raise ValueError("payload exceeds 124 bytes")
        with self.open(mutating=_wire_verb(verb) in MUTATING_VERBS) as session:
            return session.request(verb, payload)

    @staticmethod
    def parse_reply(reply):
        """Parse a 128-byte control reply into a dict.

        Returns {"ok": bool, ...}. For query verbs the server may embed a
        NUL-padded UTF-8 JSON document in bytes 4-127 -> {"ok": True,
        "data": <parsed>}. An all-zeros payload (plain ack / no data) yields
        {"ok": True, "data": None, "legacy": True}. Unparseable payloads are
        surfaced raw. An extended ack (JSON object with an "applied" key) also
        sets "applied", "reason", "fsm" and "seq".
        """
        state, value = _payload_json(reply)
        if state == "bad":
            return {"ok": False, "error": "bad reply",
                    "raw": reply.hex() if reply else ""}
        if state == "zeros":
            return {"ok": True, "data": None, "legacy": True}
        if state == "raw":
            return {"ok": True, "data": None, "raw": value}
        parsed = {"ok": True, "data": value}
        if isinstance(value, dict) and "applied" in value:
            for key in ("applied", "reason", "fsm", "seq"):
                parsed[key] = value.get(key)
        return parsed

    @staticmethod
    def parse_ack(reply, extended):
        """Ack mapping used by the CLI:
        {"applied": True|False|None, "reason": str|None, "fsm": str|None,
         "legacy": bool}. ``legacy`` is True when the server did not confirm
        the outcome (all-zero payload)."""
        state, value = _payload_json(reply)
        if state == "zeros":
            return {"applied": None, "reason": None, "fsm": None, "legacy": True}
        if state == "json" and isinstance(value, dict):
            if "applied" in value:
                return {"applied": value.get("applied"), "reason": value.get("reason"),
                        "fsm": value.get("fsm"), "legacy": False}
            if "error" in value:
                return {"applied": False, "reason": str(value["error"]),
                        "fsm": None, "legacy": False}
            return {"applied": None, "reason": None, "fsm": None, "legacy": not extended}
        return {"applied": False, "reason": "bad_reply", "fsm": None, "legacy": False}

    # --- Payload builders (shared by the CLI and the methods below) ---

    @staticmethod
    def pack_u32_list(values):
        return struct.pack("<{:d}I".format(len(values)), *[int(v) for v in values])

    @staticmethod
    def schedule_json(schedule_dict):
        return json.dumps(schedule_dict, separators=(",", ":")).encode("utf-8")

    # --- Tuner / calibration ---

    def freq(self, hz):
        payload = struct.pack("<Q", int(hz))
        return self._send("FREQ", payload)

    def gain(self, gains):
        return self._send("GAIN", self.pack_u32_list(gains))

    def gain_unified(self, value, num_channels):
        return self.gain([value] * num_channels)

    def agc(self):
        return self._send("AGC")

    def init(self):
        return self._send("INIT")

    def recal(self):
        """Re-run the INIT sequence (what every legacy server understands).
        The CLI's ``recal`` uses RECL when the server advertises it."""
        return self._send("INIT")

    def wire_recal(self):
        return self._send("RECL")

    # --- Schedule ---

    def schedule_load(self, schedule_dict):
        return self._send("SCHD", self.schedule_json(schedule_dict))

    def schedule_stop(self):
        return self._send("SCHS")

    def schedule_query(self):
        return self._send("SCHQ")

    def schedule_next(self):
        return self._send("SCHN")

    def schedule_gen_query(self):
        return self._send("SCHG")

    # --- hw_controller state / track lock ---

    def hwc_state_query(self):
        return self._send("HWSQ")

    def track_lock_query(self):
        return self._send("TLCK")

    def track_lock_set(self, approve):
        return self._send("TLKA", struct.pack("<I", 1 if approve else 0))

    # --- RF front-end (amplified receivers) ---

    def ext_gain(self, gains_tenths_db):
        """Set per-channel external LNA gains (tenths of dB)."""
        return self._send("EGAN", self.pack_u32_list(gains_tenths_db))

    def bias_tee(self, states):
        """Set per-channel bias-tee states (0/1)."""
        return self._send("BIAS", self.pack_u32_list([1 if s else 0 for s in states]))

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
