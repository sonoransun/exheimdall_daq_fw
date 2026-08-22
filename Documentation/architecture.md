# Architecture

HeIMDALL DAQ is a pipeline of cooperating processes connected by Unix pipes,
POSIX shared memory, and named FIFOs. It captures raw IQ from N coherent
RTL-SDR tuners, reshapes and decimates it, calibrates sample delay and IQ
amplitude/phase across channels, and hands coherent multichannel frames to
downstream DSP (direction finding, passive radar, GNU Radio).

## Process pipeline

```
                    stdout pipe        shm ring          shm ring
 USB  ┌───────────┐  [hdr|ch0..chN] ┌───────────┐ decimator_in ┌────────────┐ decimator_out
━━━━━▶│rtl_daq.out│━━━━━━━━━━━━━━━▶│rebuffer.out│━━━━━━━━━━━━▶│decimate.out│━━━━━━━━━━━┓
      └─────▲─────┘                 └───────────┘              └────────────┘           ┃
            ┃ ZMQ REQ/REP :1130                                                         ▼
      ┌─────┻───────────┐   delay_sync_hwc shm (header only)  ┌─────────────┐ delay_sync_iq shm
      │hw_controller.py │◀━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━│delay_sync.py │━━━━━━━━━━━┓
      └─▲───────────────┘                                     └──────▲──────┘           ┃
        │ TCP :5001 control            control files                 │ TCP :5002 status ▼
        │ (128-byte frames)     (orientation_state, bias_tee_state,  │ ZMQ PUB :5003  ┌─────────────┐
        │                        iq_track_lock in _data_control/)    │                │iq_server.out│──▶ TCP :5000
        └────────────────────────────────────────────────────────────┘                └─────────────┘   (or shmem out)
```

| Stage | Language | Role |
|---|---|---|
| `rtl_daq.out` | C | Opens all tuners via the krakenrf librtlsdr fork, one USB reader thread per channel, 8-slot ring buffer per channel, assembles `[1024 B header][ch0..chN u8 IQ]` frames on stdout, executes tuner control (ZMQ :1130), drives the noise source (GPIO 0) and per-channel bias tees (GPIO m+1), stamps `adc_overdrive_flags` and the buffer-overrun counter |
| `rebuffer.out` | C | Reads the stdout pipe, reshapes DAQ blocks into CPI-sized frames in the `decimator_in` shared-memory ring |
| `decimate.out` | C | u8→cf32 conversion + FIR decimation through the offload engine abstraction; CAL frames bypass the FIR and pass at full ADC rate; output in `decimator_out` (data_type=3, 32-bit cf32) |
| `delay_sync.py` | Python | Sync state machine: cross-correlation delay estimation, fractional-delay (ppm) correction, IQ amplitude/phase calibration via eigendecomposition; stamps the v8 telemetry slots; owns the monitoring stack (status server :5002, metrics, event bus, PUB :5003, database writes) |
| `hw_controller.py` | C-plane Python | Drives calibration (noise source bursts, gain lock), serves the TCP :5001 control interface, owns the scheduler, RF front-end budget and orientation controller; talks to rtl_daq over ZMQ :1130 |
| `iq_server.out` | C | Streams `[header][payload]` frames to one TCP :5000 client with a stop-and-wait per-frame ack (only launched when `[data_interface] out_data_iface_type = eth`; `shmem` leaves the `delay_sync_iq` ring as the output) |

## Inter-process communication

### Data plane: double-buffered shared memory + FIFO byte signaling

Every shm hop uses **two** POSIX shared-memory segments per stream, named
`<name>_A` and `<name>_B` (e.g. `decimator_in_A`/`decimator_in_B`,
`decimator_out_A/_B`, `delay_sync_iq_A/_B`, `delay_sync_hwc_A/_B`). The
producer fills one segment while the consumer reads the other. Each segment
holds a full frame: 1024-byte header + payload (the `delay_sync_hwc` segments
are 1024 bytes — header only, hw_controller never reads payload).

Signaling runs over a pair of named FIFOs in `_data_control/` (paths are
cwd-relative to `Firmware/`), one raw byte per event:

| Byte | Name | Direction | Meaning |
|---|---|---|---|
| `10` | `INIT_READY` | forward (`fw_<name>`) | producer initialized, sent exactly once |
| `1` | `A_BUFF_READY` | forward | segment `_A` filled, ready to consume |
| `2` | `B_BUFF_READY` | forward | segment `_B` filled, ready to consume |
| `1` / `2` | buffer free | backward (`bw_<name>`) | consumer done with `_A` / `_B` |
| `255` | `TERMINATE` | forward | producer shutting down |

