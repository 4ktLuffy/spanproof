"""Run scenarios in fresh interpreters, apply the checks, write results.

    python -m spanproof.runner --python /path/to/venv/bin/python [--only openai.] [--modes default,nodc]
                               [--out results/run.json] [--jobs 6]
"""

from __future__ import annotations

import argparse
import json
import re
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import oracles

ROOT = Path(__file__).resolve().parent.parent

UNSUPPORTED = {"AttributeError", "ImportError", "ModuleNotFoundError", "TypeError", "NotImplementedError"}

MODES = {
    "default": [],
    "nodc": ["--no-data-collection"],
    "stream": ["--span-streaming"],
    "legacy": ["--legacy-transport"],
}


def list_scenarios(python: str) -> list[str]:
    code = "from spanproof.scenario import load_all; ids = sorted(load_all()); print('@@IDS@@'); print('\\n'.join(ids))"
    out = subprocess.run([python, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    return [x for x in out.stdout.rsplit("@@IDS@@", 1)[1].split() if x]


def raised_in_sentry_sdk(tb: str) -> bool:
    """True when the innermost traceback frame is Sentry's code (wrapper frames above it don't count)."""
    frames = re.findall(r'File "([^"]+)", line \d+', tb)
    return bool(frames) and "sentry_sdk" in frames[-1]


def run_one(python: str, sid: str, mode: str, timeout: int = 120) -> dict:
    t0 = time.time()
    p = subprocess.run([python, "-m", "spanproof.worker", sid, *MODES[mode]], cwd=ROOT, capture_output=True,
                       text=True, timeout=timeout)
    mark = "@@SPANPROOF-RESULT@@"
    if p.returncode != 0 or mark not in p.stdout:
        return {"scenario": sid, "mode": mode, "crashed": True, "stderr": p.stderr[-3000:],
                "seconds": round(time.time() - t0, 2)}
    r = json.loads(p.stdout.rsplit(mark, 1)[1])
    r["mode"] = mode
    r["seconds"] = round(time.time() - t0, 2)
    exc = r.get("exception") or {}
    if (exc and not r.get("requests") and exc.get("type") in UNSUPPORTED
            and not raised_in_sentry_sdk(exc.get("tb") or "")):
        # the client library at this version lacks the API the scenario uses. An error that
        # originates inside sentry_sdk is never treated this way: that would be an SDK bug.
        r["unsupported"] = True
        r["findings"] = []
        return r
    r["findings"] = oracles.run_all(r)
    for f in r["findings"]:
        f["mode"] = mode
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--only", default="")
    ap.add_argument("--modes", default="default,nodc")
    ap.add_argument("--out", default="results/run.json")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--label", default="")
    a = ap.parse_args(argv)

    sids = [s for s in list_scenarios(a.python) if s.startswith(tuple(a.only.split(",")))]
    jobs = [(sid, m) for sid in sids for m in a.modes.split(",")]
    with ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(lambda j: run_one(a.python, *j), jobs))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"label": a.label, "python": a.python, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "results": results}, open(out, "w"), indent=1, default=str)
    crashed = [r for r in results if r.get("crashed")]
    nfind = sum(len(r.get("findings", [])) for r in results)
    print(f"{len(results)} runs, {len(crashed)} crashed, {nfind} findings -> {out}")
    for r in crashed:
        print("CRASH", r["scenario"], r["mode"], r["stderr"][-400:])
    for r in results:
        for f in r.get("findings", []):
            print(f"  [{f['severity']:6}] {f['check']:11} {r['scenario']:40} {r['mode']:7} {f['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
