"""OCR-7 — Tier-0 sub-3-row frames are kept as TAGGED fragments, and a page that
kept no table at all retries with ``strategy="text"``.

Before this package the per-page loop of ``_tier0_native`` dropped every frame
with fewer than 3 data rows and only ever retried a page that had *kept*
something. Two consequences, both measured on the corpus: a statement split by a
page break or an interior rule lost its pieces outright, and a page whose
statement is laid out by text alignment (no ruled cells, so the "lines" strategy
finds nothing) was unrecoverable.

The fix keeps the ``< 3`` rule's PURPOSE — find_tables() noise must not reach the
ensemble vote or the statement builder — by tagging instead of dropping: a kept
fragment carries ``fragment: True`` in ``table_provenance`` and is held out of
both consumers until CLS-1/CLS-4 reassemble it.

Fixtures are synthetic PDFs built with PyMuPDF, so nothing here reads the corpus.
"""

from __future__ import annotations

import sys
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


pytestmark = pytest.mark.skipif(
    not _fitz_available(), reason="PyMuPDF (fitz) not installed"
)

# Grid geometry — one label column + two value columns, 20pt row pitch.
_X = (50.0, 260.0, 360.0, 460.0)
_Y0 = 100.0
_ROW_H = 20.0

_HEADER = ("Rubrique comptable", "2024", "2023")
_BODY = [
    ("Immobilisations incorporelles", "210 580", "198 340"),
    ("Immobilisations corporelles", "1 120 450", "1 098 220"),
    ("Immobilisations financieres", "305 900", "287 610"),
    ("Total des actifs non courants", "1 636 930", "1 584 170"),
]


def _rows(n_data_rows: int) -> list[tuple]:
    return [_HEADER] + _BODY[:n_data_rows]


def _ruled_table_pdf(path: Path, n_data_rows: int) -> Path:
    """A single page holding ONE ruled table with ``n_data_rows`` data rows.

    The grid is drawn as real vector lines, which is what ``find_tables()``'s
    default "lines" strategy needs; ``to_pandas()`` promotes the first row to the
    header, so the frame it returns has exactly ``n_data_rows`` rows.
    """
    import fitz

    rows = _rows(n_data_rows)
    doc = fitz.open()
    page = doc.new_page()
    y1 = _Y0 + _ROW_H * len(rows)
    for i in range(len(rows) + 1):          # horizontal rules
        y = _Y0 + _ROW_H * i
        page.draw_line(fitz.Point(_X[0], y), fitz.Point(_X[-1], y), width=0.7)
    for x in _X:                             # vertical rules
        page.draw_line(fitz.Point(x, _Y0), fitz.Point(x, y1), width=0.7)
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text(
                fitz.Point(_X[c] + 3, _Y0 + _ROW_H * r + 14), cell, fontsize=8
            )
    doc.save(str(path))
    doc.close()
    return path


_PARA = (
    "Les notes annexes font partie integrante des etats financiers ci joints. "
    "Le present document presente la situation a la date de cloture de l exercice."
)


def _empty_region_pdf(path: Path) -> Path:
    """A page whose ruled region is DETECTED but yields no data row.

    One ruled band holding only its header: ``find_tables()`` reports the region,
    ``to_pandas()`` promotes that single row to the column index and the frame
    comes back with zero rows. The page therefore keeps nothing from a region it
    did detect — the one case the widened retry gate is for.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    y1 = _Y0 + _ROW_H
    for y in (_Y0, y1):
        page.draw_line(fitz.Point(_X[0], y), fitz.Point(_X[-1], y), width=0.7)
    for x in _X:
        page.draw_line(fitz.Point(x, _Y0), fitz.Point(x, y1), width=0.7)
    for c, cell in enumerate(_HEADER):
        page.insert_text(fitz.Point(_X[c] + 3, _Y0 + 14), cell, fontsize=8)
    page.insert_text(fitz.Point(50, 300), _PARA[:90], fontsize=9)
    page.insert_text(fitz.Point(50, 314), _PARA[90:], fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def _unruled_table_pdf(path: Path) -> Path:
    """A single page whose table is laid out by TEXT ALIGNMENT only.

    Not one vector line is drawn, so ``find_tables()`` detects NO region at all.
    On such a page ``strategy="text"`` does not recover a missed table — it
    manufactures one page-sized pseudo-table out of the running text — so the
    retry must NOT fire here.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    for r, row in enumerate(_rows(len(_BODY))):
        for c, cell in enumerate(row):
            page.insert_text(
                fitz.Point(_X[c] + 3, _Y0 + _ROW_H * r + 14), cell, fontsize=8
            )
    doc.save(str(path))
    doc.close()
    return path


