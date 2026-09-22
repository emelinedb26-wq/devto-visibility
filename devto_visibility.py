#!/usr/bin/env python3
"""What a logged-out reader actually gets when they open a DEV (dev.to) thread.

DEV's public comment API and DEV's public HTML do not agree, and the page says
so in one line that is easy to miss: "Some comments may only be visible to
logged-in visitors". This script measures the gap on any public article, using
two anonymous, public reads:

    1. https://dev.to/api/comments?a_id=<id>   the comment tree
    2. https://dev.to/<user>/<slug>            the HTML, no cookies

Every comment in the tree lands in one of three states, and they do not mean
the same thing:

    RENDERED     the block is in the page and so is the body. A stranger reads it.
    DOWNRANKED   the block is in the page, carries the CSS class
                 `low-quality-comment`, and where the body should be the page
                 prints "Comment deleted". The comment is not deleted.
    MISSING      no block at all. Usually the page truncating a long thread.

Telling MISSING from DOWNRANKED is the point. MISSING is mostly thread
truncation and says nothing about your text. DOWNRANKED is a judgement on your
text, shown to strangers under your name and avatar.

Usage
-----
    python3 devto_visibility.py https://dev.to/user/some-article-slug
    python3 devto_visibility.py 4702045 4640240
    python3 devto_visibility.py <url> --user someone
    python3 devto_visibility.py <url> --csv out.csv

Standard library only. No key, no account, nothing read that is not already
served to anyone who opens the page.

MIT licensed. See LICENSE.
"""

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140 Safari/537.36")
API = "https://dev.to/api"

# DEV starts refusing somewhere around three requests a second. Stay under it.
PAUSE = 0.4

# A comment block in the served page. The CSS class and the id_code are taken
# in the same match on purpose. Searching for the id_code on its own is what
# makes you believe a comment is rendered: DEV writes the id_code into a
# data-path and an anchor even for a comment whose body it has replaced.
BLOCK = re.compile(
    r'id="comment-node-(?P<node>\d+)"\s*class="(?P<css>[^"]*)"'
    r'\s*data-comment-id="\d+"\s*data-path="[^"]*/comments/(?P<code>[a-z0-9]+)"',
    re.S)

DEAD_BODY = "Comment deleted"
LOGGED_IN_NOTICE = "only be visible to logged-in"


