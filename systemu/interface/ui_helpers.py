"""Shared NiceGUI dashboard helpers.

`safe_timer` wraps a `ui.timer(...)` so its callback silently no-ops when
the parent slot has been deleted (i.e. the user navigated away from the
page that created the timer).  Without this wrapper, every periodic
refresh timer in the dashboard floods the daemon log with
``RuntimeError: The parent slot of the element has been deleted.`` after
the first navigation.

Usage — drop-in replacement for ui.timer:

    from systemu.interface.ui_helpers import safe_timer

    safe_timer(0.5, _drain_events)
    safe_timer(2.0, _log_table.refresh)

The signature matches `ui.timer(interval, callback, *, active=True, once=False)`.

This is a presentation-layer concern only — the underlying work the
callback would have done (event drain, refresh) wasn't needed anyway
once the slot is gone, so swallowing the error is the correct
semantics.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# -- Timestamp rendering: ONE convention for every dashboard surface ---------
#
# STORAGE CONVENTION (verified across the codebase): every timestamp that
# reaches a dashboard surface is UTC.
#   * systemu.interface.event_bus stamps an event's ``ts`` with
#     ``datetime.now(timezone.utc).isoformat()``          -> tz-AWARE UTC.
#   * systemu.runtime.workflow_tracker._now does the same for ``started_at`` /
#     ``updated_at`` / timeline entries.                  -> tz-AWARE UTC.
#   * systemu.core.utils.utcnow returns a NAIVE datetime that holds UTC, so
#     anything serialising it yields a naive ISO string.  -> NAIVE, means UTC.
#
# Hence the explicit naive rule: A NAIVE TIMESTAMP IS ASSUMED TO BE UTC.
# Reading a naive stamp as local time would leave it unconverted, which is
# exactly the defect these helpers exist to prevent.
#
# DISPLAY CONVENTION: the audience is a desk operator, not a server admin, so
# every surface renders the operator's LOCAL wall-clock time. Before v0.10.24
# the Home right rail, the live-events pane, the Work card and the workflow
# detail page printed the stored UTC digits verbatim while the Chat live feed
# converted -- the same event read 20:16:56 on one page and 01:46:56 on
# another for an operator in Asia/Kolkata.
#
# ``tz`` is a TEST SEAM: production always passes None (the system local
# zone); tests pin a named zone so they assert the real conversion instead of
# re-deriving it from whatever zone the test host sits in.


def to_local_datetime(ts: Any, *, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """Coerce a stored timestamp to an AWARE datetime in the operator's zone.

    Accepts an ISO string (aware, ``Z``-suffixed, or naive), epoch seconds
    (int/float), or a ``datetime``. A naive input is treated as UTC -- see the
    storage convention above. Returns None when the value is missing or
    unparseable; never raises.
    """
    if ts is None:
        return None
    try:
        if isinstance(ts, datetime):
            dt = ts
        elif isinstance(ts, bool):          # bool is an int subclass -- reject
            return None
        elif isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        else:
            s = str(ts).strip()
            if not s:
                return None
            if s[-1] in ("Z", "z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz) if tz is not None else dt.astimezone()
    except Exception:
        return None


def format_event_time(ts: Any, *, tz: Optional[tzinfo] = None) -> str:
    """``HH:MM:SS`` in the operator's LOCAL zone; ``''`` if missing/unparseable.

    The one formatter for event-feed clock stamps (live-events pane, Home
    right rail, Notifications page, Chat live feed).
    """
    dt = to_local_datetime(ts, tz=tz)
    return dt.strftime("%H:%M:%S") if dt is not None else ""


def format_stamp(ts: Any, *, tz: Optional[tzinfo] = None) -> str:
    """``YYYY-MM-DD HH:MM:SS`` in the operator's LOCAL zone; ``''`` if unusable.

    The one formatter for date-carrying stamps (Work page workflow cards,
    workflow detail stats + timeline). The date rolls with the clock: an event
    stored at 20:16 UTC on the 10th reads 01:46 on the 11th in Asia/Kolkata.
    """
    dt = to_local_datetime(ts, tz=tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else ""


def safe_timer(
    interval: float,
    callback: Callable[..., Any],
    *,
    active: bool = True,
    once: bool = False,
) -> Any:
    """Create a ``ui.timer`` whose callback is wrapped in slot-error tolerance.

    Returns the underlying NiceGUI timer so callers can deactivate it.
    """
    from nicegui import ui

    def _wrapped() -> None:
        try:
            callback()
        except RuntimeError as exc:
            # NiceGUI raises this when the timer fires after its parent
            # slot has been disposed (page navigated away, websocket
            # closed, etc.).  Silently drop the tick.
            if "slot" in str(exc).lower() or "deleted" in str(exc).lower():
                logger.debug("[safe_timer] dropping post-disposal tick: %s", exc)
                return
            raise

    return ui.timer(interval, _wrapped, active=active, once=once)


# ── W11.1: expansion state vs timer-driven repaints ─────────────────────────
# Timer-refreshed @ui.refreshable panes destroy and rebuild their widgets on
# every repaint; a ui.expansion(value=False) rebuilt 2×/second can never stay
# open (the field-reported "arrow not working").  Three pieces fix the class
# of bug:
#   * event_ui_key       — stable identity for an event dict, so state can
#                          follow the SAME event across repaints.
#   * RepaintGate        — repaint only when content actually changed; an
#                          idle pane stops re-rendering (and stops destroying
#                          interaction state) entirely.
#   * stateful_expansion — ui.expansion whose open/closed state is recorded
#                          in a caller-owned dict and restored on rebuild.

_EVENT_UI_KEY = "_ui_key"
_event_ui_counter = itertools.count(1)


def event_ui_key(event: dict) -> int:
    """Stable per-process identity for an EventBus event dict.

    Stamped once into the dict and reused on every later call — idempotent,
    so panes sharing the same dict (EventBus hands one object to every
    subscriber) agree on identity. Rendering happens on the UI thread, so
    stamping is single-threaded.
    """
    key = event.get(_EVENT_UI_KEY)
    if key is None:
        key = next(_event_ui_counter)
        event[_EVENT_UI_KEY] = key
    return key


class RepaintGate:
    """Repaint a timer-driven ``@ui.refreshable`` only when marked dirty.

    Publish threads call ``bump()`` (a cheap int increment — atomic enough
    under the GIL; the worst race costs one extra tick's delay); the
    UI-thread tick calls ``should_paint()`` and refreshes only on True.
    A fresh gate always paints its first tick so replayed history shows
    immediately.
    """

    def __init__(self) -> None:
        self._rev = 0
        self._painted = -1

    def bump(self) -> None:
        self._rev += 1

    def should_paint(self) -> bool:
        if self._rev == self._painted:
            return False
        self._painted = self._rev
        return True


def background_activity_count() -> int:
    """Best-effort count of running background work (jobs + executions).

    W13.1: drives the small Live-header spinner so the operator always has
    a fingertip indication that something is still running. Never raises.
    """
    n = 0
    try:
        from systemu.interface.jobs import JobManager
        n += len(JobManager.get().get_active_jobs())
    except Exception:
        pass
    try:
        from systemu.runtime.supervisor import Supervisor
        n += int(Supervisor.get().get_status().get("running_count", 0) or 0)
    except Exception:
        pass
    try:
        # v0.9.37: the chat lane (quick-answer + run_now) runs on an untracked
        # daemon thread, NOT a Supervisor slot — count its in-flight tasks too so
        # the Live busy dots fire for chat tasks, not just capture→execute runs.
        from systemu.runtime import chat_task_registry
        n += chat_task_registry.active_count()
    except Exception:
        pass
    return n


def gated_refresh(fingerprint_fn: Callable[[], Any],
                  refresh_fn: Callable[[], Any]) -> Callable[[], None]:
    """A timer tick that repaints ONLY when the data actually changed (W12).

    Unconditional timer repaints destroy and rebuild every widget in a
    ``@ui.refreshable`` — a click racing the repaint is silently dropped
    (buttons "do nothing"), and open expansions/dialogs die (the W11.1 /
    W5.4 bug class). Wrap the tick so the refreshable only rebuilds when its
    underlying model changed:

        safe_timer(2.0, gated_refresh(lambda: json.dumps(model()), _pane.refresh))

    A fingerprint error repaints once (fail-open: liveness beats stability).
    """
    sentinel = object()
    state = {"fp": sentinel}

    def _tick() -> None:
        try:
            fp = fingerprint_fn()
        except Exception:
            fp = None
        if fp != state["fp"]:
            state["fp"] = fp
            refresh_fn()

    return _tick


def record_open_state(open_state: dict, key: Any, value_or_args: Any) -> None:
    """Record an expansion's open/closed state (pure; unit-testable).

    Accepts either NiceGUI's ValueChangeEventArguments (``.value``) or a raw
    bool, so it wires straight into ``on_value_change``.
    """
    open_state[key] = bool(getattr(value_or_args, "value", value_or_args))


def prune_open_state(open_state: dict, live_keys) -> None:
    """Drop state for events that left the ring buffer (bounded memory)."""
    live = set(live_keys)
    for k in [k for k in open_state if k not in live]:
        open_state.pop(k, None)


def stateful_expansion(header: str, *, state_key: Any, open_state: dict,
                       read_state=None) -> Any:
    """A ``ui.expansion`` that survives ``@ui.refreshable`` repaints.

    The open/closed state lives in the caller-owned ``open_state`` dict keyed
    by ``state_key`` (use ``event_ui_key`` for event rows); each rebuild
    restores it instead of resetting to closed.

    v0.9.42: when ``read_state`` (an unread-tracking dict keyed like
    ``open_state``) is supplied, the row gains the expand chevron on the LEFT
    (Quasar ``switch-toggle-side``) plus a small leading unread dot — a solid
    accent dot when the entry is new, hollowing to an empty ring once the
    operator expands or clicks it. Read-state is in-memory (per session). The
    returned element is the expansion, so ``with stateful_expansion(...):``
    still populates the body normally.
    """
    from nicegui import ui

    if read_state is None:
        return ui.expansion(
            header,
            value=bool(open_state.get(state_key, False)),
            on_value_change=lambda e: record_open_state(open_state, state_key, e),
        )

    _DOT = ("width: 8px; height: 8px; border-radius: 50%; margin-top: 7px; "
            "flex: 0 0 auto; cursor: pointer; ")
    _UNREAD = _DOT + "background: color-mix(in srgb, var(--color-accent), #000 22%);"
    _READ = _DOT + "background: transparent; border: 1.5px solid var(--color-border);"

    with ui.row().classes("w-full items-start no-wrap").style("gap: 6px;"):
        dot = ui.element("div").style(_READ if read_state.get(state_key) else _UNREAD)

        def _mark_read(_=None) -> None:
            if not read_state.get(state_key):
                read_state[state_key] = True
                dot.style(replace=_READ)

        exp = ui.expansion(
            header,
            value=bool(open_state.get(state_key, False)),
            on_value_change=lambda e: (
                record_open_state(open_state, state_key, e), _mark_read()),
        ).props("switch-toggle-side").style("flex: 1 1 auto; min-width: 0;")
        dot.on("click", _mark_read)
    return exp


def render_floor_pierce_banner() -> None:
    """Persistent warn banner when the gate policy pierces the safety floor
    (W2.4) — rendered on the Inbox and the Settings gate-mode card.

    The escape hatches (no_floor, override→allow on a floor type) are
    deliberate operator tools, but they must never be invisible. Best-effort:
    an unreadable policy renders nothing rather than breaking the page.
    """
    from nicegui import ui

    try:
        from systemu.interface.command.gate_mode import (
            floor_pierces,
            load_default_policy,
        )
        pierces = floor_pierces(load_default_policy())
    except Exception:
        return
    if not pierces:
        return
    with ui.element("div").classes("s-banner s-banner--warn").style("margin: 4px 0 12px;"):
        ui.icon("warning")
        ui.label(
            "Safety floor pierced: " + "; ".join(pierces) +
            ". Floor gates (dep installs, destructive recovery) can now "
            "auto-grant — review in Settings."
        )
