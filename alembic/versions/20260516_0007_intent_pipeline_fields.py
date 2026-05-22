"""— intent-aware pipeline columns.

Revision ID: 0007_intent_pipeline_fields
Revises:    0006_tool_readiness_fields
Create Date: 2026-05-16

Adds nullable columns across three tables to support the intent-aware
extraction pipeline (Stages 2 / 3.5 / 5):

* **scrolls**:
  - ``expected_outcome`` — concrete "what success looks like" description
    distinct from intent (Stage 2 / v0.6.0-c)

* **skills** (Stage 3.5 / v0.6.0-d.5 intent contract + recalibration):
  - ``target_outcomes``     — JSON list, intent components served
  - ``produces``            — JSON list, output types yielded
  - ``effectiveness_score`` — Float, decays on downstream failure
  - ``skill_version``       — Integer, bumps on RECALIBRATE_SKILL
  - ``evolution_history``   — JSON list, recalibration audit

* **activities**:
  - ``intent_snapshot`` — Frozen scroll intent at extraction time so the
    Stage 5 shadow tiebreak doesn't re-load the scroll per decision
    (Stage 5 / v0.6.0-f)

All columns are nullable with sensible defaults so existing rows read
correctly without an explicit UPDATE.  Pydantic models handle null
gracefully by applying their model defaults.

**Anthropic Agent Skills Standard compliance preserved**: none of the new
skill columns are exported to the portable SKILL.md frontmatter (verified
by test_skill_md_does_not_contain_new_fields).  SKILL.md stays the
standard 5-key export; new fields live only in the JSON sidecar + SQLite.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision      = "0007_intent_pipeline_fields"
down_revision = "0006_tool_readiness_fields"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── scrolls.expected_outcome (v0.6.0-c) ───────────────────────────────
    op.add_column(
        "scrolls",
        sa.Column("expected_outcome", sa.Text(),
                  nullable=True, server_default=""),
    )

    # ── skills.* (v0.6.0-d.5 — intent contract + recalibration audit) ─────
    op.add_column(
        "skills",
        sa.Column("target_outcomes", sa.JSON(),
                  nullable=True, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "skills",
        sa.Column("produces", sa.JSON(),
                  nullable=True, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "skills",
        sa.Column("effectiveness_score", sa.Float(),
                  nullable=True, server_default="1.0"),
    )
    op.add_column(
        "skills",
        sa.Column("skill_version", sa.Integer(),
                  nullable=True, server_default="1"),
    )
    op.add_column(
        "skills",
        sa.Column("evolution_history", sa.JSON(),
                  nullable=True, server_default=sa.text("'[]'")),
    )

    # ── activities.intent_snapshot (v0.6.0-f) ─────────────────────────────
    op.add_column(
        "activities",
        sa.Column("intent_snapshot", sa.Text(),
                  nullable=True, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("activities", "intent_snapshot")
    op.drop_column("skills", "evolution_history")
    op.drop_column("skills", "skill_version")
    op.drop_column("skills", "effectiveness_score")
    op.drop_column("skills", "produces")
    op.drop_column("skills", "target_outcomes")
    op.drop_column("scrolls", "expected_outcome")
