# Operations Runbook and Troubleshooting

This runbook maps symptoms to causes and fixes. It also lists every log file, PID file and diagnostic command of the HeIMDALL DAQ chain.

All commands run from the `Firmware/` directory of the copy you operate, unless a line says otherwise. Shell examples marked `sudo` need root on Linux.

Some features below are **opt-in**. They are off in the shipped `daq_chain_config.ini` and change nothing until you enable them. Each opt-in item names the key or flag that enables it.

A few items describe tooling that this release is still building: the new systemd units and the watchdog timer (WP-16). Those items say so in their first line and carry a `verify-after` marker for the reconciliation pass. The launch-script library (`daq_common.sh`), the supervisor, the federation launchers, the capacity pre-flight (`daq_preflight.py`) and the provisioning tools are in this tree.

Related documents:

| Document | Use it for |
|---|---|
| [architecture.md](architecture.md) | Process pipeline, FIFOs, shared memory, FSMs |
| [configuration.md](configuration.md) | Every INI key, default and validation rule |
| [protocols.md](protocols.md) | `:5001`, `:5002`, `:6000`, `:7001` wire formats and reply shapes |
| [scaling_and_clustering.md](scaling_and_clustering.md) | Instances, federation, election, fencing |
| [deployment_recipes.md](deployment_recipes.md) | Worked deployments with health checklists |
| [networking_and_time_sync.md](networking_and_time_sync.md) | Port table, bind addresses, firewall, NTP/PTP |
| [capacity_planning.md](capacity_planning.md) | Cores, USB bandwidth, memory per instance |
| [hardware_setup.md](hardware_setup.md) | Serial provisioning, USB topology, LNAs, rotators |
| [upgrade_and_rollback.md](upgrade_and_rollback.md) | Full upgrade and rollback procedure |
| [theory_coherent_acquisition.md](theory_coherent_acquisition.md) | Calibration states, noise-source burst, tracking modes |
| [glossary_faq.md](glossary_faq.md) | Terms (instance, unit, node, term, track lock) |

## Contents

