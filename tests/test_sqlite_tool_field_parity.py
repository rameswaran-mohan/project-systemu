"""Backend PARITY pins for Tool persistence — the SQLite ``effect_tags`` loss.

The defect: ``ToolRow`` had no ``effect_tags`` column, and neither
``_tool_to_row`` nor ``_row_to_tool`` carried it, so a tool saved through
``SqliteVault`` read back with ``effect_tags == []``. The file vault (which
persists the whole model as JSON) kept them. Effect tags feed the action gate,
so the SAME tool was governed DIFFERENTLY depending on which storage backend
was configured.

Measuring it turned up TWELVE more model fields with the same loss (the
``ToolRow`` column list had simply stopped tracking the ``Tool`` model), so the
pins here are written against the MODEL rather than against a hand-listed set
of fields — a hand-listed set is exactly what let the drift accumulate.

FIXTURE REALISM: none of these tests hand-authors a persisted row or an index
header. Every shape under test is produced by RUNNING the real save path of a
real vault. ``test_every_model_field_survives_persistence`` is the guard for
that class: it fails if a fixture could assert a field a backend cannot
actually carry.
"""
from __future__ import annotations

import sqlite3

import pytest

from systemu.core.models import CredentialRequirement, Tool, ToolStatus


# Fields that legitimately do NOT round-trip identically, with the reason each
# is excluded. Derived by RUNNING both backends, not assumed:
#   * tool_md_path — the file vault rewrites it to the TOOL.md it just wrote.
#   * created_at / updated_at — storage-stamped timestamps.
_DERIVED_FIELDS = {"tool_md_path", "created_at", "updated_at"}


def _populated_tool() -> Tool:
    """A Tool with EVERY non-derived field set to a non-default value.

    Non-default is the point: a field left at its default round-trips
    "correctly" through a backend that drops it entirely, which is precisely how
    this loss stayed invisible.
    """
    return Tool(
        id="t-parity",
        name="probe_all",
        description="a tool exercising every persisted field",
        tool_type="python_function",
        parameters_schema={"x": {"type": "string"}},
        requires_credentials=[CredentialRequirement(key="API_KEY", label="API key")],
        return_schema={"ok": {"type": "boolean"}},
        implementation_notes="notes",
        dependencies=["requests"],
        implementation_path="vault/tools/implementations/probe_all.py",
        status=ToolStatus.DEPLOYED,
        forged_by_systemu=True,
        forged_by_execution_id="exec-9",
        grounding_inputs=["a.txt"],
        effect_tags=["money_move", "net_read"],
        external_verification_channel="api_readback",
        trusted_inprocess=True,
        enabled=True,
        version=3,
        dry_run_status="passed",
        dry_run_evidence={"e": 1},
        forge_reattempts=2,
        forge_rejected=True,
        last_successful_params=[{"x": "1"}],
        evolution_history=[{"v": 1}],
        is_action_tool=True,
        toolset="ops",
        max_result_size_chars=1234,
        timeout_seconds=42,
        check_fn_name="check_it",
    )


def _file_vault(tmp_path):
    from systemu.vault.vault import Vault
    return Vault(vault_dir=tmp_path / "fv")


def _sqlite_vault(tmp_path):
    from systemu.storage.sqlite.vault import SqliteVault
    return SqliteVault(f"sqlite:///{tmp_path / 'v.db'}", memory_dir=tmp_path / "mem")


def _make_vault(backend, tmp_path):
    return _file_vault(tmp_path) if backend == "file" else _sqlite_vault(tmp_path)


BACKENDS = ["file", "sqlite"]


# --------------------------------------------------------------------------- #
# 1. the reported defect, stated directly
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("backend", BACKENDS)
def test_effect_tags_survive_a_save_load_round_trip(backend, tmp_path):
    """effect_tags feed the action gate — a backend that drops them governs the
    same tool differently. This failed on sqlite (read back [])."""
    vault = _make_vault(backend, tmp_path)
    vault.save_tool(_populated_tool())

    assert vault.get_tool("t-parity").effect_tags == ["money_move", "net_read"], (
        f"the {backend} backend LOST Tool.effect_tags across a save/load "
        f"round-trip — the action gate scores this tool as UNKNOWN instead of "
        f"money_move, and the remote-approval floor sees a different card")


