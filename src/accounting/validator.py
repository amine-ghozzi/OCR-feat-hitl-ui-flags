"""
Automatic Financial Statement Validation

Validates extracted financial data with severity-weighted scoring:
- Balance sheet balancing (Assets = Liabilities + Equity) — CRITICAL
- Subtotal/row-sum verification — ERROR
- Variation percentage consistency — WARNING
- Field type validation — WARNING

Severity levels (used for QCS semantic layer):
- CRITICAL (weight=3): Balance equation, fundamental arithmetic
- ERROR (weight=2): Subtotal mismatches, structural issues
- WARNING (weight=1): Variation %, field types, minor inconsistencies

See: Scoring-Approach-Analysis.md §4.4 (severity-weighted validation)
"""

import re
import logging
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
import pandas as pd
import numpy as np

from accounting.french_number import parse_french_number

logger = logging.getLogger(__name__)


# Severity weights for QCS semantic layer calculation.
# INFO (D8): informational checks (V4 ratios, meta notes) — never move the score.
SEVERITY_WEIGHTS = {"CRITICAL": 3, "ERROR": 2, "WARNING": 1, "INFO": 0}


@dataclass
class ValidationResult:
    """Result of a single validation check."""
    check_name: str
    passed: bool
    message: str
    severity: str = "WARNING"  # CRITICAL, ERROR, WARNING, or INFO
    expected: Optional[float] = None
    actual: Optional[float] = None
    difference: Optional[float] = None
    # D7: the check could not actually run (missing columns/rows, implausible
    # inputs). Skipped checks are NOT passes: they render as "Skipped/N-A" and
    # are excluded from pass counts and severity-weighted scoring.
    skipped: bool = False
    # Multi-occurrence checks (Var% over N rows, VNC over N identities, …) are
    # not binary: ``stats`` carries the tallies — uniform keys
    # ``{"candidates", "verified", "passed", "failed", "not_verifiable",
    # "pass_rate"}`` (validators may add extras). None for single-fact checks.
    stats: Optional[Dict] = None
    # Full (untruncated) row label this check refers to, when locatable — used for
    # the HITL check title and as a simple single-row fallback. None for table-level.
    row_label: Optional[str] = None
    # Precise "Locate" targets: one entry per relevant ROW, each carrying the columns
    # to highlight in it. ``{"label": str, "hint": float|None, "columns": [str]}`` —
    # ``hint`` disambiguates rows that share a label (the value expected in the row);
    # ``columns`` are the value columns whose cells the check actually uses (a vertical
    # sum → one column down several rows; VNC/Var% → a few columns in one row; empty →
    # outline the whole row). Lets the UI highlight exact cells, not just rows.
    locate: Optional[List[Dict]] = None


@dataclass
class ValidationReport:
    """Complete validation report for a document with severity-weighted scoring."""
    checks: List[ValidationResult] = field(default_factory=list)
    overall_passed: bool = True
    total_checks: int = 0
    passed_checks: int = 0
    failed_checks: int = 0
    skipped_checks: int = 0
    critical_failures: int = 0
    severity_weighted_score: Optional[float] = None

    def add_check(self, check: ValidationResult):
        """Add a validation check result."""
        self.checks.append(check)
        self.total_checks += 1
        # D7: a skipped check is neither a pass nor a fail.
        if check.skipped:
            self.skipped_checks += 1
            return
        if check.passed:
            self.passed_checks += 1
        else:
            self.failed_checks += 1
            self.overall_passed = False
            if check.severity == "CRITICAL":
                self.critical_failures += 1

    def compute_severity_weighted_score(self):
        """
        Compute severity-weighted semantic score.

        Formula: sum(severity_weight * pass) / sum(severity_weight)
        where CRITICAL=3, ERROR=2, WARNING=1, INFO=0.

        This gives CRITICAL checks 3x the impact of WARNING checks. Skipped
        checks (D7) and INFO checks (weight 0) do not move the score.
        """
        if self.total_checks == 0:
            self.severity_weighted_score = None
            return

        total_weight = 0
        passed_weight = 0
        for check in self.checks:
            if check.skipped:
                continue  # D7: skipped ≠ pass; excluded from scoring
            w = SEVERITY_WEIGHTS.get(check.severity, 1)
            total_weight += w
            if check.passed:
                passed_weight += w

        self.severity_weighted_score = passed_weight / total_weight if total_weight > 0 else 0.5

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "overall_passed": self.overall_passed,
            "total_checks": self.total_checks,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "skipped_checks": self.skipped_checks,
            "critical_failures": self.critical_failures,
            "severity_weighted_score": (
                round(self.severity_weighted_score, 3)
                if self.severity_weighted_score is not None else None
            ),
            "checks": [
                {
                    "name": c.check_name,
                    "passed": c.passed,
                    "skipped": c.skipped,
                    "severity": c.severity,
                    "message": c.message,
                    "expected": c.expected,
                    "actual": c.actual,
                    "difference": c.difference,
                    "row_label": c.row_label,
                    "locate": c.locate,
                    "stats": c.stats,
                }
                for c in self.checks
            ]
        }


# Keywords for identifying financial statement components
ASSET_KEYWORDS = [
    "actif", "assets", "immobilisation", "stock", "créance", "disponibilité",
    "trésorerie actif", "total actif", "actifs courants", "actifs non courants"
]

LIABILITY_KEYWORDS = [
    "passif", "liabilities", "dette", "fournisseur", "emprunt", "provision",
    "passifs courants", "passifs non courants"
]

EQUITY_KEYWORDS = [
    "capitaux propres", "equity", "capital social", "réserve", "résultat",
    "report à nouveau"
]

TOTAL_KEYWORDS = [
    "total", "sous-total", "subtotal", "somme"
]


def extract_numeric_value(value) -> Optional[float]:
    """
    Extract numeric value from a cell.
    
    Handles:
    - Direct numbers
    - Numbers with spaces (French format: 1 234 567)
    - Numbers with parentheses for negatives
    - Empty cells
    
    Rejects:
    - Concatenated multi-number cells from garbled extraction
    """
    if pd.isna(value):
        return None
    
    if isinstance(value, (int, float)):
        return float(value)
    
    if isinstance(value, str):
        val_str = value.strip()
        if not val_str:
            return None

        # Reject concatenated numbers: multiple comma-formatted groups
        # e.g. "95,276,113 41,694,461" = two numbers jammed together
        import re
        multi_number_re = re.compile(
            r'\d{1,3}(?:,\d{3}){1,}\s+\d{1,3}(?:,\d{3}){1,}'
        )
        if multi_number_re.search(val_str):
            return None  # garbled — multiple values in one cell

        # Remove whitespace used as thousands separator
        cleaned = val_str.replace(" ", "").replace("\u00a0", "")
        
        # Handle parentheses for negatives
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        
        # Remove currency symbols and other non-numeric chars
        cleaned = re.sub(r'[^\d\-.,]', '', cleaned)
        
        # Handle French decimal format (comma as decimal separator)
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        elif "," in cleaned and "." in cleaned:
            # Mixed format, assume comma is thousands separator
            cleaned = cleaned.replace(",", "")
        
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    return None


