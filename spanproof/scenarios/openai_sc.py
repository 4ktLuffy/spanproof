"""OpenAI Python SDK scenarios (sentry_sdk.integrations.openai)."""

from __future__ import annotations

import asyncio

import openai  # noqa: F401  (ImportError => scenarios skipped)

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import DUMMY_KEY, Call, Scenario, register


def _integ():
    from sentry_sdk.integrations.openai import OpenAIIntegration

    return [OpenAIIntegration()]


def _client(url):
    from openai import OpenAI

    return OpenAI(api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=0)


def _aclient(url):
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=DUMMY_KEY, base_url=url + "/v1", max_retries=0)


MSG = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Capital of France?"}]

# -------------------------------------------------------------- chat, blocking
_chat, _chat_t = fx.openai_chat()


def _run_chat(url):
    _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG)


register(Scenario("openai.chat.sync", "openai", "openai", lambda: [Reply(body=_chat)],
                  [Call(_chat_t)], _run_chat, _integ, tags=["usage", "cached", "reasoning"]))


def _run_chat_async(url):
    async def go():
        await _aclient(url).chat.completions.create(model="gpt-5-mini", messages=MSG)

    asyncio.run(go())


register(Scenario("openai.chat.async", "openai", "openai", lambda: [Reply(body=_chat)],
                  [Call(_chat_t)], _run_chat_async, _integ, tags=["usage", "async"]))

# ------------------------------------------------------------- chat, streaming
_cs, _cs_t = fx.openai_chat_stream()


def _run_chat_stream(url):
    s = _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG, stream=True,
                                             stream_options={"include_usage": True})
    for _ in s:
        pass


register(Scenario("openai.chat.stream", "openai", "openai", lambda: [Reply(events=_cs)],
                  [Call(_cs_t)], _run_chat_stream, _integ, tags=["usage", "stream"]))


def _run_chat_stream_async(url):
    async def go():
        s = await _aclient(url).chat.completions.create(model="gpt-5-mini", messages=MSG, stream=True,
                                                        stream_options={"include_usage": True})
        async for _ in s:
            pass

    asyncio.run(go())


register(Scenario("openai.chat.stream.async", "openai", "openai", lambda: [Reply(events=_cs)],
                  [Call(_cs_t)], _run_chat_stream_async, _integ, tags=["usage", "stream", "async"]))


def _run_chat_stream_close(url):
    s = _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG, stream=True,
                                             stream_options={"include_usage": True})
    for i, _ in enumerate(s):
        if i == 2:
            break
    s.close()


register(Scenario("openai.chat.stream.early_close", "openai", "openai", lambda: [Reply(events=_cs)],
                  [Call(None, completes=False)], _run_chat_stream_close, _integ,
                  tags=["lifecycle", "stream"], notes="getsentry/sentry-python#7847"))


def _run_chat_stream_cut(url):
    s = _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG, stream=True,
                                             stream_options={"include_usage": True})
    for _ in s:
        pass


register(Scenario("openai.chat.stream.connection_drop", "openai", "openai",
                  lambda: [Reply(events=_cs, cut_after=3)], [Call(None, completes=False)],
                  _run_chat_stream_cut, _integ, tags=["lifecycle", "stream", "error"],
                  notes="openai-python ends iteration silently when the socket closes without [DONE]"))


def _run_chat_500(url):
    _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG)


register(Scenario("openai.chat.http_500", "openai", "openai",
                  lambda: [Reply(body={"error": {"message": "upstream overloaded", "type": "server_error"}},
                                 status=500)],
                  [Call(None, completes=False)], _run_chat_500, _integ, expects_exception=True,
                  tags=["lifecycle", "error"]))

# ------------------------------------------------------------ chat, tool call
_tc, _tc_t = fx.openai_chat(rid="chatcmpl-sp-tool", tool_calls=fx.openai_tool_call(), completion=40, reasoning=0)

TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
                                           "parameters": {"type": "object",
                                                          "properties": {"city": {"type": "string"}},
                                                          "required": ["city"]}}}]


def _run_chat_tool(url):
    _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG, tools=TOOLS)


register(Scenario("openai.chat.tool_call", "openai", "openai", lambda: [Reply(body=_tc)],
                  [Call(_tc_t)], _run_chat_tool, _integ, tags=["usage", "tools"]))

# ------------------------------------------------------------------ responses
_rs, _rs_t = fx.openai_response()


def _run_resp(url):
    _client(url).responses.create(model="gpt-5-mini", input="Capital of France?", instructions="Be brief.")


register(Scenario("openai.responses.sync", "openai", "openai", lambda: [Reply(body=_rs)],
                  [Call(_rs_t, op="gen_ai.responses")], _run_resp, _integ,
                  tags=["usage", "cached", "cache_write", "reasoning"]))

_rse, _rse_n, _rse_t = fx.openai_response_stream(rid="resp_sp2")


def _run_resp_stream(url):
    s = _client(url).responses.create(model="gpt-5-mini", input="Capital of France?", stream=True)
    for _ in s:
        pass


register(Scenario("openai.responses.stream", "openai", "openai",
                  lambda: [Reply(events=_rse, sse_event_names=_rse_n)],
                  [Call(_rse_t, op="gen_ai.responses")], _run_resp_stream, _integ, tags=["usage", "stream"]))


def _run_resp_stream_close(url):
    s = _client(url).responses.create(model="gpt-5-mini", input="Capital of France?", stream=True)
    for i, _ in enumerate(s):
        if i == 2:
            break
    s.close()


register(Scenario("openai.responses.stream.early_close", "openai", "openai",
                  lambda: [Reply(events=_rse, sse_event_names=_rse_n)],
                  [Call(None, op="gen_ai.responses", completes=False)], _run_resp_stream_close, _integ,
                  tags=["lifecycle", "stream"], notes="getsentry/sentry-python#7847"))
