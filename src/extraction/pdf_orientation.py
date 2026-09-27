"""PDF de-rotation for the OCR path (OCR-1 / OCR-2 / OCR-3).

docling-parse 4.7.3 hands the OCR stage no bitmap rectangles for a page whose
``/Rotate`` is 180 or 270 — ``get_ocr_rects()`` comes back empty, RapidOCR is
never called and a fully scanned document extracts as a handful of characters.
The same happens at *any* rotation value when ``/Rotate`` is INHERITED from the
page tree instead of being set on the page object, which is why the guard below
also covers inherited ``/Rotate 90``. A page-level ``/Rotate 90`` is handled
correctly today and is deliberately left alone.

The fix runs Docling against a de-rotated **temporary copy**; the original under
``Samples/`` (or any caller's upload) is never rewritten. Two facts make the
copy safe:

* ``Page.remove_rotation()`` drops the page's CropBox, so a page whose CropBox is
  smaller than its MediaBox would silently grow (570x782 -> 595x842 on the
  measured case) and every OCR box would land at the wrong offset and scale.
  A page that owns its CropBox therefore gets it promoted to MediaBox first;
  a page that only *inherits* a divergent CropBox cannot be fixed in isolation
  and is left rotated and recorded in ``skipped_pages``.
* Every de-rotated page is recorded, so the HITL source-document overlay can
  tell that the geometry it received is not in the original page frame. The
  record travels in ``ExtractionResult.metadata["orientation_transform"]``.

The helper is fail-open: anything unexpected (no PyMuPDF, an encrypted file, a
failed save) is logged and the ORIGINAL path is returned, which is exactly the
behaviour that existed before this module.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Temp copies live beside the other FinAlze temp trees (see hitl/app.py:317-322):
# `output/temp/` is git-tracked and the container user cannot write it.
TEMP_SUBDIR = "finalze_derotate"

# Rotations docling-parse mishandles whatever their provenance.
ALWAYS_DEROTATE = (180, 270)

# Rotations mishandled only when the /Rotate key is inherited from the page tree.
DEROTATE_WHEN_INHERITED = (90,)


def _has_own_key(doc, page, key: str) -> bool:
    """True when *page* carries ``key`` itself rather than inheriting it."""
    try:
        kind, _ = doc.xref_get_key(page.xref, key)
    except Exception:  # noqa: BLE001 — a malformed page must not break extraction
        return False
    return kind not in (None, "null")


def _box(rect) -> list:
    """A fitz.Rect as a plain, JSON-safe ``[x0, y0, x1, y1]``."""
    return [round(float(v), 3) for v in (rect.x0, rect.y0, rect.x1, rect.y1)]


def _needs_derotation(rotation: int, inherited: bool) -> bool:
    if rotation in ALWAYS_DEROTATE:
        return True
    return inherited and rotation in DEROTATE_WHEN_INHERITED


def _unlink(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover — a locked handle on Windows
        logger.warning("could not remove de-rotation temp file %s", path, exc_info=True)


def normalize_rotation(pdf_path: Path) -> Tuple[Path, Optional[dict]]:
    """Return ``(path_to_extract, transform_record)`` for *pdf_path*.

    When no page needs de-rotation the ORIGINAL path and ``None`` come back and
    nothing is written. Otherwise a de-rotated copy is written under
    ``<tempdir>/finalze_derotate/`` and the record describes what changed::

        {"applied": True,
         "pages": {1: {"rotation": 270, "inherited": False,
                       "mediabox": [...], "cropbox": [...], "promoted": False}},
         "skipped_pages": [],
         "temp_path": "…/finalze_derotate/derot_xxxx.pdf"}

    Page numbers are 1-based, matching the pipeline's table provenance.
    **The caller owns the temp file** and must unlink it (see
    ``DoclingExtractor.extract_from_pdf``).
    """
    pdf_path = Path(pdf_path)

    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF not installed — PDF orientation left as-is")
        return pdf_path, None

    doc = None
    temp_path: Optional[Path] = None
    try:
        doc = fitz.open(str(pdf_path))

        pages: dict = {}
        skipped: list = []
        targets: list = []
        for index in range(len(doc)):
            page = doc[index]
            # page.rotation is the EFFECTIVE rotation — MuPDF has already
            # resolved page-tree inheritance for us.
            rotation = int(page.rotation)
            inherited = not _has_own_key(doc, page, "Rotate")
            if not _needs_derotation(rotation, inherited):
                continue

            number = index + 1
            mediabox, cropbox = _box(page.mediabox), _box(page.cropbox)
            promoted = False
            if cropbox != mediabox:
                if not _has_own_key(doc, page, "CropBox"):
                    # OCR-2: an inherited CropBox cannot be promoted on this page
                    # alone without changing every page that shares the node.
                    skipped.append(number)
                    logger.info(
                        "Page %d: rotation %d left in place — inherited CropBox differs "
                        "from MediaBox", number, rotation,
                    )
                    continue
                promoted = True

            pages[number] = {
                "rotation": rotation,
                "inherited": inherited,
                "mediabox": mediabox,
                "cropbox": cropbox,
                "promoted": promoted,
            }
            targets.append((index, promoted))

        if not targets:
            if skipped:
                # Nothing was rewritten, but the skip is a fact the overlay and
                # the reviewer need.
                return pdf_path, {
                    "applied": False,
                    "pages": {},
                    "skipped_pages": skipped,
                    "temp_path": None,
                }
            return pdf_path, None

        for index, promoted in targets:
            page = doc[index]
            if promoted:
                # Promote BEFORE remove_rotation(), which drops the CropBox.
                _, cropbox_value = doc.xref_get_key(page.xref, "CropBox")
                doc.xref_set_key(page.xref, "MediaBox", cropbox_value)
                page = doc.reload_page(page)
            page.remove_rotation()

        temp_dir = Path(tempfile.gettempdir()) / TEMP_SUBDIR
        temp_dir.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile(delete=False) + reopen: the Windows-safe idiom used
        # by docling_extractor.extract_from_image.
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", prefix="derot_", dir=str(temp_dir), delete=False
        ) as handle:
            temp_path = Path(handle.name)

        try:
            doc.save(str(temp_path))
        except Exception:  # noqa: BLE001 — OCR-3: never leave the temp behind
            logger.warning(
                "de-rotated save failed for %s — extracting the original", pdf_path,
                exc_info=True,
            )
            _unlink(temp_path)
            return pdf_path, None

        logger.info(
            "De-rotated %d page(s) of %s -> %s (skipped %d)",
            len(pages), pdf_path.name, temp_path, len(skipped),
        )
        return temp_path, {
            "applied": True,
            "pages": pages,
            "skipped_pages": skipped,
            "temp_path": str(temp_path),
        }

    except Exception:  # noqa: BLE001 — extraction must survive a broken PDF
        logger.warning(
            "PDF orientation normalisation failed for %s — extracting the original",
            pdf_path, exc_info=True,
        )
        _unlink(temp_path)
        return pdf_path, None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass
