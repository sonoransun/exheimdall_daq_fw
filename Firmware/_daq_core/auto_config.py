#!/usr/bin/env python3
"""
   Auto-Configuration for HeIMDALL DAQ Firmware.

   Reads hardware capabilities (hw_caps.json) and updates
   daq_chain_config.ini with recommended settings. Only fields whose
   current value is 'auto' are updated.

   Runnable standalone:
       python3 auto_config.py _data_control/hw_caps.json daq_chain_config.ini

   Project: HeIMDALL DAQ Firmware
   License: GNU GPL V3

   This program is free software: you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation, either version 3 of the License, or
   any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import json
import logging
import os
import sys
from configparser import ConfigParser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mapping from recommended keys to config file sections/keys
# ---------------------------------------------------------------------------

_AUTO_FIELD_MAP = {
    # (section, key): lambda caps_recommended -> value
    ('pre_processing', 'fft_engine'): lambda rec: rec.get('fft_engine', 'cpu_scipy'),
    ('calibration', 'correlation_engine'): lambda rec: rec.get('correlation_engine', 'cpu_numpy'),
    ('data_interface', 'transport_type'): lambda rec: rec.get('transport', 'shm'),
    ('pre_processing', 'fft_workers'): lambda rec: str(rec.get('workers', 4)),
    ('calibration', 'xcorr_workers'): lambda rec: str(rec.get('workers', 4)),
}


def auto_configure(caps_file, config_file):
    """Update config file with optimal settings based on hardware capabilities.

    Only fields whose current value is the string 'auto' (case-insensitive) are
    updated. All other fields are left untouched.

    Parameters
    ----------
    caps_file : str
        Path to hw_caps.json (output of hw_discover.py).
    config_file : str
        Path to daq_chain_config.ini.

    Returns
    -------
    dict
        Summary of changes: {(section, key): new_value, ...}
    """
    # Read hardware capabilities
    if not os.path.isfile(caps_file):
        logger.error("Capabilities file not found: %s", caps_file)
        return {}

    with open(caps_file, 'r') as f:
        caps = json.load(f)

    recommended = caps.get('recommended', {})
    if not recommended:
        logger.warning("No recommendations found in %s", caps_file)
        return {}

    # Read existing config
    config = ConfigParser()
    config.read(config_file)

    changes = {}

    for (section, key), value_fn in _AUTO_FIELD_MAP.items():
        # Only update if current value is 'auto'
        current = config.get(section, key, fallback=None)
        if current is not None and current.strip().lower() == 'auto':
            new_value = value_fn(recommended)
            config.set(section, key, new_value)
            changes[(section, key)] = new_value
            logger.info("Auto-configured [%s] %s = %s", section, key, new_value)
        elif current is None:
            logger.debug("Field [%s] %s not present in config, skipping", section, key)
        else:
            logger.debug("Field [%s] %s = '%s' (not 'auto'), skipping",
                         section, key, current)

    # Write updated config
    if changes:
        with open(config_file, 'w') as f:
            config.write(f)
        logger.info("Updated %d field(s) in %s", len(changes), config_file)
    else:
        logger.info("No fields set to 'auto' -- config unchanged")

    return changes


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    if len(sys.argv) != 3:
        print("Usage: python3 auto_config.py <hw_caps.json> <daq_chain_config.ini>")
        print("Example: python3 auto_config.py _data_control/hw_caps.json daq_chain_config.ini")
        sys.exit(1)

    caps_path = sys.argv[1]
    config_path = sys.argv[2]

    changes = auto_configure(caps_path, config_path)

    if changes:
        print("Applied {} auto-configuration change(s):".format(len(changes)))
        for (section, key), value in changes.items():
            print("  [{}] {} = {}".format(section, key, value))
    else:
        print("No changes applied (no fields set to 'auto').")
