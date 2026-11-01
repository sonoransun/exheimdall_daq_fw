# Configuration Reference

Each DAQ instance reads one file, `Firmware/daq_chain_config.ini`. Every key
below lives in that file unless its section says otherwise. A coordinator or
IQ router started with `--config` reads a separate `federation.ini` (see
[federation.ini: coordinator and router keys](#federationini-coordinator-and-router-keys)).

Related documents: [protocols.md](protocols.md) (wire formats behind many of
these keys), [scaling_and_clustering.md](scaling_and_clustering.md),
[networking_and_time_sync.md](networking_and_time_sync.md),
[capacity_planning.md](capacity_planning.md),
[hardware_setup.md](hardware_setup.md),
[deployment_recipes.md](deployment_recipes.md),
[operations_runbook.md](operations_runbook.md),
[tutorial_directional_df.md](tutorial_directional_df.md).

## Validation

`Firmware/ini_checker.py` validates a config. `daq_start_sm.sh` runs
`python3 ini_checker.py no_hw attached` and `daq_synthetic_start.sh` runs
`python3 ini_checker.py no_hw`. Both refuse to start on a nonzero exit.

```
python3 ini_checker.py [config_path] [no_hw] [attached] [--federation]
```

Tokens may come in any order. Any token that is not a keyword is the config
path (default `daq_chain_config.ini`).

| Token | Effect |
|---|---|
| `no_hw` | Skip every hardware probe (no `lsusb`, no `rtl_eeprom`). The check reads only the INI, so it gives the same result on every machine |
| `attached` | Also compare the configured serials with the RTL2832U receivers listed in sysfs (no device is opened) and with the live host registry (`$HEIMDALL_RUN_DIR/inst*.json`). `daq_start_sm.sh` passes it on every hardware start. The synthetic start script does not |
| `--federation` | Validate a coordinator or router `federation.ini` instead. Only a `[federation]` section is needed |

| Exit code | Meaning |
|---|---|
| `0` | Valid. Warnings may still be printed |
| `1` | At least one error, a missing mandatory section or key, or an unreadable file |

Tip: before a hardware start, run `python3 ini_checker.py no_hw attached` from
`Firmware/`. It gives the same result the start script will get, including the
serial checks against sysfs and the port and `db_dir` overlap checks against
live registered instances (a shared port is an error in new-key mode, else a
warning; a shared `db_dir` is a warning).

Errors go to stderr as `ERROR:root:<text>`. Warnings go to stderr as
`WARNING: <text>`. Warnings never change the exit code, and they are printed
even when there are errors. Every shipped preset passes `no_hw` with no
warning.

Example (instance 1 with federation on, `unit_id = 0`, a bad enum value):

```
$ python3 ini_checker.py b.ini no_hw
ERROR:root:[calibration] amplitude_tolerance_mode must be one of: legacy, symmetric. Currently it is: 'strict'
WARNING: [hw] unit_id = 0 on instance 1 with federation enabled: frames from two instances are indistinguishable (set [hw] unit_id = auto)
WARNING: a resident DoA client blocks every coordinator and heimdall-ctl command on this member (set [daq] ctl_max_connections = 4)
$ echo $?
1
```

Test hooks: `HEIMDALL_SYSFS_ROOT` (default `/sys/bus/usb/devices`),
`HEIMDALL_RUN_DIR` (default `/run/heimdall`) and `HEIMDALL_PROC_ROOT` (default
`/proc`) redirect the `attached` checks. None of them selects a config file or
an instance id.

## General rules

- **Core sections** (`[hw]`, `[daq]`, `[pre_processing]`,
  `[calibration]`, `[adpis]`, `[data_interface]`) must be present.
  `[meta]` is optional and informational. ini_checker does not read it.
- **Optional sections** are everything else. They default to disabled
  (`en_* = 0` / `enable = 0`).
- **Opt-in keys** are marked "opt-in" below. An absent opt-in key reproduces
  the behaviour of the firmware before the key existed. The shipped
  `daq_chain_config.ini` lists every opt-in key as a commented `;key = default`
  line.
- **To enable an opt-in key**, remove the leading `;` and set the value. Make
  sure the key sits under its section header. For `[host]`, also uncomment the
  `; [host]` line (it is commented out in the shipped file), or add `[host]`
  yourself. A key under the wrong header is silently ignored, and ini_checker
  does not flag it.
- Unknown sections and keys are ignored. They are not an error.
- Comma-list keys marked "per channel" need exactly `num_ch` entries.
- ini_checker accepts only `0` or `1` for the boolean opt-in keys.
- For an opt-in key whose default is empty, an empty value counts as absent.
  For a numeric, boolean or enum opt-in key, an empty value is an ini_checker
  error.
- At run time, `hw_controller.py` and `delay_sync.py` read a malformed opt-in
  value as its default and log one `WARNING`. `rtl_daq` exits fatally on an
  out-of-range `usb_ring_buffers`, `usb_async_transfers`, serial key or
  `antenna_switch_gpio`.

**New-key mode.** An INI is in new-key mode when it sets any of
`[federation] cluster_members`, `coordinator_candidates`,
`register_with_coordinator = 1`, any `router_*` key, `[host] instance_registry = 1`
or `[host] instance_guard = refuse`. In new-key mode, port-window problems
(overlapping instance ports, a port above 65535, a collision with the
coordinator or router port) are errors. Otherwise they are warnings. See
[networking_and_time_sync.md](networking_and_time_sync.md#13-safe-instance-id-range).

## [meta]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `ini_version` | int | `8` | Schema version. Informational: ini_checker does not enforce it. With `[monitoring] status_extended = 1` it is reported in STATUS |
| `config_name` | string | `kraken_default` | Free-form preset name |

## [hw]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `name` | string | `kraken5` | Hardware ID stamped into the header `hardware_id` (truncated to 16 bytes) |
| `unit_id` | int or `auto` | `0` | Header `unit_id`. `auto` stamps `[federation] instance_id` instead (see below) |
| `ioo_type` | int | `0` | Illuminator type (header field) |
| `num_ch` | int | `5` | Number of coherent receiver channels M. ini_checker accepts 1..31, because the 128-byte `:1130` control message holds 31 per-channel values. rtl_daq exits fatally outside 1..32 |
| `en_bias_tee` | int list, per channel | `0,0,0,0,0` | Bias tee state rtl_daq applies at start |

### [hw] opt-in keys

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `serial_base` | absent (= 1000) | int 0..99999968 | rtl_daq, ini_checker | Channel i opens the receiver whose EEPROM serial is `serial_base + i`. Unit k of a multi-unit host is programmed to `1000 + 100*k + i`. Program the serials with `util/set_unit_serials.sh --unit K` (see [hardware_setup.md](hardware_setup.md)) |
| `device_serials` | absent | exactly `num_ch` distinct decimal serials of 1-8 digits | rtl_daq, ini_checker | Explicit per-channel serials. Wins over `serial_base`. The whole line must fit in 199 bytes (rtl_daq's INI reader keeps no more), so drop the spaces after commas on long lists |
| `antenna_switch_gpio` | absent (= GPIO 23/24) | `none` or `A,B` (two distinct GPIO numbers 0..27) | rtl_daq (`USEPIGPIO` builds only) | Antenna-switch pins. `none` leaves pigpio uninitialised |

Setting `serial_base` or a non-empty `device_serials` turns on the stricter
serial checks:

- ini_checker requires `[daq] ctr_channel_serial_no` to be one of the
  configured serials. rtl_daq compares the serial strings, so write them
  without leading zeros.
- rtl_daq exits fatally when two attached devices carry the same configured
  serial. Without serial keys it only warns and keeps today's first-match
  behaviour.
- With the `attached` token, a configured serial that is not attached is an
  error.

**`unit_id = auto` for external consumers.** With `auto`, rtl_daq (and the
synthesizer and `hat_usb3_bridge`) stamp the header `unit_id` with
`[federation] instance_id`. A client that reads several instances can then
tell their frames apart. ini_checker warns when an instance with
`instance_id > 0` and `en_federation = 1` keeps `unit_id = 0`. The synthesizer
stamps `0` for an integer `unit_id` unless it runs with `--unit-id-from-config`.

## [daq]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `log_level` | int | `5` | 0=TRACE 1=DEBUG 2=INFO 3=WARN 4=ERROR 5=FATAL (C, rxi log). Python processes use `log_level × 10` for the `logging` module |
| `daq_buffer_size` | int | `262144` | Samples per channel per DAQ block. Must be a power of two (ini_checker). The wire size is 2× for interleaved u8 I/Q. librtlsdr transfer granularity is 512 bytes |
| `center_freq` | int (Hz) | `700000000` | Initial RF center frequency |
| `sample_rate` | int (Hz) | `2400000` | ADC sample rate |
| `gain` | int (tenths dB) | `0` | Initial tuner gain for all channels. Must be a valid R820T gain value |
| `en_noise_source_ctr` | bool int | `1` | Enable calibration noise-source control (GPIO 0 on the control channel) |
| `ctr_channel_serial_no` | int | `1000` | Serial number of the control-channel (noise source) dongle |
| `listen_address` | IPv4 string | `0.0.0.0` (optional key, absent by default) | Bind address of the `:5000` IQ server, `:5001` control server, `:5002` status server and `:5003` event PUB. A coordinator or router started with `--config` also binds it. Invalid values warn and fall back to `0.0.0.0`. ini_checker validates it with `inet_aton` |

### [daq] opt-in keys

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `zmq_listen_address` | absent (`*`) | `*`, `0.0.0.0`, `127.0.0.1`, `localhost` | rtl_daq, synthesizer | Bind address of the ZMQ REP socket (`1130 + offset`). `localhost` binds `127.0.0.1`. Other addresses are refused because hw_controller and delay_sync connect to `tcp://localhost` |
| `usb_ring_buffers` | `8` | int 2..64 | rtl_daq | Depth of the circular USB read buffer per channel |
| `usb_async_transfers` | `12` | int 2..64 | rtl_daq | librtlsdr asynchronous transfers in flight per channel |
| `ctl_max_connections` | `1` | int 1..64 | hw_controller | `:5001` concurrent connections. `1` keeps the sequential accept loop with `listen(4)`. `> 1` serves that many clients at once with `listen(16)`; further clients wait in the backlog |
| `ctl_request_timeout_sec` | `10.0` | float 1..600 | hw_controller | How long a queued command waits for the FSM to reach `STATE_IQ_CAL`. Advertised to XACK clients as `rto` (ms). On expiry the command is withdrawn (`deferred_timeout`) |
| `ctl_handler_max_sec` | `12.0` | float 1..120 | hw_controller | How long an XACK connection keeps waiting for a command that was already running when `ctl_request_timeout_sec` expired. Advertised as `hmx` (ms). On expiry the reply is `handler_timeout` (outcome unknown) |
| `ctl_xack_idle_sec` | `30` | float 0..86400 (0 = off) | hw_controller | Idle timeout for connections that sent XACK. Also enables TCP keepalive (10 s idle, 5 s interval, 3 probes). Connections without XACK are unaffected |
| `ctl_idle_timeout_sec` | `0` (off) | float 0..86400 | hw_controller | When > 0, the same idle timeout and keepalive for every `:5001` connection |

ini_checker warnings for these keys:

- `ctl_request_timeout_sec` below 2 × the frame period
  (`cpi_size × decimation_ratio / sample_rate`): control verbs would time out
  on a healthy chain.
- A frame period above 8 s.
- `ctl_max_connections = 1` with `en_federation = 1` or
  `register_with_coordinator = 1`: one resident DoA client blocks every
  coordinator and heimdall-ctl command.

The XACK handshake and the reply reasons are in [protocols.md](protocols.md).

## [pre_processing]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `cpi_size` | int | `1048576` | Output samples per channel per CPI frame (post-decimation). At most 2^23 (ini_checker): larger frames overrun the `decimator_out` / `delay_sync_iq` segments |
| `decimation_ratio` | int | `1` | FIR decimation ratio R |
| `fir_relative_bandwidth` | float 0..1 | `1.0` | Passband relative to output Nyquist |
| `fir_tap_size` | int | `1` | FIR tap count K. Must be > R when R > 1 (the designer rejects K ≤ R) |
| `fir_window` | string | `hann` | scipy window name for the filter design |
| `en_filter_reset` | bool int | `0` | Reset FIR state between frames |

### [pre_processing] opt-in keys

<!-- verify-after: WP-04 -->

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `size_segments_from_config` | `0` | 0/1 | decimate.out, iq_server.out | `0`: `decimator_out` is sized for the 2^23-sample worst case and iq_server maps the worst-case `delay_sync_iq`. `1`: both use `1024 + max(cpi_size, corr_size) × num_ch × 8` bytes, the size delay_sync already creates. Payload offsets do not change |

**Cross-references.** `fir_filter_designer.py` regenerates
`_data_control/fir_coeffs.txt` from these keys at every startup. Changing
`cpi_size` or `decimation_ratio` invalidates existing coefficients (the file's
length must equal `fir_tap_size`). ini_checker enforces
`cpi_size × decimation_ratio ≥ daq_buffer_size` (the CPI must span at least
one DAQ block) and `fir_tap_size > decimation_ratio` when the ratio is not 1.
`[pre_processing] fft_workers` is not read by any process; ini_checker warns
and points to `[offload] fft_workers`.

## [calibration]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `corr_size` | int | `65536` | Cross-correlation size for delay estimation. At most 2^23 and at most `cpi_size`. ini_checker warns when it is not a multiple of 1024 |
| `std_ch_ind` | int | `0` | Reference ("standard") channel index |
| `en_iq_cal` | bool int | `1` | Enable IQ amplitude/phase calibration |
| `amplitude_cal_mode` | string | `channel_power` | `default` / `disabled` / `channel_power` |
| `en_gain_tune_init` | bool int | `0` | Gain fine-tune at init |
| `gain_lock_interval` | int | `0` | Frames required for gain-lock confirmation |
| `unified_gain_control` | bool int | `0` | Clamp all channels to the minimum requested gain. Total system gain (link budget) is computed after this clamp |
| `require_track_lock_intervention` | bool int | `0` | Hold in STATE_TRACK_LOCK until the `_data_control/<P>iq_track_lock` file holds `1`. The file is read only when this key is 1, and re-opened on every check. A missing file means "not approved". `heimdall-ctl track-lock approve` writes it over the wire (TLKA) |
| `cal_track_mode` | int | `2` | See below |
| `cal_frame_interval` | int | `687` | Mode 2: locked frames between two noise-source bursts |
| `cal_frame_burst_size` | int | `10` | Mode 2: CAL frames per burst |
| `amplitude_tolerance` | int (dB) | `2` | Amplitude drift tolerance in dB. See `amplitude_tolerance_mode` |
| `phase_tolerance` | int (deg) | `1` | Phase drift tolerance |
| `maximum_sync_fails` | int | `10` | Track-loss threshold. See below |
| `iq_adjust_source` | string | `explicit-time-delay` | `explicit-time-delay` or `touchstone` (the latter needs scikit-rf) |
| `iq_adjust_amplitude` | float list | `0,0,0,0` | Per-channel amplitude adjustment (M−1 entries, relative to the reference) |
| `iq_adjust_time_delay_ns` | float list | `0, 0, 0, 0` | Per-channel time-delay adjustment, ns |

`<P>` is empty for instance 0 and `inst<N>_` for instance N > 0.

**`cal_track_mode`.** Both tracking modes check sample-delay sync and, when
`en_iq_cal = 1`, IQ sync. They differ in which frames they check.

| Value | Behavior |
|---|---|
| `0` | Calibration tracking disabled. Once locked, the sync flags stay set |
| `1` | Continuous tracking. The checks run on every DATA frame (every k-th with `track_decimation = k`) |
| `2` | Tracking on CAL frames only. While locked, hw_controller fires a noise-source burst of `cal_frame_burst_size` frames every `cal_frame_interval` frames, and delay_sync checks only those CAL frames |

**`maximum_sync_fails`.** delay_sync keeps a fail counter while tracking.

- A failed sample or IQ sync check adds 1.
- A passing check subtracts 1. The counter never goes below 0.
- Every DUMMY frame resets it to 0. DUMMY frames follow every noise-source
  switch and every tuner command, so in mode 2 each burst starts at 0.
- A frequency change also resets it.
- When the counter reaches `maximum_sync_fails`, sync is declared lost and
  calibration restarts from STATE_INIT.

It is a net count within one burst, not a count of consecutive failures. In
mode 2 it must not exceed `cal_frame_burst_size`. ini_checker warns when
`cal_frame_burst_size < maximum_sync_fails` with `cal_track_mode = 2`, because
a sync loss would then never be detected.

### [calibration] opt-in keys

<!-- verify-after: WP-06 -->

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `track_decimation` | `1` | int ≥ 1 | delay_sync | Mode 1 only: run the tracking check on every k-th DATA frame and reuse the last flags in between. CAL frames are always checked |
| `en_wire_recal` | `0` | 0/1 | hw_controller, delay_sync | Enables the `RECL` verb on `:5001`. RECL writes `_data_control/<P>recal_request`, and delay_sync then restarts calibration even when locked. With 0, RECL replies `disabled` and heimdall-ctl `recal` falls back to `INIT` |
| `defer_control_during_burst` | `0` | 0/1 | hw_controller | Hold FREQ, GAIN, EGAN and BIAS while the noise source is on. INIT and RECL are always held |
| `amplitude_tolerance_mode` | `legacy` | `legacy` / `symmetric` | delay_sync | `legacy`: one-sided test `abs(r) > 10^(amplitude_tolerance/20)` (it only flags amplitude rises; with the default 2 the threshold is 1.259). `symmetric`: `abs(20·log10(abs(r))) > amplitude_tolerance` (both directions) |
| `cal_stall_frames` | `0` (off) | int ≥ 0 | hw_controller | After k consecutive frames with the noise source on and `sync_state < 5`, emit `cal_stalled` once and set `cal_stalled: true` in `hwc_state`. No hardware action |
| `recal_on_gain_change` | `0` | 0/1 | hw_controller | Perform the RECL actions after every applied GAIN (reason token `gain_change`). Needs `en_wire_recal = 1`; otherwise hw_controller logs a WARNING and ignores it |
| `cal_timeout_frames` | `0` (off) | int ≥ 0 | delay_sync | After this many consecutive CAL frames in the calibration states, log one WARNING, emit `cal_timeout` and write a DB cal event. No hardware action |
| `invalidate_on_overrun` | `0` | 0/1 | delay_sync | When header slot 102 (USB ring overrun count) increases, clear `delay_sync_flag` and `iq_sync_flag` on that frame and the next `frames_after_overrun` frames |
| `frames_after_overrun` | `1` | int ≥ 0 | delay_sync | See `invalidate_on_overrun` |

`[calibration] xcorr_workers` is not read by any process; ini_checker warns and
points to `[offload] xcorr_workers`.

## [adpis]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_adpis` | bool int | `0` | Enable ADPIS hardware (analog phase/gain shifters via DAC) |
| `adpis_proc_size` | int | `8192` | hw_controller processing block size |
| `adpis_gains_init` | int list | `0,0,0,0,0` | Initial ADPIS gains |

## [data_interface]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `out_data_iface_type` | string | `shmem` | `shmem` (downstream DSP attaches to the `delay_sync_iq` ring) or `eth` (launch `iq_server.out` on `:5000`) |

### [data_interface] opt-in keys

<!-- verify-after: WP-04 -->

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `iq_server_max_clients` | `1` | int 1..64 | iq_server.out | `1` keeps the single-client loop. `> 1` serves that many `:5000` clients, each receiving every frame. iq_server clamps values above 64 with a warning |
| `iq_server_frame_deadline_ms` | `0` (auto) | int ≥ 0 | iq_server.out | Multi-client mode only. A client that has not acknowledged a frame by the deadline is closed. `0` = 0.8 × the frame period, at least 100 ms |

## [schedule]: frequency hopping (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_schedule` | bool int | `0` | Enable the signal scheduler |
| `schedule_mode` | string | `none` | `none` / `file` / `inline` |
| `schedule_file` | path | empty | JSON schedule for `file` mode |
| `frequencies` | int list (Hz) | empty | Inline mode: hop frequencies |
| `gains` | list | empty | Inline mode: per-entry gains |
| `dwell_frames` | int list | empty | Frames to dwell per entry |
| `dwell_time_sec` | float list | optional key, absent by default | Alternative to `dwell_frames`. Converted to frames at load from `sample_rate`, `cpi_size` and `decimation_ratio` |
| `repeat_mode` | string | `loop` | End of schedule: `loop` (back to the first entry), `once` (stop at the last entry) or `pingpong` (reverse direction at each end). ini_checker rejects any other value |
| `require_cal_on_hop` | bool int | `1` | Wait for recalibration after each hop |
| `max_cal_wait_frames` | int | `500` | Give-up bound for the cal wait |

With `[database]` also enabled, the schedule position persists and resumes
across hw_controller restarts.

## [database]: BerkeleyDB telemetry store (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_db` | bool int | `0` | Enable persistence (needs the `berkeleydb` Python package) |
| `db_dir` | path | `_db` | Database directory |
| `max_db_size_mb` | int | `500` | Rotation size bound |
| `rotation_max_age_hours` | int | `168` | Rotation age bound |
| `write_batch_size` | int | `50` | Writer-thread batch size |
| `write_flush_interval_sec` | float | `1.0` | Writer flush interval |
| `hw_snapshot_interval` | int | `100` | Frames between hardware snapshots |

### [database] opt-in keys

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `instance_subdir` | `0` | 0/1 | delay_sync, hw_controller (via daq_db), heimdall-ctl local mode | `1`: instance N > 0 uses `<db_dir>/inst<N>`. Instance 0 and the default keep `db_dir` verbatim, so federated instances without this key need distinct `db_dir` values |
| `write_instance_meta` | `0` | 0/1 | delay_sync, hw_controller (via daq_db) | `1`: write `<db_dir>/_instance_meta.json` (instance id, node id) atomically at open. The file is advisory and not part of the BDB schema |

The on-disk schema is **generation 2** (marker file `_schema_version` in the
database directory): packed record formats (version byte 2) and big-endian
BTree keys, so byte order equals numeric order. A generation-1 database is
discarded on the first read-write open (telemetry is rolling data). Read-only
opens (`heimdall-ctl`) raise instead of deleting.

## [monitoring] (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_monitoring` | bool int | `0` | Master switch |
| `en_syslog` | bool int | `0` | Forward events to syslog |
| `syslog_address` | string | `/dev/log` | Syslog socket |
| `syslog_facility` | string | `daemon` | Syslog facility |
| `syslog_min_severity` | string | `warning` | Minimum forwarded severity |
| `en_metrics` | bool int | `0` | Collect latency/throughput metrics |
| `metrics_window_size` | int | `1000` | Rolling window per metric |
| `heartbeat_interval` | int | `100` | Frames between heartbeat events. The STATUS snapshot also refreshes on the heartbeat unless `status_update_interval` is set |
| `en_status_server` | bool int | `0` | Serve TCP `:5002` (needs `en_monitoring = 1`) |
| `status_server_port` | int | `5002` | Base port (instance offset applies) |
| `en_zmq_pub` | bool int | `0` | Publish events on ZMQ PUB (needs `en_monitoring = 1`) |
| `zmq_pub_port` | int | `5003` | Base PUB port |
| `event_ring_size` | int | `500` | Events kept for `EVENTS` queries |
| `drop_window_sec` | float | `60.0` (optional key, absent by default) | Window for the status server's `recent_drops` health derivation |

### [monitoring] opt-in keys

<!-- verify-after: WP-06 -->

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `status_extended` | `0` | 0/1 | delay_sync, status server | `1`: additive STATUS keys (`status_protocol: 2`, identity, frame geometry, `fsm_state`, `cal`, `counters`, freshness), the extended `:5002` verbs, a `{"error":"busy"}` reply at the client cap, and the `stale` health state. With 0, every `:5002` reply is unchanged |
| `status_update_interval` | `0` (= heartbeat) | int ≥ 0 | delay_sync | Refresh the STATUS snapshot every k frames, independent of the heartbeat. With `status_extended = 1` it also refreshes on every change of `sync_state`, `frame_type` or RF center frequency |
| `stale_after_sec` | `0` (auto) | float ≥ 0 | status server | Extended only. STATUS reports `pipeline_health = "stale"` when the snapshot is older than this. Auto = `3 × max(1, k) × frame_period + 1.0`, where k is the effective refresh interval in frames (132 s at the defaults) |
| `status_max_clients` | `8` | int 1..1024 | status server | Extended only. Concurrent `:5002` clients |
| `en_hwc_state_relay` | `0` | 0/1 | hw_controller (writer), delay_sync (reader) | hw_controller writes `_data_control/<P>hwc_state` every 5 frames and at once on new events. delay_sync adds it to STATUS (`hwc`, extended only) and re-emits hw_controller events on its own bus. Works without `en_monitoring` on the writer side |
| `event_tag_identity` | `0` | 0/1 | delay_sync, hw_controller (EventBus) | Tag every event with `source = {instance_id, node_id, host, cluster_id}`. Untagged event JSON is unchanged |
| `event_topic_prefix` | `0` | 0/1 | delay_sync (ZMQ PUB), heimdall-ctl | Publish events with topic `inst<N>.<event_type>` instead of `<event_type>`. This changes subscription filters, so it is a separate switch |
| `stamp_decimator_drops` | `0` | 0/1 | decimate.out (stamper), delay_sync (STATUS) | decimate.out stamps its cumulative dropped-frame count into header slot 109. delay_sync passes the slot through and reports `counters.decimator_drops` in extended STATUS |
| `stamp_cal_telemetry` | `0` | 0/1 | delay_sync | Stamp header slots 110-113: calibration quality (signed milli-dB), IQ compensation count, sample compensation count, recalibration generation |

ini_checker warnings (only with `en_monitoring = 1` and `en_status_server = 1`):
`the STATUS snapshot is never refreshed` when both `status_update_interval` and
`heartbeat_interval` are 0, and a warning when the refresh period exceeds 60 s.
The STATUS fields and verbs are in [protocols.md](protocols.md). The header
slots are listed in the IQ header section of [protocols.md](protocols.md).

## [offload] (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `rebuffer_transport` | string | `shm` | `shm` / `spi` / `pcie` / `usb3` / `net`. A non-shm value needs the matching `HAS_*_TRANSPORT` driver compiled into `rebuffer.out` |
| `decimator_transport` | string | `shm` | As above, for `decimate.out` |
| `delay_sync_transport` | string | `shm` | **Only `shm` works.** delay_sync uses the Python `transportIface`, whose spi, pcie, usb3 and net backends are unimplemented stubs. Any other value fails interface init and delay_sync exits. iq_server always reads `delay_sync_iq` over shm |
| `fir_engine` | string | `auto` | `auto` / `neon` (`cpu_neon`) / `kfr` (`cpu_kfr`) / `generic` (`cpu_generic`) / `fpga` / `gpu`. `auto` uses the platform engine compiled into `decimate.out`. `generic` selects the dependency-free plain-C engine (always available in kfr/ne10 builds) |
| `fft_engine` | string | `auto` | Python FFT engine of delay_sync. **Only `auto` (SciPy CPU FFT) works.** ini_checker rejects `cpu_scipy`, the backend's real name. `gpu` and `fpga` pass ini_checker but fail at delay_sync start: delay_sync does not pass an FFT size, and the FPGA FFT is a stub. `kfr`, `neon` and `generic` also pass ini_checker but are FIR engine names; FFTEngine rejects them at start |

### [offload] opt-in keys

<!-- verify-after: WP-06 -->

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `fft_workers` | `4` | int 1..64 or `auto` | delay_sync | SciPy FFT worker threads. `auto` = the number of cores in `[host] delay_sync_cores` when set, else the size of delay_sync's CPU affinity mask. Results are bit-identical for every value |
| `xcorr_workers` | `4` | int 1..64 or `auto` | delay_sync | Worker threads of the cross-correlation FFT engine. When it equals `fft_workers`, one engine serves both |
| `skip_copy_on_drop` | `0` | 0/1 | delay_sync | When the `delay_sync_iq` output buffer is dropped and `en_iq_cal = 0`, process the input frame in place instead of copying it into a scratch frame |

Worker threads run in parallel only when delay_sync has more than one core
(`[host] delay_sync_cores`). See [capacity_planning.md](capacity_planning.md).

## [dma], [fpga], [gpu], [pcie], [usb3], [hat_uart], [hat_i2c] (placeholders)

These sections carry `enable = 0` by default. ini_checker validates them.
Apart from the three start-script keys below, they are placeholders.

- The start scripts read `[fpga] enable` + `bitstream` (load the bitstream with
  `_daq_core/fpga_loader.py` when `enable = 1` and the file exists) and
  `[gpu] enable` (run `_daq_core/gpu_init.py`). Both fall back to CPU-only
  mode on failure.
- No runtime component reads `[dma]`, `[usb3]`, `[hat_uart]`, `[hat_i2c]`, or
  any other `[fpga]` or `[gpu]` key, even with a driver compiled in.
- The FPGA FIR engine (`offload_fpga.c`) and the SPI transport use
  compile-time constants for SPI devices, speeds and GPIO pins. Those
  constants differ from the INI values below.
- The optional `hat_pcie_bridge` reads `[pcie] bar_index` and `driver`. It only
  logs `device` and does not act on `enable`.

| Section | Keys (defaults) |
|---|---|
| `[dma]` | `enable=0`, `channel_memcpy=7`, `min_transfer_size=65536` |
| `[fpga]` | `enable=0`, `spi_device=/dev/spidev0.0`, `spi_speed_hz=62500000`, `gpio_drdy=25`, `gpio_reset=26`, `bitstream=_data_control/heimdall_fpga.bin`, `offload_fir=1`, `offload_xcorr=0` |
| `[gpu]` | `enable=0`, `backend=vc4cl`, `offload_fft=1`, `offload_fir=0`, `fft_batch_size=4` |
| `[pcie]` | `enable=0`, `device=0000:01:00.0`, `bar_index=0`, `driver=xdma` |
| `[usb3]` | `enable=0`, `vid=0x0403`, `pid=0x601f`, `transfer_size=16384`, `num_transfers=32` |
| `[hat_uart]` | `enable=0`, `device=/dev/ttyAMA1`, `baud=3000000`, `framing=cobs` |
| `[hat_i2c]` | `enable=0`, `bus=1`, `speed=400000`, `retry_count=3` |

## [federation] (optional)

Federation lets several instances share a host or a cluster. Every key here is
optional. With the section absent, the instance runs as instance 0. See
[scaling_and_clustering.md](scaling_and_clustering.md) for the model.

| Key | Type | Default | Behavior |
|---|---|---|---|
| `instance_id` | int ≥ 0 | `0` | 0 = unprefixed resources and base ports (backward compatible). N > 0 = `inst<N>_` prefix on shm, FIFOs and control files, logs under `_logs/instN/` |
| `port_stride` | int > 0 | `100` | Port offset multiplier: `port = base + instance_id × port_stride`. With stride 100, ids 0-9 are safe on one host (see [networking_and_time_sync.md](networking_and_time_sync.md#13-safe-instance-id-range)) |
| `en_federation` | bool int | `0` | Enables the per-instance federation health monitor (`FederationHealth` in delay_sync). It is built only when `peer_list` or `cluster_members` is non-empty, or `register_with_coordinator = 1`. It does not start or affect a coordinator: `federation_coordinator.py` is launched separately (`./federation_start.sh coordinator`) and ignores this key |
| `coordinator_host` | string | empty | Not read by the coordinator. Read by delay_sync as the REGISTER target when `register_with_coordinator = 1` and `coordinator_candidates` is empty, and published as the coordinator endpoint in extended STATUS. heimdall-ctl `fleet` uses `coordinator_host:coordinator_port` as its default target after `--coordinator`, `$HEIMDALL_COORDINATOR` and `coordinator_candidates`. With none of these features on, it has no runtime effect. ini_checker also uses it: when it names this host, a collision between this instance's ports and `coordinator_port` is reported (a warning, or an error in new-key mode) |
| `coordinator_port` | int 1..65535 | `6000` | The port `federation_coordinator.py --config` binds. Also the port paired with `coordinator_host` above |
| `peer_list` | list | empty | Peers polled by this instance's health monitor, as `host:instance_id` (canonical). Legacy `host:status_port` entries (number ≥ 1024) also parse. Problems are warnings. They become errors when `cluster_members` or `coordinator_candidates` is set, or when `register_with_coordinator = 1` |

<!-- verify-after: WP-08 -->
The coordinator (`--config`) and the IQ router now parse `peer_list` with the
same legacy rule. The router skips an entry whose status port is off the
`port_stride` grid.

### [federation] opt-in keys (instance INI)

<!-- verify-after: WP-06, WP-08 -->

**Identity**

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `node_id` | `<short hostname>:<instance_id>` | `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,47}$` | delay_sync, hw_controller, daq_db, coordinator, router | Stable name of this instance. The default lower-cases the hostname, turns disallowed characters into `-` and truncates to 48 characters |
| `cluster_id` | `default` | `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,30}$` | delay_sync, hw_controller, coordinator | Cluster name. Only an XACK carrying this member's own `cluster_id` can raise its fence term |
| `advertise_host` | empty (= the short lower-case `socket.gethostname()`, first label only; a loopback name is an error) | host name | delay_sync, coordinator, router | Host part of this instance's node key in REGISTER and in the self-entry match |
| `stamp_node_id` | `0` | 0/1 | delay_sync | Stamp `crc32(node_id)` into header slot 108 |
| `fence_max_term_jump` | `1000` | int ≥ 1 | hw_controller | Largest term increase one XACK may make. A bigger jump is refused with `term_jump` and that connection's mutating verbs are not executed. The fence term persists in `_data_control/<P>fence_term` |

**Membership and registration**

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `cluster_members` | empty | `host:instance_id[@node_id],...`, no duplicates, no loopback hosts | delay_sync, coordinator, router, ini_checker | Full membership including this instance. When `peer_list` is empty, the health monitor polls `cluster_members` minus the self entry. ini_checker warns when the self entry is missing and when `peer_list` overrides it |
| `coordinator_candidates` | empty | `host:port,...`, unique | delay_sync, coordinator, heimdall-ctl | Every coordinator process in the election. delay_sync registers with these. Empty = one static coordinator |
| `register_with_coordinator` | `0` | 0/1, needs `en_federation = 1` (error) | delay_sync | Start a registrar thread that sends REGISTER to `coordinator_candidates`, else to `coordinator_host:coordinator_port` |
| `register_interval_sec` | `5.0` | float 0.5..300 | delay_sync | REGISTER period |

**Peer health**

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `health_mode` | `mesh` | `mesh` / `hub` / `off` | delay_sync, coordinator | `mesh`: poll every peer. `hub`: take the peer table from the REGISTER reply (needs `register_with_coordinator = 1`, error otherwise). `off`: no polling |
| `health_poll_interval` | `5.0` | float 0.2..300 | delay_sync, coordinator | Poll period |
| `peer_timeout` | `2.0` | float 0.1..30 | delay_sync, coordinator | Per-poll timeout |
| `peer_up_successes` | `1` | int 1..10 | delay_sync, coordinator | Consecutive good polls before a peer is `up` |
| `peer_down_misses` | absent | int 1..20 | delay_sync, coordinator | Absent: a peer is down when no poll succeeded for 3 × `health_poll_interval`. Set: down after this many consecutive failed polls and that much silence |
| `peer_flap_window_sec` | `0` (off) | float ≥ 0 | delay_sync, coordinator | Flap detection window |
| `peer_flap_threshold` | `0` (off) | int ≥ 0 | delay_sync, coordinator | More state changes than this inside the window mark the peer `flapping` |
| `emit_peer_health_events` | `0` | 0/1 | delay_sync | Emit `peer_degraded` / `peer_recovered` / `peer_flapping` events |
| `health_workers` | `1` | int 1..64 | delay_sync | Parallel polls. `1` keeps the sequential loop |

**Access control**

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `auth_token` | empty | printable ASCII, no spaces, at most 128 characters | coordinator, router stats port, delay_sync (registrar, health monitor), heimdall-ctl | Shared token for `:6000` and the router stats port |
| `allow_cidrs` | empty | comma list of IPv4/IPv6 CIDRs | coordinator, router | Accept clients only from these networks. ini_checker validates it in the instance INI too, but instances do not read it |

See [networking_and_time_sync.md](networking_and_time_sync.md#3-trust-model-and-access-control)
for the trust model.

## federation.ini: coordinator and router keys

<!-- verify-after: WP-08, WP-09 -->

`federation_coordinator.py --config federation.ini` and
`federation_iq_router.py --config federation.ini` read their keys from the
`[federation]` section of that file, plus `[daq] listen_address` as the bind
address. CLI flags override file values.

The DAQ start scripts (`daq_start_sm.sh`, `daq_synthetic_start.sh`) do not
launch either process. Start them from `Firmware/` and stop them with
`./federation_stop.sh`:

```
./federation_start.sh coordinator|router|all [--config PATH] [--foreground]
                      [--state-dir DIR] [--log-dir DIR] [--pid-dir DIR]
```

Without `--config`, the launcher uses `/etc/heimdall/federation.ini` when
present, else `./daq_chain_config.ini`. See
[deployment_recipes.md](deployment_recipes.md) and
[scaling_and_clustering.md](scaling_and_clustering.md) for the systemd units.

Validate the file with:

```
$ python3 ini_checker.py federation.ini --federation
WARNING: require_quorum = 1 with 2 candidates: with 2 candidates, losing either host blocks all mutations
WARNING: auth_token and allow_cidrs are empty and the listen address 0.0.0.0 is not loopback: the coordinator accepts mutating commands from any host
```

Port rules are always errors here. `coordinator_port`, `router_port` and
`router_stats_port` must differ pairwise once any `router_*` key is present.

The coordinator also reads these instance keys from the file:
`coordinator_port`, `instance_id`, `port_stride`, `peer_list`,
`cluster_members`, `advertise_host`, `cluster_id`, `node_id`, `auth_token`,
`allow_cidrs`, `coordinator_candidates` and the peer-health keys above.

**Coordinator membership** comes from `cluster_members` when it is non-empty.
Otherwise it is `--instances`, else `peer_list`. `coordinator_include_self = 1`
adds the coordinator's own instance to that legacy list. Registered members
are added at run time.

### Election

| Key | Default | Valid values | Behavior |
|---|---|---|---|
| `coordinator_self` | empty (= the first candidate whose port is the listen port on a local host) | `host:port` from `coordinator_candidates` | Which candidate this process is (CLI `--self`) |
| `coordinator_include_self` | `0` | 0/1 | See membership above. Ignored when `cluster_members` is set |
| `election_interval_sec` | `2.0` | float 0.05..60 | Election tick I. Lease duration L = I × `election_missed_polls` |
| `election_missed_polls` | `3` | int 2..20 | K in L = I × K |
| `election_settle_polls` | `5` | int ≥ `ceil(K × (1 + election_clock_margin)) + 1` (error) | Ticks after start during which the candidate neither claims nor grants |
| `election_clock_margin` | `0.05` | float 0..0.5 | Tolerated relative clock-rate difference between candidates |
| `election_failback_sec` | `60` | float 0..3600 | How long a recovered better-ranked candidate must stay reachable before the leader hands over |
| `require_quorum` | `0` | 0/1 | Leadership and mutating verbs need a majority of unexpired leases. Needs at least 3 candidates (warning) |
| `state_dir` | empty | single-line path | Persistence directory for the term and replicated state (CLI `--state-dir`) |

### Fan-out, timeouts and reconciliation

| Key | Default | Valid values | Behavior |
|---|---|---|---|
| `fanout_workers` | `32` | int 1..1024 | Parallel per-member requests |
| `max_handlers` / `reserved_handlers` | `64` / `8` | int 1..1024 | Request handler pool, and the part kept for LEADER/PREVOTE/LEASE/REGISTER |
| `peer_connect_timeout_sec` | `2.0` | float 0.1..60 | TCP connect timeout per member |
| `xack_timeout_sec` | `3.0` | float 0.1..60 | Wait for a member's XACK reply |
| `hwc_request_timeout_sec` | `auto` | `auto` or float ≥ 0 | Per-member command deadline. `auto` derives it from the member's advertised `rto` and `hmx` |
| `max_member_rto_sec` | `30` | float 5..600 | Upper bound on a member's advertised request timeout |
| `status_timeout_sec` | `3.0` | float 0.1..60 | `:5002` query timeout per member |
| `status_cache_sec` | `0.0` (live) | float ≥ 0 | Legacy STATUS cache |
| `mutation_wait_sec` | `2.0` | float ≥ 0 | Wait for the mutation lock |
| `sync_push_timeout_sec` | `5.0` | float 0.1..60 | Leader-to-follower state push timeout |
| `breaker_failures` / `breaker_open_sec` | `3` / `10.0` | int ≥ 1 / float ≥ 0 | After this many consecutive failures a member is skipped for `breaker_open_sec` |
| `xack_cache_sec` | `60` | float ≥ 0 | Per-member ack-mode cache lifetime |
| `schc_window` | `16` | int 1..64 | Pipelined schedule chunks in flight |
| `schc_rtt_budget_ms` | `50` | ≥ 1 | Round-trip budget per chunk in the deadline |
| `member_stall_after_sec` | `auto` | `auto` or float ≥ 0 | Frame-progress stall detection on member STATUS |
| `reconcile_assignments` | `auto` | `auto` / `0` / `1` | Re-send schedule assignments that a member lost. `auto` = on when `cluster_members` or `coordinator_candidates` is set |
| `reconcile_interval_sec` | `0` (= `health_poll_interval`) | float ≥ 0 | Reconciliation period |
| `reconcile_max_attempts` | `5` | int ≥ 1 | Attempts per member (backoff 5, 10, 20, 40, 80 s) |
| `reconcile_override` | `0` | 0/1 | Reconcile even against a member-side change |
| `register_expiry_factor` | `3` | ≥ 1 | A registered member expires after this many missed REGISTER intervals |
| `register_retain_sec` | `600` | float ≥ 0 | How long an expired registration is kept |
| `router_stats_endpoint` | empty | `host:port` | Router stats port the coordinator queries |
| `dns_ttl_sec` | `60` | float ≥ 0 | Member host name cache lifetime |

### IQ router

| Key | CLI flag | Default | Valid values | Behavior |
|---|---|---|---|---|
| `router_port` | `--output-port` | `7000` | 1..65535 | Unified output port |
| `router_stats_port` | `--stats-port` | `7001` | 0..65535 (0 = off) | Stats port |
| `router_sources` | `--sources` | empty | `host:iid[@node_id]` and `router://host:port` entries | Sources. Precedence: `router_sources`, then `cluster_members`, then `peer_list` |
| `router_include_self` | | `1` | 0/1 | With the `peer_list` fallback, add this host's own instance |
| `router_queue_frames` | `--queue-frames` | `4` | 1..1024 | Per-client queue depth |
| `router_drop_policy` | `--drop-policy` | `oldest` | `oldest` / `newest` / `disconnect` | What to do when a client queue is full |
| `router_tag_mode` | `--tag-mode` | `instance` | `instance` / `unit` / `node` | `instance` rewrites `unit_id`, `unit` leaves it, `node` stamps slots 106/107 |
| `router_align_window_ms` | `--align-window-ms` | `0` | ≥ 0 | Hold frames up to W ms to release them in `time_stamp` order. 0 = arrival order |
| `router_align_max_frames` | `--align-max-frames` | `2` | 1..64 | Per-source alignment FIFO depth |
| `router_max_skew_ms` | `--max-skew-ms` | `500` | ≥ 0 (0 = off) | Clock-offset deviation that raises `skew_warning` |
| `router_connect_on_demand` | `--connect-on-demand` / `--always-connect` | `1` | 0/1 | Connect to sources only while a client is attached |
| `router_max_clients` | `--max-clients` | `8` | 1..256 | Output clients served |
| `router_send_timeout_sec` | `--send-timeout-sec` | `2.0` | > 0 | Evict a client that accepts no byte for this long. ini_checker accepts 0, but the router refuses it at start. Use a positive value |
| `router_read_timeout_sec` | `--read-timeout-sec` | `auto` | `auto` or ≥ 0 | Source read timeout. `auto` = max(5, 3 × frame period), 0 = none |
| `router_first_frame_timeout_sec` | `--first-frame-timeout-sec` | `0` | ≥ 0 | Give up waiting for an iq_server client slot after this long. 0 = keep waiting |
| `router_reconnect_sec` | `--reconnect-sec` | `3.0` | ≥ 0 | First reconnect delay. Doubles per failure up to 30 s |

`--stamp-seq`, `--stamp-rx-ts`, `--client-stall-limit`, `--connect-timeout-sec`
and `--upstream-stats` are CLI-only. Run `federation_iq_router.py --help` for
the full list.

## [amplification]: external LNAs and gain staging (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_amplification` | bool int | `0` | Enable the link-budget subsystem |
| `ext_lna_gains_db` | float list, per channel | `0,0,...` | External LNA gain, continuous dB (never quantized to the R820T table) |
| `ext_lna_nf_db` | float list, per channel | `0,0,...` | LNA noise figure for the Friis cascade |
| `ext_lna_p1db_dbm` | float list, per channel | `99,99,...` | Output-referred P1dB. `99` = not modelled |
| `tuner_nf_db` | float | `3.5` | Tuner reference NF (R820T ≈ 3.5 dB) |
| `expected_input_dbm` | float | `-60` | Assumed antenna-port level for the advisory headroom estimate |
| `p1db_margin_db` | float | `3` | Warn when estimated output is within this margin of P1dB |
| `max_total_gain_db` | float | `80` | Warn when total system gain exceeds this |
| `en_bias_tee_runtime` | bool int | `0` | Allow the runtime `BIAS` control command |
| `bias_tee_state` | int list, per channel | `0,0,...` | Initial per-channel bias-tee state that gets **reported**. With amplification on, hw_controller writes it at init to `_data_control/<P>bias_tee_state`, and delay_sync stamps it into header slot 97. It is not sent to the hardware unless `apply_bias_tee_at_init = 1`. The hardware state at start comes from `[hw] en_bias_tee`. A runtime change needs the `BIAS` command (with `en_bias_tee_runtime = 1`) |
| `lna_freq_table` | string | empty | Optional frequency-dependent response: `fMHz:gain_db:nf_db;...` (empty = flat) |

### [amplification] opt-in keys

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `apply_bias_tee_at_init` | `0` | 0/1 | hw_controller | At init, send one bias-tee message to rtl_daq when `bias_tee_state` differs from `[hw] en_bias_tee` (absent = all off). The hardware and header slot 97 then agree. Needs `en_amplification = 1` and `en_bias_tee_runtime = 1`. Without `en_bias_tee_runtime` hw_controller logs a WARNING and ignores it. Without `en_amplification` it is ignored silently |

Compression detection is advisory only. It sets header slot 96 and emits
events, but it never changes gains.

## [antenna]: directional antenna profile (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_antenna_profile` | bool int | `0` | Enable the profile |
| `profile_name` | string | `isotropic` | Label |
| `element_gain_dbi` | float | `0` | Antenna element gain (enters the link budget) |
| `beamwidth_az_deg` / `beamwidth_el_deg` | float | `360` | −3 dB beamwidths |
| `polarization` | string | `vertical` | `vertical` / `horizontal` / `rhcp` / `lhcp` / `slant45` / `dual` |
| `cable_loss_db` / `connector_loss_db` | float | `0` | Feed losses (enter the budget as pre-LNA loss) |
| `boresight_az_offset_deg` / `boresight_el_offset_deg` | float | `0` | Mechanical-to-electrical boresight offset applied to commanded bearings |

## [orientation]: rotator and pan-tilt (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_orientation` | bool int | `0` | Enable the orientation controller (runs inside hw_controller) |
| `backend` | string | `mock` | `mock` / `gs232` / `pwm_servo` / `i2c_pantilt`. The factory falls back to mock so the chain always starts |
| `device` | path | `/dev/ttyUSB0` | Serial device (gs232) |
| `baud` | int | `9600` | Serial baud (gs232) |
| `i2c_bus` / `i2c_address` | int / hex | `1` / `0x40` | PCA9685 bus and address (i2c_pantilt) |
| `az_pwm_channel` / `el_pwm_channel` | int | `0` / `1` | PWM channels (servo backends) |
| `az_min_deg`..`el_max_deg` | float | `0/360/0/90` | Motion limits |
| `az_park_deg` / `el_park_deg` | float | `0` / `0` | Park position |
| `slew_rate_dps` | float | `6.0` | Modelled slew rate (open-loop backends) |
| `settle_frames` | int | `5` | DATA frames to settle after motion |
| `position_tolerance_deg` | float | `1.0` | Position match tolerance |
| `home_on_start` | bool int | `0` | Home the rotator at init |
| `bearing_mode` | string | `external` | `manual` / `fixed` / `external` / `scan_peak` |
| `min_sync_state` | int | `5` | Minimum `sync_state` that permits motion (never slew mid-calibration) |
| `en_scan` | bool int | `0` | Auto-start scan-and-peak once sync locks |
| `scan_az_start/stop/step` | float | `0/350/10` | Azimuth scan grid |
| `scan_el_start/stop/step` | float | `0/0/10` | Elevation scan grid |
| `scan_dwell_frames` | int | `20` | DATA frames averaged per grid point |

## [host]: CPU layout, priorities and the instance registry (optional)

The whole section is opt-in. ini_checker validates it. `daq_start_sm.sh`
reads every key below through `daq_common.sh`. `daq_synthetic_start.sh` always
applies `instance_registry` and `instance_guard`, applies `cpu_cores`,
`rt_priority_offset`, `allow_core_sharing` and `preflight` only with `--rt`,
and never runs host tuning. delay_sync also reads `delay_sync_cores` and
`health_thread_sched_other`. See [deployment_recipes.md](deployment_recipes.md)
for multi-instance layouts that use these keys.

Start-script flags: `--takeover` (with `instance_guard = refuse`, stop this
instance when it runs from another copy, then start) and `--no-host-tuning`
(same as `host_tuning = 0`).

| Start-script exit code | Meaning |
|---|---|
| `0` | Started |
| `1` | Config check or FIR designer failed, or ports still busy |
| `3` | Guard refusal (`instance_guard = refuse`, or `allow_core_sharing = refuse` with an overlapping live instance) |
| `4` | Pre-flight failure with `preflight = strict` |

| Key | Default | Valid values | Read by | Behavior |
|---|---|---|---|---|
| `cpu_cores` | empty | exactly 4 distinct core numbers (hardware mode: each < nproc) | start scripts | Cores for the four tiers: rtl_daq, rebuffer+decimate, delay_sync+iq_server, hw_controller. Empty keeps today's `(N*4+k) % nproc` layout. The start scripts validate with `no_hw`, which skips the nproc check, so run `python3 ini_checker.py` (without `no_hw`) once on the target host after setting it |
| `delay_sync_cores` | empty | comma list of core numbers | start scripts, delay_sync | Wider core list for delay_sync only, so FFT workers can run in parallel. Also the core count for `fft_workers = auto` |
| `rt_priority_offset` | `0` | int 0..81 | start scripts | Subtracted from every real-time priority (99/94/92/90/88/82) |
| `allow_core_sharing` | `warn` | `warn` / `refuse` / `allow` | start scripts, preflight | What to do when this instance's cores overlap a live registered instance's cores. `refuse` exits 3. The check needs `instance_registry = 1` |
| `host_tuning` | `always` | `always` / `auto` / `1` / `0` | start scripts | `always`: today's sysctl, IRQ, usbfs and drop_caches commands. `1`: run `util/host_tuning.sh --apply`. `auto`: as `1`, but skipped when `heimdall-host-tuning.service` is active or another instance in the host registry is running. `0`: skip |
| `preflight` | `off` | `off` / `warn` / `strict` | start scripts | Run `python3 daq_preflight.py --config daq_chain_config.ini --plus-live` before start. Only tool exit 2 (failures) with `strict` stops the start (exit 4). Any other nonzero exit prints a WARN and the start continues. When `daq_preflight.py` is missing, the start script prints `WARN: daq_preflight.py not found; preflight skipped` and continues, even with `strict` <!-- verify-after: WP-13 --> |
| `instance_registry` | `0` | 0/1 | start scripts, ini_checker | Write `$HEIMDALL_RUN_DIR/inst<N>.json`, take the instance lock and the startup mutex. A host-wide view only when every instance on the host sets 1 and runs as the same user |
| `instance_guard` | `legacy` | `legacy` / `refuse` (needs `instance_registry = 1`, error) | start scripts (delay_sync checks the lock they export) | `refuse`: refuse a duplicate start of the same instance |
| `usb_irq_core` | empty | int ≥ 0 | start scripts | Passed to `irq_tuning.sh --usb-core` when set |
| `usb_controller` | empty | single-line string | start scripts | Passed to `irq_tuning.sh --controller` when set |
| `health_thread_sched_other` | `0` | 0/1 | delay_sync | The federation health and registrar threads switch to `SCHED_OTHER` instead of inheriting delay_sync's real-time priority |

## Preset catalog (`config_files/`)

| Preset | Purpose |
|---|---|
| `kraken_default/` | 5-channel KrakenSDR defaults (core sections only; the same core values as the shipped `Firmware/daq_chain_config.ini`, without its optional sections and commented opt-in keys) |
| `kerberos_default/` | 4-channel KerberosSDR |
| `kraken_development/` | 5-channel development variant |
| `unit_test_k4/` | 4-channel values the sudo pipeline tests require (`daq_buffer_size=262144`, `cpi_size=262144`, `decimation_ratio=1`, `corr_size=65536`, 1 MHz sample rate) |
| `directional_df/` | The worked amplified-receiver + high-gain-antenna + GS-232-rotator example. The one preset with `[amplification]`, `[antenna]` and `[orientation]` enabled (see [tutorial_directional_df.md](tutorial_directional_df.md)) |
| `multi_instance_x86/` | Instance 1 of an 8-core x86_64 host running two KrakenSDR units: `serial_base = 1100`, cores 4-7, `rt_priority_offset = 2`, host registry and duplicate-start guard on. It is what `util/provision_instance.py --instance 1 --cores 4,5,6,7 --rt-offset 2` writes |
| `cluster_site/daq_chain_config.ini`, `cluster_site/federation.ini` | Two-host cluster `site-a`. Members `hosta:0`, `hosta:1` and `hostb:0`. Coordinator candidates `hosta`, `hostb` and the instance-less `hostc`. `federation.ini` sets `require_quorum = 1` and `allow_cidrs`. Generated with `util/cluster_cfg_gen.py` |
| `performance/minimal.ini`, `balanced.ini`, `maximum.ini` | `[host]` + `[offload]` snippets that `util/provision_instance.py --profile NAME` merges into an instance INI. Not full configs |
| `performance/minimal.conf`, `balanced.conf`, `maximum.conf` | Documentation only. No script, Makefile or module reads any key in these files. The RT tiers (99/94/92/90/88/82) are fixed in the start scripts and shifted only by `[host] rt_priority_offset`. CPU affinity comes from `[host] cpu_cores` / `delay_sync_cores`. See `performance/README.md` for the mapping |

Every preset passes `ini_checker.py <file> no_hw` (`cluster_site/federation.ini`
with `--federation`).

To apply a preset, copy `config_files/<preset>/daq_chain_config.ini` to
`Firmware/daq_chain_config.ini`, or install it with:

```
sudo python3 util/provision_instance.py --instance N --from-config <file> --write
```

Replace the `cluster_site` host names (`hosta`, `hostb`, `hostc`) with the real
host names before use. Never use `localhost`.

Generators:

| Tool | Output |
|---|---|
| `util/cfg_gen.py` | A `daq_chain_config.ini` from signal parameters |
| `util/provision_instance.py` | One instance's INI and directory on a multi-instance host (`--instance`, `--cores`, `--rt-offset`, `--profile`, `--from-config`) |
| `util/cluster_cfg_gen.py` | Per-instance INIs and a `federation.ini` for a cluster (`--hosts`, `--coordinators`, `--register`, `--cluster-id`) |
