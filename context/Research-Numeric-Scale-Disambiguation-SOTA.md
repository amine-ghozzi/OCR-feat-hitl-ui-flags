# Technical Framework for Numeric Scale Disambiguation in OCR Financial Tables

## 1. Statistical Inference of Table-Level Numeric Conventions

The system must establish a global "Ground Truth" flag for numeric separators at the table level to mitigate catastrophic scale errors. By analyzing unambiguous cells—defined as those containing three-digit grouping sequences or specific trailing decimals—the system assigns a document-wide convention flag.

### 1.1 Logic-Gate Diagram for Convention Detection
The following logic flow must be executed upon initial table ingestion to resolve the conflict between English and German/French typographic styles:

```text
[INPUT NUMERIC STRING]
       |
       v
[Trailing ",000"?] -- YES --> [Set Flag: GERMAN_DECIMAL]
       | NO
       v
[Trailing ".00"?]  -- YES --> [Set Flag: ENGLISH_DECIMAL]
       | NO
       v
[Contains "·" (Midpoint)?] -- YES --> [TRIGGER: Ambiguity Alert (Source 2, Fig 7)]
       | NO                            (Potential decimal in educational text)
       v
[Contains "X,XXX,XXX"?] -- YES --> [Set Flag: ENGLISH_THOUSANDS]
       | NO
       v
[Contains "X.XXX.XXX"?] -- YES --> [Set Flag: GERMAN_THOUSANDS]
       | NO
       v
[DEFAULT: INHERIT FROM PREVIOUS TABLE/LOCALE]
```

### 1.2 Table-Level Convention Breakers
The "unified scale" assumption is suspended for specific data types where consistency is typographically impossible.

| Breaker Type | Example/Context | Architectural Impact |
| :--- | :--- | :--- |
| **Mixed-Unit Rows** | Price-per-share ($10.50) vs. Market Cap ($10.5M) | Local override of Columnar Magnitude logic. |
| **Header Modifiers** | "In thousands," "In millions" | Apply $10^n$ scalar to all column floats. |
| **Ratio Columns** | Interest Rates (e.g., 0.6%) | Forced decimal logic regardless of grouping flag. |
| **Date Columns** | 12.01.2024 | EXCLUDE from table-wide scale flag analysis. |
| **Mixed Symbols** | Currency symbols ($ vs. €) | Locale-sensitive subfield parsing required. |

---

## 2. Bayesian Multi-Signal Integration & Precedence Logic

The system utilizes the **Bayesian Online Numeric Discriminant (BOND)** procedure to resolve ambiguous subfields where character geometry is insufficient for identification.

### 2.1 Mathematical Definition of the Likelihood Ratio
The architecture calculates a Confidence Score by assessing the probability that a scanned subfield is numeric vs. alphabetic. Per IBM Source 3 (Rosenbaum), the system calculates the $\phi$ (phi) ratio:

```latex
\phi = \frac{\prod_{j=1}^{k} P_{ce}(a_j | n_j)}{\prod_{j=1}^{k} P_{cc}(n_j | a_j)}
```

Where:
*   $P_{ce}(a_j | n_j)$ is the **Channel Confusion Probability** of an alpha read given a numeric character.
*   The system must use the **Numeric vs. Alpha Read percentages** from IBM Table 5 (e.g., the 92.795% confusion between '0' and 'N' or the 12.925% confusion between '1' and 'I') to weight the separator score.
*   $\phi > 1$ implies a numeric field; $\phi \leq 1$ implies an alpha field.

### 2.2 Column-wise Probability Propagation
If a single cell in a vertical vector is resolved with >99% confidence (e.g., a clearly rendered "1,250.00"), this result becomes a *prior* for the Bayesian analysis of all other cells in that vertical column. This "Vertical Redundancy" ensures that faint or poorly rendered separators in lower rows are interpreted through the lens of the established column convention.

