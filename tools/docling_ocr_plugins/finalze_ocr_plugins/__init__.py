"""Benchmark-only docling OCR engine plugins (docTR, PaddleOCR PP-OCRv5).

Registered with docling's external-plugin factory (entry-point group `docling`,
loaded only when `allow_external_plugins=True`). These exist solely to evaluate
two more Tier-1 candidates on the FinAlze corpus through the SAME docling
layout/TableFormer path as the built-in engines, so the numbers are comparable.
NOT a production path — the production worker carries only the selected engine.
"""

from .doctr_model import DoctrOcrModel, DoctrOcrOptions
from .paddle_model import PaddleOcrModel, PaddleOcrOptions

__all__ = ["DoctrOcrModel", "DoctrOcrOptions", "PaddleOcrModel", "PaddleOcrOptions"]


def ocr_engines():
    """docling plugin factory hook (group `docling`)."""
    return {"ocr_engines": [DoctrOcrModel, PaddleOcrModel]}
