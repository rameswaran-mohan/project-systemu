"""Tests for the new `sharing_on tools dry-run <tool_id>` CLI (v0.7.4 Pattern 2)."""
from click.testing import CliRunner
from unittest.mock import MagicMock, patch


def test_tools_dryrun_invokes_reconciler_for_specific_tool():
    """The new CLI must invoke dry_run_tool with the named tool."""
    from systemu.interface.cli_commands import tools_group

    runner = CliRunner()

    fake_tool = MagicMock()
    fake_tool.id = "tool_42"
    fake_tool.name = "named_tool"
    fake_tool.implementation_path = "/tmp/foo.py"

    fake_vault = MagicMock()
    fake_vault.get_tool.return_value = fake_tool

    fake_config = MagicMock()
    fake_config.vault_dir = "/tmp"
    fake_config.docker_tool_timeout = 30

    class _R:
        success = True
        status = "passed"
        elapsed_ms = 10
        error = None
        skip_reason = None
        params_used = {}

    with patch(
        "systemu.interface.cli_commands._get_vault_and_config",
        return_value=(fake_config, fake_vault),
    ), patch(
        "systemu.pipelines.tool_dry_run.dry_run_tool", return_value=_R(),
    ):
        result = runner.invoke(tools_group, ["dry-run", "tool_42"])

    assert result.exit_code == 0, f"exit_code={result.exit_code} output={result.output}"
    assert "passed" in result.output.lower()
    fake_vault.get_tool.assert_called_once_with("tool_42")
