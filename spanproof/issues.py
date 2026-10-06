"""Turn agent failure detections into Sentry issues.

    python -m spanproof.issues traces.jsonl [--format spanproof|sentry|otlp] [--dsn DSN] [--dry-run]

Input formats:
  spanproof  one SpanProof result per line (what the corpus and runner write)
  sentry     one JSON list of spans per line, as Sentry's span JSON (attributes or data)
  otlp       one OTLP/JSON export per line (resourceSpans -> scopeSpans -> spans); any OpenTelemetry
             GenAI exporter works, so the detectors are not tied to Sentry's SDKs

Each detection becomes an event whose fingerprint is (failure class, agent, tool), so
the 50th tool loop of the same agent lands in the same issue as the first, the way
errors group today. With --dry-run nothing is sent; the would-be issues are printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from .detectors import Detection, detect_cost_spikes, detect_trace

TITLES = {
    "tool_loop": "Agent stuck in a tool loop",
    "retry_storm": "LLM provider retry storm",
    "silent_tool_error": "Tool failed silently; agent answered anyway",
    "lost_llm_span": "LLM call missing from the trace",
    "dead_end": "Agent stopped without a final answer",
    "truncated_answer": "Agent answer truncated by the token limit",
    "empty_answer": "Agent returned an empty answer",
    "cost_spike": "Agent run cost spike",
}


def _attr_value(v):
    if isinstance(v, dict):
        for k in ("value", "stringValue", "intValue", "doubleValue", "boolValue"):
            if k in v:
                x = v[k]
                return int(x) if k == "intValue" else x
        if "arrayValue" in v:
            return [_attr_value(i) for i in v["arrayValue"].get("values", [])]
    return v


def from_sentry(spans: list[dict]) -> list[dict]:
    out = []
    for s in spans:
        data = dict(s.get("data") or {})
        for k, v in (s.get("attributes") or {}).items():
            data[k] = _attr_value(v)
        st = s.get("status")
        out.append({"trace_id": s.get("trace_id"), "span_id": s.get("span_id"),
                    "parent_span_id": s.get("parent_span_id"), "op": s.get("op") or data.get("sentry.op"),
                    "description": s.get("description") or s.get("name"),
                    "status": "ok" if st in (None, "ok", "unset") else str(st),
                    "start": s.get("start_timestamp"), "data": data})
    return out


def from_otlp(doc: dict) -> list[dict]:
    """OTLP/JSON -> span dicts. The op is derived from gen_ai.operation.name, as Sentry does."""
    out = []
    ops = {"chat": "gen_ai.chat", "invoke_agent": "gen_ai.invoke_agent", "execute_tool": "gen_ai.execute_tool",
           "generate_content": "gen_ai.generate_content", "text_completion": "gen_ai.text_completion",
           "embeddings": "gen_ai.embeddings"}
    for rs in doc.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                data = {a["key"]: _attr_value(a.get("value")) for a in s.get("attributes", [])}
                op = ops.get(data.get("gen_ai.operation.name"))
                if op is None and ("http.request.method" in data or "http.method" in data):
                    op = "http.client"
                desc = s.get("name")
                if op == "http.client":
                    url = data.get("url.full") or data.get("http.url") or ""
                    desc = f"{data.get('http.request.method', data.get('http.method', ''))} {url}".strip()
                code = (s.get("status") or {}).get("code")
                out.append({"trace_id": s.get("traceId"), "span_id": s.get("spanId"),
                            "parent_span_id": s.get("parentSpanId") or None, "op": op, "description": desc,
                            "status": "internal_error" if code in (2, "STATUS_CODE_ERROR") else "ok",
                            "start": int(s.get("startTimeUnixNano", 0)) / 1e9, "data": data})
    return out


def load(path: str, fmt: str) -> list[list[dict]]:
    traces = []
    for line in open(path):
        if not line.strip():
            continue
        row = json.loads(line)
        if fmt == "spanproof":
            traces.append(row["spans"])
        elif fmt == "sentry":
            traces.append(from_sentry(row if isinstance(row, list) else row.get("spans", [])))
        else:
            traces.append(from_otlp(row))
    return traces


def to_event(d: Detection) -> dict:
    return {
        "level": "warning",
        "message": f"{TITLES[d.kind]}: {d.title}",
        "fingerprint": ["agent-failure", d.kind, d.agent or "-", str(d.detail.get("tool", "-"))],
        "tags": {"agent_failure.class": d.kind, "gen_ai.agent.name": d.agent or "unknown",
                 "gen_ai.tool.name": str(d.detail.get("tool", ""))},
        "contexts": {"agent_failure": {"trace_id": d.trace_id, "evidence_span_ids": d.evidence, **d.detail}},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("traces")
    ap.add_argument("--format", default="spanproof", choices=["spanproof", "sentry", "otlp"])
    ap.add_argument("--dsn")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    traces = load(a.traces, a.format)
    dets: list[Detection] = []
    for sp in traces:
        dets.extend(detect_trace(sp))
    dets.extend(detect_cost_spikes(traces).values())
    groups = Counter(tuple(to_event(d)["fingerprint"]) for d in dets)
    print(f"{len(traces)} traces -> {len(dets)} detections -> {len(groups)} issues")
    for fp, n in groups.most_common():
        print(f"  {n:4}x  {TITLES[fp[1]]}  (agent={fp[2]}, tool={fp[3]})")
    if a.dsn and not a.dry_run:
        import sentry_sdk

        sentry_sdk.init(dsn=a.dsn, default_integrations=False)
        for d in dets:
            sentry_sdk.capture_event(to_event(d))
        sentry_sdk.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
