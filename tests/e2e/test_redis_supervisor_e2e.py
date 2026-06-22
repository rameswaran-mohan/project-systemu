"""E2E: Supervisor with RedisPriorityQueue, in-process worker thread.

Highest-value test in this directory — proves RedisPriorityQueue actually
satisfies the contract Supervisor needs at runtime, not just at protocol-typing
level.  Uses fakeredis so no real Redis is required.
"""

from __future__ import annotations

import time
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from systemu.core.models import (
    Activity, Shadow, ShadowStatus, Scroll, Objective,
)


def _save_shadow_and_activity(vault, shadow_id="shadow_r", activity_id="act_r"):
    shadow = Shadow(
        id=shadow_id, name="R Shadow", description="t",
        system_prompt="t", status=ShadowStatus.AWAKENED,
    )
    vault.save_shadow(shadow)
    scroll = Scroll(
        id="scroll_r", name="t", source_session_id="s1",
        raw_instructions_path="", narrative_md="",
        objectives=[Objective(id=1, goal="g", success_criteria="c")],
    )
    vault.save_scroll(scroll)
    activity = Activity(
        id=activity_id, name="R Activity", scroll_id=scroll.id,
        required_tool_ids=[], required_skill_ids=[],
        assigned_shadow_id=shadow.id,
    )
    vault.save_activity(activity)
    return shadow, activity


def _wait_for_redis_state(queue, submission_id, target_state, timeout_s=8.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = queue._fetch_row(submission_id)
        if row.get("state") == target_state:
            return True
        time.sleep(0.05)
    return False


def test_supervisor_drives_redis_queue_to_completion(
    fake_redis, minimal_vault, real_config, reset_singletons,
):
    pytest.importorskip("redis")
    from systemu.queue.redis_priority_queue import RedisPriorityQueue
    from systemu.runtime.supervisor import Supervisor

    queue = RedisPriorityQueue("redis://localhost:6379/0", worker_id="e2e-redis-w1")

    shadow, activity = _save_shadow_and_activity(minimal_vault)

    fake_execute = AsyncMock(return_value={
        "status": "success",
        "execution_id": "exec_redis_e2e",
        "final_summary": "ok",
    })
    with patch("systemu.runtime.shadow_runtime.ShadowRuntime") as RuntimeCls:
        instance = MagicMock()
        instance.execute = fake_execute
        RuntimeCls.return_value = instance

        sup = Supervisor.init(real_config, minimal_vault, task_queue=queue)
        try:
            sub_id = sup.submit(activity.id, shadow.id, priority=2, reason="redis-e2e")
            assert _wait_for_redis_state(queue, sub_id, "completed"), (
                f"submission_id={sub_id} did not reach 'completed'. "
                f"Queue state: {queue.list_queued()}, running: {queue.list_running()}"
            )
        finally:
            sup.shutdown()

    assert fake_execute.await_count == 1


def test_redis_attempt_count_increments_correctly(fake_redis, reset_singletons):
    """Regression test for the attempt_count_raw vs attempt_count field bug.

    mark_running used to HINCRBY the wrong field, so list_queued() always
    reported attempt_count=0 even after the row had been claimed multiple times.
    """
    pytest.importorskip("redis")
    from systemu.queue.redis_priority_queue import RedisPriorityQueue

    q = RedisPriorityQueue("redis://localhost:6379/0", worker_id="w-attempt")
    sub_id = q.enqueue("act-1", "shadow-1", priority=5)

    q.mark_running(sub_id)
    running = q.list_running()
    assert len(running) == 1
    assert running[0]["attempt_count"] == 1

    # Simulate orphan recovery: requeue, then mark_running again
    q.requeue(sub_id, retry_count=1)
    q.mark_running(sub_id)
    running = q.list_running()
    assert running[0]["attempt_count"] == 2


def test_build_task_queue_strict_mode_raises_for_redis_failure(
    fake_redis, monkeypatch, reset_singletons,
):
    """In docker-enterprise mode, Redis import/connection failures must NOT
    silently degrade to in-memory — that's the whole point of the strict mode."""
    monkeypatch.setenv("SYSTEMU_MODE", "docker-enterprise")
    monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")
    monkeypatch.delenv("SYSTEMU_REDIS_URL", raising=False)

    from systemu.queue.protocol import build_task_queue
    with pytest.raises(RuntimeError, match="docker-enterprise"):
        build_task_queue(config=None)


def test_build_task_queue_lenient_mode_returns_none_for_redis_failure(
    monkeypatch, reset_singletons,
):
    """Outside docker-enterprise, missing Redis falls back to in-memory."""
    monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")
    monkeypatch.delenv("SYSTEMU_REDIS_URL", raising=False)
    monkeypatch.delenv("SYSTEMU_MODE", raising=False)

    from systemu.queue.protocol import build_task_queue
    assert build_task_queue(config=None) is None
