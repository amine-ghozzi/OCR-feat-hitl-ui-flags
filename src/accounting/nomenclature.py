"""
Nomenclature Dictionary — the pipeline's single source of truth for
French/NCT accounting terms.

Loads ``rules/nomenclature_data.yaml`` and exposes the public surface
described in §3 of the nomenclature revamp plan:

    resolve_fffd(text)              — replace U+FFFD (and ``?``) via wildcard search
    classify_statement(rows)        — infer bilan / compte_resultat / flux_tresorerie
    map_to_field(canonical_term)    — bridge to validation_rules.json
    fuzzy_match(text, section=None) — 3-stage matcher (exact → variation → rapidfuzz)
    get_terms_for(statement_type)   — canonical vocabulary per statement
    get_prompt_vocab(statement_type)— newline-joined vocab for VLM prompt
    classify_section(text)          — section heuristic for header detection (§14)

The schema mirrors §19 ``NomenclatureEntry`` and includes the column_model /
section registries from §15/§27.
"""

from __future__ import annotations

import itertools
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import yaml
from rapidfuzz import fuzz, process, utils

logger = logging.getLogger(__name__)

# Replacement characters: U+FFFD is what Python produces for undecodable bytes,
# and "?" is the ASCII fallback some extractors emit instead.
_WILDCARD_CHARS = ("�", "?")

# Cartesian-expansion cap: "b�n�fice" with ~40 unicode candidates per slot
# would explode. Cap at 144 per §9.
# For 1-slot cases 40 candidates is trivial. For 2-slot cases we need
# 40*40 = 1600 combinations to cover all accent pairs. Cap at 3000 which
# still takes microseconds to enumerate and prevents 3+-slot blow-up.
_MAX_FFFD_CANDIDATES = 3000

# Fuzzy thresholds (§13)
_FUZZ_HIGH = 85      # auto-accept as green
_FUZZ_MED_CUTOFF = 75  # below this, treat as custom/unrecognized

# Candidate character set used to fill U+FFFD. We bias toward accented French
# letters + common ligatures. The 27-char French alphabet is plenty.
_FFFD_FILL_CHARS = "éèêëàâäîïôöùûüçñÉÈÊËÀÂÄÎÏÔÖÙÛÜÇÑeaiouycnEAIOUYCN"


def _normalize(text: str) -> str:
    """Accent-strip + lowercase + collapse whitespace — used for matching keys."""
    if not text:
        return ""
    # NFKD then drop combining marks → strip accents
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.lower()
    # Apostrophes / ampersands -> space, so "l'exercice" == "l exercice"
    # and the ampersand forms ("stocks & en-cours") still match the
    # "et"-joined canonical form after token normalization.
    for sep in ("'", "’", "‘", "`", "&"):
        lowered = lowered.replace(sep, " ")
    # Collapse any whitespace (incl. NBSP) to single space
    collapsed = re.sub(r"[\s  ]+", " ", lowered).strip()
    return collapsed


# Any run of whitespace, NBSP (U+00A0) and narrow NBSP (U+202F) included — the
# same class `_normalize` collapses for its matching keys.
_WHITESPACE_RUN_RE = re.compile(r"[\s  ]+")


def _collapse_whitespace(text: str) -> str:
    """Lowercase + collapse whitespace runs, WITHOUT stripping accents.

    The structural-keyword scan in :meth:`classify_statement_detailed` needs the
    accents — its keywords carry them — but must not see a raw newline. Since
    OCR-13 the extracted text keeps the whitespace it was read with, and a
    multi-word keyword like "actifs non courants" misses against
    "actifs\\nnon courants". `_normalize` already collapses runs the same way for
    the matching keys; this is that half of it, for a scan that cannot use them.
    """
    return _WHITESPACE_RUN_RE.sub(" ", text.lower()).strip()


# Qualifier tokens that *flip a label's meaning*: a residual ("autres"), a total,
# or a gross/net variant. "Autres produits d'exploitation" is the residual *other*
# line — logically disjoint from "Produits d'exploitation" and its "Total des …"; a
# lexical scorer (token_sort_ratio) can't see this, so we guard it explicitly. Each
# family maps to the accent-stripped token variants that mark it.
_QUALIFIER_FAMILIES: dict[str, tuple[str, ...]] = {
    "autres": ("autre", "autres"),
    "total": ("total", "totaux"),
    "net": ("net", "nets", "nette", "nettes"),
    "brut": ("brut", "bruts", "brute", "brutes"),
}

