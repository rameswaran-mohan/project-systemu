import pytest
from systemu.interface.command import verbs
from systemu.interface.command.result import CommandStatus


class _FakeTool:
    def __init__(self, tool_id, dry_run_status="passed", enabled=False):
        self.id = tool_id
        self.name = "fetch_json"
        self.dry_run_status = dry_run_status
        self.enabled = enabled


class _FakeVault:
    def __init__(self, tool):
        self._tool = tool
        self.saved = None

    def get_tool(self, tid):
        if self._tool is None or tid != self._tool.id:
            raise KeyError(tid)
        return self._tool

    def save_tool(self, tool):
        self.saved = tool


# ── Policy tests (P2-T11) ────────────────────────────────────────────────────
# After consolidation, ``verbs.tools_enable`` is the ONE gated policy and
# DELEGATES the actual mutation (+ FORGED→DEPLOYED advance + log) to the ONE
# mechanism, ``tool_service.enable_tool``. These tests verify the POLICY —
# (a) the gate fires, (b) the happy path delegates, (c) not-found, (d)
# already-enabled NOOP — by monkeypatching the mechanism. The mechanism itself
# is tested elsewhere (test_tool_service / test_v0_9_7_forge_deploy).


@pytest.fixture
def _patched_mechanism(monkeypatch):
    """Patch ``tool_service.enable_tool`` and record its calls.

    Returns the ``calls`` list. The fake also flips the tool's ``enabled`` so
    the verb's data payload reflects a real enable (mirroring the mechanism).
    """
    calls = []

    def _fake_enable(tool_id, vault):
        calls.append((tool_id, vault))
        try:
            tool = vault.get_tool(tool_id)
            tool.enabled = True
            vault.save_tool(tool)
        except KeyError:
            return False
        return True

    import systemu.pipelines.tool_service as _ts
    monkeypatch.setattr(_ts, "enable_tool", _fake_enable)
    return calls


def test_tools_enable_delegates_to_mechanism_on_happy_path(_patched_mechanism):
    tool = _FakeTool("tool_a")
    vault = _FakeVault(tool)
    result = verbs.tools_enable("tool_a", vault=vault)
    assert result.status == CommandStatus.OK
    assert result.exit_code == 0
    # The verb delegated the write to the mechanism (one mechanism).
    assert _patched_mechanism == [("tool_a", vault)]
    assert tool.enabled is True
    assert result.data["tool_id"] == "tool_a"


def test_tools_enable_blocks_when_dry_run_not_passed(_patched_mechanism):
    tool = _FakeTool("tool_a", dry_run_status="failed")
    vault = _FakeVault(tool)
    result = verbs.tools_enable("tool_a", vault=vault)
    assert result.status == CommandStatus.ERROR
    # Gate fired BEFORE the mechanism — the mechanism must NOT be called.
    assert _patched_mechanism == []
    assert vault.saved is None
    assert "dry_run" in result.summary.lower()


def test_tools_enable_missing_tool_is_error_not_crash(_patched_mechanism):
    vault = _FakeVault(None)
    result = verbs.tools_enable("tool_missing", vault=vault)
    assert result.status == CommandStatus.ERROR
    assert result.exit_code == 1
    # Not-found is caught by the policy; the mechanism is not invoked.
    assert _patched_mechanism == []


def test_tools_enable_idempotent_when_already_enabled(_patched_mechanism):
    tool = _FakeTool("tool_a", enabled=True)
    vault = _FakeVault(tool)
    result = verbs.tools_enable("tool_a", vault=vault)
    assert result.status == CommandStatus.NOOP
    assert result.exit_code == 0
    # Already-enabled short-circuits in the policy — mechanism not called.
    assert _patched_mechanism == []


import json as _json
from click.testing import CliRunner
from systemu.interface import cli_commands


def test_cli_tools_enable_invokes_verb(monkeypatch):
    captured = {}
    from systemu.interface.command import verbs
    from systemu.interface.command.result import CommandResult, CommandStatus

    def _fake_enable(tool_id, *, vault):
        captured["tool_id"] = tool_id
        return CommandResult(status=CommandStatus.OK, summary="Enabled tool foo.",
                             data={"tool_id": tool_id})

    monkeypatch.setattr(verbs, "tools_enable", _fake_enable)
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), object()))

    runner = CliRunner()
    res = runner.invoke(cli_commands.tools_group, ["enable", "tool_a"])
    assert res.exit_code == 0
    assert captured["tool_id"] == "tool_a"
    assert "Enabled tool foo." in res.output


def test_cli_tools_enable_json_flag(monkeypatch):
    from systemu.interface.command import verbs
    from systemu.interface.command.result import CommandResult, CommandStatus
    monkeypatch.setattr(verbs, "tools_enable",
                        lambda tid, *, vault: CommandResult(
                            status=CommandStatus.OK, summary="ok", data={"tool_id": tid}))
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), object()))
    runner = CliRunner()
    res = runner.invoke(cli_commands.tools_group, ["enable", "tool_a", "--json"])
    assert res.exit_code == 0
    parsed = _json.loads(res.output.strip())
    assert parsed["status"] == "ok"
    assert parsed["data"]["tool_id"] == "tool_a"


def test_cli_tools_enable_error_exit_code(monkeypatch):
    from systemu.interface.command import verbs
    from systemu.interface.command.result import CommandResult, CommandStatus
    monkeypatch.setattr(verbs, "tools_enable",
                        lambda tid, *, vault: CommandResult(
                            status=CommandStatus.ERROR, summary="nope"))
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), object()))
    runner = CliRunner()
    res = runner.invoke(cli_commands.tools_group, ["enable", "tool_x"])
    assert res.exit_code == 1
