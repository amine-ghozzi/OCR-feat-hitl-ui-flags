---
name: auditing-match-quality
description: >
  This skill should be used when reviewing or trusting captured label-matching output
  or feedback data before acting on it — e.g. "are these variation candidates correct?",
  "review the fuzzy matches before promoting", "why did the matcher map X to Y?",
  "dedupe the candidate log", or before committing/promoting a candidate file. It diagnoses
  two recurring failures: approximate matches that are semantically wrong because a token
  flips the meaning (residual "autres", "total", "net", "brut"), and append-only
  candidate/feedback logs that inflate evidence through duplication. It does NOT cover
  building new nomenclature entries or general OCR/extraction accuracy.
version: 0.1.0
---

# Auditing Match Quality

Diagnose and fix two recurring failure classes in this repo's label matching: (1) **semantic
false positives** — a fuzzy/approximate match that is wrong because a discriminating token flips
the meaning, and (2) **candidate-log duplication** — append-only feedback logs that inflate
promotion evidence by re-logging the same observation. Produce a diagnosis, the fix, and tests.

## Inputs
- A candidate/feedback log (default `data/variation_candidates.jsonl`), **or** a specific bad
  mapping to investigate (`raw_text → matched_to`).
- Required tools: `venv/Scripts/python` (the repo venv — never system python).

## Steps
1. Run the analyzer to get duplication + mismatch signal:
   `venv/Scripts/python .claude/skills/auditing-match-quality/scripts/audit_candidates.py [path]`.
   It prints `total`, `unique_pairs`, `duplication_ratio`, `empty_document_id_pct`, and the list of
   qualifier mismatches; it exits non-zero when duplication or mismatches exceed the threshold.
2. Triage **correctness**. For each flagged `raw_text → matched_to`, compare the discriminative
   qualifier sets via `accounting.nomenclature._qualifier_set(_normalize(text))`. A set difference
   means the match is semantically wrong (a bare concept matched its `Autres …`/`Total des …`
   sibling). Confirm against `src/accounting/rules/nomenclature_data.yaml` (both the residual and
   the total entry usually exist).
3. Triage **duplication**. If `unique_pairs ≪ total` or `empty_document_id_pct` is high, the log is
   inflated — usually one document re-logged across pipeline stages/runs, or empty `document_id`.
4. Attribute the root cause: **matcher** (wrong mapping = `nomenclature._fuzzy_match_inner`) vs
   **logger** (duplication = `accounting/variation_logger.py`) vs **DB write path**
   (`api/routers/review.py`).
5. Apply the matching fix-class: inject a **domain constraint** (filter the candidate pool by
   qualifier set) — do NOT swap rapidfuzz scorers and do NOT raise the threshold. (`token_set_ratio`
   scores a subset 100; embeddings/char-n-gram TF-IDF miss the exclusivity — see Gotchas.)
6. Apply the logger fix-class: make writes **idempotent** (dedup key `(raw, matched, document, status)`,
   preloaded across runs) and count promotion evidence by **distinct documents**, never raw lines.
   In the DB, keep one row per `(org_id, raw_text, matched_to)` and UPSERT `seen_count`.
7. Add regression tests for every fix, then run `venv/Scripts/python -m pytest tests/ -q` and confirm
   green before reporting.

## Output
A short report with: (a) the analyzer metrics; (b) a table of confirmed wrong matches
(`raw_text → matched_to`, why); (c) the root-cause attribution; (d) the edits made (file:symbol);
(e) the new tests and the passing suite count. If asked only to *review*, stop after (a)–(c) and
recommend — do not edit.

## Edge Cases
- **Empty `document_id`** rows (local runs) carry no promotion evidence — never let them drive a
  promotion count; collapse them out, don't count them as distinct documents.
- **Candidate pool fully filtered out** by the qualifier guard → return unrecognized/custom. That is
  correct: a wrong match is worse than no match.
- **Qualifier legitimately on both sides** (`Placement & autres …` → `Placements et autres …`) → the
  sets match, so the guard must allow it. If your fix rejects these, the qualifier detection is wrong.
- **OCR-concatenated tokens** (`TotalActifsImmobilises`) hide the qualifier inside one token — detect
  it as a prefix, not only as a whole token, or you will reject correct totals.

## Gotchas
- `fuzz.token_set_ratio` returns **100 when one string is a subset of the other** — using it here
  makes residual false positives worse, not better. The repo correctly uses `token_sort_ratio`.
- rapidfuzz has **no native token-importance weighting** (`scorer_kwargs` weights are Levenshtein edit
  costs). The residual/total problem is *semantic*, not lexical — no scorer config fixes it.
- Embeddings and **character-n-gram TF-IDF** rate `X` ≈ `autres X` as near-identical (similarity, not
  exclusivity). Don't reach for them to solve this; use the deterministic qualifier constraint.
- Promotion thresholds count **distinct documents**, not log lines. Counting lines is what let 162×
  one document look like strong evidence.

## Examples
- `PRODUITS D'EXPLOITATION → Autres produits d'exploitation` (conf 0.87): qualifier sets `{}` vs
  `{autres}` differ → **wrong** (residual ≠ section concept). Fix: filter `autres`-mismatched
  candidates from the pool → returns unrecognized. Correct sibling `Total des produits d'exploitation`
  is also excluded (`{total}` ≠ `{}`), which is intended for a bare header.
- `TotalActifsImmobilises → Total des actifs immobilisés`: both qualifier sets `{total}` (prefix
  detection on the concatenated token) → **allowed**, stays matched.
- Same `(raw, matched, document)` logged 162× → analyzer shows `duplication_ratio` ≈ 0.99; fix is the
  idempotent write + distinct-document counting, not a one-off file scrub.
