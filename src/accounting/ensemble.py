"""
N-way Ensemble Voting — Tier 3 LV-ROVER + Consensus Entropy.

Generalizes the 2-way dual-tier reconciliation (:mod:`accounting.reconciliation`)
to a confidence-weighted vote across *N* engine outputs (Tier 0 native, Tier 1
OCR, Tier 2 VLM, …). This is the structured-table analog of ROVER (Recognizer
Output Voting Error Reduction, Fiscus 1997) with two upgrades grounded in recent
literature:

    LV-ROVER (arXiv 1707.07432) — the winning *label* is verified against the
        domain lexicon (NomenclatureDictionary); an agreed-upon-but-invalid term
        is rejected in favour of the next-best verified candidate.

    Consensus Entropy (arXiv 2504.11101, 2025) — a label-free, per-cell agreement
        metric that drives a table quality score ``Q_table = 1 − mean(δ)`` and
        cell flags, with no ground truth required.

Design notes
------------
* **Alignment** reuses :func:`accounting.row_aligner.align_canonical_tables`
  against an *anchor* table (the highest-weight source). Optimal N-way alignment
  is NP-complete (``O(L^N)``); ROVER itself uses this greedy align-to-reference
  approximation. Orphan rows on non-anchor sources are appended as new voting
  groups so nothing is silently dropped (§20).

* **Selection vs confidence are separate.** The *value* kept is chosen by ROVER
  score (numeric: tolerance clustering by total vote weight; label: ROVER ``S(w)``
  + lexicon check). The *flag/quality* comes from the consensus divergence ``δ``.

* **δ (consensus divergence) ∈ [0, 1].** For *label/text* cells δ is the medoid's
  mean pairwise normalized Levenshtein distance to the other candidates
  (:func:`consensus_entropy`). For *numeric* cells δ = ``1 − winning_weight/total``
  (so "1500" vs "1500.00" parse equal → δ = 0, while "1500" vs "1490" is flagged).
  We use mean-pairwise-distance rather than the paper's per-candidate softmax
  entropy because that entropy is identically 0 at K = 2 — exactly our dominant
  Tier0+Tier1 case — and so cannot separate agreement from disagreement there.

Public API
----------
    ensemble_vote(sources, …)            → list[CanonicalTable]
    consensus_entropy(candidates, …)     → (medoid_index, delta)
    canonical_tables_to_records(tables)  → list[list[dict]]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional

from rapidfuzz.distance import Levenshtein

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable
from accounting.row_aligner import align_canonical_tables

logger = logging.getLogger(__name__)

# Numeric-equality tolerance in monetary units (matches reconciliation.py).
_NUMERIC_TOLERANCE = Decimal("1")

_FLAG_ORDER = {"green": 0, "yellow": 1, "red": 2}

# Table-level facts established BEFORE the vote that remain true after it. The
# vote reconciles cell values across engines; it does not re-bind columns, re-run
# the scale resolver, or re-measure page geometry, so these must survive it.
_CARRY_FORWARD_METADATA = frozenset({
    "column_profile",        # the profile whose arithmetic rule was proven
    "arithmetic_rule",
    "arithmetic_holds",
    "arithmetic_checked",
    "x_offsets",             # column geometry, for continuation detection
    "column_confidence",
    "scale_resolutions",     # Stage 8b audit trail, surfaced in the HITL
    "resolution_iterations",
    "unit",                  # Stage 3b: the unit the table states (VAL-7/E-2)
    "unit_conflict",         # …and whether the document's tables disagree
    "dot_millimes_evidence",  # VAL-8: how much of the table/document is typed
                              # "14 267 969.725" — a measurement, never a rescale
    "footing_results",       # RTE-7: Stage 8 footing verdicts, green-flag G4
})

# Default trust priors per tier (native > OCR > VLM).
_DEFAULT_WEIGHTS = {"tier0": 1.0, "tier1": 0.8, "tier2": 0.7}

# CE-divergence → flag thresholds.
_DELTA_GREEN = 0.10   # δ ≤ this AND ≥2 sources agree → green
_DELTA_RED = 0.50     # δ ≥ this → red (no consensus)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EnsembleSource:
    """One engine's output plus its trust prior."""

    name: str                       # "tier0" | "tier1" | "tier2" | …
    tables: list[CanonicalTable]
    weight: float = 1.0


