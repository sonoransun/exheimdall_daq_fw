#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
	Designs an FIR filter for the decimator module based on specified parameters found
	in the "daq_chain_config.ini" file
	
	For the proper FIR configuration please check the documentation of the firmware.

	After starting the daq chain you can check the transfer characteristics of the generated
	FIR filter at : "_logs/Decimator_filter_transfer.html"

	Federation instance N != 0 ([federation] instance_id) writes
	"_data_control/inst<N>_fir_coeffs.txt", the name decimate.out of the same instance
	reads (build_fifo_path() in sh_mem_util.c); instance 0 keeps "_data_control/fir_coeffs.txt".
	
	Project: HeIMDALL DAQ Firmware
	Author : Tamas Peto	
"""
import os
import sys
from scipy import signal
import numpy as np
#import plotly.graph_objects as go 
from configparser import ConfigParser

parser = ConfigParser()
found = parser.read(["daq_chain_config.ini"])
if not found:
	print("Configuration file not found. exiting")
	exit(-1)
decimation_ratio = parser.getint('pre_processing', 'decimation_ratio')
bandwidth = parser.getfloat('pre_processing', 'fir_relative_bandwidth')
tap_size = parser.getint('pre_processing', 'fir_tap_size')
window = parser.get('pre_processing', 'fir_window')
fs = parser.getfloat('daq', 'sample_rate')
fc = parser.getfloat('daq','center_freq')
try:
	instance_id = parser.getint('federation', 'instance_id', fallback=0)
except ValueError:
	print("ERROR: [federation] instance_id must be an integer, exiting..")
	exit(-1)

if decimation_ratio <1:
	print("ERROR: Decimation ratio can not be smaller than 1, exiting..")
	exit(-1)

if tap_size <= decimation_ratio and decimation_ratio !=1 :
	print("ERROR: FIR tap size must be higher than the decimation ratio. Please consider increasing the tap size, exiting..")
	exit(-1)

print("Desig FIR filter with the following parameters: ")
print("Decimation ratio: {:d}".format(decimation_ratio))
print("Bandwidth: {:1.2f}".format(bandwidth))
print("Tap size: {:d}".format(tap_size))
print("Window function:", window)

transfer_fname = "_logs/Decimator_filter_transfer.html"
unprefixed_coeffs_fname = "_data_control/fir_coeffs.txt"
if instance_id == 0:
	coeffs_fname = unprefixed_coeffs_fname
else:
	coeffs_fname = "_data_control/inst{:d}_fir_coeffs.txt".format(instance_id)
	# In a separate instance directory (its _data_control is private) an
	# unprefixed file can only be stale: remove it, so a decimate.out that still
	# reads the unprefixed name fails loudly instead of filtering with old taps.
	# In a shared working copy the unprefixed file belongs to instance 0: keep it.
	if os.path.realpath(os.getcwd()) != os.path.dirname(os.path.realpath(__file__)) \
			and os.path.exists(unprefixed_coeffs_fname):
		os.remove(unprefixed_coeffs_fname)
		print("removed stale unprefixed fir_coeffs.txt (instance {:d} uses inst{:d}_fir_coeffs.txt)"
			  .format(instance_id, instance_id))
# Fir filter parameters
cut_off = bandwidth/decimation_ratio
if cut_off < 1:
	# Design band pass FIR Filter
	b = signal.firwin(tap_size, cut_off, window=window)
else:
	b=np.array([1])

# Uncomment for debugging
"""
# Plot transfer function
w, h = signal.freqz(b=b, a=1, worN=2**16, whole=True)
h+= 10**-10 # To avoid operations with zero
h =np.fft.fftshift(h)
h_log = 20*np.log10(np.abs(h))

w= np.linspace(-fs/2,  fs/2, 2**16)+fc
fig = go.Figure()
fig.add_trace(go.Scatter(x=w/10**6, y=h_log,  line=dict(width=2, dash='solid')))   

# Export figure
fig.update_layout(
            title = "Decimator filter transfer function",
            xaxis_title = "Frequency [kHz]",
            yaxis_title = "Amplitude[dB]",
            font=dict(size=18),
            hovermode='x')

fig.write_html(transfer_fname)
"""
np.savetxt(coeffs_fname, b)
print("FIR filter ready")
print("Transfer funcfion is exported to : ", transfer_fname)
print("Coefficients are exported to: ",coeffs_fname)
exit(0)
