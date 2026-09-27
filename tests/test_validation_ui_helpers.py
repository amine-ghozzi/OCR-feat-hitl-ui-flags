"""Workstreams B/C/D pure-helper tests — D3 parsed frames, D4/D5 dual-pass
pairing, D6 parsed companion + D16 edited-cell styling, D12 coverage,
D13 corrected score/quality suggestion, D16 diff_tables.
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable  # noqa: E402
from accounting.ensemble import (  # noqa: E402
    canonical_tables_to_parsed_dataframes,
    canonical_tables_to_records,
)
from accounting.rule_engine import validate_with_rules  # noqa: E402
from accounting.validator import ValidationReport, ValidationResult  # noqa: E402
from hitl import review_view  # noqa: E402


def _ct_balance(asset_raw: str, asset_parsed, passif_raw: str, passif_parsed):
    """Tiny canonical Bilan with a Total actif and a Total CP+passif row."""
    def cell(raw, parsed):
        return CanonicalCell(raw_value=raw,
                             parsed_value=Decimal(str(parsed)) if parsed is not None else None)
    rows = [
        CanonicalRow(raw_text="Total actif", cells={"2023": cell(asset_raw, asset_parsed)}),
        CanonicalRow(raw_text="Total des capitaux propres et passifs",
                     cells={"2023": cell(passif_raw, passif_parsed)}),
    ]
    return CanonicalTable(statement_type="bilan", rows=rows)


# ---------------------------------------------------------------------------
# D3 — parsed frames mirror the records shape
# ---------------------------------------------------------------------------

def test_parsed_frames_shape_and_values():
    ct = _ct_balance("5.000", 5000, "5 000", 5000)
    recs = canonical_tables_to_records([ct])
    frames = canonical_tables_to_parsed_dataframes([ct])
    assert len(frames) == 1
    df = frames[0]
    # Same keys / row order as the records serialization.
    assert list(df.columns) == list(recs[0][0].keys())
    assert len(df) == len(recs[0])
    assert df["label"].tolist() == [r["label"] for r in recs[0]]
    # Numeric cells are float(parsed_value), at canonical scale.
    assert df["2023"].tolist() == [5000.0, 5000.0]
    assert isinstance(df["2023"].iloc[0], float)


def test_parsed_frames_keep_raw_when_unparsed():
    ct = CanonicalTable(rows=[CanonicalRow(
        raw_text="X", cells={"2023": CanonicalCell(raw_value="%5'9-", parsed_value=None)})])
    df = canonical_tables_to_parsed_dataframes([ct])[0]
    assert df["2023"].iloc[0] == "%5'9-"  # garbled stays visible to V1-006


# ---------------------------------------------------------------------------
# D4/D5 — the dual pass flips a false failure; pairing classifies it
# ---------------------------------------------------------------------------

def test_dual_pass_balance_flips_fail_to_pass():
    # Raw text "5.000" parses naively as 5.0 → balance FAILS on raw;
    # the canonical parse (5000) satisfies it → PASSES on parsed values.
    ct = _ct_balance("5.000", 5000, "5 000", 5000)
    raw_df = pd.DataFrame(canonical_tables_to_records([ct])[0])
    # Force the naive read on the raw side: replace the canonical-scale text by
    # a plain decimal the naive parser keeps tiny.
    raw_df.loc[0, "2023"] = "5.0"
    before = validate_with_rules([raw_df], standard="NCT")
    after = validate_with_rules(canonical_tables_to_parsed_dataframes([ct]), standard="NCT")
    trans = review_view.pair_reports(before, after)
    bal = [n for n in trans if "Balance" in n]
    assert bal and trans[bal[0]] == "fixed", trans


def test_pair_reports_transitions():
    def rep(*pairs):
        r = ValidationReport()
        for name, passed, *rest in pairs:
            r.add_check(ValidationResult(name, passed=passed, message="",
                                         skipped=bool(rest and rest[0])))
        return r
    before = rep(("A", False), ("B", False), ("C", True), ("D", True), ("S", True, True))
    after = rep(("A", True), ("B", False), ("C", False))
    trans = review_view.pair_reports(before, after)
    assert trans == {"A": "fixed", "B": "still_fail", "C": "regressed", "D": "not_rerun"}
    # the skipped check "S" is not evidence either way
    assert "S" not in trans


# ---------------------------------------------------------------------------
# D12 — coverage: every rule shows an honest status
# ---------------------------------------------------------------------------

def test_coverage_tiered_statuses():
    r = ValidationReport()
    r.add_check(ValidationResult("[bilan] Var%: Amortissements", passed=False, message="m"))
    r.add_check(ValidationResult("[bilan] VNC Consistency", passed=True, message="ok"))
    r.add_check(ValidationResult("V4: Fonds de Roulement (FR)", passed=True, message="Skipped — x",
                                 skipped=True, severity="INFO"))
    cov = review_view.coverage(r, {"bilan"}, "NCT")
    by_id = {e["rule_id"]: e for tier in cov.values() for e in tier["rules"]}
    assert by_id["V1-005"]["status"] == "fail"
    assert by_id["V1-002"]["status"] == "pass"
    assert by_id["V4-001"]["status"] == "skipped"
    # V3-004 is now active (cross-statement): on a bilan-only report with no
    # produced check it reports as not_run with an honest reason, not inactive.
    assert by_id["V3-004"]["status"] == "not_run"
    assert "Compte de Résultat" in by_id["V3-004"]["detail"]
    assert by_id["V1-003"]["status"] == "n/a"        # needs compte_resultat
    assert by_id["V2-003"]["status"] == "not_run"    # implemented; no input here
    assert by_id["V2-003"]["detail"]                 # honest reason given
    # 15 rules, all present
    assert len(by_id) == 15
    assert cov["V1"]["counts"]["total"] == 6


def test_coverage_detail_reports_aggregate_count_not_object_count():
    """A family aggregate that verified N rows must surface its own count/message
    ('All N … verified'), not the misleading raw object count ('1 check(s)')."""
    r = ValidationReport()
    # one roll-up object that summarises 6 verified VNC identities
    r.add_check(ValidationResult(
        "[bilan] VNC Row-Layout", passed=True, severity="INFO",
        message="All 6 verifiable row-layout VNC identities verified ✓",
        stats={"candidates": 6, "verified": 6, "passed": 6, "failed": 0},
    ))
    cov = review_view.coverage(r, {"bilan"}, "NCT")
    v1_002 = next(e for tier in cov.values() for e in tier["rules"]
                  if e["rule_id"] == "V1-002")
    assert v1_002["status"] == "pass"
    assert "6" in v1_002["detail"] and "check(s)" not in v1_002["detail"]


def test_confirmed_cell_clears_risk_flag():
    """Confirming a flagged cell as-is clears its yellow → CONFIRMED colour."""
    import pandas as pd
    flat = pd.DataFrame({"": ["Stocks"], "2023": ["1 200"]})
    ct = {"rows": [{"raw_text": "Stocks", "is_header": False, "match_type": "exact",
                    "cells": {"2023": {"flag": "yellow", "basis": None}}}]}

    _, _, s0 = review_view.decorate_for_grid(flat, ct)
    assert s0["yellow"] == 1 and s0["confirmed"] == 0

    g, _, s1 = review_view.decorate_for_grid(flat, ct, confirmed_cells={(0, "2023")})
    assert s1["yellow"] == 0 and s1["confirmed"] == 1
    assert g["2023" + review_view.STYLE_SUFFIX][0] == review_view.CONFIRMED


def test_resolve_source_boxes_label_and_positional():
    """Focus targets → source rectangles via captured Docling geometry: by-label,
    positional (disambiguates repeated labels), and page filtering."""
    import pandas as pd
    df = pd.DataFrame({"": ["ACTIFS NON COURANTS", "Stocks", "Creances"],
                       "Notes": ["", "A4", "A5"], "2023": ["", "1 200", "900"]})
    geom = {"page_no": 1, "page_w": 598.0, "page_h": 636.0, "cells": [
        {"row": 0, "col": 2, "text": "2023", "l": 314, "t": 15, "r": 344, "b": 29},
        {"row": 1, "col": 0, "text": "ACTIFSNONCOURANTS", "l": 30, "t": 34, "r": 160, "b": 45},
        {"row": 2, "col": 0, "text": "Stocks", "l": 14, "t": 49, "r": 120, "b": 61},
        {"row": 2, "col": 2, "text": "1200", "l": 300, "t": 49, "r": 345, "b": 61},
        {"row": 3, "col": 0, "text": "Creances", "l": 14, "t": 64, "r": 160, "b": 77},
        {"row": 3, "col": 2, "text": "900", "l": 300, "t": 64, "r": 345, "b": 77},
    ]}
    # by-label → Stocks 2023 value cell (Docling row 2)
    b = review_view.resolve_source_boxes([{"label": "Stocks", "columns": ["2023"]}], [df], [geom])
    assert len(b) == 1
    assert {k: b[0][k] for k in ("l", "t", "r", "b", "page_w", "page_h")} == {
        "l": 300, "t": 49, "r": 345, "b": 61, "page_w": 598.0, "page_h": 636.0}
    # positional (df row 2 = Creances) → Docling row 3 via +1 header offset
    b2 = review_view.resolve_source_boxes(
        [{"row": 2, "label": "Creances", "columns": ["2023"]}], [df], [geom])
    assert b2 and b2[0]["t"] == 64
    # wrong page → nothing
    assert review_view.resolve_source_boxes(
        [{"label": "Stocks", "columns": ["2023"]}], [df], [geom], page_no=2) == []
    # missing geometry → nothing (graceful)
    assert review_view.resolve_source_boxes([{"label": "Stocks"}], [df], [None]) == []


# ---------------------------------------------------------------------------
# OCR-6 — invert_orientation: map de-rotated extraction geometry back into the
# STORED page frame. WP-2.1 extracts a de-rotated COPY, so `table_boxes` are not
# in the frame of the file on disk; this is the inverse of that record.
# ---------------------------------------------------------------------------

def _xf(page_no=1, **rec):
    """A WP-2.1 orientation record shaped exactly as `normalize_rotation` emits."""
    base = {"rotation": 0, "inherited": False, "mediabox": [0, 0, 600, 800],
            "cropbox": [0, 0, 600, 800], "promoted": False}
    base.update(rec)
    return {"applied": True, "pages": {page_no: base},
            "skipped_pages": [], "temp_path": None}


def test_invert_180_point_reflects():
    """A 180° de-rotation is undone by reflecting through the page centre."""
    boxes = [{"l": 100, "t": 50, "r": 200, "b": 90,
              "page_w": 600, "page_h": 800, "page_no": 1}]
    out = review_view.invert_orientation(boxes, _xf(rotation=180), 1, None, None)
    assert len(out) == 1
    assert {k: out[0][k] for k in ("l", "t", "r", "b")} == {
        "l": 400, "t": 710, "r": 500, "b": 750}
    # Still the same page frame, and every non-geometry key survives.
    assert (out[0]["page_w"], out[0]["page_h"]) == (600, 800)
    assert out[0]["page_no"] == 1
    # The inverse of a point reflection is itself.
    back = review_view.invert_orientation(out, _xf(rotation=180), 1, None, None)
    assert {k: back[0][k] for k in ("l", "t", "r", "b")} == {
        "l": 100, "t": 50, "r": 200, "b": 90}


def test_invert_270_swaps_axes():
    """90/270 swap the axes: a wide-short cell becomes tall-narrow, and the
    page frame goes back to the stored landscape MediaBox."""
    land = {"mediabox": [0, 0, 842, 595], "cropbox": [0, 0, 842, 595]}
    # Boxes live in the DE-ROTATED page (595 x 842 — the dimensions swapped).
    boxes = [{"l": 100, "t": 50, "r": 200, "b": 90, "page_w": 595, "page_h": 842}]

    out = review_view.invert_orientation(boxes, _xf(rotation=270, **land), 1, None, None)
    assert {k: out[0][k] for k in ("l", "t", "r", "b")} == {
        "l": 752, "t": 100, "r": 792, "b": 200}
    assert (out[0]["page_w"], out[0]["page_h"]) == (842, 595)
    assert out[0]["r"] - out[0]["l"] == 40 and out[0]["b"] - out[0]["t"] == 100, \
        "100x40 in the de-rotated frame must come back 40x100"

    # 270 turns the page anticlockwise, 90 clockwise — opposite page edges.
    out90 = review_view.invert_orientation(boxes, _xf(rotation=90, **land), 1, None, None)
    assert {k: out90[0][k] for k in ("l", "t", "r", "b")} == {
        "l": 50, "t": 395, "r": 90, "b": 495}


def test_invert_identity_without_transform():
    """Nothing was changed at extraction → nothing is undone here. This is the
    MEASURED case for every unrotated PDF and every page WP-2.1 skipped."""
    boxes = [{"l": 10, "t": 20, "r": 30, "b": 40, "page_w": 600, "page_h": 800}]
    assert review_view.invert_orientation(boxes, None, 1, None, None) == boxes
    assert review_view.invert_orientation(boxes, {}, 1, None, None) == boxes
    # a page the record does not mention (another page was de-rotated, not this one)
    assert review_view.invert_orientation(boxes, _xf(page_no=2, rotation=270), 1,
                                          None, None) == boxes
    # OCR-2: an inherited CropBox left the page rotated — `skipped_pages`, no entry
    skipped = {"applied": False, "pages": {}, "skipped_pages": [1], "temp_path": None}
    assert review_view.invert_orientation(boxes, skipped, 1, None, None) == boxes
    # a recorded page whose rotation is 0 changed nothing either
    assert review_view.invert_orientation(boxes, _xf(rotation=0), 1, None, None) == boxes
    assert review_view.invert_orientation([], _xf(rotation=270), 1, None, None) == []
    # API mode ships the metadata as JSON, so the page keys arrive as strings.
    as_json = {"applied": True, "skipped_pages": [], "temp_path": None,
               "pages": {"1": {"rotation": 180, "mediabox": [0, 0, 600, 800],
                               "cropbox": [0, 0, 600, 800], "promoted": False}}}
    assert review_view.invert_orientation(boxes, as_json, 1, None, None) != boxes, \
        "a string-keyed record must still be found — API mode is the whole point"


def test_cropbox_offset_applied():
    """A promoted CropBox means the de-rotated page covered the CROP area only;
    a MediaBox-framed render shows more page, so the boxes shift by its corner."""
    media, crop = [0, 0, 842, 595], [20, 15, 800, 560]
    boxes = [{"l": 0, "t": 0, "r": 10, "b": 10, "page_w": 545, "page_h": 780}]

    promoted = review_view.invert_orientation(
        boxes, _xf(rotation=270, mediabox=media, cropbox=crop, promoted=True),
        1, None, None)
    # Same geometry with no promotion (the page IS its CropBox) — no offset.
    plain = review_view.invert_orientation(
        boxes, _xf(rotation=270, mediabox=crop, cropbox=crop, promoted=False),
        1, None, None)

    assert {k: plain[0][k] for k in ("l", "t", "r", "b")} == {
        "l": 770, "t": 0, "r": 780, "b": 10}
    assert (plain[0]["page_w"], plain[0]["page_h"]) == (780, 545)
    # +x from the CropBox left edge, +y from the MediaBox TOP (PDF space is
    # bottom-up, the overlay frame is top-left): 20 across, 595-560 = 35 down.
    assert {k: promoted[0][k] for k in ("l", "t", "r", "b")} == {
        "l": 790, "t": 35, "r": 800, "b": 45}
    # …and the frame the boxes are now in is the MediaBox, not the CropBox.
    assert (promoted[0]["page_w"], promoted[0]["page_h"]) == (842, 595)


def test_confirmed_mapping_clears_map_flag():
    """Confirming a weak → Map mapping clears its ⚠ low-confidence flag."""
    import pandas as pd
    flat = pd.DataFrame({"": ["Stocks"], "2023": ["1 200"]})
    ct = {"rows": [{"raw_text": "Stocks", "is_header": False, "match_type": "grouped",
                    "match_confidence": 0.5, "cells": {"2023": {"flag": None}}}]}

    _, _, s0 = review_view.decorate_for_grid(flat, ct)
    assert s0["low_conf"] == 1 and s0["confirmed"] == 0

    g, _, s1 = review_view.decorate_for_grid(flat, ct, confirmed_maps={0})
    assert s1["low_conf"] == 0 and s1["confirmed"] == 1
    assert g[review_view.MAP_STYLE_COL][0] == review_view.CONFIRMED
    # badge flips ⚠ → ✓ so the change is visible, not just a colour swap
    assert "✓" in g[review_view.MAP_COL][0] and "⚠" not in g[review_view.MAP_COL][0]


def test_edit_overrides_confirm_on_same_cell():
    """An actual correction (TEAL) is stronger than a confirm and wins."""
    import pandas as pd
    flat = pd.DataFrame({"": ["Stocks"], "2023": ["1 200"]})
    ct = {"rows": [{"raw_text": "Stocks", "is_header": False, "match_type": "exact",
                    "cells": {"2023": {"flag": "yellow", "basis": None}}}]}
    g, _, s = review_view.decorate_for_grid(
        flat, ct, edited_cells={(0, "2023")}, confirmed_cells={(0, "2023")})
    assert g["2023" + review_view.STYLE_SUFFIX][0] == review_view.TEAL
    assert s["edited"] == 1 and s["confirmed"] == 0


def test_coverage_detail_falls_back_to_object_count_without_stats():
    """No aggregate stats → keep the plain object count (back-compat)."""
    r = ValidationReport()
    r.add_check(ValidationResult("[bilan] VNC Consistency", passed=True, message="ok"))
    cov = review_view.coverage(r, {"bilan"}, "NCT")
    v1_002 = next(e for tier in cov.values() for e in tier["rules"]
                  if e["rule_id"] == "V1-002")
    assert v1_002["detail"] == "1 check(s) verified"


# ---------------------------------------------------------------------------
# D13 — override-adjusted score + honest quality suggestion
# ---------------------------------------------------------------------------

def _chk(name, sev, passed=False, skipped=False):
    return ValidationResult(name, passed=passed, message="", severity=sev, skipped=skipped)


def test_corrected_score_applies_overrides_and_skips():
    checks = [
        _chk("A", "CRITICAL", passed=False),   # overridden to pass
        _chk("B", "ERROR", passed=True),
        _chk("C", "WARNING", passed=True, skipped=True),  # excluded
        _chk("D", "INFO", passed=False),       # weight 0
    ]
    eff = [True, True, True, False]
    assert review_view.corrected_score(checks, eff) == 1.0
    eff2 = [False, True, True, False]
    assert review_view.corrected_score(checks, eff2) == 2 / 5  # 2 of (3+2)


def test_suggest_quality_levels():
    crit = [_chk("A", "CRITICAL")]
    assert review_view.suggest_quality(crit, [False], 0.95) == 3   # Needs Correction
    warn = [_chk("A", "WARNING")]
    assert review_view.suggest_quality(warn, [False], 0.95) == 2   # Acceptable
    clean = [_chk("A", "CRITICAL", passed=True)]
    assert review_view.suggest_quality(clean, [True], 0.9) == 0    # Excellent
    assert review_view.suggest_quality(clean, [True], 0.8) == 1    # Good
    # An overridden-to-pass failure stops dragging the suggestion down.
    assert review_view.suggest_quality(crit, [True], 0.9) == 0


# ---------------------------------------------------------------------------
# D16 — diff_tables + edited-cell styling; D6 — parsed companion
# ---------------------------------------------------------------------------

def test_diff_tables_cell_events():
    a = pd.DataFrame({"label": ["X", "Y"], "2023": ["1", "2"]})
    b = pd.DataFrame({"label": ["X", "Y"], "2023": ["1", "99"]})
    events = review_view.diff_tables([a], [b])
    assert events == [{"table": 0, "row": 1, "col": "2023",
                       "label": "Y", "old": "2", "new": "99"}]
    # shape mismatch → no unsafe attribution
    assert review_view.diff_tables([a], [b.iloc[:1]]) == []


def test_decorate_edited_cell_wins_and_counts():
    ct = {"rows": [{"raw_text": "Amortissements", "match_type": "exact",
                    "match_confidence": 1.0, "is_header": False,
                    "cells": {"2023": {"flag": "green", "basis": "single_sep_thousands",
                                       "parsed_value": "-13473644"}}}]}
    flat = pd.DataFrame([{"label": "Amortissements", "2023": "-13 473.644"}])
    # Without edits: indigo (ambiguous)
    grid, _, summary = review_view.decorate_for_grid(flat, ct)
    assert grid["2023__style"][0] == review_view.INDIGO
    assert summary["assumption"] == 1
    # With the cell edited: teal wins, assumption count drops
    grid2, _, s2 = review_view.decorate_for_grid(flat, ct, edited_cells={(0, "2023")})
    assert grid2["2023__style"][0] == review_view.TEAL
    assert s2["assumption"] == 0 and s2["edited"] == 1


def test_decorate_resolved_basis_distinct_style():
    ct = {"rows": [{"raw_text": "Amortissements", "match_type": "exact",
                    "match_confidence": 1.0, "is_header": False,
                    "cells": {"2023": {"flag": "green", "basis": "resolved_by_vnc",
                                       "parsed_value": "-13473644"}}}]}
    flat = pd.DataFrame([{"label": "Amortissements", "2023": "-13 473.644"}])
    grid, _, summary = review_view.decorate_for_grid(flat, ct)
    assert grid["2023__style"][0] == review_view.RESOLVED  # not default green
    assert summary["resolved"] == 1


# ---------------------------------------------------------------------------
# In-place corrections (③ Final) + pre-resolution revert (② Before validation)
# ---------------------------------------------------------------------------

def _ct_with_resolution():
    return {
        "rows": [{"raw_text": "Amortissements", "is_header": False,
                  "match_type": "exact", "match_confidence": 1.0,
                  "cells": {"2023": {"raw_value": "-13 473.644",
                                     "parsed_value": "-13473644",
                                     "flag": "green",
                                     "basis": "resolved_by_vnc_row"}}}],
        "metadata": {"scale_resolutions": [{
            "table": 0, "row": 0, "col": "2023", "label": "Amortissements",
            "rule": "vnc_row", "kind": "resolved", "raw": "-13 473.644",
            "old_parsed": "-13473644", "new_parsed": "-13473644",
            "old_basis": "single_sep_thousands", "confidence": 0.9,
            "iteration": 1, "note": "VNC identity"}]},
    }


def test_apply_resolutions_inplace():
    flat = pd.DataFrame([{"label": "Amortissements", "2023": "-13 473.644"}])
    out, n = review_view.apply_resolutions_inplace(flat, _ct_with_resolution())
    assert n == 1
    assert out["2023"].iloc[0] == "-13 473 644"   # corrected IN the cell
    assert flat["2023"].iloc[0] == "-13 473.644"  # original untouched


def test_revert_resolutions_for_before_view():
    rev = review_view.revert_resolutions_ct(_ct_with_resolution())
    cell = rev["rows"][0]["cells"]["2023"]
    assert cell["basis"] == "single_sep_thousands"   # ambiguous again (indigo)
    assert cell["parsed_value"] == "-13473644"
    assert not (rev.get("metadata") or {}).get("scale_resolutions")
    # the ② view frame shows the pre-loop parsed value, formatted
    flat = pd.DataFrame([{"label": "Amortissements", "2023": "-13 473.644"}])
    pv = review_view.parsed_view_frame(flat, rev)
    assert pv["2023"].iloc[0] == "-13 473 644"
