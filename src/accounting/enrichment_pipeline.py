"""
Enrichment Pipeline — §23 DAG Orchestrator.

Converts raw DataFrames into progressively enriched ``CanonicalTable``
objects by running Stages 1–10 in correct dependency order:

    Stage 1: Multi-page table merge         (table_merger)
    Stage 2: Section header detection       (header_detector)
    Stage 3: Column identification          (column_identifier)
    Stage 3b: Unit binding                  (unit_detector)
    Stage 4: FFFD resolution                (nomenclature.resolve_fffd)
    Stage 5: Number parsing                 (french_number)
    Stage 6: Fuzzy matching                 (nomenclature.fuzzy_match)
    Stage 7: Grouped entry detection        (grouped_entry)
    Stage 8: Subtotal detection + footing   (subtotal_detector)
    Stage 9: Document classification        (statement_segmenter)
    Stage 10: Flag assignment               (cell-level green/yellow/red)

Public API:

    enrich_tables(tables, dictionary, tier, ...) → list[CanonicalTable]
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Optional

import pandas as pd

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable

logger = logging.getLogger(__name__)

# VAL-8 — the dot-millimes SHAPE. Space-grouped thousands, a dot, exactly three
# digits: "14 267 969.725". In these statements that trailing group is the
# millimes (a thousandth of a dinar), but the parser reads space + dot as a
# THOUSANDS separator and returns 14267969725 — deliberately, and pinned by
# tests/test_nomenclature.py: space + comma is the decimal form, space + dot is
# not, and a blanket dot-decimal rule was tried and reverted at a net loss.
#
# So this package does not touch the parser. It MEASURES the shape — how much of
# a document is written this way — and exposes the measurement so the scale
# resolver can use it to ORDER a cell's existing readings. Nothing here reads or
# writes a ``parsed_value``; the regex is applied to the RAW string exactly as
# the extractor produced it, before any normalisation, because the spacing IS
# the evidence.
_DOT_MILLIMES_RE = re.compile(r"^-?\d{1,3}( \d{3})+\.\d{3}$")


def _dot_millimes_evidence(df: pd.DataFrame, numeric_cols) -> dict:
    """Share of a table's numeric RAW cells written in the dot-millimes shape.

    ``{"cells", "numeric_cells", "share"}``. The denominator is the table's
    numeric cells — ``is_numeric_token`` decides what counts as one, the same
    predicate the rest of the pipeline uses — so the share is comparable between
    a dense bilan and a three-row note. A table with no numeric cell at all
    scores 0.0 rather than dividing by zero.

    Measurement only: computed from the frame, touching no cell, and its result
    is metadata.
    """
    from accounting.french_number import is_numeric_token

    total = hits = 0
    for col_idx in list(numeric_cols or []):
        if col_idx >= len(df.columns):
            continue
        for row_idx in range(len(df)):
            value = df.iloc[row_idx, col_idx]
            if pd.isna(value):
                continue
            raw = str(value).strip()
            if not is_numeric_token(raw):
                continue
            total += 1
            if _DOT_MILLIMES_RE.match(raw):
                hits += 1
    return {
        "cells": hits,
        "numeric_cells": total,
        "share": round(hits / total, 4) if total else 0.0,
    }


def _propagate_dot_millimes_evidence(canonical_tables: list[CanonicalTable]) -> None:
    """VAL-8, document pass — the evidence is a DOCUMENT-level statement.

    One table of six rows proves nothing; a document whose numeric cells are
    largely written "14 267 969.725" is telling you what its dot means. So the
    per-table counts are summed once and every table is stamped with the
    document totals, which is what the arbitration hint reads.

    Like Stage 3b, this records and nothing else: no ``parsed_value`` is read or
    written, and a share of 1.0 still does not rescale a single cell.
    """
    if not canonical_tables:
        return
    try:
        cells = numeric = 0
        for ct in canonical_tables:
            ev = ct.metadata.get("dot_millimes_evidence")
            if not isinstance(ev, dict):
                continue
            cells += int(ev.get("cells") or 0)
            numeric += int(ev.get("numeric_cells") or 0)
        share = round(cells / numeric, 4) if numeric else 0.0
        for ct in canonical_tables:
            ev = ct.metadata.get("dot_millimes_evidence")
            if not isinstance(ev, dict):
                ev = {"cells": 0, "numeric_cells": 0, "share": 0.0}
                ct.metadata["dot_millimes_evidence"] = ev
            ev["document_cells"] = cells
            ev["document_numeric_cells"] = numeric
            ev["document_share"] = share
        if cells:
            logger.info(
                "VAL-8 (dot-millimes evidence): %d/%d numeric cell(s) (%.1f%%) "
                "over %d table(s) — measurement only, nothing rescaled",
                cells, numeric, share * 100, len(canonical_tables),
            )
    except Exception as exc:  # noqa: BLE001 — a measurement may never break enrichment
        logger.debug(f"Dot-millimes evidence (document) skipped: {exc}")


def enrich_tables(
    raw_tables: list[pd.DataFrame],
    dictionary=None,
    tier: int = 0,
    font_spans_per_table: Optional[list[list[dict]]] = None,
    provenance: Optional[list[dict]] = None,
    page_texts: Optional[dict[int, str]] = None,
) -> list[CanonicalTable]:
    """Run the full 10-stage enrichment pipeline on raw DataFrames.

    Args:
        raw_tables: List of DataFrames extracted by a tier.
        dictionary: NomenclatureDictionary (loaded lazily if None).
        tier: Extraction tier (0, 1, 2) for provenance tagging.
        font_spans_per_table: Optional font metadata per table (for header detection).
        provenance: Optional per-table ``{"page_no", "x_offsets"}`` captured at
            extraction, index-aligned with ``raw_tables``. Populates
            ``CanonicalTable.page_range``, which the model has always declared
            but nothing ever assigned — leaving reconciliation and the ensemble
            propagating a value that was invariably None.
        page_texts: Optional page text the tier already extracted, keyed by
            1-based page number, for Stage 3b (unit binding, VAL-7/E-2): the
            unit is stated as often in the caption line above a table as in the
            table itself, and the caption has never been retained. Key ``0`` is
            reserved for text that belongs to the WHOLE document rather than to
            one page — a tier that cannot split its text per page passes it
            there, and Stage 3b then uses it only to seed the document default,
            never to claim a per-table caption it cannot actually locate.
            Additive: ``None`` reproduces the prior behaviour exactly.

    Returns:
        List of fully enriched CanonicalTable objects.
    """
    if not raw_tables:
        return []

    # Load dictionary lazily
    if dictionary is None:
        try:
            from accounting.nomenclature import load_default_dictionary
            dictionary = load_default_dictionary()
        except ImportError:
            logger.warning("NomenclatureDictionary not available — returning raw output")
            return _raw_fallback(raw_tables, tier)

    provenance_tag = f"tier{tier}"   # per-CELL origin label (distinct from the
                                     # per-TABLE ``provenance`` argument above)
    canonical_tables: list[CanonicalTable] = []

    for table_idx, df in enumerate(raw_tables):
        if df.empty:
            continue

        font_spans = None
        if font_spans_per_table and table_idx < len(font_spans_per_table):
            font_spans = font_spans_per_table[table_idx]

        # Stage 3b input — the text of the page this table sits on, so the unit
        # stated in the caption above it is readable. Key 0 is document-level
        # text (see the ``page_texts`` docstring) and is NOT a caption: it is
        # held back for the document-default pass below.
        caption_text = None
        if page_texts:
            _page_no = None
            if provenance and table_idx < len(provenance):
                _page_no = (provenance[table_idx] or {}).get("page_no")
            if _page_no is not None:
                caption_text = page_texts.get(int(_page_no))

        ct = _enrich_single_table(df, dictionary, provenance_tag, font_spans, caption_text)

        # Which input frame this came from. Empty frames are skipped above, so
        # the output index is NOT the input index — anything that needs to map a
        # canonical table back to the table it was built from must use this
        # rather than enumerate().
        ct.metadata["source_table_index"] = table_idx

        # Page provenance — keyed on the INPUT index, so it stays correct even
        # though empty frames are skipped above and shift the output index.
        if provenance and table_idx < len(provenance):
            meta = provenance[table_idx] or {}
            page_no = meta.get("page_no")
            if page_no is not None:
                ct.page_range = (int(page_no), int(page_no))
            offsets = meta.get("x_offsets")
            if offsets:
                ct.metadata["x_offsets"] = list(offsets)

        canonical_tables.append(ct)

    _propagate_document_unit(canonical_tables, page_texts)
    _propagate_dot_millimes_evidence(canonical_tables)

    logger.info(f"Enrichment pipeline: produced {len(canonical_tables)} canonical tables")
    return canonical_tables


def _propagate_document_unit(
    canonical_tables: list[CanonicalTable],
    page_texts: Optional[dict[int, str]] = None,
) -> None:
    """Stage 3b, document pass — VAL-7 / decision E-2.

    A document states its unit once and expects it to hold throughout: EF CLB
    writes it on some pages and not on others. Majority vote over the tables
    that DO state one gives the document default; tables that state none inherit
    it with ``source="document_default"``. Where the stated units disagree,
    every table of the document is stamped ``unit_conflict=True``.

    **Nothing is rescaled.** No ``parsed_value`` is read or written here. A
    conflict is a flag for a human — the pipeline has no authority to decide
    which of two contradictory unit statements the figures were written under,
    and guessing wrong is a 1000x error.
    """
    if not canonical_tables:
        return
    try:
        from accounting.unit_detector import (
            SOURCE_CONFIDENCE,
            match_unit,
            resolve_document_default,
        )
    except ImportError as exc:  # noqa: BLE001 — additive stage, never fatal
        logger.debug(f"Stage 3b (document unit) skipped: {exc}")
        return

    try:
        stated = [ct.metadata.get("unit") for ct in canonical_tables]
        default, conflict = resolve_document_default(stated)

        # No table stated a unit: fall back to document-level text (key 0), the
        # only thing a tier that cannot split its text per page can offer.
        if default is None and page_texts:
            hit = match_unit(page_texts.get(0))
            if hit:
                value, mult = hit
                default = {
                    "value": value,
                    "source": "document_default",
                    "confidence": round(SOURCE_CONFIDENCE["document_default"] * mult, 3),
                }

        for ct, unit in zip(canonical_tables, stated):
            if unit is None and default is not None:
                ct.metadata["unit"] = dict(default)
            if conflict:
                ct.metadata["unit_conflict"] = True

        if default is not None:
            logger.info(
                "Stage 3b (unit): document unit %s (conflict=%s) over %d table(s)",
                default["value"], conflict, len(canonical_tables),
            )
    except Exception as exc:  # noqa: BLE001 — never break enrichment over a unit
        logger.debug(f"Stage 3b (document unit) skipped: {exc}")


def _enrich_single_table(
    df: pd.DataFrame,
    dictionary,
    provenance: str,
    font_spans: Optional[list[dict]] = None,
    caption_text: Optional[str] = None,
) -> CanonicalTable:
    """Run stages 2–10 on a single table."""

    ct = CanonicalTable()

    # ---------------------------------------------------------------
    # Stage 4b — Variation-% recompute (wire-in, pre-parse)
    # ---------------------------------------------------------------
    # Recompute garbled Var% cells from the two period values BEFORE Stage 5
    # parses the cells, so the corrected '%' string flows through the normal
    # parse path into ``CanonicalCell.parsed_value``. The validator already
    # carries the ISSUE-6 guards (skips a legitimate absolute-delta column and
    # skips prior==0); we only WIRE it in here — its logic is unchanged.
    # ``corrected_pct_cells`` records (row_idx, original_col_name) for every
    # cell whose value the recompute overwrote, so those reconstructed cells can
    # be routed to review (yellow) rather than emitted green.
    corrected_pct_cells: set[tuple[int, str]] = set()
    try:
        from accounting.validator import auto_correct_variation_percentages

        df_pre = df
        df_corrected, _pct_corrections = auto_correct_variation_percentages(df)
        if _pct_corrections:
            # Identify exactly which cells changed (column-name keyed) so we can
            # flag only the reconstructed cells, then adopt the corrected df for
            # all downstream stages.
            for col_name in df_pre.columns:
                pre_col = df_pre[col_name]
                cor_col = df_corrected[col_name]
                for row_idx in range(len(df_pre)):
                    if str(pre_col.iloc[row_idx]) != str(cor_col.iloc[row_idx]):
                        corrected_pct_cells.add((row_idx, str(col_name)))
            df = df_corrected
            logger.info(
                f"Stage 4b (var%% recompute): corrected {len(_pct_corrections)} cell(s)"
            )
    except Exception as exc:
        logger.debug(f"Stage 4b (var%% recompute) skipped: {exc}")

    # ---------------------------------------------------------------
    # Stage 2 — Section header detection
    # ---------------------------------------------------------------
    sections: list[Optional[str]] = []
    header_flags: list[bool] = []
    try:
        from accounting.header_detector import detect_section_headers_with_flags
        sections, header_flags = detect_section_headers_with_flags(
            df, dictionary, font_spans
        )
    except Exception as exc:
        logger.debug(f"Stage 2 (headers) skipped: {exc}")
        sections = [None] * len(df)
        header_flags = [False] * len(df)

    # ---------------------------------------------------------------
    # Stage 3 — Column identification
    # ---------------------------------------------------------------
    column_layout = None
    try:
        from accounting.column_identifier import identify_columns
        column_layout = identify_columns(df, dictionary)
        ct.column_model = column_layout.model
        # RTE-7: the identifier's own confidence is what green-flag gate G3
        # reads. It was computed and dropped here (only ``.model`` was kept),
        # so G3 saw 0.00 on every table and Tier 0 could never accept. Record
        # it — ``_CARRY_FORWARD_METADATA`` keeps it across an ensemble vote.
        ct.metadata["column_confidence"] = float(column_layout.confidence)
    except Exception as exc:
        logger.debug(f"Stage 3 (columns) skipped: {exc}")

    # ---------------------------------------------------------------
    # Stage 3b — Unit binding (VAL-7, decision E-2)
    # ---------------------------------------------------------------
    # Read the unit the table states — in its own header, or in the caption of
    # the page it sits on — and RECORD it. It runs after column identification
    # (so the header rows have been characterised) and before parsing, so the
    # unit is on the table for everything downstream; it deliberately does not
    # feed the parser. E-2 A: record and flag, never rescale. Nothing here
    # touches a ``parsed_value``.
    try:
        from accounting.unit_detector import detect_unit
        unit = detect_unit(df, caption_text)
        if unit is not None:
            ct.metadata["unit"] = unit
    except Exception as exc:
        logger.debug(f"Stage 3b (unit) skipped: {exc}")

    # ---------------------------------------------------------------
    # Stage 4 + 5 + 6 — FFFD → Numbers → Fuzzy Match (per row)
    # ---------------------------------------------------------------
    label_col_idx = 0
    if column_layout and hasattr(column_layout, "label_col_index"):
        label_col_idx = column_layout.label_col_index

    numeric_cols = []
    if column_layout and hasattr(column_layout, "numeric_col_indices"):
        numeric_cols = column_layout.numeric_col_indices

    match_results = []
    parsed_numbers: dict[tuple[int, int], Decimal] = {}

    # Per-document comma-convention inference (completes A1): a single comma is
    # token-level ambiguous ("210,580"=thousands vs "3811603,553"=decimal). Infer
    # the convention once from all the table's numeric tokens, then apply it to
    # every cell. comma_decimal=None preserves the prior per-token behaviour.
    comma_decimal: Optional[bool] = None
    try:
        from accounting.french_number import infer_comma_convention
        _num_tokens = [
            str(df.iloc[r, ci])
            for ci in numeric_cols
            for r in range(len(df))
            if ci < len(df.columns) and pd.notna(df.iloc[r, ci])
        ]
        _conv = infer_comma_convention(_num_tokens)
        if _conv == "comma_decimal":
            comma_decimal = True
        elif _conv == "comma_thousands":
            comma_decimal = False
    except Exception as exc:
        logger.debug(f"Comma-convention inference skipped: {exc}")

    # ---------------------------------------------------------------
    # VAL-8 — Dot-millimes evidence (measurement, taken beside the
    # comma-convention inference and on the same raw tokens)
    # ---------------------------------------------------------------
    # How much of this table is written "14 267 969.725". Recorded on the table
    # so it survives to the document pass, the ensemble vote and the HITL; it is
    # NOT an input to Stage 5 and does not change how one cell parses.
    try:
        ct.metadata["dot_millimes_evidence"] = _dot_millimes_evidence(df, numeric_cols)
    except Exception as exc:
        logger.debug(f"Dot-millimes evidence skipped: {exc}")

    # Classify the table's statement_type up-front (Stage 9 moved early) so it can
    # scope the matcher: a compte_resultat row must not match bilan vocabulary
    # (ISSUE-2 cross-statement leak). Unknown classification → None preserves the
    # prior full-scope behaviour.
    stmt_type: Optional[str] = None
    try:
        raw_labels = [
            str(df.iloc[r, label_col_idx]).strip()
            for r in range(len(df))
            if label_col_idx < len(df.columns)
        ]
        classified = dictionary.classify_statement(raw_labels)
        if classified and classified != "unknown":
            stmt_type = classified
    except Exception as exc:
        logger.debug(f"Stage 9 (early classification) skipped: {exc}")

    for row_idx in range(len(df)):
        raw_label = str(df.iloc[row_idx, label_col_idx]).strip() if label_col_idx < len(df.columns) else ""
        section = sections[row_idx] if row_idx < len(sections) else None

        # Stage 4 — FFFD resolution
        resolved_label = raw_label
        try:
            resolved_label, _, _ = dictionary.resolve_fffd(raw_label)
        except Exception:
            pass

        # Stage 6 — Fuzzy matching (uses resolved label + section + statement scope).
        # statement_type keeps a CR row from matching bilan vocabulary even when a
        # stale section header would otherwise widen the scope (ISSUE-2).
        mr = dictionary.fuzzy_match(
            resolved_label, section=section, statement_type=stmt_type
        )
        match_results.append(mr)

        # Build the canonical row
        canonical_row = CanonicalRow(
            raw_text=raw_label,
            section=section,
        )

        # ISSUE-7b: an empty section-header row (e.g. "IMMOBILISATIONS
        # CORPORELLES" with every value cell blank) must NOT be promoted to a
        # canonical leaf — doing so duplicates the real data row of the same
        # label. Suppress the fuzzy match when BOTH conditions hold:
        #   (1) every value cell on this row is empty, AND
        #   (2) the header detector raised a positive header signal for the row
        #       (CAPS / bold / label-only).
        # Requiring both avoids misclassifying a genuine nil-value leaf.
        row_header_flag = header_flags[row_idx] if row_idx < len(header_flags) else False
        value_cells_all_empty = _value_cells_all_empty(
            df, row_idx, label_col_idx, numeric_cols
        )
        is_empty_header = bool(row_header_flag and value_cells_all_empty)

        if is_empty_header:
            # Mark as header, leave canonical_term null — no leaf emitted.
            canonical_row.is_header = True
            canonical_row.match_type = "header"
            canonical_row.match_confidence = mr.confidence
        elif mr.entry:
            canonical_row.canonical_term = mr.entry.canonical_term
            canonical_row.match_type = mr.match_type
            canonical_row.match_confidence = mr.confidence
            canonical_row.account_code = mr.entry.account_code
            canonical_row.validation_field = mr.entry.validation_field
            canonical_row.is_subtotal = getattr(mr.entry, "is_subtotal", False)
        else:
            canonical_row.match_type = mr.match_type
            canonical_row.match_confidence = mr.confidence

        # Stage 5 — Parse numeric cells
        for col_idx in range(len(df.columns)):
            col_name = str(df.columns[col_idx])
            raw_value = str(df.iloc[row_idx, col_idx]) if pd.notna(df.iloc[row_idx, col_idx]) else ""

            cell = CanonicalCell(
                raw_value=raw_value,
                provenance=provenance,
            )

            if col_idx in numeric_cols:
                try:
                    from accounting.french_number import (
                        numeric_parse_is_risky,
                        parse_french_number_with_basis,
                    )
                    parsed, basis = parse_french_number_with_basis(
                        raw_value, comma_decimal=comma_decimal
                    )
                    cell.parsed_value = parsed
                    cell.basis = basis
                    if parsed is not None:
                        parsed_numbers[(row_idx, col_idx)] = parsed
                        # A value that parses only via an unreliable OCR recovery
                        # (trailing-minus, stray digit-split) must not be emitted
                        # green/confident — route it to review.
                        if numeric_parse_is_risky(raw_value):
                            cell.flag = "yellow"
                    elif raw_value.strip():
                        cell.flag = "yellow"  # parse failure
                except Exception:
                    cell.flag = "yellow"

            # Stage 4b: a variation-% cell whose value was RECOMPUTED from the
            # period values is a reconstruction, not a verified read. Ensure the
            # corrected percentage reaches ``parsed_value`` even when the '%'
            # column was not classified numeric (Stage 5 only parses numeric
            # cols), then route it to review (yellow) rather than emit it green.
            if (row_idx, col_name) in corrected_pct_cells:
                if cell.parsed_value is None and raw_value.strip():
                    # The corrected token is a percentage string ("+10,0%"); use
                    # the validator's percent-aware numeric extractor (the same
                    # helper that produced the recompute) so the '%' and sign do
                    # not defeat parse_french_number.
                    try:
                        from decimal import Decimal as _Decimal
                        from accounting.validator import extract_numeric_value
                        _p = extract_numeric_value(raw_value)
                        if _p is not None:
                            cell.parsed_value = _Decimal(str(_p))
                            parsed_numbers[(row_idx, col_idx)] = cell.parsed_value
                    except Exception:
                        pass
                cell.flag = "yellow"

            # Map column to role name if available
            role_name = _col_idx_to_role(col_idx, column_layout)
            canonical_row.cells[role_name or col_name] = cell

        ct.rows.append(canonical_row)

    # ---------------------------------------------------------------
    # Stage 7 — Grouped entry detection (unmatched rows only)
    # ---------------------------------------------------------------
    try:
        from accounting.grouped_entry import classify_unmatched_rows
        grouped_results = classify_unmatched_rows(df, match_results, dictionary, sections)
        for gr in grouped_results:
            if gr.row_index < len(ct.rows):
                row = ct.rows[gr.row_index]
                if gr.classification == "grouped":
                    row.match_type = "grouped"
                    row.match_confidence = gr.confidence
                    if gr.parent_canonical:
                        row.grouped_from = [gr.parent_canonical]
    except Exception as exc:
        logger.debug(f"Stage 7 (grouped) skipped: {exc}")

    # ---------------------------------------------------------------
    # Stage 8 — Subtotal detection + footing
    # ---------------------------------------------------------------
    try:
        from accounting.subtotal_detector import detect_subtotals
        subtotal_results = detect_subtotals(
            df, column_layout, dictionary, sections, parsed_numbers
        )
        for sr in subtotal_results:
            if sr.row_index < len(ct.rows):
                ct.rows[sr.row_index].is_subtotal = True
                if sr.footing_valid is False:
                    # Footing mismatch — flag the subtotal row
                    for cell in ct.rows[sr.row_index].cells.values():
                        if cell.parsed_value is not None:
                            cell.flag = "yellow"
        # RTE-7: green-flag gate G4 asks whether at least one subtotal footed.
        # The verdicts were computed here and thrown away, so G4 always took
        # its "no subtotals" default pass. Record one tri-state per detected
        # subtotal row (True footed / False mismatched / None not checkable);
        # the list stays JSON-friendly for the metadata round-trip.
        ct.metadata["footing_results"] = [sr.footing_valid for sr in subtotal_results]
    except Exception as exc:
        logger.debug(f"Stage 8 (subtotals) skipped: {exc}")

    # ---------------------------------------------------------------
    # Stage 8b — Validation-driven scale resolution (E2)
    # ---------------------------------------------------------------
    # An ambiguous (assumption-basis) cell is arbitrated by the arithmetic
    # identities its confident neighbours pin down (VNC, Var%) — the only
    # definitive scale signal in the data. Conservative: acts only when exactly
    # one parse candidate survives; never fabricates a balancing value.
    ct.statement_type = stmt_type or ct.statement_type  # resolver needs the type
    try:
        from accounting.scale_resolver import resolve_table
        resolutions = resolve_table(ct)
        if resolutions:
            logger.info(
                f"Stage 8b (scale resolver): {len(resolutions)} cell(s) "
                f"{'/'.join(sorted({r.kind for r in resolutions}))}"
            )
    except Exception as exc:
        logger.debug(f"Stage 8b (scale resolver) skipped: {exc}")

    # ---------------------------------------------------------------
    # Stage 8c — Arithmetic verification of the column binding
    # ---------------------------------------------------------------
    # The relations BETWEEN columns do not vary between two statements of the
    # same type, even though the header wording does. So the applicable profile's
    # rule is tested against the numbers: a rule that holds corroborates the
    # binding, and the rows that break it are exactly the cells worth a
    # reviewer's attention — a rule holding on 20 of 22 rows has localised the
    # other two.
    #
    # Verification only ADDS evidence and flags; it never re-binds. A subtraction
    # identity is symmetric (`a - b == c` ⟺ `a - c == b`), so it can refute a
    # binding but cannot select one — see accounting.column_binder.
    try:
        from accounting.column_binder import (
            candidate_profiles, propose_from_headers, verify_rule,
        )

        base_roles = getattr(column_layout, "role_map", None) or {}
        if base_roles:
            label_idx = getattr(column_layout, "label_col_index", 0)
            for profile_name, cfg in candidate_profiles(dictionary, stmt_type):
                rule = cfg.get("arithmetic_rule")
                if not rule:
                    continue
                # Headers propose the roles the identity needs but the core
                # matcher has no vocabulary for (Variation/Écart/%), then the
                # arithmetic decides whether that proposal survives.
                role_map = propose_from_headers(
                    df, cfg, base_roles, exclude={label_idx},
                )
                verdict = verify_rule(df, role_map, rule)
                if not verdict.proven:
                    continue
                # Roles the identity corroborated are adopted; the cells they
                # name are already parsed, so this only labels them.
                for role, col_idx in role_map.items():
                    base_roles.setdefault(role, col_idx)
                ct.metadata["column_profile"] = profile_name
                ct.metadata["arithmetic_rule"] = rule
                ct.metadata["arithmetic_holds"] = verdict.holds
                ct.metadata["arithmetic_checked"] = verdict.checked
                # A cell on a row that breaks a PROVEN identity is suspect: the
                # identity held everywhere else, so this row is where the read
                # went wrong. Flag for review — never silently rewrite.
                from accounting.column_binder import rule_roles
                suspect_roles = rule_roles(rule)
                for row_idx in verdict.offending_rows:
                    if row_idx >= len(ct.rows):
                        continue
                    for role in suspect_roles:
                        cell = ct.rows[row_idx].cells.get(role)
                        if cell is not None and cell.flag == "green":
                            cell.flag = "yellow"
                if verdict.offending_rows:
                    logger.info(
                        f"Stage 8c: '{rule}' holds {verdict.holds}/{verdict.checked} "
                        f"— flagged rows {verdict.offending_rows} for review"
                    )
                break
    except Exception as exc:
        logger.debug(f"Stage 8c (arithmetic verification) skipped: {exc}")

    # ---------------------------------------------------------------
    # Stage 9 — Statement classification
    # ---------------------------------------------------------------
    # Adopt the classification computed before the row loop, where it was needed
    # to scope the matcher.
    #
    # There used to be a "fall back to a fresh classify if it was unknown" branch
    # here. It could never change anything: the early pass classifies
    # `df.iloc[r, label_col_idx]` and `CanonicalRow.raw_text` is built from that
    # same cell, so the fallback re-ran the identical scorer over identical
    # strings and necessarily returned "unknown" again. Left alone,
    # `statement_type` keeps the model's own "unknown" default.
    #
    # A genuinely better answer does exist, but not here — it needs the
    # reconciled cross-tier tables, which is why the authoritative pass runs in
    # `pipeline._attach_statements` (see context/Known-Gaps-*.md, D3).
    if stmt_type is not None:
        ct.statement_type = stmt_type

    # ---------------------------------------------------------------
    # Stage 10 — Flag assignment (already done per-cell above; aggregate)
    # ---------------------------------------------------------------
    # Stage 10b — Prune un-roled cell columns (confident tables only). A noisy
    # extraction leaves duplicate value columns + label echoes that were never
    # bound to a role; for a confidently-classified table these are pure noise
    # (the label is in raw_text, values/notes are in the roled cells). Dropping
    # them keeps reconciliation aligning by role and the HITL grid clean. Runs
    # AFTER the subtotal + scale-resolver stages, which need the full cell set.
    prune_noise_columns(ct, column_layout)

    ct.recompute_aggregates()

    return ct


# Canonical role keys a confidently-classified table is expected to carry.
_CANONICAL_ROLE_KEYS = frozenset(
    {"value_n", "value_n_minus_1", "brut", "amortissement", "net", "note"}
)


def prune_noise_columns(ct: CanonicalTable, column_layout=None) -> None:
    """Drop *noise* un-roled cell columns from a confidently-classified table.

    Cells are keyed by ``role_name or col_name``; a cell keyed by a raw column
    name was never bound to a canonical role. Some of those are real data (a
    Variation/% column) and must be kept — so this drops an un-roled column ONLY
    when it is noise: empty, a duplicate of a roled value column, or an echo of
    the row label (already carried by ``raw_text``). Conservative: a no-op
    unless the model is known AND value roles exist, and duplicate/echo
    detection needs ≥2 agreeing rows, so a low-confidence or thin table keeps
    every cell.

    Runs at two points: per-tier inside enrichment (``column_layout`` supplies
    the precise role map) and again on the merged tables a cross-tier
    reconciliation / ensemble vote produces — those re-introduce duplicate value
    columns from a second source, so the role keys are then inferred from the
    canonical role names present in the cells.
    """
    model = getattr(column_layout, "model", None) if column_layout is not None else None
    model = model or ct.column_model or "unknown"
    if column_layout is not None:
        role_keys = set(getattr(column_layout, "role_map", {}) or {})
    else:
        present = {k for row in ct.rows for k in row.cells}
        role_keys = present & _CANONICAL_ROLE_KEYS

    value_keys = role_keys & {"value_n", "value_n_minus_1", "brut", "amortissement", "net"}
    if model not in ("n_n1", "brut_amort_net") or not value_keys:
        return

    def _norm(s) -> str:
        return "".join(ch for ch in str(s).lower() if ch.isalnum() or ch == "-")

    # Per-column non-empty normalized values (by cell key) + the row labels.
    col_vals: dict[str, dict[int, str]] = {}
    label_vals: dict[int, str] = {}
    all_keys: set[str] = set()
    for ri, row in enumerate(ct.rows):
        lv = _norm(row.raw_text)
        if lv:
            label_vals[ri] = lv
        for k, cell in row.cells.items():
            all_keys.add(k)
            v = _norm(cell.raw_value)
            if v:
                col_vals.setdefault(k, {})[ri] = v

    def _label_echo(vals: dict[int, str]) -> bool:
        # ≥2 overlapping rows all equal to the row label → a label-echo column.
        common = set(vals) & set(label_vals)
        return len(common) >= 2 and all(vals[r] == label_vals[r] for r in common)

    def _num(key: str, ri: int):
        cell = ct.rows[ri].cells.get(key)
        return cell.parsed_value if cell is not None else None

    drop: set[str] = set()
    for k in sorted(all_keys):
        if k in role_keys:
            continue
        vals = col_vals.get(k, {})
        if not vals:                                   # empty column → noise
            drop.add(k)
            continue
        if _label_echo(vals):                          # echoes the row label
            drop.add(k)
            continue
        # Duplicate-or-repair of a value column: wherever BOTH carry a parsed
        # number they agree (≥2 rows, no genuine numeric conflict). A real
        # Variation/% column conflicts numerically and is therefore kept. When
        # the value column lost a number to label spillover (the row aligner can
        # drop a value into the wrong column on a busy table), the duplicate is
        # its repair source.
        for vk in sorted(value_keys):
            agree = conflict = 0
            repair_rows: list[int] = []
            for ri in range(len(ct.rows)):
                kn, vn = _num(k, ri), _num(vk, ri)
                if kn is not None and vn is not None:
                    if kn == vn:
                        agree += 1
                    else:
                        conflict += 1
                elif kn is not None and vn is None:
                    repair_rows.append(ri)             # k has the number, value col lost it
            if agree >= 2 and conflict == 0:
                for ri in repair_rows:
                    src = ct.rows[ri].cells.get(k)
                    if src is None or src.parsed_value is None:
                        continue
                    ct.rows[ri].cells[vk] = CanonicalCell(
                        raw_value=src.raw_value,
                        parsed_value=src.parsed_value,
                        provenance="consensus",
                        flag="yellow",                 # reconstructed → review
                        basis=src.basis,
                    )
                drop.add(k)
                break

    # Scrub label/header text that leaked into a value or note column. A
    # misaligned merge can drop the row label (or a header date) into value_n on
    # a section-header / empty row — and the repair pass can't fix it because
    # there's no number anywhere on that row to repair from. A value cell that
    # didn't parse to a number and is pure text, a bare date, or an echo of the
    # row label is never real data: blank it so the grid shows an empty value,
    # not the label. A garbled-but-numeric cell (has digits) is left for review.
    import re as _re
    _date = _re.compile(r"^\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\s*$")
    for ri, row in enumerate(ct.rows):
        lbl = label_vals.get(ri, "")
        for vk in value_keys:
            cell = row.cells.get(vk)
            if cell is None or cell.parsed_value is not None:
                continue
            s = (cell.raw_value or "").strip()
            if s and (_norm(s) == lbl or _date.match(s) or not any(c.isdigit() for c in s)):
                cell.raw_value = ""
                cell.flag = "green"
                cell.basis = None
        if "note" in role_keys:                    # a note cell echoing the label or the "Notes" header
            nc = row.cells.get("note")
            if nc is not None and nc.parsed_value is None:
                s = (nc.raw_value or "").strip()
                if s and (_norm(s) == lbl or _norm(s) in {"note", "notes"}):
                    nc.raw_value = ""
                    nc.flag = "green"

    if drop:
        for row in ct.rows:
            row.cells = {k: v for k, v in row.cells.items() if k not in drop}


def _value_cells_all_empty(
    df: pd.DataFrame,
    row_idx: int,
    label_col_idx: int,
    numeric_cols: list[int],
) -> bool:
    """True if every *value* cell on this row is empty/blank.

    ISSUE-7b helper. "Value" cells are the numeric columns when known;
    otherwise every column except the label column is considered. A cell
    counts as empty when it is NaN or whitespace-only.
    """
    value_col_indices = (
        list(numeric_cols)
        if numeric_cols
        else [c for c in range(len(df.columns)) if c != label_col_idx]
    )
    if not value_col_indices:
        return False

    for col_idx in value_col_indices:
        if col_idx >= len(df.columns):
            continue
        val = df.iloc[row_idx, col_idx]
        if pd.notna(val) and str(val).strip():
            return False
    return True


def _col_idx_to_role(col_idx: int, column_layout) -> Optional[str]:
    """Map a column index to its canonical role name."""
    if column_layout is None:
        return None
    role_map = getattr(column_layout, "role_map", {})
    for role, idx in role_map.items():
        if idx == col_idx:
            return role
    return None


def _raw_fallback(tables: list[pd.DataFrame], tier: int) -> list[CanonicalTable]:
    """Produce minimal CanonicalTables when no dictionary is available."""
    provenance = f"tier{tier}"
    result: list[CanonicalTable] = []
    for df in tables:
        ct = CanonicalTable()
        for row_idx in range(len(df)):
            row = CanonicalRow(
                raw_text=str(df.iloc[row_idx, 0]) if len(df.columns) > 0 else "",
                match_type="unrecognized",
            )
            for col_idx in range(len(df.columns)):
                raw = str(df.iloc[row_idx, col_idx]) if pd.notna(df.iloc[row_idx, col_idx]) else ""
                row.cells[str(df.columns[col_idx])] = CanonicalCell(
                    raw_value=raw, provenance=provenance
                )
            ct.rows.append(row)
        ct.recompute_aggregates()
        result.append(ct)
    return result
