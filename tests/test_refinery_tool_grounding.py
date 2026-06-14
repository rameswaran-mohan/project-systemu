"""Fix 3 — refinery failure-appraisal grounding.

Root cause X of the recorded-task RCA: the appraisal prompt was never given the
real tool registry and its own example hard-coded a non-existent tool
('web_extract_text'), so the 'PRIOR FAILURE' hints recommended tools that don't
exist. These tests pin: (1) the appraiser is grounded in the real registry, and
(2) any tool name in the feedback that isn't in the registry is flagged before
it's injected into the scroll.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_validate_feedback_flags_unknown_tool_names():
    from systemu.pipelines.refinery import _validate_feedback_tools
    out = _validate_feedback_tools(
        "If browser fails, use web_extract_text; web_extract works fine.",
        {"web_extract", "web_read"})
    assert "web_extract_text (not an available tool)" in out   # invented → flagged
    assert "web_extract works fine" in out                     # real tool → untouched


def test_available_tools_filters_to_enabled_deployed():
    from systemu.pipelines.refinery import _available_tools
    vault = MagicMock()
    vault.load_index.return_value = [
        {"name": "web_extract", "enabled": True, "status": "deployed", "description": "d"},
        {"name": "off_tool", "enabled": False, "status": "deployed"},
        {"name": "proposed_tool", "enabled": True, "status": "proposed"},
    ]
    names = {t["name"] for t in _available_tools(vault)}
    assert names == {"web_extract"}


def test_handle_scroll_refinement_sanitizes_injected_feedback():
    from systemu.pipelines.refinery import _handle_scroll_refinement
    obj = SimpleNamespace(id=1, hints={})
    scroll = SimpleNamespace(id="sc1", objectives=[obj], action_blocks=[], updated_at=None)
    vault = MagicMock()
    vault.load_index.return_value = [
        {"name": "web_extract", "enabled": True, "status": "deployed"}]
    _handle_scroll_refinement(
        {"failed_action_block_index": 1, "feedback": "use web_extract_text instead"},
        scroll, vault)
    fb = obj.hints["feedback"]
    assert "PRIOR FAILURE:" in fb
    assert "web_extract_text (not an available tool)" in fb
    vault.save_scroll.assert_called_once()
