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


def _deferred_enable_setup(dry_run_status, evidence):
    """Build a fake vault with one resolved 'Enable & run' tools_blocked
    decision pointing at a single disabled tool with the given dry-run state.
    """
    from systemu.core.models import ToolStatus

    tool = MagicMock()
    tool.id = "tool_z"
    tool.name = "z_tool"
    tool.enabled = False
    tool.status = ToolStatus.FORGED
    tool.dry_run_status = dry_run_status
    tool.dry_run_evidence = evidence

    decision = MagicMock()
    decision.choice = "Enable & run"
    decision.context = {"tool_ids": ["tool_z"]}

    fake_vault = MagicMock()
    fake_vault.load_index.return_value = [
        {"id": "dec_1", "status": "resolved", "dedup_key": "tools_blocked:abc"},
    ]
    fake_vault.get_decision.return_value = decision
    fake_vault.get_tool.return_value = tool
    return fake_vault, tool


def test_deferred_enable_completes_for_operator_verify_skip(monkeypatch):
    """Task 3.5(b): the deferred-enable reconciler must complete an
    operator_verify SKIP, not only a 'passed' dry-run."""
    from systemu.scheduler.tool_reconciler import _complete_deferred_enables

    fake_vault, tool = _deferred_enable_setup(
        "skipped", {"operator_verify": True},
    )
    enabled_calls = []
    monkeypatch.setattr(
        "systemu.pipelines.tool_service.enable_tool",
        lambda tid, vault: enabled_calls.append(tid) or True,
    )
    monkeypatch.setattr(
        "systemu.pipelines.tool_service.heal_activities_for_tool",
        lambda *a, **k: None,
    )

    _complete_deferred_enables(fake_vault, MagicMock())

    assert enabled_calls == ["tool_z"], (
        f"operator_verify skip should reach enable_tool, got: {enabled_calls}"
    )


def test_deferred_enable_skips_safety_skip(monkeypatch):
    """A plain safety-skip (no operator_verify flag) is NOT completed by the
    deferred-enable path — it still requires 'passed'."""
    from systemu.scheduler.tool_reconciler import _complete_deferred_enables

    fake_vault, tool = _deferred_enable_setup("skipped", {})
    enabled_calls = []
    monkeypatch.setattr(
        "systemu.pipelines.tool_service.enable_tool",
        lambda tid, vault: enabled_calls.append(tid) or True,
    )

    _complete_deferred_enables(fake_vault, MagicMock())

    assert enabled_calls == [], (
        f"a non-operator-verify skip must not be auto-completed, got: {enabled_calls}"
    )


# ── v0.9.49 F4: reaper finalizes a task parked on a declined/missing tool ─────
# (a `proposed`/`not_run` tool whose forge is still PENDING — forge_rejected
# False — must NOT be reaped: the operator may still approve it.)

import pytest as _pytest


@_pytest.fixture
def real_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _f4_tool(v, tid, **kw):
    from systemu.core.models import Tool, ToolStatus, ToolType
    base = dict(id=tid, name=tid, description="d", tool_type=ToolType.PYTHON_FUNCTION,
                status=ToolStatus.PROPOSED, implementation_path=f"p/{tid}.py",
                parameters_schema={}, dry_run_status="not_run")
    base.update(kw)
    v.save_tool(Tool(**base))


def _f4_act(v, aid, req):
    from systemu.core.models import Activity, ActivityStatus
    v.save_activity(Activity(id=aid, name="t", scroll_id="s",
                             required_tool_ids=list(req), status=ActivityStatus.PARTIAL))


def test_reaper_finalizes_declined_tool(real_vault):
    from systemu.core.models import ActivityStatus
    from systemu.scheduler.tool_reconciler import _fail_unsatisfiable_blocked_activities
    _f4_tool(real_vault, "arc", forge_rejected=True)
    _f4_act(real_vault, "a", ["arc"])
    _fail_unsatisfiable_blocked_activities(real_vault, None)
    assert real_vault.get_activity("a").status == ActivityStatus.FAILED


def test_reaper_skips_pending_proposed_tool(real_vault):
    from systemu.core.models import ActivityStatus
    from systemu.scheduler.tool_reconciler import _fail_unsatisfiable_blocked_activities
    _f4_tool(real_vault, "pend")   # proposed, forge_rejected False (gate still pending)
    _f4_act(real_vault, "a", ["pend"])
    _fail_unsatisfiable_blocked_activities(real_vault, None)
    assert real_vault.get_activity("a").status == ActivityStatus.PARTIAL


def test_reaper_finalizes_failed_dryrun_tool_regression(real_vault):
    from systemu.core.models import ActivityStatus, ToolStatus
    from systemu.scheduler.tool_reconciler import _fail_unsatisfiable_blocked_activities
    _f4_tool(real_vault, "bad", status=ToolStatus.FORGED, dry_run_status="failed")
    _f4_act(real_vault, "a", ["bad"])
    _fail_unsatisfiable_blocked_activities(real_vault, None)
    assert real_vault.get_activity("a").status == ActivityStatus.FAILED


def test_reaper_idempotent_across_ticks(real_vault):
    from systemu.core.models import ActivityStatus
    from systemu.scheduler.tool_reconciler import _fail_unsatisfiable_blocked_activities
    _f4_tool(real_vault, "arc", forge_rejected=True)
    _f4_act(real_vault, "a", ["arc"])
    _fail_unsatisfiable_blocked_activities(real_vault, None)
    _fail_unsatisfiable_blocked_activities(real_vault, None)   # second tick: no crash
    assert real_vault.get_activity("a").status == ActivityStatus.FAILED
