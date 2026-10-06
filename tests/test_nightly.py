"""Nightly page: catalog mapping; new/fixed per finding signature, only where the scenario ran; provenance."""

import json

from spanproof import nightly


def finding(scenario, integration, rule, sev="high"):
    return {"scenario": scenario, "integration": integration, "rule": rule, "check": rule.split(".")[0],
            "severity": sev, "message": "m"}


def matrix(cells):
    return {"cells": [{"env": env, "requested": req, "resolved": req, "results": [
        {"scenario": sc, "integration": integ, "findings": [finding(sc, integ, rule) for rule in rules]}
        for sc, integ, rules in runs]} for env, req, runs in cells]}


def run(tmp_path, m, date, sha="sdk1"):
    p = tmp_path / f"m{date}.json"
    p.write_text(json.dumps(m))
    nightly.main(["--matrix", str(p), "--site", str(tmp_path / "site"), "--date", date, "--sdk-sha", sha,
                  "--runner-sha", "runner1"])
    return json.load(open(tmp_path / "site" / "history.json"))[-1]


def sig(cell, scenario, rule, mode="default"):
    return f"{cell}|{scenario}|{mode}|{rule}|None|None"


LIT = "litellm.completion.openai"


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
    assert n2["fixed"] == [sig("openai-base@2.54.0", "openai.chat.stream.early_close", "lifecycle.lost_span")]
    assert n2["unknown"] == [sig("litellm@1.104.0", LIT, "usage.missing.cached")]
    # night 3: an uncatalogued problem appears
    weird = ("openai-base", "2.54.0", [("openai.chat.sync", "openai", ["usage.wrong.input_tokens"])])
    n3 = run(tmp_path, matrix([weird]), "2026-10-09")
    assert n3["new"] == [sig("openai-base@2.54.0", "openai.chat.sync", "usage.wrong.input_tokens")]
    assert "new:openai:usage.wrong.input_tokens" in n3["problems"]
    assert (tmp_path / "site" / "new.md").exists()
    # the litellm finding that did not run on night 2 is still carried, not silently dropped
    assert n3["unknown"] == [sig("litellm@1.104.0", LIT, "usage.missing.cached")]


def test_crashed_scenario_is_not_fixed(tmp_path):
    two = ("litellm", "1.104.0", [(LIT, "litellm", ["usage.missing.cached"]),
                                  ("litellm.completion.anthropic", "litellm", ["usage.missing.cached"])])
    run(tmp_path, matrix([two]), "2026-10-07")
    m = matrix([("litellm", "1.104.0", [(LIT, "litellm", []), ("litellm.completion.anthropic", "litellm", [])])])
    m["cells"][0]["results"][0] = {"scenario": LIT, "mode": "default", "crashed": True, "stderr": "boom"}
    n2 = run(tmp_path, m, "2026-10-08")
    # the integration ran (the other scenario passed), but the crashed scenario proves nothing
    assert n2["fixed"] == [sig("litellm@1.104.0", "litellm.completion.anthropic", "usage.missing.cached")]
    assert n2["unknown"] == [sig("litellm@1.104.0", LIT, "usage.missing.cached")]
    html = (tmp_path / "site" / "index.html").read_text()
    assert "Did not run tonight" in html and "litellm@1.104.0|litellm.completion.openai|default" in html
    # negative control: once it runs cleanly, it is fixed
    n3 = run(tmp_path, matrix([("litellm", "1.104.0", [(LIT, "litellm", [])])]), "2026-10-09")
    assert n3["fixed"] == [sig("litellm@1.104.0", LIT, "usage.missing.cached")] and n3["unknown"] == []


def test_install_failure_and_unsupported_are_not_fixed(tmp_path):
    run(tmp_path, matrix([("litellm", "1.104.0", [(LIT, "litellm", ["usage.missing.cached"])])]), "2026-10-07")
    m = {"cells": [{"env": "litellm", "requested": "1.104.0", "install_error": "no wheel", "results": []}]}
    assert run(tmp_path, m, "2026-10-08")["fixed"] == []
    m = matrix([("litellm", "1.104.0", [(LIT, "litellm", [])])])
    m["cells"][0]["results"][0]["unsupported"] = True
    n3 = run(tmp_path, m, "2026-10-09")
    assert n3["fixed"] == [] and n3["unknown"] == [sig("litellm@1.104.0", LIT, "usage.missing.cached")]


def test_new_failure_under_existing_id_is_new(tmp_path):
    one = ("litellm", "1.104.0", [(LIT, "litellm", ["usage.missing.cached"])])
    two = ("litellm", "1.104.0", [(LIT, "litellm", ["usage.missing.cached", "usage.missing.reasoning"])])
    run(tmp_path, matrix([one]), "2026-10-07")
    n2 = run(tmp_path, matrix([two]), "2026-10-08")
    assert n2["problems"] == ["SP-01"]  # same catalog id both nights ...
    assert n2["new"] == [sig("litellm@1.104.0", LIT, "usage.missing.reasoning")]  # ... yet a new defect
    assert "SP-01" in (tmp_path / "site" / "new.md").read_text()
    # negative control: an unchanged night reports nothing new
    n3 = run(tmp_path, matrix([two]), "2026-10-09")
    assert n3["new"] == [] and n3["fixed"] == [] and not (tmp_path / "site" / "new.md").exists()


