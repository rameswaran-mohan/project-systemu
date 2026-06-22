"""Tests for v0.5.0-e — resume_after_recalibration + fork auto-mapping.

Covers Supervisor.resume_after_recalibration:
  * Fork mode swaps original_tool_id → new_tool_id in shadow.available_tool_ids
  * Bump mode does NOT modify available_tool_ids (same tool id)
  * Missing activity returns a no-op submission id, doesn't crash
  * Re-queue uses elevated priority + retry_count=1
  * consult_affinity_log is False (we want the SAME shadow back)
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.core.models import Shadow, ShadowStatus, Tool, ToolStatus, ToolType
from systemu.runtime.supervisor import Supervisor


# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _supervisor_stub(vault):
    """Minimal Supervisor for resume tests — bypasses thread setup."""
    s = Supervisor.__new__(Supervisor)
    s.vault = vault
    # submit() reads these — provide minimum
    s._pending_lock      = threading.Lock()
    s._pending_activity_ids = set()
    s._running_lock      = threading.Lock()
    s._running           = {}
    s._task_queue        = None
    import queue
    s._queue             = queue.PriorityQueue()
    s._publish           = lambda *a, **kw: None
    return s


def _activity(activity_id="act-1", shadow_id="sh-1", scroll_id="scr-1"):
    return SimpleNamespace(
        id=activity_id,
        name="t",
        scroll_id=scroll_id,
        assigned_shadow_id=shadow_id,
        required_skill_ids=[],
    )


# ─────────────────────────────────────────────────────────────────────────────

class TestForkSwapsToolIds:
    def test_fork_mode_swaps_in_available_tool_ids(self, vault):
        # Set up: shadow with old tool in available_tool_ids
        sh = Shadow(
            id="sh-1", name="t", description="t",
            available_tool_ids=["tool_old"],
            status=ShadowStatus.AWAKENED,
        )
        vault.save_shadow(sh)
        # Pre-register an activity so the supervisor can find it
        from systemu.core.models import Activity
        a = Activity(
            id="act-1", name="t", scroll_id="scr-1",
            assigned_shadow_id="sh-1",
        )
        vault.save_activity(a)

        sup = _supervisor_stub(vault)
        sub_id = sup.resume_after_recalibration(
            execution_id="exec-1",
            original_tool_id="tool_old",
            new_tool_id="tool_new",
            mode="fork_new_tool",
            original_shadow_id="sh-1",
            scroll_id="scr-1",
        )
        assert sub_id.startswith("sub_")
        reloaded = vault.get_shadow("sh-1")
        assert "tool_new" in reloaded.available_tool_ids
        assert "tool_old" not in reloaded.available_tool_ids

    def test_bump_mode_does_not_modify_available_tool_ids(self, vault):
        sh = Shadow(
            id="sh-1", name="t", description="t",
            available_tool_ids=["tool_x"],
            status=ShadowStatus.AWAKENED,
        )
        vault.save_shadow(sh)
        from systemu.core.models import Activity
        a = Activity(
            id="act-1", name="t", scroll_id="scr-1",
            assigned_shadow_id="sh-1",
        )
        vault.save_activity(a)

        sup = _supervisor_stub(vault)
        sup.resume_after_recalibration(
            execution_id="exec-1",
            original_tool_id="tool_x",
            new_tool_id="tool_x",       # same id on bump
            mode="bump_version",
            original_shadow_id="sh-1",
            scroll_id="scr-1",
        )
        reloaded = vault.get_shadow("sh-1")
        assert reloaded.available_tool_ids == ["tool_x"]   # unchanged


# ─────────────────────────────────────────────────────────────────────────────

class TestActivityLookup:
    def test_missing_activity_returns_noop_id(self, vault):
        sh = Shadow(id="sh-1", name="t", description="t",
                     status=ShadowStatus.AWAKENED)
        vault.save_shadow(sh)
        # No activity saved

        sup = _supervisor_stub(vault)
        sub_id = sup.resume_after_recalibration(
            execution_id="exec-1",
            original_tool_id="tool_x",
            new_tool_id="tool_x",
            mode="bump_version",
            original_shadow_id="sh-1",
            scroll_id="scr-1",
        )
        assert sub_id.startswith("sub_no_activity_")


class TestSubmissionShape:
    def test_resume_uses_priority_2_and_skips_affinity(self, vault, monkeypatch):
        sh = Shadow(id="sh-1", name="t", description="t",
                     status=ShadowStatus.AWAKENED)
        vault.save_shadow(sh)
        from systemu.core.models import Activity
        a = Activity(id="act-1", name="t", scroll_id="scr-1",
                      assigned_shadow_id="sh-1")
        vault.save_activity(a)

        captured = {}
        def fake_submit(self, *args, **kwargs):
            captured.update(kwargs)
            return "sub_test123"
        monkeypatch.setattr(Supervisor, "submit", fake_submit)

        sup = _supervisor_stub(vault)
        sup.resume_after_recalibration(
            execution_id="exec-1",
            original_tool_id="tool_x",
            new_tool_id="tool_x",
            mode="bump_version",
            original_shadow_id="sh-1",
            scroll_id="scr-1",
        )
        assert captured.get("priority") == 2
        assert captured.get("retry_count") == 1
        assert captured.get("consult_affinity_log") is False
        # Reason should mention the mode
        assert "operator_approved_recalibration" in captured.get("reason", "")
        assert "bump_version" in captured.get("reason", "")
