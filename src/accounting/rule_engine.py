"""
Configurable Validation Rule Engine

Loads validation rules from JSON, executes them against segmented financial
statements, and produces a unified ValidationReport compatible with QCS.

Supports:
- V1: Intra-statement arithmetic (balance equation, VNC, CR equation, subtotals)
- V2: Cross-statement coherence (Bilan <-> CR <-> TFT)
- V3: Anomaly detection (negatives, nulls, magnitude mismatches)
- V4: Ratio computation (informational, non-blocking)

Rules are defined in rules/validation_rules.json and can be toggled per-standard.
"""

import json
import logging
import math
import re
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple

import pandas as pd
import numpy as np

from accounting.validator import (
    ValidationResult,
    ValidationReport,
    _canonical_numeric_value,
    normalize_comma_thousands,
    select_value_column,
    find_numeric_columns,
    identify_row_type,
    validate_balance_sheet,
    validate_row_sums,
    validate_variation_percentages,
    validate_field_types,
)
from accounting.statement_segmenter import (
    StatementSegment,
    segment_tables,
    group_by_statement,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# Rule Loader
# ===========================================================================

RULES_DIR = Path(__file__).parent / "rules"


def load_rules(standard: str = "NCT") -> List[Dict]:
    """
    Load active validation rules for a given accounting standard.

    Filters rules by standard and active flag.
    """
    rules_path = RULES_DIR / "validation_rules.json"
    if not rules_path.exists():
        logger.warning(f"Rules file not found: {rules_path}")
        return []

    with open(rules_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    all_rules = data.get("rules", [])

    # Filter by standard and active flag
    filtered = [
        r for r in all_rules
        if r.get("active", True) and standard in r.get("standards", [])
    ]

    logger.info(f"Loaded {len(filtered)}/{len(all_rules)} rules for standard={standard}")
    return filtered


def get_rules_by_tier(rules: List[Dict], tier: str) -> List[Dict]:
    """Get rules for a specific tier (V1, V2, V3, V4)."""
    return [r for r in rules if r.get("tier") == tier]


# ===========================================================================
# NCT Account Reference Loader
# ===========================================================================

def load_nct_accounts() -> Dict:
    """Load NCT account structure reference."""
    nct_path = RULES_DIR / "nct_accounts.json"
    if not nct_path.exists():
        return {}
    with open(nct_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ===========================================================================
# Value Extraction Helpers
# ===========================================================================

def _strip_accents(text: str) -> str:
    """Strip common French accents for fuzzy keyword matching."""
    for src, dst in [
        ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
        ("à", "a"), ("â", "a"),
        ("ô", "o"), ("û", "u"), ("ù", "u"),
        ("î", "i"), ("ï", "i"), ("ç", "c"),
    ]:
        text = text.replace(src, dst)
    return text


def _find_value_by_keywords(
    df: pd.DataFrame,
    keywords: List[str],
    value_col: str,
    label_col: str,
) -> Optional[float]:
    """
    Find a numeric value in a DataFrame by matching row labels against keywords.

    Iterates keywords first (priority order), then rows — so more specific
    keywords listed earlier take precedence over generic ones that might
    match a subtotal higher up in the table.
    """
    # Pre-normalize all row labels once
    labels = []
    for idx, row in df.iterrows():
        raw = str(row[label_col]) if pd.notna(row[label_col]) else ""
        norm = _strip_accents(re.sub(r'\s+', ' ', raw.lower().strip()))
        labels.append((idx, norm))

    for kw in keywords:
        kw_clean = _strip_accents(kw)
        for idx, label_clean in labels:
            if kw_clean in label_clean:
                val = _canonical_numeric_value(df.at[idx, value_col])  # amount cell
                if val is not None:
                    return val
    return None


def _find_value_and_label_by_keywords(
    df: pd.DataFrame,
    keywords: List[str],
    value_col: str,
    label_col: str,
) -> Tuple[Optional[float], Optional[str]]:
    """Like ``_find_value_by_keywords`` but also returns the matched row's FULL
    (raw) label, so a check can tell the HITL UI which row to locate."""
    labels = []
    for idx, row in df.iterrows():
        raw = str(row[label_col]) if pd.notna(row[label_col]) else ""
        norm = _strip_accents(re.sub(r'\s+', ' ', raw.lower().strip()))
        labels.append((idx, norm, raw))

    for kw in keywords:
        kw_clean = _strip_accents(kw)
        for idx, label_clean, raw in labels:
            if kw_clean in label_clean:
                val = _canonical_numeric_value(df.at[idx, value_col])  # amount cell
                if val is not None:
                    return val, raw
    return None, None


def _loc(label, value, columns):
    """One Locate target: outline ``columns`` cell(s) of the row named ``label``.

    Returns None when there is no label to point at (so callers can build a
    locate list with ``_locate(...)`` and have empty targets dropped). The grid
    highlighter applies every target to every table's grid and silently skips a
    target whose label isn't present — so a cross-statement check can name a row
    on the bilan AND a row on the CR in one list and each lights up in its own
    table."""
    if not label:
        return None
    return {"label": label, "hint": value, "columns": list(columns) if columns else []}


def _locate(*entries):
    """Assemble a locate list from ``_loc(...)`` calls, dropping the Nones."""
    return [e for e in entries if e]


# Header tokens that mark a Note/Réf reference column (not a value column).
_NOTE_HEADER_RE = re.compile(r"notes?|réf|ref", re.IGNORECASE)

# Account-code shape: 3–4 digit code, optionally paren-wrapped, e.g. "(5111)".
_CODE_CELL_RE = re.compile(r"^\(?\d{3,4}\)?$")


def _is_note_like_column(df: pd.DataFrame, col) -> bool:
    """True if a *numeric-looking* column is actually a Note/Réf code column.

    ISSUE-7a: a Note/Réf column full of short paren-wrapped account codes
    ('(5111)') is admitted by ``find_numeric_columns`` (the parens→negative
    branch parses them as numbers) and can be picked as the value column.

    Gate the exclusion on BOTH a header-name match (/notes?|réf|ref/i) AND
    a high density of account-code-shaped cells — never on magnitude alone,
    so a legitimate small-value column is not dropped.
    """
    header_match = bool(_NOTE_HEADER_RE.search(str(col).strip()))
    if not header_match:
        return False

    total = 0
    code_like = 0
    for val in df[col]:
        if pd.isna(val):
            continue
        text = str(val).strip()
        if not text:
            continue
        total += 1
        if _CODE_CELL_RE.match(text):
            code_like += 1

    # Require a real density of code-shaped cells (≥60%) so a column merely
    # *named* something ref-ish but holding genuine amounts is still kept.
    return total > 0 and (code_like / total) >= 0.60


def _get_label_col_and_value_col(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
    """Get the label column (first) and best AMOUNT column.

    Value-column choice is delegated to ``validator.select_value_column``, which
    prefers a 'Net' column, then a period (year / N) column, and excludes Note/Réf
    code columns AND derived columns (variation %, écart) — so a leading '%' or a
    'Notes' code column is never mistaken for the value column.
    NB: the label column legitimately has an empty header (''), which callers must
    guard with ``is None`` (not truthiness).
    """
    if len(df.columns) < 2:
        return None, None
    return df.columns[0], select_value_column(df)


# ===========================================================================
# V1: Intra-Statement Arithmetic
# ===========================================================================

def validate_vnc_consistency(
    df: pd.DataFrame,
    tolerance: float = 0.01,
    tolerance_min: float = 1.0,
) -> List[ValidationResult]:
    """
    V1-002: Validate VNC (Valeur Nette Comptable) consistency.

    For NCT/SYSCOHADA bilan with Brut/Amort/Net presentation:
    Net = Brut - Amortissements (- Provisions)

    Looks for column headers containing "brut", "amort", "net".
    """
    results = []

    # Find brut/amort/net columns
    brut_col = None
    amort_col = None
    net_col = None

    for col in df.columns:
        col_lower = str(col).lower().strip()
        if "brut" in col_lower:
            brut_col = col
        elif "amort" in col_lower or "deprec" in col_lower:
            amort_col = col
        elif col_lower == "net" or col_lower.endswith(" net"):
            net_col = col

    if not all([brut_col, amort_col, net_col]):
        # No Brut/Amort/Net presentation found — skip
        return results

    label_col = df.columns[0]
    checked = 0
    errors = 0
    checked_loc = []  # (label, net, cols) of every VNC row verified — for the roll-up Locate

    for idx, row in df.iterrows():
        label = str(row[label_col]) if pd.notna(row[label_col]) else ""

        brut_val = _canonical_numeric_value(row[brut_col])    # amount cells
        amort_val = _canonical_numeric_value(row[amort_col])
        net_val = _canonical_numeric_value(row[net_col])

        # Skip rows where not all three values are present
        if brut_val is None or net_val is None:
            continue
        if amort_val is None:
            amort_val = 0.0

        checked += 1
        if label:
            checked_loc.append((label, net_val, [brut_col, amort_col, net_col]))
        expected_net = brut_val - abs(amort_val)  # Amort is often shown as positive
        difference = abs(net_val - expected_net)
        threshold = max(abs(brut_val) * tolerance, tolerance_min)
        passed = difference <= threshold

        if not passed:
            errors += 1
            if errors <= 3:  # Limit reported errors
                results.append(ValidationResult(
                    check_name=f"VNC: {label[:25]}",
                    passed=False,
                    severity="ERROR",
                    message=f"Net={net_val:,.0f}, expected Brut({brut_val:,.0f}) - Amort({amort_val:,.0f}) = {expected_net:,.0f}",
                    expected=expected_net,
                    actual=net_val,
                    difference=difference,
                    row_label=label,
                    # the three cells of the Brut − Amort = Net relation (one row)
                    locate=[{"label": label, "hint": net_val,
                             "columns": [brut_col, amort_col, net_col]}],
                ))

    # Family aggregate — counts and stats, not a binary verdict. Severity INFO
    # (weight 0): the per-row failures above carry the score.
    if checked > 0:
        stats = {"candidates": checked, "verified": checked,
                 "passed": checked - errors, "failed": errors,
                 "not_verifiable": 0,
                 "pass_rate": round((checked - errors) / checked, 3)}
        results.append(ValidationResult(
            check_name="VNC Consistency",
            passed=(errors == 0),
            severity="INFO",
            message=(f"All {checked} VNC rows verified (Net = Brut - Amort)"
                     if errors == 0 else
                     f"{checked - errors}/{checked} VNC rows verified · {errors} fail"),
            stats=stats,
            locate=_locate(*[_loc(l, v, c) for l, v, c in checked_loc]),
        ))

    return results


# E1: row-layout VNC triplet detection — label patterns, accent/case-insensitive.
# The Amort/VNC rows typically carry canonical_term=None (nomenclature doesn't map
# them), so detection matches RAW labels, not canonical terms.
_AMORT_ROW_PREFIXES = ("amortissement", "moinsprovision", "provision")
_NET_ROW_NORMS = ("vnc", "net", "valeurnette")


def _vnc_row_norm(label: str) -> str:
    """Accent-strip + lowercase + alphanumerics only, for triplet detection."""
    s = _strip_accents(str(label or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def _is_amort_row(label: str) -> bool:
    n = _vnc_row_norm(label)
    return bool(n) and any(n.startswith(p) for p in _AMORT_ROW_PREFIXES)


def _is_net_row(label: str) -> bool:
    n = _vnc_row_norm(label)
    return bool(n) and (n in _NET_ROW_NORMS or n.startswith("vnc") or n.startswith("valeurnette"))


def _vnc_period_columns(df: pd.DataFrame) -> list:
    """Value columns the row-layout VNC identity applies to: year/N-style period
    columns (never %/variation/note columns)."""
    period = []
    for col in df.columns:
        cs = str(col).strip()
        if re.match(r"^\d{4}$", cs) or re.match(r"^[Nn](-\d|_minus_\d)?$", cs):
            period.append(col)
    return period


def find_vnc_row_triplets(df: pd.DataFrame) -> List[Tuple[int, int, int]]:
    """E1: (brut_row, amort_row, net_row) index triplets in a ROW-layout bilan
    (asset line / Amortissements|Provisions line / VNC|Net line, columns = periods)."""
    if len(df.columns) < 2:
        return []
    label_col = df.columns[0]
    labels = [str(df.iloc[r][label_col]) if pd.notna(df.iloc[r][label_col]) else ""
              for r in range(len(df))]
    triplets: List[Tuple[int, int, int]] = []
    for i in range(1, len(labels) - 1):
        if not _is_amort_row(labels[i]) or not _is_net_row(labels[i + 1]):
            continue
        prev = labels[i - 1]
        if not prev.strip() or _is_amort_row(prev) or _is_net_row(prev):
            continue  # the row above must be the asset/Brut line
        triplets.append((i - 1, i, i + 1))
    return triplets


def validate_vnc_rows(
    df: pd.DataFrame,
    tolerance: float = 0.01,
    tolerance_min: float = 1.0,
) -> List[ValidationResult]:
    """V1-002 for the ROW layout (E1): Brut row / Amortissements row / VNC row,
    columns = periods. ``validate_vnc_consistency`` only handles the COLUMN
    layout and silently returns [] here — this closes that coverage gap.

    Checks ``VNC ≈ Brut − |Amort|`` per period column for every detected triplet.
    """
    results: List[ValidationResult] = []
    triplets = find_vnc_row_triplets(df)
    if not triplets:
        return results
    period_cols = _vnc_period_columns(df)
    if not period_cols:
        return results

    label_col = df.columns[0]
    checked = 0
    errors = 0
    checked_loc: List[Dict] = []  # one target per verified identity — for the roll-up Locate
    # Row-level coverage honesty: a triplet column with a missing/garbled value
    # cannot be verified — recorded and surfaced as SKIPPED, never silently
    # counted toward "all verified ✓" (an empty field never justifies a pass).
    unverifiable: List[Dict] = []
    for (b, a, n) in triplets:
        asset_label = str(df.iloc[b][label_col])
        for col in period_cols:
            brut_val = _canonical_numeric_value(df.iloc[b][col])
            amort_val = _canonical_numeric_value(df.iloc[a][col])
            net_val = _canonical_numeric_value(df.iloc[n][col])
            if brut_val is None or net_val is None or amort_val is None:
                raws = [df.iloc[r][col] for r in (b, a, n)]
                if all(pd.isna(v) or not str(v).strip() for v in raws):
                    continue  # the whole column is empty for this triplet
                name_to_row = {"Brut": b, "Amort": a, "Net": n}
                missing = [name for name, v in
                           (("Brut", brut_val), ("Amort", amort_val), ("Net", net_val))
                           if v is None]
                unverifiable.append({
                    "label": asset_label, "col": str(col), "missing": missing,
                    # one pinned target PER missing cell — ``row`` is the
                    # positional index, so duplicate labels (several "Moins
                    # provisions…"/"VNC" rows) cannot mis-aim the outline.
                    "targets": [{"label": str(df.iloc[name_to_row[m]][label_col]),
                                 "row": name_to_row[m], "hint": None,
                                 "columns": [str(col)]} for m in missing],
                })
                continue
            checked += 1
            expected_net = brut_val - abs(amort_val)
            difference = abs(net_val - expected_net)
            threshold = max(abs(brut_val) * tolerance, tolerance_min)
            if difference <= threshold:
                # verified — pin the Net row (positional, duplicate-label safe)
                checked_loc.append({"label": str(df.iloc[n][label_col]), "row": int(n),
                                    "hint": net_val, "columns": [str(col)]})
                continue
            errors += 1
            if errors <= 3:
                results.append(ValidationResult(
                    check_name=f"VNC: {asset_label[:25]}",
                    passed=False,
                    severity="ERROR",
                    message=(
                        f"[{col}] Net={net_val:,.0f}, expected "
                        f"Brut({brut_val:,.0f}) - Amort({amort_val:,.0f}) = {expected_net:,.0f}"
                    ),
                    expected=expected_net,
                    actual=net_val,
                    difference=difference,
                    row_label=asset_label,
                    # the three rows of the identity, one cell each in this column
                    locate=[
                        {"label": asset_label, "hint": brut_val, "columns": [col]},
                        {"label": str(df.iloc[a][label_col]), "hint": amort_val, "columns": [col]},
                        {"label": str(df.iloc[n][label_col]), "hint": net_val, "columns": [col]},
                    ],
                ))

    if unverifiable:
        examples = ", ".join(f"{u['label'][:20]} [{u['col']}: {'/'.join(u['missing'])}]"
                             for u in unverifiable[:3])
        results.append(ValidationResult(
            check_name="VNC coverage",
            passed=True,
            skipped=True,
            severity="ERROR",
            message=(
                f"{len(unverifiable)} VNC identity column(s) NOT verifiable — "
                f"missing/garbled value(s) (e.g. {examples}). Not counted as "
                f"verified — check them manually."
            ),
            locate=[t for u in unverifiable for t in u.get("targets", [])][:8],
            stats={"not_verifiable": len(unverifiable)},
        ))

    # Family aggregate — counts and stats, not a binary verdict (INFO: the
    # per-identity failures above carry the score).
    n_cand = checked + len(unverifiable)
    if n_cand > 0:
        stats = {"candidates": n_cand, "verified": checked,
                 "passed": checked - errors, "failed": errors,
                 "not_verifiable": len(unverifiable),
                 "pass_rate": (round((checked - errors) / checked, 3)
                               if checked else None)}
        cov = f" · {len(unverifiable)} not verifiable" if unverifiable else ""
        if errors == 0 and checked > 0:
            msg = (f"All {checked} verifiable row-layout VNC identities verified "
                   f"(Net = Brut - |Amort|){cov}")
        else:
            msg = (f"{checked - errors}/{checked} verifiable row-layout VNC "
                   f"identities pass · {errors} fail{cov}")
        results.append(ValidationResult(
            check_name="VNC Row-Layout",
            passed=(errors == 0),
            # zero identities verified → the family cannot claim a pass
            skipped=(checked == 0),
            severity="INFO",
            message=msg,
            stats=stats,
            locate=checked_loc[:12],  # cap: highlight the rows it covers, not a wall
        ))
    return results


def validate_cr_equation(
    df: pd.DataFrame,
    tolerance: float = 0.01,
    tolerance_min: float = 1.0,
) -> List[ValidationResult]:
    """
    V1-003: Validate CR (Compte de Resultat) equation.

    Resultat Net = Total Produits - Total Charges
    or Resultat Net = Resultat d'Exploitation + Resultat Financier - Impots

    Tries multiple strategies to find the components.
    """
    results = []
    label_col, value_col = _get_label_col_and_value_col(df)
    # NB: an OCR label column legitimately has an EMPTY header (''), which is
    # falsy — guard on `is None`, never truthiness, or every such doc skips.
    if label_col is None or value_col is None:
        return results

    resultat_net, lbl_net = _find_value_and_label_by_keywords(
        df,
        ["resultat net", "resultat de l'exercice", "resultat net de l'exercice",
         "benefice net", "perte nette"],
        value_col, label_col,
    )

    # Strategy 1: Resultat d'Exploitation + Resultat Financier - Impots
    # This is more reliable than Total Produits - Total Charges because
    # most CRs have exploitation subtotals, not overall totals.
    res_exploit, lbl_exploit = _find_value_and_label_by_keywords(
        df, ["resultat d'exploitation"], value_col, label_col
    )
    res_financier, lbl_fin = _find_value_and_label_by_keywords(
        df, ["resultat financier", "resultat des activites financieres"], value_col, label_col
    )
    impots, lbl_impots = _find_value_and_label_by_keywords(
        df, ["impots sur les benefices", "impot sur les societes", "charge fiscale"], value_col, label_col
    )

    if resultat_net is not None and res_exploit is not None:
        res_fin = res_financier if res_financier is not None else 0.0
        tax = impots if impots is not None else 0.0
        expected = res_exploit + res_fin - abs(tax)
        difference = abs(resultat_net - expected)
        threshold = max(abs(res_exploit) * tolerance, tolerance_min)
        passed = difference <= threshold

        # A component that was never FOUND is substituted with 0.0 above, so a
        # mismatch may be measuring an equation we never actually assembled.
        # This document reports "Charges financières nettes" / "Impôts sur le
        # résultat", which match none of the keywords, so both became 0 and the
        # check raised a CRITICAL against a total it had not established.
        #
        # Only skip when the equation FAILS *and* a component was missing — if
        # it passes with the substitution the components really were absent (a
        # CR with no financial result or tax), so the pass still stands and
        # coverage is preserved. NB: the formula is separately incomplete for
        # NCT (it omits Autres gains/pertes ordinaires and Éléments
        # extraordinaires); widening it is deferred, since "Charges financières
        # nettes" is a charge and "Résultat financier" a net result — mapping
        # one onto the other would flip the sign.
        incomplete = res_financier is None or impots is None
        if not passed and incomplete:
            missing = [n for n, v in (("Resultat financier", res_financier),
                                      ("Impots", impots)) if v is None]
            results.append(ValidationResult(
                check_name="CR Equation (Exploit+Fin-Impots)",
                passed=False,
                skipped=True,
                severity="CRITICAL",
                message=f"Skipped — could not locate: {', '.join(missing)}",
            ))
            return results

        # The rows feeding the equation, each highlighted in the value column.
        pairs = [(lbl_net, resultat_net), (lbl_exploit, res_exploit),
                 (lbl_fin, res_financier), (lbl_impots, impots)]
        locate = [{"label": l, "hint": v, "columns": [value_col]}
                  for l, v in pairs if l]

        results.append(ValidationResult(
            check_name="CR Equation (Exploit+Fin-Impots)",
            passed=passed,
            severity="CRITICAL",
            message=(
                f"Resultat Net={resultat_net:,.0f}, "
                f"Exploit({res_exploit:,.0f}) + Fin({res_fin:,.0f}) - Impots({tax:,.0f}) = {expected:,.0f}"
                + ("" if passed else f" (Diff: {difference:,.0f})")
            ),
            expected=expected,
            actual=resultat_net,
            difference=difference,
            locate=locate or None,
        ))

    return results


# ===========================================================================
# V2: Cross-Statement Coherence
# ===========================================================================

def validate_cross_statement(
    grouped: Dict[str, List[StatementSegment]],
    tolerance: float = 0.01,
    tolerance_min: float = 1.0,
) -> List[ValidationResult]:
    """
    V2: Cross-statement validation.

    Checks:
    - V2-001: Bilan Resultat de l'exercice == CR Resultat Net
    - V2-002: Bilan Liquidites == TFT Tresorerie de cloture
    """
    results = []

    bilan_tables = grouped.get("bilan", [])
    cr_tables = grouped.get("compte_resultat", [])
    tft_tables = grouped.get("flux_tresorerie", [])

    # V2-001: Resultat Net match (Bilan <-> CR)
    # Prefer bilan_passif sub-table (resultat is on the equity/passif side)
    bilan_passif = grouped.get("bilan_passif", [])
    if bilan_tables and cr_tables:
        # Use passif sub-table if available, otherwise full bilan
        bilan_for_resultat = bilan_passif[0].df if bilan_passif else bilan_tables[0].df
        cr_df = cr_tables[0].df

        b_label, b_val_col = _get_label_col_and_value_col(bilan_for_resultat)
        c_label, c_val_col = _get_label_col_and_value_col(cr_df)

        if (b_label is not None and b_val_col is not None
                and c_label is not None and c_val_col is not None):
            bilan_resultat, b_lbl = _find_value_and_label_by_keywords(
                bilan_for_resultat,
                ["resultat de l'exercice", "resultat net"],
                b_val_col, b_label,
            )
            cr_resultat, c_lbl = _find_value_and_label_by_keywords(
                cr_df,
                ["resultat net", "resultat de l'exercice", "benefice net", "perte nette"],
                c_val_col, c_label,
            )

            if bilan_resultat is not None and cr_resultat is not None:
                difference = abs(bilan_resultat - cr_resultat)
                threshold = max(abs(bilan_resultat) * tolerance, tolerance_min)
                passed = difference <= threshold

                # Locate both sides of the cross-check: the Résultat row on the
                # bilan AND on the CR (each lights up in its own grid).
                locate = _locate(_loc(b_lbl, bilan_resultat, [b_val_col]),
                                 _loc(c_lbl, cr_resultat, [c_val_col]))
                results.append(ValidationResult(
                    check_name="V2: Resultat Net Match (Bilan-CR)",
                    passed=passed,
                    severity="CRITICAL",
                    message=(
                        f"Bilan Resultat={bilan_resultat:,.0f}, CR Resultat={cr_resultat:,.0f}"
                        + ("" if passed else f" (Diff: {difference:,.0f})")
                    ),
                    expected=cr_resultat,
                    actual=bilan_resultat,
                    difference=difference,
                    row_label=b_lbl,
                    locate=locate,
                ))
            else:
                # Being unable to LOCATE the Résultat row is not an arithmetic
                # error — but reporting it as a CRITICAL made it one, and a
                # CRITICAL sets ``arithmetic_fail`` in the Tier-3 ensemble,
                # which overrides Q_table unconditionally and forces HITL on a
                # correctly extracted document. Tier 0's native frames carry
                # generic headers ('Col1'/'Col2'/'Col3') so no value column can
                # be picked, while Tier 1 finds both sides and they match.
                # Skip: neither pass nor fail, excluded from critical_failures.
                results.append(ValidationResult(
                    check_name="V2: Resultat Net Match (Bilan-CR)",
                    passed=False,
                    skipped=True,
                    severity="CRITICAL",
                    message=(
                        f"Skipped — could not locate Resultat in "
                        f"{'Bilan' if bilan_resultat is None else 'CR'}"
                    ),
                ))

    # V2-002: Tresorerie match (Bilan <-> TFT)
    # Prefer bilan_actif sub-table (liquidités are on the actif side)
    bilan_actif = grouped.get("bilan_actif", [])
    if bilan_tables and tft_tables:
        bilan_for_treso = bilan_actif[0].df if bilan_actif else bilan_tables[0].df
        tft_df = tft_tables[0].df

        b_label, b_val_col = _get_label_col_and_value_col(bilan_for_treso)
        t_label, t_val_col = _get_label_col_and_value_col(tft_df)

        if (b_label is not None and b_val_col is not None
                and t_label is not None and t_val_col is not None):
            bilan_treso, b_lbl = _find_value_and_label_by_keywords(
                bilan_for_treso,
                ["liquidites et equivalents", "tresorerie", "disponibilites",
                 "liquidites"],
                b_val_col, b_label,
            )
            tft_cloture, t_lbl = _find_value_and_label_by_keywords(
                tft_df,
                ["tresorerie de cloture", "tresorerie fin", "solde de cloture"],
                t_val_col, t_label,
            )

            if bilan_treso is not None and tft_cloture is not None:
                difference = abs(bilan_treso - tft_cloture)
                threshold = max(abs(bilan_treso) * tolerance, tolerance_min)
                passed = difference <= threshold

                locate = _locate(_loc(b_lbl, bilan_treso, [b_val_col]),
                                 _loc(t_lbl, tft_cloture, [t_val_col]))
                results.append(ValidationResult(
                    check_name="V2: Tresorerie Match (Bilan-TFT)",
                    passed=passed,
                    severity="CRITICAL",
                    message=(
                        f"Bilan Liquidites={bilan_treso:,.0f}, TFT Cloture={tft_cloture:,.0f}"
                        + ("" if passed else f" (Diff: {difference:,.0f})")
                    ),
                    expected=tft_cloture,
                    actual=bilan_treso,
                    difference=difference,
                    row_label=b_lbl,
                    locate=locate,
                ))

    return results


def _find_grand_total(df, must_contain, value_col, label_col, also=None):
    """Largest-value row whose accent-stripped label contains ALL tokens in
    ``must_contain`` (or ALL in ``also``) — the grand total, never a subtotal."""
    best_v, best_l = None, None
    groups = [must_contain] + ([also] if also else [])
    for idx, row in df.iterrows():
        raw = str(row[label_col]) if pd.notna(row[label_col]) else ""
        norm = _strip_accents(re.sub(r"\s+", " ", raw.lower().strip()))
        if not any(all(_strip_accents(tok) in norm for tok in toks) for toks in groups):
            continue
        v = _canonical_numeric_value(row[value_col])
        if v is None:
            continue
        if best_v is None or abs(v) > abs(best_v):
            best_v, best_l = v, raw
    return best_v, best_l


def validate_balance_cross_segment(
    grouped: Dict[str, List[StatementSegment]],
    tolerance: float = 0.01,
    tolerance_min: float = 1.0,
) -> List[ValidationResult]:
    """V1-001 for a bilan split across TWO tables (Actif page + Passif page):
    the balance equation spans both, so the per-segment check (one df) can never
    see both sides. Finds Total Actif on the actif segment and Total Capitaux
    Propres et Passifs on the passif segment and checks they balance.

    Only fires when BOTH sub-segments exist (else the single-table per-segment
    check handles it). Skips with a clear reason when a total can't be located.
    """
    actif = grouped.get("bilan_actif", [])
    passif = grouped.get("bilan_passif", [])
    if not actif or not passif:
        return []
    adf, pdf = actif[0].df, passif[0].df
    a_label, a_val = _get_label_col_and_value_col(adf)
    p_label, p_val = _get_label_col_and_value_col(pdf)
    if a_label is None or a_val is None or p_label is None or p_val is None:
        return []

    # The GRAND total is the largest total on each side (≥ any subtotal), so a
    # substring keyword like 'total des actifs' must not grab the first match
    # ('Total des actifs NON COURANTS') — take the maximum-value total instead.
    total_actif, lbl_a = _find_grand_total(adf, ["total", "actif"], a_val, a_label)
    total_passif, lbl_p = _find_grand_total(
        pdf, ["total", "passif"], p_val, p_label,
        also=["total", "capitaux", "propres"])  # "Total CP et passifs" is the grand total

    if total_actif is None or total_passif is None:
        miss = "Total Actif" if total_actif is None else "Total Capitaux Propres et Passifs"
        return [ValidationResult(
            check_name="Balance Sheet Balance", passed=True, skipped=True,
            severity="CRITICAL",
            message=f"Skipped — could not locate {miss} across the actif/passif tables.")]

    difference = abs(total_actif - total_passif)
    passed = difference <= max(abs(total_actif) * tolerance, tolerance_min)
    return [ValidationResult(
        check_name="Balance Sheet Balance", passed=passed, severity="CRITICAL",
        message=(f"Actif = {total_actif:,.0f}, Capitaux Propres + Passifs = "
                 f"{total_passif:,.0f}" + (" ✓" if passed else f" (Diff: {difference:,.0f})")),
        expected=total_passif, actual=total_actif, difference=difference,
        locate=[{"label": lbl_a, "hint": total_actif, "columns": [a_val]},
                {"label": lbl_p, "hint": total_passif, "columns": [p_val]}])]


def _period_value_columns(df: pd.DataFrame) -> List:
    """The two period value columns of a bilan, returned **N, N-1** (most recent
    first) regardless of their order in the table — a doc may print 2022 before
    2023, so column order must NOT be assumed to be chronological."""
    period = []
    for col in df.columns:
        cs = str(col).strip()
        if re.match(r"^\d{4}$", cs) or re.match(r"^[Nn](-\d|_minus_\d)?$", cs):
            period.append(col)
    # 4-digit years: most recent first. N/N-1 tokens: 'N' is current.
    def _key(c):
        s = str(c).strip()
        if s.isdigit():
            return (0, -int(s))            # years: larger = more recent
        return (1, 0 if s.upper() == "N" else 1)  # 'N' before 'N-1'
    return sorted(period, key=_key)[:2]


def validate_report_a_nouveau(
    grouped: Dict[str, List[StatementSegment]],
    tolerance: float = 0.01,
    tolerance_min: float = 1.0,
) -> List[ValidationResult]:
    """V2-003: Report-à-Nouveau continuity.

    ``RAN_N = RAN_{N-1} + Résultat_{N-1} − Dividendes``. Needs a bilan with two
    period columns, a Report-à-Nouveau / Résultats reportés row, and the prior
    Résultat. Dividendes are rarely on the face of the statement, so:
      * if a Dividendes/Distributions row is found → exact identity check;
      * else verify the weaker no-distribution invariant
        ``RAN_N ≈ RAN_{N-1} + Résultat_{N-1}`` and, failing that, that the change
        is at least *consistent with a distribution* (0 ≤ ΔRAN ≤ Résultat_{N-1}).
    Skips (clear reason) when the inputs aren't present.
    """
    results: List[ValidationResult] = []
    bilan = grouped.get("bilan_passif", []) or grouped.get("bilan", [])
    if not bilan:
        return results
    df = bilan[0].df
    label_col, _ = _get_label_col_and_value_col(df)
    periods = _period_value_columns(df)
    if label_col is None or len(periods) < 2:
        results.append(ValidationResult(
            check_name="V2: Report a Nouveau Continuity", passed=True, skipped=True,
            severity="WARNING",
            message="Skipped — needs a bilan with two period (N, N-1) columns.",
        ))
        return results
    col_n, col_n1 = periods[0], periods[1]

    def _row(keywords):
        for kw in keywords:
            kwc = _strip_accents(kw)
            for idx, row in df.iterrows():
                raw = str(row[label_col]) if pd.notna(row[label_col]) else ""
                if kwc in _strip_accents(re.sub(r"\s+", " ", raw.lower().strip())):
                    return (_canonical_numeric_value(row[col_n]),
                            _canonical_numeric_value(row[col_n1]), raw)
        return (None, None, None)

    ran_n, ran_n1, ran_lbl = _row(["report a nouveau", "resultats reportes",
                                   "report à nouveau", "résultats reportés"])
    _, res_n1, res_lbl = _row(["resultat de l'exercice", "resultat net",
                               "résultat de l'exercice"])
    # Only an explicit dividend/distribution row — NOT 'affectation' (too loose,
    # it can hit the résultat-affectation line and corrupt the identity).
    div_n, _, div_lbl = _row(["dividendes", "dividende", "distributions"])

    if ran_n is None or ran_n1 is None or res_n1 is None:
        results.append(ValidationResult(
            check_name="V2: Report a Nouveau Continuity", passed=True, skipped=True,
            severity="WARNING",
            message=("Skipped — could not locate Report-à-Nouveau (N and N-1) "
                     "and prior Résultat on this document."),
        ))
        return results

    locate = [{"label": ran_lbl, "hint": ran_n, "columns": [col_n, col_n1]},
              {"label": res_lbl, "hint": res_n1, "columns": [col_n1]}]
    if div_n is not None:
        expected = ran_n1 + res_n1 - abs(div_n)
        difference = abs(ran_n - expected)
        passed = difference <= max(abs(ran_n1) * tolerance, tolerance_min)
        results.append(ValidationResult(
            check_name="V2: Report a Nouveau Continuity", passed=passed,
            severity="WARNING",
            message=(f"RAN N={ran_n:,.0f} vs RAN N-1({ran_n1:,.0f}) + Résultat "
                     f"N-1({res_n1:,.0f}) − Div({abs(div_n):,.0f}) = {expected:,.0f}"
                     + ("" if passed else f" (Diff: {difference:,.0f})")),
            expected=expected, actual=ran_n, difference=difference,
            row_label=ran_lbl, locate=locate))
        return results

    # No dividends row: check the no-distribution identity, then the bounded form.
    delta = ran_n - ran_n1
    exact_diff = abs(delta - res_n1)
    if exact_diff <= max(abs(res_n1) * tolerance, tolerance_min):
        results.append(ValidationResult(
            check_name="V2: Report a Nouveau Continuity", passed=True,
            severity="WARNING",
            message=(f"RAN N−1 ({ran_n1:,.0f}) + Résultat N−1 ({res_n1:,.0f}) "
                     f"= RAN N ({ran_n:,.0f}) ✓ (no distribution)"),
            expected=ran_n1 + res_n1, actual=ran_n, difference=exact_diff,
            row_label=ran_lbl, locate=locate))
    elif (res_n1 >= 0 and -tolerance_min <= delta <= res_n1 + tolerance_min) or \
         (res_n1 < 0 and res_n1 - tolerance_min <= delta <= tolerance_min):
        results.append(ValidationResult(
            check_name="V2: Report a Nouveau Continuity", passed=True,
            severity="WARNING",
            message=(f"ΔRAN ({delta:,.0f}) is consistent with Résultat N−1 "
                     f"({res_n1:,.0f}) net of a distribution ✓"),
            actual=ran_n, row_label=ran_lbl, locate=locate))
    else:
        results.append(ValidationResult(
            check_name="V2: Report a Nouveau Continuity", passed=False,
            severity="WARNING",
            message=(f"RAN moved by {delta:,.0f}, which Résultat N−1 "
                     f"({res_n1:,.0f}) cannot explain (even net of dividends)."),
            expected=ran_n1 + res_n1, actual=ran_n, difference=exact_diff,
            row_label=ran_lbl, locate=locate))
    return results


# ===========================================================================
# V3: Anomaly Detection
# ===========================================================================

def detect_anomalies(
    segments: List[StatementSegment],
) -> List[ValidationResult]:
    """
    V3: Anomaly detection for financial statements.

    Checks:
    - V3-001: Negative values where unexpected (capital social, immo brutes, stocks)
    - V3-002: Null operational values (CA = 0)
    - V3-003: Magnitude mismatch (assets in millions, liabilities in thousands)
    """
    results = []

    for seg in segments:
        df = seg.df
        label_col, value_col = _get_label_col_and_value_col(df)
        if label_col is None or value_col is None:  # '' label header is valid
            continue

        # V3-001: Negative where unexpected. Emit a verified-PASS when the check
        # ran (≥1 field found) and nothing was negative — so the rule reports
        # "ran · pass" instead of silently producing no result.
        if seg.statement_type == "bilan":
            negative_fields = {
                "capital social": "Capital social",
                "immobilisations": "Immobilisations brutes",
                "stocks": "Stocks",
            }
            checked = 0
            negatives = 0
            checked_loc = []  # (label, value) of each field actually verified
            for kw, field_name in negative_fields.items():
                val, raw_lbl = _find_value_and_label_by_keywords(df, [kw], value_col, label_col)
                if val is None:
                    continue
                checked += 1
                located = raw_lbl or field_name
                checked_loc.append((located, val))
                if val < 0:
                    negatives += 1
                    results.append(ValidationResult(
                        check_name=f"V3: Negative {field_name}",
                        passed=False,
                        severity="WARNING",
                        message=f"{field_name} = {val:,.0f} (negative — accounting impossibility)",
                        actual=val,
                        row_label=located,
                        locate=[{"label": located, "hint": val, "columns": [value_col]}],
                    ))
            if checked > 0 and negatives == 0:
                results.append(ValidationResult(
                    check_name="V3: Negative Where Unexpected",
                    passed=True,
                    severity="WARNING",
                    message=f"All {checked} sign-sensitive field(s) non-negative ✓",
                    # Aggregate verified-PASS: Locate highlights every field it
                    # cleared, so the reviewer can eyeball the same rows it did.
                    locate=_locate(*[_loc(l, v, [value_col]) for l, v in checked_loc]),
                ))

        # V3-002: Null operational values — emit pass when CA found and non-zero.
        if seg.statement_type == "compte_resultat":
            ca, ca_lbl = _find_value_and_label_by_keywords(
                df,
                ["chiffre d'affaires", "revenus", "ventes"],
                value_col, label_col,
            )
            if ca is not None:
                ca_locate = _locate(_loc(ca_lbl, ca, [value_col]))
                if ca == 0:
                    results.append(ValidationResult(
                        check_name="V3: Null CA",
                        passed=False,
                        severity="WARNING",
                        message="Chiffre d'affaires = 0 (likely extraction failure for operating company)",
                        actual=0.0,
                        row_label=ca_lbl,
                        locate=ca_locate,
                    ))
                else:
                    results.append(ValidationResult(
                        check_name="V3: Null CA",
                        passed=True,
                        severity="WARNING",
                        message=f"Chiffre d'affaires = {ca:,.0f} (non-zero ✓)",
                        actual=ca,
                        row_label=ca_lbl,
                        locate=ca_locate,
                    ))

        # V3-003: Magnitude mismatch within statement — emit pass when ≥3 values
        # span a plausible range.
        magnitudes = []
        for idx, row in df.iterrows():
            val = _canonical_numeric_value(row[value_col])  # amount cell
            if val is not None and val != 0:
                magnitudes.append(math.floor(math.log10(abs(val))) if abs(val) >= 1 else 0)

        if len(magnitudes) >= 3:
            mag_range = max(magnitudes) - min(magnitudes)
            if mag_range > 6:  # More than 6 orders of magnitude
                results.append(ValidationResult(
                    check_name=f"V3: Magnitude Mismatch (Table {seg.table_index})",
                    passed=False,
                    severity="WARNING",
                    message=(
                        f"Values span {mag_range} orders of magnitude "
                        f"(10^{min(magnitudes)} to 10^{max(magnitudes)}) — possible unit/scale OCR error"
                    ),
                ))
            else:
                results.append(ValidationResult(
                    check_name=f"V3: Magnitude Mismatch (Table {seg.table_index})",
                    passed=True,
                    severity="WARNING",
                    message=f"Values span {mag_range} orders of magnitude (consistent ✓)",
                ))

    return results


# V3-004 heuristic upper bounds, expressed as multiples of annual turnover (CA).
# A receivables balance worth more than two years of sales, or inventory worth
# more than a full year of sales, is almost always an OCR scale/units slip (or a
# genuinely distressed balance sheet) — flagged WARNING for a human glance, never
# a hard fail.
_CREANCES_CA_MAX = 2.0
_STOCKS_CA_MAX = 1.0


def validate_suspicious_proportions(
    grouped: Dict[str, List[StatementSegment]],
) -> List[ValidationResult]:
    """
    V3-004: Suspicious balance-sheet proportions relative to turnover.

    Cross-statement heuristic (WARNING — a sanity bound, not an accounting law):
      • Créances clients ≤ 2 × Chiffre d'affaires
      • Stocks           ≤ 1 × Chiffre d'affaires

    Needs the bilan (actif side, for Créances/Stocks) AND the Chiffre d'affaires
    off the compte de résultat. Conservative: SKIPS (never fails) when an input
    is missing or the CA is non-positive, so it can only ever fire on a real
    outlier — typically a units/scale OCR error on one line.
    """
    bilan_tables = grouped.get("bilan", [])
    cr_tables = grouped.get("compte_resultat", [])
    if not bilan_tables or not cr_tables:
        return [ValidationResult(
            check_name="V3: Suspicious Proportions",
            passed=True,
            skipped=True,  # D7: skipped ≠ pass
            severity="WARNING",
            message="Skipped — needs Bilan + Compte de Résultat (for Chiffre d'affaires)",
        )]

    bilan_actif = grouped.get("bilan_actif", [])
    actif_df = bilan_actif[0].df if bilan_actif else bilan_tables[0].df
    cr_df = cr_tables[0].df

    a_label, a_val = _get_label_col_and_value_col(actif_df)
    c_label, c_val = _get_label_col_and_value_col(cr_df)
    # '' label headers are valid (OCR) — guard on `is None`, not truthiness.
    if a_label is None or a_val is None or c_label is None or c_val is None:
        return [ValidationResult(
            check_name="V3: Suspicious Proportions",
            passed=True,
            skipped=True,
            severity="WARNING",
            message="Skipped — could not detect label/value columns",
        )]

    ca = _find_value_by_keywords(
        cr_df,
        ["chiffre d'affaires", "chiffres d'affaires", "produits d'exploitation",
         "revenus", "ventes"],
        c_val, c_label,
    )
    if ca is None or ca <= 0:
        return [ValidationResult(
            check_name="V3: Suspicious Proportions",
            passed=True,
            skipped=True,
            severity="WARNING",
            message=(
                "Skipped — Chiffre d'affaires not found or non-positive"
                + (f" (CA={ca:,.0f})" if ca is not None else "")
            ),
        )]

    # (short label, upper bound, lookup keywords — longest/most-specific first)
    metrics = [
        ("Créances/CA", _CREANCES_CA_MAX,
         ["creances clients et comptes rattaches", "creances clients",
          "clients et comptes rattaches"]),
        ("Stocks/CA", _STOCKS_CA_MAX,
         ["stocks et en-cours", "stocks et encours", "stocks"]),
    ]

    results: List[ValidationResult] = []
    for short, bound, keywords in metrics:
        val, raw_lbl = _find_value_and_label_by_keywords(actif_df, keywords, a_val, a_label)
        if val is None:
            continue  # metric absent on this bilan — nothing to assert
        ratio = val / ca
        located = raw_lbl or short
        if ratio <= bound:
            results.append(ValidationResult(
                check_name=f"V3: Suspicious Proportion ({short})",
                passed=True,
                severity="WARNING",
                message=f"{short} = {ratio:.2f}× turnover (≤ {bound:g}× ✓)",
                actual=val,
                row_label=located,
                locate=[{"label": located, "hint": val, "columns": [a_val]}],
            ))
        else:
            results.append(ValidationResult(
                check_name=f"V3: Suspicious Proportion ({short})",
                passed=False,
                severity="WARNING",
                message=(
                    f"{short} = {ratio:.2f}× turnover "
                    f"({val:,.0f} vs CA {ca:,.0f}) — exceeds {bound:g}× "
                    f"(likely a units/scale OCR error)"
                ),
                actual=val,
                expected=ca * bound,
                row_label=located,
                locate=[{"label": located, "hint": val, "columns": [a_val]}],
            ))

    if not results:
        return [ValidationResult(
            check_name="V3: Suspicious Proportions",
            passed=True,
            skipped=True,
            severity="WARNING",
            message="Skipped — no Créances/Stocks rows found on the bilan",
        )]
    return results


# ===========================================================================
# V4: Ratio Computation (informational)
# ===========================================================================

def compute_ratios(
    grouped: Dict[str, List[StatementSegment]],
) -> List[ValidationResult]:
    """
    V4: Compute financial ratios (informational, non-blocking).

    - Fonds de Roulement (FR)
    - Tresorerie Nette cross-check
    """
    results = []

    bilan_tables = grouped.get("bilan", [])
    bilan_actif = grouped.get("bilan_actif", [])
    bilan_passif = grouped.get("bilan_passif", [])
    if not bilan_tables:
        results.append(ValidationResult(
            check_name="V4: Fonds de Roulement (FR)",
            passed=True,
            skipped=True,  # D7: skipped ≠ pass
            severity="INFO",  # D8: V4 ratios are informational
            message="Skipped — no bilan table found",
        ))
        return results

    # Use sub-tables if available for targeted lookups
    actif_df = bilan_actif[0].df if bilan_actif else bilan_tables[0].df
    passif_df = bilan_passif[0].df if bilan_passif else bilan_tables[0].df

    a_label, a_val = _get_label_col_and_value_col(actif_df)
    p_label, p_val = _get_label_col_and_value_col(passif_df)
    # '' label headers are valid (OCR) — guard on `is None`, not truthiness.
    if a_label is None or a_val is None or p_label is None or p_val is None:
        results.append(ValidationResult(
            check_name="V4: Fonds de Roulement (FR)",
            passed=True,
            skipped=True,  # D7: skipped ≠ pass
            severity="INFO",  # D8: V4 ratios are informational
            message=(
                f"Skipped — could not detect columns "
                f"(actif: label={a_label}, val={a_val}; passif: label={p_label}, val={p_val})"
            ),
        ))
        return results

    # Try to find key values (actif side for NC assets, passif side for CP/passif NC)
    # Keywords ordered longest-first so more specific phrases match before generic ones
    total_actif_nc, anc_lbl = _find_value_and_label_by_keywords(
        actif_df,
        [
            "total des actifs non courants",
            "total actifs non courants",
            "total actifs immobilises",
            "total des actifs immobilises",
        ],
        a_val, a_label,
    )

    # For total CP, prefer the most complete total (avant affectation > avant résultat)
    total_cp, cp_lbl = _find_value_and_label_by_keywords(
        passif_df,
        [
            "total des capitaux propres avant affectation",
            "total capitaux propres avant affectation",
            "total des capitaux propres et passifs",
            "total des capitaux propres",
            "total capitaux propres",
        ],
        p_val, p_label,
    )

    total_passif_nc, pnc_lbl = _find_value_and_label_by_keywords(
        passif_df,
        [
            "total des passifs non courants",
            "total passifs non courants",
        ],
        p_val, p_label,
    )

    # Diagnostic: always produce a check so the user sees what happened
    missing = []
    if total_actif_nc is None:
        missing.append("Total Actifs NC")
    if total_cp is None:
        missing.append("Total Capitaux Propres")
    if total_passif_nc is None:
        missing.append("Total Passifs NC")

    if missing:
        results.append(ValidationResult(
            check_name="V4: Fonds de Roulement (FR)",
            passed=True,
            skipped=True,  # D7: skipped ≠ pass
            severity="INFO",  # D8: V4 ratios are informational
            message=(
                f"Skipped — could not locate: {', '.join(missing)}. "
                f"Found: Actif NC={total_actif_nc}, CP={total_cp}, Passif NC={total_passif_nc}. "
                f"Columns used: actif val='{a_val}', passif val='{p_val}'"
            ),
        ))
        return results

    # V4-001: Fonds de Roulement
    capitaux_permanents = total_cp + total_passif_nc
    fr = capitaux_permanents - total_actif_nc

    results.append(ValidationResult(
        check_name="V4: Fonds de Roulement (FR)",
        passed=fr >= 0,  # Expected positive
        severity="INFO",  # D8: V4 ratios are informational, never score-moving
        message=(
            f"FR = Capitaux permanents({capitaux_permanents:,.0f}) "
            f"- Actif NC({total_actif_nc:,.0f}) = {fr:,.0f}"
            + (" (positive)" if fr >= 0 else " (NEGATIVE — structural risk)")
        ),
        actual=fr,
        row_label=cp_lbl,
        locate=_locate(_loc(anc_lbl, total_actif_nc, [a_val]),
                       _loc(cp_lbl, total_cp, [p_val]),
                       _loc(pnc_lbl, total_passif_nc, [p_val])),
    ))

    # V4-002: Trésorerie Nette = Trésorerie Actif − Trésorerie Passif
    # (liquidités/équivalents − concours bancaires/découverts). Informational;
    # skips clearly when either side isn't on the statement.
    treso_actif, ta_lbl = _find_value_and_label_by_keywords(
        actif_df,
        ["liquidites et equivalents", "tresorerie et equivalents", "disponibilites",
         "tresorerie actif", "liquidites"],
        a_val, a_label,
    )
    treso_passif, tp_lbl = _find_value_and_label_by_keywords(
        passif_df,
        ["concours bancaires", "decouverts bancaires", "tresorerie passif",
         "banques creditrices"],
        p_val, p_label,
    )
    if treso_actif is None and treso_passif is None:
        results.append(ValidationResult(
            check_name="V4: Tresorerie Nette Cross-Check",
            passed=True, skipped=True, severity="INFO",
            message="Skipped — no Trésorerie Actif/Passif rows found on the bilan.",
        ))
    else:
        ta = treso_actif or 0.0
        tp = treso_passif or 0.0
        tn = ta - tp
        results.append(ValidationResult(
            check_name="V4: Tresorerie Nette Cross-Check",
            passed=True, severity="INFO",
            message=(f"Trésorerie Nette = Trésorerie Actif({ta:,.0f}) "
                     f"− Trésorerie Passif({tp:,.0f}) = {tn:,.0f}"
                     + (" (positive)" if tn >= 0 else " (NEGATIVE — liquidity pressure)")),
            actual=tn,
            row_label=ta_lbl or tp_lbl,
            locate=_locate(_loc(ta_lbl, treso_actif, [a_val]),
                           _loc(tp_lbl, treso_passif, [p_val])),
        ))

    return results


# ===========================================================================
# Main Entry Point: Full Validation Pipeline
# ===========================================================================

def validate_with_rules(
    tables: List[pd.DataFrame],
    standard: str = "NCT",
    run_v2: bool = True,
    run_v3: bool = True,
    run_v4: bool = True,
) -> ValidationReport:
    """
    Run the full configurable validation pipeline on extracted tables.

    Flow:
    1. Segment tables by statement type
    2. Load rules for the given standard
    3. Run V1 intra-statement checks (existing + new VNC, CR equation)
    4. Run V2 cross-statement checks (if multiple statement types found)
    5. Run V3 anomaly detection
    6. Run V4 ratio computation (informational)
    7. Compile unified ValidationReport

    Args:
        tables: List of DataFrames from extraction
        standard: Accounting standard (IFRS, NCT, SYSCOHADA)
        run_v2: Enable cross-statement validation
        run_v3: Enable anomaly detection
        run_v4: Enable ratio computation

    Returns:
        ValidationReport with all check results and severity-weighted score
    """
    report = ValidationReport()

    # "Any table with CONTENT", not "a non-empty list". A failed OCR pass on a
    # scanned document yields table REGIONS with nothing in them — one real
    # sample produces 16 frames of shape (0, 0) — and a list of 16 empty frames
    # is truthy, so this check reported "Found 16 table(s)" and passed its
    # CRITICAL. A document that extracted nothing then looked like a partial
    # success rather than the total failure it is.
    usable = [t for t in tables if t is not None and not t.empty]
    if not usable:
        report.add_check(ValidationResult(
            check_name="Tables Found",
            passed=False,
            severity="CRITICAL",
            message=(
                "No tables extracted from document" if not tables
                else f"{len(tables)} table region(s) detected but all are empty "
                     "— extraction produced no content"
            ),
        ))
        report.compute_severity_weighted_score()
        return report

    # Resolve the comma convention per table BEFORE any check reads a number.
    # A lone token cannot settle it (``210,580`` is 210580 grouped, 210.58
    # decimal) and ``extract_numeric_value`` assumes decimal, so an
    # English-grouped statement silently loses every amount. Two or more commas
    # in one token proves grouping for the whole table — see
    # ``normalize_comma_thousands``. A no-op on genuinely comma-decimal tables.
    tables = [normalize_comma_thousands(t) for t in tables]

    # Basic check
    report.add_check(ValidationResult(
        check_name="Tables Found",
        passed=True,
        severity="CRITICAL",
        message=f"Found {len(tables)} table(s)",
    ))

    # Load rules (for logging / future dynamic rule execution)
    rules = load_rules(standard)
    v1_rules = get_rules_by_tier(rules, "V1")
    v2_rules = get_rules_by_tier(rules, "V2")
    logger.info(f"Active rules: V1={len(v1_rules)}, V2={len(v2_rules)}")

    # -----------------------------------------------------------------------
    # Step 1: Segment tables
    # -----------------------------------------------------------------------
    segments = segment_tables(tables)
    grouped = group_by_statement(segments)

    segment_summary = {k: len(v) for k, v in grouped.items() if v}
    report.add_check(ValidationResult(
        check_name="Statement Segmentation",
        passed=bool(segment_summary),
        severity="INFO",  # D8: structural/meta note, not an accounting rule
        message=f"Segments: {segment_summary}",
    ))

    # A bilan split into separate Actif + Passif tables has the balance equation
    # spanning both — handled once, cross-segment, after the loop. Suppress the
    # per-segment V1-001 then (it can only ever see one side → a misleading skip).
    bilan_is_split = bool(grouped.get("bilan_actif")) and bool(grouped.get("bilan_passif"))

    # -----------------------------------------------------------------------
    # Step 2: V1 — Intra-statement validation
    # -----------------------------------------------------------------------
    for seg in segments:
        df = seg.df
        prefix = f"[{seg.statement_type}] " if seg.statement_type != "unknown" else ""

        # V1-001: Balance sheet equation (existing). Surface a genuine evaluation
        # (expected set) OR an explicit skip (actif-only page) — but not the
        # "couldn't identify totals" non-result. Skipped entirely when the bilan
        # is split (the cross-segment check below owns it).
        if seg.statement_type in ("bilan", "unknown") and not bilan_is_split:
            balance_result = validate_balance_sheet(df)
            if balance_result.expected is not None or balance_result.skipped:
                balance_result.check_name = f"{prefix}{balance_result.check_name}"
                report.add_check(balance_result)

        # V1-002: VNC consistency — column layout + row layout (E1)
        if seg.statement_type in ("bilan", "unknown"):
            vnc_results = validate_vnc_consistency(df)
            vnc_results += validate_vnc_rows(df)
            for r in vnc_results:
                r.check_name = f"{prefix}{r.check_name}"
                report.add_check(r)

        # V1-003: CR equation (new)
        if seg.statement_type in ("compte_resultat", "unknown"):
            cr_results = validate_cr_equation(df)
            for r in cr_results:
                r.check_name = f"{prefix}{r.check_name}"
                report.add_check(r)

        # V1-004: Subtotal coherence (existing)
        row_results = validate_row_sums(df)
        for r in row_results[:3]:
            r.check_name = f"{prefix}{r.check_name}"
            report.add_check(r)

        # V1-005: Variation % (existing)
        var_results = validate_variation_percentages(df)
        for r in var_results[:3]:
            r.check_name = f"{prefix}{r.check_name}"
            report.add_check(r)

        # V1-006: Field type integrity (existing)
        type_results = validate_field_types(df)
        for r in type_results:
            r.check_name = f"{prefix}{r.check_name}"
            report.add_check(r)

    # -----------------------------------------------------------------------
    # Step 3: V2 — Cross-statement coherence
    # -----------------------------------------------------------------------
    # V1-001 cross-segment balance (split Actif/Passif bilan) — runs regardless
    # of run_v2 (it is a V1 check), once, before the V2 cross-statement checks.
    for r in validate_balance_cross_segment(grouped):
        report.add_check(r)

    if run_v2:
        cross_results = validate_cross_statement(grouped)
        cross_results += validate_report_a_nouveau(grouped)  # V2-003
        for r in cross_results:
            report.add_check(r)

    # -----------------------------------------------------------------------
    # Step 4: V3 — Anomaly detection
    # -----------------------------------------------------------------------
    if run_v3:
        anomaly_results = detect_anomalies(segments)
        # V3-004 Suspicious Proportions is cross-statement (needs CA off the CR),
        # so it lives outside detect_anomalies. Gated on the catalog active flag
        # (load_rules already filters by active+standard) → the JSON stays the
        # single on/off switch for the rule.
        if any(r.get("rule_id") == "V3-004" for r in rules):
            anomaly_results += validate_suspicious_proportions(grouped)
        for r in anomaly_results:
            report.add_check(r)

    # -----------------------------------------------------------------------
    # Step 5: V4 — Ratio computation
    # -----------------------------------------------------------------------
    if run_v4:
        ratio_results = compute_ratios(grouped)
        for r in ratio_results:
            report.add_check(r)

    # -----------------------------------------------------------------------
    # Compute severity-weighted score for QCS
    # -----------------------------------------------------------------------
    report.compute_severity_weighted_score()

    logger.info(
        f"Validation complete: {report.passed_checks}/{report.total_checks} passed, "
        f"critical_failures={report.critical_failures}, "
        f"severity_weighted={report.severity_weighted_score}"
    )

    return report
