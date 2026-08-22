# Wire Protocols

This is the normative reference for every external interface of the DAQ
chain. All multi-byte integers are **little-endian** (native x86_64/ARM order)
unless stated otherwise.

## Port map

| Port | Transport | Protocol |
|---|---|---|
| 5000 | TCP | IQ frame streaming (stop-and-wait) |
| 5001 | TCP | 128-byte control frames (hw_controller) |
| 5002 | TCP | Line-command JSON status/metrics/DB endpoint |
| 5003 | ZMQ PUB | Event stream (multipart `[topic, json]`) |
| 1130 | ZMQ REQ/REP | Tuner control messages to rtl_daq |
| 6000 | TCP | Federation coordinator (text command, JSON reply) |
| 7000 | TCP | Federation IQ router unified output |

**Port formula:** every per-instance port is
`base_port + instance_id * port_stride` (`[federation]` `instance_id`,
`port_stride`, default stride 100; `compute_port()` in `sh_mem_util.c`).
Instance 0 always uses the base ports. The coordinator (6000) and router
output (7000) are single, not per-instance, services.

All TCP servers bind `0.0.0.0` by default; the optional `[daq]
listen_address` key rebinds the :5000 IQ server, the :5001 control server, the
:5002 status server, and the :5003 event PUB socket.

---

## IQ frames

Every frame, on every transport (stdout pipe, shared memory, TCP :5000/:7000,
net transport), is:

```
[1024-byte IQ header][payload]
payload_bytes = cpi_length × active_ant_chs × 2 × sample_bit_depth / 8
```

The payload is **channel-major**: `active_ant_chs` consecutive per-channel
blocks of `cpi_length` complex samples (I/Q interleaved within a sample).
After the decimator the sample format is complex float32 (8 bytes/sample);
out of rtl_daq it is interleaved uint8.

### Header v8 layout

Defined in lockstep in `iq_header.h` (C struct, **native alignment** — the
implicit padding is part of the wire format) and `iq_header.py`
(`HEADER_FORMAT`, native `struct` codec, import-time size assert). Exactly
1024 bytes. `_Static_assert`s freeze the size and the offsets of `reserved`
(252) and `header_version` (1020).

| Offset | Size | Type | Field | Notes |
|---|---|---|---|---|
| 0 | 4 | u32 | `sync_word` | `0x2bf7b95a` |
| 4 | 4 | u32 | `frame_type` | see frame types below |
| 8 | 16 | char[16] | `hardware_id` | NUL-padded, truncated at 16 |
| 24 | 4 | u32 | `unit_id` | federation IQ router overwrites with the source instance id |
| 28 | 4 | u32 | `active_ant_chs` | channel count M |
| 32 | 4 | u32 | `ioo_type` | illuminator type |
| 36 | 4 | — | *padding* | native u64 alignment |
| 40 | 8 | u64 | `rf_center_freq` | Hz |
| 48 | 8 | u64 | `adc_sampling_freq` | Hz |
| 56 | 8 | u64 | `sampling_freq` | Hz, post-decimation (set by decimator) |
| 64 | 4 | u32 | `cpi_length` | samples/channel; **0 on DUMMY and TRIGW frames (no payload)** |
| 68 | 4 | — | *padding* | |
| 72 | 8 | u64 | `time_stamp` | Unix epoch |
| 80 | 4 | u32 | `daq_block_index` | monotonically continuous per session |
| 84 | 4 | u32 | `cpi_index` | set by decimator |
| 88 | 8 | u64 | `ext_integration_cntr` | unused by RTL DAQs |
| 96 | 4 | u32 | `data_type` | 0 = dummy, 1 = raw u8 IQ (rtl_daq), 3 = decimated cf32 |
| 100 | 4 | u32 | `sample_bit_depth` | 8 out of rtl_daq, 32 after decimation |
| 104 | 4 | u32 | `adc_overdrive_flags` | per-channel bitmask, current frame |
| 108 | 128 | u32[32] | `if_gains` | actual tuner gains, tenths of dB |
| 236 | 4 | u32 | `delay_sync_flag` | |
| 240 | 4 | u32 | `iq_sync_flag` | |
| 244 | 4 | u32 | `sync_state` | 0 none, 1–4 calibrating, 5 track-lock, 6 track |
| 248 | 4 | u32 | `noise_source_state` | 1 = calibration noise on |
| 252 | 768 | u32[192] | `reserved` | v8 named slots below; rest zero |
| 1020 | 4 | u32 | `header_version` | **8** |

### v8 reserved-region slots

