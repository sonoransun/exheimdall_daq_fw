"""
    Federation Common — shared pure helpers for the federation services

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    One implementation of the rules every federation component must agree on
    (design spec C.2, D.1, D.3.2), stdlib only:

    * Membership grammar: ``host:instance_id[@node_id]`` entries, node keys,
      the legacy ``host:status_port`` peer_list form, router sources
      (``host:iid[@node_id]`` / ``router://host:port``), self entries and
      local-host detection.
    * ``:6000`` request assembly (``read_request``), reply framing
      (``json_reply``) and the matching client side (``encode_request``,
      ``request_json``).
    * Deadline-bounded parallel fan-out (``fan_out``) with a per-fan-out
      cancel event, a pre-send guard and an abandoned-worker gauge.

    Nothing here is imported by the default (federation disabled) data path.
"""
import hmac
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait

logger = logging.getLogger("federation_common")

# ---------------------------------------------------------------------------
# Ports and identity grammar
# ---------------------------------------------------------------------------

#: Base ports of instance 0; instance N adds N * port_stride (compute_port()).
BASE_PORTS = {"iq": 5000, "ctl": 5001, "status": 5002, "events": 5003, "zmq": 1130}

DEFAULT_PORT_STRIDE = 100
DEFAULT_STATUS_BASE_PORT = 5002

#: In the legacy peer_list form a number >= this is a status port, not an id.
LEGACY_STATUS_PORT_MIN = 1024

NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,47}$")
CLUSTER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,30}$")
AUTH_TOKEN_MAX_LEN = 128

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain",
                             "ip6-localhost", "ip6-loopback"})

_MEMBER_USAGE = "expected host:instance_id[@node_id]"
_ROUTER_USAGE = "expected host:instance_id[@node_id] or router://host:port"
_TAG_MODES = ("instance", "unit", "node")


def is_valid_node_id(value):
    """True when *value* matches the node_id grammar (C.1)."""
    return isinstance(value, str) and NODE_ID_RE.match(value) is not None


def is_valid_cluster_id(value):
    """True when *value* matches the cluster_id grammar (C.1, <= 31 chars)."""
    return isinstance(value, str) and CLUSTER_ID_RE.match(value) is not None


def is_valid_auth_token(value):
    """True for a non-empty printable ASCII token without spaces, <= 128 chars."""
    return (isinstance(value, str) and 0 < len(value) <= AUTH_TOKEN_MAX_LEN
            and all(33 <= ord(c) <= 126 for c in value))


def compute_ports(instance_id, port_stride=DEFAULT_PORT_STRIDE,
                  status_base_port=DEFAULT_STATUS_BASE_PORT):
    """Ports of instance *instance_id*: base + instance_id * port_stride.

    Mirrors compute_port() in sh_mem_util.c. The status base follows
    ``[monitoring] status_server_port``; the other bases are fixed.
    """
    off = int(instance_id) * int(port_stride)
    ports = {name: base + off for name, base in BASE_PORTS.items()}
    ports["status"] = int(status_base_port) + off
    return ports


def _ports_valid(ports):
    return all(p is None or 1 <= p <= 65535 for p in ports.values())


def _format_host(host):
    """Host as it appears in a node key / endpoint (IPv6 literals bracketed)."""
    return "[{}]".format(host) if ":" in host else host


def node_key(host, instance_id):
    """Cluster-unique node key ``host:instance_id`` with the host lower-cased.

    An IPv6 literal host is bracketed (``[fe80::1]:0``) so the key stays
    unambiguous and re-parseable.
    """
    return "{}:{}".format(_format_host(str(host).lower()), int(instance_id))


def _short_hostname():
    return socket.gethostname().split(".", 1)[0].lower()


def default_node_id(instance_id):
    """Default ``[federation] node_id``: ``<short lower-case hostname>:<N>``.

    Characters outside the node_id grammar are replaced by ``-`` and the
    result is truncated to the 48-character limit.
    """
    suffix = ":{}".format(int(instance_id))
    host = re.sub(r"[^A-Za-z0-9_.-]", "-", _short_hostname()) or "host"
    if not host[0].isalnum():
        host = "h" + host
    host = host[:48 - len(suffix)]
    return host + suffix


def is_loopback(host):
    """True for ``localhost`` (and its usual aliases), 127.0.0.0/8 and ::1."""
    if not host:
        return False
    h = str(host).strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in _LOOPBACK_NAMES or h.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def self_host(advertise_host=None):
    """Host part of this machine's node keys.

    ``advertise_host`` when given, else the short lower-case
    ``socket.gethostname()``. Never a loopback name: raises ValueError when
    the only available name is one.
    """
    if advertise_host is not None and str(advertise_host).strip():
        host = str(advertise_host).strip().lower()
        if is_loopback(host):
            raise ValueError("advertise_host '{}' is a loopback host; use the host's "
                             "advertised name".format(advertise_host))
        return host
    host = _short_hostname()
    if not host or is_loopback(host):
        raise ValueError("the host name '{}' is a loopback name; set [federation] "
                         "advertise_host".format(socket.gethostname()))
    return host


# ---------------------------------------------------------------------------
# Local-host detection (cached)
# ---------------------------------------------------------------------------

LOCAL_HOST_CACHE_SEC = 60.0
#: Most answers kept; the oldest is dropped first (host strings may come
#: from the network, so the cache must not grow without bound).
LOCAL_HOST_CACHE_MAX = 1024
_local_cache = OrderedDict()  # (host, resolve) -> (expires_mono, bool)
_local_cache_lock = threading.Lock()


def clear_local_host_cache():
    """Forget cached is_local_host() answers (interface addresses changed)."""
    with _local_cache_lock:
        _local_cache.clear()


def _is_unicast(addr_str):
    """False for an address that can never be a local interface's own
    address: multicast, unspecified (wildcard), reserved or broadcast. An
    IPv4-mapped IPv6 address is judged by its IPv4 address."""
    try:
        ip = ipaddress.ip_address(addr_str.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    return not (ip.version == 4 and ip == ipaddress.IPv4Address("255.255.255.255"))


def _is_local_address(addr_str):
    """True when *addr_str* (numeric) is assigned to a local interface.

    Multicast, wildcard, reserved and broadcast addresses are never local.
    For the rest, binding a socket succeeds only for a local address, which
    needs no interface enumeration and no DNS. With Linux
    ``net.ipv4.ip_nonlocal_bind=1`` (or ``ipv6.ip_nonlocal_bind``) the
    kernel accepts any bind, so every unicast literal then reports local.
    """
    if not _is_unicast(addr_str):
        return False
    try:
        infos = socket.getaddrinfo(addr_str, 0, socket.AF_UNSPEC, socket.SOCK_DGRAM,
                                   0, socket.AI_NUMERICHOST)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            s = socket.socket(family, socktype, proto)
        except OSError:
            continue
        try:
            s.bind(sockaddr)
            return True
        except OSError:
            continue
        finally:
            s.close()
    return False


def _compute_local(h, resolve):
    if is_loopback(h):
        return True
    full = socket.gethostname().lower()
    if h in (full, full.split(".", 1)[0]):
        return True
    try:
        ipaddress.ip_address(h.split("%", 1)[0])
        return _is_local_address(h)
    except ValueError:
        pass
    if not resolve:
        return False
    try:
        infos = socket.getaddrinfo(h, None, socket.AF_UNSPEC, socket.SOCK_DGRAM)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    return any(is_loopback(info[4][0]) or _is_local_address(info[4][0]) for info in infos)


def is_local_host(host, resolve=False):
    """True when *host* names this machine.

    Loopback names/addresses, this machine's host name (full or short,
    case-insensitive) and IP literals assigned to a local interface are
    local. Other names are **not** resolved unless ``resolve=True`` (a DNS
    lookup can stall; request handlers must never resolve, D.3.4).
    Answers are cached for LOCAL_HOST_CACHE_SEC seconds.
    """
    if host is None:
        return False
    h = str(host).strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if not h:
        return False
    key = (h, bool(resolve))
    now = time.monotonic()
    with _local_cache_lock:
        hit = _local_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    result = _compute_local(h, bool(resolve))
    with _local_cache_lock:
        _local_cache[key] = (now + LOCAL_HOST_CACHE_SEC, result)
        _local_cache.move_to_end(key)
        while len(_local_cache) > LOCAL_HOST_CACHE_MAX:
            _local_cache.popitem(last=False)
    return result


# ---------------------------------------------------------------------------
# Membership grammar (C.2)
# ---------------------------------------------------------------------------

_legacy_logged = set()
_legacy_logged_lock = threading.Lock()


#: Longest number accepted in a canonical entry or router source. Ten
#: digits cover every id / port; a longer number is a ``bad ...`` error.
#: The legacy peer_list form keeps today's ``int()`` acceptance instead
#: (a number int() refuses, e.g. past CPython's integer-string digit
#: limit, is a ``bad member entry``).
_MAX_NUMBER_DIGITS = 10


def _is_ipv6_literal(host):
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).version == 6
    except ValueError:
        return False


