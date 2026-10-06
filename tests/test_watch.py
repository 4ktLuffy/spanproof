"""SpanProof Watch: no double filing, per-agent cost history, grouped events with trace links."""

import json

from spanproof import watch
from spanproof.detectors import Detection


def spans(agent="a", tokens=100, loop=False, trace="t"):
    out = [{"op": "gen_ai.invoke_agent", "span_id": "ag", "parent_span_id": None, "trace_id": trace, "status": "ok",
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
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", 0.0, 1), ("t2", "p", 0.0, 1)])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=(trace == "t1")))
    filed = []
    monkeypatch.setattr(watch, "file_events", lambda send, dets: filed.extend(dets))
    state = {"seen": {}, "tokens": {}}
    first = watch.cycle(state, "1h", 10, send=object(), dry_run=False, factor=5, min_peers=5)
    assert [d.kind for d in first] == ["tool_loop"] and len(filed) == 1
    again = watch.cycle(state, "1h", 10, send=object(), dry_run=False, factor=5, min_peers=5)
    assert again == [] and len(filed) == 1  # both traces were seen: nothing filed twice


def test_dry_run_files_nothing(monkeypatch):
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", 0.0, 1)])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=True))
    monkeypatch.setattr(watch, "file_events", lambda client, dets: (_ for _ in ()).throw(AssertionError("filed")))
    assert watch.cycle({"seen": {}, "tokens": {}}, "1h", 10, None, True, 5, 5)


def test_trace_is_judged_only_when_finished(monkeypatch):
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", 1000.0, 1)])
    monkeypatch.setattr(watch, "file_events", lambda send, dets: None)
    state = {"seen": {}, "tokens": {}}
    # agent span arrived, but spans started 30s ago may still be arriving
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=True))
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=1030.0) == [] and "t1" not in state["seen"]
    # no agent span yet (agent still running): quiet for 120s is not enough
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=True)[1:])
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=1120.0) == [] and "t1" not in state["seen"]
    # agent span arrived and 60s quiet: judged
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=True))
    assert [d.kind for d in watch.cycle(state, "1h", 10, object(), False, 5, 5, now=1120.0)] == ["tool_loop"]


def test_trace_without_agent_span_is_judged_after_settle(monkeypatch):
    bare = [dict(x, parent_span_id=None) for x in spans()[1:]]  # an LLM call outside any agent
    bare[0]["data"] = dict(bare[0]["data"], **{"gen_ai.response.finish_reasons": '["length"]',
                                               "gen_ai.response.text": ""})
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", 1000.0, 1)])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: bare)
    monkeypatch.setattr(watch, "file_events", lambda send, dets: None)
    state = {"seen": {}, "tokens": {}}
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=1299.0) == [] and "t1" not in state["seen"]
    watch.cycle(state, "1h", 10, object(), False, 5, 5, now=1300.0)
    assert "t1" in state["seen"]


def test_spans_waiting_for_their_parent_hold_the_trace_until_max_wait(monkeypatch):
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", 1000.0, 3)])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=True)[1:])  # agent span missing
    monkeypatch.setattr(watch, "file_events", lambda send, dets: None)
    state = {"seen": {}, "tokens": {}}
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=2000.0) == [] and "t1" not in state["seen"]
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=2800.0) and "t1" in state["seen"]


def test_late_spans_are_caught_by_the_one_re_read(monkeypatch):
    late = {"on": False}
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", 1000.0, 2)])  # list unchanged
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans() + (
        spans(loop=True)[2:] if late["on"] else []))
    filed = []
    monkeypatch.setattr(watch, "file_events", lambda send, dets: filed.extend(dets))
    state = {"seen": {}, "tokens": {}}
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=1100.0) == []
    late["on"] = True  # tool spans arrive late; the agent/LLM span list does not change
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=1300.0) == []  # before the re-read
    assert [d.kind for d in watch.cycle(state, "1h", 10, object(), False, 5, 5, now=1400.0)] == ["tool_loop"]
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=9000.0) == []  # only one re-read


def test_nested_agent_span_does_not_mark_trace_finished():
    sp = spans()
    sp.append({"op": "gen_ai.execute_tool", "span_id": "tool", "parent_span_id": "outer", "trace_id": "t",
               "data": {}})
    sp.append({"op": "gen_ai.invoke_agent", "span_id": "outer", "parent_span_id": None, "trace_id": "t",
               "data": {}})
    sp[0]["parent_span_id"] = "tool"  # "a" runs as a tool of "outer"
    assert watch.complete(sp)
    assert not watch.complete([x for x in sp if x["span_id"] != "outer"])  # only the nested agent has arrived


def test_grown_trace_is_judged_again_and_files_only_new_failures(monkeypatch):
    seq = {"last": 1000.0, "n": 2, "loop": False}
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", seq["last"], seq["n"])])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=seq["loop"]) + (
        [] if seq["loop"] else spans(loop=True)[2:]))
    filed = []
    monkeypatch.setattr(watch, "file_events", lambda send, dets: filed.extend(dets))
    state = {"seen": {}, "tokens": {}}
    watch.cycle(state, "1h", 10, object(), False, 5, 5, now=2000.0)  # 3 identical calls: a loop
    assert [d.kind for d in filed] == ["tool_loop"]
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=3000.0) == []  # nothing new arrived
    seq.update(last=2500.0, n=4, loop=True)  # late spans: same loop, nothing new to file
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5, now=3000.0) == [] and len(filed) == 1
    assert state["tokens"]["a"] == [["t", 100]]  # judged twice, counted once


