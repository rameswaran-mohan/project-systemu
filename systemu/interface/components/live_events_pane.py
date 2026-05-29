"""Supervisor live-events pane (v0.8.8 console).

Subscribes to the in-process EventBus and renders the last N events in a
fixed-height auto-scrolling pane. Unsubscribes on client disconnect to avoid
callback leaks across page loads.
"""
from __future__ import annotations

from typing import Any, Dict, List

from nicegui import ui

from systemu.interface.dashboard_state import THEME

_MAX_EVENTS = 50


def _append_capped(buf: List[Dict[str, Any]], event: Dict[str, Any], max_len: int = _MAX_EVENTS) -> None:
    """Append event, keeping only the most recent max_len entries (ring buffer)."""
    buf.append(event)
    if len(buf) > max_len:
        del buf[: len(buf) - max_len]


def _level_color(level: str) -> str:
    """Map an event level to a THEME color."""
    return {
        "ERROR":   THEME["danger"],
        "WARNING": THEME["warning"],
        "SUCCESS": THEME["success"],
    }.get((level or "").upper(), THEME["text_muted"])


def build_supervisor_events_pane(height_px: int = 320) -> None:
    """Render the live Supervisor/EventBus stream, auto-scrolling."""
    from systemu.interface.event_bus import EventBus

    events: List[Dict[str, Any]] = []

    @ui.refreshable
    def _pane():
        if not events:
            ui.label("Waiting for live events…").style(
                f"color: {THEME['text_muted']}; font-size: 12px;"
            )
            return
        for ev in events:
            level = (ev.get("level") or "INFO").upper()
            with ui.row().style("gap: 8px; align-items: baseline; padding: 2px 0;"):
                ui.label(f"[{level}]").style(
                    f"color: {_level_color(level)}; font-size: 11px; "
                    f"font-weight: 700; min-width: 70px;"
                )
                ui.label(str(ev.get("message", ""))[:200]).style(
                    f"color: {THEME['text']}; font-size: 12px;"
                )

    with ui.scroll_area().style(f"height: {height_px}px; width: 100%;"):
        _pane()

    def _on_event(event: Dict[str, Any]) -> None:
        _append_capped(events, event)
        try:
            _pane.refresh()
        except Exception:
            pass  # client may have disconnected

    # Subscribe with replay so the pane shows recent history immediately.
    unsubscribe = EventBus.get().subscribe(_on_event, replay=True)

    # Unsubscribe when the client disconnects to avoid leaking callbacks.
    try:
        from nicegui import app
        app.on_disconnect(lambda: unsubscribe())
    except Exception:
        pass
