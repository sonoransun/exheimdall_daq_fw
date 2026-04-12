"""ZMQ SUB client for the DAQ event bus (port 5003)."""
import json


def subscribe(host="127.0.0.1", port=5003, topics=None):
    """Yield DAQEvent dicts from ZMQ PUB. Blocks until events arrive.

    topics: list of event_type strings to filter, or None for all.
    """
    try:
        import zmq
    except ImportError:
        raise RuntimeError("pyzmq is required for event subscription")

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://{host}:{port}")

    if topics:
        for t in topics:
            sub.setsockopt_string(zmq.SUBSCRIBE, t)
    else:
        sub.setsockopt_string(zmq.SUBSCRIBE, "")

    try:
        while True:
            raw = sub.recv_string()
            parts = raw.split(" ", 1)
            if len(parts) == 2:
                yield json.loads(parts[1])
            else:
                yield {"raw": raw}
    finally:
        sub.close()
        ctx.term()
