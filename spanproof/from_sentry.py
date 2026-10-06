"""Fetch traces from a Sentry organization and turn them into detector input.

    python -m spanproof.from_sentry results/corpus.jsonl --out results/corpus_from_sentry.jsonl

For a corpus that was sent to a real project (SPANPROOF_DSN set), re-reads every trace
through Sentry's span search API, so the detectors are scored on what Sentry stored,
not on what the SDK sent.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import sentry_api as api

FIELDS = ["id", "parent_span", "span.op", "span.description", "span.status", "precise.start_ts", "is_transaction",
          "gen_ai.tool.name", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result", "gen_ai.tool.input",
          "gen_ai.tool.output", "gen_ai.response.finish_reasons", "gen_ai.response.text", "gen_ai.output.messages",
          "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.agent.name", "http.response.status_code",
          "gen_ai.operation.type", "trace"]
NUMERIC = {"gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "http.response.status_code"}


def to_spans(rows: list[dict]) -> list[dict]:
    out = []
    for x in rows:
        data = {}
        for k, v in x.items():
            if v is None or not (k.startswith("gen_ai.") or k.startswith("http.")):
                continue  # "" is kept: an empty answer is evidence
            if k in NUMERIC:
                try:
                    v = int(float(v))
                except (TypeError, ValueError):
                    pass
            data[k] = v
        status = x.get("span.status")
        out.append({"trace_id": x.get("trace"), "span_id": x["id"], "parent_span_id": x.get("parent_span") or None,
                    "op": x.get("span.op"), "description": x.get("span.description"),
                    "status": None if status in (None, "unknown") else status,
                    "start": float(x["precise.start_ts"]) if x.get("precise.start_ts") else None,
                    "is_root": str(x.get("is_transaction")).lower() == "true" and not x.get("parent_span"),
                    "data": data})
    return out


def fill_from_details(rows: list[dict], trace: str, project: str) -> int:
    """Workaround: list-valued attributes sent through the default gen_ai transport are stored
    but not returned by span search. Read finish reasons from each LLM span's detail view."""
    e = api.env()
    filled = 0
    for x in rows:
        if x.get("span.op") != "gen_ai.chat" or x.get("gen_ai.response.finish_reasons") is not None:
            continue
        d = api.get(f"projects/{e['SENTRY_ORG']}/{project}/trace-items/{x['id']}/",
                    [("trace_id", trace), ("item_type", "spans"), ("statsPeriod", "7d")])
        for attr in d.get("attributes", []):
            if attr.get("name") == "gen_ai.response.finish_reasons":
                x["gen_ai.response.finish_reasons"] = attr.get("value")
                filled += 1
    return filled


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--out", required=True)
    ap.add_argument("--details", action="store_true",
                    help="also read finish reasons from span details (works around the search gap)")
    ap.add_argument("--project", default="python")
    a = ap.parse_args(argv)
    filled_total = 0
    kept = missing = partial = 0
    with open(a.out, "w") as fh:
        for line in open(a.corpus):
            r = json.loads(line)
            if r.get("crashed"):
                continue
            tr = next((s["trace_id"] for s in r["spans"] if s.get("is_root")), None)
            rows = api.spans(f"trace:{tr}", FIELDS, stats_period="7d", per_page=100)
            if not rows:
                missing += 1
                continue
            partial += len(rows) < len(r["spans"])
            if a.details:
                filled_total += fill_from_details(rows, tr, a.project)
            kept += 1
            fh.write(json.dumps({"job": r["job"], "framework": r["framework"], "spans": to_spans(rows),
                                 "sent_spans": len(r["spans"]), "stored_spans": len(rows)}) + "\n")
    print(f"{kept} traces read back from Sentry ({partial} with fewer spans than sent), {missing} not found"
          + (f"; {filled_total} finish reasons recovered from span details" if a.details else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
