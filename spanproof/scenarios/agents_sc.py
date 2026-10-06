"""Agent-framework scenarios: one tool call, then a final answer (two provider calls).

These exercise trace structure (agent -> LLM -> tool nesting) and trace-level token
aggregation, which single-call scenarios cannot.
"""

from __future__ import annotations

import asyncio

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import DUMMY_KEY, Call, Scenario, register

AGENT = {"tools": ["get_weather"], "agent_name": "weather_agent"}
Q = "What is the weather in Paris?"

# Two chat-completions turns: tool call, then answer.
_c1, _c1_t = fx.openai_chat(rid="chatcmpl-ag1", tool_calls=fx.openai_tool_call(), prompt=800, completion=30,
                            cached=512, reasoning=0)
_c2, _c2_t = fx.openai_chat(rid="chatcmpl-ag2", content="It is sunny in Paris.", prompt=900, completion=20,
                            cached=768, reasoning=0)
CHAT_REPLIES = lambda: [Reply(body=_c1), Reply(body=_c2)]  # noqa: E731
CHAT_CALLS = [Call(_c1_t), Call(_c2_t)]

# Two responses-API turns.
_r1, _r1_t = fx.openai_response(rid="resp_ag1", inp=800, out=30, cached=512, cache_write=0, reasoning=0,
                                function_call={"call_id": "call_ag1", "name": "get_weather",
                                               "arguments": '{"city":"Paris"}'})
_r2, _r2_t = fx.openai_response(rid="resp_ag2", text="It is sunny in Paris.", inp=900, out=20, cached=768,
                                cache_write=0, reasoning=0)
RESP_REPLIES = lambda: [Reply(body=_r1), Reply(body=_r2)]  # noqa: E731
RESP_CALLS = [Call(_r1_t, op="gen_ai.responses"), Call(_r2_t, op="gen_ai.responses")]


def get_weather(city: str) -> str:
    """Weather for a city."""
    return f"Sunny in {city}"


# ------------------------------------------------------------ openai-agents
try:
    import agents  # noqa: F401

    def _oa_integ():
        from sentry_sdk.integrations.openai_agents import OpenAIAgentsIntegration

        return [OpenAIAgentsIntegration()]

    def _oa_run(api):
        def run(url):
            from agents import Agent, Runner, function_tool, set_default_openai_api, set_default_openai_client
            from agents import set_tracing_disabled
            from openai import AsyncOpenAI

            set_tracing_disabled(True)
            set_default_openai_client(AsyncOpenAI(api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=0),
                                      use_for_tracing=False)
            set_default_openai_api(api)
            a = Agent(name="weather_agent", instructions="Use tools.", model="gpt-5-mini",
                      tools=[function_tool(get_weather)])
            Runner.run_sync(a, Q)

        return run

    register(Scenario("openai_agents.run.responses", "openai_agents", "openai-agents", RESP_REPLIES, RESP_CALLS,
                      _oa_run("responses"), _oa_integ, agent=AGENT,
                      tags=["structure", "aggregation", "agent"]))
    register(Scenario("openai_agents.run.chat", "openai_agents", "openai-agents", CHAT_REPLIES, CHAT_CALLS,
                      _oa_run("chat_completions"), _oa_integ, agent=AGENT,
                      tags=["structure", "aggregation", "agent"]))

    def _oa_as_tool(url):
        """An agent used as a tool by another agent (getsentry/sentry-python#4786)."""
        from agents import Agent, Runner, function_tool, set_default_openai_api, set_default_openai_client
        from agents import set_tracing_disabled
        from openai import AsyncOpenAI

        set_tracing_disabled(True)
        set_default_openai_client(AsyncOpenAI(api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=0),
                                  use_for_tracing=False)
        set_default_openai_api("chat_completions")
        inner = Agent(name="weather_agent", instructions="Use tools.", model="gpt-5-mini",
                      tools=[function_tool(get_weather)])
        outer = Agent(name="router", instructions="Delegate.", model="gpt-5-mini",
                      tools=[inner.as_tool(tool_name="ask_weather", tool_description="Ask the weather agent")])
        Runner.run_sync(outer, Q)

    _o1, _o1_t = fx.openai_chat(rid="chatcmpl-out1", prompt=700, completion=25, cached=0, reasoning=0,
                                tool_calls=fx.openai_tool_call("ask_weather", '{"input":"Paris weather"}',
                                                               "call_out1"))
    _o2, _o2_t = fx.openai_chat(rid="chatcmpl-out2", content="Sunny in Paris.", prompt=950, completion=15,
                                cached=0, reasoning=0)
    register(Scenario("openai_agents.agent_as_tool", "openai_agents", "openai-agents",
                      lambda: [Reply(body=_o1), Reply(body=_c1), Reply(body=_c2), Reply(body=_o2)],
                      [Call(_o1_t), Call(_c1_t), Call(_c2_t), Call(_o2_t)], _oa_as_tool, _oa_integ,
                      agent={"tools": ["ask_weather", "get_weather"], "agent_name": "router"},
                      tags=["structure", "agent"], notes="getsentry/sentry-python#4786"))
except ImportError:
    pass

