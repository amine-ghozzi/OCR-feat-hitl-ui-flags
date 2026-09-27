"""
Canonical output model — §26 of the nomenclature revamp plan.

Every extracted row flows through progressive enrichment:

    Stage 0: raw list-of-lists              (existing ``tables`` field)
        ↓
    Stage 5: numbers parsed                 (``CanonicalCell.parsed_value``)
        ↓
    Stage 6: labels matched                 (``CanonicalRow.canonical_term``)
        ↓
    Stage 8: subtotals detected             (``CanonicalRow.is_subtotal``)
        ↓
    Stage 9: statement classified           (``CanonicalTable.statement_type``)
        ↓
    Stage 10: flags assigned                (all cells ``flag`` = green/yellow/red)

These dataclasses are additive — nothing in the legacy pipeline depends on
them, and ``ExtractionResult.canonical_tables`` defaults to ``[]`` so existing
code paths remain unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional


_FLAG_ORDER = {"green": 0, "yellow": 1, "red": 2}


@dataclass
class CanonicalCell:
    """Single cell with parsed value + provenance + flag.

    ``raw_value`` is always the original text from the extractor; ``parsed_value``
    is the ``Decimal`` emitted by :func:`accounting.french_number.parse_french_number`
    (or ``None`` for non-numeric cells).
    """

    raw_value: str
    parsed_value: Optional[Decimal] = None
    provenance: str = "tier1"            # tier0 | tier1 | tier2 | consensus | hitl
    flag: str = "green"                  # green | yellow | red
    confidence: float = 1.0
    # How the number was parsed (see french_number.parse_french_number_with_basis).
    # A basis in ``ASSUMPTION_BASES`` means the value's scale rests on a corpus
    # assumption / per-document inference — surfaced in the HITL review UI.
    basis: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_value": self.raw_value,
            "parsed_value": (
                str(self.parsed_value) if self.parsed_value is not None else None
            ),
            "provenance": self.provenance,
            "flag": self.flag,
            "confidence": self.confidence,
            "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CanonicalCell":
        """Inverse of :meth:`to_dict` — F3: the API-mode UI rehydrates the worker's
        serialized result back into typed cells (Decimal restored from str)."""
        pv = d.get("parsed_value")
        return cls(
            raw_value=d.get("raw_value", "") or "",
            parsed_value=Decimal(str(pv)) if pv is not None else None,
            provenance=d.get("provenance", "tier1"),
            flag=d.get("flag", "green"),
            confidence=float(d.get("confidence", 1.0)),
            basis=d.get("basis"),
        )


@dataclass
class CanonicalRow:
    """Single row mapped against the Nomenclature (§5)."""

    raw_text: str
    canonical_term: Optional[str] = None
    match_type: str = "unrecognized"     # exact | fuzzy | resolved | custom | grouped | unrecognized
    match_confidence: float = 0.0
    account_code: Optional[str] = None
    section: Optional[str] = None
    validation_field: Optional[str] = None
    is_subtotal: bool = False
    # ISSUE-7b: True for an empty section-header row (e.g. "IMMOBILISATIONS
    # CORPORELLES" with all value cells blank). Such rows are NOT promoted to a
    # canonical leaf, so they cannot duplicate a real data row of the same label.
    is_header: bool = False
    # column_name → cell (e.g. ``{"brut": …, "amortissement": …, "net": …, "n_minus_1": …}``)
    cells: dict[str, CanonicalCell] = field(default_factory=dict)
    # For ``match_type == "grouped"``: which entries were aggregated
    grouped_from: list[str] = field(default_factory=list)

    @property
    def flag(self) -> str:
        """Worst flag across this row's cells (empty → green)."""
        if not self.cells:
            return "green"
        worst = max(self.cells.values(), key=lambda c: _FLAG_ORDER.get(c.flag, 0))
        return worst.flag

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "canonical_term": self.canonical_term,
            "match_type": self.match_type,
            "match_confidence": self.match_confidence,
            "account_code": self.account_code,
            "section": self.section,
            "validation_field": self.validation_field,
            "is_subtotal": self.is_subtotal,
            "is_header": self.is_header,
            "grouped_from": self.grouped_from,
            "cells": {name: cell.to_dict() for name, cell in self.cells.items()},
            "flag": self.flag,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CanonicalRow":
        """Inverse of :meth:`to_dict` (``flag`` is derived, not restored)."""
        return cls(
            raw_text=d.get("raw_text", "") or "",
            canonical_term=d.get("canonical_term"),
            match_type=d.get("match_type", "unrecognized"),
            match_confidence=float(d.get("match_confidence", 0.0)),
            account_code=d.get("account_code"),
            section=d.get("section"),
            validation_field=d.get("validation_field"),
            is_subtotal=bool(d.get("is_subtotal", False)),
            is_header=bool(d.get("is_header", False)),
            grouped_from=list(d.get("grouped_from") or []),
            cells={name: CanonicalCell.from_dict(c)
                   for name, c in (d.get("cells") or {}).items()},
        )


@dataclass
class CanonicalTable:
    """Full table — classified + enriched.

    ``statement_type`` uses the §24 vocabulary:
        ``bilan_actif`` | ``bilan_passif`` | ``bilan`` | ``compte_resultat``
        | ``flux_tresorerie`` | ``unknown``
    """

    statement_type: str = "unknown"
    column_model: Optional[str] = None
    rows: list[CanonicalRow] = field(default_factory=list)
    match_rate: float = 0.0
    custom_row_count: int = 0
    overall_flag: str = "green"
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    page_range: Optional[tuple[int, int]] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def recompute_aggregates(self) -> None:
        """Refresh ``match_rate``, ``custom_row_count``, ``overall_flag`` from rows."""
        if not self.rows:
            self.match_rate = 0.0
            self.custom_row_count = 0
            self.overall_flag = "green"
            return
        matched = sum(
            1 for r in self.rows if r.match_type in ("exact", "fuzzy", "resolved")
        )
        self.match_rate = matched / len(self.rows)
        self.custom_row_count = sum(
            1 for r in self.rows if r.match_type in ("custom", "grouped")
        )
        self.overall_flag = max(
            (r.flag for r in self.rows), key=lambda f: _FLAG_ORDER.get(f, 0)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_type": self.statement_type,
            "column_model": self.column_model,
            "rows": [r.to_dict() for r in self.rows],
            "match_rate": self.match_rate,
            "custom_row_count": self.custom_row_count,
            "overall_flag": self.overall_flag,
            "conflicts": self.conflicts,
            "page_range": list(self.page_range) if self.page_range else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CanonicalTable":
        """Inverse of :meth:`to_dict` — F3: lets the API-mode HITL UI rebuild
        typed canonical tables (flags, bases, Decimal parsed values) from the
        worker's JSON, so the flag grid and the dual validation pass behave
        identically in-process and through the dockerized backend."""
        pr = d.get("page_range")
        ct = cls(
            statement_type=d.get("statement_type", "unknown"),
            column_model=d.get("column_model"),
            rows=[CanonicalRow.from_dict(r) for r in (d.get("rows") or [])],
            conflicts=list(d.get("conflicts") or []),
            page_range=tuple(pr) if pr else None,
            metadata=dict(d.get("metadata") or {}),
        )
        ct.recompute_aggregates()
        return ct
