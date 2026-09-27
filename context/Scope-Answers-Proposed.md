# Scope questions — proposed answers (for owner review)

> **Status 2026-09-11: PROPOSED, not decided.** Drafted by Claude (AI) for the owner to edit.
> Each answer is a recommendation; items marked **DEPARTURE** differ from the owner's own documents and
> need explicit sign-off. Numbers marked **PLACEHOLDER** are judgement, not evidence.
>
> How this was produced: first-pass reasoning → three independent evidence sweeps (Tunisian regulation and
> market; the sample corpus; the repo and deployment) → four adversarial critiques (owner fidelity, bank
> realism, engineering feasibility, line-by-line fact check: 106 claims checked, 18 corrected) → this version.
>
> Owner documents used:
> - **Spec** — Finexora "Description fonctionnelle MVP" v0.5, 5 pp. (`context/OG_Product Functional Specification - MVP FinAIze.pdf`)
> - **Deck** — "Pipeline de Traitement Automatisé des États Financiers" V.0.1, Feb 2026, author omar fehri (`context/Documentation/Pipeline workflow.pdf`)
> - **Control rules** — "Règles de Contrôle pour la Validation des États Financiers", creator omar.fehri@mnc.aero, 2025-12-02; 8 rules highlighted green (`context/Documentation/Règles de Contrôle….docx`)

---

## 0. Two blockers that sit above the ten questions

### 0.1 Can a bank send client financial statements to Finexora at all?

For a Tunisian bank, processing client EF through a vendor is **outsourcing**: BCT circ. 2006-01 requires
prior BCT authorisation for outsourcing client-data processing (and for any non-resident provider), requires
the provider to accept control at its premises, and **forbids the provider from sub-contracting**. Law 2004-63
(personal data) requires an INPDP declaration and authorisation for any transfer abroad; Law 2016-48 art. 81
also applies. A cloud SaaS on third-party infrastructure, or any call to an external AI service, collides
with the sub-contracting ban.

**Proposed:** the pilot runs **on-premise at the bank** (licensed software, no vendor access to data, no
external AI calls or telemetry). Cloud only after the bank obtains BCT authorisation and completes the INPDP
steps. Consequences: Q6's reference machine is the bank's hardware (probably CPU-only); Q5's "Retrain"
happens on-prem, with the bank's consent.

### 0.2 Non-public client documents are already in circulation

Five non-public EF of named companies (`EF CLB 2024`, `EF PROV. COHAPROM 2024`, `EF LA VIANDE BLANCHE 2024`
— a compilation report addressed "à l'attention de la banque" —, `Rapport_CAC_2024_signé`,
`Rapport CAC 2024 Aquaculture`) are tracked in git and pushed to the (private) GitHub repo, bind-mounted into
the review UI (which has no login), and have been read by external AI during development — the benchmark's
Opus reference oracle read EF CLB, and Claude sessions, including the one that produced this document, read
all five. The tracked
label-learning log (`data/variation_candidates.jsonl`) records which banks clients borrow from
(e.g. "Emprunt BH", "Emprunt ATTIJARI").

**Proposed:** the owner confirms where these five came from and that their use is authorised; decide whether
the git history must be purged; stop using external AI on non-public documents; keep learning logs out of git
and scope them per tenant.

---

## 1. Proposed guiding principle (for approval)

> **Automate as far as it has been measured to be safe; until measured, flag rather than decide.**

The owner's aim is speed ("accélérer l'analyse crédit") through automation; the business context is
reliability ("fiabilisation"). This principle resolves ambiguity toward flags and review, and it drove three
departures the owner may reverse together: Q5 (no auto-approve / auto-reject until calibrated),
Q7 (NCT content only), Q9 (some untagged channels deferred).

---

## 2. Where the owner's documents disagree with each other

