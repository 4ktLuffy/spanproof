"""Checks for Sentry's MCP server spans against what the MCP server really handled.

Input: one `spanproof.mcp_worker` result. Truth comes from three places the SDK under test
never touches: what each handler received and returned (`truth.server`), what the client got
back over the wire (`truth.client`), and every JSON-RPC message as it reached the HTTP server
(`truth.wire`). Findings have the same shape as `spanproof.oracles` findings.

    python -m spanproof.mcp_checks results/mcp_sdk.json ...   # re-score saved runs, one row per rule
"""

from __future__ import annotations

import gzip
import json
import sys

from . import conventions as cv
from .mcp_sc import PNG, SECRET_ARG, SECRET_OUT
from .oracles import _f, ancestors

COVERED = ("tools/call", "prompts/get", "resources/read")  # what the integration instruments
TARGET_KEYS = {"tools/call": ("mcp.tool.name", "gen_ai.tool.name"), "prompts/get": ("mcp.prompt.name", "gen_ai.prompt.name"),
               "resources/read": ("mcp.resource.uri",)}
TRANSPORT = {"stdio": ("stdio", "pipe"), "http": ("http", "tcp"), "http-stateless": ("http", "tcp"), "sse": ("sse", "tcp")}


def is_error(span) -> bool:
    return span.get("status") not in (None, "ok", "unset")


def _key(method, target, args):
    return method, target, json.dumps(args or {}, sort_keys=True, default=str)


def requests(r) -> list[dict]:
    """One record per covered request the client sent, joined with what its handler saw (if it ran)."""
    srv = {}
    for s in r["truth"]["server"]:
        srv.setdefault(_key(s["method"], s["target"], s["args"]), []).append(s)
    out = []
    for c in r["truth"]["client"]:
        if c["method"] not in COVERED:
            continue
        lst = srv.get(_key(c["method"], c["target"], c["args"]))
        s = lst.pop(0) if lst else None
        q = dict(c, handled=s is not None, request_id=(s or {}).get("request_id"), raised=bool(s and s["raised"]),
                 error=(s or {}).get("error"), returned_is_error=bool(s and s.get("returned_is_error")))
        q["errored"] = bool(q["raised"] or q.get("is_error") or q.get("protocol_error"))
        out.append(q)
    return out


def _target(span, method):
    for k in TARGET_KEYS.get(method, ()):
        if k in span["data"]:
            return span["data"][k]
    return None


def _method(span):
    return span["data"].get("mcp.method.name") or (span.get("description") or "").split(" ")[0]


def _argval(v):
    return v if isinstance(v, str) else json.dumps(v)


def match(r) -> tuple[list[tuple[dict, list[dict]]], list[dict]]:
    """Pair each request with its span(s): request id first, then arguments, then start order
    (skipping spans whose id belongs to another request, so a wrong id is still reported as wrong).

    Returns [(request, [spans])] and the MCP spans no request claimed."""
    spans = sorted([s for s in r["spans"] if s.get("op") == "mcp.server"], key=lambda s: s.get("start") or 0)
    reqs = requests(r)
    known = {str(q["request_id"]) for q in reqs if q["request_id"] is not None}
    used, pairs = set(), []
    for q in reqs:
        cand = [s for s in spans if id(s) not in used and _method(s) == q["method"] and _target(s, q["method"]) == q["target"]]
        hit = [s for s in cand if q["request_id"] is not None and str(s["data"].get("mcp.request.id")) == str(q["request_id"])]
        if not hit:
            hit = [s for s in cand if q["args"] and all(
                s["data"].get(f"mcp.request.argument.{k}") == _argval(v) for k, v in q["args"].items())][:1]
        if not hit:
            # a span whose id belongs to another request is not this one's; an unknown id can still be
            hit = [s for s in cand if str(s["data"].get("mcp.request.id")) not in known][:1]
        for s in hit:
            used.add(id(s))
        pairs.append((q, hit))
    return pairs, [s for s in spans if id(s) not in used]


def _collection(r):
    """(inputs_on, outputs_on, prompt_content_on) as the integration documents them."""
    dc = r.get("data_collection_option")
    if dc:
        g = dc.get("gen_ai", {})
        return g.get("inputs", True), g.get("outputs", True), g.get("inputs", True)
    pii_prompts = bool(r["data_collection"] and r.get("include_prompts", True))
    return True, pii_prompts, pii_prompts


def _same(a, b) -> bool:
    def norm(x):
        try:
            return json.loads(x) if isinstance(x, str) else x
        except ValueError:
            return x
    return norm(a) == norm(b)


