"""OpenAI advanced paths: Responses built-in tools (billed per call), incomplete responses, background mode,
previous_response_id chains, structured outputs (.parse), stream helpers, audio and prediction tokens,
service tiers, n>1 choices and embeddings with dimensions.

FIXTURES lists every wire payload so tests/test_fixtures_strict.py validates each with the openai SDK's
own types, rejecting any field the SDK does not declare.
"""

from __future__ import annotations

import asyncio

import openai  # noqa: F401

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import Call, Scenario, register
from .openai_sc import MSG, _aclient, _client, _integ

R = "gen_ai.responses"

# ------------------------------------------------------------ Responses: built-in tools
# Each built-in tool call is billed per call on top of tokens.
_TOOL_ITEMS = [
    {"id": "ws_sp1", "type": "web_search_call", "status": "completed",
     "action": {"type": "search", "query": "capital of France"}},
    {"id": "ws_sp2", "type": "web_search_call", "status": "completed",
     "action": {"type": "open_page", "url": "https://example.org/paris"}},
    {"id": "fs_sp1", "type": "file_search_call", "status": "completed", "queries": ["capital"], "results": None},
    {"id": "ci_sp1", "type": "code_interpreter_call", "status": "completed", "code": "print(1)",
     "container_id": "cntr_sp1", "outputs": [{"type": "logs", "logs": "1"}]},
    {"id": "ig_sp1", "type": "image_generation_call", "status": "completed", "result": "aGVsbG8="},
]
_TOOL_EXTRAS = {"tool_calls.web_search_call": 2, "tool_calls.file_search_call": 1,
                "tool_calls.code_interpreter_call": 1, "tool_calls.image_generation_call": 1}
BUILTIN = [{"type": "web_search"}, {"type": "file_search", "vector_store_ids": ["vs_sp1"]},
           {"type": "code_interpreter", "container": {"type": "auto"}}, {"type": "image_generation"}]
_bt, _bt_t = fx.openai_response_x(rid="resp_spx_tools", tool_items=_TOOL_ITEMS, extras=_TOOL_EXTRAS)


def _run_builtin(url):
    _client(url).responses.create(model="gpt-5-mini", input="Capital of France?", tools=BUILTIN)


register(Scenario("openai.adv.responses.builtin_tools.sync", "openai", "openai", lambda: [Reply(body=_bt)],
                  [Call(_bt_t, op=R)], _run_builtin, _integ, tags=["billing", "builtin_tools"]))

_bts, _bts_n, _bts_t = fx.openai_response_stream_x(rid="resp_spx_tools_s", tool_items=_TOOL_ITEMS,
                                                   extras=_TOOL_EXTRAS)


def _run_builtin_stream(url):
    for _ in _client(url).responses.create(model="gpt-5-mini", input="Capital of France?", tools=BUILTIN,
                                           stream=True):
        pass


register(Scenario("openai.adv.responses.builtin_tools.stream", "openai", "openai",
                  lambda: [Reply(events=_bts, sse_event_names=_bts_n)], [Call(_bts_t, op=R)], _run_builtin_stream,
                  _integ, tags=["billing", "builtin_tools", "stream"]))

# ------------------------------------------------------------ Responses: incomplete (max_output_tokens)
# The response stops at max_output_tokens; every token used is billed. In a stream the final
# event is response.incomplete (not response.completed) and carries the usage.
_inc, _inc_t = fx.openai_response_x(rid="resp_spx_inc", status="incomplete", incomplete_reason="max_output_tokens",
                                    out=4096, reasoning=3900, text="Paris is")


def _run_inc(url):
    _client(url).responses.create(model="gpt-5-mini", input="Capital of France?", max_output_tokens=4096)


register(Scenario("openai.adv.responses.incomplete.sync", "openai", "openai", lambda: [Reply(body=_inc)],
                  [Call(_inc_t, op=R)], _run_inc, _integ, tags=["usage", "incomplete"],
                  notes="control: the blocking path reads usage from the body"))