# Section-discriminating tokens (ISSUE-4): *within* the "total" family a bare
# grand-total ("Total des passifs") and a more-qualified sibling ("Total des passifs
# non courants") share the same `total` qualifier, so the Stage-3 lexical scorer
# fuzzy-collapses the grand-total onto the qualified sibling. These tokens mark the
# balance-sheet section a subtotal belongs to ("non courants" vs "courants"), so
# detecting them splits the `total` pool into distinct qualifier sets that cannot
# collapse onto one another. "non courant(s)" is matched as a two-token phrase
# *before* "courant(s)" so a non-current label is tagged {"non_courants"} only,
# never {"courants"} as well.
# NOTE: "propres" was deliberately NOT included. It discriminates nothing for the
# real defects (in "capitaux propres et passifs" both the grand-total and its equity
# sibling carry "propres", so the variation pins resolve them), and it made the audit
# flag a *correct* mapping — "TOTAL DES CAPITAUX ET DES PASSIFS" (no "propres") ->
# "Total des capitaux propres et des passifs" — as a false qualifier mismatch.
_SECTION_QUALIFIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("non_courants", ("non courants", "non courant")),
    ("courants", ("courants", "courant")),
)


def _qualifier_set(normalized: str) -> frozenset[str]:
    """Return the discriminative-qualifier families present in a normalized label.

    Detects a qualifier as a **whole token** (clean spaced forms,
    ``"total des produits d exploitation"`` → ``{"total"}``) OR as the **prefix of a
    single concatenated token** (OCR-mangled forms, ``"totalactifsimmobilises"`` →
    ``{"total"}``). A bare concept yields the empty set
    (``"produits d exploitation"`` → ``frozenset()``), so it will not match its
    ``Autres …``/``Total des …`` sibling once the matcher filters on this set.

    Section-discriminating tokens (``non courants`` / ``courants`` / ``propres``) are
    added as their own families (ISSUE-4) so that, within the ``total`` family, a bare
    grand-total ("total des passifs" → ``{"total"}``) and a section-qualified sibling
    ("total des passifs non courants" → ``{"total", "non_courants"}``) land in
    different pools and cannot fuzzy-collapse onto each other.
    """
    tokens = normalized.split()
    single_token = len(tokens) == 1
    found: set[str] = set()
    for family, variants in _QUALIFIER_FAMILIES.items():
        if any(v in tokens for v in variants) or (
            single_token and any(normalized.startswith(v) for v in variants)
        ):
            found.add(family)
    # Section qualifiers — phrase-aware, on the spaced/normalized string. "non
    # courants" must win over "courants" for non-current labels, so once a label is
    # tagged "non_courants" we skip the bare "courants" family for it.
    saw_non_courants = False
    for family, phrases in _SECTION_QUALIFIERS:
        if any(phrase in normalized for phrase in phrases):
            if family == "courants" and saw_non_courants:
                continue
            found.add(family)
            if family == "non_courants":
                saw_non_courants = True
    return frozenset(found)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class NomenclatureEntry:
    """One accounting term — see §19."""

    canonical_term: str
    normalized: str
    account_code: Optional[str] = None
    class_num: Optional[int] = None
    statement_type: str = "bilan"        # bilan | compte_resultat | flux_tresorerie
    section: Optional[str] = None
    sub_type: Optional[str] = None        # actif | passif | null
    validation_field: Optional[str] = None
    is_subtotal: bool = False
    decomposable: bool = False
    parent_subtotal: Optional[str] = None
    column_model: Optional[str] = None
    variations: list[str] = field(default_factory=list)

    def normalized_variations(self) -> list[str]:
        """All variation forms pre-normalized for fast lookup."""
        return [_normalize(v) for v in self.variations]


@dataclass
class MatchResult:
    """Outcome of ``fuzzy_match`` — mirrors the canonical output schema (§5)."""

    entry: Optional[NomenclatureEntry]
    match_type: str                       # exact | resolved | fuzzy | custom | unrecognized
    confidence: float
    score: float = 0.0                     # raw fuzz score 0..100 (0 if non-fuzzy)


