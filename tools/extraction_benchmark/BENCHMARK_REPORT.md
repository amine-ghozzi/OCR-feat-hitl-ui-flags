# Extraction Benchmark — Methodology, Results & Corrections

_Corpus: 8 Tunisian NCT financial statements (6 `.docx`, 1 `.jpg`, 1 native `.pdf`) under `Samples/`._
_Baseline run `full-20260607` → final run `full-20260608-final`. All code merged to `main` (`66cc9da`)._

---

## 1. Methodology

### 1.1 Harness pipeline
Each run is a sequence of deterministic stages (`python -m tools.extraction_benchmark <stage> --run <id>`):

| Stage | What it does |
|---|---|
| `sample` | Stratified pick of documents → `_sample.json` |
| `cases` | Runs the **production pipeline** on each doc → `cases/<doc>.json` (the candidate extraction) |
| `prompts` | Builds the per-case prompt package (schema + source excerpt) |
| `reference` | An **Opus oracle** reads each source doc and writes the canonical ground-truth `reference/<doc>.json` |
| `judge` | Scores each candidate vs its reference (see §1.2) |
| `report` | Aggregates → `report/findings.md` + `summary.json` |

References are produced **independently from the source document** (never from the candidate), so they are a true oracle, not a self-fulfilling target.

### 1.2 Two-layer scoring (the core methodology fix)
A mid-project finding drove a redesign: **a single LLM judge is too noisy to trust.** Evidence — the judge once scored a candidate whose *every* value was 1000× wrong (`bilan1`, 0/51 correct) as "1 defect," then scored an objectively *better* run *lower*. Its run-to-run variance (~±0.05) exceeded the effect size of a real fix.

The fix follows the **Evaluation Stack "apply in order"** principle (L1 deterministic first, then L2 LLM):

- **L1 — deterministic metrics** (`metrics.py`, zero-cost, variance-free) compute value accuracy, sign accuracy and completeness by matching `(row-label, value)` tuples against the reference. Three brackets are reported:
  - `value_accuracy` (**strict**, same truncated label) — the lower bound, **feeds the gate**.
  - `value_accuracy_fuzzy` (difflib-fuzzy label) — the fair middle.
  - `value_recall_label_agnostic` (value present anywhere) — the upper bound.
  - A gap between strict and the others is a **relabeling effect** (OCR garble in the row label), *not* a value regression.
- **L2 — LLM-as-judge**, restricted to the two genuinely *subjective* dimensions (`mapping_correctness`, `table_structure`); the four objective dimensions are computed deterministically and the LLM's scores on them are advisory only.

**Blended score:** `benchmark_score = 0.7·L1_deterministic + 0.3·L2_subjective`.

### 1.3 The N-judge panel (variance reduction)
L2 runs as a **panel of N=3 independent judges per case** (`judge --panel 3` → `<case>.j{1,2,3}.verdict.json`). `report` aggregates them by:
- **per-dimension median** (robust to a single outlier judge — the exact failure mode observed),
- **majority-vote defects** (kept only if ≥⌈N/2⌉ judges raise them),
- an **agreement** metric (mean inter-judge stddev) — low = consensus is trustworthy.

Judge prompts use **G-Eval** (cite evidence before scoring), run as a **`critic`** (intended temp 0.0), and emit a **`reference_suspect`** list so cases where the candidate beats the reference are quarantined from scoring instead of penalizing the candidate.

### 1.4 Calibration & gate
- A **calibration gold-check** (`tests/test_benchmark_calibration.py`) feeds a perfect candidate (→ ~1.0) and a ×1000-corrupted one (→ ~0.0); if the metric ever passes the corrupted one the harness fails loudly.
- A **standalone L1 pytest gate** (`tests/test_benchmark_l1_gate.py` + `gate_baseline.json`) fails CI on a value-accuracy regression.
- **Gate pass** = `benchmark_score ≥ 0.8` **and** high-severity defects `== 0`.

---

## 2. Results — first run vs final run

| Metric | Baseline `full-20260607` | Final `full-20260608-final` | Δ |
|---|---|---|---|
| **Benchmark score** | 0.6792 | **0.9038** | **+0.225** |
| **Gate** | ❌ FAIL | ✅ **PASS** | — |
| Deterministic L1 | — (LLM-only then) | **0.9322** | — |
| LLM-judge (panel) | 0.6792 (single, noisy) | 0.85 (N=3 median) | — |
| Total defects | 36 | **2** | −34 |
| High-severity defects | 18 | **0** | −18 |
| Panel agreement (stddev) | n/a | 0.18 (judges agree) | — |
| Value bracket (strict ≤ fuzzy ≤ label-agnostic) | — | 0.855 ≤ 0.914 ≤ 0.929 | — |

