"""Anthropic Python SDK scenarios (sentry_sdk.integrations.anthropic)."""

from __future__ import annotations

import asyncio

import anthropic  # noqa: F401

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import DUMMY_KEY, Call, Scenario, register


def _integ():
    from sentry_sdk.integrations.anthropic import AnthropicIntegration

    return [AnthropicIntegration()]


def _client(url):
    from anthropic import Anthropic

    return Anthropic(api_key=DUMMY_KEY, base_url=url, max_retries=0)


def _aclient(url):
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=DUMMY_KEY, base_url=url, max_retries=0)


KW = dict(model="claude-sonnet-5-5", max_tokens=256, system="Be brief.",
          messages=[{"role": "user", "content": "Capital of France?"}])

_m, _m_t = fx.anthropic_message()


def _run(url):
    _client(url).messages.create(**KW)


register(Scenario("anthropic.messages.sync", "anthropic", "anthropic", lambda: [Reply(body=_m)], [Call(_m_t)],
                  _run, _integ, tags=["usage", "cached", "cache_write"]))


def _run_async(url):
    async def go():
        await _aclient(url).messages.create(**KW)

    asyncio.run(go())


register(Scenario("anthropic.messages.async", "anthropic", "anthropic", lambda: [Reply(body=_m)], [Call(_m_t)],
                  _run_async, _integ, tags=["usage", "async"]))

_se, _sn, _s_t = fx.anthropic_stream()


def _run_stream(url):
    for _ in _client(url).messages.create(stream=True, **KW):
        pass


register(Scenario("anthropic.messages.stream", "anthropic", "anthropic",
                  lambda: [Reply(events=_se, sse_event_names=_sn)], [Call(_s_t)], _run_stream, _integ,
                  tags=["usage", "stream"]))


def _run_stream_helper(url):
    with _client(url).messages.stream(**KW) as s:
        for _ in s.text_stream:
            pass


register(Scenario("anthropic.messages.stream_helper", "anthropic", "anthropic",
                  lambda: [Reply(events=_se, sse_event_names=_sn)], [Call(_s_t)], _run_stream_helper, _integ,
                  tags=["usage", "stream"]))


def _run_stream_async(url):
    async def go():
        s = await _aclient(url).messages.create(stream=True, **KW)
        async for _ in s:
            pass

    asyncio.run(go())


register(Scenario("anthropic.messages.stream.async", "anthropic", "anthropic",
                  lambda: [Reply(events=_se, sse_event_names=_sn)], [Call(_s_t)], _run_stream_async, _integ,
                  tags=["usage", "stream", "async"]))


def _run_stream_close(url):
    s = _client(url).messages.create(stream=True, **KW)
    for i, _ in enumerate(s):
        if i == 2:
            break
    s.close()


register(Scenario("anthropic.messages.stream.early_close", "anthropic", "anthropic",
                  lambda: [Reply(events=_se, sse_event_names=_sn)], [Call(None, completes=False)],
                  _run_stream_close, _integ, tags=["lifecycle", "stream"]))


def _run_stream_helper_close(url):
    with _client(url).messages.stream(**KW) as s:
        for i, _ in enumerate(s.text_stream):
            if i == 1:
                break


register(Scenario("anthropic.messages.stream_helper.early_close", "anthropic", "anthropic",
                  lambda: [Reply(events=_se, sse_event_names=_sn)], [Call(None, completes=False)],
                  _run_stream_helper_close, _integ, tags=["lifecycle", "stream"]))


def _run_with_raw(url):
    # .with_raw_response then .parse(): the path behind getsentry/sentry-javascript#24258 in JS
    raw = _client(url).messages.with_raw_response.create(**KW)
    raw.parse()


register(Scenario("anthropic.messages.raw_response", "anthropic", "anthropic", lambda: [Reply(body=_m)],
                  [Call(_m_t)], _run_with_raw, _integ, tags=["usage", "lifecycle"]))

_tu, _tu_t = fx.anthropic_message(rid="msg_sp_tool", out=60, tool_use={
    "type": "tool_use", "id": "toolu_sp1", "name": "get_weather", "input": {"city": "Paris"}})


def _run_tool(url):
    _client(url).messages.create(tools=[{"name": "get_weather", "description": "Weather for a city",
                                         "input_schema": {"type": "object",
                                                          "properties": {"city": {"type": "string"}}}}], **KW)


register(Scenario("anthropic.messages.tool_use", "anthropic", "anthropic", lambda: [Reply(body=_tu)],
                  [Call(_tu_t)], _run_tool, _integ, tags=["usage", "tools"]))


def _run_500(url):
    _client(url).messages.create(**KW)


register(Scenario("anthropic.messages.http_529", "anthropic", "anthropic",
                  lambda: [Reply(body={"type": "error", "error": {"type": "overloaded_error",
                                                                  "message": "Overloaded"}}, status=529)],
                  [Call(None, completes=False)], _run_500, _integ, expects_exception=True,
                  tags=["lifecycle", "error"]))
