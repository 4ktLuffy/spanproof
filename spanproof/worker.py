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

    from . import capture
    from .mockserver import MockServer
    from .scenario import load_all

    sc = load_all()[a.scenario]
    extra = {}
    if a.span_streaming:
        extra["trace_lifecycle"] = "stream"
    if a.legacy_transport:
        extra["stream_gen_ai_spans"] = False
    transport = capture.init(sc.make_integrations(), data_collection=not a.no_data_collection, extra=extra)

    import sentry_sdk

    import contextlib

    exc = None
    real_stdout = sys.stdout
    with MockServer(sc.replies()) as srv, contextlib.redirect_stdout(sys.stderr):
        root = (sentry_sdk.traces.start_span(name=sc.id) if a.span_streaming
                else sentry_sdk.start_transaction(op="spanproof.scenario", name=sc.id))
        with root:
            try:
                sc.run(srv.url)
            except BaseException as e:  # noqa: BLE001 - we record every outcome
                exc = {"type": type(e).__name__, "value": str(e)[:300],
                       "tb": traceback.format_exc()[-1500:]}
        requests = list(srv.script.requests)
    sentry_sdk.flush(timeout=5)

    def ver(p):
        try:
            return md.version(p)
        except md.PackageNotFoundError:
            return None

    out = capture.flatten(transport)
    out.update(
        scenario=sc.id,
        integration=sc.integration,
        package=sc.package,
        versions={"sentry-sdk": ver("sentry-sdk"), sc.package: ver(sc.package)},
        python=sys.version.split()[0],
        exception=exc,
        requests=[{"path": r["path"]} for r in requests],
        calls=[{"truth": c.truth.as_dict() if c.truth else None, "op": c.op, "completes": c.completes}
               for c in sc.calls],
        expects_exception=sc.expects_exception,
        agent=sc.agent,
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
