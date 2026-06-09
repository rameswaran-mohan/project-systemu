"""Right-rail helpers (Phase 2 seed).

The full persistent right-rail UI lands in Phase 4.  For now this module
exposes the pure event-filter the rail will use to follow ONE streamed run by
its ``stream_ref`` (the Job.id a ``dispatch(stream=True)`` returns on its
CommandResult), plus a minimal ``live_runs_pane`` NiceGUI component that
follows it.  The pure logic (filter + line formatter) is UI-free so it is
trivially unit-testable; the NiceGUI wrapper is a thin shell.

This component is deliberately NOT wired into the persistent IA shell /
``_build_layout`` — that integration lands in Phase 4.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List

_MAX_LIVE_EVENTS = 50


def events_for_stream(
    events: List[Dict[str, Any]], stream_ref: str,
) -> List[Dict[str, Any]]:
    """Return only the EventBus events tagged with stream_ref (rail follows one run)."""
    return [e for e in events if e.get("stream_ref") == stream_ref]


def format_live_run_line(event: Dict[str, Any]) -> str:
    """Pure one-line formatter for a streamed event in the Live pane.

    ``[LEVEL] message`` (message truncated). Kept pure so the rendering logic
    is unit-testable independently of NiceGUI.
    """
    level = str(event.get("level") or "INFO").upper()
    message = str(event.get("message", ""))[:200]
    return f"[{level}] {message}"


def live_runs_pane(stream_ref: str = "", *, height_px: int = 280) -> None:
    """Minimal Live pane: mirror recent streamed EventBus events, newest-first.

    When ``stream_ref`` is given, only events tagged with that ref are shown
    (the rail follows one specific run); otherwise all streamed events appear.

    Mirrors the ``live_events_pane`` liveness contract:
      • ``_on_event`` runs on the publish thread and ONLY appends to a deque —
        it never calls ``refresh()`` (refreshing off the UI thread is a bug).
      • A UI-thread ``safe_timer`` is the sole driver of ``_pane.refresh()``.
      • Unsubscribes from the EventBus + cancels on client disconnect.
    """
    from nicegui import ui, app

    from systemu.interface.event_bus import EventBus
    from systemu.interface.ui_helpers import safe_timer

    # Thread-safe ring buffer (deque.append is atomic under the GIL).
    events: "deque[Dict[str, Any]]" = deque(maxlen=_MAX_LIVE_EVENTS)

    def _visible() -> List[Dict[str, Any]]:
        snapshot = list(events)
        if stream_ref:
            snapshot = events_for_stream(snapshot, stream_ref)
        return list(reversed(snapshot))  # newest-first

    @ui.refreshable
    def _pane() -> None:
        rows = _visible()
        if not rows:
            ui.label("Waiting for streamed run events…").style("font-size: 12px;")
            return
        for ev in rows:
            ui.label(format_live_run_line(ev)).style(
                "font-size: 12px; font-family: monospace;"
            )

    scroll_style = f"height: {height_px}px; width: 100%;"
    with ui.scroll_area().style(scroll_style):
        _pane()

    def _on_event(event: Dict[str, Any]) -> None:
        # Publish-thread callback: ONLY append. NEVER refresh here (liveness).
        events.append(event)

    # Subscribe with replay so the pane shows recent history immediately.
    unsubscribe = EventBus.get().subscribe(_on_event, replay=True)

    # UI-thread timer is the SOLE driver of refresh (slot-error tolerant).
    safe_timer(0.5, _pane.refresh)

    # Detach the subscriber when the client disconnects to avoid leaks.
    try:
        app.on_disconnect(lambda: unsubscribe())
    except Exception:
        pass


def right_rail_section_titles() -> List[str]:
    """The persistent right rail's section order (spec §4.2): the 'Needs you'
    inbox glance sits ABOVE the 'Live' streamed-runs pane.  Pure so the
    composition order is unit-testable independently of NiceGUI.
    """
    return ["Needs you", "Live"]


def render_right_rail(vault, stream_ref: str = "") -> None:
    """Compose the persistent right rail: the 'Needs you' inbox glance section
    above the 'Live' streamed-runs pane.

    A thin shell over the two proven panes.  ``build_inbox_rail_section``
    emits its own ``s-section-head`` 'Needs you' header + glance rows;
    ``live_runs_pane`` emits no header, so we add the 'Live' header before it.
    The visible order is the pure ``right_rail_section_titles()``.
    """
    from nicegui import ui

    from systemu.interface.components.inbox_rail import build_inbox_rail_section

    # "Needs you" — pending gate descriptors (emits its own header + rows).
    build_inbox_rail_section(vault, stream_ref=stream_ref)

    # "Live" — recent streamed run events (no header of its own).
    ui.label("Live").classes("s-section-head").style("margin-top: 16px;")
    live_runs_pane(stream_ref=stream_ref)
