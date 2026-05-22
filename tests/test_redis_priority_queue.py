"""Tests for systemu/queue/redis_priority_queue.py.

Uses ``fakeredis`` so the test suite doesn't require a running Redis.  The
test module is skipped when fakeredis is unavailable; CI for docker-enterprise
mode should add ``fakeredis`` to test extras.
"""

from __future__ import annotations

import time

import pytest

fakeredis = pytest.importorskip("fakeredis")

from systemu.queue import redis_priority_queue as rpq_mod
from systemu.queue.redis_priority_queue import RedisPriorityQueue


@pytest.fixture
def fake_redis(monkeypatch):
    """Replace redis.Redis.from_url with a fakeredis instance."""
    server = fakeredis.FakeServer()

    class _FakeRedis(fakeredis.FakeStrictRedis):
        @classmethod
        def from_url(cls, url, decode_responses=False, **kw):
            return cls(server=server, decode_responses=decode_responses)

    import redis as real_redis
    monkeypatch.setattr(real_redis, "Redis", _FakeRedis)
    return server


def _make_queue(worker_id: str = "w-1") -> RedisPriorityQueue:
    return RedisPriorityQueue("redis://localhost:6379/0", worker_id=worker_id)


# ── Basic ZADD / dequeue ordering ───────────────────────────────────────────

def test_enqueue_persists_row_and_pushes_to_priority_set(fake_redis) -> None:
    q = _make_queue()
    sid = q.enqueue("act-1", "shadow-1", priority=3, reason="manual", retry_count=0)
    assert sid.startswith("sub_")
    listed = q.list_queued()
    assert len(listed) == 1
    assert listed[0]["activity_id"] == "act-1"
    assert listed[0]["priority"] == 3


def test_priority_ordering_in_list_queued(fake_redis) -> None:
    q = _make_queue()
    sid_low = q.enqueue("act-low", "s", priority=10)
    time.sleep(0.001)
    sid_high = q.enqueue("act-high", "s", priority=1)
    rows = q.list_queued()
    assert [r["activity_id"] for r in rows] == ["act-high", "act-low"]


# ── State transitions ───────────────────────────────────────────────────────

def test_mark_running_removes_from_queue_and_sets_heartbeat(fake_redis) -> None:
    q = _make_queue("w-A")
    sid = q.enqueue("act", "s", priority=5)
    q.mark_running(sid)

    queued = q.list_queued()
    assert queued == []   # gone from the queue
    running = q.list_running()
    assert len(running) == 1
    assert running[0]["claimed_by"] == "w-A"


def test_mark_completed_clears_running_and_heartbeat(fake_redis) -> None:
    q = _make_queue("w-A")
    sid = q.enqueue("act", "s")
    q.mark_running(sid)
    q.mark_completed(sid, {"status": "success"})
    assert q.list_running() == []


def test_mark_dead_letter_pushes_to_deadletter_list(fake_redis) -> None:
    q = _make_queue("w-A")
    sid = q.enqueue("act", "s")
    q.mark_running(sid)
    q.mark_dead_letter(sid, "out of retries")
    # Dead-letter list grew by one
    dl_key = f"systemu:deadletter"
    assert q._redis.llen(dl_key) == 1


def test_requeue_returns_row_to_queue_with_higher_retry(fake_redis) -> None:
    q = _make_queue("w-A")
    sid = q.enqueue("act", "s", priority=4)
    q.mark_running(sid)
    q.requeue(sid, retry_count=2)
    rows = q.list_queued()
    assert len(rows) == 1
    assert rows[0]["retry_count"] == 2
    assert q.list_running() == []


# ── Crash recovery ──────────────────────────────────────────────────────────

def test_recover_orphans_requeues_when_heartbeat_expired(fake_redis) -> None:
    """A row whose worker died (heartbeat key gone) should be requeued."""
    crashed = _make_queue("w-crashed")
    sid = crashed.enqueue("act", "s")
    crashed.mark_running(sid)

    # Simulate the crashed worker's heartbeat key expiring.
    crashed._redis.delete(crashed._hb_key(sid))

    # A different worker performs recovery.
    survivor = _make_queue("w-survivor")
    recovered = survivor.recover_orphans()
    assert len(recovered) == 1
    assert recovered[0]["action"] == "requeued"
    # Row is back in the queue, retry incremented to 1
    rows = survivor.list_queued()
    assert len(rows) == 1
    assert rows[0]["retry_count"] == 1


def test_recover_orphans_dead_letters_after_max_retries(fake_redis) -> None:
    crashed = _make_queue("w-crashed")
    sid = crashed.enqueue("act", "s", retry_count=2)   # already at max_retries
    crashed.mark_running(sid)
    crashed._redis.delete(crashed._hb_key(sid))

    survivor = _make_queue("w-survivor")
    recovered = survivor.recover_orphans()
    assert len(recovered) == 1
    assert recovered[0]["action"] == "dead_lettered"
    assert survivor.list_queued() == []


def test_recover_orphans_skips_own_running_rows(fake_redis) -> None:
    q = _make_queue("w-self")
    sid = q.enqueue("act", "s")
    q.mark_running(sid)
    q._redis.delete(q._hb_key(sid))   # even if heartbeat expired

    recovered = q.recover_orphans()
    assert recovered == []   # the watchdog handles our own rows, not recover_orphans


# ── import_from_json ────────────────────────────────────────────────────────

def test_import_from_json_creates_queued_rows(fake_redis) -> None:
    q = _make_queue()
    items = [
        {"payload": {"submission_id": "sub_1", "activity_id": "a", "shadow_id": "s",
                     "priority": 5, "retry_count": 0, "reason": "restart-restore"}},
        {"payload": {"submission_id": "sub_2", "activity_id": "b", "shadow_id": "s",
                     "priority": 2, "retry_count": 1, "reason": "restart-restore"}},
    ]
    n = q.import_from_json(items)
    assert n == 2
    rows = q.list_queued()
    assert {r["submission_id"] for r in rows} == {"sub_1", "sub_2"}


def test_import_from_json_is_idempotent(fake_redis) -> None:
    q = _make_queue()
    items = [{"payload": {"submission_id": "sub_only", "activity_id": "a", "shadow_id": "s"}}]
    q.import_from_json(items)
    n = q.import_from_json(items)
    assert n == 0   # second import skips the existing row


# ── Smoke: factory honours SYSTEMU_QUEUE_BROKER ─────────────────────────────

def test_build_task_queue_picks_redis_when_broker_redis(fake_redis, monkeypatch) -> None:
    monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")
    monkeypatch.setenv("SYSTEMU_REDIS_URL", "redis://localhost:6379/0")
    from systemu.queue.protocol import build_task_queue
    queue = build_task_queue(config=None)
    assert isinstance(queue, RedisPriorityQueue)


def test_build_task_queue_returns_none_when_redis_url_missing(monkeypatch) -> None:
    monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")
    monkeypatch.delenv("SYSTEMU_REDIS_URL", raising=False)
    from systemu.queue.protocol import build_task_queue
    assert build_task_queue(config=None) is None
