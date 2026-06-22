"""Tests for the new `sharing_on decisions` CLI subgroup (v0.8.0 Pattern 1)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


def test_decisions_list_shows_pending():
    from systemu.interface.cli_commands import decisions_group
    from systemu.approval.decision_queue import OperatorDecision

    fake_d = OperatorDecision(
        id="dec_xyz",
        title="Forge new tool?",
        body="Tool: x",
        options=["Skip", "Forge"],
        context={"tool_id": "t1"},
        dedup_key="tool_forge:t1",
        status="pending",
        choice=None,
        created_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
        resolved_at=None,
    )

    fake_vault = MagicMock()
    fake_config = MagicMock()
    fake_queue = MagicMock()
    fake_queue.list_pending.return_value = [fake_d]

    runner = CliRunner()
    with patch(
        "systemu.interface.cli_commands._get_vault_and_config",
        return_value=(fake_config, fake_vault),
    ), patch(
        "systemu.approval.decision_queue.OperatorDecisionQueue",
        return_value=fake_queue,
    ):
        result = runner.invoke(decisions_group, ["list"])

    assert result.exit_code == 0, result.output
    assert "dec_xyz" in result.output
    assert "Forge new tool" in result.output


def test_decisions_list_empty_shows_friendly_message():
    from systemu.interface.cli_commands import decisions_group
    fake_vault = MagicMock()
    fake_config = MagicMock()
    fake_queue = MagicMock()
    fake_queue.list_pending.return_value = []

    runner = CliRunner()
    with patch(
        "systemu.interface.cli_commands._get_vault_and_config",
        return_value=(fake_config, fake_vault),
    ), patch(
        "systemu.approval.decision_queue.OperatorDecisionQueue",
        return_value=fake_queue,
    ):
        result = runner.invoke(decisions_group, ["list"])

    assert result.exit_code == 0
    assert "No pending decisions" in result.output


def test_decisions_resolve_invokes_queue_resolve():
    from systemu.interface.cli_commands import decisions_group

    fake_vault = MagicMock()
    fake_config = MagicMock()
    fake_queue = MagicMock()
    resolved_d = MagicMock()
    resolved_d.choice = "Forge"
    fake_queue.resolve.return_value = resolved_d

    runner = CliRunner()
    with patch(
        "systemu.interface.cli_commands._get_vault_and_config",
        return_value=(fake_config, fake_vault),
    ), patch(
        "systemu.approval.decision_queue.OperatorDecisionQueue",
        return_value=fake_queue,
    ):
        result = runner.invoke(decisions_group, ["resolve", "dec_xyz", "--choice", "Forge"])

    assert result.exit_code == 0, result.output
    fake_queue.resolve.assert_called_once_with("dec_xyz", choice="Forge")
    assert "Forge" in result.output


def test_decisions_resolve_unknown_id_exits_1():
    from systemu.interface.cli_commands import decisions_group

    fake_vault = MagicMock()
    fake_config = MagicMock()
    fake_queue = MagicMock()
    fake_queue.resolve.side_effect = KeyError("dec_missing not found")

    runner = CliRunner()
    with patch(
        "systemu.interface.cli_commands._get_vault_and_config",
        return_value=(fake_config, fake_vault),
    ), patch(
        "systemu.approval.decision_queue.OperatorDecisionQueue",
        return_value=fake_queue,
    ):
        result = runner.invoke(decisions_group, ["resolve", "dec_missing", "--choice", "Forge"])

    assert result.exit_code == 1
    assert "Not found" in result.output or "not found" in result.output


def test_decisions_resolve_invalid_choice_exits_2():
    from systemu.interface.cli_commands import decisions_group

    fake_vault = MagicMock()
    fake_config = MagicMock()
    fake_queue = MagicMock()
    fake_queue.resolve.side_effect = ValueError("not in options")

    runner = CliRunner()
    with patch(
        "systemu.interface.cli_commands._get_vault_and_config",
        return_value=(fake_config, fake_vault),
    ), patch(
        "systemu.approval.decision_queue.OperatorDecisionQueue",
        return_value=fake_queue,
    ):
        result = runner.invoke(decisions_group, ["resolve", "dec_xyz", "--choice", "Wrong"])

    assert result.exit_code == 2
    assert "Invalid choice" in result.output or "invalid" in result.output.lower()


# ── Task 14: `decisions mode` verb (the gate-mode dial console twin) ──────────

def test_decisions_mode_no_set_shows_current(monkeypatch):
    """`decisions mode` with no --set prints the current mode + overrides +
    floor state, read from the persisted settings."""
    from systemu.interface.cli_commands import decisions_group

    runner = CliRunner()
    with patch(
        "systemu.runtime.gate_mode_settings.get_gate_mode_settings",
        return_value={"mode": "risk_tiered",
                      "overrides": {"forge": "deny"}, "no_floor": False},
    ):
        result = runner.invoke(decisions_group, ["mode"])

    assert result.exit_code == 0, result.output
    assert "risk_tiered" in result.output
    assert "forge" in result.output           # overrides surfaced
    # Floor state is reported (never silent about the safety floor).
    assert "floor" in result.output.lower()


def test_decisions_mode_set_persists_and_loads(monkeypatch, tmp_path):
    """`decisions mode --set bypass` persists the mode; load_default_policy()
    then reflects it. Targets a tmp cwd so the repo .env is never touched."""
    monkeypatch.chdir(tmp_path)
    for k in ("SYSTEMU_GATE_MODE", "SYSTEMU_GATE_OVERRIDES", "SYSTEMU_GATE_NO_FLOOR"):
        monkeypatch.delenv(k, raising=False)
    from systemu.interface.cli_commands import decisions_group
    from systemu.interface.command.gate_mode import load_default_policy, GateMode

    runner = CliRunner()
    result = runner.invoke(decisions_group, ["mode", "--set", "bypass"])
    assert result.exit_code == 0, result.output
    # The persisted setting is reflected by the live policy loader.
    assert load_default_policy().mode is GateMode.BYPASS


def test_decisions_mode_set_bypass_prints_danger(monkeypatch, tmp_path):
    """Setting (or showing) Bypass must print a DANGER note — never silent
    about the auto-grant mode (console twin of the persistent banner)."""
    monkeypatch.chdir(tmp_path)
    for k in ("SYSTEMU_GATE_MODE", "SYSTEMU_GATE_OVERRIDES", "SYSTEMU_GATE_NO_FLOOR"):
        monkeypatch.delenv(k, raising=False)
    from systemu.interface.cli_commands import decisions_group

    runner = CliRunner()
    result = runner.invoke(decisions_group, ["mode", "--set", "bypass"])
    assert result.exit_code == 0, result.output
    assert "DANGER" in result.output.upper() or "danger" in result.output.lower()
    assert "bypass" in result.output.lower()


def test_decisions_mode_invalid_set_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from systemu.interface.cli_commands import decisions_group
    runner = CliRunner()
    result = runner.invoke(decisions_group, ["mode", "--set", "banana"])
    assert result.exit_code != 0
