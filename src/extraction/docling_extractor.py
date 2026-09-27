"""
Docling-based table extraction for financial documents.

Uses DoclingTableFormer for accurate table structure recognition
with EasyOCR for French text recognition.
"""

import logging
import math
from pathlib import Path
from typing import List, Optional, Tuple
from dataclasses import dataclass, field
import io

from PIL import Image, ImageOps
import pandas as pd

from .pdf_orientation import normalize_rotation

logger = logging.getLogger(__name__)

# TIFF/EXIF "Orientation" (274). Values 1-8; 1 means "already upright".
EXIF_ORIENTATION_TAG = 0x0112

# The four-word grade vocabulary the pipeline speaks, worst first. Docling's
# ``QualityGrade`` is a lowercase str-Enum with a FIFTH member, "unspecified",
# which is not a grade at all — it is what ``PageConfidenceScores`` returns when
# every score it was asked to average is NaN. Anything outside the four words is
# dropped rather than guessed at.
GRADE_ORDER = {"POOR": 0, "FAIR": 1, "GOOD": 2, "EXCELLENT": 3}

# RTE-4 — the empty-read floor. Both are floors on EVIDENCE, not bands on a
# score: no routing threshold is involved and nothing is rejected because of
# them. They are deliberately set where only a document that read (nearly)
# nothing can reach them, because the floor's job is to catch the file Docling
# grades well and the pipeline read nothing off — not to second-guess a merely
# mediocre read, which the QCS already scores.
EMPTY_REGION_FLOOR = 0.98   # share of detected table cells that are blank
OCR_COVERAGE_FLOOR = 0.50   # share of OCR-only pages that yielded any text


def _exif_orientation(image: Image.Image) -> Optional[int]:
    """The EXIF Orientation tag of *image* (1-8), or None when it carries none.

    Fail-open on purpose: a truncated or malformed EXIF block must never stop an
    extraction, and "no readable tag" means exactly what "no tag" means — treat
    the pixel buffer as already upright.
    """
    try:
        exif = image.getexif()
    except Exception:  # noqa: BLE001 — a broken EXIF block is not an extraction failure
        logger.debug("Could not read EXIF from image", exc_info=True)
        return None
    if not exif:
        return None
    try:
        value = int(exif.get(EXIF_ORIENTATION_TAG))
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 8 else None


@dataclass
class TableExtractionResult:
    """Result from table extraction."""
    tables: List[pd.DataFrame] = field(default_factory=list)
    text: str = ""
    confidence: float = 0.0
    layout_score: float = 0.0
    ocr_score: float = 0.0
    corrections_made: int = 0  # For QCS penalty calculation
    # Grade-mapped fields (Docling grades recommended over raw scores)
    ocr_grade: str = "FAIR"
    layout_grade: str = "FAIR"
    parse_grade: str = "FAIR"
    low_grade: str = "FAIR"  # Worst page grade — POOR triggers HITL pre-gate
    metadata: dict = field(default_factory=dict)


