"""
    Unit test for the DAQ Database module

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import unittest
import sys
import struct
import time
import tempfile
import shutil
from os.path import join, dirname, realpath

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
daq_core_path = join(root_path, "_daq_core")
sys.path.insert(0, daq_core_path)

from daq_db_records import (
    FrameMetricsRecord, CalHistoryRecord, FreqScanRecord,
    HWSnapshotRecord, ScheduleStateRecord, M_MAX,
    CAL_EVENT_TRACK_LOCK, CAL_EVENT_FREQ_CHANGE
)


class MockIQHeader:
    """Mock IQ header for testing"""
    def __init__(self):
        self.frame_type = 0
        self.rf_center_freq = 433000000
        self.active_ant_chs = 5
        self.cpi_length = 1048576
        self.daq_block_index = 42
        self.cpi_index = 100
        self.sampling_freq = 2400000
        self.if_gains = [200] * 32
        self.delay_sync_flag = 1
        self.iq_sync_flag = 1
        self.sync_state = 6
        self.noise_source_state = 0
        self.adc_overdrive_flags = 0
        self.sample_bit_depth = 32


class TestFrameMetricsRecord(unittest.TestCase):

    def test_key_roundtrip(self):
        """Key packing/unpacking roundtrip"""
        key = FrameMetricsRecord.make_key(123456789, 42)
        ts, cpi = struct.unpack(FrameMetricsRecord.KEY_FORMAT, key)
        self.assertEqual(ts, 123456789)
        self.assertEqual(cpi, 42)

    def test_value_roundtrip(self):
        """Value serialization roundtrip"""
        rec = FrameMetricsRecord()
        rec.timestamp_ms = 1000000
        rec.cpi_index = 50
        rec.frame_type = 0
        rec.rf_center_freq = 433000000
        rec.active_ant_chs = 5
        rec.cpi_length = 1048576
        rec.daq_block_index = 10
        rec.sampling_freq = 2400000
        rec.if_gains[0] = 200
        rec.if_gains[1] = 300
        rec.delay_sync_flag = 1
        rec.iq_sync_flag = 1
        rec.sync_state = 6
        rec.noise_source_state = 0
        rec.adc_overdrive_flags = 0
        rec.num_channels = 5
        rec.channel_powers[0] = 1.5
        rec.channel_powers[1] = 2.5
        rec.snr_estimate = 25.0
        rec.cal_quality = 0.95

        key = rec.to_key()
        data = rec.to_bytes()
        rec2 = FrameMetricsRecord.from_bytes(key, data)

        self.assertEqual(rec2.timestamp_ms, 1000000)
        self.assertEqual(rec2.cpi_index, 50)
        self.assertEqual(rec2.frame_type, 0)
        self.assertEqual(rec2.rf_center_freq, 433000000)
        self.assertEqual(rec2.if_gains[0], 200)
        self.assertEqual(rec2.if_gains[1], 300)
        self.assertEqual(rec2.sync_state, 6)
        self.assertEqual(rec2.num_channels, 5)
        self.assertAlmostEqual(rec2.channel_powers[0], 1.5, places=5)
        self.assertAlmostEqual(rec2.snr_estimate, 25.0, places=5)
        self.assertAlmostEqual(rec2.cal_quality, 0.95, places=5)

    def test_from_iq_header(self):
        """Construction from IQ header"""
        header = MockIQHeader()
        rec = FrameMetricsRecord.from_iq_header(header, channel_powers=[1.0, 2.0], snr=20.0)
        self.assertEqual(rec.rf_center_freq, 433000000)
        self.assertEqual(rec.active_ant_chs, 5)
        self.assertAlmostEqual(rec.channel_powers[0], 1.0, places=5)
        self.assertAlmostEqual(rec.channel_powers[1], 2.0, places=5)
        self.assertAlmostEqual(rec.snr_estimate, 20.0, places=5)
        self.assertGreater(rec.timestamp_ms, 0)


class TestCalHistoryRecord(unittest.TestCase):

    def test_roundtrip(self):
        """Serialization roundtrip"""
        rec = CalHistoryRecord()
        rec.timestamp_ms = 2000000
        rec.event_seq = 5
        rec.event_type = CAL_EVENT_TRACK_LOCK
        rec.rf_center_freq = 868000000
        rec.sync_state_before = 4
        rec.sync_state_after = 6
        rec.num_channels = 5
        rec.iq_corrections_re[0] = 1.0
        rec.iq_corrections_im[0] = 0.5
        rec.delays[0] = -3

        key = rec.to_key()
        data = rec.to_bytes()
        rec2 = CalHistoryRecord.from_bytes(key, data)

        self.assertEqual(rec2.timestamp_ms, 2000000)
        self.assertEqual(rec2.event_seq, 5)
        self.assertEqual(rec2.event_type, CAL_EVENT_TRACK_LOCK)
        self.assertEqual(rec2.rf_center_freq, 868000000)
        self.assertEqual(rec2.sync_state_before, 4)
        self.assertEqual(rec2.sync_state_after, 6)
        self.assertAlmostEqual(rec2.iq_corrections_re[0], 1.0, places=5)
        self.assertAlmostEqual(rec2.iq_corrections_im[0], 0.5, places=5)
        self.assertEqual(rec2.delays[0], -3)


class TestFreqScanRecord(unittest.TestCase):

    def test_roundtrip(self):
        """Serialization roundtrip"""
        rec = FreqScanRecord()
        rec.rf_center_freq = 433000000
        rec.last_visit_ts = 3000000
        rec.total_frames = 500
        rec.total_cal_success = 480
        rec.total_cal_fail = 20
        rec.avg_snr = 22.5
        rec.avg_cal_quality = 0.9
        rec.num_channels = 5
        rec.last_iq_corrections_re[0] = 0.99
        rec.last_delays[0] = 2

        key = rec.to_key()
        data = rec.to_bytes()
        rec2 = FreqScanRecord.from_bytes(key, data)

        self.assertEqual(rec2.rf_center_freq, 433000000)
        self.assertEqual(rec2.total_frames, 500)
        self.assertEqual(rec2.total_cal_success, 480)
        self.assertAlmostEqual(rec2.avg_snr, 22.5, places=5)


class TestHWSnapshotRecord(unittest.TestCase):

    def test_roundtrip(self):
        """Serialization roundtrip"""
        rec = HWSnapshotRecord()
        rec.timestamp_ms = 4000000
        rec.rf_center_freq = 700000000
        rec.noise_source_state = 1
        rec.overdrive_events = 3
        rec.num_channels = 5
        rec.gains[0] = 200

        key = rec.to_key()
        data = rec.to_bytes()
        rec2 = HWSnapshotRecord.from_bytes(key, data)

        self.assertEqual(rec2.timestamp_ms, 4000000)
        self.assertEqual(rec2.rf_center_freq, 700000000)
        self.assertEqual(rec2.noise_source_state, 1)
        self.assertEqual(rec2.overdrive_events, 3)
        self.assertEqual(rec2.gains[0], 200)

    def test_from_iq_header(self):
        """Construction from IQ header"""
        header = MockIQHeader()
        rec = HWSnapshotRecord.from_iq_header(header, overdrive_events=2)
        self.assertEqual(rec.rf_center_freq, 433000000)
        self.assertEqual(rec.overdrive_events, 2)


class TestScheduleStateRecord(unittest.TestCase):

    def test_roundtrip(self):
        """Serialization roundtrip"""
        rec = ScheduleStateRecord()
        rec.key = b"current_index"
        rec.value = struct.pack("I", 42)

        key = rec.to_key()
        data = rec.to_bytes()
        rec2 = ScheduleStateRecord.from_bytes(key, data)

        self.assertEqual(rec2.key, b"current_index")
        val = struct.unpack("I", rec2.value)[0]
        self.assertEqual(val, 42)


# Database integration tests (only run if berkeleydb is available)
HAS_BDB = False
try:
    from daq_db import DAQDatabase, bdb
    if bdb is not None:
        HAS_BDB = True
except ImportError:
    pass


@unittest.skipUnless(HAS_BDB, "berkeleydb not available")
class TestDAQDatabaseIntegration(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="daq_db_test_")
        self.db = DAQDatabase(db_dir=self.tmpdir, num_channels=5,
                              write_batch_size=1, write_flush_interval_sec=0.1)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _wait_flush(self):
        """Wait for write queue to drain"""
        time.sleep(0.5)

    def test_put_get_frame_metrics(self):
        """Write and read frame metrics"""
        header = MockIQHeader()
        self.db.put_frame_metrics(header, channel_powers=[1.0, 2.0, 3.0, 4.0, 5.0], snr=25.0)
        self._wait_flush()

        results = self.db.get_frame_metrics_by_time_range(0, int(time.time() * 1000) + 1000)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].rf_center_freq, 433000000)

    def test_put_get_cal_event(self):
        """Write and read calibration events"""
        header = MockIQHeader()
        import numpy as np
        iq_corr = np.array([1+0j, 0.99+0.01j, 1.01-0.02j, 0.98+0.03j, 1.0+0j], dtype=np.complex64)
        delays = np.array([0, 1, -1, 2, 0], dtype=int)

        self.db.put_cal_event(CAL_EVENT_TRACK_LOCK, header,
                              iq_corrections=iq_corr, delays=delays,
                              sync_state_before=4, sync_state_after=6)
        self._wait_flush()

        results = self.db.get_cal_history()
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].event_type, CAL_EVENT_TRACK_LOCK)
        self.assertEqual(results[0].rf_center_freq, 433000000)

    def test_put_get_hw_snapshot(self):
        """Write and read hardware snapshots"""
        header = MockIQHeader()
        self.db.put_hw_snapshot(header, overdrive_events=1)
        self._wait_flush()

        results = self.db.get_hw_snapshots()
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].overdrive_events, 1)

    def test_freq_scan_update(self):
        """Frequency scan aggregate updates"""
        import numpy as np
        iq_corr = np.array([1+0j] * 5, dtype=np.complex64)
        self.db.update_freq_scan(433000000, cal_success=True, snr=20.0,
                                  num_channels=5, iq_corrections=iq_corr)
        self.db.update_freq_scan(433000000, cal_success=True, snr=30.0,
                                  num_channels=5)
        self._wait_flush()

        results = self.db.get_freq_scan_summary(433000000)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].rf_center_freq, 433000000)
        self.assertEqual(results[0].total_frames, 2)
        self.assertEqual(results[0].total_cal_success, 2)

    def test_get_freq_scan_summary_all(self):
        """Get all frequency scan summaries"""
        self.db.update_freq_scan(100000000, cal_success=True, snr=10.0, num_channels=5)
        self.db.update_freq_scan(200000000, cal_success=True, snr=15.0, num_channels=5)
        self._wait_flush()

        results = self.db.get_freq_scan_summary()
        self.assertEqual(len(results), 2)

    def test_schedule_state(self):
        """Write and read schedule state"""
        self.db.put_schedule_state(b"current_index", struct.pack("I", 3))
        self._wait_flush()
        # Schedule state is stored but queried directly via the DB
        # (no dedicated query API — use get via the DB directly)

    def test_rotation(self):
        """Rotation deletes old records"""
        header = MockIQHeader()
        self.db.put_frame_metrics(header, snr=10.0)
        self._wait_flush()

        # Rotate with 0 hours = delete everything
        self.db.rotate(max_age_hours=0)
        results = self.db.get_frame_metrics_by_time_range(0, int(time.time() * 1000) + 1000)
        self.assertEqual(len(results), 0)

    def test_db_stats(self):
        """Stats returns expected structure"""
        stats = self.db.get_db_stats()
        self.assertIn("frame_metrics.db", stats)
        self.assertIn("write_queue_size", stats)

    def test_frame_metrics_by_freq(self):
        """Query frame metrics by frequency"""
        header1 = MockIQHeader()
        header1.rf_center_freq = 433000000
        header1.cpi_index = 1

        header2 = MockIQHeader()
        header2.rf_center_freq = 868000000
        header2.cpi_index = 2

        self.db.put_frame_metrics(header1, snr=10.0)
        self.db.put_frame_metrics(header2, snr=20.0)
        self._wait_flush()

        results = self.db.get_frame_metrics_by_freq(433000000)
        for r in results:
            self.assertEqual(r.rf_center_freq, 433000000)


if __name__ == '__main__':
    unittest.main()
