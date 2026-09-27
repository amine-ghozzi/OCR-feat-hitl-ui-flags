"""Deterministic L1 extraction metrics — the zero-cost first layer of the
Evaluation Stack ("apply in order: L1 deterministic FIRST, L2 LLM-judge SECOND").

The stochastic LLM judge is noisy: against real data it scored a fully-broken case
(bilan1, 0/51 values correct) as "1 defect" and ranked an objectively-better run
(367 vs 304 reference-matching values) LOWER. These metrics are the fix — they compare
a candidate extraction against the Opus reference purely arithmetically, so they catch
60-80% of failures before any LLM is consulted.

Pure stdlib, import-safe (no heavy deps). Generalizes scripts_value_match.py:
  - value_accuracy             fraction of reference (label, value) pairs reproduced
                               under the SAME truncated label (strict; the LOWER bound)
  - value_accuracy_fuzzy       like value_accuracy, but a pair also counts when the value
                               sits under a candidate label that is a fuzzy match of the
                               reference label (difflib ratio >= 0.6 on alnum-normalized,
                               lowercased labels) — recovers fragmented / abbreviated /
                               relabeled rows WITHOUT crediting an unrelated row. The fair
                               MIDDLE. (label_agnostic >= fuzzy >= strict by construction.)
  - value_recall_label_agnostic fraction of reference values present ANYWHERE in the
                               candidate, labels ignored. The noise-free floor used in
                               analysis — the UPPER bound; over-credits because it cannot
                               tell which row a value belongs to.
  - sign_accuracy              of magnitude-matched values, fraction with the same sign
  - completeness_recall        fraction of reference row labels present in the candidate

Why the complements: the strict metric keys candidate values on the SAME truncated label
as the reference, so a correct value under a slightly different / fragmented row label is
not credited — producing phantom regressions (we observed a -3 that was purely relabeling;
label-agnostic recall was identical). The strict metric stays the conservative primary; the
two complements bracket it (strict <= fuzzy <= label_agnostic) so relabeling-only deltas are
visible instead of masquerading as value regressions.

References are reused from the `full-20260607` run (same path logic as the prototype).
"""
from __future__ import annotations

import glob
import json
import os
from difflib import SequenceMatcher
from typing import Any, Dict, Optional

# Fuzzy-label match threshold: difflib ratio on alnum-normalized, lowercased labels.
# 0.6 recovers fragmented / abbreviated labels (e.g. a truncated or split row header)
# while staying well clear of unrelated rows, which typically score far lower.
FUZZY_LABEL_THRESHOLD = 0.6

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE, "runs")
REF_RUN = "full-20260607"


# --- normalization ---------------------------------------------------------
def _norm(x: Any) -> Optional[float]:
    """Value normalized for magnitude comparison: float rounded to 3dp, sign dropped.

    Mirrors scripts_value_match.py (strip thousands separators / spaces) but keys on
    ABSOLUTE value so sign is scored separately by sign_accuracy.
    """
    try:
        return round(abs(float(str(x).replace(",", "").replace(" ", ""))), 3)
    except Exception:
        return None


def _signed(x: Any) -> Optional[float]:
    try:
        return round(float(str(x).replace(",", "").replace(" ", "")), 3)
    except Exception:
        return None


def _label(raw_text: Any) -> str:
    return (raw_text or "")[:18]


def _norm_label(raw_text: Any) -> str:
    """Lowercased, alnum-only normalization for fuzzy label comparison.

    Strips whitespace / punctuation / accentless-or-not casing differences so that
    fragmented or abbreviated labels collapse toward their content (e.g.
    "DETTES FOURNISSEURS" and "Dettes-fournisseurs" both -> "dettesfournisseurs").
    Operates on the FULL raw_text (not the 18-char strict key) so a truncation that
    splits a label is recoverable.
    """
    return "".join(ch for ch in str(raw_text or "").lower() if ch.isalnum())


