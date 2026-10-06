"""Wire-response builders for each provider, and the ground truth they imply.

Every builder returns (wire payload, Truth). The wire payload follows the provider's
published schema, and `validate()` parses it with the provider SDK's own response
types (tests/test_fixtures.py runs it on every builder), so a fixture that drifts
from the real schema fails loudly instead of producing a false finding.

Truth is expressed in Sentry's convention (sentry-conventions):
  input_tokens  includes cached and cache-write tokens
  output_tokens includes reasoning tokens
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


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
    # Billing dimensions beyond token counts (per-request tool fees, cache TTL split, audio tokens,
    # service tier). Keys are meanings from conventions.BILLING_KEYS; checked by check_billing.
    extras: dict = field(default_factory=dict)
    # What the model returned, for the output and identity checks: one string per choice, and the
    # tool calls the model asked for as [[name, arguments dict], ...]. None means not checked.
    text: list | None = None
    tool_calls: list | None = None

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
                   function_call=None, rid="gen_sp1"):
    part = {"text": text} if not function_call else {"functionCall": function_call}
    body = {
        "candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": cand,
                          "cachedContentTokenCount": cached, "thoughtsTokenCount": thoughts,
                          "totalTokenCount": prompt + cand + thoughts},
        "modelVersion": model, "responseId": rid,
    }
    # Gemini: candidatesTokenCount excludes thoughts; Sentry output_tokens includes reasoning.
    return body, Truth(prompt, cand + thoughts, cached=cached, reasoning=thoughts, model=model,
                       response_id=rid, finish="STOP")


def genai_stream(*, model="gemini-3-flash", rid="gen_sp2", pieces=("The ", "capital ", "is ", "Paris."),
                 prompt=900, per_chunk=50, cached=512, thoughts=150):
    """Gemini SSE stream: every chunk carries cumulative usageMetadata; the last one is final."""
    events = []
    for i, t in enumerate(pieces):
        last = i == len(pieces) - 1
        events.append({"candidates": [{"content": {"role": "model", "parts": [{"text": t}]},
                                       "finishReason": "STOP" if last else None, "index": 0}],
                       "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": per_chunk * (i + 1),
                                         "cachedContentTokenCount": cached, "thoughtsTokenCount": thoughts,
                                         "totalTokenCount": prompt + per_chunk * (i + 1) + thoughts},
                       "modelVersion": model, "responseId": rid})
    cand = per_chunk * len(pieces)
    return events, Truth(prompt, cand + thoughts, cached=cached, reasoning=thoughts, model=model, response_id=rid,
                         finish="STOP")


# --------------------------------------------------------------------- Cohere

def cohere_chat(*, gid="c0h-gen-sp1", text="Paris.", billed_in=40, billed_out=12, tokens_in=230, tokens_out=12,
                cached=None, finish="COMPLETE", tool_calls=None):
    meta = {"api_version": {"version": "1"}, "billed_units": {"input_tokens": billed_in, "output_tokens": billed_out},
            "tokens": {"input_tokens": tokens_in, "output_tokens": tokens_out}}
    if cached is not None:
        meta["cached_tokens"] = cached
    body = {"response_id": "c0h-resp-sp1", "text": "" if tool_calls else text, "generation_id": gid,
            "chat_history": [{"role": "USER", "message": "Capital of France?"},
                             {"role": "CHATBOT", "message": "" if tool_calls else text}],
            "finish_reason": finish, "meta": meta}
    if tool_calls:
        body["tool_calls"] = tool_calls
    # Cohere bills billed_units; tokens also counts the prompt template it adds around the user's input.
    return body, Truth(billed_in, billed_out, cached=cached or 0, response_id=gid, finish=finish,
                       tool_calls=[[c["name"], c["parameters"]] for c in tool_calls or []])


def cohere_chat_stream(*, gid="c0h-gen-sp2", text="The capital is Paris.", **kw):
    body, truth = cohere_chat(gid=gid, text=text, **kw)
    events = [{"event_type": "stream-start", "generation_id": gid}]
    events += [{"event_type": "text-generation", "text": w + " "} for w in text.split(" ")]
    events.append({"event_type": "stream-end", "finish_reason": body["finish_reason"], "response": body})
    return events, truth


def cohere_embed(*, eid="c0h-emb-sp1", texts=("Paris",), billed_in=7):
    body = {"id": eid, "response_type": "embeddings_floats", "texts": list(texts),
            "embeddings": [[0.01, -0.02, 0.03] for _ in texts],
            "meta": {"api_version": {"version": "1"}, "billed_units": {"input_tokens": billed_in}}}
    return body, Truth(billed_in, 0, response_id=eid)


def cohere_chat_v2(*, rid="c0h-v2-sp1", text="Paris.", inp=40, out=12):
    body = {"id": rid, "finish_reason": "COMPLETE",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            "usage": {"billed_units": {"input_tokens": inp, "output_tokens": out},
                      "tokens": {"input_tokens": inp + 190, "output_tokens": out}}}
    return body, Truth(inp, out, response_id=rid, finish="COMPLETE")


# -------------------------------------------------------------------- Mistral

def _calls(tool_calls):
    out = []
    for c in tool_calls or []:
        args = c["function"]["arguments"]
        out.append([c["function"]["name"], json.loads(args) if isinstance(args, str) else args])
    return out


def mistral_chat(*, model="mistral-large-2511", rid="mst-sp1", content="Paris.", prompt=48, completion=9,
                 tool_calls=None):
    msg = {"role": "assistant", "content": "" if tool_calls else content, "prefix": False}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    body = {"id": rid, "object": "chat.completion", "model": model, "created": 1790000000,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}
    return body, Truth(prompt, completion, model=model, response_id=rid, finish=finish, tool_calls=_calls(tool_calls))


def mistral_chat_stream(*, model="mistral-large-2511", rid="mst-sp2", text="The capital is Paris.", prompt=48,
                        completion=9):
    base = {"id": rid, "object": "chat.completion.chunk", "created": 1790000000, "model": model}
    events = [dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}])]
    events += [dict(base, choices=[{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}])
               for w in text.split(" ")]
    events.append(dict(base, choices=[{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
                       usage={"prompt_tokens": prompt, "completion_tokens": completion,
                              "total_tokens": prompt + completion}))
    return events, Truth(prompt, completion, model=model, response_id=rid, finish="stop")


def mistral_embed(*, model="mistral-embed", rid="mst-emb-sp1", prompt=6):
    body = {"id": rid, "object": "list", "model": model,
            "data": [{"object": "embedding", "embedding": [0.01, -0.02, 0.03], "index": 0}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": 0, "total_tokens": prompt}}
    return body, Truth(prompt, 0, model=model, response_id=rid)


# ------------------------------------------------------- Hugging Face (TGI / router)

def hf_chat(*, model="meta-llama/Llama-3.3-70B-Instruct", rid="hf-sp1", content="Paris.", prompt=52, completion=8,
            tool_calls=None):
    msg = {"role": "assistant", "content": None if tool_calls else content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    body = {"id": rid, "created": 1790000000, "model": model, "system_fingerprint": "3.3.6-sha-spanproof",
            "choices": [{"index": 0, "message": msg, "logprobs": None, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}
    return body, Truth(prompt, completion, model=model, response_id=rid, finish=finish, tool_calls=_calls(tool_calls))


def hf_chat_stream(*, model="meta-llama/Llama-3.3-70B-Instruct", rid="hf-sp2", text="The capital is Paris.",
                   prompt=52, completion=8, tool_args=None):
    base = {"id": rid, "created": 1790000000, "model": model, "system_fingerprint": "3.3.6-sha-spanproof"}
    if tool_args:  # one tool call whose arguments arrive in pieces, as TGI and the HF router stream them
        events = [dict(base, choices=[{"index": 0, "logprobs": None, "finish_reason": None, "delta": {
            "role": "assistant", "tool_calls": [{"index": 0, "id": "call_hf1", "type": "function",
                                                 "function": {"name": "get_weather", "arguments": ""}}]}}])]
        events += [dict(base, choices=[{"index": 0, "logprobs": None, "finish_reason": None, "delta": {
            "role": "assistant", "tool_calls": [{"index": 0, "id": "call_hf1", "type": "function",
                                                 "function": {"arguments": a}}]}}]) for a in tool_args]
        finish = "tool_calls"
    else:
        events = [dict(base, choices=[{"index": 0, "logprobs": None, "finish_reason": None,
                                       "delta": {"role": "assistant", "content": w + " "}}]) for w in text.split(" ")]
        finish = "stop"
    events.append(dict(base, choices=[{"index": 0, "logprobs": None, "finish_reason": finish,
                                       "delta": {"role": "assistant", "content": ""}}]))
    events.append(dict(base, choices=[], usage={"prompt_tokens": prompt, "completion_tokens": completion,
                                                "total_tokens": prompt + completion}))
    calls = [["get_weather", json.loads("".join(tool_args))]] if tool_args else []
    return events, Truth(prompt, completion, model=model, response_id=rid, finish=finish, tool_calls=calls)


def hf_text_generation(*, text="Paris.", prompt_ids=(1, 3923, 374, 279, 6864, 315, 9822, 30), generated=3,
                       finish="eos_token"):
    # decoder_input_details=True: TGI returns the prompt tokens (prefill), so the input count is known
    prefill = [{"id": i, "text": "t", "logprob": None if n == 0 else -0.5} for n, i in enumerate(prompt_ids)]
    tokens = [{"id": 100 + n, "text": "t", "logprob": -0.1, "special": n == generated - 1} for n in range(generated)]
    body = [{"generated_text": text, "details": {"finish_reason": finish, "generated_tokens": generated,
                                                 "seed": None, "prefill": prefill, "tokens": tokens}}]
    return body, Truth(len(prompt_ids), generated, finish=finish)


def hf_text_generation_stream(*, text="The capital is Paris.", input_length=8, generated=5, finish="length"):
    words = text.split(" ")
    events = []
    for n, w in enumerate(words):
        last = n == len(words) - 1
        events.append({"index": n + 1, "token": {"id": 100 + n, "text": w + " ", "logprob": -0.1, "special": False},
                       "generated_text": text if last else None,
                       "details": {"finish_reason": finish, "generated_tokens": generated,
                                   "input_length": input_length, "seed": None} if last else None})
    return events, Truth(input_length, generated, finish=finish)


# ------------------------------------------------- advanced paths (anthropic)
#
# Built from the published Messages API schema (extended thinking, server tools, cache TTLs,
# stop reasons) and checked against anthropic 1.11.0's types with validate(..., strict=True).

def anthropic_message_x(*, content, model="claude-sonnet-5-5", rid="msg_spx1", inp=40, out=120, cache_read=0,
                        cache_creation=0, stop_reason="end_turn", usage_extra=None, reasoning=0, extras=None):
    """A Message with explicit content blocks and extra usage fields (thinking, server tools, TTL split)."""
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read,
             "cache_creation_input_tokens": cache_creation, "service_tier": "standard"}
    usage.update(usage_extra or {})
    body = {"id": rid, "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason, "stop_sequence": None, "usage": usage}
    tools = [[b["name"], b.get("input") or {}] for b in content if b["type"] == "tool_use"]
    texts = [b["text"] for b in content if b["type"] == "text"]
    return body, Truth(inp + cache_read + cache_creation, out, cached=cache_read, cache_write=cache_creation,
                       reasoning=reasoning, model=model, response_id=rid, finish=stop_reason,
                       extras=dict(extras or {}), text=texts if len(texts) == 1 else None, tool_calls=tools or None)


def anthropic_stream_x(*, blocks, model="claude-sonnet-5-5", rid="msg_spx2", inp=40, out=120, cache_read=0,
                       cache_creation=0, stop_reason="end_turn", delta_usage_extra=None, reasoning=0,
                       extras=None, text=None):
    """An SSE stream from (content_block_start payload, [delta payloads]) pairs.

    message_delta carries the cumulative usage the API sends at the end of a stream, including any
    fields only known then (thinking tokens, server tool requests).
    """
    start_usage = {"input_tokens": inp, "output_tokens": 1, "cache_read_input_tokens": cache_read,
                   "cache_creation_input_tokens": cache_creation, "service_tier": "standard"}
    ev = [("message_start", {"type": "message_start", "message": {
        "id": rid, "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": start_usage}})]
    tools = []
    for i, (start, deltas) in enumerate(blocks):
        if start["type"] == "tool_use":
            raw = "".join(d.get("partial_json", "") for d in deltas)
            tools.append([start["name"], json.loads(raw) if raw else (start.get("input") or {})])
        ev.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}))
        for d in deltas:
            ev.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": d}))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read,
             "cache_creation_input_tokens": cache_creation}
    usage.update(delta_usage_extra or {})
    ev.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason,
                                                                    "stop_sequence": None}, "usage": usage}))
    ev.append(("message_stop", {"type": "message_stop"}))
    return [e for _, e in ev], [n for n, _ in ev], Truth(
        inp + cache_read + cache_creation, out, cached=cache_read, cache_write=cache_creation, reasoning=reasoning,
        model=model, response_id=rid, finish=stop_reason, extras=dict(extras or {}), text=text,
        tool_calls=tools or None)


# ---------------------------------------------------- advanced paths (openai)

def openai_chat_x(*, model="gpt-5-mini-2026-08-07", rid="chatcmpl-spx1", texts=("Paris.",), prompt=1200,
                  completion=300, cached=0, reasoning=0, audio_in=0, audio_out=0, accepted=0, rejected=0,
                  service_tier="default", finish="stop", extras=None, message_extra=None):
    """Chat Completion with n choices and the full usage breakdown (audio, predictions)."""
    choices = []
    for i, t in enumerate(texts):
        msg = {"role": "assistant", "content": t, "refusal": None}
        msg.update(message_extra or {})
        choices.append({"index": i, "message": msg, "logprobs": None, "finish_reason": finish})
    body = {
        "id": rid, "object": "chat.completion", "created": 1790000000, "model": model, "choices": choices,
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
                  "prompt_tokens_details": {"cached_tokens": cached, "audio_tokens": audio_in},
                  "completion_tokens_details": {"reasoning_tokens": reasoning, "audio_tokens": audio_out,
                                                "accepted_prediction_tokens": accepted,
                                                "rejected_prediction_tokens": rejected}},
        "service_tier": service_tier, "system_fingerprint": "fp_spanproof",
    }
    return body, Truth(prompt, completion, cached=cached, reasoning=reasoning, model=model, response_id=rid,
                       finish=finish, extras=dict(extras or {}))


def openai_chat_stream_n(*, model="gpt-5-mini-2026-08-07", rid="chatcmpl-spx2", texts=("Paris is it.", "It is Paris."),
                         prompt=1200, completion=300):
    """n>1 stream as the API sends it: every chunk carries ONE choice, identified by its index."""
    base = {"id": rid, "object": "chat.completion.chunk", "created": 1790000000, "model": model,
            "system_fingerprint": "fp_spanproof", "service_tier": "default"}
    events = []
    words = [t.split(" ") for t in texts]
    for i in range(len(texts)):
        events.append(dict(base, choices=[{"index": i, "delta": {"role": "assistant", "content": ""},
                                           "logprobs": None, "finish_reason": None}], usage=None))
    for k in range(max(len(w) for w in words)):  # interleaved, as with real n>1 streams
        for i, w in enumerate(words):
            if k < len(w):
                piece = w[k] + (" " if k < len(w) - 1 else "")
                events.append(dict(base, choices=[{"index": i, "delta": {"content": piece}, "logprobs": None,
                                                   "finish_reason": None}], usage=None))
    for i in range(len(texts)):
        events.append(dict(base, choices=[{"index": i, "delta": {}, "logprobs": None, "finish_reason": "stop"}],
                           usage=None))
    events.append(dict(base, choices=[], usage={
        "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0, "accepted_prediction_tokens": 0,
                                      "rejected_prediction_tokens": 0}}))
    return events, Truth(prompt, completion, model=model, response_id=rid, finish="stop", text=list(texts))


def openai_response_x(*, model="gpt-5-mini-2026-08-07", rid="resp_spx1", text="Paris.", inp=1500, out=400,
                      cached=0, cache_write=0, reasoning=320, tool_items=(), status="completed",
                      incomplete_reason=None, service_tier=None, extras=None, usage=True, background=None):
    """A Response with built-in tool call items (billed per call) and an optional terminal status."""
    output = []
    if reasoning:
        output.append({"id": "rs_spx1", "type": "reasoning", "summary": []})
    output.extend(tool_items)
    if text:
        output.append({"id": "msg_spx1", "type": "message", "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]})
    body = {
        "id": rid, "object": "response", "created_at": 1790000000, "status": status,
        "model": model, "output": output, "parallel_tool_calls": True, "tool_choice": "auto",
        "tools": [], "temperature": 1.0, "top_p": 1.0, "error": None,
        "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
        "instructions": None, "metadata": {}, "text": {"format": {"type": "text"}},
        "usage": {"input_tokens": inp, "input_tokens_details": {"cached_tokens": cached,
                                                                "cache_write_tokens": cache_write},
                  "output_tokens": out, "output_tokens_details": {"reasoning_tokens": reasoning},
                  "total_tokens": inp + out} if usage else None,
    }
    if service_tier:
        body["service_tier"] = service_tier
    if background is not None:
        body["background"] = background
    return body, Truth(inp, out, cached=cached, cache_write=cache_write, reasoning=reasoning, model=model,
                       response_id=rid, extras=dict(extras or {}))


def openai_response_stream_x(*, terminal="response.completed", **kw):
    """Responses stream that ends with `terminal` (response.completed, response.incomplete, response.failed).

    The terminal event carries the full Response, usage included: an incomplete or failed response
    is still billed for every token it used.
    """
    body, truth = openai_response_x(**kw)
    inprog = dict(body, status="in_progress", output=[], usage=None, incomplete_details=None)
    events = [{"type": "response.created", "response": inprog},
              {"type": "response.in_progress", "response": inprog}]
    # every output item as the API streams it: added, (message content parts and text deltas), done
    for idx, item in enumerate(body["output"]):
        if item["type"] != "message":
            events.append({"type": "response.output_item.added", "output_index": idx, "item": item})
            events.append({"type": "response.output_item.done", "output_index": idx, "item": item})
            continue
        text = item["content"][0]["text"]
        events.append({"type": "response.output_item.added", "output_index": idx,
                       "item": dict(item, status="in_progress", content=[])})
        loc = {"item_id": item["id"], "output_index": idx, "content_index": 0}
        events.append(dict(loc, type="response.content_part.added",
                           part={"type": "output_text", "text": "", "annotations": []}))
        words = text.split(" ")
        for i, w in enumerate(words):
            events.append(dict(loc, type="response.output_text.delta", delta=w + (" " if i < len(words) - 1 else ""),
                               logprobs=[]))
        events.append(dict(loc, type="response.output_text.done", text=text, logprobs=[]))
        events.append(dict(loc, type="response.content_part.done", part=item["content"][0]))
        events.append({"type": "response.output_item.done", "output_index": idx, "item": item})
    events.append({"type": terminal, "response": body})
    for seq, e in enumerate(events):
        e["sequence_number"] = seq
    return events, [e["type"] for e in events], truth


def openai_embedding(*, model="text-embedding-3-large", dims=256, n=2, prompt=24):
    body = {"object": "list", "model": model,
            "data": [{"object": "embedding", "index": i, "embedding": [0.01] * dims} for i in range(n)],
            "usage": {"prompt_tokens": prompt, "total_tokens": prompt}}
    return body, Truth(prompt, 0, model=model, extras={"embeddings.dimensions": dims})


# ------------------------------------------------------------------ validation

def _no_extra(m, path="") -> None:
    """Pydantic models that allow extra keys (Cohere, Mistral) would accept a misspelt field silently."""
    from pydantic import BaseModel

    if isinstance(m, BaseModel):
        if m.model_extra:
            raise ValueError(f"{type(m).__name__}{path}: keys not in the SDK schema: {sorted(m.model_extra)}")
        for k in type(m).model_fields:
            _no_extra(getattr(m, k), f"{path}.{k}")
    elif isinstance(m, (list, tuple)):
        for i, x in enumerate(m):
            _no_extra(x, f"{path}[{i}]")


def _hf_strict(cls, data, path="") -> None:
    """huggingface_hub's inference types accept any dict; check keys and required fields against them."""
    import dataclasses
    import typing

    from huggingface_hub.inference._generated.types.base import BaseInferenceType

    def classes(t):
        if isinstance(t, type) and issubclass(t, BaseInferenceType):
            yield t
        for a in typing.get_args(t):
            yield from classes(a)

    hints = typing.get_type_hints(cls)
    fields = {f.name for f in dataclasses.fields(cls)}
    extra = set(data) - fields
    if extra:
        raise ValueError(f"{cls.__name__}{path}: keys not in the SDK schema: {sorted(extra)}")
    for name in fields:
        t = hints[name]
        if name not in data and type(None) not in typing.get_args(t):
            raise ValueError(f"{cls.__name__}{path}: required field {name} missing")
        sub = next(classes(t), None)
        v = data.get(name)
        if sub and isinstance(v, dict):
            _hf_strict(sub, v, f"{path}.{name}")
        elif sub and isinstance(v, list):
            for i, x in enumerate(v):
                if isinstance(x, dict):
                    _hf_strict(sub, x, f"{path}.{name}[{i}]")