v8 names slots inside the previously-zero `reserved[192]` region. The struct
size is unchanged, so consumers that ignore `reserved[]` still parse v8
frames; only a consumer that hard-checks `header_version == 7` needs updating.
Every slot is a u32; signed quantities are stored two's-complement (the
Python getters sign-extend). Slot indices are `IQH_RSV_*` `#define`s in
`iq_header.h` and `RSV_*` constants with accessors in `iq_header.py`.

| Slot(s) | Name | Content | Stamped by |
|---|---|---|---|
| 0–31 | `EXT_LNA_GAINS` | per-channel external LNA gain, dB × 10 (signed) | delay_sync (gain budget) |
| 32–63 | `TOTAL_GAINS` | per-channel total system gain, dB × 10 (signed) | delay_sync |
| 64–95 | `SYSTEM_NF_MDB` | per-channel system noise figure, milli-dB (signed) | delay_sync |
| 96 | `COMPRESSION_FLAGS` | per-channel P1dB-headroom warning bitmask | delay_sync |
| 97 | `BIAS_TEE_STATE` | per-channel bias-tee bitmask (relayed via `_data_control/bias_tee_state`) | delay_sync |
| 98 | `ANTENNA_AZ_CDEG` | antenna azimuth, centi-degrees (0..36000) | delay_sync (relayed from hw_controller via `orientation_state`) |
| 99 | `ANTENNA_EL_CDEG` | antenna elevation, centi-degrees **with +9000 offset** (el_deg = value/100 − 90) | delay_sync |
| 100 | `ROTATOR_STATE` | orientation state enum: 0 IDLE, 1 SLEWING, 2 SETTLED, 3 SCANNING | delay_sync |
| 101 | `AGG_POWER_MDB` | aggregate channel power, milli-dB (signed; scan-and-peak objective) | delay_sync |
| 102 | `BUFFER_OVERRUN_CNT` | cumulative USB ring-buffer overrun events since start (unsigned; 0 = none/unsupported) | rtl_daq |

Slots 0–101 are stamped only when at least one of `[amplification]`,
`[antenna]`, `[orientation]` is enabled; otherwise they stay zero. Slot 102
is stamped unconditionally by rtl_daq.

### Frame types

| Value | Name | Semantics |
|---|---|---|
| 0 | `DATA` | Normal frame; the only type counted for scheduler dwell/settle |
| 1 | `DUMMY` | Emitted for 5 frames after every tuner command; `cpi_length = 0`, no payload; resets rebuffer state and drives the SYNC_WAIT → SAMPLE_CAL fast path |
| 2 | `RAMP` | Test ramp data |
| 3 | `CAL` | Noise-source calibration frame; **bypasses FIR decimation** — forwarded at full ADC rate as cf32 |
| 4 | `TRIGW` | Trigger-wait; `cpi_length = 0`, no payload |

---

## ZMQ tuner control — port 1130 (+ instance offset)

REQ/REP lockstep between hw_controller/delay_sync (REQ) and rtl_daq (REP).
Every message is **exactly 128 bytes**:

```
byte 0    int8   module identifier (signed; source module id)
byte 1    char   ASCII command
bytes 2+  payload (layout per command), zero-padded to 128
```

| Char | Command | Payload (from byte 2) |
|---|---|---|
| `r` | Reconfigure tuner | u32 center_freq_hz, u32 sample_rate_hz, u32 gain (12 bytes) |
| `c` | Center frequency tune | u32 center_freq_hz |
| `g` | Set gains | M × u32, tenths of dB (values from the R820T gain table) |
| `a` | Enable AGC | none |
| `s` | Sampling-frequency correction | M × float32 ppm offsets |
| `n` | Noise source control | 1 byte: 1 = on, 0 = off |
| `b` | Runtime bias-tee switch | M × u32 (0/1 per channel; GPIO m+1, deferred during a cal burst) |
| `h` | System halt | none |

The reply to every message is the 2-byte string `ok`. Every accepted command
makes rtl_daq emit `NO_DUMMY_FRAMES = 5` dummy frames while the tuners
settle. Packing helpers: `inter_module_messages.py` (`pack_msg_*`); parsing:
`rtl_daq.c`.

---

## TCP control interface — port 5001 (+ instance offset)

Served by `CtrIfaceServer` in `hw_controller.py`. Persistent connection,
strict framing:

- **Request:** exactly 128 bytes = 4-byte ASCII verb + 124-byte payload.
  Short verbs are space-padded (`"AGC "`, `"RFQ "` — trailing space is part of
  the verb).
