"""
   UART Transport with COBS Framing for HAT Control Channel.

   Provides COBS (Consistent Overhead Byte Stuffing) encoding/decoding
   and a framed UART transport for HAT control messages.

   Frame format:  [0x00] [COBS( type + seq + len_hi + len_lo + payload + crc16 )] [0x00]

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
import time

try:
    import serial
    _HAS_SERIAL = True
except ImportError:
    _HAS_SERIAL = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# COBS Codec
# ---------------------------------------------------------------------------

class COBSCodec:
    """COBS (Consistent Overhead Byte Stuffing) encoder/decoder.

    Encodes arbitrary byte sequences so that 0x00 never appears in the
    encoded output, allowing 0x00 to serve as an unambiguous frame delimiter.
    """

    @staticmethod
    def encode(data):
        """Encode data using COBS.

        Parameters
        ----------
        data : bytes or bytearray
            Input data (may contain 0x00 bytes).

        Returns
        -------
        bytes
            COBS-encoded data (no 0x00 bytes present).
        """
        output = bytearray()
        code_index = 0
        output.append(0)  # Placeholder for first code byte
        code = 1

        for byte in data:
            if byte == 0x00:
                output[code_index] = code
                code_index = len(output)
                output.append(0)  # Placeholder for next code byte
                code = 1
            else:
                output.append(byte)
                code += 1
                if code == 0xFF:
                    output[code_index] = code
                    code_index = len(output)
                    output.append(0)  # Placeholder
                    code = 1

        output[code_index] = code
        return bytes(output)

    @staticmethod
    def decode(data):
        """Decode COBS-encoded data.

        Parameters
        ----------
        data : bytes or bytearray
            COBS-encoded data (must not contain 0x00).

        Returns
        -------
        bytes
            Decoded data.

        Raises
        ------
        ValueError
            If the encoded data is malformed.
        """
        if not data:
            return b''

        output = bytearray()
        idx = 0

        while idx < len(data):
            code = data[idx]
            if code == 0:
                raise ValueError("Unexpected zero byte in COBS data at index {}".format(idx))
            idx += 1

            for _ in range(code - 1):
                if idx >= len(data):
                    raise ValueError("COBS decode: unexpected end of data")
                output.append(data[idx])
                idx += 1

            if code < 0xFF and idx < len(data):
                output.append(0x00)

        # Remove trailing zero if present (artifact of encoding)
        if output and output[-1] == 0x00:
            output = output[:-1]

        return bytes(output)


# ---------------------------------------------------------------------------
# CRC-16 (CCITT / X.25)
# ---------------------------------------------------------------------------

def _crc16(data):
    """Compute CRC-16/CCITT-FALSE.

    Parameters
    ----------
    data : bytes or bytearray

    Returns
    -------
    int
        16-bit CRC value.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
            crc &= 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# UART Transport
# ---------------------------------------------------------------------------

