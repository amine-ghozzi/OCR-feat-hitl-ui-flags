# **Autonomous Quality Assurance and Ensemble Architectures for Local Document Extraction: A Comprehensive Technical Analysis**

## **Executive Summary**

The extraction of structured intelligence from unstructured Portable Document Format (PDF) files constitutes a foundational challenge in modern data engineering. As organizations increasingly deploy Retrieval-Augmented Generation (RAG) systems and automated administrative workflows, the reliance on Optical Character Recognition (OCR) and Document Layout Analysis (DLA) has intensified. However, a critical operational gap persists: the inability to measure extraction quality in real-time production environments where ground truth references are absent. Unlike research benchmarks that rely on Character Error Rate (CER) and Word Error Rate (WER) against human-annotated gold standards, production pipelines operate "blindly," necessitating the development of robust, reference-free performance metrics.

This report provides an exhaustive technical analysis of unsupervised evaluation methodologies and ensemble extraction techniques tailored for local, privacy-preserving infrastructure. It establishes that relying on a single metric—such as the confidence score from a standalone OCR engine—is statistically insufficient for enterprise-grade accuracy. Instead, a multidimensional "Quality Confidence Score" (QCS) must be engineered, synthesizing intrinsic model uncertainty, statistical text properties, and semantic coherence signals.

Furthermore, this analysis demonstrates that the "No Free Lunch" theorem applies strictly to document extraction; no single model excels across all document modalities. Consequently, this report details the implementation of ensemble architectures using algorithms such as ROVER (Recognizer Output Voting Error Reduction) for textual consensus and Weighted Boxes Fusion (WBF) for layout consolidation. By combining the outputs of diverse engines—ranging from classical Tesseract to modern Vision-Language Models (VLMs) like Docling and Surya—via algorithmic voting, practitioners can achieve performance superior to any individual state-of-the-art model.

The following sections rigorously deconstruct the mathematical foundations, software implementations, and architectural designs required to build self-correcting, high-confidence local extraction pipelines using the Python ecosystem.

## ---

**1\. The Operational Challenge of Blind Extraction**

### **1.1 The Evaluation Gap in Production Systems**

In controlled research environments, the performance of OCR systems is quantified using Levenshtein distance-based metrics. The Character Error Rate (CER) and Word Error Rate (WER) provide precise, deterministic measures of how many insertions, deletions, or substitutions are required to transform the machine output into the human-verified reference text.1 These metrics are invaluable for model selection and benchmarking.

However, in a live production environment—processing thousands of invoices, medical records, or legal contracts daily—the reference text does not exist. This absence creates the "Evaluation Gap." Without a ground truth, standard metrics like WER are mathematically incomputable. This presents a severe risk: if an OCR engine misreads a financial figure or a patient's dosage, the error may propagate downstream silently. The objective of production engineering, therefore, shifts from *measuring exact accuracy* to *estimating the probability of correctness*.4

### **1.2 The Necessity of Local Processing**

While cloud-based solutions (e.g., Google Document AI, Amazon Textract) utilize massive, proprietary models to achieve high accuracy, they pose significant challenges regarding data privacy, latency, and cost. For industries regulated by frameworks such as GDPR, HIPAA, or strict corporate espionage protections, transmitting sensitive documents to external APIs is often prohibited. This necessitates **Local Extraction**—running models on on-premise hardware or within private clouds.6

Local extraction introduces a resource constraint. Unlike cloud APIs that can spin up effectively infinite compute for ensemble voting, local pipelines must balance accuracy with available GPU VRAM and CPU cycles. This constraint makes efficient, auto-calculable performance metrics even more critical; the system must determine when a "cheap" model (like Tesseract) has failed and when to escalate to an "expensive" model (like a VLM), a strategy known as Cascading Architecture.8

### **1.3 A Taxonomy of Reference-Free Metrics**

To bridge the Evaluation Gap, we must construct a proxy for quality using available signals. These signals can be categorized into three distinct layers of abstraction, ranging from the raw numerical outputs of the neural networks to the high-level semantic understanding of the extracted content.

| Metric Layer | Source of Signal | Nature of Evaluation | Computational Cost | Primary Use Case |
| :---- | :---- | :---- | :---- | :---- |
| **Intrinsic (Probabilistic)** | The OCR Engine (Logits/Conf) | Model Uncertainty | Low | Filtering low-confidence characters/words. |
| **Statistical (Heuristic)** | The Output Text (Characters) | Syntax & Structure | Low-Medium | Detecting gibberish, noise, and schema violations. |
| **Semantic (Linguistic)** | Language Models (SLM/LLM) | Meaning & Fluency | High | Detecting hallucinations and incoherence. |

By aggregating these uncorrelated signals, a composite score can be derived that correlates strongly with human judgment.3

## ---

**2\. Intrinsic Evaluation: The Physics of Model Uncertainty**

The first line of defense in any extraction pipeline is the model's own self-assessment. Whether the system utilizes a classical pattern-matching engine or a deep transformer-based architecture, the extraction process involves assigning probabilities to potential character sequences. Accessing and interpreting these probabilities provides the foundational "Intrinsic" metrics.

