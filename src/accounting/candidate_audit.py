"""Reusable audit of variation-candidate records — duplication & qualifier-mismatch
detection, shared by the API quality KPIs and the `auditing-match-quality` skill.

A "record" is any mapping with ``raw_text`` / ``matched_to`` / ``document_id`` keys —
a JSONL line *or* a DB row mapped to that shape — so one analyzer serves both consumers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


def audit_records(records: Iterable[dict]) -> dict:
    """Duplication / provenance health of a candidate set.

    Returns ``total``, ``unique_pairs``, ``duplication_ratio`` (``1 − unique/total`` —
    how much of the log is redundant), and ``empty_document_id_pct`` (rows with no source
    document, which carry no promotion evidence).
    """
    total = 0
    pairs: set[tuple] = set()
    empty_doc = 0
    for r in records:
        total += 1
        pairs.add((r.get("raw_text"), r.get("matched_to")))
        if not (r.get("document_id") or ""):
            empty_doc += 1
    unique = len(pairs)
    return {
        "total": total,
        "unique_pairs": unique,
        "duplication_ratio": round(1 - unique / total, 4) if total else 0.0,
        "empty_document_id_pct": round(100 * empty_doc / total, 2) if total else 0.0,
    }


def qualifier_mismatches(records: Iterable[dict]) -> list[dict]:
    """Distinct ``(raw_text → matched_to)`` pairs whose discriminative-qualifier sets
    differ — the residual/total false-positive class the matcher guard now rejects.

    Surfaces legacy or externally-sourced mismatches (e.g. ``PRODUITS D'EXPLOITATION`` →
    ``Autres produits d'exploitation``) still sitting in a candidate log.
    """
    from accounting.nomenclature import _normalize, _qualifier_set

    seen: set[tuple] = set()
    out: list[dict] = []
    for r in records:
        raw, matched = r.get("raw_text"), r.get("matched_to")
        if not raw or not matched:
            continue
        key = (raw, matched)
        if key in seen:
            continue
        seen.add(key)
        if _qualifier_set(_normalize(raw)) != _qualifier_set(_normalize(matched)):
            out.append({"raw_text": raw, "matched_to": matched})
    return out


def read_jsonl(path) -> list[dict]:
    """Load a variation-candidate JSONL file into a list of records (tolerates bad lines)."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def audit_file(path) -> dict:
    """Full report for a JSONL candidate log: health metrics + qualifier-mismatch list."""
    records = read_jsonl(path)
    report = audit_records(records)
    report["qualifier_mismatches"] = qualifier_mismatches(records)
    return report
