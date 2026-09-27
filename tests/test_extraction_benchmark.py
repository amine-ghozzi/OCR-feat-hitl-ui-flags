"""extraction_benchmark — deterministic-stage tests (config helpers, sample, prompts).

The agent stages (reference/judge) and the heavy `cases` pipeline run are exercised by
the dry-run, not here; these tests stay fast + hermetic by pointing config at a tmp repo.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.extraction_benchmark import config as C  # noqa: E402
from tools.extraction_benchmark import sample as sample_stage  # noqa: E402


def test_qcs_band_buckets():
    assert C.qcs_band(0.9) == "high"
    assert C.qcs_band(0.6) == "medium"
    assert C.qcs_band(0.2) == "low"
    assert C.qcs_band(None) == "unknown"


def test_is_fixture_narrow_markers():
    assert C.is_fixture("smoke_test.pdf")
    assert C.is_fixture("mock_bilan.docx")
    # the curated corpus must NOT be excluded (no false match on "Samples"/"sample")
    assert not C.is_fixture("Bilan 1.jpg")
    assert not C.is_fixture("Etat resultat 2.docx")


def test_case_id_is_filesystem_safe():
    assert C.case_id("Etat résultat 1.docx") == "Etat_r_sultat_1.docx"
    assert C.case_id("Samples/Bilan 1.jpg") == "Bilan_1.jpg"


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    samples = tmp_path / "Samples"
    samples.mkdir()
    for name in ["bilan1.docx", "bilan2.docx", "Etat resultat 1.docx",
                 "EF flux tresorerie.pdf", "smoke_fixture.pdf", "notes.txt"]:
        (samples / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(C, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(C, "SAMPLES_DIR", samples)
    monkeypatch.setattr(C, "RUNS_ROOT", tmp_path / "runs")
    return tmp_path


def test_sample_stratifies_and_excludes_fixtures(tmp_repo):
    res = sample_stage.run("t1", limit=10, source="local")
    rows = C.read_json(C.run_dir("t1") / "_sample.json")
    names = {r["case_id"] for r in rows}
    assert "smoke_fixture.pdf" not in names           # fixture excluded
    assert "notes.txt" not in names                   # unsupported extension excluded
    types = {r["statement_type"] for r in rows}
    assert {"bilan", "compte_resultat", "flux_tresorerie"} <= types
    assert res["count"] == 4 and all(r["tier"] is None for r in rows)


def test_sample_limit_spans_strata(tmp_repo):
    # With limit=3 the round-robin should pick one of each type before a second bilan.
    sample_stage.run("t2", limit=3, source="local")
    rows = C.read_json(C.run_dir("t2") / "_sample.json")
    assert len({r["statement_type"] for r in rows}) == 3   # all three types represented


def test_prompts_builds_packages_and_manifest(tmp_repo):
    sample_stage.run("t3", limit=4, source="local")
    from tools.extraction_benchmark import prompts as prompts_stage
    out = prompts_stage.run("t3")
    assert out["prompts"] == 4 and out["pending_reference"] == 4
    pdir = C.run_dir("t3") / "prompts"
    a_manifest = next(pdir.glob("*.manifest.json"))
    m = C.read_json(a_manifest)
    assert m["reference_out"].startswith("reference/") and m["prompt_file"].endswith(".input.md")
    assert (pdir / Path(m["prompt_file"]).name).exists()
    tasks = C.read_json(C.run_dir("t3") / "reference" / "_tasks.json")
    assert len(tasks) == 4 and all(not t["done"] for t in tasks)


# --- GOV-3: the reference oracle may only be pointed at public cases -------
def _write_corpus(root: Path, docs: dict) -> Path:
    """Write a minimal `tools/golden/corpus.yaml` under `root`. docs: {doc_id: public}."""
    lines = ["docs:"]
    for doc_id, public in docs.items():
        lines += [f"  - doc_id: {doc_id}",
                  f"    path: Samples/{doc_id}",
                  f"    public: {'true' if public else 'false'}"]
    path = root / "tools" / "golden" / "corpus.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _prompt_manifest(run: str, cid: str) -> None:
    C.write_json(C.run_dir(run) / "prompts" / f"{cid}.manifest.json", {
        "case_id": cid, "standard": "NCT", "statement_type": "bilan",
        "source_path": f"Samples/{cid}", "prompt_file": f"prompts/{cid}.input.md",
        "reference_out": f"reference/{cid}.json",
    })


def test_is_public_fails_closed(tmp_repo):
    # No manifest at all -> nothing is public.
    assert C.is_public("bilan1.docx") is False
    _write_corpus(tmp_repo, {"bilan1.docx": True, "EF_secret.pdf": False})
    assert C.is_public("bilan1.docx") is True          # listed public
    assert C.is_public("EF_secret.pdf") is False       # listed non-public
    assert C.is_public("never_listed.docx") is False   # absent -> fail closed


def test_reference_refuses_non_public(tmp_repo):
    from tools.extraction_benchmark import reference

    _write_corpus(tmp_repo, {"bilan1.docx": True, "EF_secret.pdf": False})
    _prompt_manifest("rg3", "bilan1.docx")
    _prompt_manifest("rg3", "EF_secret.pdf")
    _prompt_manifest("rg3", "unlisted.pdf")

    res = reference.prepare("rg3")
    assert sorted(res["refused"]) == ["EF_secret.pdf", "unlisted.pdf"]
    assert res["refused_reasons"]["EF_secret.pdf"] == "non-public (GOV-3)"
    assert "fail closed" in res["refused_reasons"]["unlisted.pdf"]
    assert res["total"] == 3 and res["pending"] == 1
    assert res["next"]["case_id"] == "bilan1.docx"      # a refused case is never `next`

    tasks = {t["case_id"]: t for t in C.read_json(C.run_dir("rg3") / "reference" / "_tasks.json")}
    assert tasks["EF_secret.pdf"]["refused"] is True
    assert tasks["unlisted.pdf"]["refused"] is True
    assert "refused" not in tasks["bilan1.docx"]
    # deterministic + re-runnable: a second prepare returns the same refusal set
    assert reference.prepare("rg3")["refused"] == res["refused"]


def test_reference_accepts_public(tmp_repo):
    from tools.extraction_benchmark import reference

    _write_corpus(tmp_repo, {"bilan1.docx": True, "bilan3.docx": True})
    _prompt_manifest("rg4", "bilan1.docx")
    _prompt_manifest("rg4", "bilan3.docx")
    C.write_json(C.run_dir("rg4") / "reference" / "bilan1.docx.json", {"tables": []})

    res = reference.prepare("rg4")
    assert res["refused"] == [] and res["refused_reasons"] == {}
    assert res["total"] == 2 and res["pending"] == 1
    assert res["next"]["case_id"] == "bilan3.docx"      # the one not yet done


def test_judge_refuses_non_public(tmp_repo):
    """The judge gate is independent: a hand-placed reference must not unblock a case."""
    from tools.extraction_benchmark import judge

    _write_corpus(tmp_repo, {"bilan1.docx": True, "EF_secret.pdf": False})
    run = "rg5"
    for cid in ("bilan1.docx", "EF_secret.pdf"):
        _prompt_manifest(run, cid)
        C.write_json(C.run_dir(run) / "cases" / f"{cid}.json", {"canonical_tables": []})
        # both get a reference file — the non-public one as if hand-placed
        C.write_json(C.run_dir(run) / "reference" / f"{cid}.json", {"tables": []})

    res = judge.prepare(run)
    assert res["refused"] == ["EF_secret.pdf"]
    assert res["refused_reasons"]["EF_secret.pdf"] == "non-public (GOV-3)"
    assert res["ready_pending"] == 1
    assert res["next"]["case_id"] == "bilan1.docx"      # a refused case is never `next`
    assert res["blocked"] == []                         # refused != waiting on a file

    tasks = {t["case_id"]: t for t in C.read_json(C.run_dir(run) / "judge" / "_tasks.json")}
    assert tasks["EF_secret.pdf"]["refused"] is True
    assert tasks["EF_secret.pdf"]["ready"] is False     # despite both files existing
    assert tasks["bilan1.docx"]["ready"] is True and "refused" not in tasks["bilan1.docx"]

    # panel mode refuses every slot of a refused case
    trio = judge.prepare(run, panel=3)
    assert trio["refused"] == ["EF_secret.pdf"]         # listed once, not per slot
    slots = [t for t in C.read_json(C.run_dir(run) / "judge" / "_tasks.json")
             if t["case_id"] == "EF_secret.pdf"]
    assert len(slots) == 3 and all(t["refused"] and not t["ready"] for t in slots)


@pytest.fixture
def tmp_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "RUNS_ROOT", tmp_path / "runs")
    return tmp_path


def _write_verdict(run, cid, dims, defects):
    C.write_json(
        C.run_dir(run) / "judge" / f"{cid}.verdict.json",
        {"case_id": cid, "dimensions": dims, "defects": defects, "summary": "x"},
    )


def test_report_aggregates_gates_and_promotes(tmp_runs):
    from tools.extraction_benchmark import report

    run = "r1"
    dims = {
        "cell_value_accuracy": {"prod_score": 5, "ref_score": 5, "winner": "tie"},
        "mapping_correctness": {"prod_score": 4, "ref_score": 5, "winner": "reference"},
    }
    _write_verdict(run, "c1", dims, [])
    _write_verdict(run, "c2", dims, [{"category": "mapping", "severity": "high",
                                      "detail": "d", "proposed_change": {"file": "f", "change": "c"}}])
    C.write_json(C.run_dir(run) / "reference" / "c1.json", {"tables": []})  # to promote

    out = report.run(run)
    assert out["cases_judged"] == 2 and out["defects"] == 1 and out["golden_promoted"] == 1
    summ = C.read_json(C.run_dir(run) / "report" / "summary.json")
    assert summ["benchmark_score"] == 0.9                       # mean(5,4)/5
    assert summ["defect_categories"]["mapping"] == 1 and summ["defect_severities"]["high"] == 1
    assert summ["gate"]["passed"] is False                      # 1 high defect > max 0
    assert summ["dimensions"]["mapping_correctness"]["gap"] == 1.0
    assert (C.run_dir(run) / "report" / "findings.md").exists()
    assert (C.run_dir(run) / "golden" / "c1.json").exists()


def test_report_empty_run(tmp_runs):
    from tools.extraction_benchmark import report

    out = report.run("empty")
    assert out["cases_judged"] == 0 and out["benchmark_score"] is None and out["gate_passed"] is False


def _write_panel_member(run, cid, k, dims, defects):
    C.write_json(
        C.run_dir(run) / "judge" / f"{cid}.j{k}.verdict.json",
        {"case_id": cid, "dimensions": dims, "defects": defects, "summary": "x"},
    )


def test_report_collapses_panel_to_consensus(tmp_runs):
    """Feature B: a 3-member panel collapses to a median-consensus verdict before the
    existing dimension/defect aggregation, and summary carries panel_agreement."""
    from tools.extraction_benchmark import report

    run = "rp"
    # mapping_correctness prod across judges: 5,1,1 -> median 1; table_structure: 4,4,4
    for k, mc in ((1, 5), (2, 1), (3, 1)):
        _write_panel_member(
            run, "c1", k,
            {"mapping_correctness": {"prod_score": mc, "ref_score": 5},
             "table_structure": {"prod_score": 4, "ref_score": 4}},
            [{"category": "mapping", "severity": "high", "detail": "wrong row"}]
            if k in (1, 2) else [],   # defect in 2/3 -> majority kept
        )
    out = report.run(run)

    summ = C.read_json(C.run_dir(run) / "report" / "summary.json")
    # one consensus case, not three raw verdicts
    assert summ["cases_judged"] == 1
    # consensus median prod for mapping_correctness = 1 -> prod_avg 1.0
    assert summ["dimensions"]["mapping_correctness"]["prod_avg"] == 1.0
    assert summ["dimensions"]["table_structure"]["prod_avg"] == 4.0
    # defect present in 2/3 judges survives the majority vote
    assert summ["defect_categories"].get("mapping") == 1
    # panel_agreement recorded; judges disagreed on mapping so mean_stddev > 0
    assert summ["panel_agreement"]["n_judges"] == 3
    assert summ["panel_agreement"]["mean_stddev"] > 0
    assert "Panel agreement" in (C.run_dir(run) / "report" / "findings.md").read_text(
        encoding="utf-8"
    )


def test_report_single_verdict_unchanged_panel_fields(tmp_runs):
    """Backward compat: a single-verdict run reports n_judges 1 and mean_stddev 0, and its
    dimension aggregation is identical to pre-panel behavior."""
    from tools.extraction_benchmark import report

    run = "rs"
    dims = {"mapping_correctness": {"prod_score": 4, "ref_score": 5},
            "table_structure": {"prod_score": 4, "ref_score": 5}}
    _write_verdict(run, "c1", dims, [])
    report.run(run)
    summ = C.read_json(C.run_dir(run) / "report" / "summary.json")
    assert summ["cases_judged"] == 1
    assert summ["panel_agreement"]["n_judges"] == 1
    assert summ["panel_agreement"]["mean_stddev"] == 0.0
    assert summ["dimensions"]["mapping_correctness"]["prod_avg"] == 4.0


class _FakeResult:
    def __init__(self, qcs, tier):
        self.text = "x"
        self.tables = [[{"label": "A", "2023": "1"}]]
        self.qcs_score = qcs
        self.tier_used = tier
        self.confidence_details = {}
        self.metadata = {}
        self.canonical_tables = []


def _install_fake_pipeline(monkeypatch, delay_s=0.0):
    import types as _t
    mod = _t.ModuleType("pipeline")

    class PipelineConfig:
        def __init__(self, **kw):
            self.kw = kw

    class FinAlzePipeline:
        def __init__(self, cfg):
            self.cfg = cfg

        def process_document(self, src, accounting_standard=None):
            if delay_s:
                time.sleep(delay_s)
            return _FakeResult(0.8, 1)

    mod.PipelineConfig = PipelineConfig
    mod.FinAlzePipeline = FinAlzePipeline
    monkeypatch.setitem(sys.modules, "pipeline", mod)


def test_cases_emits_runtime_and_resource_metrics(tmp_repo, monkeypatch):
    from tools.extraction_benchmark import cases as cases_stage

    _install_fake_pipeline(monkeypatch, delay_s=0.01)
    # two NCT cases under one pipeline → first carries load, second is steady-state
    (tmp_repo / "Samples" / "b1.docx").write_text("x", encoding="utf-8")
    (tmp_repo / "Samples" / "b2.docx").write_text("x", encoding="utf-8")
    C.write_json(C.run_dir("rm") / "_sample.json", [
        {"case_id": "b1.docx", "source_path": "Samples/b1.docx", "standard": "NCT"},
        {"case_id": "b2.docx", "source_path": "Samples/b2.docx", "standard": "NCT"},
    ])

    out = cases_stage.run("rm")
    assert out["cases"] == 2 and out["engine"] == "auto"

    em = C.read_json(C.run_dir("rm") / "_engine_metrics.json")
    assert em["n_cases"] == 2 and em["engine"] == "auto"
    assert len(em["per_case"]) == 2
    first, second = em["per_case"]
    assert first["first_for_pipeline"] is True and second["first_for_pipeline"] is False
    # every case carries a wall time; cpu/rss are present when psutil is available
    assert all(isinstance(m["wall_s"], float) for m in em["per_case"])
    assert em["totals"]["median_steady_wall_s"] is not None
    assert em["totals"]["model_load_wall_s"] is not None
    # the runtime block is also stitched back onto each sample row
    rows = C.read_json(C.run_dir("rm") / "_sample.json")
    assert rows[0]["runtime"]["wall_s"] == first["wall_s"]


def test_judge_prepare_panel_emits_n_slots(tmp_runs):
    """Feature B: judge --panel N emits N verdict slots per case; N=1 is single-file."""
    from tools.extraction_benchmark import judge

    run = "rj"
    C.write_json(C.run_dir(run) / "prompts" / "c1.manifest.json", {"case_id": "c1"})
    C.write_json(C.run_dir(run) / "cases" / "c1.json", {"canonical_tables": []})
    C.write_json(C.run_dir(run) / "reference" / "c1.json", {"tables": []})

    single = judge.prepare(run, panel=1)
    assert single["panel"] == 1 and single["total"] == 1
    tasks = C.read_json(C.run_dir(run) / "judge" / "_tasks.json")
    assert tasks[0]["verdict_out"] == "judge/c1.verdict.json"
    assert "panel_member" not in tasks[0]

    trio = judge.prepare(run, panel=3)
    assert trio["panel"] == 3 and trio["total"] == 3
    tasks = C.read_json(C.run_dir(run) / "judge" / "_tasks.json")
    outs = sorted(t["verdict_out"] for t in tasks)
    assert outs == ["judge/c1.j1.verdict.json", "judge/c1.j2.verdict.json",
                    "judge/c1.j3.verdict.json"]
    assert all(t["panel_member"] == k for t, k in zip(
        sorted(tasks, key=lambda x: x["verdict_out"]), (1, 2, 3)))
