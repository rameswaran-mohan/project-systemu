"""v0.8.19 (R2) — live objective checklist pane, mirroring live_events_pane."""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List

from nicegui import ui

from systemu.interface.dashboard_state import THEME

_MAX = 50
_GLYPH = {"done": "☑", "in_progress": "▣", "pending": "☐"}


def _latest_objective_items(events) -> List[Dict[str, Any]]:
    """Reduce a list of events to the most-recent objective_state items (or [])."""
    for ev in reversed(list(events)):
        if ev.get("category") == "objective_state":
            return ev.get("context", {}).get("items", []) or []
    return []


def build_live_objectives_pane(height_px: int = 220) -> None:
    """Render a live objective checklist driven by objective_state EventBus events."""
    from systemu.interface.event_bus import EventBus
    events: "deque[Dict[str, Any]]" = deque(maxlen=_MAX)

    @ui.refreshable
    def _pane():
        items = _latest_objective_items(events)
        if not items:
            ui.label("No active objectives.").style(
                f"color: {THEME['text_muted']}; font-size: 12px;")
            return
        for it in items:
            with ui.row().style("gap: 8px; align-items: baseline; padding: 2px 0;"):
                ui.label(_GLYPH.get(it.get("status"), "☐")).style("font-size: 13px;")
                ui.label(str(it.get("goal", ""))[:200]).style(
                    f"color: {THEME['text']}; font-size: 12px;")

    with ui.scroll_area().style(f"height: {height_px}px; width: 100%;"):
        _pane()

    def _on_event(ev: Dict[str, Any]) -> None:
        if ev.get("category") == "objective_state":
            events.append(ev)

    unsubscribe = EventBus.get().subscribe(_on_event, replay=True)

    def _tick():
        try:
            _pane.refresh()
        except Exception:
            pass

    ui.timer(0.5, _tick)
    # W7.2: per-client on_delete, NOT the global app.on_disconnect (any
    # client's transient drop killed this subscription — see live_events_pane).
    try:
        ui.context.client.on_delete(unsubscribe)
    except Exception:
        pass
