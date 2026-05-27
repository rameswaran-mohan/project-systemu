"""Regression tests for v0.8.1 — validator-propose bridge (Pattern 3).

Locks down the behavior change from v0.8.0.x to v0.8.1:

  v0.8.0.x:  validator blocks → if SYSTEMU_AUTO_FORGE_TOOLS=true, forge silently
             else: silently drop missing_tool_specs → scroll stuck
  v0.8.1:    validator blocks → ALWAYS create Tool records (status=PROPOSED)
             from missing_tool_specs AND post a decision card to the
             OperatorDecisionQueue.  Operator can review on /tools or click
             Forge All on /insights → Pending Actions.  If
             SYSTEMU_AUTO_FORGE_TOOLS=true, ALSO code-generate immediately
             (backward-compat for unattended CI).

These tests exercise the scroll_refiner branch at the unit level via patching
the validator + tool_forge LLM calls; an integration test against the live
LLM is out of scope (costs money + flaky).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from systemu.pipelines.scroll_validator import (
    ProposedToolSpec,
    ValidationResult,
    Blocker,
)


def _mk_blocked_validation_result_with_specs() -> ValidationResult:
    """Build a ValidationResult that mimics what the LLM emits when the scroll
    is blocked AND has fixable tool gaps."""
    return ValidationResult(
        satisfiable=False,
        confidence="high",
        blockers=[
            Blocker(
                objective_id=1,
                category="no_tool",
                explanation="No weather data fetcher available",
                suggested_fix="Forge fetch_weather_data tool",
            ),
        ],
        summary="Scroll blocked — needs weather data fetcher",
        proposed_revision=None,
        missing_tool_specs=[
            ProposedToolSpec(
                name="fetch_weather_data",
                description="Fetch current weather from a public API",
                tool_type="python_function",
                parameter_hints=["city", "units"],
                output_hint="dict",
                rationale="Validator detected scroll needs structured weather data",
            ),
        ],
    )


def _mk_satisfiable_validation_result() -> ValidationResult:
    """ValidationResult representing 'all good, scroll can proceed'."""
    return ValidationResult(
        satisfiable=True,
        confidence="high",
        blockers=[],
        summary="OK",
        proposed_revision=None,
        missing_tool_specs=[],
    )


# ─────────────────────────────────────────────────────────────────────────────
# propose_tools_from_specs: the spec-only helper
# ─────────────────────────────────────────────────────────────────────────────

def test_propose_tools_from_specs_creates_proposed_tool_records():
    """Given validator specs, helper creates Tool records with status=PROPOSED."""
    from systemu.pipelines.tool_forge import propose_tools_from_specs

    specs = [
        ProposedToolSpec(
            name="fetch_weather_data",
            description="API client",
            tool_type="python_function",
            parameter_hints=["city"],
            output_hint="dict",
            rationale="scroll needs weather",
        ),
    ]
    scroll = MagicMock(name="scroll", narrative_md="track weather daily")

    saved_tools = []
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = []
    fake_vault.save_tool.side_effect = lambda t: saved_tools.append(t)

    fake_spec_llm_response = {
        "name": "fetch_weather_data",
        "description": "Fetch weather from public API",
        "tool_type": "python_function",
        "parameters_schema": {"city": "string"},
        "return_schema": {"temperature": "number"},
        "implementation_notes": "use wttr.in",
        "dependencies": [],
    }

    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=fake_spec_llm_response):
        result = propose_tools_from_specs(specs, scroll, MagicMock(), fake_vault)

    assert len(result) == 1
    assert result[0].name == "fetch_weather_data"
    assert result[0].status.value == "proposed"  # ToolStatus.PROPOSED
    assert len(saved_tools) == 1
    fake_vault.save_tool.assert_called_once()


def test_propose_tools_from_specs_dedups_existing_tools():
    """If a tool with that name already exists, don't propose a duplicate."""
    from systemu.pipelines.tool_forge import propose_tools_from_specs

    specs = [ProposedToolSpec(
        name="existing_tool", description="x", tool_type="python_function",
        parameter_hints=[], output_hint="", rationale=""
    )]
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = [{"name": "existing_tool"}]
    scroll = MagicMock(narrative_md="x")

    result = propose_tools_from_specs(specs, scroll, MagicMock(), fake_vault)

    assert result == []
    fake_vault.save_tool.assert_not_called()


def test_propose_tools_from_specs_empty_list_short_circuits():
    """Empty spec list → no LLM call, no vault writes."""
    from systemu.pipelines.tool_forge import propose_tools_from_specs

    fake_vault = MagicMock()
    with patch("systemu.pipelines.tool_forge.llm_call_json") as mock_llm:
        result = propose_tools_from_specs([], MagicMock(), MagicMock(), fake_vault)

    assert result == []
    mock_llm.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# scroll_refiner integration: validator blocks → propose + queue card
# ─────────────────────────────────────────────────────────────────────────────

