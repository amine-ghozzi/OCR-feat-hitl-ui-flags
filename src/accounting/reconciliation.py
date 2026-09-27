"""
Dual-Tier Reconciliation — §10 of the nomenclature revamp plan.

Once Tier 0 and Tier 1 results are available for the same physical document,
this module produces a *merged* CanonicalTable per source table:

    - Numeric cells: trust Tier 0 (exact, from PDF content stream); flag
      yellow if Tier 1 disagrees by more than tolerance.
    - Label cells: prefer the Nomenclature-validated form; flag red when
      neither tier maps to a canonical entry.
    - Provenance: track which tier supplied each accepted value
      (``tier0`` | ``tier1`` | ``consensus``).

Conflict reports (§10) are attached to ``CanonicalTable.conflicts`` so the
HITL UI can surface only the disagreements.

Public API:

    reconcile_dual_tier(tier0_tables, tier1_tables) → list[CanonicalTable]
    reconcile_pair(table_a, table_b)               → CanonicalTable
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable
# The ONE definition of "metadata a merge must not throw away" (WP-2.9b).
# Imported, never duplicated: a second copy of this list is a second thing to
# forget when a stage records something new.
from accounting.ensemble import _CARRY_FORWARD_METADATA
from accounting.row_aligner import align_canonical_tables

logger = logging.getLogger(__name__)

# Numeric-equality tolerance in monetary units (Decimal). 1 unit allows for
# rounding differences between native PDF text and OCR digit recognition.
_NUMERIC_TOLERANCE = Decimal("1")

_FLAG_ORDER = {"green": 0, "yellow": 1, "red": 2}


@dataclass
class CellConflict:
    """A single (row, column) where Tier 0 and Tier 1 disagree."""

    row_index: int                 # row index in the merged table
    column_name: str
    tier0_raw: Optional[str]
    tier1_raw: Optional[str]
    tier0_parsed: Optional[Decimal]
    tier1_parsed: Optional[Decimal]
    resolution: str                # tier0 | tier1 | both_missing | label_diff
    flag: str                      # green | yellow | red

    def to_dict(self) -> dict:
        return {
            "row_index": self.row_index,
            "column_name": self.column_name,
            "tier0_raw": self.tier0_raw,
            "tier1_raw": self.tier1_raw,
            "tier0_parsed": (
                str(self.tier0_parsed) if self.tier0_parsed is not None else None
            ),
            "tier1_parsed": (
                str(self.tier1_parsed) if self.tier1_parsed is not None else None
            ),
            "resolution": self.resolution,
            "flag": self.flag,
        }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _first_page(ct: CanonicalTable) -> Optional[int]:
    return ct.page_range[0] if ct.page_range else None


def _match_rank(a: CanonicalTable, b: CanonicalTable) -> int:
    """How well two tables identify as the SAME statement. 0 = incompatible.

    Page evidence outranks type because two tables can share a type and still be
    different statements — a Bilan Actif and a Bilan Passif are both ``bilan``,
    and only the page tells them apart.
    """
    ta = a.statement_type or "unknown"
    tb = b.statement_type or "unknown"
    known_both = ta != "unknown" and tb != "unknown"
    if known_both and ta != tb:
        return 0                              # confidently different statements

    pa, pb = _first_page(a), _first_page(b)
    if pa is not None and pb is not None:
        if pa != pb:
            return 0                          # same type, different page → not the same table
        return 3 if known_both else 2
    # No page evidence on at least one side: type compatibility is all there is.
    return 1


def _pair_by_identity(
    tier0_tables: list[CanonicalTable],
    tier1_tables: list[CanonicalTable],
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Greedily pair tables across tiers by identity.

    Returns ``(pairs, unmatched_tier0, unmatched_tier1)``. Strongest matches are
    taken first so a table with real page evidence claims its counterpart before
    a weaker type-only candidate can. With no page evidence anywhere this
    degrades to the previous in-order behaviour, which is the best available
    when nothing distinguishes two same-typed tables.
    """
    scored: list[tuple[int, int, int]] = []
    for i, a in enumerate(tier0_tables):
        for j, b in enumerate(tier1_tables):
            rank = _match_rank(a, b)
            if rank > 0:
                scored.append((rank, i, j))
    # Best rank first; ties keep list order so the degenerate case is stable.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    pairs: list[tuple[int, int]] = []
    used0: set[int] = set()
    used1: set[int] = set()
    for _rank, i, j in scored:
        if i in used0 or j in used1:
            continue
        pairs.append((i, j))
        used0.add(i)
        used1.add(j)

    pairs.sort()
    return (
        pairs,
        [i for i in range(len(tier0_tables)) if i not in used0],
        [j for j in range(len(tier1_tables)) if j not in used1],
    )


