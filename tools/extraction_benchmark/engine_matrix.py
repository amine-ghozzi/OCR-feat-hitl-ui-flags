"""Phase 4 — per-engine strengths/weaknesses + complementarity matrix.

Reads each `engine-<E>` run dir:
  * deterministic accuracy via metrics.score_run (candidate cases/ vs the frozen
    Opus reference) — value/sign/recall, the engine-comparison signal;
  * runtime/resources via `_engine_metrics.json` (wall, CPU, peak RSS).

The selection driver is COMPLEMENTARITY, not best-average: for any engine SET it
computes the "oracle ensemble" value-accuracy = mean over cases of the BEST
engine's value-accuracy in that set. A set whose oracle beats every single engine
has real ensemble headroom — its members cover each other's per-case failures.
(A real voting ensemble won't hit the oracle, but the oracle gap bounds the gain.)

Usage:
    python -m tools.extraction_benchmark.engine_matrix engine-rapidocr engine-easyocr engine-tesseract_cli
writes context/OCR-Engine-Benchmark-Matrix.md
"""
from __future__ import annotations

import itertools
import sys
from typing import Any, Dict, List, Optional

from . import config as C
from . import metrics


def _engine_label(run_id: str) -> str:
    return run_id[len("engine-"):] if run_id.startswith("engine-") else run_id


def load_engine(run_id: str) -> Optional[Dict[str, Any]]:
    det = metrics.score_run(run_id)
    if det is None:
        return None
    rt_path = C.run_dir(run_id) / "_engine_metrics.json"
    rt = C.read_json(rt_path) if rt_path.exists() else {}
    return {"engine": _engine_label(run_id), "run": run_id, "det": det, "rt": rt}


def _case_value_acc(det: Dict[str, Any], case: str) -> Optional[float]:
    pc = det.get("per_case", {}).get(case)
    return pc.get("value_accuracy") if pc else None


def oracle_value_accuracy(engines: List[Dict[str, Any]], cases: List[str]) -> Optional[float]:
    """Mean over cases of the best engine's value_accuracy in this set (micro-ish:
    weighted by n_ref so it is comparable to the single-engine value_accuracy)."""
    num = den = 0
    for case in cases:
        best = None
        n_ref = 0
        for e in engines:
            pc = e["det"].get("per_case", {}).get(case)
            if not pc or pc.get("value_accuracy") is None:
                continue
            n_ref = pc["n_ref"]
            v = pc["value_accuracy"]
            best = v if best is None else max(best, v)
        if best is not None and n_ref:
            num += best * n_ref
            den += n_ref
    return round(num / den, 4) if den else None


