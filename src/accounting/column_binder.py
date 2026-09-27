"""
Column Binder — declarative profiles, *proved against the numbers*.

Column-role binding has been driven by header text alone: a hardcoded keyword
dict matched with a length-penalising whole-string ratio, then positional
guesses when that failed. Header text is exactly what varies between two
statements of the same type — wording, abbreviation, OCR damage, a title row
captured as a header — so binding on it is fragile in precisely the case it
needs to be robust.

The relations *between* the columns do not vary. ``Brut − Amortissement = Net``
holds whatever the headers say; so does ``N − N-1 = Variation``. So a profile
from ``column_models`` in the nomenclature YAML is treated as a **hypothesis**,
and the arithmetic in the table decides:

    profile (prior)                data (proof)
    ─────────────────────────      ──────────────────────────────
    brut_amort_net                 col2 − col3 == col4 on 14/16 rows ✓
      "brut - amortissement           → roles PROVEN
       == net"

Where no profile proves out, the binder **abstains** rather than guessing: an
unbound column keeps its raw header and stays visible, which is recoverable,
whereas a confidently wrong role is believed absolutely by every downstream
consumer and is not.

Direction of inference — headers PROPOSE, arithmetic DISPOSES
------------------------------------------------------------
Arithmetic **refutes**; it does not select. A subtraction identity is symmetric:
``a - b == c`` and ``a - c == b`` are the same statement, so a rule that proves
``value_n − value_n_minus_1 == variation`` proves equally well with
``value_n_minus_1`` and ``variation`` exchanged. Measured on the reference
document, both bindings hold on 20 of 22 rows.

What it *does* refute is decisive — a sign flip drops to 1/22, and pointing a
role at a Notes or percentage column collapses the evidence to nothing. So the
binding must be **generated from header/geometry evidence** (a header carrying a
year is a period column; one reading "Variation" is not) and then **filtered by
the arithmetic**. Using arithmetic as the generator would pick an algebraically
equivalent but semantically wrong assignment roughly half the time.

:func:`symmetric_roles` reports which roles a rule cannot tell apart, so a
caller knows exactly where independent evidence is required.

Public API:

    verify_rule(df, role_map, rule, ...) → RuleVerdict
    symmetric_roles(rule) → list[set[str]]
    candidate_profiles(dictionary, statement_type) → list[(name, cfg)]
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# A rule holds on a row when the two sides agree within this tolerance, in
# monetary units — matching the reconciliation/ensemble numeric tolerance.
_TOLERANCE = Decimal("1")

# Evidence required before a rule is considered PROVEN: enough rows to rule out
# coincidence, and a high enough hit rate that a couple of OCR-damaged cells do
# not sink a correct binding. On the reference document the variation rule holds
# on 19 of 21 parseable rows (90%), the two misses being genuine digit errors.
_MIN_ROWS = 3
_MIN_RATIO = 0.60

_RULE_RE = re.compile(r"^\s*(?P<lhs>.+?)\s*==\s*(?P<rhs>.+?)\s*$")
_TERM_RE = re.compile(r"\s*([+-])\s*")


@dataclass
class RuleVerdict:
    """Outcome of testing one arithmetic rule against a table."""

    rule: str
    proven: bool = False
    holds: int = 0                 # rows where the identity held
    violated: int = 0              # rows where it did not
    checked: int = 0               # rows where every referenced role parsed
    offending_rows: list[int] = field(default_factory=list)
    reason: Optional[str] = None   # why it is not proven

    @property
    def ratio(self) -> float:
        return self.holds / self.checked if self.checked else 0.0


def _terms(expr: str) -> list[tuple[int, str]]:
    """Split ``a - b + c`` into ``[(+1,'a'), (-1,'b'), (+1,'c')]``."""
    parts = [p for p in _TERM_RE.split(expr.strip()) if p != ""]
    if not parts:
        return []
    out: list[tuple[int, str]] = []
    sign = 1
    # A leading sign applies to the first term; otherwise it is implicitly +.
    if parts[0] in ("+", "-"):
        sign = -1 if parts[0] == "-" else 1
        parts = parts[1:]
    expect_term = True
    for p in parts:
        if expect_term:
            out.append((sign, p.strip()))
            expect_term = False
        else:
            sign = -1 if p == "-" else 1
            expect_term = True
    return out


def rule_roles(rule: str) -> list[str]:
    """Every role name a rule references, in order of appearance."""
    m = _RULE_RE.match(rule or "")
    if not m:
        return []
    roles: list[str] = []
    for side in (m.group("lhs"), m.group("rhs")):
        for _, name in _terms(side):
            if name and name not in roles:
                roles.append(name)
    return roles


def symmetric_roles(rule: str) -> list[set[str]]:
    """Role groups a rule provably CANNOT tell apart.

    In ``a - b == c`` the identity fixes the relationship between ``b`` and
    ``c`` but not which of them is which — exchanging them yields the same
    equation. Any role appearing with a negative sign on one side is therefore
    interchangeable with any role on the other side.

    Callers must break these ties with independent evidence (a year token in the
    header, a "Variation"/"%" keyword, relative magnitude); binding them from
    the arithmetic alone is a coin flip.
    """
    m = _RULE_RE.match(rule or "")
    if not m:
        return []
    lhs, rhs = _terms(m.group("lhs")), _terms(m.group("rhs"))
    # Moving a term across `==` flips its sign, so terms occupy one of two
    # algebraic slots. Anything sharing a slot is interchangeable — by the
    # cross-equation move (a - b == c  ⟺  a - c == b) and, within a side, by
    # plain commutativity (a + b == c  ⟺  b + a == c).
    slot_a = {n for s, n in lhs if s < 0} | {n for s, n in rhs if s > 0}
    slot_b = {n for s, n in lhs if s > 0} | {n for s, n in rhs if s < 0}
    return [g for g in (slot_a, slot_b) if len(g) > 1]


def _cell_value(df: pd.DataFrame, row: int, col_idx: int) -> Optional[Decimal]:
    """Parse one cell with the canonical French-number parser."""
    try:
        from accounting.french_number import parse_french_number
        raw = df.iloc[row, col_idx]
        if pd.isna(raw):
            return None
        return parse_french_number(str(raw))
    except Exception:
        return None


def verify_rule(
    df: pd.DataFrame,
    role_map: dict[str, int],
    rule: Optional[str],
    min_rows: int = _MIN_ROWS,
    min_ratio: float = _MIN_RATIO,
    tolerance: Decimal = _TOLERANCE,
) -> RuleVerdict:
    """Test an ``a - b == c`` style rule against a candidate role binding.

    Only rows where EVERY referenced role parses to a number are counted — a
    blank or garbled cell is absence of evidence, not evidence against, and
    counting it as a violation would reject correct bindings on tables with a
    few damaged cells.

    Returns a verdict rather than a bool so a caller can report *why* a profile
    was rejected, and so rows that violate a PROVEN rule can be surfaced as
    suspect cells — a rule that holds on 19 of 21 rows has just localised the
    other two.
    """
    verdict = RuleVerdict(rule=rule or "")
    if not rule:
        verdict.reason = "no_rule"
        return verdict

    m = _RULE_RE.match(rule)
    if not m:
        verdict.reason = "unparseable_rule"
        return verdict

    lhs, rhs = _terms(m.group("lhs")), _terms(m.group("rhs"))
    needed = {n for _, n in lhs + rhs}
    missing = [n for n in needed if n not in role_map]
    if missing:
        verdict.reason = f"roles_not_bound:{','.join(sorted(missing))}"
        return verdict

    for row in range(len(df)):
        values: dict[str, Decimal] = {}
        complete = True
        for name in needed:
            col = role_map[name]
            if col >= len(df.columns):
                complete = False
                break
            v = _cell_value(df, row, col)
            if v is None:
                complete = False
                break
            values[name] = v
        if not complete:
            continue

        verdict.checked += 1
        left = sum((Decimal(sign) * values[n] for sign, n in lhs), Decimal(0))
        right = sum((Decimal(sign) * values[n] for sign, n in rhs), Decimal(0))
        if abs(left - right) <= tolerance:
            verdict.holds += 1
        else:
            verdict.violated += 1
            verdict.offending_rows.append(row)

    if verdict.checked < min_rows:
        verdict.reason = f"insufficient_rows:{verdict.checked}<{min_rows}"
        return verdict
    if verdict.ratio < min_ratio:
        verdict.reason = f"ratio_too_low:{verdict.ratio:.2f}<{min_ratio}"
        return verdict

    verdict.proven = True
    return verdict


def propose_from_headers(
    df: pd.DataFrame,
    profile: dict,
    role_map: Optional[dict[str, int]] = None,
    exclude: Optional[Iterable[int]] = None,
    cutoff: int = 80,
) -> dict[str, int]:
    """Extend a role map using a profile's YAML ``header_keywords``.

    This is the PROPOSE half of the contract — the arithmetic can only filter,
    so something has to generate candidates, and header text is the evidence
    that distinguishes roles an identity cannot (a column headed "Variation" is
    not a period column, however well it satisfies ``N − N-1 = Variation``).

    Scoring uses ``token_set_ratio``/``partial_ratio`` rather than the plain
    ``ratio`` the legacy matcher used: ``ratio`` is length-penalising, so a real
    header like "Amortissements et provisions" scores *below* cutoff against the
    keyword "Amort" purely for being longer. Existing bindings are never
    overwritten and a column is never bound twice.
    """
    from rapidfuzz import fuzz

    proposed = dict(role_map or {})
    taken = set(proposed.values()) | set(exclude or ())
    keywords: dict = profile.get("header_keywords") or {}

    for role, variants in keywords.items():
        if role in proposed:
            continue
        best_col, best_score = None, 0.0
        for col_idx, col_name in enumerate(df.columns):
            if col_idx in taken:
                continue
            header = str(col_name).strip()
            if not header or header.lower().startswith("unnamed"):
                continue
            for kw in variants or []:
                kw = str(kw).strip()
                if not kw:
                    continue
                score = max(
                    fuzz.token_set_ratio(header.lower(), kw.lower()),
                    fuzz.partial_ratio(header.lower(), kw.lower()),
                )
                if score > best_score:
                    best_col, best_score = col_idx, score
        if best_col is not None and best_score >= cutoff:
            proposed[role] = best_col
            taken.add(best_col)
    return proposed


def candidate_profiles(dictionary, statement_type: Optional[str]) -> list[tuple[str, dict]]:
    """Column profiles applicable to a statement type, most specific first.

    ``applies_to`` keys on the QUALIFIED type (``bilan_actif`` / ``bilan_passif``),
    which the classifier could not emit until the statement stage existed — so
    this config has never been reachable. A profile carrying an
    ``arithmetic_rule`` sorts first: it can be proved, and a provable hypothesis
    is worth testing before an unprovable one.
    """
    models = dict(getattr(dictionary, "column_models", None) or {})
    if not models:
        return []

    def _applies(cfg: dict) -> bool:
        scope = cfg.get("applies_to")
        if not scope:
            return True                      # unscoped profile: always a candidate
        if statement_type is None:
            return False
        if statement_type in scope:
            return True
        # A bare "bilan" should still see the side-specific profiles when the
        # side is unknown, rather than losing every candidate.
        return statement_type == "bilan" and any(
            s.startswith("bilan") for s in scope
        )

    candidates = [(name, cfg) for name, cfg in models.items() if _applies(cfg)]
    candidates.sort(key=lambda kv: (kv[1].get("arithmetic_rule") is None, kv[0]))
    return candidates
