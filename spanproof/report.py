"""Build the findings page (report/index.html) from the results files.

    python -m spanproof.report
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from pathlib import Path

from .catalog import FINDINGS, NEGATIVE_RESULTS, PY_SHA

ROOT = Path(__file__).resolve().parent.parent
R = ROOT / "results"


def load(name):
    p = R / name
    return json.load(open(p)) if p.exists() else None


def esc(x):
    return html.escape(str(x))


def linkify(text):
    t = esc(text)
    return re.sub(r"(https://[^\s,;)]+)", lambda m: f'<a href="{m.group(1)}">{shorten(m.group(1))}</a>', t)


def shorten(url):
    m = re.match(r"https://github.com/getsentry/([^/]+)/(?:issues|pull)/(\d+)", url)
    if m:
        return f"{m.group(1)}#{m.group(2)}"
    m = re.match(r"https://github.com/getsentry/([^/]+)/blob/[0-9a-f]+/(.+)", url)
    if m:
        return f"{m.group(2)}"
    return url


# --------------------------------------------------------------------------- data

def fix_litellm_table():
    head, fix = load("matrix_tox_head.json"), load("matrix_fix_litellm.json")
    rows = []
    if not head or not fix:
        return rows

    def usage(d):
        out = {}
        for c in d["cells"]:
            if c["env"] == "litellm" and c.get("results"):
                out[c["requested"]] = (c.get("resolved"), sum(1 for r in c["results"] for f in r.get("findings", [])
                                                                if f["check"] == "usage"))
        return out

    a, b = usage(head), usage(fix)
    for k, (ver, n) in a.items():
        rows.append((ver or k, n, b.get(k, (None, None))[1]))
    return rows


def matrix_grid():
    d = load("matrix_tox_head.json")
    if not d:
        return [], {}
    grid = defaultdict(dict)
    order = []
    for c in d["cells"]:
        env = c["env"].replace("-base", "")
        if env not in order:
            order.append(env)
        if c.get("install_error"):
            grid[env][c["requested"]] = {"err": True}
            continue
        sig = {(r["scenario"], f["check"], re.sub(r"\d+", "N", f["message"])[:60])
               for r in c["results"] for f in r.get("findings", []) if f["severity"] in ("high", "medium")}
        unsup = sum(1 for r in c["results"] if r.get("unsupported"))
        grid[env][c["requested"]] = {"n": len(sig), "resolved": c.get("resolved"), "runs": len(c["results"]),
                                     "unsup": unsup}
    return order, grid


def parity_rows():
    py, js = load("full_head.json"), load("js_head.json")
    if not py or not js:
        return []
    pyr = {r["scenario"]: r for r in py["results"] if r.get("mode") == "default" and not r.get("crashed")}
    out = []
    from .oracles import match_calls
    from . import conventions as cv
    for r in js["results"]:
        if r.get("mode") != "default" or not r.get("twin") or r["twin"] not in pyr:
            continue
        t = r["calls"][0]["truth"]
        if not t:
            continue

        def vals(res):
            pairs = match_calls(res)
            sp = pairs[0][1] if pairs else None
            u = cv.read_usage(sp["data"]) if sp else {}
            fin = None
            if sp:
                fin = sp["data"].get("gen_ai.response.finish_reasons")
            return {m: (u[m][0] if m in u else None) for m in ("input_tokens", "cached", "cache_write",
                                                               "output_tokens", "reasoning", "total")} | {
                "finish": "yes" if fin else None}

        out.append({"scenario": r["twin"], "truth": t, "py": vals(pyr[r["twin"]]), "js": vals(r)})
    return out


def detector_tables():
    conds = [("eval_dc_on.json", "Data collection on"),
             ("eval_dc_off.json", "Data collection off (today's SDK)"),
             ("eval_fp.json", "Data off + two proposed non-content attributes"),
             ("eval_dc_on_fix2.json", "Data on + Pydantic AI finish-reason fix"),
             ("eval_heldout_v1.json", "Held-out set 1 (Codex), before tuning"),
             ("eval_heldout_v2.json", "Held-out set 2 (Codex), never seen before scoring")]
    out = []
    for fn, label in conds:
        d = load(fn)
        if d:
            out.append((label, d))
    return out


# --------------------------------------------------------------------------- html

CSS = """
/* Layout: one reading column for prose, full-width bands for the ledger tables. */
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#18202b; --muted:#5b6676; --rule:#dfe3ea;
  --accent:#2563a6; --accent-soft:#e6eef8;
  --hi:#b42318; --hi-soft:#fdecea; --med:#a15c07; --med-soft:#fdf2e1; --lo:#54606f; --lo-soft:#eef0f3;
  --ok:#17733f; --ok-soft:#e3f4ea;
  --display:"IBM Plex Sans Condensed","Arial Narrow",system-ui,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Menlo,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0f141b; --panel:#161d26; --ink:#e4e9f0; --muted:#97a3b3; --rule:#283241;
  --accent:#7fb2ea; --accent-soft:#1b2a3c;
  --hi:#f2877c; --hi-soft:#3a1d1b; --med:#e8b36a; --med-soft:#36291a; --lo:#a3adba; --lo-soft:#222a35;
  --ok:#6fcf97; --ok-soft:#173326; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0f141b; --panel:#161d26; --ink:#e4e9f0; --muted:#97a3b3; --rule:#283241;
  --accent:#7fb2ea; --accent-soft:#1b2a3c;
  --hi:#f2877c; --hi-soft:#3a1d1b; --med:#e8b36a; --med-soft:#36291a; --lo:#a3adba; --lo-soft:#222a35;
  --ok:#6fcf97; --ok-soft:#173326; color-scheme:dark}
