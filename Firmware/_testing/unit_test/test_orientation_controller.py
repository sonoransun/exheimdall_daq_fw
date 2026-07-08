"""
    Unit test for the antenna orientation controller (orientation_controller.py)
    and the mock rotator backend (rotator_controller.py).

    No sudo, no hardware — uses MockRotatorBackend + MockIQHeader.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import unittest
import sys
from os.path import join, dirname, realpath
from configparser import ConfigParser

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

from orientation_controller import (OrientationController, OrientationConfig,
                                     OrientationParser, ScanGrid)
from rotator_controller import MockRotatorBackend, create_rotator_backend
from antenna_profile import AntennaProfile


class MockIQHeader:
    def __init__(self, frame_type=0, sync_state=6, noise_source_state=0, agg_power_mdb=0):
        self.frame_type = frame_type
        self.sync_state = sync_state
        self.noise_source_state = noise_source_state
        self._agg = agg_power_mdb

    def get_aggregate_power_mdb(self):
        return self._agg


def make_controller(**cfg_overrides):
    cfg = OrientationConfig(**cfg_overrides)
    backend = MockRotatorBackend(slew_rate_dps=cfg.slew_rate_dps,
                                 tolerance_deg=cfg.position_tolerance_deg)
    return OrientationController(cfg, backend, M=5), backend


class TestMockRotatorBackend(unittest.TestCase):
    def test_move_and_reach(self):
        b = MockRotatorBackend(slew_rate_dps=90.0, tolerance_deg=1.0)
        b.move_to(45.0, 10.0)
        # High slew rate -> reaches within one poll
        self.assertFalse(b.is_moving())
        self.assertEqual(b.get_position(), (45.0, 10.0))

    def test_multi_step_slew(self):
        b = MockRotatorBackend(slew_rate_dps=10.0, tolerance_deg=1.0)
        b.move_to(35.0, 0.0)
        polls = 0
        while b.is_moving() and polls < 100:
            polls += 1
        self.assertGreater(polls, 1)  # took several polls
        self.assertAlmostEqual(b.get_position()[0], 35.0, places=3)

    def test_stop(self):
        b = MockRotatorBackend()
        b.move_to(180.0, 0.0)
        b.stop()
        self.assertFalse(b.is_moving())
        self.assertEqual(b.stop_calls, 1)


class TestOrientationController(unittest.TestCase):
    def test_idle_returns_none(self):
        ctrl, _ = make_controller()
        self.assertIsNone(ctrl.tick(MockIQHeader()))
        self.assertEqual(ctrl.state, OrientationController.STATE_IDLE)

    def test_set_bearing_enters_slewing(self):
        ctrl, backend = make_controller(slew_rate_dps=1.0)
        ctrl.set_bearing(90.0, 20.0)
        self.assertEqual(ctrl.state, OrientationController.STATE_SLEWING)
        self.assertEqual(backend.move_calls[-1], (90.0, 20.0))

    def test_soft_limit_clamp(self):
        ctrl, backend = make_controller(az_min_deg=0, az_max_deg=180,
                                        el_min_deg=0, el_max_deg=45)
        limited = ctrl.set_bearing(400.0, 90.0)
        self.assertTrue(limited)
        self.assertEqual((ctrl.cmd_az, ctrl.cmd_el), (180.0, 45.0))

    def test_boresight_offset_applied(self):
        prof = AntennaProfile(boresight_az_offset_deg=5.0, boresight_el_offset_deg=-2.0)
        cfg = OrientationConfig(slew_rate_dps=90.0)
        backend = MockRotatorBackend(slew_rate_dps=90.0)
        ctrl = OrientationController(cfg, backend, M=5, antenna_profile=prof)
        ctrl.set_bearing(100.0, 10.0)
        # Electrical bearing recorded as commanded; mechanism gets the offset
        self.assertEqual((ctrl.cmd_az, ctrl.cmd_el), (100.0, 10.0))
        self.assertEqual(backend.move_calls[-1], (105.0, 8.0))

    def test_slew_settles(self):
        ctrl, _ = make_controller(slew_rate_dps=90.0, settle_frames=3)
        ctrl.set_bearing(45.0, 0.0)
        events = []
        for _ in range(10):
            ev = ctrl.tick(MockIQHeader(frame_type=0, sync_state=6))
            if ev:
                events.append(ev)
        self.assertEqual(ctrl.state, OrientationController.STATE_SETTLED)
        self.assertTrue(any(e[1]["event"] == "settled" for e in events))

    def test_no_move_during_calibration(self):
        ctrl, _ = make_controller(slew_rate_dps=90.0, settle_frames=1, min_sync_state=5)
        ctrl.set_bearing(45.0, 0.0)
        # sync_state below threshold -> frozen
        for _ in range(5):
            ctrl.tick(MockIQHeader(sync_state=2))
        self.assertEqual(ctrl.state, OrientationController.STATE_SLEWING)
        # noise source active -> frozen
        for _ in range(5):
            ctrl.tick(MockIQHeader(sync_state=6, noise_source_state=1))
        self.assertEqual(ctrl.state, OrientationController.STATE_SLEWING)

    def test_settle_counts_only_data_frames(self):
        ctrl, _ = make_controller(slew_rate_dps=90.0, settle_frames=2)
        ctrl.set_bearing(30.0, 0.0)
        # Non-DATA (dummy) frames must not advance settle
        for _ in range(5):
            ctrl.tick(MockIQHeader(frame_type=1, sync_state=6))
        self.assertEqual(ctrl.state, OrientationController.STATE_SLEWING)
        # DATA frames settle it
        for _ in range(3):
            ctrl.tick(MockIQHeader(frame_type=0, sync_state=6))
        self.assertEqual(ctrl.state, OrientationController.STATE_SETTLED)

    def test_park_and_stop(self):
        ctrl, backend = make_controller(slew_rate_dps=90.0, az_park_deg=12.0, el_park_deg=3.0)
        ctrl.park()
        self.assertEqual((ctrl.cmd_az, ctrl.cmd_el), (12.0, 3.0))
        ctrl.stop()
        self.assertEqual(ctrl.state, OrientationController.STATE_SETTLED)
        self.assertEqual(backend.stop_calls, 1)

    def test_scan_selects_peak(self):
        grid = ScanGrid(az_start=0, az_stop=20, az_step=10,
                        el_start=0, el_stop=0, el_step=10, dwell_frames=2)
        ctrl, _ = make_controller(slew_rate_dps=90.0, settle_frames=1)
        ctrl.start_scan(grid)
        self.assertEqual(ctrl.state, OrientationController.STATE_SCANNING)
        peak_event = None
        for _ in range(60):
            # Synthetic power peaked at az = 10
            power = -abs(ctrl.cmd_az - 10.0)
            ev = ctrl.tick(MockIQHeader(frame_type=0, sync_state=6), power_metric=power)
            if ev and ev[1]["event"] == "scan_peak":
                peak_event = ev
                break
        self.assertIsNotNone(peak_event)
        self.assertEqual(peak_event[1]["az_deg"], 10.0)

    def test_scan_reads_header_objective(self):
        # Objective read from the header when no power_metric is passed
        grid = ScanGrid(az_start=0, az_stop=10, az_step=10, el_stop=0, dwell_frames=1)
        ctrl, _ = make_controller(slew_rate_dps=90.0, settle_frames=1)
        ctrl.start_scan(grid)
        peak = None
        for _ in range(40):
            agg = int(-abs(ctrl.cmd_az - 10.0) * 1000)  # milli-dB, peak at 10
            ev = ctrl.tick(MockIQHeader(frame_type=0, sync_state=6, agg_power_mdb=agg))
            if ev and ev[1]["event"] == "scan_peak":
                peak = ev
                break
        self.assertIsNotNone(peak)
        self.assertEqual(peak[1]["az_deg"], 10.0)

    def test_get_status_fields(self):
        ctrl, _ = make_controller()
        st = ctrl.get_status()
        for key in ("state", "backend", "bearing_az_deg", "bearing_el_deg",
                    "position", "bearing_mode", "scan_active"):
            self.assertIn(key, st)

    def test_rotator_state_enum(self):
        ctrl, _ = make_controller(slew_rate_dps=1.0)
        self.assertEqual(ctrl.rotator_state_enum(), 0)  # IDLE
        ctrl.set_bearing(90.0, 0.0)
        self.assertEqual(ctrl.rotator_state_enum(), 1)  # SLEWING


class TestOrientationParser(unittest.TestCase):
    def test_missing_section(self):
        p = ConfigParser()
        p.read_string("[daq]\ngain = 0\n")
        self.assertIsNone(OrientationParser.from_ini_section(p))

    def test_disabled_section(self):
        p = ConfigParser()
        p.read_string("[orientation]\nen_orientation = 0\n")
        self.assertIsNone(OrientationParser.from_ini_section(p))

    def test_parse_ok(self):
        p = ConfigParser()
        p.read_string(
            "[orientation]\nen_orientation = 1\nbackend = gs232\n"
            "az_min_deg = 0\naz_max_deg = 359\nel_max_deg = 80\n"
            "az_park_deg = 90\nbearing_mode = external\nmin_sync_state = 5\n"
            "en_scan = 1\nscan_az_stop = 350\nscan_dwell_frames = 30\n"
            "i2c_address = 0x40\n")
        cfg = OrientationParser.from_ini_section(p)
        self.assertEqual(cfg.backend, 'gs232')
        self.assertEqual(cfg.az_max_deg, 359.0)
        self.assertEqual(cfg.az_park_deg, 90.0)
        self.assertTrue(cfg.en_scan)
        self.assertEqual(cfg.scan.dwell_frames, 30)
        self.assertEqual(cfg.i2c_address, 0x40)

    def test_from_json(self):
        cfg = OrientationParser.from_json(
            '{"backend": "pwm_servo", "az_max_deg": 270, "scan": {"az_stop": 180, "dwell_frames": 15}}')
        self.assertEqual(cfg.backend, 'pwm_servo')
        self.assertEqual(cfg.az_max_deg, 270)
        self.assertEqual(cfg.scan.dwell_frames, 15)


class TestBackendFactory(unittest.TestCase):
    def test_mock_backend(self):
        cfg = OrientationConfig(backend='mock')
        b = create_rotator_backend(cfg)
        self.assertIsInstance(b, MockRotatorBackend)
        self.assertTrue(b.init_status)

    def test_unavailable_real_backend_falls_back(self):
        # No serial device on the test host -> factory returns a mock, never raises
        cfg = OrientationConfig(backend='gs232', device='/dev/does-not-exist')
        b = create_rotator_backend(cfg)
        self.assertIsInstance(b, MockRotatorBackend)


if __name__ == "__main__":
    unittest.main()
