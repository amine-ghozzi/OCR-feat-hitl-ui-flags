# Tier-1 OCR Engine Benchmark Matrix

> Deterministic metrics recomputed against the frozen Opus reference (`full-20260607`); runtime/resources from in-container runs (`finalze-benchmark`, one host profile). LLM-judge scores omitted — engine selection is driven by deterministic accuracy + complementarity.

## Per-engine summary

| Engine | value_acc | fuzzy | label-agnostic | sign_acc | recall | det_score | median s/page | model-load s | peak RSS MB |
|---|---|---|---|---|---|---|---|---|---|
| **rapidocr** | 0.8571 | 0.9154 | 0.9305 | 1.0 | 0.9412 | 0.9328 | 19.701 | 74.524 | 2757.8 |
| **doctr** | 0.8064 | 0.8816 | 0.9079 | 0.9982 | 0.9061 | 0.9036 | 19.419 | 58.717 | 3345.7 |
| **paddleocr** | 0.5865 | 0.6316 | 0.7481 | 1.0 | 0.8018 | 0.7961 | 79.466 | 189.937 | 3326.5 |
| **tesseract_cli** | 0.5733 | 0.6504 | 0.6842 | 0.9949 | 0.6928 | 0.7537 | 14.763 | 42.338 | 2114.2 |
| **easyocr** | 0.5338 | 0.6579 | 0.7068 | 0.9792 | 0.7306 | 0.7479 | 31.789 | 85.063 | 5776.7 |

## Per-case value accuracy (the complementarity signal)

| case | rapidocr | doctr | paddleocr | tesseract_cli | easyocr | best |
|---|---|---|---|---|---|---|
| Bilan_1.jpg | 0.705 | 0.682 | 0.409 | 0.580 | 0.386 | **rapidocr** |
| EF_CLB_2024_Provisoire_1_.pdf | 0.844 | 0.844 | 0.844 | 0.844 | 0.844 | **rapidocr** |
| Etat_r_sultat_1.docx | 0.969 | 1.000 | 0.594 | 0.000 | 0.844 | **doctr** |
| Etat_r_sultat_2.docx | 1.000 | 0.905 | 0.691 | 0.643 | 0.333 | **rapidocr** |
| Etat_r_sultat_3.docx | 0.760 | 0.733 | 0.627 | 0.493 | 0.400 | **rapidocr** |
| bilab2.docx | 0.817 | 0.648 | 0.338 | 0.606 | 0.282 | **rapidocr** |
| bilan1.docx | 0.941 | 0.922 | 0.706 | 0.196 | 0.902 | **rapidocr** |
| bilan3.docx | 1.000 | 0.909 | 0.519 | 0.727 | 0.416 | **rapidocr** |

## Per-case sign accuracy (minus-sign / negative handling)

| case | rapidocr | doctr | paddleocr | tesseract_cli | easyocr |
|---|---|---|---|---|---|
| Bilan_1.jpg | 1.000 | 1.000 | 1.000 | 1.000 | 0.971 |
| EF_CLB_2024_Provisoire_1_.pdf | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| Etat_r_sultat_1.docx | 1.000 | 1.000 | 1.000 | — | 0.963 |
| Etat_r_sultat_2.docx | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| Etat_r_sultat_3.docx | 1.000 | 1.000 | 1.000 | 1.000 | 0.900 |
| bilab2.docx | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| bilan1.docx | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| bilan3.docx | 1.000 | 0.986 | 1.000 | 0.964 | 1.000 |

## Complementarity — oracle ensemble value accuracy

Mean (n_ref-weighted) of the BEST engine's per-case value accuracy in each set. A set whose oracle beats its best single member has ensemble headroom.

- Best single engine value accuracy: **0.8571**

