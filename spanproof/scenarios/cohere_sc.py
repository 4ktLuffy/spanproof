"""Cohere Python SDK scenarios (sentry_sdk.integrations.cohere)."""

from __future__ import annotations

import asyncio

import cohere

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import DUMMY_KEY, Call, Scenario, register


def _integ():
    from sentry_sdk.integrations.cohere import CohereIntegration

    return [CohereIntegration()]


def _client(url):
    return cohere.Client(api_key=DUMMY_KEY, base_url=url)


NO_RETRY = {"max_retries": 0}
KW = dict(model="command-a-03-2025", message="Capital of France?", preamble="Be brief.",
          chat_history=[{"role": "USER", "message": "Hi"}, {"role": "CHATBOT", "message": "Hello."}])
_c, _c_t = fx.cohere_chat()


def _run(url):
    _client(url).chat(**KW)


register(Scenario("cohere.chat.sync", "cohere", "cohere", lambda: [Reply(body=_c)], [Call(_c_t)], _run, _integ,
                  tags=["usage"]))

_cc, _cc_t = fx.cohere_chat(gid="c0h-gen-sp-cache", billed_in=2048, tokens_in=2240, cached=1536)


def _run_cached(url):
    _client(url).chat(**KW)


register(Scenario("cohere.chat.cached", "cohere", "cohere", lambda: [Reply(body=_cc)], [Call(_cc_t)], _run_cached,
                  _integ, tags=["usage", "cached"], notes="meta.cached_tokens: prompt tokens that hit Cohere's cache"))


def _run_async(url):
    async def go():
        await cohere.AsyncClient(api_key=DUMMY_KEY, base_url=url).chat(**KW)

    asyncio.run(go())


register(Scenario("cohere.chat.async", "cohere", "cohere", lambda: [Reply(body=_c)], [Call(_c_t)], _run_async, _integ,
                  tags=["usage", "async"], notes="getsentry/sentry-python#5845"))

_se, _se_t = fx.cohere_chat_stream()


def _run_stream(url):
    for _ in _client(url).chat_stream(**KW):
        pass


register(Scenario("cohere.chat_stream", "cohere", "cohere", lambda: [Reply(events=_se, ndjson=True)], [Call(_se_t)],
                  _run_stream, _integ, tags=["usage", "stream"]))


def _run_stream_close(url):
    s = _client(url).chat_stream(**KW)
    for i, _ in enumerate(s):
        if i == 1:
            break
    s.close()


register(Scenario("cohere.chat_stream.early_close", "cohere", "cohere", lambda: [Reply(events=_se, ndjson=True)],
                  [Call(None, completes=False)], _run_stream_close, _integ, tags=["lifecycle", "stream"]))


def _run_stream_drop(url):
    for _ in _client(url).chat_stream(**KW):
        pass


register(Scenario("cohere.chat_stream.connection_drop", "cohere", "cohere",
                  lambda: [Reply(events=_se, ndjson=True, chunked=True, cut_after=3)], [Call(None, completes=False)],
                  _run_stream_drop, _integ, expects_exception=True, raises="httpx.RemoteProtocolError",
                  tags=["lifecycle", "stream", "error"]))

_tc, _tc_t = fx.cohere_chat(gid="c0h-gen-sp-tool", billed_out=21, tokens_out=21,
                            tool_calls=[{"name": "get_weather", "parameters": {"city": "Paris"}}])
TOOLS = [{"name": "get_weather", "description": "Weather for a city",
          "parameter_definitions": {"city": {"type": "str", "description": "City", "required": True}}}]


def _run_tool(url):
    _client(url).chat(tools=TOOLS, **KW)


register(Scenario("cohere.chat.tool_call", "cohere", "cohere", lambda: [Reply(body=_tc)], [Call(_tc_t)], _run_tool,
                  _integ, tags=["usage", "tools"]))


def _run_500(url):
    _client(url).chat(request_options=NO_RETRY, **KW)


register(Scenario("cohere.chat.http_500", "cohere", "cohere",
                  lambda: [Reply(body={"message": "internal server error"}, status=500)],
                  [Call(None, completes=False)], _run_500, _integ, expects_exception=True, tags=["lifecycle", "error"]))

_e, _e_t = fx.cohere_embed()


def _run_embed(url):
    _client(url).embed(texts=["Paris"], model="embed-v4.0", input_type="search_document")


register(Scenario("cohere.embed", "cohere", "cohere", lambda: [Reply(body=_e)],
                  [Call(_e_t, op="gen_ai.embeddings")], _run_embed, _integ, tags=["usage", "embeddings"]))

# 100 texts: the SDK splits them into batches of 96 and 4, sends both, and merges the replies
_eb1, _ = fx.cohere_embed(eid="c0h-emb-b1", texts=["Paris"] * 96, billed_in=384)
_eb2, _ = fx.cohere_embed(eid="c0h-emb-b2", texts=["Paris"] * 4, billed_in=16)


def _run_embed_batched(url):
    _client(url).embed(texts=["Paris"] * 100, model="embed-v4.0", input_type="search_document")


register(Scenario("cohere.embed.batched", "cohere", "cohere", lambda: [Reply(body=_eb1), Reply(body=_eb2)],
                  [Call(fx.Truth(400, 0), op="gen_ai.embeddings")], _run_embed_batched, _integ,
                  tags=["usage", "embeddings"]))

_v2, _v2_t = fx.cohere_chat_v2()


def _run_v2(url):
    cohere.ClientV2(api_key=DUMMY_KEY, base_url=url).chat(
        model="command-a-03-2025", messages=[{"role": "system", "content": "Be brief."},
                                             {"role": "user", "content": "Capital of France?"}])


register(Scenario("cohere.v2.chat", "cohere", "cohere", lambda: [Reply(body=_v2)], [Call(_v2_t)], _run_v2, _integ,
                  tags=["usage"], notes="getsentry/sentry-python#5844"))
