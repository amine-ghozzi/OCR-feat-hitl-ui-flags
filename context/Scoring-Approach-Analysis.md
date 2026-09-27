# Scoring Approach Analysis: Decision Document

**Purpose**: Compare the current QCS approach against alternatives from the research and documentation resources to inform a final scoring architecture decision before Tier V1 implementation.

**Date**: March 2026
**Status**: Decision reached — A + fixes, validated by deep research (see §8)

---

## 1. Approaches Under Consideration

### Approach A — Current: Unified 3-Layer QCS (Implemented)

Single composite score per extraction, computed from three weighted layers.

```
QCS = 0.4 × Intrinsic + 0.3 × Heuristic + 0.3 × Semantic − Penalty
```

| Layer | Weight | Signals | Status |
|-------|--------|---------|--------|
| Intrinsic | 0.40 | ocr_score (0.4), layout_score (0.3), parse_score (0.2), table_score (0.1) | Implemented |
| Heuristic | 0.30 | numeric_density (0.4), lexical_density (0.3), entropy (0.3) | Implemented |
| Semantic | 0.30 | validation_pass_rate (V1–V4 checks) | Partial (V1 only) |
| Penalty | −0.08 max | corrections_made / text_length ratio | Implemented |

**Routing**: Single QCS value compared against tier thresholds (τ_fast=0.40, τ_vlm=0.35, τ_ensemble=0.60).

**Grading**: excellent (≥0.90), good (0.75–0.90), fair (0.60–0.75), poor (<0.60).

---

### Approach B — Pipeline Workflow: 5 Discrete Scores (S1–S5)

From `Documentation/Pipeline workflow.pdf`. Each pipeline step produces its own score:

| Score | Step | What It Measures |
|-------|------|-----------------|
| S1 | Ingestion | Document readability, format validity, image quality |
| S2 | Segmentation | Statement identification confidence (Bilan/CR/TFT found?) |
| S3 | Extraction | Raw OCR quality (≈ current Intrinsic layer) |
| S4 | Mapping | Schema mapping confidence (line items → chart of accounts) |
| S5 | Cross-Checking | Arithmetic + cross-statement validation pass rate |

**Global Score**: Weighted aggregation of S1–S5 (weights TBD in the PDF).

**Routing**: Low → Reject/Retrain, Medium → Human review, High → Auto-approve.

---

### Approach C — Research: Full QCS with Perplexity + LLM Judge

From `context/resource1.md`. Extends Approach A with heavier semantic signals:

| Layer | Signals | What It Adds |
|-------|---------|-------------|
| Intrinsic | Logprobs from VLMs, LCD (Low-Confidence Density) | Token-level uncertainty, not just engine-reported confidence |
| Heuristic | gibberish-detector (Markov chains), regex/pattern coverage, Pandera coercion rate | More signals, domain-specific pattern validation |
| Semantic | Perplexity via quantized Llama-3-8B, LLM-as-Judge rubric, consistency cross-check (Tesseract vs VLM) | Actual language model evaluation of output quality |
| Structural | Semantic Coherence Score (SCS), Region Entropy Divergence (RED) | Reading order validation, template conformance |

**Same formula**: `QCS = 0.4 × s_i + 0.3 × s_s + 0.3 × s_m` but with richer inputs per layer.

---

## 2. Comparative Analysis

### 2.1 Granularity

| Criterion | A (Current) | B (S1–S5) | C (Full Research) |
|-----------|-------------|-----------|-------------------|
| Score resolution | 1 number per extraction | 5 numbers per extraction | 1 number per extraction (or per-segment) |
| Diagnostic value | Low — "QCS = 0.62" doesn't tell you *what* failed | High — "S2=0.3, S5=0.9" tells you segmentation failed but arithmetic is fine | Medium — layer breakdown exists but still aggregated |
| Actionable routing | Binary (pass/fail per tier) | Per-step (can retry only the failing step) | Binary (pass/fail) + per-segment |

**Verdict**: B wins on diagnostics. But A's layer breakdown (intrinsic/heuristic/semantic) already provides *some* diagnostic granularity. The question is whether that's enough.

### 2.2 Complexity

| Criterion | A (Current) | B (S1–S5) | C (Full Research) |
|-----------|-------------|-----------|-------------------|
| Signals to compute | 6 (ocr, layout, parse, table, lexical, numeric, entropy) + validation rate | 5 distinct scoring functions, each with its own logic | 10+ signals + LLM inference per page |
| Dependencies | Docling outputs + text analysis + validator | Each score depends on its pipeline step completing | Requires Llama-3-8B loaded, Tesseract as cross-check engine |
| Weight tuning | 2 levels (layer weights + sub-weights) | 5 top-level weights + sub-weights per score | 2 levels + LLM prompt calibration |
| GPU cost | ~0 (text analysis only) | ~0 (text analysis only) | +2–4GB VRAM for quantized LLM; +200ms/page for perplexity |

