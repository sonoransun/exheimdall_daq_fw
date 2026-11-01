# systemd units and host packaging for the HeIMDALL DAQ firmware

Everything under `packaging/` assumes one **install root** per host,
`/opt/heimdall` (it holds `Firmware/`, `util/`, `config_files/` and
`packaging/`). Instance 0 runs in `/opt/heimdall/Firmware` itself. Every other
instance N runs in its own **instance directory**,
`/opt/heimdall/instances/<N>/Firmware`. That directory holds a private
`daq_chain_config.ini`, `_data_control/`, `_logs/` and `_db/`; the code is
symlinked back into the install root. `util/provision_instance.py` creates
the instance directories.

| File | Purpose |
|---|---|
| `systemd/heimdall-daq.service` | instance 0, **oneshot**: starts the chain and leaves it alone |
| `systemd/heimdall-daq@.service` | instance N > 0, oneshot (template) |
| `systemd/heimdall-daq-supervised.service` | instance 0, **supervised**: `daq_supervise.sh` watches the chain, and systemd restarts it when a stage dies |
| `systemd/heimdall-daq-supervised@.service` | instance N > 0, supervised (template) |
| `systemd/heimdall-host-tuning.service` | host-global tuning, run once per boot (`util/host_tuning.sh --apply`) |
| `systemd/heimdall-coordinator.service` | federation coordinator (port 6000), runs unprivileged |
| `systemd/heimdall-iq-router.service` | federation IQ router (ports 7000/7001), runs unprivileged |
| `systemd/heimdall-daq-watchdog.service` + `.timer` | optional: restarts a supervised chain whose status server keeps reporting a failure |
| `default/heimdall-daq` | environment template for the DAQ units → `/etc/default/heimdall-daq` |
| `default/heimdall-federation` | environment template for the federation units → `/etc/default/heimdall-federation` |
| `udev/99-heimdall-rtlsdr.rules` | optional: RTL2832U access for the `plugdev` group |
| `logrotate/heimdall` | log rotation for the chain, instance and federation logs |

None of these files changes how the scripts behave when they are run by hand.

The units wrap the shell scripts, so the tiered SCHED_FIFO priorities and the
CPU affinity are still set where they are defined, in `daq_start_sm.sh`. The
DAQ units only grant the limits the chain needs (`LimitRTPRIO=99`,
`LimitMEMLOCK=infinity`) and manage the lifecycle. Do not add
`CPUAffinity=`: it would constrain the per-process `taskset` pinning the start
script performs.

## 1. Deploy the install root

```sh
sudo mkdir -p /opt/heimdall
sudo cp -r Firmware util config_files packaging /opt/heimdall/
# The C core must be built in the install root. The units do not start without
# Firmware/_daq_core/rtl_daq.out (ConditionPathExists).
cd /opt/heimdall/Firmware/_daq_core && sudo make -j"$(nproc)"

sudo cp /opt/heimdall/packaging/systemd/*.service /opt/heimdall/packaging/systemd/*.timer \
        /etc/systemd/system/
sudo cp /opt/heimdall/packaging/default/heimdall-daq /etc/default/heimdall-daq
sudo cp /opt/heimdall/packaging/logrotate/heimdall /etc/logrotate.d/heimdall
sudo systemctl daemon-reload
```

`/etc/default/heimdall-daq` is optional, but you usually want it. systemd
starts units with a minimal `PATH`, so without this file the stages run the
system `python3` instead of the conda/miniforge `kraken` environment:

```
PATH=/opt/miniforge3/envs/kraken/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
HEIMDALL_RUN_DIR=/run/heimdall
```

Edit `PATH` if your environment lives elsewhere. Per-instance overrides go in
`/etc/default/heimdall-daq@<N>`, which `provision_instance.py --systemd` writes
(it uses `--python-bin DIR` to prepend a directory to `PATH`). These files
take plain `KEY=VALUE` lines only: no inline comments and no shell syntax.

If the firmware lives somewhere other than `/opt/heimdall`, edit every
`/opt/heimdall` path in the units and in the logrotate policy.
`provision_instance.py` prints a logrotate stanza that matches its `--root`.

## 2. Choose a supervision mode per instance: oneshot or supervised

| | oneshot (`heimdall-daq[@N]`) | supervised (`heimdall-daq-supervised[@N]`) |
|---|---|---|
| Service type | `Type=oneshot`, `RemainAfterExit=yes` | `Type=simple` (main process: `daq_supervise.sh`) |
| A stage dies | stays dead until the next manual restart; `systemctl status` still says `active (exited)` | supervisor logs `stage <role> (pid P) died`, stops the instance, exits 10-15; systemd restarts it after 5 s |
| Status server healthcheck | none | `--healthcheck auto`: on when `[monitoring] en_monitoring = 1` and `en_status_server = 1` (3 consecutive failures → exit 16 → restart) |
| Stop | `daq_stop.sh <N>` | `daq_stop.sh <N>` from the supervisor, then `ExecStopPost=daq_stop.sh <N> --clean` |
| Registry `launcher` | `systemd-oneshot` | `systemd-supervised` (+ `unit`) |

