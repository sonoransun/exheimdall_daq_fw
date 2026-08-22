"""
    Rotator / Pan-Tilt Hardware Backend Abstraction

    Pluggable hardware backends that physically slew a directional antenna (or
    the whole array) to a commanded azimuth/elevation. Patterned on
    dac_controller.py: an interface-selected backend with an `init_status`
    flag, lazy hardware imports, and a mock/loopback backend for no-hardware
    unit tests.

    Backend interface (duck-typed):
        move_to(az_deg, el_deg)   -> command a slew to an absolute bearing
        get_position()            -> (az_deg, el_deg) current/last-known
        is_moving()               -> bool
        stop()                    -> halt/hold
        home()                    -> drive to the home/reference position
        close()                   -> release the hardware
        init_status               -> bool, True when the backend is usable

    Backends:
        MockRotatorBackend    - loopback simulation, the test vehicle
        SerialGS232Backend    - GS-232 / Yaesu az-el rotators over serial
        PWMServoBackend       - GPIO/PWM hobby-servo pan-tilt (pigpio)
        I2CPanTiltBackend     - PCA9685 I2C servo-driver pan-tilt

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import logging

logger = logging.getLogger(__name__)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class MockRotatorBackend:
    """Loopback rotator that simulates motion toward the commanded target.

    Advances its position toward the target by `slew_rate_dps` degrees each
    time is_moving() is polled, so a controller ticking once per frame drives
    the simulation deterministically. No hardware, no imports.
    """

    def __init__(self, slew_rate_dps=6.0, tolerance_deg=1.0):
        self.init_status = True
        self.slew_rate_dps = max(slew_rate_dps, 0.1)
        self.tolerance = tolerance_deg
        self.cur_az = 0.0
        self.cur_el = 0.0
        self.tgt_az = 0.0
        self.tgt_el = 0.0
        self._moving = False
        # Test-observable history
        self.move_calls = []
        self.stop_calls = 0

    def move_to(self, az_deg, el_deg):
        self.tgt_az = az_deg
        self.tgt_el = el_deg
        self.move_calls.append((az_deg, el_deg))
        self._moving = (abs(az_deg - self.cur_az) > self.tolerance or
                        abs(el_deg - self.cur_el) > self.tolerance)

    def _advance_axis(self, cur, tgt):
        d = tgt - cur
        if abs(d) <= self.slew_rate_dps:
            return tgt
        return cur + self.slew_rate_dps * (1.0 if d > 0 else -1.0)

    def is_moving(self):
        if self._moving:
            self.cur_az = self._advance_axis(self.cur_az, self.tgt_az)
            self.cur_el = self._advance_axis(self.cur_el, self.tgt_el)
            if (abs(self.tgt_az - self.cur_az) <= self.tolerance and
                    abs(self.tgt_el - self.cur_el) <= self.tolerance):
                self.cur_az, self.cur_el = self.tgt_az, self.tgt_el
                self._moving = False
        return self._moving

    def get_position(self):
        return (self.cur_az, self.cur_el)

    def stop(self):
        self._moving = False
        self.stop_calls += 1

    def home(self):
        self.cur_az = 0.0
        self.cur_el = 0.0
        self._moving = False

    def close(self):
        pass


class SerialGS232Backend:
    """GS-232 / Yaesu azimuth-elevation rotator controller over serial.

    Commands: "Waaa eee\\r" slews to az/el, "C2\\r" reads "+0aaa+0eee",
    "S\\r" stops. Position feedback is closed-loop via C2.
    """

    def __init__(self, cfg):
        self.init_status = False
        self._last_cmd = (0.0, 0.0)
        try:
            import serial  # pyserial, lazy import
            self._serial = serial.Serial(
                port=getattr(cfg, 'device', '/dev/ttyUSB0'),
                baudrate=int(getattr(cfg, 'baud', 9600)),
                timeout=0.5)
            self.init_status = True
        except Exception as e:
            logger.error("GS-232 serial init failed: %s", e)
            self._serial = None

    def move_to(self, az_deg, el_deg):
        self._last_cmd = (az_deg, el_deg)
        if self._serial is None:
            return
        try:
            self._serial.write("W{:03d} {:03d}\r".format(
                int(round(az_deg)) % 360, int(round(max(0.0, el_deg)))).encode())
        except Exception as e:
            logger.error("GS-232 move_to failed: %s", e)

    def get_position(self):
        if self._serial is None:
            return self._last_cmd
        try:
            self._serial.write(b"C2\r")
            resp = self._serial.readline().decode(errors='ignore').strip()
            # Expected form: +0aaa+0eee
            if '+' in resp:
                fields = resp.replace('-', '+-').split('+')
                nums = [f for f in fields if f]
                if len(nums) >= 2:
                    return (float(nums[0]), float(nums[1]))
        except Exception as e:
            logger.debug("GS-232 get_position failed: %s", e)
        return self._last_cmd

    def is_moving(self):
        az, el = self.get_position()
        return (abs(az - self._last_cmd[0]) > 1.0 or abs(el - self._last_cmd[1]) > 1.0)

    def stop(self):
        if self._serial is not None:
            try:
                self._serial.write(b"S\r")
            except Exception as e:
                logger.error("GS-232 stop failed: %s", e)

    def home(self):
        self.move_to(0.0, 0.0)

    def close(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass


class PWMServoBackend:
    """GPIO/PWM hobby-servo pan-tilt via pigpio. Open-loop position.

    Maps angle to a 500-2500 us pulse width; is_moving() uses a slew-time
    estimate since there is no position feedback.
    """

    def __init__(self, cfg):
        self.init_status = False
        self._az_min = float(getattr(cfg, 'az_min_deg', 0.0))
        self._az_max = float(getattr(cfg, 'az_max_deg', 360.0))
        self._el_min = float(getattr(cfg, 'el_min_deg', 0.0))
        self._el_max = float(getattr(cfg, 'el_max_deg', 90.0))
        self._az_pin = int(getattr(cfg, 'az_pwm_channel', 0))
        self._el_pin = int(getattr(cfg, 'el_pwm_channel', 1))
        self._slew_rate = max(float(getattr(cfg, 'slew_rate_dps', 6.0)), 0.1)
        self._cur = (0.0, 0.0)
        self._prev = (0.0, 0.0)
        self._move_started_at = 0.0
        self._move_done_at = 0.0
        try:
            import pigpio  # lazy import
            import time
            self._time = time
            self._pi = pigpio.pi()
            if not self._pi.connected:
                raise RuntimeError("pigpio daemon not connected")
            self.init_status = True
        except Exception as e:
            logger.error("PWM servo init failed: %s", e)
            self._pi = None

    def _angle_to_pulse(self, angle, lo, hi):
        frac = (_clamp(angle, lo, hi) - lo) / (hi - lo) if hi > lo else 0.0
        return int(500 + frac * 2000)  # 500-2500 us

    def _command_pulses(self, az, el):
        self._pi.set_servo_pulsewidth(self._az_pin, self._angle_to_pulse(az, self._az_min, self._az_max))
        self._pi.set_servo_pulsewidth(self._el_pin, self._angle_to_pulse(el, self._el_min, self._el_max))

    def move_to(self, az_deg, el_deg):
        prev = self._cur
        self._cur = (_clamp(az_deg, self._az_min, self._az_max),
                     _clamp(el_deg, self._el_min, self._el_max))
        if self._pi is not None:
            try:
                self._command_pulses(self._cur[0], self._cur[1])
            except Exception as e:
                logger.error("PWM servo move_to failed: %s", e)
        dist = max(abs(self._cur[0] - prev[0]), abs(self._cur[1] - prev[1]))
        self._prev = prev
        self._move_started_at = self._time.monotonic()
        self._move_done_at = self._move_started_at + dist / self._slew_rate

    def get_position(self):
        return self._cur

    def is_moving(self):
        return self._time.monotonic() < self._move_done_at

    def _estimate_position(self):
        """Interpolate the physical position along the current slew."""
        now = self._time.monotonic()
        if now >= self._move_done_at:
            return self._cur
        span = self._move_done_at - self._move_started_at
        frac = (now - self._move_started_at) / span if span > 0 else 1.0
        frac = _clamp(frac, 0.0, 1.0)
        return (self._prev[0] + frac * (self._cur[0] - self._prev[0]),
                self._prev[1] + frac * (self._cur[1] - self._prev[1]))

    def stop(self):
        # The servo keeps slewing toward the last commanded pulse on its own;
        # to actually halt, re-command the estimated in-flight position.
        if self._pi is not None and self.is_moving():
            est = self._estimate_position()
            self._cur = est
            try:
                self._command_pulses(est[0], est[1])
            except Exception as e:
                logger.error("PWM servo stop failed: %s", e)
        self._move_done_at = 0.0

    def home(self):
        self.move_to(self._az_min, self._el_min)

    def close(self):
        if self._pi is not None:
            try:
                self._pi.set_servo_pulsewidth(self._az_pin, 0)
                self._pi.set_servo_pulsewidth(self._el_pin, 0)
                self._pi.stop()
            except Exception:
                pass


class I2CPanTiltBackend:
    """PCA9685 I2C servo-driver pan-tilt. Open-loop position, slew-time model."""

    _MODE1 = 0x00
    _LED0_ON_L = 0x06
    _PRESCALE = 0xFE

    def __init__(self, cfg):
        self.init_status = False
        self._az_min = float(getattr(cfg, 'az_min_deg', 0.0))
        self._az_max = float(getattr(cfg, 'az_max_deg', 360.0))
        self._el_min = float(getattr(cfg, 'el_min_deg', 0.0))
        self._el_max = float(getattr(cfg, 'el_max_deg', 90.0))
        self._az_ch = int(getattr(cfg, 'az_pwm_channel', 0))
        self._el_ch = int(getattr(cfg, 'el_pwm_channel', 1))
        self._addr = int(getattr(cfg, 'i2c_address', 0x40))
        self._slew_rate = max(float(getattr(cfg, 'slew_rate_dps', 6.0)), 0.1)
        self._cur = (0.0, 0.0)
        self._prev = (0.0, 0.0)
        self._move_started_at = 0.0
        self._move_done_at = 0.0
        try:
            from smbus2 import SMBus  # lazy import
            import time
            self._time = time
            self._bus = SMBus(int(getattr(cfg, 'i2c_bus', 1)))
            # PCA9685 init: program the prescaler for a 50 Hz servo frame.
            # The chip powers up at prescale 0x1E (~200 Hz); the prescale
            # register can only be written while the oscillator sleeps.
            # prescale = round(25 MHz / (4096 * 50 Hz)) - 1 = 121
            prescale = int(round(25000000.0 / (4096.0 * 50.0))) - 1
            self._bus.write_byte_data(self._addr, self._MODE1, 0x10)  # SLEEP
            self._bus.write_byte_data(self._addr, self._PRESCALE, prescale)
            self._bus.write_byte_data(self._addr, self._MODE1, 0x00)  # wake
            time.sleep(0.001)  # oscillator startup (>500 us)
            self._bus.write_byte_data(self._addr, self._MODE1, 0x80)  # RESTART
            self.init_status = True
        except Exception as e:
            logger.error("I2C pan-tilt init failed: %s", e)
            self._bus = None

    def _angle_to_counts(self, angle, lo, hi):
        # 50 Hz frame -> 4096 counts; 1-2 ms pulse ~ 205-410 counts
        frac = (_clamp(angle, lo, hi) - lo) / (hi - lo) if hi > lo else 0.0
        return int(205 + frac * 205)

    def _set_channel(self, ch, counts):
        base = self._LED0_ON_L + 4 * ch
        self._bus.write_byte_data(self._addr, base, 0)
        self._bus.write_byte_data(self._addr, base + 1, 0)
        self._bus.write_byte_data(self._addr, base + 2, counts & 0xFF)
        self._bus.write_byte_data(self._addr, base + 3, (counts >> 8) & 0x0F)

    def _command_counts(self, az, el):
        self._set_channel(self._az_ch, self._angle_to_counts(az, self._az_min, self._az_max))
        self._set_channel(self._el_ch, self._angle_to_counts(el, self._el_min, self._el_max))

    def move_to(self, az_deg, el_deg):
        prev = self._cur
        self._cur = (_clamp(az_deg, self._az_min, self._az_max),
                     _clamp(el_deg, self._el_min, self._el_max))
        if self._bus is not None:
            try:
                self._command_counts(self._cur[0], self._cur[1])
            except Exception as e:
                logger.error("I2C pan-tilt move_to failed: %s", e)
        dist = max(abs(self._cur[0] - prev[0]), abs(self._cur[1] - prev[1]))
        self._prev = prev
        self._move_started_at = self._time.monotonic()
        self._move_done_at = self._move_started_at + dist / self._slew_rate

    def get_position(self):
        return self._cur

    def is_moving(self):
        return self._time.monotonic() < self._move_done_at

    def _estimate_position(self):
        """Interpolate the physical position along the current slew."""
        now = self._time.monotonic()
        if now >= self._move_done_at:
            return self._cur
        span = self._move_done_at - self._move_started_at
        frac = (now - self._move_started_at) / span if span > 0 else 1.0
        frac = _clamp(frac, 0.0, 1.0)
        return (self._prev[0] + frac * (self._cur[0] - self._prev[0]),
                self._prev[1] + frac * (self._cur[1] - self._prev[1]))

    def stop(self):
        # Servos keep slewing toward the last commanded pulse on their own;
        # to actually halt, re-command the estimated in-flight position.
        if self._bus is not None and self.is_moving():
            est = self._estimate_position()
            self._cur = est
            try:
                self._command_counts(est[0], est[1])
            except Exception as e:
                logger.error("I2C pan-tilt stop failed: %s", e)
        self._move_done_at = 0.0

    def home(self):
        self.move_to(self._az_min, self._el_min)

    def close(self):
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass


def create_rotator_backend(cfg):
    """Factory: build the backend named by cfg.backend, falling back to mock.

    Never raises — an unusable real backend is logged and replaced by the mock
    so the DAQ chain always starts.
    """
    backend = getattr(cfg, 'backend', 'mock')
    slew = getattr(cfg, 'slew_rate_dps', 6.0)
    tol = getattr(cfg, 'position_tolerance_deg', 1.0)
    try:
        if backend == 'gs232':
            b = SerialGS232Backend(cfg)
        elif backend == 'pwm_servo':
            b = PWMServoBackend(cfg)
        elif backend == 'i2c_pantilt':
            b = I2CPanTiltBackend(cfg)
        elif backend == 'mock':
            b = MockRotatorBackend(slew_rate_dps=slew, tolerance_deg=tol)
        else:
            logger.warning("Unknown rotator backend '%s', using mock", backend)
            b = MockRotatorBackend(slew_rate_dps=slew, tolerance_deg=tol)
        if getattr(b, 'init_status', False):
            return b
        logger.error("Rotator backend '%s' unavailable; falling back to mock", backend)
    except Exception as e:
        logger.error("Rotator backend '%s' init raised %s; falling back to mock", backend, e)
    return MockRotatorBackend(slew_rate_dps=slew, tolerance_deg=tol)
