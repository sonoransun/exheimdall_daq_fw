---
layout: default
title: Architecture
description: The HeIMDALL pipeline, its shared-memory data plane, the control plane and every port, in one page.
---
<div class="wrap">
<div class="prose" markdown="1">

<p class="kicker">Architecture</p>

# A chain of small programs

HeIMDALL is not one program but five or six, depending on the output, each pinned to a core and given a real-time priority. They hand frames to each other through double-buffered shared memory and talk about them through FIFOs and a few TCP and ZMQ sockets. Every stage can be read, tested and replaced on its own.

</div>
<figure class="figure-card figure-scroll" tabindex="0" aria-label="Architecture diagram">
  {% include fig-architecture.svg %}
  {% include fig-architecture-tall.svg %}
  <figcaption>Each ring has two segments, <code>_A</code> and <code>_B</code>: the producer writes ready (1 or 2) on the forward FIFO <code>fw_&lt;name&gt;</code>, and the consumer frees the segment on <code>bw_&lt;name&gt;</code>. Ports are base ports; instance N adds N&nbsp;×&nbsp;<code>port_stride</code> (default 100). With the default <code>out_data_iface_type = shmem</code>, consumers map <code>delay_sync_iq</code> directly and <code>iq_server</code> does not run.</figcaption>
</figure>
<div class="prose" markdown="1">

## The stages

| Stage | What it does |
|---|---|
| `rtl_daq.out` (C) | Opens every tuner through the krakenrf librtlsdr fork, runs one USB reader thread per channel, and writes `[1024-byte header][ch0 … chN, 8-bit IQ]` blocks to stdout. Executes tuner commands from ZMQ `:1130` and drives the noise source and bias tees. |
| `rebuffer.out` (C) | Reshapes DAQ blocks into CPI-sized frames in the `decimator_in` ring. |
| `decimate.out` (C) | Converts 8-bit IQ to complex float. With `decimation_ratio` above 1 (it is 1 by default) it also runs FIR decimation through the offload-engine abstraction (KFR, Ne10 or a plain-C engine); calibration frames always bypass the filter. |
| `delay_sync.py` (Python) | The sync state machine: delay estimation by cross-correlation, fractional-delay correction by sample-rate tuning, amplitude and phase calibration by eigendecomposition. Stamps the header telemetry and hosts the optional status server and event bus. |
| `hw_controller.py` (Python) | Runs calibration bursts and gain lock, serves the `:5001` control interface, and owns the scheduler, the RF front-end budget and the antenna-orientation controller. |
| `iq_server.out` (C) | Streams `[header][payload]` frames over TCP `:5000` with a per-frame acknowledgement. Runs only with `out_data_iface_type = eth`. |

Rates, frame sizes and memory at the default configuration are in one table on the [scaling page]({{ '/scaling.html#defaults' | relative_url }}).

## The data plane

Each hop between stages is a pair of shared-memory segments, `_A` and `_B`. The producer fills one while the consumer reads the other. Two FIFOs per hop carry single bytes: the forward FIFO says a segment is ready, the backward FIFO says it is free again. A producer that finds no free segment either waits (back-pressure, used where losing data is never acceptable) or drops the frame (drop mode, used after the decimator, where a slow consumer must not stall acquisition).

| Byte | Meaning | Direction |
|---:|---|---|
| `1` / `2` | segment `_A` / `_B` ready, then free | forward, then backward |
| `10` | `INIT_READY`: producer initialised, sent once | forward |
| `255` | `TERMINATE`: shut the chain down | forward |

## The control plane

| Base port | Owner | Protocol | Open by default |
|---:|---|---|---|
| 1130 | `rtl_daq` (or the synthesizer) | ZMQ REQ/REP, 128-byte tuner messages: `r c g a s n b h` | yes |
| 5000 | `iq_server` | TCP, IQ frames with a per-frame `IQDownload` ack | only with `eth` output |
| 5001 | `hw_controller` | TCP, 128-byte frames: 4-byte verb and 124-byte payload; every reply starts `FNSD` | yes |
| 5002 | `delay_sync` status server | TCP, one command line in, one JSON object and `\n` out | with `[monitoring]` |
| 5003 | `delay_sync` event bus | ZMQ PUB, multipart `[event_type, json]` | with `[monitoring]` |
| 6000 | federation coordinator | TCP, one request per connection, JSON reply | started separately |
| 7000 / 7001 | IQ router output / stats | TCP raw frame stream / JSON line protocol | started separately |

