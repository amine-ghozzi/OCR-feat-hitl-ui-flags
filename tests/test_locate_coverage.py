"""Locate-coverage (2026-06-15): every *working* check that maps to specific
rows must carry a ``locate`` target, so the HITL panel shows a 📍 Locate button
for it. Two earlier gaps are closed here:

  * cross-statement V2 (Résultat Net / Trésorerie) and V4 ratios (FR / Trésorerie
    Nette) referenced concrete rows but emitted no target;
  * the PASS branch of the V3 heuristics (Suspicious Proportion / Null CA /
    Negative aggregate) had none, while their FAIL branch did.

Plus the family aggregates (Row Sums, VNC, Var%) now roll up the union of the
rows they cover. Genuinely whole-table/meta checks (Field Type pass, Magnitude,
Tables Found, Statement Segmentation) intentionally stay target-less.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from accounting.rule_engine import (  # noqa: E402
    validate_with_rules, validate_cross_statement, compute_ratios,
    detect_anomalies, validate_suspicious_proportions,
)
from accounting.validator import validate_row_sums, validate_variation_percentages  # noqa: E402
from accounting.statement_segmenter import segment_tables, group_by_statement  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _actif():
    return pd.DataFrame({
        "": ["Immobilisations corporelles", "Total actifs non courants",
             "Stocks", "Creances clients", "Liquidites et equivalents",
             "Total des actifs courants", "TOTAL DES ACTIFS"],
        "2023": ["900", "900", "300", "200", "100", "600", "1500"],
    })


def _passif():
    return pd.DataFrame({
        "": ["Capital social", "Resultat de l'exercice", "Total des capitaux propres",
             "Concours bancaires courants", "Total des passifs non courants",
             "TOTAL DES CAPITAUX PROPRES ET PASSIFS"],
        "2023": ["1000", "200", "1400", "100", "100", "1500"],
    })


def _cr():
    return pd.DataFrame({
        "": ["Chiffre d'affaires", "Charges", "Resultat de l'exercice"],
        "2023": ["5000", "4800", "200"],
    })


def _grouped(*tables):
    return group_by_statement(segment_tables(list(tables)))


def _has_label(check, needle):
    return any(needle.lower() in str(t.get("label", "")).lower()
              for t in (check.locate or []))


# ---------------------------------------------------------------------------
# V2 cross-statement — both sides locatable
# ---------------------------------------------------------------------------

def test_v2_resultat_net_match_locates_both_statements():
    grouped = _grouped(_actif(), _passif(), _cr())
    res = validate_cross_statement(grouped)
    rn = [r for r in res if "Resultat Net Match" in r.check_name]
    assert rn and rn[0].locate, "V2 Résultat Net must carry a locate target"
    # one entry for the bilan résultat row, one for the CR résultat row
    assert len(rn[0].locate) == 2 and _has_label(rn[0], "resultat")


# ---------------------------------------------------------------------------
# V4 ratios — the rows feeding the ratio are locatable
# ---------------------------------------------------------------------------

def test_v4_fonds_de_roulement_locates_its_inputs():
    grouped = _grouped(_actif(), _passif(), _cr())
    res = compute_ratios(grouped)
    fr = [r for r in res if "Fonds de Roulement" in r.check_name and not r.skipped]
    assert fr and fr[0].locate, "V4 FR must locate the totals it used"
    assert _has_label(fr[0], "actifs non courants")


def test_v4_tresorerie_nette_locates_its_inputs():
    grouped = _grouped(_actif(), _passif(), _cr())
    res = compute_ratios(grouped)
    tn = [r for r in res if "Tresorerie Nette" in r.check_name and not r.skipped]
    assert tn and tn[0].locate
    assert _has_label(tn[0], "liquidites") or _has_label(tn[0], "concours")


# ---------------------------------------------------------------------------
# V3 PASS branches — locatable, not just the FAIL branch
# ---------------------------------------------------------------------------

def test_v3_null_ca_pass_is_locatable():
    segs = segment_tables([_cr()])
    res = detect_anomalies(segs)
    ca = [r for r in res if "Null CA" in r.check_name]
    assert ca and ca[0].passed and ca[0].locate and _has_label(ca[0], "chiffre")


def test_v3_suspicious_proportion_pass_is_locatable():
    grouped = _grouped(_actif(), _passif(), _cr())
    res = validate_suspicious_proportions(grouped)
    passes = [r for r in res if "Proportion" in r.check_name and r.passed and not r.skipped]
    assert passes and all(r.locate for r in passes)


def test_v3_negative_aggregate_pass_is_locatable():
    segs = segment_tables([_passif()])  # has Capital social → a checked field
    res = detect_anomalies(segs)
    agg = [r for r in res if r.check_name == "V3: Negative Where Unexpected"]
    assert agg and agg[0].passed and agg[0].locate
    assert _has_label(agg[0], "capital")


# ---------------------------------------------------------------------------
# Family aggregates — roll up the rows they cover
# ---------------------------------------------------------------------------

def test_row_sums_aggregate_locates_its_subtotals():
    df = pd.DataFrame({
        "": ["Stocks", "Creances", "Total des actifs courants",
             "Immob", "Total actifs non courants"],
        "2023": ["300", "200", "500", "900", "900"],
    })
    res = validate_row_sums(df)
    agg = [r for r in res if r.check_name == "Row Sums"]
    assert agg and agg[0].locate, "Row Sums roll-up must point at its subtotals"
    # the aggregate's targets are exactly the per-subtotal children's rows
    children = [r for r in res if r.check_name.startswith("Row Sum:")]
    assert len(agg[0].locate) == len([c for c in children if c.locate])


def test_variation_pct_aggregate_is_locatable():
    df = pd.DataFrame({
        "": ["Ventes", "Achats"],
        "2023": ["110", "210"],
        "2022": ["100", "200"],
        "Var%": ["10%", "5%"],
    })
    res = validate_variation_percentages(df)
    agg = [r for r in res if r.check_name == "Variation % Check" and not r.skipped]
    if agg:  # only if the identity actually ran on ≥1 row
        assert agg[0].locate


# ---------------------------------------------------------------------------
# completeness guard — no working, row-bound check is left non-locatable
# ---------------------------------------------------------------------------

# Checks that legitimately have NO single location (whole-table or meta).
_NO_LOCATE_OK = {
    "Tables Found", "Statement Segmentation", "Field Type Check",
}


def test_no_working_check_is_silently_non_locatable():
    rep = validate_with_rules([_actif(), _passif(), _cr()], standard="NCT")
    offenders = []
    for c in rep.checks:
        if getattr(c, "skipped", False):
            continue
        name = c.check_name.split("] ")[-1]  # drop the [statement] prefix
        if name in _NO_LOCATE_OK or name.startswith("V3: Magnitude"):
            continue
        if not getattr(c, "locate", None):
            offenders.append(c.check_name)
    assert not offenders, f"working checks with no Locate target: {offenders}"