def test_one_sub_finding_fixed_while_id_stays_open(tmp_path):
    two = ("litellm", "1.104.0", [(LIT, "litellm", ["usage.missing.cached", "usage.missing.reasoning"])])
    run(tmp_path, matrix([two]), "2026-10-07")
    n2 = run(tmp_path, matrix([("litellm", "1.104.0", [(LIT, "litellm", ["usage.missing.cached"])])]), "2026-10-08")
    assert n2["fixed"] == [sig("litellm@1.104.0", LIT, "usage.missing.reasoning")]
    assert n2["problems"] == ["SP-01"] and n2["new"] == []
    # negative control: the same disappearance with the scenario crashed is not a fix
    m = matrix([two])
    m["cells"][0]["results"][0] = {"scenario": LIT, "mode": "default", "crashed": True}
    run(tmp_path, matrix([two]), "2026-10-09")
    n4 = run(tmp_path, m, "2026-10-10")
    assert n4["fixed"] == [] and len(n4["unknown"]) == 2


def test_old_format_previous_night_loads(tmp_path):
    site = tmp_path / "site"
    (site / "nights").mkdir(parents=True)
    old = {"date": "2026-10-06", "sdk_sha": "6a4eb20f1db3de7b025bda6eedb73b9073cfbc91", "cells": ["litellm@1.104.0"],
           "problems": ["SP-01"], "new": [], "fixed": [], "crashes": [], "where": {"SP-01": ["litellm"]}}
    (site / "history.json").write_text(json.dumps([old]))
    (site / "nights" / "2026-10-06.json").write_text(json.dumps({"entry": old, "problems": {}, "cells": []}))
    m = matrix([("litellm", "1.104.0", [(LIT, "litellm", ["usage.missing.cached"])]),
                ("openai-base", "2.54.0", [("openai.chat.sync", "openai", ["usage.wrong.input_tokens"])])])
    n = run(tmp_path, m, "2026-10-07")
    assert n["baseline"] is None and n["fixed"] == [] and n["unknown"] == []
    # SP-01 existed under the old format, so it is not called new; the uncatalogued finding is
    assert n["new"] == [sig("openai-base@2.54.0", "openai.chat.sync", "usage.wrong.input_tokens")]
    html = (site / "index.html").read_text()
    assert "no finding-level baseline" in html and "package versions were not recorded" in html
    assert "2026-10-06" in html
    # negative control: the next night compares per finding
    n2 = run(tmp_path, matrix([("litellm", "1.104.0", [(LIT, "litellm", [])])]), "2026-10-08")
    assert n2["baseline"] == "2026-10-07" and n2["fixed"] == [sig("litellm@1.104.0", LIT, "usage.missing.cached")]


def test_provenance_diff_names_changed_packages(tmp_path):
    def night(oai, lit="1.104.0"):
        m = matrix([("openai-base", "latest", [("openai.chat.stream.early_close", "openai", ["lifecycle.lost_span"])]),
                    ("litellm", "1.104.0", [(LIT, "litellm", ["usage.missing.cached"])])])
        m["cells"][0]["resolved"] = oai
        m["cells"][0]["results"][0]["versions"] = {"sentry-sdk": "2.71.0", "openai": oai}
        m["cells"][1]["results"][0]["versions"] = {"sentry-sdk": "2.71.0", "litellm": lit}
        return m
    run(tmp_path, night("2.54.0"), "2026-10-07")
    n2 = run(tmp_path, night("2.55.0"), "2026-10-08", sha="sdk2")
    # a provider release does not rename the "latest" cell: the finding stays open, not new + fixed
    assert n2["new"] == [] and n2["fixed"] == []
    assert n2["provenance"]["packages"]["openai-base@latest"]["openai"] == "2.55.0"
    glob, cells = nightly.provenance_diff(json.load(open(tmp_path / "site" / "history.json"))[0]["provenance"],
                                          n2["provenance"])
    assert glob == ["sentry-python sdk1 -> sdk2"]
    assert cells == {"openai-base@latest": ["openai 2.54.0 -> 2.55.0"]}  # litellm unchanged: not listed
    html = (tmp_path / "site" / "index.html").read_text()
    assert "openai 2.54.0 -&gt; 2.55.0" in html and "not proof" in html
    # a verdict on the litellm cell names only the global change, not the openai bump
    n3 = run(tmp_path, night("2.55.0", lit="1.104.0"), "2026-10-09")
    m = night("2.55.0", lit="1.105.0")
    m["cells"][1]["results"][0]["findings"] = []
    run(tmp_path, m, "2026-10-10")
    html = (tmp_path / "site" / "index.html").read_text()
    row = next(r for r in html.split("<tr>") if "litellm@1.104.0|" in r or ">litellm@1.104.0<" in r)
    assert "litellm 1.104.0 -&gt; 1.105.0" in row and "openai 2.5" not in row
    assert n3["new"] == []


def test_issue_body_fits_github_even_when_everything_is_new(tmp_path):
    rules = [f"usage.wrong.input_tokens{i}" for i in range(30)]
    quiet = ("openai-base", "2.54.0", [("openai.chat.sync", "openai", [])])
    run(tmp_path, matrix([quiet]), "2026-10-07")
    loud = ("openai-base", "2.54.0", [(f"openai.chat.s{j}", "openai", rules) for j in range(40)])
    n = run(tmp_path, matrix([loud]), "2026-10-08")
    assert len(n["new"]) == 1200
    body = (tmp_path / "site" / "new.md").read_text()
    assert len(body) < 65536 and "1100 more new findings" in body  # 100 listed, the rest summarized


def test_double_count_keeps_its_own_label_in_without_langchain():
    sc = "js.langgraph.react_agent.without_langchain"
    assert nightly.catalog_id("js.langgraph", sc, "aggregation.double_count.input_tokens") == "SP-06"
    assert nightly.catalog_id("js.langgraph", sc, "lifecycle.duplicate_span") == "SP-43"
    assert nightly.catalog_id("js.langgraph", sc, "aggregation.filtered_mismatch.input_tokens") == "SP-43"
