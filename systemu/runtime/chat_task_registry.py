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

# ── D-5 (e2e v0.10.31): why a chat task failed, and what to do about it ───────
#
# WITNESSED: a FAILED card carried the prompt, the timestamp, a red badge and a
# restart button. The reason ("the tiers point at providers with no key") lived
# only in the COLLAPSED Live-events pane, so the one affordance the card DID
# offer -- restart -- was the one action guaranteed to fail again.
#
# Both spellings occur: ``direct_task`` writes "failed" for an extraction /
# decision crash, a ShadowRuntime result status is "failure".
FAILED_CHAT_STATUSES = frozenset({"failed", "failure"})

FAILURE_STEP_PROVIDER = "Set a provider key in Settings."
FAILURE_STEP_TOOL = "Enable the tool in the Tools Registry."
FAILURE_STEP_UNKNOWN = "See Live events for details."

# Matched against the LOWERCASED reason, provider first: an LLM-layer failure
# routinely mentions a tool (the schema it was serializing), while a tool
# failure does not mention a tier or a key. Deliberately NARROW -- an
# unrecognized reason gets the honest pointer, never a guessed remedy.
_PROVIDER_MARKERS = ("provider", "api key", "api_key", "no key", "tier=",
                     "llm call failed")
_TOOL_MARKERS = ("tool", "not enabled", "tools registry")

# The row fields that name this task to an event. ``ts`` is the chat submission
# id the lanes stamp as ``chat_submission_id``.
_TASK_ID_KEYS = ("ts", "execution_id", "activity_id", "shadow_id",
                 "submission_id", "scroll_id")


def _task_ids(entry: Dict[str, Any]) -> set:
    """Every id by which an event could name this chat task. Concrete types are
    pinned before use (DEC-36) -- these values came off disk."""
    out = set()
    for key in _TASK_ID_KEYS:
        value = entry.get(key)
        if type(value) is str and value:
            out.add(value)
    return out


def _event_names_task(event: Any, ids: set) -> bool:
    if not isinstance(event, dict):
        return False
    context = event.get("context")
    if not isinstance(context, dict):
        return False
    for value in context.values():
        if type(value) is str and value in ids:
            return True
    return False


def failure_reason(entry: Dict[str, Any], *, events: Any = None) -> str:
    """The recorded reason this chat task FAILED, or "" when none was recorded.

    Two sources, terminal record FIRST:

      * the row's own ``error`` -- every lane that fails a chat task writes it
        (``direct_task``, ``activity_completion``, ``subagent_fleet``);
      * failing that, the last ERROR-level event that NAMES this task, which is
        exactly what the operator had to open the Live-events pane to read.

    An event is only borrowed when one of its context values IS one of this
    row's ids: "the newest ERROR on screen" would happily attribute a stranger's
    failure to this card. ``events`` defaults to the in-process EventBus ring
    buffer (the pane's own source) and is the test seam.

    Never raises: an unreadable row or event stream reports NO reason, which the
    card renders as the Live-events pointer. It never invents one.
    """
    if not isinstance(entry, dict):
        return ""
    recorded = entry.get("error")
    if type(recorded) is str and recorded.strip():
        return recorded.strip()
    if events is None:
        try:
            from systemu.interface.event_bus import EventBus
            events = EventBus.get().get_buffer()
        except Exception:
            logger.debug("[ChatTaskRegistry] no event buffer to read", exc_info=True)
            return ""
    ids = _task_ids(entry)
    if not ids:
        return ""
    found = ""
    try:
        for event in list(events or []):
            if not _event_names_task(event, ids):
                continue
            level = event.get("level")
            if type(level) is not str or level.upper() != "ERROR":
                continue
            message = event.get("message")
            if type(message) is str and message.strip():
                found = message.strip()
    except Exception:
        logger.debug("[ChatTaskRegistry] unreadable event stream", exc_info=True)
        return ""
    return found


def failure_next_step(reason: Any) -> str:
    """The ONE next step a failure reason earns. ASCII; never empty.

    An unrecognized reason resolves to the Live-events pointer rather than to a
    plausible-sounding remedy: sending an operator to Settings for a failure
    Settings cannot fix is the same class of false statement this defect is."""
    text = reason.lower() if type(reason) is str else ""
    if any(marker in text for marker in _PROVIDER_MARKERS):
        return FAILURE_STEP_PROVIDER
    if any(marker in text for marker in _TOOL_MARKERS):
        return FAILURE_STEP_TOOL
    return FAILURE_STEP_UNKNOWN


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
    """Retire every pending decision card of the cancelled chat run.

    IMPL-11: routes through the ONE queue function every cancel path calls,
    ``OperatorDecisionQueue.expire_cards_for_run`` — by the run's coordinates
    (the row's ``ts`` IS the run's ``chat_submission_id``; its activity and
    execution) and the exact card the row says the run is parked on. Each card is
    expired with reason ``run cancelled``, so a late answer to it is refused.
    Never by dedup key: a gate key is params-independent and can name ANOTHER
    run's pending card. Returns ``(withdrawn, note)`` about the parked card;
    when it cannot be retired the note is what the UI shows beside it so the
    operator is not left with a live-looking gate for a run that no longer exists.
    """
    parked = entry.get("decision_id") or ""
    try:
        q = queue
        if q is None:
            from systemu.approval.decision_queue import OperatorDecisionQueue
            q = OperatorDecisionQueue(vault)
        expired = q.expire_cards_for_run(
            execution_id=entry.get("execution_id"),
            activity_id=entry.get("activity_id"),
            chat_submission_id=entry.get("ts"),
            decision_id=parked or None,
        )
    except Exception:
        logger.debug("[ChatTaskRegistry] card withdrawal failed", exc_info=True)
        expired = []
    if not (parked or entry.get("dedup_key")):
        return False, ""
    if parked and parked in expired:
        return True, ""
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
