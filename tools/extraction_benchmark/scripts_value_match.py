"""Deterministic value-accuracy metric for the extraction benchmark.

Counts how many (row-label, value) pairs from the Opus reference are reproduced
in a candidate run — a NOISE-FREE complement to the stochastic LLM judge. Match
by truncated label + value normalized to 3 decimals.

Usage: scripts_value_match.py <run_id> [<run_id_2> ...]   (references reused from full-20260607)
"""
from __future__ import annotations
import json, os, sys, glob

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE, "runs")
REF_RUN = "full-20260607"


def _norm(x):
    try:
        return round(float(str(x).replace(",", "").replace(" ", "")), 3)
    except Exception:
        return None


def _ref_vals(case):
    p = os.path.join(RUNS, REF_RUN, "reference", case)
    if not os.path.exists(p):
        return None
    d = json.load(open(p, encoding="utf-8"))
    m = {}
    for ct in d.get("tables") or d.get("canonical_tables") or []:
        for r in (ct.get("rows") if isinstance(ct, dict) else []):
            lbl = (r.get("raw_text") or "")[:18]
            for _k, c in (r.get("cells") or {}).items():
                v = c.get("value", c.get("parsed_value"))
                if v is not None:
                    m.setdefault(lbl, set()).add(_norm(v))
    return m


def _cand_vals(run, case):
    d = json.load(open(os.path.join(RUNS, run, "cases", case), encoding="utf-8"))
    m = {}
    for ct in d.get("canonical_tables", []):
        if not isinstance(ct, dict):   # DLV-15: index-alignment placeholder, no rows
            continue
        for r in ct.get("rows", []):
            lbl = (r.get("raw_text") or "")[:18]
            for _k, c in (r.get("cells") or {}).items():
                v = c.get("parsed_value")
                if v is not None:
                    m.setdefault(lbl, set()).add(_norm(v))
    return m


def score(run):
    cases = [os.path.basename(p) for p in glob.glob(os.path.join(RUNS, run, "cases", "*.json"))]
    hit = tot = 0
    per = {}
    for case in sorted(cases):
        ref = _ref_vals(case)
        if not ref:
            continue
        cand = _cand_vals(run, case)
        h = t = 0
        for lbl, vals in ref.items():
            for v in vals:
                t += 1
                if v in cand.get(lbl, set()):
                    h += 1
        per[case] = (h, t)
        hit += h
        tot += t
    return hit, tot, per


if __name__ == "__main__":
    for run in sys.argv[1:] or [REF_RUN]:
        h, t, per = score(run)
        print(f"=== {run}: {h}/{t} reference values matched ===")
        for case, (ch, ct) in per.items():
            print(f"  {case:34} {ch:>4}/{ct}")
