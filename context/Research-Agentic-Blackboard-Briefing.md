# Technical Briefing: Agentic Frameworks for High-Integrity Financial Statement Extraction and Validation

### 1. Architectural Foundation: The Agentic Blackboard Framework

The extraction of high-stakes financial data necessitates an architecture that can reconcile the extreme variability of document layouts. Engineering must mandate a **Blackboard Framework**—a model originally conceived in the 1980s for speech recognition—to facilitate collaboration between autonomous agents. This framework utilizes a central Resource Description Framework (RDF) blackboard as a common communication protocol, allowing diverse "Knowledge Sources" (agents) to contribute iteratively to the table analysis task.

A key innovation in this architecture is the **suitability of knowledge representation**, which allows agents to share partial hypotheses on the blackboard. To extract structured table boundary lines from the complex RDF graph, the system utilizes **SPARQL queries** to select and refine the physical coordinates of the data.

**Strategic Benefits of the Blackboard Framework:**
*   **Partial Knowledge Participation:** Specialized agents can contribute to the global solution even if they only possess narrow expertise (e.g., identifying ruling lines or character strings).
*   **Diverse Programming Paradigms:** The framework integrates deep learning models for layout detection with symbolic, rule-based systems for arithmetic verification.
*   **Architectural Flexibility:** Knowledge sources can be added or removed without reconfiguring the core system, allowing for rapid experimentation with new extraction techniques.
*   **Knowledge Representation Suitability:** The RDF-based protocol ensures that multidimensional relationships between cells are preserved during the interpretation phase.

**The Agentic Orchestration Layer:**
In this layer, autonomous agents operate in parallel to process the document:
*   **Document Classification Agent:** Identifies document types (e.g., Balance Sheets vs. Income Statements) to apply relevant extraction schemas.
*   **Field Extraction Agent:** Utilizes Natural Language Processing (NLP) to pull text-based keys like vendor names and payment terms.
*   **Numeric String Recogniser:** Specifically identifies and normalizes numeric sequences across various separators.
*   **Arithmetic Calculation Recognizer:** Validates the mathematical relationships between cells on the blackboard.
*   **Consistency Validation Agent:** Flags internal logic mismatches across different sections of the document.

---

### 2. Advanced Arithmetic Validation and Numeric Disambiguation

To eliminate hallucinations inherent in pure neural extraction, the framework employs **Neurosymbolic Inference**. By integrating Large Language Models (LLMs) with **Symbolic Checkers**, we implement a **Triadic Feedback Loop** (based on the TEN framework). This loop provides symbolic constraints back to the LLM, forcing the system into **verifiable convergence** where the output must satisfy mathematical laws before being finalized.

**Disambiguation Logic for Financial Cells**

| Input Ambiguity | Corrective Logic |
| :--- | :--- |
| **Numeric Scale Ambiguity** | The **Numeric String Recogniser** and **Table Index Recognizers** distinguish between decimal and thousands separators by analyzing column-wide numeric consistency and logical hierarchies. |
| **Summation Identities** | Identification of operands and results through **Target Words** (e.g., Total, Gross, Net, Subtotal, Footing) to map the arithmetic structure of the table. |
| **Cross-Statement Links** | Verification of the **Balance Equation** and fundamental **Accounting Concepts** across table regions using RDF inference rules to detect conflicting statements. |

**Inference Rules and Missing Values:**
The system utilizes **Inference Rules** (leveraging RDF sub-classes and sub-properties) to derive implied values in incomplete tables. For instance, if a subtotal is missing but individual line items and a grand total are identified, the system infers the missing value based on defined accounting ontologies.

---

### 3. Human-in-the-Loop (HITL) Interface and Confidence Scoring

Financial reliability requires a robust **OCR Confidence Score** mechanism. Scores must be assigned at the character, word, and field levels, derived from font clarity, image resolution (standardized to 300–600 DPI), and pattern matching.

**Threshold-Based Gating System:**
1.  **High Confidence:** Enables **Straight-Through Processing (STP)**. Data is moved directly to the ERP/accounting system with zero human intervention.
2.  **Medium Confidence:** Triggers a targeted review within **Agentic Workspaces** or **Content WorkDesks**, where reviewers only validate specifically flagged fields.
3.  **Low Confidence/Conflict:** Activates **Conflict Resolvers** or manual re-keying protocols to resolve significant structural or data mismatches.

**UI/UX Strategy for Validation:**
Reviewers must be presented with a validation state that includes **field-level source citations**, allowing for immediate visual verification against the original document. The UI must highlight **Severity-Weighted** errors: discrepancies in the "Grand Total" or "Net Profit" are flagged with maximum visual priority, whereas minor misspellings in a vendor address are given lower weight. This prioritizes financial risk over cosmetic errors.

---

### 4. Technical Pitfalls and Mitigation Gating

Architects must account for specific failure modes using a **Failure Mode and Effects Analysis (FMEA)**:

*   **Error Propagation:** One misidentified row delimiter causes cascading "Splitting" or "Merging" errors. **Mitigation:** Mandate the **x-y cut algorithm** or **Dual Hierarchy Models** to verify structural boundaries independently of text.
*   **Over-Correction:** The risk of neurosymbolic agents "forcing" a balance in tables that are inherently incorrect in the source. **Mitigation:** Implement **Conservative Gating** that compares final hypotheses against human-verified **Ground Truth** sets.
*   **Separator Ambiguity:** Inconsistent use of "." vs "," across international vendors. **Mitigation:** Apply **Resolution Normalization**, **Adaptive Deskewing**, and mandatory **Binarization and Denoising** during pre-processing.
*   **Rounding and Intermediate Confusion:** Misidentifying subtotal footings as final totals. **Mitigation:** Use **Table Index Recognizers** to map logical hierarchies and anchor terminal values.

---

### 5. Multi-Level Evaluation and Quality Benchmarking

We move beyond simple precision and recall to the **Granular Table Layout Evaluation Method**, defining four critical error types:
1.  **Insertion:** Extracted cells that do not exist in the source.
2.  **Deletion:** Failure to capture existing source cells.
3.  **Splitting:** Fragmenting a single logical cell into multiple entries.
4.  **Merging:** Combining distinct cells into a single data point.

**Evaluation Metrics:**
*   **Character Error Rate (CER):** The percentage of characters incorrectly converted (Levenshtein distance).
*   **Word Error Rate (WER):** The percentage of words with at least one error; essential for downstream NLP pipelines.
*   **Field-Level (Semantic) Accuracy:** The **99.9% benchmark** required for critical financial fields. While high CER is tolerable in non-essential text, financial fields require near-perfection to enable the cost savings of straight-through processing.

---

### 6. Summary of Actionable Implementation Techniques

Engineering teams must utilize this checklist to ensure 99.9% system reliability:

*   **Agentic Orchestration:** Specialized agents for classification, extraction, and numeric recognition.
*   **RDF-Based Inference:** Using sub-classes/sub-properties for deriving implied values and **Accounting Concepts**.
*   **Triadic Feedback:** Iterative loops between LLMs and symbolic checkers for **verifiable convergence**.
*   **SPARQL-Based Selection:** Querying the RDF blackboard to define table boundary lines.
*   **Neurosymbolic Self-Debugging:** Utilizing symbolic feedback to correct LLM hallucinations in real-time.
*   **Binarization and Denoising:** Mandatory pre-processing to clarify character pixels for the OCR engine.
*   **Table Index Recognizers:** Mapping logical hierarchies to prevent rounding and subtotal confusion.
*   **Granular Layout Metrics:** Benchmarking via cell-level splitting and merging analysis.