### **2.1 Classical OCR Confidence: Tesseract and Hidden Markov Models**

Legacy engines like Tesseract (v3 and v4/v5 LSTM) operate on a character-segmentation basis. When the engine processes a glyph, it calculates a distance metric between the observed pixel pattern and its learned prototypes.

The Confidence Score Mechanism:  
Tesseract returns a confidence value (0-100) for each recognized unit. In the Python ecosystem, this is accessed via pytesseract.image\_to\_data, which exposes the detailed metadata for every token, rather than the simplified string output of image\_to\_string.10

* **Granularity:** The confidence is available at the word level.  
* **Calibration Issues:** A well-documented limitation of Tesseract is its tendency to be "confidently wrong" on clean, printed text that happens to resemble valid but incorrect characters (e.g., confusing l (lowercase L) with 1 (one)). Conversely, it may assign low confidence to correctly read degraded text.  
* Metric Implementation: A robust metric is the Low-Confidence Density (LCD). Instead of averaging the confidence of the entire document (which can mask specific critical failures), one should calculate the percentage of words falling below a strict threshold (e.g., 80%).

  $$LCD \= \\frac{\\sum\_{w \\in W} \\mathbb{1}(Conf(w) \< T)}{|W|}$$

  Where $W$ is the set of all words and $T$ is the threshold.10

### **2.2 Transformer-Based Confidence: Logits and Softmax**

Modern extraction engines, including **Surya**, **Docling**, and **Got-OCR2.0**, utilize Transformer architectures. These models process the image (via a Vision Encoder) and generate text autoregressively.

Log-Probabilities (Logprobs):  
In a Transformer, the generation of a token $t$ is governed by a probability distribution over the entire vocabulary $V$. The raw output of the neural network before normalization is the logit vector. Applying the Softmax function yields probabilities. The logarithm of this probability is the logprob.

* **Why Logprobs Matter:** The logprob represents the model's uncertainty in a mathematically rigorous way. A value close to 0 (logprob close to 0\) implies near certainty. A highly negative value implies the model was guessing.11  
* Sequence-Level Confidence: For a generated string sequence $S \= \\{t\_1, t\_2,..., t\_n\\}$, the joint probability is the product of individual token probabilities. In log-space, this becomes a sum:

  $$Score(S) \= \\frac{1}{n} \\sum\_{i=1}^{n} \\log P(t\_i | t\_{\<i}, Image)$$

  This normalized log-probability serves as the primary intrinsic metric for Generative OCR. If this score drops below a calibrated threshold (e.g., \-0.5), the extraction is likely hallucinated or the text is illegible.12

**Tool-Specific Implementations:**

* **Surya OCR:** This library provides a specific confidence field in its JSON output for each text line.14 Independent benchmarks suggest Surya's confidence is better calibrated for "reading order" quality than Tesseract's.15  
* **MinerU / PDF-Extract-Kit:** This toolkit segments the document into functional blocks (Text, Formula, Table). It provides distinct confidence scores for each. For instance, a latex score is provided for mathematical formulas, which is critical for academic PDF extraction where standard OCR fails.16  
* **Docling (IBM):** Docling takes a holistic approach, providing an ocr\_score (text quality) and a layout\_score. The layout\_score is particularly vital for multi-column documents; a document might have perfect text recognition but fail completely if the reading order merges two independent columns into a single incoherent paragraph. The layout\_score quantifies the confidence in the segmentation bounding boxes.18

### **2.3 The Calibration Problem**

A major challenge in local extraction is that confidence scores are not standardized across engines. A "90" from Tesseract does not mean the same probability of correctness as a "0.9" from Surya.

* **Normalization:** When building a system that uses multiple engines, scores must be normalized. A common technique is **Platt Scaling** or simple Min-Max normalization based on a validation set, mapping all engine outputs to a pseudo-probability space.  
* **Recommendation:** Do not compare raw scores directly. Instead, define "Pass/Fail" thresholds specific to each engine and use those binary signals for downstream logic.

## ---

**3\. Statistical and Heuristic Signal Processing**

When intrinsic metrics fail (e.g., a model is hallucinating confidently), statistical analysis of the output text serves as an unbiased validator. These metrics treat the extracted text purely as a data stream, checking for conformity to the statistical laws of the target language.

### **3.1 Information-Theoretic Metrics: Entropy and Gibberish**

OCR errors often manifest as "gibberish"—sequences of characters that violate the expected distribution of the language (e.g., ^&%sdj or th5\_ n@me).

Markov Chain Transition Probabilities:  
Language is not random; the probability of the letter 'h' following 't' is high, while 'h' following 'q' is near zero. A Markov model can be trained on a clean corpus (e.g., Project Gutenberg or Wikipedia) to learn these transition probabilities (Bigrams or Trigrams).

* **Mechanism:** For an extracted string, we calculate the average probability of every character transition. If the average probability is extremely low, the string is statistically unlikely to be valid text.20  
* **Library Support:** The Python library gibberish-detector implements this logic. It allows users to train a model on a big.txt corpus and then query any string for a gibberish score. This is computationally inexpensive and highly effective for filtering noise caused by image artifacts (e.g., specks of dust recognized as punctuation).21