@dataclass
class CellVote:
    """The candidate set for a single contested cell — passed to the arbiter hook."""

    row_index: int
    column_name: str
    candidates: list[dict[str, Any]]   # [{source, raw, parsed, weight, confidence}]
    winner_parsed: Optional[Decimal]
    delta: float


# ---------------------------------------------------------------------------
# Consensus Entropy (arXiv 2504.11101) — robust K≥2 variant
# ---------------------------------------------------------------------------


def consensus_entropy(candidates: list[str], beta: float = 1.0) -> tuple[int, float]:
    """Consensus divergence over candidate strings.

    Returns ``(medoid_index, delta)`` where:
        * ``medoid_index`` is the candidate minimizing mean normalized
          Levenshtein distance to the others (the most "central" / agreed
          candidate — the argmin-divergence pick, in the spirit of the paper's
          argmin-entropy selection);
        * ``delta`` ∈ [0, 1] is that medoid's mean distance — the cell's
          residual disagreement (0 = unanimous).

    Uses ``rapidfuzz.distance.Levenshtein.normalized_distance`` which returns
    ``levenshtein / max(len)`` — exactly the paper's ``d(a_i, a_j)``. ``beta`` is
    accepted for API parity with the softmax-entropy formulation but is unused in
    this mean-distance variant.
    """
    n = len(candidates)
    if n == 0:
        return -1, 0.0
    if n == 1:
        return 0, 0.0

    # Pairwise normalized edit distances (symmetric).
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = Levenshtein.normalized_distance(candidates[i], candidates[j])
            dist[i][j] = dist[j][i] = d

    mean_to_others = [sum(dist[i]) / (n - 1) for i in range(n)]
    medoid = min(range(n), key=lambda i: mean_to_others[i])
    return medoid, mean_to_others[medoid]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def ensemble_vote(
    sources: list[EnsembleSource],
    numeric_tolerance: Decimal = _NUMERIC_TOLERANCE,
    alpha: float = 0.5,
    lexicon: Any = None,
    arbiter: Optional[Callable[[CellVote], Optional[Decimal]]] = None,
) -> list[CanonicalTable]:
    """Vote across N engine outputs into one merged CanonicalTable per table index.

    Args:
        sources: engine outputs with trust-prior weights.
        numeric_tolerance: |a − b| ≤ this counts two numbers as agreeing.
        alpha: ROVER frequency/confidence balance in ``S(w) = α·freq + (1−α)·conf``.
        lexicon: NomenclatureDictionary for LV-ROVER label verification
            (lazy-loaded default when None).
        arbiter: optional LLM-as-Judge hook; called on no-consensus (red) cells,
            may return a resolved value (demotes flag to yellow, provenance
            "arbiter"). None today.

    Returns:
        list[CanonicalTable] — one per table index. 0 sources → []; a single
        source → its tables passed through, tagged "ensemble_single_source"
        (no consensus is fabricated from one engine).
    """
    sources = [s for s in sources if s.tables]
    if not sources:
        return []

    if len(sources) == 1:
        out = []
        for ct in sources[0].tables:
            ct.metadata = {**ct.metadata, "reconciliation": "ensemble_single_source",
                           "source_names": [sources[0].name], "source_count": 1}
            out.append(ct)
        return out

    if lexicon is None:
        try:
            from accounting.nomenclature import load_default_dictionary
            lexicon = load_default_dictionary()
        except Exception:  # noqa: BLE001 — lexicon is optional (degrades to ROVER)
            lexicon = None

    n_tables = max(len(s.tables) for s in sources)
    merged_tables: list[CanonicalTable] = []

    for t_idx in range(n_tables):
        # Gather (source, table) for every source that has table t_idx.
        present = [(s, s.tables[t_idx]) for s in sources if t_idx < len(s.tables)]
        if len(present) == 1:
            only = present[0][1]
            only.metadata = {**only.metadata, "reconciliation": "ensemble_single_source",
                             "source_names": [present[0][0].name], "source_count": 1}
            merged_tables.append(only)
            continue
        merged_tables.append(
            _vote_table(present, numeric_tolerance, alpha, lexicon, arbiter)
        )

    return merged_tables


