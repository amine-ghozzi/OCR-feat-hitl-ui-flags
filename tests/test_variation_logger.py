"""VariationLogger — dedup (Layer 1) + distinct-document promotion.

Covers the proactive duplication fixes: a `(raw, matched, document, status)`
observation is logged at most once (even across a fresh logger instance pointed at
the same file), and promotion counts *distinct documents*, not raw log lines.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from accounting.variation_logger import (  # noqa: E402
    ENV_VAR,
    VariationLogger,
    resolve_log_path,
)


def _lines(path: Path) -> int:
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())


def test_same_observation_logged_once_within_instance(tmp_path):
    log = tmp_path / "cands.jsonl"
    vl = VariationLogger(log_path=log)
    for _ in range(50):  # same label re-matched across stages within one run
        vl.log_variation_candidate("PRODUITS D'EXPLOITATION", "Autres produits d'exploitation",
                                    match_type="fuzzy", confidence=0.87, document_id="doc-1")
    assert _lines(log) == 1


def test_dedup_persists_across_a_fresh_instance(tmp_path):
    """A re-run (new VariationLogger over the same file) must not re-append."""
    log = tmp_path / "cands.jsonl"
    VariationLogger(log_path=log).log_variation_candidate(
        "Réserve", "Réserves", match_type="fuzzy", confidence=0.93, document_id="doc-1")
    assert _lines(log) == 1
    # New instance preloads _seen from the file → the duplicate is skipped.
    VariationLogger(log_path=log).log_variation_candidate(
        "Réserve", "Réserves", match_type="fuzzy", confidence=0.93, document_id="doc-1")
    assert _lines(log) == 1


def test_distinct_documents_are_logged_separately(tmp_path):
    log = tmp_path / "cands.jsonl"
    vl = VariationLogger(log_path=log)
    for doc in ("doc-1", "doc-2", "doc-3"):
        vl.log_variation_candidate("Placement", "Placements", match_type="fuzzy",
                                   confidence=0.9, document_id=doc)
    assert _lines(log) == 3  # different document_id → different observation


def test_promotion_counts_distinct_documents(tmp_path):
    log = tmp_path / "cands.jsonl"
    vl = VariationLogger(log_path=log)
    for doc in ("d1", "d2", "d3"):
        vl.log_variation_candidate("CA", "Revenus", match_type="fuzzy",
                                   confidence=0.9, document_id=doc)
    promotable = vl.get_promotable_candidates(min_count=3)
    assert any(p["raw_text"] == "CA" and p["count"] == 3 and p["consistent"] for p in promotable)


def test_duplicated_single_document_does_not_promote(tmp_path):
    """162x the same pair from ONE document must not reach the promotion threshold."""
    log = tmp_path / "cands.jsonl"
    vl = VariationLogger(log_path=log)
    for _ in range(162):
        vl.log_variation_candidate("CA", "Revenus", match_type="fuzzy",
                                   confidence=0.9, document_id="only-doc")
    assert _lines(log) == 1  # deduped to one line
    assert vl.get_promotable_candidates(min_count=3) == []  # 1 distinct doc < 3


def test_empty_document_id_does_not_drive_promotion(tmp_path):
    """Empty document_id (local runs) carries no evidence, even logged many times."""
    log = tmp_path / "cands.jsonl"
    vl = VariationLogger(log_path=log)
    # Distinct labels keep the dedup from collapsing them; all have empty document_id.
    for i in range(5):
        vl.log_variation_candidate(f"LABEL {i}", "Revenus", match_type="fuzzy", confidence=0.9)
    assert vl.get_promotable_candidates(min_count=1) == []


def _tiny_nomenclature(path):
    import yaml
    path.write_text(
        yaml.safe_dump({
            "meta": {"standard": "NCT"},
            "entries": [{"canonical_term": "Revenus", "normalized": "revenus", "variations": ["ca"]}],
        }, allow_unicode=True),
        encoding="utf-8",
    )


def test_promote_variations_dry_run_then_apply_idempotent(tmp_path):
    import yaml
    nom = tmp_path / "nom.yaml"
    _tiny_nomenclature(nom)
    vl = VariationLogger(log_path=tmp_path / "c.jsonl")
    for doc in ("d1", "d2", "d3"):  # 3 distinct docs -> promotable, consistent
        vl.log_variation_candidate("Chiffre d'affaires net", "Revenus",
                                   match_type="fuzzy", confidence=0.9, document_id=doc)

    # dry-run: plan only, nothing written
    plan = vl.promote_variations(min_count=3, dry_run=True, nomenclature_path=nom)
    assert any(p["canonical_term"] == "Revenus" and p["applied"] is False
               and p["reason"] == "promote" for p in plan)
    assert "Chiffre d'affaires net" not in yaml.safe_load(nom.read_text(encoding="utf-8"))["entries"][0]["variations"]

    # apply: written into the canonical entry's variations
    vl.promote_variations(min_count=3, dry_run=False, nomenclature_path=nom)
    assert "Chiffre d'affaires net" in yaml.safe_load(nom.read_text(encoding="utf-8"))["entries"][0]["variations"]

    # idempotent: re-running finds it already present (no duplicate)
    plan3 = vl.promote_variations(min_count=3, dry_run=False, nomenclature_path=nom)
    assert all(p["reason"] == "already present" for p in plan3 if p["canonical_term"] == "Revenus")
    vars_now = yaml.safe_load(nom.read_text(encoding="utf-8"))["entries"][0]["variations"]
    assert vars_now.count("Chiffre d'affaires net") == 1


def test_promote_skips_unknown_canonical(tmp_path):
    nom = tmp_path / "nom.yaml"
    _tiny_nomenclature(nom)
    vl = VariationLogger(log_path=tmp_path / "c.jsonl")
    for doc in ("d1", "d2", "d3"):
        vl.log_variation_candidate("Mystery line", "Nonexistent Term",
                                   match_type="fuzzy", confidence=0.9, document_id=doc)
    plan = vl.promote_variations(min_count=3, dry_run=False, nomenclature_path=nom)
    assert any(p["reason"] == "canonical_term not in nomenclature" for p in plan)


# ----------------------------------------------------------------------------
# GOV-4 — log path resolution: explicit arg > FINALZE_VARIATION_LOG > per-tenant
# ----------------------------------------------------------------------------


def test_env_override_wins_over_default(tmp_path, monkeypatch):
    """With the env var set, a bare VariationLogger() uses it — not the repo default."""
    env_log = tmp_path / "from_env.jsonl"
    monkeypatch.setenv(ENV_VAR, str(env_log))
    assert VariationLogger().log_path == env_log
    # and it is genuinely written there
    VariationLogger().log_variation_candidate(
        "PRODUITS", "Autres produits", match_type="fuzzy", confidence=0.8, document_id="d1")
    assert env_log.exists() and _lines(env_log) == 1


def test_org_id_default_path(monkeypatch):
    """No env var: the default is per-tenant, under the untracked directory."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    path = VariationLogger(org_id="acme").log_path
    assert path.parent.name == "variation_candidates"
    assert path.as_posix().endswith("data/variation_candidates/acme.jsonl")
    # no org id → the process-wide "default" tenant (Q5-RT: no active loop)
    assert VariationLogger().log_path.name == "default.jsonl"


def test_placeholder_substitution(tmp_path, monkeypatch):
    """A literal {org_id} in the env value is substituted with the tenant (or default)."""
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "{org_id}.jsonl"))
    assert VariationLogger(org_id="acme").log_path == tmp_path / "acme.jsonl"
    assert VariationLogger().log_path == tmp_path / "default.jsonl"
    # a tenant id that is not a safe path segment cannot escape the directory
    assert resolve_log_path("../../etc").parent == resolve_log_path("x").parent


def test_explicit_log_path_still_wins(tmp_path, monkeypatch):
    """An explicit log_path= beats both the env var and the per-tenant default."""
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "from_env.jsonl"))
    explicit = tmp_path / "explicit.jsonl"
    assert VariationLogger(log_path=explicit).log_path == explicit
    assert VariationLogger(log_path=explicit, org_id="acme").log_path == explicit
