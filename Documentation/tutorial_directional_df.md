# Tutorial: Directional DF Station (`directional_df` preset)

This walkthrough brings up the one shipped preset that enables the RF
front-end and orientation subsystems together:
`config_files/directional_df/daq_chain_config.ini` — amplified receivers
behind a high-gain Yagi on a GS-232 azimuth/elevation rotator, tuned to
433 MHz.

## Hardware assumed

- A 5-channel KrakenSDR (`num_ch = 5`) on a Raspberry Pi 4 or x86_64 Linux
  host, firmware built and installed per the
  [developer guide](developer_guide.md).
- One inline LNA per channel (~20 dB gain, ~1 dB NF, P1dB ≈ +10 dBm),
  **powered through the receiver bias tees** — no separate supply.
- A directional antenna (the preset models a 433 MHz Yagi: 12 dBi, 40°/45°
  beamwidths, horizontal polarization) fed through ~2.5 dB of cable and
  0.5 dB of connectors.
- A Yaesu-style GS-232 rotator controller on `/dev/ttyUSB0` at 9600 baud.

No rotator yet? Set `[orientation] backend = mock` — everything below works
identically, the "motion" is simulated. That is also how the unit tests run.

## The configuration, section by section

Start from the preset:

```bash
cp config_files/directional_df/daq_chain_config.ini Firmware/daq_chain_config.ini
```

Only the sections that differ from a stock KrakenSDR config are shown here —
see [configuration.md](configuration.md) for every key.

### `[hw]` / `[daq]`

```ini
[hw]
num_ch = 5
en_bias_tee = 1,1,1,1,1      ; power the inline LNAs from cold start

[daq]
center_freq = 433000000      ; 433 MHz ISM
sample_rate = 2400000
gain = 0
```

Bias tees are on at boot so the LNAs are already powered while the chain
calibrates. GPIO 0 is reserved for the calibration noise source; channel
bias tees use GPIO m+1, so LNA power can never collide with calibration.

### `[amplification]` — tell the firmware what is bolted to the antenna ports

```ini
[amplification]
en_amplification = 1
ext_lna_gains_db = 20,20,20,20,20    ; continuous dB, per channel
ext_lna_nf_db = 1.0,1.0,1.0,1.0,1.0
ext_lna_p1db_dbm = 10,10,10,10,10
tuner_nf_db = 3.5
expected_input_dbm = -70
p1db_margin_db = 3
max_total_gain_db = 90
en_bias_tee_runtime = 1              ; allow the BIAS control command
bias_tee_state = 1,1,1,1,1
```

This drives the **advisory** link budget: per-channel total system gain
(antenna + LNA + tuner, computed after any unified-gain clamp), cascaded
Friis noise figure, and P1dB headroom warnings. It never changes gains by
itself — external LNA gain is kept in a separate continuous-dB array and is
deliberately never mixed into the tuner gain-lock machinery.

### `[antenna]` — the directional element

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
```

The element gain and feed losses enter the same link budget; the boresight
offsets (0 here) would correct commanded bearings if the element were mounted
off-axis.

### `[orientation]` — the rotator

```ini
[orientation]
en_orientation = 1
backend = gs232
device = /dev/ttyUSB0
baud = 9600
slew_rate_dps = 6.0
settle_frames = 10           ; DATA frames discarded after each move
home_on_start = 1
bearing_mode = external      ; the DF app (or you) commands bearings
min_sync_state = 5           ; never slew before track-lock
en_scan = 1                  ; auto-start one scan-and-peak once locked
scan_az_start = 0
scan_az_stop = 350
scan_az_step = 10            ; 36 azimuth points
scan_el_start = 0
scan_el_stop = 0             ; azimuth-only scan
scan_dwell_frames = 20       ; frames averaged per point
```

Motion is gated: the controller refuses to move while the calibration noise
source is on or while `sync_state < min_sync_state`. With `en_scan = 1` the
controller starts one autonomous scan-and-peak as soon as the chain reaches
track-lock: it steps the grid, discards `settle_frames` at each point,
averages the aggregate channel power (header slot 101) over
`scan_dwell_frames`, then slews to the loudest bearing.

## Bring-up

```bash
cd Firmware
python3 ini_checker.py no_hw       # must exit 0
sudo ./daq_start_sm.sh
```

Watch the logs (instance 0 logs are flat files in `_logs/`):

```bash
tail -f _logs/delay_sync.log _logs/hwc.log
```

Expect in `hwc.log`: `Amplification enabled: external LNA gains [...] dB` and
the orientation controller starting up (and homing, since
`home_on_start = 1`).
delay_sync walks the sync FSM; the chain is up when `sync_state` reaches 5–6.
Stop with `sudo ./daq_stop.sh`.

## Verifying with `heimdall-ctl`

Install once (`pip install .` from the repo root), or run
`python3 -m heimdall_ctl` from `util/`. The CLI reads ports from the config
(`--instance N` applies the federation port offset; `--json` for raw output).

```bash
heimdall-ctl status
# Pipeline: ok  Sync: 6  Freq: 433.000 MHz  Uptime: 120s ...

