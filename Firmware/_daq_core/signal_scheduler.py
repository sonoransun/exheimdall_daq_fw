"""
    Dynamic Signal Scheduler

    Automated frequency hopping/scanning with calibration-aware dwell timing.
    Designed to integrate with hw_controller.py's main loop.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import json
import logging
from dataclasses import dataclass, field
from configparser import ConfigParser


@dataclass
class ScheduleEntry:
    """Single entry in a frequency schedule"""
    frequency: int  # Hz
    gains: list = None  # R820T tenths-of-dB per channel, or None for no change
    dwell_frames: int = 100  # Number of DATA frames to dwell
    require_cal: bool = True  # Wait for calibration before counting dwell


@dataclass
class Schedule:
    """A complete frequency scanning schedule"""
    name: str = "default"
    entries: list = field(default_factory=list)
    repeat_mode: str = "loop"  # "loop", "once", "pingpong"
    active: bool = True
    current_index: int = 0
    frames_at_current: int = 0


class SignalScheduler:
    """
    Frequency hopping scheduler that integrates into the HWC main loop.

    Called once per frame via tick(). Returns command tuples when a
    frequency/gain transition is due.

    States:
        IDLE              - No active schedule
        ACTIVE_DWELLING   - Counting DATA frames at current frequency
        ACTIVE_WAITING_CAL - Waiting for calibration after frequency change
    """

    STATE_IDLE = "IDLE"
    STATE_DWELLING = "ACTIVE_DWELLING"
    STATE_WAITING_CAL = "ACTIVE_WAITING_CAL"

    def __init__(self, M, sample_rate=2400000, cpi_size=1048576, decimation_ratio=1):
        self.logger = logging.getLogger(__name__)
        self.M = M
        self.sample_rate = sample_rate
        self.cpi_size = cpi_size
        self.decimation_ratio = decimation_ratio

        self.schedule = None
        self.state = self.STATE_IDLE
        self.max_cal_wait_frames = 500
        self.cal_wait_counter = 0
        self._pingpong_forward = True  # Direction for pingpong mode

    def load_schedule(self, schedule):
        """Load and activate a schedule"""
        if not schedule.entries:
            self.logger.warning("Empty schedule, ignoring")
            return
        self.schedule = schedule
        self.schedule.active = True
        self.schedule.current_index = 0
        self.schedule.frames_at_current = 0
        self.state = self.STATE_WAITING_CAL
        self.cal_wait_counter = 0
        self._pingpong_forward = True
        self.logger.info("Schedule '{:s}' loaded with {:d} entries".format(
            schedule.name, len(schedule.entries)))

    def clear_schedule(self):
        """Stop and clear the active schedule"""
        self.schedule = None
        self.state = self.STATE_IDLE
        self.cal_wait_counter = 0
        self.logger.info("Schedule cleared")

    def skip_to_next(self):
        """Skip to the next schedule entry"""
        if self.schedule is None or not self.schedule.active:
            return
        self._advance_index()
        self.schedule.frames_at_current = 0
        self.state = self.STATE_WAITING_CAL
        self.cal_wait_counter = 0

    def get_status(self):
        """Return current scheduler status as a dict"""
        if self.schedule is None:
            return {"state": self.state, "active": False}
        entry = self.schedule.entries[self.schedule.current_index]
        return {
            "state": self.state,
            "active": self.schedule.active,
            "name": self.schedule.name,
            "current_index": self.schedule.current_index,
            "total_entries": len(self.schedule.entries),
            "current_freq": entry.frequency,
            "frames_at_current": self.schedule.frames_at_current,
            "dwell_frames": entry.dwell_frames,
            "repeat_mode": self.schedule.repeat_mode,
            "cal_wait_counter": self.cal_wait_counter,
        }

    def tick(self, iq_header):
        """
        Called once per frame in STATE_IQ_CAL of hw_controller.

        Returns:
            tuple (command_str, param_list) when a transition is due, e.g.:
                ("FREQ", [frequency])
                ("GAIN", [g0, g1, ...])
            None if no action needed
        """
        if self.schedule is None or not self.schedule.active:
            return None
        if self.state == self.STATE_IDLE:
            return None

        # Don't transition during active calibration bursts
        if iq_header.noise_source_state == 1:
            return None

        entry = self.schedule.entries[self.schedule.current_index]

        if self.state == self.STATE_WAITING_CAL:
            self.cal_wait_counter += 1

            # Check if calibration completed (sync_state >= 6 means TRACK mode)
            if not entry.require_cal or iq_header.sync_state >= 6:
                self.state = self.STATE_DWELLING
                self.schedule.frames_at_current = 0
                self.cal_wait_counter = 0
                self.logger.info("Calibration done at {:d} Hz, starting dwell".format(
                    entry.frequency))
                return None

            # Timeout: skip this entry if calibration stalls
            if self.cal_wait_counter >= self.max_cal_wait_frames:
                self.logger.warning("Cal wait timeout at {:d} Hz after {:d} frames, skipping".format(
                    entry.frequency, self.cal_wait_counter))
                return self._do_transition()
            return None

        if self.state == self.STATE_DWELLING:
            # Only count DATA frames against dwell
            if iq_header.frame_type == 0:  # FRAME_TYPE_DATA
                self.schedule.frames_at_current += 1

            if self.schedule.frames_at_current >= entry.dwell_frames:
                return self._do_transition()
            return None

        return None

    def _do_transition(self):
        """Advance to next entry and issue commands"""
        self._advance_index()

        if not self.schedule.active:
            self.state = self.STATE_IDLE
            self.logger.info("Schedule '{:s}' completed".format(self.schedule.name))
            return None

        entry = self.schedule.entries[self.schedule.current_index]
        self.schedule.frames_at_current = 0
        self.state = self.STATE_WAITING_CAL
        self.cal_wait_counter = 0

        self.logger.info("Transition to freq {:d} Hz (entry {:d}/{:d})".format(
            entry.frequency, self.schedule.current_index + 1,
            len(self.schedule.entries)))

        # Return FREQ command; GAIN will be issued on next tick if needed
        return ("FREQ", entry.frequency)

    def get_pending_gain(self):
        """Check if current entry has gains to apply after FREQ command"""
        if self.schedule is None or not self.schedule.active:
            return None
        entry = self.schedule.entries[self.schedule.current_index]
        if entry.gains is not None:
            return ("GAIN", entry.gains)
        return None

    def _advance_index(self):
        """Advance the schedule index according to repeat mode"""
        if self.schedule.repeat_mode == "loop":
            self.schedule.current_index = (self.schedule.current_index + 1) % len(self.schedule.entries)
        elif self.schedule.repeat_mode == "once":
            if self.schedule.current_index < len(self.schedule.entries) - 1:
                self.schedule.current_index += 1
            else:
                self.schedule.active = False
        elif self.schedule.repeat_mode == "pingpong":
            if self._pingpong_forward:
                if self.schedule.current_index < len(self.schedule.entries) - 1:
                    self.schedule.current_index += 1
                else:
                    self._pingpong_forward = False
                    if self.schedule.current_index > 0:
                        self.schedule.current_index -= 1
                    else:
                        self.schedule.active = False
            else:
                if self.schedule.current_index > 0:
                    self.schedule.current_index -= 1
                else:
                    self._pingpong_forward = True
                    if self.schedule.current_index < len(self.schedule.entries) - 1:
                        self.schedule.current_index += 1
                    else:
                        self.schedule.active = False


class ScheduleParser:
    """Parse schedule definitions from INI config or JSON"""

    @staticmethod
    def from_ini_section(parser, section_name="schedule"):
        """Parse a schedule from a ConfigParser [schedule] section"""
        if not parser.has_section(section_name):
            return None

        freq_str = parser.get(section_name, 'frequencies', fallback='')
        if not freq_str.strip():
            return None

        frequencies = [int(f.strip()) for f in freq_str.split(',')]

        dwell_str = parser.get(section_name, 'dwell_frames', fallback='')
        if dwell_str.strip():
            dwell_frames = [int(d.strip()) for d in dwell_str.split(',')]
        else:
            dwell_frames = [100] * len(frequencies)

        # Pad dwell_frames if shorter than frequencies
        while len(dwell_frames) < len(frequencies):
            dwell_frames.append(dwell_frames[-1] if dwell_frames else 100)

        gains_str = parser.get(section_name, 'gains', fallback='')
        gains_list = []
        if gains_str.strip():
            # Format: "g0,g1,g2;g0,g1,g2;..." semicolon-separated per entry
            for entry_gains_str in gains_str.split(';'):
                entry_gains_str = entry_gains_str.strip()
                if entry_gains_str:
                    gains_list.append([int(g.strip()) for g in entry_gains_str.split(',')])
                else:
                    gains_list.append(None)

        repeat_mode = parser.get(section_name, 'repeat_mode', fallback='loop')
        require_cal = parser.getboolean(section_name, 'require_cal_on_hop', fallback=True)
        name = parser.get(section_name, 'schedule_name', fallback='ini_schedule')

        entries = []
        for i, freq in enumerate(frequencies):
            gains = gains_list[i] if i < len(gains_list) else None
            dwell = dwell_frames[i] if i < len(dwell_frames) else 100
            entries.append(ScheduleEntry(
                frequency=freq,
                gains=gains,
                dwell_frames=dwell,
                require_cal=require_cal
            ))

        return Schedule(name=name, entries=entries, repeat_mode=repeat_mode)

    @staticmethod
    def from_json(json_str):
        """Parse a schedule from a JSON string"""
        data = json.loads(json_str)
        entries = []
        for entry_data in data.get('entries', []):
            entries.append(ScheduleEntry(
                frequency=int(entry_data['frequency']),
                gains=entry_data.get('gains'),
                dwell_frames=int(entry_data.get('dwell_frames', 100)),
                require_cal=entry_data.get('require_cal', True)
            ))
        return Schedule(
            name=data.get('name', 'json_schedule'),
            entries=entries,
            repeat_mode=data.get('repeat_mode', 'loop')
        )

    @staticmethod
    def from_file(filepath):
        """Load schedule from a JSON file"""
        with open(filepath, 'r') as f:
            return ScheduleParser.from_json(f.read())
