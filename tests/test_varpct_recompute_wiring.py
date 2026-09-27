"""Wire-in guard for the Var% recompute in the production enrichment pipeline.

Background: ``auto_correct_variation_percentages`` (validator.py) already
recomputes a garbled '%' cell from the two period values, but it was only ever
called from the HITL app — NOT from the scored canonical path. These tests pin
that ``_enrich_single_table`` (the Stage 1–10 DAG used by pipeline.py for both
Tier 0 and Tier 1) now applies the recompute BEFORE the cells are parsed, so the
corrected '%' reaches ``CanonicalCell.parsed_value``.

Guards pinned here:
  1. A garbled '%' cell IS recomputed and the corrected value reaches the
     emitted '%' cell (parsed_value + raw_value), flagged yellow (reconstructed).
  2. The ISSUE-6 absolute-delta column is NOT overwritten.
  3. A clean, correct '%' cell is left unchanged (still green).
  4. A table with no '%' column is a no-op (no crash).
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from accounting.enrichment_pipeline import _enrich_single_table  # noqa: E402
from accounting.nomenclature import load_default_dictionary  # noqa: E402


_DICT = load_default_dictionary()


def _enrich(df: pd.DataFrame):
    return _enrich_single_table(df, _DICT, provenance="tier0", font_spans=None)


def _find_pct_cell(canonical_row):
    """Return the CanonicalCell that came from the '%' column, by raw/role name."""
    for name, cell in canonical_row.cells.items():
        if str(name).strip() in ("%", "var %", "var%") or "%" in str(name):
            return cell
    return None


def test_garbled_pct_is_recomputed_in_pipeline():
    # Two clean period columns; the '%' cell is garbled OCR ("%807").
    # Expected var = ((110000 - 100000) / 100000) * 100 = +10.0
    df = pd.DataFrame({
        "Poste": ["Chiffre d'affaires"],
        "2023": [110000],
        "2022": [100000],
        "%": ["%807"],
    })
    ct = _enrich(df)
    row = ct.rows[0]
    cell = _find_pct_cell(row)
    assert cell is not None, f"no % cell found in {list(row.cells)}"

    # The garbled value must be gone; the corrected +10,0% must be present.
    assert "807" not in cell.raw_value, f"garbled value survived: {cell.raw_value!r}"
    assert cell.parsed_value is not None, "recomputed % did not parse"
    assert abs(float(cell.parsed_value) - 10.0) < 0.5, (
        f"expected ~10.0, got {cell.parsed_value}"
    )
    # A reconstructed cell is routed to review, not emitted green.
    assert cell.flag == "yellow", f"reconstructed % cell should be yellow, got {cell.flag}"


def test_absolute_delta_column_not_overwritten():
    # ISSUE-6: a 'Variation' column holding ABSOLUTE deltas (current - prior)
    # must NOT be overwritten by a recomputed percentage.
    df = pd.DataFrame({
        "Poste": ["Immobilisations", "Stocks"],
        "2023": [120000, 55000],
        "2022": [100000, 50000],
        "Variation": [20000, 5000],  # absolute deltas, NOT %
    })
    ct = _enrich(df)
    for i, expected_delta in enumerate(["20000", "5000"]):
        cell = ct.rows[i].cells.get("Variation")
        # The cell may be keyed by role; fall back to scanning if needed.
        if cell is None:
            for name, c in ct.rows[i].cells.items():
                if "variation" in str(name).lower():
                    cell = c
                    break
        assert cell is not None
        assert expected_delta in cell.raw_value, (
            f"absolute-delta cell was overwritten: {cell.raw_value!r}"
        )
        assert cell.flag != "yellow" or expected_delta in cell.raw_value


def test_clean_correct_pct_left_unchanged():
    # A clean, correct '%' cell (+10,0%) matches the recompute within tolerance,
    # so it must be left untouched and stay green.
    df = pd.DataFrame({
        "Poste": ["Chiffre d'affaires"],
        "2023": [110000],
        "2022": [100000],
        "%": ["+10,0%"],
    })
    ct = _enrich(df)
    cell = _find_pct_cell(ct.rows[0])
    assert cell is not None
    assert cell.raw_value == "+10,0%", f"clean % was altered: {cell.raw_value!r}"
    assert cell.flag == "green", f"clean % should stay green, got {cell.flag}"


def test_no_pct_column_is_noop():
    # A bilan-style table with no '%' column must pass through without crashing
    # and without spuriously flagging a recompute.
    df = pd.DataFrame({
        "Poste": ["Immobilisations", "Stocks"],
        "Brut": [120000, 55000],
        "Amortissement": [20000, 5000],
        "Net": [100000, 50000],
    })
    ct = _enrich(df)  # must not raise
    assert len(ct.rows) == 2
    # No cell should have been recompute-flagged yellow for a missing % column.
    yellow_from_recompute = [
        c for row in ct.rows for c in row.cells.values()
        if c.flag == "yellow" and "%" in c.raw_value
    ]
    assert yellow_from_recompute == []