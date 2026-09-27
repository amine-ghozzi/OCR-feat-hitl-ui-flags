"""
Quality Confidence Score (QCS) Calculator

Multi-layer evaluation framework for OCR quality assessment without ground-truth data.

Architecture (Approach A + fixes):
1. Pre-gate: low_grade check (POOR → HITL, skip QCS)
2. Layer 1: Intrinsic (grade-mapped Docling scores, table_score excluded)
3. Layer 2: Statistical Heuristics (text properties)
4. Layer 3: Semantic (severity-weighted validation pass rate via Pandera)
5. Post: critical_failures hard HITL override

References:
- Scoring-Approach-Analysis.md: decision document
- Docling confidence_scores.md: grades recommended over raw scores
- Pandera lazy validation: failure_cases for severity-weighted scoring
"""

import re
import math
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import pandas as pd

logger = logging.getLogger(__name__)


# =============================================================================
# Grade Mapping (Docling grades → fixed scores)
# =============================================================================
# Docling recommends grades over raw float scores. Raw scores (especially
# parse_score which is 10th-percentile) collapse thresholds. Grade-mapped
# values prevent this.

GRADE_TO_SCORE = {
    "EXCELLENT": 1.0,
    "GOOD": 0.80,
    "FAIR": 0.55,
    "POOR": 0.25,
}

# Severity weights for validation checks
SEVERITY_WEIGHTS = {
    "CRITICAL": 3,
    "ERROR": 2,
    "WARNING": 1,
}


def raw_score_to_grade(score: float) -> str:
    """Map a raw 0-1 float score to a grade string.

    Used as fallback when Docling grades are not available.
    """
    if score >= 0.85:
        return "EXCELLENT"
    elif score >= 0.65:
        return "GOOD"
    elif score >= 0.40:
        return "FAIR"
    else:
        return "POOR"


@dataclass
class QCSReport:
    """Complete QCS report with all layer scores and diagnostics."""

    # Final composite score (0.0 - 1.0)
    qcs_score: float

    # Pre-gate result
    low_grade_gated: bool = False  # True if low_grade == POOR → skipped QCS

    # Layer 1: Intrinsic metrics (grade-mapped)
    intrinsic_score: float = 0.0
    ocr_grade: str = "FAIR"
    layout_grade: str = "FAIR"
    parse_grade: str = "FAIR"
    # table_score excluded — not implemented in Docling

    # Layer 2: Statistical heuristics
    heuristic_score: float = 0.0
    lexical_density: Optional[float] = None
    entropy_score: Optional[float] = None
    numeric_density: Optional[float] = None

    # Layer 3: Semantic metrics (severity-weighted)
    semantic_score: float = 0.0
    validation_pass_rate: Optional[float] = None
    critical_failures: int = 0  # Hard HITL override if > 0
    # VAL-5: a check that could not run is neither a pass nor a fail. Both counts
    # are carried so no consumer has to re-derive them from the report, and so
    # "7 of 11 passed" can never again be read as "4 silently fine".
    skipped_checks: int = 0
    skipped_critical_checks: int = 0  # Hard HITL override if > 0

    # Correction penalty (from post-processing)
    correction_penalty: float = 0.0
    corrections_made: int = 0

    # Quality grade (based on QCS thresholds)
    grade: str = "unknown"  # poor, fair, good, excellent

    # Tier recommendation and routing
    tier_recommendation: int = 1
    needs_vlm: bool = False
    needs_hitl: bool = False
    hitl_reason: str = ""  # Why HITL was triggered

    # Diagnostic signals (borrowed from S1-S5 approach)
    segmentation_confidence: Optional[float] = None
    mapping_confidence: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "qcs_score": round(self.qcs_score, 3),
            "grade": self.grade,
            "low_grade_gated": self.low_grade_gated,
            "intrinsic_score": round(self.intrinsic_score, 3),
            "heuristic_score": round(self.heuristic_score, 3),
            "semantic_score": round(self.semantic_score, 3),
            "critical_failures": self.critical_failures,
            "skipped_checks": self.skipped_checks,
            "skipped_critical_checks": self.skipped_critical_checks,
            "tier_recommendation": self.tier_recommendation,
            "needs_vlm": self.needs_vlm,
            "needs_hitl": self.needs_hitl,
            "hitl_reason": self.hitl_reason,
            "correction_penalty": round(self.correction_penalty, 3),
            "corrections_made": self.corrections_made,
            "details": {
                "ocr_grade": self.ocr_grade,
                "layout_grade": self.layout_grade,
                "parse_grade": self.parse_grade,
                "lexical_density": self.lexical_density,
                "numeric_density": self.numeric_density,
                "entropy_score": self.entropy_score,
                "validation_pass_rate": self.validation_pass_rate,
                "segmentation_confidence": self.segmentation_confidence,
                "mapping_confidence": self.mapping_confidence,
            }
        }


