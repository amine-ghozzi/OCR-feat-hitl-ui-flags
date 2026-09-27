"""Feature B — N-judge panel tests (variance reduction).

Covers `panel.aggregate_panel` (median scoring, defect majority vote, inter-judge
agreement) and `panel.load_case_verdicts` (panel-member discovery + single-verdict
backward compatibility).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.extraction_benchmark import panel  # noqa: E402


def _verdict(case, dims, defects=None, reference_suspect=False):
    return {
        "case_id": case,
        "dimensions": {
            d: {"prod_score": p, "ref_score": r} for d, (p, r) in dims.items()
        },
        "defects": defects or [],
        "reference_suspect": reference_suspect,
    }


# --- aggregate_panel: median scoring + defect majority vote --------------------
def test_aggregate_panel_median_and_majority():
    """3 synthetic judges: median scoring; a defect in 2/3 kept, a defect in 1/3 dropped."""
    v1 = _verdict("c", {"mapping_correctness": (4, 5), "table_structure": (3, 4)},
                  defects=[{"category": "mapping", "severity": "high", "detail": "wrong row"}])
    v2 = _verdict("c", {"mapping_correctness": (5, 5), "table_structure": (4, 4)},
                  defects=[{"category": "mapping", "severity": "high", "detail": "wrong row"}])
    v3 = _verdict("c", {"mapping_correctness": (2, 5), "table_structure": (4, 4)},
                  defects=[{"category": "sign", "severity": "low", "detail": "sign flip"}])

    out = panel.aggregate_panel([v1, v2, v3])

    # median of prod_scores [4,5,2] = 4; median of [3,4,4] = 4
    assert out["dimensions"]["mapping_correctness"]["prod_score"] == 4
    assert out["dimensions"]["mapping_correctness"]["ref_score"] == 5
    assert out["dimensions"]["table_structure"]["prod_score"] == 4

    # the "wrong row" mapping defect appears in 2/3 judges (ceil(3/2)=2) -> kept
    cats = {(d["category"], d["severity"]) for d in out["defects"]}
    assert ("mapping", "high") in cats
    # the "sign flip" defect appears in only 1/3 -> dropped
    assert ("sign", "low") not in cats

    assert out["agreement"]["n_judges"] == 3
    assert out["agreement"]["mean_stddev"] > 0  # judges disagreed on mapping prod_score


def test_aggregate_panel_defect_support_count():
    """A kept defect carries its panel_support count."""
    d = {"category": "mapping", "severity": "high", "detail": "wrong row"}
    out = panel.aggregate_panel([
        _verdict("c", {"x": (1, 1)}, defects=[d]),
        _verdict("c", {"x": (1, 1)}, defects=[d]),
        _verdict("c", {"x": (1, 1)}, defects=[]),
    ])
    assert len(out["defects"]) == 1
    assert out["defects"][0]["panel_support"] == 2


# --- agreement: unanimous vs high-variance ------------------------------------
def test_unanimous_panel_zero_stddev():
    out = panel.aggregate_panel([
        _verdict("c", {"mapping_correctness": (4, 5)}),
        _verdict("c", {"mapping_correctness": (4, 5)}),
        _verdict("c", {"mapping_correctness": (4, 5)}),
    ])
    assert out["agreement"]["mean_stddev"] == 0.0
    assert out["dimensions"]["mapping_correctness"]["prod_score"] == 4


def test_high_variance_panel_median_picks_majority():
    """One judge scores 5, two score 1 on a dim -> median = 1 (majority), stddev > 0."""
    out = panel.aggregate_panel([
        _verdict("c", {"mapping_correctness": (5, 5)}),
        _verdict("c", {"mapping_correctness": (1, 5)}),
        _verdict("c", {"mapping_correctness": (1, 5)}),
    ])
    assert out["dimensions"]["mapping_correctness"]["prod_score"] == 1  # median of [5,1,1]
    assert out["agreement"]["per_dimension_stddev"]["mapping_correctness"] > 0
    assert out["agreement"]["mean_stddev"] > 0


def test_aggregate_panel_tolerates_missing_dimension():
    """A judge missing a dimension simply doesn't contribute to it."""
    out = panel.aggregate_panel([
        _verdict("c", {"mapping_correctness": (4, 5), "table_structure": (3, 5)}),
        _verdict("c", {"mapping_correctness": (4, 5)}),  # no table_structure
    ])
    # table_structure scored by only one judge -> median of [3] = 3, stddev 0
    assert out["dimensions"]["table_structure"]["prod_score"] == 3
    assert out["agreement"]["per_dimension_stddev"]["table_structure"] == 0.0


