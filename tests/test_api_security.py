"""WP-1.3 — auth & tenancy, exercised through the REAL ``get_principal``.

Unlike the other API suites, this one does **not** override ``get_principal``: a request carries
either a real ``X-API-Key`` row (resolved by ``verify_api_key_principal``) or a real bearer token,
so the principal under test is the one production builds. Only two seams are stubbed:

- ``get_async_session`` → shared in-memory SQLite (as everywhere else);
- ``get_redis_strategy`` → a fake strategy that resolves the session token to a seeded ``User``.

The strategy seam is the right one: it is the auth backend's own dependency, so *every*
authenticated route resolves through it — our routers and fastapi-users' alike. (Overriding
``api.users.current_active_user`` would not: ``get_users_router`` builds its own
``authenticator.current_user(...)`` closure internally, so that object is never consulted.)
``get_principal`` still derives org, scopes and ``is_superuser`` from the user row itself.

With no ``Authorization`` header fastapi-users never calls the strategy, so the API-key tests
touch no Redis at all.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from api import storage  # noqa: E402
from api.db import Base, get_async_session  # noqa: E402
from api.main import create_app  # noqa: E402
from api.models import (  # noqa: E402
    ApiKey,
    AuditEvent,
    DocState,
    Document,
    Job,
    JobStatus,
    Org,
    User,
    VariationCandidate,
    WebhookDelivery,
    WebhookEndpoint,
)
from api.routers import admin_users as admin_users_router  # noqa: E402
from api.routers import documents as documents_router  # noqa: E402
from api.security import generate_api_key  # noqa: E402
from api.users import get_redis_strategy, password_helper  # noqa: E402

# No broker in tests (same idiom as test_api_integration).
documents_router.enqueue_extraction = lambda job_id: f"task-{job_id}"

ORG_A = uuid.uuid4()
ORG_B = uuid.uuid4()

_PDF = ("bilan.pdf", b"%PDF-1.4 fake", "application/pdf")
_GOOD_PASSWORD = "correct-horse-battery"   # ≥10 chars, no email local-part (UserManager rule)


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------


TOKEN = "session-token"          # opaque; the fake strategy is what resolves it


class FakeStrategy:
    """Stands in for `RedisStrategy` — resolves one token, and records destructions.

    This is the auth backend's own `get_strategy` dependency, so overriding it exercises the
    real `get_principal` *and* fastapi-users' own routers.
    """

    def __init__(self):
        self.user: User | None = None
        self.destroyed: list[str] = []

    async def read_token(self, token, user_manager):
        return self.user if token == TOKEN else None

    async def write_token(self, user):        # pragma: no cover — login isn't under test
        return TOKEN

    async def destroy_token(self, token, user):
        self.destroyed.append(token)


class Env:
    """Handles for one isolated app + DB."""

    def __init__(self, app, engine, sm, strategy: FakeStrategy):
        self.app, self.engine, self.sm, self.strategy = app, engine, sm, strategy

    def client(self, *, swallow_errors: bool = False) -> AsyncClient:
        """`swallow_errors` returns the 500 response instead of re-raising: Starlette's
        `ServerErrorMiddleware` re-raises after building it, which `ASGITransport` propagates
        by default — unhelpful when the 500 itself is what a test asserts."""
        return AsyncClient(
            transport=ASGITransport(app=self.app, raise_app_exceptions=not swallow_errors),
            base_url="http://test",
        )

    def as_user(self, user: User) -> dict:
        """Authenticate subsequent requests as this user row; returns the auth header."""
        self.strategy.user = user
        return {"Authorization": f"Bearer {TOKEN}"}


async def _make_env() -> Env:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add_all([Org(id=ORG_A, name="Acme"), Org(id=ORG_B, name="Rival")])
        await s.commit()

    app = create_app()

    async def _session_override():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    strategy = FakeStrategy()
    app.dependency_overrides[get_redis_strategy] = lambda: strategy
    return Env(app, engine, sm, strategy)


async def _seed_user(env: Env, *, org_id, role="analyst", is_superuser=False,
                     password=_GOOD_PASSWORD) -> User:
    async with env.sm() as s:
        user = User(
            id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@example.com",
            hashed_password=password_helper.hash(password), is_active=True,
            is_superuser=is_superuser, is_verified=True, org_id=org_id, role=role,
        )
        s.add(user)
        await s.commit()
    return user


async def _seed_key(env: Env, *, org_id, scopes) -> tuple[str, uuid.UUID]:
    """Mint a real API key row; returns ``(plaintext, key_id)``."""
    full, prefix, key_hash = generate_api_key()
    async with env.sm() as s:
        key = ApiKey(org_id=org_id, name="k", prefix=prefix, key_hash=key_hash, scopes=list(scopes))
        s.add(key)
        await s.commit()
        return full, key.id


async def _seed_document(env: Env, *, org_id, with_result=None) -> uuid.UUID:
    async with env.sm() as s:
        doc = Document(org_id=org_id, filename="b.pdf", storage_key="x",
                       accounting_standard="NCT", state=DocState.draft)
        s.add(doc)
        await s.flush()
        s.add(Job(document_id=doc.id, org_id=org_id,
                  status=JobStatus.done if with_result else JobStatus.queued,
                  result=with_result))
        await s.commit()
        return doc.id


async def _audit_rows(env: Env, action: str | None = None) -> list[AuditEvent]:
    async with env.sm() as s:
        stmt = select(AuditEvent)
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        return list((await s.execute(stmt.order_by(AuditEvent.created_at.asc()))).scalars().all())


def _run(coro_fn):
    """Run an async test body against a fresh env, always disposing the engine."""
    async def body():
        env = await _make_env()
        try:
            await coro_fn(env)
        finally:
            await env.engine.dispose()

    asyncio.run(body())


# --------------------------------------------------------------------------
# SEC-1 — no self-registration
# --------------------------------------------------------------------------


def test_register_route_is_404():
    async def body(env: Env):
        async with env.client() as c:
            r = await c.post("/api/v1/auth/register",
                             json={"email": "intruder@example.com", "password": _GOOD_PASSWORD})
            assert r.status_code == 404, r.text

    _run(body)


def test_unauthenticated_is_401():
    async def body(env: Env):
        async with env.client() as c:
            assert (await c.get("/api/v1/documents")).status_code == 401
            assert (await c.post("/api/v1/users", json={
                "email": "x@example.com", "password": _GOOD_PASSWORD})).status_code == 401

    _run(body)


# --------------------------------------------------------------------------
# SEC-3 — admin actions confined to the caller's org
# --------------------------------------------------------------------------


def test_admin_create_user_forces_caller_org():
    async def body(env: Env):
        h = env.as_user(await _seed_user(env, org_id=ORG_A, role="admin"))
        async with env.client() as c:
            r = await c.post("/api/v1/users", headers=h, json={
                "email": "new.hire@example.com", "password": _GOOD_PASSWORD,
                "role": "analyst",
                # Both of these are attacker-controlled and must be ignored.
                "org_id": str(ORG_B), "is_superuser": True,
            })
            assert r.status_code == 201, r.text
            assert r.json()["org_id"] == str(ORG_A)
            assert r.json()["is_superuser"] is False
            new_id = uuid.UUID(r.json()["id"])

        async with env.sm() as s:
            created = await s.get(User, new_id)
            assert created.org_id == ORG_A          # caller's org, not the body's
            assert created.role == "analyst"
            assert created.is_superuser is False
            # The password was hashed, never stored as given.
            assert created.hashed_password != _GOOD_PASSWORD

    _run(body)


def test_admin_create_user_rejects_unknown_role():
    async def body(env: Env):
        h = env.as_user(await _seed_user(env, org_id=ORG_A, role="admin"))
        async with env.client() as c:
            r = await c.post("/api/v1/users", headers=h, json={
                "email": "x@example.com", "password": _GOOD_PASSWORD, "role": "superadmin"})
            assert r.status_code == 422, r.text

    _run(body)


def test_users_create_is_atomic_with_its_audit_row(monkeypatch):
    """F3: the account and its `users.create` audit row share one transaction."""
    async def body(env: Env):
        async def boom(*a, **k):
            raise RuntimeError("audit sink down")

        monkeypatch.setattr(admin_users_router, "audit", boom)
        h = env.as_user(await _seed_user(env, org_id=ORG_A, role="admin"))
        async with env.client(swallow_errors=True) as c:
            r = await c.post("/api/v1/users", headers=h, json={
                "email": "ghost@example.com", "password": _GOOD_PASSWORD, "role": "analyst"})
            assert r.status_code == 500, r.text

        async with env.sm() as s:
            orphan = (await s.execute(
                select(User).where(User.email == "ghost@example.com")
            )).scalars().first()
            assert orphan is None, "user row survived a failed audit write"

    _run(body)


def test_mint_key_other_org_is_403():
    async def body(env: Env):
        h = env.as_user(await _seed_user(env, org_id=ORG_A, role="admin"))
        async with env.client() as c:
            other = await c.post(f"/api/v1/partners/{ORG_B}/api-keys", headers=h,
                                 json={"name": "k", "scopes": ["documents:read"]})
            assert other.status_code == 403, other.text
            own = await c.post(f"/api/v1/partners/{ORG_A}/api-keys", headers=h,
                               json={"name": "k", "scopes": ["documents:read"]})
            assert own.status_code == 201, own.text
            # Creating a *tenant* is platform power, not org-admin power.
            assert (await c.post("/api/v1/partners", headers=h,
                                 json={"name": "X"})).status_code == 403

        # A superuser may do both.
        h = env.as_user(await _seed_user(env, org_id=ORG_A, role="admin", is_superuser=True))
        async with env.client() as c:
            assert (await c.post(f"/api/v1/partners/{ORG_B}/api-keys", headers=h,
                                 json={"name": "k", "scopes": ["documents:read"]})).status_code == 201
            assert (await c.post("/api/v1/partners", headers=h,
                                 json={"name": "X"})).status_code == 201

    _run(body)


def test_mint_key_scopes_outside_allowlist_is_422():
    async def body(env: Env):
        h = env.as_user(await _seed_user(env, org_id=ORG_A, role="admin"))
        async with env.client() as c:
            r = await c.post(f"/api/v1/partners/{ORG_A}/api-keys", headers=h,
                             json={"name": "k", "scopes": ["documents:read", "root"]})
            assert r.status_code == 422, r.text
            assert "root" in r.text

    _run(body)


# --------------------------------------------------------------------------
# F1 — a machine credential administers nobody
# --------------------------------------------------------------------------


def test_admin_scoped_api_key_cannot_administer_people_or_credentials():
    """An `admin` key keeps its *configuration* powers but gets none over people or keys."""
    async def body(env: Env):
        key, _ = await _seed_key(env, org_id=ORG_A, scopes=["admin", "documents:read"])
        h = {"X-API-Key": key}
        victim = await _seed_user(env, org_id=ORG_A, role="viewer")

        async with env.client() as c:
            forbidden = [
                ("POST", "/api/v1/users",
                 {"email": "a@example.com", "password": _GOOD_PASSWORD, "role": "admin"}),
                ("PATCH", f"/api/v1/users/{victim.id}/role", {"role": "admin"}),
                ("DELETE", f"/api/v1/users/{victim.id}", None),
                ("POST", "/api/v1/partners", {"name": "X"}),
                ("POST", f"/api/v1/partners/{ORG_A}/api-keys",
                 {"name": "k", "scopes": ["admin"]}),
                ("POST", "/api/v1/improvement/sweep", None),
                ("POST", f"/api/v1/improvement/issues/{uuid.uuid4()}/apply",
                 {"action_type": "config_tune", "config": {}}),
                ("POST", f"/api/v1/improvement/actions/{uuid.uuid4()}/rollback", None),
            ]
            for method, path, payload in forbidden:
                r = await c.request(method, path, headers=h, json=payload)
                assert r.status_code == 403, (method, path, r.status_code, r.text)

            # Revoking a key is equally human-only (mint one out of band to aim at).
            _plain, victim_key_id = await _seed_key(env, org_id=ORG_A, scopes=["documents:read"])
            r = await c.delete(f"/api/v1/partners/{ORG_A}/api-keys/{victim_key_id}", headers=h)
            assert r.status_code == 403, r.text

            # …but configuration still works: partners tune thresholds via the API by design.
            cfg = await c.put("/api/v1/config", headers=h,
                              json={"config": {"thresholds": {"tau_fast_high": 0.7}}})
            assert cfg.status_code == 200, cfg.text
            wh = await c.post("/api/v1/webhooks", headers=h,
                              json={"url": "https://partner.example.com/hook",
                                    "events": ["document.validated"]})
            assert wh.status_code == 201, wh.text

        async with env.sm() as s:
            assert (await s.get(User, victim.id)).role == "viewer"
            assert (await s.get(User, victim.id)).is_active is True

    _run(body)


# --------------------------------------------------------------------------
# SEC-15 / F2 — nobody promotes themselves, and is_superuser has no HTTP path
# --------------------------------------------------------------------------


def test_user_cannot_self_promote_role():
    """`PATCH /users/me` must not be able to change `role`.

    The local handler's `SelfUpdate` has no `role` field, and `UserUpdate` (fastapi-users'
    schema, still used elsewhere) no longer carries one either — its `create_update_dict()`
    strips only the privilege flags, so a `role` field there would reach the DB and, via
    `scopes_for_user`, hand the caller the `admin` scope set.
    """
    from api.schemas import UserUpdate  # noqa: PLC0415

    assert "role" not in UserUpdate.model_fields
    smuggled = UserUpdate.model_validate({"role": "admin", "full_name": "V"})
    assert "role" not in smuggled.create_update_dict()
    assert "is_superuser" not in smuggled.create_update_dict()

    async def body(env: Env):
        viewer = await _seed_user(env, org_id=ORG_A, role="viewer")
        h = env.as_user(viewer)
        async with env.client() as c:
            r = await c.patch("/api/v1/users/me", headers=h,
                              json={"role": "admin", "full_name": "V"})
            assert r.status_code == 200, r.text
            assert r.json()["role"] == "viewer"
            # A viewer reaches no admin surface and no role endpoint.
            assert (await c.post("/api/v1/partners", headers=h,
                                 json={"name": "X"})).status_code == 403
            assert (await c.patch(f"/api/v1/users/{viewer.id}/role", headers=h,
                                  json={"role": "admin"})).status_code == 403

        async with env.sm() as s:
            assert (await s.get(User, viewer.id)).role == "viewer"

    _run(body)


def test_patch_user_by_id_is_405_and_superuser_routes_are_shadowed():
    """F2: fastapi-users' non-org-scoped `/users/{id}` PATCH would pass `is_superuser` through."""
    async def body(env: Env):
        admin = await _seed_user(env, org_id=ORG_A, role="admin")
        target = await _seed_user(env, org_id=ORG_A, role="viewer")
        h = env.as_user(admin)
        async with env.client() as c:
            r = await c.patch(f"/api/v1/users/{target.id}", headers=h,
                              json={"is_superuser": True, "is_active": False})
            assert r.status_code == 405, r.text
            assert "set out of band" in r.json()["detail"]

        async with env.sm() as s:
            still = await s.get(User, target.id)
            assert still.is_superuser is False and still.is_active is True

    _run(body)


