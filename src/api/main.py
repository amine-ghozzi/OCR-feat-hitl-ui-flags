"""FastAPI application factory.

`create_app()` returns a fresh app (tests build one and override dependencies). Auth routers
come from fastapi-users; our routers mount under `/api/v1`; `/healthz`+`/readyz` sit at the root.
Cross-cutting middleware (CORS, rate-limit, RFC 9457 errors, Prometheus) is added in Phase 5.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator

from api.errors import register_error_handlers
from api.middleware import RequestIDMiddleware
from api.ratelimit import close_limiter, init_limiter
from api.routers import (
    admin_users, config, documents, feedback, health, improvement, partners, review, webhooks,
)
from api.schemas import UserRead, UserUpdate
from api.settings import get_settings
from api.users import auth_backend, fastapi_users

_settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_limiter()  # no-op without Redis (dev/tests)
    try:
        yield
    finally:
        await close_limiter()


def create_app() -> FastAPI:
    app = FastAPI(
        title="FinAlze API",
        version="1",
        root_path=_settings.root_path,
        docs_url="/docs" if _settings.docs_enabled else None,
        redoc_url="/redoc" if _settings.docs_enabled else None,
        lifespan=lifespan,
    )

    # Cross-cutting: CORS, request-id, RFC 9457 errors, Prometheus /metrics.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIDMiddleware)
    register_error_handlers(app)
    # Fresh registry per app so repeated create_app() (tests) doesn't duplicate global metrics.
    Instrumentator(registry=CollectorRegistry()).instrument(app).expose(
        app, endpoint="/metrics", include_in_schema=False
    )

    # Root-level ops probes.
    app.include_router(health.router)

    api = APIRouter(prefix="/api/v1")
    # fastapi-users: login/logout, reset, verify, user management.
    # NO register router (SEC-1): accounts are provisioned by an org admin through
    # `POST /api/v1/users` (routers/admin_users.py), never by an anonymous caller.
    api.include_router(fastapi_users.get_auth_router(auth_backend), prefix="/auth/jwt", tags=["auth"])
    api.include_router(fastapi_users.get_reset_password_router(), prefix="/auth", tags=["auth"])
    api.include_router(fastapi_users.get_verify_router(UserRead), prefix="/auth", tags=["auth"])
    api.include_router(admin_users.router)
    api.include_router(
        fastapi_users.get_users_router(UserRead, UserUpdate), prefix="/users", tags=["users"]
    )
    # Domain routers.
    api.include_router(documents.router)
    api.include_router(review.router)
    api.include_router(feedback.router)
    api.include_router(config.router)
    api.include_router(webhooks.router)
    api.include_router(partners.router)
    api.include_router(improvement.router)

    app.include_router(api)
    return app


app = create_app()
