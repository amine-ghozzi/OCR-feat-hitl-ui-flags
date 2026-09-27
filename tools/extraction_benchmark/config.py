"""Shared config + helpers for the extraction_benchmark harness.

Path / run-dir / artifact-IO plumbing used by every stage, plus the stratification
dimensions and test-fixture exclusion. Dependency-light (stdlib only) so the
deterministic stages import without pulling the pipeline or the API.

Modeled on leads_qualification/tools/skill_benchmark/config.py, adapted for FinAlze:
no Mongo — cases come from stored Job results (DB) or local `Samples/` + `output/`.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

# --- Repo layout -----------------------------------------------------------
# tools/extraction_benchmark/config.py → tools/ → <repo root>
_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parents[2]
SRC_ROOT = REPO_ROOT / "src"
SAMPLES_DIR = REPO_ROOT / "Samples"
OUTPUT_DIR = REPO_ROOT / "output"


def ensure_src_on_path() -> None:
    """Put `src/` on sys.path so stages can import the pipeline / accounting modules.

    Stages that touch `FinAlzePipeline`, `nomenclature`, `rule_engine`, etc. call this
    first. Kept explicit (not import-time) so importing `config` stays dependency-light.
    """
    s = str(SRC_ROOT)
    if s not in sys.path:
        sys.path.insert(0, s)


# --- Stratification dimensions (FinAlze) -----------------------------------
# A benchmark sample is stratified across these so the score reflects the real
# spread of documents, not just the easy ones.
STANDARDS = ["IFRS", "NCT", "SYSCOHADA"]
STATEMENT_TYPES = ["bilan", "compte_resultat", "flux_tresorerie"]
TIERS = [0, 1, 2, 3]
# QCS bands — gate-mapped intrinsic score buckets used for stratification + display.
QCS_BANDS = {"low": (0.0, 0.5), "medium": (0.5, 0.75), "high": (0.75, 1.0)}


def qcs_band(score: Optional[float]) -> str:
    if score is None:
        return "unknown"
    for name, (lo, hi) in QCS_BANDS.items():
        if lo <= score < hi or (name == "high" and score >= hi):
            return name
    return "unknown"


# --- Test / fixture exclusion ----------------------------------------------
# Benchmark on real documents only — never on smoke/test fixtures. Conservative:
# a single marker hit excludes the case. Markers are deliberately narrow so they
# don't false-exclude the curated `Samples/` corpus (note: callers pass the file
# NAME, not the full path, so the `Samples/` directory itself never matches).
_FIXTURE_MARKERS = ("smoke", "fixture", "dummy", "__test", "mock")


def is_fixture(*text: str) -> bool:
    blob = " ".join(str(t) for t in text if t).lower()
    return any(m in blob for m in _FIXTURE_MARKERS)


# --- Public / non-public documents (GOV-3) ---------------------------------
# The corpus manifest — never a filename heuristic — is the authority on which
# documents an external-AI stage may be pointed at. Five of the sixteen corpus
# documents are non-public client financial statements (decision G-2).


def manifest_path() -> Path:
    """`<repo root>/tools/golden/corpus.yaml` — the public/non-public authority.

    Resolved at CALL time (not import time) so a test, or a caller that repoints
    `REPO_ROOT`, gets the manifest that belongs to the tree it is working in.
    """
    return REPO_ROOT / "tools" / "golden" / "corpus.yaml"


def is_public(case_id: str, manifest: Optional[Path] = None) -> bool:
    """True only when the manifest lists `case_id` with `public: true`.

    **Fails closed.** An id the manifest does not list, a missing or unreadable
    manifest, and a manifest whose parser will not load all answer False: a case
    that is not provably public is treated as non-public (GOV-3). Parsing lives in
    `golden.load_corpus` — imported lazily so `config` stays dependency-light.
    """
    path = Path(manifest) if manifest is not None else manifest_path()
    try:
        from . import golden  # noqa: PLC0415 — lazy: pulls PyYAML
        return golden.is_public(case_id, golden.load_corpus(path))
    except Exception:  # noqa: BLE001 — unreadable manifest is not a public manifest
        return False


REFUSAL_NON_PUBLIC = "non-public (GOV-3)"
REFUSAL_UNKNOWN = "not in tools/golden/corpus.yaml (GOV-3, fail closed)"


def _manifest_ids(manifest: Optional[Path] = None) -> set:
    """The doc_ids the manifest lists — used only to word the refusal reason.

    Fails closed to the empty set, in which case every refusal reads as "not listed".
    """
    path = Path(manifest) if manifest is not None else manifest_path()
    try:
        from . import golden  # noqa: PLC0415 — lazy: pulls PyYAML
        return {d["doc_id"] for d in golden.load_corpus(path)}
    except Exception:  # noqa: BLE001
        return set()


def refusal_reason(case_id: str, manifest: Optional[Path] = None) -> Optional[str]:
    """Why an external-AI stage must refuse `case_id` — None when it may proceed.

    The single wording authority for the `reference` and `judge` gates. The *decision*
    is `is_public`; the manifest id set only chooses between "listed as non-public" and
    "not listed at all".
    """
    if is_public(case_id, manifest):
        return None
    return REFUSAL_NON_PUBLIC if case_id in _manifest_ids(manifest) else REFUSAL_UNKNOWN


# --- Run-dir / artifact IO -------------------------------------------------
RUNS_ROOT = _THIS.parent / "runs"


def run_dir(run_id: str) -> Path:
    return RUNS_ROOT / run_id


def stage_dir(run_id: str, stage: str) -> Path:
    """e.g. runs/<id>/cases/ — created on access."""
    d = run_dir(run_id) / stage
    d.mkdir(parents=True, exist_ok=True)
    return d


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def case_id(name: str) -> str:
    """Stable, filesystem-safe case id from a document name/path."""
    stem = Path(name).name
    return _SLUG_RE.sub("_", stem).strip("_") or "case"


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic serialization (sorted keys, stable separators) — keeps agent
    # prompt prefixes byte-stable so the prompt cache stays warm across cases.
    path.write_text(
        json.dumps(obj, indent=2, sort_keys=True, default=str, ensure_ascii=False),
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def iter_json(folder: Path) -> Iterable[Dict[str, Any]]:
    """Yield every *.json in folder EXCEPT manifest/task files (leading `_` or
    `.manifest.`/`.det.` infixes), so case iteration isn't polluted."""
    if not folder.exists():
        return
    for p in sorted(folder.glob("*.json")):
        if p.name.startswith("_") or ".manifest." in p.name or ".det." in p.name:
            continue
        yield read_json(p)


def iter_json_glob(folder: Path, pattern: str) -> Iterable[Dict[str, Any]]:
    if not folder.exists():
        return
    for p in sorted(folder.glob(pattern)):
        yield read_json(p)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
