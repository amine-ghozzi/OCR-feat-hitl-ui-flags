---
title: FinAIze — Decision Form
type: decision-form
applies-to: "[[Scope-Answers-Proposed]]"
related: ["[[Master-Plan]]", "[[Project-Backlog]]", "[[Known-Gaps-Unit-Scale-and-Continuation-Threshold]]"]
date: 2026-09-14
status: open
decisions-total: 29
decisions-resolved: 0
tags: [decision, scope, finaize]
---

# FinAIze — Decision Form

> [!abstract] How to use this form
> Each block below is one decision the 2026-09-11 review surfaced that I should **not** resolve unilaterally (it needs a
> product, governance or engineering call). For each:
> 1. Read the **Finding** and **Options**.
> 2. Tick the chosen option's checkbox (`- [ ]` → `- [x]`), or write in **Other / feedback**.
> 3. Fill the **Decision / Owner / Date** line.
> 4. Flip `status: open` → `resolved` in the block's callout title.
>
> When all are decided, tell me and I'll start [[Master-Plan]] phase 1 in the order the plan gives. Items already settled
> with one clear direction are in the **Confirm** section at the end — untick to override.
>
> **Basis tags** on every claim: `[O …]` owner document (+ date) · `[R …]` regulation / standard · `[M …]` measured at HEAD
> `d42309c` (file:line, sweep, probe) · `[T …]` team-authored doc, **unconfirmed** · `[P …]` my proposal. A block whose finding
> rests on **T alone** says "unconfirmed basis" in its title.

## Document status — confirm before deciding

> [!warning]+ Owner documents · are they still current?
> Quotes in this form come from three owner-authored documents. Each is treated as the owner's position **as of its date**.
> - [ ] **Spec** — "Description fonctionnelle MVP" v0.5, updated 2025-11-09 (`OG_Product Functional Specification - MVP FinAIze.pdf`) · still current — or superseded by: ____
> - [ ] **Deck** — "Pipeline de Traitement Automatisé des États Financiers" V.0.1, 2026-02 (`Documentation/Pipeline workflow.pdf`) · still current — or superseded by: ____
> - [x] **Control rules** — "Règles de Contrôle pour la Validation des États Financiers", 2025-12-02, 8 rules highlighted green (`Documentation/Règles de Contrôle….docx`) · still current — or superseded by: ____
> - [ ] Is there any **other** owner document (e.g. one carrying an S0–S7 scoring weight) I have not seen? ____

> [!warning]+ Team documents · current / stale / retire
> These were written by the team, not the owner. Ruling 2026-09-14: **suggestions unless confirmed**; nothing in the plan is
> justified by them alone. Mark each; anything marked stale or retire is removed from Master-Plan citations at the next revision.
>
> | Document | Date / status in file | current | stale | retire |
> |---|---|---|---|---|
> | `Vision-and-Architecture-Directions.md` | "living architecture doc", 2026-05-30 | [ ] | [ ] | [ ] |
> | `Decision-Support-FinAlze-Vision.md` | "advisory", 2026-05-30 | [ ] | [ ] | [ ] |
> | `PoC-Experimentation-Plan.md` | "Phase 0 In Progress" | [ ] | [ ] | [ ] |
> | `Scoring-Approach-Analysis.md` | "Decision reached", March 2026 | [ ] | [ ] | [ ] |
> | `Skill-Suite-Roadmap.md` | undated | [ ] | [ ] | [ ] |
> | `OCR-Engine-Benchmark-Matrix.md` | June 2026 measurements | [ ] | [ ] | [ ] |
> | `Research-*.md` (4 files), `resource0.md`, `resource1.md` | undated research syntheses | [ ] | [ ] | [ ] |
> | `Accounting-Standards-Handling.md` | code-verified 2026-09-14 (`nomenclature.py:914`) | [ ] | [ ] | [ ] |

## Decision log

| ID | Title | Severity | Basis | My recommendation | Decision |
|---|---|---|---|---|---|
| G-1 | Pilot deployment model | 🔴 Critical | R+P | A (on-prem at the bank) | ⬜ |
| G-2 | The five non-public client EF in git | 🔴 Critical | M+R | B (authorised → purge + untrack, stop external AI) | ⬜ |
| G-3 | Who can reach "prod"; firewall now; rebuild timing | 🔴 Critical | M | A (firewall to own IP now; rebuild after P2) | ⬜ |
| G-4 | Guiding principle: automate as far as measured safe | 🟠 High | P | A (approve) | ⬜ |
| NEW-1 | Gate order | 🟠 High | M+P | A (regression gate first; accuracy gate before automation) | ⬜ |
| Q5 | What "done" means; auto-approve and auto-reject | 🔴 Critical | O+M+P | A (both ends earned per class) | ⬜ |
| Q5-FE | Named users and four-eyes review | 🟠 High | M+R | A (all three) | ⬜ |
| Q5-RT | What "Retrain" means | 🟡 Medium | O+M | A (per-tenant corrections, on-prem) | ⬜ |
| Q1 | Audit-report packs and the identity card | 🟠 High | R+M+O | A | ⬜ |
| Q6 | The "< 30 s" target | 🟠 High | O+M | A (split target; C7 = files) | ⬜ |
| Q2 | Notes to the accounts | 🟠 High | R+O+M | A (three supporting tables for validation) | ⬜ |
| Q8 | Cash-flow statement | 🟠 High | O+R+M | A | ⬜ |
| Q3 | Entity types in scope | 🟡 Medium | R+M | A (NC 01 + consolidated in; rest unsupported) | ⬜ |
| Q4 | Rotated scans, phone captures, "END" | 🟠 High | M+O | A (helper now; END after calibration) | ⬜ |
| Q7 | NCT-only MVP | 🟡 Medium | O+M | A | ⬜ |
| Q9-A | What "(V2)" defers | 🟡 Medium | O | C (only "reporting, scoring, analyses") | ⬜ |
| Q9-B | Intake and integration deferrals | 🟡 Medium | O+M | A | ⬜ |
| Q9-C | Pilot delivery surface | 🟠 High | M+O | A | ⬜ |
| E-8 | Webhook events and secrets | 🟡 Medium | M+O | A (both events; re-issue secrets) | ⬜ |
| C6 | Accepted formats: DOCX, HEIC | 🟡 Medium | O+M | A (DOCX + HEIC as additions) | ⬜ |
| E-10 | Labelled ground-truth set | 🟠 High | P+M | A | ⬜ |
| E-2 | Unit / scale (D1) | 🟠 High | P+R | A (record + flag, never rescale) | ⬜ |
| E-4 | Tier 0 for digital PDFs | 🟡 Medium | M | A | ⬜ |
| E-5 | Continuation grouping (D2) | 🟡 Medium | M | A | ⬜ |
| C8 | Report-à-nouveau formula | 🟡 Medium | O+R | A (corrected formula) | ⬜ |
| C9 | QCS vs the deck's scores | 🟡 Medium | O+M | A (confirm QCS) | ⬜ |
| NEW-2 | Objective 5 in the MVP? | 🟡 Medium | O+R | A (BCT minimum ratios only) | ⬜ |
| NEW-3 | Retention, deletion, anonymisation | 🟠 High | O+R | A | ⬜ |
| NEW-4 | Uncovered owner items, as a set | 🟡 Medium | O+M | A (all) | ⬜ |

---

## G-1 — Pilot deployment model