| engine set | oracle value_acc | Δ vs best single |
|---|---|---|
| rapidocr+doctr | 0.859 | +0.0019 |
| rapidocr+paddleocr | 0.8571 | +0.0000 |
| rapidocr+tesseract_cli | 0.8571 | +0.0000 |
| rapidocr+easyocr | 0.8571 | +0.0000 |
| doctr+paddleocr | 0.8064 | -0.0507 |
| doctr+tesseract_cli | 0.8064 | -0.0507 |
| doctr+easyocr | 0.8064 | -0.0507 |
| paddleocr+tesseract_cli | 0.6805 | -0.1766 |
| paddleocr+easyocr | 0.6203 | -0.2368 |
| tesseract_cli+easyocr | 0.6917 | -0.1654 |
| rapidocr+doctr+paddleocr | 0.859 | +0.0019 |
| rapidocr+doctr+tesseract_cli | 0.859 | +0.0019 |
| rapidocr+doctr+easyocr | 0.859 | +0.0019 |
| rapidocr+paddleocr+tesseract_cli | 0.8571 | +0.0000 |
| rapidocr+paddleocr+easyocr | 0.8571 | +0.0000 |
| rapidocr+tesseract_cli+easyocr | 0.8571 | +0.0000 |
| doctr+paddleocr+tesseract_cli | 0.8064 | -0.0507 |
| doctr+paddleocr+easyocr | 0.8064 | -0.0507 |
| doctr+tesseract_cli+easyocr | 0.8064 | -0.0507 |
| paddleocr+tesseract_cli+easyocr | 0.7143 | -0.1428 |
| rapidocr+doctr+paddleocr+tesseract_cli+easyocr | 0.859 | +0.0019 |

> ⭐ = oracle beats the best single engine by >0.01 → genuine complementarity. Pick the smallest such set whose members are each the unique best on ≥1 case and whose runtime/RSS fit the production budget.

## Findings — full 5-engine slate (rapidocr / docTR / paddleocr / tesseract_cli / easyocr)

**rapidocr is the best single engine; no Tier-1 ensemble is justified by the evidence.**

1. **Ranking** (value_acc · det_score · latency): rapidocr 0.857 · 0.933 · 19.7 s/pg → docTR 0.806 · 0.904 · 19.4 s/pg → paddleocr 0.587 · 0.796 · **79.5 s/pg** → tesseract 0.573 · 0.754 · 14.8 s/pg → easyocr 0.534 · 0.748 · 31.8 s/pg.
2. **rapidocr wins 7/8 cases**; docTR wins the 8th (Etat_1: 1.000 vs 0.969) by a hair. paddleocr / tesseract / easyocr **never win a single case**.
3. **Complementarity is essentially nil.** The oracle ensemble (the *upper bound* of any voting scheme) tops out at **0.859** for *any* set containing rapidocr+docTR — a gain of **+0.0019** over rapidocr alone, entirely from that one Etat_1 case, and **below the 0.01 threshold**. Adding paddle/tesseract/easyocr to rapidocr+docTR changes the oracle by **0.0000**. The full 5-engine oracle = the rapidocr+docTR oracle = 0.859. Three of five engines contribute *zero*.
4. **A real voting ensemble would net-negative.** It captures *less* than the +0.0019 oracle, while doubling latency (≈39 s/pg) and RSS (≈6 GB) and risking conflict-noise pulling rapidocr's better answer toward docTR's worse one on the 7 cases rapidocr already wins. Sign accuracy is already ~perfect for the strong engines (rapidocr/paddle 1.000, docTR 0.998) — no minus-sign weakness for a partner to cover (the research's Paddle-lineage warning did not reproduce on this corpus/preprocessing).
5. **Why the literature's ensemble thesis doesn't transfer:** it is driven by *table-structure* complementarity, but our pipeline already owns structure (docling TableFormer) and already runs a **Tier-0-native ↔ Tier-1-OCR dual-tier vote** (visible in logs). The engine's only job is text recognition, where rapidocr is categorically best here.
6. **Operational notes:** paddleocr (PP-OCRv6 in paddleocr 3.7.0) only runs with `enable_mkldnn=False` — paddle 3.3.1 CPU otherwise crashes (`ConvertPirAttribute2RuntimeAttribute`, oneDNN/PIR), the research's documented Paddle version-instability — and even then is **4× slower** than rapidocr. easyocr is heaviest (5.8 GB). tesseract is fastest/lightest but unreliable (Etat_1 0.000, bilan1 0.196). _(Caveat: paddle's image pulled numpy 2.4.1→2.3.5; a minor confound, contained to the throwaway benchmark image.)_

### Recommendation
**Tier-1 = rapidocr alone** (the validated incumbent). No multi-engine Tier-1 ensemble — the maximum possible gain is +0.0019 value accuracy at 2× cost. **docTR is the validated strong #2** (det 0.904, a genuinely distinct architecture) — keep it as a documented drop-in alternative if rapidocr ever regresses, *not* as an ensemble partner. Residual rapidocr misses (value 0.857) are per-case-uniform (all engines fail the same degraded cells) → the real levers are preprocessing and the future **Tier-2 VLM**, not a second Tier-1 OCR engine. The `ensemble_vote` infrastructure stays in use for the existing Tier-0↔Tier-1 reconciliation.
