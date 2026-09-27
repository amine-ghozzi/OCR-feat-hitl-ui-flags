"""Partner API keys + the unified principal (accept a user token OR a partner API key).

- Partner keys: random secret shown **once** at mint; only an **Argon2 hash** (pwdlib) is stored.
  A short, indexed `prefix` makes lookup O(1)-ish before the constant-time hash verify.
- `get_principal` resolves a request to a `Principal` via the human token (fastapi-users, optional)
  *or* the `X-API-Key` header — `APIKeyHeader(auto_error=False)` so either path may be absent.
- `require_scopes(...)` enforces per-route scopes; scope sets derive from role / key scopes.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_async_session
from api.models import ApiKey, User
from api.users import current_user_optional

_hasher = PasswordHash((Argon2Hasher(),))
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

API_KEY_PREFIX = "fz"

# Role → granted scopes (humans). Partner keys carry their own explicit scope list.
ROLE_SCOPES: dict[str, set[str]] = {
    "admin": {"admin", "documents:submit", "documents:read", "hitl:write",
              "feedback:write", "analytics:read"},
    "analyst": {"documents:submit", "documents:read", "hitl:write", "feedback:write"},
    "viewer": {"documents:read", "analytics:read"},
}

# Every scope the system knows about. A partner key may only be minted with scopes drawn
# from this set — an unknown scope is a typo or an attempt to invent authority (SEC-4).
ALL_SCOPES: frozenset[str] = frozenset().union(*ROLE_SCOPES.values())

# Roles an admin may assign. `is_superuser` is deliberately NOT among them: no HTTP path reads
# it from a request or writes it. `routers/admin_users.py` shadows fastapi-users' superuser
# `PATCH /users/{id}` (which would pass `is_superuser` straight through) with a 405, and the
# create/role routes set or leave it False — so cross-org power is granted out of band
# (bootstrap/DB) only, never by an org admin over the API.
ASSIGNABLE_ROLES: frozenset[str] = frozenset(ROLE_SCOPES)


@dataclass
class Principal:
    kind: str                       # "user" | "partner"
    id: str
    org_id: str | None = None
    scopes: set[str] = field(default_factory=set)
    # Cross-org power. True only for a user row flagged `is_superuser`; an API key is never
    # a superuser, and this is never derived from scopes (`admin` is an *in-org* scope).
    is_superuser: bool = False


# ---------------------------------------------------------------------------
# API-key primitives (pure — unit-testable without a DB)
# ---------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str, str]:
    """Mint a key. Returns ``(full_key, prefix, key_hash)``; show ``full_key`` once."""
    raw = secrets.token_urlsafe(32)
    prefix = f"{API_KEY_PREFIX}{raw[:6]}"
    full = f"{prefix}.{raw}"
    return full, prefix, _hasher.hash(full)


def verify_api_key_hash(full_key: str, key_hash: str) -> bool:
    try:
        return _hasher.verify(full_key, key_hash)
    except Exception:  # noqa: BLE001 — malformed hash/key → not verified
        return False


def hash_secret(value: str) -> str:
    """Argon2-hash an arbitrary secret (e.g. a webhook signing secret) for at-rest storage."""
    return _hasher.hash(value)


def verify_secret(value: str, hashed: str) -> bool:
    try:
        return _hasher.verify(value, hashed)
    except Exception:  # noqa: BLE001
        return False


def scopes_for_user(user: User) -> set[str]:
    base = set(ROLE_SCOPES.get(getattr(user, "role", ""), set()))
    if getattr(user, "is_superuser", False):
        base |= ROLE_SCOPES["admin"]
    return base


def principal_has_scopes(principal: Principal, needed) -> bool:
    return set(needed).issubset(principal.scopes)


def _expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    # Normalize naive (SQLite) vs aware (Postgres) before comparing.
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        now = now.replace(tzinfo=None)
    return expires_at < now


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def verify_api_key_principal(session: AsyncSession, presented: str) -> Principal | None:
    prefix = presented.split(".", 1)[0]
    result = await session.execute(
        select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.active.is_(True))
    )
    for key in result.scalars():
        if _expired(key.expires_at):
            continue
        if verify_api_key_hash(presented, key.key_hash):
            return Principal(
                kind="partner", id=str(key.id),
                org_id=str(key.org_id), scopes=set(key.scopes or []),
            )
    return None


async def get_principal(
    user: User | None = Depends(current_user_optional),
    api_key: str | None = Depends(api_key_header),
    session: AsyncSession = Depends(get_async_session),
) -> Principal:
    """Unified auth: human token (fastapi-users) OR partner API key. 401 if neither resolves."""
    if user is not None:
        return Principal(
            kind="user", id=str(user.id),
            org_id=str(user.org_id) if user.org_id else None,
            scopes=scopes_for_user(user),
            is_superuser=bool(getattr(user, "is_superuser", False)),
        )
    if api_key:
        principal = await verify_api_key_principal(session, api_key)
        if principal is not None:
            return principal
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def require_scopes(*needed: str):
    """Dependency factory: 403 unless the principal holds all ``needed`` scopes."""
    async def checker(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal_has_scopes(principal, needed):
            missing = sorted(set(needed) - principal.scopes)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope(s): {missing}",
            )
        return principal
    return checker
