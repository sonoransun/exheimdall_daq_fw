"""
    Description :
    The `.hcap` IQ capture container (format "hcap-1").

    A capture is the plain concatenation of frames
        [1024-byte IQ header][exact payload]
    where the payload length follows from the header alone:
        active_ant_chs * cpi_length * 2 * sample_bit_depth / 8
    and is 0 for DUMMY frames. No padding, no per-frame framing: the body is
    byte-for-byte what iq_server.out sends on :5000 (post_sync_cf32) or what
    rtl_daq.out writes into rebuffer.out's stdin (raw_u8).

    A JSON sidecar `<name>.hcap.json` describes the capture:
        {"format": "hcap-1", "kind": "post_sync_cf32" | "raw_u8",
         "created": ISO-8601 UTC, "host": hostname, "instance_id": int|null,
         "fw_version": str|null (version.py), "config": full INI text|null,
         "frames": int, "notes": str|null, "source": str|null}

    API:
        HcapWriter(path, kind, meta=None).write(header_bytes, payload) / .close()
        HcapReader(path) iterates (IQHeader, payload_bytes), checks every sync
        word and exposes .meta (the sidecar dict, None without a sidecar).

    CLI (from Firmware/):
        python3 _testing/iq_capture_format.py info CAPTURE [--json]
        python3 _testing/iq_capture_format.py check CAPTURE --kind KIND [--config INI]
        python3 _testing/iq_capture_format.py wrap --kind KIND [--tee] [--frames N]
            [--instance-id N] [--config INI] [--notes TEXT] INPUT|- OUTPUT.hcap
    `wrap` turns a raw frame stream (e.g. the synthesizer's or rtl_daq's stdout)
    into a capture; with --tee every frame is also copied to stdout, so it can
    sit inside a pipeline. `check` exits 2 when the capture cannot be replayed
    as KIND (with --config: against that INI's [hw] num_ch and, for raw_u8,
    [daq] daq_buffer_size).

    Project : HeIMDALL DAQ Firmware
    License : GNU GPL V3
"""
import argparse
import json
import os
import socket
import struct
import sys
import time
from configparser import ConfigParser

currentPath = os.path.dirname(os.path.realpath(__file__))
rootPath = os.path.dirname(currentPath)
sys.path.insert(0, os.path.join(rootPath, "_daq_core"))
from iq_header import IQHeader  # noqa: E402

FORMAT = "hcap-1"
KIND_POST_SYNC_CF32 = "post_sync_cf32"
KIND_RAW_U8 = "raw_u8"
KINDS = (KIND_POST_SYNC_CF32, KIND_RAW_U8)
# sample_bit_depth every non-dummy frame of a kind must carry
KIND_BIT_DEPTH = {KIND_POST_SYNC_CF32: 32, KIND_RAW_U8: 8}
HEADER_SIZE = 1024
# Byte offset of the uint32 cpi_length in the native-aligned header
CPI_LENGTH_OFFSET = struct.calcsize("II16sIIIQQQ")
SIDECAR_SUFFIX = ".json"
META_KEYS = ("created", "host", "instance_id", "fw_version", "config",
             "frames", "notes", "source")


class HcapError(Exception):
    """A malformed capture, sidecar or frame."""


def sidecar_path(path):
    """Path of the JSON sidecar of capture `path`."""
    return path + SIDECAR_SUFFIX


def fw_version():
    """FW_VERSION from _daq_core/version.py, None when not importable."""
    try:
        from version import FW_VERSION
    except ImportError:
        return None
    return FW_VERSION


def payload_length(header):
    """Exact payload bytes following `header` (an IQHeader): 0 for DUMMY frames."""
    if header.frame_type == IQHeader.FRAME_TYPE_DUMMY:
        return 0
    return wire_payload_length(header)


