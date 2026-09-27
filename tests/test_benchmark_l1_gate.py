"""Feature A — standalone L1 pytest GATE (DeepEval `assert_test` pattern).

A deterministic CI tripwire that FAILS on a value-accuracy regression. Unlike the
hermetic calibration test (test_benchmark_calibration.py, synthetic perfect/corrupted
candidates), this gate runs against a small *real* fixture derived from production data:

  - tests/fixtures/benchmark_gate/reference.json   — copied from
    runs/full-20260607/reference/bilan1.docx.json (the Opus reference for bilan1).
  - tests/fixtures/benchmark_gate/candidate_good.json — copied from
    runs/full-20260607-r3/cases/bilan1.docx.json (the r3 candidate, which matches the
    reference well on the truncated-label metric).

The thresholds are real-measured floors set ~0.05 below the observed score so the gate is
a regression tripwire, not a flake. DeepEval-style: load the fixture, score it, assert the
metric clears each floor. If value_accuracy drops below GATE_MIN_VALUE, the gate FAILS and
the regression is caught in CI before any LLM judge is consulted.

The committed fixture makes this always-on (no live run artifacts required). A separate
run-level tripwire (test_benchmark_run_gate below) checks the live r3 run when present.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.extraction_benchmark import metrics  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "benchmark_gate"
REFERENCE = FIXTURE_DIR / "reference.json"
CANDIDATE = FIXTURE_DIR / "candidate_good.json"

# --- DeepEval-style thresholds -------------------------------------------------
# Measured baselines on the committed fixture (bilan1, r3 candidate vs Opus reference):
#   value_accuracy      = 0.6863   (35/51 reference values reproduced)
#   sign_accuracy       = 1.0
#   completeness_recall = 0.96
# Floors are set as regression tripwires, not exact-match assertions.
GATE_MIN_VALUE = 0.63  # measured 0.6863 — floor ~0.05 below to absorb metric jitter
GATE_MIN_SIGN = 0.95   # measured 1.0
GATE_MIN_RECALL = 0.80  # measured 0.96


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _score_fixture() -> dict:
    assert REFERENCE.exists(), f"missing fixture reference: {REFERENCE}"
    assert CANDIDATE.exists(), f"missing fixture candidate: {CANDIDATE}"
    return metrics.score_case(_load(CANDIDATE), _load(REFERENCE))


def test_l1_gate_value_accuracy():
    """The load-bearing tripwire: a value-accuracy regression must FAIL this gate."""
    m = _score_fixture()
    assert m["value_accuracy"] is not None
    assert m["value_accuracy"] >= GATE_MIN_VALUE, (
        f"value_accuracy {m['value_accuracy']} regressed below gate floor "
        f"{GATE_MIN_VALUE} (baseline 0.6863)"
    )


def test_l1_gate_sign_accuracy():
    m = _score_fixture()
    assert m["sign_accuracy"] is not None
    assert m["sign_accuracy"] >= GATE_MIN_SIGN, (
        f"sign_accuracy {m['sign_accuracy']} regressed below {GATE_MIN_SIGN} "
        f"(baseline 1.0)"
    )


def test_l1_gate_completeness_recall():
    m = _score_fixture()
    assert m["completeness_recall"] is not None
    assert m["completeness_recall"] >= GATE_MIN_RECALL, (
        f"completeness_recall {m['completeness_recall']} regressed below "
        f"{GATE_MIN_RECALL} (baseline 0.96)"
    )


# --- Run-level live tripwire ---------------------------------------------------
GATE_BASELINE = (
    ROOT / "tools" / "extraction_benchmark" / "gate_baseline.json"
)
RUN_REGRESSION_TOLERANCE = 0.02


def _run_dir_has_cases(run_id: str) -> bool:
    cases = metrics._cand_path(run_id, "*.json")
    import glob as _glob

    return bool(_glob.glob(cases))


def test_benchmark_run_gate():
    """IF the live run `full-20260607-r3` is present with cases, assert its
    deterministic_score has not regressed below the recorded baseline minus a small
    tolerance. Skips cleanly when the run artifacts are absent (CI without artifacts)."""
    baseline = json.loads(GATE_BASELINE.read_text(encoding="utf-8"))
    run_id = baseline["run"]
    if not _run_dir_has_cases(run_id):
        pytest.skip(f"run dir for {run_id} absent — live-run tripwire skipped")
    res = metrics.score_run(run_id)
    if res is None:
        pytest.skip(f"no reference-matched cases for {run_id} — tripwire skipped")
    floor = baseline["deterministic_score"] - RUN_REGRESSION_TOLERANCE
    assert res["deterministic_score"] >= floor, (
        f"{run_id} deterministic_score {res['deterministic_score']} regressed below "
        f"baseline {baseline['deterministic_score']} - {RUN_REGRESSION_TOLERANCE}"
    )