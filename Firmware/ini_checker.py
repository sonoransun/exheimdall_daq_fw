#!/usr/bin/env python3
import logging
import subprocess
import sys
from configparser import ConfigParser
import numpy as np

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
    found = parser.read([config_filename])
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
    lsusb_cmd = subprocess.run(["lsusb"], capture_output=True, text=True)
    lsusb_str = lsusb_cmd.stdout
    device_count = 0
    for line in lsusb_str.splitlines():
        if line.find("Realtek")>=0:device_count+=1
    logging.debug("Found {:d} receivers".format(device_count))
    return device_count
def get_serials():
    serial_nos = []
    for ch_ind in range(5):
        rtl_eeprom_cmd = subprocess.run(["rtl_eeprom", "-d", str(ch_ind)], capture_output=True, text=True)
        response    = rtl_eeprom_cmd.stderr
        response_l  = response.split('\n')
        for line in response_l:
            if line.find("Serial number:") >=0:
                serial_nos.append(int(line[line.find(':\t\t')+2:]))
    return serial_nos

# Initialize logger
logging.basicConfig(level=logging.ERROR)
valid_bias_enable_flag = [0, 1]
valid_gains = [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496]
valid_fir_windows = ['boxcar', 'triang', 'blackman', 'hamming', 'hann', 'bartlett', 'flattop', 'parzen' , 'bohman', 'blackmanharris', 'nuttall', 'barthann'] 
# See: https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.get_window.html#scipy.signal.get_window

def check_ini(parameters, en_hw_check=True):
    device_count = count_receivers()
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

    if not chk_int(hw_params['unit_id']):
        error_list.append("Unit ID must be integer. Currently it is: '{0}' ".format(hw_params['unit_id']))

    if not chk_int(hw_params['ioo_type']):
        error_list.append("IOO type must be integer. Currently it is: '{0}' ".format(hw_params['ioo_type']))

    if not chk_int(hw_params['num_ch']):
        error_list.append("Number of channels must be an integer. Currently it is: '{0}' ".format(hw_params['num_ch']))
    else:
        if int(hw_params['num_ch']) <= 0:
            error_list.append("Number of channels must be a non zero positive number. Currently it is: '{0}' ".format(hw_params['num_ch']))
        if en_hw_check and int(hw_params['num_ch']) > device_count:
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
    
    decimation_raito = -1
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

    if chk_int(preproc_params['fir_tap_size']) and chk_int(preproc_params['fir_tap_size']):
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

    # ---> Federation section check <---
    if 'federation' in daq_cfg:
        fed_params = daq_cfg['federation']

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

    return error_list

if __name__ == "__main__":    
    parameters = read_config_file('daq_chain_config.ini')
    en_hw_check=True
    if len(sys.argv) > 1:
        if sys.argv[1] == "no_hw":
            en_hw_check=False
    error_list = check_ini(parameters, en_hw_check)
    if len(error_list):
        for e in error_list:
            logging.error(e)
