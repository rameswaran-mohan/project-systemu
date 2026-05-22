"""ITaskQueue — backend-agnostic interface for activity submission and tracking.

Implementations:
  ThreadTaskQueue   — wraps the current Supervisor (threading-based, local only)
  HueyTaskQueue     — wraps Huey with SqliteHuey or RedisHuey backend (Phase 2)

All implementations must be thread-safe.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Protocol, runtime_checkable


class TaskStatus(str, Enum):
    """Lifecycle states of a submitted task."""
    PENDING   = "pending"    # queued, not yet started
    RUNNING   = "running"    # actively executing
    COMPLETE  = "complete"   # finished successfully
    FAILED    = "failed"     # finished with error
    CANCELLED = "cancelled"  # cancelled by watchdog or user
    DEAD      = "dead"       # exhausted all retries → dead letter


@runtime_checkable
class ITaskQueue(Protocol):
    """Submit activities for execution, query their status."""

    def submit(
        self,
        activity_id: str,
        shadow_id: str,
        *,
        priority: int = 5,
        reason: str = "manual",
        retry_count: int = 0,
    ) -> str:
        """Queue an activity for execution.

        Returns a submission_id for tracking.
        Lower priority number = higher urgency (1=urgent, 5=normal, 10=background).
        """
        ...

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of queue state.

        Keys: queue_depth, running_count, running (list), dead_letters,
              dead_letter_count, max_concurrent.
        """
        ...

    def shutdown(self) -> None:
        """Gracefully stop the queue — persist pending items if possible."""
        ...
