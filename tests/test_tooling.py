"""Tests for the CI gate, the tox matrix parser and the privacy fingerprint."""

import json

from spanproof import fingerprint, gate
from spanproof.detectors import ARGS_HASH
from spanproof.matrix import tox_cells


def f(scenario="s", check="usage", attribute="gen_ai.usage.input_tokens", severity="high", message="x is 1"):
    return {"scenario": scenario, "check": check, "attribute": attribute, "severity": severity, "message": message,
            "mode": "default"}


def run(*fs):
    return {"results": [{"findings": list(fs)}]}


def test_gate_known_new_fixed_and_rewording(tmp_path):
    base = tmp_path / "b.json"
    r1 = tmp_path / "r1.json"
    json.dump(run(f(), f(scenario="t", check="lifecycle", attribute=None)), open(r1, "w"))
    assert gate.main([str(r1), "--write-baseline", str(base)]) == 0
    # same findings, reworded and renumbered: still known
    r2 = tmp_path / "r2.json"
    json.dump(run(f(message="x is now 2"), f(scenario="t", check="lifecycle", attribute=None, message="y")),
              open(r2, "w"))
    assert gate.main([str(r2), "--baseline", str(base)]) == 0
    # one fixed, one new
    r3 = tmp_path / "r3.json"
    json.dump(run(f(), f(scenario="u")), open(r3, "w"))
    assert gate.main([str(r3), "--baseline", str(base)]) == 1


def test_gate_expired_entries_stop_suppressing(tmp_path):
    base = tmp_path / "b.json"
    json.dump({"known": {gate.signature(f()): {"message": "x", "until": "2000-01-01"}}}, open(base, "w"))
    r = tmp_path / "r.json"
    json.dump(run(f()), open(r, "w"))
    assert gate.main([str(r), "--baseline", str(base)]) == 1


def test_gate_ignores_low_by_default(tmp_path):
    r = tmp_path / "r.json"
    json.dump(run(f(severity="low")), open(r, "w"))
    assert gate.main([str(r)]) == 0


TOX = """
[tox]
envlist =
    {py3.8,py3.11,py3.12}-openai-base-v1.0.1
    {py3.10,py3.13,py3.14,py3.14t}-openai-base-v2.54.0

[testenv]
deps =
    openai-base-v1.0.1: openai==1.0.1
    py3.12-openai-base-v1.0.1: httpx==0.27.2
    py3.12-openai-base-v1.0.1: pytest==8.0.0
    py3.11-openai-base-v1.0.1: httpx==0.26.0
    openai-base-v2.54.0: openai==2.54.0
    py3.13-openai-base-v2.54.0: anyio==4.15.1
"""


def test_tox_cells_pick_python_and_pins(tmp_path):
    p = tmp_path / "tox.ini"
    p.write_text(TOX)
    cells = {(c["env"], c["requested"]): c for c in tox_cells(str(p))}
    c1 = cells[("openai-base", "1.0.1")]
    assert c1["python"] == "3.12"
    assert c1["pins"] == ["openai==1.0.1", "httpx==0.27.2"]  # pytest pins dropped, py3.11 pins ignored
    c2 = cells[("openai-base", "2.54.0")]
    assert c2["python"] == "3.13" and "anyio==4.15.1" in c2["pins"]
    assert ("openai-base", "latest") in cells and ("openai-base", "pre") in cells


def test_fingerprint_strips_content_and_hashes_equal_args_equally():
    def tool(args):
        return {"op": "gen_ai.execute_tool", "data": {"gen_ai.tool.call.arguments": args,
                                                      "gen_ai.tool.call.result": "secret"}}

    row = {"spans": [tool('{"b": 1, "a": 2}'), tool('{"a":2,"b":1}'), tool('{"a": 3}')]}
    out = fingerprint.transform(row)
    hashes = [s["data"][ARGS_HASH] for s in out["spans"]]
    assert hashes[0] == hashes[1] != hashes[2]
    for s in out["spans"]:
        assert "gen_ai.tool.call.arguments" not in s["data"] and "gen_ai.tool.call.result" not in s["data"]


def test_otlp_adapter_feeds_detectors_and_events_group():
    from spanproof.issues import from_otlp, to_event
    from spanproof.detectors import detect_trace

    def sp(sid, parent, name, start, **attrs):
        return {"traceId": "t", "spanId": sid, "parentSpanId": parent, "name": name,
                "startTimeUnixNano": str(start * 10**9),
                "attributes": [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else
                                {"stringValue": v}} for k, v in attrs.items()]}

    spans = [sp("a", "", "invoke_agent", 0, **{"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "x"})]
    for i in range(3):
        spans.append(sp(f"t{i}", "a", "execute_tool", i + 1, **{"gen_ai.operation.name": "execute_tool",
                                                                 "gen_ai.tool.name": "search",
                                                                 "gen_ai.tool.call.arguments": '{"q": 1}'}))
    spans.append(sp("c", "a", "chat", 9, **{"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 10}))
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
    dets = detect_trace(from_otlp(doc))
    assert [d.kind for d in dets] == ["tool_loop"]
    ev = to_event(dets[0])
    assert ev["fingerprint"] == ["agent-failure", "tool_loop", "x", "search"]


def test_unsupported_vs_sdk_bug_by_innermost_frame():
    from spanproof.runner import raised_in_sentry_sdk

    provider = ('File "/x/sentry_sdk/integrations/openai.py", line 862, in wrap\n'
                'File "/x/site-packages/openai/_utils/_utils.py", line 299, in wrapper\n'
                "TypeError: create() got an unexpected keyword argument 'stream_options'")
    sdk = ('File "/x/site-packages/openai/resources/chat.py", line 10, in create\n'
           'File "/x/sentry_sdk/integrations/openai.py", line 900, in _set_input\n'
           "TypeError: 'NoneType' object is not subscriptable")
    assert raised_in_sentry_sdk(provider) is False
    assert raised_in_sentry_sdk(sdk) is True


def test_text_length_fingerprint_decides_empty_without_content():
    from spanproof.detectors import detect_trace

    ag = {"op": "gen_ai.invoke_agent", "span_id": "a", "parent_span_id": None, "trace_id": "t", "status": "ok",
          "start": 0, "data": {"gen_ai.agent.name": "x"}}

    def chat(text):
        return {"op": "gen_ai.chat", "span_id": "c", "parent_span_id": "a", "trace_id": "t", "status": "ok",
                "start": 1, "data": {"gen_ai.response.finish_reasons": '["stop"]', "gen_ai.response.text": text}}

    empty = fingerprint.transform({"spans": [ag, chat('[""]')]})["spans"]
    full = fingerprint.transform({"spans": [dict(ag), chat('["Paris."]')]})["spans"]
    assert "gen_ai.response.text" not in empty[1]["data"]
    assert [d.kind for d in detect_trace(empty)] == ["empty_answer"]
    assert detect_trace(full) == []
