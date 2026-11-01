"""
   Per-instance resource model of the HeIMDALL DAQ chain (spec I.1) and the
   host-capacity evaluation behind daq_preflight.py (spec I.2).

   Pure module: no file, socket or /proc access. daq_preflight.py does the
   probing (host, registry, INI files) and hands plain dicts to evaluate().

   Notation (spec 0.1): M = [hw] num_ch, fs = [daq] sample_rate,
   B = [daq] daq_buffer_size, Ncpi = [pre_processing] cpi_size (after
   decimation), R = decimation_ratio, Nc = [calibration] corr_size.

   frame_sizes() mirrors the size_t helpers of _daq_core/frame_sizes.h key for
   key (asserted by test_frame_sizes.py):

       iq_frame                   hd_iq_frame_bytes(M, Ncpi, Nc)
       worst_frame                hd_worst_frame_bytes(M)
       decimator_in_object        hd_decimator_in_object_bytes(M, Ncpi, R, Nc)
       decimator_in_consumer_map  hd_decimator_in_consumer_map_bytes(M, Ncpi, R, Nc)
       decimator_out_object       worst_frame, or iq_frame with
                                  [pre_processing] size_segments_from_config = 1
       decimator_out_resident     iq_frame
       delay_sync_iq              iq_frame (delay_sync.py out_shmem_size)

   All values are per segment; every shared-memory channel is an A/B pair,
   which instance_model() applies (x2).

   Project: HeIMDALL DAQ Firmware
   License: GNU GPL V3
"""
import math
import posixpath
import socket

# ---------------------------------------------------------------------------
# Constants (C counterparts in brackets)
# ---------------------------------------------------------------------------

MIB = 1 << 20
IQ_HEADER_BYTES = 1024                 # IQ_HEADER_LENGTH / HD_IQ_HEADER_BYTES
MAX_FRAME_SAMPLES = 1 << 23            # MAX_IQFRAME_PAYLOAD_SIZE / HD_MAX_FRAME_SAMPLES
CF32_SAMPLE_BYTES = 8                  # HD_CF32_SAMPLE_BYTES
CU8_SAMPLE_BYTES = 2                   # HD_CU8_SAMPLE_BYTES
MAX_NUM_CH = 31                        # 128-byte :1130 message / shm sizing limit (I.5)

#: RT tiers of daq_start_sm.sh before [host] rt_priority_offset is subtracted
RT_TIERS = (("rtl_daq", 99), ("rebuffer", 94), ("decimate", 92), ("delay_sync", 90),
            ("iq_server", 88), ("hw_controller", 82))
CORES_PER_HW_INSTANCE = 4

DEFAULT_USB2_BUS_BUDGET_MBPS = 35.0    # usable MB/s of one USB 2.0 high-speed bus
HIGH_SPEED_MBPS = 480
MEM_HEADROOM_BYTES = 512 * MIB         # OS / Python interpreters / page cache headroom
FRAME_PERIOD_WARN_SEC = 8.0            # control timeouts (D.6.1)
EGRESS_WARN_FRACTION = 0.70            # warn from 70 % of a NIC's line rate
MEMORY_WARN_FRACTION = 0.80            # warn from 80 % of MemAvailable

DEFAULT_STATUS_PORT, DEFAULT_ZMQ_PUB_PORT = 5002, 5003
BASE_PORT_IQ, BASE_PORT_CTL, BASE_PORT_ZMQ = 5000, 5001, 1130
DEFAULT_PORT_STRIDE = 100
DEFAULT_COORDINATOR_PORT = 6000
DEFAULT_ROUTER_PORT, DEFAULT_ROUTER_STATS_PORT = 7000, 7001
DEFAULT_SERIAL_BASE = 1000
SERIAL_UNIT_STRIDE = 100               # unit k is programmed to 1000 + 100*k + i (A.1)

#: Router defaults (spec E.1)
ROUTER_DEFAULTS = {"queue_frames": 4, "max_clients": 8, "align_max_frames": 2}

#: Values of a default kraken5 instance; used for live registry entries whose
#: configuration cannot be read (only cores/ports/serials are known then).
DEFAULT_PARAMS = {"num_ch": 5, "sample_rate": 2400000, "daq_buffer_size": 1 << 18,
                  "cpi_size": 1 << 20, "decimation_ratio": 1, "corr_size": 1 << 16,
                  "usb_ring_buffers": 8, "usb_async_transfers": 12}

VERDICTS = ("ok", "warn", "fail", "skip")
_RANK = {"skip": 0, "ok": 0, "warn": 1, "fail": 2}

FEDERATION_ROUTER_PREFIX = "router_"


class ConfigError(ValueError):
    """A configuration value the model cannot use (message names the key)."""


# ---------------------------------------------------------------------------
# Size formulas (spec I.1)
# ---------------------------------------------------------------------------

def _pos_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("{0} must be a positive integer, got {1!r}".format(name, value))
    return value


def frame_sizes(M, Ncpi, R, Nc, B=None, size_segments_from_config=False):
    """Per-segment shared-memory sizes in bytes (keys listed in the module doc).

    *B* (daq_buffer_size) is accepted for the spec's signature; no segment
    depends on it, it is only validated when given.
    """
    M, Ncpi, R, Nc = (_pos_int(n, v) for n, v in (("M", M), ("Ncpi", Ncpi), ("R", R), ("Nc", Nc)))
    if B is not None:
        _pos_int("B", B)
    iq_frame = IQ_HEADER_BYTES + max(Ncpi, Nc) * M * CF32_SAMPLE_BYTES
    worst = IQ_HEADER_BYTES + MAX_FRAME_SAMPLES * M * CF32_SAMPLE_BYTES
    din_samples = max(Ncpi * R, Nc)
    return {
        "iq_frame": iq_frame,
        "worst_frame": worst,
        "decimator_in_object": IQ_HEADER_BYTES + din_samples * M * CU8_SAMPLE_BYTES,
        "decimator_in_consumer_map": IQ_HEADER_BYTES + din_samples * M * CF32_SAMPLE_BYTES,
        "decimator_out_object": iq_frame if size_segments_from_config else worst,
        "decimator_out_resident": iq_frame,
        "delay_sync_iq": iq_frame,
    }


