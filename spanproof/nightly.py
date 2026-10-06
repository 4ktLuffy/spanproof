"""Nightly status page: tonight's run versus the previous nights.

    python -m spanproof.nightly --matrix results/nightly_matrix.json --js results/nightly_js.json \
        --site site --sdk-sha <sha> [--date 2026-10-07]

Compares tonight with the previous night finding by finding, keyed by the same signature
as the CI gate (cell|scenario|mode|rule|attribute|call; the cell is env@requested, so a
provider release does not rename it); the catalog id (SP-01 ...) is only a label. A finding
is "fixed" only when its own cell, scenario and mode ran cleanly tonight; if they crashed,
failed to install, were unsupported or were missing, it is "did not run" and is carried
forward until they run again. Each night records its provenance (sentry-python SHA, this
runner's SHA, the resolved provider versions per cell and the JS package versions), and
each verdict lists what changed between the two nights. Writes <site>/index.html,
<site>/history.json and <site>/nights/<date>.json (signatures and coverage), a Markdown
summary to $GITHUB_STEP_SUMMARY when set, and <site>/new.md when something new appeared
(the workflow turns that into an issue in this repository). Nights recorded before
signatures were kept give no finding-level baseline; the page says so.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .catalog import FINDINGS
from .gate import signature

TITLE = {f["id"]: f["title"] for f in FINDINGS}
TITLE["OLD-SDK"] = "Provider SDK version predates the usage field (not a Sentry fault)"
TITLE["KNOWN-JS-23993"] = "Vercel AI reasoning tokens (known: getsentry/sentry-javascript#23993)"
TITLE["UPSTREAM-LANGCHAIN"] = "LangChain JS's own usage numbers differ from the provider's (reproduces without Sentry)"
# Oldest supported provider SDKs that predate the cached/reasoning usage details
OLD_SDK = {"openai-base@1.0.1", "langchain-base@0.1.20"}
LIST_MAX = 100  # findings listed one by one in the summary and the GitHub issue
ISSUE_MAX = 60000
ROOT = Path(__file__).resolve().parent.parent


def _explored(i: str, sc: str, rule: str) -> str | None:
    """Findings from the 2026-10-06 exploration (SP-19 to SP-44), by integration, scenario and rule."""
    usage = rule.startswith(("usage.", "conventions.total", "conventions.cached_exceeds"))
    if i in ("huggingface_hub", "cohere") and rule == "errors.swallowed":
        return "SP-19"
    if i == "huggingface_hub":
        if rule == "lifecycle.lost_span":
            return "SP-35" if ".async" in sc else "SP-20"
        if "text_generation" in sc and usage:
            return "SP-21"
        if "tool_call" in sc and rule.startswith(("identity.tool_call", "output.tool_calls")):
            return "SP-28"
    if i == "cohere":
        if rule == "lifecycle.lost_span":
            return "SP-35"
        if rule.startswith("conventions.legacy"):
            return "SP-36"
    if i == "mistral":
        if rule == "lifecycle.lost_span":
            return "SP-22"
        if rule.startswith(("output.tool_calls", "identity.tool_call", "identity.model", "identity.response_id")):
            return "SP-29"
    if rule.startswith("billing.unrecorded"):
        return "SP-31"
    if i in ("openai", "anthropic") and rule == "lifecycle.lost_span" and ".parse" in sc:
        return "SP-25"
    if i == "anthropic" and rule == "lifecycle.lost_span" and ".beta." in sc:
        return "SP-35"
    if i == "openai":
        if "incomplete" in sc and usage:
            return "SP-23"
        if "background" in sc and usage:
            return "SP-24"
        if "n2" in sc and rule.startswith("output."):
            return "SP-26"
    if i == "anthropic":
        if rule.startswith("output.") and ".stream" in sc:
            return "SP-27"
        if "thinking" in sc and rule == "usage.missing.reasoning":
            return "SP-30"
    if i == "js.langgraph":
        if "create_agent" in sc and rule.startswith("structure."):
            return "SP-37"
        if "thread" in sc and rule.startswith("aggregation.rollup"):
            return "SP-38"
        if "without_langchain" in sc and rule.startswith(("lifecycle.duplicate", "aggregation.filtered",
                                                          "aggregation.rollup")):
            return "SP-43"
        if "anthropic" in sc and (usage or rule.startswith("aggregation.")) and not rule.startswith("aggregation.double"):
            return "SP-40"
    if i == "js.google_genai" and usage:
        return "SP-39"
    if i == "js.langchain":
        if rule == "lifecycle.lost_span":
            return "SP-44"
        if "chat_google" in sc and rule in ("usage.wrong.output_tokens", "conventions.total_mismatch"):
            return "UPSTREAM-LANGCHAIN"
        if "chat_anthropic.stream" in sc and rule in ("usage.wrong.output_tokens", "usage.wrong.total"):
            return "UPSTREAM-LANGCHAIN"
        if "anthropic" in sc and usage:
            return "SP-40"
    if i == "cohere" and rule == "usage.missing.cached":
        return "SP-45"
    if i == "cohere" and rule == "output.tool_calls_missing":
        return "SP-48"
    if (i == "js.langgraph" and rule == "conventions.type") or (i == "js.langchain" and rule == "identity.response_id_wrong"):
        return "SP-46"
    if i == "js.langgraph" and ".stream" in sc and rule.startswith("structure."):
        return "SP-47"
    if i in ("js.langchain", "js.langgraph"):
        if rule == "privacy.content_leak":
            return "SP-42"
        if rule.startswith("usage.missing."):
            return "SP-41"
    return None


def catalog_id(integration: str, scenario: str, rule: str, cell: str = "") -> str | None:
    if cell in OLD_SDK and rule in ("usage.missing.cached", "usage.missing.reasoning", "identity.model_wrong"):
        return "OLD-SDK"
    if cell == "anthropic@0.16.0" and "stop_reason." in scenario and rule.startswith("usage."):
        return "OLD-SDK"  # anthropic 0.16.0 types a newer stop_reason delta as message_start (not a Sentry fault)
    js = integration.startswith("js.")
    sp = _explored(integration, scenario, rule)
    if sp:
        return sp
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


def scope(sig: str) -> str:
    return "|".join(sig.split("|")[:3])  # cell|scenario|mode


def collect(matrix: dict | None, js: dict | None) -> dict:
    """Tonight's findings by signature, the cell|scenario|mode runs that completed, a per-id rollup,
    the cells, and the package versions each cell resolved."""
    sigs, problems, cells, crashes, packages, ran = {}, {}, [], [], {}, set()

    def add(res, key, label):
        for r in res:
            mode = r.get("mode", "default")
            if r.get("crashed"):
                crashes.append(f"{label} {r.get('scenario')} [{mode}]")
                continue
            if r.get("unsupported"):  # the library lacks the API: nothing was checked
                continue
            ran.add(f"{key}|{r['scenario']}|{mode}")
            for f in r.get("findings", []):
                if f["severity"] not in ("high", "medium"):
                    continue
                f = dict(f, scenario=r["scenario"], mode=f.get("mode") or mode)
                integ = f.get("integration") or r.get("integration")
                rule = f.get("rule") or f["check"]
                cid = catalog_id(integ, r["scenario"], rule, key)
                pk = cid if cid and cid != "OLD-SDK" else (f"OLD-SDK:{key}" if cid else f"new:{integ}:{rule}")
                sigs[signature(f, key)] = {"id": cid, "key": pk, "message": f["message"]}
                p = problems.setdefault(pk, {"id": cid, "integration": integ, "rule": rule, "cells": set(),
                                             "integrations": set(), "example": f["message"], "signatures": 0})
                p["integrations"].add(integ)
                p["cells"].add(label)
                p["signatures"] += 1

    for c in (matrix or {}).get("cells", []):
        key = f"{c['env']}@{c['requested']}"
        label = key if c["requested"] not in ("latest", "pre") else f"{key} ({c.get('resolved') or '?'})"
        res = c.get("results", [])
        packages[key] = next((r["versions"] for r in res if r.get("versions")), {})
        cells.append({"cell": label, "key": key, "requested": c["requested"], "error": bool(c.get("install_error")),
                      "runs": len(res), "crashed": sum(1 for r in res if r.get("crashed")), "versions": packages[key]})
        if c.get("install_error"):
            crashes.append(f"{label}: install failed")
        add(res, key, label)
    if js:
        res = js.get("results", [])
        r0 = next((r for r in res if r.get("versions")), {})
        packages["js"] = dict(r0.get("versions", {}), **({"node": r0["node"]} if r0.get("node") else {}))
        label = f"js (@sentry/node {packages['js'].get('@sentry/node', '?')})"
        cells.append({"cell": label, "key": "js", "requested": "latest", "error": False, "runs": len(res),
                      "crashed": sum(1 for r in res if r.get("crashed")), "versions": packages["js"]})
        add(res, "js", label)
    for p in problems.values():
        p["cells"] = sorted(p["cells"])
        p["integrations"] = sorted(x for x in p["integrations"] if x)
    return {"signatures": sigs, "problems": problems, "cells": cells, "crashes": crashes, "ran": sorted(ran),
            "packages": packages}


def provenance_diff(old: dict, new: dict) -> tuple[list[str], dict]:
    """What differs between two nights: (global changes, {cell: changes})."""
    def short(v):
        return v[:8] if v else "not recorded"
    glob = [f"{name} {short(old.get(k))} -> {short(new.get(k))}"
            for k, name in (("sdk_sha", "sentry-python"), ("runner_sha", "spanproof runner"))
            if old.get(k) != new.get(k)]
    cells = {}
    if old.get("packages") is None:
        glob.append("package versions were not recorded on the previous night")
        return glob, cells
    for cell in sorted(set(old["packages"]) | set(new.get("packages", {}))):
        a, b = old["packages"].get(cell, {}), new.get("packages", {}).get(cell, {})
        d = [f"{k} {a.get(k, 'absent')} -> {b.get(k, 'absent')}"
             for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
        if d:
            cells[cell] = d
    return glob, cells


def compare(prev: dict | None, base: dict | None, tonight: dict) -> dict:
    """new / fixed / did-not-run per signature against the previous night (and what it carried forward)."""
    now = tonight["signatures"]
    v = {"new": [], "fixed": [], "unknown": [], "before": {}, "carried": {}, "unchecked_before": [], "note": None}
    if not prev:
        v["note"] = "First recorded night: nothing to compare with."
        return v
    if base is None:
        # nights recorded before signatures were kept: only a catalog id absent that night can be called new
        old = set(prev.get("problems", []))
        v["new"] = sorted(k for k, s in now.items() if s["key"] not in old)
        v["note"] = (f"The previous night ({prev['date']}) was recorded before per-finding signatures were kept, so "
                     "there is no finding-level baseline tonight: nothing is reported fixed or did-not-run, and "
                     "\"new\" only lists findings whose catalog id was absent that night. Comparisons are per "
                     "finding from the next night on.")
        return v
    before = {k: dict(s, last_seen=s.get("last_seen", prev["date"]))
              for k, s in {**base.get("carried", {}), **base["signatures"]}.items()}
    ran, prev_ran = set(tonight["ran"]), set(base.get("ran", []))
    v["before"] = before
    v["new"] = sorted(k for k in now if k not in before)
    v["unchecked_before"] = [k for k in v["new"] if scope(k) not in prev_ran]
    gone = [k for k in before if k not in now]
    v["fixed"] = sorted(k for k in gone if scope(k) in ran)
    v["unknown"] = sorted(k for k in gone if scope(k) not in ran)
    v["carried"] = {k: before[k] for k in v["unknown"]}
    return v


def render(history: list, tonight: dict, v: dict, prov: dict, prev: dict | None) -> str:
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
h3{font-size:1rem;margin-top:1.4rem}
.m{color:var(--muted);font:.82rem var(--mono)}.s{overflow-x:auto;border:1px solid var(--rule);border-radius:6px;
background:var(--panel);margin-top:.6rem}table{border-collapse:collapse;width:100%;font-size:.86rem}
th,td{padding:6px 10px;border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}
th{font:600 .72rem var(--mono);text-transform:uppercase;color:var(--muted)}.bad{color:var(--bad)}.good{color:var(--good)}
.new{color:var(--new);font-weight:600}code{font-family:var(--mono);font-size:.85em}a{color:var(--new)}"""
    last = history[-1]
    js = tonight["packages"].get("js", {})
    h = [f"<title>SpanProof Nightly</title><style>{css}</style><div class='w'>",
         "<h1>SpanProof nightly: Sentry AI monitoring vs. provider truth</h1>",
         f"<p class='m'>last run {esc(last['date'])} · sentry-python {esc(prov['sdk_sha'][:8])} · "
         f"runner {esc((prov.get('runner_sha') or 'unknown')[:8])} · "
         f"@sentry/node {esc(js.get('@sentry/node', '-'))} · {len(last['cells'])} cells · "
         f"{len(tonight['signatures'])} open findings under {len(last['problems'])} ids · "
         "<a href='https://github.com/4ktLuffy/spanproof'>repository</a></p>",
         "<p>Every night this replays scripted provider responses through Sentry's AI integrations on the latest "
         "sentry-python, against the oldest supported, latest and pre-release versions of each provider SDK, and "
         "compares the spans with the truth. Nights are compared finding by finding (cell, scenario, mode, rule, "
         "attribute, call); the catalog id is a label. A finding counts as fixed only if its own cell, scenario and "
         "mode ran cleanly tonight and no longer produce it. If they did not run (crash, failed install, "
         "unsupported, missing), the finding is listed as did not run and kept open.</p>"]
    if prev:
        h.append(f"<h2>Since the previous night ({esc(prev['date'])})</h2>")
        if not v["note"]:
            h.append(f"<p><b>{len(v['new'])} new, {len(v['fixed'])} fixed, {len(v['unknown'])} did not run.</b></p>")
    if v["note"]:
        h.append(f"<p class='m'>{esc(v['note'])}</p>")
    glob, per_cell = v.get("changes", ([], {}))
    if prev:
        items = [esc(c) for c in glob] + [f"<code>{esc(c)}</code>: {esc('; '.join(d))}" for c, d in per_cell.items()]
        h.append("<h3>What changed between the two nights</h3>" +
                 ("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>" if items else
                  "<p>No recorded change in sentry-python, the runner or any resolved package version.</p>") +
                 "<p class='m'>Listed next to each verdict below. A change that happens on the same night as a "
                 "verdict co-occurs with it; that is not proof that it caused it.</p>")

    def changed(sig):
        cell = sig.split("|")[0]
        return "; ".join(glob + [f"{cell}: {d}" for d in per_cell.get(cell, [])]) or "nothing recorded"

    def table(title, keys, info, cls, extra=None):
        if not keys:
            return
        h.append(f"<h3 class='{cls}'>{esc(title)} ({len(keys)})</h3><div class='s'><table><tr><th>id</th><th>cell</th>"
                 "<th>scenario [mode]</th><th>rule · attribute · call</th><th>changed since previous night</th></tr>")
        for k in keys[:300]:
            cell, sc, mode, rule, attr, call = (k.split("|") + [""] * 6)[:6]
            s = info.get(k, {})
            note = f"<br><span class='m'>{esc(extra(k))}</span>" if extra and extra(k) else ""
            h.append(f"<tr><td class='{cls}'>{esc(s.get('id') or 'uncatalogued')}</td><td class='m'>{esc(cell)}</td>"
                     f"<td class='m'>{esc(sc)} [{esc(mode)}]</td>"
                     f"<td class='m'>{esc(rule)} · {esc(attr)} · {esc(call)}"
                     f"<br>{esc(s.get('message', ''))}{note}</td><td class='m'>{esc(changed(k))}</td></tr>")
        if len(keys) > 300:
            h.append(f"<tr><td colspan='5'>… and {len(keys) - 300} more "
                     f"(see nights/{esc(last['date'])}.json)</td></tr>")
        h.append("</table></div>")

    ub = set(v["unchecked_before"])
    table("New", v["new"], tonight["signatures"], "new",
          lambda k: f"first seen; its scenario did not run on {prev['date']}" if k in ub else "")
    table("Fixed (scenario ran tonight, finding gone)", v["fixed"], v["before"], "good")
    if v["unknown"]:
        groups: dict = defaultdict(list)
        for k in v["unknown"]:
            groups[scope(k)].append(k)
        h.append(f"<h3 class='bad'>Did not run tonight: unknown, not fixed ({len(v['unknown'])})</h3>"
                 "<div class='s'><table><tr><th>cell|scenario|mode</th><th>open findings</th><th>ids</th>"
                 "<th>last seen</th></tr>")
        for sc, ks in sorted(groups.items()):
            ids = sorted({v["before"][k].get("id") or "uncatalogued" for k in ks})
            h.append(f"<tr><td class='m'>{esc(sc)}</td><td>{len(ks)}</td><td>{esc(', '.join(ids))}</td>"
                     f"<td class='m'>{esc(min(v['before'][k]['last_seen'] for k in ks))}</td></tr>")
        h.append("</table></div>")
    newids = defaultdict(int)
    for k in v["new"]:
        newids[tonight["signatures"][k]["key"]] += 1
    h.append("<h2>Open findings tonight</h2><div class='s'><table><tr><th>id</th><th>finding</th><th>findings</th>"
             "<th>where</th></tr>")
    for k, p in sorted(tonight["problems"].items()):
        label = p["id"] or "uncatalogued"
        if label == "OLD-SDK":
            label = "old SDK"
        title = TITLE.get(p["id"], p["example"]) if p["id"] else p["example"]
        n = f"{p['signatures']}" + (f" <span class='new'>({newids[k]} new)</span>" if newids.get(k) else "")
        h.append(f"<tr><td class='{'new' if not p['id'] else ''}'>{esc(label)}</td><td>{esc(title)}</td><td>{n}</td>"
                 f"<td class='m'>{esc(', '.join(p['cells'][:6]))}{' …' if len(p['cells']) > 6 else ''}</td></tr>")
    h.append("</table></div><h2>History</h2><div class='s'><table><tr><th>night</th><th>sentry-python</th>"
             "<th>runner</th><th>cells</th><th>open ids</th><th>new</th><th>fixed</th><th>did not run</th>"
             "<th>problems running</th></tr>")
    for n in reversed(history[-60:]):
        rs = (n.get("provenance") or {}).get("runner_sha") or "-"
        h.append(f"<tr><td>{esc(n['date'])}</td><td class='m'>{esc(n['sdk_sha'][:8])}</td>"
                 f"<td class='m'>{esc(rs[:8])}</td>"
                 f"<td>{len(n['cells'])}</td><td>{len(n['problems'])}</td><td class='new'>{len(n.get('new', []))}</td>"
                 f"<td class='good'>{len(n.get('fixed', []))}</td><td>{len(n.get('unknown', []))}</td>"
                 f"<td class='{'bad' if n.get('crashes') else ''}'>{len(n.get('crashes', []))}</td></tr>")
    h.append("</table></div><p class='m'>Nights before per-finding signatures counted new and fixed by catalog id.</p>"
             "<h2>Cells tonight</h2><div class='s'><table><tr><th>cell (resolved)</th><th>package versions</th>"
             "<th>runs</th></tr>")
    for c in tonight["cells"]:
        vers = ", ".join(f"{k} {x}" for k, x in sorted(c.get("versions", {}).items()))
        state = ("install failed" if c["error"]
                 else f"{c['runs']}" + (f", {c['crashed']} crashed" if c["crashed"] else ""))
        h.append(f"<tr><td class='m'>{esc(c['cell'])}</td><td class='m'>{esc(vers)}</td>"
                 f"<td class='{'bad' if c['error'] or c['crashed'] else ''}'>{esc(state)}</td></tr>")
    h.append("</table></div></div>")
    return "\n".join(h)


