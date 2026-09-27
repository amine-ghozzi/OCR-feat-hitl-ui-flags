"""candidate_audit — duplication / provenance health + qualifier-mismatch detection (Layer 3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from accounting.candidate_audit import (  # noqa: E402
    audit_file,
    audit_records,
    qualifier_mismatches,
)


def test_audit_records_duplication_and_empty_ids():
    records = [
        {"raw_text": "CA", "matched_to": "Revenus", "document_id": "d1"},
        {"raw_text": "CA", "matched_to": "Revenus", "document_id": "d1"},  # dup pair
        {"raw_text": "Stocks", "matched_to": "Stocks", "document_id": ""},  # empty id
        {"raw_text": "Stocks", "matched_to": "Stocks", "document_id": ""},  # dup + empty
    ]
    rep = audit_records(records)
    assert rep["total"] == 4
    assert rep["unique_pairs"] == 2
    assert rep["duplication_ratio"] == 0.5            # 1 - 2/4
    assert rep["empty_document_id_pct"] == 50.0


def test_audit_records_empty_input():
    rep = audit_records([])
    assert rep == {"total": 0, "unique_pairs": 0, "duplication_ratio": 0.0,
                   "empty_document_id_pct": 0.0}


def test_qualifier_mismatch_flags_residual_false_match():
    records = [
        {"raw_text": "PRODUITS D'EXPLOITATION", "matched_to": "Autres produits d'exploitation"},
        {"raw_text": "Réserve", "matched_to": "Réserves"},                 # ok (no qualifier)
        {"raw_text": "CAPITAUX PROPRES ET PASSIFS",
         "matched_to": "Total des capitaux propres et des passifs"},        # ok? total vs none → flag
    ]
    flagged = {m["raw_text"] for m in qualifier_mismatches(records)}
    assert "PRODUITS D'EXPLOITATION" in flagged           # residual mismatch flagged
    assert "CAPITAUX PROPRES ET PASSIFS" in flagged       # total-vs-bare mismatch flagged
    assert "Réserve" not in flagged                        # legitimate match not flagged


def test_qualifier_mismatch_dedups_pairs():
    records = [{"raw_text": "X", "matched_to": "Autres X"}] * 5
    assert len(qualifier_mismatches(records)) == 1        # distinct pairs only


def test_audit_file_roundtrip(tmp_path):
    log = tmp_path / "c.jsonl"
    rows = [
        {"raw_text": "CA", "matched_to": "Revenus", "document_id": "d1"},
        {"raw_text": "PRODUITS D'EXPLOITATION", "matched_to": "Autres produits d'exploitation",
         "document_id": "d1"},
    ]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    rep = audit_file(log)
    assert rep["total"] == 2 and rep["unique_pairs"] == 2
    assert any(m["raw_text"] == "PRODUITS D'EXPLOITATION" for m in rep["qualifier_mismatches"])
