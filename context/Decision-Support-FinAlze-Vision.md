# Decision Support — FinAlze End-to-End Vision: Soundness & SOTA Alignment

**Status:** advisory · **Date:** 2026-05-30 · **Audience:** product/eng decision-makers
**Companion:** [Vision-and-Architecture-Directions.md](Vision-and-Architecture-Directions.md) (the endorsed directions + findings + references)

> An unbiased appraisal produced before committing to the backend + knowledge-graph + agent roadmap.
> Verdict first, then evidence, risks/contrarian views, and a de-risked sequence.

## The vision (neutral restatement)

A self-hosted, partner-facing platform that:
1. **Digitizes** financial statements (bilan / compte de résultat / flux de trésorerie; IFRS / NCT /
   SYSCOHADA; French) via a cascading OCR pipeline (Tier 0 native → 1 Docling → 2 VLM → 3 ensemble)
   with a reference-free Quality Confidence Score (QCS), V1–V4 accounting validation, and human-in-the-loop (HITL).
2. **Exposes a secure backend API** (auth, async job queue) to internal UIs, automation scripts, and **partner frontends**.
3. Builds a financial **knowledge graph** (FIBO/XBRL-aligned, bitemporal, provenance-tracked) from validated data.
4. Connects **AI agents with financial skills** that reason over the graph.

## Verdict (balanced)

**Directionally sound and SOTA-aligned as a 2–3-year north star — but large, and exposed to
over-engineering ahead of validated need. The dominant risk is *sequencing*, not direction.**

Every layer maps to a genuine state-of-the-art pattern. The danger is investing in the knowledge-graph
and agent layers before two things are proven: (a) **extraction accuracy on the real French/NCT
DOCX-screenshot corpus**, and (b) **concrete partner use cases** that justify a graph + agents over a
simpler relational/warehouse model.

**Recommendation: commit to the backend now; keep the KG + agent layers seam-ready but gated on evidence.**

## Where it's sound & SOTA-aligned

| Layer | SOTA-aligned? | Note |
|---|---|---|
| Cascading OCR + reference-free QCS + HITL | **Yes** | Standard multi-tier document-AI with confidence-based routing; HITL is expected in finance. |
| Ensemble (LV-ROVER + Consensus Entropy) | **Yes** | Grounded in current OCR-ensemble literature; already built and tested in-repo. |
| Secure async backend (FastAPI + Celery/RabbitMQ + fastapi-users) | **Yes (high confidence)** | Canonical, fully validated; the lowest-risk layer. |
| Tier-2 VLM via a decoupled vLLM/Triton service | **Yes** | Matches inference-serving SOTA. |
| Financial KG (FIBO/XBRL, bitemporal, provenance) | **Yes, emerging** | Real direction (FinReflectKG, Graphiti) but young tooling. |
| Agents (MCP + text-to-Cypher + HybridRAG + rule guardrails) | **Yes, immature** | Active research; numeric/hallucination correctness not yet production-solved. |

## Risks & contrarian views

- **Over-engineering vs validated need.** Four OCR tiers + ensemble + KG + agents + warehouse + three
  accounting standards is a lot to commit to before product-market and accuracy validation; parts may be premature.
- **Foundation-first.** Downstream value is capped by extraction accuracy on the *actual* corpus
  (French, NCT, DOCX-embedded screenshots). That accuracy is **not yet measured** — a stored sample shows
  real OCR errors (`ACTIFSNONCOURANTS`, `-37.430`/`=37430`, empty cells). If Tier-1 is weak there, the KG/agents are moot.
- **Is a KG justified *yet*?** Knowledge graphs earn their cost with many entities, cross-document/period
  relationships, and multi-hop questions. For single-statement extraction + ratios, a relational/warehouse
  model suffices. The KG may be premature absent concrete relationship/agent use cases.
- **Agent numeric correctness.** LLM agents over financial figures carry hallucination and regulatory
  risk; SOTA guardrails (automated reasoning, hallucination benchmarks) are emerging, not mature. Reusing
  the V1–V4 rule engine as a verifier helps, but agents remain probabilistic — risky for authoritative output.
- **Ontology gap for NCT/SYSCOHADA.** FIBO/XBRL are IFRS/US-GAAP-centric; Tunisian NCT and OHADA
  SYSCOHADA mappings are under-tooled — non-trivial ontology work.
- **Self-hosted TCO/ops.** Self-hosting VLMs + KG + warehouse + agents is a heavy operational burden for
  a small team; cloud APIs may be more pragmatic early.
- **Immature/fast-moving tooling.** Graphiti, FinReflectKG, text-to-Cypher, MCP are all 2024–2026 —
  betting the analytics layer on them invites churn.
- **Build-vs-buy.** Commercial document-AI / financial-extraction / BI products exist; a full in-house build has opportunity cost.

## Recommended sequence (de-risked)

1. **Prove the foundation.** Measure Tier-0/1 + V1–V4 accuracy on a real labeled French/NCT/DOCX subset
   (the Quality & Regression framework's CI golden-gate is this deliverable). Gate everything else on it.
2. **Build the backend now.** Justified regardless of downstream layers — UIs/scripts/partners need
   secure access. Include the cheap forward seams (validation events, provenance, ontology-ready keys,
   rule-engine-as-service).
3. **Tier-2 VLM** only where Tier-1 measurably fails.
4. **KG + agents** — pilot only after (a) extraction quality is sufficient and (b) ≥1 concrete
   multi-hop/relationship or agent use case is defined with a partner. Start with a thin GraphRAG
   read-model, not a full bitemporal KG; expand on evidence.

## Open questions (to finalize the decision)

- Concrete near-term partner analytics/agent use cases — what questions would they ask?
- Data volume — # entities / # periods / docs-per-month (informs KG-vs-warehouse necessity)?
- Hard on-prem mandate, or is cloud acceptable early?
- Team size / runway / timeline?
- Regulatory/audit bar for agent-produced figures?
- Is a ground-truth corpus available to measure extraction accuracy?
