"""Stage 6 — report: aggregate verdicts -> report/{summary.json,findings.md}; promote
references to golden/.

Deterministic. Reads `judge/*.verdict.json`, computes a benchmark score + per-dimension
prod-vs-reference gaps + a defect taxonomy (counts by category/severity — the
open->axial->count step), applies a threshold **gate**, and copies the reference
extractions into `golden/` as the versioned seed set.

The agent verdict is the primary signal. A DeepEval `GEval`/`assert_test` wrapper over the
same dimensions + goldens is the optional CI-gate upgrade (documented in the plan); not
wired here to avoid the extra dependency + a second judge invocation.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional

from . import config as C
from . import metrics
from . import panel

log = logging.getLogger(__name__)

# Gate thresholds (tune per corpus). benchmark_score is the L1-weighted blend below.
GATE_MIN_SCORE = 0.80
GATE_MAX_HIGH_DEFECTS = 0

# Evaluation-Stack weights — "apply in order: L1 deterministic FIRST, L2 LLM-judge
# SECOND". The deterministic metric is the PRIMARY signal: it catches the bilan1-class
# failures the stochastic judge missed (judge called a 0/51-correct case "1 defect" and
# ranked a 367-vs-304 better run LOWER). So L1 dominates the blend; the LLM keeps a
# minority vote, restricted to the two genuinely-subjective dimensions.
L1_WEIGHT = 0.7
L2_WEIGHT = 0.3
# The only judge dimensions NOT covered (better) by a deterministic check.
SUBJECTIVE_DIMS = ("mapping_correctness", "table_structure")


def _avg(xs: List[float]):
    return round(sum(xs) / len(xs), 3) if xs else None


def _safe_score_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Compute the L1 deterministic metrics, never raising. Returns None (and logs a
    note) when references are missing / unreadable, so the report degrades to LLM-only."""
    try:
        res = metrics.score_run(run_id)
        if res is None:
            log.info(
                "report: no L1 references matched for run %s — "
                "falling back to LLM-only score.", run_id,
            )
        return res
    except Exception as exc:  # never crash the report on a metrics issue
        log.warning("report: L1 deterministic scoring failed for %s (%s) — "
                    "falling back to LLM-only score.", run_id, exc)
        return None


def _blend(
    deterministic_score: Optional[float],
    llm_subjective_score: Optional[float],
    llm_judge_score: Optional[float],
) -> Optional[float]:
    """L1-heavy blend per the Evaluation-Stack rule (0.7 L1 + 0.3 L2-subjective).

    Degrades gracefully: missing L1 -> LLM-only; missing L2 -> L1 alone.
    """
    if deterministic_score is None:
        return llm_judge_score  # graceful fallback (e.g. references absent)
    if llm_subjective_score is None:
        return round(deterministic_score, 4)
    return round(L1_WEIGHT * deterministic_score + L2_WEIGHT * llm_subjective_score, 4)


def _load_consensus_verdicts(jdir) -> tuple[List[Dict[str, Any]], List[float], int]:
    """Collapse the judge dir into one consensus verdict per case.

    For each case, gather its panel members (`<case>.j<k>.verdict.json`) or its single
    verdict (`<case>.verdict.json`) and run `panel.aggregate_panel`. A single-verdict case
    collapses to itself (mean_stddev 0, n_judges 1), so single-judge runs are byte-for-byte
    unchanged in their dimension/defect aggregation. Returns the consensus verdicts plus
    the per-case mean_stddev list and the max n_judges seen (for `panel_agreement`)."""
    verdicts: List[Dict[str, Any]] = []
    stddevs: List[float] = []
    max_judges = 0
    if not jdir.exists():
        return verdicts, stddevs, max_judges
    # discover the distinct case ids from every verdict file in the dir.
    case_ids: List[str] = []
    seen = set()
    for p in sorted(jdir.glob("*.verdict.json")):
        cid = panel.case_id_of(p.name)
        if cid not in seen:
            seen.add(cid)
            case_ids.append(cid)
    for cid in case_ids:
        members = panel.load_case_verdicts(jdir, cid)
        if not members:
            continue
        consensus = panel.aggregate_panel(members)
        agreement = consensus.get("agreement", {})
        n_judges = agreement.get("n_judges", len(members))
        max_judges = max(max_judges, n_judges)
        if n_judges > 1:
            stddevs.append(agreement.get("mean_stddev", 0.0))
        # carry case_id through so defect rows keep their attribution.
        verdicts.append({"case_id": cid, **consensus})
    return verdicts, stddevs, max_judges


