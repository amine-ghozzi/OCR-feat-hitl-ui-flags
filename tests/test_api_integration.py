"""Phase 4 — in-process API integration tests.

httpx ``ASGITransport`` against the real app, with `get_async_session` overridden to a shared
in-memory SQLite (StaticPool), `get_principal` overridden to inject a principal, and
`enqueue_extraction` patched (no broker). Async bodies run via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from api.db import Base, get_async_session  # noqa: E402
from api.main import create_app  # noqa: E402
from api.models import Org  # noqa: E402
from api.routers import documents as documents_router  # noqa: E402
from api.security import Principal, get_principal  # noqa: E402

# Patch enqueue so submit never touches a broker.
documents_router.enqueue_extraction = lambda job_id: f"task-{job_id}"

ORG_ID = uuid.uuid4()


def _principal(scopes, *, kind="partner", is_superuser=False) -> Principal:
    return Principal(kind=kind, id=str(uuid.uuid4()), org_id=str(ORG_ID), scopes=set(scopes),
                     is_superuser=is_superuser)


async def _new_env(principal: Principal | None):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:  # seed the org
        s.add(Org(id=ORG_ID, name="Acme"))
        await s.commit()

    app = create_app()

    async def _session_override():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    if principal is not None:
        app.dependency_overrides[get_principal] = lambda: principal
    return app, engine


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_PDF = ("bilan.pdf", b"%PDF-1.4 fake", "application/pdf")


def test_submit_then_status_and_list():
    async def body():
        app, engine = await _new_env(_principal({"documents:submit", "documents:read"}))
        async with _client(app) as c:
            r = await c.post("/api/v1/documents", files={"file": _PDF}, data={"accounting_standard": "NCT"})
            assert r.status_code == 202, r.text
            payload = r.json()
            doc_id = payload["document_id"]
            assert payload["status"] == "queued"

            r2 = await c.get(f"/api/v1/documents/{doc_id}")
            assert r2.status_code == 200
            assert r2.json()["state"] == "draft"
            assert r2.json()["latest_job"]["status"] == "queued"

            r3 = await c.get("/api/v1/documents")
            assert r3.json()["total"] == 1
        await engine.dispose()

    asyncio.run(body())


def test_submit_requires_scope():
    async def body():
        app, engine = await _new_env(_principal({"documents:read"}))  # missing submit
        async with _client(app) as c:
            r = await c.post("/api/v1/documents", files={"file": _PDF})
            assert r.status_code == 403, r.text
        await engine.dispose()

    asyncio.run(body())


def test_unauthenticated_is_401():
    async def body():
        app, engine = await _new_env(principal=None)  # no get_principal override, no creds
        async with _client(app) as c:
            r = await c.post("/api/v1/documents", files={"file": _PDF})
            assert r.status_code == 401, r.text
        await engine.dispose()

    asyncio.run(body())


def test_idempotency_key_dedupes():
    async def body():
        app, engine = await _new_env(_principal({"documents:submit", "documents:read"}))
        async with _client(app) as c:
            headers = {"Idempotency-Key": "abc-123"}
            r1 = await c.post("/api/v1/documents", files={"file": _PDF}, headers=headers)
            r2 = await c.post("/api/v1/documents", files={"file": _PDF}, headers=headers)
            assert r1.status_code == 202 and r2.status_code == 202
            assert r1.json()["document_id"] == r2.json()["document_id"]
            assert r1.json()["job_id"] == r2.json()["job_id"]
            assert (await c.get("/api/v1/documents")).json()["total"] == 1
        await engine.dispose()

    asyncio.run(body())


def test_unsupported_file_type_415():
    async def body():
        app, engine = await _new_env(_principal({"documents:submit"}))
        async with _client(app) as c:
            r = await c.post("/api/v1/documents", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
            assert r.status_code == 415, r.text
        await engine.dispose()

    asyncio.run(body())


def test_partner_create_and_mint_key():
    async def body():
        # WP-1.3: creating a tenant and minting a key for *another* org are cross-org
        # actions — `admin` alone no longer suffices, and an API key is never a superuser.
        app, engine = await _new_env(_principal({"admin"}, kind="user", is_superuser=True))
        async with _client(app) as c:
            r = await c.post("/api/v1/partners", json={"name": "Partner Co"})
            assert r.status_code == 201, r.text
            org_id = r.json()["id"]
            r2 = await c.post(
                f"/api/v1/partners/{org_id}/api-keys",
                json={"name": "ingest", "scopes": ["documents:submit"]},
            )
            assert r2.status_code == 201, r2.text
            data = r2.json()
            assert data["api_key"].startswith(data["prefix"] + ".")
            assert data["scopes"] == ["documents:submit"]
        await engine.dispose()

    asyncio.run(body())
