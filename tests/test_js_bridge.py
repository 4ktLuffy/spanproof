"""The JavaScript bridge: every scenario has a worker, jobs carry their setup, Gemini fixtures parse."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from spanproof import fixtures as fx
from spanproof import js_bridge

WORKER = (js_bridge.JS / "worker.cjs").read_text()


def worker_scenarios() -> set[str]:
    return set(re.findall(r"^  '(js\.[^']+)': async", WORKER, re.MULTILINE))


def test_every_bridge_scenario_has_a_worker_function():
    for sid, entry in js_bridge.JS_SCENARIOS.items():
        opts = entry[3] if len(entry) > 3 else {}
        assert opts.get("runs", sid) in worker_scenarios(), sid


def test_every_named_integration_exists_in_the_worker_contract():
    # the worker builds Sentry[name + 'Integration'](); names are @sentry/node exports minus the suffix
    known = {"openAI", "anthropicAI", "vercelAI", "langChain", "langGraph", "googleGenAI"}
    for sid, entry in js_bridge.JS_SCENARIOS.items():
        opts = entry[3] if len(entry) > 3 else {}
        assert set(opts.get("integrations", [])) <= known, sid


def test_langchain_comes_first_like_sentry_node_defaults():
    # LangChain must set up before the providers so it can skip their spans (server-utils integrations/index.js)
    assert js_bridge.AI_DEFAULT[0] == "langChain"


def test_job_carries_integrations_agent_and_alias(monkeypatch, tmp_path):
    seen = {}

    def fake_run(args, **kw):
        seen["job"] = json.loads(Path(args[2]).read_text())
        out = {"spans": [], "errors": [], "transactions": 0, "exception": None, "requests": [], "versions": {},
               "data_collection": "--no-data-collection" not in args, "node": "v0"}
        return subprocess.CompletedProcess(args, 0, "@@SPANPROOF-RESULT@@" + json.dumps(out), "")

    monkeypatch.setattr(js_bridge.subprocess, "run", fake_run)
    r = js_bridge.run_one("js.langgraph.react_agent.without_langchain", "default")
    assert seen["job"]["scenario"] == "js.langgraph.react_agent"  # the worker function it reuses
    assert seen["job"]["integrations"] == ["langGraph", "openAI"]
    assert r["scenario"] == "js.langgraph.react_agent.without_langchain" and r["agent"] == js_bridge.AGENT
    js_bridge.run_one("js.openai.chat.sync", "nodc")
    assert "integrations" not in seen["job"]  # original scenarios keep the worker's default three


def test_genai_fixtures_parse_and_truth_counts_thoughts_as_output():
    pytest.importorskip("google.genai")
    body, t = fx.genai_response(rid="gen_x")
    fx.validate("genai_response", body)
    assert t.output_tokens == 200 + 150 and t.reasoning == 150 and t.response_id == "gen_x"
    events, st = fx.genai_stream()
    for e in events:
        fx.validate("genai_response", e)
    last = events[-1]["usageMetadata"]
    assert st.input_tokens == last["promptTokenCount"]
    assert st.output_tokens == last["candidatesTokenCount"] + last["thoughtsTokenCount"]
    assert st.total == last["totalTokenCount"]