def test_admin_get_and_deactivate_are_org_scoped():
    async def body(env: Env):
        admin = await _seed_user(env, org_id=ORG_A, role="admin")
        mine = await _seed_user(env, org_id=ORG_A, role="analyst")
        theirs = await _seed_user(env, org_id=ORG_B, role="analyst")
        h = env.as_user(admin)
        async with env.client() as c:
            ok = await c.get(f"/api/v1/users/{mine.id}", headers=h)
            assert ok.status_code == 200 and ok.json()["id"] == str(mine.id)
            assert (await c.get(f"/api/v1/users/{theirs.id}", headers=h)).status_code == 404

            gone = await c.delete(f"/api/v1/users/{mine.id}", headers=h)
            assert gone.status_code == 200, gone.text
            assert gone.json()["is_active"] is False
            assert (await c.delete(f"/api/v1/users/{theirs.id}", headers=h)).status_code == 404

        async with env.sm() as s:
            # Deactivated, never deleted — the audit trail stays attributable.
            assert (await s.get(User, mine.id)) is not None
            assert (await s.get(User, mine.id)).is_active is False
            assert (await s.get(User, theirs.id)).is_active is True
        assert [e.action for e in await _audit_rows(env, "users.deactivate")] == ["users.deactivate"]

    _run(body)


