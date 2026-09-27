"""Partner admin — create partner orgs and mint/revoke hashed API keys (scope: admin).

The plaintext key is returned **once** at mint; only its Argon2 hash is stored.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_async_session
from api.deps import audit, require_user_principal, tenant_org_id
from api.models import ApiKey, Org
from api.schemas import ApiKeyCreate, ApiKeyCreated
from api.security import Principal, generate_api_key, require_scopes

router = APIRouter(prefix="/partners", tags=["partners"])


class PartnerCreate(BaseModel):
    name: str


class PartnerRead(BaseModel):
    id: uuid.UUID
    name: str


def _require_superuser(principal: Principal) -> None:
    """Creating tenants / reaching across them is platform power, not org-admin power.

    `admin` is an *in-org* scope (`ROLE_SCOPES`), and an API key never carries superuser
    (`security.get_principal`), so this cannot be reached by a partner key (SEC-3).
    """
    if not principal.is_superuser:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Superuser required for cross-org administration"
        )


def _target_org(principal: Principal, org_id: uuid.UUID) -> uuid.UUID:
    """The org a key operation addresses: the caller's own, unless it is a superuser.

    Checked before any lookup so a foreign org id is a 403 rather than a 404/201 that would
    confirm whether that org exists.
    """
    if not principal.is_superuser and org_id != tenant_org_id(principal):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Cannot administer keys of another org"
        )
    return org_id


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PartnerRead)
async def create_partner(
    body: PartnerCreate,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> PartnerRead:
    require_user_principal(principal)
    _require_superuser(principal)
    org = Org(name=body.name)
    session.add(org)
    await session.flush()
    await audit(session, principal, "partners.create", None, {"name": org.name},
                target_type="org", target_id=str(org.id))
    await session.commit()
    await session.refresh(org)
    return PartnerRead(id=org.id, name=org.name)


@router.post(
    "/{org_id}/api-keys", status_code=status.HTTP_201_CREATED, response_model=ApiKeyCreated
)
async def mint_api_key(
    org_id: uuid.UUID,
    body: ApiKeyCreate,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> ApiKeyCreated:
    # Minting a credential is human-only: a key that can mint keys is a key that can widen
    # its own reach, and the resulting audit row would have no actor (SEC-14).
    require_user_principal(principal)
    _target_org(principal, org_id)
    org = await session.get(Org, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Partner org not found")
    full, prefix, key_hash = generate_api_key()
    key = ApiKey(
        org_id=org_id, name=body.name, prefix=prefix, key_hash=key_hash,
        scopes=body.scopes, expires_at=body.expires_at,
    )
    session.add(key)
    await session.flush()
    # Id, name and scopes — never the secret, and never its hash.
    await audit(session, principal, "partners.key_mint", None,
                {"api_key_id": str(key.id), "name": key.name, "scopes": list(key.scopes),
                 "org_id": str(org_id)},
                target_type="api_key", target_id=str(key.id))
    await session.commit()
    await session.refresh(key)
    return ApiKeyCreated(
        id=key.id, name=key.name, prefix=key.prefix, scopes=key.scopes,
        active=key.active, expires_at=key.expires_at, created_at=key.created_at, api_key=full,
    )


@router.delete("/{org_id}/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    org_id: uuid.UUID,
    key_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    require_user_principal(principal)
    _target_org(principal, org_id)
    key = await session.get(ApiKey, key_id)
    if key is None or key.org_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="API key not found")
    key.active = False
    await audit(session, principal, "partners.key_revoke", None,
                {"api_key_id": str(key.id), "name": key.name, "org_id": str(org_id)},
                target_type="api_key", target_id=str(key.id))
    await session.commit()
