"""One real agent run on a live model with an injected tool/provider fault (see live_agents.py)."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import traceback

from . import capture, mockserver
from .mockserver import MockServer, Reply

MARK = "\n@@SPANPROOF-RESULT@@\n"
KEY = "spanproof-dummy-key"


def make_tools(fault: str, log: list):
    def call(name, args, good):
        log.append({"tool": name, "args": json.dumps(args, sort_keys=True)})
        if fault == "tool_error_payload":
            return json.dumps({"ok": False, "error": "upstream 503: service unavailable"})
        if fault == "tool_timeout_raise":
            raise TimeoutError(f"{name} backend did not respond after 30s")
        if fault == "same_result_forever":
            return "No results yet. Please call this tool again with exactly the same arguments."
        return good

    def get_weather(city: str) -> str:
        """Current weather for a city."""
        return call("get_weather", {"city": city}, f"Sunny, 18C in {city}, light wind.")

    def lookup_order(order_id: str) -> str:
        """Look up an order's status by its id."""
        return call("lookup_order", {"order_id": order_id}, f"Order {order_id}: shipped on Oct 3, arriving Oct 7.")

    def search_docs(query: str) -> str:
        """Search the internal help-center documents."""
        return call("search_docs", {"query": query}, "Refunds are accepted within 30 days with a receipt.")

    return [get_weather, lookup_order, search_docs]


def run(framework, url, tools, task):
    if framework == "openai_agents":
        from agents import Agent, Runner, function_tool, set_default_openai_api, set_default_openai_client
        from agents import set_tracing_disabled
        from openai import AsyncOpenAI

        set_tracing_disabled(True)
        set_default_openai_client(AsyncOpenAI(api_key=KEY, base_url=url + "/v1", max_retries=4), use_for_tracing=False)
        set_default_openai_api("chat_completions")
        a = Agent(name="support_agent", instructions="Use the tools to answer. Be brief.", model="gpt-oss-20b",
                  tools=[function_tool(t) for t in tools])
        return Runner.run_sync(a, task, max_turns=6).final_output
    if framework == "langgraph":
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent

        llm = ChatOpenAI(model="gpt-oss-20b", api_key=KEY, base_url=url + "/v1", max_retries=4)
        g = create_react_agent(llm, tools, name="support_agent")
        res = g.invoke({"messages": [("user", task)]}, {"recursion_limit": 12})
        return res["messages"][-1].content
    from openai import AsyncOpenAI
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from pydantic_ai.usage import UsageLimits

    model = OpenAIChatModel("gpt-oss-20b", provider=OpenAIProvider(
        openai_client=AsyncOpenAI(api_key=KEY, base_url=url + "/v1", max_retries=4)))
    a = Agent(model, name="support_agent", instructions="Use the tools to answer. Be brief.")
    for t in tools:
        a.tool_plain(t)
    return a.run_sync(task, usage_limits=UsageLimits(request_limit=6)).output


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    a = ap.parse_args()
    job = json.loads(a.job)
    fw, fault = job["framework"], job["fault"]
    mockserver.LIVE = mockserver.live_from_env()
    from .agent_worker import integrations

    transport = capture.init(integrations(fw), data_collection=True)
    import sentry_sdk

    sentry_sdk.set_tag("spanproof.fault", fault)
    log: list = []
    replies = [Reply(body={"error": {"message": "upstream overloaded", "type": "server_error"}}, status=500)
               for _ in range(3)] if fault == "provider_500x3" else []
    exc, answer = None, None
    real_stdout = sys.stdout
    with MockServer(replies) as srv, contextlib.redirect_stdout(sys.stderr):
        with sentry_sdk.start_transaction(op="spanproof.live_agent", name=f"{fw}:{fault}"):
            try:
                answer = run(fw, srv.url, make_tools(fault, log), job["task"])
            except BaseException as e:  # noqa: BLE001
                exc = {"type": type(e).__name__, "value": str(e)[:300], "tb": traceback.format_exc()[-800:]}
    recordings = list(srv.script.recordings)
    served_500 = sum(1 for r in srv.script.requests[:3]) if fault == "provider_500x3" else 0
    sentry_sdk.flush(timeout=30 if os.environ.get("SPANPROOF_DSN") else 5)

    # ground truth from our own logs (tool calls, injected errors), never from Sentry's spans
    from collections import Counter

    repeats = max(Counter(json.dumps(x, sort_keys=True) for x in log).values(), default=0)
    if exc:
        label = "loud_failure"  # the run raised: visible failure, not one of the silent classes
    elif fault == "none":
        label = "healthy"
    elif fault in ("tool_error_payload", "tool_timeout_raise"):
        label = "tool_failed_then_answered" if log else "not_triggered"  # judged later: silent or acknowledged
    elif fault == "same_result_forever":
        label = "tool_loop" if repeats >= 3 else ("healthy_gave_up" if log else "not_triggered")
    else:
        label = "retry_storm" if served_500 >= 3 and recordings else "not_triggered"
    out = capture.flatten(transport)
    out.update(framework=fw, fault=fault, label=label, answer=answer, exception=exc, tool_log=log,
               tool_repeats=repeats, provider_calls=len(recordings), task=job["task"])
    real_stdout.write(MARK + json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
