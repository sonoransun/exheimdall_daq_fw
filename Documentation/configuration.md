# Configuration Reference

The chain is configured by `Firmware/daq_chain_config.ini`. Validate it with
`python3 ini_checker.py [config_path] [no_hw]` (exit 0 = valid, 1 = errors;
`no_hw` skips hardware probing). The start scripts run this check and refuse
to start on a nonzero exit. Unknown sections and keys are non-fatal.

Two kinds of sections:

- **Core sections** — `[meta]`, `[hw]`, `[daq]`, `[pre_processing]`,
  `[calibration]`, `[adpis]`, `[data_interface]` — expected to be present.
- **Optional sections** — everything else. All default to disabled
  (`en_* = 0` / `enable = 0`); with them absent or disabled the chain behaves
  identically to the pre-abstraction firmware.

Comma-list keys are per-channel and must have exactly `num_ch` entries.

## [meta]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `ini_version` | int | `8` | Schema version. **Informational — not enforced by `ini_checker`.** v8 added the RF front-end sections (`[amplification]`, `[antenna]`, `[orientation]`) |
| `config_name` | string | `kraken_default` | Free-form preset name |

## [hw]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `name` | string | `kraken5` | Hardware ID stamped into the header `hardware_id` (truncated to 16 bytes) |
| `unit_id` | int | `0` | Header `unit_id` |
| `ioo_type` | int | `0` | Illuminator type (header field) |
| `num_ch` | int | `5` | Number of coherent receiver channels M. Validated 1..32; rtl_daq exits fatally on invalid values |
| `en_bias_tee` | int list | `0,0,0,0,0` | Per-channel bias tee enabled at startup |

## [daq]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `log_level` | int | `5` | 0=TRACE 1=DEBUG 2=INFO 3=WARN 4=ERROR 5=FATAL (C, rxi log); Python processes use `log_level × 10` for the `logging` module |
| `daq_buffer_size` | int | `262144` | Samples per channel per DAQ block. Must be a power of two (enforced by `ini_checker`); byte size on the wire is 2× for interleaved u8 I/Q; librtlsdr transfer granularity is 512 bytes |
| `center_freq` | int (Hz) | `700000000` | Initial RF center frequency |
| `sample_rate` | int (Hz) | `2400000` | ADC sample rate |
| `gain` | int (tenths dB) | `0` | Initial tuner gain for all channels — must be a valid R820T gain value |
| `en_noise_source_ctr` | bool int | `1` | Enable calibration noise-source control (GPIO 0 on the control channel) |
| `ctr_channel_serial_no` | int | `1000` | Serial number of the control-channel (noise source) dongle |
| `listen_address` | IPv4 string | `0.0.0.0` (**optional key**, absent by default) | Bind address for the :5000 IQ server, :5001 control server, :5002 status server, and :5003 event PUB. Invalid values warn and fall back to 0.0.0.0. Validated by `ini_checker` via `inet_aton` |

## [pre_processing]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `cpi_size` | int | `1048576` | Output samples per channel per CPI frame (post-decimation) |
| `decimation_ratio` | int | `1` | FIR decimation ratio R |
| `fir_relative_bandwidth` | float 0..1 | `1.0` | Passband relative to output Nyquist |
| `fir_tap_size` | int | `1` | FIR tap count K. Must be > R when R > 1 (the designer rejects K ≤ R) |
| `fir_window` | string | `hann` | scipy window name for the filter design |
| `en_filter_reset` | bool int | `0` | Reset FIR state between frames |

**Cross-references:** `fir_filter_designer.py` regenerates
`_data_control/fir_coeffs.txt` from these keys at **every** startup — changing
`cpi_size` or `decimation_ratio` invalidates existing coefficients (the file's
length must equal `fir_tap_size`). `ini_checker` enforces
`cpi_size × decimation_ratio ≥ daq_buffer_size` (the CPI must span at least
one DAQ block) and `fir_tap_size > decimation_ratio` when the ratio is not 1.

