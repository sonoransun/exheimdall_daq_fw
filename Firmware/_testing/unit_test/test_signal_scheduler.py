"""
    Unit test for the Signal Scheduler module

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import unittest
import json
import sys
from os.path import join, dirname, realpath
from configparser import ConfigParser

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

from signal_scheduler import SignalScheduler, Schedule, ScheduleEntry, ScheduleParser


class MockIQHeader:
    """Mock IQ header for testing"""
    def __init__(self, frame_type=0, sync_state=6, noise_source_state=0, cpi_index=0):
        self.frame_type = frame_type
        self.sync_state = sync_state
        self.noise_source_state = noise_source_state
        self.cpi_index = cpi_index


class TestSignalScheduler(unittest.TestCase):

    def setUp(self):
        self.scheduler = SignalScheduler(M=5)

    def test_idle_returns_none(self):
        """Tick returns None when no schedule is loaded"""
        header = MockIQHeader()
        self.assertIsNone(self.scheduler.tick(header))

    def test_load_schedule(self):
        """Loading a schedule transitions to WAITING_CAL state"""
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=10),
            ScheduleEntry(frequency=200000000, dwell_frames=20),
        ])
        self.scheduler.load_schedule(sched)
        self.assertEqual(self.scheduler.state, SignalScheduler.STATE_WAITING_CAL)

    def test_clear_schedule(self):
        """Clearing schedule transitions to IDLE"""
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=10),
        ])
        self.scheduler.load_schedule(sched)
        self.scheduler.clear_schedule()
        self.assertEqual(self.scheduler.state, SignalScheduler.STATE_IDLE)
        self.assertIsNone(self.scheduler.tick(MockIQHeader()))

    def test_empty_schedule_rejected(self):
        """Loading empty schedule stays in IDLE"""
        sched = Schedule(name="empty", entries=[])
        self.scheduler.load_schedule(sched)
        self.assertEqual(self.scheduler.state, SignalScheduler.STATE_IDLE)

    def test_cal_wait_to_dwelling(self):
        """Transitions from WAITING_CAL to DWELLING when sync_state >= 6"""
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=5),
            ScheduleEntry(frequency=200000000, dwell_frames=5),
        ])
        self.scheduler.load_schedule(sched)

        # Not yet calibrated
        header = MockIQHeader(sync_state=3)
        self.assertIsNone(self.scheduler.tick(header))
        self.assertEqual(self.scheduler.state, SignalScheduler.STATE_WAITING_CAL)

        # Calibration done
        header = MockIQHeader(sync_state=6)
        self.assertIsNone(self.scheduler.tick(header))
        self.assertEqual(self.scheduler.state, SignalScheduler.STATE_DWELLING)

    def test_dwell_counts_only_data_frames(self):
        """Only DATA frames (frame_type=0) count against dwell"""
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=3, require_cal=False),
            ScheduleEntry(frequency=200000000, dwell_frames=3, require_cal=False),
        ])
        self.scheduler.load_schedule(sched)

        # Skip cal wait (require_cal=False)
        self.scheduler.tick(MockIQHeader(sync_state=0))
        self.assertEqual(self.scheduler.state, SignalScheduler.STATE_DWELLING)

        # CAL frame should not count
        self.scheduler.tick(MockIQHeader(frame_type=3))
        self.assertEqual(self.scheduler.schedule.frames_at_current, 0)

        # DUMMY frame should not count
        self.scheduler.tick(MockIQHeader(frame_type=1))
        self.assertEqual(self.scheduler.schedule.frames_at_current, 0)

        # DATA frames count
        self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertEqual(self.scheduler.schedule.frames_at_current, 1)
        self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertEqual(self.scheduler.schedule.frames_at_current, 2)

    def test_frequency_transition(self):
        """After dwell completes, FREQ command is returned"""
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=2, require_cal=False),
            ScheduleEntry(frequency=200000000, dwell_frames=2, require_cal=False),
        ])
        self.scheduler.load_schedule(sched)

        # Enter dwelling
        self.scheduler.tick(MockIQHeader())

        # Dwell 2 DATA frames
        self.assertIsNone(self.scheduler.tick(MockIQHeader(frame_type=0)))
        result = self.scheduler.tick(MockIQHeader(frame_type=0))

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "FREQ")
        self.assertEqual(result[1], 200000000)

    def test_noise_source_defers_transition(self):
        """No transition while noise source is active"""
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=1, require_cal=False),
            ScheduleEntry(frequency=200000000, dwell_frames=1, require_cal=False),
        ])
        self.scheduler.load_schedule(sched)
        self.scheduler.tick(MockIQHeader())  # enter dwelling

        # Would transition, but noise source is on
        result = self.scheduler.tick(MockIQHeader(frame_type=0, noise_source_state=1))
        self.assertIsNone(result)

        # Now noise source off, should transition
        result = self.scheduler.tick(MockIQHeader(frame_type=0, noise_source_state=0))
        self.assertIsNotNone(result)

    def test_loop_mode(self):
        """Loop mode wraps around to first entry"""
        sched = Schedule(name="test", repeat_mode="loop", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=1, require_cal=False),
            ScheduleEntry(frequency=200000000, dwell_frames=1, require_cal=False),
        ])
        self.scheduler.load_schedule(sched)
        self.scheduler.tick(MockIQHeader())  # enter dwelling

        # Complete first entry
        result = self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertEqual(result[1], 200000000)

        # Enter dwelling at second
        self.scheduler.tick(MockIQHeader())

        # Complete second entry -> wraps to first
        result = self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertEqual(result[1], 100000000)

    def test_once_mode(self):
        """Once mode stops after last entry"""
        sched = Schedule(name="test", repeat_mode="once", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=1, require_cal=False),
            ScheduleEntry(frequency=200000000, dwell_frames=1, require_cal=False),
        ])
        self.scheduler.load_schedule(sched)
        self.scheduler.tick(MockIQHeader())  # enter dwelling

        # Complete first entry
        result = self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertEqual(result[1], 200000000)

        # Enter dwelling at second
        self.scheduler.tick(MockIQHeader())

        # Complete second entry -> schedule ends
        result = self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertIsNone(result)
        self.assertFalse(self.scheduler.schedule.active)

    def test_pingpong_mode(self):
        """Pingpong mode reverses direction at ends"""
        sched = Schedule(name="test", repeat_mode="pingpong", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=1, require_cal=False),
            ScheduleEntry(frequency=200000000, dwell_frames=1, require_cal=False),
            ScheduleEntry(frequency=300000000, dwell_frames=1, require_cal=False),
        ])
        self.scheduler.load_schedule(sched)
        self.scheduler.tick(MockIQHeader())  # enter dwelling at 0

        # 0 -> 1
        result = self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertEqual(result[1], 200000000)
        self.scheduler.tick(MockIQHeader())

        # 1 -> 2
        result = self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertEqual(result[1], 300000000)
        self.scheduler.tick(MockIQHeader())

        # 2 -> 1 (reverse)
        result = self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertEqual(result[1], 200000000)
        self.scheduler.tick(MockIQHeader())

        # 1 -> 0 (continue reverse)
        result = self.scheduler.tick(MockIQHeader(frame_type=0))
        self.assertEqual(result[1], 100000000)

    def test_cal_wait_timeout(self):
        """Calibration timeout skips to next entry"""
        self.scheduler.max_cal_wait_frames = 5
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=10, require_cal=True),
            ScheduleEntry(frequency=200000000, dwell_frames=10, require_cal=True),
        ])
        self.scheduler.load_schedule(sched)

        # Send frames with low sync_state (cal never completes)
        header = MockIQHeader(sync_state=2)
        for _ in range(4):
            self.assertIsNone(self.scheduler.tick(header))

        # 5th frame triggers timeout and transition
        result = self.scheduler.tick(header)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "FREQ")
        self.assertEqual(result[1], 200000000)

    def test_get_status(self):
        """Status dict contains expected fields"""
        sched = Schedule(name="test_sched", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=10),
        ])
        self.scheduler.load_schedule(sched)
        status = self.scheduler.get_status()

        self.assertTrue(status['active'])
        self.assertEqual(status['name'], 'test_sched')
        self.assertEqual(status['current_freq'], 100000000)
        self.assertEqual(status['total_entries'], 1)

    def test_skip_to_next(self):
        """skip_to_next advances index and resets state"""
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000, dwell_frames=100, require_cal=False),
            ScheduleEntry(frequency=200000000, dwell_frames=100, require_cal=False),
        ])
        self.scheduler.load_schedule(sched)
        self.scheduler.tick(MockIQHeader())  # enter dwelling
        self.scheduler.tick(MockIQHeader(frame_type=0))  # count 1 frame

        self.scheduler.skip_to_next()
        self.assertEqual(self.scheduler.schedule.current_index, 1)
        self.assertEqual(self.scheduler.state, SignalScheduler.STATE_WAITING_CAL)

    def test_get_pending_gain(self):
        """Pending gain returns entry's gains if set"""
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000, gains=[100, 200, 300, 400, 500]),
        ])
        self.scheduler.load_schedule(sched)
        result = self.scheduler.get_pending_gain()
        self.assertEqual(result[0], "GAIN")
        self.assertEqual(result[1], [100, 200, 300, 400, 500])

    def test_get_pending_gain_none(self):
        """Pending gain returns None when no gains set"""
        sched = Schedule(name="test", entries=[
            ScheduleEntry(frequency=100000000),
        ])
        self.scheduler.load_schedule(sched)
        self.assertIsNone(self.scheduler.get_pending_gain())