def _split_host_number(text, legacy=False):
    """Split ``host:number`` (``[v6]:number`` for IPv6) -> (host, number_str) or None.

    ``legacy=True`` (peer_list) also takes today's ``rsplit(":", 1)``
    forms: an unbracketed IPv6 literal host (``2001:db8::5:5102``) and
    whitespace around the host and the number (``hostb: 5102``).
    """
    if text.startswith("["):
        close = text.find("]")
        if close <= 1 or text[close + 1:close + 2] != ":":
            return None
        host, num = text[1:close], text[close + 2:]
        if ":" not in host:
            return None
    else:
        host, sep, num = text.rpartition(":")
        if not sep:
            return None
        if legacy:
            host = host.strip()
        if ":" in host and not (legacy and _is_ipv6_literal(host)):
            return None
    if legacy:
        num = num.strip()
    if not host or any(c.isspace() or c in "/@[]" for c in host):
        return None
    if not num.isascii() or not num.isdigit():
        return None
    if not legacy and len(num) > _MAX_NUMBER_DIGITS:
        return None
    return host, num


def parse_member(entry, default_stride=DEFAULT_PORT_STRIDE,
                 status_base_port=DEFAULT_STATUS_BASE_PORT, legacy_port_form=False,
                 allow_loopback=None):
    """Parse one membership entry ``host:instance_id[@node_id]``.

    Returns ``{"entry", "host", "connect_host", "instance_id", "node_key",
    "node_id", "port_stride", "ports": {"iq","ctl","status","events","zmq"},
    "legacy_status_port"}``. ``node_id`` is None when the entry does not
    name one.

    ``legacy_port_form=True`` (peer_list only): ``host:N`` with
    N >= 1024 is a status port (today's FederationHealth rule). The
    instance id is derived when N fits ``status_base_port + id * stride``;
    otherwise it is -1, the node key is ``host:port<N>`` and every port but
    ``status`` is None.

    In the legacy form port numbers are not range-checked, exactly as today
    (an entry whose port is unusable is accepted and simply never answers);
    otherwise a port above 65535 is a ``bad member entry``.

    ``allow_loopback`` defaults to ``legacy_port_form`` (peer_list may name
    127.0.0.1; cluster_members / --instances may not).

    Raises ValueError with the exact C.2 messages.
    """
    if allow_loopback is None:
        allow_loopback = legacy_port_form
    text = str(entry).strip()
    bad = ValueError("bad member entry '{}' ({})".format(text, _MEMBER_USAGE))
    body, at, nid = text.partition("@")
    node_id = None
    if at:
        if not is_valid_node_id(nid):
            raise ValueError("bad node_id '{}'".format(nid))
        node_id = nid
    split = _split_host_number(body, legacy=legacy_port_form)
    if split is None:
        raise bad
    host, num_str = split
    try:
        num = int(num_str)
    except ValueError:              # beyond the interpreter's int-string digit limit
        raise bad
    stride = int(default_stride)
    legacy_status = legacy_port_form and num >= LEGACY_STATUS_PORT_MIN
    if legacy_status:
        offset = num - int(status_base_port)
        if stride > 0 and offset >= 0 and offset % stride == 0:
            iid = offset // stride
            ports = compute_ports(iid, stride, status_base_port)
            key = node_key(host, iid)
        else:
            iid = -1
            ports = {name: None for name in BASE_PORTS}
            ports["status"] = num
            key = "{}:port{}".format(_format_host(host.lower()), num)
        with _legacy_logged_lock:
            first = text not in _legacy_logged
            _legacy_logged.add(text)
        if first:
            logger.info("peer_list entry '%s' is a legacy status-port form", text)
    else:
        iid = num
        ports = compute_ports(iid, stride, status_base_port)
        key = node_key(host, iid)
    if not legacy_port_form and not _ports_valid(ports):
        raise bad
    if not allow_loopback and is_loopback(host):
        raise ValueError("member '{}' uses a loopback host; use the host's "
                         "advertised name".format(text))
    return {
        "entry": text,
        "host": host,
        "connect_host": host,
        "instance_id": iid,
        "node_key": key,
        "node_id": node_id,
        "port_stride": stride,
        "ports": ports,
        "legacy_status_port": legacy_status,
    }


def _split_list(s):
    if s is None:
        return []
    if isinstance(s, (list, tuple)):
        items = s
    else:
        items = str(s).split(",")
    return [str(i).strip() for i in items if str(i).strip()]