# ------------------------------------------------------------------ checks

def check_lifecycle(r):
    out = []
    pairs, extra = match(r)
    for q, hit in pairs:
        if not hit and not q["handled"] and "method not found" in str(q.get("protocol_error")).lower():
            continue  # the server does not offer this method at all (old fastmcp has no prompts)
        if not hit and not q["handled"]:
            out.append(_f(r, "mcp.lifecycle.missing_unhandled", "low", f"{q['label']}: no {q['method']} span; the "
                          f"server rejected it before the handler ran ({q.get('protocol_error') or 'isError'})", q["label"]))
        elif not hit:
            out.append(_f(r, "mcp.lifecycle.missing", "high", f"{q['label']}: no {q['method']} span", q["label"]))
        elif len(hit) > 1:
            out.append(_f(r, "mcp.lifecycle.duplicate", "high", f"{q['label']}: {len(hit)} spans for one request",
                          q["label"], expected=1, actual=len(hit)))
        for s in hit:
            if not s.get("finished"):
                out.append(_f(r, "mcp.lifecycle.unfinished", "high", f"{q['label']}: span never finished", q["label"]))
    for s in extra:
        out.append(_f(r, "mcp.lifecycle.duplicate", "high", f"span {s.get('description')!r} matches no request "
                      "left over (a second span for one request)", attribute=s.get("description")))
    return out


def check_identity(r):
    out = []
    pairs, _ = match(r)
    wire = {str(w["id"]): w for w in r["truth"].get("wire", []) if w.get("id") is not None}
    for q, hit in pairs:
        for s in hit:
            d = s["data"]
            if d.get("mcp.method.name") != q["method"]:
                out.append(_f(r, "mcp.identity.method", "medium", f"{q['label']}: mcp.method.name is "
                              f"{d.get('mcp.method.name')!r}", q["label"], "mcp.method.name", q["method"], d.get("mcp.method.name")))
            want_name = f"{q['method']} {q['target']}"
            if s.get("description") != want_name:
                out.append(_f(r, "mcp.identity.name", "low", f"{q['label']}: span name {s.get('description')!r}",
                              q["label"], "name", want_name, s.get("description")))
            rid = d.get("mcp.request.id", d.get("jsonrpc.request.id"))
            if q["request_id"] is not None and str(rid) != str(q["request_id"]):
                out.append(_f(r, "mcp.identity.request_id", "medium", f"{q['label']}: request id {rid!r}, the handler "
                              f"ran for {q['request_id']!r}", q["label"], "mcp.request.id", q["request_id"], rid))
            if wire and q["request_id"] is not None and str(q["request_id"]) not in wire:
                out.append(_f(r, "mcp.identity.request_id", "medium", f"{q['label']}: id {q['request_id']!r} never "
                              "arrived on the wire", q["label"], "mcp.request.id"))
    return out


def check_inputs(r):
    out = []
    inputs_on, _, _ = _collection(r)
    pairs, _ = match(r)
    for q, hit in pairs:
        for s in hit:
            got = {k[len("mcp.request.argument."):]: v for k, v in s["data"].items()
                   if k.startswith("mcp.request.argument.")}
            if not inputs_on:
                continue
            for k, v in (q["args"] or {}).items():
                if k not in got:
                    out.append(_f(r, "mcp.input.missing", "medium", f"{q['label']}: argument {k!r} not recorded",
                                  q["label"], f"mcp.request.argument.{k}", _argval(v), None))
                elif got[k] != _argval(v):
                    out.append(_f(r, "mcp.input.wrong", "high", f"{q['label']}: argument {k!r} is {got[k]!r}",
                                  q["label"], f"mcp.request.argument.{k}", _argval(v), got[k]))
            for k in set(got) - set(q["args"] or {}):
                out.append(_f(r, "mcp.input.extra", "medium", f"{q['label']}: argument {k!r} was not sent",
                              q["label"], f"mcp.request.argument.{k}", None, got[k]))
    return out


def check_outputs(r):
    out = []
    _, outputs_on, prompt_on = _collection(r)
    pairs, _ = match(r)
    for q, hit in pairs:
        for s in hit:
            d = s["data"]
            content = d.get("mcp.tool.result.content", d.get("gen_ai.tool.call.result"))
            if q["method"] == "tools/call" and outputs_on and not q.get("protocol_error"):
                types = q.get("content_types") or []
                want = q.get("structured") if q.get("structured") is not None else q.get("text")
                if content is None and (q["raised"] or is_error(s)):
                    pass  # an exception left the handler: there is no result to record
                elif content is None:
                    out.append(_f(r, "mcp.output.missing", "medium", f"{q['label']}: tool result not recorded",
                                  q["label"], "mcp.tool.result.content", want, None))
                elif PNG in str(content):
                    out.append(_f(r, "mcp.output.binary", "medium", f"{q['label']}: base64 image bytes stored as the "
                                  f"tool result ({len(str(content))} chars)", q["label"], "mcp.tool.result.content",
                                  "a description of the image block", f"{str(content)[:60]}..."))
                elif [t for t in types if t != "text"] and not any(t in str(content) for t in types if t != "text"):
                    out.append(_f(r, "mcp.output.dropped_content", "medium", f"{q['label']}: result has {types} but the "
                                  f"span keeps only {str(content)[:60]!r}", q["label"], "mcp.tool.result.content", types, content))
                elif (not [t for t in types if t != "text"] and q.get("structured") is not None
                      and not _same(content, want) and _same(content, q.get("text"))):
                    out.append(_f(r, "mcp.output.structured_ignored", "low", f"{q['label']}: the result's "
                                  f"structuredContent is not recorded, its text is ({str(content)[:60]!r})", q["label"],
                                  "mcp.tool.result.content", want, content))
                elif not [t for t in types if t != "text"] and want is not None and not _same(content, want):
                    out.append(_f(r, "mcp.output.wrong", "high", f"{q['label']}: tool result {str(content)[:80]!r}",
                                  q["label"], "mcp.tool.result.content", want, content))
                n = d.get("mcp.tool.result.content_count")
                if n is not None and n != len(types):
                    out.append(_f(r, "mcp.output.content_count", "low", f"{q['label']}: content_count {n}, the result "
                                  f"has {len(types)} content item(s)", q["label"], "mcp.tool.result.content_count", len(types), n))
            if q["method"] == "prompts/get" and not q.get("protocol_error"):
                n = d.get("mcp.prompt.result.message_count")
                if n != q.get("message_count"):
                    out.append(_f(r, "mcp.output.message_count", "medium", f"{q['label']}: message_count {n!r}",
                                  q["label"], "mcp.prompt.result.message_count", q.get("message_count"), n))
                if prompt_on and q.get("message_count") == 1:
                    for k, want in (("mcp.prompt.result.message_role", (q.get("roles") or [None])[0]),
                                    ("mcp.prompt.result.message_content", q.get("text"))):
                        if d.get(k) != want:
                            out.append(_f(r, "mcp.output.prompt", "medium", f"{q['label']}: {k} is {d.get(k)!r}",
                                          q["label"], k, want, d.get(k)))
    return out


def check_privacy(r):
    out = []
    inputs_on, outputs_on, prompt_on = _collection(r)
    for s in [s for s in r["spans"] if s.get("op") == "mcp.server"]:
        blob = json.dumps(s["data"], default=str)
        if not outputs_on and (SECRET_OUT in blob or "mcp.tool.result.content" in s["data"]):
            out.append(_f(r, "mcp.privacy.output_leak", "high", f"{s.get('description')}: tool result recorded with "
                          "output collection off", attribute="mcp.tool.result.content"))
        if not prompt_on and "mcp.prompt.result.message_content" in s["data"]:
            out.append(_f(r, "mcp.privacy.output_leak", "high", f"{s.get('description')}: prompt content recorded "
                          "with collection off", attribute="mcp.prompt.result.message_content"))
        if not inputs_on and SECRET_ARG in blob:
            out.append(_f(r, "mcp.privacy.input_leak", "high", f"{s.get('description')}: argument recorded with "
                          "input collection off", attribute="mcp.request.argument.*"))
        if inputs_on and not r["data_collection"] and not r.get("data_collection_option") and SECRET_ARG in blob:
            out.append(_f(r, "mcp.privacy.arguments_without_pii", "low", f"{s.get('description')}: tool argument "
                          "values recorded with send_default_pii=False (sentry-python's tests assert this)",
                          attribute="mcp.request.argument.*"))
    return out