def build_matrix(run_ids: List[str]) -> str:
    loaded = [load_engine(r) for r in run_ids]
    engines = [e for e in loaded if e is not None]
    missing = [r for r, e in zip(run_ids, loaded) if e is None]
    if not engines:
        return "No engine runs could be scored (no reference-matched cases)."

    # union of cases, stable order
    cases: List[str] = sorted({
        c for e in engines for c in e["det"].get("per_case", {})
    })

    lines: List[str] = []
    lines.append("# Tier-1 OCR Engine Benchmark Matrix\n")
    lines.append("> Deterministic metrics recomputed against the frozen Opus reference "
                 "(`full-20260607`); runtime/resources from in-container runs "
                 "(`finalze-benchmark`, one host profile). LLM-judge scores omitted — "
                 "engine selection is driven by deterministic accuracy + complementarity.\n")
    if missing:
        lines.append(f"> ⚠ Unscored runs (no cases yet): {', '.join(missing)}\n")

    # --- per-engine summary -------------------------------------------------
    lines.append("## Per-engine summary\n")
    lines.append("| Engine | value_acc | fuzzy | label-agnostic | sign_acc | recall | det_score | median s/page | model-load s | peak RSS MB |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for e in engines:
        d, rt = e["det"], e["rt"].get("totals", {})
        lines.append(
            f"| **{e['engine']}** | {d['value_accuracy']} | {d['value_accuracy_fuzzy']} | "
            f"{d['value_recall_label_agnostic']} | {d['sign_accuracy']} | "
            f"{d['completeness_recall']} | {d['deterministic_score']} | "
            f"{rt.get('median_steady_wall_s')} | {rt.get('model_load_wall_s')} | "
            f"{rt.get('max_rss_peak_mb')} |"
        )
    lines.append("")

    # --- per-case value accuracy (who wins where) ---------------------------
    lines.append("## Per-case value accuracy (the complementarity signal)\n")
    header = "| case | " + " | ".join(e["engine"] for e in engines) + " | best |"
    lines.append(header)
    lines.append("|" + "---|" * (len(engines) + 2))
    for case in cases:
        row = [case.replace(".json", "")]
        vals = []
        for e in engines:
            v = _case_value_acc(e["det"], case)
            vals.append((e["engine"], v))
            row.append("—" if v is None else f"{v:.3f}")
        scored = [(eng, v) for eng, v in vals if v is not None]
        best_eng = max(scored, key=lambda kv: kv[1])[0] if scored else "—"
        row.append(f"**{best_eng}**")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # --- sign accuracy (the minus-sign failure mode, per research) ----------
    lines.append("## Per-case sign accuracy (minus-sign / negative handling)\n")
    lines.append("| case | " + " | ".join(e["engine"] for e in engines) + " |")
    lines.append("|" + "---|" * (len(engines) + 1))
    for case in cases:
        row = [case.replace(".json", "")]
        for e in engines:
            pc = e["det"].get("per_case", {}).get(case)
            sa = pc.get("sign_accuracy") if pc else None
            row.append("—" if sa is None else f"{sa:.3f}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # --- complementarity: oracle ensemble headroom --------------------------
    lines.append("## Complementarity — oracle ensemble value accuracy\n")
    lines.append("Mean (n_ref-weighted) of the BEST engine's per-case value accuracy in each set. "
                 "A set whose oracle beats its best single member has ensemble headroom.\n")
    singles = {e["engine"]: e["det"]["value_accuracy"] for e in engines}
    best_single = max(singles.values()) if singles else None
    lines.append(f"- Best single engine value accuracy: **{best_single}**\n")
    lines.append("| engine set | oracle value_acc | Δ vs best single |")
    lines.append("|---|---|---|")
    # all pairs and triples (and the full set)
    sets_to_eval: List[List[Dict[str, Any]]] = []
    for k in (2, 3):
        for combo in itertools.combinations(engines, k):
            sets_to_eval.append(list(combo))
    if len(engines) > 3:
        sets_to_eval.append(engines)
    for s in sets_to_eval:
        oracle = oracle_value_accuracy(s, cases)
        if oracle is None:
            continue
        delta = round(oracle - best_single, 4) if best_single is not None else None
        names = "+".join(e["engine"] for e in s)
        flag = " ⭐" if (delta is not None and delta > 0.01) else ""
        lines.append(f"| {names} | {oracle} | {delta:+.4f}{flag} |")
    lines.append("")
    lines.append("> ⭐ = oracle beats the best single engine by >0.01 → genuine "
                 "complementarity. Pick the smallest such set whose members are each "
                 "the unique best on ≥1 case and whose runtime/RSS fit the production budget.")
    lines.append("")

    return "\n".join(lines)


def main(run_ids: List[str]) -> None:
    md = build_matrix(run_ids)
    out = C.REPO_ROOT / "context" / "OCR-Engine-Benchmark-Matrix.md"
    C.write_text_file(out, md)
    print(f"wrote {out}")
    print(md)


if __name__ == "__main__":
    args = sys.argv[1:] or ["engine-rapidocr", "engine-easyocr", "engine-tesseract_cli"]
    main(args)