# =============================================================================
# Layer 1: Intrinsic Metrics (Grade-Mapped)
# =============================================================================

def calculate_intrinsic_score(
    ocr_grade: str = "FAIR",
    layout_grade: str = "FAIR",
    parse_grade: str = "FAIR",
) -> float:
    """
    Calculate intrinsic confidence from Docling grade-mapped scores.

    Uses grades instead of raw floats to prevent the 10th-percentile
    parse_score trap. table_score excluded (not implemented in Docling).

    Weights:
    - ocr_grade: 0.4 (text quality critical for numbers)
    - layout_grade: 0.4 (table structure important for financial docs)
    - parse_grade: 0.2 (digital text extraction quality)
    """
    ocr_val = GRADE_TO_SCORE.get(ocr_grade, 0.55)
    layout_val = GRADE_TO_SCORE.get(layout_grade, 0.55)
    parse_val = GRADE_TO_SCORE.get(parse_grade, 0.55)

    return ocr_val * 0.4 + layout_val * 0.4 + parse_val * 0.2


# =============================================================================
# Layer 2: Statistical Heuristics (Text Properties)
# =============================================================================

# French financial vocabulary for lexical density
FRENCH_FINANCIAL_TERMS = {
    "actif", "passif", "bilan", "résultat", "total", "capitaux", "propres",
    "immobilisations", "créances", "dettes", "trésorerie", "stocks",
    "chiffre", "affaires", "charges", "produits", "exploitation",
    "financier", "exercice", "variation", "net", "brut", "courants",
    "non", "fournisseurs", "clients", "disponibilités", "amortissements",
    "provisions", "réserves", "capital", "social", "report", "nouveau",
}


def calculate_lexical_density(text: str) -> float:
    """
    Calculate ratio of valid French financial terms to total words.
    Higher = better quality for financial documents.
    """
    if not text:
        return 0.0

    # Tokenize
    words = re.findall(r'[a-zA-ZéèêëàâäùûüîïôöçÉÈÊËÀÂÄÙÛÜÎÏÔÖÇ]+', text.lower())

    if len(words) < 5:  # Too few words to assess
        return 0.5

    # Count financial terms
    financial_count = sum(1 for w in words if w in FRENCH_FINANCIAL_TERMS)

    # For financial docs, expect ~10-20% financial terms
    density = financial_count / len(words)

    # Normalize to 0-1 scale (10% = 0.5, 20%+ = 1.0)
    return min(1.0, density * 5)


def calculate_numeric_density(text: str) -> float:
    """
    Calculate ratio of numeric content in text.
    Financial documents should have high numeric density.
    """
    if not text:
        return 0.0

    # Count digit characters
    digit_count = sum(1 for c in text if c.isdigit())
    total_chars = len(text.replace(" ", "").replace("\n", ""))

    if total_chars == 0:
        return 0.0

    density = digit_count / total_chars

    # Financial docs typically have 20-40% numeric content
    # Normalize: 20% = 0.5, 40%+ = 1.0
    return min(1.0, density * 2.5)


def calculate_entropy(text: str) -> float:
    """
    Calculate normalized Shannon entropy of text.
    Very high entropy = gibberish, very low = repetitive noise.
    Normal text has entropy around 4-5 bits/char.

    Returns score where 1.0 = normal entropy range.
    """
    if not text or len(text) < 10:
        return 0.5

    # Character frequency
    freq = {}
    for c in text.lower():
        freq[c] = freq.get(c, 0) + 1

    # Calculate entropy
    total = len(text)
    entropy = 0.0
    for count in freq.values():
        p = count / total
        entropy -= p * math.log2(p)

    # Normal French text entropy is ~4-5 bits/char
    # Score 1.0 for normal range (3.5-5.5), lower for outliers
    if 3.5 <= entropy <= 5.5:
        return 1.0
    elif entropy < 3.5:
        return entropy / 3.5  # Too uniform
    else:
        return 5.5 / entropy  # Too random (gibberish)


