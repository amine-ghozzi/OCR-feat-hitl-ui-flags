"""HITL review workflow — queue, validation view, cell corrections (→ feedback loop),
state machine (Draft→Validated→Archived/Rejected) with optimistic-lock 409, and audit trail.

Reads need `documents:read`; mutations need `hitl:write`. Corrections are recorded as
`Correction` rows and, when a canonical mapping is supplied, also seed an **approved**
`VariationCandidate` (closing the feedback loop into the DB). Every mutation writes an `AuditEvent`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_async_session
from api.deps import (
    actor_user_id,
    audit,
    owned_document,
    same_actor,
    tenant_org_id,
)
from api.models import (
    AuditEvent,
    Correction,
    DocState,
    Document,
    Job,
    JobStatus,
    Validation,
    VariationCandidate,
    VariationStatus,
)
from api.schemas import (
    AssignRequest,
    AuditEventRead,
    CellsPatch,
    CellsPatchResult,
    ReviewQueueItem,
    TransitionRequest,
    ValidationView,
)
from api.security import Principal, require_scopes

router = APIRouter(tags=["review"])

_ALLOWED_TRANSITIONS: dict[DocState, set[DocState]] = {
    DocState.draft: {DocState.validated, DocState.rejected},
    DocState.validated: {DocState.archived},
}


# Status precedence for the candidate UPSERT — a repeat confirmation may advance the
# status but must never downgrade it (a promoted/approved mapping stays put).
_STATUS_RANK: dict[VariationStatus, int] = {
    VariationStatus.rejected: 0,
    VariationStatus.candidate: 1,
    VariationStatus.confirmed_custom: 1,
    VariationStatus.approved: 2,
    VariationStatus.promoted: 3,
}


# --- helpers --------------------------------------------------------------


async def _upsert_variation_candidate(
    session: AsyncSession, *, org_id, raw_text: str, matched_to: str, document_id,
) -> None:
    """Idempotently record a human-confirmed mapping (closes the feedback loop).

    One row per ``(org_id, raw_text, matched_to)``: a repeat confirmation increments
    ``seen_count`` instead of inserting a duplicate, and advances status to ``approved``
    without ever downgrading a ``promoted`` mapping. The flush makes the new row visible
    to a later correction in the *same* request that targets the same triple.
    """
    existing = (await session.execute(
        select(VariationCandidate).where(
            VariationCandidate.org_id == org_id,
            VariationCandidate.raw_text == raw_text,
            VariationCandidate.matched_to == matched_to,
        )
    )).scalar_one_or_none()
    if existing is None:
        session.add(VariationCandidate(
            org_id=org_id, raw_text=raw_text, matched_to=matched_to,
            match_type="hitl_correction", confidence=1.0,
            status=VariationStatus.approved, document_id=document_id, seen_count=1,
        ))
        await session.flush()
    else:
        existing.seen_count += 1
        if _STATUS_RANK[VariationStatus.approved] > _STATUS_RANK[existing.status]:
            existing.status = VariationStatus.approved


async def _latest_done_job(session: AsyncSession, doc: Document) -> Job | None:
    return (
        await session.execute(
            select(Job).where(Job.document_id == doc.id, Job.status == JobStatus.done)
            .order_by(Job.created_at.desc())
        )
    ).scalars().first()


async def _get_or_create_validation(session: AsyncSession, doc: Document) -> Validation:
    v = (
        await session.execute(
            select(Validation).where(Validation.document_id == doc.id)
            .order_by(Validation.created_at.desc())
        )
    ).scalars().first()
    if v is not None:
        return v
    job = await _latest_done_job(session, doc)
    report = ((job.result or {}).get("metadata") or {}).get("validation_report") if job else None
    v = Validation(document_id=doc.id, org_id=doc.org_id, state=doc.state, report=report)
    session.add(v)
    await session.flush()
    return v


def _derive_flags(job_result: dict | None):
    conflicts: list = []
    flagged: list = []
    for ti, ct in enumerate((job_result or {}).get("canonical_tables") or []):
        # DLV-15: `None` marks a statement that produced no canonical output. It keeps
        # the list index-aligned with `metadata['statements']`; it carries no flags.
        if not isinstance(ct, dict):
            continue
        conflicts.extend(ct.get("conflicts") or [])
        for ri, row in enumerate(ct.get("rows") or []):
            for col, cell in (row.get("cells") or {}).items():
                if cell.get("flag") and cell["flag"] != "green":
                    flagged.append({
                        "table_index": ti, "row_index": ri, "column_name": col,
                        "flag": cell["flag"], "raw_value": cell.get("raw_value"),
                    })
    return conflicts, flagged


REVIEW_START = "review.start"


async def _emit_review_start(session: AsyncSession, principal: Principal, doc: Document) -> None:
    """UI-5: record that this actor began reviewing this document — **once** per (doc, actor).

    "Opened the file" is a distinct fact from "claimed it": `assign` and the first
    `get_validation` both start a review, and either may come first. The guard is per actor,
    not per document, so a second reviewer's start is still recorded.
    """
    prior = (await session.execute(
        select(AuditEvent).where(
            AuditEvent.action == REVIEW_START,
            AuditEvent.target_type == "document",
            AuditEvent.target_id == str(doc.id),
        )
    )).scalars().all()
    # Actor identity lives in `actor_id` for humans and in `detail` for keys, so the
    # comparison is done in Python (portable JSON filtering is not available on SQLite).
    if any(same_actor(principal, e) for e in prior):
        return
    await audit(session, principal, REVIEW_START, doc.id)


# --- endpoints ------------------------------------------------------------


@router.get("/review/queue", response_model=list[ReviewQueueItem])
async def review_queue(
    needs_review_only: bool = False,
    principal: Principal = Depends(require_scopes("documents:read")),
    session: AsyncSession = Depends(get_async_session),
) -> list[ReviewQueueItem]:
    """The review queue — RTE-1 / decision Q5-A: it lists EVERY document, poorest first.

    ``needs_review_only`` defaults to **false** on purpose. Under Q5-A neither end
    of the scale is taken on trust: there is no auto-approve and no auto-reject, so
    "this one was not flagged" is not a reason to hide it from the person whose job
    is to look. Filtering the queue down to the flagged documents would rebuild, in
    the UI, exactly the silent auto-accept that RTE-1 found in the cascade — and it
    would hide it behind a default nobody sets.

    Pass ``?needs_review_only=true`` to narrow it to the flagged ones. That is a
    reviewer's choice for a working session, never the system's decision about
    which documents deserve a human.

    Order: lowest score first, unscored documents ahead of scored ones (nothing is
    known about them, which is the strongest reason to look), newest first within
    a tie. The worst thing in the queue is the first thing on the screen.
    """
    # Unconditional tenant filter (SEC-2): no principal reviews another org's queue.
    stmt = select(Document).where(
        Document.state == DocState.draft, Document.org_id == tenant_org_id(principal)
    )
    docs = (await session.execute(stmt.order_by(Document.created_at.desc()))).scalars().all()
    items: list[ReviewQueueItem] = []
    for doc in docs:
        job = (
            await session.execute(
                select(Job).where(Job.document_id == doc.id).order_by(Job.created_at.desc())
            )
        ).scalars().first()
        item = ReviewQueueItem.model_validate(doc)
        if job is not None:
            item.hitl_required = bool(job.hitl_required)
            item.qcs_score = job.qcs_score
        if needs_review_only and not item.hitl_required:
            continue
        items.append(item)
    # Poor first. `-1.0` for a missing score sorts it ahead of every real one.
    items.sort(key=lambda i: (i.qcs_score if i.qcs_score is not None else -1.0))
    return items


@router.get("/documents/{document_id}/validation", response_model=ValidationView)
async def get_validation(
    document_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("documents:read")),
    session: AsyncSession = Depends(get_async_session),
) -> ValidationView:
    doc = await owned_document(session, principal, document_id)
    v = await _get_or_create_validation(session, doc)
    job = await _latest_done_job(session, doc)
    conflicts, flagged = _derive_flags(job.result if job else None)
    await _emit_review_start(session, principal, doc)   # UI-5
    await session.commit()
    return ValidationView(
        document_id=doc.id, state=v.state.value, version_id=v.version_id,
        assignee_id=v.assignee_id, report=v.report, conflicts=conflicts, flagged_cells=flagged,
    )


@router.post("/documents/{document_id}/assign", response_model=ValidationView)
async def assign(
    document_id: uuid.UUID,
    body: AssignRequest,
    principal: Principal = Depends(require_scopes("hitl:write")),
    session: AsyncSession = Depends(get_async_session),
) -> ValidationView:
    doc = await owned_document(session, principal, document_id)
    v = await _get_or_create_validation(session, doc)
    v.assignee_id = body.assignee_id or actor_user_id(principal)
    await _emit_review_start(session, principal, doc)   # UI-5
    await audit(session, principal, "review.assign", doc.id, {"assignee_id": str(v.assignee_id)})
    await session.commit()
    return ValidationView(document_id=doc.id, state=v.state.value, version_id=v.version_id,
                          assignee_id=v.assignee_id, report=v.report)


@router.patch("/documents/{document_id}/cells", response_model=CellsPatchResult)
async def patch_cells(
    document_id: uuid.UUID,
    body: CellsPatch,
    if_match: int | None = Header(default=None, alias="If-Match"),
    principal: Principal = Depends(require_scopes("hitl:write")),
    session: AsyncSession = Depends(get_async_session),
) -> CellsPatchResult:
    doc = await owned_document(session, principal, document_id)
    v = await _get_or_create_validation(session, doc)
    if if_match is not None and if_match != v.version_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Stale validation version (current {v.version_id})")

    var_count = 0
    for corr in body.corrections:
        session.add(Correction(
            document_id=doc.id, org_id=doc.org_id, user_id=actor_user_id(principal),
            table_index=corr.table_index, row_index=corr.row_index, column_name=corr.column_name,
            raw_text=corr.raw_text, old_value=corr.old_value, new_value=corr.new_value,
            corrected_canonical=corr.corrected_canonical,
        ))
        # Feedback loop: a human-confirmed label mapping → approved variation candidate
        # (idempotent UPSERT — repeat confirmations increment seen_count, not new rows).
        if corr.corrected_canonical and corr.raw_text:
            await _upsert_variation_candidate(
                session, org_id=doc.org_id, raw_text=corr.raw_text,
                matched_to=corr.corrected_canonical, document_id=doc.id,
            )
            var_count += 1

    # Cells are append-only Correction rows; the Validation row isn't mutated here, so its
    # version_id (managed by version_id_col, bumped on transition) stays stable.
    await audit(session, principal, "review.cells", doc.id, {"count": len(body.corrections)})
    await session.commit()
    return CellsPatchResult(applied=len(body.corrections), variation_candidates=var_count,
                            validation_version=v.version_id)


@router.post("/documents/{document_id}/transition", response_model=ValidationView)
async def transition(
    document_id: uuid.UUID,
    body: TransitionRequest,
    if_match: int | None = Header(default=None, alias="If-Match"),
    principal: Principal = Depends(require_scopes("hitl:write")),
    session: AsyncSession = Depends(get_async_session),
) -> ValidationView:
    doc = await owned_document(session, principal, document_id)
    v = await _get_or_create_validation(session, doc)

    try:
        target = DocState(body.to_state)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Unknown state {body.to_state!r}")
    if target not in _ALLOWED_TRANSITIONS.get(doc.state, set()):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Illegal transition {doc.state.value} → {target.value}",
        )
    if if_match is not None and if_match != v.version_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Stale validation version (current {v.version_id})")

    v.state = target  # dirty → ORM version_id_col bumps + guards via WHERE version
    doc.state = target
    await audit(session, principal, "review.transition", doc.id,
                {"to": target.value, "reason": body.reason})
    try:
        await session.commit()
    except StaleDataError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Concurrent modification; reload and retry")
    return ValidationView(document_id=doc.id, state=v.state.value, version_id=v.version_id,
                          assignee_id=v.assignee_id, report=v.report)


@router.get("/documents/{document_id}/audit", response_model=list[AuditEventRead])
async def get_audit(
    document_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("documents:read")),
    session: AsyncSession = Depends(get_async_session),
) -> list[AuditEventRead]:
    doc = await owned_document(session, principal, document_id)
    events = (
        await session.execute(
            select(AuditEvent).where(
                AuditEvent.target_type == "document", AuditEvent.target_id == str(doc.id)
            ).order_by(AuditEvent.created_at.asc())
        )
    ).scalars().all()
    return [AuditEventRead.model_validate(e) for e in events]
