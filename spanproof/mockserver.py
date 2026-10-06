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


@dataclass
class Script:
    replies: list[Reply]
    requests: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def next(self, path: str, body: Any) -> Reply:
        with self.lock:
            self.requests.append({"path": path, "body": body})
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

        do_POST = _serve
        do_GET = _serve

    return H


class MockServer:
    def __init__(self, replies: list[Reply]):
        self.script = Script(list(replies))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.script))
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