def reconcile_dual_tier(
    tier0_tables: list[CanonicalTable],
    tier1_tables: list[CanonicalTable],
) -> list[CanonicalTable]:
    """Reconcile parallel lists of Tier 0 / Tier 1 CanonicalTables.

    Tables are paired by **identity** — what statement each one is and which
    page it came from — not by list position. Positional pairing is only correct
    while both tiers find the same tables in the same order, and they routinely
    do not: one tier misses a table, or splits one, and every subsequent index
    shifts. The two lists then merge *different statements* cell by cell, which
    is silent and produces plausible-looking wrong numbers.

    Tables that cannot be paired are passed through as-is with
    ``reconciliation == "tier0_only"`` / ``"tier1_only"``.
    """
    if not tier0_tables and not tier1_tables:
        return []
    if not tier0_tables:
        return list(tier1_tables)
    if not tier1_tables:
        return list(tier0_tables)

    pairs, only0, only1 = _pair_by_identity(tier0_tables, tier1_tables)
    merged: list[CanonicalTable] = [
        reconcile_pair(tier0_tables[i], tier1_tables[j]) for i, j in pairs
    ]

    for i in only0:
        extra = tier0_tables[i]
        extra.metadata = {**extra.metadata, "reconciliation": "tier0_only"}
        merged.append(extra)
    for j in only1:
        extra = tier1_tables[j]
        extra.metadata = {**extra.metadata, "reconciliation": "tier1_only"}
        merged.append(extra)

    return merged


def reconcile_pair(
    table_a: CanonicalTable,
    table_b: CanonicalTable,
) -> CanonicalTable:
    """Merge a Tier 0 + Tier 1 pair into a single reconciled CanonicalTable.

    ``table_a`` is treated as Tier 0 (trusted for numbers); ``table_b`` is
    treated as Tier 1.
    """
    alignment = align_canonical_tables(table_a, table_b)

    merged = CanonicalTable(
        statement_type=table_a.statement_type or table_b.statement_type,
        column_model=table_a.column_model or table_b.column_model,
        page_range=table_a.page_range or table_b.page_range,
    )

    conflicts: list[CellConflict] = []

    # Aligned rows — produce one merged row per pair
    for pair in alignment.pairs:
        row_a = table_a.rows[pair.a_index]
        row_b = table_b.rows[pair.b_index]
        merged_row, row_conflicts = _merge_rows(
            row_a, row_b, len(merged.rows), pair.method, pair.confidence
        )
        merged.rows.append(merged_row)
        conflicts.extend(row_conflicts)

    # Tier 0 orphans — keep Tier 0 row, mark provenance
    for a_idx in alignment.a_orphans:
        row = _clone_row_with_provenance(table_a.rows[a_idx], "tier0_only")
        merged.rows.append(row)

    # Tier 1 orphans — keep Tier 1 row, flag yellow (likely OCR artifact)
    for b_idx in alignment.b_orphans:
        row = _clone_row_with_provenance(table_b.rows[b_idx], "tier1_only")
        # Bump cells to yellow — orphan rows on the OCR side are suspect
        for cell in row.cells.values():
            if cell.flag == "green":
                cell.flag = "yellow"
        merged.rows.append(row)

    # Stash conflicts on the merged table
    merged.conflicts = [c.to_dict() for c in conflicts]
    merged.metadata = {
        # Evidence an earlier stage ESTABLISHED about this table — the unit it
        # states (Stage 3b), the proven column profile, the scale resolver's
        # audit trail — is still true of the merged result: this merge
        # reconciles cell VALUES, it does not re-read headers or re-run the
        # resolver. Rebuilding the dict from scratch discarded all of it, so a
        # document that reached the reviewer through the dual-tier path lost the
        # unit the Tier-3 vote path keeps. Same key set as that vote, imported
        # rather than copied so the two can never drift apart, taken from
        # ``table_a`` — the Tier-0 anchor this function is documented to trust.
        **{k: v for k, v in (table_a.metadata or {}).items()
           if k in _CARRY_FORWARD_METADATA},
        "reconciliation": "dual_tier",
        "alignment_coverage": alignment.coverage,
        "conflict_count": len(conflicts),
        "tier0_orphans": len(alignment.a_orphans),
        "tier1_orphans": len(alignment.b_orphans),
    }

    merged.recompute_aggregates()

    logger.info(
        f"Reconciliation: {len(merged.rows)} merged rows "
        f"({len(alignment.pairs)} aligned, {len(alignment.a_orphans)} t0_orphans, "
        f"{len(alignment.b_orphans)} t1_orphans), {len(conflicts)} cell conflicts"
    )
    return merged