def _fuzzy_label_match(ref_norm: str, cand_norm: str) -> bool:
    """True when two normalized labels are close enough to be the same row."""
    if not ref_norm or not cand_norm:
        return False
    if ref_norm == cand_norm:
        return True
    return SequenceMatcher(None, ref_norm, cand_norm).ratio() >= FUZZY_LABEL_THRESHOLD


# --- pair extraction -------------------------------------------------------
def _iter_rows(doc: Dict[str, Any]):
    """Yield structured rows.

    Candidates carry BOTH a raw `tables` grid (list-of-lists) and the structured
    `canonical_tables`; references carry only the structured `tables`. So prefer
    `canonical_tables` when present (the candidate shape), else fall back to `tables`
    (the reference shape). Mirrors scripts_value_match.py, which read canonical_tables
    for candidates and tables-or-canonical for references.
    """
    tables = doc.get("canonical_tables")
    if not tables:
        tables = doc.get("tables")
    for ct in tables or []:
        if not isinstance(ct, dict):
            continue
        for r in ct.get("rows") or []:
            if isinstance(r, dict):
                yield r


def _cell_value(cell: Dict[str, Any]) -> Any:
    # reference cells use `value`; candidate cells use `parsed_value`.
    return cell.get("value", cell.get("parsed_value"))


def _row_values(r: Dict[str, Any]) -> set:
    """Signed values carried by one row's cells."""
    out: set = set()
    cells = r.get("cells") or {}
    # cells is a dict keyed by column header (both shapes); be tolerant of a list too.
    items = cells.values() if isinstance(cells, dict) else cells
    for c in items:
        if not isinstance(c, dict):
            continue
        v = _cell_value(c)
        if v is not None and v != "":
            sv = _signed(v)
            if sv is not None:
                out.add(sv)
    return out


def _pairs(doc: Dict[str, Any]) -> Dict[str, set]:
    """{strict 18-char label -> set(signed values)} for every cell that carries a value."""
    m: Dict[str, set] = {}
    for r in _iter_rows(doc):
        lbl = _label(r.get("raw_text"))
        vals = _row_values(r)
        if vals:
            m.setdefault(lbl, set()).update(vals)
    return m


def _norm_pairs(doc: Dict[str, Any]) -> Dict[str, set]:
    """{alnum-normalized FULL label -> set(signed values)} — basis for fuzzy matching."""
    m: Dict[str, set] = {}
    for r in _iter_rows(doc):
        nl = _norm_label(r.get("raw_text"))
        vals = _row_values(r)
        if vals:
            m.setdefault(nl, set()).update(vals)
    return m


def _all_abs_values(pairs: Dict[str, set]) -> set:
    """Every absolute value present anywhere in the doc (labels ignored)."""
    out: set = set()
    for vals in pairs.values():
        for v in vals:
            out.add(round(abs(v), 3))
    return out