### 2.3 Precedence Hierarchy
In the event of signal conflict, resolution follows this strict order:
1.  **Arithmetic Identity Satisfaction (Highest):** Forced correction to satisfy sums.
2.  **Columnar Magnitude Consistency:** Alignment with the column's $10^n$ average.
3.  **OCR Confidence Score (Lowest):** Raw vision engine probabilities.

---

## 3. Algorithmic Validation via Arithmetic Identities

### 3.1 Footing and Cross-Footing Verification
The system must force-correct scale interpretations by testing them against established financial identities.

**Vertical Footing:**
`IF Sum(Rows 1 to N) == Total (Separator X) AND != Total (Separator Y), ACCEPT X.`

**Horizontal Cross-Footing:**
`Quantity * Unit_Price + Tax == Line_Total`

### 3.2 Order-of-Magnitude Outlier Test
To catch misplaced thousands-separators (often misread as decimals), the system applies the following test:
`IF extracted_val < (column_avg * 0.01), TEST substitution of decimal for thousands-separator.`
If the substituted value satisfies the arithmetic total or the columnar magnitude, the correction is applied automatically.

---

## 4. Implementation Thresholds & Safety Gating

### 4.1 Automated Correction Constraints
*   **Magnitude-Outlier Cutoff:** Any cell deviating by >2 orders of magnitude from the column mean triggers mandatory manual review.
*   **Segmentation Error Trigger:** Per IBM standards, a **0.6% baseline probability** is used for digit segmentation errors. If an arithmetic identity fails, the system must check if a digit was split (e.g., '8' read as '3' and '5') or merged before flagging for human review.

### 4.2 Conservative Gating & Human-in-the-Loop (HITL)
The system is strictly forbidden from "forcing" a balance through hallucination.
*   **Rounding Tolerance:** All identities must resolve within a **0.01% tolerance**.
*   **Likelihood Margin:** If the Bayesian scores for "English" vs. "German" interpretations are within a **10% likelihood margin**, the system must halt and present the two "Best-Fit" records to a human operator for one-click resolution.

---

## 5. International Locale & Symbol Misrecognition

### 5.1 European and North African Formatting
*   **French/European Standard:** Check for **Space** as a thousands-grouping symbol. Warning: Typography Journal notes spaces can split strings into two numbers; the system must implement **Catenation during Pre-processing** to join these subfields.
*   **Tunisian Dinar Exception:** Use format mask `###,###.###`. Trailing 3-digit sequences must be prioritized as genuine decimals (millimes) rather than thousands-grouping.

### 5.2 Symbol Substitution Matrix
The following substitutions must occur before scale analysis to prevent character-count shifts:

| OCR Output | Substitutions (Pre-Scale Analysis) | Rationale |
| :--- | :--- | :--- |
| **cl** | **d** | Avoids unintended decimal shifting. |
| **rn** | **m** | Character count normalization. |
| **vv** | **w** | Character count normalization. |
| **ce** | **œ** | Prevent misread of 'e' as digit '0'. |
| **Midpoint (·)** | **.** | Educational text decimal convention. |
| **l, I** | **1** | Channel Confusion (IBM Table 5). |
| **S, Z** | **5, 2** | Geometric similarity. |

---

## 6. Implementation Summary for ERP Ingestion

### 6.1 Clean Record Requirements
The final output must be a schema-aligned JSON object. All decorative separators (grouping commas/periods) must be stripped, returning only raw floats or integers.

### 6.2 Metadata & Matching Integrity
Every numeric entry must be accompanied by the following metadata:
*   **Scale_Confirmed_Flag:** (Boolean) Set to `True` only if extracted totals match the Purchase Order (PO) or Goods Receipt (GR) within 0.01%.
*   **Source_Confidence_Vector:** An array containing the individual OCR confidence scores for every character in the string, enabling downstream risk scoring.
*   **Interpretation_Method:** (e.g., `Bayesian_BOND`, `Arithmetic_Force_Correction`, or `Manual_HITL`).