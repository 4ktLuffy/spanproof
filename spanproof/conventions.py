"""The sentry-conventions gen_ai registry, as a lookup table.

A pinned snapshot ships with the package (conventions_snapshot/gen_ai.json, with its
source SHA). Point SPANPROOF_CONVENTIONS at a checkout's model/attributes directory
to check against a newer registry.
"""

from __future__ import annotations

import glob
import json
import os
from functools import lru_cache
from pathlib import Path

SNAPSHOT = Path(__file__).parent / "conventions_snapshot" / "gen_ai.json"
# MCP server spans: the mcp.* registry plus the generic keys it points to as replacements.
MCP_SNAPSHOT = Path(__file__).parent / "conventions_snapshot" / "mcp.json"
MCP_KEYS = ("mcp.", "jsonrpc.", "network.transport", "network.protocol.name", "error.type", "gen_ai.tool.",
            "gen_ai.prompt.name", "gen_ai.operation.name")

# What each usage meaning is called. The canonical (non-deprecated) key comes first;
# the rest are the aliases the registry accepts. tests/test_conventions.py checks this
# table against the registry so it cannot silently drift.
USAGE_KEYS = {
    "input_tokens": ["gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"],
    "output_tokens": ["gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"],
    "total": ["gen_ai.usage.total_tokens"],
    "cached": ["gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.input_tokens.cached",
               "gen_ai.usage.cache_read_input_tokens"],
    "cache_write": ["gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.input_tokens.cache_write",
                    "gen_ai.usage.cache_creation_input_tokens"],
    "reasoning": ["gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.output_tokens.reasoning"],
}

# Billing dimensions that change what a call costs but are not token totals: per-request tool
# fees, cache writes priced by TTL, audio tokens priced apart from text, and the service tier.
# meaning -> (attribute names that would count as recording it, substrings that also count).
# The names include OpenTelemetry GenAI ones where they exist; the substrings keep the check from
# firing on an SDK that records the value under a name nobody standardised yet. Whether the
# sentry-conventions registry has ANY of the names is reported with the finding (registry_has()).
BILLING_KEYS = {
    "server_tool.web_search_requests": (["gen_ai.usage.web_search_requests",
                                         "gen_ai.usage.server_tool_use.web_search_requests"], ["web_search"]),
    "server_tool.web_fetch_requests": (["gen_ai.usage.web_fetch_requests",
                                        "gen_ai.usage.server_tool_use.web_fetch_requests"], ["web_fetch"]),
    "cache_write.ephemeral_5m": (["gen_ai.usage.cache_creation.ephemeral_5m.input_tokens"], ["5m"]),
    "cache_write.ephemeral_1h": (["gen_ai.usage.cache_creation.ephemeral_1h.input_tokens"], ["1h"]),
    "audio.input_tokens": (["gen_ai.usage.input_tokens.audio", "gen_ai.usage.audio.input_tokens"],
                           ["input_tokens.audio", "audio.input", "audio_input"]),
    "audio.output_tokens": (["gen_ai.usage.output_tokens.audio", "gen_ai.usage.audio.output_tokens"],
                            ["output_tokens.audio", "audio.output", "audio_output"]),
    "tool_calls.web_search_call": (["gen_ai.usage.web_search_requests"], ["web_search"]),
    "tool_calls.file_search_call": (["gen_ai.usage.file_search_requests"], ["file_search"]),
    "tool_calls.code_interpreter_call": (["gen_ai.usage.code_interpreter_requests"], ["code_interpreter"]),
    "tool_calls.image_generation_call": (["gen_ai.usage.image_generation_requests"], ["image_generation"]),
    "service_tier": (["gen_ai.openai.response.service_tier", "openai.response.service_tier",
                      "gen_ai.response.service_tier", "gen_ai.anthropic.service_tier"], ["service_tier"]),
    "embeddings.dimensions": (["gen_ai.embeddings.dimension.count", "gen_ai.request.dimensions"], ["dimension"]),
}


def billing_recorded(data: dict, meaning: str) -> str | None:
    """The span attribute that records a billing dimension, or None."""
    names, subs = BILLING_KEYS[meaning]
    for k in names:
        if k in data:
            return k
    for k in data:
        if k.startswith("gen_ai.") and any(s in k for s in subs):
            return k
    return None


