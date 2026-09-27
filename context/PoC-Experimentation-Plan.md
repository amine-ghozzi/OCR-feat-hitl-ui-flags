# Experimentation Plan: High-Performance OCR for Administrative Archives

<!--
Revision Notes (2026-01-20):
- Integrated FinAlze v0.1 MVP context (banking financial statement digitization)
- Added Pre-Processing layer (Language Detection, Document Type Classifier)
- Added Accounting Layer (NER, Schema Mapping, Cross-Validation)
- Elevated HITL from exception path to core workflow
- Sample analysis: DOCX files contain embedded screenshots, OCR always required
- Languages: French (MVP), Arabic (deprioritized for future expansion)

Revision Notes (2026-03-14):
- Integrated domain knowledge from Documentation/ resources (treated as suggestions/reference):
  - "Règles de Contrôle pour la Validation des États Financiers" → expanded validation rules
    (cross-statement coherence, VNC formula, anomaly detection, configurable rule engine)
  - "EF standard + Plan de regroupement de référence" → NCT bilan/CR templates and
    account number mappings used as reference data for schema calibration
  - "Pipeline workflow.pdf" → reviewed; current QCS + tier architecture is preferred over
    the 5-score (S1–S5) model proposed there (adds complexity without PoC-stage benefit)
  - "Nomenclature et Fonctionnement des comptes" → chart of accounts reference for future use
- Expanded Accounting Layer with 4-tier validation hierarchy (intra-statement → cross-statement
  → anomaly detection → ratio computation)
- Added configurable rule engine pattern (JSON rule definitions)
- Phase 0 updated to reference NCT standard templates for ground-truth schema

Revision Notes (2026-04-01):
- Full Tier 1 implementation completed:
  - V1-V4 validation rule hierarchy implemented (balance equation, VNC, CR equation,
    cross-statement coherence, anomaly detection, ratio computation)
  - Configurable JSON rule engine (src/accounting/rules/validation_rules.json, 15 rules)
  - NCT account reference structure (src/accounting/rules/nct_accounts.json)
  - Statement segmenter with actif/passif sub-classification for split bilan tables
  - Rule engine uses sub-typed bilan groups for targeted value lookups (résultat on
    passif side, liquidités on actif side)
  - QCS scoring fixes: grade-mapped Docling scores, severity-weighted validation,
    critical_failures HITL override, low_grade pre-gate
  - Docling DocumentConverter simplified to default constructor
  - VLM placeholder set to qcs_score=0.0 (was falsely passing at 0.70)
  - 15 end-to-end tests passing
- Smart OCR text correction (src/preprocessing/smart_corrector.py):
  - Replaced naive dictionary-lookup approach with word-break DP + fuzzy matching
  - Vocabulary auto-extracted from YAML correction files (361 words for NCT)
  - Word-break DP splits unknown concatenations (e.g., TOTALACTIFSNONCOURANTS)
  - Levenshtein fuzzy matching corrects misspellings (Prouisions→Provisions,
    Chients→Clients, Imumobilisations→Immobilisations)
  - Combined approach handles both concatenation + misspelling in one pass
    (e.g., Aetifsimmobilises → Actifs immobilisés)
  - YAML term files enriched: NCT 99→387, IFRS 92→226, SYSCOHADA 98→260 patterns
  - Added headers section to all YAML files for document title corrections
  - Vocabulary sourced from Nomenclature des comptes (Classes 1-7), EF standard,
    and Règles de Contrôle documentation

Revision Notes (2026-05-29):
- Tier 3 ensemble implemented (Phase 5): replaced the "pick best prior result +
  always-HITL" stub with real N-way consensus voting.
  - src/accounting/ensemble.py: confidence-weighted voting across N engine outputs
    (Tier 0 native / Tier 1 OCR / Tier 2 VLM), generalizing the 2-way dual-tier
    reconciliation. ROVER node score S(w)=α·freq+(1−α)·conf for selection.
  - LV-ROVER: the winning label is verified against the NomenclatureDictionary
    (our existing domain lexicon); an agreed-upon-but-invalid term is rejected for
    the next-best verified candidate. (LV-ROVER, arXiv 1707.07432)
  - Consensus Entropy: label-free per-cell agreement δ (mean pairwise normalized
    Levenshtein distance — the robust K≥2 variant; the paper's per-candidate softmax
    entropy degenerates to 0 at K=2, our dominant Tier0+Tier1 case). Table quality
    Q_table = 1 − mean(δ). (Consensus Entropy, arXiv 2504.11101, 2025)
  - CE-driven adaptive routing in pipeline._tier3_ensemble: Q_table ≥ 0.90 auto-accept;
    0.70–0.90 → LLM-as-Judge arbiter band (hook present, judge not wired → HITL);
    < 0.70 or any critical arithmetic failure (from V1-V4 rule engine) → HITL.
    Single-source documents pass through to HITL (no corroboration earns a lower bar).
  - Deferred (need bounding-box plumbing extractors→enrichment→canonical model):
    WBF cell-box fusion and Hungarian bipartite cell matching; documented in the
    deep-research notebook (NotebookLM ec40df31, 46 sources) for follow-up.
  - 20 new end-to-end tests (tests/test_phase5.py); full suite 126 passing.