def _tier0(path: Path):
    from pipeline import FinAlzePipeline, PipelineConfig

    return FinAlzePipeline(PipelineConfig())._tier0_native(path)


def _spy_find_tables(monkeypatch) -> list[tuple[int, str]]:
    """Record every ``find_tables`` call as ``(page_number, strategy)``."""
    import fitz

    calls: list[tuple[int, str]] = []
    original = fitz.Page.find_tables

    def _recording(self, *args, **kwargs):
        calls.append((self.number, kwargs.get("strategy", "lines")))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "find_tables", _recording)
    return calls


# ---------------------------------------------------------------------------
# 1. A sub-3-row frame is KEPT and TAGGED
# ---------------------------------------------------------------------------
def test_two_row_table_kept_as_fragment(tmp_path):
    """A 2-row ruled table survives, tagged ``fragment: True`` with its page."""
    result = _tier0(_ruled_table_pdf(tmp_path / "two_row.pdf", 2))

    provenance = (result.metadata or {}).get("table_provenance") or []
    assert result.tables, "the 2-row frame was dropped instead of kept"
    assert len(provenance) == len(result.tables)

    fragments = [p for p in provenance if p.get("fragment")]
    assert fragments, f"no frame tagged as a fragment: {provenance}"
    assert fragments[0]["page_no"] == 1
    # Reassembly needs the column geometry, which is why it is captured at all.
    assert fragments[0]["x_offsets"]


# ---------------------------------------------------------------------------
# 2. A page that kept NOTHING retries with strategy="text" — exactly once
# ---------------------------------------------------------------------------
def test_lost_region_page_does_not_retry_yet(tmp_path, monkeypatch):
    """The retry gate is unchanged — widening it is DEFERRED to WP-3.0.

    Pins the deferral so the leg is not re-widened by accident. Both shapes of
    "the page kept nothing" must leave the gate closed:

      (a) a region WAS detected and every frame came back with no data row —
          sound in principle, but measured to move four documents with frames
          that are not statement tables, and it is not the OCR-7 payoff (LVB
          p6-p9 is a Tier-3 pairing loss, CLS-3 / WP-3.1);
      (b) no region detected at all — there ``strategy="text"`` manufactures one
          page-sized pseudo-table out of the running text, which is not a
          fragment and reaches the vote and the statement inventory.
    """
    import fitz

    # (a) region detected, zero data rows kept.
    pdf = _empty_region_pdf(tmp_path / "empty_region.pdf")
    doc = fitz.open(str(pdf))
    tabs = doc[0].find_tables()
    assert len(tabs.tables) == 1, "fixture no longer has a detected region"
    assert [len(t.to_pandas()) for t in tabs.tables] == [0], "fixture kept a data row"
    doc.close()

    calls = _spy_find_tables(monkeypatch)
    _tier0(pdf)
    page0 = [strategy for page_no, strategy in calls if page_no == 0]
    assert page0 == ["lines"], (
        f"the lost-region retry is deferred to WP-3.0 but fired: {page0}"
    )

    # (b) no region detected at all.
    unruled = _unruled_table_pdf(tmp_path / "unruled.pdf")
    doc = fitz.open(str(unruled))
    assert len(doc[0].find_tables().tables) == 0, "fixture now detects a region"
    doc.close()

    calls2 = _spy_find_tables(monkeypatch)
    _tier0(unruled)
    page0b = [strategy for page_no, strategy in calls2 if page_no == 0]
    assert page0b == ["lines"], f"retry fired on a page with no region: {page0b}"


# ---------------------------------------------------------------------------
# 3. A tagged fragment never reaches the ensemble vote
# ---------------------------------------------------------------------------
def test_fragment_not_in_ensemble_sources(monkeypatch):
    """``_tier3_ensemble`` builds its sources from the full tables only."""
    import accounting.ensemble as ensemble_mod
    from accounting.canonical_model import CanonicalTable
    from pipeline import ExtractionResult, FinAlzePipeline, PipelineConfig

    full_t0 = CanonicalTable(statement_type="bilan_actif")
    fragment = CanonicalTable(statement_type="unknown", metadata={"fragment": True})
    full_t1 = CanonicalTable(statement_type="bilan_actif")

    def _result(raw):
        return ExtractionResult(
            text="x", tables=[], qcs_score=0.9, tier_used=0,
            confidence_details={}, metadata={},
            canonical_tables=list(raw), raw_canonical_tables=list(raw),
        )

    captured: dict = {}

    def _fake_vote(sources):
        captured["sources"] = sources
        return [full_t0]

    monkeypatch.setattr(ensemble_mod, "ensemble_vote", _fake_vote)

    pipeline = FinAlzePipeline(PipelineConfig())
    pipeline._tier3_ensemble(
        tier0_result=_result([full_t0, fragment]),
        tier1_result=_result([full_t1]),
        tier2_result=None,
        tier_attempts=[],
    )

    by_name = {s.name: s.tables for s in captured["sources"]}
    assert "tier0" in by_name, "the Tier-0 source was dropped entirely"
    assert fragment not in by_name["tier0"], "a fragment reached the ensemble vote"
    assert by_name["tier0"] == [full_t0]