The two units of one instance **conflict**: starting one stops the other.
Enable only one per instance.

Every stop is scoped to its instance. `heimdall-daq.service` now stops with
`daq_stop.sh 0` instead of the old no-argument global kill, so stopping it on
a multi-instance host no longer takes the other instances down. On a
single-instance host the same processes are stopped as before.

### Supervised units: exit codes and restart policy

`daq_supervise.sh` exits with:

| Code | Meaning | systemd |
|---|---|---|
| 0 | stopped by SIGTERM/SIGINT (`systemctl stop`) | not restarted |
| 1 | start failed (config error, ports still busy after 10 rounds) | **not restarted** |
| 2 | usage or misconfiguration (`--instance` ≠ INI `instance_id`, legacy guard on a multi-instance host, `--healthcheck on` with the status server disabled) | **not restarted** |
| 3 | guard refusal (instance already running from another copy, core overlap with `allow_core_sharing = refuse`) | **not restarted** |
| 4 | strict preflight refused the start (`[host] preflight = strict`) | **not restarted** |
| 10 / 11 / 12 / 13 / 14 / 15 | first stage to die: rtl_daq or synthesizer / rebuffer / decimate / delay_sync / hw_controller / iq_server | restarted (`Restart=on-failure`, `RestartSec=5`) |
| 16 | status-server healthcheck failed 3 times in a row | restarted |

Codes 1-4 are listed in `RestartPreventExitStatus=1 2 3 4`. They are not
transient, and restarting would only loop. At most 5 starts are allowed within
300 s (`StartLimitBurst=5`, `StartLimitIntervalSec=300`). After that the unit
stays `failed`, with nothing running, and nothing brings it back on its own:
the watchdog (section 6) only checks chains that are still running, and a
stopped supervised unit leaves no registry entry (`ExecStopPost=… --clean`
removes `/run/heimdall/inst<N>.json`). Find the cause
(`journalctl -u <unit>`, for example an unplugged USB hub), then recover
it by hand:

```sh
sudo systemctl reset-failed <unit>
sudo systemctl start <unit>
```

`ExecStopPost=… --clean` runs after every stop and every restart. It removes
the instance's FIFOs, `/dev/shm` segments and relay files, and keeps
`iq_track_lock`, the FIR coefficients and `fence_term`, so coordinator
fencing survives supervised restarts.

## 3. Single-instance host (instance 0)

Oneshot (no change in behaviour on a single-instance host):

```sh
sudo systemctl enable --now heimdall-daq
```

Supervised:

```sh
sudo systemctl enable --now heimdall-daq-supervised
```

Logs stay where the scripts put them: `/opt/heimdall/Firmware/_logs/*.log`.
`journalctl -u heimdall-daq` (or `-u heimdall-daq-supervised`) shows the
start/stop script output and the supervisor's messages.

## 4. Multi-instance host

`daq_start_sm.sh` reads the instance id from `daq_chain_config.ini`
(`[federation] instance_id`), not from its command line. Every instance N > 0
therefore needs its own instance directory. **Instance 0 must also be
provisioned, in place**, so that it carries `[host] instance_registry = 1`
and `instance_guard = refuse`. Otherwise its supervisor refuses to start
while another instance is live (exit 2, not restarted):

```
instance 0 runs with [host] instance_guard = legacy on a multi-instance host; provision it with util/provision_instance.py --instance 0 --in-place
```

```sh
cd /opt/heimdall
# dry run first (prints the directory plan, the INI diff and the port table), then --write
sudo python3 util/provision_instance.py --instance 0 --in-place --systemd --write
sudo python3 util/provision_instance.py --instance 1 --systemd --write
sudo python3 util/provision_instance.py --instance 2 --systemd --write

sudo systemctl enable --now heimdall-host-tuning
sudo systemctl enable --now heimdall-daq-supervised heimdall-daq-supervised@1 heimdall-daq-supervised@2
```

Layout:

```
/opt/heimdall/Firmware/                      instance 0 (instance_id = 0)
/opt/heimdall/instances/1/Firmware/          instance 1 (instance_id = 1, ports +100)
/opt/heimdall/instances/2/Firmware/          instance 2 (instance_id = 2, ports +200)
/etc/default/heimdall-daq                    written for instance 0 by --systemd
/etc/default/heimdall-daq@1, @2              written for N > 0 by --systemd
```

