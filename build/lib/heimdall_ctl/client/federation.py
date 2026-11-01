"""Client for the federation coordinator (port 6000) and other line servers.

Wire rules (spec D.1): one request per TCP connection; the request is one
line terminated by "\\n", optionally prefixed by ``AUTH <token> ``; a JSON
body is always sent with ``@<n>`` framing (the line ends with ``@<n>`` and
exactly n body bytes follow the newline). Legacy verbs reply with one JSON
object and **no** trailing newline (the server closes the connection); new
verbs reply with one JSON object followed by "\\n".

Failover (spec F.1): the configured coordinator endpoints are tried in order
**only** when the connection cannot be established; once request bytes were
sent the request is never retried elsewhere. One ``not_leader`` redirect is
followed; a second one raises ``NotLeader`` (CLI exit 4).
"""
import json
import socket
import time

from .ctl import EndpointError

CONNECT_TIMEOUT_SEC = 2.0        # peer_connect_timeout_sec default
VERSION_TIMEOUT_SEC = 5.0
CLIENT_MARGIN_SEC = 3.0          # heimdall-ctl fleet timeout = VERSION deadline + 3 s

# Coordinator defaults (D.3.1 at default keys) used when VERSION is unavailable.
DEFAULT_DEADLINES_MS = {"query_ms": 6000, "mutation_ms": 40000, "schedule_ms": 40000}
# A legacy (protocol 1) coordinator fans out sequentially with 5 s per member
# and has no advertised deadline; these bounds only stop the client waiting forever.
LEGACY_TIMEOUTS_SEC = {"query": 60.0, "mutation": 120.0, "schedule": 120.0}

MAX_RECV_BYTES = 64 * 1024 * 1024


class NotLeader(Exception):
    """not_leader after one redirect (or with no known leader): exit 4."""

    def __init__(self, endpoint, leader):
        self.endpoint = endpoint
        self.leader = leader
        where = leader.get("endpoint") if isinstance(leader, dict) else None
        super().__init__("{}: not_leader (leader: {})".format(endpoint, where or "unknown"))


class CoordinatorError(Exception):
    """The coordinator answered VERSION with an error (auth_required, busy,
    ...): the run stops with that error instead of guessing a legacy server."""

    def __init__(self, reply):
        self.reply = reply
        super().__init__("{}:{}: {}".format(reply.endpoint[0], reply.endpoint[1],
                                            reply.error))


def is_legacy_unknown_reply(data):
    """The protocol-1 coordinator's reply to a verb it lacks (no "ok" key)."""
    return isinstance(data, dict) and "ok" not in data \
        and data.get("error") == "Unknown command"


class Reply:
    """A parsed line-server reply: .data (dict), .raw (text), .endpoint."""

    def __init__(self, data, raw, endpoint):
        self.data = data
        self.raw = raw
        self.endpoint = endpoint

    @property
    def ok(self):
        return isinstance(self.data, dict) and self.data.get("ok", True) is not False \
            and "error" not in self.data

    @property
    def error(self):
        return self.data.get("error") if isinstance(self.data, dict) else "bad_reply"


def _frame_request(line, body, auth_token):
    prefix = "AUTH {} ".format(auth_token) if auth_token else ""
    if body is not None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        return (prefix + line + " @{:d}\n".format(len(body))).encode("utf-8") + body
    return (prefix + line + "\n").encode("utf-8")


def line_request(host, port, line, body=None, auth_token=None,
                 connect_timeout=CONNECT_TIMEOUT_SEC, timeout=VERSION_TIMEOUT_SEC):
    """Send one request line (+ optional @n body) and return the reply text.

    Raises EndpointError(kind="connect") when the connection could not be
    established (nothing sent) and EndpointError(sent=True) for a timeout or
    reset after the request was sent."""
    frame = _frame_request(line, body, auth_token)
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
    except socket.timeout:
        raise EndpointError(host, port, "connect timed out after {:g} s".format(
            connect_timeout), kind="connect")
    except OSError as e:
        raise EndpointError(host, port, e.strerror or str(e), kind="connect")
    deadline = time.monotonic() + timeout
    data = b""
    try:
        sock.sendall(frame)
        while len(data) < MAX_RECV_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if data.endswith(b"\n"):
                try:
                    json.loads(data.decode("utf-8"))
                    break
                except ValueError:
                    continue
    except socket.timeout:
        raise EndpointError(host, port, "no reply within {:g} s (the request was sent; "
                            "outcome unknown)".format(timeout), sent=True, kind="timeout")
    except OSError as e:
        raise EndpointError(host, port, "connection error: {}".format(e),
                            sent=True, kind="reset")
    finally:
        sock.close()
    if not data.strip():
        raise EndpointError(host, port, "connection closed without a reply",
                            sent=True, kind="reset")
    return data.decode("utf-8", errors="replace")


