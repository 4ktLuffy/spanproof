"""Anthropic advanced paths: extended thinking, server tools, cache TTLs, stop reasons, tool round trip,
structured output (messages.parse), the beta namespace and token counting.

Wire shapes follow the published Messages API; FIXTURES lists them so tests/test_fixtures_strict.py
validates each with the anthropic SDK's own types, rejecting any field the SDK does not declare.
On SDK versions that predate a feature, the scenarios that need it are reported as unsupported.
"""

from __future__ import annotations

import asyncio

import anthropic  # noqa: F401

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import Call, Scenario, register
from .anthropic_sc import KW, _aclient, _client, _integ

THINK = {"type": "enabled", "budget_tokens": 2048}
SIG = "EqQBCkYIARgCKkCspanproofsig"

# ------------------------------------------------------------ extended thinking
# usage.output_tokens_details.thinking_tokens: the billed output tokens spent on reasoning
# (output_tokens stays the inclusive total). getsentry/sentry-python#5802.
_think, _think_t = fx.anthropic_message_x(
    rid="msg_spx_think", out=1100, reasoning=900,
    content=[{"type": "thinking", "thinking": "France's capital is Paris.", "signature": SIG},
             {"type": "redacted_thinking", "data": "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT"},
             {"type": "text", "text": "Paris."}],
    usage_extra={"output_tokens_details": {"thinking_tokens": 900}})


def _run_think(url):
    _client(url).messages.create(thinking=THINK, **dict(KW, max_tokens=4096))


register(Scenario("anthropic.adv.thinking.sync", "anthropic", "anthropic", lambda: [Reply(body=_think)],
                  [Call(_think_t)], _run_think, _integ, tags=["usage", "reasoning", "thinking"],
                  notes="getsentry/sentry-python#5802"))

_think_s, _think_sn, _think_st = fx.anthropic_stream_x(
    rid="msg_spx_think_s", out=1100, reasoning=900, text=["Paris."],
    blocks=[({"type": "thinking", "thinking": "", "signature": ""},
             [{"type": "thinking_delta", "thinking": "France's capital "},
              {"type": "thinking_delta", "thinking": "is Paris."},
              {"type": "signature_delta", "signature": SIG}]),
            ({"type": "redacted_thinking", "data": "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT"}, []),
            ({"type": "text", "text": ""}, [{"type": "text_delta", "text": "Paris."}])],
    delta_usage_extra={"output_tokens_details": {"thinking_tokens": 900}})


def _run_think_stream(url):
    for _ in _client(url).messages.create(stream=True, thinking=THINK, **dict(KW, max_tokens=4096)):
        pass


register(Scenario("anthropic.adv.thinking.stream", "anthropic", "anthropic",
                  lambda: [Reply(events=_think_s, sse_event_names=_think_sn)], [Call(_think_st)], _run_think_stream,
                  _integ, tags=["usage", "reasoning", "thinking", "stream"], notes="getsentry/sentry-python#5802"))


def _run_think_helper(url):
    with _client(url).messages.stream(thinking=THINK, **dict(KW, max_tokens=4096)) as s:
        for _ in s:
            pass


register(Scenario("anthropic.adv.thinking.stream_helper", "anthropic", "anthropic",
                  lambda: [Reply(events=_think_s, sse_event_names=_think_sn)], [Call(_think_st)], _run_think_helper,
                  _integ, tags=["usage", "reasoning", "thinking", "stream"]))