> [!question]+ G-1 · status: open
> **Finding.** For a Tunisian bank, processing client EF through a vendor is **outsourcing**: BCT circ. 2006-01 requires prior BCT
> authorisation for outsourcing client-data processing (art. 3), requires the provider to accept control at its premises and
> **forbids sub-contracting** (art. 5-6) `[R]`; Law 2004-63 requires an INPDP declaration and authorisation for any transfer abroad;
> Law 2016-48 art. 81 also applies `[R]`. A cloud SaaS on third-party infrastructure, or any call to an external AI service, collides
> with the sub-contracting ban. The reference hardware for the latency target follows from this choice `[P Scope-Answers §0.1]`.
>
> **Options**
> - [ ] **A. On-premise at the bank** — licensed software, no vendor access to data, no external AI calls or telemetry; the bank's
>   (probably CPU-only) server is the reference machine. *(Recommended.)*
> - [ ] **B. Cloud, after authorisation** — obtain BCT authorisation + complete the INPDP steps first ([[Project-Backlog]] B-19), with
>   the 2006-01 art. 5-6 contract clauses; pilot waits for it.
> - [ ] **C. Undecided — pilot on synthetic / public documents only** until the model is chosen.
> - [x] **Other / feedback:** A dedicated On premise server (config to be shared)
>
> > [!tip] Recommendation: **A**. It is the only model that needs no regulatory step before a pilot, and it removes the external-AI
> > question (G-2) at the root. B is a legal project, not an engineering one.
>
> **Impacts:** GOV-6, GOV-7, MEA-3, MEA-9, OPS-5 · Master-Plan P6 (real data), P5b (hardware) · Backlog B-19, B-20.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## G-2 — The five non-public client EF tracked in git

> [!question]+ G-2 · status: open
> **Finding.** Five non-public statements of named companies are tracked in git and on the private GitHub repo (`EF CLB 2024`,
> `EF PROV. COHAPROM 2024`, `EF LA VIANDE BLANCHE 2024` — a compilation report addressed "à l'attention de la banque" —,
> `Rapport_CAC_2024_signé`, `Rapport CAC 2024 Aquaculture`; init commit `e184e02`) `[M]`. They are bind-mounted into the
> review UI, which has no login (`docker-compose.yml:109`, `hitl/app.py:2332-2337`) `[M]`. They have been read by external AI:
> the benchmark's Opus reference oracle read EF CLB (`tools/extraction_benchmark/reference.py:29-31`) and Claude sessions —
> including the one that produced this form — read all five `[M]`. The tracked learning log records which banks clients borrow
> from ("Emprunt BH / ATTIJARI / STB") `[M data/variation_candidates.jsonl]`.
>
> **Options**
> - [x] **A. Authorised for this use, including GitHub hosting** — keep them; stop external-AI reading now; log redirected per tenant.
> - [ ] **B. Authorised for local use only** — purge them from git history, untrack, keep local copies outside the repo; stop
>   external-AI reading now; golden corpus split into public / non-public (sessions see pass/fail + counts only for the latter).
>   *(Recommended.)*
> - [ ] **C. Not authorised** — remove them entirely; the corpus becomes public filings + team fixtures; the labelled set (E-10)
>   is built only from pilot-bank documents on-prem.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **B**. Whatever the authorisation says, GitHub is a third party (G-1 logic), and the purge is cheap now
> > and expensive after the first external contributor. Confirm the provenance of each of the five in the Decision line.
>
> **Impacts:** GOV-1, GOV-3, GOV-4, MEA-1 (corpus split), MEA-2, MEA-3 · Master-Plan P1, P6.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## G-3 — Who can reach "prod"; lock it down now; when to rebuild

> [!question]+ G-3 · status: open
> **Finding.** The only stack defined is the dev compose file. It publishes six ports on all interfaces — 8000 (API), 8080
> (Traefik dashboard, `--api.insecure=true`, Docker socket mounted), 8501 (review UI, no login, signs in as the bootstrap
> superuser), 5555 (Flower, no auth), 15672 (RabbitMQ, guest/guest), 9001 (MinIO console, minioadmin — MinIO holds every uploaded
> statement) `[M docker-compose.yml:37,45,86-88,107,118-125]`; no TLS; Postgres/RabbitMQ/MinIO credentials are **literal** in the
> file, so `.env` cannot change them `[M]`; anyone can self-register as admin of any organisation (`schemas.py:23-25`,
> `users.py:88`, probe → 201) and an org-less user sees every organisation's documents (`documents.py:71, :136`) `[M]`. Prod is
> "not the local run" (your statement) and a cloud SSH key sits in the repo root, but nothing maps it to a host `[M]`. Docker-published
> ports bypass `ufw`. Prod also runs code older than `57bacb9` `[M]`.
>
> **Options**
> - [x] **A. Treat as reachable: firewall now, rebuild after P2** — today: restrict inbound at the cloud provider's firewall to your
>   own IP; remove the `./Samples` mount; set `FINALZE_SECRET_KEY` + admin password via `.env`. Code + compose fixes (SEC-1/2/8/9/10)
>   land in P1; one `docker compose up --build` with the Alembic step at the end of P2. *(Recommended.)*
> - [ ] **B. Reachable by others and holds data: take it offline now** until P1 ships, then interim rebuild at the end of P1.
> - [ ] **C. Local-only (Docker Desktop on your machine, LAN blocked)** — no emergency step; P1 as scheduled.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**, unless anyone else can reach it or real client documents are stored there — then **B**.
> > Please fill in: host / address or domain: ____ · firewall rules: ____ · `.env` present? ____ · real client documents stored? ____
>
> **Impacts:** SEC-1/2/7/8/9/10, GOV-2, OPS-10, OPS-11 (rebuild timing = E-7) · Master-Plan P1, end of P2.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## G-4 — Guiding principle

> [!question]+ G-4 · status: open
> **Finding.** The owner's aim is speed through automation ("accélérer l'analyse crédit") `[O spec p.2 2025-11-09]`; the business
> context is reliability ("fiabilisation de l'entrée des EF client") `[O spec p.2]`. When the documents are ambiguous, something has
> to break the tie. My proposal `[P Scope-Answers §1]`: **"Automate as far as it has been measured to be safe; until measured, flag
> rather than decide."** It drove three answers that depart from the owner's documents — Q5 (no auto-approve / auto-reject until
> calibrated), Q7 (NCT content only), Q9-B (some untagged channels deferred) — so approving or reversing it moves all three together.
>
> **Options**
> - [x] **A. Approve** the principle; Q5/Q7/Q9-B are read in its light. *(Recommended.)*
> - [ ] **B. Reverse:** prefer automation and breadth where the documents allow it; Q5 keeps the deck's auto-approve at High from
>   day one, Q7 ships best-effort IFRS/SYSCOHADA, Q9-B keeps every listed channel in the MVP.
> - [ ] **C. Decide the three answers individually** (no standing principle).
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. A wrong figure that looks right is worse than a missing one in a credit file; the principle is also
> > reversible per class once measurement exists (P7).
>
> **Impacts:** Q5, Q6, Q7, Q9-B; RTE-5, RTE-8 · Master-Plan P2.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## NEW-1 — Gate order (sequencing choice)