def fetch(url, as_json=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read()
            time.sleep(PAUSE)
            return json.loads(raw) if as_json else raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise SystemExit("HTTP %s on %s" % (e.code, url))
    raise SystemExit("unreachable: %s" % url)


def resolve(target):
    """An article id or a dev.to URL in, (id, canonical url) out."""
    target = target.strip()
    if target.isdigit():
        art = fetch("%s/articles/%s" % (API, target), as_json=True)
        return int(target), art["url"]
    m = re.match(r"https?://(?:www\.)?dev\.to/([^/]+)/([^/?#]+)", target)
    if not m:
        raise SystemExit("not a dev.to article URL or numeric id: %s" % target)
    art = fetch("%s/articles/%s/%s" % (API, m.group(1), m.group(2)), as_json=True)
    return art["id"], art["url"]


def walk(node, depth=0):
    yield depth, node
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def blocks_in(page):
    """id_code -> two independent readings of the same fact.

    The CSS class and the replaced body are derived separately so that a
    disagreement between them shows up instead of being averaged away.
    """
    out = {}
    found = list(BLOCK.finditer(page))
    for i, m in enumerate(found):
        start = m.end()
        end = found[i + 1].start() if i + 1 < len(found) else len(page)
        segment = page[start:end]
        out[m.group("code")] = {
            "low_quality_class": "low-quality-comment" in m.group("css"),
            "body_replaced": DEAD_BODY in segment,
        }
    return out


LINK = re.compile(r'href="(https?://[^"]+)"', re.I)


def classify_links(body_html):
    urls = LINK.findall(body_html or "")
    if not urls:
        return "none", 0
    if len(urls) > 1:
        return "multiple", len(urls)
    host = urls[0].split("/")[2].lower()
    return ("internal" if host.endswith("dev.to") else "external"), 1


def age_hours(iso, now):
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((now - t).total_seconds() / 3600.0, 2)


def measure(target, only_user=None):
    art_id, url = resolve(target)
    tree = fetch("%s/comments?a_id=%s" % (API, art_id), as_json=True)
    page = fetch(url)
    blocks = blocks_in(page)
    now = datetime.now(timezone.utc)

    rows, disagreements = [], []
    for top in tree:
        for depth, c in walk(top):
            author = (c.get("user") or {}).get("username", "")
            if only_user and author != only_user:
                continue
            code = c["id_code"]
            b = blocks.get(code)
            if b is None:
                state = "MISSING"
            elif b["low_quality_class"] or b["body_replaced"]:
                state = "DOWNRANKED"
                if b["low_quality_class"] != b["body_replaced"]:
                    disagreements.append(code)
            else:
                state = "RENDERED"
            kind, count = classify_links(c.get("body_html", ""))
            rows.append({
                "article_id": art_id,
                "id_code": code,
                "author": author,
                "depth": depth,
                "age_hours": age_hours(c.get("created_at", ""), now),
                "links": kind,
                "link_count": count,
                "state": state,
            })
    return {
        "article_id": art_id,
        "url": url,
        "in_api": len(rows),
        "blocks_in_page": len(blocks),
        # A page that truncates tells you nothing about the comments it left
        # out. Compare link classes only where this is False.
        "page_truncates": len(blocks) < len(rows) and only_user is None,
        "page_carries_logged_in_notice": LOGGED_IN_NOTICE in page,
        "class_vs_body_disagreements": disagreements,
        "rows": rows,
    }


def summarise(rows, key="links"):
    out = {}
    for r in rows:
        d = out.setdefault(r[key], {"comments": 0, "RENDERED": 0,
                                    "DOWNRANKED": 0, "MISSING": 0})
        d["comments"] += 1
        d[r["state"]] += 1
    for d in out.values():
        d["rendered_rate"] = round(d["RENDERED"] / d["comments"], 3)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("targets", nargs="+", help="dev.to article URL(s) or numeric id(s)")
    p.add_argument("--user", help="only report comments by this username")
    p.add_argument("--csv", help="write every row to this CSV file")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args()

    reports = [measure(t, a.user) for t in a.targets]
    rows = [r for rep in reports for r in rep["rows"]]

    if a.json:
        print(json.dumps({"articles": reports}, ensure_ascii=False))
        return 0

    for rep in reports:
        n = {s: sum(1 for r in rep["rows"] if r["state"] == s)
             for s in ("RENDERED", "DOWNRANKED", "MISSING")}
        print("%s\n  %d in the API, %d blocks in the page: %d rendered, "
              "%d downranked, %d missing%s"
              % (rep["url"], rep["in_api"], rep["blocks_in_page"],
                 n["RENDERED"], n["DOWNRANKED"], n["MISSING"],
                 "   [thread is truncated, link rates here mean nothing]"
                 if rep["page_truncates"] else ""))
        for r in rep["rows"]:
            if r["state"] == "DOWNRANKED":
                print('    DOWNRANKED %-8s by %-24s depth %d  %sh old  links=%s'
                      '  -> page prints "Comment deleted"'
                      % (r["id_code"], r["author"], r["depth"],
                         r["age_hours"], r["links"]))

    clean = [r for rep in reports if not rep["page_truncates"] for r in rep["rows"]]
    print("\n%d comments over %d articles, of which %d on articles the page "
          "serves whole" % (len(rows), len(reports), len(clean)))
    if clean:
        print("\nby link class, untruncated articles only:")
        for k, d in sorted(summarise(clean).items()):
            print("  %-9s n=%-4d rendered %-4d downranked %-3d missing %-4d "
                  "rate %.3f" % (k, d["comments"], d["RENDERED"],
                                 d["DOWNRANKED"], d["MISSING"],
                                 d["rendered_rate"]))

    odd = [c for rep in reports for c in rep["class_vs_body_disagreements"]]
    if odd:
        print("\nCSS class and replaced body disagree on: %s" % odd)

    if a.csv:
        cols = ["article_id", "id_code", "author", "depth", "age_hours",
                "links", "link_count", "state"]
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in cols})
        print("\nwrote %d rows to %s" % (len(rows), a.csv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
