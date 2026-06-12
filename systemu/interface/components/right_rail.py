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


def live_event_row(event: Dict[str, Any]) -> Dict[str, Any]:
    """Pure row model for one Live-pane event (W5.3).

    Every event becomes a header row — timestamp + event name — and the model
    decides what sits behind the expand arrow:

      * ``title``       — event["message"], falling back to context.title
                          (operator_decision_* events used to render as a
                          blank "[INFO] " line because they carry no message).
      * ``time``        — HH:MM:SS from the event ts.
      * ``level``       — for the level tint.
      * ``details``     — the expand-arrow payload (reasoning / tool params /
                          tool result / LLM ref / outcome summary / artifacts).
      * ``decision_id`` — set for pending-decision events so the row can offer
                          an inline Answer action.
    """
    from systemu.interface.components.live_events_pane import (
        _format_event_time, _has_details)

    ctx = event.get("context") or {}
    title = str(event.get("message") or "").strip()
    if not title:
        title = str(ctx.get("title") or event.get("category") or "(event)")
    decision_id = (
        ctx.get("decision_id")
        if event.get("category") == "operator_decision_posted" else None
    )
    return {
        "time": _format_event_time(event.get("ts")),
        "level": str(event.get("level") or "INFO").upper(),
        "title": title[:200],
        "details": (event.get("details") or {}) if _has_details(event) else {},
        "decision_id": decision_id,
        "has_details": _has_details(event) or bool(decision_id),
    }


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
    from systemu.interface.ui_helpers import (
        RepaintGate, event_ui_key, prune_open_state, safe_timer,
        stateful_expansion)

    # Thread-safe ring buffer (deque.append is atomic under the GIL).
    events: "deque[Dict[str, Any]]" = deque(maxlen=_MAX_LIVE_EVENTS)

    # W11.1: expansion open/closed state survives repaints; repaints happen
    # only when content changed (see live_events_pane — same field bug).
    open_state: Dict[int, bool] = {}
    gate = RepaintGate()

    # W5.3: stable-slot host for the inline Answer dialog — creating a dialog
    # from inside the timer-refreshed _pane races slot disposal (see
    # attention.make_answer_host).
    from systemu.interface.components.attention import make_answer_host
    _answer_host = make_answer_host()

    def _visible() -> List[Dict[str, Any]]:
        snapshot = list(events)
        if stream_ref:
            snapshot = events_for_stream(snapshot, stream_ref)
        return list(reversed(snapshot))  # newest-first

    def _render_row(ev: Dict[str, Any]) -> None:
        """W5.3: one event = one header row (time + name), with an expand
        arrow when there's a payload behind it (reasoning / tool output /
        outcome) and an inline Answer action for pending-decision events."""
        from systemu.interface.components.live_events_pane import (
            render_event_details_body)

        row = live_event_row(ev)
        header = (f"{row['time']}  " if row["time"] else "") + row["title"]

        if not row["has_details"]:
            ui.label(header).style("font-size: 12px; font-family: monospace;")
            return

        with stateful_expansion(
            header, state_key=event_ui_key(ev), open_state=open_state,
        ).classes("w-full").style(
            "font-size: 12px;"
        ):
            if row["details"]:
                render_event_details_body(row["details"])
            if row["decision_id"]:
                def _answer(_=None, did=row["decision_id"]):
                    from systemu.interface.dashboard_state import AppState
                    from systemu.interface.components.attention import (
                        open_answer_dialog)
                    try:
                        vault = AppState.get().vault
                    except Exception:
                        ui.notify("Vault unavailable.", type="warning")
                        return
                    open_answer_dialog(did, vault, on_resolved=_pane.refresh,
                                       host=_answer_host)

                from systemu.interface.design.primitives import button as _btn
                _btn("Answer", variant="primary", on_click=_answer)

    @ui.refreshable
    def _pane() -> None:
        prune_open_state(open_state, (event_ui_key(e) for e in events))
        rows = _visible()
        if not rows:
            ui.label("Waiting for streamed run events…").style("font-size: 12px;")
            return
        for ev in rows:
            _render_row(ev)

    scroll_style = f"height: {height_px}px; width: 100%;"
    with ui.scroll_area().style(scroll_style):
        _pane()

    def _on_event(event: Dict[str, Any]) -> None:
        # Publish-thread callback: ONLY append + mark dirty. NEVER refresh
        # here (liveness).
        events.append(event)
        gate.bump()

    # Subscribe with replay so the pane shows recent history immediately.
    unsubscribe = EventBus.get().subscribe(_on_event, replay=True)

    # UI-thread timer is the SOLE driver of refresh (slot-error tolerant).
    # W11.1: change-gated — an unconditional refresh rebuilt every expansion
    # collapsed twice a second, so the expand arrow could never stay open.
    def _tick() -> None:
        if gate.should_paint():
            _pane.refresh()

    safe_timer(0.5, _tick)

    # Detach the subscriber when the client is DELETED (not on disconnect —
    # W7.2: app.on_disconnect is global, so any client's transient drop
    # killed this pane's subscription; see live_events_pane).
    try:
        ui.context.client.on_delete(unsubscribe)
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
