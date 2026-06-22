"""Initial Systemu schema — all entity tables.

Revision ID: 0001_initial
Revises: (none)
Create Date: 2025-05-08

This migration creates the full baseline schema.
Existing file-vault installs switching to SYSTEMU_STORAGE=sqlite will
run this migration (creating an empty DB) and then either:
  a) start fresh (SYSTEMU_STORAGE=sqlite, fresh data dir), or
  b) run the one-time data migration script to import existing JSON files
     (future tooling — not included in this migration).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision      = "0001_initial"
down_revision = None
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── scrolls ──────────────────────────────────────────────────────────────
    op.create_table(
        "scrolls",
        sa.Column("id",                    sa.String(),  primary_key=True),
        sa.Column("name",                  sa.String(),  nullable=False),
        sa.Column("source_session_id",     sa.String(),  server_default=""),
        sa.Column("raw_instructions_path", sa.Text(),    server_default=""),
        sa.Column("narrative_md",          sa.Text(),    server_default=""),
        sa.Column("intent",                sa.Text(),    server_default=""),
        sa.Column("objectives",            sa.JSON(),    server_default="[]"),
        sa.Column("constraints",           sa.JSON(),    server_default="{}"),
        sa.Column("observed_preferences",  sa.JSON(),    server_default="{}"),
        sa.Column("action_blocks",         sa.JSON(),    server_default="[]"),
        sa.Column("activity_id",           sa.String(),  nullable=True),
        sa.Column("status",                sa.String(),  server_default="draft"),
        sa.Column("version",               sa.Integer(), server_default="1"),
        sa.Column("tags",                  sa.JSON(),    server_default="[]"),
        sa.Column("created_at",            sa.DateTime()),
        sa.Column("updated_at",            sa.DateTime()),
    )

    # ── tools ─────────────────────────────────────────────────────────────────
    op.create_table(
        "tools",
        sa.Column("id",                   sa.String(),  primary_key=True),
        sa.Column("name",                 sa.String(),  nullable=False),
        sa.Column("description",          sa.Text(),    server_default=""),
        sa.Column("tool_type",            sa.String(),  server_default="python_function"),
        sa.Column("parameters_schema",    sa.JSON(),    server_default="{}"),
        sa.Column("return_schema",        sa.JSON(),    server_default="{}"),
        sa.Column("implementation_notes", sa.Text(),    server_default=""),
        sa.Column("dependencies",         sa.JSON(),    server_default="[]"),
        sa.Column("implementation_path",  sa.Text(),    server_default=""),
        sa.Column("tool_md_path",         sa.Text(),    server_default=""),
        sa.Column("status",               sa.String(),  server_default="proposed"),
        sa.Column("forged_by_systemu",    sa.Boolean(), server_default="0"),
        sa.Column("enabled",              sa.Boolean(), server_default="0"),
        sa.Column("version",              sa.Integer(), server_default="1"),
        sa.Column("created_at",           sa.DateTime()),
        sa.Column("updated_at",           sa.DateTime()),
    )
    op.create_index("ix_tools_name", "tools", ["name"])

    # ── skills ────────────────────────────────────────────────────────────────
    op.create_table(
        "skills",
        sa.Column("id",                  sa.String(), primary_key=True),
        sa.Column("name",                sa.String(), nullable=False),
        sa.Column("description",         sa.Text(),   server_default=""),
        sa.Column("category",            sa.String(), server_default=""),
        sa.Column("proficiency_level",   sa.String(), server_default="intermediate"),
        sa.Column("evidence_scroll_ids", sa.JSON(),   server_default="[]"),
        sa.Column("required_tool_ids",   sa.JSON(),   server_default="[]"),
        sa.Column("required_tool_names", sa.JSON(),   server_default="[]"),
        sa.Column("instructions_md",     sa.Text(),   server_default=""),
        sa.Column("skill_md_path",       sa.Text(),   server_default=""),
        sa.Column("created_at",          sa.DateTime()),
        sa.Column("updated_at",          sa.DateTime()),
    )
    op.create_index("ix_skills_name", "skills", ["name"])

    # ── activities ────────────────────────────────────────────────────────────
    op.create_table(
        "activities",
        sa.Column("id",                 sa.String(),  primary_key=True),
        sa.Column("name",               sa.String(),  nullable=False),
        sa.Column("scroll_id",          sa.String(),  server_default=""),
        sa.Column("required_tool_ids",  sa.JSON(),    server_default="[]"),
        sa.Column("required_skill_ids", sa.JSON(),    server_default="[]"),
        sa.Column("missing_tools",      sa.JSON(),    server_default="[]"),
        sa.Column("assigned_shadow_id", sa.String(),  nullable=True),
        sa.Column("status",             sa.String(),  server_default="unassigned"),
        sa.Column("created_at",         sa.DateTime()),
        sa.Column("updated_at",         sa.DateTime()),
    )

    # ── shadows ───────────────────────────────────────────────────────────────
    op.create_table(
        "shadows",
        sa.Column("id",                    sa.String(), primary_key=True),
        sa.Column("name",                  sa.String(), nullable=False),
        sa.Column("description",           sa.Text(),   server_default=""),
        sa.Column("system_prompt",         sa.Text(),   server_default=""),
        sa.Column("assigned_activity_ids", sa.JSON(),   server_default="[]"),
        sa.Column("available_tool_ids",    sa.JSON(),   server_default="[]"),
        sa.Column("skill_ids",             sa.JSON(),   server_default="[]"),
        sa.Column("status",                sa.String(), server_default="dormant"),
        sa.Column("execution_log",         sa.JSON(),   server_default="[]"),
        sa.Column("evolution_history",     sa.JSON(),   server_default="[]"),
        sa.Column("memory_md_path",        sa.Text(),   server_default=""),
        sa.Column("memory_buffer_path",    sa.Text(),   server_default=""),
        sa.Column("created_at",            sa.DateTime()),
        sa.Column("updated_at",            sa.DateTime()),
    )

    # ── evolutions ────────────────────────────────────────────────────────────
    op.create_table(
        "evolutions",
        sa.Column("id",                 sa.String(),  primary_key=True),
        sa.Column("evolution_type",     sa.String(),  server_default="upgrade"),
        sa.Column("target_entity_type", sa.String(),  server_default=""),
        sa.Column("target_entity_ids",  sa.JSON(),    server_default="[]"),
        sa.Column("description",        sa.Text(),    server_default=""),
        sa.Column("rationale",          sa.Text(),    server_default=""),
        sa.Column("before_snapshot",    sa.JSON(),    server_default="{}"),
        sa.Column("after_snapshot",     sa.JSON(),    server_default="{}"),
        sa.Column("status",             sa.String(),  server_default="proposed"),
        sa.Column("proposed_at",        sa.DateTime()),
        sa.Column("resolved_at",        sa.DateTime(), nullable=True),
    )

    # ── notifications ─────────────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id",          sa.String(),  primary_key=True),
        sa.Column("title",       sa.String(),  nullable=False),
        sa.Column("message",     sa.Text(),    server_default=""),
        sa.Column("actions",     sa.JSON(),    server_default="[]"),
        sa.Column("context",     sa.JSON(),    server_default="{}"),
        sa.Column("status",      sa.String(),  server_default="pending"),
        sa.Column("created_at",  sa.DateTime()),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution",  sa.String(),   nullable=True),
    )
    op.create_index("ix_notifications_status", "notifications", ["status"])

    # ── shadow_memories ───────────────────────────────────────────────────────
    op.create_table(
        "shadow_memories",
        sa.Column("shadow_id",           sa.String(), primary_key=True),
        sa.Column("memory_md",           sa.Text(),   server_default=""),
        sa.Column("memory_buffer_jsonl", sa.Text(),   server_default=""),
    )

    # ── elder_memory ──────────────────────────────────────────────────────────
    op.create_table(
        "elder_memory",
        sa.Column("id",                  sa.Integer(), primary_key=True),
        sa.Column("memory_md",           sa.Text(),    server_default=""),
        sa.Column("memory_buffer_jsonl", sa.Text(),    server_default=""),
    )

    # ── chat_history ──────────────────────────────────────────────────────────
    op.create_table(
        "chat_history",
        sa.Column("rowid",   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts",      sa.String(),  nullable=False),
        sa.Column("data",    sa.JSON(),    nullable=False),
        sa.Column("created", sa.DateTime()),
    )
    op.create_index("ix_chat_history_ts", "chat_history", ["ts"])


def downgrade() -> None:
    op.drop_table("chat_history")
    op.drop_table("elder_memory")
    op.drop_table("shadow_memories")
    op.drop_index("ix_notifications_status", "notifications")
    op.drop_table("notifications")
    op.drop_table("evolutions")
    op.drop_table("shadows")
    op.drop_table("activities")
    op.drop_index("ix_skills_name", "skills")
    op.drop_table("skills")
    op.drop_index("ix_tools_name", "tools")
    op.drop_table("tools")
    op.drop_table("scrolls")