The `%i` suffix of a unit must equal the `instance_id` in that directory's
INI. The supervisor checks this (`--instance %i`) and exits 2 on a mismatch.
A template unit does not start when
`/opt/heimdall/instances/%i/Firmware/daq_chain_config.ini` does not exist.

`heimdall-ctl instances` lists the running instances from the host registry
(`/run/heimdall/inst<N>.json`). The registry is a host-wide view only when
every instance sets `[host] instance_registry = 1` and all of them are
started as the same user (root, under these units).

After an upgrade of the install root, re-run `make` and re-link each instance
with `provision_instance.py --instance N --refresh --write`.

## 5. Host tuning (`heimdall-host-tuning.service`)

By default `daq_start_sm.sh` tunes the host on every start:
`kernel.sched_rt_runtime_us = -1`, USB IRQ affinity (`util/irq_tuning.sh`),
`usbfs_memory_mb = 0` and `drop_caches = 3`. These settings are host-global.
On a multi-instance host, run them once per boot instead:

```sh
sudo systemctl enable --now heimdall-host-tuning
sudo /opt/heimdall/util/host_tuning.sh --check     # exit 0 when every setting is applied
```

The unit is `Type=oneshot` with `RemainAfterExit=yes`, ordered before
`heimdall-daq.service` and `heimdall-daq-supervised.service`. The
supervised template units order themselves after it. Instances provisioned
with `--systemd` set `[host] host_tuning = 0`. An instance with
`host_tuning = auto` skips its own tuning while this unit is active and logs
`host tuning skipped (...)`.

`util/kernel_tuning.conf` is a separate, persistent sysctl profile
(`/etc/sysctl.d/99-heimdall-rt.conf`, installed by `util/install.sh` and
`util/system_tuning.py`). Apply it now with
`sudo sysctl -e -p /etc/sysctl.d/99-heimdall-rt.conf`. `-e` skips keys that
the running kernel does not have.

## 6. Watchdog (optional; not enabled by the installer)

The supervisor already restarts a chain whose stage dies, and with the
healthcheck on, a chain whose status server fails. The watchdog is a second
line of defence, run from a timer. It covers a hung supervisor and a chain that
keeps serving a stale or error STATUS. It only acts on chains that are still
running: it does not bring back a unit that hit its start limit (section 2
gives the manual recovery).

```sh
sudo systemctl enable --now heimdall-daq-watchdog.timer
journalctl -u heimdall-daq-watchdog
```

The timer starts `heimdall-daq-watchdog.service` 120 s after boot and every
30 s after that. The service runs
`/opt/heimdall/util/heimdall_watchdog.py --run-dir /run/heimdall --fails 3`.
On each run it does the following:

* It reads the registry (`/run/heimdall/inst<N>.json`) and considers only
  live entries with `launcher = systemd-supervised` and a status port. Every
  other entry (started by hand, by a oneshot unit, by `daq_supervise.sh`
  outside systemd, or with the status server disabled) is reported once per
  chain start and **never** restarted.
* It skips an entry during its start grace: `max(120, 3 × stale_after_sec)`
  seconds after `started_at`. `stale_after_sec` comes from the extended
  STATUS (`[monitoring] status_extended = 1`), else 132, which gives 396 s.
  `--grace SEC` overrides it.
* It sends `STATUS` to the instance's status port (2 s timeout). A check fails
  when the port is unreachable, when `pipeline_health` is `error` or `stale`,
  or when `frame_count` has not changed for the stall threshold. The
  threshold is `3 × stale_after_sec + 30` when STATUS reports it; otherwise
  it is `3 × k_eff × frame period + 30` from the instance INI, which is
  161 s at the shipped defaults. `--stall-sec SEC` overrides it. There is no
  stall check when STATUS has no `frame_count` or the snapshot is never
  refreshed.
* After 3 consecutive failing runs it runs `systemctl reset-failed <unit>`
  and then `systemctl restart <unit>`. The `reset-failed` only clears the
  unit's start-rate counter, so that systemd does not refuse this restart of
  a live but unhealthy unit.
* An entry whose registry record has no valid unit name is reported once
  per chain start and is not watched.

Counters live in `/run/heimdall/watchdog.json`, keyed by
`<instance_id>@<started_at>`. A restarted chain therefore starts again from
zero, and the counters of chains that are gone are dropped.

To see what the watchdog would do without letting it act:

```sh
sudo /opt/heimdall/util/heimdall_watchdog.py --dry-run --verbose
```

Example journal lines:

```
heimdall_watchdog: instance 1 (heimdall-daq-supervised@1.service): pipeline_health is stale - failed check 1/3
heimdall_watchdog: instance 1 (heimdall-daq-supervised@1.service): pipeline_health is stale - failed check 3/3
heimdall_watchdog: instance 1 (heimdall-daq-supervised@1.service): restarted heimdall-daq-supervised@1.service
heimdall_watchdog: instance 2: started by launcher 'manual' (only systemd-supervised chains are restarted); not watched
```

Exit status: 0 for a completed run, 1 when a `systemctl` call failed or the
state file could not be written, 2 for a usage error.

## 7. Federation services (coordinator and IQ router)

Both services run as an unprivileged user. They need no RT priority, no CPU
pinning and no privileged ports. Create the user and the configuration
directory first:

```sh
sudo /opt/heimdall/util/install.sh --extras-only --federation-user   # system user + group "heimdall"
sudo install -d -m 0755 /etc/heimdall
sudo install -m 0640 -o root -g heimdall federation.ini /etc/heimdall/federation.ini   # e.g. from util/cluster_cfg_gen.py
sudo cp /opt/heimdall/packaging/default/heimdall-federation /etc/default/heimdall-federation
sudo systemctl enable --now heimdall-coordinator heimdall-iq-router
```

`federation.ini` holds the `[federation] auth_token`, so it is installed
`0640 root:heimdall`: the service group can read it, other local users
cannot (a root-only `0600` file is unreadable to the `User=heimdall`
services).

Any unprivileged user works. To use another user, edit `User=`/`Group=` with
`systemctl edit` and give `federation.ini` that group. systemd creates the directories, owned by that user:

| Directory | Unit setting | Used for |
|---|---|---|
| `/var/lib/heimdall/coordinator` | `StateDirectory=heimdall/coordinator` | coordinator election state (`--state-dir`). A coordinator that cannot write its term file never claims leadership; static mode keeps working |
| `/var/log/heimdall` | `LogsDirectory=heimdall` | `coordinator.log`, `router.log` |
| `/run/heimdall-federation` | `RuntimeDirectory=heimdall-federation`, `RuntimeDirectoryPreserve=yes` | `coordinator.pid`, `router.pid`. Shared by both units and kept across restarts |

Both units have `ConditionPathExists=/etc/heimdall/federation.ini`, plus
`Restart=always` and `RestartSec=3`. They run
`federation_start.sh <service> --foreground` with explicit `--config`,
`--log-dir` and `--pid-dir` flags, and `--state-dir` for the coordinator.
Flags win over the `HEIMDALL_FED_*` variables in
`/etc/default/heimdall-federation`. The coordinator and the router are not
started by any DAQ unit, and no DAQ unit depends on them.

## 8. udev rule (optional)

```sh
sudo /opt/heimdall/util/install.sh --extras-only --udev
```

This installs `99-heimdall-rtlsdr.rules`
(`SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0664"`)
and reloads udev. It lets members of `plugdev` use `rtl_test`/`rtl_eeprom`
without root. The chain itself runs as root and does not need the rule.

## 9. Log rotation

`/etc/logrotate.d/heimdall` rotates, at 50 MB, keeping 7 compressed copies,
with `copytruncate` (every process keeps its log file open):

```
/opt/heimdall/Firmware/_logs/*.log                   instance 0
/opt/heimdall/Firmware/_logs/inst*/*.log
/opt/heimdall/instances/*/Firmware/_logs/*.log       instance directories
/opt/heimdall/instances/*/Firmware/_logs/inst*/*.log
/opt/heimdall/Firmware/_logs/federation/*.log        federation_start.sh run by hand
/var/log/heimdall/*.log                              federation units
```

This policy is separate from the start scripts' own per-start archive. Each
start moves the previous run's logs to `_logs[/instN]/archive/<timestamp>/`
and keeps the last 10 archives. logrotate does not touch those.

## Notes

* The DAQ units run as root because the chain requires it (USB access,
  `chrt -f 99`, `mlockall`, i2c), the same as running the scripts with sudo
  by hand. Only the federation services run unprivileged.
* `KillMode=control-group`: when a unit stops, systemd also removes any
  process left in the unit's cgroup after the stop scripts have run.
* Troubleshooting a supervised unit: `systemctl status heimdall-daq-supervised@1`
  shows the last exit code, which names the stage (see the table in
  section 2). `journalctl -u heimdall-daq-supervised@1` has the
  supervisor's `stage <role> (pid P) died` line. The stage logs are in
  `/opt/heimdall/instances/1/Firmware/_logs/inst1/`.
* A unit that does not start and logs `Condition check ... was not met` is
  missing its INI (instance not provisioned) or `rtl_daq.out` (C core not
  built).
