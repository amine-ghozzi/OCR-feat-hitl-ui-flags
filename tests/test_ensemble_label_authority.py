"""
Tier-0 label authority + orphan suppression in the N-way ensemble.

Regression guard for the EF_CLB fragmentation bug: a native PDF yields a CLEAN
Tier-0 label keyspace, but the Tier-3 ensemble used to promote Tier-1 / docling
label FRAGMENTS ('is', 'Moins: Amortiss', 'Incorporel') as standalone output
rows, diluting and duplicating the clean labels so the benchmark's label-keyed
metric could no longer match.

Contract under test:
  1. When the anchor (highest-weight) source is 'tier0', non-anchor orphan rows
     are QUARANTINED into merged.metadata['unaligned_<source>'] — NOT promoted
     into merged.rows — so the scored keyspace stays clean.
  2. Aligned rows keep the Tier-0 raw_text label exactly (belt-and-suspenders).
  3. A NON-tier0 anchor still promotes orphans into rows (no behavior change).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable
from accounting.ensemble import EnsembleSource, ensemble_vote


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_phase5.py)
# ---------------------------------------------------------------------------


def _ncell(value, raw=None, conf: float = 1.0) -> CanonicalCell:
    return CanonicalCell(
        raw_value=raw if raw is not None else (str(value) if value is not None else ""),
        parsed_value=Decimal(str(value)) if value is not None else None,
        confidence=conf,
    )


def _row(raw, canonical=None, cells=None, conf=1.0) -> CanonicalRow:
    return CanonicalRow(
        raw_text=raw,
        canonical_term=canonical,
        match_type="exact" if canonical else "custom",
        match_confidence=conf if canonical else 0.0,
        cells=cells or {},
    )


def _tbl(rows) -> CanonicalTable:
    ct = CanonicalTable(rows=rows)
    ct.recompute_aggregates()
    return ct


def _src(name, weight, tables) -> EnsembleSource:
    return EnsembleSource(name=name, tables=tables, weight=weight)


# A clean Tier-0 table: three well-formed labels.
def _clean_tier0() -> CanonicalTable:
    return _tbl([
        _row("Immobilisations incorporelles", "Immobilisations incorporelles",
             {"value_n": _ncell(1000)}),
        _row("Immobilisations corporelles", "Immobilisations corporelles",
             {"value_n": _ncell(2000)}),
        _row("Stocks", "Stocks", {"value_n": _ncell(3000)}),
    ])


# A FRAGMENTED Tier-1 table: it re-detects the same first row (so it aligns),
# but emits extra fragment rows that align to NOTHING in the Tier-0 keyspace.
# (Labels chosen so the row-aligner leaves them as true b_orphans.)
_FRAGMENTS = ("zzz-frag-A", "zzz-frag-B", "zzz-frag-C")


def _fragmented_tier1() -> CanonicalTable:
    return _tbl([
        _row("Immobilisations incorporelles", "Immobilisations incorporelles",
             {"value_n": _ncell(1000)}),
        _row(_FRAGMENTS[0], canonical=None, cells={"value_n": _ncell(11)}),
        _row(_FRAGMENTS[1], canonical=None, cells={"value_n": _ncell(22)}),
        _row(_FRAGMENTS[2], canonical=None, cells={"value_n": _ncell(33)}),
    ])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTier0LabelAuthority:
    def test_merged_labels_equal_tier0_labels_exactly(self):
        merged = ensemble_vote([
            _src("tier0", 1.0, [_clean_tier0()]),
            _src("tier1", 0.8, [_fragmented_tier1()]),
        ])
        labels = [r.raw_text for r in merged[0].rows]
        assert labels == [
            "Immobilisations incorporelles",
            "Immobilisations corporelles",
            "Stocks",
        ]

    def test_tier1_fragments_land_in_metadata_not_rows(self):
        merged = ensemble_vote([
            _src("tier0", 1.0, [_clean_tier0()]),
            _src("tier1", 0.8, [_fragmented_tier1()]),
        ])
        out = merged[0]

        # Core contract: the scored keyspace stays exactly the clean Tier-0 rows
        # (3), and NO fragment label leaks into it — neither as a promoted orphan
        # nor by overwriting a Tier-0 label.
        assert len(out.rows) == 3
        row_labels = {r.raw_text for r in out.rows}
        assert set(_FRAGMENTS).isdisjoint(row_labels)

        # The true orphan fragment(s) are quarantined for HITL — nothing dropped.
        quarantined = {q["raw_text"] for q in out.metadata["unaligned_tier1"]}
        assert quarantined  # at least one orphan landed in metadata
        assert quarantined.issubset(set(_FRAGMENTS))
        assert out.metadata["unaligned_count"] == len(out.metadata["unaligned_tier1"])

    def test_aligned_row_keeps_tier0_raw_text(self):
        # Tier-1 reports a noisier raw_text for the shared row; Tier-0 wins.
        a = _tbl([_row("Immobilisations incorporelles",
                       "Immobilisations incorporelles", {"value_n": _ncell(1000)})])
        b = _tbl([_row("Immobi1isations  incorporel1es",  # OCR-garbled raw text
                       "Immobilisations incorporelles", {"value_n": _ncell(1000)})])
        merged = ensemble_vote([_src("tier0", 1.0, [a]), _src("tier1", 0.8, [b])])
        assert merged[0].rows[0].raw_text == "Immobilisations incorporelles"

    def test_non_tier0_anchor_still_promotes_orphans(self):
        # Backward-compat: a tier1 anchor must keep the original orphan promotion.
        a = _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])
        b = _tbl([
            _row("Stocks", "Stocks", {"value_n": _ncell(1500)}),
            _row("OCR-junk-row", canonical=None, cells={"value_n": _ncell(99)}),
        ])
        merged = ensemble_vote([_src("tier1", 1.0, [a]), _src("tier2", 0.8, [b])])
        assert len(merged[0].rows) == 2                       # orphan promoted
        assert "unaligned_tier2" not in merged[0].metadata    # not quarantined
        assert merged[0].rows[1].raw_text == "OCR-junk-row"