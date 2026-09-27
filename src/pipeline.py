"""
FinAlze OCR Pipeline - Main Orchestrator

Cascading OCR pipeline with QCS-based routing:
- Tier 0: Native extraction (PyMuPDF) — digital PDFs with embedded text
- Tier 1: Fast OCR (Docling + TableFormer)
- Tier 2: VLM fallback (Qwen2.5-VL) [placeholder]
- Tier 3: Ensemble / HITL
"""

import argparse
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Result-payload version.
#   1 — `canonical_tables` keyed per TABLE (index-aligned with `tables`).
#   2 — keyed per STATEMENT (index-aligned with `metadata['statements']`);
#       `tables` is unchanged, and each statement's `table_indices` maps back.
# Stored job results carry this so a consumer knows which keying it holds.
SCHEMA_VERSION = 2


def _all_canonical_table_objects(items: list) -> bool:
    """Return True iff every element is a live ``CanonicalTable`` (not a dict).

    Used to gate dual-tier reconciliation — older runs may have serialized
    Tier 0 ``canonical_tables`` to dicts, in which case reconciliation has
    nothing to merge against and we should fall through.
    """
    if not items:
        return False
    try:
        from accounting.canonical_model import CanonicalTable
    except ImportError:
        return False
    return all(isinstance(it, CanonicalTable) for it in items)


READ_FLOOR_REASON = "empty_read_floor"

# The grade vocabulary's order, worst first (RTE-2 — one vocabulary for the
# whole pipeline). Spelled out here rather than imported so that reading the
# Tier-1 measurement below does not pull in the extractor module.
_GRADE_RANK = {"POOR": 0, "FAIR": 1, "GOOD": 2, "EXCELLENT": 3}


def _docling_grades_for_measurement(docling_confidence: Optional[dict]) -> dict:
    """The four grades a QCS call WOULD take from Docling's confidence report.

    RTE-3, the measurement half. ``calculate_qcs`` takes four grades and has
    always been handed four literal "FAIR"s, because the extractor probed the
    wrong object for them. WP-2.8 reads the real report — and does NOT put it in
    the routing call. This function shapes the recorded report into the argument
    list that call would take, so a second, unused ``calculate_qcs`` can say
    what the QCS WOULD be. That number is the input to P7/WP-7.2, where the
    adoption decision lives; the plan's ruling is "record now, adopt after
    calibration", and Q5-A/G-4 say no automation widens before it is measured.

    Every signal Docling did not measure falls back to the same "FAIR" the
    routing call defaults to, so the only difference between the two calls is
    the signals Docling actually measured. ``table_score`` has no writer in
    docling 2.70 and never contributes; ``calculate_intrinsic_score`` excludes
    it anyway.
    """
    grades = ((docling_confidence or {}).get("grades") or {})
    out = {}
    for key in ("ocr", "layout", "parse"):
        grade = grades.get(key)
        out[key] = grade if grade in _GRADE_RANK else "FAIR"
    low = grades.get("low")
    # Docling's own `low_grade` is a quantile over PAGES, so it sees one bad
    # page in a good document; the worst-of-three is only the fallback.
    out["low"] = low if low in _GRADE_RANK else min(
        (out["ocr"], out["layout"], out["parse"]), key=lambda g: _GRADE_RANK[g])
    return out


def _apply_read_floor(hitl_required: bool, hitl_reason: str,
                      read_floor: Optional[dict]) -> tuple:
    """Fold the Tier-1 empty-read floor into the review decision (RTE-4).

    The floor FLAGS and never ends. Three properties, and each is a line of
    code here rather than a promise in a docstring:

    * it may only ever RAISE ``hitl_required`` — like every other HITL writer on
      this path (``_attach_statements``, ``_enforce_quarantine``, the ensemble).
      An unflagged floor leaves a flagged document flagged;
    * it adds a reason, it does not replace one. "the extraction scored 0.58"
      and "the pages came back blank" are two different things a reviewer wants
      to know, and only one of them is visible in a score;
    * it has no reject branch and no threshold of its own in the cascade. A
      flagged document is extracted, scored and returned exactly as it would
      have been; the only difference is that a human is asked to look.

    Adoption of any of this into ROUTING — a band, an escalation, a refusal —
    is P7 and deliberately not here.
    """
    if not (read_floor or {}).get("flagged"):
        return bool(hitl_required), hitl_reason or ""
    reason = hitl_reason or ""
    return True, f"{reason}; {READ_FLOOR_REASON}" if reason else READ_FLOOR_REASON


def _cluster_words_to_grid(words, xranges, names, y_tol=3.0):
    """Cluster positioned words into an aligned row-grid (pure, no PyMuPDF).

    Reconstructs table rows by the *y-coordinate* of each word so that
    label / note / value fragments sharing a physical row align — fixing
    the index-zip explode misalignment (ISSUE-9). When a row has fewer
    value fragments than label fragments (e.g. a value-less section header
    like "Actifs"), the value cells for that row stay empty instead of
    being cross-assigned a later row's value.

    Args:
        words: list of (x0, y0, x1, y1, text) tuples (PyMuPDF "words" layout).
        xranges: list of (cx0, cx1) horizontal spans, one per column.
        names: column names (defines the number of output columns).
        y_tol: max vertical distance (points) for words to share a row.

    Returns:
        list[list[str]] — rows of cell strings aligned by y-position.
    """
    ws = sorted(words, key=lambda w: (w[1], w[0]))
    rows, cur, cy = [], [], None
    for w in ws:
        yc = (w[1] + w[3]) / 2
        if cy is None or abs(yc - cy) <= y_tol:
            cur.append(w)
            cy = yc if cy is None else (cy * 0.7 + yc * 0.3)
        else:
            rows.append(cur)
            cur = [w]
            cy = yc
    if cur:
        rows.append(cur)
    out = []
    for r in rows:
        cells = [''] * len(names)
        for w in r:
            xc = (w[0] + w[2]) / 2
            for ci, (cx0, cx1) in enumerate(xranges):
                if cx0 - 1 <= xc <= cx1 + 1:
                    cells[ci] = (cells[ci] + ' ' + w[4]).strip()
                    break
        out.append(cells)
    return out


def _reconstruct_rows_by_geometry(page, tab, y_tol=3.0):
    """Rebuild a table DataFrame from PyMuPDF word geometry (ISSUE-9).

    Thin wrapper over :func:`_cluster_words_to_grid`: derives per-column
    x-ranges from the detected header cells, collects body words inside the
    table bbox, and clusters them by y. Returns ``None`` (caller falls back
    to the legacy explode path) when header geometry is unavailable.
    """
    import pandas as pd
    names = list(tab.to_pandas().columns)
    hdr = [c for c in (tab.header.cells or []) if c]
    if not hdr or len(hdr) != len(names):
        return None  # cannot reconstruct; caller falls back
    xranges = [(c[0], c[2]) for c in hdr]
    body_top = max(c[3] for c in hdr)
    x0, y0, x1, y1 = tab.bbox
    words = [w for w in page.get_text('words')
             if w[1] >= body_top - 1 and w[3] <= y1 + 1 and w[0] >= x0 - 1 and w[2] <= x1 + 1]
    grid = _cluster_words_to_grid(words, xranges, names, y_tol)
    if not grid:
        return None
    return pd.DataFrame(grid, columns=names)


def _table_x_offsets(tab) -> list:
    """Left x-coordinate of each column of a PyMuPDF table.

    Threaded through as extraction provenance so multi-page continuation
    detection has real geometry to compare: ``table_merger``'s x-alignment
    signal returns a fixed neutral 0.5 without offsets, which silently costs it
    a quarter of its evidence. Derived from the detected header cells; ``[]``
    when the table exposes no usable header geometry.
    """
    try:
        header = getattr(tab, "header", None)
        cells = getattr(header, "cells", None) if header is not None else None
        return [float(c[0]) for c in (cells or []) if c]
    except Exception:  # geometry is best-effort — never break extraction
        return []


@dataclass
class ExtractionResult:
    """Result from OCR extraction.

    ``canonical_tables`` holds the progressively enriched output (§26 of the
    nomenclature revamp plan). Populated by Phase 2+; earlier tiers that
    haven't been migrated yet leave it as an empty list — legacy consumers
    of ``tables`` continue to work unchanged.
    """
    text: str
    tables: list
    qcs_score: float
    tier_used: int
    confidence_details: dict
    metadata: dict
    canonical_tables: list = field(default_factory=list)
    # Pre-reconciliation, per-engine canonical tables. Tier 3 ensemble voting
    # consumes these so it does not double-count Tier 0 (Tier 1's in-tier
    # dual-tier reconciliation overwrites ``canonical_tables``). Each tier sets
    # this to its own enrichment output before any cross-tier merge.
    raw_canonical_tables: list = field(default_factory=list)


@dataclass
class PipelineConfig:
    """Configuration for the OCR pipeline."""
    # QCS thresholds (grade-mapped intrinsic scores now use full 0-1 range)
    tau_native_high: float = 0.95
    tau_fast_high: float = 0.75  # Restored — grade mapping fixes threshold collapse
    tau_vlm_med: float = 0.50
    # Tier 3 ensemble routing on the Consensus-Entropy table quality Q_table
    # (arXiv 2504.11101): Q ≥ high → auto-accept; arbiter band → LLM-judge hook;
    # below arbiter (or any critical arithmetic failure) → HITL.
    tau_ensemble_high: float = 0.90
    tau_ensemble_arbiter: float = 0.70
    tau_ensemble_min: float = 0.60  # hard HITL floor (legacy)
    
    # Accounting standard. Default NCT: it is the only COMPLETE vertical — the
    # canonical nomenclature (rules/nomenclature_data.yaml) and the validation
    # identities are NCT-structured. IFRS/SYSCOHADA currently get a per-standard
    # OCR term vocabulary (preprocessing/terms/*.yaml) but otherwise ride the NCT
    # canonical/validation rails. See context/Accounting-Standards-Handling.md.
    accounting_standard: str = "NCT"  # NCT (complete) | IFRS | SYSCOHADA (spell-check only)
    
    # Performance
    enable_gpu: bool = True
    batch_size: int = 1
    
    # Output
    output_format: str = "json"  # json, markdown, csv