def calculate_heuristic_score(text: str, tables: List[pd.DataFrame] = None) -> Dict[str, float]:
    """
    Calculate Layer 2 heuristic scores from text properties.
    """
    lexical = calculate_lexical_density(text)
    numeric = calculate_numeric_density(text)
    entropy = calculate_entropy(text)

    # Weighted combination for financial documents
    # Numeric density is most important for financial docs
    heuristic_score = (
        0.30 * lexical +
        0.40 * numeric +
        0.30 * entropy
    )

    return {
        "heuristic_score": heuristic_score,
        "lexical_density": lexical,
        "numeric_density": numeric,
        "entropy_score": entropy,
    }


# =============================================================================
# Layer 3: Semantic Metrics (Severity-Weighted Validation)
# =============================================================================

def calculate_semantic_score(validation_report: Any = None) -> Dict[str, Any]:
    """
    Calculate Layer 3 semantic score from severity-weighted validation results.

    Uses severity weights: CRITICAL=3, ERROR=2, WARNING=1
    Formula: semantic_score = sum(severity_weight * pass) / sum(severity_weight)

    Also extracts critical_failures count for hard HITL override, and the two
    skipped tallies VAL-5 turns on: a check that could not run is neither a pass
    nor a fail, and a skipped CRITICAL check is a question nobody answered.
    """
    empty = {
        "semantic_score": 0.5,
        "validation_pass_rate": None,
        "critical_failures": 0,
        "skipped_checks": 0,
        "skipped_critical_checks": 0,
    }
    if validation_report is None:
        return dict(empty)

    if validation_report.total_checks == 0:
        return dict(empty)

    # Use severity-weighted score if available
    if hasattr(validation_report, 'severity_weighted_score') and \
       validation_report.severity_weighted_score is not None:
        semantic = validation_report.severity_weighted_score
    else:
        # Fallback to flat pass rate — over the checks that RAN (VAL-5). This
        # branch is only reached when nobody called
        # `compute_severity_weighted_score`, but it is the same denominator bug
        # and an auto-accept must not be able to reach it by a side door.
        _sk = int(getattr(validation_report, "skipped_checks", 0) or 0)
        semantic = validation_report.passed_checks / max(
            validation_report.total_checks - _sk, 1
        )

    # VAL-5 — "skipped is not passed", and it is not failed either. The old
    # denominator was `total_checks`, which counts skips, so a document whose
    # checks were 7 passed / 0 failed / 4 skipped reported a 0.64 pass rate:
    # a diagnostic that reads like four quiet failures when in truth eleven
    # checks were asked and only seven could be answered. The rate now measures
    # what it claims to — the share of the checks that RAN and passed. The
    # severity-weighted score above already excluded skips (validator.py D7),
    # so this moves the diagnostic only; `semantic`, and therefore the QCS
    # composite, is untouched by this line.
    skipped = int(getattr(validation_report, "skipped_checks", 0) or 0)
    ran = max(validation_report.total_checks - skipped, 1)
    pass_rate = validation_report.passed_checks / ran

    # Extract critical failures count
    critical_failures = 0
    if hasattr(validation_report, 'critical_failures'):
        critical_failures = validation_report.critical_failures

    # A CRITICAL check that was SKIPPED is the one case the counts above cannot
    # express: it is not a failure (nothing was proved wrong) and it must not be
    # read as a pass (nothing was proved right). `rule_engine` emits such a
    # result deliberately — `passed=False, skipped=True, severity="CRITICAL"` —
    # and routing has to see it, or the strictest checks in the suite become the
    # cheapest ones to ignore.
    skipped_critical = sum(
        1 for c in (getattr(validation_report, "checks", None) or [])
        if getattr(c, "skipped", False) and getattr(c, "severity", "") == "CRITICAL"
    )

    return {
        "semantic_score": semantic,
        "validation_pass_rate": pass_rate,
        "critical_failures": critical_failures,
        "skipped_checks": skipped,
        "skipped_critical_checks": skipped_critical,
    }


# =============================================================================
# QCS Composite Calculation
# =============================================================================