_incs, _incs_n, _incs_t = fx.openai_response_stream_x(
    terminal="response.incomplete", rid="resp_spx_inc_s", status="incomplete",
    incomplete_reason="max_output_tokens", out=4096, reasoning=3900, text="Paris is")


def _run_inc_stream(url):
    for _ in _client(url).responses.create(model="gpt-5-mini", input="Capital of France?", max_output_tokens=4096,
                                           stream=True):
        pass


register(Scenario("openai.adv.responses.incomplete.stream", "openai", "openai",
                  lambda: [Reply(events=_incs, sse_event_names=_incs_n)], [Call(_incs_t, op=R)], _run_inc_stream,
                  _integ, tags=["usage", "incomplete", "stream"]))


def _run_inc_stream_async(url):
    async def go():
        s = await _aclient(url).responses.create(model="gpt-5-mini", input="Capital of France?",
                                                 max_output_tokens=4096, stream=True)
        async for _ in s:
            pass

    asyncio.run(go())


register(Scenario("openai.adv.responses.incomplete.stream.async", "openai", "openai",
                  lambda: [Reply(events=_incs, sse_event_names=_incs_n)], [Call(_incs_t, op=R)],
                  _run_inc_stream_async, _integ, tags=["usage", "incomplete", "stream", "async"]))

# reasoning model that spends the whole budget thinking: no text at all, 4096 reasoning tokens billed
_incr, _incr_n, _incr_t = fx.openai_response_stream_x(
    terminal="response.incomplete", rid="resp_spx_inc_r", status="incomplete",
    incomplete_reason="max_output_tokens", out=4096, reasoning=4096, text="")

register(Scenario("openai.adv.responses.incomplete_reasoning_only.stream", "openai", "openai",
                  lambda: [Reply(events=_incr, sse_event_names=_incr_n)], [Call(_incr_t, op=R)], _run_inc_stream,
                  _integ, tags=["usage", "incomplete", "reasoning", "stream"]))

# ------------------------------------------------------------ Responses: background mode
# create(background=True) answers "queued" with no usage; the billed result arrives via retrieve().
_bg_q, _ = fx.openai_response_x(rid="resp_spx_bg", status="queued", text="", reasoning=0, usage=False,
                                background=True)
_bg_q["output"] = []
_bg_done, _bg_t = fx.openai_response_x(rid="resp_spx_bg", background=True)


def _run_background(url):
    c = _client(url)
    r = c.responses.create(model="gpt-5-mini", input="Capital of France?", background=True)
    c.responses.retrieve(r.id)


register(Scenario("openai.adv.responses.background", "openai", "openai",
                  lambda: [Reply(body=_bg_q), Reply(body=_bg_done)], [Call(_bg_t, op=R)], _run_background, _integ,
                  tags=["usage", "background"]))

# ------------------------------------------------------------ Responses: previous_response_id chain
_c1, _c1_t = fx.openai_response_x(rid="resp_spx_c1", inp=300, out=40, reasoning=0)
_c2, _c2_t = fx.openai_response_x(rid="resp_spx_c2", inp=420, out=30, cached=256, reasoning=0, text="Lyon.")


def _run_chain(url):
    c = _client(url)
    r = c.responses.create(model="gpt-5-mini", input="Capital of France?")
    c.responses.create(model="gpt-5-mini", input="And second city?", previous_response_id=r.id)


register(Scenario("openai.adv.responses.previous_response_id", "openai", "openai",
                  lambda: [Reply(body=_c1), Reply(body=_c2)], [Call(_c1_t, op=R), Call(_c2_t, op=R)], _run_chain,
                  _integ, tags=["usage", "chain"]))

# ------------------------------------------------------------ Responses: service tier
_tier, _tier_t = fx.openai_response_x(rid="resp_spx_flex", service_tier="flex", extras={"service_tier": "flex"})


def _run_tier(url):
    _client(url).responses.create(model="gpt-5-mini", input="Capital of France?", service_tier="flex")