# --------------------------------------------------------------------------- #
# 2. FIXTURE-REALISM pin — model-derived, so it cannot go stale
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("backend", BACKENDS)
def test_every_model_field_survives_persistence(backend, tmp_path):
    """Every ``Tool`` field a caller can set must come back off a real save.

    This is the guard the hand-listed column set could not be. It enumerates
    ``Tool.model_fields`` at RUN time, so adding a field to the model without
    teaching a backend to persist it fails HERE rather than silently reading as
    a default in production. A fixture that supplies a field the backend cannot
    carry is asserting a shape production never produces.
    """
    tool = _populated_tool()
    vault = _make_vault(backend, tmp_path)
    vault.save_tool(tool)

    saved = tool.model_dump(mode="json")
    loaded = vault.get_tool("t-parity").model_dump(mode="json")

    dropped = {
        k: (saved[k], loaded.get(k))
        for k in Tool.model_fields
        if k not in _DERIVED_FIELDS and loaded.get(k) != saved[k]
    }
    assert not dropped, (
        f"the {backend} backend does not persist {sorted(dropped)} — each field "
        f"reads back as a DEFAULT, so any test asserting it after a round-trip "
        f"is asserting a shape this backend cannot produce. saved->loaded: {dropped}")


@pytest.mark.parametrize("backend", BACKENDS)
def test_falsy_but_deliberate_values_are_not_rewritten_to_defaults(backend, tmp_path):
    """``0`` / ``""`` are STORED values, not absent ones.

    The read path must distinguish SQL NULL (column did not exist when the row
    was written ⇒ model default) from a falsy value the caller actually chose. A
    ``val or default`` fallback conflates them and would rewrite a
    ``timeout_seconds`` of 0 into "no timeout" — the opposite meaning.
    """
    tool = _populated_tool()
    tool.timeout_seconds = 0
    tool.max_result_size_chars = 0
    tool.forge_reattempts = 0
    tool.toolset = ""
    tool.check_fn_name = ""
    tool.external_verification_channel = ""

    vault = _make_vault(backend, tmp_path)
    vault.save_tool(tool)
    loaded = vault.get_tool("t-parity")

    assert loaded.timeout_seconds == 0, "a stored 0 timeout was rewritten to the default"
    assert loaded.max_result_size_chars == 0
    assert loaded.forge_reattempts == 0
    assert loaded.toolset == ""
    assert loaded.check_fn_name == ""
    assert loaded.external_verification_channel == ""


def test_both_backends_round_trip_a_tool_identically(tmp_path):
    """The two backends must be behaviourally interchangeable. A field carried by
    one and dropped by the other means the same tool is governed differently
    depending on storage — the divergence this whole file exists to prevent."""
    fv, sv = _file_vault(tmp_path), _sqlite_vault(tmp_path)
    fv.save_tool(_populated_tool())
    sv.save_tool(_populated_tool())

    f = fv.get_tool("t-parity").model_dump(mode="json")
    s = sv.get_tool("t-parity").model_dump(mode="json")

    diverged = {k: (f.get(k), s.get(k)) for k in Tool.model_fields
                if k not in _DERIVED_FIELDS and f.get(k) != s.get(k)}
    assert not diverged, (
        f"file-vault and sqlite disagree on {sorted(diverged)} for the SAME "
        f"tool — backend-divergent behaviour. file->sqlite: {diverged}")


# --------------------------------------------------------------------------- #
# 3. the CONSEQUENCES — the gate must reach the same decision on either backend
# --------------------------------------------------------------------------- #

def _gate_tool(backend, tmp_path, **overrides):
    """Save a tool through a REAL backend and hand back what the gate would load."""
    base = dict(
        id="t-gate", name="probe", description="d", tool_type="python_function",
        parameters_schema={"x": {"type": "string"}},
        implementation_path="vault/tools/implementations/probe.py",
        status=ToolStatus.DEPLOYED, enabled=True,
    )
    base.update(overrides)
    vault = _make_vault(backend, tmp_path)
    vault.save_tool(Tool(**base))
    return vault.get_tool("t-gate")


def _verdict_for(tool):
    from systemu.runtime.action_governance import (
        ActionContext, effective_tags, evaluate_action)
    ctx = ActionContext(
        tool=tool.name,
        effect_tags={str(t) for t in (tool.effect_tags or [])},
        classification_trusted=True,
    )
    verdict, _reason = evaluate_action(ctx)
    return verdict.value, sorted(effective_tags(ctx))