# ---------------------------------------------------------------------------
# Per-table voting
# ---------------------------------------------------------------------------


@dataclass
class _Member:
    """One source's contribution to a voting group."""

    source_name: str
    weight: float
    row: CanonicalRow


def _vote_table(
    present: list[tuple[EnsembleSource, CanonicalTable]],
    tolerance: Decimal,
    alpha: float,
    lexicon: Any,
    arbiter: Optional[Callable[[CellVote], Optional[Decimal]]],
) -> CanonicalTable:
    """Align all sources to an anchor and vote each row group cell-by-cell."""
    # Anchor = highest weight, ties broken by most rows.
    anchor_src, anchor_tbl = max(present, key=lambda p: (p[0].weight, len(p[1].rows)))

    # Tier-0 label authority: when the anchor is the trusted native extraction,
    # its label keyspace is clean and complete. Non-anchor sources (Tier-1 OCR /
    # docling) routinely emit fragment rows ('is', 'Moins: Amortiss', 'Incorporel')
    # that align to nothing; promoting them as standalone output rows dilutes and
    # duplicates the clean Tier-0 labels and breaks the benchmark's label-keyed
    # metric. So for a Tier-0 anchor we QUARANTINE non-anchor orphans into
    # metadata (preserved for HITL) instead of promoting them into scored rows.
    # For any other anchor we keep the original behavior exactly (nothing dropped).
    anchor_is_tier0 = anchor_src.name == "tier0"

    # One group per anchor row, seeded with the anchor member.
    groups: list[list[_Member]] = [
        [_Member(anchor_src.name, anchor_src.weight, row)] for row in anchor_tbl.rows
    ]
    # source_name -> list of quarantined orphan rows (Tier-0 anchor only).
    unaligned: dict[str, list[CanonicalRow]] = {}

    coverages: list[float] = []
    for src, tbl in present:
        if tbl is anchor_tbl:
            continue
        alignment = align_canonical_tables(anchor_tbl, tbl)
        coverages.append(alignment.coverage)
        for pair in alignment.pairs:
            groups[pair.a_index].append(_Member(src.name, src.weight, tbl.rows[pair.b_index]))
        if anchor_is_tier0:
            # Quarantine: keep fragments out of the scored keyspace, retain for HITL.
            if alignment.b_orphans:
                unaligned.setdefault(src.name, []).extend(
                    tbl.rows[b_idx] for b_idx in alignment.b_orphans
                )
        else:
            # Backward-compatible: non-anchor orphans become their own groups.
            for b_idx in alignment.b_orphans:
                groups.append([_Member(src.name, src.weight, tbl.rows[b_idx])])

    merged = CanonicalTable(
        statement_type=anchor_tbl.statement_type,
        column_model=anchor_tbl.column_model,
        page_range=anchor_tbl.page_range,
    )

    conflicts: list[dict[str, Any]] = []
    deltas: list[float] = []
    source_names = [s.name for s, _ in present]

    for grp in groups:
        row, row_conflicts, row_deltas = _vote_group(
            grp, len(merged.rows), tolerance, alpha, lexicon, arbiter
        )
        merged.rows.append(row)
        conflicts.extend(row_conflicts)
        deltas.extend(row_deltas)

    q_table = 1.0 - (sum(deltas) / len(deltas)) if deltas else 1.0

    merged.conflicts = conflicts
    merged.metadata = {
        # Evidence an earlier stage ESTABLISHED about this table's structure is
        # still true of the merged result — the vote reconciles cell VALUES, it
        # does not re-derive the column binding or re-run the scale resolver.
        # Rebuilding this dict from scratch silently discarded a proven
        # `column_profile` and the resolver's audit trail, so work done per tier
        # never reached the output the reviewer actually sees.
        **{k: v for k, v in (anchor_tbl.metadata or {}).items()
           if k in _CARRY_FORWARD_METADATA},
        "reconciliation": "ensemble",
        "source_names": source_names,
        "source_count": len(present),
        "conflict_count": len(conflicts),
        "q_table": round(q_table, 4),
        "mean_entropy": round((sum(deltas) / len(deltas)) if deltas else 0.0, 4),
        "alignment_coverage": round(sum(coverages) / len(coverages), 4) if coverages else 1.0,
    }
    # Tier-0 anchor: surface quarantined non-anchor fragments for HITL review,
    # one entry per source, serialized so the metadata stays JSON-friendly.
    if anchor_is_tier0 and unaligned:
        total_unaligned = 0
        for src_name, orphan_rows in unaligned.items():
            merged.metadata[f"unaligned_{src_name}"] = [r.to_dict() for r in orphan_rows]
            total_unaligned += len(orphan_rows)
        merged.metadata["unaligned_count"] = total_unaligned
    merged.recompute_aggregates()

    logger.info(
        "Ensemble vote: %d sources, %d groups, %d conflicts, Q_table=%.3f",
        len(present), len(groups), len(conflicts), q_table,
    )
    return merged