The `:5001` verbs cover tuning (`FREQ`, `GAIN`, `AGC`), initialisation, the frequency scheduler, external LNA gain, bias tees and antenna orientation. Three query verbs (`RFQ `, `OQRY`, `SCHQ`) reply with JSON and are answered in any state; commands that touch hardware wait until the controller is in its safe, calibrated state.

<div class="callout warm" markdown="1">
**Bind addresses.** The TCP servers bind `[daq] listen_address` (default `0.0.0.0`). The ZMQ tuner port binds `zmq_listen_address` (default `*`), is unauthenticated and accepts the halt command, so set it to `127.0.0.1` on any host an untrusted network can reach.
</div>

## The frame header

Every frame starts with a 1024-byte header, defined in lockstep in `iq_header.h` and `iq_header.py` and checked field by field by a test. Its sync word is `0x2bf7b95a` and its version is 8. Frame types are DATA, DUMMY, RAMP, CAL and TRIGW. Version 8 names slots inside the reserved region for RF front-end and orientation telemetry. The scaling program defines slots 103 to 113 for router sequence and receive time, source and node identity, decimator drops, and calibration quality and counters, each to be stamped only under its own opt-in key. With none of them enabled, the header is byte-identical to the one produced before the program began.

<p class="status-line small muted">Slots defined: {% include badge.html wp="WP-01" label=true %} · stamped by {% include badge.html wp="WP-04" label=true %} {% include badge.html wp="WP-06" label=true %} {% include badge.html wp="WP-09" label=true %}</p>

## Start-up and priorities

`daq_start_sm.sh` validates the configuration (an invalid one blocks start), designs the FIR filter, discovers the hardware, and launches the stages with tiered `SCHED_FIFO` priorities:

| Priority | Process | Core | Role |
|---:|---|---:|---|
| 99 | `rtl_daq` | 0 | USB I/O, the critical path |
| 94 | `rebuffer` | 1 | shared-memory hand-off |
| 92 | `decimate` | 1 | shares a core with `rebuffer` for cache locality |
| 90 | `delay_sync` | 2 | FFT and cross-correlation |
| 88 | `iq_server` | 2 | TCP output |
| 82 | `hw_controller` | 3 | control plane |

## Synchronisation

`delay_sync` reports a sync state: 0 is unsynchronised, 1 to 4 are the stages of acquiring sync, 5 and above are locked. Lock requires the residual fractional delay to stay within 0.03 samples (12.5&nbsp;ns at 2.4&nbsp;MS/s). `hw_controller` watches the state, re-runs the noise-source burst every 687 frames (about 300&nbsp;s), and triggers recalibration when alignment drifts.

## Instances

One instance is one coherent island. Instance 0 keeps unprefixed names and base ports, so an existing station is unchanged. Instance N prefixes its FIFOs and shared memory with `inst<N>_`, offsets every port by N × `port_stride`, and logs under `_logs/inst<N>/`. Two hard ceilings bound an instance: 31 channels, set by the 126-byte ZMQ payload at 4 bytes per channel, and frames of at most 2<sup>23</sup> samples, which is 3.495&nbsp;s at 2.4&nbsp;MS/s. [Scaling and clustering]({{ '/scaling.html' | relative_url }}) takes it from here.

## Read further

- [Architecture, in full]({{ '/guide/architecture.html' | relative_url }}): relay files, epochs, segment sizes, transport and offload abstractions.
- [Wire protocols]({{ '/guide/protocols.html' | relative_url }}): every header field, verb and reply shape.
- [Configuration reference]({{ '/guide/configuration.html' | relative_url }}): every key of `daq_chain_config.ini`.
- [Theory of coherent acquisition]({{ '/guide/theory_coherent_acquisition.html' | relative_url }}): why the chain is shaped this way.

</div>
</div>
