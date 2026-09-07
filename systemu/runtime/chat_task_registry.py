"""v0.9.32 (item 3, D3.2) — process-global cancel-token registry for the chat lane.

The chat lane (quick + sync ``run_now``) runs on an untracked daemon thread with
no Supervisor slot, so it has no ``cancel_event``. This module is the chat-lane
analogue of ``Supervisor._running[key]["cancel_event"]``: a process-global map
from a chat-history timestamp id to a ``threading.Event`` that the lane checks at
its loop boundary and the dashboard Stop button sets.

Per-process only (a docker multi-process cancel is documented out-of-scope).
"""
from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_EVENTS: Dict[str, threading.Event] = {}

# D6 (dogfood 0.10.28) — the chat-history statuses that mean "this task is not
# finished". `queued` belongs here: a Supervisor-routed chat task sits in that
# state for its whole run, and leaving it out is how a queued task ended up with
# neither a Stop button nor a blocking check.
ACTIVE_CHAT_STATUSES = frozenset({
    "running", "queued", "waiting_on_tools", "pending_decision", "needs_input",
})

# The note left on a pending gate card when the runtime has no way to withdraw
# it (the operator still needs to know the run behind it is gone).
GATE_CANCELLED_NOTE = "task was cancelled"

_CANCEL_SUMMARY = "Stopped by operator"


def register(ts: str) -> threading.Event:
    """Register (or re-fetch) the cancel Event for chat task ``ts``.

    Idempotent: re-registering the same id returns the SAME live Event so an
    in-flight cancel is never orphaned by a second register call."""
    with _LOCK:
        ev = _EVENTS.get(ts)
        if ev is None:
            ev = threading.Event()
            _EVENTS[ts] = ev
        return ev


def request_cancel(ts: str) -> bool:
    """Operator Stop for chat task ``ts`` — set its Event. Default-deny: an
    unknown/unregistered id returns False (no Event created)."""
    with _LOCK:
        ev = _EVENTS.get(ts)
        if ev is None:
            return False
        ev.set()
    logger.info("[ChatTaskRegistry] cancel requested for ts=%s", ts)
    return True


def unregister(ts: str) -> None:
    """Drop the Event for ``ts`` (called from the lane's ``finally``). Never
    raises; a missing id is a no-op so double-unregister is safe."""
    with _LOCK:
        _EVENTS.pop(ts, None)


def active_count() -> int:
    """Number of in-flight chat-lane tasks (registered, not yet finalized).

    v0.9.37: drives the dashboard's Live busy indicator for the chat lane.
    Chat / quick-answer tasks run on an untracked daemon thread (no Supervisor
    slot), so the spinner's ``background_activity_count`` could not see them; a
    task is registered at start and unregistered in its ``finally``, so the live
    map size is the chat-lane analogue of ``Supervisor.running_count`` (a
    cancelling task counts until its lane finalizes)."""
    with _LOCK:
        return len(_EVENTS)


def is_registered(ts: str) -> bool:
    """True iff a cancel token for ``ts`` is live IN THIS PROCESS.

    D6(d): this is the honest "restored vs live" marker, and it needs no write
    at restore time. The registry is per-process and populated only by a lane
    that is actually executing, so a chat-history row whose id is absent here
    survived a daemon restart with nobody working it. Compare with the
    Supervisor for the queued lane — see :func:`chat_task_is_live`.
    """
    if not ts:
        return False
    with _LOCK:
        return ts in _EVENTS


def _supervisor_is_working(supervisor: Any, activity_id: str) -> bool:
    """Duck-typed read of the Supervisor's running + pending sets.

    Deliberately a COPY of ``scheduler.jobs._run_is_live``'s shape rather than
    an import: this module must stay importable without the scheduler stack, and
    a fake supervisor is what the tests inject. Unlike that helper, an unreadable
    running set here reports NOT live — the caller uses this to decide whether to
    tell the operator "nothing is working on it", and claiming a live worker we
    cannot see would re-create the silence D6 is about.
    """
    if not activity_id or supervisor is None:
        return False
    running = getattr(supervisor, "_running", None)
    if isinstance(running, dict):
        lock = getattr(supervisor, "_running_lock", None)
        try:
            with (lock or contextlib.nullcontext()):
                for entry in list(running.values()):
                    payload = entry.get("payload", {}) if isinstance(entry, dict) else {}
                    if payload.get("activity_id") == activity_id:
                        return True
        except Exception:
            logger.debug("[ChatTaskRegistry] unreadable supervisor running set",
                         exc_info=True)
    pending = getattr(supervisor, "_pending_activity_ids", None)
    if pending is not None:
        plock = getattr(supervisor, "_pending_lock", None)
        try:
            with (plock or contextlib.nullcontext()):
                if activity_id in pending:
                    return True
        except Exception:
            logger.debug("[ChatTaskRegistry] unreadable supervisor pending set",
                         exc_info=True)
    return False


