"""v0.9.32 (item 3, D3.2) — process-global cancel-token registry for the chat lane.

The chat lane (quick + sync ``run_now``) runs on an untracked daemon thread with
no Supervisor slot, so it has no ``cancel_event``. This module is the chat-lane
analogue of ``Supervisor._running[key]["cancel_event"]``: a process-global map
from a chat-history timestamp id to a ``threading.Event`` that the lane checks at
its loop boundary and the dashboard Stop button sets.

Per-process only (a docker multi-process cancel is documented out-of-scope).
"""
from __future__ import annotations

import logging
import threading
from typing import Dict

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_EVENTS: Dict[str, threading.Event] = {}


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
