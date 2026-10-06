"""Compare a run sent to a real Sentry project with what Sentry stored.

    SPANPROOF real run:  SPANPROOF_DSN=... python -m spanproof.runner ... --out results/real.json
    then:                python -m spanproof.real_compare results/real.json [--wait 600]

For every scenario run it reads the trace back through Sentry's span search API and
reports three things per LLM call:
  sent     what the SDK put on the wire (recorded locally while sending)
  stored   what Sentry kept after ingestion (Relay normalization, cost calculation)
  truth    what the provider reported
plus trace-level cost: the sum over every span (what a dashboard sum shows) versus the
sum over LLM-call spans only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict

from . import sentry_api as api
from .oracles import is_client_span, match_calls

USAGE_FIELDS = ["gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens",
                "gen_ai.usage.input_tokens.cached", "gen_ai.usage.input_tokens.cache_write",
                "gen_ai.usage.output_tokens.reasoning", "gen_ai.usage.cache_read.input_tokens",
                "gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.reasoning.output_tokens"]
COST_FIELDS = ["gen_ai.cost.total_tokens", "gen_ai.cost.input_tokens", "gen_ai.cost.output_tokens"]
OTHER = ["id", "span.op", "span.description", "parent_span", "gen_ai.response.model", "gen_ai.request.model",
         "gen_ai.operation.type", "gen_ai.agent.name", "gen_ai.response.finish_reasons", "gen_ai.request.messages",
         "gen_ai.input.messages", "gen_ai.response.text", "gen_ai.output.messages"]
FIELDS = OTHER + USAGE_FIELDS + COST_FIELDS
CONTENT = ["gen_ai.request.messages", "gen_ai.input.messages", "gen_ai.response.text", "gen_ai.output.messages"]


def trace_of(r: dict) -> str | None:
    roots = [s for s in r.get("spans", []) if s.get("is_root")]
    return (roots or r.get("spans") or [{}])[0].get("trace_id")


def fetch(trace: str, expected: int, wait: int) -> list[dict]:
    deadline = time.time() + wait
    while True:
        rows = api.spans(f"trace:{trace}", FIELDS, stats_period="7d")
        if len(rows) >= expected or time.time() > deadline:
            return rows
        time.sleep(10)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--wait", type=int, default=300, help="seconds to wait for a trace to be fully ingested")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    d = json.load(open(a.results))
    out = []
    for r in d["results"]:
        if r.get("crashed") or r.get("unsupported"):
            continue
        tr = trace_of(r)
        if not tr:
            continue
        sent = [s for s in r["spans"]]
        stored = fetch(tr, len(sent), a.wait)
        by_id = {x["id"]: x for x in stored}
        row = {"scenario": r["scenario"], "mode": r.get("mode"), "trace": tr, "sent_spans": len(sent),
               "stored_spans": len(stored), "calls": []}
        for i, span in match_calls(r):
            t = r["calls"][i]["truth"]
            if span is None:
                row["calls"].append({"call": i, "truth": t, "sent": None, "stored": None})
                continue
            st = by_id.get(span["span_id"])
            row["calls"].append({"call": i, "truth": t, "sent": {k: v for k, v in span["data"].items()
                                                                if k.startswith("gen_ai.usage")
                                                                or k == "gen_ai.response.model"},
                                 "stored": st})
        clients = [x for x in stored if (x.get("span.op") or "").startswith("gen_ai.")
                   and x.get("span.op") not in ("gen_ai.invoke_agent", "gen_ai.execute_tool", "gen_ai.create_agent",
                                                "gen_ai.handoff")]
        row["cost_all_spans"] = sum(x.get("gen_ai.cost.total_tokens") or 0 for x in stored)
        row["cost_llm_spans"] = sum(x.get("gen_ai.cost.total_tokens") or 0 for x in clients)
        row["content_stored"] = sorted({k for x in stored for k in CONTENT if x.get(k)})
        row["data_collection"] = r.get("data_collection")
        out.append(row)
        print(f"{r['scenario']:46} {r.get('mode'):7} sent {len(sent):2} stored {len(stored):2} "
              f"cost all={row['cost_all_spans']:.6f} llm={row['cost_llm_spans']:.6f}", flush=True)
    path = a.out or a.results.replace(".json", "_compare.json")
    json.dump(out, open(path, "w"), indent=1, default=str)
    print("->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
