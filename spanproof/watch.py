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
in ~/.spanproof-watch.json so a trace is filed once. A trace is only judged after it has been
quiet for --settle seconds (late spans would otherwise look like dead ends or lost calls), and
only marked done once its events were handed to the SDK. --dry-run never writes the state file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
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
    # keep the state small: forget traces older than 7 days, keep the last 200 runs per agent
    cutoff = time.time() - 7 * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    state["tokens"] = {k: v[-200:] for k, v in state["tokens"].items()}
    tmp = path + ".tmp"
    json.dump(state, open(tmp, "w"))
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def recent_traces(since: str, limit: int) -> list[tuple[str, str, float]]:
    """(trace id, project slug, newest span start) of traces with agent/LLM spans in the window, newest first."""
    e = api.env()
    rows = api.get(f"organizations/{e['SENTRY_ORG']}/events/",
                   [("dataset", "spans"), ("query", AGENT_QUERY), ("statsPeriod", since), ("field", "trace"),
                    ("field", "project"), ("field", "max(precise.start_ts)"), ("sort", "-max(precise.start_ts)"),
                    ("per_page", str(limit))]).get("data", [])
    return [(r["trace"], r["project"], float(r.get("max(precise.start_ts)") or 0)) for r in rows if r.get("trace")]


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
    hist = state["tokens"].setdefault(agent, [])
    out = None
    peers = [t for t in hist if t > 0]
    if len(peers) >= min_peers:
        med = statistics.median(peers)
        if med > 0 and tokens > factor * med:
            out = Detection("cost_spike", f"run used {tokens} tokens, {tokens / med:.1f}x the median {med:.0f}",
                            spans[0].get("trace_id") or "-", agent, [], {"tokens": tokens, "median": med})
    hist.append(tokens)
    return out


def trace_url(trace: str) -> str:
    return f"https://{api.env()['SENTRY_ORG']}.sentry.io/explore/traces/trace/{trace}/"


def file_events(client, dets: list[Detection]) -> None:
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_client(client)
        for d in dets:
            ev = to_event(d)
            ev["contexts"]["agent_failure"]["trace_url"] = trace_url(d.trace_id)
            ev["tags"]["trace"] = d.trace_id
            ev["logger"] = "spanproof.watch"
            ev["culprit"] = f"agent {d.agent}" if d.agent else "LLM provider calls"
            scope.capture_event(ev)
    client.flush(timeout=30)


def cycle(state: dict, since: str, limit: int, client, dry_run: bool, factor: float, min_peers: int,
          settle: float = 300.0, now: float | None = None) -> list:
    found = []
    now = time.time() if now is None else now
    # Oldest first: each agent's cost history must be built in the order runs happened,
    # or older normal runs get judged against newer, smaller ones (measured: 10 false spikes
    # out of 15 newest-first, 0 out of 5 oldest-first on the same traces).
    for trace, project, last_start in reversed(recent_traces(since, limit)):
        if trace in state["seen"]:
            continue
        if now - last_start < settle:
            continue  # still active or spans still arriving: judge it on a later cycle
        spans = fetch_trace(trace, project)
        if not spans:
            continue
        hist = list(state["tokens"].get(spans_agent(spans) or "", []))
        dets = detect_trace(spans)
        spike = cost_spike(state, spans, factor, min_peers)
        if spike:
            dets.append(spike)
        if dets and not dry_run:
            try:
                file_events(client, dets)
            except Exception as exc:  # noqa: BLE001 - retry this trace next cycle
                if spans_agent(spans):
                    state["tokens"][spans_agent(spans)] = hist
                print(f"    filing failed for trace {trace}, will retry: {exc}", file=sys.stderr, flush=True)
                continue
        state["seen"][trace] = time.time()
        found.extend(dets)
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
                    help="seconds a trace must be quiet before it is judged (default 300)")
    ap.add_argument("--limit", type=int, default=100, help="max traces per cycle")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--spike-factor", type=float, default=5.0)
    ap.add_argument("--spike-min-peers", type=int, default=5)
    a = ap.parse_args(argv)

    client = None
    if not a.dry_run:
        import sentry_sdk

        dsn = os.environ.get("SPANPROOF_WATCH_DSN") or api.env().get("SPANPROOF_WATCH_DSN") or api.env()["SENTRY_DSN_PY"]
        client = sentry_sdk.Client(dsn=dsn, environment="spanproof-watch", default_integrations=False,
                                   traces_sample_rate=0.0)
    state = load_state(a.state)
    while True:
        t0 = dt.datetime.now().strftime("%H:%M:%S")
        dets = cycle(state, a.since, a.limit, client, a.dry_run, a.spike_factor, a.spike_min_peers, a.settle)
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
