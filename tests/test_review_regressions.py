"""One regression test per issue from the second-model code review (review/codex-code-review.md)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_detectors import AG, chat, kinds, tool  # noqa: E402
from test_oracles import GOOD, T, result, span  # noqa: E402

from spanproof import capture, gate, oracles  # noqa: E402
from spanproof.detectors import detect_cost_spikes  # noqa: E402
from spanproof.fixtures import Truth  # noqa: E402
from spanproof.matrix import tox_cells  # noqa: E402


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


# 1 -------------------------------------------------------------------------
def test_gate_fails_on_crash_install_error_and_empty_run(tmp_path):
    crashed = _write(tmp_path, "a.json", {"results": [{"scenario": "s", "crashed": True, "stderr": "x"}]})
    install = _write(tmp_path, "b.json", {"cells": [{"env": "e", "requested": "1", "install_error": "x",
                                                      "results": []}]})
    empty = _write(tmp_path, "c.json", {"results": []})
    assert gate.main([crashed]) == 2 and gate.main([install]) == 2 and gate.main([empty]) == 2


def test_gate_does_not_call_unrun_findings_fixed(tmp_path):
    f = {"scenario": "s", "rule": "usage.missing.cached", "check": "usage", "severity": "medium",
         "attribute": "a", "call": 0, "message": "m", "mode": "default"}
    base = tmp_path / "base.json"
    ok = _write(tmp_path, "ok.json", {"results": [{"scenario": "s", "mode": "default", "findings": [f]}]})
    assert gate.main([ok, "--write-baseline", str(base)]) == 0
    other = _write(tmp_path, "o.json", {"results": [{"scenario": "t", "mode": "default", "findings": []}]})
    assert gate.main([other, "--baseline", str(base)]) == 0  # s did not run: unchecked, not fixed


# 2 -------------------------------------------------------------------------
def test_distinct_structure_rules_have_distinct_signatures():
    orphan = {"scenario": "s", "rule": "structure.orphan", "check": "structure", "attribute": "parent_span_id",
              "severity": "high", "call": None}
    outside = dict(orphan, rule="structure.llm_outside_agent")
    assert gate.signature(orphan) != gate.signature(outside)
    assert gate.signature(orphan, "openai@1.0") != gate.signature(orphan, "openai@2.0")


# 3 -------------------------------------------------------------------------
def test_spurious_tokens_with_zero_truth_are_reported_not_crashing():
    r = result([span(**{"gen_ai.usage.cache_read.input_tokens": 1})],
               calls=[{"truth": Truth(1, 1).as_dict(), "completes": True}])
    f = oracles.check_aggregation(r)
    assert any(x["rule"] == "aggregation.double_count.cached" for x in f)


# 4 -------------------------------------------------------------------------
def test_malformed_child_usage_does_not_crash_rollup():
    a = span(op="gen_ai.invoke_agent", sid="a", **{"gen_ai.usage.input_tokens": 1200})
    c = span(parent="a", **dict(GOOD, **{"gen_ai.usage.input_tokens": "1200"}))
    rules = {x["rule"] for x in oracles.run_all(result([a, c]))}
    assert "usage.wrong.input_tokens" in rules and "conventions.type" in rules


# 5 -------------------------------------------------------------------------
def test_exact_response_id_match_is_not_stolen():
    ta = dict(T, response_id="A")
    tb = dict(T, response_id="B")
    b = span(sid="b", **dict(GOOD, **{"gen_ai.response.id": "B"}))
    a = span(sid="a", **{k: v for k, v in GOOD.items() if k != "gen_ai.response.id"})
    b["start"], a["start"] = 1.0, 2.0
    r = result([b, a], calls=[{"truth": ta, "completes": True}, {"truth": tb, "completes": True}])
    pairs = dict(oracles.match_calls(r))
    assert pairs[0]["span_id"] == "a" and pairs[1]["span_id"] == "b"


# 6 -------------------------------------------------------------------------
def test_parent_cycles_are_reported_not_looped():
    a = span(op="gen_ai.invoke_agent", sid="a", parent="a", **{"gen_ai.usage.input_tokens": 10})
    c = span(sid="c", parent="a", **GOOD)
    r = result([a, c], agent={"tools": []})
    rules = {x["rule"] for x in oracles.check_structure(r)}
    assert "structure.cycle" in rules
    oracles.check_rollup(r)  # must return
    kinds([dict(AG, parent_span_id="ag"), chat("c", 1)])  # detectors must return too


# 7 -------------------------------------------------------------------------
def test_every_value_is_type_checked():
    r = result([span(**{"gen_ai.response.model": "m"}), span(sid="s2", **{"gen_ai.response.model": 123})])
    assert any(x["rule"] == "conventions.type" for x in oracles.check_conventions(r))


# 8 -------------------------------------------------------------------------
def test_identity_compares_values():
    d = dict(GOOD, **{"gen_ai.response.finish_reasons": '["length"]', "gen_ai.response.id": "wrong"})
    rules = {x["rule"] for x in oracles.check_identity(result([span(**d)]))}
    assert {"identity.finish_wrong", "identity.response_id_wrong"} <= rules
    empty = dict(GOOD, **{"gen_ai.response.finish_reasons": "", "gen_ai.response.model": None})
    rules = {x["rule"] for x in oracles.check_identity(result([span(**empty)]))}
    assert {"identity.finish_missing", "identity.model_missing"} <= rules


# 9 -------------------------------------------------------------------------
def test_successful_structured_result_is_not_an_error_regardless_of_key_order():
    assert "silent_tool_error" not in kinds([AG, tool("t", 1, out='{"error":null,"ok":true}'), chat("c", 2)])


# 10 ------------------------------------------------------------------------
def test_unrecorded_answer_is_not_evidence_of_silence():
    c = chat("c", 2)
    del c["data"]["gen_ai.response.text"]
    assert "silent_tool_error" not in kinds([AG, tool("t", 1, out="TimeoutError: failed"), c])


# 11 ------------------------------------------------------------------------
def test_short_and_structured_answers_are_not_empty():
    assert "empty_answer" not in kinds([AG, chat("c", 1, text="42")])
    assert "empty_answer" not in kinds([AG, chat("c", 1, text='{"answer": 42}')])
    c = chat("c", 1, **{"gen_ai.usage.output_tokens": 1})
    del c["data"]["gen_ai.response.text"]
    assert "empty_answer" not in kinds([AG, c])


# 12 ------------------------------------------------------------------------
def test_cost_spike_reads_usage_aliases():
    def run(n):
        return [AG, chat("c", 1, **{"gen_ai.usage.prompt_tokens": n, "gen_ai.usage.completion_tokens": 1})]

    assert set(detect_cost_spikes([run(100)] * 5 + [run(10000)])) == {5}


# 13 ------------------------------------------------------------------------
def test_byte_backed_envelope_items_are_decoded():
    from sentry_sdk.envelope import Envelope, Item, PayloadRef

    txn = {"type": "transaction", "transaction": "t", "timestamp": 2.0, "start_timestamp": 1.0, "spans": [],
           "contexts": {"trace": {"trace_id": "t1", "span_id": "s1", "op": "x"}}}
    env = Envelope(items=[Item(payload=PayloadRef(bytes=json.dumps(txn).encode()), type="transaction")])
    t = capture.MemoryTransport()
    t.capture_envelope(env)
    assert capture.flatten(t)["transactions"] == 1


# 14 ------------------------------------------------------------------------
def test_tox_pins_with_comments_and_missing_pins(tmp_path):
    ok = tmp_path / "ok.ini"
    ok.write_text("envlist =\n    {py3.12}-openai-base-v1.0.1\ndeps =\n    openai-base-v1.0.1: openai==1.0.1 # pinned\n")
    (cell,) = [c for c in tox_cells(str(ok)) if c["requested"] == "1.0.1"]
    assert cell["pins"] == ["openai==1.0.1"]
    bad = tmp_path / "bad.ini"
    bad.write_text("envlist =\n    {py3.12}-openai-base-v1.0.1\ndeps =\n    py3.12-openai-base-v1.0.1: httpx==1\n")
    try:
        tox_cells(str(bad))
    except ValueError:
        return
    raise AssertionError("a versioned cell without a provider pin must be refused")
