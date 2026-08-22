# systemd units for the HeIMDALL DAQ firmware

Two units are provided:

| Unit | Purpose |
|---|---|
| `heimdall-daq.service` | The normal single-instance (instance 0) chain |
| `heimdall-daq@.service` | Optional per-federation-instance template |

Both wrap the existing shell scripts (`daq_start_sm.sh` / `daq_stop.sh`) so
the tiered SCHED_FIFO priorities and CPU affinity stay exactly where they are
defined today - in the start script. The units only grant the required limits
(`LimitRTPRIO=99`, `LimitMEMLOCK=infinity`) and manage lifecycle/cleanup.

## Install (single instance)

```sh
# 1. Deploy the firmware (source tree with built binaries) to /opt/heimdall
sudo mkdir -p /opt/heimdall
sudo cp -r Firmware util config_files /opt/heimdall/

# 2. Install and enable the unit
sudo cp packaging/systemd/heimdall-daq.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now heimdall-daq

# 3. (Optional) log rotation
sudo cp packaging/logrotate/heimdall /etc/logrotate.d/heimdall
```

Logs stay where the scripts put them: `/opt/heimdall/Firmware/_logs/*.log`
(instance 0) - `journalctl -u heimdall-daq` only shows the start/stop script
output.

If the firmware lives somewhere other than `/opt/heimdall`, edit
`WorkingDirectory=`/`ExecStart=`/`ExecStop=` accordingly (and the logrotate
paths).

## Per-instance template (federation)

`daq_start_sm.sh` reads the instance id from `daq_chain_config.ini`
(`[federation] instance_id`), not from its command line, so every federation
instance needs its own working copy:

```
/opt/heimdall/instances/1/Firmware/   with [federation] instance_id = 1
/opt/heimdall/instances/2/Firmware/   with [federation] instance_id = 2
```

Then:

```sh
sudo cp packaging/systemd/heimdall-daq@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now heimdall-daq@1
```

The `%i` instance suffix must match the `instance_id` configured in that
copy's ini - `ExecStop` passes it to `daq_stop.sh %i`, which kills via the
per-instance PID files in `_logs/inst<N>/pids/`.

## Notes

- The units run as root because the chain requires it (USB, chrt -f 99,
  mlockall, i2c) - same as invoking the scripts with sudo by hand.
- `Type=oneshot` + `RemainAfterExit=yes`: the start script launches the
  pipeline processes in the background and exits; the processes stay in the
  unit's cgroup, so `systemctl stop` first runs `daq_stop.sh` (graceful
  SIGTERM, 2 s grace, SIGKILL sweep) and then cleans up any survivor.
- Do not add `CPUAffinity=` to the unit: it would constrain the taskset
  pinning the start script performs per process.
