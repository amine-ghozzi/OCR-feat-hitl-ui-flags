"""Golden sweep (MEA-1) — record schema, compare, whitelist rules, corpus facts.

No pipeline run anywhere in this file: the snapshot fixture is synthetic, and the
record-building test hands `build_record` a duck-typed stand-in. The gate has to be
testable without a 40-minute sweep or it will not be trusted enough to be used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))            # tools.* imports
sys.path.insert(0, str(ROOT / "src"))    # house idiom

from tools.extraction_benchmark import golden as G  # noqa: E402
import tools.golden_sweep as cli  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "golden"
PUBLIC = "public_doc.pdf"
SECRET = "secret_doc.pdf"


def _snapshot():
    return json.loads((FIXTURES / "snapshot_a.json").read_text(encoding="utf-8"))


# --- the record may never carry document content ---------------------------
def _fake_result():
    """An ExtractionResult stand-in whose every field is stuffed with document text,
    so anything that leaks is visible."""
    cell = SimpleNamespace(raw_value="Total des actifs", parsed_value="14267969.725",
                           flag="yellow", basis="millimes")
    row = SimpleNamespace(raw_text="TOTAL DES ACTIFS NON COURANTS",
                          cells={"brut": cell, "net": cell})
    ct = SimpleNamespace(rows=[row, row], match_rate=0.8125, to_dict=lambda: {})
    return SimpleNamespace(
        text="Bilan au 31 décembre 2024 — TOTAL DES ACTIFS 14 267 969,725",
        tables=[[{"label": "TOTAL DES ACTIFS", "net": "14 267 969,725"}], []],
        qcs_score=0.64217,
        tier_used=1,
        confidence_details={"tier_attempts": [{"tier": 1, "name": "Docling",
                                               "passed": True, "qcs": 0.64217}]},
        canonical_tables=[ct, None],
        metadata={
            "schema_version": 2,
            "doc_type": "docling_ocr",
            "hitl_required": False,
            # WP-2.7: the pipeline now states the grade and which measure
            # `qcs_score` holds, instead of the record re-deriving them.
            "qcs_grade": "fair",
            "score_kind": "qcs",
            # WP-2.8: what Docling said about the READ. The stand-in carries the
            # full payload the extractor writes — scores, per-page breakdown and
            # all — precisely so the record can be shown to keep only the five
            # grades out of it. `low` is lowercase here on purpose: docling's
            # enum is, and a record that echoed it would break the one grade
            # vocabulary (RTE-2).
            "docling_confidence": {
                "scores": {"ocr": 0.912, "layout": 0.774, "parse": 0.51,
                           "table": None, "mean": 0.732, "low": 0.51},
                "grades": {"ocr": "EXCELLENT", "layout": "GOOD", "parse": "FAIR",
                           "table": None, "mean": "GOOD", "low": "fair"},
                "pages": {1: {"ocr": 0.912, "parse": 0.51}},
            },
            "read_floor": {
                "pages_total": 4, "image_only_pages": 4,
                "image_only_pages_without_text": 0, "pages_without_text": 0,
                "cells_total": 120, "cells_blank": 30,
                "empty_region_share": 0.25, "ocr_coverage": 1.0,
                "flagged": False,
            },
            # WP-2.8: what the QCS WOULD be on those grades. Deliberately far
            # from `qcs_score` (0.642) so the record can be shown to keep the
            # two apart — the pipeline acted on 0.642 and this number routed
            # nothing.
            "qcs_docling": 0.8314,
            "qcs_docling_grade": "good",
            "statements": [
                {"id": 1, "qualified_type": "bilan_actif", "gate": "pass",
                 "gate_reason": None, "pages": [2, 3], "table_indices": [0]},
                {"id": 2, "qualified_type": None, "gate": "quarantined",
                 "gate_reason": "low_margin", "pages": [4], "table_indices": [1]},
            ],
            "green_flag_passed": False,
            "green_flag_failures": ["G1: match rate 63.20% < 80%"],
            "validation_report": {
                "passed_checks": 2, "total_checks": 4, "skipped_checks": 1,
                "failed_checks": 1, "critical_failures": 1,
                "checks": [
                    {"name": "Balance Sheet Balance", "severity": "CRITICAL",
                     "passed": False, "skipped": False},
                    {"name": "VNC: TOTAL DES ACTIFS NON", "severity": "CRITICAL",
                     "passed": False, "skipped": False},
                ],
            },
        },
    )


def test_record_has_no_values_or_text(tmp_path):
    doc = {"doc_id": "x.pdf", "path": "Samples/x.pdf", "public": True, "unstable": False}
    src = tmp_path / "x.pdf"
    src.write_bytes(b"%PDF-1.4 not really a pdf")

    rec = G.build_record(doc, _fake_result(), seconds=1.0, env={"engine": "auto"},
                         source_path=src)

    # 1. the schema is closed — no field may appear that the baseline does not define
    assert set(rec) <= set(G.RECORD_FIELDS)
    G.assert_record_schema(rec)
    assert set(rec["statements"][0]) <= set(G.STATEMENT_FIELDS)

    # 2. no `text` key anywhere (the acceptance grep: rg -c '"text"' snapshot.json -> 0)
    blob = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
    assert '"text"' not in blob
    assert '"raw_value"' not in blob and '"parsed_value"' not in blob

    # 3. and no document content: not a label, not a value, not a line of text
    for leak in ("TOTAL DES ACTIFS", "Total des actifs", "14 267 969", "14267969",
                 "Bilan au 31", "décembre"):
        assert leak not in blob, f"record leaked {leak!r}"

    # 4. the structural facts it DOES carry are the right ones
    assert rec["tier"] == 1 and rec["qcs"] == 0.642 and rec["score_kind"] == "qcs"
    assert rec["canonical_count"] == 2 and rec["canonical_none_count"] == 1
    assert rec["table_count"] == 2 and rec["nonempty_tables"] == 1
    assert rec["quarantined"] == 1
    assert rec["statements"][0]["rows"] == 2 and rec["statements"][0]["cols"] == 2
    assert len(rec["statements"][0]["values_sha"]) == 64
    assert rec["green_flag_failures"] == ["G1"]        # gate id only, not the sentence
    assert rec["text_chars"] == len(_fake_result().text)
    # WP-2.9: `unit` is part of the closed schema. This stand-in's canonical
    # tables carry no metadata, so the document states no unit — None, never
    # invented. It is a scale label, never a figure: nothing it can leak.
    assert "unit" in G.RECORD_FIELDS and rec["unit"] is None
    # WP-2.10: `dot_millimes_cells` likewise. A COUNT — of raw cells typed
    # "14 267 969.725" — so it is 0, never null, even for this stand-in whose
    # canonical tables carry no metadata at all. A count is the one thing a
    # non-public document may have printed about it, and it can leak neither a
    # value nor a label.
    assert "dot_millimes_cells" in G.RECORD_FIELDS and rec["dot_millimes_cells"] == 0
    assert isinstance(rec["dot_millimes_cells"], int)
    # WP-2.7 (RTE-2/RTE-10): the grade travels with the score, and it is read
    # from the pipeline rather than re-derived from a band this tool does not
    # own. One of four words — never a value, never a label.
    assert "qcs_grade" in G.RECORD_FIELDS and rec["qcs_grade"] == "fair"
    assert rec["qcs_grade"] in {"excellent", "good", "fair", "poor", "unknown"}
    # WP-2.8 (RTE-3): the GRADES Docling gave the read, and only those. The raw
    # scores and the per-page breakdown the pipeline carries stay out of the
    # record — a float drifts with every model update without telling anyone
    # anything a grade does not. Uppercase, one vocabulary (RTE-2), so the
    # lowercase "fair" the stand-in supplies comes back normalised.
    assert "docling_confidence" in G.RECORD_FIELDS
    assert rec["docling_confidence"] == {
        "ocr": "EXCELLENT", "layout": "GOOD", "parse": "FAIR",
        "low": "FAIR", "mean": "GOOD",
    }
    assert "scores" not in json.dumps(rec["docling_confidence"])
    # WP-2.8 (RTE-4): the floor's verdict is a bool — a fact about whether the
    # document was read, never about what it says.
    assert "read_floor_flagged" in G.RECORD_FIELDS
    assert rec["read_floor_flagged"] is False
    # WP-2.8 (RTE-3, the P7 input): the QCS Docling's grades WOULD have given,
    # recorded beside the one the pipeline acted on and never confused with it.
    # `qcs` is 0.642 — what routed; `qcs_docling` is 0.831 — what did not.
    assert "qcs_docling" in G.RECORD_FIELDS
    assert rec["qcs_docling"] == 0.831 and rec["qcs"] == 0.642
    assert "qcs_docling" not in (rec["qcs_grade"], rec["score_kind"])
    # …and all three read null rather than inventing an answer when no Docling
    # pass stands behind the record (a Tier-0 accept).
    tier0 = _fake_result()
    tier0.metadata = {k: v for k, v in tier0.metadata.items()
                      if k not in ("docling_confidence", "read_floor", "qcs_docling")}
    bare = G.build_record(doc, tier0, source_path=src)
    assert bare["docling_confidence"] is None and bare["read_floor_flagged"] is None
    assert bare["qcs_docling"] is None


def test_critical_rule_ids_never_carry_an_interpolated_label():
    """Most check names are literals; a few interpolate a row label. The allowlist keeps
    the literal ones readable and reduces anything else to a digest."""
    rec = G.build_record({"doc_id": "x", "path": "Samples/x.pdf", "public": True},
                         _fake_result(), source_path=Path("missing.pdf"))
    ids = rec["critical_rule_ids"]
    assert "Balance Sheet Balance" in ids
    assert not any("ACTIFS" in i for i in ids)
    assert any(i.startswith("rule:") for i in ids)


def test_nonempty_tables_counts_lists():
    """MEA-11: pipeline tables are lists of record dicts. The probe this replaces asked
    them for a DataFrame `.size` and reported 0 beside a 16-table canonical count."""
    import pandas as pd

    assert G.count_nonempty([[{"a": 1}], [], [{"a": 1}, {"a": 2}]]) == 2
    assert G.count_nonempty([]) == 0
    assert G.count_nonempty([[], [], []]) == 0
    assert G.count_nonempty(None) == 0
    assert G.count_nonempty([None, [{"a": 1}]]) == 1          # placeholders don't count
    # and the DataFrame shape a tier uses internally still works
    assert G.count_nonempty([pd.DataFrame({"a": [1, 2]}), pd.DataFrame()]) == 1
    assert G.frame_shape([{"a": 1, "b": 2}, {"a": 3}]) == (2, 2)
    assert G.frame_shape(pd.DataFrame({"a": [1, 2], "b": [3, 4]})) == (2, 2)


# --- compare ---------------------------------------------------------------
def test_compare_self_is_zero_diffs(capsys):
    snap = _snapshot()
    report = G.compare(snap, snap)
    assert report["totals"]["diffs"] == 0
    assert report["totals"]["missing"] == 0
    assert report["exit_code"] == 0


def test_compare_self_via_cli_is_zero(tmp_path, capsys):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(_snapshot()), encoding="utf-8")
    code = cli.main(["compare", "--baseline", str(p), "--candidate", str(p),
                     "--diff-dir", str(tmp_path / "diffs")])
    assert code == 0
    assert "0 diffs" in capsys.readouterr().out


def test_compare_unexpected_exits_1(tmp_path, capsys):
    base = _snapshot()
    cand = _snapshot()
    cand["docs"][PUBLIC]["table_count"] = 9
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    b.write_text(json.dumps(base), encoding="utf-8")
    c.write_text(json.dumps(cand), encoding="utf-8")

    code = cli.main(["compare", "--baseline", str(b), "--candidate", str(c),
                     "--diff-dir", str(tmp_path / "diffs")])
    out = capsys.readouterr().out
    assert code == 1
    assert "!! table_count" in out and "1 unexpected" in out


def test_compare_missing_doc_exits_2(tmp_path, capsys):
    base = _snapshot()
    cand = _snapshot()
    del cand["docs"][PUBLIC]
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    b.write_text(json.dumps(base), encoding="utf-8")
    c.write_text(json.dumps(cand), encoding="utf-8")
    assert cli.main(["compare", "--baseline", str(b), "--candidate", str(c),
                     "--diff-dir", str(tmp_path / "d")]) == 2
    assert "MISSING" in capsys.readouterr().out


def test_compare_excludes_timing_unless_asked(tmp_path):
    base, cand = _snapshot(), _snapshot()
    cand["docs"][PUBLIC]["seconds"] = 99.9
    cand["docs"][PUBLIC]["env"] = {"engine": "rapidocr", "host": "container"}
    assert G.compare(base, cand)["totals"]["diffs"] == 0
    assert G.compare(base, cand, include_timing=True)["totals"]["diffs"] == 2


def test_unstable_doc_diffs_do_not_fail_the_gate():
    """A document the manifest records as non-deterministic still SHOWS its diffs — it
    just does not fail the compare. None is flagged today; the flag exists so a genuinely
    unstable document is recorded as such instead of weakening every other comparison."""
    base, cand = _snapshot(), _snapshot()
    cand["docs"][PUBLIC]["qcs"] = 0.9
    assert G.compare(base, cand)["exit_code"] == 1
    report = G.compare(base, cand, unstable=[PUBLIC])
    assert report["exit_code"] == 0
    assert report["totals"]["diffs"] == 1 and report["totals"]["unexpected"] == 0
    assert report["docs"][0]["unstable"] is True


def test_expect_rules_any_added_min_from_to_tol():
    M = G._MISSING
    assert G.rule_allows("any", 1, 2)
    assert G.rule_allows("added", None, [1]) and not G.rule_allows("added", [1], [2])
    assert G.rule_allows("added", M, 5)
    assert G.rule_allows("removed", [1], None) and not G.rule_allows("removed", None, [1])
    assert G.rule_allows({"to": 16}, 0, 16) and not G.rule_allows({"to": 16}, 0, 15)
    assert G.rule_allows({"from": 0, "to": 16}, 0, 16)
    assert not G.rule_allows({"from": 1, "to": 16}, 0, 16)   # every key must hold
    assert G.rule_allows({"min": 20000}, 29, 25367)
    assert not G.rule_allows({"min": 20000}, 29, 19999)
    assert G.rule_allows({"max": 5}, 9, 4) and not G.rule_allows({"max": 5}, 9, 6)
    assert G.rule_allows({"up": True}, 0.5, 0.6)
    assert G.rule_allows({"up": True}, 0.5, 0.5)             # "never decreases"
    assert not G.rule_allows({"up": True}, 0.5, 0.4)
    assert G.rule_allows({"tol": 0.005}, 0.640, 0.642)
    assert not G.rule_allows({"tol": 0.005}, 0.640, 0.700)
    # the WP-2.1 form: both keys together
    assert G.rule_allows({"from": 29, "min": 20000}, 29, 25367)
    assert not G.rule_allows({"from": 29, "min": 20000}, 30, 25367)
    # no rule, or an unknown key, is never a licence
    assert not G.rule_allows(None, 1, 2)
    assert not G.rule_allows({"whatever": 1}, 1, 2)


def test_expected_rules_per_doc_overrides_star():
    exp = G.ExpectedRules({
        "change": "WP-2.1",
        "tolerance": {"qcs": 0.005},
        "docs": {"*": {"table_count": "any"},
                 PUBLIC: {"table_count": {"to": 9}, "tier": "any"}},
    })
    assert exp.rules_for("other.pdf") == {"table_count": "any"}
    assert exp.rules_for(PUBLIC) == {"table_count": {"to": 9}, "tier": "any"}


def test_tolerance_absorbs_a_qcs_wobble():
    base, cand = _snapshot(), _snapshot()
    cand["docs"][PUBLIC]["qcs"] = 0.645
    exp = G.ExpectedRules({"tolerance": {"qcs": 0.005}})
    assert G.compare(base, cand, exp)["totals"]["diffs"] == 0
    cand["docs"][PUBLIC]["qcs"] = 0.7
    assert G.compare(base, cand, exp)["totals"]["unexpected"] == 1


def test_whitelisted_diff_exits_0(tmp_path, capsys):
    import yaml

    base, cand = _snapshot(), _snapshot()
    cand["docs"][PUBLIC]["text_chars"] = 25367
    cand["docs"][PUBLIC]["table_count"] = 16
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    base["docs"][PUBLIC]["text_chars"] = 29
    base["docs"][PUBLIC]["table_count"] = 0
    b.write_text(json.dumps(base), encoding="utf-8")
    c.write_text(json.dumps(cand), encoding="utf-8")
    e = tmp_path / "e.yaml"
    e.write_text(yaml.safe_dump({
        "change": "WP-2.1",
        "docs": {PUBLIC: {"text_chars": {"from": 29, "min": 20000},
                          "table_count": {"from": 0, "to": 16}}},
    }), encoding="utf-8")

    code = cli.main(["compare", "--baseline", str(b), "--candidate", str(c),
                     "--expect", str(e), "--diff-dir", str(tmp_path / "d")])
    out = capsys.readouterr().out
    assert code == 0
    assert "WP-2.1" in out and "2 expected, 0 unexpected" in out
    assert "!!" not in out


def test_nonpublic_doc_prints_counts_only(tmp_path, capsys):
    base, cand = _snapshot(), _snapshot()
    cand["docs"][SECRET]["table_count"] = 11
    cand["docs"][SECRET]["statements"][0]["pages"] = [6, 7]
    cand["docs"][SECRET]["statements"][0]["values_sha"] = "deadbeef"
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    b.write_text(json.dumps(base), encoding="utf-8")
    c.write_text(json.dumps(cand), encoding="utf-8")
    diff_dir = tmp_path / "diffs"

    code = cli.main(["compare", "--baseline", str(b), "--candidate", str(c),
                     "--only", SECRET, "--diff-dir", str(diff_dir)])
    out = capsys.readouterr().out

    assert code == 1
    assert f"{SECRET}: diffs=2 expected=0 unexpected=2" in out

    # counts ONLY — not a field name, not a value, not a page number. Judged on
    # the document's own RECORD LINE, up to the "[non-public — detail in …]"
    # marker: everything after it is a file path this run chose, and pytest's
    # `tmp_path` carries a per-run counter, so on the 11th run of this test the
    # path itself contains "11" and a whole-stdout scan fails for a reason that
    # has nothing to do with the leak it is guarding against.
    record = next(ln for ln in out.splitlines() if ln.startswith(f"{SECRET}: "))
    record = record.split("  [non-public", 1)[0]
    for forbidden in ("table_count", "statements", "values_sha", "deadbeef",
                      "bilan_actif", "11", "[6, 7]"):
        assert forbidden not in record, f"non-public detail leaked to stdout: {forbidden!r}"
    # …and the values themselves are nowhere on stdout at all, path or no path.
    for forbidden in ("deadbeef", "bilan_actif", "[6, 7]"):
        assert forbidden not in out, f"non-public detail leaked to stdout: {forbidden!r}"

    # …and the detail is on disk, in the gitignored diff dir
    detail = json.loads((diff_dir / f"{SECRET}.json").read_text(encoding="utf-8"))
    assert {d["field"] for d in detail["diffs"]} == {"table_count", "statements"}


def test_public_doc_prints_field_level_detail(tmp_path, capsys):
    base, cand = _snapshot(), _snapshot()
    cand["docs"][PUBLIC]["tier"] = 3
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    b.write_text(json.dumps(base), encoding="utf-8")
    c.write_text(json.dumps(cand), encoding="utf-8")
    cli.main(["compare", "--baseline", str(b), "--candidate", str(c),
              "--only", PUBLIC, "--diff-dir", str(tmp_path / "d")])
    out = capsys.readouterr().out
    assert "tier: 1 -> 3" in out


# --- corpus facts ----------------------------------------------------------
def _facts(**over):
    base = {"id": "F-TEST", "doc_id": PUBLIC, "expect": "pass",
            "check": {"fn": "statement_pages_superset", "args": {"pages": [2, 3, 4]}}}
    base.update(over)
    return [base]


def test_facts_known_fail_flip_exits_3(tmp_path, capsys):
    import yaml

    # HEAD: the primary statements land on pages 6 and 7, not 2-4 (the CLS-1 symptom)
    snap = _snapshot()
    snap["docs"][PUBLIC]["statements"][0]["pages"] = [6]
    snap["docs"][PUBLIC]["statements"][1]["pages"] = [7]
    p = tmp_path / "s.json"
    p.write_text(json.dumps(snap), encoding="utf-8")

    # recorded known-fail, and it still fails -> exit 0
    f = tmp_path / "f.yaml"
    f.write_text(yaml.safe_dump({"facts": _facts(expect="known-fail")}), encoding="utf-8")
    assert cli.main(["facts", "--snapshot", str(p), "--facts", str(f)]) == 0
    assert "ok" in capsys.readouterr().out

    # the fix lands: pages 2,3,4 are now covered -> the known-fail PASSES -> exit 3
    snap["docs"][PUBLIC]["statements"][0]["pages"] = [2, 3]
    snap["docs"][PUBLIC]["statements"][1]["pages"] = [4]
    p.write_text(json.dumps(snap), encoding="utf-8")
    assert cli.main(["facts", "--snapshot", str(p), "--facts", str(f)]) == 3
    out = capsys.readouterr().out
    assert "FLIP" in out and "now PASSES" in out

    # and the mirror image: a recorded pass that stops passing is also a flip
    f.write_text(yaml.safe_dump({"facts": _facts(
        expect="pass",
        check={"fn": "field", "args": {"field": "table_count", "eq": 99}})}),
        encoding="utf-8")
    assert cli.main(["facts", "--snapshot", str(p), "--facts", str(f)]) == 3
    assert "now FAILS" in capsys.readouterr().out


def test_facts_absent_doc_is_reported_not_an_error(tmp_path, capsys):
    """A `--only` smoke sweep holds few documents; facts about the rest must report
    'not in snapshot', never raise and never move the exit code."""
    import yaml

    p = tmp_path / "s.json"
    p.write_text(json.dumps(_snapshot()), encoding="utf-8")
    f = tmp_path / "f.yaml"
    f.write_text(yaml.safe_dump({"facts": _facts(doc_id="absent.pdf",
                                                 expect="known-fail")}), encoding="utf-8")
    assert cli.main(["facts", "--snapshot", str(p), "--facts", str(f)]) == 0
    out = capsys.readouterr().out
    assert "skipped" in out and "not in snapshot" in out


def test_facts_manual_is_reported_only(tmp_path, capsys):
    import yaml

    p = tmp_path / "s.json"
    p.write_text(json.dumps(_snapshot()), encoding="utf-8")
    f = tmp_path / "f.yaml"
    f.write_text(yaml.safe_dump({"facts": _facts(expect="manual")}), encoding="utf-8")
    assert cli.main(["facts", "--snapshot", str(p), "--facts", str(f)]) == 0
    assert "manual" in capsys.readouterr().out


def test_fact_checks_cover_the_registry():
    rec = _snapshot()["docs"][SECRET]
    ev = G.evaluate_check
    assert not ev(rec, {"fn": "statement_pages_superset", "args": {"pages": [2, 3, 4]}})
    assert ev(rec, {"fn": "statement_pages_superset", "args": {"pages": [6, 7]}})
    assert ev(rec, {"fn": "count_qualified_type", "args": {"type": "bilan_actif", "eq": 1}})
    assert ev(rec, {"fn": "some_statement_covers", "args": {"pages": [8]}})
    assert not ev(rec, {"fn": "some_statement_covers", "args": {"pages": [6, 7]}})
    assert ev(rec, {"fn": "no_statement_covers", "args": {"spans": [[6, 7], [8, 9]]}})
    # statement #3 lists page 11 twice — a same-page merge (E-5)
    assert not ev(rec, {"fn": "no_repeated_page_within_statement"})
    assert ev(rec, {"fn": "statement_on_page",
                    "args": {"page": 7, "qualified_type": "bilan_passif", "min_rows": 18}})
    assert not ev(rec, {"fn": "statement_on_page", "args": {"page": 7, "min_rows": 19}})
    assert ev(rec, {"all": [{"fn": "field", "args": {"field": "table_count", "eq": 9}},
                            {"fn": "field", "args": {"field": "text_chars", "min": 20000}}]})
    assert ev(rec, {"any": [{"fn": "field", "args": {"field": "table_count", "eq": 0}},
                            {"fn": "field", "args": {"field": "tier", "eq": 3}}]})
    # WP-2.9 — `not_null` states "this was identified" without pinning a value
    # that legitimately differs per document (F-UNIT).
    assert ev(rec, {"fn": "field", "args": {"field": "unit", "not_null": True}})
    assert not ev(rec, {"fn": "field", "args": {"field": "unit", "not_null": False}})
    assert ev(rec, {"fn": "field", "args": {"field": "hitl_reason", "not_null": False}})
    assert not ev(rec, {"fn": "field", "args": {"field": "hitl_reason", "not_null": True}})
    with pytest.raises(G.FactError):
        ev(rec, {"fn": "exec_whatever_i_like"})


# --- the manifest and the fact file are themselves data worth checking ------
def test_corpus_manifest_matches_the_samples_on_disk():
    corpus = G.load_corpus()
    assert len(corpus) == 16
    assert sum(1 for d in corpus if not d["public"]) == 5
    assert len({d["doc_id"] for d in corpus}) == 16
    for d in corpus:
        assert (ROOT / d["path"]).exists(), f"{d['doc_id']} -> {d['path']} missing"
    # the root copy of EF CLB is byte-identical to the Sample - OF one and is excluded
    assert not any(d["path"] == "Samples/EF CLB 2024 Provisoire (1).pdf" for d in corpus)
    assert (ROOT / "Samples" / "EF CLB 2024 Provisoire (1).pdf").read_bytes() == \
           (ROOT / "Samples" / "Sample - OF" / "EF CLB 2024 Provisoire (1).pdf").read_bytes()


def test_corpus_facts_are_well_formed():
    corpus = {d["doc_id"] for d in G.load_corpus()}
    facts = G.load_facts()
    ids = [f["id"] for f in facts]
    assert len(ids) == len(set(ids))
    for f in facts:
        assert f["expect"] in ("pass", "known-fail", "manual"), f["id"]
        doc_ids = [f["doc_id"]] if isinstance(f["doc_id"], str) else f["doc_id"]
        for d in doc_ids:
            assert d in corpus, f"{f['id']} names {d}, which is not in corpus.yaml"
        # every check resolves against the registry (evaluated on an empty record)
        G.evaluate_check({"statements": [], "table_count": 0}, f["check"])


def test_snapshot_refuses_without_the_variation_log_env(tmp_path, capsys, monkeypatch):
    """A sweep with no FINALZE_VARIATION_LOG would dirty the tracked learning log, and
    a baseline taken from a dirty worktree is not reproducible (GOV-4 / MEA-1)."""
    monkeypatch.delenv(G.VARIATION_LOG_ENV, raising=False)
    code = cli.main(["snapshot", "--out", str(tmp_path / "s.json")])
    out = capsys.readouterr().out
    assert code == 2
    assert G.VARIATION_LOG_ENV in out and "refusing" in out.lower()
    assert not (tmp_path / "s.json").exists()

    monkeypatch.setenv(G.VARIATION_LOG_ENV, "   ")     # blank is not configured either
    assert cli.main(["snapshot", "--out", str(tmp_path / "s.json")]) == 2


def test_snapshot_pins_the_hash_seed_only_when_nobody_chose_one(monkeypatch):
    """`text_sha` does not reproduce across processes unless PYTHONHASHSEED is pinned —
    measured on the six DOCX: same char count, same QCS, different digest. The sweep
    therefore restarts under seed 0, but never overrides an explicit choice."""
    monkeypatch.delenv(G.HASH_SEED_ENV, raising=False)
    assert G.needs_hash_seed_reexec() is True
    monkeypatch.setenv(G.HASH_SEED_ENV, "0")
    assert G.needs_hash_seed_reexec() is False
    monkeypatch.setenv(G.HASH_SEED_ENV, "random")     # a deliberate choice, respected
    assert G.needs_hash_seed_reexec() is False
    assert G.needs_hash_seed_reexec({}) is True
    assert G.needs_hash_seed_reexec({G.HASH_SEED_ENV: "7"}) is False


def test_snapshot_rejects_unknown_only_and_parallel_jobs(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(G.VARIATION_LOG_ENV, str(tmp_path / "log.jsonl"))
    assert cli.main(["snapshot", "--out", str(tmp_path / "s.json"),
                     "--only", "not_a_document.pdf"]) == 2
    assert "corpus manifest" in capsys.readouterr().out

    with pytest.raises(ValueError, match="fork-safe"):
        G.run_snapshot([], jobs=4)


def test_select_docs_matches_doc_id_or_file_name():
    corpus = G.load_corpus()
    chosen = G.select_docs(corpus, ["Bilan_1.jpg", "Etat résultat 2.docx"])
    assert [d["doc_id"] for d in chosen] == ["Bilan_1.jpg", "Etat_r_sultat_2.docx"]
    assert len(G.select_docs(corpus, None)) == 16
