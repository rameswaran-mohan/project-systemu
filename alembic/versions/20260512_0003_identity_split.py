"""— identity split: shadows.identity_block + accumulated_voice.

Revision ID: 0003_identity_split
Revises: 0002_phase3_events_approvals
Create Date: 2026-05-12

Adds two columns to the ``shadows`` table to support the v0.3 identity
split (see ``docs/memory-model.md``):

* ``identity_block``    — operator-editable persona contract
* ``accumulated_voice`` — consolidator-grown demonstrated traits

The legacy ``system_prompt`` column is preserved and backfilled with
its existing value as ``identity_block`` for every row.  Old readers
that still query ``system_prompt`` keep working; new code reads the
two split columns directly through the Pydantic model.

Both columns default to empty strings — pre-migration rows pick up the
default + a one-time backfill so the runtime always has something to
compose into the system_prompt at execution time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision      = "0003_identity_split"
down_revision = "0002_phase3_events_approvals"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # Add the two new columns with empty-string defaults.  SQLite + Postgres
    # both support ADD COLUMN with a default; the default is applied to
    # existing rows by the engine.
    op.add_column(
        "shadows",
        sa.Column("identity_block", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "shadows",
        sa.Column("accumulated_voice", sa.Text(), nullable=False, server_default=""),
    )

    # One-time backfill — copy every existing system_prompt into
    # identity_block so the runtime composition has the same content as
    # pre-migration.  accumulated_voice stays empty until the consolidator
    # writes the first lesson.
    op.execute(
        "UPDATE shadows SET identity_block = system_prompt "
        "WHERE identity_block = '' AND system_prompt != ''"
    )


def downgrade() -> None:
    # Drop the new columns — system_prompt was preserved unchanged so the
    # pre-shape is fully restored.
    op.drop_column("shadows", "accumulated_voice")
    op.drop_column("shadows", "identity_block")
