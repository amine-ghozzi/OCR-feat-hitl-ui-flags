"""
Variation Capture & Bootstrapping — §28 of the nomenclature revamp plan.

Every pipeline run logs fuzzy and low-confidence matches to a JSONL staging
file.  Human HITL corrections are logged with ``status="approved"`` for
immediate use; after 3+ consistent sightings a candidate is auto-promotable to
the YAML dictionary.

The staging file is **never a tracked repo file** (GOV-4).  Its path is resolved
by :func:`resolve_log_path`: an explicit ``log_path`` wins, else the
``FINALZE_VARIATION_LOG`` environment variable, else the per-tenant default
``data/variation_candidates/<org_id or "default">.jsonl`` (an untracked
directory).

Public surface:

    log_variation_candidate(…)    — called by fuzzy_match / HITL hooks
    log_hitl_correction(…)        — called when a human corrects a label
    log_confirmed_custom(…)       — called when a human marks a row "genuinely custom"
    get_approved_variations()     — returns approved variations for hot-reload
    promote_variations(…)         — periodic batch: candidate → promoted into YAML
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import yaml

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The nomenclature YAML that `promote_variations` writes durable variations into.
_DEFAULT_NOMENCLATURE_PATH = Path(__file__).resolve().parent / "rules" / "nomenclature_data.yaml"

#: Environment variable holding the learning-log path (a *file* path). A literal
#: ``{org_id}`` placeholder in it is substituted with the tenant slug.
ENV_VAR = "FINALZE_VARIATION_LOG"


def _org_slug(org_id: Optional[str]) -> str:
    """One filesystem-safe path segment for a tenant id; ``"default"`` when absent."""
    raw = str(org_id).strip() if org_id else ""
    slug = "".join(c if (c.isalnum() or c in "._-") else "_" for c in raw).strip(".")
    return slug or "default"


def resolve_log_path(
    org_id: Optional[str] = None,
    log_path: Optional[Union[str, Path]] = None,
) -> Path:
    """Resolve the variation-log path: explicit arg > env var > per-tenant default.

    1. ``log_path`` — an explicit override (tests, probes, sweeps) always wins.
    2. ``FINALZE_VARIATION_LOG`` — a *file* path; a literal ``{org_id}`` placeholder is
       replaced by the tenant slug (or ``"default"``).
    3. ``<repo>/data/variation_candidates/<org_id or "default">.jsonl`` — an untracked
       directory, so no pipeline run can ever dirty a tracked file (GOV-4).

    Resolved at **call time**, never at import: ``load_default_dictionary()`` is
    ``lru_cache``d, so an import-time constant would bake in whatever the environment
    happened to hold when this module was first imported.
    """
    if log_path:
        return Path(log_path)
    slug = _org_slug(org_id)
    env = os.environ.get(ENV_VAR)
    if env:
        # str.replace, not str.format — a path may legitimately contain other braces
        # and must not raise KeyError/IndexError here.
        return Path(env.replace("{org_id}", slug))
    return _REPO_ROOT / "data" / "variation_candidates" / f"{slug}.jsonl"


class VariationLogger:
    """Manages the variation candidate lifecycle: candidate → approved → promoted."""

    def __init__(
        self,
        log_path: Optional[Union[str, Path]] = None,
        org_id: Optional[str] = None,
    ):
        self.org_id = org_id
        # Resolved here, per instance — see resolve_log_path's note on lru_cache.
        self.log_path = resolve_log_path(org_id, log_path)
        # Ensure parent directory exists
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Idempotency: preload already-logged keys so re-runs — and the same label
        # re-matched across pipeline stages — never re-append a duplicate. Covers the
        # cross-run case, not just within one process (the source of the 35x blow-up).
        self._seen: set[tuple] = set()
        for record in self._read_all():
            self._seen.add(self._dedup_key(record))

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def log_variation_candidate(
        self,
        raw_text: str,
        canonical_term: Optional[str],
        match_type: str,
        confidence: float,
        section: Optional[str] = None,
        document_id: str = "",
    ) -> None:
        """Log a potential new variation for future review.

        Called automatically when ``fuzzy_match()`` returns ``confidence < 0.95``
        and ``match_type`` is ``fuzzy`` or ``resolved``.
        """
        if match_type not in ("fuzzy", "resolved") or confidence >= 0.95:
            return  # Only log uncertain matches

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw_text": raw_text,
            "matched_to": canonical_term,
            "confidence": round(confidence, 4),
            "match_type": match_type,
            "section": section,
            "document_id": document_id,
            "status": "candidate",
        }
        self._append(entry)

    def log_hitl_correction(
        self,
        raw_text: str,
        canonical_term: str,
        document_id: str = "",
    ) -> None:
        """Log a human-validated correction — immediately usable.

        Called when a HITL reviewer selects the correct canonical term for
        a label.  Status is ``approved`` — no candidate-stage wait.
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw_text": raw_text,
            "matched_to": canonical_term,
            "confidence": 1.0,
            "match_type": "hitl_correction",
            "section": None,
            "document_id": document_id,
            "status": "approved",
        }
        self._append(entry)

    def log_confirmed_custom(
        self,
        raw_text: str,
        document_id: str = "",
    ) -> None:
        """Mark a label as genuinely company-specific.

        Prevents future pipeline runs from re-suggesting this text as a
        fuzzy match candidate.
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw_text": raw_text,
            "matched_to": None,
            "confidence": 0.0,
            "match_type": "confirmed_custom",
            "section": None,
            "document_id": document_id,
            "status": "confirmed_custom",
        }
        self._append(entry)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_approved_variations(self) -> dict[str, str]:
        """Return all approved (human-validated) variations as {raw_text: canonical}.

        Used for hot-reload: the fuzzy matcher can check these before
        running RapidFuzz, providing O(1) lookups for known corrections.
        """
        approved: dict[str, str] = {}
        for record in self._read_all():
            if record.get("status") == "approved" and record.get("matched_to"):
                approved[record["raw_text"]] = record["matched_to"]
        return approved

    def get_confirmed_customs(self) -> set[str]:
        """Return all confirmed-custom labels (should never be fuzzy-matched)."""
        customs: set[str] = set()
        for record in self._read_all():
            if record.get("status") == "confirmed_custom":
                customs.add(record["raw_text"])
        return customs

    def get_promotable_candidates(self, min_count: int = 3) -> list[dict]:
        """Find candidates seen in ≥ ``min_count`` **distinct documents** with a
        consistent mapping.

        Evidence is the number of distinct, non-empty ``document_id``s — not raw log
        lines — so re-processing one document (or empty-``document_id`` local runs)
        cannot inflate a candidate toward promotion. Returns
        ``{raw_text, canonical_term, count, consistent}`` dicts ready for promotion to
        ``nomenclature_data.yaml``.
        """
        # Group candidates by (raw_text, matched_to) → set of distinct source documents.
        pair_docs: dict[tuple[str, str], set[str]] = {}
        for record in self._read_all():
            if record.get("status") == "candidate" and record.get("matched_to"):
                key = (record["raw_text"], record["matched_to"])
                docs = pair_docs.setdefault(key, set())
                doc = record.get("document_id") or ""
                if doc:  # empty document_id carries no promotion evidence
                    docs.add(doc)

        # Check for consistency: same raw_text always maps to same canonical
        raw_to_canonicals: dict[str, set[str]] = {}
        for (raw, canon) in pair_docs:
            raw_to_canonicals.setdefault(raw, set()).add(canon)

        promotable = []
        for (raw, canon), docs in pair_docs.items():
            count = len(docs)
            if count >= min_count:
                promotable.append({
                    "raw_text": raw,
                    "canonical_term": canon,
                    "count": count,
                    "consistent": len(raw_to_canonicals.get(raw, set())) == 1,
                })
        return promotable

    def log_promoted(self, raw_text: str, canonical_term: str, document_id: str = "") -> None:
        """Record that a variation was durably promoted into the nomenclature YAML."""
        self._append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw_text": raw_text,
            "matched_to": canonical_term,
            "confidence": 1.0,
            "match_type": "promoted",
            "section": None,
            "document_id": document_id,
            "status": "promoted",
        })

    def promote_variations(
        self,
        min_count: int = 3,
        dry_run: bool = True,
        nomenclature_path: Optional[Path] = None,
    ) -> list[dict]:
        """Promote consistent promotable candidates into ``nomenclature_data.yaml``.

        Durable ("slow path") consolidation: each candidate ``raw_text → canonical_term``
        becomes a new ``variations`` entry under the canonical entry. Idempotent (skips
        variations already present, normalized). **Defaults to ``dry_run``** — the
        permanent YAML edit should be reviewed/confirmed, not silently auto-applied (the
        reversible auto-apply path is the soft *approved* status the matcher hot-reloads).
        With ``dry_run=False`` it writes the YAML and logs a ``promoted`` status per applied
        candidate. Returns the plan: ``[{raw_text, canonical_term, count, applied, reason}]``.

        NOTE: PyYAML round-trip does not preserve comments/formatting in the file.
        """
        from accounting.nomenclature import _normalize  # local import avoids a cycle

        path = Path(nomenclature_path) if nomenclature_path else _DEFAULT_NOMENCLATURE_PATH
        candidates = [c for c in self.get_promotable_candidates(min_count) if c.get("consistent")]
        if not candidates:
            return []

        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        by_canon: dict[str, dict] = {}
        for e in data.get("entries") or []:
            ct = (e.get("canonical_term") or "").strip()
            if ct:
                by_canon.setdefault(ct, e)

        plan: list[dict] = []
        for c in candidates:
            raw, canon = c["raw_text"], c["canonical_term"]
            entry = by_canon.get(canon)
            if entry is None:
                plan.append({**c, "applied": False, "reason": "canonical_term not in nomenclature"})
                continue
            existing = {_normalize(v) for v in (entry.get("variations") or [])}
            existing.add(_normalize(entry.get("normalized") or canon))
            if _normalize(raw) in existing:
                plan.append({**c, "applied": False, "reason": "already present"})
                continue
            plan.append({**c, "applied": (not dry_run), "reason": "promote"})
            if not dry_run:
                entry.setdefault("variations", []).append(raw)

        if not dry_run and any(p["applied"] for p in plan):
            with path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
            for p in plan:
                if p["applied"]:
                    self.log_promoted(p["raw_text"], p["canonical_term"])
        return plan

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _dedup_key(record: dict) -> tuple:
        """Identity of a log entry for dedup: same (raw, matched, document, status)
        is the *same* observation and must be logged at most once."""
        return (
            record.get("raw_text"),
            record.get("matched_to"),
            record.get("document_id", "") or "",
            record.get("status"),
        )

    def _append(self, entry: dict) -> None:
        key = self._dedup_key(entry)
        if key in self._seen:
            return  # already logged this observation — skip the duplicate
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._seen.add(key)  # only mark seen after a successful write
        except OSError:
            logger.warning("Failed to write variation log entry to %s", self.log_path)

    def _read_all(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        records: list[dict] = []
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            logger.warning("Failed to read variation log from %s", self.log_path)
        return records
