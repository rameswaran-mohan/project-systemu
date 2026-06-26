"""Task 3.4 — disable_if_dry_run_failed auto-disables a DEPLOYED+enabled tool
whose fresh dry-run recorded "failed", reverting it to FORGED/enabled=False.
No-op when passed/skipped or already disabled.
"""
import pytest

from systemu.pipelines.tool_service import disable_if_dry_run_failed
from systemu.core.models import ToolStatus


class _Tool:
    def __init__(self, dry_run_status, enabled, status):
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
        if self._tool is None:
            raise KeyError(tool_id)
        return self._tool

    def save_tool(self, tool):
        self.saved.append((tool.enabled, tool.status))


@pytest.fixture(autouse=True)
def _silence_log_event(monkeypatch):
    monkeypatch.setattr(
        "systemu.interface.notifications.log_event",
        lambda *a, **k: None,
    )


def test_disables_failed_enabled_deployed_tool():
    tool = _Tool("failed", enabled=True, status=ToolStatus.DEPLOYED)
    vault = _Vault(tool)
    assert disable_if_dry_run_failed("tool_t", vault) is True
    assert tool.enabled is False
    assert tool.status == ToolStatus.FORGED


def test_noop_when_passed():
    tool = _Tool("passed", enabled=True, status=ToolStatus.DEPLOYED)
    vault = _Vault(tool)
    assert disable_if_dry_run_failed("tool_t", vault) is False
    assert tool.enabled is True
    assert tool.status == ToolStatus.DEPLOYED


def test_noop_when_already_disabled():
    tool = _Tool("failed", enabled=False, status=ToolStatus.FORGED)
    vault = _Vault(tool)
    assert disable_if_dry_run_failed("tool_t", vault) is False
    assert tool.enabled is False


def test_noop_when_tool_missing():
    vault = _Vault(None)
    assert disable_if_dry_run_failed("tool_t", vault) is False
