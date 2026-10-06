"""Unit tests for the MCP checks, on hand-built results.

Every rule gets a planted defect (it must fire) and a correct span (it must stay quiet).
"""

from spanproof import mcp_checks as mc
from spanproof.mcp_sc import PNG, SECRET_ARG, SECRET_OUT

ROOT = {"op": "spanproof.scenario", "span_id": "root", "parent_span_id": None, "trace_id": "t", "finished": True,
        "status": None, "is_root": True, "start": 0.0, "description": "x", "data": {}}


def span(sid="s1", desc="tools/call echo", parent="root", status=None, start=1.0, **data):
    method, target = desc.split(" ", 1)
    key = {"tools/call": "mcp.tool.name", "prompts/get": "mcp.prompt.name", "resources/read": "mcp.resource.uri"}[method]
    d = {"mcp.method.name": method, key: target, "mcp.request.id": "1"}
    d.update({k.replace("__", "."): v for k, v in data.items()})
    return {"op": "mcp.server", "span_id": sid, "parent_span_id": parent, "trace_id": "t", "finished": True,
            "status": status, "is_root": False, "start": start, "description": desc, "data": d}


def call(label="tool_ok", method="tools/call", target="echo", args=None, rid=1, **kw):
    c = {"label": label, "method": method, "target": target, "args": args or {}, "is_error": False,
         "protocol_error": None, "content_types": ["text"], "text": "echo:hi", "structured": None}
    c.update(kw)
    s = {"method": method, "target": target, "args": args or {}, "request_id": rid, "raised": False,
         "returned_is_error": False}
    return c, s


def result(spans, calls, transport="memory", data_collection=True, include_prompts=True, dco=None, wire=(),
           errors=(), handled=True):
    return {"scenario": "mcp", "integration": "mcp", "flavor": "lowlevel", "transport": transport, "mode": "m",
            "spans": [ROOT, *spans], "errors": list(errors), "data_collection": data_collection,
            "include_prompts": include_prompts, "data_collection_option": dco, "exception": None,
            "truth": {"client": [c for c, _ in calls], "server": [s for _, s in calls] if handled else [],
                      "wire": list(wire)}}


GOOD = span(mcp__tool__result__content="echo:hi")


def rules(findings, prefix):
    return sorted({f["rule"] for f in findings if f["rule"].startswith(prefix)})


def test_negative_control_whole_suite():
    """A correct span for a correct request raises nothing except the known convention debt."""
    s = span(gen_ai__tool__name="echo", jsonrpc__request__id="1", mcp__tool__result__content="echo:hi")
    f = mc.run_all(result([s], [call()]))
    assert [x["rule"] for x in f if x["rule"] != "mcp.conventions.deprecated"] == []


def test_lifecycle_missing_and_duplicate():
    assert rules(mc.check_lifecycle(result([], [call()])), "mcp.lifecycle") == ["mcp.lifecycle.missing"]
    two = [GOOD, span(sid="s2", mcp__tool__result__content="echo:hi")]
    assert rules(mc.check_lifecycle(result(two, [call()])), "mcp.lifecycle") == ["mcp.lifecycle.duplicate"]
    assert mc.check_lifecycle(result([GOOD], [call()])) == []


def test_lifecycle_unhandled_is_low_and_method_not_found_is_skipped():
    c = call(protocol_error="McpError: Method not found")
    assert mc.check_lifecycle(result([], [c], handled=False)) == []
    c = call(protocol_error="McpError: Input validation error")
    assert rules(mc.check_lifecycle(result([], [c], handled=False)), "") == ["mcp.lifecycle.missing_unhandled"]


def test_identity_request_id_and_name():
    bad = span(desc="tools/call echo", mcp__request__id="7")
    bad["description"] = "tools/call"
    f = mc.check_identity(result([bad], [call(rid=1)]))
    assert rules(f, "mcp.identity") == ["mcp.identity.name", "mcp.identity.request_id"]
    f = mc.check_identity(result([span(mcp__request__id="7")], [call(rid=1)]))
    assert rules(f, "") == ["mcp.identity.request_id"]
    assert mc.check_identity(result([GOOD], [call(rid=1)])) == []


def test_inputs_wrong_missing_extra():
    c = call(args={"text": "hi", "n": 3})
    s = span(mcp__request__argument__text="hi", mcp__request__argument__n="3")
    assert mc.check_inputs(result([s], [c])) == []
    s = span(mcp__request__argument__text="HI", mcp__request__argument__z="1")
    assert rules(mc.check_inputs(result([s], [c])), "") == ["mcp.input.extra", "mcp.input.missing", "mcp.input.wrong"]


def test_outputs_binary_dropped_wrong_count_structured():
    c = call(content_types=["image"], text=None)
    assert rules(mc.check_outputs(result([span(mcp__tool__result__content=f'[{{"data": "{PNG}"}}]')], [c])), "") == [
        "mcp.output.binary"]
    c = call(content_types=["image", "text"], text="caption")
    assert rules(mc.check_outputs(result([span(mcp__tool__result__content="caption")], [c])), "") == [
        "mcp.output.dropped_content"]
    assert rules(mc.check_outputs(result([span(mcp__tool__result__content="nope")], [call()])), "") == ["mcp.output.wrong"]
    c = call(structured={"a": 1, "b": 2}, text='{"a": 1, "b": 2}')
    assert rules(mc.check_outputs(result([span(mcp__tool__result__content='{"a": 1, "b": 2}',
                                               mcp__tool__result__content_count=2)], [c])), "") == ["mcp.output.content_count"]
    c = call(structured={"result": "x"}, text="x")
    assert rules(mc.check_outputs(result([span(mcp__tool__result__content="x")], [c])), "") == [
        "mcp.output.structured_ignored"]
    assert mc.check_outputs(result([GOOD], [call()])) == []
    assert rules(mc.check_outputs(result([span()], [call()])), "") == ["mcp.output.missing"]


def test_outputs_prompt():
    c = call("prompt_single", "prompts/get", "greet", message_count=1, roles=["user"], text="Hello")
    good = span(desc="prompts/get greet", mcp__prompt__result__message_count=1, mcp__prompt__result__message_role="user",
                mcp__prompt__result__message_content="Hello")
    assert mc.check_outputs(result([good], [c])) == []
    bad = span(desc="prompts/get greet", mcp__prompt__result__message_count=2)
    assert rules(mc.check_outputs(result([bad], [c])), "") == ["mcp.output.message_count", "mcp.output.prompt"]


def test_privacy_leaks():
    s = span(mcp__request__argument__password=SECRET_ARG, mcp__tool__result__content=SECRET_OUT)
    f = mc.check_privacy(result([s], [call()], data_collection=False))
    assert rules(f, "") == ["mcp.privacy.arguments_without_pii", "mcp.privacy.output_leak"]
    f = mc.check_privacy(result([s], [call()], dco={"gen_ai": {"inputs": False, "outputs": False}}))
    assert rules(f, "") == ["mcp.privacy.input_leak", "mcp.privacy.output_leak"]
    f = mc.check_privacy(result([s], [call()], include_prompts=False))
    assert rules(f, "") == ["mcp.privacy.output_leak"]
    assert mc.check_privacy(result([span()], [call()], data_collection=False)) == []
    assert mc.check_privacy(result([s], [call()])) == []


