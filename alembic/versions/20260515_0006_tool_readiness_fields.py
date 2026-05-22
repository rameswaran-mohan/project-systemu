"""+ v0.5.0-b — tool readiness + evolution audit columns.

Revision ID: 0006_tool_readiness_fields
Revises:    0005_shadow_specialty
Create Date: 2026-05-15

Adds four nullable columns to the ``tools`` table:

* ``dry_run_status``         — "not_run" | "passed" | "failed" | "skipped"
* ``dry_run_evidence``       — JSON: last params used, error, elapsed_ms
* ``last_successful_params`` — JSON list of observed-successful param sets
                               (capped at last 20), used by v0.5.0-d's
                               backward-compat replay
* ``evolution_history``      — JSON list of recalibration audit entries

Existing rows read as defaults (status "not_run", empty JSON containers)
without an explicit UPDATE.  The Pydantic model handles missing/null
gracefully.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision      = "0006_tool_readiness_fields"
down_revision = "0005_shadow_specialty"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column(
        "tools",
        sa.Column("dry_run_status", sa.String(),
                   nullable=True, server_default="not_run"),
    )
    op.add_column(
        "tools",
        sa.Column("dry_run_evidence", sa.JSON(),
                   nullable=True, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "tools",
        sa.Column("last_successful_params", sa.JSON(),
                   nullable=True, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "tools",
        sa.Column("evolution_history", sa.JSON(),
                   nullable=True, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("tools", "evolution_history")
    op.drop_column("tools", "last_successful_params")
    op.drop_column("tools", "dry_run_evidence")
    op.drop_column("tools", "dry_run_status")
