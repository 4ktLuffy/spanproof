"""Version matrix: build one venv per (integration, provider version) and run its scenarios.

    python -m spanproof.matrix --sdk ~/spanproof-work/sentry-python --envs ~/spanproof-work/envs \
        [--only openai,litellm] [--out results/matrix.json]

Versions mirror sentry-python's own tox.ini test matrix (oldest supported, middle,
newest pinned) plus "latest" and "pre" (pre-releases allowed), so a provider release
that breaks instrumentation shows up the night it is published.
"""

from __future__ import annotations

import argparse
import shutil
import re
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .runner import list_scenarios, run_one

UV = os.environ.get("UV") or shutil.which("uv") or str(Path.home() / ".local/bin/uv")

# tox env name -> (scenario prefix, distribution, extra packages for scenarios)
TOX_ENVS = {
    "openai-base": ("openai.", "openai", []),
    "anthropic": ("anthropic.", "anthropic", []),
    "litellm": ("litellm.", "litellm", []),
    "google_genai": ("google_genai.", "google-genai", []),
    "openai_agents": ("openai_agents.", "openai-agents", []),
    "pydantic_ai": ("pydantic_ai.", "pydantic-ai-slim[openai]", []),
    "langchain-base": ("langchain.", "langchain", ["langchain-openai"]),
    "langgraph": ("langgraph.", "langgraph", ["langchain-openai"]),
}
SKIP_PINS = re.compile(r"^(pytest|pytest-|sentry-sdk|coverage|tomli|iniconfig|pluggy|exceptiongroup|colorama)", re.I)


def tox_cells(tox_ini: str) -> list[dict]:
    """Every (env, version) in sentry-python's tox.ini, with the python and pins tox uses."""
    text = open(tox_ini).read()
    cells = []
    for env, (prefix, dist, extras) in TOX_ENVS.items():
        for m in re.finditer(r"^\s*\{([^}]+)\}-" + re.escape(env) + r"-v([0-9.]+)\s*$", text, re.M):
            pys = [p.replace("py", "") for p in m.group(1).split(",") if not p.endswith("t")]
            ver = m.group(2)
            pick = next((p for p in ("3.12", "3.13", "3.11", "3.10", "3.9") if p in pys), pys[-1])
            pins = []
            for line in re.finditer(r"^\s*(?:py" + re.escape(pick) + r"-)?" + re.escape(env) + r"-v" +
                                    re.escape(ver) + r":\s*(.+?)\s*$", text, re.M):
                pkg = line.group(1).split(" #", 1)[0].split("\t#", 1)[0].strip()  # drop trailing comments
                if pkg and not pkg.startswith("#") and not SKIP_PINS.match(pkg):
                    pins.append(pkg)
            base = dist.split("[")[0].lower().replace("_", "-")
            if not any(re.match(re.escape(base) + r"(\[[^\]]*\])?\s*==", p.lower().replace("_", "-"))
                       or p.lower().startswith(base.split("-")[0] + "==") for p in pins):
                raise ValueError(f"tox.ini cell {env}-v{ver} has no pin for {dist}; refusing to guess")
            cells.append({"env": env, "prefix": prefix, "dist": dist, "requested": ver, "python": pick,
                          "pins": pins, "extras": extras})
        for tag in ("latest", "pre"):
            cells.append({"env": env, "prefix": prefix, "dist": dist, "requested": tag, "python": "3.12",
                          "pins": [dist], "extras": extras})
    return cells


