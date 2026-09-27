> NotebookLM deep research (64 sources), 2026-06-12. Notebook: 8ee16d31-a7a7-44e3-89d5-4f6387474615 (queryable via nlm). Query framed neutrally to counter confirmation bias: vendor claims tested against third-party benchmarks and failure reports.

# Computational Digitization of Scanned Financial Statements: A Comparative Evaluation of OCR Engines and Vision-Language Models (2025–2026)

## Architectural Requirements of Financial Document Digitization

The digitization of scanned financial statements represents an acute challenge in the field of document processing and information extraction.[1, 2] Financial reports, such as balance sheets, income statements, and cash flow disclosures, are dense, multi-column layouts where structural relationships are as critical as individual characters.[2, 3, 4] A single character error can fundamentally invert the meaning of a metric, leading to downstream analytical, valuation, and regulatory noncompliance failures.[5, 6] 

To mathematically assess these errors, character error rate ($\text{CER}$) is modeled as:

$$\text{CER} = \frac{S + D + I}{N}$$

where $S$ represents character substitutions, $D$ represents deletions, $I$ represents insertions, and $N$ represents the total number of characters in the ground-truth document.[7] While a standard business document might tolerate a $\text{CER}$ of $5\%$, a financial pipeline requires a high level of accuracy, often exceeding $99.9\%$, to minimize human verification loops.[8]

Physical distortions represent a major cause of character deletions and substitutions.[7] Scanned documents, particularly historical or physically archived financial disclosures, frequently arrive with visible curvatures from book spines, diagonal skewing, low binarization contrast, and visual artifacts.[9, 10, 11] These distortions alter the geometric relationship of characters on the page, causing segmented text lines to drift and merge.[9, 12] 

Linguistically, French-language financial statements present additional complexities due to diacritical markers such as accents (*é*, *è*, *à*), cedillas (*ç*), and circumflexes (*â*, *î*, *û*), which must be transcribed precisely to preserve spelling and meaning.[13] 

Furthermore, financial statements use highly specific symbols to denote mathematical states. Negative numbers are represented using either a minus sign ($-$) or parentheses (e.g., $(1,500)$), while percentages are denoted by percent signs ($\%$) and currencies by their respective symbols.[14, 15, 16] 

Standard OCR systems frequently drop or misinterpret these small, isolated visual features, confusing a minus sign with background noise, or a parenthesis with the digit one or the letter "l".[16, 17, 18] 

Finally, table handling requires reconstructing a logical grid from spatial relationships rather than merely reading linear streams of characters.[2, 3] Borderless tables, where empty whitespace alone defines columns, and multi-page tables with repeated headers, consistently break traditional parsing engines.[3]

---

## Technical Performance on Accents, Digits, Signs, and Tables

The technical processing of digits, diacritics, mathematical symbols, and tabular structures varies significantly across different optical character recognition technologies.

### Digits, Currency Symbols, and Decimal Separators
Within the category of established engines, character confusion remains a persistent issue under suboptimal scan qualities.[12, 19] EasyOCR exhibits a documented vulnerability to currency symbol substitution on degraded scans, where it frequently misreads the dollar sign (`$`) as the letter `S` or the digit `8` (e.g., transforming `$616.25` into `8616.25`), and the Euro sign (`€`) as the letter `E`.[14, 20] 

Tesseract 5.5 handles standard currency symbols reliably on high-resolution, clean documents, but exhibits a notable failure mode regarding international decimal separators.[14] It frequently confuses periods and commas in European financial formats, reading a European total of `1.250,00` as `1,250.00` or corrupting the entry entirely depending on the active language pack and font weight.[14] 

PaddleOCR (PP-OCRv5) provides strong multilingual digit recognition [14], but suffers from a known issue where it inconsistently omits negative minus signs ($-$) and currency symbols (`$`, `€`).[16] This is primarily because small, isolated characters are misclassified by the visual text detector as noise and discarded before reaching the text recognition phase.[18, 21] 

Conversely, Vision-Language Models (VLMs) such as LightOnOCR-2-1B, dots.ocr 3B, and GLM-OCR utilize joint visual-textual context to infer missing or obscured symbols, maintaining a character-level accuracy above $90\%$ on degraded historical scans.[9, 13, 22]

### Diacritic and Accent Extraction
Preserving French diacritics requires specialized language training. Tesseract 5.5 requires the deployment of larger, slower `tessdata_best` language models (such as `fra.traineddata`) rather than the speed-optimized `tessdata_fast` equivalents, and its character recognition rates fall sharply if the image has a tilt exceeding $2$ degrees or a resolution under $300$ DPI.[23, 24] 

