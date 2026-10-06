"""Ground truth from live provider responses recorded by the proxy (OpenAI-compatible APIs).

Usage is read the way a correct consumer must: the provider's own final usage report.
Some providers repeat the usage block on more than one stream chunk (Groq sends it on
the last content chunk and again on a usage-only chunk); it is a running total, so the
truth is the last report, never a sum.
"""

from __future__ import annotations

import json

from .fixtures import Truth


def _usage_truth(u: dict, model: str, rid: str, finish: str) -> Truth:
    pd = u.get("prompt_tokens_details") or {}
    cd = u.get("completion_tokens_details") or {}
    return Truth(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0),
                 cached=int(pd.get("cached_tokens") or 0), cache_write=int(pd.get("cache_write_tokens") or 0),
                 reasoning=int(cd.get("reasoning_tokens") or 0), model=model or "", response_id=rid or "",
                 finish=finish or "")


def truth_from_recording(rec: dict) -> Truth | None:
    if rec["status"] >= 400 or "chat/completions" not in rec["path"]:
        return None
    raw = rec["raw"]
    if "text/event-stream" in rec["content_type"] or raw.lstrip().startswith("data:"):
        usage, model, rid, finish = None, "", "", ""
        for line in raw.splitlines():
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            try:
                c = json.loads(line[6:])
            except ValueError:
                continue
            model = c.get("model") or model
            rid = c.get("id") or rid
            for ch in c.get("choices") or []:
                finish = ch.get("finish_reason") or finish
            u = c.get("usage") or (c.get("x_groq") or {}).get("usage")
            if u:
                usage = u  # running total: keep the last one
        return _usage_truth(usage, model, rid, finish) if usage else None
    try:
        b = json.loads(raw)
    except ValueError:
        return None
    if not b.get("usage"):
        return None
    finish = ((b.get("choices") or [{}])[0] or {}).get("finish_reason") or ""
    return _usage_truth(b["usage"], b.get("model"), b.get("id"), finish)