register(Scenario("openai.adv.responses.service_tier", "openai", "openai", lambda: [Reply(body=_tier)],
                  [Call(_tier_t, op=R)], _run_tier, _integ, tags=["billing", "service_tier"]))

# ------------------------------------------------------------ structured outputs
_rp, _rp_t = fx.openai_response_x(rid="resp_spx_parse", text='{"city": "Paris"}', reasoning=0)


def _city():
    from pydantic import BaseModel

    class City(BaseModel):
        city: str

    return City


def _run_resp_parse(url):
    _client(url).responses.parse(model="gpt-5-mini", input="Capital of France?", text_format=_city())


register(Scenario("openai.adv.responses.parse", "openai", "openai", lambda: [Reply(body=_rp)],
                  [Call(_rp_t, op=R)], _run_resp_parse, _integ, tags=["lifecycle", "structured_output"],
                  notes="getsentry/sentry-python#5401"))

_cp, _cp_t = fx.openai_chat_x(rid="chatcmpl-spx-parse", texts=('{"city": "Paris"}',))


def _run_chat_parse(url):
    c = _client(url)
    chat = c.chat.completions if hasattr(c.chat.completions, "parse") else c.beta.chat.completions
    chat.parse(model="gpt-5-mini", messages=MSG, response_format=_city())


register(Scenario("openai.adv.chat.parse", "openai", "openai", lambda: [Reply(body=_cp)], [Call(_cp_t)],
                  _run_chat_parse, _integ, tags=["lifecycle", "structured_output"]))


def _run_chat_parse_async(url):
    async def go():
        c = _aclient(url)
        chat = c.chat.completions if hasattr(c.chat.completions, "parse") else c.beta.chat.completions
        await chat.parse(model="gpt-5-mini", messages=MSG, response_format=_city())

    asyncio.run(go())


register(Scenario("openai.adv.chat.parse.async", "openai", "openai", lambda: [Reply(body=_cp)], [Call(_cp_t)],
                  _run_chat_parse_async, _integ, tags=["lifecycle", "structured_output", "async"]))

# ------------------------------------------------------------ stream helpers (controls)
_rsh, _rsh_n, _rsh_t = fx.openai_response_stream_x(rid="resp_spx_helper")


def _run_resp_stream_helper(url):
    with _client(url).responses.stream(model="gpt-5-mini", input="Capital of France?") as s:
        for _ in s:
            pass


register(Scenario("openai.adv.responses.stream_helper", "openai", "openai",
                  lambda: [Reply(events=_rsh, sse_event_names=_rsh_n)], [Call(_rsh_t, op=R)],
                  _run_resp_stream_helper, _integ, tags=["usage", "stream"]))

_csh, _csh_t = fx.openai_chat_stream(rid="chatcmpl-spx-helper")


def _run_chat_stream_helper(url):
    c = _client(url)
    chat = c.chat.completions if hasattr(c.chat.completions, "stream") else c.beta.chat.completions
    with chat.stream(model="gpt-5-mini", messages=MSG, stream_options={"include_usage": True}) as s:
        for _ in s:
            pass


register(Scenario("openai.adv.chat.stream_helper", "openai", "openai", lambda: [Reply(events=_csh)],
                  [Call(_csh_t)], _run_chat_stream_helper, _integ, tags=["usage", "stream"]))

# ------------------------------------------------------------ chat: audio, predictions, tier, n>1
# gpt-4o-audio: audio tokens are part of prompt/completion tokens but priced far above text tokens.
_au, _au_t = fx.openai_chat_x(rid="chatcmpl-spx-audio", model="gpt-4o-audio-preview-2025-06-03", texts=(None,),
                              prompt=260, completion=180, audio_in=200, audio_out=150,
                              extras={"audio.input_tokens": 200, "audio.output_tokens": 150},
                              message_extra={"audio": {"id": "audio_sp1", "data": "UklGRg==", "expires_at": 1790003600,
                                                       "transcript": "Paris."}})