def test_admin_patch_role_same_org_only():
    async def body(env: Env):
        admin = await _seed_user(env, org_id=ORG_A, role="admin")
        mine = await _seed_user(env, org_id=ORG_A, role="viewer")
        theirs = await _seed_user(env, org_id=ORG_B, role="viewer")
        h = env.as_user(admin)
        async with env.client() as c:
            ok = await c.patch(f"/api/v1/users/{mine.id}/role", headers=h, json={"role": "analyst"})
            assert ok.status_code == 200, ok.text
            assert ok.json()["role"] == "analyst"

            # Another org's user is not found, not forbidden — no existence oracle.
            assert (await c.patch(f"/api/v1/users/{theirs.id}/role", headers=h,
                                  json={"role": "admin"})).status_code == 404
            # Unknown roles are refused outright.
            assert (await c.patch(f"/api/v1/users/{mine.id}/role", headers=h,
                                  json={"role": "root"})).status_code == 422

        async with env.sm() as s:
            assert (await s.get(User, mine.id)).role == "analyst"
            assert (await s.get(User, theirs.id)).role == "viewer"

    _run(body)


# --------------------------------------------------------------------------
# F4 — /users/me re-authenticates before moving the credential
# --------------------------------------------------------------------------


def test_self_update_requires_current_password_for_email_or_password():
    async def body(env: Env):
        user = await _seed_user(env, org_id=ORG_A, role="analyst")
        original_email, original_hash = user.email, user.hashed_password
        h = env.as_user(user)
        async with env.client() as c:
            # A borrowed session token alone is not enough to move the credential.
            assert (await c.patch("/api/v1/users/me", headers=h,
                                  json={"email": "taken.over@example.com"})).status_code == 422
            assert (await c.patch("/api/v1/users/me", headers=h,
                                  json={"password": "another-long-one"})).status_code == 422
            # A wrong current_password is refused.
            assert (await c.patch("/api/v1/users/me", headers=h,
                                  json={"password": "another-long-one",
                                        "current_password": "not-it"})).status_code == 403
            # A profile-only edit needs no re-authentication.
            name = await c.patch("/api/v1/users/me", headers=h, json={"full_name": "Amina"})
            assert name.status_code == 200, name.text

        async with env.sm() as s:
            after = await s.get(User, user.id)
            assert after.email == original_email
            assert after.hashed_password == original_hash
            assert after.full_name == "Amina"
        assert env.strategy.destroyed == []      # nothing sensitive changed → session kept

    _run(body)


