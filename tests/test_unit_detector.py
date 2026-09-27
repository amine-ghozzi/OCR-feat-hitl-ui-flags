"""
WP-2.9 — unit / scale binding (VAL-7, decision E-2).

The contract under test is narrow and it is the whole point of the package:
the unit a document states is READ and RECORDED, disagreement inside one
document is FLAGGED, and no figure is ever rescaled. Every test here that
asserts a value asserts it is *unchanged*.
"""

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable  # noqa: E402
from accounting.ensemble import _CARRY_FORWARD_METADATA, EnsembleSource, ensemble_vote  # noqa: E402
from accounting.enrichment_pipeline import enrich_tables  # noqa: E402
from accounting.scale_resolver import scale_candidates  # noqa: E402
from accounting.unit_detector import detect_unit, document_unit, match_unit  # noqa: E402


# --- helpers ---------------------------------------------------------------
def _frame(header: str) -> pd.DataFrame:
    """A minimal two-column statement frame whose first column carries a caption
    or header line. Labels are generic — no document content."""
    return pd.DataFrame(
        {
            "Libelle": [header, "Immobilisations incorporelles", "Stocks"],
            "2024": ["", "1 000,000", "2 000,000"],
        }
    )


def _ncell(value) -> CanonicalCell:
    return CanonicalCell(raw_value=str(value), parsed_value=Decimal(str(value)),
                         confidence=1.0)


def _tbl(metadata: dict) -> CanonicalTable:
    ct = CanonicalTable(rows=[
        CanonicalRow(raw_text="Stocks", canonical_term="Stocks",
                     match_type="exact", match_confidence=1.0,
                     cells={"value_n": _ncell(1000)}),
    ])
    ct.metadata = dict(metadata)
    ct.recompute_aggregates()
    return ct


# --- 1. the unit stated in the table's own header --------------------------
def test_header_en_dt():
    """"(en DT)" in the table header binds DT, sourced to the header."""
    unit = detect_unit(_frame("Bilan (en DT)"))
    assert unit == {"value": "DT", "source": "header", "confidence": 0.9}

    # The column names are read too — Tier 1 keeps the real header there.
    df = pd.DataFrame({"Libelle (en KDT)": ["Stocks"], "2024": ["1 000"]})
    assert detect_unit(df)["value"] == "KDT"

    # Spelled out, accented, and with a non-breaking space — all one unit.
    assert match_unit("en milliers de dinars")[0] == "KDT"
    assert match_unit("Montants exprimés en dinars tunisiens")[0] == "DT"
    assert match_unit("un tableau sans unité") is None

    # The EARLIEST statement wins, not the first pattern in the list: a caption
    # announces the unit at the top, and prose further down must not redefine
    # the document's scale.
    assert match_unit("Bilan (en DT) ... note: capital de 5 millions de dinars")[0] == "DT"
    assert match_unit("Etat (en KDT) ... exprime en dinars plus bas")[0] == "KDT"
    # A tie at the same position goes to the longer, more specific spelling.
    assert match_unit("milliers de dinars")[0] == "KDT"


# --- 2. the unit stated in the page caption above the table ----------------
def test_caption_en_mdt():
    """A table that states nothing inherits the unit from its page caption.

    ``mDT`` is the ambiguous spelling. Known-Gaps D1 pins the reading: "(en
    mDT)" read as "(en DT)" is wrong "by a factor of 1000" — a factor of 1000 is
    *milliers*, so lower-case m is thousands here and only all-caps MDT is
    millions. Either way the confidence is reduced, so a reviewer sees it is a
    reading rather than a fact and an explicit spelling outranks it.
    """
    df = _frame("Actifs non courants")          # no unit in the table itself
    assert detect_unit(df) is None

    unit = detect_unit(df, caption_text="Etat de resultat en mDT")
    assert unit["value"] == "KDT"               # milliers — the D1 reading
    assert unit["source"] == "caption"
    assert unit["confidence"] < detect_unit(_frame("(en KDT)"))["confidence"]

    # All-caps is the millions spelling, and it is still a reading.
    assert match_unit("en MDT")[0] == "MDT"
    assert match_unit("en MDT")[1] < 1.0
    assert match_unit("en millions de dinars") == ("MDT", 1.0)

    # The table's own header always wins over the caption.
    assert detect_unit(_frame("(en DT)"), caption_text="en mDT") == {
        "value": "DT", "source": "header", "confidence": 0.9,
    }


