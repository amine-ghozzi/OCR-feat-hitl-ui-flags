"""Validation-rule audit fixes (2026-06-14): make all 15 V1–V4 rules functional
and correct. Covers the value-column selection bugs (Note / % / period order),
VNC-aware footing, actif-only + cross-segment balance, V3 silent-pass→verified,
and the newly-implemented V2-003 / V4-002.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from accounting.validator import (  # noqa: E402
    select_value_column, validate_row_sums, validate_balance_sheet,
)
from accounting.rule_engine import (  # noqa: E402
    validate_with_rules, detect_anomalies, validate_report_a_nouveau,
    validate_balance_cross_segment, compute_ratios,
    validate_suspicious_proportions,
)
from accounting.statement_segmenter import segment_tables, group_by_statement  # noqa: E402


# ---------------------------------------------------------------------------
# value-column selection: skip Notes / % / Variation, prefer the period column
# ---------------------------------------------------------------------------

def test_select_value_column_skips_notes_and_percent():
    # Columns in the awkward order seen on real bilans: % before the years,
    # plus a Notes code column (A1..) that parses as numeric.
    df = pd.DataFrame({
        "": ["Stocks", "Total"],
        "%": ["4,1%", "2,0%"],
        "2022": ["100 000", "200 000"],
        "2023": ["110 000", "220 000"],
        "Notes": ["A1", "A2"],
        "Variation": ["10 000", "20 000"],
    })
    # must pick the most-recent YEAR column, never %, Variation or Notes
    assert select_value_column(df) == "2023"


def test_select_value_column_prefers_net():
    df = pd.DataFrame({"": ["x"], "Brut": ["100"], "Amort": ["-10"], "Net": ["90"]})
    assert select_value_column(df) == "Net"


# ---------------------------------------------------------------------------
# V1-004 — footing on Brut/Amort/Net row layout + mixed plain leaves
# ---------------------------------------------------------------------------

def test_row_sums_foots_vnc_row_layout():
    # immobilisations block: Brut / Amort / VNC ×2, then the subtotal = Σ VNC.
    df = pd.DataFrame({
        "": ["Immob incorporelles", "Amortissements", "VNC",
             "Immob corporelles", "Amortissements", "VNC",
             "Total actifs immobilises"],
        "2023": ["1 000", "-300", "700", "5 000", "-2 000", "3 000", "3 700"],
    })
    res = validate_row_sums(df)
    row_checks = [r for r in res if r.check_name.startswith("Row Sum:")]
    assert row_checks and all(r.passed for r in row_checks), \
        [(r.check_name, r.message) for r in row_checks if not r.passed]


def test_row_sums_mixed_vnc_and_plain_leaf():
    # a VNC triplet + a plain direct leaf (Liquidités) must BOTH count.
    df = pd.DataFrame({
        "": ["Stocks", "Moins provisions", "VNC", "Liquidites",
             "Total actifs courants"],
        "2023": ["1 200", "-200", "1 000", "500", "1 500"],
    })
    res = validate_row_sums(df)
    rc = [r for r in res if r.check_name.startswith("Row Sum:")]
    assert rc and all(r.passed for r in rc), [(r.check_name, r.message) for r in rc]


def test_row_sums_ignores_note_column():
    # The Notes A1.. column must not be summed (would yield empty/garbage).
    df = pd.DataFrame({
        "": ["Ligne A", "Ligne B", "Total"],
        "Notes": ["A1", "A2", ""],
        "2023": ["100", "200", "300"],
    })
    res = validate_row_sums(df)
    rc = [r for r in res if r.check_name.startswith("Row Sum:")]
    assert rc and rc[0].passed and "300" in rc[0].message


# ---------------------------------------------------------------------------
# V1-001 — actif-only skip + cross-segment balance for split bilan
# ---------------------------------------------------------------------------

def test_balance_skips_on_actif_only():
    df = pd.DataFrame({
        "": ["Stocks", "TOTAL DES ACTIFS"],
        "2023": ["20 000", "20 000"],
    })
    r = validate_balance_sheet(df)
    assert r.skipped and "actif-only" in r.message.lower() or "both sides" in r.message.lower()


def test_cross_segment_balance_uses_grand_totals():
    actif = pd.DataFrame({
        "": ["Total actifs non courants", "Total actifs courants", "TOTAL DES ACTIFS"],
        "2023": ["30 000", "70 000", "100 000"],
    })
    passif = pd.DataFrame({
        "": ["Total des capitaux propres", "Total des passifs",
             "TOTAL DES CAPITAUX PROPRES ET PASSIFS"],
        "2023": ["40 000", "60 000", "100 000"],
    })
    grouped = group_by_statement(segment_tables([actif, passif]))
    # ensure the split is recognized
    if not (grouped.get("bilan_actif") and grouped.get("bilan_passif")):
        import pytest
        pytest.skip("segmenter did not split into actif/passif")
    res = validate_balance_cross_segment(grouped)
    assert len(res) == 1 and res[0].passed
    # grand totals (100k), NOT a subtotal (30k/70k/40k/60k)
    assert "100,000" in res[0].message


# ---------------------------------------------------------------------------
# V3 — silent-pass becomes a reported verified-pass
# ---------------------------------------------------------------------------

def test_v3_negative_reports_verified_pass():
    df = pd.DataFrame({
        "": ["Capital social", "Stocks", "Total"],
        "2023": ["50 000", "20 000", "70 000"],
    })
    segs = segment_tables([df])
    res = detect_anomalies(segs)
    neg = [r for r in res if "Negative Where Unexpected" in r.check_name]
    assert len(neg) == 1 and neg[0].passed and not neg[0].skipped


def test_v3_negative_still_flags_real_negative():
    df = pd.DataFrame({
        "": ["Capital social", "Stocks"],
        "2023": ["-50 000", "20 000"],
    })
    segs = segment_tables([df])
    res = detect_anomalies(segs)
    fails = [r for r in res if "Negative" in r.check_name and not r.passed]
    assert len(fails) == 1 and fails[0].actual == -50000


# ---------------------------------------------------------------------------
# V2-003 Report-à-Nouveau — period order + dividends
# ---------------------------------------------------------------------------

def test_report_a_nouveau_handles_reversed_period_order():
    # Columns 2022 BEFORE 2023; N must still be 2023 (most recent).
    df = pd.DataFrame({
        "": ["Resultats Reportes", "Resultat de l'exercice", "Total des capitaux propres"],
        "2022": ["-8 000", "5 000", "10 000"],
        "2023": ["-3 000", "6 000", "13 000"],   # RAN N=-3000 = RAN N-1(-8000)+Res N-1(5000)
    })
    grouped = group_by_statement(segment_tables([df]))
    res = validate_report_a_nouveau(grouped)
    assert res and not res[0].skipped and res[0].passed, \
        (res and res[0].message)


def test_report_a_nouveau_skips_without_inputs():
    # A real bilan (passif-side rows) but no Report-à-Nouveau row → the
    # validator runs and SKIPS with a reason (not a silent no-result).
    df = pd.DataFrame({
        "": ["Capital social", "Reserves", "Total des capitaux propres",
             "Emprunts", "Total des passifs"],
        "2023": ["40 000", "5 000", "45 000", "60 000", "60 000"],
        "2022": ["38 000", "4 000", "42 000", "55 000", "55 000"],
    })
    grouped = group_by_statement(segment_tables([df]))
    res = validate_report_a_nouveau(grouped)
    assert res and res[0].skipped, (res and res[0].message if res else "no result")


# ---------------------------------------------------------------------------
# V4-002 Trésorerie Nette
# ---------------------------------------------------------------------------

def test_tresorerie_nette_computes():
    actif = pd.DataFrame({
        "": ["Total des actifs non courants", "Liquidites et equivalents",
             "Total des actifs courants"],
        "2023": ["10 000", "5 000", "15 000"],
    })
    passif = pd.DataFrame({
        "": ["Total des capitaux propres", "Concours bancaires courants",
             "Total des passifs non courants"],
        "2023": ["12 000", "2 000", "8 000"],
    })
    grouped = group_by_statement(segment_tables([actif, passif]))
    res = compute_ratios(grouped)
    tn = [r for r in res if "Tresorerie Nette" in r.check_name]
    assert tn and not tn[0].skipped
    assert tn[0].actual == 3000.0   # 5000 − 2000


# ---------------------------------------------------------------------------
# end-to-end: the full report has no false failures on a clean split bilan
# ---------------------------------------------------------------------------

def test_full_clean_split_bilan_balances():
    actif = pd.DataFrame({
        "": ["Immobilisations", "Total actifs non courants",
             "Stocks", "Total actifs courants", "TOTAL DES ACTIFS"],
        "2023": ["30 000", "30 000", "70 000", "70 000", "100 000"],
        "2022": ["28 000", "28 000", "65 000", "65 000", "93 000"],
    })
    passif = pd.DataFrame({
        "": ["Capital social", "Total des capitaux propres",
             "Emprunts", "Total des passifs",
             "TOTAL DES CAPITAUX PROPRES ET PASSIFS"],
        "2023": ["40 000", "40 000", "60 000", "60 000", "100 000"],
        "2022": ["38 000", "38 000", "55 000", "55 000", "93 000"],
    })
    rep = validate_with_rules([actif, passif], standard="NCT")
    bal = [c for c in rep.checks if "Balance" in c.check_name]
    assert len(bal) == 1 and bal[0].passed, (bal and bal[0].message)


# ---------------------------------------------------------------------------
# V3-004 Suspicious Proportions — cross-statement heuristic (now activated)
# ---------------------------------------------------------------------------

def _bilan_cr_pair(creances="300 000", stocks="8 000", ca="100 000"):
    bilan = pd.DataFrame({
        "": ["Immobilisations corporelles", "Stocks", "Creances clients",
             "TOTAL DES ACTIFS"],
        "2023": ["50 000", stocks, creances, "400 000"],
    })
    cr = pd.DataFrame({
        "": ["Chiffre d'affaires", "Charges d'exploitation", "Resultat net"],
        "2023": [ca, "80 000", "20 000"],
    })
    return bilan, cr


def _grouped_pair(**kw):
    bilan, cr = _bilan_cr_pair(**kw)
    grouped = group_by_statement(segment_tables([bilan, cr]))
    if not (grouped.get("bilan") and grouped.get("compte_resultat")):
        import pytest
        pytest.skip("segmenter did not classify both statements")
    return grouped


def test_v3_004_flags_creances_over_2x_ca():
    # créances 300k vs CA 100k = 3.0× > 2.0× → WARNING fail, locatable.
    res = validate_suspicious_proportions(_grouped_pair())
    creances = [r for r in res if "Créances/CA" in r.check_name]
    assert len(creances) == 1 and not creances[0].passed
    assert creances[0].actual == 300000.0 and creances[0].locate


def test_v3_004_passes_when_within_bounds():
    # créances 120k vs CA 100k = 1.2× ≤ 2.0× ; stocks 0.08× ≤ 1.0× → both pass.
    res = validate_suspicious_proportions(_grouped_pair(creances="120 000"))
    assert res and all(r.passed and not r.skipped for r in res), \
        [(r.check_name, r.message) for r in res]


def test_v3_004_flags_stocks_over_1x_ca():
    # stocks 150k vs CA 100k = 1.5× > 1.0× → fail on the Stocks metric.
    res = validate_suspicious_proportions(
        _grouped_pair(creances="120 000", stocks="150 000"))
    stocks = [r for r in res if "Stocks/CA" in r.check_name]
    assert len(stocks) == 1 and not stocks[0].passed


def test_v3_004_skips_without_compte_resultat():
    bilan, _ = _bilan_cr_pair()
    grouped = group_by_statement(segment_tables([bilan]))
    res = validate_suspicious_proportions(grouped)
    assert len(res) == 1 and res[0].skipped
    assert "Compte de Résultat" in res[0].message


def test_v3_004_active_in_coverage_panel():
    # The rule must no longer report as 'inactive' — it now runs end-to-end.
    from hitl.review_view import coverage
    bilan, cr = _bilan_cr_pair()
    rep = validate_with_rules([bilan, cr], standard="NCT")
    cov = coverage(rep, {"bilan", "compte_resultat"}, "NCT")
    v3 = {e["rule_id"]: e for e in cov["V3"]["rules"]}
    assert v3["V3-004"]["status"] != "inactive"
    # with 3× créances it should surface as a fail, end-to-end
    assert v3["V3-004"]["status"] == "fail"
