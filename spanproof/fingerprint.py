"""Simulate the proposed privacy-preserving fingerprints on a corpus.

Takes a corpus recorded WITH data collection, replaces every content attribute by
what an SDK could safely send with data collection OFF, and writes a new corpus:
  - content attributes removed (messages, tool arguments, tool results, outputs)
  - gen_ai.tool.call.arguments_hash = sha256(salt + canonical JSON of the arguments)
  - gen_ai.response.text_length = character count of the response text (no content)

    python -m spanproof.fingerprint results/corpus_dc_on.jsonl results/corpus_fp.jsonl
"""

from __future__ import annotations

import hashlib
import json
import sys

from .conventions import CONTENT_KEYS
from .detectors import ARGS_HASH, OUTPUT_CHARS, _output_text, _texts

SALT = b"per-project-salt"  # an SDK would use a per-DSN secret so hashes cannot be brute-forced across orgs


def canon(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return v.strip()
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def transform(row: dict) -> dict:
    for s in row.get("spans", []):
        d = s.get("data", {})
        out = _output_text(s)
        if out is not None:
            d[OUTPUT_CHARS] = len(_texts(out).strip())
        args = d.get("gen_ai.tool.call.arguments", d.get("gen_ai.tool.input"))
        if args is not None:
            d[ARGS_HASH] = hashlib.sha256(SALT + canon(args).encode()).hexdigest()[:16]
        for k in list(d):
            if k in CONTENT_KEYS or k.startswith(("gen_ai.request.messages", "gen_ai.input.messages")):
                del d[k]
    row["data_collection"] = False
    return row


def main(argv=None) -> int:
    src, dst = (argv or sys.argv[1:])[:2]
    with open(dst, "w") as out:
        for line in open(src):
            if line.strip():
                out.write(json.dumps(transform(json.loads(line))) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
