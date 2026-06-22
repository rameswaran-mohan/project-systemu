"""E2E: Supervisor with SQLite durable queue, in-process worker thread.

Proves the full chain works: enqueue → dispatcher claims → run_shadow_guarded
delegates to (mocked) ShadowRuntime.execute → mark_completed → DB row terminal.
"""

from __future__ import annotations

import time
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from systemu.core.models import (
    Activity, Shadow, ShadowStatus, Scroll, Objective,
)


def _save_shadow_and_activity(vault, shadow_id="shadow_1", activity_id="act_1"):
    shadow = Shadow(
        id=shadow_id, name="Test Shadow", description="t",
        system_prompt="t", status=ShadowStatus.AWAKENED,
    )
    vault.save_shadow(shadow)
    scroll = Scroll(
        id="scroll_1", name="t", source_session_id="s1",
        raw_instructions_path="", narrative_md="",
        objectives=[Objective(id=1, goal="g", success_criteria="c")],
    )
    vault.save_scroll(scroll)
    activity = Activity(
        id=activity_id, name="Test Activity", scroll_id=scroll.id,
        required_tool_ids=[], required_skill_ids=[],
        assigned_shadow_id=shadow.id,
    )
    vault.save_activity(activity)
    return shadow, activity


def _wait_for_state(queue, submission_id, target_state, timeout_s=8.0):
    """Poll the SQLite queue's DB row until it reaches target_state."""
    from sqlalchemy import text
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with queue._engine.begin() as conn:
            row = conn.execute(
                text("SELECT state FROM supervisor_queue WHERE submission_id=:s"),
                {"s": submission_id},
            ).fetchone()
        if row and row[0] == target_state:
            return True
        time.sleep(0.1)
    return False


def test_supervisor_drives_sqlite_queue_to_completion(
    sqlite_engine, minimal_vault, real_config, reset_singletons,
):
    from systemu.queue.sqlite_priority_queue import SqlitePriorityQueue
    from systemu.runtime.supervisor import Supervisor

    queue = SqlitePriorityQueue(sqlite_engine, worker_id="e2e-w1")

    shadow, activity = _save_shadow_and_activity(minimal_vault)

    # Patch ShadowRuntime.execute BEFORE supervisor starts so the lazy import
    # inside _run_shadow_guarded picks up the mock.
    fake_execute = AsyncMock(return_value={
        "status": "success",
        "execution_id": "exec_e2e_1",
        "final_summary": "ok",
    })
    with patch("systemu.runtime.shadow_runtime.ShadowRuntime") as RuntimeCls:
        instance = MagicMock()
        instance.execute = fake_execute
        RuntimeCls.return_value = instance

        sup = Supervisor.init(real_config, minimal_vault, task_queue=queue)
        try:
            sub_id = sup.submit(activity.id, shadow.id, priority=2, reason="e2e")

            assert _wait_for_state(queue, sub_id, "completed"), (
                f"submission_id={sub_id} did not reach 'completed' in time. "
                f"Last queue rows: {queue.list_queued()}"
            )
        finally:
            sup.shutdown()

    # ShadowRuntime.execute was actually invoked
    assert fake_execute.await_count == 1


def test_supervisor_uses_injected_task_queue_instance(
    sqlite_engine, minimal_vault, real_config, reset_singletons,
):
    """When task_queue is injected, Supervisor must keep that exact instance —
    not silently rebuild one from env vars."""
    from systemu.queue.sqlite_priority_queue import SqlitePriorityQueue
    from systemu.runtime.supervisor import Supervisor

    queue = SqlitePriorityQueue(sqlite_engine, worker_id="e2e-w2")

    sup = Supervisor.init(real_config, minimal_vault, task_queue=queue)
    try:
        assert sup._task_queue is queue
    finally:
        sup.shutdown()