def normalize_comma_thousands(df: "pd.DataFrame") -> "pd.DataFrame":
    """Strip comma thousands-separators table-wide when the table proves it uses them.

    ``extract_numeric_value`` resolves a comma per TOKEN, and a lone token is
    genuinely ambiguous — ``210,580`` is 210580 under the thousands convention
    and 210.58 under the decimal one. It assumes decimal whenever no dot is
    present, so on an English-grouped statement ``53,536,607`` becomes
    ``53.536.607`` (unparseable → None) and ``210,580`` becomes ``210.58`` — a
    silent 1000× error.

    The disambiguation only exists at TABLE level: a single token carrying two
    or more commas (``\\d,\\d{3},\\d{3}``) proves the comma groups thousands, and
    that convention then applies to every number in the same table. That is
    exactly what :func:`accounting.french_number.infer_comma_convention` scores
    (validated across the 8 benchmark documents), so this reuses it rather than
    re-deriving the rule.

    Returns ``df`` unchanged unless the table proves the thousands convention.
    Percentage cells are left alone: ``4,1%`` is a decimal regardless of how the
    table groups its amounts, and stripping there would read it as 41%.
    """
    try:
        from accounting.french_number import infer_comma_convention
    except ImportError:
        return df

    tokens = [v for v in df.values.ravel() if isinstance(v, str) and "," in v]
    if not tokens or infer_comma_convention(tokens) != "comma_thousands":
        return df

    def _strip(v):
        if isinstance(v, str) and "," in v and "%" not in v:
            return v.replace(",", "")
        return v

    out = df.copy()
    for col in out.columns:
        out[col] = out[col].map(_strip)
    return out


def _canonical_numeric_value(value) -> Optional[float]:
    """Parse an AMOUNT cell with the canonical French-number parser.

    Single source of truth for amount parsing in validation: keeps every
    check at the same scale the extraction pipeline parsed the value at
    (e.g. "-1.603448" → -1603448.0, dot as thousands separator — NOT -1.6).

    Scope — AMOUNT cells only:
    - "%"-bearing cells return None here ("4,1%" → None); percentage cells
      must stay on ``extract_numeric_value``.
    - Garbled multi-number cells ("95,276,113 41,694,461") → None, same as
      ``extract_numeric_value`` (V1-006 rejection preserved).

    Returns float (not Decimal) so downstream tolerance math is unchanged.
    """
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        d = parse_french_number(s)
        return float(d) if d is not None else None
    return None


def find_numeric_columns(df: pd.DataFrame) -> List[str]:
    """Find columns that contain primarily numeric data."""
    numeric_cols = []
    
    for col in df.columns:
        numeric_count = 0
        total_count = 0
        
        for val in df[col]:
            if not pd.isna(val) and str(val).strip():
                total_count += 1
                # Deliberately the lenient parser: column DETECTION must also
                # accept %-cells ("4,1%") that the canonical parser rejects.
                if extract_numeric_value(val) is not None:
                    numeric_count += 1
        
        # Consider column numeric if >50% of values are numbers
        if total_count > 0 and numeric_count / total_count > 0.5:
            numeric_cols.append(col)

    return numeric_cols


# Header tokens marking a Note/Réf reference column, + account-code cell shape.
_NOTE_HEADER_RE = re.compile(r"^\s*(notes?|réf|ref)\s*$", re.IGNORECASE)
_CODE_CELL_RE = re.compile(r"^\(?[A-Za-z]?\d{1,4}\)?$")   # A1, (5111), 21, …


def _is_note_like_column(df: pd.DataFrame, col) -> bool:
    """True if a numeric-LOOKING column is actually a Note/Réf code column.

    A 'Notes' column of short account codes (A1…A8, (5111)) is admitted by
    ``find_numeric_columns`` because the lenient parser reads 'A1'→1, so it can
    be wrongly picked as the value column (V1-001/V1-004 then read an empty
    column). Gate on a header-name match AND a high density of code-shaped cells
    so a legitimately-named small-value column is never dropped.
    """
    if not _NOTE_HEADER_RE.match(str(col).strip()):
        return False
    total = code_like = 0
    for v in df[col]:
        if pd.isna(v) or not str(v).strip():
            continue
        total += 1
        if _CODE_CELL_RE.match(str(v).strip()):
            code_like += 1
    return total > 0 and (code_like / total) >= 0.6


def _row_kind(label: str) -> str:
    """Classify a leaf row for footing: 'amort' (a deduction line),
    'net' (a VNC/Net value), or 'plain'. Used so Brut/Amort/Net ROW-layout
    blocks are footed on the net rows only (summing all three double-counts)."""
    n = re.sub(r"[^a-z0-9]", "", str(label or "").lower())
    if n.startswith(("amortissement", "moinsprovision", "provision", "moinsamort")):
        return "amort"
    if n == "net" or n.startswith(("vnc", "valeurnette")):
        return "net"
    return "plain"


# Columns that are numeric but are NOT amount columns: a variation % or a
# variation/écart delta column must never be picked as the value column.
_NON_VALUE_HEADER_RE = re.compile(r"^\s*(%|var\b|var\.|variation|évol|evol|écart|ecart|δ%?)",
                                  re.IGNORECASE)
_YEAR_OR_N_RE = re.compile(r"^\s*(\d{4}|[Nn]([-_]?\d)?)\s*$")


def select_value_column(df: pd.DataFrame) -> Optional[str]:
    """Best AMOUNT column for intra-statement checks. Excludes Note/Réf code
    columns AND derived columns (variation %, écart/delta). Preference order:
      1. a 'Net' column (Brut/Amort/Net presentation),
      2. a period column (year ``2023`` or ``N``/``N-1``) — most recent first,
      3. the first remaining numeric column.
    None when there is no usable value column.

    Fixes two observed mis-selections: the '%' column (when it precedes the
    period columns) and a 'Notes' A1…A8 code column being read as the value.
    """
    numeric_cols = [
        c for c in find_numeric_columns(df)
        if not _is_note_like_column(df, c)
        and not _NON_VALUE_HEADER_RE.match(str(c).strip())
    ]
    if not numeric_cols:
        return None
    for col in numeric_cols:
        if str(col).strip().lower() == "net":
            return col
    years = [c for c in numeric_cols if _YEAR_OR_N_RE.match(str(c).strip())]
    if years:
        # most recent period first: 4-digit years sort numerically; 'N' before 'N-1'
        def _year_key(c: str):
            s = str(c).strip()
            return -int(s) if s.isdigit() else 0
        return sorted(years, key=_year_key)[0]
    return numeric_cols[0]


