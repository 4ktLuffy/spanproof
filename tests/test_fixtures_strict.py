"""Every advanced-path fixture parses with the provider SDK's own types, with no undeclared fields.

A fixture that drifts from the real schema (or misspells a usage key the lenient SDK models would
quietly keep as an extra) fails here instead of producing a false finding.
"""

import pytest

from spanproof import fixtures as fx

anthropic_adv = pytest.importorskip("spanproof.scenarios.anthropic_adv_sc")
openai_adv = pytest.importorskip("spanproof.scenarios.openai_adv_sc")

ALL = [(f"anthropic-{i}", k, p) for i, (k, p) in enumerate(anthropic_adv.FIXTURES)] + \
      [(f"openai-{i}", k, p) for i, (k, p) in enumerate(openai_adv.FIXTURES)]


@pytest.mark.parametrize("name,kind,payload", ALL, ids=[a[0] for a in ALL])
def test_fixture_is_valid_and_declared(name, kind, payload):
    fx.validate(kind, payload, strict=True)


def test_strict_rejects_misspelled_usage_key():
    body, _ = fx.anthropic_message_x(content=[{"type": "text", "text": "x"}],
                                     usage_extra={"server_tool_usage": {"web_search_requests": 1}})
    fx.validate("anthropic_message", body)  # the lenient parse accepts it ...
    with pytest.raises(ValueError, match="server_tool_usage"):
        fx.validate("anthropic_message", body, strict=True)  # ... strict does not


def test_strict_rejects_wrong_type():
    body, _ = fx.openai_response_x(tool_items=[{"id": "ws", "type": "web_search_call", "status": "done",
                                                "action": {"type": "search", "query": "q"}}])
    with pytest.raises(Exception):
        fx.validate("openai_response", body, strict=True)


def test_existing_fixtures_still_validate():
    body, _ = fx.anthropic_message()
    fx.validate("anthropic_message", body)
    events, _, _ = fx.openai_response_stream()
    fx.validate("openai_response_events", events)
