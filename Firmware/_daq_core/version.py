"""
    Firmware Version — single source of truth

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3

    FW_VERSION is the firmware release string reported by the extended
    STATUS snapshot (fw_version), the coordinator VERSION verb, REGISTER
    bodies and capture sidecars. The release process bumps it here and
    nowhere else. Stdlib only; importable via sys.path like every other
    _daq_core module.

    Usage: python3 _daq_core/version.py   -> prints the version string
"""

FW_VERSION = "1.1.0-dev"


if __name__ == "__main__":
    print(FW_VERSION)