def wire_payload_length(header):
    """Payload bytes computed from the header fields alone, as the :5000
    clients compute them (iq_server sends cpi_length * active_ant_chs complex
    floats whatever the frame type; the chain zeroes cpi_length on DUMMY)."""
    bits = header.sample_bit_depth
    if bits % 8:
        raise HcapError("sample_bit_depth {:d} is not a whole number of bytes".format(bits))
    return header.active_ant_chs * header.cpi_length * 2 * (bits // 8)


def decode_header(header_bytes, frame_index=None, offset=None):
    """Decode 1024 header bytes into an IQHeader, checking the sync word."""
    if len(header_bytes) != HEADER_SIZE:
        raise HcapError("IQ header must be {:d} bytes, got {:d}".format(HEADER_SIZE, len(header_bytes)))
    where = ""
    if frame_index is not None:
        where = " in frame {:d}".format(frame_index)
        if offset is not None:
            where += " at byte offset {:d}".format(offset)
    # The sync word is checked on the raw bytes first: a desynchronised
    # stream must report a sync error, not a decode error of the text field
    sync_word = int.from_bytes(header_bytes[:4], sys.byteorder)
    if sync_word != IQHeader.SYNC_WORD:
        raise HcapError("bad IQ header sync word 0x{:08x}{} (expected 0x{:08x})".format(
            sync_word, where, IQHeader.SYNC_WORD))
    header = IQHeader()
    try:
        header.decode_header(header_bytes)
    except UnicodeDecodeError:
        raise HcapError("IQ header hardware_id is not valid UTF-8{}".format(where))
    return header


def build_meta(kind, meta=None):
    """Sidecar dict of a new capture: defaults overridden by `meta`."""
    if kind not in KINDS:
        raise ValueError("unknown capture kind {!r} (expected one of {})".format(kind, ", ".join(KINDS)))
    doc = {"format": FORMAT, "kind": kind,
           "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "host": socket.gethostname(), "instance_id": None,
           "fw_version": fw_version(), "config": None, "frames": 0,
           "notes": None, "source": None}
    for key, value in (meta or {}).items():
        if key not in META_KEYS or key == "frames":
            raise ValueError("unknown or read-only sidecar key {!r}".format(key))
        doc[key] = value
    return doc


def _write_json_atomic(path, doc):
    tmp = "{}.{:d}.tmp".format(path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def read_meta(path):
    """The sidecar of capture `path`, None when it has none."""
    side = sidecar_path(path)
    try:
        with open(side) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        raise HcapError("unreadable sidecar {}: {}".format(side, err))
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise HcapError("{} is not an {} sidecar".format(side, FORMAT))
    if doc.get("kind") not in KINDS:
        raise HcapError("{}: unknown capture kind {!r}".format(side, doc.get("kind")))
    return doc


class HcapWriter:
    """
        Writes a `.hcap` capture and its sidecar. The sidecar is written when
        the capture is opened (frames 0) and rewritten atomically by close()
        with the final frame count, so an interrupted recording still names
        its kind.

        :param path: capture path (conventionally ending in .hcap)
        :param kind: "post_sync_cf32" or "raw_u8"
        :param meta: optional sidecar overrides: created, host, instance_id,
                     fw_version, config, notes, source
    """

    def __init__(self, path, kind, meta=None):
        self.path = path
        self.kind = kind
        self.meta = build_meta(kind, meta)
        self.frames = 0
        self._file = open(path, "wb")
        try:
            _write_json_atomic(sidecar_path(path), self.meta)
        except OSError:
            self._file.close()
            raise

    def write(self, header_bytes, payload=b""):
        """Append one frame; returns its index. Raises HcapError when the
        header is malformed, the payload length disagrees with the header or
        the sample format does not match the capture kind. A DUMMY frame is
        stored header-only with its cpi_length zeroed."""
        if self._file is None:
            raise HcapError("capture {} is closed".format(self.path))
        header_bytes = bytes(header_bytes)
        header = decode_header(header_bytes, self.frames)
        if header.frame_type == IQHeader.FRAME_TYPE_DUMMY and header.cpi_length:
            # A DUMMY frame is stored header-only; its cpi_length is zeroed as
            # rtl_daq does, so that replay consumers (rebuffer.out reads a
            # payload whenever cpi_length > 0, :5000 clients read
            # cpi_length-sized payloads) see a header consistent with it
            header_bytes = (header_bytes[:CPI_LENGTH_OFFSET] + bytes(4)
                            + header_bytes[CPI_LENGTH_OFFSET + 4:])
            header.cpi_length = 0
        expected = payload_length(header)
        if len(payload) != expected:
            raise HcapError("frame {:d}: payload is {:d} bytes, the header gives {:d}".format(
                self.frames, len(payload), expected))
        if expected and header.sample_bit_depth != KIND_BIT_DEPTH[self.kind]:
            raise HcapError("frame {:d}: sample_bit_depth {:d} does not match capture kind {}".format(
                self.frames, header.sample_bit_depth, self.kind))
        self._file.write(header_bytes)
        if expected:
            self._file.write(payload)
        self.frames += 1
        return self.frames - 1

    @property
    def closed(self):
        return self._file is None

    def close(self):
        if self._file is None:
            return
        self._file.close()
        self._file = None
        self.meta["frames"] = self.frames
        _write_json_atomic(sidecar_path(self.path), self.meta)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class HcapReader:
    """
        Iterates the frames of a capture as (IQHeader, payload_bytes).
        Every header's sync word is checked; a bad sync word or a truncated
        frame raises HcapError naming the frame index and byte offset.
        `.meta` is the sidecar (None when the capture has none), `.kind` its
        kind. Each iteration restarts at the first frame.
    """

    def __init__(self, path):
        self.path = path
        self.meta = read_meta(path)
        self.kind = self.meta["kind"] if self.meta is not None else None
        if not os.path.isfile(path):
            raise HcapError("capture {} not found".format(path))

    def __iter__(self):
        return self.frames()

    def frames(self, with_payload=True):
        """Yield (IQHeader, payload) per frame; with_payload=False skips the
        payloads and yields (IQHeader, None)."""
        for _, header, payload in self.raw_frames(with_payload):
            yield header, payload

    def raw_frames(self, with_payload=True):
        """Yield (header_bytes, IQHeader, payload) per frame: the header
        exactly as stored, for byte-identical replay."""
        with open(self.path, "rb") as f:
            end = os.fstat(f.fileno()).st_size
            index = 0
            offset = 0
            while offset < end:
                header_bytes = f.read(HEADER_SIZE)
                if len(header_bytes) < HEADER_SIZE:
                    raise HcapError("frame {:d} at byte offset {:d}: truncated header ({:d} bytes)".format(
                        index, offset, len(header_bytes)))
                header = decode_header(header_bytes, index, offset)
                size = payload_length(header)
                available = end - offset - HEADER_SIZE
                if available < size:
                    raise HcapError("frame {:d} at byte offset {:d}: truncated payload ({:d} of {:d} bytes)".format(
                        index, offset, available, size))
                if with_payload:
                    payload = f.read(size)
                else:
                    payload = None
                    f.seek(size, os.SEEK_CUR)
                yield header_bytes, header, payload
                index += 1
                offset += HEADER_SIZE + size


def read_stream_frame(stream):
    """Read one [header][payload] frame from a binary stream. Returns
    (header_bytes, IQHeader, payload) or None at a clean end of stream;
    raises HcapError on a truncated frame or a bad sync word."""
    header_bytes = _read_exact(stream, HEADER_SIZE)
    if header_bytes is None:
        return None
    header = decode_header(header_bytes)
    size = wire_payload_length(header)
    payload = _read_exact(stream, size) if size else b""
    if payload is None:
        raise HcapError("stream ended inside a {:d}-byte payload".format(size))
    return header_bytes, header, payload


def _read_exact(stream, size):
    chunks = []
    got = 0
    while got < size:
        chunk = stream.read(size - got)
        if not chunk:
            if got == 0:
                return None
            raise HcapError("stream ended after {:d} of {:d} bytes".format(got, size))
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def recv_exact(sock, size):
    """Receive exactly `size` bytes; ConnectionError when the peer closes."""
    buf = bytearray(size)
    view = memoryview(buf)
    while view:
        n = sock.recv_into(view, len(view))
        if n == 0:
            raise ConnectionError("connection closed by the peer after {:d} of {:d} bytes".format(
                size - len(view), size))
        view = view[n:]
    return bytes(buf)


def recv_wire_frame(sock):
    """Receive one :5000 frame (after an IQDownload request or the streaming
    handshake). Returns (header_bytes, IQHeader, payload)."""
    header_bytes = recv_exact(sock, HEADER_SIZE)
    header = decode_header(header_bytes)
    size = wire_payload_length(header)
    payload = recv_exact(sock, size) if size else b""
    return header_bytes, header, payload


def config_text(path):
    """Full text of an INI file (None for None)."""
    if path is None:
        return None
    with open(path) as f:
        return f.read()


def check_capture(path, kind, config_path=None):
    """List of reasons why `path` cannot be replayed as `kind` (empty = ok)."""
    try:
        reader = HcapReader(path)
    except HcapError as err:
        return [str(err)]
    if reader.meta is None:
        return ["{} has no sidecar {} (capture kind unknown)".format(path, sidecar_path(path))]
    if reader.kind != kind:
        return ["{} is a {} capture, {} is required".format(path, reader.kind, kind)]
    num_ch = buffer_size = None
    if config_path is not None:
        parser = ConfigParser()
        if not parser.read(config_path):
            return ["cannot read config {}".format(config_path)]
        try:
            num_ch = parser.getint("hw", "num_ch", fallback=None)
        except ValueError:
            return ["config {}: [hw] num_ch is not an integer".format(config_path)]
        if kind == KIND_RAW_U8:
            try:
                buffer_size = parser.getint("daq", "daq_buffer_size", fallback=None)
            except ValueError:
                return ["config {}: [daq] daq_buffer_size is not an integer".format(config_path)]
    problems = []
    frames = 0
    try:
        for index, (header, _) in enumerate(reader.frames(with_payload=False)):
            frames += 1
            if header.frame_type == IQHeader.FRAME_TYPE_DUMMY or payload_length(header) == 0:
                continue
            if header.sample_bit_depth != KIND_BIT_DEPTH[kind]:
                problems.append("frame {:d}: sample_bit_depth {:d} is not {:d}".format(
                    index, header.sample_bit_depth, KIND_BIT_DEPTH[kind]))
            if num_ch is not None and header.active_ant_chs != num_ch:
                problems.append("frame {:d}: {:d} channels, the config has [hw] num_ch = {:d}".format(
                    index, header.active_ant_chs, num_ch))
            if buffer_size is not None and header.cpi_length != buffer_size:
                problems.append("frame {:d}: cpi_length {:d}, the config has [daq] daq_buffer_size = {:d}".format(
                    index, header.cpi_length, buffer_size))
            if problems:
                break
    except HcapError as err:
        problems.append(str(err))
    if not problems and frames == 0:
        problems.append("{} contains no frames".format(path))
    return problems


def capture_info(path):
    """Summary dict of a capture (sidecar without the INI text + frame stats)."""
    reader = HcapReader(path)
    stats = {"frames": 0, "data": 0, "cal": 0, "dummy": 0, "other": 0,
             "bytes": os.path.getsize(path), "channels": None, "cpi_length": None,
             "sampling_freq": None, "rf_center_freq": None,
             "first_time_stamp": None, "last_time_stamp": None,
             "first_cpi_index": None, "last_cpi_index": None}
    for header, _ in reader.frames(with_payload=False):
        stats["frames"] += 1
        key = {IQHeader.FRAME_TYPE_DATA: "data", IQHeader.FRAME_TYPE_CAL: "cal",
               IQHeader.FRAME_TYPE_DUMMY: "dummy"}.get(header.frame_type, "other")
        stats[key] += 1
        if stats["first_cpi_index"] is None:
            stats["first_cpi_index"] = header.cpi_index
            stats["first_time_stamp"] = header.time_stamp
        stats["last_cpi_index"] = header.cpi_index
        stats["last_time_stamp"] = header.time_stamp
        if header.frame_type != IQHeader.FRAME_TYPE_DUMMY:
            stats["channels"] = header.active_ant_chs
            stats["cpi_length"] = header.cpi_length
            stats["sampling_freq"] = header.sampling_freq
            stats["rf_center_freq"] = header.rf_center_freq
    meta = dict(reader.meta) if reader.meta is not None else None
    if meta is not None:
        meta["config"] = None if meta.get("config") is None else "<{:d} bytes>".format(len(meta["config"]))
    return {"path": path, "meta": meta, "stats": stats}


def wrap_stream(instream, output, kind, meta=None, tee=None, max_frames=0):
    """Record a raw frame stream into a capture, copying every frame to `tee`
    when given. max_frames > 0 closes the capture after that many frames; with
    a tee the stream keeps flowing to it until the input ends. Returns the
    number of frames recorded."""
    writer = HcapWriter(output, kind, meta)
    try:
        while True:
            frame = read_stream_frame(instream)
            if frame is None:
                break
            header_bytes, header, payload = frame
            if tee is not None:
                tee.write(header_bytes)
                tee.write(payload)
                tee.flush()
            if not writer.closed:
                writer.write(header_bytes, payload[:payload_length(header)])
                if max_frames and writer.frames >= max_frames:
                    writer.close()
                    if tee is None:
                        break
    finally:
        writer.close()
    return writer.frames


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect, check and create .hcap IQ captures")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_info = sub.add_parser("info", help="print the sidecar and frame statistics")
    p_info.add_argument("capture")
    p_info.add_argument("--json", action="store_true", help="print one JSON object")
    p_check = sub.add_parser("check", help="exit 2 unless the capture can be replayed as KIND")
    p_check.add_argument("capture")
    p_check.add_argument("--kind", required=True, choices=KINDS)
    p_check.add_argument("--config", help="INI to check num_ch / daq_buffer_size against")
    p_wrap = sub.add_parser("wrap", help="record a raw [header][payload] frame stream")
    p_wrap.add_argument("input", help="input stream file, - for stdin")
    p_wrap.add_argument("output", help="output .hcap path")
    p_wrap.add_argument("--kind", required=True, choices=KINDS)
    p_wrap.add_argument("--tee", action="store_true", help="also copy every frame to stdout")
    p_wrap.add_argument("--frames", type=int, default=0, help="stop recording after N frames (0 = all)")
    p_wrap.add_argument("--instance-id", type=int, default=None)
    p_wrap.add_argument("--config", help="INI whose full text goes into the sidecar")
    p_wrap.add_argument("--notes", default=None)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "info":
            info = capture_info(args.capture)
            if args.json:
                print(json.dumps(info, sort_keys=True))
            else:
                meta = info["meta"] or {}
                st = info["stats"]
                print("capture : {}".format(info["path"]))
                print("kind    : {}".format(meta.get("kind", "unknown (no sidecar)")))
                for key in ("created", "host", "instance_id", "fw_version", "source", "notes"):
                    if meta.get(key) is not None:
                        print("{:<8}: {}".format(key, meta[key]))
                print("frames  : {:d} (data {:d}, cal {:d}, dummy {:d}, other {:d}), {:d} bytes".format(
                    st["frames"], st["data"], st["cal"], st["dummy"], st["other"], st["bytes"]))
                if st["channels"] is not None:
                    print("format  : {:d} ch x {:d} samples, fs {:d} Hz, rf {:d} Hz".format(
                        st["channels"], st["cpi_length"], st["sampling_freq"], st["rf_center_freq"]))
                if st["frames"]:
                    print("cpi     : {:d} .. {:d}".format(st["first_cpi_index"], st["last_cpi_index"]))
            return 0
        if args.cmd == "check":
            problems = check_capture(args.capture, args.kind, args.config)
            for problem in problems:
                print("ERROR: {}".format(problem), file=sys.stderr)
            return 2 if problems else 0
        meta = {"instance_id": args.instance_id, "config": config_text(args.config),
                "notes": args.notes,
                "source": "stdin" if args.input == "-" else os.path.abspath(args.input)}
        instream = sys.stdin.buffer if args.input == "-" else open(args.input, "rb")
        tee = sys.stdout.buffer if args.tee else None
        try:
            frames = wrap_stream(instream, args.output, args.kind, meta, tee, max(0, args.frames))
        except BrokenPipeError:
            # The --tee reader went away: the capture is closed. Keep the
            # interpreter's final flush of the stdout buffer from raising again
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
            return 0
        finally:
            if instream is not sys.stdin.buffer:
                instream.close()
        print("{:d} frames written to {}".format(frames, args.output), file=sys.stderr)
        return 0
    except (HcapError, OSError) as err:
        print("ERROR: {}".format(err), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
