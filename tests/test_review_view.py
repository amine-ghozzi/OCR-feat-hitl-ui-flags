"""Unit tests for the pure HITL review-view logic (src/hitl/review_view.py)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from hitl.review_view import (
    INDIGO, RED, YELLOW, MAP_COL, FOCUS_COL,
    decorate_for_grid, classify_checks, sort_by_severity, humanize, is_advisory,
)
from accounting.validator import ValidationResult


def _ct():
    return {"rows": [
        {"raw_text": "Immobilisations incorporelles", "match_type": "exact",
         "match_confidence": 1.0, "is_header": False,
         "cells": {"2024": {"flag": "green", "basis": "plain"},
                   "2023": {"flag": "green", "basis": "single_sep_thousands"}}},
        {"raw_text": "Moins amortissement", "match_type": "fuzzy",
         "match_confidence": 0.80, "is_header": False,
         "cells": {"2024": {"flag": "yellow", "basis": "trailing_minus"},
                   "2023": {"flag": "green", "basis": "plain"}}},
        {"raw_text": "Divers X", "match_type": "custom", "match_confidence": 0.0,
         "is_header": False,
         "cells": {"2024": {"flag": "red", "basis": "plain"},
                   "2023": {"flag": "green", "basis": "plain"}}},
    ]}


def _flat_reordered():  # rows in a DIFFERENT order than the canonical table
    return pd.DataFrame([
        {"label": "Divers X", "2024": "1300", "2023": "1250"},
        {"label": "Immobilisations incorporelles", "2024": "210580", "2023": "210580"},
        {"label": "Moins amortissement", "2024": "-166074", "2023": "-118594"},
    ])


def test_per_cell_colour_label_aligned_despite_reorder():
    grid, value_cols, summary = decorate_for_grid(_flat_reordered(), _ct())
    assert value_cols == ["2024", "2023"]
    # red on Divers X 2024 (row 0), indigo on Immob 2023 (row 1), yellow on Moins 2024 (row 2)
    assert grid["2024__style"][0] == RED
    assert grid["2023__style"][1] == INDIGO
    assert grid["2024__style"][2] == YELLOW
    # green cells get no colour
    assert grid["2023__style"][0] == "" and grid["2024__style"][1] == ""


def test_mapping_badges_and_summary():
    grid, _, summary = decorate_for_grid(_flat_reordered(), _ct())
    assert grid[MAP_COL][0] == "⚠ custom"
    assert grid[MAP_COL][1] == "✓"
    assert grid[MAP_COL][2] == "≈0.80"
    assert summary == {"yellow": 1, "red": 1, "low_conf": 2, "assumption": 1,
                       "edited": 0, "resolved": 0, "confirmed": 0, "n_rows": 3}


def test_focus_marks_only_the_named_row():
    grid, _, _ = decorate_for_grid(_flat_reordered(), _ct(), focus_label="Moins amortissement")
    assert list(grid[FOCUS_COL]) == [False, False, True]


def test_focus_picks_one_row_among_duplicate_labels():
    # Two rows share the label "Amortissements" (one per asset class). Locate must
    # highlight exactly ONE — the one whose value matches the check's numeric hint.
    ct = {"rows": [
        {"raw_text": "Amortissements", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2024": {"flag": "green", "basis": "plain"}}},
        {"raw_text": "Amortissements", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2024": {"flag": "green", "basis": "plain"}}},
    ]}
    flat = pd.DataFrame([{"label": "Amortissements", "2024": "-37 430"},
                         {"label": "Amortissements", "2024": "-13 473 644"}])
    # hint matches the second row's value → only row 1 is focused
    grid, _, _ = decorate_for_grid(flat, ct, focus_label="Amortissements",
                                   focus_hint=-13473644.0)
    assert list(grid[FOCUS_COL]) == [False, True]
    # no hint → falls back to the first match (still exactly one row)
    grid2, _, _ = decorate_for_grid(flat, ct, focus_label="Amortissements")
    assert list(grid2[FOCUS_COL]) == [True, False]


def test_focus_targets_highlight_all_relevant_rows():
    # A statement/aggregate check points at several rows (Balance totals, Field-Type
    # offenders, Row-Sum components) → Locate highlights ALL of them.
    ct = {"rows": [
        {"raw_text": "Total Actif", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2024": {"flag": "green", "basis": "plain"}}},
        {"raw_text": "Stocks", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2024": {"flag": "green", "basis": "plain"}}},
        {"raw_text": "Total Passif", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2024": {"flag": "green", "basis": "plain"}}},
    ]}
    flat = pd.DataFrame([{"label": "Total Actif", "2024": "100"},
                         {"label": "Stocks", "2024": "40"},
                         {"label": "Total Passif", "2024": "100"}])
    targets = [{"label": "Total Actif", "hint": None},
               {"label": "Total Passif", "hint": None}]
    grid, _, _ = decorate_for_grid(flat, ct, focus_targets=targets)
    assert list(grid[FOCUS_COL]) == [True, False, True]


def test_locate_columns_outline_specific_cells_not_whole_row():
    # A check that names columns (VNC's Brut/Amort/Net) outlines those CELLS, leaving
    # the whole-row outline (__focus) off.
    from hitl.review_view import FOC_SUFFIX, SCROLL_COL
    ct = {"rows": [
        {"raw_text": "Immob", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"Brut": {"flag": "green", "basis": "plain"},
                                        "Amort": {"flag": "green", "basis": "plain"},
                                        "Net": {"flag": "green", "basis": "plain"}}},
    ]}
    flat = pd.DataFrame([{"label": "Immob", "Brut": "100", "Amort": "30", "Net": "70"}])
    targets = [{"label": "Immob", "hint": 70.0, "columns": ["Brut", "Amort", "Net"]}]
    grid, _, _ = decorate_for_grid(flat, ct, focus_targets=targets)
    assert grid["Brut" + FOC_SUFFIX][0] and grid["Amort" + FOC_SUFFIX][0] and grid["Net" + FOC_SUFFIX][0]
    assert list(grid[FOCUS_COL]) == [False]    # cell-level, not a whole-row outline
    assert list(grid[SCROLL_COL]) == [True]    # still scrolls into view


def test_locate_vertical_sum_outlines_one_column_down_rows():
    # A Row-Sum points at ONE value column across the subtotal + components.
    from hitl.review_view import FOC_SUFFIX
    ct = {"rows": [
        {"raw_text": r, "match_type": "exact", "match_confidence": 1.0, "is_header": False,
         "cells": {"2024": {"flag": "green", "basis": "plain"}}}
        for r in ("A", "B", "Total")
    ]}
    flat = pd.DataFrame([{"label": "A", "2024": "40", "2023": "1"},
                         {"label": "B", "2024": "60", "2023": "2"},
                         {"label": "Total", "2024": "100", "2023": "3"}])
    targets = [{"label": "Total", "hint": 100.0, "columns": ["2024"]},
               {"label": "A", "hint": 40.0, "columns": ["2024"]},
               {"label": "B", "hint": 60.0, "columns": ["2024"]}]
    grid, _, _ = decorate_for_grid(flat, ct, focus_targets=targets)
    assert list(grid["2024" + FOC_SUFFIX]) == [True, True, True]    # the summed column
    assert list(grid["2023" + FOC_SUFFIX]) == [False, False, False]  # other column untouched


def test_locate_match_text_disambiguates_duplicate_labels():
    # Two rows share the label "Moins provisions pour"; the garbled cells are in the
    # SECOND. ``match`` (exact bad-cell text) must pin the highlight to that row, not
    # the first label match.
    from hitl.review_view import FOC_SUFFIX
    ct = {"rows": [
        {"raw_text": "Moins provisions pour", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2023": {"flag": "green", "basis": "plain"}}},
        {"raw_text": "Moins provisions pour", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2023": {"flag": "red", "basis": "plain"}}},
    ]}
    flat = pd.DataFrame([{"label": "Moins provisions pour", "2023": "-1603448"},
                         {"label": "Moins provisions pour", "2023": "060201-"}])
    targets = [{"label": "Moins provisions pour", "hint": None,
                "columns": ["2023"], "match": {"2023": "060201-"}}]
    grid, _, _ = decorate_for_grid(flat, ct, focus_targets=targets)
    assert list(grid["2023" + FOC_SUFFIX]) == [False, True]   # second row, not first
    assert list(grid[FOCUS_COL]) == [False, False]            # cell-level, no row outline


def test_locate_match_confirms_row_then_outlines_all_flagged_cells():
    # The check flagged TWO cells of the lower row: "060201-" (still present) and the
    # "%" cell (since emptied by auto-correct). A match on "060201-" confirms the row,
    # then BOTH flagged columns are outlined in it — including the now-empty "%". The
    # upper same-label row (nothing confirmed) is never touched.
    from hitl.review_view import FOC_SUFFIX
    ct = {"rows": [
        {"raw_text": "Moins provisions pour", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2022": {"flag": "green", "basis": "plain"},
                                        "%": {"flag": "green", "basis": "plain"}}},
        {"raw_text": "Moins provisions pour", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2022": {"flag": "red", "basis": "plain"}}},
    ]}
    flat = pd.DataFrame([{"label": "Moins provisions pour", "2022": "-1603448", "%": "4,1%"},
                         {"label": "Moins provisions pour", "2022": "060201-", "%": ""}])
    targets = [{"label": "Moins provisions pour", "hint": None, "columns": ["2022", "%"],
                "match": {"2022": "060201-", "%": "%5'9-"}}]
    grid, _, _ = decorate_for_grid(flat, ct, focus_targets=targets)
    assert list(grid["2022" + FOC_SUFFIX]) == [False, True]   # confirmed row, both
    assert list(grid["%" + FOC_SUFFIX]) == [False, True]      # flagged % outlined though empty
    assert list(grid[FOCUS_COL]) == [False, False]            # never a wrong-row outline


def test_locate_match_unconfirmed_highlights_nothing():
    # Every reported garbled text was auto-corrected away → no row can be confirmed →
    # Locate outlines nothing rather than guessing a wrong cell.
    from hitl.review_view import FOC_SUFFIX
    ct = {"rows": [
        {"raw_text": "Moins provisions pour", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2022": {"flag": "green", "basis": "plain"}}},
        {"raw_text": "Moins provisions pour", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2022": {"flag": "green", "basis": "plain"}}},
    ]}
    flat = pd.DataFrame([{"label": "Moins provisions pour", "2022": "-1603448"},
                         {"label": "Moins provisions pour", "2022": "-2736"}])
    targets = [{"label": "Moins provisions pour", "hint": None, "columns": ["2022"],
                "match": {"2022": "060201-"}}]   # text not present anywhere
    grid, _, _ = decorate_for_grid(flat, ct, focus_targets=targets)
    assert list(grid["2022" + FOC_SUFFIX]) == [False, False]
    assert list(grid[FOCUS_COL]) == [False, False]


def test_locate_row_index_survives_text_change():
    # The bad "%" cell's text was auto-corrected ("%5'9-" -> "-6,9%"), so match can't
    # confirm it; the positional ``row`` pins it anyway (report recomputed on the same
    # tables). Four same-label rows; target is the 4th. The label guard keeps it safe.
    from hitl.review_view import FOC_SUFFIX
    flat = pd.DataFrame([
        {"label": "Moins provisions", "2022": "-1", "%": "1%"},
        {"label": "Moins provisions", "2022": "060201-", "%": ""},
        {"label": "Moins provisions", "2022": "-3", "%": ""},
        {"label": "Moins provisions", "2022": "-290", "%": "-6,9%"},
    ])
    targets = [{"label": "Moins provisions", "row": 3, "hint": None,
                "columns": ["%"], "match": {"%": "%5'9-"}}]
    grid, _, _ = decorate_for_grid(flat, None, focus_targets=targets)
    assert list(grid["%" + FOC_SUFFIX]) == [False, False, False, True]
    assert list(grid["2022" + FOC_SUFFIX]) == [False, False, False, False]


def test_locate_row_index_label_guard_falls_back():
    # The positional row points at a row whose label doesn't match (target applied to
    # the wrong table) → ignore the index; with no text match either, highlight nothing.
    from hitl.review_view import FOC_SUFFIX
    flat = pd.DataFrame([{"label": "Other A", "2022": "x"},
                         {"label": "Other B", "2022": "y"}])
    targets = [{"label": "Moins provisions", "row": 0, "hint": None,
                "columns": ["2022"], "match": {"2022": "zzz"}}]
    grid, _, _ = decorate_for_grid(flat, None, focus_targets=targets)
    assert list(grid["2022" + FOC_SUFFIX]) == [False, False]


def test_row_level_fallback_when_columns_do_not_align():
    # flat columns ColA/ColB don't match canonical cell keys 2024/2023 → whole
    # aligned row is coloured by its worst flag (Divers X has a red cell).
    flat = pd.DataFrame([{"label": "Divers X", "ColA": "1300", "ColB": "1250"}])
    grid, value_cols, _ = decorate_for_grid(flat, _ct())
    assert value_cols == ["ColA", "ColB"]
    assert grid["ColA__style"][0] == RED and grid["ColB__style"][0] == RED


def test_no_canonical_table_is_safe_noop():
    flat = _flat_reordered()
    grid, value_cols, summary = decorate_for_grid(flat, None)
    assert all(grid[c + "__style"].eq("").all() for c in value_cols)
    assert summary["n_rows"] == 3


def _chk(name, sev, passed=False, message="m", diff=None):
    return ValidationResult(check_name=name, passed=passed, message=message,
                            severity=sev, difference=diff)


def test_classify_checks_splits_meta_prominent_advisory():
    # D14 re-tier: accounting checks (incl. Var% and Field Type, trustworthy
    # post-D1/D2b) are PROMINENT; heuristics/ratios advisory; structural meta apart.
    checks = [
        _chk("Tables Found", "CRITICAL", passed=True),
        _chk("Balance Sheet Equation", "CRITICAL"),
        _chk("Row Sum: Total X...", "ERROR"),
        _chk("VNC: Immob", "ERROR"),
        _chk("V3: Magnitude span", "WARNING"),
        _chk("Field Type", "ERROR"),
        _chk("[bilan] Var%: Amortissements", "WARNING"),
    ]
    meta, prominent, advisory = classify_checks(checks)
    mnames = {c.check_name for c in meta}
    pnames = {c.check_name for c in prominent}
    anames = {c.check_name for c in advisory}
    assert mnames == {"Tables Found"}
    assert {"Balance Sheet Equation", "VNC: Immob", "Field Type",
            "[bilan] Var%: Amortissements"} == pnames
    assert {"Row Sum: Total X...", "V3: Magnitude span"} == anames


def test_critical_is_never_advisory():
    assert is_advisory(_chk("Row Sum: X", "ERROR")) is True
    assert is_advisory(_chk("Row Sum: X", "CRITICAL")) is False


def test_sort_by_severity_failed_and_critical_first():
    order = sort_by_severity([
        _chk("w-pass", "WARNING", passed=True),
        _chk("crit-fail", "CRITICAL", passed=False),
        _chk("err-fail", "ERROR", passed=False),
    ])
    assert [c.check_name for c in order][:2] == ["crit-fail", "err-fail"]


def test_sort_keeps_rule_family_adjacent():
    # The Var% aggregate (PASS, INFO) and its coverage entry (SKIPPED, WARNING)
    # belong to the same rule (V1-005) and must render next to each other —
    # never separated by another family's checks.
    from accounting.validator import ValidationResult
    cov = ValidationResult("Var% coverage", passed=True, message="", severity="WARNING",
                           skipped=True)
    fam = ValidationResult("Variation % Check", passed=True, message="", severity="INFO")
    vnc = ValidationResult("VNC Consistency", passed=True, message="", severity="INFO")
    order = sort_by_severity([cov, vnc, fam])
    names = [c.check_name for c in order]
    i, j = names.index("Variation % Check"), names.index("Var% coverage")
    assert abs(i - j) == 1, names               # adjacent
    assert i < j                                 # aggregate first, coverage after


def test_humanize_rewrites_known_and_falls_back():
    assert "Assets should equal" in humanize(_chk("Balance Sheet Equation", "CRITICAL", diff=8844012))
    assert "Net should equal Brut" in humanize(_chk("VNC: Immob", "ERROR", message="Net=1, ..."))
    assert humanize(_chk("Some Other Check", "WARNING", message="raw msg")) == "raw msg"


def test_describe_check_plain_titles_and_rule_ids():
    from hitl.review_view import describe_check, is_advisory
    # D14: Var% is a trustworthy accounting check post-D1/D2b — NOT advisory —
    # and D11 ties it to its formal rule (id + formula).
    v = describe_check(_chk("Var%: Amortissements", "WARNING"))
    assert "Year-over-year" in v.title and v.checks and v.fix
    assert v.rule and v.rule["rule_id"] == "V1-005" and v.rule.get("formula")
    assert "mark Pass" not in v.fix  # the old pass-away coaching is gone
    assert is_advisory(_chk("[bilan] Var%: X", "WARNING")) is False
    # jargon names get human titles
    assert "Balance sheet" in describe_check(_chk("Balance Sheet Equation", "CRITICAL")).title
    assert "valid numbers" in describe_check(_chk("Field Type Check", "ERROR")).title.lower()
    assert "Working-capital" in describe_check(_chk("V4: Fonds de Roulement (FR)", "WARNING")).title
    assert describe_check(_chk("Field Type Check", "ERROR")).rule["rule_id"] == "V1-006"
    # unknown check falls back to its raw name as title + a real explanation
    fb = describe_check(_chk("Some Novel Check", "WARNING"))
    assert fb.title == "Some Novel Check" and fb.checks and fb.fix and fb.rule is None


def test_focus_and_alignment_with_unnamed_label_column():
    # Regression: an OCR label column named "" is falsy — focus + alignment must
    # still work (the .jpg bug). Use the canonical-cell keys as the value columns.
    ct = {"rows": [
        {"raw_text": "Amortissements.", "match_type": "exact", "match_confidence": 1.0,
         "is_header": False, "cells": {"2023": {"flag": "yellow", "basis": "plain"}}},
        {"raw_text": "VNC", "match_type": "custom", "match_confidence": 0.0,
         "is_header": False, "cells": {"2023": {"flag": "green", "basis": "plain"}}},
    ]}
    flat = pd.DataFrame([{"": "Amortissements.", "2023": "-166074"},
                         {"": "VNC", "2023": "0"}])
    grid, value_cols, _ = decorate_for_grid(flat, ct, focus_label="Amortissements")
    # alignment worked (yellow flag landed) AND the row is focus-flagged — but focus is
    # drawn as a row outline (getRowStyle), so the cell KEEPS its own flag colour.
    assert grid["2023__style"][0] == YELLOW
    assert list(grid[FOCUS_COL]) == [True, False]
