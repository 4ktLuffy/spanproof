"""Unit tests for the failure-class detectors, with hard negatives for each."""

import json

from spanproof.detectors import detect_cost_spikes, detect_trace


def s(op, sid, parent="ag", start=1.0, status="ok", desc=None, **data):
    return {"op": op, "span_id": sid, "parent_span_id": parent, "trace_id": "t", "status": status,
            "start": start, "description": desc or op, "data": data}


AG = s("gen_ai.invoke_agent", "ag", parent=None, start=0.0, **{"gen_ai.agent.name": "a"})


def kinds(spans):
    return {d.kind for d in detect_trace(spans)}


def tool(sid, start, name="search", args=None, out="ok"):
    return s("gen_ai.execute_tool", sid, start=start, **{"gen_ai.tool.name": name,
                                                         "gen_ai.tool.call.arguments": json.dumps(args or {}),
                                                         "gen_ai.tool.call.result": out})


def chat(sid, start, finish="stop", text="done", **extra):
    return s("gen_ai.chat", sid, start=start, **{"gen_ai.response.finish_reasons": json.dumps([finish]),
                                                 "gen_ai.response.text": text, **extra})


def test_tool_loop_vs_pagination():
    loop = [AG] + [tool(f"t{i}", i, args={"q": "x"}) for i in range(3)] + [chat("c", 9)]
    pages = [AG] + [tool(f"t{i}", i, args={"q": "x", "page": i}) for i in range(4)] + [chat("c", 9)]
    assert "tool_loop" in kinds(loop)
    assert "tool_loop" not in kinds(pages)


def test_silent_tool_error_vs_benign_text():
    bad = [AG, tool("t", 1, out='{"error": "upstream 503"}'), chat("c", 2)]
    benign = [AG, tool("t", 1, out="No errors found in the last 24h"), chat("c", 2)]
    assert "silent_tool_error" in kinds(bad)
    assert "silent_tool_error" not in kinds(benign)


def test_silent_requires_the_run_to_continue():
    aborted = [dict(AG, status="internal_error"), tool("t", 1, out='{"error": "x"}'), chat("c", 2)]
    assert "silent_tool_error" not in kinds(aborted)


def http(sid, start, code, path="/v1/chat/completions"):
    return s("http.client", sid, parent="ag", start=start, desc=f"POST http://h{path}",
             **{"http.response.status_code": code})


def test_retry_storm_vs_single_retry():
    storm = [AG, http("h1", 1, 500), http("h2", 2, 500), http("h3", 3, 503), http("h4", 4, 200), chat("c", 5)]
    one = [AG, http("h1", 1, 500), http("h2", 2, 200), chat("c", 3)]
    assert "retry_storm" in kinds(storm)
    assert "retry_storm" not in kinds(one)


def test_lost_llm_span():
    lost = [AG, http("h1", 1, 200), http("h2", 2, 200), chat("c", 3)]
    fine = [AG, http("h1", 1, 200), chat("c", 3)]
    other_host = [AG, http("h1", 1, 200, path="/api/users"), chat("c", 3)]
    assert "lost_llm_span" in kinds(lost)
    assert "lost_llm_span" not in kinds(fine)
    assert "lost_llm_span" not in kinds(other_host)


def test_dead_end_truncated_empty():
    assert "dead_end" in kinds([AG, tool("t", 1), chat("c", 0.5, finish="tool_calls")])
    assert "truncated_answer" in kinds([AG, chat("c", 1, finish="length", text="The weather is")])
    assert "empty_answer" in kinds([AG, chat("c", 1, text='[""]')])
    assert kinds([AG, chat("c", 1, text="A long and complete answer.")]) == set()


def test_cost_spike_vs_2x():
    def run(tokens):
        return [AG, chat("c", 1, **{"gen_ai.usage.input_tokens": tokens, "gen_ai.usage.output_tokens": 10})]

    traces = [run(1000 + 10 * i) for i in range(8)] + [run(2100), run(20000)]
    spikes = detect_cost_spikes(traces)
    assert set(spikes) == {9}


# ---- regressions from the Codex held-out set (v1)

def test_python_repr_finish_reasons_are_parsed():
    c = chat("c", 1)
    c["data"]["gen_ai.response.finish_reasons"] = "['length']"
    assert "truncated_answer" in kinds([AG, c])


def test_whitespace_only_answer_is_empty():
    c = chat("c", 1, text='[" \\n\\t "]')
    assert "empty_answer" in kinds([AG, c])


def test_exception_style_tool_output_is_an_error():
    bad = [AG, tool("t", 1, out="RuntimeError: inventory database disconnected"), chat("c", 2, text="All good.")]
    assert "silent_tool_error" in kinds(bad)


def test_acknowledged_failure_is_not_silent():
    t = tool("t", 1, out="TimeoutError: inventory service did not respond")
    ok = [AG, t, chat("c", 2, text='["The lookup timed out, so I cannot confirm availability."]')]
    assert "silent_tool_error" not in kinds(ok)


