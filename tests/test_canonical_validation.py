"""Workstream A guard tests — one canonical parser for validation (D1/D2),
signed-prior Var% (D2b) + sign-convention guard, skipped≠pass (D7), severity
alignment (D8), and the implausible-expected ceiling (D9).

Root cause pinned here: validation used a naive dot=decimal parser while the
pipeline parsed "-1.603448" as -1603448 (dot=thousands), so V1 checks ran at
the wrong scale on exactly the assumption-flagged (indigo) cells, and
abs(prior) sign-flipped the expected Var% on negative-base rows. The three
Bilan 1.jpg Var% rows (Amortissements 3,9%, Moins provisions 4,1%, Provisions
426,4%) are all CORRECT in the document and must validate green.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from accounting.validator import (  # noqa: E402
    ValidationReport,
    ValidationResult,
    _canonical_numeric_value,
    extract_numeric_value,
    validate_field_types,
    validate_variation_percentages,
)
from accounting.rule_engine import (  # noqa: E402
    compute_ratios,
    validate_vnc_consistency,
)


# ---------------------------------------------------------------------------
# D1 — the canonical wrapper
# ---------------------------------------------------------------------------

def test_canonical_wrapper_matches_pipeline_scale():
    # THE bug: dot-as-thousands must parse at extraction scale, not /1000.
    assert _canonical_numeric_value("-1.603448") == -1603448.0
    assert _canonical_numeric_value("-13 473.644") == -13473644.0
    assert _canonical_numeric_value("(2211)") == -2211.0


def test_canonical_wrapper_returns_float():
    # parse_french_number returns Decimal; tolerance math needs float (R4).
    v = _canonical_numeric_value("-1.603448")
    assert isinstance(v, float)


def test_canonical_wrapper_rejects_percent_strings():
    # %-cells MUST stay on extract_numeric_value — canonical returns None.
    assert _canonical_numeric_value("4,1%") is None
    assert extract_numeric_value("4,1%") == 4.1


def test_canonical_wrapper_rejects_garbled():
    # Both parsers reject garbled cells (V1-006 rejection preserved — R1).
    assert _canonical_numeric_value("95,276,113 41,694,461") is None
    assert extract_numeric_value("95,276,113 41,694,461") is None
    assert _canonical_numeric_value("%5'9-") is None


def test_canonical_wrapper_empty_and_passthrough():
    assert _canonical_numeric_value("") is None
    assert _canonical_numeric_value(None) is None
    assert _canonical_numeric_value(float("nan")) is None
    assert _canonical_numeric_value(42) == 42.0
    assert _canonical_numeric_value(-1.5) == -1.5


def test_canonical_wrapper_wrong_direction_case_documented():
    # Known residual (E2's job, not D1's): a genuine millimes decimal like
    # 12083312.401 is read as thousands by the canonical heuristic. The
    # identity-based resolver (E2) arbitrates; this pin documents the input.
    assert _canonical_numeric_value("12083312.401") == 12083312401.0
    assert extract_numeric_value("12083312.401") == 12083312.401


# ---------------------------------------------------------------------------
# D2/D2b — the three Bilan 1.jpg Var% rows flip FAIL → PASS
# ---------------------------------------------------------------------------

def _bilan_var_df():
    """The three flagged rows exactly as extracted from Samples/Bilan 1.jpg."""
    return pd.DataFrame({
        "Label": ["Amortissements", "Moins provisions pour", "Provisions"],
        "2023": ["-13 473.644", "-1 668 470", "-216.982"],
        "2022": ["-12 969 942", "-1.603448", "-41 219"],
        "%": ["3,9%", "4,1%", "426,4%"],
    })


def test_bilan_negative_base_var_rows_pass():
    results = validate_variation_percentages(_bilan_var_df())
    failures = [r for r in results if not r.passed]
    assert failures == [], (
        "Document-correct Var% rows must not fail post-D1/D2b: "
        f"{[(r.check_name, r.message) for r in failures]}"
    )
    # All three rows actually verified (not silently skipped).
    summary = [r for r in results if r.check_name == "Variation % Check"]
    assert summary and "3" in summary[0].message


def test_var_sign_convention_guard():
    # Negative base + sign-flipped reported % with matching magnitude →
    # a passing sign-convention note, NOT a hard FAIL (D2b guard).
    df = pd.DataFrame({
        "Label": ["Provisions"],
        "2023": ["-150 000"],
        "2022": ["-100 000"],
        "%": ["-50,0%"],  # expected signed: +50.0%
    })
    results = validate_variation_percentages(df)
    failures = [r for r in results if not r.passed]
    assert failures == []
    sign_notes = [r for r in results if r.check_name.startswith("Var% sign:")]
    assert len(sign_notes) == 1
    assert sign_notes[0].passed and not sign_notes[0].skipped


def test_var_real_discrepancy_still_fails():
    # The fix must not blanket-pass: a genuinely wrong % still fails, and the
    # family aggregate reports it as a COUNT, not a bare binary.
    df = pd.DataFrame({
        "Label": ["Stocks"],
        "2023": ["120 000"],
        "2022": ["100 000"],
        "%": ["35,0%"],  # expected +20.0%
    })
    results = validate_variation_percentages(df)
    row_fails = [r for r in results if not r.passed and r.severity != "INFO"]
    assert len(row_fails) == 1
    assert abs(row_fails[0].expected - 20.0) < 0.01
    family = [r for r in results if r.check_name == "Variation % Check"]
    assert family and not family[0].passed
    assert family[0].stats == {"candidates": 1, "verified": 1, "passed": 0,
                               "failed": 1, "not_verifiable": 0, "pass_rate": 0.0}


# ---------------------------------------------------------------------------
# Row-level coverage honesty — an empty/garbled input never justifies a pass
# ---------------------------------------------------------------------------

def test_empty_percent_cell_is_not_a_silent_pass():
    # The reported case: amounts present but the % cell is EMPTY (and the
    # prior is garbled) — the identity cannot run, so the row must surface as
    # NOT VERIFIABLE instead of disappearing into "all verified ✓".
    df = pd.DataFrame({
        "Label": ["Moins provisions pour dépréciation"],
        "2023": ["-2736"],
        "2022": ["060201-"],   # garbled
        "%": [""],             # empty
    })
    results = validate_variation_percentages(df)
    assert [r for r in results if not r.passed] == []          # no false failure
    cov = [r for r in results if r.check_name == "Var% coverage"]
    assert len(cov) == 1 and cov[0].skipped                     # visible, skipped
    assert "NOT verifiable" in cov[0].message
    # crucially: with ZERO rows verified the family aggregate cannot claim a
    # pass — it is SKIPPED and its stats say 0 verified / 1 not verifiable.
    family = [r for r in results if r.check_name == "Variation % Check"]
    assert family and family[0].skipped
    assert family[0].stats["verified"] == 0
    assert family[0].stats["not_verifiable"] == 1


def test_summary_states_true_coverage():
    df = pd.DataFrame({
        "Label": ["Stocks", "Clients"],
        "2023": ["120 000", "200 000"],
        "2022": ["100 000", "150 000"],
        "%": ["20,0%", ""],     # second row: empty % cell
    })
    results = validate_variation_percentages(df)
    summary = [r for r in results if r.check_name == "Variation % Check"]
    assert summary and "1 verifiable" in summary[0].message
    assert "1 not verifiable" in summary[0].message
    cov = [r for r in results if r.check_name == "Var% coverage"]
    assert cov and cov[0].skipped and "empty % cell" in cov[0].message


def test_zero_percent_on_unchanged_line_is_verified_not_delta():
    # delta = 0 and reported = 0% coincide numerically — the identity has
    # priority: an unchanged line with 0,0% is VERIFIED, never skipped as
    # "delta amount, not a %".
    df = pd.DataFrame({
        "Label": ["Immobilisations financières"],
        "2023": ["19 032 672"],
        "2022": ["19 032 672"],
        "%": ["0,0%"],
    })
    results = validate_variation_percentages(df)
    assert [r for r in results if not r.passed] == []
    assert not any(r.check_name == "Var% coverage" for r in results)
    family = [r for r in results if r.check_name == "Variation % Check"]
    assert family and family[0].stats["verified"] == 1
    assert family[0].stats["not_verifiable"] == 0


def test_auto_correct_never_emits_negative_zero_token():
    # round(-0.04, 1) → -0.0 used to render the garbled token "+-0,0%".
    from accounting.validator import auto_correct_variation_percentages
    df = pd.DataFrame({
        "Label": ["Amortissements"],
        "2023": ["-37 445"],
        "2022": ["-37 430"],   # expected ≈ -0.04% → rounds to -0.0
        "%": ["garbled"],      # unparseable → triggers correction
    })
    corrected, corrections = auto_correct_variation_percentages(df)
    assert corrections, "correction should have fired"
    token = corrected["%"].iloc[0]
    assert token == "+0,0%", token  # normalised zero, no '+-' garble


def test_coverage_message_itemizes_each_row():
    df = pd.DataFrame({
        "Label": ["Stocks", "Clients"],
        "2023": ["120 000", "200 000"],
        "2022": ["100 000", "150 000"],
        "%": ["", "%5'9-"],   # empty cell · garbled token
    })
    results = validate_variation_percentages(df)
    cov = [r for r in results if r.check_name == "Var% coverage"]
    assert len(cov) == 1
    assert "• Stocks — empty % cell" in cov[0].message
    assert "• Clients — unparseable % cell `%5'9-`" in cov[0].message


def test_coverage_locate_pins_the_right_twin_row():
    # Four bilan blocks share the label "Moins provisions pour dépréciation".
    # The unverifiable one is the THIRD row — its locate entry must carry the
    # positional row index so the outline cannot land on the healthy first twin.
    df = pd.DataFrame({
        "Label": ["Moins provisions pour dépréciation",
                  "Stocks",
                  "Moins provisions pour dépréciation"],
        "2023": ["-1 668 470", "120 000", "-2736"],
        "2022": ["-1 603 448", "100 000", "060201-"],   # 3rd row garbled
        "%": ["4,1%", "20,0%", ""],                     # 3rd row empty %
    })
    results = validate_variation_percentages(df)
    cov = [r for r in results if r.check_name == "Var% coverage"]
    assert len(cov) == 1 and len(cov[0].locate) == 1
    assert cov[0].locate[0]["row"] == 2        # the broken twin, not row 0
    # the healthy twin + Stocks verified normally
    family = [r for r in results if r.check_name == "Variation % Check"]
    assert family[0].stats["verified"] == 2
    assert family[0].stats["not_verifiable"] == 1


def test_vnc_rows_coverage_locate_pins_missing_cell_row():
    df = pd.DataFrame({
        "Label": ["Immobilisations corporelles", "Amortissements", "VNC"],
        "2023": ["17 210 298", "-13 473 644", "3736654"],
        "2022": ["16 759 552", "", "3 789 609"],   # Amort missing in 2022
    })
    from accounting.rule_engine import validate_vnc_rows
    results = validate_vnc_rows(df)
    cov = [r for r in results if r.check_name == "VNC coverage"]
    assert len(cov) == 1 and len(cov[0].locate) == 1
    t = cov[0].locate[0]
    assert t["row"] == 1 and t["label"] == "Amortissements"   # the missing cell's row
    assert t["columns"] == ["2022"]


def test_vnc_rows_coverage_for_missing_values():
    df = pd.DataFrame({
        "Label": ["Immobilisations corporelles", "Amortissements", "VNC"],
        "2023": ["17 210 298", "-13 473 644", "3736654"],
        "2022": ["16 759 552", "", "3 789 609"],   # Amort missing in 2022
    })
    from accounting.rule_engine import validate_vnc_rows
    results = validate_vnc_rows(df)
    assert [r for r in results if not r.passed] == []
    cov = [r for r in results if r.check_name == "VNC coverage"]
    assert len(cov) == 1 and cov[0].skipped and "Amort" in cov[0].message
    summary = [r for r in results if r.check_name == "VNC Row-Layout"]
    assert summary and "1 not verifiable" in summary[0].message


# ---------------------------------------------------------------------------
# D9 — implausible computed expected is skipped, not reported
# ---------------------------------------------------------------------------

def test_implausible_expected_is_skipped_not_failed():
    df = pd.DataFrame({
        "Label": ["Stocks"],
        "2023": ["5 000 000"],
        "2022": ["100"],          # scale-broken prior → expected ~4,999,900%
        "%": ["5,0%"],
    })
    results = validate_variation_percentages(df)
    failures = [r for r in results if not r.passed]
    assert failures == []
    skips = [r for r in results if r.skipped]
    assert len(skips) == 1
    assert "Skipped" in skips[0].message


# ---------------------------------------------------------------------------
# D7 — skipped ≠ pass in report accounting and scoring
# ---------------------------------------------------------------------------

def test_report_excludes_skipped_from_counts_and_score():
    report = ValidationReport()
    report.add_check(ValidationResult("A", passed=True, message="", severity="CRITICAL"))
    report.add_check(ValidationResult("B", passed=False, message="", severity="CRITICAL"))
    report.add_check(ValidationResult("C", passed=True, message="", severity="WARNING",
                                      skipped=True))
    assert report.passed_checks == 1
    assert report.failed_checks == 1
    assert report.skipped_checks == 1
    report.compute_severity_weighted_score()
    # Only A (pass, w=3) and B (fail, w=3) count → 3/6.
    assert report.severity_weighted_score == 0.5
    d = report.to_dict()
    assert d["skipped_checks"] == 1
    assert d["checks"][2]["skipped"] is True


def test_info_severity_does_not_move_score():
    report = ValidationReport()
    report.add_check(ValidationResult("A", passed=True, message="", severity="ERROR"))
    report.add_check(ValidationResult("V4", passed=False, message="", severity="INFO"))
    report.compute_severity_weighted_score()
    assert report.severity_weighted_score == 1.0  # INFO weight 0


def test_v4_skip_is_marked_skipped():
    results = compute_ratios({})  # no bilan grouped → V4 cannot run
    assert len(results) == 1
    assert results[0].skipped is True
    assert results[0].severity == "INFO"


# ---------------------------------------------------------------------------
# D2 — VNC at canonical scale; V1-006 garbled detection preserved
# ---------------------------------------------------------------------------

def test_vnc_passes_at_canonical_scale():
    # Column-layout VNC with a dot-as-thousands Amort cell: naive parsing
    # (-13.47) would break the identity; canonical (-13 473 644) satisfies it.
    df = pd.DataFrame({
        "Label": ["Immobilisations corporelles"],
        "Brut": ["17 210 298"],
        "Amortissements": ["-13 473.644"],
        "Net": ["3736654"],
    })
    results = validate_vnc_consistency(df)
    failures = [r for r in results if not r.passed]
    assert failures == [], [r.message for r in failures]
    assert any(r.check_name == "VNC Consistency" and r.passed for r in results)


def test_field_type_check_still_flags_garbled_cells():
    # V1-006 must keep using the stricter lenient parser: trailing-sign and
    # quote-garbled cells stay flagged even though parse_french_number would
    # accept "060201-" (R1).
    df = pd.DataFrame({
        "Label": ["Moins provisions pou", "Moins provisions pou"],
        "2023": ["060201-", "%5'9-"],
        "2022": ["1 000", "2 000"],
    })
    results = validate_field_types(df)
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert "2 invalid" in fails[0].message
    assert fails[0].severity == "ERROR"  # D8: V1-006 spec severity


def test_field_type_check_flags_garbled_prefix_cells():
    # '"41 219' / '*41219' parse numerically after symbol-stripping, silently
    # swallowing what is usually a mis-OCR'd minus sign — V1-006 must flag
    # them as invalid instead of passing them as clean numbers.
    df = pd.DataFrame({
        "Label": ["Provisions", "Provisions"],
        "2023": ['"41 219', "*41219"],
        "2022": ["1 000", "2 000"],
    })
    results = validate_field_types(df)
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert "2 invalid" in fails[0].message
    # plain negatives / parens must NOT be flagged
    ok = validate_field_types(pd.DataFrame({
        "Label": ["A", "B"], "2023": ["-41 219", "(2211)"], "2022": ["1", "2"]}))
    assert all(r.passed for r in ok)
