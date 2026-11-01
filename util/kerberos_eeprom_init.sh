#!/bin/bash
#NOTE: Use for KerberosSDR Only
# For KrakenSDR units (several units on one host or in a cluster) use
# util/set_unit_serials.sh instead: it programs unit K to 1000 + 100*K + i and
# prints the matching [hw] serial_base for daq_chain_config.ini.

echo "This software will initialze KerberosSDR EEPROM content and set serial numbers"
device_count=$(lsusb | grep -c "Realtek")
echo "Found $device_count receivers"
while true; do
    read -r -p "Do you wish to overwrite the current EEPROM content?" yn
    case $yn in
        [Yy]* )echo "yes"; break;;
        [Nn]* )echo "no"; exit;;
        *) echo "Please answer yes or no"
    esac
done
device_cntr=$((device_count-1))
for i in $(eval echo "{0..$device_cntr}")
do
    dip_sw_count=$((i+1))
    read -r -p "Please turn off all DIP switches except #$dip_sw_count and press enter" _
    # Check the number of online devices
    curr_device_count=$(lsusb | grep -c "Realtek")
    if test "$curr_device_count" -ne 1
    then
        echo "More than one online device has been detected, exiting"
        #exit
    fi
    serial=$((i+1000))
    rtl_eeprom -d 0 -g realtek_oem
    rtl_eeprom -d 0 -s $serial -m RTL-SDR -p KerberosSDR
done
echo "EEPROM writing script finished. Plese perform a full power cycle."
