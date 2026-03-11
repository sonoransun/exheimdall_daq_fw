"""
   Raspberry Pi HAT EEPROM Reader.

   Reads the standard Raspberry Pi HAT EEPROM format per the HAT
   specification (signature 'R-Pi', atom-based vendor/GPIO/custom data).

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
import struct
import uuid

try:
    from smbus2 import SMBus
    _HAS_SMBUS2 = True
except ImportError:
    _HAS_SMBUS2 = False

logger = logging.getLogger(__name__)

# HAT EEPROM constants
_HAT_SIGNATURE = b'R-Pi'
_HEADER_SIZE = 12  # signature(4) + version(1) + reserved(1) + num_atoms(2) + eeprom_len(4)

# Atom types
_ATOM_VENDOR = 0x0001
_ATOM_GPIO   = 0x0002
_ATOM_LINUX  = 0x0003
_ATOM_CUSTOM = 0x0004


class HATEeprom:
    """Reads standard Raspberry Pi HAT EEPROM.

    Parameters
    ----------
    i2c_bus : int
        I2C bus number (default 0, typically /dev/i2c-0 for HAT EEPROM).
    address : int
        I2C address of the EEPROM (default 0x50).
    """

    def __init__(self, i2c_bus=0, address=0x50):
        self.logger = logging.getLogger(__name__)
        self.i2c_bus = i2c_bus
        self.address = address

    def read(self):
        """Read HAT EEPROM and return parsed contents.

        Returns
        -------
        dict or None
            Dictionary with keys: vendor, product, uuid, version,
            num_atoms, capabilities.  Returns None if no HAT detected
            or I2C is not available.
        """
        if not _HAS_SMBUS2:
            self.logger.warning("smbus2 not available -- cannot read HAT EEPROM")
            return None

        try:
            raw = self._read_raw_eeprom()
        except Exception as e:
            self.logger.warning("Failed to read HAT EEPROM: %s", e)
            return None

        if raw is None or len(raw) < _HEADER_SIZE:
            return None

        return self._parse_header(raw)

    def _read_raw_eeprom(self, max_size=512):
        """Read raw bytes from EEPROM via I2C.

        Parameters
        ----------
        max_size : int
            Maximum number of bytes to read.

        Returns
        -------
        bytes or None
        """
        try:
            bus = SMBus(self.i2c_bus)
        except (FileNotFoundError, OSError) as e:
            self.logger.warning("Cannot open I2C bus %d: %s", self.i2c_bus, e)
            return None

        try:
            # Set EEPROM address pointer to 0
            bus.write_byte(self.address, 0x00)

            # Read in 32-byte blocks
            data = bytearray()
            for offset in range(0, max_size, 32):
                block_size = min(32, max_size - offset)
                try:
                    block = bus.read_i2c_block_data(self.address, offset, block_size)
                    data.extend(block)
                except OSError:
                    break
            return bytes(data)
        except OSError as e:
            self.logger.debug("EEPROM read error at address 0x%02x: %s",
                              self.address, e)
            return None
        finally:
            bus.close()

    def _parse_header(self, data):
        """Parse standard RPi HAT EEPROM format.

        EEPROM Header (12 bytes):
            Signature:  4 bytes  0x52 0x2D 0x50 0x69 ('R-Pi')
            Version:    1 byte
            Reserved:   1 byte
            Num atoms:  2 bytes (little-endian)
            EEPROM len: 4 bytes (little-endian)

        Followed by atom entries:
            Type:   2 bytes (little-endian)
            Count:  2 bytes (little-endian)
            Length: 4 bytes (little-endian) -- includes type+count+length+data
            Data:   (length - 8) bytes

        Parameters
        ----------
        data : bytes
            Raw EEPROM contents.

        Returns
        -------
        dict or None
        """
        # Check signature
        if data[:4] != _HAT_SIGNATURE:
            self.logger.debug("No HAT signature found (got %s)", data[:4].hex())
            return None

        version = data[4]
        num_atoms = struct.unpack_from('<H', data, 6)[0]
        eeprom_len = struct.unpack_from('<I', data, 8)[0]

        self.logger.info("HAT EEPROM: version=%d, num_atoms=%d, eeprom_len=%d",
                         version, num_atoms, eeprom_len)

        result = {
            'version': version,
            'num_atoms': num_atoms,
            'eeprom_len': eeprom_len,
            'vendor': None,
            'product': None,
            'uuid': None,
            'capabilities': {},
        }

        # Parse atoms
        offset = _HEADER_SIZE
        for _ in range(num_atoms):
            if offset + 8 > len(data):
                break

            atom_type = struct.unpack_from('<H', data, offset)[0]
            atom_count = struct.unpack_from('<H', data, offset + 2)[0]
            atom_length = struct.unpack_from('<I', data, offset + 4)[0]

            if atom_length < 8 or offset + atom_length > len(data):
                self.logger.warning("Invalid atom at offset %d", offset)
                break

            atom_data = data[offset + 8 : offset + atom_length]

            if atom_type == _ATOM_VENDOR:
                result.update(self._parse_vendor_atom(atom_data))
            elif atom_type == _ATOM_GPIO:
                result['capabilities']['gpio_map'] = True
            elif atom_type == _ATOM_LINUX:
                result['capabilities']['device_tree'] = True
            elif atom_type == _ATOM_CUSTOM:
                result['capabilities']['custom_data'] = True

            offset += atom_length

        return result

    def _parse_vendor_atom(self, data):
        """Parse vendor info atom.

        Vendor atom data layout:
            UUID:       16 bytes
            PID:        2 bytes (little-endian)
            pver:       2 bytes (little-endian)
            vslen:      1 byte  (vendor string length)
            pslen:      1 byte  (product string length)
            vstr:       vslen bytes
            pstr:       pslen bytes

        Returns
        -------
        dict
        """
        result = {}
        if len(data) < 22:
            return result

        try:
            raw_uuid = data[:16]
            result['uuid'] = str(uuid.UUID(bytes=raw_uuid))
        except (ValueError, IndexError):
            result['uuid'] = None

        pid = struct.unpack_from('<H', data, 16)[0]
        pver = struct.unpack_from('<H', data, 18)[0]
        vslen = data[20]
        pslen = data[21]

        vstr_start = 22
        pstr_start = vstr_start + vslen

        if vstr_start + vslen <= len(data):
            result['vendor'] = data[vstr_start:vstr_start + vslen].decode('ascii', errors='replace').rstrip('\x00')

        if pstr_start + pslen <= len(data):
            result['product'] = data[pstr_start:pstr_start + pslen].decode('ascii', errors='replace').rstrip('\x00')

        result['capabilities'] = result.get('capabilities', {})
        result['capabilities']['pid'] = pid
        result['capabilities']['product_version'] = pver

        return result