# ------------------------------------------------------------ server tools
# web_search is billed per search on top of tokens; usage.server_tool_use counts them.
# The turn ends with pause_turn (the server-side loop hit its iteration limit).
WS = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
_ws_content = [
    {"type": "server_tool_use", "id": "srvtoolu_sp1", "name": "web_search", "input": {"query": "capital of France"}},
    {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_sp1",
     "content": [{"type": "web_search_result", "url": "https://example.org/paris", "title": "Paris",
                  "encrypted_content": "EqgfCioIARgB", "page_age": None}]},
    {"type": "server_tool_use", "id": "srvtoolu_sp2", "name": "web_fetch", "input": {"url": "https://example.org/paris"}},
    {"type": "text", "text": "Paris.", "citations": [
        {"type": "web_search_result_location", "url": "https://example.org/paris", "title": "Paris",
         "encrypted_index": "Eo8BCioIAhgB", "cited_text": "Paris is the capital of France."}]},
]
_SRV = {"server_tool.web_search_requests": 3, "server_tool.web_fetch_requests": 1}
_ws, _ws_t = fx.anthropic_message_x(rid="msg_spx_ws", inp=9200, out=410, content=_ws_content, stop_reason="pause_turn",
                                    usage_extra={"server_tool_use": {"web_search_requests": 3,
                                                                     "web_fetch_requests": 1}}, extras=_SRV)


def _run_ws(url):
    _client(url).messages.create(tools=WS, **KW)


register(Scenario("anthropic.adv.server_tools.sync", "anthropic", "anthropic", lambda: [Reply(body=_ws)],
                  [Call(_ws_t)], _run_ws, _integ, tags=["billing", "server_tools", "pause_turn", "citations"]))

