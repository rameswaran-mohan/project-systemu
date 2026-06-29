"""Systemu Chat — real-time supervisor & execution visibility page.

Shows a live scrolling feed of all systemu events:
  • Shadow execution steps (tool calls, observations, thoughts)
  • Supervisor actions (queue, retry, diagnosis, dead-letter)
  • Approval request dialogs (user clicks to respond)
  • Filter bar (All / Execution / Supervisor / Errors / Approvals)

Architecture (NiceGUI-safe threading):
  EventBus callbacks run on various background threads.  They MUST NOT touch
  the DOM directly.  Instead, each callback appends to a thread-safe deque
  (_mailbox).  A ui.timer (0.5 s) runs on NiceGUI's event-loop thread and
  drains the mailbox, rendering new message cards safely.

  On page unload (NiceGUI calls the cleanup registered via ui.context.client.on_disconnect),
  the EventBus subscription is cancelled and the timer is deactivated.
"""

from __future__ import annotations

import collections
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME

logger = logging.getLogger(__name__)


def _make_stop_handler(key: str):
    """Click handler: operator-cancel the running shadow with this Supervisor
    running-list key. Best-effort — a missing Supervisor/key is a no-op + notify."""
    def _stop(_=None):
        try:
            from systemu.runtime.supervisor import Supervisor
            ok = Supervisor.get().request_cancel(key)
        except Exception:
            ok = False
        try:
            ui.notify("Stopping…" if ok else "Could not stop (already finished?)",
                      type="warning" if ok else "negative")
        except Exception:
            pass
    return _stop


# ── Visual style constants ────────────────────────────────────────────────────

LEVEL_STYLES: Dict[str, Dict[str, str]] = {
    "INFO":    {"color": THEME["info"],    "icon": "ℹ️"},
    "SUCCESS": {"color": THEME["success"], "icon": "✅"},
    "WARNING": {"color": THEME["warning"], "icon": "⚠️"},
    "ERROR":   {"color": THEME["danger"],  "icon": "❌"},
}

CATEGORY_ICONS: Dict[str, str] = {
    "supervisor":        "🤖",
    "supervisor_action": "🧠",
    "shadow":            "👤",
    "tool":              "🔧",
    "tool_call":         "🔧",
    "observation":       "📊",
    "thought":           "💭",
    "approval":          "🛂",
    "system":            "⚡",
    "scroll":            "📜",
    "job":               "⚙️",
}

FILTER_OPTIONS = ["All", "Execution", "Supervisor", "Errors", "Approvals"]

FILTER_CATEGORIES: Dict[str, Optional[List[str]]] = {
    "All":        None,
    "Execution":  ["shadow", "tool", "tool_call", "observation", "thought"],
    # v0.4.1-d: "Supervisor" filter now also includes supervisor_action
    # events (the strategy-stream from the Intelligent Supervisor).
    "Supervisor": ["supervisor", "system", "supervisor_action"],
    "Errors":     None,   # handled by level check
    "Approvals":  ["approval"],
}

# Visual styling for supervisor actions — distinct glyphs make the
# operator's at-a-glance scan effective.
SUPERVISOR_ACTION_GLYPHS: Dict[str, str] = {
    "DO_NOTHING":         "⏸️",
    "NUDGE":              "👉",
    "INJECT_REFLECTION":  "💡",
    "FORCE_REFLECT":      "🔄",
    "ROLLBACK":           "↩️",
    "SWAP_SHADOW":        "🔁",
    "ESCALATE":           "📣",
    "TERMINATE":          "🛑",
    "SET_THINK_BUDGET":   "🧮",
    "RECALIBRATE_TOOL":   "🔁",
    "RECALIBRATE_SKILL":  "🎯",
}


# ── Page builder ──────────────────────────────────────────────────────────────