1. [Quick reference](#1-quick-reference)
2. [Start, stop and restart](#2-start-stop-and-restart)
3. [Health checks](#3-health-checks)
4. [Logs](#4-logs)
5. [Troubleshooting: startup failures](#5-troubleshooting-startup-failures)
6. [Troubleshooting: hung or idle chain](#6-troubleshooting-hung-or-idle-chain)
7. [Troubleshooting: calibration and sync](#7-troubleshooting-calibration-and-sync)
8. [Troubleshooting: data plane](#8-troubleshooting-data-plane)
9. [Troubleshooting: control plane](#9-troubleshooting-control-plane)
10. [Troubleshooting: federation, coordinator and router](#10-troubleshooting-federation-coordinator-and-router)
11. [Troubleshooting: database](#11-troubleshooting-database)
12. [Procedures](#12-procedures)
13. [Upgrades](#13-upgrades)
14. [Data to collect for a bug report](#14-data-to-collect-for-a-bug-report)
15. [Privilege model](#15-privilege-model)
16. [Known limitations](#16-known-limitations)

---

## 1. Quick reference

| Task | Command |
|---|---|
| Validate the config offline | `python3 ini_checker.py no_hw` (exit 0 valid, 1 errors) |
| Start with hardware | `sudo ./daq_start_sm.sh` |
| Start without hardware | `sudo ./daq_synthetic_start.sh` |
| Stop every DAQ process on the host | `sudo ./daq_stop.sh` |
| Stop one instance of this copy | `sudo ./daq_stop.sh <N>` |
| Start or stop the federation services | `./federation_start.sh all --config federation.ini`, `./federation_stop.sh all` |
| Pipeline health | `heimdall-ctl status` (needs the status server, [section 3.1](#31-enable-the-status-endpoints)) |
| hw_controller state | `heimdall-ctl hwc-status` (always available on `:5001`) |
| Port map of an instance | `heimdall-ctl --instance N ports` |
| Recent events | `heimdall-ctl events` |
| Force a recalibration | `heimdall-ctl recal` ([section 12.1](#121-force-a-recalibration)) |
| Approve the track lock | `heimdall-ctl track-lock approve` ([section 12.2](#122-track-lock-approval)) |
| Attached receivers as JSON | `python3 _daq_core/hw_discover.py --sdr` |

`heimdall-ctl` runs from an installed package (`pip install .` at the repository root) or from the in-tree launcher `util/bin/heimdall-ctl`. It reads ports from `--config`, `--instance N` and `--port-stride`. Its exit codes are:

| Code | Meaning |
|---|---|
| 0 | Success, applied, or unconfirmed by a legacy server |
| 1 | Error reply or `applied:false`; `--verify` did not observe the value |
| 2 | Usage error |
| 3 | Connection failure or timeout |
| 4 | `not_leader` after one redirect (fleet commands) |
| 5 | `capacity`: the pre-flight tool reported failures |

Per-instance ports use `offset = instance_id × port_stride` (stride default 100). See [networking_and_time_sync.md](networking_and_time_sync.md) for the full table.

| Port | Service | Instance 0 | Instance 1 (stride 100) |
|---|---|---|---|
| `1130 + offset` | rtl_daq ZMQ tuner control (hw_controller and delay_sync talk to it) | 1130 | 1230 |
| `5000 + offset` | iq_server IQ stream (`out_data_iface_type = eth` only) | 5000 | 5100 |
| `5001 + offset` | hw_controller control | 5001 | 5101 |
| `status_server_port + offset` | status server (opt-in) | 5002 | 5102 |
| `zmq_pub_port + offset` | ZMQ event publisher (opt-in) | 5003 | 5103 |
| 6000 | federation coordinator (not offset) | 6000 | 6000 |
| 7000 / 7001 | IQ router output / stats (not offset) | 7000 / 7001 | 7000 / 7001 |

---

## 2. Start, stop and restart

### 2.1 Start one instance

Run the start script as root from the `Firmware/` directory of the copy. The instance id comes from `[federation] instance_id` in that copy's `daq_chain_config.ini`. It is never a command-line argument.

```bash
cd Firmware
sudo ./daq_start_sm.sh          # receivers attached
sudo ./daq_synthetic_start.sh   # synthetic data, no receivers
```

Both start scripts source `./daq_common.sh`, the shared launch library. With no `[host]` key set, they keep the classic steps. `daq_start_sm.sh` accepts two flags. It ignores unknown arguments.

| Flag | Effect |
|---|---|
| `--takeover` | with `[host] instance_guard = refuse` only: stop instance N when it runs from another copy, then start ([2.5](#25-registry-instance-lock-and-duplicate-guard-opt-in)) |
| `--no-host-tuning` | same as `[host] host_tuning = 0` |

`daq_start_sm.sh` performs these steps in order (`<P>` is `inst<N>_` for instance N > 0, empty for instance 0):

| Step | What happens | Failure |
|---|---|---|
| 1 | Checks that `./daq_common.sh` exists and sources it | `daq_common.sh not found next to ./daq_start_sm.sh - DAQ chain not started!`, exit 1 |
| 2 | `python3 ini_checker.py no_hw attached` (`attached` adds the sysfs serial and live-registry checks) | prints `Config file check [ FAIL ]`, exit 1 |
| 3 | Reads the INI in one ConfigParser pass; early host tuning per `[host] host_tuning` (table below) | continues |
| 4 | Registry, startup mutex and instance lock, only with `[host] instance_registry = 1` ([2.5](#25-registry-instance-lock-and-duplicate-guard-opt-in)) | exit 3 under `instance_guard = refuse` |
| 5 | Recreates this instance's 8 control FIFOs in `_data_control/`; deletes `<P>recal_request` and `<P>hwc_state`; creates `<P>iq_track_lock` with content `0` when it is absent | continues |
| 6 | Creates `_db/` and the log directory; moves old `*.log` files into `archive/<timestamp>/` (keeps 10) | continues |
| 7 | Late host tuning (`host_tuning = always` only): `usbfs_memory_mb = 0`, drops the page cache | prints `WARN: /sys/module/usbcore/parameters/usbfs_memory_mb not present, skipping` |
| 8 | Port check, listener-only, on `5000`, `5001` and `status_server_port` (+ offset). `1130 + offset` and, with `en_monitoring = 1` and `en_zmq_pub = 1`, the PUB port are advisory: `WARN: port P is in use by <holder>` | each busy round prints `WARN:Ports used by the DAQ chain instance N are not free! (...)` and runs a stop ([2.3](#23-restart)). After 10 attempts: `ERROR: Ports (...) still busy after 10 attempts - DAQ chain not started!` plus the listener lines, exit 1 |
| 9 | `python3 fir_filter_designer.py` | `DAQ chain not started!`, exit 1 |
| 10 | `hw_discover.py` into `_data_control/<P>hw_caps.json`, `auto_config.py` | errors ignored |
| 11 | FPGA bitstream and GPU init when enabled | falls back to CPU, continues |
| 12 | Purges this instance's PID files; picks the cores (`[host] cpu_cores`, else `4N..4N+3` modulo `nproc`); core guard | `WARN: K cores for M instances - RT processes of multiple instances will share cores (...)`. With the registry on and `allow_core_sharing = refuse`: `ERROR: cores X of instance N are used by instance M running from C ([host] allow_core_sharing = refuse) - DAQ chain not started!`, exit 3 |
| 13 | Pre-flight, only with `[host] preflight = warn` or `strict`: `python3 daq_preflight.py --config daq_chain_config.ini --plus-live` | `WARN: preflight reported problems (exit rc); continuing (...)`. With `strict` and tool exit 2: `ERROR: preflight failed ([host] preflight = strict) - DAQ chain not started!`, exit 4 |
| 14 | Launches the stages with `taskset` and `chrt` (each priority minus `[host] rt_priority_offset`). Arms an EXIT trap after the first stage | if the script then exits non-zero, the trap prints `Start of instance N failed after launching stages (exit rc) - stopping it` and runs `./daq_stop.sh N` |
| 15 | Writes the registry entry (registry on only), disarms the trap, prints the coffee banner, exit 0 | - |

Exit codes of both start scripts:

| Code | Meaning |
|---|---|
| 0 | stages launched |
| 1 | `daq_common.sh` missing, config check failed, FIR designer failed, or ports still busy |
| 3 | guard refusal: `instance_guard = refuse` (another copy runs N, another start is in progress, or the lock is still held) or `allow_core_sharing = refuse` |
| 4 | pre-flight failure with `[host] preflight = strict` |

Exit code 0 only means the stages were launched. The script does not check that a stage survived. A stage that dies right away (missing binary, missing Python module, fatal config error) shows up only in its log file. Always run a health check after a start ([section 3](#3-health-checks)).

**`[host]` keys (opt-in).** The shipped INI carries the section commented out, including the `; [host]` header line. Uncomment the header and the keys you set. See [configuration.md](configuration.md) for validation rules.

| Key | Default | Effect |
|---|---|---|
| `cpu_cores` | unset | four distinct cores for the tiers rtl_daq, rebuffer+decimate, delay_sync+iq_server, hw_controller. Unset: `4N..4N+3` modulo `nproc` |
| `delay_sync_cores` | unset | core list for delay_sync only |
| `rt_priority_offset` | `0` | subtracted from every RT priority (0 to 81) |
| `allow_core_sharing` | `warn` | `warn`, `refuse` or `allow`. Checks core overlap with other live registry entries (needs `instance_registry = 1`) |
| `host_tuning` | `always` | `always`: the script runs `sysctl kernel.sched_rt_runtime_us=-1`, `../util/irq_tuning.sh`, the usbfs and drop-cache steps itself. `1`: runs `util/host_tuning.sh --apply` instead. `auto`: as `1`, but skips (`host tuning skipped (<reason>)`) when `heimdall-host-tuning.service` is active or another registry instance runs. `0`: skips (`host tuning skipped (disabled)`) |
| `preflight` | `off` | `off`, `warn` or `strict` (step 13) |
| `instance_registry`, `instance_guard` | `0`, `legacy` | [2.5](#25-registry-instance-lock-and-duplicate-guard-opt-in) |
| `usb_irq_core`, `usb_controller` | unset | passed to the IRQ tuning as `--usb-core` and `--controller` |

`daq_synthetic_start.sh` differs from `daq_start_sm.sh`:

| Aspect | `daq_synthetic_start.sh` |
|---|---|
| Flags | `--rt`, `--pace`, `--blocks N`, `--seed S`, `--takeover`, `--no-host-tuning` (accepted, no effect). Unknown arguments are ignored |
| Data source | `python3 _testing/test_data_synthesizer.py` in place of `rtl_daq.out`; it answers ZMQ control on `1130 + offset` |
| Config check | `python3 ini_checker.py no_hw` (no `attached`); a failure prints `Config file check [FAIL]`, exit 1 |
| Host tuning | none (no sysctl, irq_tuning, usbfs, drop_caches) |
| Port check | advisory: prints `WARN: port P is in use by <holder>` and stops nothing. Under `[host] instance_guard = refuse` the hardware rules of step 8 apply |
| CPU pinning | default: none; only `decimate.out` gets `chrt -f 99` (needs root). `--rt`: the hardware tiers, `cpu_cores`, `rt_priority_offset`, core guard and pre-flight |
| FIR designer failure | prints `DAQ chain not started!`, exit 1 |
| hw_controller | `sudo env "PATH=$PATH" python3`, as in `daq_start_sm.sh` |
| PID file of the source | `synthesizer.pid` in place of `rtl_daq.pid` |
| Exit codes | as above; 1 also for a flag without its value (`--blocks needs a value - DAQ chain not started!`); 4 only with `--rt` |

The synthesizer generates 10000 DAQ blocks of `daq_buffer_size` samples by default and then exits, which ends the chain. At the shipped sizes (`daq_buffer_size = 262144`, `cpi_size = 1048576`, `decimation_ratio = 1`) that is about 2500 frames. The start script passes `--pace`, `--blocks N` (0 = unbounded) and `--seed S` (0 to 4294967295) to the synthesizer when you give them. `--rt` implies `--pace`. The synthesizer flags `--zero-delays` and `--unit-id-from-config` are available only when you run the synthesizer by hand.

```bash
sudo ./daq_synthetic_start.sh --blocks 0 --seed 42   # unbounded, reproducible data
sudo ./daq_synthetic_start.sh --rt                   # hardware RT tiers, paced
```

### 2.2 Stop

`daq_stop.sh` stops the whole host, one instance, or this copy. All forms combine with `--legacy`.

| Command | Scope | Behaviour |
|---|---|---|
| `sudo ./daq_stop.sh` | **whole host** | Sends SIGTERM to every PID in `_logs/inst*/pids/` of this copy. Then runs `pkill -TERM` by name on `rtl_daq.out`, `decimate.out`, `rebuffer.out`, `iq_server.out`, the synthesizer, `delay_sync.py`, `hw_controller.py`. Waits 2 s, sends SIGKILL to surviving PID-file processes and `pkill -KILL` to the four C binaries and the synthesizer. Removes the PID files. Prunes dead registry entries (`Removed stale registry entry <dir>/inst<N>`) |
| `sudo ./daq_stop.sh <N>` | instance N of this copy | SIGTERM to each PID in `_logs/inst<N>/pids/`, 2 s grace per process, SIGKILL to survivors, removes each PID file. Prints `Instance N stopped`. Removes N's registry entry when it belongs to this copy, else prints `WARN: registry entry for instance N belongs to C; not removed (use --all or stop it from that copy)`. No name sweep |
| `sudo ./daq_stop.sh <N> --clean` | instance N | the `<N>` stop, then removes N's FIFOs, `/dev/shm` segments and relay files (`orientation_state`, `bias_tee_state`, `hwc_state`, `recal_request`). Prints `Instance N control files and shared memory removed (iq_track_lock, fir_coeffs and fence_term kept)` |
| `sudo ./daq_stop.sh <N> --reset-fence` | instance N | the `<N>` stop, then removes `_data_control/<P>fence_term`. Prints `fencing state of instance N reset; the next coordinator XACK sets it again` |
| `sudo ./daq_stop.sh --this-copy` | this copy | signals the PID files of this copy, then the name patterns of the global stop limited to processes whose working directory is this copy. Without `/proc` it uses only the PID files (`WARN: no proc filesystem; only PID files of this copy were used`) |
| `sudo ./daq_stop.sh --all` | **whole host** | the no-argument form plus SIGKILL for the two Python stages and removal of every registry entry (`Registry entries removed`). Keeps every `fence_term` |
| `... --federation` | adds federation | after the selected form, runs `./federation_stop.sh all`. Prints `WARN: ./federation_stop.sh not found; federation services not stopped` when the script is absent |
| `--legacy` | combinable | Uses signal 64 (SIGRT) instead of SIGTERM, for old binaries |

`--clean` and `--reset-fence` always run the per-instance stop first. They act only when no process of N survives it. Otherwise they print `ERROR: instance N still has running processes; --clean/--reset-fence refused` and exit 3.

| Exit code | Meaning |
|---|---|
| 0 | done |
| 2 | usage error: `--this-copy` with an instance id, `--all` with an id or `--this-copy`, `--clean` or `--reset-fence` without a numeric id |
| 3 | `--clean` or `--reset-fence` refused |

The no-argument form keeps the exit status it always had.

Every form skips a PID whose command line no longer contains the role name (`Warning: stale PID file ... belongs to an unrelated process, skipping`).

> **Global-stop hazard.** The no-argument form matches processes by name across the whole host. It stops every instance and every other copy, including `heimdall-daq@N` units. On a host with more than one instance, stop with `./daq_stop.sh <N>`.

The no-argument SIGKILL list does not include `delay_sync.py` or `hw_controller.py`. A Python stage that ignores SIGTERM survives the global stop. Check with `pgrep -af '_daq_core/(delay_sync|hw_controller).py'`, or use `--all`.

### 2.3 Restart

There is no restart command. Stop the instance, check that its processes are gone, then start it.

```bash
sudo ./daq_stop.sh 0
pgrep -af 'rtl_daq.out|rebuffer.out|decimate.out|iq_server.out|delay_sync.py|hw_controller.py'
sudo ./daq_start_sm.sh
```

Re-running `daq_start_sm.sh` while the same instance runs also restarts it, because its listeners make the port check busy. Each busy round runs a stop:

| Setting | Stop run by each busy round |
|---|---|
| default (`instance_guard = legacy`) | the **global** `./daq_stop.sh`, which stops every other instance on the host too |
| `HEIMDALL_SCOPED_STOP=1` in the environment, or `[host] instance_guard = refuse` | `./daq_stop.sh N` (this instance only) |

`daq_supervise.sh` exports `HEIMDALL_SCOPED_STOP=1` for its start script. To get the scoped stop by hand, run `sudo env HEIMDALL_SCOPED_STOP=1 ./daq_start_sm.sh`.

### 2.4 Several instances and a fleet

Each instance needs its own working copy (or instance directory) with its own `daq_chain_config.ini`. Instance N > 0 prefixes its FIFOs, shared memory, FIR coefficients and relay files with `inst<N>_`. It logs to `_logs/inst<N>/`.

Each hardware instance needs its own receivers with distinct EEPROM serials. Set `[hw] serial_base` (channel i opens serial `serial_base + i`, default 1000) or `[hw] device_serials` in each instance's INI. See [hardware_setup.md](hardware_setup.md).

Start and stop each instance from its own copy:

```bash
(cd /opt/heimdall/Firmware && sudo ./daq_start_sm.sh)               # instance 0
(cd /opt/heimdall/instances/1/Firmware && sudo ./daq_start_sm.sh)   # instance 1
(cd /opt/heimdall/instances/1/Firmware && sudo ./daq_stop.sh 1)
```

**Provisioning.** Two tools prepare an instance. Both print a plan and change nothing unless you add `--write` or `--yes`. Run them from `Firmware/`:

```bash
python3 ../util/provision_instance.py --instance 1             # dry run: prints the plan
sudo python3 ../util/provision_instance.py --instance 1 --write
sudo ../util/set_unit_serials.sh --unit 1                      # plan only
sudo ../util/set_unit_serials.sh --unit 1 --yes                # programs the EEPROMs
```

| Tool | Does | Exit codes |
|---|---|---|
| `util/provision_instance.py --instance N --write` | creates `<root>/instances/N/` (default `--dest`) with code symlinks into the install root (`--mode link`) and private `daq_chain_config.ini`, `_data_control/`, `_logs/`, `_db/`. Sets `[hw] serial_base` to `1000 + 100·N` unless given | 0 ok, 1 validation or write failed, 2 usage, 3 destination exists, 4 instance live |
| `util/set_unit_serials.sh --unit K --yes` | programs unit K's receivers to `base + K·stride + i` (defaults 1000 and 100), then re-reads and verifies them. Plug in one unit at a time | 0 ok or plan printed, 2 usage, 3 receivers held, 4 receiver count differs from `--count`, 5 `rtl_eeprom` not on `PATH`, 6 old serials not contiguous, 7 verify failed |

See [deployment_recipes.md](deployment_recipes.md) and [hardware_setup.md](hardware_setup.md).

**Federation services.** A federation adds the coordinator and, optionally, the IQ router. Both are plain Python processes that need no root. Create `federation.ini` with `util/cluster_cfg_gen.py` (it writes the instance INIs, `federation.ini`, `ports.md` and `provision.sh`) or by hand ([scaling_and_clustering.md](scaling_and_clustering.md)). Validate it, then start the services with the launcher:

```bash
python3 ini_checker.py federation.ini --federation
./federation_start.sh all --config federation.ini
./federation_stop.sh all
```

```
./federation_start.sh coordinator|router|all [--config PATH] [--foreground]
                      [--state-dir DIR] [--log-dir DIR] [--pid-dir DIR]
./federation_stop.sh  coordinator|router|all [--pid-dir DIR]
```

| Option | Default |
|---|---|
| `--config` | `/etc/heimdall/federation.ini` when present, else `./daq_chain_config.ini` |
| `--log-dir` | `$HEIMDALL_FED_LOG_DIR`, else `_logs/federation` (`coordinator.log`, `router.log`) |
| `--pid-dir` | `$HEIMDALL_FED_PID_DIR`, else `_logs/federation/pids` (`coordinator.pid`, `router.pid`) |
| `--state-dir` | `$HEIMDALL_FED_STATE_DIR`, else not passed (coordinator only) |
| `--foreground` | execs one service in the script's process (for systemd); not allowed with `all` |

The start creates the log and PID directories. It waits 1 s and checks that the service still runs.

| Script | Exit codes |
|---|---|
| `federation_start.sh` | 0 started; 1 the service exited immediately (`federation <name> exited immediately - see <log>`, plus its last 5 log lines); 2 usage error; 3 the service is already running (its PID file is live) |
| `federation_stop.sh` | 0 done (a missing or stale PID file is not an error); 2 usage error. It sends SIGTERM, waits 5 s, then SIGKILL |

With `all`, the start returns the coordinator's code when it is non-zero, else the router's.

To run a service by hand instead, create the log directory first and put each service in the background:

```bash
mkdir -p _logs/federation
nohup python3 _daq_core/federation_coordinator.py --config federation.ini \
    --log-file _logs/federation/coordinator.log --pid-file _logs/federation/coordinator.pid &
nohup python3 _daq_core/federation_iq_router.py --config federation.ini \
    --log-file _logs/federation/router.log --pid-file _logs/federation/router.pid &
```

The coordinator exits 3 on a configuration or log-file error and 2 when it cannot bind its port. The router exits 3 on a configuration or PID-file error and 2 when it cannot bind its output or stats port. Stop either with SIGTERM (`kill $(cat <pid-file>)`).

### 2.5 Registry, instance lock and duplicate guard (opt-in)

The registry and the guard are off by default. With `[host] instance_registry = 0`, the start scripts open no extra fd, export nothing and write no registry file.

| Key (`[host]`) | Default | Effect |
|---|---|---|
| `instance_registry` | `0` | `1`: the start script writes `$HEIMDALL_RUN_DIR/inst<N>.json` and `.env` (default `/run/heimdall`), holds an instance lock (`inst<N>.lock`, fd 9, inherited by the stages) and a startup mutex (`inst<N>.start`, fd 8). It records the directory in `_logs/inst<N>/pids/run_dir` |
| `instance_guard` | `legacy` | `legacy`: conflicts print `WARN:` and the port loop resolves them with the global stop. `refuse`: a duplicate start from another copy exits 3 (`--takeover` stops the other copy first); requires `instance_registry = 1` (`ini_checker` rejects it otherwise) |

When `/run/heimdall` is not writable, the registry falls back to `/tmp/heimdall-run-<uid>` and prints `WARN: run directory /run/heimdall is not usable; using /tmp/heimdall-run-<uid>`. When neither is usable it prints `WARN: no usable run directory (...); registry, instance lock and startup mutex disabled for this start` and starts without them. `flock` must be installed. Without it the script prints `WARN: flock not found; ...` and skips the lock.

Guard messages:

| Message | Guard | Meaning |
|---|---|---|
| `instance N is running from this copy - restarting` | `refuse` | the script runs `./daq_stop.sh N`, then takes the lock |
| `instance N is running from C - taking over (--takeover)` | `refuse` | runs `./daq_stop.sh N` in copy C |
| `ERROR: instance N is already running from C (use --takeover to stop it)` | `refuse` | exit 3 |
| `ERROR: another start of instance N is in progress` | `refuse` | exit 3 |
| `ERROR: instance lock of instance N is still held after 10 s - DAQ chain not started!` | `refuse` | exit 3 |
| `WARN: instance N is also running from C; today's behaviour applies: the port check stops all DAQ processes on this host` | `legacy` | the port check runs the global stop |
| `WARN: instance N is already running from this copy; the port check will stop it (today's behaviour)` | `legacy` | same |

The registry is a host-wide view only when every instance on the host sets `instance_registry = 1` and every start runs as the same uid (normally root). `heimdall-ctl instances` lists the entries. With the registry off it prints `no registry entries in <dir> (only instances started with [host] instance_registry = 1 are listed)`.

Under `instance_guard = refuse`, delay_sync checks that it inherited the instance lock. Without it, delay_sync logs `instance <N> started without its instance lock; refusing` and exits 3. The start script enables this check by exporting `HEIMDALL_INSTANCE_GUARD` and `HEIMDALL_INSTANCE_LOCK` when it opens the lock.

### 2.6 systemd

`packaging/systemd/` ships two `Type=oneshot` units. See `packaging/systemd/README.md` for installation.

| Unit | Working directory | ExecStop |
|---|---|---|
| `heimdall-daq.service` | `/opt/heimdall/Firmware` | `daq_stop.sh` with **no argument** (global stop) |
| `heimdall-daq@N.service` | `/opt/heimdall/instances/N/Firmware` | `daq_stop.sh N` |

Known issues of the current units:

| Issue | Effect | Workaround |
|---|---|---|
| `heimdall-daq.service` stops with the no-argument `daq_stop.sh` | `systemctl stop heimdall-daq` also kills every `heimdall-daq@N` instance | Do not run it beside the template units, or replace its `ExecStop` with the drop-in below |
| The units set no `PATH` | `python3` resolves to the system interpreter. If numpy/scipy/pyzmq live only in the conda env `kraken`, `delay_sync.log` and `hwc.log` show `ModuleNotFoundError` | `systemctl edit heimdall-daq` and add `Environment=PATH=<conda-root>/envs/kraken/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` under `[Service]` |
| logrotate globs cover only `/opt/heimdall/Firmware/_logs/` | logs under `/opt/heimdall/instances/N/Firmware/_logs/instN/` are never rotated | Add `/opt/heimdall/instances/*/Firmware/_logs/inst*/*.log` to `/etc/logrotate.d/heimdall` |

`ExecStop=` is a list setting. A drop-in that only adds a line appends it after the unit's global stop. Reset the list with an empty assignment first. Run `sudo systemctl edit heimdall-daq` and enter:

```ini
[Service]
ExecStop=
ExecStop=/opt/heimdall/Firmware/daq_stop.sh 0
```

`systemctl edit` reloads the unit. If you write the file by hand under `/etc/systemd/system/heimdall-daq.service.d/`, run `sudo systemctl daemon-reload`.

<!-- verify-after: WP-16 -->
**Planned units (WP-16, not yet in this tree).**

| Unit | Purpose |
|---|---|
| `heimdall-daq.service` | `ExecStop=daq_stop.sh 0`, `EnvironmentFile=-/etc/default/heimdall-daq` (holds `PATH`) |
| `heimdall-daq-supervised.service`, `heimdall-daq-supervised@.service` | `Type=simple`, runs `daq_supervise.sh --instance N --healthcheck auto`, `ExecStopPost=daq_stop.sh N --clean`, `Restart=on-failure`, `RestartPreventExitStatus=1 2 3 4`. Conflicts with the oneshot unit of the same instance |
| `heimdall-coordinator.service`, `heimdall-iq-router.service` | run as user `heimdall`; logs in `/var/log/heimdall/`; coordinator state in `/var/lib/heimdall/coordinator` |
| `heimdall-host-tuning.service` | oneshot `util/host_tuning.sh --apply` before the DAQ units |
| `heimdall-daq-watchdog.timer` | every 30 s runs `util/heimdall_watchdog.py`; restarts a `systemd-supervised` registry entry after 3 failed STATUS checks; optional, not enabled by install |

### 2.7 Supervisor (`daq_supervise.sh`)

`daq_supervise.sh` starts the chain of this copy and watches it. Run it from `Firmware/` as root.

```
./daq_supervise.sh [--instance N] [--synthetic] [--interval 2] [--healthcheck auto|on|off]
                   [--grace 60] [--stall-sec auto|SECONDS] [--allow-legacy-guard]
                   [-- START_SCRIPT_ARGS...]
```

It runs `./daq_start_sm.sh` (or `./daq_synthetic_start.sh` with `--synthetic`) and passes the arguments after `--` to it. The healthcheck is `off` by default; `on` needs `[monitoring] en_monitoring = 1` and `en_status_server = 1`. It exports `HEIMDALL_SCOPED_STOP=1`, so a busy-port round of the start script runs `./daq_stop.sh N` instead of the global stop. It watches the PID files every `--interval` seconds. The first failure stops the instance with `./daq_stop.sh N`. SIGTERM or SIGINT stop the instance too. Exit codes:

| Code | Meaning |
|---|---|
| 0 | stopped by SIGTERM or SIGINT |
| 1, 3, 4 | the start script's own exit code. 1 also when `daq_chain_config.ini` is missing or the start wrote no stage PID file |
| 2 | usage error, `--instance` differs from the INI, healthcheck `on` without the status server, or `instance_guard = legacy` on a multi-instance host (unless `--allow-legacy-guard`) |
| 10-15 | a stage died: 10 rtl_daq or synthesizer, 11 rebuffer, 12 decimate, 13 delay_sync, 14 hw_controller, 15 iq_server |
| 16 | the healthcheck failed 3 times in a row after the grace period |

---

## 3. Health checks

### 3.1 Enable the status endpoints

The status server and the event bus are **opt-in**. With the shipped config, `heimdall-ctl status` prints `Error: 127.0.0.1:5002: Connection refused` and exits 3. `heimdall-ctl hwc-status` works with every config, because hw_controller always serves `:5001`.

| Endpoint | Keys to set | Verbs |
|---|---|---|
| `:5002` STATUS, PING | `[monitoring] en_status_server = 1` | `PING`, `STATUS` |
| `:5002` EVENTS | also `en_monitoring = 1` (creates the event bus) | `EVENTS`, `EVENTS_DROPPED` |
| `:5002` METRICS | also `en_metrics = 1` | `METRICS` |
| `:5003` event stream | `en_monitoring = 1`, `en_zmq_pub = 1` | ZMQ PUB (`heimdall-ctl events --tail`) |
| `:5002` DB proxies | `[database] en_db = 1` plus the status server | `DB_STATS`, `SCAN_SUMMARY`, `CAL_HISTORY` |
| extended STATUS | `status_extended = 1` | adds `PEERS`, `HWC`, `HW_SNAPSHOTS` and extra STATUS keys |

A minimal monitoring block:

```ini
[monitoring]
en_monitoring = 1
en_status_server = 1
en_metrics = 1
; refresh STATUS every 10 frames instead of with the heartbeat (every 100 frames)
status_update_interval = 10
```

The STATUS snapshot is refreshed with the heartbeat by default (`heartbeat_interval = 100` frames, about 44 s at the shipped 1048576-sample CPI and 2.4 MS/s). Until the first refresh, STATUS has no `sync_state` and reports `pipeline_health: "error"`. Set `status_update_interval` to see changes sooner.

### 3.2 Health check commands

| Check | Command | Healthy result |
|---|---|---|
| Processes alive | `for f in _logs/inst0/pids/*.pid; do ps -o pid=,args= -p $(cat $f); done` | one line per PID file |
| Pipeline | `heimdall-ctl status` | `Pipeline: ok  Sync: 6` |
| hw_controller | `heimdall-ctl hwc-status` | `fsm IQ_CAL`, frame-age below a few frame periods, no `(stalled)` |
| Track lock | `heimdall-ctl track-lock` | `track lock: required=no approved=...` or `required=yes approved=yes` |
| Events | `heimdall-ctl events` | no recent `sync_lost`, `frame_drop`, `cal_timeout` |
| Ports | `heimdall-ctl --instance N ports` | the expected port per service |
| Fleet | `heimdall-ctl fleet status` | every member up, no `error` rows |
| Router | `heimdall-ctl router stats` | no evicted clients, no source gaps |

Example `hwc-status` output against a running chain:

```
$ heimdall-ctl hwc-status
fsm     pend  noise  agc  nch  freq         frame-age  frames  term
IQ_CAL  0     0      0    4    433.920 MHz  0.004s     0       0
```

| Column | HWSQ key | Meaning |
|---|---|---|
| `fsm` | `fsm` | hw_controller FSM: `INIT`, `GAIN_CTR_WAIT`, `GAIN_TUNE`, `IQ_CAL`, `NOISE_CTR_WAIT`. Mutating commands run only in `IQ_CAL` |
| `pend` | `pend` | requests waiting for the main loop (capped at 99) |
| `noise` | `ns` | noise source on (1) or off (0) |
| `agc` | `agc` | AGC enabled |
| `nch` | `nch` | channel count |
| `freq` | `f` | centre frequency |
| `frame-age` | `fage` | time since the last frame reached hw_controller; `-` before the first frame; `(stalled)` once it exceeds max(2 s, 3 × frame period) |
| `frames` | `fc` | frame counter modulo 1000000 |
| `term` | `t` | stored fencing term (0 unless an elected coordinator raised it) |

HWSQ is answered by the server thread within milliseconds, even when frames stall. It never waits for the main loop.

### 3.3 STATUS fields

`heimdall-ctl --json status` prints the raw document. Base keys (every config with the status server on):

| Key | Meaning |
|---|---|
| `sync_state` | delay_sync state: 0 DUMMY frame, 1 INIT, 2 sample delay cal, 3 fractional delay cal, 4 IQ cal, 5 track lock, 6 track |
| `calibration_status` | `locked` (sync_state ≥ 5), `calibrating` (2-4), `lost` (0-1) |
| `current_frequency_hz` | centre frequency of the last frame |
| `frame_count` | `cpi_index` of the last frame |
| `aggregate_power_db` | aggregate channel power (needs RF front-end telemetry, else 0) |
| `counters.dropped_frames_iq` / `dropped_frames_hwc` | cumulative frames delay_sync dropped toward the output and toward hw_controller |
| `counters.sync_fails_total` | cumulative failed track checks |
| `counters.sample_compensations` / `iq_compensations` | calibration corrections applied |
| `recent_drops`, `drop_window_sec` | drops in the last `drop_window_sec` seconds (default 60) |
| `pipeline_health` | see below |
| `uptime_sec`, `timestamp`, `instance_id` | server facts |
| `latency`, `throughput` | only with `en_metrics = 1` |

`pipeline_health` values:

| Value | Rule |
|---|---|
| `ok` | `sync_state ≥ 5` and no drops in the window |
| `degraded` | `sync_state ≥ 2` and not `ok`: calibrating, or locked with recent drops |
| `error` | `sync_state < 2`, including no snapshot yet |
| `stale` | only with `status_extended = 1`: the snapshot is older than `stale_after_sec` |

A DUMMY frame carries `sync_state = 0`. Each tuner command makes the source emit 5 DUMMY frames. A STATUS refresh that lands on one reports `error` once. Judge health over several readings.

<!-- verify-after: WP-06 -->
With `status_extended = 1` STATUS also carries `status_protocol`, `snapshot_age_sec`, `stale_after_sec`, identity (`node_id`, `cluster_id`, `hostname`, `pid`, `fw_version`, `config_sha256`), the frame geometry (`num_channels`, `sampling_freq_hz`, `cpi_length`, `decimation_ratio`, `frame_period_sec`), `fsm_state`, `cal` (`quality_db`, `recal_generation`, ...) and extra counters: `buffer_overrun_cnt`, `cpi_gaps`, `daq_block_gaps`, `overrun_invalidated_frames`, and `decimator_drops` when `stamp_decimator_drops = 1`. The automatic staleness threshold is `3 × max(1, k) × frame_period + 1` seconds, where `k` is the effective refresh interval in frames.

---

## 4. Logs

This section is the authoritative log legend. `Firmware/_logs/logs` holds a short copy for source trees.

### 4.1 Locations

| Path | Contents |
|---|---|
| `_logs/*.log` | instance 0 logs (flat layout; external tools read these paths) |
| `_logs/inst<N>/*.log` | instance N > 0 logs |
| `_logs/archive/<ISO time>/`, `_logs/inst<N>/archive/<ISO time>/` | the previous runs' logs, moved at each start; the last 10 archives are kept per instance |
| `_logs/inst<N>/pids/*.pid` | PID files for **every** instance, including `_logs/inst0/pids/` |
| `_testing/test_logs/` | unit-test logs, not runtime logs |
| start script stdout | terminal, or the journal under systemd (`journalctl -u heimdall-daq`) |
| `_logs/federation/coordinator.log`, `router.log` | coordinator and router started by `federation_start.sh` (`--log-dir`); started by hand they log to stderr or to the file given with `--log-file` |
| `_logs/federation/pids/*.pid` | PID files of `federation_start.sh` (`--pid-dir`) |
| `_logs/inst<N>/pids/run_dir` | the registry directory, written when `[host] instance_registry = 1` |

Each process writes its stderr to one file. The start script truncates nothing: it moves the old files into the archive first.

PID files:

| File | Process |
|---|---|
| `rtl_daq.pid` | `rtl_daq.out` (hardware mode; the process-group leader of the rtl_daq/rebuffer pipeline) |
| `synthesizer.pid` | `test_data_synthesizer.py` (synthetic mode) |
| `rebuffer.pid` | `rebuffer.out` |
| `decimate.pid` | `decimate.out` |
| `delay_sync.pid` | `delay_sync.py` |
| `hw_controller.pid` | the `sudo` wrapper of `hw_controller.py` |
| `iq_server.pid` | `iq_server.out` (only with `out_data_iface_type = eth`) |

`federation_start.sh` appends the service's stdout and stderr to its log file. <!-- verify-after: WP-16 --> Under the planned systemd units (WP-16) the federation logs go to `/var/log/heimdall/`.

### 4.2 Log files

| File | Written by | Typical content |
|---|---|---|
| `rtl_daq.log` | `_daq_core/rtl_daq.out` (hardware mode) | device open by serial, tuner settings, ZMQ commands (`Signal 'c'`, `'g'`, `'n'`, `'s'`, `'b'`), USB ring-buffer overruns, overdrive |
| `synthetic.log` | `_testing/test_data_synthesizer.py` (synthetic mode, replaces `rtl_daq.log`) | ZMQ responder bind, decoded commands, frame types |
| `rebuffer.log` | `_daq_core/rebuffer.out` | config load, frame index mismatches (lost input frames), transport errors |
| `decimator.log` | `_daq_core/decimate.out` | FIR coefficient file and tap count, engine selection, transport errors |
| `delay_sync.log` | `_daq_core/delay_sync.py` | calibration FSM, sync loss, wire recalibration, plus the in-process status server, event bus, metrics, federation health monitor and database |
| `hwc.log` | `_daq_core/hw_controller.py` | `:5001` commands, gain control, noise-source bursts, track-lock wait, scheduler, orientation, fencing |
| `iq_server.log` | `_daq_core/iq_server.out` (`eth` output only) | client connects, slow-consumer drops, send errors |
| `_logs/federation/coordinator.log` | `_daq_core/federation_coordinator.py` | effective settings, member state, election, fan-out errors |
| `_logs/federation/router.log` | `_daq_core/federation_iq_router.py` | source connects and gaps, client evictions, clock skew |

### 4.3 Log levels

`[daq] log_level` sets the level of the C binaries and of the Python stages' own loggers. The shipped value is `5`, which shows only fatal and critical lines.

| `log_level` | C binaries show | Python loggers show |
|---|---|---|
| 0 | TRACE and above | everything (level 0) |
| 1 | DEBUG and above | DEBUG (10) and above |
| 2 | INFO and above | INFO (20) and above |
| 3 | WARN and above | WARNING (30) and above |
| 4 | ERROR and above | ERROR (40) and above |
| 5 (shipped) | FATAL only | CRITICAL (50) only |

With `log_level = 5`, overrun warnings, `Correlation peak dynamic range is insufficient`, `Got command ...` and most other lines in this runbook are hidden. Set `log_level = 2` while troubleshooting and restart the chain.

Some lines bypass `log_level`. hw_controller and delay_sync configure the Python root logger at DEBUG, so calls made through the root logger always appear. They are written as `INFO:root:...`. Examples are `INFO:root:Waiting for track lock init. approval` and `INFO:root:User has approved track lock state initialization`. Python tracebacks also always appear.

Line formats:

| Source | Format | Example |
|---|---|---|
| C binaries | `HH:MM:SS LEVEL file.c:line: message` | `14:02:11 FATAL rtl_daq.c:998: The serial numbers of the devices are not yet configured ...` |
| delay_sync, hw_controller, synthesizer | `LEVEL:logger:message` (`__main__` for the stage itself, `root`, or the module name such as `daq_db`) | `WARNING:__main__:Got command FREQ` |
| ini_checker | `ERROR:root:<text>` for errors, `WARNING: <text>` for warnings (stderr) | `WARNING: [pre_processing] fft_workers is not read by any process; use [offload] fft_workers` |
| coordinator | `<asctime> [<logger>] LEVEL: message` | - |
| router | `<asctime> LEVEL <logger>: message` | - |

### 4.4 Key log lines

| Line (prefix) | File | Meaning | See |
|---|---|---|---|
| `The serial numbers of the devices are not yet configured (expected serial S for channel i; ...)` | rtl_daq | no attached receiver has serial S | [5](#5-troubleshooting-startup-failures) |
| `serial S is present on K devices; program distinct serials ...` | rtl_daq | duplicate EEPROM serials; FATAL when `serial_base` or `device_serials` is set, WARN otherwise | [5](#5-troubleshooting-startup-failures) |
| `Failed to open RTL-SDR device` | rtl_daq | the device is claimed by another process or instance | [5](#5-troubleshooting-startup-failures) |
| `Failed to identify control channel index based on its configured serial number` | rtl_daq | `[daq] ctr_channel_serial_no` matches no attached serial; noise source control falls back to device index 0 | [7](#7-troubleshooting-calibration-and-sync) |
| `ZMQ bind to ... failed ([daq] zmq_listen_address)` | rtl_daq | port `1130 + offset` is taken, or the address is invalid | [5](#5-troubleshooting-startup-failures) |
| `USB ring-buffer overrun (affected frame #n): ... - raise [daq] usb_ring_buffers (now k)` | rtl_daq | reader threads lapped the ring; channel coherence of that frame is not guaranteed | [8](#8-troubleshooting-data-plane) |
| `Likely race condition. Skipping data aquicision.` | rtl_daq | USB transfers arrive faster than the main loop consumes them | [8](#8-troubleshooting-data-plane) |
| `Overdrive detected, flags: 0x..` | rtl_daq | ADC clipping on the flagged channels | [7](#7-troubleshooting-calibration-and-sync) |
| `Frame index missmatch. Expected a <--> b Received` | rebuffer | input frames were lost between rtl_daq and rebuffer | [8](#8-troubleshooting-data-plane) |
| `FIR coefficients: <path> (<n> taps)` | decimator | coefficient file loaded (INFO) | - |
| `Failed to open FIR coefficient file <path>: cannot open coefficients` | decimator | designer did not run for this instance, or an old `decimate.out` reads the unprefixed name | [5](#5-troubleshooting-startup-failures) |
| `FIR filter coefficient file does not match the configured tap size` | decimator | the file was generated for another `fir_tap_size` | [5](#5-troubleshooting-startup-failures) |
| `Correlation peak dynamic range is insufficient to perform calibration` + `Real value: x, minimum: 20.00` | delay_sync | the noise-source correlation peak is below 20 dB | [7](#7-troubleshooting-calibration-and-sync) |
| `Sample sync may lost`, `IQ sync may lost` | delay_sync | one failed track check; `maximum_sync_fails` consecutive failures restart calibration | [7](#7-troubleshooting-calibration-and-sync) |
| `Center frequency changed, initiating recalibration` | delay_sync | a FREQ change restarts calibration | [12.1](#121-force-a-recalibration) |
| `Calibration timeout: n consecutive calibration frames ...` | delay_sync | opt-in `cal_timeout_frames` reached | [7](#7-troubleshooting-calibration-and-sync) |
| `Recalibration request (wire, epoch ..., generation g): re-entering STATE_INIT` | delay_sync | a RECL was applied | [12.1](#121-force-a-recalibration) |
| `Output interface (IQ server) initialization failed, exiting..` | delay_sync | the output FIFOs or shared memory could not be opened | [6](#6-troubleshooting-hung-or-idle-chain) |
| `DB schema version changed (x -> 2), discarding old databases` | delay_sync or hwc (`daq_db`) | old-generation telemetry was deleted | [11](#11-troubleshooting-database) |
| `Got command XXXX` | hwc (WARNING) | one `:5001` frame received | [9](#9-troubleshooting-control-plane) |
| `Control request X timed out waiting for the main loop` | hwc | the FSM was not in `IQ_CAL` within `ctl_request_timeout_sec` | [9](#9-troubleshooting-control-plane) |
| `Control request queue full, rejecting X` | hwc | the request queue stayed full for 2 s | [9](#9-troubleshooting-control-plane) |
| `Unidentified control command: XACK` | hwc of an **old** member | an old hw_controller received the extended-ack handshake; expected in mixed-version clusters | [9](#9-troubleshooting-control-plane) |
| `XACK from cluster 'a' refused (own cluster_id 'b')`, `XACK term t refused: jump above fence term ...` | hwc | fencing refusals | [12.3](#123-fencing-state) |
| `Refusing control request X from stale term t ...` | hwc | a deposed coordinator's command was refused at execution | [12.3](#123-fencing-state) |
| `Waiting for track lock init. approval` (root logger, every frame) | hwc | `require_track_lock_intervention = 1` and no approval yet | [12.2](#122-track-lock-approval) |
| `Cannot read track lock approval file ...; treating it as not approved` | hwc | the approval file is missing (logged once) | [12.2](#122-track-lock-approval) |
| `Calibration stalled: n frames with the noise source on, ...` | hwc | opt-in `cal_stall_frames` reached | [7](#7-troubleshooting-calibration-and-sync) |
| `rtl_daq control transaction failed (...), rebuilding socket` | hwc, delay_sync | no reply on ZMQ `1130 + offset` | [6](#6-troubleshooting-hung-or-idle-chain) |
| `IQ frame send exceeded 10 s deadline: dropping slow consumer` | iq_server | a `:5000` client stopped reading | [8](#8-troubleshooting-data-plane) |
| `IQ client <peer> missed the <n> ms frame deadline: closing session` | iq_server | multi-client mode: a client fell behind the frame deadline | [8](#8-troubleshooting-data-plane) |
| `Control responder on port P unavailable (...)` | synthetic | the synthesizer could not bind its ZMQ port; the chain cannot calibrate | [5](#5-troubleshooting-startup-failures) |

---

## 5. Troubleshooting: startup failures

| Symptom | Cause | Fix |
|---|---|---|
| `Config file check [ FAIL ]` (`[FAIL]` from the synthetic script) and `ERROR:root:...` lines | `ini_checker.py` found errors | Run `python3 ini_checker.py no_hw attached` (synthetic: `no_hw`) and fix each line. Lines starting `WARNING: ` never block a start |
| `daq_common.sh not found next to <script> - DAQ chain not started!`, exit 1 (or `ERROR:root:DAQ core configuration file not found.` from a direct `ini_checker.py` run) | the script was not started from `Firmware/` | `cd` into the copy's `Firmware/` directory first |
| `ERROR: Ports (5000, 5001 & 5002) still busy after 10 attempts - DAQ chain not started!` | another process listens on one of the three ports (only listening sockets count) | The script prints the listener lines. Or find it: `sudo ss -ltnp 'sport = :5001'`, `sudo lsof -nP -iTCP:5001 -sTCP:LISTEN`. Stop the owner, or give this instance another `instance_id` |
| A start stopped other instances on the host | another instance of this host listens on one of the three ports, and each busy round runs the **global** `./daq_stop.sh` (default `legacy` guard) | Stop the instance first with `./daq_stop.sh N`, or start with `HEIMDALL_SCOPED_STOP=1` in the environment, or set `[host] instance_guard = refuse` ([2.3](#23-restart)) |
| `ERROR: FIR tap size must be higher than the decimation ratio...` (stdout) | `fir_tap_size <= decimation_ratio` with `decimation_ratio > 1` | Raise `[pre_processing] fir_tap_size` |
| `rtl_daq.log`: `The serial numbers of the devices are not yet configured (expected serial 1000 for channel 0; ...)` | no receiver with that serial is attached, or the receivers still have factory serials | Check `python3 _daq_core/hw_discover.py --sdr`. Program serials (see [hardware_setup.md](hardware_setup.md)) or set `[hw] serial_base` / `device_serials` to the attached serials |
| `rtl_daq.log`: `serial S is present on 2 devices` (FATAL) | two attached receivers carry the same serial and serial keys are set | Program distinct serials, one unit at a time. Without serial keys this is a WARN and rtl_daq opens the first match |
| `rtl_daq.log`: `Failed to open RTL-SDR device` on instance N > 0 | instance N resolves the same serials as another instance (both default to `1000 + i`) | Give each instance its own `serial_base` or `device_serials` and matching receivers |
| `rtl_daq.log`: `ZMQ bind to tcp://*:1130 failed ([daq] zmq_listen_address)` | another instance or a stale rtl_daq holds `1130 + offset` | `sudo ss -ltnp \| grep 1130`; stop the stale process |
| `decimator.log`: `Failed to open FIR coefficient file _data_control/inst<N>_fir_coeffs.txt` | `fir_filter_designer.py` did not run in this copy for this instance | Start through the start script, which runs the designer |
| `decimator.log`: `Failed to open FIR coefficient file _data_control/fir_coeffs.txt` in an instance directory for N > 0 | an old `decimate.out` (C not rebuilt) reads the unprefixed name | Rebuild: `cd _daq_core && make` |
| `decimator.log`: `FIR filter coefficient file does not match the configured tap size` | the file was written for another tap size | Rerun the start script (it regenerates the file); do not edit `fir_tap_size` while running |
| A log shows `chrt: failed to execute _daq_core/rtl_daq.out: No such file or directory` | the C binaries were not built | `cd _daq_core && make` |
| A log shows `chrt: failed to set pid ...'s policy: Operation not permitted` | the script ran without root | Start with `sudo` |
| `delay_sync.log` or `hwc.log`: `ModuleNotFoundError: No module named 'numpy'` (or `zmq`, `scipy`) | the interpreter on `PATH` is not the `kraken` env; under sudo or systemd, `PATH` is reset | `sudo` resets `PATH` to its `secure_path`. Start from a shell where the env is active with `sudo env "PATH=$PATH" ./daq_start_sm.sh`, or set `PATH` in the unit ([2.6](#26-systemd)). |
| `synthetic.log`: `Control responder on port 1130 unavailable (...)` | the synthesizer could not bind `1130 + offset` (pyzmq missing, or the port is taken) | Install pyzmq in the env; stop the process holding the port |
| Chain exits after 10000 DAQ blocks (about 2500 frames at the shipped sizes) in synthetic mode | the synthesizer's default `--blocks 10000` | Expected. For an unbounded run: `sudo ./daq_synthetic_start.sh --blocks 0` |
| `WARN: K cores for M instances - RT processes of multiple instances will share cores (...)` | instance N > 0 without `[host] cpu_cores` on a host with fewer than `4·(N+1)` cores | Set distinct `[host] cpu_cores` per instance ([2.1](#21-start-one-instance)), or accept the sharing |
| `ERROR: cores X of instance N are used by instance M running from C ([host] allow_core_sharing = refuse) - DAQ chain not started!`, exit 3 | the registry shows a live instance on the same cores | Give each instance its own `[host] cpu_cores`, or set `allow_core_sharing = warn` (prints `WARN: cores X of instance N are also used by ...`) or `allow` |

With the opt-in registry and guard ([2.5](#25-registry-instance-lock-and-duplicate-guard-opt-in)) and pre-flight:

| Symptom | Cause | Fix |
|---|---|---|
| `instance N is already running from C (use --takeover to stop it)`, exit 3 | `instance_guard = refuse` and another copy runs instance N | Stop it from that copy, or start with `--takeover` |
| `ERROR: another start of instance N is in progress`, exit 3 | two concurrent starts under `refuse` | Wait for the first start |
| `WARN: instance N is also running from C; today's behaviour applies ...` | `legacy` guard with the registry on | Expected warning; the port check stops the other chain with the global stop |
| `WARN: run directory /run/heimdall is not usable; using /tmp/heimdall-run-<uid>` | `/run/heimdall` is not writable for this uid | Start as root, or set `HEIMDALL_RUN_DIR` to a writable directory for every start on the host |
| `ERROR: preflight failed ([host] preflight = strict) - DAQ chain not started!`, exit 4 | `[host] preflight = strict` and `daq_preflight.py` reported failures (its exit 2) | Run `heimdall-ctl capacity` or `python3 daq_preflight.py --config daq_chain_config.ini --plus-live` and fix the reported resource |

---

## 6. Troubleshooting: hung or idle chain

| Symptom | Cause | Fix |
|---|---|---|
| All processes run, logs are silent, `heimdall-ctl hwc-status` shows `fsm INIT` and frame-age `-` | `out_data_iface_type = shmem` (the shipped value): delay_sync blocks opening `_data_control/<P>fw_delay_sync_iq` until a consumer opens it for reading. hw_controller then blocks waiting for delay_sync | Start the consumer (for example the DoA software), or set `[data_interface] out_data_iface_type = eth` to serve `:5000` instead. See [consumer_guide.md](consumer_guide.md) |
| `fsm INIT`, frame-age `-` with `eth` output | no frames leave rtl_daq: a stage before delay_sync died or the receivers deliver nothing | Check every PID file ([3.2](#32-health-check-commands)); read `rtl_daq.log`, `rebuffer.log`, `decimator.log` |
| frame-age grows and `(stalled)` appears after frames had flowed | the USB stream stopped (unplugged receiver, hub reset, power) or a stage died | `dmesg \| tail`, `lsusb`; check PID files; restart the instance |
| `hwc.log`: `IQ frame wait timed out, retrying..` | no frame reached hw_controller within its read timeout | as above |
| `hwc.log` or `delay_sync.log`: `rtl_daq control transaction failed (...), rebuilding socket` | rtl_daq (or the synthesizer) does not answer ZMQ on `1130 + offset` | Check that rtl_daq runs and bound its port (`ss -ltnp \| grep 1130`). In synthetic mode check `synthetic.log` for the responder bind |
| Mutating commands time out while frames flow | the FSM never reaches `IQ_CAL` on a non-dummy frame (for example gain tuning loops in `GAIN_TUNE`) | `heimdall-ctl hwc-status` shows the FSM; with `log_level = 2` `hwc.log` shows the gain loop |
| Instance N > 0 hw_controller exits at start with `Failed to open control fifos, exiting..` (old trees) | the old hw_controller required `_data_control/inst<N>_iq_track_lock` | Fixed in this release: hw_controller no longer opens the file at init, and the start scripts create it. On an old tree, `printf 0 > _data_control/inst<N>_iq_track_lock` |
| `delay_sync.log`: `Output interface (IQ server) initialization failed, exiting..` | the FIFOs are missing (start not run from this copy) or a stale `/dev/shm` segment belongs to another user | Restart through the start script. List stale segments with `ls -l /dev/shm/ \| grep -E 'decimator\|delay_sync'` |

---

## 7. Troubleshooting: calibration and sync

Calibration needs the internal noise source on the control channel. delay_sync moves through `sync_state` 1 to 4 on CAL frames and reaches 5 (track lock) and 6 (track). hw_controller turns the noise source off once `sync_state ≥ 5`. With `cal_track_mode = 2` (shipped) it then sends a burst of `cal_frame_burst_size` CAL frames every `cal_frame_interval` frames. See [theory_coherent_acquisition.md](theory_coherent_acquisition.md).

| Symptom | Cause | Fix |
|---|---|---|
| Stuck at `sync_state` 2 or 3; `delay_sync.log` (log_level ≤ 3): `Correlation peak dynamic range is insufficient to perform calibration`, `Real value: x, minimum: 20.00` | the noise-source correlation peak is below the fixed 20 dB threshold: noise source not reaching all channels, a strong external signal, or gains too low or overdriven | Check that `hwc-status` shows `noise 1` during calibration. Check `[daq] en_noise_source_ctr = 1`. Check `rtl_daq.log` for overdrive. Adjust `[daq] gain`. Disconnect or switch the antennas while calibrating |
| Noise source never turns on; `rtl_daq.log`: `Failed to identify control channel index ...` | `[daq] ctr_channel_serial_no` is not the serial of the noise-source unit | Set it to that unit's channel-0 serial (for `serial_base = 1100` that is 1100). `ini_checker` reports a mismatch when serial keys are set |
| Locked chain never leaves track although sync is clearly lost; `ini_checker` warns `cal_frame_burst_size = b is below maximum_sync_fails = m with cal_track_mode = 2` | DUMMY frames reset the fail counter at each burst, so `m` failures never accumulate inside a burst of `b < m` frames | Keep `cal_frame_burst_size ≥ maximum_sync_fails` (shipped: 10 and 10) |
| Repeated relocks; `Sample sync may lost` or `IQ sync may lost` | track checks fail; after `maximum_sync_fails` consecutive failures delay_sync restarts at INIT and emits `sync_lost` | Look for temperature drift, a loose cable or interference. Tolerances are `[calibration] amplitude_tolerance` and `phase_tolerance` |
| `hwc.log`: `INFO:root:Waiting for track lock init. approval` every frame; noise source stays on at `sync_state 5` | `[calibration] require_track_lock_intervention = 1` | Approve ([12.2](#122-track-lock-approval)) |
| `ADC overdriven at channel: m` or `Overdrive detected` | input too strong | Lower the gain, or enable `en_gain_tune_init` |
| The chain recalibrates after every tune | expected: a FREQ change restarts calibration (`Center frequency changed, initiating recalibration`) | - |
| A GAIN change on a locked chain does not recalibrate | expected by default | Opt-in: `[calibration] en_wire_recal = 1` and `recal_on_gain_change = 1` |

Opt-in calibration diagnostics. They log, emit an event and record a DB cal event when the DB is on. They take no hardware action.

| Key | Default | When enabled |
|---|---|---|
| `[calibration] cal_timeout_frames = k` | 0 (off) | after k consecutive CAL frames in the calibration states: one `Calibration timeout` WARNING and event `cal_timeout` `{state, frames}` |
| `[calibration] cal_stall_frames = k` | 0 (off) | after k non-dummy frames with the noise source on and `sync_state < 5`: one `Calibration stalled` WARNING, event `cal_stalled`, and `cal_stalled: true` in the hwc relay document |
| `[calibration] invalidate_on_overrun = 1` | 0 | clears the sync flags on a frame whose overrun counter (slot 102) rose, and on the next `frames_after_overrun` frames (default 1) |
| `[calibration] amplitude_tolerance_mode = symmetric` | `legacy` | compares the absolute dB amplitude difference with `amplitude_tolerance` in both directions |

---

## 8. Troubleshooting: data plane

The data plane runs in drop mode. A slow consumer loses frames. It never stalls the producer.

| Symptom | Where to see it | Cause | Fix |
|---|---|---|---|
| USB ring-buffer overruns | `rtl_daq.log` (log_level ≤ 3; first 5, then every 100th); header slot 102 `IQH_RSV_BUFFER_OVERRUN_CNT` on every frame; extended STATUS `counters.buffer_overrun_cnt` | rtl_daq's main loop fell behind its USB reader threads; that frame's channels may come from different USB epochs | Raise `[daq] usb_ring_buffers` (default 8, range 2-64). Check CPU load and core sharing, and USB bus bandwidth ([capacity_planning.md](capacity_planning.md)). Opt-in: `invalidate_on_overrun = 1` marks affected frames unsynced |
| `Frame index missmatch` | `rebuffer.log` | frames were lost before rebuffer | same as overruns; also check that rebuffer's core is not shared |
| Decimator drops | header slot 109 with `[monitoring] stamp_decimator_drops = 1`; extended STATUS `counters.decimator_drops` | delay_sync did not free its input buffer in time | Reduce delay_sync load (see [capacity_planning.md](capacity_planning.md)); check `fft_workers`, `track_decimation` |
| Output drops (`dropped_frames_iq`) | STATUS `counters.dropped_frames_iq`, `recent_drops`; event `frame_drop` `{target: iq_server}` | the consumer (iq_server, its client, or the shmem consumer) did not free a buffer in time | Expected while no client is connected. Otherwise speed up the consumer |
| hw_controller drops (`dropped_frames_hwc`) | STATUS `counters.dropped_frames_hwc`; event `frame_drop` `{target: hwc}` | hw_controller's main loop is slow (blocking hardware calls) | Check `hwc.log` for slow handlers |
| `IQ frame send exceeded 10 s deadline: dropping slow consumer` | `iq_server.log` | a `:5000` client stopped reading | Fix the client; it may reconnect |
| `IQ client <peer> missed the <n> ms frame deadline: closing session` | `iq_server.log` | multi-client mode (`iq_server_max_clients > 1`): the client is slower than the frame deadline | Speed up the client, or set a larger explicit `iq_server_frame_deadline_ms` |
| `pipeline_health: degraded` while `sync_state` is 5 or 6 | STATUS | drops within the last `drop_window_sec` | Find which counter rises |

delay_sync does not log individual drops (`ignore_frame_drop_warning` is on). Use the counters and `frame_drop` events.

<!-- verify-after: WP-04 -->
Opt-in data-plane keys from this release: `[data_interface] iq_server_max_clients` (default 1) serves several `:5000` clients, each with its own queue. `iq_server_frame_deadline_ms` (default 0 = automatic: 0.8 × the frame period, at least 100 ms; multi-client mode only) closes a client that misses it (`IQ client <peer> missed the <n> ms frame deadline: closing session`). Without a sample rate the automatic deadline is the 10 s send bound. `[pre_processing] size_segments_from_config = 1` sizes the decimator and iq_server shared memory from the config.

---

## 9. Troubleshooting: control plane

hw_controller serves `:5001` with one connection at a time by default (`[daq] ctl_max_connections = 1`). A resident client, such as a DoA application, holds that connection. Other clients wait in the listen backlog.

| Symptom | Cause | Fix |
|---|---|---|
| `heimdall-ctl` exits 3: `server did not answer XACK within 3 s (another client may hold the control port); command not sent - retry, or use --ack legacy to queue it behind that client` | another client holds the single `:5001` connection | Opt-in: `[daq] ctl_max_connections = 4`. Or use `--ack legacy` to queue the command (it may run late, see below) |
| A command sent with `--ack legacy` ran long after the client reported a timeout | without XACK, a frame waiting in the backlog executes when the server reaches it | Use the default `--ack auto`; raise `ctl_max_connections` |
| `NOT applied: deferred_timeout (fsm=...)` | the FSM was not in `IQ_CAL` within `ctl_request_timeout_sec` (default 10 s). The command was withdrawn and never runs | Retry when `hwc-status` shows `IQ_CAL`. The frame period must be well below the timeout; `ini_checker` warns when `ctl_request_timeout_sec` is below 2 × the frame period |
| `held until the calibration burst ends` | opt-in `defer_control_during_burst = 1`, or GAIN/AGC during a noise burst; the command runs when the burst ends | Expected; exit code 0 |
| `NOT applied: not_confirmed` | rtl_daq did not acknowledge the ZMQ message (for FREQ: `FREQ change to F Hz not confirmed by rtl_daq` in `hwc.log`) | Check rtl_daq is alive and `1130 + offset` answers |
| `outcome unknown: handler_timeout` | the handler ran past `ctl_handler_max_sec` (default 12 s) | Check `hwc.log` for the slow handler |
| `NOT applied: queue_full`; query verbs reply `{"error":"busy"}` | the request queue stayed full for 2 s, or a query waited past the request timeout | Reduce command rate; check the FSM |
| Legacy (no XACK) connection gets `FNSD` + zeros after about 10 s | the legacy contract: on timeout the server sends the plain ack whatever happened | Use `--verify` to confirm the effect |
| `Error: this server does not support TLKA (legacy hw_controller); command not sent` | the server is an old hw_controller | Approve by file ([12.2](#122-track-lock-approval)) |
| `hwc.log` on an old member: `Unidentified control command: XACK` | heimdall-ctl or a coordinator sent the extended-ack handshake to an old hw_controller | Expected in mixed-version clusters. heimdall-ctl caches the legacy result for 24 h, so it appears once per user per day; a coordinator sends XACK before every command |

Verify the effect of a command:

```bash
heimdall-ctl --verify tune 433920000   # polls HWSQ f, else STATUS current_frequency_hz
heimdall-ctl hwc-status                # FSM, frequency, noise source, pending requests
```

Example outputs from this release:

```
$ heimdall-ctl tune 433920000
applied (fsm=IQ_CAL, 4 ms)
$ heimdall-ctl --verify tune 123000000
applied (fsm=IQ_CAL, 12 ms)
verified: frequency 123.000 MHz
$ heimdall-ctl schedule load big.json
applied: 20 entries (chunked, 8 chunks)
```

Against an old hw_controller a mutating command prints `sent; server does not confirm application (legacy server)` and exits 0.

Connection keys (opt-in, `[daq]`):

| Key | Default | Effect |
|---|---|---|
| `ctl_max_connections` | 1 | 1 keeps the sequential accept loop and `listen(4)`; > 1 (max 64) serves clients concurrently with `listen(16)` |
| `ctl_request_timeout_sec` | 10.0 | how long a mutating request waits for the main loop (1-600) |
| `ctl_handler_max_sec` | 12.0 | extra wait for a request already running (1-120) |
| `ctl_xack_idle_sec` | 30 | closes idle connections that sent XACK |
| `ctl_idle_timeout_sec` | 0 (off) | closes any idle connection |

---

## 10. Troubleshooting: federation, coordinator and router

Federation is opt-in (`[federation] en_federation = 1`, membership keys, a coordinator process). See [scaling_and_clustering.md](scaling_and_clustering.md) for the model.

### 10.1 Peer health

Each instance with federation enabled polls its peers' status servers from delay_sync. Events (with `en_monitoring = 1`):

| Event | Emitted when |
|---|---|
| `peer_up`, `peer_down` | a peer becomes reachable or unreachable |
| `coordinator_elected` | the lowest healthy instance id changed (advisory only) |
| `peer_degraded`, `peer_recovered`, `peer_flapping` | only with `[federation] emit_peer_health_events = 1` |

| Symptom | Cause | Fix |
|---|---|---|
| `peer_down` for a running peer | the peer's status server is off or unreachable, or the port is wrong | Enable `en_status_server` on every member; check `heimdall-ctl --host <peer> --instance N status`; check firewalls ([networking_and_time_sync.md](networking_and_time_sync.md)) |
| a member shows `stale` | its STATUS answers but `frame_count` stopped changing, or extended STATUS reports `stale` | The member's chain is stalled ([6](#6-troubleshooting-hung-or-idle-chain)) |
| `delay_sync.log`: `cluster_members does not contain this instance` | this instance's `host:N` is missing from `cluster_members` | Add it; use real host names, no loopback |

### 10.2 Coordinator errors

<!-- verify-after: WP-08 -->
`heimdall-ctl fleet ...` talks to the coordinator on `:6000`. Error replies print `Error: <error>` and exit 1, except `not_leader` (exit 4) and connection failures (exit 3).

| Error | Cause | Fix |
|---|---|---|
| `not_leader` | this coordinator is a follower; the reply names the leader. heimdall-ctl follows one redirect | List every candidate in `--coordinator H:P,H:P,...` |
| `no_quorum` | `require_quorum = 1` and this leader cannot reach a majority of candidates | Restore connectivity; quorum needs at least 3 candidates |
| `busy` (`retry_in_sec`) | another mutating request holds the lock longer than `mutation_wait_sec`, or the handler pool is full | Retry after the given delay |
| `auth_required` | `auth_token` is set and the request lacks it | Pass `--auth-token` or `HEIMDALL_AUTH_TOKEN` |
| `no master schedule`, `no federation scheduler configured` (REBALANCE) | no master schedule was set on this coordinator | `heimdall-ctl fleet schedule set FILE`, then `heimdall-ctl fleet rebalance` |
| `health_warming` (`retry_in_sec`) | the coordinator has not yet completed a health poll | Retry after the given delay |
| `no healthy instances` | every member is down or degraded | Fix the members; `heimdall-ctl fleet rebalance --include-degraded` also targets degraded members |
| `duplicate_node_key` | a REGISTER claims a `host:N` key already held from another address; event `node_key_collision` | Fix the duplicate `instance_id` or `advertise_host` on one of the two members |
| `cluster_mismatch` | the member's `[federation] cluster_id` differs from the coordinator's | Use one `cluster_id` per cluster |
| per-target `reason: stale_term` | the member has seen a higher term; an elected coordinator steps down and does not send | Expected after a failover. After a coordinator `state_dir` wipe, the next election rises above the members' stored term |
| per-target `reason: term_jump` | the coordinator's term exceeds the member's stored term by more than `fence_max_term_jump` (1000) | See [12.3](#123-fencing-state) |
| per-target `reason: breaker_open` | the member failed repeatedly; the circuit breaker pauses it | Fix the member; it is probed again after `retry_in_sec` |
| per-target `error: timeout`, `phase: xack` | the member's `:5001` did not answer XACK (a resident client holds it) | Set `ctl_max_connections = 4` on every member |
| `router_not_configured`, `router_unreachable` | `fleet router-stats` without a reachable router | Check the router endpoint |

A coordinator talking to an old member logs once per member: `member K is a legacy hw_controller (no XACK); it logs 'Unidentified control command: XACK' per coordinator command`. In static mode (no `coordinator_candidates`) it logs `member K has fence term T from an elected coordinator; static mode ignores it` and sends the command anyway.

### 10.3 IQ router

<!-- verify-after: WP-09 -->
`heimdall-ctl router stats|sources|clients` queries the router stats port (default 7001, `--router H:P`). The stats port answers `PING`, `VERSION`, `STATS`, `SOURCES`, `CLIENTS`.

| Symptom (router log) | Cause | Fix |
|---|---|---|
| `IQ Router: client <peer> evicted (...)` | a client let its queue overflow `--client-stall-limit` times (default 50) or accepted no byte for `--send-timeout-sec` (2.0 s) | Fix the client. Raise `--queue-frames` (default 4) only if memory allows |
| `IQ Router: client <peer> refused (max_clients n reached)` | `--max-clients` (default 8) reached | Raise it or reduce clients |
| `IQ Router: source <ep>: <reason> (retry in s)` | a source iq_server is down or refused; reconnect delay doubles up to 30 s | Check the source instance; with `iq_server_max_clients = 1` another client may hold its `:5000` |
| `IQ Router: source <ep> cpi_index gap a -> b` | frames lost between the source and the router | Check network and the source's drop counters |
| `IQ Router: source <ep> clock offset ... deviates ...` | host clocks differ by more than `--max-skew-ms` (500) | Run NTP or PTP ([networking_and_time_sync.md](networking_and_time_sync.md)) |
| `upstream router ... does not tag sources` | a chained upstream router runs without `--tag-mode node` | Restart the upstream router with `--tag-mode node` |
| router exits 2 with `cannot bind output port ... or stats port ...` | the port is in use | Free the port or change `--output-port` / `--stats-port` |

---

## 11. Troubleshooting: database

The database is opt-in (`[database] en_db = 1`, needs the `berkeleydb` Python module). It lives in `db_dir` (default `_db`). It is rolling telemetry: records older than `rotation_max_age_hours` (168) are removed and the total is capped at `max_db_size_mb` (500).

| Symptom | Cause | Fix |
|---|---|---|
| `DB schema version changed (1 -> 2), discarding old databases` | the on-disk schema generation differs; a read-write open deletes the old `*.db`, `__db.*` and `log.*` files | Expected once after an upgrade that changes the generation. Back up first if you need the data (below) |
| `RuntimeError: DAQ database at <dir> has schema version x (expected 2)` from a reader | a read-only open never discards | Upgrade the reader or let a read-write open migrate |
| `BerkeleyDB Python bindings not available` | `berkeleydb` is not installed in the env | Install it, or set `en_db = 0` |
| `DB write queue full, dropping record`; event `db_queue_full` | the writer thread falls behind the frame rate; records are dropped instead of blocking the chain | Check the storage speed of `db_dir`; tune `write_batch_size` and `write_flush_interval_sec` |
| two instances write one directory | every instance uses `db_dir` verbatim by default | Opt-in: `[database] instance_subdir = 1` (instance N > 0 uses `<db_dir>/inst<N>`). Provisioned instance directories have a private `_db` |

There is no built-in backup. To keep telemetry, stop the instance and copy the whole `db_dir` (all files, including `_schema_version`):

```bash
sudo ./daq_stop.sh 0
sudo tar -C _db -czf ~/heimdall-db-$(date +%F).tgz .
```

Opt-in `[database] write_instance_meta = 1` writes `<db dir>/_instance_meta.json` (instance id, node id, host, pid, open time) at every read-write open.

---

## 12. Procedures

### 12.1 Force a recalibration

`heimdall-ctl recal` sends `RECL` when the server advertises it, else `INIT`.

| Server | What `recal` does | Output |
|---|---|---|
| `[calibration] en_wire_recal = 1` (opt-in) | hw_controller writes `_data_control/<P>recal_request`, emits `recal_requested` and re-enters INIT; delay_sync restarts calibration from `STATE_INIT`, also on a locked chain | `applied (fsm=IQ_CAL, 412 ms)` |
| default (`en_wire_recal = 0`) | sends `INIT` with a warning; INIT only re-enters the hw_controller FSM and **does not recalibrate a locked chain** | `warning: server has wire recalibration disabled or lacks RECL: INIT only re-enters the hw_controller FSM and does not recalibrate a locked chain` |

On a default server, a frequency round trip forces a recalibration, because a locked delay_sync restarts calibration on any centre-frequency change. It retunes the receivers twice. The second value must be the current frequency F, in Hz. Read F from the `freq` column of `heimdall-ctl hwc-status` (for example `700.000 MHz` is 700000000), then:

```bash
heimdall-ctl tune <F+100000> && heimdall-ctl tune <F>
```

`heimdall-ctl --verify recal` waits until extended STATUS `sync_state` drops below 5 and returns to 5 or more.

### 12.2 Track-lock approval

Use this when the chain must not leave calibration until an operator confirms, for example when antennas are detached for calibration.

1. Set `[calibration] require_track_lock_intervention = 1` and start the chain.
2. Wait for `sync_state 5`. `hwc.log` repeats `INFO:root:Waiting for track lock init. approval` and the noise source stays on.
3. Reattach the antennas.
4. Approve:

   ```bash
   heimdall-ctl track-lock approve        # TLKA over :5001
   heimdall-ctl track-lock                # prints: track lock: required=yes approved=yes
   ```

   Or write the file directly (instance N > 0 uses `_data_control/inst<N>_iq_track_lock`):

   ```bash
   echo 1 | sudo tee _data_control/iq_track_lock
   ```

5. `hwc.log` shows `INFO:root:User has approved track lock state initialization` and the noise source turns off.
6. Revoke the approval once it has taken effect, or at the latest before the next start or recalibration:

   ```bash
   heimdall-ctl track-lock revoke         # writes 0; the next calibration waits again
   ```

The approval persists. The start scripts create the file only when it is absent, and `daq_stop.sh N --clean` keeps it, so a restart does not reset it. A file left at `1` lets every later relock and restart pass the gate without waiting.

`heimdall-ctl track-lock revoke` writes `0`. The file is re-read on every check, so in-place writes and atomic replacement both work. A missing file means "not approved" and is logged once.

### 12.3 Fencing state

An XACK from an elected coordinator carrying this member's `[federation] cluster_id` raises `_data_control/<P>fence_term`. hw_controller then refuses mutating requests from connections with a lower non-zero term (`stale_term`). heimdall-ctl, DoA clients and static coordinators send term 0 and are never fenced. The file survives restarts.

| Situation | Symptom | Recovery |
|---|---|---|
| Normal failover | old leader's commands get `stale_term` | none; expected |
| Coordinator `state_dir` wiped, elected mode | first commands get `stale_term`; the coordinator adopts the member's term and steps down | none; the next election rises above the stored term |
| Coordinator state wiped and terms must restart from 0 | XACK refused with `term_jump` (`XACK term t refused: jump above fence term ...`) or every command `stale_term` | `sudo ./daq_stop.sh N --reset-fence` on the member, then start it |
| Term space exhausted | coordinator log: `term space exhausted; run daq_stop.sh N --reset-fence on every member and wipe the coordinator state_dir` | as the message says, on every member |

Reset the fencing state with the stop script ([2.2](#22-stop)). It stops instance N first and refuses (exit 3) while a process of N survives:

```bash
sudo ./daq_stop.sh N --reset-fence
# fencing state of instance N reset; the next coordinator XACK sets it again
```

On an old tree without `--reset-fence`, stop the instance and remove the file by hand: `sudo rm _data_control/inst<N>_fence_term` (instance 0: `_data_control/fence_term`).

Do not delete `fence_term` as routine cleanup. `daq_stop.sh N --clean` deliberately keeps it so that supervised restarts stay fenced.

### 12.4 heimdall-ctl ack-mode cache

With the default `--ack auto`, heimdall-ctl sends XACK before the first mutating command to a `host:port` and caches the outcome in `$XDG_CACHE_HOME/heimdall-ctl/ack_modes.json` (default `~/.cache/heimdall-ctl/ack_modes.json`) for 24 h.

| Need | Action |
|---|---|
| A server was upgraded but heimdall-ctl still treats it as legacy | run one command with `--ack xack` (it re-probes and refreshes the cache), or delete the file |
| Queue a command behind a resident client | `--ack legacy` (alias `--legacy-ack`); prints `warning: without XACK a command that waits behind another client may execute after this command reports a timeout` |
| See the resolved ports, timeouts and ack mode | add `-v` |

---

## 13. Upgrades

See [upgrade_and_rollback.md](upgrade_and_rollback.md) for the full procedure. The points that cause most failures:

- **Rebuild the C binaries on every update** (`cd Firmware/_daq_core && make`). A Python-only update leaves an old `decimate.out`. For instance N > 0 the new designer writes `inst<N>_fir_coeffs.txt`, and in a separate instance directory it removes the stale unprefixed file, so an old decimator fails with `cannot open coefficients`.
- Stop every instance before swapping the tree. On a multi-instance host stop each with `daq_stop.sh N`.
- A DB schema generation change discards the old telemetry on the first read-write open ([11](#11-troubleshooting-database)).
- Mixed-version clusters work. Old members log `Unidentified control command: XACK` for each coordinator command; that is expected.
- `version.py` holds `FW_VERSION` (`1.1.0-dev` in this tree). Extended STATUS reports it as `fw_version`.

---

## 14. Data to collect for a bug report

Collect these before restarting the chain, because the next start moves the logs into the archive.

| Item | Command |
|---|---|
| Firmware version and tree | `git -C .. rev-parse HEAD`, `grep FW_VERSION _daq_core/version.py`, `heimdall-ctl --version` |
| Host | `uname -a`, `nproc`, `cat /etc/os-release` |
| Config | `daq_chain_config.ini` of the instance (and `federation.ini`). Remove or mask `[federation] auth_token` first |
| Config check | `python3 ini_checker.py no_hw attached` (stdout and stderr) |
| Logs | the whole `_logs/` (or `_logs/inst<N>/`) directory including `archive/` |
| Processes | `ps -eo pid,ni,rtprio,psr,args \| grep -E 'rtl_daq\|rebuffer\|decimate\|iq_server\|delay_sync\|hw_controller\|synthesizer'` |
| Receivers | `lsusb`, `python3 _daq_core/hw_discover.py --sdr`, `_data_control/<P>hw_caps.json` |
| Kernel | `dmesg \| tail -n 200` (USB resets, OOM) |
| Runtime state | `ls -l _data_control/ /dev/shm/` |
| Control plane | `heimdall-ctl --json hwc-status`, `heimdall-ctl --json track-lock` |
| Status (if enabled) | `heimdall-ctl --json status`, `heimdall-ctl --json events`, `heimdall-ctl --json metrics` |
| Ports | `heimdall-ctl --instance N ports`, `sudo ss -ltnp` |
| Federation | `heimdall-ctl --json fleet status`, `heimdall-ctl --json router stats`, `_logs/federation/*.log` |
| Registry (if enabled) | `heimdall-ctl --json instances` |

The `auth_token` value is the shared secret of `:6000` and the router stats port. Mask it, and any `HEIMDALL_AUTH_TOKEN` value, in configs and in `-v` output before you share them.

Set `[daq] log_level = 2` and reproduce if the logs show nothing useful at the shipped level 5.

---

## 15. Privilege model

| Component | Runs as | Why |
|---|---|---|
| DAQ chain (all six stages) | root | `chrt -f` SCHED_FIFO priorities, `mlockall`, `taskset`, sysctl and usbfs tuning, USB device access; hw_controller drives I2C |
| Start and stop scripts | root (`sudo`) | they call `sudo` internally for tuning and signals |
| Federation coordinator, IQ router | any unprivileged user | they use ports above 1024 and touch no hardware |
| `heimdall-ctl` | any user | network client; the ack-mode cache is per user |

Start every instance on a host as the same uid. The registry, lock and PID handling assume one uid ([2.5](#25-registry-instance-lock-and-duplicate-guard-opt-in)).

---

## 16. Known limitations

| Limitation | Effect |
|---|---|
| One hardware instance per set of receivers | each instance needs receivers with its own serials; two instances cannot share a receiver |
| No coherence across units | each receiver unit is calibrated on its own; frames from different units align only by wall-clock time (needs NTP or PTP). See [theory_coherent_acquisition.md](theory_coherent_acquisition.md) |
| 31 channels per instance | the 128-byte ZMQ message holds 31 values; `ini_checker` rejects `num_ch > 31` |
| delay_sync FSM timing on hardware | the FSM step (`_fsm_step`) is covered by `test_delay_sync_fsm.py` and `test_sync_loop_model.py` in CI; hardware timing is not |
| Sample-rate nudge loop gains are not characterised on hardware | convergence speed of the `s` correction may differ between receiver batches |
| Dynamic-range threshold fixed at 20 dB | not configurable |
| Shipped `log_level = 5` | hides warnings ([4.3](#43-log-levels)) |
| `daq_stop.sh` without an argument is host-wide | stops every instance ([2.2](#22-stop)) |
| The default `legacy` guard runs the host-wide `daq_stop.sh` on a busy port | re-starting one instance can stop the others; set `HEIMDALL_SCOPED_STOP=1` or `[host] instance_guard = refuse` ([2.3](#23-restart)) |