def _extras(m, path="") -> list[str]:
    """Paths of fields the SDK type does not declare (pydantic `extra`), recursively."""
    from pydantic import BaseModel

    out = []
    if isinstance(m, BaseModel):
        for k in (getattr(m, "__pydantic_extra__", None) or {}):
            out.append(f"{path}.{k}")
        for k in type(m).model_fields:
            out.extend(_extras(getattr(m, k, None), f"{path}.{k}"))
    elif isinstance(m, (list, tuple)):
        for i, x in enumerate(m):
            out.extend(_extras(x, f"{path}[{i}]"))
    return out


def validate(kind: str, payload, strict: bool = False) -> None:
    """Parse a fixture with the provider SDK's own types. Raises on schema drift.

    strict=True also rejects any field the SDK type does not declare, so a misspelled usage key
    (which the lenient provider models would silently keep as an extra) fails too.
    """
    models = []
    if kind == "openai_embedding":
        from openai.types import CreateEmbeddingResponse
        models.append(CreateEmbeddingResponse.model_validate(payload))
    elif kind == "anthropic_events" and strict:
        from anthropic.types import RawMessageStreamEvent
        from pydantic import TypeAdapter
        ta = TypeAdapter(RawMessageStreamEvent)
        models.extend(ta.validate_python(e) for e in payload)
    elif kind == "openai_response_events" and strict:
        from openai.types.responses import ResponseStreamEvent
        from pydantic import TypeAdapter
        ta = TypeAdapter(ResponseStreamEvent)
        models.extend(ta.validate_python(e) for e in payload)
    elif kind == "openai_chat_chunk" and strict:
        from openai.types.chat import ChatCompletionChunk
        models.extend(ChatCompletionChunk.model_validate(e) for e in payload)
    elif kind == "openai_chat" and strict:
        from openai.types.chat import ChatCompletion
        models.append(ChatCompletion.model_validate(payload))
    elif kind == "openai_response" and strict:
        from openai.types.responses import Response
        models.append(Response.model_validate(payload))
    elif kind == "anthropic_message" and strict:
        from anthropic.types import Message
        models.append(Message.model_validate(payload))
    else:
        _validate_lenient(kind, payload)
    if strict:
        bad = [p for m in models for p in _extras(m)]
        if bad:
            raise ValueError(f"{kind}: fields the SDK does not declare: {bad}")


