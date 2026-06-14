"""One terminal-state writer for completed activities (Wave 1.4).

The Supervisor's queued path marked activities COMPLETED inline
(`_handle_result`), but the synchronous path (`run_direct_task` with
``route_through_supervisor=False``) never did — a sync-executed task left its
activity stuck at ``assigned`` in the vault forever, so the dashboard showed
finished work as never-finished.  Both paths now call this one helper.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def mark_activity_completed(vault, activity_id: str) -> bool:
    """Persist ``ActivityStatus.COMPLETED`` on the activity (terminal state).

    Mirrors the Supervisor's original inline semantics exactly: naive-UTC
    ``updated_at`` stamp, never raises (best-effort — a failure to mark must
    not fail the run that just succeeded).  Returns True when persisted.
    """
    try:
        from systemu.core.models import ActivityStatus
        activity = vault.get_activity(activity_id)
        activity.status = ActivityStatus.COMPLETED
        activity.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        vault.save_activity(activity)
        logger.info("[ActivityCompletion] Activity %s marked COMPLETED", activity_id)
        return True
    except Exception as exc:
        logger.warning(
            "[ActivityCompletion] Could not mark activity %s COMPLETED: %s",
            activity_id, exc,
        )
        return False


def mark_activity_failed(vault, activity_id: str, *, status: str = "failed",
                         summary: str = "") -> bool:
    """Persist ``ActivityStatus.FAILED`` (terminal) so an activity that exhausted
    retries or hit a structural blocker is conclusively finished, not orphaned at
    ASSIGNED (the recorded-task "zombie" RCA). Best-effort — never raises; a
    failed mark must not fail the run that already concluded.  Returns True when
    persisted."""
    try:
        from systemu.core.models import ActivityStatus
        activity = vault.get_activity(activity_id)
        activity.status = ActivityStatus.FAILED
        activity.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        vault.save_activity(activity)
        logger.info("[ActivityCompletion] Activity %s marked FAILED (%s): %s",
                    activity_id, status, (summary or "")[:160])
        return True
    except Exception as exc:
        logger.warning(
            "[ActivityCompletion] Could not mark activity %s FAILED: %s",
            activity_id, exc,
        )
        return False
