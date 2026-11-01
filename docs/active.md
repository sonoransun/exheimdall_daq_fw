---
layout: default
title: Active arrays
description: A design preview of the active coherent-array track for HeIMDALL — USRP and Per Vices hardware, GNU Radio, precision phased geometry, amplified front ends and a transmit safety model.
---
<div class="wrap">
<div class="prose" markdown="1">

<p class="kicker">Design preview</p>

# Active arrays

<p class="status-line"><span class="badge badge-design">spec final · AT-1 in progress</span></p>

The KrakenSDR listens. The active track asks what the same discipline of coherence buys when the array is built from instrument-grade radios that can also transmit: Ettus and NI USRPs through UHD, Per Vices units through their UHD fork, and GNU Radio flowgraphs around them. It adds precision geometry, beamforming, models of amplified front ends, and a transmit path that is refused by default and guarded at every step when enabled.

This page describes a specification, not finished software. The specification is final and its first foundation modules have begun to appear in `Firmware/_active/`; nothing in the track has been reviewed yet. Its packages are numbered AT-1 to AT-8, apart from the scaling program's WP numbers.

## A separate track, by construction

The track lives entirely in new files: `Firmware/_active/` for its modules, `config_files/active_*` for presets, its own tests and its own documents. It edits no existing file. It reuses the frame header, the gain-budget arithmetic and the antenna profile of the passive chain by import only and adds no header slots, so its frames are version-8 frames that existing consumers can already parse. Hardware libraries (`uhd`, `gnuradio`, `zmq`) are imported lazily, so every module loads with nothing but the standard library, NumPy and SciPy, and a missing driver is an explicit error rather than a silent fallback to simulation.

| Package | Scope | Status |
|---|---|---|
| AT-1 | Foundation, configuration schema, device capabilities | <span class="badge badge-progress">in progress</span> |
| AT-2 | Geometry, array pattern, beamforming, DSP | <span class="badge badge-planned">planned</span> |
| AT-3 | Front-end model, regulatory policy, TX safety, RF control lines | <span class="badge badge-planned">planned</span> |
| AT-4 | Backend interface, simulation physics, timing and coherence | <span class="badge badge-planned">planned</span> |
| AT-5 | UHD and Per Vices backends, fake UHD for tests, GNU Radio | <span class="badge badge-planned">planned</span> |
| AT-6 | Calibration and its store, IQ bridge, status and control servers | <span class="badge badge-planned">planned</span> |
| AT-7 | Runner, command-line tool, presets | <span class="badge badge-planned">planned</span> |
| AT-8 | Documentation | <span class="badge badge-planned">planned</span> |

## Hardware and coherence

The backends cover the USRP families (B2xx, X3xx with UBX, SBX and TwinRX, N3xx, X4xx) and the Per Vices Crimson TNG, Cyan, Chestnut and Calamine, plus a deterministic simulator that is the test vehicle for everything else. A Per Vices Cyan carries 8 receive and 8 transmit chains by default and up to 16. Per Vices arrays of more than one unit run one `multi_usrp` session per unit, held together by a shared reference and PPS.

What an array may do depends on how much of its timing it shares. The planner classifies every configuration before it streams:

| Class | Condition | What is allowed |
|---|---|---|
| Shared LO | the local oscillator is physically shared, plus shared reference and PPS | everything; the residual is still calibrated |
| Shared reference | common 10&nbsp;MHz (or 5&nbsp;MHz) reference and PPS-aligned time | everything after calibration; recalibrate after a tune that is not repeatable |
| Time-aligned | separate GPS references, but aligned time | recording and raw output only; coherent beams and beamformed transmit are refused |
| Incoherent | anything else | single-channel use only |

## Geometry and beams

The array is described in a local East-North-Up frame with compass azimuth, and the steering conventions, including the sign of every true-time delay, are written down once and frozen. Beams can be formed by delay-and-sum, null projection, MVDR or LCMV; weights carry over to transmit by reciprocity; and every geometry is validated for grating lobes and squint before it is used.

The numbers the track is built to reproduce are textbook ones:

| 8-element half-wave line array | Uniform | 30 dB Dolph-Chebyshev |
|---|---:|---:|
| Half-power beamwidth | 12.8° | 16.4° |
| Peak sidelobe | −12.8 dB | −30.0 dB |
| Taper efficiency | 1 | 0.842 |

The uniform array's directivity is 8, its element count; the taper trades 3.6° of beamwidth for 17.2&nbsp;dB of sidelobe suppression.

Precision here is physical, not numerical. A 2&nbsp;m cable at 2.4&nbsp;GHz with an uncertain velocity factor near 0.66 already carries a phase uncertainty of about ±87°, which is why the track calibrates chains instead of trusting their lengths. Geometry is checked with the same care: the validator recognises line, rectangular, circular, triangular and irregular layouts, and a triangular lattice saves 13.4 % of the elements of a square one.

## Amplified front ends

Each receive and transmit chain is described stage by stage (amplifiers, attenuators, couplers, switches, cables) with gain, noise figure, compression and saturation. Power amplifiers are modelled with a Rapp curve including AM/PM, so a burst is judged by its predicted peak at every stage, not by its digital amplitude. The model also predicts the receive-side exposure of the array's own receivers and of any co-located KrakenSDR.

Coherent transmission concentrates power: radiated power grows with the square of the element count. Eight elements of 1&nbsp;W each through 5&nbsp;dBi antennas already reach 53&nbsp;dBm EIRP, about 200&nbsp;W.

## The transmit safety model

</div>
<figure class="figure-card figure-scroll" tabindex="0" aria-label="Transmit safety state machine">
  {% include fig-txsafety.svg %}
  {% include fig-txsafety-tall.svg %}
  <figcaption>Transmit escalates one guarded step at a time; any trip latches FAULT, and only an operator returns it to SAFE.</figcaption>
</figure>
<div class="prose" markdown="1">

Transmit is off unless the configuration enables it, and a process that starts without it can never create a transmit stream. With transmit enabled and a `tx` or `trx` mode configured, the process starts in **SAFE**; with a receive-only mode it stays in DISABLED. Arming runs fifteen ordered checks: state, mode, a typed confirmation phrase (on an interactive terminal for radiating), operator identity, backend, configuration, frequency against allow and deny bands, reference lock and coherence class, interlock, dummy load, telemetry freshness and limits, static power feasibility, calibration, and, for radiating, an authorisation, an EIRP ceiling and an exposure distance. The first failure refuses.

Every burst then needs its own permit, checked against the final sample buffer. Faults such as reverse power, VSWR, over-temperature, an opened interlock, loss of reference or LO lock, a hung send, an underflow storm or a lost heartbeat mid-burst are **trips**: the process shuts RF down in a fixed order and latches **FAULT**, which only an operator can clear, and only back to SAFE.

<div class="callout risk" markdown="1">
**Software is not an interlock.** These checks act on a millisecond scale, and an idle DAC still leaks carrier: no samples is not the same as no RF. For amplified systems the design requires hardware protection, a PA foldback and an emergency-stop loop wired to the PA enable.
</div>

## Integration with the passive chain

The active runner emits ordinary version-8 frames on a port-5000-compatible server and serves status on a `:5002`-style line protocol, honouring the federation port offsets, so existing consumers and tools can read it. The electronic beam direction goes in a sidecar file, not the header. GNU Radio runs as a separate process from generated, self-contained flowgraphs, connected over ZMQ, and receive flowgraphs can be exported for GNU Radio Companion. Hooking the track into `ini_checker`, `unit_test.sh` and the top-level documentation is deliberately deferred to a separate change.

</div>
</div>
