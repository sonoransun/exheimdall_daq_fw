"""
    Dynamic Antenna Orientation Controller

    Slews a directional / high-gain antenna (or the whole array) to a commanded
    azimuth/elevation. Patterned on signal_scheduler.py: a stateful controller
    with STATE_* constants, a tick(iq_header) called once per frame from the
    hw_controller main loop, dataclass config, and an INI/JSON parser. It owns a
    rotator backend (see rotator_controller.py) which is injected so tests can
    pass a mock.

    Bearing sources:
      - external DF app / manual command (set_bearing) via a TCP control command
      - a fixed/park config bearing
      - autonomous scan-and-peak (start_scan): sweep a grid and settle on the
        bearing of maximum received power (the aggregate-power objective that
        delay_sync stamps into the IQ header reserved region).

    Motion is gated on the delay-sync sync_state and noise-source state so the
    array is never slewed mid-calibration.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Bearing:
    az_deg: float = 0.0
    el_deg: float = 0.0


@dataclass
class ScanGrid:
    az_start: float = 0.0
    az_stop: float = 350.0
    az_step: float = 10.0
    el_start: float = 0.0
    el_stop: float = 0.0
    el_step: float = 10.0
    dwell_frames: int = 20

    def points(self):
        """Yield (az, el) grid points (elevation outer, azimuth inner)."""
        el = self.el_start
        el_step = self.el_step if self.el_step > 0 else 1.0
        az_step = self.az_step if self.az_step > 0 else 1.0
        while el <= self.el_stop + 1e-9:
            az = self.az_start
            while az <= self.az_stop + 1e-9:
                yield (round(az, 3), round(el, 3))
                az += az_step
            el += el_step


@dataclass
class OrientationConfig:
    backend: str = "mock"
    device: str = "/dev/ttyUSB0"
    baud: int = 9600
    i2c_bus: int = 1
    i2c_address: int = 0x40
    az_pwm_channel: int = 0
    el_pwm_channel: int = 1
    az_min_deg: float = 0.0
    az_max_deg: float = 360.0
    el_min_deg: float = 0.0
    el_max_deg: float = 90.0
    az_park_deg: float = 0.0
    el_park_deg: float = 0.0
    slew_rate_dps: float = 6.0
    settle_frames: int = 5
    position_tolerance_deg: float = 1.0
    home_on_start: bool = False
    bearing_mode: str = "external"
    min_sync_state: int = 5
    en_scan: bool = False
    scan: ScanGrid = field(default_factory=ScanGrid)


def _signed_mdb(raw):
    """Interpret a uint32 milli-dB value as signed."""
    raw = int(raw)
    return raw - (1 << 32) if raw >= (1 << 31) else raw


class OrientationController:
    """Antenna orientation state machine, ticked once per IQ frame."""

    STATE_IDLE = "IDLE"
    STATE_SLEWING = "SLEWING"
    STATE_SETTLED = "SETTLED"
    STATE_SCANNING = "SCANNING"

    # Rotator-state enum stamped into the IQ header reserved slot
    _STATE_ENUM = {STATE_IDLE: 0, STATE_SLEWING: 1, STATE_SETTLED: 2, STATE_SCANNING: 3}

    def __init__(self, config, backend, M, antenna_profile=None):
        self.logger = logging.getLogger(__name__)
        self.config = config
        self.backend = backend
        self.M = M
        self.antenna_profile = antenna_profile

        self.state = self.STATE_IDLE
        self.cmd_az = config.az_park_deg      # commanded electrical bearing
        self.cmd_el = config.el_park_deg
        self._settle_counter = 0

        # Scan state
        self._scan_points = []
        self._scan_index = 0
        self._scan_dwell_counter = 0
        self._scan_best_obj = None
        self._scan_best_bearing = None
        self._scan_grid = None

    # ---- public API -------------------------------------------------------
    def open(self):
        """Open the backend and optionally home on start."""
        if self.config.home_on_start:
            self.home()
        if self.config.bearing_mode == "fixed":
            self.set_bearing(self.config.az_park_deg, self.config.el_park_deg)

    def close(self):
        try:
            self.backend.close()
        except Exception as e:
            self.logger.error("Rotator backend close failed: %s", e)

    def _clamp_bearing(self, az, el):
        az = max(self.config.az_min_deg, min(self.config.az_max_deg, az))
        el = max(self.config.el_min_deg, min(self.config.el_max_deg, el))
        return az, el

    def set_bearing(self, az_deg, el_deg):
        """Slew so the antenna electrical boresight points at (az, el)."""
        az, el = self._clamp_bearing(az_deg, el_deg)
        limited = (az != az_deg) or (el != el_deg)
        self.cmd_az, self.cmd_el = az, el
        # Apply the mechanical boresight offset to get the mechanism set-point
        mech_az, mech_el = az, el
        if self.antenna_profile is not None:
            mech_az, mech_el = self.antenna_profile.apply_boresight(az, el)
        self.backend.move_to(mech_az, mech_el)
        self.state = self.STATE_SLEWING
        self._settle_counter = 0
        self.logger.info("Slewing to az=%.2f el=%.2f (mech az=%.2f el=%.2f)", az, el, mech_az, mech_el)
        return limited

    def park(self):
        self.set_bearing(self.config.az_park_deg, self.config.el_park_deg)

    def stop(self):
        try:
            self.backend.stop()
        except Exception as e:
            self.logger.error("Rotator stop failed: %s", e)
        self.state = self.STATE_SETTLED
        self._scan_points = []

    def home(self):
        try:
            self.backend.home()
        except Exception as e:
            self.logger.error("Rotator home failed: %s", e)
        self.cmd_az, self.cmd_el = 0.0, 0.0
        self.state = self.STATE_SETTLED

    def start_scan(self, grid=None):
        """Begin an autonomous scan-and-peak over a bearing grid."""
        self._scan_grid = grid or self.config.scan
        self._scan_points = list(self._scan_grid.points())
        if not self._scan_points:
            self.logger.warning("Empty scan grid, ignoring")
            return
        self._scan_index = 0
        self._scan_dwell_counter = 0
        self._scan_best_obj = None
        self._scan_best_bearing = None
        az, el = self._scan_points[0]
        self.set_bearing(az, el)
        self.state = self.STATE_SCANNING
        self.logger.info("Scan-and-peak started over %d points", len(self._scan_points))

    def get_status(self):
        return {
            "state": self.state,
            "backend": self.config.backend,
            "bearing_az_deg": round(self.cmd_az, 2),
            "bearing_el_deg": round(self.cmd_el, 2),
            "position": [round(p, 2) for p in self.backend.get_position()],
            "bearing_mode": self.config.bearing_mode,
            "scan_active": self.state == self.STATE_SCANNING,
            "scan_progress": (self._scan_index, len(self._scan_points)) if self._scan_points else (0, 0),
        }

    def rotator_state_enum(self):
        return self._STATE_ENUM.get(self.state, 0)

    # ---- per-frame tick ---------------------------------------------------
    def _can_move(self, iq_header):
        """Motion is permitted only outside calibration (avoid slewing mid-cal)."""
        if getattr(iq_header, "noise_source_state", 0) == 1:
            return False
        return getattr(iq_header, "sync_state", 0) >= self.config.min_sync_state

    def _read_objective(self, iq_header, power_metric):
        if power_metric is not None:
            return float(power_metric)
        getter = getattr(iq_header, "get_aggregate_power_mdb", None)
        if getter is not None:
            return float(_signed_mdb(getter()))
        return None

    def tick(self, iq_header, power_metric=None):
        """Called once per frame. Returns ("ORIENTATION", {...}) on a state
        transition worth reporting, else None."""
        if self.state == self.STATE_IDLE or self.state == self.STATE_SETTLED:
            return None
        # Freeze motion during calibration
        if not self._can_move(iq_header):
            return None

        is_data = getattr(iq_header, "frame_type", 0) == 0

        if self.state == self.STATE_SLEWING:
            if self.backend.is_moving():
                return None
            # Arrived; count settle frames on DATA frames only
            if is_data:
                self._settle_counter += 1
            if self._settle_counter >= self.config.settle_frames:
                self.state = self.STATE_SETTLED
                self._settle_counter = 0
                self.logger.info("Settled at az=%.2f el=%.2f", self.cmd_az, self.cmd_el)
                return ("ORIENTATION", {"event": "settled",
                                        "az_deg": self.cmd_az, "el_deg": self.cmd_el})
            return None

        if self.state == self.STATE_SCANNING:
            if self.backend.is_moving():
                return None
            # Dwell at the current grid point, sampling the objective
            if is_data:
                self._scan_dwell_counter += 1
                obj = self._read_objective(iq_header, power_metric)
                if obj is not None:
                    bearing = self._scan_points[self._scan_index]
                    if self._scan_best_obj is None or obj > self._scan_best_obj:
                        self._scan_best_obj = obj
                        self._scan_best_bearing = bearing
            if self._scan_dwell_counter >= self._scan_grid.dwell_frames:
                self._scan_dwell_counter = 0
                self._scan_index += 1
                if self._scan_index < len(self._scan_points):
                    az, el = self._scan_points[self._scan_index]
                    self.set_bearing(az, el)
                    self.state = self.STATE_SCANNING  # set_bearing forced SLEWING
                    return None
                # Scan complete: go to the best bearing and report the peak
                best = self._scan_best_bearing or (self.cmd_az, self.cmd_el)
                self.set_bearing(best[0], best[1])  # -> SLEWING -> SETTLED
                self.logger.info("Scan peak at az=%.2f el=%.2f (obj=%s)",
                                 best[0], best[1], self._scan_best_obj)
                return ("ORIENTATION", {"event": "scan_peak",
                                        "az_deg": best[0], "el_deg": best[1],
                                        "objective": self._scan_best_obj})
            return None

        return None


class OrientationParser:
    """Parse an OrientationConfig from an INI [orientation] section or JSON."""

    @staticmethod
    def from_ini_section(parser, section_name="orientation"):
        """Return an OrientationConfig, or None if the section is absent/disabled."""
        if not parser.has_section(section_name):
            return None
        if not parser.getint(section_name, 'en_orientation', fallback=0):
            return None

        def gf(key, default):
            return parser.getfloat(section_name, key, fallback=default)

        def gi(key, default):
            return parser.getint(section_name, key, fallback=default)

        grid = ScanGrid(
            az_start=gf('scan_az_start', 0.0),
            az_stop=gf('scan_az_stop', 350.0),
            az_step=gf('scan_az_step', 10.0),
            el_start=gf('scan_el_start', 0.0),
            el_stop=gf('scan_el_stop', 0.0),
            el_step=gf('scan_el_step', 10.0),
            dwell_frames=gi('scan_dwell_frames', 20),
        )
        return OrientationConfig(
            backend=parser.get(section_name, 'backend', fallback='mock').strip(),
            device=parser.get(section_name, 'device', fallback='/dev/ttyUSB0'),
            baud=gi('baud', 9600),
            i2c_bus=gi('i2c_bus', 1),
            i2c_address=int(str(parser.get(section_name, 'i2c_address', fallback='0x40')), 0),
            az_pwm_channel=gi('az_pwm_channel', 0),
            el_pwm_channel=gi('el_pwm_channel', 1),
            az_min_deg=gf('az_min_deg', 0.0),
            az_max_deg=gf('az_max_deg', 360.0),
            el_min_deg=gf('el_min_deg', 0.0),
            el_max_deg=gf('el_max_deg', 90.0),
            az_park_deg=gf('az_park_deg', 0.0),
            el_park_deg=gf('el_park_deg', 0.0),
            slew_rate_dps=gf('slew_rate_dps', 6.0),
            settle_frames=gi('settle_frames', 5),
            position_tolerance_deg=gf('position_tolerance_deg', 1.0),
            home_on_start=bool(gi('home_on_start', 0)),
            bearing_mode=parser.get(section_name, 'bearing_mode', fallback='external').strip(),
            min_sync_state=gi('min_sync_state', 5),
            en_scan=bool(gi('en_scan', 0)),
            scan=grid,
        )

    @staticmethod
    def from_json(json_str):
        data = json.loads(json_str)
        cfg = OrientationConfig()
        for k, v in data.items():
            if k == 'scan' and isinstance(v, dict):
                cfg.scan = ScanGrid(**v)
            elif hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg
