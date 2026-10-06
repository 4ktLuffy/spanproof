"""The checks. Each takes one worker result and returns findings.

A finding is a dict:
  rule      stable identifier of the rule that fired (e.g. "usage.missing", "structure.orphan")
  check     usage | lifecycle | structure | aggregation | conventions | privacy | identity | errors
  severity  high (wrong number or lost data) | medium (missing data) | low (hygiene)
  scenario, call, attribute, expected, actual, message
"""

from __future__ import annotations

import json

from . import conventions as cv

AGENT_OPS = {"gen_ai.invoke_agent", "gen_ai.execute_tool", "gen_ai.handoff", "gen_ai.create_agent",
             "gen_ai.pipeline", "gen_ai.run"}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_client_span(s: dict) -> bool:
    op = s.get("op") or ""
    if s.get("data", {}).get("gen_ai.operation.type") == "ai_client":
        return True
    return op.startswith("gen_ai.") and op not in AGENT_OPS


def _f(r, rule, severity, message, call=None, attribute=None, expected=None, actual=None):
    return {"rule": rule, "check": rule.split(".")[0], "severity": severity, "scenario": r["scenario"],
            "integration": r["integration"], "call": call, "attribute": attribute, "expected": expected,
            "actual": actual, "message": message}


def ancestors(s: dict, by_id: dict) -> tuple[list[dict], bool]:
    """Ancestors of a span, nearest first, and whether the parent chain loops."""
    seen = {s["span_id"]}
    out = []
    p = s.get("parent_span_id")
    while p in by_id:
        if p in seen:
            return out, True
        seen.add(p)
        s = by_id[p]
        out.append(s)
        p = s.get("parent_span_id")
    return out, False


def descendants(s: dict, children: dict) -> list[dict]:
    seen = {s["span_id"]}
    stack, acc = list(children.get(s["span_id"], [])), []
    while stack:
        x = stack.pop()
        if x["span_id"] in seen:
            continue
        seen.add(x["span_id"])
        acc.append(x)
        stack.extend(children.get(x["span_id"], []))
    return acc


def match_calls(r: dict) -> list[tuple[int, dict | None]]:
    """Pair each provider call with the client span that reports it.

    First pass: exact response-id matches are reserved for their call. Second pass:
    remaining calls take remaining spans in start order, skipping any span whose response
    id belongs to a different call. A span with an unknown id can still match, so the
    identity check can report the wrong id. A call with no compatible span stays unmatched.
    """
    spans = sorted([s for s in r["spans"] if is_client_span(s)], key=lambda s: s.get("start") or 0)
    calls = r["calls"]
    rid = [(c["truth"] or {}).get("response_id") for c in calls]
    known = {x for x in rid if x}
    hits: list[dict | None] = [None] * len(calls)
    used = set()
    for i, want in enumerate(rid):
        if not want:
            continue
        for s in spans:
            if id(s) not in used and s["data"].get("gen_ai.response.id") == want:
                hits[i] = s
                used.add(id(s))
                break
    for i in range(len(calls)):
        if hits[i] is not None:
            continue
        for s in spans:
            sid = s["data"].get("gen_ai.response.id")
            if id(s) in used or (sid and sid in known and sid != rid[i]):  # it belongs to another call
                continue
            hits[i] = s
            used.add(id(s))
            break
    return list(enumerate(hits))


def _usage_num(data: dict, meaning: str):
    """(value, key) when the attribute is present and numeric, else None."""
    u = cv.read_usage(data).get(meaning)
    return u if u is not None and _num(u[0]) else None


# ------------------------------------------------------------------ 1. usage

def check_usage(r):
    out = []
    for i, span in match_calls(r):
        t = r["calls"][i]["truth"]
        if not t or span is None:
            continue
        got = cv.read_usage(span["data"])
        for meaning in ("input_tokens", "output_tokens", "total", "cached", "cache_write", "reasoning"):
            want = t[meaning]
            have = got.get(meaning)
            if have is None:
                if want:  # truth non-zero but nothing reported
                    out.append(_f(r, f"usage.missing.{meaning}",
                                  "high" if meaning in ("input_tokens", "output_tokens") else "medium",
                                  f"{meaning} not reported (provider said {want})", i, cv.USAGE_KEYS[meaning][0],
                                  want, None))
            elif not _num(have[0]) or have[0] != want:
                out.append(_f(r, f"usage.wrong.{meaning}", "high", f"{meaning} is {have[0]!r}, provider said {want}",
                              i, have[1], want, have[0]))
    return out


# -------------------------------------------------------------- 2. lifecycle

def check_lifecycle(r):
    """Every logical provider call must yield one finished gen_ai span.

    Logical calls come from the scenario (a client-side retry is several HTTP requests
    but one call), capped by the requests the server actually saw.
    """
    out = []
    n_calls = min(len(r["calls"]), len(r["requests"])) if r["calls"] else len(r["requests"])
    clients = [s for s in r["spans"] if is_client_span(s)]
    finished = [s for s in clients if s["finished"]]
    if len(finished) < n_calls:
        kinds = ",".join(sorted({"early-close" if not c["completes"] else "complete" for c in r["calls"]}))
        out.append(_f(r, "lifecycle.lost_span", "high",
                      f"{n_calls} provider call(s) but {len(finished)} finished gen_ai span(s) delivered "
                      f"({kinds}); the call is invisible in Sentry", None, None, n_calls, len(finished)))
    for i, span in match_calls(r):
        c = r["calls"][i]
        if span is None or c["completes"]:
            continue
        if r["exception"] and span.get("status") in (None, "ok"):
            out.append(_f(r, "lifecycle.status_ok_on_failure", "medium", "call failed but its span status is ok", i,
                          "status", "internal_error", span.get("status")))
    return out


# -------------------------------------------------------------- 3. structure

def check_structure(r):
    out = []
    spans = r["spans"]
    if not spans:
        return out
    traces = {s["trace_id"] for s in spans}
    if len(traces) > 1:
        out.append(_f(r, "structure.split_trace", "high", f"one run produced {len(traces)} traces", None, "trace_id",
                      1, len(traces)))
    ids = [s["span_id"] for s in spans]
    if len(ids) != len(set(ids)):
        out.append(_f(r, "structure.duplicate_span_id", "high", "two spans share a span_id", None, "span_id",
                      "unique", "duplicate"))
    by_id = {s["span_id"]: s for s in spans}
    for s in spans:
        if not s["is_root"] and s["parent_span_id"] not in by_id:
            out.append(_f(r, "structure.orphan", "high", f"span {s['op']} has a parent that was never sent", None,
                          "parent_span_id", "present", s["parent_span_id"]))
        if ancestors(s, by_id)[1]:
            out.append(_f(r, "structure.cycle", "high", f"span {s['op']} is its own ancestor (parent cycle)", None,
                          "parent_span_id", "acyclic", "cycle"))
    if r.get("agent"):
        agents = [s for s in spans if s["op"] == "gen_ai.invoke_agent"]
        if not agents:
            out.append(_f(r, "structure.no_agent_span", "high", "agent run produced no gen_ai.invoke_agent span", None,
                          "op", "gen_ai.invoke_agent", None))

        def under_agent(s):
            return any(a["op"] == "gen_ai.invoke_agent" for a in ancestors(s, by_id)[0])

        for s in spans:
            if is_client_span(s) and agents and not under_agent(s):
                out.append(_f(r, "structure.llm_outside_agent", "high",
                              f"LLM span {s['op']} is not inside the agent span", None, "parent_span_id",
                              "gen_ai.invoke_agent ancestor", "none"))
        tools = [s for s in spans if s["op"] == "gen_ai.execute_tool"]
        names = [s["data"].get("gen_ai.tool.name") for s in tools]
        for t in r["agent"].get("tools", []):
            if t not in names:
                out.append(_f(r, "structure.missing_tool_span", "high", f"tool {t} ran but has no "
                              "gen_ai.execute_tool span", None, "gen_ai.tool.name", t, names))
        for s in tools:
            if not under_agent(s):
                out.append(_f(r, "structure.tool_outside_agent", "medium", "tool span not inside the agent span",
                              None, "parent_span_id", "gen_ai.invoke_agent ancestor", "none"))
    return out


# ------------------------------------------------------------ 4. aggregation

def check_aggregation(r):
    """Would a trace-level sum of token attributes equal what was billed?

    Two queries: the naive one (sum over every span that carries the attribute, which
    is what a dashboard sum() does) and the one the conventions recommend (filter to
    gen_ai.operation.type == ai_client). Both are compared with the provider's numbers.
    Non-numeric values are left to the usage and conventions checks.
    """
    out = []
    truths = [c["truth"] for c in r["calls"] if c["truth"]]
    if truths:
        for meaning in ("input_tokens", "output_tokens", "cached", "reasoning"):
            billed = sum(t[meaning] for t in truths)
            naive = filtered = 0
            any_attr = False
            for s in r["spans"]:
                u = _usage_num(s["data"], meaning)
                if u is None:
                    continue
                any_attr = True
                naive += u[0]
                if s["data"].get("gen_ai.operation.type") == "ai_client" or (
                        "gen_ai.operation.type" not in s["data"] and is_client_span(s)):
                    filtered += u[0]
            if not any_attr:
                continue
            if naive > billed:
                ratio = f" (x{naive / billed:.2f})" if billed else ""
                out.append(_f(r, f"aggregation.double_count.{meaning}", "high",
                              f"sum({meaning}) over the trace is {naive}, billed {billed}{ratio}; "
                              "hierarchical spans double count", None, meaning, billed, naive))
            if filtered != billed and naive != filtered:
                out.append(_f(r, f"aggregation.filtered_mismatch.{meaning}", "medium",
                              f"recommended filter (operation.type=ai_client) gives {filtered}, billed {billed}",
                              None, meaning, billed, filtered))
    out.extend(check_rollup(r))
    return out


def check_rollup(r):
    """An agent span's usage should equal the sum of the LLM calls made inside it."""
    out = []
    spans = r["spans"]
    children: dict = {}
    for s in spans:
        children.setdefault(s.get("parent_span_id"), []).append(s)
    for a in spans:
        if a.get("op") != "gen_ai.invoke_agent":
            continue
        inner = [d for d in descendants(a, children) if is_client_span(d)]
        for meaning in ("input_tokens", "output_tokens", "cached"):
            au = _usage_num(a["data"], meaning)
            if au is None:
                continue
            vals = [cv.read_usage(d["data"]).get(meaning) for d in inner]
            if any(v is not None and not _num(v[0]) for v in vals):
                continue  # a child value is malformed; the usage/conventions checks report it
            want = sum(v[0] for v in vals if v is not None)
            if au[0] != want:
                name = a["data"].get("gen_ai.agent.name") or a.get("description")
                out.append(_f(r, f"aggregation.rollup.{meaning}", "high",
                              f"agent '{name}' reports {meaning}={au[0]} but the LLM calls inside it sum to {want}",
                              None, au[1], want, au[0]))
    return out


# ------------------------------------------------------------ 5. conventions

def check_conventions(r):
    out = []
    seen_key, seen_violation = set(), set()
    for s in r["spans"]:
        for k, v in s["data"].items():
            if not k.startswith("gen_ai."):
                continue
            d = cv.lookup(k)
            if (k, s["op"]) not in seen_key:  # registration and deprecation are per key
                seen_key.add((k, s["op"]))
                if d is None:
                    out.append(_f(r, "conventions.unregistered", "low", f"{k} is not in sentry-conventions", None, k,
                                  "registered", "unregistered"))
                else:
                    dep = cv.deprecation(k)
                    if dep:
                        out.append(_f(r, "conventions.deprecated", "low",
                                      f"{k} is deprecated; use {dep.get('replacement')}", None, k,
                                      dep.get("replacement"), k))
            if d is not None and cv.type_ok(k, v) is False:  # types are checked on every value
                tag = (k, s["op"], type(v).__name__)
                if tag not in seen_violation:
                    seen_violation.add(tag)
                    out.append(_f(r, "conventions.type", "medium",
                                  f"{k} should be {d.get('type')}, got {type(v).__name__}", None, k, d.get("type"),
                                  type(v).__name__))
        u = {k: v for k, v in cv.read_usage(s["data"]).items() if _num(v[0])}
        if "cached" in u and "input_tokens" in u and u["cached"][0] > u["input_tokens"][0]:
            out.append(_f(r, "conventions.cached_exceeds_input", "high",
                          "cached tokens exceed input tokens (cached must be a subset)", None, u["cached"][1],
                          f"<= {u['input_tokens'][0]}", u["cached"][0]))
        if "reasoning" in u and "output_tokens" in u and u["reasoning"][0] > u["output_tokens"][0]:
            out.append(_f(r, "conventions.reasoning_exceeds_output", "high", "reasoning tokens exceed output tokens",
                          None, u["reasoning"][1], f"<= {u['output_tokens'][0]}", u["reasoning"][0]))
        if "total" in u and "input_tokens" in u and "output_tokens" in u:
            if u["total"][0] != u["input_tokens"][0] + u["output_tokens"][0]:
                out.append(_f(r, "conventions.total_mismatch", "medium", "total_tokens != input_tokens + "
                              "output_tokens", None, u["total"][1], u["input_tokens"][0] + u["output_tokens"][0],
                              u["total"][0]))
    return out


