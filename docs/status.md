---
layout: default
title: Verification status
description: What the verification review checked, which estimates it corrected, where each work package stands, and the open risks.
---
<div class="wrap">
<div class="prose" markdown="1">

<p class="kicker">Verification review · {{ site.as_of }}</p>

# Status

Every figure on the overview pages (home, architecture, scaling, status, active arrays) was checked against the current tree, the pristine pre-program baseline, the design specification and, for the active track, primary vendor sources. The synced guides in the library are reproduced as written and were not all checked; where a guide disagrees with a figure here, this page has the verified value. Nothing here is committed or tagged yet: "done" means implemented and reviewed in the working tree.

**The foundations hold.** The baseline still passes its 15 hardware-free modules and builds cleanly in all three lanes. In the current tree, the default 1024-byte header and the seed-0 synthetic stream are byte-identical to the baseline. The arithmetic behind the [default configuration]({{ '/scaling.html#defaults' | relative_url }}) is solid. Most corrections only tighten a figure. Two need action: one finished package has a test that now fails, and the default multi-client deadline cannot be met over Gigabit Ethernet.

## Claims checked

<div class="tally" role="img" aria-label="184 claims: 131 verified, 29 corrected, 2 refuted, 16 in flight, 6 unverifiable">
  <span class="t-ok" style="width:71.2%"></span><span class="t-fix" style="width:15.8%"></span><span class="t-ref" style="width:1.1%"></span><span class="t-fly" style="width:8.7%"></span><span class="t-hw" style="width:3.3%"></span>
</div>
<ul class="tally-key">
  <li><i style="background:var(--shipped)"></i>verified</li>
  <li><i style="background:var(--review)"></i>corrected</li>
  <li><i style="background:var(--risk)"></i>refuted</li>
  <li><i style="background:var(--progress)"></i>in flight</li>
  <li><i style="background:var(--planned)"></i>unverifiable</li>
</ul>

| Area | Checked | Verified | Corrected | Refuted | In flight | Unverifiable |
|---|--:|--:|--:|--:|--:|--:|
| Package reports | 14 | 10 | 2 | 1 | 1 | 0 |
| Capacity and arithmetic | 49 | 35 | 11 | 0 | 0 | 3 |
| Documentation (theory, glossary) | 50 | 36 | 6 | 1 | 6 | 1 |
| Audit headline and top-20 gaps | 32 | 18 | 5 | 0 | 9 | 0 |
| Active-arrays research | 39 | 32 | 5 | 0 | 0 | 2 |
| **Total** | **184** | **131** | **29** | **2** | **16** | **6** |

Of the audit's top 20 gaps, one is fixed, two are partly fixed, nine are being fixed by packages in flight and eight are still open.

## Corrected estimates

| Claim | Was | Verified |
|---|---|---|
| Calibration-burst outage | ~4.4&nbsp;s every ~305&nbsp;s (1.4&nbsp;%) | ~2.2–2.6&nbsp;s of samples every ~302&nbsp;s (0.7–0.9&nbsp;%); the DATA gap is ~3&nbsp;s |
| Pipe buffer | exactly 2,622,464&nbsp;B | 4&nbsp;MiB: the kernel rounds up to a power of two |
| `rtl_daq` memory per channel | ~5&nbsp;MiB | 10&nbsp;MiB |
| Every preset at 96&nbsp;MB/s | all presets | `unit_test_k4` 32&nbsp;MB/s; `kerberos_default` 76.8&nbsp;MB/s |
| Shared-memory objects, two / four instances | 1.5 / 2.9&nbsp;GiB | 1.45 / 2.89&nbsp;GiB |
| One unit saturates USB 2.0 | saturates | 24–32&nbsp;MB/s against a ~53&nbsp;MB/s theoretical bulk ceiling |
| Complex-float ceiling at 2.56&nbsp;MS/s | ≤&nbsp;~100&nbsp;MB/s | 102.4&nbsp;MB/s |
| GbE load of one instance | 77&nbsp;% | 81.6&nbsp;% of TCP goodput |
| Multi-client deadline 0.8 × frame period over GbE | achievable | needs 120&nbsp;MB/s, above the 117.7&nbsp;MB/s goodput |
| `rtl_daq` accepts 32 channels | accepts | rejects; the ceiling is 31 |
| Triangular-lattice element saving | 15&nbsp;% | 13.4&nbsp;% |
| Velocity-factor phase error, 2&nbsp;m at 2.4&nbsp;GHz | ±97° | ±87° at a velocity factor of 0.66 |
| Per Vices `ref_locked` sensor | commented out | live, alongside `pps_detected` |
| Hardware-free suite (review snapshot) | 21 of 21, 14 pending | 25 of 27 present pass; 35 registered |

Six claims could not be settled. Three need hardware: the USB bus budget per hub, the share of a Raspberry Pi 4 core that `delay_sync` needs, and the latency of a deferred command (bounded at 0.437&nbsp;s in steady state). The 40&nbsp;MiB memlock example has no derivation, and two active-track facts, the amateur and ITU spurious-emission rule text and the `python3-uhdpv` import name, were not fetched from their sources. One further figure, the exact number of blocks in a calibration burst, can also only be measured on hardware.

## Work packages

</div>
<div class="prose wide" markdown="1">

| Package | Scope | State | Tests |
|---|---|---|---|
{% assign _sorted = site.data.packages | sort: "id" %}{% for s in site.data.pkg_states %}{% for p in _sorted %}{% if p.badge == s.badge and p.id != "WP-22" %}| {{ p.id }} | {{ p.title }} | {% include badge.html wp=p.id %} | {% if p.tests != "" %}{{ p.tests }}{% else %}not started{% endif %} |
{% endif %}{% endfor %}{% endfor %}

</div>
<div class="prose" markdown="1">

{% include status_legend.html %}

WP-12, WP-13 and WP-14 have files on disk but have not been reviewed. WP-10's synthesizer is correct, but its new loop-model test binds module functions as methods; a two-line `staticmethod()` change fixes it. The specification's documentation packages are not in the package list.

## Open risks

- **A test defect keeps the CI lane red.** Until the WP-10 fix lands, the hardware-free suite cannot go green.
- **The multi-client deadline is not met over GbE.** At the defaults every Gigabit client becomes a straggler; the factor or its basis needs revisiting before WP-04 closes.
- **WP-05 behaviour on Linux is unsettled.** Its tests pass on macOS; one Linux run errored and another hung. It needs a clean re-run once the file settles.
- **A federation fan-out regression.** WP-08's fan-out test returns `reset` instead of a success reply; it is in flight and needs a clean re-run.
- **Opt-in defaults stay conservative.** A default install still shows most of the top-20 gaps after the packages land, for example a single control connection and a ZMQ port bound to every interface. Two gaps stay open until the launch scripts (WP-12) and packaging (WP-16) are done.
- **Documentation has no owner.** The specification's documentation packages (WP-18 and WP-19) are not in the package list, so five audit gaps have no owner, and the glossary's "today" wording has already drifted from the current defaults.
- **A latent partial write in `GAIN`, also in the baseline.** A value outside the valid gain table mid-list leaves earlier channels written, to be applied on the next change.
- **Some figures need hardware.** The four above, with no hardware lane yet to measure them.

</div>
</div>
