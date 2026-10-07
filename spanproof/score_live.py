"""Score the detectors on the live agent runs by what each agent actually did.

    python -m spanproof.score_live            # writes results/eval_live_behaviour.json

The fault injected into a run is not a reliable label: real models retried a failing tool,
gave up, or hit the step limit instead of failing the way the fault predicted. So truth here
comes from each run's own record, never from Sentry's spans:
  tool_loop         the run's tool log has the same call (same arguments) 3 or more times
  retry_storm       the provider fault was injected and the provider was really called
  dead_end          LangGraph's own step-limit message is the answer
  truncated/empty   the answer is empty; the last LLM call's finish reason says which
Runs that crashed loudly (an exception reached the app) are left out.

The spans scored are the ones read back from the real Sentry account
(results/live_from_sentry.jsonl), matched to runs by trace id. The synthetic traces read back
from the same account (results/eval_real1_from_sentry_details.json) are added for the total.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

from .detectors import detect_trace

R = Path(__file__).resolve().parent.parent / "results"
CLASSES = ["tool_loop", "retry_storm", "silent_tool_error", "dead_end", "truncated_answer", "empty_answer"]


def truth(run: dict) -> set:
    t = set()
    reps = collections.Counter(x["args"] for x in run["tool_log"])
    if reps and max(reps.values()) >= 3:
        t.add("tool_loop")
    if run["fault"] == "provider_500x3" and run["provider_calls"]:
        t.add("retry_storm")
    answer = run.get("answer")
    if answer is not None and "need more steps" in answer:
        t.add("dead_end")
    if answer == "":
        chats = [s for s in sorted(run["spans"], key=lambda s: s.get("start") or 0) if s["op"] == "gen_ai.chat"]
        finish = str(chats[-1]["data"].get("gen_ai.response.finish_reasons")) if chats else ""
        t.add("truncated_answer" if "length" in finish else "empty_answer")
    return t


def root_trace(spans: list[dict]) -> str | None:
    return next((s["trace_id"] for s in spans if s.get("is_root")), None) or (spans[0].get("trace_id") if spans else None)


def main() -> int:
    runs = [json.loads(line) for line in open(R / "live_agents1.jsonl")]
    stored = {root_trace(x["spans"]): x["spans"] for x in (json.loads(line) for line in open(R / "live_from_sentry.jsonl"))}
    scores = {c: {"tp": 0, "fp": 0, "fn": 0} for c in CLASSES}
    healthy = alarms = scored = loud = missing = 0
    for run in runs:
        if run["label"] == "loud_failure":
            loud += 1
            continue
        spans = stored.get(root_trace(run["spans"]))
        if spans is None:
            missing += 1
            continue
        scored += 1
        detected = {d.kind for d in detect_trace(spans)}
        want, got = truth(run), detected & set(CLASSES)
        for c in CLASSES:
            if c in want and c in got:
                scores[c]["tp"] += 1
            elif c in got:
                scores[c]["fp"] += 1
            elif c in want:
                scores[c]["fn"] += 1
        if not want:
            healthy += 1
            alarms += bool(detected)  # any alarm on a healthy run counts, not only the scored classes
    synthetic = json.load(open(R / "eval_real1_from_sentry_details.json"))["scores"]["_healthy_false_alarm_rate"]
    syn_healthy, syn_alarms = synthetic["n"], synthetic["k"]
    out = {"live_runs": len(runs), "loud_failures_left_out": loud, "not_found_in_sentry": missing, "scored": scored,
           "scores": scores, "live_healthy": healthy, "live_healthy_with_alarm": alarms,
           "synthetic_healthy_from_sentry": syn_healthy, "synthetic_healthy_with_alarm": syn_alarms,
           "healthy_total": healthy + syn_healthy, "healthy_with_alarm_total": alarms + syn_alarms}
    json.dump(out, open(R / "eval_live_behaviour.json", "w"), indent=1)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
