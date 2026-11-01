# Tutorial: Directional DF Station (`directional_df` preset)

This walkthrough brings up `config_files/directional_df/daq_chain_config.ini`.
It is the only shipped preset that enables the RF front-end and orientation
subsystems together: amplified receivers behind a high-gain Yagi on a GS-232
azimuth/elevation rotator, tuned to 433 MHz.

The three subsystems are opt-in. Each one is off in every other preset and
in the shipped `Firmware/daq_chain_config.ini`:

| Section | Enable key | What it adds |
|---|---|---|
| `[amplification]` | `en_amplification = 1` | Advisory link budget, `EGAN`/`BIAS`/`RFQ ` control verbs |
| `[antenna]` | `en_antenna_profile = 1` | Antenna gain and feed loss in the budget, boresight correction |
| `[orientation]` | `en_orientation = 1` | Rotator control: `ORNT`/`PARK`/`SCAN`/`OSTP`/`OQRY` |

Related documents:

- [hardware_setup.md](hardware_setup.md): receiver serials, USB, LNAs and
  bias tees ([section 7](hardware_setup.md#7-lnas-and-bias-tees)), rotator
  backends and wiring
  ([section 8](hardware_setup.md#8-rotators-and-pan-tilt-heads-orientation-opt-in)).
- [deployment_recipes.md](deployment_recipes.md): the general single-station
  recipe ([Recipe A](deployment_recipes.md#recipe-a-single-doa-station-on-a-raspberry-pi-4))
  and a hardware-free rehearsal
  ([Recipe E](deployment_recipes.md#recipe-e-hardware-free-rehearsal)).
- [consumer_guide.md](consumer_guide.md): how a DoA application reads the
  IQ stream.
- [configuration.md](configuration.md) for every key,
  [protocols.md](protocols.md) for the wire formats,
  [operations_runbook.md](operations_runbook.md) for troubleshooting.

## Hardware assumed

- A 5-channel KrakenSDR (`num_ch = 5`) on a Raspberry Pi 4 or an x86_64
  Linux host. Build and install the firmware per
  [deployment_recipes.md](deployment_recipes.md#build-and-install).
- A Python environment that root can use. `sudo` replaces `PATH`, so a
  conda or miniforge env (for example `kraken`) is used only when you start
  with `sudo env "PATH=$PATH" ./daq_start_sm.sh`. Install `pyserial` into
  that same environment. Without it the `gs232` backend falls back to the
  mock and the rotator never moves. See
  [deployment_recipes.md](deployment_recipes.md#python-environment-and-sudo).
- Receiver serials 1000 to 1004, with the control channel at serial 1000
  (`[daq] ctr_channel_serial_no = 1000`). See
  [hardware_setup.md](hardware_setup.md#1-receivers-and-serial-numbers).
- One inline LNA per channel (about 20 dB gain, about 1 dB NF, output P1dB
  about +10 dBm), **powered through the receiver bias tees**. There is no
  separate LNA supply. Check the LNA current against the bias-tee rating
  of your receiver first.

> **Bias-tee safety.** The preset sets `[hw] en_bias_tee = 1,1,1,1,1`.
> rtl_daq puts DC on all five antenna inputs as soon as it opens the
> receivers. Connect the LNAs before you start the chain. While
> `en_bias_tee` is 1, never connect a DC-shorted antenna (folded dipole,
> gamma-matched Yagi, loop), a passive splitter or test equipment straight
> to a receiver input. For a bench rehearsal without LNAs, set
> `[hw] en_bias_tee = 0,0,0,0,0` and
> `[amplification] bias_tee_state = 0,0,0,0,0`. See
> [hardware_setup.md](hardware_setup.md#7-lnas-and-bias-tees).
- A directional antenna. The preset models a 433 MHz Yagi: 12 dBi, 40° by
  45° beamwidth, horizontal polarization, fed through 2.5 dB of cable and
  0.5 dB of connectors.
- A Yaesu-style GS-232 rotator controller on `/dev/ttyUSB0` at 9600 baud.
  The `gs232` backend needs the `pyserial` Python package.

No rotator yet? Set `[orientation] backend = mock`. The mock simulates the
slew, so every command below works and nothing moves. Without LNAs fitted,
also turn the bias tees off as described in the safety note above. The unit tests use
the same backend. If a real backend fails to initialise (for example
`pyserial` is missing), hw_controller also falls back to `mock` and logs
`Rotator backend 'gs232' unavailable; falling back to mock`.

## Step 1: copy the preset and choose the output interface

Copy the preset into the working config:

```bash
cp config_files/directional_df/daq_chain_config.ini Firmware/daq_chain_config.ini
```

The preset ships with shared-memory output:

```ini
[data_interface]
out_data_iface_type = shmem
```

With `shmem`, nothing in the DAQ chain reads the output. `delay_sync.py`
opens `_data_control/fw_delay_sync_iq` with a blocking open and waits there
until a downstream shared-memory consumer (for example the DoA DSP
software) attaches. Until then the sync FSM does not run, hw_controller
receives no frames, and the status server on port 5002 does not start. The
chain looks hung and the logs stay quiet.

Pick one of the two interfaces:

| `out_data_iface_type` | What `daq_start_sm.sh` starts | When the sync FSM runs | Use it when |
|---|---|---|---|
| `shmem` (preset) | no output process | after your consumer attaches to `fw_delay_sync_iq` | the DoA software runs on the same host |
| `eth` | `iq_server.out` on port 5000 | at once; frames are dropped while no client reads port 5000 | the consumer is remote, or you follow this tutorial without a consumer |

The rest of this tutorial uses `eth`, because the port-5000 example in
[Reading the v8 telemetry](#reading-the-v8-telemetry-from-the-iq-stream)
needs it. Edit `Firmware/daq_chain_config.ini`:

```ini
[data_interface]
out_data_iface_type = eth
```

With `shmem`, start your consumer right after the chain. The attach
sequence is in
[consumer_guide.md](consumer_guide.md#shared-memory-output).

### Optional: enable the status server and the event publisher

The preset has no `[monitoring]` section, so `heimdall-ctl status`,
`heimdall-ctl events` and the event publisher on port 5003 are off. The
`:5001` control commands work without it. To use the monitoring commands
below, append this opt-in block:

```ini
[monitoring]
en_monitoring = 1
en_status_server = 1
status_update_interval = 5
en_zmq_pub = 1
en_hwc_state_relay = 1
```

Do not add inline comments (`key = value ; text`) to the INI. The Python
stages read it with `configparser`, which keeps the comment as part of the
value. `ini_checker.py` then fails, for example with
`[monitoring] status_update_interval must be an integer >= 0. Currently it is: '5 ; refresh'`.
Put comments on their own line. The INI excerpts below follow this rule.

| Key | Default | Why it is set here |
|---|---|---|
| `en_monitoring` | `0` | Enables the event bus (needed for `en_zmq_pub`, `heimdall-ctl events` and the hw_controller event relay). The status server and metrics have their own keys |
| `en_status_server` | `0` | Serves `heimdall-ctl status` and `heimdall-ctl events` on port 5002 |
| `status_update_interval` | `0` | `0` refreshes STATUS with `heartbeat_interval` (100 frames, about 44 s at this preset's 0.437 s frame period) |
| `en_zmq_pub` | `0` | Publishes delay_sync's event bus on port 5003 |
| `en_hwc_state_relay` | `0` | hw_controller has no publisher of its own. With this key it writes `_data_control/hwc_state`, and delay_sync re-emits its events on port 5003 |

<!-- verify-after: WP-06 -->

### Optional: more log output during bring-up

The preset sets `[daq] log_level = 5`. The C stages then log fatal errors
only, and the module loggers of delay_sync and hw_controller log critical
messages only. Set `log_level = 2` (info) while you bring the station up.
See [operations_runbook.md](operations_runbook.md#43-log-levels).

## Step 2: the configuration, section by section

Only the sections that differ from a stock KrakenSDR config are shown.

### `[hw]` and `[daq]`

```ini
[hw]
num_ch = 5
en_bias_tee = 1,1,1,1,1

[daq]
center_freq = 433000000
sample_rate = 2400000
gain = 0
ctr_channel_serial_no = 1000
```

The station tunes to 433 MHz (ISM band) at 2.4 MS/s. rtl_daq applies `[hw] en_bias_tee` when it opens the receivers, so the LNAs
are powered while the chain calibrates. GPIO 0 of the control receiver
switches the calibration noise source. Channel m's bias tee is GPIO m+1, so
LNA power never collides with calibration.

### `[amplification]`: what is connected to the antenna ports

```ini
[amplification]
en_amplification = 1
ext_lna_gains_db = 20,20,20,20,20
ext_lna_nf_db = 1.0,1.0,1.0,1.0,1.0
ext_lna_p1db_dbm = 10,10,10,10,10
tuner_nf_db = 3.5
expected_input_dbm = -70
p1db_margin_db = 3
max_total_gain_db = 90
en_bias_tee_runtime = 1
bias_tee_state = 1,1,1,1,1
lna_freq_table =
```

| Key | Preset | Meaning |
|---|---|---|
| `ext_lna_gains_db` | `20,...` | LNA gain per channel, continuous dB |
| `ext_lna_nf_db` | `1.0,...` | LNA noise figure per channel, dB |
| `ext_lna_p1db_dbm` | `10,...` | LNA output P1dB per channel, dBm |
| `tuner_nf_db` | `3.5` | R820T noise figure |
| `expected_input_dbm` | `-70` | Strongest expected signal at the antenna port |
| `p1db_margin_db` | `3` | Headroom at or below which a channel is flagged |
| `max_total_gain_db` | `90` | Total gain above which hw_controller logs a warning |
| `en_bias_tee_runtime` | `1` | Allows the runtime `BIAS` command |
| `bias_tee_state` | `1,...` | Reported bias-tee state |
| `lna_freq_table` | empty | Optional per-frequency LNA gain/NF corrections |

This section drives an **advisory** link budget per channel:

- total system gain: antenna gain minus feed loss, plus LNA gain, plus the
  tuner gain that is actually applied;
- system noise figure: Friis cascade of feed loss, LNA and tuner;
- P1dB headroom: a compression warning bit when the headroom is at or below
  `p1db_margin_db`.

The budget never changes a gain. External LNA gain lives in its own
continuous-dB array and never enters the tuner gain-lock logic.

`[hw] en_bias_tee` sets the hardware at start. `[amplification]
bias_tee_state` is only the state hw_controller reports (header slot 97 and
events). The preset keeps the two equal. If you change one, change the
other, or set the opt-in `[amplification] apply_bias_tee_at_init = 1`
(needs `en_bias_tee_runtime = 1`). hw_controller then sends
`bias_tee_state` to rtl_daq at start when the two differ.

### `[antenna]`: the directional element

```ini
[antenna]
en_antenna_profile = 1
profile_name = yagi_433
element_gain_dbi = 12
beamwidth_az_deg = 40
beamwidth_el_deg = 45
polarization = horizontal
cable_loss_db = 2.5
connector_loss_db = 0.5
boresight_az_offset_deg = 0
boresight_el_offset_deg = 0
```

The element gain and the feed loss (cable plus connectors, 3.0 dB) enter the
link budget. The model places the whole feed loss ahead of the LNA. The
boresight offsets correct commanded bearings when the element is mounted
off-axis. They are 0 here.

### `[orientation]`: the rotator

```ini
[orientation]
en_orientation = 1
backend = gs232
device = /dev/ttyUSB0
baud = 9600
az_min_deg = 0
az_max_deg = 360
el_min_deg = 0
el_max_deg = 90
az_park_deg = 0
el_park_deg = 0
slew_rate_dps = 6.0
settle_frames = 10
position_tolerance_deg = 1.0
home_on_start = 1
bearing_mode = external
min_sync_state = 5
en_scan = 1
scan_az_start = 0
scan_az_stop = 350
scan_az_step = 10
scan_el_start = 0
scan_el_stop = 0
scan_el_step = 10
scan_dwell_frames = 20
```

| Key | Preset | Meaning |
|---|---|---|
| `backend` | `gs232` | `mock`, `gs232`, `pwm_servo` or `i2c_pantilt` |
| `slew_rate_dps` | `6.0` | `pwm_servo` and `i2c_pantilt` use it to estimate when a move ends. `mock` advances this many degrees per frame. `gs232` reads the position back instead |
| `settle_frames` | `10` | DATA frames discarded after each arrival |
| `home_on_start` | `1` | Drive the rotator home when hw_controller opens it |
| `bearing_mode` | `external` | The DF application (or you) commands bearings |
| `min_sync_state` | `5` | Settle and scan progress freeze below this `sync_state` (track lock) |
| `en_scan` | `1` | Start one scan-and-peak automatically |
| `scan_az_*` | `0`, `350`, `10` | 36 azimuth points, end point included |
| `scan_el_*` | `0`, `0`, `10` | One elevation row: an azimuth-only scan |
| `scan_dwell_frames` | `20` | Frames averaged per grid point |

How motion is gated:

| Event | What happens |
|---|---|
| hw_controller start, `home_on_start = 1` | The home command goes to the rotator at once, before the chain has synced |
| `ORNT`, `PARK`, `SCAN` | They execute only in hw_controller's safe state (`IQ_CAL`, non-dummy frame). That includes calibration noise bursts. The set-point goes to the rotator at once and is clamped to the `az_/el_min/max_deg` limits |
| Every frame | The controller freezes settle counting and scan progress while the noise source is on or `sync_state < min_sync_state` |
| `en_scan = 1` | One autonomous scan starts the first time motion is allowed and the controller is idle or settled. That can be after homing, or after your first bearing, park or scan stop settles |

With `gs232`, command azimuth 0 instead of 360, or set `az_max_deg = 359`.
The backend sends `W000` for 360 but compares the read-back 0 with 360.
A bearing of 360 therefore never reports SETTLED, so `--verify` times out
and no settle or scan event follows.

A scan steps through the grid. At each point it waits for arrival, discards
`settle_frames` DATA frames, and averages the aggregate channel power
(header slot 101) over `scan_dwell_frames` frames. At the end it slews to
the point with the highest average.

At this preset's frame period (1048576 samples at 2.4 MS/s = 0.437 s), one
grid point takes at least 30 frames (about 13 s). The 36-point scan takes
at least 1080 frames (about 7.9 minutes), plus slew time and calibration
pauses.

## Step 3: validate, start, and check that the chain is up

Validate the config and start the chain. Check the bias-tee safety note in
[Hardware assumed](#hardware-assumed) first: the start energises all five
bias tees.

```bash
cd Firmware
python3 ini_checker.py no_hw attached   # the check daq_start_sm.sh runs; exit 0 = valid, 1 = errors
sudo ./daq_start_sm.sh
```

With a conda or miniforge env, start with
`conda activate kraken; sudo env "PATH=$PATH" ./daq_start_sm.sh` instead.

`ini_checker.py` prints blocking problems as `ERROR:root:<text>` and exits 1.
It prints non-blocking problems on stderr as `WARNING: <text>` and still
exits 0. The preset, with or without the edits above, passes with no output.

`daq_start_sm.sh` runs `python3 ini_checker.py no_hw attached` and refuses
to start on exit 1. `attached` adds sysfs and host-registry checks. Without
`[hw] serial_base`/`device_serials` (as in this preset), duplicate serials
and registry conflicts are only warnings. See
[hardware_setup.md](hardware_setup.md#32-what-the-checks-report).

### Logs

Instance 0 logs are flat files in `_logs/`:

```bash
tail -f _logs/hwc.log _logs/delay_sync.log _logs/rtl_daq.log
```

`hwc.log` shows these lines at every `log_level`:

```text
INFO:root:Antenna profile 'yagi_433' loaded (12.0 dBi)
INFO:root:Amplification enabled: external LNA gains [20.0, 20.0, 20.0, 20.0, 20.0] dB
INFO:root:Antenna orientation controller enabled (backend=gs232, mode=external)
```

The backend in the last line is the configured one, not the active one. To
confirm that the real rotator opened, check that `hwc.log` has no
`ERROR:rotator_controller:GS-232 serial init failed` line and no
`Rotator backend 'gs232' unavailable; falling back to mock` line. If either
appears, the mock is in use and nothing moves.

Rotator moves log from the `orientation_controller` logger, for example
`INFO:orientation_controller:Slewing to az=135.00 el=10.00 (mech az=135.00 el=10.00)`,
`Settled at az=...` and `Scan peak at az=... el=... (obj=...)`.
With `log_level = 2`, `rtl_daq.log` also shows `Bias tee on channel: 0 is enabled`
for each channel.

### Is it up?

`heimdall-ctl hwc-status` answers from the control server thread, even when
no frames flow. It needs no `[monitoring]` section:

```console
$ heimdall-ctl hwc-status
fsm     pend  noise  agc  nch  freq         frame-age  frames  term
IQ_CAL  0     0      0    5    433.000 MHz  0.212s     1532    0
```

| What you see | Meaning |
|---|---|
| `frames` rises, `fsm` reaches `IQ_CAL` | Frames reach hw_controller. Control commands can execute |
| `fsm INIT`, frame-age `-`, `frames 0` | No frame has reached hw_controller. With `shmem` output, attach the consumer. Otherwise see [operations_runbook.md](operations_runbook.md#6-troubleshooting-hung-or-idle-chain) |
| frame-age followed by `(stalled)` | Frames stopped after they had flowed |

The chain is locked when delay_sync's `sync_state` reaches 5 (track lock)
or 6 (tracking). With the `[monitoring]` block enabled:

```console
$ heimdall-ctl status
Pipeline: ok  Sync: 6  Freq: 433.000 MHz  Uptime: 120s
Calibration: locked  Aggregate power: -41.2 dB
...
```

`Pipeline` is `ok` when `sync_state >= 5` and no frame was dropped within the
last 60 s. With `eth` output and no client on port 5000, delay_sync drops
every outbound frame, so expect `degraded` until a client reads the stream.
Without `[monitoring]`, read `sync_state` from the IQ headers instead (see
[below](#reading-the-v8-telemetry-from-the-iq-stream)).

Leave the chain running. Step 4 and the telemetry section need it.
[Step 5](#step-5-stop-the-chain) stops it.

## Step 4: control and verify with `heimdall-ctl`

Install the CLI once with `pip install .` from the repository root. Or run
`util/bin/heimdall-ctl`, or `python3 -m heimdall_ctl` from `util/`. It reads
ports from `$HEIMDALL_CONFIG`, `./daq_chain_config.ini` or
`./Firmware/daq_chain_config.ini`. `--instance N` applies the federation
port offset, and `--json` prints the raw reply.

### Queries

```console
$ heimdall-ctl rf-budget
{
  "gains": [
    29.0,
    ...
  ],
  "nf": [
    4.04,
    ...
  ],
  "comp": 0
}
```

`rf-budget` sends the `RFQ ` query. The reply holds the per-channel total
system gain in dB (1 decimal), the system noise figure in dB (2 decimals) and
the compression bitmask. The preset values give:

| Quantity | Formula | Value at `gain = 0` |
|---|---|---|
| Total system gain | 12 - 3.0 + 20 + tuner gain | 29.0 dB (tuner 0 dB); 58.7 dB at a 29.7 dB tuner gain |
| System noise figure | Friis: 3.0 dB feed loss, then 1 dB LNA, then 3.5 dB tuner | 4.04 dB |
| P1dB headroom | 10 - (-70 + 20 - 3.0) | 63 dB, so `comp` is 0 |

```console
$ heimdall-ctl orientation
{
  "az": 0.0,
  "el": 0.0,
  "state": 2
}
```

This output was taken with `en_scan = 0`, after homing settled. With the
preset's `en_scan = 1`, expect `state` 3 (SCANNING) for the first minutes
after lock.

`orientation` sends `OQRY`. `state` is 0 IDLE, 1 SLEWING, 2 SETTLED or
3 SCANNING. `az` and `el` are the commanded bearing after clamping, not a
position read back from the rotator. Use `state` to see when the move has
finished.

Query verbs (`rf-budget`, `orientation`, `schedule query`) are answered in
any FSM state. The main loop answers them once per frame, so they time out
while no frames reach hw_controller. `hwc-status` does not need frames.

### Commands

With `en_scan = 1`, the station starts one azimuth scan shortly after lock.
It runs for about 8 minutes and ends on the peak bearing. Wait for the
`orientation_scan_peak` event or `state` 2 before you command a bearing.
A bearing sent before the scan has started settles, then the scan starts
and overrides it. A bearing, park or scan stop sent during the scan
abandons the scan. The automatic scan emits no `orientation_scan_start`
event; only the `SCAN` command does. For a station driven only by the DF
application, set `en_scan = 0`.

```bash
heimdall-ctl bearing 135 10     # ORNT: slew to az 135°, el 10°
heimdall-ctl orientation        # state 1 (SLEWING), later 2 (SETTLED)
heimdall-ctl scan start         # SCAN: begin scan-and-peak
heimdall-ctl scan stop          # OSTP: stop motion (state becomes SETTLED)
heimdall-ctl park               # PARK: slew to az_park_deg / el_park_deg
```

Each mutating command prints one outcome line. hw_controller confirms the
outcome through the `XACK` handshake that heimdall-ctl sends by default
(`--ack auto`):

| Output | Exit | Meaning |
|---|---|---|
| `applied (fsm=IQ_CAL, 212 ms)` | 0 | Executed |
| `NOT applied: disabled (fsm=IQ_CAL)` | 1 | The subsystem is off, for example `BIAS` without `en_bias_tee_runtime = 1` |
| `NOT applied: not_confirmed (fsm=IQ_CAL)` | 1 | rtl_daq did not answer the bias-tee change |
| `NOT applied: deferred_timeout (fsm=...)` | 1 | The FSM did not reach the safe state within `[daq] ctl_request_timeout_sec` (default 10 s). The command was withdrawn and never runs |
| `sent; server does not confirm application (legacy server)` | 0 | An older hw_controller without XACK |

Mutating commands wait for the safe state. During initial calibration that
can take longer than 10 s. Retry once `sync_state` reaches 5.

`--verify` polls `OQRY` until the rotator has settled on the requested
bearing (within 0.5°). A long slew needs a longer `--timeout`:

```console
$ heimdall-ctl --verify --timeout 60 bearing 135 10
applied (fsm=IQ_CAL, 212 ms)
verified: bearing az=135.00 el=10.00
```

Runtime front-end changes:

```bash
heimdall-ctl lna-gain --unified 20       # EGAN: dB (sent as tenths of a dB)
heimdall-ctl lna-gain 20,20,20,18,20     # per channel
heimdall-ctl bias-tee --all on           # BIAS (needs en_bias_tee_runtime = 1)
heimdall-ctl bias-tee 1,1,1,0,1
heimdall-ctl tune 433.5M                 # FREQ: the budget is recomputed
```

A list with the wrong number of values prints
`warning: expected 5 values, got k (the server uses the first 5)` and is
sent anyway. `--strict` refuses it with exit 1. With an empty
`lna_freq_table`, a frequency change leaves the budget numbers unchanged.

To watch orientation events (needs the `[monitoring]` block with
`en_zmq_pub = 1` and `en_hwc_state_relay = 1`):

```bash
heimdall-ctl events --tail --filter orientation_
```

The event types are `orientation_slew`, `orientation_settled`,
`orientation_scan_start`, `orientation_scan_peak` and `orientation_park`.
<!-- verify-after: WP-06 -->

### Protocol-level example client

`util/orientation_scan_example.py` sends raw 128-byte frames and needs no
pip install:

```bash
python3 util/orientation_scan_example.py --host localhost                # demo: EGAN, BIAS, ORNT 137.5/12
python3 util/orientation_scan_example.py --host localhost --mode autoscan # SCAN
python3 util/orientation_scan_example.py --host localhost --mode scan     # client-side scan
```

| Option | Default |
|---|---|
| `--host` | `localhost` |
| `--ctrl-port` | `5001` |
| `--status-port` | `5002` |
| `--num-ch` | `5` |
| `--mode` | `demo` (`demo`, `scan`, `autoscan`) |

`--mode scan` steps the azimuth in 10° steps, waits 1 s per step and reads
`aggregate_power_db` from STATUS on port 5002. It needs the `[monitoring]`
block, and STATUS must refresh faster than the 1 s step. Set
`status_update_interval = 2` or less (0.87 s at this frame period; `1` gives
0.44 s). With the value 5 used above (2.2 s), consecutive steps read the
same stale power.

The example script does not check the 128-byte replies. It prints
`Set external LNA gains` or `Commanded bearing` even when the command was
not applied or was withdrawn. Its 3 s socket timeout is also shorter than
the server's 10 s wait for the safe state. Confirm the result with
`heimdall-ctl orientation` or `heimdall-ctl hwc-status`.

The firmware scan (`--mode autoscan` or `heimdall-ctl scan
start`) reads the header on every frame and has no such limit.

## Reading the v8 telemetry from the IQ stream

delay_sync stamps the front-end and orientation telemetry into the header's
reserved region of every outbound frame. It does so when `[amplification]`,
`[antenna]` or `[orientation]` is enabled. With
`out_data_iface_type = eth`, read it from port 5000. Run this from the
repository root:

```python
import socket, sys
sys.path.insert(0, "Firmware/_daq_core")
from iq_header import IQHeader

s = socket.create_connection(("127.0.0.1", 5000))
s.sendall(b"streaming")                      # start the stop-and-wait stream

def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("stream closed")
        buf += chunk
    return buf

hdr = IQHeader()
for _ in range(10):
    hdr.decode_header(recv_exact(s, 1024))
    payload_len = hdr.cpi_length * hdr.active_ant_chs * 8   # complex64 samples
    recv_exact(s, payload_len)               # (or process it)
    az, el = hdr.get_antenna_bearing()       # slots 98/99, decoded from centi-degrees
    print("sync", hdr.sync_state,
          "az %.1f el %.1f state %d" % (az, el, hdr.get_rotator_state()),
          "agg %.1f dB" % (hdr.get_aggregate_power_mdb() / 1000.0),
          "gain", [g / 10 for g in hdr.get_total_system_gains(hdr.active_ant_chs)],
          "nf", [n / 1000 for n in hdr.get_system_nf_mdb(hdr.active_ant_chs)],
          "comp", hdr.get_compression_flags(),
          "bias", hdr.get_bias_tee_state(),
          "overruns", hdr.get_buffer_overrun_cnt())
    s.sendall(b"IQDownload")                 # ack: send the next frame
```

iq_server closes the session when the `IQDownload` ack does not arrive
within 5 s. Full client rules are in
[consumer_guide.md](consumer_guide.md#tcp-output-port-5000).

Slot reference. delay_sync stamps every slot except 102, which rtl_daq
stamps:

| Accessor | Slots | Meaning |
|---|---|---|
| `get_ext_lna_gains(M)` | 0-31 | External LNA gain, dB x 10 |
| `get_total_system_gains(M)` | 32-63 | Total system gain, dB x 10 |
| `get_system_nf_mdb(M)` | 64-95 | System NF, milli-dB |
| `get_compression_flags()` | 96 | Per-channel P1dB-headroom warning bits |
| `get_bias_tee_state()` | 97 | Bias-tee bitmask (bit m = channel m) |
| `get_antenna_bearing()` | 98-99 | (az, el) in degrees, decoded from centi-degrees; el carries a +90° wire offset |
| `get_rotator_state()` | 100 | 0 IDLE, 1 SLEWING, 2 SETTLED, 3 SCANNING |
| `get_aggregate_power_mdb()` | 101 | Aggregate channel power, signed milli-dB (the scan objective) |
| `get_buffer_overrun_cnt()` | 102 | Cumulative USB ring overrun events |

During a scan, slot 101 rises and falls as the beam sweeps.

Slots 103-113 carry router, node and calibration telemetry. They are 0
unless their own opt-in keys are set. For a DF station,
`[monitoring] stamp_cal_telemetry = 1` stamps the calibration quality
(slot 110), the IQ and sample compensation counts (111, 112) and the
recalibration generation (113). See
[consumer_guide.md](consumer_guide.md#reserved-slots-102-113-telemetry-for-consumers).
<!-- verify-after: WP-06 -->

## Notes and caveats

- **Compression flags are advisory.** A set bit in slot 96 (or in the
  `comp` field of `rf-budget`) means the static budget predicts the LNA
  is within `p1db_margin_db` of compression at `expected_input_dbm`. The
  firmware never reduces a gain on its own. Fit a lower-gain LNA or an
  attenuator ahead of it. Then put the new value in `ext_lna_gains_db` and
  restart the chain, or send `lna-gain` to update the `rf-budget` reply.
  `lna-gain` controls no hardware: it only tells the budget model which LNA
  gain is fitted. The tuner `gain` does not affect this flag.
- **`lna-gain` changes the `rf-budget` reply, not header slots 0-95.**
  delay_sync computes those slots from its own copy of the INI
  `[amplification]` values and the applied tuner gains. To make the header
  follow a new LNA gain, put it in `ext_lna_gains_db` and restart the chain.
- **Slot 97 shows the requested bias-tee state.** hw_controller rewrites
  `_data_control/bias_tee_state` after every `BIAS`, also when rtl_daq does
  not confirm the change (`NOT applied: not_confirmed`). At start it writes
  `[amplification] bias_tee_state`, which can differ from the hardware set
  by `[hw] en_bias_tee` unless you keep them equal.
- **Calibration bursts pause only some things.** A noise burst runs inside
  hw_controller's safe state, so most commands still execute:

  | Command | During a noise burst |
  |---|---|
  | `INIT`, `RECL` | Always wait for the burst to end |
  | `FREQ`, `EGAN` | Execute, unless the opt-in `[calibration] defer_control_during_burst = 1` is set |
  | `GAIN`, `AGC` | Stored and applied after the burst. heimdall-ctl prints `held until the calibration burst ends` (exit 0). With `defer_control_during_burst = 1`, `GAIN` waits instead |
  | `BIAS` | hw_controller acks it as applied. rtl_daq defers the GPIO change until the burst ends and logs `Bias-tee change deferred: noise-source calibration burst active` at warning level, so you need `log_level` 3 or lower to see it. With `defer_control_during_burst = 1`, `BIAS` waits instead |
  | `ORNT`, `PARK`, `SCAN`, `OSTP` | Execute. The set-point goes to the rotator at once and the rotator moves. Only settle counting and scan progress freeze until the burst ends |

- **Telemetry lag.** The bearing travels hw_controller →
  `_data_control/orientation_state` (one line, `az el state`) → delay_sync.
  hw_controller rewrites the file at once after `ORNT` and on a settle or
  scan-peak event. Otherwise it rewrites it every 5 frames while in
  `IQ_CAL`, which covers `PARK`, `SCAN`, `OSTP` and scan steps. delay_sync
  reads the file every 5 frames. Expect header slots 98-100 to lag by up to
  5 frames after `ORNT`, and by up to about 10 frames (about 4.4 s here)
  otherwise, plus pipeline latency.
- **One control client at a time by default.** With `[daq]
  ctl_max_connections = 1` (default), a DoA application that keeps port 5001
  open blocks heimdall-ctl. heimdall-ctl then prints `server did not answer
  XACK within 3 s (another client may hold the control port); command not
  sent ...` and exits 3. Set the opt-in `ctl_max_connections = 4` to serve
  several clients at once.
- **Orientation hardware is per instance.** A command sent to one
  instance's port 5001 moves only that instance's rotator. hw_controller
  never relays it to peers. The coordinator sends `ORNT` or `PARK` to every
  member only when you ask explicitly, with
  `heimdall-ctl fleet bearing AZ [EL]` or `heimdall-ctl fleet park`. See
  [scaling_and_clustering.md](scaling_and_clustering.md#62-the-fleet-cli).

## Step 5: stop the chain

When you are done, stop the chain:

```bash
sudo ./daq_stop.sh
```

With no argument it stops every DAQ process on the host.
`sudo ./daq_stop.sh 0` stops only instance 0 through its PID files.
