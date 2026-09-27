# FinAlze — Established Vision & Architecture Directions

**Status:** living architecture doc · **Date:** 2026-05-30
**Companion:** [Decision-Support-FinAlze-Vision.md](Decision-Support-FinAlze-Vision.md) (soundness/SOTA appraisal + de-risked sequencing)

This document records the **established vision**, the **endorsed architectural directions per layer**,
the **key research findings**, and the **references** (NotebookLM deep-research notebooks + context7 library
validations) behind each decision. It is the durable companion to the implementation plan.

---

## 1. Vision

A **self-hosted, portable, partner-facing** platform that turns financial statements into trustworthy,
queryable financial intelligence:

**Digitize → Validate (HITL) → Serve via API → Knowledge Graph → Agentic analysis.**

- **Standards:** IFRS · NCT (Tunisia) · SYSCOHADA (OHADA). **Language:** French (Arabic later).
- **Principles:** transparency (every score/flag/decision inspectable), a closed **feedback loop**
  (corrections improve the system), reference-free quality signals, and **forward seams** so future
  layers are additive rather than rewrites.

## 2. De-risked sequencing (the order we build)

1. **Foundation** — measure extraction accuracy on a real labeled French/NCT/DOCX subset (CI golden-gate).
2. **Backend service** — secure API + auth + async queue (justified now; needed by all consumers).
3. **Tier-2 VLM** — only where Tier-1 measurably fails.
4. **Knowledge graph + agents** — gated on proven extraction quality + a concrete partner use case.

Rationale and risks: see the Decision-Support companion.

## 3. Architecture directions by layer

### 3.1 Extraction pipeline (built)
Cascading **Tier 0 native (PyMuPDF) → Tier 1 Docling → Tier 2 VLM (stub) → Tier 3 ensemble**, with a
3-layer reference-free **QCS**, a configurable **V1–V4 rule engine**, a **Nomenclature** (canonical
terms), and **HITL**. Tier 3 ensemble is implemented as **N-way LV-ROVER voting + Consensus Entropy**
(see [src/accounting/ensemble.py](../src/accounting/ensemble.py)).

### 3.2 Backend service (next — see implementation plan)
- **FastAPI** (async, Pydantic-native) · **fastapi-users (Postgres)** for human users with **RedisStrategy**
  (revocable tokens) + **Argon2/pwdlib** · **hashed partner API keys** with scopes · unified `get_principal`
  accepting **JWT *or* API key**.
- **Celery + RabbitMQ (broker) + Redis (result backend)** durable task queue; worker lazy-loads the
  pipeline; `acks_late` + `prefetch=1` + DLQ. API stays slim (no ML deps).
- **Async API** (202 + polling + webhooks/SSE), idempotency keys, **RFC 9457** errors, per-principal
  rate-limiting, Prometheus + OpenTelemetry, MinIO object storage.
- **Portable containers** (multi-stage, non-root, distroless) behind **Traefik**; Compose now / k8s later.
- Full surface: document lifecycle, **HITL workflow** (review queue, cell edits, state machine, audit),
  and a closed **feedback loop** (corrections → Nomenclature promotion).

### 3.3 Tier-2 VLM serving (future seam)
- **Never load the VLM in the Celery worker** (prefork → OOM). Run a **dedicated inference server**;
  the Tier-2 worker calls it over HTTP via a dedicated `ocr.gpu` queue (behind a Compose `gpu` profile).
- **Engine:** **vLLM** default (OpenAI-compatible, serves Qwen2.5-VL with image inputs, PagedAttention +
  continuous batching); **SGLang/LMDeploy** ~29% higher throughput if needed; **Triton** for multi-model.
- **Quantization FP8** (≈½ VRAM, preserves text fidelity). **GPU sharing** MIG or Triton (MPS cautioned).
- **Data plane:** pass image **URIs (MinIO)**, not raw bytes.

