"""Detect quality Issues on the current build (dev-time, reference-free).

Primary source: `accounting.candidate_audit` over the variation-candidate log — the
residual/total **qualifier mismatches** (the false-positive class) and **duplication**.
Each issue carries a stable `fingerprint` so repeated sweeps don't create duplicate rows.
(Production/live drift signals are deferred to the post-deployment work.)
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from accounting import candidate_audit
from accounting.variation_logger import resolve_log_path


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def detect_issues(candidate_log: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return detected issues as plain dicts (kind, severity, title, evidence, fingerprint)."""
    path = candidate_log or resolve_log_path()
    records = candidate_audit.read_jsonl(path)
    audit = candidate_audit.audit_records(records)
    issues: List[Dict[str, Any]] = []

    for m in candidate_audit.qualifier_mismatches(records):
        issues.append({
            "kind": "qualifier_mismatch",
            "severity": "high",
            "title": f"Residual/total false match: {m['raw_text']!r} -> {m['matched_to']!r}",
            "evidence": m,
            "fingerprint": _fingerprint("qualifier_mismatch", m["raw_text"], m["matched_to"]),
        })

    if audit["total"] and audit["duplication_ratio"] > 0.5:
        issues.append({
            "kind": "candidate_duplication",
            "severity": "medium",
            "title": f"Candidate-log duplication ratio {audit['duplication_ratio']} "
                     f"({audit['unique_pairs']}/{audit['total']} unique)",
            "evidence": audit,
            "fingerprint": _fingerprint("candidate_duplication"),
        })

    return issues
