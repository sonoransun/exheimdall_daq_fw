# Changelog

All notable changes to the HeIMDALL DAQ Firmware are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

This section covers two bodies of work that have not been tagged yet:

1. **The scaling and clustering program** (firmware version `1.1.0-dev`,
   `Firmware/_daq_core/version.py`). It adds multi-unit, multi-instance and
   multi-host operation. Every new feature is **opt-in**: it rides on a new
   INI key, a new CLI flag or a new tool. With every new key absent, the
   default path is byte-identical to the previous tree, except for the 16
   ledger rows listed under [Changed (default path)](#changed-default-path).
2. **The earlier build, correctness and tooling overhaul**, kept below
   under [Build, correctness and tooling overhaul](#build-correctness-and-tooling-overhaul).

Some parts of the program were still being implemented when these notes were
written. Entries whose files are not in the tree yet are marked
**(in progress)**; do not follow them until the files exist. Some other
entries carry an HTML comment `<!-- verify-after: WP-xx -->` (visible in the
source of this file). The reconciliation pass before tagging removes or
confirms every one of these markers.

### Upgrade notes

Read [Documentation/upgrade_and_rollback.md](Documentation/upgrade_and_rollback.md)
before updating a running station or a fleet. The short version:

- **Rebuild the C core on every update** (`cd Firmware/_daq_core && make`).
  A Python-only update leaves an old `decimate.out`. For instance N > 0 the
  FIR designer now writes `_data_control/inst<N>_fir_coeffs.txt` and deletes
  a stale unprefixed file in a separate instance directory, so an old binary
  fails loudly instead of filtering with stale taps (ledger L11).
  <!-- verify-after: WP-04 -->
- **`ini_checker.py` rejects three configurations that never worked**:
  `[hw] num_ch > 31`, `[pre_processing] cpi_size > 2^23` and
  `[calibration] corr_size > 2^23` (ledger L6). The start scripts enforce the
  checker's exit code, so such a config no longer starts.
- **The IQ header stays 1024 bytes and `header_version` stays 8.** Reserved
  slots 103-113 are new and read 0 unless their opt-in key or flag is set.
  Consumers that ignore `reserved[]` are unaffected.
- **The `daq_db` schema generation is unchanged (2).** No database is
  discarded by this update.
- **heimdall-ctl talks XACK first.** With the default `--ack auto`, the first
  mutating command to a `host:port` sends `XACK`. An old `hw_controller`
  answers `FNSD` + zeros and logs one `Unidentified control command: XACK`
  line; heimdall-ctl then remembers that server as legacy for 24 h. Use
  `--ack legacy` (alias `--legacy-ack`) to skip the handshake (ledger L15).
  After you upgrade a station, run one `heimdall-ctl --ack xack <command>`
  against it, or delete `~/.cache/heimdall-ctl/ack_modes.json`, so later
  default commands use XACK again. Until then a cached legacy entry skips
  extended acks and `track-lock approve|revoke` is refused with exit 1. See
  [upgrade_and_rollback.md section 7.3](Documentation/upgrade_and_rollback.md#73-expected-log-lines-and-the-client-cache).
- **`daq_start_sm.sh` changed.** It now sources `Firmware/daq_common.sh` and
  exits 1 if that file is absent, so update both together. It validates with
  `ini_checker.py no_hw attached` (ledger L7) and accepts `--takeover` (with
  `[host] instance_guard = refuse`) and `--no-host-tuning`. A copied or
  customised start script must be merged by hand.
- **Mixed-version clusters work.** A new coordinator maps legacy acks. An old
  member logs `Unidentified control command: XACK` once per coordinator
  command; this line is expected (ledger L13). <!-- verify-after: WP-08 -->
- **Library callers of `heimdall_ctl`**: `CtlClient._send` raises
  `ValueError("payload exceeds 124 bytes")` instead of truncating, and
  connection failures raise `EndpointError`, an `OSError` subclass, so
  `except OSError` still catches them.

### Changed (default path)

These are the only default-path differences of the program (spec section 0.4,
"disabled-state ledger"). Each one is a fix for an audited defect, a passive
cleanup that does nothing unless an opt-in feature created files, or a change
of log text only.

- **L1** `daq_stop.sh` with no argument runs a registry prune after today's
  sweep. It deletes `$HEIMDALL_RUN_DIR/inst*.json` and `inst*.env` whose pids
  are all dead. Only a start with `[host] instance_registry = 1` writes those
  files, so on a host that never opted in the step does nothing.
  <!-- verify-after: WP-12 -->
- **L2** The start scripts create `_data_control/<P>iq_track_lock` containing
  `0` when it is missing. Why: without it `HWC.init()` failed, so a fresh
  clone had no control plane. <!-- verify-after: WP-12 -->
- **L3** `hw_controller` opens `iq_track_lock` lazily, only when
  `[calibration] require_track_lock_intervention = 1`, and re-opens it on
  every check. A missing file then means "not approved" and is logged once.
  Why: the file was never read when the key is 0, but its absence still made
  `init()` fail.
- **L4** `fir_decimate.c` and `iq_server.c` compute shared-memory sizes in
  `size_t` (new `frame_sizes.h`). Values are identical for up to 31
  channels; 32 channels overflowed `int`. <!-- verify-after: WP-04 -->
- **L5** The start scripts' port-busy check looks at listening sockets only
  (`ss -ltnH`, then `lsof ... -sTCP:LISTEN`) and probes
  `[monitoring] status_server_port` + offset instead of a literal 5002.
  Ports `1130` + offset and the event PUB port get an advisory `WARN:` line
  only. Why: `lsof -i:P` matched outbound client sockets, so a DoA client
  connected to `:5000` triggered a host-wide kill. The retry loop, its
  `./daq_stop.sh`, the 10 attempts and exit 1 are unchanged.
  <!-- verify-after: WP-12 -->
- **L6** `ini_checker.py` errors on `num_ch > 31`, `cpi_size > 2^23` and
  `corr_size > 2^23` in every mode. Why: 32 channels overflow the shared
  memory size and the 128-byte `:1130` message holds 31 values; frames above
  2^23 samples overrun the worst-case segments. Port-window problems are
  errors only in new-key mode (a config that uses `cluster_members`,
  `coordinator_candidates`, `register_with_coordinator = 1`, any `router_*`
  key, `[host] instance_registry = 1` or `instance_guard = refuse`) and
  warnings otherwise.
- **L7** `ini_checker.py` prints warnings to stderr as `WARNING: <text>`.
  Warnings never change the exit code (0 valid, 1 errors). Every shipped
  preset passes `no_hw` without a warning. `daq_start_sm.sh` now passes the
  `attached` token (sysfs serial and host-registry checks). With
  `serial_base`/`device_serials` absent it prints nothing unless an RTL2832U
  is attached and a duplicate-serial or registry condition holds. It never
  changes the exit code.
- **L8** `heimdall-daq.service` stops with `daq_stop.sh 0` instead of the
  host-wide no-argument form, and gains `EnvironmentFile=-`,
  `ConditionPathExists` and `Conflicts=` with the supervised unit. On a
  single-instance host the same processes stop. <!-- verify-after: WP-16 -->
- **L9** Simulation lane. The synthesizer
  (`_testing/test_data_synthesizer.py`) answers ZMQ control commands on
  `1130 + instance_id * port_stride`, so `hw_controller` gets replies and the
  simulated chain can calibrate. It still stamps header `unit_id = 0` unless
  `[hw] unit_id = auto` or `--unit-id-from-config` is given, and it stays
  unpaced, unseeded and 10000 blocks long by default. Its DUMMY frames are
  header-only (`cpi_length = 0`, no payload), as in `rtl_daq`; before, they
  carried a full payload. Because the responder answers `hw_controller`,
  default simulated runs now emit these dummy frames while a command takes
  effect.
  `daq_synthetic_start.sh` exits 1 when the FIR designer fails (a bare `exit`
  returned 0), probes ports in advisory mode only, runs `hw_controller` with
  `sudo env "PATH=$PATH"` and creates `iq_track_lock` (L2).
  <!-- verify-after: WP-12 (daq_synthetic_start.sh part) -->
- **L10** `auto_config.py` rewrites the INI atomically (temp file, fsync,
  `os.replace`, mode and owner kept). It rewrites nothing for shipped INIs,
  which have no `= auto` values.
- **L11** For instance N > 0 only: `hw_discover.py` output goes to
  `_data_control/inst<N>_hw_caps.json`, which `auto_config.py` reads, and
  `fir_filter_designer.py` writes `_data_control/inst<N>_fir_coeffs.txt`,
  which `decimate.out` reads. Why: two instances in one working copy
  overwrote each other's files. Instance 0 is unchanged.
  <!-- verify-after: WP-04, WP-12 -->
- **L12** The start scripts delete `_data_control/<P>recal_request` and
  `<P>hwc_state` next to the FIFO recreation. Only the new relays create these
  files. <!-- verify-after: WP-12 -->
- **L13** Coordinator (`federation_coordinator.py`; no script or unit starts
  it by default). `INSTANCE` with a non-integer id replies
  `{"error":"Usage: INSTANCE <id> <command>"}` instead of closing the socket.
  `REBALANCE` without a master schedule replies
  `{"error":"no master schedule"}` instead of a false success. `STATUS` counts
  unreachable members as `error`. Fan-out runs in parallel with a deadline
  fixed at receipt, and the per-command `:5001` read timeout is at least 14 s
  instead of 5 s. Every per-member command is preceded by `XACK`. Concurrent
  mutations are serialised and may reply `{"error":"busy","retry_in_sec":1}`.
  The listen backlog is 32 and a bounded handler pool (`max_handlers = 64`)
  replaces one thread per connection. CLI flags now win over `--config`.
  <!-- verify-after: WP-08 -->
- **L14** IQ router (`federation_iq_router.py`; it had no `__main__`
  before). Forwarding is non-blocking with bounded per-client queues,
  `get_stream_stats()` returns a deep copy, and source readers get a read
  timeout, keepalive and reconnect backoff. <!-- verify-after: WP-09 -->
- **L15** heimdall-ctl (client only):
  1. Mutating verbs read and report the reply. Socket errors print
     `Error: ...` and exit 3 instead of a traceback. `--instance N` applies
     the port stride without a config. `events --tail` decodes the ZMQ
     multipart frames. `CtlClient._send` raises on payloads over 124 bytes
     instead of truncating.
  2. With the default `--ack auto`, the first mutating command to a
     `host:port` sends `XACK`. A legacy server is remembered in
     `~/.cache/heimdall-ctl/ack_modes.json` (or `$XDG_CACHE_HOME/heimdall-ctl/`)
     for 24 h, after which commands go out without XACK as before.
  3. When XACK is not answered within 3 s, the command is not sent (exit 3):
     `server did not answer XACK within 3 s (another client may hold the control port); command not sent - retry, or use --ack legacy to queue it behind that client`.
     Before, the frame waited in the kernel backlog and executed later.
  4. Mutating requests use the server-derived timeout `T_cmd` (14 s against
     a legacy server) instead of 5 s. Queries keep 5 s.
  5. A `gain`, `lna-gain` or `bias-tee` list whose length differs from the
     channel count prints
     `warning: expected M values, got k (the server uses the first M)` and is
     sent as before. `--strict` refuses it (exit 1). The count comes from
     XACK or extended STATUS; against a legacy server it comes from
     `--config` `[hw] num_ch`, else 5. Pass `--config` when you use
     `--strict` against a legacy server.

  `CtlClient.parse_reply()` keeps its return shape (`ok`, `data`, `raw`) and
  only gains keys; the new `parse_ack()` returns the ack mapping. Exit code 0
  still covers an unconfirmed reply from a legacy server. Escape hatch:
  `--ack legacy`.
- **L16** Log text only:
  - `hw_controller` no longer logs the stale `Reference channel index is fixed 0`.
  - `rtl_daq` logs `ZMQ 'r' reconfiguration is deprecated` once per process,
    appends ` - raise [daq] usb_ring_buffers (now N)` to the overrun warning,
    and logs `antenna switch GPIO: none (skipped)` only when
    `[hw] antenna_switch_gpio = none`.
  - `rtl_daq` logs `serial %s is present on %d devices; program distinct serials with util/set_unit_serials.sh`
    as a warning when two attached devices share a serial and no serial key
    is set (it still uses the first matching device). The fatal
    `The serial numbers of the devices are not yet configured` line gains
    `(expected serial %s for channel %d; see [hw] serial_base/device_serials and util/set_unit_serials.sh)`.
  - `decimate.out` logs `FIR coefficients: <path> (<n> taps)` at info level.
    <!-- verify-after: WP-04 -->
  - The start scripts' core guard prints today's `WARN:` text; a line naming
    the overlapping instance appears only with `instance_registry = 1`.
    <!-- verify-after: WP-12 -->
  - `hw_controller` keeps `listen(4)` on `:5001` when
    `ctl_max_connections = 1` and uses `listen(16)` only when it is above 1.

### Added: scaling (multi-unit and multi-instance hosts)

All opt-in. See [Documentation/scaling_and_clustering.md](Documentation/scaling_and_clustering.md)
and [Documentation/capacity_planning.md](Documentation/capacity_planning.md).

- **Receiver selection by EEPROM serial** (`rtl_daq`, new `device_map.c`).
  `[hw] serial_base` (absent = 1000) maps channel i to serial
  `serial_base + i`. `[hw] device_serials` lists exactly `num_ch` distinct
  1-8 digit serials and wins over `serial_base`. When two attached devices
  carry the same serial, `rtl_daq` exits with a fatal error if serial keys are
  set, and logs a warning otherwise (it still uses the first matching device,
  as before; ledger L16).
- **`[hw] unit_id = auto`**: the header `unit_id` becomes
  `[federation] instance_id`. Honoured by `rtl_daq`, the synthesizer and the
  experimental HAT bridges.
- **`[hw] antenna_switch_gpio`** (pigpio builds only): absent = GPIO 23/24 as
  before; `none` skips pigpio initialisation; `A,B` picks two pins 0..27.
- **`[daq] zmq_listen_address`**: bind address of `rtl_daq`'s ZMQ socket
  (`*`, `0.0.0.0`, `127.0.0.1` or `localhost`; absent = `*`).
- **`[daq] usb_ring_buffers` / `usb_async_transfers`** (8 / 12, range 2..64):
  USB ring depth and in-flight transfers, previously literals.
- **Config-sized shared memory**: `[pre_processing] size_segments_from_config = 1`
  sizes `decimator_out` and the `iq_server` mapping from the configured frame
  instead of the 2^23-sample worst case. <!-- verify-after: WP-04 -->
- **Multi-client `iq_server`**: `[data_interface] iq_server_max_clients`
  (1 = the old single-client loop, unchanged) and
  `iq_server_frame_deadline_ms` (0 = auto, 0.8 x frame period).
  <!-- verify-after: WP-04 -->
- **delay_sync throughput levers**: `[offload] fft_workers` and
  `xcorr_workers` (4 / 4; `auto` = core count), `[offload] skip_copy_on_drop`
  (0), `[calibration] track_decimation` (1) and `[host] delay_sync_cores`.
  <!-- verify-after: WP-06 -->
- **Database instance awareness** (`daq_db.py`): `[database] instance_subdir = 1`
  uses `<db_dir>/inst<N>` for instance N > 0; `write_instance_meta = 1` writes
  `_instance_meta.json` at open. `instance_id` must be a plain decimal
  integer >= 0. `hw_controller` and `delay_sync` both pass the two keys to
  `daq_db`.
- **`[host]` section** (start scripts): `cpu_cores` (4 distinct cores for the
  4 RT tiers), `delay_sync_cores`, `rt_priority_offset` (0..81),
  `allow_core_sharing` (`warn` | `refuse` | `allow`), `host_tuning`
  (`always` | `auto` | `1` | `0`), `preflight` (`off` | `warn` | `strict`),
  `instance_registry` (0), `instance_guard` (`legacy` | `refuse`),
  `usb_irq_core`, `usb_controller`, `health_thread_sched_other` (0). Absent =
  today's layout. <!-- verify-after: WP-12 -->
- **Host registry, instance lock and duplicate guard**: with
  `[host] instance_registry = 1` the start scripts write
  `$HEIMDALL_RUN_DIR/inst<N>.json` and `.env` (default `/run/heimdall`,
  fallback `/tmp/heimdall-run-<uid>`) and hold an instance lock and a startup
  mutex. `instance_guard = refuse` refuses a duplicate start with exit 3.
  New POSIX library `Firmware/daq_common.sh`. <!-- verify-after: WP-12 -->
- **Instance provisioning**: `util/provision_instance.py` creates
  `<root>/instances/<N>` as a symlink working copy with private
  `daq_chain_config.ini`, `_data_control/`, `_logs/` and `_db/`. Dry run by
  default; `--write` executes; instance 0 needs `--in-place`; `--refresh`
  re-links after an upgrade. <!-- verify-after: WP-14 -->
- **Serial programming**: `sudo util/set_unit_serials.sh --unit K --yes`
  programs unit K to serials `1000 + 100*K + i` and verifies them. Without
  `--yes` it only prints the plan. Stop the chain and plug in one unit at a
  time; `rtl_eeprom` must be on PATH. Otherwise it refuses with exit 3 (a
  chain holds the receivers), 4 (receiver count differs from `--count`) or
  5 (no `rtl_eeprom`).
- **Capacity model and pre-flight**: `Firmware/daq_preflight.py`
  (`--config ... --plus-live --add-instances N --host-json FILE --strict --json`;
  exit 0 ok, 1 warnings, 2 failures (or warnings with `--strict`), 3 usage
  error) on top of `_daq_core/capacity_model.py`.
  It checks cores (4 per hardware instance), ports, memory, `/dev/shm`,
  `RLIMIT_MEMLOCK`, USB bus budget and NIC egress. `[host] preflight = strict`
  makes the start exit 4 on failures. <!-- verify-after: WP-13 -->
- **Presets and profiles**: `config_files/multi_instance_x86/`,
  `config_files/cluster_site/` (with `federation.ini`) and
  `config_files/performance/{minimal,balanced,maximum}.ini` for
  `provision_instance.py --profile`. <!-- verify-after: WP-14 -->

### Added: clustering (multi-host federation)

All opt-in; nothing here runs unless you start it. See
[Documentation/scaling_and_clustering.md](Documentation/scaling_and_clustering.md),
[Documentation/networking_and_time_sync.md](Documentation/networking_and_time_sync.md)
and [Documentation/protocols.md](Documentation/protocols.md).

- **Identity**: `[federation] node_id` (default `<short lower-case hostname>:<N>`,
  grammar `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,47}$`) and `cluster_id` (default
  `default`). Shared helpers live in the new `_daq_core/federation_common.py`
  (membership parser, request framing, parallel fan-out) and
  `_daq_core/hwc_relay.py`.
- **`:5001` control-plane extensions** (`hw_controller`). A client that sends
  `XACK` first gets JSON extended acks
  `{"applied","reason","fsm","seq"}` with reasons such as `ok`,
  `not_confirmed`, `held_for_burst`, `queue_full`, `deferred_timeout`,
  `handler_timeout`, `stale_term`. New verbs: `HWSQ` (control snapshot, answered
  in milliseconds even while frames stall), `SCHG` (schedule generation),
  `TLCK` / `TLKA` (query / set the IQ track lock), `RECL` (wire
  recalibration), and `SCHB` / `SCHC` / `SCHE` (chunked schedule upload up to
  256 KiB). Clients that never send `XACK` get byte-identical replies.
- **`:5001` connection keys** (`[daq]`): `ctl_max_connections` (1; up to 64
  concurrent clients, so a resident DoA client no longer blocks the
  coordinator or heimdall-ctl), `ctl_request_timeout_sec` (10.0),
  `ctl_handler_max_sec` (12.0), `ctl_xack_idle_sec` (30) and
  `ctl_idle_timeout_sec` (0 = off).
- **Fencing at members**: an `XACK` carrying the member's own `cluster_id`
  raises the persisted term in `_data_control/<P>fence_term`. Mutations from
  an older term are refused with `stale_term`. A jump above
  `[federation] fence_max_term_jump` (1000) is refused with `term_jump`.
  The term survives restarts; to clear it, stop the instance and run
  `./daq_stop.sh N --reset-fence`.
- **Coordinator protocol v2** (`:6000`): new newline-terminated verbs
  `VERSION` (`protocol: 2`), `LEADER`, `HEALTH`, `NODES STATUS`, `MEMBERS`,
  `MEMBER ADD|REMOVE`, `REGISTER`, `PEERS`, `ALL`, `NODE`, `FANOUT`, `RECAL`,
  `SCHEDULE SET|GET|CLEAR|STRATEGY`, `ASSIGNMENTS`, `EVENTS`, `DB STATS` and
  `ROUTER STATS`. Legacy verbs keep their framing and only gain keys. Optional
  `AUTH <token>` prefix (`[federation] auth_token`) and `allow_cidrs`.
  <!-- verify-after: WP-08 -->
- **Membership and registration**: `[federation] cluster_members`
  (`host:instance_id[@node_id],...`), `advertise_host`,
  `register_with_coordinator` (0) and `register_interval_sec` (5.0).
  <!-- verify-after: WP-06, WP-08 -->
- **Lease election and failover** across coordinator candidates:
  `coordinator_candidates`, `coordinator_self`, `coordinator_include_self`
  (0); in `federation.ini`: `election_interval_sec` (2.0),
  `election_missed_polls` (3), `election_settle_polls` (5),
  `election_clock_margin` (0.05), `election_failback_sec` (60) and
  `require_quorum` (0; a quorum needs at least 3 candidates). New module
  `_daq_core/federation_election.py`. <!-- verify-after: WP-08 -->
- **Peer health state machine**: `health_mode` (`mesh` | `hub` | `off`),
  `health_poll_interval` (5.0), `peer_timeout` (2.0), `peer_up_successes` (1),
  `peer_down_misses` (absent = the old time-only rule),
  `peer_flap_window_sec` (0), `peer_flap_threshold` (0),
  `emit_peer_health_events` (0) and `health_workers` (1).
  <!-- verify-after: WP-08 -->
- **Coordinator tuning keys** (`federation.ini`): `fanout_workers`,
  `peer_connect_timeout_sec`, `xack_timeout_sec`, `hwc_request_timeout_sec`,
  `max_member_rto_sec`, `status_timeout_sec`, `status_cache_sec`,
  `mutation_wait_sec`, `breaker_failures`, `breaker_open_sec`,
  `xack_cache_sec`, `schc_window`, `schc_rtt_budget_ms`,
  `member_stall_after_sec`, `reconcile_assignments`, `reconcile_interval_sec`,
  `reconcile_max_attempts`, `reconcile_override`, `register_expiry_factor`,
  `register_retain_sec`, `state_dir`, `router_stats_endpoint`, `max_handlers`,
  `reserved_handlers`, `sync_push_timeout_sec`, `dns_ttl_sec`. Defaults are in
  [Documentation/configuration.md](Documentation/configuration.md).
  <!-- verify-after: WP-08 -->
- **Scheduler**: one canonical schedule format with a generation counter,
  partitioning by `round_robin` or `range`, and optional reconciliation of
  member assignments. <!-- verify-after: WP-08 -->
- **IQ router CLI** (`python3 _daq_core/federation_iq_router.py`): unified
  output on `:7000`, stats line protocol on `:7001`, bounded per-client
  queues (`--queue-frames 4`, `--drop-policy oldest|newest|disconnect`),
  tagging (`--tag-mode instance|unit|node`, `--stamp-seq`, `--stamp-rx-ts`),
  bounded alignment (`--align-window-ms 0`), connect-on-demand and chaining
  of routers via `router://host:port` sources. INI keys `router_port`,
  `router_stats_port`, `router_sources`, `router_include_self`,
  `router_queue_frames`, `router_drop_policy`, `router_tag_mode`,
  `router_align_window_ms`, `router_align_max_frames`, `router_max_skew_ms`,
  `router_connect_on_demand`, `router_max_clients`, `router_send_timeout_sec`,
  `router_read_timeout_sec`, `router_first_frame_timeout_sec`,
  `router_reconnect_sec`. Exit 2 on a port bind failure, 3 on bad arguments.
  <!-- verify-after: WP-09 -->
- **Federation launchers**: `Firmware/federation_start.sh` and
  `federation_stop.sh` (`coordinator|router|all`, `--foreground` for
  systemd). <!-- verify-after: WP-12 -->
- **Cluster config generator**: `util/cluster_cfg_gen.py --template PATH --hosts hosta:2 hostb:1 --out DIR`
  emits per-instance INIs, `federation.ini`, `ports.md` and `provision.sh`,
  and validates them. <!-- verify-after: WP-14 -->

Multi-host islands are not coherent with each other: each KrakenSDR unit is
its own coherent island, and cross-host alignment is wall-clock only (needs
NTP or PTP). See
[Documentation/theory_coherent_acquisition.md](Documentation/theory_coherent_acquisition.md).

### Added: observability

- **IQ header slots 103-113** (`iq_header.h` / `iq_header.py`, lockstep,
  checked by `test_iq_header_abi.py`). Each slot is 0 unless its opt-in is
  set:

  | Slot | Content | Opt-in |
  |---|---|---|
  | 103 | router sequence number | router `--stamp-seq` |
  | 104-105 | router receive time, Unix ms (LO, HI) | router `--stamp-rx-ts` |
  | 106 | source `instance_id + 1` (0 = untagged) | router `--tag-mode node` |
  | 107 | crc32 of the node key `host:N` | router `--tag-mode node` |
  | 108 | crc32 of `[federation] node_id` | `[federation] stamp_node_id = 1` |
  | 109 | decimator drop count | `[monitoring] stamp_decimator_drops = 1` |
  | 110 | calibration quality, signed milli-dB | `[monitoring] stamp_cal_telemetry = 1` |
  | 111 | IQ compensation count | `[monitoring] stamp_cal_telemetry = 1` |
  | 112 | sample compensation count | `[monitoring] stamp_cal_telemetry = 1` |
  | 113 | recalibration generation | `[monitoring] stamp_cal_telemetry = 1` |

  `decimate.out` stamps slot 109, `delay_sync` stamps 108 and 110-113, and
  the router stamps 103-107.
- **Extended STATUS** (`:5002`): `[monitoring] status_extended = 1` adds
  `status_protocol: 2`, identity, `fw_version`, `config_sha256`, frame
  geometry, `fsm_state`, a `cal` block, overrun and gap counters, an optional
  `hwc` block and a `federation` block. It also serves the new verbs `PEERS`,
  `HWC` and `HW_SNAPSHOTS` and the 2- and 3-argument `CAL_HISTORY` forms, and
  replies `{"error":"busy"}` at the client cap (`status_max_clients`, 8). With
  the key 0 every `:5002` reply is byte-identical. <!-- verify-after: WP-06 -->
- **STATUS freshness**: `[monitoring] status_update_interval` (frames;
  0 = follow `heartbeat_interval`) and `stale_after_sec` (0 = auto); a stale
  snapshot reports `pipeline_health = "stale"` (extended mode).
  <!-- verify-after: WP-06 -->
- **hw_controller state relay**: `[monitoring] en_hwc_state_relay = 1` writes
  `_data_control/<P>hwc_state` every 5 frames and at once on new events.
  `delay_sync` reads the file, re-publishes the `hw_controller` events and
  serves it as the `hwc` block of extended STATUS.
- **Event identity**: `[monitoring] event_tag_identity = 1` tags events with
  `{instance_id, node_id, host, cluster_id}`; `event_topic_prefix = 1`
  publishes PUB topics as `inst<N>.<event_type>`. New event types `recal_requested` and `cal_stalled`
  (emitted by `hw_controller`), plus `peer_recovered`, `peer_flapping`,
  `coordinator_command`, `node_key_collision` and
  `schedule_reconcile_failed`. <!-- verify-after: WP-06, WP-08 -->
- **Wire recalibration**: `[calibration] en_wire_recal = 1` enables `RECL`,
  which writes `_data_control/<P>recal_request`, emits `recal_requested` and
  re-enters `STATE_INIT`. Without the key `RECL` replies `disabled` and
  `heimdall-ctl recal` falls back to `INIT`. `delay_sync` watches `recal_request`.
- **Calibration opt-ins** (`[calibration]`): `defer_control_during_burst`
  (0; holds FREQ/GAIN/EGAN/BIAS while the noise source is on),
  `cal_stall_frames` (0; emits `cal_stalled` once, no hardware action),
  `recal_on_gain_change` (0; needs `en_wire_recal = 1`). Also
  `[amplification] apply_bias_tee_at_init` (0; needs
  `en_bias_tee_runtime = 1`).
- **Calibration robustness** (`[calibration]`, delay_sync):
  `amplitude_tolerance_mode` (`legacy` | `symmetric`), `cal_timeout_frames`
  (0; emits `cal_timeout` and one warning, no hardware action),
  `invalidate_on_overrun` (0) and `frames_after_overrun` (1; clear the sync
  flags on frames after a slot-102 overrun increase).
  <!-- verify-after: WP-06, WP-22 -->

### Added: operations and packaging

See [Documentation/operations_runbook.md](Documentation/operations_runbook.md)
and [Documentation/deployment_recipes.md](Documentation/deployment_recipes.md).

- **Config validation** (`ini_checker.py`): validates every new key; new CLI
  tokens `attached` (compares configured serials with the RTL2832U devices in
  sysfs and with the live host registry) and `--federation` (validates a
  coordinator or router `federation.ini`). Usage:
  `python3 ini_checker.py [config_path] [no_hw] [attached] [--federation]`.
  Exit 0 valid, 1 errors. `daq_start_sm.sh` passes `attached` on every start
  (ledger L7).
- **Receiver discovery**: `python3 _daq_core/hw_discover.py --sdr` prints the
  attached receivers as JSON (`{"rtlsdr": {"count", "devices", "sysfs"}}`).
  The start scripts do not pass the flag, so `hw_caps.json` is unchanged.
- **Shipped INI documentation**: `Firmware/daq_chain_config.ini` documents
  every new key as a commented `;key = default` line, including a commented
  `[host]` block. The parsed content is unchanged.
- **Synthetic source** (`_testing/test_data_synthesizer.py`): ZMQ control
  responder (see L9), a sample-clock model for the `s` command
  (`_testing/synth_model.py`), and new flags `--seed` (0..4294967295),
  `--pace`, `--blocks` (default 10000, 0 = unbounded), `--zero-delays` and
  `--unit-id-from-config`. DUMMY frames are header-only (`cpi_length = 0`,
  no payload) and carry the current `noise_source_state`, as in `rtl_daq`.
- **Start-script flags**: `daq_start_sm.sh --takeover --no-host-tuning`;
  `daq_synthetic_start.sh --rt --pace --blocks N --seed S --takeover --no-host-tuning`.
  `HEIMDALL_SCOPED_STOP=1` makes the port-busy loop run `daq_stop.sh N`
  instead of the host-wide stop.
- **Per-instance stop forms** (`daq_stop.sh`): `<N>`, `<N> --clean`,
  `<N> --reset-fence`, `--this-copy`, `--all` and `--federation`. The
  no-argument form stays the host-wide kill and keeps its exit status. The
  new forms exit 2 on conflicting flags and 3 when `--clean` or
  `--reset-fence` is refused because a process of the instance is still
  alive.
- **Supervisor**: `Firmware/daq_supervise.sh` (`--instance N --synthetic --interval --healthcheck --grace --stall-sec --allow-legacy-guard`)
  starts a chain, watches every stage's PID file (and optionally the status
  server), and on the first failure stops it with the scoped
  `daq_stop.sh N` and exits 10-16 naming the failed stage. It does not
  restart the chain itself; the supervised systemd units restart it.
- **Host tuning**: `util/host_tuning.sh --apply|--check` and new opt-in
  `util/irq_tuning.sh` flags (`--usb-core`, `--controller`, `--eth-core`,
  `--no-governor`, `--keep-going`, `--any-arch`). The default control flow of
  `irq_tuning.sh` is unchanged. <!-- verify-after: WP-12 -->
- **systemd units (in progress)** (`packaging/systemd/`): supervised units
  `heimdall-daq-supervised.service` and `heimdall-daq-supervised@.service`,
  `heimdall-coordinator.service`, `heimdall-iq-router.service`,
  `heimdall-host-tuning.service`, and `heimdall-daq-watchdog.service` +
  `.timer` (`util/heimdall_watchdog.py`). Also env templates in
  `packaging/default/`, a udev rule `packaging/udev/99-heimdall-rtlsdr.rules`
  and an updated logrotate policy. <!-- verify-after: WP-16 -->
- **Recording and replay (in progress)**: `.hcap` capture container
  (`_testing/iq_capture_format.py`), `iq_recorder.py --hcap`,
  `_testing/iq_replay.py` (`--serve-port` emulates `iq_server`;
  `--raw-to-stdout` feeds `rebuffer.out`), `_testing/iq_latency_probe.py` and
  `Firmware/daq_replay_start.sh`. <!-- verify-after: WP-20 -->

### Added: tooling

- **heimdall-ctl** (see [Documentation/protocols.md](Documentation/protocols.md)):
  - New global flags: `--version`, `--coordinator H:P[,H:P...]`,
    `--router H:P`, `--auth-token` (or `HEIMDALL_AUTH_TOKEN`), `--timeout`,
    `--ack {auto,xack,legacy}`, `--legacy-ack`, `--strict`, `--verify`,
    `--firmware-dir`, `-v`.
  - New local subcommands: `db-stats`, `hwc-status` (HWSQ), `track-lock
    [approve|revoke]` (TLCK/TLKA), `peers`, `ports`, `instances` (host
    registry), `capacity` (runs the pre-flight tool). `recal` sends `RECL`
    when the server supports it, else `INIT`.
  - `fleet` subcommands via the coordinator: `status`, `ping`, `version`,
    `leader`, `health`, `peers`, `assignments`, `db-stats`, `router-stats`,
    `members`, `tune`, `gain`, `lna-gain`, `bias-tee`, `agc`, `recal`, `park`,
    `bearing`, `hwc-status`, `exec`, `schedule`, `rebalance`, `events`.
  - `router stats|sources|clients [--watch]` for the router stats port.
  - Exit codes: 0 ok, applied or unconfirmed (legacy server); 1 error or not
    applied; 2 usage; 3 connection failure or timeout; 4 `not_leader`;
    5 capacity failures.
  - In-tree launcher `util/bin/heimdall-ctl`.
  - `fleet` talks to `federation_coordinator.py`, `router` to the router
    stats port and `capacity` runs `Firmware/daq_preflight.py`.
- **Version string**: `Firmware/_daq_core/version.py` holds
  `FW_VERSION = "1.1.0-dev"` (`python3 _daq_core/version.py` prints it). The
  `heimdall-ctl` package version in `pyproject.toml` stays separate.
- **CI** (`.github/workflows/ci.yml`, `ci/build.sh`, `Firmware/unit_test.sh`):
  - new `pipeline` job runs `./unit_test.sh --sudo` on an Ubuntu VM; the
    nightly schedule adds a 600 s multi-instance soak;
  - new `deploy-lint` lane (`ci/build.sh deploy-lint`): `bash -n` and
    ShellCheck, `systemd-analyze verify`, preset and generator validation,
    `--help` smoke tests, udev rules;
  - registered test modules that do not exist yet print `PEND` and do not
    fail; `HEIMDALL_STRICT_MODULES=1` turns `PEND` into `FAIL`;
  - `./unit_test.sh --list` prints the registered modules per lane;
  - `LIBRTLSDR_REF` (default `master`) and `KFR_REF` (default `7.1.0`) pin the
    dependency builds in `ci/build.sh`; `util/install.sh` honours `KFR_REF`
    only.
- **Test modules**: `test_device_map`, `test_federation_common`,
  `test_hwc_relay`, `test_hwc_init_instance`, `test_synthetic_control`,
  `test_sync_loop_model`, `test_heimdall_ctl_fleet`,
  `test_federation_protocol`, `test_federation_election`, `test_iq_router`,
  `test_status_extended`, `test_delay_sync_fsm`, `test_capacity_model`,
  `test_frame_sizes`, `test_launch_scripts`, `test_provisioning` and
  `test_set_unit_serials`, plus extended `test_iq_header_abi`,
  `test_iq_header_v8`, `test_control_iface`, `test_ini_checker`,
  `test_daq_db` and `test_heimdall_ctl`. Registered but not in the tree yet
  (in progress, reported as `PEND`): `test_watchdog`,
  `test_iq_capture_format`, `test_iq_replay`, and the sudo integration lanes
  `test_start_stop_legacy`, `test_multi_instance`, `test_synthetic_chain`,
  `test_bit_identity` and `test_throughput_lane`.
- **Performance tooling**: `util/performance_monitor.py` measures latency
  through a real `IQDownload` client. The throughput lane (in progress)
  will write `Firmware/_testing/test_logs/throughput.json`.
  <!-- verify-after: WP-13 -->

### Added: documentation

New guides in `Documentation/`:
[scaling_and_clustering.md](Documentation/scaling_and_clustering.md),
[deployment_recipes.md](Documentation/deployment_recipes.md),
[networking_and_time_sync.md](Documentation/networking_and_time_sync.md),
[operations_runbook.md](Documentation/operations_runbook.md),
[capacity_planning.md](Documentation/capacity_planning.md),
[hardware_setup.md](Documentation/hardware_setup.md),
[consumer_guide.md](Documentation/consumer_guide.md),
[upgrade_and_rollback.md](Documentation/upgrade_and_rollback.md),
[theory_coherent_acquisition.md](Documentation/theory_coherent_acquisition.md) and
[glossary_faq.md](Documentation/glossary_faq.md).
Updated: `protocols.md`, `configuration.md`, `architecture.md`,
`developer_guide.md`, `tutorial_directional_df.md`, `README.md`,
`CONTRIBUTING.md` and this file. A log legend for source trees is in
`Firmware/_logs/logs`; the authoritative copy is the "Logs" section of the
runbook.

### Fixed

Fixes on the default path are the ledger rows above. In short:

- A fresh clone had no control plane, because `HWC.init()` failed without
  `_data_control/iq_track_lock` (L2, L3).
- The port-busy check matched outbound client sockets and killed every DAQ
  process on the host when a DoA client was connected (L5).
  <!-- verify-after: WP-12 -->
- `auto_config.py` rewrote the INI non-atomically (L10).
- Two instances in one working copy overwrote each other's `hw_caps.json`
  and `fir_coeffs.txt` (L11). <!-- verify-after: WP-04, WP-12 -->
- `num_ch = 32` overflowed the shared-memory size; frames above 2^23 samples
  overran the worst-case segments; both are now rejected by the checker (L4,
  L6).
- The simulation lane could not calibrate, because nothing answered
  `hw_controller`'s ZMQ commands; `daq_synthetic_start.sh` returned 0 after
  a FIR-designer failure (L9).
- `heimdall-daq.service` stopped every instance on a multi-instance host (L8).
  <!-- verify-after: WP-16 -->
- `rebuffer.out 1` (drop mode, not used by the start scripts) re-sent an
  undelivered CPI after a frame drop. <!-- verify-after: WP-04 -->
- Coordinator: silent socket close on `INSTANCE` with a bad id, false success
  on `REBALANCE`, a 5 s read timeout shorter than `hw_controller`'s own 10 s
  wait, and `--config` overriding CLI flags (L13). <!-- verify-after: WP-08 -->
- IQ router: one slow client blocked forwarding for everyone (L14).
  <!-- verify-after: WP-09 -->
- heimdall-ctl: mutating verbs ignored the reply, socket errors printed a
  traceback, `events --tail` did not decode multipart frames, and long
  payloads were truncated (L15).

### Deprecated

- **ZMQ `r` reconfiguration** on `:1130`. It still works; `rtl_daq` logs
  `ZMQ 'r' reconfiguration is deprecated` once per process.
- **`[pre_processing] fft_workers` and `[calibration] xcorr_workers`** are not
  read by any process. `ini_checker.py` warns
  `[pre_processing] fft_workers is not read by any process; use [offload] fft_workers`.
  Use `[offload] fft_workers` and `[offload] xcorr_workers`.
- **`config_files/performance/*.conf`** are documentation only; the new
  `minimal.ini`, `balanced.ini` and `maximum.ini` snippets replace them.
  <!-- verify-after: WP-14 -->

### Build, correctness and tooling overhaul

Earlier, untagged work in this release: a correctness, performance, tooling
and test overhaul across the C core, the Python control plane, the build
system and the docs (about 84 files over three rounds). Wire protocols, the
IQ header size and default runtime behaviour with all optional sections
disabled were unchanged by it.

#### Breaking / notable

- **aarch64/arm64 default FIR engine is now KFR.** The historic aarch64 Ne10
  build branch never linked (`libNE10.a` is armv7-only); `ENGINE=auto` now
  resolves aarch64 to KFR. Ne10 remains the default on 32-bit ARM only.
- **`daq_db` schema generation 2.** Keys are now big-endian (correct BTree
  ordering), records are `=`-packed version-2 formats with derived index
  offsets (the old extractors read wrong offsets). A generation-1 database
  directory is **auto-discarded and recreated** on the first read-write open;
  read-only opens raise on mismatch.
- **x86_64 decimation now actually decimates.** The KFR FIR engine previously
  heap-overflowed with `decimation_ratio > 1` and emitted full-rate data;
  downstream consumers on x86 now receive correctly decimated output.
- **Start scripts enforce config validation.** `daq_start_sm.sh` and
  `daq_synthetic_start.sh` abort on a nonzero `ini_checker.py` exit code.
  All four C binaries now zero-initialise their configuration (previously:
  uninitialised values). `rtl_daq` and `rebuffer.out` fail fast when the
  structural sizing keys are missing or invalid: `num_ch` and
  `daq_buffer_size`, and in `rebuffer.out` also `cpi_size`,
  `decimation_ratio` and `corr_size`. Other keys such as `center_freq`,
  `sample_rate` and `gain` are not validated by the binaries and default to
  0; the startup `ini_checker.py` gate validates them. Valid existing configs
  are unaffected.
- **Per-instance log layout.** Federation instance N>0 now logs to
  `_logs/instN/*.log`; instance 0 paths are unchanged. PID files live in
  `_logs/inst<N>/pids/` (new: `rtl_daq.pid`, `synthesizer.pid`).
- **net transport pairs must be upgraded together** (driver is compile-time
  optional and off by default): frame length now reflects the populated frame
  and a zero-length frame means terminate on both sides.

#### Added

- **Query replies with payloads**: the port-5001 query verbs `RFQ`, `OQRY`,
  `SCHQ` now reply `FNSD` + NUL-padded UTF-8 JSON (link budget, orientation
  state, schedule state) and are answered in any pipeline state.
- **Buffer-overrun telemetry**: USB ring-buffer overruns are detected in
  `rtl_daq` and stamped as a cumulative counter into new IQ header v8
  reserved slot 102 (`IQH_RSV_BUFFER_OVERRUN_CNT`, mirrored in
  `iq_header.py`). Header size/version unchanged.
- **Generic FIR engine** (`offload_cpu_generic.c`): dependency-free plain-C
  FIR/convert engine. `make ENGINE=generic` builds the whole core without
  KFR/Ne10, and `[offload] fir_engine = generic` selects it at runtime on
  kfr/ne10 builds.
- **Persistence wiring**: `delay_sync` now records freq-scan results,
  per-frame channel powers, and cal quality into the DB; the scheduler
  persists/resumes its position across restarts (`[schedule]` + `[database]`);
  the port-5002 status server proxies DB reads via new `DB_STATS`,
  `SCAN_SUMMARY`, `CAL_HISTORY` commands; `STATUS` replies gained
  `instance_id`, `recent_drops`, `drop_window_sec`.
- **heimdall-ctl**: new subcommands `lna-gain`, `bias-tee`, `bearing`, `park`,
  `scan start|stop`, `orientation`, `rf-budget`; installable as a pip console
  script (`pip install .`; only `util/heimdall_ctl` is packaged).
- **New optional INI keys** (absent = old behavior): `[daq] listen_address`
  (bind address for the IQ/control/status/event servers), `[monitoring]
  drop_window_sec` (windowed drop-health, default 60 s), `[schedule]
  dwell_time_sec` (per-entry dwell in seconds), `[offload]
  fir_engine = generic`.
- **Build system**: `ENGINE=auto|kfr|ne10|generic`, `BUILD=release|debug|asan`,
  and `MARCH` (incl. `portable`) Makefile variables; header dependency
  tracking; parallel-safe builds; `distclean`; `-rpath $ORIGIN` so the
  copied-in `libkfr_capi.so` works without a system install.
- **CI**: `.github/workflows/ci.yml` with a C build matrix (ubuntu x86_64 +
  arm64, generic + kfr lanes via the locally-reproducible `ci/build.sh`), an
  asan compile job, and the hardware-free Python suite on Ubuntu and macOS.
  The FPGA gateware job is deliberately omitted until lint/verify pass.
- **Packaging**: systemd units (`heimdall-daq.service`, per-instance
  `heimdall-daq@.service`) preserving the script-managed RT priorities, and a
  corrected logrotate policy covering the per-instance log layout.
- **Test suites**: `unit_test.sh --ci` (canonical hardware-free suite with
  per-module summary and a real exit code; runs on Linux and macOS) and
  `--sudo` (pipeline lane). New suites: C-vs-Python IQ-header ABI
  cross-check, real shared-memory/FIFO loopback, ZMQ message round-trips,
  port-5001 framing/reply contract, delay-sync DSP on synthetic data, FIR
  designer and ini_checker contracts, heimdall-ctl.
- **Docs**: restructured `README.md`, corrected `CLAUDE.md`, new
  `CONTRIBUTING.md`, this changelog, and the `Documentation/` set.

#### Fixed

- **C core correctness**: overdrive scan read the wrong ring slot (flags were
  always one frame stale); condition-variable use on a never-locked mutex and
  unsynchronized 64-bit buffer indices (UB/race) in `rtl_daq`; heap
  corruption in `rebuffer`'s shutdown free loop; wrong `shm_open`/`mmap`
  error sentinels; short USB transfers silently republishing stale bytes (now
  zero-filled + warned); out-of-bounds writes in both TCP command receive
  paths; NULL-deref crash in `iq_server` on pipeline TERMINATE (clean
  shutdown instead of a segfault); unbounded FIR-coefficient file reads;
  malformed-INI lines were silently ignored (now warned; handlers follow the
  inih convention).
- **KFR engine**: rewritten with per-channel/per-component filter plans
  (previously one plan shared across all channels and I/Q, which leaked
  state) and correct output sizing; reset is an exact zero-flush.
- **Python control plane**: `delay_sync` FSM bugs (stale loop variable in the
  IQ-tolerance check, correlation-peak over-indexing, divide-by-zero);
  ZMQ operations gained timeouts + socket rebuild so a hung `rtl_daq` no
  longer wedges calibration; control frames are received exactly (split TCP
  frames no longer desync the 5001 server); hardware mutations only execute
  in the safe FSM state while queries answer immediately; SIGTERM handlers
  and atomic control-file writes (no more torn bearing reads); federation
  coordinator/scheduler sent malformed text commands (now real 128-byte
  frames) and the IQ router now speaks the `IQDownload` protocol (it hung
  after the first frame); federation coordinator election actually works;
  status health uses windowed drop deltas (one historical drop no longer
  makes `ok` unreachable forever); `daq_db.rotate()` deleted nothing on real
  BerkeleyDB (missing write cursor); PCA9685 pan-tilt prescaler was never
  programmed (servo pulses were out of range); scan-and-peak averages dwell
  frames instead of trusting a single loudest frame; `heimdall-ctl recal`
  sent a `RECL` verb that the server of that time did not implement (it now
  sends `INIT`, or the opt-in `RECL` added later in this release behind
  `[calibration] en_wire_recal` when the server supports it), and
  `heimdall-ctl` read status keys the server never published;
  `auto_config` no longer destroys config comments.
- **Startup/stop scripts**: port-check no longer spins forever without
  `lsof` (falls back to `ss`); stale process names purged from `daq_stop.sh`
  (PID-file-first stop, `--legacy` preserved); `install.sh` no longer aborts
  on aarch64 and actually installs the FIR library it configures.

#### Changed / performance

- `rtl_daq` to `rebuffer` hop: single coalesced `writev` + enlarged pipe;
  `memchr`-based overdrive scan; per-transfer debug logging compiled out of
  release builds; `mlockall` across all four C processes (best-effort).
- `iq_server`: persistent listener (clients can reconnect between sessions
  without connection-refused), `TCP_NODELAY` + single-syscall `sendmsg`
  frame output.
- `offload_cpu_neon.c` now has an AArch64 NEON-intrinsic u8 to f32 convert
  path that keeps the exact divide, so it matches the scalar expression bit
  for bit. It is dormant: the NEON engine builds only in the `ENGINE=ne10`
  lane (32-bit ARM, where the scalar loop is used), and aarch64 builds use
  the KFR engine.
- `delay_sync` hot path: Hermitian `eigh` + `einsum` for IQ calibration,
  batched FFTs, precompiled header codec, and a header-only (1024-byte)
  delay_sync to hw_controller ring instead of two full-size frame buffers.
  At the 5-channel defaults this drops about 84 MB of reserved shared memory
  (2 x ~41.9 MB). Only the header was ever written to those buffers, so the
  drop in memory actually in use is small.
- Thread-safe, rate-limited logging in the C core.

#### Removed

- `test_squelch.py` and `gen_burst.py` (dead: `squelch.out` has no source, no
  Makefile target, no config section; `delay_sync` has no squelch support).
- Dead batched-control FIFO API in `sh_mem_util`.
- Checked-in build artifact `_daq_core/serial_test` (ELF) and `__pycache__`
  directories; root `.gitignore` added.
- `Firmware/BringUpInstructions.txt` content (stale Kerberos-era steps)
  replaced with a pointer to the current docs.