class DoclingExtractor:
    """
    Docling-based document extractor with TableFormer.
    
    Optimized for French financial documents:
    - Balance sheets (Bilan)
    - Income statements (Compte de Résultat)
    - Cash flow statements (Flux de Trésorerie)
    """
    
    # Class-level singleton for converter (shared across all instances)
    _shared_converter = None
    _shared_converter_threshold = None  # Track threshold used for singleton

    def __init__(
        self,
        language: str = "fr",
        confidence_threshold: float = 0.5,
        use_accurate_mode: bool = True,
        preprocess_resize: bool = True,
        preprocess_grayscale: bool = True,
        max_image_width: int = 2000,
        accounting_standard: str = "NCT"
    ):
        self.language = language
        self.confidence_threshold = confidence_threshold
        self.use_accurate_mode = use_accurate_mode
        self.preprocess_resize = preprocess_resize
        self.preprocess_grayscale = preprocess_grayscale
        self.max_image_width = max_image_width
        self.accounting_standard = accounting_standard

    @classmethod
    def _get_converter(cls, confidence_threshold: float = 0.5):
        """Lazy-load the Docling converter (class-level singleton).

        Passes pipeline options including OCR confidence threshold.
        Reinitializes if threshold changes.
        """
        if cls._shared_converter is not None and cls._shared_converter_threshold == confidence_threshold:
            return cls._shared_converter

        logger.info("Initializing Docling converter (first run may download models)...")

        from docling.document_converter import DocumentConverter

        # OCR engine is PARAMETRIZED, not whatever-is-installed: Docling's
        # auto-detection picks EasyOCR whenever the package is importable,
        # which produces materially different reads on scanned documents than
        # the RapidOCR backend the test suite + extraction benchmark validated
        # (observed: '-' read as '2'/'"', '%' read as '8'). Default is
        # therefore the validated engine; override with FINALZE_OCR_ENGINE =
        # rapidocr | easyocr | tesseract | tesseract_cli | doctr | paddleocr | auto.
        # (doctr/paddleocr are benchmark-only external docling plugins.)
        import os
        engine = os.environ.get("FINALZE_OCR_ENGINE", "rapidocr").strip().lower()
        converter = None
        if engine and engine != "auto":
            try:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import (
                    EasyOcrOptions,
                    PdfPipelineOptions,
                    RapidOcrOptions,
                    TesseractCliOcrOptions,
                    TesseractOcrOptions,
                )
                from docling.document_converter import (
                    ImageFormatOption,
                    PdfFormatOption,
                )
                # The corpus is French — engines that take a language MUST be
                # told so (Tesseract defaults to English and butchers accents).
                # Codes are engine-native (tesseract: 3-letter, easyocr: 2-letter).
                # RapidOCR keeps its validated default (no lang kwarg) so the
                # 0.9062 baseline config is preserved exactly.
                lang_by_engine = {
                    "easyocr": ["fr", "en"],
                    "tesseract": ["fra", "eng"],
                    "tesseract_cli": ["fra", "eng"],
                    "doctr": ["fr"],
                    "paddleocr": ["fr"],
                }
                pipe = PdfPipelineOptions()
                pipe.do_ocr = True
                pipe.do_table_structure = True

                external = engine in ("doctr", "paddleocr")
                if external:
                    # Benchmark-only external plugins (tools/docling_ocr_plugins).
                    from finalze_ocr_plugins import DoctrOcrOptions, PaddleOcrOptions
                    ext_cls = {"doctr": DoctrOcrOptions, "paddleocr": PaddleOcrOptions}[engine]
                    pipe.allow_external_plugins = True
                    pipe.ocr_options = ext_cls(lang=lang_by_engine[engine])
                else:
                    ocr_cls = {
                        "rapidocr": RapidOcrOptions,
                        "easyocr": EasyOcrOptions,
                        "tesseract": TesseractOcrOptions,
                        "tesseract_cli": TesseractCliOcrOptions,
                    }.get(engine)
                    if ocr_cls is None:
                        raise ValueError(f"unknown FINALZE_OCR_ENGINE {engine!r}")
                    opts_kwargs = {}
                    if engine in lang_by_engine:
                        opts_kwargs["lang"] = lang_by_engine[engine]
                    pipe.ocr_options = ocr_cls(**opts_kwargs)

                converter = DocumentConverter(format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipe),
                    InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipe),
                })
                logger.info(f"Docling OCR engine: {engine} (FINALZE_OCR_ENGINE)")
            except Exception as exc:
                logger.warning(
                    f"OCR engine {engine!r} wiring failed ({exc}) — "
                    "falling back to Docling auto-detection"
                )
                converter = None
        if converter is None:
            converter = DocumentConverter()
            logger.info("Docling OCR engine: auto (Docling default selection)")

        cls._shared_converter = converter
        cls._shared_converter_threshold = confidence_threshold
        logger.info("Docling converter initialized (singleton)")

        return cls._shared_converter
    
    def _preprocess_image(self, image: Image.Image) -> Tuple[Image.Image, dict]:
        """
        Preprocess image for OCR: EXIF orientation first, then resize/grayscale.

        OCR-4: PIL hands back the pixel buffer exactly as stored and never
        consults the EXIF Orientation tag, so an orientation-6 JPEG — a phone
        photo of a statement, the common scanned-image case — reached OCR
        rotated 90 degrees and extracted as noise. ``ImageOps.exif_transpose``
        runs FIRST, before the resize, so ``max_image_width`` is measured on the
        upright image and the ``table_boxes`` geometry is in the upright frame.

        This is the single choke point for every image ingress —
        ``extract_from_image`` (which the pipeline's image branch and the DOCX
        path both go through) — so the transpose is applied exactly once, here.

        Args:
            image: PIL Image

        Returns:
            ``(image, record)``: the preprocessed image and
            ``{"exif_orientation": n|None, "transposed": bool}``. The record
            travels to ``metadata["orientation_transform"]["images"]`` so the
            HITL source-document overlay can tell that the geometry it is drawing
            is not in the stored pixel frame.
        """
        orientation = _exif_orientation(image)
        transposed = False
        if orientation is not None and orientation != 1:
            try:
                upright = ImageOps.exif_transpose(image)
            except Exception:  # noqa: BLE001 — never fail an extraction over EXIF
                logger.warning("EXIF transpose failed — extracting the stored orientation",
                               exc_info=True)
            else:
                if upright is not None:
                    logger.info(
                        f"Applied EXIF orientation {orientation}: {image.size} -> {upright.size}"
                    )
                    image = upright
                    transposed = True
        orientation_record = {"exif_orientation": orientation, "transposed": transposed}

        original_size = image.size

        # Resize if image is too large
        if self.preprocess_resize and image.width > self.max_image_width:
            ratio = self.max_image_width / image.width
            new_height = int(image.height * ratio)
            image = image.resize((self.max_image_width, new_height), Image.Resampling.LANCZOS)
            logger.info(f"Resized image from {original_size} to {image.size}")
        
        # Convert to grayscale for faster processing
        if self.preprocess_grayscale and image.mode != 'L':
            image = image.convert('L')
            logger.info("Converted image to grayscale")

        return image, orientation_record

    def extract_from_pdf(self, pdf_path: Path) -> TableExtractionResult:
        """
        Extract tables and text from a PDF.

        Args:
            pdf_path: Path to PDF file

        Returns:
            TableExtractionResult with extracted data

        OCR-1/2/3: docling-parse returns no OCR rectangles for /Rotate 180|270
        pages (or for any inherited /Rotate), so Docling runs against a
        de-rotated temporary COPY when one is needed. The original file is never
        rewritten, and the transform is recorded in
        ``metadata["orientation_transform"]`` so the HITL overlay knows the
        geometry is not in the original page frame.
        """
        converter = self._get_converter(self.confidence_threshold)

        logger.info(f"Extracting from PDF: {pdf_path}")

        source_path, orientation_transform = normalize_rotation(pdf_path)
        # Path() on both sides: a caller passing a str must never make the
        # ORIGINAL look like a temp copy below.
        is_temp_copy = Path(source_path) != Path(pdf_path)
        try:
            result = converter.convert(str(source_path))
            return self._process_result(result, orientation_transform=orientation_transform)
        finally:
            if is_temp_copy:
                # The helper hands the temp copy over; it is ours to remove on
                # every path, including the one where Docling raised.
                try:
                    source_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(f"Could not remove de-rotation temp file {source_path}",
                                   exc_info=True)
    
    def extract_from_image(self, image: Image.Image) -> TableExtractionResult:
        """
        Extract tables and text from an image.
        
        Args:
            image: PIL Image

        Returns:
            TableExtractionResult with extracted data

        OCR-4: the EXIF transpose happens inside ``_preprocess_image``, so every
        caller of this method (the pipeline's image branch, ``extract_from_docx``)
        gets an upright page without knowing about it. What the transpose did is
        recorded in ``metadata["orientation_transform"]["images"]``.
        """
        converter = self._get_converter(self.confidence_threshold)

        # Preprocess image for faster OCR (EXIF orientation, resize, grayscale)
        image, orientation_record = self._preprocess_image(image)

        # Save image temporarily. NamedTemporaryFile(delete=False) + reopen is
        # the Windows-safe idiom; `save` belongs INSIDE the try, because a save
        # that raises (disk full, an unsupported mode) used to leave the empty
        # temp file behind forever.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            temp_path = Path(f.name)

        try:
            image.save(temp_path)
            result = converter.convert(str(temp_path))
            return self._process_result(
                result,
                orientation_transform={"images": [orientation_record]},
            )
        finally:
            temp_path.unlink(missing_ok=True)

    def extract_from_docx(self, docx_path: Path) -> TableExtractionResult:
        """
        Extract tables from DOCX with embedded images.
        
        For DOCX files containing screenshots (not native tables),
        we extract images and process each separately.
        """
        from docx import Document
        
        logger.info(f"Extracting from DOCX: {docx_path}")
        
        doc = Document(docx_path)
        all_tables = []
        all_text = []
        all_table_boxes = []   # per-table geometry, tagged with its embedded-image index
        all_orientations = []  # OCR-4: per-embedded-image EXIF transpose record
        all_confidences = []   # RTE-3: one Docling confidence payload per image
        raw_parts = []
        native_raw_tables = []
        all_read_floors = []   # RTE-4: one empty-read floor per image
        total_confidence = 0.0
        image_count = 0

        # Extract embedded images
        for rel in doc.part.rels.values():
            if "image" in rel.reltype:
                img_idx = image_count   # 0-based, matches the HITL embedded-image order
                image_count += 1
                try:
                    image_data = rel.target_part.blob
                    image = Image.open(io.BytesIO(image_data))

                    logger.info(f"Processing embedded image {image_count}")
                    result = self.extract_from_image(image)

                    all_tables.extend(result.tables)
                    all_text.append(result.text)
                    raw_parts.append((result.metadata or {}).get("raw_snapshot") or {})
                    total_confidence += result.confidence
                    # Keep geometry index-aligned with all_tables, tagging which
                    # embedded image each table (hence its boxes) came from — the
                    # HITL source view renders these same images in this order.
                    img_boxes = (result.metadata or {}).get("table_boxes") or []
                    for ti in range(len(result.tables)):
                        g = img_boxes[ti] if ti < len(img_boxes) else None
                        if g is not None:
                            g = {**g, "image_index": img_idx}
                        all_table_boxes.append(g)

                    # OCR-4: same provenance convention as the geometry above —
                    # the transpose record is per embedded image, so it carries
                    # the index the HITL source view renders these images in.
                    img_orient = ((result.metadata or {}).get("orientation_transform")
                                  or {}).get("images") or []
                    for rec in img_orient:
                        all_orientations.append({**rec, "image_index": img_idx})

                    # RTE-3/RTE-4: each embedded image is converted on its own,
                    # so each carries its own confidence report and its own read
                    # evidence. Collect them; they are merged into one document
                    # answer below rather than thrown away, which is what left
                    # every DOCX reporting the "FAIR" defaults.
                    all_confidences.append(
                        (result.metadata or {}).get("docling_confidence"))
                    all_read_floors.append((result.metadata or {}).get("read_floor"))

                except Exception as e:
                    logger.error(f"Failed to process image {image_count}: {e}")

        # Also extract any native text
        for para in doc.paragraphs:
            if para.text.strip():
                all_text.append(para.text)

        import pandas as pd
        from document_intake import snapshot
        for table in doc.tables:
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            if rows:
                frame = pd.DataFrame(rows)
                native_raw_tables.append(frame.copy(deep=True))
                all_tables.append(frame)
                all_table_boxes.append(None)
                all_text.append("\n".join(" | ".join(row) for row in rows))
        native_text = "\n".join([p.text for p in doc.paragraphs] +
                                [" | ".join(str(v) for v in row)
                                 for frame in native_raw_tables for row in frame.to_numpy().tolist()])
        raw_snapshot = snapshot(native_text, native_raw_tables, "docx_native")
        # Preserve independent image snapshots; do not pretend table indices match
        # the canonical/ensemble output.
        raw_snapshot["parts"] = raw_parts
        raw_snapshot["text"] += "\n" + "\n".join(p.get("text", "") for p in raw_parts)
        import hashlib, json
        raw_snapshot.pop("sha256", None)
        raw_snapshot["sha256"] = hashlib.sha256(json.dumps(
            raw_snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        avg_confidence = total_confidence / max(image_count, 1)

        # RTE-3/RTE-4 — the bundle's answer, from its images' answers. RECORDED
        # only: the grade fields of the result below keep their "FAIR" defaults,
        # exactly as they did before WP-2.8, because those are what the routing
        # QCS reads. See the note in `_process_result`.
        docx_confidence = _merge_confidence(all_confidences)
        docx_read_floor = _merge_read_floor(all_read_floors)

        return TableExtractionResult(
            tables=all_tables,
            text="\n".join(all_text),
            confidence=avg_confidence,
            metadata={
                "image_count": image_count,
                "source": "docx_embedded",
                "raw_snapshot": raw_snapshot,
                "quality_report_available": docx_confidence is not None,
                "docling_confidence": docx_confidence,
                "read_floor": docx_read_floor,
                "table_boxes": all_table_boxes,  # HITL source-doc overlay geometry
                # OCR-4: one record per embedded image that reached OCR; same
                # shape as the single-image path, plus `image_index`.
                "orientation_transform": {"images": all_orientations},
            }
        )
    
    @staticmethod
    def _raw_score_to_grade(score: float) -> str:
        """Map a raw 0-1 float score to a Docling-style grade string.

        Used as fallback when Docling's quality report grades are unavailable.
        """
        if score >= 0.85:
            return "EXCELLENT"
        elif score >= 0.65:
            return "GOOD"
        elif score >= 0.40:
            return "FAIR"
        else:
            return "POOR"

    def _process_result(
        self,
        docling_result,
        orientation_transform: Optional[dict] = None,
    ) -> TableExtractionResult:
        """Process Docling result into our format with text post-processing.

        ``orientation_transform`` records every orientation change applied before
        extraction, because ``table_boxes`` are then in the CHANGED frame, not the
        frame of the file on disk. It has one key per ingress:

        * PDF (OCR-1/2/3) — the ``pdf_orientation.normalize_rotation`` record
          (``applied``/``pages``/``skipped_pages``/``temp_path``), None when the
          document was extracted as-is.
        * images (OCR-4) — ``{"images": [{"exif_orientation": n|None,
          "transposed": bool}, …]}``, one entry per image that reached OCR (the
          DOCX path adds ``image_index``).

        The two never appear together: a document is a PDF or it is images.
        """
        tables = []

        # Import post-processor
        try:
            from preprocessing.text_postprocessor import postprocess_text_with_stats, postprocess_dataframe
            has_postprocessor = True
        except ImportError:
            has_postprocessor = False
            logger.warning("Text post-processor not available")

        # Track total corrections for QCS penalty
        total_corrections = 0
        correction_stats = {}

        # Extract tables as DataFrames
        doc = docling_result.document

        # Collect table confidence scores
        table_confidences = []
        # Per-table cell geometry (for the HITL source-document overlay), kept
        # index-aligned with ``tables``. None when a table has no geometry.
        table_boxes = []
        raw_tables = []

        for table in doc.tables:
            try:
                df = table.export_to_dataframe(doc=doc)
                raw_tables.append(df.copy(deep=True))

                # Docling can emit DUPLICATE column headers (e.g. an unnamed
                # label column AND a value column whose header OCR'd to "").
                # Duplicates break every df[col] consumer downstream (rule
                # engine, enrichment, the HITL grid) — dedupe at the boundary:
                # first occurrence keeps its name, repeats get a ".n" suffix.
                seen = {}
                cols = []
                for c in df.columns:
                    key = str(c)
                    n = seen.get(key, 0)
                    cols.append(key if n == 0 else f"{key}.{n}")
                    seen[key] = n + 1
                df.columns = cols

                # Apply text post-processing to fix missing spaces
                if has_postprocessor:
                    postprocess_dataframe(df, self.accounting_standard)

                tables.append(df)
                table_boxes.append(_table_geometry(table, doc))

                # Try to get table-level confidence from prov (provenance)
                if hasattr(table, 'prov') and table.prov:
                    for prov in table.prov:
                        if hasattr(prov, 'confidence') and prov.confidence is not None:
                            table_confidences.append(prov.confidence)

            except Exception as e:
                logger.warning(f"Failed to export table: {e}")

        # Extract text
        text = doc.export_to_markdown()
        from document_intake import snapshot
        raw_snapshot = snapshot(text, raw_tables, "docling")

        # Per-page text for Stage 3b (WP-2.9b, VAL-7/E-2). The single markdown
        # string above drops the page boundaries, so a table could only ever be
        # matched against the WHOLE document's text; a caption is only a caption
        # on the page it was written on.
        #
        # Deliberately taken BEFORE the post-processor and left unprocessed.
        # `smart_correct` fuzzy-matches against an accounting vocabulary that
        # has no "DT" in it, and it rewrites "(en DT)" to "(en D)" — measured on
        # this build: "Bilan arrêté au 31 décembre 2024 (en DT)" comes back as
        # "… (en D)", which matches no unit pattern at all. Post-processing the
        # page text would therefore DESTROY the very statement Stage 3b is here
        # to read. `text` below keeps its post-processing unchanged; this is the
        # narrower, verbatim copy the unit detector reads.
        page_texts = _page_texts(doc)

        # Post-process text and track corrections for QCS penalty
        if has_postprocessor:
            text, correction_stats = postprocess_text_with_stats(text, self.accounting_standard)
            total_corrections = correction_stats.get("total_corrections", 0)
            logger.info(f"Post-processing made {total_corrections} corrections")

        # =============================================================
        # Extract confidence scores and grades from Docling
        # =============================================================
        ocr_scores = []
        layout_scores = []

        # Check content items for scores
        for item in doc.texts:
            if hasattr(item, 'prov') and item.prov:
                for prov in item.prov:
                    if hasattr(prov, 'confidence') and prov.confidence is not None:
                        ocr_scores.append(prov.confidence)

        # Also check body for additional provenance
        if hasattr(doc, 'body') and doc.body:
            if hasattr(doc.body, 'children'):
                for child in doc.body.children:
                    if hasattr(child, 'prov') and child.prov:
                        for prov in child.prov:
                            if hasattr(prov, 'confidence') and prov.confidence is not None:
                                layout_scores.append(prov.confidence)

        # Calculate average scores
        avg_ocr = sum(ocr_scores) / len(ocr_scores) if ocr_scores else 0.0
        avg_layout = sum(layout_scores) / len(layout_scores) if layout_scores else 0.0

        # Combined confidence
        all_scores = ocr_scores + layout_scores + table_confidences
        avg_confidence = sum(all_scores) / len(all_scores) if all_scores else 0.0

        # =============================================================
        # Grades
        #
        # TWO SETS, AND ONLY ONE OF THEM ROUTES.
        #
        # The four fields of ``TableExtractionResult`` below are the grades the
        # QCS that drives the cascade is computed from, and WP-2.8 does not
        # touch them: they are still derived from the raw provenance averages,
        # which in docling 2.70 yield nothing, so they are still the "FAIR"
        # defaults. Docling's real grades are READ and RECORDED in
        # ``metadata["docling_confidence"]`` a few lines down, and the pipeline
        # computes a second, unused QCS from them.
        #
        # That split is deliberate and it is the whole shape of this package.
        # The plan's ruling on RTE-3 is "record now, adopt into routing in P7
        # (WP-7.2, after MEA-7 calibration)", and decisions Q5-A and G-4 say no
        # automation widens before it has been measured. Wiring the real grades
        # into the routing score was measured on the corpus and it MOVES
        # ROUTING: two PDFs stop reaching the Tier-3 two-engine vote (one of
        # them extracting measurably worse alone) and two DOCX cross the 0.90
        # no-human bar and lose their review flag. That measurement is the input
        # to P7; it is not this package's decision to act on.
        # =============================================================
        ocr_grade = "FAIR"
        layout_grade = "FAIR"
        parse_grade = "FAIR"
        low_grade = "FAIR"

        if avg_ocr > 0:
            ocr_grade = self._raw_score_to_grade(avg_ocr)
        if avg_layout > 0:
            layout_grade = self._raw_score_to_grade(avg_layout)
        # parse_grade stays FAIR (no raw parse score available from provenance)
        # low_grade: the worst of ocr and layout
        low_grade = min(ocr_grade, layout_grade,
                        key=lambda gr: GRADE_ORDER.get(gr, 1))

        # RTE-3 — what Docling actually says. ``confidence`` hangs off the
        # CONVERSION RESULT, which is why the old probe — three attribute names
        # on ``doc``, then four per-signal ``*_grade`` attributes that do not
        # exist anywhere in docling — returned None on every document ever
        # processed and ``quality_report_available`` recorded False every time.
        confidence = _confidence_payload(docling_result)

        logger.info(
            "Grades in use (routing): ocr=%s layout=%s parse=%s low=%s · "
            "Docling reports %s — recorded, not routed (P7/WP-7.2 owns adoption)",
            ocr_grade, layout_grade, parse_grade, low_grade,
            (confidence or {}).get("grades") or "nothing",
        )
        logger.debug(f"Confidence scores - OCR: {avg_ocr:.3f}, Layout: {avg_layout:.3f}")

        # RTE-4 — the empty-read floor, from per-page EVIDENCE and never from a
        # grade: Docling can grade a page it read nothing off perfectly well.
        read_floor = _read_floor(page_texts, table_boxes, confidence)
        if read_floor["flagged"]:
            logger.warning(
                "Empty-read floor flagged: %d/%d page(s) without text, "
                "ocr_coverage=%s, empty_region_share=%s — flagging for review "
                "(the extraction is returned, not rejected)",
                read_floor["pages_without_text"], read_floor["pages_total"],
                read_floor["ocr_coverage"], read_floor["empty_region_share"],
            )

        return TableExtractionResult(
            tables=tables,
            text=text,
            confidence=avg_confidence,
            ocr_score=avg_ocr,
            layout_score=avg_layout,
            corrections_made=total_corrections,
            ocr_grade=ocr_grade,
            layout_grade=layout_grade,
            parse_grade=parse_grade,
            low_grade=low_grade,
            metadata={
                "table_count": len(tables),
                "raw_snapshot": raw_snapshot,
                "postprocessed": has_postprocessor,
                "accounting_standard": self.accounting_standard,
                "ocr_samples": len(ocr_scores),
                "layout_samples": len(layout_scores),
                "table_samples": len(table_confidences),
                "correction_stats": correction_stats,
                # True whenever Docling handed us a confidence report. It read
                # False on every document before WP-2.8, because the probe was
                # aimed at the wrong object (RTE-3).
                "quality_report_available": confidence is not None,
                # {scores, grades, pages} — what Docling says about how well it
                # read this document. RECORDED and fed to the QCS's intrinsic
                # layer, which is all it does: adopting the grades into routing
                # BANDS is P7 (WP-7.2), after calibration.
                "docling_confidence": confidence,
                # RTE-4 — {pages_total, image_only_pages, pages_without_text,
                # cells_total, cells_blank, empty_region_share, ocr_coverage,
                # flagged}. A FLAG for the reviewer, never a rejection.
                "read_floor": read_floor,
                "table_boxes": table_boxes,  # HITL source-doc overlay geometry
                # Per-page text keyed by 1-based page number (WP-2.9b). Stage 3b
                # reads the unit stated in the caption above a table off the page
                # that table sits on; `table_boxes` carries the page number.
                # Empty when the document exposes no per-page provenance.
                "page_texts": page_texts,
                # None unless a de-rotated copy was extracted (OCR-1/2/3); when
                # set, `table_boxes` are in the de-rotated frame.
                "orientation_transform": orientation_transform,
            }
        )


def _finite(value) -> Optional[float]:
    """*value* as a float when it is a real number, else None.

    Docling's ``PageConfidenceScores`` fields default to ``np.nan`` and stay
    there when nothing writes them: ``table_score`` has no writer in docling
    2.70 at all, and ``ocr_score`` is NaN for a page whose text came from the
    PDF's own text layer rather than from OCR (``layout_model`` averages the
    confidence of the cells ``from_ocr``, and there were none). NaN means "not
    measured", not "measured zero", and the difference decides whether a grade
    may be derived from it at all — mapping a NaN through a score→grade table
    would silently invent a POOR.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _grade_name(value) -> Optional[str]:
    """A Docling ``QualityGrade`` (or any grade-ish value) as one of our four words.

    ``QualityGrade`` is a ``str`` Enum whose values are LOWERCASE ("good"), so
    the uppercase membership test this module and the QCS pre-gate both perform
    (``low_grade == "POOR"``) could never have matched one. Normalise at the
    boundary, once. "unspecified" — docling's answer when every score was NaN —
    is not one of the four and comes back as None, which leaves the caller's
    default in place instead of inventing a grade.
    """
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip().upper()
    return text if text in GRADE_ORDER else None


def _score_grade(score: Optional[float]) -> Optional[str]:
    """A raw 0-1 score as one of the four grades, or None when unmeasured.

    Uses ``qcs.calculator.raw_score_to_grade`` — the same mapping the rest of
    the pipeline grades by (Tier 3's Q_table included), so one vocabulary with
    one set of bands. Docling's internal ``_score_to_grade`` uses different cut
    points; adopting those is a calibration question and belongs to P7.
    """
    if score is None:
        return None
    try:
        from qcs.calculator import raw_score_to_grade  # noqa: PLC0415 — optional dep
    except ImportError:
        return DoclingExtractor._raw_score_to_grade(score)
    return str(raw_score_to_grade(score)).upper()


def _confidence_payload(docling_result) -> Optional[dict]:
    """``ConversionResult.confidence`` read into ``{scores, grades, pages}`` (RTE-3).

    Docling 2.70 exposes a ``ConfidenceReport`` on the CONVERSION RESULT — not
    on ``.document``, which is where this extractor used to look. The report
    carries four per-signal SCORES (``ocr_score``, ``layout_score``,
    ``parse_score``, ``table_score``), two aggregate GRADES (``mean_grade``,
    ``low_grade``) and the same shape again per page under ``pages``. There are
    no per-signal ``*_grade`` attributes anywhere in docling, which is the other
    half of why the old probe found nothing: even had it been aimed at the right
    object, every name it asked for was wrong.

    ``table_score`` has no writer in this build and is always NaN; it is
    recorded as None and — like every other unmeasured signal — yields no grade.
    ``calculate_intrinsic_score`` excludes it regardless.

    Returns None when the object carries no confidence report at all (an older
    docling, a stub, a hand-built result in a test), which is the caller's
    signal to keep its fallback grades.
    """
    report = getattr(docling_result, "confidence", None)
    if report is None:
        return None

    def _signals(obj) -> dict:
        return {
            "ocr": _finite(getattr(obj, "ocr_score", None)),
            "layout": _finite(getattr(obj, "layout_score", None)),
            "parse": _finite(getattr(obj, "parse_score", None)),
            "table": _finite(getattr(obj, "table_score", None)),
        }

    def _aggregates(obj) -> dict:
        # `mean_score`/`low_score` are computed properties over NaN-bearing
        # arrays: numpy raises on an all-NaN slice rather than returning one.
        out = {}
        for key, attr in (("mean", "mean_score"), ("low", "low_score")):
            try:
                out[key] = _finite(getattr(obj, attr, None))
            except Exception:  # noqa: BLE001 — a diagnostic may never break extraction
                out[key] = None
        return out

    def _aggregate_grades(obj) -> dict:
        out = {}
        for key, attr in (("mean", "mean_grade"), ("low", "low_grade")):
            try:
                out[key] = _grade_name(getattr(obj, attr, None))
            except Exception:  # noqa: BLE001
                out[key] = None
        return out

    try:
        scores = {**_signals(report), **_aggregates(report)}
        grades = {
            "ocr": _score_grade(scores["ocr"]),
            "layout": _score_grade(scores["layout"]),
            "parse": _score_grade(scores["parse"]),
            "table": _score_grade(scores["table"]),
            **_aggregate_grades(report),
        }
        pages = {}
        for page_no, page in (getattr(report, "pages", None) or {}).items():
            try:
                key = int(page_no)
            except (TypeError, ValueError):
                continue
            pages[key] = {**_signals(page), **_aggregates(page),
                          **_aggregate_grades(page)}
    except Exception as exc:  # noqa: BLE001 — additive; never break extraction
        logger.debug(f"Docling confidence report unreadable — {exc}")
        return None

    return {"scores": scores, "grades": grades, "pages": pages}


def _read_floor_verdict(counts: dict) -> dict:
    """The RTE-4 empty-read floor's shares and verdict, from raw counts.

    Split out from :func:`_read_floor` so the per-document path and the DOCX
    path — which sums one set of counts per embedded image — reach the verdict
    through the same arithmetic instead of two copies of it.

    The floor asks one question: did this document read anything? It fires when

    * no page produced a single character, or
    * effectively every table region Docling detected is empty (the shape a
      failed OCR pass makes: regions found, nothing in them — the same defect
      the "Tables Found" check was taught to fail), or
    * half or more of the pages that could ONLY be read by OCR came back blank,
      AND those pages are at least half the document.

    Each is per-page evidence about what was READ. None of them consults a
    grade, which is the point: a document Docling grades GOOD and read nothing
    off is exactly the case the grades cannot see.

    The magnitude guard on the third clause is what keeps it a floor on the
    DOCUMENT rather than on a page. A digital report with one scanned signature
    page has exactly one image-only page and it reads blank — coverage 0.0 — and
    that says nothing whatever about whether the report was read. Requiring the
    OCR-only pages to be half the document before their blankness may speak for
    it is the difference between a signal and a nuisance.
    """
    pages_total = int(counts.get("pages_total") or 0)
    image_only = int(counts.get("image_only_pages") or 0)
    image_only_blank = int(counts.get("image_only_pages_without_text") or 0)
    pages_without_text = int(counts.get("pages_without_text") or 0)
    cells_total = int(counts.get("cells_total") or 0)
    cells_blank = int(counts.get("cells_blank") or 0)

    ocr_coverage = (round(1.0 - image_only_blank / image_only, 4)
                    if image_only else None)
    empty_region_share = (round(cells_blank / cells_total, 4)
                          if cells_total else None)

    flagged = bool(pages_total and (
        pages_without_text == pages_total
        or (empty_region_share is not None
            and empty_region_share >= EMPTY_REGION_FLOOR)
        or (ocr_coverage is not None and ocr_coverage <= OCR_COVERAGE_FLOOR
            and image_only * 2 >= pages_total)
    ))

    return {
        "pages_total": pages_total,
        "image_only_pages": image_only,
        "image_only_pages_without_text": image_only_blank,
        "pages_without_text": pages_without_text,
        "cells_total": cells_total,
        "cells_blank": cells_blank,
        "empty_region_share": empty_region_share,
        "ocr_coverage": ocr_coverage,
        "flagged": flagged,
    }


def _read_floor(page_texts: Optional[dict], table_boxes: Optional[list],
                confidence: Optional[dict]) -> dict:
    """Per-page read evidence for the empty-read floor (RTE-4).

    What counts as text on a page: the page's own text (``_page_texts``) plus
    every non-blank cell of every table region that sits on it — a page whose
    only content is a table has still been read.

    What counts as an "image-only" page: one where the PDF parser found no text
    layer, so everything on it had to be OCR'd. Docling records exactly that as
    a NaN ``parse_score`` (``page_preprocessing_model`` rates the parser's own
    cells and there were none), and for an image input every page qualifies.
    Without a confidence report there is no way to tell, and the OCR-coverage
    signal stays None rather than guessing — an unmeasured signal must not be
    able to flag a document.
    """
    pages_conf = ((confidence or {}).get("pages") or {})
    chars: dict = {}
    for page_no, text in (page_texts or {}).items():
        try:
            chars[int(page_no)] = len((text or "").strip())
        except (TypeError, ValueError):
            continue
    for page_no in pages_conf:
        chars.setdefault(int(page_no), 0)

    cells_total = 0
    cells_blank = 0
    for box in (table_boxes or []):
        if not box:
            continue
        page_no = box.get("page_no")
        try:
            page_no = int(page_no) if page_no is not None else None
        except (TypeError, ValueError):
            page_no = None
        for cell in (box.get("cells") or []):
            cells_total += 1
            text = str(cell.get("text") or "").strip()
            if not text:
                cells_blank += 1
            elif page_no is not None:
                chars[page_no] = chars.get(page_no, 0) + len(text)

    image_only = [p for p in chars if (pages_conf.get(p) or {}).get("parse") is None] \
        if pages_conf else []

    return _read_floor_verdict({
        "pages_total": len(chars),
        "image_only_pages": len(image_only),
        "image_only_pages_without_text": sum(1 for p in image_only if not chars.get(p)),
        "pages_without_text": sum(1 for p in chars if not chars.get(p)),
        "cells_total": cells_total,
        "cells_blank": cells_blank,
    })


def _merge_confidence(parts: List[Optional[dict]]) -> Optional[dict]:
    """One ``{scores, grades, pages}`` payload for a bundle of embedded images.

    A DOCX is not one page Docling read — it is N screenshots, each converted on
    its own, each with its own report. The bundle's SCORES are the mean of the
    per-image scores (what a calibration pass in P7 will want to look at) and
    its GRADES are the WORST of the per-image grades: a pack whose second
    statement OCR'd badly is not a good read of the pack, and a mean would hide
    exactly the image a reviewer needs to open. Because of that the two are
    reported on different rules, and ``grades["ocr"]`` is deliberately not
    ``raw_score_to_grade(scores["ocr"])``.

    Pages are renumbered 1..N in the order the images were processed, which is
    the order ``table_boxes[i]["image_index"]`` and the HITL source view use.
    """
    present = [p for p in parts if isinstance(p, dict)]
    if not present:
        return None

    def _mean(key: str) -> Optional[float]:
        vals = [v for p in present
                if (v := (p.get("scores") or {}).get(key)) is not None]
        return round(sum(vals) / len(vals), 6) if vals else None

    def _worst(key: str) -> Optional[str]:
        vals = [v for p in present
                if (v := (p.get("grades") or {}).get(key)) in GRADE_ORDER]
        return min(vals, key=lambda g: GRADE_ORDER[g]) if vals else None

    pages: dict = {}
    for part in present:
        for _, page in sorted((part.get("pages") or {}).items()):
            pages[len(pages) + 1] = page

    return {
        "scores": {k: _mean(k) for k in ("ocr", "layout", "parse", "table",
                                         "mean", "low")},
        "grades": {k: _worst(k) for k in ("ocr", "layout", "parse", "table",
                                          "mean", "low")},
        "pages": pages,
    }


def _merge_read_floor(parts: List[Optional[dict]]) -> dict:
    """The empty-read floor over a bundle of embedded images (RTE-4).

    Counts sum and the verdict is taken once over the total, so a DOCX of four
    screenshots of which three read nothing is judged as one document that read
    a quarter of itself — not as three flagged images and one clean one.
    """
    total = {
        "pages_total": 0, "image_only_pages": 0,
        "image_only_pages_without_text": 0, "pages_without_text": 0,
        "cells_total": 0, "cells_blank": 0,
    }
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key in total:
            total[key] += int(part.get(key) or 0)
    return _read_floor_verdict(total)


def _page_texts(doc) -> dict:
    """Per-page plain text of a DoclingDocument, keyed by 1-based page number.

    Stage 3b (VAL-7/E-2) reads the unit a statement is written in off the text
    around the table — the caption line above it — and that text is only a
    caption on the page it was written on. Every page Docling saw is a key,
    pages that yielded no text included as ``""``: an empty page is a fact about
    that page, not a reason to renumber the others.

    docling-core 2.61 also exposes ``DoclingDocument.export_to_markdown(page_no=n)``
    (verified against the installed 2.61.0 signature). It is not used here for
    two reasons: it re-walks the whole document once per page — O(pages x items)
    against the single pass below — and it renders each TABLE's own cells into
    the page string, so a table stating no unit would read its neighbour's
    header as its own "caption" and record source="caption" for something the
    caption never said. ``doc.texts`` keyed by ``prov[0].page_no`` is exactly
    the text AROUND the tables, which is what Stage 3b is asking for; a unit
    written INSIDE a table is already read by ``unit_detector`` off the frame.
    """
    out: dict = {}
    try:
        for page_no in (getattr(doc, "pages", None) or {}):
            try:
                out[int(page_no)] = ""
            except (TypeError, ValueError):
                continue

        parts: dict = {}
        for item in (getattr(doc, "texts", None) or []):
            prov = getattr(item, "prov", None)
            if not prov:
                continue
            page_no = getattr(prov[0], "page_no", None)
            content = getattr(item, "text", None)
            if page_no is None or not content:
                continue
            try:
                key = int(page_no)
            except (TypeError, ValueError):
                continue
            parts.setdefault(key, []).append(str(content))

        for page_no, lines in parts.items():
            out[page_no] = "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — additive; never break extraction
        logger.debug(f"Per-page text unavailable — {exc}")
        return {}
    return out


def _table_geometry(table, doc) -> Optional[dict]:
    """Per-cell bbox geometry for a Docling table → the HITL source-document
    overlay. Returns ``{page_no, page_w, page_h, cells:[{row,col,text,l,t,r,b}]}``
    or None when no geometry is available.

    Each cell carries its grid position (``start_row_offset_idx`` /
    ``start_col_offset_idx``) so the UI can align it to the exported DataFrame, its
    OCR ``text`` (a label-match tie-breaker), and a TOP-LEFT bbox. Page size lets
    the UI scale points→pixels — for an image input Docling's page size equals the
    pixel size, so the overlay is 1:1.
    """
    try:
        data = getattr(table, "data", None)
        cells = getattr(data, "table_cells", None) if data is not None else None
        if not cells:
            return None
        page_no = table.prov[0].page_no if getattr(table, "prov", None) else None
        page_w = page_h = None
        pages = getattr(doc, "pages", None) or {}
        pg = pages.get(page_no) if page_no in pages else next(iter(pages.values()), None)
        size = getattr(pg, "size", None) if pg is not None else None
        if size is not None:
            page_w, page_h = float(size.width), float(size.height)

        out = []
        for c in cells:
            bb = getattr(c, "bbox", None)
            if bb is None:
                continue
            l, t, r, b = float(bb.l), float(bb.t), float(bb.r), float(bb.b)
            # Normalise to TOP-LEFT (cell bboxes are already TOPLEFT, but the
            # table prov is BOTTOMLEFT — guard so either survives a y-flip).
            if str(getattr(bb, "coord_origin", "")).upper().endswith("BOTTOMLEFT") and page_h:
                t, b = page_h - t, page_h - b
            out.append({
                "row": int(getattr(c, "start_row_offset_idx", -1)),
                "col": int(getattr(c, "start_col_offset_idx", -1)),
                "text": (c.text or ""),
                "l": min(l, r), "t": min(t, b), "r": max(l, r), "b": max(t, b),
            })
        return {"page_no": page_no, "page_w": page_w, "page_h": page_h, "cells": out}
    except Exception as exc:  # geometry is best-effort — never break extraction
        logger.debug(f"Table geometry capture failed: {exc}")
        return None


def extract_document(path: Path, language: str = "fr") -> TableExtractionResult:
    """
    Convenience function to extract from any supported document type.
    
    Args:
        path: Path to PDF, DOCX, or image file
        language: OCR language code
        
    Returns:
        TableExtractionResult
    """
    extractor = DoclingExtractor(language=language)
    
    suffix = path.suffix.lower()
    
    if suffix == '.pdf':
        return extractor.extract_from_pdf(path)
    elif suffix == '.docx':
        return extractor.extract_from_docx(path)
    elif suffix in ['.jpg', '.jpeg', '.png', '.tiff', '.bmp']:
        image = Image.open(path)
        return extractor.extract_from_image(image)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")


if __name__ == "__main__":
    # Test extraction
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python docling_extractor.py <file_path>")
        sys.exit(1)
    
    path = Path(sys.argv[1])
    result = extract_document(path)
    
    print(f"\n{'='*60}")
    print(f"Extraction Result: {path.name}")
    print(f"{'='*60}")
    print(f"Tables found: {len(result.tables)}")
    print(f"Confidence: {result.confidence:.3f}")
    print(f"Text length: {len(result.text)} chars")
    
    for i, table in enumerate(result.tables):
        print(f"\nTable {i+1}:")
        print(table.head().to_string())
