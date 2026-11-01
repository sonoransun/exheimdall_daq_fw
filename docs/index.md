---
layout: default
title: Overview
main_class: home
description: HeIMDALL DAQ turns the five tuners of a KrakenSDR into one phase-coherent receiver. A scaling program, in progress, adds opt-in scaling, clustering, observability and operations, and an active-array track is in design.
---
<section class="hero wrap" aria-labelledby="hero-title">
  <p class="kicker">Coherent acquisition for KrakenSDR</p>
  <h1 id="hero-title">Five tuners, one instrument.</h1>
  <p class="lede">HeIMDALL DAQ is the firmware chain behind the KrakenSDR. It takes five independent RTL-SDR tuners that share one clock and one noise source, measures how each one differs from the others, and removes that difference, so that downstream software receives a single phase-coherent stream.</p>
  <figure class="figure-card">
    {% include fig-coherence.svg %}
    <div class="coherence-legend" aria-hidden="true">
      <span><strong>Raw tuners</strong>independent phase and delay</span>
      <span><strong>Calibrate</strong>shared noise source</span>
      <span><strong>Coherent</strong>one phase reference</span>
    </div>
    <figcaption>The same five signals before and after calibration. Every bearing and every range measurement downstream depends on the right-hand picture.</figcaption>
  </figure>
  <ul class="glance" aria-label="At a glance">
    <li><span class="v">5 <small>ch</small></span><span class="k">coherent channels at 2.4&nbsp;MS/s each</span></li>
    <li><span class="v">0.437 <small>s</small></span><span class="k">per frame of 2<sup>20</sup> samples</span></li>
    <li><span class="v">24 → 96 <small>MB/s</small></span><span class="k">USB in, complex float out</span></li>
    <li><span class="v">12.5 <small>ns</small></span><span class="k">lock tolerance (0.03 samples)</span></li>
    <li><span class="v">&lt;1 <small>%</small></span><span class="k">of samples spent on calibration</span></li>
    <li><span class="v">1024 <small>B</small></span><span class="k">frame header, unchanged by default</span></li>
  </ul>
</section>

<div class="wrap">
<div class="prose" markdown="1">

## Why coherence matters

An antenna array learns where a signal comes from by comparing what each element hears. The bearing is written in the small phase differences between channels, and the range of a passive-radar echo in their timing. A tuner that is a few nanoseconds late, or whose local oscillator starts at an arbitrary phase, writes its own error into exactly the quantity you want to measure.

The KrakenSDR solves the hardware half of the problem: its tuners run from one reference clock and can be switched onto one internal noise source. HeIMDALL solves the other half. It injects that noise, cross-correlates the channels, and corrects their delay and phase until the residual offset is a small fraction of a sample. Then it keeps checking, and recalibrates when the alignment drifts.

The reward is real. Five coherent channels add 6.99&nbsp;dB of array gain, and at 2.4&nbsp;MS/s a passive-radar range bin is 125&nbsp;m wide.

## The journey of a sample

</div>
<figure class="figure-card">
  {% include fig-pipeline.svg %}
  {% include fig-pipeline-tall.svg %}
  <figcaption>Data flows through shared-memory rings, stage 1 to 5; control (dashed) runs through <code>hw_controller</code>. Rates are at the default configuration.</figcaption>
</figure>
<div class="prose" markdown="1">

<ol class="steps">
<li><strong>rtl_daq</strong> reads all five tuners over USB, one reader thread per channel, and emits blocks of 8-bit IQ, each prefixed with a 1024-byte header that carries the tuning, gain and telemetry of the moment.</li>
<li><strong>rebuffer</strong> reshapes blocks into frames of 2<sup>20</sup> samples per channel, in a double-buffered shared-memory ring.</li>
<li><strong>decimate</strong> converts the 8-bit samples to complex float. When <code>decimation_ratio</code> is above 1 it also low-pass filters and decimates; that is off at the defaults, so the stream grows to four times the USB ingress.</li>
<li><strong>delay_sync</strong> estimates each channel's delay by cross-correlation, corrects the fractional part by fine-tuning the sample rate, and fixes amplitude and phase by eigendecomposition. When the residual is inside tolerance, the frame is locked.</li>
<li><strong class="sans">The output stage</strong> hands coherent frames to the consumer (direction finding, passive radar, GNU Radio). By default consumers map the <code>delay_sync_iq</code> ring directly (<code>shmem</code>); with <code>eth</code>, <code>iq_server</code> streams frames over TCP&nbsp;:5000.</li>
</ol>

Beside the data path, `hw_controller` runs the calibration loop. Every 687 frames, about five minutes, it switches the noise source on for a short burst of 2.2 to 2.6&nbsp;seconds of samples.

