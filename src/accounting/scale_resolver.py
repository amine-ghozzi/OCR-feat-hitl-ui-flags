"""E2 — Validation-driven scale resolution (identity-primary).

An ambiguous (ASSUMPTION_BASES / indigo) cell has exactly two readings:
``thousands`` (the canonical ``parse_french_number`` heuristic: single
separator + 3-digit group = thousands) and ``decimal`` (the naive read).
The per-cell heuristic cannot arbitrate — the data mixes formats and
magnitudes inside one table (see the plan's root-cause note) — but an
**arithmetic identity** can: only one reading of ``-13 473.644`` satisfies
``VNC = Brut − |Amort|`` against its confident neighbours.

Design (conservative, SOTA "never force-balance"):
  * The resolver only ever CHOOSES between a cell's own parse candidates —
    it never fabricates or nudges a value to satisfy an identity.
  * It acts only when exactly ONE candidate survives the identity within a
    relative tolerance, and the contributing cells are confident
    (non-assumption). Tie / contradiction / ambiguous neighbours → no-op.
  * Candidates within ~10% of each other carry no real scale signal → no-op
    (millimes nuance: candidates are NOT always exactly 1000× apart).
  * A MISSING cell in an otherwise-confident identity is INFERRED and
    flagged yellow (a reconstruction, never silently green).

Resolved cells get ``basis="resolved_by_<rule>"`` (rendered distinctly in
the HITL grid) and raised confidence; inferred cells get
``basis="inferred_by_<rule>"`` + yellow.

Benchmark-sensitive: this mutates ``parsed_value`` — gated by the
extraction benchmark (require ≥ 0.9038) per the plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from accounting.canonical_model import CanonicalCell, CanonicalTable
from accounting.french_number import ASSUMPTION_BASES, parse_french_number

# Relative tolerance for an identity to count as satisfied (the wrong scale
# misses by ~1000×, so 0.5% is generous for the right one and lethal for the
# wrong one) + an absolute floor of 1 monetary unit for tiny rows.
REL_TOL = Decimal("0.005")
ABS_TOL_MIN = Decimal("1")

# Two candidates closer than this ratio carry no scale signal — do not resolve.
CANDIDATE_MARGIN = Decimal("1.1")

# VAL-8 — when MOST of a document's numeric cells are written in the dot-millimes
# shape ("14 267 969.725"), the dot is that document's decimal point and the
# smaller (naive-decimal) reading of an ambiguous cell is the likelier one.
#
# "Most" is a MAJORITY, not a tuned threshold: it is the same >half rule Stage 3b
# uses to elect a document's unit, nothing is calibrated against it, and moving it
# cannot change a value — the hint only ORDERS candidates (see `scale_candidates`).
DOT_MILLIMES_MAJORITY = 0.5

# Var% identity tolerances (V1-005 is ±0.5 absolute; small relative slack for
# rounded reported percentages).
VAR_ABS_TOL = Decimal("0.5")

# A period column's cell key. Cells are keyed by ``role_name or col_name``, so a
# table whose columns were successfully BOUND presents canonical role names
# (``value_n`` / ``value_n_minus_1``), while an unbound one falls back to the raw
# header ('2024', 'N-1'). Matching only the raw forms meant a successful binding
# silently disabled this entire stage: `_period_keys` came back empty and
# `_resolve_pass` returned before doing anything. Both vocabularies must match.
_PERIOD_KEY_RE = re.compile(
    r"^(\d{4}|[Nn](-\d|_minus_\d)?|value_n(_minus_\d+)?)$"
)
_PERCENT_TOKEN_RE = re.compile(r"^[+\-]?\d{1,3}(?:[.,]\d+)?\s*%?$")

# Row-label predicates for VNC triplets (duplicated from rule_engine's E1
# detection to keep this module's import graph light).
_AMORT_ROW_PREFIXES = ("amortissement", "moinsprovision", "provision")
_NET_ROW_NORMS = ("vnc", "net", "valeurnette")


def _norm(s: str) -> str:
    out = str(s or "").lower()
    for src, dst in [("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"), ("à", "a"),
                     ("â", "a"), ("ô", "o"), ("û", "u"), ("ù", "u"), ("î", "i"),
                     ("ï", "i"), ("ç", "c")]:
        out = out.replace(src, dst)
    return re.sub(r"[^a-z0-9]", "", out)


def _is_amort_label(label: str) -> bool:
    n = _norm(label)
    return bool(n) and any(n.startswith(p) for p in _AMORT_ROW_PREFIXES)


def _is_net_label(label: str) -> bool:
    n = _norm(label)
    return bool(n) and (n in _NET_ROW_NORMS or n.startswith("vnc") or n.startswith("valeurnette"))


# Degree of confidence per resolution kind (recorded with every action, and
# written to the cell): an identity CONFIRMING the existing parse is stronger
# than flipping to the alternative reading; an INFERRED fill is a
# reconstruction and stays low until a human verifies it.
KIND_CONFIDENCE = {"confirmed": 0.95, "resolved": 0.90, "inferred": 0.50}


@dataclass
class Resolution:
    """One resolver action — feeds the HITL corrections summary (E3)."""
    table: int
    row: int
    col: str
    label: str
    rule: str                       # e.g. "vnc_row", "var_pct"
    kind: str                       # "resolved" | "confirmed" | "inferred"
    raw: str = ""
    old_parsed: Optional[str] = None
    new_parsed: Optional[str] = None
    note: str = ""
    old_basis: Optional[str] = None  # basis BEFORE resolution (for the ② view)
    confidence: float = 0.0          # degree of confidence of this action
    iteration: int = 1               # which loop pass produced it

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "row": self.row, "col": self.col,
                "label": self.label, "rule": self.rule, "kind": self.kind,
                "raw": self.raw, "old_parsed": self.old_parsed,
                "new_parsed": self.new_parsed, "note": self.note,
                "old_basis": self.old_basis, "confidence": self.confidence,
                "iteration": self.iteration}


def _naive_decimal_read(raw: str) -> Optional[Decimal]:
    """The 'separator = decimal point' reading of a single-separator token —
    the alternative candidate to the canonical thousands heuristic."""
    s = str(raw or "").strip()
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = s.replace(" ", "").replace(" ", "")
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("-"):
        neg, s = True, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if not re.fullmatch(r"\d+[.,]\d+", s):
        return None  # not a single-separator token → no alternative reading
    s = s.replace(",", ".")
    try:
        v = Decimal(s)
    except Exception:
        return None
    return -v if neg else v


def _dot_millimes_share(hint: Any) -> Optional[float]:
    """The share carried by a ``dot_millimes_hint`` (VAL-8).

    Accepts the share itself, or the whole ``metadata["dot_millimes_evidence"]``
    mapping the enrichment pipeline records — in which case the DOCUMENT share is
    preferred over the table's own: one short table is not evidence of anything,
    and the shape is a property of how the document was typed. Anything else
    (None, a string, a malformed mapping) is simply not a hint.
    """
    if isinstance(hint, bool) or hint is None:
        return None
    if isinstance(hint, (int, float)):
        return float(hint)
    if isinstance(hint, dict):
        for key in ("document_share", "share"):
            v = hint.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
    return None


def scale_candidates(raw: str, unit_hint: Optional[str] = None,
                     dot_millimes_hint: Any = None) -> list[Decimal]:
    """The cell's possible readings: canonical (thousands-leaning) + naive
    decimal. Deduped; if the two are within ~10% of each other there is no
    scale question to answer (millimes nuance) → single candidate.

    ``unit_hint`` is the unit Stage 3b bound to the table (``"DT"``/``"KDT"``/
    ``"MDT"``, see ``accounting.unit_detector``). Decision E-2 A makes it an
    **arbitration** input and nothing more: it may only order the candidates
    this function already produced. It never adds a candidate, never removes
    one, and never rescales a value — a stated unit describes the column, not
    which side of a separator is the decimal point, so letting it drop a reading
    would be exactly the silent 1000x rescale E-2 forbids.

    Concretely: under ``KDT``/``MDT`` the figures are already written down-scale,
    so the smaller-magnitude (naive-decimal) reading is listed first; under
    ``DT`` the canonical order stands. ``len()`` is invariant, so the callers —
    which act only when exactly one candidate survives (:257, :334) — are
    unaffected by the hint.

    ``dot_millimes_hint`` is VAL-8's evidence signal, and joins ``unit_hint`` on
    exactly those terms — a second arbitration input, never a second parser. It
    is the share of the document's numeric RAW cells written in the dot-millimes
    shape ("14 267 969.725"), as measured by the enrichment pipeline (the whole
    ``dot_millimes_evidence`` mapping is accepted too). A MAJORITY of them says
    the dot is this document's decimal point, so the naive-decimal reading is
    listed first — the same ordering ``KDT``/``MDT`` produces, for the
    independent reason that the typing, rather than the caption, is the witness.

    What it deliberately does NOT do: change how ``parse_french_number`` reads
    one token (the space+dot=thousands asymmetry is deliberate and pinned by
    tests), add or drop a candidate, or rescale anything. A cell resting on an
    assumption basis stays in ``ASSUMPTION_BASES`` — yellow, for a human — no
    matter how strong the evidence is.
    """
    cands: list[Decimal] = []
    for v in (parse_french_number(str(raw or "")), _naive_decimal_read(raw)):
        if v is None:
            continue
        if not any(abs(v - c) <= max(abs(c) * Decimal("0.001"), ABS_TOL_MIN) for c in cands):
            cands.append(v)
    if len(cands) == 2:
        a, b = sorted((abs(cands[0]), abs(cands[1])))
        if a > 0 and (b / a) < CANDIDATE_MARGIN:
            return cands[:1]
        share = _dot_millimes_share(dot_millimes_hint)
        if unit_hint in ("KDT", "MDT") or (share is not None and share > DOT_MILLIMES_MAJORITY):
            cands.sort(key=lambda v: abs(v))
    return cands


def _holds(lhs: Decimal, rhs: Decimal, ref: Decimal) -> bool:
    return abs(lhs - rhs) <= max(abs(ref) * REL_TOL, ABS_TOL_MIN)


def _is_ambiguous(cell: Optional[CanonicalCell]) -> bool:
    return (cell is not None and cell.parsed_value is not None
            and cell.basis in ASSUMPTION_BASES)


def _is_confident(cell: Optional[CanonicalCell]) -> bool:
    return (cell is not None and cell.parsed_value is not None
            and cell.basis not in ASSUMPTION_BASES
            and not str(cell.basis or "").startswith("inferred_by"))


def _period_keys(ct: CanonicalTable) -> list[str]:
    keys: list[str] = []
    for row in ct.rows:
        for k in row.cells:
            if k not in keys and _PERIOD_KEY_RE.match(str(k).strip()):
                keys.append(k)
    return keys


def _percent_keys(ct: CanonicalTable) -> list[str]:
    keys: list[str] = []
    for row in ct.rows:
        for k in row.cells:
            ks = str(k)
            if k not in keys and ("%" in ks or "var" in _norm(ks) or "pourcent" in _norm(ks)):
                keys.append(k)
    return keys


def _reported_percent(cell: Optional[CanonicalCell]) -> Optional[Decimal]:
    """Parse a reported % from the cell's RAW text (strict token only)."""
    if cell is None:
        return None
    s = str(cell.raw_value or "").strip()
    if not s or not _PERCENT_TOKEN_RE.match(s):
        return None
    try:
        return Decimal(s.replace("%", "").replace(",", ".").replace("+", "").strip())
    except Exception:
        return None


