"""ISSUE-10 candidate-log hygiene (reversible; operates on the untracked learning log).

Two faithful, matcher-consistent operations:
  1. Drop status=="candidate" rows whose (raw -> matched) is a *current* qualifier
     mismatch — the matcher guard now rejects these, so they are provably stale and
     carry no promotion evidence (all have empty document_id). Approved/HITL/promoted
     rows are preserved even if they look like a mismatch (human overrides).
  2. Dedup the remainder by the logger's own _dedup_key (raw, matched, document_id,
     status), keeping the first occurrence — exactly what the now-guarded logger
     would have produced.

Usage: hygiene_candidate_log.py [--apply]   (default: dry-run report only)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting import candidate_audit  # noqa: E402
from accounting.variation_logger import VariationLogger, resolve_log_path  # noqa: E402
from accounting.nomenclature import _normalize, _qualifier_set  # noqa: E402


def is_current_mismatch(raw: str, matched: str) -> bool:
    if not raw or not matched:
        return False
    return _qualifier_set(_normalize(raw)) != _qualifier_set(_normalize(matched))


def main(apply: bool) -> None:
    # Resolved here, not at import: FINALZE_VARIATION_LOG is read when the script runs.
    log = resolve_log_path()
    records = candidate_audit.read_jsonl(log)
    before = candidate_audit.audit_records(records)
    before_mm = candidate_audit.qualifier_mismatches(records)

    kept: list[dict] = []
    seen: set[tuple] = set()
    purged_mismatch = 0
    dropped_dup = 0
    for r in records:
        raw, matched, status = r.get("raw_text"), r.get("matched_to"), r.get("status")
        if status == "candidate" and is_current_mismatch(raw, matched):
            purged_mismatch += 1
            continue
        key = VariationLogger._dedup_key(r)
        if key in seen:
            dropped_dup += 1
            continue
        seen.add(key)
        kept.append(r)

    after = candidate_audit.audit_records(kept)
    after_mm = candidate_audit.qualifier_mismatches(kept)

    print("=== BEFORE ===")
    print(f"  total={before['total']} unique_pairs={before['unique_pairs']} "
          f"dup_ratio={before['duplication_ratio']} "
          f"empty_doc%={before['empty_document_id_pct']} mismatches={len(before_mm)}")
    for m in before_mm:
        print(f"    mismatch: {m['raw_text']!r} -> {m['matched_to']!r}")
    print("=== ACTIONS ===")
    print(f"  purged matcher-rejected candidate mismatches: {purged_mismatch}")
    print(f"  dropped exact _dedup_key duplicates:          {dropped_dup}")
    print("=== AFTER ===")
    print(f"  total={after['total']} unique_pairs={after['unique_pairs']} "
          f"dup_ratio={after['duplication_ratio']} "
          f"empty_doc%={after['empty_document_id_pct']} mismatches={len(after_mm)}")
    for m in after_mm:
        print(f"    REMAINING mismatch: {m['raw_text']!r} -> {m['matched_to']!r}")

    if apply:
        import json
        with open(log, "w", encoding="utf-8") as fh:
            for r in kept:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"=== WROTE {len(kept)} records to {log} ===")
    else:
        print("=== DRY RUN (no write); pass --apply to write ===")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
