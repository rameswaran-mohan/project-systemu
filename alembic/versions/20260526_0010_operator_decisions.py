"""v0.8.0 Pattern 1: operator_decisions table.

Revision ID: 0010_operator_decisions
Revises:    0009_recovery_path
Create Date: 2026-05-26

Adds the ``operator_decisions`` table — the persisted state of the
OperatorDecisionQueue (subprocess→dashboard decision handoff).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_operator_decisions"
down_revision = "0009_recovery_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_decisions",
        sa.Column("id",          sa.String(), primary_key=True),
        sa.Column("title",       sa.String(), nullable=False, server_default=""),
        sa.Column("body",        sa.Text(),   nullable=False, server_default=""),
        sa.Column("options",     sa.JSON(),   nullable=False),
        sa.Column("context",     sa.JSON(),   nullable=False),
        sa.Column("dedup_key",   sa.String(), nullable=False, server_default=""),
        sa.Column("status",      sa.String(), nullable=False, server_default="pending"),
        sa.Column("choice",      sa.String(), nullable=True),
        sa.Column("created_at",  sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_operator_decisions_dedup_key", "operator_decisions",
        ["dedup_key"], unique=False,
    )
    op.create_index(
        "ix_operator_decisions_status", "operator_decisions",
        ["status"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_operator_decisions_status", table_name="operator_decisions")
    op.drop_index("ix_operator_decisions_dedup_key", table_name="operator_decisions")
    op.drop_table("operator_decisions")