# --- 3. document-level propagation -----------------------------------------
def test_document_default_propagates_to_unlabelled_table():
    """EF CLB's shape: the unit is stated on some pages and not on others.

    Three tables on pages 1, 3 and 4; pages 1 and 4 state the unit, page 3 does
    not. Page 3's table inherits the document default — explicitly labelled as
    inherited, so nothing downstream mistakes it for a statement the document
    actually made.
    """
    tables = [_frame("Bilan (en DT)"), _frame("Notes"), _frame("Etat (en DT)")]
    provenance = [{"page_no": 1}, {"page_no": 3}, {"page_no": 4}]
    page_texts = {1: "Bilan arrete au 31 decembre", 3: "Notes", 4: "Etat"}

    cts = enrich_tables(tables, tier=0, provenance=provenance, page_texts=page_texts)
    assert len(cts) == 3

    assert cts[0].metadata["unit"]["source"] == "header"
    assert cts[2].metadata["unit"]["source"] == "header"

    inherited = cts[1].metadata["unit"]
    assert inherited["value"] == "DT"
    assert inherited["source"] == "document_default"
    assert inherited["confidence"] < cts[0].metadata["unit"]["confidence"]

    # Agreement is not a conflict.
    assert not any("unit_conflict" in ct.metadata for ct in cts)
    assert document_unit(cts) == {"value": "DT", "source": "header", "conflict": False}

    # And a document that states nothing anywhere gets nothing invented for it.
    plain = enrich_tables([_frame("Notes")], tier=0)
    assert "unit" not in plain[0].metadata
    assert document_unit(plain) is None

    # Key 0 is document-level text, not a caption: a tier that cannot split its
    # text per page (both call sites today) still seeds the document default
    # from it — and that default IS the document's unit, so it is reported
    # rather than lost as "only inherited".
    doc_level = enrich_tables([_frame("Notes"), _frame("Autres")], tier=0,
                              page_texts={0: "Etats financiers (en KDT)"})
    assert [ct.metadata["unit"]["source"] for ct in doc_level] == \
        ["document_default", "document_default"]
    assert document_unit(doc_level) == {"value": "KDT", "source": "document_default",
                                        "conflict": False}


# --- 4. the whole point: a conflict is flagged, never reconciled ------------
def test_conflict_flagged_not_rescaled():
    """Two tables of one document disagree: flag it, change no figure.

    E-2 A. The pipeline has no authority to decide which of two contradictory
    unit statements the figures were written under — guessing wrong is a 1000x
    error, so it does not guess.
    """
    tables = [_frame("Bilan (en DT)"), _frame("Etat (en KDT)")]
    provenance = [{"page_no": 1}, {"page_no": 2}]

    cts = enrich_tables(tables, tier=0, provenance=provenance)

    assert all(ct.metadata.get("unit_conflict") is True for ct in cts)
    assert document_unit(cts)["conflict"] is True

    # NOTHING was rescaled. The same frames enriched without any unit knowledge
    # produce byte-identical parsed values.
    baseline = enrich_tables([_frame("Bilan"), _frame("Etat")], tier=0,
                             provenance=provenance)
    for ct_unit, ct_plain in zip(cts, baseline):
        for r_unit, r_plain in zip(ct_unit.rows, ct_plain.rows):
            for key, cell in r_unit.cells.items():
                assert cell.parsed_value == r_plain.cells[key].parsed_value

    # And the resolver's unit hint can only ORDER candidates — never add,
    # remove, or rescale one.
    raw = "12083312.401"
    plain = scale_candidates(raw)
    assert len(plain) == 2
    for hint in (None, "DT", "KDT", "MDT", "nonsense"):
        hinted = scale_candidates(raw, unit_hint=hint)
        assert sorted(hinted) == sorted(plain), hint
    assert abs(scale_candidates(raw, unit_hint="KDT")[0]) < abs(plain[0])
    assert scale_candidates(raw, unit_hint="DT") == plain
    # A cell with only one reading stays single-candidate under every hint.
    assert len(scale_candidates("17 210 298", unit_hint="MDT")) == 1


# --- 5. the unit must survive the Tier-3 vote ------------------------------
def test_unit_survives_ensemble_vote():
    """A table fact established before the vote is still true after it.

    The vote reconciles cell VALUES; it does not re-read headers. A key missing
    from ``_CARRY_FORWARD_METADATA`` is silently dropped — which is how
    ``source_table_index`` and ``gate`` are lost today.
    """
    assert "unit" in _CARRY_FORWARD_METADATA
    assert "unit_conflict" in _CARRY_FORWARD_METADATA

    unit = {"value": "KDT", "source": "header", "confidence": 0.9}
    merged = ensemble_vote([
        EnsembleSource(name="tier0", weight=1.0,
                       tables=[_tbl({"unit": dict(unit), "unit_conflict": True})]),
        EnsembleSource(name="tier1", weight=0.8,
                       tables=[_tbl({"unit": dict(unit), "unit_conflict": True})]),
    ])

    assert len(merged) == 1
    assert merged[0].metadata["reconciliation"] == "ensemble"
    assert merged[0].metadata["unit"] == unit
    assert merged[0].metadata["unit_conflict"] is True
    # …and the document-level read-back works off the voted tables too.
    assert document_unit(merged) == {"value": "KDT", "source": "header",
                                     "conflict": True}


