#!/usr/bin/env python3
"""Sync Documentation/*.md into the GitHub Pages site under docs/guide/.

Standard library only. Run from anywhere:

    python3 docs/_tools/sync_docs.py            # write docs/guide/*.md and docs/_data/guide.yml
    python3 docs/_tools/sync_docs.py --check    # exit 1 if the synced copies are out of date

For every Documentation/<name>.md the tool writes docs/guide/<name>.md
(README.md becomes guide/index.md) and:

* adds Jekyll front matter: layout, title (from the first "# " heading),
  description, source_file;
* pins headings to their GitHub-style anchor ({#slug}) where kramdown allows it
  (ids starting with a letter), so links written for github.com keep working;
* rewrites links between documents: foo.md -> foo.html, foo.md#a -> foo.html#a;
* turns links that leave Documentation/ (../CHANGELOG.md, ../packaging/...) into
  {% include repo_link.html %}, which links to site.repo_url when it is set in
  _config.yml and renders the path as code text when it is empty;
* turns "<!-- verify-after: WP-xx -->" markers into a visible status note
  ({% include verify_note.html %}) that reads docs/_data/packages.yml. A marker
  on a line of its own (or in a heading) is moved directly under its section
  heading, merged with the section's other markers; a marker inside running
  text or a table cell stays where it is, as an inline note;
* adds a page-level note (front matter verify_wp) for guides listed in
  PAGE_VERIFY, which describe unreviewed work without carrying markers, and
  sets has_badges: true on every page that shows a note;
* protects any literal Liquid syntax in the source with {% raw %}.

Fenced code blocks and inline code spans are never rewritten. Generated files
carry "sync_generated: true"; generated files whose source disappeared are
removed. docs/_data/guide.yml (read by library.md) is rewritten each run.
"""

import argparse
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.dirname(HERE)
REPO = os.path.dirname(DOCS)

# Guides that describe unreviewed work packages but carry no verify-after markers
# in Documentation/. The guide layout shows a page-level note for them.
PAGE_VERIFY = {
    "capacity_planning": "WP-13,WP-04",
    "glossary_faq": "WP-05,WP-08",
}

# Synced but not shown as a card in the library (library.md links it separately).
LIBRARY_EXCLUDE = {"README"}

# Curated grouping and one-line summaries for the library page. A document that is
# not listed here still syncs; it lands in "More" with its first paragraph as summary.
GROUPS = [
    ("Start here", [
        ("architecture", "Process pipeline, shared-memory and FIFO signalling, control plane, sync state machine, federation and the RF front-end subsystems."),
        ("theory_coherent_acquisition", "Why the chain exists in this form: what coherence means, what breaks it, and how calibration restores it."),
        ("glossary_faq", "Terms you meet when operating or integrating the chain, and the questions operators ask most often."),
    ]),
    ("Build and deploy", [
        ("hardware_setup", "From the box to a Linux host rtl_daq can open: serial numbers, second units, larger arrays, USB."),
        ("developer_guide", "Building the C core, the Python environment, test lanes, CI, and the invariants that protect the default path."),
        ("deployment_recipes", "End-to-end, copy-pasteable recipes for the common deployments, each ending with a check."),
        ("tutorial_directional_df", "A worked walkthrough of the directional_df preset: amplified receivers, high-gain antenna, rotator."),
        ("upgrade_and_rollback", "Upgrading an existing station, the default-path ledger, and how to roll back."),
    ]),
    ("Scale and operate", [
        ("scaling_and_clustering", "Several instances on one host or many: limits, namespacing, topologies, coordination, the IQ router."),
        ("capacity_planning", "Formulas, worked numbers and measurement steps for sizing a host and its network."),
        ("networking_and_time_sync", "Every socket the chain opens, where it binds, how to protect it, and the clock requirements."),
        ("operations_runbook", "Symptoms mapped to causes and fixes, with every log file, PID file and diagnostic command."),
    ]),
    ("Reference", [
        ("protocols", "The normative wire reference: IQ frame and header, ZMQ tuner control, TCP control, status, events, coordinator and router."),
        ("configuration", "Every daq_chain_config.ini section and key, with type, default and behaviour, plus the preset catalog."),
        ("consumer_guide", "Exact rules for attaching a downstream program, accepting frames and detecting loss."),
    ]),
]

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
MARKER_RE = re.compile(r"<!--\s*verify-after:\s*([^>]*?)\s*-->")
LINK_RE = re.compile(r"(!?)\[((?:`[^`]*`|[^\]\[])*)\]\(\s*<?([^)\s>]+)>?((?:\s+\"[^\"]*\")?)\s*\)")
CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
REFDEF_RE = re.compile(r"^(\s{0,3}\[[^\]]+\]:\s*)(\S+)(.*)$")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
LIQUID_RE = re.compile(r"\{\{|\{%")
KRAMDOWN_ID_RE = re.compile(r"^[A-Za-z][\w:-]*$", re.ASCII)