- **Reply:** exactly 128 bytes starting `FNSD`.
  - Non-query commands: bytes 4–127 all zeros.
  - Query verbs (`RFQ `, `OQRY`, `SCHQ`): bytes 4–127 carry a NUL-padded
    UTF-8 JSON object.
- `EXIT` closes the connection with **no** reply.

Payload layouts (from the `CtrIfaceServer` docstring):

```
FREQ = uint64 LE @4, GAIN/EGAN/BIAS = M x uint32 LE @4,
ORNT = 2 x float32 LE @4, SCHD = UTF-8 path/JSON @4,
STHU = float32 @4 (accepted, currently not implemented),
AGC /INIT/SCHS/SCHQ/SCHN/RFQ /PARK/SCAN/OSTP/OQRY/EXIT = no payload.
```

| Verb | Action | Payload |
|---|---|---|
| `FREQ` | Retune center frequency | u64 Hz at offset 4 (range-checked to < 2^32) |
| `GAIN` | Set tuner IF gains (disables AGC) | M × u32 tenths-dB — must be valid R820T gain values |
| `AGC ` | Enable AGC | none |
| `INIT` | Re-run hardware init (re-enter STATE_INIT: restore gains, noise off) | none |
| `STHU` | Accepted for wire compatibility, logged no-op | float32 |
| `SCHD` | Load schedule | UTF-8: file path or compact JSON |
| `SCHS` | Stop/clear schedule | none |
| `SCHQ` | Query schedule (JSON reply) | none |
| `SCHN` | Skip to next schedule entry | none |
| `EGAN` | Set per-channel external LNA gains | M × u32 tenths-dB |
| `RFQ ` | Query link budget (JSON reply) | none |
| `BIAS` | Runtime bias-tee toggle (needs `en_bias_tee_runtime=1`) | M × u32 (0/1) |
| `ORNT` | Slew antenna to bearing | float32 az_deg, float32 el_deg |
| `PARK` | Park the antenna | none |
| `SCAN` | Start scan-and-peak | none |
| `OSTP` | Stop orientation motion | none |
| `OQRY` | Query orientation (JSON reply) | none |
| `EXIT` | Close connection | none |

Query verbs are answered in **any** FSM state; hardware-mutating commands are
executed only when the FSM is in the safe `STATE_IQ_CAL` state on a non-dummy
frame — otherwise they stay pending (order preserved) and retry next frame.

**Query reply shapes** (verbatim from `HWC._handle_query_request`):

```
RFQ  -> {"gains": [per-ch total system gain dB, 1 decimal],
         "nf": [per-ch system NF dB, 2 decimals],
         "comp": <compression bitmask int>}
OQRY -> {"az": <deg>, "el": <deg>, "state": <int enum
         0=IDLE 1=SLEWING 2=SETTLED 3=SCANNING>}
SCHQ -> {"state": <str>, "active": <bool>, "idx": <int>,
         "total": <int>, "freq": <int Hz>, "frames": <int>,
         "dwell": <int>} (only {"state","active"} without a schedule)
```

With orientation disabled `OQRY` returns `{"az":0.0,"el":0.0,"state":0}`;
with no scheduler `SCHQ` returns `{"state":"IDLE","active":false}`. If a
`RFQ ` reply would exceed 124 bytes, gains/NF fall back to integer rounding,
then to `{"error":"overflow"}`. When the request queue is full or the main
loop does not answer within 10 s, query verbs reply `FNSD` +
`{"error":"busy"}`; non-query verbs reply plain `FNSD` + zeros (a known
contract limitation: indistinguishable from success). Unknown verbs are acked
with `FNSD` + zeros and logged.

---

## TCP IQ streaming — port 5000 (+ instance offset)

Served by `iq_server.out`. Stop-and-wait protocol (relied on by downstream
DoA/passive-radar clients):

1. Client connects and sends the ASCII bytes `streaming` (no terminator).
2. Server sends one `[1024 B header][payload]` frame.
3. Server waits for the client's `IQDownload` bytes before sending the next
   frame; any other bytes end the session.
4. On chain shutdown the server closes the socket.

The listening socket stays open across client sessions (a new client can
connect after the previous one disconnects). One client at a time.
`TCP_NODELAY` is set server-side; payload is cf32 after decimation.

---

## TCP status endpoint — port 5002 (+ instance offset)

Served by `StatusServer` inside delay_sync. One request per connection:
client sends a text command line, server replies with **one JSON object
terminated by `\n`** and the client reads to the terminator (or EOF).

