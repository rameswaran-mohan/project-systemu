import json as _json
from click.testing import CliRunner
from systemu.interface import cli_commands
from systemu.interface.command import verbs
from systemu.interface.command.result import CommandResult, CommandStatus


def test_cli_settings_set_invokes_verb(monkeypatch):
    seen = {}
    monkeypatch.setattr(verbs, "settings_set",
                        lambda k, v, *, vault: (seen.update({"k": k, "v": v})
                                                or CommandResult(status=CommandStatus.OK, summary="ok")))
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), object()))
    runner = CliRunner()
    res = runner.invoke(cli_commands.settings_cmd, ["set", "non_interactive", "true"])
    assert res.exit_code == 0
    assert seen == {"k": "non_interactive", "v": "true"}


class _FakeConfig:
    # minimal attrs the read-only `settings show` body reads
    tier1_model = "t1"; tier2_model = "t2"; tier3_model = "t3"
    non_interactive = False; vault_dir = "/tmp/vault"; openrouter_api_key = "k"


def test_cli_settings_bare_still_shows(monkeypatch):
    # back-compat: bare `settings` (no subcommand) still runs the read-only view
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (_FakeConfig(), object()))
    runner = CliRunner()
    res = runner.invoke(cli_commands.settings_cmd, [])
    assert res.exit_code == 0
