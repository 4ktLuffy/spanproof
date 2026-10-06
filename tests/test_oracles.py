"""Unit tests for the checks, on hand-built results.

Each check gets a positive case (it fires on a planted defect) and a negative
control (it stays quiet on a correct span), so a green run proves the check can
actually go red.
"""

from spanproof import oracles
from spanproof.fixtures import Truth

T = Truth(1200, 300, cached=1024, reasoning=256, model="m-1", response_id="r1", finish="stop").as_dict()


def span(op="gen_ai.chat", sid="s1", parent="root", trace="t1", finished=True, status="ok", **data):
    return {"op": op, "span_id": sid, "parent_span_id": parent, "trace_id": trace, "finished": finished,
            "status": status, "is_root": False, "start": 1.0, "description": op, "data": data}


ROOT = {"op": "spanproof.scenario", "span_id": "root", "parent_span_id": None, "trace_id": "t1", "finished": True,
        "status": "ok", "is_root": True, "start": 0.0, "description": "x", "data": {}}

GOOD = {"gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 300, "gen_ai.usage.total_tokens": 1500,
        "gen_ai.usage.cache_read.input_tokens": 1024, "gen_ai.usage.reasoning.output_tokens": 256,
        "gen_ai.response.model": "m-1", "gen_ai.response.id": "r1", "gen_ai.response.finish_reasons": "stop",
        "gen_ai.input.messages": "[]"}


def result(spans, calls=None, requests=1, data_collection=True, exception=None, agent=None):
    return {"scenario": "s", "integration": "i", "spans": [ROOT, *spans], "requests": [{}] * requests,
            "calls": calls if calls is not None else [{"truth": T, "op": "gen_ai.chat", "completes": True}],
            "data_collection": data_collection, "exception": exception, "expects_exception": False,
            "errors": [], "agent": agent}


def checks(findings, check):
    return [f for f in findings if f["check"] == check]


def test_usage_negative_control():
    assert checks(oracles.check_usage(result([span(**GOOD)])), "usage") == []


def test_usage_missing_cached_and_reasoning():
    d = dict(GOOD)
    del d["gen_ai.usage.cache_read.input_tokens"], d["gen_ai.usage.reasoning.output_tokens"]
    msgs = {f["message"].split()[0] for f in oracles.check_usage(result([span(**d)]))}
    assert msgs == {"cached", "reasoning"}


def test_usage_wrong_value_and_alias_accepted():
    d = dict(GOOD)
    del d["gen_ai.usage.cache_read.input_tokens"]
    d["gen_ai.usage.input_tokens.cached"] = 1024  # deprecated alias still counts as reported
    d["gen_ai.usage.input_tokens"] = 40  # Anthropic-style exclusive count
    f = oracles.check_usage(result([span(**d)]))
    assert [x["attribute"] for x in f] == ["gen_ai.usage.input_tokens"]
    assert f[0]["expected"] == 1200 and f[0]["actual"] == 40


def test_lifecycle_lost_span():
    r = result([], calls=[{"truth": None, "op": "gen_ai.chat", "completes": False}])
    assert len(checks(oracles.check_lifecycle(r), "lifecycle")) == 1


def test_lifecycle_negative_control():
    assert oracles.check_lifecycle(result([span(**GOOD)])) == []


def test_structure_split_trace_and_orphan():
    r = result([span(sid="a", trace="t2", **GOOD), span(sid="b", parent="ghost", **GOOD)])
    msgs = " ".join(f["message"] for f in oracles.check_structure(r))
    assert "2 traces" in msgs and "never sent" in msgs


def test_structure_agent_nesting():
    agent = span(op="gen_ai.invoke_agent", sid="ag")
    llm = span(sid="l1", parent="ag", **GOOD)
    tool = span(op="gen_ai.execute_tool", sid="t", parent="ag", **{"gen_ai.tool.name": "get_weather"})
    ok = result([agent, llm, tool], agent={"tools": ["get_weather"]})
    assert oracles.check_structure(ok) == []
    bad = result([agent, span(sid="l1", parent="root", **GOOD)], agent={"tools": ["get_weather"]})
    msgs = " ".join(f["message"] for f in oracles.check_structure(bad))
    assert "not inside the agent" in msgs and "no gen_ai.execute_tool" in msgs


def test_aggregation_double_count_and_rollup():
    agent = span(op="gen_ai.invoke_agent", sid="ag", **{"gen_ai.usage.input_tokens": 1500,
                                                        "gen_ai.agent.name": "a"})
    llm = span(sid="l1", parent="ag", **GOOD)
    f = oracles.check_aggregation(result([agent, llm]))
    assert any("double count" in x["message"] for x in f)
    assert any("sum to 1200" in x["message"] for x in f)  # agent says 1500, children say 1200


def test_aggregation_negative_control():
    assert oracles.check_aggregation(result([span(**GOOD)])) == []


def test_conventions_unknown_deprecated_type_and_subset():
    d = dict(GOOD, **{"gen_ai.not_a_real_attr": 1, "gen_ai.system": "openai",
                      "gen_ai.usage.output_tokens": "300"})
    d["gen_ai.usage.cache_read.input_tokens"] = 5000
    msgs = " ".join(f["message"] for f in oracles.check_conventions(result([span(**d)])))
    assert "not in sentry-conventions" in msgs
    assert "deprecated" in msgs
    assert "should be integer" in msgs
    assert "exceed input" in msgs


def test_conventions_negative_control():
    d = {k: v for k, v in GOOD.items() if k != "gen_ai.response.finish_reasons"}
    assert oracles.check_conventions(result([span(**d)])) == []


def test_privacy_leak_when_off_and_quiet_when_clean():
    assert checks(oracles.check_privacy(result([span(**GOOD)], data_collection=False)), "privacy")
    d = {k: v for k, v in GOOD.items() if k != "gen_ai.input.messages"}
    assert oracles.check_privacy(result([span(**d)], data_collection=False)) == []


def test_identity_finish_reason_and_model():
    d = {k: v for k, v in GOOD.items() if k != "gen_ai.response.finish_reasons"}
    d["gen_ai.response.model"] = "other"
    msgs = " ".join(f["message"] for f in oracles.check_identity(result([span(**d)])))
    assert "finish reason not recorded" in msgs and "model differs" in msgs
    assert oracles.check_identity(result([span(**GOOD)])) == []


def test_match_calls_prefers_response_id():
    a = span(sid="a", **dict(GOOD, **{"gen_ai.response.id": "other"}))
    b = span(sid="b", **GOOD)
    a["start"], b["start"] = 1.0, 2.0
    r = result([a, b])
    pairs = oracles.match_calls(r)
    assert pairs[0][1]["span_id"] == "b"


def test_lifecycle_counts_logical_calls_not_http_retries():
    # one call, the client retried twice at HTTP level: three requests, one span -> no finding
    r = result([span(**GOOD)], requests=3)
    assert oracles.check_lifecycle(r) == []
