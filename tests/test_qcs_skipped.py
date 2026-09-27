"""VAL-5 — "skipped is not passed", and it is not failed either.

A validation check that could not run (the columns it needs are missing, the
inputs are implausible) is recorded with ``skipped=True``. Two consumers were
reading those records as if silence were consent:

* ``calculate_semantic_score`` divided ``passed_checks`` by ``total_checks``,
  which counts skips. The sweep's ``bilab2.docx`` shows the shape of it — 7
  passed, 0 failed, 4 skipped — reported as a 0.64 pass rate, a number that
  reads like four quiet failures when in truth eleven checks were asked and
  seven could be answered.
* Routing never looked at the skips at all, so a CRITICAL check that could not
  run cost a document nothing. The strictest checks in the suite were the
  cheapest ones to make disappear.

Both are diagnostics-and-routing fixes: the severity-weighted score that feeds
the QCS composite has excluded skipped checks since D7 (``validator.py``), so
no document's QCS moves because of this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting.validator import ValidationReport, ValidationResult  # noqa: E402
from qcs.calculator import calculate_qcs, calculate_semantic_score  # noqa: E402


def _report(passed: int, skipped: int, failed: int = 0, skipped_critical: int = 0):
    report = ValidationReport()
    for i in range(passed):
        report.add_check(ValidationResult(
            check_name=f"ran_and_passed_{i}", passed=True, message="", severity="WARNING"))
    for i in range(failed):
        report.add_check(ValidationResult(
            check_name=f"ran_and_failed_{i}", passed=False, message="", severity="WARNING"))
    for i in range(skipped):
        # The shape `rule_engine` emits ten times over: a check that did not run
        # is marked passed=True AND skipped=True, so anything reading `passed`
        # without `skipped` counts it as a success.
        report.add_check(ValidationResult(
            check_name=f"could_not_run_{i}", passed=True, message="",
            severity="WARNING", skipped=True))
    for i in range(skipped_critical):
        # And the one site that emits passed=False, skipped=True, CRITICAL.
        report.add_check(ValidationResult(
            check_name=f"critical_not_run_{i}", passed=False, message="",
            severity="CRITICAL", skipped=True))
    report.compute_severity_weighted_score()
    return report


def test_pass_rate_excludes_skipped():
    """bilab2's exact shape: 7 passed, 4 skipped, nothing failed → 1.0, not 0.64."""
    report = _report(passed=7, skipped=4)
    assert report.total_checks == 11
    assert report.passed_checks == 7
    assert report.skipped_checks == 4
    assert report.failed_checks == 0

    out = calculate_semantic_score(report)

    # Seven checks ran; seven passed. The document's checks are all clean.
    assert out["validation_pass_rate"] == 1.0
    assert out["skipped_checks"] == 4
    assert out["skipped_critical_checks"] == 0
    # The composite's semantic layer already excluded skips — untouched here.
    assert out["semantic_score"] == 1.0

    # A real failure still moves it: 7 of 8 that ran.
    mixed = calculate_semantic_score(_report(passed=7, skipped=4, failed=1))
    assert mixed["validation_pass_rate"] == 7 / 8

    # Degenerate: a report where nothing ran must not divide by zero.
    all_skipped = calculate_semantic_score(_report(passed=0, skipped=3))
    assert all_skipped["validation_pass_rate"] == 0.0
    assert all_skipped["skipped_checks"] == 3


def test_skipped_critical_forces_hitl():
    """A CRITICAL check nobody could run is a question nobody answered.

    ``tau_excellent=0.0`` is passed so the band puts this document in the
    "no human needed" branch no matter what it scores — the point being that
    the override has to flip it from there. The pipeline's own thresholds are
    not involved and not changed: 0.75 stays the cascade bar and 0.90 the
    no-human bar.
    """
    kwargs = dict(
        ocr_grade="EXCELLENT", layout_grade="EXCELLENT", parse_grade="EXCELLENT",
        low_grade="EXCELLENT", text="Bilan au 31 decembre", tables=[],
        corrections_made=0, tau_excellent=0.0,
    )

    # Control: the same document, with the CRITICAL check merely absent.
    clean = calculate_qcs(validation_report=_report(passed=9, skipped=0), **kwargs)
    assert clean.grade == "excellent"
    assert clean.needs_hitl is False
    assert clean.skipped_critical_checks == 0

    # And now with one CRITICAL that could not run.
    report = _report(passed=9, skipped=0, skipped_critical=1)
    # A skip is NOT a failure — the failure counter must stay at zero, or this
    # test would be proving the pre-existing critical_failures override instead.
    assert report.critical_failures == 0
    assert report.skipped_checks == 1

    out = calculate_qcs(validation_report=report, **kwargs)
    assert out.grade == "excellent"          # same band as the control
    assert out.needs_hitl is True            # and still routed to a human
    assert out.skipped_critical_checks == 1
    assert "critical_check_skipped" in out.hitl_reason

    # A genuine CRITICAL failure keeps its own, louder reason: the override
    # raises, it never talks over a worse finding.
    with_failure = ValidationReport()
    with_failure.add_check(ValidationResult(
        check_name="Balance Sheet Balance", passed=False, message="",
        severity="CRITICAL"))
    with_failure.add_check(ValidationResult(
        check_name="CR Equation", passed=False, message="",
        severity="CRITICAL", skipped=True))
    with_failure.compute_severity_weighted_score()
    both = calculate_qcs(validation_report=with_failure, **kwargs)
    assert both.needs_hitl is True
    assert both.critical_failures == 1 and both.skipped_critical_checks == 1
    assert "CRITICAL validation failure" in both.hitl_reason


def test_low_band_reason_suggests_a_re_scan_and_never_a_rejection():
    """RTE-5 — the two scanned audit packs in this corpus score 0.497 and 0.575.

    "Low → Reject" would have thrown both away over what is a page-image
    problem, not a document problem. The bottom band earns a full manual review
    and a suggestion to re-scan; there is no reject path to reach.
    """
    report = _report(passed=0, skipped=0, failed=6)
    out = calculate_qcs(
        ocr_grade="POOR", layout_grade="FAIR", parse_grade="POOR",
        low_grade="FAIR", text="x", tables=[], validation_report=report,
    )
    assert out.grade == "poor"
    assert out.needs_hitl is True
    assert "full manual review" in out.hitl_reason
    assert "re-scan suggested" in out.hitl_reason
    for forbidden in ("reject", "Reject", "REJECT"):
        assert forbidden not in out.hitl_reason
