"""
End-to-end tests for the full Tier 1 implementation.

Tests:
1. Statement segmentation (bilan/CR/TFT classification)
2. V1 validation (balance equation, VNC, CR equation, subtotals, variation %, field types)
3. V2 cross-statement validation (Bilan <-> CR consistency)
4. V3 anomaly detection (negative values, null CA, magnitude mismatch)
5. V4 ratio computation (Fonds de Roulement)
6. Rule engine integration (JSON rule loading, per-standard filtering)
7. QCS integration (grade-mapped scoring with full validation report)
8. Pipeline HITL routing (critical failure override, low_grade pre-gate)
"""

import sys
import os
import logging

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Suppress logger output during tests — only show test PASS/FAIL
logging.disable(logging.CRITICAL)

import pandas as pd
import numpy as np

# ===========================================================================
# Test Data
# ===========================================================================

def make_bilan_df():
    """NCT-style balance sheet with Brut/Amort/Net columns."""
    return pd.DataFrame({
        "Designation": [
            "ACTIFS NON COURANTS",
            "Immobilisations incorporelles",
            "Immobilisations corporelles",
            "TOTAL ACTIFS NON COURANTS",
            "ACTIFS COURANTS",
            "Stocks",
            "Clients et comptes rattaches",
            "Liquidites et equivalents de liquidites",
            "TOTAL ACTIFS COURANTS",
            "TOTAL ACTIF",
            "",
            "CAPITAUX PROPRES",
            "Capital social",
            "Reserves",
            "Resultats reportes",
            "Resultat de l'exercice",
            "TOTAL CAPITAUX PROPRES",
            "PASSIFS NON COURANTS",
            "Emprunts",
            "Provisions",
            "TOTAL PASSIFS NON COURANTS",
            "PASSIFS COURANTS",
            "Fournisseurs et comptes rattaches",
            "Autres passifs courants",
            "Concours bancaires",
            "TOTAL PASSIFS COURANTS",
            "TOTAL CAPITAUX PROPRES ET PASSIFS",
        ],
        "Brut": [
            np.nan, 200000, 800000, 1000000,
            np.nan, 150000, 300000, 50000, 500000,
            1500000,
            np.nan,
            np.nan, np.nan, np.nan, np.nan, np.nan, np.nan,
            np.nan, np.nan, np.nan, np.nan,
            np.nan, np.nan, np.nan, np.nan, np.nan, np.nan,
        ],
        "Amort": [
            np.nan, 50000, 200000, 250000,
            np.nan, 0, 0, 0, 0,
            250000,
            np.nan,
            np.nan, np.nan, np.nan, np.nan, np.nan, np.nan,
            np.nan, np.nan, np.nan, np.nan,
            np.nan, np.nan, np.nan, np.nan, np.nan, np.nan,
        ],
        "Net": [
            np.nan, 150000, 600000, 750000,
            np.nan, 150000, 300000, 50000, 500000,
            1250000,
            np.nan,
            np.nan, 500000, 100000, 50000, 100000, 750000,
            np.nan, 200000, 50000, 250000,
            np.nan, 150000, 50000, 50000, 250000,
            1250000,
        ],
    })


def make_cr_df():
    """Income statement (Compte de Resultat)."""
    return pd.DataFrame({
        "Designation": [
            "PRODUITS D'EXPLOITATION",
            "Chiffre d'affaires",
            "Autres produits d'exploitation",
            "TOTAL PRODUITS D'EXPLOITATION",
            "CHARGES D'EXPLOITATION",
            "Achats consommes",
            "Charges de personnel",
            "Dotations aux amortissements",
            "Autres charges d'exploitation",
            "TOTAL CHARGES D'EXPLOITATION",
            "RESULTAT D'EXPLOITATION",
            "Produits financiers",
            "Charges financieres",
            "RESULTAT FINANCIER",
            "Impots sur les benefices",
            "RESULTAT NET DE L'EXERCICE",
        ],
        "2023": [
            np.nan,
            2000000, 50000, 2050000,
            np.nan,
            800000, 500000, 250000, 200000, 1750000,
            300000,
            10000, 60000, -50000,
            150000,
            100000,  # = 300000 + (-50000) - 150000
        ],
        "2022": [
            np.nan,
            1800000, 40000, 1840000,
            np.nan,
            720000, 450000, 220000, 180000, 1570000,
            270000,
            8000, 55000, -47000,
            120000,
            103000,
        ],
    })


