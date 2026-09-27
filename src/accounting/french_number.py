"""
French-locale number parser.

Implements the 3-layer parser specified in §22 of the nomenclature revamp plan:

    Layer 1 — OCR error correction (character substitutions, numeric-context only)
    Layer 2 — Locale normalization (parens → neg, dashes → 0, NBSP removal, …)
    Layer 3 — Babel ``parse_decimal`` with a plain-digit fallback

Public surface:

    parse_french_number(raw) -> Decimal | None
    is_numeric_token(text)   -> bool
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from babel.numbers import parse_decimal
from babel.core import UnknownLocaleError

# ---------------------------------------------------------------------------
# Layer 1 — OCR error correction
# ---------------------------------------------------------------------------

# Substitutions applied only when the token is in numeric context.
# (Applying them blindly would corrupt real labels, e.g. "Obligations".)
_OCR_DIGIT_SUBS = {
    "O": "0",
    "o": "0",
    "l": "1",  # lowercase L → one (context-gated)
    "I": "1",  # uppercase I → one (context-gated)
    "S": "5",  # rare OCR miss
    "B": "8",
}

# A token is "numeric context" when it is mostly digits + allowed separators
# plus at most a handful of OCR-confusable letters.
_NUMERIC_CONTEXT_RE = re.compile(r"^[\d.,\s()<>OolISB\xa0 ‑–—\-+]+$")
_HAS_DEFINITE_DIGIT_RE = re.compile(r"[2-9]")


def _apply_ocr_fixes(text: str) -> str:
    """Layer 1: swap OCR-confusable glyphs for digits, only in numeric context."""
    if not _NUMERIC_CONTEXT_RE.match(text):
        return text
    # Heuristic gate: require at least one definite digit (2-9) before swapping,
    # OR the whole token must be only confusable chars + separators. This avoids
    # turning "OOO" (unlikely but safe) into "000" without evidence.
    if not _HAS_DEFINITE_DIGIT_RE.search(text):
        # No definite digit: still allow substitution if the token length >= 2
        # and at least one separator is present (e.g. "l.500" → "1.500").
        if not re.search(r"[.,\s]", text):
            return text
    return "".join(_OCR_DIGIT_SUBS.get(ch, ch) for ch in text)


# ---------------------------------------------------------------------------
# Layer 2 — Locale normalization
# ---------------------------------------------------------------------------

# Parenthesised negatives, e.g. "(1.500,00)". Angle brackets "<...>" are the
# same accounting convention in some NCT/French statement exports, e.g. "<950>".
_PAREN_RE = re.compile(r"^\((.+)\)$")
_ANGLE_RE = re.compile(r"^<(.+)>$")
_STANDALONE_DASH_RE = re.compile(r"^[\-–—‑]+$")
# Trailing minus (e.g. "060201-") → leading sign. Only a lone trailing '-' on an
# otherwise plain integer/grouped number; we do NOT touch internal dashes.
_TRAILING_MINUS_RE = re.compile(r"^([\d.,\s]+)-$")
_NBSP_CHARS = ("\xa0", " ", " ")  # NBSP, narrow NBSP, thin space
_CURRENCY_TRAILERS_RE = re.compile(
    r"\s*(DT|TND|EUR|USD|€|\$|DH|MAD)\s*$", flags=re.IGNORECASE
)
_CURRENCY_LEADERS_RE = re.compile(
    r"^\s*(DT|TND|EUR|USD|€|\$|DH|MAD)\s+", flags=re.IGNORECASE
)


def _normalize_locale(text: str) -> str:
    """Layer 2: parens → negative, dashes → 0, NBSP + currency stripping."""
    s = text.strip()
    if not s:
        return s

    # Standalone dash (possibly multiple) → zero (NCT convention for empty cells)
    if _STANDALONE_DASH_RE.match(s):
        return "0"

    # Parentheses / angle brackets → negative sign (accounting convention)
    paren_match = _PAREN_RE.match(s) or _ANGLE_RE.match(s)
    if paren_match:
        s = "-" + paren_match.group(1).strip()

    # Trailing minus → leading sign (e.g. "060201-" → "-060201"). Applied only
    # when the rest is a plain number; do NOT special-case split-digit artifacts
    # such as "-52955 0" (upstream tokenization issue, left/flagged elsewhere).
    if not s.startswith("-"):
        tm = _TRAILING_MINUS_RE.match(s)
        if tm:
            s = "-" + tm.group(1).strip()

    # Remove non-breaking spaces and thin spaces (French thousands separators)
    for ch in _NBSP_CHARS:
        s = s.replace(ch, "")

    # Strip currency markers
    s = _CURRENCY_LEADERS_RE.sub("", s)
    s = _CURRENCY_TRAILERS_RE.sub("", s)

    return s.strip()


# ---------------------------------------------------------------------------
# Layer 3 — Locale-aware parsing
# ---------------------------------------------------------------------------


# A single separator ('.' or ',') followed by exactly a 3-digit group (or a
# repeated 3-digit grouping) and nothing fractional is a THOUSANDS separator in
# these integer-amount NCT/French statements — NOT a decimal point. Babel would
# otherwise read "10 070.594" as 10070.594. We resolve this ambiguity up front.
#
# Disambiguation by trailing-group digit count:
#   sep + 1-2 digits          → decimal   (e.g. "1,5", "1.5")  → left untouched
#   sep + one/more 3-digit grp → thousands (e.g. "210,580", "10 070.594")
#   sep + >3 digits (single)   → thousands here (e.g. "-1.603448"); see note below
_SINGLE_SEP_THOUSANDS_RE = re.compile(
    r"^(?P<sign>-?)(?P<int>\d{1,3}(?:[ ]?\d{3})*)(?P<sep>[.,])(?P<grp>\d{3}(?:[ ]?\d{3})*)$"
)
# Conservative >3-digit single-group branch: a single separator followed by a
# run of >3 digits with no further structure (e.g. "1.603448"). For these
# integer-only financial statements we strip it as thousands. Genuine high-
# precision decimals do not occur in this corpus; if that assumption changes,
# this is the branch to gate. Flagged as the main residual risk.
_SINGLE_SEP_LONG_RUN_RE = re.compile(r"^(?P<sign>-?)(?P<int>\d+)(?P<sep>[.,])(?P<grp>\d{4,})$")
# Space-grouped thousands followed by a single COMMA: the space already groups
# thousands, so the comma must be the DECIMAL separator (Tunisian millimes).
# e.g. "11202 126,240" -> 11202126.240, "37 158,492" -> 37158.492. Requires at
# least one " \d{3}" group so "210,580" (no space) stays thousands per ISSUE-1,
# and a comma so "10 070.594" (space + dot) stays thousands per ISSUE-1.
_SPACE_GROUP_COMMA_DECIMAL_RE = re.compile(
    r"^(?P<sign>-?)(?P<int>\d+(?:[ ]\d{3})+)(?P<sep>,)(?P<dec>\d+)$"
)
# Two or more commas grouping 3-digit runs = thousands ("34,340,000" -> 34340000).
# Unambiguous: a number cannot have two decimal points. Babel/_fallback drop these
# to None, losing every million-scale value (the EF_CLB failure mode).
_MULTI_COMMA_THOUSANDS_RE = re.compile(r"^(?P<sign>-?)(?P<int>\d{1,3}(?:,\d{3}){2,})$")


def infer_comma_convention(tokens) -> str:
    """Infer a *document's* comma convention from unambiguous signals across all
    its numeric tokens — resolving the token-level ambiguity that A1 cannot
    (``210,580`` = thousands vs ``3811603,553`` = decimal look identical alone).

    Returns ``"comma_decimal"``, ``"comma_thousands"``, or ``"unknown"``. A single
    unambiguous decimal signal (a space-grouped or dot-grouped thousands part
    alongside a comma, or a comma with only 1-2 trailing digits) proves the comma
    is the decimal separator for the whole document; multiple commas in one token
    proves thousands. Validated correct on all 8 benchmark documents (incl. the
    safety case: EF_CLB / bilab2 are NOT misread as decimal).
    """
    decimal_sig = thousands_sig = 0
    for t in tokens:
        if not isinstance(t, str):
            continue
        s = t.strip()
        if re.search(r"\d \d{3}.*,\d", s):        # space-grouped thousands + comma
            decimal_sig += 1
        if re.search(r"\.\d{3},\d", s):           # dot-grouped thousands + comma
            decimal_sig += 1
        if re.search(r",\d{1,2}(?!\d)", s) and not re.search(r",\d{3}", s):  # comma + 1-2 digits
            decimal_sig += 1
        if re.search(r"\d,\d{3},\d{3}", s):       # multiple commas → thousands
            thousands_sig += 1
    if decimal_sig > thousands_sig and decimal_sig > 0:
        return "comma_decimal"
    if thousands_sig > 0:
        return "comma_thousands"
    return "unknown"


def _disambiguate_single_separator(
    s: str, comma_decimal: Optional[bool] = None
) -> tuple[str, Optional[str]]:
    """Layer 2.5: resolve single '.'/',' that is really a thousands separator.

    Operates on the *space-bearing* normalized string (regular spaces still act
    as thousands groupers here). Returns ``(result, basis)`` — a plain-digit
    integer string (sign preserved) when the token is unambiguously
    thousands-grouped, otherwise ``s`` unchanged so the decimal path
    (Babel/fallback) handles it. ``basis`` names the branch that fired (or
    ``None`` if the string was left untouched).

    ``comma_decimal`` is the per-document hint from ``infer_comma_convention``:
    ``True`` → a single comma is the document's decimal separator (do not strip it
    as a thousands group); ``False``/``None`` → ISSUE-1 token heuristic (unchanged,
    backward-compatible default).
    """
    # Skip if both separators present — that is the mixed thousands+decimal case
    # ("1.500,00") already handled correctly downstream.
    if "." in s and "," in s:
        return s, None

    # Multiple commas with no dot = thousands grouping — a number has at most one
    # decimal point, so "34,340,000" is unambiguously 34340000 (NOT decimal),
    # regardless of the document convention. Babel/_fallback otherwise drop these
    # million-scale values to None (the EF_CLB failure). Strip the commas.
    m = _MULTI_COMMA_THOUSANDS_RE.match(s)
    if m:
        return m.group("sign") + m.group("int").replace(",", ""), "multi_comma"

    # Space-grouped thousands + comma → comma is the DECIMAL separator. Strip the
    # space groupers and keep the comma so Babel (fr_FR) reads it as the decimal.
    m = _SPACE_GROUP_COMMA_DECIMAL_RE.match(s)
    if m:
        return (
            m.group("sign") + m.group("int").replace(" ", "") + "," + m.group("dec"),
            "space_comma_decimal",
        )

    # Per-document inference: in a comma-decimal document a single comma is the
    # decimal point — do NOT strip it as a thousands group. Leave it for Babel
    # (fr_FR reads comma as decimal). The inference is only *material* (and so
    # tagged) when the default would otherwise have stripped it as thousands;
    # "1,5" parses to a decimal either way, so it stays untagged ("plain").
    if comma_decimal is True and "," in s and "." not in s:
        material = bool(
            _SINGLE_SEP_THOUSANDS_RE.match(s) or _SINGLE_SEP_LONG_RUN_RE.match(s)
        )
        return s, ("comma_decimal_hint" if material else None)

    # NOTE: a "comma-decimal doc → a lone dot is also decimal" rule was tried here
    # and REVERTED — it is genuinely ambiguous. Within the same comma-decimal
    # document some dots are a comma OCR'd as '.' (decimal, e.g. "-1 098 948.081")
    # while others are real thousands groupers ("1.603448"), with no per-token
    # signal to separate them; the references themselves are inconsistent, so a
    # blanket rule regressed more cells than it fixed (net -10 on the corpus).
    # This is the one tail defect that needs document context the data doesn't give.

    m = _SINGLE_SEP_THOUSANDS_RE.match(s)
    if m:
        digits = (m.group("int") + m.group("grp")).replace(" ", "")
        return m.group("sign") + digits, "single_sep_thousands"

    m = _SINGLE_SEP_LONG_RUN_RE.match(s)
    if m:
        digits = (m.group("int") + m.group("grp")).replace(" ", "")
        return m.group("sign") + digits, "single_sep_long_run"

    return s, None


def _fallback_parse(cleaned: str) -> Optional[Decimal]:
    """Plain fallback: strip spaces/dots (thousands), swap comma → dot."""
    stripped = cleaned.replace(" ", "")
    # If it has BOTH "." and ",", treat "." as thousands and "," as decimal
    if "." in stripped and "," in stripped:
        stripped = stripped.replace(".", "").replace(",", ".")
    elif "," in stripped:
        # Only comma → decimal separator in French
        stripped = stripped.replace(",", ".")
    else:
        # Only "." might be decimal OR thousands. If the last dot-group is 3
        # digits and the rest looks like a thousands pattern, treat all dots
        # as thousands. Otherwise treat as decimal.
        if re.match(r"^-?\d{1,3}(\.\d{3})+$", stripped):
            stripped = stripped.replace(".", "")
    try:
        return Decimal(stripped)
    except Exception:  # noqa: BLE001 — Decimal raises InvalidOperation
        return None


# Parse-basis tags whose value rests on a corpus assumption / per-document
# inference (NOT a deterministic read) — surfaced in the HITL UI so a reviewer
# knows to double-check the value's scale. See BENCHMARK_REPORT.md §3.4.
ASSUMPTION_BASES: frozenset[str] = frozenset(
    {"single_sep_thousands", "single_sep_long_run", "comma_decimal_hint"}
)


def _parse_core(
    raw: object, comma_decimal: Optional[bool] = None
) -> tuple[Optional[Decimal], Optional[str]]:
    """Shared parse implementation returning ``(value, basis)``.

    ``basis`` names how the value was derived (e.g. ``plain``,
    ``single_sep_thousands``, ``comma_decimal_hint``, ``angle_negative``,
    ``trailing_minus``, ``fallback``) or ``None`` on failure. Both public
    entry points delegate here so their behaviour can never drift.
    """
    if raw is None:
        return None, None
    if isinstance(raw, (int, Decimal)):
        return Decimal(raw), "plain"
    if isinstance(raw, float):
        return Decimal(str(raw)), "plain"
    if not isinstance(raw, str):
        try:
            return Decimal(str(raw)), "plain"
        except Exception:  # noqa: BLE001
            return None, None

    text = raw.strip()
    if not text:
        return None, None

    # Layer 1 — OCR fixes (numeric context only)
    text = _apply_ocr_fixes(text)

    # A sign recovery (angle-bracket / trailing-minus) is itself a noteworthy
    # heuristic — capture it before normalization rewrites the token.
    recovery_basis: Optional[str] = None
    if _ANGLE_RE.match(text):
        recovery_basis = "angle_negative"
    elif not text.startswith("-") and _TRAILING_MINUS_RE.match(text):
        recovery_basis = "trailing_minus"

    # Layer 2 — Locale normalization (parens, angle brackets, dashes, NBSP, …)
    text = _normalize_locale(text)
    if text in ("", "0"):
        return (Decimal("0"), "plain") if text == "0" else (None, None)

    # Layer 2.5 — Resolve single-separator thousands ambiguity BEFORE Babel.
    text, disamb_basis = _disambiguate_single_separator(text, comma_decimal=comma_decimal)

    # Layer 3 — Babel locale-aware parser, with plain fallback on failure
    try:
        value = parse_decimal(text, locale="fr_FR")
        parse_basis = "plain"
    except (UnknownLocaleError, Exception):  # noqa: BLE001 — Babel NumberFormatError
        value = _fallback_parse(text)
        parse_basis = "fallback"

    if value is None:
        return None, None
    # Priority: the value-scale decision (disambiguation) > sign recovery > how
    # the cleaned token finally parsed.
    basis = disamb_basis or recovery_basis or parse_basis
    return value, basis


def parse_french_number(raw: object, comma_decimal: Optional[bool] = None) -> Optional[Decimal]:
    """Parse a French-formatted number string to ``Decimal``.

    Handles OCR artifacts (``O`` ↔ ``0``), French thousands/decimal separators
    (``1 500,00`` or ``1.500,00``), parenthesised negatives, and dash-as-zero.

    ``comma_decimal`` is the optional per-document convention hint from
    ``infer_comma_convention``: ``True`` forces a single comma to be read as the
    decimal separator (resolves ``3811603,553`` → 3811603.553 in a comma-decimal
    document); ``None`` (default) preserves the exact prior token-level behavior.

    Returns ``None`` when the input cannot be coerced to a number.
    """
    return _parse_core(raw, comma_decimal)[0]


def parse_french_number_with_basis(
    raw: object, comma_decimal: Optional[bool] = None
) -> tuple[Optional[Decimal], Optional[str]]:
    """Like :func:`parse_french_number` but also returns the parse **basis** —
    how the value was derived. ``basis ∈ ASSUMPTION_BASES`` marks a value whose
    scale rests on a corpus assumption / per-document inference (for HITL review).
    """
    return _parse_core(raw, comma_decimal)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def is_numeric_token(text: str) -> bool:
    """Return True if ``text`` could plausibly represent a French number."""
    if not isinstance(text, str):
        return False
    candidate = text.strip()
    if not candidate:
        return False
    return parse_french_number(candidate) is not None


# A trailing minus on an otherwise-unsigned number (sign recovery, e.g. "060201-")
# and a stray short space-group at the end of an integer (digit-split OCR artifact,
# e.g. "-52955 0") both parse to *a* number, but the recovery is unreliable — the
# value should be flagged for review, not emitted green/confident.
_RISKY_TRAILING_MINUS_RE = re.compile(r"\d\s*-\s*$")
_RISKY_STRAY_GROUP_RE = re.compile(r"\d[ ]\d{1,2}$")


def numeric_parse_is_risky(raw: object) -> bool:
    """Return True if ``raw`` parses but only via an *unreliable* OCR recovery.

    Distinct from a parse failure (which already flags yellow): these tokens yield
    a value, but one that should be reviewed rather than trusted —
      * trailing minus on an unsigned number  ("060201-")
      * a stray 1-2 digit group at the end of an integer ("-52955 0")
    Conservative by design: a clean decimal ("11202 126,240") or a valid 3-digit
    thousands group ("<2095 893>") is NOT risky.
    """
    if not isinstance(raw, str):
        return False
    s = raw.strip()
    if not s:
        return False
    if not s.lstrip("(-<").startswith("-") and _RISKY_TRAILING_MINUS_RE.search(s):
        return True
    # Stray short trailing group only when there is no decimal separator turning
    # those digits into a fractional part.
    core = re.sub(r"[^\d ]", " ", s).strip()
    if "," not in s and "." not in s and _RISKY_STRAY_GROUP_RE.search(core):
        return True
    return False