def identify_row_type(label: str) -> str:
    """
    Identify the type of a row based on its label.
    
    Returns: 'asset', 'liability', 'equity', 'total', or 'other'
    """
    if not label:
        return 'other'
    
    label_lower = label.lower()
    
    # Check for total rows first
    for kw in TOTAL_KEYWORDS:
        if kw in label_lower:
            has_equity = any(eq_kw in label_lower for eq_kw in EQUITY_KEYWORDS)
            has_liability = any(liab_kw in label_lower for liab_kw in LIABILITY_KEYWORDS)
            has_asset = any(asset_kw in label_lower for asset_kw in ASSET_KEYWORDS)

            # "Total Capitaux Propres et Passifs" = grand total of right side
            if has_equity and has_liability:
                return 'total_equity_and_liabilities'
            if has_asset:
                return 'total_asset'
            if has_liability:
                return 'total_liability'
            if has_equity:
                return 'total_equity'
            return 'total'
    
    # Check category keywords
    for kw in ASSET_KEYWORDS:
        if kw in label_lower:
            return 'asset'
    
    for kw in LIABILITY_KEYWORDS:
        if kw in label_lower:
            return 'liability'
    
    for kw in EQUITY_KEYWORDS:
        if kw in label_lower:
            return 'equity'
    
    return 'other'


def validate_balance_sheet(df: pd.DataFrame, tolerance: float = 0.01) -> ValidationResult:
    """
    Validate that a balance sheet balances.
    
    Assets = Liabilities + Equity
    """
    # Find the label column (usually first column)
    if len(df.columns) < 2:
        return ValidationResult(
            check_name="Balance Sheet Balance",
            passed=False,
            message="Not enough columns to validate"
        )
    
    label_col = df.columns[0]
    # Note-aware: a 'Notes' code column (A1…A8) parses as numeric but must NOT
    # be chosen as the value column, or no total row's value is found.
    value_col = select_value_column(df)

    if value_col is None:
        return ValidationResult(
            check_name="Balance Sheet Balance",
            passed=False,
            message="No numeric columns found"
        )

    total_assets = 0.0
    total_liabilities = 0.0
    total_equity = 0.0
    total_equity_and_liabilities = None  # "Total Capitaux Propres et Passifs"
    total_labels: Dict[str, str] = {}    # row_type -> actual label, for Locate

    for idx, row in df.iterrows():
        label = str(row[label_col]) if pd.notna(row[label_col]) else ""
        value = _canonical_numeric_value(row[value_col])  # amount cell → canonical scale

        if value is None:
            continue

        row_type = identify_row_type(label)

        if row_type == 'total_asset':
            total_assets = value
            total_labels['total_asset'] = label
        elif row_type == 'total_liability':
            total_liabilities = value
            total_labels['total_liability'] = label
        elif row_type == 'total_equity':
            total_equity = value
            total_labels['total_equity'] = label
        elif row_type == 'total_equity_and_liabilities':
            total_equity_and_liabilities = value
            total_labels['total_equity_and_liabilities'] = label

    # Check if we found the totals
    if total_assets == 0:
        return ValidationResult(
            check_name="Balance Sheet Balance",
            passed=False,
            severity="CRITICAL",
            message="Could not identify Total Assets row"
        )

    # The balance equation needs BOTH sides. On an actif-only page (or any doc
    # missing the equity/liabilities totals), it cannot be evaluated — SKIP it
    # rather than false-fail against a zero right-hand side. expected stays None
    # so validate_with_rules surfaces it as a coverage skip, not a failure.
    has_passif_side = (
        total_equity_and_liabilities is not None
        or total_liabilities != 0 or total_equity != 0
    )
    if not has_passif_side:
        return ValidationResult(
            check_name="Balance Sheet Balance",
            passed=True,
            skipped=True,
            severity="CRITICAL",
            message=(
                f"Skipped — found Total Actif ({total_assets:,.0f}) but no "
                f"Capitaux Propres / Passifs total on this page (actif-only) — "
                f"the balance equation needs both sides."
            ),
            actual=total_assets,
            locate=[{"label": total_labels.get('total_asset', ''),
                     "hint": total_assets, "columns": [value_col]}],
        )

    # Prefer direct comparison if "Total Capitaux Propres et Passifs" exists
    if total_equity_and_liabilities is not None:
        expected = total_equity_and_liabilities
    else:
        expected = total_liabilities + total_equity

    difference = abs(total_assets - expected)

    # Allow for rounding tolerance (percentage based)
    threshold = max(abs(total_assets) * tolerance, 1.0)
    passed = difference <= threshold

    # The two/three total rows being compared — Locate highlights each total's cell
    # in the value column (the figures that must balance).
    total_vals = {'total_asset': total_assets, 'total_liability': total_liabilities,
                  'total_equity': total_equity,
                  'total_equity_and_liabilities': total_equity_and_liabilities}
    locate = [{"label": total_labels[k], "hint": total_vals.get(k), "columns": [value_col]}
              for k in ('total_asset', 'total_equity_and_liabilities',
                        'total_liability', 'total_equity')
              if total_labels.get(k)]

    return ValidationResult(
        check_name="Balance Sheet Balance",
        passed=passed,
        severity="CRITICAL",  # Balance equation is fundamental
        message=f"Assets = {total_assets:,.2f}, Liabilities + Equity = {expected:,.2f}"
                + (" ✓" if passed else f" (Difference: {difference:,.2f})"),
        expected=expected,
        actual=total_assets,
        difference=difference,
        locate=locate or None,
    )


