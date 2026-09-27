"""OCR-4 — EXIF orientation on the image ingress (`DoclingExtractor._preprocess_image`).

PIL hands back the stored pixel buffer and never consults the EXIF Orientation tag, so an
orientation-6 JPEG (the phone-photo case) used to reach OCR rotated 90 degrees. The
transpose belongs in `_preprocess_image`, the one choke point every image ingress goes
through — the single-image path, the DOCX embedded images and the pipeline's image branch.

Also covers the temp-file leak the same card fixes: `image.save` moved inside the `try`,
so a save that raises no longer strands the `NamedTemporaryFile(delete=False)` it wrote to.

No test reads the corpus: every fixture is built with PIL into memory or `tmp_path`.
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from extraction.docling_extractor import (  # noqa: E402
    EXIF_ORIENTATION_TAG,
    DoclingExtractor,
    TableExtractionResult,
)


class _StubConverter:
    """Stands in for the Docling converter — building the real one costs minutes."""

    def convert(self, path):
        return object()


def _extractor() -> DoclingExtractor:
    """An extractor whose only preprocessing step is the transpose under test."""
    return DoclingExtractor(preprocess_resize=False, preprocess_grayscale=False)


def _jpeg_with_orientation(size, orientation: int) -> Image.Image:
    """A JPEG carrying `orientation` in its EXIF block, reopened from bytes."""
    image = Image.new("RGB", size, "white")
    exif = image.getexif()
    exif[EXIF_ORIENTATION_TAG] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    buffer.seek(0)
    return Image.open(buffer)


# ---------------------------------------------------------------------------
# The transpose itself
# ---------------------------------------------------------------------------
def test_exif_6_is_transposed():
    """Orientation 6 is "rotate 90 CW to display": width and height swap."""
    source = _jpeg_with_orientation((40, 20), 6)
    assert source.size == (40, 20)  # stored buffer, before the transpose

    image, record = _extractor()._preprocess_image(source)

    assert image.size == (20, 40)
    assert record == {"exif_orientation": 6, "transposed": True}
    # exif_transpose consumes the tag — a second pass must not rotate again.
    again, again_record = _extractor()._preprocess_image(image)
    assert again.size == (20, 40)
    assert again_record["transposed"] is False


def test_no_exif_unchanged():
    """No EXIF block at all (a PNG, and every image in the golden corpus)."""
    source = Image.new("RGB", (30, 18), "white")

    image, record = _extractor()._preprocess_image(source)

    assert record == {"exif_orientation": None, "transposed": False}
    assert image.size == (30, 18)
    assert image is source          # not even a copy: the bytes OCR sees are untouched


def test_orientation_1_is_not_transposed():
    """Orientation 1 means "already upright" — the tag is read, nothing is applied."""
    source = _jpeg_with_orientation((40, 20), 1)

    image, record = _extractor()._preprocess_image(source)

    assert image.size == (40, 20)
    assert record == {"exif_orientation": 1, "transposed": False}


# ---------------------------------------------------------------------------
# The temp file
# ---------------------------------------------------------------------------
def test_image_save_failure_does_not_leak_temp(monkeypatch, tmp_path):
    """A failing `image.save` must not strand the temp PNG it was writing to."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(DoclingExtractor, "_get_converter",
                        lambda self, threshold: _StubConverter())

    image = Image.new("RGB", (10, 10), "white")

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    image.save = boom

    with pytest.raises(OSError):
        _extractor().extract_from_image(image)

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Where the record travels
# ---------------------------------------------------------------------------
def test_image_record_lands_in_orientation_transform(monkeypatch):
    """The per-image record travels as metadata["orientation_transform"]["images"]."""
    captured = {}

    def fake_process(self, docling_result, orientation_transform=None):
        captured["orientation_transform"] = orientation_transform
        return TableExtractionResult(metadata={"orientation_transform": orientation_transform})

    monkeypatch.setattr(DoclingExtractor, "_get_converter",
                        lambda self, threshold: _StubConverter())
    monkeypatch.setattr(DoclingExtractor, "_process_result", fake_process)

    result = _extractor().extract_from_image(_jpeg_with_orientation((40, 20), 6))

    assert captured["orientation_transform"] == {
        "images": [{"exif_orientation": 6, "transposed": True}]
    }
    assert result.metadata["orientation_transform"]["images"][0]["transposed"] is True


def test_docx_records_one_orientation_per_embedded_image(monkeypatch, tmp_path):
    """The DOCX path keeps `image_index` provenance, as it does for `table_boxes`."""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    # Two DIFFERENT images: python-docx stores one image part per distinct blob,
    # so two copies of the same file would come back as a single embedded image.
    for index, (size, colour) in enumerate((((24, 16), "white"), ((30, 20), "black"))):
        png = tmp_path / f"page{index}.png"
        Image.new("RGB", size, colour).save(png)
        document.add_picture(str(png))
    path = tmp_path / "two_images.docx"
    document.save(str(path))

    seen = []

    def fake_extract_from_image(self, image):
        seen.append(image.size)
        return TableExtractionResult(metadata={
            "orientation_transform": {"images": [{"exif_orientation": None,
                                                  "transposed": False}]},
            "table_boxes": [],
        })

    monkeypatch.setattr(DoclingExtractor, "extract_from_image", fake_extract_from_image)

    result = _extractor().extract_from_docx(path)

    assert len(seen) == 2
    assert result.metadata["orientation_transform"]["images"] == [
        {"exif_orientation": None, "transposed": False, "image_index": 0},
        {"exif_orientation": None, "transposed": False, "image_index": 1},
    ]
