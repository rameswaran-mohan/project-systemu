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
from typing import Any, Callable

logger = logging.getLogger(__name__)


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


def stateful_expansion(header: str, *, state_key: Any, open_state: dict) -> Any:
    """A ``ui.expansion`` that survives ``@ui.refreshable`` repaints.

    The open/closed state lives in the caller-owned ``open_state`` dict keyed
    by ``state_key`` (use ``event_ui_key`` for event rows); each rebuild
    restores it instead of resetting to closed.
    """
    from nicegui import ui

    return ui.expansion(
        header,
        value=bool(open_state.get(state_key, False)),
        on_value_change=lambda e: record_open_state(open_state, state_key, e),
    )


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
