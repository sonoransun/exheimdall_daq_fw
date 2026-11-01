"""ZMQ SUB client for the DAQ event bus (port 5003 + instance offset).

The publisher (``daq_events.ZMQPubHandler``) sends two-frame messages
``[topic, json]``. Frames are received with ``recv_multipart()`` and the
second frame is decoded; a single-frame ``"topic json"`` string is accepted
as a fallback.
"""
import json


def _require_zmq():
    try:
        import zmq
    except ImportError:
        raise RuntimeError("pyzmq is required for event subscription")
    return zmq


def decode_frames(frames):
    """Decode one received message (list of frames) into an event dict."""
    if len(frames) >= 2:
        body = frames[1]
    else:
        text = frames[0].decode("utf-8", errors="replace") if frames else ""
        parts = text.split(" ", 1)
        if len(parts) != 2:
            return {"raw": text}
        body = parts[1]
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        event = json.loads(body)
    except ValueError:
        return {"raw": body if isinstance(body, str) else repr(body)}
    return event if isinstance(event, dict) else {"raw": body}


class EventSubscriber:
    """Subscribe to one or more event endpoints.

    endpoints: list of (host, port) tuples or "host:port" strings.
    topics: list of topic prefixes, or None/empty for every event.
    One SUB socket is connected per endpoint so each event can be attributed
    to the endpoint it came from. Iterating yields (endpoint, event) pairs."""

    def __init__(self, endpoints, topics=None, context=None):
        zmq = _require_zmq()
        self._zmq = zmq
        self._own_ctx = context is None
        self._ctx = context or zmq.Context()
        self._sockets = {}
        self._poller = zmq.Poller()
        for ep in endpoints:
            if isinstance(ep, str):
                host, _, port = ep.rpartition(":")
                ep = (host, int(port))
            label = "{}:{}".format(ep[0], ep[1])
            sock = self._ctx.socket(zmq.SUB)
            sock.setsockopt(zmq.LINGER, 0)
            for topic in (topics or [""]):
                sock.setsockopt_string(zmq.SUBSCRIBE, topic)
            sock.connect("tcp://{}:{}".format(ep[0], ep[1]))
            self._sockets[sock] = label
            self._poller.register(sock, zmq.POLLIN)

    @property
    def endpoints(self):
        return list(self._sockets.values())

    def recv(self, timeout_ms=None):
        """Return a list of (endpoint, event) received within timeout_ms
        (None blocks until at least one event arrives)."""
        out = []
        for sock, _ in self._poller.poll(timeout_ms):
            while True:
                try:
                    frames = sock.recv_multipart(flags=self._zmq.NOBLOCK)
                except self._zmq.Again:
                    break
                out.append((self._sockets[sock], decode_frames(frames)))
        return out

    def __iter__(self):
        while True:
            for item in self.recv(None):
                yield item

    def close(self):
        for sock in list(self._sockets):
            try:
                self._poller.unregister(sock)
            except KeyError:
                pass
            sock.close()
        self._sockets = {}
        if self._own_ctx:
            self._ctx.term()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def subscribe(host="127.0.0.1", port=5003, topics=None):
    """Yield DAQEvent dicts from ZMQ PUB. Blocks until events arrive.

    topics: list of event_type strings to filter, or None for all.
    Thin wrapper over EventSubscriber kept for library users.
    """
    sub = EventSubscriber([(host, port)], topics)
    try:
        for _, event in sub:
            yield event
    finally:
        sub.close()