| Command | Reply |
|---|---|
| `PING` | `{"ok": true, "ts": <epoch float>}` |
| `STATUS` | Pipeline snapshot (see below) |
| `METRICS` | All metric stats from `MetricsCollector` (min/max/avg/p95 per metric) |
| `EVENTS` | `{"events": [<up to 100 recent event dicts>]}` |
| `EVENTS_DROPPED` | `{"dropped_events": <int>}` |
| `DB_STATS` | `daq_db.get_db_stats()` proxy |
| `SCAN_SUMMARY [rf_center_freq_hz]` | `{"freq_scan": [<records>]}` — all frequencies, or one |
| `CAL_HISTORY [rf_center_freq_hz]` | `{"cal_history": [<up to 100 records>]}` |

The three DB commands require `[database]` + `[monitoring]` both enabled;
otherwise they reply `{"error": "database not enabled"}`. Unknown commands
reply `{"error": "unknown command", "valid": [...]}`.

`STATUS` reply keys include: `timestamp`, `uptime_sec`, `instance_id`,
`sync_state`, `current_frequency_hz`, `pipeline_health`
(`ok` = sync ≥ 5 and zero drops in the recent window; `degraded` = sync ≥ 2;
`error` otherwise), `recent_drops`, `drop_window_sec` (windowed drop deltas,
window from `[monitoring] drop_window_sec`, default 60 s), `counters`
(cumulative, including `dropped_frames_iq`/`dropped_frames_hwc`), and — when
metrics are enabled — `latency` (`min/max/avg/p95_ms`) and `throughput`
(`min/max/avg_fps`).

---

## ZMQ event stream — port 5003 (+ instance offset)

`ZMQPubHandler` PUB socket. Each event is a multipart message:

```
frame 0: event_type (ASCII topic — subscribe-filterable)
frame 1: JSON event object (severity, module, event_type, payload, timestamp)
```

Event types (`daq_events.py`): `process_start`, `process_stop`, `sync_lock`,
`sync_lost`, `freq_change`, `gain_change`, `overdrive`, `cal_start`,
`cal_sample_done`, `cal_iq_done`, `cal_timeout`, `noise_source_on`,
`noise_source_off`, `schedule_loaded`, `schedule_transition`,
`schedule_complete`, `db_error`, `db_queue_full`, `frame_drop`, `heartbeat`,
`peer_up`, `peer_down`, `peer_degraded`, `coordinator_elected`,
`event_queue_full`, `ext_gain_change`, `compression`, `bias_tee_change`,
`orientation_slew`, `orientation_settled`, `orientation_scan_start`,
`orientation_scan_peak`, `orientation_park`, `orientation_limit`.

---

## Federation coordinator — port 6000

`federation_coordinator.py`. One text command per connection (whitespace
separated), JSON reply:

| Command | Action |
|---|---|
| `PING` | `{"ok": true, "ts": ...}` |
| `STATUS` | Aggregated STATUS snapshots from all instances' :5002 endpoints |
| `FREQ <hz>` | Fan out a `FREQ` control frame to every instance's :5001 |
| `GAIN <args>` | Fan out `GAIN` |
| `INSTANCE <id> <command>` | Send one command to a single instance |
| `REBALANCE` | Re-partition and redistribute the federation schedule (requires an attached `FederationScheduler`) |

The coordinator translates text commands into real 128-byte :5001 frames
(`_encode_hwc_command`) and parses the `FNSD`(+JSON) replies into
`{ok, data}`. Instance ports are derived with the port formula.

## Federation IQ router — port 7000

`federation_iq_router.py` connects to each instance's :5000 IQ server as a
normal `streaming`/`IQDownload` client, rewrites the header `unit_id`
(byte offset 24) to the source `instance_id`, and forwards the frames to every
client connected to its output port. Output clients simply connect and
receive `[header][payload]` frames as they arrive (no handshake, no per-frame
ack); demultiplex streams by `unit_id`.

---

## Net transport framing (optional `TRANSPORT_NET`)

`transport_net.c` (compiled only with `-DHAS_NET_TRANSPORT`) frames each
buffer on the TCP stream as:

```
[uint32 LEN, little-endian][LEN bytes: 1024-byte IQ header + payload]
```

`LEN` covers the header + populated payload (derived from the header's
`cpi_length`/`active_ant_chs`/`sample_bit_depth`; falls back to the full
buffer size when the sync word is absent). A **zero-length frame
(`LEN = 0`) is the terminate signal** in both directions; real frames are
always ≥ 1024 bytes so the encoding is unambiguous. Upgrade both ends of a
net-transport pair together.
