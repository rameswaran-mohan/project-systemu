"""v0.9.5 T0 — v2 tool visibility to LLM."""
from pathlib import Path
import pytest


class TestV2ToolsVisibleInLlmCatalog:
    """Guard tests that v2-registered tools appear in the LLM's prompt-time
    tool catalog alongside v1 vault tools."""

    def test_build_llm_tool_catalog_includes_v2_tools(self, tmp_path):
        """The helper that ShadowRuntime uses to build the LLM tool list
        must include v2-registered tools."""
        from systemu.runtime.tool_registry_v2 import registry as v2_registry
        # Force-load v2 tools that ship in v0.9.3 + v0.9.4 + v0.9.5
        import systemu.runtime.tools.file_tools  # noqa: F401
        import systemu.runtime.tools.skill_tools  # noqa: F401

        from systemu.runtime.shadow_runtime import _build_llm_tool_catalog

        # Build the catalog with no vault (vault=None should still produce v2 tools)
        catalog = _build_llm_tool_catalog(vault=None)
        names = [t["name"] for t in catalog]
        # v2-registered tools must be visible
        assert "read_file" in names
        assert "write_file" in names
        assert "skill_list_skills" in names
        assert "skill_view_skill" in names

    def test_catalog_entries_have_required_shape(self):
        """Each catalog entry must have name, description, parameters_schema."""
        from systemu.runtime.tool_registry_v2 import registry as v2_registry
        import systemu.runtime.tools.file_tools  # noqa: F401
        from systemu.runtime.shadow_runtime import _build_llm_tool_catalog

        catalog = _build_llm_tool_catalog(vault=None)
        for entry in catalog:
            assert "name" in entry
            assert "description" in entry
            assert "parameters_schema" in entry, f"entry missing parameters_schema: {entry}"

    def test_v2_tool_check_fn_filters_unavailable(self, monkeypatch):
        """When a v2 tool's check_fn returns False, it should be EXCLUDED
        from the LLM-visible catalog (otherwise the LLM tries unavailable
        tools and fails)."""
        from systemu.runtime.tool_registry_v2 import registry as v2_registry, ToolRegistry
        # Register a temporary tool with check_fn that returns False
        v2_registry.register(
            name="unavailable_test_tool",
            toolset="test",
            schema={"type": "object"},
            handler=lambda **kw: None,
            check_fn=lambda: False,
            description="Should not be visible",
        )
        try:
            from systemu.runtime.shadow_runtime import _build_llm_tool_catalog
            catalog = _build_llm_tool_catalog(vault=None)
            names = [t["name"] for t in catalog]
            assert "unavailable_test_tool" not in names
        finally:
            v2_registry._tools.pop("unavailable_test_tool", None)
