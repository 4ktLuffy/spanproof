"""Labelled agent-trace corpus for the failure-class detectors.

Each trace is produced by running a REAL agent framework (openai-agents,
langgraph, pydantic-ai) with REAL Sentry instrumentation against a scripted
OpenAI-compatible server. The script decides the model's behaviour (which tool
to call, when to fail, how many tokens it used), so the label is known by
construction, not by annotation.

    python -m spanproof.corpus --python <venv python> --n 6 --out results/corpus.jsonl

Labels (one primary class per trace, or "healthy"):
  tool_loop            same tool + same arguments called >= 3 times in one run
  retry_storm          >= 3 failed provider attempts behind one LLM call
  silent_tool_error    a tool failed but the run carried on as if it succeeded
  lost_llm_span        a provider request with no gen_ai span (instrumentation gap)
  dead_end             the run stopped after a tool call without a final answer
  truncated_answer     the final answer was cut by the token limit
  empty_answer         the final answer was empty
  cost_spike           the run used far more tokens than its peers (corpus-level)
Hard negatives (label "healthy") are built to look like failures:
  pagination (same tool, different arguments), one retry that succeeds, a tool
  whose output merely mentions the word "error", a long but complete answer, and
  a moderately larger (2x) run.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAMEWORKS = ["openai_agents", "langgraph", "pydantic_ai"]


def make_plan(label: str, rng: random.Random) -> dict:
    """A behaviour script: list of model steps plus tool behaviour."""
    base = rng.randint(700, 1300)
    steps: list[dict] = []
    tool_mode = {}
    hard = None
    if label == "healthy":
        variant = rng.choice(["plain", "one_tool", "two_tools", "pagination", "one_retry", "benign_error_word",
                              "long_answer", "bigger_run"])
        hard = variant
        if variant == "plain":
            steps = [{"final": "Paris is the capital."}]
        elif variant == "one_tool":
            steps = [{"tool": "get_weather", "args": {"city": "Paris"}}, {"final": "Sunny in Paris."}]
        elif variant == "two_tools":
            steps = [{"tool": "get_weather", "args": {"city": "Paris"}},
                     {"tool": "search_docs", "args": {"query": "umbrella policy", "page": 1}},
                     {"final": "Sunny; no umbrella needed."}]
        elif variant == "pagination":
            steps = [{"tool": "search_docs", "args": {"query": "invoices", "page": p}} for p in (1, 2, 3, 4)]
            steps.append({"final": "Found 4 pages of invoices."})
        elif variant == "one_retry":
            steps = [{"http_errors": 1, "tool": "get_weather", "args": {"city": "Rome"}}, {"final": "Warm in Rome."}]
        elif variant == "benign_error_word":
            tool_mode["check_logs"] = "benign"
            steps = [{"tool": "check_logs", "args": {"service": "billing"}}, {"final": "Logs are clean."}]
        elif variant == "long_answer":
            steps = [{"final": " ".join(["Detailed"] * 60) + " answer."}]
        elif variant == "bigger_run":
            base *= 2
            steps = [{"tool": "get_weather", "args": {"city": "Oslo"}}, {"final": "Cold in Oslo."}]
    elif label == "tool_loop":
        k = rng.randint(3, 5)
        steps = [{"tool": "search_docs", "args": {"query": "refund policy", "page": 1}} for _ in range(k)]
        steps.append({"final": "I could not find it."})
    elif label == "retry_storm":
        steps = [{"http_errors": rng.randint(3, 4), "tool": "get_weather", "args": {"city": "Paris"}},
                 {"final": "Sunny in Paris."}]
    elif label == "silent_tool_error":
        tool_mode["get_weather"] = rng.choice(["raise", "error_payload"])
        steps = [{"tool": "get_weather", "args": {"city": "Paris"}}, {"final": "It is sunny in Paris."}]
    elif label == "lost_llm_span":
        steps = [{"tool": "summarize_stream", "args": {"text": "quarterly report"}}, {"final": "Summary done."}]
        tool_mode["summarize_stream"] = "early_close_stream"
    elif label == "dead_end":
        steps = [{"tool": "search_docs", "args": {"query": "x", "page": i}} for i in range(1, 8)]
    elif label == "truncated_answer":
        steps = [{"tool": "get_weather", "args": {"city": "Paris"}},
                 {"final": "The weather in Paris today is", "finish": "length"}]
    elif label == "empty_answer":
        steps = [{"tool": "get_weather", "args": {"city": "Paris"}}, {"final": ""}]
    elif label == "cost_spike":
        base *= rng.randint(12, 25)
        steps = [{"tool": "get_weather", "args": {"city": "Paris"}}, {"final": "Sunny in Paris."}]
    return {"label": label, "hard_negative": hard, "base_tokens": base, "steps": steps, "tool_mode": tool_mode}


# pydantic-ai treats an empty model response as invalid and re-asks the model, so an
# "empty final answer" cannot reach the user there; the case does not exist.
SKIP = {("pydantic_ai", "empty_answer")}

LABELS = ["healthy", "tool_loop", "retry_storm", "silent_tool_error", "lost_llm_span", "dead_end",
          "truncated_answer", "empty_answer", "cost_spike"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--n", type=int, default=6, help="traces per (framework, label); healthy gets 3x")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="results/corpus.jsonl")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--no-data-collection", action="store_true")
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    jobs = []
    for fw in FRAMEWORKS:
        for label in LABELS:
            if (fw, label) in SKIP:
                continue
            n = a.n * 3 if label == "healthy" else a.n
            for i in range(n):
                jobs.append({"framework": fw, "plan": make_plan(label, rng), "idx": i})

    def run(job):
        args = [a.python, "-m", "spanproof.agent_worker", json.dumps(job)]
        if a.no_data_collection:
            args.append("--no-data-collection")
        p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=180)
        mark = "@@SPANPROOF-RESULT@@"
        if mark not in p.stdout:
            return {"job": job, "crashed": True, "stderr": p.stderr[-2000:]}
        r = json.loads(p.stdout.rsplit(mark, 1)[1])
        r["job"] = job
        return r

    with ThreadPoolExecutor(a.jobs) as ex:
        rows = list(ex.map(run, jobs))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")
    crashed = [r for r in rows if r.get("crashed")]
    print(f"{len(rows)} traces, {len(crashed)} crashed -> {out}")
    for r in crashed[:5]:
        print("CRASH", r["job"]["framework"], r["job"]["plan"]["label"], r["stderr"][-600:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
