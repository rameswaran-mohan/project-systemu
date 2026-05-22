"""— PipelineTrace column on scrolls.

Revision ID: 0008_pipeline_trace
Revises:    0007_intent_pipeline_fields
Create Date: 2026-05-17

Adds a single JSON column to support per-stage pipeline observability
(Stages 1/2/3.5/6 each append a TraceEvent describing what they decided).
Surfaced on the /scrolls UI as a warning badge + Pipeline Trace panel.

* **scrolls**:
  - ``pipeline_trace`` — JSON list[TraceEvent]; defaults to [] for legacy rows

The new ``ScrollStatus.VALIDATOR_BLOCKED`` enum value does not require a
schema change on SQLite (the status column is just a string with no DB-level
check constraint in the v0.5.0+ schema).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_pipeline_trace"
down_revision = "0007_intent_pipeline_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add the JSON column with server default '[]' so existing rows are valid
    with op.batch_alter_table("scrolls") as batch:
        batch.add_column(
            sa.Column("pipeline_trace", sa.JSON(), nullable=True, server_default="[]")
        )

    # 2. Explicit backfill to avoid NULL ambiguity on legacy rows
    op.execute("UPDATE scrolls SET pipeline_trace = '[]' WHERE pipeline_trace IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("scrolls") as batch:
        batch.drop_column("pipeline_trace")
