"""Bug #14: validator-blocked scrolls should trigger auto-forge via missing_tool_specs."""
from unittest.mock import MagicMock, patch
import pytest


def test_proposed_tool_spec_dataclass_exists():
    """The validator must expose a ProposedToolSpec dataclass (Bug #14 prep)."""
    from systemu.pipelines.scroll_validator import ProposedToolSpec
    spec = ProposedToolSpec(
        name="my_tool",
        description="does a thing",
        tool_type="cli_command",
        parameter_hints=["a", "b"],
        output_hint="dict",
        rationale="needed for X",
    )
    assert spec.name == "my_tool"
    assert spec.tool_type == "cli_command"
    assert spec.parameter_hints == ["a", "b"]


def test_validation_result_has_missing_tool_specs_field():
    """ValidationResult must carry missing_tool_specs (Bug #14 prep)."""
    from systemu.pipelines.scroll_validator import ValidationResult, ProposedToolSpec
    spec = ProposedToolSpec(name="x", description="y")
    vr = ValidationResult(
        satisfiable=False,
        confidence="high",
        summary="missing tools",
        missing_tool_specs=[spec],
    )
    assert len(vr.missing_tool_specs) == 1
    assert vr.missing_tool_specs[0].name == "x"


def test_parser_extracts_missing_tool_specs_from_llm_json():
    """The _parse_validator_output helper must extract missing_tool_specs (Bug #14 prep)."""
    from systemu.pipelines.scroll_validator import _parse_validator_output
    raw = {
        "satisfiable": False,
        "confidence": "high",
        "summary": "missing a thing",
        "blockers": [],
        "missing_tool_specs": [
            {
                "name": "gh_pr_fetch",
                "description": "Fetch a GitHub PR",
                "tool_type": "api_call",
                "parameter_hints": ["pr_url"],
                "output_hint": "dict",
                "rationale": "needed for security review",
            },
            {
                "name": "csv_diff",
                "description": "Diff two CSVs",
                "tool_type": "python_function",
            },
        ],
    }
    result = _parse_validator_output(raw)
    assert result.satisfiable is False
    assert len(result.missing_tool_specs) == 2
    assert result.missing_tool_specs[0].name == "gh_pr_fetch"
    assert result.missing_tool_specs[0].tool_type == "api_call"
    assert result.missing_tool_specs[1].name == "csv_diff"
    assert result.missing_tool_specs[1].tool_type == "python_function"


def test_parser_skips_missing_tool_specs_when_satisfiable():
    """missing_tool_specs should only be extracted on satisfiable=False."""
    from systemu.pipelines.scroll_validator import _parse_validator_output
    raw = {
        "satisfiable": True,
        "confidence": "high",
        "summary": "ok",
        "missing_tool_specs": [{"name": "should_not_appear"}],
    }
    result = _parse_validator_output(raw)
    assert result.satisfiable is True
    assert result.missing_tool_specs == []


def test_forge_helper_skips_existing_tool_names():
    """forge_proposed_tools_from_specs must skip names already in the vault."""
    from systemu.pipelines.tool_forge import forge_proposed_tools_from_specs
    from systemu.pipelines.scroll_validator import ProposedToolSpec

    spec = ProposedToolSpec(name="existing_tool", description="x")

    fake_scroll = MagicMock(name="scroll")
    fake_scroll.name = "Test"
    fake_scroll.narrative_md = ""

    fake_config = MagicMock(name="config")
    fake_vault = MagicMock(name="vault")
    fake_vault.load_index.return_value = [{"name": "existing_tool"}]

    forged = forge_proposed_tools_from_specs([spec], fake_scroll, fake_config, fake_vault)
    assert forged == []  # skipped — already exists


def test_forge_helper_returns_empty_for_empty_specs():
    """No specs → no forging, return []."""
    from systemu.pipelines.tool_forge import forge_proposed_tools_from_specs
    forged = forge_proposed_tools_from_specs(
        [], MagicMock(), MagicMock(), MagicMock(),
    )
    assert forged == []
