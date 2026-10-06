"""Real agents on a live model, with faults injected into real tools.

    SPANPROOF_UPSTREAM_KEY=... python -m spanproof.live_agents --n 4 --out results/live_agents.jsonl

The model's behaviour is real (no scripted replies). What we control is the tools: each
run injects one fault (or none) into the tool layer, so the label is known by
construction from the injected fault, while the agent's reaction is whatever the live
model does. A label is only kept when the trace shows the fault actually happened
(e.g. the faulty tool was called); otherwise the run is labelled "not_triggered".

Faults -> expected class
  none                 healthy
  tool_error_payload   silent_tool_error, if the agent answers without acknowledging it
  tool_timeout_raise   silent_tool_error (framework turns the exception into a tool message)
  same_result_forever  tool_loop (the tool always says "try again with the same query")
  provider_500x3       retry_storm (three upstream 500s before the real call)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAULTS = ["none", "tool_error_payload", "tool_timeout_raise", "same_result_forever", "provider_500x3"]
TASKS = [
    "What's the weather in Paris right now? Use the weather tool, then answer in one sentence.",
    "Look up order A-1042 with the order tool and tell me its status in one sentence.",
    "Find the refund policy using the docs search tool and summarize it in one sentence.",
    "Check the weather in Oslo with the tool and say whether I need a coat.",
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--n", type=int, default=4, help="runs per (framework, fault)")
    ap.add_argument("--frameworks", default="openai_agents,langgraph,pydantic_ai")
    ap.add_argument("--out", default="results/live_agents.jsonl")
    ap.add_argument("--pace", type=float, default=15.0)
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    jobs = [{"framework": fw, "fault": f, "task": rng.choice(TASKS), "idx": i}
            for fw in a.frameworks.split(",") for f in FAULTS for i in range(a.n)]
    rng.shuffle(jobs)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a") as fh:
        for k, job in enumerate(jobs):
            p = subprocess.run([a.python, "-m", "spanproof.live_agent_worker", json.dumps(job)], cwd=ROOT,
                               capture_output=True, text=True, timeout=300)
            mark = "@@SPANPROOF-RESULT@@"
            if mark not in p.stdout:
                row = {"job": job, "crashed": True, "stderr": p.stderr[-1500:]}
            else:
                row = json.loads(p.stdout.rsplit(mark, 1)[1])
                row["job"] = job
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            print(f"[{k + 1}/{len(jobs)}] {job['framework']:14} {job['fault']:20} -> "
                  f"{'CRASH' if row.get('crashed') else row.get('label')}", flush=True)
            time.sleep(a.pace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