def test_self_update_destroys_the_session_token_on_password_change():
    async def body(env: Env):
        user = await _seed_user(env, org_id=ORG_A, role="analyst")
        old_hash = user.hashed_password
        h = env.as_user(user)
        async with env.client() as c:
            r = await c.patch("/api/v1/users/me", headers=h,
                              json={"password": "a-brand-new-secret",
                                    "current_password": _GOOD_PASSWORD})
            assert r.status_code == 200, r.text

        async with env.sm() as s:
            assert (await s.get(User, user.id)).hashed_password != old_hash
        # The credential moved, so the session that moved it is destroyed.
        assert env.strategy.destroyed == [TOKEN]

        (event,) = await _audit_rows(env, "users.self_update")
        assert event.detail["fields"] == ["password"]
        # The trail records WHAT changed, never the value.
        assert "a-brand-new-secret" not in json.dumps(event.detail)

    _run(body)


# --------------------------------------------------------------------------
# SEC-2 — every data path is org-scoped
# --------------------------------------------------------------------------


def test_orgless_principal_is_403_on_documents():
    async def body(env: Env):
        # An admin with no org has no tenant to read: 403, never an unfiltered list.
        h = env.as_user(await _seed_user(env, org_id=None, role="admin"))
        async with env.client() as c:
            assert (await c.get("/api/v1/documents", headers=h)).status_code == 403
            assert (await c.get("/api/v1/review/queue", headers=h)).status_code == 403
            assert (await c.get("/api/v1/feedback/variations", headers=h)).status_code == 403
            assert (await c.get("/api/v1/config", headers=h)).status_code == 403
            assert (await c.get("/api/v1/webhooks", headers=h)).status_code == 403
            # F7: 403 regardless of whether the id exists — no existence oracle.
            assert (await c.get(f"/api/v1/improvement/issues/{uuid.uuid4()}/proposal",
                                headers=h)).status_code == 403

    _run(body)