| # | Topic | One document says | Another says | Proposed resolution |
|---|---|---|---|---|
| C1 | Cash-flow statement | Objective 1: "bilan et état de résultat" | Perimeter, §1.2, §1.3 "par défaut … flux de trésorerie"; deck locates "Bilan, CR et Flux"; green rule on TFT cash | Q8 |
| C2 | Completeness check | Spec §1.1 "vérification … de la complétude" (untagged) | Deck: "All required statements found? → Flag missing **TBD day2**" | Pull into MVP (Q8) |
| C3 | Human validation | Spec "validation manuelle possible"; deck "High confidence → Auto-approve" | Spec workflow "brouillon → validé → historisé" | Q5 |
| C4 | Flags | Deck steps "Arithmetic Validation" and "Cross-Checking" are MVP | Every deck "Flag" branch is "TBD day2" | Flags in MVP — a bank will not accept unflagged failures |
| C5 | What "(V2)" covers | Tag closes a sentence that also says "standardisées" (clearly MVP) | §6 central DB, §4 "Base centralisée" are untagged | Q9 — three readings |
| C6 | Accepted formats | PDF natif, PDF scanné, JPEG, PNG, ZIP; deck "Format Check KO → END" | The corpus holds 6 DOCX (team-made fixtures) | DOCX = proposed addition (Q4/Q9) |
| C7 | "10 états financiers en parallèle" | 10 uploaded documents? | NC 01 §18: each statement is "un état financier" → 10 statements ≈ 3 documents | Owner to say (Q6) |
| C8 | Report à nouveau (green rule) | RAN N = RAN N-1 + Résultat N-1 − dividendes | CSC art. 287 / 140: 5 % legal reserve is mandatory for SA/SARL | Correct the formula (§5) |
| C9 | Confidence score | Deck: per-step scores S1–S5, "weighted scoring model (TBD)" (an S0–S7 weighting appears only in the file's metadata) | Team built QCS + tiers; no recorded owner sign-off | Owner confirms or rejects the swap |

---

## 3. Summary

| # | Proposed answer | Departure? | Confidence | Cost |
|---|---|---|---|---|
| Q1 | Packs are a first-class input; return the primary statements (N and N-1) plus a reviewer-confirmed identity card | Adds metadata beyond "client, période" | High | L |
| Q2 | Notes: a visible, reviewer-changeable label, never discarded; three supporting tables extracted for validation only | — | Medium | M |
| Q3 | Funds, banks, insurers out of MVP content; detected as "unsupported" with a reason; consolidated NCT in | Leasing/MFI exclusion is a portfolio call | Medium-High | M |
| Q4 | Fix /Rotate 180/270 and EXIF; readability failures go to full review, never END | Deck's END deferred | High | M |
| Q5 | Owner's decision rule is the target; both auto-approve and auto-reject must be earned; four-eyes review | **DEPARTURE** | Medium | L |
| Q6 | Digital: ≤ 30 s per file, triage included; scanned: separate target after triage is prototyped | **DEPARTURE** (split target) | Medium | L |
| Q7 | NCT content only; other standards rejected clearly; plumbing made standard-agnostic | **DEPARTURE** (Objective 2) | Medium | S now, L later |
| Q8 | TFT found, typed and reviewed; TFT↔Bilan cash check redefined; value accuracy reported, not gating | Resolves C1 | Medium | M |
| Q9 | Upload + API + ZIP; client/dossier; validated snapshot; structured storage; working webhooks; BCT ratios | Several deferrals | Medium | L |
| Q10 | Treat prod as reachable; restrict it now; list of fixes before real data | — | High (policy) | S–M |

---

## 4. The answers

### Q1 — Audit-report packs

**Proposed.** Yes — a first-class input, and probably the normal one for audited borrowers. From any document
the system returns the reporting entity's **primary statements** (Bilan actif, Bilan passif, État de résultat,
TFT when present), each with its **N and N-1 columns labelled with their periods**, plus an **identity card**,
auto-suggested and confirmed by the reviewer:

- entity name, matricule fiscal / RNE identifier, bank client ID (entered by the bank)
- period start and end; annual or interim; individual or consolidated
- accounting standard and model (NCT reference / authorised)
- currency and unit/scale (DT, mDT, KDT …) — see §6
- nature: certified by a CAC / limited review / compilation / provisional / **prévisionnel (forecast)**
- auditor opinion type (sans réserve / avec réserves / défavorable / impossibilité): MVP = the opinion
  paragraph is shown and the reviewer picks from a list; réserves and going-concern paragraphs
  ("incertitude significative") are flagged; automatic extraction is Day 2.

The owner's minimum is "client, période"; the rest are **proposed additions** with a regulatory basis.
Forecasts share the layout of actual statements, so they are detected and kept out of the historical series.

**Why.**
- BCT circ. 91-24 art. 7: CAC-certified EF required from clients whose system-wide exposure exceeds 5 MD;
  art. 8: class-0 status needs certified documents < 18 months **and** provisional statements < 3 months →
  banks receive both packs and provisional sets.
