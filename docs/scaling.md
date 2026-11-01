---
layout: default
title: Scaling and clustering
description: Running several HeIMDALL instances on one host and coordinating them across hosts, with the status of every feature.
---
<div class="wrap">
<div class="prose" markdown="1">

<p class="kicker">Scaling and clustering</p>

# More receivers, the same guarantees

A single KrakenSDR gives five coherent channels. To watch more of the spectrum, or more of the sky, you add receivers. The scaling program is building this in two steps: several instances on one computer, then instances spread over several computers and driven as one fleet. Every step is opt-in, and none of them changes what an existing single-unit station sees. Features from packages that are not done yet are described as designed.

{% include status_legend.html %}

## One instance is one coherent island

An instance is one complete pipeline with its own receivers. Its channels share one sample clock and one noise source, so they are phase-coherent with each other. Channels of two different instances are not. Every scaling step here adds islands; none of them makes two islands coherent, which is why calibration and timing stay per instance.

<table id="defaults">
<caption>One instance at the default configuration</caption>
<thead><tr><th scope="col">Quantity</th><th scope="col" class="num">Per instance</th></tr></thead>
<tbody>
<tr><td>Coherent channels</td><td class="num">5 at 2.4&nbsp;MS/s</td></tr>
<tr><td>Frame</td><td class="num">2<sup>20</sup> samples, 0.437&nbsp;s</td></tr>
<tr><td>USB ingress</td><td class="num">24.0&nbsp;MB/s</td></tr>
<tr><td>Complex-float egress</td><td class="num">96&nbsp;MB/s (768&nbsp;Mbit/s)</td></tr>
<tr><td>One frame on the wire, with header</td><td class="num">41,944,064&nbsp;B</td></tr>
<tr><td>Resident shared memory</td><td class="num">180&nbsp;MiB</td></tr>
<tr><td>Private memory of the C stages</td><td class="num">about 85&nbsp;MiB</td></tr>
<tr><td>Pinned cores</td><td class="num">4</td></tr>
</tbody>
</table>

</div>
<figure class="figure-card figure-scroll" tabindex="0" aria-label="Cluster diagram">
  {% include fig-cluster.svg %}
  {% include fig-cluster-tall.svg %}
  <figcaption>Two hosts, three instances. The coordinator drives every instance's control and status ports; the IQ router reads every instance's stream.</figcaption>
</figure>
<div class="prose" markdown="1">

## Several instances on one host

Instance 0 keeps its unprefixed names and base ports. Each instance N prefixes its FIFOs and shared memory with `inst<N>_`, moves its port window by N&nbsp;×&nbsp;`port_stride` (default 100), and opens its receivers by EEPROM serial number instead of by USB order, with a defence against two units that carry the same serial.

<p class="status-line">{% include badge.html wp="WP-14" label=true %}</p>

In the provisioning design, instance N lives in its own directory under one install root: the code is symlinked from the root, while the configuration, control files, logs and database are its own. Because every stage reads its configuration and control files relative to the working directory, this isolates an instance without changing a single configuration path in the code. The instance id is read only from the instance's own configuration file, never from the environment.

**Sizing.** An instance pins four cores, so an 8-core host fits two. One instance uses 81.6&nbsp;% of the TCP goodput of Gigabit Ethernet; two need 1536&nbsp;Mbit/s and exceed it, so a second instance on the same link needs local consumers, shared-memory output or a faster network. The apparent `/dev/shm` object size of an instance is larger than its resident 180&nbsp;MiB: 1.45&nbsp;GiB for two instances and 2.89&nbsp;GiB for four.

| Feature | Package | Status |
|---|---|---|
| Serial-number device map, duplicate-serial defence, ZMQ bind address, USB ring depth | WP-03 | {% include badge.html wp="WP-03" %} |
| Per-instance database directory and metadata | WP-07 | {% include badge.html wp="WP-07" %} |
| Synthetic source with a ZMQ control responder, for testing without hardware | WP-10 | {% include badge.html wp="WP-10" %} |
| Configuration limits, port-window and serial collision checks | WP-11 | {% include badge.html wp="WP-11" %} |
| Prefixed FIR coefficients, `size_t` sizing, config-sized segments, multi-client `iq_server` | WP-04 | {% include badge.html wp="WP-04" %} |
| Host registry, instance lock, scoped stop, supervisor, federation launchers | WP-12 | {% include badge.html wp="WP-12" %} |
| Capacity model and pre-flight check | WP-13 | {% include badge.html wp="WP-13" %} |
| Instance directories, cluster configuration generator, serial programming | WP-14 | {% include badge.html wp="WP-14" %} |
| Supervised, coordinator, router and watchdog systemd units; udev rules | WP-16 | {% include badge.html wp="WP-16" %} |

