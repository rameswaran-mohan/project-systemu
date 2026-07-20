"""Per-dialect DDL pins for alembic revision 0011 (tool-model field parity).

``SqliteVault`` backs BOTH the ``sqlite`` and ``postgres`` storage modes, and
``docker/entrypoint.sh`` runs ``alembic upgrade head`` whenever
``SYSTEMU_DATABASE_URL`` is set — which the ``docker-local`` and
``docker-enterprise`` compose profiles always set to a ``postgresql://`` URL.
Revision 0011 therefore executes against real Postgres, but every test that
covered it ran against SQLite only, so a Postgres-invalid default shipped green.

WHAT WAS ACTUALLY BROKEN (measured against PostgreSQL 16.2, not assumed):

    ALTER TABLE tools ADD COLUMN trusted_inprocess BOOLEAN DEFAULT 0
    -> DatatypeMismatch: column "trusted_inprocess" is of type boolean
       but default expression is of type integer

Postgres has no implicit integer->boolean cast, so a BARE integer literal is not
a legal default for a boolean column. SQLite, being dynamically typed, accepts
it. Three columns were affected: ``trusted_inprocess``, ``forge_rejected`` and
``is_action_tool``.

WHY THAT MATTERED MORE THAN A FAILED MIGRATION: Postgres DDL is transactional,
so the failure rolled 0011 back WHOLE — all thirteen columns, ``effect_tags``
included. ``docker/entrypoint.sh`` then soft-falls-back to ``alembic stamp
head``, which records 0011 as applied. The columns are never added and the
revision is never retried, so the backend-divergent action-gate scoring that
0011 exists to fix silently persists on every Postgres deployment.

WHAT WAS **NOT** BROKEN: the ``sa.JSON()`` columns. ``JSON DEFAULT '[]'`` is
accepted by Postgres — an unknown-typed literal is coerced to the column type,
and Postgres records the default as ``'[]'::json``. Adding an explicit
``::json`` cast to satisfy Postgres would be a regression, because that is not
valid SQLite syntax. ``test_json_defaults_carry_no_explicit_cast`` pins that.

The fix is ``sa.false()``, which is dialect-aware: it renders ``false`` on
Postgres and ``0`` on SQLite, leaving the SQLite DDL byte-identical to what
shipped. It is also the pattern revisions 0004 and 0009 already use.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_ROOT = Path(__file__).resolve().parents[1]
_REVISION = _ROOT / "alembic" / "versions" / "20260719_0011_tool_model_field_parity.py"

# A boolean column whose DEFAULT is a bare (unquoted) integer literal. This is
# the exact shape Postgres rejects with DatatypeMismatch. ``DEFAULT false`` and
# ``DEFAULT '0'`` both coerce fine and must NOT match.
_BARE_INT_BOOL_DEFAULT = re.compile(r"\bBOOLEAN\s+DEFAULT\s+-?\d+", re.IGNORECASE)


def _load_revision():
    """Import the revision module from its path (alembic/versions is not a package)."""
    spec = importlib.util.spec_from_file_location("_mig_0011", _REVISION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _render(dialect_name: str) -> list[str]:
    """Run the revision's REAL ``upgrade()`` against a dialect and return its DDL.

    This drives ``upgrade()`` itself rather than re-reading its column table, so
    a column added inline — bypassing the table — is still covered.
    """
    mod = _load_revision()
    buf: list[str] = []
    ctx = MigrationContext.configure(
        dialect_name=dialect_name,
        opts={
            "as_sql": True,
            "output_buffer": type(
                "_Buf", (), {"write": lambda s, t: buf.append(t), "flush": lambda s: None}
            )(),
        },
    )
    with Operations.context(ctx):
        mod.upgrade()

    stmts = [
        line.strip().rstrip(";")
        for line in "".join(buf).splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    assert stmts, f"rendered NO DDL for {dialect_name} — the pin below would be vacuous"
    return stmts


def _boolean_columns() -> list[str]:
    """Boolean columns DERIVED from the revision, never hand-listed.

    A hand-listed set is what let the original field drift accumulate; deriving
    it means a boolean column added to 0011 later is covered automatically.
    """
    mod = _load_revision()
    cols = [name for name, type_, _default in mod._COLUMNS if isinstance(type_, sa.Boolean)]
    assert cols, "precondition: revision 0011 must add at least one boolean column"
    return cols


# --------------------------------------------------------------------------- #
# 1. the defect, stated directly
# --------------------------------------------------------------------------- #

def test_no_boolean_column_takes_a_bare_integer_default_on_postgres():
    """The exact DDL Postgres rejected. This is the regression pin for the fix."""
    offenders = [s for s in _render("postgresql") if _BARE_INT_BOOL_DEFAULT.search(s)]
    assert not offenders, (
        "revision 0011 emits a BOOLEAN column with a bare integer DEFAULT, which "
        "PostgreSQL rejects outright:\n  "
        + "\n  ".join(offenders)
        + "\n\nDatatypeMismatch: column is of type boolean but default expression "
        "is of type integer. Postgres DDL is transactional, so this rolls the "
        "WHOLE revision back — effect_tags included — and docker/entrypoint.sh "
        "then stamps head, so the columns are never added and never retried. "
        "Use sa.false(), which renders 'false' on Postgres and '0' on SQLite."
    )


@pytest.mark.parametrize("column", _boolean_columns())
def test_each_boolean_column_defaults_to_a_boolean_literal_on_postgres(column):
    """Per-column so a failure names the column, and derived so it cannot go stale."""
    stmt = next(s for s in _render("postgresql") if f" {column} " in s)
    assert "BOOLEAN" in stmt.upper(), f"precondition: {column} should render as BOOLEAN, got {stmt!r}"
    assert not _BARE_INT_BOOL_DEFAULT.search(stmt), (
        f"{column} defaults to a bare integer on Postgres: {stmt!r}"
    )


# --------------------------------------------------------------------------- #
# 2. SQLite must not change — the fix is Postgres-only
# --------------------------------------------------------------------------- #

# The exact statements revision 0011 emitted for SQLite BEFORE the Postgres fix.
# Golden, so any change to SQLite behaviour has to be deliberate: SQLite is the
# default backend for every non-docker install and it was never broken.
_SHIPPED_SQLITE_DDL = [
    "ALTER TABLE tools ADD COLUMN requires_credentials JSON DEFAULT '[]'",
    "ALTER TABLE tools ADD COLUMN forged_by_execution_id VARCHAR",
    "ALTER TABLE tools ADD COLUMN grounding_inputs JSON DEFAULT '[]'",
    "ALTER TABLE tools ADD COLUMN effect_tags JSON DEFAULT '[]'",
    "ALTER TABLE tools ADD COLUMN external_verification_channel VARCHAR",
    "ALTER TABLE tools ADD COLUMN trusted_inprocess BOOLEAN DEFAULT 0",
    "ALTER TABLE tools ADD COLUMN forge_reattempts INTEGER DEFAULT 0",
    "ALTER TABLE tools ADD COLUMN forge_rejected BOOLEAN DEFAULT 0",
    "ALTER TABLE tools ADD COLUMN is_action_tool BOOLEAN DEFAULT 0",
    "ALTER TABLE tools ADD COLUMN toolset VARCHAR",
    "ALTER TABLE tools ADD COLUMN max_result_size_chars INTEGER",
    "ALTER TABLE tools ADD COLUMN timeout_seconds INTEGER",
    "ALTER TABLE tools ADD COLUMN check_fn_name VARCHAR",
]


def test_sqlite_ddl_is_byte_identical_to_what_shipped():
    """SQLite was never broken. ``sa.text('false')`` would also fix Postgres but
    would change this too — and SQLite only grew the ``false`` keyword in 3.23.
    """
    assert _render("sqlite") == _SHIPPED_SQLITE_DDL


def test_json_defaults_carry_no_explicit_cast():
    """The defect was filed against the JSON columns; measurement says otherwise.

    PostgreSQL 16.2 accepts ``JSON DEFAULT '[]'`` — the unknown-typed literal is
    coerced and stored as ``'[]'::json``. Writing the cast into the migration to
    "fix" Postgres would emit ``DEFAULT '[]'::json`` on SQLite too, which SQLite
    cannot parse. Both dialects share one rendering here, so a cast added for
    one breaks the other.
    """
    for dialect in ("postgresql", "sqlite"):
        cast = [s for s in _render(dialect) if "::" in s]
        assert not cast, (
            f"{dialect} DDL carries an explicit cast: {cast}. Postgres does not "
            f"need one for a JSON default, and SQLite cannot parse one."
        )


# --------------------------------------------------------------------------- #
# 3. whole-versions-set guard
# --------------------------------------------------------------------------- #

def test_no_migration_in_the_versions_set_gives_a_boolean_a_bare_integer_default():
    """Render the ENTIRE chain for Postgres and scan it.

    Uses alembic's own offline mode (``upgrade head --sql``), which never
    connects, so this needs no server and no Postgres driver. Scoping the fix to
    0011 is only safe if no sibling revision carries the same shape.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env={**__import__("os").environ,
             "SYSTEMU_DATABASE_URL": "postgresql://u:p@localhost:5432/systemu"},
    )
    assert proc.returncode == 0, f"alembic offline render failed:\n{proc.stderr[-3000:]}"

    revisions = re.findall(r"Running upgrade\s+\S*\s*->\s*(\S+)", proc.stderr)
    on_disk = sorted(p for p in (_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(revisions) == len(on_disk), (
        f"rendered {len(revisions)} revisions but {len(on_disk)} exist on disk — "
        f"the scan below would silently skip the difference"
    )

    offenders = [
        s.strip()
        for s in proc.stdout.split(";")
        if _BARE_INT_BOOL_DEFAULT.search(s)
    ]
    assert not offenders, (
        "these statements give a BOOLEAN column a bare integer DEFAULT, which "
        "PostgreSQL rejects:\n  " + "\n  ".join(offenders)
    )
