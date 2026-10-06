"""Run the MCP suite over interpreters (one per mcp / fastmcp version), flavors, transports and modes.

    python -m spanproof.mcp_run --py .venv/bin/python --py .envs/mcp-1.15.0/bin/python ... \
        [--flavors lowlevel,mcpserver,fastmcp] [--transports memory,stdio,http,http-stateless,sse]
        [--modes default,nodc,noprompts,dcoff,dcout,legacy,stream] [--out results/mcp_run.json]

Each cell runs `spanproof.mcp_worker` in a fresh interpreter. A flavor the interpreter lacks
(no fastmcp installed, say) is skipped, not failed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from . import mcp_checks
from .worker import MARK


def has(py: str, mod: str) -> bool:
    return subprocess.run([py, "-c", f"import {mod}"], capture_output=True).returncode == 0


def run_cell(py, flavor, transport, mode, timeout=240):
    t0 = time.time()
    try:
        p = subprocess.run([py, "-m", "spanproof.mcp_worker", flavor, transport, "--mode", mode],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"cell": [py, flavor, transport, mode], "error": "timeout"}
    if MARK not in p.stdout:
        return {"cell": [py, flavor, transport, mode], "error": p.stderr[-2000:]}
    r = json.loads(p.stdout.split(MARK, 1)[1])
    r["seconds"] = round(time.time() - t0, 1)
    r["findings"] = mcp_checks.run_all(r)
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--py", action="append", required=True)
    ap.add_argument("--flavors", default="lowlevel,mcpserver,fastmcp")
    ap.add_argument("--transports", default="memory,stdio,http,http-stateless,sse")
    ap.add_argument("--modes", default="default,nodc,noprompts,dcoff,dcout,legacy,stream")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", default="results/mcp_run.json")
    a = ap.parse_args(argv)
    cells = []
    for py in a.py:
        for fl in a.flavors.split(","):
            mod = {"lowlevel": "mcp.server.lowlevel", "mcpserver": "mcp.server.mcpserver" if has(py, "mcp.server.mcpserver")
                   else "mcp.server.fastmcp", "fastmcp": "fastmcp"}[fl]
            if not has(py, mod):
                continue
            cells += [(py, fl, t, m) for t in a.transports.split(",") for m in a.modes.split(",")]
    with ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(lambda c: run_cell(*c), cells))
    # raw results only: findings are recomputed from them (python -m spanproof.mcp_checks <file>)
    slim = [{k: v for k, v in r.items() if k != "findings"} for r in results]
    json.dump({"results": slim}, open(a.out, "w"), separators=(",", ":"), default=str)
    bad = [r for r in results if "error" in r]
    print(f"{len(results)} cells, {len(bad)} failed to run")
    for r in bad:
        print("  FAILED", r["cell"], r["error"][-300:])
    return mcp_checks.main([a.out])


if __name__ == "__main__":
    raise SystemExit(main())
