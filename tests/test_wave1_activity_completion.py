"""Wave 1.4 — completed work must read as completed.

The sync path (run_direct_task, route_through_supervisor=False) never flipped
the activity's vault status after a successful execute — it stayed
``assigned`` forever while the chat entry said success.  Both the Supervisor
and the sync path now share ``mark_activity_completed``.
"""
from datetime import datetime, timezone

import pytest

from systemu.core.models import Activity, ActivityStatus
from systemu.runtime.activity_completion import mark_activity_completed
from systemu.storage.file_vault import FileVault
from systemu.vault.vault import Vault


@pytest.fixture()
def vault(tmp_path):
    return FileVault(Vault(str(tmp_path / "vault")))


def _activity(vault) -> Activity:
    act = Activity(
        id="act_1", name="Test activity", scroll_id="scr_1",
        status=ActivityStatus.ASSIGNED, assigned_shadow_id="sh_1",
    )
    vault.save_activity(act)
    return act


class TestMarkActivityCompleted:
    def test_flips_assigned_to_completed(self, vault):
        _activity(vault)
        assert mark_activity_completed(vault, "act_1") is True
        assert vault.get_activity("act_1").status == ActivityStatus.COMPLETED

    def test_stamps_updated_at(self, vault):
        _activity(vault)
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        mark_activity_completed(vault, "act_1")
        after = vault.get_activity("act_1").updated_at
        assert after is not None and after >= before.replace(microsecond=0)

    def test_missing_activity_is_nonfatal(self, vault):
        assert mark_activity_completed(vault, "act_nope") is False


class TestCallSitesShareTheHelper:
    def test_supervisor_uses_helper(self):
        import inspect
        from systemu.runtime import supervisor
        src = inspect.getsource(supervisor.Supervisor._handle_result)
        assert "mark_activity_completed" in src

    def test_direct_task_uses_helper(self):
        import inspect
        from systemu.pipelines import direct_task
        src = inspect.getsource(direct_task)
        assert "mark_activity_completed" in src
