"""
   USB Gadget Mode Configuration for Raspberry Pi 4.

   Configures the dwc2 USB controller in gadget mode for streaming
   IQ data over USB bulk transfers using ConfigFS.

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
import logging
import os
import shutil

logger = logging.getLogger(__name__)


class USBGadget:
    """Configure Pi4 USB gadget mode for IQ data streaming.

    Uses Linux ConfigFS to create a USB gadget with a bulk transfer
    function (FunctionFS) for high-speed IQ data streaming.

    Parameters
    ----------
    name : str
        Gadget name (default 'heimdall_iq').
    vid : int
        USB Vendor ID (default 0x1d6b, Linux Foundation).
    pid : int
        USB Product ID (default 0x0104, Multifunction Composite Gadget).
    """

    CONFIGFS_PATH = '/sys/kernel/config/usb_gadget'

    def __init__(self, name='heimdall_iq', vid=0x1d6b, pid=0x0104):
        self.logger = logging.getLogger(__name__)
        self.name = name
        self.vid = vid
        self.pid = pid
        self._gadget_path = os.path.join(self.CONFIGFS_PATH, name)
        self._enabled = False

        # Verify prerequisites
        self._configfs_available = os.path.isdir(self.CONFIGFS_PATH)
        if not self._configfs_available:
            self.logger.warning("ConfigFS not mounted at %s", self.CONFIGFS_PATH)

        self._dwc2_loaded = self._check_dwc2()

    def _check_dwc2(self):
        """Check if dwc2 overlay/module is loaded.

        Returns
        -------
        bool
        """
        # Check /sys/bus/platform/drivers/dwc2
        if os.path.isdir('/sys/bus/platform/drivers/dwc2'):
            return True

        # Check loaded modules
        try:
            with open('/proc/modules', 'r') as f:
                for line in f:
                    if line.startswith('dwc2 ') or line.startswith('dwc2\t'):
                        return True
        except (IOError, OSError):
            pass

        self.logger.warning("dwc2 module/overlay not loaded -- USB gadget unavailable")
        return False

    def is_available(self):
        """Return True if USB gadget mode can be configured."""
        return self._configfs_available and self._dwc2_loaded

    def setup(self):
        """Create gadget configuration in ConfigFS.

        Creates the directory structure, sets VID/PID and string
        descriptors, and adds a FunctionFS bulk function.

        Returns
        -------
        bool
            True on success, False on failure.
        """
        if not self.is_available():
            self.logger.error("USB gadget prerequisites not met")
            return False

        try:
            # Create gadget directory
            os.makedirs(self._gadget_path, exist_ok=True)

            # Set VID/PID
            self._write_attr('idVendor', '0x{:04x}'.format(self.vid))
            self._write_attr('idProduct', '0x{:04x}'.format(self.pid))
            self._write_attr('bcdDevice', '0x0100')
            self._write_attr('bcdUSB', '0x0200')

            # Device class: vendor-specific
            self._write_attr('bDeviceClass', '0xff')
            self._write_attr('bDeviceSubClass', '0x00')
            self._write_attr('bDeviceProtocol', '0x00')

            # String descriptors (English)
            strings_path = os.path.join(self._gadget_path, 'strings', '0x409')
            os.makedirs(strings_path, exist_ok=True)
            self._write_file(os.path.join(strings_path, 'serialnumber'), 'heimdall001')
            self._write_file(os.path.join(strings_path, 'manufacturer'), 'HeIMDALL DAQ')
            self._write_file(os.path.join(strings_path, 'product'), 'IQ Data Stream')

            # Configuration
            config_path = os.path.join(self._gadget_path, 'configs', 'c.1')
            os.makedirs(config_path, exist_ok=True)
            self._write_file(os.path.join(config_path, 'MaxPower'), '500')

            config_strings = os.path.join(config_path, 'strings', '0x409')
            os.makedirs(config_strings, exist_ok=True)
            self._write_file(os.path.join(config_strings, 'configuration'),
                             'IQ Bulk Transfer')

            # FunctionFS function
            func_path = os.path.join(self._gadget_path, 'functions', 'ffs.iq0')
            os.makedirs(func_path, exist_ok=True)

            # Link function to configuration
            link_path = os.path.join(config_path, 'ffs.iq0')
            if not os.path.exists(link_path):
                os.symlink(func_path, link_path)

            self.logger.info("USB gadget '%s' configured (VID=0x%04x, PID=0x%04x)",
                             self.name, self.vid, self.pid)
            return True

        except OSError as e:
            self.logger.error("Failed to setup USB gadget: %s", e)
            return False

    def enable(self, udc=None):
        """Bind gadget to UDC and enable it.

        Parameters
        ----------
        udc : str or None
            UDC name (e.g. 'fe980000.usb'). If None, auto-detect.

        Returns
        -------
        bool
            True on success.
        """
        if self._enabled:
            self.logger.warning("USB gadget already enabled")
            return True

        if udc is None:
            udc = self._find_udc()
            if udc is None:
                self.logger.error("No UDC found")
                return False

        try:
            self._write_attr('UDC', udc)
            self._enabled = True
            self.logger.info("USB gadget enabled on UDC: %s", udc)
            return True
        except OSError as e:
            self.logger.error("Failed to enable USB gadget: %s", e)
            return False

    def disable(self):
        """Unbind gadget from UDC and remove configuration.

        Returns
        -------
        bool
            True on success.
        """
        if not self._enabled and not os.path.isdir(self._gadget_path):
            return True

        try:
            # Unbind from UDC
            udc_path = os.path.join(self._gadget_path, 'UDC')
            if os.path.exists(udc_path):
                self._write_attr('UDC', '')

            # Remove symlinks in configs
            config_path = os.path.join(self._gadget_path, 'configs', 'c.1')
            if os.path.isdir(config_path):
                for entry in os.listdir(config_path):
                    entry_path = os.path.join(config_path, entry)
                    if os.path.islink(entry_path):
                        os.unlink(entry_path)

                # Remove config strings
                config_strings = os.path.join(config_path, 'strings', '0x409')
                if os.path.isdir(config_strings):
                    os.rmdir(config_strings)
                strings_dir = os.path.join(config_path, 'strings')
                if os.path.isdir(strings_dir):
                    os.rmdir(strings_dir)
                os.rmdir(config_path)

            # Remove configs directory
            configs_dir = os.path.join(self._gadget_path, 'configs')
            if os.path.isdir(configs_dir):
                try:
                    os.rmdir(configs_dir)
                except OSError:
                    pass

            # Remove functions
            func_path = os.path.join(self._gadget_path, 'functions', 'ffs.iq0')
            if os.path.isdir(func_path):
                os.rmdir(func_path)
            funcs_dir = os.path.join(self._gadget_path, 'functions')
            if os.path.isdir(funcs_dir):
                try:
                    os.rmdir(funcs_dir)
                except OSError:
                    pass

            # Remove gadget strings
            strings_path = os.path.join(self._gadget_path, 'strings', '0x409')
            if os.path.isdir(strings_path):
                os.rmdir(strings_path)
            strings_dir = os.path.join(self._gadget_path, 'strings')
            if os.path.isdir(strings_dir):
                try:
                    os.rmdir(strings_dir)
                except OSError:
                    pass

            # Remove gadget directory
            if os.path.isdir(self._gadget_path):
                os.rmdir(self._gadget_path)

            self._enabled = False
            self.logger.info("USB gadget '%s' disabled and removed", self.name)
            return True

        except OSError as e:
            self.logger.error("Failed to disable USB gadget: %s", e)
            return False

    def get_endpoint(self):
        """Return FunctionFS endpoint path for data streaming.

        The FunctionFS mount point must be mounted separately before use:
            mkdir -p /dev/ffs-iq0
            mount -t functionfs iq0 /dev/ffs-iq0

        Returns
        -------
        str or None
            Path to the FunctionFS mount point, or None if not available.
        """
        ffs_mount = '/dev/ffs-iq0'
        if os.path.isdir(ffs_mount):
            return ffs_mount

        self.logger.debug("FunctionFS not mounted at %s", ffs_mount)
        return None

    # -- internal helpers ---------------------------------------------------

    def _write_attr(self, name, value):
        """Write a value to a gadget attribute file."""
        path = os.path.join(self._gadget_path, name)
        self._write_file(path, value)

    @staticmethod
    def _write_file(path, value):
        """Write a string value to a file."""
        with open(path, 'w') as f:
            f.write(str(value))

    @staticmethod
    def _find_udc():
        """Auto-detect available UDC.

        Returns
        -------
        str or None
        """
        udc_path = '/sys/class/udc'
        if not os.path.isdir(udc_path):
            return None
        try:
            entries = os.listdir(udc_path)
            if entries:
                return entries[0]
        except OSError:
            pass
        return None