PaddleOCR excels at multilingual script extraction, but its performance drops in areas with watermarks or background patterns, where it tends to confuse contrast boundaries and drop entire paragraph blocks.[21] 

VLM-based approaches are highly robust against diacritic loss.[9, 13] LightOnOCR-2-1B, for instance, utilizes a native-resolution Vision Transformer initialized from a Mistral-Small-3.1 vision encoder, which preserves high-frequency spatial details and is trained on a distillation mix containing extensive French documents.[13, 25] This allows the model to reconstruct accented characters such as *é* or *ç* even when they are physically blurred or partially faded.[9, 13]

### Tabular Grid Mapping and Row Alignment
Rebuilding tables from scanned images is a primary failure point for traditional engines.[2, 3] Tesseract possesses zero table or layout awareness; it reads the document as a flat, linearized sequence of text from left to right across the page.[14] This causes adjacent columns to merge on the same line, requiring highly fragile custom post-processing to reconstruct rows and columns based on character bounding boxes.[14] 

EasyOCR provides spatial bounding boxes but does not preserve structural relationships between cells in the same row, forcing developers to implement custom heuristic clustering logic.[14] 

PaddleOCR with PP-StructureV3 stands out as a strong non-VLM option, featuring a native table structure recognition module that identifies row-column intersections and outputs cell-level coordinates mapping to a structured grid.[14] 

Among open-source parsers, Docling uses IBM's TableFormer model to identify table regions and map individual cells to their exact row and column positions, successfully preserving merged cells and nested headers.[26, 27] However, Docling's TableFormer can struggle with dense, borderless tables, sometimes merging adjacent rows.[27] 

Marker, which uses Surya as its layout engine, can split merged cells and miss nested headers on dense tables.[26] 

LlamaParse, a cloud-hosted LLM-guided service, achieves high-fidelity table extraction by rendering tables into structured HTML, ensuring exact cell alignment and preserving complex, multi-level headers.[27, 28]

---

## Comparative Landscape of OCR and VLM Technologies

The table below provides a comprehensive comparison of OCR and VLM-based options for digitizing scanned financial statements in 2025–2026.