-->

## Executive Summary

This comprehensive experimentation plan outlines a rigorous methodology to identify and validate the most performant, scalable, and cost-effective self-hosted solution for digitizing administrative archives. The approach combines modular OCR pipelines with Vision-Language Models (VLMs) in a cascading architecture, backed by a multidimensional Quality Confidence Score (QCS) framework that enables production-grade quality assessment without ground-truth reference data.

**Target Application**: FinAlze v0.1 — Banking financial statement digitization platform for balance sheets, P&L statements, and cash flow statements. Supports IFRS, NCT (Tunisia), and SYSCOHADA accounting standards.

The core innovation is the **Native-First, Cascading + Ensemble strategy**: native text extraction for digital PDFs, fast pipelines for standard scans, and resource-intensive VLMs only for difficult documents. This minimizes computational cost while maintaining enterprise-grade accuracy across diverse document types.

> **Critical Finding**: Sample document analysis revealed that DOCX files contain embedded screenshots rather than native tables. This means OCR is **always required**, and Tier 0 native extraction will rarely apply for this use case.

---

## 1. Objectives and Strategic Context

### 1.1 Primary Objectives

1. **Identify the optimal self-hosted model combination** for administrative archives, accounting for document heterogeneity and the "No Free Lunch" theorem (no single model excels across all modalities).

2. **Engineer a reference-free evaluation framework** (QCS) that operates in production environments without ground-truth data, enabling autonomous decision-making and routing.

3. **Validate a proof-of-concept (PoC) hybrid system** demonstrating practical balance among speed, accuracy, and computational cost.

4. **Define a production-ready architecture** with clear operational guidelines, hardware sizing, and cost-of-ownership projections.

### 1.2 Scope and Success Metrics

**PoC Corpus**: 5,000 documents sampled from a 500–1,000 document stratified repository covering:
- Forms with printed and handwritten entries
- Complex tables (multi-page, merged cells, nested structures)
- Multi-column layouts with varied headers/footers
- Degraded and historical scans
- Scientific/technical documents with equations and formulas

**Key Performance Indicators**:
- **Automation Rate**: ≥70% of documents processed without human intervention
- **Accuracy**: Statistically significant reduction in critical errors (names, amounts, dates, IDs, table cells) vs. best single-model baseline
- **Throughput**: Median latency aligned with production SLOs; efficient GPU utilization
- **Cost**: Total cost of ownership (TCO) demonstrating clear advantage over cloud APIs ($1.5k–$50k per million pages)

---

## 2. Model Selection and Architectural Overview

### 2.1 Architectural Philosophies

| Philosophy | Architecture | Characteristics | Representatives |
|---|---|---|---|
| **Pipeline** | Modular sequential stages (detection → recognition → layout) | High throughput, parallelizable, deterministic | Surya, Marker, Docling, Tesseract |
| **End-to-End VLM** | Single forward pass with vision transformer + LLM | Superior layout/handwriting handling, contextual reasoning, higher latency | Qwen2.5-VL, DeepSeek-OCR, GOT-OCR2.0 |

### 2.2 Shortlisted Models and Roles

#### Tier 0: Native Text Extraction
- **PyMuPDF (fitz)**: Fastest extraction of embedded text layers from digital-born PDFs (~40–50ms per page)
- **pdfplumber**: Precision table extraction from native PDFs with visual debugging

#### Tier 1: Fast OCR Pipelines

| Model | Strengths | Target Use Case | Key Metrics |
|-------|----------|-----------------|-------------|
| **Surya** | High-performance layout analysis, 90+ language support, parallelizable detection+recognition | High-throughput bulk digitization; structured documents | 25 pgs/sec (H100, batch); 440MB VRAM/detection item |
| **Marker** | Fast Markdown/JSON conversion, artifact removal, hybrid `--use-llm` mode for tables/math | Speed-critical with structure preservation; LLM-ready output | 25 pgs/sec projected (H100); 2.8s single-page |
| **Docling** | Leading table extraction (97.9% accuracy), explicit `ocr_score`/`layout_score` | Financial/scientific PDFs; structure-critical workflows | 3.7s/page; strong hierarchical data fidelity |
| **Tesseract** | CPU-viable baseline, OCRmyPDF for searchable PDF overlays | Low-resource, archival compliance | 10 pgs/sec on Ryzen 5950X CPU |

#### Tier 2: VLM-Based OCR and Reasoning

