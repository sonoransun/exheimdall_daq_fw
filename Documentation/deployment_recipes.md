# Deployment Recipes

This page gives end-to-end, copy-pasteable recipes for the common HeIMDALL DAQ deployments.
Each recipe lists the hardware, the exact config, the start and stop commands, and ends
with a health checklist. Every feature beyond the single-instance default is **opt-in**:
nothing changes until you set the key or pass the flag that the recipe names.

| Recipe | Host | Instances | Output | Opt-in features used |
|---|---|---|---|---|
| [A: single DoA station](#recipe-a-single-doa-station-on-a-raspberry-pi-4) | Raspberry Pi 4 | 1 | shmem (co-located DSP) or eth (remote DSP) | status server, `ctl_max_connections` |
| [B: passive-radar station](#recipe-b-passive-radar-station) | Pi 4 or x86 | 1 | shmem or eth | decimation (`decimation_ratio` >= 2) |
| [C: two KrakenSDRs on one host](#recipe-c-two-krakensdrs-on-one-x86-host) | 8-core x86, two xHCI controllers | 2 | eth or shmem | serial keys, instance dirs, `[host]` section, registry |
| [D: multi-site networked DF](#recipe-d-multi-site-networked-df) | several hosts | 1-2 per host | eth into IQ routers | membership, coordinator election, routers, auth |
| [E: hardware-free rehearsal](#recipe-e-hardware-free-rehearsal) | any Linux (CI: Linux or macOS) | 1 | eth | synthetic source, record/replay |
| [F: systemd supervised deployment](#recipe-f-systemd-supervised-deployment) | any of the above | 1 or more | any | supervised units, watchdog |

Related documents: [scaling_and_clustering.md](scaling_and_clustering.md) (model and limits),
[networking_and_time_sync.md](networking_and_time_sync.md) (ports, binds, firewall, clocks),
[operations_runbook.md](operations_runbook.md) (start/stop forms, exit codes, symptoms),
[capacity_planning.md](capacity_planning.md) (sizing worksheet),
[hardware_setup.md](hardware_setup.md) (USB, serials, LNAs, rotators),
[consumer_guide.md](consumer_guide.md) (reading the IQ stream),
[configuration.md](configuration.md) (every key and default).

---

## Common prerequisites

Do these steps once per host before any recipe.

**How to apply an INI snippet.** The snippets in this page list keys to set. Every section
they name already exists in the shipped `Firmware/daq_chain_config.ini`. Change the existing
key in its section, or add the key under the existing section header. Never append a second
copy of a section: `configparser` then raises `DuplicateSectionError`, `ini_checker.py`
exits 1 and the start script refuses to run.

### Build and install

`util/install.sh` installs the build dependencies, builds the krakenrf `librtlsdr` fork and
the FIR engine library (KFR on x86_64 and aarch64, Ne10 on 32-bit armv7l), installs the
Python packages with `pip`, and runs `make` in `Firmware/_daq_core`. Run it as a normal user;
it calls `sudo` itself.

```bash
git clone <your heimdall repository URL> heimdall
cd heimdall
bash util/install.sh
```

Rebuild by hand after every update. The C binaries and the Python modules ship together, so
a Python-only update is not a complete update.

```bash
cd Firmware/_daq_core
make -j"$(nproc)"                 # ENGINE=auto: kfr on x86_64/aarch64, ne10 on armv7l
make -j"$(nproc)" ENGINE=generic  # no DSP library needed (slower FIR)
```

| Make variable | Values | Use |
|---|---|---|
| `ENGINE` | `auto` (default), `kfr`, `ne10`, `generic` | FIR engine |
| `BUILD` | `release` (default), `debug`, `asan` | optimisation and sanitizers |
| `MARCH` | unset (native tuning), `portable`, or explicit flags | build on the target host with the default |

Check the build without hardware:

```bash
cd ..                      # from Firmware/_daq_core back to Firmware/
./unit_test.sh --ci        # exit 0 = every module passed; missing optional deps SKIP
python3 ini_checker.py no_hw
```

### Install root layout

The recipes use one built tree per host at `/opt/heimdall`. Instance 0 runs in
`/opt/heimdall/Firmware`. Every further instance N runs in its own instance directory
`/opt/heimdall/instances/<N>/Firmware` (Recipe C).

```bash
sudo mkdir -p /opt/heimdall
sudo cp -r Firmware util config_files packaging /opt/heimdall/
```

Every stage reads `daq_chain_config.ini` and `_data_control/` relative to its working
directory. Always start and stop a chain from its own `Firmware/` directory.

### Python environment and sudo

The start scripts call `python3` and run some stages through `sudo`. `sudo` replaces `PATH`
with its `secure_path`, so a conda or miniforge environment (the project convention is an
env named `kraken`) is not used unless you pass `PATH` through:

```bash
conda activate kraken
cd /opt/heimdall/Firmware
sudo env "PATH=$PATH" ./daq_start_sm.sh
```

With the system `python3` that `util/install.sh` uses, plain `sudo ./daq_start_sm.sh` works.

### heimdall-ctl

`heimdall-ctl` is the operator CLI for a running chain. It is client-only and needs no root.
Use any of these forms:

```bash
pip install .                                  # from the repo root: installs the heimdall-ctl console script
util/bin/heimdall-ctl status                   # in a source tree
PYTHONPATH=util python3 -m heimdall_ctl status # in any tree
```

Run it inside an instance's `Firmware/` directory and it reads that `daq_chain_config.ini`
(instance id and port stride). Elsewhere, pass `--instance N` (ports move by `N x 100`) or
`--config PATH`. Exit codes: `0` ok, applied or unconfirmed (legacy server), `1` error or
not applied, `2` usage, `3` connection failure or timeout, `4` `not_leader`, `5` capacity
failures.

```text
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

### Receiver serials

`rtl_daq` opens channel i as the receiver whose EEPROM serial is `serial_base + i`. With the
key absent, `serial_base` behaves as `1000`, so a factory KrakenSDR (serials 1000-1004) works
unchanged. For several units on one host or in one cluster, program unit k to
`1000 + 100*k + i` and give the instance that owns unit k `[hw] serial_base = 1000 + 100*k`.
Set `[daq] ctr_channel_serial_no` to one of the resolved serials (normally the first);
`ini_checker` rejects any other value. `provision_instance.py` and `cluster_cfg_gen.py` set
it for you: they keep the control channel on the same channel index within the instance's
serials (1000 becomes 1100 for unit 1).

```ini
[hw]
serial_base = 1100
; or list every channel explicitly; this wins over serial_base:
;device_serials = 1100,1101,1102,1103,1104

[daq]
ctr_channel_serial_no = 1100
```

Program a unit with `util/set_unit_serials.sh`. Plug in **one unit at a time**: the tool
refuses (exit 4) when the number of RTL2832U devices differs from `--count` (default 5).

<!-- verify-after: WP-14 -->
```bash
sudo util/set_unit_serials.sh --list                 # idx  bus:dev  sysfs-serial  eeprom-serial  state
sudo util/set_unit_serials.sh --unit 1 --dry-run     # prints the plan: 1000..1004 -> 1100..1104
sudo util/set_unit_serials.sh --unit 1 --yes         # writes, re-reads and verifies (exit 7 on mismatch)
# Power-cycle the unit afterwards.
```

| Exit | Meaning |
|---|---|
| 0 | done, or plan printed (dry run / no `--yes`) |
| 2 | usage error |
| 3 | a DAQ chain (`rtl_daq.out` or a live registered instance) or another program (for example an open SDR application) holds the receivers |
| 4 | device count differs from `--count` |
| 5 | `rtl_eeprom` not on `PATH` |
| 6 | old serials are not a contiguous range (override with `--force`) |
| 7 | verification after writing failed |

Without that tool, use `rtl_eeprom` from librtlsdr directly. It prints the current serial on
stderr (`Serial number:` line) and asks for confirmation before writing. Keep the factory
channel order: new serial = old serial + 100*k.

```bash
for j in 0 1 2 3 4; do rtl_eeprom -d "$j" 2>&1 | grep 'Serial number'; done
sudo rtl_eeprom -d 0 -s 1100      # repeat per device with old serial + 100
```

Check which serials the host sees. This reads sysfs only and opens no device:

```bash
python3 Firmware/_daq_core/hw_discover.py --sdr
```

```json
{
  "rtlsdr": {
    "count": 0,
    "devices": [],
    "sysfs": false
  }
}
```

(That output is from a host without sysfs. On Linux each device entry carries `busnum`,
`devnum`, `path` and `serial`.) Before a hardware start, compare the INI with the attached
receivers:

```bash
cd /opt/heimdall/Firmware
python3 ini_checker.py no_hw attached
```

With serial keys set, a missing or duplicated serial is an error, for example:

```text
ERROR:root:configured serial 1103 (channel 3) is not attached (sysfs); plug in the unit or fix [hw] serial_base/device_serials
ERROR:root:serial 1000 (channel 0) is present on 2 devices (/sys/bus/usb/devices/1-1.1, /sys/bus/usb/devices/3-1.1); plug in one unit at a time and program distinct serials with util/set_unit_serials.sh --unit 1
```

### Enable the status server for health checks

The health checklists read `:5002` STATUS. It is off in the shipped INI. Turn it on in every
recipe:

```ini
[monitoring]
en_monitoring = 1
en_status_server = 1
; STATUS refreshes every heartbeat_interval frames (100 = about 44 s at the
; default 0.437 s frame period). A smaller value gives a fresher view.
heartbeat_interval = 10
```

`pipeline_health` in STATUS is `ok` when `sync_state >= 5` and no frame was dropped in the
last `drop_window_sec` (60 s), `degraded` when `sync_state >= 2`, and `error` otherwise. With
the opt-in `[monitoring] status_extended = 1` it can also be `stale`.
<!-- verify-after: WP-06 -->

### systemd

Supervision is optional and described in [Recipe F](#recipe-f-systemd-supervised-deployment).

---

## Recipe A: single DoA station on a Raspberry Pi 4

One KrakenSDR, one instance, one DoA DSP. A Pi 4 has 4 cores, and each hardware instance
pins 4 real-time tiers, so a Pi 4 (or Pi 5) runs exactly one hardware instance.

### Choose the output interface

| | `shmem` (shipped default) | `eth` |
|---|---|---|
| DSP location | same host | any host |
| Transport | shared memory `delay_sync_iq` + FIFOs | TCP `:5000` (`streaming` / `IQDownload`) |
| `iq_server.out` | not started | started |
| Start-up | **`delay_sync.py` blocks until the DSP attaches** (see below) | runs without a client |
| Egress at defaults | none on the network | 96 MB/s per client (5 ch x 2.4 MS/s x 8 B) |

**Consumer attach requirement (shmem).** `delay_sync.py` opens its output FIFO
`_data_control/fw_delay_sync_iq` with a blocking open. Until a consumer opens it, the sync
FSM does not run, `decimate.out` drops frames at its output, and the status server is not
started (`heimdall-ctl status` reports `Connection refused`, exit 3). Start the DSP, or use
`eth` when you want the chain to come up on its own. The
[directional DF tutorial](tutorial_directional_df.md) uses the `directional_df` preset,
which ships `shmem`, so the same rule applies there.

**Egress (eth).** A Pi 4 has one gigabit link (125 MB/s). 96 MB/s is 77 % of it. For a
remote DSP over a shared or slower link, reduce the rate with decimation
([Recipe B](#recipe-b-passive-radar-station) shows the keys).

### Configure

Edit the shipped `Firmware/daq_chain_config.ini` in place (see
[How to apply an INI snippet](#common-prerequisites)). It is not the same file as
`config_files/kraken_default/daq_chain_config.ini`, which lacks the `[monitoring]` and
`[federation]` sections.

Co-located DSP:

```ini
[data_interface]
out_data_iface_type = shmem

[daq]
center_freq = 433920000
ctr_channel_serial_no = 1000
; A resident DSP holds the :5001 control connection. With 4 slots heimdall-ctl
; and a coordinator can still connect.
ctl_max_connections = 4

[monitoring]
en_monitoring = 1
en_status_server = 1
heartbeat_interval = 10
```

Remote DSP: as above, but change `out_data_iface_type` to `eth` and add these `[daq]` keys:

```ini
[data_interface]
out_data_iface_type = eth

[daq]
; :5000-:5003 bind here (default 0.0.0.0)
listen_address = 0.0.0.0
; keep rtl_daq's :1130 tuner socket local (accepted: *, 0.0.0.0, 127.0.0.1, localhost)
zmq_listen_address = 127.0.0.1
```

**Restrict the ports (eth).** `:5000` to `:5003` have no authentication. `:5001` accepts
mutating commands (tune, gain, bias-tee, recalibration) and `:5000` serves the IQ stream to
anyone who connects. Allow them only from the DSP host. With `listen_address = 0.0.0.0`,
use a firewall rule (DSP host `192.168.1.50` in this example):

```text
table inet heimdall {
  chain input {
    type filter hook input priority 0; policy accept;
    iif "lo" accept
    ip saddr 192.168.1.50 tcp dport 5000-5003 accept
    tcp dport { 1130, 5000-5003 } drop
  }
}
```

Alternatively set `listen_address` to the address of the interface that faces the DSP. Then
`127.0.0.1` stops working for `:5000` to `:5003`, so run `heimdall-ctl --host <that
address>`. Details: [networking_and_time_sync.md](networking_and_time_sync.md) (bind
addresses and firewall examples) and [Recipe D, Firewall](#4-firewall).

Validate. Exit 0 means valid; problems that do not block start print as `WARNING:` lines.

```bash
cd /opt/heimdall/Firmware
python3 ini_checker.py no_hw
```

### Start and stop

```bash
cd /opt/heimdall/Firmware
sudo ./daq_start_sm.sh            # or: sudo env "PATH=$PATH" ./daq_start_sm.sh
# ... start the DSP (shmem) or point the remote DSP at <pi>:5000 and <pi>:5001 (eth)
sudo ./daq_stop.sh 0              # stops instance 0 through its PID files
```

The no-argument `./daq_stop.sh` is a host-wide stop of every DAQ process. On a single-instance
host it is equivalent.

| Start exit | Meaning |
|---|---|
| 0 | started |
| 1 | config or FIR-designer failure, or ports still busy after 10 attempts |
| 3 | guard refusal (only with `[host] instance_guard = refuse` or `allow_core_sharing = refuse`) |
| 4 | strict pre-flight failure (only with `[host] preflight = strict`) |

<!-- verify-after: WP-12 -->

Logs: `_logs/rtl_daq.log`, `rebuffer.log`, `decimator.log`, `delay_sync.log`, `hwc.log`,
`iq_server.log` (eth only). PID files: `_logs/inst0/pids/*.pid`.

### Health checklist (Recipe A)

1. Config and receivers:
   ```bash
   python3 ini_checker.py no_hw attached; echo $?    # 0
   ```
2. Listeners. Expect `:5001` and `:5002`, plus `:5000` with eth:
   ```bash
   ss -ltnp | grep -E ':(5000|5001|5002|1130) '
   ```
3. Pipeline. Expect `Pipeline: ok`, `Calibration: locked` (sync 5 or higher) and no drops.
   Allow one STATUS refresh after start.
   ```text
   $ heimdall-ctl status
   Pipeline: ok  Sync: 6  Freq: 433.920 MHz  Uptime: 412s
   Calibration: locked  Aggregate power: -41.7 dB
   Dropped frames (total): 0  (recent: 0)
   ```
   For scripts:
   ```bash
   heimdall-ctl --json status | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["pipeline_health"]=="ok" else 1)'
   ```
4. Control plane. Expect `fsm IQ_CAL` once the chain is calibrated:
   ```text
   $ heimdall-ctl hwc-status
   fsm     pend  noise  agc  nch  freq         frame-age  frames  term
   IQ_CAL  0     0      0    4    433.920 MHz  0.004s     0       0
   ```
   (Output from a 4-channel test chain; a KrakenSDR shows `nch 5`.)
5. A mutating command round trip. Expect `applied (...)`:
   ```bash
   heimdall-ctl tune 433920000
   ```
   `NOT applied: deferred_timeout (fsm=...)` means the FSM did not reach `IQ_CAL` in time.
   This error (exit 3) means another client holds the only control slot:
   ```text
   Error: 127.0.0.1:5001: server did not answer XACK within 3 s (another client may hold the control port); command not sent - retry, or use --ack legacy to queue it behind that client
   ```
   Raise `[daq] ctl_max_connections`.
6. Logs have no `ERROR` or `FATAL` lines:
   ```bash
   grep -iE 'error|fatal' _logs/*.log
   ```

---

## Recipe B: passive-radar station

The DAQ side of a passive radar is the same coherent chain as DoA. The differences are the
sample-rate, decimation and CPI choices. The firmware calibrates and delivers all channels
the same way. **Which channel is the reference and which are surveillance channels is
decided by the downstream DSP.** `[calibration] std_ch_ind` only selects the calibration
reference channel; it does not assign radar roles.

### Choose the rates

Notation: `fs` = `[daq] sample_rate`, `R` = `[pre_processing] decimation_ratio`,
`Ncpi` = `[pre_processing] cpi_size` (samples per channel after decimation).

| Quantity | Formula | Example below |
|---|---|---|
| Output sample rate | `fs / R` | 2.4 MS/s / 2 = 1.2 MS/s |
| Frame period (CPI duration) | `T_fp = Ncpi x R / fs` | 524288 x 2 / 2.4e6 = 0.437 s |
| Doppler resolution of one CPI | about `1 / T_fp` | about 2.3 Hz |
| FIR cut-off (normalised) | `fir_relative_bandwidth / R` | 0.8 / 2 = 0.4 |
| Egress per `:5000` client | `num_ch x fs x 8 / R` B/s | 5 x 2.4e6 x 8 / 2 = 48 MB/s |

Example INI section values (valid: `ini_checker.py no_hw` exits 0 with no warning):

```ini
[daq]
sample_rate = 2400000
daq_buffer_size = 262144

[pre_processing]
cpi_size = 524288
decimation_ratio = 2
fir_tap_size = 64
fir_relative_bandwidth = 0.8
fir_window = hann

[data_interface]
; shmem for a co-located DSP, eth for a remote one (Recipe A explains the difference)
out_data_iface_type = eth
```

`fir_filter_designer.py` regenerates the coefficients at every start, so changing `R`,
`fir_tap_size` or `fir_relative_bandwidth` needs only a restart.

### Limits the checker enforces

| Rule | Checked by | Result |
|---|---|---|
| `fir_tap_size > decimation_ratio` when `decimation_ratio > 1` | ini_checker, fir_filter_designer | error, no start |
| `cpi_size x decimation_ratio >= daq_buffer_size` | ini_checker | error |
| `cpi_size <= 2^23`, `corr_size <= 2^23` | ini_checker | error |
| `daq_buffer_size` is a power of 2 | ini_checker | error |
| `T_fp > 8 s` | ini_checker | `WARNING:` (control replies, status refreshes and stall detection all wait frames) |
| `[daq] ctl_request_timeout_sec` (default 10) `< 2 x T_fp` | ini_checker | `WARNING:` (control verbs will time out on a healthy chain) |
| `corr_size` not a multiple of 1024 | ini_checker | `WARNING:` |

Longer CPIs give finer Doppler resolution and slower control. Commands execute at frame
boundaries, so at `cpi_size = 2097152` and `R = 2` (`T_fp` = 1.75 s) a `tune` takes seconds.

### Start, stop and health checklist (Recipe B)

Start and stop as in [Recipe A](#start-and-stop). Run the
[Recipe A checklist](#health-checklist-recipe-a), then:

1. Confirm the FIR designer used your values. The start output prints `Decimation ratio: 2`,
   `Tap size: 64`, `FIR filter ready`.
2. Confirm the frame period. `frame_count` in `heimdall-ctl --json status` should advance by
   about `1 / T_fp` per second (2.3 per second in the example).
3. With eth, confirm the egress fits the link (48 MB/s in the example).

---

## Recipe C: two KrakenSDRs on one x86 host

Two units, two instances (0 and 1), one 8-core host. Each instance gets its own receivers
(serial keys), its own working directory, its own ports (`+100` for instance 1), its own
4 cores, and its own stop scope.

### Hardware rules

| Resource | Need per hardware instance | Check |
|---|---|---|
| CPU cores | 4 (one per RT tier) | `nproc` >= 8 for two instances |
| USB 2.0 high-speed bus | 24 MB/s (5 ch x 2.4 MS/s x 2 B) | the capacity model budgets 35 MB/s per bus, so **two units on one bus do not fit**. Put each unit on its own xHCI controller (`lsusb -t`, `hw_discover.py --sdr` shows `busnum`) |
| Locked memory (rtl_daq ring) | 20 MiB at defaults | `ulimit -l` unlimited under root |

See [capacity_planning.md](capacity_planning.md) for the full worksheet and
[hardware_setup.md](hardware_setup.md) for USB topology.

### 1. Program the serials

Unit 0 keeps the factory serials 1000-1004. Unit 1 becomes 1100-1104. Plug in unit 1 alone:

```bash
sudo /opt/heimdall/util/set_unit_serials.sh --unit 1 --yes
# power-cycle unit 1, then plug both units in
```
<!-- verify-after: WP-14 -->

### 2. Provision the instances

`util/provision_instance.py` creates the instance directory and writes the instance INI.
It is a dry run unless you pass `--write`. **On a multi-instance host, provision instance 0
too, with `--in-place`**, so it carries the same registry and guard keys.

Set the output interface in `Firmware/daq_chain_config.ini` first. That file is the default
template for every instance, so no separate template is needed. The tool sets
`ctr_channel_serial_no` itself.

**Decide on supervision now.** If you plan to run the instances under systemd
([step 5](#5-supervise-optional)), add `--systemd` to both provisioning lines.

```bash
cd /opt/heimdall
sudo sed -i 's/^out_data_iface_type = .*/out_data_iface_type = eth/' Firmware/daq_chain_config.ini

sudo python3 util/provision_instance.py --instance 0 --in-place --write   # add --systemd for step 5
sudo python3 util/provision_instance.py --instance 1 --write              # add --systemd for step 5
```
<!-- verify-after: WP-14 -->

Instance 1's directory (`/opt/heimdall/instances/1`) has symlinks to the shared code
(`_daq_core`, `_testing`, `_calibration`, `_fpga_gateware`, the scripts, `util`,
`config_files`) and private real `daq_chain_config.ini`, `_data_control/`, `_logs/` and
`_db/`. `--mode copy` makes a full copy instead of symlinks.

Keys written with `--template` when you pass no flag for them:

| Key | Instance 0 (`--in-place`) | Instance 1 |
|---|---|---|
| `[federation] instance_id` / `port_stride` | 0 / 100 | 1 / 100 |
| `[federation] node_id` / `cluster_id` | `<short hostname>:0` / `default` | `<short hostname>:1` / `default` |
| `[hw] serial_base` | 1000 | 1100 |
| `[hw] unit_id` | `auto` (header `unit_id` = instance id) | `auto` |
| `[daq] ctr_channel_serial_no` | same channel index in 1000-1004 (1000 in the shipped INI) | same channel index in 1100-1104 (1100) |
| `[daq] ctl_max_connections` | 4 | 4 |
| `[host] cpu_cores` | `0,1,2,3` | `4,5,6,7` |
| `[host] rt_priority_offset` | 0 | 0 |
| `[host] instance_registry` / `instance_guard` | 1 / `refuse` | 1 / `refuse` |
| `[host] allow_core_sharing` | `refuse` | `refuse` |
| `[host] host_tuning` | `auto` (`0` with `--systemd`) | same |
| `[host] preflight` | `warn` | `warn` |
| `[host] health_thread_sched_other` | 1 | 1 |
| `[monitoring] en_monitoring, en_status_server, status_extended, status_update_interval, event_tag_identity` | 1, 1, 1, 10, 1 | same |

After writing, the tool runs `ini_checker.py daq_chain_config.ini no_hw` in the new
`Firmware/` and exits 1 on errors (the files stay for inspection). Add `--check-attached`
on the target host with the units plugged in to run the serial checks as well.

| Exit | Meaning |
|---|---|
| 0 | ok (or dry run ok) |
| 1 | validation failed |
| 2 | usage (for example `--instance 0` without `--in-place`) |
| 3 | destination exists (without `--force` / `--refresh`) |
| 4 | instance is running |

The instance-aware files need the C build of this release: instance 1's `decimate.out`
reads `_data_control/inst1_fir_coeffs.txt`, which `fir_filter_designer.py` writes for
instance 1. Run `make` in `Firmware/_daq_core` after every update.

### 3. Validate

```bash
cd /opt/heimdall/Firmware             && python3 ini_checker.py no_hw attached
cd /opt/heimdall/instances/1/Firmware && python3 ini_checker.py no_hw attached
python3 daq_preflight.py --config /opt/heimdall/Firmware/daq_chain_config.ini \
                                  /opt/heimdall/instances/1/Firmware/daq_chain_config.ini
```

Pass one `--config` followed by every INI. A second `--config` flag replaces the first, and
then only one instance is checked. `heimdall-ctl capacity --config A B` builds the same
command.
<!-- verify-after: WP-13 -->

The pre-flight tool prints `need / have / verdict` per resource (cores, core overlap, memory,
`/dev/shm`, memlock, USB bus load, NIC egress, ports, frame period). Exit 0 ok, 1 warnings,
2 failures. The core-count rule fails when `4 x hardware instances > nproc`.

### 4. Start and stop

Start each instance from its own directory:

```bash
cd /opt/heimdall/Firmware             && sudo ./daq_start_sm.sh
cd /opt/heimdall/instances/1/Firmware && sudo ./daq_start_sm.sh
```

Stop one instance from its own directory. **Never use the no-argument `./daq_stop.sh` on a
multi-instance host**: it stops every DAQ process on the host.

```bash
cd /opt/heimdall/instances/1/Firmware
sudo ./daq_stop.sh 1                 # this instance only (PID files)
sudo ./daq_stop.sh 1 --clean         # and remove its FIFOs, shm segments and relay files
```

<!-- verify-after: WP-12 -->
With `instance_guard = refuse` (written by the provisioning tool):

- a second start of the same instance from the same directory stops and restarts it;
- a start of the same instance id from a different directory exits 3
  (`instance 1 is already running from <dir> (use --takeover to stop it)`);
- a busy-port round at start runs the scoped `./daq_stop.sh 1`, never the host-wide stop.

Whatever the guard setting, `daq_stop.sh` refuses `--clean` and `--reset-fence` (exit 3)
while a process of that instance is still alive. `daq_stop.sh 1 --clean` keeps
`iq_track_lock`, the FIR coefficients and `fence_term`.

### 5. Supervise (optional)

Supervision needs instances provisioned with `--systemd`. That flag writes
`/etc/default/heimdall-daq` (instance 0) or `/etc/default/heimdall-daq@<N>` and sets
`[host] host_tuning = 0`. The start script then applies no host tuning, so the
`heimdall-host-tuning` unit must run. Without it, `sched_rt_runtime_us`, the IRQ tuning and
`usbfs_memory_mb` are never applied.

If you provisioned in step 2 without `--systemd`, re-run with it:

```bash
cd /opt/heimdall
sudo python3 util/provision_instance.py --instance 0 --in-place --systemd --write
sudo python3 util/provision_instance.py --instance 1 --systemd --force --write
```
<!-- verify-after: WP-14 -->

Instance 0 needs no extra flag: `--in-place` writes a backup
`daq_chain_config.ini.bak.<time>` and rewrites the INI. Instance 1 exists already, so
without `--force` the tool exits 3. `--force` re-creates the code links and regenerates the
instance INI from the template; the old INI is saved as `daq_chain_config.ini.bak.<time>`
(merge any hand edits back), and `_data_control/`, `_logs/` and `_db/` are kept.

Install the units as in [Recipe F](#recipe-f-systemd-supervised-deployment), then enable
host tuning and one supervised unit per instance:

```bash
sudo systemctl enable --now heimdall-host-tuning heimdall-daq-supervised heimdall-daq-supervised@1
```
<!-- verify-after: WP-16 -->

### Health checklist (Recipe C)

1. Both instances are registered and alive:
   ```bash
   heimdall-ctl instances --ping       # id mode launcher cwd ports.status pids-alive started (+ PING latency)
   ```
   With no entries it prints `no registry entries in <dir> (only instances started with
   [host] instance_registry = 1 are listed)` and exits 0.
   <!-- verify-after: WP-12 -->
2. Each instance on its own ports and cores:
   ```bash
   heimdall-ctl --instance 0 ports
   heimdall-ctl --instance 1 ports
   ss -ltnp | grep -E ':(5000|5001|5002|5100|5101|5102) '
   for p in $(cat /opt/heimdall/instances/1/Firmware/_logs/inst1/pids/*.pid); do taskset -cp "$p"; done
   ```
   Instance 1's stages should report cores within `4-7`.
3. Each instance healthy (run the [Recipe A checklist](#health-checklist-recipe-a) per
   instance):
   ```bash
   heimdall-ctl --instance 0 status
   heimdall-ctl --instance 1 status
   heimdall-ctl --instance 1 hwc-status
   ```
4. Capacity with both instances live:
   ```bash
   heimdall-ctl capacity               # exit 0 ok/warnings, 5 failures, 3 tool not found
   ```
   <!-- verify-after: WP-13 -->
5. No USB overruns. The cumulative overrun count is stamped in IQ header slot 102 on every
   frame. The extended STATUS (`status_extended = 1`, written by the provisioning tool)
   reports it as `counters.buffer_overrun_cnt`. Read it twice, a minute apart, and compare:
   ```bash
   heimdall-ctl --instance 1 --json status | python3 -c 'import json,sys; print(json.load(sys.stdin)["counters"]["buffer_overrun_cnt"])'
   ```
   A growing count means the bus or the ring is too small (raise
   `[daq] usb_ring_buffers`, default 8, range 2..64) or the two units share a bus.

---

## Recipe D: multi-site networked DF

Several hosts, each with one or more units, coordinated as one cluster. Each site computes
its own coherent IQ. **Coherence exists only inside one unit**: frames from different units
are not phase-aligned. Cross-site work (bearing fusion, TDOA) is done by the central DSP
using the header `time_stamp` (Unix epoch, milliseconds, taken by `rtl_daq`), so the hosts'
clocks must be synchronised.

### Topology

```text
 site A: hosta (8-core x86)          site B: hostb (Pi 4)           ops: hostc
 +---------------------------+       +----------------------+       +-----------------------------+
 | inst 0  :5000-:5003       |       | inst 0  :5000-:5003  |       | coordinator :6000 (cand. 3) |
 | inst 1  :5100-:5103       |       | router  :7000/:7001  |       | central router :7000/:7001  |
 | router  :7000/:7001       |       |  --tag-mode node     |       |  sources router://hosta:7000|
 |  --tag-mode node          |       | coordinator :6000    |       |          router://hostb:7000|
 | coordinator :6000         |       +----------+-----------+       | central DSP -> hostc:7000   |
 +-------------+-------------+                  |                   +--------------+--------------+
               +------------------ cluster network / WireGuard ------------------+
```

| Choice | Value | Why |
|---|---|---|
| Instance ids | 0..k-1 per host, unique per host | ports are per host; the node key `host:instance_id` is cluster-unique |
| Port stride | 100 | with stride 100, use instance ids 0-9 per host; id 10 collides with `:6000` |
| Coordinator candidates | three (`hosta:6000,hostb:6000,hostc:6000`) | a quorum (`require_quorum = 1`) needs at least 3 candidates |
| Routers | one per site host with `--tag-mode node`, one central router chaining them | by default (`[data_interface] iq_server_max_clients = 1`) each `iq_server` serves one client, so the site router is the fan-out point; node tagging lets the central router tell sources apart |
| Output | `out_data_iface_type = eth` on every instance | routers read `:5000 + offset` |

### 1. Generate the configs

`util/cluster_cfg_gen.py` writes every instance INI, the `federation.ini` files, a port
table and a provisioning script, and validates all of them. Run it on one host (here hosta).
`/opt/heimdall` and `/root` are root-owned, so every write uses `sudo`.

The generator copies the template's other keys into every instance INI. Set the output
interface and the loopback bind of rtl_daq's `:1130` tuner socket in the template first:

<!-- verify-after: WP-14 -->
```bash
cd /opt/heimdall
sudo sed -i -e 's/^out_data_iface_type = .*/out_data_iface_type = eth/' \
            -e 's/^;zmq_listen_address = .*/zmq_listen_address = 127.0.0.1/' Firmware/daq_chain_config.ini
sudo sh -c 'umask 077; head -c 32 /dev/urandom | base64 | tr -d "=+/" > /root/cluster.token'
sudo python3 util/cluster_cfg_gen.py --template Firmware/daq_chain_config.ini \
    --hosts hosta:2 hostb:1 --cores-per-host hosta=8,hostb=4 \
    --coordinators hosta:6000,hostb:6000,hostc:6000 \
    --router hostc:7000 --cluster-id site-north --register \
    --auth-token-file /root/cluster.token --out /root/cluster
```

The second `sed` expression uncomments the shipped `;zmq_listen_address = *` line. Check
with `grep -n '^zmq_listen_address' Firmware/daq_chain_config.ini`.

Output (the tool prints `written: <path>` per file, then `ok: 3 instance(s) on 2 host(s)`):

| Path | Content |
|---|---|
| `cluster/hosta/inst0/daq_chain_config.ini`, `cluster/hosta/inst1/...`, `cluster/hostb/inst0/...` | instance INIs |
| `cluster/federation.ini` | coordinator keys and the central router (`router_sources = router://hosta:7000,router://hostb:7000`); install on hostc |
| `cluster/hosta/federation.ini`, `cluster/hostb/federation.ini` | the same coordinator keys plus that host's site router (`router_sources = hosta:0,hosta:1`, `router_tag_mode = node`, `router_include_self = 0`); install on that host |
| `cluster/ports.md` | per-instance port table (1130/5000-5003/6000/7000/7001) |
| `cluster/provision.sh` | a script with one `case` branch per host; run `sudo sh provision.sh [HOST]` (default: the short host name). `HEIMDALL_ROOT` (default `/opt/heimdall`) and `PYTHON` (default `python3`) override the paths |

Every generated file carries `auth_token` in plain text and is written with mode 0644.
`/root/cluster` itself is readable only by root.

Each instance INI gets: `[federation] instance_id`, `port_stride`, `en_federation = 1`,
`cluster_members` (every instance, real host names), `coordinator_candidates`, `node_id`,
`cluster_id`, `advertise_host`, `register_with_coordinator = 1`, `health_mode`, and
`stamp_node_id = 1` (because `--router` is given); `[hw] serial_base = 1000 + 100 x unit`
with units numbered across the cluster (hosta inst 0 = 1000, hosta inst 1 = 1100, hostb
inst 0 = 1200) and `unit_id = auto`; `[daq] ctl_max_connections = 4`; `[host]
instance_registry = 1`, `instance_guard = refuse`, `cpu_cores` (4 per instance; exit 1 when a
host's plan exceeds `--cores-per-host`); `[monitoring] en_monitoring = 1`,
`en_status_server = 1`, `status_extended = 1`, `status_update_interval = 10`. It never writes
`peer_list`, so each instance's health peers are `cluster_members` minus itself.

It also sets `[daq] ctr_channel_serial_no` to the same channel index within each unit's
serials (1000 in the template gives 1100 and 1200). Any validation error exits 1 and names
the file and the key. Program hostb's unit as unit 2 (`set_unit_serials.sh --unit 2`,
serials 1200-1204).

An instance INI from this recipe looks like this (key excerpt; valid: `ini_checker.py no_hw`
exits 0). `zmq_listen_address` and `out_data_iface_type` come from the template:

```ini
[hw]
serial_base = 1100
unit_id = auto

[daq]
ctr_channel_serial_no = 1100
ctl_max_connections = 4
zmq_listen_address = 127.0.0.1

[data_interface]
out_data_iface_type = eth

[monitoring]
en_monitoring = 1
en_status_server = 1
status_extended = 1
status_update_interval = 10

[federation]
instance_id = 1
port_stride = 100
en_federation = 1
cluster_members = hosta:0,hosta:1,hostb:0
coordinator_candidates = hosta:6000,hostb:6000,hostc:6000
cluster_id = site-north
node_id = hosta:1
advertise_host = hosta
register_with_coordinator = 1
stamp_node_id = 1
auth_token = <contents of /root/cluster.token>

[host]
cpu_cores = 4,5,6,7
instance_registry = 1
instance_guard = refuse
```

### 2. Provision each site host

Run the [Build and install](#build-and-install) and [Install root layout](#install-root-layout)
steps on every host. Do not copy binaries between hosts: hostb (aarch64) and hosta (x86_64)
need different builds, and the default `MARCH` tunes for the build host's CPU, so a binary
copied to another CPU can die with `SIGILL`. Use `MARCH=portable` if you must build once.

Then copy the `cluster/` directory to `/root/cluster` on each site host over SSH (it holds
the token, so keep it root-only) and run the script there. It picks that host's branch:

```bash
# on hosta (on hostb the same command runs hostb's branch)
sudo sh /root/cluster/provision.sh
```

For hosta the script runs:

```bash
python3 /opt/heimdall/util/provision_instance.py --root /opt/heimdall --instance 0 --from-config /root/cluster/hosta/inst0/daq_chain_config.ini --in-place --write
python3 /opt/heimdall/util/provision_instance.py --root /opt/heimdall --instance 1 --from-config /root/cluster/hosta/inst1/daq_chain_config.ini --write
```
<!-- verify-after: WP-14 -->

A host with no planned instances exits 2 (`no instances planned for host ...`). Pass the
host name (`sudo sh provision.sh hosta`) when the short host name differs from the plan.
With a conda env, pass its interpreter: `sudo PYTHON=/opt/miniforge3/envs/kraken/bin/python3
sh /root/cluster/provision.sh`.

`--from-config` copies the INI byte for byte. It refuses key-rewriting flags (exit 2) and an
INI whose `instance_id` differs from `--instance` (exit 2).

The instance INIs now hold `auth_token`. Keep them unreadable for other users, but readable
for the operator group, because `heimdall-ctl` reads the INI in its working directory:

```bash
sudo chgrp <operator group> /opt/heimdall/Firmware/daq_chain_config.ini /opt/heimdall/instances/*/Firmware/daq_chain_config.ini
sudo chmod 640 /opt/heimdall/Firmware/daq_chain_config.ini /opt/heimdall/instances/*/Firmware/daq_chain_config.ini
```

### 3. Synchronise the clocks

Router alignment and every cross-host timestamp comparison assume synchronised wall clocks.
The firmware does not synchronise clocks. Run chrony (NTP) on every host; use PTP
(`linuxptp`) where you need sub-millisecond agreement and the NICs support it.

```bash
sudo apt-get install -y chrony
chronyc tracking        # "System time" offset; keep it far below router_max_skew_ms (500 ms default)
chronyc sources -v
```

The router reports each source's `clock_offset_ms` and sets `skew_warning` when the offset
deviates by more than `--max-skew-ms`. Details: [networking_and_time_sync.md](networking_and_time_sync.md).

### 4. Firewall

The legacy ports (`:1130`, `:5000`, `:5001`, `:5002`, `:5003`) have no authentication.
`auth_token` protects the coordinator (`:6000`) and the router stats port (`:7001`).
`allow_cidrs` also restricts the router output port (`:7000`), which has no authentication:
only `allow_cidrs` and the firewall limit who reads the IQ stream. Restrict everything to the
cluster network. `:1130 + offset` stays on loopback through the `[daq] zmq_listen_address =
127.0.0.1` set in step 1.

| From | To | Port(s) |
|---|---|---|
| coordinator (each candidate) | every instance | `5001 + off` (control), `5002 + off` (status) |
| coordinator candidates | each other | `6000` |
| instances (registrar, health mesh) | coordinators; peer instances | `6000`; `5002 + off` |
| site router | local instances | `5000 + off` |
| central router | site routers | `7000` (and `7001` for `--upstream-stats`) |
| central DSP | central router | `7000` |
| operator workstation | coordinator, routers, instances | `6000`, `7001`, `5002 + off` |

nftables example for hosta (cluster network `10.20.0.0/24`; SSH and other traffic untouched):

```text
table inet heimdall {
  chain input {
    type filter hook input priority 0; policy accept;
    iif "lo" accept
    ip saddr 10.20.0.0/24 tcp dport { 5000-5003, 5100-5103, 6000, 7000, 7001 } accept
    tcp dport { 1130, 1230, 5000-5003, 5100-5103, 6000, 7000, 7001 } drop
  }
}
```

Across sites, carry the cluster network over WireGuard. Examples:
[networking_and_time_sync.md](networking_and_time_sync.md).

### 5. Coordinator and routers

Every host that runs a coordinator or a router reads `/etc/heimdall/federation.ini`.
Install the generated files from step 1; do not write them by hand.

| Host | Install as `/etc/heimdall/federation.ini` |
|---|---|
| hostc (coordinator candidate, central router) | `cluster/federation.ini` |
| hosta (coordinator candidate, site router) | `cluster/hosta/federation.ini` |
| hostb (coordinator candidate, site router) | `cluster/hostb/federation.ini` |

The services run unprivileged as the account you choose: user `heimdall`
([Recipe F](#install) creates it) or your own user. Create the state, log and PID
directories owned by that account on every host that runs a service. Make the file readable
only by root and that account's group (mode 0640), because it holds `auth_token`:

```bash
sudo install -d -o heimdall -g heimdall /var/lib/heimdall/coordinator /var/lib/heimdall/pids /var/log/heimdall
sudo install -d -m 755 /etc/heimdall
sudo install -m 640 -o root -g heimdall /root/cluster/hosta/federation.ini /etc/heimdall/federation.ini   # on hosta
sudo install -m 640 -o root -g heimdall /root/cluster/federation.ini /etc/heimdall/federation.ini         # on hostc
```

hostc runs no instance, so copy only `cluster/federation.ini` there (over SSH, root-only).

The coordinator cannot create `<state_dir>` without a writable parent directory. A
coordinator that cannot write `<state_dir>/term` never claims leadership.

The generator does not write `require_quorum`, `state_dir` or `allow_cidrs`. Add them under
the existing `[federation]` header of each installed file:

```ini
require_quorum = 1
state_dir = /var/lib/heimdall/coordinator
allow_cidrs = 10.20.0.0/24
```

The installed hosta file then reads (comments omitted):

```ini
[federation]
cluster_id = site-north
port_stride = 100
cluster_members = hosta:0,hosta:1,hostb:0
coordinator_port = 6000
coordinator_candidates = hosta:6000,hostb:6000,hostc:6000
auth_token = <token>
router_port = 7000
router_stats_port = 7001
router_tag_mode = node
router_include_self = 0
router_sources = hosta:0,hosta:1
require_quorum = 1
state_dir = /var/lib/heimdall/coordinator
allow_cidrs = 10.20.0.0/24
```

hostc's file differs only in `router_sources = router://hosta:7000,router://hostb:7000`;
hostb's has `router_sources = hostb:0`.

Validate on each host. Exit 0 means valid:

```bash
cd /opt/heimdall/Firmware
python3 ini_checker.py /etc/heimdall/federation.ini --federation
```

Without `auth_token` and `allow_cidrs` the checker warns:

```text
WARNING: auth_token and allow_cidrs are empty and the listen address 0.0.0.0 is not loopback: the coordinator accepts mutating commands from any host
```

Start the services as that account. They run without RT priority or core pinning. By
default `federation_start.sh` writes logs and PID files under `_logs/federation` in the
root-owned `/opt/heimdall/Firmware`, so point them at the directories created above:

<!-- verify-after: WP-12 -->
```bash
cd /opt/heimdall/Firmware
sudo -u heimdall ./federation_start.sh all --config /etc/heimdall/federation.ini \
    --state-dir /var/lib/heimdall/coordinator --log-dir /var/log/heimdall --pid-dir /var/lib/heimdall/pids
sudo -u heimdall ./federation_stop.sh all --pid-dir /var/lib/heimdall/pids
```

`federation_start.sh` exits 0 started, 1 the service exited immediately, 2 usage error, 3
already running (its PID file is live).

Under systemd use `heimdall-coordinator.service` and `heimdall-iq-router.service`
([Recipe F](#recipe-f-systemd-supervised-deployment)). The direct commands are (run them as
the same account, for example with `sudo -u heimdall`):

<!-- verify-after: WP-08 -->
<!-- verify-after: WP-09 -->
```bash
python3 _daq_core/federation_coordinator.py --config /etc/heimdall/federation.ini \
    --state-dir /var/lib/heimdall/coordinator --log-file /var/log/heimdall/coordinator.log
python3 _daq_core/federation_iq_router.py --config /etc/heimdall/federation.ini \
    --upstream-stats hosta:7001,hostb:7001 --log-file /var/log/heimdall/router.log
```

Router flags win over the file. Useful ones:

| Flag (INI key) | Default | Recipe value |
|---|---|---|
| `--tag-mode` (`router_tag_mode`) | `instance` | `node` on every router whose `:7000` feeds another router |
| `--connect-on-demand` / `--always-connect` (`router_connect_on_demand`) | on-demand | on-demand: an idle router does not hold `iq_server`'s client slot (one slot at the default `iq_server_max_clients = 1`) |
| `--queue-frames` (`router_queue_frames`) | 4 | 4 |
| `--drop-policy` (`router_drop_policy`) | `oldest` | `oldest` |
| `--max-clients` (`router_max_clients`) | 8 | 8 |
| `--align-window-ms` (`router_align_window_ms`) | 0 (arrival order) | 0, or a window once the clocks are synchronised |
| `--max-skew-ms` (`router_max_skew_ms`) | 500 | 500 |
| `--upstream-stats` | none | the site routers' `:7001`, so the central router names chained sources by node key |

The coordinator picks its own candidate entry from `coordinator_candidates` (the one whose
port equals its listen port and whose host is local); `--self host:port` overrides that.
Router exit codes: 0 normal stop, 2 output or stats port bind failure, 3 bad arguments or
config.

### 6. Start the instances and the central DSP

Start each instance on each site host as in [Recipe C](#4-start-and-stop) (or with the
supervised units). Point the central DSP at `hostc:7000`. The router output is a raw
`[1024-byte header][payload]` stream with no handshake; with `--tag-mode node` header slots
106/107 identify the source instance and node. See [consumer_guide.md](consumer_guide.md).

Egress budget: each source instance at `R = 1` sends 96 MB/s into its site router, and the
central router receives the sum. Over a WAN link, use decimation (Recipe B) to cut it by `R`.

### Health checklist (Recipe D)

1. Each host passes [Recipe C's checklist](#health-checklist-recipe-c) (or Recipe A's for a
   single-instance host).
2. Clocks: `chronyc tracking` on every host shows a small offset.
3. Coordinator and fleet. Expect `overall: ok`, every node `ok` with sync 5 or higher. Run
   this on the operator workstation. Copy `/root/cluster.token` there once over SSH, to
   `~/.heimdall/cluster.token` with mode 600 (`chmod 600`).
   ```bash
   export HEIMDALL_COORDINATOR=hosta:6000,hostb:6000,hostc:6000
   export HEIMDALL_AUTH_TOKEN="$(cat ~/.heimdall/cluster.token)"
   heimdall-ctl fleet version
   heimdall-ctl fleet leader
   heimdall-ctl fleet status --fresh
   heimdall-ctl fleet health
   ```
   Output format (from a local test coordinator whose two members were not running; host
   names changed):
   ```text
   $ heimdall-ctl fleet version
   protocol 2  fw 1.1.0-dev  node hostc:coordinator  cluster default  role static  term 0  members 2
   deadlines: query 6000 ms, mutation 40000 ms, schedule 73000 ms; abandoned workers 0
   $ heimdall-ctl fleet leader
   role static  term 0  leader unknown  quorum yes  schedule version 0
   $ heimdall-ctl fleet status
   leader 127.0.0.1:16000 (term 0)   overall: error   members: 2   unreachable: 0
   NODE         HEALTH  SYNC  FREQ  AGE  OVERRUNS  DROPS
   127.0.0.1:0  down    -     -     -    -         -
   127.0.0.1:1  down    -     -     -    -         -
   ```
   With candidates configured, `fleet leader` shows the elected leader and its term, and
   `fleet status --watch` prints `leader changed: ...` on failover.
   <!-- verify-after: WP-08 -->
4. Peer view from one instance (needs `status_extended = 1`):
   ```bash
   heimdall-ctl --host hosta --instance 1 peers
   ```
   <!-- verify-after: WP-06 -->
5. Routers. Expect every source `streaming` while a client is attached, `LOST 0`, `GAPS 0`,
   `unattributed 0`, and no `skew_warning`:
   ```bash
   heimdall-ctl --router hostc:7001 router stats
   heimdall-ctl --router hosta:7001 router sources
   heimdall-ctl fleet router-stats
   ```
   Output format (a test router with no client attached, so the sources are `idle`):
   ```text
   uptime 1.5s  output :17000  tag_mode instance  clients 0  late_frames 0  unattributed 0
   SOURCE       STATE  CONNECTED  FRAMES  LOST  GAPS  RESTARTS  LAST-AGE  OFFSET-MS
   127.0.0.1:0  idle   no         0       0     0     0         -         None
   127.0.0.1:1  idle   no         0       0     0     0         -         None

   CLIENT  CONNECTED  QUEUED  SENT  DROPPED
   ```
   A non-zero `unattributed` on the central router means an upstream router does not tag
   sources: run it with `--tag-mode node`.
6. A fleet command round trip. Expect `applied` per node and exit 0:
   ```bash
   heimdall-ctl fleet tune 433920000
   ```

---

## Recipe E: hardware-free rehearsal

Rehearse a deployment, a config change or a DSP integration without receivers.

### 1. Offline checks (Linux or macOS, no root)

```bash
cd ~/heimdall/Firmware                     # the source tree (/opt/heimdall/Firmware once installed)
./unit_test.sh --ci                        # hardware-free suite
python3 ini_checker.py no_hw               # the shipped INI
python3 ini_checker.py /path/to/candidate.ini no_hw
python3 ini_checker.py /path/to/federation.ini --federation
```

`no_hw` reads only the INI (no sysfs, no `/proc`, no registry), so it gives the same exit
code on every machine. One advisory `WARNING:` depends on the host: with
`register_with_coordinator = 1` and an empty `advertise_host`, a host named like a common
image default (for example `raspberrypi`) gets a node-key collision warning. `ci/build.sh generic|kfr|asan|deploy-lint` reproduces the CI lanes.

### 2. Synthetic chain (Linux, root)

`daq_synthetic_start.sh` replaces `rtl_daq.out` with `_testing/test_data_synthesizer.py`.
The synthesizer answers the ZMQ control port (`1130 + offset`, bind address
`[daq] zmq_listen_address` or `*`), so `hw_controller.py` gets replies, `tune`/`gain`
commands work, and the simulated chain can calibrate. It does not check receiver serials.

Use eth output, or `delay_sync.py` waits for a shmem consumer
([Recipe A](#choose-the-output-interface)):

```ini
[data_interface]
out_data_iface_type = eth

[monitoring]
en_monitoring = 1
en_status_server = 1
heartbeat_interval = 10
```

```bash
cd /opt/heimdall/Firmware
sudo ./daq_synthetic_start.sh
heimdall-ctl status
heimdall-ctl tune 433920000           # expect "applied (fsm=IQ_CAL, ... ms)"
sudo ./daq_stop.sh 0
```

By default the synthesizer is unpaced, unseeded, and stops after 10000 DAQ blocks, which
ends the chain. The synthesizer flags change that:

| Synthesizer flag | Default | Effect |
|---|---|---|
| `--pace` | off | emit blocks at `daq_buffer_size / sample_rate` |
| `--blocks N` | 10000 | number of blocks, `0` = unbounded |
| `--seed S` | unseeded | reproducible data (0..4294967295) |
| `--zero-delays` | off | no per-channel sample delay |
| `--unit-id-from-config` | off | stamp `[hw] unit_id` instead of 0 |

`daq_synthetic_start.sh` passes `--pace`, `--blocks N` and `--seed S` to the synthesizer
when you give them to the script; `--rt` uses the hardware core and priority tiers and
implies `--pace`:

<!-- verify-after: WP-12 -->
```bash
sudo ./daq_synthetic_start.sh --pace --blocks 0 --seed 1
```

The synthetic log is `_logs/synthetic.log`. It logs `Control responder bound to ...` at INFO
level when the ZMQ port is bound, but only with `[daq] log_level` 2 or lower: the logger
level is `log_level x 10`, and the shipped value 5 shows only CRITICAL. With the shipped
level, check the listener instead:

```bash
ss -ltnp | grep ':1130 '
```

### 3. Record and replay for field debugging

Record a problem on site, replay it at your desk. The `.hcap` container is a sequence of
`[1024-byte header][payload]` frames with a JSON sidecar `<name>.hcap.json` (`format`,
`kind` = `post_sync_cf32` or `raw_u8`, host, `instance_id`, firmware version, the full INI
text, frame count, notes).

<!-- verify-after: WP-20 -->
```bash
# On site: record 200 post-sync frames from an eth instance (port 5000 + N*stride).
# At the default iq_server_max_clients = 1, iq_server serves one client: stop the DSP
# first, record through a router, or set [data_interface] iq_server_max_clients = 2 (1..64).
python3 _testing/iq_recorder.py --instance 0 --frames 200 --min-block-index 0 --hcap field.hcap

# Measure a live stream: frames, MB/s, latency_ms, cpi_index gaps, slot-102 overrun delta.
python3 _testing/iq_latency_probe.py --instance 0 --duration 30 --json

# At the desk: serve the capture to a DSP with iq_server's streaming/IQDownload protocol.
python3 _testing/iq_replay.py field.hcap --serve-port 5000 --realtime --loop

# Replay raw receiver bytes through the whole chain (raw_u8 captures only).
sudo ./daq_replay_start.sh raw_capture.hcap
```

`iq_analyzer.py` reads `.hcap` files and takes the sample rate from the header when `--fs`
is absent. `daq_replay_start.sh` refuses a capture that is not `raw_u8` with exit 2.

### Health checklist (Recipe E)

1. `./unit_test.sh --ci` exits 0.
2. `ini_checker.py ... no_hw` exits 0 for every INI you plan to deploy, and `--federation`
   exits 0 for every `federation.ini`.
3. Synthetic chain: `heimdall-ctl status` reaches `Pipeline: ok` and `Calibration: locked`;
   `heimdall-ctl tune` prints `applied`.
4. Replay: the DSP shows the recorded scene; `iq_latency_probe.py` against the replay port
   reports no `cpi_index` gaps.

---

## Recipe F: systemd supervised deployment

Two supervision choices exist per instance. They conflict, so enable only one per instance.

| Unit | Type | What it does |
|---|---|---|
| `heimdall-daq.service` (instance 0), `heimdall-daq@.service` (N > 0) | oneshot | runs `daq_start_sm.sh` once; `ExecStop` runs `daq_stop.sh <N>`; no restart on stage death |
| `heimdall-daq-supervised.service` (instance 0), `heimdall-daq-supervised@.service` (N > 0) | simple | runs `daq_supervise.sh`, which starts the chain, watches every stage and the status server, and exits with a stage-specific code so systemd restarts it |
| `heimdall-host-tuning.service` | oneshot | applies host-global tuning once (`util/host_tuning.sh --apply`) before the DAQ units |
| `heimdall-daq-watchdog.service` + `.timer` | oneshot every 30 s | restarts a supervised unit after 3 failed STATUS checks (optional) |
| `heimdall-coordinator.service`, `heimdall-iq-router.service` | simple | federation services, run as user `heimdall` |

<!-- verify-after: WP-16 -->

### Install

```bash
cd /opt/heimdall
sudo cp packaging/systemd/*.service packaging/systemd/*.timer /etc/systemd/system/
sudo cp packaging/default/heimdall-daq /etc/default/heimdall-daq            # PATH (kraken env) + HEIMDALL_RUN_DIR
sudo cp packaging/logrotate/heimdall /etc/logrotate.d/heimdall
sudo systemctl daemon-reload
```
<!-- verify-after: WP-16 -->

The coordinator and router units run as user `heimdall`. On those hosts create it with
`util/install.sh --federation-user`, or by hand:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin heimdall
```

`/etc/default/heimdall-daq` sets the `PATH` the chain uses, for example
`PATH=/opt/miniforge3/envs/kraken/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.
Provisioning with `--systemd` writes `/etc/default/heimdall-daq@<N>` and sets
`[host] host_tuning = 0`, because the host-tuning unit does that job.

### Enable

```bash
# single-instance host
sudo systemctl enable --now heimdall-host-tuning heimdall-daq-supervised

# multi-instance host (every instance provisioned with --systemd; instance 0 with --in-place)
sudo systemctl enable --now heimdall-host-tuning heimdall-daq-supervised heimdall-daq-supervised@1

# coordinator / router hosts
sudo systemctl enable --now heimdall-coordinator heimdall-iq-router

# optional watchdog
sudo systemctl enable --now heimdall-daq-watchdog.timer
```
<!-- verify-after: WP-16 -->

### How the supervisor behaves

`daq_supervise.sh` always exports `HEIMDALL_SCOPED_STOP=1`, so a busy-port round of the start
script stops only its own instance. It supervises the PID files in `_logs/inst<N>/pids/`.
The units pass `--healthcheck auto`: the STATUS check runs only when the status server is
enabled. A STATUS failure counts after the `--grace` period (60 s) and must persist for 3
checks; the stall threshold `--stall-sec auto` is `3 x k x T_fp + 30` s (161 s at defaults).

<!-- verify-after: WP-12 -->
| Supervisor exit | Meaning | Restarted |
|---|---|---|
| 0 | stopped by SIGTERM/SIGINT | no |
| 1, 3, 4 | the start script's code (config/ports, guard refusal, strict pre-flight) | no (`RestartPreventExitStatus=1 2 3 4`) |
| 2 | usage or misconfiguration (for example `instance_guard = legacy` on a multi-instance host) | no |
| 10 / 11 / 12 / 13 / 14 / 15 | stage died: rtl_daq or synthesizer / rebuffer / decimate / delay_sync / hw_controller / iq_server | yes |
| 16 | health check failed | yes |

`ExecStopPost` runs `daq_stop.sh <N> --clean`, which keeps `fence_term`, so coordinator
fencing survives restarts.

### Health checklist (Recipe F)

1. Units active:
   ```bash
   systemctl status heimdall-daq-supervised heimdall-daq-supervised@1
   journalctl -u heimdall-daq-supervised@1 -n 50
   ```
2. Registry shows `launcher systemd-supervised`:
   ```bash
   heimdall-ctl instances --ping
   ```
3. The recipe's own checklist (A, B, C or D) passes.
4. A deliberate stage kill is recovered: `sudo pkill -f '_daq_core/delay_sync.py'` on a test
   host makes the unit exit 13 and restart within `RestartSec` (5 s). Do this only on a host
   where a host-wide name match is acceptable.

---

## Host sizing matrix

Per-instance figures at the defaults (5 channels, 2.4 MS/s, `daq_buffer_size` 2^18,
`cpi_size` 2^20, `decimation_ratio` 1). Formulas and the full worksheet are in
[capacity_planning.md](capacity_planning.md).

| Recipe | Host | Cores | USB | Network egress | Notes |
|---|---|---|---|---|---|
| A, shmem | Pi 4 / Pi 5 (4 cores) | 4 (all) | 24 MB/s on one HS bus | none | one hardware instance per 4-core host |
| A, eth | Pi 4 / Pi 5 | 4 (all) | 24 MB/s | 96 MB/s per client (77 % of 1 GbE) | decimate for slower links |
| B | Pi 4 or x86 | 4 | 24 MB/s | `96 / R` MB/s per client | `T_fp = cpi_size x R / fs` |
| C | 8-core x86 | 8 (4 per instance) | 24 MB/s per unit, one xHCI controller per unit | 96 MB/s per client per instance | two units on one HS bus exceed the 35 MB/s budget |
| D, site host | as A or C | as A or C | as A or C | sum of local instances into the site router, and on to the central router | routers and coordinator need no RT cores |
| D, central host | any Linux | 2+ | none | router output to the DSP | router memory is bounded by queue depth x clients x frame size |
| E, `--ci` | Linux or macOS | any | none | none | no root |
| E, synthetic | Linux | any (4 with `--rt`) | none | as eth | root |

Per-instance memory at defaults: `rtl_daq` locked ring 20 MiB (`num_ch x usb_ring_buffers x
2 x daq_buffer_size`), plus the shared-memory segments between the stages. Check a planned
host with `daq_preflight.py` (it can plan for another machine with `--dump-host-json` on the
target and `--host-json FILE` here).
<!-- verify-after: WP-13 -->

---

## Health checklist reference

| Check | Command | Healthy | Needs |
|---|---|---|---|
| Config valid | `python3 ini_checker.py no_hw` | exit 0 | none |
| Receivers attached | `python3 ini_checker.py no_hw attached` | exit 0 | Linux sysfs |
| Receivers seen | `python3 _daq_core/hw_discover.py --sdr` | `count` = 5 per unit | Linux sysfs |
| Listeners | `ss -ltnp` | `5001+off`, `5002+off`; `5000+off` with eth | none |
| Pipeline | `heimdall-ctl status` | `Pipeline: ok`, `Calibration: locked`, no recent drops | `en_monitoring = 1`, `en_status_server = 1` |
| Control plane | `heimdall-ctl hwc-status` | `fsm IQ_CAL` | hw_controller of this release |
| Command round trip | `heimdall-ctl tune F` | `applied (...)`, exit 0 | free control slot (`ctl_max_connections`) |
| Ports | `heimdall-ctl ports` | offsets as planned | none |
| Instances | `heimdall-ctl instances --ping` | one live row per instance | `instance_registry = 1` |
| Capacity | `heimdall-ctl capacity` | exit 0 | `daq_preflight.py` |
| Peers | `heimdall-ctl peers` | every peer up | `status_extended = 1`, federation |
| Fleet | `heimdall-ctl fleet status --fresh` | `overall: ok` | coordinator |
| Router | `heimdall-ctl router stats` | `LOST 0`, `GAPS 0`, `unattributed 0` | router stats port |
| USB overruns | IQ header slot 102; `counters.buffer_overrun_cnt` in `heimdall-ctl --json status`; `fleet status` `OVERRUNS` column | constant | the header slot needs nothing; the STATUS counter and the fleet column need `status_extended = 1` (otherwise the column shows `-`) |
| Logs | `grep -iE 'error|fatal' _logs/*.log` (`_logs/inst<N>/` for N > 0) | nothing new | none |

`heimdall-ctl` exits 3 when it cannot reach the addressed server. That is the first thing
to check when a row fails.

---

## Upgrade and rollback

Follow [upgrade_and_rollback.md](upgrade_and_rollback.md), section 6 (6.1 for instance 0,
6.2 for instance directories). The outline below adds the steps that the
[Install root layout](#install-root-layout) of these recipes needs. That layout creates
`/opt/heimdall` as a real directory, and instance 0's state lives in `/opt/heimdall/Firmware`.

1. List and stop: `heimdall-ctl instances`, then stop each unit (or `daq_stop.sh --all`).
2. Once only, rename the real directory: `sudo mv /opt/heimdall /opt/heimdall-<old>`. If you
   skip this, `ln -sfn` creates `/opt/heimdall/heimdall-<new>` inside the old tree and the old
   tree stays active.
3. Install the new tree at `/opt/heimdall-<new>`, **including `make` in
   `Firmware/_daq_core`**.
4. Copy instance 0's state (its provisioned INI, calibration files, database, track-lock and
   fence files):
   ```bash
   OLD=/opt/heimdall-<old>; NEW=/opt/heimdall-<new>
   cd $OLD/Firmware
   sudo cp -a daq_chain_config.ini _calibration _db $NEW/Firmware/
   for f in _data_control/*iq_track_lock _data_control/*fence_term; do
       [ -e "$f" ] && sudo cp -a "$f" $NEW/Firmware/_data_control/
   done
   ```
5. Copy the instance directories (private INI, `_data_control/`, `_logs/`, `_db/`):
   `sudo cp -a $OLD/instances $NEW/`.
6. Switch: `sudo ln -sfn $NEW /opt/heimdall`.
7. For each N > 0: `cd /opt/heimdall && sudo python3 util/provision_instance.py --instance N
   --refresh --write` (re-links, re-validates, lists template keys missing from the instance
   INI; exit 4 while the instance is live).
8. Start, then `heimdall-ctl fleet status --fresh` (or the recipe's checklist).

Rollback: point the symlink back to `/opt/heimdall-<old>` and run `--refresh` again. The old
tree still holds its own copy of every state file. A database schema generation
change discards generation-1 data on the first read-write open.
<!-- verify-after: WP-14 -->
<!-- verify-after: WP-12 -->
