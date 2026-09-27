"""Shared FastAPI dependencies: pagination, tenant scoping, ownership and audit.

This module is the **single** tenancy surface (SEC-2). Routers must not re-implement
"which org is this principal" or "does this principal own that document" locally:

- ``tenant_org_id`` — the org every query is scoped by; a principal without one is refused (403).
- ``owned_document`` — fetch a document with an *unconditional* org filter (no org → no document).
- ``audit`` — write an ``AuditEvent`` in the caller's transaction with a correct actor (SEC-14:
  an API-key principal has **no** ``actor_id``; the key id travels in ``detail``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import AuditEvent, Document
from api.security import Principal


@dataclass
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


def tenant_org_id(principal: Principal) -> uuid.UUID:
    """The org to scope every query by.

    There is no "cross-org" read path: a principal without an org cannot address tenant data,
    so a falsy ``org_id`` is a 403 rather than an unfiltered query.
    """
    if not principal.org_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="No org context for this principal"
        )
    return uuid.UUID(principal.org_id)


def require_user_principal(principal: Principal) -> Principal:
    """Refuse machine credentials on routes that administer *people or credentials*.

    `admin` stays mintable for partner keys on purpose — the config router's docstring is
    explicit that partners tune thresholds through the API, so `PUT /config` and webhook
    registration must keep working for a key. What a key must never do is create people, mint
    or revoke credentials, or drive the improvement loop: those need a human on the record, and
    an audit row with an `actor_id` (a key's is `None` by design — SEC-14).
    """
    if principal.kind != "user":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Human credential required for this action"
        )
    return principal


async def owned_document(
    session: AsyncSession, principal: Principal, document_id: uuid.UUID
) -> Document:
    """Load a document owned by the principal's org, or 404.

    The org filter is unconditional: a cross-tenant id is indistinguishable from a
    non-existent one (no existence oracle).
    """
    org_id = tenant_org_id(principal)
    doc = await session.get(Document, document_id)
    if doc is None or doc.org_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def actor_user_id(principal: Principal) -> uuid.UUID | None:
    """The *user* id behind a principal — ``None`` for machine (API-key) principals.

    SEC-14: ``AuditEvent.actor_id`` / ``Correction.user_id`` hold user ids; writing an ApiKey id
    there silently mislabels partner activity as a human's. The key id goes into ``detail``.
    """
    return uuid.UUID(principal.id) if principal.kind == "user" else None


def audit_detail(principal: Principal, detail: dict | None = None) -> dict:
    """``detail`` enriched with the actor's kind and, for machine principals, the key id."""
    enriched = dict(detail or {})
    enriched["actor_kind"] = principal.kind
    if principal.kind != "user":
        enriched["api_key_id"] = principal.id
    return enriched


def same_actor(principal: Principal, event: AuditEvent) -> bool:
    """Whether ``event`` was written by this principal (users by id, keys by ``detail``)."""
    if principal.kind == "user":
        return event.actor_id is not None and str(event.actor_id) == principal.id
    return (event.detail or {}).get("api_key_id") == principal.id


async def audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    document_id=None,
    detail: dict | None = None,
    *,
    target_type: str = "document",
    target_id=None,
) -> None:
    """Record an ``AuditEvent`` in the caller's transaction (committed with the action)."""
    session.add(AuditEvent(
        org_id=uuid.UUID(principal.org_id) if principal.org_id else None,
        actor_id=actor_user_id(principal),
        actor_kind=principal.kind,
        action=action,
        target_type=target_type,
        target_id=str(target_id if target_id is not None else document_id),
        detail=audit_detail(principal, detail),
    ))