def calculate_correction_penalty(corrections_made: int, text_length: int = 0) -> float:
    """
    Calculate penalty score based on number of corrections made.

    More corrections = lower raw quality = higher penalty (but very light).

    Penalty formula: min(0.08, corrections / max(text_length/100, 10) * 0.01)
    - 0 corrections = 0 penalty
    - ~50 corrections per 1000 chars = ~0.05 penalty (5%)
    - ~80+ corrections per 1000 chars = 0.08 penalty (8% max)
    """
    if corrections_made == 0:
        return 0.0

    # Normalize by text length (corrections per 100 chars)
    normalization_factor = max(text_length / 100, 10)
    raw_penalty = corrections_made / normalization_factor

    # Cap at 0.08 (8% max penalty) - very light touch
    return min(0.08, raw_penalty * 0.01)


def calculate_qcs(
    # Layer 1: Intrinsic (grade-mapped)
    ocr_grade: str = "FAIR",
    layout_grade: str = "FAIR",
    parse_grade: str = "FAIR",
    # Pre-gate
    low_grade: str = "FAIR",
    # Layer 2: Heuristic (from text)
    text: str = "",
    tables: List[pd.DataFrame] = None,
    # Layer 3: Semantic (from validation)
    validation_report: Any = None,
    # Correction penalty
    corrections_made: int = 0,
    # Weights
    w_intrinsic: float = 0.4,
    w_heuristic: float = 0.3,
    w_semantic: float = 0.3,
    # Thresholds
    tau_excellent: float = 0.90,
    tau_good: float = 0.75,
    tau_fair: float = 0.60,
) -> QCSReport:
    """
    Calculate composite Quality Confidence Score.

    Flow:
    1. Check low_grade pre-gate (POOR → HITL immediately)
    2. Compute 3-layer score (intrinsic + heuristic + semantic)
    3. Check critical_failures hard override
    4. Determine grade and routing

    Returns QCSReport with score, grade, diagnostics, and tier recommendation.
    """
    # =========================================================================
    # Pre-gate: low_grade check
    # If any page graded POOR by Docling, route to HITL without computing QCS
    # =========================================================================
    if low_grade == "POOR":
        logger.warning("low_grade pre-gate triggered: worst page is POOR → HITL")
        return QCSReport(
            qcs_score=0.0,
            low_grade_gated=True,
            grade="poor",
            tier_recommendation=3,
            needs_vlm=True,
            needs_hitl=True,
            hitl_reason="low_grade pre-gate: Docling graded worst page as POOR",
            ocr_grade=ocr_grade,
            layout_grade=layout_grade,
            parse_grade=parse_grade,
        )

    # =========================================================================
    # Layer 1: Intrinsic (grade-mapped, table_score excluded)
    # =========================================================================
    intrinsic = calculate_intrinsic_score(
        ocr_grade=ocr_grade,
        layout_grade=layout_grade,
        parse_grade=parse_grade,
    )

    # =========================================================================
    # Layer 2: Heuristic
    # =========================================================================
    heuristic_results = calculate_heuristic_score(text, tables)
    heuristic = heuristic_results["heuristic_score"]

    # =========================================================================
    # Layer 3: Semantic (severity-weighted)
    # =========================================================================
    semantic_results = calculate_semantic_score(validation_report)
    semantic = semantic_results["semantic_score"]
    critical_failures = semantic_results["critical_failures"]
    skipped_checks = semantic_results.get("skipped_checks", 0)
    skipped_critical = semantic_results.get("skipped_critical_checks", 0)

    # =========================================================================
    # Correction penalty
    # =========================================================================
    correction_penalty = calculate_correction_penalty(corrections_made, len(text))

    # =========================================================================
    # Composite score
    # =========================================================================
    qcs_raw = (
        w_intrinsic * intrinsic +
        w_heuristic * heuristic +
        w_semantic * semantic
    )
    qcs = max(0.0, qcs_raw - correction_penalty)

    # =========================================================================
    # Grade and routing
    # =========================================================================
    if qcs >= tau_excellent:
        grade = "excellent"
    elif qcs >= tau_good:
        grade = "good"
    elif qcs >= tau_fair:
        grade = "fair"
    else:
        grade = "poor"

    # Tier recommendation
    if qcs >= tau_excellent:
        tier = 1
        needs_vlm = False
        needs_hitl = False
        hitl_reason = ""
    elif qcs >= tau_good:
        tier = 1
        needs_vlm = False
        needs_hitl = True
        hitl_reason = f"QCS grade is '{grade}' — quick review recommended"
    elif qcs >= tau_fair:
        tier = 2
        needs_vlm = True
        needs_hitl = True
        hitl_reason = f"QCS grade is '{grade}' — VLM + review needed"
    else:
        tier = 3
        needs_vlm = True
        needs_hitl = True
        # RTE-5: the bottom band earns a full manual review and a suggestion to
        # re-scan — never a rejection. Two scanned audit packs in this corpus sit
        # here (QCS 0.497 and 0.575); "Low → Reject" would have thrown both away
        # over what is a page-image problem, not a document problem. The pipeline
        # has no reject path and this reason is why.
        hitl_reason = (
            f"QCS grade is '{grade}' — full manual review; re-scan suggested"
        )

    # =========================================================================
    # Critical failures hard override
    # Any CRITICAL validation failure → force HITL regardless of QCS
    # =========================================================================
    if critical_failures > 0:
        needs_hitl = True
        hitl_reason = (
            f"CRITICAL validation failure(s): {critical_failures} "
            f"(QCS={qcs:.3f}, grade={grade})"
        )
        logger.warning(f"critical_failures override: {critical_failures} CRITICAL failure(s) → HITL")

    # =========================================================================
    # VAL-5 — skipped CRITICAL hard override
    # A CRITICAL check that could not run has not been satisfied; treating the
    # silence as consent is exactly the auto-accept this register row is about.
    # Raising only: a real CRITICAL failure keeps its (louder) reason.
    # =========================================================================
    if skipped_critical > 0:
        needs_hitl = True
        if critical_failures == 0:
            hitl_reason = (
                f"critical_check_skipped: {skipped_critical} CRITICAL check(s) "
                f"could not run (QCS={qcs:.3f}, grade={grade})"
            )
        logger.warning(
            "critical_check_skipped override: %d CRITICAL check(s) skipped → HITL",
            skipped_critical,
        )

    return QCSReport(
        qcs_score=qcs,
        grade=grade,
        intrinsic_score=intrinsic,
        ocr_grade=ocr_grade,
        layout_grade=layout_grade,
        parse_grade=parse_grade,
        heuristic_score=heuristic,
        lexical_density=heuristic_results.get("lexical_density"),
        numeric_density=heuristic_results.get("numeric_density"),
        entropy_score=heuristic_results.get("entropy_score"),
        semantic_score=semantic,
        validation_pass_rate=semantic_results.get("validation_pass_rate"),
        critical_failures=critical_failures,
        skipped_checks=skipped_checks,
        skipped_critical_checks=skipped_critical,
        correction_penalty=correction_penalty,
        corrections_made=corrections_made,
        tier_recommendation=tier,
        needs_vlm=needs_vlm,
        needs_hitl=needs_hitl,
        hitl_reason=hitl_reason,
    )