def validate_row_sums(df: pd.DataFrame, tolerance: float = 0.01) -> List[ValidationResult]:
    """
    Validate that subtotals match the sum of their component rows.
    
    This is a heuristic validation that looks for 'total' rows and
    checks if nearby numeric values sum correctly.
    """
    results = []
    
    if len(df.columns) < 2:
        return results
    
    label_col = df.columns[0]
    # Note-aware value column (skip a 'Notes' A1..A8 code column — see
    # select_value_column); otherwise the sum reads an empty column.
    value_col = select_value_column(df)
    if value_col is None:
        return results

    # Find all total rows and their values
    for idx, row in df.iterrows():
        label = str(row[label_col]) if pd.notna(row[label_col]) else ""

        if any(kw in label.lower() for kw in TOTAL_KEYWORDS):
            total_value = _canonical_numeric_value(row[value_col])  # amount cell

            if total_value is not None and total_value != 0:
                # Collect the component rows above this total (until another total
                # or the top of the block). Classify each leaf so a Brut/Amort/Net
                # ROW layout (Immobilisations(Brut) / Amortissements / VNC) is
                # footed on the NET rows only — summing all three double-counts
                # (Brut + (−Amort) + VNC = 2·VNC) and would false-fail.
                comps = []   # forward-order (label, value, kind) kind ∈ plain|amort|net
                locate = [{"label": label, "hint": total_value, "columns": [value_col]}]

                # NB floor is -1 (exclusive) so row 0 IS scanned — `max(0, …)`
                # wrongly dropped the first component when the total sits near
                # the top of the table.
                for prev_idx in range(int(idx) - 1, max(-1, int(idx) - 20), -1):
                    if prev_idx not in df.index:
                        continue
                    prev_row = df.loc[prev_idx]
                    prev_label = str(prev_row[label_col]) if pd.notna(prev_row[label_col]) else ""
                    if any(kw in prev_label.lower() for kw in TOTAL_KEYWORDS):
                        break
                    prev_value = _canonical_numeric_value(prev_row[value_col])
                    if prev_value is not None:
                        comps.append((prev_label, prev_value, _row_kind(prev_label)))
                comps.reverse()  # top-to-bottom within the block

                # Foot mixed Brut/Amort/Net + plain-leaf blocks correctly:
                #   net   → include (the net value of a triplet)
                #   amort → exclude ONLY when the block carries a net/VNC row,
                #           because that is the sole case where the deduction is
                #           already accounted for. In the very common NCT
                #           presentation the block runs gross → "Moins:
                #           Amortissement" (a NEGATIVE line) → total with no VNC
                #           row at all; there the deductions ARE addends, and
                #           excluding them over-counted the total by exactly
                #           their sum (observed: 53,772,140 vs 29,496,190, the
                #           difference being the three amortisation lines).
                #   plain → include UNLESS it is the Brut/gross row of a triplet,
                #           i.e. a 'net' row follows it before the next 'plain'
                #           (then its net value is the VNC row, not this gross).
                block_has_net = any(k == "net" for _, _, k in comps)
                summed = []
                for j, (l, v, k) in enumerate(comps):
                    if k == "amort":
                        if block_has_net:
                            continue
                        summed.append((l, v))
                        continue
                    if k == "net":
                        summed.append((l, v)); continue
                    # plain: look ahead to the next plain row; is there a net between?
                    is_gross_of_triplet = False
                    for l2, v2, k2 in comps[j + 1:]:
                        if k2 == "plain":
                            break
                        if k2 == "net":
                            is_gross_of_triplet = True
                            break
                    if not is_gross_of_triplet:
                        summed.append((l, v))
                component_sum = sum(v for _, v in summed)
                component_count = len(summed)
                for l, v in summed:
                    if l:
                        locate.append({"label": l, "hint": v, "columns": [value_col]})

                if component_count >= 2:  # Need at least 2 components
                    difference = abs(total_value - component_sum)
                    threshold = max(abs(total_value) * tolerance, 1.0)
                    passed = difference <= threshold

                    results.append(ValidationResult(
                        check_name=f"Row Sum: {label[:30]}...",
                        passed=passed,
                        severity="ERROR",  # Subtotal mismatches are structural
                        message=f"Expected {total_value:,.2f}, Sum of {component_count} rows = {component_sum:,.2f}"
                                + (" ✓" if passed else f" (Diff: {difference:,.2f})"),
                        expected=total_value,
                        actual=component_sum,
                        difference=difference,
                        row_label=label,
                        locate=locate,
                    ))

    # Family aggregate FIRST (callers cap the list — the stats line must
    # survive the cap): counts, not a binary verdict. INFO weight 0; the
    # per-subtotal entries carry the score.
    if results:
        n = len(results)
        failed = sum(1 for r in results if not r.passed)
        # Roll-up Locate: highlight every subtotal row this tally covers (each
        # child's locate[0] is its subtotal). One click outlines all of them.
        agg_locate = [r.locate[0] for r in results if r.locate]
        results.insert(0, ValidationResult(
            check_name="Row Sums",
            passed=(failed == 0),
            severity="INFO",
            message=(f"All {n} footable subtotals foot ✓" if failed == 0 else
                     f"{n - failed}/{n} footable subtotals foot · {failed} fail"),
            stats={"candidates": n, "verified": n, "passed": n - failed,
                   "failed": failed, "not_verifiable": 0,
                   "pass_rate": round((n - failed) / n, 3)},
            locate=agg_locate,
        ))

    return results


# Plausibility ceiling for a year-over-year variation percentage. Real financial
# YoY swings essentially never exceed a few hundred percent; values far above this
# indicate the cell holds an ABSOLUTE delta amount (current - prior) or is garbled
# OCR — not a percentage. Used to guard the Var% identity (ISSUE-6).
VAR_PCT_PLAUSIBILITY_CEILING = 1000.0

# Strict percent-token parse: an optional sign, digits with optional decimal part
# (comma or dot), optionally followed by a '%'. Garbled OCR cells like "%807" or
# "%5'9-" must NOT pass this. Anchored so stray symbols cause a reject.
_STRICT_PERCENT_TOKEN_RE = re.compile(r'^[+\-]?\d{1,3}(?:[.,]\d+)?\s*%?$')


def _is_strict_percent_token(raw) -> bool:
    """Return True if the raw cell parses cleanly as a percentage token.

    Rejects garbled OCR percent cells (leading '%', embedded quotes/apostrophes,
    concatenated digits) that would otherwise be coerced to a misleading number.
    """
    if pd.isna(raw):
        return False
    s = str(raw).strip()
    if not s:
        return False
    return bool(_STRICT_PERCENT_TOKEN_RE.match(s))


def _is_absolute_delta_cell(reported_var: float, current_val: float, prior_val: float,
                            expected_var: float) -> bool:
    """Decide whether a "variation" cell is an ABSOLUTE delta, not a percentage.

    Returns True (=> the Var% identity should be skipped for this row) when:
      (a) |reported| exceeds the YoY plausibility ceiling AND greatly exceeds
          |expected_var| (an order of magnitude), OR
      (b) reported matches the absolute delta (current - prior) within tolerance,
          i.e. the column carries the delta amount rather than a percent.
    """
    # Identity has PRIORITY: a value that already matches the expected %
    # (within the V1-005 tolerance) IS a percentage — even when it numerically
    # coincides with the delta. Without this, every 0% row on an unchanged
    # line (delta = 0 = reported) was mis-skipped as "delta amount".
    if abs(reported_var - expected_var) <= 0.5:
        return False
    abs_delta = current_val - prior_val
    # (b) reported value IS the absolute delta amount
    delta_tol = max(abs(abs_delta) * 0.01, 1.0)
    if abs(reported_var - abs_delta) <= delta_tol:
        return True
    # (a) implausibly large for a percentage AND far above the expected %
    if abs(reported_var) > VAR_PCT_PLAUSIBILITY_CEILING and \
            abs(reported_var) > abs(expected_var) * 10:
        return True
    return False


