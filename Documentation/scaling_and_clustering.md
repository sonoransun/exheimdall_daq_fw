# Scaling and Clustering

This guide is the single reference for running more than one DAQ instance, on one host or on several hosts. It covers the limits, what is namespaced and what is shared, the supported topologies, the coordination features, the failure modes, and worked examples with the real tools.

Read [architecture.md](architecture.md) first if you do not know the pipeline. Wire formats are in [protocols.md](protocols.md) and every key is in [configuration.md](configuration.md). Related guides: [capacity_planning.md](capacity_planning.md) (sizing worksheet), [deployment_recipes.md](deployment_recipes.md) (step-by-step recipes), [networking_and_time_sync.md](networking_and_time_sync.md) (ports, firewalls, NTP/PTP), [operations_runbook.md](operations_runbook.md) (start, stop, symptoms) and [theory_coherent_acquisition.md](theory_coherent_acquisition.md) (why islands are not coherent).

## Contents

1. [Status of the features in this guide](#1-status-of-the-features-in-this-guide)
2. [The scaling unit: one instance is one coherent island](#2-the-scaling-unit-one-instance-is-one-coherent-island)
3. [Vertical scaling inside one instance](#3-vertical-scaling-inside-one-instance)
4. [Several instances on one host](#4-several-instances-on-one-host)
5. [Multi-host federation](#5-multi-host-federation)
6. [Coordination features](#6-coordination-features)
7. [Data aggregation: the IQ router](#7-data-aggregation-the-iq-router)
8. [Failure modes and recovery](#8-failure-modes-and-recovery)
9. [Worked examples](#9-worked-examples)
10. [Checklists](#10-checklists)

Notation used throughout:

| Symbol | Meaning |
|---|---|
| `N` | `[federation] instance_id` of an instance |
| `<P>` | resource prefix: empty for N = 0, `inst<N>_` for N > 0 |
| `<off>` | port offset `N * port_stride` (default stride 100) |
| `M` | `[hw] num_ch` |
| `fs` | `[daq] sample_rate` |
| `Ncpi` | `[pre_processing] cpi_size` |
| `R` | `[pre_processing] decimation_ratio` |
| `Nc` | `[calibration] corr_size` |
| `T_fp` | frame period `Ncpi * R / fs`; 0.437 s at the shipped defaults |
| node key | `host:instance_id` as written in a membership entry, host lower-cased |

## 1. Status of the features in this guide

Everything here is opt-in. With every new key absent and no new flag passed, the chain behaves as before: same process names, FIFO names, shm names, ports and wire bytes. The exceptions are listed in the disabled-state ledger in [upgrade_and_rollback.md](upgrade_and_rollback.md).

Every feature in this guide is in this tree:

| Area | Where |
|---|---|
| Serial mapping (`[hw] serial_base`, `device_serials`), duplicate-serial defence, `unit_id = auto`, ZMQ bind address, USB ring depth | `rtl_daq.c`, `device_map.c` |
| Lazy track-lock file, extended acks (`XACK`), fencing, control snapshot (`HWSQ`), `RECL` | `hw_controller.py` |
| Prefixed FIR coefficients for N > 0, `size_t` sizing, config-sized segments, multi-client `iq_server` | `fir_filter_designer.py`, `fir_decimate.c`, `iq_server.c` |
| Extended STATUS, staleness, `cluster_members` mesh peers, registrar wiring, slot 108 stamping | `delay_sync.py`, `daq_status_server.py` |
| Coordinator protocol v2, election, fencing, health state machine, scheduler | `federation_coordinator.py`, `federation_election.py`, `federation_health.py`, `federation_scheduler.py` |
| IQ router CLI, bounded queues, tagging, alignment, stats port | `federation_iq_router.py` |
| Per-instance DB directory and sidecar (`instance_subdir`, `write_instance_meta`) | `daq_db.py` |
| Config limits and port-window checks, `--federation` validation | `ini_checker.py` |
| `heimdall-ctl` `fleet`, `router`, `ports`, `instances`, `peers` | `util/heimdall_ctl/` |
| `[host]` keys in the start scripts, registry, instance lock, startup mutex, scoped stop, supervisor, federation launchers | `daq_common.sh`, `daq_start_sm.sh`, `daq_synthetic_start.sh`, `daq_stop.sh`, `daq_supervise.sh`, `federation_start.sh`, `federation_stop.sh` |
| Capacity model and pre-flight tool | `_daq_core/capacity_model.py`, `daq_preflight.py` |
| `provision_instance.py`, `cluster_cfg_gen.py`, serial programming tool `set_unit_serials.sh` | `util/` |
| Supervised, coordinator, router, host-tuning and watchdog units | `packaging/systemd/` |

The start scripts (`daq_start_sm.sh`, `daq_synthetic_start.sh`) read the `[host]` keys through `daq_common.sh`. With every `[host]` key absent they behave as before: the core formula of section 2, the original RT priorities, host tuning on every start, no registry, no lock and the global stop in the busy-port loop.

| Key | Effect |
|---|---|
| `[host] cpu_cores` | Four distinct core ids for the tiers: rtl_daq; rebuffer + decimate; delay_sync + iq_server; hw_controller. Replaces the core formula |
| `[host] delay_sync_cores` | The start scripts run delay_sync with this `taskset` list instead of the tier-2 core. Its core count also sizes `[offload] fft_workers` and `xcorr_workers` when they are `auto` |
| `[host] rt_priority_offset` | Subtracted from every RT priority (default 0; a value above 81 is ignored) |
| `[host] instance_registry`, `instance_guard`, `allow_core_sharing` | Registry, instance lock, startup mutex and guards (section 4.5) |
| `[host] host_tuning` | `always` (default): sysctl, `irq_tuning.sh`, usbfs and `drop_caches` on every `daq_start_sm.sh` start. `auto`: run `util/host_tuning.sh --apply` unless `heimdall-host-tuning.service` is active or another registered instance runs. `1`: always run `host_tuning.sh --apply`. `0`: skip (also `--no-host-tuning`) |
| `[host] preflight` | `off` (default), `warn` or `strict`: run `daq_preflight.py --plus-live` at start; with `strict` a failure stops the start with exit 4 |
| `[host] health_thread_sched_other` | Moves the federation health poller thread off SCHED_FIFO (read by `delay_sync.py`) |

## 2. The scaling unit: one instance is one coherent island

An instance is one complete pipeline: `rtl_daq.out → rebuffer.out → decimate.out → delay_sync.py → [iq_server.out | shmem]`, plus `hw_controller.py`. Its channels share one sample clock and one noise source, so they are phase-coherent with each other. Channels of two different instances are not coherent. Every scaling step in this guide adds islands; none of them makes two islands coherent.

One instance consumes these resources:

| Resource | Per instance | Source |
|---|---|---|
| Processes | 5 or 6 (`iq_server.out` only with `out_data_iface_type = eth`) | `daq_start_sm.sh` |
| Pinned cores | 4: `CORE_k = (N*4 + k) % nproc`, k = 0..3 | `daq_start_sm.sh` |
| RT tiers (SCHED_FIFO) | rtl_daq 99 (core 0), rebuffer 94 (core 1), decimate 92 (core 1), delay_sync 90 (core 2), iq_server 88 (core 2), hw_controller 82 (core 3) | `daq_start_sm.sh` |
| Shared-memory rings | `decimator_in`, `decimator_out`, `delay_sync_iq`, `delay_sync_hwc`, each with `_A` and `_B` segments, prefixed `<P>` | `rebuffer.c`, `fir_decimate.c`, `delay_sync.py` |
| FIFOs | `_data_control/<P>{fw,bw}_{decimator_in,decimator_out,delay_sync_iq,delay_sync_hwc}` | `daq_start_sm.sh` |
| TCP/ZMQ ports | `1130`, `5000`, `5001`, `5002`, `5003`, each `+ <off>` | `compute_port()` |
| USB receivers | `M` RTL-SDR dongles, opened by EEPROM serial | `rtl_daq.c` |
| Logs | N = 0: `_logs/*.log`; N > 0: `_logs/inst<N>/*.log` | `daq_start_sm.sh` |
| PID files | `_logs/inst<N>/pids/*.pid` (also for N = 0) | `daq_start_sm.sh` |

The shipped defaults are `M = 5`, `fs = 2.4 MS/s`, `daq_buffer_size = 2^18`, `Ncpi = 2^20`, `R = 1`, `Nc = 2^16`.

## 3. Vertical scaling inside one instance

Scale one instance first. A bigger island is coherent; two islands are not.

### 3.1 Limits

| Limit | Value | Enforced by |
|---|---|---|
| Channels per instance | 1..31 | `ini_checker.py` error; `DEVICE_MAP_MAX_CH 31` in `device_map.h`. The 128-byte `:1130` message holds 31 per-channel values |
| `cpi_size` | ≤ 2^23 | `ini_checker.py` error (worst-case segment size) |
| `corr_size` | ≤ 2^23 | `ini_checker.py` error |
| Frame period | warning above 8 s | `ini_checker.py` warning (control timeouts) |

Example with `num_ch = 32`:

```
$ python3 ini_checker.py n32.ini no_hw
ERROR:root:Number of channels must be at most 31: the 128-byte :1130 control message holds 31 per-channel values. Currently it is: '32'
ERROR:root:Amplification ext_lna_gains_db must have one value per channel (32). Currently: 5
ERROR:root:Amplification ext_lna_nf_db must have one value per channel (32). Currently: 5
ERROR:root:Amplification ext_lna_p1db_dbm must have one value per channel (32). Currently: 5
$ echo $?
1
```

The last three errors come from the shipped 5-entry `[amplification]` lists. Changing `num_ch` also requires every per-channel list (`[amplification]` and others) to have `num_ch` entries.

### 3.2 The capacity levers

| Lever | Effect | Cost |
|---|---|---|
| `decimation_ratio` (R) | Output rate and frame rate drop by R. Egress per `:5000` client is `M * fs * 8 / R` bytes/s. `T_fp` grows by R | `decimate.out` still filters at the full input rate |
| `cpi_size` (Ncpi) | Longer integration per frame, fewer frames per second | Larger shm segments; delay_sync copies `Ncpi * M * 8` bytes in and out per frame; slower reaction of control and health |
| `corr_size` (Nc) | Better delay estimate during calibration | FFT cost in delay_sync during calibration; segments are sized for `max(Ncpi, Nc)` |
| `[offload] fft_workers`, `xcorr_workers` (default 4) | FFT parallelism in delay_sync | With `auto`, the worker count follows the core count of `[host] delay_sync_cores`, and the start scripts pin delay_sync to that core list |
| `[pre_processing] size_segments_from_config = 1` | `decimator_out` and `iq_server`'s map are sized from the config instead of the 2^23 worst case | None; off by default |
| `[data_interface] iq_server_max_clients` (default 1) | More than one `:5000` client per instance | Egress multiplies by the client count |
| `[daq] usb_ring_buffers` / `usb_async_transfers` (8 / 12) | Deeper USB ring against overruns | Locked memory `M * usb_ring_buffers * 2 * daq_buffer_size` bytes |

### 3.3 Numbers at the shipped defaults

| Quantity | Formula | Default |
|---|---|---|
| USB ingress | `M * fs * 2` B/s | 24.0 MB/s |
| Frame period | `Ncpi * R / fs` | 0.437 s |
| `decimator_in` object (A+B) | `2 * (1024 + max(Ncpi*R, Nc) * M * 2)` | 20 MiB |
| `decimator_out` object (A+B) | `2 * (1024 + 2^23 * M * 8)`; with `size_segments_from_config = 1`: `2 * (1024 + max(Ncpi, Nc) * M * 8)` | 640 MiB; 80 MiB |
| `delay_sync_iq` object (A+B) | `2 * (1024 + max(Ncpi, Nc) * M * 8)` | 80 MiB |
| cf32 egress per `:5000` client | `M * fs * 8 / R` B/s | 96 MB/s (768 Mbit/s) |

[capacity_planning.md](capacity_planning.md) has the full worksheet. `python3 daq_preflight.py --config daq_chain_config.ini` (in `Firmware/`) checks these numbers against the host it runs on; `--add-instances N` adds planned copies and `--plus-live` counts the live registered instances. Exit codes: 0 ok, 1 warnings, 2 failures, 3 usage error. `[host] preflight = warn|strict` runs it at every start.

## 4. Several instances on one host

### 4.1 Default behaviour and the opt-in fixes

| Item | Default (no `[host]` keys) | Opt-in |
|---|---|---|
| Serial numbers | Without serial keys, channel i opens serial `1000 + i` in every instance, so a second instance fails to open its dongles | `[hw] serial_base` (channel i uses `serial_base + i`) or `[hw] device_serials` (explicit comma list, one per channel) |
| Working copy | Every stage reads the cwd-relative `daq_chain_config.ini` and `_data_control/`. Each instance needs its own working directory (section 4.4) | `util/provision_instance.py` creates it |
| Track-lock file | Not required at start. hw_controller opens `_data_control/<P>iq_track_lock` only when `require_track_lock_intervention = 1`; a missing file then means "not approved" | - |
| CPU cores | `CORE_k = (N*4 + k) % nproc`. When `(N+1)*4 > nproc` the start script only prints a `WARN:` line and the instances share cores | `[host] cpu_cores`; with `instance_registry = 1`, `allow_core_sharing = refuse` exits 3 on overlap with a live instance |
| Stopping | The busy-port loop of the start script runs the no-argument `./daq_stop.sh`, which kills every DAQ process on the host by name. A busy port on one instance's start therefore stops all instances | `instance_guard = refuse` (needs `instance_registry = 1`) or `HEIMDALL_SCOPED_STOP=1` (exported by the supervisor): the loop runs `./daq_stop.sh N` |
| Port check | Only listening sockets count (`ss -ltnH`, else `lsof -sTCP:LISTEN`); outbound client sockets on the same port number are ignored | - |
| Host tuning | Every start runs the host-global `sysctl`, `irq_tuning.sh`, `usbfs_memory_mb = 0` and `drop_caches` steps again | `[host] host_tuning = auto` or `0`, plus `heimdall-host-tuning.service` |

The core-sharing rule matters most on 4-core boards. On a Raspberry Pi 4 the formula maps instance 1 to cores 0-3, which are instance 0's cores. The start script warns and continues. Plan one hardware instance per 4 cores.

### 4.2 Prefixed and shared resources

| Resource | Namespace | Separated by |
|---|---|---|
| POSIX shm `/dev/shm/<P>decimator_in_A` etc. | Host-global | `<P>` prefix |
| FIFOs `_data_control/<P>fw_*`, `<P>bw_*` | cwd-relative | prefix, and the working copy |
| Control files `<P>orientation_state`, `<P>bias_tee_state`, `<P>iq_track_lock`, `<P>fence_term`, `<P>hwc_state`, `<P>recal_request` | cwd-relative | prefix, and the working copy |
| `_data_control/<P>fir_coeffs.txt` | cwd-relative | prefixed for N > 0 by `fir_filter_designer.py`, read by `decimate.out` |
| `_data_control/<P>hw_caps.json` | cwd-relative | prefixed for N > 0 by the start scripts, and the working copy |
| `daq_chain_config.ini` | cwd-relative | the working copy only |
| `[database] db_dir` (default `_db`) | cwd-relative | the working copy, or `[database] instance_subdir = 1` (N > 0 uses `<db_dir>/inst<N>`) |
| Logs and PID files | cwd-relative | `_logs/inst<N>/` |
| Ports | Host-global | `<off>` |
| USB dongles | Host-global | `serial_base` / `device_serials` |
| CPU cores, RT priorities | Host-global | core formula, or `[host] cpu_cores` / `delay_sync_cores` / `rt_priority_offset` |
| pigpio antenna-switch GPIO | Host-global | `[hw] antenna_switch_gpio` (`none` or `A,B`; absent = GPIO 23/24 as before) |
| Host tuning (sysctl, IRQ, usbfs, caches) | Host-global | run by every start by default; once per boot with `[host] host_tuning = auto` or `0` and `heimdall-host-tuning.service` |

`heimdall-ctl ports` prints an instance's port window from the config. Real output for instance 1 with the shipped config:

```
$ heimdall-ctl --instance 1 ports
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

### 4.3 Port table and the safe instance-id range

| Service | N = 0 | N = 1 | N = 2 | N = 3 |
|---|---|---|---|---|
| rtl_daq ZMQ (`:1130`) | 1130 | 1230 | 1330 | 1430 |
| IQ stream (`:5000`) | 5000 | 5100 | 5200 | 5300 |
| Control (`:5001`) | 5001 | 5101 | 5201 | 5301 |
| Status (`:5002`, opt-in) | 5002 | 5102 | 5202 | 5302 |
| Events PUB (`:5003`, opt-in) | 5003 | 5103 | 5203 | 5303 |

The cluster services use fixed ports: coordinator 6000, router 7000, router stats 7001. With stride 100, instance 10 lands on 6000-6003 and instance 20 on 7000-7003. Use instance ids 0-9 per host with the default stride, or move the service ports.

`ini_checker.py` reports the collision only when this INI places a coordinator on this host (`coordinator_host` or a `coordinator_candidates` entry is local) or sets a `router_*` key. It does not know about a coordinator or router started by hand with other settings. With the shipped config (`coordinator_host` empty), instance 10 passes with no output and exit 0. Keep instance ids at 0-9 with stride 100 regardless. When the check runs, it is a warning for an existing config and an error in new-key mode (any of `cluster_members`, `coordinator_candidates`, `register_with_coordinator = 1`, a `router_*` key, `[host] instance_registry = 1` or `instance_guard = refuse`):

```
$ python3 ini_checker.py i10.ini no_hw          # instance_id = 10, coordinator_host local
WARNING: port 6000 of instance 10 collides with the coordinator port; with stride 100 use instance ids 0-9
$ echo $?
0
$ python3 ini_checker.py i10_newkey.ini no_hw   # same, plus [host] instance_registry = 1
ERROR:root:port 6000 of instance 10 collides with the coordinator port; with stride 100 use instance ids 0-9
$ echo $?
1
```

A stride below 4 makes windows overlap:

```
$ python3 ini_checker.py s2.ini no_hw           # instance_id = 1, port_stride = 2
WARNING: [federation] port_stride = 2 is below 4 on instance 1: its ports overlap another instance's 5000-5003 window
```

### 4.4 The instance-directory model

Use one built install root per host and one symlinked working copy per instance N > 0. Instance 0 runs in `<root>/Firmware` unchanged.

```
<root>/                         # install root, e.g. /opt/heimdall
<root>/Firmware/                # instance 0
<root>/instances/<N>/
    util          -> <root>/util
    config_files  -> <root>/config_files
    Firmware/
        every other entry of <root>/Firmware                  -> symlinks
            (_daq_core, _testing, daq_common.sh, daq_start_sm.sh, daq_stop.sh,
             daq_supervise.sh, daq_preflight.py, ini_checker.py, ...)
        daq_chain_config.ini     (real file, instance_id = N)
        _data_control/  _logs/  _db/   (real, private directories)
```

The private `daq_chain_config.ini`, `_data_control/`, `_logs/` and `_db/` isolate every cwd-relative file. The code is shared. Python caches (including the numba cache) are shared through the `_daq_core` symlink. Concurrent first compiles may log a harmless numba cache warning.

`instance_id` is read only from the instance's own INI. No environment variable selects a config file or an instance id.

**Rebuild on every update.** Run `make` in `Firmware/_daq_core` after every update. An old `decimate.out` reads the unprefixed `fir_coeffs.txt`, so an instance N > 0 with stale binaries fails at start instead of filtering with stale taps.

`util/provision_instance.py` (below) creates this layout. To create an instance directory by hand instead (run as the user that owns `/opt/heimdall`), link every `Firmware` entry except the private ones. The start scripts need `daq_common.sh` next to them and stop with `daq_common.sh not found` without it:

```sh
R=/opt/heimdall; N=1
mkdir -p $R/instances/$N/Firmware && cd $R/instances/$N
ln -s $R/util util
ln -s $R/config_files config_files
cd Firmware
for f in $R/Firmware/*; do
    case "${f##*/}" in
        _data_control|_logs|_db|__pycache__|daq_chain_config.ini*) ;;
        *) ln -s "$f" "${f##*/}" ;;
    esac
done
mkdir _data_control _logs _db
cp $R/Firmware/daq_chain_config.ini .
# edit daq_chain_config.ini: [federation] instance_id = 1, [hw] serial_base = 1100,
#                            [daq] ctr_channel_serial_no = 1100
python3 ini_checker.py daq_chain_config.ini no_hw
```

`[daq] ctr_channel_serial_no` must be one of the instance's resolved serials, normally `serial_base`. The shipped value is 1000. Left at 1000 on instance 1, `ini_checker.py` fails and the start script refuses to start:

```
ERROR:root:[daq] ctr_channel_serial_no 1000 is not one of the configured serials 1100, 1101, 1102, 1103, 1104 (...)
```

**`util/provision_instance.py`** creates the same layout and writes the instance INI. The default run is a dry run that prints the directory plan, the INI diff and the port table; `--write` executes.

```
sudo python3 util/provision_instance.py --instance N [--root /opt/heimdall] [--dest DIR]
    [--template PATH | --from-config PATH] [--mode link|copy]
    [--serial-base B | --device-serials LIST] [--unit-id K|auto]
    [--cores c0,c1,c2,c3] [--delay-sync-cores LIST] [--rt-offset K] [--stride 100]
    [--antenna-switch-gpio none|A,B]
    [--cluster-members LIST] [--node-id NAME] [--cluster-id ID] [--coordinator H:P[,H:P]]
    [--advertise-host H] [--register] [--profile minimal|balanced|maximum] [--systemd]
    [--env-file PATH] [--python-bin DIR] [--check-attached]
    [--in-place] [--refresh] [--from-root OLD] [--force] [--write]
```

With `--template` (the default template is `<root>/Firmware/daq_chain_config.ini`), it writes these defaults for instance N: `serial_base = 1000 + 100*N`, `unit_id = auto`, `cpu_cores = 4N,4N+1,4N+2,4N+3`, `[daq] ctl_max_connections = 4`, `[host] instance_registry = 1`, `instance_guard = refuse`, `allow_core_sharing = refuse`, `host_tuning = auto` (`0` with `--systemd`), `preflight = warn`, `health_thread_sched_other = 1`, and `[monitoring] en_monitoring = 1`, `en_status_server = 1`, `status_extended = 1`, `status_update_interval = 10`, `event_tag_identity = 1`. `--from-config` uses an INI verbatim. Instance 0 needs `--in-place`. On a multi-instance host, provision instance 0 with `--in-place` too, so it carries the registry and guard keys. Exit codes: 0 ok, 1 validation failed, 2 usage, 3 destination exists, 4 instance live.

### 4.5 Start and stop order

With the default `[host]` keys:

1. Build once in `<root>/Firmware/_daq_core` (`make`).
2. Start instance 0 from `<root>/Firmware`: `sudo ./daq_start_sm.sh`.
3. Wait until its control port listens, then start instance N from `<root>/instances/N/Firmware`: `sudo ./daq_start_sm.sh`. Wait for instance N the same way before you start the next one (port `5001 + N*100`):

   ```sh
   until ss -ltn | grep -q ':5001 '; do sleep 1; done    # instance 0; use :5101 for instance 1
   ```
4. Stop one instance with its id: `sudo ./daq_stop.sh N` (PID files of this copy only).
5. Never run the no-argument `./daq_stop.sh` on a multi-instance host unless you mean to stop everything. It is the documented host-wide kill.

Start instances one after the other. With the default keys the start scripts have no startup mutex, and a start that finds its ports busy runs the global stop.

**Registry, lock, guard and scoped stop.** The first five rows activate only with `[host] instance_registry = 1`; the environment variable and the stop forms work in every configuration:

| Feature | Behaviour |
|---|---|
| Registry | `$HEIMDALL_RUN_DIR` (default `/run/heimdall`, fallback `/tmp/heimdall-run-<uid>`) holds `inst<N>.json` and `inst<N>.env` per live instance. `heimdall-ctl instances` lists them. It is host-wide only when every instance sets the key and all starts run as the same uid |
| Instance lock | `inst<N>.lock`, held through fd 9 by the stages. A second start of the same N finds it held |
| Startup mutex | `inst<N>.start`, fd 8, held by the start script only. Serialises concurrent starts of the same N |
| `[host] instance_guard` | `legacy` (default): conflicts print `WARN:` and the global stop of the busy-port loop resolves them. `refuse` (requires `instance_registry = 1`): a duplicate start from another copy exits 3, a restart from the same copy stops it with `./daq_stop.sh N` first, and busy-port rounds use the scoped `./daq_stop.sh N`. `--takeover` stops the other copy first |
| `[host] allow_core_sharing` | `warn` (default), `refuse` (exit 3 when cores overlap a live instance), `allow` |
| `HEIMDALL_SCOPED_STOP=1` | Exported by the supervisor. The busy-port loop then runs `./daq_stop.sh N` instead of the global stop |
| `daq_stop.sh N --clean` | Stops instance N, then removes its FIFOs, shm objects and relay files (`orientation_state`, `bias_tee_state`, `hwc_state`, `recal_request`). Keeps `iq_track_lock`, `fir_coeffs` and `fence_term`. Refused (exit 3) when a stage is still alive after the stop |
| `daq_stop.sh N --reset-fence` | Removes `<P>fence_term` of a stopped instance. Refused (exit 3, nothing stopped) while a stage of N runs |
| `daq_stop.sh --this-copy` | Signals only processes whose cwd is this copy |
| `daq_stop.sh --all` | Global stop, force-kill of the python stages and removal of every registry entry |

Start-script exit codes: 0 started, 1 config/designer failure or ports still busy, 3 guard refusal, 4 strict pre-flight failure.

Without registry files `heimdall-ctl instances` prints:

```
$ heimdall-ctl instances
no registry entries in /run/heimdall, /tmp/heimdall-run-<uid> (only instances started with [host] instance_registry = 1 are listed)
```

The tool searches `$HEIMDALL_RUN_DIR` (default `/run/heimdall`) and the per-uid fallback `/tmp/heimdall-run-<uid>`, so `<uid>` is `0` when run as root.

**Supervisor.** `./daq_supervise.sh [--instance N] [--synthetic] [--interval 2] [--healthcheck auto|on|off] [--grace 60] [--stall-sec auto|SECONDS] [--allow-legacy-guard] [-- START_SCRIPT_ARGS...]` starts this copy's chain with `HEIMDALL_SCOPED_STOP=1`, watches every stage's PID file (and, with the health check, the status server), and on the first failure stops the instance with `./daq_stop.sh N`. It refuses (exit 2) to start an instance that runs with `instance_guard = legacy` while another instance is live, unless `--allow-legacy-guard` is given.

### 4.6 systemd units

`packaging/systemd/heimdall-daq@.service` starts `/opt/heimdall/instances/%i/Firmware/daq_start_sm.sh` and stops with `daq_stop.sh %i`. `%i` must equal the `instance_id` in that copy's INI. The instance-0 unit `heimdall-daq.service` starts `/opt/heimdall/Firmware/daq_start_sm.sh` and stops with `daq_stop.sh 0`, so it stops only instance 0.

`heimdall-daq-supervised@.service` (and `heimdall-daq-supervised.service` for instance 0) runs `daq_supervise.sh --instance %i --healthcheck auto`, restarts on failure and runs `daq_stop.sh %i --clean` after stop. The oneshot and supervised units of one instance conflict; pick one per instance. The supervisor exits 10-15 when a stage dies (10 rtl_daq or synthesizer, 11 rebuffer, 12 decimate, 13 delay_sync, 14 hw_controller, 15 iq_server) and 16 on a failed health check. Exit codes 1-4 are not restarted. Further units: `heimdall-coordinator.service`, `heimdall-iq-router.service` (both unprivileged, user `heimdall`), `heimdall-host-tuning.service`, and an optional `heimdall-daq-watchdog.timer`. See [packaging/systemd/README.md](../packaging/systemd/README.md).

## 5. Multi-host federation

Federation is off by default (`[federation] en_federation = 0`). With it off, no health thread runs and STATUS has no `federation` block.

### 5.1 Identity rules

| Identity | Source | Must be unique | Used for |
|---|---|---|---|
| `instance_id` | `[federation] instance_id` | per host | prefix `<P>`, port offset |
| `unit_id` | `[hw] unit_id`, or `auto` (= instance_id) | operator's choice | header byte 24 |
| node key | membership entry `host:instance_id`, host lower-cased, never DNS-resolved for keying | cluster-wide | key of every coordinator and router table |
| `node_id` | `[federation] node_id`, default `<short lower-case hostname>:<N>`, max 48 chars, grammar `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,47}$` | advisory | label in STATUS, events, REGISTER, DB sidecar, header slot 108 |
| `cluster_id` | `[federation] cluster_id`, default `default`, max 31 chars | cluster-wide label | coordinators and members reject a foreign cluster (`cluster_mismatch`) |

Two hosts may both run instance 0. Their node keys (`hosta:0`, `hostb:0`) differ. Loopback hosts (`localhost`, `127.0.0.0/8`, `::1`) are rejected wherever node keys must be cluster-unique:

```
$ python3 ini_checker.py federation.ini --federation      # cluster_members = hosta:0,localhost:1
ERROR:root:[federation] cluster_members: member 'localhost:1' uses a loopback host; use the host's advertised name
WARNING: auth_token and allow_cidrs are empty and the listen address 0.0.0.0 is not loopback: the coordinator accepts mutating commands from any host
```

Fleet images often share a host name (`raspberrypi`, `krakensdr`, `ubuntu`, ...). Give every host a unique name, or set `advertise_host` and `node_id`.

### 5.2 Membership

| Key | Meaning |
|---|---|
| `cluster_members` | Full membership including this instance: `host:instance_id[@node_id],...`. No duplicates, no loopback hosts. Use the same list on every instance and on the coordinator |
| `peer_list` (legacy) | Peers this instance polls. An entry `host:N` with N ≥ 1024 is read as a status port (legacy form). Problems are warnings unless a new key is present |

Mesh peers of an instance: `peer_list` when non-empty, else `cluster_members` minus the self entry, else none. With `cluster_members`, peer polling is symmetric by construction: every instance polls every other one. With hand-written `peer_list` entries you must keep the lists symmetric yourself. When both keys are set, `peer_list` wins and `ini_checker.py` warns `peer_list overrides cluster_members for health polling`.

**Status server prerequisite.** Health polling reads each peer's `:5002 STATUS`. Every member needs `[monitoring] en_monitoring = 1` and `en_status_server = 1`. Without them every peer reads as down.

**Control-port prerequisite.** `hw_controller` serves one control connection at a time by default. A resident DoA client then blocks every coordinator command. Set `[daq] ctl_max_connections = 4` on every member. `ini_checker.py` warns when federation is on and the key is 1: `a resident DoA client blocks every coordinator and heimdall-ctl command on this member (set [daq] ctl_max_connections = 4)`.

**Data-plane prerequisite.** The router reads each instance's `:5000`. That needs `[data_interface] out_data_iface_type = eth`; the shipped default is `shmem`, which starts no `iq_server`.

### 5.3 What runs where

| Process | Count | Placement | Started by |
|---|---|---|---|
| DAQ instance | one per receiver set | the host with the dongles | `daq_start_sm.sh` (root) |
| Coordinator | one static, or 3 or more election candidates | any host that can reach every member's `:5001` and `:5002` | by hand, `federation_start.sh coordinator` or `heimdall-coordinator.service` |
| IQ router | one per host, plus optionally one central router | per-host: next to its instances; central: next to the consumers | by hand, `federation_start.sh router` or `heimdall-iq-router.service` |

The coordinator and router need no root and no RT priority.

Network flows to allow:

| Port | From | To | Purpose |
|---|---|---|---|
| 6000/tcp | every coordinator candidate host | every other candidate host | election (PREVOTE/LEASE) and state replication |
| 6000/tcp | operator hosts | every candidate host | `heimdall-ctl fleet` |
| 5001/tcp, 5002/tcp | every candidate host | every member | commands and health polls |
| 5002/tcp | every member | every other member | mesh health polling |
| 5000/tcp | the router host | every member | IQ frames |
| 7000/tcp | consumer hosts | the router host | aggregated IQ |
| 7001/tcp | operator hosts | the router host | `heimdall-ctl router stats` |

Add `instance_id * port_stride` to the 5000-5002 ports for members with instance_id > 0.

The coordinator drives the instances, not the other way round. It polls each member's `:5002` and sends commands to each member's `:5001`. An instance contacts a coordinator only when `register_with_coordinator = 1` (section 5.4). `[federation] coordinator_host`/`coordinator_port` are read by `heimdall-ctl fleet` and by that registrar; the pipeline does not use them otherwise.

Run the coordinator by hand:

```
$ cd /opt/heimdall/Firmware
$ python3 _daq_core/federation_coordinator.py --help
usage: federation_coordinator.py [-h] [--config CONFIG] [--port PORT]
                                 [--listen-address LISTEN_ADDRESS]
                                 [--instances INSTANCES] [--self SELF_EP]
                                 [--state-dir STATE_DIR]
                                 [--auth-token AUTH_TOKEN]
                                 [--pid-file PID_FILE] [--log-file LOG_FILE]
                                 [--log-level LOG_LEVEL]
```

| Rule | Value |
|---|---|
| Precedence | CLI flag > `--config` value > built-in default |
| Exit codes | 0 normal stop, 2 bind failure, 3 bad arguments or config |
| Members | `cluster_members` if set; else `--instances`, else `peer_list`; plus registered members (REGISTER) |
| State | persisted in `state_dir` only when `--state-dir`/`state_dir` is set or `coordinator_candidates` is non-empty (default then `_data_control/federation`) |

Validate a coordinator or router config with `--federation`. It needs only a `[federation]` section:

```
$ python3 ini_checker.py federation.ini --federation      # 2 candidates, require_quorum = 1
WARNING: require_quorum = 1 with 2 candidates: with 2 candidates, losing either host blocks all mutations
WARNING: auth_token and allow_cidrs are empty and the listen address 0.0.0.0 is not loopback: the coordinator accepts mutating commands from any host
$ echo $?
0
```

**Federation launchers** (in `Firmware/`, no root, no RT priority, no CPU pinning):

```
./federation_start.sh coordinator|router|all [--config PATH] [--foreground]
                      [--state-dir DIR] [--log-dir DIR] [--pid-dir DIR]
./federation_stop.sh  coordinator|router|all [--pid-dir DIR]
```

Defaults: config `/etc/heimdall/federation.ini` if present, else `./daq_chain_config.ini`; logs in `_logs/federation/` (`$HEIMDALL_FED_LOG_DIR`); PID files in `_logs/federation/pids/` (`$HEIMDALL_FED_PID_DIR`); `--state-dir` (coordinator only) from `$HEIMDALL_FED_STATE_DIR`, not passed when empty. `--foreground` execs one service in place (used by the systemd units). Start exit codes: 0 started, 1 the service exited immediately, 2 usage error, 3 already running. `federation_stop.sh` sends SIGTERM, waits 5 s, then SIGKILL; exit 0 done, 2 usage error.

**`util/cluster_cfg_gen.py`** generates a whole cluster's configuration:

```
python3 util/cluster_cfg_gen.py --template PATH --hosts hosta:2 hostb:1 [--stride 100]
    [--serial-base 1000] [--serial-stride 100] [--cores-per-host hosta=8,hostb=4]
    [--coordinators hosta:6000,hostb:6000,hostc:6000] [--router hosta:7000] [--cluster-id ID]
    [--register] [--health-mode mesh|hub] [--auth-token-file PATH] --out DIR
```

It writes one INI per instance (`DIR/<host>/inst<N>/daq_chain_config.ini`, ids 0..k-1 per host) with `cluster_members`, never `peer_list`, plus `DIR/federation.ini`, `DIR/ports.md` and `DIR/provision.sh` (`provision_instance.py --from-config` lines per host). With `--router`, every host other than the router host also gets `DIR/<host>/federation.ini` for a per-host router chained into the central one. It validates every file and exits 1 (files kept for inspection) on a validation error, a port collision or a core plan that exceeds a host; 2 on usage errors.

### 5.4 Dynamic registration

With `[federation] register_with_coordinator = 1` (requires `en_federation = 1`), delay_sync's registrar thread sends `REGISTER` every `register_interval_sec` (default 5.0) to every coordinator endpoint in parallel: `coordinator_candidates`, else `coordinator_host:coordinator_port`. A registered member is added to the coordinator's membership. The coordinator uses the observed source address to reach it when `advertise_host` is not an IP literal.

| Incoming REGISTER for node key K | Result |
|---|---|
| K unknown | accepted |
| same nonce | accepted; address updated |
| new nonce, same address | accepted as a restart |
| new nonce, new address, holder not expired | rejected `duplicate_node_key`; listed in `key_collisions` |
| holder expired (no REGISTER for 3 × its interval) | accepted; replaces the holder |

Expired members show `state: "expired"` and are removed after `register_retain_sec` (600 s).

## 6. Coordination features

The coordinator listens on `:6000`. One request per TCP connection. Legacy verbs reply one JSON object without a trailing newline; new verbs reply JSON followed by `\n` and always carry `"ok"`. The full verb table is in [protocols.md](protocols.md).

### 6.1 Feature status

| Feature | Verbs | Behaviour |
|---|---|---|
| Fan-out control | `FREQ`, `GAIN`, `ALL <VERB>`, `NODE <key> <VERB>`, `FANOUT <k1,k2> <VERB>`, `RECAL` | Parallel; every member gets `XACK` first, then the command; per-target result with `applied` true/false/null |
| Health roll-up | `STATUS`, `HEALTH`, `NODES STATUS` | `error` when any member is down or unreachable; `degraded` when any is degraded, stale, flapping or unknown. In `HEALTH`, a member that has never answered stays `unknown` and counts as degraded, even when unreachable. `STATUS` and `NODES STATUS` report `error` for any unreachable member |
| Master schedule | `SCHEDULE SET/GET/CLEAR/STRATEGY`, `REBALANCE`, `ASSIGNMENTS` | Partition by `round_robin` or `range` over healthy members, then load per target |
| Membership | `MEMBERS`, `MEMBER ADD/REMOVE`, `REGISTER` | Runtime members persist with `state_dir` |
| Merged events | `EVENTS [n] [age:<sec>]` | Members' `:5002 EVENTS`, ordered by coordinator receive time |
| Version and skew | `VERSION`, `XACK` caps | `VERSION` reports `protocol: 2`, `fw_version` and deadlines; per member, `XACK` reports channel count and capabilities, so legacy members are detected and mapped |
| Election | `LEADER`, `PREVOTE`, `LEASE` | Only with `coordinator_candidates` (section 6.4) |
| Advisory leader inside instances | none | Lowest healthy `instance_id`, emitted as `coordinator_elected`. Telemetry only; nothing acts on it |

Real replies of a static coordinator with two unreachable members (host name replaced):

```
$ printf 'VERSION\n' | nc 127.0.0.1 6000
{"ok": true, "protocol": 2, "fw_version": "1.1.0-dev", "node_id": "hosta:coordinator", "cluster_id": "default", "role": "static", "term": 0, "members": 2, "verbs": ["PING", "STATUS", "FREQ", "GAIN", "INSTANCE", "REBALANCE", "VERSION", "LEADER", "PREVOTE", "LEASE", "HEALTH", "NODES", "MEMBERS", "MEMBER", "REGISTER", "PEERS", "ALL", "NODE", "FANOUT", "RECAL", "SCHEDULE", "ASSIGNMENTS", "EVENTS", "DB", "ROUTER"], "deadlines": {"query_ms": 6000, "mutation_ms": 40000, "schedule_ms": 73000}, "abandoned_workers": 0}

$ printf 'REBALANCE\n' | nc 127.0.0.1 6000
{"error": "no master schedule"}
```

### 6.2 The fleet CLI

`heimdall-ctl fleet` talks to the coordinator. It finds it from `--coordinator H:P[,H:P...]`, else `$HEIMDALL_COORDINATOR`, else `[federation] coordinator_candidates`, else `coordinator_host:coordinator_port`, else `127.0.0.1:6000`. It tries the endpoints in order on connect failure and follows one `not_leader` redirect. It never retries a request on another coordinator once the request was sent.

| Subcommand | Coordinator verb |
|---|---|
| `fleet status [--watch] [--fresh]` | `NODES STATUS` + `HEALTH` |
| `fleet leader` | `LEADER` |
| `fleet health`, `fleet peers`, `fleet members [add\|remove]` | `HEALTH`, `PEERS`, `MEMBERS` / `MEMBER` |
| `fleet tune`, `gain`, `lna-gain`, `bias-tee`, `agc`, `recal`, `park`, `bearing` | `ALL FREQ`, `ALL GAIN`, `ALL EGAN`, `ALL BIAS`, `ALL AGC`, `RECAL`, `ALL PARK`, `ALL ORNT` |
| `fleet exec [--targets K1,K2] [--node K] VERB ...` | `ALL`, `FANOUT` or `NODE` |
| `fleet schedule set FILE [--strategy round_robin\|range]`, `get`, `clear`, `strategy` | `SCHEDULE ...` |
| `fleet rebalance [--strategy ...] [--include-degraded]` | `REBALANCE` |
| `fleet assignments`, `fleet events`, `fleet db-stats`, `fleet router-stats` | `ASSIGNMENTS`, `EVENTS`, `DB STATS`, `ROUTER STATS` |

Exit codes: 0 ok, applied or unconfirmed (legacy member), 1 error or not applied, 2 usage, 3 connection failure or timeout, 4 `not_leader`, 5 capacity failures.

Real output against a static coordinator whose two members are not running:

```
$ heimdall-ctl --coordinator 127.0.0.1:6000 fleet status
leader 127.0.0.1:6000 (term 0)   overall: error   members: 2   unreachable: 0
NODE         HEALTH  SYNC  FREQ  AGE  OVERRUNS  DROPS
127.0.0.1:0  down    -     -     -    -         -
127.0.0.1:1  down    -     -     -    -         -
```

`unreachable: 0` is not a contradiction. Neither member has ever answered, so `HEALTH` keeps both as `unknown` and lists no unreachable members (section 6.1).

```
$ heimdall-ctl --coordinator 127.0.0.1:6000 fleet tune 433M
NODE         RESULT
127.0.0.1:0  NOT applied: [Errno 61] Connection refused
127.0.0.1:1  NOT applied: [Errno 61] Connection refused
summary: applied 0, not applied 2
$ echo $?
1

$ heimdall-ctl --coordinator 127.0.0.1:6000 fleet rebalance
Error: no master schedule
```

### 6.3 Master schedule and REBALANCE

1. Load the master schedule: `heimdall-ctl fleet schedule set site-scan.json`. This stores and previews the partition. Nothing is sent to members.
2. Distribute it: `heimdall-ctl fleet rebalance`.
3. Check it: `heimdall-ctl fleet assignments`.

The schedule document uses the `signal_scheduler` JSON format:

```json
{"name": "site-scan", "repeat_mode": "loop", "strategy": "round_robin",
 "entries": [{"frequency": 433000000, "gains": [297,297,297,297,297], "dwell_frames": 100},
             {"frequency": 868000000, "dwell_time_sec": 2.0, "require_cal": false}]}
```

| Rule | Behaviour |
|---|---|
| Targets | Members in state `up`, plus `degraded` with `--include-degraded`, sorted by node key |
| No master schedule | `{"error":"no master schedule"}` |
| No healthy member | `{"error":"no healthy instances"}` |
| New leader, members not yet polled | `{"error":"health_warming","retry_in_sec":s}` |
| Transfer | Inline `SCHD` when ≤ 124 bytes; chunked `SCHB/SCHC/SCHE` when the member supports it; a temp-file path for a local legacy member; refused for a remote legacy member (`schedule_too_large_for_legacy_server`) |
| Size limit | One per-target document ≤ 262144 bytes |
| Members with an empty partition, or not targeted | Sent `SCHS` (stop), so no frequency is covered twice |
| Reconciliation | On by default when `cluster_members` or `coordinator_candidates` is set. The leader re-sends a partition to a member that restarted or came back. A member whose schedule an operator replaced directly is marked `overridden` and left alone unless `reconcile_override = 1` |

Without a `state_dir`, a static coordinator forgets the master schedule on restart. `REBALANCE` then replies `no master schedule`.

### 6.4 Coordinator election and fencing

Election runs only when `[federation] coordinator_candidates` lists the coordinator processes (`host:port,...`). Otherwise the coordinator is `static` with term 0.

| Key (in `federation.ini`) | Default | Meaning |
|---|---|---|
| `coordinator_candidates` | empty | candidates taking part |
| `coordinator_self` | first local candidate on the listen port | which candidate this process is (`--self`) |
| `election_interval_sec` | 2.0 | tick period I |
| `election_missed_polls` | 3 | K; lease length L = I × K = 6 s |
| `election_settle_polls` | 5 | after start, no claim and no grant for this many ticks |
| `election_clock_margin` | 0.05 | tolerated clock-rate difference |
| `election_failback_sec` | 60 | a recovered better-ranked candidate must be eligible this long before handover |
| `require_quorum` | 0 | 1: leadership and mutations need a majority of unexpired leases |

The leader holds leases granted by the other candidates. Followers answer every mutating verb with `{"ok":false,"error":"not_leader","leader":{...}}`. A follower takes over within about `L + rank × I + I` (about 10 s for rank 1) after the leader dies.

**Quorum needs at least 3 candidates.** With 2 candidates and `require_quorum = 1`, losing either host blocks all mutations. With `require_quorum = 0`, two leaders can exist during a network partition. This is not consensus. Members fence stale leaders instead.

**Fencing at members.** The coordinator sends its term in `XACK`. Each hw_controller keeps the highest term it has seen in `_data_control/<P>fence_term` and refuses mutating requests with a lower non-zero term (`reason: "stale_term"`). The file survives restarts. Connections with term 0 (heimdall-ctl, DoA clients, static coordinators) are never fenced. A term more than `fence_max_term_jump` (default 1000) above the stored one is refused with `term_jump`. Only an XACK whose `cluster_id` equals the member's own can raise the stored term. `./daq_stop.sh N --reset-fence` removes the file of a stopped instance (section 4.5).

Replicated state (master schedule, runtime members, assignments) is pushed from the leader to followers after every change. With `require_quorum = 1`, a change stored on a minority replies `not_replicated`. With `require_quorum = 0`, the reply carries `"durable": false` when not every follower stored it.

### 6.5 Timeouts

Every coordinator request has a deadline fixed at receipt. `VERSION` advertises it, and `heimdall-ctl` waits that long plus 3 s.

| Quantity | Default | Formula |
|---|---|---|
| Query deadline | 6 s | `peer_connect_timeout_sec + status_timeout_sec + 1` |
| Mutation deadline | 40 s | `mutation_wait_sec + peer_connect_timeout_sec + xack_timeout_sec + max_member_rto_sec + 2 + 1` |
| Member read timeout `T_cmd`, extended member | 26 s | `2 + ctl_request_timeout_sec + ctl_handler_max_sec + 2` |
| Member read timeout `T_cmd`, legacy member | 14 s | `2 + 10 + 2` |
| Circuit breaker | 3 failures, 10 s hold | `breaker_failures`, `breaker_open_sec` |

A target whose `T_cmd` exceeds the remaining budget is skipped before the command is sent (`rto_exceeds_deadline`).

## 7. Data aggregation: the IQ router

`federation_iq_router.py` reads several instances' `:5000` streams as an ordinary client and serves one multi-client output on `:7000`, with statistics on `:7001`.

```
$ python3 _daq_core/federation_iq_router.py --help
usage: federation_iq_router.py [-h] [--config PATH] [--sources LIST]
                               [--output-port PORT] [--listen-address ADDR]
                               [--stats-port PORT] [--queue-frames N]
                               [--drop-policy P] [--client-stall-limit N]
                               [--send-timeout-sec S] [--max-clients N]
                               [--tag-mode M] [--stamp-seq] [--stamp-rx-ts]
                               [--align-window-ms W] [--align-max-frames N]
                               [--max-skew-ms MS]
                               [--connect-on-demand | --always-connect]
                               [--connect-timeout-sec S]
                               [--read-timeout-sec S]
                               [--first-frame-timeout-sec S]
                               [--reconnect-sec S] [--upstream-stats LIST]
                               [--auth-token T] [--allow-cidrs LIST]
                               [--pid-file PATH] [--log-file PATH]
                               [--log-level LEVEL]
```

| Setting | Default | Meaning |
|---|---|---|
| `--sources` / `router_sources` | `cluster_members` (already includes this instance), else `peer_list` plus this instance unless `router_include_self = 0` (config only, default 1) | `host:iid[@node_id]` reads `host:(5000 + iid*stride)`; `router://host:port` reads an upstream router |
| `--queue-frames` | 4 | per-client queue depth |
| `--drop-policy` | `oldest` | full queue: `oldest`, `newest` or `disconnect` |
| `--max-clients` | 8 | output clients |
| `--tag-mode` | `instance` | `instance` rewrites header `unit_id` with the source instance_id; `unit` leaves it; `node` stamps slots 106/107 (instance_id + 1, CRC-32 of the node key) |
| `--connect-on-demand` | on | connect to sources only while a client is attached, so an idle router does not hold `iq_server`'s client slot |
| `--align-window-ms` | 0 | 0 = forward in arrival order; > 0 = hold frames to release them in `time_stamp` order |
| `--read-timeout-sec` | `auto` | `max(5, 3 × frame period)` |

A slow output client never blocks a source reader. It drops frames per the policy and is evicted after 50 consecutive drops, after 2 s without progress, or after a partial frame. Memory is bounded at `frame_bytes × (queue_frames × max_clients + align_max_frames × sources + sources)`; the router logs the bound at start. Exit codes: 0 normal stop, 2 port bind failure, 3 bad arguments or config.

**Topology.** Run one router per host, attached to the local instances. Consumers on that host and a central router connect to its `:7000`. A central router reads per-host routers with `router://host:7000` sources. Every upstream router used this way must run `--tag-mode node`, so the central router can tell the sources apart. Set `[federation] stamp_node_id = 1` on the instances to stamp slot 108 at the source as well.

Real stats query against a router with two idle sources (no client attached):

```
$ heimdall-ctl --router 127.0.0.1:7001 router sources
SOURCE   STATE  CONNECTED  FRAMES  LOST  GAPS  RESTARTS  LAST-AGE  OFFSET-MS
hosta:0  idle   no         0       0     0     0         -         None
hostb:0  idle   no         0       0     0     0         -         None
```

What the router does not do:

* It does not make islands coherent. Frames from two instances come from independent clocks and independent calibrations.
* It does not align by sample. Alignment groups frames by the header `time_stamp`, which is wall-clock milliseconds at USB completion. Comparing timestamps across hosts requires NTP or PTP on every host. The router reports `clock_offset_ms` per source and sets `skew_warning` when a source's offset differs from the median of all sources by more than `--max-skew-ms` (500). It does not detect a fleet-wide offset.
* It does not hide loss. Per source it counts `frames_lost`, `cpi_gaps` (jumps in `cpi_index`), `restarts` and `overruns` (slot 102).

Calibration bursts are per island. Each instance runs its own noise-source burst and sync state machine. During a burst its frames are CAL or DUMMY frames; consumers must check the frame type and the sync flags per source.

See [consumer_guide.md](consumer_guide.md) for the header slots and [networking_and_time_sync.md](networking_and_time_sync.md) for clock discipline.

## 8. Failure modes and recovery

| Failure | What you see | Recovery |
|---|---|---|
| Peer down | `peer_down` event; STATUS `federation` row `alive: false`. With `peer_down_misses` absent: down when a poll fails and the peer was last seen more than `3 × health_poll_interval` ago (15 s at defaults). With `peer_down_misses = k`: after k consecutive failed polls, once the peer was last seen more than k × `health_poll_interval` ago | Automatic `peer_up` on the next good poll |
| Peer flapping | Only with `peer_flap_window_sec` and `peer_flap_threshold` set: state `flapping`, reported as degraded | Fix the link; state returns after a quiet window |
| Static coordinator dies | `fleet` commands fail with exit 3. Instances keep running | Restart it anywhere that reaches the members. Without `state_dir` the master schedule is lost; load it again |
| Elected leader dies | Followers reply `not_leader` until one takes over (about 10 s for rank 1) | Automatic. heimdall-ctl follows the redirect |
| Network partition, `require_quorum = 1` | Minority side replies `no_quorum` to mutations | Heal the network |
| Network partition, `require_quorum = 0` | Two leaders possible | Members refuse the lower term (`stale_term`) |
| Instance crash | Peers report it down; coordinator STATUS `error`; router source goes to `backoff` | Restart the instance. The supervised units restart it automatically (section 4.6) |
| Stalled pipeline (processes alive, no frames) | With `[monitoring] status_extended = 1`: STATUS `pipeline_health: "stale"` after `stale_after_sec` (auto `3 × k × T_fp + 1` s, k = `status_update_interval` when > 0, else `heartbeat_interval`; 132 s at defaults). The coordinator also flags a member whose `frame_count` stops advancing (`member_stall_after_sec`, auto) | Inspect the logs; restart the instance |
| Duplicate node key | REGISTER rejected with `duplicate_node_key`; `MEMBERS` lists `key_collisions` | Set a unique `advertise_host` or `node_id` |
| Foreign cluster | `cluster_mismatch` | Align `cluster_id` |
| Router consumer lost | Router evicts the client; sources keep streaming to other clients | Client reconnects |
| `iq_server` slot held by another client | Router source in `waiting_for_slot` | Free the slot, or set `[data_interface] iq_server_max_clients > 1` |
| Busy ports at start (default `instance_guard = legacy`) | Start script runs the global `./daq_stop.sh`; every instance stops | Start instances one at a time; set `instance_registry = 1` and `instance_guard = refuse`, or start under the supervisor, so the busy-port loop runs the scoped `./daq_stop.sh N` |

## 9. Worked examples

### 9.1 Two hardware instances on one 8-core x86 host

Hardware: 8 cores, two xHCI controllers, two 5-channel units on separate controllers. Unit A has serials 1000-1004 and unit B 1100-1104.

| Instance | Directory | Serials | Cores | Ports |
|---|---|---|---|---|
| 0 | `/opt/heimdall/Firmware` | `serial_base = 1000` (default) | 0,1,2,3 | 1130, 5000-5003 |
| 1 | `/opt/heimdall/instances/1/Firmware` | `serial_base = 1100` | 4,5,6,7 | 1230, 5100-5103 |

Steps by hand:

1. Build: `cd /opt/heimdall/Firmware/_daq_core && make`.
2. Program unit B's EEPROM serials to 1100-1104. Plug in unit B alone and run `sudo util/set_unit_serials.sh --unit 1` (it prints the plan; add `--yes` to write and verify), or use `rtl_eeprom` directly. See [hardware_setup.md](hardware_setup.md).
3. Create instance 1's directory as in section 4.4. Set in its INI:

   ```ini
   [hw]
   serial_base = 1100
   unit_id = auto

   [daq]
   ctr_channel_serial_no = 1100

   [monitoring]
   en_monitoring = 1
   en_status_server = 1

   [federation]
   instance_id = 1
   ```

   `ctr_channel_serial_no` must be one of this instance's serials (section 4.4).

4. In instance 0, set `[hw] unit_id = auto`, so consumers can tell the streams apart by header `unit_id`. Also set `[monitoring] en_monitoring = 1` and `en_status_server = 1` there for step 7.
5. Validate both: `python3 ini_checker.py daq_chain_config.ini no_hw` in each `Firmware/` directory.
6. Start instance 0, then instance 1: `sudo ./daq_start_sm.sh` in each directory. The core formula gives instance 1 cores 4-7 with no warning on 8 cores.
7. Check: `heimdall-ctl status` and `heimdall-ctl --instance 1 status`. These read the status servers (`:5002` and `:5102`) enabled in steps 3 and 4.
8. Stop one: `sudo ./daq_stop.sh 1` in instance 1's directory.

With the provisioning tool and the supervised units, steps 3-6 become (run from `/opt/heimdall`; drop `--write` first to see the dry-run plan):

```sh
sudo python3 util/provision_instance.py --instance 0 --in-place --systemd --write
sudo python3 util/provision_instance.py --instance 1 --systemd --write
sudo systemctl enable --now heimdall-host-tuning
sudo systemctl enable --now heimdall-daq-supervised heimdall-daq-supervised@1
```

`--systemd` sets `[host] host_tuning = 0`, so `heimdall-host-tuning.service` does the host tuning once per boot. The provisioned INIs also enable the status servers, so step 7 works unchanged.

### 9.2 Three hosts, one instance each, with coordinator and router

Hosts `hosta`, `hostb`, `hostc`, each with one 5-channel unit as instance 0. Coordinator candidates on all three; one router on `hosta` serving the consumers.

Instance INI on `hostb` (the other hosts differ only in `advertise_host`):

```ini
[hw]
unit_id = auto

[daq]
ctl_max_connections = 4

[data_interface]
out_data_iface_type = eth

[monitoring]
en_monitoring = 1
en_status_server = 1

[federation]
instance_id = 0
en_federation = 1
cluster_id = site-a
cluster_members = hosta:0,hostb:0,hostc:0
coordinator_candidates = hosta:6000,hostb:6000,hostc:6000
advertise_host = hostb
stamp_node_id = 1
auth_token = 3f9c2b7e1d
```

`federation.ini` for the coordinators and the router:

```ini
[federation]
cluster_id = site-a
port_stride = 100
cluster_members = hosta:0,hostb:0,hostc:0
coordinator_candidates = hosta:6000,hostb:6000,hostc:6000
require_quorum = 1
auth_token = 3f9c2b7e1d
router_sources = hosta:0,hostb:0,hostc:0
router_tag_mode = node
router_stats_endpoint = hosta:7001
```

Both files pass validation with no warnings:

```
$ python3 ini_checker.py daq_chain_config.ini no_hw && echo ok
ok
$ python3 ini_checker.py federation.ini --federation && echo ok
ok
```

Start order:

1. On every host: start the instance (`sudo ./daq_start_sm.sh`).
2. On every host: install `federation.ini` and the coordinator's state directory. Replace `<coordinator-user>` with the unprivileged user that runs the coordinator. Mode 640 keeps `auth_token` unreadable to other users:

   ```sh
   sudo install -d -o <coordinator-user> -m 750 /var/lib/heimdall/coordinator
   sudo install -D -m 640 -o root -g <coordinator-user> federation.ini /etc/heimdall/federation.ini
   ```

   A candidate that cannot write its `state_dir` cannot persist its term. It is never eligible to lead and logs `state_dir_unwritable`. If no candidate can write, no leader is elected and every fleet mutation fails.

3. On every host: start a coordinator candidate as `<coordinator-user>`. Keep its log out of `_logs/*.log`: instance 0's start script moves those files into `_logs/archive/` and prunes old archives. `--self` names this host's `coordinator_candidates` entry (here on `hostb`). `nohup` keeps the process alive after the SSH session ends:

   ```sh
   cd /opt/heimdall/Firmware
   mkdir -p _logs/federation/pids
   nohup python3 _daq_core/federation_coordinator.py --config /etc/heimdall/federation.ini \
       --self hostb:6000 --state-dir /var/lib/heimdall/coordinator \
       --log-file _logs/federation/coordinator.log \
       --pid-file _logs/federation/pids/coordinator.pid >/dev/null 2>&1 &
   ```

4. On `hosta`: start the router the same way:

   ```sh
   nohup python3 _daq_core/federation_iq_router.py --config /etc/heimdall/federation.ini \
       --log-file _logs/federation/router.log \
       --pid-file _logs/federation/pids/router.pid >/dev/null 2>&1 &
   ```

   Stop them with `kill $(cat _logs/federation/pids/coordinator.pid)` and `kill $(cat _logs/federation/pids/router.pid)`, or with `./federation_stop.sh all`, which uses the same PID files.

   Instead of steps 3 and 4 you can run `./federation_start.sh coordinator --state-dir /var/lib/heimdall/coordinator` and `./federation_start.sh router` (both read `/etc/heimdall/federation.ini` when it exists), or enable `heimdall-coordinator.service` and `heimdall-iq-router.service` (user `heimdall`). Neither passes `--self`, so set `[federation] coordinator_self` in each host's `federation.ini`, or rely on the host-name match described under Requirements.

5. Check:

   ```sh
   export HEIMDALL_AUTH_TOKEN=3f9c2b7e1d
   heimdall-ctl --coordinator hosta:6000,hostb:6000,hostc:6000 fleet leader
   heimdall-ctl --coordinator hosta:6000,hostb:6000,hostc:6000 fleet status
   heimdall-ctl --router hosta:7001 router stats
   ```

Requirements:

* chrony or PTP on every host (router alignment and timestamp comparison).
* Name resolution: every host resolves `hosta`, `hostb` and `hostc` to routable addresses (DNS or `/etc/hosts`, not `127.0.1.1`).
* Candidate identity: pass `--self <host>:6000` on each host, or set `[federation] coordinator_self`. Without either, the coordinator matches `coordinator_candidates` against this machine's host name and local interface IPs, without DNS. That works only when the host name is exactly `hosta`, `hostb` or `hostc`. Otherwise the coordinator exits 3 with `cannot tell which coordinator_candidates entry this process is`.
* Firewall: the flows in the table in section 5.3. The candidates must reach each other on 6000; blocking it stops election and replication, and with `require_quorum = 1` mutations fail with `no_quorum`.

See [networking_and_time_sync.md](networking_and_time_sync.md). The three islands are independent; downstream DSP must treat them as separate arrays.

### 9.3 Raspberry Pi 4: one hardware instance

The Pi 4 has 4 cores. One hardware instance uses all four RT tiers. A second hardware instance maps to the same cores (`(1*4 + k) % 4 = k`); the start script warns and the RT processes compete. Run one hardware instance per Pi 4 (the same holds for the Pi 5, whose separate RP1 USB buses do not add cores). Scale out by adding Pis as federation members (section 9.2), not by adding instances. A synthetic instance for testing is possible, but it also uses the RT tiers.

## 10. Checklists

### Several instances on one host

- [ ] One working copy per instance N > 0 (section 4.4); instance 0 in `<root>/Firmware`.
- [ ] `make` run after the last update.
- [ ] Distinct serials per instance (`serial_base` or `device_serials`); `unit_id = auto`.
- [ ] `instance_id` 0-9 with stride 100; `heimdall-ctl --instance N ports` shows no overlap.
- [ ] `[daq] ctr_channel_serial_no` is one of the instance's serials (normally `serial_base`).
- [ ] Instance ids on a host are contiguous from 0, and `4 × (highest instance_id + 1) ≤ nproc`.
- [ ] Units on separate USB controllers where the bus budget requires it ([capacity_planning.md](capacity_planning.md)).
- [ ] `ini_checker.py ... no_hw` passes with no `WARNING:` in every copy.
- [ ] Start one instance at a time; stop with `./daq_stop.sh N`, never the no-argument form.
- [ ] `[database]`: private `_db` per copy, or `instance_subdir = 1`.

### Multi-host federation

- [ ] Unique host names, or `advertise_host` / `node_id` set.
- [ ] The same `cluster_members` and `cluster_id` on every instance and in `federation.ini`.
- [ ] `en_federation = 1`, `en_monitoring = 1`, `en_status_server = 1`, `ctl_max_connections = 4` on every member.
- [ ] `out_data_iface_type = eth` on members the router reads.
- [ ] `ini_checker.py federation.ini --federation` passes.
- [ ] 3 or more coordinator candidates if `require_quorum = 1`.
- [ ] `auth_token` or `allow_cidrs` set when the coordinator listens on a routable address.
- [ ] NTP or PTP on every host.
- [ ] Upstream routers run `--tag-mode node`; instances set `stamp_node_id = 1`.
- [ ] `heimdall-ctl fleet status` shows every member `up`; `heimdall-ctl router stats` shows every source `streaming` while a client is attached.