Shannon Entropy:  
Entropy ($H$) measures the unpredictability of information content.

$$H(X) \= \- \\sum\_{i} P(x\_i) \\log\_2 P(x\_i)$$

* **Application:** Garbled OCR output (random characters) typically has higher entropy than structured language. Conversely, extremely repetitive noise (e.g., eeeeeeee) has very low entropy. By defining an "Entropy Safe Range" based on valid documents, outliers can be flagged.23

### **3.2 Lexical Density and Dictionary Coverage**

For documents containing standard prose (newsletters, contracts), the ratio of valid words to total words is a powerful proxy for quality.

* Implementation: Using a hashed dictionary (Bloom Filter or Python set), the system checks every extracted token.

  $$Density \= \\frac{|W\_{valid}|}{|W\_{total}|}$$  
* **Domain Adaptation:** For technical documents, a standard English dictionary is insufficient. It must be augmented with domain-specific terms (e.g., medical ontology for clinical trials, legal lexicon for contracts) to prevent false positives.21

### **3.3 Regular Expression (Regex) Discovery and Validation**

Administrative documents (invoices, tax forms, ID cards) are defined by their structure. They contain entities like Dates, Social Security Numbers, IBANs, and Currency amounts.

* **Automated Regex Discovery (RED):** Advanced systems can "learn" regular expressions from a seed dataset of high-quality extractions. The RED algorithm can identify that a specific document type always contains a pattern like \[A-Z\]{3}-\\d{4}.24  
* **Pattern Match Rate:** If a document is classified as an "Invoice," the system anticipates the presence of Date (\\d{2}/\\d{2}/\\d{4}) and Currency (\\$\\d+\\.\\d{2}) patterns. A metric can be defined as the *density* of these expected patterns. If an "Invoice" yields zero date patterns, the OCR has likely failed to resolve the specific region containing the header.25

### **3.4 Dataframe Schema Validation**

For table extraction—one of the hardest OCR tasks—statistical validation of the columns is the strongest available metric.

* **The Schema Contract:** Tools like **Pandera** or **Great Expectations** allow engineers to define a strict schema for a DataFrame. For example: "Column 1 must be a String, Column 2 must be a DateTime, Column 3 must be a Float greater than 0".27  
* **Coercion Failure Rate:** When the extracted table is loaded into a Pandas DataFrame, the validation library attempts to coerce the types. The percentage of rows that fail this coercion (e.g., a cell in the 'Amount' column containing the string 'O.00' instead of the number '0.00') is a direct, deterministic quality score.29  
* **Integration:** This requires no LLM and no ground truth. It purely validates internal consistency.

## ---

**4\. Semantic Intelligence: The Role of Local LLMs**

Statistical metrics capture *syntax* errors but often miss *semantic* errors. For example, "The bill is due on May 1st" and "The hill is due on May 1st" are both grammatically plausible and pass spell-check, but the latter is a hallucination or error. To detect this, we require models that understand context.

### **4.1 Perplexity (PPL) as a Fluency Proxy**

Perplexity is a measurement of how "surprised" a language model is by a sequence of text. It is the exponentiated average negative log-likelihood of a sequence.31

$$PPL(X) \= \\exp \\left( \-\\frac{1}{t} \\sum\_{i=1}^{t} \\log P(w\_i | w\_{\<i}) \\right)$$

* **Interpretation:** A lower perplexity indicates the text is fluent and predictable to the model. OCR errors typically result in unnatural token sequences (e.g., "th3 c@t"), which cause probability spikes and thus high perplexity.32  
* **Local Implementation:** Using **llama.cpp** or **Hugging Face Transformers**, one can load a quantized Small Language Model (SLM) like Llama-3-8B-Instruct. The extracted text is fed to the model to compute the loss.  
* **Ranking Mechanism:** Perplexity is most effective as a *relative* metric. If three OCR engines process the same paragraph, the version with the lowest perplexity is statistically the most coherent and likely the most accurate.13

### **4.2 The "LLM-as-a-Judge" Paradigm**

Instead of asking an LLM to perform the extraction (which is prone to hallucination), we can use the LLM to *evaluate* the extraction performed by a dedicated OCR engine.34

* **Prompt Engineering:** The LLM is provided with the OCR text and a rubric.  
  * *Prompt:* "You are a QA specialist. Rate the following text for coherence, legibility, and formatting on a scale of 1-5. Return the score and a list of potential OCR errors (e.g., 'rn' misread as 'm').".34  
* **Hallucination Detection via Consistency:**  
  * *Method:* Run a cheap OCR (Tesseract) and a powerful VLM (Qwen2-VL) on the same document.  
  * *Judgment:* Ask the LLM judge: "Does the VLM output roughly align with the keywords found by Tesseract?" If the VLM generates a fluent paragraph that shares zero keywords with the Tesseract output, the VLM is likely hallucinating.37  
