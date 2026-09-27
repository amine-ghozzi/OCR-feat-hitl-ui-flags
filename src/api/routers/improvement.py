"""Self-improving control & correction loop — on-demand (dev/build-time).

`POST /sweep` detects issues on the current build; `GET /issues` lists them; `.../proposal`
maps an issue to a remediation; `.../apply` applies a REVERSIBLE fix (config_tune) behind
the regression gate and records a `RemediationAction` (before/after) for rollback; code/
durable actions are propose-only. All mutations require `admin` and write an `AuditEvent`.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# improvement/signals etc. live under src/ alongside `accounting`; ensure importable.
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from api.db import get_async_session
from api.deps import Pagination, audit, pagination, require_user_principal, tenant_org_id
from api.models import (
    Issue,
    IssueStatus,
    RemediationAction,
    RemediationStatus,
)
from api.schemas import (
    ApplyRequest,
    IssueRead,
    ProposalRead,
    RemediationActionRead,
    SweepResult,
)
from api.security import Principal, require_scopes
from improvement import applier, signals

router = APIRouter(prefix="/improvement", tags=["improvement"])


async def _audit(session, principal, action, target_id, detail):
    """Thin adapter onto the shared helper (`deps.audit`) — improvement targets are not documents."""
    await audit(session, principal, action, detail=detail,
                target_type="improvement", target_id=target_id)


@router.post("/sweep", response_model=SweepResult)
async def sweep(
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> SweepResult:
    """Detect issues on the current build; create new Issue rows (deduped by fingerprint)."""
    # The loop inspects and mutates the build itself — a machine credential must not drive it.
    require_user_principal(principal)
    org = tenant_org_id(principal)
    # Dedupe within the caller's org only — another tenant's fingerprint must not suppress ours.
    existing = set((await session.execute(
        select(Issue.fingerprint).where(
            Issue.org_id == org, Issue.status != IssueStatus.dismissed
        )
    )).scalars())
    detected = signals.detect_issues()
    created: list[Issue] = []
    for d in detected:
        if d["fingerprint"] in existing:
            continue
        row = Issue(org_id=org, kind=d["kind"], severity=d["severity"], title=d["title"],
                    evidence=d.get("evidence"), fingerprint=d["fingerprint"], status=IssueStatus.open)
        session.add(row)
        created.append(row)
    await _audit(session, principal, "improvement.sweep", "sweep",
                 {"detected": len(detected), "created": len(created)})
    await session.commit()
    for r in created:
        await session.refresh(r)
    return SweepResult(detected=len(detected), created=len(created),
                       issues=[IssueRead.model_validate(r) for r in created])


@router.get("/issues", response_model=list[IssueRead])
async def list_issues(
    status_filter: str | None = Query(default=None, alias="status"),
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
    pg: Pagination = Depends(pagination),
) -> list[IssueRead]:
    stmt = (
        select(Issue)
        .where(Issue.org_id == tenant_org_id(principal))
        .order_by(Issue.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(Issue.status == IssueStatus(status_filter))
    rows = (await session.execute(stmt.limit(pg.limit).offset(pg.offset))).scalars().all()
    return [IssueRead.model_validate(r) for r in rows]


async def _get_issue(session, principal, issue_id) -> Issue:
    """Fetch an issue owned by the caller's org, or 404 (SEC-2: no cross-tenant id probing).

    The org is resolved *before* the lookup: for an org-less principal the 403 must not depend
    on whether the id happens to exist.
    """
    org = tenant_org_id(principal)
    issue = await session.get(Issue, issue_id)
    if issue is None or issue.org_id != org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Issue not found")
    return issue


@router.get("/issues/{issue_id}/proposal", response_model=ProposalRead)
async def proposal(
    issue_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> ProposalRead:
    issue = await _get_issue(session, principal, issue_id)
    p = applier.build_proposal({"kind": issue.kind, "evidence": issue.evidence})
    return ProposalRead(issue_id=issue.id, **p)


@router.post("/issues/{issue_id}/apply", response_model=RemediationActionRead)
async def apply(
    issue_id: uuid.UUID,
    body: ApplyRequest,
    run_tests: bool = Query(default=False, description="run the full pytest gate (slow)"),
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> RemediationActionRead:
    require_user_principal(principal)
    org = tenant_org_id(principal)
    issue = await _get_issue(session, principal, issue_id)
    if body.action_type not in applier.REVERSIBLE_ACTIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"action_type {body.action_type!r} is not auto-appliable; it is propose-only.",
        )
    if body.action_type == "config_tune":
        if body.config is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="config_tune requires `config`")
        result = await applier.apply_config_tune(session, org, body.config, run_tests=run_tests)

    applied = result["applied"]
    action = RemediationAction(
        issue_id=issue.id, org_id=org, action_type=body.action_type,
        status=RemediationStatus.applied if applied else RemediationStatus.failed,
        proposal=body.model_dump(), before=result["before"], after=result["after"],
        gate_result=result["gate_result"],
        applied_at=datetime.now(timezone.utc) if applied else None,
    )
    session.add(action)
    if applied:
        issue.status = IssueStatus.applied
    await _audit(session, principal, "improvement.apply", issue.id,
                 {"action_type": body.action_type, "applied": applied,
                  "gate_passed": result["gate_result"]["passed"]})
    await session.commit()
    await session.refresh(action)
    return RemediationActionRead.model_validate(action)


@router.post("/actions/{action_id}/rollback", response_model=RemediationActionRead)
async def rollback(
    action_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> RemediationActionRead:
    require_user_principal(principal)
    org = tenant_org_id(principal)          # resolved before the lookup — see `_get_issue`
    action = await session.get(RemediationAction, action_id)
    if action is None or action.org_id != org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Action not found")
    if action.status != RemediationStatus.applied:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Action is {action.status.value}, not applied")
    if action.action_type == "config_tune" and action.org_id is not None:
        await applier.rollback_config(session, action.org_id, action.before)
    action.status = RemediationStatus.rolled_back
    await _audit(session, principal, "improvement.rollback", action.id, {"restored": action.before})
    await session.commit()
    await session.refresh(action)
    return RemediationActionRead.model_validate(action)


@router.get("/actions", response_model=list[RemediationActionRead])
async def list_actions(
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
    pg: Pagination = Depends(pagination),
) -> list[RemediationActionRead]:
    rows = (await session.execute(
        select(RemediationAction)
        .where(RemediationAction.org_id == tenant_org_id(principal))
        .order_by(RemediationAction.created_at.desc())
        .limit(pg.limit).offset(pg.offset)
    )).scalars().all()
    return [RemediationActionRead.model_validate(r) for r in rows]