# ---------------------------------------------------------------------------
# Row + cell merging
# ---------------------------------------------------------------------------


def _merge_rows(
    row_a: CanonicalRow,
    row_b: CanonicalRow,
    merged_index: int,
    align_method: str,
    align_confidence: float,
) -> tuple[CanonicalRow, list[CellConflict]]:
    """Merge a single (row_a, row_b) pair, returning (merged_row, conflicts)."""

    # Pick the better metadata: prefer the row that has a Nomenclature match.
    # If both match, prefer row_a (Tier 0). If neither, take row_a as base.
    a_has_match = row_a.canonical_term is not None
    b_has_match = row_b.canonical_term is not None

    if a_has_match:
        base = row_a
    elif b_has_match:
        base = row_b
    else:
        base = row_a

    merged = CanonicalRow(
        raw_text=row_a.raw_text or row_b.raw_text,
        canonical_term=base.canonical_term,
        match_type=base.match_type,
        match_confidence=base.match_confidence,
        account_code=base.account_code,
        section=base.section or (row_b.section if base is row_a else row_a.section),
        validation_field=base.validation_field,
        is_subtotal=base.is_subtotal or row_a.is_subtotal or row_b.is_subtotal,
        grouped_from=list(base.grouped_from),
    )

    # Label-side conflict (canonical disagreement)
    label_conflict: list[CellConflict] = []
    if a_has_match and b_has_match and row_a.canonical_term != row_b.canonical_term:
        label_conflict.append(CellConflict(
            row_index=merged_index,
            column_name="<label>",
            tier0_raw=row_a.raw_text,
            tier1_raw=row_b.raw_text,
            tier0_parsed=None,
            tier1_parsed=None,
            resolution="label_diff",
            flag="red",
        ))

    # Cell-by-cell merge — union of column names from both rows
    column_names: list[str] = []
    seen_cols: set[str] = set()
    for col in list(row_a.cells.keys()) + list(row_b.cells.keys()):
        if col not in seen_cols:
            seen_cols.add(col)
            column_names.append(col)

    cell_conflicts: list[CellConflict] = []

    for col in column_names:
        cell_a = row_a.cells.get(col)
        cell_b = row_b.cells.get(col)
        merged_cell, conflict = _merge_cells(
            cell_a, cell_b, merged_index, col, align_method, align_confidence
        )
        merged.cells[col] = merged_cell
        if conflict is not None:
            cell_conflicts.append(conflict)

    return merged, label_conflict + cell_conflicts


