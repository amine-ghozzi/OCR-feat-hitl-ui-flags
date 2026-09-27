"""Stage 5 — judge: manage the Opus judge tasks (candidate vs reference).

Deterministic: builds `judge/_tasks.json` pairing each `cases/<case>.json` with its
`reference/<case>.json`, reports which are ready (both present) and which still need a
verdict. The **extraction-judge** subagent reads each ready pair and writes
`judge/<case>.verdict.json`.

**GOV-3 — the judge is an external AI and `cases/<case>.json` carries the document's raw
extracted text.** Same gate as the `reference` stage, applied independently: a case that
is not provably public in `tools/golden/corpus.yaml` is written into `_tasks.json` marked
`refused`, is never `ready`, never becomes `next`, and its id is returned under `refused`
so the CLI can fail the stage. Independent on purpose — the `reference` refusal already
starves a non-public case of its reference file, but a hand-placed file would defeat
that, and this gate would not care. Fails closed; a manifest check, never a filename
heuristic. Refused cases are reviewed by people (MEA-3), never by a judge agent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from . import config as C


def prepare(run_id: str, panel: int = 1) -> Dict[str, Any]:
    """Build the judge task manifest.

    With `panel == 1` (default) each case gets one task writing `judge/<case>.verdict.json`
    — unchanged behavior. With `panel > 1` each case gets N task slots writing
    `judge/<case>.j<k>.verdict.json` (k = 1..N); the N independent spawns are done by the
    orchestrator, not here. `report.py` collapses the N members to a consensus verdict.

    A case the corpus manifest does not prove public is refused (GOV-3): every one of its
    slots is marked `refused` and can never be `ready`, whatever files exist on disk.
    """
    panel = max(1, int(panel))
    casedir = C.run_dir(run_id) / "cases"
    refdir = C.run_dir(run_id) / "reference"
    jdir = C.stage_dir(run_id, "judge")
    tasks = []
    refused_ids: List[str] = []
    refused_reasons: Dict[str, str] = {}
    for m in C.iter_json_glob(C.run_dir(run_id) / "prompts", "*.manifest.json"):
        cid = m["case_id"]
        reason = C.refusal_reason(cid)
        if reason is not None and cid not in refused_reasons:
            refused_ids.append(cid)
            refused_reasons[cid] = reason
        # A refused case is never ready — the candidate carries the document's raw text,
        # so a hand-placed reference file must not be enough to unblock the judge.
        ready = reason is None and (
            (casedir / f"{cid}.json").exists() and (refdir / f"{cid}.json").exists()
        )
        if panel == 1:
            slots = [(f"judge/{cid}.verdict.json", None)]
        else:
            slots = [(f"judge/{cid}.j{k}.verdict.json", k) for k in range(1, panel + 1)]
        for verdict_out, k in slots:
            task = {
                "case_id": cid,
                "candidate": f"cases/{cid}.json",
                "reference": f"reference/{cid}.json",
                "verdict_out": verdict_out,
                "ready": ready,
                "done": (jdir / Path(verdict_out).name).exists(),
            }
            if reason is not None:
                task["refused"] = True
                task["refused_reason"] = reason
            if k is not None:
                task["panel_member"] = k
            tasks.append(task)
    C.write_json(jdir / "_tasks.json", tasks)
    ready_pending = [t for t in tasks if t["ready"] and not t["done"]]
    return {
        "run": run_id,
        "panel": panel,
        "total": len(tasks),
        "ready_pending": len(ready_pending),
        # `blocked` means "waiting on an artifact"; a refused case is not waiting on one.
        "blocked": sorted({t["case_id"] for t in tasks
                           if not t["ready"] and not t.get("refused")}),
        "refused": refused_ids,
        "refused_reasons": refused_reasons,
        "next": ready_pending[0] if ready_pending else None,
        "instructions": (
            "For each ready+pending task, spawn the `extraction-judge` agent with the "
            "run-dir + candidate + reference; it writes verdict_out. Blocked cases need "
            "their cases/ or reference/ produced first. NEVER point the judge — or any "
            "other external AI — at a task marked `refused`: those cases are not public "
            "per tools/golden/corpus.yaml and the candidate carries the document's raw "
            "text; they are reviewed by people (MEA-3)."
            + (
                f" Panel mode: {panel} independent judges per case "
                f"(<case>.j<k>.verdict.json); report.py takes the per-dimension median + "
                f"defect majority vote." if panel > 1 else ""
            )
        ),
    }
