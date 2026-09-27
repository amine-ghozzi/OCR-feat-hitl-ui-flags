"""Stage 3 — prompts: per-case input package + reference task manifest.

Each package is what the **extraction-reference** agent needs to produce an INDEPENDENT
reference extraction: the source-doc reference (the agent Reads it directly — the Read
tool renders PDFs/images), the canonical-schema snippet for the statement type, and the
extraction contract. The candidate is deliberately NOT included (it would bias the
oracle). Static schema/contract first, per-case pointer last (prompt-cache discipline).
Writes `reference/_tasks.json` for the agent stage to consume.
"""
from __future__ import annotations

from typing import Any, Dict

from . import config as C

_CONTRACT = """\
# FinAlze extraction reference task

You are the **reference oracle** for Tunisian/French financial-statement extraction.
Produce the *correct* canonical extraction for the source document below, under the
canonical schema. Rules:
- Follow the canonical schema exactly; use the canonical term vocabulary provided.
- Transcribe values precisely: preserve signs (negatives in parentheses or `-`),
  French number formats (space/NBSP thousands, comma decimals), and units.
- Never invent values that are not present in the document; leave a cell empty/null if
  the source has no value.
- Map each row label to its canonical term. A bare section concept is NOT its
  `Autres …` (residual) or `Total des …` sibling — keep them distinct.
- Output ONLY a JSON object matching the reference schema (canonical tables + per-cell
  values). No prose.
"""


def _build_input(cid: str, standard: str, statement_type: str, source_path: str,
                 vocab: str, raw_text: str) -> str:
    schema_block = vocab.strip() or "(no statement-type vocabulary available; use the general canonical terms)"
    # The document CONTENT is the raw extracted text (independent of the pipeline's
    # canonical interpretation — that would bias the oracle). For image/PDF sources the
    # agent may also Read `source_path` directly (the Read tool renders them).
    doc_block = raw_text.strip() or "(no raw text captured; Read the source_path directly)"
    return (
        f"{_CONTRACT}\n"
        f"## Canonical term vocabulary ({statement_type}, {standard})\n"
        f"{schema_block}\n\n"
        f"## Source document (raw extracted content — `{source_path}`)\n"
        f"Extract the reference from this content. For image/PDF sources you may also Read\n"
        f"`{source_path}` (relative to the repo root) to verify.\n\n"
        f"```\n{doc_block}\n```\n\n"
        f"Write your reference extraction to `reference/{cid}.json`.\n"
    )


def run(run_id: str) -> Dict[str, Any]:
    C.ensure_src_on_path()
    from accounting.nomenclature import load_default_dictionary

    d = load_default_dictionary()
    sample_path = C.run_dir(run_id) / "_sample.json"
    if not sample_path.exists():
        raise FileNotFoundError(f"run {run_id}: missing _sample.json — run `sample` first")
    sample = C.read_json(sample_path)
    pdir = C.stage_dir(run_id, "prompts")
    casedir = C.run_dir(run_id) / "cases"

    tasks = []
    for row in sample:
        cid, st, std = row["case_id"], row.get("statement_type") or "unknown", row.get("standard") or "NCT"
        try:
            vocab = d.get_prompt_vocab(st) if st != "unknown" else ""
        except Exception:  # noqa: BLE001 — vocab is best-effort context
            vocab = ""
        # Raw document text (the `text` field only — NOT the candidate's canonical mapping).
        raw_text = ""
        case_file = casedir / f"{cid}.json"
        if case_file.exists():
            raw_text = (C.read_json(case_file).get("text") or "")
        C.write_text_file(pdir / f"{cid}.input.md",
                          _build_input(cid, std, st, row["source_path"], vocab, raw_text))
        manifest = {
            "case_id": cid, "standard": std, "statement_type": st,
            "source_path": row["source_path"],
            "prompt_file": f"prompts/{cid}.input.md",
            "reference_out": f"reference/{cid}.json",
        }
        C.write_json(pdir / f"{cid}.manifest.json", manifest)
        done = (C.run_dir(run_id) / "reference" / f"{cid}.json").exists()
        tasks.append({**manifest, "done": done})

    C.write_json(C.run_dir(run_id) / "reference" / "_tasks.json", tasks)
    return {
        "run": run_id,
        "prompts": len(tasks),
        "pending_reference": sum(1 for t in tasks if not t["done"]),
    }