@dataclass
class Classification:
    """A statement-type verdict WITH the evidence behind it.

    ``classify_statement`` returns a bare string, so a 3-vs-3 near-tie is
    indistinguishable from a 30-vs-0 landslide and nothing downstream can decide
    whether a weak answer is worth acting on. This carries the full picture so a
    caller can gate on it.

    ``term_scores`` counts only rows that matched the nomenclature; the
    structural keyword families are counted separately in ``booster_scores``.
    Keeping them apart matters: a single booster is worth +3 and can carry a
    verdict on its own with zero real term matches — the mechanism by which a
    Compte de Résultat fragment scores as ``bilan`` via the "amort" family.
    """

    statement_type: str                    # bilan | compte_resultat | flux_tresorerie | unknown
    sub_type: Optional[str] = None         # actif | passif | None
    scores: dict = field(default_factory=dict)          # term + booster, per type
    term_scores: dict = field(default_factory=dict)     # nomenclature matches only
    booster_scores: dict = field(default_factory=dict)  # structural keywords only
    winner: Optional[str] = None
    runner_up: Optional[str] = None
    margin: int = 0                        # winner − runner-up
    confidence: float = 0.0

    @property
    def qualified_type(self) -> str:
        """``bilan_actif`` / ``bilan_passif`` when the side is known.

        This is the vocabulary the YAML ``column_models[*].applies_to`` uses —
        the bare ``classify_statement`` can only ever emit ``bilan``, so those
        profile bindings could never match anything.
        """
        if self.statement_type == "bilan" and self.sub_type:
            return f"bilan_{self.sub_type}"
        return self.statement_type

    @property
    def passes_gate(self) -> bool:
        """Whether this verdict is solid enough to act on.

        All three must hold — a bare ``score >= 2`` is satisfiable by one
        structural booster alone:
          * the winner clears the absolute floor,
          * it beats the runner-up by a clear margin (not a coin-flip), and
          * at least one REAL nomenclature term backs it, not boosters alone.
        """
        if self.statement_type == "unknown" or self.winner is None:
            return False
        return (
            self.scores.get(self.winner, 0) >= _GATE_MIN_SCORE
            and self.margin >= _GATE_MIN_MARGIN
            and self.term_scores.get(self.winner, 0) >= _GATE_MIN_TERM_HITS
        )

    @property
    def gate_reason(self) -> Optional[str]:
        """Why :attr:`passes_gate` is False — None when it passes."""
        if self.passes_gate:
            return None
        if self.statement_type == "unknown" or self.winner is None:
            return "unclassified"
        if self.scores.get(self.winner, 0) < _GATE_MIN_SCORE:
            return "score_below_floor"
        if self.margin < _GATE_MIN_MARGIN:
            return "margin_below_threshold"
        return "no_term_evidence"


# Gate thresholds. Calibrated against the Samples/ corpus — see the
# ``passes_gate`` docstring for why all three are needed rather than the single
# ``score >= 2`` the legacy classifier used.
_GATE_MIN_SCORE = 2
_GATE_MIN_MARGIN = 2
_GATE_MIN_TERM_HITS = 1


# ---------------------------------------------------------------------------
# Dictionary
# ---------------------------------------------------------------------------