* **Self-Consistency Checks:** For quantitative documents, the LLM can be prompted to verify math. "Extract the line items and the total. Sum the line items. Does the calculated sum match the extracted total?" A discrepancy indicates an extraction error in one of the fields.39

## ---

**5\. Structural Integrity and Layout Preservation**

Documents are not merely streams of characters; they rely on spatial layout to convey meaning. A failure to preserve layout (e.g., reading across two columns as one line) is a catastrophic failure, even if every character is recognized correctly.

### **5.1 Unsupervised Layout Metrics**

* **Semantic Coherence Score (SCS):** This metric evaluates whether the reading order predicted by the layout engine results in semantically adjacent sentences. It can be estimated by calculating the Perplexity of the text in the extracted order versus alternative permutation orderings. A sharp drop in perplexity in the chosen order confirms the layout is correct.9  
* **Region Entropy Divergence (RED):** This compares the entropy distribution of the extracted layout against a canonical document template. If the entropy "heat map" of the extraction deviates significantly from the expected template (e.g., a dense block of text appears where a header should be), it signals a layout analysis failure.9

### **5.2 Table Structure Validation**

Tables are particularly vulnerable to layout errors.

* **Row/Column Consistency:** A robust metric is the variance in row length. In a well-extracted table, most rows should have the same number of columns. High variance suggests the OCR engine missed cell delimiters.  
* **Header Detection:** The presence of a distinct header row (often bolded or separated by lines) is a quality indicator. Tools like **Docling** specifically score the confidence of table structure detection.19

## ---

**6\. Algorithmic Consensus: The ROVER Methodology**

The "No Free Lunch" theorem holds true for OCR: Tesseract excels at clean printed text; EasyOCR handles scene text; VLMs handle complex layouts. No single model wins every time. **Ensemble Extraction**—running multiple models and combining their outputs—is the gold standard for maximizing accuracy.40

### **6.1 ROVER (Recognizer Output Voting Error Reduction)**

Originally developed for Automatic Speech Recognition (ASR) at NIST, ROVER is the premier algorithm for combining noisy text sequences.41 The process involves two critical steps: **Alignment** and **Voting**.

#### **6.1.1 The Alignment Challenge**

You cannot simply vote on "Word 1" from Model A and "Word 1" from Model B, because Model A might have missed a word, shifting the entire sequence. The sequences must be elastically aligned.

* **Needleman-Wunsch Algorithm:** This dynamic programming algorithm computes the optimal global alignment between two sequences by inserting "gaps" to maximize the alignment score.  
* **Multiple Sequence Alignment (MSA):** For more than two models, MSA algorithms (derived from bioinformatics for DNA alignment) are used.  
* **Python Implementation:** The difflib.SequenceMatcher class or the **BioPython** library can be repurposed to align text strings. By treating words as "amino acids," these libraries generate the optimal alignment matrix.43

#### **6.1.2 Voting Mechanisms**

Once aligned into a Word Transition Network (WTN), the system iterates through each "slot" to determine the final word.

* **Majority Voting:** The simplest approach. If 2 out of 3 models say "Invoice", the output is "Invoice".46  
* Confidence-Weighted Voting: If the engines provide confidence scores (see Section 2), the vote is weighted.

  $$Score(w) \= \\sum\_{i=1}^{N} \\alpha\_i \\cdot Conf\_i(w)$$

  Here, $\\alpha\_i$ is a static reliability weight for the engine (e.g., we might trust Docling 20% more than Tesseract). The token with the highest summed confidence wins.46

### **6.2 Spatial Consensus: Weighted Boxes Fusion (WBF)**

For Layout Analysis (Object Detection), the ensemble must combine bounding boxes (e.g., "Where is the table?").

* **The Problem with NMS:** Standard Non-Maximum Suppression (NMS) discards overlapping boxes, keeping only the one with the highest confidence. This throws away valuable information from the other models.  
* Weighted Boxes Fusion (WBF): This algorithm averages the coordinates of overlapping boxes from different models, weighted by their confidence.

  $$Coord\_{final} \= \\frac{\\sum C\_i \\cdot Coord\_i}{\\sum C\_i}$$

  This results in a "fused" bounding box that is statistically likely to be more precise than any single model's prediction.48  
* **Application:** WBF is critical for accurately cropping tables and figures before passing them to specialized OCR engines.

## ---

**7\. System Architecture: The Cascading Pipeline**

Implementing ensembles (running 3+ models) is computationally expensive. To balance accuracy with efficiency (latency/cost), a **Cascading** or **Fast-Fail** architecture is recommended.8

### **7.1 The Fast-Slow Pipeline Design**

The system routes documents through tiers of increasing complexity based on the **Quality Confidence Score (QCS)** derived from the metrics discussed in Sections 2-4.

1. **Stage 1 (Fast Tier):**  
   * **Engine:** Lightweight, fast OCR (e.g., Tesseract or a quantized Got-OCR2.0).  
   * **Evaluation:** Calculate QCS using intrinsic confidence, dictionary density, and regex matching.  
   * **Gate logic:**  
     * If $QCS \> Threshold\_{High}$ (e.g., 0.95): **Auto-Accept**.  
     * If $QCS \< Threshold\_{High}$: **Proceed to Stage 2**.  
