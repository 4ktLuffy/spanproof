"""MCP servers and client sessions for the MCP checks.

One server exposes the same tools, prompts and resources in three flavors: the low-level
`mcp.server.lowlevel.Server`, the SDK's high-level server (`FastMCP` in mcp 1.x, `MCPServer` in
2.x) and the standalone `fastmcp` package. Every handler records what it really received and
returned (`SERVER`); the client records what came back over the wire (`CLIENT`); the HTTP
transports record every JSON-RPC message as it arrived (`WIRE`). Those three are the truth the
spans are checked against. Nothing in the SDK or in sentry_sdk is patched.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.metadata as md
import json

SERVER: list[dict] = []  # one record per handler invocation
CLIENT: list[dict] = []  # one record per client operation
WIRE: list[dict] = []  # one record per JSON-RPC message received over HTTP

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082")).decode()
SECRET_ARG = "SPANPROOF-SECRET-ARG"  # never allowed on a span with data collection off
SECRET_OUT = "SPANPROOF-SECRET-OUT"


def _ver(p):
    try:
        return tuple(int(x) for x in md.version(p).split(".")[:3] if x.isdigit())
    except md.PackageNotFoundError:
        return None


MCP_V = _ver("mcp") or (0,)
V2 = MCP_V >= (2,)
LOWLEVEL_RETURNS_RESULT = True

TOOLS = {  # name -> (input schema properties, output schema or None)
    "echo": ({"text": {"type": "string"}, "n": {"type": "integer"}}, None),
    "boom": ({"x": {"type": "string"}}, None),
    "soft_error": ({}, None),
    "image": ({}, None),
    "image_only": ({}, None),
    "embedded": ({}, None),
    "structured": ({"city": {"type": "string"}},
                   {"type": "object", "properties": {"city": {"type": "string"}, "temp": {"type": "integer"}}}),
    "logs": ({}, None),
    "secret": ({"password": {"type": "string"}}, None),
    "slow": ({"i": {"type": "integer"}}, None),
}
PROMPTS = ["greet", "multi", "boom_prompt"]
RESOURCES = ["res://doc", "res://missing"]


def behave(name: str, args: dict):
    """The tool's real behaviour, independent of flavor: ("text", s) | ("raise", exc) |
    ("is_error", s) | ("blocks", [...]) | ("structured", dict)."""
    if name == "echo":
        return "text", f"echo:{args.get('text')}:{args.get('n')}"
    if name == "boom":
        return "raise", ValueError(f"boom-{args.get('x')}")
    if name == "soft_error":
        return "is_error", "soft failure"
    if name == "image":
        return "blocks", [("image", PNG), ("text", "caption")]
    if name == "image_only":
        return "blocks", [("image", PNG)]
    if name == "embedded":
        return "blocks", [("resource", "res://inline", "embedded body")]
    if name == "structured":
        return "structured", {"city": args.get("city"), "temp": 21}
    if name == "secret":
        return "text", f"{SECRET_OUT}:{len(args.get('password') or '')}"
    if name in ("slow", "logs"):
        return "text", f"{name}:{args.get('i')}"
    raise KeyError(name)


def _blocks(kind, payload, t):
    if kind == "text":
        return [t.TextContent(type="text", text=payload)]
    if kind == "is_error":
        return [t.TextContent(type="text", text=payload)]
    out = []
    for b in payload:
        if b[0] == "image":
            out.append(t.ImageContent(type="image", data=b[1], mimeType="image/png") if not V2
                       else t.ImageContent(type="image", data=b[1], mime_type="image/png"))
        elif b[0] == "text":
            out.append(t.TextContent(type="text", text=b[1]))
        else:
            res = (t.TextResourceContents(uri=b[1], text=b[2]) if not V2
                   else t.TextResourceContents(uri=b[1], text=b[2]))
            out.append(t.EmbeddedResource(type="resource", resource=res))
    return out


def _truth(method, target, args, request_id, kind, payload, protocol=None):
    r = {"method": method, "target": target, "args": args, "request_id": request_id, "protocol": protocol,
         "raised": kind == "raise", "returned_is_error": kind == "is_error"}
    if kind == "text":
        r.update(content_types=["text"], text=payload)
    elif kind == "is_error":
        r.update(content_types=["text"], text=payload)
    elif kind == "blocks":
        r.update(content_types=[b[0] for b in payload],
                 text=" ".join(b[1] for b in payload if b[0] == "text") or None)
    elif kind == "structured":
        r.update(content_types=["text"], structured=payload)
    elif kind == "raise":
        r.update(error=f"{type(payload).__name__}: {payload}")
    SERVER.append(r)
    return r


def _v1_rid():
    try:
        from mcp.server.lowlevel.server import request_ctx

        c = request_ctx.get()
        return c.request_id
    except Exception:
        return None


def _prompt_result(name, args, t):
    def msg(role, text):
        return t.PromptMessage(role=role, content=t.TextContent(type="text", text=text))

    if name == "greet":
        return [msg("user", f"Hello {args.get('who')}")]
    if name == "multi":
        return [msg("user", "first"), msg("assistant", "second")]
    raise RuntimeError("boom-prompt")


def _resource_body(uri):
    if uri == "res://doc":
        return "DOC-CONTENT"
    raise FileNotFoundError(f"missing {uri}")


# ---------------------------------------------------------------- low-level server

def _returns_result() -> bool:
    """Can a v1 low-level tool return a CallToolResult (mcp >= ~1.17)? Read the module source,
    since Server.call_tool itself is replaced by Sentry's patch at this point."""
    import inspect

    import mcp.server.lowlevel.server as m

    return V2 or "isinstance(results, types.CallToolResult)" in inspect.getsource(m)


