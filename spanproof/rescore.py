"""Re-apply the current checks to stored results (no scenarios are re-run).

    python -m spanproof.rescore results/full_head.json [more.json ...]
"""

from __future__ import annotations

import json
import sys

from . import oracles


def rescore(path: str) -> tuple[int, int]:
    d = json.load(open(path))
    results = d.get("results") or [r for c in d.get("cells", []) for r in c.get("results", [])]
    before = after = 0
    for r in results:
        if r.get("crashed") or r.get("unsupported") or "spans" not in r:
            continue
        before += len(r.get("findings", []))
        r["findings"] = oracles.run_all(r)
        for f in r["findings"]:
            f["mode"] = r.get("mode", "default")
        after += len(r["findings"])
    json.dump(d, open(path, "w"), indent=1, default=str)
    return before, after


def main(argv=None) -> int:
    for p in argv or sys.argv[1:]:
        b, a = rescore(p)
        print(f"{p}: {b} -> {a} findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
