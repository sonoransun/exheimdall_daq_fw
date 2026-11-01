# Architecture

HeIMDALL DAQ is a chain of cooperating processes. Unix pipes, POSIX shared
memory and named FIFOs connect them. The chain captures raw IQ from N coherent
RTL-SDR tuners, reshapes and decimates it, and calibrates sample delay and IQ
amplitude/phase across channels. It then hands coherent multichannel frames to
downstream DSP software (direction finding, passive radar, GNU Radio).

This document describes how the pieces fit together. Related documents:

| Topic | Document |
|---|---|
| Wire formats, verbs, reply shapes | [protocols.md](protocols.md) |
| Every configuration key | [configuration.md](configuration.md) |
| Several instances and several hosts | [scaling_and_clustering.md](scaling_and_clustering.md) |
| Sizing and pre-flight | [capacity_planning.md](capacity_planning.md) |
| Ports, bind addresses, clocks | [networking_and_time_sync.md](networking_and_time_sync.md) |
| Calibration theory | [theory_coherent_acquisition.md](theory_coherent_acquisition.md) |
| Upgrades and the default-path ledger | [upgrade_and_rollback.md](upgrade_and_rollback.md) |
| Building and testing | [developer_guide.md](developer_guide.md) |

**Default path and opt-in features.** Every feature added by the scaling and
clustering work is off by default. With every new key absent and no new flag
passed, process names, FIFO bytes, shared-memory names and sizes, ports, wire
frames and the 1024-byte header are unchanged. The few deliberate exceptions
are listed in the ledger in [upgrade_and_rollback.md](upgrade_and_rollback.md).
This document marks each opt-in feature with the key or flag that enables it.

