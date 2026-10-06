"""Google GenAI SDK scenarios (sentry_sdk.integrations.google_genai)."""

from __future__ import annotations

from google import genai  # noqa: F401

from .. import fixtures as fx
from ..mockserver import Reply
from ..scenario import DUMMY_KEY, Call, Scenario, register


def _integ():
    from sentry_sdk.integrations.google_genai import GoogleGenAIIntegration

    return [GoogleGenAIIntegration()]


def _client(url):
    from google import genai
    from google.genai import types

    return genai.Client(api_key=DUMMY_KEY, http_options=types.HttpOptions(base_url=url))


_g, _g_t = fx.genai_response()


def _run(url):
    c = _client(url)
    c.models.generate_content(model="gemini-3-flash", contents="Capital of France?")


register(Scenario("google_genai.generate_content", "google_genai", "google-genai", lambda: [Reply(body=_g)],
                  [Call(_g_t)], _run, _integ, tags=["usage", "cached", "reasoning"]))


# Stream: Gemini sends cumulative usageMetadata on each chunk; the last one is final.
def _stream_events():
    return fx.genai_stream()[0]


_gs_t = fx.Truth(900, 200 + 150, cached=512, reasoning=150, model="gemini-3-flash", response_id="gen_sp2")


def _run_stream(url):
    c = _client(url)
    for _ in c.models.generate_content_stream(model="gemini-3-flash", contents="Capital of France?"):
        pass


register(Scenario("google_genai.generate_content_stream", "google_genai", "google-genai",
                  lambda: [Reply(events=_stream_events(), sse_done=False)], [Call(_gs_t)], _run_stream, _integ,
                  tags=["usage", "stream"]))


def _run_stream_close(url):
    c = _client(url)
    s = c.models.generate_content_stream(model="gemini-3-flash", contents="Capital of France?")
    for i, _ in enumerate(s):
        if i == 1:
            break
    s.close()


register(Scenario("google_genai.generate_content_stream.early_close", "google_genai", "google-genai",
                  lambda: [Reply(events=_stream_events(), sse_done=False)], [Call(None, completes=False)],
                  _run_stream_close, _integ, tags=["lifecycle", "stream"]))