**Verdict**: A is simplest. C is heaviest. B is moderate but requires more engineering to define what each S-score actually computes.

### 2.3 Calibration Difficulty

| Criterion | A (Current) | B (S1–S5) | C (Full Research) |
|-----------|-------------|-----------|-------------------|
| Ground truth needed | Moderate — need labeled docs to tune 0.4/0.3/0.3 weights and thresholds | High — need to tune 5 weights + 5 sets of sub-weights | High — need perplexity baselines, judge prompt calibration, Platt scaling |
| Threshold sensitivity | Current issue: thresholds dropped to 0.40/0.35 because raw Docling confidence is low | Same issue but spread across 5 dimensions — more knobs to turn | Perplexity-based ranking is relative (not threshold-based), more robust |
| Cold start problem | Yes — weights are guesses until calibrated on real data | Worse — 5× more guesses | Partially mitigated — perplexity provides an absolute language quality signal |

**Verdict**: C's perplexity approach is theoretically more robust to calibration issues (it's an absolute language quality measure, not a model-specific confidence), but the practical cost is high. A's cold-start problem is real but manageable with iterative tuning.

### 2.4 What Each Approach Catches

| Failure Mode | A (Current) | B (S1–S5) | C (Full Research) |
|-------------|-------------|-----------|-------------------|
| Bad OCR (garbled text) | ✓ entropy + lexical density detect it | ✓ S3 catches it | ✓ gibberish-detector + perplexity catch it strongly |
| Broken table structure | ✓ layout_score + subtotal validation | ✓ S3 + S5 | ✓ same as A |
| Wrong statement segmentation | ✗ not scored | ✓ S2 explicitly measures this | ✗ not scored (but consistency cross-check may catch downstream effects) |
| Schema mapping failure | ✗ not scored (mapping confidence not tracked) | ✓ S4 explicitly measures this | ✗ not scored |
| Arithmetic errors | ✓ validation pass rate | ✓ S5 | ✓ validation pass rate |
| Cross-statement inconsistency | ✓ (once V2 implemented) in validation rate | ✓ S5 | ✓ LLM-as-Judge could also catch this |
| VLM hallucination | ✗ no cross-check | ✗ no cross-check | ✓ consistency check (Tesseract vs VLM) + perplexity |
| OCR "confidently wrong" | ✗ trusts engine confidence | ✗ same | ✓ perplexity catches fluent-but-wrong text |

**Verdict**: B catches segmentation and mapping failures that A misses. C catches hallucinations and "confidently wrong" OCR that both A and B miss. But for Tier 1 only (no VLM), hallucination detection is irrelevant.

---

## 3. The Real Question: What Problem Are We Solving?

The current scoring problem isn't the formula — it's the **threshold collapse**.

The plan specified `τ_fast_high = 0.85` but the implementation uses `0.40`. This happened because:

1. **Docling's intrinsic scores are often near-zero** → Intrinsic layer (40% weight) pulls the whole QCS down
2. **Semantic layer defaults to 0.5** when validation isn't run → 30% of the score is a constant
3. **Result**: Most documents land in 0.35–0.65 range regardless of actual quality, making thresholds meaningless

**Root cause identified** (from Docling documentation research):

- `parse_score` is a **10th-percentile** measure, not an average. On documents with mostly clean text but a few corrupted cells, it will be near-zero even when 90% of content is fine. This is *by design* — it highlights local failures.
- `table_score` is **not yet implemented** in Docling — it always returns 0, contributing 10% dead weight to the intrinsic layer.
- Docling's own documentation states: *"Numerical scores are for informational purposes only; their computation may change in future releases."* Docling **recommends using grades** (`EXCELLENT/GOOD/FAIR/POOR`), not raw floats, for export-facing logic.

This is a **calibration problem**, not an architecture problem. Switching to S1–S5 would spread the same poorly-calibrated signals across 5 dimensions. Adding perplexity would help (it provides an independent quality signal) but at significant cost.

---

## 4. Recommendations

### For Tier V1 Implementation (Now)

**Keep Approach A** (unified 3-layer QCS) but fix the calibration issues:

1. **Grade-map intrinsic scores** instead of consuming raw floats. Docling recommends grades, not scores, for decision logic. Map to fixed values that prevent the 10th-percentile trap:
   ```python
   grade_to_score = {"EXCELLENT": 1.0, "GOOD": 0.80, "FAIR": 0.55, "POOR": 0.25}
   intrinsic = layout_grade * 0.4 + ocr_grade * 0.4 + parse_grade * 0.2
   # Exclude table_score (not yet implemented in Docling)
   ```
   This replaces the current approach of consuming `ocr_score`, `layout_score`, `parse_score` as raw floats, which are documented as unstable and informational-only.

2. **Add `low_grade` as a hard pre-gate**: Docling computes both `mean_grade` (document-level) and `low_grade` (worst page). If `low_grade == POOR`, route to HITL *before* QCS even computes — there's no point scoring a document where Docling itself flagged a page as unreadable.

3. **Make Semantic layer non-optional**: Run V1 validation (balance checks, subtotals, field types) on every extraction, not just when explicitly triggered. This gives the 30% semantic weight a real signal instead of defaulting to 0.5.

4. **Add severity weighting to validation pass rate**: Currently all checks count equally. A CRITICAL check failing (balance equation) should tank the semantic score more than a WARNING (variation %) failing:
   ```
   semantic_score = Σ(severity_weight × pass) / Σ(severity_weight)
   where CRITICAL=3, ERROR=2, WARNING=1
   ```
   Use Pandera's `lazy=True` mode to collect all failures in one pass via `failure_cases` DataFrame — this gives severity-weighted scoring AND CRITICAL override detection for free.

5. **Set Docling OCR `confidence_threshold`**: Filter low-confidence word-level bounding boxes at ingestion (e.g., `confidence_threshold=0.5` on EasyOCR). Cleaner input = better scores naturally.

6. **Platt scaling on QCS** (after 50 labeled docs): Instead of manually tuning the 0.4/0.3/0.3 weights or threshold values, fit a logistic regressor to map the compressed QCS distribution to calibrated HITL probabilities:
   ```python
   # X = raw QCS scores, y = human labels (0=needs review, 1=auto-approve)
   platt = LogisticRegression().fit(X_labeled, y_labeled)
   needs_hitl = platt.predict_proba([[qcs_score]])[0][1] < 0.80
   ```
   This decouples threshold calibration from weight tuning — the weights become an internal detail, the threshold is learned. Effective with as few as 30–50 labeled documents (sigmoid calibration, per sklearn docs).

### Borrow Selectively from B (S1–S5)

Don't implement 5 discrete scores, but **add two diagnostic signals** to the QCS report that track what B's S2 and S4 measure:

- **segmentation_confidence**: Did we identify the statement type? How confident? (Feeds into existing heuristic layer)
- **mapping_confidence**: What % of line items mapped to known chart of accounts entries? (Feeds into existing semantic layer)

These provide diagnostic value ("segmentation was weak") without the overhead of a full 5-score system. They can be displayed in the HITL UI alongside the QCS breakdown.

### Defer C (Full Research) Until VLM Integration

Perplexity and LLM-as-Judge become valuable when:
- Multiple OCR engines are running (need to pick the best output)
- VLMs are in the pipeline (need hallucination detection)
- The system processes documents where OCR engines are "confidently wrong"

None of these apply to Tier 1 only. Revisit when VLM integration begins.

---

## 5. Proposed Scoring Architecture (Post-Fix)