# ------------------------------------------------------------- pydantic-ai
try:
    import pydantic_ai  # noqa: F401

    def _pa_integ():
        from sentry_sdk.integrations.pydantic_ai import PydanticAIIntegration

        return [PydanticAIIntegration()]

    def _pa_agent(url):
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        from openai import AsyncOpenAI

        model = OpenAIChatModel("gpt-5-mini", provider=OpenAIProvider(
            openai_client=AsyncOpenAI(api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=0)))
        a = Agent(model, name="weather_agent", instructions="Use tools.")
        a.tool_plain(get_weather)
        return a

    def _pa_run(url):
        _pa_agent(url).run_sync(Q)

    register(Scenario("pydantic_ai.run_sync", "pydantic_ai", "pydantic-ai-slim", CHAT_REPLIES, CHAT_CALLS, _pa_run,
                      _pa_integ, agent=AGENT, tags=["structure", "aggregation", "agent"]))

    def _pa_iter(url):
        """Agent.iter(): getsentry/sentry-python#5606."""
        a = _pa_agent(url)

        async def go():
            async with a.iter(Q) as run:
                async for _ in run:
                    pass

        asyncio.run(go())

    register(Scenario("pydantic_ai.iter", "pydantic_ai", "pydantic-ai-slim", CHAT_REPLIES, CHAT_CALLS, _pa_iter,
                      _pa_integ, agent=AGENT, tags=["structure", "agent"], notes="getsentry/sentry-python#5606"))
except ImportError:
    pass

# --------------------------------------------------------- langchain / langgraph
try:
    import langchain_openai  # noqa: F401

    def _lc_integ():
        from sentry_sdk.integrations.langchain import LangchainIntegration

        return [LangchainIntegration()]

    def _lg_integ():
        from sentry_sdk.integrations.langchain import LangchainIntegration
        from sentry_sdk.integrations.langgraph import LanggraphIntegration

        return [LangchainIntegration(), LanggraphIntegration()]

    def _llm(url):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model="gpt-5-mini", api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=0)

    _lc, _lc_t = fx.openai_chat(rid="chatcmpl-lc1")

    def _lc_invoke(url):
        _llm(url).invoke([("system", "Be brief."), ("user", "Capital of France?")])

    register(Scenario("langchain.chat_openai.invoke", "langchain", "langchain", lambda: [Reply(body=_lc)],
                      [Call(_lc_t)], _lc_invoke, _lc_integ, tags=["usage", "cached", "reasoning"]))

    _lcs, _lcs_t = fx.openai_chat_stream(rid="chatcmpl-lc2")

    def _lc_stream(url):
        from langchain_openai import ChatOpenAI

        m = ChatOpenAI(model="gpt-5-mini", api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=0,
                       stream_usage=True)
        for _ in m.stream([("user", "Capital of France?")]):
            pass

    register(Scenario("langchain.chat_openai.stream", "langchain", "langchain", lambda: [Reply(events=_lcs)],
                      [Call(_lcs_t)], _lc_stream, _lc_integ, tags=["usage", "stream"]))

    def _lg_react(url):
        from langgraph.prebuilt import create_react_agent

        g = create_react_agent(_llm(url), [get_weather], name="weather_agent")
        g.invoke({"messages": [("user", Q)]})

    register(Scenario("langgraph.react_agent", "langgraph", "langgraph", CHAT_REPLIES, CHAT_CALLS, _lg_react,
                      _lg_integ, agent=AGENT, tags=["structure", "aggregation", "agent"]))

    def _lg_stream(url):
        """graph.stream(): the path behind getsentry/sentry-javascript#19626 in JS."""
        from langgraph.prebuilt import create_react_agent

        g = create_react_agent(_llm(url), [get_weather], name="weather_agent")
        for _ in g.stream({"messages": [("user", Q)]}):
            pass

    register(Scenario("langgraph.react_agent.stream", "langgraph", "langgraph", CHAT_REPLIES, CHAT_CALLS, _lg_stream,
                      _lg_integ, agent=AGENT, tags=["structure", "agent"]))

    def _lg_astream(url):
        from langgraph.prebuilt import create_react_agent

        g = create_react_agent(_llm(url), [get_weather], name="weather_agent")

        async def go():
            async for _ in g.astream({"messages": [("user", Q)]}):
                pass

        asyncio.run(go())

    register(Scenario("langgraph.react_agent.astream", "langgraph", "langgraph", CHAT_REPLIES, CHAT_CALLS,
                      _lg_astream, _lg_integ, agent=AGENT, tags=["structure", "agent", "async"]))

    def _lg_stream_close(url):
        """Stop reading after the first graph step (the model's tool call)."""
        from langgraph.prebuilt import create_react_agent

        g = create_react_agent(_llm(url), [get_weather], name="weather_agent")
        s = g.stream({"messages": [("user", Q)]})
        next(s)
        s.close()

    register(Scenario("langgraph.react_agent.stream.early_close", "langgraph", "langgraph", CHAT_REPLIES,
                      [Call(_c1_t)], _lg_stream_close, _lg_integ,
                      agent={"tools": [], "agent_name": "weather_agent"}, tags=["structure", "lifecycle", "agent"]))
except ImportError:
    pass
