"""Async SQLAlchemy engine, session factory, and the per-request session dependency.

One ``AsyncSession`` per request via :func:`get_async_session` (yield + context-manager
cleanup). ``expire_on_commit=False`` so objects stay usable after commit in async flows.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from api.settings import get_settings

_settings = get_settings()

# `pool_pre_ping` avoids stale-connection errors after DB restarts / idle periods.
engine = create_async_engine(str(_settings.database_url), pool_pre_ping=True, future=True)

async_session_maker = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models (also used as Alembic ``target_metadata``)."""


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one session per request, always closed."""
    async with async_session_maker() as session:
        yield session