# --- per-case scoring ------------------------------------------------------
def score_case(candidate: Dict[str, Any], reference: Dict[str, Any]) -> Dict[str, Any]:
    """L1 metrics for one (candidate_doc, reference_doc) pair.

    Returns {value_accuracy, value_accuracy_fuzzy, value_recall_label_agnostic,
    sign_accuracy, completeness_recall, n_ref, n_matched, n_matched_fuzzy,
    n_matched_label_agnostic}. The three value metrics bracket one another by
    construction — strict <= fuzzy <= label_agnostic:
      - value_accuracy               strict, same-label match (the LOWER bound; primary)
      - value_accuracy_fuzzy         value under a fuzzy-matched label (the fair MIDDLE)
      - value_recall_label_agnostic  value present anywhere, labels ignored (UPPER bound)
    The value/sign accuracies are None when there is nothing to score.
    """
    ref = _pairs(reference)
    cand = _pairs(candidate)

    # magnitude index for the candidate: label -> set(abs values)
    cand_abs: Dict[str, set] = {
        lbl: {abs(v) for v in vals} for lbl, vals in cand.items()
    }

    n_ref = sum(len(vs) for vs in ref.values())
    n_matched = 0           # strict magnitude matches (same truncated label)
    sign_total = 0          # magnitude matches that we can sign-check
    sign_ok = 0

    for lbl, vals in ref.items():
        cand_signed = cand.get(lbl, set())
        cand_mag = cand_abs.get(lbl, set())
        for v in vals:
            mag = round(abs(v), 3)
            if mag in cand_mag:
                n_matched += 1
                # sign check: does the candidate reproduce the SIGNED value too?
                sign_total += 1
                if v in cand_signed:
                    sign_ok += 1

    # --- complement 1: label-agnostic recall (values present ANYWHERE) ---------
    cand_any_abs = _all_abs_values(cand)
    n_matched_la = 0
    for vals in ref.values():
        for v in vals:
            if round(abs(v), 3) in cand_any_abs:
                n_matched_la += 1

    # --- complement 2: fuzzy-label accuracy ------------------------------------
    # For each reference row (normalized full label), gather the abs values of EVERY
    # candidate row whose normalized label fuzzy-matches it, then require the value to
    # be among them. Precompute the candidate's (norm_label -> abs values) index once.
    ref_n = _norm_pairs(reference)
    cand_n = _norm_pairs(candidate)
    cand_n_abs = {
        nl: {round(abs(v), 3) for v in vals} for nl, vals in cand_n.items()
    }
    # Cache fuzzy-matched candidate labels per reference label (labels repeat across rows).
    _match_cache: Dict[str, set] = {}

    def _fuzzy_abs_for(ref_nl: str) -> set:
        cached = _match_cache.get(ref_nl)
        if cached is not None:
            return cached
        acc: set = set()
        for cand_nl, abs_vals in cand_n_abs.items():
            if _fuzzy_label_match(ref_nl, cand_nl):
                acc |= abs_vals
        _match_cache[ref_nl] = acc
        return acc

    n_matched_fuzzy = 0
    for ref_nl, vals in ref_n.items():
        candidate_abs = _fuzzy_abs_for(ref_nl)
        for v in vals:
            if round(abs(v), 3) in candidate_abs:
                n_matched_fuzzy += 1

    # n_ref must be identical across the three (same reference values); _norm_pairs may
    # collapse rows that share a normalized label, so derive each metric's recall against
    # the ORIGINAL n_ref to keep them directly comparable.
    n_ref_fuzzy = sum(len(vs) for vs in ref_n.values())

    ref_labels = set(ref.keys())
    cand_labels = set(cand.keys())
    n_ref_labels = len(ref_labels)
    labels_present = len(ref_labels & cand_labels)

    return {
        "value_accuracy": round(n_matched / n_ref, 4) if n_ref else None,
        "value_accuracy_fuzzy": (
            round(n_matched_fuzzy / n_ref_fuzzy, 4) if n_ref_fuzzy else None
        ),
        "value_recall_label_agnostic": (
            round(n_matched_la / n_ref, 4) if n_ref else None
        ),
        "sign_accuracy": round(sign_ok / sign_total, 4) if sign_total else None,
        "completeness_recall": (
            round(labels_present / n_ref_labels, 4) if n_ref_labels else None
        ),
        "n_ref": n_ref,
        "n_matched": n_matched,
        "n_matched_fuzzy": n_matched_fuzzy,
        "n_matched_label_agnostic": n_matched_la,
    }


