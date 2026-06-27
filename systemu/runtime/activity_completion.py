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
        # v0.9.32: an operator interrupt is terminal but NOT a failure — map the
        # "cancelled" status to its own terminal state so the dashboard and the
        # post-mortem skip-check can tell an intentional stop from a real failure.
        terminal = (ActivityStatus.CANCELLED if status == "cancelled"
                    else ActivityStatus.FAILED)
        activity = vault.get_activity(activity_id)
        activity.status = terminal
        activity.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        vault.save_activity(activity)
        logger.info("[ActivityCompletion] Activity %s marked %s: %s",
                    activity_id, terminal.value.upper(), (summary or "")[:160])
        return True
    except Exception as exc:
        logger.warning(
            "[ActivityCompletion] Could not mark activity %s FAILED: %s",
            activity_id, exc,
        )
        return False


def _tool_unavailable_reason(vault, tool_id: str):
    """v0.9.49: return a human reason iff ``tool_id`` can NEVER become available to
    this run — its record is missing, the operator DECLINED its forge gate
    (``forge_rejected``), or its dry-run permanently FAILED. Returns None for a
    satisfiable tool, INCLUDING a ``proposed``/``not_run`` tool whose forge is
    merely *pending* (``forge_rejected`` False) — that one the operator may still
    approve, so it must not be treated as permanently blocked."""
    try:
        tool = vault.get_tool(tool_id)
    except Exception:
        return f"{tool_id} (tool record missing)"
    name = getattr(tool, "name", tool_id) or tool_id
    if getattr(tool, "forge_rejected", False):
        return f"{name} (declined at forge review)"
    if (getattr(tool, "dry_run_status", "") or "") == "failed":
        err = (getattr(tool, "dry_run_evidence", None) or {}).get("error") or ""
        return f"{name} (dry-run failed{': ' + err[:120] if err else ''})"
    return None


def _tool_is_permanently_unavailable(vault, tool_id: str) -> bool:
    """True iff the tool can never become enable-able this run (see
    ``_tool_unavailable_reason``). The single source of truth shared by the inbox
    handlers and the reconciler reaper."""
    return _tool_unavailable_reason(vault, tool_id) is not None


def finalize_unsatisfiable_activity(vault, activity_id: str, *, context: str = "") -> str:
    """v0.9.49: idempotently finalize a PARTIAL activity parked on a tool that can
    never become available.

    Returns the failure summary string (TRUTHY) when it finalizes, else ``""``
    (falsy). Finalizes only when BOTH hold: the activity is currently
    ``ActivityStatus.PARTIAL`` (idempotency + only-reap-parked guard — a second
    call, or one racing the reaper, is a no-op once it's terminal), AND **ANY** of
    its ``required_tool_ids`` is permanently unavailable (so a task with one
    satisfiable tool + one declined/failed tool — the repro shape — is finalized,
    while a task still waiting on a satisfiable tool is left alone). The summary
    names the blocking tool(s) (incl. a failed tool's dry-run error) so the caller
    can surface it; the terminal write delegates to ``mark_activity_failed`` and
    the parked ``waiting_on_tools`` chat entry is flipped to ``failed``.
    Best-effort throughout; never raises."""
    try:
        from systemu.core.models import ActivityStatus
        activity = vault.get_activity(activity_id)
    except Exception:
        return ""
    if getattr(activity, "status", None) != ActivityStatus.PARTIAL:
        return ""
    reasons = []
    for tid in (getattr(activity, "required_tool_ids", None) or []):
        r = _tool_unavailable_reason(vault, tid)
        if r:
            reasons.append(r)
    if not reasons:
        return ""

    summary = ((context.strip() + " ") if context else "") + (
        "Required tool(s) unavailable: " + "; ".join(reasons)
        + ". Task finalized — re-run and approve/repair the tool(s) to retry.")
    if not mark_activity_failed(vault, activity_id, summary=summary):
        return ""
    # Best-effort: flip the parked waiting_on_tools chat entry so the chat surface
    # stops showing it as in-flight.
    try:
        for entry in vault.load_chat_history(limit=50):
            if (entry.get("activity_id") == activity_id
                    and entry.get("status") == "waiting_on_tools"):
                vault.update_chat_history_entry(
                    entry.get("ts"), {"status": "failed", "error": summary})
                break
    except Exception:
        logger.debug(
            "[ActivityCompletion] could not flip chat entry for %s",
            activity_id, exc_info=True)
    return summary
