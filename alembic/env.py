"""Alembic env.py — wires SQLAlchemy metadata for auto-generate migrations.

The database URL is resolved in this priority order:
  1. SYSTEMU_DATABASE_URL environment variable
  2. DATABASE_URL environment variable (production / docker-compose)
  3. alembic.ini sqlalchemy.url value (local fallback)

To generate a new migration after editing models.py:
    alembic revision --autogenerate -m "describe_change"

To apply all pending migrations:
    alembic upgrade head

To downgrade one step:
    alembic downgrade -1
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import the shared Base so Alembic can inspect the ORM models.
from systemu.storage.sqlite.models import Base

# ── Alembic Config object ─────────────────────────────────────────────────────
config = context.config
fileConfig(config.config_file_name)

# ── Target metadata for --autogenerate ───────────────────────────────────────
target_metadata = Base.metadata


def _get_url() -> str:
    """Resolve the database URL from env or alembic.ini."""
    return (
        os.environ.get("SYSTEMU_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )


# ── Offline mode (generate SQL without a live connection) ────────────────────

def run_migrations_offline() -> None:
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online mode (apply migrations against a live connection) ─────────────────

def run_migrations_online() -> None:
    configuration = dict(config.get_section(config.config_ini_section))
    configuration["sqlalchemy.url"] = _get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