```
  Docling ──► low_grade == POOR? ──YES──► HITL (skip QCS)
  output          │ NO
                  ▼
          ┌─────────────────────────────────┐
          │         QCS Calculator           │
          │                                  │
          │  Layer 1: Intrinsic (0.40)       │
          │    Grade-mapped values:           │
          │    ocr_grade (0.4)               │◄── CHANGED: grades not raw floats
          │    layout_grade (0.4)            │    EXCELLENT=1.0, GOOD=0.80,
          │    parse_grade (0.2)             │    FAIR=0.55, POOR=0.25
          │    (table_score excluded —       │◄── FIX: not implemented in Docling
          │     was contributing dead 0)     │
          │                                  │
          │  Layer 2: Heuristic (0.30)       │
          │    numeric_density (0.35)         │
          │    lexical_density (0.25)         │
          │    entropy (0.20)                 │
          │    segmentation_confidence (0.10) │◄── NEW: from Step A
          │    mapping_confidence (0.10)      │◄── NEW: from Step B
          │                                  │
          │  Layer 3: Semantic (0.30)         │
          │    severity-weighted pass rate    │◄── CHANGED: weighted by severity
          │    (CRITICAL=3, ERROR=2, WARN=1)  │
          │    via Pandera lazy validation    │◄── NEW: failure_cases DataFrame
          │                                  │
          │  Correction Penalty (−0.08 max)  │
          │                                  │
          └───────────┬───────────────────────┘
                      │
          ┌───────────▼───────────────────────┐
          │         QCS Report                │
          │                                   │
          │  qcs_score: 0.72                  │
          │  grade: fair                      │
          │  intrinsic: 0.65                  │
          │  heuristic: 0.78                  │
          │  semantic: 0.80                   │
          │  segmentation_confidence: 0.90    │◄── Diagnostic (from B)
          │  mapping_confidence: 0.70         │◄── Diagnostic (from B)
          │  critical_failures: 0             │◄── Hard HITL override if > 0
          │  tier_recommendation: 1           │
          │  needs_hitl: true                 │
          │  hitl_reason: "fair grade"        │◄── Why HITL was triggered
          └───────────────────────────────────┘
                      │
          (after 50+ labeled docs)
                      ▼
          ┌───────────────────────────────────┐
          │  Platt Scaling (optional)         │
          │  LogisticRegression on QCS → P()  │◄── FUTURE: learned threshold
          │  needs_hitl = P(auto) < 0.80      │    replaces manual τ tuning
          └───────────────────────────────────┘
```

### Key Changes from Current

| Change | Rationale | Source |
|--------|-----------|--------|
| `low_grade` hard pre-gate | If any page is POOR, skip QCS entirely → HITL | Docling docs: `low_grade` is worst-page signal |
| Grade-mapped intrinsic scores | Prevents 10th-percentile `parse_score` from collapsing the layer | Docling docs: grades recommended over raw scores |
| Exclude `table_score` | Not yet implemented in Docling — was contributing 0 at 10% weight | Docling docs: field confirmed unimplemented |
| Severity-weighted semantic via Pandera lazy | `failure_cases` gives weighted score + CRITICAL override in one pass | Pandera docs: `lazy=True` collects all failures |
| `critical_failures` hard override | Any CRITICAL failure → force HITL regardless of QCS | FNRP / CVSS-adaptation pattern (NotebookLM) |
| Platt scaling (future) | Learned threshold from 50 labeled docs; no manual weight tuning needed | sklearn calibration docs: effective at small sample sizes |

### What Stays The Same

- 3-layer architecture with `0.4 / 0.3 / 0.3` weights (calibrate output not inputs)
- Correction penalty formula and cap
- Grade thresholds (excellent/good/fair/poor)
- Tier routing logic in pipeline.py

---

## 6. Decision Matrix

| Factor | Keep A as-is | A + fixes (recommended) | Switch to B (S1–S5) | Switch to C (full) |
|--------|-------------|------------------------|---------------------|-------------------|
| Implementation effort | None | Low (modify calculator.py + validator.py) | High (new scoring architecture) | Very high (LLM integration) |
| Diagnostic value | Low | Medium (add segmentation + mapping + hitl_reason) | High | Medium |
| Calibration robustness | Poor (threshold collapse) | Good (real signals in all layers) | Unknown (more knobs) | Good (perplexity is absolute) |
| GPU overhead | None | None | None | +2–4GB VRAM |
| Relevant for Tier 1 only | Yes | Yes | Partially (S1/S2 always useful) | No (perplexity/judge need VLM context) |
| Future-proof for VLM | Needs extension | Ready (add logprobs to intrinsic when VLM arrives) | Ready | Already designed for it |

**Recommendation**: **A + fixes**. Fix the calibration issues, borrow S2/S4 diagnostic signals, add severity weighting. This gives 80% of the benefit of B and C with 20% of the effort.

---

## 7. Open Questions — Resolved

Answers informed by NotebookLM deep research and Context7 library documentation analysis.

| # | Question | Answer | Evidence |
|---|----------|--------|----------|
| Q1 | Should CRITICAL failures override QCS? | **Yes.** Pandera `lazy=True` gives `failure_cases` DataFrame — filter by CRITICAL prefix, set `needs_hitl=True`. Zero extra cost. Aligns with FNRP (False-Negative Risk Penalty) patterns used in enterprise IDP. | Pandera docs, NotebookLM FNRP/CVSS research |
| Q2 | Should segmentation confidence be a hard gate? | **Lean yes.** Use Docling's `low_grade` (worst page) as proxy. If `low_grade == POOR`, gate before running validators — there's no meaningful content to segment. | Docling docs: `low_grade` vs `mean_grade` distinction |
| Q3 | Is zero Docling confidence always wrong? | **Likely not.** `parse_score` is a 10th-percentile measure — a single corrupted cell tanks it even when 90% of content is fine. Switch to grade-mapped values (`EXCELLENT=1.0, GOOD=0.80, FAIR=0.55, POOR=0.25`) to prevent this. Also: `table_score` is not yet implemented (always 0). | Docling confidence_scores.md documentation |
| Q4 | How to tune weights/thresholds? | **Platt scaling on output, not input weight tuning.** Fit a logistic regressor on 50–100 labeled docs to map QCS → P(auto-approve). Leave 0.4/0.3/0.3 as-is initially. Sigmoid calibration effective at small sample sizes per sklearn docs. Upgrade to isotonic regression at 200+ docs. | sklearn calibration.rst, NotebookLM CRC research |