def runner_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix")
    ap.add_argument("--js")
    ap.add_argument("--site", default="site")
    ap.add_argument("--sdk-sha", default="unknown")
    ap.add_argument("--runner-sha", help="git SHA of this runner (default: git rev-parse HEAD of the checkout)")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    a = ap.parse_args(argv)
    site = Path(a.site)
    (site / "nights").mkdir(parents=True, exist_ok=True)
    hist_path = site / "history.json"
    history = [n for n in (json.load(open(hist_path)) if hist_path.exists() else []) if n["date"] != a.date]
    tonight = collect(json.load(open(a.matrix)) if a.matrix else None, json.load(open(a.js)) if a.js else None)
    prov = {"sdk_sha": a.sdk_sha, "runner_sha": a.runner_sha or runner_sha(), "packages": tonight["packages"]}
    prev = max((n for n in history if n["date"] < a.date), key=lambda n: n["date"], default=None)
    base = None
    if prev:
        pf = site / "nights" / f"{prev['date']}.json"
        detail = json.load(open(pf)) if pf.exists() else {}
        base = detail if "signatures" in detail else None
    v = compare(prev, base, tonight)
    if prev:
        v["changes"] = provenance_diff(prev.get("provenance") or {"sdk_sha": prev.get("sdk_sha")}, prov)
    now = tonight["signatures"]
    entry = {"date": a.date, "format": 2, "sdk_sha": a.sdk_sha, "provenance": prov,
             "cells": [c["cell"] for c in tonight["cells"]], "problems": sorted(tonight["problems"]),
             "new": v["new"], "fixed": v["fixed"], "unknown": v["unknown"], "crashes": tonight["crashes"],
             "baseline": prev["date"] if base else None}
    history = sorted(history + [entry], key=lambda n: n["date"])
    json.dump(history, open(hist_path, "w"), indent=1)
    json.dump({"entry": entry, "problems": tonight["problems"], "cells": tonight["cells"], "signatures": now,
               "ran": tonight["ran"], "carried": v["carried"]},
              open(site / "nights" / f"{a.date}.json", "w"), indent=1)
    (site / "index.html").write_text("<!doctype html><meta charset='utf-8'>"
                                     "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                                     + render(history, tonight, v, prov, prev))
    glob, per_cell = v.get("changes", ([], {}))
    lines = [f"## SpanProof nightly {a.date}: {len(now)} open findings, {len(v['new'])} new, {len(v['fixed'])} fixed, "
             f"{len(v['unknown'])} did not run, {len(tonight['crashes'])} problems running"]
    if v["note"]:
        lines.append(f"\n{v['note']}")
    if glob or per_cell:
        lines.append("\nChanged since the previous night (co-occurrence, not proof of cause):")
        lines += [f"- {c}" for c in glob] + [f"- `{c}`: {'; '.join(d)}" for c, d in per_cell.items()]
    if v["new"]:
        by_id = Counter(now[k]["id"] or "uncatalogued" for k in v["new"])
        lines.append("\nNew by catalog id: " + ", ".join(f"{i} {n}" for i, n in sorted(by_id.items())))
    lines += [f"- new: [{now[k]['id'] or 'uncatalogued'}] `{k}` {now[k]['message']}" for k in v["new"][:LIST_MAX]]
    if len(v["new"]) > LIST_MAX:
        lines.append(f"- ... and {len(v['new']) - LIST_MAX} more new findings (listed on the nightly page)")
    lines += [f"- fixed: [{v['before'][k].get('id') or 'uncatalogued'}] `{k}`" for k in v["fixed"][:LIST_MAX]]
    if len(v["fixed"]) > LIST_MAX:
        lines.append(f"- ... and {len(v['fixed']) - LIST_MAX} more fixed findings (listed on the nightly page)")
    lines += [f"- did not run (not fixed): `{k}`" for k in v["unknown"][:50]]
    lines += [f"- run problem: {c}" for c in tonight["crashes"][:20]]
    text = "\n".join(lines)
    if len(text) > ISSUE_MAX:  # a GitHub issue body holds 65,536 characters
        text = text[:ISSUE_MAX].rsplit("\n", 1)[0] + "\n- ... (cut to fit a GitHub issue; the nightly page has all)"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(text + "\n")
    newfile = site / "new.md"
    if v["new"]:
        newfile.write_text(text + "\n\nSee the nightly page for details.\n")
    elif newfile.exists():
        newfile.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