- BCT circ. 2006-19 art. 24: every credit file holds the EF of the last three years (borrower, guarantor,
  group entities) plus forecasts. BCT circ. 2022-01 (non-performing loans) art. 7: "durant tout le cycle de
  vie de la créance", viability is assessed on certified EF of the last three years, with at minimum
  EBITDA / charges financières and Dettes / EBITDA, and on certified consolidated EF where the borrower
  belongs to a group ("le cas échéant").
- NC 01 §22: audited EF are presented with the auditor's opinion. Law 2018-52 art. 32 (RNE) files EF with the
  CAC reports (per a secondary legal database; not checked against the JORT).
- Corpus: 6 of 16 distinct files are audit or limited-review packs; 8 of 9 PDFs carry notes. In the 6 audit
  packs, primary statements are 21 of 194 pages (10.8 %). One pack (BRA) carries a qualified opinion.
- The spec's perimeter says "documents EF scannés ou **annexés**"; the deck classifies each page as Balance
  Sheet / Income Statement / CF Statement / Other content and "localise Bilan, CR et Flux dans le document".

**Cost and traps (L).** Needs a page classifier, which does not exist (§7). First find out why the primary
pages of Air Liquide, COHAPROM and Somocer produce no statement in the sweep — wrong page provenance or an
extraction defect; the two have very different fixes. Changing the output re-indexes `canonical_tables` and
breaks corrections keyed by table index.

### Q2 — Notes to the accounts

**Proposed.**
- Notes are **recognised and kept, never discarded**. Each page carries a visible label (statement / note /
  auditor report / cover / forecast) that the reviewer can see in a page strip and **change** — for example,
  promoting a "note" to a statement.
- Notes are excluded from the statement output and from statement validation, not from the reviewer's view.
- A required statement that was not found is a flag that blocks validation and lists candidate pages.
- **Option A (recommended):** extract three supporting tables, used for validation only and never output as
  statements. They are what four of the owner's eight green rules (§5) and the BCT EBITDA ratios need:
  1. charges by nature / "tableau de passage" (reference-model income statements),
  2. the cash and overdraft note (liquidités / concours bancaires),
  3. the equity-movement / result-appropriation table.