def _percent_column_is_delta_bearing(df: pd.DataFrame, candidate_col,
                                     current_col, prior_col) -> bool:
    """At column-selection time, decide if a candidate '%'/'Variation' column
    actually holds absolute delta amounts (so it must not be chosen as the
    percent column). Inspects the rows: if the majority of non-empty cells look
    like absolute deltas rather than percentages, the column is delta-bearing.
    """
    delta_like = 0
    pct_like = 0
    for _, row in df.iterrows():
        current_val = _canonical_numeric_value(row[current_col])  # amount cells
        prior_val = _canonical_numeric_value(row[prior_col])
        # %-candidate cell: MUST stay on the lenient parser — the canonical
        # parser returns None for "%"-bearing strings ("4,1%" → None).
        reported_var = extract_numeric_value(row[candidate_col])
        if current_val is None or prior_val is None or reported_var is None:
            continue
        if prior_val == 0:
            continue
        # D2b: SIGNED prior — abs(prior) sign-flips the expected % for
        # negative-base rows (amortissements/provisions), failing correct cells.
        expected_var = ((current_val - prior_val) / prior_val) * 100
        if _is_absolute_delta_cell(reported_var, current_val, prior_val, expected_var):
            delta_like += 1
        else:
            pct_like += 1
    # Treat as delta-bearing only when we have evidence and deltas dominate.
    return delta_like > 0 and delta_like >= pct_like


def validate_variation_percentages(df: pd.DataFrame, tolerance: float = 0.5) -> List[ValidationResult]:
    """
    Validate that variation percentage columns match the actual calculated variance.
    
    Common column headers: "Variation %", "Var %", "Δ%", "Evolution %"
    Formula: (Current - Prior) / Prior * 100
    
    Args:
        df: DataFrame with period columns and variation column
        tolerance: Allowed percentage point difference (e.g., 0.5 = ±0.5%)
        
    Returns:
        List of validation results for rows with variance discrepancies
    """
    results = []
    
    if len(df.columns) < 3:  # Need at least: label, value1, value2 (or variation)
        return results
    
    # Find period columns FIRST (e.g., 2023, 2022, N, N-1) so the column-selection
    # guard below can inspect candidate cells against the real delta.
    period_cols = []
    for col in df.columns:
        col_str = str(col)
        # Match year patterns or N/N-1 patterns
        if re.match(r'^\d{4}$', col_str) or re.match(r'^N(-\d)?$', col_str):
            period_cols.append(col)

    if len(period_cols) < 2:
        return results

    # Assume first period column is current, second is prior
    current_col = period_cols[0]
    prior_col = period_cols[1]
    label_col = df.columns[0]

    # Find variation column. ISSUE-6 guard: a column headed 'Variation'/'Écart'
    # that actually carries the ABSOLUTE delta amount must NOT be chosen as the
    # percent column, or every row mis-fires a false WARNING.
    variation_col = None
    variation_keywords = ["variation", "var %", "δ%", "evolution", "écart"]

    for col in df.columns:
        col_lower = str(col).lower()
        if any(kw in col_lower for kw in variation_keywords) or "%" in str(col):
            if _percent_column_is_delta_bearing(df, col, current_col, prior_col):
                continue  # delta-bearing column, not a percentage — skip it
            variation_col = col
            break

    if variation_col is None:
        return results
    
    # Row-level coverage honesty: a row this check could not verify (empty %
    # cell, garbled/missing amounts, zero base, non-% token) must NOT vanish
    # into an "all verified ✓" summary — it is collected and surfaced as a
    # SKIPPED coverage entry instead of silently passing.
    validated_count = 0
    validated_loc: List[Dict] = []  # one target per verified row — for the roll-up Locate
    unverifiable: List[Dict] = []   # {"label", "reason", "token", "row", "columns"}
    # Positional row index per df index — duplicate labels (four "Moins
    # provisions pour dépréciation" rows on a bilan) make label-only Locate
    # land on the FIRST twin; the positional index pins the right row.
    pos_of = {ix: p for p, ix in enumerate(df.index)}

    def _blank(v) -> bool:
        return pd.isna(v) or not str(v).strip()

    for idx, row in df.iterrows():
        label = str(row[label_col]) if pd.notna(row[label_col]) else f"Row {idx}"

        current_val = _canonical_numeric_value(row[current_col])  # amount cells
        prior_val = _canonical_numeric_value(row[prior_col])
        # %-cell: stays on the lenient parser (canonical rejects "%" strings)
        reported_var = extract_numeric_value(row[variation_col])

        raw_cur, raw_prior, raw_rep = row[current_col], row[prior_col], row[variation_col]
        if _blank(raw_cur) and _blank(raw_prior) and _blank(raw_rep):
            continue  # genuinely empty row (header/separator) — not a candidate

        # Not verifiable: some inputs exist but the identity cannot run.
        if current_val is None or prior_val is None or reported_var is None:
            if _blank(raw_rep) and (current_val is None or prior_val is None) \
                    and _blank(raw_cur if current_val is None else raw_prior):
                # only one period figure exists and no % — nothing to check
                continue
            if _blank(raw_rep):
                reason, token = "empty % cell", ""
            elif current_val is None or prior_val is None:
                bad = raw_cur if current_val is None else raw_prior
                reason, token = "garbled/missing amount", str(bad).strip()
            else:
                reason, token = "unparseable % cell", str(raw_rep).strip()
            unverifiable.append({"label": label, "reason": reason, "token": token,
                                 "row": pos_of.get(idx),
                                 "columns": [variation_col, current_col, prior_col]})
            continue

        if prior_val == 0:
            unverifiable.append({"label": label, "reason": "zero prior-year base",
                                 "token": str(raw_prior).strip(),
                                 "row": pos_of.get(idx),
                                 "columns": [variation_col, current_col, prior_col]})
            continue

        # Calculate expected variation. D2b: SIGNED prior — dividing by
        # abs(prior) sign-flips the expected % on negative-base rows
        # (amortissements/provisions), failing values the document prints.
        expected_var = ((current_val - prior_val) / prior_val) * 100
        difference = abs(reported_var - expected_var)

        # ISSUE-6 guard: rows where the chosen cell is not actually a
        # percentage cannot be verified — recorded, never silently passed.
        #   (c) raw cell fails a strict percent-token parse (garbled OCR), OR
        #   (a)/(b) the cell holds an absolute delta amount, not a %.
        if not _is_strict_percent_token(row[variation_col]):
            unverifiable.append({"label": label, "reason": "garbled % token",
                                 "token": str(row[variation_col]).strip(),
                                 "row": pos_of.get(idx),
                                 "columns": [variation_col]})
            continue
        if _is_absolute_delta_cell(reported_var, current_val, prior_val, expected_var):
            unverifiable.append({"label": label, "reason": "delta amount, not a %",
                                 "token": str(row[variation_col]).strip(),
                                 "row": pos_of.get(idx),
                                 "columns": [variation_col]})
            continue

        # D9: a computed expected far beyond any plausible YoY % means the
        # INPUT amounts have a scale problem (not a real % discrepancy) —
        # don't surface an absurd "Expected −104,055,036%" as a failure.
        if abs(expected_var) > VAR_PCT_PLAUSIBILITY_CEILING:
            results.append(ValidationResult(
                check_name=f"Var%: {label[:25]}",
                passed=True,
                skipped=True,
                severity="WARNING",
                message=(
                    f"Skipped — computed expected ({expected_var:,.1f}%) is not a "
                    f"plausible percentage; the period amounts likely have a "
                    f"scale/OCR issue. Verify the amount cells."
                ),
                actual=reported_var,
                row_label=label,
                locate=[{"label": label, "hint": reported_var,
                         "columns": [variation_col, current_col, prior_col]}],
            ))
            continue

        # The identity actually ran on this row — it counts as verified
        # whether it passes, fails, or triggers the sign-convention note.
        validated_count += 1
        if label:
            validated_loc.append({"label": label, "hint": reported_var,
                                  "columns": [variation_col, current_col, prior_col]})

        # Only report if there's a significant discrepancy
        if difference > tolerance:
            # D2b sign-convention guard: some preparers report Var% against
            # |prior|. If the base is negative and ONLY the sign differs
            # (magnitudes agree within tolerance), flag the convention as a
            # passing note instead of a hard FAIL.
            if (prior_val < 0 and reported_var * expected_var < 0
                    and abs(abs(reported_var) - abs(expected_var)) <= tolerance):
                results.append(ValidationResult(
                    check_name=f"Var% sign: {label[:25]}",
                    passed=True,
                    severity="WARNING",
                    message=(
                        f"Reported {reported_var:.1f}% vs expected {expected_var:.1f}% — "
                        f"magnitude matches; sign differs (|prior| convention likely)"
                    ),
                    expected=expected_var,
                    actual=reported_var,
                    difference=difference,
                    row_label=label,
                    locate=[{"label": label, "hint": reported_var,
                             "columns": [variation_col, current_col, prior_col]}],
                ))
                continue
            passed = False
            results.append(ValidationResult(
                check_name=f"Var%: {label[:25]}",
                passed=passed,
                severity="WARNING",  # Variation % is informational
                message=f"Reported {reported_var:.1f}%, Expected {expected_var:.1f}%",
                expected=expected_var,
                actual=reported_var,
                difference=difference,
                row_label=label,
                # the % cell + the two period cells it's computed from (one row)
                locate=[{"label": label, "hint": reported_var,
                         "columns": [variation_col, current_col, prior_col]}],
            ))
    
    # Coverage entry: rows the identity could NOT run on are surfaced as a
    # SKIPPED result — visible, excluded from scoring — instead of vanishing
    # into the success summary. An empty/garbled input never justifies a pass
    # (row-level D7: skipped ≠ pass).
    if unverifiable:
        reasons: Dict[str, int] = {}
        for u in unverifiable:
            reasons[u["reason"]] = reasons.get(u["reason"], 0) + 1
        # Itemize PER ROW (the located cells must be explainable one by one),
        # quoting the offending token where there is one.
        items = []
        for u in unverifiable[:8]:
            tok = f" `{u['token'][:20]}`" if u.get("token") else ""
            items.append(f"• {u['label'][:30]} — {u['reason']}{tok}")
        more = f" · +{len(unverifiable) - 8} more" if len(unverifiable) > 8 else ""
        results.append(ValidationResult(
            check_name="Var% coverage",
            passed=True,
            skipped=True,
            severity="WARNING",
            message=(
                f"{len(unverifiable)} row(s) NOT verifiable — the identity could "
                f"not run on them (not counted as verified):  "
                + "  ".join(items) + more
            ),
            # ``row`` (positional) pins the EXACT row — without it, duplicate
            # labels made the outline land on the first same-named twin.
            locate=[{"label": u["label"], "row": u.get("row"), "hint": None,
                     "columns": u["columns"]}
                    for u in unverifiable[:8]],
            stats={"not_verifiable": len(unverifiable), "reasons": reasons},
        ))

    # Family aggregate — a multi-occurrence check is COUNTS AND STATS, not a
    # binary verdict: always emitted when candidate rows exist. Severity INFO
    # (weight 0) so it never double-scores — the per-row failure entries above
    # carry the score; this line carries the statistics.
    failures = [r for r in results if not r.passed]
    n_failed = len(failures)
    n_candidates = validated_count + len(unverifiable)
    if n_candidates > 0:
        n_passed = validated_count - n_failed
        stats = {
            "candidates": n_candidates,
            "verified": validated_count,
            "passed": n_passed,
            "failed": n_failed,
            "not_verifiable": len(unverifiable),
            "pass_rate": round(n_passed / validated_count, 3) if validated_count else None,
        }
        cov = f" · {len(unverifiable)} not verifiable" if unverifiable else ""
        if n_failed == 0 and validated_count > 0:
            msg = f"All {validated_count} verifiable variation percentages verified ✓{cov}"
        else:
            msg = (f"{n_passed}/{validated_count} verifiable variation "
                   f"percentages pass · {n_failed} fail{cov}")
        results.append(ValidationResult(
            check_name="Variation % Check",
            passed=(n_failed == 0),
            # zero rows verified → the family cannot claim a pass (skipped)
            skipped=(validated_count == 0),
            severity="INFO",
            message=msg,
            stats=stats,
            locate=validated_loc[:12],  # cap: highlight the rows it covers, not a wall
        ))

    return results


