"""Supervisor live-events pane (v0.8.16 — origin-partitioned, live).

Subscribes to the in-process EventBus and renders recent events in a
fixed-height auto-scrolling pane, filtered by trigger `origin`.

v0.8.16 liveness contract (CRITICAL):
  • The EventBus callback (`_on_event`) runs on the *publish thread* and ONLY
    appends to a thread-safe deque — it MUST NOT call `_pane.refresh()` (doing
    so from a non-UI thread is the liveness bug this release fixes).
  • A `ui.timer` on the *UI thread* is the sole driver of `_pane.refresh()`.

Each pane declares which `origins` it shows ({"chat"} for the Supervisor pane,
{"capture","manual","scheduled"} for Manual Logs). A muted "Show system" switch
folds the `system` origin in/out. Unsubscribes on client disconnect.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional

from nicegui import ui

from systemu.interface.dashboard_state import THEME

_MAX_EVENTS = 50


def _append_capped(buf: List[Dict[str, Any]], event: Dict[str, Any], max_len: int = _MAX_EVENTS) -> None:
    """Append event to a list, keeping only the most recent max_len entries.

    Retained for back-compat (the live pane now uses a bounded ``deque``).
    """
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


def _format_event_time(ts) -> str:
    """Return HH:MM:SS from an ISO string, epoch float/int, or '' if missing/unparseable."""
    from datetime import datetime, timezone
    if not ts:
        return ""
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s).strftime("%H:%M:%S")
    except Exception:
        return ""


def _display_order(buf) -> list:
    """Return a new list with newest-first ordering (input is oldest-first)."""
    return list(reversed(buf))


def _passes_origin_filter(event, origins, *, show_system: bool) -> bool:
    """Pure predicate — does this event belong in a pane showing `origins`?

    A missing/empty origin is treated as ``"manual"`` (the coerce_origin
    default). The ``system`` origin is hidden unless ``show_system`` is on,
    regardless of which origins the pane otherwise shows.
    """
    o = event.get("origin") or "manual"
    if o == "system":
        return show_system
    return o in origins


def _has_details(event) -> bool:
    """Pure predicate — does this event carry a non-empty ``details`` payload?

    True only when ``details`` exists AND at least one of its values is truthy
    (so an all-``None`` per-iteration detail dict still renders as a plain row).
    """
    d = event.get("details")
    return bool(isinstance(d, dict) and any(d.values()))


def _load_llm_text(vault_root, llm_ref) -> str:
    """Lazily read the raw LLM ``response`` for an event's ``llm_ref``.

    ``llm_ref`` is ``{"exec_id", "call_index"}`` (or None). Reads the per-
    execution transcript via ``read_call``. NEVER raises — on a missing ref,
    vault root, or transcript entry, returns a safe human-readable string.
    """
    if not llm_ref or not vault_root:
        return "(no LLM transcript for this event)"
    try:
        from systemu.runtime.llm_transcript import read_call
        entry = read_call(vault_root, llm_ref.get("exec_id"), llm_ref.get("call_index"))
        if not entry:
            return "(no LLM transcript for this event)"
        return str(entry.get("response") or "")
    except Exception:
        return "(no LLM transcript for this event)"


def render_event_details_body(details: Dict[str, Any], *,
                              context: Optional[Dict[str, Any]] = None) -> None:
    """Render an event's ``details`` payload (reasoning, tool params/result,
    lazy LLM transcript) — the body of the expand-arrow row.

    Module-level + surface-agnostic (BUG-2 fix): shared by this pane (the
    /insights Manual Logs feed) AND the /chat Live Events feed, so the
    expand-for-details affordance exists wherever events render.

    v0.9.51: when the event's ``context`` ties it back to an activity, a
    "Re-trigger" button lets the operator re-run that task in place — the
    recovery affordance for a failed/cancelled/stuck run they expanded to
    investigate (the event context carries ``activity_id``).
    """
    import json as _json

    details = details or {}
    # W5.3: task-outcome payloads — the run's final summary + where produced
    # files landed. Rendered first: the outcome IS the headline. (Token
    # classes, not inline f-styles — the lint baseline holds at 0-new.)
    summary = details.get("summary")
    if summary:
        ui.label("Outcome").classes("s-field-label")
        ui.label(str(summary)).classes("s-cell").style("white-space: pre-wrap;")
    # W8.4: per-file artifacts when the run tracked them; the folder as
    # fallback context either way.
    files = details.get("files")
    output_dir = details.get("output_dir")
    if files or output_dir:
        ui.label("Artifacts").classes("s-field-label")
        for f in (files or []):
            ui.label(str(f)).classes("s-mono").style("white-space: pre-wrap;")
        if output_dir:
            ui.label(str(output_dir)).classes("s-mono").style("white-space: pre-wrap;")
    reasoning = details.get("reasoning")
    if reasoning:
        ui.label("Reasoning").style(
            f"color: {THEME['text_muted']}; font-size: 11px; font-weight: 700;"
        )
        ui.label(str(reasoning)).style(
            f"color: {THEME['text']}; font-size: 12px; white-space: pre-wrap;"
        )
    tool_params = details.get("tool_params")
    if tool_params is not None:
        ui.label("Tool params").style(
            f"color: {THEME['text_muted']}; font-size: 11px; font-weight: 700;"
        )
        try:
            _pp = _json.dumps(tool_params, indent=2, default=str)
        except Exception:
            _pp = str(tool_params)
        ui.code(_pp).style("font-size: 11px; width: 100%;")
    tool_result = details.get("tool_result")
    if tool_result is not None:
        ui.label("Tool result").style(
            f"color: {THEME['text_muted']}; font-size: 11px; font-weight: 700;"
        )
        try:
            _rr = _json.dumps(tool_result, indent=2, default=str)
        except Exception:
            _rr = str(tool_result)
        ui.code(_rr).style("font-size: 11px; width: 100%;")

    # v0.9.51 re-trigger affordance — when the event ties back to an activity,
    # offer an in-place re-run (the recovery path for a failed/cancelled/stuck task
    # the operator expanded to investigate). Quasar color/class props only (no new
    # inline f-style → the UI-style lint baseline holds at 0-new).
    _act_id = (context or {}).get("activity_id")
    if _act_id:
        def _retrigger(_=None, _aid=_act_id) -> None:
            from systemu.interface.pages.work import rerun_workflow_by_activity
            try:
                ui.notify(rerun_workflow_by_activity(_aid), type="positive")
            except Exception as _e:   # plain-language reason (no shadow / no activity)
                ui.notify(str(_e), type="warning")
        ui.button("↻ Re-trigger this task", on_click=_retrigger).props(
            "flat dense size=sm color=primary").classes("q-mt-xs")

    # Lazy raw-LLM transcript: only fetched when the button is clicked.
    # W5.3: only offered when the event actually carries an llm_ref —
    # outcome-only payloads (summary/output_dir) skip the dead button.
    llm_ref = details.get("llm_ref")
    if not llm_ref:
        return
    _llm_out = ui.label("").style(
        f"color: {THEME['text']}; font-size: 11px; white-space: pre-wrap; "
        f"font-family: monospace;"
    )

    def _show_llm() -> None:
        try:
            from systemu.interface.dashboard_state import AppState
            vault_root = AppState.get().vault.root
        except Exception:
            vault_root = None
        _llm_out.set_text(_load_llm_text(vault_root, llm_ref))

    ui.button("Show LLM response", on_click=_show_llm).props(
        "flat dense size=sm"
    ).style(f"color: {THEME['text_muted']}; font-size: 11px; margin-top: 4px;")


def build_supervisor_events_pane(
    height_px: int = 320,
    *,
    origins=frozenset({"chat"}),
    show_system_default: bool = False,
) -> None:
    """Render a live, origin-filtered EventBus stream, newest-first.

    Args:
        height_px: scroll-area height.
        origins: the set of trigger origins this pane shows (system is always
            gated behind the "Show system" switch, separately).
        show_system_default: initial state of the "Show system" switch.

    Liveness: `_on_event` only appends to a deque (publish thread); a UI-thread
    `ui.timer` is the only thing that calls `_pane.refresh()`.
    """
    from systemu.interface.event_bus import EventBus
    from systemu.interface.ui_helpers import (
        RepaintGate, event_ui_key, prune_open_state, stateful_expansion)

    # Thread-safe ring buffer (deque.append is atomic under the GIL).
    events: "deque[Dict[str, Any]]" = deque(maxlen=_MAX_EVENTS)
    state = {"show_system": bool(show_system_default)}

    # W11.1: expansion open/closed state, keyed by event_ui_key — restored on
    # every repaint so an opened row STAYS open. Repaints themselves are
    # change-gated (an idle pane stops re-rendering entirely).
    open_state: Dict[int, bool] = {}
    read_state: Dict[int, bool] = {}   # v0.9.42: per-session unread tracking
    gate = RepaintGate()

    # ── Muted "Show system" toggle ────────────────────────────────────────
    def _on_toggle(e) -> None:
        state["show_system"] = bool(getattr(e, "value", e))
        gate.bump()
        _tick()  # late-bound closure — defined below, before any toggle fires

    with ui.row().classes("items-center").style("gap: 10px;"):
        switch = ui.switch(
            "Show system", value=show_system_default, on_change=_on_toggle
        ).props("dense")
        switch.style(f"color: {THEME['text_muted']}; font-size: 11px; margin-bottom: 4px;")
        # W13.1: fingertip signal that work is still running in the background.
        # Explicit brand color — the bare default `primary` is muted at size="sm"
        # on the dark rail and reads as "no dots" (field report).
        _busy = ui.spinner("dots", size="sm", color=THEME["primary"])
        _busy.set_visibility(False)
        _busy.tooltip("Working in the background…")
        # W13.1: repaints pause while the operator reads an expansion — a
        # mid-stream rebuild yanked open content and could eat clicks.
        _paused_chip = ui.label(
            "live updates paused while reading — close details to resume"
        ).classes("s-muted").style("font-size: 11px;")
        _paused_chip.set_visibility(False)

    def _plain_row(ev) -> None:
        """Render one event as the flat time / level / message row (no arrow)."""
        level = (ev.get("level") or "INFO").upper()
        tstr = _format_event_time(ev.get("ts"))
        with ui.row().style("gap: 8px; align-items: baseline; padding: 2px 0;"):
            if tstr:
                ui.label(tstr).style(
                    f"color: {THEME['text_muted']}; font-size: 11px; "
                    f"font-family: monospace; min-width: 62px;"
                )
            ui.label(f"[{level}]").style(
                f"color: {_level_color(level)}; font-size: 11px; "
                f"font-weight: 700; min-width: 70px;"
            )
            ui.label(str(ev.get("message", ""))[:200]).style(
                f"color: {THEME['text']}; font-size: 12px;"
            )

    def _detail_row(ev) -> None:
        """Render one event as an expandable arrow exposing its `details`.

        Header is the usual time / level / message line; the body (shared
        ``render_event_details_body``) shows reasoning, tool params (JSON),
        tool result (JSON), plus a lazy "Show LLM response" button.
        """
        level = (ev.get("level") or "INFO").upper()
        tstr = _format_event_time(ev.get("ts"))
        header = (f"{tstr}  " if tstr else "") + f"[{level}] " + str(ev.get("message", ""))[:200]
        with stateful_expansion(
            header, state_key=event_ui_key(ev), open_state=open_state,
            read_state=read_state,
        ).style(
            f"font-size: 12px; color: {THEME['text']};"
        ):
            render_event_details_body(ev.get("details") or {}, context=ev.get("context"))

    @ui.refreshable
    def _pane():
        prune_open_state(open_state, (event_ui_key(e) for e in events))
        visible = [
            e for e in _display_order(events)
            if _passes_origin_filter(e, origins, show_system=state["show_system"])
        ]
        if not visible:
            ui.label("Waiting for live events…").style(
                f"color: {THEME['text_muted']}; font-size: 12px;"
            )
            return
        for ev in visible:   # newest first
            if _has_details(ev):
                _detail_row(ev)
            else:
                _plain_row(ev)

    with ui.scroll_area().style(f"height: {height_px}px; width: 100%;"):
        _pane()

    def _on_event(event: Dict[str, Any]) -> None:
        # Publish-thread callback: ONLY append + mark dirty. NEVER refresh
        # here (liveness).
        events.append(event)
        gate.bump()

    # Subscribe with replay so the pane shows recent history immediately.
    unsubscribe = EventBus.get().subscribe(_on_event, replay=True)

    # UI-thread timer is the SOLE driver of refresh.
    #
    # BUG-1 fix: this used to be gated on _should_schedule_refresh(client)
    # (has_socket_connection) — but during the initial page BUILD the websocket
    # is never connected yet, so the timer was NEVER scheduled and the pane
    # showed only the replay until a manual page refresh. The gate belongs to
    # POST-RUN refreshes (its v0.8.11 origin), not build-time scheduling.
    # safe_timer tolerates post-disposal ticks, and we cancel on disconnect.
    #
    # W11.1: the tick repaints ONLY when the gate is dirty — an unconditional
    # 0.5 s refresh destroyed and rebuilt every expansion collapsed, so the
    # expand arrow could never stay open (a fresh gate paints its first tick,
    # so the replayed history still shows immediately).
    _busy_state = {"on": False}

    def _tick() -> None:
        # W13.1: background-activity spinner (cheap; only flips on change).
        try:
            from systemu.interface.ui_helpers import background_activity_count
            busy = background_activity_count() > 0
            if busy != _busy_state["on"]:
                _busy_state["on"] = busy
                _busy.set_visibility(busy)
        except Exception:
            pass
        if not gate.should_paint():
            return
        # W13.1: never rebuild under the reader — pause while any expansion
        # is open; re-arm the gate so the queued update applies on collapse.
        if any(open_state.values()):
            gate.bump()
            try:
                _paused_chip.set_visibility(True)
            except Exception:
                pass
            return
        try:
            _paused_chip.set_visibility(False)
            _pane.refresh()
        except Exception:
            pass  # client may have disconnected

    from systemu.interface.ui_helpers import safe_timer
    pane_timer = safe_timer(0.5, _tick)

    # Unsubscribe + stop the timer when the client is DELETED — not on
    # disconnect. W7.2: app.on_disconnect is GLOBAL (fires for EVERY client's
    # disconnect), so any navigation or transient websocket drop anywhere
    # killed this pane's subscription process-wide; NiceGUI then reconnected
    # the same page WITHOUT rebuilding it → the feed was dead until a manual
    # refresh. client.on_delete fires only when this page is truly gone.
    def _on_client_gone() -> None:
        try:
            pane_timer.cancel()
        except Exception:
            pass
        try:
            unsubscribe()
        except Exception:
            pass

    try:
        ui.context.client.on_delete(_on_client_gone)
    except Exception:
        pass