def sentence_case(title):
    """Library card titles in sentence case; acronyms and identifiers are kept."""
    out = []
    for i, w in enumerate(title.split(" ")):
        core = w.strip("()[],:;")
        keep = (i == 0 or not core or "_" in core or any(c.isdigit() for c in core)
                or any(c.isupper() for c in core[1:]))
        out.append(w if keep else w.lower())
    return " ".join(out)


def slug_for(stem):
    return "index" if stem.lower() == "readme" else stem


def plain_heading_text(text):
    """Text of a heading as GitHub renders it (markup removed)."""
    t = MARKER_RE.sub("", text)
    t = re.sub(r"\s+\{#[^}]*\}\s*$", "", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)  # links -> text
    t = re.sub(r"<[^>]+>", "", t)  # inline html
    t = t.replace("`", "")
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)
    return t.strip()


def github_slug(text):
    t = plain_heading_text(text).lower()
    out = []
    for ch in t:
        cat = unicodedata.category(ch)
        if ch in " ":
            out.append("-")
        elif ch in "-_":
            out.append(ch)
        elif cat[0] in ("L", "N") or cat == "Mn" or cat == "Pc":
            out.append(ch)
        # everything else (punctuation, symbols) is dropped, as on GitHub
    return "".join(out)


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def text_to_html(text):
    """Link text -> safe inline HTML (code spans kept as <code>)."""
    parts, pos = [], 0
    for m in CODE_SPAN_RE.finditer(text):
        parts.append(html_escape(text[pos:m.start()]))
        parts.append("<code>%s</code>" % html_escape(m.group(2).strip()))
        pos = m.end()
    parts.append(html_escape(text[pos:]))
    return "".join(parts)


def liquid_str(s):
    # include parameters are double-quoted strings; html_escape already removed '"'
    return s.replace("\\", "&#92;")


