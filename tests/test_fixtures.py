"""Every fixture parses with its provider SDK's own types (skipped when the SDK is not installed)."""

import importlib.util

import pytest

from spanproof import fixtures as fx

TOOL = [{"id": "Xq9s1tool", "type": "function", "index": 0,
         "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]
HF_TOOL = [{k: v for k, v in TOOL[0].items() if k != "index"}]
CASES = [
    ("openai", "openai_chat", lambda: fx.openai_chat()[0]),
    ("openai", "openai_chat", lambda: fx.openai_chat(tool_calls=fx.openai_tool_call())[0]),
    ("openai", "openai_chat_chunk", lambda: fx.openai_chat_stream()[0]),
    ("openai", "openai_response", lambda: fx.openai_response()[0]),
    ("openai", "openai_response_events", lambda: fx.openai_response_stream()[0]),
    ("anthropic", "anthropic_message", lambda: fx.anthropic_message()[0]),
    ("anthropic", "anthropic_events", lambda: fx.anthropic_stream()[0]),
    ("google.genai", "genai_response", lambda: fx.genai_response()[0]),
    ("cohere", "cohere_chat", lambda: fx.cohere_chat(cached=16)[0]),
    ("cohere", "cohere_chat", lambda: fx.cohere_chat(tool_calls=[{"name": "f", "parameters": {"a": 1}}])[0]),
    ("cohere", "cohere_chat_events", lambda: fx.cohere_chat_stream()[0]),
    ("cohere", "cohere_embed", lambda: fx.cohere_embed()[0]),
    ("cohere", "cohere_chat_v2", lambda: fx.cohere_chat_v2()[0]),
    ("mistralai", "mistral_chat", lambda: fx.mistral_chat()[0]),
    ("mistralai", "mistral_chat", lambda: fx.mistral_chat(tool_calls=TOOL)[0]),
    ("mistralai", "mistral_chat_chunk", lambda: fx.mistral_chat_stream()[0]),
    ("mistralai", "mistral_embed", lambda: fx.mistral_embed()[0]),
    ("huggingface_hub", "hf_chat", lambda: fx.hf_chat()[0]),
    ("huggingface_hub", "hf_chat", lambda: fx.hf_chat(tool_calls=HF_TOOL)[0]),
    ("huggingface_hub", "hf_chat_chunk", lambda: fx.hf_chat_stream()[0]),
    ("huggingface_hub", "hf_chat_chunk", lambda: fx.hf_chat_stream(tool_args=['{"ci', 'ty": "Paris"}'])[0]),
    ("huggingface_hub", "hf_text_generation", lambda: fx.hf_text_generation()[0]),
    ("huggingface_hub", "hf_text_generation_chunk", lambda: fx.hf_text_generation_stream()[0]),
]


def _have(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except ModuleNotFoundError:
        return False


@pytest.mark.parametrize("mod,kind,build", CASES, ids=[f"{k}-{i}" for i, (_, k, _) in enumerate(CASES)])
def test_fixture_matches_sdk_schema(mod, kind, build):
    if not _have(mod):
        pytest.skip(f"{mod} not installed")
    fx.validate(kind, build())


def test_strict_validation_rejects_drift():
    if _have("cohere"):
        body = fx.cohere_chat()[0]
        with pytest.raises(ValueError):
            fx.validate("cohere_chat", dict(body, generation_idd="x"))  # misspelt key: Cohere's types allow extras
    if _have("huggingface_hub"):
        body = fx.hf_chat()[0]
        with pytest.raises(ValueError):
            fx.validate("hf_chat", dict(body, object="chat.completion"))  # not in huggingface_hub's type
        ev = fx.hf_text_generation_stream()[0]
        del ev[-1]["details"]["input_length"]
        with pytest.raises(ValueError):
            fx.validate("hf_text_generation_chunk", ev)  # required field missing