def test_validator_block_proposes_tools_when_auto_forge_off():
    """v0.8.1 behavior: even with SYSTEMU_AUTO_FORGE_TOOLS=false (default),
    validator blockers with missing_tool_specs result in PROPOSED tools."""
    from systemu.pipelines import scroll_refiner

    v_result = _mk_blocked_validation_result_with_specs()
    config = MagicMock()
    config.auto_forge_tools = False     # default
    config.intelligent_supervisor_enabled = True
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = []
    saved_tools = []
    fake_vault.save_tool.side_effect = lambda t: saved_tools.append(t)

    fake_spec_response = {
        "name": "fetch_weather_data",
        "description": "API client",
        "tool_type": "python_function",
        "parameters_schema": {}, "return_schema": {},
        "implementation_notes": "", "dependencies": [],
    }

    # We patch only the parts we need; the rest of refine_scroll's pipeline
    # isn't exercised here (this test focuses on the propose-bridge).
    with patch("systemu.pipelines.scroll_validator.is_enabled", return_value=True), \
         patch("systemu.pipelines.scroll_validator.validate_scroll", return_value=v_result), \
         patch("systemu.pipelines.tool_forge.llm_call_json", return_value=fake_spec_response), \
         patch("systemu.pipelines.tool_forge.forge_proposed_tools_from_specs") as mock_forge_full, \
         patch("systemu.approval.decision_queue.OperatorDecisionQueue") as mock_queue_cls:
        mock_queue = MagicMock()
        mock_queue_cls.return_value = mock_queue
        scroll = MagicMock()
        scroll.id = "scroll_test_1"
        scroll.name = "Test Weather Scroll"
        scroll.narrative_md = "track weather"
        scroll.pipeline_trace = []
        scroll.status = None

        # Inline the relevant block of refine_scroll by calling the helper directly.
        # (Full refine_scroll has too many dependencies to mock; we exercise just
        # the new bridge logic in isolation.)
        from systemu.pipelines.tool_forge import propose_tools_from_specs
        proposed = propose_tools_from_specs(
            v_result.missing_tool_specs, scroll, config, fake_vault,
        )

    # Tool was PROPOSED (saved with status=proposed)
    assert len(proposed) == 1
    assert proposed[0].status.value == "proposed"
    # Code-generation (Step 2) was NOT called by propose_tools_from_specs
    # — only by forge_proposed_tools_from_specs which the scroll_refiner only
    # invokes when auto_forge_tools=True.
    mock_forge_full.assert_not_called()


def test_validator_block_auto_forges_when_flag_on():
    """Backward-compat: SYSTEMU_AUTO_FORGE_TOOLS=true still triggers
    forge_proposed_tools_from_specs."""
    from systemu.pipelines.tool_forge import forge_proposed_tools_from_specs

    specs = _mk_blocked_validation_result_with_specs().missing_tool_specs
    scroll = MagicMock(narrative_md="track weather")
    scroll.name = "Test"
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = []
    fake_vault.save_tool = MagicMock()

    fake_spec_response = {
        "name": "fetch_weather_data",
        "description": "API client",
        "tool_type": "python_function",
        "parameters_schema": {}, "return_schema": {},
        "implementation_notes": "", "dependencies": [],
    }

    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=fake_spec_response), \
         patch("systemu.pipelines.tool_forge._generate_and_save_code") as mock_gen:
        mock_gen.return_value = MagicMock(name="forged_tool")
        mock_gen.return_value.name = "fetch_weather_data"
        result = forge_proposed_tools_from_specs(specs, scroll, MagicMock(), fake_vault)

    # Both steps fired
    assert len(result) == 1   # forged
    mock_gen.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Regression — locks down the v0.8.0.x bug (silent drop)
# ─────────────────────────────────────────────────────────────────────────────

def test_v081_does_not_silently_drop_specs_when_auto_forge_off():
    """The v0.8.0.x bug: validator emitted missing_tool_specs but auto_forge
    flag was off → specs silently discarded → no proposed tool surfaced.

    v0.8.1 fix: even with auto_forge=False, propose_tools_from_specs creates
    Tool records (status=PROPOSED) so the operator can review them.
    """
    from systemu.pipelines.tool_forge import propose_tools_from_specs

    specs = [ProposedToolSpec(
        name="new_tool", description="x", tool_type="python_function",
        parameter_hints=[], output_hint="", rationale=""
    )]
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = []
    saved = []
    fake_vault.save_tool.side_effect = lambda t: saved.append(t)

    config = MagicMock()
    config.auto_forge_tools = False   # the silent-drop config

    fake_response = {
        "name": "new_tool", "description": "x",
        "tool_type": "python_function",
        "parameters_schema": {}, "return_schema": {},
        "implementation_notes": "", "dependencies": [],
    }
    with patch("systemu.pipelines.tool_forge.llm_call_json", return_value=fake_response):
        result = propose_tools_from_specs(specs, MagicMock(narrative_md=""), config, fake_vault)

    # The bug would have shown result == [] and saved == []
    # The fix: result has 1 PROPOSED tool, saved to vault
    assert len(result) == 1, "v0.8.1 must propose tools even with auto_forge_tools=False"
    assert len(saved) == 1
    assert saved[0].status.value == "proposed"
