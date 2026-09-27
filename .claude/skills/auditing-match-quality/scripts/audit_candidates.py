#!/usr/bin/env python
"""Audit a variation-candidate JSONL for duplication and qualifier mismatches.

Thin CLI over the shared analyzer (``src/accounting/candidate_audit.py``) so the skill and
the API report the *same* numbers. Doubles as a CI gate: exits non-zero when the duplication
ratio exceeds ``--max-dup`` or any qualifier mismatch is found.

Usage:
    python audit_candidates.py [path-to.jsonl] [--max-dup 0.5]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo-local skill: scripts/ -> skill -> skills -> .claude -> repo root.
REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

from accounting.candidate_audit import audit_file  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?",
                    default=str(REPO / "data" / "variation_candidates.jsonl"))
    ap.add_argument("--max-dup", type=float, default=0.5,
                    help="fail if duplication_ratio exceeds this (default 0.5)")
    args = ap.parse_args()

    report = audit_file(args.path)
    metrics = {k: v for k, v in report.items() if k != "qualifier_mismatches"}
    print(json.dumps(metrics, indent=2))

    mismatches = report["qualifier_mismatches"]
    if mismatches:
        print(f"\n{len(mismatches)} qualifier mismatch(es) — residual/total false matches:")
        for m in mismatches[:20]:
            print(f"  {m['raw_text']!r} -> {m['matched_to']!r}")
        if len(mismatches) > 20:
            print(f"  ... and {len(mismatches) - 20} more")

    failed = report["duplication_ratio"] > args.max_dup or bool(mismatches)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
