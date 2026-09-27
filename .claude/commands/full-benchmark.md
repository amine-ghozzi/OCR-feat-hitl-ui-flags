---
description: Run a full Opus-reference extraction benchmark on all Samples, then drive the improvement loop on the findings.
argument-hint: "[run-label] [fast]"
---

Run a FULL extraction benchmark on the documents in `Samples/`, then drive the improvement
loop on the findings. Work at the repo root; always use `venv/Scripts/python` (never system
python).

Arguments: `$ARGUMENTS`
- Use the first word as the RUN_ID if given, else RUN_ID=`full-<today's date YYYYMMDD>`.
- If the word `fast` appears, keep only the `.docx` cases after sampling (Tier-0 native text —
  no OCR model downloads); otherwise benchmark ALL supported docs.

## PHASE 1 — Benchmark (Part A)
1. Sample (no `--limit` = all), run the pipeline, build prompts:
   - `venv/Scripts/python -m tools.extraction_benchmark sample  --run RUN_ID`
   - if `fast`: edit `runs/RUN_ID/_sample.json` to keep only entries whose `source_path` ends in `.docx`.
   - `venv/Scripts/python -m tools.extraction_benchmark cases   --run RUN_ID`
   - `venv/Scripts/python -m tools.extraction_benchmark prompts --run RUN_ID`
   NOTE: image/PDF samples load OCR models on first use (slow / may download weights) — expected;
   `cases` records any per-doc failure and continues.
2. References: read `runs/RUN_ID/reference/_tasks.json`; for EACH task with `done=false`, spawn the
   `extraction-reference` subagent (Task tool; a few in parallel) with the absolute run-dir + its
   `prompt_file` + `reference_out`. Re-run `reference` until `pending==0`:
   `venv/Scripts/python -m tools.extraction_benchmark reference --run RUN_ID`
3. Judge: re-run `judge`, then for EACH ready+pending task spawn the `extraction-judge` subagent with
   the candidate + reference paths. Re-run `judge` until `ready_pending==0`:
   `venv/Scripts/python -m tools.extraction_benchmark judge --run RUN_ID`
4. Aggregate and show `runs/RUN_ID/report/findings.md`:
   `venv/Scripts/python -m tools.extraction_benchmark report --run RUN_ID`

## PHASE 2 — Improvement loop
5. Invoke the **running-improvement-loop** skill. Detect issues (signals over the candidate log) AND
   fold in the benchmark defect list from step 4. For each issue: `issue-diagnostician` →
   `remediation-planner`.
6. Apply behind the FULL regression gate (the gate must run `venv/Scripts/python -m pytest tests/ -q`
   GREEN and show no NEW qualifier mismatches before any change is kept):
   - Reversible (`config_tune` / `variation_promotion`): apply + record before/after.
   - Code fixes (most defects are `nomenclature_data.yaml` additions): use the `code-fix-applier`
     subagent — edit in an ISOLATED git worktree, run the full suite there, return the diff. Do NOT
     merge, do NOT push, do NOT run the durable `promote_variations` YAML write without confirmation.
7. Report a table: issue → diagnosis → action (applied+gated / proposed-diff / dismissed) → gate
   verdict, citing `Issue`/`RemediationAction` ids and showing each diff. Stop for review before
   merging anything.

Guardrails: nothing auto-merged or pushed; reversible changes are gated + rollback-able; if a fix
turns the suite red, keep it proposed-only and show the failing tests.
