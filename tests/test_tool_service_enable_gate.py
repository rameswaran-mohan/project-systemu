"""Task 3.1 — tool_service.enable_tool is the authoritative mechanism-level enable
gate. It refuses ONLY a tool whose dry-run definitively FAILED (the Defect-C bug:
a known-broken tool reaching DEPLOYED). passed / skipped / not_run stay enable-able
at this floor — stricter "must have PASSED" validation is enforced by the specific
paths that require it (e.g. the readiness-gate verb). This closes ALL enable paths
(recalibration, tools_blocked, CLI, deferred-enable) against a `failed` tool.
"""
import pytest

from systemu.pipelines import tool_service
from systemu.pipelines.tool_service import ENABLE_BLOCKED_DRY_RUN_STATUSES, can_enable, enable_tool
from systemu.core.models import ToolStatus


class _Tool:
    def __init__(self, dry_run_status="not_run", enabled=False, status=ToolStatus.FORGED):
        self.id = "tool_t"
        self.name = "t_tool"
        self.dry_run_status = dry_run_status
        self.enabled = enabled
        self.status = status


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


def test_constant_blocks_only_failed():
    assert ENABLE_BLOCKED_DRY_RUN_STATUSES == frozenset({"failed"})


@pytest.mark.parametrize(
    "status,expected",
    [("passed", True), ("skipped", True), ("failed", False), ("not_run", True), (None, True)],
)
def test_can_enable_truth_table(status, expected):
    assert can_enable(_Tool(dry_run_status=status)) is expected


def test_enable_refused_for_failed_dry_run():
    tool = _Tool(dry_run_status="failed")
    vault = _Vault(tool)
    assert enable_tool("tool_t", vault) is False
    assert tool.enabled is False
    assert tool.status == ToolStatus.FORGED
    assert vault.saved == []  # never persisted


def test_enable_allowed_for_not_run_dry_run():
    # The reviewed-approve / dep-install flows enable an operator-vetted tool that
    # has not been dry-run yet — only a *failed* dry-run blocks enable at the floor.
    tool = _Tool(dry_run_status="not_run")
    vault = _Vault(tool)
    assert enable_tool("tool_t", vault) is True
    assert tool.enabled is True
    assert tool.status == ToolStatus.DEPLOYED


def test_enable_allowed_for_passed_dry_run():
    tool = _Tool(dry_run_status="passed")
    vault = _Vault(tool)
    assert enable_tool("tool_t", vault) is True
    assert tool.enabled is True
    assert tool.status == ToolStatus.DEPLOYED


def test_enable_allowed_for_skipped_dry_run():
    tool = _Tool(dry_run_status="skipped")
    vault = _Vault(tool)
    assert enable_tool("tool_t", vault) is True
    assert tool.enabled is True
    assert tool.status == ToolStatus.DEPLOYED