# ---------------------------------------------------------------------------
# 4. A tagged fragment never reaches the statement builder
# ---------------------------------------------------------------------------
def test_fragment_not_in_statement_builder(monkeypatch):
    """``_attach_statements`` blanks a fragment, keeping every index aligned."""
    import pandas as pd

    import accounting.statement_builder as sb
    from pipeline import ExtractionResult, FinAlzePipeline, PipelineConfig

    seen: dict = {}

    def _fake_build(tables, provenance=None, dictionary=None):
        seen["frames"] = tables
        seen["provenance"] = provenance
        return []

    monkeypatch.setattr(sb, "build_statements", _fake_build)

    full = [{"Rubrique": "Total actif", "2024": "1 000"}] * 3
    frag = [{"Rubrique": "Report", "2024": "500"}]
    result = ExtractionResult(
        text="x", tables=[full, frag, full], qcs_score=0.9, tier_used=0,
        confidence_details={},
        metadata={
            "table_provenance": [
                {"page_no": 1, "x_offsets": [0, 100]},
                {"page_no": 2, "x_offsets": [0, 100], "fragment": True},
                {"page_no": 3, "x_offsets": [0, 100]},
            ]
        },
    )

    FinAlzePipeline(PipelineConfig())._attach_statements(result)

    frames = seen["frames"]
    assert len(frames) == 3, "a fragment was removed instead of blanked"
    assert frames[1].empty, "the fragment frame was handed to the builder"
    assert not frames[0].empty and not frames[2].empty
    # The provenance still describes `tables` one-for-one, so the builder's
    # length guard keeps it and every page number survives.
    assert len(seen["provenance"]) == 3
    assert isinstance(frames[0], pd.DataFrame)


# ---------------------------------------------------------------------------
# 5. A tagged fragment never reaches dual-tier reconciliation (nor supplementation)
# ---------------------------------------------------------------------------
def test_fragment_not_in_dual_tier_reconciliation(tmp_path, monkeypatch):
    """Tier 1 reconciles against the FULL Tier-0 tables only.

    Also guards the second Tier-0 consumer inside ``_tier1_fast_ocr``: the
    supplementation leg, which appends Tier-0 frames straight into
    ``result.tables`` when Tier 0 found more tables than Docling. A fragment must
    neither be appended there nor inflate the count that decides the leg fires.
    """
    import pandas as pd

    import accounting.reconciliation as recon_mod
    import extraction.docling_extractor as dx
    from accounting.canonical_model import CanonicalTable
    from pipeline import ExtractionResult, FinAlzePipeline, PipelineConfig

    full_ct = CanonicalTable(
        statement_type="bilan_actif", metadata={"source_table_index": 0}
    )
    fragment_ct = CanonicalTable(
        statement_type="unknown", metadata={"source_table_index": 1, "fragment": True}
    )
    docling_frame = pd.DataFrame(
        {"Rubrique": ["Total des actifs"], "2024": ["1 000"], "2023": ["900"]}
    )

    class _StubExtractor:
        def __init__(self, **kwargs):
            pass

        def extract_from_pdf(self, path):
            return dx.TableExtractionResult(
                tables=[docling_frame.copy()],
                text=_PARA,
                metadata={"table_boxes": []},
            )

    monkeypatch.setattr(dx, "DoclingExtractor", _StubExtractor)

    seen: dict = {}

    def _fake_reconcile(tier0_tables, tier1_tables):
        seen["tier0"] = list(tier0_tables)
        return list(tier1_tables)

    monkeypatch.setattr(recon_mod, "reconcile_dual_tier", _fake_reconcile)

    tier0 = ExtractionResult(
        text=_PARA,
        tables=[
            [{"Rubrique": "Total des actifs", "2024": "1 000"}] * 3,   # full
            [{"Rubrique": "Report", "2024": "500"}],                   # fragment
        ],
        qcs_score=0.5, tier_used=0, confidence_details={},
        metadata={
            "table_provenance": [
                {"page_no": 1, "x_offsets": [0, 100]},
                {"page_no": 2, "x_offsets": [0, 100], "fragment": True},
            ]
        },
        canonical_tables=[full_ct, fragment_ct],
        raw_canonical_tables=[full_ct, fragment_ct],
    )

    pdf = tmp_path / "stub.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")   # never opened — the extractor is stubbed
    result = FinAlzePipeline(PipelineConfig())._tier1_fast_ocr(
        pdf, [], tier0_result=tier0
    )

    assert "tier0" in seen, "reconciliation never ran — the test proves nothing"
    assert fragment_ct not in seen["tier0"], "a fragment reached reconciliation"
    assert seen["tier0"] == [full_ct]
    # Supplementation: 2 Tier-0 frames vs 1 Docling frame would have fired the
    # leg; counting only the full frame (1 vs 1) it must not.
    assert len(result.tables) == 1, "a fragment was supplemented into Tier-1 tables"


