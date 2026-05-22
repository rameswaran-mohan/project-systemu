"""approve_and_install + is_allowlisted tests for the dashboard
runtime approval workflow.

These tests do NOT exercise the existing ``DepApprovalStore`` JSON-file
allow-list (PROMPT mode).  They cover the new database-backed runtime
workflow added in v0.6.8-e for the dashboard recovery panel.
"""

from unittest.mock import MagicMock


def test_approve_and_install_persists_and_runs_pip(monkeypatch):
    from systemu.runtime import dep_approvals

    saved = []
    monkeypatch.setattr(dep_approvals, "_persist_approval",
                        lambda approval: saved.append(approval))

    pip_calls = []

    def fake_pip(pkg):
        pip_calls.append(pkg)
        return 0
    monkeypatch.setattr(dep_approvals, "_run_pip_install", fake_pip)

    dry_run_calls = []
    monkeypatch.setattr(dep_approvals, "_rerun_dry_run",
                        lambda tid: dry_run_calls.append(tid))

    dep_approvals.approve_and_install(tool_id="tool_a", package="requests",
                                      source="dashboard")
    assert pip_calls == ["requests"]
    assert dry_run_calls == ["tool_a"]
    assert len(saved) == 1
    assert saved[0].package_name == "requests"
    assert saved[0].source == "dashboard"
    assert saved[0].baked_in_image is False


def test_approve_and_install_raises_on_pip_failure(monkeypatch):
    import pytest
    from systemu.runtime import dep_approvals

    monkeypatch.setattr(dep_approvals, "_persist_approval", lambda a: None)
    monkeypatch.setattr(dep_approvals, "_run_pip_install", lambda p: 1)
    monkeypatch.setattr(dep_approvals, "_rerun_dry_run", lambda t: None)
    with pytest.raises(RuntimeError, match="pip install"):
        dep_approvals.approve_and_install(tool_id="t", package="evil", source="x")


def test_is_allowlisted(monkeypatch):
    from systemu.runtime import dep_approvals
    monkeypatch.setattr(dep_approvals, "_load_allowlist",
                        lambda: {"requests", "lxml"})
    assert dep_approvals.is_allowlisted("requests") is True
    assert dep_approvals.is_allowlisted("evil-pkg") is False