def test_gate_verdict_is_identical_across_backends(tmp_path):
    """A ``net_read`` tool is the frictionless-ALLOW majority the governor is
    tuned for. With its tags dropped it scores UNKNOWN and cards instead."""
    got = {
        b: _verdict_for(_gate_tool(b, tmp_path / b, name="fetch_weather",
                                   effect_tags=["net_read"]))
        for b in BACKENDS
    }
    assert got["file"] == got["sqlite"], (
        f"the gate reached DIFFERENT decisions for the same net_read tool "
        f"depending on storage backend: {got}")


def test_shell_carveout_does_not_open_on_a_backend_that_drops_tags(tmp_path):
    """``_command_gate_already_scored`` delegates to the command gate when the
    tool's tags are a SUBSET of the delegable set — and the empty set is a
    subset of everything. A shell tool that also carries ``local_delete`` must
    NOT be waved through on either backend.
    """
    from systemu.runtime.tool_sandbox import _command_gate_already_scored

    got = {}
    for b in BACKENDS:
        tool = _gate_tool(b, tmp_path / b, name="run_command",
                          effect_tags=["shell_exec", "local_delete"])
        tags = {str(t) for t in (tool.effect_tags or [])}
        got[b] = _command_gate_already_scored(tool, "run_command", tags)

    assert got["file"] == got["sqlite"] is False, (
        f"the per-tool action gate was SKIPPED on one backend but not the other "
        f"for a local_delete-capable shell tool: {got}. An empty tag list is a "
        f"subset of the delegable set, so dropping tags silently opens the "
        f"carve-out and the delete never gets its approval card")


def test_forged_money_move_requires_isolation_on_both_backends(tmp_path):
    """§13.3: a forged money-capable actuator must never run in-daemon at full
    privilege. ``requires_isolation`` reads the tool's effect tags, so a backend
    that drops them cannot see the money_move that forces isolation."""
    from systemu.runtime.action_governance import requires_isolation

    got = {}
    for b in BACKENDS:
        tool = _gate_tool(b, tmp_path / b, name="wire_payout",
                          effect_tags=["money_move"], forged_by_systemu=True,
                          trusted_inprocess=True)
        got[b] = requires_isolation(tool.effect_tags or ())

    assert got["file"] == got["sqlite"] is True, (
        f"requires_isolation disagreed across backends for a forged money_move "
        f"tool: {got}. Combined with a persisted trusted_inprocess=True this is "
        f"an in-process execution of forged money-capable code")


def test_remote_approval_class_is_identical_across_backends(tmp_path):
    """The remote lane floors on empty/unknown-only tags. A dropped tag set turns
    a positively-classified card into an unclassified one, moving it off the
    phone — the same action, two different approval surfaces.

    ``fetch_weather``/``net_read`` is chosen deliberately: a name like
    ``post_update`` is rescued by the name-verb escalator (``update`` ⇒
    net_mutate), which re-derives a positive classification from the NAME and
    hides the storage loss. This name has no verb-map hit, so the card's tags
    come from storage alone and the divergence is visible.
    """
    from systemu.messaging.decision_bridge import classify_resolution

    got = {}
    for b in BACKENDS:
        tool = _gate_tool(b, tmp_path / b, name="fetch_weather",
                          effect_tags=["net_read"])
        verdict, scored = _verdict_for(tool)
        got[b] = classify_resolution({
            "kind": "gate", "gate_type": "tool",
            "verdict": verdict, "effect_tags": scored,
        })

    assert got["file"] == got["sqlite"], (
        f"the same tool resolves on different approval surfaces per backend: {got}")


# --------------------------------------------------------------------------- #
# 4. MIGRATION safety — idempotent, non-destructive, safe to re-run
# --------------------------------------------------------------------------- #

def _tool_columns(db_path) -> set:
    con = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(tools)")}
    finally:
        con.close()


def test_upgrade_schema_is_idempotent_on_an_already_migrated_store(tmp_path):
    """Opening an already-migrated store must be a no-op, not an error, and must
    not disturb stored rows. Re-running a migration is the normal case — every
    single boot re-runs it."""
    from systemu.storage.sqlite.vault import SqliteVault

    url = f"sqlite:///{tmp_path / 'v.db'}"
    v1 = SqliteVault(url, memory_dir=tmp_path / "mem")
    v1.save_tool(_populated_tool())
    cols_before = _tool_columns(tmp_path / "v.db")

    # re-open twice more — each __init__ re-runs _upgrade_schema
    SqliteVault(url, memory_dir=tmp_path / "mem")
    v3 = SqliteVault(url, memory_dir=tmp_path / "mem")

    assert _tool_columns(tmp_path / "v.db") == cols_before, "re-running altered the schema"
    assert v3.get_tool("t-parity").effect_tags == ["money_move", "net_read"], (
        "re-running the migration disturbed stored data")


