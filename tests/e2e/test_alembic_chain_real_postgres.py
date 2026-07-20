"""End-to-end: the alembic chain must APPLY against real Postgres.

CI runs this in the `integration` job with ``SYSTEMU_DATABASE_URL`` pointing at
a live Postgres container; locally it skips unless that env var is set — the
same gating as ``test_memory_tier_real_postgres.py``.

Why this exists
    ``SqliteVault`` backs both the ``sqlite`` and ``postgres`` storage modes,
    and ``docker/entrypoint.sh`` runs ``alembic upgrade head`` whenever
    ``SYSTEMU_DATABASE_URL`` is set — which the ``docker-local`` and
    ``docker-enterprise`` compose profiles always do. Every test covering the
    migrations ran on SQLite only, so revision 0011 shipped emitting

        ALTER TABLE tools ADD COLUMN trusted_inprocess BOOLEAN DEFAULT 0

    which SQLite accepts and Postgres rejects (no implicit integer→boolean
    cast). Postgres DDL is transactional, so the rejection rolled the whole
    revision back — ``effect_tags`` included — and the entrypoint then
    soft-falls-back to ``alembic stamp head``, recording it as applied. The
    columns were never added and the revision was never retried.

    ``tests/test_migration_0011_postgres_ddl.py`` pins that specific shape at
    the DDL-rendering level and needs no server. This test is the general
    version: it asserts the emitted DDL is something Postgres will actually
    ACCEPT, which catches dialect hazards nobody thought to write a regex for.

Isolation
    The chain runs inside a throwaway schema that is dropped afterwards, so
    this never touches the tables the rest of the integration job uses.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_PG_URL = os.environ.get("SYSTEMU_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _PG_URL.startswith("postgresql"),
    reason="set SYSTEMU_DATABASE_URL=postgresql+psycopg2://… to enable",
)

_ROOT = Path(__file__).resolve().parents[2]


def _render_chain_sql() -> str:
    """Ask alembic itself for the Postgres DDL, via offline mode.

    Offline mode never connects, so this is the migration SQL exactly as
    written — not a re-implementation of it that could drift.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "SYSTEMU_DATABASE_URL": _PG_URL},
    )
    assert proc.returncode == 0, f"alembic offline render failed:\n{proc.stderr[-3000:]}"
    rendered = re.findall(r"Running upgrade\s+\S*\s*->\s*(\S+)", proc.stderr)
    on_disk = list((_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(rendered) == len(on_disk), (
        f"rendered {len(rendered)} revisions but {len(on_disk)} exist on disk — "
        f"this test would silently skip the difference"
    )
    return proc.stdout


def _statements(sql: str) -> list[str]:
    out = []
    for chunk in sql.split(";"):
        body = "\n".join(
            ln for ln in chunk.splitlines() if not ln.strip().startswith("--")
        ).strip()
        if body and body.upper() not in ("BEGIN", "COMMIT"):
            out.append(body)
    return out


@pytest.fixture
def throwaway_schema():
    """A private schema for this test, dropped on the way out."""
    from sqlalchemy import create_engine, text

    name = f"mig_probe_{uuid.uuid4().hex[:8]}"
    engine = create_engine(_PG_URL)
    with engine.begin() as c:
        c.execute(text(f"CREATE SCHEMA {name}"))
    try:
        yield engine, name
    finally:
        with engine.begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))
        engine.dispose()


def test_full_migration_chain_applies_to_real_postgres(throwaway_schema):
    """Every revision must be DDL Postgres accepts. 0011 was not."""
    from sqlalchemy import text

    engine, schema = throwaway_schema
    stmts = _statements(_render_chain_sql())
    assert len(stmts) > 20, f"suspiciously few statements ({len(stmts)}) — render broke"

    with engine.begin() as conn:
        conn.execute(text(f"SET search_path TO {schema}"))
        for stmt in stmts:
            try:
                conn.execute(text(stmt))
            except Exception as exc:  # noqa: BLE001 — re-raised with the statement
                pytest.fail(
                    f"PostgreSQL rejected migration DDL:\n  {stmt}\n\n"
                    f"{type(exc).__name__}: {str(exc).strip().splitlines()[0]}\n\n"
                    f"Postgres DDL is transactional, so this aborts the whole "
                    f"revision; docker/entrypoint.sh then stamps head and the "
                    f"columns are never added and never retried."
                )


def test_restored_tool_columns_have_the_types_the_orm_expects(throwaway_schema):
    """Applying is not enough — the columns must be the types ``ToolRow`` declares.

    ``trusted_inprocess`` as INTEGER rather than BOOLEAN applies cleanly and
    then fails on every write ("column is of type integer but expression is of
    type boolean"), so an apply-only check would pass over it.
    """
    from sqlalchemy import text

    from systemu.storage.sqlite.models import ToolRow

    engine, schema = throwaway_schema
    with engine.begin() as conn:
        conn.execute(text(f"SET search_path TO {schema}"))
        for stmt in _statements(_render_chain_sql()):
            conn.execute(text(stmt))

    with engine.begin() as conn:
        actual = {
            r[0]: r[1]
            for r in conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'tools'"
                ),
                {"s": schema},
            )
        }

    # Expectation derived from the ORM model, never hand-listed.
    import sqlalchemy as sa

    wrong = {}
    for col in ToolRow.__table__.columns:
        got = actual.get(col.name)
        if got is None:
            wrong[col.name] = ("MISSING", type(col.type).__name__)
        elif isinstance(col.type, sa.Boolean) and got != "boolean":
            wrong[col.name] = (got, "boolean")
        elif isinstance(col.type, sa.JSON) and got not in ("json", "jsonb"):
            wrong[col.name] = (got, "json")
    assert not wrong, (
        f"the migrated Postgres schema disagrees with ToolRow on "
        f"{sorted(wrong)} (actual, expected): {wrong}"
    )


def test_boolean_defaults_apply_as_booleans(throwaway_schema):
    """A row that omits the new booleans must read False, not NULL or 0."""
    from sqlalchemy import text

    engine, schema = throwaway_schema
    with engine.begin() as conn:
        conn.execute(text(f"SET search_path TO {schema}"))
        for stmt in _statements(_render_chain_sql()):
            conn.execute(text(stmt))
        conn.execute(text("INSERT INTO tools (id, name) VALUES ('t-def', 'bare')"))
        row = conn.execute(
            text(
                "SELECT trusted_inprocess, forge_rejected, is_action_tool, effect_tags "
                "FROM tools WHERE id = 't-def'"
            )
        ).one()

    assert row[0] is False and row[1] is False and row[2] is False, (
        f"boolean server defaults did not apply as booleans: {row!r}"
    )
    assert row[3] == [], f"effect_tags default should be an empty JSON array, got {row[3]!r}"
