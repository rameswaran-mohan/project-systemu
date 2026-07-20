"""Dialect-correctness pins for the additive schema-upgrade path.

The defect
    ``SqliteVault._upgrade_schema`` hand-wrote each column's TYPE as a literal
    SQL string. ``SqliteVault`` backs BOTH the ``sqlite`` and the ``postgres``
    mode (``__init__`` sets ``_storage_backend`` from the URL scheme), so that
    one string had to be right for two dialects. It was not: four columns the
    ORM model declares ``Boolean`` were emitted as ``INTEGER``.

    SQLite is dynamically typed, so a column declared ``INTEGER`` holding 0/1
    behaves identically to one declared ``BOOLEAN`` — the mismatch is invisible
    there, which is why it survived. PostgreSQL is statically typed and creates
    a genuine ``integer`` column instead.

Why ``_upgrade_schema`` is load-bearing on Postgres rather than dead code
    ``Base.metadata.create_all()`` runs first, but it only creates MISSING
    TABLES — it never adds a column to a table that already exists. So on a
    store that predates these columns, ``_upgrade_schema`` is what adds them.
    For the three ``evolutions`` columns it is the ONLY thing that ever adds
    them: no alembic revision mentions ``edit_classification``,
    ``fields_changed`` or ``reverted`` (verified by searching ``alembic/``).

Scope of what these pins verify — read this before trusting them
    Everything below is asserted at the DDL-RENDERING level: SQLAlchemy
    compiles the emitted DDL for a named dialect, which needs no server. The
    end-to-end tests in section 3 run against real SQLite.

    NO PART OF THIS FILE WAS EXERCISED AGAINST A LIVE POSTGRESQL SERVER — none
    was reachable in the environment where it was written. The claim these pins
    actually make is the server-independent one, and it is sufficient on its
    own: *the upgrade path emits a column type that differs from the type the
    ORM model declares and that ``create_all`` would have produced*, so the
    same store ends up with two different schemas depending on whether it was
    created fresh or upgraded in place. That is a defect whatever PostgreSQL
    does with the result.

``tests/test_sqlite_tool_field_parity.py`` already learned this lesson one
level up: it pins the FIELD LIST against the model because "a hand-listed set
is exactly what let the drift accumulate". The TYPES were still hand-listed.
These pins close that gap — type and default are now read from the model and
compiled per dialect, so neither can drift from it again.
"""
from __future__ import annotations

import sqlite3

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite as sqlite_dialect

from systemu.storage.sqlite.models import Base
from systemu.storage.sqlite.vault import _UPGRADE_COLUMNS, _add_column_ddl

PG = postgresql.dialect()
SQ = sqlite_dialect.dialect()

# The four columns that actually carried the wrong type. Named rather than
# derived so these stay meaningful if the generic checks are ever weakened.
_MODEL_BOOLEANS = [
    ("tools", "trusted_inprocess"),
    ("tools", "forge_rejected"),
    ("tools", "is_action_tool"),
    ("evolutions", "reverted"),
]


# --------------------------------------------------------------------------- #
# 1. The model is the single source of truth for the TYPE
# --------------------------------------------------------------------------- #

def test_every_upgraded_column_exists_on_the_model():
    """The upgrade list must name real model columns.

    ``_add_column_ddl`` derives the type from the model, so a pair naming a
    column the model does not have cannot be rendered at all. This runs before
    the rendering pins so that a bad pair reports as itself rather than as a
    confusing KeyError inside every other test.
    """
    for table, column in _UPGRADE_COLUMNS:
        assert table in Base.metadata.tables, f"unknown table {table!r}"
        assert column in Base.metadata.tables[table].columns, (
            f"{table}.{column} is in the upgrade list but not on the model"
        )


@pytest.mark.parametrize("dialect_name,dialect", [("postgresql", PG), ("sqlite", SQ)])
def test_emitted_ddl_declares_the_model_type_on_every_dialect(dialect_name, dialect):
    """The emitted type must equal the model's own type for that dialect.

    This is the regression itself: the model said ``Boolean`` and the old code
    emitted ``INTEGER``.
    """
    for table, column in _UPGRADE_COLUMNS:
        want = Base.metadata.tables[table].columns[column].type.compile(dialect=dialect)
        ddl = _add_column_ddl(table, column, dialect)
        head = f"ALTER TABLE {table} ADD COLUMN {column} "
        assert ddl.startswith(head), f"unexpected DDL shape: {ddl!r}"
        emitted = ddl[len(head):].split(" DEFAULT ")[0].strip()
        assert emitted == want, (
            f"{dialect_name}: {table}.{column} model declares {want!r} "
            f"but the upgrade emits {emitted!r}"
        )


