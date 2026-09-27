"""
Regression tests for two related structure bugs.

ISSUE-7a — Note/Réf column (full of account codes like '(5111)') was admitted
           as numeric and picked as the value column → V4 "could not locate"
           skips, and V3 flagged the paren-wrapped codes as negative values.

ISSUE-7b — An empty section-header row (e.g. 'CLIENTS' in CAPS with every value
           cell blank) was fuzzy-matched to a canonical leaf and emitted,
           duplicating the real data row of the same label.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402
import numpy as np  # noqa: E402


# ===========================================================================
# ISSUE-7a — Note column not selected as value column
# ===========================================================================


def _make_bilan_with_note_codes() -> pd.DataFrame:
    """A bilan whose 'Note' column holds short paren-wrapped account codes.

    The codes ('(2211)', '(1011)', '(311)') parse as numbers via the
    parens→negative branch, so ``find_numeric_columns`` admits the Note column.
    The only real value column is 'Exercice N'.
    """
    return pd.DataFrame(
        {
            "Designation": [
                "ACTIFS NON COURANTS",
                "Immobilisations corporelles",
                "Capital social",
                "Stocks",
                "TOTAL ACTIF",
                "TOTAL CAPITAUX PROPRES ET PASSIFS",
            ],
            "Note": ["", "(2211)", "(1011)", "(311)", "", ""],
            "Exercice N": ["", "600000", "500000", "150000", "1250000", "1250000"],
        }
    )


def test_7a_note_column_is_admitted_as_numeric_but_must_not_win():
    """Guard: the Note column IS mis-admitted by find_numeric_columns — this is
    the bug surface — yet the value-column selector must skip it.
    """
    from accounting.validator import find_numeric_columns, extract_numeric_value

    df = _make_bilan_with_note_codes()

    # The bug surface: paren-wrapped codes parse as (negative) numbers, so the
    # Note column passes the >50% numeric gate.
    assert "Note" in find_numeric_columns(df)
    assert extract_numeric_value("(2211)") == -2211.0


def test_7a_value_column_resolves_to_real_period_not_note():
    """The value-column selector must pick 'Exercice N', not 'Note'."""
    from accounting.rule_engine import _get_label_col_and_value_col

    df = _make_bilan_with_note_codes()
    label_col, value_col = _get_label_col_and_value_col(df)

    assert label_col == "Designation"
    assert value_col == "Exercice N"
    assert value_col != "Note"


def test_7a_v3_does_not_flag_note_codes_as_negatives():
    """V3 negativity must not flag the '(NNNN)' account codes as negative values."""
    from accounting.statement_segmenter import segment_tables
    from accounting.rule_engine import detect_anomalies

    df = _make_bilan_with_note_codes()
    segments = segment_tables([df])
    assert segments[0].statement_type == "bilan"

    results = detect_anomalies(segments)
    # No FAILING negative flag (the rule may now also emit a passing "ran clean"
    # aggregate — that is the intended coverage-honesty behavior, not a flag).
    negative_flags = [r for r in results if "Negative" in r.check_name and not r.passed]

    assert negative_flags == [], (
        "Note/Réf account codes were flagged as negative values: "
        f"{[(r.check_name, r.actual) for r in negative_flags]}"
    )


def test_7a_legitimate_small_value_column_is_not_dropped():
    """A column merely *named* ref-ish but holding genuine amounts must survive
    (exclusion is gated on header-name match AND code-pattern density)."""
    from accounting.rule_engine import _get_label_col_and_value_col

    # 'Référence' header but the cells are real amounts, not 3-4 digit codes.
    df = pd.DataFrame(
        {
            "Designation": ["Immobilisations corporelles", "Stocks", "TOTAL ACTIF"],
            "Référence": ["600000", "150000", "750000"],
        }
    )
    label_col, value_col = _get_label_col_and_value_col(df)
    assert label_col == "Designation"
    assert value_col == "Référence"


# ===========================================================================
# ISSUE-7b — Empty CAPS header row not promoted to a duplicate canonical leaf
# ===========================================================================


def test_7b_header_flags_exposed_additively():
    """detect_section_headers_with_flags returns per-row header booleans while
    the original detect_section_headers API is unchanged."""
    from accounting.header_detector import (
        detect_section_headers,
        detect_section_headers_with_flags,
    )

    df = pd.DataFrame(
        {
            "Label": ["CLIENTS", "Clients", "Capital social"],
            "N": ["", "300000", "500000"],
        }
    )
    sections, flags = detect_section_headers_with_flags(df)

    assert len(flags) == len(df)
    assert flags[0] is True  # CAPS header row
    assert flags[1] is False  # real data row
    # Backward-compatible: original API still returns just the section list.
    assert detect_section_headers(df) == sections


def test_7b_empty_caps_header_does_not_duplicate_data_row():
    """An empty CAPS 'CLIENTS' header row whose label duplicates a following
    data row must NOT emit a second canonical leaf for the same term."""
    from accounting.enrichment_pipeline import enrich_tables

    df = pd.DataFrame(
        {
            "Label": ["CLIENTS", "Clients", "Capital social"],
            "N": ["", "300000", "500000"],
            "N-1": ["", "280000", "500000"],
        }
    )
    ct = enrich_tables([df], tier=0)[0]

    by_label = {r.raw_text: r for r in ct.rows}

    # The empty CAPS header is suppressed: marked is_header, no canonical leaf.
    header_row = by_label["CLIENTS"]
    assert header_row.is_header is True
    assert header_row.canonical_term is None

    # The real data row keeps its canonical match.
    data_row = by_label["Clients"]
    assert data_row.is_header is False
    assert data_row.canonical_term is not None

    # Exactly one canonical leaf carries the 'clients' term — no duplicate.
    clients_leaves = [
        r
        for r in ct.rows
        if r.canonical_term and "client" in r.canonical_term.lower()
    ]
    assert len(clients_leaves) == 1


def test_7b_genuine_nil_value_leaf_is_preserved():
    """A genuine nil-value leaf (all-empty cells but NOT a header signal) must
    keep its canonical match — both conditions are required to suppress."""
    from accounting.enrichment_pipeline import enrich_tables

    # Lowercase label → no CAPS header signal, even though value cells are empty.
    df = pd.DataFrame({"Label": ["Capital social"], "N": [""], "N-1": [""]})
    ct = enrich_tables([df], tier=0)[0]
    row = ct.rows[0]

    assert row.is_header is False
    assert row.canonical_term is not None