def test_reference_suspect_is_union():
    out = panel.aggregate_panel([
        _verdict("c", {"x": (1, 1)}, reference_suspect=False),
        _verdict("c", {"x": (1, 1)}, reference_suspect=True),
    ])
    assert out["reference_suspect"] is True


def test_defect_majority_by_normalized_detail_when_categories_vary():
    """When judges disagree on category, group by a normalized first-8-words detail key."""
    out = panel.aggregate_panel([
        _verdict("c", {"x": (1, 1)},
                 defects=[{"category": "mapping", "severity": "high",
                           "detail": "Account 401 mapped to the wrong balance sheet row"}]),
        _verdict("c", {"x": (1, 1)},
                 defects=[{"category": "classification", "severity": "high",
                           "detail": "Account 401 mapped to the wrong balance sheet line"}]),
    ])
    # same finding, different category + last word -> first-8-words key matches -> kept (2/2)
    assert len(out["defects"]) == 1


# --- load_case_verdicts: panel members vs single ------------------------------
def test_load_case_verdicts_single_backward_compat(tmp_path):
    """Only a single <case>.verdict.json -> 1-element list (backward compatible)."""
    jdir = tmp_path / "judge"
    jdir.mkdir()
    (jdir / "bilan1.docx.verdict.json").write_text(
        json.dumps(_verdict("bilan1.docx", {"x": (3, 4)})), encoding="utf-8"
    )
    got = panel.load_case_verdicts(jdir, "bilan1.docx")
    assert len(got) == 1
    assert got[0]["case_id"] == "bilan1.docx"


def test_load_case_verdicts_panel_members(tmp_path):
    jdir = tmp_path / "judge"
    jdir.mkdir()
    for k in (1, 2, 3):
        (jdir / f"bilan1.docx.j{k}.verdict.json").write_text(
            json.dumps(_verdict("bilan1.docx", {"x": (k, 5)})), encoding="utf-8"
        )
    got = panel.load_case_verdicts(jdir, "bilan1.docx")
    assert len(got) == 3
    # sorted by k -> prod_scores 1,2,3 in order
    assert [g["dimensions"]["x"]["prod_score"] for g in got] == [1, 2, 3]


def test_load_case_verdicts_prefers_panel_over_single(tmp_path):
    """If both panel members and a single file exist, panel members win."""
    jdir = tmp_path / "judge"
    jdir.mkdir()
    (jdir / "c.verdict.json").write_text(json.dumps(_verdict("c", {"x": (9, 9)})),
                                         encoding="utf-8")
    (jdir / "c.j1.verdict.json").write_text(json.dumps(_verdict("c", {"x": (1, 5)})),
                                            encoding="utf-8")
    (jdir / "c.j2.verdict.json").write_text(json.dumps(_verdict("c", {"x": (2, 5)})),
                                            encoding="utf-8")
    got = panel.load_case_verdicts(jdir, "c")
    assert len(got) == 2  # panel members, not the single


def test_load_case_verdicts_absent(tmp_path):
    jdir = tmp_path / "judge"
    jdir.mkdir()
    assert panel.load_case_verdicts(jdir, "nope") == []


def test_single_verdict_aggregates_to_itself():
    """A 1-verdict panel returns its scores unchanged with mean_stddev 0 (the backward-
    compat guarantee report.py relies on)."""
    v = _verdict("c", {"mapping_correctness": (4, 5), "table_structure": (3, 4)},
                 defects=[{"category": "mapping", "severity": "high", "detail": "x"}])
    out = panel.aggregate_panel([v])
    assert out["dimensions"]["mapping_correctness"]["prod_score"] == 4
    assert out["dimensions"]["table_structure"]["prod_score"] == 3
    assert out["agreement"]["mean_stddev"] == 0.0
    assert out["agreement"]["n_judges"] == 1
    # ceil(1/2)=1 -> the single judge's defect is kept
    assert len(out["defects"]) == 1


def test_case_id_helpers():
    assert panel.case_id_of("bilan1.docx.verdict.json") == "bilan1.docx"
    assert panel.case_id_of("bilan1.docx.j3.verdict.json") == "bilan1.docx"
    assert panel.is_panel_member("bilan1.docx.j3.verdict.json") is True
    assert panel.is_panel_member("bilan1.docx.verdict.json") is False