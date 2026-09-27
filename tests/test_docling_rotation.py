"""OCR-1/2/3 + DOC-9 — `extraction.pdf_orientation.normalize_rotation`.

docling-parse 4.7.3 returns no OCR rectangles for a page carrying /Rotate 180 or
/Rotate 270, and none for any page whose /Rotate is INHERITED from the page
tree: a fully scanned document then extracts as a few dozen characters. The
helper hands Docling a de-rotated temp COPY instead — never touching the
original — and records what it changed.

Every fixture is built with PyMuPDF into `tmp_path`; no test reads the corpus.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _fitz_available() -> bool:
    try:
        import fitz  # noqa: F401
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _fitz_available(), reason="PyMuPDF (fitz) not installed")


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------
def _pages_node(doc, page) -> int:
    """xref of the /Pages node a page hangs from (where inherited keys live)."""
    kind, value = doc.xref_get_key(page.xref, "Parent")
    assert kind == "xref", f"unexpected /Parent: {kind} {value}"
    return int(value.split()[0])


def _build_pdf(
    path: Path,
    pages: list,
    *,
    inherited_rotate: int = None,
    inherited_cropbox: str = None,
) -> Path:
    """Write a PDF whose pages follow ``pages`` — a list of per-page dicts with
    optional ``rotate`` (page-level /Rotate), ``cropbox`` (own /CropBox array
    string) and ``width``/``height``.

    ``inherited_rotate`` / ``inherited_cropbox`` move that key OFF every page and
    onto the /Pages node, which is how a real-world inherited attribute looks.
    """
    import fitz

    doc = fitz.open()
    for spec in pages:
        page = doc.new_page(width=spec.get("width", 595), height=spec.get("height", 842))
        page.insert_text((72, 72), "Bilan")
        if spec.get("rotate"):
            page.set_rotation(spec["rotate"])
        if spec.get("cropbox"):
            doc.xref_set_key(page.xref, "CropBox", spec["cropbox"])
    doc.save(str(path))
    doc.close()

    if inherited_rotate is None and inherited_cropbox is None:
        return path

    doc = fitz.open(str(path))
    try:
        parent = _pages_node(doc, doc[0])
        for page in doc:
            if inherited_rotate is not None:
                doc.xref_set_key(page.xref, "Rotate", "null")
            if inherited_cropbox is not None:
                doc.xref_set_key(page.xref, "CropBox", "null")
        if inherited_rotate is not None:
            doc.xref_set_key(parent, "Rotate", str(inherited_rotate))
        if inherited_cropbox is not None:
            doc.xref_set_key(parent, "CropBox", inherited_cropbox)
        doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    finally:
        doc.close()
    return path


def _rotations(path: Path) -> list:
    import fitz

    doc = fitz.open(str(path))
    try:
        return [page.rotation for page in doc]
    finally:
        doc.close()


def _temp_pdfs() -> set:
    d = Path(tempfile.gettempdir()) / "finalze_derotate"
    return set(d.glob("*.pdf")) if d.exists() else set()


# ---------------------------------------------------------------------------
# The guard: what gets de-rotated
# ---------------------------------------------------------------------------
def test_digital_180_is_derotated(tmp_path):
    from extraction.pdf_orientation import normalize_rotation

    src = _build_pdf(tmp_path / "rot180.pdf", [{"rotate": 180}])
    out, record = normalize_rotation(src)
    try:
        assert out != src
        assert record["applied"] is True
        assert record["skipped_pages"] == []
        assert record["temp_path"] == str(out)
        assert record["pages"][1]["rotation"] == 180
        assert record["pages"][1]["inherited"] is False
        assert record["pages"][1]["promoted"] is False
        assert _rotations(out) == [0]
        assert _rotations(src) == [180], "the ORIGINAL must not be rewritten"
    finally:
        out.unlink(missing_ok=True)


def test_digital_270_is_derotated(tmp_path):
    from extraction.pdf_orientation import normalize_rotation

    src = _build_pdf(tmp_path / "rot270.pdf", [{"rotate": 270}])
    out, record = normalize_rotation(src)
    try:
        assert out != src
        assert record["applied"] is True
        assert record["pages"][1]["rotation"] == 270
        assert _rotations(out) == [0]
        assert _rotations(src) == [270]
    finally:
        out.unlink(missing_ok=True)


def test_mixed_rotation_only_touches_180_270(tmp_path):
    from extraction.pdf_orientation import normalize_rotation

    src = _build_pdf(
        tmp_path / "mixed.pdf",
        [{}, {"rotate": 90}, {"rotate": 180}, {"rotate": 270}],
    )
    out, record = normalize_rotation(src)
    try:
        assert sorted(record["pages"]) == [3, 4]
        # page 1 was already upright, page 2 keeps its page-level 90.
        assert _rotations(out) == [0, 90, 0, 0]
    finally:
        out.unlink(missing_ok=True)


def test_page_level_90_untouched(tmp_path):
    """docling-parse handles a page-level /Rotate 90 — nothing to do, no copy."""
    from extraction.pdf_orientation import normalize_rotation

    src = _build_pdf(tmp_path / "rot90.pdf", [{"rotate": 90}])
    before = _temp_pdfs()
    out, record = normalize_rotation(src)
    assert out == src
    assert record is None
    assert _temp_pdfs() == before, "no temp copy may be written when nothing changes"


def test_inherited_90_is_derotated(tmp_path):
    """OCR-3 — /Rotate on the /Pages node is mishandled at every value."""
    from extraction.pdf_orientation import normalize_rotation

    src = _build_pdf(tmp_path / "inh90.pdf", [{}], inherited_rotate=90)
    assert _rotations(src) == [90], "fixture must present an inherited rotation"

    out, record = normalize_rotation(src)
    try:
        assert out != src
        assert record["pages"][1]["rotation"] == 90
        assert record["pages"][1]["inherited"] is True
        assert _rotations(out) == [0]
    finally:
        out.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# OCR-2: the CropBox
# ---------------------------------------------------------------------------
def test_cropbox_promoted_size_preserved(tmp_path):
    """remove_rotation() drops the CropBox; promoting it first keeps the frame."""
    import fitz

    from extraction.pdf_orientation import normalize_rotation

    src = _build_pdf(
        tmp_path / "cropbox.pdf",
        [{"rotate": 180, "width": 595, "height": 842, "cropbox": "[0 0 570 782]"}],
    )
    out, record = normalize_rotation(src)
    try:
        assert record["pages"][1]["promoted"] is True
        assert record["skipped_pages"] == []
        doc = fitz.open(str(out))
        try:
            rect = doc[0].rect
            assert round(rect.width) == 570
            assert round(rect.height) == 782
        finally:
            doc.close()
    finally:
        out.unlink(missing_ok=True)


def test_inherited_cropbox_page_skipped_and_recorded(tmp_path):
    """An inherited CropBox cannot be promoted page-by-page — skip and record."""
    from extraction.pdf_orientation import normalize_rotation

    src = _build_pdf(
        tmp_path / "inh_cropbox.pdf",
        [{"rotate": 180}],
        inherited_cropbox="[0 0 570 782]",
    )
    before = _temp_pdfs()
    out, record = normalize_rotation(src)

    assert out == src, "the page stays rotated rather than losing its frame"
    assert record["applied"] is False
    assert record["pages"] == {}
    assert record["skipped_pages"] == [1]
    assert record["temp_path"] is None
    assert _temp_pdfs() == before


# ---------------------------------------------------------------------------
# No-ops and failures
# ---------------------------------------------------------------------------
def test_no_rotation_returns_original_path_and_none(tmp_path):
    from extraction.pdf_orientation import normalize_rotation

    src = _build_pdf(tmp_path / "plain.pdf", [{}, {}])
    before = _temp_pdfs()
    out, record = normalize_rotation(src)
    assert out == src
    assert record is None
    assert _temp_pdfs() == before


def test_temp_removed_on_save_failure(tmp_path, monkeypatch):
    """OCR-3 — a failed save must not leave the temp copy behind."""
    import fitz

    from extraction.pdf_orientation import normalize_rotation

    src = _build_pdf(tmp_path / "boom.pdf", [{"rotate": 180}])

    def _boom(self, *args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(fitz.Document, "save", _boom)

    before = _temp_pdfs()
    out, record = normalize_rotation(src)
    assert out == src, "extraction falls back to the original"
    assert record is None
    assert _temp_pdfs() == before, "the temp copy must be removed"


# ---------------------------------------------------------------------------
# The seam itself: DoclingExtractor.extract_from_pdf owns the temp copy.
# ---------------------------------------------------------------------------
def test_extract_from_pdf_removes_temp_copy_when_docling_raises(tmp_path, monkeypatch):
    """OCR-1/3 + DOC-9 — Docling reads the COPY, and the `finally` removes it
    even on the path where the converter raises mid-document."""
    from extraction.docling_extractor import DoclingExtractor

    src = _build_pdf(tmp_path / "extract180.pdf", [{"rotate": 180}])
    seen = {}

    class _Converter:
        def convert(self, path):
            seen["path"] = Path(path)
            seen["existed"] = Path(path).exists()
            raise RuntimeError("docling exploded")

    extractor = DoclingExtractor()
    monkeypatch.setattr(extractor, "_get_converter", lambda *a, **k: _Converter())

    before = _temp_pdfs()
    with pytest.raises(RuntimeError):
        extractor.extract_from_pdf(src)

    assert seen["path"] != src, "Docling must read the de-rotated copy, never the original"
    assert seen["existed"] is True
    assert _temp_pdfs() == before, "the de-rotated copy must not survive the failure"
    assert _rotations(src) == [180], "the ORIGINAL must not be rewritten"
