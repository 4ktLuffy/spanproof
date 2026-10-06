"""Negative controls for `Scenario.raises`: without Sentry, the client library itself raises there.

An `errors.swallowed` finding is only meaningful if the same call raises when Sentry is not loaded,
so each scenario that declares `raises` is replayed here in a fresh interpreter without sentry_sdk.init.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from spanproof.scenario import load_all

ROOT = Path(__file__).resolve().parent.parent
CODE = """
import sys
from spanproof.mockserver import MockServer
from spanproof.scenario import load_all
sc = load_all()[sys.argv[1]]
with MockServer(sc.replies()) as srv:
    try:
        sc.run(srv.url)
        print("@@RAISED@@none")
    except BaseException as e:
        print("@@RAISED@@" + type(e).__name__)
"""
SCENARIOS = sorted(s.id for s in load_all().values() if s.raises)


@pytest.mark.parametrize("sid", SCENARIOS or ["none"])
def test_client_raises_without_sentry(sid):
    if sid == "none":
        pytest.skip("no installed provider has a scenario with `raises`")
    p = subprocess.run([sys.executable, "-c", CODE, sid], cwd=ROOT, capture_output=True, text=True, timeout=60)
    got = p.stdout.rsplit("@@RAISED@@", 1)[-1].strip()
    assert got == load_all()[sid].raises.rsplit(".", 1)[-1], p.stderr[-800:]