def auto_correct_variation_percentages(df: pd.DataFrame, tolerance: float = 5.0) -> Tuple[pd.DataFrame, List[Dict]]:
    """
    Auto-correct variation percentage columns by recalculating from numeric values.
    
    When OCR mangles values like "-6,9%" → "%5'9", this function:
    1. Identifies the % variation column (often follows a "Variation" column)
    2. Finds prior/current period columns
    3. Recalculates the % from the numeric values
    4. Replaces obviously wrong values
    
    Args:
        df: DataFrame with period columns and variation column
        tolerance: Max allowed difference before triggering correction (percentage points)
        
    Returns:
        Tuple of (corrected DataFrame, list of corrections made)
    """
    corrections = []
    df_corrected = df.copy()
    
    if len(df.columns) < 3:
        logger.debug(f"auto_correct: Skipping - only {len(df.columns)} columns")
        return df_corrected, corrections
    
    # Find the percentage variation column - try multiple strategies
    variation_pct_col = None
    columns_list = list(df.columns)
    
    # Strategy 1: Look for "%" column that follows a "Variation" column
    for i, col in enumerate(columns_list):
        col_str = str(col).strip().lower()
        if col_str in ["%", "var %", "var%"]:
            variation_pct_col = col
            logger.debug(f"auto_correct: Found % column by exact match: {col}")
            break
        # Check if this column is "Variation" and next one is "%"
        if "variation" in col_str and i + 1 < len(columns_list):
            next_col = str(columns_list[i + 1]).strip()
            if next_col == "%" or "%" in next_col:
                variation_pct_col = columns_list[i + 1]
                logger.debug(f"auto_correct: Found % column after Variation: {variation_pct_col}")
                break
    
    # Strategy 2: Look for column containing variation keywords + %
    if variation_pct_col is None:
        for col in columns_list:
            col_lower = str(col).lower()
            if "%" in col_lower and any(kw in col_lower for kw in ["var", "évol", "ecart"]):
                variation_pct_col = col
                logger.debug(f"auto_correct: Found variation % column by keywords: {col}")
                break
    
    # Strategy 3: Check last column for % symbols in values
    if variation_pct_col is None and len(df.columns) >= 4:
        last_col = columns_list[-1]
        sample_values = df[last_col].dropna().astype(str).head(5)
        if any("%" in str(v) for v in sample_values):
            variation_pct_col = last_col
            logger.debug(f"auto_correct: Found % column by value inspection (last col): {last_col}")
    
    if variation_pct_col is None:
        logger.debug(f"auto_correct: No % column found. Columns: {columns_list}")
        return df_corrected, corrections
    
    # Find numeric period columns (large values = financial amounts)
    period_cols = []
    for col in columns_list:
        if col == variation_pct_col:
            continue
        col_lower = str(col).lower()
        # Skip columns that look like variation/difference columns
        if "variation" in col_lower or "ecart" in col_lower or "diff" in col_lower:
            continue
        # Check if column has large numeric values - sample multiple rows
        non_null = df[col].dropna()
        if len(non_null) == 0:
            continue
        # Check up to 10 values to find a large one
        found_large = False
        for i, val in enumerate(non_null):
            if i >= 10:
                break
            parsed = extract_numeric_value(val)
            if parsed is not None and abs(parsed) > 100:
                found_large = True
                logger.debug(f"auto_correct: Found period column: {col} (sample={parsed})")
                break
        if found_large:
            period_cols.append(col)
    
    if len(period_cols) < 2:
        logger.debug(f"auto_correct: Only {len(period_cols)} period columns found, need 2")
        return df_corrected, corrections
    
    # Use first two large-value columns as current and prior period
    current_col = period_cols[0]
    prior_col = period_cols[1]
    label_col = columns_list[0]
    
    logger.info(f"auto_correct: Processing - label={label_col}, current={current_col}, prior={prior_col}, variation_pct={variation_pct_col}")
    
    # Pre-compute column dot consistency analysis ONCE (cache for performance)
    # If most values don't have dots, but some do, those dots are OCR artifacts
    column_has_dot_artifacts = {}
    for col in [current_col, prior_col]:
        col_values = df[col].dropna().astype(str).tolist()
        dot_count = sum(1 for v in col_values if '.' in v and ',' not in v)
        no_dot_count = sum(1 for v in col_values if '.' not in v and len(re.sub(r'[^\d]', '', v)) > 3)
        # If majority of large values don't have dots, treat any dots as OCR artifacts
        column_has_dot_artifacts[col] = no_dot_count > dot_count
        if column_has_dot_artifacts[col]:
            logger.debug(f"auto_correct: Column {col} has dot artifacts (no_dot={no_dot_count} > dot={dot_count})")
    
    def normalize_column_value(val_str: str, col_name: str) -> Optional[float]:
        """Parse a numeric value, using cached column analysis for dot handling."""
        if pd.isna(val_str):
            return None
        val_str = str(val_str).strip()
        
        # If this column has dot artifacts and this value has a dot (no comma), remove it
        if column_has_dot_artifacts.get(col_name, False) and '.' in val_str and ',' not in val_str:
            cleaned = val_str.replace(" ", "").replace("\xa0", "")
            cleaned = re.sub(r'[^\d\-.]', '', cleaned)
            cleaned = cleaned.replace(".", "")  # Remove OCR artifact dot
            try:
                return float(cleaned) if cleaned else None
            except ValueError:
                return None
        
        return extract_numeric_value(val_str)

    
    for idx, row in df.iterrows():
        label = str(row[label_col]) if pd.notna(row[label_col]) else f"Row {idx}"
        
        # Use column-aware normalization for period values (handles OCR dot artifacts)
        current_val = normalize_column_value(row[current_col], current_col)
        prior_val = normalize_column_value(row[prior_col], prior_col)
        reported_raw = row[variation_pct_col]
        reported_var = extract_numeric_value(reported_raw)
        
        # Skip if missing values or zero base
        if current_val is None or prior_val is None:
            continue
        if prior_val == 0:
            continue
        
        # Calculate expected variation (standard accounting formula, preserves sign)
        expected_var = ((current_val - prior_val) / prior_val) * 100

        # ISSUE-6 guard: do NOT overwrite a legitimate ABSOLUTE-delta column with a
        # recomputed %. If the reported value parses cleanly and equals the delta
        # amount (or is an implausibly large non-percentage), leave it untouched.
        if reported_var is not None and _is_absolute_delta_cell(
                reported_var, current_val, prior_val,
                # D2b: SIGNED prior, consistent with expected_var above
                ((current_val - prior_val) / prior_val) * 100):
            logger.debug(
                f"auto_correct: Row {idx} '{label}': reported={reported_var} looks like "
                f"an absolute delta, not a %; skipping correction")
            continue

        # Check if reported value is clearly wrong
        needs_correction = False

        if reported_var is None:
            # Couldn't even parse a number - definitely needs correction
            needs_correction = True
            logger.debug(f"auto_correct: Row {idx} '{label}': couldn't parse '{reported_raw}'")
        elif abs(reported_var - expected_var) > tolerance:
            # Large discrepancy - likely OCR error
            needs_correction = True
            logger.debug(f"auto_correct: Row {idx} '{label}': mismatch - reported={reported_var}, expected={expected_var:.1f}")
        
        if needs_correction:
            # Format the corrected value (French format with comma).
            # Normalise negative zero first: round(-0.04, 1) → -0.0, and
            # f"+{-0.0:.1f}" renders the garbled token "+-0,0%".
            rounded = round(expected_var, 1)
            if rounded == 0:
                rounded = 0.0
            if rounded >= 0:
                corrected_str = f"+{rounded:.1f}%".replace(".", ",")
            else:
                corrected_str = f"{rounded:.1f}%".replace(".", ",")
            
            corrections.append({
                "row": idx,
                "label": label[:30],
                "original": str(reported_raw),
                "corrected": corrected_str,
                "expected": round(expected_var, 1),
                "parsed_as": reported_var
            })
            
            df_corrected.at[idx, variation_pct_col] = corrected_str
    
    logger.info(f"auto_correct: Made {len(corrections)} corrections")
    return df_corrected, corrections


