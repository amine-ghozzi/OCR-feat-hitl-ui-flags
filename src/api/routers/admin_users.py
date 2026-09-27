"""User administration — the *only* way an account is created, changed or retired (SEC-1/3/15).

Self-registration is gone (`main.py` no longer mounts fastapi-users' register router), so an
account exists because an admin of a specific org asked for it. This router is mounted **before**
fastapi-users' own users router and deliberately shadows its superuser-only `/users/{id}`
GET/PATCH/DELETE, which are not org-scoped and can set `is_superuser` over HTTP. The routes here
use Starlette's ``uuid`` convertor so the literal ``/users/me`` can never be captured by them.

Invariants:

- **Org comes from the caller, never the body.** The request models carry no `org_id`, so a
  supplied one is ignored rather than trusted.
- **`is_superuser` has no HTTP path at all.** No route reads or writes it from a request; it is
  set out of band (bootstrap/DB). `role` is an allow-list of in-org roles.
- **A machine credential administers nobody.** Every mutation here is `require_user_principal`.
- **Row and audit row commit together** — `users.create` builds the `User` directly rather than
  calling `UserManager.create` (which commits on its own), so a failure to record the audit
  event rolls the account back with it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi_users.authentication import Strategy
from fastapi_users.exceptions import InvalidPasswordException
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_async_session
from api.deps import actor_user_id, audit, require_user_principal, tenant_org_id
from api.models import User
from api.schemas import UserRead
from api.security import ASSIGNABLE_ROLES, Principal, get_principal, require_scopes
from api.users import (
    UserManager,
    bearer_transport,
    get_redis_strategy,
    get_user_manager,
)

router = APIRouter(prefix="/users", tags=["users"])


def _check_role(value: str) -> str:
    if value not in ASSIGNABLE_ROLES:
        raise ValueError(f"role must be one of {sorted(ASSIGNABLE_ROLES)}")
    return value


class AdminUserCreate(BaseModel):
    """What an admin may specify. Deliberately **no** `org_id` and no `is_superuser`."""
    email: EmailStr
    password: str
    role: str = "analyst"
    full_name: str | None = None

    @field_validator("role")
    @classmethod
    def _role_allowed(cls, v: str) -> str:
        return _check_role(v)


class AdminRoleUpdate(BaseModel):
    """A role change an admin may make to a user **inside its own org**."""
    role: str

    @field_validator("role")
    @classmethod
    def _role_allowed(cls, v: str) -> str:
        return _check_role(v)


class SelfUpdate(BaseModel):
    """Self-service profile edits. `role`, `org_id` and the privilege flags are all absent."""
    email: EmailStr | None = None
    password: str | None = None
    full_name: str | None = None
    current_password: str | None = None


async def _scoped_user(session: AsyncSession, principal: Principal, user_id: uuid.UUID) -> User:
    """A user inside the caller's org (superuser sees across orgs), or 404 — no existence oracle."""
    org_id = tenant_org_id(principal)
    user = await session.get(User, user_id)
    if user is None or (not principal.is_superuser and user.org_id != org_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


# ---------------------------------------------------------------------------
# Self-service — must precede fastapi-users' own /users/me
# ---------------------------------------------------------------------------


@router.patch("/me", response_model=UserRead)
async def update_me(
    body: SelfUpdate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
    user_manager: UserManager = Depends(get_user_manager),
    strategy: Strategy = Depends(get_redis_strategy),
    token: str | None = Depends(bearer_transport.scheme),
) -> UserRead:
    """Edit your own profile. Re-authentication is required to move the credential itself.

    fastapi-users' `/me` accepts an email or password change on the session token alone, so a
    borrowed token is enough to take over the account. Here `email`/`password` need
    `current_password`, and a successful change **destroys the caller's token** so the old
    session cannot continue. Other tokens issued to this user are not enumerable through the
    Redis strategy; they lapse within `access_token_lifetime_seconds`.
    """
    require_user_principal(principal)
    user = await session.get(User, actor_user_id(principal))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")

    sensitive = body.email is not None or body.password is not None
    if sensitive:
        if not body.current_password:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="current_password is required to change email or password",
            )
        ok, updated_hash = user_manager.password_helper.verify_and_update(
            body.current_password, user.hashed_password
        )
        if not ok:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="current_password is incorrect")
        if updated_hash:                       # hasher parameters moved on — rehash in place
            user.hashed_password = updated_hash

    changed: list[str] = []
    if body.email is not None and body.email != user.email:
        existing = (await session.execute(
            select(User).where(User.email == body.email)
        )).scalars().first()
        if existing is not None and existing.id != user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="That email is already in use")
        user.email = body.email
        user.is_verified = False               # a new address is unproven until re-verified
        changed.append("email")
    if body.password is not None:
        await user_manager.validate_password(body.password, user)
        user.hashed_password = user_manager.password_helper.hash(body.password)
        changed.append("password")
    if body.full_name is not None and body.full_name != user.full_name:
        user.full_name = body.full_name
        changed.append("full_name")

    # Field NAMES only: an audit trail must never carry the credential it is recording.
    await audit(session, principal, "users.self_update", None, {"fields": changed},
                target_type="user", target_id=str(user.id))
    await session.commit()

    if sensitive and changed and token:
        await strategy.destroy_token(token, user)
    return UserRead.model_validate(user, from_attributes=True)


