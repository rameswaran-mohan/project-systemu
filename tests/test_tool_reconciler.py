"""Tests for the tool lifecycle reconciler (v0.7.4 Pattern 2)."""
from unittest.mock import MagicMock
from pathlib import Path
import pytest


def test_find_pending_dry_run_returns_disabled_forged_tools(tmp_path):
    """v0.7.4: the sweep finder must return FORGED tools even when enabled=False,
    because the reconciler is responsible for advancing them to DEPLOYED.
    """
    from systemu.scheduler.jobs import _find_pending_dry_run_via_index

    # Synthesise an index with one enabled+pending and one disabled+pending
    index = [
        {"id": "tool_enabled", "enabled": True, "dry_run_status": "not_run", "status": "forged"},
        {"id": "tool_disabled", "enabled": False, "dry_run_status": None, "status": "forged"},
        {"id": "tool_done", "enabled": True, "dry_run_status": "passed", "status": "deployed"},
    ]
    result = _find_pending_dry_run_via_index(index)
    ids = sorted(h["id"] for h in result)
    assert ids == ["tool_disabled", "tool_enabled"], (
        "expected both FORGED tools (enabled and disabled), got: " + str(ids)
    )


def test_reconciler_advances_forged_tools_on_dry_run_pass(tmp_path, monkeypatch):
    """The reconciler must advance FORGED tools to DEPLOYED when dry-run passes."""
    from systemu.scheduler.tool_reconciler import reconcile_once
    from systemu.core.models import ToolStatus

    # Build mock vault returning one forged-pending tool header.
    fake_tool = MagicMock()
    fake_tool.id = "tool_x"
    fake_tool.name = "x_tool"
    fake_tool.status = ToolStatus.FORGED
    fake_tool.dry_run_status = "not_run"
    fake_tool.implementation_path = "/tmp/x.py"

    save_calls = []
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = [
        {"id": "tool_x", "status": "forged", "dry_run_status": "not_run", "enabled": False},
    ]
    fake_vault.get_tool.return_value = fake_tool
    fake_vault.save_tool.side_effect = lambda t: save_calls.append(t.status)

    fake_config = MagicMock()
    fake_config.vault_dir = str(tmp_path)
    fake_config.docker_tool_timeout = 30

    # Patch dry_run_tool to simulate a pass.
    class _DryRunResult:
        success = True
        status = "passed"
        params_used = {}
        elapsed_ms = 10
        error = None
    monkeypatch.setattr(
        "systemu.pipelines.tool_dry_run.dry_run_tool",
        lambda tool, **kw: _DryRunResult(),
    )

    reconcile_once(fake_vault, fake_config)

    # Tool.status should have been written as DEPLOYED at least once.
    assert any(s == ToolStatus.DEPLOYED for s in save_calls), (
        f"expected DEPLOYED in save_calls, got: {save_calls}"
    )


def test_reconciler_publishes_event_on_dry_run_fail(tmp_path, monkeypatch):
    """The reconciler must publish a quality-event when a tool fails dry-run."""
    from systemu.scheduler.tool_reconciler import reconcile_once
    from systemu.core.models import ToolStatus

    fake_tool = MagicMock()
    fake_tool.id = "tool_y"
    fake_tool.name = "y_tool"
    fake_tool.status = ToolStatus.FORGED
    fake_tool.dry_run_status = "not_run"
    fake_tool.implementation_path = "/tmp/y.py"

    fake_vault = MagicMock()
    fake_vault.load_index.return_value = [
        {"id": "tool_y", "status": "forged", "dry_run_status": "not_run", "enabled": True},
    ]
    fake_vault.get_tool.return_value = fake_tool

    fake_config = MagicMock()
    fake_config.vault_dir = str(tmp_path)
    fake_config.docker_tool_timeout = 30

    class _DryRunResult:
        success = False
        status = "failed"
        params_used = {}
        elapsed_ms = 10
        error = "import error"
    monkeypatch.setattr(
        "systemu.pipelines.tool_dry_run.dry_run_tool",
        lambda tool, **kw: _DryRunResult(),
    )

    published = []
    monkeypatch.setattr(
        "systemu.interface.notifications.log_event",
        lambda level, category, message, context=None: published.append((level, category, message)),
    )

    reconcile_once(fake_vault, fake_config)

    assert any(level == "WARNING" and category == "tool" for level, category, _ in published), (
        f"expected WARNING tool event published, got: {published}"
    )