heimdall-ctl rf-budget          # RFQ  -> {"gains":[...], "nf":[...], "comp":0}
```

`rf-budget` sends the `RFQ ` query frame; the reply is the per-channel total
system gain (1 decimal, dB), system noise figure (2 decimals, dB), and the
compression bitmask. With the preset values expect roughly
`12 − 3 + 20 + tuner_gain` dB per channel and an NF close to the LNA's ~1 dB
plus the feed loss ahead of it.

```bash
heimdall-ctl orientation        # OQRY -> {"az": ..., "el": ..., "state": 0..3}
heimdall-ctl bearing 135 10     # ORNT: slew to az 135°, el 10°
heimdall-ctl orientation        # state: 1 (SLEWING), later 2 (SETTLED)
heimdall-ctl scan start         # SCAN: begin scan-and-peak
heimdall-ctl scan stop          # OSTP: halt motion
heimdall-ctl park               # PARK: return to the park position
```

State enum in `orientation` replies: 0 = IDLE, 1 = SLEWING, 2 = SETTLED,
3 = SCANNING. Commands issued mid-calibration are deferred to the next safe
frame — a slow first response is normal.

Runtime front-end tweaks:

```bash
heimdall-ctl lna-gain --unified 20      # EGAN, takes dB (x10 wire encoding is internal)
heimdall-ctl lna-gain 20,20,20,18,20    # per-channel
heimdall-ctl bias-tee --all on          # BIAS (needs en_bias_tee_runtime=1)
heimdall-ctl bias-tee 1,1,1,0,1
heimdall-ctl tune 433.5M                # FREQ — the budget re-computes at the new frequency
```

A protocol-level example client (raw 128-byte frames, no pip install) is
`util/orientation_scan_example.py`:

```bash
python3 util/orientation_scan_example.py --host localhost
```

## Reading the v8 telemetry from the IQ stream

Every outbound frame carries the front-end/orientation telemetry in the
header's reserved region (see [protocols.md](protocols.md)). With
`[data_interface] out_data_iface_type = eth`, read it straight off port 5000:

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
        if not chunk: raise ConnectionError
        buf += chunk
    return buf

hdr = IQHeader()
for _ in range(10):
    hdr.decode_header(recv_exact(s, 1024))
    payload_len = hdr.cpi_length * hdr.active_ant_chs * 2 * hdr.sample_bit_depth // 8
    recv_exact(s, payload_len)               # (or process it)
    az, el = hdr.get_antenna_bearing()       # slots 98/99, centi-deg decoded
    print("sync", hdr.sync_state,
          "az %.1f el %.1f state %d" % (az, el, hdr.get_rotator_state()),
          "agg %.1f dB" % (hdr.get_aggregate_power_mdb() / 1000.0),
          "gain", [g/10 for g in hdr.get_total_system_gains(hdr.active_ant_chs)],
          "nf", [n/1000 for n in hdr.get_system_nf_mdb(hdr.active_ant_chs)],
          "comp", hdr.get_compression_flags(),
          "bias", hdr.get_bias_tee_state(),
          "overruns", hdr.get_buffer_overrun_cnt())
    s.sendall(b"IQDownload")                 # ack -> next frame
```

Slot cheat sheet (all stamped by delay_sync except slot 102, stamped by
rtl_daq):

| Accessor | Slots | Meaning |
|---|---|---|
| `get_ext_lna_gains(M)` | 0–31 | external LNA gain, dB × 10 |
| `get_total_system_gains(M)` | 32–63 | total system gain, dB × 10 |
| `get_system_nf_mdb(M)` | 64–95 | system NF, milli-dB |
| `get_compression_flags()` | 96 | per-channel P1dB-headroom warnings |
| `get_bias_tee_state()` | 97 | bias-tee bitmask |
| `get_antenna_bearing()` | 98–99 | (az°, el°) — decoded from centi-deg, el has a +90° wire offset |
| `get_rotator_state()` | 100 | 0 IDLE / 1 SLEWING / 2 SETTLED / 3 SCANNING |
| `get_aggregate_power_mdb()` | 101 | aggregate channel power, milli-dB (the scan objective) |
| `get_buffer_overrun_cnt()` | 102 | cumulative USB ring overrun events |

During a `SCAN` you can watch slot 101 rise and fall as the beam sweeps, and
the `orientation_scan_peak` event fire on :5003 when the controller commits to
the loudest bearing.

## Notes and caveats

- **Compression flags are advisory.** A set bit in slot 96 (or a `comp` value
  in `rf-budget`) means the *static* budget predicts the LNA is within
  `p1db_margin_db` of compression at `expected_input_dbm` — the firmware
  never reduces gain on its own. Lower `lna-gain`/`gain` yourself.
- Recalibration bursts pause everything: bias changes defer, the rotator
  freezes, dummy frames flow. This is by design.
- The bearing reaching the header travels
  hw_controller → `_data_control/orientation_state` → delay_sync, polled
  every 5th frame — expect up to a few frames of latency between a commanded
  slew and the telemetry reflecting it.
- Orientation hardware is per-instance: federation does **not** fan out
  `ORNT`/`SCAN` to peers.
- Query verbs (`rf-budget`, `orientation`, `schedule query`) answer in any
  state; mutating commands wait for the safe calibration window.
