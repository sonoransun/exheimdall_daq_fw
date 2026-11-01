---
layout: default
title: Library
description: The full HeIMDALL DAQ documentation, synced from the repository's Documentation directory.
---
<div class="wrap">
<div class="prose">
<p class="kicker">Library</p>
<h1>The documentation</h1>
<p class="lede">The complete reference, from first principles to the wire format. These pages mirror <code>Documentation/</code> in the repository exactly; a sync tool keeps them in step.</p>
<p class="small muted">Sections the authors marked with a work package carry a note with that package's state; two guides that describe unreviewed tooling carry one at the top. Unmarked text may still describe unreviewed work, so check the <a href="{{ '/status.html' | relative_url }}">status page</a>. Where a guide and the source code disagree, the code wins.</p>
</div>
{% for g in site.data.guide %}
<section class="lib-group" aria-labelledby="g-{{ forloop.index }}">
  <h2 id="g-{{ forloop.index }}">{{ g.group }}</h2>
  <ul class="lib-list">
    {%- for d in g.docs %}
    <li><a href="{{ '/guide/' | append: d.slug | append: '.html' | relative_url }}"><span class="t">{{ d.title }}</span><span class="d">{{ d.summary }}</span><span class="f">Documentation/{{ d.file }}</span></a></li>
    {%- endfor %}
  </ul>
</section>
{% endfor %}
<div class="prose lib-more">
<p class="small muted"><a href="{{ '/guide/index.html' | relative_url }}">Repository map</a>: the documentation index from <code>Documentation/README.md</code>, with reading paths and where things live in the source tree.</p>
<p class="small muted">The original 2020 firmware manual, <code>Documentation/HDAQ_firmware_ver1.0.20201130.pdf</code>, is kept in the repository for reference only. It predates the current header, build system and every subsystem described here.</p>
</div>
</div>