The forward FIFO carries producer→consumer "ready" signals, the backward FIFO
carries consumer→producer "free" signals; splitting them prevents deadlock.
**Drop mode** (used on the delay_sync→hw_controller hop and the transport
`FLOW_DROP` policy) opens the producer's backward FIFO `O_NONBLOCK`: when no
buffer is free the frame is dropped and the producer-side API returns the
sentinel `3` (`BUFFER_DROPPED`) instead of blocking. The C implementation is
`sh_mem_util.c`; `shmemIface.py` speaks the identical byte protocol.

The rtl_daq→rebuffer hop is not shared memory: it is a plain stdout pipe
carrying `[1024 B header][ch_no × buffer_size bytes u8 interleaved IQ]` per
frame (dummy frames are header-only), written as one coalesced `writev` with
an enlarged pipe buffer.

### Control plane

- **ZMQ REQ/REP :1130** (`+ instance_id × port_stride`) — 128-byte tuner
  control messages from delay_sync/hw_controller to rtl_daq (commands
  `r c g a s n b h`, reply `"ok"`; each command triggers 5 dummy frames while
  the tuners settle). See [protocols.md](protocols.md).
- **TCP :5001** — 128-byte command frames (4-byte verb + 124-byte payload)
  served by `CtrIfaceServer` inside hw_controller; query verbs reply
  `FNSD`+JSON. See [protocols.md](protocols.md).
- **TCP :5002** — line-command JSON status/metrics/events/DB endpoint inside
  delay_sync.
- **ZMQ PUB :5003** — event stream (multipart `[event_type, json]`).
- **Control relay files** in `_data_control/` (atomic `os.replace` writes,
  `inst<N>_` prefixed for federation instances):
  - `orientation_state` — `"az el state"` (two floats + int enum), written
    throttled by hw_controller, polled every 5th frame by delay_sync to stamp
    header slots 98–100.
  - `bias_tee_state` — one decimal bitmask line, written by hw_controller at
    init and on every `BIAS` command, polled by delay_sync into slot 97.
  - `iq_track_lock` — track-lock intervention flag used with
    `require_track_lock_intervention=1`.

## Startup orchestration

`daq_start_sm.sh` (hardware) and `daq_synthetic_start.sh` (simulation) run
from `Firmware/` under sudo. Both:

1. Validate the config via `python3 ini_checker.py no_hw` (nonzero exit blocks
   startup).
2. Regenerate FIR coefficients: `fir_filter_designer.py` →
   `_data_control/fir_coeffs.txt` (every start; length must equal
   `fir_tap_size`).
3. Run hardware discovery (`hw_discover.py` → `_data_control/hw_caps.json`,
   cached for the whole run) and `auto_config.py` resolution of `auto` keys.
4. Optionally load an FPGA bitstream / initialize GPU offload, falling back to
   CPU-only on failure.
5. Create the FIFOs, then launch the processes with tiered `SCHED_FIFO`
   priorities and CPU affinity:

| FIFO priority | Process | Core (instance 0) | Role |
|---|---|---|---|
| 99 | `rtl_daq.out` | 0 | USB I/O critical path |
| 94 | `rebuffer.out` | 1 | Shared-memory handoff |
| 92 | `decimate.out` | 1 | Cache locality with rebuffer |
| 90 | `delay_sync.py` | 2 | FFT / cross-correlation |
| 88 | `iq_server.out` | 2 | TCP output (eth mode only) |
| 82 | `hw_controller.py` | 3 | Control plane |

Priorities are hardcoded in `daq_start_sm.sh` — there is no config option.
Instance N uses cores `(N*4 + 0..3) mod nproc` so two federation instances do
not stack their RT processes on the same cores (a warning is printed when they
must share). PID files land in `_logs/inst<N>/pids/` (including `rtl_daq.pid`
and, in synthetic mode, `synthesizer.pid`). Instance 0 logs stay at the
historical flat `_logs/*.log` paths; instance N>0 logs at `_logs/instN/*.log`.

