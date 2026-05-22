"""Tests for v0.5.0-b — Tool.evolution_history audit + version bump."""
from __future__ import annotations

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.pipelines.tool_dry_run import record_evolution


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _t():
    return Tool(
        id="tool_x", name="x", description="t",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.DEPLOYED, enabled=True,
    )


class TestRecordEvolution:
    def test_bump_increments_version(self, vault):
        t = _t()
        vault.save_tool(t)
        assert t.version == 1
        record_evolution(t, mode="bump", reason="fix bug",
                          diff_summary="added error handling", vault=vault)
        reloaded = vault.get_tool("tool_x")
        assert reloaded.version == 2
        assert len(reloaded.evolution_history) == 1
        assert reloaded.evolution_history[0]["mode"] == "bump"
        assert reloaded.evolution_history[0]["reason"] == "fix bug"

    def test_explicit_version_used_when_provided(self, vault):
        t = _t()
        vault.save_tool(t)
        record_evolution(t, mode="bump", reason="r", diff_summary="d",
                          vault=vault, new_version=5)
        assert vault.get_tool("tool_x").version == 5

    def test_fork_starts_at_one(self, vault):
        # Fork = a brand-new tool record; version starts at 1.
        new_tool = _t()
        new_tool.id = "tool_x_fork"
        new_tool.name = "x_fork"
        vault.save_tool(new_tool)
        record_evolution(new_tool, mode="fork",
                          reason="forked from tool_x for specialized use",
                          diff_summary="adds templating param",
                          vault=vault)
        reloaded = vault.get_tool("tool_x_fork")
        assert reloaded.version == 1
        assert reloaded.evolution_history[0]["mode"] == "fork"

    def test_appends_to_existing_history(self, vault):
        t = _t()
        t.evolution_history = [{"version": 1, "mode": "bump", "reason": "old"}]
        vault.save_tool(t)
        record_evolution(t, mode="bump", reason="new", diff_summary="d", vault=vault)
        reloaded = vault.get_tool("tool_x")
        assert len(reloaded.evolution_history) == 2
        assert reloaded.evolution_history[-1]["reason"] == "new"

    def test_reason_capped_at_500(self, vault):
        t = _t()
        vault.save_tool(t)
        long_reason = "x" * 1000
        record_evolution(t, mode="bump", reason=long_reason,
                          diff_summary="d", vault=vault)
        entry = vault.get_tool("tool_x").evolution_history[0]
        assert len(entry["reason"]) <= 500
