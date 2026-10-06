"""Agent failure-class detectors over a trace's spans.

They read only what Sentry already receives in production (gen_ai.* and http.client
spans), so they can run on live traces: no ground truth, no extra instrumentation.
Each detection becomes an issue-like record with a stable fingerprint, so repeats of
the same failure group together the way Sentry groups errors.

Input: a list of span dicts with keys op, status, description, data, span_id,
parent_span_id, start, trace_id (the shape SpanProof's capture produces, which maps
1:1 from Sentry's span JSON).
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field

LLM_PATH = re.compile(r"(/chat/completions|/responses\b|/v1/messages|:generateContent|:streamGenerateContent"
                      r"|/v1/completions|/embeddings)")
ERROR_TEXT = [
    re.compile(r'^\s*\{\s*"error"\s*:', re.I),
    re.compile(r"\berror occurred\b", re.I),
    re.compile(r"\btraceback \(most recent call last\)", re.I),
    re.compile(r"\b(exception|errno)\b", re.I),
    re.compile(r"\b(5\d\d|429)\b[^\n]{0,40}\b(unavailable|error|timeout|overloaded)\b", re.I),
    re.compile(r"\btimed? ?out\b", re.I),
    re.compile(r"^\s*[A-Z][A-Za-z]*(Error|Exception|Timeout)\b\s*:"),
    re.compile(r"\b(unreachable|refused|disconnected)\b", re.I),
]
BENIGN = re.compile(r"\bno (errors?|exceptions?|issues?) (found|detected)\b", re.I)
TOOL_STOP = {"tool_calls", "tool_use", "function_call", "tool-calls", "tool_call"}
LENGTH_STOP = {"length", "max_tokens", "max_output_tokens"}
# The final answer tells the user the step failed: then the failure is not silent.
ACKNOWLEDGED = re.compile(r"\b(timed? ?out|failed|unable|could ?n[o']t|cannot|can't|unavailable|did not respond|"
                          r"error|sorry|try again|retry later)\b", re.I)
CLIENT_OPS = {"gen_ai.chat", "gen_ai.responses", "gen_ai.text_completion", "gen_ai.generate_content",
              "gen_ai.embeddings", "gen_ai.completion"}


@dataclass
class Detection:
    kind: str
    title: str
    trace_id: str
    agent: str | None
    evidence: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}:{self.agent or '-'}:{self.detail.get('tool', '-')}"


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, TypeError):
        return False


def _structured_error(text: str) -> bool:
    """A JSON tool result that says it failed: an "error" value, ok/success false, or status error."""
    try:
        j = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(j, dict):
        return False
    if j.get("error") not in (None, False, "", {}, []):
        return True
    if j.get("ok") is False or j.get("success") is False:
        return True
    return str(j.get("status", "")).lower() in ("error", "failed", "failure")


def _parse_listish(v: str):
    """finish_reasons arrive as JSON ('["stop"]'), Python repr ("['stop']") or a bare string."""
    import ast

    try:
        return json.loads(v)
    except ValueError:
        pass
    try:
        out = ast.literal_eval(v)
        if isinstance(out, (list, tuple)):
            return list(out)
    except (ValueError, SyntaxError):
        pass
    return [v]


def _texts(v) -> str:
    """All human-readable text inside an output value (JSON strings, message lists, parts)."""
    if v is None:
        return ""
    if isinstance(v, str):
        try:
            j = json.loads(v)
        except ValueError:
            return v
        if isinstance(j, str):
            return j
        v = j
    if isinstance(v, dict):
        keys = [k for k in ("content", "text", "parts") if k in v]
        if not keys:  # an application's own JSON answer, not a message wrapper
            return json.dumps(v)
        return " ".join(_texts(v[k]) for k in keys)
    if isinstance(v, (list, tuple)):
        return " ".join(_texts(x) for x in v)
    return str(v)  # numbers and booleans are answers too


def _is_client(s):
    return s.get("op") in CLIENT_OPS or s.get("data", {}).get("gen_ai.operation.type") == "ai_client"


def _status_code(s):
    d = s.get("data", {})
    v = d.get("http.response.status_code") or d.get("http.status_code")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _finish(s):
    v = s.get("data", {}).get("gen_ai.response.finish_reasons")
    if v is None:
        v = s.get("data", {}).get("gen_ai.response.finish_reason")
    if isinstance(v, str):
        v = _parse_listish(v)
    if isinstance(v, str):
        v = [v]
    return {str(x).lower() for x in (v or [])}


def _tool_text(s):
    d = s.get("data", {})
    for k in ("gen_ai.tool.call.result", "gen_ai.tool.output"):
        if k in d and d[k] is not None:
            v = d[k]
            if isinstance(v, str):
                try:  # langchain wraps outputs as a serialized message
                    j = json.loads(v)
                    if isinstance(j, dict) and "content" in j and isinstance(j["content"], str):
                        return j["content"]
                except ValueError:
                    pass
                return v
            return json.dumps(v)
    return None


# Proposed attribute (not emitted by any SDK today): a salted hash of the tool-call
# arguments, safe to send with data collection off. Equal hashes mean equal arguments,
# which is all loop detection needs. See the report's "privacy-preserving fingerprint".
ARGS_HASH = "gen_ai.tool.call.arguments_hash"
# Proposed attribute: the character count of the response text, also safe with data collection
# off. It lets "empty answer" be decided without seeing the answer.
OUTPUT_CHARS = "gen_ai.response.text_length"


def _tool_args(s):
    d = s.get("data", {})
    if d.get(ARGS_HASH):
        return "sha256:" + str(d[ARGS_HASH])
    for k in ("gen_ai.tool.call.arguments", "gen_ai.tool.input"):
        if k in d and d[k] is not None:
            v = d[k]
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    return v.strip()
            return json.dumps(v, sort_keys=True)
    return None


def _output_text(s):
    d = s.get("data", {})
    for k in ("gen_ai.output.messages", "gen_ai.response.text"):
        if k in d:
            return d[k]
    return None


def _runs(spans):
    """Yield (agent_name, agent_span_or_None, spans_in_run) per agent run, outermost runs only."""
    by_parent = {}
    for s in spans:
        by_parent.setdefault(s.get("parent_span_id"), []).append(s)

    def desc(root):
        seen = {root["span_id"]}
        out, stack = [], list(by_parent.get(root["span_id"], []))
        while stack:
            x = stack.pop()
            if x["span_id"] in seen:
                continue
            seen.add(x["span_id"])
            out.append(x)
            stack.extend(by_parent.get(x["span_id"], []))
        return out

    agents = [s for s in spans if s.get("op") == "gen_ai.invoke_agent"]
    ids = {a["span_id"] for a in agents}
    by_id = {s["span_id"]: s for s in spans}

    def has_agent_ancestor(s):
        p, seen = s.get("parent_span_id"), {s["span_id"]}
        while p in by_id and p not in seen:
            if p in ids:
                return True
            seen.add(p)
            p = by_id[p].get("parent_span_id")
        return False

    outer = [a for a in agents if not has_agent_ancestor(a)]
    if not outer:
        yield None, None, spans
        return
    for a in outer:
        yield a.get("data", {}).get("gen_ai.agent.name") or a.get("description"), a, desc(a)


def _scope(s, by_id, agent_ids):
    """The nearest enclosing agent span of a span (its own agent's scope)."""
    p, seen = s.get("parent_span_id"), {s["span_id"]}
    while p in by_id and p not in seen:
        if p in agent_ids:
            return p
        seen.add(p)
        p = by_id[p].get("parent_span_id")
    return None


def _ordered(xs):
    return sorted(xs, key=lambda s: s.get("start") or 0)


def detect_trace(spans: list[dict]) -> list[Detection]:
    out: list[Detection] = []
    if not spans:
        return out
    trace = spans[0].get("trace_id") or "-"

    # ---- trace-wide: provider calls without gen_ai spans; retry storms
    http = [s for s in spans if (s.get("op") or "").startswith("http.client")
            and LLM_PATH.search(s.get("description") or "")]
    ok_http = [s for s in http if (_status_code(s) or 200) < 400]
    clients = [s for s in spans if _is_client(s)]
    if len(ok_http) > len(clients):
        out.append(Detection("lost_llm_span",
                             f"{len(ok_http) - len(clients)} provider call(s) have no gen_ai span",
                             trace, None, [s["span_id"] for s in ok_http],
                             {"provider_calls": len(ok_http), "gen_ai_spans": len(clients)}))
    streak, best, ev = 0, 0, []
    for s in _ordered(http):
        code = _status_code(s)
        if code is not None and (code >= 500 or code == 429):
            streak += 1
            ev.append(s["span_id"])
            best = max(best, streak)
        else:
            streak = 0
    if best >= 3:
        out.append(Detection("retry_storm", f"{best} consecutive failed provider attempts", trace, None, ev,
                             {"attempts": best}))

    by_id = {s["span_id"]: s for s in spans}
    agent_ids = {s["span_id"] for s in spans if s.get("op") == "gen_ai.invoke_agent"}

    # ---- per agent run
    for agent, a, run in _runs(spans):
        tools = _ordered([s for s in run if s.get("op") == "gen_ai.execute_tool"])
        chats = _ordered([s for s in run if _is_client(s)])

        # tool loop: same tool and same arguments three times or more, within one agent's own
        # scope (two nested agents each calling a tool twice is not a loop)
        counts: dict = {}
        for t in tools:
            name = t.get("data", {}).get("gen_ai.tool.name") or t.get("description")
            key = (_scope(t, by_id, agent_ids), name, _tool_args(t))
            counts.setdefault(key, []).append(t["span_id"])
        for (_, name, args), ids in counts.items():
            if args is not None and len(ids) >= 3:
                out.append(Detection("tool_loop", f"tool {name} called {len(ids)}x with identical arguments",
                                     trace, agent, ids, {"tool": name, "calls": len(ids)}))
            # With no arguments recorded (data collection off) a loop is indistinguishable from
            # pagination; measured precision of a name-only rule was 0.14, so we do not guess.

        # silent tool error: a tool failed, yet the run went on and answered
        final = chats[-1] if chats else None
        # The run's answer: the last LLM call's output, else the agent span's own output.
        answer_raw = _output_text(final) if final is not None else None
        if answer_raw is None and a is not None:
            answer_raw = _output_text(a)
        for t in tools:
            text = _tool_text(t) or ""
            if t.get("status") not in (None, "ok"):
                failed = True
            elif _is_json_object(text):
                failed = _structured_error(text)  # structured results speak for themselves
            else:
                failed = any(p.search(text) for p in ERROR_TEXT) and not BENIGN.search(text)
            if not failed:
                continue
            answered_after = final is not None and (final.get("start") or 0) > (t.get("start") or 0) and \
                not (_finish(final) & TOOL_STOP)
            run_ok = a is None or a.get("status") in (None, "ok")
            # Without the answer's text we cannot tell a silent failure from an acknowledged one.
            if answered_after and run_ok and answer_raw is not None and not ACKNOWLEDGED.search(_texts(answer_raw)):
                name = t.get("data", {}).get("gen_ai.tool.name") or t.get("description")
                out.append(Detection("silent_tool_error",
                                     f"tool {name} failed but the agent answered as if it succeeded", trace,
                                     agent, [t["span_id"], final["span_id"]], {"tool": name}))
                break

        if final is not None:
            fin = _finish(final)
            if fin & TOOL_STOP or (a is not None and a.get("status") not in (None, "ok") and tools and
                                   (tools[-1].get("start") or 0) > (final.get("start") or 0)):
                out.append(Detection("dead_end", "run stopped after a tool call without a final answer", trace,
                                     agent, [final["span_id"]], {"finish": sorted(fin)}))
            elif fin & LENGTH_STOP:
                out.append(Detection("truncated_answer", "final answer cut off by the token limit", trace, agent,
                                     [final["span_id"]], {"finish": sorted(fin)}))
            else:
                # Missing content is unknown, not empty; a token count alone proves nothing.
                chars = final.get("data", {}).get(OUTPUT_CHARS)
                if chars is None and a is not None:
                    chars = a.get("data", {}).get(OUTPUT_CHARS)
                empty = (not _texts(answer_raw).strip()) if answer_raw is not None else (
                    isinstance(chars, int) and chars == 0)
                if empty:
                    out.append(Detection("empty_answer", "final answer is empty", trace, agent, [final["span_id"]]))
    return out


def run_tokens(spans: list[dict]) -> int:
    """Billed input+output tokens of a trace, counting only LLM-call spans (no double count)."""
    from .conventions import read_usage

    tot = 0
    for s in spans:
        if _is_client(s):
            u = read_usage(s.get("data", {}))
            for meaning in ("input_tokens", "output_tokens"):
                v = u.get(meaning, (None,))[0]
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    tot += v
    return tot


def detect_cost_spikes(traces: list[list[dict]], factor: float = 5.0, min_peers: int = 5) -> dict[int, Detection]:
    """Corpus-level: runs of the same agent whose tokens exceed factor x the peer median."""
    by_agent: dict = {}
    for i, sp in enumerate(traces):
        agent = next((s.get("data", {}).get("gen_ai.agent.name") for s in sp if s.get("op") == "gen_ai.invoke_agent"),
                     None)
        by_agent.setdefault(agent, []).append((i, run_tokens(sp)))
    out = {}
    for agent, rows in by_agent.items():
        if len(rows) < min_peers:
            continue
        for i, tok in rows:
            peers = [t for j, t in rows if j != i and t > 0]
            if len(peers) < min_peers - 1:
                continue
            med = statistics.median(peers)
            if med > 0 and tok > factor * med:
                out[i] = Detection("cost_spike", f"run used {tok} tokens, {tok / med:.1f}x the median {med:.0f}",
                                   traces[i][0].get("trace_id") or "-", agent, [],
                                   {"tokens": tok, "median": med})
    return out