`daq_stop.sh` stops the chain: no argument = global stop (PID files first,
then a name-based orphan sweep; SIGTERM, 2 s grace, SIGKILL sweep), `<id>` =
one instance via its PID files, `--legacy` = SIGRT(64) instead of SIGTERM for
old binaries. All C binaries still trap signal 64.

## Sync state machine

`delay_sync.py` walks the calibration FSM; the header `sync_state` field is
the externally visible encoding:

```
             ┌────────────────────────── sync lost (max_sync_fails) ──────────────┐
             ▼                                                                    │
 STATE_INIT ──▶ STATE_SAMPLE_CAL ──▶ STATE_SYNC_WAIT ─┐                           │
   (1)             (2)                  (3)           │                           │
                     ▲                                ▼                           │
                     └── dummy frame ── STATE_FRAC_SAMPLE_CAL / FRAC_SYNC_WAIT    │
                                                (3)                               │
                                                 │                                │
                                                 ▼                                │
                                          STATE_IQ_CAL ──▶ STATE_TRACK_LOCK ──▶ STATE_TRACK
                                                (4)             (5)                (6)
```

- `0` = no sync, `1–4` = calibration stages, `5` = track lock, `6` = tracking.
- Consumers rely on the numbering: `sync_state >= 5` means locked (status
  server health, DB sync-lost queries), `>= 6` is the scheduler's cal-wait
  exit, and orientation motion is gated on `sync_state >= min_sync_state`
  (default 5) **and** `noise_source_state == 0`.
- Sample-delay calibration uses cross-correlation over `corr_size` samples;
  fractional-sample residuals are corrected by per-channel sampling-frequency
  ppm tuning (ZMQ `s`); IQ amplitude/phase correction comes from the dominant
  eigenvector of the spatial correlation matrix.
- hw_controller mirrors the state: it enables the noise source while
  `sync_state < 5`, counts calibration bursts at `6`, and triggers
  recalibration on drift. External hardware-mutating control commands execute
  only in `STATE_IQ_CAL` on non-dummy frames (queries answer in any state).

## Transport and offload abstractions

Both are vtable-dispatched C interfaces with factory functions; the defaults
are behavior-identical to the pre-abstraction firmware.

### Transport (`transport.h` / `transport.c`)

```c
struct transport_handle* transport_create(name, size, is_producer,
                                          flow_control, instance_id, type);
```

`transport_type_t`: `TRANSPORT_SHM` (0, default — a pure wrapper over
`sh_mem_util.c`), `SPI` (1), `PCIE` (2), `USB3` (3), `NET` (4). Optional
drivers are compiled in only with `-DHAS_SPI_TRANSPORT`, `-DHAS_PCIE_TRANSPORT`,
`-DHAS_USB3_TRANSPORT`, `-DHAS_NET_TRANSPORT` (commented out in the Makefile
by default); an unknown/unavailable type falls back to shm. The vtable
(`init_producer/init_consumer/destroy/get_write_buf/submit_write/get_read_buf/
release_read/send_terminate`) mirrors the shmem protocol: buffer index 0/1,
`3` = frame dropped, `255` = TERMINATE, negative = error; `buffer_size`
includes the 1024-byte header; `num_buffers` is always 2. Flow control is
`FLOW_BACKPRESSURE` (block) or `FLOW_DROP`.

Python mirrors: `transportIface.py` (`TransportProducer`/`TransportConsumer`
wrapping `shmemIface.py`, attribute-compatible with `outShmemIface`/
`inShmemIface` including the frozen `destory_sm_buffer` typo).

### Offload (`offload.h` / `offload.c`)

```c
struct fir_engine*     fir_engine_create(offload_engine_t type);      /* OFFLOAD_AUTO = detect */
struct convert_engine* convert_engine_create(offload_engine_t type);
```

Engine matrix (FIR decimation + u8→f32 conversion):

| Engine | enum | Source | Compiled when | Notes |
|---|---|---|---|---|
| NEON / Ne10 | `OFFLOAD_CPU_NEON` (0) | `offload_cpu_neon.c` | `ENGINE=ne10` lane | 32-bit ARM only (libNE10.a is armv7); real NEON intrinsics for the u8→f32 convert on AArch64 |
| KFR | `OFFLOAD_CPU_KFR` (1) | `offload_cpu_kfr.c` | `ENGINE=kfr` lane (default on x86_64 **and** aarch64) | Per-channel/per-component KFR plans, allocation-free reset |
| FPGA | `OFFLOAD_FPGA` (2) | `offload_fpga.c` | `-DHAS_FPGA_OFFLOAD` | SPI + GPIO DRDY, triple buffered |
| GPU | `OFFLOAD_GPU` (3) | `offload_gpu.c` | `-DHAS_GPU_OFFLOAD` | VideoCore VI mailbox QPU, software fallback |
| Generic | `OFFLOAD_CPU_GENERIC` (4) | `offload_cpu_generic.c` | always in kfr/ne10 lanes (`-DHAS_GENERIC_OFFLOAD`, runtime-selectable); sole engine under `-DOFFLOAD_GENERIC_ONLY` (`ENGINE=generic`) | Dependency-free plain C99, bit-exact direct-form FIR |

