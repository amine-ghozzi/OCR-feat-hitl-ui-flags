# Known gaps — unit/scale binding & the continuation threshold

> Status (2026-07-31): **documented, not fixed.** Both were found during the
> post-OCR statement-classification work. Neither blocks the current pipeline;
> both are latent and worth closing deliberately rather than opportunistically.
>
> **Update 2026-09-14:** the D2 paragraph below was wrong about D2 being latent
> and blocked for lack of data — it is a **live defect** with calibration pairs
> already in the corpus (see "D2 — correction"). D1 latent, D3 deferred by
> measurement, D4 open. Tracked as VAL-7 / CLS-6-7-8 / CLS-17 / CLS-4 in
> [[Master-Plan]].

---

## D1 — unit / scale is never bound (a latent 1000× error)

A statement headed `(en mDT)` read as though it were `(en DT)` is wrong by a
factor of 1000, and nothing in the model can currently express the difference.

### Why it cannot be represented today

| Where a unit would live | Field exists? |
|---|---|
| `CanonicalTable` (`canonical_model.py`) | ❌ no unit field |
| `CanonicalCell` | ❌ |
| `ColumnLayout` (`column_identifier.py`) | ❌ |

`french_number` parses a token in isolation, which is the right scope for it —
the unit is never in the cell. It lives in either:

* a **column header** — `2024 (en mDT)`, `(en DT)`; or
* a **caption above the table** — `Montants exprimés en milliers de dinars`.

The caption case is harder: the pipeline does not retain the non-table text
adjacent to each table, so there is currently nothing to inspect.

### It is inconsistent *within* a single document

`Samples/EF CLB 2024 Provisoire (1).pdf` carries `(en DT)` as a column header on
**pages 1 and 4 but not on page 3**. A naive per-table reading therefore treats
the Bilan Passif as unit-less while its own Actif is explicit — so per-table
detection alone is not enough; the document-level default has to propagate.

### Fix sketch

1. Detect the unit token in column headers **and** in the text block immediately
   above the table (requires threading that text through, as page/offset
   provenance already is).
2. Bind it as `CanonicalTable.metadata['unit']` plus a scale factor.
3. Feed `scale_resolver` — it already arbitrates ambiguous magnitudes from the
   arithmetic identities and is the natural consumer.
4. Raise a check when statements within one document **disagree**: a Bilan in
   mDT beside a Compte de Résultat in DT is far more likely an extraction error
   than a real presentation.

### Open decision before implementing

Whether to **rescale values** or merely **record the unit**. Rescaling silently
would violate the codebase's standing rule that a reconstructed value is never
emitted green (cf. `prune_noise_columns`' repair path and Stage 8b, which both
flag `yellow`). The safe default is: record the unit, surface the scale, and let
the reviewer confirm.

---

## D2 — the continuation threshold is inherited, and its fixture is authored

`table_merger._CONTINUATION_THRESHOLD = 2.5` predates this work and has never
been calibrated against data.

### Measured

| pair | score |
|---|---|
| authored Bilan Actif split across two pages | 3.5 |
| Bilan Passif → Compte de Résultat, identical layout | **2.5 — exactly the threshold** |
| real document, Actif → Passif | 1.0 |

The middle row is the problem. A pair that must *never* merge scores **precisely
the bar**, so the score carries **zero margin**, and all of the safety rests on
the statement-type veto added alongside it (`ContinuationSignal.same_statement`).

**Consequence: that veto is load-bearing.** It must not be removed, weakened, or
turned into a soft signal without first recalibrating the threshold.

### Why it was deferred (2026-07-31 reasoning — superseded)

The original text argued that no sample in the repo contains a statement
continued across a page break (`EF CLB` page 2 being blank), so any threshold
would be fitted to authored fragments. That reasoning was wrong: it looked only
at the small samples, not at the 9 multi-page packs.

### D2 — correction (2026-09-14)

The 2026-08-14 full-corpus sweep grouped four statements. A verifier read each
merge against the source pages:

| sweep merge | what the pages hold | verdict |
|---|---|---|
| ciments_bizerte `compte_resultat` pages [46, 47, 48] | Notes 18, 19 and 20 — three different note tables | **false merge** |
| ciments_bizerte `bilan_actif` pages [41, 42] | two unrelated note tables with different totals | **false merge** |
| COHAPROM `bilan_actif` pages [11, 11] | Notes 3–6 on the **same page** | **false merge** (same page) |
| *(not merged)* ciments_bizerte Note 14, pages 40 → 41 | a genuine continuation; the halves were typed `bilan_passif` and `bilan`, so the same-statement veto blocked it | **missed** |

So the corpus holds one positive and three negative calibration pairs today, and
the grouping is currently worse than "no grouping": it fuses distinct notes and
misses the one real continuation. Two defects follow:

1. `statement_builder._is_continuation` accepts a page gap of **0**
   (`src/accounting/statement_builder.py:223`, `not in (0, 1)`) although the
   docstring says consecutive *pages* — same-page merges must be forbidden.
