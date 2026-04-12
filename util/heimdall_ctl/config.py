"""Resolve DAQ configuration and connection parameters."""
import configparser
import os


_SEARCH_PATHS = [
    "daq_chain_config.ini",
    "Firmware/daq_chain_config.ini",
]


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


def resolve_ports(cfg, instance_id=None):
    """Derive control/status/events ports from the config."""
    if instance_id is None:
        instance_id = cfg.getint("federation", "instance_id", fallback=0)
    stride = cfg.getint("federation", "port_stride", fallback=100)
    offset = instance_id * stride
    return {
        "ctl": 5001 + offset,
        "iq": 5000 + offset,
        "status": 5002 + offset,
        "events": 5003 + offset,
    }


def get_num_channels(cfg):
    return cfg.getint("hw", "num_ch", fallback=5)
