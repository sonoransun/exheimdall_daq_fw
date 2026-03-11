"""
   Extended I2C Controller for HAT Peripherals.

   Provides retry logic, bus scanning, batch transfers, and optional
   high-speed mode configuration for Raspberry Pi HAT communication.

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
import time

try:
    from smbus2 import SMBus, i2c_msg
    _HAS_SMBUS2 = True
except ImportError:
    _HAS_SMBUS2 = False

logger = logging.getLogger(__name__)


class HATI2CController:
    """Extended I2C controller for HAT peripherals.

    Parameters
    ----------
    bus : int
        I2C bus number (default 1 for Raspberry Pi).
    speed : int
        Bus clock speed in Hz (default 400000 for Fast-mode).
        Note: actual speed change requires kernel/device-tree support.
    retry_count : int
        Number of retries on I/O errors (default 3).
    retry_delay_ms : int
        Delay between retries in milliseconds (default 10).
    """

    def __init__(self, bus=1, speed=400000, retry_count=3, retry_delay_ms=10):
        self.logger = logging.getLogger(__name__)
        self.bus_num = bus
        self.speed = speed
        self.retry_count = retry_count
        self.retry_delay = retry_delay_ms / 1000.0
        self._bus = None

        if not _HAS_SMBUS2:
            self.logger.error("smbus2 not available -- I2C controller disabled")
            return

        try:
            self._bus = SMBus(bus)
            self.logger.info("I2C bus %d opened (requested speed: %d Hz)",
                             bus, speed)
        except (FileNotFoundError, OSError) as e:
            self.logger.error("Failed to open I2C bus %d: %s", bus, e)

    def is_open(self):
        """Return True if the I2C bus is open and ready."""
        return self._bus is not None

    def close(self):
        """Close the I2C bus."""
        if self._bus is not None:
            self._bus.close()
            self._bus = None
            self.logger.info("I2C bus %d closed", self.bus_num)

    def scan(self):
        """Scan bus and return list of detected I2C addresses.

        Returns
        -------
        list of int
            Detected device addresses (7-bit).
        """
        if self._bus is None:
            self.logger.error("I2C bus not open")
            return []

        detected = []
        for addr in range(0x03, 0x78):
            try:
                self._bus.read_byte(addr)
                detected.append(addr)
            except OSError:
                pass

        self.logger.info("I2C scan: found %d device(s) at %s",
                         len(detected),
                         ['0x{:02x}'.format(a) for a in detected])
        return detected

    def read_register(self, address, register, length=1):
        """Read register(s) with retry logic.

        Parameters
        ----------
        address : int
            I2C device address (7-bit).
        register : int
            Register address to read from.
        length : int
            Number of bytes to read (default 1).

        Returns
        -------
        list of int or None
            Read data bytes, or None on failure.
        """
        if self._bus is None:
            self.logger.error("I2C bus not open")
            return None

        for attempt in range(self.retry_count):
            try:
                if length == 1:
                    data = [self._bus.read_byte_data(address, register)]
                else:
                    data = self._bus.read_i2c_block_data(address, register, length)
                return data
            except OSError as e:
                self.logger.warning("I2C read error (addr=0x%02x, reg=0x%02x, "
                                    "attempt %d/%d): %s",
                                    address, register, attempt + 1,
                                    self.retry_count, e)
                if attempt < self.retry_count - 1:
                    time.sleep(self.retry_delay)

        self.logger.error("I2C read failed after %d retries (addr=0x%02x, reg=0x%02x)",
                          self.retry_count, address, register)
        return None

    def write_register(self, address, register, data):
        """Write register(s) with retry logic.

        Parameters
        ----------
        address : int
            I2C device address (7-bit).
        register : int
            Register address to write to.
        data : int or list of int
            Byte(s) to write.

        Returns
        -------
        bool
            True on success, False on failure.
        """
        if self._bus is None:
            self.logger.error("I2C bus not open")
            return False

        for attempt in range(self.retry_count):
            try:
                if isinstance(data, int):
                    self._bus.write_byte_data(address, register, data)
                else:
                    self._bus.write_i2c_block_data(address, register, list(data))
                return True
            except OSError as e:
                self.logger.warning("I2C write error (addr=0x%02x, reg=0x%02x, "
                                    "attempt %d/%d): %s",
                                    address, register, attempt + 1,
                                    self.retry_count, e)
                if attempt < self.retry_count - 1:
                    time.sleep(self.retry_delay)

        self.logger.error("I2C write failed after %d retries (addr=0x%02x, reg=0x%02x)",
                          self.retry_count, address, register)
        return False

    def batch_transfer(self, address, messages):
        """Batch multiple I2C messages using i2c_rdwr for efficiency.

        Parameters
        ----------
        address : int
            I2C device address (7-bit).
        messages : list of dict
            Each dict has:
              'type': 'read' or 'write'
              'register': int (register address)
              'length': int (for reads)
              'data': list of int (for writes)

        Returns
        -------
        list
            Results for each message. Read messages return list of int,
            write messages return True/False.
        """
        if self._bus is None:
            self.logger.error("I2C bus not open")
            return [None] * len(messages)

        if not _HAS_SMBUS2:
            self.logger.error("smbus2 i2c_msg not available for batch transfer")
            return [None] * len(messages)

        results = []
        i2c_msgs = []
        result_map = []  # Maps i2c_msgs index to results index for reads

        for idx, msg in enumerate(messages):
            msg_type = msg.get('type', 'read')
            register = msg.get('register', 0)

            if msg_type == 'write':
                payload = [register] + list(msg.get('data', []))
                write_msg = i2c_msg.write(address, payload)
                i2c_msgs.append(write_msg)
                results.append(True)
            else:
                length = msg.get('length', 1)
                # Write register address, then read data
                write_msg = i2c_msg.write(address, [register])
                read_msg = i2c_msg.read(address, length)
                i2c_msgs.append(write_msg)
                i2c_msgs.append(read_msg)
                results.append(None)
                result_map.append((len(i2c_msgs) - 1, idx))

        for attempt in range(self.retry_count):
            try:
                self._bus.i2c_rdwr(*i2c_msgs)

                # Extract read results
                for msg_idx, result_idx in result_map:
                    results[result_idx] = list(i2c_msgs[msg_idx])

                return results
            except OSError as e:
                self.logger.warning("I2C batch transfer error (addr=0x%02x, "
                                    "attempt %d/%d): %s",
                                    address, attempt + 1, self.retry_count, e)
                if attempt < self.retry_count - 1:
                    time.sleep(self.retry_delay)

        self.logger.error("I2C batch transfer failed after %d retries (addr=0x%02x)",
                          self.retry_count, address)
        return [None] * len(messages)
