import json as _json
from click.testing import CliRunner
from systemu.interface import cli_commands
from systemu.interface.command import verbs
from systemu.interface.command.result import CommandStatus


class _FakeTool:
    def __init__(self):
        self.id = "tool_a"; self.name = "fetch_json"; self.tool_type = "http"
        self.status = "deployed"; self.description = "Fetch JSON"
        self.enabled = True; self.dry_run_status = "passed"; self.version = 1
        self.evolution_history = []


class _FakeVault:
    def __init__(self): self.tool = _FakeTool(); self.saved = None
    def get_tool(self, tid):
        if tid != self.tool.id: raise KeyError(tid)
        return self.tool
    def save_tool(self, t): self.saved = t
    def list_tools(self, status=None):
        return [{"id": "tool_a", "name": "fetch_json", "tool_type": "http",
                 "status": "deployed", "description": "Fetch JSON",
                 "enabled": True, "dry_run_status": "passed"}]


def test_tools_show_returns_view_model_data():
    result = verbs.tools_show("tool_a", vault=_FakeVault())
    assert result.status == CommandStatus.OK
    assert result.data["card"]["name"] == "fetch_json"


def test_tools_recalibrate_bumps_version_and_records_history():
    v = _FakeVault()
    result = verbs.tools_recalibrate("tool_a", reason="slow", vault=v)
    assert result.status == CommandStatus.OK
    assert v.saved.version == 2
    assert v.saved.evolution_history[-1]["reason"] == "slow"
    assert v.saved.evolution_history[-1]["mode"] == "bump"


def test_settings_set_writes_known_key(monkeypatch):
    written = {}
    monkeypatch.setattr(verbs, "_persist_setting",
                        lambda key, value: written.update({key: value}))
    result = verbs.settings_set("non_interactive", "true", vault=None)
    assert result.status == CommandStatus.OK
    assert written["non_interactive"] == "true"


def test_settings_set_rejects_unknown_key():
    result = verbs.settings_set("bogus_key", "x", vault=None)
    assert result.status == CommandStatus.ERROR
    assert "unknown" in result.summary.lower()


def test_cli_tools_show_json(monkeypatch):
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), _FakeVault()))
    runner = CliRunner()
    res = runner.invoke(cli_commands.tools_group, ["show", "tool_a", "--json"])
    assert res.exit_code == 0
    assert _json.loads(res.output.strip())["data"]["card"]["id"] == "tool_a"
