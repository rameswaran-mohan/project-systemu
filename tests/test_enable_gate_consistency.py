"""Task 3.5 — enable-gate consistency / defense-in-depth.

(a) BOTH the policy verb (verbs.tools_enable) AND the mechanism
    (tool_service.enable_tool) independently refuse a `failed`-dry-run tool.
(b) The deferred-enable reconciler completes for an operator_verify SKIP
    (dry_run_status="skipped" + dry_run_evidence["operator_verify"]==True),
    not only for "passed" — so the Phase 1 operator-verify path can deploy.
"""
import pytest

from systemu.core.models import ToolStatus
from systemu.interface.command import verbs
from systemu.interface.command.result import CommandStatus
from systemu.pipelines import tool_service


class _Tool:
    def __init__(self, dry_run_status, enabled=False, status=ToolStatus.FORGED, evidence=None):
        self.id = "tool_t"
        self.name = "t_tool"
        self.dry_run_status = dry_run_status
        self.enabled = enabled
        self.status = status
        self.dry_run_evidence = evidence or {}


class _Vault:
    def __init__(self, tool):
        self._tool = tool
        self.saved = []

    def get_tool(self, tool_id):
        return self._tool

    def save_tool(self, tool):
        self.saved.append((tool.enabled, tool.status))


@pytest.fixture(autouse=True)
def _silence_log_event(monkeypatch):
    monkeypatch.setattr(
        "systemu.interface.notifications.log_event",
        lambda *a, **k: None,
    )


def test_verb_and_mechanism_both_refuse_failed():
    # Mechanism refuses.
    tool_m = _Tool("failed")
    assert tool_service.enable_tool("tool_t", _Vault(tool_m)) is False
    assert tool_m.enabled is False

    # Policy verb refuses.
    tool_v = _Tool("failed")
    res = verbs.tools_enable("tool_t", vault=_Vault(tool_v))
    assert res.status == CommandStatus.ERROR
    assert tool_v.enabled is False


def test_is_operator_verify_skip_predicate():
    from systemu.scheduler.tool_reconciler import _is_operator_verify_skip

    assert _is_operator_verify_skip(_Tool("skipped", evidence={"operator_verify": True})) is True
    # safety-skip (no operator_verify flag) is NOT an operator-verify skip
    assert _is_operator_verify_skip(_Tool("skipped", evidence={})) is False
    assert _is_operator_verify_skip(_Tool("failed", evidence={"operator_verify": True})) is False
    assert _is_operator_verify_skip(_Tool("passed")) is False
