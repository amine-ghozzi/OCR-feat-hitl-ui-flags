"""extraction_benchmark — Opus-4.8 reference benchmark for FinAlze extraction.

Pre-deployment validation harness (Part A of the quality plan). Pulls a stratified
sample of documents, runs the pipeline to get candidate extractions, reconstructs the
per-case input package, has the Opus reference agent produce the *correct* extraction,
judges candidate-vs-reference against the FinAlze rubric, and emits a findings report +
a promoted golden set.

Hybrid, mirroring `skill_benchmark`:
  - Python CLI subcommands do the deterministic work (sample / cases / prompts / report).
  - Agent-in-the-loop stages (the `extraction-reference` and `extraction-judge` Claude
    Code subagents) read/write the same run-dir artifacts.

Run stages via:  python -m tools.extraction_benchmark <stage> --run <id> [...]
"""