## [calibration]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `corr_size` | int | `65536` | Cross-correlation size for delay estimation |
| `std_ch_ind` | int | `0` | Reference ("standard") channel index |
| `en_iq_cal` | bool int | `1` | Enable IQ amplitude/phase calibration |
| `amplitude_cal_mode` | string | `channel_power` | `default` / `disabled` / `channel_power` |
| `en_gain_tune_init` | bool int | `0` | Gain fine-tune at init |
| `gain_lock_interval` | int | `0` | Frames required for gain-lock confirmation |
| `unified_gain_control` | bool int | `0` | Clamp all channels to the minimum requested gain. Total system gain (link budget) is computed **after** this clamp |
| `require_track_lock_intervention` | bool int | `0` | Hold in STATE_TRACK_LOCK until the `_data_control/iq_track_lock` control file permits |
| `cal_track_mode` | int | `2` | 0 = no calibration tracking, 1 = sample-delay tracking, 2 = sample-delay + IQ tracking (with `en_iq_cal`) |
| `cal_frame_interval` | int | `687` | Frames between periodic calibration bursts |
| `cal_frame_burst_size` | int | `10` | Calibration frames per burst |
| `amplitude_tolerance` | int (dB) | `2` | Amplitude drift tolerance before recal |
| `phase_tolerance` | int (deg) | `1` | Phase drift tolerance before recal |
| `maximum_sync_fails` | int | `10` | Consecutive failures before sync is declared lost |
| `iq_adjust_source` | string | `explicit-time-delay` | `explicit-time-delay` or `touchstone` (the latter needs scikit-rf) |
| `iq_adjust_amplitude` | float list | `0,0,0,0` | Per-channel (M−1 entries, relative to the reference) amplitude adjustment |
| `iq_adjust_time_delay_ns` | float list | `0, 0, 0, 0` | Per-channel time-delay adjustment, ns |

## [adpis]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_adpis` | bool int | `0` | Enable ADPIS hardware (analog phase/gain shifters via DAC) |
| `adpis_proc_size` | int | `8192` | hw_controller processing block size |
| `adpis_gains_init` | int list | `0,0,0,0,0` | Initial ADPIS gains |

## [data_interface]

| Key | Type | Default | Behavior |
|---|---|---|---|
| `out_data_iface_type` | string | `shmem` | `shmem` (downstream DSP attaches to the `delay_sync_iq` ring) or `eth` (launch `iq_server.out` on :5000) |

## [schedule] — frequency hopping (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_schedule` | bool int | `0` | Enable the signal scheduler |
| `schedule_mode` | string | `none` | `none` / `file` / `inline` |
| `schedule_file` | path | empty | JSON schedule for `file` mode |
| `frequencies` | int list (Hz) | empty | Inline mode: hop frequencies |
| `gains` | list | empty | Inline mode: per-entry gains |
| `dwell_frames` | int list | empty | Frames to dwell per entry |
| `dwell_time_sec` | float list | (**optional key**, absent by default) | Alternative to `dwell_frames`; converted to frames at load from sample_rate/cpi_size/decimation_ratio |
| `repeat_mode` | string | `loop` | `loop` or one-shot behavior at end of schedule |
| `require_cal_on_hop` | bool int | `1` | Wait for recalibration after each hop |
| `max_cal_wait_frames` | int | `500` | Give-up bound for the cal wait |

With `[database]` also enabled, the schedule position persists and resumes
across hw_controller restarts.

## [database] — BerkeleyDB telemetry store (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_db` | bool int | `0` | Enable persistence (needs the `berkeleydb` Python package) |
| `db_dir` | path | `_db` | Database directory. Federated instances should each set a distinct `db_dir` |
| `max_db_size_mb` | int | `500` | Rotation size bound |
| `rotation_max_age_hours` | int | `168` | Rotation age bound |
| `write_batch_size` | int | `50` | Writer-thread batch size |
| `write_flush_interval_sec` | float | `1.0` | Writer flush interval |
| `hw_snapshot_interval` | int | `100` | Frames between hardware snapshots |

The on-disk schema is **generation 2** (marker file `_schema_version` in
`db_dir`): packed native-endian record formats (version byte 2), big-endian
BTree keys so byte order equals numeric order. A v1 database is discarded
automatically on the first read-write open (telemetry is rolling data);
read-only opens (`heimdall-ctl`) raise instead of deleting.

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
| `heartbeat_interval` | int | `100` | Frames between heartbeat events |
| `en_status_server` | bool int | `0` | Serve TCP :5002 |
| `status_server_port` | int | `5002` | Base port (instance offset applies) |
| `en_zmq_pub` | bool int | `0` | Publish events on ZMQ PUB |
| `zmq_pub_port` | int | `5003` | Base PUB port |
| `event_ring_size` | int | `500` | Events kept for `EVENTS` queries |
| `drop_window_sec` | float | `60.0` (**optional key**, absent by default) | Window for the status server's `recent_drops` health derivation |

## [offload] (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `rebuffer_transport` | string | `shm` | `shm` / `spi` / `pcie` / `usb3` / `net` — non-shm needs the driver compiled in |
| `decimator_transport` | string | `shm` | as above |
| `delay_sync_transport` | string | `shm` | as above |
| `fir_engine` | string | `auto` | `auto` / `neon` (`cpu_neon`) / `kfr` (`cpu_kfr`) / `generic` (`cpu_generic`) / `fpga` / `gpu`. `auto` uses the platform engine compiled into `decimate.out`; `generic` selects the dependency-free plain-C engine (always available in kfr/ne10 builds) |
| `fft_engine` | string | `auto` | Python-side FFT engine selection (`offload_engines.py`) |

