"""huggingface_hub InferenceClient scenarios (sentry_sdk.integrations.huggingface_hub).

The client points at a TGI-compatible base_url, so no model id is passed (with a model id, old
versions route to the hosted API instead of base_url).
"""

from __future__ import annotations

import asyncio

import huggingface_hub

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import DUMMY_KEY, Call, Scenario, register


def _integ():
    from sentry_sdk.integrations.huggingface_hub import HuggingfaceHubIntegration

    return [HuggingfaceHubIntegration()]


def _client(url):
    return huggingface_hub.InferenceClient(base_url=url, token=DUMMY_KEY)


# Hold the client while a stream is read: huggingface_hub >= 1.0 closes its HTTP session when the client
# is garbage collected, so `for x in InferenceClient(...).chat_completion(stream=True)` fails by itself.


MSG = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Capital of France?"}]
_c, _c_t = fx.hf_chat()


def _run(url):
    _client(url).chat_completion(MSG, max_tokens=256)


register(Scenario("huggingface_hub.chat_completion", "huggingface_hub", "huggingface_hub", lambda: [Reply(body=_c)],
                  [Call(_c_t)], _run, _integ, tags=["usage"]))


def _run_alias(url):
    _client(url).chat.completions.create(messages=MSG, max_tokens=256)


register(Scenario("huggingface_hub.chat.completions.create", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(body=_c)], [Call(_c_t)], _run_alias, _integ, tags=["usage"],
                  notes="getsentry/sentry-python#5848 says this alias is uninstrumented"))


def _run_async(url):
    async def go():
        await huggingface_hub.AsyncInferenceClient(base_url=url, token=DUMMY_KEY).chat_completion(MSG, max_tokens=256)

    asyncio.run(go())


register(Scenario("huggingface_hub.chat_completion.async", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(body=_c)], [Call(_c_t)], _run_async, _integ, tags=["usage", "async"],
                  notes="getsentry/sentry-python#5846"))

_tc, _tc_t = fx.hf_chat(rid="hf-sp-tool", completion=19, tool_calls=[
    {"id": "call_hf1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}])
TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
                                           "parameters": {"type": "object",
                                                          "properties": {"city": {"type": "string"}}}}}]


def _run_tool(url):
    _client(url).chat_completion(MSG, tools=TOOLS, max_tokens=256)


register(Scenario("huggingface_hub.chat_completion.tool_call", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(body=_tc)], [Call(_tc_t)], _run_tool, _integ, tags=["usage", "tools"]))


def _run_503(url):
    _client(url).chat_completion(MSG, max_tokens=256)


register(Scenario("huggingface_hub.chat_completion.http_503", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(body={"error": "Model is overloaded", "error_type": "overloaded"}, status=503)],
                  [Call(None, completes=False)], _run_503, _integ, expects_exception=True,
                  tags=["lifecycle", "error"]))

_se, _se_t = fx.hf_chat_stream()
STREAM = dict(max_tokens=256, stream=True, stream_options={"include_usage": True})


def _run_stream(url):
    c = _client(url)
    for _ in c.chat_completion(MSG, **STREAM):
        pass


register(Scenario("huggingface_hub.chat_completion.stream", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(events=_se)], [Call(_se_t)], _run_stream, _integ, tags=["usage", "stream"]))


def _run_stream_close(url):
    c = _client(url)
    s = c.chat_completion(MSG, **STREAM)
    for i, _ in enumerate(s):
        if i == 1:
            break
    s.close()


register(Scenario("huggingface_hub.chat_completion.stream.early_close", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(events=_se)], [Call(None, completes=False)], _run_stream_close, _integ,
                  tags=["lifecycle", "stream"]))


def _run_stream_break(url):
    # the common form: leave the loop; CPython closes the generator when it goes out of scope
    c = _client(url)
    for i, _ in enumerate(c.chat_completion(MSG, **STREAM)):
        if i == 1:
            break


register(Scenario("huggingface_hub.chat_completion.stream.break", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(events=_se)], [Call(None, completes=False)], _run_stream_break, _integ,
                  tags=["lifecycle", "stream"]))

# TGI reports a failure that happens during generation as an error event inside the stream
_se_err = _se[:2] + [{"error": "Request failed during generation: Server error: CUDA out of memory",
                      "error_type": "generation"}]


def _run_stream_error(url):
    c = _client(url)
    for _ in c.chat_completion(MSG, **STREAM):
        pass


register(Scenario("huggingface_hub.chat_completion.stream.server_error", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(events=_se_err, sse_done=False)], [Call(None, completes=False)], _run_stream_error,
                  _integ, expects_exception=True, raises="huggingface_hub.errors.GenerationError",
                  tags=["lifecycle", "stream", "error"]))

_st, _st_t = fx.hf_chat_stream(rid="hf-sp-tool-s", completion=19, tool_args=['{"ci', 'ty": "Pa', 'ris"}'])


def _run_stream_tool(url):
    c = _client(url)
    for _ in c.chat_completion(MSG, tools=TOOLS, **STREAM):
        pass


register(Scenario("huggingface_hub.chat_completion.stream.tool_call", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(events=_st)], [Call(_st_t)], _run_stream_tool, _integ,
                  tags=["usage", "stream", "tools"]))

_tg, _tg_t = fx.hf_text_generation()


def _run_tg(url):
    _client(url).text_generation("Capital of France?", max_new_tokens=16, details=True, decoder_input_details=True)


register(Scenario("huggingface_hub.text_generation.details", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(body=_tg)], [Call(_tg_t, op="gen_ai.text_completion")], _run_tg, _integ,
                  tags=["usage"]))

_tgs, _tgs_t = fx.hf_text_generation_stream()


def _run_tg_stream(url):
    c = _client(url)
    for _ in c.text_generation("Capital of France?", max_new_tokens=5, details=True, stream=True):
        pass


register(Scenario("huggingface_hub.text_generation.stream", "huggingface_hub", "huggingface_hub",
                  lambda: [Reply(events=_tgs, sse_done=False)], [Call(_tgs_t, op="gen_ai.text_completion")],
                  _run_tg_stream, _integ, tags=["usage", "stream"]))