# --- run-level scoring (mirrors scripts_value_match.py path logic) ----------
def _load(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _ref_path(case: str) -> str:
    return os.path.join(RUNS, REF_RUN, "reference", case)


def _cand_path(run: str, case: str) -> str:
    return os.path.join(RUNS, run, "cases", case)


def score_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Aggregate L1 metrics across every case in `run_id` (references reused from
    full-20260607). Returns None when no case can be matched to a reference (caller
    then falls back to the LLM-only score)."""
    cases = [
        os.path.basename(p)
        for p in glob.glob(_cand_path(run_id, "*.json"))
        if not os.path.basename(p).startswith("_")
    ]

    per_case: Dict[str, Dict[str, Any]] = {}
    agg_n_ref = agg_matched = 0
    agg_matched_fuzzy = agg_matched_la = 0
    sign_acc_vals = []
    recall_vals = []

    for case in sorted(cases):
        reference = _load(_ref_path(case))
        if reference is None:
            continue
        candidate = _load(_cand_path(run_id, case))
        if candidate is None:
            continue
        m = score_case(candidate, reference)
        per_case[case] = m
        agg_n_ref += m["n_ref"]
        agg_matched += m["n_matched"]
        agg_matched_fuzzy += m.get("n_matched_fuzzy", 0)
        agg_matched_la += m.get("n_matched_label_agnostic", 0)
        if m["sign_accuracy"] is not None:
            sign_acc_vals.append(m["sign_accuracy"])
        if m["completeness_recall"] is not None:
            recall_vals.append(m["completeness_recall"])

    if not per_case:
        return None

    value_accuracy = round(agg_matched / agg_n_ref, 4) if agg_n_ref else None
    # Additive complements (micro-averaged like value_accuracy). They bracket the strict
    # metric: value_accuracy (lower) <= value_accuracy_fuzzy (middle)
    # <= value_recall_label_agnostic (upper). They DO NOT enter deterministic_score.
    value_accuracy_fuzzy = (
        round(agg_matched_fuzzy / agg_n_ref, 4) if agg_n_ref else None
    )
    value_recall_label_agnostic = (
        round(agg_matched_la / agg_n_ref, 4) if agg_n_ref else None
    )
    sign_accuracy = (
        round(sum(sign_acc_vals) / len(sign_acc_vals), 4) if sign_acc_vals else None
    )
    completeness_recall = (
        round(sum(recall_vals) / len(recall_vals), 4) if recall_vals else None
    )
    # deterministic_score formula is UNCHANGED — strict value_accuracy stays the
    # conservative primary; the complements are reported alongside but not blended in.
    parts = [x for x in (value_accuracy, sign_accuracy, completeness_recall) if x is not None]
    deterministic_score = round(sum(parts) / len(parts), 4) if parts else None

    return {
        "run": run_id,
        "cases_scored": len(per_case),
        "value_accuracy": value_accuracy,
        "value_accuracy_fuzzy": value_accuracy_fuzzy,
        "value_recall_label_agnostic": value_recall_label_agnostic,
        "sign_accuracy": sign_accuracy,
        "completeness_recall": completeness_recall,
        "deterministic_score": deterministic_score,
        "n_ref": agg_n_ref,
        "n_matched": agg_matched,
        "n_matched_fuzzy": agg_matched_fuzzy,
        "n_matched_label_agnostic": agg_matched_la,
        "per_case": per_case,
    }


if __name__ == "__main__":  # pragma: no cover
    import sys

    for run in sys.argv[1:] or [REF_RUN]:
        res = score_run(run)
        if res is None:
            print(f"=== {run}: no reference-matched cases ===")
            continue
        print(
            f"=== {run}: value_acc={res['value_accuracy']} "
            f"(fuzzy={res['value_accuracy_fuzzy']} "
            f"label_agnostic={res['value_recall_label_agnostic']}) "
            f"sign_acc={res['sign_accuracy']} "
            f"recall={res['completeness_recall']} -> deterministic={res['deterministic_score']} "
            f"({res['n_matched']}/{res['n_ref']} values) ==="
        )
        for case, m in res["per_case"].items():
            print(
                f"  {case:34} val={m['value_accuracy']} "
                f"fuzzy={m['value_accuracy_fuzzy']} "
                f"la={m['value_recall_label_agnostic']} sign={m['sign_accuracy']} "
                f"recall={m['completeness_recall']} ({m['n_matched']}/{m['n_ref']})"
            )