def parse_member_list(s, *, allow_duplicates=False, **kw):
    """Parse a comma-separated membership list (or a list of entries).

    Empty items are skipped. Duplicate node keys raise
    ``duplicate member '<key>'`` unless ``allow_duplicates`` (peer_list and
    the legacy coordinator list keep duplicates, in order). Other keyword
    arguments go to parse_member().
    """
    out = []
    seen = set()
    for item in _split_list(s):
        m = parse_member(item, **kw)
        if m["node_key"] in seen and not allow_duplicates:
            raise ValueError("duplicate member '{}'".format(m["node_key"]))
        seen.add(m["node_key"])
        out.append(m)
    return out


def self_member(instance_id, port_stride=DEFAULT_PORT_STRIDE, advertise_host=None,
                status_base_port=DEFAULT_STATUS_BASE_PORT):
    """Implicit self entry: node key ``<self_host>:<N>``, connections to 127.0.0.1."""
    host = self_host(advertise_host)
    m = parse_member("{}:{}".format(_format_host(host), int(instance_id)),
                     default_stride=port_stride, status_base_port=status_base_port,
                     allow_loopback=False)
    m["connect_host"] = "127.0.0.1"
    m["self"] = True
    return m


def parse_router_source(entry, default_stride=DEFAULT_PORT_STRIDE):
    """Parse one router source.

    ``host:iid[@node_id]`` -> ``{"kind":"iqserver","entry","host",
    "connect_host","instance_id","node_key","node_id","port"}`` with
    port = 5000 + iid * stride. ``router://host:port`` -> ``{"kind":"router",
    "entry","host","connect_host","port","node_key":None,"via":"host:port"}``.
    """
    text = str(entry).strip()
    bad = ValueError("bad router source '{}' ({})".format(text, _ROUTER_USAGE))
    if text.lower().startswith("router://"):
        split = _split_host_number(text[len("router://"):])
        if split is None:
            raise bad
        host, port_str = split
        port = int(port_str)
        if not 1 <= port <= 65535:
            raise bad
        return {"kind": "router", "entry": text, "host": host, "connect_host": host,
                "port": port, "node_key": None,
                "via": "{}:{}".format(_format_host(host), port)}
    if "://" in text:
        raise bad
    try:
        m = parse_member(text, default_stride=default_stride, allow_loopback=True)
    except ValueError as e:
        if str(e).startswith("bad node_id"):
            raise
        raise bad
    return {"kind": "iqserver", "entry": text, "host": m["host"],
            "connect_host": m["connect_host"], "instance_id": m["instance_id"],
            "node_key": m["node_key"], "node_id": m["node_id"], "port": m["ports"]["iq"]}


def parse_router_source_list(s, *, tag_mode="instance", **kw):
    """Parse ``router_sources`` / ``--sources``.

    Duplicates (same node key, or the same ``router://`` endpoint) raise
    ``duplicate router source '<key>'``. With ``tag_mode="node"`` an
    iq_server source on a loopback host raises (node keys must be
    cluster-unique). Keyword arguments go to parse_router_source().
    """
    if tag_mode not in _TAG_MODES:
        raise ValueError("bad tag_mode '{}' (expected instance, unit or node)".format(tag_mode))
    out = []
    seen = set()
    for item in _split_list(s):
        src = parse_router_source(item, **kw)
        if src["kind"] == "router":
            key = "router://{}".format(src["via"].lower())
        else:
            key = src["node_key"]
            if tag_mode == "node" and is_loopback(src["host"]):
                raise ValueError("router source '{}' uses a loopback host; with tag_mode=node "
                                 "use the host's advertised name".format(src["entry"]))
        if key in seen:
            raise ValueError("duplicate router source '{}'".format(key))
        seen.add(key)
        out.append(src)
    return out


# ---------------------------------------------------------------------------
# :6000 request assembly and reply framing (D.1)
# ---------------------------------------------------------------------------

#: Verbs today's clients send without a trailing newline; replies keep
#: today's framing (no trailing newline).
LEGACY_VERBS = frozenset({"PING", "STATUS", "FREQ", "GAIN", "INSTANCE", "REBALANCE"})
#: Verbs whose argument is a JSON document (@n body or <= 3072-byte inline).
JSON_VERBS = frozenset({"REGISTER", "SCHEDULE"})

FIRST_READ_BYTES = 4096
FIRST_READ_TIMEOUT_SEC = 5.0
CHUNK_TIMEOUT_SEC = 2.0
LINE_TIMEOUT_SEC = 10.0
BODY_TIMEOUT_SEC = 10.0
MAX_LINE_BYTES = 65536
MAX_BODY_BYTES = 1048576
MAX_INLINE_BYTES = 3072

