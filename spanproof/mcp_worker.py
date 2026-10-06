"""Run the MCP suite for one (flavor, transport, mode) in this interpreter; print JSON on stdout.

    python -m spanproof.mcp_worker <lowlevel|mcpserver|fastmcp> <memory|stdio|http|http-stateless|sse>
        [--mode default|nodc|noprompts|dcoff|dcout|legacy|stream]

Real MCP transports only: `stdio` launches this module again as a subprocess server (with its own
Sentry client, whose envelopes come back in a file), `http`/`sse` serve a Starlette app with uvicorn
on a free local port, with Sentry's Starlette integration on as in a real deployment.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata as md
import json
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import parse_qs

from . import capture, mcp_sc
from .worker import MARK

MODES = {  # mode -> (send_default_pii, include_prompts, extra init options)
    "default": (True, True, {}),
    "nodc": (False, True, {}),
    "noprompts": (True, False, {}),
    "dcoff": (True, True, {"data_collection": {"gen_ai": {"inputs": False, "outputs": False}}}),
    "dcout": (True, True, {"data_collection": {"gen_ai": {"inputs": True, "outputs": False}}}),
    "legacy": (True, True, {"stream_gen_ai_spans": False}),
    "stream": (True, True, {"trace_lifecycle": "stream"}),
    "asyncio": (True, True, {}),  # + AsyncioIntegration (a fork of the scope per task); a control, not a default
}
MODE = {"name": "default"}


def maybe_asyncio():
    """In "asyncio" mode, enable AsyncioIntegration on the running loop (it needs one)."""
    if MODE["name"] == "asyncio":
        from sentry_sdk.integrations.asyncio import enable_asyncio_integration

        enable_asyncio_integration()


def init_sentry(mode: str, http: bool):
    from sentry_sdk.integrations.mcp import MCPIntegration

    MODE["name"] = mode
    from sentry_sdk.integrations.logging import LoggingIntegration

    pii, prompts, extra = MODES[mode]
    # LoggingIntegration is on by default in a real app, and MCP frameworks log the errors they swallow
    integrations = [MCPIntegration(include_prompts=prompts), LoggingIntegration()]
    if http:
        from sentry_sdk.integrations.starlette import StarletteIntegration

        integrations.append(StarletteIntegration())
    return capture.init(integrations, data_collection=pii, extra=dict(extra))


def error_events(transport) -> list[dict]:
    """Every error event with how it was captured (mcp integration, logging integration, ...)."""
    out = []
    for t, p in transport.items:
        if t != "event" or not isinstance(p, dict):
            continue
        vals = (p.get("exception") or {}).get("values") or [{}]
        exc = vals[-1]  # outermost; the chain (`raise ... from`) is kept in `chain`
        out.append({"type": exc.get("type"), "value": str(exc.get("value"))[:300], "level": p.get("level"),
                    "chain": [f"{v.get('type')}: {str(v.get('value'))[:200]}" for v in vals],
                    "mechanism": (exc.get("mechanism") or {}).get("type"), "logger": p.get("logger"),
                    "message": str((p.get("logentry") or {}).get("formatted") or (p.get("logentry") or {}).get("message"))[:300]})
    return out


def wire_recorder(app):
    """ASGI wrapper recording every JSON-RPC message posted to the server, before MCP parses it."""

    async def asgi(scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await app(scope, receive, send)
        msgs, body = [], b""
        while True:
            m = await receive()
            msgs.append(m)
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        qs = parse_qs(scope.get("query_string", b"").decode())
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        for p in payload if isinstance(payload, list) else [payload]:
            if isinstance(p, dict) and p.get("method"):
                mcp_sc.WIRE.append({"id": p.get("id"), "method": p["method"], "params": p.get("params"),
                                    "session_header": headers.get("mcp-session-id"),
                                    "session_query": (qs.get("session_id") or [None])[0]})
        it = iter(msgs)

        async def replay():
            try:
                return next(it)
            except StopIteration:
                return await receive()

        return await app(scope, replay, send)

    return asgi


def http_app(ll, transport: str):
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    if transport == "sse":
        from mcp.server.sse import SseServerTransport

        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(request.scope, request.receive, request._send) as (r, w):
                await ll.run(r, w, ll.create_initialization_options())
            return Response()

        return Starlette(routes=[Route("/sse", endpoint=handle_sse), Mount("/messages/", app=sse.handle_post_message)])

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    mgr = StreamableHTTPSessionManager(app=ll, stateless=transport == "http-stateless")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        maybe_asyncio()
        async with mgr.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=mgr.handle_request)], lifespan=lifespan)


@contextlib.contextmanager
def serve(app):
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        th.join(10)


async def run_client(flavor: str, transport: str, port: int | None, child_cmd: list | None, ll=None):
    import anyio

    maybe_asyncio()
    if transport == "memory":
        from mcp.shared.memory import create_client_server_memory_streams

        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                async def server():
                    await ll.run(ss[0], ss[1], ll.create_initialization_options())

                tg.start_soon(server)
                async with mcp_sc.client_session(cs[0], cs[1]) as s:
                    await s.initialize()
                    await mcp_sc.drive(s)
                tg.cancel_scope.cancel()
        return
    if transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=child_cmd[0], args=child_cmd[1:], env=dict(os.environ))
        cm = stdio_client(params)
    elif transport == "sse":
        from mcp.client.sse import sse_client

        cm = sse_client(f"http://127.0.0.1:{port}/sse")
    else:
        try:
            from mcp.client.streamable_http import streamable_http_client as shc
        except ImportError:
            from mcp.client.streamable_http import streamablehttp_client as shc
        cm = shc(f"http://127.0.0.1:{port}/mcp/")
    async with cm as streams:
        async with mcp_sc.client_session(streams[0], streams[1]) as s:
            await s.initialize()
            await mcp_sc.drive(s)


def stdio_server_main(flavor: str, mode: str, out: str) -> int:
    """The stdio child: serve one connection over stdin/stdout, then write spans and truth to `out`."""
    transport = init_sentry(mode, http=False)
    import anyio
    import sentry_sdk
    from mcp.server.stdio import stdio_server

    ll = mcp_sc.lowlevel_of(mcp_sc.FLAVORS[flavor]())

    async def main():
        maybe_asyncio()
        async with stdio_server() as (r, w):
            await ll.run(r, w, ll.create_initialization_options())

    exc = None
    try:
        anyio.run(main)
    except BaseException as e:  # noqa: BLE001
        exc = f"{type(e).__name__}: {e}"
    sentry_sdk.flush(timeout=5)
    res = capture.flatten(transport)
    res.update(server_truth=mcp_sc.SERVER, server_exception=exc, error_events=error_events(transport))
    with open(out, "w") as f:
        json.dump(res, f, default=str)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("flavor", choices=sorted(mcp_sc.FLAVORS))
    ap.add_argument("transport", choices=["memory", "stdio", "http", "http-stateless", "sse", "stdio-server"])
    ap.add_argument("--mode", default="default", choices=sorted(MODES))
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.transport == "stdio-server":
        return stdio_server_main(a.flavor, a.mode, a.out)

    http = a.transport in ("http", "http-stateless", "sse")
    tr = init_sentry(a.mode, http=http) if a.transport != "stdio" else capture.init([], data_collection=True)
    import anyio
    import sentry_sdk

    real_stdout = sys.stdout
    exc, child = None, {}
    out_file = tempfile.mktemp(suffix=".json", prefix="spanproof-mcp-")
    with contextlib.redirect_stdout(sys.stderr):
        try:
            ll = None if a.transport == "stdio" else mcp_sc.lowlevel_of(mcp_sc.FLAVORS[a.flavor]())
            cmd = [sys.executable, "-m", "spanproof.mcp_worker", a.flavor, "stdio-server", "--mode", a.mode,
                   "--out", out_file]
            stream = a.mode == "stream"
            root = (sentry_sdk.traces.start_span(name=f"mcp.{a.flavor}.{a.transport}") if stream
                    else sentry_sdk.start_transaction(op="spanproof.scenario", name=f"mcp.{a.flavor}.{a.transport}"))
            with root:
                if http:
                    with serve(wire_recorder(http_app(ll, a.transport))) as port:
                        anyio.run(run_client, a.flavor, a.transport, port, None, ll)
                        time.sleep(0.3)  # let the last response's transaction finish
                else:
                    anyio.run(run_client, a.flavor, a.transport, None, cmd, ll)
        except BaseException as e:  # noqa: BLE001 - every outcome is recorded
            exc = {"type": type(e).__name__, "value": str(e)[:500], "tb": traceback.format_exc()[-3000:]}
        if a.transport == "stdio":
            for _ in range(100):
                if os.path.exists(out_file) and os.path.getsize(out_file):
                    break
                time.sleep(0.05)
            try:
                child = json.load(open(out_file))
                os.unlink(out_file)
            except (OSError, ValueError) as e:
                child = {"spans": [], "errors": [], "server_exception": f"no child result: {e}"}
    sentry_sdk.flush(timeout=5)
    out = capture.flatten(tr)
    out["error_events"] = error_events(tr)
    if a.transport == "stdio":  # the server's spans live in the child; the parent only has its root
        out["spans"] += child.get("spans", [])
        out["errors"] += child.get("errors", [])
        out["error_events"] += child.get("error_events", [])
        mcp_sc.SERVER[:] = child.get("server_truth", [])

    def ver(p):
        try:
            return md.version(p)
        except md.PackageNotFoundError:
            return None

    out.update(scenario=f"mcp.{a.flavor}.{a.transport}", integration="mcp", flavor=a.flavor, transport=a.transport,
               mode=a.mode, data_collection=MODES[a.mode][0], include_prompts=MODES[a.mode][1],
               data_collection_option=MODES[a.mode][2].get("data_collection"),
               span_streaming=a.mode == "stream", versions={p: ver(p) for p in ("sentry-sdk", "mcp", "fastmcp")},
               python=sys.version.split()[0], exception=exc, server_exception=child.get("server_exception"),
               truth={"server": mcp_sc.SERVER, "client": mcp_sc.CLIENT, "wire": mcp_sc.WIRE})
    real_stdout.write(MARK + json.dumps(out, default=str))
    real_stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
