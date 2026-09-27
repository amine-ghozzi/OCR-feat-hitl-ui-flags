"""Phase 4b — HITL review workflow integration tests."""

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
from api.models import (  # noqa: E402
    DocState,
    Document,
    Job,
    JobStatus,
    Org,
    VariationCandidate,
    VariationStatus,
)
from api.security import Principal, get_principal  # noqa: E402

ORG_ID = uuid.uuid4()
USER_ID = uuid.uuid4()

_RESULT = {
    "metadata": {"validation_report": {"critical_failures": 1, "passed_checks": 3, "total_checks": 5}},
    "canonical_tables": [
        {
            "conflicts": [{"row_index": 0, "column_name": "value_n", "flag": "yellow"}],
            "rows": [
                {
                    "raw_text": "Stocks",
                    "canonical_term": "Stocks",
                    "cells": {"value_n": {"flag": "yellow", "raw_value": "1500"}},
                }
            ],
        }
    ],
}


def _principal(scopes) -> Principal:
    return Principal(kind="user", id=str(USER_ID), org_id=str(ORG_ID), scopes=set(scopes))


async def _env(scopes):
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
        doc = Document(org_id=ORG_ID, filename="bilan.pdf", storage_key="x",
                       accounting_standard="NCT", state=DocState.draft)
        s.add(doc)
        await s.flush()
        s.add(Job(document_id=doc.id, org_id=ORG_ID, status=JobStatus.done,
                  hitl_required=True, qcs_score=0.6, result=_RESULT))
        await s.commit()
        doc_id = str(doc.id)

    app = create_app()

    async def _session_override():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    app.dependency_overrides[get_principal] = lambda: _principal(scopes)
    return app, engine, sm, doc_id


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_queue_and_validation_view():
    async def body():
        app, engine, _sm, doc_id = await _env({"documents:read", "hitl:write"})
        async with _client(app) as c:
            q = await c.get("/api/v1/review/queue")
            assert q.status_code == 200
            assert len(q.json()) == 1 and q.json()[0]["hitl_required"] is True
            assert q.json()[0]["qcs_score"] == 0.6

            v = await c.get(f"/api/v1/documents/{doc_id}/validation")
            assert v.status_code == 200
            d = v.json()
            assert d["state"] == "draft" and d["version_id"] == 1
            assert d["report"]["critical_failures"] == 1
            assert any(f["flag"] == "yellow" for f in d["flagged_cells"])
            assert len(d["conflicts"]) >= 1
        await engine.dispose()

    asyncio.run(body())


async def _queue_env(scopes):
    """Two draft documents: one flagged for review, one not. RTE-1 / Q5-A is
    about what the queue does with the SECOND one."""
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
        ids = {}
        for name, hitl, qcs in (("flagged.pdf", True, 0.60), ("quiet.pdf", False, 0.82)):
            doc = Document(org_id=ORG_ID, filename=name, storage_key="x",
                           accounting_standard="NCT", state=DocState.draft)
            s.add(doc)
            await s.flush()
            s.add(Job(document_id=doc.id, org_id=ORG_ID, status=JobStatus.done,
                      hitl_required=hitl, qcs_score=qcs, result=_RESULT))
            ids[name] = str(doc.id)
        await s.commit()

    app = create_app()

    async def _session_override():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    app.dependency_overrides[get_principal] = lambda: _principal(scopes)
    return app, engine, ids


def test_queue_lists_all_done_by_default():
    """RTE-1 / decision Q5-A — no auto-approve means no hidden documents.

    A document nobody flagged is still a document nobody has looked at. Hiding
    it from the queue by default would rebuild, in the UI, exactly the silent
    auto-accept the cascade was found doing — and hide it behind a default
    nobody sets. The queue lists everything, worst first.
    """
    async def body():
        app, engine, _ids = await _queue_env({"documents:read"})
        async with _client(app) as c:
            q = await c.get("/api/v1/review/queue")
            assert q.status_code == 200
            items = q.json()

            names = [i["filename"] for i in items]
            assert names == ["flagged.pdf", "quiet.pdf"], "poorest first"
            assert len(items) == 2, "the unflagged document is listed, not hidden"

            quiet = next(i for i in items if i["filename"] == "quiet.pdf")
            assert quiet["hitl_required"] is False    # it is honestly labelled
            assert quiet["qcs_score"] == 0.82         # and its score is shown
        await engine.dispose()

    asyncio.run(body())


def test_queue_needs_review_only_filter():
    """The narrowing is a reviewer's choice for a working session — opt-in, and
    never the system deciding which documents deserve a human."""
    async def body():
        app, engine, _ids = await _queue_env({"documents:read"})
        async with _client(app) as c:
            narrowed = await c.get("/api/v1/review/queue?needs_review_only=true")
            assert narrowed.status_code == 200
            assert [i["filename"] for i in narrowed.json()] == ["flagged.pdf"]

            # And the explicit false is the same as the default.
            explicit = await c.get("/api/v1/review/queue?needs_review_only=false")
            assert len(explicit.json()) == 2
        await engine.dispose()

    asyncio.run(body())