### Per-dimension (final prod avg; gap vs reference)
| Dimension | Baseline prod | Final prod | gap (base → final) |
|---|---|---|---|
| table_structure | 3.00 | 4.375 | 1.875 → **0.000** |
| invariant_satisfaction | 2.75 | 3.75 | 1.375 → 0.500 |
| mapping_correctness | 2.25 | 4.00 | 2.625 → 0.625 |
| number_sign_parsing | 3.50 | 4.375 | 1.375 → 0.500 |
| cell_value_accuracy | 4.25 | 4.375 | 0.75 → 0.625 |
| completeness | 4.625 | 4.625 | 0.25 → **0.000** |

### Per-case deterministic value accuracy (final)
| Case | strict | fuzzy | matched/ref |
|---|---|---|---|
| Etat_résultat_2 | 1.00 | 1.00 | 42/42 |
| bilan3 | 0.99 | 0.99 | 76/77 |
| Etat_résultat_1 | 0.97 | 0.97 | 31/32 |
| bilan1 | 0.94 | 1.00 | 48/51 |
| EF_CLB.pdf | 0.84 | 1.00 | 81/96 |
| bilab2 | 0.82 | 0.84 | 58/71 |
| Etat_résultat_3 | 0.76 | 0.76 | 57/75 |
| Bilan_1.jpg | 0.70 | 0.80 | 62/88 |

> The catastrophic baseline failures are gone. EF_CLB (a native two-page PDF) went from **12/96 → 81/96**; `bilan1`, whose every value was 1000× wrong at baseline (0/51), is now 48/51. The deterministic value-match across the corpus rose **304 → 457 (+50%)** over the project.

---

## 3. All corrections made

### 3.1 Extraction pipeline (production code)
**Basis** legend: ✅ deterministic (no judgment) · ⚠️ rests on a corpus **assumption / heuristic / trade-off** (highlighted, see §3.4) · 🛑 attempted then **reverted as ambiguous**.

| # | Correction | Basis | Commit |
|---|---|---|---|
| ISSUE-1 | French number parsing: `.`/`,` thousands-sep misread as decimal; `<…>` negatives; trailing-minus | ⚠️ corpus assumption: `sep + 3-digit group = thousands` (integer-amount statements) | `71ad94c` |
| ISSUE-2 | Cross-statement vocabulary leak — thread `statement_type` so a compte_resultat row can't match bilan terms | ✅ | `e4850e0` |
| ISSUE-3 | Missing compte_resultat synonyms (`Production stockée`, `Produits financiers`, …) | ✅ | `e4850e0` |
| ISSUE-5 | Residual/qualifier over-eager fuzzy match + candidate-log hygiene | ✅ | `e4850e0`, `502b18d` |
| ISSUE-6 | Var% invariant false positives — guard against an absolute-delta column read as % | ⚠️ plausibility heuristic (`|%| > 1000` treated as implausible) | `6493e6d` |
| ISSUE-7 | Note column selected as value column; empty section-header rows duplicated as leaves | ✅ | `23b5db1` |
| ISSUE-8 | Section misassignment — close/reset the active section at its subtotal | ✅ | `a93f75d` |
| ISSUE-9 | **Re-diagnosed**: not a two-panel merge but an explode misalignment → **tier-0 y-coordinate row reconstruction**; + T3 ensemble red-label suppression; + tier-0 label authority / orphan suppression | ✅ (geometry; `y_tol=3pt` tuned constant) | `1b2240b`, `744fdcf`, `1296628` |
| ISSUE-4 | Grand-total absorption — section-discriminating qualifier sets | ⚠️ **trade-off**: dropped the over-strict `propres` token (it caused an audit false-positive) — which re-opened the grand-total collapse later fixed by Tail ①② | `502b18d` |
| ISSUE-10 | Candidate-log duplication hygiene (251 → 31 rows; dup-ratio 0.86 → 0.0) | ✅ | `502b18d` |
| A1 | Space-grouped thousands + comma = decimal (`11202 126,240` → 11202126.240) | ✅ (space already groups thousands ⇒ comma is unambiguously decimal) | `4cb4bfd` |
| A2 | Mapping synonyms, incl. **curated** `Charges financières nette(s)` → `Charges financières` | ⚠️ **judgment**: a *net* line canonicalized to the base concept (debatable; matches the reference, reverses an earlier "don't bind" decision) | `4cb4bfd` |
| B3 | Confidence honesty — flag risky OCR recoveries (`-52955 0`, `060201-`) **yellow → HITL** | ⚠️ **acknowledges ambiguity** (routes to human, does NOT resolve the value) | `4cb4bfd` |
| comma-inference | Per-document comma-convention inference (resolves no-space `3811603,553` → decimal) | ⚠️ **document-level heuristic** (validated on all 8 docs; could mis-infer a mixed-convention doc) | `bdf8d35` |
| multi-comma | Parse multi-comma thousands (`34,340,000`) — recovered EF_CLB **18 → 81/96** | ✅ (2+ commas ⇒ unambiguously thousands) | `ea2d1be` |
| Var% recompute | Wire `auto_correct_variation_percentages` into the extraction path | ✅ arithmetic (recomputed cells flagged yellow if a period value is itself suspect) | `c9aa194` |
| Tail ①② | Compound grand-total searches **unscoped** so it isn't hidden by section-scoping | ✅ | `66cc9da` |
| Tail ④ | Intermediate equity subtotal (`…avant résultat`) left null (matches reference) | ⚠️ **presentation judgment** (label-pattern guard; "avant résultat" treated as a non-canonical intermediate) | `66cc9da` |
| Tail ⑤ | OCR-truncated label resolves via a variation-alias **fallback** | ⚠️ **brittle**: depends on OCR producing a near-complete label; fails on heavy truncation | `66cc9da` |
| ③ (attempted) | Dot-as-decimal in comma-decimal docs | 🛑 **REVERTED — genuinely ambiguous** (net −10 on corpus; see §4) | n/a |

