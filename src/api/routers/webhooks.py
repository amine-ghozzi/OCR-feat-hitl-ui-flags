"""Partner webhooks — register (SSRF-guarded, HMAC secret shown once), list, deliveries, replay.

The signing secret is returned once at creation; only its hash is stored. Delivery itself
(signing + retry/backoff) is performed by the worker on `document.validated` (Phase 5/worker);
these endpoints manage configuration and delivery observability.
"""

from __future__ import annotations

import ipaddress
import secrets
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_async_session
from api.deps import audit, tenant_org_id
from api.models import WebhookDelivery, WebhookEndpoint
from api.schemas import (
    WebhookCreate,
    WebhookCreated,
    WebhookDeliveryRead,
    WebhookRead,
)
from api.security import Principal, hash_secret, require_scopes

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _ssrf_ok(url: str) -> bool:
    """Reject non-http(s), localhost, private/loopback/link-local IPs, and internal TLDs."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname
    if host in ("localhost", "127.0.0.1", "::1"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        return not (host.endswith(".internal") or host.endswith(".local"))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=WebhookCreated)
async def create_webhook(
    body: WebhookCreate,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> WebhookCreated:
    org = tenant_org_id(principal)
    if not _ssrf_ok(body.url):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Callback URL not allowed (SSRF guard)")
    signing_secret = secrets.token_urlsafe(32)
    wh = WebhookEndpoint(
        org_id=org, url=body.url, secret_hash=hash_secret(signing_secret), events=body.events,
    )
    session.add(wh)
    await session.flush()
    # URL and events — never the signing secret or its hash.
    await audit(session, principal, "webhooks.create", None,
                {"url": wh.url, "events": list(wh.events)},
                target_type="webhook", target_id=str(wh.id))
    await session.commit()
    await session.refresh(wh)
    return WebhookCreated(
        id=wh.id, url=wh.url, events=wh.events, active=wh.active,
        created_at=wh.created_at, secret=signing_secret,
    )


@router.get("", response_model=list[WebhookRead])
async def list_webhooks(
    principal: Principal = Depends(require_scopes("documents:read")),
    session: AsyncSession = Depends(get_async_session),
) -> list[WebhookRead]:
    stmt = select(WebhookEndpoint).where(WebhookEndpoint.org_id == tenant_org_id(principal))
    rows = (await session.execute(stmt)).scalars().all()
    return [WebhookRead.model_validate(w) for w in rows]


@router.get("/deliveries", response_model=list[WebhookDeliveryRead])
async def list_deliveries(
    principal: Principal = Depends(require_scopes("documents:read")),
    session: AsyncSession = Depends(get_async_session),
) -> list[WebhookDeliveryRead]:
    stmt = select(WebhookDelivery).join(
        WebhookEndpoint, WebhookDelivery.endpoint_id == WebhookEndpoint.id
    ).where(WebhookEndpoint.org_id == tenant_org_id(principal))
    rows = (await session.execute(stmt.order_by(WebhookDelivery.created_at.desc()))).scalars().all()
    return [WebhookDeliveryRead.model_validate(d) for d in rows]


@router.post("/deliveries/{delivery_id}/replay", status_code=status.HTTP_202_ACCEPTED)
async def replay_delivery(
    delivery_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    # The delivery is reachable only through an endpoint this org owns — without the join a
    # bare id would let any admin replay another tenant's webhook (SEC-2).
    delivery = (await session.execute(
        select(WebhookDelivery)
        .join(WebhookEndpoint, WebhookDelivery.endpoint_id == WebhookEndpoint.id)
        .where(
            WebhookDelivery.id == delivery_id,
            WebhookEndpoint.org_id == tenant_org_id(principal),
        )
    )).scalars().first()
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Delivery not found")
    delivery.delivered = False
    delivery.attempts = 0
    await audit(session, principal, "webhooks.replay", None,
                {"event": delivery.event, "endpoint_id": str(delivery.endpoint_id)},
                target_type="webhook_delivery", target_id=str(delivery.id))
    await session.commit()
    return {"status": "requeued"}
