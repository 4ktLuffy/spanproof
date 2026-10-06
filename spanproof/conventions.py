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

# Attributes that carry prompt or completion content. With data collection off none of
# them may appear; with it on, the input side should.
CONTENT_KEYS = {
    "gen_ai.input.messages", "gen_ai.request.messages", "gen_ai.prompt", "gen_ai.output.messages",
    "gen_ai.response.text", "gen_ai.response.tool_calls", "gen_ai.system_instructions",
    "gen_ai.system.message", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result", "gen_ai.tool.input",
    "gen_ai.tool.output", "gen_ai.tool.message", "gen_ai.embeddings.input",
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
