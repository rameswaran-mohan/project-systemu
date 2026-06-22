"""— shadows.specialty operator-labelled routing tag.

Revision ID: 0005_shadow_specialty
Revises:    0004_shadow_supervisor_enabled
Create Date: 2026-05-15

Adds a single nullable Text column ``specialty`` to the ``shadows``
table.  The v0.4.0 Intelligent Supervisor's affinity-routing preference
(v0.4.3-b) uses this as a categorical tag complementing the metric-based
shadow ranking.

The column defaults to empty string so existing rows read as "no
specialty set" without an explicit UPDATE.  The Pydantic model defaults
to ``""``; the routing logic treats empty as "no preference".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision      = "0005_shadow_specialty"
down_revision = "0004_shadow_supervisor_enabled"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column(
        "shadows",
        sa.Column(
            "specialty",
            sa.Text(),
            nullable=True,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("shadows", "specialty")