class UARTTransport:
    """UART control transport with COBS framing.

    Message types:
        MSG_CTRL_REQ  (0x01) -- control request from host
        MSG_CTRL_RSP  (0x02) -- control response from HAT
        MSG_STATUS    (0x03) -- status report
        MSG_HEARTBEAT (0xFF) -- keepalive heartbeat

    Parameters
    ----------
    device : str
        Serial device path (default '/dev/ttyAMA1').
    baud : int
        Baud rate (default 3000000 for high-speed HAT link).
    """

    MSG_CTRL_REQ  = 0x01
    MSG_CTRL_RSP  = 0x02
    MSG_STATUS    = 0x03
    MSG_HEARTBEAT = 0xFF

    def __init__(self, device='/dev/ttyAMA1', baud=3000000):
        self.logger = logging.getLogger(__name__)
        self.device = device
        self.baud = baud
        self._port = None
        self._seq = 0

        if not _HAS_SERIAL:
            self.logger.error("pyserial not available -- UART transport disabled")
            return

        try:
            self._port = serial.Serial(
                port=device,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            self.logger.info("UART transport opened: %s @ %d baud", device, baud)
        except (serial.SerialException, OSError) as e:
            self.logger.error("Failed to open serial port %s: %s", device, e)

    def is_open(self):
        """Return True if the serial port is open."""
        return self._port is not None and self._port.is_open

    def send(self, msg_type, payload):
        """Send a framed message.

        Frame structure:
            [0x00] [COBS(type + seq + len_hi + len_lo + payload + crc16_hi + crc16_lo)] [0x00]

        Parameters
        ----------
        msg_type : int
            Message type byte (e.g. MSG_CTRL_REQ).
        payload : bytes or bytearray
            Message payload.

        Returns
        -------
        bool
            True on success, False on failure.
        """
        if not self.is_open():
            self.logger.error("UART port not open")
            return False

        seq = self._seq & 0xFF
        self._seq = (self._seq + 1) & 0xFF

        payload_len = len(payload)
        # Build raw frame: type(1) + seq(1) + length(2) + payload(N)
        raw = struct.pack('>BBH', msg_type, seq, payload_len) + bytes(payload)

        # Append CRC-16 over the raw frame
        crc = _crc16(raw)
        raw += struct.pack('>H', crc)

        # COBS encode and frame with delimiters
        encoded = COBSCodec.encode(raw)
        frame = b'\x00' + encoded + b'\x00'

        try:
            self._port.write(frame)
            self._port.flush()
            self.logger.debug("TX: type=0x%02x seq=%d len=%d crc=0x%04x",
                              msg_type, seq, payload_len, crc)
            return True
        except (serial.SerialException, OSError) as e:
            self.logger.error("UART write error: %s", e)
            return False

    def receive(self, timeout=1.0):
        """Receive and decode a framed message.

        Parameters
        ----------
        timeout : float
            Receive timeout in seconds.

        Returns
        -------
        tuple or None
            (msg_type, seq, payload) on success, None on timeout or error.
        """
        if not self.is_open():
            self.logger.error("UART port not open")
            return None

        old_timeout = self._port.timeout
        self._port.timeout = timeout

        try:
            # Read until we get a frame delimiter
            buf = bytearray()
            frame_started = False
            deadline = time.monotonic() + timeout

            while time.monotonic() < deadline:
                byte = self._port.read(1)
                if not byte:
                    continue

                b = byte[0]

                if b == 0x00:
                    if frame_started and len(buf) > 0:
                        # End of frame
                        break
                    else:
                        # Start of frame
                        frame_started = True
                        buf = bytearray()
                elif frame_started:
                    buf.append(b)
            else:
                # Timeout
                return None

            if len(buf) == 0:
                return None

            # COBS decode
            try:
                raw = COBSCodec.decode(bytes(buf))
            except ValueError as e:
                self.logger.warning("COBS decode error: %s", e)
                return None

            # Minimum raw size: type(1) + seq(1) + length(2) + crc(2) = 6
            if len(raw) < 6:
                self.logger.warning("Frame too short: %d bytes", len(raw))
                return None

            # Verify CRC
            received_crc = struct.unpack('>H', raw[-2:])[0]
            computed_crc = _crc16(raw[:-2])
            if received_crc != computed_crc:
                self.logger.warning("CRC mismatch: received=0x%04x, computed=0x%04x",
                                    received_crc, computed_crc)
                return None

            # Parse header
            msg_type, seq, payload_len = struct.unpack('>BBH', raw[:4])
            payload = raw[4:-2]

            if len(payload) != payload_len:
                self.logger.warning("Payload length mismatch: header=%d, actual=%d",
                                    payload_len, len(payload))
                return None

            self.logger.debug("RX: type=0x%02x seq=%d len=%d", msg_type, seq, payload_len)
            return (msg_type, seq, payload)

        except (serial.SerialException, OSError) as e:
            self.logger.error("UART read error: %s", e)
            return None
        finally:
            self._port.timeout = old_timeout

    def close(self):
        """Close the serial port."""
        if self._port is not None and self._port.is_open:
            self._port.close()
            self.logger.info("UART transport closed: %s", self.device)
            self._port = None
