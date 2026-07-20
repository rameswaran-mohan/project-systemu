"""Tool-model field parity — columns ``ToolRow`` had stopped tracking.

Revision ID: 0011_tool_model_field_parity
Revises:    0010_operator_decisions
Create Date: 2026-07-19

Thirteen fields existed on the Pydantic ``Tool`` but had no column here, so on a
SQL backend every one of them silently read back as its model DEFAULT while the
file vault (which persists the whole model as JSON) kept them.

``effect_tags`` is the load-bearing one: it feeds the action gate, so the SAME
tool was governed differently depending on which storage backend was configured
— a ``net_read`` tool scored ALLOW on the file vault and REQUIRE_APPROVAL here,
and a ``local_delete``-capable shell tool lost its approval card entirely
(the command-gate carve-out delegates when the tag set is a SUBSET of the
delegable set, and the empty set is a subset of everything).

They are added TOGETHER on purpose. ``effect_tags`` and ``trusted_inprocess``
currently cancel: a forged money_move tool loses the tag that forces isolation,
but also loses the ``trusted_inprocess=True`` that would let it skip isolation,
so it still runs isolated. Restoring either one ALONE re-opens that hole
(§13.3 — forged money-capable code in-daemon at full privilege with an ambient
secret). Do not split this revision.

All columns are nullable with a server default, so existing rows keep every
value they had and read the new columns as NULL, which ``_row_to_tool`` maps
back to the model default. No UPDATE is issued and no existing column is
touched, so this is non-destructive and safe to re-run against a store that has
already been upgraded (alembic's version table makes the re-run a no-op; the
``SqliteVault._upgrade_schema`` ALTER path is separately idempotent because the
duplicate-column error is caught).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision      = "0011_tool_model_field_parity"
down_revision = "0010_operator_decisions"
branch_labels = None
depends_on    = None


# (column, type, server_default) — mirrors ToolRow in systemu/storage/sqlite/models.py
#
# Boolean defaults use ``sa.false()``, NOT ``sa.text("0")``. This migration runs
# on Postgres as well as SQLite (docker/entrypoint.sh runs `alembic upgrade head`
# whenever SYSTEMU_DATABASE_URL is set, and the docker-* compose profiles always
# set it to postgresql://), and Postgres has no implicit integer→boolean cast:
#
#     ALTER TABLE tools ADD COLUMN trusted_inprocess BOOLEAN DEFAULT 0
#     → DatatypeMismatch: column "trusted_inprocess" is of type boolean but
#       default expression is of type integer            [PostgreSQL 16.2]
#
# Postgres DDL is transactional, so that rejection rolled back the WHOLE
# revision — effect_tags included — and the entrypoint's soft-fallback then
# `alembic stamp head`s it, so the columns are never added and never retried.
# ``sa.false()`` is dialect-aware: it renders `false` on Postgres and `0` on
# SQLite, so the SQLite DDL is byte-identical to what shipped. Revisions 0004
# and 0009 already use it.
#
# The sa.JSON() defaults are deliberately left uncast: Postgres coerces the
# unknown-typed literal and stores `'[]'::json`, and writing an explicit
# `::json` cast here would emit it on SQLite too, which cannot parse it.
# ``forge_reattempts`` is an Integer, so a bare `0` is correct on both.
# Pinned per-dialect in tests/test_migration_0011_postgres_ddl.py.
_COLUMNS = [
    ("requires_credentials",          sa.JSON(),    sa.text("'[]'")),
    ("forged_by_execution_id",        sa.String(),  None),
    ("grounding_inputs",              sa.JSON(),    sa.text("'[]'")),
    ("effect_tags",                   sa.JSON(),    sa.text("'[]'")),
    ("external_verification_channel", sa.String(),  None),
    ("trusted_inprocess",             sa.Boolean(), sa.false()),
    ("forge_reattempts",              sa.Integer(), sa.text("0")),
    ("forge_rejected",                sa.Boolean(), sa.false()),
    ("is_action_tool",                sa.Boolean(), sa.false()),
    ("toolset",                       sa.String(),  None),
    ("max_result_size_chars",         sa.Integer(), None),
    ("timeout_seconds",               sa.Integer(), None),
    ("check_fn_name",                 sa.String(),  None),
]


def upgrade() -> None:
    for name, type_, default in _COLUMNS:
        op.add_column(
            "tools",
            sa.Column(name, type_, nullable=True, server_default=default),
        )


def downgrade() -> None:
    for name, _type, _default in reversed(_COLUMNS):
        op.drop_column("tools", name)
