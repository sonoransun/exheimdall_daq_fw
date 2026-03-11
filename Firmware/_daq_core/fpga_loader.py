"""
   FPGA Bitstream Loader via SPI.

   Loads FPGA configuration bitstreams over SPI and verifies successful
   programming by checking the DONE signal and reading the ID register.

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
import struct
import time

try:
    import spidev
    _HAS_SPIDEV = True
except ImportError:
    _HAS_SPIDEV = False

try:
    import RPi.GPIO as GPIO
    _HAS_GPIO = True
except ImportError:
    _HAS_GPIO = False

logger = logging.getLogger(__name__)

# FPGA ID register constants
_FPGA_ID_MAGIC = 0x48454D44  # 'HEMD'
_SPI_ID_CMD = 0x9F  # Standard JEDEC Read ID command (reused for FPGA ID)
_SPI_MAX_SPEED_HZ = 16000000  # 16 MHz for bitstream loading
_SPI_CHUNK_SIZE = 4096  # Bytes per SPI transfer during loading


class FPGALoader:
    """Load FPGA bitstream via SPI and verify.

    Parameters
    ----------
    spi_bus : int
        SPI bus number (default 0).
    spi_cs : int
        SPI chip select (default 0).
    gpio_reset : int
        GPIO pin for FPGA RESET (active low, BCM numbering, default 26).
    gpio_done : int
        GPIO pin for FPGA DONE signal (active high, BCM numbering, default 25).
    """

    def __init__(self, spi_bus=0, spi_cs=0, gpio_reset=26, gpio_done=25):
        self.logger = logging.getLogger(__name__)
        self.spi_bus = spi_bus
        self.spi_cs = spi_cs
        self.gpio_reset = gpio_reset
        self.gpio_done = gpio_done
        self._spi = None
        self._gpio_initialized = False

        if not _HAS_SPIDEV:
            self.logger.error("spidev not available -- FPGA loader disabled")
            return

        if not _HAS_GPIO:
            self.logger.error("RPi.GPIO not available -- FPGA loader disabled")
            return

        try:
            self._spi = spidev.SpiDev()
            self._spi.open(spi_bus, spi_cs)
            self._spi.max_speed_hz = _SPI_MAX_SPEED_HZ
            self._spi.mode = 0b00
            self._spi.bits_per_word = 8
            self.logger.info("SPI opened: bus=%d, cs=%d, speed=%d Hz",
                             spi_bus, spi_cs, _SPI_MAX_SPEED_HZ)
        except (IOError, OSError) as e:
            self.logger.error("Failed to open SPI: %s", e)
            self._spi = None
            return

        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(gpio_reset, GPIO.OUT, initial=GPIO.HIGH)
            GPIO.setup(gpio_done, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            self._gpio_initialized = True
            self.logger.info("GPIO initialized: RESET=%d, DONE=%d",
                             gpio_reset, gpio_done)
        except Exception as e:
            self.logger.error("Failed to initialize GPIO: %s", e)

    def is_ready(self):
        """Return True if the loader has SPI and GPIO available."""
        return self._spi is not None and self._gpio_initialized

    def load_bitstream(self, bitstream_path):
        """Load bitstream file to FPGA.

        Steps:
            1. Assert RESET (low)
            2. Wait for FPGA to clear
            3. Release RESET (high)
            4. Send bitstream via SPI in chunks
            5. Wait for DONE signal
            6. Read ID register to verify

        Parameters
        ----------
        bitstream_path : str
            Path to the FPGA bitstream file (.bin or .bit).

        Returns
        -------
        dict
            Result with keys: success (bool), id_info (dict or None),
            elapsed_ms (float), size_bytes (int).
        """
        if not self.is_ready():
            return {'success': False, 'id_info': None, 'elapsed_ms': 0,
                    'size_bytes': 0, 'error': 'Loader not ready'}

        if not os.path.isfile(bitstream_path):
            self.logger.error("Bitstream file not found: %s", bitstream_path)
            return {'success': False, 'id_info': None, 'elapsed_ms': 0,
                    'size_bytes': 0, 'error': 'File not found'}

        file_size = os.path.getsize(bitstream_path)
        self.logger.info("Loading bitstream: %s (%d bytes)", bitstream_path, file_size)

        start_time = time.monotonic()

        # Step 1: Assert RESET
        GPIO.output(self.gpio_reset, GPIO.LOW)
        time.sleep(0.001)  # 1 ms

        # Step 2: Release RESET
        GPIO.output(self.gpio_reset, GPIO.HIGH)
        time.sleep(0.010)  # 10 ms for FPGA init

        # Step 3: Send bitstream via SPI
        bytes_sent = 0
        try:
            with open(bitstream_path, 'rb') as f:
                while True:
                    chunk = f.read(_SPI_CHUNK_SIZE)
                    if not chunk:
                        break
                    self._spi.writebytes2(list(chunk))
                    bytes_sent += len(chunk)
        except (IOError, OSError) as e:
            self.logger.error("Error sending bitstream: %s", e)
            return {'success': False, 'id_info': None,
                    'elapsed_ms': (time.monotonic() - start_time) * 1000,
                    'size_bytes': bytes_sent, 'error': str(e)}

        # Step 4: Send extra clocks (dummy bytes) for FPGA startup
        self._spi.writebytes2([0x00] * 64)

        # Step 5: Wait for DONE signal
        done = False
        for _ in range(100):  # Up to 1 second
            if GPIO.input(self.gpio_done) == GPIO.HIGH:
                done = True
                break
            time.sleep(0.010)

        elapsed_ms = (time.monotonic() - start_time) * 1000

        if not done:
            self.logger.error("FPGA DONE signal not asserted after loading")
            return {'success': False, 'id_info': None,
                    'elapsed_ms': elapsed_ms, 'size_bytes': bytes_sent,
                    'error': 'DONE timeout'}

        # Step 6: Verify via ID register
        id_info = self.read_id()
        success = id_info is not None and id_info.get('magic') == _FPGA_ID_MAGIC

        if success:
            self.logger.info("Bitstream loaded successfully in %.1f ms "
                             "(%d bytes, version=%d)",
                             elapsed_ms, bytes_sent,
                             id_info.get('version', 0))
        else:
            self.logger.warning("Bitstream loaded but ID verification failed")

        return {'success': success, 'id_info': id_info,
                'elapsed_ms': elapsed_ms, 'size_bytes': bytes_sent}

    def read_id(self):
        """Read FPGA ID register via SPI.

        The ID register returns 12 bytes:
            Magic:        4 bytes (0x48454D44 = 'HEMD')
            Capabilities: 4 bytes (bitmask)
            Version:      4 bytes (major.minor.patch encoded)

        Returns
        -------
        dict or None
            ID information with keys: magic, caps, version, version_str.
            None if SPI is not available.
        """
        if self._spi is None:
            return None

        try:
            # Send ID read command + 12 dummy bytes
            tx = [_SPI_ID_CMD] + [0x00] * 12
            rx = self._spi.xfer2(tx)

            # Response starts at byte 1 (after command echo)
            if len(rx) < 13:
                self.logger.warning("ID read returned only %d bytes", len(rx))
                return None

            raw = bytes(rx[1:13])
            magic = struct.unpack('>I', raw[0:4])[0]
            caps = struct.unpack('>I', raw[4:8])[0]
            version_raw = struct.unpack('>I', raw[8:12])[0]

            version_major = (version_raw >> 16) & 0xFF
            version_minor = (version_raw >> 8) & 0xFF
            version_patch = version_raw & 0xFF

            return {
                'magic': magic,
                'caps': caps,
                'version': version_raw,
                'version_str': '{}.{}.{}'.format(version_major, version_minor,
                                                  version_patch),
            }
        except (IOError, OSError) as e:
            self.logger.error("Failed to read FPGA ID: %s", e)
            return None

    def verify(self):
        """Verify FPGA is configured and responsive.

        Returns
        -------
        bool
            True if FPGA is programmed and ID magic matches.
        """
        if not self.is_ready():
            return False

        # Check DONE pin
        if not GPIO.input(self.gpio_done):
            self.logger.warning("FPGA DONE signal not asserted")
            return False

        # Check ID register
        id_info = self.read_id()
        if id_info is None:
            self.logger.warning("Cannot read FPGA ID register")
            return False

        if id_info['magic'] != _FPGA_ID_MAGIC:
            self.logger.warning("FPGA ID magic mismatch: expected 0x%08x, got 0x%08x",
                                _FPGA_ID_MAGIC, id_info['magic'])
            return False

        self.logger.info("FPGA verified: version=%s, caps=0x%08x",
                         id_info['version_str'], id_info['caps'])
        return True

    def close(self):
        """Release SPI and GPIO resources."""
        if self._spi is not None:
            self._spi.close()
            self._spi = None
            self.logger.info("SPI closed")

        if self._gpio_initialized:
            try:
                GPIO.cleanup([self.gpio_reset, self.gpio_done])
            except Exception:
                pass
            self._gpio_initialized = False
            self.logger.info("GPIO cleaned up")
