"""ThreadTaskQueue — ITaskQueue adapter around the current thread-based Supervisor.

Zero behaviour change.  Delegates submit/get_status/shutdown to Supervisor so
call sites using ITaskQueue are decoupled from the concrete Supervisor class.

When Phase 2 (Huey) lands, this class is retired and HueyTaskQueue takes its
place — no call sites change.

Usage:
    from systemu.runtime.supervisor import Supervisor
    from systemu.queue.thread_task_queue import ThreadTaskQueue

    sup   = Supervisor.init(config, vault)
    queue: ITaskQueue = ThreadTaskQueue(sup)
"""

from __future__ import annotations

from typing import Any, Dict


class ThreadTaskQueue:
    """ITaskQueue implementation backed by the thread-based Supervisor."""

    def __init__(self, supervisor: Any) -> None:
        """
        Args:
            supervisor: A Supervisor instance (or any object with .submit(),
                        .get_status(), and .shutdown() methods).
        """
        self._sup = supervisor

    def submit(
        self,
        activity_id: str,
        shadow_id: str,
        *,
        priority: int = 5,
        reason: str = "manual",
        retry_count: int = 0,
    ) -> str:
        """Queue an activity for execution.  Returns the submission_id."""
        return self._sup.submit(
            activity_id,
            shadow_id,
            priority=priority,
            reason=reason,
            retry_count=retry_count,
        )

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of supervisor state."""
        return self._sup.get_status()

    def shutdown(self) -> None:
        """Gracefully shut down the supervisor, persisting pending queue items."""
        self._sup.shutdown()
