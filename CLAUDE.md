# FinAlze — session rules

## Non-public documents

Five documents under `Samples/` are client financial statements and are NOT public:

- `EF CLB 2024 Provisoire (1).pdf`
- `EF LA VIANDE BLANCHE 2024.pdf`
- `EF PROV. COHAPROM 2024 VCAD.pdf`
- `Rapport CAC 2024 Aquaculture.pdf`
- `Rapport_CAC_2024_signé_(1)[1].pdf`

For these, any session may report counts, page numbers and structural facts. Never
report values, labels or text. Never paste their content into a prompt, a report, a
commit message or a test. Never point an external-AI stage at them — the
`extraction-reference` / `extraction-judge` agents, NotebookLM, any API. Field-level
diffs for them go to `tools/golden/diffs/` (gitignored), never to stdout.

`tools/extraction_benchmark/runs/` (gitignored) holds case JSON and prompt packages that
carry the raw extracted text of non-public documents: they must never leave the machine
or be handed to an external AI, and the `reference` and `judge` stages refuse them.

`tools/golden/corpus.yaml` is the authority on public vs non-public — the manifest, not
the file name. `tools/extraction_benchmark/config.is_public()` reads it and fails
closed; the benchmark's `reference` and `judge` stages refuse every case it does not pass.

## Python

Always `venv/Scripts/python` at the repo root. Worktrees have no venv of their own — use
the repo-root absolute path from a worktree. Never bare `python`, never `.venv/`.

## Before any extraction run

Set `FINALZE_VARIATION_LOG` to a scratch path outside the repo before anything that
touches the pipeline — pytest that imports it, `tools/golden_sweep.py`, a benchmark
stage, the UI. The learning log is per-tenant and must never be a tracked file.

## Golden gate

`tools/golden_sweep.py snapshot | compare | facts` is the regression gate. Every merge
that changes extraction declares its expected diffs in `tools/golden/expected/<wp>.yaml`;
a diff that is not declared fails the compare. Snapshots run under a pinned
`PYTHONHASHSEED` — the tool sets it, do not work around it.

## Never write

`output/temp/` (tracked) or `data/`. Never commit extraction JSON, run dirs or snapshots.

## Document authority

Owner documents in `context/` are current as of their stated date. Team-authored
documents in `context/` are suggestions unless the owner confirmed them. Where a
document conflicts with what the code's docstrings, comments or tests intend, the code's
intent is presumed correct: implement the code's intent and report the conflict — do not
resolve it silently.

## Roadmap

- `context/Master-Plan.md` — the register and the phases.
- `context/Decision-Form.md` — decisions.
- `context/Project-Backlog.md` — backlog.