**Packages still being implemented.** One section follows the design
specification, not the code in this tree:
[Recording and replay](#recording-and-replay). It carries a visible
**Planned** note. Everything else describes the code in this tree.

## Process pipeline

```
                 stdout pipe           shm ring              shm ring
 USB  +-----------+ [hdr|ch0..chN] +------------+ decimator_in +------------+ decimator_out
=====>|rtl_daq.out|===============>|rebuffer.out|=============>|decimate.out|=============+
      +-----^-----+  back-pressure +------------+ back-pressure+------------+  drop mode  |
            | ZMQ REQ/REP :1130                                                          v
      +-----+------------+  delay_sync_hwc shm (header only)  +--------------+  delay_sync_iq shm
      | hw_controller.py |<===================================| delay_sync.py|=============+
      +-----^------------+            drop mode                +------^-------+  drop mode  |
            | TCP :5001 control         control files                 | TCP :5002 status    v
            | (128-byte frames)         in _data_control/             | ZMQ PUB :5003  +-------------+
            +---------------------------------------------------------+                |iq_server.out|--> TCP :5000
                                                                                       +-------------+  (or shmem out)
```

Every port above is the base port. Instance N adds `N x port_stride`
(default stride 100). See [Instances and namespacing](#instances-and-namespacing).

| Stage | Language | Role |
|---|---|---|
| `rtl_daq.out` | C | Opens all tuners through the krakenrf librtlsdr fork. Runs one USB reader thread per channel with a ring of `[daq] usb_ring_buffers` blocks per channel (default 8). Assembles `[1024 B header][ch0..chN u8 IQ]` frames on stdout. Executes tuner control from ZMQ :1130 (bound to `[daq] zmq_listen_address`, default `*`). Drives the noise source (GPIO 0) and the per-channel bias tees (GPIO m+1). Stamps `adc_overdrive_flags` and the buffer-overrun counter (slot 102). Opt-in: `[hw] serial_base` / `device_serials` open tuners by serial instead of by index |
| `rebuffer.out` | C | Reads the stdout pipe and reshapes DAQ blocks into CPI-sized frames in the `decimator_in` ring |
| `decimate.out` | C | Converts u8 to cf32 and runs FIR decimation through the offload engine abstraction. CAL frames bypass the FIR and pass at the full ADC rate. Writes `decimator_out` (data_type 3, cf32). Opt-in: `[monitoring] stamp_decimator_drops = 1` stamps its drop count into slot 109 |
| `delay_sync.py` | Python | Runs the sync state machine: cross-correlation delay estimation, fractional-delay correction by sample-rate tuning, IQ amplitude/phase calibration by eigendecomposition. Stamps the v8 telemetry slots. Owns the monitoring stack when `[monitoring] en_monitoring = 1`: status server :5002, metrics, event bus, PUB :5003, database writes. Hosts the federation health monitor when federation is enabled |
| `hw_controller.py` | Python | Drives calibration (noise-source bursts, gain lock). Serves the TCP :5001 control interface. Owns the scheduler, the RF front-end budget and the orientation controller. Talks to rtl_daq over ZMQ :1130 |
| `iq_server.out` | C | Streams `[header][payload]` frames over TCP :5000 with a per-frame `IQDownload` ack. Launched only when `[data_interface] out_data_iface_type = eth`. With `shmem` the `delay_sync_iq` ring is the output. Default: one client at a time. Opt-in: `[data_interface] iq_server_max_clients > 1` serves several clients |

<!-- verify-after: WP-04 -->
`iq_server.out` multi-client mode: an accept thread performs the `streaming`
handshake for up to `iq_server_max_clients` sessions (upper clamp 64). The
main thread hands each frame to every session and waits until each one has
sent it and read its `IQDownload` ack, or until the frame deadline. The
deadline is `[data_interface] iq_server_frame_deadline_ms`, where `0` (default)
means 0.8 x the frame period with a 100 ms floor. Stragglers are closed. Then
the buffer is released. Each session receives the same bytes as the
single-client loop.

### Output attach behaviour

The shipped config sets `[data_interface] out_data_iface_type = shmem`. In
that mode the chain stalls at startup until a downstream consumer attaches.

| Mode | Who opens `_data_control/<P>fw_delay_sync_iq` | Effect at startup |
|---|---|---|
| `shmem` (shipped default) | the downstream DSP application (the shm consumer) | delay_sync blocks in its open of the forward FIFO until a consumer opens it. Until then no frame flows, the sync FSM does not run and hw_controller waits too, because delay_sync creates `delay_sync_hwc` only after `delay_sync_iq`. :5001 still accepts connections, but mutating verbs never execute and time out |
| `eth` | `iq_server.out`, launched by the start script | The chain runs with no client attached. Clients connect to :5000 |

Every process stays alive while the chain waits, so a stalled `shmem` chain
looks healthy in `ps`. The open is a plain blocking `O_WRONLY` open; no lazy
or non-blocking attach mode exists.

- For a standalone or bench start, set `out_data_iface_type = eth`, or start
  the shm consumer.
- Only one consumer may attach to the `delay_sync_iq` ring. It has one
  forward/backward FIFO pair. Several readers need `eth` (with
  `iq_server_max_clients > 1`) or the [IQ router](#iq-router).

## Inter-process communication

### Data plane: double-buffered shared memory and FIFO byte signaling

Every shared-memory hop uses two POSIX shared-memory segments per stream,
named `<name>_A` and `<name>_B`. The streams are `decimator_in`,
`decimator_out`, `delay_sync_iq` and `delay_sync_hwc`. The producer fills one
segment while the consumer reads the other. Each segment holds one full frame:
the 1024-byte header plus the payload. The `delay_sync_hwc` segments hold the
header only, because hw_controller never reads a payload.

Signaling runs over a pair of named FIFOs per stream in `_data_control/`.
The paths are relative to `Firmware/` (the working directory). Each event is
one raw byte:

| Byte | Name | FIFO | Meaning |
|---|---|---|---|
| `10` | `INIT_READY` | forward (`fw_<name>`) | Producer initialized; sent once |
| `1` | `A_BUFF_READY` | forward | Segment `_A` filled, ready to consume |
| `2` | `B_BUFF_READY` | forward | Segment `_B` filled, ready to consume |
| `1` / `2` | buffer free | backward (`bw_<name>`) | Consumer is done with `_A` / `_B` |
| `255` | `TERMINATE` | forward | Producer is shutting down |

The forward FIFO carries producer-to-consumer "ready" signals. The backward
FIFO carries consumer-to-producer "free" signals. Splitting them prevents
deadlock. The C implementation is `sh_mem_util.c`. `shmemIface.py` speaks the
same byte protocol.

The rtl_daq to rebuffer hop is not shared memory. It is a plain stdout pipe
carrying `[1024 B header][ch_no x buffer_size bytes u8 interleaved IQ]` per
frame. Dummy frames are header-only. rtl_daq writes each frame as one
coalesced `writev` into an enlarged pipe buffer.

### Flow control per hop

Two policies exist. **Back-pressure** blocks the producer until a buffer is
free. **Drop mode** opens the producer's backward FIFO `O_NONBLOCK`. When no
buffer is free, the producer drops the frame and the producer-side API returns
the sentinel `3` (`BUFFER_DROPPED`) instead of blocking. The transport layer
calls these `FLOW_BACKPRESSURE` and `FLOW_DROP`.

| Hop | Policy as launched | Why |
|---|---|---|
| rtl_daq to rebuffer (pipe) | back-pressure | A full pipe blocks the writer. USB overruns then show up in slot 102 |
| rebuffer to decimate (`decimator_in`) | back-pressure | The start scripts launch `rebuffer.out 0` |
| decimate to delay_sync (`decimator_out`) | drop | `decimate.out` is launched without an argument, so `drop_mode` defaults to true |
| delay_sync to iq_server or shmem consumer (`delay_sync_iq`) | drop | Created with `drop_mode=True`. Once a consumer is attached, a slow IQ client loses frames (counted in `dropped_frames_iq`) and never stalls the chain. Before any consumer attaches, the chain waits (see [Output attach behaviour](#output-attach-behaviour)) |
| delay_sync to hw_controller (`delay_sync_hwc`) | drop | Created with `drop_mode=True` (counted in `dropped_frames_hwc`) |

Both C stages accept one argument that overrides the default: `0` selects
back-pressure and `1` selects drop mode.

### Segment sizes

`Firmware/_daq_core/frame_sizes.h` is the single C definition of the segment
sizes. All arithmetic is in `size_t`. M is `[hw] num_ch`, Ncpi is
`[pre_processing] cpi_size`, R is `decimation_ratio`, Nc is
`[calibration] corr_size`.

| Segment (each of `_A`, `_B`) | Bytes |
|---|---|
| `decimator_in` object (rebuffer creates it, u8 IQ) | `1024 + max(Ncpi x R, Nc) x M x 2` |
| `decimator_in` mapping in decimate.out | `1024 + max(Ncpi x R, Nc) x M x 8` (virtual over-map; pages past the object end are never touched) |
| `decimator_out` (default) | `1024 + 2^23 x M x 8` (worst case) |
| `decimator_out` with `[pre_processing] size_segments_from_config = 1` | `1024 + max(Ncpi, Nc) x M x 8` |
| `delay_sync_iq` | `1024 + max(Ncpi, Nc) x M x 8` |
| `delay_sync_hwc` | `1024` |

`ini_checker.py` rejects `num_ch > 31`, `cpi_size > 2^23` and
`corr_size > 2^23`. The 128-byte ZMQ message carries at most 31 per-channel
values, and larger frames would overrun the worst-case segments.
See [capacity_planning.md](capacity_planning.md) for the full resource model.

### Control plane

| Channel | Base port | Owner | Purpose |
|---|---|---|---|
| ZMQ REQ/REP | 1130 | rtl_daq (or the synthesizer) | 128-byte tuner messages from delay_sync and hw_controller. Commands `r c g a s n b h`, reply `ok`. Each command makes the source emit 5 header-only dummy frames while the tuners settle |
| TCP control | 5001 | hw_controller (`CtrIfaceServer`) | 128-byte frames: 4-byte verb + 124-byte payload. Every reply is 128 bytes starting `FNSD` |
| TCP status | 5002 | delay_sync (`[monitoring] en_monitoring = 1` and `en_status_server = 1`; port `status_server_port`) | One line command, one JSON object + `\n` |
| ZMQ PUB | 5003 | delay_sync (`[monitoring] en_monitoring = 1` and `en_zmq_pub = 1`; port `zmq_pub_port`) | Event stream, multipart `[event_type, json]` |

The TCP servers bind `[daq] listen_address` (default `0.0.0.0`). See
[protocols.md](protocols.md) for every verb.

### Control relay files

hw_controller and delay_sync also exchange state through small files in
`_data_control/`. Writers replace them atomically (temp file + `os.replace`).
For instance N > 0 every name gets the `inst<N>_` prefix.

| File | Writer | Reader | Content | Enabled by |
|---|---|---|---|---|
| `orientation_state` | hw_controller (throttled) | delay_sync, polled every 5th frame | `az el state` (two floats + int) for header slots 98-100 | `[orientation] en_orientation = 1` |
| `bias_tee_state` | hw_controller (at init and on every `BIAS`) | delay_sync, polled every 5th frame | one decimal bitmask line for slot 97 | `[amplification] en_amplification = 1` |
| `iq_track_lock` | operator, or the `TLKA` verb (written in place) | hw_controller, re-opened on every check | first byte `1` = approved | read only with `[calibration] require_track_lock_intervention = 1`; a missing file means "not approved" |
| `fence_term` | hw_controller, when an accepted `XACK` carries a higher term for this member's `cluster_id` | hw_controller at start | decimal term + newline, fsynced | written only by coordinator XACKs; survives restarts |
| `hwc_state` | hw_controller main loop (every 5 frames, at once when events arrive, once at the end of init) | delay_sync | JSON document `"v": 1` with `epoch`, FSM name, gains, schedule, orientation and the last 32 events | `[monitoring] en_hwc_state_relay = 1` |
| `recal_request` | hw_controller on `RECL` (or after a GAIN with `recal_on_gain_change = 1`) | delay_sync (`RecalWatcher`) | `<epoch> <gen> <unix_ms> <reason>` | `[calibration] en_wire_recal = 1` |

**Epochs.** hw_controller computes `epoch = "<pid>-<start_ms>"` once at start.
Event `seq` numbers and the recalibration generation restart at 0 in every
process. A reader treats an event as new when the epoch changed or its `seq`
is higher than the last one seen. At start, delay_sync reads each relay file
once and records a baseline without acting on it, so a leftover file from a
previous run is never replayed.

<!-- verify-after: WP-06 -->
**Event relay.** With `en_hwc_state_relay = 1`, delay_sync re-emits new
hw_controller events on its own event bus with `module` preserved and
`payload.relayed = true`. They then reach :5003, the `EVENTS` ring and syslog
through the one existing stream. With `[monitoring] status_extended = 1` the
document (minus `events`) also appears as `STATUS.hwc`. More than 32
hw_controller events between two reads (about 2 s) are lost; the document
holds only the last 32.

### hw_controller control-plane gating

hw_controller runs its own five-state FSM: `INIT`, `GAIN_CTR_WAIT`,
`GAIN_TUNE`, `NOISE_CTR_WAIT`, `IQ_CAL` (wire names drop the `STATE_`
prefix). The :5001 server thread queues each request. The main loop drains the
queue once per frame.

| Verb class | Verbs | Where it runs | When |
|---|---|---|---|
| Hardware-mutating | `FREQ GAIN AGC INIT EGAN BIAS SCHD SCHS SCHN ORNT PARK SCAN OSTP RECL` | main loop | only in `STATE_IQ_CAL` on a non-dummy frame (see the burst note below) |
| Threshold (no-op) | `STHU` | server thread | answered at once; accepted for wire compatibility, not implemented (`FNSD` + zeros, or `unsupported` with `XACK`) |
| Queries | `RFQ `, `OQRY`, `SCHQ` | main loop | any FSM state |
| Snapshot and session verbs | `XACK`, `HWSQ`, `SCHG`, `TLCK`, `TLKA`, `SCHB`, `SCHC` | server thread | any time, within milliseconds, even while frames stall |
| Chunked schedule commit | `SCHE` | main loop | as a queued schedule load |

**Noise-burst deferral.** `INIT` and `RECL` also wait while a calibration
noise burst is active. With `[calibration] defer_control_during_burst = 1`,
`FREQ`, `GAIN`, `EGAN` and `BIAS` wait too. A deferred command stays queued in
order and runs on a later frame, unless its request timeout
(`[daq] ctl_request_timeout_sec`) expires first.

`HWSQ` and `SCHG` read a control snapshot. The main loop publishes it on every
frame after the queue drain, before it frees the input buffer.

**Extended acks (opt-in per connection).** A client that sends `XACK` first
gets JSON acks such as `{"applied":true,"reason":"ok","fsm":"IQ_CAL","seq":17}`
for mutating verbs. A connection that never sends `XACK` gets the legacy reply
bytes. heimdall-ctl and the coordinator send `XACK` automatically. A legacy
hw_controller answers `XACK` with `FNSD` + zeros and logs
`Unidentified control command: XACK` once.

**Connection keys (opt-in).** `[daq] ctl_max_connections` (default 1) keeps
today's sequential accept loop. A value above 1 serves that many clients at
once, so a resident DoA client no longer blocks heimdall-ctl or the
coordinator. The timeout keys `ctl_request_timeout_sec` (10.0) and
`ctl_handler_max_sec` (12.0) are described in [protocols.md](protocols.md).

## Startup orchestration

`daq_start_sm.sh` (hardware) and `daq_synthetic_start.sh` (simulation) run
from `Firmware/` under sudo. In the synthetic script,
`_testing/test_data_synthesizer.py` takes the place of `rtl_daq.out`. It
answers ZMQ commands on `1130 + N x port_stride`, so hw_controller and
delay_sync can calibrate the simulated chain.

Both scripts source the shared library `Firmware/daq_common.sh` and run these
steps in this order. `<P>` is the FIFO prefix: empty for instance 0,
`inst<N>_` for N > 0.

1. Validate the config: `python3 ini_checker.py no_hw attached` (hardware) or
   `python3 ini_checker.py no_hw` (synthetic). The `attached` token adds the
   sysfs serial and host-registry checks. A nonzero exit blocks startup with
   exit 1. Warnings print on stderr as `WARNING: ...` and never change the
   exit code.
2. Read the INI through `hd_read_config`: `out_data_iface_type`,
   `instance_id`, the port stride, the monitoring ports and the `[host]` keys.
3. Hardware only: early host tuning (`kernel.sched_rt_runtime_us=-1` and
   `../util/irq_tuning.sh` by default; see `[host] host_tuning`).
4. With `[host] instance_registry = 1` only: resolve the run directory and
   take the startup mutex and the instance lock.
5. Recreate the 8 FIFOs. Delete `<P>recal_request` and `<P>hwc_state` left by
   the previous run. Create `<P>iq_track_lock` containing `0` when it is
   missing.
6. Create `_db/` and the log directory, and archive old logs (last 10
   archives kept per instance).
7. Hardware only: late host tuning (`usbfs_memory_mb = 0`, drop caches).
8. Port check, listener-only: ports `5000`, `5001` and
   `[monitoring] status_server_port` (default 5002), each + offset. In the
   hardware script a busy port triggers `./daq_stop.sh` (the scoped
   `./daq_stop.sh N` under `instance_guard = refuse` or a supervisor), retried
   up to 10 times, then exit 1. In the synthetic script the check only prints
   `WARN: port ... in use`, unless `instance_guard = refuse`, which applies the
   hardware rules.
9. Regenerate the FIR coefficients with `fir_filter_designer.py`. Instance 0
   writes `_data_control/fir_coeffs.txt`. Instance N > 0 writes
   `_data_control/inst<N>_fir_coeffs.txt`, which `decimate.out` of the same
   instance reads. On failure both scripts exit 1.
10. Run hardware discovery (`hw_discover.py` > `_data_control/<P>hw_caps.json`,
   cached for the whole run), then `auto_config.py` on that file. auto_config only rewrites
   values that are literally `auto` in `[pre_processing] fft_engine` /
   `fft_workers`, `[calibration] correlation_engine` / `xcorr_workers` and
   `[data_interface] transport_type`. No process reads those keys and the
   shipped config defines none of them, so this step normally changes nothing.
   When it does write, the write is atomic and keeps the file mode. The keys
   that ship as `auto`, `[offload] fir_engine` and `fft_engine`, are resolved
   at runtime: `decimate.out` passes `fir_engine` to `fir_engine_create()`, and
   delay_sync maps `fft_engine = auto` to `cpu_scipy`.
11. If `[fpga] enable = 1` and the `[fpga] bitstream` file exists, run
   `_daq_core/fpga_loader.py`. That module has no CLI entry point, so no
   bitstream is loaded. If `[gpu] enable = 1`, run `_daq_core/gpu_init.py` as
   a probe. On failure either step only prints `... continuing with CPU-only
   mode`. Neither step changes the `[offload]` engine selection. A CPU fallback
   happens later, at runtime, inside the engine factories.
12. Create `_logs/inst<N>/pids/` and delete stale PID files there.
13. Resolve the core layout and run the core guard and `[host] preflight`
   (hardware, or synthetic with `--rt`).
14. Launch the processes in the background and write one PID file per
   process. With `instance_registry = 1`, write the registry record.

The start script returns once every stage is launched. A stage that dies
later does not change its exit code. Check the stage logs, or run the chain
under [`daq_supervise.sh`](#supervision-and-systemd).

### Real-time layout

`daq_start_sm.sh` gives each process a tiered `SCHED_FIFO` priority (`chrt -f`)
and pins it to cores with `taskset`. Without `--rt`, `daq_synthetic_start.sh`
does not: only `decimate.out` runs real-time (`chrt -f 99`), and every other
process runs at normal priority with no CPU pinning. With `--rt` it applies the
hardware tiers, with the synthesizer in rtl_daq's slot.

| Process | Hardware: priority | Hardware: core (instance 0, 4+ cores) | Synthetic |
|---|---|---|---|
| `rtl_daq.out` / synthesizer | 99 | 0 | normal, unpinned |
| `rebuffer.out 0` | 94 | 1 | normal, unpinned |
| `decimate.out` | 92 | 1 (cache locality with rebuffer) | `chrt -f 99`, unpinned |
| `delay_sync.py` | 90 | 2 (core 1 on hosts with fewer than 3 cores) | normal, unpinned |
| `iq_server.out` (eth only) | 88 | 2 (unpinned on hosts with fewer than 4 cores) | normal, unpinned |
| `hw_controller.py` (via `sudo`) | 82 | 3 (unpinned on hosts with fewer than 4 cores) | normal, unpinned |

The tier priorities are fixed in the script. `[host] rt_priority_offset`
lowers all of them by the same amount. By default instance N uses cores
`(N x 4 + 0..3) mod nproc`, so two instances on an 8-core host do not stack
their RT processes on the same cores. When cores must be shared, the script
prints a `WARN:` line and continues.

PID files land in `_logs/inst<N>/pids/`: `rtl_daq.pid` (or `synthesizer.pid`),
`rebuffer.pid`, `decimate.pid`, `delay_sync.pid`, `hw_controller.pid` and,
with eth output, `iq_server.pid`. Instance 0 logs stay at the flat
`_logs/*.log` paths. Instance N > 0 logs to `_logs/instN/*.log`.

### Stopping

| Command | Behaviour |
|---|---|
| `./daq_stop.sh` | Global stop: PID files of this copy first, then a host-wide name sweep (SIGTERM, 2 s grace, SIGKILL sweep). Stops every DAQ chain on the host |
| `./daq_stop.sh <N>` | Stops instance N only, through its PID files |
| `./daq_stop.sh --legacy` | Sends SIGRT(64) instead of SIGTERM, for old binaries. All C binaries still trap signal 64 |

### Launcher rework: registry, locks, guards and host layout

<!-- verify-after: WP-12 -->
The launch, stop and supervise scripts share a POSIX sh library,
`Firmware/daq_common.sh`. Its features are opt-in through a new optional
`[host]` section. With every `[host]` key absent, the scripts run today's
commands, cores and priorities in today's order, open no extra file
descriptors and export no new variables.

| `[host]` key | Default | Effect |
|---|---|---|
| `cpu_cores` | empty | Exactly 4 cores for tiers T0 (rtl_daq or synthesizer), T1 (rebuffer + decimate), T2 (delay_sync + iq_server), T3 (hw_controller). Empty = `(N x 4 + k) mod nproc` |
| `delay_sync_cores` | empty | A wider `taskset` list for delay_sync only, so `[offload] fft_workers > 1` can run in parallel |
| `rt_priority_offset` | `0` | Subtracted from every tier (99/94/92/90/88/82). Range 0-81 |
| `allow_core_sharing` | `warn` | `warn`, `refuse` (exit 3 on overlap with a live instance) or `allow` |
| `host_tuning` | `always` | `always` = today's commands; `1` = `util/host_tuning.sh --apply`; `auto` = as `1` unless `heimdall-host-tuning.service` is active or another instance is live; `0` = skip |
| `preflight` | `off` | `warn` or `strict` runs `daq_preflight.py --plus-live` before launch; `strict` + failures = exit 4 |
| `instance_registry` | `0` | `1` writes registry records and takes the instance lock and startup mutex |
| `instance_guard` | `legacy` | `refuse` refuses or restarts duplicate starts (needs `instance_registry = 1`) |
| `usb_irq_core`, `usb_controller` | empty | Passed to `irq_tuning.sh --usb-core` / `--controller` when set |

**Host registry.** With `instance_registry = 1` the start script writes
`$RUN/inst<N>.json` and `$RUN/inst<N>.env` after the last stage is launched.
`$RUN` is `$HEIMDALL_RUN_DIR` (default `/run/heimdall`), else
`/tmp/heimdall-run-<uid>` when that directory cannot be written. The record
holds the instance id, mode (`hw` or `synthetic`), working directory, ports,
cores, serials, database directory and the PID of every stage. The script also
writes the resolved `$RUN` path into `_logs/inst<N>/pids/run_dir`.
`heimdall-ctl instances` and `ini_checker.py ... attached` read the registry.
The registry is a host-wide view only when every instance on the host sets
`instance_registry = 1` and all starts run as the same uid.

**Instance lock.** The start script opens `$RUN/inst<N>.lock` on fd 9 and
takes `flock(LOCK_EX)` before it touches any FIFO or shared-memory object.
Every stage inherits fd 9, so the lock means "some stage of instance N is
alive". The kernel releases it when the last stage exits. There is no guard
process. hw_controller does not hold it, because `sudo` closes inherited fds.

**Startup mutex.** `$RUN/inst<N>.start` on fd 8 serialises two concurrent
starts of the same instance. Stages never inherit fd 8.

**Guard modes.**

| Situation | `instance_guard = legacy` | `instance_guard = refuse` |
|---|---|---|
| Same copy started again | `WARN:`; the port check stops the old chain (today) | Stops instance N and restarts it |
| Another copy runs the same N | `WARN:` naming it; the port check stops all DAQ processes (today) | Exit 3 before any FIFO is touched, unless `--takeover` |
| Concurrent start of the same N | waits up to 60 s for the mutex | second start exits 3 |
| Busy ports | global `./daq_stop.sh` | scoped `./daq_stop.sh N` |

With `HEIMDALL_INSTANCE_GUARD=refuse` in its environment, delay_sync checks at
start that it inherited the instance lock. If not, it logs
`instance <N> started without its instance lock; refusing` and exits 3 before
it opens any shared-memory interface. This exit is not the start script's
exit code. The start script launches delay_sync in the background and has
already returned 0. The refusal shows up in the delay_sync log, and under
`daq_supervise.sh` as supervisor exit 13 (dead delay_sync stage).

**Start order.** The numbered list in
[Startup orchestration](#startup-orchestration) is the order the library
runs.

**Exit codes of both start scripts.**

| Code | Meaning |
|---|---|
| 0 | Started (every stage launched) |
| 1 | Config or FIR-designer failure, or ports still busy after 10 rounds |
| 3 | Guard refusal (`instance_guard = refuse`, `allow_core_sharing = refuse`) |
| 4 | Pre-flight failure with `preflight = strict` (synthetic: only with `--rt`) |

**Synthetic script additions.** `daq_synthetic_start.sh` keeps today's launch
tiers unless `--rt` is passed. `--rt` applies the hardware tiers with the
synthesizer in rtl_daq's slot. `--rt` implies `--pace` and also runs the core
guard and `[host] preflight`. Otherwise the script forwards `--pace`,
`--blocks N` and `--seed S` to the synthesizer only when it received them.

```
./daq_synthetic_start.sh [--rt] [--pace] [--blocks N] [--seed S]
                         [--takeover] [--no-host-tuning]
```

**New stop forms.**

| Command | Behaviour |
|---|---|
| `./daq_stop.sh <N> --clean` | After the stop, removes instance N's FIFOs, shm segments and relay files. Keeps `iq_track_lock`, `fir_coeffs` and `fence_term`. Refused (exit 3) while a stage of N is alive |
| `./daq_stop.sh <N> --reset-fence` | Removes `<P>fence_term`. Refused (exit 3) while a stage of N is alive |
| `./daq_stop.sh --this-copy` | Name sweep limited to processes whose working directory is this copy |
| `./daq_stop.sh --all` | The global stop plus a SIGKILL pass for delay_sync and hw_controller and removal of every registry entry |
| `./daq_stop.sh --federation` | The selected form, then `./federation_stop.sh all` |

The no-argument `./daq_stop.sh` stays the host-wide global stop.

## Sync state machine

delay_sync walks the calibration FSM once per frame and writes the result into
the header `sync_state` field. The mapping from states to values is
many-to-one:

| `sync_state` | delay_sync state |
|---|---|
| 0 | every dummy frame, and the initial value |
| 1 | `STATE_INIT` |
| 2 | `STATE_SAMPLE_CAL` |
| 3 | `STATE_SYNC_WAIT`, `STATE_FRAC_SAMPLE_CAL`, `STATE_FRAC_SYNC_WAIT` |
| 4 | `STATE_IQ_CAL` |
| 5 | `STATE_TRACK_LOCK` |
| 6 | `STATE_TRACK` |

```
                         +----------------- sync lost (maximum_sync_fails) ----------------+
                         |                  or RF centre frequency changed                 |
                         v                  or RECL (en_wire_recal = 1, any state)         |
  +------------+  CAL frame   +------------------+  integer tune sent  +-----------------+ |
  | STATE_INIT |------------->| STATE_SAMPLE_CAL |-------------------->| STATE_SYNC_WAIT | |
  |    (1)     |              |       (2)        |<--- dummy frame ----|       (3)       | |
  +------------+              +------------------+                     +-----------------+ |
                                 |          ^                                              |
              all integer delays |          +--------------------------+                   |
              are zero           |                                     |                   |
                                 v                                     |                   |
  +-----------------------+  frac tune sent  +----------------------+  |                   |
  | STATE_FRAC_SAMPLE_CAL |----------------->| STATE_FRAC_SYNC_WAIT |  |                   |
  |          (3)          |<-- dummy frame --|         (3)          |  |                   |
  +-----------------------+                  +----------------------+  |                   |
        | all fractional delays within tolerance                       |                   |
        v                                                              |                   |
  +--------------+  any dynamic range < 20 dB (en_iq_cal = 1)          |                   |
  | STATE_IQ_CAL |-----------------------------------------------------+                   |
  |     (4)      |                                                                         |
  +--------------+                                                                         |
        | sample + IQ sync ok (en_iq_cal = 0: at once)                                     |
        v                                                                                  |
  +------------------+  DATA frame  +-------------+                                        |
  | STATE_TRACK_LOCK |------------->| STATE_TRACK |----------------------------------------+
  |       (5)        |              |     (6)     |
  +------------------+              +-------------+
```

Transition rules, as implemented in `delaySynchronizer._fsm_step()`:

| From | To | Condition |
|---|---|---|
| `INIT` | `SAMPLE_CAL` | A CAL frame arrives. Other frames keep the state |
| `SAMPLE_CAL` | `SYNC_WAIT` | A non-zero integer delay was measured and the ZMQ `s` tune was confirmed |
| `SAMPLE_CAL` | `FRAC_SAMPLE_CAL` | Every integer delay is zero |
| `SAMPLE_CAL` | `SAMPLE_CAL` | A correlation peak has less than 20 dB dynamic range, or the tune transaction failed |
| `SYNC_WAIT` | `SAMPLE_CAL` | The next dummy frame (the tuners report the change) |
| `FRAC_SAMPLE_CAL` | `FRAC_SYNC_WAIT` | A fractional delay exceeds the fixed tolerance of 0.03 samples and the tune was confirmed |
| `FRAC_SAMPLE_CAL` | `IQ_CAL` | Every fractional delay is within tolerance |
| `FRAC_SYNC_WAIT` | `FRAC_SAMPLE_CAL` | The next dummy frame |
| `IQ_CAL` | `SAMPLE_CAL` | Any channel's dynamic range is below 20 dB (sample sync lost). Checked only with `[calibration] en_iq_cal = 1` |
| `IQ_CAL` | `TRACK_LOCK` | Sample sync holds and the IQ differences are within the amplitude and phase tolerances. Otherwise the corrections are updated and the state repeats. With `en_iq_cal = 0` this transition happens on the first IQ_CAL frame |
| `TRACK_LOCK` | `TRACK` | The first DATA frame (the noise source is off) |
| `TRACK` | `INIT` | The sync-fail counter reaches `[calibration] maximum_sync_fails`, or the RF centre frequency changed |
| any | `INIT` | A wire recalibration request (`RECL`, opt-in `[calibration] en_wire_recal = 1`) |

A dummy frame also resets the instantaneous sync-fail counter, because a new
noise burst follows.

Consumers rely on the numbering:

- `sync_state >= 5` means locked. The status server reports
  `calibration_status: locked` from 5, `calibrating` from 2, `lost` below.
- hw_controller enables the noise source while `sync_state < 5`, counts
  calibration bursts at 6 and triggers recalibration on drift. It reads
  `sync_state` only from non-dummy frames.
- Orientation motion is gated on `sync_state >= min_sync_state` (default 5)
  **and** `noise_source_state == 0`.

Calibration details: sample-delay estimation uses cross-correlation over
`corr_size` samples. Fractional residuals are corrected by per-channel
sampling-rate tuning (ZMQ `s`; the values are fractional rate offsets). IQ
amplitude/phase correction comes from the dominant eigenvector of the spatial
correlation matrix. See
[theory_coherent_acquisition.md](theory_coherent_acquisition.md).

Opt-in calibration keys that change the FSM's side effects but not its
transitions: `[calibration] track_decimation` (check every k-th DATA frame in
`TRACK`), `cal_timeout_frames` (warn and emit `cal_timeout` after k CAL frames
in the calibration states), `invalidate_on_overrun` (clear the sync flags on
frames that saw a USB overrun).

## Instances and namespacing

An instance is one coherent chain: one set of tuners, one set of processes,
one calibration. Several instances can run on one host. They never share
coherence. `[federation] instance_id` (N) selects the prefix and the port
offset. N is read only from the instance's own INI.

| Resource | Instance 0 | Instance N > 0 |
|---|---|---|
| FIFOs | `_data_control/fw_decimator_in` ... | `_data_control/instN_fw_decimator_in` ... |
| Shared memory | `/dev/shm/decimator_in_A` ... | `/dev/shm/instN_decimator_in_A` ... |
| Relay and control files | `_data_control/orientation_state` ... | `_data_control/instN_orientation_state` ... |
| FIR coefficients | `_data_control/fir_coeffs.txt` | `_data_control/instN_fir_coeffs.txt` |
| Ports 1130, 5000-5003 | base ports | base + N x `port_stride` (default 100) |
| Logs | `_logs/*.log` | `_logs/instN/*.log` |
| PID files | `_logs/inst0/pids/` | `_logs/instN/pids/` |
| Database | `[database] db_dir` | same `db_dir`, unless `[database] instance_subdir = 1` (then `<db_dir>/instN`) |
| `hw_caps.json` | `_data_control/hw_caps.json` | `_data_control/instN_hw_caps.json` |

The coordinator (6000) and router (7000, 7001) ports are service ports and do
not take the instance offset. With stride 100, instance ids 0-9 are safe on one
host. Ids 10 and 20 collide with the service ports. ini_checker reports the
collision. See [networking_and_time_sync.md](networking_and_time_sync.md).

Each stage reads `daq_chain_config.ini` and `_data_control/...` relative to
its working directory. That is why every instance needs its own working
directory.

### Instance directory model

<!-- verify-after: WP-14 -->
`util/provision_instance.py` creates one working directory per instance N > 0
under one install root. The default is a dry run that prints the directory
plan, the INI diff and the port table. `--write` executes the plan.

```
sudo python3 util/provision_instance.py --instance 1                     # dry run
sudo python3 util/provision_instance.py --instance 1 --write
sudo python3 util/provision_instance.py --instance 0 --in-place --write  # instance 0 in <root>/Firmware
sudo python3 util/provision_instance.py --instance 1 --refresh --write   # re-run on an existing dir
```

| Exit | Meaning |
|---|---|
| 0 | ok (or dry run ok) |
| 1 | validation or write failed |
| 2 | usage |
| 3 | destination exists (without `--force` / `--refresh`) |
| 4 | instance live (`--refresh` without `--force`) |

On a multi-instance host, provision instance 0 with `--in-place --write` too.
`daq_supervise.sh` refuses (exit 2) to supervise an instance that runs with
`instance_guard = legacy` while another instance is live, and tells you to run
this command.

The resulting layout:

```
<root>/                          install root, e.g. /opt/heimdall; instance 0 runs in <root>/Firmware
<root>/instances/<N>/
    util          -> <root>/util
    config_files  -> <root>/config_files
    Firmware/
        _daq_core  _testing  _calibration  _fpga_gateware   -> symlinks into <root>/Firmware
        ini_checker.py  fir_filter_designer.py  daq_preflight.py  -> symlinks
        daq_start_sm.sh  daq_synthetic_start.sh  daq_stop.sh  daq_common.sh
        daq_supervise.sh  federation_start.sh  federation_stop.sh -> symlinks
        daq_chain_config.ini          real file, generated
        _data_control/  _logs/  _db/  real, private directories
```

Code is shared through the symlinks. Config, control files, logs and the
database are private. No process needs a config-path option. One `make` in
`<root>/Firmware/_daq_core` updates every instance. `--mode copy` makes a full
copy instead of symlinks. Start instance N from its own directory:

```
cd <root>/instances/<N>/Firmware && sudo ./daq_start_sm.sh
```

### Identity

| Identity | Source | Used for |
|---|---|---|
| `instance_id` | `[federation] instance_id` | Prefix and port offset; unique per host |
| `unit_id` | `[hw] unit_id`, or `auto` = instance_id | Header bytes 24-27 (the physical unit) |
| node key | membership entry `host:instance_id`, host lower-cased | Key of every coordinator and router table |
| `node_id` | `[federation] node_id`, default `<short hostname>:<N>` | Label in STATUS, events, REGISTER, DB metadata and (opt-in) header slot 108 |
| `cluster_id` | `[federation] cluster_id`, default `default` | Peers with a different cluster id are rejected |

## Supervision and systemd

<!-- verify-after: WP-12 -->
`Firmware/daq_supervise.sh` runs one instance and watches it:

```
./daq_supervise.sh [--instance N] [--synthetic] [--interval 2]
                   [--healthcheck auto|on|off] [--grace 60]
                   [--stall-sec auto|SECONDS] [--allow-legacy-guard]
                   [-- START_SCRIPT_ARGS...]
```

| Option | Default | Effect |
|---|---|---|
| `--instance N` | the INI's `instance_id` | Must match `[federation] instance_id` of the INI in the cwd, else exit 2 |
| `--synthetic` | off | Runs `./daq_synthetic_start.sh` instead of `./daq_start_sm.sh` |
| `--interval` | `2` s | PID and health check period (at least 1) |
| `--healthcheck` | `off` | `auto` checks only when the status server is enabled; `on` without it exits 2 |
| `--grace` | `60` s | Health failures before this time after start are ignored |
| `--stall-sec` | `auto` | Stall threshold for `frame_count` (see below) |
| `--allow-legacy-guard` | off | Continue with `instance_guard = legacy` while another instance is live |
| `-- ARGS...` | none | Passed to the start script, for example `-- --takeover` or `-- --rt` |

- It runs `./daq_start_sm.sh` (or `./daq_synthetic_start.sh` with
  `--synthetic`) and propagates a nonzero start exit code (1, 3 or 4).
- It always exports `HEIMDALL_SCOPED_STOP=1`. A busy-port round of the start
  script then runs `./daq_stop.sh N` instead of the global stop, so a
  supervised restart never kills other instances.
- The PID files in `_logs/inst<N>/pids/` are the source of truth. Every
  `--interval` seconds each PID is checked for life and role.
- The health check is off by default, so a hand-run `./daq_supervise.sh`
  does no health check and never exits 16. systemd units should pass
  `--healthcheck auto`. When enabled it sends `STATUS` to the instance's
  status port (`[monitoring] status_server_port` + offset). That needs
  `[monitoring] en_monitoring = 1` and `en_status_server = 1`.
- A check fails when STATUS is unreachable, when `pipeline_health` is stale,
  or when `frame_count` stays unchanged for `--stall-sec` seconds.
  `--stall-sec auto` is `3 x k_eff x frame period + 30` s. The stall check is
  skipped (reachability only) when the STATUS snapshot is never refreshed or
  the frame period is unknown. A failure must persist for 3 consecutive checks
  after `--grace` seconds.
- On the first dead stage it runs `./daq_stop.sh N` and exits with 10
  (rtl_daq or synthesizer), 11 (rebuffer), 12 (decimate), 13 (delay_sync),
  14 (hw_controller) or 15 (iq_server). A failed health check exits 16.
  SIGTERM stops the instance and exits 0.
- With `instance_guard = legacy` on a host where another instance is live, it
  exits 2 unless `--allow-legacy-guard` is given.
- Exit 2 also covers usage errors and misconfiguration (a bad flag value, an
  `--instance` mismatch, `--healthcheck on` with the status server off).

`packaging/systemd/` ships these units:

| Unit | What it runs |
|---|---|
| `heimdall-daq.service` | instance 0, oneshot (`daq_start_sm.sh`) |
| `heimdall-daq@.service` | instance N > 0, oneshot (template) |
| `heimdall-daq-supervised.service` | instance 0, supervised: `daq_supervise.sh --instance 0`, run from `<root>/Firmware`, `Restart=on-failure`, `ExecStopPost=daq_stop.sh 0 --clean` |
| `heimdall-daq-supervised@.service` | instance N > 0, supervised (template): `daq_supervise.sh --instance %i`, `Restart=on-failure`, `ExecStopPost=daq_stop.sh %i --clean` |
| `heimdall-coordinator.service` | federation coordinator (`federation_start.sh coordinator --foreground`) |
| `heimdall-iq-router.service` | federation IQ router (`federation_start.sh router --foreground`) |
| `heimdall-host-tuning.service` | host tuning, oneshot (`util/host_tuning.sh --apply`) |
| `heimdall-daq-watchdog.service` + `heimdall-daq-watchdog.timer` | optional watchdog (`util/heimdall_watchdog.py`), run every 30 s by the timer, that restarts a supervised chain whose status server keeps reporting a failure |

The oneshot and supervised units of one instance conflict, so starting one
stops the other. `heimdall-daq.service` and `heimdall-daq@.service` are still
oneshot units. See
[packaging/systemd/README.md](../packaging/systemd/README.md) for deployment
and for choosing a supervision mode.

## Federation components

Federation is opt-in. Nothing below runs unless the listed keys are set or the
process is started by hand.

| Component | File | Runs in | Ports | Enabled by |
|---|---|---|---|---|
| Health monitor | `federation_health.py` (`FederationHealth`) | a thread in delay_sync | polls peers' status port | `[federation] en_federation = 1` and one of `peer_list`, `cluster_members` or `register_with_coordinator = 1` |
| Registrar | `federation_health.py` (`CoordinatorRegistrar`) | a thread in delay_sync | sends `REGISTER` to :6000 | `register_with_coordinator = 1` |
| Coordinator | `federation_coordinator.py` | own process | 6000 | started by hand, by `federation_start.sh coordinator` or by `heimdall-coordinator.service` |
| Election | `federation_election.py` | threads in the coordinator | `LEADER`, `PREVOTE`, `LEASE` on :6000 | `[federation] coordinator_candidates` non-empty |
| Scheduler | `federation_scheduler.py` | the coordinator | per-member `SCHD` (or `SCHB`/`SCHC`/`SCHE` chunks) to :5001 | a master schedule loaded on the coordinator |
| IQ router | `federation_iq_router.py` | own process | 7000 output, 7001 stats | started by hand or by `federation_start.sh router` |
| Fleet CLI | `util/heimdall_ctl` (`fleet`, `router`, `instances`) | client | | |

The health monitor also runs an advisory election inside each instance: the
lowest healthy `instance_id` among peers and self. It emits
`coordinator_elected` events and does not control the coordinator.

```
 host A                                      host B
 +-------------------------------+           +-------------------------------+
 | instance 0          instance 1|           | instance 0                    |
 | :5000 :5001 :5002   :5100 ... |           | :5000 :5001 :5002             |
 +---|-----|-----|-------|-------+           +---|-----|-----|---------------+
     |     |     |       |                       |     |     |
     |     |  STATUS polls, REGISTER             |     |     |
     |     |     +----------------+--------------------------+
     |     |                      v                    |
     |     |   XACK + verbs  +------------------+      |
     |     +<----------------| coordinator :6000|----->+  (fan-out, schedule)
     |                       | (leader of N     |
     |                       |  candidates)     |
     |                       +------------------+
     |  streaming/IQDownload                           |
     +---------------------->+------------------+<-----+
                             | IQ router :7000  |----> DSP clients (several)
                             |   stats :7001    |
                             +------------------+
```

### Coordinator protocol 2

<!-- verify-after: WP-08 -->
The coordinator speaks protocol version 2 (`VERSION` reply
`{"ok":true,"protocol":2,"fw_version":...}`). Each TCP connection carries one
request and one reply. The legacy verbs `PING`, `STATUS`, `FREQ`, `GAIN`,
`INSTANCE` and `REBALANCE` keep their framing (no trailing newline). New verbs
(`VERSION`, `LEADER`, `HEALTH`, `NODES`, `MEMBERS`, `REGISTER`, `ALL`, `NODE`,
`FANOUT`, `RECAL`, `SCHEDULE`, `ROUTER` and others) are newline-terminated and
always reply with an `ok` key. Bodies follow the request line with an `@<n>`
length suffix. With `[federation] auth_token` set, every request must start
with `AUTH <token> `.

Fan-out runs in parallel with a deadline fixed when the request arrives.
Before each per-member command the coordinator sends `XACK` on the same
connection, so it learns the member's channel count, capabilities and
timeouts and gets extended acks. Mutating requests are serialised; a request
that waits too long replies `{"error":"busy","retry_in_sec":1}`.

**Election and fencing.** With `coordinator_candidates` set, the candidates
elect a leader with time-bounded leases (`election_interval_sec` 2.0 x
`election_missed_polls` 3 = 6 s lease). Only the leader executes mutating
verbs; followers reply `not_leader`. Each leadership has a term, persisted in
`<state_dir>/term`. The term travels in every `XACK`. A member stores the
highest term it accepted in `_data_control/<P>fence_term` and refuses
mutating commands from a lower term with `stale_term`. Without candidates the
coordinator runs as one static coordinator (term 0).

| Item | Behaviour |
|---|---|
| `require_quorum = 0` (default) | Not consensus. Two leaders are possible during a network partition. Only member-side fencing protects the hardware: members reject the lower term with `stale_term` |
| `require_quorum = 1` | Set it with 3 or more candidates for a single leader |
| Term file | `<state_dir>/term`. With `state_dir` unset and candidates set: `_data_control/federation/term` |
| Unwritable state dir | The candidate becomes ineligible (`state_dir_unwritable`): it never claims and never grants |

**Access control.** The coordinator binds `[daq] listen_address` (default
`0.0.0.0`) and has no `auth_token` by default. It can fan out `FREQ` and
`GAIN` to every member. When :6000 is reachable beyond a trusted LAN, set
`[federation] auth_token` and a narrower `[daq] listen_address`. See
[networking_and_time_sync.md](networking_and_time_sync.md).

### IQ router

<!-- verify-after: WP-09 -->
The router reads several IQ streams and serves one combined stream to many
clients:

```
python3 _daq_core/federation_iq_router.py [--config PATH] [--sources LIST]
    [--output-port 7000] [--stats-port 7001] [--queue-frames 4]
    [--drop-policy oldest|newest|disconnect] [--max-clients 8]
    [--tag-mode instance|unit|node] [--connect-on-demand | --always-connect] ...
```

- Sources are `host:iid[@node_id]` (an iq_server, with the `streaming` /
  `IQDownload` handshake) or `router://host:port` (an upstream router's raw
  output, for chaining).
- Each client has a bounded queue (`--queue-frames`, default 4). A full queue
  drops per `--drop-policy`. A client that accepts no byte for
  `--send-timeout-sec` (2.0) is evicted. A slow client never stalls a source
  reader, so no instance drops frames because of a router client.
- `--tag-mode instance` (default) writes the source `instance_id` into
  `unit_id`. `node` stamps slots 106/107 instead; upstream routers in a chain
  must use it.
- `--connect-on-demand` (default for the CLI) connects to a source only while
  a client is attached, so an idle router does not hold an iq_server client
  slot.
- The worst-case memory is
  `frame_bytes x (queue_frames x max_clients + align_max_frames x sources + sources)`.
  The router prints it at start.

See [scaling_and_clustering.md](scaling_and_clustering.md) for topologies and
worked examples.

## Capacity model and pre-flight

<!-- verify-after: WP-13 -->
`Firmware/_daq_core/capacity_model.py` computes per-instance resource needs:
USB ingress, usbfs and ring memory, shared-memory object and mapping sizes,
frame period, egress per client, cores and RT tiers. Its frame sizes match
`frame_sizes.h` key for key; `test_frame_sizes.py` cross-checks them.

`Firmware/daq_preflight.py` compares those needs with the host:

```
python3 daq_preflight.py [--config daq_chain_config.ini ...] [--plus-live] [--add-instances N]
                         [--host-json FILE] [--dump-host-json] [--strict] [--json]
```

It reports `need / have / verdict` per resource. Checks include the
core-count rule (`4 x hardware instances > nproc` fails), port windows,
memory, `/dev/shm`, RLIMIT_MEMLOCK, USB bandwidth per bus and NIC speed.
Exit codes: 0 ok, 1 warnings, 2 failures. `heimdall-ctl capacity` runs it.
The start scripts run it only with `[host] preflight = warn` or `strict`. See
[capacity_planning.md](capacity_planning.md).

## Recording and replay

<!-- verify-after: WP-20 -->
**Planned, not yet in the tree.** The `.hcap` format and the tools in the
table below are designed but not implemented. Today `_testing/iq_recorder.py`
records `.iqf` files (options `--host --port --frames --min-block-index
--prefix --test-case --ctrl-file --out-dir`), and `_testing/iq_analyzer.py`
reads only `.iqf`. `iq_replay.py`, `iq_latency_probe.py` and
`daq_replay_start.sh` do not exist yet.

Captures use the `.hcap` container: a concatenation of
`[1024-byte header][exact payload]` frames. The payload length comes from the
header (`active_ant_chs x cpi_length x 2 x sample_bit_depth / 8`, 0 for dummy
frames). A JSON sidecar `<name>.hcap.json` records `format: "hcap-1"`, `kind`
(`post_sync_cf32` or `raw_u8`), host, instance id, firmware version, the full
INI text and the frame count.

| Tool | Role |
|---|---|
| `_testing/iq_recorder.py --hcap PATH` | Records a `:5000` stream (`--instance N` / `--port-stride S` select the port) |
| `_testing/iq_analyzer.py` | Reads `.hcap`; takes the sample rate from the header when `--fs` is absent |
| `_testing/iq_replay.py --serve-port P` | Serves a `post_sync_cf32` capture with the iq_server `streaming` / `IQDownload` protocol (`--realtime`, `--loop`) |
| `_testing/iq_replay.py --raw-to-stdout` | Emits a `raw_u8` capture for piping into `rebuffer.out 0` |
| `daq_replay_start.sh` | Runs the chain from a `raw_u8` capture instead of tuners; refuses other kinds with exit 2 |
| `_testing/iq_latency_probe.py` | Connects as a client and reports frames, MB/s, latency against the header `time_stamp`, `cpi_index` gaps and slot-102 overrun deltas |

## Transport and offload abstractions

Both are vtable-dispatched C interfaces with factory functions. The defaults
behave the same as the firmware before the abstraction.

### Transport (`transport.h` / `transport.c`)

```c
struct transport_handle* transport_create(name, size, is_producer,
                                          flow_control, instance_id, type);
```

`transport_type_t`: `TRANSPORT_SHM` (0, default, a wrapper over
`sh_mem_util.c`), `SPI` (1), `PCIE` (2), `USB3` (3), `NET` (4). Optional
drivers are compiled in only with `-DHAS_SPI_TRANSPORT`,
`-DHAS_PCIE_TRANSPORT`, `-DHAS_USB3_TRANSPORT` or `-DHAS_NET_TRANSPORT`
(commented out in the Makefile by default). An unknown or unavailable type
falls back to shm. The vtable (`init_producer`, `init_consumer`, `destroy`,
`get_write_buf`, `submit_write`, `get_read_buf`, `release_read`,
`send_terminate`) mirrors the shmem protocol: buffer index 0/1, `3` = frame
dropped, `255` = TERMINATE, negative = error. `buffer_size` includes the
1024-byte header. `num_buffers` is always 2. Flow control is
`FLOW_BACKPRESSURE` or `FLOW_DROP`.

Python mirrors: `transportIface.py` (`TransportProducer` / `TransportConsumer`
wrapping `shmemIface.py`, attribute-compatible with `outShmemIface` /
`inShmemIface`, including the frozen `destory_sm_buffer` typo).

### Offload (`offload.h` / `offload.c`)

```c
struct fir_engine*     fir_engine_create(offload_engine_t type);      /* OFFLOAD_AUTO = detect */
struct convert_engine* convert_engine_create(offload_engine_t type);
```

| Engine | enum | Source | Compiled when | Notes |
|---|---|---|---|---|
| NEON / Ne10 | `OFFLOAD_CPU_NEON` (0) | `offload_cpu_neon.c` | `ENGINE=ne10` lane | 32-bit ARM only (libNE10.a is armv7). Not built on AArch64, which uses KFR |
| KFR | `OFFLOAD_CPU_KFR` (1) | `offload_cpu_kfr.c` | `ENGINE=kfr` lane (default on x86_64 and aarch64) | Per-channel, per-component KFR plans; allocation-free reset |
| FPGA | `OFFLOAD_FPGA` (2) | `offload_fpga.c` | `-DHAS_FPGA_OFFLOAD` | SPI + GPIO DRDY. Each channel is processed synchronously: write I, write Q, wait for DRDY, read I, read Q. Three staging buffers are allocated, but no data passes through them, so there is no triple-buffered pipelining |
| GPU | `OFFLOAD_GPU` (3) | `offload_gpu.c` | `-DHAS_GPU_OFFLOAD` | VideoCore VI mailbox QPU, software fallback |
| Generic | `OFFLOAD_CPU_GENERIC` (4) | `offload_cpu_generic.c` | always in the kfr/ne10 lanes (`-DHAS_GENERIC_OFFLOAD`, runtime-selectable); sole engine under `-DOFFLOAD_GENERIC_ONLY` (`ENGINE=generic`) | Dependency-free C99, bit-exact direct-form FIR |

Selection: `[offload] fir_engine` accepts `auto`, `neon`/`cpu_neon`,
`kfr`/`cpu_kfr`, `generic`/`cpu_generic`, `fpga` and `gpu`. `auto` picks the
platform engine compiled into the binary. Python-side engines live in
`offload_engines.py` (FFT and correlation) and `offload_gpu.py`.
`[offload] fft_workers` and `xcorr_workers` set the delay_sync FFT worker
counts.

## RF front-end and orientation subsystems

Three optional subsystems are owned by `hw_controller.py`. They are disabled
by default and launch no new processes.

- **`gain_budget.py`** (`[amplification]`): link-budget math for total system
  gain, cascaded Friis noise figure and P1dB compression headroom, plus
  `RfFrontendConfig` / `RfFrontendParser`. External LNA gain is continuous dB
  in a separate array (`hw_controller.ext_lna_gains_db`). It never goes
  through the R820T `valid_gains` quantizer and is never written into
  `self.gains` or the header `if_gains[]`, which drive the gain-lock equality
  checks. Total gain is computed after the `unified_gain_control` clamp.
  Compression is advisory only: it augments the overdrive picture and never
  changes gains.
- **`antenna_profile.py`** (`[antenna]`): element gain (dBi), beamwidths,
  polarization, cable and connector loss, boresight offsets. The gain budget
  and the orientation boresight correction share it.
- **`orientation_controller.py`** and **`rotator_controller.py`**
  (`[orientation]`): a per-frame state machine (IDLE=0, SLEWING=1, SETTLED=2,
  SCANNING=3) ticked beside the scheduler in hw_controller's `STATE_IQ_CAL`.
  It drives a backend abstraction (`mock`, `gs232`, `pwm_servo`,
  `i2c_pantilt`). The factory never raises; it falls back to `mock` so the
  chain always starts. Bearing sources: external `ORNT` commands, fixed or park
  config, or autonomous scan-and-peak (`SCAN`). Scan-and-peak maximizes the
  aggregate channel power that delay_sync stamps into header slot 101 (mean
  per grid point over `scan_dwell_frames` after discarding `settle_frames`).
  Motion is gated on sync state and on the noise source being off. Rotator
  hardware is per instance and is not fanned out by federation.

Runtime bias-tee: the `BIAS` control frame becomes the ZMQ `b` message.
`rtl_daq.c` applies `rtlsdr_set_bias_tee_gpio(dev, m+1, ...)` per channel.
GPIO 0 is reserved for the noise source, so bias power never collides with
calibration. Bias changes wait while a calibration noise burst is active.
Opt-in: `[amplification] apply_bias_tee_at_init = 1` (needs
`en_bias_tee_runtime = 1`) sends one bias-tee message at start when the
configured state differs from `[hw] en_bias_tee`.

delay_sync stamps the telemetry of all three subsystems into the v8 header
reserved slots. See [protocols.md](protocols.md).

## Header telemetry slots added in this release

Slots 103-113 of `reserved[]` are defined in lockstep in `iq_header.h` and
`iq_header.py`. The header stays 1024 bytes and `header_version` stays 8. Each
slot is 0 unless its opt-in is enabled.

| Slot | Content | Written by | Enabled by |
|---|---|---|---|
| 103 | router sequence number | router | `--stamp-seq` |
| 104-105 | router receive time, Unix ms (LO, HI) | router | `--stamp-rx-ts` |
| 106-107 | source instance_id + 1, crc32 of the node key | router | `--tag-mode node` |
| 108 | crc32 of `[federation] node_id` | delay_sync | `[federation] stamp_node_id = 1` |
| 109 | decimator drop count | decimate.out | `[monitoring] stamp_decimator_drops = 1` |
| 110-113 | calibration quality (signed milli-dB), IQ and sample compensation counts, recalibration generation | delay_sync | `[monitoring] stamp_cal_telemetry = 1` |