def _vote_group(
    members: list[_Member],
    row_index: int,
    tolerance: Decimal,
    alpha: float,
    lexicon: Any,
    arbiter: Optional[Callable[[CellVote], Optional[Decimal]]],
) -> tuple[CanonicalRow, list[dict[str, Any]], list[float]]:
    """Vote a single aligned row group into one merged CanonicalRow."""
    # Base metadata from the highest-weight member that carries a canonical term,
    # else the highest-weight member overall.
    labelled = [m for m in members if m.row.canonical_term]
    base = max(labelled or members, key=lambda m: m.weight).row

    # Tier-0 label authority (belt-and-suspenders): if the trusted native source
    # is in this aligned group, its raw_text is the canonical label — force it so
    # an aligned row can never inherit a Tier-1 fragment's text. Falls back to the
    # first non-empty member otherwise (unchanged behavior).
    tier0_member = next((m for m in members if m.source_name == "tier0"), None)
    if tier0_member is not None and tier0_member.row.raw_text:
        raw_text = tier0_member.row.raw_text
    else:
        raw_text = next((m.row.raw_text for m in members if m.row.raw_text), "")

    merged = CanonicalRow(
        raw_text=raw_text,
        match_type=base.match_type,
        match_confidence=base.match_confidence,
        account_code=base.account_code,
        section=base.section,
        validation_field=base.validation_field,
        is_subtotal=any(m.row.is_subtotal for m in members),
        grouped_from=list(base.grouped_from),
    )

    conflicts: list[dict[str, Any]] = []
    deltas: list[float] = []

    # --- Label vote (LV-ROVER) -------------------------------------------
    canonical_term, label_conflict = _vote_label(members, row_index, alpha, lexicon)
    merged.canonical_term = canonical_term
    if label_conflict is not None:
        conflicts.append(label_conflict)
        # ISSUE-9 T3 safety net: on a RED-consensus label conflict (lowest
        # agreement tier), the ROVER winner is confident-but-untrustworthy.
        # Suppress the asserted mapping rather than emit a wrong canonical_term
        # — keep raw_text + numeric cells intact for HITL. Narrowly scoped to
        # the red tier; yellow/green resolutions are left untouched.
        if label_conflict.get("flag") == "red":
            merged.canonical_term = None
            merged.account_code = None
            merged.section = None
            merged.validation_field = None
            merged.match_type = "unrecognized"
            merged.match_confidence = 0.0

    # --- Cell votes -------------------------------------------------------
    column_names: list[str] = []
    seen: set[str] = set()
    for m in members:
        for col in m.row.cells.keys():
            if col not in seen:
                seen.add(col)
                column_names.append(col)

    n_sources = len(members)
    for col in column_names:
        cell, conflict, delta = _vote_cell(
            members, col, row_index, n_sources, tolerance, arbiter
        )
        merged.cells[col] = cell
        deltas.append(delta)
        if conflict is not None:
            conflicts.append(conflict)

    return merged, conflicts, deltas


