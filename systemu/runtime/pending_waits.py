"""R-A12a: pure record model + helpers for durable retry timers.

A *pending wait* is a plain, JSON-round-trippable ``dict`` that represents a
deferred retry of an activity. Records live in
``ExecutionSnapshot.pending_waits`` (a ``list[dict]``) so they survive process
restarts; a later reconciler task fires the ones that are due.

This module is deliberately **pure**: the helpers never read the wall clock —
callers inject ``now`` so behaviour is deterministic and testable. A wall-clock
default is fine only at the *arm site* (the caller), never inside these
functions.

Design notes:

* ``wait_id`` is a stable dedupe key derived from the run + attempt, so
  re-arming the same attempt is idempotent (``arm_wait`` refuses duplicates and
  the store never accumulates twins of the same timer).
* Mutating helpers (``mark_dispatched``, ``expire_all``) return a **new** list
  of **new** dicts rather than mutating in place. Callers are expected to
  persist the returned list back onto the snapshot — returning fresh objects
  keeps the durable store's writer path explicit and avoids surprising a caller
  that still holds the old list.
"""

from __future__ import annotations

from typing import Any

# The only wait kind modelled today. Named so no bare "retry" literal leaks
# into call sites or records.
WAIT_KIND_RETRY = "retry"

# The initial dispatch state of a freshly-armed wait: it has not yet fired.
_UNDISPATCHED = False
_DISPATCHED = True


def _as_list(waits: Any) -> list[dict]:
    """Defensively coerce ``waits`` to a list.

    A ``None`` or non-list argument (e.g. a snapshot field that was never
    initialised) is treated as an empty collection rather than raising.
    """
    if isinstance(waits, list):
        return waits
    return []


def make_retry_wait(
    *,
    execution_id: str,
    activity_id: str,
    shadow_id: str,
    root_execution_id: str,
    delay_s: float,
    attempt: int,
    max_attempts: int,
    now: float,
) -> dict:
    """Build a durable retry-wait record.

    ``wait_id`` is ``"{execution_id}:{WAIT_KIND_RETRY}:{attempt}"`` — a stable
    dedupe key: the same run + attempt always produces the same id, so
    re-arming an already-armed attempt is idempotent.

    ``fire_at`` is ``now + delay_s``. All fields are plain
    ``str``/``float``/``int``/``bool`` so the record is JSON-round-trippable.
    """
    wait_id = f"{execution_id}:{WAIT_KIND_RETRY}:{attempt}"
    return {
        "wait_id": wait_id,
        "wait_kind": WAIT_KIND_RETRY,
        "execution_id": execution_id,
        "activity_id": activity_id,
        "shadow_id": shadow_id,
        "root_execution_id": root_execution_id,
        "fire_at": float(now) + float(delay_s),
        "attempt": int(attempt),
        "max_attempts": int(max_attempts),
        "dispatched": _UNDISPATCHED,
        "created_at": float(now),
    }


def due_waits(waits: Any, *, now: float) -> list[dict]:
    """Return the waits that are undispatched **and** due (``fire_at <= now``).

    Order is preserved from the input list.
    """
    return [
        w
        for w in _as_list(waits)
        if not w.get("dispatched", False) and w.get("fire_at", float("inf")) <= now
    ]


def mark_dispatched(waits: Any, wait_id: str) -> list[dict]:
    """Return a NEW list with the wait matching ``wait_id`` flagged dispatched.

    The input list and its dicts are not mutated; the matching record is
    replaced by a copy with ``dispatched=True``. Callers must persist the
    returned list.
    """
    out: list[dict] = []
    for w in _as_list(waits):
        if w.get("wait_id") == wait_id:
            updated = dict(w)
            updated["dispatched"] = _DISPATCHED
            out.append(updated)
        else:
            out.append(w)
    return out


def arm_wait(context: Any, record: dict) -> None:
    """Append ``record`` to ``context._pending_waits``, deduped by ``wait_id``.

    Ensures ``context._pending_waits`` exists as a list, then appends only if no
    existing wait already carries the same ``wait_id``. Re-arming the same
    attempt is therefore a no-op.
    """
    existing = getattr(context, "_pending_waits", None)
    if not isinstance(existing, list):
        existing = []
        context._pending_waits = existing
    wait_id = record.get("wait_id")
    if any(w.get("wait_id") == wait_id for w in existing):
        return
    existing.append(record)


def expire_all(waits: Any) -> list[dict]:
    """Return a NEW list with every wait flagged dispatched (for cancellation).

    Used when an activity is cancelled/superseded: mark all its timers so the
    reconciler ignores them. Inputs are not mutated.
    """
    out: list[dict] = []
    for w in _as_list(waits):
        updated = dict(w)
        updated["dispatched"] = _DISPATCHED
        out.append(updated)
    return out


def is_exhausted(wait: dict) -> bool:
    """True when this wait has used all its attempts (``attempt >= max_attempts``)."""
    return wait.get("attempt", 0) >= wait.get("max_attempts", 0)
