"""Golden sweep — a per-document STRUCTURAL snapshot of the extraction pipeline.

MEA-1: there is no regression gate at HEAD. The L1 benchmark re-scores a June run and
the scratchpad sweep has no comparison step. This module is the gate: it takes one
record per corpus document describing the *shape* of what the pipeline produced, and a
compare that proves a later merge changed only what its whitelist says it would.

**No values, labels or text ever enter a record.** Every field is a count, a shape, a
class name, a boolean, a rounded score or a digest. That is not only a privacy rule for
the five non-public client documents (decision G-2) — it is what makes the snapshot a
*structural* baseline: a record that carried cell values would diff on every OCR
nudge and prove nothing.

Three commands (driven by ``tools/golden_sweep.py``):

    snapshot  run the pipeline over the manifest -> one record per document
    compare   baseline vs candidate, each diff judged against an expected.yaml whitelist
    facts     assert the corpus facts the review established, each as pass / known-fail

The compare's whole point is *attribution*: after each merge the coordinator re-snapshots
and compares. Exit 0 means "only the whitelisted fields moved"; exit 1 means the change
did something it did not declare.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config as C

# --- Layout ----------------------------------------------------------------
GOLDEN_DIR = C.REPO_ROOT / "tools" / "golden"
CORPUS_FILE = GOLDEN_DIR / "corpus.yaml"
FACTS_FILE = GOLDEN_DIR / "corpus_facts.yaml"
EXPECTED_DIR = GOLDEN_DIR / "expected"
DEFAULT_DIFF_DIR = GOLDEN_DIR / "diffs"

SNAPSHOT_SCHEMA = "golden-sweep/1"

# The record schema, frozen. A record may hold exactly these keys — the test asserts
# it, so adding a field is a deliberate act (and a baseline-invalidating one).
RECORD_FIELDS: Tuple[str, ...] = (
    "doc_id", "public", "path", "size_kb", "ok", "error",
    "seconds", "env",
    "tier", "tier_attempts", "qcs", "score_kind",
    "hitl_required", "hitl_reason", "schema_version", "doc_type",
    "table_count", "nonempty_tables", "canonical_count", "canonical_none_count",
    "statements", "quarantined",
    "checks", "critical_rule_ids",
    "green_flag_passed", "green_flag_failures", "routing_reason", "ensemble",
    "table_shapes", "text_chars", "text_sha", "page_count", "native_chars_per_page",
    # WP-2.9 (VAL-7/E-2). Adding a field INVALIDATES older baselines for this
    # field alone: records taken before it exists have no `unit`, so a compare
    # reports it as `added` on every document and the package whitelists it
    # (tools/golden/expected/2.9.yaml). The field records the unit the document
    # states — it is never used to rescale anything.
    "unit",
    # WP-2.10 (VAL-8). Same story: absent from every older baseline, so a compare
    # reports it `added` on every document and tools/golden/expected/2.10.yaml
    # whitelists exactly that. A COUNT of the document's raw cells written in the
    # dot-millimes shape — a structural fact about how the document was typed, not
    # a value and not a label, and safe to print for a non-public document. It is
    # 0, never null: a count of nothing is zero, and a document that produced no
    # table at all produced no such cell either.
    "dot_millimes_cells",
    # WP-2.7 (RTE-2). The grade that goes with `qcs` — one of the four words the
    # pipeline's single grade vocabulary uses (excellent / good / fair / poor),
    # read straight from `metadata['qcs_grade']` rather than re-derived here from
    # a band this tool does not own. Absent from every older baseline, so a
    # compare reports it `added` on every document and
    # tools/golden/expected/2.7.yaml whitelists exactly that. Read it together
    # with `score_kind`: at Tier 3 the grade describes a Q_table, not a QCS.
    "qcs_grade",
    # WP-2.8 (RTE-3). What Docling says about how well it READ the document —
    # `{ocr, layout, parse, low, mean}`, each one of the four grade words
    # UPPERCASE or null. Absent from every older baseline, so a compare reports
    # it `added` and tools/golden/expected/2.8.yaml whitelists exactly that.
    #
    # GRADES only, never the raw scores: a float moves with every model nudge
    # and would make the gate noisy, while the grade is what the QCS's intrinsic
    # layer actually consumes. A grade is a structural fact about the read, not
    # about the content — never a value, never a label — so it is safe to print
    # for a non-public document.
    #
    # Null where no Docling pass stands behind the record: a Tier-0 accept never
    # runs the extractor, and a Tier-3 record carries it only when the tier whose
    # extraction won `best_prior` was Tier 1.
    "docling_confidence",
    # WP-2.8 (RTE-4). The empty-read floor's verdict: did this document read
    # (nearly) nothing off the pages that could only be read by OCR? A bool, or
    # null on the same records that have no `docling_confidence`. It FLAGS — the
    # document is extracted, scored and returned either way; `hitl_required` is
    # the only field it can move, and only upwards.
    "read_floor_flagged",
    # WP-2.8 (RTE-3). The QCS this document WOULD have scored if the cascade
    # took Docling's grades instead of the ones it uses — a float, or null on the
    # same records that have no `docling_confidence`.
    #
    # It routes nothing. `qcs` is still the score the pipeline acted on and
    # `qcs_grade` still describes `qcs`; this is the measurement the P7/WP-7.2
    # adoption decision is made from, recorded per document so that decision has
    # evidence instead of an argument. Read the two together: the delta IS the
    # thing being measured. Measured pre-quarantine, while `qcs` may have been
    # recomputed over the surviving statements (RTE-6), so on a document with
    # `quarantined > 0` compare it against the pre-quarantine QCS in
    # `confidence_details`, not against this record's `qcs`.
    "qcs_docling",
)

# Fields excluded from `compare` unless --include-timing: they describe the machine
# and the clock, not the extraction. `timing` is reserved for the P3 extension.
TIMING_FIELDS: Tuple[str, ...] = ("seconds", "env", "timing")

STATEMENT_FIELDS: Tuple[str, ...] = (
    "idx", "qualified_type", "statement_id", "gate", "gate_reason",
    "pages", "rows", "cols", "match_rate", "values_sha",
)

# Every CRITICAL check name in the rule engine is a static literal today, but a few
# non-critical names interpolate a row LABEL (``f"VNC: {label[:25]}"``). An allowlist
# — rather than a sanitiser — is the only construction that provably cannot leak one:
# anything unrecognised is reduced to a digest, which still diffs when it changes.
KNOWN_CRITICAL_RULES = frozenset({
    "Tables Found",
    "Balance Sheet Balance",
    "CR Equation (Exploit+Fin-Impots)",
    "V2: Resultat Net Match (Bilan-CR)",
    "V2: Tresorerie Match (Bilan-TFT)",
})


# --- small helpers ---------------------------------------------------------
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _rel_path(p: Path) -> str:
    """Repo-relative, forward slashes (reused from sample.py:49) so a snapshot taken
    on Windows compares byte-for-byte against one taken in the container."""
    try:
        return str(Path(p).resolve().relative_to(C.REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _round(v: Any, n: int = 3) -> Any:
    return round(float(v), n) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def _load_yaml(path: Path) -> Any:
    import yaml  # noqa: PLC0415 — keep module import dependency-light
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


# --- corpus manifest -------------------------------------------------------
def load_corpus(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read ``tools/golden/corpus.yaml`` -> the document manifest.

    The manifest — not a filename heuristic — is the authority on which documents
    exist and which are non-public (GOV-3 reads the same file).
    """
    data = _load_yaml(path or CORPUS_FILE) or {}
    docs = data.get("docs") or []
    out: List[Dict[str, Any]] = []
    for d in docs:
        out.append({
            "doc_id": d["doc_id"],
            "path": str(d["path"]).replace("\\", "/"),
            "public": bool(d.get("public", False)),
            "unstable": bool(d.get("unstable", False)),
        })
    return out