def test_patch_cells_feeds_variation_loop():
    async def body():
        app, engine, sm, doc_id = await _env({"documents:read", "hitl:write"})
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/documents/{doc_id}/cells",
                json={"corrections": [
                    {"raw_text": "Stoks", "corrected_canonical": "Stocks",
                     "column_name": "value_n", "old_value": "1500", "new_value": "1600"}
                ]},
            )
            assert r.status_code == 200, r.text
            assert r.json()["applied"] == 1 and r.json()["variation_candidates"] == 1
        async with sm() as s:
            cands = (await s.execute(select(VariationCandidate))).scalars().all()
            assert len(cands) == 1
            assert cands[0].status == VariationStatus.approved
            assert cands[0].matched_to == "Stocks" and cands[0].raw_text == "Stoks"
        await engine.dispose()

    asyncio.run(body())


def test_patch_cells_upsert_increments_seen_count():
    """Confirming the same (org, raw, matched) twice increments seen_count, not rows (Layer 2)."""
    async def body():
        app, engine, sm, doc_id = await _env({"documents:read", "hitl:write"})
        payload = {"corrections": [
            {"raw_text": "Stoks", "corrected_canonical": "Stocks",
             "column_name": "value_n", "old_value": "1500", "new_value": "1600"}
        ]}
        async with _client(app) as c:
            r1 = await c.patch(f"/api/v1/documents/{doc_id}/cells", json=payload)
            r2 = await c.patch(f"/api/v1/documents/{doc_id}/cells", json=payload)
            assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
        async with sm() as s:
            cands = (await s.execute(select(VariationCandidate))).scalars().all()
            assert len(cands) == 1            # one row, not two
            assert cands[0].seen_count == 2   # incremented on the repeat confirmation
            assert cands[0].status == VariationStatus.approved
        await engine.dispose()

    asyncio.run(body())


def test_upsert_never_downgrades_status():
    """A repeat confirmation increments seen_count but must not downgrade a promoted mapping."""
    async def body():
        app, engine, sm, doc_id = await _env({"documents:read", "hitl:write"})
        async with sm() as s:  # pre-seed a promoted mapping for the same triple
            s.add(VariationCandidate(org_id=ORG_ID, raw_text="Stoks", matched_to="Stocks",
                                     match_type="fuzzy", confidence=0.9,
                                     status=VariationStatus.promoted, seen_count=5))
            await s.commit()
        async with _client(app) as c:
            r = await c.patch(f"/api/v1/documents/{doc_id}/cells", json={"corrections": [
                {"raw_text": "Stoks", "corrected_canonical": "Stocks", "column_name": "value_n"}
            ]})
            assert r.status_code == 200, r.text
        async with sm() as s:
            cands = (await s.execute(select(VariationCandidate))).scalars().all()
            assert len(cands) == 1
            assert cands[0].status == VariationStatus.promoted  # not downgraded to approved
            assert cands[0].seen_count == 6                     # still incremented
        await engine.dispose()

    asyncio.run(body())


def test_transition_with_optimistic_lock():
    async def body():
        app, engine, _sm, doc_id = await _env({"documents:read", "hitl:write"})
        async with _client(app) as c:
            assert (await c.get(f"/api/v1/documents/{doc_id}/validation")).json()["version_id"] == 1
            ok = await c.post(f"/api/v1/documents/{doc_id}/transition",
                              json={"to_state": "validated", "reason": "ok"},
                              headers={"If-Match": "1"})
            assert ok.status_code == 200, ok.text
            assert ok.json()["state"] == "validated" and ok.json()["version_id"] == 2
            # stale version → 409
            stale = await c.post(f"/api/v1/documents/{doc_id}/transition",
                                 json={"to_state": "archived"}, headers={"If-Match": "1"})
            assert stale.status_code == 409
            # audit recorded the transition
            audit = await c.get(f"/api/v1/documents/{doc_id}/audit")
            assert "review.transition" in [e["action"] for e in audit.json()]
        await engine.dispose()

    asyncio.run(body())


def test_illegal_transition_rejected():
    async def body():
        app, engine, _sm, doc_id = await _env({"documents:read", "hitl:write"})
        async with _client(app) as c:
            r = await c.post(f"/api/v1/documents/{doc_id}/transition", json={"to_state": "archived"})
            assert r.status_code == 409  # draft → archived is illegal
        await engine.dispose()

    asyncio.run(body())


def test_cells_requires_hitl_scope():
    async def body():
        app, engine, _sm, doc_id = await _env({"documents:read"})  # no hitl:write
        async with _client(app) as c:
            r = await c.patch(f"/api/v1/documents/{doc_id}/cells", json={"corrections": []})
            assert r.status_code == 403
        await engine.dispose()

    asyncio.run(body())