def validate_field_types(df: pd.DataFrame) -> List[ValidationResult]:
    """
    Validate that each column contains the expected data type.
    
    - First column: labels (strings expected)
    - Other columns: numeric values expected
    
    Returns:
        List of validation results for type issues
    """
    results = []
    
    if len(df.columns) < 2:
        return results
    
    label_col = df.columns[0]
    numeric_cols = df.columns[1:]
    
    # Track issues per column
    empty_count = 0
    invalid_count = 0
    invalid_cells = []
    # Group offending cells per ROW (by df index, not label) → one Locate entry per
    # row. ``match`` records each bad cell's exact text so the UI pinpoints the right
    # row even when two rows share a label (e.g. "Moins provisions pour"). ``order``
    # preserves first-seen order for stable, readable output.
    by_row = {}     # df index -> {label, row, columns, match}
    order = []      # df indices, first-seen order
    pos_of = {ix: p for p, ix in enumerate(df.index)}   # df index -> positional row

    for col in numeric_cols:
        for idx, val in df[col].items():
            label = str(df.loc[idx, label_col]) if label_col in df.columns else f"Row {idx}"

            # Skip empty cells (they might be intentional)
            if pd.isna(val) or str(val).strip() == "":
                empty_count += 1
                continue

            # Check if value should be numeric but isn't
            val_str = str(val).strip()

            # Skip if it's clearly a header or section title
            if any(kw in val_str.lower() for kw in ["total", "actif", "passif", "note"]):
                continue

            # Garbled-prefix guard: a numeric cell led by stray punctuation
            # ('"41 219', '*41219') is OCR garble even though symbol-stripping
            # makes it parseable — the stripped read silently swallows what is
            # usually a mis-OCR'd minus sign. Flag it; never auto-trust it.
            if re.match(r'^["\'*#&@^~`|]', val_str) and any(c.isdigit() for c in val_str):
                numeric = None
            else:
                # Try to extract numeric value — deliberately the STRICTER
                # lenient parser, not the canonical one: parse_french_number
                # accepts trailing-sign garble ("060201-" → -60201) that
                # V1-006 must keep flagging as an invalid cell.
                numeric = extract_numeric_value(val)

            if numeric is None and val_str:
                # Value looks like it should be numeric but isn't
                # Check if it contains digits (suggesting it's a malformed number)
                if any(c.isdigit() for c in val_str):
                    invalid_count += 1
                    if len(invalid_cells) < 3:  # Limit examples
                        invalid_cells.append(f"'{val_str}' at {label[:20]}")
                    if idx not in by_row:
                        by_row[idx] = {"label": label, "row": pos_of.get(idx),
                                       "columns": [], "match": {}}
                        order.append(idx)
                    if col not in by_row[idx]["columns"]:
                        by_row[idx]["columns"].append(col)
                    by_row[idx]["match"][col] = val_str   # exact text → identifies row

    # Report findings — with counts/stats: this check spans every value cell,
    # so its verdict is a tally, not a bare binary.
    n_cells = int(df[numeric_cols].notna().sum().sum())
    checked = max(n_cells - empty_count, 0)
    stats = {
        "candidates": n_cells,
        "verified": checked,
        "passed": max(checked - invalid_count, 0),
        "failed": invalid_count,
        "not_verifiable": empty_count,  # empty cells: nothing to type-check
        "pass_rate": (round((checked - invalid_count) / checked, 3)
                      if checked else None),
    }
    if invalid_count > 0:
        examples = ", ".join(invalid_cells)
        # ``row`` (positional index) pins the exact row even when several share a label
        # AND the bad cell's text was later auto-corrected; ``match`` is a text fallback.
        locate = [{"label": by_row[i]["label"], "row": by_row[i]["row"], "hint": None,
                   "columns": by_row[i]["columns"], "match": by_row[i]["match"]}
                  for i in order]
        results.append(ValidationResult(
            check_name="Field Type Check",
            passed=False,
            severity="ERROR",  # D8: V1-006 spec severity
            message=(f"{invalid_count} invalid of {checked} non-empty value "
                     f"cells (e.g., {examples})"),
            locate=locate or None,
            stats=stats,
        ))
    else:
        results.append(ValidationResult(
            check_name="Field Type Check",
            passed=True,
            severity="ERROR",  # D8: V1-006 spec severity
            message=f"All {checked} non-empty value cells parse as numbers ✓",
            stats=stats,
        ))
    
    return results