_BODY_MARK_RE = re.compile(r"^@([0-9]+)$")      # ASCII decimal only (D.1 step 6)


class RequestError(Exception):
    """A malformed or refused request; send ``json_reply(e.reply, e.newline)``."""

    def __init__(self, reply, newline=True):
        super().__init__(reply.get("error", "bad_request"))
        self.reply = reply
        self.code = reply.get("error", "bad_request")
        self.newline = newline


class Request(object):
    """One assembled ``:6000`` request.

    verb      upper-cased first token (after the AUTH prefix)
    args      the rest of the line, stripped, without the ``@n`` marker
    tokens    ``args.split()``
    body      the ``@n`` body bytes, or None
    legacy    True when assembled by the legacy no-newline rule (D.1 step 4)
    line      the request line without the AUTH prefix
    """
    __slots__ = ("verb", "args", "tokens", "body", "legacy", "line")

    def __init__(self, verb, args, body, legacy, line):
        self.verb = verb
        self.args = args
        self.tokens = args.split()
        self.body = body
        self.legacy = legacy
        self.line = line

    @property
    def legacy_verb(self):
        """True when the reply must use legacy framing (no trailing newline)."""
        return self.verb in LEGACY_VERBS

    def inline_json(self):
        """The inline JSON text of the line (from the first ``{`` or ``[``) or None."""
        for i, c in enumerate(self.args):
            if c in "{[":
                return self.args[i:]
        return None

    def body_json(self):
        """Parse the JSON document: the ``@n`` body, else the inline JSON.

        Returns None when the request carries neither; raises RequestError
        (``bad_request``) on invalid JSON.
        """
        if self.body is not None:
            text = self.body.decode("utf-8", errors="replace")
        else:
            text = self.inline_json()
            if text is None:
                return None
        try:
            return json.loads(text)
        except ValueError as e:
            raise RequestError({"ok": False, "error": "bad_request",
                                "message": "invalid JSON body: {}".format(e)},
                               newline=not self.legacy_verb)

    def __repr__(self):
        return "Request(verb={!r}, args={!r}, body={}, legacy={})".format(
            self.verb, self.args[:80],
            None if self.body is None else "{} bytes".format(len(self.body)), self.legacy)


def _token_ok(presented, auth_token):
    """Constant-time check of a presented AUTH token (str or bytes)."""
    if presented is None:
        return False
    if isinstance(presented, str):
        presented = presented.encode("utf-8")
    return hmac.compare_digest(presented, str(auth_token).encode("utf-8"))


def _chunk_token(chunk):
    """The token of an ``AUTH <token>`` prefix of a raw chunk, else None."""
    toks = chunk.split(None, 2)
    if len(toks) >= 2 and toks[0].upper() == b"AUTH":
        return toks[1]
    return None


def _verb_of(chunk):
    """Upper-cased first token of a raw chunk, skipping an ``AUTH <token>`` prefix."""
    toks = chunk.split()
    if toks and toks[0].upper() == b"AUTH":
        toks = toks[2:]
    if not toks:
        return ""
    return toks[0].decode("utf-8", errors="replace").upper()


def _recv(sock, n):
    try:
        return sock.recv(n)
    except socket.timeout:
        raise
    except OSError:
        return b""