| Model | Strengths | Target Use Case | Key Metrics |
|-------|----------|-----------------|-------------|
| **Qwen2.5-VL** | Naive Dynamic Resolution (native aspect ratio), 90% handwriting accuracy, VQA capabilities, DocVQA SOTA | Handwritten forms, fine-print engineering docs, visual reasoning | 2B: ≤6GB VRAM (quantized); 7B: 12–14GB FP16 |
| **DeepSeek-OCR** | Optical Context Compression (~100 visual tokens/page vs. 256+ for others), 4-bit quantization-ready | High-speed VLM fallback; token efficiency | ~2.5–60x speedup vs. standard VLMs |
| **GOT-OCR2.0** | Chart/formula interpretation, fine-grained control via prompting | Academic/scientific PDFs; data extraction from charts | 580M params; 1.4GB weights |
| **Nougat** | LaTeX generation from PDFs, arXiv-trained | Scientific document digitization | Slower due to autoregressive nature |

#### Tier 3: Ensemble Methods
- **ROVER (Recognizer Output Voting Error Reduction)**: Confidence-weighted textual consensus via sequence alignment
- **WBF (Weighted Boxes Fusion)**: Consensus bounding boxes for tables/regions

### 2.3 Pre-Processing Layer (NEW)

| Component | Tool | Purpose |
|-----------|------|--------|
| **Language Detection** | langdetect / fasttext | Route to appropriate OCR model (French primary, Arabic future) |
| **Document Type Classifier** | Simple CNN or heuristic | Distinguish Bilan vs Compte de Résultat vs Flux de Trésorerie |
| **Multi-page Segmentation** | PyMuPDF | Split documents for parallel processing |
| **Image Extraction** | Marker / python-docx | Extract embedded screenshots from DOCX files |
| **Horizontal Line Removal** | OpenCV (HoughLinesP) | Remove underlines that cause OCR misreads (e.g., "-944" → "=944") — *Future* |

### 2.4 Accounting Layer (NEW)

| Component | Tool | Purpose | Confidence |
|-----------|------|---------|------------|
| **Accounting NER** | spaCy (fr_core_news_sm + custom) | Identify "Chiffre d'affaires", "Immobilisations", etc. | Medium — requires custom training |
| **Schema Mapping** | Custom mapping tables | Map extracted items to IFRS/NCT/SYSCOHADA chart of accounts | High |
| **DataFrame Validation** | Pandera | Column types, coercion failure rate as QCS metric | High — user-validated |
| **Record Validation** | Pydantic | Cross-field validation (balance checks), computed fields | High |

#### 2.4.1 Validation Rule Hierarchy

Validation is organized in four tiers of increasing scope. Each tier produces pass/fail results with severity levels (VALID, WARNING, ERROR, CRITICAL) that feed into QCS Layer 3 and HITL routing.

**Tier V1 — Intra-Statement Arithmetic** (per table, automated):

| Rule | Formula | Tolerance | Severity |
|------|---------|-----------|----------|
| Balance equation | Total Actif = Total Capitaux Propres et Passifs | 1% or min 1.0 | CRITICAL |
| VNC consistency | Valeur Nette = Valeur Brute − Amortissements − Provisions | 1% | ERROR |
| CR equation | Résultat Net = Résultat d'Exploitation + Résultat Financier + Résultat Exceptionnel − Impôts | 1% | CRITICAL |
| Subtotal coherence | Each subtotal row = sum of its component rows | 1% | ERROR |
| Variation % | Reported variation = (N − N-1) / N-1 × 100 | 0.5 pp | WARNING |
| Field type integrity | Numeric columns contain valid numbers | — | ERROR |

> **Domain reference**: NCT standard (NCG § 38–40) defines the official bilan structure with Gross/Amortization/Net presentation per line item. VNC validation implements this directly.

**Tier V2 — Cross-Statement Coherence** (multi-table, requires statement grouping):

