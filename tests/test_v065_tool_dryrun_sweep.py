"""v0.6.5-f — startup dry-run sweep auto-disables broken tools."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_sweep_skips_passed_tools():
    from systemu.scheduler.jobs import dry_run_all_pending_tools

    vault = MagicMock()
    vault.load_index.return_value = [
        {"id": "t1", "enabled": True, "dry_run_status": "passed"},
        {"id": "t2", "enabled": True, "dry_run_status": "failed"},
        {"id": "t3", "enabled": False, "dry_run_status": "not_run", "status": "deployed"},  # already-deployed; excluded by status filter
    ]

    with patch("systemu.pipelines.tool_dry_run.dry_run_tool") as mock_run:
        dry_run_all_pending_tools(vault, MagicMock(), max_concurrent=2)
        mock_run.assert_not_called()


def test_sweep_runs_pending_and_marks_failure_non_destructively():
    """v0.6.8-c contract: dry-run failures NO LONGER disable the tool.
    Instead the sweep sets dry_run_status='failed' and populates
    dry_run_evidence so the recovery panel can surface a fix path."""
    from systemu.scheduler.jobs import dry_run_all_pending_tools

    vault = MagicMock()
    # v0.6.8-c: force the load_index fallback path. MagicMock would otherwise
    # auto-return an empty iterable from find_tools_pending_dry_run().
    vault.find_tools_pending_dry_run = None
    vault.load_index.return_value = [
        {"id": "t_ok", "enabled": True, "dry_run_status": "not_run"},
        {"id": "t_bad", "enabled": True, "dry_run_status": None},
    ]

    def fake_get_tool(tid):
        tool = MagicMock(id=tid, name=tid, enabled=True)
        return tool

    vault.get_tool.side_effect = fake_get_tool

    def fake_dry_run(tool, *, vault=None, config=None):
        result = MagicMock()
        result.success = (tool.id == "t_ok")
        result.error = None if result.success else "ImportError: missing module xyz"
        result.evidence = {}
        result.status = "passed" if result.success else "failed"
        return result

    with patch("systemu.pipelines.tool_dry_run.dry_run_tool",
               side_effect=fake_dry_run):
        dry_run_all_pending_tools(vault, MagicMock(), max_concurrent=2)

    saves = [c.args[0] for c in vault.save_tool.call_args_list]
    bad = next((t for t in saves if t.id == "t_bad"), None)
    assert bad is not None, "t_bad should still be saved (status update)"
    # v0.6.8-c: tool stays enabled; failure is recorded as evidence
    assert bad.enabled is True
    assert bad.dry_run_status == "failed"


def test_sweep_noop_when_no_pending():
    from systemu.scheduler.jobs import dry_run_all_pending_tools
    vault = MagicMock()
    vault.load_index.return_value = []
    with patch("systemu.pipelines.tool_dry_run.dry_run_tool") as mock_run:
        dry_run_all_pending_tools(vault, MagicMock())
        mock_run.assert_not_called()


def test_sweep_handles_get_tool_exception_gracefully():
    """If vault.get_tool() raises, the sweep logs and continues with the next tool."""
    from systemu.scheduler.jobs import dry_run_all_pending_tools

    vault = MagicMock()
    vault.load_index.return_value = [
        {"id": "t_corrupt", "enabled": True, "dry_run_status": "not_run"},
        {"id": "t_ok", "enabled": True, "dry_run_status": "not_run"},
    ]

    def fake_get_tool(tid):
        if tid == "t_corrupt":
            raise KeyError("missing")
        return MagicMock(id=tid, name=tid, enabled=True)

    vault.get_tool.side_effect = fake_get_tool

    def fake_dry_run(tool, *, vault=None, config=None):
        return MagicMock(success=True, error=None, evidence={}, status="passed")

    with patch("systemu.pipelines.tool_dry_run.dry_run_tool",
               side_effect=fake_dry_run):
        # Should not raise
        dry_run_all_pending_tools(vault, MagicMock(), max_concurrent=1)


def test_sweep_does_not_disable_on_pending_dependency(monkeypatch):
    """v0.6.5-i hotfix kept the tool enabled when deps were pending.
    v0.6.8-c generalises: the tool stays enabled for ANY failure, and the
    sweep now records dry_run_status='failed' via save_tool (so the dashboard
    recovery panel can surface a fix path)."""
    from systemu.scheduler.jobs import dry_run_all_pending_tools

    vault = MagicMock()
    vault.find_tools_pending_dry_run = None
    vault.load_index.return_value = [
        {"id": "t_pending_dep", "enabled": True, "dry_run_status": "not_run"},
    ]

    fake_tool = MagicMock(id="t_pending_dep", name="needs_requests", enabled=True)
    vault.get_tool.return_value = fake_tool

    def fake_dry_run(tool, *, vault=None, config=None):
        return MagicMock(
            success=False,
            error="DepInstaller: treating all packages as pending: ['requests']",
            evidence={"pending_packages": ["requests"]},
            status="skipped",
        )

    with patch("systemu.pipelines.tool_dry_run.dry_run_tool",
               side_effect=fake_dry_run):
        dry_run_all_pending_tools(vault, MagicMock(), max_concurrent=1)

    # v0.6.8-c: tool stays enabled (the killer assertion)
    assert fake_tool.enabled is True
    # save_tool IS now called to persist the dry_run_evidence
    saves = [c.args[0] for c in vault.save_tool.call_args_list]
    assert any(getattr(t, "id", None) == "t_pending_dep" for t in saves)