def read_request(sock, auth_token=None, *, legacy_verbs=LEGACY_VERBS, json_verbs=JSON_VERBS,
                 first_timeout=FIRST_READ_TIMEOUT_SEC, chunk_timeout=CHUNK_TIMEOUT_SEC,
                 line_timeout=LINE_TIMEOUT_SEC, body_timeout=BODY_TIMEOUT_SEC,
                 max_line=MAX_LINE_BYTES, max_body=MAX_BODY_BYTES,
                 max_inline=MAX_INLINE_BYTES, first_read=FIRST_READ_BYTES):
    """Assemble one request from *sock* by the D.1 rules.

    Returns a Request, or None when there is nothing to answer (EOF or
    timeout before any byte, or an empty line). Raises RequestError for
    ``line_too_long``, ``timeout`` (body), ``body_too_large``,
    ``body_too_large_use_@n``, ``auth_required`` and ``bad_request``.

    When *auth_token* is set every request must begin ``AUTH <token> ``
    (constant-time compare); the check happens before any ``@n`` body is
    read. Without a token an ``AUTH`` prefix is stripped and ignored.
    The socket's timeout is left modified; set your own before replying.
    """
    sock.settimeout(first_timeout)
    try:
        first = _recv(sock, first_read)
    except socket.timeout:
        return None
    if not first:
        return None
    buf = first
    legacy = False
    nl = buf.find(b"\n")
    if nl < 0:
        verb = _verb_of(buf)
        if verb in legacy_verbs:
            legacy = True                      # D.1 step 4: the chunk is the request
        elif verb in json_verbs and len(first) >= first_read:
            if auth_token and not _token_ok(_chunk_token(buf), auth_token):
                raise RequestError({"ok": False, "error": "auth_required"})  # D.1 step 2 first
            raise RequestError({"ok": False, "error": "body_too_large_use_@n",
                                "limit": max_inline})
        else:
            line_deadline = time.monotonic() + line_timeout
            while nl < 0 and len(buf) < max_line:
                remaining = line_deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(min(chunk_timeout, remaining))
                try:
                    chunk = _recv(sock, 4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                start = len(buf)
                buf += chunk
                nl = buf.find(b"\n", start)
    if nl < 0:
        if not legacy and len(buf) >= max_line:
            raise RequestError({"ok": False, "error": "line_too_long", "limit": max_line})
        line_bytes, rest = buf, b""
    else:
        if nl > max_line:
            raise RequestError({"ok": False, "error": "line_too_long", "limit": max_line})
        line_bytes, rest = buf[:nl], buf[nl + 1:]

    line = line_bytes.decode("utf-8", errors="replace").strip()
    presented = None
    parts = line.split(None, 1)
    if parts and parts[0].upper() == "AUTH":
        tail = parts[1] if len(parts) > 1 else ""
        tparts = tail.split(None, 1)
        presented = tparts[0] if tparts else ""
        line = tparts[1].strip() if len(tparts) > 1 else ""
    parts = line.split(None, 1)
    verb = parts[0].upper() if parts else ""
    newline = verb not in legacy_verbs
    if auth_token:
        if not _token_ok(presented, auth_token):
            raise RequestError({"ok": False, "error": "auth_required"}, newline=newline)
    if not verb:
        return None
    args = parts[1].strip() if len(parts) > 1 else ""

    body = None
    toks = args.rsplit(None, 1)
    mark = _BODY_MARK_RE.match(toks[-1]) if toks else None
    if mark:
        digits = mark.group(1).lstrip("0") or "0"
        args = toks[0].strip() if len(toks) > 1 else ""
        # compare lengths first: never int() an attacker-sized digit string
        if len(digits) > len(str(max_body)) or int(digits) > max_body:
            raise RequestError({"ok": False, "error": "body_too_large", "limit": max_body},
                               newline=newline)
        n = int(digits)
        if n < 1:
            raise RequestError({"ok": False, "error": "bad_request",
                                "message": "bad body length '@{}' (expected @1..@{})".format(
                                    mark.group(1), max_body)}, newline=newline)
        body = rest
        deadline = time.monotonic() + body_timeout
        while len(body) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestError({"ok": False, "error": "timeout"}, newline=newline)
            sock.settimeout(remaining)
            try:
                chunk = _recv(sock, min(65536, n - len(body)))
            except socket.timeout:
                raise RequestError({"ok": False, "error": "timeout"}, newline=newline)
            if not chunk:
                raise RequestError({"ok": False, "error": "bad_request",
                                    "message": "body truncated: got {} of {} bytes".format(
                                        len(body), n)}, newline=newline)
            body += chunk
        body = body[:n]

    req = Request(verb, args, body, legacy, line)
    if body is None and verb in json_verbs:
        inline = req.inline_json()
        if inline is not None and len(inline.encode("utf-8")) > max_inline:
            raise RequestError({"ok": False, "error": "body_too_large_use_@n",
                                "limit": max_inline}, newline=newline)
    return req


def json_reply(obj, newline):
    """Encode a reply: JSON (default separators, as today) plus ``\\n`` when *newline*.

    Legacy verbs reply without the newline (today's framing); new verbs with.
    """
    data = json.dumps(obj).encode("utf-8")
    return data + b"\n" if newline else data


def encode_request(line, body=None, auth_token=None):
    """Client side of D.1: ``[AUTH <token> ]<line>[ @<n>]\\n[<body>]``.

    *body* may be bytes, str or a JSON-serialisable object (encoded
    compactly); it is always sent as an ``@n`` body.
    """
    line = str(line).strip()
    if "\n" in line or "\r" in line:
        raise ValueError("request line must not contain a newline")
    if auth_token:
        if not is_valid_auth_token(auth_token):
            raise ValueError("bad auth_token (printable, no spaces, <= {} chars)".format(
                AUTH_TOKEN_MAX_LEN))
        line = "AUTH {} {}".format(auth_token, line)
    if body is None:
        return line.encode("utf-8") + b"\n"
    if isinstance(body, str):
        payload = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray)):
        payload = bytes(body)
    else:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(payload) <= MAX_BODY_BYTES:
        raise ValueError("body must be 1..{} bytes (got {})".format(MAX_BODY_BYTES, len(payload)))
    return "{} @{}\n".format(line, len(payload)).encode("utf-8") + payload


