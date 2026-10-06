"""Precision and recall of the failure-class detectors on a labelled corpus.

    python -m spanproof.evaluate results/corpus.jsonl [--json out.json]

Intervals are Wilson score intervals (95%), the right choice for small counts.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict

from .detectors import detect_cost_spikes, detect_trace

CLASSES = ["tool_loop", "retry_storm", "silent_tool_error", "lost_llm_span", "dead_end", "truncated_answer",
           "empty_answer", "cost_spike"]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def label(r):
    return r["label"] if "label" in r else r["job"]["plan"]["label"]


def group_key(r):
    if "job" in r:
        return (r["framework"], label(r), r["job"]["plan"].get("hard_negative"))
    return ("heldout", label(r), r.get("variant"))


def predict(rows):
    traces = [r["spans"] for r in rows]
    preds = [set() for _ in rows]
    dets = [[] for _ in rows]
    for i, sp in enumerate(traces):
        for d in detect_trace(sp):
            preds[i].add(d.kind)
            dets[i].append(d)
    for i, d in detect_cost_spikes(traces).items():
        preds[i].add(d.kind)
        dets[i].append(d)
    return preds, dets


def score(rows, preds):
    out = {}
    for c in CLASSES:
        tp = sum(1 for r, p in zip(rows, preds) if label(r) == c and c in p)
        fn = sum(1 for r, p in zip(rows, preds) if label(r) == c and c not in p)
        fp = sum(1 for r, p in zip(rows, preds) if label(r) != c and c in p)
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        out[c] = {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec,
                  "precision_ci": wilson(tp, tp + fp), "recall_ci": wilson(tp, tp + fn)}
    healthy = [p for r, p in zip(rows, preds) if label(r) == "healthy"]
    out["_healthy_false_alarm_rate"] = {"k": sum(1 for p in healthy if p), "n": len(healthy),
                                        "ci": wilson(sum(1 for p in healthy if p), len(healthy))}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    rows = [json.loads(x) for x in open(a.corpus) if x.strip()]
    rows = [r for r in rows if not r.get("crashed")]
    preds, dets = predict(rows)
    s = score(rows, preds)
    print(f"{len(rows)} traces")
    print(f"{'class':18} {'TP':>3} {'FP':>3} {'FN':>3}  precision [95% CI]      recall [95% CI]")
    for c in CLASSES:
        m = s[c]
        print(f"{c:18} {m['tp']:3} {m['fp']:3} {m['fn']:3}  {m['precision']:.2f} [{m['precision_ci'][0]:.2f},"
              f"{m['precision_ci'][1]:.2f}]   {m['recall']:.2f} [{m['recall_ci'][0]:.2f},{m['recall_ci'][1]:.2f}]")
    h = s["_healthy_false_alarm_rate"]
    print(f"healthy traces with any alarm: {h['k']}/{h['n']}  CI [{h['ci'][0]:.2f},{h['ci'][1]:.2f}]")
    # error analysis
    by = defaultdict(Counter)
    for r, p in zip(rows, preds):
        lab = label(r)
        key = group_key(r)
        for c in CLASSES:
            if (lab == c) != (c in p):
                by[key]["FN:" + c if lab == c else "FP:" + c] += 1
    if by:
        print("errors by (framework, label, variant):")
        for k, v in sorted(by.items()):
            print("  ", k, dict(v))
    if a.json:
        json.dump({"n": len(rows), "scores": s, "errors": {str(k): dict(v) for k, v in by.items()}},
                  open(a.json, "w"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