def build_systemu_chat_page() -> None:
    """Render the Systemu Chat page.  Called once per page load by dashboard.py."""

    state = AppState.get()

    # Per-page mailbox: EventBus callbacks drop events here; ui.timer drains it
    _mailbox: Deque[Dict[str, Any]] = collections.deque(maxlen=1000)

    # Mutable state boxes (avoid closure rebinding issues)
    _active_filter = ["All"]
    _auto_scroll   = [True]
    _paused        = [False]
    # Disconnect guard: set to True on _cleanup(); drain/render check this first
    # so the timer tick that fires *just after* cancel() can't touch deleted DOM.
    _disconnected  = [False]

    # ── Page header ───────────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 16px;"):
        with ui.column().style("gap: 2px;"):
            ui.label("🤖 Systemu Chat").style(
                f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
            )
            ui.label(
                "Live feed of all execution, supervisor, and system events."
            ).style(f"font-size: 13px; color: {THEME['text_muted']};")

        # Controls row
        with ui.row().style("gap: 10px; align-items: center;"):
            # Filter buttons
            filter_btns: Dict[str, Any] = {}
            for opt in FILTER_OPTIONS:
                is_active = opt == _active_filter[0]
                btn = ui.button(opt).style(
                    f"background: {'color-mix(in srgb, ' + THEME['primary'] + ' 30%, transparent)' if is_active else THEME['surface2']}; "
                    f"color: {THEME['text'] if is_active else THEME['text_muted']}; "
                    f"border-radius: 20px; font-size: 12px; padding: 4px 12px; "
                    f"border: 1px solid {'color-mix(in srgb, ' + THEME['primary'] + ' 50%, transparent)' if is_active else THEME['border']};"
                )
                filter_btns[opt] = btn

            def _make_filter_handler(opt: str):
                def _handler():
                    _active_filter[0] = opt
                    for k, b in filter_btns.items():
                        is_a = k == opt
                        b.style(
                            f"background: {'color-mix(in srgb, ' + THEME['primary'] + ' 30%, transparent)' if is_a else THEME['surface2']}; "
                            f"color: {THEME['text'] if is_a else THEME['text_muted']}; "
                            f"border-radius: 20px; font-size: 12px; padding: 4px 12px; "
                            f"border: 1px solid {'color-mix(in srgb, ' + THEME['primary'] + ' 50%, transparent)' if is_a else THEME['border']};"
                        )
                return _handler

            for opt in FILTER_OPTIONS:
                filter_btns[opt].on_click(_make_filter_handler(opt))

            # Pause / auto-scroll toggle
            pause_btn = ui.button("⏸ Pause").style(
                f"background: {THEME['surface2']}; color: {THEME['text_muted']}; "
                f"border-radius: 20px; font-size: 12px; padding: 4px 12px;"
            )

            def _toggle_pause():
                _paused[0] = not _paused[0]
                pause_btn.set_text("▶ Resume" if _paused[0] else "⏸ Pause")
                pause_btn.style(
                    f"background: {'color-mix(in srgb, ' + THEME['warning'] + ' 25%, transparent)' if _paused[0] else THEME['surface2']}; "
                    f"color: {THEME['warning'] if _paused[0] else THEME['text_muted']}; "
                    f"border-radius: 20px; font-size: 12px; padding: 4px 12px;"
                )

            pause_btn.on_click(_toggle_pause)

            # Clear button
            clear_btn = ui.button("🗑 Clear").style(
                f"background: {THEME['surface2']}; color: {THEME['text_muted']}; "
                f"border-radius: 20px; font-size: 12px; padding: 4px 12px;"
            )

    # ── Submit activity panel ─────────────────────────────────────────────────
    with ui.expansion("➕ Submit Activity to Supervisor", icon="send").style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 10px; margin-bottom: 16px;"
    ):
        with ui.row().classes("w-full items-end").style("gap: 10px; padding: 12px;"):
            # Shadow selector
            try:
                shadows = state.vault.load_index("shadow_army") or []
            except Exception:
                shadows = []

            shadow_options = {s.get("name", s.get("id", "?")): s.get("id", "") for s in shadows}
            shadow_select = ui.select(
                label="Shadow",
                options=list(shadow_options.keys()) if shadow_options else ["(no shadows)"],
            ).style("min-width: 180px;")
            if shadow_options:
                shadow_select.value = list(shadow_options.keys())[0]

            # Activity ID input
            activity_input = ui.input(
                label="Activity ID",
                placeholder="activity_abc123",
            ).style("flex: 1;")

            # Priority — ui.select validates value= against option labels (keys),
            # not numeric payloads, so we use label strings and parse on submit.
            _PRIORITY_OPTIONS = ["1 — Urgent", "5 — Normal", "10 — Background"]
            priority_select = ui.select(
                label="Priority",
                options=_PRIORITY_OPTIONS,
                value="5 — Normal",
            ).style("min-width: 140px;")

            def _submit_activity():
                activity_id = activity_input.value.strip()
                shadow_name = shadow_select.value or ""
                shadow_id   = shadow_options.get(shadow_name, "")
                priority    = int((priority_select.value or "5 — Normal").split(" — ")[0])

                if not activity_id or not shadow_id:
                    ui.notify("Please enter both Activity ID and select a Shadow.", type="warning")
                    return

                try:
                    from systemu.runtime.supervisor import Supervisor
                    sup = Supervisor.get()
                    # v0.8.16: operator-initiated manual submit → "manual" origin
                    # so it partitions into Manual Logs, not the Supervisor (chat) pane.
                    sid = sup.submit(activity_id, shadow_id, priority=priority,
                                     reason="ui-submit", origin="manual")
                    ui.notify(f"✅ Submitted — submission_id: {sid}", type="positive")
                    activity_input.set_value("")
                except RuntimeError:
                    ui.notify("Supervisor not running — start the daemon first.", type="negative")
                except Exception as exc:
                    ui.notify(f"Error: {exc}", type="negative")

            ui.button("▶ Submit", on_click=_submit_activity).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px; "
                f"font-weight: 700; padding: 10px 18px;"
            )

    # ── Workflow Pipeline card (compact) ──────────────────────────────────────
    # Mirrors the card on the Overview page so operators monitoring the
    # live feed can see at-a-glance where every in-flight workflow is.
    try:
        from systemu.interface.components.workflow_pipeline import build_workflow_pipeline
        with ui.column().classes("w-full").style("margin-bottom: 12px;"):
            build_workflow_pipeline(compact=True)
    except Exception as _wp_exc:
        logger.debug("[Chat] workflow pipeline component unavailable: %s", _wp_exc)

    # ── Live status bar ───────────────────────────────────────────────────────
    status_bar = ui.label("").style(
        f"font-size: 12px; color: {THEME['text_muted']}; margin-bottom: 8px;"
    )

    # v0.9.32 (D3.3): per-running-shadow Stop list — reads Supervisor running set
    # (each entry exposes its `key`) and offers a cooperative cancel button.
    running_panel = ui.column().classes("w-full").style("margin-bottom: 8px; gap: 4px;")

    @ui.refreshable
    def _render_running_stops():
        running = []
        try:
            from systemu.runtime.supervisor import Supervisor
            running = Supervisor.get().get_status().get("running", [])
        except Exception:
            running = []
        for r in running:
            key = r.get("key")
            if not key:
                continue
            # v0.9.32: token-derived styles as named locals — UI-style lint
            # forbids inline .style(f"…"); name the style, re-theme via tokens.
            _row_style = (
                f"padding: 4px 8px; border: 1px solid {THEME['border']}; "
                f"border-radius: 8px;"
            )
            _lbl_style = f"font-size: 12px; color: {THEME['text']};"
            _stop_style = (
                f"background: {THEME['danger']}; color: white; border-radius: 6px; "
                f"padding: 2px 10px; font-size: 11px; font-weight: 600;"
            )
            with ui.row().classes("w-full items-center justify-between").style(_row_style):
                ui.label(
                    f"▶ {r.get('activity_id', key)}  ({r.get('status', 'running')})"
                ).style(_lbl_style)
                ui.button("Stop", on_click=_make_stop_handler(key)).style(_stop_style)

    with running_panel:
        _render_running_stops()

    # ── Message feed ─────────────────────────────────────────────────────────
    feed_container = ui.column().classes("w-full").style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 16px; gap: 8px; "
        f"min-height: 400px; max-height: 70vh; overflow-y: auto;"
    )
    feed_container.props('id="systemu-chat-feed"')

    # Initial empty state
    _empty_label = [None]
    with feed_container:
        _empty_label[0] = ui.label(
            "⚡ Waiting for events… (start the daemon or run an activity)"
        ).style(f"color: {THEME['text_muted']}; font-style: italic; font-size: 14px;")

    # ── Pending approval dialogs ──────────────────────────────────────────────
    # Maps approval key → small dict holding {col, btn_row} so we can
    # surgically update the card when a dismissal event arrives.  Keyed by
    # request_id for classic blocking approvals, and by dedup_key for
    # v0.3.6 out-of-band approvals (e.g. tool-dep installs).
    _pending_approvals: Dict[str, Dict[str, Any]] = {}

    # ── Render a single message card ─────────────────────────────────────────

    def _render_message(event: Dict[str, Any]) -> None:
        """Render one event as a styled card in the feed.
        Must be called from NiceGUI's event-loop thread.
        """
        category = event.get("category", "system")
        level    = event.get("level", "INFO").upper()
        message  = event.get("message", "")
        ctx      = event.get("context", {})
        ts_raw   = event.get("ts", "")

        # Filter check
        active = _active_filter[0]
        if active == "Errors" and level not in ("ERROR", "WARNING"):
            return
        if active == "Approvals" and category != "approval":
            return
        cats = FILTER_CATEGORIES.get(active)
        if cats is not None and category not in cats and active not in ("Errors", "Approvals"):
            return

        # Clear empty state
        if _empty_label[0] is not None:
            try:
                _empty_label[0].delete()
            except Exception:
                pass
            _empty_label[0] = None

        # Format timestamp
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            ts_str = ts.astimezone().strftime("%H:%M:%S")
        except Exception:
            ts_str = ts_raw[11:19] if len(ts_raw) >= 19 else ts_raw

        style = LEVEL_STYLES.get(level, LEVEL_STYLES["INFO"])
        cat_icon = CATEGORY_ICONS.get(category, "•")
        level_color = style["color"]

        # Special handling for approval requests
        if category == "approval":
            _render_approval_card(event, ts_str, level_color)
            return

        # v0.3.6: out-of-band approval resolved on another surface
        # (Tools page approve / revoke).  Close any open card with the
        # matching dedup_key so the operator sees the resolution land.
        if category == "approval_dismissed":
            _handle_approval_dismissed(event)
            return

        # v0.4.1-d: supervisor strategy-stream tick — compact inline card.
        if category == "supervisor_action":
            _render_supervisor_action(event, ts_str)
            return

        # Regular message card
        with feed_container:
            with ui.row().classes("w-full items-start").style(
                f"padding: 10px 12px; border-radius: 8px; "
                f"background: color-mix(in srgb, {level_color} 6%, {THEME['surface2']}); "
                f"border-left: 3px solid {level_color}; gap: 10px;"
            ):
                # Icon + timestamp
                with ui.column().style("gap: 2px; min-width: 44px; align-items: center;"):
                    ui.label(cat_icon).style("font-size: 16px; line-height: 1;")
                    ui.label(ts_str).style(
                        f"font-size: 10px; color: {THEME['text_muted']}; font-family: monospace;"
                    )

                # Message body
                with ui.column().style("gap: 4px; flex: 1; min-width: 0;"):
                    # Category badge + message
                    with ui.row().style("gap: 8px; align-items: baseline; flex-wrap: wrap;"):
                        ui.label(category.upper()).style(
                            f"font-size: 10px; font-weight: 700; letter-spacing: 0.06em; "
                            f"color: {level_color};"
                        )
                        # Main message (wrap long lines)
                        ui.label(message).style(
                            f"font-size: 13px; color: {THEME['text']}; "
                            f"word-break: break-word; white-space: pre-wrap; flex: 1;"
                        )

                    # Relevant context keys (not the full dict — too noisy)
                    _interesting = {
                        k: v for k, v in ctx.items()
                        if k in ("activity_id", "shadow_id", "execution_id",
                                 "failure_category", "retry_count", "tool", "type")
                        and v
                    }
                    if _interesting:
                        meta_str = "  ".join(f"{k}: {v}" for k, v in _interesting.items())
                        ui.label(meta_str).style(
                            f"font-size: 11px; color: {THEME['text_muted']}; font-family: monospace;"
                        )

                    # Analysis block (failure diagnosis)
                    if ctx.get("type") == "failure_analysis" and ctx.get("analysis"):
                        analysis = ctx["analysis"]
                        with ui.column().style(
                            f"background: color-mix(in srgb, {THEME['primary']} 10%, {THEME['surface2']}); "
                            f"border-radius: 6px; padding: 8px; gap: 4px; margin-top: 4px;"
                        ):
                            ui.label("📋 Failure Analysis").style(
                                f"font-size: 11px; font-weight: 700; color: {THEME['primary']};"
                            )
                            for field, label in [
                                ("root_cause", "Root Cause"),
                                ("failure_category", "Category"),
                                ("immediate_fix", "Immediate Fix"),
                                ("prevention", "Prevention"),
                            ]:
                                val = analysis.get(field)
                                if val:
                                    ui.label(f"• {label}: {val}").style(
                                        f"font-size: 12px; color: {THEME['text']};"
                                    )
                            retry = analysis.get("retry_recommended")
                            if retry is not None:
                                color = THEME["success"] if retry else THEME["danger"]
                                ui.label(f"• Retry: {'Recommended ✅' if retry else 'Not recommended ❌'}").style(
                                    f"font-size: 12px; color: {color}; font-weight: 600;"
                                )

                    # BUG-2: the expand-arrow for events carrying a details
                    # payload (reasoning / tool params / tool result / lazy
                    # LLM transcript) — the same body the /insights live pane
                    # renders, so the affordance exists on this feed too.
                    from systemu.interface.components.live_events_pane import (
                        _has_details, render_event_details_body,
                    )
                    if _has_details(event):
                        with ui.expansion("Details", value=False).classes(
                            "w-full s-muted"
                        ).style("font-size: 11px;"):
                            render_event_details_body(event.get("details") or {}, context=event.get("context"))

        # Auto-scroll (inject JS — runs in the browser)
        if _auto_scroll[0]:
            ui.run_javascript(
                'var el=document.getElementById("systemu-chat-feed"); '
                'if(el){el.scrollTop=el.scrollHeight;}'
            )

    def _render_approval_card(event: Dict[str, Any], ts_str: str, level_color: str) -> None:
        """Render an approval request as an interactive card.

        Two flavours:

        * **Classic blocking approval** (``request_id`` set) — caller is
          blocked on ``threading.Event``; renders one button per option
          that calls ``EventBus.resolve_approval(request_id, choice)``.

        * **Out-of-band approval** (``redirect_to`` set, no request_id;
          v0.3.6) — operator resolves it on a dedicated page; we render
          a single navigation button.  Dedup by ``dedup_key`` so 50
          shadows hitting the same missing dep produce one card.
        """
        ctx         = event.get("context", {})
        request_id  = ctx.get("request_id", "")
        dedup_key   = ctx.get("dedup_key", "")
        redirect_to = ctx.get("redirect_to", "")
        message     = ctx.get("approval_message", event.get("message", ""))
        options     = ctx.get("options", []) or []

        key = request_id or dedup_key
        if not key:
            return

        if _pending_approvals.get(key):
            return

        with feed_container:
            card_col = ui.column().classes("w-full").style(
                f"padding: 14px; border-radius: 8px; "
                f"background: color-mix(in srgb, {THEME['warning']} 10%, {THEME['surface2']}); "
                f"border: 1px solid color-mix(in srgb, {THEME['warning']} 40%, transparent); gap: 10px;"
            )
            with card_col:
                with ui.row().style("gap: 8px; align-items: center;"):
                    ui.label("🛂").style("font-size: 18px;")
                    ui.label("Approval Required").style(
                        f"font-size: 14px; font-weight: 700; color: {THEME['warning']};"
                    )
                    ui.label(ts_str).style(
                        f"font-size: 11px; color: {THEME['text_muted']}; font-family: monospace;"
                    )

                ui.label(message).style(
                    f"font-size: 13px; color: {THEME['text']}; white-space: pre-wrap;"
                )

                btn_row = ui.row().style("gap: 8px; margin-top: 4px;")

                # v0.3.6: redirect-style approval — single navigation button.
                if redirect_to and not request_id:
                    with btn_row:
                        ui.button(
                            f"Review on Tools page →",
                            on_click=lambda url=redirect_to: ui.navigate.to(url),
                        ).style(
                            f"background: {THEME['primary']}; color: white; "
                            f"border-radius: 6px; font-size: 13px; font-weight: 600; "
                            f"padding: 6px 14px;"
                        )
                        ui.button(
                            "Dismiss notice",
                            on_click=lambda c=card_col, k=key,
                                            sig=ctx.get("pattern_signature"),
                                            act=ctx.get("supervisor_action"):
                                            _dismiss_card(
                                                c, k, outcome="dismissed",
                                                pattern_signature=sig,
                                                action=act,
                                            ),
                        ).style(
                            f"background: transparent; color: {THEME['text_muted']}; "
                            f"border: 1px solid {THEME['border']}; border-radius: 6px; "
                            f"font-size: 12px; padding: 6px 12px;"
                        )
                # Classic blocking approval — resolve buttons.
                else:
                    def _make_resolve(opt: str, row: Any, col: Any):
                        def _handler():
                            try:
                                from systemu.interface.event_bus import EventBus
                                resolved = EventBus.get().resolve_approval(request_id, opt)
                                if resolved:
                                    row.clear()
                                    with row:
                                        ui.label(f"✅ Resolved: {opt}").style(
                                            f"font-size: 12px; color: {THEME['success']}; font-weight: 600;"
                                        )
                                else:
                                    ui.notify("Approval request already resolved or expired.", type="warning")
                            except Exception as exc:
                                ui.notify(f"Error resolving approval: {exc}", type="negative")
                        return _handler

                    with btn_row:
                        for opt in (options or ["OK"]):
                            bg = THEME["success"] if opt.lower() in ("approve", "yes", "ok", "retry") else (
                                THEME["danger"] if opt.lower() in ("reject", "no", "cancel", "skip") else
                                THEME["primary"]
                            )
                            ui.button(opt, on_click=_make_resolve(opt, btn_row, card_col)).style(
                                f"background: {bg}; color: white; border-radius: 6px; "
                                f"font-size: 13px; font-weight: 600; padding: 6px 14px;"
                            )

            _pending_approvals[key] = {"col": card_col, "btn_row": btn_row}

    def _render_supervisor_action(event: Dict[str, Any], ts_str: str) -> None:
        """Render an Intelligent Supervisor strategy-stream tick.

        Compact inline card showing the action glyph + rationale + tier
        used.  Distinct from approval cards (no operator action) and
        from raw 'supervisor' messages (which carry free-form text).
        """
        ctx       = event.get("context", {}) or {}
        action    = (ctx.get("supervisor_action") or "DO_NOTHING").upper()
        glyph     = SUPERVISOR_ACTION_GLYPHS.get(action, "🧠")
        rationale = (ctx.get("rationale") or "").strip()
        exec_id   = ctx.get("execution_id", "")
        consec    = ctx.get("consec_failures", 0)
        tier      = ctx.get("tier_used") or ""
        classifier = ctx.get("classifier") or ""

        # DO_NOTHING noise control — render it minimally so the feed
        # isn't flooded when the supervisor is mostly inactive.
        is_quiet = action == "DO_NOTHING"

        # Colour: warning for high-impact, info otherwise
        color = THEME["warning"] if action in (
            "ROLLBACK", "SWAP_SHADOW", "TERMINATE", "FORCE_REFLECT",
        ) else THEME["info"]

        with feed_container:
            with ui.row().classes("w-full items-start").style(
                f"padding: 6px 10px; gap: 10px; "
                f"background: {THEME['surface'] if not is_quiet else 'transparent'}; "
                f"border-left: 3px solid {color if not is_quiet else THEME['border']}; "
                f"margin-bottom: 4px;"
            ):
                ui.label(ts_str).style(
                    f"font-family: monospace; font-size: 10px; "
                    f"color: {THEME['text_muted']}; min-width: 60px;"
                )
                ui.label(glyph).style("font-size: 14px; min-width: 18px;")
                with ui.column().style("flex: 1; gap: 2px;"):
                    header_parts = [f"SUPERVISOR · {action}"]
                    if classifier:
                        header_parts.append(f"classifier={classifier}")
                    if consec:
                        header_parts.append(f"consec={consec}")
                    if tier:
                        header_parts.append(f"tier={tier}")
                    if exec_id:
                        header_parts.append(f"exec={exec_id[:12]}")
                    ui.label("  ·  ".join(header_parts)).style(
                        f"font-size: 10px; font-weight: 700; letter-spacing: 0.06em; "
                        f"color: {color};"
                    )
                    if rationale and not is_quiet:
                        ui.label(rationale[:280]).style(
                            f"font-size: 12px; color: {THEME['text']}; "
                            f"word-break: break-word;"
                        )
                    # BUG-2: strategy ticks carry the richest details
                    # (reasoning + llm_ref) — give them the expand-arrow too.
                    from systemu.interface.components.live_events_pane import (
                        _has_details, render_event_details_body,
                    )
                    if _has_details(event):
                        with ui.expansion("Details", value=False).classes(
                            "w-full s-muted"
                        ).style("font-size: 11px;"):
                            render_event_details_body(event.get("details") or {}, context=event.get("context"))

    def _handle_approval_dismissed(event: Dict[str, Any]) -> None:
        """Close any open approval card whose dedup_key matches.

        Fired by ``EventBus.publish_dep_approval_dismissed`` when the
        operator approves / revokes the underlying package on the Tools
        page.  Updates the existing card in place — no new card is
        rendered.
        """
        ctx       = event.get("context", {}) or {}
        dedup_key = ctx.get("dedup_key", "")
        outcome   = ctx.get("outcome", "resolved")
        package   = ctx.get("package", "")
        record    = _pending_approvals.pop(dedup_key, None)
        if not record:
            return
        try:
            record["btn_row"].clear()
            with record["btn_row"]:
                glyph = "✅" if outcome == "approved" else ("↩️" if outcome == "revoked" else "•")
                ui.label(f"{glyph} {package} — {outcome}").style(
                    f"font-size: 12px; color: {THEME['success']}; font-weight: 600;"
                )
        except Exception:
            # Card was already removed (e.g. clear feed clicked) — fine.
            logger.debug("[SystemuChat] could not update dismissed card", exc_info=True)

    def _dismiss_card(col: Any, key: str, *, outcome: str,
                       pattern_signature: Optional[str] = None,
                       action: Optional[str] = None) -> None:
        """Operator clicked 'Dismiss notice' — hide the card locally.

        v0.4.1-c: also records the dismissal to the RejectionStore so the
        Intelligent Supervisor can consult it before re-proposing the same
        intervention.  The card carries a ``pattern_signature`` when the
        supervisor or a v0.4.0+ pipeline published it; legacy cards
        without one are dismissed silently.

        Note: this only hides the LOCAL card.  The underlying approval is
        not resolved (the package remains pending in the store).  Use
        this for "yes I saw it; I'll handle it later".
        """
        _pending_approvals.pop(key, None)
        try:
            col.delete()
        except Exception:
            pass
        if pattern_signature:
            try:
                from systemu.runtime.rejection_store import get_rejection_store
                get_rejection_store().record_rejection(
                    pattern_signature,
                    dedup_key=key,
                    action=action,
                    reason="operator_dismissed",
                )
            except Exception:
                logger.debug("[SystemuChat] rejection record skipped", exc_info=True)

    # ── Clear handler ─────────────────────────────────────────────────────────
    def _do_clear():
        feed_container.clear()
        _empty_label[0] = None
        _pending_approvals.clear()
        with feed_container:
            _empty_label[0] = ui.label("Feed cleared.").style(
                f"color: {THEME['text_muted']}; font-style: italic; font-size: 14px;"
            )

    clear_btn.on_click(_do_clear)

    # ── EventBus subscription (non-blocking callback) ─────────────────────────
    def _on_event(event: Dict[str, Any]) -> None:
        """EventBus callback — must not block.  Drops event into mailbox."""
        if not _paused[0]:
            _mailbox.append(event)

    try:
        from systemu.interface.event_bus import EventBus
        unsubscribe = EventBus.get().subscribe(_on_event, replay=True)
    except Exception as exc:
        logger.warning("[SystemuChat] Could not subscribe to EventBus: %s", exc)
        unsubscribe = lambda: None  # noqa: E731

    # ── Timer: drain mailbox → render DOM ────────────────────────────────────
    BATCH_SIZE = 20   # max messages rendered per tick (prevents frame drops)
    _msg_count = [0]

    def _drain():
        """Called by ui.timer every 0.5 s on NiceGUI's event-loop thread."""
        # Guard: if client already disconnected, do nothing — avoids
        # "parent slot deleted" RuntimeError on the last in-flight tick.
        if _disconnected[0]:
            return
        rendered = 0
        while _mailbox and rendered < BATCH_SIZE:
            event = _mailbox.popleft()
            _render_message(event)
            _msg_count[0] += 1
            rendered += 1

        # Update status bar
        running_count = 0
        queue_depth   = 0
        try:
            from systemu.runtime.supervisor import Supervisor
            st = Supervisor.get().get_status()
            running_count = st.get("running_count", 0)
            queue_depth   = st.get("queue_depth", 0)
        except Exception:
            pass

        status_bar.set_text(
            f"📨 {_msg_count[0]} events  •  "
            f"🔄 {running_count} running  •  "
            f"📥 {queue_depth} queued"
            + ("  •  ⏸ PAUSED" if _paused[0] else "")
        )

        # v0.9.32 (D3.3): refresh the per-shadow Stop list every drain tick.
        try:
            _render_running_stops.refresh()
        except Exception:
            pass

    from systemu.interface.ui_helpers import safe_timer
    feed_timer = safe_timer(0.5, _drain)

    # ── Cleanup on page disconnect ────────────────────────────────────────────
    def _cleanup():
        """Stop timer and unsubscribe when user navigates away.

        Sets _disconnected[0] = True BEFORE cancel() so the timer tick
        that may already be queued/in-flight exits early instead of touching
        deleted DOM elements (prevents 'parent slot deleted' RuntimeError).
        """
        _disconnected[0] = True    # must come first — guards in-flight ticks
        try:
            feed_timer.cancel()
        except Exception:
            pass
        try:
            unsubscribe()
        except Exception:
            pass
        logger.debug("[SystemuChat] Client disconnected — timer cancelled, EventBus unsubscribed.")

    # W7.2: cleanup on true client DELETION, not on disconnect — a transient
    # websocket drop (e.g. during a heavy approve) fired _cleanup, and NiceGUI
    # reconnected the same page WITHOUT rebuilding it → feed dead until reload.
    ui.context.client.on_delete(_cleanup)
