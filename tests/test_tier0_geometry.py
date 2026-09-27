"""
ISSUE-9 tests — native-PDF table row reconstruction by y-coordinate.

Replaces the index-zip explode misalignment in ``_tier0_native``:
PyMuPDF returns each table column as one ``\n``-separated mega-cell, and the
legacy ``_explode_merged_rows`` zips columns *by index*. When the columns have
unequal part counts (value-less section headers like "Actifs" produce a label
with no value), index-zip cross-assigns values to the wrong labels. The new
``_cluster_words_to_grid`` rebuilds rows by physical y-position so fragments
that share a row align.

Part (a): pure synthetic-word unit tests (no PyMuPDF required).
Part (b): integration against the real CLB 2024 PDF (skipped if unavailable).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pipeline import _cluster_words_to_grid  # noqa: E402


# ---------------------------------------------------------------------------
# Geometry fixture: 1 label column + 2 value columns.
#
# Layout (x-centres): label ~50, note ~150, value2024 ~250, value2023 ~350.
# We model 3 columns here (label, 2024, 2023) and place words by (x0,y0,x1,y1).
# Row pitch = 20pt; each word ~10pt tall, so y_tol=3 cleanly separates rows.
# ---------------------------------------------------------------------------

# Column x-ranges (cx0, cx1)
LABEL = (10, 110)
V2024 = (210, 290)
V2023 = (310, 390)
XRANGES = [LABEL, V2024, V2023]
NAMES = ["label", "2024", "2023"]


def _w(text, xc, yc, w=40, h=8):
    """Build a (x0,y0,x1,y1,text) word centred at (xc,yc)."""
    return (xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2, text)


def test_cluster_aligns_label_with_value():
    words = [
        _w("Capital", 50, 100), _w("Social", 75, 100),   # label row 1
        _w("34,340,000", 250, 100), _w("7,840,000", 350, 100),
        _w("Reserves", 50, 120),                          # label row 2
        _w("1,000,000", 250, 120), _w("900,000", 350, 120),
    ]
    grid = _cluster_words_to_grid(words, XRANGES, NAMES, y_tol=3.0)
    assert grid[0] == ["Capital Social", "34,340,000", "7,840,000"]
    assert grid[1] == ["Reserves", "1,000,000", "900,000"]


def test_value_less_section_header_has_empty_values():
    """A section header ("Actifs") has NO value fragment on its physical row.

    The new clustering must leave its value cells EMPTY rather than borrowing
    the next data row's values.
    """
    words = [
        _w("Actifs", 50, 100),                            # header: label only
        _w("Immobilisations", 50, 120), _w("Incorporelles", 95, 120),
        _w("210,580", 250, 120), _w("210,580", 350, 120),
    ]
    grid = _cluster_words_to_grid(words, XRANGES, NAMES, y_tol=3.0)
    # Header row: label present, both values empty.
    assert grid[0] == ["Actifs", "", ""]
    # Data row aligned correctly.
    assert grid[1] == ["Immobilisations Incorporelles", "210,580", "210,580"]


def test_clustering_beats_index_zip_when_counts_differ():
    """Reproduce the ISSUE-9 corruption and prove clustering fixes it.

    Per-column mega-cell part counts differ (3 labels, 2 values) because the
    first label row is a value-less section header. Index-zip pairs label[i]
    with value[i], so it would assign the FIRST value (10) to the SECTION
    HEADER and shift every subsequent value up by one row — the exact
    cross-assignment corruption. Clustering by y keeps them aligned.
    """
    # 3 label rows, but only 2 value rows (the header "Section" has no value).
    labels = ["Section", "Item A", "Item B"]
    values = ["10", "20"]  # belong to Item A and Item B

    # --- legacy index-zip (end-padded), the BUGGY behaviour ---
    padded_values = values + [""] * (len(labels) - len(values))
    index_zip = list(zip(labels, padded_values))
    # Bug: "Section" wrongly paired with "10"; "Item B" loses its value.
    assert index_zip == [("Section", "10"), ("Item A", "20"), ("Item B", "")]

    # --- new y-clustering: correct alignment ---
    words = [
        _w("Section", 50, 100),                       # y=100: header, no value
        _w("Item", 50, 120), _w("A", 80, 120), _w("10", 250, 120),
        _w("Item", 50, 140), _w("B", 80, 140), _w("20", 250, 140),
    ]
    grid = _cluster_words_to_grid(words, XRANGES, NAMES, y_tol=3.0)
    labels_out = [r[0] for r in grid]
    v2024_out = [r[1] for r in grid]
    assert labels_out == ["Section", "Item A", "Item B"]
    # Section header keeps an EMPTY value; A->10, B->20 (NOT shifted up).
    assert v2024_out == ["", "10", "20"]


def test_empty_words_returns_empty_grid():
    assert _cluster_words_to_grid([], XRANGES, NAMES) == []


def test_words_outside_columns_are_dropped():
    """A word whose x-centre falls in no column span produces an empty row."""
    words = [_w("orphan", 500, 100)]  # x-centre 500 outside all xranges
    grid = _cluster_words_to_grid(words, XRANGES, NAMES, y_tol=3.0)
    assert grid == [["", "", ""]]


# ---------------------------------------------------------------------------
# Part (b): integration against the real PDF (skipped if unavailable).
# ---------------------------------------------------------------------------

PDF_PATH = ROOT / "Samples" / "Sample - OF" / "EF CLB 2024 Provisoire (1).pdf"


def _fitz_available() -> bool:
    try:
        import fitz  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _fitz_available(), reason="PyMuPDF (fitz) not installed")
@pytest.mark.skipif(not PDF_PATH.exists(), reason="CLB 2024 sample PDF not available")
def test_reconstruct_real_pdf_page0_actif():
    """Reconstruct page 0 (Actif) and assert correct y-aligned rows.

    'Immobilisations Incorporelles' must carry value '210,580' in a value
    column, and the section header 'Actifs' must have empty value cells.
    """
    import fitz

    from pipeline import _reconstruct_rows_by_geometry

    doc = fitz.open(str(PDF_PATH))
    try:
        page = doc[0]
        tabs = page.find_tables()
        assert tabs.tables, "expected at least one detected table on page 0"

        rebuilt = None
        for tab in tabs.tables:
            r = _reconstruct_rows_by_geometry(page, tab)
            if r is not None and len(r) > 0:
                rebuilt = r
                break
        assert rebuilt is not None, "geometry reconstruction returned None for all tables"

        def _row_text(row) -> str:
            return " ".join(str(c) for c in row).lower()

        rows = [list(rebuilt.iloc[i]) for i in range(len(rebuilt))]

        # --- 'Immobilisations Incorporelles' -> 210,580 in some value cell ---
        imm = [r for r in rows if "immobilisations incorporelles" in _row_text(r)]
        assert imm, "row 'Immobilisations Incorporelles' not found"
        assert any("210,580" in str(c) for c in imm[0]), (
            f"expected 210,580 on the Immobilisations Incorporelles row, got {imm[0]}"
        )

        # --- value-less section header 'Actifs' has empty value cells ---
        # The first y-row is the COLUMN header ("Actifs | Notes | dates ..."),
        # which legitimately carries the date/note column titles. The value-less
        # BODY section header "Actifs" is a later row whose value cells must be
        # empty (proving values were NOT cross-assigned from the next data row).
        def _is_column_header(r) -> bool:
            txt = _row_text(r)
            return ("notes" in txt) or ("2024" in txt) or ("2023" in txt)

        header_rows = [
            r for r in rows
            if str(r[0]).strip().lower() in ("actifs", "actif")
            and not _is_column_header(r)
        ]
        assert header_rows, "value-less body section header row 'Actifs' not found"
        hr = header_rows[0]
        # All non-label cells must be empty / whitespace for the section header.
        assert all(not str(c).strip() for c in hr[1:]), (
            f"section header 'Actifs' should have empty value cells, got {hr}"
        )
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# OCR-14: the PDF handle is closed on every path, the exception one included.
# An open handle on Windows blocks deletion of the caller's temp copy.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _fitz_available(), reason="PyMuPDF (fitz) not installed")
def test_tier0_closes_doc_on_exception(monkeypatch, tmp_path):
    import fitz

    from pipeline import FinAlzePipeline

    class _Page:
        def get_text(self, *args, **kwargs):
            raise RuntimeError("unreadable page")

    class _Doc:
        def __init__(self):
            self.is_closed = False

        def __len__(self):
            return 1

        def __getitem__(self, index):
            return _Page()

        def close(self):
            self.is_closed = True

    stub = _Doc()
    monkeypatch.setattr(fitz, "open", lambda *args, **kwargs: stub)

    result = FinAlzePipeline()._tier0_native(tmp_path / "never-opened.pdf")

    assert result.metadata.get("failed") is True
    assert stub.is_closed is True, "Tier 0 leaked the PDF handle on the failure path"
