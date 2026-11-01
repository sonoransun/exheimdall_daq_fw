"""
    HeIMDALL "active" capability: configuration schema (``active_chain_config.ini``).

    The active chain has its own INI file, separate from ``daq_chain_config.ini``
    (which is never read or written here). Every section is prefixed
    ``active`` (plus ``[meta]``).

    Parsing (``parse_config_text`` / ``load_config``) turns the INI into frozen
    dataclasses and raises ``ConfigError`` for parse-level failures:

      - unreadable file or INI syntax error;
      - a missing required key or a wrong type;
      - a per-chain list of the wrong length (``LIST_LEN``);
      - an unknown backend or device family;
      - ANY unknown key in ``[active_tx]`` (a typo in a safety key must never
        fall back to a default).

    Range / enum / port / safety-key checks are reported as ``Issue`` lists by
    ``validate_config`` (codes ``RANGE``, ``ENUM``, ``PORT``, ``TX_LOCK`` ...).
    Device-capability checks live in ``active_devices.validate_device_plan``.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
from __future__ import annotations

import configparser
import dataclasses
import json
import math
import os
import re
from dataclasses import dataclass

import active_common as ac
from active_common import (ChannelRef, ConfigError, Issue, parse_az_el_list, parse_bands_hz,
                           parse_bool, parse_float_list, parse_int_list, parse_kv_list)

ac.ensure_daq_core_path()
from antenna_profile import AntennaProfileParser, VALID_POLARIZATIONS  # noqa: E402  (read-only reuse)

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
BACKENDS = ("sim", "uhd", "pervices", "gnuradio")
MODES = ("rx", "tx", "trx")
LOG_LEVELS = ("debug", "info", "warning", "error")
ROLES = ("master", "slave", "auto")
LO_SOURCES = ("internal", "external", "companion")
OTW_FORMATS = ("sc16", "sc8", "sc12")
SYNC_METHODS = ("auto", "none", "unknown_pps", "next_pps_host", "gps_time")
SOURCE_NAMES = ("auto", "internal", "external", "gpsdo", "mboard", "sfp0", "qsfp0")
LO_PLANS = ("none", "auto", "n321_star", "twinrx", "external_lo", "pervices_common")
TIMED_TUNE = ("auto", "on", "off")
EPOCHS = ("host", "device")
GEOMETRY_PRESETS = ("ula", "ura", "uca", "tri")
PHASE_REFERENCES = ("element0", "centroid")
ELEMENT_PATTERNS = ("isotropic", "cos_q", "dipole_v", "dipole_h")
RX_BEAM_METHODS = ("off", "das", "projection", "mvdr", "lcmv")
TX_BEAM_METHODS = ("das", "projection")
TAPERS = ("uniform", "hamming", "hann", "chebyshev", "taylor")
STEERING = ("phase", "ttd")
NORMALIZE = ("unit_gain", "unit_norm")
RX_CAL_METHODS = ("none", "apriori", "injection", "beacon")
TX_CAL_METHODS = ("none", "loopback_tdm", "loopback_hadamard", "cabled", "reciprocity")
CAL_WAVEFORMS = ("zc", "noise", "tone")
TX_MODES = ("dummy_load", "cabled", "radiate")
AUTHORIZATION_KINDS = ("none", "amateur", "experimental", "research", "other")
OUTPUT_MODES = ("elements", "beams", "raw")
UNCALIBRATED_FRAMES = ("dummy", "flagged", "none")
APPLY_CAL_IN = ("bridge", "flowgraph")
SIM_TX_LOADS = ("auto", "antenna", "dummy_load", "cabled")
SIM_WAVEFORMS = ("noise", "tone", "zc", "lfm")

_STAGE_KINDS_TX = ("amp", "pa", "passive", "cable", "pad", "coupler", "switch", "isolator", "filter")
_STAGE_KINDS_RX = ("limiter", "lna", "amp", "passive", "cable", "pad", "switch", "coupler", "filter")
_PASSIVE_KINDS = ("passive", "cable", "pad", "coupler", "switch", "isolator", "filter", "limiter")

MAX_HEADER_CHANNELS = 32
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
_CHAIN_OVERRIDE_RE = re.compile(r"^(rx|tx)_chain\.(\d+)$")

# ---------------------------------------------------------------------------
# Schema: section -> ordered {key: default INI string}. KNOWN_KEYS derives from it.
# None marks a required key (or one with a context-dependent default).
# ---------------------------------------------------------------------------
_SCHEMA = {
    "meta": {"active_ini_version": "1", "config_name": ""},
    "active": {
        "en_active": "0", "backend": "sim", "allow_sim_fallback": "0", "mode": "rx",
        "instance_id": "0", "port_stride": "100", "state_dir": "", "log_dir": "",
        "log_level": "info", "operator": "", "hardware_id": "", "unit_id": "0",
    },
    "active_devices": {"devices": ""},
    "active_device:<name>": {
        "family": None, "args": "", "addr": "", "serial": "", "group": "", "role": "auto",
        "rx_channels": None, "tx_channels": "", "subdev_rx": "", "subdev_tx": "",
        "rx_antenna": "", "tx_antenna": "", "master_clock_rate_hz": "0", "clock_source": "",
        "time_source": "", "lo_source": "internal", "lo_export": "0", "iq_conjugate": "0",
        "gpsdo_fitted": "0", "link_ports": "1", "link_gbps": "10", "pervices_tree": "",
        "pervices_device_delay": "0", "pervices_full_tx_image": "0", "max_tx_dbm_override": "",
    },
    "active_rf": {
        "center_freq_hz": "915e6", "sample_rate_hz": "2e6", "bandwidth_hz": "0",
        "rx_gain_db": "30", "lo_offset_hz": "0", "cpi_samples": "65536", "otw_format": "sc16",
    },
    "active_timing": {
        "sync_method": "auto", "clock_source": "auto", "time_source": "auto",
        "require_ref_lock": "1", "ref_lock_timeout_s": "5.0", "lo_plan": "auto", "lo_master": "",
        "timed_tune": "auto", "command_lead_s": "0.1", "stream_lead_s": "0.3",
        "max_pps_skew_ticks": "1", "epoch": "host", "verify_captures": "4",
        "max_phase_std_deg": "1.0", "max_freq_offset_hz": "0.5",
    },
    "active_geometry": {
        "geometry_file": "", "preset": "ula", "num_elements": "4", "nx": "2", "ny": "2",
        "spacing_m": "0", "spacing_lambda": "0.5", "radius_m": "0", "axis_az_deg": "90",
        "design_freq_hz": "0", "heading_deg": "0", "pitch_deg": "0", "roll_deg": "0",
        "phase_reference": "element0", "scan_limit_deg": "60", "f_min_hz": "0", "f_max_hz": "0",
        "element_pattern": "isotropic", "element_q": "1.0", "element_size_m": "0",
    },
    "active_antenna": {
        "en_antenna_profile": "0", "profile_name": "isotropic", "element_gain_dbi": "0",
        "beamwidth_az_deg": "360", "beamwidth_el_deg": "360", "polarization": "vertical",
        "cable_loss_db": "0", "connector_loss_db": "0", "boresight_az_offset_deg": "0",
        "boresight_el_offset_deg": "0",
    },
    "active_beam": {
        "rx_method": "das", "tx_method": "das", "look_az_deg": "0", "look_el_deg": "0",
        "tx_look_az_deg": "", "tx_look_el_deg": "", "taper": "uniform", "tx_taper": "",
        "sll_db": "30", "nbar": "4", "nulls": "", "tx_nulls": "", "diag_loading": "10",
        "snapshots": "0", "steering": "phase", "focus_range_m": "0", "normalize": "unit_gain",
    },
    "active_calibration": {
        "rx_method": "injection", "injection_source": "external", "injection_paths_file": "",
        "beacon_az_deg": "", "beacon_el_deg": "", "beacon_enu_m": "", "tx_method": "none",
        "coupler_paths_file": "", "loopback_rx_chan": "-1", "cal_samples": "16384",
        "cal_repeats": "4", "cal_waveform": "zc", "tx_cal_amplitude": "0.1",
        "quality_threshold_db": "-20", "max_phase_resid_deg": "2.0", "recal_on_tune": "1",
        "recal_interval_s": "600", "max_cal_age_s": "3600", "recal_temp_delta_c": "3.0",
        "drift_check_interval_s": "30", "drift_threshold_deg": "3.0", "drive_tolerance_db": "1.0",
        "require_sign_check": "1", "use_apriori": "1", "auto_recal": "1", "store_dir": "",
    },
    "active_frontend": {
        "en_frontend": "0", "rx_chain": "", "tx_chain": "", "rx_chain.<i>": "",
        "tx_chain.<i>": "", "device_nf_db": "6.0", "tx_power_cal_file": "",
        "tr_isolation_db": "0", "tx_rx_isolation_db": "0", "tx_rx_isolation_file": "",
        "lna_off_isolation_db": "0", "s_matrix_file": "", "expected_input_dbm": "-60",
        "p1db_margin_db": "3", "lna_freq_table": "",
    },
    "active_rf_control": {
        "pa_enable_line": "none", "tr_switch_line": "none", "lna_bias_line": "none",
        "cal_injection_line": "none", "cal_loopback_line": "none", "noise_source_line": "none",
        "use_atr": "1", "tr_guard_us": "50", "pa_settle_us": "100", "pa_hold_between_bursts": "0",
        "interlock": "none", "dummy_load_sense": "none", "power_monitor": "none",
        "telemetry_max_age_s": "1.0",
    },
    "active_tx": {
        "en_tx": "0", "allowed_modes": "dummy_load", "authorization": "",
        "authorization_kind": "none", "callsign": "", "station_id_interval_s": "600",
        "allow_bands": "", "deny_bands": "", "conducted_ignore_builtin_deny": "0",
        "occupied_bw_hz": "0", "arm_timeout_s": "60", "max_arm_total_s": "900",
        "heartbeat_timeout_s": "2.0", "max_digital_amplitude": "0.5", "max_tx_gain_db": "",
        "max_chain_power_dbm": "0", "max_total_power_dbm": "6", "max_eirp_dbm": "10",
        "mpe_exclusion_m": "0", "max_burst_s": "0.1", "duty_max": "0.1", "duty_window_s": "10",
        "papr_margin_db": "0", "pa_backoff_db": "1.0", "dummy_load_rating_w": "0",
        "dummy_load_pep_w": "0", "cabled_rx_path_loss_db": "0", "loopback_path_loss_db": "0",
        "rx_safe_margin_db": "10", "max_reverse_power_dbm": "", "max_vswr": "2.0",
        "max_temp_c": "70", "require_power_monitor_for_radiate": "1",
        "underflow_trip_count": "5", "underflow_window_s": "1.0", "mute_rx_during_tx": "1",
        "lna_off_during_tx": "1", "protect_passive_chain": "1", "passive_isolation_db": "0",
        "passive_rx_damage_dbm": "10", "min_start_lead_s": "0.05",
    },
    "active_output": {
        "en_iq_server": "1", "listen_address": "127.0.0.1", "iq_port": "5050",
        "allow_port_5000": "0", "control_port": "5051", "status_port": "5052",
        "event_port": "5053", "en_event_pub": "0", "output_mode": "elements", "beams": "",
        "uncalibrated_frames": "dummy", "emit_cal_frames": "0", "queue_len": "4",
        "sidecar_path": "", "record_path": "", "stamp_platform_bearing": "0",
    },
    "active_gnuradio": {
        "flowgraph_dir": "", "zmq_address": "tcp://127.0.0.1:5055", "python": "python3",
        "start_timeout_s": "15", "apply_cal_in": "bridge",
    },
    "active_sim": {
        "seed": "12345", "emulate_family": "sim", "emitters": "30:0:0:-60:noise",
        "noise_dbfs": "-60", "rx_fullscale_dbm": "0.0", "rx_delay_spread_samples": "3.0",
        "tx_delay_spread_samples": "2.0", "rx_gain_sigma_db": "1.0", "tx_gain_sigma_db": "1.0",
        "gain_phase_deg_per_db": "0.3", "ref_ppm_sigma": "2.0", "pps_skew_ticks": "0",
        "iq_conjugate_devices": "", "coupling_db": "-99", "injection_dbm": "-50",
        "injection_path_sigma_deg": "0", "coupler_path_sigma_deg": "0",
        "turnaround_samples": "37", "tx_true_offset_db": "3.0", "load_vswr": "1.2",
        "tx_leak_dbc": "-40", "tx_load": "auto", "temperature_c": "35",
        "temp_drift_deg_per_c": "0.2", "heat_c_per_w": "5.0",
    },
}

KNOWN_KEYS = {sec: tuple(keys) for sec, keys in _SCHEMA.items()}

# Keys whose values use ';' list separators. configparser treats " ;" (whitespace
# then ';') as an inline comment and would silently truncate such a list.
_SEMICOLON_LIST_KEYS = {
    "allow_bands", "deny_bands", "nulls", "tx_nulls", "beams", "emitters", "pervices_tree",
    "rx_chain", "tx_chain", "lna_freq_table",
}

# Short aliases accepted by default_config(**overrides).
_SECTION_ALIASES = {
    "meta": "meta", "active": "active", "rf": "active_rf", "timing": "active_timing",
    "geometry": "active_geometry", "antenna": "active_antenna", "beam": "active_beam",
    "calibration": "active_calibration", "frontend": "active_frontend",
    "rf_control": "active_rf_control", "tx": "active_tx", "output": "active_output",
    "gnuradio": "active_gnuradio", "sim": "active_sim",
}

# Auxiliary files whose CONTENTS enter the fingerprint: (section attr, field).
_AUX_FILES = (
    ("geometry", "geometry_file"),
    ("calibration", "injection_paths_file"),
    ("calibration", "coupler_paths_file"),
    ("frontend", "tx_power_cal_file"),
    ("frontend", "s_matrix_file"),
    ("frontend", "tx_rx_isolation_file"),
)


# ---------------------------------------------------------------------------
# Section dataclasses (fields = INI keys)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MetaCfg:
    active_ini_version: int = 1
    config_name: str = ""


@dataclass(frozen=True)
class ActiveCfg:
    en_active: bool = False
    backend: str = "sim"
    allow_sim_fallback: bool = False
    mode: str = "rx"
    instance_id: int = 0
    port_stride: int = 100
    state_dir: str = ""
    log_dir: str = ""
    log_level: str = "info"
    operator: str = ""
    hardware_id: str = ""
    unit_id: int = 0


@dataclass(frozen=True)
class DeviceCfg:
    name: str
    family: str
    args: str = ""
    addr: str = ""
    serial: str = ""
    group: str = ""
    role: str = "auto"
    rx_channels: tuple = ()
    tx_channels: tuple = ()
    subdev_rx: str = ""
    subdev_tx: str = ""
    rx_antenna: str = ""
    tx_antenna: str = ""
    master_clock_rate_hz: float = 0.0
    clock_source: str = ""
    time_source: str = ""
    lo_source: str = "internal"
    lo_export: bool = False
    iq_conjugate: bool = False
    gpsdo_fitted: bool = False
    link_ports: int = 1
    link_gbps: float = 10.0
    pervices_tree: tuple = ()          # ((path, value), ...) in file order
    pervices_device_delay: bool = False
    pervices_full_tx_image: bool = False
    max_tx_dbm_override: float = None  # None = not set

    def pervices_tree_dict(self) -> dict:
        return dict(self.pervices_tree)


@dataclass(frozen=True)
class RfCfg:
    center_freq_hz: float = 915e6
    sample_rate_hz: float = 2e6
    bandwidth_hz: float = 0.0
    rx_gain_db: tuple = ()             # one value per global RX channel
    lo_offset_hz: float = 0.0
    cpi_samples: int = 65536
    otw_format: str = "sc16"

    def effective_bandwidth_hz(self) -> float:
        return self.bandwidth_hz if self.bandwidth_hz > 0 else self.sample_rate_hz


@dataclass(frozen=True)
class TimingCfg:
    sync_method: str = "auto"
    clock_source: str = "auto"
    time_source: str = "auto"
    require_ref_lock: bool = True
    ref_lock_timeout_s: float = 5.0
    lo_plan: str = "auto"
    lo_master: str = ""
    timed_tune: str = "auto"
    command_lead_s: float = 0.1
    stream_lead_s: float = 0.3
    max_pps_skew_ticks: int = 1
    epoch: str = "host"
    verify_captures: int = 4
    max_phase_std_deg: float = 1.0
    max_freq_offset_hz: float = 0.5


@dataclass(frozen=True)
class GeometryCfg:
    geometry_file: str = ""
    preset: str = "ula"
    num_elements: int = 4
    nx: int = 2
    ny: int = 2
    spacing_m: float = 0.0
    spacing_lambda: float = 0.5
    radius_m: float = 0.0
    axis_az_deg: float = 90.0
    design_freq_hz: float = 0.0
    heading_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    phase_reference: str = "element0"
    scan_limit_deg: float = 60.0
    f_min_hz: float = 0.0
    f_max_hz: float = 0.0
    element_pattern: str = "isotropic"
    element_q: float = 1.0
    element_size_m: float = 0.0

    def preset_num_elements(self) -> int:
        """Element count of the preset (ignored when ``geometry_file`` is set)."""
        if self.preset in ("ura", "tri"):
            return int(self.nx) * int(self.ny)
        return int(self.num_elements)


@dataclass(frozen=True)
class BeamCfg:
    rx_method: str = "das"
    tx_method: str = "das"
    look_az_deg: float = 0.0
    look_el_deg: float = 0.0
    tx_look_az_deg: float = None       # None = same as look
    tx_look_el_deg: float = None
    taper: str = "uniform"
    tx_taper: str = ""                 # "" = same as taper
    sll_db: float = 30.0
    nbar: int = 4
    nulls: tuple = ()                  # ((az, el), ...)
    tx_nulls: tuple = ()
    diag_loading: float = 10.0
    snapshots: int = 0
    steering: str = "phase"
    focus_range_m: float = 0.0
    normalize: str = "unit_gain"

    def tx_look(self) -> tuple:
        az = self.look_az_deg if self.tx_look_az_deg is None else self.tx_look_az_deg
        el = self.look_el_deg if self.tx_look_el_deg is None else self.tx_look_el_deg
        return (az, el)

    def effective_tx_taper(self) -> str:
        return self.tx_taper or self.taper


@dataclass(frozen=True)
class CalibrationCfg:
    rx_method: str = "injection"
    injection_source: str = "external"
    injection_paths_file: str = ""
    beacon_az_deg: float = None
    beacon_el_deg: float = None
    beacon_enu_m: tuple = None         # (x, y, z) or None
    tx_method: str = "none"
    coupler_paths_file: str = ""
    loopback_rx_chan: int = -1
    cal_samples: int = 16384
    cal_repeats: int = 4
    cal_waveform: str = "zc"
    tx_cal_amplitude: float = 0.1
    quality_threshold_db: float = -20.0
    max_phase_resid_deg: float = 2.0
    recal_on_tune: bool = True
    recal_interval_s: float = 600.0
    max_cal_age_s: float = 3600.0
    recal_temp_delta_c: float = 3.0
    drift_check_interval_s: float = 30.0
    drift_threshold_deg: float = 3.0
    drive_tolerance_db: float = 1.0
    require_sign_check: bool = True
    use_apriori: bool = True
    auto_recal: bool = True
    store_dir: str = ""

    def injection_tx_index(self):
        """Global TX index for ``injection_source = tx:<k>``, else None."""
        src = self.injection_source
        if src.startswith("tx:"):
            try:
                return int(src[3:])
            except ValueError:
                return None
        return None


@dataclass(frozen=True)
class FrontendCfg:
    en_frontend: bool = False
    rx_chain: str = ""
    tx_chain: str = ""
    rx_chain_overrides: tuple = ()     # ((global_rx_index, spec), ...) from rx_chain.<i>
    tx_chain_overrides: tuple = ()
    device_nf_db: float = 6.0
    tx_power_cal_file: str = ""
    tr_isolation_db: float = 0.0
    tx_rx_isolation_db: float = 0.0
    tx_rx_isolation_file: str = ""
    lna_off_isolation_db: float = 0.0
    s_matrix_file: str = ""
    expected_input_dbm: float = -60.0
    p1db_margin_db: float = 3.0
    lna_freq_table: str = ""

    def rx_chain_spec(self, i: int) -> str:
        """Chain spec for global RX ``i`` ("" when the front end is disabled)."""
        if not self.en_frontend:
            return ""
        return dict(self.rx_chain_overrides).get(int(i), self.rx_chain)

    def tx_chain_spec(self, i: int) -> str:
        if not self.en_frontend:
            return ""
        return dict(self.tx_chain_overrides).get(int(i), self.tx_chain)


@dataclass(frozen=True)
class RfControlCfg:
    pa_enable_line: str = "none"
    tr_switch_line: str = "none"
    lna_bias_line: str = "none"
    cal_injection_line: str = "none"
    cal_loopback_line: str = "none"
    noise_source_line: str = "none"
    use_atr: bool = True
    tr_guard_us: float = 50.0
    pa_settle_us: float = 100.0
    pa_hold_between_bursts: bool = False
    interlock: str = "none"
    dummy_load_sense: str = "none"
    power_monitor: str = "none"
    telemetry_max_age_s: float = 1.0


@dataclass(frozen=True)
class TxCfg:
    en_tx: bool = False
    allowed_modes: tuple = ("dummy_load",)
    authorization: str = ""
    authorization_kind: str = "none"
    callsign: str = ""
    station_id_interval_s: float = 600.0
    allow_bands: tuple = ()            # ((lo_hz, hi_hz), ...)
    deny_bands: tuple = ()
    conducted_ignore_builtin_deny: bool = False
    occupied_bw_hz: float = 0.0
    arm_timeout_s: float = 60.0
    max_arm_total_s: float = 900.0
    heartbeat_timeout_s: float = 2.0
    max_digital_amplitude: float = 0.5
    max_tx_gain_db: tuple = ()         # per global TX; None entry = caps MINIMUM (fail-safe)
    max_chain_power_dbm: tuple = ()    # per global TX
    max_total_power_dbm: float = 6.0
    max_eirp_dbm: float = 10.0
    mpe_exclusion_m: float = 0.0
    max_burst_s: float = 0.1
    duty_max: float = 0.1
    duty_window_s: float = 10.0
    papr_margin_db: float = 0.0
    pa_backoff_db: float = 1.0
    dummy_load_rating_w: tuple = ()    # per global TX
    dummy_load_pep_w: tuple = ()       # per global TX; 0 = use the rating
    cabled_rx_path_loss_db: tuple = () # per global TX (TX n -> RX n)
    loopback_path_loss_db: tuple = ()  # per global TX (TX n -> loopback RX)
    rx_safe_margin_db: float = 10.0
    max_reverse_power_dbm: float = None
    max_vswr: float = 2.0
    max_temp_c: float = 70.0
    require_power_monitor_for_radiate: bool = True
    underflow_trip_count: int = 5
    underflow_window_s: float = 1.0
    mute_rx_during_tx: bool = True
    lna_off_during_tx: bool = True
    protect_passive_chain: bool = True
    passive_isolation_db: float = 0.0
    passive_rx_damage_dbm: float = 10.0
    min_start_lead_s: float = 0.05

    def effective_occupied_bw_hz(self, sample_rate_hz: float) -> float:
        return self.occupied_bw_hz if self.occupied_bw_hz > 0 else float(sample_rate_hz)

    def dummy_load_pep_effective_w(self, i: int) -> float:
        pep = self.dummy_load_pep_w[i]
        return pep if pep > 0 else self.dummy_load_rating_w[i]


@dataclass(frozen=True)
class OutputCfg:
    en_iq_server: bool = True
    listen_address: str = "127.0.0.1"
    iq_port: int = 5050
    allow_port_5000: bool = False
    control_port: int = 5051
    status_port: int = 5052
    event_port: int = 5053
    en_event_pub: bool = False
    output_mode: str = "elements"
    beams: tuple = ()                  # ((az, el), ...)
    uncalibrated_frames: str = "dummy"
    emit_cal_frames: bool = False
    queue_len: int = 4
    sidecar_path: str = ""
    record_path: str = ""
    stamp_platform_bearing: bool = False


@dataclass(frozen=True)
class GnuradioCfg:
    flowgraph_dir: str = ""
    zmq_address: str = "tcp://127.0.0.1:5055"
    python: str = "python3"
    start_timeout_s: float = 15.0
    apply_cal_in: str = "bridge"


@dataclass(frozen=True)
class SimEmitter:
    """One simulated emitter: ``az:el:range_m:power_dbm[:waveform[:f_offset_hz]]``."""
    az_deg: float
    el_deg: float
    range_m: float = 0.0               # 0 = far field
    power_dbm: float = -60.0
    waveform: str = "noise"
    f_offset_hz: float = 0.0


@dataclass(frozen=True)
class SimCfg:
    seed: int = 12345
    emulate_family: str = "sim"
    emitters: tuple = (SimEmitter(30.0, 0.0, 0.0, -60.0, "noise", 0.0),)
    noise_dbfs: float = -60.0
    rx_fullscale_dbm: float = 0.0
    rx_delay_spread_samples: float = 3.0
    tx_delay_spread_samples: float = 2.0
    rx_gain_sigma_db: float = 1.0
    tx_gain_sigma_db: float = 1.0
    gain_phase_deg_per_db: float = 0.3
    ref_ppm_sigma: float = 2.0
    pps_skew_ticks: int = 0
    iq_conjugate_devices: tuple = ()
    coupling_db: float = -99.0
    injection_dbm: float = -50.0
    injection_path_sigma_deg: float = 0.0
    coupler_path_sigma_deg: float = 0.0
    turnaround_samples: int = 37
    tx_true_offset_db: float = 3.0
    load_vswr: float = 1.2
    tx_leak_dbc: float = -40.0
    tx_load: str = "auto"
    temperature_c: float = 35.0
    temp_drift_deg_per_c: float = 0.2
    heat_c_per_w: float = 5.0


@dataclass(frozen=True)
class ActiveConfig:
    path: str
    base_dir: str
    meta: MetaCfg
    active: ActiveCfg
    devices: tuple                     # DeviceCfg in [active_devices] order
    rf: RfCfg
    timing: TimingCfg
    geometry: GeometryCfg
    beam: BeamCfg
    calibration: CalibrationCfg
    frontend: FrontendCfg
    rf_control: RfControlCfg
    tx: TxCfg
    output: OutputCfg
    gnuradio: GnuradioCfg
    sim: SimCfg
    antenna_profile: object = None     # AntennaProfile | None
    parse_issues: tuple = ()
    # [active_antenna] keys set to a non-default value while en_antenna_profile = 0
    # (they would be ignored: EIRP / MPE would omit the antenna gain).
    antenna_ignored_keys: tuple = ()

    # --- devices and channels ----------------------------------------------
    def device(self, name) -> DeviceCfg:
        for d in self.devices:
            if d.name == name:
                return d
        raise ConfigError("unknown device %r (configured: %s)"
                          % (name, ", ".join(d.name for d in self.devices) or "none"),
                          (Issue("error", "UNKNOWN_DEVICE", "unknown device %r" % name),))

    def device_names(self) -> list:
        return [d.name for d in self.devices]

    def rx_refs(self) -> list:
        return [ChannelRef(d.name, int(c)) for d in self.devices for c in d.rx_channels]

    def tx_refs(self) -> list:
        return [ChannelRef(d.name, int(c)) for d in self.devices for c in d.tx_channels]

    def num_rx(self) -> int:
        return sum(len(d.rx_channels) for d in self.devices)

    def num_tx(self) -> int:
        return sum(len(d.tx_channels) for d in self.devices)

    def master_device(self) -> DeviceCfg:
        """The explicit ``role = master`` device, else the first device."""
        for d in self.devices:
            if d.role == "master":
                return d
        return self.devices[0]

    # --- paths ---------------------------------------------------------------
    def resolve_path(self, p) -> str:
        """Relative paths resolve against the INI directory; "" stays ""."""
        if not p:
            return ""
        p = os.path.expanduser(str(p))
        if os.path.isabs(p):
            return os.path.normpath(p)
        return os.path.normpath(os.path.join(self.base_dir, p))

    def resolved_state_dir(self) -> str:
        """``[active] state_dir`` or the per-instance default (created)."""
        return ac.state_dir(self.active.instance_id, self.resolve_path(self.active.state_dir))

    def resolved_log_dir(self) -> str:
        return ac.log_dir(self.active.instance_id, self.resolve_path(self.active.log_dir))

    # --- misc ----------------------------------------------------------------
    def port_offset(self) -> int:
        return int(self.active.instance_id) * int(self.active.port_stride)

    def resolved_hardware_id(self, simulation: bool = None) -> str:
        """``hardware_id`` or ``ACT-<FAMILY0>`` (<= 16 chars). In simulation the
        ``ACT-`` prefix becomes ``SIM-`` and any other id gets a ``SIM-`` prefix."""
        if simulation is None:
            simulation = self.active.backend == "sim"
        hid = self.active.hardware_id
        if not hid:
            fam = self.devices[0].family.upper() if self.devices else "NONE"
            hid = ("ACT-" + fam)[:16]
        if simulation:
            if hid.startswith("ACT-"):
                hid = "SIM-" + hid[4:]
            elif not hid.startswith("SIM-"):
                hid = ("SIM-" + hid)[:16]
        return hid[:16]

    def tx_enabled(self) -> bool:
        """TX is enabled only with ``en_tx = 1`` AND ``mode`` in (tx, trx)."""
        return bool(self.tx.en_tx) and self.active.mode in ("tx", "trx")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class _Reader:
    """Typed access to one INI section with defaults from ``_SCHEMA``.

    Parse errors are collected in ``errors`` (raised together at the end);
    warnings in ``warnings``.
    """

    def __init__(self, cp, section, schema_key, errors, warnings):
        self.cp = cp
        self.section = section
        self.schema = _SCHEMA[schema_key]
        self.errors = errors
        self.warnings = warnings

    def where(self, key):
        return "[%s] %s" % (self.section, key)

    def present(self, key) -> bool:
        return self.cp.has_section(self.section) and self.cp.has_option(self.section, key)

    def raw(self, key, default=None) -> str:
        if key not in self.schema:
            raise KeyError("schema has no key %r in %s" % (key, self.section))
        if self.present(key):
            return self.cp.get(self.section, key).strip()
        d = self.schema[key] if default is None else default
        return "" if d is None else d

    def _guard(self, fn, key, fallback):
        try:
            return fn()
        except ConfigError as exc:
            self.errors.extend(exc.issues or (Issue("error", "TYPE", str(exc), self.where(key)),))
            return fallback

    def s(self, key) -> str:
        return self.raw(key)

    def i(self, key) -> int:
        return self._guard(lambda: ac._to_int(self.raw(key), self.where(key)), key,
                           int(float(self.schema[key] or 0)))

    def f(self, key) -> float:
        return self._guard(lambda: ac._to_float(self.raw(key), self.where(key)), key,
                           float(self.schema[key] or 0.0))

    def optf(self, key):
        txt = self.raw(key)
        if txt == "":
            return None
        return self._guard(lambda: ac._to_float(txt, self.where(key)), key, None)

    def b(self, key) -> bool:
        return self._guard(lambda: parse_bool(self.raw(key), self.where(key), self.warnings), key,
                           self.schema[key] == "1")

    def enum(self, key) -> str:
        return self.raw(key).lower()

    def flist(self, key, n, default=0.0) -> tuple:
        return tuple(self._guard(lambda: parse_float_list(self.raw(key), n, default,
                                                          self.where(key)), key, [default] * n))

    def optflist(self, key, n) -> tuple:
        return tuple(self._guard(lambda: parse_float_list(self.raw(key), n, None,
                                                          self.where(key)), key, [None] * n))

    def ilist(self, key, raw=None) -> tuple:
        txt = self.raw(key) if raw is None else raw
        return tuple(self._guard(lambda: parse_int_list(txt, None, None, self.where(key)), key, []))

    def bands(self, key) -> tuple:
        return tuple(self._guard(lambda: parse_bands_hz(self.raw(key), self.where(key)), key, []))

    def azel(self, key) -> tuple:
        return tuple(self._guard(lambda: parse_az_el_list(self.raw(key), self.where(key)), key, []))

    def kv(self, key) -> tuple:
        return tuple(self._guard(lambda: parse_kv_list(self.raw(key), self.where(key)), key,
                                 {}).items())

    def names(self, key) -> tuple:
        txt = self.raw(key)
        return tuple(t.strip() for t in txt.split(",") if t.strip()) if txt else ()


def _families():
    from active_devices import families  # lazy: active_devices imports this module
    return families()


# configparser's [DEFAULT] section would be inherited by every section (including
# the strict [active_tx]); parsing uses this unreachable name instead, so a literal
# [DEFAULT] becomes an ordinary section and is rejected by _check_unknown.
_NO_DEFAULT_SECTION = "\x00active-no-default\x00"
_INLINE_PREFIXES = ("#", ";")
_INLINE_RE = re.compile(r"\s[#;]")
_KEYLIKE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*[=:]")


def _new_parser(inline_comments: bool = True):
    return configparser.ConfigParser(
        interpolation=None, default_section=_NO_DEFAULT_SECTION,
        inline_comment_prefixes=_INLINE_PREFIXES if inline_comments else None)


def _list_key(key) -> bool:
    return _CHAIN_OVERRIDE_RE.sub(r"\1_chain", key) in _SEMICOLON_LIST_KEYS


def _issue_for(section, errors, warnings, code, msg, where):
    if section == "active_tx":
        errors.append(Issue("error", code, msg, where))
    else:
        warnings.append(Issue("warning", code, msg, where))


def _scan_inline_semicolons(cp, text, errors, warnings):
    """Detect ';'-separated list values that configparser truncated.

    1. Every list value (and every value of the strict [active_tx]) configparser
       produced is compared with a second parse
       that keeps inline comments: any non-separator text removed (a ' ;' list
       separator, a ' #' comment, on the key line, a continuation line, or after
       a commented section header) is reported.
    2. An indented full-line comment (';...' / '#...') inside a multi-line list
       (or [active_tx]) value is dropped by configparser; the raw text is scanned for it.
    Error in [active_tx], warning elsewhere.
    """
    seen = set()
    cp_raw = _new_parser(inline_comments=False)
    try:
        cp_raw.read_string(text)
    except configparser.Error:
        cp_raw = None
    norm = lambda v: re.sub(r"[\s;]+", "", v)  # noqa: E731
    for sec in cp.sections():
        for key in cp.options(sec):
            # Every key of the strict [active_tx] is checked: a stripped ' ;' in a
            # comma list of per-chain limits (written with ';' like the band lists
            # beside it) would broadcast the first value to every chain.
            is_list = _list_key(key)
            if not is_list and sec != "active_tx":
                continue
            kept = cp.get(sec, key)
            raw = None
            if cp_raw is not None and cp_raw.has_section(sec) and cp_raw.has_option(sec, key):
                raw = cp_raw.get(sec, key)
            if raw is None or norm(raw) == norm(kept):
                continue
            seen.add((sec, key))
            if is_list:
                msg = ("an inline comment (' ;' or ' #') was stripped from the %s list (kept %r of "
                       "%r); write ';' separators without a preceding space and put comments on "
                       "their own line" % (key, kept.strip(), raw.strip()))
            else:
                msg = ("an inline comment (' ;' or ' #') was stripped from %s (kept %r of %r); "
                       "[active_tx] allows no inline comments: per-chain lists are "
                       "comma-separated and comments go on their own line"
                       % (key, kept.strip(), raw.strip()))
            _issue_for(sec, errors, warnings, "INLINE_COMMENT", msg, "[%s] %s" % (sec, key))

    # configparser-like line walk for indented full-line comments inside list values
    section, cur_key, cur_indent = None, None, 0
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if stripped[0] in "#;":
            if cur_key is not None and indent > cur_indent \
                    and (_list_key(cur_key) or section == "active_tx") \
                    and (section, cur_key) not in seen:
                msg = ("line %d: an indented comment line inside the multi-line %s value is dropped "
                       "by configparser (an entry written as ';...' is lost); put list "
                       "separators at the end of the previous line" % (lineno, cur_key))
                _issue_for(section, errors, warnings, "INLINE_COMMENT", msg,
                           "[%s] %s" % (section, cur_key))
            continue
        if cur_key is not None and indent > cur_indent:
            continue  # continuation line
        m = _INLINE_RE.search(stripped)
        value = stripped[:m.start()].strip() if m else stripped
        mo = configparser.ConfigParser.SECTCRE.match(value)
        if mo:
            section, cur_key = mo.group("header"), None
            continue
        km = re.match(r"^([^=:]+?)\s*[=:]", value)
        if km:
            cur_key, cur_indent = km.group(1).strip().lower(), indent


def _check_multiline(cp, errors, warnings):
    """A value spanning several lines is either a wrapped list or an indented key
    swallowed by the previous value. In [active_tx] every multi-line scalar value
    is an error; a continuation line that starts with a known key of the section
    is an error in [active_tx] and a warning elsewhere."""
    for sec in cp.sections():
        if sec.startswith("active_device:"):
            schema_key = "active_device:<name>"
        elif sec in _SCHEMA:
            schema_key = sec
        else:
            continue
        known = _SCHEMA[schema_key]
        for key in cp.options(sec):
            val = cp.get(sec, key).strip()
            if "\n" not in val:
                continue
            where = "[%s] %s" % (sec, key)
            cont = val.split("\n")[1:]
            swallowed = [m.group(1) for m in (_KEYLIKE_RE.match(c) for c in cont)
                         if m and m.group(1).lower() in known]
            if swallowed:
                _issue_for(sec, errors, warnings, "SYNTAX",
                           "multi-line value / indented key: %s swallowed by %s (keys must "
                           "start in column 0)" % (", ".join(swallowed), key), where)
            elif sec == "active_tx" and not _list_key(key):
                errors.append(Issue("error", "SYNTAX", "multi-line value / indented key: %s in "
                                    "the safety section must fit on one line" % key, where))


def _check_unknown(cp, errors, warnings):
    for sec in cp.sections():
        if sec.upper() == "DEFAULT":
            errors.append(Issue("error", "SYNTAX", "[%s] is not supported: configparser would "
                                "inherit its keys into every section (including [active_tx]); "
                                "put each key in its own section" % sec, "[%s]" % sec))
            continue
        if sec.startswith("active_device:"):
            schema_key = "active_device:<name>"
        elif sec in _SCHEMA:
            schema_key = sec
        else:
            warnings.append(Issue("warning", "UNKNOWN_KEY", "unknown section [%s] ignored" % sec,
                                  "[%s]" % sec))
            continue
        known = _SCHEMA[schema_key]
        for key in cp.options(sec):
            if key in known:
                continue
            if sec == "active_frontend" and _CHAIN_OVERRIDE_RE.match(key):
                continue
            msg = "unknown key %r" % key
            where = "[%s] %s" % (sec, key)
            if sec == "active_tx":
                errors.append(Issue("error", "UNKNOWN_KEY",
                                    msg + " in the safety section (typos never fall back to "
                                          "defaults)", where))
            else:
                warnings.append(Issue("warning", "UNKNOWN_KEY", msg + " ignored", where))


def _parse_devices(cp, active, geometry, errors, warnings):
    fams = _families()
    if not cp.has_section("active_devices"):
        if active.backend != "sim":
            errors.append(Issue("error", "REQUIRED",
                                "[active_devices] is required for backend %r" % active.backend,
                                "[active_devices]"))
            return ()
        if geometry.geometry_file:
            errors.append(Issue("error", "REQUIRED",
                                "[active_devices] is required when geometry_file is set "
                                "(the channel map comes from the file)", "[active_devices]"))
            return ()
        n = max(0, geometry.preset_num_elements())
        return (DeviceCfg(name="sim0", family="sim", rx_channels=tuple(range(n)), tx_channels=()),)

    rd = _Reader(cp, "active_devices", "active_devices", errors, warnings)
    names = list(rd.names("devices"))
    if not names:
        errors.append(Issue("error", "REQUIRED", "[active_devices] devices is empty",
                            "[active_devices] devices"))
        return ()
    seen = set()
    for nm in names:
        if not _NAME_RE.match(nm):
            errors.append(Issue("error", "TYPE", "invalid device name %r ([A-Za-z0-9_-], <= 32)" % nm,
                                "[active_devices] devices"))
        if nm in seen:
            errors.append(Issue("error", "TYPE", "duplicate device name %r" % nm,
                                "[active_devices] devices"))
        seen.add(nm)
    for sec in cp.sections():
        if sec.startswith("active_device:") and sec.split(":", 1)[1] not in seen:
            warnings.append(Issue("warning", "UNKNOWN_KEY",
                                  "section [%s] is not listed in [active_devices] devices" % sec,
                                  "[%s]" % sec))
    devs = []
    for k, nm in enumerate(names):
        sec = "active_device:" + nm
        if not cp.has_section(sec):
            errors.append(Issue("error", "REQUIRED", "missing section [%s]" % sec, "[%s]" % sec))
            continue
        r = _Reader(cp, sec, "active_device:<name>", errors, warnings)
        family = r.raw("family").lower()
        if not family:
            errors.append(Issue("error", "REQUIRED", "family is required", r.where("family")))
            continue
        if family not in fams:
            errors.append(Issue("error", "ENUM", "unknown family %r (valid: %s)"
                                % (family, ", ".join(fams)), r.where("family")))
            continue
        rx_default = "0" if k == 0 else ""
        rx_raw = cp.get(sec, "rx_channels").strip() if cp.has_option(sec, "rx_channels") else rx_default
        devs.append(DeviceCfg(
            name=nm, family=family, args=r.s("args"), addr=r.s("addr"), serial=r.s("serial"),
            group=r.s("group"), role=r.enum("role"), rx_channels=r.ilist("rx_channels", rx_raw),
            tx_channels=r.ilist("tx_channels"), subdev_rx=r.s("subdev_rx"),
            subdev_tx=r.s("subdev_tx"), rx_antenna=r.s("rx_antenna"), tx_antenna=r.s("tx_antenna"),
            master_clock_rate_hz=r.f("master_clock_rate_hz"),
            clock_source=r.enum("clock_source"), time_source=r.enum("time_source"),
            lo_source=r.enum("lo_source"), lo_export=r.b("lo_export"),
            iq_conjugate=r.b("iq_conjugate"), gpsdo_fitted=r.b("gpsdo_fitted"),
            link_ports=r.i("link_ports"), link_gbps=r.f("link_gbps"),
            pervices_tree=r.kv("pervices_tree"),
            pervices_device_delay=r.b("pervices_device_delay"),
            pervices_full_tx_image=r.b("pervices_full_tx_image"),
            max_tx_dbm_override=r.optf("max_tx_dbm_override"),
        ))
    return tuple(devs)


def _parse_vec3(r, key):
    txt = r.raw(key)
    if not txt:
        return None
    try:
        vals = parse_float_list(txt, 3, None, r.where(key))
    except ConfigError as exc:
        r.errors.extend(exc.issues)
        return None
    if len(txt.split(",")) != 3:
        r.errors.append(Issue("error", "TYPE", "expected x,y,z", r.where(key)))
        return None
    return tuple(vals)


def _parse_emitters(r, key):
    txt = r.raw(key)
    out = []
    if not txt:
        return ()
    for ent in (e.strip() for e in txt.split(";")):
        if not ent:
            continue
        parts = [p.strip() for p in ent.split(":")]
        if not 4 <= len(parts) <= 6:
            r.errors.append(Issue("error", "TYPE", "emitter %r must be "
                                  "az:el:range_m:power_dbm[:waveform[:f_offset_hz]]" % ent,
                                  r.where(key)))
            continue
        try:
            az, el, rng, pwr = (ac._to_float(p, r.where(key)) for p in parts[:4])
            wf = parts[4].lower() if len(parts) > 4 and parts[4] else "noise"
            foff = ac._to_float(parts[5], r.where(key)) if len(parts) > 5 and parts[5] else 0.0
        except ConfigError as exc:
            r.errors.extend(exc.issues)
            continue
        out.append(SimEmitter(az, el, rng, pwr, wf, foff))
    return tuple(out)


def _chain_overrides(cp, direction, errors):
    out = []
    if not cp.has_section("active_frontend"):
        return ()
    for key in cp.options("active_frontend"):
        m = _CHAIN_OVERRIDE_RE.match(key)
        if m and m.group(1) == direction:
            out.append((int(m.group(2)), cp.get("active_frontend", key).strip()))
    return tuple(sorted(out))


def _parse(cp, text, base_dir, path):
    errors, warnings = [], []
    _check_unknown(cp, errors, warnings)
    _scan_inline_semicolons(cp, text, errors, warnings)
    _check_multiline(cp, errors, warnings)

    def R(sec):
        return _Reader(cp, sec, sec, errors, warnings)

    r = R("meta")
    meta = MetaCfg(active_ini_version=r.i("active_ini_version"), config_name=r.s("config_name"))

    r = R("active")
    backend = r.enum("backend")
    if backend not in BACKENDS:
        errors.append(Issue("error", "ENUM", "unknown backend %r (valid: %s)"
                            % (backend, ", ".join(BACKENDS)), r.where("backend")))
    active = ActiveCfg(
        en_active=r.b("en_active"), backend=backend, allow_sim_fallback=r.b("allow_sim_fallback"),
        mode=r.enum("mode"), instance_id=r.i("instance_id"), port_stride=r.i("port_stride"),
        state_dir=r.s("state_dir"), log_dir=r.s("log_dir"), log_level=r.enum("log_level"),
        operator=r.s("operator"), hardware_id=r.s("hardware_id"), unit_id=r.i("unit_id"))

    r = R("active_geometry")
    geometry = GeometryCfg(
        geometry_file=r.s("geometry_file"), preset=r.enum("preset"),
        num_elements=r.i("num_elements"), nx=r.i("nx"), ny=r.i("ny"), spacing_m=r.f("spacing_m"),
        spacing_lambda=r.f("spacing_lambda"), radius_m=r.f("radius_m"),
        axis_az_deg=r.f("axis_az_deg"), design_freq_hz=r.f("design_freq_hz"),
        heading_deg=r.f("heading_deg"), pitch_deg=r.f("pitch_deg"), roll_deg=r.f("roll_deg"),
        phase_reference=r.enum("phase_reference"), scan_limit_deg=r.f("scan_limit_deg"),
        f_min_hz=r.f("f_min_hz"), f_max_hz=r.f("f_max_hz"),
        element_pattern=r.enum("element_pattern"), element_q=r.f("element_q"),
        element_size_m=r.f("element_size_m"))

    devices = _parse_devices(cp, active, geometry, errors, warnings)
    n_rx = sum(len(d.rx_channels) for d in devices)
    n_tx = sum(len(d.tx_channels) for d in devices)

    r = R("active_rf")
    rf = RfCfg(
        center_freq_hz=r.f("center_freq_hz"), sample_rate_hz=r.f("sample_rate_hz"),
        bandwidth_hz=r.f("bandwidth_hz"), rx_gain_db=r.flist("rx_gain_db", n_rx, 30.0),
        lo_offset_hz=r.f("lo_offset_hz"), cpi_samples=r.i("cpi_samples"),
        otw_format=r.enum("otw_format"))

    r = R("active_timing")
    timing = TimingCfg(
        sync_method=r.enum("sync_method"), clock_source=r.enum("clock_source"),
        time_source=r.enum("time_source"), require_ref_lock=r.b("require_ref_lock"),
        ref_lock_timeout_s=r.f("ref_lock_timeout_s"), lo_plan=r.enum("lo_plan"),
        lo_master=r.s("lo_master"), timed_tune=r.enum("timed_tune"),
        command_lead_s=r.f("command_lead_s"), stream_lead_s=r.f("stream_lead_s"),
        max_pps_skew_ticks=r.i("max_pps_skew_ticks"), epoch=r.enum("epoch"),
        verify_captures=r.i("verify_captures"), max_phase_std_deg=r.f("max_phase_std_deg"),
        max_freq_offset_hz=r.f("max_freq_offset_hz"))

    r = R("active_beam")
    beam = BeamCfg(
        rx_method=r.enum("rx_method"), tx_method=r.enum("tx_method"),
        look_az_deg=r.f("look_az_deg"), look_el_deg=r.f("look_el_deg"),
        tx_look_az_deg=r.optf("tx_look_az_deg"), tx_look_el_deg=r.optf("tx_look_el_deg"),
        taper=r.enum("taper"), tx_taper=r.enum("tx_taper"), sll_db=r.f("sll_db"),
        nbar=r.i("nbar"), nulls=r.azel("nulls"), tx_nulls=r.azel("tx_nulls"),
        diag_loading=r.f("diag_loading"), snapshots=r.i("snapshots"),
        steering=r.enum("steering"), focus_range_m=r.f("focus_range_m"),
        normalize=r.enum("normalize"))

    r = R("active_calibration")
    calibration = CalibrationCfg(
        rx_method=r.enum("rx_method"), injection_source=r.enum("injection_source"),
        injection_paths_file=r.s("injection_paths_file"), beacon_az_deg=r.optf("beacon_az_deg"),
        beacon_el_deg=r.optf("beacon_el_deg"), beacon_enu_m=_parse_vec3(r, "beacon_enu_m"),
        tx_method=r.enum("tx_method"), coupler_paths_file=r.s("coupler_paths_file"),
        loopback_rx_chan=r.i("loopback_rx_chan"), cal_samples=r.i("cal_samples"),
        cal_repeats=r.i("cal_repeats"), cal_waveform=r.enum("cal_waveform"),
        tx_cal_amplitude=r.f("tx_cal_amplitude"), quality_threshold_db=r.f("quality_threshold_db"),
        max_phase_resid_deg=r.f("max_phase_resid_deg"), recal_on_tune=r.b("recal_on_tune"),
        recal_interval_s=r.f("recal_interval_s"), max_cal_age_s=r.f("max_cal_age_s"),
        recal_temp_delta_c=r.f("recal_temp_delta_c"),
        drift_check_interval_s=r.f("drift_check_interval_s"),
        drift_threshold_deg=r.f("drift_threshold_deg"),
        drive_tolerance_db=r.f("drive_tolerance_db"), require_sign_check=r.b("require_sign_check"),
        use_apriori=r.b("use_apriori"), auto_recal=r.b("auto_recal"), store_dir=r.s("store_dir"))

    r = R("active_frontend")
    frontend = FrontendCfg(
        en_frontend=r.b("en_frontend"), rx_chain=r.s("rx_chain"), tx_chain=r.s("tx_chain"),
        rx_chain_overrides=_chain_overrides(cp, "rx", errors),
        tx_chain_overrides=_chain_overrides(cp, "tx", errors),
        device_nf_db=r.f("device_nf_db"), tx_power_cal_file=r.s("tx_power_cal_file"),
        tr_isolation_db=r.f("tr_isolation_db"), tx_rx_isolation_db=r.f("tx_rx_isolation_db"),
        tx_rx_isolation_file=r.s("tx_rx_isolation_file"),
        lna_off_isolation_db=r.f("lna_off_isolation_db"), s_matrix_file=r.s("s_matrix_file"),
        expected_input_dbm=r.f("expected_input_dbm"), p1db_margin_db=r.f("p1db_margin_db"),
        lna_freq_table=r.s("lna_freq_table"))

    r = R("active_rf_control")
    rf_control = RfControlCfg(
        pa_enable_line=r.s("pa_enable_line") or "none", tr_switch_line=r.s("tr_switch_line") or "none",
        lna_bias_line=r.s("lna_bias_line") or "none",
        cal_injection_line=r.s("cal_injection_line") or "none",
        cal_loopback_line=r.s("cal_loopback_line") or "none",
        noise_source_line=r.s("noise_source_line") or "none", use_atr=r.b("use_atr"),
        tr_guard_us=r.f("tr_guard_us"), pa_settle_us=r.f("pa_settle_us"),
        pa_hold_between_bursts=r.b("pa_hold_between_bursts"),
        interlock=r.s("interlock") or "none", dummy_load_sense=r.s("dummy_load_sense") or "none",
        power_monitor=r.s("power_monitor") or "none",
        telemetry_max_age_s=r.f("telemetry_max_age_s"))

    r = R("active_tx")
    tx = TxCfg(
        en_tx=r.b("en_tx"), allowed_modes=tuple(m.lower() for m in r.names("allowed_modes")),
        authorization=r.s("authorization"), authorization_kind=r.enum("authorization_kind"),
        callsign=r.s("callsign"), station_id_interval_s=r.f("station_id_interval_s"),
        allow_bands=r.bands("allow_bands"), deny_bands=r.bands("deny_bands"),
        conducted_ignore_builtin_deny=r.b("conducted_ignore_builtin_deny"),
        occupied_bw_hz=r.f("occupied_bw_hz"), arm_timeout_s=r.f("arm_timeout_s"),
        max_arm_total_s=r.f("max_arm_total_s"), heartbeat_timeout_s=r.f("heartbeat_timeout_s"),
        max_digital_amplitude=r.f("max_digital_amplitude"),
        max_tx_gain_db=r.optflist("max_tx_gain_db", n_tx),
        max_chain_power_dbm=r.flist("max_chain_power_dbm", n_tx),
        max_total_power_dbm=r.f("max_total_power_dbm"), max_eirp_dbm=r.f("max_eirp_dbm"),
        mpe_exclusion_m=r.f("mpe_exclusion_m"), max_burst_s=r.f("max_burst_s"),
        duty_max=r.f("duty_max"), duty_window_s=r.f("duty_window_s"),
        papr_margin_db=r.f("papr_margin_db"), pa_backoff_db=r.f("pa_backoff_db"),
        dummy_load_rating_w=r.flist("dummy_load_rating_w", n_tx),
        dummy_load_pep_w=r.flist("dummy_load_pep_w", n_tx),
        cabled_rx_path_loss_db=r.flist("cabled_rx_path_loss_db", n_tx),
        loopback_path_loss_db=r.flist("loopback_path_loss_db", n_tx),
        rx_safe_margin_db=r.f("rx_safe_margin_db"),
        max_reverse_power_dbm=r.optf("max_reverse_power_dbm"), max_vswr=r.f("max_vswr"),
        max_temp_c=r.f("max_temp_c"),
        require_power_monitor_for_radiate=r.b("require_power_monitor_for_radiate"),
        underflow_trip_count=r.i("underflow_trip_count"),
        underflow_window_s=r.f("underflow_window_s"), mute_rx_during_tx=r.b("mute_rx_during_tx"),
        lna_off_during_tx=r.b("lna_off_during_tx"),
        protect_passive_chain=r.b("protect_passive_chain"),
        passive_isolation_db=r.f("passive_isolation_db"),
        passive_rx_damage_dbm=r.f("passive_rx_damage_dbm"),
        min_start_lead_s=r.f("min_start_lead_s"))

    r = R("active_output")
    output = OutputCfg(
        en_iq_server=r.b("en_iq_server"), listen_address=r.s("listen_address"),
        iq_port=r.i("iq_port"), allow_port_5000=r.b("allow_port_5000"),
        control_port=r.i("control_port"), status_port=r.i("status_port"),
        event_port=r.i("event_port"), en_event_pub=r.b("en_event_pub"),
        output_mode=r.enum("output_mode"), beams=r.azel("beams"),
        uncalibrated_frames=r.enum("uncalibrated_frames"), emit_cal_frames=r.b("emit_cal_frames"),
        queue_len=r.i("queue_len"), sidecar_path=r.s("sidecar_path"),
        record_path=r.s("record_path"), stamp_platform_bearing=r.b("stamp_platform_bearing"))

    r = R("active_gnuradio")
    gnuradio = GnuradioCfg(
        flowgraph_dir=r.s("flowgraph_dir"), zmq_address=r.s("zmq_address"),
        python=r.s("python") or "python3", start_timeout_s=r.f("start_timeout_s"),
        apply_cal_in=r.enum("apply_cal_in"))

    r = R("active_sim")
    emulate = r.enum("emulate_family")
    if emulate not in _families():
        errors.append(Issue("error", "ENUM", "unknown emulate_family %r" % emulate,
                            r.where("emulate_family")))
    sim = SimCfg(
        seed=r.i("seed"), emulate_family=emulate, emitters=_parse_emitters(r, "emitters"),
        noise_dbfs=r.f("noise_dbfs"), rx_fullscale_dbm=r.f("rx_fullscale_dbm"),
        rx_delay_spread_samples=r.f("rx_delay_spread_samples"),
        tx_delay_spread_samples=r.f("tx_delay_spread_samples"),
        rx_gain_sigma_db=r.f("rx_gain_sigma_db"), tx_gain_sigma_db=r.f("tx_gain_sigma_db"),
        gain_phase_deg_per_db=r.f("gain_phase_deg_per_db"), ref_ppm_sigma=r.f("ref_ppm_sigma"),
        pps_skew_ticks=r.i("pps_skew_ticks"), iq_conjugate_devices=r.names("iq_conjugate_devices"),
        coupling_db=r.f("coupling_db"), injection_dbm=r.f("injection_dbm"),
        injection_path_sigma_deg=r.f("injection_path_sigma_deg"),
        coupler_path_sigma_deg=r.f("coupler_path_sigma_deg"),
        turnaround_samples=r.i("turnaround_samples"), tx_true_offset_db=r.f("tx_true_offset_db"),
        load_vswr=r.f("load_vswr"), tx_leak_dbc=r.f("tx_leak_dbc"), tx_load=r.enum("tx_load"),
        temperature_c=r.f("temperature_c"), temp_drift_deg_per_c=r.f("temp_drift_deg_per_c"),
        heat_c_per_w=r.f("heat_c_per_w"))

    antenna = None
    try:
        antenna = AntennaProfileParser.from_ini_section(cp, "active_antenna")
    except (ValueError, configparser.Error) as exc:
        errors.append(Issue("error", "TYPE", "invalid [active_antenna]: %s" % exc, "[active_antenna]"))
    antenna_ignored = ()
    if cp.has_section("active_antenna"):
        pol = cp.get("active_antenna", "polarization", fallback="vertical").strip().lower()
        if pol not in VALID_POLARIZATIONS:
            warnings.append(Issue("warning", "ENUM", "unknown polarization %r (using vertical)" % pol,
                                  "[active_antenna] polarization"))
        if antenna is None and not any(i.where.startswith("[active_antenna]") for i in errors):
            antenna_ignored = _antenna_ignored_keys(cp)

    if errors:
        raise ConfigError("invalid active configuration %s:\n%s" % (path, ac.format_issues(errors)),
                          tuple(errors) + tuple(warnings))

    return ActiveConfig(
        path=path, base_dir=base_dir, meta=meta, active=active, devices=devices, rf=rf,
        timing=timing, geometry=geometry, beam=beam, calibration=calibration, frontend=frontend,
        rf_control=rf_control, tx=tx, output=output, gnuradio=gnuradio, sim=sim,
        antenna_profile=antenna, parse_issues=tuple(warnings),
        antenna_ignored_keys=antenna_ignored)


def _antenna_ignored_keys(cp) -> tuple:
    """[active_antenna] keys whose value differs from the default (en_antenna_profile = 0)."""
    out = []
    for key, default in _SCHEMA["active_antenna"].items():
        if key == "en_antenna_profile" or not cp.has_option("active_antenna", key):
            continue
        val = cp.get("active_antenna", key).strip()
        try:
            same = float(val) == float(default)
        except ValueError:
            same = val.lower() == default.lower()
        if not same:
            out.append(key)
    return tuple(out)


def parse_config_text(text: str, base_dir: str, path: str = "<string>") -> ActiveConfig:
    """Parse INI ``text``. Relative paths in the config resolve against ``base_dir``."""
    cp = _new_parser()
    try:
        cp.read_string(text, source=path)
    except configparser.Error as exc:
        raise ConfigError("INI syntax error in %s: %s" % (path, exc),
                          (Issue("error", "SYNTAX", str(exc), path),)) from None
    return _parse(cp, text, os.path.abspath(base_dir or "."), path)


def load_config(path: str = None) -> ActiveConfig:
    """Load ``path``; None -> ``$HEIMDALL_ACTIVE_CONFIG`` or ``DEFAULT_CONFIG``."""
    if path is None:
        path = os.environ.get("HEIMDALL_ACTIVE_CONFIG") or ac.DEFAULT_CONFIG
    path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("cannot read active config %s: %s" % (path, exc),
                          (Issue("error", "UNREADABLE", str(exc), path),)) from None
    return parse_config_text(text, os.path.dirname(path), path)


def _ini_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (list, tuple)):
        if v and all(isinstance(e, (list, tuple)) for e in v):
            return ";".join(":".join(_ini_value(x) for x in e) for e in v)
        return ",".join(_ini_value(e) for e in v)
    return str(v)


def render_ini(sections: dict) -> str:
    """Render ``{section: {key: value}}`` as INI text (bools -> 0/1, lists -> comma
    lists, lists of pairs -> ``a:b;c:d``). Section names accept the short
    aliases of ``default_config``; ``devices`` is ``{name: {key: value}}``."""
    lines = []
    for sec, keys in sections.items():
        if sec == "devices":
            lines.append("[active_devices]")
            lines.append("devices = " + ",".join(keys.keys()))
            lines.append("")
            for name, dkeys in keys.items():
                lines.append("[active_device:%s]" % name)
                for k, v in dkeys.items():
                    lines.append("%s = %s" % (k, _ini_value(v)))
                lines.append("")
            continue
        lines.append("[%s]" % _SECTION_ALIASES.get(sec, sec))
        for k, v in keys.items():
            lines.append("%s = %s" % (k, _ini_value(v)))
        lines.append("")
    return "\n".join(lines) + "\n"


def default_config(**overrides) -> ActiveConfig:
    """All defaults (sim backend), with optional per-section overrides.

    Example: ``default_config(active={"mode": "trx"}, tx={"en_tx": 1},
    devices={"sim0": {"family": "sim", "rx_channels": "0..7", "tx_channels": "0..7"}})``.
    ``base_dir=`` sets the directory relative paths resolve against.
    """
    base_dir = overrides.pop("base_dir", os.getcwd())
    for sec in overrides:
        if sec != "devices" and _SECTION_ALIASES.get(sec, sec) not in _SCHEMA:
            raise ConfigError("default_config: unknown section %r" % sec)
    return parse_config_text(render_ini(overrides), base_dir, "<default_config>")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class _V:
    def __init__(self):
        self.issues = []

    def add(self, level, code, msg, where=""):
        self.issues.append(Issue(level, code, msg, where))

    def err(self, code, msg, where=""):
        self.add("error", code, msg, where)

    def warn(self, code, msg, where=""):
        self.add("warning", code, msg, where)

    def enum(self, value, allowed, where):
        if value not in allowed:
            self.err("ENUM", "%r is not one of %s" % (value, ", ".join(allowed)), where)

    def rng(self, value, where, lo=None, hi=None, lo_open=False, hi_open=False):
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            self.err("TYPE", "not a number: %r" % (value,), where)
            return
        bad = not math.isfinite(v)
        if lo is not None:
            bad = bad or (v <= lo if lo_open else v < lo)
        if hi is not None:
            bad = bad or (v >= hi if hi_open else v > hi)
        if bad:
            lo_s = "" if lo is None else ("(%g" % lo if lo_open else "[%g" % lo)
            hi_s = "" if hi is None else ("%g)" % hi if hi_open else "%g]" % hi)
            self.err("RANGE", "%g outside %s, %s" % (v, lo_s or "(-inf", hi_s or "inf)"), where)

    def list_len(self, values, n, where, allow_none=False):
        if len(values) != n:
            self.err("LIST_LEN", "expected %d values, got %d" % (n, len(values)), where)
            return False
        if not allow_none and any(v is None for v in values):
            self.err("TYPE", "missing value", where)
            return False
        return True


def _w(sec, key):
    return "[%s] %s" % (sec, key)


def forbidden_ports_passive(max_instances: int = 16) -> set:
    """Ports of the passive chain (and its federation instances) that the active
    chain must never bind: ``{1130, 5000..5003, 6000} + k*100``."""
    base = (1130, 5000, 5001, 5002, 5003, 6000)
    return {b + k * 100 for k in range(int(max_instances)) for b in base}


def _zmq_port(addr):
    m = re.match(r"^tcp://([^:/]+|\[[0-9A-Fa-f:]+\]):(\d+)$", addr or "")
    return int(m.group(2)) if m else None


def active_port_map(cfg) -> dict:
    """``{"iq","control","status","event","gr_zmq"}`` with the instance offset
    applied (0 stays 0 = off/ephemeral; gr_zmq is None for non-TCP addresses)."""
    off = cfg.port_offset()
    o = cfg.output

    def p(v):
        return int(v) + off if int(v) > 0 else 0

    zp = _zmq_port(cfg.gnuradio.zmq_address)
    return {"iq": p(o.iq_port) if o.en_iq_server else 0, "control": p(o.control_port),
            "status": p(o.status_port), "event": p(o.event_port) if o.en_event_pub else 0,
            "gr_zmq": (zp + off if zp else None) if cfg.active.backend == "gnuradio" else None}


def gr_zmq_address(cfg) -> str:
    """``[active_gnuradio] zmq_address`` with the instance port offset applied."""
    addr = cfg.gnuradio.zmq_address
    zp = _zmq_port(addr)
    if zp is None:
        return addr
    return addr[:addr.rfind(":") + 1] + str(zp + cfg.port_offset())


def _check_ports(v, cfg):
    forbidden = forbidden_ports_passive()
    passive_iq = {5000 + k * 100 for k in range(16)}
    ports = active_port_map(cfg)
    seen = {}
    for name, port in ports.items():
        if not port:
            continue
        where = "[active_output] %s_port" % name if name != "gr_zmq" else "[active_gnuradio] zmq_address"
        if not 1 <= port <= 65535:
            v.err("PORT", "port %d (with instance offset) outside 1..65535" % port, where)
            continue
        if port in forbidden:
            if name == "iq" and port in passive_iq and cfg.output.allow_port_5000:
                v.warn("PORT", "IQ port %d is the passive chain's IQ port (allow_port_5000=1): "
                              "the passive chain must be stopped; a busy port is never stolen" % port,
                       where)
            else:
                v.err("PORT", "port %d is reserved for the passive chain / federation" % port, where)
        if port < 1024:
            v.warn("PORT", "port %d is privileged" % port, where)
        if port in seen:
            v.err("PORT", "port %d collides with %s" % (port, seen[port]), where)
        seen[port] = name


def _check_line_spec(v, spec, cfg, where, allow_bare_sim=False, allow_list=False, n=1):
    spec = (spec or "none").strip()
    toks = [t.strip() for t in spec.split(",")] if allow_list else [spec]
    if allow_list and len(toks) not in (1, n):
        v.err("LIST_LEN", "expected 1 or %d line specs, got %d" % (n, len(toks)), where)
    names = set(cfg.device_names())
    for t in toks:
        low = t.lower()
        if low == "none" or (allow_bare_sim and low == "sim"):
            continue
        if low.startswith("sim:"):
            if not t[4:].strip():
                v.err("LINE_SPEC", "sim:<name> needs a name", where)
            continue
        if low.startswith("file:"):
            if not t[5:].strip():
                v.err("LINE_SPEC", "file:<path> needs a path", where)
            continue
        parts = t.split(":")
        if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3].lower() != "low"):
            v.err("LINE_SPEC", "line spec %r must be none | <device>:<bank>:<mask>[:low] | "
                               "sim:<name> | file:<path>" % t, where)
            continue
        dev, bank, mask = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if dev not in names:
            v.err("LINE_SPEC", "line spec %r names unknown device %r" % (t, dev), where)
        if not bank:
            v.err("LINE_SPEC", "line spec %r has an empty bank" % t, where)
        try:
            m = int(mask, 0)
            if m <= 0 or m >= 1 << 32:
                raise ValueError
        except ValueError:
            v.err("LINE_SPEC", "line spec %r needs a mask in 1..2^32-1" % t, where)


def _light_chain(v, spec, direction, where, tx_lock):
    """Light §11.1 grammar check (``active_frontend.parse_chain`` is authoritative)."""
    kinds = _STAGE_KINDS_TX if direction == "tx" else _STAGE_KINDS_RX
    stages = []
    for ent in (e.strip() for e in (spec or "").split(";")):
        if not ent:
            continue
        parts = [p.strip() for p in ent.split(":")]
        if len(parts) < 3:
            v.err("CHAIN", "stage %r must be kind:name:gain_db[:nf[:p1db[:psat[:max_in[:am_pm]]]]]"
                  % ent, where)
            continue
        kind = parts[0].lower()
        if kind not in kinds:
            v.err("CHAIN", "stage kind %r not valid for %s (valid: %s)"
                  % (kind, direction.upper(), ", ".join(kinds)), where)
            continue
        if len(parts) > 8:
            if not any(parts[8:]):
                # only trailing empty fields: harmless, check the first 8 as usual
                v.warn("CHAIN", "stage %r has %d fields (> 8); the trailing empty fields are "
                                "ignored" % (ent, len(parts)), where)
                parts = parts[:8]
            elif tx_lock:
                v.err("CHAIN", "stage %r has %d fields (at most 8: kind:name:gain_db[:nf[:p1db"
                               "[:psat[:max_in[:am_pm]]]]]); not allowed with TX enabled"
                      % (ent, len(parts)), where)
                stages.append((kind, None, None, None, None))
                continue
            else:
                # Field semantics are ambiguous: leave them to active_frontend.parse_chain.
                v.warn("CHAIN", "stage %r has %d fields (> 8); not checked here, see "
                                "active_frontend.parse_chain" % (ent, len(parts)), where)
                stages.append((kind, None, None, None, None))
                continue
        nums = []
        ok = True
        for p in parts[2:]:
            if p == "":
                nums.append(None)
                continue
            try:
                x = float(p)
                if not math.isfinite(x):
                    raise ValueError
                nums.append(x)
            except ValueError:
                v.err("CHAIN", "stage %r has a non-numeric field %r" % (ent, p), where)
                ok = False
                break
        if not ok:
            continue
        nums += [None] * (6 - len(nums))
        gain, p1db, psat, max_in = nums[0], nums[2], nums[3], nums[4]
        if gain is None:
            v.err("CHAIN", "stage %r needs gain_db" % ent, where)
            continue
        if kind in _PASSIVE_KINDS and gain > 0:
            v.err("CHAIN", "passive stage %r must have gain_db <= 0" % ent, where)
        if p1db is not None and psat is not None and psat < p1db:
            v.err("CHAIN", "stage %r: psat < p1db" % ent, where)
        stages.append((kind, gain, p1db, psat, max_in))
    if tx_lock:
        for kind, _g, p1db, psat, _m in stages:
            if kind in ("amp", "pa") and p1db is None and psat is None:
                v.err("TX_LOCK", "TX enabled: every amp/pa stage needs p1db or psat", where)
        if direction == "rx":
            for kind, _g, _p, _s, max_in in stages:
                if kind in ("lna", "amp"):
                    if max_in is None:
                        v.err("TX_LOCK", "TX enabled: the first active RX stage needs max_in_dbm",
                              where)
                    break
    return stages


def _file_check(v, cfg, sec, key, value):
    if value and not os.path.isfile(cfg.resolve_path(value)):
        v.err("FILE_MISSING", "file not found: %s" % cfg.resolve_path(value), _w(sec, key))


def validate_config(cfg) -> list:
    """Schema-level validation -> ``[Issue]`` (includes ``cfg.parse_issues``).

    Codes: VERSION, ENUM, RANGE, TYPE, LIST_LEN, PORT, REQUIRED, LINE_SPEC,
    CHAIN, FILE_MISSING, OTW_FORMAT, NBAR, COHERENCE_PC_CLOCK, TX_LOCK,
    TX_RADIATE, DUMMY_LOAD, CABLED, LOOPBACK, UNKNOWN_DEVICE, ANTENNA_IGNORED (plus the parse
    warnings UNKNOWN_KEY, BOOL_SPELLING, INLINE_COMMENT).
    """
    v = _V()
    v.issues.extend(cfg.parse_issues)
    n_rx, n_tx = cfg.num_rx(), cfg.num_tx()
    names = set(cfg.device_names())

    # [meta]
    if cfg.meta.active_ini_version != 1:
        v.err("VERSION", "active_ini_version must be 1, got %r" % cfg.meta.active_ini_version,
              _w("meta", "active_ini_version"))

    # [active]
    a = cfg.active
    S = "active"
    v.enum(a.backend, BACKENDS, _w(S, "backend"))
    v.enum(a.mode, MODES, _w(S, "mode"))
    v.enum(a.log_level, LOG_LEVELS, _w(S, "log_level"))
    v.rng(a.instance_id, _w(S, "instance_id"), 0, 15)
    v.rng(a.port_stride, _w(S, "port_stride"), 1, 1000)
    v.rng(a.unit_id, _w(S, "unit_id"), 0, 2 ** 32 - 1)
    if len(a.hardware_id) > 16 or not all(32 <= ord(ch) < 127 for ch in a.hardware_id):
        v.err("RANGE", "hardware_id must be <= 16 printable ASCII characters", _w(S, "hardware_id"))
    if not cfg.devices:
        v.err("REQUIRED", "no devices configured", "[active_devices]")

    # devices (schema part; capability checks are in active_devices)
    for d in cfg.devices:
        S = "active_device:" + d.name
        v.enum(d.role, ROLES, _w(S, "role"))
        v.enum(d.lo_source, LO_SOURCES, _w(S, "lo_source"))
        for key in ("clock_source", "time_source"):
            val = getattr(d, key)
            if val:
                v.enum(val, SOURCE_NAMES[1:], _w(S, key))
        v.rng(d.master_clock_rate_hz, _w(S, "master_clock_rate_hz"), 0)
        v.rng(d.link_ports, _w(S, "link_ports"), 1, 16)
        v.rng(d.link_gbps, _w(S, "link_gbps"), 0, lo_open=True)
        for key in ("rx_channels", "tx_channels"):
            chans = getattr(d, key)
            if any(c < 0 for c in chans):
                v.err("RANGE", "negative channel index", _w(S, key))
    # duplicate channels and several masters are reported by
    # active_devices.validate_device_plan (DEVICE_CHANNEL / DEVICE_ROLE)

    # [active_rf]
    rf = cfg.rf
    S = "active_rf"
    v.rng(rf.center_freq_hz, _w(S, "center_freq_hz"), 0, lo_open=True)
    v.rng(rf.sample_rate_hz, _w(S, "sample_rate_hz"), 0, lo_open=True)
    v.rng(rf.bandwidth_hz, _w(S, "bandwidth_hz"), 0)
    if v.list_len(rf.rx_gain_db, n_rx, _w(S, "rx_gain_db")):
        for g in rf.rx_gain_db:
            v.rng(g, _w(S, "rx_gain_db"), -50, 120)
    v.rng(rf.cpi_samples, _w(S, "cpi_samples"), 1024, 2 ** 22)
    v.enum(rf.otw_format, OTW_FORMATS, _w(S, "otw_format"))
    if rf.otw_format == "sc12":
        v.warn("OTW_FORMAT", "sc12 is not supported by every device/driver", _w(S, "otw_format"))
    if rf.lo_offset_hz and abs(rf.lo_offset_hz) < rf.effective_bandwidth_hz() / 2:
        v.warn("RANGE", "lo_offset_hz inside the occupied band puts the LO leakage in band",
               _w(S, "lo_offset_hz"))

    # [active_timing]
    t = cfg.timing
    S = "active_timing"
    if t.sync_method == "pc_clock":
        v.err("COHERENCE_PC_CLOCK", "sync_method pc_clock cannot align device time (use "
                                    "unknown_pps, next_pps_host or gps_time)", _w(S, "sync_method"))
    else:
        v.enum(t.sync_method, SYNC_METHODS, _w(S, "sync_method"))
    v.enum(t.clock_source, SOURCE_NAMES, _w(S, "clock_source"))
    v.enum(t.time_source, SOURCE_NAMES, _w(S, "time_source"))
    v.enum(t.lo_plan, LO_PLANS, _w(S, "lo_plan"))
    v.enum(t.timed_tune, TIMED_TUNE, _w(S, "timed_tune"))
    v.enum(t.epoch, EPOCHS, _w(S, "epoch"))
    v.rng(t.ref_lock_timeout_s, _w(S, "ref_lock_timeout_s"), 0.5, 60)
    v.rng(t.command_lead_s, _w(S, "command_lead_s"), 0.01, 2)
    if t.stream_lead_s < t.command_lead_s + 0.05 - 1e-12:
        v.err("RANGE", "stream_lead_s must be >= command_lead_s + 0.05", _w(S, "stream_lead_s"))
    v.rng(t.max_pps_skew_ticks, _w(S, "max_pps_skew_ticks"), 0)
    v.rng(t.verify_captures, _w(S, "verify_captures"), 2, 64)
    v.rng(t.max_phase_std_deg, _w(S, "max_phase_std_deg"), 0.01, 30)
    v.rng(t.max_freq_offset_hz, _w(S, "max_freq_offset_hz"), 0, lo_open=True)
    if not t.require_ref_lock:
        v.warn("RANGE", "require_ref_lock = 0: beamformed TX will be refused", _w(S, "require_ref_lock"))
    if t.lo_plan in ("n321_star", "twinrx"):
        if not t.lo_master:
            v.err("REQUIRED", "lo_master is required for lo_plan %s" % t.lo_plan, _w(S, "lo_master"))
    if t.lo_master and t.lo_master not in names:
        v.err("UNKNOWN_DEVICE", "lo_master %r is not a configured device" % t.lo_master,
              _w(S, "lo_master"))

    # [active_geometry]
    g = cfg.geometry
    S = "active_geometry"
    _file_check(v, cfg, S, "geometry_file", g.geometry_file)
    if not g.geometry_file:
        v.enum(g.preset, GEOMETRY_PRESETS, _w(S, "preset"))
        if g.preset in ("ula", "uca"):
            v.rng(g.num_elements, _w(S, "num_elements"), 2, 256)
        else:
            v.rng(g.nx, _w(S, "nx"), 1, 64)
            v.rng(g.ny, _w(S, "ny"), 1, 64)
            if g.nx * g.ny < 2:
                v.err("RANGE", "nx*ny must be >= 2", _w(S, "nx"))
    v.rng(g.spacing_m, _w(S, "spacing_m"), 0)
    v.rng(g.spacing_lambda, _w(S, "spacing_lambda"), 0.05, 10)
    v.rng(g.radius_m, _w(S, "radius_m"), 0)
    v.rng(g.design_freq_hz, _w(S, "design_freq_hz"), 0)
    v.rng(g.axis_az_deg, _w(S, "axis_az_deg"), -360, 360)
    v.rng(g.heading_deg, _w(S, "heading_deg"), -360, 360)
    v.rng(g.pitch_deg, _w(S, "pitch_deg"), -90, 90)
    v.rng(g.roll_deg, _w(S, "roll_deg"), -180, 180)
    v.enum(g.phase_reference, PHASE_REFERENCES, _w(S, "phase_reference"))
    v.rng(g.scan_limit_deg, _w(S, "scan_limit_deg"), 0, 90)
    v.rng(g.f_min_hz, _w(S, "f_min_hz"), 0)
    v.rng(g.f_max_hz, _w(S, "f_max_hz"), 0)
    if g.f_min_hz > 0 and g.f_max_hz > 0 and not g.f_min_hz < g.f_max_hz:
        v.err("RANGE", "f_min_hz must be < f_max_hz", _w(S, "f_min_hz"))
    v.enum(g.element_pattern, ELEMENT_PATTERNS, _w(S, "element_pattern"))
    v.rng(g.element_q, _w(S, "element_q"), 0, lo_open=True)
    v.rng(g.element_size_m, _w(S, "element_size_m"), 0)

    # [active_beam]
    b = cfg.beam
    S = "active_beam"
    v.enum(b.rx_method, RX_BEAM_METHODS, _w(S, "rx_method"))
    v.enum(b.tx_method, TX_BEAM_METHODS, _w(S, "tx_method"))
    for key, val in (("look_el_deg", b.look_el_deg), ("tx_look_el_deg", b.tx_look_el_deg)):
        v.rng(val, _w(S, key), -90, 90)
    for key in ("look_az_deg", "tx_look_az_deg"):
        v.rng(getattr(b, key), _w(S, key), -360, 360)
    v.enum(b.taper, TAPERS, _w(S, "taper"))
    if b.tx_taper:
        v.enum(b.tx_taper, TAPERS, _w(S, "tx_taper"))
    v.rng(b.sll_db, _w(S, "sll_db"), 13, 80)
    v.rng(b.nbar, _w(S, "nbar"), 2)
    if "taylor" in (b.taper, b.effective_tx_taper()) and 13 <= b.sll_db <= 80:
        amp = math.acosh(10 ** (b.sll_db / 20.0)) / math.pi
        if b.nbar < 2 * amp * amp + 0.5:
            v.warn("NBAR", "nbar %d < 2A^2+0.5 = %.2f: the Taylor taper will not reach %g dB"
                   % (b.nbar, 2 * amp * amp + 0.5, b.sll_db), _w(S, "nbar"))
    v.rng(b.diag_loading, _w(S, "diag_loading"), 0)
    v.rng(b.snapshots, _w(S, "snapshots"), 0)
    v.enum(b.steering, STEERING, _w(S, "steering"))
    v.rng(b.focus_range_m, _w(S, "focus_range_m"), 0)
    v.enum(b.normalize, NORMALIZE, _w(S, "normalize"))

    # [active_calibration]
    c = cfg.calibration
    S = "active_calibration"
    v.enum(c.rx_method, RX_CAL_METHODS, _w(S, "rx_method"))
    v.enum(c.tx_method, TX_CAL_METHODS, _w(S, "tx_method"))
    v.enum(c.cal_waveform, CAL_WAVEFORMS, _w(S, "cal_waveform"))
    if c.injection_source != "external":
        k = c.injection_tx_index()
        if k is None:
            v.err("ENUM", "injection_source must be external or tx:<global_tx>",
                  _w(S, "injection_source"))
        elif not 0 <= k < n_tx:
            v.err("RANGE", "injection_source tx:%d but only %d TX channels" % (k, n_tx),
                  _w(S, "injection_source"))
        elif "cabled" not in cfg.tx.allowed_modes:
            v.warn("REQUIRED", "injection_source tx:<k> needs a cabled arm; cabled is not in "
                               "[active_tx] allowed_modes", _w(S, "injection_source"))
    _file_check(v, cfg, S, "injection_paths_file", c.injection_paths_file)
    _file_check(v, cfg, S, "coupler_paths_file", c.coupler_paths_file)
    v.rng(c.beacon_el_deg, _w(S, "beacon_el_deg"), -90, 90)
    if c.rx_method == "beacon":
        has_dir = c.beacon_az_deg is not None and c.beacon_el_deg is not None
        if not has_dir and c.beacon_enu_m is None:
            v.err("REQUIRED", "rx_method beacon needs beacon_az_deg + beacon_el_deg or beacon_enu_m",
                  _w(S, "rx_method"))
        if has_dir and c.beacon_enu_m is not None:
            v.warn("REQUIRED", "both beacon_az/el and beacon_enu_m set; beacon_enu_m is used",
                   _w(S, "beacon_enu_m"))
    if (c.beacon_az_deg is None) != (c.beacon_el_deg is None):
        v.err("REQUIRED", "beacon_az_deg and beacon_el_deg must be set together",
              _w(S, "beacon_az_deg"))
    if c.tx_method.startswith("loopback"):
        if not 0 <= c.loopback_rx_chan < max(n_rx, 0):
            v.err("REQUIRED", "tx_method %s needs loopback_rx_chan in 0..%d"
                  % (c.tx_method, n_rx - 1), _w(S, "loopback_rx_chan"))
    elif c.loopback_rx_chan != -1 and not 0 <= c.loopback_rx_chan < n_rx:
        v.err("RANGE", "loopback_rx_chan %d outside 0..%d" % (c.loopback_rx_chan, n_rx - 1),
              _w(S, "loopback_rx_chan"))
    v.rng(c.cal_samples, _w(S, "cal_samples"), 1024, 2 ** 22)
    v.rng(c.cal_repeats, _w(S, "cal_repeats"), 1, 64)
    v.rng(c.tx_cal_amplitude, _w(S, "tx_cal_amplitude"), 0, cfg.tx.max_digital_amplitude,
          lo_open=True)
    v.rng(c.quality_threshold_db, _w(S, "quality_threshold_db"), None, 0)
    v.rng(c.max_phase_resid_deg, _w(S, "max_phase_resid_deg"), 0, lo_open=True)
    if c.recal_interval_s != 0:
        v.rng(c.recal_interval_s, _w(S, "recal_interval_s"), 10)
    v.rng(c.max_cal_age_s, _w(S, "max_cal_age_s"), 0, lo_open=True)
    v.rng(c.recal_temp_delta_c, _w(S, "recal_temp_delta_c"), 0, lo_open=True)
    v.rng(c.drift_check_interval_s, _w(S, "drift_check_interval_s"), 0)
    v.rng(c.drift_threshold_deg, _w(S, "drift_threshold_deg"), 0, lo_open=True)
    v.rng(c.drive_tolerance_db, _w(S, "drive_tolerance_db"), 0, lo_open=True)

    # [active_frontend]
    fe = cfg.frontend
    S = "active_frontend"
    tx_lock = bool(cfg.tx.en_tx)
    v.rng(fe.device_nf_db, _w(S, "device_nf_db"), 0, 30)
    for key in ("tr_isolation_db", "tx_rx_isolation_db", "lna_off_isolation_db", "p1db_margin_db"):
        v.rng(getattr(fe, key), _w(S, key), 0)
    v.rng(fe.expected_input_dbm, _w(S, "expected_input_dbm"), -200, 60)
    for key in ("tx_power_cal_file", "tx_rx_isolation_file", "s_matrix_file"):
        _file_check(v, cfg, S, key, getattr(fe, key))
    for i, _spec in fe.rx_chain_overrides:
        if not 0 <= i < n_rx:
            v.err("RANGE", "rx_chain.%d: no global RX %d (n_rx = %d)" % (i, i, n_rx),
                  _w(S, "rx_chain.%d" % i))
    for i, _spec in fe.tx_chain_overrides:
        if not 0 <= i < n_tx:
            v.err("RANGE", "tx_chain.%d: no global TX %d (n_tx = %d)" % (i, i, n_tx),
                  _w(S, "tx_chain.%d" % i))
    if fe.en_frontend:
        _light_chain(v, fe.rx_chain, "rx", _w(S, "rx_chain"), tx_lock and n_rx > 0)
        _light_chain(v, fe.tx_chain, "tx", _w(S, "tx_chain"), tx_lock and n_tx > 0)
        for i, spec in fe.rx_chain_overrides:
            _light_chain(v, spec, "rx", _w(S, "rx_chain.%d" % i), tx_lock)
        for i, spec in fe.tx_chain_overrides:
            _light_chain(v, spec, "tx", _w(S, "tx_chain.%d" % i), tx_lock)
    else:
        # A declared chain with en_frontend = 0 would be dropped (rx/tx_chain_spec
        # return ""): the power / EIRP model would see a bare device port, tens of
        # dB below the real (amplified) output, and the PA checks would not run.
        declared = [k for k, spec in (("rx_chain", fe.rx_chain), ("tx_chain", fe.tx_chain))
                    if spec.strip()]
        declared += ["rx_chain.%d" % i for i, spec in fe.rx_chain_overrides if spec.strip()]
        declared += ["tx_chain.%d" % i for i, spec in fe.tx_chain_overrides if spec.strip()]
        if declared:
            msg = ("%s configured but en_frontend = 0: the chain%s would be ignored and the "
                   "power/EIRP model would omit their gain; set en_frontend = 1 or remove them"
                   % (", ".join(declared), "s" if len(declared) > 1 else ""))
            if tx_lock:
                v.err("TX_LOCK", msg, _w(S, "en_frontend"))
            else:
                v.warn("CHAIN", msg, _w(S, "en_frontend"))
    # A declared PA requires pa_enable_line whatever en_frontend says (raw specs).
    tx_has_pa = False
    raw_tx = dict(fe.tx_chain_overrides)
    for i in range(n_tx):
        stages = _light_chain(_V(), raw_tx.get(i, fe.tx_chain), "tx", "", False)
        tx_has_pa = tx_has_pa or any(st[0] == "pa" for st in stages)
    for ent in (e.strip() for e in fe.lna_freq_table.split(";")):
        if not ent:
            continue
        parts = ent.split(":")
        try:
            if not 1 <= len(parts) <= 3:
                raise ValueError
            [float(p) for p in parts]
        except ValueError:
            v.err("RANGE", "malformed lna_freq_table entry %r (fMHz:gain_db:nf_db)" % ent,
                  _w(S, "lna_freq_table"))

    # [active_rf_control]
    rc = cfg.rf_control
    S = "active_rf_control"
    _check_line_spec(v, rc.pa_enable_line, cfg, _w(S, "pa_enable_line"), allow_list=True,
                     n=max(n_tx, 1))
    for key in ("tr_switch_line", "lna_bias_line", "cal_injection_line", "cal_loopback_line",
                "noise_source_line"):
        _check_line_spec(v, getattr(rc, key), cfg, _w(S, key))
    for key in ("interlock", "dummy_load_sense"):
        _check_line_spec(v, getattr(rc, key), cfg, _w(S, key), allow_bare_sim=True)
    pm = rc.power_monitor.strip().lower()
    if not (pm in ("none", "sim") or (pm.startswith("file:") and rc.power_monitor[5:].strip())):
        v.err("ENUM", "power_monitor must be none, sim or file:<path>", _w(S, "power_monitor"))
    v.rng(rc.tr_guard_us, _w(S, "tr_guard_us"), 0)
    v.rng(rc.pa_settle_us, _w(S, "pa_settle_us"), 0)
    v.rng(rc.telemetry_max_age_s, _w(S, "telemetry_max_age_s"), 0, lo_open=True)
    if fe.en_frontend and fe.tr_isolation_db > 0 and rc.tr_switch_line.lower() == "none":
        v.err("REQUIRED", "tr_isolation_db > 0 requires tr_switch_line", _w(S, "tr_switch_line"))

    # [active_tx]
    tx = cfg.tx
    S = "active_tx"
    for m in tx.allowed_modes:
        v.enum(m, TX_MODES, _w(S, "allowed_modes"))
    if len(set(tx.allowed_modes)) != len(tx.allowed_modes):
        v.err("ENUM", "duplicate entry in allowed_modes", _w(S, "allowed_modes"))
    v.enum(tx.authorization_kind, AUTHORIZATION_KINDS, _w(S, "authorization_kind"))
    v.rng(tx.station_id_interval_s, _w(S, "station_id_interval_s"), 0, lo_open=True)
    v.rng(tx.occupied_bw_hz, _w(S, "occupied_bw_hz"), 0)
    v.rng(tx.arm_timeout_s, _w(S, "arm_timeout_s"), 1, 600)
    if tx.max_arm_total_s < tx.arm_timeout_s:
        v.err("RANGE", "max_arm_total_s must be >= arm_timeout_s", _w(S, "max_arm_total_s"))
    v.rng(tx.heartbeat_timeout_s, _w(S, "heartbeat_timeout_s"), 0.1, 30)
    v.rng(tx.max_digital_amplitude, _w(S, "max_digital_amplitude"), 0, 0.9, lo_open=True)
    if v.list_len(tx.max_tx_gain_db, n_tx, _w(S, "max_tx_gain_db"), allow_none=True):
        for gval in tx.max_tx_gain_db:
            v.rng(gval, _w(S, "max_tx_gain_db"), 0)
    v.list_len(tx.max_chain_power_dbm, n_tx, _w(S, "max_chain_power_dbm"))
    for key in ("max_chain_power_dbm",):
        for pv in getattr(tx, key):
            v.rng(pv, _w(S, key), -100, 70)
    v.rng(tx.max_total_power_dbm, _w(S, "max_total_power_dbm"), -100, 80)
    v.rng(tx.max_eirp_dbm, _w(S, "max_eirp_dbm"), -100, 100)
    v.rng(tx.mpe_exclusion_m, _w(S, "mpe_exclusion_m"), 0)
    v.rng(tx.max_burst_s, _w(S, "max_burst_s"), 1e-6, 10)
    v.rng(tx.duty_max, _w(S, "duty_max"), 0, 1, lo_open=True)
    if tx.duty_window_s < 10 * tx.max_burst_s - 1e-12:
        v.err("RANGE", "duty_window_s must be >= 10 * max_burst_s", _w(S, "duty_window_s"))
    v.rng(tx.papr_margin_db, _w(S, "papr_margin_db"), 0)
    v.rng(tx.pa_backoff_db, _w(S, "pa_backoff_db"), 0)
    for key in ("dummy_load_rating_w", "dummy_load_pep_w", "cabled_rx_path_loss_db",
                "loopback_path_loss_db"):
        if v.list_len(getattr(tx, key), n_tx, _w(S, key)):
            for val in getattr(tx, key):
                v.rng(val, _w(S, key), 0)
    v.rng(tx.rx_safe_margin_db, _w(S, "rx_safe_margin_db"), 0)
    v.rng(tx.max_vswr, _w(S, "max_vswr"), 1.1)
    v.rng(tx.max_temp_c, _w(S, "max_temp_c"), -40, 150)
    v.rng(tx.underflow_trip_count, _w(S, "underflow_trip_count"), 1)
    v.rng(tx.underflow_window_s, _w(S, "underflow_window_s"), 0, lo_open=True)
    v.rng(tx.passive_isolation_db, _w(S, "passive_isolation_db"), 0)
    v.rng(tx.min_start_lead_s, _w(S, "min_start_lead_s"), 0)
    if tx.authorization_kind == "amateur" and not tx.callsign:
        v.err("REQUIRED", "authorization_kind amateur requires callsign", _w(S, "callsign"))

    if tx.en_tx:
        if not a.operator:
            v.err("TX_LOCK", "en_tx = 1 requires [active] operator", _w("active", "operator"))
        if not tx.allow_bands:
            v.err("TX_LOCK", "en_tx = 1 requires a non-empty allow_bands", _w(S, "allow_bands"))
        if not tx.allowed_modes:
            v.err("TX_LOCK", "en_tx = 1 requires allowed_modes", _w(S, "allowed_modes"))
        if n_tx == 0:
            v.err("TX_LOCK", "en_tx = 1 but no TX channels are configured", "[active_device:*] tx_channels")
        if a.mode == "rx":
            v.warn("TX_LOCK", "en_tx = 1 with mode rx: TX stays disabled", _w("active", "mode"))
        if tx_has_pa and rc.pa_enable_line.strip().lower() == "none":
            v.err("TX_LOCK", "TX chains contain a pa stage: pa_enable_line is required",
                  _w("active_rf_control", "pa_enable_line"))
        if tx.max_reverse_power_dbm is not None and pm == "none":
            v.err("TX_LOCK", "max_reverse_power_dbm is set but power_monitor = none",
                  _w("active_rf_control", "power_monitor"))
        if pm != "none" and tx.max_reverse_power_dbm is None:
            v.err("TX_LOCK", "power_monitor is configured: max_reverse_power_dbm is required",
                  _w(S, "max_reverse_power_dbm"))
        if "dummy_load" in tx.allowed_modes and len(tx.dummy_load_rating_w) == n_tx:
            if any(r <= 0 for r in tx.dummy_load_rating_w):
                v.err("DUMMY_LOAD", "dummy_load mode requires dummy_load_rating_w > 0 on every chain",
                      _w(S, "dummy_load_rating_w"))
            if len(tx.dummy_load_pep_w) == n_tx:
                for i in range(n_tx):
                    pep = tx.dummy_load_pep_w[i]
                    if 0 < pep < tx.dummy_load_rating_w[i]:
                        v.warn("DUMMY_LOAD", "chain %d: dummy_load_pep_w < dummy_load_rating_w" % i,
                               _w(S, "dummy_load_pep_w"))
        if "cabled" in tx.allowed_modes and len(tx.cabled_rx_path_loss_db) == n_tx:
            if any(x <= 0 for x in tx.cabled_rx_path_loss_db):
                v.err("CABLED", "cabled mode requires cabled_rx_path_loss_db > 0 on every chain",
                      _w(S, "cabled_rx_path_loss_db"))
        if c.tx_method.startswith("loopback") and len(tx.loopback_path_loss_db) == n_tx:
            if any(x <= 0 for x in tx.loopback_path_loss_db):
                v.err("LOOPBACK", "loopback TX cal requires loopback_path_loss_db > 0 on every chain",
                      _w(S, "loopback_path_loss_db"))
    if "radiate" in tx.allowed_modes:
        if not tx.authorization:
            v.err("TX_RADIATE", "radiate requires an authorization reference", _w(S, "authorization"))
        if tx.authorization_kind == "none":
            v.err("TX_RADIATE", "radiate requires authorization_kind", _w(S, "authorization_kind"))
        if not fe.tx_power_cal_file:
            v.err("TX_RADIATE", "radiate requires a measured tx_power_cal_file",
                  _w("active_frontend", "tx_power_cal_file"))
        if rc.interlock.strip().lower() == "none":
            v.err("TX_RADIATE", "radiate requires an interlock", _w("active_rf_control", "interlock"))
        if not tx.mpe_exclusion_m > 0:
            v.err("TX_RADIATE", "radiate requires mpe_exclusion_m > 0", _w(S, "mpe_exclusion_m"))
        if not t.require_ref_lock:
            v.err("TX_RADIATE", "radiate requires require_ref_lock = 1",
                  _w("active_timing", "require_ref_lock"))
        if tx.require_power_monitor_for_radiate and pm == "none":
            v.err("TX_RADIATE", "radiate requires a power monitor",
                  _w("active_rf_control", "power_monitor"))
        if tx.protect_passive_chain and not tx.passive_isolation_db > 0:
            v.err("TX_RADIATE", "radiate with protect_passive_chain requires passive_isolation_db > 0",
                  _w(S, "passive_isolation_db"))
    if cfg.antenna_ignored_keys:
        msg = ("[active_antenna] %s set but en_antenna_profile = 0: the antenna profile is "
               "ignored, so EIRP and MPE limits would omit the antenna gain; set "
               "en_antenna_profile = 1 or remove the keys" % ", ".join(cfg.antenna_ignored_keys))
        if "radiate" in tx.allowed_modes:
            v.err("ANTENNA_IGNORED", msg, _w("active_antenna", "en_antenna_profile"))
        else:
            v.warn("ANTENNA_IGNORED", msg, _w("active_antenna", "en_antenna_profile"))
    if tx.conducted_ignore_builtin_deny and "radiate" in tx.allowed_modes:
        v.warn("TX_RADIATE", "conducted_ignore_builtin_deny never applies to radiate",
               _w(S, "conducted_ignore_builtin_deny"))

    # [active_output]
    o = cfg.output
    S = "active_output"
    v.enum(o.output_mode, OUTPUT_MODES, _w(S, "output_mode"))
    v.enum(o.uncalibrated_frames, UNCALIBRATED_FRAMES, _w(S, "uncalibrated_frames"))
    v.rng(o.queue_len, _w(S, "queue_len"), 1, 64)
    for key in ("iq_port", "control_port", "status_port", "event_port"):
        v.rng(getattr(o, key), _w(S, key), 0, 65535)
    if len(o.beams) > MAX_HEADER_CHANNELS:
        v.err("RANGE", "at most %d beams" % MAX_HEADER_CHANNELS, _w(S, "beams"))
    if not o.listen_address:
        v.err("REQUIRED", "listen_address must not be empty", _w(S, "listen_address"))
    _check_ports(v, cfg)

    # [active_gnuradio]
    gr = cfg.gnuradio
    S = "active_gnuradio"
    v.enum(gr.apply_cal_in, APPLY_CAL_IN, _w(S, "apply_cal_in"))
    v.rng(gr.start_timeout_s, _w(S, "start_timeout_s"), 0, 600, lo_open=True)
    if not (_zmq_port(gr.zmq_address) or gr.zmq_address.startswith("ipc://")):
        v.err("ENUM", "zmq_address must be tcp://<host>:<port> or ipc://<path>",
              _w(S, "zmq_address"))

    # [active_sim]
    sm = cfg.sim
    S = "active_sim"
    v.rng(sm.seed, _w(S, "seed"), 0)
    v.enum(sm.tx_load, SIM_TX_LOADS, _w(S, "tx_load"))
    for e in sm.emitters:
        v.rng(e.el_deg, _w(S, "emitters"), -90, 90)
        v.rng(e.range_m, _w(S, "emitters"), 0)
        v.enum(e.waveform, SIM_WAVEFORMS, _w(S, "emitters"))
    for dn in sm.iq_conjugate_devices:
        if dn not in names:
            v.err("UNKNOWN_DEVICE", "iq_conjugate_devices names unknown device %r" % dn,
                  _w(S, "iq_conjugate_devices"))
    v.rng(sm.pps_skew_ticks, _w(S, "pps_skew_ticks"), -10 ** 9, 10 ** 9)
    v.rng(sm.turnaround_samples, _w(S, "turnaround_samples"), 0)
    v.rng(sm.load_vswr, _w(S, "load_vswr"), 1.0)
    for key in ("rx_delay_spread_samples", "tx_delay_spread_samples", "rx_gain_sigma_db",
                "tx_gain_sigma_db", "ref_ppm_sigma", "injection_path_sigma_deg",
                "coupler_path_sigma_deg", "heat_c_per_w"):
        v.rng(getattr(sm, key), _w(S, key), 0)
    return v.issues


# ---------------------------------------------------------------------------
# Serialisation and fingerprint
# ---------------------------------------------------------------------------
def config_to_dict(cfg) -> dict:
    """JSON-ready nested dict of the whole configuration."""
    d = {"path": cfg.path, "base_dir": cfg.base_dir}
    for f in dataclasses.fields(cfg):
        if f.name in ("path", "base_dir"):
            continue
        val = getattr(cfg, f.name)
        if f.name == "parse_issues":
            d[f.name] = ac.issues_to_json(val)
        else:
            d[f.name] = ac.to_jsonable(val)
    return d


def aux_file_hashes(cfg) -> dict:
    """``{"<section>.<key>": sha256 | "" (unset) | "missing:<path>"}``."""
    out = {}
    for sec, key in _AUX_FILES:
        val = getattr(getattr(cfg, sec), key)
        name = "%s.%s" % (sec, key)
        if not val:
            out[name] = ""
            continue
        p = cfg.resolve_path(val)
        try:
            out[name] = ac.sha256_file(p)
        except OSError:
            out[name] = "missing:" + p
    return out


def config_fingerprint(cfg) -> str:
    """sha256 of the canonical JSON of the safety/coherence-relevant config plus
    the sha256 of every referenced auxiliary file.

    Excludes ``[active_output]``, ``[active_gnuradio] python/flowgraph_dir/
    start_timeout_s``, ``[active] log_level/state_dir/log_dir``, the file
    location and the parse warnings. Any change in a safety-relevant value or
    table changes the fingerprint (and so invalidates an arm).
    """
    d = config_to_dict(cfg)
    for k in ("path", "base_dir", "parse_issues", "output"):
        d.pop(k, None)
    for k in ("python", "flowgraph_dir", "start_timeout_s"):
        d["gnuradio"].pop(k, None)
    for k in ("log_level", "state_dir", "log_dir"):
        d["active"].pop(k, None)
    doc = {"config": d, "aux": aux_file_hashes(cfg)}
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=True)
    return ac.sha256_bytes(blob.encode("utf-8"))
