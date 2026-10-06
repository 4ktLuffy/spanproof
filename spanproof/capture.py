"""Initialise sentry_sdk with an in-memory transport and turn envelopes into a flat span list."""

from __future__ import annotations

import json
from typing import Any

import sentry_sdk
from sentry_sdk.transport import Transport


class MemoryTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.items: list[tuple[str, Any]] = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            t = item.headers.get("type")
            payload = item.payload.json
            if payload is None and item.payload.bytes is not None:
                try:  # byte-backed items carry the same JSON
                    payload = json.loads(item.payload.bytes)
                except ValueError:
                    payload = {"_undecodable": True}
            self.items.append((t, payload))


class ForwardingTransport(MemoryTransport):
    """Records every envelope like MemoryTransport AND sends it to a real Sentry project.

    Used when SPANPROOF_DSN is set, so a run can be compared at three points: what the SDK
    sent (recorded here), what Sentry stored (read back through the API), and the truth.
    """

    def __init__(self, options=None):
        super().__init__(options)
        from sentry_sdk.transport import HttpTransport

        self.inner = HttpTransport(options)

    def capture_envelope(self, envelope):
        super().capture_envelope(envelope)
        self.inner.capture_envelope(envelope)

    def flush(self, timeout, callback=None):
        self.inner.flush(timeout, callback)

    def kill(self):
        self.inner.kill()


def init(integrations: list, *, data_collection: bool = True, extra: dict | None = None) -> MemoryTransport:
    import os

    real_dsn = os.environ.get("SPANPROOF_DSN")
    transport = MemoryTransport()
    opts = dict(
        dsn="http://spanproof@127.0.0.1:9/1",  # never contacted: transport is in-memory
        transport=transport,
        traces_sample_rate=1.0,
        send_default_pii=data_collection,
        integrations=integrations,
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    opts.update(extra or {})
    if real_dsn:
        opts["dsn"] = real_dsn
        opts["environment"] = os.environ.get("SPANPROOF_ENV", "spanproof")
        opts.pop("transport")
        sentry_sdk.init(**opts)
        transport = ForwardingTransport(sentry_sdk.get_client().options)
        sentry_sdk.get_client().transport = transport
        run = os.environ.get("SPANPROOF_RUN")
        if run:
            sentry_sdk.set_tag("spanproof.run", run)
        return transport
    sentry_sdk.init(**opts)
    return transport


def _ts(v):
    """Timestamps arrive as epoch floats or ISO-8601 strings; return epoch seconds."""
    if v is None or isinstance(v, (int, float)):
        return v
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def flatten(transport: MemoryTransport) -> dict:
    """Return {"spans": [...], "errors": [...], "transactions": n}.

    Every span carries trace_id, span_id, parent_span_id, op, description, data,
    and `finished` (has a timestamp). The transaction root is included as a span.
    """
    spans: list[dict] = []
    errors: list[dict] = []
    txn = 0
    for t, p in transport.items:
        if t == "transaction" and isinstance(p, dict):
            txn += 1
            tc = (p.get("contexts") or {}).get("trace") or {}
            spans.append(
                {
                    "trace_id": tc.get("trace_id"),
                    "span_id": tc.get("span_id"),
                    "parent_span_id": tc.get("parent_span_id"),
                    "op": tc.get("op"),
                    "description": p.get("transaction"),
                    "data": tc.get("data") or {},
                    "status": tc.get("status"),
                    "start": _ts(p.get("start_timestamp")),
                    "finished": p.get("timestamp") is not None,
                    "is_root": True,
                }
            )
            for s in p.get("spans") or []:
                spans.append(
                    {
                        "trace_id": s.get("trace_id"),
                        "span_id": s.get("span_id"),
                        "parent_span_id": s.get("parent_span_id"),
                        "op": s.get("op"),
                        "description": s.get("description"),
                        "data": s.get("data") or {},
                        "status": s.get("status"),
                        "start": _ts(s.get("start_timestamp")),
                        "finished": s.get("timestamp") is not None,
                        "is_root": False,
                    }
                )
        elif t == "event" and isinstance(p, dict):
            exc = ((p.get("exception") or {}).get("values") or [{}])[-1]
            errors.append({"type": exc.get("type"), "value": str(exc.get("value"))[:300]})
        elif t == "span" and isinstance(p, dict):
            # span-streaming protocol (one item per span)
            for s in p.get("items") or [p]:
                attrs = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (s.get("attributes") or {}).items()}
                spans.append(
                    {
                        "trace_id": s.get("trace_id"),
                        "span_id": s.get("span_id"),
                        "parent_span_id": s.get("parent_span_id"),
                        "op": attrs.get("sentry.op"),
                        "description": s.get("name"),
                        "data": attrs,
                        "status": s.get("status"),
                        "start": _ts(s.get("start_timestamp")),
                        "finished": s.get("end_timestamp") is not None,
                        "is_root": not s.get("parent_span_id"),
                    }
                )
    return {"spans": spans, "errors": errors, "transactions": txn}