def _vote_label(
    members: list[_Member],
    row_index: int,
    alpha: float,
    lexicon: Any,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """ROVER vote over canonical_terms, then LV-ROVER lexicon verification."""
    # Aggregate per-term frequency + mean confidence, weighted by source.
    terms: dict[str, dict[str, float]] = {}
    n = 0
    for m in members:
        ct = m.row.canonical_term
        if not ct:
            continue
        n += 1
        slot = terms.setdefault(ct, {"count": 0.0, "conf_sum": 0.0, "wsum": 0.0})
        slot["count"] += 1
        slot["conf_sum"] += m.row.match_confidence
        slot["wsum"] += m.weight

    if not terms:
        return None, None

    def rover_score(name: str) -> float:
        s = terms[name]
        freq = s["count"] / n
        mean_conf = s["conf_sum"] / s["count"]
        return alpha * freq + (1 - alpha) * mean_conf

    ranked = sorted(terms.keys(), key=rover_score, reverse=True)

    # LV-ROVER: prefer the highest-scoring term that the lexicon validates.
    winner = ranked[0]
    if lexicon is not None:
        for cand in ranked:
            try:
                if lexicon.lookup(cand) is not None:
                    winner = cand
                    break
            except Exception:  # noqa: BLE001
                break

    # Label disagreement → red conflict (≥2 distinct canonical terms voted).
    label_conflict: Optional[dict[str, Any]] = None
    if len(terms) >= 2:
        label_conflict = {
            "row_index": row_index,
            "column_name": "<label>",
            "candidates": [
                {"term": t, "score": round(rover_score(t), 3),
                 "lexicon_ok": (lexicon.lookup(t) is not None) if lexicon else None}
                for t in ranked
            ],
            "winner": winner,
            "resolution": "lv_rover",
            "flag": "red",
        }

    return winner, label_conflict


def _vote_cell(
    members: list[_Member],
    column_name: str,
    row_index: int,
    n_sources: int,
    tolerance: Decimal,
    arbiter: Optional[Callable[[CellVote], Optional[Decimal]]],
) -> tuple[CanonicalCell, Optional[dict[str, Any]], float]:
    """Vote one column across the group. Returns (cell, conflict?, delta)."""
    # Collect this column's candidates from every member that has it.
    cands: list[dict[str, Any]] = []
    for m in members:
        cell = m.row.cells.get(column_name)
        if cell is None:
            continue
        cands.append({
            "source": m.source_name,
            "weight": m.weight,
            "raw": cell.raw_value,
            "parsed": cell.parsed_value,
            "confidence": cell.confidence,
            "flag": getattr(cell, "flag", None),  # the source engine's own parse flag
        })

    if not cands:
        return CanonicalCell(raw_value="", provenance="ensemble", flag="green"), None, 0.0

    n_contrib = len(cands)
    numeric = [c for c in cands if c["parsed"] is not None]

    # ---------------- Numeric branch -----------------------------------
    if numeric:
        clusters = _cluster_numeric(numeric, tolerance)
        total_w = sum(c["weight"] for c in numeric)
        winning = max(clusters, key=lambda cl: cl["weight"])
        win_member = max(winning["members"], key=lambda c: c["weight"])
        win_frac = winning["weight"] / total_w if total_w else 1.0
        delta = 0.0 if len(clusters) == 1 else (1.0 - win_frac)

        flag = _flag_from(delta, n_contrib)
        # Single contributing engine: there's no second read to corroborate, so
        # the ≥2-to-be-green rule would paint EVERY such cell yellow. Inherit the
        # source engine's own parse-confidence flag instead — green when it read
        # cleanly, yellow only when genuinely risky.
        if n_contrib == 1:
            flag = win_member.get("flag") or "green"
        unanimous = len(clusters) == 1 and n_contrib >= 2
        provenance = "consensus" if unanimous else "ensemble"

        conflict = None
        if len(clusters) > 1:
            # Optional LLM-as-Judge arbitration on no-consensus cells.
            resolved = None
            if flag == "red" and arbiter is not None:
                resolved = arbiter(CellVote(
                    row_index=row_index, column_name=column_name,
                    candidates=numeric, winner_parsed=win_member["parsed"], delta=delta,
                ))
            if resolved is not None:
                win_member = {**win_member, "parsed": resolved, "raw": str(resolved)}
                flag = "yellow"
                provenance = "arbiter"
            conflict = {
                "row_index": row_index,
                "column_name": column_name,
                "candidates": [
                    {"source": c["source"], "raw": c["raw"],
                     "parsed": str(c["parsed"]) if c["parsed"] is not None else None,
                     "weight": c["weight"]}
                    for c in cands
                ],
                "winner": str(win_member["parsed"]),
                "consensus_entropy": round(delta, 4),
                "resolution": provenance,
                "flag": flag,
            }

        cell = CanonicalCell(
            raw_value=win_member["raw"],
            parsed_value=win_member["parsed"],
            provenance=provenance,
            flag=flag,
            confidence=round(1.0 - delta, 3),
        )
        return cell, conflict, delta

    # ---------------- Text branch (no parsed numbers) ------------------
    strings = [c["raw"] or "" for c in cands]
    medoid_idx, delta = consensus_entropy(strings)
    win = cands[medoid_idx]
    flag = _flag_from(delta, n_contrib)
    # Single contributing engine → inherit its own flag, not a blanket yellow.
    if n_contrib == 1:
        flag = win.get("flag") or "green"
    provenance = "consensus" if delta <= _DELTA_GREEN and n_contrib >= 2 else "ensemble"

    conflict = None
    if delta > _DELTA_GREEN:
        conflict = {
            "row_index": row_index,
            "column_name": column_name,
            "candidates": [{"source": c["source"], "raw": c["raw"], "weight": c["weight"]}
                           for c in cands],
            "winner": win["raw"],
            "consensus_entropy": round(delta, 4),
            "resolution": provenance,
            "flag": flag,
        }

    cell = CanonicalCell(
        raw_value=win["raw"],
        parsed_value=None,
        provenance=provenance,
        flag=flag,
        confidence=round(1.0 - delta, 3),
    )
    return cell, conflict, delta


def _cluster_numeric(numeric: list[dict[str, Any]], tolerance: Decimal) -> list[dict[str, Any]]:
    """Greedy clustering of numeric candidates by |a − b| ≤ tolerance.

    Each cluster carries its total vote weight and members. Candidates are
    processed highest-weight-first so the heaviest vote anchors each cluster.
    """
    clusters: list[dict[str, Any]] = []
    for c in sorted(numeric, key=lambda x: x["weight"], reverse=True):
        placed = False
        for cl in clusters:
            if abs(c["parsed"] - cl["anchor"]) <= tolerance:
                cl["members"].append(c)
                cl["weight"] += c["weight"]
                placed = True
                break
        if not placed:
            clusters.append({"anchor": c["parsed"], "weight": c["weight"], "members": [c]})
    return clusters


def _flag_from(delta: float, n_contrib: int) -> str:
    """Map consensus divergence + contributor count to a cell flag."""
    if delta >= _DELTA_RED:
        return "red"
    if delta <= _DELTA_GREEN and n_contrib >= 2:
        return "green"
    return "yellow"


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


# Presentation order for canonical cell keys. ``CanonicalRow.cells`` is a plain
# dict whose insertion order is the SOURCE COLUMN INDEX order from the
# enrichment loop — so the reviewer's grid mirrored whatever order the extractor
# happened to produce (on a real bilan: value_n │ note │ value_n_minus_1, while
# the document reads Notes │ 2024 │ 2023). Ordering by ROLE instead makes the
# grid read like the statement regardless of extractor quirks. Keys not listed
# here (un-roled columns such as a Variation/% column) follow, in their original
# order, so nothing is hidden.
_ROLE_DISPLAY_ORDER = (
    "note", "brut", "amortissement", "net",
    "value_n", "value_n_minus_1", "value_n_minus_2", "variation", "pct",
)


def _ordered_cell_keys(ct: CanonicalTable) -> list[str]:
    """Cell keys for ``ct`` in canonical role order, un-roled keys last.

    Computed per TABLE (not per row) so every row emits the same columns in the
    same order — a DataFrame built from these records must not have its column
    order decided by whichever row happened to be first.
    """
    seen: list[str] = []
    for row in ct.rows:
        for k in row.cells:
            if k not in seen:
                seen.append(k)
    rank = {k: i for i, k in enumerate(_ROLE_DISPLAY_ORDER)}
    # Un-roled keys keep their relative order, after every roled key.
    return sorted(seen, key=lambda k: (rank.get(k, len(rank)), seen.index(k)))


def canonical_tables_to_records(tables: list[CanonicalTable]) -> list[list[dict[str, Any]]]:
    """Serialize voted tables into the legacy ``ExtractionResult.tables`` shape.

    Each table → list of row dicts ``{"label": …, <column>: raw_value, …}`` so
    legacy/JSON consumers keep working. Columns follow
    :data:`_ROLE_DISPLAY_ORDER`.

    ``label`` carries the canonical term when the row matched, else the raw OCR
    text; the original text is always preserved in ``label_raw``. Emitting
    ``label`` and ``canonical_term`` as two separate columns rendered two label
    columns side by side in the reviewer's grid — both marked meta (so neither
    editable) and largely redundant. The feedback loop reads ``canonical_term``
    from the CANONICAL dict, not from these records, so collapsing them here is
    safe.
    """
    out: list[list[dict[str, Any]]] = []
    for ct in tables:
        keys = _ordered_cell_keys(ct)
        rows: list[dict[str, Any]] = []
        for row in ct.rows:
            rec: dict[str, Any] = {
                "label": row.canonical_term or row.raw_text,
                "label_raw": row.raw_text,
            }
            for col in keys:
                cell = row.cells.get(col)
                rec[col] = cell.raw_value if cell is not None else ""
            rows.append(rec)
        out.append(rows)
    return out


def canonical_tables_to_parsed_dataframes(tables: list[CanonicalTable]) -> "list":
    """D3: the voted tables as DataFrames carrying PARSED values.

    Emits the exact same keys / columns / row order as
    :func:`canonical_tables_to_records` (``label`` = ``raw_text``,
    ``canonical_term``, one column per cell key) so grid column names and
    ``ValidationResult.locate`` targets stay valid — but each numeric cell is
    ``float(parsed_value)`` (canonical scale) instead of the raw OCR text.
    Cells with no parse keep ``raw_value`` so V1-006 still sees garbled text.

    This is the "after" input for the dual validation pass (D4): running the
    rule engine on these frames shows what validation looks like once every
    value is at the scale the pipeline actually parsed.
    """
    import pandas as pd  # local import — keep ensemble's core deps minimal

    frames = []
    for ct in tables:
        keys = _ordered_cell_keys(ct)
        rows: list[dict[str, Any]] = []
        for row in ct.rows:
            rec: dict[str, Any] = {
                "label": row.canonical_term or row.raw_text,
                "label_raw": row.raw_text,
            }
            for col in keys:
                cell = row.cells.get(col)
                if cell is None:
                    rec[col] = ""
                elif cell.parsed_value is not None:
                    rec[col] = float(cell.parsed_value)
                else:
                    rec[col] = cell.raw_value
            rows.append(rec)
        frames.append(pd.DataFrame(rows))
    return frames
