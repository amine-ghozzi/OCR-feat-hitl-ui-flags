"""Feature B — N-judge PANEL (variance reduction).

Average N independent judge verdicts per case to cut LLM scoring variance, and report
inter-judge agreement. This is the calibration fix for the stochastic judge: a single
judge once scored a fully-broken case as "1 defect" — an outlier verdict. With a panel of
N judges we take the MEDIAN per dimension (robust to one outlier) and a MAJORITY vote on
defects, then report the per-dimension stddev as a calibration signal (low stddev = the
judges agree, so the consensus is trustworthy).

Verdict file convention:
  - panel member : judge/<case>.j<k>.verdict.json   (k = 1..N)
  - single judge : judge/<case>.verdict.json         (backward compatible)

Pure stdlib. Tolerant of verdicts that omit a dimension (that judge simply doesn't
contribute to that dimension's median / stddev).
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
from collections import defaultdict
from statistics import median, pstdev
from typing import Any, Dict, List

# A panel-member verdict filename: <case>.j<k>.verdict.json
_PANEL_RE = re.compile(r"^(?P<case>.+)\.j(?P<k>\d+)\.verdict\.json$")

_SCORE_KEYS = ("prod_score", "ref_score")


# --- defect grouping -----------------------------------------------------------
def _norm_detail_key(detail: Any) -> str:
    """Normalized first-8-words key of a defect `detail` (used when categories vary)."""
    words = re.findall(r"\w+", str(detail or "").lower())
    return " ".join(words[:8])


def _defect_group_key(defect: Dict[str, Any], categories_vary: bool) -> tuple:
    """Group key for majority-voting defects across judges.

    Group by (category, severity) when categories are consistent; otherwise fall back to a
    normalized first-8-words key of `detail` (so phrasings of the same finding collapse)."""
    if categories_vary:
        return ("_detail", _norm_detail_key(defect.get("detail")))
    return (defect.get("category", "uncategorized"), defect.get("severity", "unknown"))


# --- panel consensus -----------------------------------------------------------
def aggregate_panel(verdicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Collapse N verdicts of the SAME case into one consensus verdict.

    - per-dimension prod_score/ref_score = MEDIAN across the judges that scored it.
    - defects: kept if present in >= ceil(N/2) judges (majority). Grouped by
      (category, severity), or by a normalized detail key when categories vary.
    - reference_suspect: union (any judge flagging the reference is worth surfacing).
    - agreement: per-dimension stddev (population), mean stddev, n_judges — the
      calibration signal.

    A single-verdict list returns that verdict's scores unchanged with mean_stddev == 0,
    so single-judge runs are unaffected.
    """
    verdicts = [v for v in verdicts if isinstance(v, dict)]
    n = len(verdicts)
    if n == 0:
        return {"dimensions": {}, "defects": [], "reference_suspect": False,
                "agreement": {"per_dimension_stddev": {}, "mean_stddev": 0.0,
                              "n_judges": 0}}

    # --- dimensions: median per score key, stddev per (dim, key) ---
    # collect raw scores: dim -> key -> [values]
    raw: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for v in verdicts:
        for dim, s in (v.get("dimensions") or {}).items():
            if not isinstance(s, dict):
                continue
            for key in _SCORE_KEYS:
                val = s.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    raw[dim][key].append(float(val))

    dimensions: Dict[str, Dict[str, Any]] = {}
    per_dim_std: Dict[str, float] = {}
    for dim, keys in raw.items():
        consensus: Dict[str, Any] = {}
        stds: List[float] = []
        for key in _SCORE_KEYS:
            vals = keys.get(key) or []
            if vals:
                consensus[key] = round(float(median(vals)), 4)
                stds.append(pstdev(vals) if len(vals) > 1 else 0.0)
        if consensus:
            dimensions[dim] = consensus
        # per-dimension stddev = mean of the score-key stddevs we could compute
        if stds:
            per_dim_std[dim] = round(sum(stds) / len(stds), 4)

    mean_stddev = (
        round(sum(per_dim_std.values()) / len(per_dim_std), 4) if per_dim_std else 0.0
    )

    # --- defects: majority vote ---
    cats = {
        df.get("category")
        for v in verdicts
        for df in (v.get("defects") or [])
        if isinstance(df, dict)
    }
    # If all defects share a category (or there are none), group by (category, severity).
    # Only fall back to the normalized detail key when judges disagree on category.
    categories_vary = len({c for c in cats if c is not None}) > 1

    threshold = math.ceil(n / 2)
    grouped: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for v in verdicts:
        # dedupe within a single judge so one judge can't satisfy the majority alone
        seen_in_judge = set()
        for df in (v.get("defects") or []):
            if not isinstance(df, dict):
                continue
            gk = _defect_group_key(df, categories_vary)
            if gk in seen_in_judge:
                continue
            seen_in_judge.add(gk)
            grouped[gk].append(df)

    defects: List[Dict[str, Any]] = []
    for gk, occurrences in grouped.items():
        if len(occurrences) >= threshold:
            # surface the first occurrence as the representative defect, annotated
            # with the panel support count.
            rep = dict(occurrences[0])
            rep["panel_support"] = len(occurrences)
            defects.append(rep)

    reference_suspect = any(
        bool(v.get("reference_suspect")) for v in verdicts
    )

    return {
        "dimensions": dimensions,
        "defects": defects,
        "reference_suspect": reference_suspect,
        "agreement": {
            "per_dimension_stddev": per_dim_std,
            "mean_stddev": mean_stddev,
            "n_judges": n,
        },
    }


# --- verdict loading -----------------------------------------------------------
def load_case_verdicts(judge_dir: Any, case_id: str) -> List[Dict[str, Any]]:
    """Return all panel-member verdicts for `case_id` if present, else the single
    verdict as a 1-element list (backward compatible).

    `judge_dir` may be a str or pathlib.Path. Returns [] if nothing is found.
    """
    jdir = str(judge_dir)
    members = sorted(
        glob.glob(os.path.join(jdir, f"{case_id}.j*.verdict.json")),
        key=_panel_sort_key,
    )
    if members:
        out = []
        for p in members:
            try:
                with open(p, encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except Exception:
                continue
        return out
    single = os.path.join(jdir, f"{case_id}.verdict.json")
    if os.path.exists(single):
        try:
            with open(single, encoding="utf-8") as fh:
                return [json.load(fh)]
        except Exception:
            return []
    return []


def _panel_sort_key(path: str):
    m = _PANEL_RE.match(os.path.basename(path))
    return int(m.group("k")) if m else 0


def is_panel_member(filename: str) -> bool:
    """True for a `<case>.j<k>.verdict.json` panel-member filename."""
    return bool(_PANEL_RE.match(os.path.basename(filename)))


def case_id_of(filename: str) -> str:
    """Extract the case id from a verdict filename (panel member or single)."""
    base = os.path.basename(filename)
    m = _PANEL_RE.match(base)
    if m:
        return m.group("case")
    if base.endswith(".verdict.json"):
        return base[: -len(".verdict.json")]
    return base