def test_cross_org_document_is_404():
    async def body(env: Env):
        doc_b = await _seed_document(env, org_id=ORG_B, with_result={"canonical_tables": []})
        key, _ = await _seed_key(env, org_id=ORG_A, scopes=["documents:read", "hitl:write"])
        h = {"X-API-Key": key}
        async with env.client() as c:
            for path in (f"/api/v1/documents/{doc_b}",
                         f"/api/v1/documents/{doc_b}/result",
                         f"/api/v1/documents/{doc_b}/validation",
                         f"/api/v1/documents/{doc_b}/audit"):
                r = await c.get(path, headers=h)
                assert r.status_code == 404, (path, r.status_code, r.text)
            # …and it is invisible in the listings too.
            assert (await c.get("/api/v1/documents", headers=h)).json()["total"] == 0
            assert (await c.get("/api/v1/review/queue", headers=h)).json() == []

    _run(body)


def test_cross_org_webhook_delivery_replay_is_404():
    """`replay_delivery` fetched a delivery by bare id — any admin could requeue another
    tenant's webhook. The lookup now joins through the owning endpoint."""
    async def body(env: Env):
        async with env.sm() as s:
            ep = WebhookEndpoint(org_id=ORG_B, url="https://b.example.com/hook",
                                 secret_hash="x", events=["document.validated"])
            s.add(ep)
            await s.flush()
            delivery = WebhookDelivery(endpoint_id=ep.id, event="document.validated",
                                       payload={}, attempts=3, delivered=True)
            s.add(delivery)
            await s.commit()
            delivery_id = delivery.id

        h = env.as_user(await _seed_user(env, org_id=ORG_A, role="admin"))
        async with env.client() as c:
            r = await c.post(f"/api/v1/webhooks/deliveries/{delivery_id}/replay", headers=h)
            assert r.status_code == 404, r.text
            assert (await c.get("/api/v1/webhooks", headers=h)).json() == []
            assert (await c.get("/api/v1/webhooks/deliveries", headers=h)).json() == []

        async with env.sm() as s:
            untouched = await s.get(WebhookDelivery, delivery_id)
            assert untouched.delivered is True and untouched.attempts == 3

    _run(body)


