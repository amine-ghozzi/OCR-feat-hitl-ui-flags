"""Part B — self-improving control loop: sweep, proposal, gated apply, rollback."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from api.db import Base, get_async_session  # noqa: E402
from api.main import create_app  # noqa: E402
from api.models import Issue, OrgConfig, Org  # noqa: E402
from api.security import Principal, get_principal  # noqa: E402
from improvement import signals  # noqa: E402

ORG_ID = uuid.uuid4()
USER_ID = uuid.uuid4()

_FAKE_ISSUES = [
    {"kind": "qualifier_mismatch", "severity": "high",
     "title": "Residual/total false match: 'PRODUITS D'EXPLOITATION' -> 'Autres produits d'exploitation'",
     "evidence": {"raw_text": "PRODUITS D'EXPLOITATION", "matched_to": "Autres produits d'exploitation"},
     "fingerprint": "fp-qmismatch-1"},
    {"kind": "candidate_duplication", "severity": "medium", "title": "dup ratio 0.9",
     "evidence": {"duplication_ratio": 0.9}, "fingerprint": "fp-dup-1"},
]


def _principal(scopes):
    return Principal(kind="user", id=str(USER_ID), org_id=str(ORG_ID), scopes=set(scopes))


async def _env(monkeypatch_detect=None):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
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
    app.dependency_overrides[get_principal] = lambda: _principal({"admin"})
    return app, engine, sm


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_sweep_creates_then_dedupes(monkeypatch):
    async def body():
        app, engine, _sm = await _env()
        monkeypatch.setattr(signals, "detect_issues", lambda *a, **k: list(_FAKE_ISSUES))
        async with _client(app) as c:
            r1 = await c.post("/api/v1/improvement/sweep")
            assert r1.status_code == 200, r1.text
            assert r1.json()["detected"] == 2 and r1.json()["created"] == 2
            # second sweep: same fingerprints → nothing new
            r2 = await c.post("/api/v1/improvement/sweep")
            assert r2.json()["created"] == 0
            issues = (await c.get("/api/v1/improvement/issues")).json()
            assert len(issues) == 2
        await engine.dispose()

    asyncio.run(body())


def test_proposal_for_qualifier_mismatch_is_propose_only(monkeypatch):
    async def body():
        app, engine, sm = await _env()
        async with sm() as s:
            iss = Issue(org_id=ORG_ID, kind="qualifier_mismatch", severity="high",
                        title="x", evidence={"raw_text": "A", "matched_to": "Autres A"},
                        fingerprint="fp-x")
            s.add(iss); await s.commit(); iid = str(iss.id)
        async with _client(app) as c:
            p = await c.get(f"/api/v1/improvement/issues/{iid}/proposal")
            assert p.status_code == 200, p.text
            assert p.json()["action_type"] == "code_fix_proposal" and p.json()["reversible"] is False
            # applying a non-reversible action is rejected
            ap = await c.post(f"/api/v1/improvement/issues/{iid}/apply",
                              json={"action_type": "code_fix_proposal"})
            assert ap.status_code == 400
        await engine.dispose()

    asyncio.run(body())


def test_apply_config_tune_gated_then_rollback(monkeypatch):
    async def body():
        app, engine, sm = await _env()
        async with sm() as s:
            iss = Issue(org_id=ORG_ID, kind="rule_false_positive", severity="medium",
                        title="noisy rule", evidence={}, fingerprint="fp-cfg")
            s.add(iss); await s.commit(); iid = str(iss.id)
        async with _client(app) as c:
            # apply a reversible config tune (run_tests=False so the gate doesn't recurse pytest)
            ap = await c.post(f"/api/v1/improvement/issues/{iid}/apply",
                              json={"action_type": "config_tune",
                                    "config": {"thresholds": {"tau_fast_high": 0.6}}})
            assert ap.status_code == 200, ap.text
            action = ap.json()
            assert action["status"] == "applied" and action["after"]["thresholds"]["tau_fast_high"] == 0.6
            assert action["gate_result"]["passed"] is True
        # OrgConfig now reflects the tune
        async with sm() as s:
            cfg = await s.get(OrgConfig, ORG_ID)
            assert cfg.config["thresholds"]["tau_fast_high"] == 0.6
        async with _client(app) as c:
            rb = await c.post(f"/api/v1/improvement/actions/{action['id']}/rollback")
            assert rb.status_code == 200 and rb.json()["status"] == "rolled_back"
        # rolled back to the prior (empty) config
        async with sm() as s:
            cfg = await s.get(OrgConfig, ORG_ID)
            assert cfg.config == {}
        await engine.dispose()

    asyncio.run(body())