### 3.4 Analytics & intelligence — knowledge graph + agents (future seam)
- **Knowledge-graph-centric**, not just a warehouse: **Labeled Property Graph (Cypher)** aligned to
  **FIBO + XBRL** ontologies (FinAlze's canonical nomenclature is the natural anchor). **Bitemporal**
  (valid-time + system-time) modeling + **provenance/confidence** on every node/edge (FinAlze already
  emits these per cell).
- **Store:** Neo4j (mature, MCP server) or Memgraph (lean) — KùzuDB embedded; avoid cloud lock-in.
- **KG composes with** a ClickHouse/Postgres warehouse (hybrid) for heavy aggregation.
- **Agents** reason via **MCP + text-to-Cypher + HybridRAG (graph+vector)**, with **Graphiti/Zep**-style
  temporal memory and **provenance-attached** answers.
- **Numeric guardrail:** reuse the **V1–V4 rule engine** as the agent's accounting-invariant verifier.
- **Backend seams built now:** `document.validated` events; ontology/bitemporal-ready keys (canonical
  term, entity, period, standard, currency, provenance, confidence); rule-engine-as-service; reserved
  `analytics`/agent scopes + MCP endpoint.

### 3.5 Quality & Regression framework (transparent, automated)
- **Pre-deployment CI gate:** failure taxonomy → golden set (50–200 diverse docs) → scorer mix
  (~60% deterministic / 30% model-judge / 10% human) → baseline envelopes → CI regression-block.
  Metrics: **CER**, **TEDS/TEDS-S/GriTS**; **LLM-as-judge** (validated for bias via double-swap, jury,
  cognitive-load decomposition; agreement via **Gwet's AC1/PABAK**). Tooling: **DeepEval**.
- **Post-deployment (cheap, reference-free):** V1–V4 reconciliation + **Consensus Entropy** + QCS
  (all already produced) + new **`Recall_OCR`**; **drift** via KS test (input) and **PSI** (output).
  Tooling: **Evidently**.
- **Closed loop:** failures → HITL → feedback (variation→nomenclature) → **Lessons-Learned Registry** →
  grow golden set → tighten gate.

## 4. Cross-cutting principles
Multi-tenancy via `org_id` + scopes (enforced per query; RLS backstop) · encryption at rest + retention/
right-to-delete (financial PII) · upload AV-scan + content sniffing · SSRF-guarded HMAC-signed webhooks ·
end-to-end trace correlation (OTel across the Celery broker) · per-job **lineage** (pipeline + model +
config versions) for reproducibility.

## 5. Key findings (condensed)

- **Queue:** Celery + **RabbitMQ broker** + Redis backend is the recommended reliable pairing; **ARQ is
  unmaintained**. `acks_late`+`prefetch=1`+DLQ; the `worker_process_init` >4 s footgun → lazy model load.
- **Auth:** hybrid — short-lived revocable tokens for humans (fastapi-users **RedisStrategy**; no built-in
  refresh) + hashed/rotating **API keys** for partners; **OAuth2 client-credentials/mTLS** + Keycloak as escalation; BFF cookie for SPA.
- **Tier-2 serving:** decoupled inference server; vLLM default, SGLang/LMDeploy faster; **FP8**; MIG/Triton.
- **Analytics:** KG (LPG + FIBO/XBRL + bitemporal + provenance) composed with a warehouse; agents via MCP +
  text-to-Cypher + HybridRAG; **reuse V1–V4 as the agent guardrail**; emerging tooling (Graphiti, FinReflectKG).
- **Quality/regression:** two-tier (CI golden-gate with validated LLM-judge; reference-free runtime signals +
  KS/PSI drift) reusing QCS/V1–V4/Consensus-Entropy; learning loop via a lessons registry.
- **Corrected assumptions during research:** PyJWT (not python-jose); pwdlib/Argon2 (not passlib); Celery+RabbitMQ
  (not ARQ); fastapi-users RedisStrategy (not stateless-JWT+blocklist).

## 6. References

### 6.1 NotebookLM deep-research notebooks (this initiative)
| Notebook ID | Topic | Sources |
|---|---|---|
| `ec40df31` | Tier-3 Ensemble — OCR/table consensus SOTA (ROVER, WBF, Consensus Entropy) | 46 |
| `121dbb86` | Backend — API, Auth, Queue, Deploy + Tier-2 VLM serving (multiple reports) | ~200 |
| `3f5f64d5` | Analytics — warehouse/Medallion (ClickHouse, dbt, Cube) | 44 |
| `ceed50ab` | Analytics — open-ended knowledge-graph + agentic directions | 79 |
| `9b37f851` | Quality & Regression framework (pre/post-deploy + learning loop) | 67 |

Each report (and its imported web sources) is retained in the corresponding NotebookLM notebook;
full report text was also captured to the session's tool-results.

### 6.2 context7-validated libraries (decision-grounding)
FastAPI · fastapi-users · PyJWT · pwdlib (Argon2) · Celery (+ Kombu/RabbitMQ) · Redis · fastapi-limiter ·
Alembic (async) · pydantic-settings · SQLAlchemy (async + `version_id_col`) · rapidfuzz · vLLM ·
DeepEval · Evidently · OpenTelemetry-Python-Contrib.

### 6.3 Key external standards / works named in the research
ROVER / LV-ROVER · Weighted Boxes Fusion · Consensus Entropy (arXiv 2504.11101) · FinCriticalED ·
FIBO (EDM Council) · XBRL · Graphiti/Zep (temporal KG) · FinReflectKG (multi-hop / HalluBench) ·
Model Context Protocol (MCP) · RFC 9457 (problem+json) · OWASP API Security Top-10 · TEDS/GriTS · Gwet's AC1.
