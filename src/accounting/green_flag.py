"""
Green-Flag 5-Gate Check — §18.

Determines whether a Tier 0 extraction is good enough to skip higher tiers.
All 5 gates must pass for the green-flag short-circuit to fire.

Gates:
  1. All label rows matched (≥95% exact/fuzzy match rate)
  2. All numeric cells parsed (0% red flags from number parser)
  3. Column model identified with confidence ≥ 0.80
  4. At least one subtotal footing validates (if subtotals exist)
  5. Statement classification confidence ≥ 0.60

Public API:

    check_green_flag(enrichment_report) → GreenFlagResult
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Thresholds for each gate
_MATCH_RATE_THRESHOLD = 0.95
_PARSE_FAIL_THRESHOLD = 0.0  # no parse failures allowed
_COLUMN_CONF_THRESHOLD = 0.80
_CLASSIFICATION_CONF_THRESHOLD = 0.60


@dataclass
class GreenFlagResult:
    """Result of the 5-gate green-flag check."""

    passed: bool
    gate_1_match_rate: float = 0.0
    gate_1_passed: bool = False
    gate_2_parse_fail_rate: float = 0.0
    gate_2_passed: bool = False
    gate_3_column_confidence: float = 0.0
    gate_3_passed: bool = False
    gate_4_footing_valid: bool = False
    gate_4_passed: bool = False
    gate_4_no_subtotals: bool = False  # True if no subtotals to check
    gate_5_classification_confidence: float = 0.0
    gate_5_passed: bool = False
    failure_reasons: list[str] = None

    def __post_init__(self):
        if self.failure_reasons is None:
            self.failure_reasons = []


def check_green_flag(
    match_rate: float,
    parse_fail_rate: float,
    column_confidence: float,
    footing_results: list = None,
    classification_confidence: float = 0.0,
) -> GreenFlagResult:
    """Run the 5-gate check.

    Args:
        match_rate: Ratio of rows with exact/fuzzy/resolved matches (0–1).
        parse_fail_rate: Ratio of numeric cells that failed to parse (0–1).
        column_confidence: Confidence from column identifier (0–1).
        footing_results: List of SubtotalResult from subtotal_detector.
        classification_confidence: From statement classifier (0–1).

    Returns:
        GreenFlagResult with per-gate pass/fail and overall decision.
    """
    result = GreenFlagResult(passed=False)
    reasons: list[str] = []

    # Gate 1 — Match rate
    result.gate_1_match_rate = match_rate
    result.gate_1_passed = match_rate >= _MATCH_RATE_THRESHOLD
    if not result.gate_1_passed:
        reasons.append(f"G1: match rate {match_rate:.2%} < {_MATCH_RATE_THRESHOLD:.0%}")

    # Gate 2 — Parse failures
    result.gate_2_parse_fail_rate = parse_fail_rate
    result.gate_2_passed = parse_fail_rate <= _PARSE_FAIL_THRESHOLD
    if not result.gate_2_passed:
        reasons.append(f"G2: parse fail rate {parse_fail_rate:.2%} > 0%")

    # Gate 3 — Column model confidence
    result.gate_3_column_confidence = column_confidence
    result.gate_3_passed = column_confidence >= _COLUMN_CONF_THRESHOLD
    if not result.gate_3_passed:
        reasons.append(f"G3: column confidence {column_confidence:.2f} < {_COLUMN_CONF_THRESHOLD}")

    # Gate 4 — Footing validation
    if footing_results:
        valid_count = sum(1 for f in footing_results if f.footing_valid is True)
        total_checked = sum(1 for f in footing_results if f.footing_valid is not None)
        result.gate_4_footing_valid = valid_count > 0
        result.gate_4_passed = valid_count > 0 or total_checked == 0
        result.gate_4_no_subtotals = False
        if not result.gate_4_passed:
            reasons.append(f"G4: 0/{total_checked} subtotals validated")
    else:
        # No subtotals to check — pass by default
        result.gate_4_no_subtotals = True
        result.gate_4_passed = True

    # Gate 5 — Classification confidence
    result.gate_5_classification_confidence = classification_confidence
    result.gate_5_passed = classification_confidence >= _CLASSIFICATION_CONF_THRESHOLD
    if not result.gate_5_passed:
        reasons.append(
            f"G5: classification confidence {classification_confidence:.2f} < "
            f"{_CLASSIFICATION_CONF_THRESHOLD}"
        )

    # Overall
    result.passed = all([
        result.gate_1_passed,
        result.gate_2_passed,
        result.gate_3_passed,
        result.gate_4_passed,
        result.gate_5_passed,
    ])
    result.failure_reasons = reasons

    if result.passed:
        logger.info("Green-flag gate: ALL 5 GATES PASSED — short-circuit approved")
    else:
        logger.info(f"Green-flag gate: FAILED ({len(reasons)} gate failures: {reasons})")

    return result
