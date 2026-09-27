"""Feedback loop — variation promotion, rule-flag stats, quality KPIs, ground-truth export.

This is what makes corrections *compound* and powers the foundation-first accuracy gate.
Reads/writes need `feedback:write`.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from accounting.candidate_audit import audit_records, qualifier_mismatches
from api.db import get_async_session
from api.deps import Pagination, audit, pagination, tenant_org_id
from api.models import (
    Correction,
    Document,
    Job,
    JobStatus,
    VariationCandidate,
    VariationStatus,
)
from api.schemas import (
    QualityKPIs,
    RuleFlagStats,
    VariationCandidateRead,
)
from api.security import Principal, require_scopes

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.get("/variations", response_model=list[VariationCandidateRead])
async def list_variations(
    status_filter: str | None = Query(default=None, alias="status"),
    principal: Principal = Depends(require_scopes("feedback:write")),
    session: AsyncSession = Depends(get_async_session),
    pg: Pagination = Depends(pagination),
) -> list[VariationCandidateRead]:
    stmt = select(VariationCandidate).where(
        VariationCandidate.org_id == tenant_org_id(principal)
    )
    if status_filter:
        stmt = stmt.where(VariationCandidate.status == VariationStatus(status_filter))
    rows = (
        await session.execute(
            stmt.order_by(VariationCandidate.created_at.desc()).limit(pg.limit).offset(pg.offset)
        )
    ).scalars().all()
    return [VariationCandidateRead.model_validate(r) for r in rows]


async def _get_owned_candidate(session, cand_id, principal) -> VariationCandidate:
    """Fetch a candidate owned by the caller's org, or 404.

    The org match is required, not best-effort: a **null-org** candidate (bulk-imported local
    data, `models.VariationCandidate` docstring) belongs to no tenant and must not be
    approvable/rejectable through a tenant's credentials (SEC-2).
    """
    org = tenant_org_id(principal)
    cand = await session.get(VariationCandidate, cand_id)
    if cand is None or cand.org_id != org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Variation candidate not found")
    return cand


@router.post("/variations/{cand_id}/approve", response_model=VariationCandidateRead)
async def approve_variation(
    cand_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("feedback:write")),
    session: AsyncSession = Depends(get_async_session),
) -> VariationCandidateRead:
    cand = await _get_owned_candidate(session, cand_id, principal)
    cand.status = VariationStatus.promoted
    # Bridge into the pipeline's hot-reload store so future extractions benefit (best-effort).
    try:
        from accounting.variation_logger import VariationLogger  # noqa: PLC0415

        if cand.matched_to:
            # Per-tenant learning log (GOV-4): the correction belongs to the caller's org.
            VariationLogger(org_id=principal.org_id).log_hitl_correction(
                cand.raw_text, cand.matched_to
            )
    except Exception:  # noqa: BLE001 — logger is best-effort
        pass
    await audit(session, principal, "feedback.variation_approve", None,
                {"raw_text": cand.raw_text, "matched_to": cand.matched_to},
                target_type="variation_candidate", target_id=str(cand.id))
    await session.commit()
    return VariationCandidateRead.model_validate(cand)


@router.post("/variations/{cand_id}/reject", response_model=VariationCandidateRead)
async def reject_variation(
    cand_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("feedback:write")),
    session: AsyncSession = Depends(get_async_session),
) -> VariationCandidateRead:
    cand = await _get_owned_candidate(session, cand_id, principal)
    cand.status = VariationStatus.rejected
    await audit(session, principal, "feedback.variation_reject", None,
                {"raw_text": cand.raw_text, "matched_to": cand.matched_to},
                target_type="variation_candidate", target_id=str(cand.id))
    await session.commit()
    return VariationCandidateRead.model_validate(cand)


@router.get("/rules/flags", response_model=RuleFlagStats)
async def rule_flags(
    principal: Principal = Depends(require_scopes("feedback:write")),
    session: AsyncSession = Depends(get_async_session),
) -> RuleFlagStats:
    stmt = select(Job).where(
        Job.status == JobStatus.done, Job.org_id == tenant_org_id(principal)
    )
    jobs = (await session.execute(stmt)).scalars().all()
    rates: list[float] = []
    crit = 0
    for j in jobs:
        rep = ((j.result or {}).get("metadata") or {}).get("validation_report") or {}
        total, passed = rep.get("total_checks"), rep.get("passed_checks")
        # VAL-5 — over the checks that RAN. A skipped check is neither a pass nor
        # a fail, and dividing by `total_checks` counted every skip as a silent
        # failure: this is a RULE-HEALTH figure, and it was making rules that
        # could not run on a document look like rules that got it wrong. Same
        # denominator fix as `qcs.calculator.calculate_semantic_score`; a report
        # written before `skipped_checks` existed reads 0 and is unchanged.
        ran = (total or 0) - (rep.get("skipped_checks") or 0)
        if ran > 0:
            rates.append((passed or 0) / ran)
        if rep.get("critical_failures"):
            crit += 1
    return RuleFlagStats(
        jobs_evaluated=len(jobs),
        avg_pass_rate=(sum(rates) / len(rates)) if rates else None,
        critical_failure_rate=(crit / len(jobs)) if jobs else 0.0,
    )


@router.get("/quality", response_model=QualityKPIs)
async def quality(
    principal: Principal = Depends(require_scopes("feedback:write")),
    session: AsyncSession = Depends(get_async_session),
) -> QualityKPIs:
    org = tenant_org_id(principal)
    jstmt = select(Job).where(Job.org_id == org)
    jobs = (await session.execute(jstmt)).scalars().all()
    done = [j for j in jobs if j.status == JobStatus.done]
    hitl = [j for j in done if j.hitl_required]
    qcs = [j.qcs_score for j in done if j.qcs_score is not None]

    def _count(model):
        return select(func.count()).select_from(model).where(model.org_id == org)

    corrections_total = (await session.execute(_count(Correction))).scalar_one()
    docs_total = (await session.execute(_count(Document))).scalar_one()

    # Variation-candidate health (Layer 3) via the shared analyzer.
    cstmt = select(VariationCandidate).where(VariationCandidate.org_id == org)
    cands = (await session.execute(cstmt)).scalars().all()
    crecords = [
        {"raw_text": c.raw_text, "matched_to": c.matched_to,
         "document_id": str(c.document_id) if c.document_id else ""}
        for c in cands
    ]
    caudit = audit_records(crecords)

    return QualityKPIs(
        jobs_total=len(jobs), jobs_done=len(done),
        # RTE-2 — read this as "the share of finished jobs NOBODY FLAGGED for a
        # look", not "the share that came out right". It counts `hitl_required`,
        # which is a routing decision, and says nothing about whether the numbers
        # in an unflagged document are correct — no ground truth is consulted
        # here. It moved sharply when WP-2.7 plumbed the Tier-1 accept path
        # (documents scoring 0.75–0.90 are accepted by the cascade AND flagged
        # for review, and used to be counted as automated); that is the metric
        # becoming honest, not automation regressing.
        automation_rate=((len(done) - len(hitl)) / len(done)) if done else 0.0,
        hitl_rate=(len(hitl) / len(done)) if done else 0.0,
        avg_qcs=(sum(qcs) / len(qcs)) if qcs else None,
        corrections_total=corrections_total,
        correction_rate=(corrections_total / docs_total) if docs_total else 0.0,
        candidate_duplication_ratio=caudit["duplication_ratio"],
        candidate_empty_document_id_pct=caudit["empty_document_id_pct"],
        candidate_qualifier_mismatches=len(qualifier_mismatches(crecords)),
    )


@router.get("/ground-truth")
async def ground_truth(
    principal: Principal = Depends(require_scopes("feedback:write")),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """Export HITL corrections as a JSONL labeled dataset (golden-set / calibration source)."""
    org = tenant_org_id(principal)
    stmt = select(Correction).where(Correction.org_id == org)
    rows = (await session.execute(stmt.order_by(Correction.created_at.asc()))).scalars().all()
    lines = [
        json.dumps(
            {
                "document_id": str(c.document_id),
                "table_index": c.table_index,
                "row_index": c.row_index,
                "column_name": c.column_name,
                "raw_text": c.raw_text,
                "corrected_value": c.new_value,
                "corrected_canonical": c.corrected_canonical,
            },
            ensure_ascii=False,
        )
        for c in rows
    ]
    # SEC-12: bulk export of labelled financial data is an auditable event.
    await audit(session, principal, "feedback.export", None, {"rows": len(rows)},
                target_type="export", target_id="ground-truth")
    await session.commit()
    return Response("\n".join(lines), media_type="application/x-ndjson")
