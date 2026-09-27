"""worker.runtime — configure_pipeline (OrgConfig -> PipelineConfig wiring, M0)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from worker.runtime import configure_pipeline  # noqa: E402


def _pipe(**cfg):
    return SimpleNamespace(config=SimpleNamespace(**cfg))


def test_configure_pipeline_applies_org_thresholds():
    pipe = _pipe(tau_fast_high=0.75, tau_native_high=0.95, accounting_standard="IFRS")
    configure_pipeline(pipe, "NCT", {"thresholds": {"tau_fast_high": 0.6, "tau_missing": 0.1, "not_tau": 9}})
    cfg = pipe.config
    assert cfg.accounting_standard == "NCT"        # standard applied
    assert cfg.tau_fast_high == 0.6                # existing tau_ overridden
    assert cfg.tau_native_high == 0.95             # untouched default
    assert not hasattr(cfg, "tau_missing")         # absent attr not created (hasattr guard)
    assert not hasattr(cfg, "not_tau")             # non-tau key ignored


def test_configure_pipeline_none_config_is_noop_except_standard():
    pipe = _pipe(tau_fast_high=0.75, accounting_standard="IFRS")
    configure_pipeline(pipe, "NCT", None)
    assert pipe.config.accounting_standard == "NCT"
    assert pipe.config.tau_fast_high == 0.75


def test_configure_pipeline_rejects_non_numeric_threshold():
    pipe = _pipe(tau_fast_high=0.75, accounting_standard="NCT")
    configure_pipeline(pipe, "NCT", {"thresholds": {"tau_fast_high": "high"}})
    assert pipe.config.tau_fast_high == 0.75       # string ignored


# --- DLV-15: serialize_result must keep the None placeholders ---------------
class _CT:
    """Minimal CanonicalTable stand-in — serialize_result only needs ``to_dict``."""

    def __init__(self, name):
        self.name = name

    def to_dict(self):
        return {"statement_type": self.name, "rows": []}


def _result(canonical, statements=None):
    return SimpleNamespace(
        text="", tables=[], qcs_score=0.5, tier_used=1,
        confidence_details={}, canonical_tables=canonical,
        metadata={"schema_version": 2, "statements": statements or []},
    )


def test_serialize_result_keeps_none_placeholders():
    """DLV-15. From schema v2 ``canonical_tables`` is index-aligned with
    ``metadata['statements']``: a statement whose tables produced no canonical output is
    a ``None`` at its own index. Dropping it shortened the list and slid every later
    statement onto the wrong canonical table — silently, and with the wrong numbers in
    front of a reviewer."""
    from worker.runtime import serialize_result  # noqa: PLC0415

    statements = [{"id": 1, "qualified_type": "bilan_actif", "table_indices": [0]},
                  {"id": 2, "qualified_type": None, "table_indices": [1]},
                  {"id": 3, "qualified_type": "compte_resultat", "table_indices": [2]}]
    payload = serialize_result(
        _result([_CT("bilan_actif"), None, _CT("compte_resultat")], statements)
    )

    cts = payload["canonical_tables"]
    assert len(cts) == 3                                   # not 2
    assert cts[1] is None                                  # the placeholder survived
    assert cts[0]["statement_type"] == "bilan_actif"
    # and the alignment schema v2 promises actually holds
    assert len(cts) == len(payload["statements"])
    assert cts[2]["statement_type"] == payload["statements"][2]["qualified_type"]


def test_serialize_result_all_none_still_aligns():
    from worker.runtime import serialize_result  # noqa: PLC0415

    payload = serialize_result(_result([None, None], [{"id": 1}, {"id": 2}]))
    assert payload["canonical_tables"] == [None, None]
    assert payload["schema_version"] == 2


def test_serialize_has_score_kind():
    """WP-2.7 — `qcs_score` alone is not self-describing, and the stored row is
    where the API and the UI read from.

    `score_kind` says which measure the number is: a QCS, or — at Tier 3 — the
    ensemble's Q_table, which lives in the same field and is a different
    quantity on a different scale (RTE-10). `qcs_grade` is that score in the one
    grade vocabulary the pipeline uses, and `hitl_reason` is why
    `Job.hitl_required` is what it is. All three were decided upstream and
    simply never reached the row, so every consumer re-derived its own version.
    """
    from worker.runtime import serialize_result  # noqa: PLC0415

    tier1 = SimpleNamespace(
        text="", tables=[], qcs_score=0.786, tier_used=1,
        confidence_details={}, canonical_tables=[],
        metadata={
            "schema_version": 2, "statements": [],
            "score_kind": "qcs", "qcs_grade": "good",
            "hitl_required": True,
            "hitl_reason": "QCS grade is 'good' — quick review recommended",
        },
    )
    payload = serialize_result(tier1)
    assert payload["score_kind"] == "qcs"
    assert payload["qcs_grade"] == "good"
    assert "quick review" in payload["hitl_reason"]
    assert payload["qcs_score"] == 0.786

    tier3 = SimpleNamespace(
        text="", tables=[], qcs_score=1.0, tier_used=3,
        confidence_details={}, canonical_tables=[],
        metadata={"schema_version": 2, "statements": [],
                  "score_kind": "q_table", "qcs_grade": "excellent",
                  "hitl_required": False, "hitl_reason": ""},
    )
    assert serialize_result(tier3)["score_kind"] == "q_table"

    # A result produced before the keys existed must still serialize — the row
    # says "unknown" by omission rather than claiming a measure it never had.
    legacy = serialize_result(_result([]))
    assert legacy["score_kind"] is None
    assert legacy["qcs_grade"] is None
    assert legacy["hitl_reason"] is None


def test_serialize_result_empty_canonical_is_empty_list():
    from worker.runtime import serialize_result  # noqa: PLC0415

    assert serialize_result(_result([]))["canonical_tables"] == []
    assert serialize_result(_result(None))["canonical_tables"] == []