def lowlevel():
    import mcp.types as t
    from mcp.server.lowlevel import Server

    global LOWLEVEL_RETURNS_RESULT
    LOWLEVEL_RETURNS_RESULT = _returns_result()

    tools = []
    for n, (props, out) in TOOLS.items():
        kw = {"name": n, "description": n}
        if V2:
            kw.update(input_schema={"type": "object", "properties": props})
            if out:
                kw.update(output_schema=out)
        else:
            kw.update(inputSchema={"type": "object", "properties": props})
            if out:
                kw.update(outputSchema=out)
        tools.append(t.Tool(**kw))
    prompts = [t.Prompt(name=p, description=p) for p in PROMPTS]
    resources = [t.Resource(uri=u, name=u.split("//")[1]) for u in RESOURCES]

    async def tool(name, args, rid, protocol=None, log=None):
        if name not in TOOLS:
            e = KeyError(f"unknown tool {name}")
            _truth("tools/call", name, args, rid, "raise", e, protocol)
            raise e
        kind, payload = behave(name, args)
        _truth("tools/call", name, args, rid, kind, payload, protocol)
        if name == "logs" and log:
            await log()
        if name == "slow":
            await asyncio.sleep(delay(args.get("i") or 0))
        if kind == "raise":
            raise payload
        if kind == "structured":
            return payload
        if kind == "is_error":
            if V2:
                return t.CallToolResult(content=_blocks(kind, payload, t), is_error=True)
            if not LOWLEVEL_RETURNS_RESULT:  # mcp < 1.17 cannot return a CallToolResult: plain text, no error
                SERVER[-1]["returned_is_error"] = False
                return _blocks("text", payload, t)
            return t.CallToolResult(content=_blocks(kind, payload, t), isError=True)
        return _blocks(kind, payload, t)

    async def prompt(name, args, rid, protocol=None):
        try:
            msgs = _prompt_result(name, args, t)
        except RuntimeError as e:
            _truth("prompts/get", name, args, rid, "raise", e, protocol)
            raise
        r = _truth("prompts/get", name, args, rid, "text", msgs[0].content.text, protocol)
        r.update(message_count=len(msgs), roles=[m.role for m in msgs])
        return t.GetPromptResult(messages=msgs)

    async def resource(uri, rid, protocol=None):
        try:
            body = _resource_body(uri)
        except FileNotFoundError as e:
            _truth("resources/read", uri, {}, rid, "raise", e, protocol)
            raise
        _truth("resources/read", uri, {}, rid, "text", body, protocol)
        return body

    if not V2:
        srv = Server("spanproof")

        @srv.list_tools()
        async def _lt():
            return tools

        @srv.call_tool()
        async def _ct(name, arguments):
            async def log():
                await srv.request_context.session.send_log_message(level="info", data="log-from-tool")
            return await tool(name, arguments or {}, _v1_rid(), log=log)

        @srv.list_prompts()
        async def _lp():
            return prompts

        @srv.get_prompt()
        async def _gp(name, arguments):
            return await prompt(name, arguments or {}, _v1_rid())

        @srv.list_resources()
        async def _lr():
            return resources

        @srv.read_resource()
        async def _rr(uri):
            return await resource(str(uri), _v1_rid())

        return srv

    async def on_list_tools(ctx, params):
        return t.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params):
        async def log():
            await ctx.session.send_log_message(level="info", data="log-from-tool")
        r = await tool(params.name, params.arguments or {}, ctx.request_id, ctx.protocol_version, log)
        if isinstance(r, t.CallToolResult):
            return r
        if isinstance(r, dict):
            return t.CallToolResult(content=[t.TextContent(type="text", text=json.dumps(r))], structured_content=r)
        return t.CallToolResult(content=r)

    async def on_list_prompts(ctx, params):
        return t.ListPromptsResult(prompts=prompts)

    async def on_get_prompt(ctx, params):
        return await prompt(params.name, params.arguments or {}, ctx.request_id, ctx.protocol_version)

    async def on_list_resources(ctx, params):
        return t.ListResourcesResult(resources=resources)

    async def on_read_resource(ctx, params):
        body = await resource(str(params.uri), ctx.request_id, ctx.protocol_version)
        return t.ReadResourceResult(contents=[t.TextResourceContents(uri=str(params.uri), text=body)])

    return Server("spanproof", on_list_tools=on_list_tools, on_call_tool=on_call_tool,
                  on_list_prompts=on_list_prompts, on_get_prompt=on_get_prompt,
                  on_list_resources=on_list_resources, on_read_resource=on_read_resource)