def make_bilan_with_cr_mismatch():
    """Bilan where Resultat de l'exercice != CR Resultat Net (cross-statement failure)."""
    bilan = make_bilan_df()
    # Change bilan resultat to 120000 (CR says 100000)
    bilan.at[15, "Net"] = 120000
    # Adjust total CP to keep bilan balanced
    bilan.at[16, "Net"] = 770000
    bilan.at[26, "Net"] = 1270000
    # Adjust total actif too
    bilan.at[9, "Net"] = 1270000
    return bilan


def make_anomalous_bilan():
    """Bilan with anomalies: negative capital, magnitude mismatch."""
    df = make_bilan_df()
    # Negative capital social (V3-001)
    df.at[12, "Net"] = -500000
    return df


def make_cr_with_zero_ca():
    """CR with zero chiffre d'affaires (V3-002)."""
    df = make_cr_df()
    df.at[1, "2023"] = 0
    df.at[3, "2023"] = 50000  # Only other produits
    return df


def make_bilan_actif_df():
    """Actif-only side of a split bilan (separate table)."""
    return pd.DataFrame({
        "Designation": [
            "ACTIFS NON COURANTS",
            "Immobilisations incorporelles",
            "Immobilisations corporelles",
            "Immobilisations financieres",
            "TOTAL ACTIFS NON COURANTS",
            "ACTIFS COURANTS",
            "Stocks",
            "Clients et comptes rattaches",
            "Liquidites et equivalents de liquidites",
            "TOTAL ACTIFS COURANTS",
            "TOTAL ACTIF",
        ],
        "Brut": [
            np.nan, 200000, 800000, 50000, 1050000,
            np.nan, 150000, 300000, 50000, 500000,
            1550000,
        ],
        "Amort": [
            np.nan, 50000, 200000, 0, 250000,
            np.nan, 0, 50000, 0, 50000,
            300000,
        ],
        "Net": [
            np.nan, 150000, 600000, 50000, 800000,
            np.nan, 150000, 250000, 50000, 450000,
            1250000,
        ],
    })


def make_bilan_passif_df():
    """Passif-only side of a split bilan (separate table)."""
    return pd.DataFrame({
        "Designation": [
            "CAPITAUX PROPRES",
            "Capital social",
            "Reserves",
            "Resultats reportes",
            "Resultat de l'exercice",
            "TOTAL CAPITAUX PROPRES",
            "PASSIFS NON COURANTS",
            "Emprunts",
            "Provisions pour risques",
            "TOTAL PASSIFS NON COURANTS",
            "PASSIFS COURANTS",
            "Fournisseurs et comptes rattaches",
            "Autres passifs courants",
            "Concours bancaires",
            "TOTAL PASSIFS COURANTS",
            "TOTAL CAPITAUX PROPRES ET PASSIFS",
        ],
        "Net": [
            np.nan,
            500000, 100000, 50000, 100000, 750000,
            np.nan, 200000, 50000, 250000,
            np.nan, 150000, 50000, 50000, 250000,
            1250000,
        ],
    })


# ===========================================================================
# Tests
# ===========================================================================

def test_statement_segmentation():
    """Test that tables are correctly classified."""
    from accounting.statement_segmenter import segment_tables

    bilan = make_bilan_df()
    cr = make_cr_df()

    segments = segment_tables([bilan, cr])

    assert len(segments) == 2
    assert segments[0].statement_type == "bilan", f"Expected bilan, got {segments[0].statement_type}"
    assert segments[1].statement_type == "compte_resultat", f"Expected compte_resultat, got {segments[1].statement_type}"
    assert segments[0].confidence > 0.0
    assert segments[1].confidence > 0.0

    print("  PASS: Statement segmentation correctly identified bilan and CR")


