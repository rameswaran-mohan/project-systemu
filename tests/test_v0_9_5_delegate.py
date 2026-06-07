"""v0.9.5 T1 — delegate.spawn_subagent tests."""
from unittest.mock import patch
import pytest


class TestDelegateSpawnSubagent:
    def test_returns_child_result(self, monkeypatch):
        from systemu.runtime.tools.delegate import spawn_subagent
        monkeypatch.setattr(
            "systemu.runtime.tools.delegate.llm_call_json",
            lambda **kw: {"summary": "child finished", "key_findings": ["A", "B"]},
        )
        from sharing_on.config import Config
        result = spawn_subagent(
            task="research X",
            config=Config(),
            parent_depth=0,
        )
        assert result["success"] is True
        assert "summary" in result
        assert result["depth"] == 1

    def test_rejects_recursion_at_max_depth(self):
        from systemu.runtime.tools.delegate import spawn_subagent
        from sharing_on.config import Config
        cfg = Config()
        cfg.delegate_max_depth = 2
        result = spawn_subagent(
            task="recurse forever",
            config=cfg,
            parent_depth=2,  # already at max
        )
        assert result["success"] is False
        assert "max_depth" in result["error"].lower() or "depth" in result["error"].lower()

    def test_handles_llm_exception(self, monkeypatch):
        from systemu.runtime.tools.delegate import spawn_subagent
        def boom(**kw):
            raise RuntimeError("LLM unavailable")
        monkeypatch.setattr(
            "systemu.runtime.tools.delegate.llm_call_json", boom,
        )
        from sharing_on.config import Config
        result = spawn_subagent(
            task="test", config=Config(), parent_depth=0,
        )
        assert result["success"] is False
        assert "unavailable" in result["error"].lower()

    def test_tool_whitelist_excludes_delegate(self, monkeypatch):
        """Child whitelist must exclude 'delegate' (and 'spawn_subagent')
        to prevent infinite recursion."""
        from systemu.runtime.tools.delegate import _compute_child_whitelist
        parent_whitelist = {"read_file", "write_file", "delegate", "spawn_subagent"}
        child_whitelist = _compute_child_whitelist(parent_whitelist)
        assert "delegate" not in child_whitelist
        assert "spawn_subagent" not in child_whitelist
        assert "read_file" in child_whitelist
        assert "write_file" in child_whitelist


class TestDelegateRegistered:
    def test_registered_in_v2_registry(self):
        from systemu.runtime.tool_registry_v2 import registry as singleton
        import systemu.runtime.tools.delegate  # noqa: F401
        entry = singleton.get("spawn_subagent")
        assert entry is not None
        assert entry.toolset == "delegate"

    def test_dynamic_schema_reflects_config_limits(self, monkeypatch):
        """The tool's description should mention the current config limits
        via dynamic_schema_overrides (Hermes pattern)."""
        from systemu.runtime.tool_registry_v2 import registry as singleton
        import systemu.runtime.tools.delegate  # noqa: F401
        entry = singleton.get("spawn_subagent")
        assert entry.dynamic_schema_overrides is not None
        overrides = entry.dynamic_schema_overrides()
        # The dynamic description should mention max_depth or max_turns
        joined = str(overrides)
        assert "depth" in joined.lower() or "max" in joined.lower()
