"""
Multi-Page Table Continuation Detector & Merger — §21.

Detects when a financial table spans multiple PDF pages by checking
4 continuation signals:
  1. Column count match
  2. Column X-offset alignment
  3. Header deduplication (repeated header on next page)
  4. Open section (no subtotal row yet)

After detection, merges fragments into a single DataFrame.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

_CONTINUATION_THRESHOLD = 2.5  # need ≥ 2.5 out of 4 signals to merge
_X_OFFSET_TOLERANCE_PT = 5  # column X-offset alignment tolerance in points
_HEADER_FUZZY_CUTOFF = 85  # fuzzy match score for header deduplication
_MAX_MERGE_DEPTH = 5  # no financial table exceeds 5 pages


@dataclass
class PageTable:
    """A table extracted from a single PDF page, with page metadata."""

    df: pd.DataFrame
    page_index: int
    table_index_on_page: int = 0
    col_x_offsets: list[float] = field(default_factory=list)


@dataclass
class ContinuationSignal:
    """Results of the 4-signal continuation check."""

    col_count_match: bool = False
    x_alignment_ratio: float = 0.0
    header_duplicated: bool = False
    open_section: bool = False
    total_score: float = 0.0
    # None = could not tell (at least one fragment is unclassified); False = the
    # two fragments are confidently DIFFERENT statements.
    same_statement: Optional[bool] = None
    veto_reason: Optional[str] = None

    @property
    def is_continuation(self) -> bool:
        """Score alone is not sufficient — a confident type disagreement vetoes.

        The four signals are weak evidence of *layout* similarity, not of
        belonging to the same statement. Measured on a real 3-statement pack,
        a Bilan Passif → Compte de Résultat pair scores IDENTICALLY to a
        Bilan Actif → Bilan Passif pair (both 1.5), because every table there
        happens to have 4 columns and `col_count_match` fires for all of them.
        Raising the score by threading real x-offsets therefore risks merging
        two unrelated statements into one — the veto is what makes the
        remaining signals safe to strengthen.
        """
        if self.same_statement is False:
            return False
        return self.total_score >= _CONTINUATION_THRESHOLD


# ---------------------------------------------------------------------------
# Signal detectors
# ---------------------------------------------------------------------------


def _signal_col_count(table_a: PageTable, table_b: PageTable) -> float:
    """Signal 1: Do both tables have the same number of columns?"""
    if table_a.df.shape[1] == table_b.df.shape[1]:
        return 1.0
    return 0.0


def _signal_x_alignment(table_a: PageTable, table_b: PageTable) -> float:
    """Signal 2: Are column X-offsets aligned within tolerance?"""
    if not table_a.col_x_offsets or not table_b.col_x_offsets:
        return 0.5  # no bbox data — neutral score

    n_cols = min(len(table_a.col_x_offsets), len(table_b.col_x_offsets))
    if n_cols == 0:
        return 0.5

    aligned = sum(
        1
        for i in range(n_cols)
        if abs(table_a.col_x_offsets[i] - table_b.col_x_offsets[i])
        < _X_OFFSET_TOLERANCE_PT
    )
    ratio = aligned / n_cols
    return 1.0 if ratio >= 0.80 else 0.0


def _signal_header_dedup(table_a: PageTable, table_b: PageTable) -> tuple[float, bool]:
    """Signal 3: Is table_b's first row a duplicate of table_a's header?

    Returns (score, is_duplicate_header).
    """
    if table_b.df.empty:
        return 0.0, False

    # Compare header names (column labels) with first data row
    header_text = " ".join(str(c) for c in table_a.df.columns)
    first_row_text = " ".join(str(v) for v in table_b.df.iloc[0])

    score = fuzz.token_sort_ratio(header_text, first_row_text)
    if score >= _HEADER_FUZZY_CUTOFF:
        return 1.0, True

    # Check if first row contains NO header keywords (likely continuation)
    header_keywords = {"total", "brut", "amort", "net", "n-1", "exercice", "note"}
    first_row_lower = first_row_text.lower()
    has_header_words = any(kw in first_row_lower for kw in header_keywords)
    if not has_header_words:
        return 0.5, False

    return 0.0, False


def _signal_open_section(
    table_a: PageTable,
    dictionary=None,
) -> float:
    """Signal 4: Does the last table end mid-section (no subtotal row)?

    Uses the NomenclatureDictionary to check for subtotal markers.
    """
    if table_a.df.empty:
        return 0.0

    # Check last few rows for subtotal keywords
    subtotal_keywords = {"total", "sous-total", "total des"}
    n_check = min(3, len(table_a.df))
    label_col = table_a.df.columns[0] if len(table_a.df.columns) > 0 else None

    if label_col is None:
        return 0.5

    for idx in range(len(table_a.df) - n_check, len(table_a.df)):
        cell_text = str(table_a.df.iloc[idx][label_col]).lower().strip()
        if any(kw in cell_text for kw in subtotal_keywords):
            return 0.0  # section properly closed

    return 1.0  # no subtotal found — section still open


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _same_statement(
    table_a: PageTable,
    table_b: PageTable,
    dictionary,
) -> tuple[Optional[bool], Optional[str]]:
    """Do both fragments confidently classify to the SAME statement?

    Returns ``(verdict, reason)``. ``None`` means *no evidence either way* — and
    that distinction is the whole design. A genuine continuation page lacks the
    headers and section markers that identify a statement, so it very often
    cannot be classified at all; treating "unclassified" as disagreement would
    veto exactly the merges this exists to enable. Only a CONFIDENT
    disagreement (both sides clear the classification gate and name different
    statements) is allowed to veto.
    """
    if dictionary is None:
        return None, None
    try:
        from accounting.statement_segmenter import _collect_table_labels
    except Exception:  # pragma: no cover - defensive
        return None, None

    try:
        ca = dictionary.classify_statement_detailed(_collect_table_labels(table_a.df))
        cb = dictionary.classify_statement_detailed(_collect_table_labels(table_b.df))
    except Exception:  # pragma: no cover - never break merging
        return None, None

    if not (ca.passes_gate and cb.passes_gate):
        return None, None
    if ca.qualified_type == cb.qualified_type:
        return True, None
    return False, f"{ca.qualified_type} != {cb.qualified_type}"


def detect_continuation(
    table_a: PageTable,
    table_b: PageTable,
    dictionary=None,
) -> ContinuationSignal:
    """Run all 4 continuation signals between two consecutive tables."""
    sig = ContinuationSignal()
    sig.same_statement, sig.veto_reason = _same_statement(table_a, table_b, dictionary)

    # Signal 1
    s1 = _signal_col_count(table_a, table_b)
    sig.col_count_match = s1 >= 1.0

    # Signal 2
    s2 = _signal_x_alignment(table_a, table_b)
    sig.x_alignment_ratio = s2

    # Signal 3
    s3, sig.header_duplicated = _signal_header_dedup(table_a, table_b)

    # Signal 4
    s4 = _signal_open_section(table_a, dictionary)
    sig.open_section = s4 >= 1.0

    sig.total_score = s1 + s2 + s3 + s4
    return sig


def merge_page_tables(tables: list[PageTable], dictionary=None) -> list[pd.DataFrame]:
    """Merge multi-page table fragments into complete DataFrames.

    Iteratively checks consecutive table pairs and merges those that
    pass the continuation threshold. Returns a list of merged DataFrames.

    Args:
        tables: Tables sorted by (page_index, table_index_on_page).
        dictionary: Optional NomenclatureDictionary for open-section signal.

    Returns:
        List of merged DataFrames — each one is a complete table.
    """
    if not tables:
        return []
    if len(tables) == 1:
        return [tables[0].df]

    # Sort by page order
    sorted_tables = sorted(tables, key=lambda t: (t.page_index, t.table_index_on_page))

    merged: list[pd.DataFrame] = []
    current_df = sorted_tables[0].df.copy()
    current_table = sorted_tables[0]

    for i in range(1, len(sorted_tables)):
        next_table = sorted_tables[i]

        # Only check continuation for tables on consecutive pages
        page_gap = next_table.page_index - current_table.page_index
        if page_gap > 1:
            # Non-consecutive pages — never a continuation
            merged.append(current_df)
            current_df = next_table.df.copy()
            current_table = next_table
            continue

        # Check continuation signals
        sig = detect_continuation(current_table, next_table, dictionary)

        if sig.is_continuation:
            logger.info(
                f"Multi-page merge: tables on page {current_table.page_index} → "
                f"{next_table.page_index} (score={sig.total_score:.1f})"
            )
            # Merge
            next_df = next_table.df.copy()
            if sig.header_duplicated and len(next_df) > 1:
                next_df = next_df.iloc[1:]  # skip duplicate header

            # Ensure columns align
            if list(current_df.columns) == list(next_df.columns):
                current_df = pd.concat([current_df, next_df], ignore_index=True)
            else:
                # Force column alignment by name overlap
                next_df.columns = current_df.columns[: len(next_df.columns)]
                current_df = pd.concat([current_df, next_df], ignore_index=True)

            # Update current_table reference for next iteration
            current_table = PageTable(
                df=current_df,
                page_index=next_table.page_index,
                table_index_on_page=next_table.table_index_on_page,
                col_x_offsets=current_table.col_x_offsets,
            )
        else:
            merged.append(current_df)
            current_df = next_table.df.copy()
            current_table = next_table

    merged.append(current_df)
    logger.info(
        f"Multi-page merge: {len(sorted_tables)} fragments → {len(merged)} tables"
    )
    return merged
