"""
Column Identification — §15 of the nomenclature revamp plan.

Identifies which column layout model a table uses:
  - ``brut_amort_net``: Brut / Amortissement / Net / N-1 (Bilan Actif)
  - ``n_n1``: Value N / Value N-1 (Bilan Passif, CdR, TFT)
  - ``single``: Single value column (summary tables)

Also identifies which columns are label vs numeric for downstream
number-parsing (§22) and maps column indices to canonical roles.

Public API:

    identify_columns(df, dictionary=None) → ColumnLayout
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)


@dataclass
class ColumnLayout:
    """Result of column identification."""

    model: str  # brut_amort_net | n_n1 | single | unknown
    label_col_index: int = 0
    note_col_index: Optional[int] = None
    numeric_col_indices: list[int] = field(default_factory=list)
    # Maps canonical role → column index
    role_map: dict[str, int] = field(default_factory=dict)
    confidence: float = 0.0


# Header keyword → canonical role mappings (from YAML §15)
_HEADER_KEYWORDS: dict[str, list[str]] = {
    "brut": ["brut", "montant brut", "valeur brute"],
    "amortissement": ["amort", "amortissement", "dépréciation", "amort. et dépréc.",
                      "amort et deprec", "provisions"],
    "net": ["net", "valeur nette", "montant net"],
    # Dated value columns are bound by the YEAR in their header (see
    # _assign_value_columns_by_year), not by a literal "31/12/" keyword: that
    # keyword matched only DD/MM headers, so on a doc mixing 12/31/2024 (N)
    # with 31/12/2023 (N-1) it bound value_n to the prior year. Keep only the
    # textual fallbacks here for year-less headers ("Exercice N" / "N-1").
    "value_n": ["n", "exercice n", "exercice clos"],
    "value_n_minus_1": ["n-1", "exercice n-1", "exercice précédent",
                         "exercice clos le"],
    "note": ["note", "notes", "réf", "ref"],
}

# Threshold for considering a column numeric
_NUMERIC_RATIO_THRESHOLD = 0.40  # ≥40% of cells contain numbers

# 4-digit year (1900–2099) anywhere in a column header. The recency of the
# year — not the date format — decides N vs N-1.
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def identify_columns(
    df: pd.DataFrame,
    dictionary=None,
) -> ColumnLayout:
    """Identify column layout model and map columns to canonical roles.

    Three-layer approach:
      1. Header keyword matching (fastest, most reliable)
      2. Data-type inference (numeric ratio per column)
      3. Column-count heuristic (fallback)

    Args:
        df: Table DataFrame to classify.
        dictionary: Optional NomenclatureDictionary (for column_models config).

    Returns:
        ColumnLayout with model type, role mapping, and numeric column indices.
    """
    if df.empty or len(df.columns) < 2:
        return ColumnLayout(model="single", confidence=0.5)

    n_cols = len(df.columns)

    # Layer 1 — Header keyword matching
    role_map = _match_header_keywords(df)
    model, conf = _infer_model_from_roles(role_map, n_cols)
    if conf >= 0.80:
        layout = ColumnLayout(model=model, confidence=conf, role_map=role_map)
        # Label/note first: _fill_numeric_indices excludes them from the
        # data-detected side, and cannot do so before they are known.
        layout.label_col_index = _find_label_column(df)
        layout.note_col_index = _find_note_column(df, layout.label_col_index)
        _fill_numeric_indices(layout, df)
        _assign_positional_roles(layout)
        return layout

    # Layer 2 — Data-type inference
    numeric_cols = _find_numeric_columns(df)
    label_col = _find_label_column(df)

    # Layer 3 — Column-count heuristic
    non_label_cols = [i for i in range(n_cols) if i != label_col]
    n_numeric = len(numeric_cols)

    if n_numeric >= 3 and n_cols >= 4:
        model = "brut_amort_net"
        conf = 0.60
    elif n_numeric >= 1:
        model = "n_n1"
        conf = 0.55
    else:
        model = "unknown"
        conf = 0.30

    layout = ColumnLayout(
        model=model,
        label_col_index=label_col,
        numeric_col_indices=numeric_cols,
        confidence=conf,
        role_map=role_map,
    )

    # Try to detect note column (usually col 1, all short strings)
    layout.note_col_index = _find_note_column(df, label_col)

    # Header matching didn't role the value columns (generic headers — common
    # in Tier-0 native extraction, where the real headers land in row 0).
    # Assign value/note roles by position so both tiers key these cells
    # identically and reconciliation aligns them instead of duplicating.
    _assign_positional_roles(layout)

    return layout


def _assign_positional_roles(layout: ColumnLayout) -> None:
    """Fill value/note roles by column position when header matching didn't.

    Tier-0 native extraction often yields generic column names ("Col2",
    "(en DT)") because the real headers land in the first data row. Without a
    role, those cells key by raw name and fail to align with the role-keyed
    cells from Tier 1 during reconciliation — producing duplicate value
    columns. Assigning roles by position (the French bilan convention is N then
    N-1, left to right) makes both tiers key value cells identically so the
    reconciler merges them. Only fills *missing* roles; never re-assigns a
    column already bound to another role.
    """
    role_map = layout.role_map
    roled_indices = set(role_map.values())

    note_idx = layout.note_col_index
    if note_idx is not None and "note" not in role_map and note_idx not in roled_indices:
        role_map["note"] = note_idx
        roled_indices.add(note_idx)

    # Numeric value columns, left to right, excluding label / note / columns
    # already bound to a role.
    value_cols = [
        i for i in layout.numeric_col_indices
        if i not in roled_indices
        and i != layout.label_col_index
        and i != note_idx
    ]
    if layout.model == "brut_amort_net":
        order = ["brut", "amortissement", "net", "value_n_minus_1"]
    else:  # n_n1 / single / unknown → N, N-1
        order = ["value_n", "value_n_minus_1"]
    for role, col_idx in zip(order, value_cols):
        if role not in role_map:
            role_map[role] = col_idx


def _match_header_keywords(df: pd.DataFrame) -> dict[str, int]:
    """Match column headers against canonical role keywords."""
    role_map: dict[str, int] = {}

    for col_idx, col_name in enumerate(df.columns):
        col_str = str(col_name).strip().lower()
        if not col_str or col_str.startswith("unnamed"):
            continue

        best_role: Optional[str] = None
        best_score: float = 0.0

        for role, keywords in _HEADER_KEYWORDS.items():
            for kw in keywords:
                score = fuzz.ratio(col_str, kw.lower())
                if score > best_score and score >= 70:
                    best_score = score
                    best_role = role

        if best_role and best_role not in role_map:
            role_map[best_role] = col_idx

    # Dated headers override any keyword guess for the two value columns:
    # the most recent year is N, the next is N-1. Robust to mixed date
    # orderings (12/31/2024 vs 31/12/2023) that broke the old keyword path.
    _assign_value_columns_by_year(df, role_map)

    return role_map


def _assign_value_columns_by_year(df: pd.DataFrame, role_map: dict[str, int]) -> None:
    """Bind ``value_n`` / ``value_n_minus_1`` from the year in each header.

    Overrides any keyword-based assignment for these two roles. The header
    carrying the most recent year becomes ``value_n``; the next most recent
    becomes ``value_n_minus_1``. When several columns share a year (e.g. a
    fragmented header), the leftmost wins. No-op when no NUMERIC header carries
    a year — textual headers ("Exercice N" / "N-1") still go through keyword
    matching.

    Only **numeric** columns are eligible. A year in a header does not make the
    column a period column: PyMuPDF names the first column after the table
    *title*, so a natively-extracted Bilan yields
    ``['Bilan au 31/12/2024', 'Col1', 'Col2', '(en DT)']`` — the LABEL column
    carries the only year on the page. Considering it bound ``value_n`` to the
    labels and orphaned the real amounts entirely (the amount column, whose
    generic ``Col2`` header has no year, then got no role at all). The previous
    "upgrade to a numeric column" rule could not catch this: it only fired when
    a *second* column shared the same year, and with a lone year there is no
    competition.
    """
    numeric_cols = set(_find_numeric_columns(df))
    best_for_year: dict[int, int] = {}  # year → chosen column index
    for col_idx, col_name in enumerate(df.columns):
        if col_idx not in numeric_cols:
            continue
        match = _YEAR_RE.search(str(col_name))
        if not match:
            continue
        # First (leftmost) numeric column for a year wins.
        best_for_year.setdefault(int(match.group()), col_idx)

    if not best_for_year:
        return

    by_recency = sorted(best_for_year.items(), key=lambda kv: kv[0], reverse=True)
    role_map["value_n"] = by_recency[0][1]
    if len(by_recency) >= 2:
        role_map["value_n_minus_1"] = by_recency[1][1]


def _infer_model_from_roles(role_map: dict[str, int], n_cols: int) -> tuple[str, float]:
    """Determine column model from identified roles."""
    has_brut = "brut" in role_map
    has_amort = "amortissement" in role_map
    has_net = "net" in role_map
    has_n = "value_n" in role_map
    has_n1 = "value_n_minus_1" in role_map

    if has_brut and has_net:
        return "brut_amort_net", 0.95 if has_amort else 0.85
    if has_n or has_n1:
        return "n_n1", 0.90 if (has_n and has_n1) else 0.75
    if n_cols == 2:
        return "single", 0.50

    return "unknown", 0.0


def _find_numeric_columns(df: pd.DataFrame) -> list[int]:
    """Find columns where ≥ threshold of non-null cells are numeric."""
    numeric_cols: list[int] = []
    _NUM_RE = re.compile(r"^[\d\s.,()—–\-€DT]+$")

    for col_idx in range(len(df.columns)):
        total = 0
        numeric_count = 0
        for val in df.iloc[:, col_idx]:
            if pd.isna(val):
                continue
            total += 1
            text = str(val).strip()
            if text and _NUM_RE.match(text):
                numeric_count += 1

        if total > 0 and numeric_count / total >= _NUMERIC_RATIO_THRESHOLD:
            numeric_cols.append(col_idx)

    return numeric_cols


def _find_label_column(df: pd.DataFrame) -> int:
    """Find the column most likely to contain row labels (longest text)."""
    avg_lengths: list[float] = []
    for col_idx in range(len(df.columns)):
        lengths = [
            len(str(v))
            for v in df.iloc[:, col_idx]
            if pd.notna(v)
        ]
        avg_lengths.append(sum(lengths) / max(len(lengths), 1))

    if not avg_lengths:
        return 0
    return avg_lengths.index(max(avg_lengths))


def _find_note_column(df: pd.DataFrame, label_col: int) -> Optional[int]:
    """Detect a 'Note' reference column (short strings like '1', '2a', etc.)."""
    for col_idx in range(len(df.columns)):
        if col_idx == label_col:
            continue

        lengths = []
        for val in df.iloc[:, col_idx]:
            if pd.notna(val):
                lengths.append(len(str(val).strip()))

        if not lengths:
            continue

        avg_len = sum(lengths) / len(lengths)
        max_len = max(lengths)
        # Note columns are very short (1-4 chars)
        if avg_len <= 3.0 and max_len <= 5:
            return col_idx

    return None


def _fill_numeric_indices(layout: ColumnLayout, df: pd.DataFrame) -> None:
    """Populate numeric_col_indices — the UNION of roled and data-detected.

    This used to return only the roled columns, which made a header-keyword miss
    silently destructive rather than merely incomplete: enrichment parses a cell
    only when its column is in this list, so a numeric column the keywords did
    not recognise (a Variation/Écart column, an extra period) stopped being read
    as a number anywhere downstream. Being unroled should cost a column its ROLE,
    never its VALUES.

    The label and note columns stay excluded. A Note/Réf column of parenthesised
    account codes ('(5111)') parses as numeric via the parens→negative branch, so
    a naive union would readmit it as an amount column — the ISSUE-7a defect.
    """
    numeric_roles = {"brut", "amortissement", "net", "value_n", "value_n_minus_1"}
    roled = {idx for role, idx in layout.role_map.items() if role in numeric_roles}
    excluded = {layout.label_col_index, layout.note_col_index} - {None}
    detected = {i for i in _find_numeric_columns(df) if i not in excluded}
    layout.numeric_col_indices = sorted(roled | detected)