def check_errors(r):
    out = []
    pairs, _ = match(r)
    events = r.get("error_events") or [dict(e, mechanism=None, message=None) for e in r.get("errors", [])]
    for q, hit in pairs:
        for s in hit:
            if q["errored"] and not is_error(s) and "error.type" not in s["data"] and not s["data"].get(
                    "mcp.tool.result.is_error"):
                how = ("the handler raised and the server answered isError" if q["raised"] and q.get("is_error") else
                       "the server answered isError without running the handler" if q.get("is_error") and not q["handled"]
                       else "the handler returned isError" if q.get("is_error") else
                       "the handler raised" if q["raised"] else "the request failed")
                out.append(_f(r, "mcp.errors.status", "high", f"{q['label']}: span status {s.get('status')!r} but "
                              f"{how}", q["label"], "status", "error", s.get("status")))
            if not q["errored"] and is_error(s):
                out.append(_f(r, "mcp.errors.spurious", "medium", f"{q['label']}: span marked {s.get('status')!r} for "
                              "a request that succeeded", q["label"], "status", "ok", s.get("status")))
        if q["raised"]:
            msg = (q["error"] or "").split(": ", 1)[-1]
            hits = [e for e in events if msg and msg in f"{e.get('type')}: {e.get('value')} {e.get('message')} "
                    f"{' '.join(e.get('chain') or [])}"]
            if not hits:
                out.append(_f(r, "mcp.errors.not_captured", "medium", f"{q['label']}: handler raised {q['error']!r}, "
                              "no error event", q["label"], expected=q["error"]))
            elif not any(e.get("mechanism") == "mcp" for e in hits) and r.get("error_events") is not None:
                out.append(_f(r, "mcp.errors.captured_by_logging_only", "low", f"{q['label']}: {q['error']!r} reached "
                              f"Sentry only through the framework's log call ({hits[0].get('mechanism')}), not the MCP "
                              "integration", q["label"], expected="mcp", actual=hits[0].get("mechanism")))
    return out


def check_transport(r):
    out = []
    want = TRANSPORT.get(r.get("transport"))
    if not want:
        return out  # in-memory streams are not a transport
    wire = {str(w["id"]): w for w in r["truth"].get("wire", []) if w.get("id") is not None}
    pairs, _ = match(r)
    for q, hit in pairs:
        for s in hit:
            d = s["data"]
            for k, v in zip(("mcp.transport", "network.transport"), want):
                if d.get(k) != v:
                    out.append(_f(r, "mcp.transport.value", "medium", f"{q['label']}: {k} is {d.get(k)!r}",
                                  q["label"], k, v, d.get(k)))
            w = wire.get(str(q["request_id"]))
            sid = (w or {}).get("session_header") or (w or {}).get("session_query") if r["transport"] != "stdio" else None
            if r["transport"] in ("http", "http-stateless", "sse") and not w:
                continue
            if d.get("mcp.session.id") != sid:
                out.append(_f(r, "mcp.transport.session", "medium", f"{q['label']}: mcp.session.id "
                              f"{d.get('mcp.session.id')!r}, the request carried {sid!r}", q["label"], "mcp.session.id",
                              sid, d.get("mcp.session.id")))
    return out


def check_structure(r):
    out = []
    by_id = {s["span_id"]: s for s in r["spans"]}
    mcp_spans = [s for s in r["spans"] if s.get("op") == "mcp.server"]
    http = r.get("transport") in ("http", "http-stateless", "sse")
    under = {}
    for s in mcp_spans:
        anc, loop = ancestors(s, by_id)
        if loop:
            out.append(_f(r, "mcp.structure.loop", "high", f"{s.get('description')}: parent chain loops"))
        nest = [a for a in anc if a.get("op") == "mcp.server"]
        if nest:
            out.append(_f(r, "mcp.structure.nested", "high", f"{s.get('description')} (id {s['data'].get('mcp.request.id')}) "
                          f"is a child of {nest[0].get('description')} (id {nest[0]['data'].get('mcp.request.id')}); "
                          "independent requests", attribute="parent_span_id", expected="request's own parent",
                          actual=nest[0].get("description")))
        if s.get("parent_span_id") and s["parent_span_id"] not in by_id:
            out.append(_f(r, "mcp.structure.orphan", "high", f"{s.get('description')}: parent "
                          f"{s['parent_span_id']} was never sent", attribute="parent_span_id"))
        h = [a for a in anc if a.get("op") == "http.server"]
        if http and not h:
            out.append(_f(r, "mcp.structure.detached", "medium", f"{s.get('description')}: not under the HTTP request "
                          "that carried it", attribute="parent_span_id", expected="http.server"))
        if h:
            under.setdefault(h[0]["span_id"], []).append(s)
    for hid, ss in under.items():
        if len(ss) > 1 and r.get("transport") != "sse":
            out.append(_f(r, "mcp.structure.shared_http_parent", "medium", f"{len(ss)} MCP spans under one HTTP request "
                          f"{by_id[hid].get('description')}", attribute="parent_span_id", actual=len(ss)))
    return out


