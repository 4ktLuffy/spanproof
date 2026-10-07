"""Replay saved traces into SpanProof Watch the way Sentry would deliver them, and count its mistakes.

    python -m spanproof.watch_replay --seeds 5

Each trace is stretched to a production-like duration and placed at a random time in a
six-hour window. A span becomes visible when it has ended (after all of its children) plus
an ingestion delay with a long tail, so a running agent shows up as a partial trace whose
top-level agent span is missing. Watch polls every 300 seconds (its default) against this fake Sentry.
Deliveries fail at random, and in some cycles the process dies after its first 1-3 deliveries,
losing whatever it had not saved yet. The fake Sentry drops repeated event ids, as the real one does (checked on a real
project: one event sent three times with the same id was stored once).

Truth is what the detectors find on each complete trace, so the replay measures only what
Watch's lifecycle adds: false issues (not in the complete trace), correct issues filed while
some of the trace's spans were still in flight, failures never filed,
and events stored twice. Trace discovery uses the real query: agent or LLM-call spans that
ended in the last hour, newest 100 traces.
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
import statistics
import uuid
from collections import Counter

from . import watch
from .detectors import _is_client, detect_trace

INTERVAL = 300.0  # watch.py's default --interval
LIMIT = 100  # watch.py's default --limit


def load(paths: list[str]) -> list[list[dict]]:
    out = []
    for p in paths:
        for line in open(p):
            if line.strip():
                sp = json.loads(line)["spans"]
                if any((s.get("op") or "").startswith("gen_ai.") for s in sp):
                    out.append(sp)
    return out


# Extra arrival scenarios, run with --scenario NAME. They only change how traces are scheduled; the
# default (no --scenario) draws the same random numbers as before, so the published numbers do not move.
SCENARIOS = {
    "default": {},
    # every span ending in a 20-minute outage window is ingested only after it, all at once
    "burst": {"burst": (3 * 3600.0, 1200.0)},
    # the same, but the outage lasts 90 minutes: longer than Watch's one-hour discovery window
    "burst-90m": {"burst": (3 * 3600.0, 5400.0)},
    # a long tail of agents running up to 3 hours (default caps at 30 minutes)
    "long-agents": {"max_duration": 10800.0, "mu": 5.5},
    # 25% of spans ingested 1-20 minutes late (default: 2% by 1-10 minutes)
    "backlog-heavy": {"backlog_p": 0.25, "backlog_max": 1200.0},
    # the process dies in 30% of cycles (set by run(); see main())
    "restart-heavy": {"crash": 0.3},
    # the same 301 traces packed into one hour, so more than Watch's 100-trace discovery limit are live at once
    "dense": {"window": 3600.0},
}


def schedule(traces: list[list[dict]], rng: random.Random, window: float = 6 * 3600, *,
             max_duration: float = 1800.0, mu: float = 3.7, backlog_p: float = 0.02, backlog_max: float = 600.0,
             burst: tuple | None = None, crash: float | None = None) -> list[list[dict]]:
    """Copies of the traces with start times stretched and placed, and an arrival time per span."""
    out = []
    for n, sp in enumerate(traces):
        t0 = min(s["start"] for s in sp)
        span = max(s["start"] for s in sp) - t0
        duration = min(rng.lognormvariate(mu, 1.2), max_duration)  # median ~40 s, a few runs near 30 min
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
            if rng.random() < backlog_p:
                delay += rng.uniform(60, backlog_max)  # ingestion backlog
            s["_arrive"] = end(s) + delay
            if burst and burst[0] <= s["_arrive"] < burst[0] + burst[1]:
                s["_arrive"] = burst[0] + burst[1] + rng.uniform(0, 30)  # held back, then flushed together
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
        # the real query: agent spans or LLM-call spans, ended within the last hour, newest 100 traces
        rows = []
        for tid, sp in self.traces.items():
            seen = [s for s in sp if s["_arrive"] <= self.now and s["_end"] >= self.now - 3600
                    and (s.get("op") == "gen_ai.invoke_agent" or _is_client(s))]
            if seen:
                rows.append((tid, "p", max(s["start"] for s in seen), len(seen)))
        return sorted(rows, key=lambda r: -r[2])[:limit]

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
                    watch.cycle(state, "1h", LIMIT, None, False, 5.0, 5, settle=300.0, now=world.now, quiet=60.0)
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
    delays = [filed_at[k] - finish[k[0]] for k in want if k in filed_at]
    agent_at = {sp[0]["trace_id"]: top_agent_arrival(sp) for sp in traces}
    return {"delays": delays,
            "early_before_agent": sum(1 for k in got if agent_at[k[0]] is not None and k in filed_at
                                      and filed_at[k] < agent_at[k[0]]),"false": sum(1 for k in got if k not in want), "missed": sum(1 for k in want if k not in got),
            "early": sum(1 for k in got if k in want and filed_at[k] < finish[k[0]]),
            "duplicates": sum(n - 1 for n in got.values()), "true": len(want), "posts": world.posts,
            "detail": {"false": sorted(k for k in got if k not in want),
                       "missed": sorted(k for k in want if k not in got)},
            }


_FILE_EVENTS = watch.file_events


def top_agent_arrival(sp: list[dict]) -> float | None:
    """When the trace's first top-level agent span (not one running as a tool of another agent) arrived."""
    ops = {s["span_id"]: s["op"] for s in sp}
    parent = {s["span_id"]: s.get("parent_span_id") for s in sp}
    out = []
    for s in sp:
        if s["op"] != "gen_ai.invoke_agent":
            continue
        p, nested = parent[s["span_id"]], False
        while p in ops and not nested:
            nested = ops[p] in ("gen_ai.invoke_agent", "gen_ai.execute_tool")
            p = parent.get(p)
        if not nested:
            out.append(s["_arrive"])
    return min(out, default=None)


