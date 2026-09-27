"""Inspection-based Alembic startup sync (OPS-1 / OPS-2).

``python -m api.bootstrap`` seeds the schema with ``Base.metadata.create_all``, which builds
every table the ORM declares but never creates ``alembic_version``. A database in that state
is at the head *schema* while Alembic believes it is at no revision at all, so a plain
``upgrade`` would replay ``varcand_unique_0001`` / ``improvement_loop_0002`` against objects
that already exist, and fail. Volumes built before ``dc1016c`` are a second shape: also
unversioned, but genuinely missing ``corrections.statement_id`` / ``corrections.schema_version``,
which only ``correction_stmt_key_0003`` can add.

The revision to stamp is therefore **read off the live schema** — never assumed, never taken
from an environment flag. :func:`decide` is a pure function of two booleans; the runner's only
job is to answer those two booleans and execute what comes back.

Run: ``python -m api.migrate`` (see ``Dockerfile.api``: it runs after ``api.bootstrap`` and
before uvicorn, and exits non-zero on any Alembic failure so the server never starts on a
half-migrated database).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s")
logger = logging.getLogger("api.migrate")

# Repo root: src/api/migrate.py -> src/api -> src -> <root> (holds alembic.ini + alembic/).
REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

#: Revision that added ``corrections.statement_id`` / ``schema_version`` (result schema v2).
REVISION_0003 = "correction_stmt_key_0003"
#: Its predecessor — the last revision a pre-``dc1016c`` volume already satisfies.
REVISION_0002 = "improvement_loop_0002"

#: Table Alembic uses to record the current revision.
VERSION_TABLE = "alembic_version"
#: Table/column that separate a head-equivalent volume from a pre-``dc1016c`` one.
PROBE_TABLE = "corrections"
PROBE_COLUMN = "statement_id"


def decide(has_version_table: bool, has_0003_columns: bool) -> list[str]:
    """Return the Alembic commands to run, given the observed state of the database.

    Pure: no I/O, no environment, no clock. The three cases —

    (a) ``alembic_version`` present — Alembic already owns this database, so what it records
        is the truth; just move it forward.
    (b) no version table and no ``corrections.statement_id`` — a volume built before
        ``dc1016c``. It satisfies everything up to :data:`REVISION_0002` and nothing beyond,
        so stamp that and let ``upgrade`` actually add the missing columns.
    (c) no version table but the column is present — ``create_all`` has built the
        head-equivalent schema. Stamp :data:`REVISION_0003`, whose work is demonstrably
        already done, and let ``upgrade`` confirm there is nothing left.

    Case (c) is the user-accepted refinement of "never assume the head revision": the revision
    is chosen because the schema was inspected, not because it is the newest one. Every plan
    ends with ``upgrade head``, so a stamp is never the last word, and no plan names a revision
    that an observation did not justify.
    """
    if has_version_table:
        return ["upgrade head"]
    if has_0003_columns:
        return [f"stamp {REVISION_0003}", "upgrade head"]
    return [f"stamp {REVISION_0002}", "upgrade head"]


def _read_state(sync_conn: Connection) -> tuple[bool, bool]:
    """Inspector body — runs on the *sync* Connection handed over by ``run_sync``."""
    insp = inspect(sync_conn)
    has_version_table = insp.has_table(VERSION_TABLE)
    if not insp.has_table(PROBE_TABLE):
        # No `corrections` table at all: nothing has built the schema yet. Report the column
        # as absent rather than letting `get_columns` raise NoSuchTableError.
        return has_version_table, False
    columns = {col["name"] for col in insp.get_columns(PROBE_TABLE)}
    return has_version_table, PROBE_COLUMN in columns


async def inspect_state(url: str) -> tuple[bool, bool]:
    """Answer ``(has_version_table, has_0003_columns)`` for the database at ``url``.

    The application engine is async but ``sqlalchemy.inspect`` needs a sync Connection, so the
    reflection runs inside ``connection.run_sync``.
    """
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_read_state)
    finally:
        await engine.dispose()


def _database_url(override: str | None) -> str:
    """The DSN to inspect: ``--url`` when given, else the configured one."""
    if override:
        return override
    from api.settings import get_settings  # local: Settings validate at construction

    return str(get_settings().database_url)


def _run_alembic(command: str, url: str) -> int:
    """Run one Alembic command from the repo root; return its exit code.

    ``alembic.ini`` uses a relative ``script_location`` / ``prepend_sys_path``, so the
    subprocess must start in :data:`REPO_ROOT`. ``alembic/env.py`` reads the DSN from
    settings, so it travels through the environment.
    """
    argv = [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *shlex.split(command)]
    env = {**os.environ, "FINALZE_DATABASE_URL": url}
    logger.info("alembic %s", command)
    return subprocess.run(argv, cwd=str(REPO_ROOT), env=env, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m api.migrate",
        description="Bring the database to the head revision, deciding any stamp by inspection.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Inspect the database and print the decided plan; run no Alembic command.",
    )
    parser.add_argument(
        "--url", default=None, metavar="DSN",
        help="Inspect this DSN instead of the configured one (and pass it to Alembic). It "
             "chooses which database is read; it never influences which revision is stamped.",
    )
    args = parser.parse_args(argv)

    url = _database_url(args.url)
    try:
        has_version_table, has_0003_columns = asyncio.run(inspect_state(url))
    except Exception as exc:  # noqa: BLE001 — any driver error is fatal and must stay readable
        logger.error("Could not inspect the database: %s: %s", type(exc).__name__, exc)
        return 1

    plan = decide(has_version_table, has_0003_columns)
    logger.info(
        "Observed: %s=%s, %s.%s=%s -> plan: %s",
        VERSION_TABLE, has_version_table, PROBE_TABLE, PROBE_COLUMN, has_0003_columns,
        "; ".join(plan),
    )

    if args.dry_run:
        for command in plan:
            print(f"alembic {command}")
        return 0

    for command in plan:
        code = _run_alembic(command, url)
        if code != 0:
            logger.error("`alembic %s` failed with exit code %s - stopping.", command, code)
            return code
    logger.info("Database is at the head revision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