def check_conventions(r):
    out, seen = [], set()
    for s in [s for s in r["spans"] if s.get("op") == "mcp.server"]:
        for k, v in s["data"].items():
            if not k.startswith(cv.MCP_KEYS) and not k.startswith("mcp."):
                continue
            d = cv.mcp_lookup(k)
            tag = k if not k.startswith("mcp.request.argument.") else "mcp.request.argument.<key>"
            if d is None:
                if ("unknown", tag) not in seen:
                    out.append(_f(r, "mcp.conventions.unknown", "low", f"{k} is not in sentry-conventions", attribute=k))
                seen.add(("unknown", tag))
                continue
            t = d.get("type")
            ok = {"string": isinstance(v, str), "integer": isinstance(v, int) and not isinstance(v, bool),
                  "boolean": isinstance(v, bool), "double": isinstance(v, (int, float))}.get(t, True)
            if not ok and ("type", tag) not in seen:
                seen.add(("type", tag))
                out.append(_f(r, "mcp.conventions.type", "low", f"{k} is {type(v).__name__} {v!r}, registry says {t}",
                              attribute=k, expected=t, actual=type(v).__name__))
            dep = d.get("deprecation")
            if dep and dep.get("replacement") and dep["replacement"] not in s["data"] and ("dep", tag) not in seen:
                seen.add(("dep", tag))
                out.append(_f(r, "mcp.conventions.deprecated", "low", f"{k} is deprecated for {dep['replacement']}, "
                              "which is not set", attribute=k, expected=dep["replacement"]))
    return out


def check_coverage(r):
    """Requests the integration does not trace at all (it covers tools/call, prompts/get, resources/read)."""
    sent = {c["method"] for c in r["truth"]["client"]} | {w["method"] for w in r["truth"].get("wire", [])}
    traced = {_method(s) for s in r["spans"] if s.get("op") == "mcp.server"}
    return [_f(r, "mcp.coverage.unspanned", "low", f"{m} requests produce no span", attribute=m)
            for m in sorted(sent - traced - set(COVERED))]


def check_harness(r):
    out = []
    if r.get("exception") or r.get("server_exception"):
        out.append(_f(r, "mcp.harness.exception", "high", f"run failed: {r.get('exception') or r.get('server_exception')}"))
    return out


CHECKS = (check_harness, check_lifecycle, check_identity, check_inputs, check_outputs, check_privacy, check_errors,
          check_transport, check_structure, check_conventions, check_coverage)


def run_all(r: dict) -> list[dict]:
    out = []
    for c in CHECKS:
        for f in c(r):
            f.update(flavor=r.get("flavor"), transport=r.get("transport"), mode=r.get("mode"),
                     versions=r.get("versions"))
            out.append(f)
    return out


def summarize(findings: list[dict]) -> list[dict]:
    """One row per rule: where it fired (flavor/transport/mode/version) and one example."""
    rows = {}
    for f in findings:
        row = rows.setdefault(f["rule"], {"rule": f["rule"], "severity": f["severity"], "n": 0, "where": set(),
                                          "example": f["message"]})
        row["n"] += 1
        v = f.get("versions") or {}
        row["where"].add(f"{f['flavor']}/{f['transport']}/{f['mode']} mcp={v.get('mcp')} fastmcp={v.get('fastmcp')}"
                         if f["flavor"] == "fastmcp" else f"{f['flavor']}/{f['transport']}/{f['mode']} mcp={v.get('mcp')}")
    return sorted(({**x, "where": sorted(x["where"])} for x in rows.values()),
                  key=lambda x: ({"high": 0, "medium": 1, "low": 2}[x["severity"]], x["rule"]))


def main(argv=None) -> int:
    """Re-score saved runs with the current checks and print one row per rule."""
    findings = []
    for path in argv or sys.argv[1:]:
        with (gzip.open(path, "rt") if path.endswith(".gz") else open(path)) as fh:  # results/ keeps them gzipped
            runs = json.load(fh)["results"]
        findings += [f for res in runs if "error" not in res for f in run_all(res)]
    for row in summarize(findings):
        print(f"{row['severity']:6} {row['rule']:36} x{row['n']:<4} {row['example'][:110]}")
        for w in row["where"][:6]:
            print(f"{'':45}{w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
