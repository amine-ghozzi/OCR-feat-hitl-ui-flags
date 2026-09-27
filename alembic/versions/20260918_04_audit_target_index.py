"""audit_events: index the (target_type, target_id) lookup

Revision ID: audit_target_index_0004
Revises: correction_stmt_key_0003
Create Date: 2026-09-18

WP-1.3 turned `audit_events` from a write-mostly table into one that is *read on the request
path*: `GET /documents/{id}/audit` filters on `(target_type, target_id)`, and the UI-5
"review.start once per (document, actor)" guard runs the same filter on every validation view
and assign. Without an index each of those is a full scan of a table that only ever grows.

`target_id` is a String column holding a stringified UUID, so a plain composite B-tree over the
two columns serves the equality lookup exactly. Non-unique on purpose: many events share a
target — that is the point of a trail.

The index is deliberately **not** declared on the `AuditEvent` model: `bootstrap._create_tables`
runs `create_all`, and `api.migrate.decide` then stamps `correction_stmt_key_0003` and upgrades.
Declaring it in both places would have `create_all` build it and this revision fail on the
duplicate.
"""
import sqlalchemy as sa  # noqa: F401  (imported for parity with the other revisions)
from alembic import op

revision = "audit_target_index_0004"
down_revision = "correction_stmt_key_0003"
branch_labels = None
depends_on = None

_INDEX = "ix_audit_events_target"


def upgrade() -> None:
    op.create_index(_INDEX, "audit_events", ["target_type", "target_id"], unique=False)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="audit_events")
