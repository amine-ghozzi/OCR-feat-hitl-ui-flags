"""Calibration gold-check for the deterministic L1 benchmark metrics.

This is the gate that would have caught bilan1: the stochastic LLM judge scored a
fully-broken extraction (0/51 values correct) as "1 defect". A deterministic
calibration check (the standard's Cohen's-Kappa / calibration idea, done arithmetically)
synthesizes a reference and two candidates derived from it — a "perfect" copy and a
"corrupted" copy where every value is x1000 — and asserts the metric separates them:
perfect value_accuracy approx 1.0, corrupted approx 0.0.

Hermetic: in-memory dicts only, no live run / references on disk.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.extraction_benchmark import metrics  # noqa: E402


def _reference() -> dict:
    """A small Opus-shaped reference (uses `tables` + `value` cells)."""
    return {
        "statement_type": "bilan",
        "standard": "NCT",
        "tables": [
            {
                "title": "PASSIF",
                "rows": [
                    {"raw_text": "CAPITAL SOCIAL", "cells": {
                        "31/12/2023": {"raw_value": "2 000 000,000", "value": 2000000.0},
                        "31/12/2022": {"raw_value": "1 500 000,000", "value": 1500000.0},
                    }},
                    {"raw_text": "RESULTAT DE L'EXERCICE", "cells": {
                        "31/12/2023": {"raw_value": "-50 000,000", "value": -50000.0},
                        "31/12/2022": {"raw_value": "120 000,000", "value": 120000.0},
                    }},
                    {"raw_text": "DETTES FOURNISSEURS", "cells": {
                        "31/12/2023": {"raw_value": "340 250,500", "value": 340250.5},
                    }},
                ],
            }
        ],
    }


def _to_candidate(ref: dict, transform) -> dict:
    """Re-shape the reference into the candidate shape (`canonical_tables` +
    `parsed_value` cells), applying `transform` to every numeric value."""
    tables = []
    for t in ref["tables"]:
        rows = []
        for r in t["rows"]:
            cells = {}
            for col, c in r["cells"].items():
                cells[col] = {
                    "raw_value": c["raw_value"],
                    "parsed_value": transform(c["value"]),
                    "flag": "green",
                }
            rows.append({"raw_text": r["raw_text"], "cells": cells})
        tables.append({"statement_type": "bilan", "rows": rows})
    return {"canonical_tables": tables}


def test_perfect_candidate_scores_one():
    ref = _reference()
    perfect = _to_candidate(ref, lambda v: v)
    m = metrics.score_case(perfect, ref)
    assert m["value_accuracy"] == 1.0
    assert m["sign_accuracy"] == 1.0          # signs preserved exactly
    assert m["completeness_recall"] == 1.0
    assert m["n_matched"] == m["n_ref"] == 5


def test_corrupted_candidate_scores_zero():
    """Every value x1000 -> no magnitude match -> value_accuracy collapses to 0.
    This is exactly the bilan1 failure the LLM judge missed."""
    ref = _reference()
    corrupted = _to_candidate(ref, lambda v: v * 1000.0)
    m = metrics.score_case(corrupted, ref)
    assert m["value_accuracy"] == 0.0
    assert m["n_matched"] == 0
    assert m["n_ref"] == 5
    # labels are unchanged, so completeness_recall stays high — proving value_accuracy,
    # not recall, is what catches a values-broken extraction.
    assert m["completeness_recall"] == 1.0


def test_calibration_separates_perfect_from_corrupted():
    """The single load-bearing assertion: the metric must rank perfect >> corrupted."""
    ref = _reference()
    good = metrics.score_case(_to_candidate(ref, lambda v: v), ref)["value_accuracy"]
    bad = metrics.score_case(_to_candidate(ref, lambda v: v * 1000.0), ref)["value_accuracy"]
    assert good >= 0.99 and bad <= 0.01
    assert good - bad >= 0.98


def test_sign_accuracy_flags_sign_flip():
    """Flip every sign but keep magnitudes: magnitude matches stay 1.0, sign drops."""
    ref = _reference()
    flipped = _to_candidate(ref, lambda v: -v)
    m = metrics.score_case(flipped, ref)
    assert m["value_accuracy"] == 1.0         # magnitudes all match
    assert m["sign_accuracy"] == 0.0          # but every sign is wrong


# --- label-fuzzy / label-agnostic complements ------------------------------
# These guard the relabeling-robustness fix: a correct value under a slightly
# different / fragmented row label must not read as a value regression.

def _to_candidate_relabel(ref: dict, relabel) -> dict:
    """Re-shape the reference into candidate shape with values intact, but rewrite each
    row's raw_text via `relabel(original_label)` — simulates fragmented/abbreviated rows."""
    tables = []
    for t in ref["tables"]:
        rows = []
        for r in t["rows"]:
            cells = {
                col: {"raw_value": c["raw_value"], "parsed_value": c["value"],
                      "flag": "green"}
                for col, c in r["cells"].items()
            }
            rows.append({"raw_text": relabel(r["raw_text"]), "cells": cells})
        tables.append({"statement_type": "bilan", "rows": rows})
    return {"canonical_tables": tables}


