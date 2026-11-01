# Consuming the IQ Stream: Integration Guide

This guide gives the exact rules for attaching a downstream program (DoA, passive radar,
recorder, GNU Radio) to a running HeIMDALL chain, accepting frames, and detecting loss.
It covers the three outputs: TCP port 5000, the shared-memory ring, and the optional IQ
router on port 7000.

Every opt-in feature in this guide is off by default. The table below lists them and how to
enable each one. With none enabled, every interface behaves as described in the "default"
paragraphs.

| Opt-in feature | Enable with | Section |
|---|---|---|
| Several concurrent :5000 clients | `[data_interface] iq_server_max_clients = N` (2..64) | [Multi-client iq_server](#multi-client-iq_server-opt-in) |
| IQ router fan-out on :7000 | `./federation_start.sh router [--config PATH]` (or run `_daq_core/federation_iq_router.py` directly); sources need `out_data_iface_type = eth` | [Router output](#router-output-port-7000-opt-in) |
| STATUS drop counters on :5002 | `[monitoring] en_monitoring = 1` and `en_status_server = 1` | [Detecting loss](#detecting-loss-and-integrity-problems) |
| Node attribution (slot 108) | `[federation] stamp_node_id = 1` | [Reserved slots](#reserved-slots-102-113-telemetry-for-consumers) |
| Decimator drop counter (slot 109) | `[monitoring] stamp_decimator_drops = 1` | same |
| Calibration telemetry (slots 110-113) | `[monitoring] stamp_cal_telemetry = 1` | same |
| Clear sync flags after a USB overrun | `[calibration] invalidate_on_overrun = 1` | [Sync flags](#frame-types-sync-flags-and-the-acceptance-rule) |
| Header `unit_id` = instance id | `[hw] unit_id = auto` | [Multiple instances](#multiple-instances) |

Related references: [protocols.md](protocols.md) (wire reference),
[configuration.md](configuration.md) (every key), [scaling_and_clustering.md](scaling_and_clustering.md)
(multi-instance and multi-host), [capacity_planning.md](capacity_planning.md) (throughput budgets).

---

## Choose an interface

Pick the interface by where your program runs and how many readers you need.

| Interface | Select with | Readers | Where the reader runs | Handshake |
|---|---|---|---|---|
| TCP :5000 (`iq_server.out`) | `[data_interface] out_data_iface_type = eth` | 1 (default); up to 64 with `iq_server_max_clients` | any host that can reach the port | `streaming`, then one `IQDownload` per frame |
| Shared memory `delay_sync_iq` | `[data_interface] out_data_iface_type = shmem` | exactly 1 | same host, as root | FIFO byte protocol |
| IQ router :7000 (`federation_iq_router.py`) | `./federation_start.sh router` | up to `--max-clients` (8) | any host | none: connect and read |

Two facts decide most deployments:

* The shipped `Firmware/daq_chain_config.ini` sets `out_data_iface_type = shmem`. In that mode
  the start script launches no output process, and the chain waits until your consumer
  attaches. See [Shared-memory output](#shared-memory-output).
* When the key is absent, the start scripts fall back to `eth` and launch `iq_server.out`.

The router is itself a :5000 client. It reads each instance's `iq_server` and serves the
frames to many clients. Use it when several programs need the same stream, or when one
program needs the streams of several instances.

---

## Frame format

Every interface delivers the same frame: a 1024-byte header followed by the payload.

```
[1024-byte IQ header][payload]
payload_bytes = cpi_length * active_ant_chs * 2 * sample_bit_depth / 8
```

On all three outputs `sample_bit_depth` is 32 on every frame, because the decimator sets it.
The payload is therefore complex float32 (8 bytes per sample), so
`payload_bytes = cpi_length * active_ant_chs * 8`. DUMMY frames have `cpi_length = 0` and no
payload.

The payload is channel-major. It holds `active_ant_chs` consecutive blocks of `cpi_length`
samples, one block per channel, with I and Q interleaved inside each sample.

```python
import numpy as np
x = np.frombuffer(payload, dtype=np.complex64).reshape(hdr.active_ant_chs, hdr.cpi_length)
# x[m, n] = sample n of channel m
```

Sample values and corrections:

| Property | Value |
|---|---|
| Scaling | the u8 ADC samples are mapped to `(v - 127.5) / 127.5`, so raw values lie in [-1, 1] before filtering |
| DC | with `[calibration] en_iq_cal = 1` (shipped default) delay_sync subtracts each channel's mean per frame |
| Amplitude and phase | with `en_iq_cal = 1` each channel is multiplied by its current IQ correction, so channels are phase-aligned to the reference channel |
| `en_iq_cal = 0` | the payload is copied unchanged from the decimator |

Frame sizes at the shipped defaults (5 channels, `cpi_size = 1048576`, `decimation_ratio = 1`,
`corr_size = 65536`, `sample_rate = 2400000`):

| Frame | `cpi_length` | Bytes on the wire | Rate |
|---|---|---|---|
| DATA | 1048576 | 41,944,064 | one per 436.9 ms, 96.0 MB/s (768 Mbit/s) |
| CAL | 65536 (`corr_size`) | 2,622,464 | during calibration bursts |
| DUMMY | 0 | 1,024 | 5 after every control command |

One default :5000 client uses about 77 % of a 1 GbE link (768 of 1000 Mbit/s). Two remote
clients, or a router client of two sources, do not fit. Raise `decimation_ratio` or lower
`cpi_size`/`num_ch` for remote consumers; see [capacity_planning.md](capacity_planning.md).

---

## Header fields consumers use

Read `sampling_freq`, `cpi_length` and `active_ant_chs` from every frame. They change between
frame types and after reconfiguration.

The header uses the native C alignment of `iq_header.h` (padding at bytes 36 and 68) and
native byte order. Every supported platform (x86_64, aarch64, armv7) is little-endian. A
consumer on a big-endian host, or in a language without the C struct, must decode the bytes
below as little-endian. `header_version` stays **8**; do not reject frames on
`header_version != 7`.

| Offset | Type | Field | What a consumer needs to know |
|---|---|---|---|
| 0 | u32 | `sync_word` | `0x2bf7b95a`. Anything else means the stream is not frame-aligned: drop the connection |
| 4 | u32 | `frame_type` | 0 DATA, 1 DUMMY, 2 RAMP, 3 CAL, 4 TRIGW. Key all decisions on this field |
| 8 | char[16] | `hardware_id` | `[hw] name`, NUL-padded, truncated to 15 characters (for example `kraken5`) |
| 24 | u32 | `unit_id` | `[hw] unit_id` (default 0; `auto` = instance id). The router may overwrite it (see [Router output](#router-output-port-7000-opt-in)) |
| 28 | u32 | `active_ant_chs` | channel count M |
| 32 | u32 | `ioo_type` | illuminator type, passed through |
| 40 | u64 | `rf_center_freq` | Hz |
| 48 | u64 | `adc_sampling_freq` | Hz, tuner rate |
| 56 | u64 | `sampling_freq` | Hz of this payload. DATA: `adc_sampling_freq / decimation_ratio`. CAL: `adc_sampling_freq` (CAL frames bypass the FIR) |
| 64 | u32 | `cpi_length` | samples per channel. DATA: `cpi_size`. CAL: `corr_size`. DUMMY: 0 |
| 72 | u64 | `time_stamp` | Unix epoch **milliseconds** from `gettimeofday()` in rtl_daq when the block's USB transfers completed, back-dated by rebuffer by the duration of samples still queued beyond this CPI. Wall clock, not monotonic, no PPS discipline |
| 80 | u32 | `daq_block_index` | rtl_daq's USB block counter of the newest block in the CPI. It advances by `cpi_size * decimation_ratio / daq_buffer_size` per DATA frame (4 at defaults). It is not a frame counter |
| 84 | u32 | `cpi_index` | decimator frame counter, +1 per decimator input frame (DUMMY frames included). Use it for loss detection. Wraps at 2^32 |
| 88 | u64 | `ext_integration_cntr` | unused by RTL receivers |
| 96 | u32 | `data_type` | always 3 on every output frame (the decimator sets it). Values 0 and 1 exist only on the rtl_daq pipe. Do not use it to detect dummy frames |
| 100 | u32 | `sample_bit_depth` | always 32 on the outputs |
| 104 | u32 | `adc_overdrive_flags` | bit m set = channel m clipped during this CPI |
| 108 | u32[32] | `if_gains` | tuner gain per channel, tenths of dB |
| 236 | u32 | `delay_sync_flag` | 1 = sample-level synchronisation valid for this frame |
| 240 | u32 | `iq_sync_flag` | 1 = amplitude and phase calibration valid for this frame |
| 244 | u32 | `sync_state` | delay_sync FSM state, see below |
| 248 | u32 | `noise_source_state` | 1 = calibration noise source on |
| 252 | u32[192] | `reserved` | named slots; slot k is at byte `252 + 4*k` |
| 1020 | u32 | `header_version` | 8 |

Python: `IQHeader` in `Firmware/_daq_core/iq_header.py` decodes the header and asserts at
import that the native layout is 1024 bytes. C: include `Firmware/_daq_core/iq_header.h` and
cast the buffer to `struct iq_header_struct *`.

---

## Frame types, sync flags and the acceptance rule

Accept a frame for coherent processing only when all four conditions hold:

```python
def usable(hdr):
    return (hdr.frame_type == IQHeader.FRAME_TYPE_DATA   # 0
            and hdr.delay_sync_flag == 1
            and hdr.iq_sync_flag == 1
            and hdr.noise_source_state == 0)
```

Frame types as a consumer sees them:

| `frame_type` | Name | Payload | What to do |
|---|---|---|---|
| 0 | DATA | `cpi_size` samples at the decimated rate | process when the flags are 1 |
| 1 | DUMMY | none | skip. rtl_daq emits 5 after every control command it accepts (frequency, gain, AGC, noise source and the others). `sync_state` is 0 on these frames |
| 2 | RAMP | none in practice | defined in the header but never produced; rebuffer discards it. Skip it if seen |
| 3 | CAL | `corr_size` samples at the ADC rate, noise source on | skip unless you study calibration |
| 4 | TRIGW | none in practice | defined in the header but never produced; rebuffer discards it. Skip it if seen |

`sync_state` values:

| Value | delay_sync state | Flags |
|---|---|---|
| 0 | DUMMY frame (the state is not evaluated) | 0 |
| 1 | `STATE_INIT` | 0 |
| 2 | `STATE_SAMPLE_CAL` | `delay_sync_flag` may be 1 (all channel delays under one sample); `iq_sync_flag` 0 |
| 3 | `STATE_SYNC_WAIT`, `STATE_FRAC_SAMPLE_CAL`, `STATE_FRAC_SYNC_WAIT` | 0 |
| 4 | `STATE_IQ_CAL` | both may be 1 (no correction needed, or `en_iq_cal = 0`) |
| 5 | `STATE_TRACK_LOCK` | 1 |
| 6 | `STATE_TRACK` | 1 while tracking holds |

Key on the flags, not on `sync_state`. The flags can already be 1 in states 2 and 4, and
`sync_state` can be 6 on a frame whose flags are 0 (see the retune sequence below).

Rules worth knowing:

* With `[calibration] cal_track_mode = 2` (shipped default), delay_sync checks sync quality
  only on CAL frames. On DATA frames in `STATE_TRACK` both flags are 1 without a per-frame
  check. After `maximum_sync_fails` failed checks on the calibration bursts, the FSM returns
  to `STATE_INIT` and restarts calibration. The flags then follow the table above.
* A retune produces this sequence: 5 DUMMY frames, then one DATA frame with `sync_state` 6
  and flags 0 (delay_sync detects the frequency change on it), then DATA frames at the new
  `rf_center_freq` with flags 0 (`sync_state` 1..4), CAL frames during the calibration burst,
  and DATA frames with flags 1 once the chain locks again.
* With `[calibration] en_iq_cal = 0` and `cal_track_mode = 1`, delay_sync never sets
  `iq_sync_flag` in `STATE_TRACK`. In that configuration key on `delay_sync_flag` alone.
* Opt-in: `[calibration] invalidate_on_overrun = 1` clears both flags on the frame whose
  slot 102 (USB overrun count) increased, and on the next `frames_after_overrun` frames
  (default 1). Default 0 leaves the flags unchanged. <!-- verify-after: WP-22 -->
* Phase coherence holds between the channels of one frame of one instance. It never holds
  between instances or hosts. See [theory_coherent_acquisition.md](theory_coherent_acquisition.md).

---

## Reserved slots 102-113: telemetry for consumers

Slots 0-101 carry RF front-end and orientation telemetry (gains, noise figure, antenna
bearing, aggregate power). They are stamped only when `[amplification]`, `[antenna]` or
`[orientation]` is enabled. Their layout is in [protocols.md](protocols.md).

Slots 102-113 support loss detection and attribution. Each slot is a native u32. Every slot
except 102 stays 0 unless its opt-in is set.

| Slot | Bytes | Name (`IQH_RSV_*` / `RSV_*`) | Content | Populated when |
|---|---|---|---|---|
| 102 | 660-663 | `BUFFER_OVERRUN_CNT` | cumulative USB ring-buffer overrun events since start | always (rtl_daq) |
| 103 | 664-667 | `ROUTER_SEQ` | per-source forwarded-frame counter, first frame = 1, wraps | router `--stamp-seq` |
| 104 | 668-671 | `ROUTER_RX_TS_LO` | router receive time, Unix ms, low 32 bits | router `--stamp-rx-ts` |
| 105 | 672-675 | `ROUTER_RX_TS_HI` | router receive time, Unix ms, high 32 bits | router `--stamp-rx-ts` |
| 106 | 676-679 | `SOURCE_INSTANCE` | source `instance_id + 1`; 0 = untagged | router `--tag-mode node` |
| 107 | 680-683 | `SOURCE_KEY_CRC32` | `zlib.crc32(b"host:N")` of the source node key (host lower-cased) | router `--tag-mode node` |
| 108 | 684-687 | `NODE_ID_CRC32` | `zlib.crc32(node_id)` of `[federation] node_id` (default `<short hostname>:<instance_id>`) | `[federation] stamp_node_id = 1` |
| 109 | 688-691 | `DECIMATOR_DROP_CNT` | cumulative frames the decimator dropped because delay_sync was busy | `[monitoring] stamp_decimator_drops = 1` |
| 110 | 692-695 | `CAL_QUALITY_MDB` | calibration quality (minimum correlation-peak dynamic range), milli-dB, **signed**; NaN reads 0, out-of-range values saturate | `[monitoring] stamp_cal_telemetry = 1` |
| 111 | 696-699 | `IQ_COMP_CNT` | amplitude/phase compensations applied since start | `stamp_cal_telemetry = 1` |
| 112 | 700-703 | `SAMPLE_COMP_CNT` | sample-delay compensations applied since start | `stamp_cal_telemetry = 1` |
| 113 | 704-707 | `RECAL_GEN` | generation of the last wire recalibration (`RECL`); 0 = none | `stamp_cal_telemetry = 1` and `[calibration] en_wire_recal = 1`; counts `RECL` only |

<!-- verify-after: WP-04 -->
<!-- verify-after: WP-06 -->
<!-- verify-after: WP-09 -->

The counters wrap modulo 2^32. Compare successive values with modular arithmetic.

Python accessors on a decoded `IQHeader`:

```python
import zlib
from iq_header import IQHeader

h = IQHeader()
h.decode_header(buf[:1024])
h.get_buffer_overrun_cnt()           # slot 102
h.get_router_seq()                   # slot 103
h.get_router_rx_ts_ms()              # slots 104/105 combined, 0 = not stamped
h.get_source_instance()              # slot 106 raw value: instance_id + 1, 0 = untagged
h.get_source_key_crc32()             # slot 107
h.get_node_id_crc32()                # slot 108
h.get_decimator_drop_cnt()           # slot 109
h.get_cal_quality_mdb() / 1000.0     # slot 110 in dB (sign-extended)
h.get_iq_comp_cnt(), h.get_sample_comp_cnt(), h.get_recal_gen()

# match a frame to a node key
zlib.crc32(b"hosta:0") & 0xFFFFFFFF  # 3437068901 (0xccdd8265)
```

C consumers index the slots with the defines, for example
`hdr->reserved[IQH_RSV_NODE_ID_CRC32]`. Read slot 110 as
`(int32_t)hdr->reserved[IQH_RSV_CAL_QUALITY_MDB]`.

---

## TCP output: port 5000

`iq_server.out` serves the stream on `5000 + instance_id * port_stride`, bound to
`[daq] listen_address` (default `0.0.0.0`). It runs only when
`out_data_iface_type = eth`. The port has no authentication; restrict it with a firewall
(see [networking_and_time_sync.md](networking_and_time_sync.md)).

### Protocol

1. Connect, then send the 9 ASCII bytes `streaming` in one write, with no terminator.
2. Read one frame: 1024 header bytes, then `payload_bytes`.
3. Send the 10 ASCII bytes `IQDownload`, with no terminator. The server sends the next frame
   only after this ack.
4. Repeat from step 2.

The server compares each `recv()` with the exact string. Any other bytes end the session. So
does a coalesced double ack (`IQDownloadIQDownload`): send exactly one ack per frame, after
the frame has fully arrived.

Server timers:

| Timer | Value | Effect on the client |
|---|---|---|
| `streaming` wait | no timeout (single-client mode); 5 s (multi-client mode) | in single-client mode a client that connects and stays silent blocks every later client. Send `streaming` immediately |
| Send progress (`SO_SNDTIMEO`) | 2 s | if your receive window stays full for 2 s, the session closes |
| Per-frame send deadline | 10 s | a frame that takes longer than 10 s to deliver closes the session |
| Ack wait (`SO_RCVTIMEO`) | 5 s | `IQDownload` must arrive within 5 s after the frame was sent |

The server sets `TCP_NODELAY`. Set it on the client too, because the ack is a small write.

### Pacing and drops

The chain never waits for a :5000 client. delay_sync writes its output ring in drop mode:
when your client has not acked the previous frame, delay_sync drops the new one. You then
see a gap in `cpi_index`. With the status server enabled (opt-in, see the table at the top),
`:5002 STATUS` also reports `counters.dropped_frames_iq` and `pipeline_health` turns
`degraded`. The STATUS snapshot refreshes every `[monitoring] status_update_interval`
frames; 0 (default) means every `heartbeat_interval` frames (100, about 44 s at the
defaults). Set `status_update_interval = 1` for prompt counters.

To keep up, receive and ack in one thread and process in another. Hand frames to the worker
through a bounded queue and drop locally when it is full. Your receive loop must stay
faster than the frame period (`cpi_length / sampling_freq`).

### Stale first frames

Expect the first two frames of every session to be old. iq_server reads the ring only while
a client is connected. While no client is attached, delay_sync fills both ring buffers once
and then drops. A new session therefore starts with those two buffered frames, whose
`time_stamp` can be minutes old, followed by a `cpi_index` jump. Discard frames whose
`time_stamp` is older than a few frame periods, or skip the first two frames.

### Minimal Python client

This client implements the protocol above. It needs NumPy and `iq_header.py` from
`Firmware/_daq_core/`. The protocol handling was tested against an emulation of iq_server's
single-client loop.

Save it as `iq_client.py` and edit the `sys.path.insert` line to point at your
`Firmware/_daq_core` folder. The client is minimal: it waits forever for each frame
(`settimeout(None)`) and exits with a `ConnectionError` when the server closes the session.
A production client sets a read timeout and reconnects with backoff, as described in
[Reconnect and restart behaviour](#reconnect-and-restart-behaviour).

```python
#!/usr/bin/env python3
"""Minimal HeIMDALL :5000 consumer (streaming / IQDownload)."""
import socket
import sys
import time

import numpy as np

sys.path.insert(0, "/path/to/Firmware/_daq_core")  # folder that holds iq_header.py
from iq_header import IQHeader

HEADER_BYTES = 1024


def recv_exact(sock, n):
    """Read exactly n bytes. recv() == 0 means the server closed the session."""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        k = sock.recv_into(view[got:])
        if k == 0:
            raise ConnectionError("iq_server closed the connection")
        got += k
    return buf


def frames(host="127.0.0.1", port=5000):
    """Yield (header, samples) per frame. samples is None for header-only frames."""
    sock = socket.create_connection((host, port), timeout=10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(None)            # frames can be seconds apart
    sock.sendall(b"streaming")       # exact bytes, no newline
    try:
        while True:
            hdr = IQHeader()
            hdr.decode_header(bytes(recv_exact(sock, HEADER_BYTES)))
            if hdr.check_sync_word() != 0:
                raise ValueError("bad sync word: stream is not frame-aligned")
            nbytes = hdr.cpi_length * hdr.active_ant_chs * 2 * hdr.sample_bit_depth // 8
            payload = recv_exact(sock, nbytes) if nbytes else None
            sock.sendall(b"IQDownload")  # ack: the server sends the next frame now
            samples = None
            if payload is not None:
                samples = np.frombuffer(payload, dtype=np.complex64).reshape(
                    hdr.active_ant_chs, hdr.cpi_length)
            yield hdr, samples
    finally:
        sock.close()


def usable(hdr):
    """True for a calibrated, coherent DATA frame."""
    return (hdr.frame_type == IQHeader.FRAME_TYPE_DATA
            and hdr.delay_sync_flag == 1
            and hdr.iq_sync_flag == 1
            and hdr.noise_source_state == 0)


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    last_cpi = None
    for hdr, x in frames(host, port):
        if last_cpi is not None and hdr.cpi_index != (last_cpi + 1) & 0xFFFFFFFF:
            print("gap: %d frame(s) not delivered" % ((hdr.cpi_index - last_cpi - 1) & 0xFFFFFFFF))
        last_cpi = hdr.cpi_index
        age_ms = int(time.time() * 1000) - hdr.time_stamp
        print("cpi=%d type=%d sync_state=%d usable=%s fs=%d shape=%s age_ms=%d"
              % (hdr.cpi_index, hdr.frame_type, hdr.sync_state, usable(hdr),
                 hdr.sampling_freq, None if x is None else x.shape, age_ms))
```

Run it with `python3 iq_client.py <host> <port>`. For instance N use port
`5000 + N * port_stride`.

### Multi-client iq_server (opt-in)

<!-- verify-after: WP-04 -->

By default iq_server serves one client at a time. A second client's TCP connect succeeds,
but it waits in the listen backlog (5 deep) and receives nothing until the first session
ends.

Set `[data_interface] iq_server_max_clients` above 1 to serve several clients from one
instance:

```ini
[data_interface]
out_data_iface_type = eth
iq_server_max_clients = 4
; 0 = auto: 0.8 x frame period, at least 100 ms
iq_server_frame_deadline_ms = 0
```

| Key | Default | Range | Meaning |
|---|---|---|---|
| `iq_server_max_clients` | 1 | 1..64. ini_checker rejects larger values, and the start scripts then refuse to start. `iq_server.out` run without the checker clamps to 64 with a warning | 1 keeps the single-client loop unchanged |
| `iq_server_frame_deadline_ms` | 0 | ≥ 0 | multi-client mode only: time each session has to receive a frame and ack it. 0 = `0.8 * cpi_size * decimation_ratio / sample_rate`, floored at 100 ms (349 ms at defaults) |

Behaviour in multi-client mode:

* Each session uses the same protocol and receives the same bytes as a single client.
* A session joins at the next frame. Sessions beyond the cap wait in the backlog.
* A client must send `streaming` within 5 s of connecting.
* iq_server releases a frame only after every session acked it or the deadline passed.
  A session that misses the deadline is closed (`missed the ... ms frame deadline` in
  `iq_server.log`); the others continue.
* The slowest session sets the pace for all. A remote client must sustain 1.25 times the
  stream rate to meet the automatic deadline. Set `iq_server_frame_deadline_ms` explicitly
  for slow links, and accept that delay_sync then drops more frames for every client.

---

## Shared-memory output

With `out_data_iface_type = shmem`, your program reads delay_sync's output ring directly. It
runs on the same host, as the only reader, as root.

### Names

| Object | Instance 0 | Instance N > 0 |
|---|---|---|
| Segment A | `delay_sync_iq_A` (`/dev/shm/delay_sync_iq_A`) | `instN_delay_sync_iq_A` |
| Segment B | `delay_sync_iq_B` | `instN_delay_sync_iq_B` |
| Forward FIFO (producer to you) | `_data_control/fw_delay_sync_iq` | `_data_control/instN_fw_delay_sync_iq` |
| Backward FIFO (you to producer) | `_data_control/bw_delay_sync_iq` | `_data_control/instN_bw_delay_sync_iq` |

FIFO paths are relative to the `Firmware/` working copy that runs the chain. The start
script creates them with `mkfifo`. Each segment holds one frame and is
`1024 + max(cpi_size, corr_size) * num_ch * 8` bytes (41,944,064 at defaults). Read the actual
frame size from the header, not from the segment size.

### Attach sequence and byte protocol

1. Open the forward FIFO for reading.
2. Open the backward FIFO for writing.
3. Read one byte. It must be `10` (`INIT_READY`).
4. Map segments `_A` and `_B`.
5. Loop: read one byte from the forward FIFO.
   * `1`: segment A is full. `2`: segment B is full.
   * `255` (`TERMINATE`): the chain is stopping. Detach.
   * EOF: delay_sync exited without `TERMINATE`. Detach.
6. Parse the header and payload. Copy what you need.
7. Write the same byte (`1` or `2`) to the backward FIFO to hand the segment back.

Every signal is one raw byte. delay_sync overwrites a segment as soon as you release it, so
copy before step 7.

### Blocking and failure behaviour

| Situation | What happens |
|---|---|
| Chain started, no consumer yet | delay_sync blocks in its `open()` of the forward FIFO. It processes no frames, its :5002 status server does not start, it does not open the hw_controller ring, and the chain does not calibrate. The chain looks hung until you attach |
| Consumer slower than the frame period | delay_sync drops frames (it opens the backward FIFO non-blocking). You see `cpi_index` gaps; STATUS (opt-in) counts `dropped_frames_iq` |
| Consumer exits | delay_sync keeps running and logs `Backward FIFO EOF (consumer exited)`. All further output is lost |
| Consumer restarts | it cannot reattach: `INIT_READY` is sent only once, at delay_sync start, so the new consumer blocks in step 3. Restart the chain (`sudo ./daq_stop.sh`, then start again), then start the consumer |

Start the consumer right after the chain, or switch to `eth` when the consumer may restart
independently. Consumer reattach is not implemented.

Privileges: the chain runs under `sudo`, so the segments are owned by root with mode 0600
and the FIFOs are created by root. Run the consumer as root.

### Minimal Python client

This client reuses `inShmemIface` from `Firmware/_daq_core/shmemIface.py`, which implements
the consumer side of the protocol above. It was tested against `outShmemIface` in drop mode,
the producer class delay_sync uses. Save it as `shm_client.py`.

```python
#!/usr/bin/env python3
"""Minimal consumer of the delay_sync_iq shared-memory output."""
import os
import sys

import numpy as np

FIRMWARE = os.environ.get("HEIMDALL_FIRMWARE", "/home/krakenrf/heimdall_daq_fw/Firmware")
INSTANCE = int(os.environ.get("HEIMDALL_INSTANCE", "0"))

sys.path.insert(0, os.path.join(FIRMWARE, "_daq_core"))
os.chdir(FIRMWARE)  # the FIFOs are opened as _data_control/... relative to Firmware/

from iq_header import IQHeader
from shmemIface import inShmemIface, TERMINATE

iface = inShmemIface("delay_sync_iq", instance_id=INSTANCE)  # blocks until delay_sync opens its end
if not iface.init_ok:
    sys.exit("attach failed (is the chain running with out_data_iface_type = shmem?)")

hdr = IQHeader()
try:
    while True:
        idx = iface.wait_buff_free()  # despite the name: waits for a FILLED buffer
        if idx == TERMINATE:
            print("chain stopped")
            break
        if idx not in (0, 1):
            print("producer gone")
            break
        buf = iface.buffers[idx]
        hdr.decode_header(buf[:1024].tobytes())
        n = hdr.cpi_length * hdr.active_ant_chs
        x = None
        if n:
            # copy before releasing: delay_sync overwrites the buffer afterwards
            x = buf[1024:1024 + 8 * n].view(np.complex64).reshape(
                hdr.active_ant_chs, hdr.cpi_length).copy()
        iface.send_ctr_buff_ready(idx)  # hand the buffer back to delay_sync
        print("cpi=%d type=%d sync_state=%d shape=%s"
              % (hdr.cpi_index, hdr.frame_type, hdr.sync_state, None if x is None else x.shape))
finally:
    iface.destory_sm_buffer()  # (sic) the misspelling is the real method name
```

```bash
sudo env "PATH=$PATH" HEIMDALL_FIRMWARE=/home/krakenrf/heimdall_daq_fw/Firmware python3 shm_client.py
```

`env "PATH=$PATH"` keeps your interpreter under sudo. Plain `sudo python3` resolves to the
system Python, which usually lacks NumPy. The start scripts launch hw_controller the same way.

`multiprocessing.shared_memory` registers attached segments with the resource tracker by
default. On exit it prints `resource_tracker: There appear to be 2 leaked
shared_memory objects` and may unlink the segment names. This does not affect the running
chain, because a detached consumer must restart the chain anyway.

A C consumer follows the same steps with `Firmware/_daq_core/sh_mem_util.h`.
`iq_server.c` is the reference: it opens `delay_sync_iq` with
`transport_create("delay_sync_iq", size, false, FLOW_BACKPRESSURE, instance_id, TRANSPORT_SHM)`.

---

## Router output: port 7000 (opt-in)

<!-- verify-after: WP-09 -->

`Firmware/_daq_core/federation_iq_router.py` connects to one or more `iq_server` instances
(or upstream routers) and serves all their frames to many clients on one port.

Prerequisites:

1. On every source instance set `[data_interface] out_data_iface_type = eth` and restart that
   chain. The shipped config is `shmem`, which launches no `iq_server`, so the router's
   sources would stay in connecting/backoff.
2. Make sure port `5000 + N * port_stride` of each source is reachable from the router host.
3. If a local program also needs :5000 on a source instance, set `iq_server_max_clients` to
   2 or more there.
4. Start the router.

The DAQ start scripts do not launch the router. Start it with
`./federation_start.sh router [--config PATH]` from `Firmware/` (no sudo) and stop it with
`./federation_stop.sh`. The launcher logs to `_logs/federation/router.log` and writes
`_logs/federation/pids/router.pid`. `--config` defaults to `/etc/heimdall/federation.ini`
when present, else `./daq_chain_config.ini`. Launcher exit codes: 0 started, 1 the router
exited immediately, 2 usage error, 3 already running. For testing, run the script directly:
<!-- verify-after: WP-12 --> <!-- verify-after: WP-16 -->

```bash
cd Firmware
python3 _daq_core/federation_iq_router.py --sources hosta:0,hostb:0 --tag-mode node --stamp-seq
```

Source entries are `host:iid[@node_id]` (connects to `host:(5000 + iid * port_stride)`) or
`router://host:port` (an upstream router). `--config federation.ini` reads the
`[federation] router_*` keys instead; flags win over the file. Exit codes: 0 normal stop,
2 output or stats port bind failure, 3 bad arguments or config.

### Reading the router stream

Connect and read. There is no handshake and no ack. Frames use the format above and arrive
in arrival order, interleaved across sources. The router discards anything a client sends.

```python
import socket

def router_frames(host="127.0.0.1", port=7000):
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(None)
    try:
        while True:
            hdr = IQHeader()
            hdr.decode_header(bytes(recv_exact(sock, 1024)))   # recv_exact from the :5000 client
            if hdr.check_sync_word() != 0:
                raise ValueError("bad sync word")
            nbytes = hdr.cpi_length * hdr.active_ant_chs * 2 * hdr.sample_bit_depth // 8
            yield hdr, (recv_exact(sock, nbytes) if nbytes else b"")
    finally:
        sock.close()
```

### Telling sources apart

Demultiplex by the field your tag mode sets:

| `--tag-mode` | `unit_id` (bytes 24-27) | Slots | Demultiplex by |
|---|---|---|---|
| `instance` (default) | overwritten with the source `instance_id` | untouched | `unit_id`. Two hosts that both run instance 0 collide |
| `unit` | untouched (`[hw] unit_id`) | untouched | `unit_id` as configured on each unit |
| `node` | untouched | 106 = `instance_id + 1`, 107 = `crc32("host:N")` | slot 107, unique across hosts |

`--stamp-seq` adds slot 103 and `--stamp-rx-ts` adds slots 104/105 in any tag mode, for frames
read from iq_server sources. Frames from `router://` sources keep the upstream values. With
`stamp_node_id = 1` on the instances, slot 108 identifies the node even without the router.
`node` mode rejects loopback source hosts (exit 3): use the hosts' advertised names.

### Client-side behaviour

| Behaviour | Default | Flag |
|---|---|---|
| Frames queued per client | 4 | `--queue-frames` |
| Full queue | drop the oldest queued frame | `--drop-policy oldest\|newest\|disconnect` |
| Evict after consecutive drops | 50 (0 = never) | `--client-stall-limit` |
| Evict when no byte is accepted for | 2.0 s | `--send-timeout-sec` |
| Clients served; extra clients are closed at once | 8 | `--max-clients` |
| Connect to sources only while a client is attached | on | `--connect-on-demand` / `--always-connect` |
| Hold frames to release them in `time_stamp` order | 0 ms (off) | `--align-window-ms`, `--align-max-frames` |

A slow client loses frames from its own queue only. It never delays a source or another
client. A frame left half-written always evicts the client, so a connected client's stream
is always frame-aligned.

With connect-on-demand, the router holds no `iq_server` slot while it has no client. When
the first client connects, the router connects to each source, so the first frames per source
are the two stale frames described in [Stale first frames](#stale-first-frames). While the
router is attached, a single-client `iq_server` serves no other client. Enable
`iq_server_max_clients` on the instance if a local program also needs :5000.

### Stats port

The stats port (default 7001, `--stats-port 0` disables it) answers one command per
connection with one JSON object and `\n`. Verbs: `PING`, `VERSION`, `STATS`, `SOURCES`,
`CLIENTS`. With `--auth-token T`, prefix the command: `AUTH T STATS`.

```
$ printf 'VERSION\n' | nc 127.0.0.1 7001
{"ok": true, "component": "iq_router", "fw_version": "1.1.0-dev", "protocol": 1, "verbs": ["PING", "VERSION", "STATS", "SOURCES", "CLIENTS"]}
$ printf 'FOO\n' | nc 127.0.0.1 7001
{"ok": false, "error": "unknown_command", "valid": ["PING", "VERSION", "STATS", "SOURCES", "CLIENTS"]}
```

`STATS` reports per source `frames_lost`, `cpi_gaps`, `restarts`, `overruns`, `state`,
`clock_offset_ms`, `skew_warning`, and per client `queued`, `sent_frames`, `dropped_frames`,
`overflows`. `heimdall-ctl` formats it. The output below comes from a test router started
with `--sources 127.0.0.1:106 --output-port 17000 --stats-port 17001`:

```
$ heimdall-ctl --router 127.0.0.1:17001 router stats
uptime 1.1s  output :17000  tag_mode instance  clients 0  late_frames 0  unattributed 0
SOURCE         STATE  CONNECTED  FRAMES  LOST  GAPS  RESTARTS  LAST-AGE  OFFSET-MS
127.0.0.1:106  idle   no         0       0     0     0         -         None

CLIENT  CONNECTED  QUEUED  SENT  DROPPED
```

`idle` is the connect-on-demand state with no client attached. The full STATS document,
chaining with `router://` sources and the alignment rules are in
[protocols.md](protocols.md) and [scaling_and_clustering.md](scaling_and_clustering.md).

---

## Detecting loss and integrity problems

Check `cpi_index` continuity on every frame. A jump means frames were produced but not
delivered to you.

| Signal | Where | What it means |
|---|---|---|
| `cpi_index` gap | every frame | frames dropped after the decimator: by the decimator output, by delay_sync because your consumer (or iq_server) was busy, or in a router queue |
| `time_stamp` step larger than one frame period between consecutive DATA frames | every frame | samples were lost upstream: check slot 102 (USB overrun) on the same frame. Or DUMMY/CAL frames came between the DATA frames. (rebuffer drops CPIs only when started with argument 1, which the shipped scripts do not do) |
| slot 102 increased | every frame | rtl_daq's USB ring overran: samples were lost at the source. The frame content may be discontinuous |
| slot 109 increased | opt-in `stamp_decimator_drops = 1` | the decimator dropped frames because delay_sync was busy |
| `counters.dropped_frames_iq`, `pipeline_health` | `:5002 STATUS` (`heimdall-ctl status`), opt-in, see the table at the top | delay_sync output drops; refreshed every `status_update_interval` frames |
| `frames_lost`, `cpi_gaps`, `restarts`, `clients[].dropped_frames` | router `STATS` | router-side accounting per source and per client |
| `cpi_index` decreases | every frame | the source chain restarted |
| `sync_word` mismatch | every frame | your reader lost frame alignment. Reconnect |

Do not assume sample continuity between frames. Treat each DATA frame as one independent CPI.

---

## Reconnect and restart behaviour

| Event | What the consumer sees | What to do |
|---|---|---|
| Chain stops (`daq_stop.sh`) | :5000: EOF (`recv()` returns 0). shmem: byte `255`, or EOF | reconnect with backoff; for shmem, restart the consumer after the chain |
| iq_server closes the session (timeout, bad ack) | EOF or connection reset | reconnect; the listening socket stays open while iq_server runs |
| Chain restarts | connection refused until iq_server listens again | retry with backoff (for example 1 s doubling to 30 s) |
| Another client holds the single slot | connect succeeds, no frame arrives | wait, or enable `iq_server_max_clients`; avoid short first-frame timeouts that leave stale backlog connections |
| Retune or gain change | 5 DUMMY frames, then frames with flags 0 until relock | keep the connection; skip until `usable()` |
| Router source down | router keeps serving other sources; that source's `state` goes to `backoff` (3 s, doubling to 30 s) | nothing; watch `STATS` |
| Shared-memory consumer exits | cannot reattach | restart the chain |

Pick a read timeout no shorter than `max(5 s, 3 * cpi_length / sampling_freq)`. Large CPIs
can take several seconds per frame, and calibration bursts interrupt the DATA flow.

---

## Multiple instances

Each instance N on a host offsets its ports by `N * port_stride` (`[federation] port_stride`,
default 100) and prefixes its shared memory and FIFOs with `instN_`. `heimdall-ctl ports`
prints the map. This output was produced from `Firmware/` with the shipped config. The note
`disabled in config` marks services whose opt-in is off (`en_status_server = 0`); with no
config found the note is empty:

```
$ heimdall-ctl ports --instance 1
instance 1 (offset 100)
service                port  note
zmq (rtl_daq control)  1230
iq (iq_server)         5100
ctl (hw_controller)    5101
status                 5102  disabled in config
events                 5103  disabled in config
coordinator            6000  cluster service
router                 7000  cluster service
router stats           7001  cluster service
```

| Resource | Instance 0 | Instance N |
|---|---|---|
| IQ port | 5000 | `5000 + N * port_stride` |
| Status port | 5002 | `5002 + N * port_stride` |
| Shared memory | `delay_sync_iq_A/_B` | `instN_delay_sync_iq_A/_B` |
| FIFOs | `_data_control/fw_delay_sync_iq` | `_data_control/instN_fw_delay_sync_iq` |
| Router, coordinator | 7000/7001, 6000 | same; these are single cluster services, not offset |

With `port_stride` 100, use instance ids 0-9 per host. Ids 10 and 20 collide with the
coordinator (6000) and the router (7000/7001). See
[scaling_and_clustering.md section 4.3](scaling_and_clustering.md#43-port-table-and-the-safe-instance-id-range).

Identifying frames from several instances:

* On :5000 the port tells you the instance. The header `unit_id` is `[hw] unit_id`, which is
  0 on every instance unless you set it.
* `[hw] unit_id = auto` makes rtl_daq stamp `[federation] instance_id` into `unit_id`. Tools
  that parse the INI must read a non-integer `unit_id` as "equal to `instance_id`".
* Nothing makes instance ids differ across hosts, and two hosts often both run instance 0.
  For multi-host streams use slot 107
  (router `--tag-mode node`) or slot 108 (`[federation] stamp_node_id = 1`, with a unique
  `node_id` per instance).
* `time_stamp` is each host's wall clock. Compare it across hosts only when the hosts are
  NTP- or PTP-disciplined; see [networking_and_time_sync.md](networking_and_time_sync.md).
  Frames from different instances are never phase-coherent with each other.

How to run more than one instance per host, and its current limits, is in
[scaling_and_clustering.md](scaling_and_clustering.md).

---

## Recording and replay for testing consumers

Use recordings to test a consumer without hardware.

### Available today

`Firmware/_testing/iq_recorder.py` records frames from :5000 into `.iqf` files, one frame per
file (header, payload, then 262144 padding bytes). `Firmware/_testing/iq_analyzer.py` plots
them. Run the recorder from `Firmware/`.

By default (`--test-case phase_cont`) it records the first 200 DATA frames with
`delay_sync_flag == 1` and `daq_block_index > 300`. It then keeps the session open and keeps
acking frames. Write `2` to `_data_control/rec_ctr` (or press Ctrl-C) to stop it.
`--test-case calibration` records every CAL frame and ignores `--frames`.

```
$ python3 _testing/iq_recorder.py --help
usage: iq_recorder.py [-h] [--host HOST] [--port PORT] [--frames FRAMES]
                      [--min-block-index MIN_BLOCK_INDEX] [--prefix PREFIX]
                      [--test-case {decimator,calibration,phase_cont}]
                      [--ctrl-file CTRL_FILE] [--out-dir OUT_DIR]
```

`sudo ./daq_synthetic_start.sh` (Linux) runs the chain from synthetic data, so a consumer
can attach to a real :5000 or shared-memory output. See
[developer_guide.md](developer_guide.md).

### `.hcap` captures and replay (planned)

<!-- verify-after: WP-20 -->

The program adds a capture container and replay tools. This section describes their
specified behaviour.

The `.hcap` container is a concatenation of frames, each `[1024-byte header][exact payload]`,
with the payload length computed from the header
(`active_ant_chs * cpi_length * 2 * sample_bit_depth / 8`, 0 for DUMMY frames). A JSON
sidecar `<name>.hcap.json` holds:

| Key | Content |
|---|---|
| `format` | `"hcap-1"` |
| `kind` | `"post_sync_cf32"` (a :5000 capture) or `"raw_u8"` (rtl_daq output) |
| `created`, `host`, `instance_id` | capture metadata |
| `fw_version` | from `version.py` when importable |
| `config` | full INI text or null |
| `frames`, `notes` | frame count, free text |

Tools:

| Tool | Purpose |
|---|---|
| `_testing/iq_recorder.py --hcap PATH` | writes the container; `--instance N --port-stride S` selects port `5000 + N * S`; `--frames` applies in every trigger mode |
| `_testing/iq_replay.py --serve-port P FILE` | serves a `post_sync_cf32` capture with the real `streaming`/`IQDownload` protocol; `--realtime` keeps the recorded inter-frame timing, `--loop` repeats, `--listen-address` sets the bind address |
| `_testing/iq_replay.py --raw-to-stdout FILE` | emits a `raw_u8` capture for piping into `rebuffer.out` |
| `_testing/iq_latency_probe.py` | connects (`--host/--port` or `--instance/--port-stride`), reports frames, MB/s, latency (receive time minus `time_stamp`), `cpi_index` gaps and the slot-102 delta; `--duration`, `--json` |
| `daq_replay_start.sh` | runs the chain from a `raw_u8` capture; refuses other kinds with exit 2 |
| `_testing/iq_capture_format.py` | `HcapWriter(path, kind, meta)` and `HcapReader(path)` for your own tests |

A consumer test then looks like this:

```bash
python3 _testing/iq_replay.py --serve-port 15000 --realtime --loop capture.hcap &
python3 iq_client.py 127.0.0.1 15000
```

---

## GNU Radio and DoA client notes

### GNU Radio

The repository ships no GNU Radio block. Use the :5000 client above inside a Python block:

* Run `frames()` in a reader thread. Put only `usable()` frames into a bounded queue and
  drop when it is full, so the ack loop never stalls.
* Expose one complex output per channel. In `work()`, copy row m of the current frame to
  output m.
* Add stream tags at each frame start: `rx_freq` from `rf_center_freq`, `rx_rate` from
  `sampling_freq`, `rx_time` from `time_stamp / 1000`, and `cpi_index`. Downstream blocks can
  then reset on retunes and gaps.
* Expect a discontinuity at every frame boundary. Frames are separated by drops, DUMMY frames
  and calibration bursts.
* For several instances, read the router port and demultiplex by `unit_id` or slot 107 into
  separate block instances.

### DoA and passive radar

* Process only `usable()` frames. During calibration the flags are 0 and the payload is not
  phase-aligned.
* With `en_iq_cal = 1` the payload is already DC-removed and amplitude/phase corrected per
  channel. Do not apply a second correction from the same calibration.
* Read `rf_center_freq` and `sampling_freq` from each frame. A retune shows up as a new
  `rf_center_freq` after DUMMY frames and a recalibration.
* Watch `adc_overdrive_flags`. A set bit means that channel clipped during the CPI.
* For directional setups with `[orientation]` enabled, slots 98-100 carry the antenna azimuth,
  elevation and rotator state of the frame (`IQHeader.get_antenna_bearing()`,
  `get_rotator_state()`).
* Every recalibration (retune, sync loss, `INIT`, `RECL`) shows up as frames with flags 0,
  so `usable()` already excludes them. With `stamp_cal_telemetry = 1`, slot 110 gives the
  last calibration quality in milli-dB, and slots 111/112 increase on each compensation.
  Slot 113 additionally marks operator-requested `RECL` recalibrations; it needs
  `[calibration] en_wire_recal = 1` as well, and other recalibrations never change it.
* Combine instances only incoherently (per-instance bearings, per-instance range-Doppler
  maps). See [theory_coherent_acquisition.md](theory_coherent_acquisition.md).

The worked directional example is [tutorial_directional_df.md](tutorial_directional_df.md).
Deployment layouts with consumers are in [deployment_recipes.md](deployment_recipes.md), and
symptoms such as a chain that waits for its consumer are in
[operations_runbook.md](operations_runbook.md).
