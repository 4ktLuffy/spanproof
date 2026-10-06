"""Replay saved traces into SpanProof Watch the way Sentry would deliver them, and count its mistakes.

    python -m spanproof.watch_replay --seeds 5

Each trace is stretched to a production-like duration and placed at a random time in a
six-hour window. A span becomes visible when it has ended (after all of its children) plus
an ingestion delay with a long tail, so a running agent shows up as a partial trace whose
top-level agent span is missing. Watch polls every 60 seconds against this fake Sentry.
Deliveries fail at random and the process crashes at random, losing whatever it had not
saved yet. The fake Sentry drops repeated event ids, as the real one does (checked on a real
project: one event sent three times with the same id was stored once).

Truth is what the detectors find on each complete trace, so the replay measures only what
Watch's lifecycle adds: issues filed too early, failures never filed, and events stored twice.
Three versions run on the same arrivals:

  first-sight   judge a trace when it first appears, random event ids, a delivery error ends the cycle
                (the Watch in 4ktLuffy/spanproof before 9e5a444)
  settle-only   judge after 300 s with no new span, retry failed deliveries, random event ids (9e5a444)
  watch         the current Watch: judge when the top-level agent span has arrived and 60 s are quiet
                (or 300 s without it), re-judge traces that grow, stable event ids
"""

from __future__ import annotations

import argparse
import json
import random
import uuid
from collections import Counter

from . import watch
from .detectors import detect_trace

INTERVAL = 60.0


def load(paths: list[str]) -> list[list[dict]]:
    out = []
    for p in paths:
        for line in open(p):
            if line.strip():
                sp = json.loads(line)["spans"]
                if any((s.get("op") or "").startswith("gen_ai.") for s in sp):
                    out.append(sp)
    return out


def schedule(traces: list[list[dict]], rng: random.Random, window: float = 6 * 3600) -> list[list[dict]]:
    """Copies of the traces with start times stretched and placed, and an arrival time per span."""
    out = []
    for n, sp in enumerate(traces):
        t0 = min(s["start"] for s in sp)
        span = max(s["start"] for s in sp) - t0
        duration = min(rng.lognormvariate(3.7, 1.2), 1800.0)  # median ~40 s, a few runs near 30 min
        stretch = duration / max(span, 0.01)
        base = rng.uniform(0, window)
        tid = f"r{n:04d}"
        ids = {s["span_id"]: f"{tid}-{s['span_id']}" for s in sp}
        new = [dict(s, trace_id=tid, span_id=ids[s["span_id"]], parent_span_id=ids.get(s.get("parent_span_id")),
                    start=base + (s["start"] - t0) * stretch) for s in sp]
        kids: dict = {}
        for s in new:
            kids.setdefault(s["parent_span_id"], []).append(s)

        def end(s):
            if "_end" not in s:
                own = s["start"] + rng.lognormvariate(1.4 if s["op"] == "gen_ai.chat" else 0.0, 0.8)
                s["_end"] = max([own] + [end(c) + 0.05 for c in kids.get(s["span_id"], [])])
            return s["_end"]

        for s in new:
            delay = rng.lognormvariate(1.0, 0.7)
            if rng.random() < 0.02:
                delay += rng.uniform(60, 600)  # ingestion backlog
            s["_arrive"] = end(s) + delay
        out.append(new)
    return out


class World:
    """Fake Sentry: span search over what has arrived, and an ingest endpoint that drops repeated ids."""

    def __init__(self, traces, rng, fail=0.1):
        self.traces = {sp[0]["trace_id"]: sp for sp in traces}
        self.rng, self.fail, self.now = rng, fail, 0.0
        self.stored: list[dict] = []
        self.ids: set = set()
        self.posts = 0

    def visible(self, tid):
        return [{k: v for k, v in s.items() if not k.startswith("_")}
                for s in self.traces[tid] if s["_arrive"] <= self.now]

    def recent_traces(self, since, limit):
        rows = []
        for tid, sp in self.traces.items():
            # like the real span search, which only matches gen_ai spans
            seen = [s for s in sp if s["_arrive"] <= self.now and (s.get("op") or "").startswith("gen_ai.")]
            if seen:
                rows.append((tid, "p", max(s["start"] for s in seen), len(seen)))
        return sorted(rows, key=lambda r: -r[2])

    def fetch_trace(self, trace, project):
        return self.visible(trace)

    def send(self, ev):
        if self.rng.random() < self.fail:
            raise OSError("503 from ingest")
        self.posts += 1
        if ev["event_id"] not in self.ids:
            self.ids.add(ev["event_id"])
            self.stored.append(ev)


class Crash(BaseException):  # not an Exception: Watch must not catch it as a delivery error
    pass


def truth(traces: list[list[dict]]) -> set:
    """(trace, failure key) the detectors find on complete traces, judged in the order traces finish."""
    state = {"seen": {}, "tokens": {}}
    out = set()
    for sp in sorted(traces, key=lambda sp: max(s["_arrive"] for s in sp)):
        clean = [{k: v for k, v in s.items() if not k.startswith("_")} for s in sp]
        dets = detect_trace(clean)
        spike = watch.cost_spike(state, clean, 5.0, 5)
        out |= {(sp[0]["trace_id"], watch.key(d)) for d in dets + ([spike] if spike else [])}
    return out


