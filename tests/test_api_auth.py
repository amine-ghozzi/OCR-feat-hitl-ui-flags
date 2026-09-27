"""Phase 2 — auth unit tests: API-key hashing/verify, scopes, and DB-backed key resolution.

Async DB paths run via ``asyncio.run`` against in-memory SQLite (no pytest-asyncio dep).
Importing ``api.*`` is side-effect-free (Redis client is lazy; no Postgres connection).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.db import Base  # noqa: E402
from api.models import ApiKey, Org  # noqa: E402
from api.security import (  # noqa: E402
    Principal,
    generate_api_key,
    principal_has_scopes,
    scopes_for_user,
    verify_api_key_hash,
    verify_api_key_principal,
)


# --- pure primitives ------------------------------------------------------


def test_api_key_hash_roundtrip():
    full, prefix, key_hash = generate_api_key()
    assert full.startswith(prefix + ".")
    assert verify_api_key_hash(full, key_hash) is True
    assert verify_api_key_hash(full + "x", key_hash) is False
    assert verify_api_key_hash("not-a-key", key_hash) is False


class _FakeUser:
    def __init__(self, role, is_superuser=False, email="user@example.com"):
        self.role = role
        self.is_superuser = is_superuser
        self.email = email


def test_scopes_for_user():
    analyst = scopes_for_user(_FakeUser("analyst"))
    assert "documents:read" in analyst and "hitl:write" in analyst
    assert "admin" not in analyst
    # superuser gets the admin superset regardless of role
    assert "admin" in scopes_for_user(_FakeUser("viewer", is_superuser=True))


def test_principal_has_scopes():
    p = Principal(kind="partner", id="x", scopes={"documents:submit", "documents:read"})
    assert principal_has_scopes(p, ["documents:read"])
    assert principal_has_scopes(p, ["documents:submit", "documents:read"])
    assert not principal_has_scopes(p, ["hitl:write"])


# --- DB-backed resolution -------------------------------------------------


async def _seed_and_resolve(scopes, *, active=True, mutate=None):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    full, prefix, key_hash = generate_api_key()
    async with sessionmaker() as s:
        org = Org(name="Acme")
        s.add(org)
        await s.flush()
        org_id = str(org.id)
        key = ApiKey(org_id=org.id, name="k", prefix=prefix, key_hash=key_hash,
                     scopes=scopes, active=active)
        if mutate:
            mutate(key)
        s.add(key)
        await s.commit()

    async with sessionmaker() as s:
        good = await verify_api_key_principal(s, full)
        bad = await verify_api_key_principal(s, full + "tampered")
    await engine.dispose()
    return org_id, good, bad


def test_verify_api_key_happy_and_tampered():
    org_id, good, bad = asyncio.run(_seed_and_resolve(["documents:submit"]))
    assert good is not None
    assert good.kind == "partner"
    assert good.org_id == org_id
    assert good.scopes == {"documents:submit"}
    assert bad is None


def test_verify_api_key_inactive_is_rejected():
    _org, good, _bad = asyncio.run(_seed_and_resolve(["x"], active=False))
    assert good is None


def test_verify_api_key_expired_is_rejected():
    # naive past timestamp (matches SQLite's naive DateTime storage)
    def expire(k):
        k.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)

    _org, good, _bad = asyncio.run(_seed_and_resolve(["x"], mutate=expire))
    assert good is None