def test_errors_status_spurious_not_captured():
    c, s = call(is_error=True, text="soft failure")
    s["returned_is_error"] = True
    good = span(mcp__tool__result__content="soft failure")
    assert rules(mc.check_errors(result([good], [(c, s)])), "") == ["mcp.errors.status"]
    assert mc.check_errors(result([span(status="internal_error")], [(c, s)])) == []
    assert mc.check_errors(result([span(error__type="tool_error")], [(c, s)])) == []
    assert rules(mc.check_errors(result([span(status="error")], [call()])), "") == ["mcp.errors.spurious"]
    c, s = call(protocol_error="McpError: boom-1", text=None)
    s.update(raised=True, error="ValueError: boom-1")
    assert rules(mc.check_errors(result([span(status="internal_error")], [(c, s)])), "") == ["mcp.errors.not_captured"]
    assert mc.check_errors(result([span(status="internal_error")], [(c, s)],
                                  errors=[{"type": "ValueError", "value": "boom-1"}])) == []


def test_errors_captured_only_by_logging():
    c, s = call(is_error=True, text="Error executing tool echo")
    s.update(raised=True, error="ValueError: boom-1")
    r = result([span(status="internal_error")], [(c, s)])
    r["error_events"] = [{"type": "UnexpectedToolError", "value": "Error executing tool echo", "mechanism": "logging",
                          "chain": ["ValueError: boom-1", "UnexpectedToolError: Error executing tool echo"]}]
    assert rules(mc.check_errors(r), "") == ["mcp.errors.captured_by_logging_only"]
    r["error_events"][0]["mechanism"] = "mcp"
    assert mc.check_errors(r) == []


def test_transport_value_and_session():
    wire = [{"id": 1, "method": "tools/call", "session_header": "abc", "session_query": None}]
    good = span(mcp__transport="http", network__transport="tcp", mcp__session__id="abc")
    assert mc.check_transport(result([good], [call()], transport="http", wire=wire)) == []
    bad = span(mcp__transport="stdio", network__transport="pipe")
    assert rules(mc.check_transport(result([bad], [call()], transport="http", wire=wire)), "") == [
        "mcp.transport.session", "mcp.transport.value"]
    assert mc.check_transport(result([bad], [call()], transport="memory")) == []


def test_structure_nested_detached_shared():
    a, b = span(sid="a", start=1), span(sid="b", parent="a", start=2)
    assert rules(mc.check_structure(result([a, b], [call()])), "") == ["mcp.structure.nested"]
    assert mc.check_structure(result([a, span(sid="b", start=2)], [call()])) == []
    h1 = dict(ROOT, span_id="h1", op="http.server", is_root=True)
    h2 = dict(ROOT, span_id="h2", op="http.server", is_root=True)
    under = [h1, h2, span(sid="a", parent="h1"), span(sid="b", parent="h2")]
    assert mc.check_structure(result(under, [call()], transport="http")) == []
    shared = [h1, span(sid="a", parent="h1"), span(sid="b", parent="h1")]
    assert rules(mc.check_structure(result(shared, [call()], transport="http")), "") == ["mcp.structure.shared_http_parent"]
    assert rules(mc.check_structure(result([span(parent=None)], [call()], transport="sse")), "") == [
        "mcp.structure.detached"]
    assert rules(mc.check_structure(result([span(parent="gone")], [call()])), "") == ["mcp.structure.orphan"]


def test_conventions_unknown_type_deprecated():
    s = span(mcp__made__up="x")
    s["data"]["mcp.request.id"] = 1
    f = mc.check_conventions(result([s], [call()]))
    assert rules(f, "") == ["mcp.conventions.deprecated", "mcp.conventions.type", "mcp.conventions.unknown"]
    ok = span(gen_ai__tool__name="echo", jsonrpc__request__id="1", mcp__request__argument__text="hi")
    assert mc.check_conventions(result([ok], [call()])) == []


def test_coverage_unspanned_methods():
    ping = {"label": "ping", "method": "ping", "target": None, "args": {}}
    r = result([GOOD], [call()])
    r["truth"]["client"].append(ping)
    assert rules(mc.check_coverage(r), "") == ["mcp.coverage.unspanned"]
    assert mc.check_coverage(result([GOOD], [call()])) == []
