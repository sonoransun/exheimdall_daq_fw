# Wire Protocols

This is the normative reference for every external interface of the DAQ
chain: the IQ frame and header, the ZMQ tuner messages, the TCP control,
status, event and IQ ports, the shared-memory output and its FIFO
signalling, the federation coordinator and the IQ router.
All multi-byte integers are **little-endian** (native x86_64/ARM order)
unless stated otherwise.

Every addition described here is **opt-in**. Without a new configuration key
or a new verb, the per-instance endpoints :1130, :5000, :5001 (on a connection
that sends no `XACK`), :5002 and :5003 return the same bytes as before. The
coordinator on :6000 differs. Its legacy verbs keep their framing (no
trailing newline) and their old keys, but they add keys (`by_node`,
`summary`, `deadline_ms`, ...). The coordinator now opens every member
command with `XACK`. `REBALANCE` now needs a master schedule set with
`SCHEDULE SET`. The table below lists each opt-in and how to enable it.

| Feature | Endpoint | How to enable | Default |
|---|---|---|---|
| Extended acks | `:5001` | the client sends `XACK` first on its connection | off per connection |
| Snapshot queries (`HWSQ`, `SCHG`, `TLCK`), `TLKA`, chunked schedules (`SCHB`/`SCHC`/`SCHE`) | `:5001` | always served on any connection; `RECL` needs `en_wire_recal = 1` | n/a |
| Schedule verbs (`SCHD`, `SCHB`/`SCHE`, coordinator `REBALANCE`) | `:5001` | `[schedule] en_schedule = 1` on the member | `0` |
| Wire recalibration (`RECL`) | `:5001` | `[calibration] en_wire_recal = 1` | `0` |
| Concurrent control clients | `:5001` | `[daq] ctl_max_connections = n` (n > 1) | `1` |
| Multi-client IQ server | `:5000` | `[data_interface] iq_server_max_clients = n` (n > 1) | `1` |
| Extended status grammar | `:5002` | `[monitoring] status_extended = 1` | `0` |
| Event identity tag / topic prefix | `:5003` | `[monitoring] event_tag_identity = 1` / `event_topic_prefix = 1` | `0` |
| hw_controller events on `:5003` | `:5003`, `:5002 HWC` | `[monitoring] en_hwc_state_relay = 1` | `0` |
| Peer health events | `:5003` | `[federation] en_federation = 1` with a non-empty `peer_list` or `cluster_members` (or `register_with_coordinator = 1`), `[monitoring] en_monitoring = 1` and `en_zmq_pub = 1`, then `[federation] emit_peer_health_events = 1` | `0` |
| Header telemetry slots 103-113 | IQ header | router flags, `stamp_node_id`, `stamp_decimator_drops`, `stamp_cal_telemetry` | all `0` |
| Coordinator protocol v2 verbs | `:6000` | always served; legacy verbs keep their framing | n/a |
| Coordinator / router authentication | `:6000`, `:7001` | `[federation] auth_token` or `--auth-token` | empty (off) |
| Coordinator | `:6000` | start with `federation_start.sh coordinator` (or run `federation_coordinator.py`) | not started |
| IQ router | `:7000`, `:7001` | start with `federation_start.sh router` (or run `federation_iq_router.py`) | not started |

`auth_token` protects only :6000 and :7001. The router output :7000 and the
per-instance ports :1130 and :5000-:5003 have no authentication. Restrict
them with `listen_address`, `zmq_listen_address`, `--allow-cidrs` and a
firewall (see [networking_and_time_sync.md](networking_and_time_sync.md)).

The DAQ start scripts (`daq_start_sm.sh`, `daq_synthetic_start.sh`) do not
start the coordinator or the router. Start them from `Firmware/` with the
federation launcher and stop them with `federation_stop.sh`:

```
./federation_start.sh coordinator|router|all [--config PATH] [--foreground]
                      [--state-dir DIR] [--log-dir DIR] [--pid-dir DIR]
./federation_stop.sh coordinator|router|all [--pid-dir DIR]
```

| Launcher option | Default |
|---|---|
| `--config` | `/etc/heimdall/federation.ini` when present, else `./daq_chain_config.ini` |
| `--log-dir` | `$HEIMDALL_FED_LOG_DIR`, else `_logs/federation` (`coordinator.log`, `router.log`) |
| `--pid-dir` | `$HEIMDALL_FED_PID_DIR`, else `_logs/federation/pids` (`coordinator.pid`, `router.pid`) |
| `--state-dir` | `$HEIMDALL_FED_STATE_DIR`, else not passed (coordinator only) |
| `--foreground` | off; on, the launcher execs one service in its own process (for systemd) |