def request_json(host, port, line, *, body=None, auth_token=None, connect_timeout=2.0,
                 timeout=5.0, max_reply=16 * 1024 * 1024):
    """Send one request to a ``:6000``-style server and return the JSON reply object.

    Reads until the server closes (one request per connection) or the
    overall *timeout* expires. Raises OSError (incl. socket.timeout) on
    transport failure and ValueError when the reply is not a JSON object.
    """
    data = encode_request(line, body=body, auth_token=auth_token)
    deadline = time.monotonic() + timeout
    with socket.create_connection((host, port), timeout=connect_timeout) as sock:
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        sock.sendall(data)
        chunks = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("no complete reply within {:.1f} s".format(timeout))
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_reply:
                raise ValueError("reply larger than {} bytes".format(max_reply))
    obj = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


# ---------------------------------------------------------------------------
# Parallel fan-out (D.3.2)
# ---------------------------------------------------------------------------

#: Phases in which the mutating frame was never sent (straggler applied=False).
UNSENT_PHASES = frozenset({"connect", "xack", "chunks"})
#: Phases in which the frame was sent and the outcome is unknown (applied=None).
SENT_PHASES = frozenset({"command", "verify"})


class FanOutBusy(Exception):
    """Too many abandoned workers; reply ``self.reply`` (D.3.2)."""

    def __init__(self, abandoned, limit):
        super().__init__("{} abandoned fan-out workers (limit {})".format(abandoned, limit))
        self.reply = {"ok": False, "error": "busy", "retry_in_sec": 5}


