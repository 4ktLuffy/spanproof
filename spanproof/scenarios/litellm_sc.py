"""LiteLLM scenarios (sentry_sdk.integrations.litellm). getsentry/sentry-python#5455 lives here."""

from __future__ import annotations

import asyncio

import litellm  # noqa: F401

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import DUMMY_KEY, Call, Scenario, register


def _integ():
    from sentry_sdk.integrations.litellm import LiteLLMIntegration

    return [LiteLLMIntegration()]


MSG = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Capital of France?"}]


def _flush_callbacks():
    # LiteLLM runs success callbacks on a worker thread; give them a moment.
    import time

    time.sleep(0.5)


_c, _c_t = fx.openai_chat(rid="chatcmpl-sp-litellm")


def _run(url):
    import litellm

    litellm.completion(model="openai/gpt-5-mini", messages=MSG, api_base=url + "/v1", api_key=DUMMY_KEY)
    _flush_callbacks()


register(Scenario("litellm.completion.openai", "litellm", "litellm", lambda: [Reply(body=_c)], [Call(_c_t)], _run,
                  _integ, tags=["usage", "cached", "reasoning"], notes="getsentry/sentry-python#5455"))


def _run_async(url):
    import litellm

    async def go():
        await litellm.acompletion(model="openai/gpt-5-mini", messages=MSG, api_base=url + "/v1", api_key=DUMMY_KEY)
        await asyncio.sleep(0.5)

    asyncio.run(go())


register(Scenario("litellm.acompletion.openai", "litellm", "litellm", lambda: [Reply(body=_c)], [Call(_c_t)],
                  _run_async, _integ, tags=["usage", "async"], notes="getsentry/sentry-python#5455"))

_cs, _cs_t = fx.openai_chat_stream(rid="chatcmpl-sp-litellm-s")


def _run_stream(url):
    import litellm

    s = litellm.completion(model="openai/gpt-5-mini", messages=MSG, api_base=url + "/v1", api_key=DUMMY_KEY,
                           stream=True, stream_options={"include_usage": True})
    for _ in s:
        pass
    _flush_callbacks()


register(Scenario("litellm.completion.openai.stream", "litellm", "litellm", lambda: [Reply(events=_cs)],
                  [Call(_cs_t)], _run_stream, _integ, tags=["usage", "stream"], notes="getsentry/sentry-python#5455"))


def _run_stream_close(url):
    import litellm

    s = litellm.completion(model="openai/gpt-5-mini", messages=MSG, api_base=url + "/v1", api_key=DUMMY_KEY,
                           stream=True, stream_options={"include_usage": True})
    for i, _ in enumerate(s):
        if i == 2:
            break
    _flush_callbacks()


register(Scenario("litellm.completion.openai.stream.early_close", "litellm", "litellm",
                  lambda: [Reply(events=_cs)], [Call(None, completes=False)], _run_stream_close, _integ,
                  tags=["lifecycle", "stream"]))

_a, _a_t = fx.anthropic_message(rid="msg_sp_litellm")


def _run_anthropic(url):
    import litellm

    litellm.completion(model="anthropic/claude-sonnet-5-5", messages=MSG, api_base=url, api_key=DUMMY_KEY,
                       max_tokens=256)
    _flush_callbacks()


register(Scenario("litellm.completion.anthropic", "litellm", "litellm", lambda: [Reply(body=_a)], [Call(_a_t)],
                  _run_anthropic, _integ, tags=["usage", "cached", "cache_write"],
                  notes="getsentry/sentry-python#5455"))