def test_model_boolean_columns_are_not_emitted_as_integer_on_postgres():
    """Explicit pin on the four columns that carried the wrong type."""
    for table, column in _MODEL_BOOLEANS:
        assert (table, column) in _UPGRADE_COLUMNS, (
            f"{table}.{column} left the upgrade list — update this pin"
        )
        assert isinstance(Base.metadata.tables[table].columns[column].type, sa.Boolean), (
            f"{table}.{column} is no longer a model Boolean — update this pin"
        )
        ddl = _add_column_ddl(table, column, PG)
        assert " BOOLEAN" in ddl.upper(), f"expected BOOLEAN, got: {ddl!r}"
        assert " INTEGER" not in ddl.upper(), (
            f"PostgreSQL would create an integer column here, which is not the "
            f"type the ORM model declares: {ddl!r}"
        )


# --------------------------------------------------------------------------- #
# 2. Defaults must be legal for the type they are attached to
# --------------------------------------------------------------------------- #

def test_boolean_defaults_render_as_boolean_literals_on_postgres():
    """A boolean column's default must be a boolean literal, not ``0``.

    ``sa.false()`` is the dialect-neutral spelling: it renders ``0`` on SQLite
    (byte-identical to what this path emitted before the fix) and ``false`` on
    PostgreSQL. Migrations 0004 and 0009 already use it.

    NOT verified against a live server — the assertion is that the rendered
    literal is a boolean one, not that PostgreSQL rejects the alternative.
    """
    for table, column in _UPGRADE_COLUMNS:
        if not isinstance(Base.metadata.tables[table].columns[column].type, sa.Boolean):
            continue
        ddl = _add_column_ddl(table, column, PG)
        assert " DEFAULT " in ddl, f"{table}.{column}: expected a default — {ddl!r}"
        default = ddl.split(" DEFAULT ", 1)[1].strip()
        # Compared case-SENSITIVELY on purpose. Lowercasing here made this pin
        # vacuous: a mutation that rendered the Python repr ``False`` slipped
        # through, because ``"False".lower()`` is ``"false"``. ``sa.false()``
        # renders exactly ``false``, so anything else is a rendering change.
        assert default in {"false", "true"}, (
            f"{table}.{column}: a BOOLEAN column needs a boolean default on "
            f"PostgreSQL; got DEFAULT {default}"
        )


def test_sqlite_defaults_are_byte_identical_to_the_pre_fix_path(request):
    """The SQLite path must keep emitting exactly the defaults it emitted before.

    Transcribed from the hand-written ``new_cols`` table as it stood at
    724e5a6b. SQLite is the common deployment, so holding this constant is what
    makes the change safe there. Only the DEFAULT is pinned — the declared TYPE
    deliberately changes on SQLite too (``INTEGER`` -> ``BOOLEAN`` for the four
    booleans), which is what makes an upgraded store match a fresh one.
    """
    pre_fix_defaults = {
        ("evolutions", "edit_classification"): None,
        ("evolutions", "fields_changed"): "'[]'",
        ("evolutions", "reverted"): "0",
        ("tools", "requires_credentials"): "'[]'",
        ("tools", "forged_by_execution_id"): None,
        ("tools", "grounding_inputs"): "'[]'",
        ("tools", "effect_tags"): "'[]'",
        ("tools", "external_verification_channel"): None,
        ("tools", "trusted_inprocess"): "0",
        ("tools", "forge_reattempts"): "0",
        ("tools", "forge_rejected"): "0",
        ("tools", "is_action_tool"): "0",
        ("tools", "toolset"): None,
        ("tools", "max_result_size_chars"): None,
        ("tools", "timeout_seconds"): None,
        ("tools", "check_fn_name"): None,
    }
    assert set(pre_fix_defaults) == set(_UPGRADE_COLUMNS), (
        "the upgrade column set changed — re-derive the expected SQLite defaults "
        "from the pre-fix hand-written list before editing this pin"
    )
    for (table, column), want in pre_fix_defaults.items():
        ddl = _add_column_ddl(table, column, SQ)
        if want is None:
            assert " DEFAULT " not in ddl, f"{table}.{column}: unexpected default in {ddl!r}"
        else:
            assert ddl.endswith(f" DEFAULT {want}"), (
                f"{table}.{column}: sqlite default changed — {ddl!r}"
            )


def test_add_column_ddl_never_emits_not_null():
    """``ADD COLUMN ... NOT NULL`` without a default fails on a populated table.

    ``evolutions.reverted`` and ``evolutions.fields_changed`` are non-nullable
    on the model, so rendering the model column verbatim would emit NOT NULL and
    break the upgrade on any store that already has rows. The pre-fix path never
    emitted it either; this holds that constant.
    """
    for table, column in _UPGRADE_COLUMNS:
        for dialect in (PG, SQ):
            ddl = _add_column_ddl(table, column, dialect)
            assert "NOT NULL" not in ddl.upper(), f"{table}.{column}: {ddl!r}"


def test_non_nullable_model_columns_are_actually_present_in_the_set():
    """Guard for the test above: it is vacuous if no column is non-nullable.

    If the model ever makes all of these nullable, ``test_..._never_emits_not_null``
    stops testing anything and this pin says so.
    """
    non_nullable = [
        (t, c) for t, c in _UPGRADE_COLUMNS
        if not Base.metadata.tables[t].columns[c].nullable
    ]
    assert non_nullable, (
        "no upgraded column is non-nullable any more — the NOT NULL pin is now "
        "vacuous and should be re-derived or removed"
    )


