"""
Grouped Entry Detection — §17.

After fuzzy matching (Stage 6), some rows remain unmatched. This module
classifies them as:
  - **grouped**: A sub-item that belongs to a known parent entry
    (e.g., "Terrains" under "Immobilisations corporelles")
  - **custom**: A company-specific line item not in the Nomenclature
    (e.g., "Indemnité de départ" — legitimate but non-standard)

Runs only on rows where ``match_type == 'custom'`` or ``'unrecognized'``.

Public API:

    classify_unmatched_rows(df, match_results, dictionary, sections) → list[GroupedResult]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class GroupedResult:
    """Classification of an unmatched row."""

    row_index: int
    raw_label: str
    classification: str  # "grouped" | "custom" | "header" | "empty"
    parent_canonical: Optional[str] = None  # if grouped, which parent
    parent_section: Optional[str] = None
    confidence: float = 0.0


def classify_unmatched_rows(
    df: pd.DataFrame,
    match_results: list,
    dictionary=None,
    sections: Optional[list[Optional[str]]] = None,
) -> list[GroupedResult]:
    """Classify rows that fuzzy_match couldn't resolve.

    Strategy:
      1. Empty/whitespace rows → "empty"
      2. Rows that look like section headers → "header"
      3. Rows within a known section → "grouped" (child of that section)
      4. Everything else → "custom"

    Args:
        df: Table DataFrame.
        match_results: List of MatchResult from fuzzy_match (one per row).
        dictionary: NomenclatureDictionary.
        sections: Section assignments per row (from header_detector).

    Returns:
        List of GroupedResult for unmatched rows only.
    """
    if df.empty or len(df.columns) == 0:
        return []

    label_col_idx = 0
    results: list[GroupedResult] = []

    for i, mr in enumerate(match_results):
        # Only process unmatched rows
        if mr.entry is not None:
            continue

        label = str(df.iloc[i, label_col_idx]).strip() if i < len(df) else ""

        # Case 1 — Empty
        if not label:
            results.append(GroupedResult(
                row_index=i,
                raw_label=label,
                classification="empty",
                confidence=1.0,
            ))
            continue

        # Case 2 — Looks like a header (all caps, no numbers)
        if _is_header_like(label):
            results.append(GroupedResult(
                row_index=i,
                raw_label=label,
                classification="header",
                confidence=0.80,
            ))
            continue

        # Case 3 — Within a known section → grouped sub-item
        section = sections[i] if sections and i < len(sections) else None
        if section and dictionary:
            parent = _find_section_parent(section, dictionary)
            results.append(GroupedResult(
                row_index=i,
                raw_label=label,
                classification="grouped",
                parent_canonical=parent,
                parent_section=section,
                confidence=0.70,
            ))
            continue

        # Case 4 — Custom / company-specific
        results.append(GroupedResult(
            row_index=i,
            raw_label=label,
            classification="custom",
            confidence=0.50,
        ))

    logger.info(
        f"Grouped entry detection: {len(results)} unmatched rows classified — "
        f"{sum(1 for r in results if r.classification == 'grouped')} grouped, "
        f"{sum(1 for r in results if r.classification == 'custom')} custom"
    )
    return results


def _is_header_like(label: str) -> bool:
    """Quick check if a label looks like a section header."""
    import re
    # ALL-CAPS with possible accented chars
    caps_re = re.compile(r"^[A-ZÀÂÄÉÈÊËÏÎÔÙÛÜŸÇŒÆ\s\-'.,()]+$")
    return bool(caps_re.match(label)) and len(label) >= 3


def _find_section_parent(section_key: str, dictionary) -> Optional[str]:
    """Find the canonical parent entry for a section."""
    section_cfg = dictionary.sections.get(section_key, {})
    return section_cfg.get("label_canonical")