def test_perfect_candidate_all_three_metrics_one():
    """A perfect candidate: strict, fuzzy and label-agnostic all ≈ 1.0."""
    ref = _reference()
    m = metrics.score_case(_to_candidate(ref, lambda v: v), ref)
    assert m["value_accuracy"] == 1.0
    assert m["value_accuracy_fuzzy"] == 1.0
    assert m["value_recall_label_agnostic"] == 1.0


def test_corrupted_candidate_all_three_metrics_near_zero():
    """The x1000 corruption the gate exists to catch must collapse ALL three value
    metrics — fuzzy/label-agnostic must NOT rescue a values-broken extraction, so the
    gate still fires."""
    ref = _reference()
    m = metrics.score_case(_to_candidate(ref, lambda v: v * 1000.0), ref)
    assert m["value_accuracy"] <= 0.01
    assert m["value_accuracy_fuzzy"] <= 0.01
    assert m["value_recall_label_agnostic"] <= 0.01


def test_fragmented_labels_strict_low_but_fuzzy_and_agnostic_high():
    """Correct values, but every row label is fragmented/abbreviated so the strict
    18-char key no longer matches. strict value_accuracy collapses; fuzzy and
    label-agnostic stay high — proving the -3 'regression' we saw was pure relabeling."""
    ref = _reference()

    def fragment(label: str) -> str:
        # Abbreviate + reorder + punctuate so the 18-char prefix differs, yet the
        # alnum-normalized form stays a strong fuzzy match to the original.
        mangle = {
            "CAPITAL SOCIAL": "Cap. social (capital social)",
            "RESULTAT DE L'EXERCICE": "Resultat de l exercice / RESULTAT",
            "DETTES FOURNISSEURS": "Dettes-fournisseurs, dettes fournisseurs",
        }
        return mangle.get(label, label)

    cand = _to_candidate_relabel(ref, fragment)
    m = metrics.score_case(cand, ref)

    # Strict keys on the SAME first-18-chars label → at most a couple match by accident.
    assert m["value_accuracy"] <= 0.4
    # Fuzzy recovers the fragmented labels; label-agnostic ignores labels entirely.
    assert m["value_accuracy_fuzzy"] >= 0.99
    assert m["value_recall_label_agnostic"] >= 0.99
    # The fix's invariant: strict ≤ fuzzy ≤ label-agnostic.
    assert (
        m["value_accuracy"]
        <= m["value_accuracy_fuzzy"]
        <= m["value_recall_label_agnostic"]
    )


def test_fuzzy_does_not_credit_unrelated_rows():
    """A value under a TOTALLY unrelated label must not be credited by fuzzy (it only
    recovers near-matches); but label-agnostic, which ignores labels, still counts it."""
    ref = _reference()

    def scramble(label: str) -> str:
        # Replace each label with an unrelated token sharing no meaningful substring.
        return {"CAPITAL SOCIAL": "ZZZZ", "RESULTAT DE L'EXERCICE": "QQQQ",
                "DETTES FOURNISSEURS": "WWWW"}.get(label, "XXXX")

    cand = _to_candidate_relabel(ref, scramble)
    m = metrics.score_case(cand, ref)
    assert m["value_accuracy"] == 0.0          # strict: labels differ
    assert m["value_accuracy_fuzzy"] == 0.0    # fuzzy: no label is close enough
    assert m["value_recall_label_agnostic"] == 1.0  # values still present somewhere


def test_corrupted_run_does_not_change_benchmark_gate_formula():
    """deterministic_score must be unchanged by the additive metrics: it still averages
    only strict value_accuracy, sign_accuracy and completeness_recall."""
    ref = _reference()
    perfect = metrics.score_case(_to_candidate(ref, lambda v: v), ref)
    parts = [perfect["value_accuracy"], perfect["sign_accuracy"],
             perfect["completeness_recall"]]
    assert round(sum(parts) / len(parts), 4) == 1.0