def _apply(cell: CanonicalCell, value: Decimal, basis: str, kind: str) -> None:
    cell.parsed_value = value
    cell.basis = basis
    if kind == "inferred":
        cell.flag = "yellow"
        cell.confidence = min(cell.confidence, 0.5)
    else:
        cell.flag = "green"
        cell.confidence = max(cell.confidence, 0.95)


# ---------------------------------------------------------------------------
# VNC (row-layout) constraint
# ---------------------------------------------------------------------------

def _resolve_vnc_triplet(ct: CanonicalTable, table_idx: int,
                         b: int, a: int, n: int, key: str) -> Optional[Resolution]:
    rows = ct.rows
    cb, ca, cn = (rows[b].cells.get(key), rows[a].cells.get(key), rows[n].cells.get(key))
    cells = {"brut": cb, "amort": ca, "net": cn}
    rowidx = {"brut": b, "amort": a, "net": n}

    amb = [name for name, c in cells.items() if _is_ambiguous(c)]
    conf = {name: c.parsed_value for name, c in cells.items() if _is_confident(c)}
    missing = [name for name, c in cells.items()
               if c is not None and c.parsed_value is None and not _is_ambiguous(c)]

    def _identity_ok(brut: Decimal, amort: Decimal, net: Decimal) -> bool:
        return _holds(net, brut - abs(amort), brut)

    # Case 1 — exactly one ambiguous cell, two confident neighbours: choose
    # the candidate (and only act if exactly one) that satisfies the identity.
    if len(amb) == 1 and len(conf) == 2:
        name = amb[0]
        cell = cells[name]
        cands = scale_candidates(cell.raw_value)
        if len(cands) < 2:
            # No alternative reading exists; if the identity CONFIRMS the
            # current parse, upgrade the assumption to a resolution.
            cands = [cell.parsed_value]
        survivors = []
        for cand in cands:
            trial = {**conf, name: cand}
            if _identity_ok(trial["brut"], trial["amort"], trial["net"]):
                survivors.append(cand)
        if len(survivors) != 1:
            return None  # tie or contradiction → leave indigo (never force)
        old = cell.parsed_value
        old_basis = cell.basis
        kind = "confirmed" if survivors[0] == old else "resolved"
        _apply(cell, survivors[0], "resolved_by_vnc_row", kind)
        return Resolution(
            table=table_idx, row=rowidx[name], col=key, label=rows[rowidx[name]].raw_text,
            rule="vnc_row", kind=kind, raw=cell.raw_value,
            old_parsed=str(old) if old is not None else None,
            new_parsed=str(survivors[0]),
            note=f"VNC identity vs {', '.join(sorted(conf))}",
            old_basis=old_basis, confidence=KIND_CONFIDENCE[kind],
        )

    # Case 2 — one MISSING cell, two confident neighbours: infer it (yellow).
    if len(missing) == 1 and len(conf) == 2 and not amb:
        name = missing[0]
        cell = cells[name]
        if name == "net":
            value = conf["brut"] - abs(conf["amort"])
        elif name == "brut":
            value = conf["net"] + abs(conf["amort"])
        else:  # amort — negative by convention
            value = -(conf["brut"] - conf["net"])
        old_basis = cell.basis
        _apply(cell, value, "inferred_by_vnc_row", "inferred")
        return Resolution(
            table=table_idx, row=rowidx[name], col=key, label=rows[rowidx[name]].raw_text,
            rule="vnc_row", kind="inferred", raw=cell.raw_value,
            old_parsed=None, new_parsed=str(value),
            note=f"inferred from {', '.join(sorted(conf))} (flagged for review)",
            old_basis=old_basis, confidence=KIND_CONFIDENCE["inferred"],
        )
    return None


