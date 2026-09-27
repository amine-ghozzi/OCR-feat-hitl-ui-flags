"""docTR (Mindee) OCR engine for docling — benchmark-only.

docTR is the only Tier-1 candidate from a genuinely different architecture
family (DBNet detection + CRNN/ViT recognition, not Paddle-lineage), so it is
the best remaining shot at covering rapidocr's residual per-case misses.

Mirrors docling's RapidOcrModel.__call__ exactly — same get_ocr_rects → high-res
crop → recognize → TextCell(bbox) → post_process_cells flow — swapping only the
engine call and the coordinate mapping (docTR returns RELATIVE geometry).
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


class DoctrOcrOptions(OcrOptions):
    """Options for the docTR engine."""

    kind: ClassVar[Literal["finalze_doctr"]] = "finalze_doctr"
    lang: List[str] = ["fr"]  # docTR is script-based; lang kept for interface parity


class DoctrOcrModel(BaseOcrModel):
    def __init__(
        self,
        enabled: bool,
        artifacts_path: Optional[Path],
        options: DoctrOcrOptions,
        accelerator_options: AcceleratorOptions,
    ):
        super().__init__(
            enabled=enabled,
            artifacts_path=artifacts_path,
            options=options,
            accelerator_options=accelerator_options,
        )
        self.options: DoctrOcrOptions
        self.scale = 3  # 72dpi * 3 = 216 dpi, matching RapidOcrModel

        if self.enabled:
            try:
                from doctr.models import ocr_predictor  # type: ignore
            except ImportError:
                raise ImportError(
                    "python-doctr is not installed. `pip install python-doctr[torch]`."
                )
            # CPU predictor; detect + recognize, straight pages, assume vertical text off.
            self.predictor = ocr_predictor(
                pretrained=True, assume_straight_pages=True, detect_orientation=False
            )

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
                    W, H = hi.size
                    im = numpy.array(hi)
                    try:
                        doc = self.predictor([im])
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(f"docTR failed on a crop: {exc}")
                        continue
                    del hi, im
                    ix = 0
                    for pg in doc.pages:
                        for block in pg.blocks:
                            for line in block.lines:
                                for word in line.words:
                                    (xmin, ymin), (xmax, ymax) = word.geometry
                                    # geometry is RELATIVE to the crop image
                                    all_ocr_cells.append(TextCell(
                                        index=ix,
                                        text=word.value,
                                        orig=word.value,
                                        confidence=float(word.confidence),
                                        from_ocr=True,
                                        rect=BoundingRectangle.from_bounding_box(
                                            BoundingBox.from_tuple(
                                                coord=(
                                                    (xmin * W) / self.scale + ocr_rect.l,
                                                    (ymin * H) / self.scale + ocr_rect.t,
                                                    (xmax * W) / self.scale + ocr_rect.l,
                                                    (ymax * H) / self.scale + ocr_rect.t,
                                                ),
                                                origin=CoordOrigin.TOPLEFT,
                                            )
                                        ),
                                    ))
                                    ix += 1
                self.post_process_cells(all_ocr_cells, page)
            if settings.debug.visualize_ocr:
                self.draw_ocr_rects_and_cells(conv_res, page, ocr_rects)
            yield page

    @classmethod
    def get_options_type(cls) -> Type[OcrOptions]:
        return DoctrOcrOptions
