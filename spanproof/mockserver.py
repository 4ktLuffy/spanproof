"""A scripted provider server.

Each request pops the next response from a queue, so a scenario can script a
multi-turn agent run (tool call, then final answer). Responses are plain JSON
bodies or Server-Sent-Event streams, and a stream can be cut part-way to model a
dropped connection.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Reply:
    body: Any = None  # JSON body for a non-streaming reply
    events: list[Any] | None = None  # SSE payloads for a streaming reply
    status: int = 200
    sse_event_names: list[str] | None = None  # Anthropic-style "event:" lines
    sse_done: bool = True  # emit "data: [DONE]" (OpenAI style)
    cut_after: int | None = None  # drop the connection after N events
    content_type: str | None = None
    path_contains: str | None = None  # optional routing guard
    proxy: bool = False  # forward to the live upstream and record the real response


LIVE = None  # set by live_from_env(): {"base", "key", "model", "max_tokens"}


def live_from_env() -> dict | None:
    """Live mode: SPANPROOF_LIVE=1 forwards every request to SPANPROOF_UPSTREAM (OpenAI-compatible)."""
    import os

    if os.environ.get("SPANPROOF_LIVE") != "1":
        return None
    return {"base": os.environ["SPANPROOF_UPSTREAM"].rstrip("/"), "key": os.environ["SPANPROOF_UPSTREAM_KEY"],
            "model": os.environ.get("SPANPROOF_UPSTREAM_MODEL"),
            "max_tokens": int(os.environ.get("SPANPROOF_UPSTREAM_MAX_TOKENS", "512"))}


@dataclass
class Script:
    replies: list[Reply]
    requests: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    recordings: list[dict] = field(default_factory=list)  # live mode: what the upstream really returned

    def next(self, path: str, body: Any) -> Reply:
        with self.lock:
            self.requests.append({"path": path, "body": body})
            if LIVE:
                # scripted replies queued in live mode are injected faults (e.g. three 500s),
                # served before the real upstream is used
                return self.replies.pop(0) if self.replies else Reply(proxy=True)
            if not self.replies:
                return Reply(body={"error": {"message": "script exhausted", "type": "spanproof"}}, status=500)
            return self.replies.pop(0)


def _handler(script: Script):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence
            pass

        def _serve(self):
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = raw.decode("utf-8", "replace")
            reply = script.next(self.path, body)
            if reply.proxy:
                self._proxy(body)
                return
            if reply.events is None:
                data = json.dumps(reply.body).encode()
                self.send_response(reply.status)
                self.send_header("content-type", reply.content_type or "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(reply.status)
            self.send_header("content-type", reply.content_type or "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            for i, ev in enumerate(reply.events):
                if reply.cut_after is not None and i >= reply.cut_after:
                    # abrupt close: no terminator, socket shut
                    try:
                        self.wfile.flush()
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    return
                chunk = ""
                if reply.sse_event_names:
                    chunk += f"event: {reply.sse_event_names[i]}\n"
                payload = ev if isinstance(ev, str) else json.dumps(ev)
                chunk += f"data: {payload}\n\n"
                self.wfile.write(chunk.encode())
                self.wfile.flush()
            if reply.sse_done and not reply.sse_event_names:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        def _proxy(self, body):
            """Forward to the live upstream, stream the bytes back unchanged, keep a copy."""
            import urllib.error
            import urllib.request

            if isinstance(body, dict):
                if LIVE.get("model"):
                    body["model"] = LIVE["model"]
                for k in ("max_tokens", "max_completion_tokens"):
                    if k in body:
                        body[k] = min(int(body[k]), LIVE["max_tokens"])
                if "max_tokens" not in body and "max_completion_tokens" not in body:
                    body["max_tokens"] = LIVE["max_tokens"]
                for k in ("service_tier", "store", "parallel_tool_calls", "metadata", "verbosity",
                          "prompt_cache_key", "safety_identifier"):
                    body.pop(k, None)  # not every OpenAI-compatible upstream accepts these
            data = json.dumps(body).encode() if body is not None else None
            path = self.path if not self.path.startswith("/v1") else "/v1" + self.path[3:]
            req = urllib.request.Request(LIVE["base"] + path, data=data, method=self.command,
                                         headers={"Authorization": f"Bearer {LIVE['key']}",
                                                  "Content-Type": "application/json", "User-Agent": "spanproof/0.1"})
            try:
                resp = urllib.request.urlopen(req, timeout=120)
                status, ctype = resp.status, resp.headers.get("content-type", "application/json")
            except urllib.error.HTTPError as e:
                resp, status, ctype = e, e.code, e.headers.get("content-type", "application/json")
            raw = b""
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                    if not chunk:
                        break
                    raw += chunk
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except OSError:
                        # the client stopped reading (early close); keep draining so the
                        # recording holds what the provider actually produced and billed
                        pass
            finally:
                with script.lock:
                    script.recordings.append({"path": path, "status": status, "content_type": ctype,
                                              "request": body, "raw": raw.decode("utf-8", "replace")})

        do_POST = _serve
        do_GET = _serve

    return H


class MockServer:
    def __init__(self, replies: list[Reply]):
        self.script = Script(list(replies))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.script))
        if LIVE:
            # join request threads on close, so a recording is complete before anyone reads it
            # (only in live mode: scripted replies finish instantly, and keep-alive threads would block)
            self.httpd.daemon_threads = False
            self.httpd.block_on_close = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
