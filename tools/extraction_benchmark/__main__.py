"""CLI entry for the extraction_benchmark harness.

    python -m tools.extraction_benchmark <stage> --run <id> [stage args]

Deterministic stages (sample/cases/prompts/report) run to completion. Agent stages
(reference/judge) print the task manifest the Claude Code subagents then fill; the
deterministic ingest of their outputs happens in the same stage on the next call.
"""
from __future__ import annotations

import argparse
import json

# GOV-3: the stages that hand a case to an external AI. `reference` points the oracle at
# the source document; `judge` points the judge at `cases/<case>.json`, which carries the
# document's raw extracted text. Both refuse any case the corpus manifest does not prove
# public, and both must fail the CLI loudly rather than let a caller call the run complete.
_EXTERNAL_AI_STAGES = {
    "reference": "extraction-reference",
    "judge": "extraction-judge",
}


def _print_refusals(stage: str, result: dict) -> bool:
    """Print the refused case ids + reasons. True when the stage must exit non-zero."""
    refused = result.get("refused") or []
    if not refused:
        return False
    reasons = result.get("refused_reasons") or {}
    print(f"\nREFUSED — do NOT point the `{_EXTERNAL_AI_STAGES[stage]}` agent at these cases:")
    for cid in refused:
        print(f"  {cid}: {reasons.get(cid, 'non-public (GOV-3)')}")
    print("Refused cases are handled by people (MEA-3), never by an external AI.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="extraction_benchmark", description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)

    p_sample = sub.add_parser("sample", help="stratified document sample -> _sample.json")
    p_sample.add_argument("--run", required=True)
    p_sample.add_argument("--limit", type=int, default=12, help="max cases (bounded run)")
    p_sample.add_argument("--source", choices=["local", "db"], default="local")

    p_cases = sub.add_parser("cases", help="run the pipeline -> cases/<case>.json")
    p_cases.add_argument("--run", required=True)

    p_prompts = sub.add_parser("prompts", help="build per-case input packages + manifest")
    p_prompts.add_argument("--run", required=True)

    p_ref = sub.add_parser("reference", help="emit/ingest the Opus reference tasks")
    p_ref.add_argument("--run", required=True)

    p_judge = sub.add_parser("judge", help="emit/ingest the Opus judge tasks")
    p_judge.add_argument("--run", required=True)
    p_judge.add_argument(
        "--panel", type=int, default=1,
        help="N independent judges per case (variance reduction); N>1 emits "
             "<case>.j<k>.verdict.json slots. Default 1 = single judge.",
    )

    p_report = sub.add_parser("report", help="aggregate verdicts -> report/ + golden/")
    p_report.add_argument("--run", required=True)

    args = parser.parse_args(argv)

    # Lazy import per stage so an unbuilt/optional stage doesn't break `--help`.
    if args.stage == "sample":
        from . import sample
        result = sample.run(args.run, limit=args.limit, source=args.source)
    elif args.stage == "cases":
        from . import cases
        result = cases.run(args.run)
    elif args.stage == "prompts":
        from . import prompts
        result = prompts.run(args.run)
    elif args.stage == "reference":
        from . import reference
        result = reference.prepare(args.run)
    elif args.stage == "judge":
        from . import judge
        result = judge.prepare(args.run, panel=args.panel)
    elif args.stage == "report":
        from . import report
        result = report.run(args.run)
    else:  # pragma: no cover - argparse enforces choices
        parser.error(f"unknown stage {args.stage!r}")
        return 2

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))

    if (args.stage in _EXTERNAL_AI_STAGES and isinstance(result, dict)
            and _print_refusals(args.stage, result)):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