def first_sight_cycle(state, world, file_events):
    for trace, project, *_ in reversed(world.recent_traces("1h", LIMIT)):
        if trace in state["seen"]:
            continue
        spans = world.fetch_trace(trace, project)
        dets = detect_trace(spans)
        spike = watch.cost_spike(state, spans, 5.0, 5)
        state["seen"][trace] = world.now
        if dets or spike:
            file_events(None, dets + ([spike] if spike else []))  # an error here ended the process


def settle_only_cycle(state, world, file_events):
    for trace, project, last, _ in reversed(world.recent_traces("1h", LIMIT)):
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
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--fail", type=float, default=0.1, help="share of deliveries that fail")
    ap.add_argument("--crash", type=float, default=0.02,
                    help="share of cycles in which a crash is armed: the process dies at its next delivery after 1-3")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="default",
                    help="harsher arrival pattern (default: the published one)")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    base = load(a.traces)
    sc = dict(SCENARIOS[a.scenario])
    if "crash" in sc:
        a.crash = sc.pop("crash")
    rows = {}
    for version in ("first-sight", "settle-only", "watch"):
        tot = Counter()
        delays = []
        for seed in range(a.seeds):
            traces = schedule(base, random.Random(seed), **sc)
            r = run(version, traces, seed, a.fail, a.crash)
            delays += r.pop("delays")
            r.pop("detail")
            tot.update(r)
        rows[version] = dict(tot, median_delay=statistics.median(delays))  # first filing of each true failure
    print(f"scenario {a.scenario}: {len(base)} traces x {a.seeds} arrival orders; {a.fail:.0%} of deliveries fail, "
          f"a crash is armed in {a.crash:.0%} of cycles")
    print(f"{'version':12} {'true':>6} {'false':>6} {'right, spans in flight':>23} {'never filed':>12} {'stored twice':>13}"
          f" {'median delay':>13}")
    for v, r in rows.items():
        print(f"{v:12} {r['true']:6} {r['false']:6} {r['early']:23} {r['missed']:12} {r['duplicates']:13}"
              f" {r['median_delay']:12.0f}s")
    if a.json:
        json.dump({"traces": len(base), "seeds": a.seeds, "fail": a.fail, "crash": a.crash, "scenario": a.scenario, "versions": rows},
                  open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