2. The veto depends on statement typing that D4 gets wrong on notes; the
   p40→41 pair cannot merge until the typing improves or the veto treats
   `bilan` / `bilan_passif` as one family.

The measured scores are unchanged (authored Actif split 3.5; Passif→CR 2.5 —
exactly the bar; real Actif→Passif 1.0), so the veto stays load-bearing.

### Calibration plan (now possible)

1. Forbid same-page merges first (CLS-8), then score the four corpus pairs.
2. Choose the threshold from the observed **separation**, not from a round
   number — and record the sample it came from next to the constant.
3. Re-check that the Passif→CR case still fails on score *alone*, so the veto
   stops being the only line of defence.
4. Only then revisit the veto's tolerance for `bilan`/`bilan_passif`.

The caveat next to the fixture in `tests/test_phase0_fixes.py` should point
here rather than repeat the "no sample" claim.
---

## D3 — the quarantine gate cannot be moved earlier

Requirement 3 says an unrecognized statement "shouldn't continue through the
process". The obvious reading is to gate *before* enrichment. **Measured, that
is wrong** — it would destroy real data.

Classification quality depends heavily on which representation it sees. Same
document, same classifier, same gate:

| statement | on the **voted** output | on the **raw per-tier** frame |
|---|---|---|
| Bilan Actif | pass, margin 19 | pass, margin 7 |
| Bilan Passif | pass, margin 20 | pass, margin 4 |
| **Compte de Résultat** | **pass, margin 18** | **FAIL, margin 1** |

Tier 0 emits 3-row mega-cell frames; the rows are only exploded and reconciled
later. Gating on those frames would quarantine a genuine Compte de Résultat —
exactly the over-eager-gate data loss the design set out to avoid.

**Consequence.** The gate runs last, on the final tables, and therefore cannot
un-run the matcher — enrichment has already touched every table by then. What it
*can* enforce, and now does, is that a quarantined statement stops contributing
validated output: its canonical table is marked `gate: quarantined` and the
validation report is recomputed over the surviving statements only, so its
checks no longer feed the QCS semantic layer.

A stronger gate (skipping the matcher itself) would require enrichment to run
twice — once to produce a classifiable representation, once for real — or a
cheap pre-pass that reconstructs rows well enough to classify without the full
DAG. Neither is justified by current evidence.

`tests/test_phase0_fixes.py::test_the_gate_must_not_run_on_raw_per_tier_frames`
pins the measurement so this is re-checked rather than re-argued.

---

## D4 — a "statement" is still largely one extracted table

Requirement 2 asked that a document be able to hold several statements. It does
— and true continuations now group (same type, consecutive pages, continuation
detector agreeing). But **grouping distinct tables into the statement they
belong to is not solved**, and the corpus shows why that matters.

### Measured across all 17 samples

| | |
|---|---|
| `ciments_bizerte_efi300625.pdf` | 65 tables → 62 statements, **26 labelled `bilan_actif`** |
| `somocer_efc311224.pdf` | 36 tables → 36 statements |
| `air_liquide_efi300625.pdf` | 28 tables → 28 statements |

A company has **one** Bilan Actif. The other 25 are notes elaborating it —
immobilisations detail and so on — which classify as `bilan_actif` because they
use the balance sheet's vocabulary. So the inventory is closer to *a labelled
table list* than a statement list.

### Why this is not simply "merge same-typed tables"

Folding those 26 together would be considerably worse than leaving them apart:
a note about immobilisations is not part of the Bilan Actif, and merging would
corrupt both the canonical output and every arithmetic check that runs over it.
The grouping deliberately refuses to do it. It did merge 4 statements across
the whole corpus — and, as the D2 correction above records, **all four were
false merges** of distinct note tables, while the one genuine continuation
(ciments Note 14, p40→41) was missed. So today no multi-page statement is
grouped correctly, and the over-count is compounded by occasional fusion of
unrelated notes.

### What is actually missing

A way to distinguish a **primary statement** from a **supporting note that
shares its vocabulary**. Signals worth exploring, none yet implemented:

* **Position** — the primary statements lead the pack; notes follow.
* **Structure** — the Bilan Actif carries the section headers (`Actifs non
  courants` / `Actifs courants`) and a grand total (`Total des Actifs`); a note
  carries neither.
* **A note reference** — notes are numbered and referenced from the statement's
  `Notes` column, which the binder already identifies.
* **Completeness** — a primary statement's subtotals foot to its grand total;
  a note's do not, because it is a fragment of one.

The structural signal looks strongest: `header_detector` and
`subtotal_detector` already compute most of what it needs.

### Until then

The inventory over-counts on large packs, and `statements` should be read as
"classified tables, with true continuations joined" rather than "the statements
in this document". The count is honest for small documents (1–3 statements) and
inflated for full accounts packs.
