# Accounting-standard handling — what actually changes between IFRS / NCT / SYSCOHADA

> Status (2026-06-14): **NCT is the only complete vertical.** The standard
> selector swaps the OCR term-correction vocabulary and toggles one validation
> rule; the canonical label-matching / classification / validation engine is
> NCT-based regardless of the selected standard. Default is **NCT** (was IFRS).

## How `accounting_standard` flows

`PipelineConfig.accounting_standard` (default `NCT`) → `process_document` →
Tier-1 → two consumers:
1. **OCR post-processing** — `postprocess_text_with_stats(text, standard)` /
   `postprocess_dataframe(df, standard)` (`pipeline.py` ~625).
2. **Validation** — `validate_with_rules(tables, standard=standard)`
   (`rule_engine.py`).

The API default lives in `api/settings.py` (`default_accounting_standard = "NCT"`,
already NCT); the worker uses each document's stored standard.

## What genuinely differs by standard

### 1. OCR term-correction vocabulary (the main difference)
`preprocessing/text_postprocessor.py:44` loads `terms/{standard}.lower().yaml`.
Three **genuinely distinct** vocabularies (measured):

| | IFRS | NCT | SYSCOHADA |
|---|---|---|---|
| pattern→correction pairs | 225 | 381 | 250 |
| unique to it | 61 | 158 | 46 |

141 pairs are shared by all three (generic structural fixes, e.g.
`TOTALACTIF→TOTAL ACTIF`). Pairwise: NCT∩SYSCOHADA 202, IFRS∩NCT 162,
IFRS∩SYSCOHADA 143. The unique entries encode real standard semantics —
IFRS: right-of-use assets (IFRS 16), contract assets (IFRS 15), biological
assets (IAS 41), *autres éléments du résultat global* (OCI); SYSCOHADA: `HAO`
(hors activités ordinaires), *système normal/allégé*, CAFG; NCT: *amortissements
dérogatoires*, *achats d'approvisionnements consommés*.
**But these only normalize OCR spelling** — no structure, no `validation_field`.

### 2. FFFD (`�`/`?`) placeholder resolution — NCT only
`text_postprocessor.py:234`: resolving OCR placeholders to canonical terms via
the nomenclature runs **only when standard == NCT**. IFRS/SYSCOHADA fall through
to identity (code comment: *"until Phase 6 adds multi-standard dictionaries"*).

### 3. Validation rules — one rule differs
`rule_engine.load_rules(standard)` filters `validation_rules.json` by each rule's
`standards` array. Of 15 rules, exactly **one** isn't universal: **V1-002 (VNC
Consistency, `Net = Brut − Amort`)** applies to NCT+SYSCOHADA but **not IFRS**
(IFRS bilans don't use the Brut/Amort/Net columnar presentation). The other 14
run identically for all three.

## What does NOT differ (the gap)

The canonical engine is single-dictionary and NCT-shaped:
- `nomenclature_data.yaml` is explicitly *"Nomenclature Data — NCT"* (account
  classes 1–7 = NCT plan). `load_default_dictionary()` takes **no standard
  argument**; `enrich_tables` calls it with none.
- So `fuzzy_match`, `classify_statement`, grouped-entry + subtotal detection, and
  every `validation_field`-anchored check run against the **NCT dictionary even
  for IFRS/SYSCOHADA**. Each of its 66 entries carries `validation_field` /
  `section` / `account_code` / `parent_subtotal` / `column_model` / `variations`
  — the structure that makes matching *and* validation work. A label with no
  entry lands `unrecognized`/`grouped`/`custom`, gets no `validation_field`, and
  cannot participate in any identity check.
- Number parsing, scale resolver, ensemble voting, and OCR engine are all
  standard-agnostic.

| Stage | Varies by standard? |
|---|---|
| OCR term correction (vocab) | **Yes** — 3 distinct word lists |
| FFFD `�` resolution | **Partly** — NCT only |
| Validation rules | **Barely** — only VNC (off for IFRS) |
| Canonical matching / classification / validation anchors | **No** — always NCT |
| Number parsing, scale resolver, ensemble, OCR | **No** |

## What a true multi-standard system would take (the "Phase 6" work)

1. **Code threading (easy):** `load_default_dictionary()` → `load_dictionary(standard)`;
   thread `standard` into `enrich_tables`; scope the matcher to that standard's
   entries. ~3–4 call sites. Removes the FFFD-NCT-only and single-VNC special cases.
2. **Two more canonical dictionaries (the real cost):** author IFRS (~50–80
   entries: right-of-use, contract assets, OCI, IFRS's current/non-current vs
   liquidity statement structure, single vs two-statement P&L) and SYSCOHADA
   (~60–80: HAO sections, système normal/allégé, OHADA classes **1–9** vs NCT's
   1–7, TAFIRE/CAFG). Each entry needs the full annotation set + reference docs +
   accounting expertise (the NCT one was built from OECT / Ministère des Finances).
3. **Per-standard column models + section hierarchies:** IFRS doesn't use
   `brut_amort_net` (gross/depreciation in notes) → `n_n1`; SYSCOHADA's tree
   differs (HAO split).
4. **Per-standard validation identities:** the rule engine is NCT-shaped; IFRS
   wants OCI / changes-in-equity links, SYSCOHADA the AO/HAO split.
5. **Statement-classification vocab:** `classify_statement` keys on NCT/French
   keywords; "Statement of Financial Position" / "Système Normal" need their own.

**Effort:** engineering is modest; the cost is the domain content (two expertly
annotated dictionaries + their column models, sections, and identities).
