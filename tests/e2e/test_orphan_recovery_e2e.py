"""E2E: orphan recovery across both queue adapters.

The most fragile new code path — if anything silently breaks the requeue or
dead-letter behaviour for either adapter, this test screams.
"""

from __future__ import annotations

import time

import pytest


# ─── SQLite adapter ──────────────────────────────────────────────────────────

def test_sqlite_orphan_recovery_requeues_under_max_retries(sqlite_engine, reset_singletons):
    from systemu.queue.sqlite_priority_queue import SqlitePriorityQueue

    crashed = SqlitePriorityQueue(sqlite_engine, worker_id="w-crashed")
    sub_id = crashed.enqueue("act", "shadow", priority=5, retry_count=0)
    crashed.mark_running(sub_id)

    # Fast-forward the heartbeat by manipulating the row timestamp directly.
    # SqlitePriorityQueue uses time-based stale detection on last_heartbeat_at.
    from sqlalchemy import text
    long_ago = time.time() - 10_000   # well past any conceivable lease window
    with sqlite_engine.begin() as conn:
        conn.execute(
            text("UPDATE supervisor_queue SET last_heartbeat_at=:t, claimed_at=:t "
                 "WHERE submission_id=:s"),
            {"t": long_ago, "s": sub_id},
        )

    survivor = SqlitePriorityQueue(sqlite_engine, worker_id="w-survivor")
    recovered = survivor.recover_orphans()
    assert any(r["submission_id"] == sub_id for r in recovered)

    rows = survivor.list_queued()
    matching = [r for r in rows if r["submission_id"] == sub_id]
    assert len(matching) == 1
    assert matching[0]["retry_count"] == 1


def test_sqlite_orphan_dead_letters_after_max_retries(sqlite_engine, reset_singletons):
    from systemu.queue.sqlite_priority_queue import SqlitePriorityQueue
    from sqlalchemy import text

    crashed = SqlitePriorityQueue(sqlite_engine, worker_id="w-x", max_retries=2)
    # Already at max_retries=2, so recovery should dead-letter.
    sub_id = crashed.enqueue("act", "shadow", retry_count=2)
    crashed.mark_running(sub_id)

    long_ago = time.time() - 10_000
    with sqlite_engine.begin() as conn:
        conn.execute(
            text("UPDATE supervisor_queue SET last_heartbeat_at=:t, claimed_at=:t "
                 "WHERE submission_id=:s"),
            {"t": long_ago, "s": sub_id},
        )

    survivor = SqlitePriorityQueue(sqlite_engine, worker_id="w-survivor", max_retries=2)
    survivor.recover_orphans()

    # Row should be in dead_letter state, not requeued.
    with sqlite_engine.begin() as conn:
        state = conn.execute(
            text("SELECT state FROM supervisor_queue WHERE submission_id=:s"),
            {"s": sub_id},
        ).scalar()
    assert state == "dead_letter"
    assert sub_id not in [r["submission_id"] for r in survivor.list_queued()]


# ─── Redis adapter ────────────────────────────────────────────────────────────

def test_redis_orphan_recovery_requeues(fake_redis, reset_singletons):
    pytest.importorskip("redis")
    from systemu.queue.redis_priority_queue import RedisPriorityQueue

    crashed = RedisPriorityQueue("redis://localhost:6379/0", worker_id="w-crashed-r")
    sub_id = crashed.enqueue("act", "shadow", retry_count=0)
    crashed.mark_running(sub_id)

    # Simulate worker death: heartbeat key TTL'd out
    crashed._redis.delete(crashed._hb_key(sub_id))

    survivor = RedisPriorityQueue("redis://localhost:6379/0", worker_id="w-survivor-r")
    recovered = survivor.recover_orphans()
    assert len(recovered) == 1
    assert recovered[0]["action"] == "requeued"
    assert recovered[0]["new_retry"] == 1


def test_redis_orphan_dead_letters(fake_redis, reset_singletons):
    pytest.importorskip("redis")
    from systemu.queue.redis_priority_queue import RedisPriorityQueue

    crashed = RedisPriorityQueue("redis://localhost:6379/0", worker_id="w-c-r2", max_retries=2)
    sub_id = crashed.enqueue("act", "shadow", retry_count=2)
    crashed.mark_running(sub_id)
    crashed._redis.delete(crashed._hb_key(sub_id))

    survivor = RedisPriorityQueue("redis://localhost:6379/0", worker_id="w-s-r2", max_retries=2)
    recovered = survivor.recover_orphans()
    assert recovered[0]["action"] == "dead_lettered"
    assert survivor.list_queued() == []
    # Dead-letter list grew
    dl_key = f"{survivor._prefix}:deadletter"
    assert survivor._redis.llen(dl_key) == 1
