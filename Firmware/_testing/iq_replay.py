"""
    Description :
    Replays a `.hcap` IQ capture (see iq_capture_format.py).

    --serve-port P     emulates iq_server.out on TCP port P for a
                       post_sync_cf32 capture: one client at a time, the
                       "streaming" handshake, then one frame per "IQDownload"
                       ack - the same single-recv exact-match checks, socket
                       timeouts and close rules as iq_server's default loop.
                       A client that disconnects is followed by the next one,
                       which continues with the next frame.
    --raw-to-stdout    writes a raw_u8 capture to stdout in rtl_daq.out's
                       stdout format, for piping into `rebuffer.out 0`
                       (daq_replay_start.sh does this).

    Options for both modes:
    --realtime         keep the recorded inter-frame timing (header time_stamp
                       deltas; the frame period cpi_length / sampling_freq
                       where time stamps are missing or not increasing)
    --loop             start over at the end of the capture (otherwise the
                       replay ends: the served client is closed, stdout is
                       closed)
    --restamp          rewrite the header time_stamp to the send time (Unix
                       ms), so latency measurements against a replay are
                       meaningful
    --control-port P   answer the 128-byte inter-module control messages of
                       hw_controller/delay_sync on a ZMQ REP socket at port P
                       with "ok" (as rtl_daq does) without applying them; 'h'
                       (halt) ends the replay. 0 (default) = no responder.
                       With --raw-to-stdout every other message turns the
                       next 5 frames into header-only DUMMY frames, as
                       rtl_daq does while a change takes effect.

    Usage (from Firmware/):
        python3 _testing/iq_replay.py field.hcap --serve-port 5000 [--listen-address A]
            [--realtime] [--loop] [--restamp]
        python3 _testing/iq_replay.py raw.hcap --raw-to-stdout [--control-port 1130] \
            | _daq_core/rebuffer.out 0

    Exit codes: 0 replay finished (or the pipe reader went away), 1 runtime
    error (bind failure, unreadable or corrupt capture), 2 refused (wrong
    capture kind, no sidecar, usage error).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import argparse
import logging
import os
import socket
import struct
import sys
import threading
import time

currentPath = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, currentPath)
import iq_capture_format as hcap  # noqa: E402  (also puts _daq_core on sys.path)

logger = logging.getLogger("iq_replay")

# iq_server.c single-client loop constants
SEND_FRAME_DEADLINE_S = 10.0  # bound on the total send time of one frame
SERVER_RCVTIMEO_S = 5.0       # SO_RCVTIMEO: ack (and handshake) wait
LISTEN_BACKLOG = 5
HANDSHAKE = b"streaming"
ACK = b"IQDownload"
# Byte offsets in the native-aligned header
FRAME_TYPE_OFFSET = 4
TIME_STAMP_OFFSET = struct.calcsize("II16sIIIQQQIQ") - 8  # uint64 time_stamp
DATA_TYPE_OFFSET = struct.calcsize("II16sIIIQQQIQIIQ")    # uint32 data_type
# rtl_daq.c NO_DUMMY_FRAMES: frames sent as DUMMY after every control message
NO_DUMMY_FRAMES = 5


class CaptureRefused(hcap.HcapError):
    """The capture is readable but cannot be replayed in this mode."""


class ReplayStopped(Exception):
    """The replay was stopped (halt command or stop())."""


def frame_period_s(header):
    """CPI duration of a frame (0.0 when unknown)."""
    if header.cpi_length and header.sampling_freq:
        return header.cpi_length / float(header.sampling_freq)
    return 0.0


def open_capture(path, kind):
    """Validate `path` for replay as `kind` and return its HcapReader. Every
    header is checked up front (payloads are skipped), so a corrupt capture
    is rejected before the first frame is sent. Raises CaptureRefused when
    the capture is not of `kind`, hcap.HcapError when it is corrupt."""
    reader = hcap.HcapReader(path)
    if reader.meta is None:
        raise CaptureRefused("{} has no sidecar {}: capture kind unknown".format(
            path, hcap.sidecar_path(path)))
    if reader.kind != kind:
        raise CaptureRefused("{} is a {} capture; this mode replays {} captures".format(
            path, reader.kind, kind))
    if not any(True for _ in reader.frames(with_payload=False)):
        raise CaptureRefused("{} contains no frames".format(path))
    return reader


def _put_u32(header_bytes, offset, value):
    return header_bytes[:offset] + value.to_bytes(4, sys.byteorder) + header_bytes[offset + 4:]


def dummy_frame(header_bytes):
    """Header-only DUMMY version of a frame, as rtl_daq.c builds it while a
    control change takes effect: frame_type DUMMY, data_type 0, cpi_length 0,
    every other field (daq_block_index, cpi_index, ...) unchanged."""
    header_bytes = _put_u32(header_bytes, FRAME_TYPE_OFFSET, hcap.IQHeader.FRAME_TYPE_DUMMY)
    header_bytes = _put_u32(header_bytes, DATA_TYPE_OFFSET, 0)
    return _put_u32(header_bytes, hcap.CPI_LENGTH_OFFSET, 0)


def frame_delay(prev, header):
    """Recorded gap (seconds) between frame `prev` and the next frame
    `header`: the time_stamp delta, or prev's frame period when either time
    stamp is missing or they do not increase (also at a loop wrap)."""
    delta = (header.time_stamp - prev.time_stamp) / 1000.0
    if header.time_stamp == 0 or prev.time_stamp == 0 or delta <= 0:
        return frame_period_s(prev)
    return delta


class FrameSource:
    """Frame sequencer shared by both modes: loop, pacing and restamping.
    `frames` is an HcapReader (read lazily, reopened on every pass) or a
    list of (header_bytes, IQHeader, payload). request_dummies(n) (thread
    safe) sends the next n frames as header-only DUMMY frames, in place of
    their recorded content, as rtl_daq does after a control message."""

    def __init__(self, frames, realtime=False, loop=False, restamp=False):
        if isinstance(frames, hcap.HcapReader):
            self._open = frames.raw_frames
        else:
            self._open = lambda: iter(frames)
        self.realtime = realtime
        self.loop = loop
        self.restamp = restamp
        self.sent = 0
        self._frames = self._open()
        self._prev_header = None
        self._next_due = None
        self._stop = threading.Event()
        self._dummy_lock = threading.Lock()
        self._dummies_left = 0

    def stop(self):
        self._stop.set()

    def request_dummies(self, count=NO_DUMMY_FRAMES):
        """Send the next `count` frames as DUMMY frames (restarts the count,
        like rtl_daq's dummy_frame_cntr = 0)."""
        with self._dummy_lock:
            self._dummies_left = count

    def _take_dummy(self):
        with self._dummy_lock:
            if self._dummies_left <= 0:
                return False
            self._dummies_left -= 1
            return True

    @property
    def stopped(self):
        return self._stop.is_set()

    def _advance(self):
        frame = next(self._frames, None)
        if frame is None and self.loop:
            self._frames = self._open()
            frame = next(self._frames, None)
        return frame

    def next_frame(self):
        """The next (header_bytes, payload) to send, or None at the end.
        Sleeps until the frame is due with --realtime."""
        if self._stop.is_set():
            raise ReplayStopped()
        frame = self._advance()
        if frame is None:
            return None
        header_bytes, header, payload = frame
        if self.realtime:
            now = time.monotonic()
            if self._next_due is None:
                self._next_due = now
            else:
                self._next_due += frame_delay(self._prev_header, header)
            # A consumer that fell behind by more than one second does not get
            # a burst of catch-up frames
            if self._next_due < now - 1.0:
                self._next_due = now
            if self._stop.wait(max(0.0, self._next_due - now)):
                raise ReplayStopped()
        self._prev_header = header
        if self._take_dummy():
            header_bytes, payload = dummy_frame(header_bytes), b""
        if self.restamp:
            stamp = int(time.time() * 1000).to_bytes(8, sys.byteorder)
            header_bytes = header_bytes[:TIME_STAMP_OFFSET] + stamp + header_bytes[TIME_STAMP_OFFSET + 8:]
        self.sent += 1
        return header_bytes, payload


class ControlResponder(threading.Thread):
    """ZMQ REP stand-in for rtl_daq's control thread: replies b"ok" to every
    128-byte message (b"err" otherwise) without applying it; 'h' stops the
    source. Every other acknowledged message requests `dummy_frames` DUMMY
    frames from the source (rtl_daq's en_dummy_frame; 0 = none). Commands
    are logged once per command character."""

    RCV_TIMEOUT_MS = 200
    MSG_SIZE = 128

    def __init__(self, port, source, bind_addr="127.0.0.1", dummy_frames=0):
        import zmq  # optional dependency, only needed with --control-port
        threading.Thread.__init__(self, name="replay-zmq-ctr", daemon=True)
        self._zmq = zmq
        self.source = source
        self.dummy_frames = dummy_frames
        self._seen = set()
        self._ctx = zmq.Context()
        self._sock = self._ctx.socket(zmq.REP)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.setsockopt(zmq.RCVTIMEO, self.RCV_TIMEOUT_MS)
        try:
            self._sock.bind("tcp://{}:{:d}".format(bind_addr, port))
        except zmq.ZMQError:
            self._sock.close()
            self._ctx.term()
            raise

    def run(self):
        zmq = self._zmq
        try:
            while not self.source.stopped:
                try:
                    msg = self._sock.recv()
                except zmq.Again:
                    continue
                # byte 0: source module id, byte 1: command character
                cmd = chr(msg[1]) if len(msg) == self.MSG_SIZE else None
                self._sock.send(b"ok" if cmd is not None else b"err")
                if cmd is None:
                    logger.error("Control message of %d bytes rejected", len(msg))
                elif cmd == "h":
                    logger.info("Halt command received - ending the replay")
                    self.source.stop()
                else:
                    if self.dummy_frames:
                        self.source.request_dummies(self.dummy_frames)
                    if cmd not in self._seen:
                        self._seen.add(cmd)
                        logger.warning("Control command '%s' acknowledged but not applied (replay)", cmd)
        except zmq.ZMQError as err:
            logger.error("Control responder failed: %s", err)
        finally:
            self._sock.close(linger=1000)
            self._ctx.term()


def bind_server(port, listen_address="0.0.0.0"):
    """The listening socket of the emulated iq_server (SO_REUSEADDR, backlog 5)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((listen_address, port))
        sock.listen(LISTEN_BACKLOG)
    except OSError:
        sock.close()
        raise
    return sock


def _handshake(client):
    """iq_stream_accept(): one recv, exact match of "streaming"."""
    try:
        data = client.recv(1023)
    except OSError:
        return False
    return data == HANDSHAKE


def serve_session(client, source):
    """One client session of iq_server's default loop. Returns False when
    the capture is exhausted (the replay ends), True when the client left."""
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    client.settimeout(SERVER_RCVTIMEO_S)
    if not _handshake(client):
        logger.error("Client did not send the streaming request - closing")
        return True
    logger.info("Streaming request received")
    while True:
        frame = source.next_frame()
        if frame is None:
            logger.info("End of capture after %d frames", source.sent)
            return False
        header_bytes, payload = frame
        try:
            client.settimeout(SEND_FRAME_DEADLINE_S)
            client.sendall(header_bytes + payload if payload else header_bytes)
            client.settimeout(SERVER_RCVTIMEO_S)
            ack = client.recv(1023)
        except OSError as err:
            logger.error("Closing connection: %s", err)
            return True
        if ack != ACK:
            logger.info("Client closed the session")
            return True


def serve(listen_sock, source):
    """Accept clients one after another until the capture ends or the
    source is stopped."""
    listen_sock.settimeout(0.2)
    while not source.stopped:
        try:
            client, addr = listen_sock.accept()
        except socket.timeout:
            continue
        logger.info("New connection from (%s , %d)", addr[0], addr[1])
        with client:
            try:
                more = serve_session(client, source)
            except ReplayStopped:
                return
        if not more:
            return


def stream_raw(out, source):
    """Write every frame to the binary stream `out` until the end."""
    while True:
        frame = source.next_frame()
        if frame is None:
            return
        header_bytes, payload = frame
        out.write(header_bytes)
        if payload:
            out.write(payload)
        out.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Replay a .hcap IQ capture")
    parser.add_argument("capture", help=".hcap capture file (with its .hcap.json sidecar)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve-port", type=int, metavar="P",
                      help="emulate iq_server on TCP port P (post_sync_cf32 captures)")
    mode.add_argument("--raw-to-stdout", action="store_true",
                      help="write a raw_u8 capture to stdout for rebuffer.out")
    parser.add_argument("--listen-address", default="0.0.0.0",
                        help="bind address of --serve-port (default: %(default)s)")
    parser.add_argument("--realtime", action="store_true", help="keep the recorded frame timing")
    parser.add_argument("--loop", action="store_true", help="start over at the end of the capture")
    parser.add_argument("--restamp", action="store_true",
                        help="rewrite the header time_stamp to the send time")
    parser.add_argument("--control-port", type=int, default=0, metavar="P",
                        help="acknowledge control messages on ZMQ port P (0 = off)")
    parser.add_argument("--control-address", default="127.0.0.1",
                        help="bind address of --control-port (default: %(default)s)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s iq_replay: %(message)s")

    for name, value in (("--serve-port", args.serve_port), ("--control-port", args.control_port)):
        if value is not None and not 0 <= value <= 65535:
            print("ERROR: {} must be 0..65535".format(name), file=sys.stderr)
            return 2

    kind = hcap.KIND_RAW_U8 if args.raw_to_stdout else hcap.KIND_POST_SYNC_CF32
    try:
        reader = open_capture(args.capture, kind)
    except CaptureRefused as err:
        print("ERROR: {}".format(err), file=sys.stderr)
        return 2
    except hcap.HcapError as err:
        print("ERROR: {}".format(err), file=sys.stderr)
        return 1
    except OSError as err:
        print("ERROR: {}".format(err), file=sys.stderr)
        return 1
    source = FrameSource(reader, realtime=args.realtime, loop=args.loop, restamp=args.restamp)
    logger.info("Replaying %s capture %s (%s frames)", kind, args.capture, reader.meta.get("frames"))

    responder = None
    if args.control_port:
        try:
            # raw mode feeds the chain like rtl_daq: DUMMY frames after each
            # command release delay_sync's post-retune waits
            responder = ControlResponder(args.control_port, source, args.control_address,
                                         NO_DUMMY_FRAMES if args.raw_to_stdout else 0)
        except ImportError:
            logger.error("pyzmq is not installed: control messages on port %d are not answered",
                         args.control_port)
        except Exception as err:  # zmq.ZMQError (zmq is an optional import)
            logger.error("Control responder on port %d unavailable: %s", args.control_port, err)
        else:
            responder.start()

    try:
        if args.raw_to_stdout:
            try:
                stream_raw(sys.stdout.buffer, source)
            except BrokenPipeError:
                logger.info("Reader closed the pipe after %d frames", source.sent)
                # keep the interpreter's final flush of stdout from raising again
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
                os.close(devnull)
        else:
            try:
                listen_sock = bind_server(args.serve_port, args.listen_address)
            except OSError as err:
                print("ERROR: cannot listen on {}:{:d}: {}".format(
                    args.listen_address, args.serve_port, err), file=sys.stderr)
                return 1
            logger.info("IQ Data TCP Server waiting for client on port %d", args.serve_port)
            with listen_sock:
                serve(listen_sock, source)
    except (ReplayStopped, KeyboardInterrupt):
        pass
    except (hcap.HcapError, OSError) as err:  # capture changed or became unreadable
        print("ERROR: {}".format(err), file=sys.stderr)
        return 1
    finally:
        source.stop()
        if responder is not None:
            responder.join(2.0)
    logger.info("Replay finished: %d frames sent", source.sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
