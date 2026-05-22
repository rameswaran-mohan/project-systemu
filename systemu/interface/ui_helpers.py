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