# --------------------------------------------------------------------------
# SEC-12 / SEC-14 / F5 — audit events with a correct actor
# --------------------------------------------------------------------------


def test_upload_and_result_write_audit_events():
    async def body(env: Env):
        key, _key_id = await _seed_key(
            env, org_id=ORG_A, scopes=["documents:submit", "documents:read"])
        h = {"X-API-Key": key}
        async with env.client() as c:
            up = await c.post("/api/v1/documents", files={"file": _PDF}, headers=h)
            assert up.status_code == 202, up.text
            doc_id = uuid.UUID(up.json()["document_id"])

            # Give the document a finished job so /result has something to return.
            async with env.sm() as s:
                job = (await s.execute(select(Job).where(Job.document_id == doc_id))).scalars().first()
                job.status = JobStatus.done
                job.result = {"canonical_tables": [], "metadata": {}}
                await s.commit()

            res = await c.get(f"/api/v1/documents/{doc_id}/result", headers=h)
            assert res.status_code == 200, res.text

        actions = [e.action for e in await _audit_rows(env)]
        assert "documents.upload" in actions
        assert "documents.result" in actions

        upload = (await _audit_rows(env, "documents.upload"))[0]
        assert upload.org_id == ORG_A
        assert upload.target_type == "document" and upload.target_id == str(doc_id)

    _run(body)


