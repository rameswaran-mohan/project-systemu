"""HueyTaskQueue — ITaskQueue implementation backed by Huey SqliteHuey.

Cross-process shadow execution for SYSTEMU_STORAGE=sqlite mode.

Architecture:
  - Dashboard process calls submit() → enqueues a Huey task into the SQLite DB.
  - Worker process (systemu-worker) runs huey_consumer, picks up the task,
    bootstraps its own AppState, and executes the shadow.
  - Results are stored in Huey's result store (same SQLite DB) and polled by
    the dashboard via the result handle.

Usage:
    queue = HueyTaskQueue.create_sqlite("sqlite:///path/to/systemu.db")
    submission_id = queue.submit(activity_id, shadow_id, priority=5)
    status = queue.get_status()
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# In-process tracking of submitted Huey results (submission_id → AsyncResult).
# Protected by _results_lock — submit() and get_status() may be called from
# different NiceGUI async handlers or Huey callback threads.
_pending_results: Dict[str, Any] = {}
_results_lock = threading.Lock()


class HueyTaskQueue:
    """ITaskQueue backed by Huey SqliteHuey for cross-process task dispatch.

    One instance per process — the Huey instance (and DB connection) is shared
    via the module-level singleton in huey_app.py.
    """

    def __init__(self) -> None:
        from systemu.queue.huey_app import get_huey, get_execute_shadow_task
        self._huey    = get_huey()          # raises ImportError if huey not installed
        self._task_fn = get_execute_shadow_task()

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create_sqlite(cls, db_url_or_path: Any) -> "HueyTaskQueue":
        """Create a HueyTaskQueue pointing at the given SQLite database.

        Args:
            db_url_or_path: Either a SQLAlchemy URL ("sqlite:///path/to.db")
                            or a pathlib.Path / str to the .db file.

        This method sets SYSTEMU_DATABASE_URL before importing huey_app so
        the Huey instance points at the correct DB file.  If SYSTEMU_DATABASE_URL
        is already set in the environment (e.g. set by docker-compose), it is
        NOT overwritten — the docker-compose value takes precedence.
        """
        # Normalise to a URL string
        if isinstance(db_url_or_path, Path):
            db_url = f"sqlite:///{db_url_or_path}"
        elif isinstance(db_url_or_path, str) and not db_url_or_path.startswith("sqlite"):
            db_url = f"sqlite:///{db_url_or_path}"
        else:
            db_url = str(db_url_or_path)

        # setdefault: only write if not already set (docker-compose env wins)
        os.environ.setdefault("SYSTEMU_DATABASE_URL", db_url)
        os.environ.setdefault("SYSTEMU_QUEUE_BROKER", "sqlite")

        return cls()

    @classmethod
    def create_redis(cls, redis_url: str) -> "HueyTaskQueue":
        """Create a HueyTaskQueue backed by RedisHuey.

        Args:
            redis_url: redis:// connection URL (e.g. redis://redis:6379/0).

        Mirrors create_sqlite() but selects the Redis broker.  The Redis URL
        is exposed to huey_app.get_huey() through SYSTEMU_REDIS_URL.
        """
        os.environ.setdefault("SYSTEMU_REDIS_URL", redis_url)
        os.environ["SYSTEMU_QUEUE_BROKER"] = "redis"
        return cls()

    # ── ITaskQueue interface ──────────────────────────────────────────────────

    def submit(
        self,
        activity_id: str,
        shadow_id:   str,
        *,
        priority:    int = 5,
        reason:      str = "manual",
        retry_count: int = 0,
    ) -> str:
        """Enqueue a shadow execution job and return a submission ID.

        The job is serialised into the Huey SQLite task table.  The worker
        process picks it up asynchronously.

        Returns:
            A submission_id string (Huey task UUID) for tracking.
        """
        result = self._task_fn(
            activity_id,
            shadow_id,
            priority=priority,
            reason=reason,
            retry_count=retry_count,
        )
        submission_id = str(result.id)
        with _results_lock:
            _pending_results[submission_id] = result
        logger.info(
            "[HueyQueue] Enqueued — activity=%s shadow=%s submission=%s",
            activity_id, shadow_id, submission_id,
        )
        return submission_id

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of the task queue state.

        Huey's SqliteHuey doesn't expose running/pending counts via a public
        API the way a thread-based supervisor does.  We track submitted results
        in-process and check their status.
        """
        pending   = 0
        complete  = 0
        failed    = 0
        dead_list = []
        done_keys = []

        with _results_lock:
            snapshot = list(_pending_results.items())

        for sid, result in snapshot:
            try:
                if result.is_complete:
                    val = result.get(blocking=False)
                    status = val.get("status", "complete") if isinstance(val, dict) else "complete"
                    if status in ("failure", "failed", "error"):
                        failed  += 1
                        dead_list.append({
                            "submission_id": sid,
                            "status": status,
                            "error": val.get("error", "") if isinstance(val, dict) else str(val),
                        })
                    else:
                        complete += 1
                    done_keys.append(sid)
                else:
                    pending += 1
            except Exception:
                pending += 1

        # Prune completed results to prevent unbounded memory growth (keep last 20)
        with _results_lock:
            for key in done_keys[:-20]:
                _pending_results.pop(key, None)

        return {
            "queue_depth":       pending,
            "running_count":     0,       # Huey threads run in the worker process
            "running":           [],
            "dead_letters":      dead_list[-20:],
            "dead_letter_count": failed,
            "max_concurrent":    4,       # Huey default worker count
        }

    def shutdown(self) -> None:
        """No-op — Huey consumer manages its own lifecycle in the worker process."""
        pass

    # ── Public accessors ──────────────────────────────────────────────────────

    @property
    def huey(self) -> Any:
        """Expose the underlying Huey instance (used by worker.py consumer)."""
        return self._huey