def rebuffer_ring_bytes(M, Ncpi, R, Nc, B):
    """rebuffer.out circular buffers: M * max(Ncpi*R/B + 2, Nc/B + 2) * 2B
    (C integer division, rebuffer.c)."""
    buffers = max(Ncpi * R // B + 2, Nc // B + 2)
    return M * buffers * B * 2


def frame_period_sec(Ncpi, R, fs):
    """T_fp = Ncpi * R / fs (rebuffer.c)."""
    return Ncpi * R / float(fs)


def rt_priorities(offset=0):
    """{stage: SCHED_FIFO priority} with [host] rt_priority_offset applied."""
    return {name: prio - int(offset) for name, prio in RT_TIERS}


def default_cores(instance_id, nproc=None):
    """Today's per-instance core layout of daq_start_sm.sh: (N*4 + k) % nproc."""
    base = int(instance_id) * CORES_PER_HW_INSTANCE
    cores = [base + k for k in range(CORES_PER_HW_INSTANCE)]
    if nproc:
        cores = [c % int(nproc) for c in cores]
    return cores


def router_memory_bound(frame_bytes, sources, queue_frames=4, max_clients=8, align_max_frames=2):
    """Worst-case router memory (spec E.2): frame_bytes * (queue_frames *
    max_clients + align_max_frames * sources + sources)."""
    return int(frame_bytes) * (int(queue_frames) * int(max_clients)
                               + int(align_max_frames) * int(sources) + int(sources))


def usb_bus_budget_MBps(speed_mbps, usb2_bus_budget_MBps=DEFAULT_USB2_BUS_BUDGET_MBPS):
    """Usable RTL-SDR ingress of one bus. RTL2832U receivers are high-speed
    devices, so a bus at or above 480 Mbit/s offers the USB 2.0 budget; a
    slower bus offers the proportional share."""
    if speed_mbps >= HIGH_SPEED_MBPS:
        return float(usb2_bus_budget_MBps)
    return float(usb2_bus_budget_MBps) * float(speed_mbps) / HIGH_SPEED_MBPS


# ---------------------------------------------------------------------------
# INI parameters -> instance description
# ---------------------------------------------------------------------------

def _sec(params, name):
    sec = params.get(name) if isinstance(params, dict) else None
    return sec if isinstance(sec, dict) else {}


def _raw(params, section, key):
    v = _sec(params, section).get(key)
    return None if v is None else str(v).strip()


def _int(params, section, key, default=None, minimum=None):
    v = _raw(params, section, key)
    if v is None or v == "":
        if default is None:
            raise ConfigError("missing [{0}] {1}".format(section, key))
        return default
    try:
        n = int(v)
    except ValueError:
        try:
            f = float(v)
        except ValueError:
            raise ConfigError("[{0}] {1} = {2!r} is not a number".format(section, key, v))
        if not math.isfinite(f) or f != int(f):
            raise ConfigError("[{0}] {1} = {2!r} is not an integer".format(section, key, v))
        n = int(f)
    if minimum is not None and n < minimum:
        raise ConfigError("[{0}] {1} = {2} is below {3}".format(section, key, n, minimum))
    return n


def _flag(params, section, key):
    return _raw(params, section, key) == "1"


def _int_list(text):
    items = [x.strip() for x in text.split(",") if x.strip()]
    if not items or not all(x.isdigit() for x in items):
        return None
    return [int(x) for x in items]


def is_federation_ini(params):
    """A federation.ini has a [federation] section and no [hw] section."""
    return "federation" in params and "hw" not in params


def enabled_ports(instance_id, port_stride, en_status=False, status_port=DEFAULT_STATUS_PORT,
                  en_events=False, events_port=DEFAULT_ZMQ_PUB_PORT):
    """{iq, ctl, status, events, zmq} of an instance (spec I.3); status/events
    are None unless enabled."""
    off = int(instance_id) * int(port_stride)
    return {"iq": BASE_PORT_IQ + off, "ctl": BASE_PORT_CTL + off,
            "status": status_port + off if en_status else None,
            "events": events_port + off if en_events else None,
            "zmq": BASE_PORT_ZMQ + off}


def resolve_serials(params, num_ch):
    """Serials rtl_daq opens (A.1): a non-empty device_serials wins, else
    str(serial_base + i) with serial_base defaulting to 1000."""
    listed = [x.strip() for x in (_raw(params, "hw", "device_serials") or "").split(",") if x.strip()]
    if listed:
        return listed
    base = _int(params, "hw", "serial_base", DEFAULT_SERIAL_BASE, minimum=0)
    return [str(base + i) for i in range(num_ch)]


def _default_is_local(host):
    h = (host or "").strip().lower().strip("[]")
    if h in ("localhost", "127.0.0.1", "::1", "localhost.localdomain") or h.startswith("127."):
        return True
    name = socket.gethostname().lower()
    return bool(h) and (h == name or h == name.split(".")[0])


def _local_is_host():
    try:
        from federation_common import is_local_host  # noqa: WPS433 (soft dependency, WP-02)
        return is_local_host
    except ImportError:
        return _default_is_local


def service_ports(params, federation_file=False, is_local=None):
    """[(name, port)] federation service ports on this host (spec I.2/I.3).

    In a federation.ini the coordinator port always counts; in an instance
    INI only when coordinator_host or a coordinator_candidates entry names
    this host. router_port/router_stats_port count when any router_* key is
    present (router_stats_port 0 = off).
    """
    fed = _sec(params, "federation")
    is_local = is_local or _local_is_host()
    out = []
    ini_port = _int(params, "federation", "coordinator_port", DEFAULT_COORDINATOR_PORT, minimum=0)
    coord_ports = [ini_port] if federation_file else []
    if not federation_file:
        host = _raw(params, "federation", "coordinator_host") or ""
        if host and is_local(host):
            coord_ports.append(ini_port)
        for item in (_raw(params, "federation", "coordinator_candidates") or "").split(","):
            item = item.strip()
            if not item:
                continue
            host, sep, port_text = item.rpartition(":")
            if not sep:
                host, port_text = item, ""
            if not is_local(host):
                continue
            if not port_text:
                coord_ports.append(ini_port)
            elif port_text.isdigit():
                coord_ports.append(int(port_text))
            else:
                raise ConfigError("[federation] coordinator_candidates entry {0!r} has a "
                                  "non-numeric port".format(item))
    for port in sorted(set(coord_ports)):
        out.append(("coordinator", port))
    if any(k.startswith(FEDERATION_ROUTER_PREFIX) for k in fed):
        out.append(("router", _int(params, "federation", "router_port", DEFAULT_ROUTER_PORT, minimum=0)))
        stats = _int(params, "federation", "router_stats_port", DEFAULT_ROUTER_STATS_PORT, minimum=0)
        if stats:
            out.append(("router_stats", stats))
    return out


def instance_from_params(params, config=None, mode="hw", nproc=None, is_local=None):
    """Instance description (plain dict) from INI parameters
    ({section: {key: value}}). Raises ConfigError for unusable values."""
    if mode not in ("hw", "synthetic"):
        raise ValueError("mode must be hw or synthetic")
    M = _int(params, "hw", "num_ch", minimum=1)
    sample_rate = _int(params, "daq", "sample_rate", minimum=1)
    B = _int(params, "daq", "daq_buffer_size", minimum=1)
    Ncpi = _int(params, "pre_processing", "cpi_size", minimum=1)
    R = _int(params, "pre_processing", "decimation_ratio", minimum=1)
    Nc = _int(params, "calibration", "corr_size", minimum=1)
    iid = _int(params, "federation", "instance_id", 0, minimum=0)
    stride = _int(params, "federation", "port_stride", DEFAULT_PORT_STRIDE, minimum=1)
    en_mon = _flag(params, "monitoring", "en_monitoring")
    ports = enabled_ports(
        iid, stride,
        en_status=en_mon and _flag(params, "monitoring", "en_status_server"),
        status_port=_int(params, "monitoring", "status_server_port", DEFAULT_STATUS_PORT, minimum=0),
        en_events=en_mon and _flag(params, "monitoring", "en_zmq_pub"),
        events_port=_int(params, "monitoring", "zmq_pub_port", DEFAULT_ZMQ_PUB_PORT, minimum=0))
    cores = _int_list(_raw(params, "host", "cpu_cores") or "")
    explicit_cores = cores is not None and len(cores) == CORES_PER_HW_INSTANCE \
        and len(set(cores)) == CORES_PER_HW_INSTANCE
    if not explicit_cores:
        cores = default_cores(iid, nproc)
    ds_cores = _int_list(_raw(params, "host", "delay_sync_cores") or "") or []
    sharing = _raw(params, "host", "allow_core_sharing") or "warn"
    if sharing not in ("warn", "refuse", "allow"):
        sharing = "warn"
    offset = _int(params, "host", "rt_priority_offset", 0, minimum=0)
    iface = _raw(params, "data_interface", "out_data_iface_type") or "eth"
    return {
        "config": config, "source": "config", "instance_id": iid, "port_stride": stride,
        "mode": mode, "ports": ports,
        "service_ports": service_ports(params, False, is_local),
        "cores": cores, "explicit_cores": explicit_cores, "delay_sync_cores": ds_cores,
        "allow_core_sharing": sharing, "rt_priority_offset": offset,
        "serials": resolve_serials(params, M) if mode == "hw" else [],
        "out_data_iface_type": iface,
        "iq_server_max_clients": _int(params, "data_interface", "iq_server_max_clients", 1, minimum=1),
        "num_ch": M, "sample_rate": sample_rate, "daq_buffer_size": B, "cpi_size": Ncpi,
        "decimation_ratio": R, "corr_size": Nc,
        "usb_ring_buffers": _int(params, "daq", "usb_ring_buffers", 8, minimum=1),
        "usb_async_transfers": _int(params, "daq", "usb_async_transfers", 12, minimum=1),
        "size_segments_from_config": _flag(params, "pre_processing", "size_segments_from_config"),
        "params_known": True,
    }


def instance_from_registry(entry, params=None, nproc=None, is_local=None):
    """Instance description of a live registry entry (B.3.1). Cores, ports,
    serials and mode come from the entry (what is actually running); the
    sizes from *params* (its INI) when given, else DEFAULT_PARAMS with
    num_ch = len(serials)."""
    mode = entry.get("mode") if entry.get("mode") in ("hw", "synthetic") else "hw"
    serials = [str(s) for s in entry.get("serials") or []]
    if params is not None:
        cwd = entry.get("cwd")
        config = posixpath.join(cwd, "daq_chain_config.ini") if isinstance(cwd, str) else None
        inst = instance_from_params(params, config, mode, nproc, is_local)
    else:
        d = dict(DEFAULT_PARAMS)
        if serials:
            d["num_ch"] = len(serials)
        iid = int(entry.get("instance_id", 0))
        inst = {"config": None, "source": "live", "instance_id": iid,
                "port_stride": DEFAULT_PORT_STRIDE, "mode": mode,
                "ports": enabled_ports(iid, DEFAULT_PORT_STRIDE), "service_ports": [],
                "cores": default_cores(iid, nproc), "explicit_cores": False,
                "delay_sync_cores": [], "allow_core_sharing": "warn",
                "rt_priority_offset": 0, "serials": serials, "out_data_iface_type": "eth",
                "iq_server_max_clients": 1, "size_segments_from_config": False,
                "params_known": False}
        inst.update(d)
    inst["source"] = "live"
    inst["instance_id"] = int(entry.get("instance_id", inst["instance_id"]))
    if isinstance(entry.get("ports"), dict):
        inst["ports"] = {k: entry["ports"].get(k) for k in ("iq", "ctl", "status", "events", "zmq")}
    if isinstance(entry.get("cores"), list) and entry["cores"]:
        inst["cores"] = [int(c) for c in entry["cores"]]
        inst["explicit_cores"] = True
    elif mode == "synthetic" and entry.get("cores") == []:
        # daq_synthetic_start.sh without --rt pins nothing (CORES_USED="")
        inst["cores"] = []
        inst["delay_sync_cores"] = []
        inst["explicit_cores"] = True
    if "serials" in entry:
        inst["serials"] = serials
    if "rt_priority_offset" in entry:
        inst["rt_priority_offset"] = int(entry["rt_priority_offset"])
    return inst


def planned_instance(template, instance_id, nproc=None):
    """A hypothetical instance for --add-instances: a copy of *template* with
    the provisioning defaults of util/provision_instance.py for id N
    (serial_base = 1000 + 100*N, cpu_cores = 4N..4N+3, ports + N*stride)."""
    inst = dict(template)
    stride = template["port_stride"]
    old = template["ports"]
    off = (instance_id - template["instance_id"]) * stride
    inst["ports"] = {k: (None if v is None else v + off) for k, v in old.items()}
    inst["instance_id"] = instance_id
    inst["source"] = "planned"
    inst["config"] = None
    inst["cores"] = [CORES_PER_HW_INSTANCE * instance_id + k for k in range(CORES_PER_HW_INSTANCE)]
    inst["explicit_cores"] = True
    inst["delay_sync_cores"] = []
    inst["service_ports"] = []
    base = DEFAULT_SERIAL_BASE + SERIAL_UNIT_STRIDE * instance_id
    inst["serials"] = [str(base + i) for i in range(template["num_ch"])] if template["mode"] == "hw" else []
    return inst


def federation_from_params(params, path=None, is_local=None):
    """Federation.ini description: its service ports and router bound inputs."""
    fed = _sec(params, "federation")
    has_router = any(k.startswith(FEDERATION_ROUTER_PREFIX) for k in fed)
    sources = _router_source_count(params)
    return {
        "config": path, "service_ports": service_ports(params, True, is_local),
        "has_router": has_router, "router_sources": sources,
        "router_queue_frames": _int(params, "federation", "router_queue_frames",
                                    ROUTER_DEFAULTS["queue_frames"], minimum=1),
        "router_max_clients": _int(params, "federation", "router_max_clients",
                                   ROUTER_DEFAULTS["max_clients"], minimum=1),
        "router_align_max_frames": _int(params, "federation", "router_align_max_frames",
                                        ROUTER_DEFAULTS["align_max_frames"], minimum=1),
    }


def _router_source_count(params):
    """router_sources > cluster_members > peer_list + self (spec E.1)."""
    for key, extra in (("router_sources", 0), ("cluster_members", 0), ("peer_list", 1)):
        items = [x for x in (_raw(params, "federation", key) or "").split(",") if x.strip()]
        if items:
            return len(items) + extra
    return 1


# ---------------------------------------------------------------------------
# Per-instance resource model (spec I.1 table)
# ---------------------------------------------------------------------------

def instance_model(inst):
    """The I.1 quantities of one instance (bytes, bytes/s, seconds)."""
    M, fs, B = inst["num_ch"], inst["sample_rate"], inst["daq_buffer_size"]
    Ncpi, R, Nc = inst["cpi_size"], inst["decimation_ratio"], inst["corr_size"]
    sz = frame_sizes(M, Ncpi, R, Nc, B, inst["size_segments_from_config"])
    hw = inst["mode"] == "hw"
    usb_ring = M * inst["usb_ring_buffers"] * 2 * B if hw else 0
    usb_xfer = M * inst["usb_async_transfers"] * 2 * B if hw else 0
    reb_ring = rebuffer_ring_bytes(M, Ncpi, R, Nc, B)
    din = 2 * sz["decimator_in_object"]
    dout_obj = 2 * sz["decimator_out_object"]
    dout_res = 2 * sz["decimator_out_resident"]
    dsiq = 2 * sz["delay_sync_iq"]
    eth = inst["out_data_iface_type"] == "eth"
    clients = inst["iq_server_max_clients"] if eth else 0
    per_process_locked = {
        "rtl_daq": usb_ring + usb_xfer,           # mlockall(MCL_CURRENT | MCL_FUTURE)
        "rebuffer": reb_ring + din,               # MCL_ONFAULT: ring + written segments
        "decimate": din + dout_res,               # MCL_ONFAULT: touched input + output
        "iq_server": dsiq if eth else 0,          # MCL_ONFAULT: frames it sends
    }
    locked = usb_ring + usb_xfer + reb_ring
    shm_resident = din + dout_res + dsiq
    copy = 2 * Ncpi * M * CF32_SAMPLE_BYTES
    return {
        "frame_sizes": sz,
        "usb_ingress_Bps": M * fs * 2 if hw else 0,
        "usb_transfer_bytes": usb_xfer,
        "usb_ring_bytes": usb_ring,
        "rebuffer_ring_bytes": reb_ring,
        "decimator_in_bytes": din,
        "decimator_in_consumer_map_bytes": 2 * sz["decimator_in_consumer_map"],
        "decimator_out_object_bytes": dout_obj,
        "decimator_out_resident_bytes": dout_res,
        "delay_sync_iq_bytes": dsiq,
        "delay_sync_copy_bytes": copy,
        "shm_object_bytes": din + dout_obj + dsiq,
        "shm_resident_bytes": shm_resident,
        "locked_bytes": locked,
        "resident_bytes": shm_resident + copy,
        "memory_need_bytes": locked + shm_resident + copy,
        "per_process_locked_bytes": per_process_locked,
        "frame_period_sec": frame_period_sec(Ncpi, R, fs),
        "egress_per_client_Bps": M * fs * CF32_SAMPLE_BYTES / float(R),
        "egress_clients": clients,
        "egress_Bps": M * fs * CF32_SAMPLE_BYTES / float(R) * clients,
        "cores": list(inst["cores"]),
        "rt_priorities": rt_priorities(inst["rt_priority_offset"]),
    }


def limit_violations(inst):
    """Texts of the hard limits an instance breaks (I.3/I.5)."""
    out = []
    if inst["num_ch"] > MAX_NUM_CH:
        out.append("instance {0}: num_ch {1} > {2}".format(inst["instance_id"], inst["num_ch"], MAX_NUM_CH))
    for key in ("cpi_size", "corr_size"):
        if inst[key] > MAX_FRAME_SAMPLES:
            out.append("instance {0}: {1} {2} > 2^23".format(inst["instance_id"], key, inst[key]))
    return out


# ---------------------------------------------------------------------------
# Evaluation (spec I.2)
# ---------------------------------------------------------------------------

def _res(need, have, unit, verdict, note=""):
    assert verdict in VERDICTS
    return {"need": need, "have": have, "unit": unit, "verdict": verdict, "note": note}


def _skip(unit, note, need=None):
    return _res(need, None, unit, "skip", note)


def _worst(verdicts):
    worst = "ok"
    for v in verdicts:
        if _RANK[v] > _RANK[worst]:
            worst = v
    return worst


def _fmt_ids(ids):
    return ",".join(str(i) for i in sorted(ids))


def _check_cores(insts, host):
    nproc = host.get("nproc")
    hw = [i for i in insts if i["mode"] == "hw"]
    need = CORES_PER_HW_INSTANCE * len(hw)
    if nproc is None:
        return _skip("cores", "nproc not probed", need)
    union = sorted({c for i in insts for c in i["cores"] + i["delay_sync_cores"]})
    notes, verdicts = [], ["ok"]
    if need > nproc:
        planned = [i for i in hw if i["source"] != "live"]
        allowed = bool(planned) and all(i["allow_core_sharing"] == "allow" for i in planned)
        verdicts.append("warn" if allowed else "fail")
        notes.append("{0} hardware instances need {1} cores ({2} per instance){3}".format(
            len(hw), need, CORES_PER_HW_INSTANCE,
            "; sharing allowed by [host] allow_core_sharing = allow" if allowed else ""))
    beyond = [c for c in union if c >= nproc]
    if beyond:
        verdicts.append("fail")
        notes.append("cores {0} do not exist (nproc {1})".format(_fmt_ids(beyond), nproc))
    notes.append("cores in use: {0}".format(_fmt_ids(union) if union else "none"))
    return _res(need, nproc, "cores", _worst(verdicts), "; ".join(notes))


def _check_core_overlap(insts):
    owners = {}
    for i in insts:
        for c in set(i["cores"] + i["delay_sync_cores"]):
            owners.setdefault(c, []).append(i)
    shared = {c: o for c, o in owners.items() if len(o) > 1}
    if not shared:
        return _res(0, 0, "cores", "ok")
    verdicts, notes = [], []
    for c in sorted(shared):
        policies = [i["allow_core_sharing"] for i in shared[c] if i["source"] != "live"] or ["warn"]
        if "refuse" in policies:
            verdicts.append("fail")
        elif all(p == "allow" for p in policies):
            verdicts.append("ok")
        else:
            verdicts.append("warn")
        notes.append("core {0}: instances {1}".format(c, _fmt_ids(i["instance_id"] for i in shared[c])))
    return _res(len(shared), 0, "cores", _worst(verdicts), "; ".join(notes))


def _check_ports(insts, services):
    used = {}
    problems = []
    for i in insts:
        for name, port in sorted(i["ports"].items()):
            if port is None:
                continue
            if port > 65535:
                problems.append("{0} port {1} of instance {2} is above 65535".format(name, port, i["instance_id"]))
            used.setdefault(port, []).append("instance {0} {1}".format(i["instance_id"], name))
    for origin, name, port in services:
        used.setdefault(port, []).append("{0} port ({1})".format(name, origin))
    for port in sorted(used):
        owners = list(dict.fromkeys(used[port]))
        if len(used[port]) > 1:
            problems.append("port {0}: {1}".format(port, ", ".join(owners) if len(owners) > 1
                                                    else owners[0] + " (twice)"))
    return _res(len(problems), 0, "collisions", "fail" if problems else "ok", "; ".join(problems))


def _check_serials(insts):
    owners = {}
    for i in insts:
        for s in i["serials"]:
            owners.setdefault(s, []).append(i["instance_id"])
    clash = {s: o for s, o in owners.items() if len(o) > 1}
    if not clash:
        return _res(0, 0, "collisions", "ok")
    note = "; ".join("serial {0}: instances {1}".format(s, _fmt_ids(o)) for s, o in sorted(clash.items()))
    return _res(len(clash), 0, "collisions", "fail", note)


def _check_usb(insts, host, budget):
    out = {}
    hw = [i for i in insts if i["mode"] == "hw"]
    if not hw:
        out["usb_bus"] = _skip("MB/s", "no hardware instance")
        return out
    buses = host.get("usb_buses")
    if buses is None:
        out["usb_bus"] = _skip("MB/s", "USB buses not probed")
        return out
    where = {}
    for b in buses:
        for d in b.get("devices") or []:
            if d.get("serial") is not None:
                where[str(d["serial"])] = b
    load, users, unplaced = {}, {}, []
    for i in hw:
        per_ch = i["sample_rate"] * 2 / 1e6
        missing = []
        for s in i["serials"]:
            b = where.get(s)
            if b is None:
                missing.append(s)
                continue
            load[b["bus"]] = load.get(b["bus"], 0.0) + per_ch
            users.setdefault(b["bus"], set()).add(i["instance_id"])
        if missing:
            unplaced.append((i, missing, per_ch * len(missing)))
    for b in sorted(buses, key=lambda x: x["bus"]):
        if b["bus"] not in load:
            continue
        need = round(load[b["bus"]], 3)
        have = round(usb_bus_budget_MBps(b.get("speed_mbps", HIGH_SPEED_MBPS), budget), 3)
        verdict = "fail" if need > have else "ok"
        note = "instances {0}".format(_fmt_ids(users[b["bus"]]))
        if verdict == "fail":
            note += "; more receivers than one bus carries - move a unit to another controller"
        out["usb_bus:{0}".format(b["bus"])] = _res(need, have, "MB/s", verdict, note)
    if unplaced:
        need = round(sum(x[2] for x in unplaced), 3)
        note = "; ".join("instance {0}: serials {1} not attached".format(i["instance_id"], ",".join(m))
                         for i, m, _ in unplaced)
        out["usb_unplaced"] = _res(need, 0.0, "MB/s", "warn", note)
    if not out:
        out["usb_bus"] = _skip("MB/s", "no receiver serial matched")
    return out


def _check_egress(insts, host):
    eth = [i for i in insts if i["model"]["egress_clients"] > 0]
    if not eth:
        return {"egress": _skip("MB/s", "no instance uses eth output")}
    nics = host.get("nics")
    if nics is None:
        return {"egress": _skip("MB/s", "NICs not probed")}
    need = round(sum(i["model"]["egress_Bps"] for i in eth) / 1e6, 3)
    clients = sum(i["model"]["egress_clients"] for i in eth)
    links = [n for n in nics if n.get("speed_mbps") and n["speed_mbps"] > 0 and n.get("name") != "lo"]
    if not links:
        return {"egress": _skip("MB/s", "no NIC with a known link speed")}
    # The IQ stream leaves through one interface, which pre-flight cannot
    # identify. Only the fastest link(s) are judged: a slower interface (a VPN
    # tun at 10 Mbit/s, a spare port) need not be the data path.
    fastest = max(n["speed_mbps"] for n in links)
    best = [n["name"] for n in links if n["speed_mbps"] == fastest]
    out = {}
    for n in links:
        have = round(n["speed_mbps"] / 8.0, 3)
        frac = need / have
        verdict = "fail" if frac > 1.0 else ("warn" if frac >= EGRESS_WARN_FRACTION else "ok")
        note = "{0:.0f} % of link ({1} client{2} at cf32)".format(100.0 * frac, clients,
                                                                 "" if clients == 1 else "s")
        if n["speed_mbps"] < fastest and verdict != "ok":
            verdict = "skip"
            note += "; not judged: {0} is faster".format(", ".join(best))
        out["egress:{0}".format(n["name"])] = _res(need, have, "MB/s", verdict, note)
    return out


def _check_memory(insts, host):
    out = {}
    new = [i for i in insts if i["source"] != "live"]
    mem_need = sum(i["model"]["memory_need_bytes"] for i in new) + MEM_HEADROOM_BYTES
    have = host.get("mem_available")
    note = "locked + resident of the new instances + {0} MiB headroom".format(MEM_HEADROOM_BYTES // MIB)
    if have is None:
        out["mem_available"] = _skip("bytes", "MemAvailable not probed", mem_need)
    else:
        verdict = "fail" if mem_need > have else ("warn" if mem_need > MEMORY_WARN_FRACTION * have else "ok")
        out["mem_available"] = _res(mem_need, have, "bytes", verdict, note)
    shm_need = sum(i["model"]["shm_resident_bytes"] for i in new)
    have = host.get("shm_free")
    if have is None:
        out["shm_free"] = _skip("bytes", "/dev/shm not probed", shm_need)
    else:
        out["shm_free"] = _res(shm_need, have, "bytes", "fail" if shm_need > have else "ok",
                               "resident shared memory of the new instances")
    obj_need = sum(i["model"]["shm_object_bytes"] for i in insts)
    have = host.get("shm_size")
    if have is None:
        out["shm_size"] = _skip("bytes", "/dev/shm not probed", obj_need)
    elif obj_need > have:
        out["shm_size"] = _res(obj_need, have, "bytes", "warn",
                               "object sizes exceed /dev/shm (only resident pages are allocated; "
                               "[pre_processing] size_segments_from_config = 1 shrinks decimator_out)")
    else:
        out["shm_size"] = _res(obj_need, have, "bytes", "ok", "object sizes")
    lock_need = max([max(i["model"]["per_process_locked_bytes"].values()) for i in new] or [0])
    have = host.get("memlock")
    if have is None:
        out["memlock"] = _skip("bytes", "RLIMIT_MEMLOCK not probed", lock_need)
    elif have < 0:
        out["memlock"] = _res(lock_need, have, "bytes", "ok", "unlimited")
    else:
        out["memlock"] = _res(lock_need, have, "bytes", "warn" if lock_need > have else "ok",
                              "largest per-process locked set (RLIMIT_MEMLOCK is per process; "
                              "root is exempt)")
    return out


def _check_frame_period(insts):
    if not insts:
        return _skip("s", "no instance")
    need = round(max(i["model"]["frame_period_sec"] for i in insts), 3)
    return _res(need, FRAME_PERIOD_WARN_SEC, "s", "warn" if need > FRAME_PERIOD_WARN_SEC else "ok",
                "warn above {0:g} s".format(FRAME_PERIOD_WARN_SEC))


def _check_router(insts, feds, host):
    feds = [f for f in feds if f["has_router"]]
    if not feds:
        return _skip("bytes", "no federation.ini with router keys")
    known = [i for i in insts if i["params_known"]]
    if known:
        frame = max(i["model"]["frame_sizes"]["iq_frame"] for i in known)
        src_note = "largest instance frame"
    else:
        d = DEFAULT_PARAMS
        frame = frame_sizes(d["num_ch"], d["cpi_size"], d["decimation_ratio"], d["corr_size"])["iq_frame"]
        src_note = "default 5-channel frame"
    need = sum(router_memory_bound(frame, f["router_sources"], f["router_queue_frames"],
                                   f["router_max_clients"], f["router_align_max_frames"]) for f in feds)
    have = host.get("mem_available")
    if have is None:
        return _skip("bytes", "MemAvailable not probed", need)
    verdict = "fail" if need > have else ("warn" if need > MEMORY_WARN_FRACTION * have else "ok")
    return _res(need, have, "bytes", verdict, "worst case ({0})".format(src_note))


def _check_limits(insts):
    problems = [p for i in insts for p in limit_violations(i)]
    return _res(len(problems), 0, "violations", "fail" if problems else "ok", "; ".join(problems))


def _check_board(insts, host):
    model = host.get("model")
    hw = [i for i in insts if i["mode"] == "hw"]
    if model is None:
        return _skip("instances", "board model not probed", len(hw))
    if "raspberry pi 5" in model.lower() and len(hw) > 1:
        return _res(len(hw), 1, "instances", "warn",
                    "Raspberry Pi 5: the RP1 USB buses are independent, but its 4 cores carry "
                    "one hardware instance")
    return _res(len(hw), None, "instances", "ok", model)


def evaluate(instances, host, federations=(), usb2_bus_budget_MBps=DEFAULT_USB2_BUS_BUDGET_MBPS,
             strict=False):
    """Pre-flight report (the I.2 --json document) for *instances* on *host*.

    instances: dicts from instance_from_params/instance_from_registry/
    planned_instance; host: the --host-json schema (missing key = not
    probed, plus "probed": bool); federations: dicts from
    federation_from_params. strict: warnings count as failures for "exit".
    """
    insts = []
    for i in instances:
        j = dict(i)
        j["model"] = instance_model(j)
        insts.append(j)
    # One listener per (name, port) wherever it is declared: the coordinator
    # named by a federation.ini and by an instance INI on this host is the
    # same service and must not collide with itself.
    services, seen = [], set()
    for f in federations:
        for name, port in f["service_ports"]:
            if (name, port) not in seen:
                seen.add((name, port))
                services.append((f["config"] or "federation.ini", name, port))
    for i in insts:
        for name, port in i.get("service_ports", []):
            if (name, port) not in seen:
                seen.add((name, port))
                services.append(("instance {0} config".format(i["instance_id"]), name, port))
    resources = {
        "cores": _check_cores(insts, host),
        "core_overlap": _check_core_overlap(insts),
    }
    resources.update(_check_memory(insts, host))
    resources.update(_check_usb(insts, host, usb2_bus_budget_MBps))
    resources.update(_check_egress(insts, host))
    resources["ports"] = _check_ports(insts, services)
    resources["serials"] = _check_serials(insts)
    resources["frame_period"] = _check_frame_period(insts)
    resources["router_memory"] = _check_router(insts, list(federations), host)
    resources["limits"] = _check_limits(insts)
    resources["board"] = _check_board(insts, host)
    worst = _worst(r["verdict"] for r in resources.values())
    code = _RANK[worst]
    if strict and code == 1:
        code = 2
    return {
        "schema": 1,
        "exit": code,
        "host": {"nproc": host.get("nproc"), "model": host.get("model"),
                 "probed": bool(host.get("probed"))},
        "instances": [{"config": i["config"], "instance_id": i["instance_id"], "mode": i["mode"],
                       "source": i["source"], "ports": dict(i["ports"]), "cores": list(i["cores"]),
                       "model": _public_model(i["model"])} for i in insts],
        "resources": resources,
        "virtual_bytes": {"decimator_in_consumer_map":
                          sum(i["model"]["decimator_in_consumer_map_bytes"] for i in insts)},
    }


def _public_model(m):
    keys = ("usb_ingress_Bps", "usb_transfer_bytes", "usb_ring_bytes", "rebuffer_ring_bytes",
            "decimator_in_bytes", "decimator_out_object_bytes", "decimator_out_resident_bytes",
            "delay_sync_iq_bytes", "delay_sync_copy_bytes", "shm_object_bytes", "shm_resident_bytes",
            "locked_bytes", "memory_need_bytes", "frame_period_sec", "egress_Bps", "rt_priorities")
    return {k: m[k] for k in keys}
