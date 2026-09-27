"""Stage 1 — sample: select a stratified document sample -> runs/<id>/_sample.json.

Local source (default): enumerate the curated `Samples/` corpus, exclude fixtures,
infer the statement type from the filename, and round-robin across types so a small
`--limit` still spans strata. DB source is deferred. `tier`/`qcs_band` are filled in by
the `cases` stage (unknown until the pipeline runs).
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from . import config as C

_SUPPORTED = {".pdf", ".docx", ".jpg", ".jpeg", ".png"}


def _statement_guess(name: str) -> str:
    n = name.lower()
    if "bilan" in n:
        return "bilan"
    if "resultat" in n or "résultat" in n or "etat resultat" in n or n.startswith("cr"):
        return "compte_resultat"
    if "flux" in n or "tft" in n or "tresorerie" in n or "trésorerie" in n:
        return "flux_tresorerie"
    return "unknown"


def _sample_local(limit: int) -> List[Dict[str, Any]]:
    docs: List[Path] = []
    if C.SAMPLES_DIR.exists():
        for p in sorted(C.SAMPLES_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in _SUPPORTED and not C.is_fixture(p.name):
                docs.append(p)
    # Round-robin by statement type so a small limit still spans strata.
    by_type: Dict[str, List[Path]] = {}
    for p in docs:
        by_type.setdefault(_statement_guess(p.name), []).append(p)
    ordered: List[Path] = []
    while any(by_type.values()):
        for t in list(by_type.keys()):
            if by_type[t]:
                ordered.append(by_type[t].pop(0))
    selected = ordered[:limit]
    return [
        {
            "case_id": C.case_id(p.name),
            "source_path": str(p.relative_to(C.REPO_ROOT)).replace("\\", "/"),
            "statement_type": _statement_guess(p.name),
            "standard": "NCT",          # default for the local corpus; override per case if known
            "tier": None,               # filled by the cases stage
            "qcs_band": None,           # filled by the cases stage
        }
        for p in selected
    ]


def _sample_db(limit: int) -> List[Dict[str, Any]]:
    # Deferred: pull stored Job results (Documents with a done Job) from the API DB,
    # stratify by statement_type × standard × tier × QCS band, exclude fixtures.
    raise NotImplementedError("db source not yet implemented; use --source local")


def run(run_id: str, limit: int = 12, source: str = "local") -> Dict[str, Any]:
    cases = _sample_db(limit) if source == "db" else _sample_local(limit)
    C.write_json(C.run_dir(run_id) / "_sample.json", cases)
    strata = Counter((c["statement_type"], c["standard"]) for c in cases)
    return {
        "run": run_id,
        "source": source,
        "count": len(cases),
        "strata": {f"{st}/{std}": n for (st, std), n in sorted(strata.items())},
        "sample_file": str(C.run_dir(run_id) / "_sample.json"),
    }
