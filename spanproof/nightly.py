"""Nightly status page: tonight's run versus the previous nights.

    python -m spanproof.nightly --matrix results/nightly_matrix.json --js results/nightly_js.json \
        --site site --sdk-sha <sha> [--date 2026-10-07]

Reads the previous history from <site>/history.json (if any), maps tonight's findings to
the catalog ids (SP-01 ...), marks what appeared or disappeared since the last night, and
writes <site>/index.html, <site>/history.json and <site>/nights/<date>.json. Writes a
Markdown summary to $GITHUB_STEP_SUMMARY when set, and <site>/new.md when something new
appeared (the workflow turns that into an issue in this repository).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from .catalog import FINDINGS

TITLE = {f["id"]: f["title"] for f in FINDINGS}
TITLE["OLD-SDK"] = "Provider SDK version predates the usage field (not a Sentry fault)"
TITLE["KNOWN-JS-23993"] = "Vercel AI reasoning tokens (known: getsentry/sentry-javascript#23993)"
# Oldest supported provider SDKs that predate the cached/reasoning usage details
OLD_SDK = {"openai-base@1.0.1", "langchain-base@0.1.20"}


def catalog_id(integration: str, scenario: str, rule: str, cell: str = "") -> str | None:
    if cell in OLD_SDK and rule in ("usage.missing.cached", "usage.missing.reasoning", "identity.model_wrong"):
        return "OLD-SDK"
    js = integration.startswith("js.")
    if js:
        if integration == "js.openai" and rule.startswith("usage.missing.") and rule.endswith(("cached", "reasoning")):
            return "SP-11"
        if integration == "js.openai" and rule == "usage.missing.cache_write":
            return "SP-04"
        if integration == "js.anthropic" and rule.startswith(("usage.", "conventions.total")):
            return "SP-12"
        if integration == "js.anthropic" and rule.startswith("lifecycle") and "as_response" in scenario:
            return "SP-17"
        if integration == "js.vercel_ai" and rule == "structure.orphan":
            return "SP-13"
        if integration == "js.vercel_ai" and rule == "usage.missing.reasoning":
            return "KNOWN-JS-23993"
        if rule == "identity.finish_missing":
            return "SP-10"
        if rule.startswith("aggregation.double_count"):
            return "SP-06"
        return None
    if rule.startswith("aggregation.double_count"):
        return "SP-06"
    if rule.startswith("aggregation.rollup"):
        return "SP-07"
    if rule == "identity.finish_missing":
        return "SP-10"
    if rule == "conventions.type":
        return "SP-14"
    if integration == "litellm":
        if rule.startswith("usage.missing."):
            return "SP-01"
        if rule == "lifecycle.lost_span":
            return "SP-03"
        if rule == "identity.model_wrong":
            return "SP-15"
    if integration == "openai":
        if rule == "lifecycle.lost_span":
            return "SP-02"
        if rule == "usage.missing.cache_write":
            return "SP-04"
    if integration == "anthropic" and "raw_response" in scenario and rule.startswith("usage."):
        return "SP-05"
    if integration == "langgraph" and rule.startswith("structure.") and "stream" in scenario:
        return "SP-08"
    if integration == "pydantic_ai" and rule.startswith("structure.") and "iter" in scenario:
        return "SP-09"
    if integration == "pydantic_ai" and rule == "usage.missing.reasoning":
        return "SP-18"
    return None


def collect(matrix: dict | None, js: dict | None) -> dict:
    """{problem key: {"id", "integration", "rule", "cells": [...]}} for medium/high findings."""
    problems: dict = {}
    cells = []
    crashes = []

    def add(res, cell):
        for r in res:
            if r.get("crashed"):
                crashes.append(f"{cell} {r.get('scenario')}")
                continue
            for f in r.get("findings", []):
                if f["severity"] not in ("high", "medium"):
                    continue
                integ = f.get("integration") or r.get("integration")
                cid = catalog_id(integ, r["scenario"], f.get("rule", f["check"]), cell)
                key = cid if cid and cid != "OLD-SDK" else (f"OLD-SDK:{cell}" if cid else f"new:{integ}:{f.get('rule')}")
                p = problems.setdefault(key, {"id": cid, "integration": integ, "rule": f.get("rule"), "cells": set(),
                                              "integrations": set(), "example": f["message"]})
                p["integrations"].add(integ)
                p["cells"].add(cell)

    ran = set()
    for c in (matrix or {}).get("cells", []):
        cell = f"{c['env']}@{c['requested'] if c['requested'] not in ('latest', 'pre') else (c.get('resolved') or c['requested'])}"
        if not c.get("install_error"):
            ran.update(r.get("integration") for r in c.get("results", []) if not r.get("crashed"))
        cells.append({"cell": cell, "requested": c["requested"], "error": bool(c.get("install_error")),
                      "runs": len(c.get("results", []))})
        if c.get("install_error"):
            crashes.append(f"{cell}: install failed")
        add(c.get("results", []), cell)
    if js:
        v = next((r.get("versions", {}) for r in js.get("results", []) if r.get("versions")), {})
        cell = f"@sentry/node@{v.get('@sentry/node', '?')}"
        cells.append({"cell": cell, "requested": "latest", "error": False, "runs": len(js.get("results", []))})
        ran.update(r.get("integration") for r in js.get("results", []) if not r.get("crashed"))
        add(js.get("results", []), cell)
    for p in problems.values():
        p["cells"] = sorted(p["cells"])
        p["integrations"] = sorted(x for x in p["integrations"] if x)
    return {"problems": problems, "cells": cells, "crashes": crashes, "ran": sorted(x for x in ran if x)}


def render(history: list, tonight: dict, new: list, fixed: list) -> str:
    esc = html.escape
    css = """:root{--bg:#f7f8fa;--panel:#fff;--ink:#17202c;--muted:#5d6878;--rule:#dde2ea;--bad:#b42318;--good:#17733f;
--new:#2563a6;--mono:ui-monospace,Menlo,monospace;--body:system-ui,-apple-system,"Segoe UI",sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#10151c;--panel:#171e27;--ink:#e3e8ef;
--muted:#98a3b2;--rule:#2a3442;--bad:#f2877c;--good:#6fcf97;--new:#7fb2ea;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#10151c;--panel:#171e27;--ink:#e3e8ef;--muted:#98a3b2;--rule:#2a3442;--bad:#f2877c;
--good:#6fcf97;--new:#7fb2ea;color-scheme:dark}
body{background:var(--bg);color:var(--ink);font:15px/1.55 var(--body);margin:0}
.w{max-width:1000px;margin:0 auto;padding-inline:18px;padding-block:28px 56px}
h1{font-size:1.7rem;margin:0}h2{font-size:1.15rem;margin-top:2rem;border-top:1px solid var(--rule);padding-top:1rem}
.m{color:var(--muted);font:.82rem var(--mono)}.s{overflow-x:auto;border:1px solid var(--rule);border-radius:6px;
background:var(--panel);margin-top:.6rem}table{border-collapse:collapse;width:100%;font-size:.86rem}
th,td{padding:6px 10px;border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}
th{font:600 .72rem var(--mono);text-transform:uppercase;color:var(--muted)}.bad{color:var(--bad)}.good{color:var(--good)}
.new{color:var(--new);font-weight:600}code{font-family:var(--mono);font-size:.85em}a{color:var(--new)}"""
    last = history[-1]
    h = [f"<title>SpanProof Nightly</title><style>{css}</style><div class='w'>",
         "<h1>SpanProof nightly: Sentry AI monitoring vs. provider truth</h1>",
         f"<p class='m'>last run {esc(last['date'])} · sentry-python {esc(last['sdk_sha'][:8])} · "
         f"{len(last['cells'])} cells · {len(last['problems'])} open findings · "
         "<a href='https://github.com/4ktLuffy/spanproof'>repository</a></p>",
         "<p>Every night this replays recorded provider responses through Sentry's AI integrations on the latest "
         "sentry-python, against the oldest supported, latest and pre-release versions of each provider SDK, and "
         "compares the spans with the truth. A finding that disappears means it was fixed; one that appears means "
         "a release broke something.</p>"]
    if new or fixed:
        h.append("<h2>Since the previous night</h2><ul>")
        h += [f"<li class='new'>new: {esc(k)}</li>" for k in new]
        h += [f"<li class='good'>fixed: {esc(k)} {esc(TITLE.get(k, ''))}</li>" for k in fixed]
        h.append("</ul>")
    h.append("<h2>Open findings tonight</h2><div class='s'><table><tr><th>id</th><th>finding</th><th>where</th></tr>")
    for k, p in sorted(tonight["problems"].items()):
        label = p["id"] or "new"
        if label == "OLD-SDK":
            label = "old SDK"
        title = TITLE.get(p["id"], p["example"]) if p["id"] else p["example"]
        h.append(f"<tr><td class='{'new' if not p['id'] else ''}'>{esc(label)}</td><td>{esc(title)}</td>"
                 f"<td class='m'>{esc(', '.join(p['cells'][:6]))}{' …' if len(p['cells']) > 6 else ''}</td></tr>")
    h.append("</table></div><h2>History</h2><div class='s'><table><tr><th>night</th><th>sentry-python</th>"
             "<th>cells</th><th>open</th><th>new</th><th>fixed</th><th>problems running</th></tr>")
    for n in reversed(history[-60:]):
        h.append(f"<tr><td>{esc(n['date'])}</td><td class='m'>{esc(n['sdk_sha'][:8])}</td><td>{len(n['cells'])}</td>"
                 f"<td>{len(n['problems'])}</td><td class='new'>{len(n.get('new', []))}</td>"
                 f"<td class='good'>{len(n.get('fixed', []))}</td>"
                 f"<td class='{'bad' if n.get('crashes') else ''}'>{len(n.get('crashes', []))}</td></tr>")
    h.append("</table></div><h2>Cells tonight</h2><div class='s'><table><tr><th>integration @ version</th>"
             "<th>runs</th></tr>")
    for c in tonight["cells"]:
        h.append(f"<tr><td class='m'>{esc(c['cell'])}</td><td class='{'bad' if c['error'] else ''}'>"
                 f"{'install failed' if c['error'] else c['runs']}</td></tr>")
    h.append("</table></div></div>")
    return "\n".join(h)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix")
    ap.add_argument("--js")
    ap.add_argument("--site", default="site")
    ap.add_argument("--sdk-sha", default="unknown")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    a = ap.parse_args(argv)
    site = Path(a.site)
    (site / "nights").mkdir(parents=True, exist_ok=True)
    hist_path = site / "history.json"
    history = json.load(open(hist_path)) if hist_path.exists() else []
    tonight = collect(json.load(open(a.matrix)) if a.matrix else None, json.load(open(a.js)) if a.js else None)
    prev = set(history[-1]["problems"]) if history else set()
    prev_where = history[-1].get("where", {}) if history else {}
    now = set(tonight["problems"])
    new = sorted(now - prev) if history else []
    # "fixed" only when tonight actually ran the integrations where it used to show up
    ran = set(tonight["ran"])
    fixed = sorted(k for k in prev - now if prev_where.get(k) and set(prev_where[k]) <= ran)
    entry = {"date": a.date, "sdk_sha": a.sdk_sha, "cells": [c["cell"] for c in tonight["cells"]],
             "problems": sorted(now), "new": new, "fixed": fixed, "crashes": tonight["crashes"],
             "where": {k: p["integrations"] for k, p in tonight["problems"].items()}}
    history = [n for n in history if n["date"] != a.date] + [entry]
    json.dump(history, open(hist_path, "w"), indent=1)
    json.dump({"entry": entry, "problems": tonight["problems"], "cells": tonight["cells"]},
              open(site / "nights" / f"{a.date}.json", "w"), indent=1)
    (site / "index.html").write_text("<!doctype html><meta charset='utf-8'>"
                                     "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                                     + render(history, tonight, new, fixed))
    lines = [f"## SpanProof nightly {a.date}: {len(now)} open, {len(new)} new, {len(fixed)} fixed, "
             f"{len(tonight['crashes'])} problems running"]
    lines += [f"- new: `{k}`" for k in new] + [f"- fixed: `{k}` {TITLE.get(k, '')}" for k in fixed]
    lines += [f"- run problem: {c}" for c in tonight["crashes"][:20]]
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(text + "\n")
    newfile = site / "new.md"
    if new:
        newfile.write_text(text + "\n\nSee the nightly page for details.\n")
    elif newfile.exists():
        newfile.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