def validate_extraction(tables: List[pd.DataFrame]) -> ValidationReport:
    """
    Run all validation checks on extracted tables.
    
    Args:
        tables: List of DataFrames from extraction
        
    Returns:
        ValidationReport with all check results
    """
    report = ValidationReport()
    
    if not tables:
        report.add_check(ValidationResult(
            check_name="Tables Found",
            passed=False,
            severity="CRITICAL",
            message="No tables extracted from document"
        ))
        report.compute_severity_weighted_score()
        return report

    # Add basic check
    report.add_check(ValidationResult(
        check_name="Tables Found",
        passed=True,
        severity="CRITICAL",
        message=f"Found {len(tables)} table(s)"
    ))
    
    for i, df in enumerate(tables):
        table_prefix = f"Table {i+1}" if len(tables) > 1 else ""
        
        # 1. Balance sheet validation
        balance_result = validate_balance_sheet(df)
        if balance_result.expected is not None:
            balance_result.check_name = f"{table_prefix}: {balance_result.check_name}".strip(": ")
            report.add_check(balance_result)
        
        # 2. Row sum validation
        row_results = validate_row_sums(df)
        for result in row_results[:3]:  # Limit to first 3 to avoid noise
            result.check_name = f"{table_prefix}: {result.check_name}".strip(": ")
            report.add_check(result)
        
        # 3. Variation % validation (NEW)
        var_results = validate_variation_percentages(df)
        for result in var_results[:3]:  # Limit to first 3 issues
            result.check_name = f"{table_prefix}: {result.check_name}".strip(": ")
            report.add_check(result)
        
        # 4. Field type validation (NEW)
        type_results = validate_field_types(df)
        for result in type_results:
            result.check_name = f"{table_prefix}: {result.check_name}".strip(": ")
            report.add_check(result)

    # Compute severity-weighted score for QCS semantic layer
    report.compute_severity_weighted_score()

    return report


if __name__ == "__main__":
    # Test with sample data including variation column
    sample_data = {
        "Label": [
            "ACTIFS NON COURANTS",
            "Immobilisations incorporelles",
            "Immobilisations corporelles",
            "TOTAL ACTIFS NON COURANTS",
            "ACTIFS COURANTS",
            "Stocks",
            "Créances",
            "TOTAL ACTIFS COURANTS",
            "TOTAL ACTIF",
        ],
        "2023": [
            "",
            120000,
            220000,
            340000,
            "",
            55000,
            165000,
            220000,
            560000,
        ],
        "2022": [
            "",
            100000,
            200000,
            300000,
            "",
            50000,
            150000,
            200000,
            500000,
        ],
        "Variation %": [
            "",
            20.0,    # Correct: (120000-100000)/100000 = 20%
            10.0,    # Correct: (220000-200000)/200000 = 10%
            13.3,    # Correct: (340000-300000)/300000 = 13.33%
            "",
            10.0,    # Correct: (55000-50000)/50000 = 10%
            10.0,    # Correct: (165000-150000)/150000 = 10%
            10.0,    # Correct
            12.0,    # Correct: (560000-500000)/500000 = 12%
        ]
    }
    
    df = pd.DataFrame(sample_data)
    report = validate_extraction([df])
    
    print("\nValidation Report:")
    print(f"  Overall: {'PASSED' if report.overall_passed else 'FAILED'}")
    print(f"  Checks: {report.passed_checks}/{report.total_checks} passed")
    
    for check in report.checks:
        status = "✓" if check.passed else "✗"
        print(f"  {status} {check.check_name}: {check.message}")

