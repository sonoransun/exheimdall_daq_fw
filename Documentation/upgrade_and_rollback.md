# Upgrade, Rollback and Version Compatibility

This release is a drop-in upgrade for an existing station. Every new feature is opt-in. With every new key absent and no new flag passed, the chain behaves as before. The only exceptions are the sixteen default-path differences listed in [section 4](#4-default-path-changes-in-this-release-ledger-l1-l16). This document explains how to identify versions, what to keep, how to upgrade a station or a fleet, and how to roll back.

Two packages of this release are still being implemented: the systemd unit changes (WP-16, ledger row L8) and the `.hcap` capture sidecar (WP-20). The rows that describe them follow the design specification and carry a `verify-after` marker in the source of this page. Everything else on this page describes the code in this tree.

## Key facts

| Item | Value in this release | Changed from the previous tree? |
|---|---|---|
| Firmware version | `FW_VERSION = "1.1.0-dev"` in `Firmware/_daq_core/version.py` | New file. Older trees have no version string |
| heimdall-ctl package version | `0.1.0` (`pyproject.toml`) | Unchanged. It does not track the firmware version |
| IQ header | 1024 bytes, `header_version = 8` | Unchanged. New reserved slots 103-113 are zero unless an opt-in key stamps them |
| INI schema | `ini_version = 8` | Unchanged. Informational, not checked by `ini_checker.py` |
| Database | schema generation 2 | Unchanged |
| Coordinator `:6000` protocol | `2` (reported by the new `VERSION` verb) | The previous coordinator is protocol 1 |
| C build | `make` in `Firmware/_daq_core` | **Mandatory** on every update ([section 5](#5-rebuild-requirements)) |

## 1. Version identifiers

Read the firmware version from the tree itself:

```
$ cd Firmware
$ python3 _daq_core/version.py
1.1.0-dev
```

If `_daq_core/version.py` does not exist, the tree predates this release.

The release process changes `FW_VERSION` in `version.py` and nowhere else. Running components report it in these places:

| Where | Field | Needs |
|---|---|---|
| hw_controller relay file `_data_control/<P>hwc_state` | `fw_version` | `[monitoring] en_hwc_state_relay = 1` |
| `:5002` `STATUS` reply | `fw_version`, `ini_version` | `[monitoring] en_monitoring = 1`, `en_status_server = 1` and `status_extended = 1` |
| delay_sync `REGISTER` body sent to the coordinator | `fw_version` | `[federation] en_federation = 1` and `register_with_coordinator = 1` |
| Coordinator `VERSION` verb (`heimdall-ctl fleet version`) | `fw_version`, `protocol` | a running coordinator |
| IQ router stats port `VERSION` verb | `fw_version`, `protocol` (`1`) | a running router |
| `.hcap` capture sidecar (`<name>.hcap.json`) | `fw_version` | a capture made with `iq_recorder.py --hcap` <!-- verify-after: WP-20 --> |

`<P>` is the resource prefix: empty for instance 0, `inst<N>_` for instance N > 0.

Example against a coordinator started locally with one member (hostname replaced):

```
$ heimdall-ctl --coordinator 127.0.0.1:6000 fleet version
protocol 2  fw 1.1.0-dev  node host-a:coordinator  cluster default  role static  term 0  members 1
deadlines: query 6000 ms, mutation 40000 ms, schedule 73000 ms; abandoned workers 0
```

`heimdall-ctl --version` prints `heimdall-ctl 0.1.0` for both the previous and this release. To tell the clients apart, check `heimdall-ctl --help`: this release lists the `--ack {auto,xack,legacy}` option.

## 2. What a tree carries

An upgrade replaces code. It must keep the files below. Everything else under `Firmware/` is code, or is regenerated at every start.

| File or directory | Purpose | Keep? |
|---|---|---|
| `Firmware/daq_chain_config.ini` | the instance configuration | Yes |
| `Firmware/_calibration/cable_ch<m>.s1p` | cable Touchstone files, read when `[calibration] iq_adjust_source = touchstone` | Yes |
| `Firmware/_db/` | BerkeleyDB telemetry (schema generation 2, [section 9](#9-database-schema-generation-2)) | Yes, if you need the history |
| `Firmware/_logs/` | logs and PID files | Optional |
| `Firmware/_data_control/<P>iq_track_lock` | track-lock approval, read when `[calibration] require_track_lock_intervention = 1` | Yes, if you use track-lock approval |
| `Firmware/_data_control/<P>fence_term` | fencing term written by hw_controller when an elected coordinator sends `XACK` | Yes, if it exists. It must survive restarts |
| `federation.ini` and the coordinator state directory | coordinator configuration and persisted state (`term`, `master_schedule.json`, `members_runtime.json`, `assignments.json`). The directory is `[federation] state_dir` of `federation.ini`, or `--state-dir`. Without either, an elected coordinator uses `_data_control/federation/` relative to its working directory | Yes, on coordinator hosts ([section 7.4](#74-coordinator-and-router-hosts)) |
| `Firmware/_daq_core/librtlsdr.a`, `rtl-sdr.h`, `rtl-sdr_export.h` | copied-in krakenrf librtlsdr (always required by `make`) | Copy into the new tree |
| `Firmware/_daq_core/libkfr_capi.so*` and `kfr/` (`ENGINE=kfr`), or `libNE10.a` (`ENGINE=ne10`) | copied-in DSP library | Copy into the new tree |
| `Firmware/_daq_core/*.gcda` | PGO profile data | Discard. Profiles of old sources do not describe the new code |

Regenerated at every start, do not copy: `_data_control/<P>fir_coeffs.txt`, `_data_control/hw_caps.json`, the FIFOs in `_data_control/`, and the `/dev/shm` segments.

## 3. INI compatibility

### 3.1 An existing INI on this release

An existing `daq_chain_config.ini` works unchanged. `ini_version` stays `8`. `ini_checker.py` does not read it. delay_sync reports it in the extended `STATUS`.

Every new key is optional. An absent key gives the previous behaviour. The full key list with defaults is in [configuration.md](configuration.md). The shipped `Firmware/daq_chain_config.ini` documents each new key as a commented `;key = default` line, including a commented `; [host]` block at the end. The upgrade procedure saves that file as `daq_chain_config.ini.shipped` before it copies your INI in ([section 6.1](#61-single-station), step 5). Diff the two to see the new keys:

```
cd /opt/heimdall-<new>/Firmware
diff -u daq_chain_config.ini daq_chain_config.ini.shipped
```

Every shipped preset and the previous release's shipped INI pass the new checker silently:

```
$ python3 ini_checker.py ../config_files/kraken_default/daq_chain_config.ini no_hw; echo "exit=$?"
exit=0
```

`ini_checker.py` does not report unknown keys. A misspelled key is ignored, and its feature stays off. The checker accepts `instance_regstry = 1` without a message, for example. Check the spelling against [configuration.md](configuration.md).

### 3.2 New validation (ledger rows L6 and L7)

Three limits on existing keys are now errors in every mode. They block start, because the start scripts enforce the checker's exit code.

| Key | Limit | Reason |
|---|---|---|
| `[hw] num_ch` | at most 31 | the 128-byte `:1130` control message holds 31 per-channel values |
| `[pre_processing] cpi_size` | at most 2^23 | larger frames overrun the worst-case shared-memory segments |
| `[calibration] corr_size` | at most 2^23 | same as above |

Output with `num_ch = 32`:

```
$ python3 ini_checker.py a.ini no_hw; echo "exit=$?"
ERROR:root:Number of channels must be at most 31: the 128-byte :1130 control message holds 31 per-channel values. Currently it is: '32'
...
exit=1
```

Non-blocking problems print to stderr as `WARNING: <text>`. Warnings never change the exit code. Port-window problems are warnings on an existing INI:

```
$ python3 ini_checker.py b.ini no_hw; echo "exit=$?"
WARNING: [federation] port_stride = 3 is below 4 on instance 1: its ports overlap another instance's 5000-5003 window
exit=0
```

The same problem is an error when the INI is in **new-key mode**. An INI is in new-key mode when it contains any of these:

- `[federation] cluster_members` (non-empty)
- `[federation] coordinator_candidates` (non-empty)
- `[federation] register_with_coordinator = 1`
- any `[federation] router_*` key
- `[host] instance_registry = 1`
- `[host] instance_guard = refuse`

None of these keys existed before, so no existing INI is in new-key mode. Adding `[host] instance_registry = 1` to the INI above turns the warning into `ERROR:root:[federation] port_stride = 3 is below 4 on instance 1: ...` and exit 1.

New checker forms:

| Command | Checks |
|---|---|
| `python3 ini_checker.py [ini] no_hw` | the INI only, as before. `daq_synthetic_start.sh` runs this form |
| `python3 ini_checker.py [ini] no_hw attached` | also compares configured serials with the RTL2832U receivers in sysfs and with the live host registry. No device is opened. `daq_start_sm.sh` runs this form. Without `[hw] serial_base` or `device_serials`, a serial found on several receivers is only a warning |
| `python3 ini_checker.py federation.ini --federation` | a coordinator or router file |

Exit codes stay 0 (valid) and 1 (errors or unreadable file).

### 3.3 A new INI on an older release

The previous release ignores keys it does not know, so most new keys are harmless after a rollback. Two exceptions matter.

**Serial keys.** The previous `rtl_daq` ignores `[hw] serial_base` and `device_serials`. It always opens the receivers with serials `1000` to `1000 + num_ch - 1`. On a host with several hardware instances, instance N > 0 would then open instance 0's receivers. The previous release therefore runs only one hardware instance per host. Before you roll back such a host, stop and disable the instances N > 0 or move them to other hosts ([section 8.1](#81-procedure)).

**`[hw] unit_id = auto`.** `provision_instance.py` writes this value by default. The previous checker rejects it and blocks start:

```
$ python3 ini_checker.py d.ini no_hw; echo "exit=$?"     # previous release
ERROR:root:Unit ID must be integer. Currently it is: 'auto'
exit=1
```

Set `unit_id` back to an integer before you start an older tree. The rollback procedure in [section 8](#8-rollback) restores the old INI anyway.

## 4. Default-path changes in this release (ledger L1-L16)

"Default path" means an existing INI with every new key absent, and scripts invoked with no new flag. These rows are the complete list of differences on that path. Each row is a fix of an audited defect, a cleanup that is a no-op unless an opt-in feature created files, or (L16) log text only. Everything else is byte-identical: process names, FIFO and shm names and sizes, ports, the 128-byte `:5001` and `:1130` frames, the `FNSD` replies of existing verbs, the `:5002` JSON of existing verbs, the `:5003` frames and the 1024-byte IQ header.

"Implemented" means the change is in the code of this tree. "Specified" means its package (WP-16) is still in progress. The row describes the specification.

| Row | Difference on the default path | Status |
|---|---|---|
| L1 | The no-argument `daq_stop.sh` runs a registry prune after its usual sweep. It deletes `$HEIMDALL_RUN_DIR/inst*.json` and `inst*.env` files whose pids are all dead. Only a start with `[host] instance_registry = 1` writes such files, so the step does nothing on a host that never opted in | Implemented |
| L2 | The start scripts create `_data_control/<P>iq_track_lock` containing `0` when it is absent | Implemented |
| L3 | hw_controller no longer opens `iq_track_lock` at init. It reads the file only when `require_track_lock_intervention = 1`, and re-opens it on every check. A missing file no longer stops the control plane | Implemented |
| L4 | `fir_decimate.c` and `iq_server.c` compute buffer sizes in `size_t`. Values are identical for `num_ch` up to 31 | Implemented |
| L5 | The start script's port-busy test matches listening sockets only (`ss -ltnH`, then `lsof -sTCP:LISTEN`), so a connected DoA client no longer triggers a kill. The status port probed is `[monitoring] status_server_port` + offset (fallback 5002). Ports `1130` + offset and the event PUB port get an advisory `WARN:` line only. The retry loop, its `./daq_stop.sh` call, the 10 attempts and exit 1 are unchanged | Implemented |
| L6 | `ini_checker` errors on `num_ch > 31`, `cpi_size > 2^23` and `corr_size > 2^23`. Port-window checks are errors only in new-key mode ([section 3.2](#32-new-validation-ledger-rows-l6-and-l7)) | Implemented |
| L7 | `ini_checker` prints warnings to stderr with the prefix `WARNING: `. The exit code is unchanged. Every shipped preset prints no warning in `no_hw` mode. `daq_start_sm.sh` now runs the `no_hw attached` form. Without serial keys its serial checks only warn, for example when two attached receivers share a serial | Implemented |
| L8 | `heimdall-daq.service` uses `ExecStop=daq_stop.sh 0`, an optional `EnvironmentFile=-`, `ConditionPathExists` and `Conflicts=` with the supervised unit. `KillMode=control-group` is unchanged | Specified (WP-16) <!-- verify-after: WP-16 --> |
| L9 | Synthetic mode. The synthesizer answers ZMQ control commands on `1130` + offset, so the simulated chain can calibrate. It still stamps header `unit_id = 0` unless `[hw] unit_id = auto` or `--unit-id-from-config` is given. It stays unpaced, unseeded and 10 000 blocks long by default | Implemented (synthesizer) |
| L9 (scripts) | `daq_synthetic_start.sh` exits 1 when the FIR designer fails (it returned 0 before), runs an advisory port probe, uses `sudo env "PATH=$PATH"` for hw_controller and creates `iq_track_lock` (L2) | Implemented |
| L10 | `auto_config.py` rewrites the INI atomically (temporary file and `os.replace`, mode and owner kept). It writes nothing when no value is `auto`, which is the case for every shipped INI | Implemented |
| L11 (FIR) | For instance N > 0 only: `fir_filter_designer.py` writes `_data_control/inst<N>_fir_coeffs.txt` and `decimate.out` reads that name. In a separate instance directory the designer also deletes a stale unprefixed `fir_coeffs.txt`, so an old `decimate.out` fails loudly instead of filtering with old taps. Instance 0 is unchanged | Implemented |
| L11 (hw_caps) | For instance N > 0 only: hardware discovery writes `_data_control/inst<N>_hw_caps.json` and `auto_config.py` reads that path | Implemented |
| L12 | The start scripts delete `_data_control/<P>recal_request` and `<P>hwc_state` next to the FIFO recreation. Only the new opt-in relays create these files | Implemented |
| L13 | Coordinator. No script or unit starts it by default. Legacy verbs change: `INSTANCE` with a non-integer id replies a usage error; `REBALANCE` without a master schedule replies `{"error":"no master schedule"}`; `STATUS` counts unreachable members as `error` and gains additive keys; fan-out is parallel with a per-request deadline; the `:5001` read timeout is at least 14 s instead of 5 s; every per-member command is preceded by `XACK`; concurrent mutations may get `{"error":"busy","retry_in_sec":1}`; CLI flags take precedence over `--config` | Implemented |
| L14 | IQ router. No launcher started it before. Forwarding becomes non-blocking with bounded per-client queues, and source readers get read timeouts, keepalive and reconnect backoff | Implemented |
| L15 | heimdall-ctl (client only). See the five items below | Implemented |
| L16 | Log text only. See the list below | Implemented |

**L15 heimdall-ctl items.** These change the client only. The escape hatch is `--ack legacy` (alias `--legacy-ack`).

1. Mutating verbs read and report the reply. A socket error prints `Error: ...` and exits 3 instead of a traceback. `--instance N` applies the port stride without a config. `events --tail` decodes the multipart frames. A payload over 124 bytes raises an error instead of being truncated.
2. With the default `--ack auto`, the first mutating command to a `host:port` sends `XACK` first. A previous-release server answers `FNSD` + zeros and logs one `Unidentified control command: XACK` line. The client remembers it as legacy in `~/.cache/heimdall-ctl/ack_modes.json` (or `$XDG_CACHE_HOME/heimdall-ctl/ack_modes.json`) for 24 h, and later commands to it are sent without `XACK`.
3. When `XACK` gets no answer within 3 s, the command is not sent. The client exits 3 with `server did not answer XACK within 3 s (another client may hold the control port); command not sent - retry, or use --ack legacy to queue it behind that client`. Before, the frame waited in the kernel backlog and ran later.
4. Mutating commands wait for the server-derived timeout (14 s against a previous-release server) instead of 5 s. Queries keep 5 s.
5. A `gain`, `lna-gain` or `bias-tee` list of the wrong length prints `warning: expected M values, got k (the server uses the first M)` and is sent as before. `--strict` refuses it with exit 1.

Against a previous-release server a mutating command prints `sent; server does not confirm application (legacy server)` and exits 0.

**L16 log-text changes.**

| Process | Change | Status |
|---|---|---|
| hw_controller | The stale `Reference channel index is fixed 0` line is gone. `:5001` keeps `listen(4)` when `ctl_max_connections = 1` | Implemented |
| rtl_daq | Logs `ZMQ 'r' reconfiguration is deprecated` on the first `r` command; appends ` - raise [daq] usb_ring_buffers (now N)` to the overrun warning; prints an expanded fatal text for an unresolved serial; logs `serial <s> is present on <n> devices; program distinct serials with util/set_unit_serials.sh` as a warning when two attached receivers share a resolved serial and no serial key is set (the same text is fatal with `serial_base` or `device_serials` set); logs `antenna switch GPIO: none (skipped)` only when the new key says so | Implemented |
| decimate | Logs `FIR coefficients: <path> (<n> taps)` at info level | Implemented |
| start scripts (`hd_core_guard`) | Print the same `WARN:` text as before. The extra line that names an overlapping instance appears only with `instance_registry = 1` | Implemented |

Not on this list, and so unchanged: the no-argument `daq_stop.sh` stays the host-wide global kill with the same SIGKILL list; the start scripts' exit codes stay 0 (started) and 1 (failure, including busy ports); the start scripts open no extra file descriptors, export no new variables and wait on no lock; the shipped `[database] db_dir = _db` is used verbatim for every instance.

## 5. Rebuild requirements

Run `make` on every update, even when you think only Python files changed.

```
cd Firmware/_daq_core
make -j4
```

Use the same `ENGINE`, `BUILD` and `MARCH` values as your previous build (for example `make ENGINE=generic`). The copied-in libraries of [section 2](#2-what-a-tree-carries) must be present first.

Why the build is mandatory:

- `rtl_daq.out` now links the new `device_map.c` (serial keys and the duplicate-serial check).
- `iq_header.h` gained the slot defines 103-113 and `frame_sizes.h` is new. The Makefile tracks header dependencies, so `make` rebuilds what depends on them.
- For instance N > 0, a new `fir_filter_designer.py` writes `inst<N>_fir_coeffs.txt`. An old `decimate.out` still reads the unprefixed `fir_coeffs.txt` (L11). In a separate instance directory that file is deleted, and the old decimator stops with an error. In a shared working copy the old decimator reads instance 0's coefficients without an error.

Other build notes:

| Situation | Action |
|---|---|
| You toggled `PIGPIO` | run `make clean` first. A target-specific flag change does not rebuild `rtl_daq.o` by itself |
| You use PGO | run `make distclean` to drop the old `*.gcda` profiles, then the `make pgo-profile`, workload, `make pgo-optimize` cycle again. `make clean` keeps the profiles on purpose |
| `ENGINE=kfr` | `decimate.out` finds the copied-in `libkfr_capi.so` next to itself through `-rpath $ORIGIN`. Keep the `.so` in the same directory as the binary |
| You build the dependencies with `ci/build.sh` | it honours `LIBRTLSDR_REF` (default `master`) and `KFR_REF` (default `7.1.0`) to pin the dependency versions |
| heimdall-ctl was installed with `pip install .` | reinstall it from the new tree: `pip install --force-reinstall --no-deps .`. The package version stays `0.1.0`, so a plain `pip install .` may treat it as already installed. The in-tree launcher `util/bin/heimdall-ctl` needs no install |

Copy the whole tree. hw_controller imports the new modules `version.py`, `hwc_relay.py` and `federation_common.py` at start, so a hw_controller copied on its own into an old tree fails to start.

## 6. Upgrade procedure

### 6.1 Single station

Install the new release next to the old one, then switch a symlink. The old tree stays intact and is your rollback. Steps 1 to 6 run while the old chain keeps running, so the outage covers only steps 7 to 12. A failed build or a failed check leaves the station running on the old tree.

**Prepare (chain still running).**

1. Note the running version and set the two tree paths. `OLD` is the current tree: the target of the `/opt/heimdall` symlink, or `/opt/heimdall` itself when it is a real directory.

   ```
   cd /opt/heimdall/Firmware && python3 _daq_core/version.py   # absent file = pre-1.1.0
   OLD=$(readlink -f /opt/heimdall)
   NEW=/opt/heimdall-<new>
   ```

2. Put the new tree at `$NEW` (git checkout or copy). It must not be inside `$OLD`.
3. Copy the vendor files into the new tree:

   ```
   cd $OLD/Firmware/_daq_core
   sudo cp -a librtlsdr.a rtl-sdr.h rtl-sdr_export.h $NEW/Firmware/_daq_core/
   sudo cp -a libkfr_capi.so* kfr $NEW/Firmware/_daq_core/     # ENGINE=kfr
   sudo cp -a libNE10.a $NEW/Firmware/_daq_core/               # ENGINE=ne10
   ```

   The alternative is `$NEW/util/install.sh`. It copies the libraries into the `_daq_core` of the tree that holds the script, so run the new tree's copy. It also runs `apt-get`, installs udev rules and copies the kernel tuning to `/etc/sysctl.d/`.

4. Build with the same `ENGINE`, `BUILD` and `MARCH` values as before ([section 5](#5-rebuild-requirements)):

   ```
   cd $NEW/Firmware/_daq_core && make -j4
   ```

5. Save the shipped INI, copy yours in and validate it:

   ```
   cd $NEW/Firmware
   sudo cp daq_chain_config.ini daq_chain_config.ini.shipped
   sudo cp -a $OLD/Firmware/daq_chain_config.ini .
   python3 ini_checker.py no_hw; echo "exit=$?"            # must print exit=0
   diff -u daq_chain_config.ini daq_chain_config.ini.shipped   # the new keys, as ;key = default
   ```

   On the target host, `python3 ini_checker.py no_hw attached` is the form `daq_start_sm.sh` runs. It also checks the configured serials against the receivers in sysfs. It opens no device, so it is safe while the old chain runs.

6. If you installed the systemd units, save the installed copies for a rollback:

   ```
   sudo mkdir -p /root/heimdall-units-<old>
   sudo cp -a /etc/systemd/system/heimdall-daq*.service /root/heimdall-units-<old>/
   ```

**Switch (chain stopped).**

7. Stop the chain and check that it stopped:

   ```
   sudo systemctl stop heimdall-daq          # or: cd /opt/heimdall/Firmware && sudo ./daq_stop.sh
   systemctl is-active heimdall-daq          # prints inactive
   ```

8. If `/opt/heimdall` is a real directory, rename it once and update `OLD`:

   ```
   sudo mv /opt/heimdall /opt/heimdall-<old>
   OLD=/opt/heimdall-<old>
   ```

   Do not skip this. On a real directory, `ln -sfn` in step 10 creates the link inside it instead of replacing it.

9. Copy the state of [section 2](#2-what-a-tree-carries). `_data_control/federation/` exists only where an elected coordinator ran from this tree without a `state_dir`:

   ```
   cd $OLD/Firmware
   sudo mkdir -p $NEW/Firmware/_data_control
   for f in _calibration _db _data_control/*iq_track_lock _data_control/*fence_term _data_control/federation; do
       [ -e "$f" ] && sudo cp -a "$f" "$NEW/Firmware/$(dirname "$f")/"
   done
   ```

10. Switch the symlink and check it:

    ```
    sudo ln -sfn $NEW /opt/heimdall
    readlink /opt/heimdall                    # prints $NEW
    ```

    The units use `WorkingDirectory=/opt/heimdall/Firmware`, which now resolves into `$NEW`.

11. If you installed the systemd units, install the new tree's copies and reload:

    ```
    sudo cp $NEW/packaging/systemd/heimdall-daq.service /etc/systemd/system/
    sudo cp $NEW/packaging/systemd/heimdall-daq@.service /etc/systemd/system/   # if you use instance units
    sudo cp $NEW/packaging/logrotate/heimdall /etc/logrotate.d/heimdall
    sudo systemctl daemon-reload
    ```

    The unit changes of ledger row L8 take effect only through this step. <!-- verify-after: WP-16 -->

12. Start:

    ```
    sudo systemctl start heimdall-daq         # or: cd /opt/heimdall/Firmware && sudo ./daq_start_sm.sh; echo "exit=$?"
    ```

    `daq_start_sm.sh` exits 0 when the chain started and 1 on a config, FIR-designer or busy-port failure.

**Check.**

13. Check the chain. These checks work on the default INI:

    ```
    pgrep -af 'rtl_daq.out|rebuffer.out|decimate.out|delay_sync.py|hw_controller.py|iq_server.out'
    tail -n 20 /opt/heimdall/Firmware/_logs/delay_sync.log /opt/heimdall/Firmware/_logs/hwc.log
    ```

    `iq_server.out` runs only with the Ethernet output interface. `heimdall-ctl status` reads the `:5002` status server, which is off by default. It needs `[monitoring] en_monitoring = 1` and `en_status_server = 1`. Without them it prints `Error: 127.0.0.1:5002: Connection refused` and exits 3, even on a healthy chain.

14. Reinstall a pip-installed heimdall-ctl ([section 5](#5-rebuild-requirements)) and clear its ack-mode cache ([section 7.3](#73-expected-log-lines-and-the-client-cache)).

If a check fails, roll back ([section 8](#8-rollback)). The old tree is unchanged. Enable new opt-in keys only after the station runs well on the defaults.

### 6.2 Several instances on one host

Stop every instance before the switch, and stop each systemd unit by name. The units are `Type=oneshot` with `RemainAfterExit=yes`. `systemctl stop heimdall-daq` runs the host-wide `daq_stop.sh`, which kills the processes of every instance. The `heimdall-daq@N` units then stay `active (exited)`, so a later `systemctl start heimdall-daq@N` does nothing and instance N stays down without an error. Stop and start each unit explicitly, or use `systemctl restart`.

Both multi-instance layouts keep the instance directories under `/opt/heimdall/instances/<N>`, which is inside the versioned tree. The switch must carry them into `$NEW`.

**Full working copy per instance** (the layout of [packaging/systemd/README.md](../packaging/systemd/README.md): `/opt/heimdall/instances/<N>/Firmware` with its own INI).

1. Do 6.1 steps 1 to 6 for the root tree (instance 0).
2. For each N > 0, make a copy of the built `Firmware/` and validate the instance INI. `cp -a` keeps the built binaries, so the copy needs no second build:

   ```
   N=1
   sudo mkdir -p $NEW/instances/$N
   sudo cp -a $NEW/Firmware $NEW/instances/$N/
   sudo cp -a $OLD/instances/$N/Firmware/daq_chain_config.ini $NEW/instances/$N/Firmware/
   cd $NEW/instances/$N/Firmware && python3 ini_checker.py no_hw; echo "exit=$?"
   ```

3. Stop every unit and check:

   ```
   sudo systemctl stop heimdall-daq@1 heimdall-daq@2 heimdall-daq
   systemctl is-active heimdall-daq heimdall-daq@1 heimdall-daq@2    # one inactive per unit
   ```

4. Do 6.1 steps 8 and 9 for the root tree.
5. For each N > 0, copy that instance's state:

   ```
   cd $OLD/instances/$N/Firmware
   for f in _calibration _db _data_control/*iq_track_lock _data_control/*fence_term; do
       [ -e "$f" ] && sudo cp -a "$f" "$NEW/instances/$N/Firmware/$(dirname "$f")/"
   done
   ```

6. Do 6.1 steps 10 and 11. `/opt/heimdall/instances/<N>/Firmware` now resolves to the new copies.
7. Start every unit, then check each instance as in 6.1 step 13. Instance N > 0 logs to `_logs/inst<N>/` of its own copy.

   ```
   sudo systemctl start heimdall-daq heimdall-daq@1 heimdall-daq@2
   systemctl is-active heimdall-daq heimdall-daq@1 heimdall-daq@2
   ```

   With the status server enabled, `heimdall-ctl --instance N status` checks instance N.

**Symlink instance directories** (created by `util/provision_instance.py`). Instance 0 runs in `<root>/Firmware`. Instance N > 0 runs in `<root>/instances/<N>`: its code entries are symlinks through `/opt/heimdall`, and its INI, `_data_control/`, `_logs/` and `_db/` are private. The links follow the switch by themselves, but the directory must move.

1. Do 6.1 steps 1 to 6 for the root tree (instance 0).
2. List and stop the instances. `heimdall-ctl instances` lists only instances started with `[host] instance_registry = 1`. On a host where none opted in, it prints:

   ```
   no registry entries in /run/heimdall, /tmp/heimdall-run-<uid> (only instances started with [host] instance_registry = 1 are listed)
   ```

   Stop each unit by name as in the full-copy step 3. Without units, `cd /opt/heimdall/Firmware && sudo ./daq_stop.sh --all` kills every instance and removes every registry entry.

3. Do 6.1 steps 8 and 9 for the root tree.
4. Move the instance directories into the new tree. `$NEW/instances` must not exist yet:

   ```
   sudo mv $OLD/instances $NEW/
   ```

   Skip this for instances created with `--dest` outside the tree.

5. Do 6.1 steps 10 and 11.
6. Re-link each instance N > 0 with the new tree's script. Its default root is `/opt/heimdall`, the symlink, not its target:

   ```
   sudo python3 /opt/heimdall/util/provision_instance.py --instance N --refresh --write
   ```

   Add `--dest <dir>` for an instance outside the tree. `--refresh` re-creates the code links and never touches the instance INI. It lists the template keys that are missing from the instance INI.

   | Exit | Meaning |
   |---|---|
   | 0 | re-linked |
   | 1 | validation or write failed |
   | 2 | usage error. `nothing to refresh: <dir> has no Firmware/daq_chain_config.ini` means the instance directory is not at `<root>/instances/<N>`: step 4 was skipped |
   | 4 | the instance is live. Stop it first, or pass `--force` |

7. Start and check every instance as in the full-copy step 7. `heimdall-ctl fleet status --fresh` works only where a coordinator runs. No script or unit starts one by default.

## 7. Rolling a fleet and mixed-version clusters

Mixed versions across hosts are supported. Upgrade one host at a time. Within one host, every process of an instance must come from the same tree.

### 7.1 Compatibility matrix

| Client or peer | Server or member | Result |
|---|---|---|
| previous heimdall-ctl | new hw_controller (`:5001`) | Unchanged. Without `XACK`, every existing verb replies byte-identically |
| new heimdall-ctl | previous hw_controller | Works. One `XACK` probe, one `Unidentified control command: XACK` log line on the server, then legacy mode from the cache (L15). Mutating commands exit 0 with `sent; server does not confirm application (legacy server)` |
| new heimdall-ctl `fleet` | previous coordinator (protocol 1) | Treated as legacy. Only the reply `{"error":"Unknown command"}` to `VERSION` marks a coordinator legacy |
| previous coordinator | new member | Unchanged. The member's default path answers as before |
| new coordinator | previous member | Works. The coordinator sends `XACK` before every per-member command. The member logs `Unidentified control command: XACK` at error level once per command. Results report `applied: null` with `legacy_ack: true` (counted as unconfirmed) |
| new coordinator in elected mode | another candidate on the previous release | Not supported. The previous coordinator has no election verbs. Set `coordinator_candidates` only after every candidate runs this release |
| any IQ consumer | new `iq_server` or shmem output | Unchanged. The header is 1024 bytes, version 8, and slots 103-113 are zero unless an opt-in key stamps them. See [consumer_guide.md](consumer_guide.md) |
| new IQ router | previous `iq_server` | Works. The router reads the unchanged `:5000` stream |
| new delay_sync / hw_controller | previous `_db` directory | Works. Both use schema generation 2 |

### 7.2 Recommended order

1. **Members, one host at a time** ([section 6](#6-upgrade-procedure)). On the default path a new member answers the previous coordinator exactly as before, so the cluster keeps working.
2. **Routers** ([section 7.4](#74-coordinator-and-router-hosts)). A new router reads the `:5000` stream of old and new members alike.
3. **Coordinator hosts last** ([section 7.4](#74-coordinator-and-router-hosts)). Upgrading the coordinator last avoids the per-command `XACK` log line on members that are still on the previous release.
4. **Opt-ins after the roll.** Enable cluster keys (`coordinator_candidates`, `register_with_coordinator`, `status_extended`, `ctl_max_connections` and others) only when every host runs this release. Check each host's version with `python3 _daq_core/version.py`, or with the `fw_version` fields of [section 1](#1-version-identifiers).

### 7.3 Expected log lines and the client cache

| Where | Line | Meaning |
|---|---|---|
| previous-release `_logs/hwc.log` (hw_controller) | `Unidentified control command: XACK` | a new client or coordinator probed it. Expected during mixed-version operation. Harmless |
| new coordinator log | `member <key> is a legacy hw_controller (no XACK); it logs 'Unidentified control command: XACK' per coordinator command` | logged at most once per member per cache period |
| new `daq_db` log, first start after a schema change | `DB schema version changed (<old> -> 2), discarding old databases` | see [section 9](#9-database-schema-generation-2). Not expected when you upgrade from the previous release |

After you upgrade a hw_controller, heimdall-ctl may still hold a `legacy` entry for its `host:port` for up to 24 h. Commands then run without `XACK`, as against an old server. Delete the cache as the user who runs heimdall-ctl. The file is under `$XDG_CACHE_HOME` when that variable holds an absolute path, else under `~/.cache`:

```
rm -f "${XDG_CACHE_HOME:-$HOME/.cache}/heimdall-ctl/ack_modes.json"
sudo rm -f /root/.cache/heimdall-ctl/ack_modes.json     # if you also run it with sudo
```

Every probe is a mutating command, because only mutating verbs send `XACK`. If you probe instead of deleting, re-send the station's current value, for example `heimdall-ctl --ack xack --host <host> tune <current_freq_hz>`.

The other direction needs no action. After a rollback, a cached `extended` entry still sends `XACK`, the old server answers `FNSD` + zeros, and the client records it as legacy.

### 7.4 Coordinator and router hosts

The coordinator and the IQ router are separate processes. No script or unit starts them by default. `Firmware/federation_start.sh` and `federation_stop.sh` are new in this release. The previous release has neither script, and its coordinator has no `--pid-file` option, so stop a previous-release coordinator by its process id.

1. Stop the service:

   ```
   cd /opt/heimdall/Firmware
   ./federation_stop.sh coordinator          # or: router, all
   ```

   The script reads `_logs/federation/pids/<name>.pid` (or `--pid-dir`, or `$HEIMDALL_FED_PID_DIR`). A process started by hand with `--pid-file PATH` stops with `kill $(cat PATH)`.

2. Keep the configuration and the state. `/etc/heimdall/federation.ini` stays in place. Copy a `federation.ini` that lives inside the tree to the same path in `$NEW`. Copy the state directory too: `[federation] state_dir` or the `--state-dir` value, else `_data_control/federation/` relative to the coordinator's working directory (6.1 step 9 copies the `Firmware/` one).
3. Switch the tree as in [section 6.1](#61-single-station).
4. Validate the file with the new checker:

   ```
   cd /opt/heimdall/Firmware
   python3 ini_checker.py /etc/heimdall/federation.ini --federation; echo "exit=$?"
   ```

5. Start with the same flags as before. Run it from the same working directory when the state directory is the relative default:

   ```
   ./federation_start.sh coordinator --config /etc/heimdall/federation.ini --state-dir <dir>
   ```

   Exit codes: 0 started, 1 the service exited at once (the last log lines are printed), 2 usage error, 3 already running.

6. Check: `heimdall-ctl --coordinator 127.0.0.1:6000 fleet version` and `heimdall-ctl --router 127.0.0.1:7001 router stats`.

## 8. Rollback

### 8.1 Procedure

Rollback is the upgrade in reverse. It needs no rebuild when the old tree still has its binaries. The old tree still holds its own INI, because the upgrade copied it rather than moved it.

**Multi-instance hosts first.** The previous release runs only one hardware instance per host ([section 3.3](#33-a-new-ini-on-an-older-release)). Before you roll back such a host, stop and disable the instances N > 0, or move them to other hosts:

```
sudo systemctl disable --now heimdall-daq@1 heimdall-daq@2
```

Leave `$NEW/instances` where it is. The previous release does not use it, and a later roll forward to the same `$NEW` finds it in place. To run an instance N > 0 on the old tree anyway (for example a synthetic one), move the directory back with `sudo mv $NEW/instances $OLD/` after step 1. Then run the refresh after step 3 with the new tree's script, because the previous release has no `provision_instance.py`:

```
sudo python3 $NEW/util/provision_instance.py --instance N --root /opt/heimdall --refresh --write
```

The old root must still contain `Firmware/_daq_core`, `ini_checker.py`, `daq_start_sm.sh` and `daq_stop.sh`. Set `[hw] unit_id` in that instance INI to an integer first.

**Procedure.**

1. Stop the chain and every remaining unit by name, then check: `sudo systemctl stop heimdall-daq`, `systemctl is-active heimdall-daq` prints `inactive`. Without units: `cd /opt/heimdall/Firmware && sudo ./daq_stop.sh`.
2. Copy the state you want to keep from the new tree back to the old one: `_db/` and, if they exist, `_data_control/<P>fence_term` and `_data_control/federation/`. Use the loop of 6.1 step 9 with `$NEW` and `$OLD` swapped. Set `[hw] unit_id` back to an integer if you used `auto` ([section 3.3](#33-a-new-ini-on-an-older-release)).
3. Repoint the symlink and check it:

   ```
   sudo ln -sfn $OLD /opt/heimdall
   readlink /opt/heimdall                    # prints $OLD
   ```

4. If you installed the systemd units, restore the copies saved in 6.1 step 6:

   ```
   sudo cp -a /root/heimdall-units-<old>/heimdall-daq*.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

5. Validate with the old tree's checker: `cd /opt/heimdall/Firmware && python3 ini_checker.py no_hw; echo "exit=$?"`.
6. Start, then check as in 6.1 step 13. `heimdall-ctl status` again needs the `:5002` status server.

For a coordinator or router host, stop the service as in [section 7.4](#74-coordinator-and-router-hosts), repoint the symlink and start the previous service the way it was started before.

If you roll back to binaries that predate the SIGTERM stop path, stop them with `./daq_stop.sh --legacy` (SIGRT 64).

### 8.2 Files left behind by this release

Opt-in features of this release write files that an older release does not read. None of them stops an older release from starting.

| File | Written when | Older release |
|---|---|---|
| `_data_control/<P>fence_term` | an elected coordinator sent `XACK` with this member's `cluster_id` | ignores it. After a roll forward, this release reads it again. Only `daq_stop.sh N --reset-fence` removes it |
| `_data_control/<P>hwc_state`, `<P>recal_request` | `en_hwc_state_relay = 1`, `en_wire_recal = 1` | ignores them |
| `_data_control/inst<N>_fir_coeffs.txt`, `inst<N>_hw_caps.json` | instance N > 0 started on this release | ignores them. The older designer writes the unprefixed `fir_coeffs.txt` again at start |
| `_db/inst<N>/` | `[database] instance_subdir = 1` and N > 0 | does not read it. The older release uses the shared `_db` for every instance |
| `_db/_instance_meta.json` | `[database] write_instance_meta = 1` | ignores it. The schema check deletes only `*.db`, `__db.*` and `log.*` files |
| `$HEIMDALL_RUN_DIR/inst<N>.json`, `.env` (default `/run/heimdall`) | `[host] instance_registry = 1` | does not prune them. Delete them by hand after a rollback |
| coordinator `<state_dir>/term` | elected mode | ignored by a previous coordinator. Static and previous coordinators send term 0 or no `XACK`, and term-0 connections are never fenced |
| `~/.cache/heimdall-ctl/ack_modes.json` | heimdall-ctl | self-correcting ([section 7.3](#73-expected-log-lines-and-the-client-cache)) |

If you later roll forward an elected coordinator with an empty `state_dir`, members may refuse its commands with `stale_term` until the next election raises its term above theirs. See [operations_runbook.md](operations_runbook.md) for the `term_jump` and term-exhaustion recovery.

## 9. Database schema generation 2

This release keeps schema generation 2. An upgrade from the previous release keeps the database.

How the check works (`Firmware/_daq_core/daq_db.py`):

- The marker file `<db_dir>/_schema_version` holds the generation (`2`). The check runs under an exclusive lock on `<db_dir>/_schema_version.lock`, because delay_sync and hw_controller open the same directory at start.
- A **read-write** open (delay_sync, hw_controller) with a missing or different marker deletes every `*.db`, `__db.*` and `log.*` file in the directory, logs `DB schema version changed (<old> -> 2), discarding old databases` at WARNING, and writes the marker. The records are rolling telemetry, so the discard is deliberate.
- A **read-only** open (heimdall-ctl local DB mode) with a mismatch raises `DAQ database at <dir> has schema version <old> (expected 2)` and deletes nothing.

Rules for operators:

- Copy `_db/` aside before an upgrade if you need the history. A release that changes the generation discards it on the first read-write open. [CHANGELOG.md](../CHANGELOG.md) states every generation change.
- Move `_db/` aside before you start a tree older than generation 2. This document does not cover that rollback.
- `[database] instance_subdir = 1` moves the database of instance N > 0 to `<db_dir>/inst<N>`. Existing data in the shared directory is not moved. Instance 0 keeps `db_dir` unchanged.

## 10. Related documents

- [configuration.md](configuration.md): every key with default and validation
- [protocols.md](protocols.md): `XACK`, extended acks and the `:6000` verbs
- [operations_runbook.md](operations_runbook.md): start and stop forms, symptoms and fixes
- [scaling_and_clustering.md](scaling_and_clustering.md): instance model, election and fencing
- [consumer_guide.md](consumer_guide.md): IQ header slots for downstream software
- [deployment_recipes.md](deployment_recipes.md): end-to-end installs
- [../CHANGELOG.md](../CHANGELOG.md): release notes
