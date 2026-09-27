"""Var% invariant guard (ISSUE-6) — a 'Variation' column holding ABSOLUTE deltas
must not raise false WARNINGs, while a true-percentage column still validates.

Background: validate_variation_percentages selects a column by header ('%' or
'variation'/'écart') and compares it against ((current-prior)/abs(prior))*100.
When that column actually carries the absolute delta amount (current-prior) — or
a garbled OCR percent token — the legacy code emitted huge false WARNINGs
("Reported -1640358% vs Expected -64.1%"). These tests pin the guarded behavior.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from accounting.validator import (  # noqa: E402
    validate_variation_percentages,
    auto_correct_variation_percentages,
)


def _warnings(results):
    """Discrepancy WARNINGs only (exclude the all-verified success summary)."""
    return [r for r in results if not r.passed]


def test_absolute_delta_column_does_not_raise_false_warning():
    # 'Variation' column holds the ABSOLUTE delta (current - prior), not a percent.
    df = pd.DataFrame({
        "Label": ["Immobilisations", "Stocks", "Créances"],
        "2023": [120000, 55000, 165000],
        "2022": [100000, 50000, 150000],
        "Variation": [20000, 5000, 15000],  # absolute deltas, NOT %
    })
    results = validate_variation_percentages(df)
    assert _warnings(results) == [], (
        "Absolute-delta column must not produce Var% WARNINGs: "
        f"{[r.message for r in _warnings(results)]}"
    )


def test_huge_implausible_value_is_skipped():
    # An implausibly large non-percentage value (e.g. OCR concatenation) must be
    # treated as a non-% and skipped, not reported as a -1640358% discrepancy.
    df = pd.DataFrame({
        "Label": ["Trésorerie"],
        "2023": [1234567],
        "2022": [2345678],
        "Variation %": [-1640358.0],  # nonsense magnitude, far above expected ~-47%
    })
    results = validate_variation_percentages(df)
    assert _warnings(results) == []


def test_garbled_percent_token_is_skipped():
    # Garbled OCR percent cells ("%807", "%5'9-") coerce to numbers but fail a
    # strict percent-token parse and must be skipped.
    df = pd.DataFrame({
        "Label": ["Ventes", "Achats"],
        "2023": [110000, 90000],
        "2022": [100000, 100000],
        "Variation %": ["%807", "%5'9-"],
    })
    results = validate_variation_percentages(df)
    assert _warnings(results) == []


def test_true_percentage_column_still_validates():
    # Legitimate percentages must still be checked and pass (no false negative).
    df = pd.DataFrame({
        "Label": ["Immobilisations", "Stocks"],
        "2023": [120000, 55000],
        "2022": [100000, 50000],
        "Variation %": [20.0, 10.0],  # true, correct percentages
    })
    results = validate_variation_percentages(df)
    assert _warnings(results) == []
    # The all-verified success summary should be present.
    assert any(r.passed and "verified" in r.message for r in results)


def test_true_percentage_mismatch_is_still_reported():
    # A genuinely wrong percentage (within plausible range) must still WARN —
    # the guard must not over-suppress real Var% errors.
    df = pd.DataFrame({
        "Label": ["Immobilisations"],
        "2023": [120000],
        "2022": [100000],
        "Variation %": [99.0],  # plausible % but wrong (expected 20%)
    })
    results = validate_variation_percentages(df)
    # the per-row failure carries the score; the family aggregate (INFO)
    # mirrors the failure with counts/stats
    row_warns = [r for r in _warnings(results) if r.severity != "INFO"]
    assert len(row_warns) == 1
    assert "99.0%" in row_warns[0].message
    family = [r for r in results if r.check_name == "Variation % Check"]
    assert family and family[0].stats["failed"] == 1
    assert family[0].stats["verified"] == 1 and not family[0].passed


def test_auto_correct_does_not_overwrite_absolute_delta_column():
    # auto_correct must not rewrite a legitimate absolute-delta cell into a %.
    df = pd.DataFrame({
        "Label": ["Immobilisations", "Stocks"],
        "2023": [120000, 55000],
        "2022": [100000, 50000],
        "Variation": [20000, 5000],  # absolute deltas
    })
    corrected, corrections = auto_correct_variation_percentages(df)
    # No correction should be recorded for delta cells, and the column must be
    # left byte-for-byte unchanged.
    assert corrections == []
    pd.testing.assert_frame_equal(corrected, df)