- **Option B:** skip those checks with the status "non vérifiable sans annexes" — faster to build, weaker.
- Also tag, without extracting, the off-balance-sheet / pledges note ("engagements hors bilan", "sûretés
  consenties") so the reviewer finds it.
- Day 2: targeted extraction of debt maturity schedules, fixed-asset movements, related parties.

**Why.** The spec targets bilan, CR and TFT "par défaut"; the deck puts everything else in "Other content".
But notes are legally part of the EF (NC 01 §18), and NC 01 §43 makes the **reference** income statement
classify charges **by function**, with the by-nature breakdown required in the notes (§45) — Air Liquide and
Ciments de Bizerte use it. The owner filed both "détail des charges/produits" rules under "Cohérence Compte de
Résultat – Annexes". Today notes masquerade as statements: in the latest sweep Ciments de Bizerte yields
62 "statements", 25 of them typed `bilan_actif`, for a document with one Bilan actif.

**Cost and traps (M).** The real cost is gap D4 (primary statement vs note). A misclassified primary
statement must never silently disappear — hence the visible, changeable label.

### Q3 — Investment funds and other non-corporate entities

**Proposed.** Entity type becomes a document attribute that selects the statement model, just as the
standard does (spec: "Modèle de données paramétrable selon le type d'entreprise ou le référentiel"). The MVP
ships the general company model (NC 01) **and consolidated NCT statements** (goodwill, intérêts minoritaires,
part du groupe — none of which the nomenclature has today), because BCT 2022-01 art. 7 draws on certified
consolidated EF when the borrower belongs to a group, and the corpus holds one (Somocer). Everything else is
detected and given a document-level **"unsupported" outcome with a reason**, never forced into the company
mapping:

| Type | Reason | Decision |
|---|---|---|
| Funds — OPCVM, SICAV, FCP, FCPR, FIS (NC 16-18) | Tunisian funds may not borrow; different statement model (actif net, état de variation de l'actif net, no TFT) | Out |
| Banks (NC 21-25), insurers (NC 26-31) | Not borrowers in this use case | Out |
| Leasing companies (file under NCT 21), microfinance (NC 32-34) | **They are bank borrowers** | Pilot bank decides from its portfolio |
| Simplified regime (NC 42) | Sole proprietors; no TFT | Out for MVP |

Ask the owner whether `fis_innov_invest` was placed in "Sample - OF" as a case expected to work.

**Cost and traps (M).** No entity-type detector exists: the quarantine gate is per table and its reasons are
confidence reasons only; there is no document-level "unsupported" state. Bank and insurer balance sheets share
vocabulary ("Total actif", "Capitaux propres") and could pass the gate today. Build a keyword detector over the
first pages (actif net, valeur liquidative, OPCVM / FCP / SICAV, dépôts de la clientèle, provisions
techniques). Use digital cmf.tn filings as fixtures (a SICAV, a bank, an insurer, FCPR TDF III), not
fis_innov, whose empty output has two confounded causes.

### Q4 — Rotated scans; phone captures

**Proposed.**
- **/Rotate 180/270: fix it.** It is proven: on fis_innov, docling-parse mis-reads those pages, so RapidOCR is
  never called and the document comes back empty. The fix de-rotates a temporary copy and handles pages whose
  crop box differs from their media box. Verify that the "Locate cell" overlay still lands correctly.
- **/Rotate 90** (La Viande Blanche p6-p9, Air Liquide p6, BIAT): not shown to cause any loss. The La Viande
  Blanche p6 loss traces to Tier 0 discarding sub-3-row table fragments and to its retry condition — fix that
  instead. Verify /Rotate end to end on fis_innov, La Viande Blanche and BIAT.
- **EXIF orientation: fix it** (proven lost on image upload; small).
- **Skew and unflagged pixel rotation:** measure on the scanned packs first; fix only what the measurement shows.
- **Phone captures:** scanner-app PDFs (CamScanner etc.) are scanned PDFs and supported. Camera images
  (JPEG/PNG) are accepted with EXIF honoured; HEIC is a proposed addition.
- **Readability (deck step 1, "Quality Score → KO → END") — DEPARTURE.** In the MVP an unreadable or
  low-quality document is flagged for full manual review with a re-scan request; it is never ended on an
  uncalibrated score. END becomes available once a pre-OCR quality metric is calibrated on real phone
  captures. An empty or near-empty extraction must always be a loud failure, never a silent success.

**Why.** "PDF scanné" is accepted unconditionally. The only phone capture (BRA) has the lowest QCS (0.497),
yet its three primary statements were found — ending it would have lost a usable document.

**Cost and traps (M).** END needs a new terminal state with no retries (Celery retries exceptions three times
today). Any orientation or skew correction moves Docling's boxes into the corrected frame, so the review
overlay must use the same frame.

### Q5 — What "done" means; when human review is mandatory

**Proposed — DEPARTURE.** The owner's decision rule is the target, verbatim: *"Low confidence → Reject /
Retrain; Medium confidence → Human review; High confidence → Auto-approve"*. **Both ends are earned.** In the
MVP no document is auto-approved or auto-rejected on an uncalibrated score:
- **High** = fast-track review: pre-filled, attention directed to flagged cells.
- **Low** = full manual review, with a re-scan suggestion when readability is the cause.

A document class (PDF natif, PDF scanné, image/phone capture; screenshot-DOCX counts as image) may
auto-approve only when all of these hold:
1. its measured silent-error rate is at or below the owner's threshold, over enough cells (proving ≤ 0.5 %
   with zero observed errors needs about 600 green key-field cells per class);
2. all eight green controls (§5) ran and passed — skipped is not passed;
3. unit, period, entity, nature and opinion were confirmed;
4. the bank's risk / internal-control function co-signs the thresholds.

**"Retrain" in the MVP:** reviewer corrections are captured as labelled data (a corrections export and an
improvement loop already exist), kept on-prem per bank, with no promotion across tenants without
anonymisation. Learned label variations are promotable into the shared nomenclature today — a cross-bank
leakage path to close.

**Preconditions (small).**
- **One definition of High / Medium / Low.** Today Tier 1 auto-accepts at QCS 0.75 while the QCS module says
  review below 0.90, and the worker drops the module's `needs_hitl`. So four DOCX skipped review at QCS
  0.776–0.795; the June benchmark had scored them 1.00, 0.99, 0.82 and 0.76. The highest-QCS file scored lowest,
  so QCS does not separate them (different code versions; accuracy measured against an Opus reference).
- **Four-eyes review.** A personal login for every review action (today the UI signs in as the bootstrap
  superuser). The preparer who corrects key fields is not the validator. API keys cannot validate. The audit log
  names the person and what changed (BCT 2006-19 art. 8: separation of functions).

**"Done" for the MVP** — measured per document, per input class, on a human-labelled, bank-representative set:

| Metric | PLACEHOLDER target |
|---|---|
| Documents with every key field correct (~25 fields, EBITDA inputs included) | ≥ 90 % PDF natif · ≥ 75 % PDF scanné |
| Key-field accuracy | ≥ 99.5 % PDF natif · ≥ 98.5 % PDF scanné |
| Scale (DT vs mDT), period, entity, N/N-1 column-swap errors | **0** — these still balance, so arithmetic cannot catch them |
| Primary statements found and typed; notes returned as statements; forecasts returned as actuals | 100 % · 0 · 0 |
| Silent-error rate on green cells | measured by audit sampling: blind re-keying of green key fields on a fixed share of fast-track documents (a one-click confirm cannot reveal them) |
| Review effort | minutes per document vs manual keying (needs a review-start event) |

The PoC plan's "≥ 70 % automation, < 2 % critical errors" is **replaced**, not restated — the metric,
threshold and meaning of "automation" all change; the owner should say which one stands.

**Cost and traps (L).** None of these metrics is measurable today. The benchmark matches 18-character raw
labels against values pooled from all columns (so a swapped year passes), ignores flags, has 8 cases and
532 values with no audit pack and no TFT, and its reference is produced by an AI, not by people. It needs
canonical-ID + period scoring, flag awareness, page-class labels, and a double-keyed human set — which in
turn needs real bank documents, hence §0.

### Q6 — "< 30 s par document"

**Proposed — DEPARTURE (split target).**
- **Digital documents:** ≤ 30 s per uploaded file, triage included, p95, measured on the target hardware
  (the bank's server if §0.1 holds).
- **Scanned documents:** a separate target, set once page triage has been prototyped and measured.
- The spec's **load test** (§7.2 "Tests de charge planifiés") and **latency / throughput / failure-rate
  instrumentation** (§4 Monitoring) become MVP acceptance items.
- Owner to settle **C7**: "10 états financiers en parallèle" = 10 files or 10 statements.

**Why.** Measured: 5.9 s/page digital, about 11.7–12 s/page scanned, 13.6 s/image, on unrecorded hardware;
the repo's in-container benchmark measured 19.7 s/page OCR plus a 74.5 s model load. If only the primary pages
were processed and nothing else cost time, 7 of 8 multi-page packs would fall under 30 s. Counting even
1 s/page to triage the other pages, only 1 of 8 does — the out-of-scope fund, and only because OCR read
almost nothing on it. For scanned
pages no sub-second classifier exists: classification runs after OCR. Runs of the same file vary up to 2×.

**Cost and traps (L).**
- The worker runs one document at a time and reloads its models every 20 tasks (74.5 s cold start), which lands
  on the p95 line.
- Ten parallel workers need about 28 GB of RAM.
- The worker image is CPU-only (a GPU needs a different image and a device reservation).
- Tier 0 decides digital vs scanned for the whole file from page 1, so mixed packs are misrouted.
- No load-test harness exists.

### Q7 — NCT-only MVP

**Proposed — DEPARTURE from Objective 2** (untagged, priority 2). MVP content is NCT/SCE only. What ships of
Objective 2:
- **(S)** the UI, API and configuration offer NCT only and reject other values with a clear message. Today they
  offer IFRS and SYSCOHADA, silently run the NCT engine on them, and the API accepts any string.
- **(M)** the plumbing becomes standard-agnostic: nomenclature, profiles and rules are selected by the standard.
  Today `load_default_dictionary()` takes no standard argument (3–4 call sites).
- **(optional)** per-bank editable mapping tables and rule toggles — the owner's "Module de Règles Configurables"
  (rule_id, formula, tolerance, severity, active).

IFRS and SYSCOHADA documents are detected and marked unsupported. The next standard is chosen by the first
non-Tunisian customer — Finexora's target market has no public trace, so the owner must say. Correct the README,
which claims all three standards are supported.

**Why.** All 9 corpus PDFs are NCT/SCE. The CNC decided IFRS for the consolidated statements of public-interest
entities (from 2021, postponed to 2023), yet FY2024/FY2025 consolidated EF measured are still NCT: one listed
industrial group (STIP) and two banks. IFAC (2026) rates Tunisia "partially adopted". Tunisia is not an OHADA
member, so SYSCOHADA matters only for borrowers in the 17 OHADA states — some Tunisian bank groups have that
exposure.

### Q8 — Cash-flow statement

**Proposed.** Resolves C1: the TFT is **located, typed, extracted and reviewed** whenever present.
- **In the MVP gate:** TFT detection and typing (every TFT present is found and typed).
- **Reported, not gating:** TFT value accuracy, because the corpus holds only 5 TFTs.
- **Green TFT↔Bilan check, redefined correctly:** closing cash in the TFT = bilan liquidités − bank
  overdrafts (NC 01 §67 counts overdrafts in cash). Where the bilan line mixes overdrafts with loan maturities,
  use the cash note (Q2 option A); otherwise "non vérifiable". It stays below CRITICAL until validated on the
  5 corpus TFTs.
- **"TFT absent"** is a completeness flag. The spec's completeness check is untagged, while the deck marks
  "Flag missing" TBD day2 — **proposed: pull it into the MVP** (sign-off).
- "An auditor's opinion cites a TFT that is missing" is Day 2: it needs OCR of the report pages.

**Why.** The TFT is mandatory for every commercial company under the SCE (exceptions: NC 42 simplified regime;
NC 16 funds, SICAVs included), in a direct or indirect model. Present in 5 of 16 files; in 2 audit packs the
auditor cites a TFT absent from the file.

**Defects, not scope.**
- The two TFTs whose page provenance is trustworthy (Ciments de Bizerte p5, La Viande Blanche p9) were both typed
  `bilan`; the other three cannot be judged until Tier-3 provenance is fixed.
- The active CRITICAL rule V2-002 matches keywords that miss the corpus label "Trésorerie à la clôture de
  l'exercice" and compares gross liquidités, so today it skips — or would raise a false critical. Fix TFT typing
  before relying on it.

### Q9 — Intake, exports, integrations, and "(V2)"

**One criterion for every untagged spec item:** it is MVP if a bank pilot cannot work end to end without it,
or if it is cheap; anything else is a **proposed deferral** needing sign-off. The only owner-sanctioned
deferrals are the "(V2)" sentence and the deck's "day2" boxes.

**Proposed MVP.**
- **Intake:** web upload and REST API (exist) + ZIP (spec lists it twice). ZIP gets its own size limit (the cap
  is 25 MB and one 24-page phone scan is 11.2 MB), zip-bomb and path-traversal guards, and members grouped into one
  dossier. The format check follows the owner's list; DOCX and HEIC are proposed additions.
- **Client and dossier:** a Client keyed by matricule fiscal / RNE, and a credit-file Dossier grouping borrower,
  guarantor and group entities. A per-client list of documents and periods, with the period on each output
  column. Duplicate-upload detection by content hash (the corpus holds a byte-identical duplicate).
- **Structured storage (Objective 1):** validated values in a normalised relational schema (entity, period,
  statement, line item, value, unit, source page and box). Today each result is one JSON blob on the job row.
- **Validated snapshot:** a versioned result with corrections applied. Today corrections are stored but never
  applied, the result endpoint returns raw output in any state, and the API correction schema lacks `statement_id`.
- **API output:** extracted data with its workflow status (brouillon / validé), per-field confidence and flags;
  "validated only" is a bank-configurable policy. Exports: JSON (API), CSV and Excel (today UI-only — expose them),
  XML if the pilot bank needs it.
- **Webhooks:** today configured but never sent. MVP: `document.processed` (no figures) and `document.validated`
  (link to the data) — the owner picks. Secrets must be stored reversibly (today a one-way hash that cannot sign),
  so existing partners get new secrets.
- **Review UI:** a queue and document list (today the UI cannot review documents submitted through the API),
  an import/status dashboard, version history, the "historisé" transition.
- **Objective 5:** inconsistency alerts (exist as validation). Ratios on validated data only — first the BCT
  2022-01 minimum (EBITDA, EBITDA / charges financières, Dettes / EBITDA; EBITDA needs depreciation, which a
  reference-model CR shows only in the notes), then the owner's list (FR, BFR, trésorerie nette, endettement,
  autonomie financière, solvabilité, marge, ROE, ROA).
- **Auth:** "OAuth2 **ou** SSO" is met by OAuth2 bearer tokens for people (exist) plus API keys or client
  credentials for systems.

**Proposed deferrals (sign-off):** e-mail intake; shared-folder batch import; SSO/OIDC with the bank's IdP (at
pilot time); dashboards and periodic reports; BI/ERP/CRM connectors; the consolidated central repository and
"Vision 360°"; cross-document **reconciliation** of overlapping years — when year N's N-1 column differs from
year N-1's N column it is a restatement, a credit red flag to show, never a duplicate to delete (proposed
reading of "élimination des doublons"); a BCT Centrale des bilans export (ask the pilot bank for its format).
QuickBooks: propose dropping.

