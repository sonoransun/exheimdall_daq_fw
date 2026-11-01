"""Read-only wrapper around daq_db for local CLI queries.

Only the *local* DB mode (``cal-history``/``freq-scan`` without
``--remote``) uses this module; it needs ``Firmware/_daq_core`` and the
berkeleydb bindings on this machine. The remote mode goes through the
``:5002`` proxies in ``status.py`` and imports nothing from the firmware.
"""
import os
import sys


def _ensure_daq_core_path(firmware_dir=None):
    """Add Firmware/_daq_core to sys.path so we can import daq_db."""
    candidates = []
    if firmware_dir:
        candidates.append(os.path.join(firmware_dir, "_daq_core"))
    candidates += [
        os.path.join(os.path.dirname(__file__), "..", "..", "..",
                     "Firmware", "_daq_core"),
        os.path.join("Firmware", "_daq_core"),
        "_daq_core",
    ]
    for p in candidates:
        absp = os.path.abspath(p)
        if os.path.isdir(absp):
            if absp not in sys.path:
                sys.path.insert(0, absp)
            return


def default_db_dir(cfg, cfg_path, instance_id):
    """Default --db-dir for the local mode (the B.2 rule).

    ``[database] db_dir`` of the resolved config (relative paths resolve
    against the config's directory, the chain's working directory), plus
    ``/inst<N>`` only when that config sets ``instance_subdir = 1`` and N > 0.
    Without a config: ``_db`` (relative to the current directory, as before).
    """
    if cfg is None:
        return "_db"
    db_dir = cfg.get("database", "db_dir", fallback="_db").strip() or "_db"
    if not os.path.isabs(db_dir) and cfg_path:
        db_dir = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), db_dir)
    subdir = cfg.get("database", "instance_subdir", fallback="0").strip()
    if subdir == "1" and instance_id and instance_id > 0:
        db_dir = os.path.join(db_dir, "inst{:d}".format(instance_id))
    return db_dir


def open_db(db_dir="_db", firmware_dir=None):
    _ensure_daq_core_path(firmware_dir)
    try:
        from daq_db import DAQDatabase, bdb
    except ImportError:
        raise RuntimeError(
            "berkeleydb or daq_db not available. "
            "Install python-berkeleydb and ensure Firmware/_daq_core is accessible.")
    if bdb is None:
        raise RuntimeError(
            "berkeleydb Python bindings not available "
            "(install berkeleydb or bsddb3).")
    return DAQDatabase(db_dir=db_dir, read_only=True)


def cal_history(db, since_ms=None, freq=None, limit=50):
    return db.get_cal_history(
        start_ts_ms=since_ms or 0, rf_center_freq=freq, limit=limit)


def freq_scan_summary(db, freq=None):
    if freq is None:
        return db.get_freq_scan_summary()
    return db.get_freq_scan_summary(freq)
