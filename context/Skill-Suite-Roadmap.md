# FinAlze Skill Suite — Roadmap (Continuous Improvement · Control · Corrections)

Repo-local agent skills that make the extraction system **self-correcting**: detect problems,
apply human corrections consistently, and feed the fixes back so accuracy compounds. Each skill is
authored to the skill-authoring standard, lives in `.claude/skills/<name>/`, and is **not** registered
in any external portfolio. Skills stay scoped to one recurring task class (the standard warns that
over-broad skills trigger unreliably and over-narrow ones force multiple loads at once).

## The loop these skills close

```
   CONTROL (detect / gate)  →  CORRECTIONS (human fixes)  →  IMPROVEMENT (system learns)
   monitoring-extraction-drift   triaging-review-queue         promoting-variations
   gating-extraction-regressions tuning-validation-rules       recording-lessons → golden set
        ▲──────────────────────────────────────────────────────────────────────────────┘
                         (auditing-match-quality: detect+fix node, already built)
```

A failure is **detected** (control), a human **corrects** it (corrections), and the correction becomes
a durable change + a regression test (improvement). `auditing-match-quality` (built) sits on the
detect→fix edge for the matcher/feedback-log; the skills below complete the rest of the loop.

**Research grounding.** Validated against a NotebookLM deep-research survey of SOTA self-improving
extraction systems — see [Research-Self-Improving-Extraction-Systems.md](Research-Self-Improving-Extraction-Systems.md)
(MADP multi-agent IDP, LLM Readiness Harness, Snorkel weak supervision, ConTrOn/ODKE+ ontology loops,
Hyperscience/Rossum, ML-observability drift theory). The per-skill upgrades below carry citations.

**Multi-speed learning** is the mental model for the whole loop: a **fast path** (recent HITL
corrections injected as in-context examples to the Tier-2 VLM — instant adaptation, no retraining;
"PFTFI") and a **slow path** (durable consolidation = our nomenclature promotion). Today only the slow
path exists; the fast path is gated on Tier-2. Both feed the *same* validated-correction store.

## Status legend
- ✅ built · 🟢 now (runs on existing code) · 🟡 next (existing + small new) · 🔴 gated (needs new infra)

---

## Corrections — turn human fixes into durable changes

