"""Tests for the v0.7.4 Pattern 2 [Dry-Run] button on /tools."""
from unittest.mock import MagicMock, patch

import pytest


def test_tools_page_renders_dryrun_button_for_forged_tools():
    """When a tool is FORGED with dry_run not run, the row must include a
    [Dry-Run] button in the actions column."""
    # We can't render NiceGUI without a running app loop. Instead, import
    # the helper that decides what action buttons to render for a row.
    from systemu.interface.pages.tools import _row_actions_for

    header = {"id": "tool_a", "status": "forged", "dry_run_status": "not_run", "enabled": False}
    actions = _row_actions_for(header)
    labels = [a["label"] for a in actions]
    assert "Dry-Run" in labels, f"expected Dry-Run action, got: {labels}"


def test_tools_page_no_dryrun_button_for_deployed_tools():
    from systemu.interface.pages.tools import _row_actions_for
    header = {"id": "tool_b", "status": "deployed", "dry_run_status": "passed"}
    actions = _row_actions_for(header)
    labels = [a["label"] for a in actions]
    assert "Dry-Run" not in labels, f"DEPLOYED tools should not show Dry-Run; got: {labels}"


# ── P2-T11: Gate-3 toggle routes its enable through verbs.tools_enable ─────────

class _FakeTool:
    def __init__(self, tool_id, name="fetch_json"):
        self.id = tool_id
        self.name = name


class _FakeVault:
    def __init__(self, tool):
        self._tool = tool

    def get_tool(self, tid):
        if self._tool is None or tid != self._tool.id:
            raise KeyError(tid)
        return self._tool

    def list_pending_notifications(self):
        return []


class _FakeState:
    def __init__(self, vault):
        self.vault = vault
        self.config = object()


@pytest.fixture
def _toggle_env(monkeypatch):
    """Neutralise the NiceGUI / asyncio seams so ``_toggle_enabled`` runs in a
    plain test process. Returns a dict the tests inspect."""
    import systemu.interface.pages.tools as tools_pg

    rec = {"notify": [], "heal_scheduled": []}

    monkeypatch.setattr(tools_pg.ui, "notify",
                        lambda msg, **k: rec["notify"].append((msg, k)))
    # asyncio.create_task needs a running loop; the toggle only uses it to
    # background the heal chain. Capture the coroutine without scheduling it.
    def _capture_task(coro):
        rec["heal_scheduled"].append(coro)
        coro.close()  # avoid "coroutine was never awaited" warnings
        return None
    monkeypatch.setattr(tools_pg.asyncio, "create_task", _capture_task)
    # Don't actually touch notifications / dependency reminders.
    monkeypatch.setattr(tools_pg, "_resolve_forge_notification", lambda *a, **k: None)
    monkeypatch.setattr(tools_pg, "_queue_dependency_reminder", lambda *a, **k: None)
    return tools_pg, rec


def test_toggle_enable_routes_through_verb_on_happy_path(_toggle_env, monkeypatch):
    tools_pg, rec = _toggle_env
    from systemu.interface.command.result import CommandResult, CommandStatus
    from systemu.interface.command import verbs

    tool = _FakeTool("tool_a")
    vault = _FakeVault(tool)
    monkeypatch.setattr(tools_pg.AppState, "get", classmethod(lambda cls: _FakeState(vault)))

    seen = {}
    def _fake_verb(tool_id, *, vault):
        seen["tool_id"] = tool_id
        return CommandResult(status=CommandStatus.OK, summary="Enabled tool fetch_json.",
                             data={"tool_id": tool_id})
    monkeypatch.setattr(verbs, "tools_enable", _fake_verb)

    tools_pg._toggle_enabled("tool_a", True)

    assert seen["tool_id"] == "tool_a"            # toggle delegated to the verb
    assert rec["heal_scheduled"], "heal chain should run after a successful enable"
    assert any(k.get("type") == "positive" for _, k in rec["notify"])


def test_toggle_enable_refused_when_verb_errors(_toggle_env, monkeypatch):
    """A non-dry-run-passed enable (verb ERROR) must NOT heal and must surface
    the verb's negative summary — the gate now protects the toggle too."""
    tools_pg, rec = _toggle_env
    from systemu.interface.command.result import CommandResult, CommandStatus
    from systemu.interface.command import verbs

    tool = _FakeTool("tool_a")
    vault = _FakeVault(tool)
    monkeypatch.setattr(tools_pg.AppState, "get", classmethod(lambda cls: _FakeState(vault)))

    monkeypatch.setattr(
        verbs, "tools_enable",
        lambda tool_id, *, vault: CommandResult(
            status=CommandStatus.ERROR,
            summary="Cannot enable fetch_json: dry_run_status='failed' (must be 'passed').",
            data={"tool_id": tool_id}),
    )

    tools_pg._toggle_enabled("tool_a", True)

    assert not rec["heal_scheduled"], "refused enable must not schedule the heal chain"
    msgs = [m for m, _ in rec["notify"]]
    assert any("dry_run" in m.lower() for m in msgs), f"expected the gate summary, got: {msgs}"
    assert any(k.get("type") == "negative" for _, k in rec["notify"])


def test_toggle_disable_still_uses_disable_tool(_toggle_env, monkeypatch):
    """Disable branch is unchanged — it must NOT route through the enable verb."""
    tools_pg, rec = _toggle_env
    from systemu.interface.command import verbs
    import systemu.pipelines.tool_service as _ts

    tool = _FakeTool("tool_a")
    vault = _FakeVault(tool)
    monkeypatch.setattr(tools_pg.AppState, "get", classmethod(lambda cls: _FakeState(vault)))

    monkeypatch.setattr(verbs, "tools_enable",
                        lambda *a, **k: pytest.fail("enable verb must not run on disable"))
    disabled = []
    monkeypatch.setattr(_ts, "disable_tool", lambda tid, v: disabled.append(tid) or True)

    tools_pg._toggle_enabled("tool_a", False)
    assert disabled == ["tool_a"]