**What "(V2)" defers — three readings, owner to choose:** (a) the whole introduction paragraph; (b) its last
sentence; (c) only the downstream uses "(reporting, scoring, analyses)". Recommended: **(c)** — Objective 1 needs
structured storage and Objective 2 needs standardisation, so neither can be V2.

**Option to raise with the pilot bank:** e-filing companies submit bilan, CR and TFT as schema-validated XML in
the liasse fiscale (F6001–F6004; the notes, F6006, have no published XSD). It is not certified, so it cannot
replace certified EF, but it could give exact figures without OCR for provisional sets — recorded with source
"liasse fiscale (non certifiée)" and reconciled against the certified EF.

**Naming:** the project also calls a validation tier "V2". Write "version 2" or "Day 2" for deferrals, and mark
which deferrals are the owner's and which are proposed.

### Q10 — Who can reach "prod"

**Owner fact.** From the repo alone, "prod" is either Docker Desktop on the developer's machine or a remote
host — the developer has said prod is not the local run, and a cloud SSH key sits in the repo root, but nothing
maps it to a host.
The only stack defined is the development compose file:
- ports published on all interfaces: 8000 (API via Traefik), 8080 (Traefik dashboard, unauthenticated, with the
  Docker socket mounted), 8501 (review UI, no login, signs in as the bootstrap superuser), 5555 (Flower, no
  auth), 15672 (RabbitMQ UI, default credentials), 9001 (MinIO console, default credentials — MinIO holds every
  uploaded statement);