def build_env(envs: Path, sdk: str, cell: dict) -> tuple[str | None, str]:
    name = f"{cell['env']}-{cell['requested']}-py{cell['python']}"
    venv = envs / name
    py = venv / "bin" / "python"
    if not py.exists():
        p = subprocess.run([UV, "venv", "-q", "-p", cell["python"], str(venv)], capture_output=True, text=True)
        if p.returncode != 0:
            return None, p.stderr[-800:]
    args = [UV, "pip", "install", "-q", "-p", str(py), "-e", sdk, *cell["pins"], *cell["extras"]]
    if cell["requested"] == "pre":
        args += ["--prerelease", "allow", "--upgrade-package", cell["dist"].split("[")[0]]
    elif cell["requested"] == "latest":
        args += ["--upgrade-package", cell["dist"].split("[")[0]]
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        return None, p.stderr[-1500:]
    return str(py), ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdk", required=True, help="sentry-python checkout (its tox.ini defines the matrix)")
    ap.add_argument("--envs", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--modes", default="default")
    ap.add_argument("--out", default="results/matrix.json")
    ap.add_argument("--slim", action="store_true",
                    help="per integration only the oldest supported version, latest and pre-release (nightly)")
    ap.add_argument("--resume", action="store_true",
                    help="keep cells from --out that installed and ran cleanly; redo the rest")
    a = ap.parse_args(argv)
    envs = Path(a.envs).expanduser()
    envs.mkdir(parents=True, exist_ok=True)

    plan = [c for c in tox_cells(os.path.join(os.path.expanduser(a.sdk), "tox.ini"))
            if not a.only or c["env"].split("-")[0] in a.only.split(",")]
    if a.slim:
        def ver(c):
            return tuple(int(x) for x in c["requested"].split(".") if x.isdigit())

        oldest = {}
        for c in plan:
            if c["requested"] not in ("latest", "pre"):
                if c["env"] not in oldest or ver(c) < ver(oldest[c["env"]]):
                    oldest[c["env"]] = c
        plan = [c for c in plan if c["requested"] in ("latest", "pre") or oldest.get(c["env"]) is c]

    done = {}
    if a.resume and Path(a.out).exists():
        for c in json.load(open(a.out))["cells"]:
            if c.get("results") and not c.get("install_error") and not any(r.get("crashed") for r in c["results"]):
                done[(c["env"], c["requested"])] = c

    def do_cell(cell):
        if (cell["env"], cell["requested"]) in done:
            return done[(cell["env"], cell["requested"])]
        t0 = time.time()
        py, err = build_env(envs, os.path.expanduser(a.sdk), cell)
        cell["results"] = []
        if py is None:
            cell["install_error"] = err
            print(f"[install-fail] {cell['env']} {cell['requested']}: {err[-200:]}", flush=True)
            return cell
        ids = [s for s in list_scenarios(py) if s.startswith(cell["prefix"])]
        jobs = [(sid, m) for sid in ids for m in a.modes.split(",")]
        with ThreadPoolExecutor(4) as ex:
            cell["results"] = list(ex.map(lambda j: run_one(py, *j), jobs))
        got = next((r.get("versions", {}).get(cell["dist"].split("[")[0]) for r in cell["results"]
                    if r.get("versions")), None)
        cell["resolved"] = got
        if cell["requested"] not in ("latest", "pre") and got and got != cell["requested"]:
            cell["version_mismatch"] = f"requested {cell['requested']}, installed {got}"
            print(f"[version-mismatch] {cell['env']}: {cell['version_mismatch']}", flush=True)
        nf = sum(1 for r in cell["results"] for f in r.get("findings", []) if f["severity"] != "low")
        nu = sum(1 for r in cell["results"] if r.get("unsupported"))
        nc = sum(1 for r in cell["results"] if r.get("crashed"))
        print(f"{cell['env']} {cell['requested']} (py{cell['python']}) -> {got}: {len(ids)} scenarios, "
              f"{nf} med/high findings, {nu} unsupported, {nc} crashed ({time.time() - t0:.0f}s)", flush=True)
        return cell

    with ThreadPoolExecutor(int(os.environ.get("SPANPROOF_CELL_JOBS", "3"))) as ex:
        cells = list(ex.map(do_cell, plan))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "cells": cells}, open(out, "w"), default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