def test_event_ids_are_stable_per_trace_and_failure():
    d = Detection("tool_loop", "x", "abc", "a", [], {"tool": "search"})
    assert watch.event_id(d) == watch.event_id(Detection("tool_loop", "other text", "abc", "a", ["s"],
                                                         {"tool": "search"}))
    assert watch.event_id(d) != watch.event_id(Detection("tool_loop", "x", "abd", "a", [], {"tool": "search"}))
    assert len(watch.event_id(d)) == 32


def test_sender_builds_an_envelope(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen.update(url=req.full_url, auth=req.headers["X-sentry-auth"], body=req.data)
        return Resp()

    monkeypatch.setattr(watch.urllib.request, "urlopen", fake_urlopen)
    watch.Sender("https://pub123@o1.ingest.example.com/42")({"event_id": "e" * 32, "message": "m"})
    assert seen["url"] == "https://o1.ingest.example.com/api/42/envelope/"
    assert "sentry_key=pub123" in seen["auth"]
    header, item, payload = seen["body"].split(b"\n")
    assert json.loads(header)["event_id"] == "e" * 32
    assert json.loads(item) == {"type": "event", "length": len(payload)}


def test_old_state_files_still_load(monkeypatch, tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"seen": {"t1": 9e12}, "tokens": {"a": [100, 110]}}))
    state = watch.load_state(str(p))
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", 5.0, 1)])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: (_ for _ in ()).throw(AssertionError))
    assert watch.cycle(state, "1h", 10, None, True, 5, 5) == []  # judged by the old version: left alone
    assert watch.cost_spike(state, spans(), 5, 2) is None and state["tokens"]["a"][-1] == ["t", 100]


def test_failed_filing_is_retried(monkeypatch):
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", 0.0, 1)])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=True))
    calls = []

    def flaky(send, dets):
        calls.append(len(dets))
        if len(calls) == 1:
            raise OSError("network down")

    monkeypatch.setattr(watch, "file_events", flaky)
    state = {"seen": {}, "tokens": {}}
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5) == [] and "t1" not in state["seen"]
    assert state["tokens"].get("a", []) == []  # the failed attempt did not count towards cost history
    assert watch.cycle(state, "1h", 10, object(), False, 5, 5) and "t1" in state["seen"] and len(calls) == 2


def test_dry_run_writes_no_state(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [("t1", "p", 0.0, 1)])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(loop=True))
    path = tmp_path / "state.json"
    assert watch.main(["--once", "--dry-run", "--state", str(path)]) == 0
    assert not path.exists()  # a later real run still files these traces


def test_cost_spike_uses_the_agents_own_history():
    state = {"seen": {}, "tokens": {}}
    for i, n in enumerate((100, 110, 90, 105, 95)):
        assert watch.cost_spike(state, spans(tokens=n, trace=f"r{i}"), 5, 5) is None  # building history
    assert watch.cost_spike(state, spans(tokens=300, trace="r5"), 5, 5) is None  # 3x is not a spike
    hit = watch.cost_spike(state, spans(tokens=2000, trace="r6"), 5, 5)
    assert hit and hit.kind == "cost_spike" and hit.agent == "a"
    assert watch.cost_spike(state, spans(agent="other", tokens=2000), 5, 5) is None  # no history yet


def test_events_carry_fingerprint_and_trace_link(monkeypatch):
    monkeypatch.setattr(watch.api, "env", lambda: {"SENTRY_ORG": "acme"})
    sent = []
    d = Detection("tool_loop", "tool search called 3x", "abc123", "a", ["t0"], {"tool": "search"})
    watch.file_events(sent.append, [d])
    ev = sent[0]
    assert ev["fingerprint"] == ["agent-failure", "tool_loop", "a", "search"]
    assert ev["contexts"]["agent_failure"]["trace_url"] == "https://acme.sentry.io/explore/traces/trace/abc123/"
    assert ev["event_id"] == watch.event_id(d) and ev["environment"] == "spanproof-watch"
    assert json.dumps(ev)  # serializable


def test_state_is_pruned(tmp_path):
    p = str(tmp_path / "s.json")
    watch.save_state(p, {"seen": {"old": 0, "new": 9e12}, "tokens": {"a": list(range(500))}})
    s = watch.load_state(p)
    assert list(s["seen"]) == ["new"] and len(s["tokens"]["a"]) == 200


def test_history_is_built_oldest_first(monkeypatch):
    # recent_traces returns newest first: five small new runs, then five large old runs.
    sizes = {f"n{i}": 100 for i in range(5)} | {f"o{i}": 1000 for i in range(5)}
    monkeypatch.setattr(watch, "recent_traces", lambda since, limit: [(t, "p", 0.0, 1) for t in sizes])
    monkeypatch.setattr(watch, "fetch_trace", lambda trace, project: spans(tokens=sizes[trace], trace=trace))
    dets = watch.cycle({"seen": {}, "tokens": {}}, "1h", 50, None, True, 5, 5)
    # judged in time order, the drop to 100 is not a spike and the old 1000s are not either
    assert [d for d in dets if d.kind == "cost_spike"] == []