def run(version: str, traces, seed: int, fail: float, crash: float) -> dict:
    rng = random.Random(seed * 7919)
    world = World(traces, rng, fail)
    saved = json.dumps({"seen": {}, "tokens": {}})
    state = json.loads(saved)
    filed_at: dict = {}
    end = max(s["_arrive"] for sp in traces for s in sp) + 3600

    orig = watch.recent_traces, watch.fetch_trace, watch.event_id, watch.trace_url
    watch.recent_traces, watch.fetch_trace = world.recent_traces, world.fetch_trace
    watch.trace_url = lambda trace: f"https://example.sentry.io/explore/traces/trace/{trace}/"
    if version != "watch":
        watch.event_id = lambda d: uuid.UUID(int=rng.getrandbits(128)).hex
    try:
        while world.now < end:
            world.now += INTERVAL
            crash_after = rng.randrange(1, 4) if rng.random() < crash else None
            sends = [0]

            def send(ev):
                if crash_after is not None and sends[0] >= crash_after:
                    raise Crash
                sends[0] += 1
                world.send(ev)
                filed_at.setdefault((ev["contexts"]["agent_failure"]["trace_id"], ev["_key"]), world.now)

            def file_events(_, dets):
                for d in dets:
                    send(dict(watch.build_event(d), _key=watch.key(d)))

            watch.file_events = file_events
            try:
                if version == "first-sight":
                    first_sight_cycle(state, world, file_events)
                elif version == "settle-only":
                    settle_only_cycle(state, world, file_events)
                else:
                    watch.cycle(state, "1h", 10**6, None, False, 5.0, 5, settle=300.0, now=world.now, quiet=60.0)
            except (Crash, OSError):
                state = json.loads(saved)  # the process died: restart from the last saved state
                continue
            saved = json.dumps(state)
    finally:
        watch.recent_traces, watch.fetch_trace, watch.event_id, watch.trace_url = orig
        watch.file_events = _FILE_EVENTS
    want = truth(traces)
    got = Counter((e["contexts"]["agent_failure"]["trace_id"], e["_key"]) for e in world.stored)
    finish = {sp[0]["trace_id"]: max(s["_arrive"] for s in sp) for sp in traces}
    delays = sorted(filed_at[k] - finish[k[0]] for k in want if k in filed_at)
    return {"false": sum(1 for k in got if k not in want), "missed": sum(1 for k in want if k not in got),
            "duplicates": sum(n - 1 for n in got.values()), "true": len(want), "posts": world.posts,
            "median_delay": delays[len(delays) // 2] if delays else None}


_FILE_EVENTS = watch.file_events


def first_sight_cycle(state, world, file_events):
    for trace, project, *_ in reversed(world.recent_traces("1h", 10**6)):
        if trace in state["seen"]:
            continue
        spans = world.fetch_trace(trace, project)
        dets = detect_trace(spans)
        spike = watch.cost_spike(state, spans, 5.0, 5)
        state["seen"][trace] = world.now
        if dets or spike:
            file_events(None, dets + ([spike] if spike else []))  # an error here ended the process


def settle_only_cycle(state, world, file_events):
    for trace, project, last, _ in reversed(world.recent_traces("1h", 10**6)):
        if trace in state["seen"] or world.now - last < 300:
            continue
        spans = world.fetch_trace(trace, project)
        hist = list(state["tokens"].get(watch.spans_agent(spans) or "", []))
        dets = detect_trace(spans)
        spike = watch.cost_spike(state, spans, 5.0, 5)
        dets += [spike] if spike else []
        if dets:
            try:
                file_events(None, dets)
            except OSError:
                if watch.spans_agent(spans):
                    state["tokens"][watch.spans_agent(spans)] = hist
                continue
        state["seen"][trace] = world.now


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="*", default=["results/live_agents1.jsonl", "results/corpus_dc_on.jsonl"])
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--fail", type=float, default=0.1, help="share of deliveries that fail")
    ap.add_argument("--crash", type=float, default=0.02, help="share of cycles in which the process dies")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    base = load(a.traces)
    rows = {}
    for version in ("first-sight", "settle-only", "watch"):
        tot = Counter()
        delays = []
        for seed in range(a.seeds):
            traces = schedule(base, random.Random(seed))
            r = run(version, traces, seed, a.fail, a.crash)
            delays.append(r.pop("median_delay"))
            tot.update(r)
        rows[version] = dict(tot, median_delay=sorted(d for d in delays if d is not None)[len(delays) // 2])
    print(f"{len(base)} traces x {a.seeds} arrival orders; {a.fail:.0%} of deliveries fail, "
          f"the process dies in {a.crash:.0%} of cycles")
    print(f"{'version':12} {'true':>6} {'filed early/false':>18} {'never filed':>12} {'stored twice':>13}"
          f" {'median delay':>13}")
    for v, r in rows.items():
        print(f"{v:12} {r['true']:6} {r['false']:18} {r['missed']:12} {r['duplicates']:13}"
              f" {r['median_delay']:12.0f}s")
    if a.json:
        json.dump({"traces": len(base), "seeds": a.seeds, "fail": a.fail, "crash": a.crash, "versions": rows},
                  open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