> [!question]+ NEW-1 · status: open
> **Finding.** No accuracy has been measured at HEAD: zero human-labelled documents; the benchmark scores 8 small cases against an
> AI-written reference, matches 18-character raw labels against values pooled from all columns (a swapped year passes), and has no
> audit pack, scanned PDF or TFT `[M tools/extraction_benchmark/metrics.py:73-74, :124-138; BENCHMARK_REPORT.md:18]`. A human-labelled
> gate needs real documents (G-1/G-2, P5a security) and ≈ 10 days of double-keying. The team's Vision doc suggested "measure accuracy
> first, gate everything on it" `[T Vision-and-Architecture-Directions §2, 2026-05-30 — unconfirmed]`.
>
> **Options**
> - [x] **A. Two gates** — a golden **regression** snapshot at HEAD over the 16-file corpus + a `corpus_facts` list built only from facts
>   the review established (P1, ≈ 0.5 d) gates the extraction fixes of P2–P4 (each change whitelisted one at a time); the human
>   **accuracy** gate (P6) is required before any automation widening (P7). *(Recommended.)*
> - [ ] **B. Accuracy gate first** — no extraction or routing code changes until the labelled set exists (weeks on the owner's calendar);
>   only security (P1) proceeds.
> - [ ] **C. No formal gate** — rely on the 515 unit tests and the L1 gate.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. The regression gate proves each fix changed only what it intended; nothing that widens automation
> > ships before the accuracy gate. C cannot say which change caused which diff; B stalls known, observable defects.
>
> **Impacts:** MEA-1, start of P2–P4, MEA-2…7 · Master-Plan §4.0.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q5 — What "done" means; when auto-approve and auto-reject are allowed

> [!question]+ Q5 · status: open
> **Finding.** The spec sets no accuracy target and calls manual validation "possible" `[O spec p.3 2025-11-09]`; its workflow is
> "brouillon → validé → historisé" `[O spec p.5]`. The deck's decision rule is *"Low confidence → Reject / Retrain; Medium → Human
> review; High → Auto-approve"* `[O deck p.3 2026-02]`. Today: Tier 1 auto-accepts at QCS 0.75 while the QCS module says review below
> 0.90 and the worker drops its `needs_hitl` flag `[M pipeline.py:162, :464-467; worker/tasks.py:41; calculator.py:469-473]`, so four
> DOCX skipped review at QCS 0.776–0.795 whose June benchmark accuracy was 1.00, 0.99, 0.82 and 0.76 — the highest QCS scored lowest
> `[M]`. Docling grades are always FAIR (the extractor probes attributes the document lacks), capping QCS at 0.82 — so carrying
> `needs_hitl` through sends 100 % of Tier-1 documents to review `[M docling_extractor.py:418-464]`. "Low → Reject" on today's bands
> would reject both scanned audit packs (BRA 0.497 with its 3 primary statements found; Aquaculture 0.575) `[M]`. Skipped checks count
> as "not failed" in routing `[M sweep bilab2: 4 skipped, hitl false]`. Arithmetic cannot see scale, period, entity or column-swap
> errors — they still balance `[P]`. The only written targets ("≥ 70 % automation, < 2 % critical errors") are a team suggestion, never
> measured `[T PoC-Experimentation-Plan §5.4 — unconfirmed]`.
>
> **Options**
> - [x] **A. Both ends earned.** The deck's rule is the target; in the MVP no document is auto-approved **or** auto-rejected on an
>   uncalibrated score: High = fast-track review (pre-filled), Low = full review with a re-scan suggestion. One definition of
>   High/Medium/Low (0.90, `needs_hitl` carried through); "skipped is not passed". A class (PDF natif / PDF scanné / image) may
>   auto-approve only when its measured silent-error rate is at or below your threshold, all 8 green rules ran and passed,
>   unit/period/entity/nature/opinion were confirmed, and the bank's risk function co-signs. Proposed "done" metrics `[P]`:
>   documents with every key field correct ≥ 90 % (natif) / ≥ 75 % (scanné); key-field accuracy ≥ 99.5 % / ≥ 98.5 %; **zero**
>   scale/period/entity/column-swap errors; 100 % primary statements found, 0 notes returned as statements. *(Recommended.)*
> - [ ] **B. Keep auto-approve at High from day one** (deck as written), threshold: ____ ; fix only the `needs_hitl` inconsistency.
> - [ ] **C. Everything reviewed, permanently** — no auto-approve in this product version.
> - [ ] **Other / feedback (targets to change):** ____
>
> > [!tip] Recommendation: **A**. Under B, "High" means a score nobody has calibrated, and today it is literally two different numbers.
> > Note the trap: fixing `needs_hitl` alone under B sends 100 % of Tier-1 documents to review anyway (RTE-3) — we ship RTE-1 with
> > RTE-3/4 recorded in the same slice.
>
> **Impacts:** RTE-1, RTE-2, RTE-3, RTE-5, VAL-5, UI-4, MEA-4, MEA-5, MEA-7, SCP-4 · merges C3, C4, E-3 · Master-Plan P2, P7 · Backlog B-23.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q5-FE — Named users and four-eyes review

> [!question]+ Q5-FE · status: open
> **Finding.** The review UI signs in silently as the bootstrap superuser, so every correction and validation is attributed to one
> shared admin `[M hitl/app.py:282-286; bootstrap.py:45-48; review.py:145-150]`. A machine API key can move a document to `validated`,
> and the same person can upload, correct and validate `[M review.py:251-277]`. BCT circ. 2006-19 art. 8 requires a clear separation of
> authorisation, execution, recording and control `[R]`.
>
> **Options**
> - [x] **A. All three:** per-user login in the UI (named actor in every audit event); preparer ≠ validator for key-field corrections;
>   API keys cannot validate. *(Recommended.)*
> - [ ] **B. Named login only** — no preparer/validator split in the pilot (single analyst).
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. A is required under every Q5 answer for a bank audit trail; B is defensible only for a one-person pilot,
> > and it should be written down as such.
>
> **Impacts:** SEC-5, SEC-6, UI-2 · Master-Plan P5a.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q5-RT — What "Retrain" means

> [!question]+ Q5-RT · status: open
> **Finding.** The deck says "Low confidence → Reject / **Retrain**" and "Approve, retrain, or discard" `[O deck pp.2-3 2026-02]`; the spec
> promises a "bibliothèque de templates apprenante" `[O spec p.5]`. Today: reviewer corrections are captured and an improvement loop
> exists `[M feedback.py:175-201]`, but the deployed matcher reads approved variations from a local file once per process and compose
> has no shared volume, so the worker never sees them `[M nomenclature.py:913-914]`; learned label variations are promotable into the
> **shared** nomenclature — a cross-bank leakage path `[M models.py:225-233]`; retraining on a bank's client data is a purpose extension
> under Law 2004-63 `[R]`.
>
> **Options**
> - [x] **A. Retrain = reviewer corrections captured as per-tenant labelled data, on-prem**, with the bank's consent; no promotion across
>   tenants without anonymisation; the deployed loop closed (worker reads approved variations from Postgres). *(Recommended.)*
> - [ ] **B. Shared learning across banks** (variations promoted into the common nomenclature after review).
> - [x] **C. No learning loop in the MVP** — corrections stored only for the accuracy measurement.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. It keeps the owner's loop and stays inside 2004-63; B needs an anonymisation step first (Backlog B-13).
>
> **Impacts:** GOV-5, OPS-9, SCP-9 · Master-Plan P5a · Backlog B-13.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q1 — Audit-report packs and the identity card

> [!question]+ Q1 · status: open
> **Finding.** BCT circ. 91-24 art. 7: banks must obtain CAC-certified EF from clients with more than 5 MD of system-wide exposure;
> art. 8: class-0 status needs certified documents < 18 months **and** provisional statements < 3 months `[R]`. Circ. 2006-19 art. 24:
> every credit file holds three years of EF plus forecasts; circ. 2022-01 art. 7: viability assessed on certified EF of the last three
> years, consolidated where the borrower belongs to a group `[R]`. NC 01 §22: audited EF are presented with the auditor's opinion `[R]`.
> Corpus: 6 of 16 distinct files are audit or limited-review packs; in them the primary statements are 21 of 194 pages (10.8 %) `[M]`;
> one pack (BRA) carries a qualified opinion `[M]`. The spec's perimeter says "documents EF scannés ou **annexés**" and lists metadata
> "client, période" `[O spec p.2, p.5 2025-11-09]`; the deck classifies each page and "localise Bilan, CR et Flux" `[O deck p.3 2026-02]`.
> Today no page classifier exists — classification runs after OCR on tables `[M pipeline.py:401-408]`; Tier-3 page provenance is wrong on
> three digital packs `[M]`; a forecast has the same layout as an actual statement and no "nature" field exists `[M]`.
>
> **Options**
> - [ ] **A. Packs are a first-class input.** Return the primary statements (N and N-1 labelled) plus an **identity card** auto-suggested
>   and reviewer-confirmed: entity, matricule fiscal / RNE id, bank client id; period start/end; annual/interim; individual/consolidated;
>   standard and model; currency and unit; nature (certified / limited review / compilation / provisional / **prévisionnel**);
>   auditor opinion — the paragraph is shown and the reviewer picks the type; réserves and going-concern paragraphs flagged.
>   Fields beyond "client, période" are **proposed additions**. *(Recommended.)*
> - [ ] **B. Packs supported, identity card = owner minimum only** (client, période); nature and opinion Day 2.
> - [x] **C. Statement-only documents in the MVP**; packs rejected with a message.
> - [x] **Other / feedback (strike any field):** Keep for later iterations
>
> > [!tip] Recommendation: **A**. "Certified by CAC" cannot be set without the opinion type, and a forecast stored as actuals is the
> > silent error a bank fears most; both are cheap once the page classifier exists.
>
> **Impacts:** CLS-4, CLS-10, CLS-15, SCP-10, VAL-9, MEA-2 · Master-Plan P3, P4 · Backlog B-17.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q6 — The "< 30 s par document" target

> [!question]+ Q6 · status: open
> **Finding.** Spec §7.2: "traitement < 30 s par document" and "traitement en parallèle de 10 états financiers" `[O spec p.4 2025-11-09]`;
> the deck's unit is "Document Received" — the uploaded file `[O deck p.3 2026-02]`; NC 01 §18 calls each statement "un état financier",
> so "10 états financiers" may mean 10 statements ≈ 3 documents `[R]`. Measured (sequential, unrecorded hardware): 5.9 s/page digital,
> ≈ 11.7 s/page scanned, 13.6 s/image; every multi-page pack misses 30 s (42–348 s); if only the primary pages were processed and
> triage cost 1 s/page, still only 1 of 8 packs passes `[M sweep 2026-08-14]`. In-container figures are 2–3× worse
> `[T OCR-Engine-Benchmark-Matrix, June 2026 — re-measure]`. The worker runs one document at a time and reloads models every 20 tasks
> `[M Dockerfile.worker:33; celery_app.py:47]`; no load test or timing instrumentation exists `[M]`; no sub-second page classifier
> exists for scanned pages `[M]`.
>
> **Options**
> - [ ] **A. Split target.** Digital documents: ≤ 30 s per uploaded file, triage included, p95, measured at concurrency 10 on the G-1
>   machine. Scanned documents: a separate target set once page triage is built and measured. Load test + latency/throughput
>   instrumentation become MVP acceptance items. "10 états financiers" = **10 uploaded files** unless you say statements. *(Recommended.)*
> - [ ] **B. Per file, all inputs, 30 s** — accept that scanned packs fail until a GPU or a faster OCR path exists.
> - [ ] **C. Per statement set** (primary pages only), 30 s; other pages off the clock.
> - [x] **Other** : We'll try to optimize time but not sure what's feasible; parallelization will depend on available ressources
>
> > [!tip] Recommendation: **A**. It keeps the owner's number where it is achievable and makes the scanned target a measurement, not a guess.
>
> **Impacts:** OCR-8, CLS-10, OPS-3, OPS-4, OPS-6, MEA-9, MEA-10, SCP-7 · merges C7 · Master-Plan P3, P5b.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q2 — Notes to the accounts

> [!question]+ Q2 · status: open
> **Finding.** The spec targets bilan, CR and TFT "par défaut" and never mentions notes `[O spec p.3 2025-11-09]`; the deck puts
> everything else in "Other content" `[O deck p.3 2026-02]`. But notes are legally part of the EF (NC 01 §18) `[R]`, and NC 01 §43/§45 make
> the **reference** income statement classify charges by function with the by-nature breakdown in the notes — Air Liquide and Ciments de
> Bizerte use it `[R]` `[M]`. The owner filed the green rules "détail des charges / produits = total du CR" under "Cohérence Compte de
> Résultat – **Annexes**" `[O rules docx 2025-12-02]`; the green TFT↔Bilan cash rule reconciles only through the overdraft note, and the
> Report-à-nouveau rule needs dividends and reserve allocations from the notes `[R NC 01 §67]` `[M]`. So **4 of the 8 green rules need
> note data.** Today notes masquerade as statements: Ciments de Bizerte yields 62 "statements", 25 typed `bilan_actif` `[M sweep]`.
>
> **Options**
> - [x] **A. Notes recognised and kept, three supporting tables extracted for validation only** (never output as statements): charges by
>   nature / tableau de passage; the cash-and-overdraft note; the equity-movement / result-appropriation table. Every page carries a
>   visible label (statement / note / auditor report / cover / forecast) the reviewer can change; the off-balance-sheet / pledges note
>   is tagged so the reviewer finds it. *(Recommended.)*
> - [ ] **B. Notes recognised and kept, nothing extracted** — the four rules return "non vérifiable sans annexes".
> - [ ] **C. Notes discarded** at page triage (not retained).
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. B is faster to build but leaves half the owner's own checks unevaluable on reference-model statements;
> > C loses information a reviewer needs. In every option a misclassified primary statement must never silently disappear — hence
> > the changeable label.
>
> **Impacts:** CLS-5, VAL-1, VAL-2, VAL-3, VAL-6, SCP-14 · Master-Plan P4 · Backlog B-16.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q8 — Cash-flow statement (TFT)

> [!question]+ Q8 · status: open
> **Finding.** Objective 1 names only "bilan et état de résultat", while the perimeter and §1.2/§1.3 include the TFT "par défaut"
> `[O spec pp.2-3 2025-11-09]` — conflict C1. The deck locates "Bilan, CR et Flux" and marks "All required statements found? → Flag
> missing **TBD day2**" `[O deck p.3 2026-02]`; the spec's "vérification automatique … de la complétude" is untagged `[O spec p.3]` —
> conflict C2. The TFT is mandatory for every commercial company under the SCE (exceptions: NC 42 simplified regime; NC 16 funds) `[R]`;
> the green rule "Trésorerie de clôture du TFT = trésorerie au bilan" is the owner's `[O rules docx]`. Corpus: TFT present in 5 of 16
> files; in 2 audit packs the auditor cites a TFT absent from the file `[M]`. Today: of the 2 TFTs with trustworthy page provenance,
> both were typed `bilan`; the active CRITICAL rule V2-002 matches keywords that miss the corpus label "Trésorerie à la clôture de
> l'exercice" and compares gross liquidités, so it skips — or would raise a false critical `[M rule_engine.py:724-734]`.
>
> **Options**
> - [x] **A. TFT located, typed, extracted and reviewed whenever present.** Detection/typing is in the MVP gate; value accuracy is
>   reported, not gating (only 5 TFTs to measure on). The cash rule is redefined as liquidités − découverts (NC 01 §67), stays below
>   CRITICAL until validated on the 5 corpus TFTs. "TFT absent" is a completeness flag — pulled into the MVP although the deck says
>   TBD day2. *(Recommended.)*
> - [ ] **B. Objective 1 literally** — TFT extracted best-effort, no gate, no completeness flag in the MVP.
> - [ ] **C. TFT fully in the gate**, value accuracy included.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. It resolves C1 in favour of the perimeter and C2 in favour of the spec — say so in the Decision line
> > so the deck can be updated.
>
> **Impacts:** VAL-1, CLS-14, SCP-3 · merges C1, C2 · Master-Plan P4 · Backlog B-17.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q3 — Entity types in scope

> [!question]+ Q3 · status: open
> **Finding.** The spec targets "entreprises" and a data model "paramétrable selon le type d'entreprise" `[O spec p.3 2025-11-09]`.
> 1 of the 12 corpus entities is an investment fund (`fis_innov_invest`, placed in your "Sample - OF" folder) `[M]`; Tunisian funds may
> not borrow (SICAV, FCP, FCPR) and use a different statement model (actif net, état de variation de l'actif net, no TFT) `[R NC 16;
> code OPC]`; banks (NC 21-25), insurers (NC 26-31), microfinance (NC 32-34), simplified regime (NC 42) each have their own model `[R]`;
> leasing companies file under NCT 21 and **are** bank borrowers (ATL borrows 48–125 MD short-term) `[M]`. BCT 2022-01 art. 7 draws on
> certified **consolidated** EF where the borrower belongs to a group; the corpus holds one consolidated pack (somocer) and the
> nomenclature has no consolidated lines (goodwill, intérêts minoritaires, part du groupe) `[R]` `[M]`. No entity-type detector and no
> document-level "unsupported" outcome exist; bank/insurer bilans share vocabulary and could pass the gate `[M nomenclature.py:238-251]`.
>
> **Options**
> - [x] **A. NC 01 companies + consolidated NCT in the MVP.** Funds, banks, insurers, NC 42 are **detected** and given an "unsupported"
>   outcome with a reason, never forced into the company mapping. Leasing and microfinance: the pilot bank decides from its portfolio.
>   *(Recommended.)*
> - [ ] **B. Individual NC 01 companies only**; consolidated packs also "unsupported" in the MVP.
> - [ ] **C. Add the fund model too** (fis_innov was meant to work) — Backlog B-3 moves into P4.
> - [ ] **Other / feedback — was fis_innov included deliberately as a case expected to work?** ____
>
> > [!tip] Recommendation: **A**. Consolidated EF are in the regulator's own checklist; funds are not borrowers. Either way the
> > "unsupported with reason" outcome is needed so a bank's own balance sheet is never mapped as a company's.
>
> **Impacts:** CLS-11, CLS-12, CLS-13, CLS-16, UI-6, SCP-8 · Master-Plan P4 · Backlog B-3, B-4.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q4 — Rotated scans, phone captures, and "Quality KO → END"

> [!question]+ Q4 · status: open
> **Finding.** "PDF scanné" and "images (JPEG, PNG)" are accepted unconditionally `[O spec p.5 2025-11-09]`. Root cause of the unreadable
> document, verified: every fis_innov page carries `/Rotate 270`; docling-parse 4.7.3 (pinned by docling 2.70.0) returns no bitmap rects
> for 180/270 pages, so RapidOCR is never called — the document comes back empty (29 characters) `[M]`. A PyMuPDF de-rotation of a
> temp copy recovers 25,367 characters and 16/16 tables, costs ≈ 20 ms, leaves EF CLB byte-identical — but must promote the page
> CropBox to MediaBox first or cropped pages grow and the overlay misaligns `[M]`. Upstream fixes exist in docling-parse 5.3.1/5.3.2
> (docling ≥ 2.75), an untested upgrade `[M constraints.txt]`. `/Rotate 90` (La Viande Blanche, Air Liquide, BIAT) causes no proven loss —
> the LVB p6 loss is the Tier-3 index pairing `[M]`. EXIF orientation is dropped on image upload `[M pipeline.py:1011-1013]`. The deck's
> "Quality Score KO → END" `[O deck p.3 2026-02]` has no pre-OCR signal to run on; Celery retries exceptions 3×; the only phone capture
> (BRA) has the lowest QCS (0.497) yet its three primary statements were found `[M]`.
>
> **Options**
> - [x] **A. Fix now with the PyMuPDF helper (CropBox promotion, inherited-rotation guard, four fixtures) + EXIF transpose**; skew measured
>   before any deskew; camera images accepted behind a readability check that **flags for full review with a re-scan request, never
>   ENDs** until a pre-OCR metric is calibrated (P7); Docling upgrade evaluated later behind the golden gate (B-1). *(Recommended.)*
> - [ ] **B. Wait for the Docling upgrade** (docling ≥ 2.75) instead of the helper; fis_innov stays unreadable until then.
> - [ ] **C. As A, but implement END now** on Docling's own grade (note: it grades the unreadable file GOOD).
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. The helper is small, measured and reversible; C would have ended a usable document (BRA) on a signal that
> > passed an empty one.
>
> **Impacts:** OCR-1, OCR-2, OCR-3, OCR-4, OCR-5, OCR-11, RTE-8 · merges E-1 · Master-Plan P2, P7 · Backlog B-1, B-2.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q7 — NCT-only MVP

> [!question]+ Q7 · status: open
> **Finding.** Objective 2 (priority 2, untagged): "permettre la configuration du plan comptable (IFRS, NCT, SYSCOHADA)"; §3 calls all
> three "supportés" `[O spec pp.2-3 2025-11-09]`. Today only NCT is complete: `load_default_dictionary()` takes no standard argument;
> the UI, API and config still offer IFRS and SYSCOHADA and silently run the NCT engine; the API accepts any string as the standard
> `[M nomenclature.py:914; config.py:29-33; hitl/app.py:207; documents.py:79,109]`. All 9 corpus PDFs are NCT/SCE `[M]`. Tunisian
> listed groups' FY2024/FY2025 consolidated EF are still NCT (STIP; two banks) although the CNC decided IFRS for public-interest
> consolidated statements (2021, postponed to 2023; IFAC 2026 "partially adopted") `[R]`. Tunisia is not an OHADA member `[R]`.
> Finexora's target market has no public trace `[M web search]`.
>
> **Options**
> - [x] **A. NCT content only.** Now (S): the UI/API/config offer NCT only and reject other values with a clear message. Later (B-18):
>   standard-agnostic plumbing, then the next standard chosen by the first non-Tunisian customer. Optional: per-bank editable mapping
>   tables and rule toggles (the owner's "Module de Règles Configurables"). **Departure from Objective 2.** *(Recommended.)*
> - [ ] **B. Best-effort IFRS/SYSCOHADA in the MVP** — keep offering them; documents run through the NCT engine with a warning.
> - [ ] **C. Build a second standard in the MVP:** [ ] IFRS [ ] SYSCOHADA — large domain-content work.
> - [ ] **Other / feedback — target market:** ____
>
> > [!tip] Recommendation: **A**. B is the current silent behaviour, which is the worst of the three; C needs a customer to justify it.
>
> **Impacts:** DOC-1, VAL-4, CLS-16 · Master-Plan P4, P5b · Backlog B-18.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q9-A — What "(V2)" defers; structured storage

> [!question]+ Q9-A · status: open
> **Finding.** The only "(V2)" tag closes the sentence *"Les données ainsi extraites sont standardisées et stockées dans un référentiel
> central, ce qui facilite leur utilisation ultérieure (reporting, scoring, analyses). (V2)"* `[O spec p.2 2025-11-09]` — but Objective 1
> requires "stocker ces données dans une base de données structurée" and §6 / §4 describe a central database, both untagged `[O]`.
> Today each result is one JSON blob on the job row; the normalised layer is explicitly out of the model `[M models.py:3-4, :169]`.
> The project also names a validation tier "V2" `[M validation_rules.json]`.
>
> **Options**
> - [ ] **A. "(V2)" covers the whole introduction paragraph** (collection, analysis, centralisation).
> - [ ] **B. "(V2)" covers the last sentence** — standardisation **and** the central repository are version 2.
> - [x] **C. "(V2)" covers only the downstream uses "(reporting, scoring, analyses)"** — structured storage per document is MVP
>   (Objective 1), standardisation is MVP (Objective 2); the consolidated multi-period repository / "Vision 360°" is version 2.
>   Deferrals are written "Day 2", never "V2". *(Recommended.)*
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **C**. It is the only reading under which Objectives 1 and 2 are not themselves deferred.
>
> **Impacts:** DLV-9, VAL-9, DOC-4, SCP-5 · merges C5 · Master-Plan P5b · Backlog B-10.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q9-B — Intake and integration deferrals

> [!question]+ Q9-B · status: open
> **Finding.** The spec lists, untagged: channels "upload web, glisser-déposer, e-mail, import batch" (§1.1) and "dépôt via API, import
> batch depuis dossier partagé" (§3.1); formats incl. ZIP (twice); exports "Excel, QuickBooks, systèmes bancaires" and "API JSON/XML,
> CSV, connecteurs vers BI/ERP/CRM"; "OAuth2 ou SSO"; "historisation par client"; "Vision 360°" `[O spec pp.3-5 2025-11-09]`. Today:
> web upload and REST API exist; ZIP returns 415; no e-mail, batch folder, XML, QuickBooks or connectors; auth is bearer tokens + API
> keys (no SSO/OIDC) `[M documents.py:46, :89; users.py:74-80]`. One criterion is applied to every untagged item: MVP if a bank pilot
> cannot run end to end without it, or if it is cheap; everything else is a **proposed deferral** `[P]`. Liasse fiscale XML (F6001–F6004,
> not certified) and the BCT Centrale des bilans are possible sources/exports to raise with the pilot bank `[R]`.
>
> **Options**
> - [x] **A. As proposed.** MVP: web upload + API + **ZIP** (own size limit, bomb/traversal guards, members grouped in one dossier).
>   Deferred (Backlog B-5…B-10): e-mail intake, shared-folder batch, SSO/OIDC (at pilot), BI/ERP/CRM connectors, dashboards and
>   periodic reports, consolidated repository / Vision 360°, cross-document reconciliation, Centrale des bilans, liasse XML.
>   QuickBooks: **drop** (not relevant to Tunisian bank credit). "OAuth2 ou SSO" is met by bearer + API keys. *(Recommended.)*
> - [ ] **B. Keep every listed channel in the MVP** (e-mail + shared folder included).
> - [ ] **C. Defer ZIP as well** — upload + API only.
> - [ ] **Other / feedback (strike or restore items):** ____
>
> > [!tip] Recommendation: **A**. E-mail and shared-folder intake are bank-infrastructure specific; ZIP is cheap and promised twice.
>
> **Impacts:** DLV-5, DLV-7, DLV-11, DLV-12, DLV-13, SCP-12, SCP-13 · Master-Plan P5b · Backlog B-5, B-6, B-7, B-8, B-9, B-10.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## Q9-C — Pilot delivery surface

> [!question]+ Q9-C · status: open
> **Finding.** `GET /documents/{id}/result` returns the **uncorrected** job result in any state (draft or rejected); reviewer corrections
> are stored but never applied; the API correction schema lacks `statement_id` `[M documents.py:170-185; schemas.py:118-125]`. CSV and
> Excel exist only as UI buttons `[M hitl/app.py:1042-1070]`. The UI cannot review documents submitted through the API (no queue, list
> or search); its history is per session; the "historisé" transition is API-only `[M hitl/app.py:472, :2310, :2288-2290]`. `Document.entity`
> and `period` are never written; no Client or Dossier entity exists although credit files hold three years per borrower, guarantor and
> group entity `[M models.py:151-153]` `[R BCT 2006-19 art. 24]`. The corpus holds a byte-identical duplicate upload `[M]`.
>
> **Options**
> - [x] **A. As proposed.** A versioned **validated snapshot** (result + corrections applied) is the only export source; API returns data
>   with its workflow status (brouillon / validé), per-field confidence and flags, "validated only" as a bank-configurable policy;
>   JSON + CSV + Excel via API (XML if the bank needs it); review queue, import/status dashboard, persistent history and historisé
>   in the UI; Client keyed by matricule fiscal / RNE and a credit-file Dossier; content-hash dedupe. *(Recommended.)*
> - [ ] **B. Export raw + corrections separately** (no snapshot); UI queue only.
> - [ ] **C. Validated-only exports, hard-coded** (no draft data ever leaves the API).
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. Without the snapshot, "validated data" does not exist as a thing the API can return.
>
> **Impacts:** DLV-3, DLV-4, DLV-6, DLV-8, DLV-10, UI-1, UI-2, UI-3 · Master-Plan P5a (snapshot), P5b.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## E-8 — Webhook events and secrets

> [!question]+ E-8 · status: open
> **Finding.** The spec: "Webhook de notification … dès qu'un document est **traité**" `[O spec p.4 2025-11-09]`; the code's design says
> `document.validated` `[M webhooks.py:3-5]`. Webhooks can be registered but are **never sent** — no code creates a delivery `[M]`. The
> signing secret is stored as an Argon2 hash, from which an HMAC cannot be computed, so every existing partner needs a new secret
> `[M webhooks.py:67; security.py:69-70]`.
>
> **Options**
> - [x] **A. Two events:** `document.processed` (no figures — a draft exists) and `document.validated` (link to the validated data);
>   secrets stored reversibly (encrypted); partners re-issued. *(Recommended.)*
> - [ ] **B. `document.validated` only.**
> - [ ] **C. `document.processed` only** (as the spec literally says), payload includes draft figures.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. C would announce figures Q9-C says are not exportable; B loses the "traité" signal the spec promises.
>
> **Impacts:** DLV-1, DLV-2 · Master-Plan P5b.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## C6 — Accepted formats: DOCX and HEIC

> [!question]+ C6 · status: open
> **Finding.** The owner's list is "PDF natif, PDF scanné, images (JPEG, PNG), ZIP" `[O spec p.5 2025-11-09]`; the deck's "Format Check ?
> KO → END" would end a DOCX at step 1 `[O deck p.3 2026-02]`. The corpus holds 6 DOCX — team-made fixtures (all created within two
> minutes on 2025-11-11) that contain only pasted screenshots, no text layer, no Word tables `[M]`. HEIC (the iPhone default) is
> accepted by neither the API nor the UI, and their allowlists differ `[M documents.py:46; hitl/app.py:246,2326]`.
>
> **Options**
> - [x] **A. DOCX and HEIC as proposed additions** — DOCX kept (accountants send it); screenshot-DOCX counted as an **image** input class
>   for accuracy targets; HEIC accepted with EXIF honoured; API/UI allowlists aligned. *(Recommended.)*
> - [ ] **B. Owner's list only** — DOCX and HEIC rejected with a message.
> - [ ] **C. DOCX yes, HEIC no.**
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. Both are cheap; the class labelling matters more than the format.
>
> **Impacts:** OCR-12, SCP-6, MEA-2 (class labels) · Master-Plan P5b.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## E-10 — Labelled ground-truth set

> [!question]+ E-10 · status: open
> **Finding.** Zero human-labelled documents exist `[M Scoring-Approach-Analysis.md:302 — a fact, T doc]`; the benchmark's reference is
> AI-written (Opus) `[M BENCHMARK_REPORT.md:18]`. Proving a silent-error rate ≤ 0.5 % with zero observed errors needs about 600 green
> key-field cells **per class** (rule of three), more than the whole current benchmark (532 values); a per-cell rate hides per-document
> risk — at 0.5 %/cell over 120–200 cells, 45–63 % of documents carry at least one wrong green figure `[P]`. Real client documents
> cannot be used until G-1/G-2 and the P5a security items are done `[P]`.
>
> **Options**
> - [x] **A. Owner + one analyst double-key**, adjudicate disagreements; ≥ 600 green key-field cells per class (PDF natif / PDF scanné /
>   image incl. screenshot-DOCX); documents = the corpus + pilot-bank documents; stored on-prem, outside git. ≈ 10 days of labelling.
>   *(Recommended.)*
> - [ ] **B. Single labeller**, smaller set (≥ 200 cells per class), wider confidence intervals accepted.
> - [ ] **C. Bank staff label** during the pilot (the pilot itself produces the set; auto-approve earned later).
> - [ ] **Other / feedback — who, how many, which documents:** ____
>
> > [!tip] Recommendation: **A**. The set decides when anything may auto-approve; under Q5-A a slip costs throughput, not safety.
>
> **Impacts:** MEA-2, MEA-3, MEA-4, MEA-6 · Master-Plan P6.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## E-2 — Unit / scale (Known-Gaps D1)

> [!question]+ E-2 · status: open
> **Finding.** A statement headed "(en mDT)" read as "(en DT)" is wrong by 1000× and nothing in the model can express the difference: no
> unit field on `CanonicalTable`, `CanonicalCell` or `ColumnLayout`; the text above a table is not retained; EF CLB carries "(en DT)" on
> pages 1 and 4 but not page 3 `[P Known-Gaps D1]` `[M]`. NC 01 §19(c) requires the unit on every page; STB's FY2025 filing mixes mDT, DT
> and KDT `[R]` `[M]`. The codebase's standing rule: a reconstructed value is never emitted green `[M]`.
>
> **Options**
> - [x] **A. Record + flag, never rescale.** Detect the unit in headers **and** captions; bind it per statement; propagate a document
>   default; flag disagreements between statements; reviewer confirms; carry it in every export; feed `scale_resolver`. *(Recommended.)*
> - [ ] **B. Rescale to a document unit** automatically (values marked yellow).
> - [ ] **C. Record only**, no flags.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. B silently changes figures; C leaves the 1000× error invisible.
>
> **Impacts:** VAL-7, VAL-10, MEA-5 · Master-Plan P2.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## E-4 — Tier 0 for digital PDFs

> [!question]+ E-4 · status: open
> **Finding.** Tier 0 (native PyMuPDF text) can never accept a digital PDF today: gate G3 reads `metadata['column_confidence']`, which no
> code writes — a regression of the `dc1016c` branch, since metadata is now non-empty and the 0.80 default no longer applies (0.00 on
> 6/6) `[M pipeline.py:847-851]`; but G1 (canonical match 6.7–57 % vs a 95 % bar) fails 6/6 and Tier-0 QCS (0.740–0.942) is below the
> 0.95 acceptance bar on 6/6, so a G3 patch alone enables nothing `[M pipeline.py:421]`.
>
> **Options**
> - [x] **A. Fix G3 now** (read the real `ColumnLayout.confidence`); raise canonical label coverage toward 95 % in P4; revisit the 0.95
>   threshold only after calibration (P7). *(Recommended.)*
> - [ ] **B. Accept that Tier 0 stays a source for the Tier-3 vote, never an acceptor** — remove the Tier-0 accept path.
> - [ ] **C. Lower the Tier-0 bars now** so digital PDFs can auto-accept.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. C widens automation before measurement (G-4); B is honest but throws away the fastest path for clean files.
>
> **Impacts:** RTE-7 · Master-Plan P2 (G3), P4, P7.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## E-5 — Continuation grouping (Known-Gaps D2)

> [!question]+ E-5 · status: open
> **Finding.** The 2026-08-14 sweep grouped four statements across the corpus; a verifier checked each: all four were **false merges**
> (ciments p46-48 = Notes 18/19/20; p41-42 = two unrelated tables; COHAPROM p11 = same-page Notes 3–6), and a genuine continuation
> (ciments Note 14, p40→41) was **missed** because its halves were typed `bilan_passif` / `bilan` and the same-statement veto blocked it
> `[M]`. `statement_builder` accepts a page gap of 0 `[M statement_builder.py:223]`. The threshold 2.5 was never calibrated; the
> must-never-merge Passif→CR pair scores exactly 2.5, so the veto is load-bearing `[P Known-Gaps D2, corrected 2026-09-14]`.
>
> **Options**
> - [x] **A. Forbid same-page merges now; keep the veto; recalibrate the threshold on the corpus pairs** (p40→41 positive; p41→42, p46→48,
>   COHAPROM p11 negatives) after the note/statement typing improves (P4). *(Recommended.)*
> - [ ] **B. Disable grouping entirely** until a calibrated threshold exists (one table = one statement).
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. Same-page merges are forbidden under both options; B loses the one genuine case for nothing.
>
> **Impacts:** CLS-6, CLS-7, CLS-8 · Master-Plan P3 (CLS-8), P4.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## C8 — Report-à-nouveau rule (green rule 2)

> [!question]+ C8 · status: open
> **Finding.** The owner's green rule reads *"Report à nouveau N = Report à nouveau N-1 + Résultat N-1 − Dividendes distribués"*
> `[O rules docx 2025-12-02]`. Under the Code des sociétés commerciales, 5 % of profit goes to the legal reserve until it reaches 10 % of
> capital (art. 287 for SA; art. 140 for SARL), plus statutory and special reserves `[R]`. As written, the rule would fire on almost every
> profitable company. It is coded as V2-003, WARNING, with a bounded approximation because dividends live in the notes
> `[M rule_engine.py:855]`.
>
> **Options**
> - [ ] **A. Corrected formula:** RAN N = RAN N-1 + Résultat N-1 − dividendes − **affectations aux réserves** (± other appropriations);
>   returns "non vérifiable" when the appropriation is unknown; inputs from the equity-movement table (Q2-A). *(Recommended.)*
> - [x] **B. Keep the owner's formula** as an approximate WARNING only.
> - [ ] **C. Drop the rule** from the MVP.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A** — please confirm the formula is what you intended.
>
> **Impacts:** VAL-2 · Master-Plan P4.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## C9 — QCS versus the deck's scoring model

> [!question]+ C9 · status: open
> **Finding.** The deck shows per-step scores S1–S5 and "Apply weighted scoring model (TBD)"; step 6 stores "flags, scores, confidence
> interval" `[O deck p.3 2026-02]`. An S0–S7 weighting (0.10·S0 + 0.15·S1 … + 0.05·S7) appears only in the PDF's metadata title, not on any
> visible page `[M pipeline_workflow.txt:1]`. The team replaced that model with QCS (three layers + tiers) — no owner sign-off is recorded
> `[T PoC-Experimentation-Plan §3 — unconfirmed]` `[M src/qcs/calculator.py]`. QCS is a point score; no confidence band is stored `[M]`.
>
> **Options**
> - [x] **A. Confirm QCS as the "weighted scoring model (TBD)"**; document the S1–S5 → QCS-layer mapping; a calibrated confidence band is
>   Day 2 (B-11). *(Recommended.)*
> - [ ] **B. Re-implement the deck's S-score** alongside or instead of QCS (weights: ____).
> - [ ] **Other / feedback — does a separate document with the S0–S7 weighting exist?** ____
>
> > [!tip] Recommendation: **A**, unless an S0–S7 document exists that I have not seen.
>
> **Impacts:** DOC-5, MEA-8 · Master-Plan P0 · Backlog B-11.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## NEW-2 — Objective 5 (ratios, dashboards, alerts) in the MVP?

> [!question]+ NEW-2 · status: open
> **Finding.** Objective 5 "Reporting & KPI: calcul automatique des ratios financiers et tableaux de bord" ranks **above** Security (6) and
> Scalability (7) in the owner's prioritised list, untagged; §3.7 adds "rapports périodiques, alertes si incohérence" `[O spec pp.2, 5
> 2025-11-09]`. It had no roadmap item and no scope question; the only deferral ("full ratio computation is a V2 feature") is
> engineering's own `[T PoC-Experimentation-Plan:221 — unconfirmed]`. Today: inconsistency alerts exist as validation rules V1–V4; ratios
> = two INFO checks (FR, trésorerie nette) `[M rule_engine.py:1199-1350]`. BCT circ. 2022-01 art. 7 requires at minimum EBITDA / charges
> financières and Dettes / EBITDA in viability assessments `[R]`; the owner's rules doc lists FR, BFR, trésorerie nette, endettement,
> autonomie financière, solvabilité, marge, ROE, ROA `[O rules docx]`.
>
> **Options**
> - [ ] **A. Alerts (existing) + the BCT minimum ratios on validated data in the MVP**; the owner's full list and dashboards / periodic
>   reports Day 2 (B-15, B-10). *(Recommended.)*
> - [x] **B. Full Objective 5 in the MVP** (all ratios + dashboards + periodic reports).
> - [ ] **C. Alerts only**; all ratios Day 2.
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**. Two regulator-mandated ratios on validated data are cheap and show value; dashboards need the
> > repository Q9-A defers.
>
> **Impacts:** VAL-6, DOC-7 · Master-Plan P5b · Backlog B-15, B-10.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## NEW-3 — Retention, deletion, anonymisation

> [!question]+ NEW-3 · status: open
> **Finding.** Spec §7.1 requires "chiffrement … en transit et au repos (TLS 1.3, AES-256)" and §4 "anonymisation si nécessaire"
> `[O spec pp.4-5 2025-11-09]`; Law 2004-63 governs personal data `[R]`. Today: no TLS, no encryption at rest (MinIO SSE / encrypted
> volumes absent), no retention or deletion policy, no anonymisation `[M]`. Encryption at rest is treated as MVP with no decision
> needed (SEC-8, SEC-11).
>
> **Options**
> - [ ] **A. Retention + deletion in the MVP** with bank-configured limits (defaults proposed by you: ____ months for drafts, ____ years
>   for validated documents); anonymisation Day 2 unless development use of client data needs it earlier (B-13). *(Recommended.)*
> - [x] **B. Retention, deletion and anonymisation all in the MVP.**
> - [ ] **C. Policy documented, enforcement Day 2.**
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: **A**.
>
> **Impacts:** GOV-7, SEC-11 · Master-Plan P5a · Backlog B-13.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

## NEW-4 — Owner items none of the ten questions covered (accept as a set; strike what you disagree with)

> [!question]+ NEW-4 · status: open
> **Finding.** These spec items had no home in Q1–Q10 `[O spec 2025-11-09]` `[P Scope-Answers §6]`; each has a proposed disposition.
>
> **Options** (multi-select — untick to strike)
> - [x] **Language detection** (§3.2): French only in the MVP; Arabic/English detected and marked unsupported (CLS-16).
> - [x] **Missing values, dates** (§3.4, §6): explicit null vs zero; ISO period dates in the identity card (VAL-10).
> - [x] **Freshness flag** (BCT 91-24 art. 8: certified < 18 months, provisional < 3 months): optional MVP flag with configurable limits (SCP-11).
> - [x] **Configurable rules module** (rules docx R001): expose tolerance / severity / active per bank (VAL-4).
> - [x] **Monitoring** (§4: failure rate, latency, throughput): MVP, with the Q6 load test (OPS-7).
> - [x] **Status dashboard, version history, "historisé"** (§2.4): MVP (UI-2, UI-3); **search** deferred (B-10).
> - [x] **Confidence interval** (deck step 6): Day 2 after calibration (B-11).
> - [ ] **Other / feedback:** ____
>
> > [!tip] Recommendation: accept **all seven** as listed.
>
> **Impacts:** CLS-16, VAL-4, VAL-10, SCP-11, OPS-7, UI-2, UI-3 · Master-Plan P4, P5b.
> **Decision:** ____ · **Owner:** ____ · **Date:** ____

---

## Confirm: judgement calls already applied or settled by measurement

> [!warning]+ These carry one clear direction — untick to override, and say what instead.
> - [x] **No commit split of `dc1016c`** (OPS-12): every split point would have to pass the 515 tests; the four commits are already pushed to the branch.
> - [x] **Rebuild prod once, after P2** (E-7 / OPS-11), with the Alembic step OPS-2 → OPS-1 (`alembic stamp improvement_loop_0002` then `upgrade head`; never stamp head). Interim rebuild only if G-3 = B.
> - [x] **Same-page merges forbidden** (CLS-8) under either E-5 option.
> - [x] **The validated snapshot is the only export source** (DLV-3) under Q9-C options A and C.
> - [x] **D3 stays deferred** (CLS-17): measured, pinned by a test; adjacent fix RTE-6 lands in P2.
> - [x] **Tier-2 VLM stays a stub; no LLM arbiter yet** (RTE-9), until the accuracy gate says where Tier 1 fails.
> - [x] **High availability explicitly deferred** (OPS-13, Objective 7).
> - [x] **Golden corpus split public / non-public** (from G-2) — sessions see pass/fail + counts on the five client EF.
> - [x] **Known-Gaps D2 corrected** (2026-09-14): four false merges, p40→41 missed — done.
> - [x] **README standards claim fixed, `*.pem` added to `.dockerignore`, June probe outputs removed** — done 2026-09-14.

## Sign-off

- **Reviewed by:** ____
- **Date:** ____
- **Owner documents confirmed current (or superseded noted):** [ ]
- **Team documents classified (current / stale / retire):** [ ]
- **All 29 decisions resolved:** [ ]
- **Ready for me to start [[Master-Plan]] phase 1:** [ ]