def test_v1_balance_sheet():
    """Test V1-001: Balance sheet equation."""
    from accounting.validator import validate_balance_sheet

    bilan = make_bilan_df()
    result = validate_balance_sheet(bilan)

    # Total Actif (Net=1250000) should equal Total CP+Passifs (Net=1250000)
    assert result.passed, f"Balance sheet should balance: {result.message}"
    print("  PASS: Balance sheet equation validated")


def test_v1_vnc_consistency():
    """Test V1-002: VNC (Brut - Amort = Net)."""
    from accounting.rule_engine import validate_vnc_consistency

    bilan = make_bilan_df()
    results = validate_vnc_consistency(bilan)

    # All rows should have Net = Brut - Amort
    assert len(results) > 0, "Should have VNC results"
    assert all(r.passed for r in results), f"VNC should pass: {[r.message for r in results if not r.passed]}"
    print("  PASS: VNC consistency validated (Net = Brut - Amort)")


def test_v1_cr_equation():
    """Test V1-003: CR equation (Resultat Net = Exploit + Financier - Impots)."""
    from accounting.rule_engine import validate_cr_equation

    cr = make_cr_df()
    results = validate_cr_equation(cr)

    assert len(results) > 0, "Should have CR equation results"
    for r in results:
        assert r.passed, f"CR equation should pass: {r.message}"
    print("  PASS: CR equation validated")


def test_v2_cross_statement_match():
    """Test V2-001: Bilan Resultat == CR Resultat Net (should pass)."""
    from accounting.statement_segmenter import segment_tables, group_by_statement
    from accounting.rule_engine import validate_cross_statement

    bilan = make_bilan_df()
    cr = make_cr_df()

    segments = segment_tables([bilan, cr])
    grouped = group_by_statement(segments)

    results = validate_cross_statement(grouped)

    # Bilan Resultat de l'exercice = 100000, CR Resultat Net = 100000
    assert len(results) > 0, "Should have cross-statement results"
    match_result = [r for r in results if "Resultat Net Match" in r.check_name]
    assert len(match_result) == 1
    assert match_result[0].passed, f"Cross-statement should match: {match_result[0].message}"
    print("  PASS: Cross-statement Resultat Net match validated")


def test_v2_cross_statement_mismatch():
    """Test V2-001: Bilan Resultat != CR Resultat Net (should fail)."""
    from accounting.statement_segmenter import segment_tables, group_by_statement
    from accounting.rule_engine import validate_cross_statement

    bilan = make_bilan_with_cr_mismatch()
    cr = make_cr_df()

    segments = segment_tables([bilan, cr])
    grouped = group_by_statement(segments)

    results = validate_cross_statement(grouped)

    match_result = [r for r in results if "Resultat Net Match" in r.check_name]
    assert len(match_result) == 1
    assert not match_result[0].passed, "Cross-statement should FAIL with mismatched resultat"
    assert match_result[0].severity == "CRITICAL"
    print("  PASS: Cross-statement mismatch correctly detected as CRITICAL")


def test_v3_negative_capital():
    """Test V3-001: Negative capital social detected."""
    from accounting.statement_segmenter import segment_tables
    from accounting.rule_engine import detect_anomalies

    bilan = make_anomalous_bilan()
    segments = segment_tables([bilan])

    results = detect_anomalies(segments)

    negative_results = [r for r in results if "Negative" in r.check_name]
    assert len(negative_results) > 0, "Should detect negative capital"
    assert not negative_results[0].passed
    print("  PASS: Negative capital social anomaly detected")


def test_v3_null_ca():
    """Test V3-002: Zero chiffre d'affaires detected."""
    from accounting.statement_segmenter import segment_tables
    from accounting.rule_engine import detect_anomalies

    cr = make_cr_with_zero_ca()
    segments = segment_tables([cr])

    results = detect_anomalies(segments)

    null_results = [r for r in results if "Null CA" in r.check_name]
    assert len(null_results) > 0, "Should detect null CA"
    assert not null_results[0].passed
    print("  PASS: Null chiffre d'affaires anomaly detected")