_ws_s, _ws_sn, _ws_st = fx.anthropic_stream_x(
    rid="msg_spx_ws_s", inp=9200, out=410, stop_reason="pause_turn", extras=_SRV, text=["Paris."],
    blocks=[({"type": "server_tool_use", "id": "srvtoolu_sp1", "name": "web_search", "input": {}},
             [{"type": "input_json_delta", "partial_json": '{"query": "capital of France"}'}]),
            (_ws_content[1], []),
            ({"type": "text", "text": "", "citations": []},
             [{"type": "citations_delta", "citation": _ws_content[3]["citations"][0]},
              {"type": "text_delta", "text": "Paris."}])],
    delta_usage_extra={"server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 1}})


def _run_ws_stream(url):
    for _ in _client(url).messages.create(stream=True, tools=WS, **KW):
        pass


register(Scenario("anthropic.adv.server_tools.stream", "anthropic", "anthropic",
                  lambda: [Reply(events=_ws_s, sse_event_names=_ws_sn)], [Call(_ws_st)], _run_ws_stream, _integ,
                  tags=["billing", "server_tools", "pause_turn", "stream"]))

# ------------------------------------------------------------ prompt cache TTLs
# A 1h cache write costs 2x base input, a 5m write 1.25x; usage.cache_creation splits them.
_TTL = {"cache_write.ephemeral_5m": 300, "cache_write.ephemeral_1h": 1700}
_ttl, _ttl_t = fx.anthropic_message_x(rid="msg_spx_ttl", cache_read=4096, cache_creation=2000, extras=_TTL,
                                      content=[{"type": "text", "text": "Paris."}],
                                      usage_extra={"cache_creation": {"ephemeral_5m_input_tokens": 300,
                                                                      "ephemeral_1h_input_tokens": 1700}})
SYS_1H = [{"type": "text", "text": "Long policy document.", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


def _run_ttl(url):
    _client(url).messages.create(**dict(KW, system=SYS_1H))


register(Scenario("anthropic.adv.cache_ttl.sync", "anthropic", "anthropic", lambda: [Reply(body=_ttl)],
                  [Call(_ttl_t)], _run_ttl, _integ, tags=["billing", "cache_write", "cached"]))

_ttl_s, _ttl_sn, _ttl_st = fx.anthropic_stream_x(
    rid="msg_spx_ttl_s", cache_read=4096, cache_creation=2000, extras=_TTL,
    blocks=[({"type": "text", "text": ""}, [{"type": "text_delta", "text": "Paris."}])])
# message_start carries the TTL split in a real stream
_ttl_s[0]["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 1700}


def _run_ttl_stream(url):
    for _ in _client(url).messages.create(stream=True, **dict(KW, system=SYS_1H)):
        pass


register(Scenario("anthropic.adv.cache_ttl.stream", "anthropic", "anthropic",
                  lambda: [Reply(events=_ttl_s, sse_event_names=_ttl_sn)], [Call(_ttl_st)], _run_ttl_stream, _integ,
                  tags=["billing", "cache_write", "stream"]))

# ------------------------------------------------------------ stop reasons
for _reason in ("refusal", "model_context_window_exceeded", "max_tokens"):
    _b, _bt = fx.anthropic_message_x(rid=f"msg_spx_{_reason}", content=[{"type": "text", "text": "I can't help."}],
                                     stop_reason=_reason)
    register(Scenario(f"anthropic.adv.stop_reason.{_reason}", "anthropic", "anthropic",
                      (lambda b=_b: [Reply(body=b)]), [Call(_bt)], lambda url: _client(url).messages.create(**KW),
                      _integ, tags=["identity", "finish"]))

_ref_s, _ref_sn, _ref_st = fx.anthropic_stream_x(
    rid="msg_spx_refusal_s", stop_reason="refusal", text=["I can't help."],
    blocks=[({"type": "text", "text": ""}, [{"type": "text_delta", "text": "I can't help."}])])


def _run_refusal_stream(url):
    for _ in _client(url).messages.create(stream=True, **KW):
        pass


register(Scenario("anthropic.adv.stop_reason.refusal.stream", "anthropic", "anthropic",
                  lambda: [Reply(events=_ref_s, sse_event_names=_ref_sn)], [Call(_ref_st)], _run_refusal_stream,
                  _integ, tags=["identity", "finish", "stream"]))

# ------------------------------------------------------------ tool use round trip
TOOLS = [{"name": "get_weather", "description": "Weather for a city",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
_rt1, _rt1_t = fx.anthropic_message_x(
    rid="msg_spx_rt1", out=60, stop_reason="tool_use",
    content=[{"type": "text", "text": "Let me check."},
             {"type": "tool_use", "id": "toolu_sp1", "name": "get_weather", "input": {"city": "Paris"}}])
_rt2, _rt2_t = fx.anthropic_message_x(rid="msg_spx_rt2", inp=180, out=30,
                                      content=[{"type": "text", "text": "It is 18C in Paris."}])


def _run_round_trip(url):
    c = _client(url)
    msgs = [{"role": "user", "content": "Weather in Paris?"}]
    r = c.messages.create(model="claude-sonnet-5-5", max_tokens=256, tools=TOOLS, messages=msgs)
    tu = next(b for b in r.content if b.type == "tool_use")
    msgs += [{"role": "assistant", "content": [b.model_dump() for b in r.content]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu.id, "content": "18C"}]}]
    c.messages.create(model="claude-sonnet-5-5", max_tokens=256, tools=TOOLS, messages=msgs)


register(Scenario("anthropic.adv.tool_round_trip.sync", "anthropic", "anthropic",
                  lambda: [Reply(body=_rt1), Reply(body=_rt2)], [Call(_rt1_t), Call(_rt2_t)], _run_round_trip,
                  _integ, tags=["usage", "tools"]))

_rt1_s, _rt1_sn, _rt1_st = fx.anthropic_stream_x(
    rid="msg_spx_rt1_s", out=60, stop_reason="tool_use", text=["Let me check."],
    blocks=[({"type": "text", "text": ""}, [{"type": "text_delta", "text": "Let me check."}]),
            ({"type": "tool_use", "id": "toolu_sp1", "name": "get_weather", "input": {}},
             [{"type": "input_json_delta", "partial_json": '{"city": '},
              {"type": "input_json_delta", "partial_json": '"Paris"}'}])])
_rt2_s, _rt2_sn, _rt2_st = fx.anthropic_stream_x(
    rid="msg_spx_rt2_s", inp=180, out=30, text=["It is 18C in Paris."],
    blocks=[({"type": "text", "text": ""}, [{"type": "text_delta", "text": "It is 18C in Paris."}])])


def _run_round_trip_stream(url):
    c = _client(url)
    msgs = [{"role": "user", "content": "Weather in Paris?"}]
    with c.messages.stream(model="claude-sonnet-5-5", max_tokens=256, tools=TOOLS, messages=msgs) as s:
        for _ in s:
            pass
        r = s.get_final_message()
    tu = next(b for b in r.content if b.type == "tool_use")
    msgs += [{"role": "assistant", "content": [b.model_dump() for b in r.content]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu.id, "content": "18C"}]}]
    for _ in c.messages.create(model="claude-sonnet-5-5", max_tokens=256, tools=TOOLS, messages=msgs, stream=True):
        pass


register(Scenario("anthropic.adv.tool_round_trip.stream", "anthropic", "anthropic",
                  lambda: [Reply(events=_rt1_s, sse_event_names=_rt1_sn), Reply(events=_rt2_s, sse_event_names=_rt2_sn)],
                  [Call(_rt1_st), Call(_rt2_st)], _run_round_trip_stream, _integ, tags=["usage", "tools", "stream"]))

# ------------------------------------------------------------ structured output: messages.parse
_parse, _parse_t = fx.anthropic_message_x(rid="msg_spx_parse", content=[{"type": "text", "text": '{"city": "Paris"}'}])


def _run_parse(url):
    from pydantic import BaseModel

    class City(BaseModel):
        city: str

    _client(url).messages.parse(output_format=City, **KW)


register(Scenario("anthropic.adv.parse.sync", "anthropic", "anthropic", lambda: [Reply(body=_parse)],
                  [Call(_parse_t)], _run_parse, _integ, tags=["lifecycle", "structured_output"]))


def _run_parse_async(url):
    from pydantic import BaseModel

    class City(BaseModel):
        city: str

    async def go():
        await _aclient(url).messages.parse(output_format=City, **KW)

    asyncio.run(go())


register(Scenario("anthropic.adv.parse.async", "anthropic", "anthropic", lambda: [Reply(body=_parse)],
                  [Call(_parse_t)], _run_parse_async, _integ, tags=["lifecycle", "structured_output", "async"]))

# ------------------------------------------------------------ beta namespace (#5806)
_beta, _beta_t = fx.anthropic_message_x(rid="msg_spx_beta", content=[{"type": "text", "text": "Paris."}],
                                        cache_read=2048, cache_creation=512)


def _run_beta(url):
    _client(url).beta.messages.create(betas=["context-management-2025-06-27"], **KW)


register(Scenario("anthropic.adv.beta.sync", "anthropic", "anthropic", lambda: [Reply(body=_beta)],
                  [Call(_beta_t)], _run_beta, _integ, tags=["lifecycle", "beta"],
                  notes="getsentry/sentry-python#5806"))

_beta_s, _beta_sn, _beta_st = fx.anthropic_stream_x(
    rid="msg_spx_beta_s", blocks=[({"type": "text", "text": ""}, [{"type": "text_delta", "text": "Paris."}])])


def _run_beta_stream(url):
    with _client(url).beta.messages.stream(betas=["context-management-2025-06-27"], **KW) as s:
        for _ in s:
            pass


register(Scenario("anthropic.adv.beta.stream", "anthropic", "anthropic",
                  lambda: [Reply(events=_beta_s, sse_event_names=_beta_sn)], [Call(_beta_st)], _run_beta_stream,
                  _integ, tags=["lifecycle", "beta", "stream"], notes="getsentry/sentry-python#5806"))


# ------------------------------------------------------------ fixtures (strictly validated in tests)
FIXTURES = [("anthropic_message", b) for b in (_think, _ws, _ttl, _rt1, _rt2, _parse, _beta)] + \
           [("anthropic_events", e) for e in (_think_s, _ws_s, _ttl_s, _ref_s, _rt1_s, _rt2_s, _beta_s)]
