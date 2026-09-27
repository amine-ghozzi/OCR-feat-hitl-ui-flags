# Alembic migration versions

The ORM metadata (`src/api/models.py`) is the source of truth; `alembic/env.py` reads the DSN
from settings (`FINALZE_DATABASE_URL`) and uses `Base.metadata` as `target_metadata`.

## The chain

| Revision | File | What it does |
|---|---|---|
| `varcand_unique_0001` | `20260603_01_varcand_unique.py` | Collapses duplicate `variation_candidates`, then adds `uq_variation_candidate_org_raw_matched`. **Postgres-only** (window functions / `array_agg`). |
| `improvement_loop_0002` | `20260607_02_improvement_loop.py` | Creates `issues` and `remediation_actions`. |
| `correction_stmt_key_0003` | `20260730_03_correction_statement_key.py` | Adds `corrections.statement_id` and `corrections.schema_version` (result schema v2). |
| `audit_target_index_0004` | `20260918_04_audit_target_index.py` | Creates `ix_audit_events_target` on `audit_events(target_type, target_id)`. WP-1.3 made that pair a request-path lookup (`GET /documents/{id}/audit`, and the UI-5 "review.start once per (document, actor)" guard on every validation view and assign). **head** |

`ix_audit_events_target` is deliberately **not** declared on the `AuditEvent` model. `bootstrap`
runs `create_all` and `api.migrate` then stamps `correction_stmt_key_0003` and upgrades: if the
index were on the model too, `create_all` would build it and 0004 would fail on the duplicate.
Schema objects introduced by a revision after the `create_all` baseline belong in the revision
only.

New revision (needs a live Postgres — autogenerate reflects the database):

```bash
FINALZE_DATABASE_URL=postgresql+asyncpg://finalze:finalze@localhost:5432/finalze \
  venv/Scripts/python -m alembic revision --autogenerate -m "..."
```

## Why startup does not simply run `upgrade`

`python -m api.bootstrap` builds the schema with `Base.metadata.create_all`, which creates every
table the ORM declares but **never creates `alembic_version`**. On such a database `alembic
current` prints nothing, and an `upgrade` would replay 0001 and 0002 against objects that already
exist and fail. Some deployed volumes are a second shape: also unversioned, but built before
`dc1016c` and so genuinely missing the two columns 0003 adds.

`python -m api.migrate` (run by `Dockerfile.api` between `api.bootstrap` and uvicorn) resolves
this by **inspecting the schema** — two booleans, `alembic_version` present and
`corrections.statement_id` present — and choosing accordingly. It never assumes the newest
revision, and nothing about the choice comes from an environment flag.

| # | `alembic_version` | `corrections.statement_id` | Meaning | Commands |
|---|---|---|---|---|
| a | present | either | Alembic already owns this database | `upgrade head` |
| b | absent | **absent** | volume built before `dc1016c` | `stamp improvement_loop_0002`, then `upgrade head` (0003 adds the columns) |
| c | absent | present | `create_all` built the 0003-equivalent schema (fresh volume, or today's prod) | `stamp correction_stmt_key_0003`, then `upgrade head` (which now **applies 0004**) |

Case (c) is the accepted refinement of the "never assume the head revision" rule: the revision is
stamped because the schema was inspected and 0003's work was observed to be already done — not
because it happens to be newest. Every plan still ends with `upgrade head`, so a stamp is never
the last word.

That last clause stopped being a formality once `audit_target_index_0004` existed. `create_all`
builds what the **models** declare, and the index is not one of them (see above), so on a fresh
or `create_all` volume the `upgrade head` after the stamp is no longer a no-op confirmation: it
runs 0004 and creates `ix_audit_events_target`. The inspection in `decide()` is unchanged — it
only ever asked whether 0003's *columns* are present, which is still the right question for
choosing the stamp — but the stamp is now deliberately one revision behind the head, and the
upgrade closes the gap. Any future revision adding schema beyond the `create_all` baseline
inherits the same shape.

Inspect without changing anything:

```bash
python -m api.migrate --dry-run             # against the configured DSN
python -m api.migrate --dry-run --url ...   # against another DSN (inspection target only)
```

### Known gap in case (b)

Stamping `improvement_loop_0002` marks 0001 and 0002 as done without running them. For a
pre-`dc1016c` volume that is correct for 0002's tables (they exist), but if that volume's
`variation_candidates` table predates 0001 it will **not** have the
`uq_variation_candidate_org_raw_matched` constraint: `create_all` creates missing tables and never
alters existing ones, and the stamp means 0001 will never run. The upsert in
`api/routers/review.py` (`_upsert_variation_candidate`) depends on that constraint. Closing this
would need a fourth case that inspects the constraint too; it is out of scope for WP-1.4 and is
recorded here so the next person does not rediscover it in production.

## Verifying

`scripts/verify_migrations.sh --yes` is the integration proof (Postgres/compose only, since 0001
cannot run on SQLite): it drives a fresh volume and a simulated pre-`dc1016c` volume through a
container start and asserts `alembic current` reports `<head> (head)` both times, that the
dropped columns come back, that `ix_audit_events_target` exists on both paths, and that a
statement-scoped correction row writes.

The expected head is **read from the chain at runtime** (`alembic heads` inside the api
container, refusing to continue if the chain has branched) rather than hardcoded — 0004 broke
the previous hardcoded `correction_stmt_key_0003` assertion on every path, and a script that
needs editing for each new revision is a script that silently rots. The simulated pre-`dc1016c`
volume drops the index along with the columns, so the simulation stays faithful to a volume that
never had it.

`tests/test_migrate_decision.py` unit-tests the decision function and pins the chain to a single
head reaching back to `varcand_unique_0001` — that one needs no database.
