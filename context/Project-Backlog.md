---
title: FinAIze — Project Backlog
type: backlog
applies-to: "[[Master-Plan]]"
related: ["[[Decision-Form]]", "[[Scope-Answers-Proposed]]"]
date: 2026-09-14
tags: [backlog, finaize, day2]
---

# Project Backlog

Deferred / Day-2 work created by the recommendations in [[Decision-Form]] and the sequencing in [[Master-Plan]].
Each item names the register ID it carries and the decision that creates it. Nothing here is scheduled;
an item moves into a Master-Plan phase only when its creating decision says so.

Source-authority tags: `[O]` owner statement (+date) · `[R]` regulation/standard · `[M]` measured at HEAD · `[T]` team doc, unconfirmed · `[P]` proposal.

## GA blockers (not pilot blockers)

> [!todo] B-12 · High availability (Objective 7) — (register **OPS-13**; confirm list)
> `[O spec p.2 2025-11-09]` lists "haute disponibilité"; nothing in the stack addresses it. Explicit deferral: GA item, not pilot.
> **Owner:** ____ · **Target:** ____
> - [ ] Done

> [!todo] B-13 · Anonymisation; cross-tenant promotion of learned variations only with anonymisation — (**GOV-7**, **GOV-5**; decisions **NEW-3**, **Q5-RT**)
> Retention + deletion ship in P5a; anonymisation is Day 2 unless development use of client data needs it earlier. `[R Law 2004-63]` `[M models.py:225-233]`
> - [ ] Done

> [!todo] B-19 · Cloud deployment path — (**GOV-6** cloud half; decision **G-1**)
> BCT circ. 2006-01 authorisation + INPDP declaration / transfer authorisation before any cloud hosting of client EF. Owner/legal work, not engineering. `[R BCT 2006-01 art. 3, 5-6]`
> - [ ] Done

## Day 2 — created by scope decisions

> [!todo] B-1 · Docling upgrade ≥ 2.75 (docling-parse ≥ 5.3.2) evaluated behind the golden gate — (**OCR-11**; decision **Q4 / E-1**)
> Upstream fixes #217/#219 make the PyMuPDF de-rotation helper unnecessary if they hold on fis_innov and the four rotation fixtures. A prior 2.70→2.101 drift broke `export_to_dataframe`, so this runs only after P6 with the golden compare = 0 diffs. `[M constraints.txt]`
> - [ ] Done

> [!todo] B-2 · Deskew correction for pages rotated > 1° — (**OCR-5**; decision **Q4**)
> Conditional on the P2 skew measurement of the scanned packs (BRA, Aquaculture, `Bilan 1.jpg`). Any correction must go through the single extraction/overlay frame (OCR-6). `[T Research-OCR-Engine-Selection-SOTA.md:178 — unconfirmed]`
> - [ ] Done

> [!todo] B-3 · Investment-fund statement model (NC 16-18) — (**CLS-12**; decision **Q3**)
> Only if the owner says fund cases (fis_innov) are expected to work: fund line items (PR/CH/AN codes, actif net, valeur liquidative), état de variation de l'actif net, no TFT. `[R NC 16]` `[M grep: 0 fund line items in src]`
> - [ ] Done

> [!todo] B-4 · Leasing (NCT 21) and microfinance (NC 32-34) statement models — (decision **Q3**, pilot bank)
> Both are real bank borrowers (ATL borrows 48–125 MD short-term under NCT 21). The pilot bank decides from its portfolio share. `[M atl_efi300624 extract]`
> - [ ] Done

> [!todo] B-5 · E-mail intake and shared-folder batch import — (**DLV-7**; decision **Q9-B**)
> `[O spec §1.1 and §3.1 2025-11-09]` lists both twice, like ZIP. Proposed deferral needing the owner's sign-off; bank-infrastructure-specific.
> - [ ] Done

> [!todo] B-6 · SSO / OIDC with the bank's identity provider — (**DLV-12**; decision **Q9-B**)
> "OAuth2 **ou** SSO" `[O spec §7.1]` is met by bearer tokens for people + API keys for systems; SSO is wired at pilot time against the bank's IdP.
> - [ ] Done

> [!todo] B-7 · Cross-document reconciliation of overlapping years — (**DLV-13**; decision **Q9-B**)
> When year N's N-1 column differs from year N-1's N column it is a **restatement** — a credit red flag to show, never a duplicate to delete. Needs DLV-9 (structured storage). `[R NC 01 §20]` `[M air_liquide "colonne comparative retraitée"]`
> - [ ] Done

> [!todo] B-8 · Liasse fiscale XML intake — (**SCP-12**; decision **Q9-B**)
> E-filing companies submit bilan, CR and TFT as schema-validated XML (F6001–F6004; the notes F6006 have no published XSD). Not certified, so recorded as source "liasse fiscale (non certifiée)" and reconciled against the certified EF. Raise with the pilot bank. `[R CCT liasse spec]`
> - [ ] Done

> [!todo] B-9 · BCT Centrale des bilans export / data source — (**SCP-13**; decision **Q9-B**)
> Banks already transmit clients' annual accounts to the BCT. Ask the pilot bank for its current format; map the canonical output to it or list it as an export. `[R IMF 2007; OECT 2022]`
> - [ ] Done

