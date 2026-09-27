"""
Statement Builder — the authoritative post-OCR classification stage.

The pipeline's unit of work has always been *a DataFrame at a list index*, with
no structure representing a financial *statement*. Classification happened in
five independent places over five different text projections, none of them
reconciled, and the only one that ran before OCR threw its answer away. This
module makes it one explicit stage that runs AFTER extraction and produces a
single inventory of what the document actually contains.

    build_statements(tables, provenance=…, dictionary=…) → list[StatementUnit]

Each statement carries the evidence behind its classification and an explicit
gate verdict, so an unrecognized statement can be *stopped* rather than silently
defaulted. Today's `unknown` does the opposite: it widens the nomenclature
matcher to the full dictionary, giving the least-recognizable table the most
permissive matching.

Scope note — a statement currently holds exactly one fragment. The ``Fragment``
model is shaped for the multi-page merge (a statement continued across a page
break) and for intra-table splits, but neither is wired here: merging needs the
continuation detector hardened first, since on real documents it scores a Bilan
Passif → Compte de Résultat pair identically to a genuine continuation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Gate outcomes.
GATE_PASS = "pass"
GATE_QUARANTINED = "quarantined"


@dataclass
class Fragment:
    """One extracted table (or, later, a row-range of one) inside a statement."""

    table_index: int                       # back-pointer into the tier's table list
    df: pd.DataFrame = field(repr=False)
    page_no: Optional[int] = None
    row_slice: Optional[tuple[int, int]] = None   # None = the whole frame
    col_x_offsets: list[float] = field(default_factory=list)


@dataclass
class StatementUnit:
    """One financial statement found in the document."""

    id: int
    statement_type: Optional[str] = None   # None → unclassified
    sub_type: Optional[str] = None         # actif | passif
    confidence: float = 0.0
    margin: int = 0
    gate: str = GATE_PASS
    gate_reason: Optional[str] = None
    fragments: list[Fragment] = field(default_factory=list, repr=False)
    column_profile: Optional[str] = None
    evidence: dict = field(default_factory=dict)

    @property
    def table_indices(self) -> list[int]:
        return [f.table_index for f in self.fragments]

    @property
    def pages(self) -> list[int]:
        return [f.page_no for f in self.fragments if f.page_no is not None]

    @property
    def qualified_type(self) -> Optional[str]:
        """``bilan_actif`` / ``bilan_passif`` when the side is known — the
        vocabulary the YAML ``column_models[*].applies_to`` keys on."""
        if self.statement_type == "bilan" and self.sub_type:
            return f"bilan_{self.sub_type}"
        return self.statement_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.statement_type,
            "sub_type": self.sub_type,
            "qualified_type": self.qualified_type,
            "confidence": self.confidence,
            "margin": self.margin,
            "gate": self.gate,
            "gate_reason": self.gate_reason,
            "table_indices": self.table_indices,
            "pages": self.pages,
            "column_profile": self.column_profile,
            "evidence": self.evidence,
        }


def _frame_labels(df: pd.DataFrame) -> list[str]:
    """Label-ish text for classification. Delegates to the segmenter's collector
    so both paths read a table the same way."""
    try:
        from accounting.statement_segmenter import _collect_table_labels
        return _collect_table_labels(df)
    except Exception:  # pragma: no cover - defensive
        return [str(v) for v in df.iloc[:, 0]] if len(df.columns) else []


def build_statements(
    tables: list[pd.DataFrame],
    provenance: Optional[list[dict]] = None,
    dictionary=None,
) -> list[StatementUnit]:
    """Inventory the statements in a document.

    Args:
        tables: the extracted frames, in order.
        provenance: optional per-table ``{"page_no", "x_offsets"}``, index-aligned
            with ``tables``. Ignored when its length disagrees with ``tables`` —
            a mismatched mapping would attribute pages to the wrong statements,
            which is worse than having none.
        dictionary: NomenclatureDictionary (loaded lazily when omitted).

    Returns:
        One ``StatementUnit`` per non-empty frame. An empty frame yields no
        statement at all — a blank page is not an unclassified statement, and
        quarantining it would put an empty grid in front of a reviewer.
    """
    if not tables:
        return []

    if dictionary is None:
        try:
            from accounting.nomenclature import load_default_dictionary
            dictionary = load_default_dictionary()
        except ImportError:
            logger.warning("Nomenclature unavailable — cannot build statements")
            return []

    if provenance is not None and len(provenance) != len(tables):
        logger.debug(
            "Statement builder: provenance length %d != table count %d — ignoring",
            len(provenance), len(tables),
        )
        provenance = None

    units: list[StatementUnit] = []
    for table_idx, df in enumerate(tables):
        if df is None or df.empty:
            continue

        meta = (provenance[table_idx] if provenance else None) or {}
        fragment = Fragment(
            table_index=table_idx,
            df=df,
            page_no=meta.get("page_no"),
            col_x_offsets=list(meta.get("x_offsets") or []),
        )

        labels = _frame_labels(df)
        classification = dictionary.classify_statement_detailed(labels)
        passed = classification.passes_gate

        unit = StatementUnit(
            id=len(units),
            statement_type=classification.statement_type if passed else None,
            sub_type=classification.sub_type if passed else None,
            confidence=classification.confidence,
            margin=classification.margin,
            gate=GATE_PASS if passed else GATE_QUARANTINED,
            gate_reason=classification.gate_reason,
            fragments=[fragment],
            evidence={
                "scores": classification.scores,
                "term_scores": classification.term_scores,
                "booster_scores": classification.booster_scores,
                "winner": classification.winner,
                "runner_up": classification.runner_up,
                # What the legacy classifier would have said — kept so a
                # quarantine decision can be audited against the old behaviour.
                "legacy_type": classification.statement_type,
            },
        )
        units.append(unit)

    units = _merge_continuations(units, dictionary)

    quarantined = [u for u in units if u.gate == GATE_QUARANTINED]
    logger.info(
        "Statement builder: %d statement(s) — %s%s",
        len(units),
        ", ".join(str(u.qualified_type) for u in units if u.gate == GATE_PASS) or "none classified",
        f"; {len(quarantined)} quarantined" if quarantined else "",
    )
    return units


def _is_continuation(prev: StatementUnit, nxt: StatementUnit, dictionary) -> bool:
    """Is ``nxt`` the same statement as ``prev``, continued?

    Deliberately narrow. Type agreement alone is nowhere near enough: on a real
    accounts pack the notes that elaborate the balance sheet classify as
    ``bilan_actif`` too, because they use its vocabulary — one document yields
    26 of them. Folding those together would be far worse than leaving them
    separate. So a merge additionally requires:

      * both sides classified (a quarantined fragment is never absorbed —
        it must stay visible for the reviewer),
      * consecutive PAGES, so a note twenty pages later cannot attach, and
      * the continuation detector's own verdict, which weighs the repeated
        header and open-section signals that actually separate "continued" from
        "a new table about the same subject".
    """
    if prev.gate != GATE_PASS or nxt.gate != GATE_PASS:
        return False
    if prev.qualified_type != nxt.qualified_type:
        return False

    prev_pages, next_pages = prev.pages, nxt.pages
    if not prev_pages or not next_pages:
        return False                      # no page evidence → do not guess
    if next_pages[-1] - prev_pages[-1] not in (0, 1):
        return False

    try:
        from accounting.table_merger import PageTable, detect_continuation
        a = PageTable(df=prev.fragments[-1].df, page_index=prev_pages[-1],
                      col_x_offsets=prev.fragments[-1].col_x_offsets)
        b = PageTable(df=nxt.fragments[0].df, page_index=next_pages[0],
                      col_x_offsets=nxt.fragments[0].col_x_offsets)
        return detect_continuation(a, b, dictionary).is_continuation
    except Exception:  # pragma: no cover - never break the inventory
        return False


def _merge_continuations(units: list[StatementUnit], dictionary) -> list[StatementUnit]:
    """Fold continuation fragments into the statement they continue.

    Only LIST-adjacent units are considered. The voted table order does not
    follow page order (a real pack yields pages 6, 7, 19, 5, …), so sorting by
    page to find candidates would reorder the inventory and break the
    positional alignment between statements and canonical tables. A genuine
    continuation is emitted consecutively in practice.
    """
    if len(units) < 2:
        return units

    merged: list[StatementUnit] = [units[0]]
    for unit in units[1:]:
        prev = merged[-1]
        if _is_continuation(prev, unit, dictionary):
            prev.fragments.extend(unit.fragments)
            logger.info(
                "Statement builder: folded table %s into statement #%d (%s, pages %s)",
                unit.table_indices, prev.id, prev.qualified_type, prev.pages,
            )
        else:
            merged.append(unit)

    for i, unit in enumerate(merged):
        unit.id = i
    return merged


def statements_to_dicts(units: list[StatementUnit]) -> list[dict[str, Any]]:
    """Serialize for ``ExtractionResult.metadata`` / the stored job result."""
    return [u.to_dict() for u in units]
