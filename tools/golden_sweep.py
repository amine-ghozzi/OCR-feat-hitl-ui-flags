#!/usr/bin/env python
"""Golden sweep CLI — the regression gate for the extraction pipeline (MEA-1).

    python tools/golden_sweep.py snapshot --out tools/golden/baseline-<sha>.json
    python tools/golden_sweep.py compare --baseline A.json --candidate B.json \
                                         --expect tools/golden/expected/2.1.yaml
    python tools/golden_sweep.py facts --snapshot A.json

`snapshot` runs the pipeline over the manifest in `tools/golden/corpus.yaml` and writes
one STRUCTURAL record per document — counts, shapes, classes, digests; never a value, a
label or a line of text. `compare` diffs two snapshots field by field and judges each
diff against the whitelist the work package declared. `facts` asserts the corpus facts
the code review established, each recorded as `pass` or `known-fail`; a fact that flips
in either direction is the thing to look at.

Output discipline (decision G-2): for a PUBLIC document the field-level diff goes to
stdout. For a NON-PUBLIC one stdout carries counts only — `<doc_id>: diffs=N expected=M
unexpected=K` — and the detail goes to a file under --diff-dir (gitignored).

`snapshot` runs under a pinned `PYTHONHASHSEED` (it re-executes itself with `0` when the
variable is unset). Measured on this corpus: without it, two runs of identical code over
the six DOCX give the same character count and the same QCS but a different `text_sha` —
a set/dict iteration in the text post-processor rides Python's randomised string hash.
A baseline that cannot be reproduced is not a baseline.

Exit codes
    snapshot  0 ok · 1 a document failed to process · 2 the environment is not safe
    compare   0 every diff whitelisted · 1 an unexpected diff · 2 a document is missing
              from one of the two snapshots
    facts     0 every fact matches what was recorded · 1 a malformed fact · 3 a FLIP
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.extraction_benchmark import config as C  # noqa: E402
from tools.extraction_benchmark import golden as G  # noqa: E402


def _split(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _say(msg: str = "") -> None:
    print(msg, flush=True)


# --- snapshot --------------------------------------------------------------
def cmd_snapshot(args) -> int:
    if not G.variation_log_configured():
        _say(G.ENV_GUARD_MESSAGE)
        return 2

    # Restart under a pinned hash seed rather than hand back a snapshot whose `text_sha`
    # cannot be reproduced. Set PYTHONHASHSEED yourself to override. Guarded on argv[0]
    # so an in-process `main([...])` call (a test, another tool) never re-executes
    # somebody else's command line.
    if G.needs_hash_seed_reexec() and Path(sys.argv[0]).name == Path(__file__).name:
        _say(G.HASH_SEED_MESSAGE)
        child = dict(os.environ, **{G.HASH_SEED_ENV: "0"})
        return subprocess.run([sys.executable, *sys.argv], env=child).returncode

    corpus = G.load_corpus(Path(args.corpus) if args.corpus else None)
    try:
        docs = G.select_docs(corpus, _split(args.only))
    except KeyError as exc:
        _say(f"error: {exc}")
        return 2
    if not docs:
        _say("error: nothing selected")
        return 2

    missing = [d for d in docs if not (C.REPO_ROOT / d["path"]).exists()]
    if missing:
        _say("error: manifest entries with no file on disk: "
             + ", ".join(d["doc_id"] for d in missing))
        return 2

    _say(f"golden snapshot — {len(docs)} document(s)")
    try:
        snap = G.run_snapshot(docs, jobs=args.jobs, progress=_say)
    except ValueError as exc:
        _say(f"error: {exc}")
        return 2

    out = Path(args.out)
    C.write_json(out, snap)
    failed = [r["doc_id"] for r in snap["docs"].values() if not r["ok"]]
    _say(f"\nwrote {out} ({len(snap['docs'])} record(s))")
    if failed:
        _say(f"FAILED to process: {', '.join(failed)}")
        return 1
    return 0


# --- compare ---------------------------------------------------------------
def _fmt(value, limit: int = 160) -> str:
    s = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def cmd_compare(args) -> int:
    baseline = C.read_json(Path(args.baseline))
    candidate = C.read_json(Path(args.candidate))
    expected = G.ExpectedRules.load(Path(args.expect) if args.expect else None)
    try:  # best effort — a compare must still work with no manifest on hand
        unstable = [d["doc_id"] for d in G.load_corpus() if d["unstable"]]
    except Exception:  # noqa: BLE001
        unstable = []
    report = G.compare(baseline, candidate, expected, _split(args.only),
                       args.include_timing, unstable)

    diff_dir = Path(args.diff_dir) if args.diff_dir else G.DEFAULT_DIFF_DIR
    if report["change"]:
        _say(f"expected change: {report['change']}")

    for doc in report["docs"]:
        doc_id, n = doc["doc_id"], doc["n_diffs"]
        head = (f"{doc_id}: diffs={n} expected={doc['n_expected']} "
                f"unexpected={doc['n_unexpected']}" + ("  [unstable]" if doc["unstable"] else ""))
        if not doc["public"]:
            # Non-public: counts on stdout, detail to a file nobody commits.
            _say(head + ("  [non-public — detail in " + str(diff_dir / f"{doc_id}.json") + "]"
                         if n else "  [non-public]"))
            if n:
                C.write_json(diff_dir / f"{doc_id}.json", doc)
            continue
        _say(head)
        for d in doc["diffs"]:
            mark = "ok " if d["expected"] else "!! "
            rule = f"  rule={_fmt(d['rule'], 60)}" if d["rule"] is not None else ""
            _say(f"    {mark}{d['field']}: {_fmt(d['before'])} -> {_fmt(d['after'])}{rule}")

    for m in report["missing"]:
        _say(f"MISSING {m['doc_id']}: absent from the {m['side']} snapshot")

    t = report["totals"]
    _say(f"\n{t['docs']} document(s) · {t['diffs']} diffs "
         f"({t['expected']} expected, {t['unexpected']} unexpected)"
         + (f" · {t['missing']} missing" if t["missing"] else ""))
    if not args.include_timing:
        _say("(seconds/env excluded — pass --include-timing to compare them)")
    return report["exit_code"]


# --- facts -----------------------------------------------------------------
def cmd_facts(args) -> int:
    snap = C.read_json(Path(args.snapshot))
    facts = G.load_facts(Path(args.facts) if args.facts else None)
    report = G.evaluate_facts(facts, snap)

    for r in report["facts"]:
        if r["status"] == "ok":
            _say(f"  ok      {r['id']:<16} expect={r['expect']:<10} actual={r['actual']}")
        elif r["status"] == "flip":
            _say(f"  FLIP    {r['id']:<16} expect={r['expect']:<10} actual={r['actual']}"
                 f"  — {r['note']}")
        elif r["status"] == "manual":
            _say(f"  manual  {r['id']:<16} {r['note']}")
        elif r["status"] == "skipped":
            _say(f"  skipped {r['id']:<16} {r['note']}")
        else:
            _say(f"  ERROR   {r['id']:<16} {r['note']}")

    t = report["totals"]
    _say(f"\n{t['total']} fact(s): {t['ok']} as recorded · {t['flip']} flipped · "
         f"{t['skipped']} skipped · {t['manual']} manual · {t['error']} error")
    if report["flips"]:
        _say("A FLIP means the corpus changed behaviour where a fact was pinned — "
             "look at it before merging.")
    return report["exit_code"]


# --- parser ----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="golden_sweep", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("snapshot", help="run the pipeline over the corpus -> a snapshot")
    s.add_argument("--out", required=True, help="snapshot JSON to write")
    s.add_argument("--only", default="", help="comma-separated doc_ids (or file names)")
    s.add_argument("--jobs", type=int, default=1, help="reserved; only 1 is supported")
    s.add_argument("--corpus", default=None, help="manifest (default tools/golden/corpus.yaml)")
    s.set_defaults(func=cmd_snapshot)

    c = sub.add_parser("compare", help="baseline vs candidate, judged against a whitelist")
    c.add_argument("--baseline", required=True)
    c.add_argument("--candidate", required=True)
    c.add_argument("--expect", default=None, help="expected/<wp>.yaml whitelist")
    c.add_argument("--only", default="", help="comma-separated doc_ids")
    c.add_argument("--diff-dir", default=None,
                   help="where non-public detail is written (default tools/golden/diffs/)")
    c.add_argument("--include-timing", action="store_true",
                   help="also compare seconds/env/timing")
    c.set_defaults(func=cmd_compare)

    f = sub.add_parser("facts", help="assert corpus_facts.yaml against a snapshot")
    f.add_argument("--snapshot", required=True)
    f.add_argument("--facts", default=None,
                   help="fact file (default tools/golden/corpus_facts.yaml)")
    f.set_defaults(func=cmd_facts)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