def parse_json_reply(text, host, port):
    try:
        data = json.loads(text)
    except ValueError:
        raise EndpointError(host, port, "reply is not JSON", sent=True, kind="bad_reply")
    if not isinstance(data, dict):
        raise EndpointError(host, port, "reply is not a JSON object", sent=True,
                            kind="bad_reply")
    return data


class CoordinatorClient:
    """Requests to the coordinator with failover, redirect and the
    VERSION-derived timeout chain."""

    def __init__(self, endpoints, auth_token=None, timeout=None,
                 connect_timeout=CONNECT_TIMEOUT_SEC, log=None):
        if not endpoints:
            raise ValueError("no coordinator endpoint")
        self.endpoints = list(endpoints)
        self.auth_token = auth_token
        self.timeout = timeout            # explicit --timeout overrides the chain
        self.connect_timeout = connect_timeout
        self._log = log or (lambda msg: None)
        self._version = None
        self._version_fetched = False
        self._stale_version = None        # last good VERSION across refresh_version()
        self.last_endpoint = None

    # -- transport --
    def _send_to(self, ep, line, body, timeout):
        text = line_request(ep[0], ep[1], line, body, self.auth_token,
                            self.connect_timeout, timeout)
        return Reply(parse_json_reply(text, ep[0], ep[1]), text.rstrip("\n"), ep)

    def _send_failover(self, line, body, timeout):
        failures = []
        for ep in list(self.endpoints):
            try:
                reply = self._send_to(ep, line, body, timeout)
            except EndpointError as e:
                if e.kind != "connect":
                    raise
                failures.append("{}:{} ({})".format(ep[0], ep[1], e.reason))
                self._log("coordinator {}:{} unreachable: {}".format(ep[0], ep[1], e.reason))
                continue
            # Stick to the endpoint that answered for the rest of this run.
            self.endpoints.remove(ep)
            self.endpoints.insert(0, ep)
            return reply
        first = self.endpoints[0]
        raise EndpointError(first[0], first[1], "all coordinators unreachable: " +
                            ", ".join(failures), kind="connect")

    def request(self, line, body=None, kind="query", timeout=None):
        """Send one request; returns a Reply. kind selects the timeout
        ("query", "mutation" or "schedule"); an explicit timeout skips the
        VERSION lookup (used for PING/VERSION themselves)."""
        if timeout is None:
            timeout = self.timeout_for(kind)
        self._log("coordinator request {!r} (timeout {:.1f} s)".format(line, timeout))
        reply = self._send_failover(line, body, timeout)
        if reply.error == "not_leader":
            leader = reply.data.get("leader")
            target = leader.get("endpoint") if isinstance(leader, dict) else None
            if not target:
                raise NotLeader("{}:{}".format(*reply.endpoint), leader)
            host, _, port = str(target).rpartition(":")
            try:
                ep = (host, int(port))
            except ValueError:
                raise NotLeader("{}:{}".format(*reply.endpoint), leader)
            self._log("not_leader: following the redirect to {}".format(target))
            reply = self._send_to(ep, line, body, timeout)
            if reply.error == "not_leader":
                raise NotLeader(target, reply.data.get("leader"))
            if ep in self.endpoints:
                self.endpoints.remove(ep)
            self.endpoints.insert(0, ep)
        self.last_endpoint = reply.endpoint
        return reply

    # -- VERSION and the timeout chain --
    def version(self):
        """VERSION reply dict, or None for a legacy (protocol 1) coordinator.

        Only the protocol-1 unknown-verb reply marks the coordinator legacy;
        any other error reply raises CoordinatorError (after a
        refresh_version() the previous good VERSION is kept instead)."""
        if not self._version_fetched:
            timeout = self.timeout if self.timeout is not None else VERSION_TIMEOUT_SEC
            reply = self._send_failover("VERSION", None, timeout)
            self.last_endpoint = reply.endpoint
            data = reply.data
            if isinstance(data, dict) and data.get("ok") is True and "protocol" in data:
                self._version = data
            elif is_legacy_unknown_reply(data):
                self._version = None
            elif self._stale_version is not None:
                self._log("VERSION re-read failed ({}); keeping the previous "
                          "reply".format(reply.error))
                self._version = self._stale_version
            else:
                raise CoordinatorError(reply)
            self._version_fetched = True
        return self._version

    def refresh_version(self):
        """Re-read VERSION on the next use (watch loops follow role/term changes)."""
        if self._version is not None:
            self._stale_version = self._version
        self._version_fetched = False
        self._version = None

    @property
    def legacy(self):
        return self.version() is None

    def timeout_for(self, kind):
        if self.timeout is not None:
            return self.timeout
        info = self.version()
        if info is None:
            return LEGACY_TIMEOUTS_SEC[kind]
        deadlines = dict(DEFAULT_DEADLINES_MS)
        if isinstance(info.get("deadlines"), dict):
            deadlines.update({k: v for k, v in info["deadlines"].items()
                              if isinstance(v, (int, float))})
        key = {"query": "query_ms", "mutation": "mutation_ms", "schedule": "schedule_ms"}[kind]
        return deadlines[key] / 1000.0 + CLIENT_MARGIN_SEC


