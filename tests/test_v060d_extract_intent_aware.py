"""Tests for v0.6.0-d — Stage 3 schema-aware activity extraction.

Covers:
  * Catalog enrichment helpers (_enrich_tool_for_catalog, _enrich_skill_for_catalog)
  * _summarise_schema correctly strips JSON Schema → {field: type} pairs
  * extract_and_process propagates intent_snapshot onto the Activity
  * extract_and_process now sends expected_outcome to the LLM
  * _upsert_skill picks up target_outcomes + produces from the spec
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixture

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


# ─────────────────────────────────────────────────────────────────────────────
# Schema summarisation

class TestSummariseSchema:
    def test_extracts_top_level_field_types(self):
        from systemu.pipelines.activity_extractor import _summarise_schema
        out = _summarise_schema({
            "type": "object",
            "properties": {
                "url":   {"type": "string"},
                "count": {"type": "integer"},
            },
        })
        assert out == {"url": "string", "count": "integer"}

    def test_handles_flat_dict_without_properties(self):
        from systemu.pipelines.activity_extractor import _summarise_schema
        out = _summarise_schema({"x": {"type": "string"}})
        assert out == {"x": "string"}

    def test_caps_field_count(self):
        from systemu.pipelines.activity_extractor import _summarise_schema
        big = {"properties": {f"f{i}": {"type": "string"} for i in range(40)}}
        out = _summarise_schema(big)
        assert len(out) == 20   # capped at 20

    def test_invalid_inputs_return_empty(self):
        from systemu.pipelines.activity_extractor import _summarise_schema
        assert _summarise_schema(None) == {}
        assert _summarise_schema("not a dict") == {}


# ─────────────────────────────────────────────────────────────────────────────
# Catalog enrichment

class TestCatalogEnrichment:
    def test_enrich_tool_includes_schemas(self, vault):
        from systemu.core.models import Tool, ToolStatus, ToolType
        from systemu.pipelines.activity_extractor import _enrich_tool_for_catalog

        t = Tool(
            id="tool_x", name="fetch_json",
            description="Fetch JSON from URL",
            tool_type=ToolType.PYTHON_FUNCTION,
            status=ToolStatus.DEPLOYED, enabled=True,
            parameters_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
            },
            return_schema={
                "type": "object",
                "properties": {"data": {"type": "object"}},
            },
        )
        vault.save_tool(t)

        index_entry = vault.load_index("tools")[0]
        entry = _enrich_tool_for_catalog(index_entry, vault)
        assert entry["name"] == "fetch_json"
        assert entry["parameters_schema"] == {"url": "string"}
        assert entry["return_schema"] == {"data": "object"}

    def test_enrich_skill_picks_up_new_fields(self):
        from systemu.pipelines.activity_extractor import _enrich_skill_for_catalog
        entry = _enrich_skill_for_catalog({
            "id": "s1", "name": "weather_capture",
            "description": "Capture weather",
            "target_outcomes": ["document weather data"],
            "produces": ["data", "structured_document"],
        })
        assert entry["target_outcomes"] == ["document weather data"]
        assert entry["produces"] == ["data", "structured_document"]

    def test_enrich_skill_defaults_when_legacy(self):
        from systemu.pipelines.activity_extractor import _enrich_skill_for_catalog
        # Legacy starter-pack skill index entry without target_outcomes / produces.
        entry = _enrich_skill_for_catalog({
            "id": "s1", "name": "legacy",
            "description": "A legacy skill",
        })
        assert entry["target_outcomes"] == []
        assert entry["produces"] == []


# ─────────────────────────────────────────────────────────────────────────────
# extract_and_process: intent_snapshot + expected_outcome propagation

class TestExtractAndProcessIntent:
    def _scroll(self, vault, intent="document weather data",
                expected_outcome="weather doc on disk"):
        from systemu.core.models import Objective, Scroll, ScrollStatus
        s = Scroll(
            id="scroll_ix", name="weather",
            source_session_id="x", raw_instructions_path="",
            narrative_md="The user did some things.",
            intent=intent,
            expected_outcome=expected_outcome,
            objectives=[Objective(
                id=1, goal="fetch weather data",
                success_criteria="JSON received",
                output_type="data",
            )],
            status=ScrollStatus.APPROVED,
        )
        vault.save_scroll(s)
        return s

    def test_activity_carries_intent_snapshot(self, vault, monkeypatch):
        scroll = self._scroll(vault)

        def fake_llm(**kw):
            return {
                "skills": [{
                    "name": "weather_data_fetch",
                    "description": "Fetch weather data",
                    "category": "data",
                    "proficiency_level": "intermediate",
                    "required_tools": ["fetch_json"],
                    "instructions_md": "Use fetch_json to retrieve weather data from the API.",
                    "target_outcomes": ["document weather data"],
                    "produces": ["data"],
                    "is_new": True, "existing_id": None,
                }],
                "tools": [{
                    "name": "fetch_json",
                    "description": "Fetch JSON from URL",
                    "tool_type": "python_function",
                    "parameters_schema": {"url": {"type": "string"}},
                    "return_schema": {"success": {"type": "boolean"}, "data": {"type": "object"}},
                    "implementation_notes": "Use requests.get and parse JSON",
                    "dependencies": ["requests"],
                    "is_new": True, "existing_id": None,
                }],
            }

        monkeypatch.setattr(
            "systemu.pipelines.activity_extractor.llm_call_json", fake_llm,
        )
        config = MagicMock()
        config.openrouter_api_key = "k"
        config.tier1_model = "t"
        config.auto_forge_tools = False
        config.non_interactive = False

        from systemu.pipelines import activity_extractor as ae
        ae.init_pipeline(config, vault)

        with patch("systemu.pipelines.shadow_decision.decide_shadow"):
            activity = ae.extract_and_process(scroll, vault=vault)

        assert activity is not None
        assert activity.intent_snapshot == "document weather data"

    def test_skill_picks_up_target_outcomes_and_produces(self, vault, monkeypatch):
        scroll = self._scroll(vault)

        def fake_llm(**kw):
            return {
                "skills": [{
                    "name": "weather_data_capture",
                    "description": "Capture and structure weather data",
                    "category": "data",
                    "proficiency_level": "intermediate",
                    "required_tools": ["fetch_json"],
                    "instructions_md": "Fetch weather JSON from the configured endpoint and parse the relevant fields.",
                    "target_outcomes": ["document factual data", "produce dated report"],
                    "produces": ["data", "data_extraction"],
                    "is_new": True, "existing_id": None,
                }],
                "tools": [{
                    "name": "fetch_json",
                    "description": "Fetch JSON from URL",
                    "tool_type": "python_function",
                    "parameters_schema": {"url": {"type": "string"}},
                    "return_schema": {"success": {"type": "boolean"}, "data": {"type": "object"}},
                    "implementation_notes": "x",
                    "dependencies": ["requests"],
                    "is_new": True, "existing_id": None,
                }],
            }

        monkeypatch.setattr(
            "systemu.pipelines.activity_extractor.llm_call_json", fake_llm,
        )
        config = MagicMock()
        config.openrouter_api_key = "k"
        config.tier1_model = "t"
        config.auto_forge_tools = False
        config.non_interactive = False

        from systemu.pipelines import activity_extractor as ae
        ae.init_pipeline(config, vault)

        with patch("systemu.pipelines.shadow_decision.decide_shadow"):
            activity = ae.extract_and_process(scroll, vault=vault)

        assert activity is not None
        skill = vault.get_skill(activity.required_skill_ids[0])
        assert "document factual data" in skill.target_outcomes
        assert "data" in skill.produces

    def test_llm_receives_expected_outcome_in_task_spec(self, vault, monkeypatch):
        scroll = self._scroll(
            vault,
            intent="i", expected_outcome="EXPECTED MARKER VALUE",
        )
        captured = {}

        def fake_llm(**kw):
            captured["user"] = kw.get("user", "")
            return {"skills": [], "tools": []}

        monkeypatch.setattr(
            "systemu.pipelines.activity_extractor.llm_call_json", fake_llm,
        )
        config = MagicMock()
        config.openrouter_api_key = "k"
        config.tier1_model = "t"
        config.auto_forge_tools = False
        config.non_interactive = False

        from systemu.pipelines import activity_extractor as ae
        ae.init_pipeline(config, vault)

        with patch("systemu.pipelines.shadow_decision.decide_shadow"):
            ae.extract_and_process(scroll, vault=vault)

        # The LLM user payload should contain the expected_outcome value
        assert "EXPECTED MARKER VALUE" in captured["user"]
