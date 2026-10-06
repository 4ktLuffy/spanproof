"""Scenario model and registry.

A scenario scripts the provider (replies), knows the ground truth of every
provider call it triggers (calls), names the Sentry integration under test, and
drives the client library (run). Scenarios execute in a fresh interpreter, one
per run, so integrations never leak state into each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .fixtures import Truth
from .mockserver import Reply

DUMMY_KEY = "spanproof-dummy-key"


@dataclass
class Call:
    """One provider request the scenario will make, and what it really cost."""

    truth: Truth | None  # None when the provider never reported usage (cut stream, error)
    op: str = "gen_ai.chat"
    completes: bool = True  # False when the call ends early (close, cut, error)


@dataclass
class Scenario:
    id: str
    integration: str  # sentry integration module, e.g. "openai"
    package: str  # pip distribution the integration instruments
    replies: Callable[[], list[Reply]]
    calls: list[Call]
    run: Callable[[str], None]  # gets the mock server base URL
    make_integrations: Callable[[], list]
    expects_exception: bool = False
    raises: str = ""  # exception the client library raises here without Sentry (tests/test_controls.py)
    agent: dict | None = None  # {"tools": [...], "agent_name": "..."} for agent scenarios
    tags: list[str] = field(default_factory=list)
    notes: str = ""


REGISTRY: dict[str, Scenario] = {}


def register(s: Scenario) -> Scenario:
    if s.id in REGISTRY:
        raise ValueError(f"duplicate scenario {s.id}")
    REGISTRY[s.id] = s
    return s


def load_all() -> dict[str, Scenario]:
    import importlib
    import pkgutil

    from . import scenarios

    for m in pkgutil.iter_modules(scenarios.__path__):
        try:
            importlib.import_module(f"{scenarios.__name__}.{m.name}")
        except ImportError:
            # provider package absent in this environment; its scenarios are skipped
            pass
    return REGISTRY
