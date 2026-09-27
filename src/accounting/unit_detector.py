"""
Unit / scale binding — VAL-7, decision E-2.

A Tunisian financial statement states the unit its figures are written in
("(en DT)", "en KDT", "en milliers de dinars", "en mDT") in a table header or in
the caption line that sits above the table on the page. Until now that text was
read by nobody: the unit was never bound to the table, the caption text was not
retained, and a document that states "(en DT)" on some pages but not on others
left every consumer guessing.

**Decision E-2 (A) — record and flag, never rescale.** This module *reads* the
unit and records it on the table; it never touches a ``parsed_value`` and never
multiplies or divides anything. Where two tables of one document disagree, the
disagreement is flagged (``metadata["unit_conflict"]``) and surfaced — it is a
question for a human, not a licence for the pipeline to "correct" figures.

Public API::

    detect_unit(df, caption_text=None)  -> dict | None   # one table
    resolve_document_default(units)     -> (dict | None, conflict)
    document_unit(canonical_tables)     -> dict | None   # read back, doc level
    match_unit(text)                    -> (value, confidence_multiplier) | None

``detect_unit`` returns ``{"value": "DT"|"KDT"|"MDT", "source": "header"|
"caption", "confidence": float}`` or ``None`` when the table states no unit.
``enrichment_pipeline`` adds the third source, ``"document_default"``, when it
propagates one table's unit to a sibling that states none.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The unit vocabulary. ``DT`` = dinar, ``KDT`` = thousands of dinars,
#: ``MDT`` = millions of dinars. Deliberately small: a value outside this set is
#: not a scale this pipeline can reason about, so it is not recorded at all.
UNIT_VALUES: tuple[str, ...] = ("DT", "KDT", "MDT")

#: Where a unit may come from, weakest last.
UNIT_SOURCES: tuple[str, ...] = ("header", "caption", "document_default")

#: How much of the table's own text is treated as "header". A unit statement
#: that has sunk below the third row is a data row, not a header.
_HEADER_ROWS = 3

#: Confidence per source. A unit written in the table itself is stronger than
#: one read off the page around it, which is stronger than one inherited from a
#: sibling table.
SOURCE_CONFIDENCE = {"header": 0.90, "caption": 0.80, "document_default": 0.60}

#: ``mDT`` is the ambiguous spelling. Known-Gaps D1 states the risk precisely —
#: "a statement headed ``(en mDT)`` read as though it were ``(en DT)`` is wrong
#: by a factor of 1000" — so in this corpus lower-case *m* is *milliers*, i.e.
#: KDT, not millions. All-caps ``MDT`` is read as millions; any other casing
#: takes the D1 reading (KDT) at reduced confidence, so a reviewer sees it is a
#: reading rather than a fact and an explicit spelling elsewhere outranks it.
_AMBIGUOUS_MDT_CONFIDENCE = 0.60

# --- patterns --------------------------------------------------------------
# The EARLIEST match in the text wins (see ``match_unit``); this ordering only
# breaks a tie at the same position, so the longer, more specific spellings are
# listed before the bare tokens contained inside them. Every pattern is applied
# to text that has been lower-cased, de-accented and whitespace-collapsed.
_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    # thousands ------------------------------------------------------------
    (re.compile(r"\bmilliers?\s+de\s+dinars?\b"), "KDT", False),
    (re.compile(r"\bmille\s+dinars?\b"), "KDT", False),
    (re.compile(r"\bmilliers?\s+de\s+dt\b"), "KDT", False),
    (re.compile(r"\bkdt\b"), "KDT", False),
    (re.compile(r"\bk\s?dinars?\b"), "KDT", False),
    # millions -------------------------------------------------------------
    (re.compile(r"\bmillions?\s+de\s+dinars?\b"), "MDT", False),
    (re.compile(r"\bmillions?\s+de\s+dt\b"), "MDT", False),
    # ``mdt`` — resolved against the ORIGINAL casing (see ``_resolve_mdt``).
    # The value here is the Known-Gaps D1 default; the flag (3rd item) is what
    # marks the hit for that pass and for the reduced confidence.
    (re.compile(r"\bmdt\b"), "KDT", True),
    # dinars ---------------------------------------------------------------
    (re.compile(r"\bdinars?\s+tunisiens?\b"), "DT", False),
    (re.compile(r"\ben\s+dinars?\b"), "DT", False),
    (re.compile(r"\bdt\b"), "DT", False),
    (re.compile(r"\btnd\b"), "DT", False),
)

_ACCENTS = str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc")


def _norm_pair(text: Any) -> tuple[str, str]:
    """``(lowercased, case-preserved)`` normalisations with ALIGNED offsets.

    Both go through the same de-accenting and whitespace collapse, so an offset
    found in one indexes the same characters in the other — which is how an
    ``mdt`` hit is re-read against its original casing.
    """
    s = str(text or "")
    s = s.replace("\u00a0", " ").replace("\u202f", " ")
    s = s.translate(_ACCENTS)
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower(), s


def _norm(text: Any) -> str:
    """Lower-case, de-accent, and collapse whitespace (NBSP included)."""
    return _norm_pair(text)[0]


def _resolve_mdt(spelling: str) -> str:
    """``mDT`` -> thousands, ``MDT`` -> millions.

    Known-Gaps D1: "a statement headed ``(en mDT)`` read as though it were
    ``(en DT)`` is wrong by a factor of 1000" — a factor of 1000 is *milliers*,
    so in this corpus a lower-case ``m`` is thousands. All-caps is the millions
    spelling. Any other casing takes the D1 reading, at reduced confidence.
    """
    return "MDT" if spelling.isupper() else "KDT"


def match_unit(text: Any) -> Optional[tuple[str, float]]:
    """The unit stated in ``text``, as ``(value, confidence_multiplier)``.

    ``None`` when the text states no unit this module recognises. The multiplier
    is 1.0 for an unambiguous spelling and lower for ``mDT`` (see
    ``_AMBIGUOUS_MDT_CONFIDENCE``); the caller scales its source confidence by
    it, so an ambiguous spelling never outranks an explicit one.

    The EARLIEST match wins, not the first pattern in the list. A statement
    announces its unit at the top — "Bilan arrêté au 31 décembre 2024 (en DT)" —
    while "millions de dinars" three pages later is prose in a note. Ranking by
    pattern order instead would let that note redefine the whole document's
    scale, which is precisely the silent 1000x error E-2 exists to prevent.
    Pattern order breaks a tie at the same position, so the longer, more
    specific spelling still wins over the bare ``dt`` inside it.
    """
    s, s_cased = _norm_pair(text)
    if not s:
        return None
    best: Optional[tuple[int, int, int, str, bool]] = None
    for order, (pattern, value, ambiguous) in enumerate(_PATTERNS):
        m = pattern.search(s)
        if m is None:
            continue
        key = (m.start(), order, m.end(), value, ambiguous)
        if best is None or key[:2] < best[:2]:
            best = key
    if best is None:
        return None
    start, _order, end, value, ambiguous = best
    if ambiguous:
        # Re-read the hit in its ORIGINAL casing: mDT is thousands, MDT millions.
        value = _resolve_mdt(s_cased[start:end])
    return value, (_AMBIGUOUS_MDT_CONFIDENCE if ambiguous else 1.0)


def _table_text(df) -> list[str]:
    """The table's own header text: the column names plus the first rows.

    Tier 0 frequently emits a frame whose real header has become row 0 (the
    column names are then ``0, 1, 2…``), so both are read.
    """
    out: list[str] = []
    try:
        out.extend(str(c) for c in df.columns)
    except Exception:  # noqa: BLE001 — a frame-like object without .columns
        return out
    try:
        for row_idx in range(min(_HEADER_ROWS, len(df))):
            for col_idx in range(len(df.columns)):
                out.append(str(df.iloc[row_idx, col_idx]))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"unit_detector: header row scan skipped — {exc}")
    return out


def detect_unit(df, caption_text: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The unit this table states, or ``None``.

    Args:
        df: the table's DataFrame. Its column names and first ``_HEADER_ROWS``
            rows are read — nothing else, and nothing is written.
        caption_text: the text of the page the table sits on, when the tier can
            supply it. Only consulted when the table itself states no unit.

    Returns:
        ``{"value": "DT"|"KDT"|"MDT", "source": "header"|"caption",
        "confidence": float}`` or ``None``.

    E-2: the return value is a RECORD. No caller may use it to rescale a value.
    """
    if df is None:
        return None

    for cell in _table_text(df):
        hit = match_unit(cell)
        if hit:
            value, mult = hit
            return {
                "value": value,
                "source": "header",
                "confidence": round(SOURCE_CONFIDENCE["header"] * mult, 3),
            }

    if caption_text:
        hit = match_unit(caption_text)
        if hit:
            value, mult = hit
            return {
                "value": value,
                "source": "caption",
                "confidence": round(SOURCE_CONFIDENCE["caption"] * mult, 3),
            }
    return None