def test_loops_are_counted_per_agent_scope():
    inner = s("gen_ai.invoke_agent", "in", parent="ag", start=5, **{"gen_ai.agent.name": "worker"})
    outer_calls = [tool(f"o{i}", i, args={"sku": "A"}) for i in range(2)]
    inner_calls = [dict(tool(f"i{i}", 6 + i, args={"sku": "A"}), parent_span_id="in") for i in range(2)]
    assert "tool_loop" not in kinds([AG, inner, *outer_calls, *inner_calls, chat("c", 9)])


def test_structured_error_payload_anywhere_in_the_object():
    out = '{"ok":false,"error":{"code":"ACCESS_DENIED","message":"Cannot read audit record"}}'
    bad = [AG, tool("t", 1, out=out), chat("c", 2, text='["Verified: approved."]')]
    assert "silent_tool_error" in kinds(bad)
    fine = [AG, tool("t", 1, out='{"ok": true, "error": null, "items": 3}'), chat("c", 2)]
    assert "silent_tool_error" not in kinds(fine)


# ---- boundary cases added after mutation testing (these mutants were not caught by any test)

def _silent(out):
    return "silent_tool_error" in kinds([AG, tool("t", 1, out=out), chat("c", 2, text='["Done, all good."]')])


def test_structured_error_variants_each_count_and_empty_error_values_do_not():
    for bad in ('{"error": "boom"}', '{"ok": false}', '{"success": false}', '{"status": "ERROR"}',
                '{"status": "failed"}', '{"status": "Failure"}'):
        assert _silent(bad), bad
    for fine in ('{"error": null}', '{"error": false}', '{"error": ""}', '{"error": {}}', '{"error": []}',
                 '{"ok": true}', '{"success": true}', '{"status": "ok"}', '{"status": 200}', '[{"error": "x"}]'):
        assert not _silent(fine), fine


def test_lost_llm_span_counts_http_399_but_not_400():
    assert "lost_llm_span" in kinds([AG, http("h1", 1, 200), http("h2", 2, 399), chat("c", 3)])
    assert "lost_llm_span" not in kinds([AG, http("h1", 1, 200), http("h2", 2, 400), chat("c", 3)])


def _agent_run(tokens):
    return [AG, chat("c", 1, **{"gen_ai.usage.input_tokens": tokens, "gen_ai.usage.output_tokens": 0})]


def test_cost_spike_threshold_is_strictly_greater_than_factor_times_median():
    peers = [_agent_run(100) for _ in range(5)]
    assert detect_cost_spikes(peers + [_agent_run(500)]) == {}  # exactly 5x the median: not a spike
    assert set(detect_cost_spikes(peers + [_agent_run(501)])) == {5}


def test_cost_spike_needs_enough_peers():
    run = _agent_run
    # the minimum is min_peers runs of the agent: the outlier plus min_peers-1 peers
    assert set(detect_cost_spikes([run(100)] * 4 + [run(9999)])) == {4}
    assert detect_cost_spikes([run(100)] * 3 + [run(9999)]) == {}  # one run short
    # a peer with zero tokens does not count towards the peers, so only 3 are left
    assert detect_cost_spikes([run(0), run(100), run(100), run(100), run(9999)]) == {}
    assert set(detect_cost_spikes([run(0), run(100), run(100), run(100), run(100), run(9999)])) == {5}


def test_silent_tool_error_needs_the_answer_to_come_after_the_failed_tool():
    after = [AG, tool("t", 1, out='{"error": "x"}'), chat("c", 2, text='["Done, all good."]')]
    before = [AG, chat("c", 1, text='["Done, all good."]'), tool("t", 2, out='{"error": "x"}')]
    same_time = [AG, tool("t", 1, out='{"error": "x"}'), chat("c", 1, text='["Done, all good."]')]
    stopped = [AG, tool("t", 1, out='{"error": "x"}'), chat("c", 2, finish="tool_calls", text='["Done."]')]
    assert "silent_tool_error" in kinds(after)
    for case in (before, same_time, stopped):
        assert "silent_tool_error" not in kinds(case)


def test_dead_end_after_an_errored_run_needs_a_tool_started_after_the_last_answer():
    failed_agent = dict(AG, status="internal_error")
    late_tool = [failed_agent, chat("c", 1, text='["partial"]'), tool("t", 2)]
    early_tool = [failed_agent, tool("t", 1), chat("c", 2, text='["partial"]')]
    same_time = [failed_agent, tool("t", 1), chat("c", 1, text='["partial"]')]
    assert "dead_end" in kinds(late_tool)
    assert "dead_end" not in kinds(early_tool) and "dead_end" not in kinds(same_time)
    assert "dead_end" not in kinds([AG, chat("c", 1, text='["partial"]'), tool("t", 2)])  # the run did not fail
