#!/usr/bin/env python3
"""
   Hardware Discovery for HeIMDALL DAQ Firmware.

   Probes the system for available accelerators (GPU, PCIe, USB3, DMA) and
   HAT peripherals, then computes recommended transport and engine settings
   for each pipeline stage.

   Runnable standalone:  python3 hw_discover.py
   Outputs JSON to stdout.

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
import platform
import re
import sys

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Individual probe functions
# ---------------------------------------------------------------------------

def discover_hat():
    """Check for HAT via device tree and EEPROM.

    Returns
    -------
    dict
        Keys: detected (bool), product (str or None), vendor (str or None),
        uuid (str or None), source (str).
    """
    result = {
        'detected': False,
        'product': None,
        'vendor': None,
        'uuid': None,
        'source': None,
    }

    # Method 1: Device tree (fastest, works on running Pi)
    dt_product_path = '/proc/device-tree/hat/product'
    dt_vendor_path = '/proc/device-tree/hat/vendor'
    dt_uuid_path = '/proc/device-tree/hat/uuid'

    if os.path.exists(dt_product_path):
        try:
            with open(dt_product_path, 'r') as f:
                result['product'] = f.read().strip().rstrip('\x00')
            result['detected'] = True
            result['source'] = 'device-tree'
        except (IOError, OSError) as e:
            logger.debug("Cannot read device-tree HAT product: %s", e)

        if os.path.exists(dt_vendor_path):
            try:
                with open(dt_vendor_path, 'r') as f:
                    result['vendor'] = f.read().strip().rstrip('\x00')
            except (IOError, OSError):
                pass

        if os.path.exists(dt_uuid_path):
            try:
                with open(dt_uuid_path, 'r') as f:
                    result['uuid'] = f.read().strip().rstrip('\x00')
            except (IOError, OSError):
                pass

    # Method 2: EEPROM direct read (fallback)
    if not result['detected']:
        try:
            from hat_eeprom import HATEeprom
            eeprom = HATEeprom(i2c_bus=0, address=0x50)
            info = eeprom.read()
            if info is not None and info.get('vendor') is not None:
                result['detected'] = True
                result['product'] = info.get('product')
                result['vendor'] = info.get('vendor')
                result['uuid'] = info.get('uuid')
                result['source'] = 'eeprom'
        except Exception as e:
            logger.debug("EEPROM probe failed: %s", e)

    return result


def discover_gpu():
    """Check for VideoCore VI GPU or other OpenCL-capable devices.

    Returns
    -------
    dict
        Keys: available (bool), device_name (str or None), driver (str or None),
        opencl (bool), dri_device (str or None).
    """
    result = {
        'available': False,
        'device_name': None,
        'driver': None,
        'opencl': False,
        'dri_device': None,
    }

    # Check for DRI device
    dri_paths = ['/dev/dri/card0', '/dev/dri/card1']
    for dri_path in dri_paths:
        if os.path.exists(dri_path):
            result['dri_device'] = dri_path
            result['available'] = True
            break

    # Try pyopencl enumeration
    try:
        import pyopencl as cl
        platforms = cl.get_platforms()
        for plat in platforms:
            devices = plat.get_devices()
            for dev in devices:
                result['opencl'] = True
                result['device_name'] = dev.name
                result['driver'] = plat.name
                result['available'] = True
                break
            if result['opencl']:
                break
    except ImportError:
        logger.debug("pyopencl not available for GPU enumeration")
    except Exception as e:
        logger.debug("OpenCL enumeration failed: %s", e)

    return result


def discover_pcie():
    """Scan PCIe bus for known accelerator devices.

    Looks for FPGA accelerators (Xilinx, Intel/Altera, Lattice) on the
    PCI bus via sysfs.

    Returns
    -------
    dict
        Keys: available (bool), devices (list of dicts with vendor_id,
        device_id, class_code, path).
    """
    result = {
        'available': False,
        'devices': [],
    }

    # Known accelerator vendor IDs
    known_vendors = {
        '10ee': 'Xilinx',
        '1172': 'Intel/Altera',
        '1204': 'Lattice',
        '1d0f': 'Amazon FPGA',
    }

    pci_base = '/sys/bus/pci/devices'
    if not os.path.isdir(pci_base):
        return result

    try:
        for dev_name in os.listdir(pci_base):
            dev_path = os.path.join(pci_base, dev_name)
            vendor_path = os.path.join(dev_path, 'vendor')
            device_path = os.path.join(dev_path, 'device')
            class_path = os.path.join(dev_path, 'class')

            if not os.path.exists(vendor_path):
                continue

            try:
                with open(vendor_path, 'r') as f:
                    vendor_id = f.read().strip().lower().replace('0x', '')
                with open(device_path, 'r') as f:
                    device_id = f.read().strip().lower().replace('0x', '')
                class_code = ''
                if os.path.exists(class_path):
                    with open(class_path, 'r') as f:
                        class_code = f.read().strip().lower().replace('0x', '')
            except (IOError, OSError):
                continue

            if vendor_id in known_vendors:
                result['available'] = True
                result['devices'].append({
                    'vendor_id': vendor_id,
                    'vendor_name': known_vendors[vendor_id],
                    'device_id': device_id,
                    'class_code': class_code,
                    'path': dev_path,
                })
    except OSError as e:
        logger.debug("PCIe scan error: %s", e)

    return result


def discover_usb3():
    """Check for USB3 accelerator devices.

    Returns
    -------
    dict
        Keys: available (bool), devices (list of dicts).
    """
    result = {
        'available': False,
        'devices': [],
    }

    # Known USB accelerator vendor:product pairs
    known_devices = {
        ('2100', '9e5d'): 'HeIMDALL USB3 Bridge',
        ('0403', '6014'): 'FTDI Hi-Speed',
        ('04b4', '00f1'): 'Cypress FX3',
    }

    # Try libusb enumeration
    try:
        import usb.core
        for known_vid_pid, name in known_devices.items():
            vid = int(known_vid_pid[0], 16)
            pid = int(known_vid_pid[1], 16)
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev is not None:
                result['available'] = True
                result['devices'].append({
                    'vendor_id': known_vid_pid[0],
                    'product_id': known_vid_pid[1],
                    'name': name,
                    'bus': dev.bus,
                    'address': dev.address,
                })
    except ImportError:
        logger.debug("pyusb not available for USB enumeration")
    except Exception as e:
        logger.debug("USB enumeration failed: %s", e)

    return result


def discover_cpu():
    """Get CPU capabilities.

    Returns
    -------
    dict
        Keys: arch (str), cores (int), neon (bool), model (str or None),
        freq_mhz (float or None).
    """
    result = {
        'arch': platform.machine(),
        'cores': os.cpu_count() or 1,
        'neon': False,
        'model': None,
        'freq_mhz': None,
    }

    cpuinfo_path = '/proc/cpuinfo'
    if os.path.exists(cpuinfo_path):
        try:
            with open(cpuinfo_path, 'r') as f:
                cpuinfo = f.read()

            # Check for NEON
            if 'neon' in cpuinfo.lower() or 'asimd' in cpuinfo.lower():
                result['neon'] = True

            # Model name
            model_match = re.search(r'model name\s*:\s*(.+)', cpuinfo)
            if model_match:
                result['model'] = model_match.group(1).strip()
            else:
                # ARM may use 'Hardware' field
                hw_match = re.search(r'Hardware\s*:\s*(.+)', cpuinfo)
                if hw_match:
                    result['model'] = hw_match.group(1).strip()

            # CPU frequency
            freq_match = re.search(r'cpu MHz\s*:\s*([\d.]+)', cpuinfo)
            if freq_match:
                result['freq_mhz'] = float(freq_match.group(1))
        except (IOError, OSError) as e:
            logger.debug("Cannot read /proc/cpuinfo: %s", e)

    # Fallback: check for NEON via architecture
    if not result['neon'] and result['arch'] in ('aarch64', 'armv7l'):
        result['neon'] = True

    return result


def discover_dma():
    """Check DMA engine availability.

    Returns
    -------
    dict
        Keys: available (bool), dma_heap (bool), dev_mem (bool),
        engines (list of str).
    """
    result = {
        'available': False,
        'dma_heap': False,
        'dev_mem': False,
        'engines': [],
    }

    # Check /dev/dma_heap/
    dma_heap_path = '/dev/dma_heap'
    if os.path.isdir(dma_heap_path):
        result['dma_heap'] = True
        result['available'] = True
        try:
            for entry in os.listdir(dma_heap_path):
                result['engines'].append(entry)
        except OSError:
            pass

    # Check /dev/mem access
    if os.path.exists('/dev/mem') and os.access('/dev/mem', os.R_OK):
        result['dev_mem'] = True
        result['available'] = True

    # Check for DMA controller in sysfs
    dma_sysfs = '/sys/class/dma'
    if os.path.isdir(dma_sysfs):
        try:
            channels = os.listdir(dma_sysfs)
            if channels:
                result['available'] = True
                result['engines'].extend(channels[:8])  # Limit output
        except OSError:
            pass

    return result


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------

def compute_recommendations(caps):
    """Select optimal transport and compute engine per pipeline stage.

    Priority order:
        PCIe FPGA > SPI FPGA > GPU > CPU NEON > CPU KFR

    Parameters
    ----------
    caps : dict
        Hardware capabilities from discover_hardware().

    Returns
    -------
    dict
        Recommended settings keyed by pipeline stage.
    """
    rec = {
        'fft_engine': 'cpu_scipy',
        'correlation_engine': 'cpu_numpy',
        'transport': 'shm',
        'workers': min(caps['cpu']['cores'], 4),
    }

    # PCIe FPGA is the best option
    if caps['pcie']['available']:
        rec['fft_engine'] = 'fpga'
        rec['correlation_engine'] = 'fpga'
        rec['transport'] = 'pcie'
        logger.info("Recommendation: PCIe FPGA accelerator detected")
        return rec

    # GPU available
    if caps['gpu']['available'] and caps['gpu']['opencl']:
        rec['fft_engine'] = 'gpu'
        rec['correlation_engine'] = 'gpu'
        logger.info("Recommendation: GPU compute offload via OpenCL")
        return rec

    # CPU with NEON (ARM)
    if caps['cpu']['neon']:
        rec['workers'] = caps['cpu']['cores']
        logger.info("Recommendation: CPU with NEON (ARM), workers=%d",
                     rec['workers'])
        return rec

    # Fallback: CPU with KFR/SciPy
    logger.info("Recommendation: CPU (scipy/KFR), workers=%d", rec['workers'])
    return rec


# ---------------------------------------------------------------------------
# Main discovery function
# ---------------------------------------------------------------------------

def discover_hardware():
    """Probe system for all available hardware accelerators.

    Returns
    -------
    dict
        Complete hardware capabilities including per-subsystem results
        and computed recommendations.
    """
    caps = {
        'hat': discover_hat(),
        'gpu': discover_gpu(),
        'pcie': discover_pcie(),
        'usb3': discover_usb3(),
        'cpu': discover_cpu(),
        'dma': discover_dma(),
        'recommended': {},
    }
    caps['recommended'] = compute_recommendations(caps)
    return caps


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    caps = discover_hardware()
    print(json.dumps(caps, indent=2, default=str))
