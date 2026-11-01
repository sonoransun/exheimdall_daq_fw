#!/usr/bin/env python3
"""Check internal links and #anchors in a built site (standard library only).

    python3 docs/_tools/check_links.py <built _site dir> [--baseurl /repo]

Reports every internal href or src whose target page or fragment id does not
exist. With --baseurl, a site-relative link without that prefix (one that
skipped relative_url) is reported as "missing baseurl".
External links (http:, https:, mailto:) are not fetched. Exit 1 on any problem.
"""

import argparse
import os
import sys
from html.parser import HTMLParser
from urllib.parse import urlsplit, unquote


class Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.links = set(), []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "a" and a.get("name"):
            self.ids.add(a["name"])
        for key in ("href", "src", "xlink:href"):
            if a.get(key) and tag in ("a", "link", "script", "img", "use", "image"):
                self.links.append(a[key])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("site")
    ap.add_argument("--baseurl", default="")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.site)
    base = args.baseurl.rstrip("/")
    pages = {}
    for d, _, files in os.walk(root):
        for f in files:
            if f.endswith(".html"):
                p = os.path.join(d, f)
                c = Collector()
                with open(p, encoding="utf-8") as fh:
                    c.feed(fh.read())
                pages[p] = c
    problems = 0
    for page, c in sorted(pages.items()):
        for href in c.links:
            u = urlsplit(href)
            if u.scheme or href.startswith("//"):
                continue
            if u.path:
                path = unquote(u.path)
                if path.startswith("/"):
                    if base:
                        if path != base and not path.startswith(base + "/"):
                            print("%s: missing baseurl %s" % (os.path.relpath(page, root), href))
                            problems += 1
                            continue
                        path = path[len(base):]
                    target = os.path.join(root, path.lstrip("/"))
                else:
                    target = os.path.normpath(os.path.join(os.path.dirname(page), path))
                if os.path.isdir(target):
                    target = os.path.join(target, "index.html")
            else:
                target = page
            rel = os.path.relpath(page, root)
            if not os.path.exists(target):
                print("%s: missing target %s" % (rel, href))
                problems += 1
                continue
            if u.fragment and target.endswith(".html"):
                ids = pages[target].ids if target in pages else set()
                if unquote(u.fragment) not in ids:
                    print("%s: missing anchor %s" % (rel, href))
                    problems += 1
    print("checked %d pages: %d problem(s)" % (len(pages), problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
