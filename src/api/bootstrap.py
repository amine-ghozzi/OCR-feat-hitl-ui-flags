"""Dev/compose bootstrap: create tables (idempotent) and an initial superuser.

For production, prefer Alembic migrations (`alembic upgrade head`) over `create_all`; this
exists so `docker compose up` works out-of-the-box before the first migration is generated.
Run: ``python -m api.bootstrap``.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import select

from api.db import Base, async_session_maker, engine
import api.models  # noqa: F401 — register all tables on Base
from api.models import Org, User
from api.users import password_helper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api.bootstrap")


async def _create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Schema ensured (create_all).")


async def _ensure_admin() -> None:
    email = os.environ.get("FINALZE_ADMIN_EMAIL")
    password = os.environ.get("FINALZE_ADMIN_PASSWORD")
    if not (email and password):
        logger.info("No FINALZE_ADMIN_EMAIL/PASSWORD set — skipping admin bootstrap.")
        return
    async with async_session_maker() as session:
        existing = (await session.execute(select(User).where(User.email == email))).scalars().first()
        if existing is not None:
            logger.info("Admin %s already exists.", email)
            return
        org = Org(name="Default")
        session.add(org)
        await session.flush()
        session.add(User(
            email=email, hashed_password=password_helper.hash(password),
            is_active=True, is_superuser=True, is_verified=True,
            role="admin", org_id=org.id, full_name="Bootstrap Admin",
        ))
        await session.commit()
        logger.info("Created bootstrap admin %s (org=%s).", email, org.id)


async def _run() -> None:
    await _create_tables()
    await _ensure_admin()
    await engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