### promoting-variations 🟢
- **Trigger:** "promote the approved candidates", "update the nomenclature from feedback", "which variations are ready to promote?"
- **Scope:** Review candidates that passed the distinct-document threshold + consistency check and safely write them into `nomenclature_data.yaml` (new canonical term or variation), with an audit note and a re-run of the matcher tests. The completion of the loop `auditing-match-quality` opens. NOT for inventing canonical terms with no feedback evidence.
- **Reuses:** `variation_logger.get_promotable_candidates` (distinct-doc counting), `rules/nomenclature_data.yaml`, `routers/feedback.py` (`/variations`), `_qualifier_set` (don't promote a qualifier-mismatched mapping).
- **New infra:** the YAML promotion writer (`promote_variations` is referenced in the logger docstring but unimplemented).
- **Depends on:** `auditing-match-quality` (vetting must precede promotion).

### triaging-review-queue 🟢
- **Trigger:** "work the HITL queue", "resolve the flags on this document", "what needs review?"
- **Scope:** The standard analyst loop — claim from `/review/queue`, read the V1–V4 report + conflicts, apply `PATCH /cells` corrections, transition Draft→Validated (or Rejected) with a reason. Makes correction *behavior* consistent so the feedback loop receives clean signal. NOT for bulk auto-approval.
- **Reuses:** `routers/review.py` (queue, validation view, cells, transition, audit), `rule_engine.ValidationReport`.
- **New infra:** none.

---

## Control — gates and regression prevention

### tuning-validation-rules 🟢
- **Trigger:** "the V1–V4 flags are too noisy", "tune thresholds for org X", "why is this rule over-firing?"
- **Scope:** Analyze rule flag stats, adjust per-org `tau_*` / rule tolerances **without redeploy**, and report before/after impact on automation vs. HITL rate. NOT for changing rule *logic* (that's a code change).
- **Reuses:** `/feedback/rules/flags`, `OrgConfig`, `routers/config.py`, `rule_engine`.
- **New infra:** none (per-org config already editable).

### gating-extraction-regressions 🔴
- **Trigger:** "run the quality gate", "did this change regress extraction?", "score against the golden set"
- **Scope:** The foundation-first pre-deploy gate — run the golden set, score with deterministic checks (CER / TEDS / GriTS + V1–V4) and a *validated* LLM-judge, compare to baseline envelopes, block on regression. NOT a post-deploy monitor (that's `monitoring-extraction-drift`).
- **Reuses:** `/feedback/ground-truth` (golden source), DeepEval.
- **New infra:** golden-eval harness, baseline envelopes, judge validation (Gwet's AC1 / jury). The largest new build — per the de-risked plan, gated on a labelled corpus.

### self-healing-extraction 🟡  *(surfaced by the research — "Try-Heal-Retry")*
- **Trigger:** "the parser keeps failing on this format", "auto-recover extraction errors", "why did ingestion throw on this doc?"
- **Scope:** Intercept a parse/extraction failure instead of terminating; route the error context + raw sample to a diagnostic step that emits **structured Pydantic repair parameters** (delimiter / encoding / column-offset / tier override); retry once with the adjusted config (Tenacity-style), and log the heal for review. Cuts manual pipeline babysitting. NOT for semantic mis-mapping (that's `auditing-match-quality`).
- **Reuses:** `FinAlzePipeline` tier cascade, `canonical_model` schemas; pairs with the existing tier-fallback logic.
- **New infra:** the diagnostic→repair-param→retry wrapper. **Anti-pattern guard:** a heal must still pass V1–V4 before its output is trusted (never auto-accept a healed extraction into the feedback stream).
- **Source:** Towards Data Science "self-healing data pipeline"; MADP validator-retry loop.

---

## Continuous improvement — close the learning loop

### monitoring-extraction-drift 🟡
- **Trigger:** "is extraction drifting?", "weekly quality check", "did the distribution shift?"
- **Scope:** Track cheap reference-free signals over time (V1–V4 pass rate, Consensus Entropy, QCS distribution, `Recall_OCR`) and input/output drift (KS / PSI ≥ 0.25), routing anomalies to HITL or `recording-lessons`. NOT a pre-deploy gate.
- **Reuses:** `/feedback/quality` (incl. the candidate-health KPIs added in the dedup work), `ensemble.consensus_entropy`, `candidate_audit`, Evidently.
- **New infra:** `Recall_OCR` signal, drift monitors (KS/PSI), a time-series of the KPIs.

### recording-lessons (→ grows the golden set) 🔴
- **Trigger:** "log this failure's root cause", "add this to the regression set", "what have we learned?"
- **Scope:** When a failure is found+fixed, record root cause in a Lessons-Learned Registry (error taxonomy) **and** convert the validated correction into a golden case — active learning that feeds `gating-extraction-regressions`. One action, both outputs (deliberately merged).
- **Reuses:** `/feedback/ground-truth`.
- **New infra:** lessons-registry table + golden-set store.

---

## Sequencing (incremental, lowest-risk first)
1. **promoting-variations** — completes the loop the built skill opens; machinery mostly exists. *(small)*
2. **tuning-validation-rules** + **triaging-review-queue** — pure playbooks over existing endpoints; no new infra. *(small)*
3. **monitoring-extraction-drift** — builds on the new `/feedback/quality` KPIs; adds `Recall_OCR` + drift. *(medium)*
4. **gating-extraction-regressions** + **recording-lessons** — the foundation-first gate + lessons/golden-set; biggest new build. *(large, gated on a labelled corpus)*

## Data-flow dependencies
- `auditing-match-quality` (vet) → **promoting-variations** (apply) — never promote an unvetted/qualifier-mismatched mapping.
- `monitoring-extraction-drift` (detect) → `triaging-review-queue` (correct) → `recording-lessons` (learn) → `gating-extraction-regressions` (gate). The golden set grows only from validated corrections.
- `tuning-validation-rules` reads the same signals `monitoring-extraction-drift` watches; keep their KPI definitions shared (one analyzer, like `candidate_audit`).

## SOTA refinements (research-backed — fold into each skill's body when built)
| Skill | Upgrade from the research | Source |
|---|---|---|
| monitoring-extraction-drift | Name the **4 drift types** (feature / prediction / label / **concept**); add **Jensen-Shannon divergence** (symmetric, bounded) beside PSI/KS; use **XAI feature-importance to suppress low-importance drift** (anti-alert-fatigue); arch = inline async telemetry + offline scheduled eval; merge delayed ground-truth via rollup windows. | LLM Readiness Harness; Fiddler; Snowflake `MODEL_MONITOR`; Datadog |
| tuning-validation-rules | Treat V1–V4 rules + nomenclature variations as **weak-supervision Labeling Functions**; track Snorkel's **Polarity / Coverage / Overlaps / Conflicts** per rule to find redundant vs competing rules. | Snorkel Flow |
| gating-extraction-regressions | Adopt the **LLM Readiness Harness**: weighted **Readiness Score**, **risk-first** profile, **policy compliance = hard non-negotiable gate**, **Pareto cost-utility frontier**, **PROMOTE / HOLD / ROLLBACK**. | arXiv 2603.27355 |
| promoting-variations | Our nomenclature *is* the ontology; this is the **ConTrOn / ODKE+** enrichment loop. Add: validate-before-enrich; **inject dynamic ontology fragments into the Tier-2 VLM prompt** (`get_prompt_vocab` is the hook) for type-consistent extraction. | DLR ConTrOn; Apple ODKE+ |
| triaging-review-queue | Active-learning selection by **entropy/margin** (= our Consensus Entropy + QCS); split into **prioritized vs coverage** queues; track **inter-annotator agreement**. | Relativity; Toloka; LlamaIndex |

## Control checklist — anti-patterns to bake into the "Gotchas" of control/improvement skills
The research's four failure modes are acceptance criteria for the loop (verbatim into the relevant skills):
1. **Bias amplification** — never feed unvalidated/low-confidence corrections into learning; mandate human review. *(Our approved-only, distinct-document promotion + `auditing-match-quality` vetting already enforce this.)*
2. **Feedback overfitting** — keep a clean **static** eval set; don't retrain on narrow corrected slices; regularize.
3. **Compounding pipeline errors** — per-stage verification + hard thresholds that halt processing (our tier cascade + V1–V4).
4. **Historical data drift** — strict data/model **versioning** + confidence-over-time monitoring + rapid rollback.
Plus the meta-principle: **isolate feedback-loop monitoring from overall accuracy** — a system can look accurate on monitored outputs while silently degrading on edge cases. *(The candidate-health KPIs added to `/feedback/quality` are exactly this separate signal.)*

## Caveats from the survey (don't over-adopt)
- The literature is IDP/LLM-centric and **does not address French/NCT matching nuance** — the qualifier-guard remains our own contribution, not a borrowed pattern.
- ConTrOn validates new terms against Wikidata/WordNet; those are **weak for Tunisian NCT/SYSCOHADA** — adopt the loop *shape*, skip the external-KB validation step.
- The continuous-fine-tuning / catastrophic-forgetting toolbox (LoRA/EWC/rehearsal) is relevant **only if** we fine-tune Tier-2; the in-context fast path avoids it and stays in scope.

## Out of scope (deliberately)
- **KG / financial-agent skills** — gated until extraction accuracy is proven and a concrete partner use case exists (locked de-risked sequence).
- **Meta skill-quality skills** — the skill-authoring / skill-reviewer / skill-optimizer standards already cover authoring and review; no project-local duplicate.
- **Backend hardening playbooks** (SSRF, tenant isolation, secrets) — operational, not part of the extraction learning loop; track separately if needed.

## Shared-component principle
Where two skills compute the same metric, factor it into `src/` and import from both (as `candidate_audit`
serves the skill *and* `/feedback/quality`). Avoids drift between what a skill reports and what the API/CI reports.
