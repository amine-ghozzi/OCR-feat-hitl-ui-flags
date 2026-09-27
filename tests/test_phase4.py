"""
Phase 4 tests — row alignment (§20) + dual-tier reconciliation (§10).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable
from accounting.row_aligner import align_canonical_tables
from accounting.reconciliation import reconcile_pair, reconcile_dual_tier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    raw: str,
    canonical: str | None = None,
    section: str | None = None,
    cells: dict | None = None,
    is_subtotal: bool = False,
) -> CanonicalRow:
    return CanonicalRow(
        raw_text=raw,
        canonical_term=canonical,
        match_type="exact" if canonical else "custom",
        match_confidence=1.0 if canonical else 0.0,
        section=section,
        is_subtotal=is_subtotal,
        cells=cells or {},
    )


def _cell(raw: str, value: Decimal | None = None, provenance: str = "tier0") -> CanonicalCell:
    return CanonicalCell(
        raw_value=raw,
        parsed_value=value,
        provenance=provenance,
        flag="green",
        confidence=1.0,
    )


def _make_table(rows: list[CanonicalRow]) -> CanonicalTable:
    ct = CanonicalTable(rows=rows)
    ct.recompute_aggregates()
    return ct


# ===========================================================================
# Row alignment
# ===========================================================================


class TestRowAligner:
    def test_canonical_key_join(self):
        a = _make_table([
            _make_row("Stocks", canonical="Stocks"),
            _make_row("Capital social", canonical="Capital social"),
        ])
        b = _make_table([
            _make_row("Capital social", canonical="Capital social"),
            _make_row("Stocks", canonical="Stocks"),
        ])
        result = align_canonical_tables(a, b)
        assert len(result.pairs) == 2
        # Both should have method="canonical"
        assert all(p.method == "canonical" for p in result.pairs)
        assert not result.a_orphans and not result.b_orphans

    def test_text_match_when_no_canonical(self):
        a = _make_table([_make_row("Custom Row Foo")])
        b = _make_table([_make_row("Custom Row Foo!")])
        result = align_canonical_tables(a, b)
        assert len(result.pairs) == 1
        assert result.pairs[0].method == "text"
        assert result.pairs[0].confidence >= 0.70

    def test_a_orphan_when_no_b_match(self):
        a = _make_table([
            _make_row("Stocks", canonical="Stocks"),
            _make_row("Tier-0-only-row", canonical=None),
        ])
        b = _make_table([_make_row("Stocks", canonical="Stocks")])
        result = align_canonical_tables(a, b)
        assert len(result.pairs) == 1
        assert result.a_orphans == [1]
        assert not result.b_orphans

    def test_b_orphan_when_no_a_match(self):
        a = _make_table([_make_row("Stocks", canonical="Stocks")])
        b = _make_table([
            _make_row("Stocks", canonical="Stocks"),
            _make_row("OCR-decoration-line", canonical=None),
        ])
        result = align_canonical_tables(a, b)
        assert len(result.pairs) == 1
        assert result.b_orphans == [1]

    def test_positional_within_section(self):
        a = _make_table([
            _make_row("Custom A1", section="actifs_courants"),
            _make_row("Custom A2", section="actifs_courants"),
        ])
        b = _make_table([
            _make_row("CustomA1", section="actifs_courants"),
            _make_row("CustomA2-OCR-mangled", section="actifs_courants"),
        ])
        result = align_canonical_tables(a, b)
        # Either text or positional method aligns them
        assert len(result.pairs) == 2

    def test_empty_inputs(self):
        empty = _make_table([])
        result = align_canonical_tables(empty, empty)
        assert result.pairs == []
        assert result.a_orphans == [] and result.b_orphans == []

    def test_coverage(self):
        a = _make_table([
            _make_row("Stocks", canonical="Stocks"),
            _make_row("Orphan", canonical=None),
        ])
        b = _make_table([_make_row("Stocks", canonical="Stocks")])
        result = align_canonical_tables(a, b)
        assert result.coverage == 0.5  # 1 of 2 a-rows aligned


# ===========================================================================
# Dual-tier reconciliation
# ===========================================================================


class TestReconcilePair:
    def test_numeric_agreement_is_green(self):
        a = _make_table([_make_row(
            "Stocks", canonical="Stocks",
            cells={"value_n": _cell("1500", Decimal("1500"))},
        )])
        b = _make_table([_make_row(
            "Stocks", canonical="Stocks",
            cells={"value_n": _cell("1500", Decimal("1500"))},
        )])
        merged = reconcile_pair(a, b)
        assert len(merged.rows) == 1
        cell = merged.rows[0].cells["value_n"]
        assert cell.parsed_value == Decimal("1500")
        assert cell.flag == "green"
        assert cell.provenance == "consensus"
        assert merged.metadata["conflict_count"] == 0

    def test_numeric_disagreement_trusts_tier0(self):
        a = _make_table([_make_row(
            "Stocks", canonical="Stocks",
            cells={"value_n": _cell("1500", Decimal("1500"))},
        )])
        b = _make_table([_make_row(
            "Stocks", canonical="Stocks",
            cells={"value_n": _cell("15OO", Decimal("1500.00"))},  # different OCR digits
        )])
        b.rows[0].cells["value_n"].parsed_value = Decimal("1490")  # drift > tolerance (1)
        merged = reconcile_pair(a, b)
        cell = merged.rows[0].cells["value_n"]
        # Disagreement > tolerance → yellow, value from Tier 0
        assert cell.parsed_value == Decimal("1500")
        assert cell.flag == "yellow"
        assert cell.provenance == "tier0"
        assert merged.metadata["conflict_count"] == 1
        conflict = merged.conflicts[0]
        assert conflict["resolution"] == "tier0"
        assert conflict["tier0_parsed"] == "1500"
        assert conflict["tier1_parsed"] == "1490"

    def test_tier0_orphan_passes_through(self):
        a = _make_table([
            _make_row("Stocks", canonical="Stocks", cells={"value_n": _cell("1500", Decimal("1500"))}),
            _make_row("Tier0-only", canonical=None, cells={"value_n": _cell("99", Decimal("99"))}),
        ])
        b = _make_table([
            _make_row("Stocks", canonical="Stocks", cells={"value_n": _cell("1500", Decimal("1500"))}),
        ])
        merged = reconcile_pair(a, b)
        assert len(merged.rows) == 2
        assert merged.metadata["tier0_orphans"] == 1
        # Orphan keeps Tier 0 provenance
        orphan_cell = merged.rows[1].cells["value_n"]
        assert orphan_cell.provenance == "tier0_only"
        assert orphan_cell.parsed_value == Decimal("99")

    def test_tier1_orphan_flagged_yellow(self):
        a = _make_table([_make_row(
            "Stocks", canonical="Stocks",
            cells={"value_n": _cell("1500", Decimal("1500"))},
        )])
        b = _make_table([
            _make_row("Stocks", canonical="Stocks", cells={"value_n": _cell("1500", Decimal("1500"))}),
            _make_row("OCR-artifact-row", canonical=None,
                      cells={"value_n": _cell("999", Decimal("999"))}),
        ])
        merged = reconcile_pair(a, b)
        assert merged.metadata["tier1_orphans"] == 1
        # Tier 1 orphan should be yellow (suspect OCR artifact)
        artifact_row = merged.rows[1]
        assert artifact_row.cells["value_n"].flag == "yellow"

    def test_label_disagreement_is_red(self):
        a = _make_table([_make_row(
            "Stocks", canonical="Stocks",
            cells={"value_n": _cell("100", Decimal("100"))},
        )])
        # Same row index/text but different canonical mapping
        b_row = _make_row("Stocks", canonical="Capital social",
                          cells={"value_n": _cell("100", Decimal("100"))})
        b = _make_table([b_row])
        merged = reconcile_pair(a, b)
        # Different canonical_term blocks Phase 1 join; these rows fall to
        # Phase 2 (text match) and pair on identical raw_text. The merge
        # records a label-disagreement conflict with red flag.
        assert len(merged.rows) == 1
        red_label_conflicts = [
            c for c in merged.conflicts
            if c["column_name"] == "<label>" and c["flag"] == "red"
        ]
        assert len(red_label_conflicts) == 1

    def test_tier1_only_when_tier0_missed(self):
        a = _make_table([_make_row(
            "Stocks", canonical="Stocks",
            cells={"value_n": _cell("", None)},  # Tier 0 didn't parse
        )])
        b = _make_table([_make_row(
            "Stocks", canonical="Stocks",
            cells={"value_n": _cell("1500", Decimal("1500"))},
        )])
        merged = reconcile_pair(a, b)
        cell = merged.rows[0].cells["value_n"]
        assert cell.parsed_value == Decimal("1500")
        assert cell.provenance == "tier1"
        assert cell.flag == "yellow"  # Tier 1 fallback, flag for review


class TestReconcileDualTier:
    def test_passes_through_tier0_only(self):
        a = _make_table([_make_row("Row", canonical="Stocks")])
        merged = reconcile_dual_tier([a], [])
        assert len(merged) == 1
        assert merged[0] is a  # same object passed through

    def test_passes_through_tier1_only(self):
        b = _make_table([_make_row("Row")])
        merged = reconcile_dual_tier([], [b])
        assert len(merged) == 1
        assert merged[0] is b

    def test_pairs_by_index(self):
        a1 = _make_table([_make_row("X", canonical="Stocks",
                                     cells={"v": _cell("1", Decimal("1"))})])
        a2 = _make_table([_make_row("Y", canonical="Capital social",
                                     cells={"v": _cell("2", Decimal("2"))})])
        b1 = _make_table([_make_row("X", canonical="Stocks",
                                     cells={"v": _cell("1", Decimal("1"))})])
        b2 = _make_table([_make_row("Y", canonical="Capital social",
                                     cells={"v": _cell("2", Decimal("2"))})])

        merged = reconcile_dual_tier([a1, a2], [b1, b2])
        assert len(merged) == 2
        assert all(m.metadata.get("reconciliation") == "dual_tier" for m in merged)

    def test_extras_passed_through(self):
        a1 = _make_table([_make_row("X", canonical="Stocks",
                                     cells={"v": _cell("1", Decimal("1"))})])
        a2 = _make_table([_make_row("Extra", canonical="Capital social")])
        b1 = _make_table([_make_row("X", canonical="Stocks",
                                     cells={"v": _cell("1", Decimal("1"))})])

        merged = reconcile_dual_tier([a1, a2], [b1])
        assert len(merged) == 2
        # First is reconciled, second is tier0_only
        assert merged[0].metadata.get("reconciliation") == "dual_tier"
        assert merged[1].metadata.get("reconciliation") == "tier0_only"

    def test_empty_both_sides(self):
        assert reconcile_dual_tier([], []) == []
