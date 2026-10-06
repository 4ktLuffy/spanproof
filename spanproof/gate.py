"""CI gate: compare a SpanProof run with a baseline of known findings.

    python -m spanproof.gate results/run.json --baseline spanproof-baseline.json [--summary out.md]
    python -m spanproof.gate results/run.json --write-baseline spanproof-baseline.json

Exit codes:
  0  every finding is known (or none at all), and everything that should have run did run
  1  a finding appeared that the baseline does not know
  2  the run is not trustworthy: a scenario crashed, an environment failed to install, or
     nothing ran. A broken run must never look green.
Known findings keep CI green; a known finding whose scenario ran cleanly and no longer
produces it is reported as fixed, so the baseline can shrink. A finding whose scenario
did not run is neither fixed nor new: it is reported as unchecked.
Baseline entries may carry "until": "YYYY-MM-DD"; after that date they stop suppressing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

MIN_SEVERITY = {"low": 0, "medium": 1, "high": 2}


def _cell(r: dict) -> str:
    return r.get("_cell", "-")


def signature(f: dict, cell: str = "-") -> str:
    """Stable identity of a finding: the rule that fired, where, and on which attribute and call."""
    rule = f.get("rule") or f["check"]
    return f"{cell}|{f['scenario']}|{f.get('mode', 'default')}|{rule}|{f.get('attribute')}|{f.get('call')}"


def results_of(run: dict) -> tuple[list[dict], list[str]]:
    """All results (tagged with their matrix cell) and a list of execution problems."""
    problems, out = [], []
    if "cells" in run:
        for c in run["cells"]:
            cell = f"{c.get('env')}@{c.get('requested')}"
            if c.get("install_error"):
                problems.append(f"{cell}: environment failed to install")
                continue
            if not c.get("results"):
                problems.append(f"{cell}: no scenarios ran")
            if c.get("version_mismatch"):
                problems.append(f"{cell}: {c['version_mismatch']}")
            for r in c.get("results", []):
                out.append(dict(r, _cell=cell))
    else:
        out = list(run.get("results", []))
    for r in out:
        if r.get("crashed"):
            problems.append(f"{_cell(r)} {r.get('scenario')} [{r.get('mode')}]: worker crashed")
    if not out:
        problems.append("the run contains no results")
    return out, problems


def findings(results: list[dict], min_sev: str) -> tuple[dict[str, dict], set[str]]:
    found, ran = {}, set()
    for r in results:
        if r.get("crashed") or r.get("unsupported"):  # unsupported: the library lacks the API, nothing was checked
            continue
        ran.add(f"{_cell(r)}|{r.get('scenario')}|{r.get('mode', 'default')}")
        for f in r.get("findings", []):
            if MIN_SEVERITY.get(f["severity"], 0) >= MIN_SEVERITY[min_sev]:
                found[signature(f, _cell(r))] = f
    return found, ran


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--baseline")
    ap.add_argument("--write-baseline")
    ap.add_argument("--min-severity", default="medium", choices=list(MIN_SEVERITY))
    ap.add_argument("--summary", help="write a Markdown summary (e.g. $GITHUB_STEP_SUMMARY)")
    a = ap.parse_args(argv)
    results, problems = results_of(json.load(open(a.run)))
    now, ran = findings(results, a.min_severity)

    if a.write_baseline:
        if problems:
            print("refusing to write a baseline from an unhealthy run:\n  " + "\n  ".join(problems))
            return 2
        json.dump({"generated": dt.date.today().isoformat(),
                   "known": {k: {"message": v["message"], "until": None, "ref": None} for k, v in sorted(now.items())}},
                  open(a.write_baseline, "w"), indent=1)
        print(f"wrote {len(now)} known findings to {a.write_baseline}")
        return 0

    known = json.load(open(a.baseline))["known"] if a.baseline else {}
    today = dt.date.today().isoformat()
    active = {k: v for k, v in known.items() if not v.get("until") or v["until"] >= today}
    new = {k: v for k, v in now.items() if k not in active}
    scope = lambda k: "|".join(k.split("|")[:3])  # noqa: E731  cell|scenario|mode
    fixed = [k for k in active if k not in now and scope(k) in ran]
    unchecked = [k for k in active if k not in now and scope(k) not in ran]
    expired = [k for k, v in known.items() if v.get("until") and v["until"] < today and k in now]

    lines = [f"## SpanProof: {len(new)} new, {len(fixed)} fixed, {len(now) - len(new)} known, "
             f"{len(unchecked)} unchecked"]
    if problems:
        lines.append("\n### Run problems (failing)")
        lines += [f"- {p}" for p in problems]
    if new:
        lines.append("\n### New (failing)")
        for k, f in sorted(new.items()):
            lines.append(f"- `{f['scenario']}` [{f['severity']}] {f.get('rule', f['check'])}: {f['message']}")
    if fixed:
        lines.append("\n### Fixed (remove from the baseline)")
        lines += [f"- {known[k]['message']}  \n  `{k}`" for k in sorted(fixed)]
    if unchecked:
        lines.append(f"\n{len(unchecked)} known finding(s) were not checked because their scenario did not run.")
    if expired:
        lines.append(f"\n{len(expired)} baseline entr{'y' if len(expired) == 1 else 'ies'} expired and now fail.")
    text = "\n".join(lines)
    print(text)
    if a.summary:
        with open(a.summary, "a") as fh:
            fh.write(text + "\n")
    if problems:
        return 2
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
