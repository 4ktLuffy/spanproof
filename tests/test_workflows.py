"""Every GitHub workflow file must parse, and the nightly one must keep its triggers."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_workflows_parse_and_nightly_triggers():
    files = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert files
    for f in files:
        doc = yaml.safe_load(f.read_text())
        assert isinstance(doc, dict) and doc.get("jobs"), f
    nightly = yaml.safe_load((ROOT / ".github" / "workflows" / "nightly.yml").read_text())
    triggers = nightly.get("on", nightly.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert "schedule" in triggers and "workflow_dispatch" in triggers