## Clustering across hosts

A cluster member is named `host:instance_id`, and the shared helpers for membership, request framing and parallel fan-out are done. The rest of this section is the design.

<p class="status-line">{% include badge.html wp="WP-08" label=true %}</p>

The **coordinator** listens on port 6000 and is designed to drive the instances, not the other way round: it will poll each member's status on `:5002` and send commands to its `:5001`, in parallel, collecting a per-target result that says whether each command was applied. It will roll member health up into one view, merge their events, and partition a master frequency schedule over the healthy members, either round-robin or as contiguous ranges, then reconcile members that restart.

<p class="status-line">{% include badge.html wp="WP-08" label=true %} {% include badge.html wp="WP-05" label=true %}</p>

With several candidate coordinators, one is to hold a leader lease of 6&nbsp;s while the others stand by. Each command is preceded by an `XACK` that carries the leader's term; members persist the highest term they have seen, so a deposed leader cannot steer them. At the specified defaults a member answers an extended command within 24&nbsp;s (12&nbsp;s on a legacy connection), and a fleet-wide mutation has a fixed 40&nbsp;s deadline.

<p class="status-line">{% include badge.html wp="WP-09" label=true %}</p>

The **IQ router** will read the instances' `:5000` streams as an ordinary client and serve one multi-client output on `:7000`, with statistics on `:7001`. Each client gets a bounded queue, so a slow client is cut off rather than allowed to stall a reader. The router is designed to tag frames with their source, detect loss, and align streams within a bounded window. At two sources and eight clients its queues are bounded at 1.48&nbsp;GiB.

<p class="status-line">{% include badge.html wp="WP-15" label=true %}</p>

**`heimdall-ctl fleet`** is the operator's handle on all of this: status, health, members, tune, gain, recalibrate, schedule, rebalance and merged events. It finds the coordinator from flags, the environment or the configuration, and follows one `not_leader` redirect. `heimdall-ctl router` reads the router's statistics.

| Feature | Package | Status |
|---|---|---|
| Shared helpers: membership parser, request framing, parallel fan-out, version | WP-02 | {% include badge.html wp="WP-02" %} |
| Acknowledged control (`XACK`), fencing, control snapshot, wire recalibration | WP-05 | {% include badge.html wp="WP-05" %} |
| `heimdall-ctl fleet`, `router`, `ports`, `instances`, `peers` | WP-15 | {% include badge.html wp="WP-15" %} |
| Extended status, staleness, registrar wiring, node attribution in the header | WP-06 | {% include badge.html wp="WP-06" %} |
| Coordinator protocol v2, lease election, health state machine, scheduler | WP-08 | {% include badge.html wp="WP-08" %} |
| IQ router: bounded queues, tagging, loss detection, alignment, stats | WP-09 | {% include badge.html wp="WP-09" %} |
| Recording and replay, IQ client probe | WP-20 | {% include badge.html wp="WP-20" %} |
| Cross-package integration tests | WP-17b | {% include badge.html wp="WP-17b" %} |

<div class="callout risk" markdown="1">
**A known limit before WP-04 closes.** The multi-client `iq_server` waits for every client for at most 0.8 of a frame period by default, which is 349.5&nbsp;ms. Delivering a 40&nbsp;MiB frame in that time needs 120&nbsp;MB/s, slightly more than the 117.7&nbsp;MB/s that Gigabit Ethernet carries as TCP goodput. As specified, a GbE client would miss the deadline on every frame. The factor or its basis is being revisited.
</div>

## Read further

- [Scaling and clustering, in full]({{ '/guide/scaling_and_clustering.html' | relative_url }}): limits, port table, start and stop order, worked examples, checklists.
- [Capacity planning]({{ '/guide/capacity_planning.html' | relative_url }}): the resource model and how to measure a host.
- [Networking and time sync]({{ '/guide/networking_and_time_sync.html' | relative_url }}): every socket, bind address and clock requirement.
- [Deployment recipes]({{ '/guide/deployment_recipes.html' | relative_url }}): copy-pasteable set-ups for the common deployments.

</div>
</div>