def test_admin_surface_writes_audit_events():
    """F5: every credential/config mutation leaves a row — and never leaks the secret."""
    async def body(env: Env):
        h = env.as_user(await _seed_user(env, org_id=ORG_A, role="admin", is_superuser=True))
        async with env.client() as c:
            partner = await c.post("/api/v1/partners", headers=h, json={"name": "Partner Co"})
            assert partner.status_code == 201, partner.text
            new_org = partner.json()["id"]

            mint = await c.post(f"/api/v1/partners/{new_org}/api-keys", headers=h,
                                json={"name": "ingest", "scopes": ["documents:submit"]})
            assert mint.status_code == 201, mint.text
            secret = mint.json()["api_key"]

            revoke = await c.delete(
                f"/api/v1/partners/{new_org}/api-keys/{mint.json()['id']}", headers=h)
            assert revoke.status_code == 204, revoke.text

            assert (await c.put("/api/v1/config", headers=h,
                                json={"config": {"thresholds": {"x": 1}}})).status_code == 200
            wh = await c.post("/api/v1/webhooks", headers=h,
                              json={"url": "https://p.example.com/h", "events": ["document.validated"]})
            assert wh.status_code == 201, wh.text

        rows = await _audit_rows(env)
        actions = [e.action for e in rows]
        for expected in ("partners.create", "partners.key_mint", "partners.key_revoke",
                         "config.update", "webhooks.create"):
            assert expected in actions, (expected, actions)

        # The plaintext key and the webhook secret must appear in no audit detail.
        blob = json.dumps([e.detail for e in rows])
        assert secret not in blob
        assert wh.json()["secret"] not in blob
        (minted,) = await _audit_rows(env, "partners.key_mint")
        assert minted.detail["scopes"] == ["documents:submit"]
        assert "key_hash" not in json.dumps(minted.detail)

    _run(body)


def test_feedback_variation_decisions_are_audited():
    async def body(env: Env):
        async with env.sm() as s:
            cand = VariationCandidate(org_id=ORG_A, raw_text="Stoks", matched_to="Stocks",
                                      match_type="fuzzy", confidence=0.8)
            s.add(cand)
            await s.commit()
            cand_id = cand.id

        h = env.as_user(await _seed_user(env, org_id=ORG_A, role="admin"))
        async with env.client() as c:
            assert (await c.post(f"/api/v1/feedback/variations/{cand_id}/approve",
                                 headers=h)).status_code == 200
            assert (await c.post(f"/api/v1/feedback/variations/{cand_id}/reject",
                                 headers=h)).status_code == 200

        actions = [e.action for e in await _audit_rows(env)]
        assert "feedback.variation_approve" in actions
        assert "feedback.variation_reject" in actions

    _run(body)


def test_audit_actor_none_for_api_key():
    async def body(env: Env):
        key, key_id = await _seed_key(env, org_id=ORG_A, scopes=["documents:submit"])
        async with env.client() as c:
            up = await c.post("/api/v1/documents", files={"file": _PDF},
                              headers={"X-API-Key": key})
            assert up.status_code == 202, up.text

        (event,) = await _audit_rows(env, "documents.upload")
        # SEC-14: an ApiKey id must never land in actor_id (a user column).
        assert event.actor_id is None
        assert event.actor_kind == "partner"
        assert event.detail["actor_kind"] == "partner"
        assert event.detail["api_key_id"] == str(key_id)

    _run(body)