# --------------------------------------------------------------- 6. privacy

def check_privacy(r):
    out = []
    content = sorted({k for s in r["spans"] for k in s["data"] if k in cv.CONTENT_KEYS})
    if not r["data_collection"] and content:
        out.append(_f(r, "privacy.content_leak", "high",
                      f"content recorded with data collection off: {', '.join(content)}", None, content[0], "absent",
                      "present"))
    if r["data_collection"]:
        clients = [s for s in r["spans"] if is_client_span(s) and s["finished"]]
        for s in clients:
            if not any(k in s["data"] for k in ("gen_ai.input.messages", "gen_ai.request.messages", "gen_ai.prompt",
                                                "gen_ai.embeddings.input")):
                out.append(_f(r, "privacy.no_input_recorded", "low",
                              f"{s['op']}: data collection on but no input messages recorded", None,
                              "gen_ai.input.messages", "present", "absent"))
                break
    return out


# -------------------------------------------------------------- 7. identity

def finish_values(v) -> set[str]:
    """Normalize finish reasons: list, JSON string, Python-repr string or scalar."""
    if v is None:
        return set()
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            import ast

            try:
                v = ast.literal_eval(v)
            except (ValueError, SyntaxError):
                v = [v]
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        v = [v]
    return {str(x).strip().lower() for x in v if str(x).strip()}


def check_identity(r):
    out = []
    for i, span in match_calls(r):
        t = r["calls"][i]["truth"]
        if not t or span is None:
            continue
        d = span["data"]
        model = d.get("gen_ai.response.model")
        if t.get("model"):
            if model in (None, ""):
                out.append(_f(r, "identity.model_missing", "low", "response model not recorded", i,
                              "gen_ai.response.model", t["model"], None))
            elif model != t["model"]:
                out.append(_f(r, "identity.model_wrong", "medium",
                              "response model differs from what the provider returned", i, "gen_ai.response.model",
                              t["model"], model))
        if t.get("finish"):
            raw = d.get("gen_ai.response.finish_reasons", d.get("gen_ai.response.finish_reason"))
            got = finish_values(raw)
            if not got:
                out.append(_f(r, "identity.finish_missing", "medium",
                              "finish reason not recorded (truncation and tool-stop are invisible)", i,
                              "gen_ai.response.finish_reasons", t["finish"], None))
            elif t["finish"].lower() not in got:
                out.append(_f(r, "identity.finish_wrong", "high", f"finish reason {sorted(got)} differs from the "
                              f"provider's {t['finish']!r}", i, "gen_ai.response.finish_reasons", t["finish"], raw))
        if t.get("response_id"):
            rid = d.get("gen_ai.response.id")
            if rid in (None, ""):
                out.append(_f(r, "identity.response_id_missing", "low",
                              "provider response id not recorded (cannot join with provider logs)", i,
                              "gen_ai.response.id", t["response_id"], None))
            elif rid != t["response_id"]:
                out.append(_f(r, "identity.response_id_wrong", "high", "response id differs from the provider's", i,
                              "gen_ai.response.id", t["response_id"], rid))
    return out


# ---------------------------------------------------------------- 8. errors

def check_errors(r):
    out = []
    if r["expects_exception"] and not r["exception"]:
        out.append(_f(r, "errors.expected_raise", "low", "scenario expected the client to raise but it did not",
                      None, None, "exception", None))
    if r["exception"] and not r["expects_exception"]:
        out.append(_f(r, "errors.unexpected_raise", "high",
                      f"client raised {r['exception']['type']}: {r['exception']['value']}", None, None, None,
                      r["exception"]["type"]))
    if r["expects_exception"] and r["exception"] and not r["errors"]:
        out.append(_f(r, "errors.not_captured", "low", "provider failure was not captured as a Sentry error event",
                      None, None, "error event", None))
    return out


ALL = [check_usage, check_lifecycle, check_structure, check_aggregation, check_conventions, check_privacy,
       check_identity, check_errors]


def run_all(r: dict) -> list[dict]:
    out = []
    for chk in ALL:
        out.extend(chk(r))
    return out