Selection: `[offload] fir_engine` accepts `auto`, `neon`/`cpu_neon`,
`kfr`/`cpu_kfr`, `generic`/`cpu_generic`, `fpga`, `gpu`; `auto` picks the
platform engine compiled into the binary. Python-side engines live in
`offload_engines.py` (FFT/correlation) and `offload_gpu.py`.

## Federation (multi-instance)

Multiple DAQ instances on one or more hosts coordinate through
`federation_coordinator.py` (TCP :6000, JSON replies) and optionally merge
their IQ streams through `federation_iq_router.py` (TCP :7000, frames tagged
with the source instance in the header `unit_id`).

Resource namespacing (`[federation]` section):

- Instance 0 is fully backward compatible: unprefixed shm/FIFO/control-file
  names, base ports.
- Instance N: `inst<N>_` prefix on shared-memory segments, FIFOs, and control
  files; logs under `_logs/instN/`.
- Every port derives from `base_port + instance_id × port_stride`
  (default stride 100; `compute_port()` in `sh_mem_util.c`): bases 5000 (IQ),
  5001 (control), 5002 (status), 5003 (events), 1130 (ZMQ).
- `federation_health.py` polls peer status servers (canonical peer form
  `host:instance_id`) and runs coordinator election; `federation_scheduler.py`
  partitions a master frequency schedule across instances with `round_robin`
  (interleaved) or `range` (contiguous block) strategies and distributes it via
  `SCHD` control frames.
- Orientation/rotator hardware is per-instance and **not** fanned out by
  federation.

## RF front-end and orientation subsystems

Three optional, disabled-by-default subsystems owned by `hw_controller.py`
(no new processes are launched):

- **`gain_budget.py`** (`[amplification]`) — pure link-budget math: total
  system gain, cascaded Friis noise figure, P1dB compression headroom, plus
  `RfFrontendConfig`/`RfFrontendParser`. External LNA gain is continuous dB in
  a **separate** array (`hw_controller.ext_lna_gains_db`) — never routed
  through the R820T `valid_gains` quantizer or written into `self.gains` /
  header `if_gains[]` (those drive the gain-lock equality checks). Total gain
  is computed **after** the `unified_gain_control` clamp. Compression is
  advisory only (augments the overdrive picture, never auto-changes gains).
- **`antenna_profile.py`** (`[antenna]`) — element gain (dBi), beamwidths,
  polarization, cable/connector loss, boresight offsets. Shared by the gain
  budget and the orientation boresight correction.
- **`orientation_controller.py`** + **`rotator_controller.py`**
  (`[orientation]`) — a per-frame state machine (IDLE=0, SLEWING=1,
  SETTLED=2, SCANNING=3) ticked beside the scheduler in `STATE_IQ_CAL`, over a
  backend abstraction (`mock`/`gs232`/`pwm_servo`/`i2c_pantilt`; the factory
  never raises — it falls back to mock so the chain always starts). Bearing
  sources: external `ORNT` commands, fixed/park config, or autonomous
  scan-and-peak (`SCAN`) maximizing the aggregate channel power that
  delay_sync stamps into header slot 101 (per-grid-point mean over
  `scan_dwell_frames` after discarding `settle_frames`). Motion is gated on
  sync state and the noise source being off.

Runtime bias-tee: the `BIAS` control frame becomes the ZMQ `b` message;
`rtl_daq.c` applies `rtlsdr_set_bias_tee_gpio(dev, m+1, ...)` per channel.
GPIO 0 is reserved for the noise source so bias power can never collide with
calibration, and bias changes defer while a calibration noise burst is active.

Telemetry from all three subsystems is stamped into the v8 header reserved
slots by delay_sync (see [protocols.md](protocols.md)).