- no TLS; default secrets throughout; Swagger and `/metrics` public;
- anyone can self-register as admin of any organisation, and a user with no organisation sees every
  organisation's documents;
- `./Samples` (client documents, §0.2) is mounted into the UI with a sample picker.

**Proposed policy: treat it as reachable.**
1. No change needed: restrict inbound traffic at the cloud provider's firewall to your own IP (Docker-published
   ports bypass ufw).
2. No cost: remove the `./Samples` mount on any reachable host.
3. `.env`: `FINALZE_SECRET_KEY`, admin password.
4. Compose edit or override file: Postgres, RabbitMQ and MinIO credentials — literal values in
   `docker-compose.yml`, so `.env` cannot change them — moved to `${…}` or Docker secrets; bind ports to
   127.0.0.1.
5. Code: registration and tenant-filter fix; per-user login in the review UI.

Before any real client data: the above, plus TLS, encryption at rest (spec §7.1 "AES-256" — MinIO SSE or
encrypted volumes), a retention and deletion policy, a named-user audit log, and the §0.1 decision.
Owner to confirm: the host, its address or domain, firewall rules, whether an `.env` overrides the defaults,
and whether real client documents are stored there.
Housekeeping: add `*.pem` to `.dockerignore` — the key is sent in the build context, but no Dockerfile copies
it into an image.