def test_migration_completes_a_PARTIALLY_migrated_store(tmp_path):
    """An INTERRUPTED migration must finish on the next open.

    ``_upgrade_schema`` commits per column, so a crash mid-loop leaves a store
    with SOME of the new columns. Re-opening must add exactly the missing ones,
    leave the already-present ones alone (their ADD COLUMN raises
    duplicate-column, which is the idempotency mechanism), and lose no data.
    This is the failure path: every caught error happens BEFORE any row write,
    so a store can never be left worse than it started.
    """
    from systemu.storage.sqlite.vault import SqliteVault

    db = tmp_path / "v.db"
    url = f"sqlite:///{db}"
    v1 = SqliteVault(url, memory_dir=tmp_path / "mem")
    v1.save_tool(_populated_tool())

    # Simulate a crash after only some columns landed: drop a subset, keeping
    # effect_tags present so this run exercises BOTH branches of the loop.
    dropped = ["trusted_inprocess", "toolset", "timeout_seconds"]
    con = sqlite3.connect(str(db))
    try:
        for col in dropped:
            con.execute(f"ALTER TABLE tools DROP COLUMN {col}")
        con.commit()
    finally:
        con.close()
    partial = _tool_columns(db)
    assert "effect_tags" in partial and "trusted_inprocess" not in partial

    v2 = SqliteVault(url, memory_dir=tmp_path / "mem")

    assert set(dropped) <= _tool_columns(db), "the interrupted migration did not finish"
    survivor = v2.get_tool("t-parity")
    assert survivor.name == "probe_all", "a partially-migrated store lost its rows"
    # a column that was never dropped kept its stored value ...
    assert survivor.effect_tags == ["money_move", "net_read"]
    # ... and a re-added one reads the model default, not garbage
    assert survivor.trusted_inprocess is False
    assert survivor.timeout_seconds is None


def test_migration_adds_columns_to_a_legacy_store_without_destroying_rows(tmp_path):
    """A store written BEFORE these columns existed must gain them on open, keep
    every pre-existing value, and read the new fields as model defaults.

    The legacy schema is DERIVED by dropping the new columns from the real one —
    never hand-authored — so this cannot drift from what production creates.
    """
    from systemu.storage.sqlite.vault import SqliteVault

    db = tmp_path / "v.db"
    url = f"sqlite:///{db}"
    v1 = SqliteVault(url, memory_dir=tmp_path / "mem")
    v1.save_tool(_populated_tool())

    # Simulate a pre-migration store: drop the columns the migration adds.
    new_cols = sorted(_tool_columns(db) - _LEGACY_TOOL_COLUMNS)
    assert new_cols, "precondition: the migration must add at least one column"
    con = sqlite3.connect(str(db))
    try:
        for col in new_cols:
            con.execute(f"ALTER TABLE tools DROP COLUMN {col}")
        con.commit()
    finally:
        con.close()
    assert _tool_columns(db) == _LEGACY_TOOL_COLUMNS

    # Re-open: the migration must run and must not lose the surviving row.
    v2 = SqliteVault(url, memory_dir=tmp_path / "mem")
    assert _tool_columns(db) >= _LEGACY_TOOL_COLUMNS | set(new_cols), "columns not re-added"

    legacy = v2.get_tool("t-parity")
    assert legacy.name == "probe_all", "the legacy row was destroyed by the migration"
    assert legacy.description == "a tool exercising every persisted field"
    assert legacy.version == 3, "a pre-existing column value was lost"
    assert legacy.effect_tags == [], "a legacy row must read the new field as its default"

    # and the store is writable again at full fidelity
    v2.save_tool(_populated_tool())
    assert v2.get_tool("t-parity").effect_tags == ["money_move", "net_read"]


# The tools columns that existed BEFORE this migration. Used only to synthesize a
# legacy store in the test above.
_LEGACY_TOOL_COLUMNS = {
    "id", "name", "description", "tool_type", "parameters_schema", "return_schema",
    "implementation_notes", "dependencies", "implementation_path", "tool_md_path",
    "status", "forged_by_systemu", "enabled", "version", "dry_run_status",
    "dry_run_evidence", "last_successful_params", "evolution_history",
    "created_at", "updated_at",
}