def chat_task_is_live(entry: Dict[str, Any], *, supervisor: Any = None) -> bool:
    """Is this chat-history row actually being WORKED right now?

    D6(d). Two lanes, two oracles:
      * the sync/quick chat lane runs on this process's own daemon thread and
        registers a cancel token here for its whole life  -> ``is_registered``;
      * a queued workflow is owned by the Supervisor, whose ``_running`` /
        ``_pending_activity_ids`` sets name the activity -> ``_supervisor_is_working``.

    A row in a non-terminal status that neither oracle claims is RESTORED: the
    persisted state came back after a restart (correctly) but no worker owns it.
    A terminal row is never live.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("status") not in ACTIVE_CHAT_STATUSES:
        return False
    if is_registered(entry.get("ts") or ""):
        return True
    return _supervisor_is_working(supervisor, entry.get("activity_id") or "")


def _find_entry(vault: Any, ts: str) -> Optional[Dict[str, Any]]:
    try:
        for entry in reversed(vault.load_chat_history(limit=200) or []):
            if entry.get("ts") == ts:
                return entry
    except Exception:
        logger.debug("[ChatTaskRegistry] could not read chat history", exc_info=True)
    return None


def task_title(entry: Dict[str, Any], *, cap: int = 60) -> str:
    """A short human name for a chat task — its prompt, else its id."""
    prompt = (entry.get("prompt") or "").strip().replace("\n", " ")
    if not prompt:
        return str(entry.get("ts") or "the task")
    return prompt[:cap] + ("..." if len(prompt) > cap else "")


def _withdraw_gate(vault: Any, entry: Dict[str, Any], queue: Any) -> tuple:
    """Retire the pending operator decision a cancelled run is parked on.

    Uses the decision queue's EXISTING public retirement API
    (``expire_by_dedup_key``) — nothing in ``inbox.py`` / ``gate.py`` is touched.
    Returns ``(withdrawn, note)``; when the card cannot be retired the note is
    what the UI shows beside it so the operator is not left with a live-looking
    gate for a run that no longer exists.
    """
    if not (entry.get("decision_id") or entry.get("dedup_key")):
        return False, ""
    dedup_key = entry.get("dedup_key") or ""
    if dedup_key:
        try:
            q = queue
            if q is None:
                from systemu.approval.decision_queue import OperatorDecisionQueue
                q = OperatorDecisionQueue(vault)
            if q.expire_by_dedup_key(dedup_key):
                return True, ""
        except Exception:
            logger.debug("[ChatTaskRegistry] gate withdrawal failed", exc_info=True)
    return False, GATE_CANCELLED_NOTE


def cancel_chat_task(vault: Any, ts: str, *, entry: Optional[Dict[str, Any]] = None,
                     supervisor: Any = None, queue: Any = None) -> Dict[str, Any]:
    """D6(b) — the operator's STOP for a chat task. The chat lane's cancel entry.

    Does all four things a stop has to do, and REPORTS each one rather than
    returning a bare bool (the 0.10.28 Stop button returned False for a wedged
    task and said "Task is no longer running", which was both unhelpful and
    untrue — the row stayed RUNNING forever):

      1. sets the in-process cancel token, so a live chat-lane loop exits at its
         next boundary;
      2. asks the Supervisor to cancel the run by ACTIVITY ID, so a queued run
         in a worker thread exits too;
      3. retires the pending gate card the run is parked on (or reports the note
         to leave beside it);
      4. writes the terminal ``cancelled`` status DURABLY — this is the step that
         actually unwedges Chat, because a restored row has no worker left to
         write its own terminal state.

    Never raises. Returns a report dict:
    ``{ts, title, stopped, live, signalled, supervisor_signalled,
       gate_withdrawn, gate_note, reason}``.
    """
    report: Dict[str, Any] = {
        "ts": ts, "title": ts, "stopped": False, "live": False,
        "signalled": False, "supervisor_signalled": False,
        "gate_withdrawn": False, "gate_note": "", "reason": "",
    }
    # Signal the in-process token FIRST and unconditionally (v0.9.32 D3.2's
    # contract): the operator clicked Stop, and a live lane must exit at its next
    # boundary whether or not its chat-history row can be read back here.
    try:
        report["signalled"] = request_cancel(ts)
    except Exception:
        logger.debug("[ChatTaskRegistry] cancel-token signal failed", exc_info=True)

    row = entry if entry is not None else _find_entry(vault, ts)
    if row is None:
        report["reason"] = "no chat task with that id (it may have been cleared)"
        return report

    report["title"] = task_title(row)
    status = row.get("status")
    if status not in ACTIVE_CHAT_STATUSES:
        report["reason"] = f"the task already finished ({status})"
        return report

    report["live"] = chat_task_is_live(row, supervisor=supervisor)

    activity_id = row.get("activity_id") or ""
    if activity_id and supervisor is not None:
        try:
            report["supervisor_signalled"] = bool(
                supervisor.request_cancel_by_activity(activity_id))
        except Exception:
            logger.debug("[ChatTaskRegistry] supervisor cancel failed", exc_info=True)

    withdrawn, note = _withdraw_gate(vault, row, queue)
    report["gate_withdrawn"] = withdrawn
    report["gate_note"] = note

    try:
        vault.update_chat_history_entry(ts, {
            "status": "cancelled",
            "summary": _CANCEL_SUMMARY,
            "cancelled_by": "operator",
        })
    except Exception as exc:
        report["reason"] = f"could not write the cancelled state: {exc}"
        return report

    report["stopped"] = True
    logger.info("[ChatTaskRegistry] stopped chat task ts=%s (live=%s, gate_withdrawn=%s)",
                ts, report["live"], report["gate_withdrawn"])
    return report
