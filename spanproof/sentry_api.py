"""Minimal read-only client for a Sentry organization (credentials from ~/.spanproof.env)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request


def env() -> dict:
    out = {}
    for line in open(os.path.expanduser("~/.spanproof.env")):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v.strip().strip("'\"")
    return out


def get(path: str, params: list[tuple[str, str]] | None = None) -> object:
    e = env()
    url = f"{e['SENTRY_REGION_URL']}/api/0/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {e['SENTRY_AUTH_TOKEN']}"})
    import http.client
    import time

    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError) as err:
            code = getattr(err, "code", None)
            if code is not None and code < 500 and code != 429:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Sentry API unreachable after retries: {path}")


def spans(query: str, fields: list[str], project: str | None = None, per_page: int = 100,
          stats_period: str = "24h") -> list[dict]:
    e = env()
    params = [("dataset", "spans"), ("query", query), ("statsPeriod", stats_period), ("per_page", str(per_page))]
    params += [("field", f) for f in fields]
    if project:
        params.append(("project", project))
    return get(f"organizations/{e['SENTRY_ORG']}/events/", params).get("data", [])
