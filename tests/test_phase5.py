"""
Phase 5 tests — N-way ensemble voting (LV-ROVER) + Consensus-Entropy routing.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable
from accounting.ensemble import (
    CellVote,
    EnsembleSource,
    canonical_tables_to_records,
    consensus_entropy,
    ensemble_vote,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ncell(value, raw=None, conf: float = 1.0) -> CanonicalCell:
    return CanonicalCell(
        raw_value=raw if raw is not None else (str(value) if value is not None else ""),
        parsed_value=Decimal(str(value)) if value is not None else None,
        confidence=conf,
    )


def _row(raw, canonical=None, cells=None, section=None, conf=1.0, is_subtotal=False):
    return CanonicalRow(
        raw_text=raw,
        canonical_term=canonical,
        match_type="exact" if canonical else "custom",
        match_confidence=conf if canonical else 0.0,
        section=section,
        is_subtotal=is_subtotal,
        cells=cells or {},
    )


def _tbl(rows):
    ct = CanonicalTable(rows=rows)
    ct.recompute_aggregates()
    return ct


def _src(name, weight, tables):
    return EnsembleSource(name=name, tables=tables, weight=weight)


class _FakeLexicon:
    """Minimal lexicon stub: ``lookup`` returns a truthy entry iff term is valid."""

    def __init__(self, valid):
        self._valid = set(valid)

    def lookup(self, term):
        return object() if term in self._valid else None


# ===========================================================================
# Consensus Entropy
# ===========================================================================


class TestConsensusEntropy:
    def test_identical_candidates_zero_divergence(self):
        idx, delta = consensus_entropy(["1500", "1500", "1500"])
        assert delta == 0.0
        assert idx in (0, 1, 2)

    def test_medoid_is_majority_not_outlier(self):
        idx, delta = consensus_entropy(["1500", "1500", "9999"])
        assert idx in (0, 1)          # a majority member, never the outlier
        assert delta > 0.0

    def test_divergence_is_monotonic(self):
        _, near = consensus_entropy(["abc", "abc", "abd"])   # one-char drift
        _, far = consensus_entropy(["abc", "abc", "xyz"])    # full divergence
        assert far > near

    def test_degenerate_sizes(self):
        assert consensus_entropy([]) == (-1, 0.0)
        assert consensus_entropy(["x"]) == (0, 0.0)


# ===========================================================================
# ensemble_vote — numeric consensus
# ===========================================================================


class TestEnsembleNumeric:
    def test_three_sources_unanimous_all_green(self):
        sources = [
            _src("tier0", 1.0, [_tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])]),
            _src("tier1", 0.8, [_tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])]),
            _src("tier2", 0.7, [_tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])]),
        ]
        merged = ensemble_vote(sources)
        assert len(merged) == 1
        ct = merged[0]
        assert ct.metadata["q_table"] == 1.0
        cell = ct.rows[0].cells["value_n"]
        assert cell.parsed_value == Decimal("1500")
        assert cell.flag == "green"
        assert cell.provenance == "consensus"
        assert ct.metadata["conflict_count"] == 0

    def test_one_numeric_outlier_majority_wins(self):
        sources = [
            _src("tier0", 1.0, [_tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])]),
            _src("tier1", 0.8, [_tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])]),
            _src("tier2", 0.7, [_tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1490)})])]),
        ]
        merged = ensemble_vote(sources)
        cell = merged[0].rows[0].cells["value_n"]
        assert cell.parsed_value == Decimal("1500")   # majority value
        assert cell.flag in ("yellow", "red")
        # Conflict lists all three candidates with the consensus entropy.
        conflicts = [c for c in merged[0].conflicts if c["column_name"] == "value_n"]
        assert len(conflicts) == 1
        assert len(conflicts[0]["candidates"]) == 3
        assert "consensus_entropy" in conflicts[0]

    def test_two_sources_no_majority_trusts_higher_weight(self):
        sources = [
            _src("tier0", 1.0, [_tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])]),
            _src("tier1", 0.8, [_tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1490)})])]),
        ]
        merged = ensemble_vote(sources)
        cell = merged[0].rows[0].cells["value_n"]
        assert cell.parsed_value == Decimal("1500")   # higher-weight tier0
        assert cell.flag in ("yellow", "red")
        assert merged[0].metadata["conflict_count"] >= 1

    def test_numbers_equal_despite_string_form(self):
        # "1500" vs "1500.00" parse equal → numeric branch → no penalty.
        sources = [
            _src("tier0", 1.0, [_tbl([_row("Stocks", "Stocks",
                                           {"value_n": _ncell(1500, raw="1500")})])]),
            _src("tier1", 0.8, [_tbl([_row("Stocks", "Stocks",
                                           {"value_n": _ncell(Decimal("1500.00"), raw="1500.00")})])]),
        ]
        merged = ensemble_vote(sources)
        cell = merged[0].rows[0].cells["value_n"]
        assert cell.flag == "green"
        assert cell.provenance == "consensus"


# ===========================================================================
# ensemble_vote — LV-ROVER label verification
# ===========================================================================


class TestLVRover:
    def test_unverified_winner_falls_back_to_lexicon_match(self):
        # tier0 emits a high-confidence but bogus term; tier1 emits a valid one.
        # They share raw_text so the aligner pairs them (text match).
        a = _tbl([_row("Stocks", canonical="Bogus", conf=1.0,
                       cells={"value_n": _ncell(10)})])
        b = _tbl([_row("Stocks", canonical="Stocks", conf=0.9,
                       cells={"value_n": _ncell(10)})])
        merged = ensemble_vote(
            [_src("tier0", 1.0, [a]), _src("tier1", 0.8, [b])],
            lexicon=_FakeLexicon(["Stocks"]),
        )
        # Label disagreement recorded as a red <label> conflict.
        label_conflicts = [c for c in merged[0].conflicts if c["column_name"] == "<label>"]
        assert len(label_conflicts) == 1
        assert label_conflicts[0]["flag"] == "red"
        # ISSUE-9 T3: LV-ROVER still elects "Stocks" as the conflict winner …
        assert label_conflicts[0]["winner"] == "Stocks"
        # … but on a RED-consensus conflict the safety net suppresses the
        # asserted mapping rather than emit a confident-but-wrong term.
        assert merged[0].rows[0].canonical_term is None
        # Raw text + numeric cell are preserved for HITL.
        assert merged[0].rows[0].raw_text == "Stocks"
        assert merged[0].rows[0].cells["value_n"].parsed_value == Decimal("10")

    def test_red_label_conflict_suppresses_canonical_term(self):
        # ISSUE-9 T3 safety net (focused): two tiers disagree on the label of
        # the same physical row → red-consensus <label> conflict. The ensemble
        # must NOT assert a winning canonical_term; it nulls the mapping but
        # keeps raw_text and the numeric cells for downstream HITL.
        a = _tbl([_row("Moins: Amortiss", canonical="Amortissements", conf=1.0,
                       cells={"value_n": _ncell(500)})])
        b = _tbl([_row("Moins: Amortiss", canonical="Résultats reportés", conf=0.95,
                       cells={"value_n": _ncell(500)})])
        merged = ensemble_vote(
            [_src("tier0", 1.0, [a]), _src("tier1", 0.8, [b])],
            lexicon=_FakeLexicon(["Amortissements", "Résultats reportés"]),
        )
        row = merged[0].rows[0]
        # A red <label> conflict was recorded …
        label_conflicts = [c for c in merged[0].conflicts if c["column_name"] == "<label>"]
        assert len(label_conflicts) == 1
        assert label_conflicts[0]["flag"] == "red"
        # … and the confident-but-wrong canonical_term is suppressed.
        assert row.canonical_term is None
        assert row.account_code is None
        assert row.validation_field is None
        assert row.match_type == "unrecognized"
        # Data preserved for HITL: raw label + numeric cell survive intact.
        assert row.raw_text == "Moins: Amortiss"
        assert row.cells["value_n"].parsed_value == Decimal("500")


# ===========================================================================
# ensemble_vote — alignment, orphans, degenerate cases
# ===========================================================================


class TestEnsembleStructure:
    def test_non_anchor_orphan_row_preserved(self):
        # Non-tier0 anchor → orphans still promoted into rows (backward-compatible).
        a = _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])
        b = _tbl([
            _row("Stocks", "Stocks", {"value_n": _ncell(1500)}),
            _row("OCR-clean-orphan", canonical=None, cells={"value_n": _ncell(99)}),
        ])
        merged = ensemble_vote([_src("tier1", 1.0, [a]), _src("tier2", 0.8, [b])])
        assert len(merged[0].rows) == 2          # orphan not dropped
        orphan = merged[0].rows[1].cells["value_n"]
        # A single-source cell inherits its engine's OWN flag (clean parse → green),
        # consistent with a Tier-1-only read — not a blanket yellow that paints a
        # whole non-aligning ensemble table suspect. The uncorroborated signal lives
        # in provenance ("ensemble", not "consensus").
        assert orphan.flag == "green"
        assert orphan.provenance == "ensemble"

    def test_single_source_risky_cell_stays_yellow(self):
        # The meaningful signal is preserved: a single-source cell the source engine
        # itself flagged risky is NOT greened by the ensemble.
        risky = _ncell(42)
        risky.flag = "yellow"
        a = _tbl([_row("X", "X", {"value_n": risky})])
        merged = ensemble_vote([_src("tier1", 1.0, [a]), _src("tier2", 0.8, [_tbl([])])])
        assert merged[0].rows[0].cells["value_n"].flag == "yellow"

    def test_tier0_anchor_orphan_quarantined(self):
        # Tier-0 anchor → fragment orphan is QUARANTINED into metadata, not scored.
        a = _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])
        b = _tbl([
            _row("Stocks", "Stocks", {"value_n": _ncell(1500)}),
            _row("OCR-junk-row", canonical=None, cells={"value_n": _ncell(99)}),
        ])
        merged = ensemble_vote([_src("tier0", 1.0, [a]), _src("tier1", 0.8, [b])])
        assert len(merged[0].rows) == 1                    # fragment NOT promoted
        assert merged[0].metadata["unaligned_count"] == 1
        quarantined = merged[0].metadata["unaligned_tier1"]
        assert quarantined[0]["raw_text"] == "OCR-junk-row"

    def test_single_source_passthrough(self):
        a = _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])
        merged = ensemble_vote([_src("tier1", 0.8, [a])])
        assert len(merged) == 1
        assert merged[0].metadata["reconciliation"] == "ensemble_single_source"

    def test_empty_sources(self):
        assert ensemble_vote([]) == []

    def test_table_count_mismatch(self):
        a = _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])
        a2 = _tbl([_row("Capital social", "Capital social", {"value_n": _ncell(800)})])
        b = _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])
        merged = ensemble_vote([_src("tier0", 1.0, [a, a2]), _src("tier1", 0.8, [b])])
        assert len(merged) == 2
        assert merged[0].metadata["reconciliation"] == "ensemble"          # voted
        assert merged[1].metadata["reconciliation"] == "ensemble_single_source"


# ===========================================================================
# ensemble_vote — arbiter (LLM-as-Judge) hook
# ===========================================================================


class TestArbiterHook:
    def _conflicting_pair(self):
        # Equal weights + values beyond tolerance → win_frac 0.5 → δ 0.5 → red.
        a = _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500)})])
        b = _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1490)})])
        return [_src("tier0", 1.0, [a]), _src("tier1", 1.0, [b])]

    def test_red_cell_without_arbiter_stays_red(self):
        merged = ensemble_vote(self._conflicting_pair(), arbiter=None)
        assert merged[0].rows[0].cells["value_n"].flag == "red"

    def test_arbiter_resolves_red_to_yellow(self):
        captured = {}

        def judge(vote: CellVote):
            captured["called"] = True
            assert len(vote.candidates) == 2
            return Decimal("1495")

        merged = ensemble_vote(self._conflicting_pair(), arbiter=judge)
        cell = merged[0].rows[0].cells["value_n"]
        assert captured.get("called") is True
        assert cell.parsed_value == Decimal("1495")
        assert cell.flag == "yellow"
        assert cell.provenance == "arbiter"


# ===========================================================================
# Serialization
# ===========================================================================


def test_canonical_tables_to_records():
    a = _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(1500, raw="1 500")})])
    records = canonical_tables_to_records([a])
    # P0-3: one label column (canonical when matched), raw text kept alongside.
    assert records == [[{"label": "Stocks", "label_raw": "Stocks", "value_n": "1 500"}]]


def test_records_label_falls_back_to_raw_when_unmatched():
    a = _tbl([_row("Stokcs (ocr)", None, {"value_n": _ncell(1500, raw="1 500")})])
    rec = canonical_tables_to_records([a])[0][0]
    assert rec["label"] == "Stokcs (ocr)"
    assert rec["label_raw"] == "Stokcs (ocr)"


def test_records_use_canonical_role_order_not_source_order():
    """P0-2: the grid must read like the statement (note → N → N-1), not in
    whatever column order the extractor happened to emit."""
    a = _tbl([
        _row("Stocks", "Stocks", {
            "value_n": _ncell(1500, raw="1 500"),
            "note": _ncell(None, raw="3"),
            "value_n_minus_1": _ncell(1400, raw="1 400"),
        })
    ])
    cols = list(canonical_tables_to_records([a])[0][0].keys())
    assert cols.index("note") < cols.index("value_n") < cols.index("value_n_minus_1")


def test_unroled_columns_are_kept_after_the_roled_ones():
    """A real Variation/% column has no canonical role — it must still appear."""
    a = _tbl([
        _row("Stocks", "Stocks", {
            "Variation": _ncell(100, raw="100"),
            "value_n": _ncell(1500, raw="1 500"),
        })
    ])
    cols = list(canonical_tables_to_records([a])[0][0].keys())
    assert "Variation" in cols
    assert cols.index("value_n") < cols.index("Variation")


# ===========================================================================
# Pipeline Tier 3 routing
# ===========================================================================


class TestTier3Routing:
    def _pipeline(self):
        from pipeline import FinAlzePipeline
        return FinAlzePipeline()

    def _result(self, raw_tables, qcs, critical_failures=0, tier=1):
        from pipeline import ExtractionResult
        return ExtractionResult(
            text="", tables=[], qcs_score=qcs, tier_used=tier,
            confidence_details={},
            metadata={"validation_report": {"critical_failures": critical_failures}},
            raw_canonical_tables=raw_tables,
        )

    def _agree_table(self, value=1500):
        return _tbl([_row("Stocks", "Stocks", {"value_n": _ncell(value)})])

    def test_two_sources_high_consensus_accepts(self):
        t0 = self._result([self._agree_table()], qcs=0.6, tier=0)
        t1 = self._result([self._agree_table()], qcs=0.6, tier=1)
        res = self._pipeline()._tier3_ensemble(t0, t1, None, [])
        assert res.tier_used == 3
        assert res.metadata["hitl_required"] is False
        assert res.metadata["ensemble"]["routing_reason"] == "high_consensus"

    def test_single_source_routes_to_hitl(self):
        t1 = self._result([self._agree_table()], qcs=0.6, tier=1)
        res = self._pipeline()._tier3_ensemble(None, t1, None, [])
        assert res.metadata["hitl_required"] is True
        assert res.metadata["ensemble"]["reason"] == "single_source"

    def test_arbiter_band_without_judge_routes_to_hitl(self):
        # One agreeing cell + one disagreeing cell → mid Q_table (arbiter band).
        def mixed():
            return _tbl([_row("Stocks", "Stocks",
                              {"value_a": _ncell(1500), "value_b": _ncell(100)})])

        def mixed2():
            return _tbl([_row("Stocks", "Stocks",
                              {"value_a": _ncell(1500), "value_b": _ncell(200)})])

        t0 = self._result([mixed()], qcs=0.6, tier=0)
        t1 = self._result([mixed2()], qcs=0.6, tier=1)
        res = self._pipeline()._tier3_ensemble(t0, t1, None, [])
        q = res.metadata["ensemble"]["q_table"]
        assert 0.70 <= q < 0.90
        assert res.metadata["hitl_required"] is True
        assert res.metadata["ensemble"]["routing_reason"] == "arbiter_band_no_judge"

    def test_critical_arithmetic_failure_overrides_high_consensus(self):
        t0 = self._result([self._agree_table()], qcs=0.6, tier=0)
        t1 = self._result([self._agree_table()], qcs=0.6, critical_failures=1, tier=1)
        res = self._pipeline()._tier3_ensemble(t0, t1, None, [])
        assert res.metadata["hitl_required"] is True
        assert res.metadata["ensemble"]["routing_reason"] == "arithmetic_failure"
        assert res.metadata["ensemble"]["arithmetic_override"] is True
