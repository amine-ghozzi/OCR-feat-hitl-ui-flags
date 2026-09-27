"""variation_candidate idempotency: collapse duplicates + UNIQUE(org_id, raw_text, matched_to)

Revision ID: varcand_unique_0001
Revises:
Create Date: 2026-06-03

Incremental migration on top of the create_all-bootstrapped schema (the project seeds
dev/test DBs via ``Base.metadata.create_all``; this is the first Alembic delta on a prod
DB created that way). It makes the HITL feedback write path idempotent:

1. **Collapse** any pre-existing duplicate rows — fold each ``(org_id, raw_text,
   matched_to)`` group's ``seen_count`` into the earliest row, then drop the rest.
   This MUST run before the constraint, or its creation fails on the existing dupes.
2. **Add** the UNIQUE constraint that backs the ``ON CONFLICT``-style upsert in
   ``api/routers/review.py`` (``_upsert_variation_candidate``).

Postgres-targeted (uses window functions / ``array_agg``); SQLite test DBs carry the
constraint via ``create_all`` and never run this migration.
"""
from alembic import op

revision = "varcand_unique_0001"
down_revision = None
branch_labels = None
depends_on = None

_UQ = "uq_variation_candidate_org_raw_matched"

# Fold each duplicate group's seen_count into its keeper (earliest by created_at, id).
_COLLAPSE_SEEN_COUNT = """
WITH grp AS (
    SELECT (array_agg(id ORDER BY created_at, id))[1] AS keep_id,
           SUM(seen_count) AS total
    FROM variation_candidates
    GROUP BY org_id, raw_text, matched_to
    HAVING COUNT(*) > 1
)
UPDATE variation_candidates v
SET seen_count = g.total
FROM grp g
WHERE v.id = g.keep_id;
"""

# Delete the non-keeper duplicates (anything but the first row in its group).
_DELETE_DUPLICATES = """
DELETE FROM variation_candidates v
USING (
    SELECT id, ROW_NUMBER() OVER (
        PARTITION BY org_id, raw_text, matched_to
        ORDER BY created_at, id
    ) AS rn
    FROM variation_candidates
) d
WHERE v.id = d.id AND d.rn > 1;
"""


def upgrade() -> None:
    op.execute(_COLLAPSE_SEEN_COUNT)
    op.execute(_DELETE_DUPLICATES)
    op.create_unique_constraint(
        _UQ, "variation_candidates", ["org_id", "raw_text", "matched_to"]
    )


def downgrade() -> None:
    op.drop_constraint(_UQ, "variation_candidates", type_="unique")
