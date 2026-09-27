"""
Regression tests for the Phase-0 fixes.

P0-1 — ``_assign_value_columns_by_year`` bound ``value_n`` to the LABEL column.
       PyMuPDF names a natively-extracted table's first column after the table
       *title*, so a Bilan yields ``['Bilan au 31/12/2024', 'Col1', 'Col2',
       '(en DT)']`` and the label column carries the only year on the page. It
       won outright, putting label text in ``value_n`` and leaving the real
       amount column with no role at all.

P0-5 — Footing validation only ever considered the flat run of plain rows since
       the previous subtotal, which is wrong for two shapes present in every NCT
       pack: a *rollup* (``Total des actifs non courants`` = a nested subtotal +
       a leaf) under-counts, and a *restatement* (``Résultat avant
       modifications`` → ``Résultat net`` carried forward) double-counts. Both
       produced false CRITICALs, and a CRITICAL sets ``arithmetic_fail`` in the
       Tier-3 ensemble, which overrides ``Q_table`` unconditionally and forces
       HITL on a correctly extracted document.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from accounting.column_identifier import identify_columns  # noqa: E402
from accounting.subtotal_detector import detect_subtotals  # noqa: E402


# ===========================================================================
# P0-1 — a value role must never bind to a non-numeric / label column
# ===========================================================================


def _bilan_with_title_header() -> pd.DataFrame:
    """The real Tier-0 shape: the table title became the label column's header,
    so the ONLY year on the page sits on a non-numeric column."""
    return pd.DataFrame(
        {
            "Bilan au 31/12/2024": [
                "Immobilisations Incorporelles",
                "Immobilisations Corporelles",
                "Total des actifs immobilisés",
                "Autres actifs non courants",
            ],
            # A real Notes column is sparse — most rows carry no reference.
            "Col1": ["1", "", "", ""],
            "Col2": ["210,580", "53,536,607", "29,496,190", "4,919,664"],
            "(en DT)": ["210,580", "52,938,385", "32,498,818", "3,500,000"],
        }
    )


def test_value_n_never_binds_to_the_label_column():
    df = _bilan_with_title_header()
    layout = identify_columns(df)

    assert "value_n" in layout.role_map, "value_n should still bind"
    assert layout.role_map["value_n"] != layout.label_col_index, (
        "value_n bound to the label column — the table title's year won"
    )
    assert layout.role_map["value_n"] in layout.numeric_col_indices, (
        "value_n must bind to a numeric column"
    )


def test_real_amount_column_is_not_orphaned():
    """Col2 holds the N amounts; it must receive a role, not be left unroled."""
    df = _bilan_with_title_header()
    layout = identify_columns(df)
    assert 2 in layout.role_map.values(), "the real amount column got no role"


def test_year_binding_still_works_for_genuine_dated_columns():
    """The mixed FR/US date case this function exists for must keep working:
    the most recent year is N, the next is N-1."""
    df = pd.DataFrame(
        {
            "Libellé": ["Revenus", "Charges", "Total"],
            "12/31/2024": ["95,276,113", "41,694,461", "136,970,574"],
            "31/12/2023": ["88,000,000", "39,000,000", "127,000,000"],
        }
    )
    layout = identify_columns(df)
    assert layout.role_map.get("value_n") == 1
    assert layout.role_map.get("value_n_minus_1") == 2


# ===========================================================================
# P0-5 — hierarchy-aware footing
# ===========================================================================


def _footing_case(labels: list[str], values: list[int]) -> tuple:
    """Build a (df, layout, parsed_numbers) triple for the footing checker."""
    df = pd.DataFrame({"Libellé": labels, "Montant": [f"{v}" for v in values]})
    layout = identify_columns(df)
    layout.numeric_col_indices = [1]
    parsed = {(i, 1): Decimal(str(v)) for i, v in enumerate(values)}
    return df, layout, parsed


def test_rollup_subtotal_includes_the_nested_total():
    """``Total des actifs non courants`` = nested subtotal + a leaf.

    The flat grouping sees only the leaf (4,919,664) and fails; the correct
    reading adds the nested ``Total des actifs immobilisés``.
    """
    df, layout, parsed = _footing_case(
        ["Immobilisations", "Autres immo", "Total des actifs immobilisés",
         "Autres actifs non courants", "Total des actifs non courants"],
        [20_000_000, 9_496_190, 29_496_190, 4_919_664, 34_415_854],
    )
    results = detect_subtotals(df, layout, None, None, parsed)
    by_row = {r.row_index: r for r in results}

    assert by_row[2].footing_valid is True
    rollup = by_row[4]
    assert rollup.footing_valid is True, (
        f"rollup failed: expected={rollup.expected_value} actual={rollup.actual_value}"
    )
    assert rollup.footing_basis == "rollup"
    assert 2 in rollup.child_indices, "the nested subtotal must be a child"


def test_restatement_is_not_summed():
    """A figure carried forward is not the sum of the rows above it."""
    # NB: labels must match ``_SUBTOTAL_RE`` since no dictionary is supplied —
    # on the real document the dictionary's ``is_subtotal`` flag detects
    # "Résultat Net de L'EXERCICE".
    df, layout, parsed = _footing_case(
        ["Éléments extraordinaires", "Résultat avant modifications",
         "Résultat net après modifications", "Total des résultats"],
        [400, -2_972_966, -2_972_966, -2_972_966],
    )
    results = detect_subtotals(df, layout, None, None, parsed)
    total = [r for r in results if r.row_index == 3][0]
    assert total.footing_valid is True, (
        f"restatement summed instead of carried: expected={total.expected_value}"
    )
    assert total.footing_basis == "restatement"


def test_grand_total_of_subtotals_validates():
    """``Total des Actifs`` = non courants + courants — previously unchecked."""
    df, layout, parsed = _footing_case(
        ["Immo", "Total des actifs non courants", "Stocks",
         "Total des actifs courants", "Total des Actifs"],
        [34_415_854, 34_415_854, 126_021_358, 126_021_358, 160_437_212],
    )
    results = detect_subtotals(df, layout, None, None, parsed)
    grand = [r for r in results if r.row_index == 4][0]
    assert grand.footing_valid is True
    assert grand.footing_basis == "subtotals"


def test_a_genuine_mismatch_still_fails():
    """The hypotheses must not rescue a real arithmetic error."""
    df, layout, parsed = _footing_case(
        ["Poste A", "Poste B", "Total des postes"],
        [100, 200, 999],
    )
    results = detect_subtotals(df, layout, None, None, parsed)
    total = [r for r in results if r.row_index == 2][0]
    assert total.footing_valid is False
    assert total.footing_basis is None


# ===========================================================================
# An extraction that produced nothing must not report success
# ===========================================================================


def test_empty_table_regions_fail_the_tables_found_check():
    """A failed OCR pass on a scanned document yields table REGIONS with no
    content — one real sample gives 16 frames of shape (0,0). A list of 16
    empty frames is truthy, so the check reported 'Found 16 table(s)' and
    passed its CRITICAL, making a total extraction failure look like a partial
    success."""
    from accounting.rule_engine import validate_with_rules

    report = validate_with_rules([pd.DataFrame() for _ in range(16)], standard="NCT")
    found = [c for c in report.checks if c.check_name == "Tables Found"][0]
    assert found.passed is False
    assert report.critical_failures == 1
    assert "empty" in found.message.lower()


def test_no_tables_at_all_still_fails_distinctly():
    from accounting.rule_engine import validate_with_rules

    report = validate_with_rules([], standard="NCT")
    found = [c for c in report.checks if c.check_name == "Tables Found"][0]
    assert found.passed is False
    assert "No tables extracted" in found.message


def test_a_table_with_content_still_passes():
    from accounting.rule_engine import validate_with_rules

    df = pd.DataFrame({"L": ["Stocks", "Total des actifs"], "2024": ["1", "1"]})
    report = validate_with_rules([df], standard="NCT")
    found = [c for c in report.checks if c.check_name == "Tables Found"][0]
    assert found.passed is True


# ===========================================================================
# Classification happens ONCE — redundant sites retired
# ===========================================================================


def test_there_is_no_pre_ocr_classifier():
    """`_classify_document` pooled the first three pages, returned ONE type for
    a multi-statement document, defaulted to 'bilan' on four failure paths, and
    had no reader. Classification belongs after extraction."""
    from pipeline import FinAlzePipeline

    assert not hasattr(FinAlzePipeline, "_classify_document")


def test_doc_type_metadata_has_a_single_meaning():
    """The key held an extraction METHOD in Tier 0 and a STATEMENT TYPE in
    Tier 1 — two domains under one name, which is why nothing could read it."""
    import inspect
    from pipeline import FinAlzePipeline

    src = inspect.getsource(FinAlzePipeline._tier1_fast_ocr)
    assert "'doc_type': 'docling_ocr'" in src
    assert "doc_type: str" not in inspect.signature(
        FinAlzePipeline._tier1_fast_ocr).__str__()


def test_enrichment_has_no_dead_reclassification_branch():
    """The old fallback re-ran the identical scorer over identical strings
    (`raw_text` is built from the same cell the early pass read), so it could
    only ever return 'unknown' again."""
    import inspect
    from accounting import enrichment_pipeline

    src = inspect.getsource(enrichment_pipeline._enrich_single_table)
    stage9 = src[src.index("Stage 9 — Statement classification"):]
    stage9 = stage9[:stage9.index("Stage 10")]
    assert "classify_statement(labels)" not in stage9


def test_hitl_labels_come_from_the_inventory_not_a_fresh_pass(monkeypatch):
    """Re-deriving the labels lets the UI disagree with the payload it is
    showing. They must come from the result's own `statements` block."""
    import types
    import hitl.app as app

    class SS(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    result = types.SimpleNamespace(metadata={"statements": [
        {"id": 0, "type": "bilan", "sub_type": "actif",
         "confidence": 0.88, "table_indices": [0]},
        {"id": 1, "type": None, "sub_type": None,
         "confidence": 0.18, "table_indices": [2]},
    ]})
    monkeypatch.setattr(app, "st", types.SimpleNamespace(
        session_state=SS(extraction_result=result, table_segments=[])))
    # Any call to the old path would be a re-classification.
    monkeypatch.setattr(app, "segment_tables",
                        lambda *_a, **_k: pytest.fail("re-classified"))

    tables = [pd.DataFrame({"L": ["a"]}), pd.DataFrame({"L": ["b"]}),
              pd.DataFrame({"L": ["c"]})]
    segs = app._table_segments(tables)
    assert (segs[0].statement_type, segs[0].sub_type) == ("bilan", "actif")
    assert segs[1].statement_type == "unknown", "no statement covers table 1"
    assert segs[2].statement_type == "unknown", "quarantined statements surface"


def test_hitl_still_classifies_a_pre_inventory_result(monkeypatch):
    import types
    import hitl.app as app

    class SS(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    monkeypatch.setattr(app, "st", types.SimpleNamespace(
        session_state=SS(extraction_result=types.SimpleNamespace(metadata={}))))
    called = {"n": 0}

    def _fake(tables):
        called["n"] += 1
        return []

    monkeypatch.setattr(app, "segment_tables", _fake)
    app._table_segments([pd.DataFrame({"L": ["a"]})])
    assert called["n"] == 1, "must fall back when there is no inventory"


# ===========================================================================
# The quarantine gate must have teeth (and must not be moved earlier)
# ===========================================================================


def _gate_scenario():
    from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable
    from pipeline import ExtractionResult, FinAlzePipeline, PipelineConfig

    def ct(label):
        t = CanonicalTable(statement_type="bilan")
        row = CanonicalRow(raw_text=label)
        row.cells["value_n"] = CanonicalCell(raw_value="1")
        t.rows.append(row)
        return t

    good = pd.DataFrame({
        "L": ["Actifs non courants", "Immobilisations Incorporelles",
              "Immobilisations Corporelles", "Total des actifs immobilises"],
        "2024": ["1", "2", "3", "6"]})
    junk = pd.DataFrame({"L": ["Widget A", "Widget B", "Sundry"],
                         "2024": ["1", "2", "3"]})

    pipe = FinAlzePipeline.__new__(FinAlzePipeline)
    pipe.config = PipelineConfig()
    result = ExtractionResult(
        text="", tables=[good.to_dict("records"), junk.to_dict("records")],
        qcs_score=0.9, tier_used=1, confidence_details={}, metadata={},
        canonical_tables=[ct("Immobilisations"), ct("Widget A")])
    pipe._attach_statements(result)
    return result


def test_a_quarantined_statement_is_marked_and_excluded_from_validation():
    """Recording the verdict is not enough — 'should not continue' has to mean
    the statement stops contributing validated numbers."""
    result = _gate_scenario()
    gates = [s["gate"] for s in result.metadata["statements"]]
    assert gates == ["pass", "quarantined"]
    assert result.canonical_tables[1].metadata["gate"] == "quarantined"
    assert result.canonical_tables[0].metadata.get("gate") is None
    assert result.metadata["hitl_required"] is True
    assert (result.metadata.get("validation_report") or {}).get("total_checks"), \
        "the surviving statement must still be validated"


def _quarantine_scenario(score_kind, qcs=0.90, hitl=False):
    """`_gate_scenario` with the two facts RTE-6 needs: a declared `score_kind`
    and the intrinsic grades the recomputation reuses."""
    from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable
    from pipeline import ExtractionResult, FinAlzePipeline, PipelineConfig

    def ct(label):
        t = CanonicalTable(statement_type="bilan")
        row = CanonicalRow(raw_text=label)
        row.cells["value_n"] = CanonicalCell(raw_value="1")
        t.rows.append(row)
        return t

    good = pd.DataFrame({
        "L": ["Actifs non courants", "Immobilisations Incorporelles",
              "Immobilisations Corporelles", "Total des actifs immobilises"],
        "2024": ["1", "2", "3", "6"]})
    junk = pd.DataFrame({"L": ["Widget A", "Widget B", "Sundry"],
                         "2024": ["1", "2", "3"]})

    pipe = FinAlzePipeline.__new__(FinAlzePipeline)
    pipe.config = PipelineConfig()
    result = ExtractionResult(
        text="Bilan au 31 decembre 2024",
        tables=[good.to_dict("records"), junk.to_dict("records")],
        qcs_score=qcs, tier_used=1,
        confidence_details={
            "ocr_grade": "EXCELLENT", "layout_grade": "EXCELLENT",
            "parse_grade": "EXCELLENT", "low_grade": "EXCELLENT",
            "corrections_made": 0,
        },
        metadata={"score_kind": score_kind, "hitl_required": hitl},
        canonical_tables=[ct("Immobilisations"), ct("Widget A")])
    pipe._attach_statements(result)
    return result


def test_quarantine_recomputes_qcs_never_lowers_hitl():
    """RTE-6 — the gate recomputed the validation report and left the score alone.

    So a document could be told "these statements do not count" and still carry
    a score that counted them: the semantic layer of a QCS built on checks over
    tables the result no longer stands behind. The recomputation runs over the
    surviving statements only, and is recorded under its own key on every
    document so the before/after is always visible.
    """
    kept = _quarantine_scenario("qcs")
    meta = kept.metadata

    # The gate fired, and the recomputation is recorded.
    assert [s["gate"] for s in meta["statements"]] == ["pass", "quarantined"]
    assert "qcs_after_quarantine" in meta
    after = meta["qcs_after_quarantine"]
    assert isinstance(after, float) and 0.0 <= after <= 1.0
    assert meta["qcs_after_quarantine_grade"] in {"excellent", "good", "fair", "poor"}

    # score_kind == "qcs" → the recomputed score REPLACES the stale one, and the
    # grade moves with it rather than describing the number it used to be.
    assert kept.qcs_score == pytest.approx(after, abs=5e-4)
    assert kept.qcs_score != 0.90, "the pre-quarantine score must not survive"
    assert meta["qcs_grade"] == meta["qcs_after_quarantine_grade"]

    # RTE-10 — at Tier 3 `qcs_score` holds Q_table. A QCS must not be written
    # over a consensus measure; it is still recorded next to it.
    ens = _quarantine_scenario("q_table")
    assert ens.qcs_score == 0.90, "Q_table is left exactly as the ensemble set it"
    assert "qcs_after_quarantine" in ens.metadata

    # And the flag only ever goes up. Quarantine raises it; nothing in the
    # recomputation may put it back down, however well the survivors score.
    assert kept.metadata["hitl_required"] is True
    assert ens.metadata["hitl_required"] is True
    assert kept.metadata["hitl_reason"], "a flagged document must say why"
    started_flagged = _quarantine_scenario("qcs", hitl=True)
    assert started_flagged.metadata["hitl_required"] is True


def test_tier3_score_kind_is_q_table():
    """RTE-10 — Tier 3 stores the ensemble's Q_table in the `qcs_score` field.

    Q_table measures how far the engines agreed about the table; a QCS measures
    how good one extraction looked. Different quantities, different bands, one
    field name — and every consumer comparing them as if they were the same
    thing. The field is labelled now; the Q_table itself is untouched.
    """
    from pipeline import FinAlzePipeline, PipelineConfig

    pipe = FinAlzePipeline.__new__(FinAlzePipeline)
    pipe.config = PipelineConfig()

    tier0 = _extraction_result([_one_row_table("11202126")], 0)
    tier1 = _extraction_result([_one_row_table("11202126")], 1)
    out = pipe._tier3_ensemble(tier0, tier1, None, [])

    assert out.tier_used == 3
    assert out.metadata["score_kind"] == "q_table"
    assert round(out.qcs_score, 4) == out.metadata["ensemble"]["q_table"], \
        "the labelled field is the one the ensemble wrote — semantics unchanged"
    # One grade vocabulary across the tiers: the same four lowercase words.
    assert out.metadata["qcs_grade"] in {"excellent", "good", "fair", "poor"}

    # Every other tier says "qcs" — the label is a fact about the measure, not
    # a decoration on Tier 3.
    accepted = FinAlzePipeline._carry_hitl(_extraction_result([], 1), tier=1)
    assert accepted.metadata["score_kind"] == "qcs"


def test_the_gate_must_not_run_on_raw_per_tier_frames():
    """Guard against 'move the gate earlier'. Tier-0 emits 3-row mega-cell
    frames; a real Compte de Résultat classifies with a wide margin once the
    rows are exploded and voted, but barely at all beforehand. Gating on the
    raw frames would quarantine genuine statements — the over-eager-gate data
    loss the design exists to avoid."""
    import fitz
    from accounting.column_identifier import identify_columns
    from accounting.nomenclature import load_default_dictionary
    from accounting.statement_segmenter import _collect_table_labels

    doc = fitz.open("Samples/EF CLB 2024 Provisoire (1).pdf")
    frames = [t.to_pandas() for p in doc for t in p.find_tables().tables]
    doc.close()
    d = load_default_dictionary()

    cr_raw = frames[2]                                  # the Compte de Résultat
    layout = identify_columns(cr_raw)
    labels = [str(cr_raw.iloc[r, layout.label_col_index]).strip()
              for r in range(len(cr_raw))]
    on_raw = d.classify_statement_detailed(labels)
    assert on_raw.statement_type == "compte_resultat"
    assert on_raw.margin <= 2, (
        "if this grows, re-measure before relying on an early gate; the point is "
        "that raw per-tier frames carry far weaker evidence than voted output"
    )
    assert on_raw.passes_gate is False, "a real statement would be quarantined"

    # Same document, same classifier, richer projection → comfortably gated in.
    on_full = d.classify_statement_detailed(_collect_table_labels(frames[0]))
    assert on_full.passes_gate is True


# ===========================================================================
# Tier-1 supplementation must not fabricate ensemble consensus
# ===========================================================================


def _one_row_table(value, stype="bilan"):
    from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable
    ct = CanonicalTable(statement_type=stype)
    row = CanonicalRow(raw_text="Stocks")
    row.cells["value_n"] = CanonicalCell(raw_value=value)
    ct.rows.append(row)
    return ct


def _extraction_result(raw_canonical, tier, qcs=0.6):
    from pipeline import ExtractionResult
    return ExtractionResult(
        text="", tables=[], qcs_score=qcs, tier_used=tier, confidence_details={},
        metadata={}, canonical_tables=list(raw_canonical),
        raw_canonical_tables=list(raw_canonical))


def test_canonical_tables_record_their_source_frame():
    """Empty frames are skipped during enrichment, so the output index is NOT
    the input index — anything mapping a canonical table back to the frame it
    came from must use this rather than enumerate()."""
    from accounting.enrichment_pipeline import enrich_tables

    frames = [
        pd.DataFrame({"L": ["Stocks", "Clients", "Total des actifs"],
                      "2024": ["1", "2", "3"]}),
        pd.DataFrame(),                                    # skipped
        pd.DataFrame({"L": ["Capital", "Reserves", "Total des passifs"],
                      "2024": ["4", "5", "9"]}),
    ]
    cts = enrich_tables(frames, tier=1)
    assert [c.metadata["source_table_index"] for c in cts] == [0, 2]


def test_a_supplemented_tier0_frame_does_not_vote_as_a_second_engine():
    """A frame Docling missed is appended from Tier 0 and enriched with tier=1
    only because Tier 1 is the tier doing the enriching. Letting it vote makes
    the ensemble score one engine's data as two engines agreeing — the table is
    then auto-accepted as corroborated when nothing corroborated it."""
    from pipeline import FinAlzePipeline, PipelineConfig

    pipe = FinAlzePipeline.__new__(FinAlzePipeline)
    pipe.config = PipelineConfig()

    tier0 = _extraction_result([_one_row_table("11202126")], 0)

    leaky = _extraction_result([_one_row_table("11202126")], 1)   # duplicate votes
    out = pipe._tier3_ensemble(tier0, leaky, None, [])
    assert out.metadata["ensemble"]["source_count"] == 2
    assert out.metadata["hitl_required"] is False, "fabricated consensus auto-accepts"

    clean = _extraction_result([], 1)                             # duplicate excluded
    out = pipe._tier3_ensemble(tier0, clean, None, [])
    assert out.metadata["ensemble"]["reason"] == "single_source"
    assert out.metadata["hitl_required"] is True, \
        "a table only one engine read must earn a review"


# ===========================================================================
# Phase 1 / step 7 — cross-tier pairing by statement identity
# ===========================================================================


def _ct(stype, page, tag):
    from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable
    ct = CanonicalTable()
    ct.statement_type = stype
    ct.page_range = (page, page)
    row = CanonicalRow(raw_text=tag)
    row.cells["value_n"] = CanonicalCell(raw_value=tag)
    ct.rows.append(row)
    return ct


def test_pairing_survives_a_table_count_disagreement():
    """Positional pairing is only correct while both tiers find the same tables
    in the same order. One extra table in either tier shifts every later index,
    and the two lists then merge DIFFERENT statements cell by cell."""
    from accounting.reconciliation import _pair_by_identity

    t0 = [_ct("bilan", 1, "A"), _ct("bilan", 3, "P"), _ct("compte_resultat", 4, "C")]
    t1 = [_ct("bilan", 1, "A"), _ct("bilan", 2, "EXTRA"),
          _ct("bilan", 3, "P"), _ct("compte_resultat", 4, "C")]

    pairs, only0, only1 = _pair_by_identity(t0, t1)
    assert pairs == [(0, 0), (1, 2), (2, 3)], "p3 must pair with p3, not with p2"
    assert only0 == [] and only1 == [1]


def test_same_type_on_different_pages_never_pairs():
    """A Bilan Actif and a Bilan Passif are BOTH 'bilan' — only the page tells
    them apart, so type alone must not be enough to pair."""
    from accounting.reconciliation import _match_rank

    assert _match_rank(_ct("bilan", 1, "A"), _ct("bilan", 3, "P")) == 0
    assert _match_rank(_ct("bilan", 1, "A"), _ct("bilan", 1, "A")) > 0


def test_different_types_never_pair():
    from accounting.reconciliation import _match_rank

    assert _match_rank(_ct("bilan", 1, "A"), _ct("compte_resultat", 1, "C")) == 0


def test_pairing_degrades_to_list_order_without_page_evidence():
    """Pre-provenance results carry no page_range; behaviour must not regress."""
    from accounting.reconciliation import _pair_by_identity

    a, b = _ct("bilan", 1, "A"), _ct("bilan", 1, "B")
    a.page_range = b.page_range = None
    c, d = _ct("bilan", 1, "C"), _ct("bilan", 1, "D")
    c.page_range = d.page_range = None
    pairs, _, _ = _pair_by_identity([a, b], [c, d])
    assert pairs == [(0, 0), (1, 1)]


def test_proven_column_binding_survives_the_ensemble_vote():
    """The vote reconciles cell VALUES; it does not re-bind columns or re-run
    the scale resolver. Rebuilding metadata from scratch discarded a proven
    profile, so per-tier work never reached the output the reviewer sees."""
    from accounting.ensemble import EnsembleSource, ensemble_vote

    anchor = _ct("bilan", 1, "Stocks")
    anchor.metadata = {
        "column_profile": "n_n1_variation",
        "arithmetic_rule": "value_n - value_n_minus_1 == variation",
        "arithmetic_holds": 20,
        "x_offsets": [57.0, 201.0],
        "transient_junk": "drop me",
    }
    other = _ct("bilan", 1, "Stocks")

    voted = ensemble_vote([EnsembleSource("tier0", [anchor], 1.0),
                           EnsembleSource("tier1", [other], 0.8)])
    md = voted[0].metadata
    assert md["column_profile"] == "n_n1_variation"
    assert md["arithmetic_holds"] == 20
    assert md["x_offsets"] == [57.0, 201.0]
    assert "transient_junk" not in md, "only established facts carry forward"
    assert "q_table" in md, "the vote's own metrics must still be written"


# ===========================================================================
# Phase 1 / step 6 — multi-page continuation, guarded by statement type
# ===========================================================================
#
# THRESHOLD PROVENANCE: these fragments are AUTHORED, not observed — no sample
# in the repo contains a statement continued across a page break. The merge
# threshold must be revalidated against the first real multi-page document.
# What is NOT fixture-dependent is the veto: it is exercised below against the
# real 3-statement pack as well.

_MP_COLS = ["Actifs", "Notes", "12/31/2024", "31/12/2023"]
_MP_OFFSETS = [57.0, 201.0, 340.0, 455.0]


def _page_table(rows, page_index, cols=None, offsets=None):
    from accounting.table_merger import PageTable
    return PageTable(
        df=pd.DataFrame(rows, columns=cols or _MP_COLS),
        page_index=page_index,
        col_x_offsets=list(_MP_OFFSETS if offsets is None else offsets),
    )


def _bilan_actif_split():
    """One Bilan Actif split mid-section: the continuation repeats no header and
    the section stays open (its Total lands on the second fragment)."""
    a = _page_table([
        ["Actifs non courants", "", "", ""],
        ["Immobilisations Incorporelles", "1", "210,580", "210,580"],
        ["Immobilisations Corporelles", "2", "53,536,607", "52,938,385"],
        ["Immobilisations Financières", "3", "24,953", "25,853"],
    ], 0)
    b = _page_table([
        ["Autres actifs non courants", "4", "4,919,664", "3,500,000"],
        ["Charges reportées", "", "120,000", "95,000"],
        ["Total des actifs non courants", "", "34,415,854", "35,998,818"],
    ], 1)
    return a, b


def test_a_genuine_continuation_merges():
    from accounting.nomenclature import load_default_dictionary
    from accounting.table_merger import detect_continuation, merge_page_tables

    a, b = _bilan_actif_split()
    d = load_default_dictionary()
    sig = detect_continuation(a, b, d)
    assert sig.same_statement is True
    assert sig.is_continuation is True
    merged = merge_page_tables([a, b], d)
    assert len(merged) == 1
    assert len(merged[0]) == 7, "4 + 3 rows"


def test_different_statements_never_merge_even_at_a_passing_score():
    """THE guard. A Bilan Passif followed by a Compte de Résultat with an
    identical layout scores 2.5 — exactly the merge threshold — so every layout
    signal agrees and only the statement-type veto separates them. Without it
    two unrelated statements silently become one table."""
    from accounting.nomenclature import load_default_dictionary
    from accounting.table_merger import detect_continuation, merge_page_tables

    passif = _page_table([
        ["Capitaux Propres", "", "", ""],
        ["Capital Social", "", "34,340,000", "34,340,000"],
        ["Autres Capitaux Propres", "", "10,590,695", "10,590,695"],
        ["Passifs non Courants", "", "", ""],
        ["Total des Passifs courants", "", "110,135,404", "98,000,000"],
    ], 0, cols=["Libellés", "Notes", "12/31/2024", "31/12/2023"])
    cr = _page_table([
        ["Produits d'Exploitation", "", "", ""],
        ["Revenus", "1", "95,276,113", "88,000,000"],
        ["Autres produits d'exploitation", "2", "41,694,461", "39,000,000"],
        ["Charges de personnel", "3", "5,512,843", "5,000,000"],
    ], 1, cols=["Libellés", "Notes", "12/31/2024", "31/12/2023"])

    d = load_default_dictionary()
    sig = detect_continuation(passif, cr, d)
    assert sig.total_score >= 2.5, "the layout signals really do all agree here"
    assert sig.same_statement is False
    assert sig.veto_reason == "bilan_passif != compte_resultat"
    assert sig.is_continuation is False
    assert len(merge_page_tables([passif, cr], d)) == 2, "must stay two tables"


def test_an_unclassifiable_fragment_is_not_vetoed():
    """A real continuation page lacks the headers that identify a statement, so
    'unclassified' must mean *no evidence*, not disagreement — otherwise the
    veto would block exactly the merges it exists to permit."""
    from accounting.nomenclature import load_default_dictionary
    from accounting.table_merger import detect_continuation

    a, _ = _bilan_actif_split()
    sparse = _page_table([
        ["Divers", "", "1,000", "900"],
        ["Autres", "", "2,000", "1,800"],
    ], 1)
    sig = detect_continuation(a, sparse, load_default_dictionary())
    assert sig.same_statement is None
    assert sig.veto_reason is None


def test_the_veto_holds_on_the_real_document():
    """Not fixture-dependent: on the real 3-statement pack the Actif→Passif pair
    is vetoed by type even though both are 'bilan'."""
    import fitz
    from accounting.nomenclature import load_default_dictionary
    from accounting.table_merger import PageTable, detect_continuation

    doc = fitz.open("Samples/EF CLB 2024 Provisoire (1).pdf")
    pts = []
    for i, pg in enumerate(doc):
        for tab in pg.find_tables().tables:
            hdr = [c for c in (tab.header.cells or []) if c]
            pts.append(PageTable(df=tab.to_pandas(), page_index=i,
                                 col_x_offsets=[float(c[0]) for c in hdr]))
    doc.close()
    d = load_default_dictionary()
    sig = detect_continuation(pts[0], pts[1], d)     # Actif -> Passif
    assert sig.same_statement is False
    assert sig.is_continuation is False


# ===========================================================================
# Phase 1 / step 5 — column binding proved against the arithmetic
# ===========================================================================


def test_unroled_numeric_columns_stay_parseable():
    """Being unroled must cost a column its ROLE, never its VALUES.

    ``numeric_col_indices`` used to be the roled columns only, and enrichment
    parses a cell only when its column is listed — so a keyword miss silently
    stopped a real amount column (a Variation/Écart column, an extra period)
    from being read as a number anywhere downstream.
    """
    from accounting.column_identifier import identify_columns

    df = pd.DataFrame({
        "": ["Stocks", "Clients", "Autres"],
        "Notes": ["3", "4", ""],
        "2023": ["11202126", "25545086", "64818247"],
        "2022": ["9295099", "23000000", "60000000"],
        "Variation": ["1907027", "2545086", "4818247"],
    })
    layout = identify_columns(df)
    assert layout.role_map.get("value_n") == 2
    assert 4 in layout.numeric_col_indices, "the Variation column lost its values"


def test_note_column_is_not_readmitted_as_an_amount_column():
    """ISSUE-7a guard: a Note/Réf column of parenthesised account codes parses
    as numeric via the parens→negative branch, so the union must keep excluding
    the label and note columns."""
    from accounting.column_identifier import identify_columns

    df = pd.DataFrame({
        "Designation": ["Immobilisations", "Stocks", "Clients", "Total des actifs"],
        "Note": ["(2211)", "(1011)", "(311)", ""],
        "Exercice N": ["1000", "2000", "3000", "6000"],
    })
    layout = identify_columns(df)
    assert layout.note_col_index not in layout.numeric_col_indices
    assert layout.label_col_index not in layout.numeric_col_indices


def _comparative_frame():
    """label | Notes | N | N-1 | Variation — the common comparative layout."""
    return pd.DataFrame({
        "": ["Stocks", "Clients", "Autres actifs", "Liquidités"],
        "Notes": ["3", "4", "", "5"],
        "2023": ["11202126", "25545086", "64818247", "26177260"],
        "2022": ["9295099", "23000000", "60000000", "25000000"],
        "Variation": ["1907027", "2545086", "4818247", "1177260"],
    })


def test_arithmetic_proves_a_correct_binding():
    from accounting.column_binder import verify_rule

    v = verify_rule(_comparative_frame(),
                    {"value_n": 2, "value_n_minus_1": 3, "variation": 4},
                    "value_n - value_n_minus_1 == variation")
    assert v.proven is True
    assert v.holds == v.checked == 4


def test_arithmetic_refutes_a_sign_flipped_binding():
    from accounting.column_binder import verify_rule

    v = verify_rule(_comparative_frame(),
                    {"value_n": 3, "value_n_minus_1": 2, "variation": 4},
                    "value_n - value_n_minus_1 == variation")
    assert v.proven is False
    assert v.reason.startswith("ratio_too_low")


def test_arithmetic_refutes_a_note_column_bound_as_a_value():
    """A Notes column of short reference codes ('3', '4', …) does parse as
    numbers, so it is not rejected for lack of evidence — it is rejected because
    the identity never holds. Either way the wrong binding cannot survive."""
    from accounting.column_binder import verify_rule

    v = verify_rule(_comparative_frame(),
                    {"value_n": 1, "value_n_minus_1": 3, "variation": 4},
                    "value_n - value_n_minus_1 == variation")
    assert v.proven is False
    assert v.holds == 0


def test_a_role_pointed_at_an_empty_column_has_no_evidence():
    from accounting.column_binder import verify_rule

    df = _comparative_frame()
    df["Empty"] = ["", "", "", ""]
    v = verify_rule(df, {"value_n": 2, "value_n_minus_1": 3, "variation": 5},
                    "value_n - value_n_minus_1 == variation")
    assert v.proven is False
    assert v.reason.startswith("insufficient_rows")


def test_a_subtraction_rule_cannot_disambiguate_its_own_operands():
    """`a - b == c` and `a - c == b` are the same statement, so the identity
    constrains the SET {value_n_minus_1, variation} but not which is which.
    Header/geometry evidence must generate the binding; arithmetic only filters.
    """
    from accounting.column_binder import symmetric_roles, verify_rule

    df = _comparative_frame()
    rule = "value_n - value_n_minus_1 == variation"
    correct = verify_rule(df, {"value_n": 2, "value_n_minus_1": 3, "variation": 4}, rule)
    swapped = verify_rule(df, {"value_n": 2, "value_n_minus_1": 4, "variation": 3}, rule)
    assert correct.proven and swapped.proven, "the symmetry is real, not a bug"
    assert {"value_n_minus_1", "variation"} in symmetric_roles(rule)


def test_symmetry_covers_both_algebraic_slots():
    from accounting.column_binder import symmetric_roles

    # cross-equation move
    assert {"amortissement", "net"} in symmetric_roles("brut - amortissement == net")
    # plain commutativity within a side
    assert {"a", "b"} in symmetric_roles("a + b == c")
    # nothing ambiguous
    assert symmetric_roles("a == b") == []


def test_rows_violating_a_proven_rule_are_reported():
    """A rule holding on most rows has localised the rest — those are the cells
    worth a reviewer's attention."""
    from accounting.column_binder import verify_rule

    df = _comparative_frame()
    df.loc[1, "Variation"] = "999999999"          # break one row
    v = verify_rule(df, {"value_n": 2, "value_n_minus_1": 3, "variation": 4},
                    "value_n - value_n_minus_1 == variation")
    assert v.proven is True                        # 3/4 still clears the bar
    assert v.offending_rows == [1]


def test_headers_propose_the_roles_the_identity_needs():
    """The core matcher has no vocabulary for Variation/%, so the identity's
    operands could never be bound and the rule could never be tested."""
    from accounting.column_binder import propose_from_headers
    from accounting.nomenclature import load_default_dictionary

    df = _comparative_frame()
    cfg = load_default_dictionary().column_models["n_n1_variation"]
    proposed = propose_from_headers(
        df, cfg, {"note": 1, "value_n": 2, "value_n_minus_1": 3}, exclude={0})
    assert proposed["variation"] == 4


def test_proposing_never_overwrites_or_double_binds():
    from accounting.column_binder import propose_from_headers

    df = _comparative_frame()
    cfg = {"header_keywords": {"value_n": ["Variation"], "variation": ["Variation"]}}
    proposed = propose_from_headers(df, cfg, {"value_n": 2}, exclude={0})
    assert proposed["value_n"] == 2, "an existing binding must stand"
    assert proposed.get("variation") == 4
    assert len(set(proposed.values())) == len(proposed), "a column bound twice"


def test_header_matching_survives_a_longer_real_header():
    """`fuzz.ratio` is length-penalising, so 'Amortissements et provisions'
    scored below cutoff against 'Amort' purely for being longer."""
    from accounting.column_binder import propose_from_headers

    df = pd.DataFrame({
        "Designation": ["Immobilisations"],
        "Montant brut": ["1000"],
        "Amortissements et provisions": ["400"],
        "Valeur nette comptable": ["600"],
    })
    cfg = {"header_keywords": {"brut": ["Brut"], "amortissement": ["Amort"],
                               "net": ["Net", "Valeur nette"]}}
    proposed = propose_from_headers(df, cfg, {}, exclude={0})
    assert proposed == {"brut": 1, "amortissement": 2, "net": 3}


def test_enrichment_records_a_proven_profile_and_flags_its_violations():
    from accounting.enrichment_pipeline import enrich_tables

    df = _comparative_frame()
    df.loc[2, "Variation"] = "999999999"           # one bad cell
    ct = enrich_tables([df], tier=1)[0]
    assert ct.metadata.get("column_profile") == "n_n1_variation"
    assert ct.metadata.get("arithmetic_holds") == 3
    assert any(c.flag == "yellow" for c in ct.rows[2].cells.values()), \
        "the row breaking a proven identity must be flagged"


def test_profiles_are_scoped_by_qualified_statement_type():
    """`applies_to` keys on bilan_actif / bilan_passif — unreachable until the
    classifier could emit those."""
    from accounting.column_binder import candidate_profiles
    from accounting.nomenclature import load_default_dictionary

    d = load_default_dictionary()
    actif = [n for n, _ in candidate_profiles(d, "bilan_actif")]
    passif = [n for n, _ in candidate_profiles(d, "bilan_passif")]
    assert "brut_amort_net" in actif
    assert "brut_amort_net" not in passif, "Brut/Amort/Net is an Actif presentation"
    assert "n_n1_variation" in passif
    # Provable profiles are offered first — a hypothesis that can be tested is
    # worth trying before one that cannot.
    assert actif[0] == "brut_amort_net"


# ===========================================================================
# Phase 1 / step 4 — canonical_tables keyed per STATEMENT (schema v2)
# ===========================================================================


def _divergent_result():
    """tables and statements deliberately disagree: a blank frame sits between
    two real statements, so table index 2 maps to statement slot 1."""
    from accounting.canonical_model import CanonicalRow, CanonicalTable
    from pipeline import ExtractionResult, FinAlzePipeline

    def ct(tag):
        t = CanonicalTable()
        t.rows = [CanonicalRow(raw_text=tag)]
        return t

    bilan = pd.DataFrame({
        "L": ["Actifs non courants", "Immobilisations Incorporelles",
              "Immobilisations Corporelles", "Total des actifs immobilises"],
        "2024": ["1", "2", "3", "6"]})
    passif = pd.DataFrame({
        "L": ["Capitaux Propres", "Capital Social", "Passifs non Courants",
              "Total des Passifs courants", "Total des Capitaux Propres et Passifs"],
        "2024": ["1", "2", "3", "4", "5"]})

    r = ExtractionResult(
        text="", qcs_score=0.9, tier_used=1, confidence_details={}, metadata={},
        tables=[bilan.to_dict("records"), [], passif.to_dict("records")],
        canonical_tables=[ct("T0"), ct("T1-blank"), ct("T2")])
    FinAlzePipeline.__new__(FinAlzePipeline)._attach_statements(r)
    return r


def test_canonical_tables_are_keyed_per_statement():
    r = _divergent_result()
    assert r.metadata["schema_version"] == 2
    assert [s["table_indices"] for s in r.metadata["statements"]] == [[0], [2]]
    assert [c.rows[0].raw_text for c in r.canonical_tables] == ["T0", "T2"]


def test_table_index_resolves_through_the_back_pointer(monkeypatch):
    """The grid, the overlay and the stored corrections are all keyed on TABLE
    index; storage is keyed per statement. The back-pointer is the bridge."""
    import types
    import hitl.app as app
    monkeypatch.setattr(app, "st", types.SimpleNamespace(session_state={}))

    r = _divergent_result()
    assert app._canonical_dict(r, 0)["rows"][0]["raw_text"] == "T0"
    assert app._canonical_dict(r, 2)["rows"][0]["raw_text"] == "T2"


def test_a_table_covered_by_no_statement_resolves_to_nothing(monkeypatch):
    """Regression: the positional fallback fired even when an inventory existed,
    so the blank frame at table 1 returned statement slot 1 — the NEXT
    statement's canonical data, silently attributed to the wrong table."""
    import types
    import hitl.app as app
    monkeypatch.setattr(app, "st", types.SimpleNamespace(session_state={}))

    r = _divergent_result()
    assert app._canonical_slot(r, 1) is None
    assert app._canonical_dict(r, 1) is None


def test_pre_v2_results_still_resolve_positionally(monkeypatch):
    """A stored result from before the statement stage has no inventory; the
    identity mapping must still work."""
    import types
    import hitl.app as app
    from accounting.canonical_model import CanonicalRow, CanonicalTable
    from pipeline import ExtractionResult
    monkeypatch.setattr(app, "st", types.SimpleNamespace(session_state={}))

    def ct(tag):
        t = CanonicalTable()
        t.rows = [CanonicalRow(raw_text=tag)]
        return t

    old = ExtractionResult(text="", tables=[], qcs_score=0.9, tier_used=1,
                           confidence_details={}, metadata={},
                           canonical_tables=[ct("A"), ct("B")])
    assert app._canonical_dict(old, 0)["rows"][0]["raw_text"] == "A"
    assert app._canonical_dict(old, 1)["rows"][0]["raw_text"] == "B"


# ===========================================================================
# Phase 1 / step 3 — StatementBuilder + quarantine gate
# ===========================================================================


def _bilan_actif_frame():
    return pd.DataFrame({
        "L": ["Actifs non courants", "Immobilisations Incorporelles",
              "Immobilisations Corporelles", "Total des actifs immobilises"],
        "2024": ["1", "2", "3", "6"],
    })


def _not_a_statement_frame():
    return pd.DataFrame({"L": ["Widget A", "Widget B", "Sundry"],
                         "2024": ["1", "2", "3"]})


def test_builder_inventories_one_statement_per_frame():
    from accounting.statement_builder import build_statements

    units = build_statements([_bilan_actif_frame()],
                             provenance=[{"page_no": 1, "x_offsets": [10.0]}])
    assert len(units) == 1
    assert units[0].qualified_type == "bilan_actif"
    assert units[0].gate == "pass"
    assert units[0].pages == [1]


def test_blank_frame_yields_no_statement_rather_than_a_quarantine():
    """A blank page is not an unclassified statement — quarantining it would put
    an empty grid in front of a reviewer."""
    from accounting.statement_builder import build_statements

    units = build_statements([pd.DataFrame(), _bilan_actif_frame()],
                             provenance=[{"page_no": 2}, {"page_no": 1}])
    assert len(units) == 1
    assert units[0].table_indices == [1], "back-pointer must survive the skip"


def test_unrecognized_statement_is_quarantined_with_a_reason():
    from accounting.statement_builder import build_statements

    units = build_statements([_not_a_statement_frame()])
    assert len(units) == 1
    assert units[0].gate == "quarantined"
    assert units[0].statement_type is None
    assert units[0].gate_reason is not None
    # The legacy verdict is retained so the decision can be audited.
    assert "legacy_type" in units[0].evidence


def test_statement_ids_renumber_but_table_indices_do_not():
    from accounting.statement_builder import build_statements

    units = build_statements(
        [_bilan_actif_frame(), pd.DataFrame(), _not_a_statement_frame()])
    assert [u.id for u in units] == [0, 1]
    assert [u.table_indices for u in units] == [[0], [2]]


def test_mismatched_provenance_is_ignored_not_misapplied():
    """A provenance list that disagrees with the table count would attribute
    pages to the wrong statements — worse than having none."""
    from accounting.statement_builder import build_statements

    units = build_statements([_bilan_actif_frame()],
                             provenance=[{"page_no": 1}, {"page_no": 2}])
    assert units[0].pages == []


def test_attach_statements_only_ever_raises_hitl(monkeypatch):
    """Advisory stage: it may add review, never remove it."""
    from pipeline import ExtractionResult, FinAlzePipeline

    pipe = FinAlzePipeline.__new__(FinAlzePipeline)   # no model init needed

    clean = ExtractionResult(text="", tables=[_bilan_actif_frame().to_dict("records")],
                             qcs_score=0.9, tier_used=1, confidence_details={},
                             metadata={"hitl_required": False})
    pipe._attach_statements(clean)
    assert clean.metadata["hitl_required"] is False
    assert len(clean.metadata["statements"]) == 1

    dirty = ExtractionResult(text="", tables=[_not_a_statement_frame().to_dict("records")],
                             qcs_score=0.9, tier_used=1, confidence_details={},
                             metadata={"hitl_required": False})
    pipe._attach_statements(dirty)
    assert dirty.metadata["hitl_required"] is True, "quarantine must force review"

    # Already flagged for review — must stay flagged.
    was_true = ExtractionResult(text="", tables=[_bilan_actif_frame().to_dict("records")],
                                qcs_score=0.9, tier_used=1, confidence_details={},
                                metadata={"hitl_required": True})
    pipe._attach_statements(was_true)
    assert was_true.metadata["hitl_required"] is True


# ===========================================================================
# Phase 1 / step 2 — classification confidence, margin, actif/passif
# ===========================================================================


def _dict():
    from accounting.nomenclature import load_default_dictionary
    return load_default_dictionary()


def test_string_classifier_semantics_are_unchanged():
    """Step 2 is strictly additive — the legacy verdict must not move, or every
    existing caller silently changes behaviour."""
    d = _dict()
    actif = ["Actifs non courants", "Immobilisations Incorporelles",
             "Immobilisations Corporelles", "Total des actifs immobilisés"]
    assert d.classify_statement(actif) == "bilan"
    assert d.classify_statement_detailed(actif).statement_type == "bilan"
    assert d.classify_statement([]) == "unknown"


def test_bilan_actif_and_passif_are_reachable():
    """``classify_statement``'s score dict has only three keys, so bilan_actif /
    bilan_passif could never be emitted — which is why the YAML's
    ``applies_to: [bilan_actif]`` matched nothing."""
    d = _dict()
    actif = d.classify_statement_detailed(
        ["Actifs non courants", "Actifs courants", "Immobilisations Incorporelles",
         "Immobilisations Corporelles", "Total des actifs immobilisés"])
    passif = d.classify_statement_detailed(
        ["Capitaux Propres", "Capital Social", "Passifs non Courants",
         "Total des Passifs courants", "Total des Capitaux Propres et Passifs"])
    assert actif.qualified_type == "bilan_actif", actif.scores
    assert passif.qualified_type == "bilan_passif", passif.scores


def test_booster_only_verdict_is_reported_without_term_evidence():
    """A single +3 structural family can carry a verdict on its own. The tally
    must expose that: this CR fragment scores as 'bilan' with ZERO bilan terms,
    while the only real matches point at compte_resultat."""
    d = _dict()
    frag = ["Dotations aux amortissements et aux provisions",
            "Autres charges ordinaires", "Charges financieres nettes"]
    c = d.classify_statement_detailed(frag)
    assert c.term_scores.get("bilan", 0) == 0, "no real bilan vocabulary here"
    assert c.booster_scores.get("bilan", 0) > 0, "carried by the amort booster"
    assert c.passes_gate is False
    assert c.gate_reason is not None


def test_gate_requires_score_margin_and_term_evidence():
    from accounting.nomenclature import Classification

    solid = Classification(statement_type="bilan", scores={"bilan": 9, "compte_resultat": 1},
                           term_scores={"bilan": 6}, winner="bilan", margin=8)
    assert solid.passes_gate is True and solid.gate_reason is None

    thin_margin = Classification(statement_type="bilan", scores={"bilan": 3, "compte_resultat": 2},
                                 term_scores={"bilan": 3}, winner="bilan", margin=1)
    assert thin_margin.passes_gate is False
    assert thin_margin.gate_reason == "margin_below_threshold"

    boosters_only = Classification(statement_type="bilan", scores={"bilan": 6},
                                   term_scores={"bilan": 0}, winner="bilan", margin=6)
    assert boosters_only.passes_gate is False
    assert boosters_only.gate_reason == "no_term_evidence"

    unclassified = Classification(statement_type="unknown")
    assert unclassified.passes_gate is False
    assert unclassified.gate_reason == "unclassified"


def test_segmenter_delegates_side_detection_to_the_dictionary():
    """The duplicated actif/passif vocabulary is gone; the segmenter must still
    report the side."""
    from accounting.statement_segmenter import classify_table

    df = pd.DataFrame({
        "Libellé": ["Capitaux Propres", "Capital Social", "Passifs non Courants",
                    "Total des Passifs courants", "Total des Capitaux Propres et Passifs"],
        "2024": ["1", "2", "3", "4", "5"],
    })
    assert classify_table(df).sub_type == "passif"


# ===========================================================================
# Phase 1 / step 1 — extraction provenance (page + column x-offsets)
# ===========================================================================


def _prov_frames():
    return [
        pd.DataFrame({"L": ["Immobilisations", "Stocks", "Total des actifs"],
                      "2024": ["100", "200", "300"]}),
        pd.DataFrame({"L": ["Capital", "Reserves", "Total des passifs"],
                      "2024": ["400", "500", "900"]}),
    ]


def test_provenance_assigns_page_range():
    """``CanonicalTable.page_range`` has always been declared, serialized and
    propagated by reconciliation/ensemble — but never actually assigned, so it
    was invariably None."""
    from accounting.enrichment_pipeline import enrich_tables

    prov = [{"page_no": 1, "x_offsets": [78.4, 333.4]},
            {"page_no": 3, "x_offsets": [78.8, 320.1]}]
    out = enrich_tables(_prov_frames(), tier=0, provenance=prov)
    assert [ct.page_range for ct in out] == [(1, 1), (3, 3)]
    assert out[0].metadata["x_offsets"] == [78.4, 333.4]


def test_provenance_is_optional():
    """Back-compat: every existing caller omits it."""
    from accounting.enrichment_pipeline import enrich_tables

    out = enrich_tables(_prov_frames(), tier=0)
    assert all(ct.page_range is None for ct in out)


def test_provenance_keys_on_the_input_index_not_the_output_index():
    """``enrich_tables`` drops empty frames, so the output index slides relative
    to the input. Provenance must follow the INPUT index or every page number
    after an empty frame is attributed to the wrong table."""
    from accounting.enrichment_pipeline import enrich_tables

    frames = [pd.DataFrame()] + _prov_frames()
    prov = [{"page_no": 9, "x_offsets": []},      # the skipped frame
            {"page_no": 1, "x_offsets": []},
            {"page_no": 3, "x_offsets": []}]
    out = enrich_tables(frames, tier=0, provenance=prov)
    assert [ct.page_range for ct in out] == [(1, 1), (3, 3)], "page 9 leaked in"


# ===========================================================================
# P0-10 — a deduction is "already in the net" only when a net row exists
# ===========================================================================


def _row_sums(labels, values):
    from accounting.validator import validate_row_sums
    df = pd.DataFrame({"L": labels, "V": [str(v) for v in values]})
    return [r for r in validate_row_sums(df) if r.check_name.startswith("Row Sum:")]


def test_deductions_are_addends_when_no_net_row_exists():
    """The common NCT Actif block runs gross → 'Moins: Amortissement'
    (negative) → total, with no VNC row. Excluding the deductions there
    over-counted the total by exactly their sum."""
    checks = _row_sums(
        ["Immobilisations Incorporelles", "Moins: Amortissement",
         "Immobilisations Corporelles", "Moins: Amortissement",
         "Immobilisations Financieres", "Total des actifs immobilises"],
        [210580, -166074, 53536607, -24109876, 24953, 29496190],
    )
    assert checks and checks[0].passed is True, (
        f"expected={checks[0].expected} actual={checks[0].actual}" if checks else "no check"
    )


def test_deductions_stay_excluded_when_a_net_row_exists():
    """Brut/Amort/VNC triplets must still foot on the NET rows only — summing
    all three double-counts (Brut + (−Amort) + VNC = 2·VNC)."""
    checks = _row_sums(
        ["Immo Incorporelles", "Amortissements", "VNC",
         "Immo Corporelles", "Amortissements", "VNC",
         "Total des actifs immobilises"],
        [1000, -400, 600, 2000, -500, 1500, 2100],
    )
    assert checks and checks[0].passed is True
    assert checks[0].actual == 2100.0, "must sum the VNC rows only"


def test_provisions_count_as_deductions_too():
    """'Moins: Provisions' classifies as amort and behaves identically."""
    checks = _row_sums(
        ["Clients", "Moins: Provisions", "Autres Actifs Courants",
         "Total des actifs courants"],
        [25545086, -1085961, 64818247, 89277372],
    )
    assert checks and checks[0].passed is True


def test_row_sum_still_catches_a_real_error():
    checks = _row_sums(["Poste A", "Poste B", "Total des postes"], [100, 200, 999])
    assert checks and checks[0].passed is False


# ===========================================================================
# P0-9 — table-level comma convention
# ===========================================================================


def test_grouped_table_has_commas_stripped():
    """Two commas in one token prove the comma groups thousands, and that
    settles every number in the same table — including the single-comma ones a
    token-level parser reads as decimals (210,580 -> 210.58, a 1000x error)."""
    from accounting.validator import normalize_comma_thousands, extract_numeric_value

    df = pd.DataFrame({
        "Libellé": ["Immobilisations", "Incorporelles"],
        "2024": ["53,536,607", "210,580"],
    })
    assert extract_numeric_value("53,536,607") is None      # the bug
    assert extract_numeric_value("210,580") == 210.58       # the 1000x error

    out = normalize_comma_thousands(df)
    assert list(out["2024"]) == ["53536607", "210580"]
    assert extract_numeric_value(out["2024"].iloc[0]) == 53536607.0
    assert extract_numeric_value(out["2024"].iloc[1]) == 210580.0


def test_comma_decimal_table_is_left_alone():
    """A genuinely French-decimal table must not be touched — stripping there
    would turn 1 234,56 into 123456."""
    from accounting.validator import normalize_comma_thousands

    df = pd.DataFrame({"L": ["A", "B"], "V": ["1 234,56", "9 876,10"]})
    assert list(normalize_comma_thousands(df)["V"]) == ["1 234,56", "9 876,10"]


def test_percentage_cells_keep_their_decimal_comma():
    """A percentage is a decimal regardless of how the table groups amounts;
    stripping there would read 4,1% as 41%."""
    from accounting.validator import normalize_comma_thousands

    df = pd.DataFrame({"L": ["Immo", "Var"], "V": ["53,536,607", "4,1%"]})
    out = normalize_comma_thousands(df)
    assert list(out["V"]) == ["53536607", "4,1%"]


def test_table_without_commas_is_unchanged():
    from accounting.validator import normalize_comma_thousands

    df = pd.DataFrame({"L": ["A"], "V": ["34 340 000"]})
    assert list(normalize_comma_thousands(df)["V"]) == ["34 340 000"]


# ===========================================================================
# P0-6 / P0-7 — a check that cannot establish its inputs must SKIP, not FAIL
# ===========================================================================


def test_unlocatable_resultat_skips_instead_of_raising_critical():
    """P0-6: 'Could not find Resultat' was reported as a CRITICAL arithmetic
    failure, which sets arithmetic_fail in the Tier-3 ensemble and forces HITL
    on a correctly extracted document."""
    from accounting.rule_engine import validate_cross_statement
    from accounting.statement_segmenter import StatementSegment

    # Generic Tier-0 headers: no value column can be identified.
    bilan = pd.DataFrame({
        "Bilan au 31/12/2024": ["Capital Social", "Résultat de l'exercice"],
        "Col1": ["", ""], "Col2": ["", ""],
    })
    cr = pd.DataFrame({
        "Etat de Résultat": ["Revenus", "Résultat Net de l'Exercice"],
        "Col1": ["", ""], "Col2": ["", ""],
    })
    grouped = {
        "bilan": [StatementSegment(0, "bilan", 0.8, 5, bilan, "passif")],
        "bilan_passif": [StatementSegment(0, "bilan", 0.8, 5, bilan, "passif")],
        "compte_resultat": [StatementSegment(1, "compte_resultat", 0.8, 5, cr)],
        "flux_tresorerie": [],
    }
    checks = validate_cross_statement(grouped)
    resultat = [c for c in checks if "Resultat Net Match" in c.check_name]
    for c in resultat:
        assert c.skipped is True, "an unlocatable Résultat must skip, not fail"


def test_cr_equation_skips_when_a_component_is_missing():
    """P0-7: missing components were coerced to 0.0 and the check then failed
    against a total it never established."""
    from accounting.rule_engine import validate_cr_equation

    # "Charges financières nettes" / "Impôts sur le résultat" match none of the
    # keywords, so both become 0 and the equation cannot balance.
    df = pd.DataFrame({
        "Libellés": ["Résultat d'exploitation", "Charges financières nettes",
                     "Impôts sur le résultat", "Résultat Net de l'Exercice"],
        "12/31/2024": ["7356384", "11988557", "0", "-2972966"],
    })
    checks = validate_cr_equation(df)
    eq = [c for c in checks if "CR Equation" in c.check_name]
    assert eq, "the check should still report something"
    assert all(c.skipped for c in eq), "must skip rather than raise a CRITICAL"


def test_cr_equation_still_passes_when_components_are_genuinely_absent():
    """Coverage must not be lost: if the equation balances with the components
    absent, they really were absent and the pass stands."""
    from accounting.rule_engine import validate_cr_equation

    df = pd.DataFrame({
        "Libellés": ["Résultat d'exploitation", "Résultat Net de l'Exercice"],
        "12/31/2024": ["7356384", "7356384"],
    })
    eq = [c for c in validate_cr_equation(df) if "CR Equation" in c.check_name]
    assert eq and eq[0].passed is True
    assert eq[0].skipped is False


def test_a_real_cr_equation_error_still_fails():
    """The skip must not rescue a genuine mismatch when every component IS
    found."""
    from accounting.rule_engine import validate_cr_equation

    df = pd.DataFrame({
        "Libellés": ["Résultat d'exploitation", "Résultat financier",
                     "Impots sur les benefices", "Résultat Net de l'Exercice"],
        "12/31/2024": ["1000", "500", "100", "9999"],
    })
    eq = [c for c in validate_cr_equation(df) if "CR Equation" in c.check_name]
    assert eq and eq[0].passed is False
    assert eq[0].skipped is False, "all components found — a real failure"


def _app_with_boxes(monkeypatch, table_boxes, orientation_transform=None):
    """Import the HITL app with a stubbed Streamlit session holding geometry.

    ``orientation_transform`` is the WP-2.1/WP-2.2 record; in API mode it is all
    the UI ever receives about the frame the geometry was captured in (OCR-6).
    """
    import types
    import hitl.app as app

    class _SS(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    metadata = {"table_boxes": table_boxes}
    if orientation_transform is not None:
        metadata["orientation_transform"] = orientation_transform
    result = types.SimpleNamespace(metadata=metadata)
    monkeypatch.setattr(
        app, "st",
        types.SimpleNamespace(
            session_state=_SS(extraction_result=result, table_segments=[])
        ),
    )
    return app


def test_pdf_locator_is_a_page(monkeypatch):
    app = _app_with_boxes(monkeypatch, [{"page_no": 3}])
    assert app._table_source_locator(0) == ("page", 3)
    assert app._table_source_page(0) == 3
    assert app._table_label(0).endswith("· p.3")


def test_docx_locator_is_an_image_not_a_bogus_page(monkeypatch):
    """P0-4: Docling converts each embedded DOCX image as its own single-page
    document, so page_no is 1 for EVERY table. Reading it alone labelled them
    all '· p.1'."""
    app = _app_with_boxes(monkeypatch, [{"page_no": 1, "image_index": 1}])
    assert app._table_source_locator(0) == ("image", 1)
    assert app._table_source_page(0) is None, "DOCX must not report a page"
    assert app._table_label(0).endswith("· img 2")


def test_missing_geometry_yields_no_locator(monkeypatch):
    app = _app_with_boxes(monkeypatch, [None])
    assert app._table_source_locator(0) is None
    assert "·" not in app._table_label(0)


# --- OCR-6: one frame for extraction and overlay ---------------------------

# The record fis_innov actually produced: 18 pages, page-level /Rotate 270,
# CropBox == MediaBox so nothing was promoted. String keys on purpose — in API
# mode the metadata reaches the UI as JSON.
_FIS_INNOV_XF = {
    "applied": True, "skipped_pages": [], "temp_path": "/tmp/derot_x.pdf",
    "pages": {str(n): {"rotation": 270, "inherited": False,
                       "mediabox": [0.0, 0.0, 842.04, 595.44],
                       "cropbox": [0.0, 0.0, 842.04, 595.44], "promoted": False}
              for n in range(1, 19)},
}


def test_overlay_uses_transform_record(monkeypatch):
    """The overlay decides its frame from the metadata record — the only thing
    that reaches the UI in API mode, where the worker's de-rotated temp copy is
    long gone. A render already in the de-rotated frame is left alone; one in the
    stored frame gets the boxes mapped back."""
    app = _app_with_boxes(monkeypatch, [{"page_no": 1}],
                          orientation_transform=_FIS_INNOV_XF)
    boxes = [{"l": 100, "t": 50, "r": 200, "b": 90,
              "page_w": 595.44, "page_h": 842.04, "page_no": 1}]

    # fitz.get_pixmap honours /Rotate, so the page rendered from the ORIGINAL is
    # the de-rotated page — same frame, nothing to correct.
    assert app._boxes_in_render_frame(list(boxes), 1, 595.44, 842.04) == boxes
    # A render with the rotation stripped is the stored frame → invert.
    moved = app._boxes_in_render_frame(list(boxes), 1, 842.04, 595.44)
    assert moved != boxes
    assert (moved[0]["page_w"], moved[0]["page_h"]) == (842.04, 595.44)
    assert round(moved[0]["l"], 2) == 752.04 and round(moved[0]["t"], 2) == 100.0
    # A page the record does not mention is never touched.
    assert app._boxes_in_render_frame(list(boxes), 99, 842.04, 595.44) == boxes
    # No record at all → identity, whatever the render.
    plain = _app_with_boxes(monkeypatch, [{"page_no": 1}])
    assert plain._boxes_in_render_frame(list(boxes), 1, 842.04, 595.44) == boxes


def test_fis_innov_overlay_needs_no_inversion(monkeypatch):
    """MEASURED at bf6967f — pins the verdict so a later change cannot silently
    flip it.

    fis_innov is 18 pages of page-level /Rotate 270, so WP-2.1 extracts a
    de-rotated copy and every `table_boxes` entry came back 595.44 x 842.04 —
    the dimensions swapped. `fitz.Page.get_pixmap()` honours /Rotate and
    `Page.remove_rotation()` is appearance-preserving, so the page the HITL view
    renders from the ORIGINAL is byte-for-byte that de-rotated page
    (page.rect = 595.44 x 842.04, pixmap 1241 x 1755 either way). The overlay
    therefore lands on the cells already — scratch evidence
    `fis_innov_p5_overlay.png` — and inverting it unconditionally scattered every
    box (`fis_innov_p5_overlay_IF_INVERTED.png`). It must stay the identity.
    """
    from hitl import review_view

    app = _app_with_boxes(monkeypatch, [{"page_no": 5}],
                          orientation_transform=_FIS_INNOV_XF)
    render_w, render_h = 595.44, 842.04  # page.rect of the ORIGINAL, measured
    boxes = [{"l": 54.5, "t": 214.3, "r": 195.2, "b": 231.0,
              "page_w": 595.44, "page_h": 842.04, "page_no": 5}]

    for page_no in range(1, 19):
        assert review_view.derotated_page_size(_FIS_INNOV_XF, page_no) == \
            (595.44, 842.04), "the de-rotated page IS what the view renders"
        assert app._boxes_in_render_frame(list(boxes), page_no,
                                          render_w, render_h) == boxes, \
            f"page {page_no}: a correct overlay must not be 'corrected'"


def test_exif_transposed_image_preview_is_turned_upright(monkeypatch):
    """The image branch is the half that WAS broken (measured on an
    orientation-6 JPEG: the highlight landed outside the table). WP-2.2 turns the
    image upright before OCR, so `table_boxes` are upright while `Image.open`
    returns the stored sideways buffer — the preview has to be transposed to
    match. Only when the record says the transpose actually happened: the
    fail-open path in `_preprocess_image` records `transposed: False` and leaves
    the geometry in the stored frame."""
    def _app(images):
        return _app_with_boxes(monkeypatch, [{"page_no": 1}],
                               orientation_transform={"images": images})

    assert _app([{"exif_orientation": 6, "transposed": True}])._image_was_exif_transposed()
    assert not _app([{"exif_orientation": 6, "transposed": False}])._image_was_exif_transposed()
    assert not _app([{"exif_orientation": None, "transposed": False}])._image_was_exif_transposed()
    assert not _app([])._image_was_exif_transposed()
    # A PDF record carries no "images" key at all.
    assert not _app_with_boxes(
        monkeypatch, [{"page_no": 1}],
        orientation_transform=_FIS_INNOV_XF)._image_was_exif_transposed()
    # DOCX: one record per embedded image, addressed by image_index.
    docx = _app([{"exif_orientation": 1, "transposed": False, "image_index": 0},
                 {"exif_orientation": 8, "transposed": True, "image_index": 1}])
    assert docx._image_was_exif_transposed(image_index=1)
    assert not docx._image_was_exif_transposed(image_index=0)
    assert not docx._image_was_exif_transposed(image_index=7)


def test_derived_figure_is_left_unchecked_not_failed():
    """"Résultat d'exploitation" is produits MINUS charges — no summation
    hypothesis can foot it, so it must stay unchecked rather than raise a
    false CRITICAL."""
    df, layout, parsed = _footing_case(
        ["Revenus", "Total des produits", "Achats", "Total des charges",
         "Total de l'exploitation"],
        [136_970_574, 136_970_574, 129_614_190, 129_614_190, 7_356_384],
    )
    results = detect_subtotals(df, layout, None, None, parsed)
    derived = [r for r in results if r.row_index == 4][0]
    assert derived.footing_valid is None, (
        "a derived (non-sum) figure must not be reported as a footing failure"
    )
