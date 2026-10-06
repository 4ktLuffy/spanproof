"""SpanProof Watch: no double filing, per-agent cost history, grouped events with trace links."""

import json

from spanproof import watch
from spanproof.detectors import Detection


def spans(agent="a", tokens=100, loop=False):
    out = [{"op": "gen_ai.invoke_agent", "span_id": "ag", "parent_span_id": None, "trace_id": "t", "status": "ok",
            "start": 0, "data": {"gen_ai.agent.name": agent}},
           {"op": "gen_ai.chat", "span_id": "c", "parent_span_id": "ag", "trace_id": "t", "status": "ok", "start": 9,
            "data": {"gen_ai.usage.input_tokens": tokens, "gen_ai.usage.output_tokens": 0,
                     "gen_ai.response.finish_reasons": '["stop"]', "gen_ai.response.text": "done"}}]
    if loop:
        for i in range(3):
            out.append({"op": "gen_ai.execute_tool", "span_id": f"t{i}", "parent_span_id": "ag", "trace_id": "t",
                        "status": "ok", "start": i + 1, "data": {"gen_ai.tool.name": "search",
                                                                 "gen_ai.tool.call.arguments": '{"q": 1}'}})
    return out


def test_cycle_files_once_and_skips_seen(monkeypatch):
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p"), ("t2", "p")])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=(trace == "t1")))
    filed = []
    monkeypatch.setattr(watch, "file_events", lambda client, dets: filed.extend(dets))
    state = {"seen": {}, "tokens": {}}
    first = watch.cycle(state, "1h", 10, client=object(), dry_run=False, factor=5, min_peers=5)
    assert [d.kind for d in first] == ["tool_loop"] and len(filed) == 1
    again = watch.cycle(state, "1h", 10, client=object(), dry_run=False, factor=5, min_peers=5)
    assert again == [] and len(filed) == 1  # both traces were seen: nothing filed twice


def test_dry_run_files_nothing(monkeypatch):
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p")])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=True))
    monkeypatch.setattr(watch, "file_events", lambda client, dets: (_ for _ in ()).throw(AssertionError("filed")))
    assert watch.cycle({"seen": {}, "tokens": {}}, "1h", 10, None, True, 5, 5)


def test_cost_spike_uses_the_agents_own_history():
    state = {"seen": {}, "tokens": {}}
    for n in (100, 110, 90, 105, 95):
        assert watch.cost_spike(state, spans(tokens=n), 5, 5) is None  # building history
    assert watch.cost_spike(state, spans(tokens=300), 5, 5) is None  # 3x is not a spike
    hit = watch.cost_spike(state, spans(tokens=2000), 5, 5)
    assert hit and hit.kind == "cost_spike" and hit.agent == "a"
    assert watch.cost_spike(state, spans(agent="other", tokens=2000), 5, 5) is None  # no history yet


def test_events_carry_fingerprint_and_trace_link(monkeypatch):
    monkeypatch.setattr(watch.api, "env", lambda: {"SENTRY_ORG": "acme"})
    sent = []

    class Client:
        def capture_event(self, event, hint=None, scope=None):
            sent.append(event)

        def flush(self, timeout=None):
            pass

        def is_active(self):
            return True

        options = {"send_default_pii": False}

    import sentry_sdk

    monkeypatch.setattr(sentry_sdk.Scope, "capture_event", lambda self, ev, *a, **k: sent.append(ev))
    d = Detection("tool_loop", "tool search called 3x", "abc123", "a", ["t0"], {"tool": "search"})
    watch.file_events(Client(), [d])
    ev = sent[0]
    assert ev["fingerprint"] == ["agent-failure", "tool_loop", "a", "search"]
    assert ev["contexts"]["agent_failure"]["trace_url"] == "https://acme.sentry.io/explore/traces/trace/abc123/"
    assert json.dumps(ev)  # serializable


def test_state_is_pruned(tmp_path):
    p = str(tmp_path / "s.json")
    watch.save_state(p, {"seen": {"old": 0, "new": 9e12}, "tokens": {"a": list(range(500))}})
    s = watch.load_state(p)
    assert list(s["seen"]) == ["new"] and len(s["tokens"]["a"]) == 200


def test_history_is_built_oldest_first(monkeypatch):
    # recent_traces returns newest first: five small new runs, then five large old runs.
    sizes = {f"n{i}": 100 for i in range(5)} | {f"o{i}": 1000 for i in range(5)}
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [(t, "p") for t in sizes])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(tokens=sizes[trace]))
    dets = watch.cycle({"seen": {}, "tokens": {}}, "1h", 50, None, True, 5, 5)
    # judged in time order, the drop to 100 is not a spike and the old 1000s are not either
    assert [d for d in dets if d.kind == "cost_spike"] == []