# --------------------------------------------------------------------------- #
# 3. Behavioural — a legacy store, through the real vault (call-site pin)
# --------------------------------------------------------------------------- #

def _declared_types(db_path, table: str) -> dict:
    """SQLite records the DECLARED type verbatim, which is what we're pinning."""
    con = sqlite3.connect(str(db_path))
    try:
        return {r[1]: (r[2] or "").upper() for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def test_legacy_store_is_upgraded_to_the_model_declared_types(tmp_path):
    """The end-to-end pin, driven through the real ``SqliteVault``.

    This is the pin that would catch ``_add_column_ddl`` being correct but never
    CALLED: it never references the helper, only the vault's observable effect
    on a real database file.

    Builds a store, strips the post-initial columns to simulate one created
    before they existed, then re-opens it — exactly what an operator upgrading
    in place does — and asserts every re-added column carries the type the ORM
    model declares.
    """
    from systemu.storage.sqlite.vault import SqliteVault

    db = tmp_path / "legacy.db"
    url = f"sqlite:///{db}"
    SqliteVault(url, memory_dir=tmp_path / "mem")

    con = sqlite3.connect(str(db))
    try:
        for table, column in _UPGRADE_COLUMNS:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        con.commit()
    finally:
        con.close()

    for table, column in _UPGRADE_COLUMNS:
        assert column not in _declared_types(db, table), "setup failed to strip the column"

    # Re-opening re-runs _upgrade_schema — the in-place upgrade path.
    SqliteVault(url, memory_dir=tmp_path / "mem")

    for table, column in _UPGRADE_COLUMNS:
        got = _declared_types(db, table).get(column)
        want = Base.metadata.tables[table].columns[column].type.compile(dialect=SQ).upper()
        assert got == want, (
            f"{table}.{column}: legacy upgrade declared {got!r}, model says {want!r}"
        )


def test_legacy_upgrade_still_round_trips_a_tool(tmp_path):
    """Types are only right if data still survives the upgrade.

    Guards the failure mode that made this defect cheap to miss: a schema
    upgrade that "succeeds" while the rows it was supposed to enable are
    unusable.
    """
    from systemu.core.models import Tool, ToolStatus
    from systemu.storage.sqlite.vault import SqliteVault

    db = tmp_path / "legacy.db"
    url = f"sqlite:///{db}"
    v1 = SqliteVault(url, memory_dir=tmp_path / "mem")
    v1.save_tool(Tool(
        id="t-legacy", name="legacy_tool", description="d",
        tool_type="python_function",
        status=ToolStatus.DEPLOYED, effect_tags=["money_move"],
        trusted_inprocess=True, is_action_tool=True,
    ))

    con = sqlite3.connect(str(db))
    try:
        for column in ("trusted_inprocess", "is_action_tool", "effect_tags"):
            con.execute(f"ALTER TABLE tools DROP COLUMN {column}")
        con.commit()
    finally:
        con.close()

    v2 = SqliteVault(url, memory_dir=tmp_path / "mem")
    v2.save_tool(Tool(
        id="t-after", name="after_tool", description="d",
        tool_type="python_function",
        status=ToolStatus.DEPLOYED, effect_tags=["net_read"],
        trusted_inprocess=True, is_action_tool=True,
    ))
    after = v2.get_tool("t-after")
    assert after.trusted_inprocess is True
    assert after.is_action_tool is True
    assert after.effect_tags == ["net_read"]

    # The pre-existing row survives; its stripped columns read back as the
    # model default rather than raising.
    assert v2.get_tool("t-legacy").name == "legacy_tool"


def test_upgraded_store_matches_a_freshly_created_one(tmp_path):
    """An upgraded store and a fresh one must have the same declared types.

    Both are compared against the model independently above; this pins the
    consequence that made the bug concrete — before the fix these two paths
    disagreed, so 'which schema you get' depended on when your store was made.
    """
    from systemu.storage.sqlite.vault import SqliteVault

    fresh_db = tmp_path / "fresh.db"
    SqliteVault(f"sqlite:///{fresh_db}", memory_dir=tmp_path / "m1")

    legacy_db = tmp_path / "legacy.db"
    SqliteVault(f"sqlite:///{legacy_db}", memory_dir=tmp_path / "m2")
    con = sqlite3.connect(str(legacy_db))
    try:
        for table, column in _UPGRADE_COLUMNS:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        con.commit()
    finally:
        con.close()
    SqliteVault(f"sqlite:///{legacy_db}", memory_dir=tmp_path / "m2")

    for table in {t for t, _ in _UPGRADE_COLUMNS}:
        assert _declared_types(legacy_db, table) == _declared_types(fresh_db, table), (
            f"{table}: an upgraded store does not match a freshly created one"
        )
