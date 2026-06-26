"""Task 3.2 — the Tools-page "Enable & Resume" recalibration handler must NOT
launder a failed dry-run to "skipped" and must route the enable through the
gated tool_service.enable_tool mechanism. On a refused enable it notifies and
returns WITHOUT resuming the Supervisor.
"""
import pytest

from systemu.interface.pages import tools as tools_page
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


class _FakeUi:
    def __init__(self):
        self.notifications = []

    def notify(self, msg, **kw):
        self.notifications.append((msg, kw))


@pytest.fixture
def patched(monkeypatch):
    fake_ui = _FakeUi()
    monkeypatch.setattr(tools_page, "ui", fake_ui)

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
    # EventBus publish is best-effort; stub it so the test doesn't touch a bus.
    monkeypatch.setattr(
        "systemu.interface.event_bus.EventBus",
        type("EB", (), {"get": staticmethod(lambda: type("B", (), {"publish": lambda self, e: None})())}),
        raising=False,
    )
    return fake_ui, resumed


def test_failed_dry_run_not_enabled_not_laundered_not_resumed(patched):
    fake_ui, resumed = patched
    tool = _Tool(dry_run_status="failed")
    vault = _Vault(tool)
    rec = {"new_tool_id": "tool_new", "original_tool_id": "tool_old", "mode": "bump_version"}
    ctx = {"execution_id": "exec_1", "shadow_id": "sh_1"}

    tools_page._on_enable_and_resume(rec, ctx, vault)

    # Not enabled, status untouched, never laundered to skipped.
    assert tool.enabled is False
    assert tool.dry_run_status == "failed"
    assert tool.status == ToolStatus.FORGED
    # Supervisor resume must NOT have been called.
    assert resumed == []
    # Operator was told.
    assert fake_ui.notifications, "expected a notify() on refused enable"


def test_passed_dry_run_enables_and_resumes(patched):
    fake_ui, resumed = patched
    tool = _Tool(dry_run_status="passed")
    vault = _Vault(tool)
    rec = {"new_tool_id": "tool_new", "original_tool_id": "tool_old", "mode": "bump_version"}
    ctx = {"execution_id": "exec_1", "shadow_id": "sh_1"}

    tools_page._on_enable_and_resume(rec, ctx, vault)

    assert tool.enabled is True
    assert tool.status == ToolStatus.DEPLOYED
    assert len(resumed) == 1
    assert resumed[0]["new_tool_id"] == "tool_new"
