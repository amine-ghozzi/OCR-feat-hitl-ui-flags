"""corrections: statement-scoped addressing (result schema v2)

Revision ID: correction_stmt_key_0003
Revises: improvement_loop_0002
Create Date: 2026-07-30

`canonical_tables` is now keyed per STATEMENT rather than per table (result
`schema_version` 2), so `(table_index, row_index, column_name)` alone no longer
identifies a cell unambiguously once a document's statements and tables stop
coinciding — a blank page between two statements is enough.

Both columns are nullable and purely additive: existing rows keep `table_index`
semantics and are read as v1. Nothing is backfilled, because a v1 row's
`table_index` is still correct under its own keying — rewriting it would be the
only way to actually lose data here.
"""
import sqlalchemy as sa
from alembic import op

revision = "correction_stmt_key_0003"
down_revision = "improvement_loop_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("corrections", sa.Column("statement_id", sa.Integer(), nullable=True))
    op.add_column(
        "corrections",
        sa.Column("schema_version", sa.Integer(), nullable=True, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("corrections", "schema_version")
    op.drop_column("corrections", "statement_id")
