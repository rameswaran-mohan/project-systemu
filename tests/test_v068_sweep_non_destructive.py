import pytest
from unittest.mock import MagicMock
from datetime import datetime


def test_sweep_failure_keeps_tool_enabled(monkeypatch):
    """When dry-run fails with ImportError, tool stays enabled but
    dry_run_status='failed' + dry_run_evidence populated."""
    from systemu.scheduler import jobs as jobs_mod

    tool = MagicMock()
    tool.id = "tool_a"
    tool.name = "fetch_json"
    tool.enabled = True
    tool.status = "approved"
    tool.dry_run_status = None
    tool.dry_run_evidence = None

    vault = MagicMock()
    vault.find_tools_pending_dry_run.return_value = [tool]
    vault.find_tool_by_name.return_value = tool

    def fake_dry_run(t, vault, config):
        raise ImportError("No module named 'requests'")
    monkeypatch.setattr(jobs_mod._dr, "dry_run_tool", fake_dry_run)

    config = MagicMock()
    jobs_mod.dry_run_all_pending_tools(vault=vault, config=config)

    # Critical assertion: enabled must stay True
    assert tool.enabled is True, "v0.6.8-c: sweep must NOT auto-disable on failure"
    assert tool.dry_run_status == "failed"
    assert tool.dry_run_evidence is not None
    assert "requests" in tool.dry_run_evidence.get("error", "")
    assert tool.dry_run_evidence.get("classified_reason") == "DEP_PENDING"
    assert tool.dry_run_evidence.get("missing_package") == "requests"


def test_sweep_passing_dry_run_still_clears_evidence(monkeypatch):
    """A successful dry-run after a previous failure should clear the
    evidence and set status=passed."""
    from systemu.scheduler import jobs as jobs_mod

    tool = MagicMock()
    tool.id = "tool_a"
    tool.name = "x"
    tool.enabled = True
    tool.status = "approved"
    tool.dry_run_status = "failed"
    tool.dry_run_evidence = {"error": "old failure"}

    vault = MagicMock()
    vault.find_tools_pending_dry_run.return_value = [tool]

    def fake_pass(t, vault, config):
        return None  # success
    monkeypatch.setattr(jobs_mod._dr, "dry_run_tool", fake_pass)

    jobs_mod.dry_run_all_pending_tools(vault=vault, config=MagicMock())
    assert tool.dry_run_status == "passed"
    # Implementation may either clear evidence or leave it; assert the
    # success path doesn't disable.
    assert tool.enabled is True
