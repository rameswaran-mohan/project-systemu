"""TaskQueueProtocol — the contract Supervisor uses for durable, crash-safe queues.

Two concrete implementations live alongside this module:
  • SqlitePriorityQueue (systemu/queue/sqlite_priority_queue.py) — local + docker-local
  • RedisPriorityQueue  (systemu/queue/redis_priority_queue.py)  — docker-enterprise

The protocol captures every method Supervisor calls on its `_task_queue` attribute.
Keeping it as a typing.Protocol (no abstract base, no runtime cost) lets either
implementation be injected without inheritance and keeps tests trivial to mock.

Selecting an implementation:
  build_task_queue(config) — factory in this module — branches on
  SYSTEMU_QUEUE_BROKER (sqlite | redis).  Returns None when the supervisor is
  meant to run in pure in-memory mode (file-backend installations).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class TaskQueueProtocol(Protocol):
    """Methods Supervisor (and operational surfaces) call on a durable task queue.

    Implementations must satisfy this contract — both
    :class:`SqlitePriorityQueue` and :class:`RedisPriorityQueue` do today.  The
    operational getters (``list_running``, ``count_queued``) are needed by the
    dashboard and metrics endpoints, so they live on the protocol rather than
    being adapter-private.
    """

    # ── Submission lifecycle ────────────────────────────────────────────────
    def enqueue(
        self,
        activity_id: str,
        shadow_id: str,
        *,
        priority: int = 5,
        reason: str = "manual",
        retry_count: int = 0,
        submission_id: Optional[str] = None,
    ) -> str: ...

    def mark_running(self, submission_id: str) -> None: ...
    def mark_completed(self, submission_id: str, result: Dict[str, Any]) -> None: ...
    def mark_failed(self, submission_id: str, error: str) -> None: ...
    def mark_dead_letter(self, submission_id: str, reason: str) -> None: ...
    def requeue(self, submission_id: str, retry_count: int) -> None: ...

    # ── Liveness ────────────────────────────────────────────────────────────
    def update_heartbeat(self, submission_id: str) -> None: ...

    # ── Inspection / recovery ───────────────────────────────────────────────
    def list_queued(self) -> List[Dict[str, Any]]: ...
    def list_running(self) -> List[Dict[str, Any]]: ...
    def count_queued(self) -> int: ...
    def recover_orphans(self) -> List[Dict[str, Any]]: ...
    def import_from_json(self, items: List[Dict[str, Any]]) -> int: ...


# ─────────────────────────────────────────────────────────────────────────────
#  Factory — picks the right adapter for the active mode
# ─────────────────────────────────────────────────────────────────────────────

def build_task_queue(config: Any) -> Optional[TaskQueueProtocol]:
    """Construct the durable task queue for the current environment.

    Selection rules (most-specific first):
      • SYSTEMU_QUEUE_BROKER=redis  → RedisPriorityQueue (requires SYSTEMU_REDIS_URL)
      • SYSTEMU_STORAGE in (sqlite, postgres) and SYSTEMU_DATABASE_URL set
            → SqlitePriorityQueue (over the SQLAlchemy engine for that URL)
      • Otherwise → None (Supervisor runs pure in-memory; suitable for file mode)

    The returned object satisfies TaskQueueProtocol.  Callers that want the
    legacy unconditional behaviour can ignore None — Supervisor already has
    a None-safe path for the file backend.
    """
    broker = os.environ.get("SYSTEMU_QUEUE_BROKER", "").lower()
    storage = os.environ.get("SYSTEMU_STORAGE", "file").lower()
    db_url = os.environ.get("SYSTEMU_DATABASE_URL", "")
    mode = os.environ.get("SYSTEMU_MODE", "").lower()
    strict = mode == "docker-enterprise"

    worker_id = f"proc-{os.getpid()}-{uuid.uuid4().hex[:6]}"

    def _fail(reason: str) -> None:
        """In strict mode (docker-enterprise) refuse to silently degrade —
        crash safety is the whole point of choosing that mode.  In other modes
        log loudly but allow the in-memory fallback so a broken Redis doesn't
        knock the whole dashboard offline."""
        if strict:
            raise RuntimeError(
                f"[TaskQueue] {reason}  Refusing to degrade to in-memory queue "
                f"because SYSTEMU_MODE=docker-enterprise (crash safety required)."
            )
        logger.error("[TaskQueue] %s  Falling back to in-memory queue.", reason)

    if broker == "redis":
        redis_url = os.environ.get("SYSTEMU_REDIS_URL", "")
        if not redis_url:
            _fail("SYSTEMU_QUEUE_BROKER=redis but SYSTEMU_REDIS_URL is unset.")
            return None
        try:
            from systemu.queue.redis_priority_queue import RedisPriorityQueue
            return RedisPriorityQueue(redis_url, worker_id=worker_id)
        except ImportError as exc:
            _fail(
                f"redis package not installed ({exc}). "
                f"Install with: pip install -e '.[docker-enterprise]'"
            )
            return None
        except Exception as exc:
            _fail(f"Redis connection failed: {exc}")
            return None

    if storage in ("sqlite", "postgres") and db_url:
        try:
            from sqlalchemy import create_engine
            from systemu.queue.sqlite_priority_queue import SqlitePriorityQueue

            connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
            engine = create_engine(db_url, connect_args=connect_args)
            return SqlitePriorityQueue(engine, worker_id=worker_id)
        except ImportError as exc:
            _fail(f"sqlalchemy not installed ({exc}).")
            return None

    return None
