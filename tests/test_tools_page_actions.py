"""Tests for the v0.7.4 Pattern 2 [Dry-Run] button on /tools."""
from unittest.mock import MagicMock, patch


def test_tools_page_renders_dryrun_button_for_forged_tools():
    """When a tool is FORGED with dry_run not run, the row must include a
    [Dry-Run] button in the actions column."""
    # We can't render NiceGUI without a running app loop. Instead, import
    # the helper that decides what action buttons to render for a row.
    from systemu.interface.pages.tools import _row_actions_for

    header = {"id": "tool_a", "status": "forged", "dry_run_status": "not_run", "enabled": False}
    actions = _row_actions_for(header)
    labels = [a["label"] for a in actions]
    assert "Dry-Run" in labels, f"expected Dry-Run action, got: {labels}"


def test_tools_page_no_dryrun_button_for_deployed_tools():
    from systemu.interface.pages.tools import _row_actions_for
    header = {"id": "tool_b", "status": "deployed", "dry_run_status": "passed"}
    actions = _row_actions_for(header)
    labels = [a["label"] for a in actions]
    assert "Dry-Run" not in labels, f"DEPLOYED tools should not show Dry-Run; got: {labels}"
