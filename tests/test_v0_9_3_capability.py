"""v0.9.3 capability ledger tests."""
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import pytest

from sharing_on.config import Config
from systemu.core.models import Capability


class TestCapabilityModel:
    def _make(self, **overrides):
        kwargs = dict(
            name="read_file",
            kind="tool",
            registered_at=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc),
            last_used_at=None,
            invocations=0,
            successes=0,
            failures=0,
            last_error=None,
        )
        kwargs.update(overrides)
        return Capability(**kwargs)

    def test_minimal_construction(self):
        c = self._make()
        assert c.name == "read_file"
        assert c.kind == "tool"
        assert c.invocations == 0

    def test_with_usage_stats(self):
        c = self._make(
            last_used_at=datetime(2026, 6, 7, 13, 0, tzinfo=timezone.utc),
            invocations=10, successes=8, failures=2,
            last_error="HTTP 403",
        )
        assert c.invocations == 10
        assert c.last_error == "HTTP 403"

    def test_json_round_trip(self):
        c = self._make(invocations=5, successes=4, failures=1)
        rebuilt = Capability.model_validate_json(c.model_dump_json())
        assert rebuilt.name == c.name
        assert rebuilt.invocations == 5


class TestConfigCapabilityFields:
    _KEYS = (
        "SYSTEMU_CAPABILITY_LEDGER_ENABLED",
        "SYSTEMU_CAPABILITY_TRACK_OUTCOMES",
        "SYSTEMU_CHECK_FN_CACHE_TTL_SECONDS",
        "SYSTEMU_TOOL_OUTPUT_MAX_CHARS_DEFAULT",
    )

    def test_defaults(self, monkeypatch):
        for k in self._KEYS:
            monkeypatch.delenv(k, raising=False)
        cfg = Config()
        assert cfg.capability_ledger_enabled is True
        assert cfg.capability_track_outcomes is True
        assert cfg.check_fn_cache_ttl_seconds == 30
        assert cfg.tool_output_max_chars_default == 100_000

    def test_env_overrides(self):
        env = {
            "SYSTEMU_CAPABILITY_LEDGER_ENABLED": "false",
            "SYSTEMU_CAPABILITY_TRACK_OUTCOMES": "false",
            "SYSTEMU_CHECK_FN_CACHE_TTL_SECONDS": "60",
            "SYSTEMU_TOOL_OUTPUT_MAX_CHARS_DEFAULT": "200000",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = Config.from_env()
        assert cfg.capability_ledger_enabled is False
        assert cfg.capability_track_outcomes is False
        assert cfg.check_fn_cache_ttl_seconds == 60
        assert cfg.tool_output_max_chars_default == 200_000


from systemu.vault.vault import Vault


class TestCapabilityLedger:
    def _make_vault(self, tmp_path):
        return Vault(root=tmp_path)

    def test_register_creates_capability(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        cap = cl.register(v, name="read_file", kind="tool")
        assert cap.name == "read_file"
        assert cap.kind == "tool"
        assert cap.invocations == 0

    def test_register_is_idempotent(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        cap1 = cl.register(v, name="read_file", kind="tool")
        cap2 = cl.register(v, name="read_file", kind="tool")
        # Same registered_at — proves it returned the existing record
        assert cap1.registered_at == cap2.registered_at

    def test_record_invocation_success(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        cl.register(v, name="read_file", kind="tool")
        cl.record_invocation(v, "read_file", success=True)
        cap = cl.get_capability(v, "read_file")
        assert cap.invocations == 1
        assert cap.successes == 1
        assert cap.failures == 0
        assert cap.last_used_at is not None

    def test_record_invocation_failure(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        cl.register(v, name="read_file", kind="tool")
        cl.record_invocation(v, "read_file", success=False, error="ENOENT")
        cap = cl.get_capability(v, "read_file")
        assert cap.invocations == 1
        assert cap.successes == 0
        assert cap.failures == 1
        assert cap.last_error == "ENOENT"

    def test_record_invocation_auto_registers(self, tmp_path):
        """First record_invocation on an unknown name auto-creates the row."""
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        cl.record_invocation(v, "auto_tool", success=True)
        cap = cl.get_capability(v, "auto_tool")
        assert cap is not None
        assert cap.invocations == 1

    def test_get_capability_returns_none_when_missing(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        assert cl.get_capability(v, "nonexistent") is None

    def test_list_capabilities_returns_all(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        cl.register(v, name="t1", kind="tool")
        cl.register(v, name="t2", kind="tool")
        cl.register(v, name="s1", kind="skill")
        out = cl.list_capabilities(v)
        names = sorted([c.name for c in out])
        assert names == ["s1", "t1", "t2"]

    def test_list_capabilities_filters_by_kind(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        cl.register(v, name="t1", kind="tool")
        cl.register(v, name="s1", kind="skill")
        tools_only = cl.list_capabilities(v, kind="tool")
        assert [c.name for c in tools_only] == ["t1"]

    def test_get_stats_returns_success_rate(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        cl.register(v, name="read_file", kind="tool")
        for _ in range(8):
            cl.record_invocation(v, "read_file", success=True)
        for _ in range(2):
            cl.record_invocation(v, "read_file", success=False)
        stats = cl.get_stats(v, "read_file")
        assert stats["invocations"] == 10
        assert stats["successes"] == 8
        assert stats["failures"] == 2
        assert abs(stats["success_rate"] - 0.8) < 0.001

    def test_get_stats_returns_none_for_unknown(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        assert cl.get_stats(v, "nope") is None

    def test_sidecar_file_path(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = self._make_vault(tmp_path)
        cl.register(v, name="t1", kind="tool")
        sidecar = tmp_path / "capabilities" / "_usage.json"
        assert sidecar.exists()


from systemu.core.models import Tool, ToolType
from systemu.runtime.tool_sandbox import ToolSandbox


class TestSandboxCapabilityHook:
    def test_success_records_invocation(self, tmp_path, monkeypatch):
        from systemu.runtime import capability_ledger as cl
        v = Vault(root=tmp_path)
        sandbox = ToolSandbox(vault=v, config=Config())
        tool = Tool(id="t", name="my_tool", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        # Invoke the post-call hook directly to drive the integration.
        sandbox._record_capability_outcome(tool=tool, success=True, error=None)
        cap = cl.get_capability(v, "my_tool")
        assert cap is not None
        assert cap.invocations == 1
        assert cap.successes == 1

    def test_failure_records_invocation(self, tmp_path, monkeypatch):
        from systemu.runtime import capability_ledger as cl
        v = Vault(root=tmp_path)
        sandbox = ToolSandbox(vault=v, config=Config())
        tool = Tool(id="t", name="my_tool", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        sandbox._record_capability_outcome(tool=tool, success=False, error="HTTP 403")
        cap = cl.get_capability(v, "my_tool")
        assert cap is not None
        assert cap.invocations == 1
        assert cap.failures == 1
        assert cap.last_error == "HTTP 403"

    def test_disabled_skips_hook(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = Vault(root=tmp_path)
        cfg = Config()
        cfg.capability_track_outcomes = False
        sandbox = ToolSandbox(vault=v, config=cfg)
        tool = Tool(id="t", name="my_tool", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        sandbox._record_capability_outcome(tool=tool, success=True, error=None)
        # Hook is a no-op — nothing written
        assert cl.get_capability(v, "my_tool") is None

    def test_degrades_when_vault_none(self):
        sandbox = ToolSandbox(vault=None, config=Config())
        tool = Tool(id="t", name="my_tool", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        # Must NOT raise
        sandbox._record_capability_outcome(tool=tool, success=True, error=None)

    def test_swallows_ledger_exception(self, tmp_path, monkeypatch):
        """If the ledger write blows up, the tool call must still succeed."""
        v = Vault(root=tmp_path)
        def boom(*a, **kw):
            raise RuntimeError("disk full")
        monkeypatch.setattr(
            "systemu.runtime.capability_ledger.record_invocation", boom)
        sandbox = ToolSandbox(vault=v, config=Config())
        tool = Tool(id="t", name="my_tool", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        # Must NOT raise
        sandbox._record_capability_outcome(tool=tool, success=True, error=None)


class TestCapabilityTools:
    def test_list_my_capabilities(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        from systemu.runtime.tools.capability_tools import capability_list_my_capabilities
        v = Vault(root=tmp_path)
        cl.register(v, name="read_file", kind="tool")
        cl.register(v, name="skill_a", kind="skill")
        results = capability_list_my_capabilities(vault=v)
        names = sorted([c["name"] for c in results])
        assert names == ["read_file", "skill_a"]

    def test_list_filters_by_kind(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        from systemu.runtime.tools.capability_tools import capability_list_my_capabilities
        v = Vault(root=tmp_path)
        cl.register(v, name="read_file", kind="tool")
        cl.register(v, name="skill_a", kind="skill")
        tools_only = capability_list_my_capabilities(vault=v, kind="tool")
        assert [c["name"] for c in tools_only] == ["read_file"]

    def test_get_stats_returns_dict(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        from systemu.runtime.tools.capability_tools import capability_get_stats
        v = Vault(root=tmp_path)
        cl.register(v, name="t1", kind="tool")
        cl.record_invocation(v, "t1", success=True)
        stats = capability_get_stats(vault=v, name="t1")
        assert stats["name"] == "t1"
        assert stats["invocations"] == 1
        assert stats["success_rate"] == 1.0

    def test_get_stats_none_for_unknown(self, tmp_path):
        from systemu.runtime.tools.capability_tools import capability_get_stats
        v = Vault(root=tmp_path)
        assert capability_get_stats(vault=v, name="nope") is None

    def test_last_used_returns_iso(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        from systemu.runtime.tools.capability_tools import capability_last_used
        v = Vault(root=tmp_path)
        cl.register(v, name="t1", kind="tool")
        cl.record_invocation(v, "t1", success=True)
        ts = capability_last_used(vault=v, name="t1")
        assert ts is not None
        from datetime import datetime
        datetime.fromisoformat(ts)  # parses cleanly

    def test_last_used_none_for_unused(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        from systemu.runtime.tools.capability_tools import capability_last_used
        v = Vault(root=tmp_path)
        cl.register(v, name="t1", kind="tool")
        # Never invoked
        assert capability_last_used(vault=v, name="t1") is None


class TestCapabilityCli:
    def _seed(self, tmp_path):
        from systemu.runtime import capability_ledger as cl
        v = Vault(root=tmp_path)
        cl.register(v, name="read_file", kind="tool")
        cl.register(v, name="skill_a", kind="skill")
        for _ in range(8):
            cl.record_invocation(v, "read_file", success=True)
        cl.record_invocation(v, "read_file", success=False, error="ENOENT")
        return v

    def test_capability_list(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import capability_cli
        self._seed(tmp_path)
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        result = CliRunner().invoke(capability_cli, ["list"])
        assert result.exit_code == 0
        assert "read_file" in result.output
        assert "skill_a" in result.output

    def test_capability_show(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import capability_cli
        self._seed(tmp_path)
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        result = CliRunner().invoke(capability_cli, ["show", "read_file"])
        assert result.exit_code == 0
        assert "read_file" in result.output
        assert "ENOENT" in result.output  # last_error surfaced

    def test_capability_stats(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import capability_cli
        self._seed(tmp_path)
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        result = CliRunner().invoke(capability_cli, ["stats"])
        assert result.exit_code == 0
        # Aggregate: 2 capabilities, total invocations >= 9
        assert "2" in result.output


class TestToolCheckFnNameField:
    def test_default_is_none(self):
        from systemu.core.models import Tool, ToolType
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION)
        assert tool.check_fn_name is None

    def test_set_to_string(self):
        from systemu.core.models import Tool, ToolType
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION,
                    check_fn_name="docker_is_running")
        assert tool.check_fn_name == "docker_is_running"

    def test_round_trip_json(self):
        from systemu.core.models import Tool, ToolType
        tool = Tool(id="t", name="t", description="d",
                    tool_type=ToolType.PYTHON_FUNCTION,
                    check_fn_name="check_postgres")
        rebuilt = Tool.model_validate_json(tool.model_dump_json())
        assert rebuilt.check_fn_name == "check_postgres"