# ---------------------------------------------------------------- high-level servers

def _highlevel(app, rid, t, is_error_result):
    """Register the same tools on a FastMCP-style app (decorators .tool/.prompt/.resource)."""

    def tool_body(name):
        def run(args):
            kind, payload = behave(name, args)
            _truth("tools/call", name, args, rid(), kind, payload)
            if kind == "raise":
                raise payload
            if kind == "structured":
                return payload
            if kind == "is_error":
                return is_error_result(payload)
            if kind == "text":
                return payload
            return _blocks(kind, payload, t)
        return run

    def deco(fn, name):
        try:
            return app.tool(name=name)(fn)
        except TypeError:
            fn.__name__ = name
            return app.tool()(fn)

    async def echo(text: str, n: int) -> str:
        return tool_body("echo")({"text": text, "n": n})

    async def boom(x: str) -> str:
        return tool_body("boom")({"x": x})

    async def soft_error():
        return tool_body("soft_error")({})

    async def image() -> list:
        return tool_body("image")({})

    async def image_only() -> list:
        return tool_body("image_only")({})

    async def embedded() -> list:
        return tool_body("embedded")({})

    async def structured(city: str) -> dict:
        return tool_body("structured")({"city": city})

    async def secret(password: str) -> str:
        return tool_body("secret")({"password": password})

    async def slow(i: int) -> str:
        await asyncio.sleep(delay(i))
        return tool_body("slow")({"i": i})

    async def logs() -> str:
        return tool_body("logs")({})

    for fn in (echo, boom, soft_error, image, image_only, embedded, structured, secret, slow, logs):
        deco(fn, fn.__name__)

    if hasattr(app, "prompt"):
        def prompt_body(name, args, texts):
            if name == "boom_prompt":
                e = RuntimeError("boom-prompt")
                _truth("prompts/get", name, args, rid(), "raise", e)
                raise e
            r = _truth("prompts/get", name, args, rid(), "text", texts[0])
            r.update(message_count=len(texts), roles=["user"] * len(texts))
            return texts[0] if len(texts) == 1 else texts

        async def greet(who: str):  # a plain string is one user message in every flavor
            return prompt_body("greet", {"who": who}, [f"Hello {who}"])

        async def multi():
            return prompt_body("multi", {}, ["first", "second"])

        async def boom_prompt():
            return prompt_body("boom_prompt", {}, [])

        for fn in (greet, multi, boom_prompt):
            try:
                app.prompt(name=fn.__name__)(fn)
            except TypeError:
                app.prompt()(fn)

    def res(uri):
        def read() -> str:
            try:
                body = _resource_body(uri)
            except FileNotFoundError as e:
                _truth("resources/read", uri, {}, rid(), "raise", e)
                raise
            _truth("resources/read", uri, {}, rid(), "text", body)
            return body
        read.__name__ = uri.split("//")[1]
        return read

    for u in RESOURCES:
        app.resource(u)(res(u))
    return app


def mcpserver():
    import mcp.types as t

    if V2:
        from contextvars import ContextVar

        from mcp.server.mcpserver import MCPServer

        app = MCPServer("spanproof")
        rid: ContextVar = ContextVar("spanproof_rid", default=None)

        # MCPServer has no request contextvar: read the id from the lowlevel context via middleware
        # (a ContextVar, so concurrent requests each see their own)
        async def grab(ctx, call_next):
            rid.set(ctx.request_id)
            return await call_next(ctx)

        lowlevel_of(app).middleware.insert(0, grab)
        return _highlevel(app, rid.get, t,
                          lambda s: t.CallToolResult(content=[t.TextContent(type="text", text=s)], is_error=True))
    from mcp.server.fastmcp import FastMCP

    return _highlevel(FastMCP("spanproof"), _v1_rid, t,
                      lambda s: t.CallToolResult(content=[t.TextContent(type="text", text=s)], isError=True))


