"""— shadows.supervisor_enabled per-shadow opt-in flag.

Revision ID: 0004_shadow_supervisor_enabled
Revises:    0003_identity_split
Create Date: 2026-05-14

Adds a single nullable Boolean column ``supervisor_enabled`` to the
``shadows`` table.  The Intelligent Supervisor (v0.4.0) was previously
gated by a global env flag; v0.4.1 lets the operator opt-in per shadow
so they can A/B test the supervisor on one specialist before flipping
the global switch.

The column is nullable + defaults to False so existing rows read as
"supervisor not opted-in" without an explicit UPDATE.  Forward-compat:
the Pydantic model treats missing/null as False.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision      = "0004_shadow_supervisor_enabled"
down_revision = "0003_identity_split"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column(
        "shadows",
        sa.Column(
            "supervisor_enabled",
            sa.Boolean(),
            nullable=True,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    # SQLite + Postgres both support DROP COLUMN with the right dialect.
    op.drop_column("shadows", "supervisor_enabled")
