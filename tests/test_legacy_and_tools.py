"""Checks added for the Cohere, Mistral and Hugging Face scenarios: legacy ai.* spans, backfilled aliases,
hidden failures, streaming flag and tool calls. Each has a positive case and a negative control."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_oracles import GOOD, T, result, span

from spanproof import oracles
from spanproof.fixtures import Truth

COHERE = {"gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 300, "gen_ai.usage.total_tokens": 1500,
          "ai.generation_id": "r1", "ai.finish_reason": "stop", "ai.input_messages": "[]", "ai.streaming": False}


def rules(findings):
    return {f["rule"] for f in findings}


def test_legacy_ai_op_is_a_client_span_and_reported():
    t = dict(T, cached=0, reasoning=0, model="")
    r = result([span(op="ai.chat_completions.create.cohere", **COHERE)], calls=[{"truth": t, "completes": True}])
    got = rules(oracles.run_all(r))
    assert "lifecycle.lost_span" not in got and "conventions.legacy_op" in got
    assert not any(x.startswith(("usage.", "identity.")) for x in got)  # backfilled aliases count as recorded
    assert "conventions.legacy_op" not in rules(oracles.run_all(result([span(**GOOD)])))


def test_legacy_content_keys_leak_with_data_collection_off():
    r = result([span(op="ai.chat_completions.create.cohere", **COHERE)], data_collection=False)
    assert "privacy.content_leak" in rules(oracles.check_privacy(r))
    d = {k: v for k, v in COHERE.items() if k != "ai.input_messages"}
    r = result([span(op="ai.chat_completions.create.cohere", **d)], data_collection=False)
    assert "privacy.content_leak" not in rules(oracles.check_privacy(r))


def test_legacy_keys_are_deprecated_but_not_type_checked():
    r = result([span(op="ai.chat_completions.create.cohere", **dict(COHERE, **{"ai.responses": '["x"]'}))])
    f = oracles.check_conventions(r)
    assert any(x["rule"] == "conventions.deprecated" and x["attribute"] == "ai.responses" for x in f)
    assert not any(x["rule"] == "conventions.type" and x["attribute"].startswith("ai.") for x in f)


def test_swallowed_failure_is_high_and_replaces_expected_raise():
    r = dict(result([span(**GOOD)]), expects_exception=True, raises="httpx.RemoteProtocolError")
    f = oracles.check_errors(r)
    assert [x["rule"] for x in f] == ["errors.swallowed"] and f[0]["severity"] == "high"
    r["exception"] = {"type": "RemoteProtocolError", "value": "x"}
    assert "errors.swallowed" not in rules(oracles.check_errors(r))
    r = dict(result([span(**GOOD)]), expects_exception=True)  # no control: stays the low hygiene rule
    assert rules(oracles.check_errors(r)) == {"errors.expected_raise"}


def test_streaming_flag():
    r = dict(result([span(**dict(GOOD, **{"gen_ai.response.streaming": False}))]), tags=["stream"])
    assert "identity.streaming_wrong" in rules(oracles.check_identity(r))
    r = dict(result([span(**dict(GOOD, **{"gen_ai.response.streaming": True}))]), tags=["stream"])
    assert "identity.streaming_wrong" not in rules(oracles.check_identity(r))
    r = dict(result([span(**dict(GOOD, **{"gen_ai.response.streaming": True}))]), tags=[])
    assert "identity.streaming_wrong" in rules(oracles.check_identity(r))
    r = dict(result([span(**dict(GOOD, **{"gen_ai.response.streaming": True}))]), tags=None)  # JS bridge: no tags
    assert "identity.streaming_wrong" not in rules(oracles.check_identity(r))


def _tool_result(**data):
    t = Truth(1200, 300, cached=1024, reasoning=256, model="m-1", response_id="r1", finish="stop",
              tool_calls=[["get_weather", {"city": "Paris"}]]).as_dict()
    return result([span(**dict(GOOD, **data))], calls=[{"truth": t, "completes": True}])


def ident(**data):
    return rules(oracles.check_identity(_tool_result(**data)))


def test_tool_call_shapes_and_fragments():
    args = '{\\"city\\": \\"Paris\\"}'
    whole = '[{"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "%s"}}]' % args
    assert not ident(**{"gen_ai.response.tool_calls": whole}) & {"identity.tool_call_wrong",
                                                                 "identity.tool_call_missing"}
    parts = ('[{"role": "assistant", "parts": [{"type": "tool_call", "name": "get_weather", '
             '"arguments": {"city": "Paris"}}]}]')
    assert "identity.tool_call_wrong" not in ident(**{"gen_ai.output.messages": parts})
    cohere = '[{"name": "get_weather", "parameters": {"city": "Paris"}}]'
    for k, v in (("gen_ai.response.tool_calls", whole), ("gen_ai.output.messages", parts), ("ai.tool_calls", cohere)):
        assert oracles.recorded_tool_calls({k: v}) == [["get_weather", {"city": "Paris"}]]  # parsed, not skipped
    assert "identity.tool_call_wrong" not in ident(**{"ai.tool_calls": cohere})
    last_fragment = '[{"function": {"arguments": "ris\\"}", "name": "None"}, "id": "c1", "type": "function"}]'
    assert "identity.tool_call_wrong" in ident(**{"gen_ai.response.tool_calls": last_fragment})


def test_tool_call_missing_only_when_outputs_recorded():
    text_only = '[{"role": "assistant", "parts": [{"type": "text", "content": ""}]}]'
    assert "identity.tool_call_missing" in ident(**{"gen_ai.output.messages": text_only})
    assert "identity.tool_call_missing" not in ident()


def test_tool_calls_recorded_in_two_attributes_count_once():
    call = '[{"name": "get_weather", "arguments": "{\\"city\\": \\"Paris\\"}"}]'
    msgs = '[{"role": "assistant", "parts": [{"type": "tool_call", "name": "get_weather", "arguments": {"city": "Paris"}}]}]'
    d = {"gen_ai.response.tool_calls": call, "gen_ai.output.messages": msgs}
    assert oracles.recorded_tool_calls(d) == [["get_weather", {"city": "Paris"}]]


def test_langchain_js_args_are_read():
    d = {"gen_ai.response.tool_calls": '[{"name": "get_weather", "args": {"city": "Rome"}, "type": "tool_call"}]'}
    assert oracles.recorded_tool_calls(d) == [["get_weather", {"city": "Rome"}]]