class FanOutRegistry(object):
    """Running fan-outs' cancel events plus the abandoned-worker gauge.

    The module keeps one process-wide instance (DEFAULT_REGISTRY, used by
    the module-level functions); a coordinator may own its own instance so
    several coordinators in one test process stay independent.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._events = set()
        self._abandoned = 0

    def register(self, cancel_event):
        with self._lock:
            self._events.add(cancel_event)

    def unregister(self, cancel_event):
        with self._lock:
            self._events.discard(cancel_event)

    def cancel_all(self):
        """Set every registered cancel event (leadership change); returns the count."""
        with self._lock:
            events = list(self._events)
        for ev in events:
            ev.set()
        return len(events)

    def running(self):
        with self._lock:
            return len(self._events)

    def abandoned_workers(self):
        with self._lock:
            return self._abandoned

    def _abandon(self):
        with self._lock:
            self._abandoned += 1

    def _release(self, _future=None):
        with self._lock:
            self._abandoned = max(0, self._abandoned - 1)


DEFAULT_REGISTRY = FanOutRegistry()


def register_fanout(cancel_event):
    DEFAULT_REGISTRY.register(cancel_event)


def unregister_fanout(cancel_event):
    DEFAULT_REGISTRY.unregister(cancel_event)


def cancel_all_fanouts():
    return DEFAULT_REGISTRY.cancel_all()


def abandoned_workers():
    """Process-wide gauge of fan-out workers still running after their deadline."""
    return DEFAULT_REGISTRY.abandoned_workers()


class FanOutContext(object):
    """Per-target context handed to a fan-out worker.

    ``phase`` is set by the worker as it progresses (``connect``, ``xack``,
    ``chunks``, ``verify``) so a straggler's result names it.
    Call ``check_before_send(t_cmd)`` immediately before every command
    frame and send only when it returns None. The check itself publishes
    the ``command`` phase *before* testing the guards, so a fan-out that
    times out while a worker sits between a passing check and its send
    reports ``applied:null`` (possibly sent), never ``applied:false``.
    """

    def __init__(self, key, target, deadline_mono, cancel_event, still_valid=None):
        self.key = key
        self.target = target
        self.deadline_mono = deadline_mono
        self.cancel_event = cancel_event
        self._still_valid = still_valid
        self.phase = "connect"

    def remaining(self):
        """Seconds left until the fan-out deadline (may be negative)."""
        return self.deadline_mono - time.monotonic()

    def still_valid(self):
        """The injected guard (e.g. ``role == "leader" and term == request.term``)."""
        return True if self._still_valid is None else bool(self._still_valid())

    def read_timeout(self, t_cmd):
        """Socket timeout for a read after the frame was sent: min(T_cmd, remaining), > 0."""
        return max(0.001, min(float(t_cmd), self.remaining()))

    def check_before_send(self, t_cmd=None, phase=None, next_phase="command"):
        """None when the command frame may be sent, else the not-sent result R.

        *next_phase* (a sent phase, default ``command``) is published before
        any guard is tested. fan_out sets ``cancel_event`` before it reads a
        straggler's phase, so either this check sees the cancel and the frame
        is never sent, or the snapshot already sees *next_phase* and reports
        ``applied:null``. On failure the previous phase is restored and R
        names it (or *phase* when given); R's ``applied`` is False when that
        phase is a never-sent one and None when an earlier frame was sent.
        """
        if next_phase not in SENT_PHASES:
            raise ValueError("next_phase must be one of %s" % sorted(SENT_PHASES))
        old = self.phase
        self.phase = next_phase
        ph = phase or old
        applied = _applied_for_phase(ph)
        r = None
        if not self.still_valid():
            r = {"ok": False, "applied": applied, "error": "leadership_lost", "phase": ph}
        elif self.cancel_event.is_set():
            r = {"ok": False, "applied": applied, "error": "cancelled", "phase": ph}
        elif t_cmd is not None:
            remaining = self.remaining()
            if remaining < float(t_cmd):
                r = {"ok": False, "applied": applied, "reason": "rto_exceeds_deadline",
                     "rto_ms": int(round(float(t_cmd) * 1000)),
                     "remaining_ms": max(0, int(remaining * 1000))}
        if r is not None:
            self.phase = old
        return r


def _applied_for_phase(phase):
    return False if phase in UNSENT_PHASES else None


def _default_key(target):
    if isinstance(target, dict):
        return target["node_key"]
    return target


def fan_out(targets, worker, *, max_workers, deadline_sec, cancel_event=None,
            still_valid=None, key=None, registry=None):
    """Run ``worker(target, ctx)`` for every target in parallel, bounded by a deadline.

    Returns ``{target_key: R}`` in target order. ``key(target)`` defaults to
    ``target["node_key"]`` for dicts and the target itself otherwise.
    Stragglers at the deadline get ``{"ok":False,"applied":A,"error":"timeout",
    "phase":ctx.phase}`` with A False for connect/xack/chunks and None
    otherwise; a worker exception gives ``error:"internal_error"`` the same way.

    At the deadline *cancel_event* (created when not given) is set, queued
    workers are cancelled and the pool is shut down without waiting; running
    stragglers count in the registry's abandoned-worker gauge until they
    finish. Raises FanOutBusy when the gauge already exceeds
    ``2 * max_workers``. The cancel event is registered in *registry*
    (DEFAULT_REGISTRY) while the fan-out runs.
    """
    reg = DEFAULT_REGISTRY if registry is None else registry
    keyf = _default_key if key is None else key
    targets = list(targets)
    keys = [keyf(t) for t in targets]
    if len(set(keys)) != len(keys):
        raise ValueError("fan_out targets have duplicate keys")
    if not targets:
        return {}
    limit = 2 * int(max_workers)
    if reg.abandoned_workers() > limit:
        raise FanOutBusy(reg.abandoned_workers(), limit)
    if cancel_event is None:
        cancel_event = threading.Event()
    deadline = time.monotonic() + float(deadline_sec)
    contexts = [FanOutContext(k, t, deadline, cancel_event, still_valid)
                for k, t in zip(keys, targets)]

    def _run(target, ctx):
        try:
            return worker(target, ctx)
        except Exception as e:  # a worker bug must not lose the other results
            logger.exception("fan-out worker for %s failed", ctx.key)
            return {"ok": False, "applied": _applied_for_phase(ctx.phase),
                    "error": "internal_error", "message": str(e), "phase": ctx.phase}

    reg.register(cancel_event)
    executor = ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(targets))),
                                  thread_name_prefix="fanout")
    try:
        futures = [executor.submit(_run, t, ctx) for t, ctx in zip(targets, contexts)]
        _done, not_done = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        if not_done:
            cancel_event.set()        # before anything else: no worker may send from now on
        results = {}
        for ctx, fut in zip(contexts, futures):
            if fut.done() and not fut.cancelled():
                results[ctx.key] = fut.result()
                continue
            if not fut.cancel():
                reg._abandon()
                fut.add_done_callback(reg._release)
            phase = ctx.phase
            results[ctx.key] = {"ok": False, "applied": _applied_for_phase(phase),
                                "error": "timeout", "phase": phase}
        return results
    finally:
        executor.shutdown(wait=False)
        reg.unregister(cancel_event)