def test_audit_actor_is_the_user_for_human_principals():
    async def body(env: Env):
        user = await _seed_user(env, org_id=ORG_A, role="analyst")
        doc_id = await _seed_document(env, org_id=ORG_A)
        h = env.as_user(user)
        async with env.client() as c:
            assert (await c.get(f"/api/v1/documents/{doc_id}/validation",
                                headers=h)).status_code == 200

        (event,) = await _audit_rows(env, "review.start")
        assert event.actor_id == user.id
        assert event.actor_kind == "user"
        assert "api_key_id" not in event.detail

    _run(body)


# --------------------------------------------------------------------------
# UI-5 — review.start once per (document, actor)
# --------------------------------------------------------------------------


def test_review_start_event_once_per_actor():
    async def body(env: Env):
        doc_id = await _seed_document(env, org_id=ORG_A)
        first = await _seed_user(env, org_id=ORG_A, role="analyst")
        second = await _seed_user(env, org_id=ORG_A, role="analyst")

        h = env.as_user(first)
        async with env.client() as c:
            for _ in range(3):
                assert (await c.get(f"/api/v1/documents/{doc_id}/validation",
                                    headers=h)).status_code == 200
            # Claiming the document is also a "start", but not a second one for this actor.
            assert (await c.post(f"/api/v1/documents/{doc_id}/assign", headers=h,
                                 json={})).status_code == 200

        starts = await _audit_rows(env, "review.start")
        assert len(starts) == 1, [e.actor_id for e in starts]
        assert starts[0].actor_id == first.id

        # A different reviewer opening the same document IS a new start.
        h = env.as_user(second)
        async with env.client() as c:
            assert (await c.get(f"/api/v1/documents/{doc_id}/validation",
                                headers=h)).status_code == 200

        starts = await _audit_rows(env, "review.start")
        assert len(starts) == 2
        assert {e.actor_id for e in starts} == {first.id, second.id}

    _run(body)


# --------------------------------------------------------------------------
# SEC-17 (F9) — the upload key is built from a basename
# --------------------------------------------------------------------------


def test_upload_filename_cannot_escape_the_org_prefix():
    key = storage._logical_key(str(ORG_A), "../../etc/passwd.pdf")
    assert key.startswith(f"{ORG_A}/")
    assert key.endswith("/passwd.pdf")
    assert ".." not in key

    win = storage._logical_key(str(ORG_A), r"..\..\windows\evil.pdf")
    assert win.startswith(f"{ORG_A}/") and ".." not in win and "\\" not in win

    # A name that is nothing but separators still yields a usable key.
    assert storage._logical_key(str(ORG_A), "../").endswith("/upload.bin")
    assert storage._logical_key(str(ORG_A), "").endswith("/upload.bin")


def test_traversal_filename_is_stored_under_the_org_prefix():
    async def body(env: Env):
        key, _ = await _seed_key(env, org_id=ORG_A, scopes=["documents:submit"])
        async with env.client() as c:
            up = await c.post(
                "/api/v1/documents",
                files={"file": ("../../escaped.pdf", b"%PDF-1.4 fake", "application/pdf")},
                headers={"X-API-Key": key},
            )
            assert up.status_code == 202, up.text
            doc_id = uuid.UUID(up.json()["document_id"])

        async with env.sm() as s:
            doc = await s.get(Document, doc_id)
        stored = Path(doc.storage_key)
        assert stored.name == "escaped.pdf"
        # The org prefix is intact and the path never climbed out of the upload root.
        assert str(ORG_A) in stored.as_posix()
        assert ".." not in stored.as_posix()
        assert storage._LOCAL_DIR.resolve() in stored.resolve().parents

    _run(body)