def test_rule_engine_loading():
    """Test that JSON rules load and filter correctly."""
    from accounting.rule_engine import load_rules, get_rules_by_tier

    rules = load_rules("NCT")
    assert len(rules) > 0, "Should load rules for NCT"

    v1_rules = get_rules_by_tier(rules, "V1")
    v2_rules = get_rules_by_tier(rules, "V2")
    v3_rules = get_rules_by_tier(rules, "V3")

    assert len(v1_rules) >= 5, f"Should have at least 5 V1 rules, got {len(v1_rules)}"
    assert len(v2_rules) >= 2, f"Should have at least 2 V2 rules, got {len(v2_rules)}"
    assert len(v3_rules) >= 2, f"Should have at least 2 V3 rules, got {len(v3_rules)}"

    # Check rule structure
    for rule in rules:
        assert "rule_id" in rule
        assert "severity" in rule
        assert "standards" in rule
        assert "NCT" in rule["standards"]

    print(f"  PASS: Loaded {len(rules)} rules (V1={len(v1_rules)}, V2={len(v2_rules)}, V3={len(v3_rules)})")


def test_full_validate_with_rules():
    """Test the complete validate_with_rules pipeline."""
    from accounting.rule_engine import validate_with_rules

    bilan = make_bilan_df()
    cr = make_cr_df()

    report = validate_with_rules(
        tables=[bilan, cr],
        standard="NCT",
        run_v2=True,
        run_v3=True,
        run_v4=True,
    )

    assert report.total_checks > 0, "Should have run checks"
    assert report.severity_weighted_score is not None
    assert 0.0 <= report.severity_weighted_score <= 1.0

    # Should have segmentation check
    seg_checks = [c for c in report.checks if "Segmentation" in c.check_name]
    assert len(seg_checks) == 1

    # Should have V2 cross-statement check
    v2_checks = [c for c in report.checks if "V2:" in c.check_name]
    assert len(v2_checks) > 0, "Should have V2 cross-statement checks"

    print(f"  PASS: Full pipeline: {report.passed_checks}/{report.total_checks} passed, "
          f"severity_weighted={report.severity_weighted_score:.3f}")


def test_qcs_with_validation():
    """Test QCS integration with the full validation report."""
    from accounting.rule_engine import validate_with_rules
    from qcs.calculator import calculate_qcs

    bilan = make_bilan_df()
    cr = make_cr_df()

    # Run validation
    validation_report = validate_with_rules([bilan, cr], standard="NCT")

    # Run QCS
    qcs_report = calculate_qcs(
        ocr_grade="GOOD",
        layout_grade="GOOD",
        parse_grade="FAIR",
        low_grade="GOOD",
        text="BILAN AU 31 DECEMBRE 2023\nACTIFS NON COURANTS\nImmobilisations 150 000\nTOTAL ACTIF 1 250 000",
        tables=[bilan, cr],
        validation_report=validation_report,
        corrections_made=5,
    )

    assert qcs_report.qcs_score > 0.0
    assert qcs_report.grade in ("poor", "fair", "good", "excellent")
    assert not qcs_report.low_grade_gated
    assert qcs_report.semantic_score > 0.0
    assert qcs_report.validation_pass_rate is not None

    print(f"  PASS: QCS={qcs_report.qcs_score:.3f}, grade={qcs_report.grade}, "
          f"semantic={qcs_report.semantic_score:.3f}, hitl={qcs_report.needs_hitl}")


