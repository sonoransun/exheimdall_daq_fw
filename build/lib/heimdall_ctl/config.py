"""Resolve DAQ configuration and connection parameters.

The config file is only a client-side convenience (``--config``, then
``$HEIMDALL_CONFIG``, then ``./daq_chain_config.ini`` and
``./Firmware/daq_chain_config.ini``); every value it supplies can be given
on the command line instead.
"""
import configparser
import os


_SEARCH_PATHS = [
    "daq_chain_config.ini",
    "Firmware/daq_chain_config.ini",
]

DEFAULT_PORT_STRIDE = 100
DEFAULT_COORDINATOR = ("127.0.0.1", 6000)
DEFAULT_ROUTER_STATS_PORT = 7001


def find_config(explicit_path=None):
    if explicit_path:
        return explicit_path
    env = os.environ.get("HEIMDALL_CONFIG")
    if env and os.path.isfile(env):
        return env
    for p in _SEARCH_PATHS:
        if os.path.isfile(p):
            return p
    return None


def load_config(path):
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg


def _getint(cfg, section, key, fallback):
    if cfg is None:
        return fallback
    try:
        return cfg.getint(section, key, fallback=fallback)
    except ValueError:
        return fallback


def _get(cfg, section, key, fallback=""):
    if cfg is None:
        return fallback
    return cfg.get(section, key, fallback=fallback).strip()


def config_instance_id(cfg):
    return _getint(cfg, "federation", "instance_id", 0)


def config_port_stride(cfg):
    return _getint(cfg, "federation", "port_stride", DEFAULT_PORT_STRIDE)


def resolve_ports(cfg, instance_id=None, port_stride=None):
    """Derive the per-instance ports.

    instance_id: explicit value, else the config's instance_id, else 0.
    port_stride: explicit value, else the config's port_stride, else 100.
    The offset ``instance_id * port_stride`` is always applied, with or
    without a config. Status/events use the configured
    ``[monitoring] status_server_port``/``zmq_pub_port`` (5002/5003)."""
    if instance_id is None:
        instance_id = config_instance_id(cfg)
    stride = port_stride if port_stride is not None else config_port_stride(cfg)
    offset = instance_id * stride
    return {
        "iq": 5000 + offset,
        "ctl": 5001 + offset,
        "status": _getint(cfg, "monitoring", "status_server_port", 5002) + offset,
        "events": _getint(cfg, "monitoring", "zmq_pub_port", 5003) + offset,
        "zmq": 1130 + offset,
    }


def get_num_channels(cfg):
    return _getint(cfg, "hw", "num_ch", 5)


def parse_endpoint(text, default_port=None):
    """'host:port' -> (host, port). A bare host takes default_port."""
    text = text.strip()
    if not text:
        raise ValueError("empty endpoint")
    if text.startswith("["):  # [v6]:port
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
    else:
        host, port = text, ""
    if not port:
        if default_port is None:
            raise ValueError("endpoint needs host:port: {}".format(text))
        return host, int(default_port)
    try:
        port_no = int(port)
    except ValueError:
        raise ValueError("bad port in endpoint: {}".format(text))
    if not 0 < port_no < 65536:
        raise ValueError("port out of range in endpoint: {}".format(text))
    return host, port_no


def parse_endpoint_list(text, default_port=None):
    return [parse_endpoint(p, default_port) for p in text.split(",") if p.strip()]


def coordinator_endpoints(cfg, explicit=None):
    """--coordinator, else $HEIMDALL_COORDINATOR, else [federation]
    coordinator_candidates, else coordinator_host:coordinator_port, else
    127.0.0.1:6000. Returns a list of (host, port)."""
    port = _getint(cfg, "federation", "coordinator_port", DEFAULT_COORDINATOR[1])
    for source in (explicit, os.environ.get("HEIMDALL_COORDINATOR"),
                   _get(cfg, "federation", "coordinator_candidates")):
        if source:
            return parse_endpoint_list(source, default_port=port)
    host = _get(cfg, "federation", "coordinator_host")
    if host:
        return [(host, port)]
    return [DEFAULT_COORDINATOR]


def router_endpoint(cfg, coordinators, explicit=None):
    """--router, else [federation] router_stats_endpoint, else
    <first coordinator host>:7001."""
    for source in (explicit, _get(cfg, "federation", "router_stats_endpoint")):
        if source:
            return parse_endpoint(source)
    return coordinators[0][0], DEFAULT_ROUTER_STATS_PORT


def auth_token(cfg, explicit=None):
    """--auth-token, else $HEIMDALL_AUTH_TOKEN, else [federation] auth_token."""
    if explicit:
        return explicit
    env = os.environ.get("HEIMDALL_AUTH_TOKEN")
    if env:
        return env
    return _get(cfg, "federation", "auth_token") or None


def firmware_dir(cfg_path, explicit=None):
    """--firmware-dir, else $HEIMDALL_FIRMWARE, else the directory of the
    resolved config, else the Firmware/ directory of this source tree."""
    if explicit:
        return explicit
    env = os.environ.get("HEIMDALL_FIRMWARE")
    if env:
        return env
    if cfg_path:
        return os.path.dirname(os.path.abspath(cfg_path))
    tree = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "Firmware"))
    return tree if os.path.isdir(tree) else None


def service_ports(cfg, instance_id=None, port_stride=None):
    """Rows (service, port, note) for the ``ports`` subcommand."""
    ports = resolve_ports(cfg, instance_id, port_stride)
    monitoring = _getint(cfg, "monitoring", "en_monitoring", 0) == 1
    status_on = monitoring and _getint(cfg, "monitoring", "en_status_server", 0) == 1
    events_on = monitoring and _getint(cfg, "monitoring", "en_zmq_pub", 0) == 1
    known = cfg is not None
    rows = [
        ("zmq (rtl_daq control)", ports["zmq"], ""),
        ("iq (iq_server)", ports["iq"], ""),
        ("ctl (hw_controller)", ports["ctl"], ""),
        ("status", ports["status"], "" if not known or status_on else "disabled in config"),
        ("events", ports["events"], "" if not known or events_on else "disabled in config"),
        ("coordinator", _getint(cfg, "federation", "coordinator_port", 6000), "cluster service"),
        ("router", _getint(cfg, "federation", "router_port", 7000), "cluster service"),
        ("router stats", _getint(cfg, "federation", "router_stats_port", 7001), "cluster service"),
    ]
    return rows
