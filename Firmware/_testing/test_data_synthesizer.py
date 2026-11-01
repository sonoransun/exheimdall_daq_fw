"""
	Description :
	Signal generator for testing the daq chain sample and phase
	synchronization mechanism

	In simulation mode (daq_synthetic_start.sh) this script stands in for
	rtl_daq.out: it writes IQ frames (1024-byte header + payload) to stdout and
	answers the inter-module ZMQ control messages of hw_controller.py and
	delay_sync.py on tcp://<[daq] zmq_listen_address or *>:<1130 + instance_id *
	port_stride>, so the simulated chain can calibrate.

	Usage (from Firmware/, next to daq_chain_config.ini):
	    python3 _testing/test_data_synthesizer.py [--seed S] [--pace]
	        [--blocks N] [--zero-delays] [--unit-id-from-config]
	With no flag it behaves as it always did: unpaced, unseeded, 10000 blocks.


	Project : HeIMDALL DAQ Firmware
	License : GNU GPL V3
	Author  : Tamas Peto

	Copyright (C) 2018-2020  Tamás Pető

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
import argparse
import itertools
import os
import sys
import time
import numpy as np
from configparser import ConfigParser
import logging
from threading import Thread, Event, Lock

try:
    import zmq
except ImportError:  # the control responder is disabled without pyzmq
    zmq = None


# Import IQ header module
currentPath = os.path.dirname(os.path.realpath(__file__))
rootPath = os.path.dirname(currentPath)
sys.path.insert(0, os.path.join(rootPath, "_daq_core"))
sys.path.insert(0, currentPath)
from iq_header import IQHeader
import inter_module_messages
from synth_model import FsSlipModel, channel_tables, advance_delays

# Import third party modules
# pyargus is only needed by (disabled-by-default) test case 4 - import it
# lazily so plain simulation mode does not depend on it
gen_scanning_vectors = None

def _require_pyargus():
    global gen_scanning_vectors
    if gen_scanning_vectors is None:
        from pyargus.directionEstimation import gen_scanning_vectors as _gsv
        gen_scanning_vectors = _gsv
    return gen_scanning_vectors

logger = logging.getLogger(__name__)

ZMQ_BASE_PORT = 1130     # rtl_daq control port of instance 0
DEFAULT_BLOCKS = 10000   # number of generated DAQ blocks without --blocks
NO_DUMMY_FRAMES = 5      # rtl_daq.c NO_DUMMY_FRAMES


class SynthControl:
    """
    Description:
    ------------
    Control state of the synthetic source, updated by the inter-module ZMQ
    messages exactly as rtl_daq.c applies them:

         c: Center frequency change         -> rf_center_freq
         g: Gain change (first M u32)       -> gains
         a: Enable AGC                      -> agc
         n: Noise source on/off             -> noise_source_state (CAL frames)
         b: Runtime bias-tee (first M u32)  -> bias_tee_flags (header slot 97)
         s: Sampling frequency correction   -> fs_model (sample slips)
         r: Tuner reconfiguration           -> deprecated, ignored
         h: System halt                     -> exit_request (clean exit)

    Every well-formed message requests NO_DUMMY_FRAMES header-only DUMMY
    frames (en_dummy_frame), as rtl_daq does while a change takes effect.
    The generation loop reads the attributes; take_dummy_request() and
    fs_slip_block() serialise against the message handler.
    """
    def __init__(self, ch_no, block_size):
        self.M = ch_no
        self.B = block_size
        self.lock = Lock()
        self.gains = [0]*self.M
        self.rf_center_freq = 0
        self.noise_source_state = 0
        self.en_dummy_frame = 0
        self.agc = 0
        self.bias_tee_flags = None  # None until the first 'b' message
        self.exit_request = False
        self.fs_model = FsSlipModel(self.M, self.B)

    def handle_message(self, buf):
        """
            Apply one received control message.

            Return:
            -------
                The reply to send: b"ok", or b"err" for a message that is not
                exactly 128 bytes (nothing is applied then)
        """
        try:
            module_id, cmd, params = inter_module_messages.unpack_msg(buf)
        except ValueError as err:
            logger.error("Malformed control message ignored: %s", err)
            return b"err"
        n = min(self.M, inter_module_messages.MAX_MSG_CHANNELS)
        logger.info("IM Request from: %d, command id: %s", module_id, cmd)
        with self.lock:
            if cmd == 'c':
                self.rf_center_freq = inter_module_messages.unpack_u32_params(params, 1)[0]
                logger.info("Center frequency: {:.2f} MHz".format(self.rf_center_freq/10**6))
            elif cmd == 'g':
                self.gains = inter_module_messages.unpack_u32_params(params, n)
                for m in range(n):
                    logger.debug("Channel: {:d}, Gain:{:d} /10 dB".format(m, self.gains[m]))
            elif cmd == 'a':
                self.agc = 1
                logger.info("AGC enabled")
            elif cmd == 'n':
                self.noise_source_state = 0 if params[0] == 0 else 1
                logger.info("Noise source turned {0}".format("on" if self.noise_source_state else "off"))
            elif cmd == 'b':
                states = inter_module_messages.unpack_u32_params(params, n)
                self.bias_tee_flags = sum(1 << m for m, state in enumerate(states) if state)
                logger.info("Bias-tee flags: 0x{:08X}".format(self.bias_tee_flags))
            elif cmd == 's':
                offsets = inter_module_messages.unpack_f32_params(params, n)
                self.fs_model.on_command(offsets)
                for m in range(n):
                    logger.info("Channel: {:d}, fs offset: {:.8f}".format(m, offsets[m]))
            elif cmd == 'r':
                logger.warning("ZMQ 'r' reconfiguration is deprecated, ignored")
            elif cmd == 'h':
                self.exit_request = True
                logger.info("Halt request received")
            else:
                logger.warning("Unidentified control command: %r", cmd)
            # Send out dummy frames while the change takes effect (rtl_daq.c)
            self.en_dummy_frame = 1
        return b"ok"

    def take_dummy_request(self):
        """Return True (once) when a message requested dummy frames."""
        with self.lock:
            requested = bool(self.en_dummy_frame)
            self.en_dummy_frame = 0
        return requested

    def fs_slip_block(self):
        """Advance the sampling-clock model by one DAQ block; per-channel slips."""
        with self.lock:
            return self.fs_model.on_block()

    def stop(self, timeout=None):
        """No responder thread to stop."""


class ZmqCtrThread(SynthControl, Thread):
    """
    Description:
    ------------
    ZMQ REP responder standing in for rtl_daq's control thread: receives the
    128-byte inter-module messages, applies them (SynthControl) and replies
    b"ok". The socket is bound in the constructor, so a bind failure raises
    zmq.ZMQError to the caller before the thread starts.

        :param: M: Number of channels
        :param: B: DAQ block size [sample]
        :param: port: TCP port; 0 binds an ephemeral port (see .port)
        :param: bind_addr: Bind host ("*", "0.0.0.0", "127.0.0.1", "localhost")
    """
    RCV_TIMEOUT_MS = 200  # stop() latency bound

    def __init__(self, M, B, port, bind_addr="*"):
        if zmq is None:
            raise RuntimeError("pyzmq is not installed")
        SynthControl.__init__(self, M, B)
        Thread.__init__(self, name="synth-zmq-ctr", daemon=True)
        host = "127.0.0.1" if bind_addr == "localhost" else (bind_addr or "*")
        self._stop_event = Event()
        self._ctx = zmq.Context()
        self._sock = self._ctx.socket(zmq.REP)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.setsockopt(zmq.RCVTIMEO, self.RCV_TIMEOUT_MS)
        try:
            if port:
                self.endpoint = "tcp://{0}:{1:d}".format(host, port)
                self._sock.bind(self.endpoint)
            else:
                self._sock.bind("tcp://{0}:*".format(host))
                self.endpoint = self._sock.getsockopt_string(zmq.LAST_ENDPOINT)
        except zmq.ZMQError:
            self._close()
            raise
        self.port = int(self.endpoint.rsplit(":", 1)[1])
        logger.info("Control responder bound to %s", self.endpoint)

    HALT_LINGER_MS = 1000  # lets the queued 'h' reply reach the REQ peer

    def _close(self, linger=0):
        if self._sock is not None:
            self._sock.close(linger=linger)
            self._sock = None
            self._ctx.term()

    def run(self):
        try:
            while not self._stop_event.is_set():
                try:
                    buf = self._sock.recv()
                except zmq.Again:
                    continue
                except zmq.ZMQError as err:
                    logger.error("Control socket receive failed: %s", err)
                    break
                reply = self.handle_message(buf)
                try:
                    self._sock.send(reply)
                except zmq.ZMQError as err:
                    logger.error("Control socket reply failed: %s", err)
                    break
                if self.exit_request:
                    break
        finally:
            self._close(self.HALT_LINGER_MS if self.exit_request else 0)

    def stop(self, timeout=None):
        """Stop the responder thread and release the socket."""
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)
        elif self.ident is None:  # never started: release the bound socket here
            self._close()


def zmq_endpoint(parser):
    """(bind_addr, port) of the control responder from the INI, as rtl_daq."""
    bind_addr = parser.get('daq', 'zmq_listen_address', fallback='').strip() or '*'
    instance_id = parser.getint('federation', 'instance_id', fallback=0)
    port_stride = parser.getint('federation', 'port_stride', fallback=100)
    return bind_addr, ZMQ_BASE_PORT + instance_id * port_stride


def resolve_unit_id(parser, from_config):
    """
        Header unit_id: `[hw] unit_id = auto` -> instance_id; with
        --unit-id-from-config the INI integer; otherwise 0 (historical value).
    """
    raw = parser.get('hw', 'unit_id', fallback='0').strip()
    if raw == 'auto':
        return parser.getint('federation', 'instance_id', fallback=0)
    if from_config:
        return int(raw)
    return 0


def _non_negative_int(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0, got {0}".format(value))
    return value


SEED_MAX = 2**32 - 1  # numpy's legacy global RNG accepts [0, 2**32 - 1]


def _seed(text):
    value = int(text)
    if not 0 <= value <= SEED_MAX:
        raise argparse.ArgumentTypeError("must be between 0 and {0:d}, got {1:d}".format(SEED_MAX, value))
    return value


def build_arg_parser():
    ap = argparse.ArgumentParser(
        description="Synthetic IQ source for the HeIMDALL DAQ chain (stands in for rtl_daq.out). "
                    "Writes IQ frames to stdout and answers the ZMQ control port.")
    ap.add_argument("--seed", type=_seed, default=None,
                    help="seed numpy's global RNG before the first draw (default: unseeded)")
    ap.add_argument("--pace", action="store_true",
                    help="emit blocks at the real-time rate daq_buffer_size/sample_rate (default: unpaced)")
    ap.add_argument("--blocks", type=_non_negative_int, default=DEFAULT_BLOCKS,
                    help="number of DAQ blocks to generate, 0 = unbounded (default: %(default)s)")
    ap.add_argument("--zero-delays", action="store_true",
                    help="render every channel with zero sample delay")
    ap.add_argument("--unit-id-from-config", action="store_true",
                    help="stamp [hw] unit_id from the INI into the header (default: 0)")
    return ap


def main(argv=None, out=None):
    """Run the synthetic source. Returns the process exit status."""
    args = build_arg_parser().parse_args(argv)
    if out is None:
        out = sys.stdout.buffer

    ####################################
    #           PARAMETERS
    ####################################
    logging.basicConfig(level=logging.INFO)

    parser = ConfigParser()
    found = parser.read(['daq_chain_config.ini'])
    if not found:
        logger.error("DAQ core configuration file not found. Exiting..")
        return 1
    logger.setLevel((parser.getint('daq', 'log_level')*10))

    N = parser.getint('pre_processing', 'cpi_size')
    N_daq = parser.getint('daq', 'daq_buffer_size')
    M = parser.getint('hw', 'num_ch')
    R = parser.getint('pre_processing', 'decimation_ratio')
    fs  = parser.getint('daq', 'sample_rate') # Sampling frequency [Hz]
    rf_freq = parser.getint('daq','center_freq') # Only used in the correspondig header field
    try:
        unit_id = resolve_unit_id(parser, args.unit_id_from_config)
    except ValueError:
        logger.error("[hw] unit_id must be an integer or 'auto' for --unit-id-from-config")
        return 1

    # Synchronization related parameters
    # delays [sample], phase_diffs [deg], cal_noise_phase_diffs [deg], power_diffs [dB]
    delays, phase_diffs, cal_noise_phase_diffs, power_diffs = channel_tables(M, args.zero_delays)
    max_delay = max(delays)
    delay_margin = N_daq # One previous block is kept: delays up to N_daq can be rendered

    blocks = args.blocks
    block_size = N_daq*2
    sig_type = "noise" #"none" / "noise" / "cw" / "swept-cw" / "pulse"
    sig_freq = 0.024 * 10**6# Interpreted as the distance from the center freq [Hz]
    # For swept-CW source type:
    sig_freq_sweep_start = -fs/2
    sig_freq_sweep_stop  =  fs/2
    sig_freq_sweep_step  =  fs/N*2**12
    """
        --> Power levels <--

        Signal amplitude equals to 1 means that the ADC is full scaled
        and the IF gain is at maximum 49.6 dB.
        Let us set call this power level to 0 dB.

    """
    ps = -200  # Signal power [dB]
    pn = -20  # Uncorrelated noise power [dB]
    pow_noise_source_dB = -23 # Correlated noise signal from the internal source [dB]

    """
        --> Antenna layout <-- [Test Case 4]
    """
    ant_config = "ULA"
    ula_d = 0.5 # Inter element spacing [lambda]
    uca_r = 1/(2*np.sqrt(2))

    if ant_config == "ULA":
        ant_x = np.zeros(M)
        ant_y = np.arange(M) * ula_d

    else: # UCA
        ant_x = uca_r*np.cos(np.deg2rad(np.arange(M)*360/M))
        ant_y = -uca_r*np.sin(np.deg2rad(np.arange(M)*360/M))

    """
        --> Test case control vector <--

        Put 1 in the corresponding position to enable a test case

        0: Phase diff is changed at the 30th data block
        1: Internal noise source phase diff is changed
        2: Dummy frame generation
        3: Increase power level of the usefull signal after the specified number of DAQ blocks
        4: Direction of arrival of the SOI is increased from block to block
        5: Squelch testing with one sinus bursts that has a length of one DAQ block
        6: Squelch testing with a short pulse
    """
    test_case_ctr_vector    = [0]*7

    tc3_start = 100 # Block index when tc3 is initiated
    tc4_start = 150 # Block index when tc4 is initiated
    tc5_start = 150 # Block index when tc5 is initiated
    tc6_start = 90 # Block index when tc6 is initiated

    theta_tc4 = 90 # Initial angle for tc4

    en_dummy_frame = False
    dummy_frame_cntr = 0

    ####################################
    #         Initialization
    ####################################
    iq_header = IQHeader()

    iq_header.sync_word            = IQHeader.SYNC_WORD
    iq_header.header_version       = IQHeader.HEADER_VERSION
    iq_header.frame_type           = 0 # 0 - Normal data frame, 3 - calibration frame
    iq_header.hardware_id          = "K"+str(M)
    iq_header.unit_id              = unit_id
    iq_header.active_ant_chs       = M
    iq_header.ioo_type             = 3
    iq_header.rf_center_freq       = 0
    iq_header.adc_sampling_freq    = fs
    iq_header.sampling_freq        = fs
    iq_header.cpi_length           = N_daq
    iq_header.time_stamp           = 0
    iq_header.daq_block_index      = 0
    iq_header.cpi_index            = 0
    iq_header.ext_integration_cntr = 0
    iq_header.data_type            = 2
    iq_header.sample_bit_depth     = 8
    iq_header.adc_overdrive_flags  = 0
    iq_header.if_gains             = [0]*32
    iq_header.noise_source_state   = 0

    logger.info("Decimation ratio: {:d}".format(R))
    logger.debug("IQ header size: {:d}".format(len(iq_header.encode_header())))

    bind_addr, zmq_port = zmq_endpoint(parser)
    ctr_errors = (RuntimeError, zmq.ZMQError) if zmq is not None else (RuntimeError,)
    try:
        ctr = ZmqCtrThread(M, N_daq, zmq_port, bind_addr)
    except ctr_errors as err:
        logger.error("Control responder on port %d unavailable (%s): hw_controller and delay_sync "
                     "commands will time out and the chain cannot calibrate", zmq_port, err)
        ctr = SynthControl(M, N_daq)
    ctr.rf_center_freq = rf_freq
    if isinstance(ctr, Thread):
        ctr.start()

    if args.seed is not None:
        np.random.seed(args.seed)

    ####################################
    #            Simulation
    ####################################

    # Allocation
    signal = np.zeros((block_size), dtype=np.uint8) # This array stores the samples that are written to output
    raw_sig_multiblock = np.zeros(N_daq*2, dtype=complex) # Stores 2 block of IQ samples
    internal_noise_multiblock = np.zeros(N_daq*2, dtype=complex) # Stores 2 block of IQ samples
    rendered_delays = list(delays) # Per-channel delays after the sample slips of the 's' model
    t_start = 4500 # For continuity testing
    block_period = N_daq / fs
    t_pace = time.monotonic()
    try:
        # Calculate signal amplitude
        sig_pow = 10**(ps/10)

        for b in (range(blocks) if blocks > 0 else itertools.count()):
            if ctr.exit_request:
                logger.info("Halt requested, exiting")
                break
            logger.info("Writing block: {:d}".format(b))
            iq_header.adc_overdrive_flags = 0
            iq_header.daq_block_index = b
            iq_header.time_stamp = int(time.time_ns()/10**6)
            if ctr.bias_tee_flags is not None:
                iq_header.set_bias_tee_state(ctr.bias_tee_flags)
            # Generate signal of interest

            if sig_type == "noise":
                std_dev = np.sqrt(sig_pow/2)
                raw_sig = np.random.normal(0,std_dev,(N_daq))+1j*np.random.normal(0,std_dev,(N_daq))
            elif sig_type == "cw":
                t = np.arange(N_daq) + t_start
                t_start = t[-1]
                sig_amp = np.sqrt(sig_pow)
                raw_sig =  sig_amp * np.exp(1j*2*np.pi*(sig_freq/fs)*t)
                logging.info("Current power : {:.2f} dB".format(ps))
            elif sig_type == "swept-cw":
                t = np.arange(N_daq)
                sig_amp = np.sqrt(sig_pow)
                logging.info("amp: {:.2f}".format(sig_amp))
                if b < tc3_start:
                    sig_freq = sig_freq_sweep_start
                    sent_data_frames = 0 # Since the last frequency change
                elif en_dummy_frame == False:
                    if sent_data_frames == 14: # 2+ decimation ratio
                        sig_freq += sig_freq_sweep_step
                        en_dummy_frame = True
                        dummy_frame_cntr = 0
                        sent_data_frames = 0
                    sent_data_frames += 1

                if sig_freq > sig_freq_sweep_stop:
                    sig_freq = sig_freq_sweep_start
                logging.info("Current CW frequency: {:.2f}".format(sig_freq))
                raw_sig =  sig_amp * np.exp(1j*2*np.pi*(sig_freq/fs)*t)
            elif sig_type == "none":
                raw_sig = np.zeros(N_daq+max_delay, dtype=np.complex64)
            elif sig_type == "pulse":
                mask = np.zeros(N_daq, dtype=np.complex64)
                mask[0:100] = 1
                t = np.arange(N_daq)
                sig_amp = np.sqrt(sig_pow)
                raw_sig =  sig_amp * np.exp(1j*2*np.pi*(sig_freq/fs)*t)
                raw_sig*=mask


            # Generate coherent noise for calibration to simulate the internal noise source
            std_dev = np.sqrt(10**(pow_noise_source_dB/10)/2)
            internal_noise = np.random.normal(0,std_dev,(N_daq+max_delay))+1j*np.random.normal(0,std_dev,(N_daq+max_delay))

            # Arange coherent signal components in multiblock array
            start_index = b%2*(N_daq)
            raw_sig_multiblock[start_index:start_index+N_daq] = raw_sig[0:N_daq]
            internal_noise_multiblock[start_index:start_index+N_daq] = internal_noise[0:N_daq]

            # Dummy frame control
            if ctr.take_dummy_request():
                en_dummy_frame = True
                dummy_frame_cntr = 0

            # Sampling clock model: integer sample slips caused by 's' commands
            rendered_delays = advance_delays(rendered_delays, ctr.fs_slip_block(), delay_margin)

            # Pack coherent multichannel signal array
            for m in range(M):
                d = rendered_delays[m]
                raw_sig_m = np.zeros((block_size//2), dtype = complex)
                #####################
                # Test case control
                #####################
                if m==0: # Chec and set only once
                    if test_case_ctr_vector[0]:
                        if b == 50:
                            if m==0: logging.info("Activating test case 0")
                            phase_diffs=[0 , 50, 20, 30] # deg

                    if test_case_ctr_vector[1]:
                        if b == 70:
                            if m==0: logging.info("Activating test case 1")
                            cal_noise_phase_diffs=[0 , 0, 0, 0] # deg

                    if test_case_ctr_vector[2]:
                        if (b==8):
                            en_dummy_frame = True
                            dummy_frame_cntr = 0
                        else:
                            en_dummy_frame = False

                    if test_case_ctr_vector[3]:
                        if (b==tc3_start):
                            ps = -10
                            sig_pow = 10**(ps/10)

                    if test_case_ctr_vector[4] and b > tc4_start:
                        if (tc4_start-b)%20 == 0:
                            theta_tc4 += 5
                        phase_diffs = np.rad2deg(np.angle(_require_pyargus()(M, ant_x, ant_y, [theta_tc4,])[:,0]))
                        logging.info("DoA simulation - incident anlge :{:.2f}".format(theta_tc4))

                    if test_case_ctr_vector[5] and b> tc5_start:
                        if b%20 == 0:
                            ps = -3
                            sig_pow = 10**(ps/10)
                        elif b%20 == 1:
                            ps = -200
                            sig_pow = 10**(ps/10)

                    if test_case_ctr_vector[6] and b> tc6_start:
                        if b%20 == 0:
                            ps = -6
                            sig_pow = 10**(ps/10)
                            sig_type="pulse"
                        elif b%20 == 1:
                            sig_type="none"
                            ps = -200
                            sig_pow = 10**(ps/10)


                #######################################
                # Compose received signal at channel m
                #######################################

                # Add Internal coherent noise source signal if the noise source is enabled
                if ctr.noise_source_state == 1:
                    if b%2 == 0:
                        raw_sig_m[0:d]     += internal_noise_multiblock[2*N_daq-d:2*N_daq] \
                                                   * 10**(power_diffs[m]/20) \
                                                   *  np.exp(1j*np.deg2rad(cal_noise_phase_diffs[m]))

                        raw_sig_m[d:N_daq] += internal_noise_multiblock[0:N_daq-d] \
                                                   * 10**(power_diffs[m]/20) \
                                                   *  np.exp(1j*np.deg2rad(cal_noise_phase_diffs[m]))
                    else: # b%2 =1
                        raw_sig_m[:] += internal_noise_multiblock[N_daq-d:2*N_daq-d] \
                                     * 10**(power_diffs[m]/20) \
                                     *  np.exp(1j*np.deg2rad(cal_noise_phase_diffs[m]))

                # Add usefull signal component and perform amplitude and phase distortion
                if b%2 == 0:
                    raw_sig_m[0:d]     += raw_sig_multiblock[2*N_daq-d:2*N_daq] \
                                               * 10**(power_diffs[m]/20) \
                                               *  np.exp(1j*np.deg2rad(phase_diffs[m]))

                    raw_sig_m[d:N_daq] += raw_sig_multiblock[0:N_daq-d] \
                                               * 10**(power_diffs[m]/20) \
                                               *  np.exp(1j*np.deg2rad(phase_diffs[m]))
                else: # b%2 =1
                    raw_sig_m[:] += raw_sig_multiblock[N_daq-d:2*N_daq-d] \
                                 * 10**(power_diffs[m]/20) \
                                 *  np.exp(1j*np.deg2rad(phase_diffs[m]))

                # # Corrupt useful signal with additive non-coherent noise
                std_dev = np.sqrt(10**(pn/10)/2)
                noise = np.random.normal(0,std_dev,(N_daq))+1j*np.random.normal(0,std_dev,(N_daq))
                raw_sig_m += noise

                #######################################
                # Test case [2] -Dummy frame generation
                #######################################
                if en_dummy_frame and m==0:
                    if dummy_frame_cntr == NO_DUMMY_FRAMES:
                        en_dummy_frame = False
                    else:
                        dummy_frame_cntr +=1
                if en_dummy_frame:
                    raw_sig_m = np.zeros((N_daq), dtype=complex)

                # Simulating ADC saturation
                raw_sig_m.real[raw_sig_m.real > 1] = 1
                raw_sig_m.imag[raw_sig_m.imag > 1] = 1

                raw_sig_m.real[raw_sig_m.real < -1] = -1
                raw_sig_m.imag[raw_sig_m.imag < -1] = -1

                #######################################
                #          IQ HEADER Control
                #######################################
                # Stamped on every frame, dummies included, as rtl_daq.c
                iq_header.noise_source_state = ctr.noise_source_state
                if en_dummy_frame : # Dummy Frame: header only, as rtl_daq.c
                    iq_header.data_type  = 0
                    iq_header.frame_type = IQHeader.FRAME_TYPE_DUMMY
                    iq_header.cpi_length = 0
                    if m==0 :logging.info("Frame type: Dummy")
                else:
                    iq_header.cpi_length = N_daq
                    if ctr.noise_source_state == 1: # Calibration Frame
                        iq_header.frame_type           = IQHeader.FRAME_TYPE_CAL
                        iq_header.data_type            = 1
                        if m==0 : logging.info("Frame type: Calibration")
                    else: # Normal Data Frame
                        iq_header.frame_type           = IQHeader.FRAME_TYPE_DATA
                        iq_header.data_type            = 1
                        if m==0 : logging.info("Frame type: Data")

                # Update gain status in the header
                iq_header.if_gains[m] = ctr.gains[m]
                # Update center frequency field in the header
                iq_header.rf_center_freq = int(ctr.rf_center_freq+sig_freq)

                if (raw_sig_m.real == 1).any():
                    iq_header.adc_overdrive_flags |= 1<<m
                    logging.warning("Overdrive at channel: {:d}".format(m))

                if (raw_sig_m.imag == 1).any():
                    iq_header.adc_overdrive_flags |= 1<<m
                    logging.warning("Overdrive at channel: {:d}".format(m))

                #######################################
                #           SEND DATA FRAME
                #######################################

                raw_sig_m += (1+1j)
                raw_sig_m *= (255/2)

                signal[0::2] = raw_sig_m.real
                signal[1::2] = raw_sig_m.imag
                # Pass IQ header and data to the next processing block
                if m==0:
                    out.write(iq_header.encode_header()) # Write the IQ header
                if not en_dummy_frame:
                    out.write(signal.tobytes()) #Write IQ data

            if args.pace:
                t_pace += block_period
                delay = t_pace - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    t_pace = time.monotonic() # fell behind: do not burst to catch up
        out.flush()
    except Exception:
        logging.exception("Unexpected error in the synthetic data chain")
    finally:
        ctr.stop(timeout=1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