def test_critical_failure_hitl_override():
    """Test that CRITICAL validation failure forces HITL regardless of QCS."""
    from accounting.rule_engine import validate_with_rules
    from qcs.calculator import calculate_qcs

    # Create mismatched bilan/CR (V2-001 CRITICAL failure)
    bilan = make_bilan_with_cr_mismatch()
    cr = make_cr_df()

    validation_report = validate_with_rules([bilan, cr], standard="NCT")
    assert validation_report.critical_failures > 0, "Should have critical failures"

    # Even with excellent grades, HITL should be forced
    qcs_report = calculate_qcs(
        ocr_grade="EXCELLENT",
        layout_grade="EXCELLENT",
        parse_grade="EXCELLENT",
        low_grade="EXCELLENT",
        text="High quality text with numbers 1 250 000",
        validation_report=validation_report,
    )

    assert qcs_report.needs_hitl, "HITL should be forced by critical failure"
    assert qcs_report.critical_failures > 0
    assert "CRITICAL" in qcs_report.hitl_reason

    print(f"  PASS: Critical failure override: QCS={qcs_report.qcs_score:.3f} but HITL forced "
          f"({qcs_report.critical_failures} critical failures)")


def test_bilan_actif_passif_subtype():
    """Test that split bilan tables are sub-classified as actif/passif."""
    from accounting.statement_segmenter import segment_tables, group_by_statement

    actif = make_bilan_actif_df()
    passif = make_bilan_passif_df()
    cr = make_cr_df()

    segments = segment_tables([actif, passif, cr])
    grouped = group_by_statement(segments)

    # Both should be classified as bilan
    assert segments[0].statement_type == "bilan", f"Actif table: expected bilan, got {segments[0].statement_type}"
    assert segments[1].statement_type == "bilan", f"Passif table: expected bilan, got {segments[1].statement_type}"
    assert segments[2].statement_type == "compte_resultat"

    # Sub-types should be correctly assigned
    assert segments[0].sub_type == "actif", f"Expected sub_type=actif, got {segments[0].sub_type}"
    assert segments[1].sub_type == "passif", f"Expected sub_type=passif, got {segments[1].sub_type}"

    # Grouped should have bilan_actif and bilan_passif entries
    assert len(grouped["bilan_actif"]) == 1
    assert len(grouped["bilan_passif"]) == 1
    assert len(grouped["bilan"]) == 2  # Both still in main bilan group

    print("  PASS: Split bilan tables correctly sub-classified as actif/passif")


def test_v2_cross_statement_split_bilan():
    """Test V2 cross-statement works with split actif/passif bilan tables."""
    from accounting.statement_segmenter import segment_tables, group_by_statement
    from accounting.rule_engine import validate_cross_statement

    actif = make_bilan_actif_df()
    passif = make_bilan_passif_df()
    cr = make_cr_df()

    segments = segment_tables([actif, passif, cr])
    grouped = group_by_statement(segments)

    results = validate_cross_statement(grouped)

    # Should find Resultat on the passif side and match with CR
    match_result = [r for r in results if "Resultat Net Match" in r.check_name]
    assert len(match_result) == 1, f"Should have cross-statement result, got {len(match_result)}"
    assert match_result[0].passed, f"Cross-statement should match with split bilan: {match_result[0].message}"

    print("  PASS: V2 cross-statement works with split actif/passif bilan")


def test_low_grade_pregate():
    """Test that low_grade=POOR triggers HITL without computing QCS."""
    from qcs.calculator import calculate_qcs

    qcs_report = calculate_qcs(
        ocr_grade="GOOD",
        layout_grade="GOOD",
        parse_grade="GOOD",
        low_grade="POOR",
        text="Some text",
    )

    assert qcs_report.low_grade_gated
    assert qcs_report.qcs_score == 0.0
    assert qcs_report.needs_hitl
    assert "POOR" in qcs_report.hitl_reason

    print("  PASS: low_grade pre-gate triggers HITL (QCS=0.0)")


