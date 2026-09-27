"""Phase 4c — feedback loop, config, and webhooks integration tests."""

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
from api.models import (  # noqa: E402
    Correction,
    DocState,
    Document,
    Job,
    JobStatus,
    Org,
    VariationCandidate,
)
from api.security import Principal, get_principal  # noqa: E402

ORG_ID = uuid.uuid4()
ADMIN_SCOPES = {"admin", "feedback:write", "documents:read", "documents:submit", "hitl:write", "analytics:read"}

_REPORT = {"metadata": {"validation_report": {"total_checks": 5, "passed_checks": 5, "critical_failures": 0}}}


def _principal(scopes) -> Principal:
    return Principal(kind="user", id=str(uuid.uuid4()), org_id=str(ORG_ID), scopes=set(scopes))


async def _env(scopes=ADMIN_SCOPES):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(Org(id=ORG_ID, name="Acme"))
        await s.flush()
        doc = Document(org_id=ORG_ID, filename="b.pdf", storage_key="x",
                       accounting_standard="NCT", state=DocState.validated)
        s.add(doc)
        await s.flush()
        s.add(Job(document_id=doc.id, org_id=ORG_ID, status=JobStatus.done,
                  hitl_required=False, qcs_score=0.9, result=_REPORT))
        s.add(VariationCandidate(org_id=ORG_ID, raw_text="Stoks", matched_to="Stocks",
                                 match_type="fuzzy", confidence=0.8))
        s.add(Correction(org_id=ORG_ID, document_id=doc.id, raw_text="Stoks",
                         new_value="1600", corrected_canonical="Stocks"))
        await s.commit()

    app = create_app()

    async def _session_override():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    app.dependency_overrides[get_principal] = lambda: _principal(scopes)
    return app, engine


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_variations_list_and_approve():
    async def body():
        app, engine = await _env()
        async with _client(app) as c:
            lst = await c.get("/api/v1/feedback/variations")
            assert lst.status_code == 200 and len(lst.json()) == 1
            cand_id = lst.json()[0]["id"]
            assert lst.json()[0]["status"] == "candidate"
            ap = await c.post(f"/api/v1/feedback/variations/{cand_id}/approve")
            assert ap.status_code == 200 and ap.json()["status"] == "promoted"
            promoted = await c.get("/api/v1/feedback/variations?status=promoted")
            assert len(promoted.json()) == 1
        await engine.dispose()

    asyncio.run(body())


def test_quality_and_rule_flags():
    async def body():
        app, engine = await _env()
        async with _client(app) as c:
            q = (await c.get("/api/v1/feedback/quality")).json()
            assert q["jobs_done"] == 1
            assert q["automation_rate"] == 1.0 and q["hitl_rate"] == 0.0
            assert q["avg_qcs"] == 0.9
            assert q["corrections_total"] == 1
            rf = (await c.get("/api/v1/feedback/rules/flags")).json()
            assert rf["jobs_evaluated"] == 1
            assert rf["avg_pass_rate"] == 1.0 and rf["critical_failure_rate"] == 0.0
        await engine.dispose()

    asyncio.run(body())


def test_ground_truth_export():
    async def body():
        app, engine = await _env()
        async with _client(app) as c:
            r = await c.get("/api/v1/feedback/ground-truth")
            assert r.status_code == 200
            assert "ndjson" in r.headers["content-type"]
            assert '"raw_text": "Stoks"' in r.text
            assert '"corrected_canonical": "Stocks"' in r.text
        await engine.dispose()

    asyncio.run(body())


def test_config_and_reference_data():
    async def body():
        app, engine = await _env()
        async with _client(app) as c:
            put = await c.put("/api/v1/config", json={"config": {"tau_fast_high": 0.8}})
            assert put.status_code == 200
            got = await c.get("/api/v1/config")
            assert got.json()["config"]["tau_fast_high"] == 0.8
            assert len((await c.get("/api/v1/standards")).json()) == 3
            nom = await c.get("/api/v1/nomenclature")
            assert nom.status_code == 200 and len(nom.json()) > 0
        await engine.dispose()

    asyncio.run(body())


def test_webhook_create_list_and_ssrf_guard():
    async def body():
        app, engine = await _env()
        async with _client(app) as c:
            ok = await c.post("/api/v1/webhooks",
                              json={"url": "https://partner.example.com/hook", "events": ["document.validated"]})
            assert ok.status_code == 201, ok.text
            assert ok.json()["secret"]
            ssrf = await c.post("/api/v1/webhooks", json={"url": "http://localhost:9000/x"})
            assert ssrf.status_code == 422
            assert len((await c.get("/api/v1/webhooks")).json()) == 1
        await engine.dispose()

    asyncio.run(body())