class FinAlzePipeline:
    """Main OCR pipeline orchestrator."""
    
    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()
        self._initialize_models()
    
    def _initialize_models(self):
        """Lazy-load OCR models."""
        logger.info("Initializing FinAlze OCR Pipeline...")
        # Models will be loaded on first use
        self._docling_converter = None
        self._surya_predictor = None
        self._marker_converter = None
    
    def process_document(
        self,
        input_path: Path,
        preprocess_resize: bool = True,
        preprocess_grayscale: bool = True,
        max_image_width: int = 2000,
        accounting_standard: str = "NCT",
        progress_callback=None,
    ) -> ExtractionResult:
        """
        Process a single document through the cascading pipeline.
        
        Args:
            input_path: Path to PDF, DOCX, or image file
            preprocess_resize: Resize large images for faster processing
            preprocess_grayscale: Convert to grayscale for faster OCR
            max_image_width: Maximum image width before resizing
            accounting_standard: One of 'IFRS', 'NCT', 'SYSCOHADA'
            
        Returns:
            ExtractionResult with extracted text, tables, and QCS score
        """
        from document_intake import inspect_document, event
        events = []
        def emit(item):
            events.append(item)
            if progress_callback:
                progress_callback(item)
        self.config.accounting_standard = accounting_standard
        admission, preflight_extraction = inspect_document(
            input_path, emit, standard=accounting_standard, reader_options=dict(
                preprocess_resize=preprocess_resize, preprocess_grayscale=preprocess_grayscale,
                max_image_width=max_image_width))
        emit(event("extraction", "running", "Extraction, mapping et contrôles comptables"))
        result = self._run_tier_cascade(
            input_path,
            preprocess_resize=preprocess_resize,
            preprocess_grayscale=preprocess_grayscale,
            max_image_width=max_image_width,
            accounting_standard=accounting_standard,
            preflight_extraction=preflight_extraction,
            progress_callback=emit,
        )
        if not result.tables:
            from document_intake import DocumentRejected
            raise DocumentRejected(dict(code="extraction_empty", accepted=False,
                reason="Document financier détecté, mais aucun tableau exploitable extrait.",
                action="Déposez une copie plus lisible ou vérifiez la structure du document."))
        emit(event("extraction", "completed", "Extraction et contrôles terminés"))
        emit(event("framing", "running", "Cadrage des états et des unités"))
        self._attach_statements(result)
        self._attach_document_unit(result)
        emit(event("framing", "completed", "États et unités cadrés"))
        emit(event("review", "waiting", "Résultat disponible — validation humaine attendue"))
        result.metadata.update(admission=admission, workflow_events=events)
        return result

    def _attach_document_unit(self, result: "ExtractionResult") -> None:
        """Record the document-level unit on the result — WP-2.9b, VAL-7/E-2.

        ONE place. Stage 3b binds the unit per TABLE, and until now the only way
        to learn what unit a DOCUMENT states was to re-derive it from the
        canonical tables — which the golden recorder does and every other
        consumer (the HITL header, an export, an API response) did not. The nine
        literal ``ExtractionResult(...)`` returns in this module are not nine
        places to write it: this runs after the cascade and after
        ``_attach_statements``, so it sees the tables the pipeline actually
        emitted whichever tier won and after the statement re-keying, which is
        exactly the set the record is read off.

        ``{"value", "source", "conflict"}`` or ``None`` when no table of the
        document states a unit. **E-2: a record, never a licence to rescale.**
        A ``conflict`` is a question for a human — nothing here reads or writes
        a ``parsed_value``.
        """
        try:
            from accounting.unit_detector import document_unit
        except ImportError:  # pragma: no cover — additive, never fatal
            logger.debug("Unit detector not available — document unit not recorded")
            return
        try:
            unit = document_unit(
                [ct for ct in (result.canonical_tables or []) if ct is not None]
            )
        except Exception as exc:  # noqa: BLE001 — a record may never break a run
            logger.debug(f"Document unit not recorded — {exc}")
            return
        result.metadata = {**(result.metadata or {}), "unit": unit}
        if unit is not None:
            logger.info(
                "Document unit: %s (source=%s, conflict=%s)",
                unit.get("value"), unit.get("source"), unit.get("conflict"),
            )

    def _attach_statements(self, result: "ExtractionResult") -> None:
        """Authoritative post-OCR classification — one explicit stage.

        Runs on whatever the cascade returned, so the inventory always describes
        the tables actually emitted, whichever tier won. Classification used to
        happen in five unreconciled places, and the only pre-OCR pass threw its
        answer away.

        **Advisory for now:** it records ``metadata['statements']`` and may only
        ever RAISE ``hitl_required``, never clear it. Enrichment, validation and
        the matcher are untouched — routing on the gate is a later, breaking
        step. Failure here can never break extraction.
        """
        try:
            from accounting.statement_builder import (
                GATE_QUARANTINED, build_statements, statements_to_dicts,
            )
        except ImportError:
            logger.debug("Statement builder not available")
            return

        try:
            import pandas as pd
            frames = [
                t if isinstance(t, pd.DataFrame) else pd.DataFrame(t)
                for t in (result.tables or [])
            ]
            provenance = (result.metadata or {}).get("table_provenance")
            # OCR-7: a tagged Tier-0 fragment is a PIECE of a statement, not a
            # statement. Classified on its own it would quarantine and put a
            # two-row grid in front of a reviewer, so it is held out until
            # CLS-1/CLS-4 reassemble the pieces. Blanked rather than removed:
            # ``build_statements`` yields no unit for an empty frame, and every
            # surviving index keeps pointing at the same entry of
            # ``result.tables`` — which is what the ``table_indices`` re-key of
            # ``canonical_tables`` below depends on.
            if provenance and len(provenance) == len(frames):
                frames = [
                    pd.DataFrame() if (p or {}).get("fragment") else f
                    for f, p in zip(frames, provenance)
                ]
            units = build_statements(frames, provenance=provenance)
        except Exception as exc:  # noqa: BLE001 — advisory, never fatal
            logger.warning(f"Statement builder failed — {exc}")
            return

        if not units:
            return

        meta = {
            **(result.metadata or {}),
            "statements": statements_to_dicts(units),
            # v2: `canonical_tables` is keyed per STATEMENT (index-aligned with
            # `statements`), not per table. `tables` is unchanged. Stored results
            # carry the version so a consumer can tell which keying it is holding.
            "schema_version": SCHEMA_VERSION,
        }

        # Re-key canonical_tables from per-TABLE to per-STATEMENT via each
        # statement's table_indices back-pointer. Entries whose table produced no
        # canonical output become None placeholders rather than being dropped —
        # silently shortening the list would slide every later statement onto the
        # wrong canonical table.
        cts = list(result.canonical_tables or [])
        if cts:
            per_statement = []
            for unit in units:
                parts = [
                    cts[i] for i in unit.table_indices
                    if i < len(cts) and cts[i] is not None
                ]
                if not parts:
                    per_statement.append(None)
                elif len(parts) == 1:
                    per_statement.append(parts[0])
                else:
                    # A statement built from several fragments (a continuation
                    # across a page break) must carry ALL of its rows. Taking
                    # only the first fragment would silently drop the rest from
                    # the canonical output while `tables` still held them.
                    head = parts[0]
                    for extra in parts[1:]:
                        head.rows.extend(extra.rows)
                    try:
                        head.recompute_aggregates()
                    except Exception:  # noqa: BLE001
                        pass
                    per_statement.append(head)
            result.canonical_tables = per_statement

        quarantined = [u for u in units if u.gate == GATE_QUARANTINED]
        if quarantined:
            # Conservative direction only: an unrecognized statement earns a
            # look, it can never talk the pipeline out of one.
            meta["hitl_required"] = True
            logger.info(
                "Statements quarantined (%d) → HITL: %s",
                len(quarantined),
                ", ".join(f"#{u.id}({u.gate_reason})" for u in quarantined),
            )
            self._enforce_quarantine(result, units, meta)
        result.metadata = meta

    def _enforce_quarantine(self, result, units, meta: dict) -> None:
        """Stop a quarantined statement from counting as validated output.

        The gate deliberately runs on the FINAL tables rather than the raw
        per-tier frames: on this corpus a Compte de Résultat classifies with
        margin 18 once the rows are exploded and voted, but margin 1 on Tier-0's
        3-row mega-cell frame. Gating earlier would quarantine real statements —
        the over-eager-gate data loss the design warned about.

        Running last means enrichment has already touched every table, so the
        gate cannot un-run the matcher. What it CAN do — and what "should not
        continue through the process" means at this boundary — is refuse to let
        an unrecognized statement contribute validated numbers: its canonical
        table is marked, and the validation report is recomputed over the
        surviving statements only, so its checks stop counting toward the QCS
        semantic layer.
        """
        from accounting.statement_builder import GATE_QUARANTINED

        keep_indices, drop_indices = [], []
        for slot, unit in enumerate(units):
            (drop_indices if unit.gate == GATE_QUARANTINED else keep_indices).append(slot)

        # The caller already raised `hitl_required` for the quarantine; give the
        # flag a reason, or the reviewer is shown a "Yes" with nothing next to it.
        if drop_indices and not meta.get("hitl_reason"):
            meta["hitl_reason"] = (
                f"statement_quarantined: {len(drop_indices)} statement(s) did not "
                "classify and are excluded from the validated output"
            )

        cts = list(result.canonical_tables or [])
        for slot in drop_indices:
            if slot < len(cts) and cts[slot] is not None:
                cts[slot].metadata = {
                    **(cts[slot].metadata or {}),
                    "gate": GATE_QUARANTINED,
                    "gate_reason": units[slot].gate_reason,
                }

        if not keep_indices:
            meta["validation_report"] = None
            logger.warning("Every statement was quarantined — no validated output")
            return

        try:
            from accounting.ensemble import canonical_tables_to_parsed_dataframes
            from accounting.rule_engine import validate_with_rules

            surviving = [cts[s] for s in keep_indices if s < len(cts) and cts[s] is not None]
            if not surviving:
                return
            frames = [
                f.drop(columns=["label_raw"], errors="ignore")
                for f in canonical_tables_to_parsed_dataframes(surviving)
            ]
            report = validate_with_rules(
                tables=frames, standard=self.config.accounting_standard,
                run_v2=True, run_v3=True, run_v4=True,
            )
            meta["validation_report"] = report.to_dict()
            logger.info(
                "Quarantine enforced: revalidated %d surviving statement(s) "
                "(%d/%d checks, critical=%d)",
                len(surviving), report.passed_checks, report.total_checks,
                report.critical_failures,
            )
            self._recompute_qcs_after_quarantine(result, meta, frames, report)
        except Exception as exc:  # noqa: BLE001 — advisory, never fatal
            logger.warning(f"Quarantine revalidation failed — {exc}")

    def _recompute_qcs_after_quarantine(self, result, meta: dict, frames, report) -> None:
        """RTE-6 — re-score the document over what SURVIVED the gate.

        Quarantine already recomputed the validation report; the QCS built on
        top of that report was left at its pre-quarantine value. So a document
        could be told "these statements do not count" and still carry a score
        that counted them — the semantic layer of a score describing tables the
        result no longer stands behind.

        The recomputation reuses the intrinsic grades (an OCR grade is a fact
        about the page image, unchanged by which statement was dropped), the
        surviving frames for the heuristic layer, and the post-quarantine report
        for the semantic layer.

        Two deliberate limits:

        * ``qcs_score`` is overwritten ONLY when ``score_kind == "qcs"``. At
          Tier 3 that field holds Q_table (RTE-10) — a consensus measure this
          function does not compute and must not silently replace with a QCS.
          The recomputed number is still recorded, under its own key, for every
          document.
        * ``hitl_required`` may only ever be RAISED, like every other writer on
          this path. Losing statements can add a reason for a human to look; it
          can never be the reason one stops being needed.
        """
        try:
            from qcs.calculator import calculate_qcs
        except ImportError:
            return

        cd = result.confidence_details or {}
        recomputed = calculate_qcs(
            ocr_grade=cd.get("ocr_grade") or "FAIR",
            layout_grade=cd.get("layout_grade") or "FAIR",
            parse_grade=cd.get("parse_grade") or "FAIR",
            low_grade=cd.get("low_grade") or "FAIR",
            text=result.text or "",
            tables=frames,
            validation_report=report,
            corrections_made=int(cd.get("corrections_made") or 0),
        )

        before = result.qcs_score
        meta["qcs_after_quarantine"] = round(recomputed.qcs_score, 4)
        meta["qcs_after_quarantine_grade"] = recomputed.grade

        if meta.get("score_kind") == "qcs":
            result.qcs_score = recomputed.qcs_score
            meta["qcs_grade"] = recomputed.grade

        if recomputed.needs_hitl and not meta.get("hitl_required"):
            meta["hitl_required"] = True
        if meta.get("hitl_required") and not meta.get("hitl_reason"):
            meta["hitl_reason"] = recomputed.hitl_reason or ""

        logger.info(
            "Quarantine QCS recompute: %.3f → %.3f (grade=%s, score_kind=%s, applied=%s)",
            before if isinstance(before, (int, float)) else float("nan"),
            recomputed.qcs_score, recomputed.grade,
            meta.get("score_kind"), meta.get("score_kind") == "qcs",
        )

    @staticmethod
    def _carry_hitl(result: "ExtractionResult", tier: int) -> "ExtractionResult":
        """RTE-1 — put the QCS module's ``needs_hitl`` where the worker reads it.

        ``calculate_qcs`` has always decided this: ≥ ``tau_excellent`` (0.90) is
        "no human needed", 0.75–0.90 is "accepted, and a human still looks". The
        cascade accepts at ``tau_fast_high`` (0.75) and returned the result
        without ever copying that decision out of ``confidence_details`` — so
        ``worker/tasks.py``, which reads ``metadata['hitl_required']``, took the
        ``False`` default and persisted "no review needed" for every document in
        the band. Two bars by design; one of them was simply not plumbed.

        This is plumbing, not a threshold: nothing here compares a score to
        anything. It also only ever RAISES — a stage that already asked for a
        human keeps its answer, and the four metadata keys are written on every
        accepted result so a consumer never has to guess whether the absence of
        a flag means "no" or "nobody said".
        """
        details = result.confidence_details or {}
        meta = dict(result.metadata or {})

        needs = bool(details.get("needs_hitl", False))
        meta["hitl_required"] = bool(meta.get("hitl_required", False)) or needs
        if meta["hitl_required"] and not meta.get("hitl_reason"):
            meta["hitl_reason"] = details.get("hitl_reason") or ""
        meta.setdefault("hitl_reason", "")

        # One grade vocabulary (RTE-2): the four lowercase words the QCS bands
        # produce — excellent / good / fair / poor. No High / Medium / Low.
        if meta.get("qcs_grade") is None:
            meta["qcs_grade"] = details.get("qcs_grade")
        # RTE-10: say which measure `qcs_score` is holding. Tiers 0-2 hold a QCS;
        # Tier 3 holds the ensemble's Q_table, which is a different quantity on a
        # different scale and must never be silently compared with one.
        meta.setdefault("score_kind", "q_table" if tier == 3 else "qcs")

        result.metadata = meta
        return result

    def _run_tier_cascade(
        self,
        input_path: Path,
        preprocess_resize: bool = True,
        preprocess_grayscale: bool = True,
        max_image_width: int = 2000,
        accounting_standard: str = "NCT",
        preflight_extraction=None,
        progress_callback=None,
    ) -> ExtractionResult:
        """The Tier 0→3 cascade. See :meth:`process_document`."""
        logger.info(f"Processing: {input_path}")

        from document_intake import event
        def announce(message):
            if progress_callback:
                progress_callback(event("extraction", "running", message))
        # Track tier attempts for transparency
        tier_attempts = []
        
        # Step 1: Pre-processing
        # NB: there is deliberately no pre-OCR classification here. The old
        # `_classify_document` pooled the first three pages, returned ONE type
        # for a document that routinely holds several statements, and fell back
        # to "bilan" on four separate failure paths — and nothing ever read its
        # answer. Classification now happens once, after extraction, in
        # `_attach_statements`, where it sees the reconciled tables and can tell
        # the statements apart (see context/Known-Gaps-*.md, D3).
        images = self._extract_images(input_path)

        logger.info(f"Extracting: {input_path.name} (embedded images: {len(images)})")
        
        # Step 2: Tier 0 - Native extraction (digital PDFs with embedded text)
        tier0_result = None
        if input_path.suffix.lower() == '.pdf':
            announce('Lecture native des tableaux PDF')
            tier0_result = self._tier0_native(input_path)
            # §18 short-circuit: green-flag is the authoritative gate. QCS is
            # checked too as a guard against gross quality failures, but a
            # green-flag pass *plus* QCS ≥ tau_native_high is required to skip
            # Tier 1.
            green_passed = bool(tier0_result.metadata.get("green_flag_passed"))
            qcs_ok = tier0_result.qcs_score >= self.config.tau_native_high
            tier0_accepted = green_passed and qcs_ok

            tier_attempts.append({
                "tier": 0,
                "name": "Native PDF",
                "qcs": round(tier0_result.qcs_score, 3),
                "threshold": self.config.tau_native_high,
                "green_flag": green_passed,
                "passed": tier0_accepted,
            })
            if tier0_accepted:
                logger.info(
                    f"Tier 0 accepted (green_flag=PASS, QCS={tier0_result.qcs_score:.3f})"
                )
                tier0_result.confidence_details['tier_attempts'] = tier_attempts
                return self._carry_hitl(tier0_result, tier=0)
            # Tier 0 below threshold — its findings will be forwarded to Tier 1
            if tier0_result.text or tier0_result.tables:
                gates_failed = tier0_result.metadata.get("green_flag_failures") or []
                logger.info(
                    f"Tier 0 below threshold (green_flag={'PASS' if green_passed else 'FAIL'}, "
                    f"QCS={tier0_result.qcs_score:.3f}) — forwarding to Tier 1 "
                    f"(text={len(tier0_result.text)} chars, tables={len(tier0_result.tables)}, "
                    f"gate_failures={len(gates_failed)})"
                )
        
        # Step 3: Tier 1 - Fast OCR (with Tier 0 context if available)
        announce('Lecture OCR et mapping L1 — contrôles comptables')
        result = self._tier1_fast_ocr(
            input_path, images,
            preprocess_resize=preprocess_resize,
            preprocess_grayscale=preprocess_grayscale,
            max_image_width=max_image_width,
            accounting_standard=accounting_standard,
            tier0_result=tier0_result,
            preflight_extraction=preflight_extraction,
        )
        tier_attempts.append({
            "tier": 1, 
            "name": "Docling OCR", 
            "qcs": round(result.qcs_score, 3),
            "threshold": self.config.tau_fast_high,
            "passed": result.qcs_score >= self.config.tau_fast_high
        })
        if result.qcs_score >= self.config.tau_fast_high:
            # RTE-1: "Tier 1 accepted" is a statement about escalation — this
            # document does not need another engine. It is NOT a statement that
            # nobody needs to look at it: that second question is the QCS
            # report's `needs_hitl`, and `_carry_hitl` is where its answer stops
            # being thrown away. A document at QCS 0.78 is accepted here AND
            # queued for review, which is the design (0.75 cascade bar, 0.90
            # no-human bar), not a contradiction.
            logger.info(f"Tier 1 accepted (QCS={result.qcs_score:.3f})")
            result.confidence_details['tier_attempts'] = tier_attempts
            return self._carry_hitl(result, tier=1)
        
        # Step 4: Tier 2 - VLM (placeholder for now)
        logger.info("Escalating to Tier 2 (VLM)...")
        tier1_result = result  # Keep Tier 1 result for the ensemble
        tier2_result = self._tier2_vlm(images)
        tier_attempts.append({
            "tier": 2,
            "name": "VLM",
            "qcs": round(tier2_result.qcs_score, 3),
            "threshold": self.config.tau_vlm_med,
            "passed": tier2_result.qcs_score >= self.config.tau_vlm_med
        })
        if tier2_result.qcs_score >= self.config.tau_vlm_med:
            logger.info(f"Tier 2 accepted (QCS={tier2_result.qcs_score:.3f})")
            tier2_result.confidence_details['tier_attempts'] = tier_attempts
            return self._carry_hitl(tier2_result, tier=2)

        # Step 5: Tier 3 - Ensemble consensus (LV-ROVER vote + Consensus-Entropy routing)
        announce('Réconciliation des moteurs et calcul du score')
        return self._tier3_ensemble(
            tier0_result=tier0_result,
            tier1_result=tier1_result,
            tier2_result=tier2_result,
            tier_attempts=tier_attempts,
        )
    
    def _extract_images(self, path: Path) -> list:
        """Extract embedded images from DOCX or PDF pages."""
        images = []
        
        if path.suffix.lower() == '.docx':
            # Extract images from DOCX
            try:
                from docx import Document
                doc = Document(path)
                for rel in doc.part.rels.values():
                    if "image" in rel.reltype:
                        images.append(rel.target_part.blob)
                logger.info(f"Extracted {len(images)} images from DOCX")
            except Exception as e:
                logger.error(f"DOCX image extraction failed: {e}")
        
        elif path.suffix.lower() in ['.jpg', '.jpeg', '.png']:
            # Direct image
            images.append(path.read_bytes())
        
        return images
    
    def _tier0_native(self, path: Path) -> ExtractionResult:
        """Tier 0: Native PDF text extraction using PyMuPDF.

        For digitally-generated PDFs (embedded text, not scanned images),
        this extracts text and tables natively — no OCR needed.

        Advantages over Tier 1 (Docling OCR):
        - Perfect text encoding (no garbled accents)
        - Native table extraction (no cell-reference issues)
        - Faster execution (no model inference)

        Falls through to Tier 1 if:
        - PDF is scanned (no embedded text)
        - Table extraction fails or finds no tables
        - QCS < tau_native_high (0.95)
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.warning("PyMuPDF not installed — skipping Tier 0")
            return ExtractionResult(
                text="", tables=[], qcs_score=0.0, tier_used=0,
                confidence_details={"status": "fitz_not_available"},
                metadata={}
            )

        # OCR-14: the handle must be closed on EVERY path. The success path
        # closes it after the page loop; before this `finally` the exception
        # path below returned with the file still open, which on Windows blocks
        # deletion of the caller's temp copy.
        pdf_doc = None
        try:
            pdf_doc = fitz.open(str(path))
            num_pages = len(pdf_doc)
            logger.info(f"Tier 0: Opened PDF with {num_pages} page(s)")

            # ---------------------------------------------------------------
            # Step 1: Detect digital vs scanned PDF
            # ---------------------------------------------------------------
            first_page_text = pdf_doc[0].get_text("text").strip()
            if len(first_page_text) < 50:
                logger.info("Tier 0: PDF appears scanned (little embedded text) — skipping")
                pdf_doc.close()
                return ExtractionResult(
                    text="", tables=[], qcs_score=0.0, tier_used=0,
                    confidence_details={"status": "scanned_pdf", "text_length": len(first_page_text)},
                    metadata={}
                )

            # ---------------------------------------------------------------
            # Step 2: Extract text from all pages
            # ---------------------------------------------------------------
            all_text_parts = []
            # Stage 3b input (VAL-7/E-2): each page's text, keyed by its 1-based
            # page number so a table's caption line stays reachable FROM ITS OWN
            # PAGE. ``full_text`` below still joins the non-empty pages and is
            # unchanged, but the join drops the page boundaries — and a caption
            # is only a caption on the page it was written on. Empty pages are
            # kept as "": a page that carries no text is a fact about that page,
            # not a licence to shift every later page's number by one.
            native_page_texts: dict[int, str] = {}
            for page_idx, page in enumerate(pdf_doc):
                page_text = page.get_text("text")
                native_page_texts[page_idx + 1] = page_text
                if page_text.strip():
                    all_text_parts.append(page_text)

            full_text = "\n\n".join(all_text_parts)
            logger.info(f"Tier 0: Extracted {len(full_text)} chars of native text")

            # ---------------------------------------------------------------
            # Step 3: Extract tables natively (two-strategy approach)
            # ---------------------------------------------------------------
            import pandas as pd
            native_tables = []
            # Index-aligned with ``native_tables``: which page each table came
            # from and its column x-offsets. Both are computed during extraction
            # and were previously discarded (page_idx survived only inside log
            # f-strings), leaving nothing downstream able to tell whether two
            # frames are one statement continued across a page break.
            native_provenance: list = []

            def _has_mega_cells(df: pd.DataFrame) -> bool:
                """True if any cell holds a \\n-collapsed mega-cell (>=3 newlines)."""
                for col in df.columns:
                    for val in df[col]:
                        if pd.notna(val) and isinstance(val, str) and str(val).count('\n') >= 3:
                            return True
                return False

            def _maybe_rebuild_by_geometry(page, tab, df: pd.DataFrame) -> pd.DataFrame:
                """Reconstruct rows by y-coordinate when ``df`` has mega-cells.

                ISSUE-9: PyMuPDF returns each column as one \\n-separated mega-cell;
                index-zip explode cross-assigns values when label/value/note part
                counts differ. Rebuild by word geometry instead. Falls back to the
                original ``df`` (and the later ``_explode_merged_rows`` step) on any
                failure or when the rebuild is not strictly larger.
                """
                if not _has_mega_cells(df):
                    return df
                try:
                    rebuilt = _reconstruct_rows_by_geometry(page, tab)
                    if rebuilt is not None and len(rebuilt) > len(df):
                        logger.info(
                            f"Tier 0: rebuilt mega-cell table by geometry "
                            f"({len(df)} → {len(rebuilt)} rows)"
                        )
                        return rebuilt
                except Exception as e:
                    logger.warning(f"Tier 0: geometry reconstruction failed: {e}")
                return df

            def _table_looks_garbled(df: pd.DataFrame) -> bool:
                """Detect if find_tables() produced garbled output.

                Garbled tables have cells with multiple space-separated numbers
                that should be in separate columns (e.g. '95,276,113 41,694,461').
                """
                if len(df.columns) < 2:
                    return True  # single column when expecting multi-column

                import re
                multi_num_re = re.compile(
                    r'\d[\d\s.,]{3,}\s+\d[\d\s.,]{3,}'  # two number groups separated by space
                )
                garbled_cells = 0
                total_cells = 0

                for col_idx in range(1, len(df.columns)):  # skip label column
                    for val in df.iloc[:, col_idx]:
                        if pd.isna(val):
                            continue
                        val_str = str(val).strip()
                        if not val_str:
                            continue
                        total_cells += 1
                        if multi_num_re.search(val_str):
                            garbled_cells += 1

                # If >20% of non-empty cells have concatenated numbers, it's garbled
                return total_cells > 0 and garbled_cells / total_cells > 0.20

            for page_idx, page in enumerate(pdf_doc):
                try:
                    # Strategy 1: "lines" (default — uses vector graphics for grid)
                    tabs = page.find_tables()
                    page_tables = []
                    page_offsets = []   # column x-offsets, aligned with page_tables
                    # OCR-7: which entries of ``page_tables`` are sub-3-row
                    # FRAGMENTS, index-aligned with the two lists above. The
                    # ``< 3`` rule was written to keep find_tables() noise out of
                    # the output, but dropping the frame threw away real statement
                    # pieces along with it — a block split by a page break or by an
                    # interior rule (LVB p6-p9 lost their tables exactly this way).
                    # Keep the frame and TAG it instead: a tagged fragment travels
                    # in ``tables`` + ``table_provenance`` for CLS-1/CLS-4 to
                    # reassemble, and is held out of the ensemble vote and the
                    # statement builder until then — so the noise the rule guards
                    # against still reaches neither.
                    page_fragments = []
                    for tab_idx, tab in enumerate(tabs.tables):
                        df = tab.to_pandas()
                        df = _maybe_rebuild_by_geometry(page, tab, df)
                        if len(df) < 1:
                            continue        # no data row at all — nothing to keep
                        page_tables.append(df)
                        page_offsets.append(_table_x_offsets(tab))
                        page_fragments.append(len(df) < 3)

                    # Check if any table looks garbled. Deliberately computed over
                    # the FULL frames only — that is exactly the set this check saw
                    # before fragments were kept, and a 1-row noise frame (a single
                    # column, which `_table_looks_garbled` always calls garbled)
                    # must not be what decides that a page re-extracts.
                    needs_retry = any(
                        _table_looks_garbled(df)
                        for df, frag in zip(page_tables, page_fragments) if not frag
                    )

                    # The retry gate is UNCHANGED, deliberately. OCR-7 also names
                    # a page that detected a region and kept nothing from it —
                    # a region whose frames came back with no data row — which
                    # this gate can never reach. Widening it was measured and
                    # DEFERRED TO WP-3.0 (P3 diagnosis); do not re-widen it here.
                    #
                    #   * `needs_retry or not page_tables` (the first cut) fires
                    #     on every page with no detected region — a narrative or
                    #     notes page — where `strategy="text"` does not find a
                    #     missed table, it manufactures ONE page-sized
                    #     pseudo-table of 50-70 rows out of the running text: 25
                    #     of 28 pages on air_liquide, 29 of 42 on somocer, 9 of
                    #     57 on ciments_bizerte. Such a frame has >= 3 rows, so
                    #     it is NOT a fragment and it reaches enrichment, the
                    #     vote and the statement inventory. 32 unexpected golden
                    #     diffs, two pinned facts flipped.
                    #   * `needs_retry or (bool(tabs.tables) and not page_tables)`
                    #     — only a DETECTED region that was lost — is sound and
                    #     fires narrowly (air_liquide p13, somocer p25-26, LVB
                    #     p14, COHAPROM p3-p6/p8/p9). It still moved 4 documents:
                    #     the frames it recovers ([61, 16], [41, 9], [17, 9]) are
                    #     not statement tables, and it changes how the ensemble
                    #     merges. It is also NOT the OCR-7 payoff — LVB p6-p9 is
                    #     a Tier-3 pairing loss (CLS-3 / WP-3.1), and the frame
                    #     this leg recovers on LVB is on p14.
                    #
                    # The fragment tagging above is the half that ships: it is the
                    # enabling half for CLS-1/CLS-4 and is inert in every consumer.
                    if needs_retry and page_tables:
                        logger.info(
                            f"Tier 0: Page {page_idx+1} has garbled columns — "
                            f"retrying with strategy='text'"
                        )
                        # Strategy 2: "text" (uses text positions for column boundaries)
                        tabs_text = page.find_tables(strategy="text")
                        text_tables = []
                        text_offsets = []
                        text_fragments = []
                        for tab_idx, tab in enumerate(tabs_text.tables):
                            df = tab.to_pandas()
                            df = _maybe_rebuild_by_geometry(page, tab, df)
                            if len(df) < 1:
                                continue
                            text_tables.append(df)
                            text_offsets.append(_table_x_offsets(tab))
                            text_fragments.append(len(df) < 3)

                        # Use text-strategy tables if they have MORE columns (better split)
                        if text_tables:
                            # ``default=0`` guards a latent crash: bare
                            # ``max()`` over an empty ``page_tables`` raises
                            # ValueError, which the per-page ``except`` would
                            # swallow as a warning. Unreachable while the gate
                            # requires ``page_tables``, and required the moment
                            # WP-3.0 revisits it. The rule itself — adopt only on
                            # a strictly wider split — is unchanged.
                            lines_cols = max(
                                (len(df.columns) for df in page_tables), default=0
                            )
                            text_cols = max(len(df.columns) for df in text_tables)
                            if text_cols > lines_cols:
                                page_tables = text_tables
                                page_offsets = text_offsets   # keep geometry aligned
                                page_fragments = text_fragments
                                logger.info(
                                    f"Tier 0: Text strategy improved: "
                                    f"{lines_cols} → {text_cols} columns"
                                )

                    for df, offsets, is_fragment in zip(
                        page_tables, page_offsets, page_fragments
                    ):
                        native_tables.append(df)
                        prov = {
                            "page_no": page_idx + 1,
                            "x_offsets": offsets,
                        }
                        if is_fragment:
                            # Only a fragment carries the key — a full table's
                            # provenance shape is exactly what it always was.
                            prov["fragment"] = True
                        native_provenance.append(prov)
                        logger.info(
                            f"Tier 0: Page {page_idx+1}: "
                            f"{df.shape[0]} rows × {df.shape[1]} cols"
                            f"{' (fragment)' if is_fragment else ''}"
                        )
                except AttributeError:
                    logger.warning("Tier 0: find_tables() not available — upgrade PyMuPDF >= 1.23")
                    break
                except Exception as e:
                    logger.warning(f"Tier 0: Table extraction error on page {page_idx+1}: {e}")

            pdf_doc.close()

            if not native_tables:
                logger.info("Tier 0: No tables found — falling through to Tier 1")
                return ExtractionResult(
                    text=full_text, tables=[], qcs_score=0.0, tier_used=0,
                    confidence_details={"status": "no_tables_found", "text_length": len(full_text)},
                    metadata={"native_text_available": True}
                )

            logger.info(f"Tier 0: Found {len(native_tables)} table(s) natively")

            # ---------------------------------------------------------------
            # Step 3b: Explode merged cells
            # ---------------------------------------------------------------
            # Some accounting PDFs have physically merged table cells where
            # the entire body is in one row with \n-separated values.
            # Detect and explode these into proper row-per-line-item DFs.

            def _explode_merged_rows(df: pd.DataFrame) -> pd.DataFrame:
                """Explode a DataFrame whose body is collapsed into merged cells.

                Pattern: row 0 = header, row 1 = mega-cell with \\n-separated
                values across all columns, row 2+ = totals. The mega-cell is
                detected when any cell has >= 3 newline characters.
                """
                new_rows = []
                for _, row in df.iterrows():
                    # Check if any cell in this row has multiple newlines
                    max_parts = 1
                    for val in row:
                        if pd.notna(val) and isinstance(val, str) and '\n' in val:
                            parts_count = len(val.split('\n'))
                            max_parts = max(max_parts, parts_count)

                    if max_parts <= 2:
                        # Normal row — pass through
                        new_rows.append(row.tolist())
                    else:
                        # Merged row — explode into max_parts rows
                        split_cols = []
                        for val in row:
                            if pd.notna(val) and isinstance(val, str) and '\n' in val:
                                split_cols.append(val.split('\n'))
                            else:
                                # Single value — pad with empty strings
                                split_cols.append([str(val) if pd.notna(val) else ''] * max_parts)

                        # Pad shorter columns to max_parts
                        for i, parts in enumerate(split_cols):
                            if len(parts) < max_parts:
                                split_cols[i] = parts + [''] * (max_parts - len(parts))

                        for row_idx in range(max_parts):
                            new_rows.append([col[row_idx] for col in split_cols])

                result = pd.DataFrame(new_rows, columns=df.columns)
                return result

            exploded_tables = []
            for i, df in enumerate(native_tables):
                # Detect: any cell with >= 3 newlines indicates a merged table
                has_merged = False
                for col in df.columns:
                    for val in df[col]:
                        if pd.notna(val) and isinstance(val, str) and val.count('\n') >= 3:
                            has_merged = True
                            break
                    if has_merged:
                        break

                if has_merged:
                    exploded = _explode_merged_rows(df)
                    logger.info(
                        f"Tier 0: Table {i+1} had merged cells — "
                        f"exploded {df.shape[0]} rows → {exploded.shape[0]} rows"
                    )
                    exploded_tables.append(exploded)
                else:
                    exploded_tables.append(df)

            native_tables = exploded_tables
            from document_intake import snapshot
            raw_snapshot = snapshot(full_text, native_tables, 'pymupdf')

            # ---------------------------------------------------------------
            # Step 4: Post-processing (same as Tier 1)
            # ---------------------------------------------------------------
            total_corrections = 0
            correction_stats = {}
            standard = self.config.accounting_standard

            try:
                from preprocessing.text_postprocessor import postprocess_text_with_stats, postprocess_dataframe
                full_text, correction_stats = postprocess_text_with_stats(full_text, standard)
                total_corrections = correction_stats.get("total_corrections", 0)
                for df in native_tables:
                    postprocess_dataframe(df, standard)
                logger.info(f"Tier 0: Post-processing made {total_corrections} corrections")
            except ImportError:
                logger.warning("Tier 0: Text post-processor not available")

            # ---------------------------------------------------------------
            # Step 4b: Enrichment Pipeline (§23 DAG Stages 1–10)
            # ---------------------------------------------------------------
            canonical_tables = []
            green_flag_result = None
            try:
                from accounting.enrichment_pipeline import enrich_tables
                canonical_tables = enrich_tables(
                    raw_tables=native_tables,
                    tier=0,
                    provenance=native_provenance,
                    # Stage 3b (VAL-7/E-2): the unit is stated in the caption
                    # line above a table as often as in the table itself. Step 2
                    # keeps each page's text under its own 1-based number, so a
                    # table whose provenance names page 4 is matched against the
                    # text of page 4 — not against the whole document, which is
                    # what the joined ``full_text`` could only ever offer.
                    # Key 0 stays the document-level text (post-processed, as
                    # before) and still seeds the document default for the tables
                    # whose page states nothing.
                    page_texts={0: full_text, **native_page_texts},
                )
                logger.info(
                    f"Tier 0: Enrichment pipeline produced "
                    f"{len(canonical_tables)} canonical tables"
                )

                # OCR-7: carry the fragment tag onto the canonical tables so the
                # ensemble vote can hold them out. Keyed on
                # ``source_table_index`` — the INPUT index enrich_tables records
                # precisely because an empty frame would make the output index
                # differ from it — never on enumerate().
                for _ct in canonical_tables:
                    _src = (_ct.metadata or {}).get("source_table_index")
                    if (isinstance(_src, int) and 0 <= _src < len(native_provenance)
                            and (native_provenance[_src] or {}).get("fragment")):
                        _ct.metadata["fragment"] = True

                # OCR-7: the green flag judges the quality of the STATEMENTS this
                # tier produced, and a tagged fragment is not one — it is a piece
                # held back for CLS-1/CLS-4 and excluded from every consumer
                # downstream. Scoring it here would be the one place a retained
                # fragment could still move the record: its rows are unmatched and
                # its cells unparsed by construction, so it drags `match_rate`
                # down and `parse_fail_rate` up, which changes
                # `green_flag_failures` and `tier_attempts` (both survive into the
                # Tier-3 result via ``best_prior.metadata``) on a document whose
                # extraction is otherwise identical. Measured on LVB before this
                # guard: `green_flag_failures` moved with no other cause.
                # This list is exactly what the gate saw before fragments were
                # kept at all.
                full_canonical = [
                    ct for ct in canonical_tables
                    if not (ct.metadata or {}).get("fragment")
                ]

                # Green-flag gate (§18) — check if Tier 0 quality is sufficient
                if full_canonical:
                    try:
                        from accounting.green_flag import check_green_flag
                        # Compute metrics from enrichment results
                        total_rows = sum(len(ct.rows) for ct in full_canonical)
                        matched_rows = sum(
                            sum(1 for r in ct.rows
                                if r.match_type in ("exact", "fuzzy", "resolved"))
                            for ct in full_canonical
                        )
                        match_rate = matched_rows / max(total_rows, 1)

                        total_numeric = sum(
                            sum(1 for c in r.cells.values() if c.parsed_value is not None)
                            for ct in full_canonical for r in ct.rows
                        )
                        parse_fails = sum(
                            sum(1 for c in r.cells.values()
                                if c.flag == "yellow" and c.raw_value.strip())
                            for ct in full_canonical for r in ct.rows
                        )
                        parse_fail_rate = parse_fails / max(total_numeric, 1)

                        col_conf = max(
                            (ct.metadata.get("column_confidence", 0.0)
                             for ct in full_canonical),
                            default=0.0,
                        ) if any(ct.metadata for ct in full_canonical) else 0.80

                        class_conf = max(
                            (ct.match_rate for ct in full_canonical),
                            default=0.0,
                        )

                        # RTE-7: G4 has never run — the footing verdicts
                        # Stage 8 computed were dropped, so the gate always
                        # took its "no subtotals" default pass. Feed them in
                        # as the signature intends. Stricter only: G4 can now
                        # fail (every subtotal checked and none footed); it
                        # cannot newly pass anything it passed before.
                        # OCR-7: full tables only, like every metric above — a
                        # fragment's subtotals are half a statement's and would
                        # fail G4 for a reason that is not a quality defect.
                        from types import SimpleNamespace
                        footing_results = [
                            SimpleNamespace(footing_valid=v)
                            for ct in full_canonical
                            for v in (ct.metadata.get("footing_results") or [])
                        ]

                        green_flag_result = check_green_flag(
                            match_rate=match_rate,
                            parse_fail_rate=parse_fail_rate,
                            column_confidence=col_conf,
                            footing_results=footing_results,
                            classification_confidence=class_conf,
                        )
                        logger.info(
                            f"Tier 0 green-flag: {'PASSED' if green_flag_result.passed else 'FAILED'} "
                            f"(match={match_rate:.0%}, parse_fail={parse_fail_rate:.0%})"
                        )
                    except ImportError:
                        logger.debug("Tier 0: Green-flag module not available")
            except ImportError:
                logger.debug("Tier 0: Enrichment pipeline not available")

            # ---------------------------------------------------------------
            # Step 5: Run V1-V4 validation (same as Tier 1)
            # ---------------------------------------------------------------
            validation_report = None
            try:
                from accounting.rule_engine import validate_with_rules
                validation_report = validate_with_rules(
                    tables=native_tables,
                    standard=standard,
                    run_v2=True,
                    run_v3=True,
                    run_v4=True,
                )
                logger.info(
                    f"Tier 0 validation: {validation_report.passed_checks}/"
                    f"{validation_report.total_checks} passed, "
                    f"critical={validation_report.critical_failures}"
                )
            except ImportError:
                try:
                    from accounting.validator import validate_extraction
                    validation_report = validate_extraction(native_tables)
                except ImportError:
                    logger.warning("Tier 0: No validator available")

            # ---------------------------------------------------------------
            # Step 6: Calculate QCS with EXCELLENT intrinsic grades
            # (native text = perfect OCR, layout, and parse quality)
            # ---------------------------------------------------------------
            try:
                from qcs.calculator import calculate_qcs

                qcs_report = calculate_qcs(
                    ocr_grade="EXCELLENT",
                    layout_grade="EXCELLENT",
                    parse_grade="EXCELLENT",
                    low_grade="EXCELLENT",
                    text=full_text,
                    tables=native_tables,
                    validation_report=validation_report,
                    corrections_made=total_corrections,
                )
                qcs_score = qcs_report.qcs_score
                qcs_grade = qcs_report.grade
                qcs_details = qcs_report.to_dict()
            except ImportError:
                qcs_score = 0.95 if validation_report and validation_report.overall_passed else 0.5
                qcs_grade = "excellent" if qcs_score >= 0.90 else "good"
                qcs_details = {}

            # Convert tables to list of dicts (same format as Tier 1)
            tables_data = [df.to_dict(orient='records') for df in native_tables]

            # Determine HITL from QCS report or validation
            hitl_required = False
            hitl_reason = ""
            if hasattr(qcs_report, 'needs_hitl'):
                hitl_required = qcs_report.needs_hitl
                hitl_reason = qcs_report.hitl_reason

            return ExtractionResult(
                text=full_text,
                tables=tables_data,
                qcs_score=qcs_score,
                tier_used=0,
                confidence_details={
                    'extraction_method': 'native_pymupdf',
                    'ocr_grade': 'EXCELLENT',
                    'layout_grade': 'EXCELLENT',
                    'parse_grade': 'EXCELLENT',
                    'low_grade': 'EXCELLENT',
                    'qcs_grade': qcs_grade,
                    'hitl_reason': hitl_reason,
                    **qcs_details,
                },
                metadata={
                    'doc_type': 'native_pdf',
                    'table_count': len(native_tables),
                    'page_count': num_pages,
                    'text_length': len(full_text),
                    'native_extraction': True,
                    'raw_snapshot': raw_snapshot,
                    'hitl_required': hitl_required,
                    # RTE-1/RTE-2/RTE-10: the review decision, the grade and the
                    # name of the measure `qcs_score` holds travel WITH the
                    # result, in the one dict every consumer already reads. They
                    # used to live in `confidence_details` (or nowhere), which is
                    # why the worker never saw them.
                    'hitl_reason': hitl_reason,
                    'qcs_grade': qcs_grade,
                    'score_kind': 'qcs',
                    'accounting_standard': standard,
                    'validation_report': validation_report.to_dict() if validation_report else None,
                    'correction_stats': correction_stats,
                    'green_flag_passed': green_flag_result.passed if green_flag_result else None,
                    'green_flag_failures': green_flag_result.failure_reasons if green_flag_result else [],
                    # Per-table {page_no, x_offsets}, index-aligned with `tables`.
                    'table_provenance': native_provenance,
                },
                canonical_tables=canonical_tables,
                raw_canonical_tables=canonical_tables,
            )

        except Exception as e:
            import traceback
            logger.error(f"Tier 0 failed: {e}\n{traceback.format_exc()}")
            return ExtractionResult(
                text="", tables=[], qcs_score=0.0, tier_used=0,
                confidence_details={"error": str(e)},
                metadata={"failed": True}
            )
        finally:
            # Idempotent: the success path already closed it after the page loop.
            if pdf_doc is not None and not pdf_doc.is_closed:
                pdf_doc.close()

    def _tier1_fast_ocr(
        self,
        path: Path,
        images: list,
        preprocess_resize: bool = True,
        preprocess_grayscale: bool = True,
        max_image_width: int = 2000,
        accounting_standard: str = "NCT",
        tier0_result: Optional[ExtractionResult] = None,
        preflight_extraction=None,
    ) -> ExtractionResult:
        """Tier 1: Fast OCR with Docling/TableFormer.

        If tier0_result is provided (from a sub-threshold Tier 0 run), its
        findings are leveraged:
        - Native text feeds the QCS heuristic layer (clean encoding)
        - Tables missing from Docling are supplemented from Tier 0
        """
        try:
            from extraction.docling_extractor import DoclingExtractor
            
            logger.info(f"Tier 1: Processing with Docling (standard={accounting_standard})...")
            extractor = DoclingExtractor(
                language="fr",
                preprocess_resize=preprocess_resize,
                preprocess_grayscale=preprocess_grayscale,
                max_image_width=max_image_width,
                accounting_standard=accounting_standard
            )
            
            # Choose extraction method based on file type
            suffix = path.suffix.lower()
            if preflight_extraction is not None:
                result = preflight_extraction
            elif suffix == '.pdf':
                result = extractor.extract_from_pdf(path)
            elif suffix == '.docx':
                result = extractor.extract_from_docx(path)
            elif suffix in ['.jpg', '.jpeg', '.png', '.tiff', '.bmp']:
                from PIL import Image
                # OCR-4: the EXIF Orientation tag is applied inside
                # `DoclingExtractor._preprocess_image` — the one choke point every
                # image ingress goes through — so the bytes that reach OCR are
                # upright and the transpose is recorded in
                # metadata["orientation_transform"]["images"]. Opening in a `with`
                # closes the file handle once the extractor has materialised the
                # image (PIL's lazy load happens under the transpose/save).
                with Image.open(path) as image:
                    result = extractor.extract_from_image(image)
            else:
                raise ValueError(f"Unsupported file type: {suffix}")
            
            # =============================================================
            # Leverage Tier 0 findings if available
            # =============================================================
            tier0_supplemented = 0
            tier0_text_used = False
            # Indices in `result.tables` that are Tier-0 frames, not Docling's.
            # They must not vote in the Tier-3 ensemble as a "tier1" source.
            supplemented_indices: set[int] = set()

            # OCR-7: supplementation appends Tier-0 frames straight into
            # ``result.tables`` — the list ``build_statements``, the rule engine
            # and QCS all read. A tagged fragment is a piece of a statement and
            # belongs in none of them, so it is not a candidate here, and it must
            # not inflate the count that decides whether Docling missed tables
            # either (that count is why this leg fires at all).
            t0_full_frames: list = []
            if tier0_result and tier0_result.tables:
                t0_prov = (tier0_result.metadata or {}).get("table_provenance") or []
                t0_full_frames = [
                    t for i, t in enumerate(tier0_result.tables)
                    if not ((t0_prov[i] or {}).get("fragment") if i < len(t0_prov) else False)
                ]

            if t0_full_frames and len(t0_full_frames) > len(result.tables):
                # Tier 0 found tables that Docling missed — supplement
                import pandas as pd
                tier0_tables_as_df = []
                for t0_table in t0_full_frames:
                    if isinstance(t0_table, dict):
                        # Already dict-of-records from Tier 0 serialization
                        tier0_tables_as_df.append(pd.DataFrame(t0_table))
                    elif isinstance(t0_table, list):
                        tier0_tables_as_df.append(pd.DataFrame(t0_table))
                    elif isinstance(t0_table, pd.DataFrame):
                        tier0_tables_as_df.append(t0_table)

                if len(tier0_tables_as_df) > len(result.tables):
                    # Append Tier 0 tables that Docling didn't find
                    # (Tier 0 extracted more tables natively)
                    for extra_df in tier0_tables_as_df[len(result.tables):]:
                        supplemented_indices.add(len(result.tables))
                        result.tables.append(extra_df)
                        tier0_supplemented += 1
                    logger.info(
                        f"Tier 1: Supplemented {tier0_supplemented} table(s) from Tier 0 native extraction"
                    )

            # Convert tables to list of dicts for JSON serialization
            tables_data = []
            for df in result.tables:
                tables_data.append(df.to_dict(orient='records'))

            # =============================================================
            # Enrichment Pipeline (§23 DAG Stages 1–10) — Tier 1
            # =============================================================
            # Docling already records each table's page (and, for DOCX, which
            # embedded image it came from) while capturing the HITL overlay
            # geometry — surface it as provenance instead of letting it stay
            # locked inside the overlay payload.
            t1_boxes = (result.metadata or {}).get("table_boxes") or []
            t1_provenance = [
                {
                    "page_no": (g or {}).get("page_no"),
                    "image_index": (g or {}).get("image_index"),
                    "x_offsets": sorted({
                        round(float(c["l"]), 1)
                        for c in ((g or {}).get("cells") or []) if "l" in c
                    }) if g else [],
                }
                for g in t1_boxes
            ]

            # Stage 3b input (WP-2.9b), taken off the extractor and NOT carried
            # further: `page_texts` is an extraction-boundary hand-off, read here
            # and needed nowhere downstream. The return below spreads
            # `**result.metadata` into `ExtractionResult.metadata`, so leaving it
            # in place would put a second full copy of the document's text into
            # every result, session and dump taken of one.
            t1_page_texts = {}
            if isinstance(result.metadata, dict):
                t1_page_texts = result.metadata.pop("page_texts", None) or {}

            canonical_tables_t1: list = []
            try:
                from accounting.enrichment_pipeline import enrich_tables
                if result.tables:
                    canonical_tables_t1 = enrich_tables(
                        raw_tables=result.tables,
                        tier=1,
                        provenance=t1_provenance,
                        # Stage 3b (VAL-7/E-2). The extractor now keeps the text
                        # of each page under its 1-based number, alongside the
                        # page number `table_boxes` already gave every table, so
                        # a Tier-1 table is matched against the caption on ITS
                        # page. Key 0 stays the document-level text, which is all
                        # a document with no per-page provenance can offer and is
                        # what still seeds the document default.
                        page_texts={0: result.text or "", **t1_page_texts},
                    )
                    logger.info(
                        f"Tier 1: Enrichment pipeline produced "
                        f"{len(canonical_tables_t1)} canonical tables"
                    )
            except ImportError:
                logger.debug("Tier 1: Enrichment pipeline not available")
            except Exception as exc:  # noqa: BLE001 — keep tier alive
                logger.warning(f"Tier 1: Enrichment failed — {exc}")

            # Preserve Tier 1's own enrichment output *before* any cross-tier
            # merge — the Tier 3 ensemble votes over genuine per-engine tables,
            # not the already-reconciled product (which would double-count Tier 0).
            #
            # Supplemented frames are excluded here for the same reason: they ARE
            # Tier-0 tables, appended above because Docling missed them, and they
            # were enriched with tier=1 only because that is the tier doing the
            # enriching. Leaving them in makes the ensemble see one engine's data
            # twice and score it as two independent engines agreeing — fabricated
            # consensus, which inflates Q_table and can auto-accept a table no
            # second engine ever read. They remain in `result.tables` and in the
            # reconciled `canonical_tables`, so nothing is lost from the output;
            # they simply do not get a second vote.
            raw_canonical_tables_t1 = [
                ct for ct in canonical_tables_t1
                if ct.metadata.get("source_table_index") not in supplemented_indices
            ]
            if supplemented_indices:
                logger.info(
                    "Tier 1: %d supplemented Tier-0 table(s) excluded from the "
                    "ensemble vote (they are not a second engine)",
                    len(canonical_tables_t1) - len(raw_canonical_tables_t1),
                )

            # =============================================================
            # Dual-tier reconciliation (§10 + §20) — Tier 0 ⨝ Tier 1
            # When Tier 0 produced canonical_tables (digital PDF), merge
            # them with Tier 1's results: trust Tier 0 numbers, flag
            # disagreements, attach a per-cell conflict report.
            # =============================================================
            tier0_canonical = []
            if tier0_result is not None:
                tier0_canonical = list(tier0_result.canonical_tables or [])
                # Skip reconciliation if Tier 0 yielded only serialized dicts
                # (e.g. older runs) — we need live CanonicalTable objects.
                if tier0_canonical and not _all_canonical_table_objects(tier0_canonical):
                    logger.debug(
                        "Tier 1: Tier 0 canonical_tables are serialized — "
                        "skipping reconciliation"
                    )
                    tier0_canonical = []
                # OCR-7: a tagged Tier-0 fragment is a PIECE of a statement, and a
                # piece is an input to nothing until CLS-1/CLS-4 reassemble it —
                # not to this reconciliation, not to the ensemble vote, not to the
                # statement builder. Paired by identity it would merge a partial
                # block into a whole Tier-1 statement (or arrive as a spurious
                # "tier0_only" table); it stays in Tier-0 ``tables`` +
                # ``table_provenance`` carrying ``fragment: True`` instead.
                tier0_canonical = [
                    ct for ct in tier0_canonical
                    if not (ct.metadata or {}).get("fragment")
                ]

            reconciliation_report: dict = {}
            if tier0_canonical and canonical_tables_t1:
                try:
                    from accounting.reconciliation import reconcile_dual_tier
                    reconciled = reconcile_dual_tier(
                        tier0_tables=tier0_canonical,
                        tier1_tables=canonical_tables_t1,
                    )
                    canonical_tables_t1 = reconciled
                    total_conflicts = sum(
                        len(ct.conflicts or []) for ct in reconciled
                    )
                    avg_coverage = (
                        sum(ct.metadata.get("alignment_coverage", 0.0) for ct in reconciled)
                        / max(len(reconciled), 1)
                    )
                    reconciliation_report = {
                        "applied": True,
                        "table_count": len(reconciled),
                        "conflict_count": total_conflicts,
                        "avg_alignment_coverage": round(avg_coverage, 3),
                    }
                    logger.info(
                        f"Tier 1: Dual-tier reconciliation produced "
                        f"{len(reconciled)} merged tables, "
                        f"{total_conflicts} cell conflicts, "
                        f"avg_coverage={avg_coverage:.0%}"
                    )
                except ImportError:
                    logger.debug("Tier 1: Reconciliation module not available")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Tier 1: Reconciliation failed — {exc}")

            # =============================================================
            # Run full validation hierarchy (V1-V4) with statement segmentation
            # Uses configurable rule engine with JSON rule definitions
            # =============================================================
            validation_report = None
            try:
                from accounting.rule_engine import validate_with_rules
                if result.tables:
                    validation_report = validate_with_rules(
                        tables=result.tables,
                        standard=accounting_standard,
                        run_v2=True,   # Cross-statement coherence
                        run_v3=True,   # Anomaly detection
                        run_v4=True,   # Ratio computation
                    )
                    logger.info(
                        f"Validation (V1-V4): {validation_report.passed_checks}/{validation_report.total_checks} passed, "
                        f"critical_failures={validation_report.critical_failures}, "
                        f"severity_weighted={validation_report.severity_weighted_score}"
                    )
            except ImportError:
                # Fallback to basic validator if rule engine not available
                try:
                    from accounting.validator import validate_extraction
                    if result.tables:
                        validation_report = validate_extraction(result.tables)
                except ImportError:
                    logger.warning("No validator available — semantic layer will default to 0.5")

            # =============================================================
            # Calculate QCS using grade-mapped 3-layer architecture
            # Includes: low_grade pre-gate, severity-weighted semantic,
            #           critical_failures HITL override
            # =============================================================
            try:
                from qcs.calculator import calculate_qcs

                qcs_report = calculate_qcs(
                    # Layer 1: Intrinsic (grade-mapped, table_score excluded)
                    ocr_grade=result.ocr_grade,
                    layout_grade=result.layout_grade,
                    parse_grade=result.parse_grade,
                    # Pre-gate
                    low_grade=result.low_grade,
                    # Layer 2: Heuristic (from OCR text — better accent handling)
                    text=result.text,
                    tables=result.tables,
                    # Layer 3: Semantic (severity-weighted validation)
                    validation_report=validation_report,
                    # Correction penalty
                    corrections_made=result.corrections_made,
                )

                qcs_score = qcs_report.qcs_score
                qcs_grade = qcs_report.grade
                qcs_details = qcs_report.to_dict()

            except ImportError:
                # Fallback if QCS module not available
                qcs_score = result.confidence if result.confidence > 0 else 0.5
                qcs_grade = "unknown"
                qcs_details = {}

            # =============================================================
            # RTE-3 — the SAME QCS, measured a second time with the grades
            # Docling actually reported. MEASUREMENT, NOT POLICY.
            #
            # Nothing below this point reads `qcs_docling`. It does not route,
            # it does not gate, it does not flag; `qcs_score` above is still the
            # only score the cascade, the HITL decision and the record's `qcs`
            # ever see, and it is still computed from the grades this pipeline
            # has always used. The plan's ruling on RTE-3 is "record now, adopt
            # into routing in P7 (WP-7.2, after MEA-7 calibration)" and
            # decisions Q5-A / G-4 forbid widening any automation before it has
            # been measured — so the adoption delta is written down, per
            # document, for the package that will make that call.
            #
            # Measured pre-quarantine, at the same moment as `qcs_score` above,
            # so the two are directly comparable. `_enforce_quarantine` may
            # later recompute `qcs_score` over the surviving statements (RTE-6);
            # it does not recompute this one, so read the pair here — both
            # numbers are in `confidence_details` — rather than against the
            # record's post-quarantine `qcs`.
            # =============================================================
            docling_grades = _docling_grades_for_measurement(
                (result.metadata or {}).get("docling_confidence"))
            qcs_docling = None
            qcs_docling_grade = None
            try:
                from qcs.calculator import calculate_qcs

                _docling_report = calculate_qcs(
                    ocr_grade=docling_grades["ocr"],
                    layout_grade=docling_grades["layout"],
                    parse_grade=docling_grades["parse"],
                    low_grade=docling_grades["low"],
                    # Every other input identical to the call above, so the ONLY
                    # difference between the two numbers is the four grades.
                    text=result.text,
                    tables=result.tables,
                    validation_report=validation_report,
                    corrections_made=result.corrections_made,
                )
                qcs_docling = round(_docling_report.qcs_score, 3)
                qcs_docling_grade = _docling_report.grade
            except ImportError:
                pass

            grades_in_use = {
                "ocr": result.ocr_grade, "layout": result.layout_grade,
                "parse": result.parse_grade, "low": result.low_grade,
            }
            logger.info(
                "Tier-1 QCS %.3f (%s) from grades IN USE %s — this is the routing "
                "score. Docling's grades %s would give %s (%s); recorded as "
                "qcs_docling for P7/WP-7.2 calibration and used for nothing.",
                qcs_score, qcs_grade, grades_in_use, docling_grades,
                qcs_docling, qcs_docling_grade,
            )

            # RTE-1 — the QCS report's review decision, lifted into `metadata`.
            # `_run_tier_cascade` re-asserts it on the accept path; writing it
            # here too means a Tier-1 result carries its own answer wherever it
            # goes — including into Tier 3 as `best_prior`, and into the UI —
            # rather than having it reconstructed by whoever happens to look.
            t1_hitl = bool(qcs_details.get("needs_hitl", False))
            t1_hitl_reason = qcs_details.get("hitl_reason") or ""

            # RTE-4 — the empty-read floor. Docling's grades describe how
            # confidently it read what it found; they cannot tell you it found
            # nothing. The floor reads the per-page evidence instead and FLAGS.
            t1_hitl, t1_hitl_reason = _apply_read_floor(
                t1_hitl, t1_hitl_reason, (result.metadata or {}).get("read_floor"),
            )

            return ExtractionResult(
                text=result.text,
                tables=tables_data,
                qcs_score=qcs_score,
                tier_used=1,
                confidence_details={
                    'docling_confidence': result.confidence,
                    'ocr_grade': result.ocr_grade,
                    'layout_grade': result.layout_grade,
                    'parse_grade': result.parse_grade,
                    'low_grade': result.low_grade,
                    'qcs_grade': qcs_grade,
                    # RTE-3 — the two grade sets, under names that say which is
                    # which. `grades_in_use` are the four `qcs_score` above was
                    # computed from; `docling_grades` are what Docling reported
                    # and what `qcs_docling` was computed from. They are spelled
                    # out separately so that nothing here can be read as "the
                    # routing score uses Docling's grades" — it does not, and
                    # will not until P7/WP-7.2 says so.
                    'grades_in_use': grades_in_use,
                    'docling_grades': docling_grades,
                    'qcs_docling': qcs_docling,
                    'qcs_docling_grade': qcs_docling_grade,
                    **qcs_details,
                },
                metadata={
                    # Extraction METHOD, matching Tier 0's 'native_pdf'. This key
                    # used to hold a statement type here and a method there — two
                    # incompatible domains under one name, which is why nothing
                    # could safely read it. Statement types live in
                    # metadata['statements'] now.
                    'doc_type': 'docling_ocr',
                    'table_count': len(result.tables),
                    'preprocess_resize': preprocess_resize,
                    'preprocess_grayscale': preprocess_grayscale,
                    'validation_report': validation_report.to_dict() if validation_report else None,
                    'tier0_text_used': tier0_text_used,
                    'tier0_tables_supplemented': tier0_supplemented,
                    'reconciliation': reconciliation_report,
                    # Per-table {page_no, image_index, x_offsets}.
                    'table_provenance': t1_provenance,
                    **result.metadata,
                    # AFTER the extractor's own metadata on purpose: these four
                    # are this stage's verdict on this stage's output, and an
                    # upstream key of the same name must not quietly win.
                    'hitl_required': t1_hitl,
                    'hitl_reason': t1_hitl_reason,
                    'qcs_grade': qcs_grade,
                    'score_kind': 'qcs',
                    # RTE-3 — what the QCS WOULD be if the cascade took
                    # Docling's grades. Recorded beside the real one, read by
                    # nothing, and the input to the P7/WP-7.2 adoption decision.
                    # `score_kind` above describes `qcs_score`, not this.
                    'qcs_docling': qcs_docling,
                    'qcs_docling_grade': qcs_docling_grade,
                },
                canonical_tables=canonical_tables_t1,
                raw_canonical_tables=raw_canonical_tables_t1,
            )
        except Exception as e:
            import traceback
            logger.error(f"Tier 1 extraction failed: {e}\n{traceback.format_exc()}")
            # Return low-confidence result to trigger escalation
            return ExtractionResult(
                text="",
                tables=[],
                qcs_score=0.0,
                tier_used=1,
                confidence_details={'error': str(e)},
                metadata={'doc_type': 'docling_ocr', 'failed': True}
            )
    
    def _tier2_vlm(self, images: list) -> ExtractionResult:
        """Tier 2: VLM-based OCR."""
        # NOT IMPLEMENTED — return QCS=0 so it never falsely passes
        logger.warning("Tier 2 VLM is not implemented — returning QCS=0")
        return ExtractionResult(
            text="",
            tables=[],
            qcs_score=0.0,
            tier_used=2,
            confidence_details={"status": "not_implemented"},
            metadata={"vlm_implemented": False}
        )

    def _tier3_ensemble(
        self,
        tier0_result: Optional[ExtractionResult],
        tier1_result: ExtractionResult,
        tier2_result: Optional[ExtractionResult],
        tier_attempts: list,
    ) -> ExtractionResult:
        """Tier 3: N-way ensemble consensus (LV-ROVER vote + Consensus-Entropy routing).

        Votes the per-engine canonical tables (Tier 0 native, Tier 1 OCR, Tier 2
        VLM) into a single merged set, scores cross-engine agreement with the
        Consensus-Entropy table quality ``Q_table`` (arXiv 2504.11101), and routes:

            * < 2 engine sources      → HITL (no corroboration earns a lower bar)
            * critical arithmetic fail → HITL (overrides Q_table; from V1-V4 rules)
            * Q_table ≥ tau_ensemble_high     → auto-accept
            * tau_ensemble_arbiter ≤ Q < high → LLM-as-Judge band (no judge yet → HITL)
            * Q_table < tau_ensemble_arbiter  → HITL

        Replaces the legacy "pick best prior result + always-HITL" stub.
        """
        from accounting.ensemble import (
            EnsembleSource, ensemble_vote, canonical_tables_to_records,
        )

        # Assemble engine sources from each tier's *pre-reconciliation* canonical
        # tables (live CanonicalTable objects only — see _all_canonical_table_objects).
        weighted = [("tier0", 1.0, tier0_result), ("tier1", 0.8, tier1_result),
                    ("tier2", 0.7, tier2_result)]
        sources = []
        for name, weight, res in weighted:
            if res is None:
                continue
            raw = list(res.raw_canonical_tables or [])
            if not (raw and _all_canonical_table_objects(raw)):
                continue
            # OCR-7: a tagged Tier-0 fragment is a piece of a statement. It has no
            # counterpart in the other engines, so the vote could only mis-align
            # it onto a real table or carry it through as a lone unsupported
            # source — both worse than leaving it where it is. It stays in
            # ``tables`` + ``table_provenance`` for CLS-1/CLS-4.
            raw = [ct for ct in raw if not (ct.metadata or {}).get("fragment")]
            if raw:
                sources.append(EnsembleSource(name=name, tables=raw, weight=weight))

        priors = [r for r in (tier0_result, tier1_result, tier2_result) if r is not None]
        best_prior = max(priors, key=lambda r: r.qcs_score) if priors else tier1_result

        # A critical arithmetic failure from any prior tier forces HITL regardless
        # of consensus (a balanced-looking-but-wrong table is the worst outcome).
        arithmetic_fail = any(
            ((r.metadata or {}).get("validation_report") or {}).get("critical_failures")
            for r in priors
        )

        source_count = len(sources)

        # ---- Degenerate: fewer than 2 engines → no consensus possible --------
        if source_count < 2:
            logger.warning(
                "Tier 3: %d engine source(s) — no consensus possible, routing to HITL",
                source_count,
            )
            result = best_prior
            tier_attempts.append({
                "tier": 3, "name": "Ensemble",
                "qcs": round(result.qcs_score, 3),
                "threshold": self.config.tau_ensemble_arbiter,
                "passed": False,
                "sources": [s.name for s in sources],
                "single_source": True,
            })
            _ss_reason = (
                f"ensemble single_source: {source_count} engine source(s) — "
                "no consensus possible"
            )
            _prior_reason = (result.metadata or {}).get("hitl_reason") or ""
            result.metadata = {
                **result.metadata, "hitl_required": True,
                # Keep the prior tier's reason as well: "the ensemble could not
                # run" and "the extraction scored 0.50" are two different things
                # a reviewer wants to know, and this path used to report neither.
                "hitl_reason": (
                    f"{_prior_reason}; {_ss_reason}" if _prior_reason else _ss_reason
                ),
                "ensemble": {"applied": False, "reason": "single_source",
                             "source_count": source_count},
            }
            result.confidence_details['tier_attempts'] = tier_attempts
            return result

        # ---- ≥2 engines → vote ----------------------------------------------
        voted = ensemble_vote(sources)
        # The vote re-merges columns from each source, which can re-introduce a
        # duplicate value column (e.g. a Tier-1 "Col2" echoing the roled value_n)
        # that per-tier enrichment had already pruned. Re-run the noise prune on
        # the merged tables so the canonical output + HITL grid stay clean.
        try:
            from accounting.enrichment_pipeline import prune_noise_columns
            for _vt in voted:
                prune_noise_columns(_vt)
        except Exception as exc:
            logger.debug(f"Ensemble noise-column prune skipped: {exc}")

        # Validate the VOTED output rather than inherit whichever tier happened
        # to score highest. Tier 3 merges *data* from every source but used to
        # take ``metadata`` — including ``validation_report`` — wholesale from
        # ``best_prior``, so the reported validation described frames the final
        # output is not made of. Measured on a real 3-statement pack, the tiers
        # failed DIFFERENT checks (Tier 0: V2 Resultat Net; Tier 1: CR Equation)
        # and which one surfaced was decided purely by QCS — an artifact of tier
        # selection, not a property of the document.
        #
        # ``label_raw`` is dropped: it is a text companion column, and the rule
        # engine's value-column selection can pick a text column (observed
        # choosing 'Actifs.1' on a duplicated header), which would corrupt every
        # amount lookup.
        ensemble_validation = None
        try:
            from accounting.ensemble import canonical_tables_to_parsed_dataframes
            from accounting.rule_engine import validate_with_rules
            frames = [
                f.drop(columns=["label_raw"], errors="ignore")
                for f in canonical_tables_to_parsed_dataframes(voted)
            ]
            if frames:
                ensemble_validation = validate_with_rules(
                    tables=frames,
                    standard=self.config.accounting_standard,
                    run_v2=True, run_v3=True, run_v4=True,
                )
                logger.info(
                    f"Tier 3 validation (own output): "
                    f"{ensemble_validation.passed_checks}/"
                    f"{ensemble_validation.total_checks} passed, "
                    f"critical={ensemble_validation.critical_failures}"
                )
        except Exception as exc:  # noqa: BLE001 — never break the vote
            logger.warning(f"Tier 3: validating voted output failed — {exc}")

        # ADD the emitted output's own criticals to the per-tier signal — never
        # replace it. Replacing would discard a genuine arithmetic failure in a
        # source tier whenever the ensemble's own validation failed to reproduce
        # it, and a balanced-looking-but-wrong table is the worst outcome. The
        # per-tier signal is trustworthy again now that the false CRITICALs are
        # skipped rather than failed (see the V2 Résultat and CR Equation
        # checks), so OR-ing is strictly more conservative than either alone.
        if ensemble_validation is not None and ensemble_validation.critical_failures > 0:
            arithmetic_fail = True

        # Conservative routing on the *worst* voted table's Q_table.
        q_tables = [
            ct.metadata.get("q_table", 1.0) for ct in voted
            if ct.metadata.get("reconciliation") == "ensemble"
        ]
        q_table = min(q_tables) if q_tables else best_prior.qcs_score
        total_conflicts = sum(len(ct.conflicts or []) for ct in voted)

        if arithmetic_fail or q_table < self.config.tau_ensemble_arbiter:
            passed, reason = False, ("arithmetic_failure" if arithmetic_fail else "low_consensus")
        elif q_table >= self.config.tau_ensemble_high:
            passed, reason = True, "high_consensus"
        else:
            passed, reason = False, "arbiter_band_no_judge"  # LLM judge not wired yet

        tier_attempts.append({
            "tier": 3, "name": "Ensemble",
            "qcs": round(q_table, 3),
            "threshold": self.config.tau_ensemble_high,
            "passed": passed,
            "sources": [s.name for s in sources],
            "conflicts": total_conflicts,
            "q_table": round(q_table, 3),
        })

        logger.info(
            "Tier 3 ensemble: %d sources, Q_table=%.3f, conflicts=%d → %s (%s)",
            source_count, q_table, total_conflicts,
            "ACCEPT" if passed else "HITL", reason,
        )

        # RTE-10 — `qcs_score` below holds Q_table, the ensemble's agreement
        # measure, NOT a QCS. The two are different quantities on different
        # bands and were being compared as if they were one. `score_kind` names
        # which one this record carries; `q_table` semantics are untouched.
        # The grade uses the SAME four words as a QCS grade (one vocabulary,
        # RTE-2) via the shared `raw_score_to_grade` helper — the bands differ
        # because the quantity differs, which is precisely what `score_kind`
        # exists to disclose.
        try:
            from qcs.calculator import raw_score_to_grade
            t3_grade = raw_score_to_grade(q_table).lower()
        except ImportError:
            t3_grade = "unknown"
        # Conservative direction only, like every other HITL writer here: the
        # ensemble may add a review, never cancel one a prior tier asked for.
        # A voted table is a different extraction from the one the prior tier
        # scored, so the ensemble gets to say "this needs a look" — it does not
        # get to say "forget what the last stage found".
        t3_hitl = (not passed) or bool((best_prior.metadata or {}).get("hitl_required", False))
        t3_reason = "" if passed else f"ensemble {reason} (Q_table={q_table:.3f})"
        if t3_hitl and not t3_reason:
            t3_reason = (best_prior.metadata or {}).get("hitl_reason") or ""

        return ExtractionResult(
            text=best_prior.text,
            tables=canonical_tables_to_records(voted),
            qcs_score=q_table,
            tier_used=3,
            confidence_details={
                **best_prior.confidence_details,
                "ensemble_q_table": round(q_table, 4),
                "ensemble_sources": [s.name for s in sources],
                "tier_attempts": tier_attempts,
            },
            metadata={
                **best_prior.metadata,
                "tier_used": 3,
                "hitl_required": t3_hitl,
                "hitl_reason": t3_reason,
                "qcs_grade": t3_grade,
                "score_kind": "q_table",
                # Carry the Docling cell geometry from the Tier-1 pass for the HITL
                # source-document overlay (best_prior is often Tier-0, which has
                # none). The overlay resolves cells by label text, so it survives
                # any row/table reordering the ensemble vote introduced.
                "table_boxes": (tier1_result.metadata or {}).get("table_boxes"),
                # Rebuild provenance from the VOTED tables. Inheriting it from
                # best_prior left it describing a different table list — on a
                # real pack, 3 entries against 28 voted tables — so the length
                # guard in the statement builder discarded it and every
                # statement lost its page number. The voted tables carry their
                # own page_range and x_offsets (see _CARRY_FORWARD_METADATA), so
                # this now always matches what it describes.
                "table_provenance": [
                    {
                        "page_no": ct.page_range[0] if ct.page_range else None,
                        "x_offsets": (ct.metadata or {}).get("x_offsets") or [],
                    }
                    for ct in voted
                ],
                # Report the validation of the VOTED tables — the data the user
                # is actually shown — not best_prior's, which describes frames
                # this result is not made of.
                "validation_report": (
                    ensemble_validation.to_dict() if ensemble_validation is not None
                    else (best_prior.metadata or {}).get("validation_report")
                ),
                "ensemble": {
                    "applied": True,
                    "source_count": source_count,
                    "source_names": [s.name for s in sources],
                    "q_table": round(q_table, 4),
                    "conflict_count": total_conflicts,
                    "routing_reason": reason,
                    "arithmetic_override": arithmetic_fail,
                },
            },
            canonical_tables=voted,
            raw_canonical_tables=best_prior.raw_canonical_tables,
        )


def main():
    parser = argparse.ArgumentParser(description='FinAlze OCR Pipeline')
    parser.add_argument('--input', '-i', required=True, help='Input file path')
    parser.add_argument('--output', '-o', default='output/', help='Output directory')
    parser.add_argument('--standard', '-s', default='NCT',
                        choices=['NCT', 'IFRS', 'SYSCOHADA'],
                        help='Accounting standard (NCT is the complete vertical; '
                             'IFRS/SYSCOHADA are spell-check only — see '
                             'context/Accounting-Standards-Handling.md)')
    parser.add_argument('--remote', metavar='API_URL', default=None,
                        help='Submit to a FinAlze API (e.g. http://localhost:8000) instead of running locally')
    parser.add_argument('--api-key', default=None, help='Partner API key for --remote')
    parser.add_argument('--token', default=None, help='Bearer token for --remote')
    args = parser.parse_args()

    # Remote mode: submit to the backend API and poll for the result.
    if args.remote:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from client.sdk import FinAlzeClient
        with FinAlzeClient(base_url=args.remote, api_key=args.api_key, token=args.token) as client:
            result_dict = client.submit_and_wait(args.input, accounting_standard=args.standard)
        print(f"\n{'='*60}\nFinAlze OCR Result (remote)\n{'='*60}")
        print(f"Input: {Path(args.input).name}")
        print(f"Tier Used: {result_dict.get('tier_used')}")
        print(f"QCS Score: {result_dict.get('qcs_score')}")
        print(f"HITL Required: {(result_dict.get('metadata') or {}).get('hitl_required', False)}")
        print(f"{'='*60}")
        return

    # Initialize pipeline
    config = PipelineConfig(accounting_standard=args.standard)
    pipeline = FinAlzePipeline(config)

    # Process document
    input_path = Path(args.input)
    result = pipeline.process_document(input_path)
    
    # Output
    print(f"\n{'='*60}")
    print(f"FinAlze OCR Result")
    print(f"{'='*60}")
    print(f"Input: {input_path.name}")
    print(f"Tier Used: {result.tier_used}")
    print(f"QCS Score: {result.qcs_score:.3f}")
    print(f"HITL Required: {result.metadata.get('hitl_required', False)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
