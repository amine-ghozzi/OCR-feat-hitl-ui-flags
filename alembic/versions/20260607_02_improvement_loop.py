"""improvement loop: issues + remediation_actions tables

Revision ID: improvement_loop_0002
Revises: varcand_unique_0001
Create Date: 2026-06-07

Adds the dev-time control-loop ledger: detected `issues` and the `remediation_actions`
that fix them (with before/after for rollback). Enum-valued columns are stored as short
strings (the ORM `str`-enums emit their values).
"""
import sqlalchemy as sa
from alembic import op

revision = "improvement_loop_0002"
down_revision = "varcand_unique_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "issues",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=True, index=True),
        sa.Column("kind", sa.String(64), nullable=False, index=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open", index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("fingerprint", sa.String(128), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "remediation_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("issue_id", sa.Uuid(), sa.ForeignKey("issues.id"), nullable=False, index=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=True, index=True),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed", index=True),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("gate_result", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("remediation_actions")
    op.drop_table("issues")