2. **Stage 2 (Slow/Accurate Tier):**  
   * **Engine:** Heavy VLM (e.g., Docling or Qwen2-VL) or a specialized proprietary model.  
   * **Evaluation:** Recalculate QCS. Check specifically for Hallucination (using Tesseract comparison).  
   * **Gate logic:**  
     * If $QCS \> Threshold\_{Med}$: **Accept**.  
     * If $QCS \< Threshold\_{Med}$: **Proceed to Stage 3**.  
3. **Stage 3 (Ensemble Tier \- "The Nuclear Option"):**  
   * **Engine:** Run *all* available engines (ROVER method).  
   * **Evaluation:** Use the consensus output.  
   * **Final Gate:** If the consensus is still low confidence, flag for **Human-in-the-Loop (HITL)** review.50

This architecture ensures that expensive compute (Ensembles/VLMs) is reserved only for the "long tail" of difficult documents, while clean invoices pass through the cheap tier instantly.

## ---

**8\. Technical Implementation Guide: The Python Ecosystem**

To implement these architectures locally, the Python ecosystem offers a robust suite of open-source tools.

### **8.1 Recommended Library Stack**

| Component | Library | Function |
| :---- | :---- | :---- |
| **OCR Engine A** | pytesseract | Fast, character-level confidence extraction.10 |
| **OCR Engine B** | surya-ocr | Accurate, transformer-based line-level confidence.14 |
| **OCR Engine C** | docling | Specialized table/layout parsing with IBM's Granite model.19 |
| **Schema Validator** | pandera | Dataframe schema definition and statistical type checking.28 |
| **Gibberish Detection** | gibberish-detector | Markov chain analysis for noise filtering.21 |
| **Sequence Alignment** | difflib / biopython | Implementing ROVER alignment logic.44 |
| **Local LLM** | llama-cpp-python | Running quantized models for Perplexity and Judging.33 |
| **Layout Ensemble** | ensemble-boxes | Implementation of Weighted Boxes Fusion (WBF).52 |

### **8.2 Implementing the Hybrid Quality Score**

The final output of the evaluation module should be a scalar score used for routing.

Python

def calculate\_qcs(ocr\_result, text\_content):  
    \# 1\. Intrinsic Score (Normalized 0-1)  
    intrinsic \= ocr\_result\['average\_confidence'\] / 100.0  
      
    \# 2\. Statistical Score (Gibberish check)  
    gibberish\_prob \= gibberish\_detector.estimate(text\_content)  
    statistical \= 1.0 \- gibberish\_prob  
      
    \# 3\. Semantic Score (Perplexity) \- Inverted and Normalized  
    ppl \= calculate\_perplexity(text\_content, local\_llm)  
    semantic \= 1.0 / (1.0 \+ math.log(ppl)) \# Dampening high PPL  
      
    \# Weighted Sum  
    QCS \= (0.4 \* intrinsic) \+ (0.3 \* statistical) \+ (0.3 \* semantic)  
    return QCS

### **8.3 Implementing ROVER Alignment**

A simplified Pythonic implementation using difflib:

Python

from difflib import SequenceMatcher

def align\_and\_vote(text\_a, text\_b, conf\_a, conf\_b):  
    matcher \= SequenceMatcher(None, text\_a, text\_b)  
    final\_text \=  
      
    for tag, i1, i2, j1, j2 in matcher.get\_opcodes():  
        if tag \== 'equal':  
            final\_text.append(text\_a\[i1:i2\])  
        elif tag \== 'replace':  
            \# Vote based on confidence  
            segment\_a \= text\_a\[i1:i2\]  
            segment\_b \= text\_b\[j1:j2\]  
            \# Assumes we have token-level confidence mapping  
            if avg(conf\_a\[i1:i2\]) \> avg(conf\_b\[j1:j2\]):  
                final\_text.append(segment\_a)  
            else:  
                final\_text.append(segment\_b)  
        \# Handle insert/delete similarly...  
    return "".join(final\_text)

## ---

**9\. Conclusion**

The transition from research-grade OCR to production-grade Document Intelligence requires a fundamental shift in how quality is defined. In the absence of ground truth, "Accuracy" ceases to be a deterministic measurement and becomes a probabilistic confidence interval.

This report has demonstrated that **Reference-Free Evaluation** is not only possible but can be made highly robust by stacking uncorrelated signals: intrinsic model logits, statistical text properties, and semantic language model judgments. Furthermore, the adoption of **Ensemble Architectures** (ROVER, WBF) and **Cascading Pipelines** allows local systems to achieve state-of-the-art performance by mitigating the individual weaknesses of specific models. By implementing the algorithms and architectures detailed herein, organizations can deploy autonomous, self-correcting extraction pipelines that adhere to strict data privacy and efficiency mandates.

#### **Sources des citations**