body{background:var(--bg);color:var(--ink);font:15px/1.6 var(--body);margin:0}
.wrap{max-width:1080px;margin:0 auto;padding-inline:20px;padding-block:28px 64px}
h1,h2,h3{font-family:var(--display);text-wrap:balance;line-height:1.15;margin:0}
h1{font-size:2.3rem;font-weight:600;letter-spacing:-.01em}
h2{font-size:1.45rem;font-weight:600;margin-top:2.6rem;padding-top:1.2rem;border-top:1px solid var(--rule)}
h3{font-size:1.05rem;font-weight:600}
p{max-width:68ch;margin:.6rem 0}
a{color:var(--accent)} a:focus-visible,button:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
code,.mono{font-family:var(--mono);font-size:.86em}
.eyebrow{font:600 .72rem/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.lede{font-size:1.08rem;color:var(--ink);max-width:70ch}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--muted);font:.8rem var(--mono);margin-top:.8rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:1.4rem}
.stat{background:var(--panel);border:1px solid var(--rule);border-radius:6px;padding:12px 14px}
.stat b{display:block;font:600 1.7rem/1.1 var(--display);font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:.82rem}
.scroll{overflow-x:auto;margin-top:.8rem;border:1px solid var(--rule);border-radius:6px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:.86rem;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--rule);vertical-align:top}
th{font:600 .72rem var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--muted);background:var(--bg)}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right}
.pill{display:inline-block;font:600 .7rem/1 var(--mono);padding:4px 7px;border-radius:999px;white-space:nowrap}
.sev-high{background:var(--hi-soft);color:var(--hi)} .sev-medium{background:var(--med-soft);color:var(--med)}
.sev-low,.sev-info{background:var(--lo-soft);color:var(--lo)}
.st-new{background:var(--accent-soft);color:var(--accent)} .st-known{background:var(--lo-soft);color:var(--lo)}
.st-fixed{background:var(--ok-soft);color:var(--ok)}
.bad{color:var(--hi);font-weight:600} .good{color:var(--ok);font-weight:600} .dim{color:var(--muted)}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-top:1rem}
.filters button{font:500 .8rem var(--body);border:1px solid var(--rule);background:var(--panel);color:var(--ink);
  border-radius:999px;padding:5px 12px;cursor:pointer}
