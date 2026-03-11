"""
    DAQ Database Record Types

    Binary record definitions for BerkeleyDB persistent storage.
    Follows the same struct.pack/struct.unpack pattern as iq_header.py.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import struct
import time

M_MAX = 32  # Maximum number of channels (matches IQ header)

# Record version constants
FRAME_METRICS_VERSION = 1
CAL_HISTORY_VERSION = 1
FREQ_SCAN_VERSION = 1
HW_SNAPSHOT_VERSION = 1
SCHEDULE_STATE_VERSION = 1

# Calibration event types
CAL_EVENT_SAMPLE_CAL_DONE = 0
CAL_EVENT_IQ_CAL_DONE = 1
CAL_EVENT_TRACK_LOCK = 2
CAL_EVENT_TRACK_LOST = 3
CAL_EVENT_FREQ_CHANGE = 4
CAL_EVENT_NOISE_ON = 5
CAL_EVENT_NOISE_OFF = 6
CAL_EVENT_GAIN_CHANGE = 7
CAL_EVENT_CAL_START = 8


class FrameMetricsRecord:
    """Per-frame metrics record stored in frame_metrics.db"""

    # Key format: timestamp_ms (Q) + cpi_index (Q)
    KEY_FORMAT = "QQ"
    KEY_SIZE = struct.calcsize(KEY_FORMAT)

    # Value format: version(B) + frame_type(I) + rf_center_freq(Q) + active_ant_chs(I) +
    #   cpi_length(I) + daq_block_index(I) + sampling_freq(Q) + if_gains[32](I*32) +
    #   delay_sync_flag(I) + iq_sync_flag(I) + sync_state(I) + noise_source_state(I) +
    #   adc_overdrive_flags(I) + num_channels(I) + channel_powers[32](f*32) +
    #   snr_estimate(f) + cal_quality(f)
    VALUE_FORMAT = "B I Q I I I Q " + "I " * M_MAX + "I I I I I I " + "f " * M_MAX + "f f"
    VALUE_FORMAT = VALUE_FORMAT.replace(" ", "")

    def __init__(self):
        self.timestamp_ms = 0
        self.cpi_index = 0
        self.frame_type = 0
        self.rf_center_freq = 0
        self.active_ant_chs = 0
        self.cpi_length = 0
        self.daq_block_index = 0
        self.sampling_freq = 0
        self.if_gains = [0] * M_MAX
        self.delay_sync_flag = 0
        self.iq_sync_flag = 0
        self.sync_state = 0
        self.noise_source_state = 0
        self.adc_overdrive_flags = 0
        self.num_channels = 0
        self.channel_powers = [0.0] * M_MAX
        self.snr_estimate = 0.0
        self.cal_quality = 0.0

    @staticmethod
    def make_key(timestamp_ms, cpi_index):
        return struct.pack(FrameMetricsRecord.KEY_FORMAT, timestamp_ms, cpi_index)

    def to_key(self):
        return self.make_key(self.timestamp_ms, self.cpi_index)

    def to_bytes(self):
        return struct.pack(
            self.VALUE_FORMAT,
            FRAME_METRICS_VERSION,
            self.frame_type, self.rf_center_freq, self.active_ant_chs,
            self.cpi_length, self.daq_block_index, self.sampling_freq,
            *self.if_gains,
            self.delay_sync_flag, self.iq_sync_flag, self.sync_state,
            self.noise_source_state, self.adc_overdrive_flags, self.num_channels,
            *self.channel_powers,
            self.snr_estimate, self.cal_quality
        )

    @classmethod
    def from_bytes(cls, key_data, value_data):
        rec = cls()
        rec.timestamp_ms, rec.cpi_index = struct.unpack(cls.KEY_FORMAT, key_data)
        vals = struct.unpack(cls.VALUE_FORMAT, value_data)
        idx = 0
        _version = vals[idx]; idx += 1
        rec.frame_type = vals[idx]; idx += 1
        rec.rf_center_freq = vals[idx]; idx += 1
        rec.active_ant_chs = vals[idx]; idx += 1
        rec.cpi_length = vals[idx]; idx += 1
        rec.daq_block_index = vals[idx]; idx += 1
        rec.sampling_freq = vals[idx]; idx += 1
        rec.if_gains = list(vals[idx:idx + M_MAX]); idx += M_MAX
        rec.delay_sync_flag = vals[idx]; idx += 1
        rec.iq_sync_flag = vals[idx]; idx += 1
        rec.sync_state = vals[idx]; idx += 1
        rec.noise_source_state = vals[idx]; idx += 1
        rec.adc_overdrive_flags = vals[idx]; idx += 1
        rec.num_channels = vals[idx]; idx += 1
        rec.channel_powers = list(vals[idx:idx + M_MAX]); idx += M_MAX
        rec.snr_estimate = vals[idx]; idx += 1
        rec.cal_quality = vals[idx]; idx += 1
        return rec

    @classmethod
    def from_iq_header(cls, iq_header, channel_powers=None, snr=0.0, cal_quality=0.0):
        rec = cls()
        rec.timestamp_ms = int(time.time() * 1000)
        rec.cpi_index = iq_header.cpi_index
        rec.frame_type = iq_header.frame_type
        rec.rf_center_freq = iq_header.rf_center_freq
        rec.active_ant_chs = iq_header.active_ant_chs
        rec.cpi_length = iq_header.cpi_length
        rec.daq_block_index = iq_header.daq_block_index
        rec.sampling_freq = iq_header.sampling_freq
        rec.if_gains = list(iq_header.if_gains[:M_MAX])
        while len(rec.if_gains) < M_MAX:
            rec.if_gains.append(0)
        rec.delay_sync_flag = iq_header.delay_sync_flag
        rec.iq_sync_flag = iq_header.iq_sync_flag
        rec.sync_state = iq_header.sync_state
        rec.noise_source_state = iq_header.noise_source_state
        rec.adc_overdrive_flags = iq_header.adc_overdrive_flags
        rec.num_channels = iq_header.active_ant_chs
        if channel_powers is not None:
            for i, p in enumerate(channel_powers[:M_MAX]):
                rec.channel_powers[i] = float(p)
        rec.snr_estimate = float(snr)
        rec.cal_quality = float(cal_quality)
        return rec


class CalHistoryRecord:
    """Calibration event record stored in cal_history.db"""

    KEY_FORMAT = "QQ"  # timestamp_ms + event_seq
    KEY_SIZE = struct.calcsize(KEY_FORMAT)

    # Value: version(B) + event_type(I) + rf_center_freq(Q) + sync_state_before(I) +
    #   sync_state_after(I) + num_channels(I) + iq_corrections_re[32](f*32) +
    #   iq_corrections_im[32](f*32) + delays[32](i*32) + sync_failed_cntr(I)
    VALUE_FORMAT = "B I Q I I I " + "f " * M_MAX + "f " * M_MAX + "i " * M_MAX + "I"
    VALUE_FORMAT = VALUE_FORMAT.replace(" ", "")

    def __init__(self):
        self.timestamp_ms = 0
        self.event_seq = 0
        self.event_type = 0
        self.rf_center_freq = 0
        self.sync_state_before = 0
        self.sync_state_after = 0
        self.num_channels = 0
        self.iq_corrections_re = [0.0] * M_MAX
        self.iq_corrections_im = [0.0] * M_MAX
        self.delays = [0] * M_MAX
        self.sync_failed_cntr = 0

    @staticmethod
    def make_key(timestamp_ms, event_seq):
        return struct.pack(CalHistoryRecord.KEY_FORMAT, timestamp_ms, event_seq)

    def to_key(self):
        return self.make_key(self.timestamp_ms, self.event_seq)

    def to_bytes(self):
        return struct.pack(
            self.VALUE_FORMAT,
            CAL_HISTORY_VERSION,
            self.event_type, self.rf_center_freq,
            self.sync_state_before, self.sync_state_after, self.num_channels,
            *self.iq_corrections_re, *self.iq_corrections_im, *self.delays,
            self.sync_failed_cntr
        )

    @classmethod
    def from_bytes(cls, key_data, value_data):
        rec = cls()
        rec.timestamp_ms, rec.event_seq = struct.unpack(cls.KEY_FORMAT, key_data)
        vals = struct.unpack(cls.VALUE_FORMAT, value_data)
        idx = 0
        _version = vals[idx]; idx += 1
        rec.event_type = vals[idx]; idx += 1
        rec.rf_center_freq = vals[idx]; idx += 1
        rec.sync_state_before = vals[idx]; idx += 1
        rec.sync_state_after = vals[idx]; idx += 1
        rec.num_channels = vals[idx]; idx += 1
        rec.iq_corrections_re = list(vals[idx:idx + M_MAX]); idx += M_MAX
        rec.iq_corrections_im = list(vals[idx:idx + M_MAX]); idx += M_MAX
        rec.delays = list(vals[idx:idx + M_MAX]); idx += M_MAX
        rec.sync_failed_cntr = vals[idx]; idx += 1
        return rec


class FreqScanRecord:
    """Per-frequency aggregate record stored in freq_scan.db"""

    KEY_FORMAT = "Q"  # rf_center_freq
    KEY_SIZE = struct.calcsize(KEY_FORMAT)

    # Value: version(B) + last_visit_ts(Q) + total_frames(Q) + total_cal_success(Q) +
    #   total_cal_fail(Q) + avg_snr(f) + avg_cal_quality(f) + num_channels(I) +
    #   last_iq_corrections_re[32](f*32) + last_iq_corrections_im[32](f*32) +
    #   last_delays[32](i*32)
    VALUE_FORMAT = "B Q Q Q Q f f I " + "f " * M_MAX + "f " * M_MAX + "i " * M_MAX
    VALUE_FORMAT = VALUE_FORMAT.replace(" ", "")

    def __init__(self):
        self.rf_center_freq = 0
        self.last_visit_ts = 0
        self.total_frames = 0
        self.total_cal_success = 0
        self.total_cal_fail = 0
        self.avg_snr = 0.0
        self.avg_cal_quality = 0.0
        self.num_channels = 0
        self.last_iq_corrections_re = [0.0] * M_MAX
        self.last_iq_corrections_im = [0.0] * M_MAX
        self.last_delays = [0] * M_MAX

    @staticmethod
    def make_key(rf_center_freq):
        return struct.pack(FreqScanRecord.KEY_FORMAT, rf_center_freq)

    def to_key(self):
        return self.make_key(self.rf_center_freq)

    def to_bytes(self):
        return struct.pack(
            self.VALUE_FORMAT,
            FREQ_SCAN_VERSION,
            self.last_visit_ts, self.total_frames,
            self.total_cal_success, self.total_cal_fail,
            self.avg_snr, self.avg_cal_quality, self.num_channels,
            *self.last_iq_corrections_re, *self.last_iq_corrections_im,
            *self.last_delays
        )

    @classmethod
    def from_bytes(cls, key_data, value_data):
        rec = cls()
        rec.rf_center_freq = struct.unpack(cls.KEY_FORMAT, key_data)[0]
        vals = struct.unpack(cls.VALUE_FORMAT, value_data)
        idx = 0
        _version = vals[idx]; idx += 1
        rec.last_visit_ts = vals[idx]; idx += 1
        rec.total_frames = vals[idx]; idx += 1
        rec.total_cal_success = vals[idx]; idx += 1
        rec.total_cal_fail = vals[idx]; idx += 1
        rec.avg_snr = vals[idx]; idx += 1
        rec.avg_cal_quality = vals[idx]; idx += 1
        rec.num_channels = vals[idx]; idx += 1
        rec.last_iq_corrections_re = list(vals[idx:idx + M_MAX]); idx += M_MAX
        rec.last_iq_corrections_im = list(vals[idx:idx + M_MAX]); idx += M_MAX
        rec.last_delays = list(vals[idx:idx + M_MAX]); idx += M_MAX
        return rec


class HWSnapshotRecord:
    """Hardware state snapshot stored in hw_snapshots.db"""

    KEY_FORMAT = "Q"  # timestamp_ms
    KEY_SIZE = struct.calcsize(KEY_FORMAT)

    # Value: version(B) + rf_center_freq(Q) + noise_source_state(I) +
    #   overdrive_events(I) + num_channels(I) + gains[32](I*32)
    VALUE_FORMAT = "B Q I I I " + "I " * M_MAX
    VALUE_FORMAT = VALUE_FORMAT.replace(" ", "")

    def __init__(self):
        self.timestamp_ms = 0
        self.rf_center_freq = 0
        self.noise_source_state = 0
        self.overdrive_events = 0
        self.num_channels = 0
        self.gains = [0] * M_MAX

    @staticmethod
    def make_key(timestamp_ms):
        return struct.pack(HWSnapshotRecord.KEY_FORMAT, timestamp_ms)

    def to_key(self):
        return self.make_key(self.timestamp_ms)

    def to_bytes(self):
        return struct.pack(
            self.VALUE_FORMAT,
            HW_SNAPSHOT_VERSION,
            self.rf_center_freq, self.noise_source_state,
            self.overdrive_events, self.num_channels,
            *self.gains
        )

    @classmethod
    def from_bytes(cls, key_data, value_data):
        rec = cls()
        rec.timestamp_ms = struct.unpack(cls.KEY_FORMAT, key_data)[0]
        vals = struct.unpack(cls.VALUE_FORMAT, value_data)
        idx = 0
        _version = vals[idx]; idx += 1
        rec.rf_center_freq = vals[idx]; idx += 1
        rec.noise_source_state = vals[idx]; idx += 1
        rec.overdrive_events = vals[idx]; idx += 1
        rec.num_channels = vals[idx]; idx += 1
        rec.gains = list(vals[idx:idx + M_MAX]); idx += M_MAX
        return rec

    @classmethod
    def from_iq_header(cls, iq_header, overdrive_events=0):
        rec = cls()
        rec.timestamp_ms = int(time.time() * 1000)
        rec.rf_center_freq = iq_header.rf_center_freq
        rec.noise_source_state = iq_header.noise_source_state
        rec.overdrive_events = overdrive_events
        rec.num_channels = iq_header.active_ant_chs
        rec.gains = list(iq_header.if_gains[:M_MAX])
        while len(rec.gains) < M_MAX:
            rec.gains.append(0)
        return rec


class ScheduleStateRecord:
    """Schedule state key-value record stored in schedule_state.db"""

    # Key is a variable-length string (e.g., b"current_index")
    # Value: version(B) + value_bytes (variable length)

    def __init__(self):
        self.key = b""
        self.value = b""

    def to_key(self):
        return self.key

    def to_bytes(self):
        return struct.pack("B", SCHEDULE_STATE_VERSION) + self.value

    @classmethod
    def from_bytes(cls, key_data, value_data):
        rec = cls()
        rec.key = key_data
        _version = value_data[0]
        rec.value = value_data[1:]
        return rec