def run(run_id: str) -> Dict[str, Any]:
    jdir = C.run_dir(run_id) / "judge"
    refdir = C.run_dir(run_id) / "reference"
    verdicts, panel_stddevs, panel_n_judges = _load_consensus_verdicts(jdir)

    dim_scores: Dict[str, Dict[str, List[float]]] = {}
    defect_cat, defect_sev = Counter(), Counter()
    all_defects: List[Dict[str, Any]] = []
    for v in verdicts:
        for dim, s in (v.get("dimensions") or {}).items():
            d = dim_scores.setdefault(dim, {"prod": [], "ref": []})
            if isinstance(s.get("prod_score"), (int, float)):
                d["prod"].append(s["prod_score"])
            if isinstance(s.get("ref_score"), (int, float)):
                d["ref"].append(s["ref_score"])
        for df in (v.get("defects") or []):
            defect_cat[df.get("category", "uncategorized")] += 1
            defect_sev[df.get("severity", "unknown")] += 1
            all_defects.append({"case_id": v.get("case_id"), **df})

    dim_summary = {
        dim: {
            "prod_avg": _avg(s["prod"]), "ref_avg": _avg(s["ref"]),
            "gap": (round(_avg(s["ref"]) - _avg(s["prod"]), 3)
                    if s["prod"] and s["ref"] else None),
        }
        for dim, s in sorted(dim_scores.items())
    }
    prod_avgs = [d["prod_avg"] for d in dim_summary.values() if d["prod_avg"] is not None]
    # L2: the old LLM-only score (all dims, normalized to 0..1) — kept for comparison.
    llm_judge_score = (
        round((sum(prod_avgs) / len(prod_avgs)) / 5, 4) if prod_avgs else None
    )
    # L2-subjective: restrict to the two genuinely-subjective dimensions for the blend.
    subj = [
        dim_summary[d]["prod_avg"]
        for d in SUBJECTIVE_DIMS
        if d in dim_summary and dim_summary[d]["prod_avg"] is not None
    ]
    llm_subjective_score = round((sum(subj) / len(subj)) / 5, 4) if subj else None

    # L1: deterministic metrics (the PRIMARY signal). Falls back gracefully if the
    # references can't be located for this run.
    l1 = _safe_score_run(run_id)
    deterministic_score = l1["deterministic_score"] if l1 else None

    # benchmark_score = L1-heavy blend (0.7 L1 + 0.3 L2-subjective). If L1 is missing,
    # fall back to the LLM-only score so the report never crashes; if the LLM-subjective
    # piece is missing, L1 stands alone.
    benchmark_score = _blend(deterministic_score, llm_subjective_score, llm_judge_score)

    # Panel agreement (variance-reduction calibration signal). Mean of per-case
    # mean_stddev across multi-member cases; n_judges is the panel width seen.
    panel_agreement = {
        "mean_stddev": (
            round(sum(panel_stddevs) / len(panel_stddevs), 4) if panel_stddevs else 0.0
        ),
        "n_judges": panel_n_judges,
        "panel_cases": len(panel_stddevs),
    }

    high_defects = defect_sev.get("high", 0)
    passed = (
        benchmark_score is not None
        and benchmark_score >= GATE_MIN_SCORE
        and high_defects <= GATE_MAX_HIGH_DEFECTS
    )

    summary = {
        "run": run_id,
        "cases_judged": len(verdicts),
        "benchmark_score": benchmark_score,
        "deterministic_score": deterministic_score,
        "llm_judge_score": llm_judge_score,
        "llm_subjective_score": llm_subjective_score,
        "deterministic_metrics": (
            {k: v for k, v in l1.items() if k != "per_case"} if l1 else None
        ),
        "deterministic_per_case": (l1.get("per_case") if l1 else None),
        "gate": {"passed": passed, "min_score": GATE_MIN_SCORE,
                 "max_high_defects": GATE_MAX_HIGH_DEFECTS, "high_defects": high_defects},
        "panel_agreement": panel_agreement,
        "dimensions": dim_summary,
        "defect_categories": dict(defect_cat.most_common()),
        "defect_severities": dict(defect_sev),
        "defects": all_defects,
    }
    rdir = C.stage_dir(run_id, "report")
    C.write_json(rdir / "summary.json", summary)
    C.write_text_file(rdir / "findings.md", _findings_md(summary))

    # Promote reference extractions into the versioned golden set.
    gdir = C.stage_dir(run_id, "golden")
    promoted = 0
    if refdir.exists():
        for p in sorted(refdir.glob("*.json")):
            if p.name.startswith("_"):
                continue
            (gdir / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            promoted += 1

    return {
        "run": run_id, "cases_judged": len(verdicts), "benchmark_score": benchmark_score,
        "deterministic_score": deterministic_score, "llm_judge_score": llm_judge_score,
        "gate_passed": passed, "defects": len(all_defects), "golden_promoted": promoted,
        "report_dir": str(rdir),
    }


def _findings_md(summary: Dict[str, Any]) -> str:
    g = summary["gate"]
    lines = [
        f"# Extraction benchmark — run `{summary['run']}`\n",
        f"**Benchmark score:** {summary['benchmark_score']} "
        f"(0.7·L1 deterministic + 0.3·L2 subjective) · "
        f"**Gate:** {'PASS' if g['passed'] else 'FAIL'} "
        f"(min {g['min_score']}, high-defects {g['high_defects']}/{g['max_high_defects']}) · "
        f"**Cases judged:** {summary['cases_judged']}\n",
        f"**Deterministic (L1):** {summary.get('deterministic_score')} · "
        f"**LLM-judge (L2, all dims):** {summary.get('llm_judge_score')} · "
        f"**LLM-subjective (L2 blend input):** {summary.get('llm_subjective_score')}\n",
    ]
    pa = summary.get("panel_agreement") or {}
    if pa.get("n_judges", 1) > 1:
        lines.append(
            f"**Panel agreement (N={pa.get('n_judges')}):** mean inter-judge stddev "
            f"{pa.get('mean_stddev')} across {pa.get('panel_cases')} panel case(s) "
            f"(lower = judges agree, consensus is trustworthy).\n"
        )
    dm = summary.get("deterministic_metrics") or {}
    if dm:
        lines.append(
            f"**Value accuracy bracket (L1):** strict "
            f"{dm.get('value_accuracy')} ≤ fuzzy "
            f"{dm.get('value_accuracy_fuzzy')} ≤ label-agnostic "
            f"{dm.get('value_recall_label_agnostic')} "
            f"_(strict feeds the gate; the spread above it is a relabeling effect, "
            f"not a value regression)_\n"
        )
    lines += _l1_md(summary)
    lines += [
        "## Dimensions (prod avg vs reference avg, gap)\n",
        "| Dimension | prod | ref | gap |",
        "|---|---|---|---|",
    ]
    for dim, d in summary["dimensions"].items():
        lines.append(f"| {dim} | {d['prod_avg']} | {d['ref_avg']} | {d['gap']} |")
    lines.append("\n## Defect taxonomy\n")
    for cat, n in summary["defect_categories"].items():
        lines.append(f"- **{cat}** × {n}")
    lines.append("\n## Prioritized defects (high → low)\n")
    order = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
    for df in sorted(summary["defects"], key=lambda x: order.get(x.get("severity"), 3)):
        pc = df.get("proposed_change") or {}
        lines.append(
            f"- [{df.get('severity','?')}] `{df.get('case_id')}` — {df.get('detail','')} "
            f"→ `{pc.get('file','')}`: {pc.get('change','')}"
        )
    return "\n".join(lines) + "\n"


def _l1_md(summary: Dict[str, Any]) -> List[str]:
    """L1 deterministic per-case table — the zero-cost first layer (Evaluation Stack)."""
    per = summary.get("deterministic_per_case")
    if not per:
        return [
            "## L1 deterministic metrics\n",
            "_No reference-matched cases for this run — benchmark_score fell back to "
            "the LLM-only judge score._\n",
        ]
    out = [
        "## L1 deterministic metrics (vs Opus reference, per case)\n",
        "_value_acc = strict same-label (lower bound, primary) · fuzzy = fuzzy-label "
        "match (fair middle) · label-agnostic = value present anywhere (upper bound). "
        "A gap between strict and fuzzy/label-agnostic is a relabeling effect, not a "
        "value regression._\n",
        "| Case | value_acc | value_acc_fuzzy | value_recall_label_agnostic | "
        "sign_acc | recall | matched/ref |",
        "|---|---|---|---|---|---|---|",
    ]
    for case in sorted(per):
        m = per[case]
        out.append(
            f"| {case} | {m.get('value_accuracy')} | "
            f"{m.get('value_accuracy_fuzzy')} | "
            f"{m.get('value_recall_label_agnostic')} | {m.get('sign_accuracy')} | "
            f"{m.get('completeness_recall')} | {m.get('n_matched')}/{m.get('n_ref')} |"
        )
    out.append("")
    return out