def document_unit(canonical_tables) -> Optional[dict[str, Any]]:
    """The document-level unit, read back off the enriched tables.

    ``{"value": …, "source": …, "conflict": bool}`` or ``None`` when no table of
    the document states a unit. The value is the majority reading (ties broken
    by summed confidence), ``source`` the strongest source that supports it, and
    ``conflict`` true when the document's tables do not agree — which is exactly
    the D1 gap this package closes, and exactly the condition under which a
    human, not the pipeline, decides what the figures mean.
    """
    tally: dict[str, float] = {}
    counts: dict[str, int] = {}
    best_source: dict[str, str] = {}
    # Inherited units are tallied separately: they must not vote (they ARE the
    # document default — counting them would let it re-elect itself and drown a
    # minority table that genuinely states something else). But when inheritance
    # is ALL the document has — a tier that could only offer document-level text
    # — the default is still the document's unit and must be reported.
    inherited: dict[str, int] = {}
    conflict = False

    for ct in (canonical_tables or []):
        if ct is None:
            continue
        # A CanonicalTable, or the dict it round-trips through (``to_dict``).
        meta = (ct.get("metadata") if isinstance(ct, dict)
                else getattr(ct, "metadata", None)) or {}
        if meta.get("unit_conflict"):
            conflict = True
        unit = meta.get("unit")
        if not isinstance(unit, dict):
            continue
        value = unit.get("value")
        if value not in UNIT_VALUES:
            continue
        if unit.get("source") == "document_default":
            inherited[value] = inherited.get(value, 0) + 1
            continue
        counts[value] = counts.get(value, 0) + 1
        tally[value] = tally.get(value, 0.0) + float(unit.get("confidence") or 0.0)
        src = str(unit.get("source") or "")
        if src in UNIT_SOURCES:
            prior = best_source.get(value)
            if prior is None or UNIT_SOURCES.index(src) < UNIT_SOURCES.index(prior):
                best_source[value] = src

    if not counts:
        if not inherited:
            return None
        value = max(inherited, key=lambda v: inherited[v])
        return {"value": value, "source": "document_default",
                "conflict": conflict or len(inherited) > 1}
    if len(counts) > 1:
        conflict = True
    value = max(counts, key=lambda v: (counts[v], tally[v]))
    return {"value": value, "source": best_source.get(value, "header"), "conflict": conflict}


def resolve_document_default(
    units: list[Optional[dict[str, Any]]],
) -> tuple[Optional[dict[str, Any]], bool]:
    """Majority vote over per-table units → ``(default_unit, conflict)``.

    ``default_unit`` is the unit a table that states none should inherit
    (``source`` rewritten to ``"document_default"``); ``conflict`` is true when
    the stated units disagree. E-2: a conflict is flagged, never reconciled by
    rescaling.
    """
    counts: dict[str, int] = {}
    tally: dict[str, float] = {}
    for unit in units:
        if not isinstance(unit, dict):
            continue
        value = unit.get("value")
        if value not in UNIT_VALUES:
            continue
        counts[value] = counts.get(value, 0) + 1
        tally[value] = tally.get(value, 0.0) + float(unit.get("confidence") or 0.0)

    if not counts:
        return None, False
    conflict = len(counts) > 1
    value = max(counts, key=lambda v: (counts[v], tally[v]))
    default = {
        "value": value,
        "source": "document_default",
        "confidence": SOURCE_CONFIDENCE["document_default"],
    }
    return default, conflict