def _run_audio(url):
    _client(url).chat.completions.create(model="gpt-4o-audio-preview", messages=MSG, modalities=["text", "audio"],
                                         audio={"voice": "alloy", "format": "wav"})


register(Scenario("openai.adv.chat.audio_tokens", "openai", "openai", lambda: [Reply(body=_au)], [Call(_au_t)],
                  _run_audio, _integ, tags=["billing", "audio"]))

# Predicted outputs: rejected prediction tokens are billed and already inside completion_tokens.
_pr, _pr_t = fx.openai_chat_x(rid="chatcmpl-spx-pred", completion=300, accepted=120, rejected=60)


def _run_pred(url):
    _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG,
                                         prediction={"type": "content", "content": "Paris."})


register(Scenario("openai.adv.chat.predictions", "openai", "openai", lambda: [Reply(body=_pr)], [Call(_pr_t)],
                  _run_pred, _integ, tags=["usage", "predictions"],
                  notes="control: rejected prediction tokens are inside completion_tokens"))

_ct, _ct_t = fx.openai_chat_x(rid="chatcmpl-spx-prio", service_tier="priority", extras={"service_tier": "priority"})


def _run_chat_tier(url):
    _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG, service_tier="priority")


register(Scenario("openai.adv.chat.service_tier", "openai", "openai", lambda: [Reply(body=_ct)], [Call(_ct_t)],
                  _run_chat_tier, _integ, tags=["billing", "service_tier"]))

_n2, _n2_t = fx.openai_chat_x(rid="chatcmpl-spx-n2", texts=("Paris is it.", "It is Paris."))


def _run_n2(url):
    _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG, n=2)


register(Scenario("openai.adv.chat.n2.sync", "openai", "openai", lambda: [Reply(body=_n2)], [Call(_n2_t)], _run_n2,
                  _integ, tags=["usage", "choices"]))

_n2s, _n2s_t = fx.openai_chat_stream_n()


def _run_n2_stream(url):
    for _ in _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG, n=2, stream=True,
                                                  stream_options={"include_usage": True}):
        pass


register(Scenario("openai.adv.chat.n2.stream", "openai", "openai", lambda: [Reply(events=_n2s)], [Call(_n2s_t)],
                  _run_n2_stream, _integ, tags=["usage", "choices", "stream"]))

_n1s, _n1s_t = fx.openai_chat_stream_n(rid="chatcmpl-spx-n1", texts=("Paris is it.",))


def _run_chat_stream_n1(url):
    for _ in _client(url).chat.completions.create(model="gpt-5-mini", messages=MSG, stream=True,
                                                  stream_options={"include_usage": True}):
        pass


register(Scenario("openai.adv.chat.n1.stream", "openai", "openai", lambda: [Reply(events=_n1s)], [Call(_n1s_t)],
                  _run_chat_stream_n1, _integ, tags=["usage", "stream"],
                  notes="control for openai.adv.chat.n2.stream: same chunk shape, one choice"))

# ------------------------------------------------------------ embeddings with dimensions
_emb, _emb_t = fx.openai_embedding()


def _run_emb(url):
    _client(url).embeddings.create(model="text-embedding-3-large", input=["Paris", "Lyon"], dimensions=256)


register(Scenario("openai.adv.embeddings.dimensions", "openai", "openai", lambda: [Reply(body=_emb)],
                  [Call(_emb_t, op="gen_ai.embeddings")], _run_emb, _integ, tags=["usage", "embeddings"]))

# ------------------------------------------------------------ fixtures (strictly validated in tests)
FIXTURES = [("openai_response", b) for b in (_bt, _inc, _bg_q, _bg_done, _c1, _c2, _tier, _rp)] + \
           [("openai_response_events", e) for e in (_bts, _incs, _incr, _rsh)] + \
           [("openai_chat", b) for b in (_cp, _au, _pr, _ct, _n2)] + \
           [("openai_chat_chunk", _n2s), ("openai_chat_chunk", _n1s), ("openai_chat_chunk", _csh),
            ("openai_embedding", _emb)]
