"""Stage 4 — reference: manage the Opus reference-generation tasks.

Deterministic: (re)builds `reference/_tasks.json` from the prompt manifests and reports
pending vs done. The **extraction-reference** subagent then reads each pending
`prompt_file` and writes its `reference_out` (`reference/<case>.json`). Re-run to see
remaining work. Generator-pluggable: an API generator could fill the same files later.

**GOV-3 — the oracle is an external AI that READS the source document.** It may only
ever be pointed at a public case. This stage refuses every case that is not provably
public in `tools/golden/corpus.yaml`: the task is written into `_tasks.json` marked
`refused`, it is kept out of `pending`/`next`, and its id is returned under `refused`
so the CLI can fail the stage. The check fails closed — an id the manifest does not
list is refused too — and it is a *manifest* check, never a filename heuristic. Ground
truth for a refused case is produced by people, not by an oracle (MEA-3).
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import config as C


def prepare(run_id: str) -> Dict[str, Any]:
    pdir = C.run_dir(run_id) / "prompts"
    refdir = C.stage_dir(run_id, "reference")
    tasks: List[Dict[str, Any]] = []
    refused_ids: List[str] = []
    refused_reasons: Dict[str, str] = {}
    for m in C.iter_json_glob(pdir, "*.manifest.json"):
        cid = m["case_id"]
        task = {**m, "done": (refdir / f"{cid}.json").exists()}
        reason = C.refusal_reason(cid)
        if reason is not None:
            task["refused"] = True
            task["refused_reason"] = reason
            refused_ids.append(cid)
            refused_reasons[cid] = reason
        tasks.append(task)
    C.write_json(refdir / "_tasks.json", tasks)
    pending = [t for t in tasks if not t["done"] and not t.get("refused")]
    return {
        "run": run_id,
        "total": len(tasks),
        "pending": len(pending),
        "refused": refused_ids,
        "refused_reasons": refused_reasons,
        "next": pending[0] if pending else None,
        "instructions": (
            "For each pending task, spawn the `extraction-reference` agent with the "
            "run-dir + prompt_file; it Reads the source doc and writes reference_out. "
            "Re-run `reference` to confirm all are done. NEVER point the oracle — or any "
            "other external AI — at a task marked `refused`: those cases are not public "
            "per tools/golden/corpus.yaml, and their ground truth is produced by people "
            "(MEA-3), not by an oracle."
        ),
    }