def registry_has(meaning: str) -> list[str]:
    """Registry attributes that could carry this billing dimension (empty: a sentry-conventions gap)."""
    names, subs = BILLING_KEYS[meaning]
    return sorted(k for k in attrs() if k in names or (k.startswith("gen_ai.") and any(s in k for s in subs)))


# Attributes that carry prompt or completion content. With data collection off none of
# them may appear; with it on, the input side should.
CONTENT_KEYS = {
    "gen_ai.input.messages", "gen_ai.request.messages", "gen_ai.prompt", "gen_ai.output.messages",
    "gen_ai.response.text", "gen_ai.response.tool_calls", "gen_ai.system_instructions",
    "gen_ai.system.message", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result", "gen_ai.tool.input",
    "gen_ai.tool.output", "gen_ai.tool.message", "gen_ai.embeddings.input",
    # pre-gen_ai names still sent by some integrations (Cohere)
    "ai.input_messages", "ai.texts", "ai.responses", "ai.tool_calls", "ai.tools", "ai.preamble", "ai.citations",
    "ai.documents", "ai.search_queries", "ai.search_results",
}


@lru_cache(maxsize=1)
def registry() -> dict:
    src = os.environ.get("SPANPROOF_CONVENTIONS")
    if src:
        out = {}
        for f in glob.glob(os.path.join(src, "**", "*.json"), recursive=True):
            d = json.load(open(f))
            if d.get("key", "").startswith(("gen_ai", "ai.")):
                out[d["key"]] = d
        return {"source": src, "sha": "local", "attributes": out}
    return json.load(open(SNAPSHOT))


def attrs() -> dict:
    return registry()["attributes"]


def lookup(key: str) -> dict | None:
    return attrs().get(key)


def deprecation(key: str) -> dict | None:
    d = lookup(key)
    dep = (d or {}).get("deprecation")
    if dep and dep.get("_status") in ("backfill", "normalize", "transform", None):
        return dep
    return None


def type_ok(key: str, value) -> bool | None:
    d = lookup(key)
    if not d:
        return None
    t = d.get("type")
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "double":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "string[]":
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    return None


def read_usage(data: dict) -> dict:
    """Map a span's data to {meaning: (value, key_used)} using canonical keys or aliases."""
    out = {}
    for meaning, keys in USAGE_KEYS.items():
        for k in keys:
            if k in data:
                out[meaning] = (data[k], k)
                break
    return out


def _load(src: str, keep) -> dict:
    out = {}
    for f in glob.glob(os.path.join(src, "**", "*.json"), recursive=True):
        d = json.load(open(f))
        if keep(d.get("key", "")):
            out[d["key"]] = d
    return out


def snapshot_mcp(src: str, sha: str, out: Path = MCP_SNAPSHOT) -> None:
    """Write conventions_snapshot/mcp.json from a sentry-conventions model/attributes directory."""
    a = _load(src, lambda k: k.startswith(MCP_KEYS))
    json.dump({"source": "getsentry/sentry-conventions model/attributes", "sha": sha,
               "attributes": dict(sorted(a.items()))}, open(out, "w"), indent=1)


@lru_cache(maxsize=1)
def mcp_registry() -> dict:
    src = os.environ.get("SPANPROOF_CONVENTIONS")
    if src:
        return {"source": src, "sha": "local", "attributes": _load(src, lambda k: k.startswith(MCP_KEYS))}
    return json.load(open(MCP_SNAPSHOT))


def mcp_lookup(key: str) -> dict | None:
    """Registry entry for an attribute on an MCP span; dynamic-suffix keys (mcp.request.argument.<key>) match."""
    a = mcp_registry()["attributes"]
    if key in a:
        return a[key]
    for k, d in a.items():
        if d.get("has_dynamic_suffix") and key.startswith(k.split("<")[0]):
            return d
    return None


def value(data: dict, *keys: str):
    """First of `keys` present in data, else a deprecated alias the registry backfills into one of them."""
    for k in keys:
        if k in data:
            return data[k]
    for k, d in attrs().items():
        dep = d.get("deprecation") or {}
        if k in data and dep.get("replacement") in keys and dep.get("_status") == "backfill":
            return data[k]
    return None
