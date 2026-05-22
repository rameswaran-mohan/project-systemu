"""— `_build_catalog` + `_enrich_tool_for_catalog` read schemas
from the index header alone, no per-tool `vault.get_tool()` fetch.

Closes review issue #4.  Verifies both:
  * `_tool_header` now carries `parameters_schema_summary` + `return_schema_summary`
  * Catalog builders in both pipelines consume those header fields and do
    NOT call `vault.get_tool()` per tool.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder", "notifications"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    (tmp_path / "global_memory.jsonl").write_text("")
    (tmp_path / "chat_history.jsonl").write_text("")
    return Vault(str(tmp_path))


def _seed_tools(vault, n=10):
    from systemu.core.models import Tool, ToolStatus, ToolType
    for i in range(n):
        vault.save_tool(Tool(
            id=f"t_{i}", name=f"tool_{i}", description=f"d{i}",
            tool_type=ToolType.PYTHON_FUNCTION,
            status=ToolStatus.DEPLOYED, enabled=True,
            parameters_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
            },
            return_schema={
                "type": "object",
                "properties": {"data": {"type": "object"}},
            },
        ))


class TestHeaderCarriesSchemas:
    def test_tool_header_includes_summaries(self, vault):
        _seed_tools(vault, n=1)
        index = vault.load_index("tools")
        assert "parameters_schema_summary" in index[0]
        assert "return_schema_summary" in index[0]
        assert index[0]["parameters_schema_summary"] == {"x": "string"}
        assert index[0]["return_schema_summary"] == {"data": "object"}


class TestCatalogNoNPlusOne:
    def test_scroll_validator_does_not_fetch_per_tool(self, vault):
        _seed_tools(vault, n=10)
        from systemu.pipelines import scroll_validator as sv

        with patch.object(vault, "get_tool",
                          side_effect=AssertionError("must not be called")):
            catalog = sv._build_catalog(vault)

        assert len(catalog["tools"]) == 10
        # Schemas still surface — sourced from headers
        assert all("parameters_schema" in t for t in catalog["tools"])
        assert all(t["parameters_schema"] for t in catalog["tools"])
        assert all(t["return_schema"]    for t in catalog["tools"])

    def test_activity_extractor_does_not_fetch_per_tool(self, vault):
        _seed_tools(vault, n=10)
        from systemu.pipelines import activity_extractor as ae

        index = vault.load_index("tools")
        with patch.object(vault, "get_tool",
                          side_effect=AssertionError("must not be called")):
            entries = [ae._enrich_tool_for_catalog(t, vault) for t in index]

        assert len(entries) == 10
        assert all(e["parameters_schema"] for e in entries)
        assert all(e["return_schema"]    for e in entries)