## What the scaling program adds

6 of 19 packages are done; the line under each card shows the state of the packages it depends on.

The chain was built for one KrakenSDR on one computer. The scaling work lets it grow without changing what an existing station sees: every new feature sits behind a key or a flag that is off by default.

</div>
<div class="cards">
  <article class="card card-accent">
    <h3>Scaling on one host</h3>
    <p>Several instances side by side, each with its own receivers opened by serial number, its own prefixed shared memory and FIFOs, and its own port window. One instance pins four cores, so an 8-core host fits two.</p>
    <div class="card-foot">{% include pkg_summary.html wps="WP-03,WP-07,WP-04,WP-12" %}</div>
  </article>
  <article class="card card-accent">
    <h3>Clustering across hosts</h3>
    <p>A coordinator on port 6000 is designed to poll every member's status and fan commands out in parallel, with leases, fencing and a master schedule. An IQ router will merge many streams into one multi-client output.</p>
    <div class="card-foot">{% include pkg_summary.html wps="WP-02,WP-08,WP-09,WP-15" %}</div>
  </article>
  <article class="card card-accent">
    <h3>Observability</h3>
    <p>Opt-in header slots 103 to 113 for router sequence and receive time, source and node identity, decimator drops, and calibration quality and counters: defined now, stamped once the stages land. Then extended status with staleness, and acknowledged control.</p>
    <div class="card-foot">{% include pkg_summary.html wps="WP-01,WP-05,WP-04,WP-06,WP-09" %}</div>
  </article>
  <article class="card card-accent">
    <h3>Operations</h3>
    <p>Configuration checks that catch port and serial collisions, now in review. To follow: a capacity pre-flight, provisioning of instance directories, supervised systemd units, and recording and replay.</p>
    <div class="card-foot">{% include pkg_summary.html wps="WP-11,WP-13,WP-14,WP-16,WP-20" %}</div>
  </article>
  <article class="card card-warm card-wide">
    <h3>A new track: active arrays</h3>
    <p>A separate, opt-in capability for coherent arrays built on USRP and Per Vices hardware, with GNU Radio integration, precision phased geometry, amplified front ends and a transmit safety model.</p>
    <div class="card-foot"><span class="badge badge-design">spec final · AT-1 in progress</span> <a href="{{ '/active.html' | relative_url }}">Preview</a></div>
  </article>
</div>
<div class="prose" markdown="1">

{% include status_legend.html compact=true %}

<div class="callout">
<p><strong>Nothing changes until you ask.</strong> With every new key absent, the default 1024-byte header is byte-identical to the pre-program baseline (same SHA-1), and the seed-0 synthetic stream produces the same payloads (same SHA-256). The generic, KFR and ASan build lanes compile with zero warnings. Verified on 2026-11-01, before WP-04 and WP-06 land; to be re-checked when they close.</p>
</div>

## Where it stands

A verification pass checked 184 claims made by the design, the package reports and the documentation against the code and against primary vendor sources. 131 held as stated and 29 needed a corrected figure; the rest are refuted, still in flight, or could not be settled.

<div class="tally" role="img" aria-label="184 claims: 131 verified, 29 corrected, 2 refuted, 16 in flight, 6 unverifiable">
  <span class="t-ok" style="width:71.2%"></span><span class="t-fix" style="width:15.8%"></span><span class="t-ref" style="width:1.1%"></span><span class="t-fly" style="width:8.7%"></span><span class="t-hw" style="width:3.3%"></span>
</div>
<ul class="tally-key">
  <li><i style="background:var(--shipped)"></i>131 verified</li>
  <li><i style="background:var(--review)"></i>29 corrected</li>
  <li><i style="background:var(--risk)"></i>2 refuted</li>
  <li><i style="background:var(--progress)"></i>16 in flight</li>
  <li><i style="background:var(--planned)"></i>6 unverifiable</li>
</ul>

Of the 19 work packages, six are done, three are in review, seven are being implemented and three have not started. The hardware-free suite now registers 35 modules, up from 15; in the review snapshot 25 of the 27 then present passed, and the rest wait on packages still to land. One reviewed package has a two-line test defect that keeps the hardware-free suite red; see [Status]({{ '/status.html' | relative_url }}).

</div>
<ul class="lib-list next-steps">
  <li><a href="{{ '/architecture.html' | relative_url }}"><span class="d">How the chain works</span><span class="t">Architecture →</span></a></li>
  <li><a href="{{ '/scaling.html' | relative_url }}"><span class="d">Grow it</span><span class="t">Scaling and clustering →</span></a></li>
  <li><a href="{{ '/library.html' | relative_url }}"><span class="d">Look it up</span><span class="t">The library →</span></a></li>
</ul>
</div>
