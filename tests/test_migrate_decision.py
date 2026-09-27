"""WP-1.4 — the startup migration decision (`src/api/migrate.py`).

`decide()` is pure, so the three startup shapes are tested as plain input/output. The fourth
test pins the safety property the whole package exists for: whatever the inputs, the plan never
tells Alembic to assume the newest revision.

The last test covers the one piece of wiring `decide()` cannot: reading those two booleans off a
live database, where the engine is async but `sqlalchemy.inspect` needs a sync Connection. It
builds throwaway SQLite databases in `tmp_path` (never committed) in the three shapes the runner
must tell apart. The Postgres proof is `scripts/verify_migrations.sh` — migration 0001 is
Postgres-only, so the real chain cannot run here.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.migrate import REVISION_0002, REVISION_0003, decide, inspect_state  # noqa: E402

#: The one plan entry that must never be produced: it would skip `correction_stmt_key_0003` on a
#: volume that has not actually got its columns, which is exactly the OPS-2 failure.
FORBIDDEN_PLAN_ENTRY = "stamp head"


def test_fresh_create_all_stamps_0003():
    """No version table but the 0003 columns are there -> `create_all` built the head schema."""
    assert decide(has_version_table=False, has_0003_columns=True) == [
        f"stamp {REVISION_0003}",
        "upgrade head",
    ]


def test_pre_dc1016c_stamps_0002_then_upgrades():
    """No version table and no `corrections.statement_id` -> a volume from before `dc1016c`.

    Stamping 0002 is what lets the following upgrade actually run 0003 and add the columns.
    """
    assert decide(has_version_table=False, has_0003_columns=False) == [
        f"stamp {REVISION_0002}",
        "upgrade head",
    ]


def test_existing_version_table_only_upgrades():
    """Once Alembic owns the database, its own record is the truth — never re-stamp it."""
    assert decide(has_version_table=True, has_0003_columns=True) == ["upgrade head"]
    assert decide(has_version_table=True, has_0003_columns=False) == ["upgrade head"]


def test_never_emits_stamp_head():
    """For every possible observation, no plan assumes the head revision."""
    for has_version_table, has_0003_columns in itertools.product([True, False], repeat=2):
        plan = decide(has_version_table, has_0003_columns)
        assert FORBIDDEN_PLAN_ENTRY not in " ".join(plan), (
            f"{(has_version_table, has_0003_columns)} produced {plan}"
        )
        for entry in plan:
            assert " ".join(entry.split()) != FORBIDDEN_PLAN_ENTRY
            # A stamp is only ever of a revision justified by an observation.
            if entry.startswith("stamp "):
                assert entry.split(maxsplit=1)[1] in {REVISION_0002, REVISION_0003}
        # A stamp is never the last word.
        assert plan[-1] == "upgrade head"


def test_alembic_chain_has_one_head():
    """The chain is linear and ends at 0004 — no database involved.

    Two things depend on this. `api.migrate` always finishes with `upgrade head`, which is
    ambiguous the moment the chain branches; and `scripts/verify_migrations.sh` resolves the
    expected revision by asking for the head, refusing to continue if there is more than one.
    Walking back to `varcand_unique_0001` also catches an orphaned revision whose
    `down_revision` points outside the chain.
    """
    from alembic.config import Config  # noqa: PLC0415
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["audit_target_index_0004"]

    chain = []
    rev = script.get_revision("audit_target_index_0004")
    while rev is not None:
        chain.append(rev.revision)
        down = rev.down_revision
        # A tuple here would be a merge revision, i.e. the branch this test exists to forbid.
        assert not isinstance(down, tuple), f"{rev.revision} is a merge revision: {down}"
        rev = script.get_revision(down) if down else None

    assert chain == [
        "audit_target_index_0004",
        "correction_stmt_key_0003",
        "improvement_loop_0002",
        "varcand_unique_0001",
    ]
    assert REVISION_0003 in chain and REVISION_0002 in chain


def test_inspect_state_reads_schema_via_async_engine(tmp_path):
    """`inspect_state` reports the two booleans through an async engine + sync inspector."""
    pytest.importorskip("aiosqlite")
    from sqlalchemy import create_engine, text

    from api.db import Base
    import api.models  # noqa: F401 — registers every table on Base.metadata

    def build(name: str, *statements: str) -> str:
        path = tmp_path / name
        sync_engine = create_engine(f"sqlite:///{path.as_posix()}")
        try:
            Base.metadata.create_all(sync_engine)
            with sync_engine.begin() as conn:
                for statement in statements:
                    conn.execute(text(statement))
        finally:
            sync_engine.dispose()
        return f"sqlite+aiosqlite:///{path.as_posix()}"

    # (c) what `api.bootstrap` leaves behind: head-equivalent schema, unversioned.
    fresh = build("fresh.db")
    # (b) a pre-`dc1016c` volume: same, minus the two columns 0003 adds.
    pre_dc1016c = build(
        "pre_dc1016c.db",
        "ALTER TABLE corrections DROP COLUMN statement_id",
        "ALTER TABLE corrections DROP COLUMN schema_version",
    )
    # (a) a database Alembic already owns.
    versioned = build(
        "versioned.db",
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)",
        f"INSERT INTO alembic_version (version_num) VALUES ('{REVISION_0003}')",
    )

    assert asyncio.run(inspect_state(fresh)) == (False, True)
    assert asyncio.run(inspect_state(pre_dc1016c)) == (False, False)
    assert asyncio.run(inspect_state(versioned)) == (True, True)

    # And the decisions those observations drive.
    assert decide(*asyncio.run(inspect_state(fresh)))[0] == f"stamp {REVISION_0003}"
    assert decide(*asyncio.run(inspect_state(pre_dc1016c)))[0] == f"stamp {REVISION_0002}"
    assert decide(*asyncio.run(inspect_state(versioned))) == ["upgrade head"]
