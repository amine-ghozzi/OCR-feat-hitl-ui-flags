"""PaddleOCR PP-OCRv5 OCR engine for docling — benchmark-only.

PP-OCRv5 uses newer Paddle models than rapidocr's bundled PP-OCRv4-on-ONNX, so
this checks whether the latest weights recover any of rapidocr's residual misses.
(Low architectural diversity vs rapidocr — same lineage — but the user asked for
full coverage of the slate.)

PaddleOCR's API is version-sensitive (the research flagged this); the call is
guarded so a result-shape change degrades to "no cells on this crop" rather than
aborting the whole run, and the box→page coordinate mapping mirrors RapidOcrModel.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar, List, Literal, Optional, Type

import numpy
from docling_core.types.doc import BoundingBox, CoordOrigin
from docling_core.types.doc.page import BoundingRectangle, TextCell

from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.base_models import Page
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import OcrOptions
from docling.datamodel.settings import settings
from docling.models.base_ocr_model import BaseOcrModel
from docling.utils.profiling import TimeRecorder

_log = logging.getLogger(__name__)


class PaddleOcrOptions(OcrOptions):
    """Options for the PaddleOCR PP-OCRv5 engine."""

    kind: ClassVar[Literal["finalze_paddleocr"]] = "finalze_paddleocr"
    lang: List[str] = ["fr"]


def _paddle_lines(result) -> list:
    """Normalize PaddleOCR's version-varying result into [(box4pts, text, score)].

    Handles both the classic `.ocr()` shape ([[ [box], (txt, score) ], ...]) and
    the 3.x predict shape (dict with rec_polys / rec_texts / rec_scores)."""
    out: list = []
    if result is None:
        return out
    # 3.x predict(): list of dict-like results with rec_* arrays
    first = result[0] if isinstance(result, (list, tuple)) and result else None
    if isinstance(first, dict) or hasattr(first, "get"):
        for res in result:
            polys = res.get("rec_polys") or res.get("dt_polys") or res.get("rec_boxes") or []
            texts = res.get("rec_texts") or []
            scores = res.get("rec_scores") or []
            for i, txt in enumerate(texts):
                box = polys[i] if i < len(polys) else None
                score = scores[i] if i < len(scores) else 0.0
                if box is not None:
                    out.append((box, txt, float(score)))
        return out
    # classic .ocr(): result is [page] where page = [[box, (txt, score)], ...]
    page = result[0] if isinstance(result, (list, tuple)) and result else result
    for line in page or []:
        try:
            box, (txt, score) = line[0], line[1]
            out.append((box, txt, float(score)))
        except Exception:  # noqa: BLE001
            continue
    return out


class PaddleOcrModel(BaseOcrModel):
    def __init__(
        self,
        enabled: bool,
        artifacts_path: Optional[Path],
        options: PaddleOcrOptions,
        accelerator_options: AcceleratorOptions,
    ):
        super().__init__(
            enabled=enabled,
            artifacts_path=artifacts_path,
            options=options,
            accelerator_options=accelerator_options,
        )
        self.options: PaddleOcrOptions
        self.scale = 3

        if self.enabled:
            try:
                from paddleocr import PaddleOCR  # type: ignore
            except ImportError:
                raise ImportError(
                    "paddleocr is not installed. `pip install paddlepaddle paddleocr`."
                )
            lang = (self.options.lang or ["fr"])[0]
            # enable_mkldnn=False is REQUIRED on paddlepaddle 3.3.1 CPU: oneDNN's
            # PIR executor raises NotImplementedError (ConvertPirAttribute2Runtime
            # Attribute) on these OCR models — the research's documented paddle CPU
            # version-instability. With mkldnn off, inference runs (PP-OCRv6 models
            # in paddleocr 3.7.0). 'fr'/'french' both map to the latin model.
            for kw in (
                dict(lang=lang, enable_mkldnn=False),
                dict(use_angle_cls=True, lang=lang, show_log=False),  # classic 2.x
                dict(lang=lang),
            ):
                try:
                    self.reader = PaddleOCR(**kw)
                    break
                except TypeError:
                    continue

    def _run(self, im):
        # Prefer .predict (3.x); fall back to .ocr (classic).
        if hasattr(self.reader, "predict"):
            try:
                return self.reader.predict(im)
            except Exception:  # noqa: BLE001
                pass
        try:
            return self.reader.ocr(im, cls=True)
        except TypeError:
            return self.reader.ocr(im)

    def __call__(
        self, conv_res: ConversionResult, page_batch: Iterable[Page]
    ) -> Iterable[Page]:
        if not self.enabled:
            yield from page_batch
            return

        for page in page_batch:
            assert page._backend is not None
            if not page._backend.is_valid():
                yield page
                continue
            with TimeRecorder(conv_res, "ocr"):
                ocr_rects = self.get_ocr_rects(page)
                all_ocr_cells: List[TextCell] = []
                for ocr_rect in ocr_rects:
                    if ocr_rect.area() == 0:
                        continue
                    hi = page._backend.get_page_image(scale=self.scale, cropbox=ocr_rect)
                    im = numpy.array(hi)
                    try:
                        result = self._run(im)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(f"PaddleOCR failed on a crop: {exc}")
                        continue
                    del hi, im
                    for ix, (box, txt, score) in enumerate(_paddle_lines(result)):
                        try:
                            xs = [float(p[0]) for p in box]
                            ys = [float(p[1]) for p in box]
                            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
                        except Exception:  # noqa: BLE001
                            continue
                        all_ocr_cells.append(TextCell(
                            index=ix,
                            text=txt,
                            orig=txt,
                            confidence=score,
                            from_ocr=True,
                            rect=BoundingRectangle.from_bounding_box(
                                BoundingBox.from_tuple(
                                    coord=(
                                        x0 / self.scale + ocr_rect.l,
                                        y0 / self.scale + ocr_rect.t,
                                        x1 / self.scale + ocr_rect.l,
                                        y1 / self.scale + ocr_rect.t,
                                    ),
                                    origin=CoordOrigin.TOPLEFT,
                                )
                            ),
                        ))
                self.post_process_cells(all_ocr_cells, page)
            if settings.debug.visualize_ocr:
                self.draw_ocr_rects_and_cells(conv_res, page, ocr_rects)
            yield page

    @classmethod
    def get_options_type(cls) -> Type[OcrOptions]:
        return PaddleOcrOptions