| Rule | Cross-reference | Severity |
|------|----------------|----------|
| Résultat Net match | Bilan (Capitaux Propres → Résultat de l'exercice) = CR (bottom line) | CRITICAL |
| Trésorerie match | Bilan (Liquidités et équivalents) = TFT (Trésorerie de clôture) | CRITICAL |
| Report à nouveau | Report à nouveau N = Report à nouveau N-1 + Résultat N-1 − Dividendes | WARNING |
| Variation capitaux propres | CP(N) − CP(N-1) ≈ Résultat Net (± distributions/apports) | WARNING |

> **Prerequisite**: Cross-statement rules require Step 2 segmentation to group pages by statement type within a single document. Single-table validation cannot detect these.

**Tier V3 — Anomaly Detection** (heuristic, flags for HITL):

| Check | Condition | Rationale |
|-------|-----------|-----------|
| Negative where unexpected | Capital social < 0, Immobilisations brutes < 0 | Accounting impossibility |
| Null operational values | CA = 0 for operating company | Likely extraction failure |
| Magnitude mismatch | Assets in millions, liabilities in thousands | Unit/scale OCR error |
| Suspicious proportions | Créances clients > 2× CA, Stocks > CA | Domain implausibility |
| Year-over-year rupture | Major post doubles or halves without explanation | Possible row misalignment |

**Tier V4 — Ratio Computation** (derived metrics, informational):

| Ratio | Formula | Expected Range |
|-------|---------|----------------|
| Fonds de Roulement (FR) | Capitaux permanents − Actif immobilisé | Positive |
| Besoin en FR (BFR) | Actif circulant (hors tréso) − Passif circulant (hors tréso) | Sector-dependent |
| Trésorerie Nette | FR − BFR = Tréso Actif − Tréso Passif | Cross-check |
| Endettement | Dettes financières / Capitaux propres | < 1 typical |
| Solvabilité | Actif réel / Dettes totales | > 1 |

> Ratios are informational (not blocking). They provide additional QCS signals and support the analyst's review in HITL. Full ratio computation is a V2 feature.

#### 2.4.2 Configurable Rule Engine

Validation rules should be defined as data, not hardcoded logic, to support per-standard rule sets and threshold tuning:

```json
{
  "rule_id": "V1-001",
  "rule_name": "Balance Sheet Equation",
  "tier": "V1",
  "formula": "total_assets = total_liabilities + equity",
  "tolerance": 0.01,
  "tolerance_min": 1.0,
  "severity": "CRITICAL",
  "applicable_statements": ["bilan"],
  "standards": ["IFRS", "NCT", "SYSCOHADA"],
  "active": true
}
```

This enables: adding new rules without code changes, per-standard rule activation, severity-based HITL routing, and audit trail of which rules were applied.

---

## 3. Quality Confidence Score (QCS) Framework

### 3.1 Three-Layer Evaluation Architecture

In production, reference text is unavailable. The QCS synthesizes three uncorrelated signal layers to estimate extraction quality and automate routing decisions.

#### Layer 1: Intrinsic Metrics (Model Uncertainty)

These derive directly from the OCR engine's internal state:

| Metric | Source | Description | Implementation |
|--------|--------|-------------|---|
| **Confidence Scores** | Model output | Per-word or per-line confidence (0–100 for Tesseract; 0–1 normalized for VLMs) | Calculate **Low-Confidence Density (LCD)**: percentage of words below calibrated threshold (e.g., 80%) |
| **Log-Probabilities (Logprobs)** | VLM logits | Softmax probability per generated token; log-space aggregation | Normalized sequence log-probability = mean of per-token logprobs; threshold e.g., –0.5 indicates likely hallucination |
| **Layout Confidence** | Docling/Surya | Explicit `ocr_score` (text quality) and `layout_score` (segmentation certainty) | Use both; low `layout_score` signals multi-column misreads |

**Calibration Note**: Confidence scales are non-standard across engines. Normalize via min-max scaling or engine-specific thresholds calibrated on a labeled validation subset.

#### Layer 2: Statistical Heuristics (Text Properties)

These analyze syntactic and structural patterns of extracted text:

| Metric | Tool/Method | Description | Use Case |
|--------|-------------|-------------|----------|
| **Gibberish Detection** | gibberish-detector (Markov chains) | Character-level transition probability; low probability indicates noise | Filter image artifacts and encoding errors |
| **Shannon Entropy** | Custom implementation | Measure text unpredictability; garbled OCR has high entropy; repetitive noise has low entropy | Flag outliers vs. "entropy safe range" calibrated on valid documents |
| **Lexical Density** | Standard spell-check libraries | Ratio of valid dictionary words to total words | Proxy for quality in prose-heavy documents; domain-specific dictionaries for technical content |
| **Regex/Pattern Coverage** | Custom regex + Automated Regex Discovery (RED) | Detect expected entities: dates, currency amounts, IBANs, SSNs | Validate administrative documents (invoices, tax forms, ID cards) |
| **Table Schema Validation** | Pandera library | Define strict DataFrame schema (column types, constraints) and measure coercion failure rate | Validate financial/tabular data; measure percentage of rows violating type constraints |

#### Layer 3: Semantic Metrics (Linguistic Understanding)

These employ local language models to evaluate meaning and coherence:

| Metric | Tool/Method | Description | Use Case |
|--------|-------------|-------------|----------|
| **Perplexity (PPL)** | quantized Llama-3-8B via llama.cpp | Measure how "surprised" an SLM is by extracted text; lower PPL = more fluent | **Ranking mechanism**: Compare outputs from multiple engines; lowest PPL indicates coherent extraction |
| **LLM-as-a-Judge** | Local SLM with prompt rubric | Evaluate coherence, formatting, and consistency on 1–5 scale | Detect VLM hallucinations; verify numeric consistency (e.g., sum line items = total) |
| **Consistency Cross-Check** | Comparative analysis | Run fast OCR (Tesseract) and VLM on same page; ask judge if outputs semantically align | Major divergence indicates VLM hallucination or corrupt native text |

### 3.2 QCS Aggregation Formula

For each page/segment:

1. **Normalize** all confidence scores to [0, 1] using min-max scaling calibrated on validation set.
2. **Compute layer scores**:
   - \( s_i \) = Intrinsic score (normalized confidence + logprobs)
   - \( s_s \) = Statistical score (1 – gibberish probability; entropy band check; lexical density; regex coverage; schema validation rate)
   - \( s_m \) = Semantic score (1 / (1 + ln(PPL)); LLM judge rating / 5)
3. **Aggregate**:
   $$QCS = w_i \cdot s_i + w_s \cdot s_s + w_m \cdot s_m$$

   Initial weights: \( w_i = 0.4, w_s = 0.3, w_m = 0.3 \) (tuned via correlation with human validation)

4. **Per-segment QCS** for critical regions (tables, key fields) to enable granular routing.

---

## 4. Experimental Design: Phased Execution

### Phase 0: Corpus Preparation and Calibration

**Objective**: Build a representative test corpus with partial ground truth for metric calibration.

**Tasks**:

1. Curate 500–1,000 documents stratified across:
   - Forms (printed + handwritten)
   - Complex tables (multi-page, merged cells)
   - Complex layouts (multi-column, scattered headers/footers)
   - Degraded scans (faint text, noise)
   - Scientific/technical (equations, formulas)

2. Create 10–15% ground-truth subset:
   - Full transcription: 30–50 documents
   - Partial annotation: key fields, 2–3 sample paragraphs, representative tables
   - Use for CER/WER calculation and QCS threshold calibration

3. Define templates and schemas:
   - Pandera schemas for financial statements (Bilan, CR, TFT)
   - Regex patterns for domain-specific entities (amounts, dates, account references)
   - YAML-driven term correction lists per accounting standard

4. Calibrate against official NCT standard templates:
   - Use the "Plan de regroupement de référence" (NCG § 38–40) as the canonical bilan structure:
     Actifs non courants (Immobilisations incorporelles/corporelles/financières with Brut/Amort/Net),
     Actifs courants (Stocks, Clients, Placements, Liquidités),
     Capitaux propres (Capital, Réserves, Résultats reportés, Résultat),
     Passifs non courants (Emprunts, Autres passifs, Provisions),
     Passifs courants (Fournisseurs, Autres, Concours bancaires)
   - Build account number → line item reference mappings (for schema mapping validation)
   - Note: NCT allows presentation in dinars or milliers de dinars (NCG § 20) — rounding level must be detected
   - Reference: EF Standard + Plan de regroupement de référence.pdf (Documentation/)

5. Define validation rule set (JSON-configurable):
   - Encode Tier V1–V4 rules from §2.4.1 as JSON rule definitions
   - Set initial tolerances (1% arithmetic, 0.5pp variation %, sector-neutral anomaly thresholds)
   - Activate per-standard rule subsets

**Deliverable**: Labeled corpus, calibration dataset, NCT reference schemas, and configurable validation rule set.

---

### Phase 1: Individual Model Benchmarking

**Objective**: Establish baseline performance for each candidate model.

**Methodology**:

1. **Environment**: Test on 1×RTX 4090 (24GB); project H100/A100 throughput from published benchmarks.

2. **Per-model evaluation** (Tesseract, Surya, Marker, Docling, DeepSeek-OCR, Qwen2.5-VL, optionally GOT-OCR2.0/Nougat):
   - **Throughput & Resources**: pages/sec, latency distribution, peak VRAM, GPU%, CPU%
   - **Quality on labeled subset**:
     - CER/WER vs. ground truth
     - Table structure: row length variance, header detection, schema coercion failures
     - Domain-specific accuracy: handwriting (%), multi-column reading order, table break handling, degraded scan resilience, formula parsing
   - **QCS metrics**: Compute full three-layer QCS for every page; analyze correlation with human evaluation
   - **Qualitative review**: 40–50 pages per model, focus on known failure modes

3. **Hardware variations**:
   - Compare FP16 vs. 4-bit quantization (vLLM, bitsandbytes, GGUF)
   - Measure VRAM and latency trade-offs

**Deliverable**: Performance matrix per domain with speed, cost, QCS distributions, and error typology.

---

### Phase 2: Cascading Architectures

**Objective**: Validate fast-fail pipelines that minimize VLM compute while preserving accuracy.

#### 2A: Native-First + Tier 1 Fast OCR

**Workflow per page**:

1. **Tier 0 – Native Extraction**
   - Use PyMuPDF `page.get_text()` and pdfplumber for text and tables
   - Run statistical checks (gibberish, entropy, lexical density) and schema validation
   - **Gate logic**: If \( QCS_{native} \geq \tau_{native\_high} \) (e.g., 0.95), accept and store; else escalate

2. **Tier 1 – Fast OCR Pipeline**
   - Route based on document type:
     - Table-heavy PDFs → Docling
     - Speed-critical bulk → Marker+Surya
     - CPU-only scenarios → Tesseract + OCRmyPDF
   - Compute full QCS
   - **Gate logic**: If \( QCS_{fast} \geq \tau_{fast\_high} \) (e.g., 0.85), accept; else escalate

#### 2B: VLM Tier and LLM-as-Judge

3. **Tier 2 – VLM OCR and Reasoning**
   - Full-page OCR:
     - DeepSeek-OCR as mid-tier (token-efficient)
     - Qwen2.5-VL as high-accuracy fallback (strong handwriting, fine print, VQA)
   - Specialized content:
     - GOT-OCR2.0 for formula/chart-heavy pages
     - Nougat for LaTeX-heavy academic PDFs
   - **LLM-as-Judge**: Consistency check against Tier 0/1 outputs (keywords, numeric sum, field alignment)
   - **Gate logic**: If \( QCS_{vlm} \geq \tau_{vlm\_med} \) (e.g., 0.75), accept; else escalate

4. **Tier 3 – Ensemble / Human-in-the-Loop**
   - Run 2–3 engines (e.g., Marker+Surya, Docling, DeepSeek-OCR/Qwen2.5-VL)
   - Apply ROVER for textual consensus, WBF for bounding boxes
   - Recompute \( QCS_{ensemble} \)
   - If still below \( \tau_{ensemble\_min} \), flag for human review with diff tooling

**Experimental Variables**:
- Threshold sensitivity: vary \( \tau_{native\_high}, \tau_{fast\_high}, \tau_{vlm\_med}, \tau_{ensemble\_min} \)
- Which model pairings minimize VLM calls while meeting error targets

**Deliverable**: Curves of automation rate vs. error rate vs. GPU cost; recommended thresholds and architecture.

---

### Phase 3: Ensembles and Specialized Flows

**Objective**: Quantify where ensembles deliver ROI and which combinations are optimal.

1. **Textual ROVER Experiments**
   - Triplets:
     - Tesseract + Surya + DeepSeek-OCR
     - Marker + Docling + Qwen2.5-VL
   - Measure CER/WER improvements; analyze gains by document type

2. **Layout Consensus (WBF)**
   - Fuse table/region boxes from Surya, Docling, and VLM detection
   - Evaluate crop quality and table structure metrics

3. **Task-Specific Ensembles**
   - Invoice/financial flows: tight schema validation + consensus
   - ID forms: regex/pattern validation + consensus

**Deliverable**: Accuracy-cost curves; recommendations for ensemble triggers.

---

## 5. Proof of Concept (PoC) Implementation

### 5.1 PoC Technical Stack

| Layer | Component | Tool/Library |
|-------|-----------|---|
| **File handling** | PDF/image parsing | PyMuPDF, pdfplumber |
| **Native text** | Text extraction | PyMuPDF (fitz) |
| **Tier 1 – Fast OCR** | Pipeline engines | Marker+Surya, Docling, Tesseract |
| **Tier 2 – VLM** | Model inference | vLLM (DeepSeek-OCR, Qwen2.5-VL), transformers library |
| **Tier 2 – Judge** | Semantic evaluation | quantized Llama-3-8B (llama.cpp or transformers) |
| **QCS evaluation** | Statistical metrics | gibberish-detector, custom entropy/lexical density, Pandera |
| **Ensembles** | ROVER + WBF | difflib/Biopython, ensemble-boxes |
| **Orchestration** | Pipeline logic | Python 3.10+; async for parallel Tier 1/2 |
| **Output format** | Canonical representation | Markdown/JSON with logical blocks and table metadata |
| **Deployment** | Infrastructure | Docker; self-hosted on NVIDIA A100/H100 or managed endpoints (e.g., E2E Networks) |
| **Pre-Processing** | Language + DocType | langdetect, custom classifier, Marker (for DOCX image extraction) |
| **Accounting Layer** | NER + Validation | spaCy, Pandera, Pydantic, JSON rule engine |
| **Validation Rules** | Configurable rule definitions | JSON rule files per standard (V1–V4 tiers) |
| **HITL UI** | Human Validation | Streamlit (st.data_editor, st.file_uploader) |

### 5.2 PoC Workflow (per document)

```
Input: PDF or scanned document

Step 1: Native Extraction (Tier 0)
  → PyMuPDF.get_text() + pdfplumber tables
  → Compute QCS (intrinsic + statistical)
  → If QCS ≥ τ_native_high → Accept, store, END
  → Else → Proceed to Step 2

Step 2: Fast OCR (Tier 1)
  → Route to Marker/Surya (bulk), Docling (tables), or Tesseract (CPU)
  → Compute QCS
  → If QCS ≥ τ_fast_high → Accept, store, END
  → Else → Proceed to Step 3

Step 3: VLM OCR (Tier 2)
  → DeepSeek-OCR or Qwen2.5-VL (full page)
  → LLM-as-Judge consistency check vs Tier 1
  → Compute QCS
  → If QCS ≥ τ_vlm_med → Accept, store, END
  → Else → Proceed to Step 4

Step 4: Ensemble (Tier 3)
  → Run 2–3 engines (Marker, Docling, Qwen2.5-VL)
  → ROVER voting + WBF layout consensus
  → Compute QCS_ensemble
  → If QCS_ensemble ≥ τ_ensemble_min → Accept, store, END
  → Else → Flag for human review, store with HITL flag

Output: Markdown/JSON with extraction confidence, metadata, and routing history
```

### 5.3 Accounting Post-Processing (NEW)

```
After OCR extraction (any Tier), for each document:

Step A: Statement Segmentation
  → Group extracted tables by statement type (Bilan, CR, TFT)
  → Use keyword matching on headers/labels (e.g., "ACTIF", "PASSIF", "PRODUITS")
  → Assign identification confidence per table
  → Flag if required statements are missing (e.g., no CR found)

Step B: Accounting NER & Schema Mapping
  → Identify financial line items via term matching (YAML-driven)
  → Map to selected accounting standard (IFRS / NCT / SYSCOHADA)
  → Reference: NCT Plan de regroupement provides account number → line item mapping
    (e.g., accounts 211–237 → Immobilisations incorporelles)
  → Validate against chart of accounts template
  → Track mapping confidence (confident vs. ambiguous → flag for HITL)

Step C: Validation (4-tier hierarchy, see §2.4.1)
  → Tier V1: Intra-statement arithmetic (balance equation, VNC, subtotals, variation %)
  → Tier V2: Cross-statement coherence (Résultat Net Bilan = CR, Trésorerie = TFT)
  → Tier V3: Anomaly detection (negatives, nulls, magnitude, proportions)
  → Tier V4: Ratio computation (FR, BFR, Trésorerie Nette — informational)
  → Each rule produces: status (VALID/WARNING/ERROR/CRITICAL), expected, actual, deviation
  → Aggregate validation pass rate feeds QCS Layer 3 (semantic score)

Step D: Auto-Correction (where safe)
  → Variation % recalculation from base values (already implemented)
  → OCR dot artifact removal in percentage columns
  → French numeric formatting normalization
  → Track all corrections for QCS penalty and audit trail

Step E: HITL Validation (Core Workflow)
  → Route based on validation results:
    - All VALID + high QCS → Auto-approve candidate (analyst confirms)
    - Any ERROR/CRITICAL → Mandatory human review with flagged cells highlighted
    - WARNINGs only → Expedited review (show warnings, pre-approve if overridden)
  → Display source document + extracted data side-by-side
  → Editable fields via st.data_editor
  → Show validation results with override capability
  → Workflow: Draft → Validated → Archived
  → Store: validated data + flags + scores + corrections + document references
```

> **Design note**: Cross-statement validation (Tier V2) is the highest-impact gap in the current implementation. It requires the pipeline to process all tables from a single document as a unit, which depends on Step A segmentation. This is prioritized over VLM integration for the MVP because banking users need assurance that Bilan, CR, and TFT are mutually consistent.

### 5.4 Success Criteria and Deliverables

| Criterion | Target | Rationale |
|-----------|--------|-----------|
| **Automation Rate** | ≥70% | Significant cost reduction; human review for residual high-risk pages |
| **Critical Error Rate** | <2% on names/amounts/IDs | Acceptable risk threshold for administrative workflows |
| **Median Latency** | <2s/page (Tiers 0–1) | Acceptable for batch and interactive workflows |
| **GPU Cost** | <$0.02/page | Competitive with cloud APIs; clear TCO advantage |
| **Core Extraction Accuracy** | Improvement of ≥10% vs. best single model | Ensemble ROI justifies operational complexity |

**Final Deliverables**:

1. **Benchmark Report**: Per-domain model rankings, cost curves, recommended QCS thresholds
2. **PoC Implementation**: Docker-ized pipeline with config for model selection, thresholds, hardware
3. **Production Roadmap**:
   - Hardware sizing (1–2× A100/H100 for batch; 1× RTX 3090 for dev)
   - Operational playbook: threshold retraining, schema updates, new model integration, QCS monitoring
   - Economic analysis: TCO vs. cloud APIs ($0.0015–$0.05/page cloud vs. ~$0.01–0.02/page self-hosted)

---

## 6. Hardware and Infrastructure Recommendations

### 6.1 GPU Selection

| Use Case | Recommended Hardware | Rationale |
|----------|---------------------|-----------|
| **Development/PoC** | 1× NVIDIA RTX 4090 (24GB) or RTX 3090 (24GB) | Excellent for R&D; supports quantized 7B models; moderate batch processing |
| **Production Batch** | 1–2× A100 (40GB) or H100 (80GB) | Enterprise throughput; high parallelism; FP8 support for new transformers |
| **Edge/Mobile** | Quantized models on RTX 4060 (8GB) or consumer GPUs | For edge deployment or post-processing |

### 6.2 Optimization Strategies

- **Quantization**: 4-bit (bitsandbytes) reduces Qwen2.5-VL-7B from 14GB to ~6GB FP16 VRAM
- **Flash Attention 2**: 2–3× speedup for long-context processing on Ampere+ GPUs
- **Batch Processing**: Dynamic batching (small batches for detection, large for recognition) maximizes throughput
- **Async/Parallel**: Run Tier 1 fast OCR in parallel; reserve Tier 2 VLMs for sequential high-value pages

---

## 7. Implementation Timeline

| Phase | Duration | Key Outputs |
|-------|----------|------------|
| **Phase 0** | 2–3 weeks | Labeled corpus, NCT reference schemas, validation rule set (JSON), calibration dataset |
| **Phase 1** | 4–6 weeks | Benchmark matrix, per-model cost/accuracy profile |
| **Phase 2** | 4–6 weeks | Cascading architecture performance; recommended thresholds |
| **Phase 3** | 2–4 weeks | Ensemble ROI analysis; final model pairings |
| **PoC Build** | 3–4 weeks | Docker-ized pipeline, integration tests, validation on holdout set |
| **Production Prep** | 2–3 weeks | Operational playbook, hardware procurement, deployment guide |

**Total**: 17–26 weeks

---

## 8. Risk Mitigation and Contingencies

| Risk | Mitigation |
|------|-----------|
| **VLM hallucinations** on structured data | LLM-as-Judge consistency checks; Tier 3 ensemble voting; schema validation |
| **Threshold brittleness** | Calibrate on stratified validation set; monitor QCS distribution drift in production |
| **Hardware bottlenecks** | Test quantization and batch-size strategies early; plan for elastic scaling |
| **Model availability** | Maintain Tier 1/2 fallbacks; containerized setup allows easy model swaps |
| **Annotation effort** | Start with 10–15% ground truth; use stratified sampling to maximize coverage |
| **Cross-statement inconsistency** | Tier V2 validation catches Résultat Net and Trésorerie mismatches between statements; requires reliable statement segmentation (Step A) as prerequisite |
| **Magnitude/unit OCR errors** | Tier V3 anomaly detection flags order-of-magnitude mismatches (e.g., actif in millions, passif in thousands); NCT standard note: documents may use dinars or milliers de dinars (NCG § 20) |
| **Standard-specific rule drift** | Configurable JSON rule engine (§2.4.2) allows per-standard activation and threshold tuning without code changes; new standards added as rule files |

---

## 9. Conclusion

This experimentation plan provides a data-driven roadmap to build a production-ready, self-hosted OCR system optimized for administrative archives. By combining native extraction, cascading fast-fail pipelines, and ensemble methods backed by a robust reference-free QCS framework, the system achieves enterprise-grade accuracy while maintaining cost efficiency and operational simplicity.

The phased approach—from corpus preparation through individual benchmarking, cascading validation, ensemble optimization, and final PoC—ensures rigorous evaluation and clear decision points for production deployment.

---

## Appendix: Quick Reference

### Key Metrics Formulas

**Low-Confidence Density (LCD)**:
$$LCD = \frac{|\{w \in W : \text{Conf}(w) < T\}|}{|W|}$$

**Normalized Sequence Log-Probability**:
$$\text{LogProb}_{\text{seq}} = \frac{1}{n} \sum_{i=1}^{n} \log P(t_i | t_{<i}, \text{Image})$$

**Quality Confidence Score (QCS)**:
$$QCS = 0.4 \cdot s_i + 0.3 \cdot s_s + 0.3 \cdot s_m$$

where \( s_i, s_s, s_m \in [0,1] \) and normalized to [0,1].

### Python Libraries and Tools

```
OCR/VDU:
  - PyMuPDF, pdfplumber, unstructured.io
  - surya-ocr, marker-pdf, docling, pytesseract
  - transformers, vLLM, DeepSeek-OCR, Qwen2.5-VL

Evaluation:
  - gibberish-detector, pandera, llama-cpp-python
  - ensemble-boxes, difflib, biopython

Output:
  - markdown, json, pandas
```

---

**Document Version**: 3.0
**Last Updated**: March 2026
**Author**: Technical Research Team
**Status**: Phase 0 In Progress
**Revision**: v3.0 — Integrated domain validation rules (cross-statement coherence, VNC, anomaly detection, configurable rule engine) from Documentation/ resources; NCT standard reference for schema calibration