class NomenclatureDictionary:
    """Central dictionary — loads YAML once, serves all consumers."""

    def __init__(
        self,
        entries: list[NomenclatureEntry],
        column_models: dict,
        sections: dict,
        meta: Optional[dict] = None,
    ):
        self.entries: list[NomenclatureEntry] = entries
        self.column_models: dict = column_models
        self.sections: dict = sections
        self.meta: dict = meta or {}

        # Indices --------------------------------------------------------
        # Exact normalized → entry (first canonical wins; variations do not
        # override canonicals with the same normalized form).
        self._by_normalized: dict[str, NomenclatureEntry] = {}
        # Variation normalized → entry (§13 Stage 2)
        self._by_variation: dict[str, NomenclatureEntry] = {}
        # statement_type → [entries]
        self._by_statement: dict[str, list[NomenclatureEntry]] = {}
        # section → [entries]
        self._by_section: dict[str, list[NomenclatureEntry]] = {}
        # validation_field → entry
        self._by_validation_field: dict[str, NomenclatureEntry] = {}
        # account_code → entry (first registered wins)
        self._by_account_code: dict[str, NomenclatureEntry] = {}

        # Variation logger (§28) — optional, loaded lazily
        self._variation_logger = None
        self._approved_variations: dict[str, str] = {}  # raw → canonical
        self._confirmed_customs: set[str] = set()
        self._load_variation_overrides()

        self._build_indices()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path | str) -> "NomenclatureDictionary":
        path = Path(path)
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        raw_entries = data.get("entries", []) or []
        entries: list[NomenclatureEntry] = []
        for raw in raw_entries:
            canonical = raw.get("canonical_term", "").strip()
            if not canonical:
                continue
            normalized = raw.get("normalized") or _normalize(canonical)
            entries.append(
                NomenclatureEntry(
                    canonical_term=canonical,
                    normalized=normalized,
                    account_code=raw.get("account_code"),
                    class_num=raw.get("class_num"),
                    statement_type=raw.get("statement_type", "bilan"),
                    section=raw.get("section"),
                    sub_type=raw.get("sub_type"),
                    validation_field=raw.get("validation_field"),
                    is_subtotal=bool(raw.get("is_subtotal", False)),
                    decomposable=bool(raw.get("decomposable", False)),
                    parent_subtotal=raw.get("parent_subtotal"),
                    column_model=raw.get("column_model"),
                    variations=list(raw.get("variations") or []),
                )
            )

        return cls(
            entries=entries,
            column_models=data.get("column_models", {}) or {},
            sections=data.get("sections", {}) or {},
            meta=data.get("meta", {}) or {},
        )

    def _load_variation_overrides(self) -> None:
        """Load approved variations + confirmed customs from the log (§28)."""
        try:
            from accounting.variation_logger import VariationLogger
            self._variation_logger = VariationLogger()
            self._approved_variations = self._variation_logger.get_approved_variations()
            self._confirmed_customs = self._variation_logger.get_confirmed_customs()
            if self._approved_variations:
                logger.info(
                    "Loaded %d approved variation overrides from log",
                    len(self._approved_variations),
                )
        except Exception:  # noqa: BLE001 — variation logger is optional
            self._variation_logger = None
            self._approved_variations = {}
            self._confirmed_customs = set()

    def _build_indices(self) -> None:
        for entry in self.entries:
            # Canonical index — skip collisions so the first registration wins.
            # (The YAML intentionally lists "Résultat net de l'exercice" under
            # both compte_resultat and flux_tresorerie; the first is the
            # authoritative mapping for exact lookups.)
            self._by_normalized.setdefault(entry.normalized, entry)
            # Variations index
            for v_norm in entry.normalized_variations():
                if v_norm and v_norm != entry.normalized:
                    self._by_variation.setdefault(v_norm, entry)
            # Statement / section buckets
            self._by_statement.setdefault(entry.statement_type, []).append(entry)
            if entry.section:
                self._by_section.setdefault(entry.section, []).append(entry)
            # Validation field index
            if entry.validation_field:
                self._by_validation_field.setdefault(entry.validation_field, entry)
            # Account code index
            if entry.account_code:
                self._by_account_code.setdefault(str(entry.account_code), entry)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def all_entries(self) -> list[NomenclatureEntry]:
        return self.entries

    def get_terms_for(self, statement_type: str) -> list[NomenclatureEntry]:
        """All canonical entries scoped to one statement type."""
        return list(self._by_statement.get(statement_type, []))

    def get_prompt_vocab(self, statement_type: str) -> str:
        """Newline-joined canonical terms — for grounding the VLM prompt (§6)."""
        terms = [e.canonical_term for e in self.get_terms_for(statement_type)]
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for t in terms:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        return "\n".join(f"- {t}" for t in unique)

    def map_to_field(self, canonical_term: str) -> Optional[str]:
        """Canonical term or known variation → validation_field.

        Bridge to ``validation_rules.json``. Falls through to the variations
        index so aliases (e.g. "chiffre d'affaires" → ``Revenus``) resolve
        without the caller having to run ``fuzzy_match`` first.
        """
        norm = _normalize(canonical_term)
        entry = self._by_normalized.get(norm) or self._by_variation.get(norm)
        return entry.validation_field if entry else None

    def lookup(self, canonical_term: str) -> Optional[NomenclatureEntry]:
        """Direct lookup by canonical term or known variation (normalized)."""
        norm = _normalize(canonical_term)
        return self._by_normalized.get(norm) or self._by_variation.get(norm)

    def lookup_by_account(self, code: str) -> Optional[NomenclatureEntry]:
        return self._by_account_code.get(str(code))

    def get_column_model(self, name: str) -> Optional[dict]:
        return self.column_models.get(name)

    def get_section(self, name: str) -> Optional[dict]:
        return self.sections.get(name)

    # ------------------------------------------------------------------
    # FFFD resolution (§9)
    # ------------------------------------------------------------------

    def resolve_fffd(self, text: str) -> tuple[str, float, int]:
        """Replace ``\\uFFFD`` / ``?`` placeholders with accented candidates.

        Returns ``(resolved_text, confidence, candidate_count)``:
            - ``resolved_text``: the best canonical form (or original if no
              dictionary hit is found).
            - ``confidence``: 0.99 for unambiguous single match, 0.80 for 2-3
              candidates, 0.50 for >3 (§18 confidence gate), 1.0 if nothing
              to resolve.
            - ``candidate_count``: how many dictionary entries matched the
              wildcard pattern (1 = unambiguous).
        """
        if not text:
            return text, 1.0, 0
        if not any(ch in text for ch in _WILDCARD_CHARS):
            return text, 1.0, 0

        # Normalize both wildcards to a single "?" for uniform handling.
        probe = text
        for wc in _WILDCARD_CHARS:
            probe = probe.replace(wc, "?")

        slot_positions = [i for i, ch in enumerate(probe) if ch == "?"]
        # Bound the search: if too many wildcards, skip expensive Cartesian.
        if len(_FFFD_FILL_CHARS) ** len(slot_positions) > _MAX_FFFD_CANDIDATES * 10:
            # Too many slots — fall back to full-phrase fuzzy match.
            mr = self.fuzzy_match(probe.replace("?", ""))
            if mr.entry:
                return mr.entry.canonical_term, min(0.80, mr.confidence), 1
            return text, 0.50, 0

        # Enumerate candidate fills, but cap at _MAX_FFFD_CANDIDATES.
        fills = itertools.islice(
            itertools.product(_FFFD_FILL_CHARS, repeat=len(slot_positions)),
            _MAX_FFFD_CANDIDATES,
        )

        probe_norm = _normalize(probe.replace("?", ""))

        hits: list[NomenclatureEntry] = []
        seen_ids: set[int] = set()

        for fill in fills:
            chars = list(probe)
            for pos, ch in zip(slot_positions, fill):
                chars[pos] = ch
            candidate_norm = _normalize("".join(chars))
            entry = self._by_normalized.get(candidate_norm) or self._by_variation.get(
                candidate_norm
            )
            if entry and id(entry) not in seen_ids:
                seen_ids.add(id(entry))
                hits.append(entry)

        if not hits:
            # No exact dictionary hit — try fuzzy against the wildcard-stripped form.
            mr = self.fuzzy_match(probe.replace("?", ""))
            if mr.entry and mr.confidence >= 0.80:
                return mr.entry.canonical_term, min(0.80, mr.confidence), 1
            return text, 0.50, 0

        if len(hits) == 1:
            return hits[0].canonical_term, 0.99, 1
        if len(hits) <= 3:
            # Ambiguous — prefer the one whose normalized form is closest
            best = max(
                hits,
                key=lambda e: fuzz.ratio(e.normalized, probe_norm, processor=utils.default_process),
            )
            return best.canonical_term, 0.80, len(hits)
        return hits[0].canonical_term, 0.50, len(hits)

    # ------------------------------------------------------------------
    # Fuzzy matching (§13)
    # ------------------------------------------------------------------

    def fuzzy_match(
        self,
        raw_label: str,
        section: Optional[str] = None,
        statement_type: Optional[str] = None,
        document_id: str = "",
    ) -> MatchResult:
        """3-stage cascade: exact → variation → rapidfuzz.

        ``section`` narrows the search to one section's entries when known
        (much faster and more accurate — see §13 section scoping).
        ``statement_type`` narrows similarly (used when section is unknown).
        ``document_id`` is forwarded to the variation logger (§28) so
        promotable candidates can be traced back to source documents.
        """
        result = self._fuzzy_match_inner(raw_label, section, statement_type)
        # §28 — log fuzzy/resolved matches with confidence < 0.95 for the
        # candidate→promoted lifecycle. ``log_variation_candidate`` itself
        # filters by match_type/confidence, so this is just a forwarding hook.
        if (
            self._variation_logger is not None
            and result.entry is not None
            and result.match_type in ("fuzzy", "resolved")
        ):
            try:
                self._variation_logger.log_variation_candidate(
                    raw_text=raw_label,
                    canonical_term=result.entry.canonical_term,
                    match_type=result.match_type,
                    confidence=result.confidence,
                    section=section,
                    document_id=document_id,
                )
            except Exception:  # noqa: BLE001 — logging is best-effort
                pass
        return result

    def _fuzzy_match_inner(
        self,
        raw_label: str,
        section: Optional[str] = None,
        statement_type: Optional[str] = None,
    ) -> MatchResult:
        """Core matcher — pure function with no logging side-effects."""
        if not raw_label:
            return MatchResult(entry=None, match_type="unrecognized", confidence=0.0)

        normalized = _normalize(raw_label)
        if not normalized:
            return MatchResult(entry=None, match_type="unrecognized", confidence=0.0)

        # Stage 0 — confirmed custom bypass (§28)
        if raw_label in self._confirmed_customs:
            return MatchResult(entry=None, match_type="custom", confidence=0.0)

        # Grand-total guard: the balance-sheet grand total ("Total des capitaux
        # propres et des passifs") straddles the equity/liability boundary — its
        # canonical lives in the passifs section, so a row mis-scoped to
        # capitaux_propres would fuzzy-collapse onto the equity-only "Total des
        # capitaux propres". Search UNSCOPED for a compound capitaux+passifs label
        # so the grand-total canonical is reachable regardless of section. Only
        # bilan grand-totals carry both tokens, so cross-statement scope is safe.
        if "capitaux" in normalized and "passif" in normalized:
            section = None
            statement_type = None

        # Intermediate equity subtotal: "Total des capitaux propres avant résultat
        # de l'exercice" is a presentation subtotal (equity BEFORE the year's
        # result), distinct from the final "...avant affectation" total. Mapping
        # both to the same canonical double-binds total_capitaux_propres, so the
        # intermediate is left unmapped — matching the reference.
        if "capitaux propres avant resultat" in normalized:
            return MatchResult(entry=None, match_type="custom", confidence=0.0)

        scope = self._resolve_scope(section, statement_type)

        # Stage 1 — exact normalized lookup
        if scope is self.entries:
            entry = self._by_normalized.get(normalized) or self._by_variation.get(
                normalized
            )
            if entry:
                match_type = "exact" if entry.normalized == normalized else "resolved"
                confidence = 1.0 if match_type == "exact" else 0.98
                return MatchResult(entry=entry, match_type=match_type, confidence=confidence, score=100.0)
        else:
            for entry in scope:
                if entry.normalized == normalized:
                    return MatchResult(entry=entry, match_type="exact", confidence=1.0, score=100.0)
            for entry in scope:
                if normalized in entry.normalized_variations():
                    return MatchResult(entry=entry, match_type="resolved", confidence=0.98, score=98.0)

        # Stage 2.5 — approved variation override (§28 hot-reload)
        if raw_label in self._approved_variations:
            canonical = self._approved_variations[raw_label]
            entry = self._by_normalized.get(_normalize(canonical))
            if entry:
                return MatchResult(entry=entry, match_type="resolved", confidence=0.98, score=98.0)

        # Stage 3 — rapidfuzz (token_sort_ratio, then partial_ratio).
        # Discriminative-qualifier guard: a bare concept must not fuzzy-match its
        # "Autres"/"Total"/"Net"/"Brut" sibling — the extra token flips the meaning,
        # which the lexical scorer cannot see (and token_set_ratio would score the
        # subset 100). Pre-filter the pool to entries whose qualifier set matches the
        # query's; an emptied pool falls through to custom/unrecognized below.
        q_quals = _qualifier_set(normalized)
        candidates = {e.normalized: e for e in scope if _qualifier_set(e.normalized) == q_quals}
        if not candidates:
            return MatchResult(entry=None, match_type="custom", confidence=0.0)

        best_result = process.extractOne(
            normalized,
            list(candidates.keys()),
            scorer=fuzz.token_sort_ratio,
            processor=utils.default_process,
            score_cutoff=_FUZZ_MED_CUTOFF,
        )
        if best_result is not None:
            matched_key, score, _ = best_result
            if score >= _FUZZ_HIGH:
                return MatchResult(
                    entry=candidates[matched_key],
                    match_type="fuzzy",
                    confidence=score / 100.0,
                    score=float(score),
                )

        partial_result = process.extractOne(
            normalized,
            list(candidates.keys()),
            scorer=fuzz.partial_ratio,
            processor=utils.default_process,
            score_cutoff=80,
        )
        if partial_result is not None:
            matched_key, score, _ = partial_result
            if score >= _FUZZ_HIGH:
                return MatchResult(
                    entry=candidates[matched_key],
                    match_type="fuzzy",
                    confidence=score / 100.0,
                    score=float(score),
                )

        # Fallback — variation aliases. Only reached when the canonical-form fuzzy
        # above found nothing, so it never overrides an existing match (no
        # regression). Lets an OCR-truncated label ("...et des enc") still match
        # its full variation ("...et des encours"), behind the same qualifier guard.
        alias_pool: dict[str, NomenclatureEntry] = {}
        for e in scope:
            for v in e.normalized_variations():
                if v and v not in candidates and _qualifier_set(v) == q_quals:
                    alias_pool.setdefault(v, e)
        if alias_pool:
            alias_result = process.extractOne(
                normalized,
                list(alias_pool.keys()),
                scorer=fuzz.token_sort_ratio,
                processor=utils.default_process,
                score_cutoff=_FUZZ_HIGH,
            )
            if alias_result is not None:
                matched_key, score, _ = alias_result
                return MatchResult(
                    entry=alias_pool[matched_key],
                    match_type="fuzzy",
                    confidence=score / 100.0,
                    score=float(score),
                )

        # Best available below threshold — return as custom with score info
        best_score = 0.0
        best_entry = None
        if best_result is not None:
            matched_key, best_score, _ = best_result
            best_entry = candidates[matched_key]
        return MatchResult(
            entry=None,
            match_type="custom",
            confidence=best_score / 100.0 if best_score else 0.0,
            score=float(best_score),
        )

    def _resolve_scope(
        self, section: Optional[str], statement_type: Optional[str]
    ) -> list[NomenclatureEntry]:
        if section and section in self._by_section:
            scope = self._by_section[section]
            # Intersect section AND statement when both are known, so a stale
            # section header (e.g. a bilan header still attached to a
            # compte_resultat row) cannot re-widen the scope across statements
            # (ISSUE-2).
            if statement_type and statement_type in self._by_statement:
                narrowed = [e for e in scope if e.statement_type == statement_type]
                if narrowed:
                    return narrowed
                # The section belongs to a *different* statement than the row —
                # i.e. it is a stale header. Trust the statement over the section
                # and fall back to the statement bucket rather than re-widening
                # back into the foreign section's vocabulary.
                return self._by_statement[statement_type]
            return scope
        if statement_type and statement_type in self._by_statement:
            return self._by_statement[statement_type]
        return self.entries

    # ------------------------------------------------------------------
    # Statement classification (§24)
    # ------------------------------------------------------------------

    def classify_statement(self, rows: Iterable[str]) -> str:
        """Classify a table by term distribution — Nomenclature-powered (§24).

        Back-compat string API. Semantics are unchanged; use
        :meth:`classify_statement_detailed` when the evidence behind the verdict
        matters (confidence, margin, actif/passif side).
        """
        return self.classify_statement_detailed(rows).statement_type

    def _bilan_side_keywords(self) -> tuple[list[str], list[str]]:
        """Normalized (actif, passif) vocabularies, built once from the entries.

        Line-item entries carry ``sub_type``, and section configs carry it too;
        both feed the side score. Cached on the instance — the dictionary is
        itself cached, so this runs once per process.
        """
        cached = getattr(self, "_bilan_sides_cache", None)
        if cached is not None:
            return cached

        actif: list[str] = []
        passif: list[str] = []
        for entry in self.entries:
            if entry.statement_type != "bilan":
                continue
            forms = [entry.normalized] + entry.normalized_variations()
            if entry.sub_type == "actif":
                actif.extend(forms)
            elif entry.sub_type == "passif":
                passif.extend(forms)
        for cfg in self.sections.values():
            if cfg.get("statement_type") != "bilan":
                continue
            label = _normalize(str(cfg.get("label_canonical", "")))
            if not label:
                continue
            if cfg.get("sub_type") == "actif":
                actif.append(label)
            elif cfg.get("sub_type") == "passif":
                passif.append(label)

        result = (
            list(dict.fromkeys(t for t in actif if t)),
            list(dict.fromkeys(t for t in passif if t)),
        )
        self._bilan_sides_cache = result
        return result

    def _score_bilan_sub_type(self, texts: list[str]) -> Optional[str]:
        """``actif`` / ``passif`` for a bilan, or None when it carries both
        sides (a combined presentation) or the evidence is too thin."""
        actif_kw, passif_kw = self._bilan_side_keywords()
        joined = _normalize(" ".join(texts))
        a = sum(1 for kw in actif_kw if kw in joined)
        p = sum(1 for kw in passif_kw if kw in joined)
        if a >= 3 and p >= 3:
            return None          # combined bilan — both sides present
        if a >= 2 and p <= 1:
            return "actif"
        if p >= 2 and a <= 1:
            return "passif"
        return None

    def classify_statement_detailed(self, rows: Iterable[str]) -> "Classification":
        """Classify a table AND report the evidence — see :class:`Classification`.

        Term-distribution hits and structural-keyword boosters are tallied
        separately so a caller can tell a verdict backed by real vocabulary from
        one carried entirely by a single ``+3`` keyword family.
        """
        term = {"bilan": 0, "compte_resultat": 0, "flux_tresorerie": 0}
        boost = {"bilan": 0, "compte_resultat": 0, "flux_tresorerie": 0}
        texts = [r for r in rows if r]
        if not texts:
            return Classification(statement_type="unknown")

        # Term distribution — one point per row that matches the nomenclature.
        for raw_text in texts:
            mr = self.fuzzy_match(raw_text)
            if mr.entry and mr.match_type in ("exact", "fuzzy", "resolved"):
                st = mr.entry.statement_type
                term[st] = term.get(st, 0) + 1

        # Structural boosters — tallied into `boost`, NOT into the term counts,
        # so a verdict carried entirely by one +3 keyword family is visible as
        # such to anything that gates on the result.
        scores = boost
        # Whitespace-collapsed: a cell can carry an internal newline (OCR-13
        # keeps the whitespace the text was read with), and every keyword below
        # is multi-word, so a raw "\n" would silently make the scan miss.
        joined = _collapse_whitespace(" ".join(texts))
        if any(kw in joined for kw in ("brut", "amort", "amortissement", "dépréc", "deprec")):
            # Brut/Amort/Net column layout → Bilan Actif
            scores["bilan"] += 3
        if any(kw in joined for kw in (
            "actifs non courants", "actifs courants", "actifs immobilis",
            "total des actifs", "total de l'actif", "total actif",
            "immobilisations incorporelles", "immobilisations corporelles",
        )):
            # Bilan Actif structural terms
            scores["bilan"] += 3
        if any(kw in joined for kw in (
            "capitaux propres", "passifs non courants", "passifs courants",
            "total des passifs", "total du passif", "total passif",
            "total des capitaux propres", "capital social",
            "capitaux propres et passifs",
        )):
            # Bilan Passif structural terms
            scores["bilan"] += 3
        if any(kw in joined for kw in ("résultat d'exploitation", "resultat d'exploitation",
                                        "produits d'exploitation", "charges d'exploitation",
                                        "charges de personnel", "achats consomm")):
            scores["compte_resultat"] += 3
        if any(kw in joined for kw in ("flux de tr", "flux net", "trésorerie de clôture",
                                        "tresorerie de cloture", "activités d'exploitation",
                                        "activités d'investissement", "activités de financement")):
            scores["flux_tresorerie"] += 3

        # Dict-union key order is deterministic (term's keys first), so ties
        # resolve exactly as the legacy ``max(scores, key=scores.get)`` did —
        # first maximal key in iteration order.
        combined = {k: term.get(k, 0) + boost.get(k, 0) for k in {**term, **boost}}
        ranked = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)
        winner, top = ranked[0]
        runner_up, second = ranked[1] if len(ranked) > 1 else (None, 0)

        # Legacy verdict semantics preserved EXACTLY (score >= 2 wins) — the
        # stricter three-part gate lives on ``Classification.passes_gate`` and
        # is opt-in, so nothing downstream changes behaviour yet.
        statement_type = winner if top >= 2 else "unknown"
        sub_type = (
            self._score_bilan_sub_type(texts) if statement_type == "bilan" else None
        )
        total = sum(combined.values())
        return Classification(
            statement_type=statement_type,
            sub_type=sub_type,
            scores=combined,
            term_scores=dict(term),
            booster_scores=dict(boost),
            winner=winner if top > 0 else None,
            runner_up=runner_up,
            margin=top - second,
            confidence=round(top / total, 3) if total else 0.0,
        )

    # ------------------------------------------------------------------
    # Section classifier (for header detection — §14 Signal 3)
    # ------------------------------------------------------------------

    def classify_section(self, text: str) -> Optional[str]:
        """Return a section key (e.g. 'actifs_non_courants') if ``text``
        matches a section label.

        Used by the font-based header detector (§14 Signal 3) and the OCR
        bbox-based fallback (§25).
        """
        if not text:
            return None
        norm = _normalize(text)
        if not norm:
            return None
        # Direct section-label match
        for section_key, cfg in self.sections.items():
            label_norm = _normalize(cfg.get("label_canonical", ""))
            if label_norm and (label_norm in norm or norm in label_norm):
                return section_key
        # Looser keyword matches for Bilan sides
        if any(kw in norm for kw in ("actifs non courants", "actif non courant",
                                      "actifs immobilises", "actif immobilise")):
            return "actifs_non_courants"
        if "actifs courants" in norm or "actif courant" in norm:
            return "actifs_courants"
        if "capitaux propres" in norm:
            return "capitaux_propres"
        if "passifs non courants" in norm or "passif non courant" in norm:
            return "passifs_non_courants"
        if "passifs courants" in norm or "passif courant" in norm:
            return "passifs_courants"
        return None


# ---------------------------------------------------------------------------
# Default loader (cached)
# ---------------------------------------------------------------------------


_DEFAULT_YAML_PATH = (
    Path(__file__).resolve().parent / "rules" / "nomenclature_data.yaml"
)


@lru_cache(maxsize=1)
def load_default_dictionary() -> NomenclatureDictionary:
    """Load ``src/accounting/rules/nomenclature_data.yaml`` (cached)."""
    return NomenclatureDictionary.from_yaml(_DEFAULT_YAML_PATH)