---

## 8. Why Not S1–S5? (Stakeholder Summary)

The Pipeline Workflow document proposed tracking 5 discrete confidence scores (S1–S5), one per pipeline step. We evaluated this thoroughly and chose to keep the unified QCS model with targeted fixes. Here's why:

**1. It multiplies the calibration burden, not the insight.**
Each of the 5 scores needs its own threshold tuning, weight calibration, and ground-truth labeling. With our current labeled dataset (zero documents), we'd be guessing 5 sets of thresholds instead of one. The scoring architecture isn't the bottleneck — the calibration of Docling's raw confidence signals is. Switching to S1–S5 spreads the same poorly-calibrated inputs across more dimensions without fixing the root cause.

**2. The diagnostic value we need is captured without a full rewrite.**
S1–S5's main advantage is diagnostic: "S2=0.3 tells you segmentation failed." We get the same insight by adding `segmentation_confidence` and `mapping_confidence` as named fields in the QCS report — visible in the HITL UI, logged for analysis — without rebuilding the scoring engine. The unified QCS still drives routing; the diagnostics tell operators *why*.

**3. Validation rules are the real quality gate, not the scoring model.**
Whether we use 1 score or 5, the actual error detection comes from deterministic validation: balance equation checks, subtotal consistency, cross-statement coherence (V1–V4 rules). These rules catch real financial errors. The scoring model's job is to route documents to human review when validation can't run or returns failures — it doesn't replace validation. A simpler routing model with strong validation rules outperforms a complex routing model with weak rules.

**4. The unified model extends cleanly to VLM integration.**
When VLMs are added later, their logprobs slot directly into the existing Intrinsic layer. LLM-as-Judge results slot into the Semantic layer. No architectural change needed. S1–S5 would require defining new scores (S6? S7?) for each new capability.

> **Bottom line**: We adopted the diagnostic insights from S1–S5 (what failed and where) without adopting the architectural complexity (5 independent scoring functions). The fixes to the unified model — grade-mapping, severity weighting, hard gates — address the actual calibration problems that S1–S5 would inherit unchanged.

---

## 9. Implementation Sequence

Based on the analysis and research validation, the scoring fixes should be implemented in this order:

**Short-term (before Tier V1 launch):**

1. Replace raw `parse_score`/`ocr_score`/`layout_score` with grade-mapped values
2. Remove `table_score` from intrinsic (not implemented in Docling)
3. Add `low_grade` hard pre-gate (POOR → HITL, skip QCS)
4. Implement Pandera lazy validation with severity-tagged checks
5. Compute severity-weighted semantic score from `failure_cases`
6. Add `critical_failures` count → hard HITL override if > 0
7. Set Docling OCR engine `confidence_threshold` at ingestion

**Medium-term (after 50 labeled docs):**

8. Fit Platt scaler on labeled QCS → HITL probability mapping
9. Add `segmentation_confidence` and `mapping_confidence` to heuristic layer
10. Upgrade to isotonic regression calibration at 200+ labeled docs

**Deferred (VLM integration phase):**

11. Add logprobs to intrinsic layer (from VLM outputs)
12. LLM-as-Judge consistency checks
13. Perplexity-based quality signal via quantized Llama-3-8B

---

## 10. Research Sources

- **Docling confidence scores**: `docling/docs/concepts/confidence_scores.md` — grade vs. score distinction, `parse_score` as 10th percentile, `table_score` unimplemented
- **Pandera lazy validation**: `pandera` library — `failure_cases` DataFrame, severity tagging via check error prefixes
- **sklearn calibration**: `sklearn/doc/modules/calibration.rst` — Platt scaling (sigmoid), isotonic regression, `CalibratedClassifierCV`
- **NotebookLM deep research**: FNRP/SwF1 severity patterns, ED-ECE calibration, Conformal Risk Control, Expert Threshold routing, deterministic-first validation layering
