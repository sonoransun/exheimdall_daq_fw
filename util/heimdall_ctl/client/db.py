"""Read-only wrapper around daq_db for CLI queries."""
import sys
import os


def _ensure_daq_core_path():
    """Add Firmware/_daq_core to sys.path so we can import daq_db."""
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "..",
                     "Firmware", "_daq_core"),
        os.path.join("Firmware", "_daq_core"),
        "_daq_core",
    ]
    for p in candidates:
        absp = os.path.abspath(p)
        if os.path.isdir(absp) and absp not in sys.path:
            sys.path.insert(0, absp)
            return


def open_db(db_dir="_db"):
    _ensure_daq_core_path()
    try:
        from daq_db import DAQDatabase
    except ImportError:
        raise RuntimeError(
            "berkeleydb or daq_db not available. "
            "Install python-berkeleydb and ensure Firmware/_daq_core is accessible.")
    return DAQDatabase(db_dir=db_dir, enable_write=False)


def cal_history(db, since_ms=None, freq=None, limit=50):
    return db.get_cal_history(
        start_ts_ms=since_ms, rf_center_freq=freq, limit=limit)


def freq_scan_summary(db):
    return db.get_freq_scan_summary()
