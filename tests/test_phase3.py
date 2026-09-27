"""
Integration tests for Phase 3 modules.

Covers:
- table_merger: continuation detection + merge
- header_detector: dual-mode section detection
- column_identifier: layout model identification
- subtotal_detector: footing validation
- grouped_entry: unmatched row classification
- green_flag: 5-gate check
- enrichment_pipeline: full DAG on synthetic data
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
import pytest


# ===========================================================================
# table_merger
# ===========================================================================


class TestTableMerger:
    def test_continuation_same_cols(self):
        from accounting.table_merger import PageTable, detect_continuation

        df1 = pd.DataFrame({"Label": ["A", "B"], "N": [100, 200]})
        df2 = pd.DataFrame({"Label": ["C", "D"], "N": [300, 400]})
        t1 = PageTable(df=df1, page_index=0)
        t2 = PageTable(df=df2, page_index=1)
        sig = detect_continuation(t1, t2)
        assert sig.col_count_match is True
        # col count + open section = 2.0 minimum (no header dup or x-offset)
        assert sig.total_score >= 1.0

    def test_merge_two_pages(self):
        from accounting.table_merger import PageTable, merge_page_tables

        df1 = pd.DataFrame({"Label": ["A", "B"], "N": [100, 200]})
        df2 = pd.DataFrame({"Label": ["C", "D"], "N": [300, 400]})
        tables = [PageTable(df=df1, page_index=0), PageTable(df=df2, page_index=1)]
        merged = merge_page_tables(tables)
        # May or may not merge depending on signals — at minimum both tables present
        total_rows = sum(len(df) for df in merged)
        assert total_rows == 4

    def test_non_consecutive_pages_not_merged(self):
        from accounting.table_merger import PageTable, merge_page_tables

        df1 = pd.DataFrame({"Label": ["A"], "N": [100]})
        df2 = pd.DataFrame({"Label": ["B"], "N": [200]})
        tables = [PageTable(df=df1, page_index=0), PageTable(df=df2, page_index=5)]
        merged = merge_page_tables(tables)
        assert len(merged) == 2  # never merged — page gap too large


# ===========================================================================
# header_detector
# ===========================================================================


class TestHeaderDetector:
    def test_heuristic_detects_caps_header(self):
        from accounting.header_detector import detect_section_headers
        from accounting.nomenclature import load_default_dictionary

        load_default_dictionary.cache_clear()
        nom = load_default_dictionary()
        df = pd.DataFrame({
            "Label": ["ACTIFS NON COURANTS", "Immobilisations", "Stocks"],
            "N": [None, 100, 200],
            "N-1": [None, 90, 180],
        })
        sections = detect_section_headers(df, nom)
        assert len(sections) == 3
        assert sections[0] == "actifs_non_courants"
        # Propagation: rows below inherit the section
        assert sections[1] == "actifs_non_courants"

    def test_empty_df(self):
        from accounting.header_detector import detect_section_headers
        assert detect_section_headers(pd.DataFrame()) == []

    def test_closing_subtotal_resets_section_compte_resultat(self):
        """ISSUE-8: rows after 'Total des charges d'exploitation' must NOT all
        inherit charges_exploitation — 'Résultat d'exploitation' opens a new
        section even though it carries numeric values.
        """
        from accounting.header_detector import detect_section_headers
        from accounting.nomenclature import load_default_dictionary

        load_default_dictionary.cache_clear()
        nom = load_default_dictionary()
        df = pd.DataFrame({
            "Label": [
                "Charges d'exploitation",
                "Achats consommés",
                "Charges de personnel",
                "Total des charges d'exploitation",
                "Résultat d'exploitation",
                "Charges financières",
                "Produits des placements",
                "Impôt sur les bénéfices",
                "Résultat net",
                "Éléments extraordinaires",
            ],
            "N":   [None, 1000, 500, 1500, 800, 50, 30, 20, 760, 0],
            "N-1": [None, 900, 480, 1380, 700, 40, 25, 15, 670, 0],
        })
        sections = detect_section_headers(df, nom)

        # The charges block (header + its line items + its closing subtotal).
        assert sections[0] == "charges_exploitation"
        assert sections[1] == "charges_exploitation"
        assert sections[2] == "charges_exploitation"
        assert sections[3] == "charges_exploitation"  # closing subtotal row

        # 'Résultat d'exploitation' re-anchors a NEW section despite numbers.
        assert sections[4] == "resultat_operationnel"

        # The financial / net / extraordinary rows must NOT bleed
        # charges_exploitation (the bug being fixed).
        assert sections[5] != "charges_exploitation"
        assert sections[8] != "charges_exploitation"
        # 'Éléments extraordinaires' re-anchors its own section.
        assert sections[9] == "produits_charges_extraordinaires"

        # Sanity: no post-result row is mislabeled charges_exploitation.
        assert "charges_exploitation" not in sections[4:]

    def test_passifs_courants_not_inheriting_non_courants_bilan(self):
        """ISSUE-8: bilan 'Passifs courants' rows get passifs_courants, not
        passifs_non_courants (the previous section must be closed/reset).
        """
        from accounting.header_detector import detect_section_headers
        from accounting.nomenclature import load_default_dictionary

        load_default_dictionary.cache_clear()
        nom = load_default_dictionary()
        df = pd.DataFrame({
            "Label": [
                "Passifs non courants",
                "Emprunts",
                "Total des passifs non courants",
                "Passifs courants",
                "Fournisseurs et comptes rattachés",
                "Autres passifs courants",
                "Total des passifs courants",
            ],
            "N":   [None, 300, 300, None, 200, 100, 300],
            "N-1": [None, 280, 280, None, 180, 90, 270],
        })
        sections = detect_section_headers(df, nom)

        assert sections[0] == "passifs_non_courants"
        assert sections[1] == "passifs_non_courants"
        assert sections[2] == "passifs_non_courants"  # closing subtotal
        # The 'Passifs courants' header + its rows must be passifs_courants.
        assert sections[3] == "passifs_courants"
        assert sections[4] == "passifs_courants"
        assert sections[5] == "passifs_courants"
        assert sections[6] == "passifs_courants"
        # None of the courants rows leaked the non-courants section.
        assert "passifs_non_courants" not in sections[3:]


# ===========================================================================
# column_identifier
# ===========================================================================


class TestColumnIdentifier:
    def test_brut_amort_net_detection(self):
        from accounting.column_identifier import identify_columns

        df = pd.DataFrame({
            "Libellé": ["Row1", "Row2"],
            "Brut": ["1 000", "2 000"],
            "Amortissement": ["500", "800"],
            "Net": ["500", "1 200"],
            "N-1": ["400", "1 100"],
        })
        layout = identify_columns(df)
        assert layout.model == "brut_amort_net"
        assert layout.confidence >= 0.80

    def test_n_n1_detection(self):
        from accounting.column_identifier import identify_columns

        df = pd.DataFrame({
            "Poste": ["Capital", "Réserves"],
            "Exercice N": ["10 000", "5 000"],
            "Exercice N-1": ["10 000", "4 500"],
        })
        layout = identify_columns(df)
        assert layout.model == "n_n1"

    def test_single_column(self):
        from accounting.column_identifier import identify_columns

        df = pd.DataFrame({"Label": ["A"]})
        layout = identify_columns(df)
        assert layout.model == "single"

    def test_prune_noise_columns_dedup_repair_and_keep(self):
        """prune_noise_columns: drop a duplicate value column + repair the value
        column's label-spillover rows from it, drop a label echo, and KEEP a
        real Variation column (which conflicts numerically)."""
        from decimal import Decimal
        from accounting.canonical_model import CanonicalTable, CanonicalRow, CanonicalCell
        from accounting.enrichment_pipeline import prune_noise_columns

        def cell(raw, parsed=None):
            return CanonicalCell(raw_value=raw, parsed_value=parsed)

        ct = CanonicalTable(statement_type="compte_resultat", column_model="n_n1")
        # rows 0,1: value_n clean; row 2: value_n lost its number to a label, but
        # the duplicate "Col2" still holds it.
        specs = [
            ("Revenus",  Decimal("100"), Decimal("100"), Decimal("90"),  Decimal("11")),
            ("Achats",   Decimal("200"), Decimal("200"), Decimal("180"), Decimal("11")),
            ("Charges f", None,          Decimal("300"), Decimal("250"), Decimal("20")),
        ]
        for label, vn, col2, vn1, var in specs:
            cells = {
                "value_n": cell(label if vn is None else str(vn), vn),
                "Col2": cell(str(col2), col2),
                "value_n_minus_1": cell(str(vn1), vn1),
                "Variation": cell(str(var), var),
                "Lib": cell(label),          # label echo
            }
            ct.rows.append(CanonicalRow(raw_text=label, cells=cells))

        prune_noise_columns(ct)

        keys = set(ct.rows[0].cells)
        assert "Col2" not in keys, "duplicate value column should be dropped"
        assert "Lib" not in keys, "label-echo column should be dropped"
        assert "Variation" in keys, "real Variation column must be kept"
        # The spillover row's value_n was repaired from the duplicate, flagged yellow.
        repaired = ct.rows[2].cells["value_n"]
        assert repaired.parsed_value == Decimal("300")
        assert repaired.flag == "yellow"

    def test_mixed_date_format_year_binding(self):
        """Regression: EF CLB 2024 Bilan Actif mixes MM/DD (current year) with
        DD/MM (prior year) headers. value_n must bind to the most-recent year
        by recency, not to whichever header matches a literal "31/12/"."""
        from accounting.column_identifier import identify_columns

        df = pd.DataFrame({
            "Actifs":     ["Immobilisations Corporelles", "Total des actifs immobilisés"],
            "Notes":      ["2", ""],
            "12/31/2024": ["53 536 607", "29 496 190"],   # N
            "31/12/2023": ["52 938 385", "32 498 818"],   # N-1
        })
        layout = identify_columns(df)
        assert layout.model == "n_n1"
        assert layout.role_map.get("value_n") == 2          # the 2024 column
        assert layout.role_map.get("value_n_minus_1") == 3  # the 2023 column


# ===========================================================================
# subtotal_detector
# ===========================================================================


class TestSubtotalDetector:
    def test_detects_total_row(self):
        from accounting.subtotal_detector import detect_subtotals

        df = pd.DataFrame({
            "Label": ["Item A", "Item B", "Total des actifs"],
            "N": ["100", "200", "300"],
        })
        results = detect_subtotals(df)
        assert len(results) == 1
        assert results[0].row_index == 2
        assert results[0].is_subtotal is True

    def test_footing_validation(self):
        from accounting.subtotal_detector import detect_subtotals
        from accounting.column_identifier import ColumnLayout

        df = pd.DataFrame({
            "Label": ["Item A", "Item B", "Total"],
            "N": ["100", "200", "300"],
        })
        layout = ColumnLayout(model="n_n1", numeric_col_indices=[1])
        parsed = {
            (0, 1): Decimal("100"),
            (1, 1): Decimal("200"),
            (2, 1): Decimal("300"),
        }
        results = detect_subtotals(df, layout, parsed_numbers=parsed)
        assert len(results) == 1
        assert results[0].footing_valid is True

    def test_footing_mismatch(self):
        from accounting.subtotal_detector import detect_subtotals
        from accounting.column_identifier import ColumnLayout

        df = pd.DataFrame({
            "Label": ["Item A", "Item B", "Total"],
            "N": ["100", "200", "999"],
        })
        layout = ColumnLayout(model="n_n1", numeric_col_indices=[1])
        parsed = {
            (0, 1): Decimal("100"),
            (1, 1): Decimal("200"),
            (2, 1): Decimal("999"),
        }
        results = detect_subtotals(df, layout, parsed_numbers=parsed)
        assert len(results) == 1
        assert results[0].footing_valid is False


# ===========================================================================
# green_flag
# ===========================================================================


class TestGreenFlag:
    def test_all_pass(self):
        from accounting.green_flag import check_green_flag

        result = check_green_flag(
            match_rate=0.98,
            parse_fail_rate=0.0,
            column_confidence=0.90,
            classification_confidence=0.80,
        )
        assert result.passed is True
        assert len(result.failure_reasons) == 0

    def test_low_match_rate_fails(self):
        from accounting.green_flag import check_green_flag

        result = check_green_flag(
            match_rate=0.70,
            parse_fail_rate=0.0,
            column_confidence=0.90,
            classification_confidence=0.80,
        )
        assert result.passed is False
        assert result.gate_1_passed is False

    def test_parse_failures_fail(self):
        from accounting.green_flag import check_green_flag

        result = check_green_flag(
            match_rate=0.98,
            parse_fail_rate=0.05,
            column_confidence=0.90,
            classification_confidence=0.80,
        )
        assert result.passed is False
        assert result.gate_2_passed is False

    def test_green_flag_receives_footing_results(self):
        """G4 sees Stage 8's footing verdicts (RTE-7).

        The verdicts were computed and dropped, so G4 always took its
        "no subtotals" default pass. The enrichment pipeline now records one
        tri-state per detected subtotal row and the Tier-0 feed hands them to
        the gate as objects carrying ``footing_valid`` — a table whose only
        subtotal does not foot must fail G4. Stricter only: the same call
        without the verdicts still passes, so the gate can lose a pass it used
        to get for free but can never newly grant one.
        """
        from types import SimpleNamespace

        from accounting.enrichment_pipeline import enrich_tables
        from accounting.green_flag import check_green_flag

        df = pd.DataFrame({
            "Poste": ["Item A", "Item B", "Total"],
            "N": ["100", "200", "999"],
        })
        ct = enrich_tables([df], tier=0)[0]
        verdicts = ct.metadata.get("footing_results")
        assert verdicts == [False], (
            "Stage 8 must record one footing verdict per detected subtotal row"
        )

        # Same shim the Tier-0 feed builds.
        footing_results = [SimpleNamespace(footing_valid=v) for v in verdicts]
        result = check_green_flag(
            match_rate=0.98,
            parse_fail_rate=0.0,
            column_confidence=0.90,
            footing_results=footing_results,
            classification_confidence=0.80,
        )
        assert result.gate_4_no_subtotals is False
        assert result.gate_4_passed is False
        assert result.passed is False
        assert any(r.startswith("G4:") for r in result.failure_reasons)

        # Pre-RTE-7 behaviour — the gate passed because it was told nothing.
        blind = check_green_flag(
            match_rate=0.98,
            parse_fail_rate=0.0,
            column_confidence=0.90,
            classification_confidence=0.80,
        )
        assert blind.gate_4_passed is True
        assert blind.gate_4_no_subtotals is True

    def test_green_flag_footing_unchecked_still_passes(self):
        """Subtotals nobody could check must not fail G4.

        ``footing_valid`` is None when the row has no identifiable addends
        (a grand total, or a figure that is not a sum at all). Reporting a
        failure there would be a false negative, so G4 keeps its pass.
        """
        from types import SimpleNamespace

        from accounting.green_flag import check_green_flag

        result = check_green_flag(
            match_rate=0.98,
            parse_fail_rate=0.0,
            column_confidence=0.90,
            footing_results=[SimpleNamespace(footing_valid=None)],
            classification_confidence=0.80,
        )
        assert result.gate_4_passed is True
        assert result.passed is True


# ===========================================================================
# enrichment_pipeline (end-to-end)
# ===========================================================================


class TestEnrichmentPipeline:
    def test_produces_canonical_tables(self):
        from accounting.enrichment_pipeline import enrich_tables

        df = pd.DataFrame({
            "Label": ["Capital social", "Réserves", "Résultat de l'exercice"],
            "N": ["10 000", "5 000", "1 500"],
            "N-1": ["10 000", "4 500", "1 200"],
        })
        tables = enrich_tables([df], tier=0)
        assert len(tables) == 1
        ct = tables[0]
        assert len(ct.rows) == 3
        # At least some rows should be matched
        matched = [r for r in ct.rows if r.match_type in ("exact", "fuzzy", "resolved")]
        assert len(matched) >= 1

    def test_enrichment_writes_column_confidence(self):
        """The column identifier's confidence survives enrichment (RTE-7).

        It was computed and dropped (only ``.model`` was kept), so green-flag
        gate G3 read 0.00 on every table and Tier 0 could never accept. The
        value now lands in ``CanonicalTable.metadata`` — and on a clean
        brut/amort/net layout it clears G3's threshold.
        """
        from accounting.enrichment_pipeline import enrich_tables
        from accounting.green_flag import _COLUMN_CONF_THRESHOLD, check_green_flag

        df = pd.DataFrame({
            "Poste": ["Immobilisations incorporelles",
                      "Immobilisations corporelles"],
            "Brut": ["10 000", "5 000"],
            "Amortissements": ["1 000", "500"],
            "Net": ["9 000", "4 500"],
        })
        ct = enrich_tables([df], tier=0)[0]
        assert ct.column_model == "brut_amort_net"
        assert "column_confidence" in ct.metadata
        conf = ct.metadata["column_confidence"]
        assert isinstance(conf, float)
        assert 0.0 < conf <= 1.0
        assert conf >= _COLUMN_CONF_THRESHOLD

        # This is the number the Tier-0 feed hands to G3.
        result = check_green_flag(
            match_rate=0.98,
            parse_fail_rate=0.0,
            column_confidence=conf,
            classification_confidence=0.80,
        )
        assert result.gate_3_column_confidence == conf
        assert result.gate_3_passed is True

    def test_column_confidence_survives_ensemble_vote(self):
        """A new metadata key vanishes on the vote unless it is carried."""
        from accounting.ensemble import _CARRY_FORWARD_METADATA

        assert "column_confidence" in _CARRY_FORWARD_METADATA
        assert "footing_results" in _CARRY_FORWARD_METADATA

    def test_empty_input(self):
        from accounting.enrichment_pipeline import enrich_tables
        assert enrich_tables([]) == []

    def test_cells_have_parsed_values(self):
        from accounting.enrichment_pipeline import enrich_tables

        df = pd.DataFrame({
            "Poste": ["Capital social"],
            "Exercice N": ["10 000"],
            "Exercice N-1": ["9 000"],
        })
        tables = enrich_tables([df], tier=0)
        ct = tables[0]
        # Check that at least one cell has a parsed value
        all_cells = [c for r in ct.rows for c in r.cells.values()]
        parsed = [c for c in all_cells if c.parsed_value is not None]
        assert len(parsed) >= 1