# ===========================================================================
# WP-2.9b — the caption path made LIVE at both tiers, the dual-tier merge
# stopped from throwing the record away, and one document-level write.
# ===========================================================================


# --- 6. Tier 0: a caption is only a caption on its OWN page ----------------
def test_tier0_page_texts_reach_caption(tmp_path):
    """Two pages, two different unit statements, two correctly bound tables.

    Tier 0 joined every page into one ``full_text`` and could only offer it as
    document-level text, so the EARLIEST unit in the whole document decided the
    whole document: page 2's "(en mDT)" was unreachable and both tables
    inherited page 1's DT as a ``document_default``. That is the D1 gap — a
    statement written in thousands read as though it were in dinars — arriving
    through the plumbing rather than the detector.

    With the per-page map, each table reads the caption on the page it actually
    sits on, the two disagree, and the document is flagged. E-2: flagged, not
    reconciled, and no figure moves.
    """
    fitz = pytest.importorskip("fitz")
    from pipeline import FinAlzePipeline

    rows = [("Libelle", "2024"), ("Stocks", "1 200"),
            ("Clients", "3 400"), ("Total actif", "4 600")]

    def _page(doc, caption):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 70), caption, fontsize=11)
        top, left, row_h, col_w, n_cols = 100, 60, 24, 200, 2
        for r in range(len(rows) + 1):
            y = top + r * row_h
            page.draw_line(fitz.Point(left, y), fitz.Point(left + n_cols * col_w, y))
        for c in range(n_cols + 1):
            x = left + c * col_w
            page.draw_line(fitz.Point(x, top), fitz.Point(x, top + len(rows) * row_h))
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                page.insert_text((left + c * col_w + 6, top + r * row_h + 16),
                                 cell, fontsize=10)

    pdf = tmp_path / "two_units.pdf"
    doc = fitz.open()
    _page(doc, "Bilan arrete au 31 decembre 2024 (en DT)")
    _page(doc, "Etat de resultat 2024 (en mDT)")
    doc.save(str(pdf))
    doc.close()

    result = FinAlzePipeline()._tier0_native(pdf)
    cts = [ct for ct in (result.canonical_tables or []) if ct is not None]
    assert len(cts) == 2, "the fixture must yield one table per page"

    by_page = {ct.page_range[0]: ct for ct in cts}
    assert set(by_page) == {1, 2}

    # Each table took the unit off ITS OWN page's caption — not the document's.
    assert by_page[1].metadata["unit"]["value"] == "DT"
    assert by_page[1].metadata["unit"]["source"] == "caption"
    # Known-Gaps D1: lower-case mDT is *milliers*, and it is a READING, so it
    # arrives at reduced confidence rather than as a fact.
    assert by_page[2].metadata["unit"]["value"] == "KDT"
    assert by_page[2].metadata["unit"]["source"] == "caption"
    assert by_page[2].metadata["unit"]["confidence"] < by_page[1].metadata["unit"]["confidence"]

    # The document disagrees with itself and says so, at table and at document
    # level — E-2: a question for a human, never a licence to rescale.
    assert all(ct.metadata.get("unit_conflict") is True for ct in cts)
    assert document_unit(cts)["conflict"] is True


# --- 7. Tier 1: Docling's per-page text is exposed, not thrown away --------
def test_tier1_page_texts_exposed():
    """``_process_result`` keeps each page's text under its 1-based number.

    Docling exports ONE markdown string and the extractor kept only that, so
    Tier 1 could offer Stage 3b nothing but document-level text however good its
    per-item page provenance was. The fake below is a ConversionResult shaped
    exactly as ``_process_result`` reads it: two pages, one unit statement on
    each.
    """
    from extraction.docling_extractor import DoclingExtractor

    def _text(content, page_no):
        return SimpleNamespace(text=content, prov=[SimpleNamespace(page_no=page_no)])

    document = SimpleNamespace(
        tables=[],
        texts=[
            _text("Bilan arrete au 31 decembre 2024 (en DT)", 1),
            _text("Stocks 1 200", 1),
            _text("Etat de resultat 2024 (en milliers de dinars)", 2),
            # No provenance at all — it belongs to no page and must not be
            # invented onto one.
            SimpleNamespace(text="orphelin", prov=[]),
        ],
        body=None,
        pages={1: SimpleNamespace(), 2: SimpleNamespace(), 3: SimpleNamespace()},
        export_to_markdown=lambda: "Bilan (en DT)\n\nEtat (en milliers de dinars)",
    )

    result = DoclingExtractor()._process_result(SimpleNamespace(document=document))
    page_texts = result.metadata["page_texts"]

    # Every page Docling saw is a key — page 3 carried no text and is "" rather
    # than missing, so no later page is silently renumbered.
    assert set(page_texts) == {1, 2, 3}
    assert page_texts[3] == ""
    assert "orphelin" not in "".join(page_texts.values())

    # …and the two pages state different units, each readable on its own page.
    assert match_unit(page_texts[1])[0] == "DT"
    assert match_unit(page_texts[2])[0] == "KDT"
    assert match_unit(page_texts[3]) is None