class TestScheduleParser(unittest.TestCase):

    def test_from_ini_section(self):
        """Parse schedule from INI config"""
        parser = ConfigParser()
        parser.add_section('schedule')
        parser.set('schedule', 'frequencies', '100000000, 200000000, 300000000')
        parser.set('schedule', 'dwell_frames', '10, 20, 30')
        parser.set('schedule', 'gains', '100,200,300,400,500;110,210,310,410,510;')
        parser.set('schedule', 'repeat_mode', 'loop')
        parser.set('schedule', 'require_cal_on_hop', 'yes')

        sched = ScheduleParser.from_ini_section(parser)
        self.assertIsNotNone(sched)
        self.assertEqual(len(sched.entries), 3)
        self.assertEqual(sched.entries[0].frequency, 100000000)
        self.assertEqual(sched.entries[1].dwell_frames, 20)
        self.assertEqual(sched.entries[0].gains, [100, 200, 300, 400, 500])
        self.assertIsNone(sched.entries[2].gains)
        self.assertEqual(sched.repeat_mode, "loop")

    def test_from_ini_no_section(self):
        """Returns None when section missing"""
        parser = ConfigParser()
        self.assertIsNone(ScheduleParser.from_ini_section(parser))

    def test_from_ini_empty_frequencies(self):
        """Returns None when frequencies empty"""
        parser = ConfigParser()
        parser.add_section('schedule')
        parser.set('schedule', 'frequencies', '')
        self.assertIsNone(ScheduleParser.from_ini_section(parser))

    def test_from_json(self):
        """Parse schedule from JSON string"""
        json_str = json.dumps({
            "name": "json_test",
            "repeat_mode": "once",
            "entries": [
                {"frequency": 433000000, "dwell_frames": 50},
                {"frequency": 868000000, "dwell_frames": 100, "gains": [200, 200, 200, 200, 200]},
            ]
        })
        sched = ScheduleParser.from_json(json_str)
        self.assertEqual(sched.name, "json_test")
        self.assertEqual(sched.repeat_mode, "once")
        self.assertEqual(len(sched.entries), 2)
        self.assertEqual(sched.entries[0].frequency, 433000000)
        self.assertIsNone(sched.entries[0].gains)
        self.assertEqual(sched.entries[1].gains, [200, 200, 200, 200, 200])


if __name__ == '__main__':
    unittest.main()
