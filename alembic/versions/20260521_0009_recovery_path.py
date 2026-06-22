"""recovery path: tool_dep_approvals table.

Revision ID: 0009_recovery_path
Revises:    0008_pipeline_trace
Create Date: 2026-05-21

Adds the ``tool_dep_approvals`` table — the source-of-truth for
operator-approved third-party Python packages used by forged tools.

Note: the ``tools.dry_run_evidence`` JSON column already exists from
migration 0006 (tool_readiness_fields); this migration does not re-add it.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_recovery_path"
down_revision = "0008_pipeline_trace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_dep_approvals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("package_name", sa.String(), nullable=False),
        sa.Column("package_version_spec", sa.String(), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column(
            "approved_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "source",
            sa.String(),
            nullable=False,
            server_default="dashboard",
        ),
        sa.Column(
            "baked_in_image",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_tool_dep_approvals_package",
        "tool_dep_approvals",
        ["package_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tool_dep_approvals_package", table_name="tool_dep_approvals")
    op.drop_table("tool_dep_approvals")