1. CTRLEval: An Unsupervised Reference-Free Metric for Evaluating Controlled Text Generation \- ACL Anthology, consulté le janvier 8, 2026, [https://aclanthology.org/2022.acl-long.164.pdf](https://aclanthology.org/2022.acl-long.164.pdf)  
2. Reference-Based Post-OCR Processing with LLM for Precise Diacritic Text in Historical Document Recognition \- arXiv, consulté le janvier 8, 2026, [https://arxiv.org/html/2410.13305v3](https://arxiv.org/html/2410.13305v3)  
3. 2025 Guide to OCR Accuracy: Choosing the Right API for Your Business \- Mindee, consulté le janvier 8, 2026, [https://www.mindee.com/blog/ocr-accuracy-choosing-right-api](https://www.mindee.com/blog/ocr-accuracy-choosing-right-api)  
4. CTRLEval: An Unsupervised Reference-Free Metric for Evaluating Controlled Text Generation \- ACL Anthology, consulté le janvier 8, 2026, [https://aclanthology.org/2022.acl-long.164/](https://aclanthology.org/2022.acl-long.164/)  
5. Unsupervised Reference-Free Summary Quality Evaluation via Contrastive Learning, consulté le janvier 8, 2026, [https://aclanthology.org/2020.emnlp-main.294/](https://aclanthology.org/2020.emnlp-main.294/)  
6. Building an Open Source Perplexity AI with Open Source LLMs : r/LocalLLaMA \- Reddit, consulté le janvier 8, 2026, [https://www.reddit.com/r/LocalLLaMA/comments/1dj7mkq/building\_an\_open\_source\_perplexity\_ai\_with\_open/](https://www.reddit.com/r/LocalLLaMA/comments/1dj7mkq/building_an_open_source_perplexity_ai_with_open/)  
7. llama.cpp/tools/perplexity/README.md at master · ggml-org/llama.cpp · GitHub, consulté le janvier 8, 2026, [https://github.com/ggml-org/llama.cpp/blob/master/tools/perplexity/README.md](https://github.com/ggml-org/llama.cpp/blob/master/tools/perplexity/README.md)  
8. Combining Multiple Classifiers for Faster Optical Character Recognition \- Microsoft, consulté le janvier 8, 2026, [https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/chellapilla\_das06.pdf](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/chellapilla_das06.pdf)  
9. Layout-Aware OCR in Black Digital Archives: An Unsupervised Evaluation Approach \- arXiv, consulté le janvier 8, 2026, [https://arxiv.org/html/2509.13236](https://arxiv.org/html/2509.13236)  
10. Confidence in KTP-OCR using Pytesseract | by Firhanmaulanarusli \- Medium, consulté le janvier 8, 2026, [https://medium.com/@firhanmaulanarusli/confidence-in-ktp-ocr-using-pytesseract-46c870f314b7](https://medium.com/@firhanmaulanarusli/confidence-in-ktp-ocr-using-pytesseract-46c870f314b7)  
11. Extracting Log Probabilities for Tokens | CodeSignal Learn, consulté le janvier 8, 2026, [https://codesignal.com/learn/courses/advanced-scoring-techniques-for-llms/lessons/extracting-log-probabilities-for-tokens](https://codesignal.com/learn/courses/advanced-scoring-techniques-for-llms/lessons/extracting-log-probabilities-for-tokens)  
12. Getting Started with Logprobs \- Together.ai Docs, consulté le janvier 8, 2026, [https://docs.together.ai/docs/logprobs](https://docs.together.ai/docs/logprobs)  
13. Text Reordering & Perplexity Calculation LLM Model \- Kaggle, consulté le janvier 8, 2026, [https://www.kaggle.com/code/shahzaibmalik44/text-reordering-perplexity-calculation-llm-model](https://www.kaggle.com/code/shahzaibmalik44/text-reordering-perplexity-calculation-llm-model)  
14. surya-ocr \- PyPI, consulté le janvier 8, 2026, [https://pypi.org/project/surya-ocr/0.3.0/](https://pypi.org/project/surya-ocr/0.3.0/)  
15. datalab-to/surya: OCR, layout analysis, reading order, table recognition in 90+ languages \- GitHub, consulté le janvier 8, 2026, [https://github.com/datalab-to/surya](https://github.com/datalab-to/surya)  
16. Output File Format \- MinerU, consulté le janvier 8, 2026, [https://opendatalab.github.io/MinerU/reference/output\_files/](https://opendatalab.github.io/MinerU/reference/output_files/)  
17. Document Content Extraction Project \- pdf-extract-kit \- Read the Docs, consulté le janvier 8, 2026, [https://pdf-extract-kit.readthedocs.io/en/latest/project/pdf\_extract.html](https://pdf-extract-kit.readthedocs.io/en/latest/project/pdf_extract.html)  
18. Confidence Scores \- Docling \- GitHub Pages, consulté le janvier 8, 2026, [https://docling-project.github.io/docling/concepts/confidence\_scores/](https://docling-project.github.io/docling/concepts/confidence_scores/)  
19. IBM Granite-Docling: End-to-end document understanding with one tiny model, consulté le janvier 8, 2026, [https://www.ibm.com/new/announcements/granite-docling-end-to-end-document-conversion](https://www.ibm.com/new/announcements/granite-docling-end-to-end-document-conversion)  
20. Gibberish Text Detector in Python | by Nishant Sushmakar | Medium, consulté le janvier 8, 2026, [https://medium.com/@nishantsushmakar/gibberish-text-detector-in-python-291a221a0ffa](https://medium.com/@nishantsushmakar/gibberish-text-detector-in-python-291a221a0ffa)  
21. 5 Lesser-Known Python Libraries for Your Next NLP Project | Towards Data Science, consulté le janvier 8, 2026, [https://towardsdatascience.com/5-lesser-known-python-libraries-for-your-next-nlp-project-ff13fc652553/](https://towardsdatascience.com/5-lesser-known-python-libraries-for-your-next-nlp-project-ff13fc652553/)  
22. gibberish-detector \- PyPI, consulté le janvier 8, 2026, [https://pypi.org/project/gibberish-detector/](https://pypi.org/project/gibberish-detector/)  
23. Detecting Gibberish and Nonsense Strings with Python | by Max Bade | Dev Genius, consulté le janvier 8, 2026, [https://blog.devgenius.io/detecting-gibberish-and-nonsense-strings-with-python-a557a03e66e1](https://blog.devgenius.io/detecting-gibberish-and-nonsense-strings-with-python-a557a03e66e1)  
24. (PDF) Learning regular expressions for clinical text classification \- ResearchGate, consulté le janvier 8, 2026, [https://www.researchgate.net/publication/260431619\_Learning\_regular\_expressions\_for\_clinical\_text\_classification](https://www.researchgate.net/publication/260431619_Learning_regular_expressions_for_clinical_text_classification)  
25. Using Regex to Improve Data Search & Data Classification with Komprise, consulté le janvier 8, 2026, [https://www.komprise.com/blog/using-regex-to-improve-data-search-data-classification-with-komprise/](https://www.komprise.com/blog/using-regex-to-improve-data-search-data-classification-with-komprise/)  
26. Implement intelligent Document Processing correctly: OCR, AI and REGEX \- PaperOffice, consulté le janvier 8, 2026, [https://start.paperoffice.com/en/document-processing-ocr-ai-regex](https://start.paperoffice.com/en/document-processing-ocr-ai-regex)  
27. Resources to validate CSV files using complex rules \- Python Discussions, consulté le janvier 8, 2026, [https://discuss.python.org/t/resources-to-validate-csv-files-using-complex-rules/105173](https://discuss.python.org/t/resources-to-validate-csv-files-using-complex-rules/105173)  
28. pandera documentation, consulté le janvier 8, 2026, [https://pandera.readthedocs.io/](https://pandera.readthedocs.io/)  
29. Data Type Validation \- pandera documentation, consulté le janvier 8, 2026, [https://pandera.readthedocs.io/en/stable/dtype\_validation.html](https://pandera.readthedocs.io/en/stable/dtype_validation.html)  
30. How to define a Pandera DataFrame schema for validating and parsing datetime columns?, consulté le janvier 8, 2026, [https://stackoverflow.com/questions/76390954/how-to-define-a-pandera-dataframe-schema-for-validating-and-parsing-datetime-col](https://stackoverflow.com/questions/76390954/how-to-define-a-pandera-dataframe-schema-for-validating-and-parsing-datetime-col)  
31. Perplexity of fixed-length models \- Hugging Face, consulté le janvier 8, 2026, [https://huggingface.co/docs/transformers/en/perplexity](https://huggingface.co/docs/transformers/en/perplexity)  
32. Two minutes NLP — Perplexity explained with simple probabilities | by Fabio Chiusano | Generative AI | Medium, consulté le janvier 8, 2026, [https://medium.com/nlplanet/two-minutes-nlp-perplexity-explained-with-simple-probabilities-6cdc46884584](https://medium.com/nlplanet/two-minutes-nlp-perplexity-explained-with-simple-probabilities-6cdc46884584)  
33. Is it possible to evaluate llama.cpp perplexity using llama-cpp-python? \#461 \- GitHub, consulté le janvier 8, 2026, [https://github.com/abetlen/llama-cpp-python/discussions/461](https://github.com/abetlen/llama-cpp-python/discussions/461)  
34. Evaluating the Effectiveness of LLM-Evaluators (aka LLM-as-Judge), consulté le janvier 8, 2026, [https://eugeneyan.com/writing/llm-evaluators/](https://eugeneyan.com/writing/llm-evaluators/)  
35. LLM-as-a-Judge: Can Language Models Be Trusted to Evaluate Other Models? \- Medium, consulté le janvier 8, 2026, [https://medium.com/1mgofficial/llm-as-a-judge-can-language-models-be-trusted-to-evaluate-other-models-9ff50bac2e77](https://medium.com/1mgofficial/llm-as-a-judge-can-language-models-be-trusted-to-evaluate-other-models-9ff50bac2e77)  
36. Building and Managing an LLM-based OCR System with MLflow, consulté le janvier 8, 2026, [http://mlflow.org/blog/mlflow-prompt-evaluate](http://mlflow.org/blog/mlflow-prompt-evaluate)  
37. Early evidence of how LLMs outperform traditional systems on OCR/HTR tasks for historical records \- arXiv, consulté le janvier 8, 2026, [https://arxiv.org/html/2501.11623v1](https://arxiv.org/html/2501.11623v1)  
38. Reference-Based Post-OCR Processing with LLM for Precise Diacritic Text in Historical Document Recognition \- arXiv, consulté le janvier 8, 2026, [https://arxiv.org/html/2410.13305v1](https://arxiv.org/html/2410.13305v1)  
39. LLM-as-a-judge: a complete guide to using LLMs for evaluations \- Evidently AI, consulté le janvier 8, 2026, [https://www.evidentlyai.com/llm-guide/llm-as-a-judge](https://www.evidentlyai.com/llm-guide/llm-as-a-judge)  
40. How to combine the results of multiple OCR tools to get better text recognition \[closed\], consulté le janvier 8, 2026, [https://stackoverflow.com/questions/55367637/how-to-combine-the-results-of-multiple-ocr-tools-to-get-better-text-recognition](https://stackoverflow.com/questions/55367637/how-to-combine-the-results-of-multiple-ocr-tools-to-get-better-text-recognition)  
41. SCTK/doc/rover/rover.htm at master · usnistgov/SCTK \- GitHub, consulté le janvier 8, 2026, [https://github.com/usnistgov/SCTK/blob/master/doc/rover/rover.htm](https://github.com/usnistgov/SCTK/blob/master/doc/rover/rover.htm)  
42. \[1707.07432\] LV-ROVER: Lexicon Verified Recognizer Output Voting Error Reduction \- ar5iv, consulté le janvier 8, 2026, [https://ar5iv.labs.arxiv.org/html/1707.07432](https://ar5iv.labs.arxiv.org/html/1707.07432)  
43. RaphaelOlivier/gard\_eval2\_public \- GitHub, consulté le janvier 8, 2026, [https://github.com/RaphaelOlivier/gard\_eval2\_public](https://github.com/RaphaelOlivier/gard_eval2_public)  
44. Multiple Sequence Alignment objects — Biopython 1.87.dev0 documentation, consulté le janvier 8, 2026, [https://biopython.org/docs/dev/Tutorial/chapter\_msa.html](https://biopython.org/docs/dev/Tutorial/chapter_msa.html)  
45. Multiple Sequence Alignment objects — test test documentation \- Biopython, consulté le janvier 8, 2026, [https://biopython-tutorial.readthedocs.io/en/latest/notebooks/06%20-%20Multiple%20Sequence%20Alignment%20objects.html](https://biopython-tutorial.readthedocs.io/en/latest/notebooks/06%20-%20Multiple%20Sequence%20Alignment%20objects.html)  
46. \[PDF\] Voting-Based Ocr System | Semantic Scholar, consulté le janvier 8, 2026, [https://www.semanticscholar.org/paper/Voting-Based-Ocr-System-Boiangiu-Ioanitescu/baea034e8394890435b09e914cfb39a2eb57422c](https://www.semanticscholar.org/paper/Voting-Based-Ocr-System-Boiangiu-Ioanitescu/baea034e8394890435b09e914cfb39a2eb57422c)  
47. Voting-Based Ocr System \- IDEAS/RePEc, consulté le janvier 8, 2026, [https://ideas.repec.org/a/rau/jisomg/v10y2016i2p470-486.html](https://ideas.repec.org/a/rau/jisomg/v10y2016i2p470-486.html)  
48. Set of methods to ensemble boxes from different object detection models, including implementation of "Weighted boxes fusion (WBF)" method. \- GitHub, consulté le janvier 8, 2026, [https://github.com/ZFTurbo/Weighted-Boxes-Fusion](https://github.com/ZFTurbo/Weighted-Boxes-Fusion)  
49. Weighted Boxes Fusion — A detailed view | by Sambasivarao. K | Analytics Vidhya | Medium, consulté le janvier 8, 2026, [https://medium.com/analytics-vidhya/weighted-boxes-fusion-86fad2c6be16](https://medium.com/analytics-vidhya/weighted-boxes-fusion-86fad2c6be16)  
50. Building a Scalable OCR Pipeline: Technical Architecture Behind HealthEdge's Document Processing Platform, consulté le janvier 8, 2026, [https://healthedge.com/resources/blog/building-a-scalable-ocr-pipeline-technical-architecture-behind-healthedge-s-document-processing-platform](https://healthedge.com/resources/blog/building-a-scalable-ocr-pipeline-technical-architecture-behind-healthedge-s-document-processing-platform)  
51. Scalable OCR Pipelines using AWS \- Towards Data Science, consulté le janvier 8, 2026, [https://towardsdatascience.com/scalable-ocr-pipelines-using-aws-88b3c130a1ea/](https://towardsdatascience.com/scalable-ocr-pipelines-using-aws-88b3c130a1ea/)  
52. ensemble-boxes \- PyPI, consulté le janvier 8, 2026, [https://pypi.org/project/ensemble-boxes/](https://pypi.org/project/ensemble-boxes/)