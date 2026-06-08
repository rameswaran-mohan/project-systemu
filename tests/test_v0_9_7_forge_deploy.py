"""v0.9.7 Phase 2.2 — synchronous deploy_forged_tool tests.

All four cases required by the spec:
1. FORGED tool + passing dry-run → DEPLOYED + enabled=True + callable-status True.
2. FORGED tool + failing dry-run → not deployed, reason given, never raises.
3. Already-deployed+enabled tool → no-op {"already": True}.
4. Unknown tool_id → {"deployed": False} (no raise).

No network; dry_run_tool is monkeypatched.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_dry_run_result(*, success: bool, status: str = None, error: str = None,
                          skip_reason: str = None, elapsed_ms: int = 5):
    """Build a minimal DryRunResult-shaped object for monkeypatching."""
    r = MagicMock()
    r.success = success
    r.status = status or ("passed" if success else "failed")
    r.error = error
    r.skip_reason = skip_reason
    r.elapsed_ms = elapsed_ms
    r.to_evidence.return_value = {
        "success": success,
        "status": r.status,
        "error": error,
        "skip_reason": skip_reason,
        "elapsed_ms": elapsed_ms,
    }
    return r


def _make_forged_tool(tool_id: str = "tool_abc") -> MagicMock:
    """Return a MagicMock that resembles a FORGED Tool model object."""
    from systemu.core.models import ToolStatus
    tool = MagicMock()
    tool.id = tool_id
    tool.name = "test_tool"
    tool.status = ToolStatus.FORGED
    tool.enabled = False
    tool.dry_run_status = "not_run"
    tool.dry_run_evidence = {}
    tool.implementation_path = "/tmp/test_tool.py"
    return tool


def _make_deployed_tool(tool_id: str = "tool_dep") -> MagicMock:
    """Return a MagicMock that resembles an already-deployed+enabled Tool."""
    from systemu.core.models import ToolStatus
    tool = MagicMock()
    tool.id = tool_id
    tool.name = "already_tool"
    tool.status = ToolStatus.DEPLOYED
    tool.enabled = True
    tool.dry_run_status = "passed"
    return tool


def _make_vault(tool=None, *, raise_key_error: bool = False) -> MagicMock:
    vault = MagicMock()
    if raise_key_error:
        vault.get_tool.side_effect = KeyError("not found")
    else:
        vault.get_tool.return_value = tool
    return vault


# ─────────────────────────────────────────────────────────────────────────────
# Test cases
# ─────────────────────────────────────────────────────────────────────────────

class TestDeployForgedTool:

    def test_forged_passing_dryrun_is_deployed_and_enabled(self, monkeypatch):
        """Case 1: FORGED tool + passing dry-run → DEPLOYED + enabled=True."""
        from systemu.core.models import ToolStatus
        import systemu.pipelines.tool_dry_run as _dr

        tool = _make_forged_tool("tool_01")
        vault = _make_vault(tool)
        config = MagicMock()

        pass_result = _make_dry_run_result(success=True, status="passed")
        monkeypatch.setattr(_dr, "dry_run_tool", lambda t, **kw: pass_result)

        from systemu.pipelines.tool_deploy import deploy_forged_tool
        outcome = deploy_forged_tool("tool_01", vault, config)

        # Return value
        assert outcome == {"deployed": True}, f"unexpected outcome: {outcome}"

        # Status and enabled promoted on the tool object
        assert tool.status == ToolStatus.DEPLOYED, (
            f"expected DEPLOYED, got {tool.status}"
        )
        assert tool.enabled is True, "expected enabled=True after successful deploy"

        # Vault was asked to persist
        vault.save_tool.assert_called()

    def test_callable_status_true_after_deploy(self, monkeypatch):
        """Case 1 (callable check): after deploy, status+enabled satisfy
        the runtime callability predicate (_RUNTIME_READY_STATUSES + enabled).
        """
        from systemu.core.models import ToolStatus
        from systemu.runtime.shadow_runtime import tool_is_runtime_ready
        import systemu.pipelines.tool_dry_run as _dr

        tool = _make_forged_tool("tool_01b")
        vault = _make_vault(tool)
        config = MagicMock()

        pass_result = _make_dry_run_result(success=True, status="passed")
        monkeypatch.setattr(_dr, "dry_run_tool", lambda t, **kw: pass_result)

        from systemu.pipelines.tool_deploy import deploy_forged_tool
        deploy_forged_tool("tool_01b", vault, config)

        # Both gates must be satisfied
        assert tool_is_runtime_ready(tool.status), (
            f"tool.status={tool.status} is not runtime-ready"
        )
        assert tool.enabled is True, "tool.enabled must be True for Gate-3 to pass"

    def test_forged_failing_dryrun_not_deployed_reason_given(self, monkeypatch):
        """Case 2: FORGED tool + failing dry-run → not deployed, reason present."""
        import systemu.pipelines.tool_dry_run as _dr
        from systemu.core.models import ToolStatus

        tool = _make_forged_tool("tool_02")
        original_status = tool.status
        vault = _make_vault(tool)
        config = MagicMock()

        fail_result = _make_dry_run_result(
            success=False, status="failed", error="import error: requests"
        )
        monkeypatch.setattr(_dr, "dry_run_tool", lambda t, **kw: fail_result)

        from systemu.pipelines.tool_deploy import deploy_forged_tool
        outcome = deploy_forged_tool("tool_02", vault, config)

        assert outcome["deployed"] is False, f"should not be deployed, got: {outcome}"
        assert "reason" in outcome, "expected 'reason' key in failure outcome"
        assert outcome["reason"], "reason should be non-empty"

        # Tool must NOT have been promoted
        assert tool.status == ToolStatus.FORGED, (
            f"tool should remain FORGED, got {tool.status}"
        )
        assert tool.enabled is False, "tool should remain disabled after failed dry-run"

    def test_failing_dryrun_never_raises(self, monkeypatch):
        """Case 2 (safety): even if dry_run_tool itself raises, never propagates."""
        import systemu.pipelines.tool_dry_run as _dr

        tool = _make_forged_tool("tool_02b")
        vault = _make_vault(tool)
        config = MagicMock()

        def _explode(t, **kw):
            raise RuntimeError("sandbox crashed")

        monkeypatch.setattr(_dr, "dry_run_tool", _explode)

        from systemu.pipelines.tool_deploy import deploy_forged_tool
        outcome = deploy_forged_tool("tool_02b", vault, config)

        assert outcome["deployed"] is False
        assert "reason" in outcome
        # No exception escaped

    def test_already_deployed_tool_is_noop(self, monkeypatch):
        """Case 3: already-deployed+enabled tool → {"deployed": True, "already": True}."""
        import systemu.pipelines.tool_dry_run as _dr

        tool = _make_deployed_tool("tool_03")
        vault = _make_vault(tool)
        config = MagicMock()

        # dry_run_tool must NOT be called
        dry_run_called = []
        monkeypatch.setattr(
            _dr, "dry_run_tool",
            lambda t, **kw: dry_run_called.append(t) or _make_dry_run_result(success=True),
        )

        from systemu.pipelines.tool_deploy import deploy_forged_tool
        outcome = deploy_forged_tool("tool_03", vault, config)

        assert outcome == {"deployed": True, "already": True}, (
            f"expected already-True outcome, got: {outcome}"
        )
        assert dry_run_called == [], (
            "dry_run_tool must not be called for already-deployed tools"
        )
        vault.save_tool.assert_not_called()

    def test_unknown_tool_id_returns_not_deployed_no_raise(self, monkeypatch):
        """Case 4: unknown tool_id → {"deployed": False} with no exception."""
        vault = _make_vault(raise_key_error=True)
        config = MagicMock()

        from systemu.pipelines.tool_deploy import deploy_forged_tool
        outcome = deploy_forged_tool("nonexistent_id", vault, config)

        assert outcome["deployed"] is False, f"expected deployed=False, got: {outcome}"
        # No exception escaped

    def test_already_deployed_but_disabled_is_not_noop(self, monkeypatch):
        """Edge case: DEPLOYED but enabled=False should NOT be treated as already-callable.
        The tool needs re-enabling.
        """
        from systemu.core.models import ToolStatus
        import systemu.pipelines.tool_dry_run as _dr

        tool = MagicMock()
        tool.id = "tool_04"
        tool.name = "disabled_deployed"
        tool.status = ToolStatus.DEPLOYED
        tool.enabled = False  # disabled despite DEPLOYED status
        tool.dry_run_status = "not_run"
        tool.dry_run_evidence = {}
        tool.implementation_path = "/tmp/disabled.py"

        vault = _make_vault(tool)
        config = MagicMock()

        pass_result = _make_dry_run_result(success=True, status="passed")
        monkeypatch.setattr(_dr, "dry_run_tool", lambda t, **kw: pass_result)

        from systemu.pipelines.tool_deploy import deploy_forged_tool
        outcome = deploy_forged_tool("tool_04", vault, config)

        # Should have promoted (not early-returned with "already")
        assert "already" not in outcome, (
            "should not return 'already' when enabled=False even if status=DEPLOYED"
        )
        assert outcome["deployed"] is True

    def test_skipped_dryrun_not_deployed_reason_given(self, monkeypatch):
        """Skipped dry-run (e.g. destructive tool) → not deployed, reason present."""
        import systemu.pipelines.tool_dry_run as _dr
        from systemu.core.models import ToolStatus

        tool = _make_forged_tool("tool_05")
        vault = _make_vault(tool)
        config = MagicMock()

        skip_result = _make_dry_run_result(
            success=False, status="skipped",
            skip_reason="destructive tool without dry_run support",
        )
        monkeypatch.setattr(_dr, "dry_run_tool", lambda t, **kw: skip_result)

        from systemu.pipelines.tool_deploy import deploy_forged_tool
        outcome = deploy_forged_tool("tool_05", vault, config)

        assert outcome["deployed"] is False
        assert "reason" in outcome
        assert tool.status == ToolStatus.FORGED
        assert tool.enabled is False