### 3.2 Benchmark methodology (harness)
| Correction | Commit |
|---|---|
| L1 deterministic metrics + report blend + calibration gold-check | `d478307` |
| L1 label-fuzzy + label-agnostic complements | `f059419` |
| N-judge panel + standalone L1 pytest gate | `02237f6` |
| Judge rubric: critic preset + G-Eval + objective-dims-advisory + `reference_suspect` | `bdb7931` |

### 3.3 Reference (ground-truth) audit
- `bilan3` — corrected a **sign error** (asset-block Provisions `41219 → -41219`, ⚠️ *inferred* from the net invariant, not read directly) and a **wrong mapping** (asset-side contra line → null, not the liability `Provisions` concept). `Etat_résultat_1` audited and confirmed correct.

### 3.4 ⚠️ Corrections that rest on ambiguity / judgment (read this before trusting them)
Most fixes are deterministic. These nine are **not fully certain** — each rests on a corpus assumption, a heuristic, or a human judgment, and could be wrong on a document outside this corpus:

| Correction | The ambiguity / judgment | Failure mode if the assumption breaks |
|---|---|---|
| **ISSUE-1** number parsing | `sep + 3-digit group = thousands` assumes integer-amount (millimes-free) statements | a genuine 3-decimal value (`1,250`=1.25) would be read as 1250 |
| **comma-inference** | infers ONE comma convention per document from signals | a document that genuinely mixes comma-decimal and comma-thousands mis-parses |
| **ISSUE-4** `propres` drop | removing the discriminator was a trade-off vs an audit false-positive | needed Tail ①② as a compensating fix; a different grand-total label could still slip |
| **A2** `Charges financières nette(s)` | a *net* line treated as ≡ the base concept (curated) | if a statement reports both gross and net financial charges, they'd collide |
| **ISSUE-6** Var% guard | `|%| > 1000` ⇒ "not a percentage" | a real >1000% YoY swing (tiny prior-year base) would be skipped |
| **Tail ④** intermediate-equity null | label pattern `capitaux propres avant resultat` ⇒ non-canonical | a statement using that label *as* its final total would be left unmapped |
| **Tail ⑤** alias fallback | relies on OCR producing a near-complete label | heavy truncation still falls through (as it did this run) |
| **B3** risky-parse flag | does **not** resolve the value — flags it yellow for HITL | correct by design; the value is still unknown until a human/VLM reads it |
| **Reference audit** (bilan3 sign) | the corrected value was *inferred* from the balance invariant | if the sibling cells were also wrong, the inference would be too |

The single correction that was **attempted and abandoned** as irreducibly ambiguous is the **dot-as-decimal** rule (③) — see §4.

---

## 4. Remaining known limitations (the 2 final defects)

1. **[medium] Dot-decimal ambiguity** (`bilan1` `12083312.401`). In a comma-decimal document, a lone `.` is sometimes a decimal (`-1 098 948.081`) and sometimes a thousands grouper (`1.603448`), with **no per-token signal to separate them — and the references themselves are inconsistent across documents**. A blanket rule was implemented, measured (**net −10** on the corpus), and **reverted**. This is the one defect that needs document context the data does not provide; the honest resolution is HITL or a future VLM tier-2.
2. **[low] `Etat_résultat_3` `Variation des stocks…` → null.** The variation-alias fallback (Tail ⑤) resolves this when the OCR label is intact, but the docx extraction garbles/truncates that label non-deterministically run-to-run, so it occasionally still falls through.

### Deferred (out of scope, as agreed)
- **VLM tier-2** (the `QCS=0` stub) — the ceiling for scanned-image (`Bilan_1.jpg`) OCR quality (split/garbled digits like `060201-`→true `-107090` are not deterministically recoverable).
- **Judge temperature=0 enforcement** — infra-blocked in this environment (no API key); the N=3 panel median is the working variance mitigation.
