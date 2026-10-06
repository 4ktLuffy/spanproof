"""Run ONE scenario in this interpreter and print a JSON result on stdout.

    python -m spanproof.worker <scenario-id> [--no-data-collection] [--span-streaming]
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import os
import sys
import traceback

MARK = "\n@@SPANPROOF-RESULT@@\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--no-data-collection", action="store_true")
    ap.add_argument("--span-streaming", action="store_true")
    ap.add_argument("--legacy-transport", action="store_true",
                    help="stream_gen_ai_spans=False: gen_ai spans stay inside the transaction")
    a = ap.parse_args()

    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        os.environ[k] = "spanproof-dummy-key"
    os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    from . import capture, mockserver
    from .mockserver import MockServer
    from .scenario import load_all

    mockserver.LIVE = mockserver.live_from_env()

    sc = load_all()[a.scenario]
    extra = {}
    if a.span_streaming:
        extra["trace_lifecycle"] = "stream"
    if a.legacy_transport:
        extra["stream_gen_ai_spans"] = False
    transport = capture.init(sc.make_integrations(), data_collection=not a.no_data_collection, extra=extra)

    import sentry_sdk

    sentry_sdk.set_tag("spanproof.scenario", sc.id)
    sentry_sdk.set_tag("spanproof.mode", "nodc" if a.no_data_collection else (
        "legacy" if a.legacy_transport else ("stream" if a.span_streaming else "default")))

    import contextlib

    exc = None
    real_stdout = sys.stdout
    # live: every request goes to the real provider (queued replies would be served first, as faults)
    with MockServer([] if mockserver.LIVE else sc.replies()) as srv, contextlib.redirect_stdout(sys.stderr):
        root = (sentry_sdk.traces.start_span(name=sc.id) if a.span_streaming
                else sentry_sdk.start_transaction(op="spanproof.scenario", name=sc.id))
        with root:
            try:
                sc.run(srv.url)
            except BaseException as e:  # noqa: BLE001 - we record every outcome
                exc = {"type": type(e).__name__, "value": str(e)[:300],
                       "tb": traceback.format_exc()[-1500:]}
        requests = list(srv.script.requests)
    # A real DSN needs time to deliver before this process exits.
    recordings = list(srv.script.recordings)  # after the server closed and joined its threads
    sentry_sdk.flush(timeout=30 if os.environ.get("SPANPROOF_DSN") else 5)

    def ver(p):
        try:
            return md.version(p)
        except md.PackageNotFoundError:
            return None

    out = capture.flatten(transport)
    calls = [{"truth": c.truth.as_dict() if c.truth else None, "op": c.op, "completes": c.completes}
             for c in sc.calls]
    if mockserver.LIVE:
        # Live: the truth is what the provider really returned for each request, in order.
        from .live import truth_from_recording

        early = any(not c.completes for c in sc.calls)
        calls = []
        for rec in recordings:
            t = truth_from_recording(rec)
            calls.append({"truth": None if early else (t.as_dict() if t else None), "op": "gen_ai.chat",
                          "completes": not early, "provider_usage": t.as_dict() if t else None})
        cas = os.environ.get("SPANPROOF_CASSETTES")
        if cas:
            os.makedirs(cas, exist_ok=True)
            mode = "nodc" if a.no_data_collection else ("legacy" if a.legacy_transport else "default")
            json.dump(recordings, open(os.path.join(cas, f"{sc.id}.{mode}.json"), "w"), indent=1)
    out.update(
        scenario=sc.id,
        integration=sc.integration,
        package=sc.package,
        versions={"sentry-sdk": ver("sentry-sdk"), sc.package: ver(sc.package)},
        python=sys.version.split()[0],
        exception=exc,
        requests=[{"path": r["path"]} for r in requests],
        calls=calls,
        live=bool(mockserver.LIVE),
        expects_exception=sc.expects_exception,
        agent=(dict(sc.agent, tools=[]) if (sc.agent and mockserver.LIVE) else sc.agent),
        data_collection=not a.no_data_collection,
        span_streaming=a.span_streaming,
        legacy_transport=a.legacy_transport,
        tags=sc.tags,
    )
    real_stdout.write(MARK + json.dumps(out, default=str))
    real_stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
