"""Run the JavaScript scenarios (js/worker.cjs) with the SAME fixtures and truth as Python.

    python -m spanproof.js_bridge [--only js.openai.] [--modes default,nodc] [--out results/js.json]

Each JS scenario mirrors a Python one; the pairing lets the report show where the
two SDKs disagree on the same provider response (cross-language parity).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from . import fixtures as fx, oracles
from .scenario import Call

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "js"

_c, _ct = fx.openai_chat()
_cs, _cst = fx.openai_chat_stream()
_tc, _tct = fx.openai_chat(rid="chatcmpl-sp-tool", tool_calls=fx.openai_tool_call(), completion=40, reasoning=0)
_r, _rt = fx.openai_response()
_rs, _rsn, _rst = fx.openai_response_stream(rid="resp_sp2")
_m, _mt = fx.anthropic_message()
_ms, _msn, _mst = fx.anthropic_stream()
_v, _vt = fx.openai_chat(rid="chatcmpl-vercel", cached=0)
_d, _dt = fx.openai_chat(rid="chatcmpl-direct", prompt=60, completion=8, cached=0, reasoning=0)
_d2, _d2t = fx.openai_chat(rid="chatcmpl-direct2", prompt=70, completion=9, cached=0, reasoning=0)
_g, _gt = fx.genai_response()
_g2, _g2t = fx.genai_response(rid="gen_sp3", text="Rome.", prompt=950, cand=20, cached=0, thoughts=40)
_gs, _gst = fx.genai_stream()

# Agent turns (tool call, then answer), shared with the Python agent scenarios.
_a1, _a1t = fx.openai_chat(rid="chatcmpl-ag1", tool_calls=fx.openai_tool_call(), prompt=800, completion=30,
                           cached=512, reasoning=0)
_a2, _a2t = fx.openai_chat(rid="chatcmpl-ag2", content="It is sunny in Paris.", prompt=900, completion=20,
                           cached=768, reasoning=0)
_a3, _a3t = fx.openai_chat(rid="chatcmpl-ag3", content="Also sunny.", prompt=1000, completion=12, cached=896,
                           reasoning=0)
_aa1, _aa1t = fx.anthropic_message(rid="msg_ag1", inp=30, out=25, cache_read=1024, cache_creation=0,
                                   tool_use={"type": "tool_use", "id": "toolu_ag1", "name": "get_weather",
                                             "input": {"city": "Paris"}})
_aa2, _aa2t = fx.anthropic_message(rid="msg_ag2", text="It is sunny in Paris.", inp=60, out=15, cache_read=1024,
                                   cache_creation=0)
AGENT = {"tools": ["get_weather"], "agent_name": "weather_agent"}
# @sentry/node's default AI integrations, in its own order (LangChain first, so it can skip provider spans).
AI_DEFAULT = ["langChain", "langGraph", "vercelAI", "openAI", "anthropicAI", "googleGenAI"]
LC = {"integrations": AI_DEFAULT}
LG = {"integrations": AI_DEFAULT, "agent": AGENT}


def R(body=None, events=None, names=None, done=True):
    return {"body": body, "events": events, "sse_event_names": names, "sse_done": done}


# id -> (replies, calls, python twin[, {"integrations": [...], "agent": {...}, "runs": worker scenario}])
JS_SCENARIOS = {
    "js.openai.chat.sync": ([R(_c)], [Call(_ct)], "openai.chat.sync"),
    "js.openai.chat.stream": ([R(events=_cs)], [Call(_cst)], "openai.chat.stream"),
    "js.openai.chat.stream.early_close": ([R(events=_cs)], [Call(None, completes=False)],
                                          "openai.chat.stream.early_close"),
    "js.openai.chat.tool_call": ([R(_tc)], [Call(_tct)], "openai.chat.tool_call"),
    "js.openai.responses.sync": ([R(_r)], [Call(_rt, op="gen_ai.responses")], "openai.responses.sync"),
    "js.openai.responses.stream": ([R(events=_rs, names=_rsn)], [Call(_rst, op="gen_ai.responses")],
                                   "openai.responses.stream"),
    "js.openai.responses.stream.early_close": ([R(events=_rs, names=_rsn)],
                                               [Call(None, op="gen_ai.responses", completes=False)],
                                               "openai.responses.stream.early_close"),
    "js.anthropic.messages.sync": ([R(_m)], [Call(_mt)], "anthropic.messages.sync"),
    "js.anthropic.messages.stream": ([R(events=_ms, names=_msn)], [Call(_mst)], "anthropic.messages.stream"),
    "js.anthropic.messages.stream_helper": ([R(events=_ms, names=_msn)], [Call(_mst)],
                                            "anthropic.messages.stream_helper"),
    "js.anthropic.messages.stream.early_close": ([R(events=_ms, names=_msn)], [Call(None, completes=False)],
                                                 "anthropic.messages.stream.early_close"),
    "js.anthropic.messages.stream.as_response": ([R(events=_ms, names=_msn)], [Call(None, completes=False)],
                                                 None),
    "js.anthropic.messages.with_response": ([R(_m)], [Call(_mt)], "anthropic.messages.raw_response"),
    "js.vercel_ai.generate_text": ([R(_v)], [Call(_vt)], None),
    "js.vercel_ai.stream_text.early_close": ([R(events=fx.openai_chat_stream(rid="chatcmpl-vs", cached=0)[0])],
                                             [Call(None, completes=False)], None),
    # LangChain (provider spans are skipped inside LangChain; the callback handler reports the call)
    "js.langchain.chat_openai.invoke": ([R(_c)], [Call(_ct)], "langchain.chat_openai.invoke", LC),
    "js.langchain.chat_openai.stream": ([R(events=_cs)], [Call(_cst)], "langchain.chat_openai.stream", LC),
    "js.langchain.chat_openai.tool_call": ([R(_tc)], [Call(_tct)], None, LC),
    "js.langchain.chat_anthropic.invoke": ([R(_m)], [Call(_mt)], None, LC),
    "js.langchain.chat_anthropic.stream": ([R(events=_ms, names=_msn)], [Call(_mst)], None, LC),
    "js.langchain.chat_google.invoke": ([R(_g)], [Call(_gt)], None, LC),
    # getsentry/sentry-javascript#19687: a direct provider call after a LangChain call
    "js.langchain.then_direct_openai": ([R(_c), R(_d)], [Call(_ct), Call(_dt)], None, LC),
    "js.langchain.direct_openai_first": ([R(_d), R(_c), R(_d2)], [Call(_dt), Call(_ct), Call(_d2t)], None, LC),
    "js.langchain.then_direct_openai.control": ([R(_c), R(_d)], [Call(_ct), Call(_dt)], None,
                                                {"integrations": ["openAI"],
                                                 "runs": "js.langchain.then_direct_openai"}),
    "js.langchain.then_direct_anthropic": ([R(_c), R(_m)], [Call(_ct), Call(_mt)], None, LC),
    "js.langchain.then_direct_google": ([R(_c), R(_g)], [Call(_ct), Call(_gt)], None, LC),
    # LangGraph
    "js.langgraph.react_agent": ([R(_a1), R(_a2)], [Call(_a1t), Call(_a2t)], "langgraph.react_agent", LG),
    "js.langgraph.react_agent.stream": ([R(_a1), R(_a2)], [Call(_a1t), Call(_a2t)], "langgraph.react_agent.stream",
                                        LG),
    "js.langgraph.react_agent.anthropic": ([R(_aa1), R(_aa2)], [Call(_aa1t), Call(_aa2t)], None, LG),
    "js.langgraph.react_agent.thread": ([R(_a1), R(_a2), R(_a3)], [Call(_a1t), Call(_a2t), Call(_a3t)], None, LG),
    # LangGraph enabled without the LangChain integration (explicit integration lists)
    "js.langgraph.react_agent.without_langchain": ([R(_a1), R(_a2)], [Call(_a1t), Call(_a2t)], None,
                                                   {"integrations": ["langGraph", "openAI"], "agent": AGENT,
                                                    "runs": "js.langgraph.react_agent"}),
    "js.langgraph.create_agent": ([R(_a1), R(_a2)], [Call(_a1t), Call(_a2t)], None, LG),
    # Google GenAI
    "js.google_genai.generate_content": ([R(_g)], [Call(_gt)], "google_genai.generate_content", LC),
    "js.google_genai.generate_content_stream": ([R(events=_gs, done=False)], [Call(_gst)],
                                                "google_genai.generate_content_stream", LC),
    "js.google_genai.generate_content_stream.early_close": ([R(events=_gs, done=False)], [Call(None, completes=False)],
                                                            "google_genai.generate_content_stream.early_close", LC),
    "js.google_genai.chat.send_message": ([R(_g), R(_g2)], [Call(_gt), Call(_g2t)], None, LC),
    "js.google_genai.chat.send_message_stream": ([R(events=_gs, done=False)], [Call(_gst)], None, LC),
}


def run_one(sid: str, mode: str, js_dir: Path = JS) -> dict:
    replies, calls, twin, opts = (*JS_SCENARIOS[sid], {})[:4]
    job = {"scenario": opts.get("runs", sid), "replies": replies}
    if opts.get("integrations"):
        job["integrations"] = opts["integrations"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(job, fh)
        path = fh.name
    t0 = time.time()
    args = ["node", "worker.cjs", path] + (["--no-data-collection"] if mode == "nodc" else [])
    p = subprocess.run(args, cwd=js_dir, capture_output=True, text=True, timeout=120)
    mark = "@@SPANPROOF-RESULT@@"
    if mark not in p.stdout:
        return {"scenario": sid, "mode": mode, "crashed": True, "stderr": p.stderr[-2000:]}
    r = json.loads(p.stdout.rsplit(mark, 1)[1])
    integ = sid.split(".")[1]
    r.update(scenario=sid, mode=mode, integration=f"js.{integ}", package=integ, twin=twin,
             calls=[{"truth": c.truth.as_dict() if c.truth else None, "op": c.op, "completes": c.completes}
                    for c in calls],
             expects_exception=False, agent=opts.get("agent"), seconds=round(time.time() - t0, 2),
             language="javascript")
    r["findings"] = oracles.run_all(r)
    for f in r["findings"]:
        f["mode"] = mode
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="js.")
    ap.add_argument("--modes", default="default,nodc")
    ap.add_argument("--out", default="results/js.json")
    ap.add_argument("--js-dir", default=str(JS), help="directory with worker.cjs and its node_modules")
    a = ap.parse_args(argv)
    jobs = [(s, m) for s in JS_SCENARIOS if s.startswith(tuple(a.only.split(","))) for m in a.modes.split(",")]
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda j: run_one(*j, js_dir=Path(a.js_dir)), jobs))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": results}, open(a.out, "w"), indent=1,
              default=str)
    print(f"{len(results)} runs, {sum(1 for r in results if r.get('crashed'))} crashed -> {a.out}")
    for r in results:
        if r.get("crashed"):
            print("CRASH", r["scenario"], r["stderr"][-500:])
        for f in r.get("findings", []):
            print(f"  [{f['severity']:6}] {f['check']:11} {r['scenario']:42} {r['mode']:7} {f['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