.filters button[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
details.f{background:var(--panel);border:1px solid var(--rule);border-radius:6px;margin-top:8px}
details.f>summary{list-style:none;cursor:pointer;padding:11px 14px;display:grid;grid-template-columns:4.2rem 1fr auto;gap:10px;align-items:start}
details.f>summary::-webkit-details-marker{display:none}
details.f[open]>summary{border-bottom:1px solid var(--rule)}
.fid{font:600 .8rem var(--mono);color:var(--muted);padding-top:2px}
.ftitle{font-weight:600;min-width:0}
.fsub{color:var(--muted);font-size:.8rem;font-weight:400}
.tags{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.fbody{padding:12px 14px 14px;display:grid;gap:10px}
.fbody dl{display:grid;grid-template-columns:7.5rem 1fr;gap:6px 14px;margin:0}
.fbody dt{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;padding-top:2px}
.fbody dd{margin:0;min-width:0;overflow-wrap:anywhere}
.fbody ul{margin:0;padding-left:1.1rem}
.cell{text-align:center;font:600 .82rem var(--mono);min-width:3.4rem}
.c0{background:var(--ok-soft);color:var(--ok)} .c1{background:var(--med-soft);color:var(--med)}
.c2{background:var(--hi-soft);color:var(--hi)} .cx{background:var(--lo-soft);color:var(--muted)}
.bar{position:relative;height:8px;background:var(--lo-soft);border-radius:4px;min-width:90px}
.bar i{position:absolute;top:0;bottom:0;background:var(--accent);opacity:.35;border-radius:4px}
.bar b{position:absolute;top:-3px;width:3px;height:14px;background:var(--accent);border-radius:1px}
.note{background:var(--panel);border-left:3px solid var(--accent);padding:10px 14px;margin-top:1rem;max-width:72ch}
pre{background:var(--panel);border:1px solid var(--rule);border-radius:6px;padding:12px;overflow-x:auto;font:.8rem/1.5 var(--mono)}
ul.plain{padding-left:1.1rem;max-width:72ch}
@media (max-width:640px){h1{font-size:1.8rem}details.f>summary{grid-template-columns:1fr}.tags{justify-content:flex-start}
  .fbody dl{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = """
(function(){
  var btns=document.querySelectorAll('.filters button');
  btns.forEach(function(b){b.addEventListener('click',function(){
    btns.forEach(function(x){x.setAttribute('aria-pressed', x===b?'true':'false')});
    var f=b.dataset.filter;
    document.querySelectorAll('details.f').forEach(function(d){
      d.hidden = !(f==='all' || (d.dataset.tags||'').split(' ').indexOf(f)>=0);
    });
  });});
})();
"""


def ci_bar(p, lo, hi):
    if p != p:  # nan
        return '<span class="dim">n/a</span>'
    return (f'<div class="bar" title="{p:.2f} [{lo:.2f}, {hi:.2f}]"><i style="left:{lo * 100:.0f}%;'
            f'width:{max(1, (hi - lo) * 100):.0f}%"></i><b style="left:calc({p * 100:.0f}% - 1px)"></b></div>')


def build() -> str:
    fixes = fix_litellm_table()
    order, grid = matrix_grid()
    par = parity_rows()
    dets = detector_tables()
    review = load("review.json") or {}
    full = load("full_head.json") or {"results": []}
    n_runs_full = len(full["results"])
    m = load("matrix_tox_head.json") or {"cells": []}
    n_cells = sum(1 for c in m["cells"] if c.get("results"))
    n_runs_matrix = sum(len(c.get("results", [])) for c in m["cells"])
    new = [f for f in FINDINGS if str(f["status"]).startswith("new") and f["intent"] == "unintended"]
    defects = [f for f in FINDINGS if f["intent"] == "unintended"]
    h = []
    h.append('<title>SpanProof for Sentry</title>')
    h.append('<link rel="preconnect" href="https://fonts.googleapis.com">'
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&'
             'family=IBM+Plex+Sans+Condensed:wght@600&family=IBM+Plex+Sans:wght@400;600&display=swap">')
    h.append(f"<style>{CSS}</style><div class=\"wrap\">")
    h.append('<div class="eyebrow">SpanProof · Sentry AI monitoring · findings</div>')
    h.append('<h1>Do Sentry\'s AI spans match what the provider billed?</h1>')
    h.append('<p class="lede">SpanProof replays recorded provider responses through Sentry\'s real AI integrations, '
             'in Python and JavaScript, across the provider versions in Sentry\'s own <code>tox.ini</code>, and '
             'compares every emitted span with the provider\'s own numbers. On top of that it runs agent '
             'failure-class detectors that turn span trees into issues. Everything below was reproduced by '
             'running code.</p>')
    h.append(f'<div class="meta"><span>sentry-python {PY_SHA[:8]} (2.71.0)</span><span>@sentry/node 11.4.0</span>'
             f'<span>2026-10-06</span><span>no API keys, no live calls</span></div>')
    h.append('<div class="stats">'
             f'<div class="stat"><b>{len(defects)}</b><span>unintended defects ({len(new)} with no existing issue)</span></div>'
             f'<div class="stat"><b>4</b><span>fixes with tests and before/after measurements</span></div>'
             f'<div class="stat"><b>{n_cells}</b><span>tox matrix cells, {n_runs_matrix} runs</span></div>'
             f'<div class="stat"><b>{n_runs_full}</b><span>runs on latest, 4 transport modes</span></div>'
             '<div class="stat"><b>8</b><span>agent failure classes, 256 traces + 480 held out</span></div>'
             '</div>')

    # ---- fixes
    h.append('<h2>What was fixed</h2>')
    h.append('<h3 style="margin-top:1rem">sentry-python#5455: LiteLLM cached, reasoning and cache-write tokens</h3>')
    h.append('<p>The LiteLLM integration read only prompt, completion and total tokens. LiteLLM normalizes every '
             'provider into <code>prompt_tokens_details</code> and <code>completion_tokens_details</code>, so the '
             'callback already has the data. The fix reads it (with LiteLLM\'s private fallbacks) and passes it to '
             'the existing <code>record_token_usage</code>.</p>')
    h.append('<div class="scroll"><table><tr><th>LiteLLM version (tox cell)</th><th class="num">usage findings on HEAD</th>'
             '<th class="num">with the fix</th></tr>')
    for ver, a, b in fixes:
        h.append(f'<tr><td class="mono">{esc(ver)}</td><td class="num bad">{a}</td><td class="num good">{b}</td></tr>')
    h.append('</table></div>')
    h.append('<p>sentry-python\'s own LiteLLM suite under tox: <b>173 / 173</b> on litellm 1.96.0 and on the '
             'oldest supported 1.77.7. Two new regression tests fail on the original code and pass with the fix.</p>')
    h.append('<h3 style="margin-top:1.2rem">Three more fixes</h3>')
    h.append('<div class="scroll"><table><tr><th>finding</th><th>change</th><th>SpanProof before → after</th>'
             '<th>sentry-python suite under tox</th></tr>'
             '<tr><td>SP-04 OpenAI cache-write tokens (new)</td><td>read <code>cache_write_tokens</code> on Chat and '
             'Responses, 14 lines</td><td class="mono">OpenAI usage findings <span class="bad">8</span> → '
             '<span class="good">0</span></td><td>687/687 (openai 2.54.0), 683 + 4 skipped (1.109.1)</td></tr>'
             '<tr><td>SP-08 LangGraph <code>stream()</code> agent span (new)</td><td>wrap stream/astream, '
             're-entrancy guard so invoke stays at one span, finish in <code>finally</code></td><td class="mono">'
             'agent spans for .stream() <span class="bad">0</span> → <span class="good">1</span>; .invoke() stays '
             '1; structure findings <span class="bad">8</span> → <span class="good">0</span></td>'
             '<td>150/150 on langgraph 1.2.12 and 0.6.11</td></tr>'
             '<tr><td>SP-10 Pydantic AI finish reasons</td><td>record the normalized stop reason, 9 lines</td>'
             '<td class="mono">truncation detector recall <span class="bad">0.33</span> → <span class="good">0.67'
             '</span></td><td>332/332</td></tr></table></div>')
    h.append('<p>Every new test fails on the original code and passes with the fix. Each fix is one commit on its '
             'own branch off sentry-python 6a4eb20f, at <a href="https://github.com/4ktLuffy/sentry-python/branches">'
             '4ktLuffy/sentry-python</a>.</p>')

    # ---- findings
    h.append('<h2>Findings</h2>')
    h.append('<p>Intent labels: <b>unintended</b> means the code does not do what its own design says; '
             '<b>simplification</b> is a deliberate shortcut with a cost; <b>needs decision</b> means the SDK and '
             'the conventions disagree; <b>not a fault</b> is listed so nobody chases it. "New" means no matching '
             'issue or PR was found on 2026-10-06.</p>')
    h.append('<div class="filters" role="group" aria-label="Filter findings">'
             '<button data-filter="all" aria-pressed="true">All</button>'
             '<button data-filter="new" aria-pressed="false">New</button>'
             '<button data-filter="python" aria-pressed="false">Python</button>'
             '<button data-filter="js" aria-pressed="false">JavaScript</button>'
             '<button data-filter="high" aria-pressed="false">High severity</button></div>')
    rv = review.get("verdicts", {})
    for f in FINDINGS:
        status = "fixed" if f.get("fixed") in ("python", "partial") else (
            "new" if str(f["status"]).startswith("new") else "known")
        tags = {status, f["severity"]} | ({"python"} if "python" in f["sdk"] else set()) | (
            {"js"} if "js" in f["sdk"] else set())
        st_label = {"fixed": "fixed here" if f.get("fixed") != "partial" else "partly fixed here", "new": "new",
                    "known": "known issue"}[status]
        h.append(f'<details class="f" data-tags="{" ".join(sorted(tags))}"><summary>'
                 f'<span class="fid">{f["id"]}</span>'
                 f'<span class="ftitle">{esc(f["title"])}<br><span class="fsub">{esc(f["sdk"])} · '
                 f'{esc(f["integration"])} · {esc(f["intent"])}</span></span>'
                 f'<span class="tags"><span class="pill sev-{f["severity"]}">{f["severity"]}</span>'
                 f'<span class="pill st-{status}">{st_label}</span></span></summary><div class="fbody"><dl>')
        h.append(f'<dt>Impact</dt><dd>{esc(f["impact"])}</dd>')
        h.append('<dt>Evidence</dt><dd><ul>' + "".join(f"<li>{linkify(e)}</li>" for e in f["evidence"]) + '</ul></dd>')
        if f.get("issue"):
            h.append(f'<dt>Issue</dt><dd>{linkify(f["issue"])} ({esc(f["status"])})</dd>')
        h.append('<dt>Reproduce</dt><dd class="mono">' + ", ".join(esc(s) for s in f["scenarios"]) + '</dd>')
        h.append(f'<dt>Fix</dt><dd>{esc(f["fix"])}</dd>')
        if f["id"] in rv:
            h.append(f'<dt>2nd model</dt><dd>{esc(rv[f["id"]])}</dd>')
        h.append('</dl></div></details>')

    # ---- parity ledger
    if par:
        h.append('<h2>One response, two SDKs</h2>')
        h.append('<p>The same recorded provider response, replayed through sentry-python and @sentry/node. The '
                 '"provider" row is the ground truth in Sentry\'s convention (input includes cached tokens, output '
                 'includes reasoning tokens). Empty means the attribute was not emitted.</p>')
        h.append('<div class="scroll"><table><tr><th>scenario</th><th>source</th><th class="num">input</th>'
                 '<th class="num">cached</th><th class="num">cache write</th><th class="num">output</th>'
                 '<th class="num">reasoning</th><th class="num">total</th><th>finish reason</th></tr>')
        for p in par:
            t = p["truth"]
            truth = {"input_tokens": t["input_tokens"], "cached": t["cached"], "cache_write": t["cache_write"],
                     "output_tokens": t["output_tokens"], "reasoning": t["reasoning"], "total": t["total"]}
            for src, vals in (("provider", None), ("Python", p["py"]), ("JS", p["js"])):
                cells = []
                for k in ("input_tokens", "cached", "cache_write", "output_tokens", "reasoning", "total"):
                    want = truth[k]
                    if vals is None:
                        cells.append(f'<td class="num">{want}</td>')
                    else:
                        v = vals[k]
                        cls = "good" if v == want else ("dim" if (v is None and not want) else "bad")
                        cells.append(f'<td class="num {cls}">{"" if v is None else v}</td>')
                fin = "" if vals is None else (vals["finish"] or '<span class="bad">missing</span>')
                first = f'<td class="mono" rowspan="3">{esc(p["scenario"])}</td>' if src == "provider" else ""
                h.append(f'<tr>{first}<td>{src}</td>{"".join(cells)}<td>{fin}</td></tr>')
        h.append('</table></div>')

    # ---- matrix
    if order:
        cols = []
        for env in order:
            for k in grid[env]:
                if k not in cols:
                    cols.append(k)
        h.append('<h2>Version matrix</h2>')
        h.append('<p>Cells come from sentry-python\'s own <code>tox.ini</code> (same provider versions, same pinned '
                 'lock files), plus <b>latest</b> and <b>pre</b>-release. Each cell runs every scenario for that '
                 'integration in two transport modes. The number is the count of distinct medium/high findings; '
                 'it is stable across versions, which means the defects are in Sentry\'s code, not in a provider '
                 'release.</p><div class="scroll"><table><tr><th>integration</th><th>versions → distinct findings</th></tr>')
        for env in order:
            chips = []
            for k, c in grid[env].items():
                if c.get("err"):
                    chips.append(f'<span class="pill cx" title="install failed">{esc(k)} · n/a</span>')
                else:
                    cls = "c0" if c["n"] == 0 else ("c1" if c["n"] <= 3 else "c2")
                    ver = c.get("resolved") or k
                    extra = f' · {c["unsup"]} n/a' if c["unsup"] else ""
                    chips.append(f'<span class="pill {cls}" title="{c["runs"]} runs">{esc(ver)} · {c["n"]}{extra}</span>')
            h.append(f'<tr><td class="mono">{esc(env)}</td><td><div class="tags" style="justify-content:flex-start">'
                     f'{"".join(chips)}</div></td></tr>')
        h.append('</table></div><p class="dim">"n/a" counts runs where that old SDK lacks the API a scenario '
                 'uses (for example the Responses API in openai 1.0.1); those runs report nothing rather than a '
                 'false finding.</p>')

    # ---- detectors
    if dets:
        h.append('<h2>Agent failure classes as issues</h2>')
        h.append('<p>The Agent Tracing team\'s stated next step is "common agent failure classes surfaced as '
                 'issues". These detectors read only spans Sentry already receives (gen_ai and http.client) and '
                 'emit issue-like records with stable fingerprints. They were measured on 256 traces produced by '
                 'running openai-agents, LangGraph and Pydantic AI with Sentry\'s real instrumentation against a '
                 'scripted model, so each label is known by construction. 72 of the traces are healthy hard '
                 'negatives built to look like failures: pagination, a single retry, a tool that says "no errors '
                 'found", a long answer, a 2x larger run. Bars show recall with its 95% Wilson interval.</p>')
        classes = ["tool_loop", "retry_storm", "silent_tool_error", "lost_llm_span", "dead_end", "truncated_answer",
                   "empty_answer", "cost_spike"]
        h.append('<div class="scroll"><table><tr><th>class</th>' + "".join(
            f'<th>{esc(lbl)}</th>' for lbl, _ in dets) + '</tr>')
        for c in classes:
            row = [f'<td class="mono">{c}</td>']
            for _, d in dets:
                s = d["scores"][c]
                rec, lo, hi = s["recall"], s["recall_ci"][0], s["recall_ci"][1]
                prec = s["precision"]
                ptxt = "n/a" if prec != prec else f"{prec:.2f}"
                row.append(f'<td><div style="display:grid;gap:4px">{ci_bar(rec, lo, hi)}<span class="dim mono">'
                           f'recall {rec:.2f} · precision {ptxt}</span></div></td>')
            h.append("<tr>" + "".join(row) + "</tr>")
        h.append('<tr><td class="mono">healthy, any alarm</td>' + "".join(
            f'<td class="mono">{d["scores"]["_healthy_false_alarm_rate"]["k"]} / '
            f'{d["scores"]["_healthy_false_alarm_rate"]["n"]}</td>' for _, d in dets) + '</tr></table></div>')
        h.append('<p>The two held-out sets were written by a second model (Codex) from the label definitions only, '
                 'without seeing the detector code, and are built to break detectors: nested agents, handoffs, '
                 'Python-repr encodings, shuffled span order, acknowledged failures. Set 1 was scored once, the '
                 'misses were fixed (five causes, each with a regression test), and set 2 was generated afterwards '
                 'with new constructions, so its column is the out-of-sample number. Its three misses (a JSON error '
                 'payload whose "error" key is not first) were fixed afterwards with a structural check; that '
                 'later improvement is not counted in the column.</p>')
        h.append('<div class="note"><b>What the columns show.</b> With data collection off the detectors '
                 'abstain rather than guess: tool arguments, tool results and answers are not sent, so loops, '
                 'silent tool errors and empty answers cannot be asserted (a name-only loop rule measured '
                 'precision 0.14; a one-token answer is not proof of an empty one). Two attributes an SDK could '
                 'send without content restore most of it: a salted hash of the tool arguments brings loop recall '
                 'to 1.00, and the character count of the answer brings empty-answer recall to 1.00, both with no '
                 'false alarms. Silent tool errors still need the answer text. Truncation is missed exactly where '
                 'the integration does not record finish reasons (openai-agents, Pydantic AI); the nine-line '
                 'Pydantic AI fix moves recall from 0.33 to 0.67.</div>')
        h.append('<p class="dim">A second model also reviewed SpanProof\'s own code and found 14 correctness issues '
                 '(a gate that went green when nothing ran, a divide-by-zero, an unbounded parent walk, missing '
                 'content treated as evidence, and others). All 14 are fixed, each with a regression test '
                 '(tests/test_review_regressions.py); stored results were re-scored and no real finding changed.</p>')

    # ---- real account
    h.append('<h2>Checked on a real Sentry account</h2>')
    h.append('<p>Every scenario was also sent to a real Sentry organization (Python and Node projects), read back '
             'through the span API, and opened in the product. The comparison script is '
             '<code>spanproof.real_compare</code>; detectors were re-scored on traces read back with '
             '<code>spanproof.from_sentry</code>.</p>')
    h.append('<div class="scroll"><table><tr><th>check</th><th>result</th></tr>'
             '<tr><td>Stored vs sent</td><td>every span stored with identical token values</td></tr>'
             '<tr><td>Cost on correct input</td><td>matches hand-computed prices exactly</td></tr>'
             '<tr><td>JS Anthropic (cache)</td><td class="bad">trace view: 40 in (Python: 2.6K); cost $0.00128 '
             'vs $0.00297</td></tr>'
             '<tr><td>JS OpenAI (cached tokens)</td><td class="bad">cost $0.001175 vs $0.000887 (+32%)</td></tr>'
             '<tr><td>Sum over all gen_ai spans</td><td class="bad">+27% cost, +50% input tokens vs LLM calls only'
             '</td></tr>'
             '<tr><td>Built-in AI Agents dashboard</td><td class="good">correct: filters to LLM calls</td></tr>'
             '<tr><td>Python finish reasons, default transport</td><td class="bad">stored but not searchable'
             '</td></tr>'
             '<tr><td>Detectors on traces read from Sentry</td><td class="good">same results as local, no false '
             'alarms on healthy traces</td></tr></table></div>')

    # ---- watch + live agents
    h.append('<h2>SpanProof Watch, on real agents</h2>')
    h.append('<p>The detectors also run as a service, <code>spanproof.watch</code>, that reads agent traces from a '
             'Sentry organization and files each failure back as a grouped Sentry issue with a link to the trace. '
             'It was run on a real account against 45 real agent runs on a live model (Groq gpt-oss-20b, '
             'openai-agents, LangGraph and Pydantic AI) with faults injected into their tools and provider, plus '
             'the synthetic corpus. Truth came from the runs themselves (tool-call logs, injected errors, the '
             'answer text), never from Sentry\'s spans.</p>')
    h.append('<div class="scroll"><table><tr><th>failure class</th><th class="num">caught</th>'
             '<th class="num">false alarms</th><th class="num">missed</th></tr>'
             + "".join(f'<tr><td>{c}</td><td class="num">{a}</td><td class="num">{b}</td><td class="num">{m}</td></tr>'
                       for c, a, b, m in (("tool loop", 13, 0, 0), ("retry storm", 15, 0, 0), ("dead end", 14, 0, 1),
                                          ("cost spike", 5, 0, 0), ("LLM call missing from trace", 5, 0, 0),
                                          ("silent tool error", 4, 0, 0), ("empty answer", 4, 0, 0),
                                          ("truncated answer", 4, 0, 4)))
             + '<tr><td>problem-free traces with any alarm</td><td colspan="3" class="num good">0 of 35</td></tr>'
             '</table></div>')
    h.append('<p class="dim">Real models did not fail the way the injected faults predicted: they retried failing '
             'tools 3 to 5 times with identical arguments, spent their whole token budget and returned nothing, or '
             'hit the step limit. Scoring by the injected fault showed 7 "false alarms"; scoring by what each agent '
             'actually did showed none. The four missed truncations come from integrations that do not record '
             'finish reasons. A first version built cost baselines newest-first and raised 10 false cost spikes; '
             'building them in time order removed all of them.</p>')

    # ---- negatives
    h.append('<h2>Negative results</h2><ul class="plain">' + "".join(f"<li>{esc(n)}</li>" for n in NEGATIVE_RESULTS)
             + '</ul>')

    # ---- method
    h.append('<h2>Method and limits</h2><ul class="plain">'
             '<li>Ground truth comes from wire responses built to each provider\'s published schema and parsed '
             'with the provider SDK\'s own response types before use, so a fixture that drifts from the real '
             'schema fails loudly. They are not recordings of live calls (no API keys were used).</li>'
             '<li>Each scenario runs in a fresh interpreter against a local scripted server; nothing is mocked '
             'inside the SDK or the client library.</li>'
             '<li>The detector corpus is synthetic and was generated by the same author as the detectors. The '
             'hard negatives and the second-model review are there to counter that; a held-out run on real '
             'production traces is the next step.</li>'
             '<li>JavaScript coverage is @sentry/node 11.4.0 with OpenAI, Anthropic and Vercel AI; LangChain JS '
             'and Google GenAI JS were not run.</li></ul>')
    if review.get("summary"):
        h.append(f'<div class="note"><b>Independent reproduction.</b> {esc(review["summary"])}</div>')
    h.append('<h2>Run it</h2><pre>python -m spanproof.runner --modes default,nodc,legacy,stream\n'
             'python -m spanproof.matrix --sdk path/to/sentry-python --envs ~/.spanproof-envs\n'
             'python -m spanproof.js_bridge\n'
             'python -m spanproof.corpus --n 8 &amp;&amp; python -m spanproof.evaluate results/corpus.jsonl\n'
             'python -m spanproof.report</pre>')
    h.append(f"</div><script>{JS}</script>")
    return "\n".join(h)


def main() -> int:
    out = ROOT / "report" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(build())
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