def _merge_cells(
    cell_a: Optional[CanonicalCell],
    cell_b: Optional[CanonicalCell],
    row_index: int,
    column_name: str,
    align_method: str,
    align_confidence: float,
) -> tuple[CanonicalCell, Optional[CellConflict]]:
    """Merge a single cell pair following §10 rules."""

    # Both missing
    if cell_a is None and cell_b is None:
        return CanonicalCell(raw_value="", provenance="consensus", flag="green"), None

    # One side missing — pass through, flag yellow if alignment was weak
    if cell_a is None:
        cell = CanonicalCell(
            raw_value=cell_b.raw_value,
            parsed_value=cell_b.parsed_value,
            provenance="tier1_only",
            flag="yellow" if align_method == "positional" else cell_b.flag,
            confidence=cell_b.confidence,
        )
        return cell, None
    if cell_b is None:
        cell = CanonicalCell(
            raw_value=cell_a.raw_value,
            parsed_value=cell_a.parsed_value,
            provenance="tier0_only",
            flag="yellow" if align_method == "positional" else cell_a.flag,
            confidence=cell_a.confidence,
        )
        return cell, None

    # Both present — apply numeric vs label rules
    a_num = cell_a.parsed_value
    b_num = cell_b.parsed_value

    if a_num is not None and b_num is not None:
        diff = abs(a_num - b_num)
        within_tol = diff <= _NUMERIC_TOLERANCE

        # Trust Tier 0 (exact, from PDF content stream)
        cell = CanonicalCell(
            raw_value=cell_a.raw_value,
            parsed_value=a_num,
            provenance="consensus" if within_tol else "tier0",
            flag="green" if within_tol else "yellow",
            confidence=1.0 if within_tol else 0.85,
        )
        if not within_tol:
            return cell, CellConflict(
                row_index=row_index,
                column_name=column_name,
                tier0_raw=cell_a.raw_value,
                tier1_raw=cell_b.raw_value,
                tier0_parsed=a_num,
                tier1_parsed=b_num,
                resolution="tier0",
                flag="yellow",
            )
        return cell, None

    # Tier 0 has number, Tier 1 missed → trust Tier 0
    if a_num is not None and b_num is None:
        return (
            CanonicalCell(
                raw_value=cell_a.raw_value,
                parsed_value=a_num,
                provenance="tier0",
                flag="green",
                confidence=1.0,
            ),
            None,
        )

    # Tier 0 missed parsing, Tier 1 got it → take Tier 1 but flag yellow
    if a_num is None and b_num is not None:
        return (
            CanonicalCell(
                raw_value=cell_b.raw_value,
                parsed_value=b_num,
                provenance="tier1",
                flag="yellow",
                confidence=cell_b.confidence,
            ),
            CellConflict(
                row_index=row_index,
                column_name=column_name,
                tier0_raw=cell_a.raw_value,
                tier1_raw=cell_b.raw_value,
                tier0_parsed=None,
                tier1_parsed=b_num,
                resolution="tier1",
                flag="yellow",
            ),
        )

    # Both non-numeric — string compare
    if (cell_a.raw_value or "").strip() == (cell_b.raw_value or "").strip():
        return (
            CanonicalCell(
                raw_value=cell_a.raw_value,
                parsed_value=None,
                provenance="consensus",
                flag="green",
                confidence=1.0,
            ),
            None,
        )

    # Strings differ — keep Tier 0, flag yellow
    return (
        CanonicalCell(
            raw_value=cell_a.raw_value,
            parsed_value=None,
            provenance="tier0",
            flag="yellow",
            confidence=0.80,
        ),
        CellConflict(
            row_index=row_index,
            column_name=column_name,
            tier0_raw=cell_a.raw_value,
            tier1_raw=cell_b.raw_value,
            tier0_parsed=None,
            tier1_parsed=None,
            resolution="tier0",
            flag="yellow",
        ),
    )


def _clone_row_with_provenance(row: CanonicalRow, provenance: str) -> CanonicalRow:
    """Shallow-clone a row, retagging cell provenance for orphan tracking."""
    cloned_cells = {
        name: CanonicalCell(
            raw_value=c.raw_value,
            parsed_value=c.parsed_value,
            provenance=provenance,
            flag=c.flag,
            confidence=c.confidence,
        )
        for name, c in row.cells.items()
    }
    return CanonicalRow(
        raw_text=row.raw_text,
        canonical_term=row.canonical_term,
        match_type=row.match_type,
        match_confidence=row.match_confidence,
        account_code=row.account_code,
        section=row.section,
        validation_field=row.validation_field,
        is_subtotal=row.is_subtotal,
        cells=cloned_cells,
        grouped_from=list(row.grouped_from),
    )
