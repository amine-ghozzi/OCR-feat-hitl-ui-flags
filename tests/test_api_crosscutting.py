"""Phase 5 — cross-cutting: RFC 9457 errors, request-id, CORS, Prometheus /metrics."""

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
from api.security import Principal, get_principal  # noqa: E402

ORG_ID = uuid.uuid4()


async def _env(scopes={"documents:read"}):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(Org(id=ORG_ID, name="Acme"))
        await s.commit()
    app = create_app()

    async def _session_override():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        kind="user", id=str(uuid.uuid4()), org_id=str(ORG_ID), scopes=set(scopes)
    )
    return app, engine


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_problem_json_on_404():
    async def body():
        app, engine = await _env()
        async with _client(app) as c:
            r = await c.get(f"/api/v1/documents/{uuid.uuid4()}")
            assert r.status_code == 404
            assert r.headers["content-type"].startswith("application/problem+json")
            j = r.json()
            assert j["status"] == 404 and j["title"] == "Not Found"
            assert "instance" in j and "request_id" in j
        await engine.dispose()

    asyncio.run(body())


def test_validation_error_is_problem_json_422():
    async def body():
        app, engine = await _env({"admin"})
        async with _client(app) as c:
            r = await c.put("/api/v1/config", json={})  # missing required 'config'
            assert r.status_code == 422
            assert r.headers["content-type"].startswith("application/problem+json")
            assert "errors" in r.json()
        await engine.dispose()

    asyncio.run(body())


def test_request_id_header_present():
    async def body():
        app, engine = await _env()
        async with _client(app) as c:
            r = await c.get("/healthz")
            assert r.status_code == 200
            assert any(k.lower() == "x-request-id" for k in r.headers)
        await engine.dispose()

    asyncio.run(body())


def test_metrics_endpoint():
    async def body():
        app, engine = await _env()
        async with _client(app) as c:
            r = await c.get("/metrics")
            assert r.status_code == 200
            assert "# HELP" in r.text or "# TYPE" in r.text
        await engine.dispose()

    asyncio.run(body())


def test_cors_preflight_allows_configured_origin():
    async def body():
        app, engine = await _env()
        async with _client(app) as c:
            r = await c.options(
                "/api/v1/documents",
                headers={
                    "Origin": "http://localhost:8501",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert r.headers.get("access-control-allow-origin") == "http://localhost:8501"
        await engine.dispose()

    asyncio.run(body())
