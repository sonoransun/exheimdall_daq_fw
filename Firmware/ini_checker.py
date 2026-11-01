#!/usr/bin/env python3
import glob
import ipaddress
import json
import logging
import math
import os
import re
import socket
import subprocess
import sys
from configparser import ConfigParser

"""
	Checks the values in config ini file
	
	Project: HeIMDALL DAQ Firmware
	Author : Tamas Peto	
"""
def read_config_file(config_filename):
    """
    Reads the config file and creates a python dictionary from the
    read value.
    Parameters:
    -----------
        :param: config_filename: Name of the configuration file
        :type:  config_filename: string
            
    Return values:
    --------------
        :return: params: Confiugrations file fields arranged in a python dictionary
                    None: If internal error occured during the read of the configuration file
    """
    parser = ConfigParser()
    try:
        found = parser.read([config_filename])
    except Exception as e:
        logging.error("Failed to parse configuration file: {0}".format(e))
        return None
    if not found:
        logging.error("DAQ core configuration file not found.")
        return None
    return parser._sections

def chk_int(s):
    try: 
        int(s)
        return True
    except ValueError:
        return False

def chk_float(s):
    try: 
        float(s)
        return True
    except ValueError:
        return False

def count_receivers():
    try:
        lsusb_cmd = subprocess.run(["lsusb"], capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        logging.warning("lsusb not available - cannot count receivers")
        return 0
    lsusb_str = lsusb_cmd.stdout
    device_count = 0
    for line in lsusb_str.splitlines():
        if line.find("Realtek")>=0:device_count+=1
    logging.debug("Found {:d} receivers".format(device_count))
    return device_count
def get_serials(indices=None):
    """EEPROM serial numbers (ints) read with ``rtl_eeprom -d i``.

    Without *indices* the historic probe of device indices 0..4 runs
    unchanged. With *indices* (used when [hw] serial_base/device_serials are
    set: every device counted by count_receivers()) the readable serials of
    probe_eeprom_devices() are returned.
    """
    if indices is not None:
        return [int(d["serial"]) for d in probe_eeprom_devices(indices)
                if d["serial"] is not None and d["serial"].isdigit()]
    serial_nos = []
    for ch_ind in range(5):
        try:
            rtl_eeprom_cmd = subprocess.run(["rtl_eeprom", "-d", str(ch_ind)], capture_output=True, text=True)
        except (FileNotFoundError, OSError):
            logging.warning("rtl_eeprom not available - cannot read serial numbers")
            return serial_nos
        response    = rtl_eeprom_cmd.stderr
        response_l  = response.split('\n')
        for line in response_l:
            if line.find("Serial number:") >=0:
                serial_nos.append(int(line[line.find(':\t\t')+2:]))
    return serial_nos

# rtl_eeprom results per index tuple: check_ini() and check_ini_warnings()
# share one probe per process (each rtl_eeprom call briefly claims a device).
_eeprom_probe_cache = {}
# rtl_eeprom texts of a device that exists but cannot be claimed or opened
# (librtlsdr prints the first on a claim failure, rtl_eeprom the second on
# any rtlsdr_open() failure -- including an index past the last device, which
# is why the device count below gates them)
_EEPROM_BUSY_MARKERS = ("usb_claim_interface error", "Failed to open")
# rtl_eeprom: "Found %d device(s):" (librtlsdr's count of RTL2832U devices),
# or "No supported devices found." when that count is zero
_EEPROM_FOUND_RE = re.compile(r"Found (\d+) device\(s\)")
_EEPROM_NONE_MARKER = "No supported devices found"

def _eeprom_index_absent(output, ch_ind):
    """True when rtl_eeprom's own device count says index *ch_ind* does not exist.

    count_receivers() counts every Realtek USB device (NICs, card readers),
    so indices past librtlsdr's RTL2832U count are probed too; for those
    rtl_eeprom prints "Using device N: " and "Failed to open rtlsdr device
    #N." -- an absent device, not a busy one.
    """
    if _EEPROM_NONE_MARKER in output:
        return True
    found = _EEPROM_FOUND_RE.search(output)
    return found is not None and ch_ind >= int(found.group(1))

def probe_eeprom_devices(indices):
    """Read the EEPROM serial of every device index in *indices*.

    Returns a list of ``{"index", "serial", "busy"}`` for every index that
    holds an RTL2832U. An index at or past the ``Found N device(s)`` count
    rtl_eeprom prints (or with ``No supported devices found.``) is left out:
    count_receivers() also counts other Realtek USB devices. A device inside
    that count whose output reports a USB claim/open failure
    (``usb_claim_interface error``, ``Failed to open``) is ``busy`` (claimed
    by another process) and has ``serial`` None; one with neither a failure
    nor a ``Serial number:`` line is left out. An empty list when rtl_eeprom
    is missing.
    """
    key = tuple(indices)
    if key in _eeprom_probe_cache:
        return _eeprom_probe_cache[key]
    devices = []
    for ch_ind in key:
        try:
            res = subprocess.run(["rtl_eeprom", "-d", str(ch_ind)], capture_output=True, text=True)
        except (FileNotFoundError, OSError):
            logging.warning("rtl_eeprom not available - cannot read serial numbers")
            devices = []
            break
        output = (res.stderr or "") + (res.stdout or "")
        serial = None
        for line in output.splitlines():
            if line.find("Serial number:") >= 0:
                serial = line.split(":", 1)[1].strip()
                break
        if _eeprom_index_absent(output, ch_ind):
            continue  # past librtlsdr's RTL2832U count: not a receiver
        busy = any(marker in output for marker in _EEPROM_BUSY_MARKERS)
        if not busy and serial is None:
            continue  # no RTL2832U at this index
        devices.append({"index": ch_ind, "serial": None if busy else serial, "busy": busy})
    _eeprom_probe_cache[key] = devices
    return devices

# Initialize logger
logging.basicConfig(level=logging.ERROR)
valid_bias_enable_flag = [0, 1]
valid_gains = [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496]
valid_fir_windows = ['boxcar', 'triang', 'blackman', 'hamming', 'hann', 'bartlett', 'flattop', 'parzen' , 'bohman', 'blackmanharris', 'nuttall', 'barthann']
valid_rotator_backends = ['mock', 'gs232', 'pwm_servo', 'i2c_pantilt']
valid_polarizations = ['vertical', 'horizontal', 'rhcp', 'lhcp', 'slant45', 'dual']
valid_bearing_modes = ['manual', 'fixed', 'external', 'scan_peak']
# See: https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.get_window.html#scipy.signal.get_window

def check_ini(parameters, en_hw_check=True):
    # ALL hardware probing (lsusb / rtl_eeprom) is gated on en_hw_check so that
    # the 'no_hw' mode runs on systems without USB tooling or SDR hardware.
    # check_ini() never reads sysfs, /proc or the host registry (any mode).
    keys_set = serial_keys_set(parameters.get('hw', {}))
    device_count = count_receivers() if en_hw_check else 0
    if en_hw_check and keys_set:
        # Enumerate every attached receiver instead of the historic indices 0..4
        serials = get_serials(range(device_count))
    else:
        serials = get_serials() if en_hw_check else []

    error_list = []
    """
    --------------------------------
        | HW | Parameter group
    --------------------------------
    """

    hw_params = parameters['hw']
    if len(hw_params['name']) > 16:
        error_list.append("Hardware name has to be less than 16 character, currently it is: {:d}".format(len(hw_params['name'])))

    # 'auto' = stamp [federation] instance_id into the header unit_id
    if hw_params['unit_id'].strip() != 'auto' and not chk_int(hw_params['unit_id']):
        error_list.append("Unit ID must be integer. Currently it is: '{0}' ".format(hw_params['unit_id']))

    if not chk_int(hw_params['ioo_type']):
        error_list.append("IOO type must be integer. Currently it is: '{0}' ".format(hw_params['ioo_type']))

    if not chk_int(hw_params['num_ch']):
        error_list.append("Number of channels must be an integer. Currently it is: '{0}' ".format(hw_params['num_ch']))
    else:
        if int(hw_params['num_ch']) <= 0:
            error_list.append("Number of channels must be a non zero positive number. Currently it is: '{0}' ".format(hw_params['num_ch']))
        if int(hw_params['num_ch']) > MAX_NUM_CH:
            error_list.append("Number of channels must be at most {0}: the 128-byte :1130 control message holds {0} per-channel values. Currently it is: '{1}' ".format(MAX_NUM_CH, hw_params['num_ch']))
        resolved = None
        if en_hw_check and keys_set:
            try:
                resolved = resolve_serials(hw_params)
            except ValueError:
                pass  # grammar error, reported by _check_scaling_keys()
        if resolved is not None:
            # Serial keys set: count the configured serials that are attached
            # instead of every receiver on the host
            readable = set(d["serial"] for d in probe_eeprom_devices(range(device_count)))
            missing = [s for s in resolved if s not in readable]
            if missing:
                error_list.append("Only {0} of the {1} configured serials are attached; missing: {2} (see [hw] serial_base/device_serials)".format(len(resolved) - len(missing), len(resolved), ", ".join(missing)))
                device_count = len(resolved) - len(missing)
            else:
                device_count = int(hw_params['num_ch'])
        elif en_hw_check and int(hw_params['num_ch']) > device_count:
            error_list.append("Only {0} receiver channels are available, but {1} is requested!".format(device_count, hw_params['num_ch']))
        else:
            device_count = int(hw_params['num_ch'])
    bias_init_str = hw_params['en_bias_tee']
    bias_init_str = bias_init_str.split(',')
    for bias_en_str in bias_init_str:
        if not chk_int(bias_en_str):
            error_list.append("Bias tee init value must be a list of integers, Currently it is: '{0}' ".format(bias_en_str))
        else:
            if not int(bias_en_str) in valid_bias_enable_flag:
                error_list.append("Bias tee init values should be one of the followings:{0}. Currently one of it is: '{1}' ".format(valid_bias_enable_flag,int(bias_en_str)))
    if en_hw_check and len(bias_init_str) != device_count:
        error_list.append("The number of specified bias tee init values does not much with availble channels. Set:{0}, available:{1}".format(len(bias_init_str), device_count))

    """
    --------------------------------
        | DAQ | Parameter group
    --------------------------------
    """

    daq_params = parameters['daq']
    if not chk_int(daq_params['log_level']):
        error_list.append("Log level must be an integer. Currently it is: '{0}' ".format(daq_params['log_level']))
    else:
        if not int(daq_params['log_level']) in [0,1,2,3,4,5]:
            error_list.append("Valid log level range is: 0-5. Currently it is: '{0}' ".format(daq_params['log_level']))
    daq_buffer_size = -1
    if not chk_int(daq_params['daq_buffer_size']):
        error_list.append("DAQ buffer size must be a non-zero integer. Currently it is: '{0}' ".format(daq_params['daq_buffer_size']))
    else:
        daq_buffer_size = int(daq_params['daq_buffer_size'])
        if daq_buffer_size <= 0:
            error_list.append("DAQ buffer size must be a non-zero integer Currently it is: '{0}' ".format(daq_params['daq_buffer_size']))
        
        if not (daq_buffer_size & (daq_buffer_size-1) == 0):
            error_list.append("DAQ buffer size must be a the power of 2 Currently it is: '{0}' ".format(daq_params['daq_buffer_size']))

    if not chk_int(daq_params['center_freq']):
        error_list.append("DAQ center frequency must be a non-zero integer. Currently it is: '{0}' ".format(daq_params['center_freq']))
    else:
        if int(daq_params['center_freq']) <= 0:
            error_list.append("DAQ center frequency must be a non-zero integer. Currently it is: '{0}' ".format(daq_params['center_freq']))

    if not chk_int(daq_params['sample_rate']):
        error_list.append("Sample rate must be a non-zero integer. Currently it is: '{0}' ".format(daq_params['sample_rate']))
    else:
        if int(daq_params['sample_rate']) <= 0:
            error_list.append("Sample rate must be a non-zero integer. Currently it is: '{0}' ".format(daq_params['sample_rate']))

    if not chk_int(daq_params['gain']):
        error_list.append("Gain must be a non-zero integer. Currently it is: '{0}' ".format(daq_params['gain']))
    else:
        if not int(daq_params['gain']) in valid_gains:
            error_list.append("The gain value should be one of the followings:{0}. Currently it is: '{1}' ".format(valid_gains, daq_params['gain']))

    if not chk_int(daq_params['en_noise_source_ctr']):
        error_list.append("Noise source control enable must be 0 or 1. Currently it is: '{0}' ".format(daq_params['en_noise_source_ctr']))
    else:
        if not int(daq_params['en_noise_source_ctr']) in [0,1]:
            error_list.append("Noise source control enable must be 0 or 1. Currently it is: '{0}' ".format(daq_params['en_noise_source_ctr']))

    if not chk_int(daq_params['ctr_channel_serial_no']):
        error_list.append("Control channel serial number must be an integer. Currently it is: '{0}' ".format(daq_params['ctr_channel_serial_no']))
    else:
        if en_hw_check and not int(daq_params['ctr_channel_serial_no']) in serials:
            error_list.append("Invalid control channel serial number. Available serial numbers: {0}, Currrently set:{1}".format(serials, daq_params['ctr_channel_serial_no']))

    # Optional [daq] listen_address (bind address for the TCP/ZMQ listeners;
    # absent means 0.0.0.0 - previous behavior)
    listen_address = daq_params.get('listen_address')
    if listen_address is not None:
        try:
            socket.inet_aton(listen_address.strip())
        except (OSError, ValueError):
            error_list.append("DAQ listen_address must be a valid IPv4 address (e.g. 0.0.0.0 or 127.0.0.1). Currently it is: '{0}' ".format(listen_address))

    """
    --------------------------------------
        | PRE PROCESSING | Parameter group
    --------------------------------------
    """

    preproc_params = parameters['pre_processing']

    cpi_size = -1
    if not chk_int(preproc_params['cpi_size']):
        error_list.append("CPI size must be a positive integer. Currently it is: '{0}' ".format(preproc_params['cpi_size']))
    else:
        cpi_size = int(preproc_params['cpi_size'])
        if cpi_size <1:
            error_list.append("CPI size must be a positive integer. Currently it is: '{0}' ".format(preproc_params['cpi_size']))
        if cpi_size > MAX_FRAME_SAMPLES:
            error_list.append("CPI size must be at most {0} (2^23): larger frames overrun the decimator_out/delay_sync_iq shared-memory segments. Currently it is: '{1}' ".format(MAX_FRAME_SAMPLES, preproc_params['cpi_size']))
    
    decimation_ratio = -1
    if not chk_int(preproc_params['decimation_ratio']):
        error_list.append("Decimation ratio must be an integer. Currently it is: '{0}' ".format(preproc_params['decimation_ratio']))
    else:
        decimation_ratio = int(preproc_params['decimation_ratio'])
        if decimation_ratio <1:
            error_list.append("Decimation ratio must be an integer. Currently it is: '{0}' ".format(preproc_params['decimation_ratio']))

    if not chk_float(preproc_params['fir_relative_bandwidth']):
        error_list.append("FIR filter relative bandwidth must be a float in a range of ]0-1]. Currently it is: '{0}' ".format(preproc_params['fir_relative_bandwidth']))
    else:
        if float(preproc_params['fir_relative_bandwidth']) <=0 or float(preproc_params['fir_relative_bandwidth']) >1:
            error_list.append("FIR filter relative bandwidth must be a float in a range of ]0-1]. Currently it is: '{0}' ".format(preproc_params['fir_relative_bandwidth']))

    if not chk_int(preproc_params['fir_tap_size']):
        error_list.append("FIR filter tap size must be a positive integer. Currently it is: '{0}' ".format(preproc_params['fir_tap_size']))
    else:
        if int(preproc_params['fir_tap_size']) < 1:
            error_list.append("FIR filter tap size must be a positive integer. Currently it is: '{0}' ".format(preproc_params['fir_tap_size']))

    if not preproc_params['fir_window'] in valid_fir_windows:
        error_list.append("Invalid FIR window type. Valid options are: {0},  Currently it is: '{1}' ".format(valid_fir_windows, preproc_params['fir_window']))

    if not chk_int(preproc_params['en_filter_reset']):
        error_list.append("Filter reset enable must be 0 or 1. Currently it is: '{0}' ".format(preproc_params['en_filter_reset']))
    else:
        if not int(preproc_params['en_filter_reset']) in [0,1]:
            error_list.append("Filter reset enable must be 0 or 1. Currently it is: '{0}' ".format(preproc_params['en_filter_reset']))

    if chk_int(preproc_params['fir_tap_size']) and chk_int(preproc_params['decimation_ratio']):
        if int(preproc_params['fir_tap_size']) <= int(preproc_params['decimation_ratio']) and int(preproc_params['decimation_ratio']) !=1 :
            error_list.append("FIR tap size must be higher than the decimation ratio. Please consider increasing the tap size")

    # -- Module operation related checks -- 
    if daq_buffer_size != -1 and cpi_size != -1 and decimation_ratio !=-1:
        if (cpi_size*decimation_ratio) < daq_buffer_size:
            error_list.append("The duration of the CPI size (including decimation) must be larger than the duration of the DAQ buffer size")
    
    """
    --------------------------------
        | Calibration | Parameter group
    --------------------------------
    """

    cal_params = parameters['calibration']

    if not chk_int(cal_params['corr_size']):
        error_list.append("Calibration correlation size must be a positive integer. Currently it is: '{0}' ".format(cal_params['corr_size']))
    else:
        corr_size = int(cal_params['corr_size'])
        if corr_size < 1:
            error_list.append("Calibration correlation size must be a positive integer. Currently it is: '{0}' ".format(cal_params['corr_size']))
        if corr_size > MAX_FRAME_SAMPLES:
            error_list.append("Calibration correlation size must be at most {0} (2^23): larger frames overrun the decimator_out/delay_sync_iq shared-memory segments. Currently it is: '{1}' ".format(MAX_FRAME_SAMPLES, cal_params['corr_size']))
        """ Obsolete from ksdr82
        if corr_size > cpi_size:
            error_list.append("Calibration correlation size must greater than the CPI size")
        """
    if not chk_int(cal_params['std_ch_ind']):
        error_list.append("Standard channel index must be a non negative integer. Currently it is: '{0}' ".format(cal_params['std_ch_ind']))
    else:
        if int(cal_params['std_ch_ind']) < 0:
            error_list.append("Standard channel index must be a non negative integer. Currently it is: '{0}' ".format(cal_params['std_ch_ind']))
        if en_hw_check and int(cal_params['std_ch_ind']) > device_count-1:
            error_list.append("Standard channel index is higher than the number of available channels. Currently it is: '{0}' , available: 0..{1}".format(cal_params['std_ch_ind'], device_count-1))

    if not chk_int(cal_params['en_iq_cal']):
        error_list.append("IQ calibration enable must be 0 or 1. Currently it is: '{0}' ".format(cal_params['en_iq_cal']))
    else:
        if not int(cal_params['en_iq_cal']) in [0,1]:
            error_list.append("IQ calibration enable must be 0 or 1. Currently it is: '{0}' ".format(cal_params['en_iq_cal']))

    if not cal_params['amplitude_cal_mode'] in ['default','disabled','channel_power']:
        error_list.append("Invalid amplitude calibration mode. Valid options: 'default','disabled','channel_power', Currently it is: '{0}'".format(cal_params['amplitude_cal_mode']))
    
    if not chk_int(cal_params['en_gain_tune_init']):
        error_list.append("Initial gain tune enable must be 0 or 1. Currently it is: '{0}' ".format(cal_params['en_gain_tune_init']))
    else:
        if not int(cal_params['en_gain_tune_init']) in [0,1]:
            error_list.append("Initial gain tune enable must be 0 or 1. Currently it is: '{0}' ".format(cal_params['en_gain_tune_init']))

    if not chk_int(cal_params['gain_lock_interval']):
        error_list.append("Gain lock interval must be a non negative integer. Currently it is: '{0}' ".format(cal_params['gain_lock_interval']))
    else:
        if int(cal_params['gain_lock_interval']) < 0:
            error_list.append("Gain lock interval must be a non negative integer. Currently it is: '{0}' ".format(cal_params['gain_lock_interval']))

    if not chk_int(cal_params['unified_gain_control']):
        error_list.append("Unified gain control enable must be 0 or 1. Currently it is: '{0}' ".format(cal_params['unified_gain_control']))
    else:
        if not int(cal_params['unified_gain_control']) in [0,1]:
            error_list.append("Unified gain control enable must be 0 or 1. Currently it is: '{0}' ".format(cal_params['unified_gain_control']))

    if not chk_int(cal_params['require_track_lock_intervention']):
        error_list.append("Track lock interventation enable field must be 0 or 1. Currently it is: '{0}' ".format(cal_params['require_track_lock_intervention']))
    else:
        if not int(cal_params['require_track_lock_intervention']) in [0,1]:
            error_list.append("Track lock interventation enable field must be 0 or 1. Currently it is: '{0}' ".format(cal_params['require_track_lock_intervention']))

    if not chk_int(cal_params['cal_track_mode']):
        error_list.append("Calibration track mode should be one of the followings: 0/1/2. Currently it is: '{0}' ".format(cal_params['cal_track_mode']))
    else:
        if not int(cal_params['cal_track_mode']) in [0,1,2]:
            error_list.append("Calibration track mode should be one of the followings: 0/1/2. Currently it is: '{0}' ".format(cal_params['cal_track_mode']))

    if not chk_int(cal_params['cal_frame_interval']):
        error_list.append("Calibration frame interval must be a positive integer. Currently it is: '{0}' ".format(cal_params['cal_frame_interval']))
    else:
        if int(cal_params['cal_frame_interval']) < 1:
            error_list.append("Calibration frame interval must be a positive integer. Currently it is: '{0}' ".format(cal_params['cal_frame_interval']))

    if not chk_int(cal_params['cal_frame_burst_size']):
        error_list.append("Calibration frame burst size must be a positive integer. Currently it is: '{0}' ".format(cal_params['cal_frame_burst_size']))
    else:
        if int(cal_params['cal_frame_burst_size']) < 1:
            error_list.append("Calibration frame burst size must be a positive integer. Currently it is: '{0}' ".format(cal_params['cal_frame_burst_size']))

    if not chk_int(cal_params['amplitude_tolerance']):
        error_list.append("Calibration amplitude tolerance must be a positive integer. Currently it is: '{0}' ".format(cal_params['amplitude_tolerance']))
    else:
        if int(cal_params['amplitude_tolerance']) < 1:
            error_list.append("Calibration amplitude tolerance must be a positive integer. Currently it is: '{0}' ".format(cal_params['amplitude_tolerance']))

    if not chk_int(cal_params['phase_tolerance']):
        error_list.append("Calibration phase tolerance must be a positive integer. Currently it is: '{0}' ".format(cal_params['phase_tolerance']))
    else:
        if int(cal_params['phase_tolerance']) < 1:
            error_list.append("Calibration phase tolerance must be a positive integer. Currently it is: '{0}' ".format(cal_params['phase_tolerance']))

    if not chk_int(cal_params['maximum_sync_fails']):
        error_list.append("Maximum allowed sync check fails must be a positive integer. Currently it is: '{0}' ".format(cal_params['maximum_sync_fails']))
    else:
        if int(cal_params['maximum_sync_fails']) < 1:
            error_list.append("Maximum allowed sync check fails must be a positive integer. Currently it is: '{0}' ".format(cal_params['maximum_sync_fails']))

    iq_adjust_amplitude_str = cal_params['iq_adjust_amplitude']
    iq_adjust_amplitude_str = iq_adjust_amplitude_str.split(',')
    iq_adjust_time_delay_str     = cal_params['iq_adjust_time_delay_ns']
    iq_adjust_time_delay_str     = iq_adjust_time_delay_str.split(',')
    for amplitude_str in iq_adjust_amplitude_str:
        if not chk_float(amplitude_str):
            error_list.append("IQ amplitude adjust value must be a list of floats, Currently it is: '{0}' ".format(iq_adjust_amplitude_str))
    if en_hw_check and len(iq_adjust_amplitude_str) != device_count-1:
        error_list.append("The number of specified IQ amplitude adjustment values does not much with available channels. It should contain channel count-1  values. Set:{0}, available:{1}".format(len(iq_adjust_amplitude_str), device_count))

    iq_adjust_time_str = cal_params['iq_adjust_time_delay_ns']
    iq_adjust_time_str = iq_adjust_time_str.split(',')
    for time_str in iq_adjust_time_str:
        if not chk_float(time_str):
            error_list.append("IQ timde delay adjust value must be a list of floats, Currently it is: '{0}' ".format(iq_adjust_time_str))
    if en_hw_check and len(iq_adjust_time_str) != device_count-1:
        error_list.append("The number of specified IQ time delay adjustment values does not much with available channels. It should contain channel count-1  values. Set:{0}, available:{1}".format(len(iq_adjust_time_delay_str), device_count))

    """
    --------------------------------
        | ADPIS | Parameter group
    --------------------------------
    """

    adpis_params = parameters['adpis']

    if not chk_int(adpis_params['en_adpis']):
        error_list.append("ADPIS enable must be 0 or 1. Currently it is: '{0}' ".format(adpis_params['en_adpis']))
    else:
        if not int(adpis_params['en_adpis']) in [0,1]:
            error_list.append("ADPIS enable must be 0 or 1. Currently it is: '{0}' ".format(adpis_params['en_adpis']))

    if not chk_int(adpis_params['adpis_proc_size']):
        error_list.append("ADPIS processing size must be a positive integer. Currently it is: '{0}' ".format(adpis_params['adpis_proc_size']))
    else:
        if int(adpis_params['adpis_proc_size']) < 1:
            error_list.append("ADPIS processing size must be a positive integer. Currently it is: '{0}' ".format(adpis_params['adpis_proc_size']))

    gains_init_str = adpis_params['adpis_gains_init']
    gains_init_str = gains_init_str.split(',')
    for gain_str in gains_init_str:
        if not chk_int(gain_str):
            error_list.append("ADPIS gain init value must be a list of integers, Currently it is: '{0}' ".format(gains_init_str))
        else:
            if not int(gain_str) in valid_gains:
                error_list.append("ADPIS gain init values should be one of the followings:{0}. Currently one of it is: '{1}' ".format(valid_gains,int(gain_str)))
    if en_hw_check and len(gains_init_str) != device_count:
        error_list.append("The number of specified ADPIS gain init values does not much with availble channels. Set:{0}, available:{1}".format(len(gains_init_str), device_count))

    """
    ----------------------------------------
        | DATA INTERFACE | Parameter group
    ----------------------------------------
    """
    data_iface_params = parameters['data_interface']
    if not (data_iface_params['out_data_iface_type'] == "eth" or data_iface_params['out_data_iface_type'] == "shmem"):
        error_list.append("Output data interface type should be 'eth' or 'shmem'. Currently one of it is: '{0}' ".format(data_iface_params['out_data_iface_type']))

    """
    ----------------------------------------
        | SCHEDULE | Parameter group (optional)
    ----------------------------------------
    """
    if 'schedule' in parameters:
        sched_params = parameters['schedule']

        if not chk_int(sched_params.get('en_schedule', '0')):
            error_list.append("Schedule enable must be 0 or 1. Currently it is: '{0}' ".format(sched_params.get('en_schedule')))
        else:
            if not int(sched_params.get('en_schedule', '0')) in [0,1]:
                error_list.append("Schedule enable must be 0 or 1. Currently it is: '{0}' ".format(sched_params.get('en_schedule')))

        sched_mode = sched_params.get('schedule_mode', 'none')
        if sched_mode not in ['none', 'file', 'inline']:
            error_list.append("Schedule mode must be one of: none, file, inline. Currently it is: '{0}' ".format(sched_mode))

        if sched_mode == 'inline':
            freq_str = sched_params.get('frequencies', '')
            dwell_str = sched_params.get('dwell_frames', '')
            if freq_str.strip():
                freq_parts = freq_str.split(',')
                for fp in freq_parts:
                    if not chk_int(fp.strip()):
                        error_list.append("Schedule frequencies must be comma-separated positive integers. Invalid: '{0}' ".format(fp.strip()))
                    elif int(fp.strip()) <= 0:
                        error_list.append("Schedule frequencies must be positive integers. Invalid: '{0}' ".format(fp.strip()))

                if dwell_str.strip():
                    dwell_parts = dwell_str.split(',')
                    for dp in dwell_parts:
                        if not chk_int(dp.strip()):
                            error_list.append("Schedule dwell_frames must be comma-separated positive integers. Invalid: '{0}' ".format(dp.strip()))
                        elif int(dp.strip()) <= 0:
                            error_list.append("Schedule dwell_frames must be positive integers. Invalid: '{0}' ".format(dp.strip()))
                    if len(dwell_parts) != len(freq_parts):
                        error_list.append("Schedule frequencies and dwell_frames must have the same number of entries. Frequencies: {0}, Dwell: {1}".format(len(freq_parts), len(dwell_parts)))

                gains_str = sched_params.get('gains', '')
                if gains_str.strip():
                    gain_entries = gains_str.split(';')
                    for ge in gain_entries:
                        ge = ge.strip()
                        if ge:
                            for gv in ge.split(','):
                                gv = gv.strip()
                                if gv and not chk_int(gv):
                                    error_list.append("Schedule gain values must be integers. Invalid: '{0}' ".format(gv))
                                elif gv and int(gv) not in valid_gains:
                                    error_list.append("Schedule gain value should be one of: {0}. Currently: '{1}' ".format(valid_gains, gv))

        repeat_mode = sched_params.get('repeat_mode', 'loop')
        if repeat_mode not in ['loop', 'once', 'pingpong']:
            error_list.append("Schedule repeat_mode must be one of: loop, once, pingpong. Currently it is: '{0}' ".format(repeat_mode))

        if not chk_int(sched_params.get('require_cal_on_hop', '1')):
            error_list.append("Schedule require_cal_on_hop must be 0 or 1. Currently it is: '{0}' ".format(sched_params.get('require_cal_on_hop')))

        max_cal_wait = sched_params.get('max_cal_wait_frames', '500')
        if not chk_int(max_cal_wait):
            error_list.append("Schedule max_cal_wait_frames must be a positive integer. Currently it is: '{0}' ".format(max_cal_wait))
        elif int(max_cal_wait) <= 0:
            error_list.append("Schedule max_cal_wait_frames must be a positive integer. Currently it is: '{0}' ".format(max_cal_wait))

    """
    ----------------------------------------
        | DATABASE | Parameter group (optional)
    ----------------------------------------
    """
    if 'database' in parameters:
        db_params = parameters['database']

        if not chk_int(db_params.get('en_db', '0')):
            error_list.append("Database enable must be 0 or 1. Currently it is: '{0}' ".format(db_params.get('en_db')))
        else:
            if not int(db_params.get('en_db', '0')) in [0,1]:
                error_list.append("Database enable must be 0 or 1. Currently it is: '{0}' ".format(db_params.get('en_db')))

        db_dir = db_params.get('db_dir', '')
        if not db_dir.strip():
            error_list.append("Database db_dir must be a non-empty string")

        max_size = db_params.get('max_db_size_mb', '500')
        if not chk_int(max_size):
            error_list.append("Database max_db_size_mb must be a positive integer. Currently it is: '{0}' ".format(max_size))
        elif int(max_size) <= 0:
            error_list.append("Database max_db_size_mb must be a positive integer. Currently it is: '{0}' ".format(max_size))

        rot_age = db_params.get('rotation_max_age_hours', '168')
        if not chk_int(rot_age):
            error_list.append("Database rotation_max_age_hours must be a positive integer. Currently it is: '{0}' ".format(rot_age))
        elif int(rot_age) <= 0:
            error_list.append("Database rotation_max_age_hours must be a positive integer. Currently it is: '{0}' ".format(rot_age))

        batch_size = db_params.get('write_batch_size', '50')
        if not chk_int(batch_size):
            error_list.append("Database write_batch_size must be a positive integer. Currently it is: '{0}' ".format(batch_size))
        elif int(batch_size) <= 0:
            error_list.append("Database write_batch_size must be a positive integer. Currently it is: '{0}' ".format(batch_size))

        flush_interval = db_params.get('write_flush_interval_sec', '1.0')
        if not chk_float(flush_interval):
            error_list.append("Database write_flush_interval_sec must be a positive float. Currently it is: '{0}' ".format(flush_interval))
        elif float(flush_interval) <= 0:
            error_list.append("Database write_flush_interval_sec must be a positive float. Currently it is: '{0}' ".format(flush_interval))

        hw_interval = db_params.get('hw_snapshot_interval', '100')
        if not chk_int(hw_interval):
            error_list.append("Database hw_snapshot_interval must be a positive integer. Currently it is: '{0}' ".format(hw_interval))
        elif int(hw_interval) <= 0:
            error_list.append("Database hw_snapshot_interval must be a positive integer. Currently it is: '{0}' ".format(hw_interval))

    """
    ----------------------------------------
        | MONITORING | Parameter group (optional)
    ----------------------------------------
    """
    if 'monitoring' in parameters:
        mon_params = parameters['monitoring']

        if not chk_int(mon_params.get('en_monitoring', '0')):
            error_list.append("Monitoring enable must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_monitoring')))
        else:
            if not int(mon_params.get('en_monitoring', '0')) in [0,1]:
                error_list.append("Monitoring enable must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_monitoring')))

        if not chk_int(mon_params.get('en_syslog', '0')):
            error_list.append("Monitoring en_syslog must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_syslog')))
        else:
            if not int(mon_params.get('en_syslog', '0')) in [0,1]:
                error_list.append("Monitoring en_syslog must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_syslog')))

        syslog_addr = mon_params.get('syslog_address', '/dev/log')
        if not syslog_addr.strip():
            error_list.append("Monitoring syslog_address must be a non-empty string")

        syslog_fac = mon_params.get('syslog_facility', 'daemon')
        valid_facilities = ['daemon', 'local0', 'local1', 'local2', 'local3', 'local4', 'local5', 'local6', 'local7']
        if syslog_fac not in valid_facilities:
            error_list.append("Monitoring syslog_facility must be one of: {0}. Currently it is: '{1}' ".format(valid_facilities, syslog_fac))

        syslog_sev = mon_params.get('syslog_min_severity', 'warning')
        valid_severities = ['info', 'warning', 'error', 'critical']
        if syslog_sev not in valid_severities:
            error_list.append("Monitoring syslog_min_severity must be one of: {0}. Currently it is: '{1}' ".format(valid_severities, syslog_sev))

        if not chk_int(mon_params.get('en_metrics', '0')):
            error_list.append("Monitoring en_metrics must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_metrics')))
        else:
            if not int(mon_params.get('en_metrics', '0')) in [0,1]:
                error_list.append("Monitoring en_metrics must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_metrics')))

        win_size = mon_params.get('metrics_window_size', '1000')
        if not chk_int(win_size):
            error_list.append("Monitoring metrics_window_size must be a positive integer. Currently it is: '{0}' ".format(win_size))
        elif int(win_size) <= 0:
            error_list.append("Monitoring metrics_window_size must be a positive integer. Currently it is: '{0}' ".format(win_size))

        hb_interval = mon_params.get('heartbeat_interval', '100')
        if not chk_int(hb_interval):
            error_list.append("Monitoring heartbeat_interval must be a non-negative integer. Currently it is: '{0}' ".format(hb_interval))
        elif int(hb_interval) < 0:
            error_list.append("Monitoring heartbeat_interval must be a non-negative integer. Currently it is: '{0}' ".format(hb_interval))

        if not chk_int(mon_params.get('en_status_server', '0')):
            error_list.append("Monitoring en_status_server must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_status_server')))
        else:
            if not int(mon_params.get('en_status_server', '0')) in [0,1]:
                error_list.append("Monitoring en_status_server must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_status_server')))

        status_port = mon_params.get('status_server_port', '5002')
        if not chk_int(status_port):
            error_list.append("Monitoring status_server_port must be a valid port number. Currently it is: '{0}' ".format(status_port))
        elif int(status_port) < 1 or int(status_port) > 65535:
            error_list.append("Monitoring status_server_port must be a valid port number (1-65535). Currently it is: '{0}' ".format(status_port))

        if not chk_int(mon_params.get('en_zmq_pub', '0')):
            error_list.append("Monitoring en_zmq_pub must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_zmq_pub')))
        else:
            if not int(mon_params.get('en_zmq_pub', '0')) in [0,1]:
                error_list.append("Monitoring en_zmq_pub must be 0 or 1. Currently it is: '{0}' ".format(mon_params.get('en_zmq_pub')))

        zmq_port = mon_params.get('zmq_pub_port', '5003')
        if not chk_int(zmq_port):
            error_list.append("Monitoring zmq_pub_port must be a valid port number. Currently it is: '{0}' ".format(zmq_port))
        elif int(zmq_port) < 1 or int(zmq_port) > 65535:
            error_list.append("Monitoring zmq_pub_port must be a valid port number (1-65535). Currently it is: '{0}' ".format(zmq_port))

        ring_size = mon_params.get('event_ring_size', '500')
        if not chk_int(ring_size):
            error_list.append("Monitoring event_ring_size must be a positive integer. Currently it is: '{0}' ".format(ring_size))
        elif int(ring_size) <= 0:
            error_list.append("Monitoring event_ring_size must be a positive integer. Currently it is: '{0}' ".format(ring_size))

    """
    ----------------------------------------
        | OFFLOAD | Parameter group (optional)
    ----------------------------------------
    """
    if 'offload' in parameters:
        offload_params = parameters['offload']
        valid_transports = ['shm', 'spi', 'pcie', 'usb3', 'net']
        valid_engines = ['auto', 'cpu_neon', 'neon', 'cpu_kfr', 'kfr', 'cpu_generic', 'generic', 'fpga', 'gpu']

        for tkey in ['rebuffer_transport', 'decimator_transport', 'delay_sync_transport']:
            tval = offload_params.get(tkey, 'shm')
            if tval not in valid_transports:
                error_list.append("Offload {0} must be one of: {1}. Currently it is: '{2}' ".format(tkey, valid_transports, tval))

        for ekey in ['fir_engine', 'fft_engine']:
            eval_ = offload_params.get(ekey, 'auto')
            if eval_ not in valid_engines:
                error_list.append("Offload {0} must be one of: {1}. Currently it is: '{2}' ".format(ekey, valid_engines, eval_))

    """
    ----------------------------------------
        | DMA | Parameter group (optional)
    ----------------------------------------
    """
    if 'dma' in parameters:
        dma_params = parameters['dma']

        if not chk_int(dma_params.get('enable', '0')):
            error_list.append("DMA enable must be 0 or 1. Currently it is: '{0}' ".format(dma_params.get('enable')))
        else:
            if not int(dma_params.get('enable', '0')) in [0,1]:
                error_list.append("DMA enable must be 0 or 1. Currently it is: '{0}' ".format(dma_params.get('enable')))

        ch_memcpy = dma_params.get('channel_memcpy', '7')
        if not chk_int(ch_memcpy):
            error_list.append("DMA channel_memcpy must be a non-negative integer. Currently it is: '{0}' ".format(ch_memcpy))
        elif int(ch_memcpy) < 0:
            error_list.append("DMA channel_memcpy must be a non-negative integer. Currently it is: '{0}' ".format(ch_memcpy))

        min_xfer = dma_params.get('min_transfer_size', '65536')
        if not chk_int(min_xfer):
            error_list.append("DMA min_transfer_size must be a positive integer. Currently it is: '{0}' ".format(min_xfer))
        elif int(min_xfer) <= 0:
            error_list.append("DMA min_transfer_size must be a positive integer. Currently it is: '{0}' ".format(min_xfer))

    """
    ----------------------------------------
        | FPGA | Parameter group (optional)
    ----------------------------------------
    """
    if 'fpga' in parameters:
        fpga_params = parameters['fpga']

        if not chk_int(fpga_params.get('enable', '0')):
            error_list.append("FPGA enable must be 0 or 1. Currently it is: '{0}' ".format(fpga_params.get('enable')))
        else:
            if not int(fpga_params.get('enable', '0')) in [0,1]:
                error_list.append("FPGA enable must be 0 or 1. Currently it is: '{0}' ".format(fpga_params.get('enable')))

        spi_speed = fpga_params.get('spi_speed_hz', '62500000')
        if not chk_int(spi_speed):
            error_list.append("FPGA spi_speed_hz must be a positive integer. Currently it is: '{0}' ".format(spi_speed))
        elif int(spi_speed) <= 0:
            error_list.append("FPGA spi_speed_hz must be a positive integer. Currently it is: '{0}' ".format(spi_speed))

        for flag_key in ['offload_fir', 'offload_xcorr']:
            flag_val = fpga_params.get(flag_key, '0')
            if not chk_int(flag_val):
                error_list.append("FPGA {0} must be 0 or 1. Currently it is: '{1}' ".format(flag_key, flag_val))
            else:
                if not int(flag_val) in [0,1]:
                    error_list.append("FPGA {0} must be 0 or 1. Currently it is: '{1}' ".format(flag_key, flag_val))

        # Check bitstream file exists when FPGA is enabled
        if int(fpga_params.get('enable', '0')) == 1:
            import os
            bitstream_path = fpga_params.get('bitstream', '')
            if not bitstream_path.strip():
                error_list.append("FPGA bitstream path must be set when FPGA is enabled")
            elif en_hw_check and not os.path.isfile(bitstream_path):
                error_list.append("FPGA bitstream file does not exist: '{0}' ".format(bitstream_path))

    """
    ----------------------------------------
        | GPU | Parameter group (optional)
    ----------------------------------------
    """
    if 'gpu' in parameters:
        gpu_params = parameters['gpu']

        if not chk_int(gpu_params.get('enable', '0')):
            error_list.append("GPU enable must be 0 or 1. Currently it is: '{0}' ".format(gpu_params.get('enable')))
        else:
            if not int(gpu_params.get('enable', '0')) in [0,1]:
                error_list.append("GPU enable must be 0 or 1. Currently it is: '{0}' ".format(gpu_params.get('enable')))

        for flag_key in ['offload_fft', 'offload_fir']:
            flag_val = gpu_params.get(flag_key, '0')
            if not chk_int(flag_val):
                error_list.append("GPU {0} must be 0 or 1. Currently it is: '{1}' ".format(flag_key, flag_val))
            else:
                if not int(flag_val) in [0,1]:
                    error_list.append("GPU {0} must be 0 or 1. Currently it is: '{1}' ".format(flag_key, flag_val))

        fft_batch = gpu_params.get('fft_batch_size', '4')
        if not chk_int(fft_batch):
            error_list.append("GPU fft_batch_size must be a positive integer. Currently it is: '{0}' ".format(fft_batch))
        elif int(fft_batch) <= 0:
            error_list.append("GPU fft_batch_size must be a positive integer. Currently it is: '{0}' ".format(fft_batch))

    """
    ----------------------------------------
        | PCIE | Parameter group (optional)
    ----------------------------------------
    """
    if 'pcie' in parameters:
        pcie_params = parameters['pcie']

        if not chk_int(pcie_params.get('enable', '0')):
            error_list.append("PCIe enable must be 0 or 1. Currently it is: '{0}' ".format(pcie_params.get('enable')))
        else:
            if not int(pcie_params.get('enable', '0')) in [0,1]:
                error_list.append("PCIe enable must be 0 or 1. Currently it is: '{0}' ".format(pcie_params.get('enable')))

        bar_idx = pcie_params.get('bar_index', '0')
        if not chk_int(bar_idx):
            error_list.append("PCIe bar_index must be a non-negative integer. Currently it is: '{0}' ".format(bar_idx))
        elif int(bar_idx) < 0:
            error_list.append("PCIe bar_index must be a non-negative integer. Currently it is: '{0}' ".format(bar_idx))

    """
    ----------------------------------------
        | USB3 | Parameter group (optional)
    ----------------------------------------
    """
    if 'usb3' in parameters:
        usb3_params = parameters['usb3']

        if not chk_int(usb3_params.get('enable', '0')):
            error_list.append("USB3 enable must be 0 or 1. Currently it is: '{0}' ".format(usb3_params.get('enable')))
        else:
            if not int(usb3_params.get('enable', '0')) in [0,1]:
                error_list.append("USB3 enable must be 0 or 1. Currently it is: '{0}' ".format(usb3_params.get('enable')))

        xfer_size = usb3_params.get('transfer_size', '16384')
        if not chk_int(xfer_size):
            error_list.append("USB3 transfer_size must be a positive integer. Currently it is: '{0}' ".format(xfer_size))
        elif int(xfer_size) <= 0:
            error_list.append("USB3 transfer_size must be a positive integer. Currently it is: '{0}' ".format(xfer_size))

        num_xfer = usb3_params.get('num_transfers', '32')
        if not chk_int(num_xfer):
            error_list.append("USB3 num_transfers must be a positive integer. Currently it is: '{0}' ".format(num_xfer))
        elif int(num_xfer) <= 0:
            error_list.append("USB3 num_transfers must be a positive integer. Currently it is: '{0}' ".format(num_xfer))

    """
    ----------------------------------------
        | HAT_UART | Parameter group (optional)
    ----------------------------------------
    """
    if 'hat_uart' in parameters:
        uart_params = parameters['hat_uart']

        if not chk_int(uart_params.get('enable', '0')):
            error_list.append("HAT UART enable must be 0 or 1. Currently it is: '{0}' ".format(uart_params.get('enable')))
        else:
            if not int(uart_params.get('enable', '0')) in [0,1]:
                error_list.append("HAT UART enable must be 0 or 1. Currently it is: '{0}' ".format(uart_params.get('enable')))

        baud = uart_params.get('baud', '3000000')
        if not chk_int(baud):
            error_list.append("HAT UART baud must be a positive integer. Currently it is: '{0}' ".format(baud))
        elif int(baud) <= 0:
            error_list.append("HAT UART baud must be a positive integer. Currently it is: '{0}' ".format(baud))

        framing = uart_params.get('framing', 'cobs')
        if framing not in ['cobs', 'slip', 'raw']:
            error_list.append("HAT UART framing must be one of: cobs, slip, raw. Currently it is: '{0}' ".format(framing))

    """
    ----------------------------------------
        | HAT_I2C | Parameter group (optional)
    ----------------------------------------
    """
    if 'hat_i2c' in parameters:
        i2c_params = parameters['hat_i2c']

        if not chk_int(i2c_params.get('enable', '0')):
            error_list.append("HAT I2C enable must be 0 or 1. Currently it is: '{0}' ".format(i2c_params.get('enable')))
        else:
            if not int(i2c_params.get('enable', '0')) in [0,1]:
                error_list.append("HAT I2C enable must be 0 or 1. Currently it is: '{0}' ".format(i2c_params.get('enable')))

        bus = i2c_params.get('bus', '1')
        if not chk_int(bus):
            error_list.append("HAT I2C bus must be a non-negative integer. Currently it is: '{0}' ".format(bus))
        elif int(bus) < 0:
            error_list.append("HAT I2C bus must be a non-negative integer. Currently it is: '{0}' ".format(bus))

        speed = i2c_params.get('speed', '400000')
        if not chk_int(speed):
            error_list.append("HAT I2C speed must be a positive integer. Currently it is: '{0}' ".format(speed))
        elif int(speed) <= 0:
            error_list.append("HAT I2C speed must be a positive integer. Currently it is: '{0}' ".format(speed))

        retry = i2c_params.get('retry_count', '3')
        if not chk_int(retry):
            error_list.append("HAT I2C retry_count must be a non-negative integer. Currently it is: '{0}' ".format(retry))
        elif int(retry) < 0:
            error_list.append("HAT I2C retry_count must be a non-negative integer. Currently it is: '{0}' ".format(retry))

    # ---> Federation section check <---
    if 'federation' in parameters:
        fed_params = parameters['federation']

        instance_id = fed_params.get('instance_id', '0')
        if not chk_int(instance_id):
            error_list.append("Federation instance_id must be a non-negative integer. Currently it is: '{0}' ".format(instance_id))
        elif int(instance_id) < 0:
            error_list.append("Federation instance_id must be a non-negative integer. Currently it is: '{0}' ".format(instance_id))

        port_stride = fed_params.get('port_stride', '100')
        if not chk_int(port_stride):
            error_list.append("Federation port_stride must be a positive integer. Currently it is: '{0}' ".format(port_stride))
        elif int(port_stride) <= 0:
            error_list.append("Federation port_stride must be a positive integer. Currently it is: '{0}' ".format(port_stride))

        if not chk_int(fed_params.get('en_federation', '0')):
            error_list.append("Federation en_federation must be 0 or 1. Currently it is: '{0}' ".format(fed_params.get('en_federation')))
        else:
            if not int(fed_params.get('en_federation', '0')) in [0,1]:
                error_list.append("Federation en_federation must be 0 or 1. Currently it is: '{0}' ".format(fed_params.get('en_federation')))

        coord_port = fed_params.get('coordinator_port', '6000')
        if not chk_int(coord_port):
            error_list.append("Federation coordinator_port must be a valid port number. Currently it is: '{0}' ".format(coord_port))
        elif int(coord_port) < 1 or int(coord_port) > 65535:
            error_list.append("Federation coordinator_port must be a valid port number (1-65535). Currently it is: '{0}' ".format(coord_port))

    # Number of receiver channels, used for per-channel list length checks below
    try:
        num_ch_val = int(parameters['hw']['num_ch'])
    except (KeyError, ValueError):
        num_ch_val = None

    def _chk_float_list(raw, key, section):
        """Validate a comma-separated float list; check length against num_ch."""
        items = [x.strip() for x in raw.split(',') if x.strip() != '']
        for v in items:
            if not chk_float(v):
                error_list.append("{0} {1} values must be floats. Currently one is: '{2}' ".format(section, key, v))
        if num_ch_val is not None and len(items) != num_ch_val:
            error_list.append("{0} {1} must have one value per channel ({2}). Currently: {3} ".format(section, key, num_ch_val, len(items)))

    # ---> Amplification (external LNA / gain-staging) section check <---
    if 'amplification' in parameters:
        amp = parameters['amplification']

        if not chk_int(amp.get('en_amplification', '0')) or int(amp.get('en_amplification', '0')) not in [0, 1]:
            error_list.append("Amplification en_amplification must be 0 or 1. Currently it is: '{0}' ".format(amp.get('en_amplification')))

        _chk_float_list(amp.get('ext_lna_gains_db', ''), 'ext_lna_gains_db', 'Amplification')
        _chk_float_list(amp.get('ext_lna_nf_db', ''), 'ext_lna_nf_db', 'Amplification')
        _chk_float_list(amp.get('ext_lna_p1db_dbm', ''), 'ext_lna_p1db_dbm', 'Amplification')

        for key in ('tuner_nf_db', 'expected_input_dbm', 'p1db_margin_db', 'max_total_gain_db'):
            val = amp.get(key)
            if val is not None and not chk_float(val):
                error_list.append("Amplification {0} must be a float. Currently it is: '{1}' ".format(key, val))

        if not chk_int(amp.get('en_bias_tee_runtime', '0')) or int(amp.get('en_bias_tee_runtime', '0')) not in [0, 1]:
            error_list.append("Amplification en_bias_tee_runtime must be 0 or 1. Currently it is: '{0}' ".format(amp.get('en_bias_tee_runtime')))
        bias_state = [x.strip() for x in amp.get('bias_tee_state', '').split(',') if x.strip() != '']
        for v in bias_state:
            if not chk_int(v) or int(v) not in valid_bias_enable_flag:
                error_list.append("Amplification bias_tee_state values must be 0 or 1. Currently one is: '{0}' ".format(v))

    # ---> Antenna profile section check <---
    if 'antenna' in parameters:
        ant = parameters['antenna']

        if not chk_int(ant.get('en_antenna_profile', '0')) or int(ant.get('en_antenna_profile', '0')) not in [0, 1]:
            error_list.append("Antenna en_antenna_profile must be 0 or 1. Currently it is: '{0}' ".format(ant.get('en_antenna_profile')))

        pol = ant.get('polarization', 'vertical')
        if pol not in valid_polarizations:
            error_list.append("Antenna polarization must be one of: {0}. Currently it is: '{1}' ".format(valid_polarizations, pol))

        for key in ('element_gain_dbi', 'cable_loss_db', 'connector_loss_db',
                    'boresight_az_offset_deg', 'boresight_el_offset_deg'):
            val = ant.get(key)
            if val is not None and not chk_float(val):
                error_list.append("Antenna {0} must be a float. Currently it is: '{1}' ".format(key, val))

        for key in ('beamwidth_az_deg', 'beamwidth_el_deg'):
            val = ant.get(key)
            if val is not None:
                if not chk_float(val):
                    error_list.append("Antenna {0} must be a float. Currently it is: '{1}' ".format(key, val))
                elif not (0.0 < float(val) <= 360.0):
                    error_list.append("Antenna {0} must be in (0, 360]. Currently it is: '{1}' ".format(key, val))

    # ---> Orientation (rotator / pan-tilt) section check <---
    if 'orientation' in parameters:
        ori = parameters['orientation']

        if not chk_int(ori.get('en_orientation', '0')) or int(ori.get('en_orientation', '0')) not in [0, 1]:
            error_list.append("Orientation en_orientation must be 0 or 1. Currently it is: '{0}' ".format(ori.get('en_orientation')))

        backend = ori.get('backend', 'mock')
        if backend not in valid_rotator_backends:
            error_list.append("Orientation backend must be one of: {0}. Currently it is: '{1}' ".format(valid_rotator_backends, backend))

        mode = ori.get('bearing_mode', 'external')
        if mode not in valid_bearing_modes:
            error_list.append("Orientation bearing_mode must be one of: {0}. Currently it is: '{1}' ".format(valid_bearing_modes, mode))

        for lo_key, hi_key in (('az_min_deg', 'az_max_deg'), ('el_min_deg', 'el_max_deg')):
            lo, hi = ori.get(lo_key), ori.get(hi_key)
            if lo is not None and hi is not None:
                if not chk_float(lo) or not chk_float(hi):
                    error_list.append("Orientation {0}/{1} must be floats. Currently: '{2}'/'{3}' ".format(lo_key, hi_key, lo, hi))
                elif float(lo) >= float(hi):
                    error_list.append("Orientation {0} must be less than {1}. Currently: {2} >= {3} ".format(lo_key, hi_key, lo, hi))

        slew = ori.get('slew_rate_dps')
        if slew is not None and (not chk_float(slew) or float(slew) <= 0.0):
            error_list.append("Orientation slew_rate_dps must be a positive float. Currently it is: '{0}' ".format(slew))

        for key in ('settle_frames', 'scan_dwell_frames'):
            val = ori.get(key)
            if val is not None and (not chk_int(val) or int(val) <= 0):
                error_list.append("Orientation {0} must be a positive integer. Currently it is: '{1}' ".format(key, val))

        min_ss = ori.get('min_sync_state')
        if min_ss is not None and (not chk_int(min_ss) or not (0 <= int(min_ss) <= 6)):
            error_list.append("Orientation min_sync_state must be an integer in 0-6. Currently it is: '{0}' ".format(min_ss))

    # ---> Scaling / clustering keys (all new and optional) <---
    error_list.extend(_check_scaling_keys(parameters, en_hw_check))

    return error_list


"""
----------------------------------------------------------------------------
    Scaling / clustering keys (design spec A.1, A.4, B.6, C.2, C.4.1, D.6.1,
    G.2, I.3, J.4). Every key is new and optional; an INI without them
    validates exactly as before, except the num_ch/cpi_size/corr_size
    limits checked in check_ini() above.
----------------------------------------------------------------------------
"""
# federation_common (stdlib only) owns the membership grammar; appended, not
# prepended, so _daq_core never shadows a caller's own modules.
_DAQ_CORE_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "_daq_core")
if _DAQ_CORE_DIR not in sys.path:
    sys.path.append(_DAQ_CORE_DIR)
try:
    import federation_common as _fc
except ImportError:  # a stripped-down copy without _daq_core
    _fc = None

#: The 128-byte :1130 message carries 124 parameter bytes = 31 channel values
MAX_NUM_CH = 31
#: Worst-case decimator_out / delay_sync_iq segment holds 2^23 samples per channel
MAX_FRAME_SAMPLES = 2 ** 23
#: Largest serial an 8-digit EEPROM serial string can hold; base + 30 must fit
SERIAL_MAX = 99999999
SERIAL_BASE_MAX = SERIAL_MAX - MAX_NUM_CH
DEFAULT_SERIAL_BASE = 1000
#: federation_coordinator's election_settle_polls when the key is absent
DEFAULT_ELECTION_SETTLE_POLLS = 5
#: inih (rtl_daq) keeps INI_MAX_LINE - 1 = 199 bytes of a line
INI_MAX_LINE_BYTES = 199
#: Base ports of instance 0 (instance N adds N * port_stride)
BASE_PORT_IQ, BASE_PORT_CTL, BASE_PORT_ZMQ = 5000, 5001, 1130
DEFAULT_STATUS_PORT, DEFAULT_ZMQ_PUB_PORT = 5002, 5003
DEFAULT_COORDINATOR_PORT, DEFAULT_ROUTER_PORT, DEFAULT_ROUTER_STATS_PORT = 6000, 7000, 7001
#: USB ids of RTL2832U receivers (KrakenSDR / RTL-SDR)
RTL_VENDOR_ID = "0bda"
RTL_PRODUCT_IDS = ("2838", "2832")
DEFAULT_SYSFS_ROOT = "/sys/bus/usb/devices"
DEFAULT_RUN_DIR = "/run/heimdall"
#: Host names shipped by common fleet images (C.3)
COMMON_IMAGE_HOSTNAMES = ("raspberrypi", "krakensdr", "kraken", "ubuntu", "debian",
                          "dietpi", "orangepi", "armbian", "localhost")
ZMQ_LISTEN_ADDRESSES = ("*", "0.0.0.0", "127.0.0.1", "localhost")
FRAME_PERIOD_WARN_SEC = 8.0
STATUS_REFRESH_WARN_SEC = 60.0

_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_BOOL = ("bool",)


def _strict_int(value):
    """int of a plain decimal string (what strtol()/getint() both accept), else None."""
    s = str(value).strip()
    return int(s) if _INT_RE.match(s) else None


def _finite_float(value):
    try:
        f = float(str(value).strip())
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _range_text(lo, hi):
    return ">= {0}".format(lo) if hi is None else "in {0}..{1}".format(lo, hi)


def _rule_error(section, key, raw, rule):
    """Error text when *raw* violates *rule*, else None.

    Rules: ("bool",), ("int", lo, hi), ("opt_int", lo, hi) (empty = absent),
    ("auto_int", lo, hi), ("float", lo, hi),
    ("auto_float", lo) and ("enum", choices); hi None = unbounded.
    ``auto_int`` also accepts ``auto`` in any case and treats an empty value
    as absent (offload_engines.resolve_workers()).
    """
    kind = rule[0]
    v = raw.strip()
    name = "[{0}] {1}".format(section, key)
    if kind == "bool":
        if v not in ("0", "1"):
            return "{0} must be 0 or 1. Currently it is: '{1}' ".format(name, raw)
    elif kind in ("int", "opt_int"):
        n = _strict_int(v)
        if kind == "opt_int" and not v:
            return None  # empty = absent (federation_coordinator's opt_int)
        if n is None or n < rule[1] or (rule[2] is not None and n > rule[2]):
            return "{0} must be an integer {1}. Currently it is: '{2}' ".format(
                name, _range_text(rule[1], rule[2]), raw)
    elif kind == "auto_int":
        n = _strict_int(v)
        if v and v.lower() != "auto" and (
                n is None or n < rule[1] or (rule[2] is not None and n > rule[2])):
            return "{0} must be 'auto' or an integer {1}. Currently it is: '{2}' ".format(
                name, _range_text(rule[1], rule[2]), raw)
    elif kind == "float":
        f = _finite_float(v)
        if f is None or f < rule[1] or (rule[2] is not None and f > rule[2]):
            return "{0} must be a number {1}. Currently it is: '{2}' ".format(
                name, _range_text(rule[1], rule[2]), raw)
    elif kind == "auto_float":
        f = _finite_float(v)
        if v != "auto" and (f is None or f < rule[1]):
            return "{0} must be 'auto' or a number {1}. Currently it is: '{2}' ".format(
                name, _range_text(rule[1], None), raw)
    elif kind == "enum":
        if v not in rule[1]:
            return "{0} must be one of: {1}. Currently it is: '{2}' ".format(
                name, ", ".join(rule[1]), raw)
    return None


# Scalar keys of the instance INI. An empty value is an error for these (their
# consumers parse a number); list/string keys are validated separately.
_INSTANCE_RULES = {
    "hw": {
        "serial_base": ("int", 0, SERIAL_BASE_MAX),
    },
    "daq": {
        "usb_ring_buffers": ("int", 2, 64),
        "usb_async_transfers": ("int", 2, 64),
        "ctl_max_connections": ("int", 1, 64),
        "ctl_request_timeout_sec": ("float", 1.0, 600.0),
        "ctl_handler_max_sec": ("float", 1.0, 120.0),
        "ctl_xack_idle_sec": ("float", 0.0, 86400.0),
        "ctl_idle_timeout_sec": ("float", 0.0, 86400.0),
    },
    "pre_processing": {
        "size_segments_from_config": _BOOL,
    },
    "calibration": {
        "track_decimation": ("int", 1, None),
        "en_wire_recal": _BOOL,
        "defer_control_during_burst": _BOOL,
        "amplitude_tolerance_mode": ("enum", ("legacy", "symmetric")),
        "cal_stall_frames": ("int", 0, None),
        "recal_on_gain_change": _BOOL,
        "cal_timeout_frames": ("int", 0, None),
        "invalidate_on_overrun": _BOOL,
        "frames_after_overrun": ("int", 0, None),
    },
    "amplification": {
        "apply_bias_tee_at_init": _BOOL,
    },
    "offload": {
        "fft_workers": ("auto_int", 1, 64),
        "xcorr_workers": ("auto_int", 1, 64),
        "skip_copy_on_drop": _BOOL,
    },
    "data_interface": {
        "iq_server_max_clients": ("int", 1, 64),
        "iq_server_frame_deadline_ms": ("int", 0, None),
    },
    "database": {
        "instance_subdir": _BOOL,
        "write_instance_meta": _BOOL,
    },
    "monitoring": {
        "status_extended": _BOOL,
        "status_update_interval": ("int", 0, None),
        "stale_after_sec": ("float", 0.0, None),
        "status_max_clients": ("int", 1, 1024),
        "en_hwc_state_relay": _BOOL,
        "event_tag_identity": _BOOL,
        "event_topic_prefix": _BOOL,
        "stamp_decimator_drops": _BOOL,
        "stamp_cal_telemetry": _BOOL,
    },
    "host": {
        "rt_priority_offset": ("int", 0, 81),
        "allow_core_sharing": ("enum", ("warn", "refuse", "allow")),
        "host_tuning": ("enum", ("always", "auto", "1", "0")),
        "preflight": ("enum", ("off", "warn", "strict")),
        "instance_registry": _BOOL,
        "instance_guard": ("enum", ("legacy", "refuse")),
        "health_thread_sched_other": _BOOL,
    },
}

# Scalar [federation] keys: instance-side (C.2, C.4.5) plus the coordinator and
# router keys of federation.ini (C.4.1, D.3.3, E.1, I.3 grammar table).
_FEDERATION_RULES = {
    "coordinator_include_self": _BOOL,
    "router_include_self": _BOOL,
    "register_with_coordinator": _BOOL,
    "register_interval_sec": ("float", 0.5, 300.0),
    "health_mode": ("enum", ("mesh", "hub", "off")),
    "health_poll_interval": ("float", 0.2, 300.0),
    "peer_timeout": ("float", 0.1, 30.0),
    "peer_up_successes": ("int", 1, 10),
    "peer_down_misses": ("opt_int", 1, 20),
    "peer_flap_window_sec": ("float", 0.0, None),
    "peer_flap_threshold": ("int", 0, None),
    "emit_peer_health_events": _BOOL,
    "health_workers": ("int", 1, 64),
    "stamp_node_id": _BOOL,
    "fence_max_term_jump": ("int", 1, None),
    "router_port": ("int", 1, 65535),
    "router_stats_port": ("int", 0, 65535),
    "election_interval_sec": ("float", 0.05, 60.0),
    "election_missed_polls": ("int", 2, 20),
    "election_clock_margin": ("float", 0.0, 0.5),
    "election_failback_sec": ("float", 0.0, 3600.0),
    "require_quorum": _BOOL,
    "router_connect_on_demand": _BOOL,
    "reconcile_override": _BOOL,
    "reconcile_assignments": ("enum", ("auto", "0", "1")),
    "fanout_workers": ("int", 1, 1024),
    "max_handlers": ("int", 1, 1024),
    "reserved_handlers": ("int", 1, 1024),
    "peer_connect_timeout_sec": ("float", 0.1, 60.0),
    "xack_timeout_sec": ("float", 0.1, 60.0),
    "status_timeout_sec": ("float", 0.1, 60.0),
    "sync_push_timeout_sec": ("float", 0.1, 60.0),
    "hwc_request_timeout_sec": ("auto_float", 0.0),
    "member_stall_after_sec": ("auto_float", 0.0),
    "router_read_timeout_sec": ("auto_float", 0.0),
    "max_member_rto_sec": ("float", 5.0, 600.0),
    "status_cache_sec": ("float", 0.0, None),
    "mutation_wait_sec": ("float", 0.0, None),
    "breaker_open_sec": ("float", 0.0, None),
    "xack_cache_sec": ("float", 0.0, None),
    "reconcile_interval_sec": ("float", 0.0, None),
    "register_retain_sec": ("float", 0.0, None),
    "dns_ttl_sec": ("float", 0.0, None),
    "router_first_frame_timeout_sec": ("float", 0.0, None),
    "router_reconnect_sec": ("float", 0.0, None),
    "router_send_timeout_sec": ("float", 0.0, None),
    "breaker_failures": ("int", 1, None),
    "reconcile_max_attempts": ("int", 1, None),
    "register_expiry_factor": ("int", 1, None),
    "schc_rtt_budget_ms": ("int", 1, None),
    "schc_window": ("int", 1, 64),
    "router_queue_frames": ("int", 1, 1024),
    "router_max_clients": ("int", 1, 256),
    "router_align_max_frames": ("int", 1, 64),
    "router_align_window_ms": ("int", 0, None),
    "router_max_skew_ms": ("int", 0, None),
    "router_drop_policy": ("enum", ("oldest", "newest", "disconnect")),
    "router_tag_mode": ("enum", ("instance", "unit", "node")),
}

# Keys that need federation_common (membership / identity grammar)
_FC_KEYS = ("node_id", "cluster_id", "auth_token", "cluster_members", "peer_list",
            "router_sources", "advertise_host")


def _sec(parameters, name):
    sec = parameters.get(name)
    return sec if isinstance(sec, dict) else {}


def _val(section, key, default=None):
    """Stripped value of *key*, *default* when the key is absent."""
    v = section.get(key)
    return default if v is None else v.strip()


def _int_or(section, key, default):
    v = _strict_int(section[key]) if key in section else None
    return default if v is None else v


def _float_or(section, key, default):
    v = _finite_float(section[key]) if key in section else None
    return default if v is None else v


def _flag(section, key):
    return _val(section, key, "0") == "1"


# ---------------------------------------------------------------------------
# Serial keys (A.1)
# ---------------------------------------------------------------------------

def serial_keys_set(hw_params):
    """True when [hw] serial_base is present or device_serials is non-empty
    (the rule rtl_daq applies: an empty device_serials counts as absent)."""
    return "serial_base" in hw_params or bool(_val(hw_params, "device_serials", ""))


def resolve_serials(hw_params):
    """Per-channel serial strings rtl_daq opens (A.1 rule).

    A non-empty ``device_serials`` wins; otherwise channel i is
    ``str(serial_base + i)`` (``serial_base`` defaults to 1000). Raises
    ValueError with the device_map.c reason when the keys are invalid.
    """
    num_ch = _strict_int(hw_params.get("num_ch", ""))
    if num_ch is None or not 1 <= num_ch <= MAX_NUM_CH:
        raise ValueError("num_ch out of range 1..{0}".format(MAX_NUM_CH))
    serials_raw = _val(hw_params, "device_serials", "")
    if serials_raw:
        items = [x.strip() for x in serials_raw.split(",")]
        if len(items) != num_ch:
            raise ValueError("device_serials has {0} entries but num_ch is {1}".format(len(items), num_ch))
        for i, item in enumerate(items):
            if not (1 <= len(item) <= 8 and item.isascii() and item.isdigit()):
                raise ValueError("device_serials entry {0} is not a decimal serial of 1-8 digits".format(i))
            if item in items[:i]:
                raise ValueError("device_serials entry {0} duplicates entry {1}".format(i, items.index(item)))
        return items
    base = DEFAULT_SERIAL_BASE
    if "serial_base" in hw_params:
        base = _strict_int(hw_params["serial_base"])
        if base is None or not 0 <= base <= SERIAL_BASE_MAX:
            raise ValueError("serial_base out of range")
    return [str(base + i) for i in range(num_ch)]


def _antenna_gpio_error(raw):
    v = raw.strip()
    if v in ("", "none"):
        return None
    parts = [p.strip() for p in v.split(",")]
    pins = [_strict_int(p) for p in parts]
    if len(parts) == 2 and None not in pins and all(0 <= p <= 27 for p in pins) and pins[0] != pins[1]:
        return None
    return ("[hw] antenna_switch_gpio must be 'none' or two distinct GPIO numbers 0..27 as 'A,B'. "
            "Currently it is: '{0}' ".format(raw))


def _check_serial_keys(parameters, errors):
    hw = _sec(parameters, "hw")
    for key, rule in _INSTANCE_RULES["hw"].items():
        if key in hw:
            err = _rule_error("hw", key, hw[key], rule)
            if err:
                errors.append(err)
    if "antenna_switch_gpio" in hw:
        err = _antenna_gpio_error(hw["antenna_switch_gpio"])
        if err:
            errors.append(err)
    if not serial_keys_set(hw):
        return
    serials_raw = _val(hw, "device_serials", "")
    if serials_raw and len("device_serials = " + serials_raw) > INI_MAX_LINE_BYTES:
        errors.append("[hw] device_serials line is longer than the {0} bytes rtl_daq's INI reader keeps; "
                      "shorten it (e.g. drop the spaces after the commas)".format(INI_MAX_LINE_BYTES))
    try:
        resolved = resolve_serials(hw)
    except ValueError as e:
        if not str(e).startswith("num_ch") and not str(e).startswith("serial_base"):
            errors.append("[hw] {0}".format(e))
        return
    ctr = _strict_int(_sec(parameters, "daq").get("ctr_channel_serial_no", ""))
    # rtl_daq looks the control channel up by the string sprintf("%d", ctr)
    if ctr is not None and str(ctr) not in resolved:
        errors.append("[daq] ctr_channel_serial_no {0} is not one of the configured serials {1} "
                      "([hw] serial_base/device_serials; rtl_daq compares the serial strings, so "
                      "write them without leading zeros)".format(ctr, ", ".join(resolved)))


# ---------------------------------------------------------------------------
# Generic grammar helpers
# ---------------------------------------------------------------------------

def _parse_endpoint(text):
    """``host:port`` / ``[v6]:port`` -> (host, port) or None."""
    text = text.strip()
    if text.startswith("["):
        close = text.find("]")
        if close <= 1 or text[close + 1:close + 2] != ":":
            return None
        host, port = text[1:close], text[close + 2:]
    else:
        host, sep, port = text.rpartition(":")
        if not sep or ":" in host:
            return None
    if not host or any(c.isspace() or c in "/@[]," for c in host):
        return None
    p = _strict_int(port) if port.isdigit() else None
    if p is None or not 1 <= p <= 65535:
        return None
    return host, p


def _int_list_error(section, key, raw, exact=None):
    items = [x.strip() for x in raw.split(",")] if raw.strip() else []
    vals = [_strict_int(x) for x in items]
    if None in vals or any(v < 0 for v in vals):
        return "[{0}] {1} must be a comma list of non-negative integers. Currently it is: '{2}' ".format(section, key, raw)
    if exact is not None and (len(vals) != exact or len(set(vals)) != exact):
        return "[{0}] {1} must list exactly {2} distinct core numbers. Currently it is: '{3}' ".format(section, key, exact, raw)
    return None


def _check_host(parameters, en_hw_check, errors):
    if "host" not in parameters:
        return
    host = _sec(parameters, "host")
    for key, rule in _INSTANCE_RULES["host"].items():
        if key in host:
            err = _rule_error("host", key, host[key], rule)
            if err:
                errors.append(err)
    cores_raw = _val(host, "cpu_cores", "")
    if cores_raw:
        err = _int_list_error("host", "cpu_cores", cores_raw, exact=4)
        if err:
            errors.append(err)
        elif en_hw_check:
            nproc = os.cpu_count() or 1
            too_big = [c for c in (_strict_int(x) for x in cores_raw.split(",")) if c >= nproc]
            if too_big:
                errors.append("[host] cpu_cores {0} exceed the {1} CPU cores of this host".format(too_big, nproc))
    for key in ("delay_sync_cores",):
        raw = _val(host, key, "")
        if raw:
            err = _int_list_error("host", key, raw)
            if err:
                errors.append(err)
    irq = _val(host, "usb_irq_core", "")
    if irq and (_strict_int(irq) is None or _strict_int(irq) < 0):
        errors.append("[host] usb_irq_core must be a non-negative integer. Currently it is: '{0}' ".format(irq))
    if "\n" in host.get("usb_controller", ""):
        errors.append("[host] usb_controller must be a single line")
    if _val(host, "instance_guard") == "refuse" and _val(host, "instance_registry", "0") != "1":
        errors.append("[host] instance_guard = refuse requires [host] instance_registry = 1")


def _check_cidrs(raw):
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            return "[federation] allow_cidrs entry '{0}' is not an IPv4/IPv6 CIDR".format(item)
    return None


def _check_federation_scalars(fed, errors):
    """Grammar shared by the instance INI and federation.ini (I.3 table)."""
    for key, rule in _FEDERATION_RULES.items():
        if key in fed:
            err = _rule_error("federation", key, fed[key], rule)
            if err:
                errors.append(err)
    # Pairwise-distinct service ports (router_stats_port 0 = off). Only once a
    # router key is set: coordinator_port alone is an existing key and keeps
    # validating as before.
    ports = []
    if any(k.startswith("router_") for k in fed):
        for key, default in (("coordinator_port", DEFAULT_COORDINATOR_PORT),
                             ("router_port", DEFAULT_ROUTER_PORT),
                             ("router_stats_port", DEFAULT_ROUTER_STATS_PORT)):
            p = _int_or(fed, key, default)
            if p:
                ports.append((key, p))
    for i, (k1, p1) in enumerate(ports):
        for k2, p2 in ports[i + 1:]:
            if p1 == p2:
                errors.append("[federation] {0} and {1} are both {2}; they must differ".format(k1, k2, p1))
    # Election settle bound: >= ceil(L*(1+eps)/I) + 1 with L = I*K
    # (checked whenever any election key is set: the coordinator applies the
    # default settle value 5 when only missed_polls/clock_margin are raised)
    if any(key in fed for key in ("election_settle_polls", "election_missed_polls",
                                  "election_clock_margin")):
        k = _int_or(fed, "election_missed_polls", 3)
        eps = _float_or(fed, "election_clock_margin", 0.05)
        bound = int(math.ceil(round(k * (1.0 + eps), 9))) + 1
        rule = "(ceil(election_missed_polls*(1+election_clock_margin)) + 1)"
        if "election_settle_polls" in fed:
            v = _strict_int(fed["election_settle_polls"])
            if v is None or v < bound:
                errors.append("[federation] election_settle_polls must be an integer >= {0} {1}. "
                              "Currently it is: '{2}' ".format(bound, rule, fed["election_settle_polls"]))
        elif DEFAULT_ELECTION_SETTLE_POLLS < bound:
            errors.append("[federation] the default election_settle_polls = {0} is below the safe minimum "
                          "{1} {2}; set election_settle_polls to at least {1}".format(
                              DEFAULT_ELECTION_SETTLE_POLLS, bound, rule))
    cands = _val(fed, "coordinator_candidates", "")
    cand_set = []
    if cands:
        for item in [x.strip() for x in cands.split(",")]:
            ep = _parse_endpoint(item)
            if ep is None:
                errors.append("[federation] coordinator_candidates entry '{0}' is not host:port".format(item))
                continue
            key = (ep[0].lower(), ep[1])
            if key in cand_set:
                errors.append("[federation] coordinator_candidates lists '{0}' twice".format(item))
            cand_set.append(key)
    cself = _val(fed, "coordinator_self", "")
    if cself:
        ep = _parse_endpoint(cself)
        if ep is None:
            errors.append("[federation] coordinator_self '{0}' is not host:port".format(cself))
        elif cands and (ep[0].lower(), ep[1]) not in cand_set:
            errors.append("[federation] coordinator_self '{0}' is not one of coordinator_candidates".format(cself))
    endpoint = _val(fed, "router_stats_endpoint", "")
    if endpoint and _parse_endpoint(endpoint) is None:
        errors.append("[federation] router_stats_endpoint '{0}' is not host:port".format(endpoint))
    cidrs = _val(fed, "allow_cidrs", "")
    if cidrs:
        err = _check_cidrs(cidrs)
        if err:
            errors.append(err)
    if "\n" in fed.get("state_dir", ""):
        errors.append("[federation] state_dir must be a single line")
    if _fc is None:
        return
    for key, check in (("node_id", _fc.is_valid_node_id), ("cluster_id", _fc.is_valid_cluster_id)):
        v = _val(fed, key, "")
        if v and not check(v):
            errors.append("[federation] {0} '{1}' does not match the {0} grammar".format(key, v))
    token = fed.get("auth_token", "")
    if token.strip() and not _fc.is_valid_auth_token(token.strip()):
        errors.append("[federation] auth_token must be printable ASCII without spaces, at most {0} "
                      "characters".format(_fc.AUTH_TOKEN_MAX_LEN))
    adv = _val(fed, "advertise_host", "")
    if adv:
        try:
            _fc.self_host(adv)
            if any(c.isspace() or c in "/@," for c in adv):
                raise ValueError("advertise_host '{0}' is not a host name".format(adv))
        except ValueError as e:
            errors.append("[federation] {0}".format(e))
    members = _val(fed, "cluster_members", "")
    if members:
        try:
            _fc.parse_member_list(members, default_stride=_int_or(fed, "port_stride", 100))
        except ValueError as e:
            errors.append("[federation] cluster_members: {0}".format(e))
    sources = _val(fed, "router_sources", "")
    if sources:
        try:
            _fc.parse_router_source_list(sources, tag_mode=_val(fed, "router_tag_mode", "instance"),
                                         default_stride=_int_or(fed, "port_stride", 100))
        except ValueError as e:
            errors.append("[federation] router_sources: {0}".format(e))


# ---------------------------------------------------------------------------
# Instance identity, new-key mode and ports (C.2, I.3)
# ---------------------------------------------------------------------------

def is_new_key_mode(parameters):
    """True when the INI opts into the cluster features (spec 0.4).

    Then port-window problems are errors instead of warnings.
    """
    fed = _sec(parameters, "federation")
    host = _sec(parameters, "host")
    return bool(_val(fed, "cluster_members", "") or _val(fed, "coordinator_candidates", "")
                or _val(fed, "register_with_coordinator") == "1"
                or any(k.startswith("router_") for k in fed)
                or _val(host, "instance_registry") == "1"
                or _val(host, "instance_guard") == "refuse")


def _identity(parameters):
    """(instance_id, port_stride) with today's fallbacks, or None when invalid."""
    fed = _sec(parameters, "federation")
    iid = _strict_int(fed.get("instance_id", "0"))
    stride = _strict_int(fed.get("port_stride", "100"))
    if iid is None or iid < 0 or stride is None or stride <= 0:
        return None
    return iid, stride


def enabled_ports(parameters):
    """{name: port} of the ports this instance listens on (I.3).

    ``iq``, ``ctl`` and ``zmq`` always; ``status`` when ``en_monitoring = 1
    and en_status_server = 1``; ``events`` when ``en_monitoring = 1 and
    en_zmq_pub = 1``. None when instance_id/port_stride are invalid.
    """
    ident = _identity(parameters)
    if ident is None:
        return None
    iid, stride = ident
    off = iid * stride
    ports = {"iq": BASE_PORT_IQ + off, "ctl": BASE_PORT_CTL + off, "zmq": BASE_PORT_ZMQ + off}
    mon = _sec(parameters, "monitoring")
    if _flag(mon, "en_monitoring"):
        if _flag(mon, "en_status_server"):
            ports["status"] = _int_or(mon, "status_server_port", DEFAULT_STATUS_PORT) + off
        if _flag(mon, "en_zmq_pub"):
            ports["events"] = _int_or(mon, "zmq_pub_port", DEFAULT_ZMQ_PUB_PORT) + off
    return ports


def _local_service_ports(parameters):
    """[(name, port)] of the federation service ports on this host (I.3)."""
    fed = _sec(parameters, "federation")
    out = []
    local_coord = False
    if _fc is not None:
        if _val(fed, "coordinator_host", ""):
            local_coord = _fc.is_local_host(_val(fed, "coordinator_host"))
        for item in [x.strip() for x in _val(fed, "coordinator_candidates", "").split(",") if x.strip()]:
            ep = _parse_endpoint(item)
            if ep is not None and _fc.is_local_host(ep[0]):
                local_coord = True
    if local_coord:
        out.append(("coordinator", _int_or(fed, "coordinator_port", DEFAULT_COORDINATOR_PORT)))
    if any(k.startswith("router_") for k in fed):
        out.append(("router", _int_or(fed, "router_port", DEFAULT_ROUTER_PORT)))
        stats = _int_or(fed, "router_stats_port", DEFAULT_ROUTER_STATS_PORT)
        if stats:
            out.append(("router stats", stats))
    return out


def _first_colliding_id(bases, stride, svc_port):
    """Smallest instance id whose port base + id*stride equals *svc_port*."""
    ids = [(svc_port - b) // stride for b in bases if svc_port >= b and (svc_port - b) % stride == 0]
    return min(ids) if ids else None


def _port_findings(parameters):
    """Port-window problems of an instance INI as (kind, text).

    kind ``mode``: error in new-key mode, warning otherwise; ``warn``:
    always a warning (I.3 table).
    """
    out = []
    ident = _identity(parameters)
    ports = enabled_ports(parameters)
    if ident is None or ports is None:
        return out
    iid, stride = ident
    if stride < 4:
        if iid > 0:
            out.append(("mode", "[federation] port_stride = {0} is below 4 on instance {1}: its ports overlap "
                                "another instance's 5000-5003 window".format(stride, iid)))
        else:
            out.append(("warn", "[federation] port_stride = {0} is below 4: instances 0 and 1 would share "
                                "ports in the 5000-5003 window".format(stride)))
    for name, port in sorted(ports.items()):
        if port > 65535:
            out.append(("mode", "{0} port {1} of instance {2} is above 65535".format(name, port, iid)))
    seen = {}
    for name, port in sorted(ports.items()):
        if port in seen:
            out.append(("mode", "{0} port and {1} port of instance {2} are both {3}".format(seen[port], name, iid, port)))
        else:
            seen[port] = name
    off = iid * stride
    bases = [p - off for p in ports.values()]
    for svc, svc_port in _local_service_ports(parameters):
        if svc_port in seen:
            first = _first_colliding_id(bases, stride, svc_port)
            hint = ("use instance ids 0-{0}".format(first - 1) if first else "move the {0} port".format(svc))
            out.append(("mode", "port {0} of instance {1} collides with the {2} port; with stride {3} {4}".format(
                svc_port, iid, svc, stride, hint)))
    ids = sorted(set(range(16)) | {iid})
    for i in ids:
        zmq = BASE_PORT_ZMQ + i * stride
        for j in ids:
            if i != j and BASE_PORT_IQ + j * stride <= zmq <= BASE_PORT_IQ + 3 + j * stride:
                out.append(("warn", "with port_stride {0} the ZMQ port {1} of instance {2} falls inside instance {3}'s "
                                    "{4}-{5} window".format(stride, zmq, i, j, BASE_PORT_IQ + j * stride,
                                                            BASE_PORT_IQ + 3 + j * stride)))
                return out
    return out


def _self_matches(member, iid, advertise_host):
    if member.get("instance_id") != iid:
        return False
    if _fc.is_local_host(member["host"]):
        return True
    try:
        return member["host"].lower() == _fc.self_host(advertise_host or None)
    except ValueError:
        return False


def _self_host_text(advertise_host):
    try:
        return _fc.self_host(advertise_host or None)
    except ValueError:
        return "<host>"


def _peer_list_findings(parameters):
    """peer_list problems as text (C.2): grammar, duplicates, contains self."""
    fed = _sec(parameters, "federation")
    raw = _val(fed, "peer_list", "")
    if not raw or _fc is None:
        return []
    ident = _identity(parameters)
    stride = ident[1] if ident else 100
    status_base = _int_or(_sec(parameters, "monitoring"), "status_server_port", DEFAULT_STATUS_PORT)
    out = []
    keys = []
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        try:
            m = _fc.parse_member(item, default_stride=stride, status_base_port=status_base,
                                 legacy_port_form=True)
        except ValueError as e:
            out.append("[federation] peer_list: {0}".format(e))
            continue
        if m["node_key"] in keys:
            out.append("[federation] peer_list lists '{0}' twice".format(m["node_key"]))
        keys.append(m["node_key"])
        if ident is not None and _self_matches(m, ident[0], _val(fed, "advertise_host", "")):
            out.append("[federation] peer_list entry '{0}' is this instance; it would poll itself".format(item))
    return out


def _peer_list_escalates(fed):
    return bool(_val(fed, "cluster_members", "") or _val(fed, "coordinator_candidates", "")
                or _val(fed, "register_with_coordinator") == "1")


def _check_federation_instance(parameters, errors):
    if "federation" not in parameters:
        return
    fed = _sec(parameters, "federation")
    _check_federation_scalars(fed, errors)
    if _val(fed, "register_with_coordinator") == "1" and _val(fed, "en_federation", "0") != "1":
        errors.append("[federation] register_with_coordinator = 1 requires en_federation = 1")
    if _val(fed, "health_mode") == "hub" and _val(fed, "register_with_coordinator") != "1":
        errors.append("[federation] health_mode = hub requires register_with_coordinator = 1 "
                      "(the peer table comes from the REGISTER reply)")
    if _peer_list_escalates(fed):
        errors.extend(_peer_list_findings(parameters))


def _check_scaling_keys(parameters, en_hw_check):
    """Errors for the new optional keys of every section (A.1, B.6, C.2, D.6.1, G.2, J.4)."""
    errors = []
    _check_serial_keys(parameters, errors)
    daq = _sec(parameters, "daq")
    if "zmq_listen_address" in daq:
        v = daq["zmq_listen_address"].strip()
        if v and v not in ZMQ_LISTEN_ADDRESSES:
            errors.append("[daq] zmq_listen_address must be one of: {0}. hw_controller and delay_sync "
                          "connect to tcp://localhost, so binding another address would cut the local "
                          "chain. Currently it is: '{1}' ".format(", ".join(ZMQ_LISTEN_ADDRESSES), v))
    for section, rules in _INSTANCE_RULES.items():
        if section in ("hw", "host"):
            continue
        sec = _sec(parameters, section)
        for key, rule in rules.items():
            if key in sec:
                err = _rule_error(section, key, sec[key], rule)
                if err:
                    errors.append(err)
    _check_host(parameters, en_hw_check, errors)
    _check_federation_instance(parameters, errors)
    if is_new_key_mode(parameters):
        errors.extend(text for kind, text in _port_findings(parameters) if kind == "mode")
    return errors


# ---------------------------------------------------------------------------
# Warnings (stderr, "WARNING: " prefix, exit code unchanged)
# ---------------------------------------------------------------------------

def frame_period_sec(parameters):
    """cpi_size * decimation_ratio / sample_rate, None when not computable."""
    pre = _sec(parameters, "pre_processing")
    cpi = _strict_int(pre.get("cpi_size", ""))
    ratio = _strict_int(pre.get("decimation_ratio", ""))
    fs = _strict_int(_sec(parameters, "daq").get("sample_rate", ""))
    if not cpi or not ratio or not fs or cpi < 1 or ratio < 1 or fs < 1:
        return None
    return cpi * ratio / float(fs)


def _fmt_sec(v):
    return "{0:.1f}".format(v)


def check_ini_warnings(parameters, en_hw_check=True):
    """Non-fatal findings of the I.3 / A.4 / C.2 / D.6.1 / G.2 rules.

    Pure like check_ini(): in ``no_hw`` mode it reads nothing but
    *parameters* (no sysfs, /proc or host registry). In hardware mode it
    also reports rtl_eeprom devices that are busy when serial keys are set.
    Returns a list of texts; ``__main__`` prints each as ``WARNING: <text>``
    on stderr.
    """
    warnings = []
    hw = _sec(parameters, "hw")
    daq = _sec(parameters, "daq")
    cal = _sec(parameters, "calibration")
    mon = _sec(parameters, "monitoring")
    fed = _sec(parameters, "federation")
    ident = _identity(parameters)
    en_fed = _val(fed, "en_federation", "0") == "1"
    registering = _val(fed, "register_with_coordinator") == "1"

    # Indistinguishable frames (G37)
    if ident and ident[0] > 0 and en_fed and _strict_int(hw.get("unit_id", "")) == 0:
        warnings.append("[hw] unit_id = 0 on instance {0} with federation enabled: frames from two "
                        "instances are indistinguishable (set [hw] unit_id = auto)".format(ident[0]))
    # Calibration (G79, G26)
    corr = _strict_int(cal.get("corr_size", ""))
    if corr and corr > 0 and corr % 1024 != 0:
        warnings.append("[calibration] corr_size = {0} is not a multiple of 1024; the fractional delay "
                        "estimator assumes it is".format(corr))
    burst = _strict_int(cal.get("cal_frame_burst_size", ""))
    max_fails = _strict_int(cal.get("maximum_sync_fails", ""))
    if _strict_int(cal.get("cal_track_mode", "")) == 2 and burst is not None and max_fails is not None \
            and burst < max_fails:
        warnings.append("[calibration] cal_frame_burst_size = {0} is below maximum_sync_fails = {1} with "
                        "cal_track_mode = 2: dummy frames reset the fail counter, so a sync loss is never "
                        "detected".format(burst, max_fails))
    # Port windows
    new_key = is_new_key_mode(parameters)
    for kind, text in _port_findings(parameters):
        if kind == "warn" or not new_key:
            warnings.append(text)
    # Frame period vs control timeouts (D.6.1)
    t_fp = frame_period_sec(parameters)
    if t_fp is not None:
        if t_fp > FRAME_PERIOD_WARN_SEC:
            warnings.append("the frame period is {0} s (cpi_size x decimation_ratio / sample_rate), above "
                            "{1:.0f} s: control replies, status refreshes and stall detection all wait "
                            "frames".format(_fmt_sec(t_fp), FRAME_PERIOD_WARN_SEC))
        rto = _float_or(daq, "ctl_request_timeout_sec", 10.0)
        if rto < 2.0 * t_fp:
            warnings.append("[daq] ctl_request_timeout_sec = {0:g} is below 2 x the frame period ({1} s): "
                            "control verbs will time out on a healthy chain".format(rto, _fmt_sec(t_fp)))
    if (en_fed or registering) and _int_or(daq, "ctl_max_connections", 1) == 1:
        warnings.append("a resident DoA client blocks every coordinator and heimdall-ctl command on this "
                        "member (set [daq] ctl_max_connections = 4)")
    # STATUS freshness (G.2, R88)
    if _flag(mon, "en_monitoring") and _flag(mon, "en_status_server"):
        heartbeat = _int_or(mon, "heartbeat_interval", 100)
        update = _int_or(mon, "status_update_interval", 0)
        k_eff = update if update > 0 else heartbeat
        if k_eff <= 0:
            warnings.append("the STATUS snapshot is never refreshed (set [monitoring] status_update_interval)")
        elif t_fp is not None and k_eff * t_fp > STATUS_REFRESH_WARN_SEC:
            warnings.append("the STATUS snapshot refreshes every {0} s; set [monitoring] status_update_interval "
                            "so health and stall detection react faster".format(_fmt_sec(k_eff * t_fp)))
    # Registration from a fleet image with a default host name (C.3)
    if registering and not _val(fed, "advertise_host", ""):
        hostname = socket.gethostname().split(".", 1)[0].lower()
        if hostname in COMMON_IMAGE_HOSTNAMES:
            warnings.append("register_with_coordinator = 1 on a host named '{0}' (a common image default) "
                            "with an empty advertise_host: node keys may collide (set [federation] "
                            "advertise_host or node_id)".format(hostname))
    # Membership (C.2)
    if _fc is None:
        if any(_val(fed, k, "") for k in _FC_KEYS):
            warnings.append("_daq_core/federation_common.py is not importable; [federation] membership and "
                            "identity checks skipped")
    else:
        if not _peer_list_escalates(fed):
            warnings.extend(_peer_list_findings(parameters))
        members_raw = _val(fed, "cluster_members", "")
        if members_raw and _val(fed, "peer_list", ""):
            warnings.append("peer_list overrides cluster_members for health polling")
        if members_raw and en_fed and ident is not None:
            try:
                members = _fc.parse_member_list(members_raw, default_stride=ident[1])
            except ValueError:
                members = None  # reported as an error by check_ini()
            adv = _val(fed, "advertise_host", "")
            if members is not None and not any(_self_matches(m, ident[0], adv) for m in members):
                warnings.append("cluster_members does not contain this instance (expected {0}:{1})".format(
                    _self_host_text(adv), ident[0]))
    # Inert worker keys (R72)
    for section, key in (("pre_processing", "fft_workers"), ("calibration", "xcorr_workers")):
        if key in _sec(parameters, section):
            warnings.append("[{0}] {1} is not read by any process; use [offload] {1}".format(section, key))
    # Busy receivers (hardware mode with serial keys)
    if en_hw_check and serial_keys_set(hw):
        warnings.extend(_busy_device_warnings(parameters))
    return warnings


def _busy_device_warnings(parameters):
    busy = [d for d in probe_eeprom_devices(range(count_receivers())) if d["busy"]]
    if not busy:
        return []
    readable = set(d["serial"] for d in probe_eeprom_devices(range(count_receivers())))
    devices = enumerate_sdr_sysfs() or []
    unread = sorted(set(d["serial"] for d in devices if d["serial"] and d["serial"] not in readable))
    owners = _registry_serial_owners(read_live_registry(exclude_instance=(_identity(parameters) or (None,))[0]))
    hints = []
    for s in unread:
        entry = owners.get(s)
        if entry is not None:
            hints.append("serial {0} is used by instance {1} running from {2}".format(
                s, entry.get("instance_id"), entry.get("cwd")))
        else:
            hints.append("serial {0} is held by a process outside the registry".format(s))
    text = "device index(es) {0} busy (claimed by another process; rtl_eeprom could not read them)".format(
        ", ".join(str(d["index"]) for d in busy))
    if hints:
        text += ": " + "; ".join(hints)
    return [text]


# ---------------------------------------------------------------------------
# Attached devices and the host registry (A.4) - only with the 'attached' token
# ---------------------------------------------------------------------------

def _read_attr(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def enumerate_sdr_sysfs(root=None):
    """RTL2832U receivers listed in sysfs; opens no device.

    Returns ``[{"busnum", "devnum", "path", "serial", "product_id"}]`` for
    every device with ``idVendor == 0bda`` and ``idProduct`` 2838/2832
    (``serial`` None when the attribute is missing), sorted by sysfs name.
    *root* defaults to ``$HEIMDALL_SYSFS_ROOT`` else /sys/bus/usb/devices.
    Returns None when the root does not exist (non-Linux, some containers).
    """
    if root is None:
        root = os.environ.get("HEIMDALL_SYSFS_ROOT") or DEFAULT_SYSFS_ROOT
    if not os.path.isdir(root):
        return None
    devices = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        path = os.path.join(root, name)
        vendor = (_read_attr(os.path.join(path, "idVendor")) or "").lower()
        product = (_read_attr(os.path.join(path, "idProduct")) or "").lower()
        if vendor != RTL_VENDOR_ID or product not in RTL_PRODUCT_IDS:
            continue
        devices.append({
            "busnum": _strict_int(_read_attr(os.path.join(path, "busnum")) or ""),
            "devnum": _strict_int(_read_attr(os.path.join(path, "devnum")) or ""),
            "path": path,
            "serial": _read_attr(os.path.join(path, "serial")) or None,
            "product_id": product,
        })
    return devices


def _run_dirs(run_dir=None):
    if run_dir is not None:
        return [run_dir]
    dirs = [os.environ.get("HEIMDALL_RUN_DIR") or DEFAULT_RUN_DIR]
    if hasattr(os, "getuid"):
        dirs.append("/tmp/heimdall-run-{0}".format(os.getuid()))
    return list(dict.fromkeys(dirs))


def _process_args(pid):
    proc_root = os.environ.get("HEIMDALL_PROC_ROOT") or "/proc"
    cmdline = os.path.join(proc_root, str(pid), "cmdline")
    if os.path.isdir(proc_root) and os.path.exists(os.path.join(proc_root, str(pid))):
        try:
            with open(cmdline, "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return ""
    try:
        res = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def _pid_role_alive(pid, role):
    """True when *pid* runs and its command line contains *role*
    (the hd_pid_role_alive rule of daq_common.sh)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    return str(role) in _process_args(pid)


def read_live_registry(run_dir=None, exclude_instance=None):
    """Live host-registry entries (``$RUN/inst<N>.json``, B.3.1).

    *run_dir* defaults to ``$HEIMDALL_RUN_DIR`` (else /run/heimdall) plus the
    ``/tmp/heimdall-run-<uid>`` fallback. An entry is live when any of its
    ``pids`` runs with a matching role name. Entries of *exclude_instance*
    and unreadable files are skipped. Only instances started with
    ``[host] instance_registry = 1`` have entries.
    """
    live = []
    for d in _run_dirs(run_dir):
        for path in sorted(glob.glob(os.path.join(d, "inst*.json"))):
            try:
                with open(path, "r") as f:
                    entry = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("pids"), dict):
                continue
            iid = entry.get("instance_id")
            if exclude_instance is not None and iid == exclude_instance:
                continue
            if any(_pid_role_alive(pid, role) for role, pid in entry["pids"].items()):
                live.append(entry)
    return live


def _registry_serial_owners(entries):
    owners = {}
    for entry in entries:
        serials = entry.get("serials")
        if isinstance(serials, list):
            for s in serials:
                owners.setdefault(str(s), entry)
    return owners


def _suggest_unit(devices):
    used = set(_strict_int(d["serial"]) for d in devices if d["serial"])
    k = 0
    while any(DEFAULT_SERIAL_BASE + 100 * k <= s <= DEFAULT_SERIAL_BASE + 100 * k + 99 for s in used if s is not None):
        k += 1
    return k


def check_attached_serials(parameters, sysfs_root=None, run_dir=None):
    """Serial checks against the attached receivers and the host registry.

    Run by ``__main__`` only with the ``attached`` token (daq_start_sm.sh).
    Returns ``(errors, warnings)``. With [hw] serial_base/device_serials
    set: a configured serial that is not attached, one present on more than
    one device, or one attached and listed by another live instance is an
    error. Without them, the duplicate and registry conditions are warnings,
    and nothing is reported unless at least one RTL2832U is attached.
    """
    errors, warnings = [], []
    hw = _sec(parameters, "hw")
    keys = serial_keys_set(hw)
    try:
        resolved = resolve_serials(hw)
    except ValueError:
        return errors, warnings  # grammar errors come from check_ini()
    devices = enumerate_sdr_sysfs(sysfs_root)
    if devices is None:
        if keys:
            warnings.append("sysfs not available; attached-serial checks skipped")
        return errors, warnings
    if not keys and not devices:
        return errors, warnings
    paths = {}
    for d in devices:
        if d["serial"]:
            paths.setdefault(d["serial"], []).append(d["path"])
    ident = _identity(parameters)
    owners = _registry_serial_owners(read_live_registry(run_dir, exclude_instance=ident[0] if ident else None))
    report = errors if keys else warnings
    for ch, serial in enumerate(resolved):
        found = paths.get(serial, [])
        if keys and not found:
            errors.append("configured serial {0} (channel {1}) is not attached (sysfs); plug in the unit or "
                          "fix [hw] serial_base/device_serials".format(serial, ch))
        if len(found) > 1:
            report.append("serial {0} (channel {1}) is present on {2} devices ({3}); plug in one unit at a "
                          "time and program distinct serials with util/set_unit_serials.sh --unit {4}".format(
                              serial, ch, len(found), ", ".join(found), _suggest_unit(devices)))
        owner = owners.get(serial)
        if found and owner is not None:
            report.append("serial {0} is used by instance {1} running from {2}".format(
                serial, owner.get("instance_id"), owner.get("cwd")))
    return errors, warnings


def check_live_registry(parameters, run_dir=None, config_dir=None):
    """Port and database-directory overlap with other live instances (I.3, B.2).

    Run by ``__main__`` only with the ``attached`` token. Returns
    ``(errors, warnings)``: a shared enabled port is an error in new-key
    mode and a warning otherwise; a shared database directory (``en_db = 1``,
    ``instance_subdir = 0``) is a warning. *config_dir* resolves a relative
    ``db_dir`` (default: the working directory).
    """
    errors, warnings = [], []
    ident = _identity(parameters)
    ports = enabled_ports(parameters)
    if ident is None or ports is None:
        return errors, warnings
    live = read_live_registry(run_dir, exclude_instance=ident[0])
    if not live:
        return errors, warnings
    report = errors if is_new_key_mode(parameters) else warnings
    mine = set(ports.values())
    db = _sec(parameters, "database")
    my_db = None
    if _flag(db, "en_db") and _val(db, "instance_subdir", "0") != "1" and _val(db, "db_dir", ""):
        my_db = os.path.realpath(os.path.join(config_dir or os.getcwd(), _val(db, "db_dir")))
    for entry in live:
        other = entry.get("ports") if isinstance(entry.get("ports"), dict) else {}
        for p in sorted(set(v for v in other.values() if isinstance(v, int)) & mine):
            report.append("port {0} of instance {1} is also used by instance {2} running from {3}".format(
                p, ident[0], entry.get("instance_id"), entry.get("cwd")))
        if my_db and entry.get("db_dir") and os.path.realpath(str(entry["db_dir"])) == my_db:
            warnings.append("[database] db_dir {0} is also used by instance {1} running from {2} (set "
                            "[database] instance_subdir = 1 or give each instance its own db_dir)".format(
                                my_db, entry.get("instance_id"), entry.get("cwd")))
    return errors, warnings


# ---------------------------------------------------------------------------
# federation.ini (coordinator / router) validation (I.3, R106)
# ---------------------------------------------------------------------------

def check_federation_ini(parameters):
    """Validate a coordinator/router ``federation.ini``.

    Needs only a ``[federation]`` section. Returns ``(errors, warnings)``:
    the grammar of every coordinator/router key, ``cluster_members`` and
    ``router_sources`` (C.2), the candidate list, and the port rules for the
    member entries that run on this host (always errors here).
    """
    errors, warnings = [], []
    if "federation" not in parameters:
        return ["federation.ini has no [federation] section"], warnings
    fed = _sec(parameters, "federation")
    for key in ("instance_id", "port_stride"):
        if key in fed and (_strict_int(fed[key]) is None or _strict_int(fed[key]) < (0 if key == "instance_id" else 1)):
            errors.append("[federation] {0} must be a {1} integer. Currently it is: '{2}' ".format(
                key, "non-negative" if key == "instance_id" else "positive", fed[key]))
    if "coordinator_port" in fed:
        err = _rule_error("federation", "coordinator_port", fed["coordinator_port"], ("int", 1, 65535))
        if err:
            errors.append(err)
    _check_federation_scalars(fed, errors)
    if _fc is None:
        errors.append("_daq_core/federation_common.py is not importable; federation.ini cannot be validated")
        return errors, warnings
    if _peer_list_escalates(fed):
        errors.extend(_peer_list_findings(parameters))
    else:
        warnings.extend(_peer_list_findings(parameters))
    errors.extend(_federation_member_port_errors(fed))
    # Warnings
    cands = [x for x in _val(fed, "coordinator_candidates", "").split(",") if x.strip()]
    if _val(fed, "require_quorum") == "1" and len(cands) < 3:
        if len(cands) == 2:
            warnings.append("require_quorum = 1 with 2 candidates: with 2 candidates, losing either host "
                            "blocks all mutations")
        else:
            warnings.append("require_quorum = 1 with {0} candidate(s): a quorum needs at least 3 "
                            "coordinator_candidates".format(len(cands)))
    listen = _val(_sec(parameters, "daq"), "listen_address", "0.0.0.0") or "0.0.0.0"
    if not fed.get("auth_token", "").strip() and not _val(fed, "allow_cidrs", "") and not _fc.is_loopback(listen):
        warnings.append("auth_token and allow_cidrs are empty and the listen address {0} is not loopback: "
                        "the coordinator accepts mutating commands from any host".format(listen))
    return errors, warnings


def _federation_member_port_errors(fed):
    """Port rules for the member entries hosted on this machine (I.3)."""
    stride = _int_or(fed, "port_stride", 100)
    if stride <= 0:
        return []
    cself = _parse_endpoint(_val(fed, "coordinator_self", "")) if _val(fed, "coordinator_self", "") else None
    self_host_name = cself[0].lower() if cself else None
    entries = []
    try:
        entries.extend(_fc.parse_member_list(_val(fed, "cluster_members", ""), default_stride=stride))
        for src in _fc.parse_router_source_list(_val(fed, "router_sources", ""), default_stride=stride,
                                                tag_mode=_val(fed, "router_tag_mode", "instance")):
            if src["kind"] == "iqserver":
                entries.append({"host": src["host"], "instance_id": src["instance_id"],
                                "node_key": src["node_key"],
                                "ports": _fc.compute_ports(src["instance_id"], stride)})
    except ValueError:
        return []  # grammar errors are reported by _check_federation_scalars()
    local = [m for m in entries if _fc.is_local_host(m["host"]) or
             (self_host_name is not None and m["host"].lower() == self_host_name)]
    service = [("coordinator", _int_or(fed, "coordinator_port", DEFAULT_COORDINATOR_PORT))]
    if any(k.startswith("router_") for k in fed):
        service.append(("router", _int_or(fed, "router_port", DEFAULT_ROUTER_PORT)))
        stats = _int_or(fed, "router_stats_port", DEFAULT_ROUTER_STATS_PORT)
        if stats:
            service.append(("router stats", stats))
    errors = []
    owner = {}
    done = set()
    for m in local:
        if m["node_key"] in done:
            continue  # the same member listed in cluster_members and router_sources
        done.add(m["node_key"])
        iid = m["instance_id"]
        if iid > 0 and stride < 4:
            errors.append("member {0}: port_stride = {1} is below 4".format(m["node_key"], stride))
        for name, port in sorted(m["ports"].items()):
            if port > 65535:
                errors.append("member {0}: {1} port {2} is above 65535".format(m["node_key"], name, port))
            elif port in owner and owner[port] != m["node_key"]:
                errors.append("member {0}: {1} port {2} is also used by member {3} on this host".format(
                    m["node_key"], name, port, owner[port]))
            owner.setdefault(port, m["node_key"])
            for svc, svc_port in service:
                if port == svc_port:
                    first = _first_colliding_id([p - iid * stride for p in m["ports"].values()], stride, svc_port)
                    hint = "use instance ids 0-{0}".format(first - 1) if first else "move the {0} port".format(svc)
                    errors.append("port {0} of instance {1} collides with the {2} port; with stride {3} {4}".format(
                        port, iid, svc, stride, hint))
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv):
    """CLI: python3 ini_checker.py [config_path] [no_hw] [attached] [--federation]

    Tokens in any order; any other token is the config path. Exit 0 -> valid,
    1 -> errors (or config unreadable). 'no_hw' skips ALL hardware probing
    (no lsusb / rtl_eeprom) and reads nothing but the INI. 'attached' adds
    the sysfs serial and host-registry checks (daq_start_sm.sh only).
    '--federation' validates a coordinator/router federation.ini instead.
    Errors are logged; warnings are printed to stderr as 'WARNING: <text>'.
    """
    config_path = 'daq_chain_config.ini'
    en_hw_check = True
    attached = False
    federation = False
    for arg in argv:
        if arg == "no_hw":
            en_hw_check = False
        elif arg == "attached":
            attached = True
        elif arg == "--federation":
            federation = True
        else:
            config_path = arg
    parameters = read_config_file(config_path)
    if parameters is None:
        logging.error("Unable to read configuration file: '{0}'".format(config_path))
        return 1
    if federation:
        error_list, warnings = check_federation_ini(parameters)
    else:
        try:
            error_list = check_ini(parameters, en_hw_check)
        except KeyError as e:
            logging.error("Mandatory configuration section/key missing: {0}".format(e))
            return 1
        warnings = check_ini_warnings(parameters, en_hw_check)
        if attached:
            for errs, warns in (check_attached_serials(parameters),
                                check_live_registry(parameters,
                                                    config_dir=os.path.dirname(os.path.abspath(config_path)))):
                error_list.extend(errs)
                warnings.extend(warns)
    for e in error_list:
        logging.error(e)
    for w in warnings:
        print("WARNING: " + w, file=sys.stderr)
    return 1 if error_list else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
