"""Tests for v0.6.0-e + v0.6.0-f — Stage 4 forge spec + Stage 5 shadow tiebreak.

Stage 4 (forge spec): the tool_forge spec LLM now receives scroll intent,
expected_outcome, and the requesting objective so it can design schemas
that fit the data-flow chain, not just match the bare tool name.

Stage 5 (shadow tiebreak): the shadow_decision LLM tiebreak now receives
scroll_intent + scroll_expected_outcome so semantic match plays into the
assignment, not just ID overlap.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures

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


def _config():
    c = MagicMock()
    c.intelligent_supervisor_enabled = False
    c.openrouter_api_key = "test"
    c.tier1_model = "t1"
    c.tier2_model = "t2"
    return c


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4 — _spec_and_forge_new payload enrichment

class TestForgeSpecPayload:
    def test_payload_includes_intent_when_provided(self, vault, monkeypatch):
        from systemu.pipelines import tool_forge as tf

        captured: dict = {}

        def fake_llm(**kw):
            captured["user"] = kw.get("user", "")
            # Return a minimal valid spec so _spec_and_forge_new doesn't fail.
            return {
                "name": "weather_fetch",
                "description": "Fetch weather",
                "tool_type": "python_function",
                "parameters_schema": {"city": {"type": "string"}},
                "return_schema": {"success": {"type": "boolean"}, "data": {"type": "object"}},
                "implementation_notes": "x",
                "dependencies": [],
            }

        monkeypatch.setattr(
            "systemu.pipelines.tool_forge.llm_call_json", fake_llm,
        )
        # Stub forge_tool so we don't actually try to generate code
        monkeypatch.setattr(
            "systemu.pipelines.tool_forge.forge_tool",
            lambda tool, scroll, config, vault: tool,
        )

        tf._spec_and_forge_new(
            tool_name="weather_fetch",
            context_hint="some narrative",
            config=_config(),
            vault=vault,
            scroll_intent="Document current weather data",
            scroll_expected_outcome="A weather report exists on disk",
            requesting_objective={
                "id": 1, "goal": "fetch weather JSON",
                "success_criteria": "JSON received",
                "output_type": "data",
            },
        )

        assert "Document current weather data" in captured["user"]
        assert "A weather report exists on disk" in captured["user"]
        assert "fetch weather JSON" in captured["user"]
        assert '"output_type": "data"' in captured["user"]

    def test_payload_omits_intent_keys_when_not_provided(self, vault, monkeypatch):
        from systemu.pipelines import tool_forge as tf

        captured: dict = {}

        def fake_llm(**kw):
            captured["user"] = kw.get("user", "")
            return {
                "name": "x", "description": "y",
                "tool_type": "python_function",
                "parameters_schema": {}, "return_schema": {},
                "implementation_notes": "x", "dependencies": [],
            }

        monkeypatch.setattr(
            "systemu.pipelines.tool_forge.llm_call_json", fake_llm,
        )
        monkeypatch.setattr(
            "systemu.pipelines.tool_forge.forge_tool",
            lambda tool, scroll, config, vault: tool,
        )

        tf._spec_and_forge_new(
            tool_name="x",
            context_hint="hint only",
            config=_config(),
            vault=vault,
        )

        # No intent keys should appear in the payload
        payload = json.loads(captured["user"])
        assert "scroll_intent" not in payload
        assert "scroll_expected_outcome" not in payload
        assert "requesting_objective" not in payload
        assert payload["tool_name"] == "x"
        assert payload["scroll_narrative"] == "hint only"


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4 — forge_tool_by_name auto-resolves intent context

class TestForgeByNameAutoResolves:
    def test_when_tool_not_in_vault_pulls_intent_from_scroll(self, vault, monkeypatch):
        from systemu.core.models import (
            Activity, ActivityStatus, Objective, Scroll, ScrollStatus,
        )
        from systemu.pipelines import tool_forge as tf

        # Set up scroll → activity referencing a missing tool name
        scroll = Scroll(
            id="scroll_int", name="t",
            source_session_id="x", raw_instructions_path="",
            narrative_md="n",
            intent="document factual weather data",
            expected_outcome="report.md exists with weather rows",
            objectives=[Objective(
                id=1, goal="fetch weather using brand_new_tool",
                success_criteria="data received", output_type="data",
            )],
            status=ScrollStatus.APPROVED,
        )
        vault.save_scroll(scroll)

        act = Activity(
            id="act_int", name="t", scroll_id=scroll.id,
            missing_tools=["brand_new_tool"],
            status=ActivityStatus.PARTIAL,
        )
        vault.save_activity(act)

        captured: dict = {}

        def fake_llm(**kw):
            captured["user"] = kw.get("user", "")
            return {
                "name": "brand_new_tool", "description": "y",
                "tool_type": "python_function",
                "parameters_schema": {}, "return_schema": {},
                "implementation_notes": "x", "dependencies": [],
            }

        monkeypatch.setattr(
            "systemu.pipelines.tool_forge.llm_call_json", fake_llm,
        )
        monkeypatch.setattr(
            "systemu.pipelines.tool_forge.forge_tool",
            lambda tool, scroll, config, vault: tool,
        )

        tf.forge_tool_by_name(
            "brand_new_tool",
            config=_config(),
            vault=vault,
        )

        # The auto-resolved intent context should be in the payload
        payload = json.loads(captured["user"])
        assert payload.get("scroll_intent") == "document factual weather data"
        assert payload.get("scroll_expected_outcome") == "report.md exists with weather rows"
        assert payload.get("requesting_objective", {}).get("output_type") == "data"


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5 — shadow_decision tiebreak gets intent

class TestShadowTiebreakIntent:
    def test_payload_includes_scroll_intent(self, vault, monkeypatch):
        from systemu.core.models import (
            Activity, ActivityStatus, Objective, Scroll, ScrollStatus,
            Shadow, ShadowStatus,
        )
        from systemu.pipelines import shadow_decision as sd

        scroll = Scroll(
            id="scroll_sd", name="t", source_session_id="x",
            raw_instructions_path="", narrative_md="n",
            intent="finance reporting daily",
            expected_outcome="dated finance report exists",
            objectives=[Objective(id=1, goal="fetch", success_criteria="x")],
            status=ScrollStatus.APPROVED,
        )
        vault.save_scroll(scroll)

        act = Activity(
            id="act_sd", name="t", scroll_id=scroll.id,
            required_tool_ids=["tool_a"], required_skill_ids=["skill_a"],
            status=ActivityStatus.UNASSIGNED,
            intent_snapshot="finance reporting daily",
        )
        vault.save_activity(act)

        # Add a single specialist so the LLM has something to consider
        s = Shadow(
            id="sh_a", name="FinanceShadow",
            description="finance specialist",
            system_prompt="t", status=ShadowStatus.AWAKENED,
            skill_ids=["skill_a"], available_tool_ids=["tool_a"],
        )
        vault.save_shadow(s)

        # Capture all LLM calls; only the first one is the tiebreak we're
        # asserting on (subsequent calls may be persona generation, etc.).
        all_calls: list = []

        def fake_llm(**kw):
            all_calls.append(kw.get("user", ""))
            return {"decision": "ASSIGN_EXISTING", "shadow_id": "sh_a",
                    "reasoning": "ok"}

        monkeypatch.setattr(
            "systemu.pipelines.shadow_decision.llm_call_json", fake_llm,
        )

        sd._llm_shadow_decision(act, _config(), vault)

        tiebreak_payload = json.loads(all_calls[0])
        assert tiebreak_payload.get("scroll_intent") == "finance reporting daily"
        assert tiebreak_payload.get("scroll_expected_outcome") == "dated finance report exists"

    def test_intent_snapshot_takes_priority_over_scroll_lookup(self, vault, monkeypatch):
        """If Activity.intent_snapshot is set, use it directly (avoids
        re-loading the scroll on every decision)."""
        from systemu.core.models import (
            Activity, ActivityStatus, Scroll, ScrollStatus,
            Shadow, ShadowStatus,
        )
        from systemu.pipelines import shadow_decision as sd

        scroll = Scroll(
            id="scroll_x", name="t", source_session_id="x",
            raw_instructions_path="", narrative_md="n",
            intent="OLD-INTENT-FROM-SCROLL",
            expected_outcome="x",
            status=ScrollStatus.APPROVED,
        )
        vault.save_scroll(scroll)

        act = Activity(
            id="act_x", name="t", scroll_id=scroll.id,
            required_tool_ids=[], required_skill_ids=[],
            status=ActivityStatus.UNASSIGNED,
            intent_snapshot="FROZEN-INTENT-SNAPSHOT",
        )
        vault.save_activity(act)

        s = Shadow(
            id="sh_x", name="X", description="d",
            system_prompt="t", status=ShadowStatus.AWAKENED,
        )
        vault.save_shadow(s)

        all_calls: list = []

        def fake_llm(**kw):
            all_calls.append(kw.get("user", ""))
            return {"decision": "CREATE_NEW", "reasoning": "x"}

        monkeypatch.setattr(
            "systemu.pipelines.shadow_decision.llm_call_json", fake_llm,
        )
        # Stub create_shadow so the test stays minimal
        monkeypatch.setattr(
            "systemu.pipelines.shadow_decision.create_shadow",
            lambda *a, **kw: s,
        )

        sd._llm_shadow_decision(act, _config(), vault)

        tiebreak_payload = json.loads(all_calls[0])
        # Snapshot wins over scroll's current intent value
        assert tiebreak_payload.get("scroll_intent") == "FROZEN-INTENT-SNAPSHOT"
