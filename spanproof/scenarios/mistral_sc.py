"""Mistral Python SDK scenarios (sentry_sdk.integrations.mistral)."""

from __future__ import annotations

import asyncio

from mistralai.client import (
    Mistral,
)

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import DUMMY_KEY, Call, Scenario, register


def _integ():
    from sentry_sdk.integrations.mistral import MistralIntegration

    return [MistralIntegration()]


def _client(url):
    from mistralai.client.utils import RetryConfig

    return Mistral(api_key=DUMMY_KEY, server_url=url, retry_config=RetryConfig("none", None, False))


MSG = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Capital of France?"}]
KW = dict(model="mistral-large-latest", messages=MSG, max_tokens=256)
_c, _c_t = fx.mistral_chat()


def _run(url):
    _client(url).chat.complete(**KW)


register(Scenario("mistral.chat.complete", "mistral", "mistralai", lambda: [Reply(body=_c)], [Call(_c_t)], _run,
                  _integ, tags=["usage"]))


def _run_async(url):
    async def go():
        await _client(url).chat.complete_async(**KW)

    asyncio.run(go())


register(Scenario("mistral.chat.complete.async", "mistral", "mistralai", lambda: [Reply(body=_c)], [Call(_c_t)],
                  _run_async, _integ, tags=["usage", "async"]))

_tc, _tc_t = fx.mistral_chat(rid="mst-sp-tool", completion=22, tool_calls=[
    {"id": "Xq9s1tool", "type": "function", "index": 0,
     "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}])
TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
                                           "parameters": {"type": "object",
                                                          "properties": {"city": {"type": "string"}},
                                                          "required": ["city"]}}}]


def _run_tool(url):
    _client(url).chat.complete(tools=TOOLS, **KW)


register(Scenario("mistral.chat.complete.tool_call", "mistral", "mistralai", lambda: [Reply(body=_tc)], [Call(_tc_t)],
                  _run_tool, _integ, tags=["usage", "tools"]))


def _run_429(url):
    _client(url).chat.complete(**KW)


register(Scenario("mistral.chat.complete.http_429", "mistral", "mistralai",
                  lambda: [Reply(body={"object": "error", "message": "Requests rate limit exceeded",
                                       "type": "rate_limited", "param": None, "code": "1300"}, status=429)],
                  [Call(None, completes=False)], _run_429, _integ, expects_exception=True,
                  tags=["lifecycle", "error"]))

_se, _se_t = fx.mistral_chat_stream()


def _run_stream(url):
    for _ in _client(url).chat.stream(**KW):
        pass


register(Scenario("mistral.chat.stream", "mistral", "mistralai", lambda: [Reply(events=_se)], [Call(_se_t)],
                  _run_stream, _integ, tags=["usage", "stream"]))


def _run_stream_async(url):
    async def go():
        async for _ in await _client(url).chat.stream_async(**KW):
            pass

    asyncio.run(go())


register(Scenario("mistral.chat.stream.async", "mistral", "mistralai", lambda: [Reply(events=_se)], [Call(_se_t)],
                  _run_stream_async, _integ, tags=["usage", "stream", "async"]))

_e, _e_t = fx.mistral_embed()


def _run_embed(url):
    _client(url).embeddings.create(model="mistral-embed", inputs=["Paris"])


register(Scenario("mistral.embeddings.create", "mistral", "mistralai", lambda: [Reply(body=_e)],
                  [Call(_e_t, op="gen_ai.embeddings")], _run_embed, _integ, tags=["usage", "embeddings"]))