# --------------------------------------------------------------------------
# Per-target result classification (D.4 summary rules)
# --------------------------------------------------------------------------

def classify(result):
    """-> 'applied' | 'not_applied' | 'unconfirmed' | 'timeout' | 'error' | 'ok'."""
    if not isinstance(result, dict):
        return "error"
    if result.get("error") == "timeout":
        return "timeout"
    if "applied" in result:
        applied = result["applied"]
        if applied is True:
            return "applied"
        if applied is False:
            return "not_applied"
        if "error" in result:
            return "error"
        return "unconfirmed"
    if "error" in result or result.get("ok") is False:
        return "error"
    if "data" in result and result.get("data") is not None:
        return "ok"
    return "unconfirmed"   # legacy {"ok": true}: sent, outcome not confirmed


def format_result(result):
    """One RESULT cell for a per-target R."""
    cls = classify(result)
    if not isinstance(result, dict):
        return "error: bad result"
    fsm = result.get("fsm")
    fsm_suffix = " (fsm={})".format(fsm) if fsm else ""
    fallback = " [INIT fallback]" if result.get("fallback") == "INIT" else ""
    if cls == "applied":
        ms = result.get("elapsed_ms")
        return ("applied ({:d} ms)".format(int(ms)) if isinstance(ms, (int, float))
                else "applied") + fallback
    if cls == "not_applied":
        reason = result.get("reason") or result.get("error") or "unknown"
        return "NOT applied: {}{}{}".format(reason, fsm_suffix, fallback)
    if cls == "timeout":
        phase = result.get("phase") or "unknown"
        if phase in ("command", "verify"):
            return "timeout during {} (outcome unknown)".format(phase)
        return "timeout during {} (not sent)".format(phase)
    if cls == "error":
        text = "error: {}".format(result.get("error") or result.get("reason") or "unknown")
        if result.get("phase"):
            text += " (phase={})".format(result["phase"])
        return text
    if cls == "ok":
        return json.dumps(result["data"], separators=(",", ":"))
    if result.get("reason") == "held_for_burst":
        return "held until the calibration burst ends" + fsm_suffix
    return "sent; server does not confirm application (legacy server)" + fallback


def summarize(results):
    counts = {"applied": 0, "not_applied": 0, "unconfirmed": 0, "timeout": 0,
              "error": 0, "ok": 0}
    for r in results.values():
        counts[classify(r)] += 1
    return counts


def summary_line(counts):
    order = [("applied", "applied"), ("not_applied", "not applied"),
             ("unconfirmed", "unconfirmed"), ("timeout", "timeout"), ("error", "error"),
             ("ok", "answered")]
    parts = ["{} {:d}".format(label, counts[key]) for key, label in order
             if counts.get(key) or key in ("applied", "not_applied")]
    return "summary: " + ", ".join(parts)


def results_exit_code(counts):
    return 1 if counts["not_applied"] or counts["timeout"] or counts["error"] else 0