## [dma], [fpga], [gpu], [pcie], [usb3], [hat_uart], [hat_i2c] (optional hardware offload)

All carry `enable = 0` by default and are inert unless the corresponding
driver was compiled in / hardware exists.

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

| Key | Type | Default | Behavior |
|---|---|---|---|
| `instance_id` | int | `0` | 0 = unprefixed resources and base ports (backward compatible); N > 0 = `inst<N>_` prefix on shm/FIFOs/control files, logs under `_logs/instN/` |
| `port_stride` | int | `100` | Port offset multiplier: `port = base + instance_id × port_stride` |
| `en_federation` | bool int | `0` | Enable federation health/coordination |
| `coordinator_host` | string | empty | Coordinator address |
| `coordinator_port` | int | `6000` | Coordinator port |
| `peer_list` | list | empty | Peers as `host:instance_id` (canonical); legacy `host:status_port` entries (number ≥ 1024) still parse |

## [amplification] — external LNAs / gain staging (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_amplification` | bool int | `0` | Enable the link-budget subsystem |
| `ext_lna_gains_db` | float list | `0,0,...` | Per-channel external LNA gain, **continuous dB** (never quantized to the R820T table) |
| `ext_lna_nf_db` | float list | `0,0,...` | Per-channel LNA noise figure for the Friis cascade |
| `ext_lna_p1db_dbm` | float list | `99,99,...` | Output-referred P1dB; `99` = not modelled |
| `tuner_nf_db` | float | `3.5` | Tuner reference NF (R820T ≈ 3.5 dB) |
| `expected_input_dbm` | float | `-60` | Assumed antenna-port level for the (advisory) headroom estimate |
| `p1db_margin_db` | float | `3` | Warn when estimated output is within this margin of P1dB |
| `max_total_gain_db` | float | `80` | Warn when total system gain exceeds this |
| `en_bias_tee_runtime` | bool int | `0` | Allow the runtime `BIAS` control command |
| `bias_tee_state` | int list | `0,0,...` | Initial per-channel bias-tee state (applied at init when amplification is on) |
| `lna_freq_table` | string | empty | Optional frequency-dependent response: `fMHz:gain_db:nf_db;...` (empty = flat) |

Compression detection is **advisory only** — it sets header slot 96 and emits
events but never changes gains automatically.

## [antenna] — directional antenna profile (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_antenna_profile` | bool int | `0` | Enable the profile |
| `profile_name` | string | `isotropic` | Label |
| `element_gain_dbi` | float | `0` | Antenna element gain (enters the link budget) |
| `beamwidth_az_deg` / `beamwidth_el_deg` | float | `360` | −3 dB beamwidths |
| `polarization` | string | `vertical` | `vertical` / `horizontal` / `rhcp` / `lhcp` / `slant45` / `dual` |
| `cable_loss_db` / `connector_loss_db` | float | `0` | Feed losses (enter the budget as pre-LNA loss) |
| `boresight_az_offset_deg` / `boresight_el_offset_deg` | float | `0` | Mechanical→electrical boresight offset applied to commanded bearings |

## [orientation] — rotator / pan-tilt (optional)

| Key | Type | Default | Behavior |
|---|---|---|---|
| `en_orientation` | bool int | `0` | Enable the orientation controller (runs inside hw_controller) |
| `backend` | string | `mock` | `mock` / `gs232` / `pwm_servo` / `i2c_pantilt`; the factory falls back to mock so the chain always starts |
| `device` | path | `/dev/ttyUSB0` | Serial device (gs232) |
| `baud` | int | `9600` | Serial baud (gs232) |
| `i2c_bus` / `i2c_address` | int / hex | `1` / `0x40` | PCA9685 bus/address (i2c_pantilt) |
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

## Preset catalog (`config_files/`)

| Preset | Purpose |
|---|---|
| `kraken_default/` | 5-channel KrakenSDR defaults (mirrors the shipped live config) |
| `kerberos_default/` | 4-channel KerberosSDR |
| `kraken_development/` | 5-channel development variant |
| `unit_test_k4/` | 4-channel values the sudo pipeline tests require (`daq_buffer_size=262144`, `cpi_size=262144`, `decimation_ratio=1`, `corr_size=65536`, 1 MHz sample rate) |
| `directional_df/` | The worked amplified-receiver + high-gain-antenna + GS-232-rotator example — the one preset with `[amplification]`, `[antenna]`, `[orientation]` enabled (see [tutorial_directional_df.md](tutorial_directional_df.md)) |
| `performance/minimal.conf`, `balanced.conf`, `maximum.conf` | `[performance]` profiles documenting CPU affinity, memory locking, RT priorities, LTO/PGO, kernel tuning choices. Note: the `enable_batched_control` key in these files is orphaned — no code reads it (the batched-control API was removed) |

`util/cfg_gen.py` generates a `daq_chain_config.ini` from signal parameters.
