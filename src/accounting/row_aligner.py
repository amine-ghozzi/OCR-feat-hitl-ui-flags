"""
Row Alignment Across Tiers — §20 of the nomenclature revamp plan.

Tier 0 (PyMuPDF native) and Tier 1 (Docling OCR) frequently extract a
*different* number of rows from the same physical table — OCR may turn a
decorative line into an extra row, or split a multi-line label into two.
Cell-level reconciliation cannot be done by row index in that case;
``row[5]`` from Tier 0 is not necessarily the same line item as
``row[5]`` from Tier 1.

The plan resolves this with a 4-phase alignment cascade:

    Phase 1 — Canonical key join     (O(n)) — rows that match the same
                                                  canonical_term align directly
    Phase 2 — Normalized text match  (O(n*m)) — Levenshtein on raw_text
    Phase 3 — Positional tiebreaker   — relative position within section
    Phase 4 — Orphan handling          — record provenance, never silently drop

Public API:

    align_canonical_tables(table_a, table_b) → AlignmentResult
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz

from accounting.canonical_model import CanonicalTable

logger = logging.getLogger(__name__)


# Confidence thresholds (§20)
_CANONICAL_MATCH_CONFIDENCE = 1.0
_TEXT_MATCH_CONFIDENCE_FLOOR = 0.70  # Levenshtein ratio
_POSITIONAL_MATCH_CONFIDENCE = 0.50  # weakest — flag yellow downstream


@dataclass
class AlignedRowPair:
    """A single aligned (a_row, b_row) pair plus how the alignment happened."""

    a_index: int
    b_index: int
    method: str          # canonical | text | positional
    confidence: float


@dataclass
class AlignmentResult:
    """Outcome of aligning two CanonicalTables."""

    pairs: list[AlignedRowPair] = field(default_factory=list)
    a_orphans: list[int] = field(default_factory=list)  # indices in table_a only
    b_orphans: list[int] = field(default_factory=list)  # indices in table_b only

    @property
    def coverage(self) -> float:
        """Fraction of table_a rows that found a partner in table_b."""
        total_a = len(self.pairs) + len(self.a_orphans)
        if total_a == 0:
            return 0.0
        return len(self.pairs) / total_a


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def align_canonical_tables(
    table_a: CanonicalTable,
    table_b: CanonicalTable,
) -> AlignmentResult:
    """Align rows between two CanonicalTables using the §20 cascade.

    The cascade short-circuits — once a row is aligned via Phase 1 (canonical
    key match) it is removed from the candidate pool for Phase 2/3, so each
    row is paired at most once.
    """
    result = AlignmentResult()
    n_a = len(table_a.rows)
    n_b = len(table_b.rows)

    if n_a == 0 and n_b == 0:
        return result
    if n_a == 0:
        result.b_orphans = list(range(n_b))
        return result
    if n_b == 0:
        result.a_orphans = list(range(n_a))
        return result

    matched_a: set[int] = set()
    matched_b: set[int] = set()

    # ------------------------------------------------------------------
    # Phase 1 — Canonical key join (O(n))
    # ------------------------------------------------------------------
    # Build canonical_term → b_index map for unmatched B rows. When the
    # same canonical_term appears multiple times on the B side (rare —
    # would be a duplicate row), only the first remains available.
    b_by_canonical: dict[str, int] = {}
    for i, row_b in enumerate(table_b.rows):
        ct = row_b.canonical_term
        if ct and ct not in b_by_canonical:
            b_by_canonical[ct] = i

    for i, row_a in enumerate(table_a.rows):
        if not row_a.canonical_term:
            continue
        b_idx = b_by_canonical.get(row_a.canonical_term)
        if b_idx is not None and b_idx not in matched_b:
            result.pairs.append(AlignedRowPair(
                a_index=i,
                b_index=b_idx,
                method="canonical",
                confidence=_CANONICAL_MATCH_CONFIDENCE,
            ))
            matched_a.add(i)
            matched_b.add(b_idx)

    # ------------------------------------------------------------------
    # Phase 2 — Normalized text match (O(remaining_a * remaining_b))
    # ------------------------------------------------------------------
    remaining_a = [i for i in range(n_a) if i not in matched_a]
    remaining_b = [i for i in range(n_b) if i not in matched_b]

    if remaining_a and remaining_b:
        # Greedy best-match from each remaining_a row.
        # Higher fuzz scores get priority by sorting (a, score) descending.
        candidate_pairs: list[tuple[int, int, float]] = []
        for a_i in remaining_a:
            text_a = table_a.rows[a_i].raw_text or ""
            if not text_a.strip():
                continue
            for b_i in remaining_b:
                text_b = table_b.rows[b_i].raw_text or ""
                if not text_b.strip():
                    continue
                score = fuzz.ratio(text_a.lower(), text_b.lower()) / 100.0
                if score >= _TEXT_MATCH_CONFIDENCE_FLOOR:
                    candidate_pairs.append((a_i, b_i, score))

        # Resolve greedily: highest-scoring pair first, exclude consumed indices.
        candidate_pairs.sort(key=lambda t: t[2], reverse=True)
        for a_i, b_i, score in candidate_pairs:
            if a_i in matched_a or b_i in matched_b:
                continue
            result.pairs.append(AlignedRowPair(
                a_index=a_i,
                b_index=b_i,
                method="text",
                confidence=score,
            ))
            matched_a.add(a_i)
            matched_b.add(b_i)

    # ------------------------------------------------------------------
    # Phase 3 — Positional tiebreaker (within-section)
    # ------------------------------------------------------------------
    # If both sides have remaining rows in the same section, pair by
    # relative order of appearance — useful when OCR mangles labels but
    # row order is preserved.
    remaining_a = [i for i in range(n_a) if i not in matched_a]
    remaining_b = [i for i in range(n_b) if i not in matched_b]

    if remaining_a and remaining_b:
        # Bucket by section
        a_by_section: dict[Optional[str], list[int]] = {}
        b_by_section: dict[Optional[str], list[int]] = {}
        for a_i in remaining_a:
            sec = table_a.rows[a_i].section
            a_by_section.setdefault(sec, []).append(a_i)
        for b_i in remaining_b:
            sec = table_b.rows[b_i].section
            b_by_section.setdefault(sec, []).append(b_i)

        for section, a_indices in a_by_section.items():
            b_indices = b_by_section.get(section)
            if not b_indices:
                continue
            # Pair off in order
            for a_i, b_i in zip(a_indices, b_indices):
                result.pairs.append(AlignedRowPair(
                    a_index=a_i,
                    b_index=b_i,
                    method="positional",
                    confidence=_POSITIONAL_MATCH_CONFIDENCE,
                ))
                matched_a.add(a_i)
                matched_b.add(b_i)

    # ------------------------------------------------------------------
    # Phase 4 — Orphan handling
    # ------------------------------------------------------------------
    result.a_orphans = sorted(i for i in range(n_a) if i not in matched_a)
    result.b_orphans = sorted(i for i in range(n_b) if i not in matched_b)

    logger.info(
        f"Row alignment: {len(result.pairs)} pairs, "
        f"a_orphans={len(result.a_orphans)}, b_orphans={len(result.b_orphans)}, "
        f"coverage={result.coverage:.0%}"
    )
    return result
