"""Task 3.3 — the auto-approve (low-risk) recalibration path must NOT launder a
failed dry-run to "skipped" and must route enable through the gated
tool_service.enable_tool. On a refused enable it logs and returns WITHOUT
resuming the Supervisor.
"""
import pytest

from systemu.runtime import shadow_runtime
from systemu.core.models import ToolStatus


class _Tool:
    def __init__(self, dry_run_status, enabled=False, status=ToolStatus.FORGED):
        self.id = "tool_new"
        self.name = "new_tool"
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
        self.saved.append((tool.dry_run_status, tool.enabled))


class _Result:
    def __init__(self):
        self.new_tool_id = "tool_new"
        self.original_tool_id = "tool_old"
        self.mode = "bump_version"


class _Shadow:
    id = "sh_1"


@pytest.fixture
def patched(monkeypatch):
    resumed = []

    class _Sup:
        @staticmethod
        def get():
            return _Sup()

        def resume_after_recalibration(self, **kw):
            resumed.append(kw)
            return "sub_123"

    monkeypatch.setattr(
        "systemu.runtime.supervisor.Supervisor", _Sup, raising=False
    )
    return resumed


def test_failed_dry_run_not_enabled_not_laundered_not_resumed(patched):
    resumed = patched
    tool = _Tool(dry_run_status="failed")
    vault = _Vault(tool)

    shadow_runtime._auto_approve_recalibration(
        result=_Result(), vault=vault, shadow=_Shadow(), scroll=None, execution_id="exec_1",
    )

    assert tool.enabled is False
    assert tool.dry_run_status == "failed"
    assert tool.status == ToolStatus.FORGED
    assert resumed == []


def test_passed_dry_run_enables_and_resumes(patched):
    resumed = patched
    tool = _Tool(dry_run_status="passed")
    vault = _Vault(tool)

    shadow_runtime._auto_approve_recalibration(
        result=_Result(), vault=vault, shadow=_Shadow(), scroll=None, execution_id="exec_1",
    )

    assert tool.enabled is True
    assert tool.status == ToolStatus.DEPLOYED
    assert len(resumed) == 1
    assert resumed[0]["new_tool_id"] == "tool_new"