# ---------------------------------------------------------------------------
# Admin — shadows fastapi-users' non-org-scoped /users/{id}
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, response_model=UserRead)
async def create_user(
    body: AdminUserCreate,
    request: Request,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
    user_manager: UserManager = Depends(get_user_manager),
) -> UserRead:
    require_user_principal(principal)
    org_id = tenant_org_id(principal)

    existing = (await session.execute(
        select(User).where(User.email == body.email)
    )).scalars().first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="A user with that email already exists")

    candidate = User(email=body.email, org_id=org_id, role=body.role, full_name=body.full_name)
    try:
        await user_manager.validate_password(body.password, candidate)
    except InvalidPasswordException as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.reason)

    # Built directly rather than via `UserManager.create`, which commits by itself: the account
    # and its `users.create` audit row must land in ONE transaction or not at all.
    candidate.hashed_password = user_manager.password_helper.hash(body.password)
    candidate.is_active = True
    candidate.is_verified = False
    candidate.is_superuser = False           # no HTTP path may ever raise this
    session.add(candidate)
    await session.flush()

    await audit(session, principal, "users.create", None,
                {"email": body.email, "role": body.role, "org_id": str(org_id)},
                target_type="user", target_id=str(candidate.id))
    await session.commit()
    await user_manager.on_after_register(candidate, request)
    return UserRead.model_validate(candidate, from_attributes=True)


@router.get("/{user_id:uuid}", response_model=UserRead)
async def get_user(
    user_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> UserRead:
    """Org-scoped read. Shadows fastapi-users' superuser `/users/{id}`, which is not scoped."""
    user = await _scoped_user(session, principal, user_id)
    return UserRead.model_validate(user, from_attributes=True)


@router.patch("/{user_id:uuid}", response_model=UserRead)
async def patch_user_is_closed(
    user_id: uuid.UUID,
    _: Principal = Depends(require_scopes("admin")),
) -> UserRead:
    """Closed on purpose (405).

    fastapi-users' `PATCH /users/{id}` uses `create_update_dict_superuser()`, which passes
    `is_superuser`/`is_active`/`is_verified` straight through — i.e. cross-org power grantable
    over HTTP. Shadowing it with an explicit 405 keeps that door shut and names the alternative.
    """
    raise HTTPException(
        status.HTTP_405_METHOD_NOT_ALLOWED,
        detail="use /users/{id}/role; is_superuser/is_active are set out of band",
    )


@router.patch("/{user_id:uuid}/role", response_model=UserRead)
async def update_user_role(
    user_id: uuid.UUID,
    body: AdminRoleUpdate,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> UserRead:
    """Change a user's role — the replacement for the `role` field removed from `UserUpdate`
    (SEC-15). The target must be in the caller's org unless the caller is a superuser."""
    require_user_principal(principal)
    user = await _scoped_user(session, principal, user_id)
    before = user.role
    user.role = body.role
    await audit(session, principal, "users.role", None, {"from": before, "to": body.role},
                target_type="user", target_id=str(user.id))
    await session.commit()
    return UserRead.model_validate(user, from_attributes=True)


@router.delete("/{user_id:uuid}", response_model=UserRead)
async def deactivate_user(
    user_id: uuid.UUID,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> UserRead:
    """Retire an account by deactivating it — never a row delete.

    `Correction.user_id` and `Validation.assignee_id` point at users, and an audit trail whose
    actor has been deleted is not a trail. `is_active=False` stops authentication
    (`current_user_optional` requires active) while the history stays attributable.
    """
    require_user_principal(principal)
    user = await _scoped_user(session, principal, user_id)
    user.is_active = False
    await audit(session, principal, "users.deactivate", None, {"email": user.email},
                target_type="user", target_id=str(user.id))
    await session.commit()
    return UserRead.model_validate(user, from_attributes=True)