---

## 5. The owner's eight green rules — proposed MVP status

The deck makes arithmetic and cross-checking MVP steps; only contextual validation is Day 2.

| # | Rule (owner's wording) | Needs | Today | Proposed |
|---|---|---|---|---|
| 1 | Résultat net (bilan) = résultat net (CR) | Bilan passif + CR | V2-001, CRITICAL | MVP |
| 2 | Report à nouveau N = RAN N-1 + Résultat N-1 − dividendes | N-1 column; dividends and **reserve allocations** (notes) | V2-003, WARNING, bounded approximation | MVP with the formula corrected to "− affectations aux réserves" (legal reserve, CSC art. 287); "non vérifiable" when appropriation is unknown. **Owner confirms formula** |
| 3 | Trésorerie de clôture TFT = trésorerie au bilan | TFT, bilan, cash/overdraft note | V2-002, CRITICAL, but skips on real labels | MVP, redefined (liquidités − découverts); below CRITICAL until validated |
| 4 | Détail des charges = total charges du CR | CR (authorised model) or by-nature note (reference model) | Not implemented | MVP: on the face for the authorised model; via the note for the reference model (Q2-A) |
| 5 | Détail des produits = total produits | Same as 4 | Not implemented | Same as 4 |
| 6 | Total actif = total passif | Bilan | V1-001, CRITICAL | MVP |
| 7 | Actif = passif + capitaux propres | Bilan | V1-001 | MVP |
| 8 | VNC = brut − amortissements − provisions | Bilan actif (brut / amort / net) | V1-002, ERROR | MVP |

