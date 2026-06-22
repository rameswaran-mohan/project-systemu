"""RedisPriorityQueue — durable, crash-safe Supervisor task queue backed by Redis.

Mirrors the semantics of SqlitePriorityQueue (the docker-local + local adapter)
against a Redis instance shared across the dashboard and any number of workers.
Used by docker-enterprise mode where the same Redis also hosts the Huey broker.

Key layout (all keys prefixed with ``systemu:`` so multiple deployments can
share a Redis instance via different prefixes — set SYSTEMU_REDIS_PREFIX to
override the default ``systemu``):

    systemu:queue                 ZSET   score=priority*1e10+enqueued_at  member=submission_id
    systemu:row:<sub_id>          HASH   full payload + state fields
    systemu:running               HASH   submission_id → worker_id (acts as the running set)
    systemu:heartbeat:<sub_id>    STRING last_heartbeat ts (TTL = lease window)
    systemu:deadletter            LIST   JSON-encoded payloads (most recent first)

The TTL on ``heartbeat:<sub_id>`` is the dead-man switch the watchdog reads:
when it expires, ``recover_orphans`` requeues or dead-letters the row.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _resolve_lease_timeout() -> int:
    """Lease window for the heartbeat dead-man switch.

    Why the +100 s margin matters:
        The in-process watchdog (Supervisor.STUCK_THRESHOLD_S, default 300 s)
        is the FAST path — it cancels the local thread and re-queues without
        waiting for any TTL.  The Redis lease is the SLOW path that fires
        only when the entire worker process is gone (crash / kill / network
        partition).  We deliberately set the lease longer than the watchdog
        threshold so:
          1. A healthy-but-slow shadow doesn't get reclaimed by *another*
             host while the local watchdog still has authority over it.
          2. After a real crash, the surviving workers wait one full lease
             window before assuming the row is orphaned — gives the crashed
             worker a chance to come back up cleanly without a duplicate.

    Operator overrides via SYSTEMU_QUEUE_LEASE_TIMEOUT_S; sensible range is
    300–900 seconds.  Going below 300 races the watchdog; going above 900
    delays orphan recovery noticeably during a real outage.

    Resolution order:
        1. SYSTEMU_QUEUE_LEASE_TIMEOUT_S env var (operator override)
        2. Supervisor.STUCK_THRESHOLD_S + 100 s safety margin
        3. 400 s fallback (matches SqlitePriorityQueue, used if Supervisor
           import fails — e.g. early in module init)
    """
    explicit = os.environ.get("SYSTEMU_QUEUE_LEASE_TIMEOUT_S")
    if explicit and explicit.isdigit():
        return int(explicit)
    try:
        from systemu.runtime.supervisor import STUCK_THRESHOLD_S
        return STUCK_THRESHOLD_S + 100
    except Exception:
        return 400


_LEASE_TIMEOUT_S = _resolve_lease_timeout()


def _key(prefix: str, *parts: str) -> str:
    return ":".join((prefix, *parts))


class RedisPriorityQueue:
    """Crash-safe priority queue backed by Redis (used in docker-enterprise mode)."""

    def __init__(
        self,
        redis_url: str,
        worker_id: str,
        *,
        prefix: Optional[str] = None,
        max_retries: int = 2,
    ) -> None:
        try:
            import redis
        except ImportError as exc:
            raise ImportError(
                "redis package is not installed. Run: "
                "pip install -e '.[docker-enterprise]'"
            ) from exc

        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        # Force an early connection so misconfiguration fails loudly at startup.
        self._redis.ping()

        import os
        self._prefix = prefix or os.environ.get("SYSTEMU_REDIS_PREFIX", "systemu")
        self._worker_id = worker_id
        self._max_retries = max_retries

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _row_key(self, submission_id: str) -> str:
        return _key(self._prefix, "row", submission_id)

    def _hb_key(self, submission_id: str) -> str:
        return _key(self._prefix, "heartbeat", submission_id)

    def _queue_key(self) -> str:
        return _key(self._prefix, "queue")

    def _running_key(self) -> str:
        return _key(self._prefix, "running")

    def _dead_letter_key(self) -> str:
        return _key(self._prefix, "deadletter")

    def _score(self, priority: int, enqueued_at: float) -> float:
        """Compose ZSET score: ``priority * 1e10 + enqueued_at``.

        Why this formula (do NOT "simplify" — load-bearing):

        * The score must totally order by (priority ASC, enqueued_at ASC) so
          dequeue order matches SqlitePriorityQueue's
          ``ORDER BY priority, enqueued_at``.
        * Priority is an int 1..10 and must dominate enqueued_at.
        * 1e10 buys ~317 years of unix-timestamp headroom before priority N
          and priority N+1 collide on a float64 — (priority+1)*1e10 only
          equals priority*1e10 + enqueued_at once enqueued_at exceeds 1e10
          seconds ≈ year 2286.
        * float64 has 15–17 significant decimal digits; 1e10 + ts (ts up to
          ~2e9 today) sits comfortably inside that, so no precision loss.

        Lower score sorts earlier — same direction as the Sqlite adapter.
        """
        return priority * 1e10 + enqueued_at

    # ── Write operations ──────────────────────────────────────────────────────

    def enqueue(
        self,
        activity_id: str,
        shadow_id: str,
        *,
        priority: int = 5,
        reason: str = "manual",
        retry_count: int = 0,
        submission_id: Optional[str] = None,
    ) -> str:
        if submission_id is None:
            submission_id = f"sub_{uuid.uuid4().hex[:8]}"
        now = time.time()

        row = {
            "submission_id": submission_id,
            "activity_id": activity_id,
            "shadow_id": shadow_id,
            "priority": priority,
            "retry_count": retry_count,
            "reason": reason,
            "enqueued_at": now,
            "state": "queued",
            "attempt_count": 0,
            "claimed_by": "",
            "claimed_at": 0,
            "last_heartbeat_at": 0,
            "result_json": "",
            "error_text": "",
        }

        # transaction=True wraps in MULTI/EXEC so the row HSET and queue
        # ZADD are atomic — a crash between them can never leave a row
        # without its corresponding queue entry (or vice versa).
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(self._row_key(submission_id), mapping={k: json.dumps(v) for k, v in row.items()})
        pipe.zadd(self._queue_key(), {submission_id: self._score(priority, now)})
        pipe.execute()

        logger.debug(
            "[RedisQueue] Enqueued %s (activity=%s priority=%d retry=%d)",
            submission_id, activity_id, priority, retry_count,
        )
        return submission_id

    def mark_running(self, submission_id: str) -> None:
        """Claim a queued row: row → running, ZREM, register, set heartbeat.

        Concurrency invariant — this method is NOT defended by WATCH.  Two
        workers cannot race to claim the same row because:
          1. Workers dequeue by ZRANGE+ZREM under the dispatcher's lock; the
             ZREM-loser sees an empty result and never calls mark_running.
          2. Even if a stale process called mark_running on an already-claimed
             row, the ZREM here is a no-op (row is gone from the queue) and
             the running-set HSET overwrites with this worker's id.
        If the assumed dequeue-and-claim contract ever changes (e.g. fanout
        of submissions to multiple workers), revisit this with a
        WATCH(row_key) + MULTI/EXEC retry loop.
        """
        now = time.time()
        # Read-modify-write of attempt_count: read the JSON-encoded current
        # value, increment it, then bundle the increment into the same MULTI
        # transaction as the rest of the transition.  Without WATCH this is a
        # plain read; protected by the single-claimer invariant above.
        current_raw = self._redis.hget(self._row_key(submission_id), "attempt_count")
        try:
            current_attempt = json.loads(current_raw) if current_raw else 0
        except (ValueError, TypeError):
            current_attempt = 0

        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(self._row_key(submission_id), mapping={
            "state": json.dumps("running"),
            "claimed_by": json.dumps(self._worker_id),
            "claimed_at": json.dumps(now),
            "last_heartbeat_at": json.dumps(now),
            "attempt_count": json.dumps(int(current_attempt) + 1),
        })
        pipe.zrem(self._queue_key(), submission_id)
        pipe.hset(self._running_key(), submission_id, self._worker_id)
        pipe.set(self._hb_key(submission_id), now, ex=_LEASE_TIMEOUT_S)
        pipe.execute()

    def mark_completed(self, submission_id: str, result: Dict[str, Any]) -> None:
        # MULTI/EXEC: row state, running-set removal, and heartbeat delete
        # all commit together — recover_orphans can never see a "completed"
        # row that's still in the running hash.
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(self._row_key(submission_id), mapping={
            "state": json.dumps("completed"),
            "result_json": json.dumps(json.dumps(result)),
        })
        pipe.hdel(self._running_key(), submission_id)
        pipe.delete(self._hb_key(submission_id))
        pipe.execute()

    def mark_failed(self, submission_id: str, error: str) -> None:
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(self._row_key(submission_id), mapping={
            "state": json.dumps("failed"),
            "error_text": json.dumps(error),
        })
        pipe.hdel(self._running_key(), submission_id)
        pipe.delete(self._hb_key(submission_id))
        pipe.execute()

    def mark_dead_letter(self, submission_id: str, reason: str) -> None:
        row = self._fetch_row(submission_id)
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(self._row_key(submission_id), mapping={
            "state": json.dumps("dead_letter"),
            "error_text": json.dumps(reason),
        })
        pipe.hdel(self._running_key(), submission_id)
        pipe.delete(self._hb_key(submission_id))
        if row:
            row["dead_letter_reason"] = reason
            pipe.lpush(self._dead_letter_key(), json.dumps(row))
            pipe.ltrim(self._dead_letter_key(), 0, 199)  # keep last 200
        pipe.execute()

    def requeue(self, submission_id: str, retry_count: int) -> None:
        now = time.time()
        row = self._fetch_row(submission_id)
        priority = int(row.get("priority", 5)) if row else 5
        # MULTI/EXEC: re-queue must be atomic across (state→queued, remove
        # from running, drop heartbeat, add back to ZSET).  A torn write here
        # would leave a row both in the queue AND in the running hash —
        # recover_orphans would then keep re-requeuing it forever.
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(self._row_key(submission_id), mapping={
            "state": json.dumps("queued"),
            "retry_count": json.dumps(retry_count),
            "enqueued_at": json.dumps(now),
            "claimed_by": json.dumps(""),
            "claimed_at": json.dumps(0),
            "last_heartbeat_at": json.dumps(0),
            "error_text": json.dumps(""),
        })
        pipe.hdel(self._running_key(), submission_id)
        pipe.delete(self._hb_key(submission_id))
        pipe.zadd(self._queue_key(), {submission_id: self._score(priority, now)})
        pipe.execute()

    def update_heartbeat(self, submission_id: str) -> None:
        # Heartbeat doesn't need MULTI/EXEC — both writes are idempotent and
        # losing one is harmless (the next heartbeat refreshes both).
        # Keeping the pipeline for the round-trip cost saving only.
        now = time.time()
        pipe = self._redis.pipeline()
        pipe.hset(self._row_key(submission_id), "last_heartbeat_at", json.dumps(now))
        pipe.set(self._hb_key(submission_id), now, ex=_LEASE_TIMEOUT_S)
        pipe.execute()

    # ── Read operations ───────────────────────────────────────────────────────

    def _fetch_row(self, submission_id: str) -> Dict[str, Any]:
        raw = self._redis.hgetall(self._row_key(submission_id))
        if not raw:
            return {}
        out: Dict[str, Any] = {}
        for k, v in raw.items():
            try:
                out[k] = json.loads(v)
            except (ValueError, TypeError):
                out[k] = v
        return out

    def list_queued(self) -> List[Dict[str, Any]]:
        ids = self._redis.zrange(self._queue_key(), 0, -1)
        rows: List[Dict[str, Any]] = []
        for sid in ids:
            row = self._fetch_row(sid)
            if row and row.get("state") == "queued":
                rows.append({
                    "submission_id": sid,
                    "activity_id": row.get("activity_id", ""),
                    "shadow_id": row.get("shadow_id", ""),
                    "priority": int(row.get("priority", 5)),
                    "retry_count": int(row.get("retry_count", 0)),
                    "reason": row.get("reason", ""),
                    "enqueued_at": float(row.get("enqueued_at", 0)),
                    "attempt_count": int(row.get("attempt_count", 0)),
                })
        return rows

    def list_running(self) -> List[Dict[str, Any]]:
        members = self._redis.hgetall(self._running_key())
        rows: List[Dict[str, Any]] = []
        for sid, worker in members.items():
            row = self._fetch_row(sid)
            if not row:
                continue
            rows.append({
                "submission_id": sid,
                "activity_id": row.get("activity_id", ""),
                "shadow_id": row.get("shadow_id", ""),
                "retry_count": int(row.get("retry_count", 0)),
                "claimed_by": worker,
                "claimed_at": float(row.get("claimed_at", 0)),
                "last_heartbeat_at": float(row.get("last_heartbeat_at", 0)),
                "attempt_count": int(row.get("attempt_count", 0)),
            })
        return rows

    def count_queued(self) -> int:
        return int(self._redis.zcard(self._queue_key()) or 0)

    # ── Crash recovery ────────────────────────────────────────────────────────

    def recover_orphans(self) -> List[Dict[str, Any]]:
        """Re-queue or dead-letter rows whose heartbeat has expired.

        For each entry in the running hash, check whether its heartbeat key
        still exists.  If absent (TTL expired or worker crashed before SET),
        treat the row as orphaned: re-queue with retry+1, or move to the dead
        letter list once retries are exhausted.

        Rows still owned by *this* worker are skipped (the heartbeat thread is
        responsible for them).
        """
        members = self._redis.hgetall(self._running_key())
        recovered: List[Dict[str, Any]] = []

        for sid, worker in members.items():
            if worker == self._worker_id:
                continue   # our own running rows; the watchdog handles them

            if self._redis.exists(self._hb_key(sid)):
                continue   # still within lease; another worker may be alive

            row = self._fetch_row(sid)
            if not row:
                # Row vanished; clean up the dangling running entry
                self._redis.hdel(self._running_key(), sid)
                continue

            retry = int(row.get("retry_count", 0))
            new_retry = retry + 1
            if new_retry <= self._max_retries:
                self.requeue(sid, new_retry)
                action = "requeued"
            else:
                self.mark_dead_letter(
                    sid,
                    f"Orphaned after crash — exhausted retries ({retry})",
                )
                action = "dead_lettered"

            recovered.append({
                "submission_id": sid,
                "activity_id": row.get("activity_id", ""),
                "shadow_id": row.get("shadow_id", ""),
                "old_worker": worker,
                "action": action,
                "new_retry": new_retry,
            })
            logger.info(
                "[RedisQueue] Orphan recovery: %s (activity=%s old_worker=%s) → %s",
                sid, row.get("activity_id", ""), worker, action,
            )

        return recovered

    def import_from_json(self, items: List[Dict[str, Any]]) -> int:
        """One-shot import of supervisor_queue.json items at startup.

        Skips items whose submission_id already has a row in Redis (safe to
        call repeatedly).
        """
        imported = 0
        now = time.time()
        for item in items:
            payload = item.get("payload", item)
            sid = payload.get("submission_id", f"sub_{uuid.uuid4().hex[:8]}")
            try:
                if self._redis.exists(self._row_key(sid)):
                    continue
                self.enqueue(
                    payload.get("activity_id", ""),
                    payload.get("shadow_id", ""),
                    priority=int(payload.get("priority", 5)),
                    reason=payload.get("reason", "restart-restore"),
                    retry_count=int(payload.get("retry_count", 0)),
                    submission_id=sid,
                )
                imported += 1
            except Exception as exc:
                logger.warning("[RedisQueue] Import skipped row %s: %s", sid, exc)
        return imported
