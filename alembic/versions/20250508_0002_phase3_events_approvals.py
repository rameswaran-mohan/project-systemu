"""Phase 3 — cross-process event streaming + approval gate tables.

Revision ID: 0002_phase3_events_approvals
Revises: 0001_initial
Create Date: 2025-05-08

Adds two tables required for SqliteEventBroker and SqliteApprovalGate:

  events    — one row per published event; poller bridges worker→dashboard.
  approvals — one row per structured approval request; worker blocks until
              the dashboard user resolves it (or it times out).

Both tables are additive — no existing column is changed.
Alembic ensures idempotent upgrades via checkfirst=True on create_all, but
explicit ops here let Alembic track state correctly for future migrations.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision      = "0002_phase3_events_approvals"
down_revision = "0001_initial"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── events ────────────────────────────────────────────────────────────────
    # Monotonic auto-increment id used as watermark by SqliteEventBroker poller.
    op.create_table(
        "events",
        sa.Column("id",      sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts",      sa.DateTime(), nullable=False),
        sa.Column("source",  sa.String(),   nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(),     nullable=False),
    )
    op.create_index("ix_events_ts",     "events", ["ts"])
    op.create_index("ix_events_source", "events", ["source"])

    # ── approvals ─────────────────────────────────────────────────────────────
    # status lifecycle: "pending" → "resolved" | "timed_out"
    op.create_table(
        "approvals",
        sa.Column("request_id",  sa.String(),  primary_key=True),
        sa.Column("title",       sa.String(),  nullable=False, server_default=""),
        sa.Column("message",     sa.Text(),    server_default=""),
        sa.Column("options",     sa.JSON(),    server_default="[]"),
        sa.Column("context",     sa.JSON(),    server_default="{}"),
        sa.Column("status",      sa.String(),  nullable=False, server_default="pending"),
        sa.Column("choice",      sa.String(),  nullable=True),
        sa.Column("default",     sa.String(),  server_default=""),
        sa.Column("timeout_s",   sa.Float(),   server_default="120.0"),
        sa.Column("created_at",  sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_approvals_status", "approvals", ["status"])


def downgrade() -> None:
    op.drop_index("ix_approvals_status", "approvals")
    op.drop_table("approvals")
    op.drop_index("ix_events_source", "events")
    op.drop_index("ix_events_ts", "events")
    op.drop_table("events")