`federation_start.sh` exits 0 when started, 1 when the service exited
immediately, 2 on a usage error and 3 when the service already runs (its PID
file is live). `federation_stop.sh` sends SIGTERM, waits 5 s, then sends
SIGKILL, and exits 0 when done or 2 on a usage error. You can also run the
Python modules directly (commands in
[Federation coordinator](#federation-coordinator-port-6000) and
[Federation IQ router](#federation-iq-router-ports-7000-and-7001)).
<!-- verify-after: WP-12 --> <!-- verify-after: WP-16 -->

Related documents: [configuration.md](configuration.md) (every key),
[architecture.md](architecture.md) (processes and relay files),
[consumer_guide.md](consumer_guide.md) (writing an IQ client),
[networking_and_time_sync.md](networking_and_time_sync.md) (ports, bind
addresses, clocks), [scaling_and_clustering.md](scaling_and_clustering.md)
(multi-instance model), [operations_runbook.md](operations_runbook.md)
(symptoms and fixes).

---

## Port map

| Port | Transport | Protocol | Bind key |
|---|---|---|---|
| 5000 | TCP | IQ frame streaming (stop-and-wait) | `[daq] listen_address` |
| 5001 | TCP | 128-byte control frames (hw_controller) | `[daq] listen_address` |
| 5002 | TCP | Line-command JSON status/metrics/DB endpoint | `[daq] listen_address` |
| 5003 | ZMQ PUB | Event stream (multipart `[topic, json]`) | `[daq] listen_address` |
| 1130 | ZMQ REQ/REP | Tuner control messages to rtl_daq | `[daq] zmq_listen_address` |
| 6000 | TCP | Federation coordinator (text request, JSON reply) | `--listen-address`, else `[daq] listen_address` of `--config` |
| 7000 | TCP | Federation IQ router unified output | `--listen-address`, else `[daq] listen_address` of `--config` |
| 7001 | TCP | Federation IQ router stats (line command, JSON reply) | same as 7000 |

**Port formula.** Ports 5000-5003 and 1130 are per instance:
`effective_port = base_port + instance_id * port_stride`
(`[federation] instance_id`, `port_stride`, default stride 100;
`compute_port()` in `sh_mem_util.c`, `compute_ports()` in
`federation_common.py`). Instance 0 always uses the base ports.

The coordinator (6000, from `[federation] coordinator_port` or `--port`), the
router output (7000, `router_port` or `--output-port`) and the router stats
port (7001, `router_stats_port` or `--stats-port`) are single services. They
are **not** offset by instance. With stride 100, instance ids 10 and 20 land
on 6000 and 7000/7001; `ini_checker.py` reports this collision. See
[networking_and_time_sync.md](networking_and_time_sync.md) for the safe id
range per stride.

**Bind addresses.**

* `[daq] listen_address` (default `0.0.0.0`) sets the bind address of the
  :5000 IQ server, the :5001 control server, the :5002 status server and the
  :5003 event PUB socket.
* `[daq] zmq_listen_address` (default `*`, all interfaces) sets the bind host
  of rtl_daq's :1130 ZMQ REP socket. Accepted values: `*`, `0.0.0.0`,
  `127.0.0.1`, `localhost` (mapped to `127.0.0.1`). An empty value means the
  key is absent. rtl_daq does not read `listen_address`.
* The coordinator and the router use `--listen-address`. Without the flag
  they use `[daq] listen_address` from the `--config` file, else `0.0.0.0`.

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
After the decimator the sample format is complex float32 (8 bytes/sample).
Out of rtl_daq it is interleaved uint8.

### Header v8 layout

The header is defined in lockstep in `iq_header.h` (C struct, **native
alignment**; the implicit padding is part of the wire format) and
`iq_header.py` (`HEADER_FORMAT`, native `struct` codec, import-time size
assert). It is exactly 1024 bytes. `_Static_assert`s freeze the size and the
offsets of `reserved` (252) and `header_version` (1020).

| Offset | Size | Type | Field | Notes |
|---|---|---|---|---|
| 0 | 4 | u32 | `sync_word` | `0x2bf7b95a` |
| 4 | 4 | u32 | `frame_type` | see [Frame types](#frame-types) |
| 8 | 16 | char[16] | `hardware_id` | NUL-padded, truncated at 16 |
| 24 | 4 | u32 | `unit_id` | `[hw] unit_id`, or `instance_id` with `unit_id = auto`. The IQ router in its default `instance` tag mode overwrites it with the source `instance_id` |
| 28 | 4 | u32 | `active_ant_chs` | channel count M |
| 32 | 4 | u32 | `ioo_type` | illuminator type |
| 36 | 4 | n/a | *padding* | native u64 alignment |
| 40 | 8 | u64 | `rf_center_freq` | Hz |
| 48 | 8 | u64 | `adc_sampling_freq` | Hz |
| 56 | 8 | u64 | `sampling_freq` | Hz, post-decimation (set by the decimator) |
| 64 | 4 | u32 | `cpi_length` | samples/channel; **0 on DUMMY and TRIGW frames (no payload)** |
| 68 | 4 | n/a | *padding* | |
| 72 | 8 | u64 | `time_stamp` | Unix epoch in **milliseconds**. Wall clock from `gettimeofday` in rtl_daq when the block is emitted. rebuffer moves it back by the time span of the samples still buffered beyond the emitted CPI. Not monotonic, not PPS/GPS-disciplined |
| 80 | 4 | u32 | `daq_block_index` | rtl_daq block counter; see below |
| 84 | 4 | u32 | `cpi_index` | set by the decimator; +1 per frame the decimator reads, including frames it drops on output, so a gap at the consumer marks lost frames. Use it to detect loss |
| 88 | 8 | u64 | `ext_integration_cntr` | unused by RTL DAQs |
| 96 | 4 | u32 | `data_type` | set per hop; see below |
| 100 | 4 | u32 | `sample_bit_depth` | 8 out of rtl_daq, 32 after decimation |
| 104 | 4 | u32 | `adc_overdrive_flags` | per-channel bitmask, current frame |
| 108 | 128 | u32[32] | `if_gains` | actual tuner gains, tenths of dB |
| 236 | 4 | u32 | `delay_sync_flag` | |
| 240 | 4 | u32 | `iq_sync_flag` | |
| 244 | 4 | u32 | `sync_state` | 0 none, 1-4 calibrating, 5 track-lock, 6 track |
| 248 | 4 | u32 | `noise_source_state` | 1 = calibration noise on |
| 252 | 768 | u32[192] | `reserved` | named slots below; the rest is zero |
| 1020 | 4 | u32 | `header_version` | **8** |

**`daq_block_index`.** rtl_daq increments it by exactly 1 per block, so it
is continuous only on the rtl_daq to rebuffer hop. After rebuffer, each frame
carries the index of the last rtl_daq block in it. DATA frames step by
`(cpi_size × decimation_ratio) / daq_buffer_size` per frame (4 with the
defaults). CAL frames step by `cal_size / daq_buffer_size` and DUMMY frames
by 1. The value always increases, but a consumer must not expect consecutive
values. Detect lost frames with `cpi_index`.

**`data_type`.** rtl_daq writes `0` on dummy frames and `1` on raw u8 IQ
frames. The decimator overwrites it with `3` (decimated cf32) on every frame
it forwards, DUMMY frames included. Everything after the decimator therefore
carries `3`. Detect dummy frames with `frame_type` (and `cpi_length == 0`),
not with `data_type`.

### Reserved-region slots

Slot `k` sits at byte offset `252 + 4·k`. Every slot is a u32. Signed
quantities are stored two's-complement, and the Python getters sign-extend.
Slot indices are `IQH_RSV_*` `#define`s in `iq_header.h` and `RSV_*`
constants with accessors in `iq_header.py`; `test_iq_header_abi.py` checks
the lockstep field by field. The struct size and `header_version` (8) are
unchanged, so consumers that ignore `reserved[]` are unaffected. Only a
consumer that hard-checks `header_version == 7` needs updating.

**v8 front-end and orientation slots (0-102):**

| Slot(s) | Name | Content | Stamped by |
|---|---|---|---|
| 0-31 | `EXT_LNA_GAINS` | per-channel external LNA gain, dB × 10 (signed) | delay_sync (gain budget) |
| 32-63 | `TOTAL_GAINS` | per-channel total system gain, dB × 10 (signed) | delay_sync |
| 64-95 | `SYSTEM_NF_MDB` | per-channel system noise figure, milli-dB (signed) | delay_sync |
| 96 | `COMPRESSION_FLAGS` | per-channel P1dB-headroom warning bitmask | delay_sync |
| 97 | `BIAS_TEE_STATE` | per-channel bias-tee bitmask (relayed via `_data_control/bias_tee_state`) | delay_sync |
| 98 | `ANTENNA_AZ_CDEG` | antenna azimuth, centi-degrees (0..36000) | delay_sync (relayed from hw_controller via `orientation_state`) |
| 99 | `ANTENNA_EL_CDEG` | antenna elevation, centi-degrees **with +9000 offset** (el_deg = value/100 - 90) | delay_sync |
| 100 | `ROTATOR_STATE` | orientation state enum: 0 IDLE, 1 SLEWING, 2 SETTLED, 3 SCANNING | delay_sync |
| 101 | `AGG_POWER_MDB` | aggregate channel power, milli-dB (signed; scan-and-peak objective) | delay_sync |
| 102 | `BUFFER_OVERRUN_CNT` | cumulative USB ring-buffer overrun events since start (unsigned; 0 = none) | rtl_daq, every frame |

Slots 0-101 are stamped only when at least one of `[amplification]`,
`[antenna]`, `[orientation]` is enabled; otherwise they stay zero. Slot 102
is stamped unconditionally by rtl_daq.

**Scaling and telemetry slots (103-113, opt-in):** each slot is 0 unless its
own opt-in is set.

| Slot | Bytes | Name | Content | Stamped when |
|---|---|---|---|---|
| 103 | 664-667 | `ROUTER_SEQ` | per-source forwarded-frame counter (u32, wraps) | IQ router `--stamp-seq` |
| 104 | 668-671 | `ROUTER_RX_TS_LO` | router receive time, Unix ms, low 32 bits | IQ router `--stamp-rx-ts` |
| 105 | 672-675 | `ROUTER_RX_TS_HI` | router receive time, Unix ms, high 32 bits | IQ router `--stamp-rx-ts` |
| 106 | 676-679 | `SOURCE_INSTANCE` | source `instance_id + 1` (0 = untagged) | IQ router `--tag-mode node` |
| 107 | 680-683 | `SOURCE_KEY_CRC32` | `zlib.crc32(b"host:N") & 0xFFFFFFFF` of the source node key | IQ router `--tag-mode node` |
| 108 | 684-687 | `NODE_ID_CRC32` | `zlib.crc32(node_id.encode()) & 0xFFFFFFFF` of `[federation] node_id` | delay_sync, `[federation] stamp_node_id = 1` |
| 109 | 688-691 | `DECIMATOR_DROP_CNT` | cumulative decimator output frames dropped | decimate.out, `[monitoring] stamp_decimator_drops = 1` |
| 110 | 692-695 | `CAL_QUALITY_MDB` | calibration quality, milli-dB, **signed**; saturates at the int32 range; NaN reads as 0 | delay_sync, `[monitoring] stamp_cal_telemetry = 1` |
| 111 | 696-699 | `IQ_COMP_CNT` | IQ (amplitude/phase) compensation count | delay_sync, `stamp_cal_telemetry = 1` |
| 112 | 700-703 | `SAMPLE_COMP_CNT` | sample-delay compensation count | delay_sync, `stamp_cal_telemetry = 1` |
| 113 | 704-707 | `RECAL_GEN` | recalibration generation (see `RECL`) | delay_sync, `stamp_cal_telemetry = 1` |

The u32 counters wrap modulo 2^32. delay_sync carries slot 109 through from
its input header to its output header. Frames read directly from `:5000`
(no router) keep zeros in slots 103-107. Reading the slots in Python:

```python
from iq_header import IQHeader
h = IQHeader()
h.decode_header(buf[:1024])
h.get_node_id_crc32()              # slot 108
h.get_router_rx_ts_ms()            # slots 104/105 as one u64, 0 = not stamped
h.get_cal_quality_mdb() / 1000.0   # slot 110 in dB
```

In C, read `hdr->reserved[IQH_RSV_NODE_ID_CRC32]`, and read slot 110 as
`(int32_t)hdr->reserved[IQH_RSV_CAL_QUALITY_MDB]`.

### Frame types

| Value | Name | Semantics |
|---|---|---|
| 0 | `DATA` | Normal frame; the only type counted for scheduler dwell/settle |
| 1 | `DUMMY` | Emitted for 5 frames after every tuner command; `cpi_length = 0`, no payload; resets rebuffer state and drives the SYNC_WAIT to SAMPLE_CAL fast path |
| 2 | `RAMP` | Test ramp data |
| 3 | `CAL` | Noise-source calibration frame; **bypasses FIR decimation** and is forwarded at full ADC rate as cf32 |
| 4 | `TRIGW` | Trigger-wait; `cpi_length = 0`, no payload |

---

## ZMQ tuner control: port 1130 (+ instance offset)

REQ/REP lockstep between hw_controller/delay_sync (REQ) and rtl_daq (REP).
Every message is **exactly 128 bytes**:

```
byte 0    int8   module identifier (signed; source module id)
byte 1    char   ASCII command
bytes 2+  payload (layout per command), zero-padded to 128
```

| Char | Command | Payload (from byte 2) |
|---|---|---|
| `r` | Reconfigure tuner (**deprecated**) | u32 center_freq_hz, u32 sample_rate_hz, u32 gain (12 bytes) |
| `c` | Center frequency tune | u32 center_freq_hz |
| `g` | Set gains | M × u32, tenths of dB (values from the R820T gain table) |
| `a` | Enable AGC | none |
| `s` | Sampling-frequency correction | M × float32 ppm offsets |
| `n` | Noise source control | 1 byte: 1 = on, 0 = off |
| `b` | Runtime bias-tee switch | M × u32 (0/1 per channel; GPIO m+1, deferred during a cal burst) |
| `h` | System halt | none |

* **31-channel limit.** The 126 payload bytes hold at most 31 per-channel
  u32 or float32 values. rtl_daq reads at most 31 values for `g`, `s` and
  `b`. The `inter_module_messages.py` packers raise `ValueError` for more
  than 31 values, and `ini_checker.py` rejects `num_ch > 31`.
* **`r` is deprecated.** rtl_daq still applies it and logs
  `ZMQ 'r' reconfiguration is deprecated` once per process. No in-tree
  client sends it.
* The reply to every message is the 2-byte string `ok`. Every accepted
  command makes rtl_daq emit `NO_DUMMY_FRAMES = 5` dummy frames while the
  tuners settle.
* Packing helpers: `inter_module_messages.py` (`pack_msg_*`). Decoding
  helpers: `unpack_msg`, `unpack_u32_params`, `unpack_f32_params`. The C
  parser is in `rtl_daq.c`.
* In simulation mode the synthesizer (`_testing/test_data_synthesizer.py`)
  binds the same endpoint and answers the same messages with `ok`.

---

## TCP control interface: port 5001 (+ instance offset)

Served by `CtrIfaceServer` in `hw_controller.py`. The connection is
persistent and the framing is strict:

- **Request:** exactly 128 bytes = 4-byte ASCII verb + 124-byte payload.
  Short verbs are space-padded (`"AGC "`, `"RFQ "`; the trailing space is
  part of the verb).
- **Reply:** exactly 128 bytes starting `FNSD`. Bytes 4-127 are either all
  zeros or a NUL-padded compact UTF-8 JSON object (at most 124 bytes).
- `EXIT` closes the connection with **no** reply.

A connection that never sends `XACK` gets the same bytes as before for every
existing verb (the **legacy contract**). The new verbs in
[Extended control](#extended-control-xack) answer JSON on any connection.

### Legacy verbs

Payload layouts:

```
FREQ = uint64 LE @4, GAIN/EGAN/BIAS = M x uint32 LE @4,
ORNT = 2 x float32 LE @4, SCHD = UTF-8 path/JSON @4,
STHU = float32 @4 (accepted, currently not implemented),
AGC /INIT/SCHS/SCHQ/SCHN/RFQ /PARK/SCAN/OSTP/OQRY/EXIT = no payload.
```

`GAIN`, `EGAN` and `BIAS` carry M × u32 at offset 4, so the 124 payload bytes
also cap M at 31.

| Verb | Action | Payload |
|---|---|---|
| `FREQ` | Retune center frequency | u64 Hz at offset 4 (range-checked to < 2^32) |
| `GAIN` | Set tuner IF gains (disables AGC) | M × u32 tenths-dB; must be valid R820T gain values |
| `AGC ` | Enable AGC | none |
| `INIT` | Re-run hardware init (re-enter STATE_INIT: restore gains, noise off) | none |
| `STHU` | Accepted for wire compatibility, logged no-op | float32 |
| `SCHD` | Load schedule. Needs `[schedule] en_schedule = 1` on the member; otherwise a legacy connection gets `FNSD` + zeros and nothing loads, and an XACK connection gets `disabled`. hw_controller opens a path on the member host, relative to its working directory (`Firmware/` with the start scripts) | UTF-8: file path or compact JSON |
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

The legacy query verbs `RFQ `, `OQRY` and `SCHQ` (the set
`HWC.QUERY_COMMANDS`) are answered in **any** FSM state. Hardware-mutating commands run only when the
FSM is in the safe `STATE_IQ_CAL` state on a non-dummy frame. Otherwise they
stay pending (order preserved) and retry on the next frame. `INIT` and `RECL`
are always held while the calibration noise source is on.

**Legacy query reply shapes** (from `HWC._handle_query_request`):

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

With orientation disabled `OQRY` returns `{"az":0.0,"el":0.0,"state":0}`.
With no scheduler `SCHQ` returns `{"state":"IDLE","active":false}`. If an
`RFQ ` reply would exceed 124 bytes, gains/NF fall back to integer rounding,
then to `{"error":"overflow"}`.

When the request queue is full, or the main loop does not start the request
within `ctl_request_timeout_sec` (default 10 s), query verbs reply `FNSD` +
`{"error":"busy"}`. On a legacy connection non-query verbs then reply plain
`FNSD` + zeros, which a client cannot tell from success. Use `XACK` to get the
real outcome. Unknown verbs are acked with `FNSD` + zeros and logged as
`Unidentified control command: <verb>`.

### Extended control (XACK)

A client that sends `XACK` as the first frame of a connection switches that
connection to **extended acks**. Every mutating verb then replies with its
real outcome instead of zeros. The other new verbs work on any connection.

| Verb | Served by | Request payload (offsets from frame start) | Reply (bytes 4-127, NUL-padded JSON) |
|---|---|---|---|
| `XACK` | server thread, any state | `u32 term @4` (0 = none), `char cluster_id[32] @8` (at most 31 bytes + NUL; empty = no check), `u32 flags @40` (reserved, 0) | `{"xack":true,"v":1,"nch":M,"term":<max_term_seen>,"caps":"hwsq,schb,schg,tlck[,recl]","rto":<ms>,"fp":<ms>,"hmx":<ms>}`, or `{"xack":false,"error":"cluster_mismatch"\|"term_jump","term":<max_term_seen>}` |
| `HWSQ` | server thread, any state, never queued | none | control snapshot (table below) |
| `SCHG` | server thread, any state, never queued | none | `{"gen":<u32>\|null,"loaded":bool,"active":bool,"st":"IDLE"\|"ACTIVE_DWELLING"\|"ACTIVE_WAITING_CAL","idx":n,"total":n}` |
| `TLCK` | server thread | none | `{"req":0\|1,"appr":0\|1}`: `req` from `require_track_lock_intervention`, `appr` = first byte of the track-lock file is `1` (re-opened per request) |
| `TLKA` | server thread | `u32 value @4` (1 approve, 0 revoke) | `{"applied":true,"appr":v}` or `{"applied":false,"reason":"range"\|"stale_term"\|"error:<msg>"}`; on an XACK connection the extended ack plus `appr`. Writes `_data_control/<P>iq_track_lock` in place |
| `RECL` | queued; mutating (FSM-gated like INIT, held during a noise burst) | none | extended ack. Needs `[calibration] en_wire_recal = 1`, else `disabled` |
| `SCHB` | server thread | `u32 total_len @4` (1..262144), `u32 crc32 @8` (`zlib.crc32` of the full UTF-8 JSON), `u16 nchunks @12`, `u16 0 @14` | `{"ok":true,"sess":s}` or `{"ok":false,"reason":"too_large"\|"bad_request"}` |
| `SCHC` | server thread | `u16 sess @4`, `u16 seq @6` (0-based, strictly sequential), `u16 len @8` (1..116), `bytes data @10` | `{"ok":true,"seq":k}` or `{"ok":false,"reason":"bad_session"\|"out_of_order"\|"overflow","seq":k}`; any error aborts the session |
| `SCHE` | queued; mutating (the reassembled JSON is loaded like `SCHD`) | `u16 sess @4` | always JSON: `{"applied":bool,"reason":…,"entries":n,"fsm":…,"seq":n}`; reasons add `crc_mismatch`, `incomplete`, `bad_session` |

`<P>` is the per-instance file prefix: empty for instance 0, `inst<N>_` for
instance N > 0.

**XACK details.**

* `caps` contains `recl` only when `[calibration] en_wire_recal = 1`.
* `rto` = `ctl_request_timeout_sec` in ms (cap 600000). `hmx` =
  `ctl_handler_max_sec` in ms (cap 120000). `fp` = frame period in ms (cap
  999999). At the defaults: `rto` 10000, `hmx` 12000, `fp` 437.
* A non-empty `cluster_id` that differs from the member's
  `[federation] cluster_id` (default `default`) is refused with
  `cluster_mismatch`. A term above `max_term_seen + fence_max_term_jump`
  (default 1000) with a matching `cluster_id` is refused with `term_jump`.
  A refused connection is marked `cluster_bad`: every mutating verb on it
  replies `FNSD` + zeros and is **not executed**. Query verbs still work.
* Only an XACK with a non-empty matching `cluster_id` can raise the fence
  term. The new term is persisted atomically to `_data_control/<P>fence_term`
  and survives restarts. heimdall-ctl sends an empty `cluster_id` and never
  fences.

**HWSQ fields** (answered within milliseconds even while frames stall):

| Key | Meaning |
|---|---|
| `fsm` | hw_controller FSM state without `STATE_`: `INIT`, `GAIN_TUNE`, `GAIN_CTR_WAIT`, `IQ_CAL`, `NOISE_CTR_WAIT` |
| `pend` | deferred plus queued control requests, capped at 99 |
| `ns` | noise source on (0/1) |
| `agc` | AGC on (0/1) |
| `nch` | channel count M |
| `f` | current RF center frequency, Hz |
| `fage` | ms since the last frame, capped at 99999; -1 before the first frame |
| `fr` | 1 when `0 <= fage <= max(2000, 3 × fp)`, else 0 |
| `fc` | frames processed, modulo 10^6 |
| `t` | `max_term_seen` (fence term) |

**Extended ack.** On an XACK connection every mutating verb (`FREQ GAIN AGC 
INIT SCHD SCHS SCHN EGAN BIAS ORNT PARK SCAN OSTP STHU RECL TLKA SCHE`)
replies:

```
{"applied":true|false|null,"reason":"<r>","fsm":"<FSM name>","seq":<n>}
```

`seq` is a per-process counter. The reasons:

| `reason` | `applied` | When |
|---|---|---|
| `ok` | true | the handler completed and rtl_daq confirmed every transaction |
| `not_confirmed` | false | an rtl_daq ZMQ transaction timed out or failed; for `FREQ` the new frequency is not recorded |
| `held_for_burst` | null | `GAIN`/`AGC` while the noise source is on: stored and applied when the burst ends |
| `queue_full` | false | the request queue stayed full for 2 s |
| `deferred_timeout` | false | the FSM did not reach `IQ_CAL` within `ctl_request_timeout_sec`; the request was withdrawn and will never run |
| `handler_timeout` | null | the request was already running and did not finish within `ctl_handler_max_sec` more; outcome unknown |
| `parse_error` | false | `SCHD`/`SCHE` JSON or file could not be loaded, or the schedule is empty |
| `range` | false | value out of range, wrong count, or not a valid gain |
| `disabled` | false | the feature is off: `RECL` without `en_wire_recal`, schedule verbs without a scheduler, orientation verbs without `[orientation]`, `EGAN` without `[amplification]`, `BIAS` without `en_bias_tee_runtime` |
| `stale_term` | false | at execution time `0 < request term < max_term_seen` |
| `error:<msg>` | false (null for `RECL` when the `recal_request` write failed) | the handler raised; the text is truncated so the JSON fits 124 bytes |
| `unsupported` | false | `STHU` |

Two behaviours are kept from before. `EGAN` without `[amplification]` still
stores the values and emits its event, then reports `disabled`. `BIAS` that
rtl_daq does not confirm still rewrites the `bias_tee_state` relay file, then
reports `not_confirmed`.

Real replies captured from `CtrIfaceServer.process_ctr_frame` with no main
loop running (so the FSM is still `INIT` and `FREQ` times out):

```
XACK  -> FNSD{"xack":true,"v":1,"nch":5,"term":0,"caps":"hwsq,schb,schg,tlck","rto":10000,"fp":437,"hmx":12000}
SCHG  -> FNSD{"gen":null,"loaded":false,"active":false,"st":"IDLE","idx":0,"total":0}
TLCK  -> FNSD{"req":0,"appr":0}
FREQ  -> FNSD{"applied":false,"reason":"deferred_timeout","fsm":"INIT","seq":1}
STHU  -> FNSD{"applied":false,"reason":"unsupported","fsm":"INIT","seq":2}
XACK cluster_id="lab-b" -> FNSD{"xack":false,"error":"cluster_mismatch","term":0}
```

**Timing.** The server waits `ctl_request_timeout_sec` (T_srv) for the main
loop to **start** a request. A request still pending then is cancelled and
never runs. On an extended connection a request that is already running is
awaited for `ctl_handler_max_sec` (H) more. So a client should allow
`2 + T_srv + H + 2` seconds per mutating command on an extended connection
(26 s at the defaults) and `2 + T_srv + 2` seconds on a legacy connection
(14 s).

**Chunked schedules.** `SCHB`/`SCHC`/`SCHE` need `[schedule] en_schedule = 1`
on the member, like `SCHD`. A schedule larger than one `SCHD` frame is sent as
`SCHB`, then `SCHC` chunks of up to 116 bytes, then `SCHE`. Clients may send
up to 16 `SCHC` frames before reading replies; the server answers them
strictly in order. One session exists per connection, a new `SCHB` replaces
an unfinished one, a session expires after 30 s of inactivity, and the cap is
256 KiB. A schedule document with a top-level u32 `gen` sets `SCHG.gen`; a
document without `gen`, or `SCHS`, clears it. `SCHQ` bytes are unchanged.

**Connection keys** (`[daq]`, hw_controller; absent = previous behaviour).
`ini_checker.py` rejects an out-of-range value, which blocks startup through
the start scripts. hw_controller itself falls back to the default with a
WARNING.

| Key | Default | Range | Effect |
|---|---|---|---|
| `ctl_request_timeout_sec` | `10.0` | 1..600 | T_srv; advertised as `rto` |
| `ctl_handler_max_sec` | `12.0` | 1..120 | H; advertised as `hmx` |
| `ctl_max_connections` | `1` | 1..64 | `1` = one connection at a time, `listen(4)`. `n > 1` = up to n concurrent connections, `listen(16)`; requests still serialise through one queue |
| `ctl_xack_idle_sec` | `30` | 0..86400 (0 = off) | closes an idle XACK connection; enables TCP keepalive (10 s idle, 5 s interval, 3 probes) |
| `ctl_idle_timeout_sec` | `0` | 0..86400 (0 = off) | the same timeout and keepalive for every connection |

**Late execution of backlogged frames.** With `ctl_max_connections = 1` a
resident client (for example a DoA application) holds the only slot. A second
client's connection waits in the kernel backlog. A legacy client sends its
frame at once; if it gives up, the frame still sits in the backlog and runs
when the connection is finally served. Only the XACK-first handshake prevents
this: a client that sends `XACK`, waits for the reply, and closes without
sending the command when no reply arrives never leaves a mutating frame
behind. The coordinator always does this. heimdall-ctl does it unless the
target is cached as legacy or `--ack legacy` is given. A legacy
hw_controller logs `Unidentified control command: XACK` at error level for
every XACK; this line is expected in mixed-version clusters.

---

## TCP IQ streaming: port 5000 (+ instance offset)

:5000 exists only with `[data_interface] out_data_iface_type = eth`. The
shipped config uses `shmem`, so the start script runs no iq_server and
nothing listens on :5000 (see
[Shared-memory output](#shared-memory-output-and-fifo-signalling)).

Served by `iq_server.out`. Stop-and-wait protocol (relied on by downstream
DoA/passive-radar clients):

1. Client connects and sends the ASCII bytes `streaming` (no terminator).
2. Server sends one `[1024 B header][payload]` frame.
3. Server waits for the client's `IQDownload` bytes before sending the next
   frame; any other bytes end the session.
4. On chain shutdown the server closes the socket.

The listening socket stays open across client sessions (a new client can
connect after the previous one disconnects). `TCP_NODELAY` is set
server-side; the payload is cf32 after decimation. The server uses a 2 s send
timeout and a 5 s receive timeout per session, set after the handshake.
Each frame must be sent completely within 10 s, or the client is dropped.
In single-client mode the `streaming` handshake has no timeout. A connected
client that never sends it blocks the server until it disconnects.

**Default: one client at a time.** Further clients wait in the listen
backlog. While a client holds the slot, the IQ router and other clients
cannot read this instance.

**Opt-in multi-client mode** (`[data_interface] iq_server_max_clients = n`,
1..64, default 1). <!-- verify-after: WP-04 -->

* An accept thread performs the `streaming` handshake (5 s timeout) and adds
  sessions up to n. Further clients wait in the backlog.
* The server reads a frame only while at least one session exists. It hands
  the frame to every session, and each session sends it and reads its
  `IQDownload` ack exactly as in single-client mode.
* A session that does not finish within the frame deadline is closed.
  `[data_interface] iq_server_frame_deadline_ms` sets the deadline; `0`
  (default) means `0.8 × cpi_size × decimation_ratio / sample_rate`, at least
  100 ms.
* Each client receives byte for byte what a single client would receive.

---

## Shared-memory output and FIFO signalling

With `[data_interface] out_data_iface_type = shmem` (the shipped default),
delay_sync writes its output frames to a double-buffered shared-memory ring
on the same host. The internal pipeline stages use the same byte protocol
between each other. [consumer_guide.md](consumer_guide.md#shared-memory-output)
has the attach sequence, the failure behaviour and a Python client.

| Object | Instance 0 | Instance N > 0 |
|---|---|---|
| Segments | `delay_sync_iq_A`, `delay_sync_iq_B` (`/dev/shm/`) | `instN_delay_sync_iq_A`, `instN_delay_sync_iq_B` |
| Forward FIFO (producer to consumer) | `_data_control/fw_delay_sync_iq` | `_data_control/instN_fw_delay_sync_iq` |
| Backward FIFO (consumer to producer) | `_data_control/bw_delay_sync_iq` | `_data_control/instN_bw_delay_sync_iq` |

Each segment holds one `[1024-byte header][payload]` frame. Every signal is
one raw byte:

| Byte | Name | Direction | Meaning |
|---|---|---|---|
| `10` | `INIT_READY` | forward | producer initialized; sent once at producer start |
| `1` | `A_BUFF_READY` | forward, then backward | segment A is full; the consumer echoes it to release A |
| `2` | `B_BUFF_READY` | forward, then backward | segment B is full; the consumer echoes it to release B |
| `255` | `TERMINATE` | forward | the chain is stopping |

**Drop mode.** delay_sync opens the backward FIFO with `O_NONBLOCK`. When the
consumer has not released a segment, delay_sync drops the new frame instead
of waiting. The consumer sees a `cpi_index` gap.

### `.hcap` capture format (planned)

The `.hcap` recording container is not implemented in this tree yet. See
[consumer_guide.md](consumer_guide.md#hcap-captures-and-replay-planned) for
its planned layout. <!-- verify-after: WP-20 -->

---

## TCP status endpoint: port 5002 (+ instance offset)

Served by `StatusServer` (`daq_status_server.py`) inside delay_sync. It runs
when `[monitoring] en_monitoring = 1` and `en_status_server = 1`. One request
per connection: the client sends a text command line, the server replies with
**one JSON object terminated by `\n`**, and the client reads to the
terminator (or EOF).

| Command | Reply |
|---|---|
| `PING` | `{"ok": true, "ts": <epoch float>}` |
| `STATUS` | Pipeline snapshot (see below) |
| `METRICS` | All metric stats from `MetricsCollector` (min/max/avg/p95 per metric) |
| `EVENTS` | `{"events": [<up to 100 recent event dicts>]}` |
| `EVENTS_DROPPED` | `{"dropped_events": <int>}` |
| `DB_STATS` | `daq_db.get_db_stats()` proxy |
| `SCAN_SUMMARY [rf_center_freq_hz]` | `{"freq_scan": [<records>]}`: all frequencies, or one |
| `CAL_HISTORY [rf_center_freq_hz]` | `{"cal_history": [<up to 100 records>]}`; a bad argument replies `{"error": "usage: CAL_HISTORY [rf_center_freq_hz]"}` |

The three DB commands require `[database]` and `[monitoring]` both enabled;
otherwise they reply `{"error": "database not enabled"}`. Unknown commands
reply `{"error": "unknown command", "valid": [...]}`. At most 8 clients are
served at once; a ninth connection is closed without a reply.

**STATUS keys** (default mode):

| Key | Meaning |
|---|---|
| `timestamp`, `uptime_sec` | server wall time and uptime |
| `instance_id` | `[federation] instance_id` |
| `sync_state` | delay_sync sync state (0-6) |
| `current_frequency_hz` | RF center frequency of the last frame |
| `frame_count` | `cpi_index` of the last frame |
| `aggregate_power_db` | aggregate channel power, dB |
| `calibration_status` | `locked` (sync_state >= 5), `calibrating` (>= 2), else `lost` |
| `counters` | cumulative: `dropped_frames_iq`, `dropped_frames_hwc`, `sync_fails_total`, `sample_compensations`, `iq_compensations` |
| `recent_drops`, `drop_window_sec` | drops in the window of `[monitoring] drop_window_sec` (default 60 s) |
| `pipeline_health` | `ok` = sync_state >= 5 and zero recent drops; `degraded` = sync_state >= 2; `error` otherwise |
| `latency`, `throughput` | only with metrics enabled: `min/max/avg/p95_ms` and `min/max/avg_fps` |

The snapshot is refreshed with the heartbeat (`[monitoring]
heartbeat_interval`, default every 100 frames).
`[monitoring] status_update_interval = k` (frames, default 0 = follow the
heartbeat) refreshes it every k frames instead.

### Extended status (`status_extended = 1`)

`[monitoring] status_extended = 1` adds keys and verbs. With the key at `0`
none of this is served, and every reply is byte-identical to the default
mode. <!-- verify-after: WP-06 -->

**Additional STATUS keys:**

| Key | Source |
|---|---|
| `status_protocol` | constant `2`; its presence is the capability probe for the extended grammar |
| `node_id`, `cluster_id`, `hostname`, `pid` | config / process |
| `fw_version` | `version.FW_VERSION` |
| `ini_version`, `config_sha256` | `[meta] ini_version`; sha256 of the INI bytes read at start |
| `num_channels`, `sampling_freq_hz`, `cpi_length`, `decimation_ratio`, `frame_period_sec` | config (`frame_period_sec = cpi_size × decimation_ratio / sample_rate`) |
| `frame_type`, `cpi_index`, `last_frame_ts_ms` | last header |
| `fsm_state` | delay_sync state without `STATE_` |
| `cal` | `{quality_db, iq_compensations, sample_compensations, track_mode, recal_generation}` |
| `counters` (added) | `buffer_overrun_cnt` (slot 102), `cpi_gaps`, `daq_block_gaps`, `overrun_invalidated_frames`; `decimator_drops` (slot 109) with `stamp_decimator_drops = 1`; `hwc_relay_errors` with the relay on |
| `hwc` | the hw_controller relay document minus events, plus `age_sec` (only with `en_hwc_state_relay = 1`); `null` plus `hwc_error` when unreadable |
| `federation` | `{instance_id, node_id, cluster_id, health_mode, coordinator: {endpoint, role, term, registered, last_register_ok_age_sec, last_error}, advisory_leader, peers}`; only when delay_sync runs a FederationHealth monitor: `[federation] en_federation = 1` and a non-empty `peer_list` or `cluster_members`, or `register_with_coordinator = 1` |
| `snapshot_age_sec`, `stale_after_sec` | seconds since the last snapshot refresh, and the staleness threshold |

In extended mode:

* `pipeline_health` becomes `stale` when `snapshot_age_sec > stale_after_sec`.
  `[monitoring] stale_after_sec = 0` (default) means automatic:
  `3 × max(1, k) × frame_period_sec + 1.0`, with k the effective refresh
  interval in frames.
* The snapshot is also refreshed whenever `sync_state`, `frame_type` or
  `rf_center_freq` changes.
* At the client cap the server replies `{"error": "busy"}` before closing.
  `[monitoring] status_max_clients` (default 8, 1..1024) sets the cap, and
  the listen backlog is 16.

**Additional verbs:**

| Request | Reply |
|---|---|
| `PEERS` | `{"instance_id":N,"node_id":…,"advisory_leader":…,"peers":{addr:{row}}}` when a FederationHealth monitor runs (`[federation] en_federation = 1` and a non-empty `peer_list` or `cluster_members`, or `register_with_coordinator = 1`), else `{"error":"federation not enabled"}` |
| `HWC` | the relay document minus events, plus `age_sec`, or `{"error":"hwc relay not enabled"}` |
| `CAL_HISTORY <freq\|-> <since_ms> [limit]` | 2- and 3-argument form; `-` = any frequency; limit 1..1000 (default 100); bad request: `{"error":"usage: CAL_HISTORY [rf_center_freq_hz\|-] [since_ms] [limit]"}`. The 0- and 1-argument forms behave as in default mode |
| `HW_SNAPSHOTS [since_ms] [limit]` | `{"hw_snapshots":[…]}` or `{"error":"database not enabled"}`; bad request: `{"error":"usage: HW_SNAPSHOTS [since_ms] [limit]"}` |
| `DB_STATS` | the default object plus `db_dir` and `instance_id` |
| unknown verb | the default object, with `PEERS`, `HWC`, `HW_SNAPSHOTS` added to `valid` |

---

## ZMQ event stream: port 5003 (+ instance offset)

A `ZMQPubHandler` PUB socket on delay_sync's event bus. It runs when
`[monitoring] en_monitoring = 1` and `en_zmq_pub = 1` (port from
`zmq_pub_port`, default 5003). Each event is a multipart message:

```
frame 0: topic (ASCII, subscribe-filterable): event_type, or inst<N>.<event_type>
frame 1: JSON event object {timestamp, severity, module, event_type, payload[, source]}
```

* `[monitoring] event_topic_prefix = 1` publishes the topic as
  `inst<N>.<event_type>` (also `inst0.` for instance 0). It changes
  subscription filters, so it is a separate switch.
* `[monitoring] event_tag_identity = 1` adds
  `"source": {"instance_id", "node_id", "host", "cluster_id"}` to every
  event. Without it the `source` key is absent and the JSON is unchanged.

**What is published.** Only delay_sync publishes on :5003. Its stream
carries:

| Event types | Emitted by | Condition |
|---|---|---|
| `process_start`, `process_stop`, `sync_lock`, `sync_lost`, `freq_change`, `cal_start`, `heartbeat`, `frame_drop`, `event_queue_full` | delay_sync | always (with the bus on) |
| `cal_timeout` | delay_sync | `[calibration] cal_timeout_frames > 0` |
| `db_error`, `db_queue_full` | daq_db inside delay_sync | `[database]` enabled |
| `peer_up`, `peer_down`, `coordinator_elected` | FederationHealth inside delay_sync | `[federation] en_federation = 1` and a non-empty `peer_list` or `cluster_members`, or `register_with_coordinator = 1` |
| `peer_degraded`, `peer_recovered`, `peer_flapping` | FederationHealth | also `[federation] emit_peer_health_events = 1` (flapping needs `peer_flap_window_sec` and `peer_flap_threshold`) |
| hw_controller events (below), with `payload.relayed = true` | hw_controller, relayed by delay_sync | `[monitoring] en_hwc_state_relay = 1` |

hw_controller emits `process_start`, `process_stop`, `freq_change`,
`gain_change`, `overdrive`, `noise_source_on`, `noise_source_off`,
`schedule_transition`, `ext_gain_change`, `compression`, `bias_tee_change`,
`orientation_slew`, `orientation_settled`, `orientation_scan_start`,
`orientation_scan_peak`, `orientation_park`, `recal_requested` and
`cal_stalled` on its own bus. That bus has no PUB socket. These events reach
:5003 only through the relay: hw_controller writes them to
`_data_control/<P>hwc_state` (needs its own `en_monitoring = 1` for the bus),
and delay_sync re-emits new ones every 5 frames. More than 32 hw_controller
events between two reads are lost.

`daq_events.py` also defines `cal_sample_done`, `cal_iq_done`,
`schedule_loaded`, `schedule_complete`, `orientation_limit`,
`coordinator_command`, `node_key_collision` and `schedule_reconcile_failed`.
No module publishes the first six on :5003. The coordinator emits
`node_key_collision`, `schedule_overridden` and `schedule_reconcile_failed`
into its own event list, which its `EVENTS` verb returns.

---

## Federation coordinator: port 6000

`federation_coordinator.py` is one control endpoint for many instances. It
fans out commands to every member's :5001 and polls every member's :5002. It
is protocol version 2. <!-- verify-after: WP-08 -->

**Member prerequisites.** The shipped config enables none of these. Set them
on every member:

| Key | Needed for |
|---|---|
| `[monitoring] en_monitoring = 1` and `en_status_server = 1` | :5002 on the member. The coordinator learns member health only from its :5002 `STATUS` poll. Without it, legacy `STATUS` shows the member as an error, `HEALTH` reports it degraded, `NODES STATUS` and `PEERS` return an error entry for it, and `REBALANCE` finds no target (`{"error": "no healthy instances"}`) |
| `[schedule] en_schedule = 1` | `REBALANCE` and `SCHEDULE CLEAR`; without it the member answers `disabled` |
| `[daq] ctl_max_connections >= 2` | any member where a resident :5001 client (for example a DoA application) stays connected; otherwise coordinator commands wait in the backlog |

To rebalance without health polling, set `[federation] health_mode = off` on
the coordinator. `REBALANCE` then targets every member.

```
python3 _daq_core/federation_coordinator.py [--config federation.ini]
    [--port 6000] [--listen-address 0.0.0.0] [--instances host:id,...]
    [--self host:port] [--state-dir DIR] [--auth-token T]
    [--pid-file PATH] [--log-file PATH] [--log-level INFO]
```

Precedence: CLI flag, then the `--config` value, then the built-in default.
Static membership: `[federation] cluster_members` when it is non-empty, else
`--instances`, else `peer_list` (plus the coordinator's own instance with
`coordinator_include_self = 1`). Members added with `MEMBER ADD` or
`REGISTER` join at runtime. Exit codes: 0 normal stop; 2 bind failure; 3 bad
command-line arguments (usage errors), bad config, bad `--instances` value,
bad `--log-level` or unwritable `--log-file`. SIGTERM and SIGINT stop it
cleanly.

### Framing

One request per TCP connection; the server replies once and closes.

* **Request line.** The first read is up to 4096 bytes with a 5 s timeout.
  The line ends at the first `\n`. A chunk with no `\n` whose first word is a
  **legacy verb** (`PING`, `STATUS`, `FREQ`, `GAIN`, `INSTANCE`, `REBALANCE`)
  is the complete request, as before. Otherwise the server keeps reading
  until `\n`, EOF, a 2 s gap between chunks, 10 s in total, or 65536 bytes
  (`{"ok":false,"error":"line_too_long","limit":65536}`).
* **Body.** If the last token of the line is `@<n>` (1..1048576), exactly n
  body bytes follow the `\n`, read within 10 s
  (`{"ok":false,"error":"timeout"}`; above the limit
  `{"ok":false,"error":"body_too_large","limit":1048576}`). `REGISTER` and
  `SCHEDULE` also accept inline JSON on the line up to 3072 bytes; longer
  inline JSON gets `{"ok":false,"error":"body_too_large_use_@n","limit":3072}`.
* **Legacy verbs** reply one JSON object with **no trailing newline**, with
  the old keys plus additive keys. An unknown verb replies exactly
  `{"error": "Unknown command"}` (no newline).
* **New verbs** reply one JSON object followed by `\n`. Every reply has
  `"ok": true|false`; failures add `"error"` (snake_case) and optional
  `"message"` and `"usage"`. Mutating and fan-out replies carry
  `"deadline_ms"`.
* **Authentication** (opt-in). With `[federation] auth_token` (or
  `--auth-token`) set, every request must start with `AUTH <token> `
  (constant-time compare), else `{"ok":false,"error":"auth_required"}`.
  Without a token an `AUTH` prefix is stripped and ignored.
* **Peer filter** (opt-in). `[federation] allow_cidrs` closes connections
  from outside the listed networks without a reply.
* **Bounded handling.** A pool of `max_handlers` (64) threads serves
  requests. When it is full, `LEADER`, `PREVOTE`, `LEASE` and `REGISTER` go to
  a reserved pool of `reserved_handlers` (8); anything else gets
  `{"error": "busy", "retry_in_sec": 1}` (no newline) and is closed.
* A leader-only verb that cannot take the mutation lock within
  `mutation_wait_sec` (2 s) replies busy: `{"error":"busy","retry_in_sec":1}`
  for legacy verbs, `{"ok":false,"error":"busy","retry_in_sec":1}` for new
  ones.

```
$ printf 'STATUS' | nc 127.0.0.1 6000          # legacy: no newline needed
$ printf 'VERSION\n' | nc 127.0.0.1 6000
$ printf 'SCHEDULE SET round_robin @123\n{...123 bytes...}' | nc 127.0.0.1 6000
$ printf 'AUTH s3cret VERSION\n' | nc 127.0.0.1 6000   # with auth_token set
```

### Roles and leadership

Without `[federation] coordinator_candidates` the coordinator is `static`
(term 0) and answers every verb. With candidates it runs a lease election
(`federation_election.py`). A follower refuses leader-only verbs with
`{"ok":false,"error":"not_leader","leader":{…}}`. With `require_quorum = 1`
a leader without a majority of leases refuses them with
`{"ok":false,"error":"no_quorum"}`. Leader-only verbs: `FREQ`, `GAIN`,
`REBALANCE`, `INSTANCE`/`ALL`/`NODE`/`FANOUT` with a mutating verb, `RECAL`,
`MEMBER ADD/REMOVE`, `SCHEDULE SET/CLEAR/STRATEGY`. See
[scaling_and_clustering.md](scaling_and_clustering.md) for the election
model.

### Per-member execution and the result `R`

For every member command the coordinator opens one :5001 connection and
**always sends `XACK` first**, also to members cached as legacy:

* `{"xack":true,…}`: extended connection. `nch`, `caps`, `rto`, `hmx` are
  cached for `xack_cache_sec` (60 s).
* `{"xack":false,"error":…}`: the command is not sent;
  `R = {"ok":false,"applied":false,"reason":"cluster_mismatch"|"term_jump","phase":"xack"}`.
* `FNSD` + zeros: legacy member; the command is sent in legacy mode.
* No reply within `xack_timeout_sec` (3 s): not sent;
  `R = {"ok":false,"applied":false,"error":"timeout","phase":"xack"}`.

For `GAIN`/`EGAN`/`BIAS` on an extended member whose `nch` differs from the
value count, the frame is not sent:
`R = {"ok":false,"applied":false,"reason":"channel_count_mismatch","expected":nch,"got":k}`.

The per-member result `R` always carries `ok`. Every result except a
coordinator-side encoding error (for example `ALL FREQ abc` or a bad `GAIN`
list: `{"ok":false,"applied":false,"error":…,"phase":"connect"}`) also
carries `elapsed_ms`:

| Situation | `R` |
|---|---|
| extended ack | `{"ok":true,"applied":true\|false\|null,"reason":…,"fsm":…,"seq":…}` |
| legacy `FNSD` + zeros for a mutating verb | `{"ok":true,"applied":null,"legacy_ack":true}` |
| query verb | `{"ok":true,"data":{…}}`; a busy query `{"ok":false,"error":"busy","data":{…}}` |
| no reply after the command was sent | `{"ok":false,"applied":null,"error":"timeout","phase":"command"}` |
| connect refused, DNS failure, reset | `{"ok":false,"applied":false\|null,"error":…,"error_kind":"refused"\|"dns"\|"reset"\|"bad_reply","phase":…}` |
| circuit breaker open (3 consecutive failures, held 10 s) | `{"ok":false,"applied":false,"reason":"breaker_open","retry_in_sec":t}` |
| member needs more time than the deadline leaves | `{"ok":false,"applied":false,"reason":"rto_exceeds_deadline","rto_ms":…,"remaining_ms":…}` |
| extended-only verb (`RECL HWSQ TLCK TLKA SCHG`) on a legacy member | `{"ok":false,"applied":false,"reason":"unsupported_by_member"}` |

`applied:false` means the command did not take effect. Either it was never
sent (phase `connect`, `xack` or `chunks`, or a coordinator-side reason such
as `breaker_open` or `channel_count_mismatch`), or the member received it and
reported it not applied (its extended-ack reason, for example `range` or
`not_confirmed`). `applied:null` means it was sent and the outcome is
unknown. `summary` counts `applied` (true),
`not_applied` (false), `unconfirmed` (null with `legacy_ack` or
`held_for_burst`), `timeout` and `error`.

**Deadlines.** Each request gets a deadline fixed at receipt and never
extended. `VERSION.deadlines` advertises it, so clients set their timeout
above it.

| Kind | Formula | Default |
|---|---|---|
| query | `peer_connect_timeout_sec + status_timeout_sec + 1` | 6 s |
| mutation | `mutation_wait_sec + peer_connect_timeout_sec + xack_timeout_sec + max_member_rto_sec + 2 + 1` | 40 s |
| schedule (`REBALANCE`) | mutation + `T_chunks_max` + `max_member_rto_sec` + 2 | 73 s with no master schedule |

A member needs `2 + rto + hmx` seconds per mutation (its advertised
`ctl_request_timeout_sec` and `ctl_handler_max_sec`). When that exceeds
`[federation] max_member_rto_sec` (default 30), every mutation to it is
refused with `rto_exceeds_deadline`. Raise `max_member_rto_sec` on the
coordinator to at least that value for the slowest member. The mutation
deadline grows with it.

### Legacy verbs

| Request | Reply (no trailing newline) |
|---|---|
| `PING` | `{"ok": true, "ts": <float>}` |
| `STATUS [FRESH]` | `federation_health`, `instance_count`, `healthy_count`, `instances` (keyed by `str(instance_id)`, each the member's :5002 STATUS or an error), `timestamp`; additive: `unreachable_count`, `unreachable`, `degraded`, `stale`, `cached`, `poll_duration_ms`, `role`, `term`, `node_keys`, `abandoned_workers`, `deadline_ms`, and `key_collisions` when present. Polled live unless `status_cache_sec > 0`; `FRESH` always polls |
| `FREQ <hz>` / `GAIN <v,…>` | `{"command":…,"results":{str(iid):R},"by_node":{node_key:R},"summary":{…},"deadline_ms":d}` |
| `INSTANCE <id> <VERB> [args]` | `R` for that member, plus `node_key`. Errors: `{"error":"Usage: INSTANCE <id> <command>"}`, `{"error":"Instance <id> not found"}`, and with `cluster_members` `{"error":"ambiguous instance_id","candidates":[…]}` |
| `REBALANCE [round_robin\|range] [INCLUDE_DEGRADED]` | see below |

STATUS is aggregated by polling each member's status server
(`5002 + instance_id × port_stride`). FREQ and GAIN are sent to every
member's :5001. `INSTANCE` accepts every verb that `ALL` accepts.

**REBALANCE** partitions the master schedule (set with `SCHEDULE SET`) across
the members in health state `up` (plus `degraded` with `INCLUDE_DEGRADED`;
all members when `health_mode = off`). It sends each member its share and
sends `SCHS` to every other reachable member. Transfer per member: inline
`SCHD` when the JSON fits 124 bytes, else chunked `SCHB/SCHC/SCHE` when the
member advertises `schb`, else a temp-file path for a same-host legacy
member, else `schedule_too_large_for_legacy_server`. Each load is verified
with `SCHG` (extended) or `SCHQ` (legacy). The stand-alone coordinator
attaches a `FederationScheduler` at start.

| Condition | Reply |
|---|---|
| bad argument | `{"error": "Usage: REBALANCE [round_robin\|range] [INCLUDE_DEGRADED]"}` |
| no scheduler attached (library use only) | `{"error": "no federation scheduler configured"}` |
| no master schedule | `{"error": "no master schedule"}` |
| the leader has not polled every member yet | `{"error": "health_warming", "retry_in_sec": s}` |
| no member in a target state | `{"error": "no healthy instances"}` |
| replication to the other candidates failed | `{"error": "not_replicated", "replicated": {…}, "version": v}` |
| success | `{"status":"rebalanced","instances":n,"strategy":s,"gen_seq":r,"results":{str(iid):S},"by_node":{node_key:S},"skipped":[…],"empty":[…],"inactive":[…],"deadline_ms":d,"replicated":{…}}` |

`S = {"ok","applied","entries","gen","verified","transfer":"inline"|"chunked"|"path"|null,"reason","phase"}`.
A success reply also carries `"superseded": true` when the master schedule
changed during the fan-out. A member without `[schedule] en_schedule = 1`
reports `disabled` in its `S`.

### New verbs

| Request | Success reply | Errors and notes |
|---|---|---|
| `VERSION` | `{"ok":true,"protocol":2,"fw_version":…,"node_id":…,"cluster_id":…,"role":"leader"\|"follower"\|"static","term":n,"members":k,"verbs":[…],"deadlines":{"query_ms","mutation_ms","schedule_ms"},"abandoned_workers":a}` | |
| `LEADER [cluster_id]` | `{"ok":true,"role":…,"term":t,"eligible":bool,"reason":…,"schedule_version":v,"leader":{…}\|null,"lease":{"holder","term","expires_in_ms"},"candidates":[…],"quorum":bool}` | `cluster_mismatch` |
| `PREVOTE <t> <host:port> [cluster_id]` | `{"ok":true,"granted":bool,"term":n,"lease_holder":…,"reason":…}` | election traffic between candidates |
| `LEASE <t> <host:port> [cluster_id]` | `{"ok":true,"granted":bool,"term":n,"lease_ms":L,"reason":…}` | election traffic between candidates |
| `HEALTH` | `{"ok":true,"overall":"ok"\|"degraded"\|"error"\|"unknown","members":{node_key:{state,reachable,error_kind,health,sync_state,frequency_hz,seen_age_sec,snapshot_age_sec,frame_count_changed_age_sec,stale,flapping,busy_streak,last_seen}},"unreachable":[…],"degraded":[…],"stale":[…]}` | from the background poll cache |
| `NODES STATUS [FRESH]` | `{"ok":true,"overall":…,"cached":bool,"poll_duration_ms":n,"nodes":{node_key:<member STATUS>\|{"error","error_kind"}},"deadline_ms":d}` | |
| `MEMBERS` | `{"ok":true,"members":[{"node_key","node_id","host","advertised_host","instance_id","ports","source","state","addr","last_register_age_sec"}],"key_collisions":[…]}` | |
| `MEMBER ADD host:iid[@node_id]` / `MEMBER REMOVE <node_key>` | `{"ok":true,"members":[…],"changed":bool,"replicated":{…}}` | `duplicate_member`, `unknown_member`, `bad_request`, `not_replicated` |
| `REGISTER @n` + JSON body | `{"ok":true,"term":…,"role":…,"leader":…,"next_register_sec":s,"registered_as":{"node_key","host"}[,"peers":{…}]}` | `cluster_mismatch`, `duplicate_node_key`, `bad_request` |
| `PEERS` | `{"ok":true,"peers":{node_key:<member :5002 PEERS>},"deadline_ms":d}` | members without extended status return an error entry |
| `ALL <VERB> [args]` | `{"ok":true,"command":VERB,"results":{node_key:R},"summary":{…},"deadline_ms":d}` | `ALL SCHD` refused with `use_schedule_set`; unknown VERB `unknown_hwc_command` |
| `NODE <node_key> <VERB> [args]` | `{"ok":true,"node":k,"result":R,"deadline_ms":d}` | `unknown_member` |
| `FANOUT <k1,k2,…> <VERB> [args]` | as `ALL`, restricted to the listed members | `unknown_member` |
| `RECAL [node_key]` | as `ALL`/`NODE` with VERB `RECL`; members without `recl`, or that answer `disabled`, get `INIT` and `R.fallback = "INIT"` | |
| `SCHEDULE SET [round_robin\|range] @n` + JSON | `{"ok":true,"entries":n,"strategy":s,"version":v,"bytes":b,"assignments":{node_key:count},"replicated":{…},"durable":bool}` (preview only; `REBALANCE` distributes) | `bad_schedule`; a schedule above 262144 bytes as one document is refused |
| `SCHEDULE GET` | `{"ok":true,"schedule":{…},"version":v,"term":t,"set_at":ts}` | `no_master_schedule` |
| `SCHEDULE CLEAR` | `{"ok":true,"stopped":{node_key:R},"replicated":{…}}`; sends `SCHS` to reachable members | `not_replicated` |
| `SCHEDULE STRATEGY round_robin\|range` | `{"ok":true,"strategy":s,"replicated":{…}}` | `no_master_schedule`, `not_replicated` |
| `SCHEDULE SYNC @n` + JSON | `{"ok":true,"stored":bool}` | replication between candidates |
| `ASSIGNMENTS` | `{"ok":true,"strategy":s,"gen_seq":r,"assignments":{node_key:{"gen","entries","desired","state","attempts","last_result"}},"empty":[…],"partitioned_at":ts}` | |
| `EVENTS [n] [age:<sec> \| <since_ts>]` | `{"ok":true,"now":t,"events":[{…,"node_key":k,"rx_ts":t}]}`, merged from members' :5002 `EVENTS` plus the coordinator's own events (`node_key: "coordinator"`); n 1..1000 (default 100) | members that fail are listed in `"errors"` |
| `DB STATS` | `{"ok":true,"nodes":{node_key:<:5002 DB_STATS>},"deadline_ms":d}` | |
| `ROUTER STATS` | `{"ok":true,"router":"host:port","stats":{…}}` (proxy of the router's `STATS`) | `router_not_configured` (empty `[federation] router_stats_endpoint`), `router_unreachable` |

The schedule document format (`SCHEDULE SET/GET`, per-member `SCHD`):

```json
{"name":"site-scan","repeat_mode":"loop","strategy":"round_robin",
 "entries":[{"frequency":433000000,"gains":[297,297,297,297,297],"dwell_frames":100},
            {"frequency":868000000,"dwell_time_sec":2.0,"require_cal":false}]}
```

Real replies from a static coordinator with one unreachable member
(`--instances 127.0.0.1:0`, host name shortened):

```
$ printf 'VERSION\n' | nc 127.0.0.1 6000
{"ok": true, "protocol": 2, "fw_version": "1.1.0-dev", "node_id": "hosta:coordinator", "cluster_id": "default", "role": "static", "term": 0, "members": 1, "verbs": ["PING", "STATUS", "FREQ", "GAIN", "INSTANCE", "REBALANCE", "VERSION", "LEADER", "PREVOTE", "LEASE", "HEALTH", "NODES", "MEMBERS", "MEMBER", "REGISTER", "PEERS", "ALL", "NODE", "FANOUT", "RECAL", "SCHEDULE", "ASSIGNMENTS", "EVENTS", "DB", "ROUTER"], "deadlines": {"query_ms": 6000, "mutation_ms": 40000, "schedule_ms": 73000}, "abandoned_workers": 0}
$ printf 'FREQ 433920000\n' | nc 127.0.0.1 6000
{"command": "FREQ", "results": {"0": {"applied": false, "error": "[Errno 61] Connection refused", "error_kind": "refused", "phase": "connect", "ok": false, "elapsed_ms": 0}}, "by_node": {"127.0.0.1:0": {"applied": false, "error": "[Errno 61] Connection refused", "error_kind": "refused", "phase": "connect", "ok": false, "elapsed_ms": 0}}, "summary": {"applied": 0, "not_applied": 1, "unconfirmed": 0, "timeout": 0, "error": 0}, "deadline_ms": 40000}
$ printf 'REBALANCE\n' | nc 127.0.0.1 6000
{"error": "no master schedule"}
```

---

## Federation IQ router: ports 7000 and 7001

`federation_iq_router.py` reads the IQ streams of several instances (and of
upstream routers) and serves them as one multi-client TCP stream.
<!-- verify-after: WP-09 -->

```
python3 _daq_core/federation_iq_router.py [--config PATH]
    [--sources host:iid[@node_id]|router://host:port,...]
    [--output-port 7000] [--listen-address 0.0.0.0] [--stats-port 7001]
    [--queue-frames 4] [--drop-policy oldest|newest|disconnect]
    [--client-stall-limit 50] [--send-timeout-sec 2.0] [--max-clients 8]
    [--tag-mode instance|unit|node] [--stamp-seq] [--stamp-rx-ts]
    [--align-window-ms 0] [--align-max-frames 2] [--max-skew-ms 500]
    [--connect-on-demand | --always-connect] [--connect-timeout-sec 2.0]
    [--read-timeout-sec auto] [--first-frame-timeout-sec 0]
    [--reconnect-sec 3.0] [--upstream-stats host:port,...]
    [--auth-token T] [--allow-cidrs LIST]
    [--pid-file PATH] [--log-file PATH] [--log-level INFO]
```

Flags win over `--config` (`[federation] router_*` keys, `auth_token`,
`allow_cidrs`, membership, and `[daq] listen_address`). Sources:
`--sources`, else `router_sources`, else `cluster_members`, else `peer_list`
plus the self entry (`router_include_self`, default 1). Exit codes: 0 normal
stop, 2 output or stats port bind failure, 3 bad arguments or config.

### Sources

| Entry | Kind | Connects to | Protocol |
|---|---|---|---|
| `host:iid[@node_id]` | `iqserver` | `host:(5000 + iid × port_stride)` | `streaming` + one `IQDownload` per frame |
| `router://host:port` | `router` | an upstream router's output port | raw `[header][payload]` stream, no handshake |

A `host:iid` source needs `[data_interface] out_data_iface_type = eth` on
that instance. With the shipped `shmem` no iq_server runs, and the source
never gets past `connecting`/`backoff`.

* With `--connect-on-demand` (default from the CLI and INI) a source is
  connected only while at least one output client is attached. The router
  then does not hold an idle instance's single iq_server slot.
* Source reader states (STATS `state`): `idle`, `connecting`,
  `waiting_for_slot` (connected, no frame yet because another client holds
  iq_server), `streaming`, `backoff`, `stale` (chained logical source only:
  the upstream connection is streaming but this source has sent no frame
  within its stale time).
* Read timeout `auto` = `max(5, 3 × frame period)` from the first header.
  Reconnect delay starts at `--reconnect-sec` and doubles per failure up to
  30 s. TCP keepalive is set on every source socket.

### Output port (7000)

Clients connect and receive `[1024-byte header][payload]` frames as they
arrive. There is no handshake and no per-frame ack. Demultiplex streams by
the tag fields below.

* Each client has a queue of `--queue-frames` frames and its own sender, so a
  slow client never delays a source or another client.
* Full queue: `oldest` drops the oldest queued frame, `newest` drops the
  incoming frame, `disconnect` evicts the client. A client with
  `--client-stall-limit` consecutive drops is evicted (`slow_consumer`).
* A client that accepts no byte for `--send-timeout-sec` is evicted
  (`send_timeout`). A client left with a partly written frame is always
  evicted (`partial_frame`), because its stream is no longer frame-aligned.
* Clients beyond `--max-clients` are accepted and closed at once
  (`rejected_clients` in STATS). `--allow-cidrs` closes output and stats
  connections from outside the listed networks.
* `--align-window-ms W > 0` holds frames up to W ms and releases them in
  `time_stamp` order. This groups frames by wall-clock time; it does not make
  separately clocked units coherent, and it needs NTP/PTP on every host (see
  [networking_and_time_sync.md](networking_and_time_sync.md)).

### Tagging

| `--tag-mode` | Header bytes 24-27 (`unit_id`) | Reserved slots |
|---|---|---|
| `instance` (default) | overwritten with the source `instance_id` | untouched |
| `unit` | untouched (rtl_daq's `[hw] unit_id`, or N with `unit_id = auto`) | untouched |
| `node` | untouched | slot 106 = `instance_id + 1`, slot 107 = crc32 of the node key |

`--stamp-seq` stamps slot 103 and `--stamp-rx-ts` stamps slots 104/105 on
frames read from an iq_server. Frames read from an upstream router are never
re-stamped. In `instance` mode their `unit_id` is set from slot 106 minus 1
when slot 106 is non-zero.

**Chaining.** An upstream router used as a source must run
`--tag-mode node`. The downstream router splits its stream into logical
sources by slot 107, else slot 108 (`stamp_node_id = 1` on the instances).
Frames with both slots zero are counted in `unattributed_frames`.
`--upstream-stats` lets the downstream router name chained sources by node
key (it polls each upstream's `SOURCES` every 30 s).

### Stats port (7001)

Line protocol like :5002: one command per connection, one JSON object
followed by `\n`. A request without a newline is accepted. `AUTH <token> ` is
required when a token is configured. At most 16 requests are served at once;
the next one gets `{"ok":false,"error":"busy","retry_in_sec":1}`.

| Verb | Reply |
|---|---|
| `PING` | `{"ok":true,"ts":…}` |
| `VERSION` | `{"ok":true,"component":"iq_router","fw_version":…,"protocol":1,"verbs":[…]}` |
| `STATS` | full document (example below) |
| `SOURCES` | `{"ok":true,"sources":{key:{…}}}` |
| `CLIENTS` | `{"ok":true,"clients":[…]}` |
| other | `{"ok":false,"error":"unknown_command","valid":["PING","VERSION","STATS","SOURCES","CLIENTS"]}` |

Real `STATS` reply of a router with one idle source and no client:

```
$ printf 'STATS\n' | nc 127.0.0.1 7001
{"ok": true, "uptime_sec": 2.0, "output_port": 7000, "stats_port": 7001, "tag_mode": "instance", "align_window_ms": 0, "align_max_frames": 2, "connect_on_demand": true, "drop_policy": "oldest", "queue_frames": 4, "max_clients": 8, "memory_bound_bytes": 0, "memory_bound_frames": 35, "sources": {"127.0.0.1:0": {"instance_id": 0, "node_key": "127.0.0.1:0", "node_id": null, "node_key_crc32": 437873319, "node_id_crc32": 0, "connected": false, "state": "idle", "state_since_sec": 2.0, "read_timeout_sec": 0, "backoff_sec": 0.0, "consecutive_failures": 0, "kind": "iqserver", "via": null, "endpoint": "127.0.0.1:5000", "frames_received": 0, "bytes_received": 0, "frames_lost": 0, "cpi_gaps": 0, "restarts": 0, "overruns": 0, "last_cpi_index": null, "last_frame_age_sec": null, "clock_offset_ms": null, "skew_warning": false, "reconnects": 0, "unit_id_seen": null}}, "upstreams": {}, "clients": [], "evicted": {"slow_consumer": 0, "partial_frame": 0, "send_timeout": 0, "queue_full": 0}, "rejected_clients": 0, "late_frames": 0, "forced_releases": 0, "held_frames": 0, "unattributed_frames": 0, "alignment_lag_ms": {"p50": 0, "p95": 0}}
```

Per-source loss counters: `frames_lost` and `cpi_gaps` from `cpi_index`
jumps, `restarts` when `cpi_index` decreases, `overruns` from slot 102.
`clock_offset_ms` is the median over the last 64 frames of router receive
time minus header `time_stamp`; `skew_warning` is true when it differs from
the median of all sources by more than `--max-skew-ms`.
`memory_bound_bytes` is 0 until the frame size is known.

---

## Control files (internal)

These files in `_data_control/` connect hw_controller and delay_sync. They
are internal, but their formats are fixed. `<P>` is empty for instance 0 and
`inst<N>_` otherwise.

| File | Writer | Format | Enabled by |
|---|---|---|---|
| `<P>hwc_state` | hw_controller (every 5 frames, at once on new events) | JSON `{"v":1,"epoch","seq","ts","pid","fw_version","fsm","pending","noise_source","agc","gains_idx","gains_tenths","ext_lna_gains_db","bias_tee","schedule":{state,active,gen},"orientation":{az,el,state},"frame_age_ms","max_term_seen","cal_stalled","events":[≤ 32]}` | `[monitoring] en_hwc_state_relay = 1` |
| `<P>recal_request` | hw_controller on `RECL` | one line `<epoch> <gen> <unix_ms> <reason>`, reason `wire` or `gain_change` | `[calibration] en_wire_recal = 1` |
| `<P>fence_term` | hw_controller on a fencing XACK | decimal term + newline | a coordinator with `coordinator_candidates` |
| `<P>iq_track_lock` | operator, or `TLKA` | first byte `1` = approved | read only with `require_track_lock_intervention = 1` |

The fence term survives restarts and `daq_stop.sh <id> --clean`. Clear it
with `./daq_stop.sh <id> --reset-fence` after the instance has stopped. The
flag is refused (exit 3) while the instance still runs.

`epoch` is `<pid>-<process start ms>`. delay_sync records the current
`(epoch, seq)` at its own start and never replays older content.

---

## Net transport framing (optional `TRANSPORT_NET`)

`transport_net.c` (compiled only with `-DHAS_NET_TRANSPORT`) frames each
buffer on the TCP stream as:

```
[uint32 LEN, little-endian][LEN bytes: 1024-byte IQ header + payload]
```

`LEN` covers the header and the populated payload (derived from the header's
`cpi_length`/`active_ant_chs`/`sample_bit_depth`; it falls back to the full
buffer size when the sync word is absent). A **zero-length frame
(`LEN = 0`) is the terminate signal** in both directions. Real frames are
always at least 1024 bytes, so the encoding is unambiguous. Upgrade both ends
of a net-transport pair together.