def select_docs(corpus: Sequence[Dict[str, Any]], only: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    """Filter the manifest by ``--only``. A selector matches a doc_id or a file name."""
    if not only:
        return list(corpus)
    wanted = [s.strip() for s in only if s and s.strip()]
    chosen, unknown = [], []
    for sel in wanted:
        hit = [d for d in corpus if d["doc_id"] == sel or Path(d["path"]).name == sel]
        if not hit:
            unknown.append(sel)
        for d in hit:
            if d not in chosen:
                chosen.append(d)
    if unknown:
        raise KeyError(f"not in the corpus manifest: {', '.join(unknown)}")
    return chosen


def is_public(doc_id: str, corpus: Optional[Sequence[Dict[str, Any]]] = None) -> bool:
    for d in (corpus if corpus is not None else load_corpus()):
        if d["doc_id"] == doc_id:
            return bool(d["public"])
    return False


# --- shape helpers ---------------------------------------------------------
def frame_shape(table: Any) -> Tuple[int, int]:
    """(rows, cols) for whatever shape a tier emitted.

    Pipeline ``tables`` are lists of record dicts by the time ``process_document``
    returns, but Tier 0 works in DataFrames internally and a caller may hand us either.
    """
    shape = getattr(table, "shape", None)
    if shape is not None and not isinstance(table, (list, tuple, dict, str)):
        try:
            return int(shape[0]), int(shape[1])
        except Exception:  # noqa: BLE001
            return 0, 0
    if isinstance(table, (list, tuple)):
        rows = len(table)
        keys: set = set()
        width = 0
        for r in table:
            if isinstance(r, dict):
                keys.update(r.keys())
            elif isinstance(r, (list, tuple)):
                width = max(width, len(r))
        return rows, (len(keys) if keys else width)
    return 0, 0


def count_nonempty(tables: Optional[Sequence[Any]]) -> int:
    """MEA-11: count tables that actually hold rows.

    The probe this replaces asked ``getattr(t, 'size', 0)`` — a DataFrame attribute —
    of list-of-record frames, which have no ``size``, so it reported 0 beside a
    16-table canonical count. A frame has content iff it has at least one row.
    """
    n = 0
    for t in (tables or []):
        if t is None:
            continue
        empty = getattr(t, "empty", None)
        if empty is not None and not isinstance(t, (list, tuple, dict, str)):
            if not bool(empty):
                n += 1
        elif len(t) > 0:
            n += 1
    return n


def _cells_of(row: Any) -> Iterable[Any]:
    cells = row.get("cells") if isinstance(row, dict) else getattr(row, "cells", None)
    if isinstance(cells, dict):
        return list(cells.values())
    return list(cells or [])


def _parsed_value(cell: Any) -> Any:
    return cell.get("parsed_value") if isinstance(cell, dict) else getattr(cell, "parsed_value", None)


def statement_values_sha(ct: Any) -> Optional[str]:
    """sha256 over the SORTED parsed values of one statement — a digest, never a value.

    Deliberately over ``parsed_value`` (the Decimal the parser produced) rather than
    ``ensemble.canonical_tables_to_records``: those records carry ``label``/``label_raw``
    and RAW cell text, i.e. exactly the document content a record may not touch even to
    hash it in a way that could be reversed by guessing a label.
    """
    if ct is None:
        return None
    rows = ct.get("rows") if isinstance(ct, dict) else getattr(ct, "rows", None)
    values: List[str] = []
    for row in (rows or []):
        for cell in _cells_of(row):
            pv = _parsed_value(cell)
            if pv is not None and pv != "":
                values.append(str(pv))
    return _sha256(json.dumps(sorted(values), ensure_ascii=False))


def _ct_shape(ct: Any) -> Tuple[Optional[int], Optional[int]]:
    if ct is None:
        return None, None
    rows = ct.get("rows") if isinstance(ct, dict) else getattr(ct, "rows", None)
    rows = list(rows or [])
    keys: set = set()
    for r in rows:
        cells = r.get("cells") if isinstance(r, dict) else getattr(r, "cells", None)
        if isinstance(cells, dict):
            keys.update(cells.keys())
    return len(rows), len(keys)


def _ct_match_rate(ct: Any) -> Optional[float]:
    if ct is None:
        return None
    mr = ct.get("match_rate") if isinstance(ct, dict) else getattr(ct, "match_rate", None)
    return _round(mr) if mr is not None else None


# --- PDF page probe --------------------------------------------------------
def native_text_profile(path: Path) -> Tuple[Optional[int], Optional[List[int]]]:
    """(page_count, chars of embedded text per page) for a PDF; (None, None) otherwise.

    Counts only — the text itself never leaves this function. A scanned PDF reads as a
    list of near-zeros, which is precisely the fis_innov signature OCR-1 is about.
    """
    if Path(path).suffix.lower() != ".pdf":
        return None, None
    try:
        import fitz  # noqa: PLC0415 — PyMuPDF, optional at import time
    except Exception:  # noqa: BLE001
        return None, None
    doc = None
    try:
        doc = fitz.open(str(path))
        return doc.page_count, [len(doc[i].get_text() or "") for i in range(doc.page_count)]
    except Exception:  # noqa: BLE001 — a probe may never break a sweep
        return None, None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass


# --- record construction ---------------------------------------------------
def _statement_records(result: Any) -> Tuple[List[Dict[str, Any]], int]:
    meta = getattr(result, "metadata", None) or {}
    stmts = meta.get("statements") or []
    cts = list(getattr(result, "canonical_tables", None) or [])
    tables = list(getattr(result, "tables", None) or [])
    out: List[Dict[str, Any]] = []
    quarantined = 0
    for idx, s in enumerate(stmts):
        s = s or {}
        # schema v2: canonical_tables is index-aligned with statements — which is only
        # true while the None placeholders survive serialisation (DLV-15).
        ct = cts[idx] if idx < len(cts) else None
        rows, cols = _ct_shape(ct)
        if rows is None:
            # No canonical output for this statement: fall back to the source frames it
            # was built from, so `rows` still describes something real.
            shapes = [frame_shape(tables[i]) for i in (s.get("table_indices") or [])
                      if isinstance(i, int) and i < len(tables)]
            if shapes:
                rows = sum(r for r, _ in shapes)
                cols = max(c for _, c in shapes)
        if (s.get("gate") or "") == "quarantined":
            quarantined += 1
        out.append({
            "idx": idx,
            "qualified_type": s.get("qualified_type"),
            "statement_id": s.get("id"),
            "gate": s.get("gate"),
            "gate_reason": s.get("gate_reason"),
            "pages": list(s.get("pages") or []),
            "rows": rows,
            "cols": cols,
            "match_rate": _ct_match_rate(ct),
            "values_sha": statement_values_sha(ct),
        })
    return out, quarantined


def _checks(meta: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    vr = meta.get("validation_report") or {}
    checks = {
        "passed": vr.get("passed_checks"),
        "total": vr.get("total_checks"),
        "skipped": vr.get("skipped_checks"),
        "failed": vr.get("failed_checks"),
        "critical": vr.get("critical_failures"),
    }
    ids: List[str] = []
    for c in (vr.get("checks") or []):
        if not isinstance(c, dict):
            continue
        if c.get("severity") == "CRITICAL" and not c.get("skipped") and not c.get("passed"):
            name = str(c.get("name") or "")
            ids.append(name if name in KNOWN_CRITICAL_RULES else "rule:" + _sha256(name)[:12])
    return checks, sorted(ids)


def _green_flag_gates(meta: Dict[str, Any]) -> List[str]:
    """Gate IDs only (``["G1", "G3"]``).

    ``failure_reasons`` are sentences carrying measured rates ("G1: match rate 63.20%
    < 80%"). Those are derived numbers, not document content, but they would make the
    field diff on every rounding wobble — the gate identity is what a regression gate
    needs to see.
    """
    out: List[str] = []
    for r in (meta.get("green_flag_failures") or []):
        token = str(r).split(":", 1)[0].strip()
        out.append(token if len(token) <= 4 else "G?")
    return out


def _tier_attempts(result: Any) -> List[Dict[str, Any]]:
    """Structural cascade trace: which tiers ran, under which name, and whether each
    passed. The per-attempt QCS is deliberately dropped — it would re-introduce
    float jitter into a field that no tolerance rule reaches."""
    cd = getattr(result, "confidence_details", None) or {}
    out: List[Dict[str, Any]] = []
    for a in (cd.get("tier_attempts") or []):
        if isinstance(a, dict):
            out.append({"tier": a.get("tier"), "name": a.get("name"), "passed": a.get("passed")})
    return out


def _document_unit(cts: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """The document-level unit (WP-2.9, VAL-7/E-2) — ``{value, source, conflict}``.

    Read back off the enriched tables, which is where Stage 3b binds it. A
    structural fact about the document: what unit it says its figures are in,
    which source said so, and whether its tables disagree. ``None`` when no
    table of the document states a unit at all.

    The canonical tables are the only carrier — ``ExtractionResult.metadata``
    does not hold a document unit today (see the package notes).
    """
    try:
        C.ensure_src_on_path()
        from accounting.unit_detector import document_unit  # noqa: PLC0415 — heavy path
    except Exception:  # noqa: BLE001 — the record must survive a missing module
        return None
    try:
        return document_unit([ct for ct in (cts or []) if ct is not None])
    except Exception:  # noqa: BLE001
        return None


def _dot_millimes_cells(cts: Sequence[Any]) -> int:
    """Document-level count of raw cells in the dot-millimes shape (WP-2.10, VAL-8).

    Summed off the enriched tables, which is where the enrichment pipeline records
    the measurement (``metadata["dot_millimes_evidence"]["cells"]``) — the same
    carrier ``unit`` uses, and the per-TABLE counts are summed rather than the
    document total read back, so the number describes the tables this run actually
    emitted even after the Tier-3 vote has merged them.

    A count, so 0 when nothing matched and 0 when there is nothing to match.
    """
    total = 0
    for ct in (cts or []):
        if ct is None:
            continue
        meta = (ct.get("metadata") if isinstance(ct, dict)
                else getattr(ct, "metadata", None)) or {}
        ev = meta.get("dot_millimes_evidence")
        if not isinstance(ev, dict):
            continue
        try:
            total += int(ev.get("cells") or 0)
        except (TypeError, ValueError):  # noqa: PERF203 — a bad record may not break the sweep
            continue
    return total


# The grade vocabulary, uppercase as the extractor normalises it. Kept local:
# this tool records what the pipeline said, it does not re-derive a grade.
_GRADE_WORDS = frozenset({"EXCELLENT", "GOOD", "FAIR", "POOR"})


def _docling_grades(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The Docling confidence GRADES behind this record (WP-2.8, RTE-3).

    `{ocr, layout, parse, low, mean}` — each one of the four words or None. The
    raw scores, the per-page breakdown and `table_score` (which has no writer in
    docling 2.70 and is always NaN) stay out of the record deliberately: the
    grade is what the QCS consumes and what a human can act on, and a float that
    drifts with every model update would make this gate noisy without making it
    sharper.

    None when the record has no Docling confidence at all — a Tier-0 accept never
    runs the extractor, and ``metadata['docling_confidence']`` is then absent.
    Note that ``confidence_details['docling_confidence']`` is a different thing
    entirely (a single float); this reads ``metadata``, and the isinstance guard
    means a record built from the other shape reads None rather than nonsense.
    """
    conf = (meta or {}).get("docling_confidence")
    if not isinstance(conf, dict):
        return None
    grades = conf.get("grades")
    if not isinstance(grades, dict):
        return None
    out: Dict[str, Any] = {}
    for key in ("ocr", "layout", "parse", "low", "mean"):
        val = grades.get(key)
        word = str(val).strip().upper() if val is not None else None
        out[key] = word if word in _GRADE_WORDS else None
    return out


def _read_floor_flagged(meta: Dict[str, Any]) -> Optional[bool]:
    """Did the empty-read floor flag this document (WP-2.8, RTE-4)?

    True / False where a Docling pass produced the per-page evidence, None where
    none did. A verdict, not a value: it says the document read (nearly) nothing,
    never what it read.
    """
    floor = (meta or {}).get("read_floor")
    if not isinstance(floor, dict) or floor.get("flagged") is None:
        return None
    return bool(floor.get("flagged"))


def build_record(
    doc: Dict[str, Any],
    result: Any,
    *,
    seconds: Optional[float] = None,
    env: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    source_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """One document -> one structural record. ``result`` may be None (the run failed)."""
    src = Path(source_path) if source_path else (C.REPO_ROOT / doc["path"])
    size_kb = round(src.stat().st_size / 1024, 1) if src.exists() else None
    page_count, per_page = native_text_profile(src)

    rec: Dict[str, Any] = {
        "doc_id": doc["doc_id"],
        "public": bool(doc.get("public", False)),
        "path": doc["path"],
        "size_kb": size_kb,
        "ok": result is not None and error is None,
        "error": error,
        "seconds": round(seconds, 2) if seconds is not None else None,
        "env": env,
        "tier": None, "tier_attempts": [], "qcs": None, "score_kind": None,
        "hitl_required": None, "hitl_reason": None, "schema_version": None, "doc_type": None,
        "table_count": None, "nonempty_tables": None,
        "canonical_count": None, "canonical_none_count": None,
        "statements": [], "quarantined": None,
        "checks": {"passed": None, "total": None, "skipped": None, "failed": None, "critical": None},
        "critical_rule_ids": [],
        "green_flag_passed": None, "green_flag_failures": [],
        "routing_reason": None, "ensemble": None,
        "table_shapes": [], "text_chars": None, "text_sha": None,
        "page_count": page_count, "native_chars_per_page": per_page,
        "unit": None,
        "dot_millimes_cells": 0,
        "qcs_grade": None,
        "docling_confidence": None,
        "read_floor_flagged": None,
        "qcs_docling": None,
    }
    if result is None:
        return rec

    meta = getattr(result, "metadata", None) or {}
    tables = list(getattr(result, "tables", None) or [])
    cts = list(getattr(result, "canonical_tables", None) or [])
    tier = getattr(result, "tier_used", None)
    statements, quarantined = _statement_records(result)
    checks, critical_ids = _checks(meta)
    ens = meta.get("ensemble") or {}
    text = getattr(result, "text", None) or ""

    rec.update({
        "tier": tier,
        "tier_attempts": _tier_attempts(result),
        "qcs": _round(getattr(result, "qcs_score", None)),
        # RTE-10: at Tier 3 the "qcs" column actually holds the ensemble's Q_table.
        # Label it rather than silently comparing two different measures. Since
        # WP-2.7 the pipeline states this itself, so read what it says; the
        # tier-derived expression stays as the fallback for a result produced by
        # code that predates the metadata key (an older run, a stub, a failure
        # path) and gives the same answer for every current one.
        "score_kind": meta.get("score_kind") or ("q_table" if tier == 3 else "qcs"),
        "qcs_grade": meta.get("qcs_grade"),
        "hitl_required": meta.get("hitl_required"),
        "hitl_reason": meta.get("hitl_reason"),
        "schema_version": meta.get("schema_version"),
        "doc_type": meta.get("doc_type"),
        "table_count": len(tables),
        "nonempty_tables": count_nonempty(tables),
        "canonical_count": len(cts),                       # INCLUDING None placeholders
        "canonical_none_count": sum(1 for ct in cts if ct is None),
        "statements": statements,
        "quarantined": quarantined,
        "checks": checks,
        "critical_rule_ids": critical_ids,
        "green_flag_passed": meta.get("green_flag_passed"),
        "green_flag_failures": _green_flag_gates(meta),
        "routing_reason": ens.get("routing_reason") if tier == 3 else None,
        "ensemble": ({
            "source_count": ens.get("source_count"),
            "q_table": _round(ens.get("q_table"), 4),
            "conflict_count": ens.get("conflict_count"),
            "arithmetic_override": ens.get("arithmetic_override"),
        } if ens else None),
        "table_shapes": [list(frame_shape(t)) for t in tables],
        "text_chars": len(text),
        "text_sha": _sha256(text),
        "unit": _document_unit(cts),
        "dot_millimes_cells": _dot_millimes_cells(cts),
        "docling_confidence": _docling_grades(meta),
        "read_floor_flagged": _read_floor_flagged(meta),
        # Rounded like `qcs` so the two compare on the same terms. The pipeline
        # already rounds it; this is belt and braces for a record built from an
        # older or hand-made result.
        "qcs_docling": _round(meta.get("qcs_docling")),
    })
    return rec


def assert_record_schema(rec: Dict[str, Any]) -> None:
    extra = set(rec) - set(RECORD_FIELDS)
    if extra:
        raise ValueError(f"record carries fields outside the schema: {sorted(extra)}")


# --- snapshot --------------------------------------------------------------
def run_snapshot(
    docs: Sequence[Dict[str, Any]],
    *,
    jobs: int = 1,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Run the pipeline over ``docs`` and return the snapshot document.

    One pipeline, reused across documents (the model load is minutes). Sequential:
    the OCR stack is neither fork- nor thread-safe, and the worker itself runs
    ``--concurrency=1`` for exactly that reason.
    """
    if jobs != 1:
        raise ValueError("--jobs > 1 is not supported: the OCR models are not fork-safe "
                         "(the worker runs --pool=prefork --concurrency=1 for the same reason)")
    C.ensure_src_on_path()
    from pipeline import FinAlzePipeline, PipelineConfig  # noqa: PLC0415 — heavy import

    from .cases import _env_fingerprint  # noqa: PLC0415 — reuse the harness fingerprint
    # The seed is part of what produced this snapshot: without it pinned, `text_sha`
    # does not reproduce across processes (see HASH_SEED_MESSAGE).
    env = {**_env_fingerprint(), "python_hash_seed": os.environ.get(HASH_SEED_ENV)}

    say = progress or (lambda _m: None)
    say("loading FinAlzePipeline (NCT) — the first document pays the model-load cost…")
    pipe = FinAlzePipeline(PipelineConfig(accounting_standard="NCT"))

    records: Dict[str, Any] = {}
    for i, doc in enumerate(docs, 1):
        src = C.REPO_ROOT / doc["path"]
        say(f"[{i}/{len(docs)}] {doc['doc_id']}")
        t0 = time.perf_counter()
        result, error = None, None
        try:
            result = pipe.process_document(src, accounting_standard="NCT")
        except Exception as exc:  # noqa: BLE001 — one bad document may not abort the sweep
            error = f"{type(exc).__name__}: {exc}"
        seconds = time.perf_counter() - t0
        rec = build_record(doc, result, seconds=seconds, env=env, error=error, source_path=src)
        assert_record_schema(rec)
        records[doc["doc_id"]] = rec
        say(f"      tier={rec['tier']} qcs={rec['qcs']} statements={len(rec['statements'])} "
            f"({rec['seconds']}s)" + (f" ERROR {error}" if error else ""))

    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "docs": records,
    }


def snapshot_records(snap: Dict[str, Any]) -> Dict[str, Any]:
    return snap.get("docs") or {}


# --- expected.yaml (the whitelist) -----------------------------------------
class ExpectedRules:
    """``expected/<wp>.yaml`` — what a work package declares it will change."""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        data = data or {}
        self.change = data.get("change")
        self.tolerance: Dict[str, float] = dict(data.get("tolerance") or {})
        self.docs: Dict[str, Dict[str, Any]] = dict(data.get("docs") or {})

    @classmethod
    def load(cls, path: Optional[Path]) -> "ExpectedRules":
        if not path:
            return cls()
        return cls(_load_yaml(path) or {})

    def rules_for(self, doc_id: str) -> Dict[str, Any]:
        """Per-document rules override the ``"*"`` defaults, field by field."""
        merged = dict(self.docs.get("*") or {})
        merged.update(self.docs.get(doc_id) or {})
        return merged


_MISSING = object()


def rule_allows(rule: Any, before: Any, after: Any) -> bool:
    """Judge one diff against one whitelist rule.

    Forms: ``any`` · ``added`` · ``removed`` · ``{to}`` · ``{from, to}`` · ``{min}`` ·
    ``{max}`` · ``{up: true}`` · ``{tol}``. A mapping may combine keys — WP-2.1 declares
    ``{from: 29, min: 20000}`` — and then EVERY key must hold.
    """
    if rule is None:
        return False
    if isinstance(rule, str):
        rule = rule.strip()
        if rule == "any":
            return True
        if rule == "added":
            return before in (None, [], {}, _MISSING) and after not in (None, [], {}, _MISSING)
        if rule == "removed":
            return before not in (None, [], {}, _MISSING) and after in (None, [], {}, _MISSING)
        return False
    if not isinstance(rule, dict):
        return False

    def _num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    for key, want in rule.items():
        if key == "from":
            if before != want:
                return False
        elif key == "to":
            if after != want:
                return False
        elif key == "min":
            a = _num(after)
            if a is None or a < float(want):
                return False
        elif key == "max":
            a = _num(after)
            if a is None or a > float(want):
                return False
        elif key == "up":
            a, b = _num(after), _num(before)
            # "up" reads as "never decreases" (WP-2.5: "qcs never decreases").
            if bool(want) and (a is None or b is None or a < b):
                return False
            if not bool(want) and (a is None or b is None or a > b):
                return False
        elif key == "tol":
            a, b = _num(after), _num(before)
            if a is None or b is None or abs(a - b) > float(want):
                return False
        else:
            return False
    return True


# --- compare ---------------------------------------------------------------
def _compare_fields(include_timing: bool) -> List[str]:
    if include_timing:
        return list(RECORD_FIELDS)
    return [f for f in RECORD_FIELDS if f not in TIMING_FIELDS]


def compare_records(
    before: Dict[str, Any],
    after: Dict[str, Any],
    rules: Dict[str, Any],
    tolerance: Dict[str, float],
    include_timing: bool = False,
) -> List[Dict[str, Any]]:
    """Field-level diff of one document, each entry judged expected / unexpected."""
    diffs: List[Dict[str, Any]] = []
    for field in _compare_fields(include_timing):
        b = before.get(field, _MISSING)
        a = after.get(field, _MISSING)
        if b == a:
            continue
        tol = tolerance.get(field)
        if tol is not None and isinstance(b, (int, float)) and isinstance(a, (int, float)):
            if abs(float(a) - float(b)) <= float(tol):
                continue
        rule = rules.get(field)
        diffs.append({
            "field": field,
            "before": None if b is _MISSING else b,
            "after": None if a is _MISSING else a,
            "rule": rule,
            "expected": rule_allows(rule, b, a),
        })
    return diffs


def compare(
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
    expected: Optional[ExpectedRules] = None,
    only: Optional[Sequence[str]] = None,
    include_timing: bool = False,
    unstable: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Compare two snapshots. Returns the full report; the CLI decides what to print.

    A document present in one snapshot and absent from the other is `missing` — the
    compare cannot prove anything about it, so it is exit code 2, not a diff.

    ``unstable`` are doc_ids the manifest flags as not reproducing identically between
    two runs of the same code. Their diffs are still listed and still counted, but they
    do not fail the gate — the flag is how a genuinely non-deterministic document is
    recorded honestly instead of quietly weakening every other document's comparison.
    """
    exp = expected or ExpectedRules()
    unstable_ids = set(unstable or ())
    b_docs, a_docs = snapshot_records(baseline), snapshot_records(candidate)
    ids = sorted(set(b_docs) | set(a_docs))
    if only:
        wanted = {s.strip() for s in only if s and s.strip()}
        ids = [i for i in ids if i in wanted]

    per_doc: List[Dict[str, Any]] = []
    missing: List[Dict[str, str]] = []
    for doc_id in ids:
        b, a = b_docs.get(doc_id), a_docs.get(doc_id)
        if b is None or a is None:
            missing.append({"doc_id": doc_id, "side": "candidate" if a is None else "baseline"})
            continue
        diffs = compare_records(b, a, exp.rules_for(doc_id), exp.tolerance, include_timing)
        flagged = doc_id in unstable_ids
        if flagged:
            for d in diffs:
                d["expected"] = True
                d["rule"] = d["rule"] if d["rule"] is not None else "unstable"
        per_doc.append({
            "doc_id": doc_id,
            # `public` is read from the SNAPSHOT, not the manifest: the record is what a
            # reviewer holds, and a compare must not need a second file to stay safe.
            "public": bool(a.get("public", b.get("public", False))),
            "unstable": flagged,
            "diffs": diffs,
            "n_diffs": len(diffs),
            "n_expected": sum(1 for d in diffs if d["expected"]),
            "n_unexpected": sum(1 for d in diffs if not d["expected"]),
        })

    totals = {
        "docs": len(per_doc),
        "diffs": sum(d["n_diffs"] for d in per_doc),
        "expected": sum(d["n_expected"] for d in per_doc),
        "unexpected": sum(d["n_unexpected"] for d in per_doc),
        "missing": len(missing),
    }
    exit_code = 2 if missing else (1 if totals["unexpected"] else 0)
    return {"change": exp.change, "docs": per_doc, "missing": missing,
            "totals": totals, "exit_code": exit_code}


# --- corpus facts ----------------------------------------------------------
class FactError(Exception):
    """A fact is malformed or names a check that does not exist."""


def _all_pages(rec: Dict[str, Any]) -> List[int]:
    out: List[int] = []
    for s in rec.get("statements") or []:
        out.extend(int(p) for p in (s.get("pages") or []) if isinstance(p, int))
    return out


def _check_statement_pages_superset(rec, pages) -> bool:
    return set(int(p) for p in pages) <= set(_all_pages(rec))


def _check_count_qualified_type(rec, type, eq=None, min=None, max=None) -> bool:  # noqa: A002
    n = sum(1 for s in (rec.get("statements") or []) if s.get("qualified_type") == type)
    if eq is not None and n != int(eq):
        return False
    if min is not None and n < int(min):
        return False
    if max is not None and n > int(max):
        return False
    return True


def _check_some_statement_covers(rec, pages) -> bool:
    want = set(int(p) for p in pages)
    return any(want <= set(s.get("pages") or []) for s in (rec.get("statements") or []))


def _check_no_statement_covers(rec, spans) -> bool:
    for span in spans:
        if _check_some_statement_covers(rec, span):
            return False
    return True


def _check_no_repeated_page_within_statement(rec) -> bool:
    for s in (rec.get("statements") or []):
        pages = list(s.get("pages") or [])
        if len(pages) != len(set(pages)):
            return False
    return True


def _check_statement_on_page(rec, page, qualified_type=None, min_rows=None) -> bool:
    for s in (rec.get("statements") or []):
        if int(page) not in (s.get("pages") or []):
            continue
        if qualified_type is not None and s.get("qualified_type") != qualified_type:
            continue
        if min_rows is not None and (s.get("rows") or 0) < int(min_rows):
            continue
        return True
    return False


def _check_field(rec, field, eq=None, min=None, max=None, not_null=None) -> bool:  # noqa: A002
    v = rec.get(field)
    # ``not_null`` asserts the field carries SOMETHING without pinning what — the
    # only way to state "this document's unit was identified" (WP-2.9/F-UNIT)
    # when the value itself is a structure that legitimately differs per
    # document. ``not_null: false`` asserts the converse.
    if not_null is not None and (v is not None) != bool(not_null):
        return False
    if eq is not None and v != eq:
        return False
    if min is not None and (not isinstance(v, (int, float)) or v < min):
        return False
    if max is not None and (not isinstance(v, (int, float)) or v > max):
        return False
    return True


#: The whole fact vocabulary. A registry of named callables, not an expression
#: evaluator: a fact file is data the coordinator edits, and data must never be able
#: to execute anything.
CHECKS: Dict[str, Callable[..., bool]] = {
    "statement_pages_superset": _check_statement_pages_superset,
    "count_qualified_type": _check_count_qualified_type,
    "some_statement_covers": _check_some_statement_covers,
    "no_statement_covers": _check_no_statement_covers,
    "no_repeated_page_within_statement": _check_no_repeated_page_within_statement,
    "statement_on_page": _check_statement_on_page,
    "field": _check_field,
}


def evaluate_check(rec: Dict[str, Any], check: Any) -> bool:
    """Run one check (or an ``all`` / ``any`` combination of them) against a record."""
    if not isinstance(check, dict):
        raise FactError(f"check must be a mapping, got {type(check).__name__}")
    if "all" in check:
        return all(evaluate_check(rec, c) for c in check["all"])
    if "any" in check:
        return any(evaluate_check(rec, c) for c in check["any"])
    fn_name = check.get("fn")
    if fn_name not in CHECKS:
        raise FactError(f"unknown check '{fn_name}' (known: {', '.join(sorted(CHECKS))})")
    args = dict(check.get("args") or {})
    try:
        return bool(CHECKS[fn_name](rec, **args))
    except FactError:
        raise
    except TypeError as exc:
        raise FactError(f"check '{fn_name}': {exc}") from exc


def load_facts(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    data = _load_yaml(path or FACTS_FILE) or {}
    return list(data.get("facts") or [])


def evaluate_facts(facts: Sequence[Dict[str, Any]], snap: Dict[str, Any]) -> Dict[str, Any]:
    """Assert every fact against a snapshot.

    Three outcomes matter. ``ok`` — the fact matched what was recorded (a pass still
    passes, a known-fail still fails). ``flip`` — a known-fail now passes, or a pass now
    fails; THAT is the signal, in both directions. ``skipped`` — the document is not in
    this snapshot (a ``--only`` sweep), which is information, not an error.
    """
    recs = snapshot_records(snap)
    results: List[Dict[str, Any]] = []
    flips = errors = 0
    for fact in facts:
        fid = fact.get("id") or "?"
        expect = (fact.get("expect") or "pass").strip()
        doc_ids = fact.get("doc_id")
        doc_ids = [doc_ids] if isinstance(doc_ids, str) else list(doc_ids or [])
        row: Dict[str, Any] = {"id": fid, "doc_id": doc_ids, "expect": expect,
                               "status": "ok", "actual": None, "note": None}

        if expect == "manual":
            row.update(status="manual", note="human judgement — not evaluable from a record")
            results.append(row)
            continue

        absent = [d for d in doc_ids if d not in recs]
        if not doc_ids or absent:
            row.update(status="skipped", note=f"not in snapshot: {', '.join(absent or ['<no doc_id>'])}")
            results.append(row)
            continue

        try:
            actual = all(evaluate_check(recs[d], fact.get("check")) for d in doc_ids)
        except FactError as exc:
            row.update(status="error", note=str(exc))
            errors += 1
            results.append(row)
            continue

        row["actual"] = actual
        want = (expect == "pass")
        if actual != want:
            row["status"] = "flip"
            row["note"] = ("recorded known-fail but it now PASSES" if want is False
                           else "recorded pass but it now FAILS")
            flips += 1
        results.append(row)

    exit_code = 3 if flips else (1 if errors else 0)
    return {"facts": results, "flips": flips, "errors": errors,
            "totals": {"total": len(results),
                       "ok": sum(1 for r in results if r["status"] == "ok"),
                       "flip": flips, "error": errors,
                       "skipped": sum(1 for r in results if r["status"] == "skipped"),
                       "manual": sum(1 for r in results if r["status"] == "manual")},
            "exit_code": exit_code}


# --- env guard -------------------------------------------------------------
VARIATION_LOG_ENV = "FINALZE_VARIATION_LOG"

ENV_GUARD_MESSAGE = (
    f"{VARIATION_LOG_ENV} is not set — refusing to run the sweep.\n"
    "\n"
    "Every extraction writes variation candidates through accounting.variation_logger.\n"
    "With no override the logger falls back to its per-tenant default under data/, and a\n"
    "16-document sweep would leave that file dirty in the worktree — the baseline would\n"
    "then describe a run that no clean checkout can reproduce (GOV-4 / MEA-1).\n"
    "\n"
    "Point it somewhere scratch first, e.g.\n"
    f"    set {VARIATION_LOG_ENV}=%TEMP%\\golden_variation_candidates.jsonl\n"
    f"    export {VARIATION_LOG_ENV}=$TMPDIR/golden_variation_candidates.jsonl\n"
)


def variation_log_configured(env: Optional[Dict[str, str]] = None) -> bool:
    return bool((env if env is not None else os.environ).get(VARIATION_LOG_ENV, "").strip())


# --- hash-seed guard -------------------------------------------------------
HASH_SEED_ENV = "PYTHONHASHSEED"

HASH_SEED_MESSAGE = (
    f"{HASH_SEED_ENV} was not set — re-running the sweep with {HASH_SEED_ENV}=0.\n"
    "Measured on this corpus: two runs of identical code over the six DOCX produce the\n"
    "same character COUNT and the same QCS but a different `text_sha`, because some\n"
    "set/dict iteration in the text post-processor rides Python's randomised string\n"
    "hash. Pinning the seed makes two runs byte-identical (verified), which is what a\n"
    "baseline has to be. Set it yourself to keep control of it.\n"
)


def needs_hash_seed_reexec(env: Optional[Dict[str, str]] = None) -> bool:
    """True when the sweep should restart under a pinned hash seed.

    Only when the operator has expressed no opinion. An explicitly set
    ``PYTHONHASHSEED`` — including ``random`` — is respected as a deliberate choice.
    """
    return (env if env is not None else os.environ).get(HASH_SEED_ENV) is None