# --- 8. the dual-tier merge must not throw the record away -----------------
def test_reconciliation_carries_unit_forward():
    """What Stage 3b established survives ``reconcile_pair``.

    The Tier-3 vote already carries this set forward; the dual-tier merge
    rebuilt its metadata from scratch, so a document reconciled rather than
    voted reached the reviewer with no unit at all. Both paths now read the same
    ``_CARRY_FORWARD_METADATA`` — imported, not copied, so they cannot drift.
    """
    from accounting.reconciliation import reconcile_pair
    import accounting.reconciliation as R

    assert R._CARRY_FORWARD_METADATA is _CARRY_FORWARD_METADATA

    unit = {"value": "KDT", "source": "caption", "confidence": 0.8}
    table_a = _tbl({"unit": dict(unit), "unit_conflict": True,
                    "x_offsets": [60.0, 260.0], "column_profile": "two_period",
                    "source_table_index": 0})
    table_b = _tbl({"unit": {"value": "DT", "source": "header", "confidence": 0.9}})

    merged = reconcile_pair(table_a, table_b)

    assert merged.metadata["reconciliation"] == "dual_tier"
    # Taken from the Tier-0 anchor, which this merge is documented to trust.
    assert merged.metadata["unit"] == unit
    assert merged.metadata["unit_conflict"] is True
    assert merged.metadata["x_offsets"] == [60.0, 260.0]
    assert merged.metadata["column_profile"] == "two_period"
    # …and only that set: a key outside it is still not carried.
    assert "source_table_index" not in merged.metadata
    # The merge's own bookkeeping is untouched by the carry-forward.
    assert merged.metadata["conflict_count"] == 0
    assert document_unit([merged])["value"] == "KDT"

    # Nothing was rescaled — the merged figures are the anchor's figures.
    assert [r.cells["value_n"].parsed_value for r in merged.rows] == \
           [r.cells["value_n"].parsed_value for r in table_a.rows]


# --- 9. one document-level write, on the path every tier takes -------------
def test_document_unit_in_result_metadata(monkeypatch, tmp_path):
    """``ExtractionResult.metadata["unit"]`` is written in exactly ONE place.

    The document unit lived only on the canonical tables, so every consumer that
    wanted it had to re-derive it — and only the golden recorder ever did. The
    write sits after the cascade and after ``_attach_statements``, so it sees
    the tables the pipeline actually emitted whichever of the nine
    ``ExtractionResult`` returns produced them.
    """
    from pipeline import ExtractionResult, FinAlzePipeline

    cts = [_tbl({"unit": {"value": "KDT", "source": "caption", "confidence": 0.8}}),
           _tbl({"unit": {"value": "KDT", "source": "document_default",
                          "confidence": 0.6}})]
    prepared = ExtractionResult(
        text="", tables=[{}], qcs_score=0.9, tier_used=0,
        confidence_details={}, metadata={"hitl_required": False},
        canonical_tables=cts,
    )

    pipe = FinAlzePipeline()
    monkeypatch.setattr(FinAlzePipeline, "_run_tier_cascade",
                        lambda self, *a, **k: prepared)
    # This unit test isolates post-cascade units; admission is tested separately.
    monkeypatch.setattr("document_intake.inspect_document",
                        lambda *a, **k: ({"accepted": True}, None))
    result = pipe.process_document(tmp_path / "does_not_need_to_exist.pdf")

    assert result.metadata["unit"] == {"value": "KDT", "source": "caption",
                                       "conflict": False}
    # The write is additive — it replaces nothing the cascade recorded.
    assert result.metadata["hitl_required"] is False

    # A document whose tables state nothing records `None`, not an invention.
    prepared.canonical_tables = [_tbl({})]
    prepared.metadata = {}
    assert pipe.process_document(tmp_path / "x.pdf").metadata["unit"] is None