def first_paragraph(lines):
    para, in_fence, seen_h1 = [], False, False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            if para:
                break
            continue
        if in_fence:
            continue
        s = line.strip()
        if not seen_h1:
            if s.startswith("# "):
                seen_h1 = True
            continue
        if not s:
            if para:
                break
            continue
        if s.startswith(("#", "|", "<", "-", "*", ">")) or re.match(r"^\d+\.", s):
            if para:
                break
            continue
        para.append(s)
    text = " ".join(para)
    text = MARKER_RE.sub("", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("**", "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 220:
        cut = text[:220].rsplit(" ", 1)[0].rstrip(",;:")
        text = cut + "…"
    return text


class Converter:
    def __init__(self, src_dir, repo_root, names):
        self.src_dir = os.path.abspath(src_dir)
        self.repo_root = os.path.abspath(repo_root)
        self.names = set(names)  # stems that are synced
        self.liquid = []
        self.warnings = []
        self.marker_count = 0

    def _token(self, liquid):
        self.liquid.append(liquid)
        return "\x00%d\x00" % (len(self.liquid) - 1)

    @staticmethod
    def _ids(raw):
        return [i.strip() for i in raw.split(",") if i.strip()]

    def _verify(self, ids, block=False):
        if not isinstance(ids, list):
            ids = self._ids(ids)
        self.marker_count += 1
        return self._token('{%% include verify_note.html wp="%s"%s %%}'
                           % (liquid_str(",".join(ids)), " block=true" if block else ""))

    def _rewrite_target(self, target, text, is_image, fname):
        """Return replacement markdown/token for a link, or None to keep it."""
        if target.startswith("#") or SCHEME_RE.match(target) or target.startswith("//"):
            return None
        path, _, anchor = target.partition("#")
        if not path:
            return None
        resolved = os.path.normpath(os.path.join(self.src_dir, path))
        inside_src = os.path.dirname(resolved) == self.src_dir
        stem, ext = os.path.splitext(os.path.basename(resolved))
        if inside_src and ext.lower() == ".md" and stem in self.names:
            new = slug_for(stem) + ".html" + ("#" + anchor if anchor else "")
            return "%s[%s](%s)" % ("!" if is_image else "", text, new)
        # leaves the synced set: point at the repository
        rel = os.path.relpath(resolved, self.repo_root)
        if rel.startswith(".."):
            self.warnings.append("%s: link escapes the repository, left as text: %s" % (fname, target))
            return "`%s`" % target
        rel = rel.replace(os.sep, "/")
        if path.endswith("/") or os.path.isdir(resolved):
            rel = rel.rstrip("/") + "/"
        if anchor:
            rel_a = rel + "#" + anchor
        else:
            rel_a = rel
        if is_image:
            self.warnings.append("%s: image outside Documentation/ not copied: %s" % (fname, target))
        return self._token('{%% include repo_link.html path="%s" text="%s" %%}'
                           % (liquid_str(html_escape(rel_a)), liquid_str(text_to_html(text))))

    def _rewrite_links(self, line, fname):
        spans = [(m.start(), m.end()) for m in CODE_SPAN_RE.finditer(line)]

        def in_code(pos):
            return any(a <= pos < b for a, b in spans)

        out, pos = [], 0
        for m in LINK_RE.finditer(line):
            if in_code(m.start()):
                continue
            rep = self._rewrite_target(m.group(3), m.group(2), bool(m.group(1)), fname)
            if rep is None:
                continue
            if m.group(4) and not rep.startswith("\x00"):
                rep = rep[:-1] + m.group(4) + ")"
            out.append(line[pos:m.start()])
            out.append(rep)
            pos = m.end()
        out.append(line[pos:])
        return "".join(out)

    def convert(self, text, fname):
        self.liquid = []
        self.marker_count = 0
        lines = text.split("\n")
        out = []
        fence = None
        slugs = {}
        # Section notes: out[] holds a placeholder after each heading; standalone
        # markers anywhere in the section add their ids to it.
        sections = [[]]  # sections[0]: before the first heading
        out.append("\x01S0\x01")

        def add_ids(raw):
            for i in self._ids(raw):
                if i not in sections[-1]:
                    sections[-1].append(i)

        for line in lines:
            fm = FENCE_RE.match(line)
            if fence is not None:
                out.append(line)
                s = line.strip()
                if len(s) >= len(fence) and s == fence[0] * len(s):
                    fence = None
                continue
            if fm:
                fence = fm.group(2)
                out.append(line)
                continue
            m = REFDEF_RE.match(line)
            if m and not line.lstrip().startswith("[^"):
                rep = self._rewrite_target(m.group(2), m.group(2), False, fname)
                if rep is not None and not rep.startswith("\x00"):
                    tgt = re.search(r"\]\(([^)]*)\)", rep).group(1)
                    line = m.group(1) + tgt + m.group(3)
                elif rep is not None:
                    self.warnings.append("%s: reference link leaves Documentation/, kept: %s" % (fname, m.group(2)))
                out.append(line)
                continue
            hm = HEADING_RE.match(line)
            if hm:
                hashes, body = hm.group(1), hm.group(2)
                body = re.sub(r"\s+#+\s*$", "", body)  # closing hashes
                markers = MARKER_RE.findall(body)
                body = MARKER_RE.sub("", body).rstrip()
                base = github_slug(body)
                n = slugs.get(base, 0)
                slug = base if n == 0 else "%s-%d" % (base, n)
                slugs[base] = n + 1
                body = self._rewrite_links(body, fname)
                if re.search(r"\{#[^}]*\}\s*$", body) or not KRAMDOWN_ID_RE.match(slug):
                    # kramdown only accepts {#id} starting with a letter; otherwise its
                    # GFM auto-id (which keeps leading digits, like GitHub) applies.
                    out.append("%s %s" % (hashes, body))
                else:
                    out.append("%s %s {#%s}" % (hashes, body, slug))
                sections.append([])
                out.append("\x01S%d\x01" % (len(sections) - 1))
                for ids in markers:
                    add_ids(ids)
                continue
            if MARKER_RE.search(line) and not MARKER_RE.sub("", line).strip():
                for ids in MARKER_RE.findall(line):
                    add_ids(ids)
                continue
            line = MARKER_RE.sub(lambda mm: self._verify(mm.group(1)), line)
            line = self._rewrite_links(line, fname)
            out.append(line)
        def section_note(mm):
            ids = sections[int(mm.group(1))]
            return "\n%s\n" % self._verify(ids, block=True) if ids else ""

        body = "\n".join(out)
        body = re.sub(r"\n?\x01S(\d+)\x01", lambda mm: ("\n" + section_note(mm)) if sections[int(mm.group(1))] else "", body)
        body = body.lstrip("\n")
        if LIQUID_RE.search(body):
            if "endraw" in body:
                self.warnings.append("%s: contains 'endraw'; Liquid protection may be incomplete" % fname)
            body = "{% raw %}" + body + "{% endraw %}"
            sub = lambda mm: "{% endraw %}" + self.liquid[int(mm.group(1))] + "{% raw %}"
        else:
            sub = lambda mm: self.liquid[int(mm.group(1))]
        return re.sub(r"\x00(\d+)\x00", sub, body)


def yaml_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def title_of(lines, fallback):
    in_fence = False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("# "):
            return plain_heading_text(line[2:])
    return fallback


def build(src, dest, data_file):
    names = sorted(os.path.splitext(f)[0] for f in os.listdir(src)
                   if f.endswith(".md") and os.path.isfile(os.path.join(src, f)))
    conv = Converter(src, REPO, names)
    outputs, entries = {}, {}
    for stem in names:
        fname = stem + ".md"
        with open(os.path.join(src, fname), encoding="utf-8") as fh:
            text = fh.read().replace("\r\n", "\n")
        lines = text.split("\n")
        title = title_of(lines, stem.replace("_", " ").title())
        if stem.lower() == "readme":
            title = "Documentation index"
        desc = first_paragraph(lines)
        body = conv.convert(text, fname)
        fm_lines = [
            "---",
            "layout: guide",
            "title: " + yaml_str(title),
            "description: " + yaml_str(desc),
            "source_file: " + yaml_str(fname),
        ]
        if stem in PAGE_VERIFY:
            fm_lines.append("verify_wp: " + yaml_str(PAGE_VERIFY[stem]))
        if stem in PAGE_VERIFY or conv.marker_count:
            fm_lines.append("has_badges: true")
        fm = "\n".join(fm_lines + [
            "sync_generated: true",
            "---",
            "<!-- Generated by docs/_tools/sync_docs.py from Documentation/%s. "
            "Edit the source and re-run the tool; do not edit this copy. -->" % fname,
            "",
        ])
        slug = slug_for(stem)
        outputs[os.path.join(dest, slug + ".md")] = fm + body.rstrip("\n") + "\n"
        entries[stem] = {"slug": slug, "title": sentence_case(title), "file": fname, "summary": desc}

    # library data
    grouped, used = [], set()
    for gname, items in GROUPS:
        docs = []
        for stem, summary in items:
            if stem in entries:
                e = dict(entries[stem])
                e["summary"] = summary
                docs.append(e)
                used.add(stem)
        if docs:
            grouped.append((gname, docs))
    rest = [entries[s] for s in names if s not in used and s not in LIBRARY_EXCLUDE]
    if rest:
        grouped.append(("More", rest))
    y = ["# Generated by docs/_tools/sync_docs.py. Do not edit; edit GROUPS in the tool."]
    for gname, docs in grouped:
        y.append("- group: " + yaml_str(gname))
        y.append("  docs:")
        for e in docs:
            y.append("    - slug: " + yaml_str(e["slug"]))
            y.append("      title: " + yaml_str(e["title"]))
            y.append("      file: " + yaml_str(e["file"]))
            y.append("      summary: " + yaml_str(e["summary"]))
    outputs[data_file] = "\n".join(y) + "\n"
    return outputs, conv.warnings


def stale_files(dest, outputs):
    stale = []
    if os.path.isdir(dest):
        for f in os.listdir(dest):
            p = os.path.join(dest, f)
            if f.endswith(".md") and p not in outputs:
                with open(p, encoding="utf-8") as fh:
                    head = fh.read(600)
                if "sync_generated: true" in head:
                    stale.append(p)
    return stale


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", default=os.path.join(REPO, "Documentation"), help="source directory (default: Documentation/)")
    ap.add_argument("--dest", default=os.path.join(DOCS, "guide"), help="output directory (default: docs/guide/)")
    ap.add_argument("--data", default=os.path.join(DOCS, "_data", "guide.yml"), help="library data file")
    ap.add_argument("--check", action="store_true", help="report differences and exit 1 instead of writing")
    args = ap.parse_args(argv)

    src, dest, data = (os.path.abspath(p) for p in (args.src, args.dest, args.data))
    if not os.path.isdir(src):
        print("error: source directory not found: %s" % src, file=sys.stderr)
        return 2
    outputs, warnings = build(src, dest, data)
    stale = stale_files(dest, outputs)
    for w in warnings:
        print("warning: " + w, file=sys.stderr)

    changed = []
    for path, content in sorted(outputs.items()):
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == content:
                    continue
        except FileNotFoundError:
            pass
        changed.append(path)

    rel = lambda p: os.path.relpath(p, REPO)
    if args.check:
        for p in changed:
            print("out of date: " + rel(p))
        for p in stale:
            print("stale: " + rel(p))
        return 1 if (changed or stale) else 0

    os.makedirs(dest, exist_ok=True)
    os.makedirs(os.path.dirname(data), exist_ok=True)
    for p in changed:
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(outputs[p])
    for p in stale:
        os.remove(p)
    n_docs = len(outputs) - 1
    print("synced %d documents into %s (%d written, %d removed)" % (n_docs, rel(dest), len(changed), len(stale)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