if __name__ == "__main__":
    # Test QCS calculation with grade-mapped scores
    test_text = """
    BILAN AU 31 DECEMBRE 2023

    ACTIFS NON COURANTS
    Immobilisations incorporelles    120 000    100 000    20.0%
    Immobilisations corporelles      220 000    200 000    10.0%
    TOTAL ACTIFS NON COURANTS        340 000    300 000    13.3%

    TOTAL ACTIF                      560 000    500 000    12.0%
    """

    # Test 1: Normal grades
    report = calculate_qcs(
        ocr_grade="GOOD",
        layout_grade="EXCELLENT",
        parse_grade="GOOD",
        low_grade="GOOD",
        text=test_text,
    )

    print("Test 1: Normal grades (GOOD/EXCELLENT/GOOD)")
    print(f"  Score: {report.qcs_score:.3f}")
    print(f"  Grade: {report.grade}")
    print(f"  Tier: {report.tier_recommendation}")
    print(f"  Needs HITL: {report.needs_hitl}")
    print(f"  HITL Reason: {report.hitl_reason}")
    print()
    print("Layer Breakdown:")
    print(f"  Intrinsic: {report.intrinsic_score:.3f}")
    print(f"  Heuristic: {report.heuristic_score:.3f}")
    print(f"  Semantic:  {report.semantic_score:.3f}")
    print()

    # Test 2: low_grade pre-gate
    report2 = calculate_qcs(
        ocr_grade="GOOD",
        layout_grade="GOOD",
        parse_grade="GOOD",
        low_grade="POOR",  # Triggers pre-gate
        text=test_text,
    )

    print("Test 2: low_grade POOR (pre-gate)")
    print(f"  Score: {report2.qcs_score:.3f}")
    print(f"  Gated: {report2.low_grade_gated}")
    print(f"  Needs HITL: {report2.needs_hitl}")
    print(f"  HITL Reason: {report2.hitl_reason}")
