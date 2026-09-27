"""The regression gate — a candidate change must pass before it's accepted.

Fast, reference-free tier:
  1. full pytest suite green (catches code regressions a fix elsewhere introduced),
  2. no NEW qualifier mismatches in the candidate log vs a baseline,
  3. an optional KPI-delta hook (inline pipeline reprocess on a sample — heavy; the caller
     injects it so the gate stays importable/cheap without the ML stack).

`run_tests=False` is used by unit tests (avoids recursively invoking pytest). Returns
`GateResult{passed, checks, failures}`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from accounting import candidate_audit
from accounting.variation_logger import resolve_log_path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_pytest(target: str = "tests/", timeout: int = 1200) -> Dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", target],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return {"passed": proc.returncode == 0, "returncode": proc.returncode, "tail": out[-1500:]}


def candidate_health(candidate_log: Optional[str] = None) -> Dict[str, Any]:
    records = candidate_audit.read_jsonl(candidate_log or resolve_log_path())
    audit = candidate_audit.audit_records(records)
    audit["qualifier_mismatches"] = len(candidate_audit.qualifier_mismatches(records))
    return audit


def run_gate(
    *,
    run_tests: bool = True,
    pytest_target: str = "tests/",
    candidate_log: Optional[str] = None,
    baseline_mismatches: Optional[int] = None,
    kpi_delta_fn: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run the gate. `baseline_mismatches` = the count before the change (fail if it rises).
    `kpi_delta_fn` = optional heavy reprocess-and-compare returning `{passed, ...}`."""
    checks: Dict[str, Any] = {}
    failures: list[str] = []

    if run_tests:
        t = run_pytest(pytest_target)
        checks["pytest"] = t
        if not t["passed"]:
            failures.append(f"pytest suite failed (rc={t['returncode']})")

    health = candidate_health(candidate_log)
    checks["candidate_health"] = health
    if baseline_mismatches is not None and health["qualifier_mismatches"] > baseline_mismatches:
        failures.append(
            f"new qualifier mismatches: {health['qualifier_mismatches']} > {baseline_mismatches}"
        )

    if kpi_delta_fn is not None:
        kd = kpi_delta_fn()
        checks["kpi_delta"] = kd
        if not kd.get("passed", True):
            failures.append("KPI regression on reprocessed sample")

    return {"passed": not failures, "checks": checks, "failures": failures}
