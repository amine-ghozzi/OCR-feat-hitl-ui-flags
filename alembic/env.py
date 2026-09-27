"""Alembic environment — async (context7-validated pattern).

The DB URL comes from settings (`FINALZE_DATABASE_URL`), not `alembic.ini`. Online mode
uses `async_engine_from_config` + `connection.run_sync(do_run_migrations)` driven by
`asyncio.run`. `target_metadata = Base.metadata` (importing `api.models` registers every table).

Generate the first migration once Postgres is reachable:
    FINALZE_DATABASE_URL=... alembic revision --autogenerate -m "initial schema"
    FINALZE_DATABASE_URL=... alembic upgrade head
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make `src` importable for the ORM metadata.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from api.db import Base  # noqa: E402
import api.models  # noqa: E402,F401  — registers all tables on Base.metadata
from api.settings import get_settings  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime DSN (kept out of alembic.ini).
config.set_main_option("sqlalchemy.url", str(get_settings().database_url))

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