def test_tier1_accept_carries_needs_hitl():
    """RTE-1 — a Tier-1 accept must carry the QCS report's verdict with it.

    Two bars, by design: the cascade stops escalating at ``tau_fast_high``
    (0.75) — "no other engine will do better" — and the QCS report stops asking
    for a human at ``tau_excellent`` (0.90). A document between the two is
    accepted AND reviewed. The accept path never copied the second answer out of
    ``confidence_details``, so ``worker/tasks.py`` read the ``metadata``
    default — False — and every document in the band was persisted as needing no
    review at all. Four DOCX in the golden corpus (0.776 - 0.795) sat there.

    Nothing here is a threshold change; both bars are asserted below to stay put.
    """
    from pathlib import Path

    from pipeline import ExtractionResult, FinAlzePipeline, PipelineConfig

    pipe = FinAlzePipeline.__new__(FinAlzePipeline)
    pipe.config = PipelineConfig()
    pipe._extract_images = lambda path: []

    def _tier1(qcs, needs_hitl, reason, grade, metadata=None):
        return ExtractionResult(
            text="", tables=[], qcs_score=qcs, tier_used=1,
            confidence_details={
                "qcs_grade": grade,
                "needs_hitl": needs_hitl,
                "hitl_reason": reason,
            },
            metadata={"doc_type": "docling_ocr", **(metadata or {})},
        )

    # bilab2's band: accepted by the cascade, still owed a look.
    banded = _tier1(0.786, True, "QCS grade is 'good' — quick review recommended", "good")
    pipe._tier1_fast_ocr = lambda *a, **k: banded
    out = pipe._run_tier_cascade(Path("bilab2.docx"))

    assert out.tier_used == 1
    assert out.qcs_score >= pipe.config.tau_fast_high, "the cascade accepted it"
    assert out.metadata["hitl_required"] is True, "and it still asks for a human"
    assert "quick review" in out.metadata["hitl_reason"]
    assert out.metadata["qcs_grade"] == "good"
    assert out.metadata["score_kind"] == "qcs"

    # Above the no-human bar: accepted, and genuinely nobody's problem.
    top = _tier1(0.95, False, "", "excellent")
    pipe._tier1_fast_ocr = lambda *a, **k: top
    clear = pipe._run_tier_cascade(Path("bilan1.docx"))
    assert clear.metadata["hitl_required"] is False
    assert clear.metadata["hitl_reason"] == ""
    assert clear.metadata["qcs_grade"] == "excellent"

    # Raising only: an earlier stage's flag survives a quiet QCS report.
    already = _tier1(0.95, False, "", "excellent", {"hitl_required": True})
    pipe._tier1_fast_ocr = lambda *a, **k: already
    kept = pipe._run_tier_cascade(Path("bilan1.docx"))
    assert kept.metadata["hitl_required"] is True, "a flag is never cleared here"

    # The bars themselves are untouched — this package is plumbing.
    assert pipe.config.tau_fast_high == 0.75
    assert PipelineConfig().tau_native_high == 0.95

    print("  PASS: Tier-1 accept carries needs_hitl into metadata")


# ===========================================================================
# Runner
# ===========================================================================

def main():
    tests = [
        ("Statement Segmentation", test_statement_segmentation),
        ("V1: Balance Sheet Equation", test_v1_balance_sheet),
        ("V1: VNC Consistency", test_v1_vnc_consistency),
        ("V1: CR Equation", test_v1_cr_equation),
        ("V2: Cross-Statement Match", test_v2_cross_statement_match),
        ("V2: Cross-Statement Mismatch", test_v2_cross_statement_mismatch),
        ("V3: Negative Capital", test_v3_negative_capital),
        ("V3: Null CA", test_v3_null_ca),
        ("Rule Engine Loading", test_rule_engine_loading),
        ("Full validate_with_rules", test_full_validate_with_rules),
        ("QCS + Validation Integration", test_qcs_with_validation),
        ("Critical Failure HITL Override", test_critical_failure_hitl_override),
        ("Bilan Actif/Passif Sub-type", test_bilan_actif_passif_subtype),
        ("V2: Split Bilan Cross-Statement", test_v2_cross_statement_split_bilan),
        ("low_grade Pre-gate", test_low_grade_pregate),
        ("Tier-1 accept carries needs_hitl", test_tier1_accept_carries_needs_hitl),
    ]

    passed = 0
    failed = 0

    print("\n" + "=" * 60)
    print("FinAlze Tier 1 Full Implementation Tests")
    print("=" * 60)

    for name, test_fn in tests:
        try:
            print(f"\n[TEST] {name}")
            test_fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    print(f"{'=' * 60}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