> [!todo] B-10 · Integrations and analytics surface — (decisions **Q9-B**, **NEW-4**)
> BI/ERP/CRM connectors; dashboards and periodic reports; the consolidated central repository / "Vision 360°"; review-UI search. QuickBooks: proposed drop (owner to confirm). Owner-sanctioned "(V2)" items plus proposed deferrals — the form's Q9-A says which reading of "(V2)" applies. `[O spec §2.6, §3.6, §3.7]`
> - [ ] Done

> [!todo] B-11 · Calibrated confidence band storage — (**MEA-8**; decision **C9**)
> The deck's step 6 stores a "confidence interval"; QCS is a point score. A calibrated band exists only after MEA-7 (P7). `[O deck p.3 2026-02]`
> - [ ] Done

> [!todo] B-14 · Currency conversion — (**VAL-10** Day-2 half; decision **NEW-4**)
> Currency is recorded in MVP; conversion ("Conversion montants (format, devises)") is Day 2. `[O spec §3.4]`
> - [ ] Done

> [!todo] B-15 · The owner's full ratio list — (**VAL-6** second half; decision **NEW-2**)
> FR, BFR, trésorerie nette, endettement, autonomie financière, solvabilité, marge commerciale, ROE, ROA — after the BCT 2022-01 minimum (EBITDA, EBITDA/charges financières, Dettes/EBITDA) ships on validated data. `[O rules docx 2025-12-02]` `[R BCT 2022-01 art. 7]`
> - [ ] Done

> [!todo] B-16 · Targeted note extraction — (decision **Q2**)
> Debt maturity schedules (échéancier des emprunts), fixed-asset movement tables, related-party transactions — the notes with the highest credit value beyond the three supporting tables extracted for validation in P4.
> - [ ] Done

> [!todo] B-17 · Automatic auditor-opinion extraction; "opinion cites a TFT that is missing" check — (decisions **Q1**, **Q8**)
> MVP shows the opinion paragraph and the reviewer picks the type. Automation needs OCR of the report pages (scanned on Aquaculture p3 and BRA p4). `[M corpus]`
> - [ ] Done

> [!todo] B-18 · Standard-agnostic plumbing and the next accounting standard — (**DOC-1** M-part; decision **Q7**)
> `load_default_dictionary()` takes no standard argument (`nomenclature.py:914`, 3–4 call sites); profiles and rules selected by standard; then IFRS or SYSCOHADA content, chosen by the first non-Tunisian customer. `[M]` `[O spec Objective 2]`
> - [ ] Done

> [!todo] B-20 · CUDA worker image + GPU device reservation — (**OPS-5** GPU half; decisions **G-1 / E-9**)
> Only if the reference machine is a GPU box. The worker image pins CPU `onnxruntime`; compose reserves no device. `[M Dockerfile.worker:24]`
> - [ ] Done

> [!todo] B-21 · D3 — move the quarantine gate earlier — (**CLS-17**; confirm list)
> Measured 2026-07-31: gating raw per-tier frames quarantines a genuine Compte de Résultat (margin 1). Stays deferred; pinned by `tests/test_phase0_fixes.py::test_the_gate_must_not_run_on_raw_per_tier_frames`. Adjacent fix RTE-6 (recompute QCS after quarantine) lands in P2. `[M]`
> - [ ] Done

> [!todo] B-22 · Tier-2 VLM serving + Tier-3 LLM arbiter for the 0.70–0.90 band — (**RTE-9**; confirm list)
> Only where Tier 1 measurably fails — which needs the P6 accuracy gate first. `[T Vision §2 item 3 — unconfirmed]` `[M pipeline.py:469, :1437]`
> - [ ] Done

> [!todo] B-24 · Fuzzy-match tie-break policy + vocabulary curation — (**OCR-15**; decisions **Q5**, **NEW-1**)
> WP-2.6 made the corrector deterministic (sorted vocabulary iteration). Three tie-break policies were measured on ciments against the hash-accident baseline — sorted first-best-wins (2 statements moved), ranked first-char → same-length → lexicographic (6 moved; qualitatively better: CARGE→CHARGE, BLAN→BILAN, COURAT→COURANT), refuse-on-ambiguity (27 moved, degrades labels). None reproduces the baseline because its winners were hash accidents. Choose with the labelled set (MEA-3) and calibration (MEA-7); the tie set is dominated by 1–3 letter function words (A/AN/AU/AUX/DE/DES/EN/ET/NE/OU) that arguably should not be fuzzy targets. Also: the corrector rewrites the unit token "(en DT)" → "(en D)" (OCR-16) — a unit-token whitelist belongs to the same curation. `[M]`
> - [ ] Done

## Pointers (not backlog)

> [!info] B-23 · Auto-reject / END and auto-approve per document class — (**RTE-5**, **RTE-8**, **MEA-7**; decisions **Q5**, **Q4**)
> Not deferred work: these live in Master-Plan **P7** and are "earned" per class after calibration. Listed so the form can point at where "earned" lands.

## Notes
- B-12, B-13 and B-19 are GA/compliance prerequisites; the pilot runs without them only under the on-prem model (G-1 = A).
- B-1 is the only item that can *remove* code (the de-rotation helper).
- Items B-3/B-4 change the nomenclature, not the pipeline; they are content work.