| Engine Name | Measured Accuracy on Digits & Signs | Table Handling Strategy | CPU vs. GPU Latency | Licensing | Python Integration | Determinism Level | Documented Failure Modes |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Tesseract 5.5** | High on clean text; confuses periods and commas on international scans.[14] | Linearizes text; reads left-to-right across columns; no table awareness.[14] | ~0.77s to 1.0s per page (CPU); no GPU support.[14, 29] | Apache 2.0.[30] | Highly mature via `pytesseract` [14]; small footprint (~10MB).[29] | Fully deterministic.[17] | Fails on skewed text (>2°); high error rates on low contrast.[12, 23, 24] |
| **PaddleOCR (PP-OCRv5)** | High; strong multilingual support; inconsistently omits minus signs.[14, 16] | Strong table extraction via native PP-StructureV3 module.[14] | ~4.85s per page (CPU) [29]; ~120 pages/min on GPU (RTX 3090).[31] | Apache 2.0.[30] | Moderate; depends on the complex PaddlePaddle framework.[14, 32] | Fully deterministic. | Paragraph truncation in watermarked regions; complex dependency setup.[21, 32] |
| **EasyOCR** | Poor on currency symbols (`$` read as `8` or `S`).[14, 20] | Returns spatial bounding boxes only; lacks cell-row relational grouping.[14] | ~2.5s to 3.0s per page on CPU; ~60 pages/min on GPU (RTX 3090).[14, 31] | MIT.[33] | Simple Python API with native PyTorch dependencies.[31, 34] | Fully deterministic. | Hallucinates alphanumeric characters in dense text; high latency spikes.[21] |
| **RapidOCR** | Moderate; struggles with tight character spacing.[20] | No native table extraction capabilities.[14, 20] | ~0.5s to 1.0s on CPU via ONNX Runtime.[14, 20] | Apache 2.0.[29] | Mature; lightweight option (~80MB container footprint).[14, 20] | Fully deterministic. | Merges adjacent words; fails on complex table layouts.[20] |
| **Docling (IBM)** | High; depends on configured backend (EasyOCR/Tesseract).[26] | Employs TableFormer model to map cell-row alignments.[26, 27] | ~5.0s to 8.0s per scanned page on CPU.[26] | MIT.[26, 33] | High; native LangChain and LlamaIndex integrations.[35] | High (governed by backend). | High memory footprint (~2GB); GraniteDocling VLM mode hallucinates numbers.[26, 28] |
| **Marker** | High; powered by the Surya OCR backbone.[30, 35] | Stage-based layout extraction; tends to split merged cells.[26] | ~3.0s to 5.0s per page (GPU-accelerated by default).[26] | GPL-3.0.[33] | Moderate; outputs markdown but lacks detailed bounding boxes.[26, 35] | High (governed by backend). | Splits multi-line table rows; GPL license restricts commercial use.[27, 33] |
| **LightOnOCR-2-1B** | High on European diacritics and scanned administrative documents.[13, 25] | Unified VLM end-to-end; outputs normalized bounding boxes for figures.[25, 36] | High throughput; ~5.71 pages/sec on a single H100.[37] | Apache 2.0.[25, 33] | Moderate; integrates with HuggingFace Transformers.[36] | Probabilistic; prompt-sensitive.[9] | Fails on complex CJK scripts; prone to standard VLM numeric hallucinations.[17, 35] |
| **dots.ocr 3B** | Excellent; scores 87.5 (EN) on OmniDocBench v1.5.[38] | Unified VLM end-to-end; preserves layout, tables, and relationships.[29, 38] | ~3.5s per page on 1x A10 GPU; requires minimum 6GB VRAM.[29] | Apache 2.0.[29] | Moderate; requires PyTorch and vLLM stack dependencies.[29] | Probabilistic. | Struggles with high character-to-pixel ratios; repeating special characters.[39] |
| **GLM-OCR** | Superior; scores 94.6% on OmniDocBench v1.5.[22, 40] | Integrates PP-DocLayoutV3 layout analysis with parallel content recognition.[22] | Highly optimized via Multi-Token Prediction (MTP) decoding.[22] | Gated / Cloud MaaS API.[22, 33] | Emerging; supports vLLM, SGLang, and Ollama serving.[22] | Probabilistic. | High compute requirements; API-dependent versions restrict offline use.[33] |
| **PaddleOCR-VL-1.5** | Superior; scores 94.5% on OmniDocBench v1.5.[10] | Integrates PP-DocLayoutV3 with element-level text recognition.[10] | Compact 0.9B parameter VLM; runs on 8GB VRAM GPUs.[10, 30] | Apache 2.0.[10] | Complex installation; tied to the PaddlePaddle framework.[22, 32] | Probabilistic. | Complex installation on non-standard hardware; limited documentation.[32, 35] |

---

## Verifying Surya OCR: Benchmarks vs. Production Reality

Surya OCR 2 is a 650-million parameter vision-language model developed by Datalab, designed for multilingual document parsing, mathematical extraction, and layout-aware text recognition.[41, 42] It collapses text recognition, layout detection, and table recognition into a single unified VLM, leaving text detection as a small, dedicated Torch-based model to optimize latency.[42] While vendor marketing positions Surya 2 as a state-of-the-art solution, an objective review of independent benchmarks and user failure reports reveals a notable gap between controlled evaluations and production realities.

### Empirical Benchmark Performance
On the standard `olmOCR-Bench` default preset, Surya 2 achieves an overall score of $83.3\%$.[42] This is a strong performance for a 650M parameter model, allowing it to beat the first-generation Chandra OCR 1 (a 9B parameter model scoring $83.1\%$) and match the performance of LightOnOCR 2-1B ($83.2\%$).[42] 

However, independent evaluations confirm that Surya 2 is consistently outperformed by larger, modern architectures.[42] For instance, Chandra OCR 2 (Datalab’s 4.0B parameter flagship model) achieves $85.9\%$, while advanced architectures like the Infinity-Parser2-Pro (35.1B parameters) score $87.6\%$.[42] 

Furthermore, on public document understanding benchmarks such as OmniDocBench v1.5, Surya’s underlying architecture is significantly outpaced by modern 0.9B parameter models like GLM-OCR ($94.6\%$) and PaddleOCR-VL-1.5 ($94.5\%$).[10, 22, 40] Thus, while Surya 2 is highly optimized for its parameter size, it does not represent the absolute state-of-the-art across the broader document processing landscape.

### Software Engineering and Runtime Criticisms
Practitioners deploying Surya OCR in enterprise environments have identified several recurring architectural and operational failures:

* **Progress Feedback Failures (Issue #511):** When running in full-page mode (`surya_ocr`), the engine operates silently without providing any progress feedback until the entire document is processed.[43] In multi-page PDF processing pipelines, the backend batches pages using a `ThreadPoolExecutor` and passes them directly to the inference server, entirely bypassing the progress bars used in detection and layout modules.[43] This behavior makes it impossible for developers to programmatically determine if a job is actively processing or permanently hung.[43]
* **Italics Detection Failures (Issue #507):** Although the model's design theoretically supports the identification and extraction of italicized text formatting, user evaluations indicate that italics detection consistently fails on standard scanned document sets.[44]
* **Breaking Dependency Fragmentation (Issues #484, #486, #509):** The Surya Python package has suffered from severe version sensitivity with the HuggingFace `transformers` ecosystem.[44, 45] The release of `transformers 5.0.0` deprecates internal helper functions, such as `find_pruneable_heads_and_indices`, which immediately triggers runtime failures in Surya.[44, 45] Furthermore, instances of `'SuryaDecoderConfig' object has no attribute 'pad_token_id'` cause immediate execution crashes unless developers manually pin the environment to older versions (e.g., `transformers==4.56.0` or `<4.40.0`).[44, 45]
* **Table Extraction and Header Misclassification:** While Surya's layout analyzer detects tabular regions and identifies rows and columns, it often misclassifies or completely misses table headers.[41, 46] In dense or borderless table environments, the engine cannot natively output a structured, schema-validated grid (such as HTML or a Pandas DataFrame).[14] It merely provides bounding boxes, leaving the complex row-and-column alignment to custom, fragile post-processing heuristics.[14]
* **Extreme Latency on CPU Hardware:** Although traditional OCR engines like Tesseract require minimal CPU resources, Surya's Transformer architecture requires high compute.[14, 20] Processing a standard 10-page document on CPU can take upwards of 30 minutes, requiring a minimum of 4–5 GB of RAM.[46] For commercial pipelines, dedicated GPU servers are mandatory, which limits Surya's utility in high-throughput, low-cost operations.[46]
* **Restrictive Licensing Constraints:** Surya is licensed under the GPL-3.0 copyleft license.[20, 33] This represents a critical blocker for commercial enterprises, as integrating Surya directly into proprietary SaaS pipelines can legally mandate the disclosure of the proprietary source code.[20, 33]

---

## Negative Evidence, Regressions, and VLM Hallucination Risks

A comprehensive evaluation of the modern document processing landscape must address known regressions, overfitting, and scenarios where simpler engines beat newer, more complex architectures.

### Overfitting to Benchmark Sets
The rapid escalation of scores on benchmarks like OmniDocBench v1.5 suggests a high risk of model overfitting.[40] OmniDocBench contains $1,355$ to $1,651$ pages, which is relatively small compared to the diversity of real-world documents.[4, 40] 

The benchmark dataset is heavily weighted toward academic papers and textbook layouts, completely omitting complex real-world financial presentations, multi-page market research reports, and custom insurance claims.[40] 

Furthermore, OmniDocBench relies on continuous metrics such as BLEU and edit distance, which are highly sensitive and penalize minor, harmless formatting differences (e.g., a misplaced line break or punctuation mark) while failing to capture structural errors in dense financial data.[37, 40] 

Models that score above $94\%$ on OmniDocBench can fail when deployed on real-world, out-of-distribution financial scans, as their training sets are highly optimized for academic and clean printed layouts.[9, 40]

### Known Technical Regressions
Software version mismatches represent a recurring friction point in deep-learning OCR deployment.[45, 47] In addition to Surya's breaking compatibility issues with HuggingFace `transformers >= 5.0.0` [44, 45], PaddlePaddle has exhibited version-related regressions.[47] 

For instance, developers who fine-tune recognition models using PaddlePaddle version 3.2.2 but run inference on runtime environments configured with PaddlePaddle 3.0.0 report silent errors and execution crashes.[47] 

These runtime regressions require strict environment pin-point alignments to avoid breaking production pipelines.[44, 47]

### Cases Where Simpler Engines Outperform Newer Architectures
While vision-language models and deep-learning pipelines are highly capable, there are several standard production scenarios where traditional, simpler engines outperform them:

* **Clean, High-Resolution Printed Text:** On high-resolution, natively digital PDFs or clean scans (300+ DPI), Tesseract 5.5 and RapidOCR achieve near-perfect character accuracy.[14, 20, 23] Tesseract processes pages in under 1 second on commodity CPU hardware with a tiny 10MB memory footprint, while a VLM requires heavy GPU infrastructure, introduces high latency, and risks hallucinatory character substitutions (such as swapping `O` for `0` or `l` for `1`).[14, 17, 20]
* **Strict Audit Trail and Lineage Requirements:** In financial auditing, every extracted number must map to its exact pixel coordinate on the source image.[3, 17] Traditional engines (and layout tools like PaddleOCR or Docling) provide native, pixel-level bounding boxes for every extracted word.[26, 41] Most end-to-end VLMs output flat markdown or JSON, which strips away spatial metadata and makes it difficult to establish a verifiable audit trail.[17, 26]
* **Resource-Constrained Environments:** In edge computing or lightweight containerized environments (such as AWS Lambda or serverless microservices), the deep-learning dependencies of modern VLMs (such as PyTorch or PaddlePaddle) are too heavy, requiring gigabytes of memory and slow cold-start times.[20, 34] Tesseract or RapidOCR can be deployed in a tiny 50MB to 80MB Docker image, cold-starting instantly and running efficiently on a single CPU core.[20]
* **Unstructured Diffusion Hallucinations:** MinerU-Diffusion, which utilizes parallel diffusion decoding, is architecturally advanced but prone to severe content duplication on busy pages and hallucinating characters on small, dense text.[33] Autoregressive VLMs also suffer from hallucinations under financial analysis conditions.[5, 7] For example, in the 2026 Juris Hallucination Benchmark, models like GLM-5.1 confidently fabricated management names and mixed annual and quarterly metrics in the same calculation, producing highly plausible but completely false ratios.[5]

---

## Strategy for Zero-Hallucination Financial Digitization

To digitize scanned financial statements with zero-hallucination tolerance, a multi-layered hybrid architecture is recommended, combining the deterministic accuracy of traditional OCR with the layout awareness of vision-language models [17]:

```
                       +-------------------------+
                       |   Scanned PDF / Image   |
                       +------------+------------+
                                    |
                                    v
                       +------------+------------+
                       |    Image Preprocessing  |  <-- 300 DPI, Deskew, Sauvola Binarization [12, 24]
                       +------------+------------+
                                    |
                                    |
            +-----------------------+-----------------------+
            |                                               |
            v                                               v
+-----------+-----------+                       +-----------+-----------+
|    OCR Text Engine    |                       |  Layout Analysis VLM  |
|  (PaddleOCR / Tess)   |                       |  (Docling / LightOn)  |
+-----------+-----------+                       +-----------+-----------+
| Generates exact,      |                       | Extracts structural   |
| deterministic raw     |                       | relationships, rows,  |
| characters & coords   |                       | columns, and cells    |
+-----------+-----------+                       +-----------+-----------+
            |                                               |
            +-----------------------+-----------------------+
                                    |
                                    v
                       +------------+------------+
                       |  Heuristic Merge Layer  |  <-- Maps deterministic characters into VLM grid [17]
                       +------------+------------+
                                    |
                                    v
                       +------------+------------+
                       |   Output: Validated     |
                       |   Markdown / JSON       |
                       +-------------------------+
```

### Preprocessing Protocol
Prior to character extraction, the source image must be normalized to eliminate physical distortions.[12, 24] The resolution should be upscaled to exactly $300$ DPI.[12, 24] 

A deskewing algorithm must be applied to correct page rotations greater than $1$ degree, which prevents line segmentation failures.[12, 23, 24] 

Adaptive binarization, specifically using Sauvola's or Adaptive Otsu's local thresholding algorithms, must be executed to remove shadows and background noise while maintaining the visual integrity of thin character strokes and diacritical marks.[12, 24]

### Extraction of Deterministic Text
The raw text extraction must be handled by a deterministic OCR engine to ensure that characters, signs, and numbers are not altered.[17] 

PaddleOCR PP-OCRv5 or Tesseract 5.5 (utilizing the `tessdata_best` language models) should be deployed, with dictionary-based word corrections disabled to prevent the engine from substituting non-dictionary financial codes or numeric values.[12, 14, 24] 

This step generates a clean coordinate map where every character, period, comma, minus sign, and percentage symbol is recorded with its exact pixel boundaries on the page.[14]

### Structural Layout Extraction
Simultaneously, the same page image is processed through a layout analysis model.[17] Docling’s TableFormer model is utilized to detect table regions, identify cell boundaries, and output the structural grid coordinates.[26, 27] 

This structural model acts as a geometric template, defining where columns, rows, and merged headers exist on the page, without transcribing the raw characters.[3, 17]

### Spatial Projective Merging and Mathematical Validation
The deterministic text map is geometrically projected into the table coordinates.[17] Character coordinates from the text engine are mapped directly to the table cells.[3, 17] 

This spatial projection assigns the exact, deterministic numbers into the cells, avoiding the risk of hallucination.[3, 17] 

Finally, a programmatic validation layer must be applied to check the integrity of the extraction [3, 8]:

$$\text{Total}_{\text{Calculated}} = \sum_{i=1}^{K} \text{Line\_Item}_{i}$$

The system checks that calculated column totals match the printed values.[17] Any mathematical discrepancies, missing parentheses, or sign mismatches are automatically flagged for human review, ensuring an accurate and reliable audit trail.[3, 17]

---

1. Best Vision Language Models & Agentic OCR Tools for Developers - LlamaIndex, [https://www.llamaindex.ai/insights/best-vision-language-models](https://www.llamaindex.ai/insights/best-vision-language-models)
2. Why OCR Alone Fails in Real-World Documents - DEV Community, [https://dev.to/jakemiller/why-ocr-alone-fails-in-real-world-documents-5f86](https://dev.to/jakemiller/why-ocr-alone-fails-in-real-world-documents-5f86)
3. Why Table Extraction Fails: OCR Reads Characters, Tables Encode Relationships : r/microsaas - Reddit, [https://www.reddit.com/r/microsaas/comments/1rvp7v9/why_table_extraction_fails_ocr_reads_characters/](https://www.reddit.com/r/microsaas/comments/1rvp7v9/why_table_extraction_fails_ocr_reads_characters/)
4. OmniDocBench: Benchmarking Diverse PDF Document Parsing with Comprehensive Annotations - 2077AI, [https://www.2077ai.com/datasets/dataset-omnidocbench](https://www.2077ai.com/datasets/dataset-omnidocbench)
5. Best LLM Tools for Financial Analysis 2026: JurisTech's Hallucination Benchmark Report, [https://juristech.net/best-llm-tools-for-financial-analysis-2026/](https://juristech.net/best-llm-tools-for-financial-analysis-2026/)
6. LLM Hallucinations: What Are the Implications for Financial Institutions? | BizTech Magazine, [https://biztechmagazine.com/article/2025/08/llm-hallucinations-what-are-implications-financial-institutions](https://biztechmagazine.com/article/2025/08/llm-hallucinations-what-are-implications-financial-institutions)
7. FailSafeQA: Evaluating AI Hallucinations, Robustness, and Compliance in Financial LLMs, [https://ajithp.com/2025/02/15/failsafeqa-evaluating-ai-hallucinations-robustness-and-compliance-in-financial-llms/](https://ajithp.com/2025/02/15/failsafeqa-evaluating-ai-hallucinations-robustness-and-compliance-in-financial-llms/)
8. OCR Accuracy Benchmarks: The 2026 Digital Transformation Revolution | by VAO AI, [https://medium.com/@info_59976/ocr-accuracy-benchmarks-the-2026-digital-transformation-revolution-2f7095c2696f](https://medium.com/@info_59976/ocr-accuracy-benchmarks-the-2026-digital-transformation-revolution-2f7095c2696f)
9. OCR vs VLM: Accuracy, Performance & Real-World Use - F22 Labs, [https://www.f22labs.com/blogs/ocr-vs-vlm-vision-language-models-key-comparison/](https://www.f22labs.com/blogs/ocr-vs-vlm-vision-language-models-key-comparison/)
10. PaddleOCR-VL-1.5: Towards a Multi-Task 0.9B VLM for Robust In-the-Wild Document Parsing - arXiv, [https://arxiv.org/html/2601.21957v1](https://arxiv.org/html/2601.21957v1)
11. PaddlePaddle/Real5-OmniDocBench · Datasets at Hugging Face, [https://huggingface.co/datasets/PaddlePaddle/Real5-OmniDocBench](https://huggingface.co/datasets/PaddlePaddle/Real5-OmniDocBench)
12. Improving the quality of the output | tessdoc - Tesseract documentation, [https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)
13. LightOnOCR: A 1B End-to-End Multilingual Vision-Language Model for State-of-the-Art OCR - arXiv, [https://arxiv.org/html/2601.14251v1](https://arxiv.org/html/2601.14251v1)
14. Best Python OCR Library for Invoices: 5 Engines Compared, [https://invoicedataextraction.com/blog/python-ocr-library-comparison-invoices](https://invoicedataextraction.com/blog/python-ocr-library-comparison-invoices)
15. What do the negative numeric signs mean in the screenshot? : r/baduk - Reddit, [https://www.reddit.com/r/baduk/comments/1muvebs/what_do_the_negative_numeric_signs_mean_in_the/](https://www.reddit.com/r/baduk/comments/1muvebs/what_do_the_negative_numeric_signs_mean_in_the/)
16. Characters such as '-', '$', '€' not being detected by paddleocr #12161 - GitHub, [https://github.com/PaddlePaddle/PaddleOCR/issues/12161](https://github.com/PaddlePaddle/PaddleOCR/issues/12161)
17. OCR vs VLM: Why You Need Both (And How Hybrid Approaches Win) - DEV Community, [https://dev.to/kesimo/ocr-vs-vlm-why-you-need-both-and-how-hybrid-approaches-win-5bo4](https://dev.to/kesimo/ocr-vs-vlm-why-you-need-both-and-how-hybrid-approaches-win-5bo4)
18. PaddleOCR doesn't recognize digit 0 #14625 - GitHub, [https://github.com/PaddlePaddle/PaddleOCR/discussions/14625](https://github.com/PaddlePaddle/PaddleOCR/discussions/14625)
19. [P] Tesseract OCR - Has anybody used it for reading from PDF-s? : r/MachineLearning, [https://www.reddit.com/r/MachineLearning/comments/1f87yfg/p_tesseract_ocr_has_anybody_used_it_for_reading/](https://www.reddit.com/r/MachineLearning/comments/1f87yfg/p_tesseract_ocr_has_anybody_used_it_for_reading/)
20. Best Python OCR Library in 2026: 6 Libraries Tested with Real Code ..., [https://codesota.com/ocr/best-for-python](https://codesota.com/ocr/best-for-python)
21. PaddleOCR vs EasyOCR: Text loss and watermark issues when processing complex PDFs in Python - Stack Overflow, [https://stackoverflow.com/questions/79945759/paddleocr-vs-easyocr-text-loss-and-watermark-issues-when-processing-complex-pdf](https://stackoverflow.com/questions/79945759/paddleocr-vs-easyocr-text-loss-and-watermark-issues-when-processing-complex-pdf)
22. 1 Performance of GLM-OCR on OmniDocBench v1.5. - arXiv, [https://arxiv.org/html/2603.10910v1](https://arxiv.org/html/2603.10910v1)
23. Accuracy of Optical Character Recognition Software Google Tesseract - USM Digital Commons, [https://digitalcommons.usm.maine.edu/cgi/viewcontent.cgi?article=1042&context=thinking_matters](https://digitalcommons.usm.maine.edu/cgi/viewcontent.cgi?article=1042&context=thinking_matters)
24. Question: Tesseract OCR sometimes reads text wrong – any tips #180418 - GitHub, [https://github.com/orgs/community/discussions/180418](https://github.com/orgs/community/discussions/180418)
25. [2601.14251] LightOnOCR: A 1B End-to-End Multilingual Vision-Language Model for State-of-the-Art OCR - arXiv, [https://arxiv.org/abs/2601.14251](https://arxiv.org/abs/2601.14251)
26. Docling vs Marker: Which Document Parser Works Better for RAG ..., [https://docs.bswen.com/blog/2026-04-16-docling-vs-marker-document-parsing/](https://docs.bswen.com/blog/2026-04-16-docling-vs-marker-document-parsing/)
27. codecut-blog/docling-vs-marker-vs-llamaparse.ipynb at main - GitHub, [https://github.com/khuyentran1401/codecut-blog/blob/main/docling-vs-marker-vs-llamaparse.ipynb](https://github.com/khuyentran1401/codecut-blog/blob/main/docling-vs-marker-vs-llamaparse.ipynb)
28. PDF Table Extraction: Docling vs Marker vs LlamaParse Compared ..., [https://codecut.ai/docling-vs-marker-vs-llamaparse/](https://codecut.ai/docling-vs-marker-vs-llamaparse/)
29. PaddleOCR vs Tesseract vs EasyOCR: OCR Speed and Accuracy 2026 | CodeSOTA, [https://codesota.com/ocr/paddleocr-vs-tesseract](https://codesota.com/ocr/paddleocr-vs-tesseract)
30. OCR Speed Benchmarks - GIGAGPU, [https://gigagpu.com/ocr-speed-benchmarks/](https://gigagpu.com/ocr-speed-benchmarks/)
31. PaddleOCR vs Tesseract vs EasyOCR: OCR Model Comparison - GIGAGPU, [https://gigagpu.com/paddleocr-vs-tesseract-vs-easyocr/](https://gigagpu.com/paddleocr-vs-tesseract-vs-easyocr/)
32. [D] TesseractOCR vs PaddleOCR vs EasyOCR for Japanese text extraction - Reddit, [https://www.reddit.com/r/MachineLearning/comments/170j47f/d_tesseractocr_vs_paddleocr_vs_easyocr_for/](https://www.reddit.com/r/MachineLearning/comments/170j47f/d_tesseractocr_vs_paddleocr_vs_easyocr_for/)
33. Which Small vLLM OCR Model Is the Best For Private Use in 2026? Code Included, [https://medium.com/@AiDocTakes/i-tested-four-ocr-models-on-scanned-medical-records-and-the-smallest-one-won-ed7185b1c0b2](https://medium.com/@AiDocTakes/i-tested-four-ocr-models-on-scanned-medical-records-and-the-smallest-one-won-ed7185b1c0b2)
34. PaddleOCR vs EasyOCR Speed Comparison 2025 - Codesota, [https://www.codesota.com/ocr/paddleocr-vs-easyocr](https://www.codesota.com/ocr/paddleocr-vs-easyocr)
35. Best Open-Source PDF-to-Markdown Tools in 2026: Marker vs Docling vs MinerU vs pdf-craft vs PyMuPDF4LLM - The Menon Lab, [https://themenonlab.blog/blog/best-open-source-pdf-to-markdown-tools-2026](https://themenonlab.blog/blog/best-open-source-pdf-to-markdown-tools-2026)
36. LightOnOCR-2–1B: Best OCR model, beats DeepSeek OCR | by Mehul Gupta | Data Science in Your Pocket | Medium, [https://medium.com/data-science-in-your-pocket/lightonocr-2-1b-best-ocr-model-beats-deepseek-ocr-55871623e0a6](https://medium.com/data-science-in-your-pocket/lightonocr-2-1b-best-ocr-model-beats-deepseek-ocr-55871623e0a6)
37. LightOnOCR-1B: Making Knowledge Machine-Readable - LightOn AI, [https://lighton.ai/lighton-blogs/making-knowledge-machine-readable](https://lighton.ai/lighton-blogs/making-knowledge-machine-readable)
38. dots.ocr: Multilingual Document Layout Parsing in a Single Vision-Language Model - arXiv, [https://arxiv.org/html/2512.02498v3](https://arxiv.org/html/2512.02498v3)
39. 7 Best Open-Source OCR Models 2025: Benchmarks & Cost Comparison | E2E Networks, [https://www.e2enetworks.com/blog/complete-guide-open-source-ocr-models-2025](https://www.e2enetworks.com/blog/complete-guide-open-source-ocr-models-2025)
40. OmniDocBench is Saturated, What's Next for OCR Benchmarks? - LlamaIndex, [https://www.llamaindex.ai/blog/omnidocbench-is-saturated-what-s-next-for-ocr-benchmarks](https://www.llamaindex.ai/blog/omnidocbench-is-saturated-what-s-next-for-ocr-benchmarks)
41. datalab-to/surya: OCR, layout analysis, reading order, table recognition in 90+ languages - GitHub, [https://github.com/datalab-to/surya](https://github.com/datalab-to/surya)
42. Announcing Surya OCR 2: small, accurate, multilingual | Datalab, [https://www.datalab.to/blog/surya-2](https://www.datalab.to/blog/surya-2)
43. No progress indication for full-page OCR (surya_ocr) · Issue #511 · datalab-to/surya - GitHub, [https://github.com/datalab-to/surya/issues/511](https://github.com/datalab-to/surya/issues/511)
44. Issues · datalab-to/surya - GitHub, [https://github.com/datalab-to/surya/issues](https://github.com/datalab-to/surya/issues)
45. [BUG: Breaking] 'SuryaDecoderConfig' object has no attribute 'pad_token_id' #484 - GitHub, [https://github.com/datalab-to/surya/issues/484](https://github.com/datalab-to/surya/issues/484)
46. Surya OCR: A Practical Deep Dive into Multilingual OCR for Real-World Documents | by Jay, [https://medium.com/@programmerjay1215/surya-ocr-a-practical-deep-dive-into-multilingual-ocr-for-real-world-documents-e2ab79363ddb](https://medium.com/@programmerjay1215/surya-ocr-a-practical-deep-dive-into-multilingual-ocr-for-real-world-documents-e2ab79363ddb)
47. PaddleOCR/main/en/version3.x/module_usage/text_recognition · PaddlePaddle ... - GitHub, [https://github.com/PaddlePaddle/PaddleOCR/discussions/15712](https://github.com/PaddlePaddle/PaddleOCR/discussions/15712)
