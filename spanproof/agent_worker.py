"""Run one scripted agent trace (see corpus.py) and print its spans.

    python -m spanproof.agent_worker '<job json>' [--no-data-collection]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import traceback

from . import capture, fixtures as fx
from .mockserver import MockServer, Reply
from .scenario import DUMMY_KEY

MARK = "\n@@SPANPROOF-RESULT@@\n"


def build_replies(plan: dict) -> list[Reply]:
    replies: list[Reply] = []
    base = plan["base_tokens"]
    for i, st in enumerate(plan["steps"]):
        for _ in range(st.get("http_errors", 0)):
            replies.append(Reply(body={"error": {"message": "upstream overloaded", "type": "server_error"}},
                                 status=500))
        prompt = base + 150 * i
        if "tool" in st:
            body, _ = fx.openai_chat(rid=f"chatcmpl-c{i}", prompt=prompt, completion=25, cached=0, reasoning=0,
                                     tool_calls=fx.openai_tool_call(st["tool"], json.dumps(st["args"]),
                                                                    f"call_{i}"))
            replies.append(Reply(body=body))
            if plan["tool_mode"].get(st["tool"]) == "early_close_stream":
                ev, _ = fx.openai_chat_stream(rid=f"chatcmpl-inner{i}", prompt=300, completion=80, cached=0,
                                              reasoning=0)
                replies.append(Reply(events=ev))
        else:
            body, _ = fx.openai_chat(rid=f"chatcmpl-c{i}", prompt=prompt, completion=max(1, len(st["final"]) // 4),
                                     cached=0, reasoning=0, content=st["final"])
            body["choices"][0]["finish_reason"] = st.get("finish", "stop")
            replies.append(Reply(body=body))
    # dead-end runs keep asking for tools until the framework's turn limit stops them
    if plan["label"] == "dead_end":
        last = plan["steps"][-1]
        for j in range(20):
            body, _ = fx.openai_chat(rid=f"chatcmpl-x{j}", prompt=base + 1200, completion=25, cached=0, reasoning=0,
                                     tool_calls=fx.openai_tool_call(last["tool"], json.dumps(dict(last["args"],
                                                                                                page=100 + j)),
                                                                    f"call_x{j}"))
            replies.append(Reply(body=body))
    return replies


def make_tools(plan: dict, url: str, framework: str):
    mode = dict(plan["tool_mode"])
    if framework in ("pydantic_ai", "langgraph") and mode.get("get_weather") == "raise":
        # these frameworks let a raising tool abort the run, which is a loud failure,
        # not a silent one; model the silent case as an error payload instead
        mode["get_weather"] = "error_payload"

    def get_weather(city: str) -> str:
        """Current weather for a city."""
        m = mode.get("get_weather")
        if m == "raise":
            raise RuntimeError("weather backend timeout after 30s")
        if m == "error_payload":
            return json.dumps({"error": "upstream 503: weather backend unavailable"})
        return f"Sunny, 22C in {city}"

    def search_docs(query: str, page: int = 1) -> str:
        """Search internal documents."""
        return f"page {page}: 3 results for {query}"

    def check_logs(service: str) -> str:
        """Check a service's logs."""
        return "No errors found in the last 24h for " + service

    def summarize_stream(text: str) -> str:
        """Summarize text with a streaming model call."""
        from openai import OpenAI

        c = OpenAI(api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=0)
        s = c.chat.completions.create(model="gpt-5-mini", messages=[{"role": "user", "content": text}],
                                      stream=True, stream_options={"include_usage": True})
        out = []
        for i, ch in enumerate(s):
            if ch.choices and ch.choices[0].delta.content:
                out.append(ch.choices[0].delta.content)
            if i == 2:
                break
        s.close()
        return "".join(out) or "summary"

    return [get_weather, search_docs, check_logs, summarize_stream]


def run_openai_agents(url, tools):
    from agents import Agent, Runner, function_tool, set_default_openai_api, set_default_openai_client
    from agents import set_tracing_disabled
    from openai import AsyncOpenAI

    set_tracing_disabled(True)
    set_default_openai_client(AsyncOpenAI(api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=5),
                              use_for_tracing=False)
    set_default_openai_api("chat_completions")
    a = Agent(name="support_agent", instructions="Use tools.", model="gpt-5-mini",
              tools=[function_tool(t) for t in tools])
    Runner.run_sync(a, "Help me.", max_turns=6)


def run_langgraph(url, tools):
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent

    llm = ChatOpenAI(model="gpt-5-mini", api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=5)
    g = create_react_agent(llm, tools, name="support_agent")
    g.invoke({"messages": [("user", "Help me.")]}, {"recursion_limit": 12})


def run_pydantic_ai(url, tools):
    from openai import AsyncOpenAI
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from pydantic_ai.usage import UsageLimits

    model = OpenAIChatModel("gpt-5-mini", provider=OpenAIProvider(
        openai_client=AsyncOpenAI(api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=5)))
    a = Agent(model, name="support_agent", instructions="Use tools.")
    for t in tools:
        a.tool_plain(t)
    a.run_sync("Help me.", usage_limits=UsageLimits(request_limit=6))


def integrations(framework):
    # Mirror Sentry's defaults: with an agent framework installed, the plain OpenAI
    # integration is auto-deactivated, so it is not listed here (listing both would
    # double every chat span, which is a configuration artefact, not an SDK bug).
    from sentry_sdk.integrations.httpx import HttpxIntegration

    if framework == "openai_agents":
        from sentry_sdk.integrations.openai_agents import OpenAIAgentsIntegration

        return [OpenAIAgentsIntegration(), HttpxIntegration()]
    if framework == "langgraph":
        from sentry_sdk.integrations.langchain import LangchainIntegration
        from sentry_sdk.integrations.langgraph import LanggraphIntegration

        return [LangchainIntegration(), LanggraphIntegration(), HttpxIntegration()]
    from sentry_sdk.integrations.pydantic_ai import PydanticAIIntegration

    return [PydanticAIIntegration(), HttpxIntegration()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--no-data-collection", action="store_true")
    a = ap.parse_args()
    job = json.loads(a.job)
    plan, fw = job["plan"], job["framework"]
    for k in ("OPENAI_API_KEY",):
        os.environ[k] = DUMMY_KEY
    transport = capture.init(integrations(fw), data_collection=not a.no_data_collection)
    import sentry_sdk

    exc = None
    real_stdout = sys.stdout
    with MockServer(build_replies(plan)) as srv, contextlib.redirect_stdout(sys.stderr):
        tools = make_tools(plan, srv.url, fw)
        with sentry_sdk.start_transaction(op="spanproof.agent_run", name=f"{fw}:{plan['label']}"):
            try:
                {"openai_agents": run_openai_agents, "langgraph": run_langgraph,
                 "pydantic_ai": run_pydantic_ai}[fw](srv.url, tools)
            except BaseException as e:  # noqa: BLE001
                exc = {"type": type(e).__name__, "value": str(e)[:300], "tb": traceback.format_exc()[-800:]}
        requests = list(srv.script.requests)
    sentry_sdk.flush(timeout=30 if os.environ.get("SPANPROOF_DSN") else 5)
    out = capture.flatten(transport)
    out.update(exception=exc, requests=[{"path": r["path"]} for r in requests], framework=fw,
               data_collection=not a.no_data_collection)
    real_stdout.write(MARK + json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
