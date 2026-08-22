"""
    DAQ Database Module — BerkeleyDB Persistent Storage

    Provides non-blocking write API (queued to background thread) and
    synchronous read API for frame metrics, calibration history,
    frequency scan aggregates, hardware snapshots, and schedule state.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import os
import fcntl
import struct
import time
import logging
import threading
import queue

try:
    from berkeleydb import db as bdb
except ImportError:
    try:
        from bsddb3 import db as bdb
    except ImportError:
        bdb = None

from daq_db_records import (
    FrameMetricsRecord, CalHistoryRecord, FreqScanRecord,
    HWSnapshotRecord, ScheduleStateRecord, M_MAX,
    DB_SCHEMA_VERSION, RecordVersionError
)


class DAQDatabase:
    """
    BerkeleyDB-backed persistent storage for DAQ metrics.

    Thread-safe: writes are queued to a background daemon thread.
    Reads are synchronous.
    """

    def __init__(self, db_dir="_db", max_db_size_mb=500,
                 rotation_max_age_hours=168,
                 write_batch_size=50, write_flush_interval_sec=1.0,
                 num_channels=5, read_only=False):
        self.logger = logging.getLogger(__name__)
        self.db_dir = db_dir
        self.max_db_size_mb = max_db_size_mb
        self.rotation_max_age_hours = rotation_max_age_hours
        self.write_batch_size = write_batch_size
        self.write_flush_interval_sec = write_flush_interval_sec
        self.num_channels = num_channels
        self.read_only = read_only
        self.event_bus = None  # Set externally for monitoring integration
        self._closed = False
        self._event_seq = 0
        self._event_seq_lock = threading.Lock()
        self._read_errors = 0

        if bdb is None:
            self.logger.error("BerkeleyDB Python bindings not available (install berkeleydb or bsddb3)")
            self._enabled = False
            return
        self._enabled = True

        # Create DB directory
        if not read_only:
            os.makedirs(self.db_dir, exist_ok=True)
        elif not os.path.isdir(self.db_dir):
            raise RuntimeError("No DAQ database directory at {:s}".format(self.db_dir))

        # Discard databases written by an incompatible schema generation
        self._check_schema_version()

        # Open BDB environment
        env_flags = bdb.DB_INIT_CDB | bdb.DB_INIT_MPOOL | bdb.DB_THREAD
        if not read_only:
            env_flags |= bdb.DB_CREATE
        self._env = bdb.DBEnv()
        self._env.open(self.db_dir, env_flags)

        # Open primary databases
        self._frame_metrics_db = self._open_db("frame_metrics.db", bdb.DB_BTREE)
        self._cal_history_db = self._open_db("cal_history.db", bdb.DB_BTREE)
        self._freq_scan_db = self._open_db("freq_scan.db", bdb.DB_BTREE)
        self._hw_snapshots_db = self._open_db("hw_snapshots.db", bdb.DB_BTREE)
        self._schedule_state_db = self._open_db("schedule_state.db", bdb.DB_BTREE)

        # Open secondary index databases for frame_metrics
        self._idx_freq_db = self._open_secondary(
            "idx_freq.db", self._frame_metrics_db, self._extract_freq)
        self._idx_frame_type_db = self._open_secondary(
            "idx_frame_type.db", self._frame_metrics_db, self._extract_frame_type)
        self._idx_sync_state_db = self._open_secondary(
            "idx_sync_state.db", self._frame_metrics_db, self._extract_sync_state)
        self._idx_time_db = self._open_secondary(
            "idx_time.db", self._frame_metrics_db, self._extract_time)

        # Secondary indices for cal_history
        self._idx_cal_freq_db = self._open_secondary(
            "idx_cal_freq.db", self._cal_history_db, self._extract_cal_freq)
        self._idx_cal_event_db = self._open_secondary(
            "idx_cal_event.db", self._cal_history_db, self._extract_cal_event_type)

        if not read_only:
            # Write queue and background writer thread
            self._write_queue = queue.Queue(maxsize=10000)
            self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True,
                                                    name="daq_db_writer")
            self._writer_thread.start()

            # Run initial rotation
            self.rotate(self.rotation_max_age_hours)
        else:
            self._write_queue = None
            self._writer_thread = None
        self.logger.info("DAQ database opened at {:s}{:s}".format(
            self.db_dir, " (read-only)" if read_only else ""))

    def _check_schema_version(self):
        """Discard database files written by an older/other schema generation.

        Records are rolling telemetry with a bounded retention window, so a
        clean re-create on a schema change is acceptable (and the only safe
        option: v1 records used native-alignment values and little-endian
        keys, unreadable with the current formats).

        The whole check-marker/delete/write-marker sequence is serialized
        with an exclusive flock: delay_sync and hw_controller both open the
        same db_dir at startup, and without the lock the slower process can
        read a stale marker and delete the environment the faster one has
        already re-created.
        """
        lock_path = os.path.join(self.db_dir, "_schema_version.lock")
        lock_f = None
        try:
            lock_f = open(lock_path, "a")
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        except OSError as e:
            self.logger.warning("Schema lock unavailable ({:s}), proceeding unlocked".format(str(e)))
            if lock_f is not None:
                lock_f.close()
                lock_f = None
        try:
            self._check_schema_version_locked()
        finally:
            if lock_f is not None:
                try:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                lock_f.close()

    def _check_schema_version_locked(self):
        """Body of _check_schema_version, run under the schema lock."""
        marker_path = os.path.join(self.db_dir, "_schema_version")
        stored = None
        try:
            with open(marker_path, "r") as f:
                stored = f.read().strip()
        except OSError:
            stored = None

        if stored == str(DB_SCHEMA_VERSION):
            return

        db_files = []
        try:
            for fname in os.listdir(self.db_dir):
                if fname.endswith(".db") or fname.startswith("__db.") or \
                   fname.startswith("log."):
                    db_files.append(fname)
        except OSError:
            db_files = []

        if self.read_only:
            if db_files:
                raise RuntimeError(
                    "DAQ database at {:s} has schema version {} (expected {:d})".format(
                        self.db_dir, stored, DB_SCHEMA_VERSION))
            return

        if db_files:
            self.logger.warning(
                "DB schema version changed ({} -> {:d}), discarding old databases".format(
                    stored, DB_SCHEMA_VERSION))
            for fname in db_files:
                try:
                    os.remove(os.path.join(self.db_dir, fname))
                except OSError as e:
                    self.logger.error("Failed to remove old DB file {:s}: {:s}".format(
                        fname, str(e)))
        try:
            with open(marker_path, "w") as f:
                f.write("{:d}\n".format(DB_SCHEMA_VERSION))
        except OSError as e:
            self.logger.error("Failed to write schema marker: {:s}".format(str(e)))

    def _open_db(self, filename, dbtype):
        d = bdb.DB(self._env)
        flags = bdb.DB_THREAD
        flags |= bdb.DB_RDONLY if self.read_only else bdb.DB_CREATE
        d.open(filename, dbtype=dbtype, flags=flags)
        return d

    def _open_secondary(self, filename, primary_db, key_extractor):
        sec = bdb.DB(self._env)
        sec.set_flags(bdb.DB_DUPSORT)
        flags = bdb.DB_THREAD
        flags |= bdb.DB_RDONLY if self.read_only else bdb.DB_CREATE
        sec.open(filename, dbtype=bdb.DB_BTREE, flags=flags)
        primary_db.associate(sec, key_extractor)
        return sec

    # --- Secondary key extractors ---
    # Value offsets are derived from the record VALUE_FORMATs ('=' packed) in
    # daq_db_records.py; index keys are packed big-endian so BerkeleyDB's
    # memcmp ordering equals numeric ordering.

    @staticmethod
    def _extract_freq(key, data):
        """Extract rf_center_freq from FrameMetricsRecord value"""
        rf_freq = struct.unpack_from("=Q", data, FrameMetricsRecord.OFFSET_RF_CENTER_FREQ)[0]
        return struct.pack(">Q", rf_freq)

    @staticmethod
    def _extract_frame_type(key, data):
        """Extract frame_type from FrameMetricsRecord value"""
        frame_type = struct.unpack_from("=I", data, FrameMetricsRecord.OFFSET_FRAME_TYPE)[0]
        return struct.pack(">I", frame_type)

    @staticmethod
    def _extract_sync_state(key, data):
        """Extract sync_state from FrameMetricsRecord value"""
        sync_state = struct.unpack_from("=I", data, FrameMetricsRecord.OFFSET_SYNC_STATE)[0]
        return struct.pack(">I", sync_state)

    @staticmethod
    def _extract_time(key, data):
        """Extract timestamp_ms from the primary key (already big-endian)"""
        return key[0:8]

    @staticmethod
    def _extract_cal_freq(key, data):
        """Extract rf_center_freq from CalHistoryRecord value"""
        rf_freq = struct.unpack_from("=Q", data, CalHistoryRecord.OFFSET_RF_CENTER_FREQ)[0]
        return struct.pack(">Q", rf_freq)

    @staticmethod
    def _extract_cal_event_type(key, data):
        """Extract event_type from CalHistoryRecord value"""
        event_type = struct.unpack_from("=I", data, CalHistoryRecord.OFFSET_EVENT_TYPE)[0]
        return struct.pack(">I", event_type)

    # --- Write API (non-blocking, queued) ---

    def put_frame_metrics(self, iq_header, channel_powers=None, snr=0.0, cal_quality=0.0):
        if not self._enabled or self._closed or self.read_only:
            return
        rec = FrameMetricsRecord.from_iq_header(iq_header, channel_powers, snr, cal_quality)
        self._enqueue(("frame_metrics", rec.to_key(), rec.to_bytes()))

    def put_cal_event(self, event_type, iq_header, iq_corrections=None,
                      delays=None, sync_state_before=0, sync_state_after=0):
        if not self._enabled or self._closed or self.read_only:
            return
        rec = CalHistoryRecord()
        rec.timestamp_ms = int(time.time() * 1000)
        with self._event_seq_lock:
            rec.event_seq = self._event_seq
            self._event_seq += 1
        rec.event_type = event_type
        rec.rf_center_freq = iq_header.rf_center_freq
        rec.sync_state_before = sync_state_before
        rec.sync_state_after = sync_state_after
        rec.num_channels = iq_header.active_ant_chs
        if iq_corrections is not None:
            for i in range(min(len(iq_corrections), M_MAX)):
                rec.iq_corrections_re[i] = float(iq_corrections[i].real)
                rec.iq_corrections_im[i] = float(iq_corrections[i].imag)
        if delays is not None:
            for i in range(min(len(delays), M_MAX)):
                rec.delays[i] = int(delays[i])
        self._enqueue(("cal_history", rec.to_key(), rec.to_bytes()))

    def update_freq_scan(self, rf_center_freq, cal_success=True, snr=0.0,
                         cal_quality=0.0, iq_corrections=None, delays=None,
                         num_channels=0):
        if not self._enabled or self._closed or self.read_only:
            return
        self._enqueue(("freq_scan_update", rf_center_freq, cal_success, snr,
                        cal_quality, iq_corrections, delays, num_channels))

    def put_hw_snapshot(self, iq_header, overdrive_events=0):
        if not self._enabled or self._closed or self.read_only:
            return
        rec = HWSnapshotRecord.from_iq_header(iq_header, overdrive_events)
        self._enqueue(("hw_snapshot", rec.to_key(), rec.to_bytes()))

    def put_schedule_state(self, key_bytes, value_bytes):
        if not self._enabled or self._closed or self.read_only:
            return
        rec = ScheduleStateRecord()
        rec.key = key_bytes
        rec.value = value_bytes
        self._enqueue(("schedule_state", rec.to_key(), rec.to_bytes()))

    def get_schedule_state(self, key_bytes):
        """Synchronous read of a schedule-state value (or None if absent).

        Note: writes are queued to the background thread, so a value written
        moments ago may not be visible yet.
        """
        if not self._enabled:
            return None
        try:
            data = self._schedule_state_db.get(key_bytes)
        except Exception as e:
            self._count_read_error("get_schedule_state", e)
            return None
        if not data:
            return None
        try:
            return ScheduleStateRecord.from_bytes(key_bytes, data).value
        except (RecordVersionError, struct.error) as e:
            self._count_read_error("get_schedule_state", e)
            return None

    def _enqueue(self, item):
        try:
            self._write_queue.put_nowait(item)
        except queue.Full:
            self.logger.warning("DB write queue full, dropping record")
            if self.event_bus is not None:
                from daq_events import DAQEvent, EVT_DB_QUEUE_FULL
                self.event_bus.emit(DAQEvent(severity="warning", module="daq_db",
                    event_type=EVT_DB_QUEUE_FULL, payload={}))

    # --- Background writer thread ---

    def _writer_loop(self):
        batch = []
        last_flush = time.time()
        last_rotation = time.time()

        while not self._closed:
            try:
                item = self._write_queue.get(timeout=self.write_flush_interval_sec)
                batch.append(item)
            except queue.Empty:
                pass

            now = time.time()
            if len(batch) >= self.write_batch_size or \
               (now - last_flush >= self.write_flush_interval_sec and batch):
                self._flush_batch(batch)
                batch = []
                last_flush = now

            # Hourly rotation
            if now - last_rotation >= 3600:
                self.rotate(self.rotation_max_age_hours)
                last_rotation = now

        # Flush remaining
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch):
        for item in batch:
            try:
                if item[0] == "frame_metrics":
                    self._frame_metrics_db.put(item[1], item[2])
                elif item[0] == "cal_history":
                    self._cal_history_db.put(item[1], item[2])
                elif item[0] == "hw_snapshot":
                    self._hw_snapshots_db.put(item[1], item[2])
                elif item[0] == "schedule_state":
                    self._schedule_state_db.put(item[1], item[2])
                elif item[0] == "freq_scan_update":
                    self._do_freq_scan_update(*item[1:])
            except Exception as e:
                self.logger.error("DB write error: {:s}".format(str(e)))
                if self.event_bus is not None:
                    from daq_events import DAQEvent, EVT_DB_ERROR
                    self.event_bus.emit(DAQEvent(severity="error", module="daq_db",
                        event_type=EVT_DB_ERROR, payload={"error": str(e)}))

    def _do_freq_scan_update(self, rf_center_freq, cal_success, snr,
                              cal_quality, iq_corrections, delays, num_channels):
        key = FreqScanRecord.make_key(rf_center_freq)
        existing = self._freq_scan_db.get(key)
        if existing:
            rec = FreqScanRecord.from_bytes(key, existing)
        else:
            rec = FreqScanRecord()
            rec.rf_center_freq = rf_center_freq

        rec.last_visit_ts = int(time.time() * 1000)
        rec.total_frames += 1
        if cal_success:
            rec.total_cal_success += 1
        else:
            rec.total_cal_fail += 1
        # Running average for SNR and cal_quality
        n = rec.total_frames
        rec.avg_snr = rec.avg_snr * (n - 1) / n + float(snr) / n
        rec.avg_cal_quality = rec.avg_cal_quality * (n - 1) / n + float(cal_quality) / n
        rec.num_channels = num_channels

        if iq_corrections is not None:
            for i in range(min(len(iq_corrections), M_MAX)):
                rec.last_iq_corrections_re[i] = float(iq_corrections[i].real)
                rec.last_iq_corrections_im[i] = float(iq_corrections[i].imag)
        if delays is not None:
            for i in range(min(len(delays), M_MAX)):
                rec.last_delays[i] = int(delays[i])

        self._freq_scan_db.put(key, rec.to_bytes())

    # --- Query API (synchronous reads) ---

    def _count_read_error(self, where, exc):
        self._read_errors += 1
        self.logger.error("DB read error in {:s}: {:s}".format(where, str(exc)))

    def get_frame_metrics_by_time_range(self, start_ts_ms, end_ts_ms, limit=1000):
        if not self._enabled:
            return []
        results = []
        cursor = self._idx_time_db.cursor()
        try:
            start_key = struct.pack(">Q", start_ts_ms)
            # Secondary cursor pget returns (idx_key, primary_key, value)
            rec = cursor.pget(start_key, flags=bdb.DB_SET_RANGE)
            while rec and len(results) < limit:
                idx_key, pkey, value = rec
                ts = struct.unpack(">Q", idx_key)[0]
                if ts > end_ts_ms:
                    break
                try:
                    results.append(FrameMetricsRecord.from_bytes(pkey, value))
                except (RecordVersionError, struct.error) as e:
                    self._count_read_error("get_frame_metrics_by_time_range", e)
                rec = cursor.pget(flags=bdb.DB_NEXT)
        except Exception as e:
            self._count_read_error("get_frame_metrics_by_time_range", e)
        finally:
            cursor.close()
        return results

    def get_frame_metrics_by_freq(self, rf_center_freq, limit=1000):
        if not self._enabled:
            return []
        results = []
        cursor = self._idx_freq_db.cursor()
        try:
            search_key = struct.pack(">Q", rf_center_freq)
            rec = cursor.pget(search_key, flags=bdb.DB_SET)
            while rec and len(results) < limit:
                idx_key, pkey, value = rec
                try:
                    results.append(FrameMetricsRecord.from_bytes(pkey, value))
                except (RecordVersionError, struct.error) as e:
                    self._count_read_error("get_frame_metrics_by_freq", e)
                rec = cursor.pget(flags=bdb.DB_NEXT_DUP)
        except Exception as e:
            self._count_read_error("get_frame_metrics_by_freq", e)
        finally:
            cursor.close()
        return results

    def get_frames_with_sync_lost(self, limit=1000):
        if not self._enabled:
            return []
        results = []
        cursor = self._idx_sync_state_db.cursor()
        try:
            rec = cursor.pget(flags=bdb.DB_FIRST)
            while rec and len(results) < limit:
                idx_key, pkey, value = rec
                sync_state = struct.unpack(">I", idx_key)[0]
                if sync_state >= 5:
                    break
                try:
                    results.append(FrameMetricsRecord.from_bytes(pkey, value))
                except (RecordVersionError, struct.error) as e:
                    self._count_read_error("get_frames_with_sync_lost", e)
                rec = cursor.pget(flags=bdb.DB_NEXT)
        except Exception as e:
            self._count_read_error("get_frames_with_sync_lost", e)
        finally:
            cursor.close()
        return results

    def get_cal_history(self, start_ts_ms=0, end_ts_ms=None, rf_center_freq=None, limit=1000):
        if not self._enabled:
            return []
        if end_ts_ms is None:
            end_ts_ms = int(time.time() * 1000) + 1
        results = []

        if rf_center_freq is not None:
            cursor = self._idx_cal_freq_db.cursor()
            try:
                search_key = struct.pack(">Q", rf_center_freq)
                rec = cursor.pget(search_key, flags=bdb.DB_SET)
                while rec and len(results) < limit:
                    idx_key, pkey, value = rec
                    try:
                        cal_rec = CalHistoryRecord.from_bytes(pkey, value)
                        if start_ts_ms <= cal_rec.timestamp_ms <= end_ts_ms:
                            results.append(cal_rec)
                    except (RecordVersionError, struct.error) as e:
                        self._count_read_error("get_cal_history", e)
                    rec = cursor.pget(flags=bdb.DB_NEXT_DUP)
            except Exception as e:
                self._count_read_error("get_cal_history", e)
            finally:
                cursor.close()
        else:
            cursor = self._cal_history_db.cursor()
            try:
                start_key = CalHistoryRecord.make_key(start_ts_ms, 0)
                rec = cursor.set_range(start_key)
                while rec and len(results) < limit:
                    key, value = rec
                    try:
                        cal_rec = CalHistoryRecord.from_bytes(key, value)
                        if cal_rec.timestamp_ms > end_ts_ms:
                            break
                        results.append(cal_rec)
                    except (RecordVersionError, struct.error) as e:
                        self._count_read_error("get_cal_history", e)
                    rec = cursor.next()
            except Exception as e:
                self._count_read_error("get_cal_history", e)
            finally:
                cursor.close()
        return results

    def get_freq_scan_summary(self, rf_center_freq=None):
        if not self._enabled:
            return []
        results = []
        if rf_center_freq is not None:
            key = FreqScanRecord.make_key(rf_center_freq)
            try:
                data = self._freq_scan_db.get(key)
                if data:
                    results.append(FreqScanRecord.from_bytes(key, data))
            except (RecordVersionError, struct.error) as e:
                self._count_read_error("get_freq_scan_summary", e)
            except Exception as e:
                self._count_read_error("get_freq_scan_summary", e)
        else:
            cursor = self._freq_scan_db.cursor()
            try:
                rec = cursor.first()
                while rec:
                    key, value = rec
                    try:
                        results.append(FreqScanRecord.from_bytes(key, value))
                    except (RecordVersionError, struct.error) as e:
                        self._count_read_error("get_freq_scan_summary", e)
                    rec = cursor.next()
            except Exception as e:
                self._count_read_error("get_freq_scan_summary", e)
            finally:
                cursor.close()
        return results

    def get_hw_snapshots(self, start_ts_ms=0, end_ts_ms=None, limit=1000):
        if not self._enabled:
            return []
        if end_ts_ms is None:
            end_ts_ms = int(time.time() * 1000) + 1
        results = []
        cursor = self._hw_snapshots_db.cursor()
        try:
            start_key = HWSnapshotRecord.make_key(start_ts_ms)
            rec = cursor.set_range(start_key)
            while rec and len(results) < limit:
                key, value = rec
                try:
                    hw_rec = HWSnapshotRecord.from_bytes(key, value)
                    if hw_rec.timestamp_ms > end_ts_ms:
                        break
                    results.append(hw_rec)
                except (RecordVersionError, struct.error) as e:
                    self._count_read_error("get_hw_snapshots", e)
                rec = cursor.next()
        except Exception as e:
            self._count_read_error("get_hw_snapshots", e)
        finally:
            cursor.close()
        return results

    # --- Maintenance ---

    def rotate(self, max_age_hours=None):
        """Delete records older than max_age_hours"""
        if not self._enabled or self.read_only:
            return
        if max_age_hours is None:
            max_age_hours = self.rotation_max_age_hours
        cutoff_ms = int((time.time() - max_age_hours * 3600) * 1000)

        deleted = 0
        for db_obj in [self._frame_metrics_db, self._cal_history_db, self._hw_snapshots_db]:
            # CDB environments hand out read-only cursors by default; deleting
            # through one fails with BDB0697, so request a write cursor.
            cursor = db_obj.cursor(flags=bdb.DB_WRITECURSOR)
            try:
                # Keys are big-endian, so memcmp iteration order is numeric
                # timestamp order and breaking at the cutoff is correct.
                rec = cursor.first()
                while rec:
                    key, _ = rec
                    ts = struct.unpack_from(">Q", key, 0)[0]
                    if ts >= cutoff_ms:
                        break
                    cursor.delete()
                    deleted += 1
                    rec = cursor.next()
            except Exception as e:
                self._count_read_error("rotate", e)
            finally:
                cursor.close()

        if deleted > 0:
            self.logger.info("Rotated {:d} old records".format(deleted))

    def compact(self):
        """Compact all databases to reclaim space"""
        if not self._enabled or self.read_only:
            return
        for db_obj in [self._frame_metrics_db, self._cal_history_db,
                       self._freq_scan_db, self._hw_snapshots_db,
                       self._schedule_state_db]:
            try:
                db_obj.compact()
            except Exception:
                pass

    def get_db_stats(self):
        """Return file sizes and record counts"""
        if not self._enabled:
            return {}
        stats = {}
        db_files = ["frame_metrics.db", "cal_history.db", "freq_scan.db",
                     "hw_snapshots.db", "schedule_state.db"]
        for fname in db_files:
            fpath = os.path.join(self.db_dir, fname)
            try:
                stats[fname] = {"size_bytes": os.path.getsize(fpath)}
            except OSError:
                stats[fname] = {"size_bytes": 0}
        stats["write_queue_size"] = self._write_queue.qsize() if self._write_queue else 0
        stats["read_errors"] = self._read_errors
        return stats

    def close(self):
        """Close all databases and the environment"""
        if not self._enabled or self._closed:
            return
        self._closed = True
        # Wait for writer thread to flush
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=5.0)

        # Close secondary indices first
        for sec_db in [self._idx_freq_db, self._idx_frame_type_db,
                       self._idx_sync_state_db, self._idx_time_db,
                       self._idx_cal_freq_db, self._idx_cal_event_db]:
            try:
                sec_db.close()
            except Exception:
                pass

        # Close primary databases
        for primary_db in [self._frame_metrics_db, self._cal_history_db,
                           self._freq_scan_db, self._hw_snapshots_db,
                           self._schedule_state_db]:
            try:
                primary_db.close()
            except Exception:
                pass

        try:
            self._env.close()
        except Exception:
            pass
        self.logger.info("DAQ database closed")