def _validate_lenient(kind: str, payload) -> None:
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
    elif kind == "cohere_chat":
        from cohere import NonStreamedChatResponse
        _no_extra(NonStreamedChatResponse.model_validate(payload))
    elif kind == "cohere_chat_events":
        from cohere import StreamedChatResponse
        from pydantic import TypeAdapter
        ta = TypeAdapter(StreamedChatResponse)
        for e in payload:
            _no_extra(ta.validate_python(e))
    elif kind == "cohere_embed":
        from cohere import EmbedResponse
        from pydantic import TypeAdapter
        _no_extra(TypeAdapter(EmbedResponse).validate_python(payload))
    elif kind == "cohere_chat_v2":
        from cohere import V2ChatResponse
        _no_extra(V2ChatResponse.model_validate(payload))
    elif kind == "mistral_chat":
        from mistralai.client.models import ChatCompletionResponse
        _no_extra(ChatCompletionResponse.model_validate(payload))
    elif kind == "mistral_chat_chunk":
        from mistralai.client.models import CompletionChunk
        for e in payload:
            _no_extra(CompletionChunk.model_validate(e))
    elif kind == "mistral_embed":
        from mistralai.client.models import EmbeddingResponse
        _no_extra(EmbeddingResponse.model_validate(payload))
    elif kind == "hf_chat":
        from huggingface_hub import ChatCompletionOutput
        _hf_strict(ChatCompletionOutput, payload)
    elif kind == "hf_chat_chunk":
        from huggingface_hub import ChatCompletionStreamOutput
        for e in payload:
            _hf_strict(ChatCompletionStreamOutput, e)
    elif kind == "hf_text_generation":
        from huggingface_hub import TextGenerationOutput
        for e in payload:
            _hf_strict(TextGenerationOutput, e)
    elif kind == "hf_text_generation_chunk":
        from huggingface_hub import TextGenerationStreamOutput
        for e in payload:
            _hf_strict(TextGenerationStreamOutput, e)
    else:
        raise ValueError(kind)
