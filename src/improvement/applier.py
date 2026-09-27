"""Apply reversible remediations behind the regression gate, with rollback.

Reversible + auto-appliable: **config_tune** (per-org thresholds in `OrgConfig`, which the
worker now reads via `runtime.configure_pipeline`). Durable/code actions
(`variation_promotion`, `code_fix_proposal`) are **propose-only** here — surfaced as a
plan/diff for human review, per the locked autonomy decision.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from api.models import OrgConfig
from improvement import regression_gate

REVERSIBLE_ACTIONS = {"config_tune"}


def build_proposal(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Map a detected issue to a remediation proposal (pure)."""
    kind = issue.get("kind")
    if kind == "qualifier_mismatch":
        ev = issue.get("evidence") or {}
        return {
            "action_type": "code_fix_proposal",
            "reversible": False,
            "rationale": "A meaning-flipping token produced a wrong canonical mapping; fix in "
                         "the nomenclature/matcher. Propose-only (code change).",
            "proposal": {
                "file": "src/accounting/rules/nomenclature_data.yaml",
                "raw_text": ev.get("raw_text"), "wrong_match": ev.get("matched_to"),
                "change": "Add the correct canonical entry/variation; the qualifier guard keeps "
                          "residual/total siblings distinct.",
            },
        }
    if kind == "candidate_duplication":
        return {
            "action_type": "ops_note", "reversible": False,
            "rationale": "Duplication is structurally prevented (in-write dedup + DB unique "
                         "constraint); informational only.",
            "proposal": {"note": "No code change required; verify the candidate log."},
        }
    return {"action_type": "unknown", "reversible": False, "rationale": "No mapping.", "proposal": {}}


async def apply_config_tune(
    session: AsyncSession, org_id, new_config: Dict[str, Any], *,
    run_tests: bool = False, candidate_log: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a per-org config delta behind the gate. Returns
    `{applied, before, after, gate_result}`. The change is committed only if the gate passes."""
    cfg = await session.get(OrgConfig, org_id)
    before = dict(cfg.config) if cfg and cfg.config else {}

    gate = regression_gate.run_gate(run_tests=run_tests, candidate_log=candidate_log)
    if not gate["passed"]:
        return {"applied": False, "before": before, "after": None, "gate_result": gate}

    if cfg is None:
        cfg = OrgConfig(org_id=org_id, config=new_config)
        session.add(cfg)
    else:
        cfg.config = new_config
    await session.flush()
    return {"applied": True, "before": before, "after": new_config, "gate_result": gate}


async def rollback_config(session: AsyncSession, org_id, before: Optional[Dict[str, Any]]) -> None:
    cfg = await session.get(OrgConfig, org_id)
    if cfg is not None:
        cfg.config = before or {}
        await session.flush()
