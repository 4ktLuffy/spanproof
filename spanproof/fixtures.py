"""Wire-response builders for each provider, and the ground truth they imply.

Every builder returns (wire payload, Truth). The wire payload follows the provider's
published schema, and `validate()` parses it with the provider SDK's own response
types, so a fixture that drifts from the real schema fails loudly instead of
producing a false finding.

Truth is expressed in Sentry's convention (sentry-conventions):
  input_tokens  includes cached and cache-write tokens
  output_tokens includes reasoning tokens
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class Truth:
    input_tokens: int
    output_tokens: int
    cached: int = 0
    cache_write: int = 0
    reasoning: int = 0
    model: str = ""
    response_id: str = ""
    finish: str = ""

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        d = asdict(self)
        d["total"] = self.total
        return d


# ---------------------------------------------------------------- OpenAI chat

def openai_chat(*, model="gpt-5-mini-2026-08-07", rid="chatcmpl-sp1", content="Paris.",
                prompt=1200, completion=300, cached=1024, reasoning=256, tool_calls=None):
    msg = {"role": "assistant", "content": None if tool_calls else content, "refusal": None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    body = {
        "id": rid,
        "object": "chat.completion",
        "created": 1790000000,
        "model": model,
        "choices": [{"index": 0, "message": msg, "logprobs": None,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": reasoning, "audio_tokens": 0,
                                          "accepted_prediction_tokens": 0,
                                          "rejected_prediction_tokens": 0},
        },
        "service_tier": "default",
        "system_fingerprint": "fp_spanproof",
    }
    return body, Truth(prompt, completion, cached=cached, reasoning=reasoning, model=model, response_id=rid,
                       finish="tool_calls" if tool_calls else "stop")


def openai_tool_call(name="get_weather", args='{"city":"Paris"}', cid="call_sp1"):
    return [{"id": cid, "type": "function", "function": {"name": name, "arguments": args}}]


def openai_chat_stream(*, model="gpt-5-mini-2026-08-07", rid="chatcmpl-sp2", text="The capital is Paris.",
                       prompt=1200, completion=300, cached=1024, reasoning=256, include_usage=True):
    base = {"id": rid, "object": "chat.completion.chunk", "created": 1790000000, "model": model,
            "system_fingerprint": "fp_spanproof", "service_tier": "default"}
    events = [dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": ""},
                                   "logprobs": None, "finish_reason": None}], usage=None)]
    for word in text.split(" "):
        events.append(dict(base, choices=[{"index": 0, "delta": {"content": word + " "},
                                           "logprobs": None, "finish_reason": None}], usage=None))
    events.append(dict(base, choices=[{"index": 0, "delta": {}, "logprobs": None, "finish_reason": "stop"}],
                       usage=None))
    if include_usage:
        events.append(dict(base, choices=[], usage={
            "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": reasoning, "audio_tokens": 0,
                                          "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0}}))
    return events, Truth(prompt, completion, cached=cached, reasoning=reasoning, model=model, response_id=rid,
                         finish="stop")


# ------------------------------------------------------------ OpenAI responses

def openai_response(*, model="gpt-5-mini-2026-08-07", rid="resp_sp1", text="Paris.",
                    inp=1500, out=400, cached=1280, cache_write=96, reasoning=320, function_call=None):
    output = []
    if reasoning:
        output.append({"id": "rs_sp1", "type": "reasoning", "summary": []})
    if function_call:
        output.append(dict({"type": "function_call", "id": "fc_sp1", "status": "completed"}, **function_call))
    else:
        output.append({"id": "msg_sp1", "type": "message", "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]})
    body = {
        "id": rid, "object": "response", "created_at": 1790000000, "status": "completed",
        "model": model, "output": output, "parallel_tool_calls": True, "tool_choice": "auto",
        "tools": [], "temperature": 1.0, "top_p": 1.0, "error": None, "incomplete_details": None,
        "instructions": None, "metadata": {}, "text": {"format": {"type": "text"}},
        "usage": {"input_tokens": inp, "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": cache_write},
                  "output_tokens": out, "output_tokens_details": {"reasoning_tokens": reasoning},
                  "total_tokens": inp + out},
    }
    return body, Truth(inp, out, cached=cached, cache_write=cache_write, reasoning=reasoning, model=model,
                       response_id=rid)


def openai_response_stream(**kw):
    body, truth = openai_response(**kw)
    inprog = dict(body, status="in_progress", output=[], usage=None)
    events = [{"type": "response.created", "sequence_number": 0, "response": inprog},
              {"type": "response.in_progress", "sequence_number": 1, "response": inprog}]
    seq = 2
    text = kw.get("text", "Paris.")
    for i, piece in enumerate(text.split(" ")):
        events.append({"type": "response.output_text.delta", "sequence_number": seq, "item_id": "msg_sp1",
                       "output_index": 0, "content_index": 0, "delta": piece + " ", "logprobs": []})
        seq += 1
    events.append({"type": "response.completed", "sequence_number": seq, "response": body})
    names = [e["type"] for e in events]
    return events, names, truth


# ------------------------------------------------------------------ Anthropic

def anthropic_message(*, model="claude-sonnet-5-5", rid="msg_sp1", text="Paris.", inp=40, out=120,
                      cache_read=2048, cache_creation=512, tool_use=None):
    content = [{"type": "text", "text": text}] if not tool_use else [tool_use]
    body = {
        "id": rid, "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": "tool_use" if tool_use else "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read,
                  "cache_creation_input_tokens": cache_creation, "service_tier": "standard"},
    }
    return body, Truth(inp + cache_read + cache_creation, out, cached=cache_read,
                       cache_write=cache_creation, model=model, response_id=rid,
                       finish="tool_use" if tool_use else "end_turn")


def anthropic_stream(*, model="claude-sonnet-5-5", rid="msg_sp2", text="The capital is Paris.", inp=40,
                     out=120, cache_read=2048, cache_creation=512):
    start_usage = {"input_tokens": inp, "output_tokens": 1, "cache_read_input_tokens": cache_read,
                   "cache_creation_input_tokens": cache_creation, "service_tier": "standard"}
    ev = [("message_start", {"type": "message_start", "message": {
        "id": rid, "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": start_usage}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}})]
    for w in text.split(" "):
        ev.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                           "delta": {"type": "text_delta", "text": w + " "}}))
    ev.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    ev.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                                    "stop_sequence": None},
                                 "usage": {"output_tokens": out}}))
    ev.append(("message_stop", {"type": "message_stop"}))
    names = [n for n, _ in ev]
    events = [e for _, e in ev]
    return events, names, Truth(inp + cache_read + cache_creation, out, cached=cache_read,
                                cache_write=cache_creation, model=model, response_id=rid, finish="end_turn")


# --------------------------------------------------------------- Google GenAI

def genai_response(*, model="gemini-3-flash", text="Paris.", prompt=900, cand=200, cached=512, thoughts=150,
                   function_call=None):
    part = {"text": text} if not function_call else {"functionCall": function_call}
    body = {
        "candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": cand,
                          "cachedContentTokenCount": cached, "thoughtsTokenCount": thoughts,
                          "totalTokenCount": prompt + cand + thoughts},
        "modelVersion": model, "responseId": "gen_sp1",
    }
    # Gemini: candidatesTokenCount excludes thoughts; Sentry output_tokens includes reasoning.
    return body, Truth(prompt, cand + thoughts, cached=cached, reasoning=thoughts, model=model,
                       response_id="gen_sp1", finish="STOP")


# ------------------------------------------------------------------ validation

def validate(kind: str, payload) -> None:
    """Parse a fixture with the provider SDK's own types. Raises on schema drift."""
    if kind == "openai_chat":
        from openai.types.chat import ChatCompletion
        ChatCompletion.model_validate(payload)
    elif kind == "openai_chat_chunk":
        from openai.types.chat import ChatCompletionChunk
        for e in payload:
            ChatCompletionChunk.model_validate(e)
    elif kind == "openai_response":
        from openai.types.responses import Response
        Response.model_validate(payload)
    elif kind == "openai_response_events":
        from openai.types.responses import ResponseStreamEvent
        from pydantic import TypeAdapter
        ta = TypeAdapter(ResponseStreamEvent)
        for e in payload:
            ta.validate_python(e)
    elif kind == "anthropic_message":
        from anthropic.types import Message
        Message.model_validate(payload)
    elif kind == "anthropic_events":
        from anthropic.types import RawMessageStreamEvent
        from pydantic import TypeAdapter
        ta = TypeAdapter(RawMessageStreamEvent)
        for e in payload:
            ta.validate_python(e)
    elif kind == "genai_response":
        from google.genai import types
        types.GenerateContentResponse.model_validate(payload)
    else:
        raise ValueError(kind)