# ---------------------------------------------------------------------------
# 6. A tagged fragment never scores in Tier 0's own green-flag gate
# ---------------------------------------------------------------------------
def test_green_flag_metrics_ignore_fragments(tmp_path, monkeypatch):
    """The green flag judges the STATEMENTS Tier 0 produced, not the pieces.

    This is the one place a retained fragment could still move the record: its
    rows are unmatched and its cells unparsed by construction, so scoring it
    drags ``match_rate`` down and ``parse_fail_rate`` up, which changes
    ``green_flag_failures`` and ``tier_attempts`` — both of which survive into the
    Tier-3 result through ``best_prior.metadata``.
    """
    from decimal import Decimal

    import accounting.enrichment_pipeline as enrich_mod
    import accounting.green_flag as gf_mod
    from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable

    def _table(*, matched: bool, parsed: bool, col_conf: float, footing: bool, meta=None):
        cell = CanonicalCell(
            raw_value="1 000",
            parsed_value=Decimal("1000") if parsed else None,
            flag="green" if parsed else "yellow",
        )
        row = CanonicalRow(
            raw_text="Total des actifs",
            match_type="exact" if matched else "custom",
            cells={"value_n": cell},
        )
        ct = CanonicalTable(
            rows=[row],
            metadata={"column_confidence": col_conf, "footing_results": [footing],
                      **(meta or {})},
        )
        ct.match_rate = 1.0 if matched else 0.0
        return ct

    full = _table(matched=True, parsed=True, col_conf=0.90, footing=True)
    fragment = _table(matched=False, parsed=False, col_conf=0.10, footing=False,
                      meta={"fragment": True})

    monkeypatch.setattr(
        enrich_mod, "enrich_tables", lambda *a, **kw: [full, fragment]
    )

    seen: dict = {}

    def _spy_check(**kwargs):
        seen.update(kwargs)
        return gf_mod.GreenFlagResult(passed=False, failure_reasons=["stub"])

    monkeypatch.setattr(gf_mod, "check_green_flag", _spy_check)

    _tier0(_ruled_table_pdf(tmp_path / "green_flag.pdf", 4))

    assert seen, "the green-flag gate never ran — the test proves nothing"
    # Scoring both tables would give match_rate 0.5 and parse_fail_rate 1.0.
    assert seen["match_rate"] == 1.0, "a fragment scored in match_rate"
    assert seen["parse_fail_rate"] == 0.0, "a fragment scored in parse_fail_rate"
    assert seen["column_confidence"] == 0.90
    assert seen["classification_confidence"] == 1.0
    footing = list(seen.get("footing_results") or [])
    assert len(footing) == 1 and footing[0].footing_valid is True, (
        "a fragment's subtotals reached G4"
    )


# ---------------------------------------------------------------------------
# 7. A full table's provenance shape is untouched
# ---------------------------------------------------------------------------
def test_provenance_shape_unchanged_for_full_tables(tmp_path):
    """A >= 3-row table records exactly ``{page_no, x_offsets}`` — no new key."""
    result = _tier0(_ruled_table_pdf(tmp_path / "four_row.pdf", 4))

    provenance = (result.metadata or {}).get("table_provenance") or []
    assert provenance, "the 4-row table was not extracted"
    full = [p for p in provenance if not p.get("fragment")]
    assert full, f"the 4-row table was mis-tagged as a fragment: {provenance}"
    for entry in full:
        assert set(entry) == {"page_no", "x_offsets"}