---

## 6. Owner requirements that none of Q1–Q10 covers

| Item (source) | Proposed |
|---|---|
| Unit / currency normalisation (spec §3.4; NC 01 §19(c) requires the unit on every page) — the known 1000× gap D1 | **MVP.** Unit bound per statement, document default propagated, disagreements flagged, reviewer confirms, carried in every export. No silent rescaling. Currency recorded; conversion Day 2 |
| Language detection (spec §3.2) | MVP: French only; Arabic or English documents detected and marked unsupported |
| Missing values, date standardisation (spec §3.4, §6) | MVP: explicit null vs zero; ISO period dates in the identity card |
| Status dashboard, version history, search (spec §2.4) | Dashboard and history MVP (Q9); search a proposed deferral |
| Configurable rules module (control-rules doc, R001 JSON) | Rules already live in `validation_rules.json`; expose tolerance, severity and active per bank (proposed) |
| Ambiguous mapping → flag (deck step 3, TBD day2) | Already flagged yellow; keep |
| Learning loop ("bibliothèque de templates apprenante"; deck "Retrain") | Q5: corrections captured on-prem, per tenant |
| Monitoring (spec §4: failure rate, latency, throughput) | MVP, with the Q6 load test |
| Confidence interval storage (deck step 6) | QCS gives a point score; store a calibrated band once calibration exists (Day 2) |
| Encryption at rest, retention, anonymisation (spec §7.1, §4) | Encryption and retention MVP (Q10); anonymisation Day 2 unless needed for development use of data |
| Freshness (BCT 91-24 art. 8: certified < 18 months, provisional < 3 months) | Optional MVP flag with configurable limits (BCT circ. 2025-08 announces new classification rules) |

---

## 7. Dependency order and effort

As written, the MVP is **L–XL**. Suggested order:

1. **Decisions:** §0.1 deployment and data governance, §1 principle, §2 conflicts.
2. **Security and hygiene (S–M):** registration and tenant fix, per-user UI login, compose secrets and ports,
   Samples mount, NCT-only selector, one High/Medium/Low definition, /Rotate 180/270 + EXIF fixes.
3. **Page classifier with per-page digital/scanned routing (L — the critical path).** First diagnose the
   missing primary pages of Air Liquide, COHAPROM and Somocer.
4. **Primary vs note, TFT typing, completeness flag, the three supporting tables (M).**
5. **Identity card incl. unit and opinion (M); validated snapshot, client/dossier, webhooks, exports, review
   queue (L).**
6. **Labelled bank-representative set and measurement harness (L)** — needs real documents, so needs 1–2.
7. **Calibration**, then earned auto-approve per document class.

---

## 8. Sources

- Owner: spec v0.5 (pp. 2–5); deck V.0.1 (pp. 2–3); control-rules docx (green highlights).
- BCT: circ. 91-24 art. 6–8 (bct.gov.tn Cir_91_24_M_fr.pdf); circ. 2006-19 art. 8, 24–25; circ. 2006-01
  art. 3, 5–6 (outsourcing); circ. 2021-05 art. 71; circ. 2022-01 art. 7; circ. 2025-08 (secondary sources);
  circ. 2025-17 (visas); note 2023-29 (IBRD SME line).
- Law: 2004-63 (personal data), 2016-48 art. 61 and 81 (banks), 2018-52 art. 32 and 49 (RNE; secondary source),
  96-112; Code des sociétés commerciales art. 13 bis/quater, 140, 287; décret 2006-1546 art. 5.
- Standards: NC 01 §18–22, §43–56, §67; NC 16; NCT 42; CNC 2018 IFRS decision and 2021 postponement;
  IFAC Tunisia profile (May 2026).
- Filings measured: STB FY2025, BIAT FY2024, STIP FY2024, FCPR TDF III FY2024, ATL H1-2024 (cmf.tn / tustex.com).
- Corpus: the 17 samples (16 distinct); full-pipeline sweep of 2026-08-14.
- Evidence and critique records: workflow runs wf_60ce2646-d09 (evidence) and wf_0258a168-b13 (critique),
  kept in the session transcript directory.
