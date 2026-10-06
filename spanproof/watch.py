"""SpanProof Watch: turn agent failures in a Sentry organization into Sentry issues.

    python -m spanproof.watch --once --since 24h --dry-run      # look, file nothing
    python -m spanproof.watch --interval 300                    # poll every 5 minutes

Each cycle it
  1. finds traces with new agent or LLM spans (span search API),
  2. reads each trace's spans back (and the final LLM call's finish reason from the span
     detail view, because list-valued finish reasons are not searchable: getsentry/sentry-python#7873),
  3. runs the failure-class detectors (tool loops, retry storms, silent tool errors, lost
     LLM calls, dead ends, truncated and empty answers) and a per-agent token-spike check
     against that agent's own recent runs,
  4. files every detection as an event with a stable fingerprint, so repeats of the same
     failure group into one issue, with the trace linked.

Credentials come from ~/.spanproof.env: SENTRY_AUTH_TOKEN (read-only scopes are enough),
SENTRY_ORG, SENTRY_REGION_URL, and SPANPROOF_WATCH_DSN (the project issues are filed into;
defaults to SENTRY_DSN_PY). State (traces already seen, per-agent token history) is kept
in ~/.spanproof-watch.json. A trace is judged once it has finished (see cycle), each failure
in it is filed once with a stable event id, and failed deliveries are retried on the next
cycle. --dry-run never writes the state file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter

from . import sentry_api as api
from .detectors import Detection, detect_trace, run_tokens
from .from_sentry import FIELDS, to_spans
from .issues import TITLES, to_event

STATE = os.path.expanduser("~/.spanproof-watch.json")
AGENT_QUERY = "(span.op:gen_ai.invoke_agent OR gen_ai.operation.type:ai_client)"


def load_state(path: str) -> dict:
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {"seen": {}, "tokens": {}}


def save_state(path: str, state: dict) -> None:
    # keep the state small: forget traces judged more than 7 days ago, keep the last 200 runs per agent
    cutoff = time.time() - 7 * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if _seen(v)["at"] > cutoff}
    state["pending"] = {k: v for k, v in state.get("pending", {}).items() if v["since"] > cutoff}
    state["tokens"] = {k: v[-200:] for k, v in state["tokens"].items()}
    tmp = path + ".tmp"
    json.dump(state, open(tmp, "w"))
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _seen(v) -> dict:
    # older state files stored only the time a trace was judged
    return v if isinstance(v, dict) else {"at": v, "last": float("inf"), "n": 10**9, "filed": []}


def recent_traces(since: str, limit: int) -> list[tuple[str, str, float, int]]:
    """(trace id, project, newest span start, agent/LLM span count) of traces in the window, newest first."""
    e = api.env()
    rows = api.get(f"organizations/{e['SENTRY_ORG']}/events/",
                   [("dataset", "spans"), ("query", AGENT_QUERY), ("statsPeriod", since), ("field", "trace"),
                    ("field", "project"), ("field", "max(precise.start_ts)"), ("field", "count()"),
                    ("sort", "-max(precise.start_ts)"),
                    ("per_page", str(limit))]).get("data", [])
    return [(r["trace"], r["project"], float(r.get("max(precise.start_ts)") or 0), int(r.get("count()") or 0))
            for r in rows if r.get("trace")]


def fetch_trace(trace: str, project: str) -> list[dict]:
    e = api.env()
    rows = api.spans(f"trace:{trace}", FIELDS, stats_period="7d")
    # Only the final LLM call's finish reason decides dead ends and truncation; read just that one.
    chats = sorted([x for x in rows if x.get("span.op") == "gen_ai.chat"],
                   key=lambda x: float(x.get("precise.start_ts") or 0))
    if chats and chats[-1].get("gen_ai.response.finish_reasons") is None:
        last = chats[-1]
        try:
            d = api.get(f"projects/{e['SENTRY_ORG']}/{project}/trace-items/{last['id']}/",
                        [("trace_id", trace), ("item_type", "spans"), ("statsPeriod", "7d")])
            for a in d.get("attributes", []):
                if a.get("name") == "gen_ai.response.finish_reasons":
                    last["gen_ai.response.finish_reasons"] = a.get("value")
        except Exception:  # noqa: BLE001 - detail view is a best-effort enrichment
            pass
    return to_spans(rows)


def cost_spike(state: dict, spans: list[dict], factor: float, min_peers: int) -> Detection | None:
    agent = spans_agent(spans)
    tokens = run_tokens(spans)
    if not agent or not tokens:
        return None
    trace = spans[0].get("trace_id") or "-"
    # history entries are [trace, tokens] so judging a trace again replaces its entry instead of adding one
    hist = [h for h in state["tokens"].setdefault(agent, []) if not (isinstance(h, list) and h[0] == trace)]
    out = None
    peers = [h[1] if isinstance(h, list) else h for h in hist]
    peers = [t for t in peers if t > 0]
    if len(peers) >= min_peers:
        med = statistics.median(peers)
        if med > 0 and tokens > factor * med:
            out = Detection("cost_spike", f"run used {tokens} tokens, {tokens / med:.1f}x the median {med:.0f}",
                            trace, agent, [], {"tokens": tokens, "median": med})
    state["tokens"][agent] = hist + [[trace, tokens]]
    return out


def trace_url(trace: str) -> str:
    return f"https://{api.env()['SENTRY_ORG']}.sentry.io/explore/traces/trace/{trace}/"


def key(d: Detection) -> str:
    return f"{d.kind}|{d.agent or '-'}|{d.detail.get('tool', '-')}"


def event_id(d: Detection) -> str:
    # the same failure in the same trace always gets the same id, so a retry after a partial
    # failure or a crash is dropped by Sentry as a duplicate instead of counted twice
    return uuid.uuid5(uuid.NAMESPACE_URL, f"spanproof-watch/{d.trace_id}/{key(d)}").hex


def build_event(d: Detection) -> dict:
    ev = to_event(d)
    ev["contexts"]["agent_failure"]["trace_url"] = trace_url(d.trace_id)
    ev["tags"]["trace"] = d.trace_id
    ev.update(event_id=event_id(d), timestamp=time.time(), platform="python", environment="spanproof-watch",
              logger="spanproof.watch", culprit=f"agent {d.agent}" if d.agent else "LLM provider calls")
    return ev


class Sender:
    """Posts one event per envelope to a DSN and raises unless Sentry accepted it."""

    def __init__(self, dsn: str, timeout: float = 30.0):  # ingest occasionally takes ~20 s (measured)
        u = urllib.parse.urlsplit(dsn)
        self.url = f"{u.scheme}://{u.hostname}{f':{u.port}' if u.port else ''}/api/{u.path.strip('/')}/envelope/"
        self.auth = f"Sentry sentry_version=7, sentry_key={u.username}, sentry_client=spanproof-watch/1"
        self.dsn, self.timeout = dsn, timeout

    def __call__(self, ev: dict) -> None:
        payload = json.dumps(ev).encode()
        body = b"\n".join([json.dumps({"event_id": ev["event_id"], "dsn": self.dsn}).encode(),
                           json.dumps({"type": "event", "length": len(payload)}).encode(), payload])
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"X-Sentry-Auth": self.auth,
                                              "Content-Type": "application/x-sentry-envelope"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # raises on 4xx/5xx, including 429
            if r.status != 200:
                raise OSError(f"Sentry answered {r.status}")


def file_events(send, dets: list[Detection]) -> None:
    for d in dets:
        send(build_event(d))


def orphans(spans: list[dict]) -> bool:
    """Some span's parent has not arrived: a span ends after its children, so its parent is still
    running or still being ingested, and judging now would see e.g. a provider call without its LLM span."""
    ids = {s["span_id"] for s in spans}
    return any(s.get("parent_span_id") and s["parent_span_id"] not in ids for s in spans)


def complete(spans: list[dict]) -> bool:
    """A top-level agent span has arrived (it ends after everything it ran, so it is sent last)
    and no span is waiting for its parent."""
    if orphans(spans):
        return False
    ops = {s["span_id"]: s.get("op") or "" for s in spans}
    parent = {s["span_id"]: s.get("parent_span_id") for s in spans}
    for s in spans:
        if s.get("op") != "gen_ai.invoke_agent":
            continue
        p, nested = parent.get(s["span_id"]), False
        while p in ops and not nested:
            nested = ops[p] in ("gen_ai.invoke_agent", "gen_ai.execute_tool")
            p = parent.get(p)
        if not nested:
            return True
    return False


def cycle(state: dict, since: str, limit: int, send, dry_run: bool, factor: float, min_peers: int,
          settle: float = 300.0, now: float | None = None, quiet: float = 60.0, max_wait: float = 1800.0) -> list:
    """Judge each trace once it is finished: its top-level agent span has arrived, no span is
    waiting for its parent, and for `quiet` seconds no new span has started and the span list
    has not changed (counted from when this watcher first saw the trace). Without an agent span
    (bare LLM calls, agents that died) both must hold for `settle` seconds, and with spans still
    waiting for their parents it waits up to `max_wait` (data that never arrives must not block
    forever). Every judgment that saw new spans schedules one more read `settle` seconds later,
    for late spans the span list does not show (tool and provider-call spans); if that read finds
    more, the trace is judged again, only failures not filed before are filed, and another read
    is scheduled.
    Nothing is recorded until its events were accepted, so failed deliveries are retried, and
    event ids are stable, so retries never double count."""
    found = []
    now = time.time() if now is None else now
    pending = state.setdefault("pending", {})
    # Oldest first: each agent's token history must be built in the order runs happened,
    # or older normal runs get judged against newer, smaller ones (measured: 10 false spikes
    # out of 15 newest-first, 0 out of 5 oldest-first on the same traces).
    for trace, project, last_start, count in reversed(recent_traces(since, limit)):
        rec = dict(_seen(state["seen"][trace])) if trace in state["seen"] else None  # a copy: commit on success
        grown = not rec or last_start > rec["last"] or count > rec.get("n", 10**9)
        due = bool(rec and rec.get("recheck") and now >= rec["recheck"])
        if not grown and not due:
            continue
        if grown:
            # when the span list last changed, as seen by this watcher (spans arrive late and out of order)
            p = pending.get(trace)
            if not p or p["n"] != count or p["last"] != last_start:
                p = pending[trace] = {"n": count, "last": last_start, "since": now}
            # quiet on both clocks: no span started recently, and the span list stopped changing
            idle = min(now - last_start, now - p["since"])
            if idle < min(quiet, settle):
                continue  # spans are still starting or still arriving
        spans = fetch_trace(trace, project)
        if not spans:
            continue
        if not grown:  # the late re-read
            if len(spans) <= rec.get("spans", 0):
                state["seen"][trace] = dict(rec, recheck=None)
                continue
            if orphans(spans) and now - rec["at"] < max_wait:
                continue  # late spans are still waiting for their parents: read again next cycle
        elif not complete(spans) and (idle < settle or (orphans(spans) and idle < max_wait)):
            continue  # still running or still arriving: judge it on a later cycle
        agent = spans_agent(spans)
        hist = list(state["tokens"].get(agent or "", []))
        dets = detect_trace(spans)
        spike = cost_spike(state, spans, factor, min_peers)
        if spike:
            dets.append(spike)
        filed = set(rec["filed"]) if rec else set()
        new = [d for d in dets if key(d) not in filed]
        if new and not dry_run:
            try:
                file_events(send, new)
            except Exception as exc:  # noqa: BLE001 - retry this trace next cycle
                if agent:
                    state["tokens"][agent] = hist
                print(f"    filing failed for trace {trace}, will retry: {exc}", file=sys.stderr, flush=True)
                continue
        # every judgment that saw new data schedules one more re-read for spans still in flight
        state["seen"][trace] = {"at": now, "last": max(last_start, rec["last"] if rec else 0), "n": count,
                                "spans": len(spans), "recheck": now + settle,
                                "filed": sorted(filed | {key(d) for d in new})}
        pending.pop(trace, None)
        found.extend(new)
    return found


def spans_agent(spans: list[dict]) -> str | None:
    return next((s["data"].get("gen_ai.agent.name") for s in spans if s.get("op") == "gen_ai.invoke_agent"), None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="1h", help="how far back to look for new traces (e.g. 15m, 24h)")
    ap.add_argument("--interval", type=int, default=300, help="seconds between cycles")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="detect and print, file nothing, save no state")
    ap.add_argument("--settle", type=float, default=300.0,
                    help="judge a trace with no finished agent span after this many quiet seconds (default 300)")
    ap.add_argument("--quiet", type=float, default=60.0,
                    help="judge a trace whose agent span has arrived after this many quiet seconds (default 60)")
    ap.add_argument("--limit", type=int, default=100, help="max traces per cycle")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--spike-factor", type=float, default=5.0)
    ap.add_argument("--spike-min-peers", type=int, default=5)
    a = ap.parse_args(argv)

    send = None
    if not a.dry_run:
        dsn = os.environ.get("SPANPROOF_WATCH_DSN") or api.env().get("SPANPROOF_WATCH_DSN") or api.env()["SENTRY_DSN_PY"]
        send = Sender(dsn)
    state = load_state(a.state)
    while True:
        t0 = dt.datetime.now().strftime("%H:%M:%S")
        dets = cycle(state, a.since, a.limit, send, a.dry_run, a.spike_factor, a.spike_min_peers, a.settle,
                     quiet=a.quiet)
        if a.once and state.get("pending"):
            # a trace is judged only after it was seen unchanged for --quiet seconds: look a second time
            print(f"[{t0}] {len(state['pending'])} trace(s) seen for the first time; looking again in "
                  f"{a.quiet:.0f} s", flush=True)
            time.sleep(a.quiet)
            dets += cycle(state, a.since, a.limit, send, a.dry_run, a.spike_factor, a.spike_min_peers, a.settle,
                          quiet=a.quiet)
        if not a.dry_run:
            save_state(a.state, state)
        groups = Counter((d.kind, d.agent or "-", str(d.detail.get("tool", "-"))) for d in dets)
        print(f"[{t0}] {len(dets)} detection(s) in {len(groups)} group(s)"
              f"{' (dry run, nothing filed)' if a.dry_run else ''}", flush=True)
        for (kind, agent, tool), n in groups.most_common():
            print(f"    {n:3}x  {TITLES[kind]}  (agent={agent}, tool={tool})", flush=True)
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