# ---------------------------------------------------------------------------
# Var% constraint
# ---------------------------------------------------------------------------

def _var_identity_ok(cur: Decimal, prior: Decimal, reported: Decimal) -> bool:
    """Reported % matches (cur−prior)/prior×100 — signed, or |prior|-based
    (some preparers report against the absolute base; D2b)."""
    if prior == 0:
        return False
    signed = (cur - prior) / prior * Decimal(100)
    tol = max(VAR_ABS_TOL, abs(reported) * Decimal("0.02"))
    if abs(signed - reported) <= tol:
        return True
    absbase = (cur - prior) / abs(prior) * Decimal(100)
    return abs(absbase - reported) <= tol


def _resolve_var_pct(ct: CanonicalTable, table_idx: int, r: int,
                     cur_key: str, prior_key: str, pct_key: str) -> Optional[Resolution]:
    row = ct.rows[r]
    c_cur, c_prior = row.cells.get(cur_key), row.cells.get(prior_key)
    reported = _reported_percent(row.cells.get(pct_key))
    if reported is None or abs(reported) > 1000:
        return None

    if _is_ambiguous(c_cur) and _is_confident(c_prior):
        target, target_key, other = c_cur, cur_key, c_prior.parsed_value
        def ok(cand):
            return _var_identity_ok(cand, other, reported)
    elif _is_ambiguous(c_prior) and _is_confident(c_cur):
        target, target_key, other = c_prior, prior_key, c_cur.parsed_value
        def ok(cand):
            return _var_identity_ok(other, cand, reported)
    else:
        return None  # zero or two ambiguous cells → not solvable here

    cands = scale_candidates(target.raw_value)
    if len(cands) < 2:
        cands = [target.parsed_value]
    survivors = [c for c in cands if ok(c)]
    if len(survivors) != 1:
        return None
    old = target.parsed_value
    old_basis = target.basis
    kind = "confirmed" if survivors[0] == old else "resolved"
    _apply(target, survivors[0], "resolved_by_var_pct", kind)
    return Resolution(
        table=table_idx, row=r, col=target_key, label=row.raw_text,
        rule="var_pct", kind=kind, raw=target.raw_value,
        old_parsed=str(old) if old is not None else None,
        new_parsed=str(survivors[0]),
        note=f"Var% identity (reported {reported}%)",
        old_basis=old_basis, confidence=KIND_CONFIDENCE[kind],
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _resolve_pass(ct: CanonicalTable, table_idx: int) -> list[Resolution]:
    """ONE pass of every applicable constraint over one canonical table,
    mutating the cells it can resolve/confirm/infer. Returns the actions."""
    out: list[Resolution] = []
    period_keys = _period_keys(ct)
    if not period_keys:
        return out

    # VNC row-layout triplets (bilan-shaped tables only).
    if ct.statement_type in ("bilan", "bilan_actif", "bilan_passif", "unknown"):
        labels = [row.raw_text for row in ct.rows]
        for i in range(1, len(labels) - 1):
            if not _is_amort_label(labels[i]) or not _is_net_label(labels[i + 1]):
                continue
            prev = labels[i - 1]
            if not prev.strip() or _is_amort_label(prev) or _is_net_label(prev):
                continue
            for key in period_keys:
                res = _resolve_vnc_triplet(ct, table_idx, i - 1, i, i + 1, key)
                if res:
                    out.append(res)

    # Var% identity — current/prior assumed to be the first two period keys
    # (cells preserve column order).
    pct_keys = _percent_keys(ct)
    if pct_keys and len(period_keys) >= 2:
        cur_key, prior_key = period_keys[0], period_keys[1]
        for r in range(len(ct.rows)):
            if any(o.row == r and o.col in (cur_key, prior_key) for o in out):
                continue  # already settled by a stronger identity this pass
            res = _resolve_var_pct(ct, table_idx, r, cur_key, prior_key, pct_keys[0])
            if res:
                out.append(res)
    return out


def _remaining_ambiguous(ct: CanonicalTable) -> int:
    return sum(1 for row in ct.rows for c in row.cells.values() if _is_ambiguous(c))


def resolve_table(ct: CanonicalTable, table_idx: int = 0,
                  max_iterations: int = 5) -> list[Resolution]:
    """The validate → infer → correct FIXPOINT LOOP over one table.

    Each pass re-runs every constraint against the current cell states; a cell
    resolved in pass N becomes a CONFIDENT contributor in pass N+1, so
    corrections cascade (e.g. Var% fixes the Brut cell, which then lets VNC fix
    the Net cell). The loop stops when a pass corrects nothing — i.e. nothing
    further is inferable — or at ``max_iterations`` (a safety bound; each pass
    strictly shrinks the ambiguous/missing set, so convergence is guaranteed).

    Inferred (yellow) cells deliberately stay NON-contributors: a
    reconstruction must never be evidence for further reconstruction.

    Per-iteration records land in ``ct.metadata``:
      ``scale_resolutions``      — every action (rule, kind, confidence, iteration)
      ``resolution_iterations``  — per pass: actions taken + ambiguity remaining
    """
    all_out: list[Resolution] = []
    iterations: list[dict[str, Any]] = []
    for it in range(1, max_iterations + 1):
        out = _resolve_pass(ct, table_idx)
        if not out:
            break
        for o in out:
            o.iteration = it
        all_out.extend(out)
        iterations.append({
            "iteration": it,
            "actions": len(out),
            "kinds": {k: sum(1 for o in out if o.kind == k)
                      for k in ("confirmed", "resolved", "inferred")},
            "remaining_ambiguous": _remaining_ambiguous(ct),
        })

    if all_out:
        ct.metadata.setdefault("scale_resolutions", [])
        ct.metadata["scale_resolutions"].extend(o.to_dict() for o in all_out)
        ct.metadata["resolution_iterations"] = iterations
        ct.recompute_aggregates()
    return all_out


def resolve_canonical_tables(tables: list[CanonicalTable]) -> list[Resolution]:
    """Resolve every table; returns all actions (table index included)."""
    out: list[Resolution] = []
    for i, ct in enumerate(tables):
        out.extend(resolve_table(ct, table_idx=i))
    return out
