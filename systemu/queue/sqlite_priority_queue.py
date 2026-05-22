"""SqlitePriorityQueue — durable, crash-safe task queue backed by SQLite.

Implements the same interface as ThreadTaskQueue / Supervisor's in-memory queue
but persists every state transition atomically to the supervisor_queue table.

Crash recovery (soft-lease pattern):
  Workers claim rows by writing (state="running", claimed_by=<worker_id>, claimed_at=<ts>).
  On restart, _recover_orphans() finds rows in "running" state claimed by a
  worker_id that no longer matches the current process — these are re-queued
  (if retry_count < MAX_RETRIES) or dead-lettered (if exhausted).

Dequeue is a two-phase atomic operation:
  1. SELECT … WHERE state='queued' ORDER BY priority, enqueued_at LIMIT 1 FOR UPDATE
  2. UPDATE … SET state='running', claimed_by=…, claimed_at=…
  Both wrapped in a single transaction to prevent double-dispatch.

Usage:
    from systemu.queue.sqlite_priority_queue import SqlitePriorityQueue
    q = SqlitePriorityQueue(engine, worker_id="w-1234")
    q.enqueue(payload)
    payload = q.claim_next()
    q.mark_completed(payload["submission_id"], result={})
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

# How long (seconds) a "running" row can be silent before it is reclaimable.
# Must be > STUCK_THRESHOLD_S in supervisor.py (300s) so the watchdog fires first.
_LEASE_TIMEOUT_S = 400


class SqlitePriorityQueue:
    """Crash-safe priority queue backed by the supervisor_queue SQLite table."""

    def __init__(self, engine: Engine, worker_id: str, max_retries: int = 2) -> None:
        self._engine     = engine
        self._worker_id  = worker_id
        self._max_retries = max_retries

    # ── Write operations (all atomic) ─────────────────────────────────────────

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
        """Insert a new queued row.  Returns the submission_id."""
        if submission_id is None:
            submission_id = f"sub_{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO supervisor_queue
                  (submission_id, activity_id, shadow_id, priority, retry_count,
                   reason, enqueued_at, state, attempt_count)
                VALUES
                  (:sid, :aid, :shid, :prio, :retry, :reason, :now, 'queued', 0)
            """), {
                "sid":    submission_id,
                "aid":    activity_id,
                "shid":   shadow_id,
                "prio":   priority,
                "retry":  retry_count,
                "reason": reason,
                "now":    now,
            })
        logger.debug("[SqliteQueue] Enqueued %s (activity=%s retry=%d)", submission_id, activity_id, retry_count)
        return submission_id

    def claim_next(self) -> Optional[Dict[str, Any]]:
        """Atomically claim the highest-priority queued row.

        Returns the payload dict, or None if the queue is empty.
        The row is moved to state='running' in the same transaction.
        """
        now = time.time()
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                SELECT submission_id, activity_id, shadow_id, priority,
                       retry_count, reason, enqueued_at, attempt_count
                FROM supervisor_queue
                WHERE state = 'queued'
                ORDER BY priority ASC, enqueued_at ASC
                LIMIT 1
            """)).fetchone()

            if row is None:
                return None

            sid = row[0]
            conn.execute(text("""
                UPDATE supervisor_queue
                SET state = 'running',
                    claimed_by = :worker,
                    claimed_at = :now,
                    last_heartbeat_at = :now,
                    attempt_count = attempt_count + 1
                WHERE submission_id = :sid
            """), {"worker": self._worker_id, "now": now, "sid": sid})

        return {
            "submission_id": row[0],
            "activity_id":   row[1],
            "shadow_id":     row[2],
            "priority":      row[3],
            "retry_count":   row[4],
            "reason":        row[5],
            "enqueued_at":   row[6],
            "attempt_count": row[7],
        }

    def mark_running(self, submission_id: str) -> None:
        """Mark a queued row as running (dual-write mode — in-memory queue drives dispatch)."""
        now = time.time()
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE supervisor_queue
                SET state = 'running',
                    claimed_by = :worker,
                    claimed_at = :now,
                    last_heartbeat_at = :now,
                    attempt_count = attempt_count + 1
                WHERE submission_id = :sid
            """), {"worker": self._worker_id, "now": now, "sid": submission_id})

    def mark_completed(self, submission_id: str, result: Dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE supervisor_queue
                SET state = 'completed', result_json = :res
                WHERE submission_id = :sid
            """), {"sid": submission_id, "res": json.dumps(result)})

    def mark_failed(self, submission_id: str, error: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE supervisor_queue
                SET state = 'failed', error_text = :err
                WHERE submission_id = :sid
            """), {"sid": submission_id, "err": error})

    def mark_dead_letter(self, submission_id: str, reason: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE supervisor_queue
                SET state = 'dead_letter', error_text = :reason
                WHERE submission_id = :sid
            """), {"sid": submission_id, "reason": reason})

    def requeue(self, submission_id: str, retry_count: int) -> None:
        """Move a running/failed row back to queued (for retry)."""
        now = time.time()
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE supervisor_queue
                SET state = 'queued',
                    retry_count = :retry,
                    enqueued_at = :now,
                    claimed_by = NULL,
                    claimed_at = NULL,
                    last_heartbeat_at = NULL,
                    error_text = NULL
                WHERE submission_id = :sid
            """), {"sid": submission_id, "retry": retry_count, "now": now})

    def update_heartbeat(self, submission_id: str) -> None:
        """Update the heartbeat timestamp for a running row."""
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE supervisor_queue
                SET last_heartbeat_at = :now
                WHERE submission_id = :sid
            """), {"sid": submission_id, "now": time.time()})

    # ── Read operations ────────────────────────────────────────────────────────

    def list_queued(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT submission_id, activity_id, shadow_id, priority,
                       retry_count, reason, enqueued_at, attempt_count
                FROM supervisor_queue WHERE state = 'queued'
                ORDER BY priority ASC, enqueued_at ASC
            """)).fetchall()
        return [
            {"submission_id": r[0], "activity_id": r[1], "shadow_id": r[2],
             "priority": r[3], "retry_count": r[4], "reason": r[5],
             "enqueued_at": r[6], "attempt_count": r[7]}
            for r in rows
        ]

    def list_running(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT submission_id, activity_id, shadow_id, retry_count,
                       claimed_by, claimed_at, last_heartbeat_at, attempt_count
                FROM supervisor_queue WHERE state = 'running'
            """)).fetchall()
        return [
            {"submission_id": r[0], "activity_id": r[1], "shadow_id": r[2],
             "retry_count": r[3], "claimed_by": r[4], "claimed_at": r[5],
             "last_heartbeat_at": r[6], "attempt_count": r[7]}
            for r in rows
        ]

    def count_queued(self) -> int:
        with self._engine.connect() as conn:
            return conn.execute(
                text("SELECT COUNT(*) FROM supervisor_queue WHERE state = 'queued'")
            ).scalar() or 0

    # ── Crash recovery ─────────────────────────────────────────────────────────

    def recover_orphans(self) -> List[Dict[str, Any]]:
        """Find running rows claimed by a different worker_id (orphaned after a crash).

        Rows within the lease window (< _LEASE_TIMEOUT_S old) are left alone —
        a parallel worker may still be executing them.  Rows past the timeout are
        re-queued (if retries remain) or dead-lettered.

        Returns a list of dicts describing what was recovered (for logging).
        """
        now = time.time()
        lease_cutoff = now - _LEASE_TIMEOUT_S

        with self._engine.connect() as conn:
            orphans = conn.execute(text("""
                SELECT submission_id, activity_id, shadow_id, retry_count,
                       priority, reason, claimed_by, claimed_at
                FROM supervisor_queue
                WHERE state = 'running'
                  AND claimed_by != :wid
                  AND (claimed_at IS NULL OR claimed_at < :cutoff)
            """), {"wid": self._worker_id, "cutoff": lease_cutoff}).fetchall()

        recovered = []
        for row in orphans:
            sid, aid, shid, retry, prio, reason, old_worker, _ = row
            new_retry = retry + 1
            if new_retry <= self._max_retries:
                self.requeue(sid, new_retry)
                action = "requeued"
            else:
                self.mark_dead_letter(sid, f"Orphaned after crash — exhausted retries ({retry})")
                action = "dead_lettered"
            recovered.append({
                "submission_id": sid,
                "activity_id":   aid,
                "shadow_id":     shid,
                "old_worker":    old_worker,
                "action":        action,
                "new_retry":     new_retry,
            })
            logger.info(
                "[SqliteQueue] Orphan recovery: %s (activity=%s old_worker=%s) → %s",
                sid, aid, old_worker, action,
            )

        return recovered

    def import_from_json(self, items: List[Dict[str, Any]]) -> int:
        """One-shot import of supervisor_queue.json items at startup.

        Returns count of rows imported.  Skips items whose submission_id already
        exists (safe to call even if import ran partially before a crash).
        """
        imported = 0
        now = time.time()
        for item in items:
            payload = item.get("payload", item)   # support both raw payload and wrapped
            sid = payload.get("submission_id", f"sub_{uuid.uuid4().hex[:8]}")
            try:
                with self._engine.begin() as conn:
                    conn.execute(text("""
                        INSERT OR IGNORE INTO supervisor_queue
                          (submission_id, activity_id, shadow_id, priority, retry_count,
                           reason, enqueued_at, state, attempt_count)
                        VALUES
                          (:sid, :aid, :shid, :prio, :retry, :reason, :now, 'queued', 0)
                    """), {
                        "sid":    sid,
                        "aid":    payload.get("activity_id", ""),
                        "shid":   payload.get("shadow_id", ""),
                        "prio":   payload.get("priority", 5),
                        "retry":  payload.get("retry_count", 0),
                        "reason": payload.get("reason", "restart-restore"),
                        "now":    payload.get("enqueued_at", now),
                    })
                imported += 1
            except Exception as exc:
                logger.warning("[SqliteQueue] Import skipped row %s: %s", sid, exc)
        return imported
