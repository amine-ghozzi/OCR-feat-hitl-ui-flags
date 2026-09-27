"""WP-2.8 — Docling confidence RECORDED for Tier-1 (RTE-3) and the empty-read
floor that flags and never ends (RTE-4).

The register rows this file pins:

* **RTE-3** — every Tier-1 document reported ``ocr/layout/parse/low = FAIR``,
  which capped the QCS's intrinsic layer at 0.55 for the whole tier. The cause
  was a probe aimed at the wrong object: the extractor asked
  ``ConversionResult.document`` for ``quality`` / ``confidence_report`` /
  ``quality_report`` and then for per-signal ``*_grade`` attributes. The report
  lives on the ``ConversionResult`` itself, and per-signal grades do not exist
  anywhere in docling — only scores do. So the probe returned None on every
  document ever processed and the "FAIR" defaults were never overwritten.

  WP-2.8 **records** what Docling says and computes a second, unused QCS from
  it. It does NOT route on it: the plan's ruling is "record now, adopt into
  routing in P7 (WP-7.2, after MEA-7 calibration)", and measuring the adoption
  showed it moves routing materially. ``test_routing_qcs_ignores_docling_grades_until_p7``
  is the guard that makes a later package flip that consciously.
* **RTE-4** — nothing in the pipeline could notice that a document Docling
  graded well had been read as blank. The floor reads per-page evidence, and it
  FLAGS: no reject, no END, no band.

Everything here runs against a ``SimpleNamespace`` stand-in for the
``ConversionResult`` — the shape docling 2.70 actually hands over, built by
hand so the assertions do not depend on a model, a PDF, or a 6-minute cold
start.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (str(ROOT), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from extraction.docling_extractor import (  # noqa: E402
    DoclingExtractor,
    _confidence_payload,
    _read_floor,
)
from pipeline import _apply_read_floor, _docling_grades_for_measurement  # noqa: E402

NAN = float("nan")
FOUR_GRADES = {"EXCELLENT", "GOOD", "FAIR", "POOR"}


# --- the ConversionResult stand-in ----------------------------------------
def _page(ocr=0.9, layout=0.9, parse=0.9, table=NAN, mean=None, low=None):
    """One ``PageConfidenceScores``.

    ``mean_grade``/``low_grade`` are computed properties on the real class; the
    stand-in takes them as plain attributes so a test can state the grade it
    means to exercise instead of reverse-engineering numpy's quantile.
    """
    scores = [s for s in (ocr, layout, parse, table) if s == s]  # drop NaN
    mean_score = sum(scores) / len(scores) if scores else NAN
    return SimpleNamespace(
        ocr_score=ocr, layout_score=layout, parse_score=parse, table_score=table,
        mean_score=mean_score, low_score=min(scores) if scores else NAN,
        mean_grade=mean if mean is not None else "good",
        low_grade=low if low is not None else "good",
    )


def _document(page_count=1, texts=("Bilan arrete au 31 decembre 2024",)):
    """A ``DoclingDocument`` stand-in; ``texts`` are laid out one per page."""
    pages = {n: SimpleNamespace(size=SimpleNamespace(width=595.0, height=842.0))
             for n in range(1, page_count + 1)}
    items = [SimpleNamespace(text=t, prov=[SimpleNamespace(page_no=i % page_count + 1)])
             for i, t in enumerate(texts)]
    return SimpleNamespace(
        tables=[], texts=items, body=None, pages=pages,
        export_to_markdown=lambda: "\n".join(texts),
    )


def _result(confidence, document=None, page_count=1, texts=("Bilan 2024",)):
    doc = document if document is not None else _document(page_count, texts)
    ns = SimpleNamespace(document=doc)
    if confidence is not None:
        ns.confidence = confidence
    return ns


def _confidence(pages, **doc_level):
    """A ``ConfidenceReport``: document-level signals plus the per-page dict."""
    defaults = dict(ocr_score=NAN, layout_score=NAN, parse_score=NAN,
                    table_score=NAN, mean_grade="good", low_grade="good",
                    mean_score=NAN, low_score=NAN)
    defaults.update(doc_level)
    return SimpleNamespace(pages=pages, **defaults)


def _process(conv_result):
    return DoclingExtractor()._process_result(conv_result)


def _routing_grades(res):
    """The four grades the routing QCS is computed from."""
    return (res.ocr_grade, res.layout_grade, res.parse_grade, res.low_grade)


# --- RTE-3 -----------------------------------------------------------------
def test_grades_read_from_conversion_result():
    """The grades are read off ``ConversionResult.confidence`` — not ``.document``.

    The stand-in's DOCUMENT carries the old probe's three attribute names, each
    holding a report that would grade everything POOR. If the extractor were
    still looking there the recorded grades below would read POOR; they read the
    conversion result's scores instead, which is the whole of RTE-3.
    """
    trap = SimpleNamespace(ocr_grade="POOR", layout_grade="POOR",
                           parse_grade="POOR", low_grade="POOR")
    doc = _document()
    doc.quality = doc.confidence_report = doc.quality_report = trap

    conv = _result(
        _confidence({1: _page(ocr=0.93, layout=0.72, parse=0.55)},
                    ocr_score=0.93, layout_score=0.72, parse_score=0.55,
                    low_grade="fair", mean_grade="good"),
        document=doc,
    )
    res = _process(conv)
    payload = res.metadata["docling_confidence"]

    # raw_score_to_grade: >= 0.85 EXCELLENT, >= 0.65 GOOD, >= 0.40 FAIR.
    assert payload["scores"]["ocr"] == pytest.approx(0.93)
    assert payload["grades"] == {
        "ocr": "EXCELLENT", "layout": "GOOD", "parse": "FAIR", "table": None,
        # `low`/`mean` are docling's own aggregates — `low` a quantile over
        # PAGES, so it sees a single bad page a worst-of-three cannot.
        "mean": "GOOD", "low": "FAIR",
    }
    assert set(payload["pages"]) == {1}
    # …and not one of them is the "FAIR" the whole tier used to report.
    assert {payload["grades"]["ocr"], payload["grades"]["layout"]} != {"FAIR"}


def test_lowercase_grades_normalised():
    """``QualityGrade`` is a lowercase str-Enum; the pipeline speaks uppercase.

    This is not cosmetic. ``calculate_qcs``'s pre-gate compares ``low_grade ==
    "POOR"`` and this module's own membership test is uppercase too, so a
    lowercase "poor" sails through both — a document docling graded unreadable
    would be measured as merely mediocre. Normalised at the boundary, once.
    """
    class Grade(str):
        """Stands in for ``QualityGrade``: a str subclass with ``.value``."""
        @property
        def value(self):
            return str(self)

    conv = _result(_confidence({1: _page(ocr=0.2, layout=0.2, parse=0.2)},
                               ocr_score=0.2, layout_score=0.2, parse_score=0.2,
                               low_grade=Grade("poor"), mean_grade=Grade("poor")))
    grades = _process(conv).metadata["docling_confidence"]["grades"]

    assert grades["low"] == "POOR" and grades["mean"] == "POOR"
    for key in ("ocr", "layout", "parse", "low", "mean"):
        assert grades[key] in FOUR_GRADES

    # The pre-gate these feed — once P7 adopts them — now matches.
    from qcs.calculator import calculate_qcs
    measured = _docling_grades_for_measurement({"grades": grades})
    report = calculate_qcs(ocr_grade=measured["ocr"], layout_grade=measured["layout"],
                           parse_grade=measured["parse"], low_grade=measured["low"],
                           text="x", tables=[])
    assert report.low_grade_gated and report.needs_hitl


def test_nan_table_score_ignored():
    """``table_score`` has no writer in docling 2.70 — NaN is "not measured".

    Mapping NaN through a score→grade table would invent a POOR for every
    document in the corpus and, via the pre-gate, condemn the lot on the
    strength of a field nothing ever wrote. NaN reads as None, and None yields
    no grade — while the signals that WERE measured are unaffected.
    """
    conv = _result(_confidence({1: _page(ocr=0.9, layout=0.9, parse=0.9, table=NAN)},
                               ocr_score=0.9, layout_score=0.9, parse_score=0.9,
                               table_score=NAN))
    payload = _process(conv).metadata["docling_confidence"]

    assert payload["scores"]["table"] is None
    assert payload["grades"]["table"] is None
    assert payload["pages"][1]["table"] is None
    assert payload["grades"]["ocr"] == "EXCELLENT"
    assert payload["grades"]["layout"] == "EXCELLENT"
    # And the unmeasured signal does not drag the measurement down either.
    assert _docling_grades_for_measurement(payload)["low"] != "POOR"


def test_missing_confidence_falls_back_to_fair():
    """No confidence report → the documented default, not an exception.

    An older docling, another backend or a hand-built result carries no
    ``confidence`` attribute at all. The extractor must keep extracting: the
    routing grades stay at the "FAIR" default they have always had,
    ``docling_confidence`` is null rather than invented, and the empty-read
    floor's OCR-coverage signal — which can only be measured from that report —
    stays None instead of guessing.
    """
    conv = _result(None)
    res = _process(conv)

    assert _routing_grades(res) == ("FAIR", "FAIR", "FAIR", "FAIR")
    assert res.metadata["docling_confidence"] is None
    assert res.metadata["quality_report_available"] is False
    assert res.metadata["read_floor"]["ocr_coverage"] is None
    assert res.metadata["read_floor"]["flagged"] is False
    assert _confidence_payload(conv) is None
    # With nothing recorded, the measurement falls back to the same four grades
    # the routing call uses — so the P7 delta on such a document is zero, which
    # is the truth about it.
    assert _docling_grades_for_measurement(None) == {
        "ocr": "FAIR", "layout": "FAIR", "parse": "FAIR", "low": "FAIR"}


def test_quality_report_available_true():
    """The flag that read False on every document ever processed.

    It was the visible symptom of RTE-3 — ``quality_report_available`` recorded
    "we asked Docling and it had nothing to say", when in fact nobody had asked.
    """
    conv = _result(_confidence({1: _page()}, ocr_score=0.9, layout_score=0.9,
                               parse_score=0.9))
    res = _process(conv)

    assert res.metadata["quality_report_available"] is True
    assert isinstance(res.metadata["docling_confidence"], dict)
    # The fallback, for contrast — same extractor, same document, no report.
    assert _process(_result(None)).metadata["quality_report_available"] is False


def test_routing_qcs_ignores_docling_grades_until_p7():
    """RECORD, DO NOT ROUTE. The guard on the P7/WP-7.2 boundary.

    Docling grades this document EXCELLENT on every signal it measures. The
    grades the QCS is computed from are still the four "FAIR" defaults, so the
    routing score is byte-for-byte what it was before WP-2.8, and the score
    Docling's grades would have produced is a different number recorded beside
    it.

    Adoption was measured on the corpus before this split was chosen, and it
    moves routing: two PDFs stop reaching the Tier-3 two-engine vote (one of
    them extracting measurably worse alone) and two DOCX cross the 0.90
    no-human bar and lose their review flag. The plan's ruling on RTE-3 is
    "record now, adopt into routing in P7 (WP-7.2, after MEA-7 calibration)" and
    Q5-A / G-4 forbid widening automation before it is measured — so this test
    exists to make the later package flip it on purpose rather than by accident.
    """
    from qcs.calculator import calculate_qcs

    conv = _result(_confidence({1: _page(ocr=0.97, layout=0.95, parse=0.93)},
                               ocr_score=0.97, layout_score=0.95, parse_score=0.93,
                               low_grade="excellent", mean_grade="excellent"))
    res = _process(conv)

    # 1. Docling's answer is recorded in full, and it is emphatically not FAIR.
    recorded = res.metadata["docling_confidence"]["grades"]
    assert (recorded["ocr"], recorded["layout"], recorded["parse"], recorded["low"]) \
        == ("EXCELLENT", "EXCELLENT", "EXCELLENT", "EXCELLENT")

    # 2. The grades the routing QCS reads are untouched by it.
    assert _routing_grades(res) == ("FAIR", "FAIR", "FAIR", "FAIR")

    # 3. The two scores differ — so the split is doing something, and a package
    #    that wires the recorded grades into the routing call will fail here.
    kwargs = dict(text=res.text, tables=res.tables, validation_report=None,
                  corrections_made=res.corrections_made)
    routing = calculate_qcs(ocr_grade=res.ocr_grade, layout_grade=res.layout_grade,
                            parse_grade=res.parse_grade, low_grade=res.low_grade,
                            **kwargs)
    measured = _docling_grades_for_measurement(res.metadata["docling_confidence"])
    docling = calculate_qcs(ocr_grade=measured["ocr"], layout_grade=measured["layout"],
                            parse_grade=measured["parse"], low_grade=measured["low"],
                            **kwargs)
    assert docling.qcs_score > routing.qcs_score
    # The intrinsic layer is 40 % of the composite and moves 0.55 -> 1.00 here.
    assert docling.qcs_score - routing.qcs_score == pytest.approx(0.18, abs=1e-6)


# --- RTE-4 -----------------------------------------------------------------
def test_empty_read_floor_flags_not_ends():
    """A document Docling grades GOOD and read nothing off is FLAGGED, not ended.

    Three pages that only OCR could read, no text on any of them, and a
    confidence report that calls the result "good" throughout — the exact shape
    the grades cannot see. The floor is built from the per-page evidence, so it
    fires; and every consequence of firing is additive.
    """
    conv = _result(
        _confidence({n: _page(ocr=0.9, layout=0.9, parse=NAN) for n in (1, 2, 3)},
                    ocr_score=0.9, layout_score=0.9, parse_score=NAN,
                    low_grade="good", mean_grade="good"),
        document=_document(page_count=3, texts=()),
    )
    res = _process(conv)
    floor = res.metadata["read_floor"]

    assert floor["pages_total"] == 3
    assert floor["image_only_pages"] == 3      # parse_score NaN → no text layer
    assert floor["pages_without_text"] == 3
    assert floor["ocr_coverage"] == 0.0
    assert floor["flagged"] is True
    # …while Docling itself is perfectly happy, which is the point of the floor.
    assert res.metadata["docling_confidence"]["grades"]["low"] == "GOOD"

    # FLAGS, NEVER ENDS — the extraction is returned, not refused.
    assert isinstance(res.text, str)
    assert res.tables == []
    assert res.metadata["table_count"] == 0

    # The pipeline side: the flag may only ever RAISE the review decision, and
    # it adds its reason rather than replacing the one the QCS gave.
    assert _apply_read_floor(False, "", floor) == (True, "empty_read_floor")
    assert _apply_read_floor(True, "qcs 0.58 below 0.90", floor) == \
        (True, "qcs 0.58 below 0.90; empty_read_floor")
    # An unflagged floor never lowers a flag somebody else raised…
    assert _apply_read_floor(True, "critical check failed", {"flagged": False}) == \
        (True, "critical check failed")
    # …and a document with no floor at all is left exactly as it was.
    assert _apply_read_floor(False, "", None) == (False, "")

    # A document that DID read is not flagged, on the same three-page shape.
    ok = _result(
        _confidence({n: _page(ocr=0.9, layout=0.9, parse=NAN) for n in (1, 2, 3)},
                    ocr_score=0.9, layout_score=0.9, parse_score=NAN),
        document=_document(page_count=3, texts=("TOTAL ACTIFS", "TOTAL PASSIFS")),
    )
    ok_floor = _process(ok).metadata["read_floor"]
    assert ok_floor["ocr_coverage"] > 0.5 and ok_floor["flagged"] is False

    # And the magnitude guard: a digital report with ONE scanned signature page
    # that reads blank has ocr_coverage 0.0 and must NOT be flagged — one page
    # says nothing about whether the other twenty-three were read.
    signed = _read_floor(
        {n: ("Note %d aux etats financiers" % n if n < 24 else "") for n in range(1, 25)},
        [],
        {"pages": {n: {"parse": 0.8 if n < 24 else None} for n in range(1, 25)}},
    )
    assert signed["image_only_pages"] == 1 and signed["ocr_coverage"] == 0.0
    assert signed["flagged"] is False


def test_empty_region_share_flags_regions_detected_but_blank():
    """The other empty-read shape: table regions found, nothing inside them.

    A failed OCR pass on a scanned document yields table REGIONS with no content
    — the defect the "Tables Found" check was taught to fail. Here the page has
    text, so the OCR-coverage signal is satisfied and only the empty-region
    share can fire.
    """
    boxes = [{"page_no": 1, "cells": [{"text": ""} for _ in range(50)]}]
    floor = _read_floor({1: "Bilan 2024"}, boxes,
                        {"pages": {1: {"parse": None}}})

    assert floor["cells_total"] == 50 and floor["cells_blank"] == 50
    assert floor["empty_region_share"] == 1.0
    assert floor["flagged"] is True

    # A normal financial table is full of blank cells — section headers, an
    # unfilled comparative column — and must NOT trip the floor.
    normal = [{"page_no": 1, "cells": [{"text": "" if i % 2 else "1 234"}
                                       for i in range(50)]}]
    assert _read_floor({1: "Bilan 2024"}, normal,
                       {"pages": {1: {"parse": None}}})["flagged"] is False