def fastmcp_app():
    import mcp.types as t
    from fastmcp import FastMCP

    app = FastMCP("spanproof")

    def rid():
        r = _v1_rid()
        if r is not None:
            return r
        try:
            from fastmcp.server.dependencies import get_context

            return get_context().request_id
        except Exception:
            return None

    kw = (lambda s: t.CallToolResult(content=[t.TextContent(type="text", text=s)], is_error=True)) if V2 else (
        lambda s: t.CallToolResult(content=[t.TextContent(type="text", text=s)], isError=True))
    return _highlevel(app, rid, t, kw)


FLAVORS = {"lowlevel": lowlevel, "mcpserver": mcpserver, "fastmcp": fastmcp_app}


def lowlevel_of(app):
    for a in ("_mcp_server", "_lowlevel_server"):
        if hasattr(app, a):
            return getattr(app, a)
    return app


# ---------------------------------------------------------------- client operations

OPS = [  # (label, method, target, args)
    ("tool_ok", "tools/call", "echo", {"text": "hi", "n": 3}),
    ("tool_raise", "tools/call", "boom", {"x": "1"}),
    ("tool_is_error", "tools/call", "soft_error", {}),
    ("tool_image", "tools/call", "image", {}),
    ("tool_image_only", "tools/call", "image_only", {}),
    ("tool_embedded", "tools/call", "embedded", {}),
    ("tool_structured", "tools/call", "structured", {"city": "Oslo"}),
    ("tool_secret", "tools/call", "secret", {"password": SECRET_ARG}),
    ("tool_logs", "tools/call", "logs", {}),
    ("tool_bad_args", "tools/call", "echo", {"text": "bad", "n": "not-a-number"}),  # fails input validation
    ("tool_unknown", "tools/call", "nope", {}),
    ("prompt_single", "prompts/get", "greet", {"who": "ada"}),
    ("prompt_multi", "prompts/get", "multi", {}),
    ("prompt_raise", "prompts/get", "boom_prompt", {}),
    ("resource_ok", "resources/read", "res://doc", {}),
    ("resource_raise", "resources/read", "res://missing", {}),
    ("list_tools", "tools/list", None, {}),
    ("list_prompts", "prompts/list", None, {}),
    ("list_resources", "resources/list", None, {}),
    ("ping", "ping", None, {}),
]
CONCURRENT = 5  # parallel `slow` calls on one session: 0-4 finish last-first, 5-9 first-first


def delay(i: int) -> float:
    return 0.05 * (5 - i) if i < CONCURRENT else 0.05 * (i - CONCURRENT + 1)


def _tool_outcome(r) -> dict:
    """What a CallToolResult told the client (mcp 1.x uses camelCase fields, 2.x snake_case)."""
    content = getattr(r, "content", None) or []
    st = getattr(r, "structuredContent", None)
    return {"is_error": bool(getattr(r, "isError", None) or getattr(r, "is_error", None)),
            "content_types": [getattr(c, "type", None) for c in content],
            "text": " ".join(c.text for c in content if getattr(c, "type", None) == "text") or None,
            "structured": st if st is not None else getattr(r, "structured_content", None)}


async def drive(session, ops=None):
    """Run every operation on an initialized ClientSession and record what came back."""
    for label, method, target, args in ops or OPS:
        rec = {"label": label, "method": method, "target": target, "args": args, "is_error": None,
               "protocol_error": None}
        try:
            if method == "tools/call":
                rec.update(_tool_outcome(await session.call_tool(target, args)))
            elif method == "prompts/get":
                r = await session.get_prompt(target, {k: str(v) for k, v in args.items()} or None)
                rec.update(message_count=len(r.messages), roles=[str(m.role) for m in r.messages],
                           text=getattr(r.messages[0].content, "text", None) if r.messages else None)
            elif method == "resources/read":
                r = await session.read_resource(target)
            elif method == "tools/list":
                await session.list_tools()
            elif method == "prompts/list":
                await session.list_prompts()
            elif method == "resources/list":
                await session.list_resources()
            elif method == "ping":
                await session.send_ping()
        except Exception as e:
            rec["protocol_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        CLIENT.append(rec)

    async def one(i):
        rec = {"label": f"concurrent_{i}", "method": "tools/call", "target": "slow", "args": {"i": i},
               "is_error": None, "protocol_error": None}
        try:
            rec.update(_tool_outcome(await session.call_tool("slow", {"i": i})))
        except Exception as e:
            rec["protocol_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        CLIENT.append(rec)

    if ops is None:
        await asyncio.gather(*(one(i) for i in range(CONCURRENT)))
        await asyncio.gather(*(one(i) for i in range(CONCURRENT, 2 * CONCURRENT)))


def client_session(read, write):
    from mcp import ClientSession

    async def _log(params):
        return None

    try:
        return ClientSession(read, write, logging_callback=_log)
    except TypeError:
        return ClientSession(read, write)
