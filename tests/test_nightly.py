"""Nightly page: catalog mapping, new/fixed only where coverage allows."""

import json

from spanproof import nightly


def finding(scenario, integration, rule, sev="high"):
    return {"scenario": scenario, "integration": integration, "rule": rule, "check": rule.split(".")[0],
            "severity": sev, "message": "m"}


def matrix(cells):
    return {"cells": [{"env": env, "requested": req, "resolved": req, "results": [
        {"scenario": sc, "integration": integ, "findings": [finding(sc, integ, rule) for rule in rules]}
        for sc, integ, rules in runs]} for env, req, runs in cells]}


def run(tmp_path, m, date):
    p = tmp_path / f"m{date}.json"
    p.write_text(json.dumps(m))
    nightly.main(["--matrix", str(p), "--site", str(tmp_path / "site"), "--date", date])
    return json.load(open(tmp_path / "site" / "history.json"))[-1]


def test_mapping():
    assert nightly.catalog_id("litellm", "litellm.completion.openai", "usage.missing.cached") == "SP-01"
    assert nightly.catalog_id("openai", "openai.chat.sync", "usage.missing.cached", "openai-base@1.0.1") == "OLD-SDK"
    assert nightly.catalog_id("openai", "x", "something.unknown") is None


def test_new_fixed_and_coverage(tmp_path):
    lit = ("litellm", "1.104.0", [("litellm.completion.openai", "litellm", ["usage.missing.cached"])])
    oai = ("openai-base", "2.54.0", [("openai.chat.stream.early_close", "openai", ["lifecycle.lost_span"])])
    n1 = run(tmp_path, matrix([lit, oai]), "2026-10-07")
    assert set(n1["problems"]) == {"SP-01", "SP-02"} and n1["new"] == []
    # night 2 only runs openai, and SP-02 is gone: SP-02 fixed, SP-01 NOT fixed (litellm did not run)
    oai_ok = ("openai-base", "2.54.0", [("openai.chat.stream.early_close", "openai", [])])
    n2 = run(tmp_path, matrix([oai_ok]), "2026-10-08")
    assert n2["fixed"] == ["SP-02"]
    # night 3: an unknown problem appears
    weird = ("openai-base", "2.54.0", [("openai.chat.sync", "openai", ["usage.wrong.input_tokens"])])
    n3 = run(tmp_path, matrix([weird]), "2026-10-09")
    assert n3["new"] == ["new:openai:usage.wrong.input_tokens"]
    assert (tmp_path / "site" / "new.md").exists()
