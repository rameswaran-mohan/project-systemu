"""NiceGUI Dashboard — Notifications page.

Two-tab layout:
  • Manual Logs — real-time tail of vault/notifications/event_log.jsonl (auto-refreshes every 2s)
  • Pending    — list of PENDING notifications with Approve/Reject actions

Fixes the 404 that occurred because this route was never registered.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME, status_badge_html
from systemu.interface.scroll_gate import open_scroll_review_dialog

logger = logging.getLogger(__name__)

# Level → colour mapping
_LEVEL_COLOR = {
    "INFO":    "#3b82f6",   # blue
    "SUCCESS": "#22c55e",   # green
    "WARNING": "#f59e0b",   # amber
    "ERROR":   "#ef4444",   # red
}

# Category → Material icon name
_CAT_ICON = {
    "scroll":  "description",
    "shadow":  "person",
    "tool":    "build",
    "job":     "settings",
    "system":  "bolt",
}


def _filter_events(events: list, mode: str) -> list:
    """Filter events by origin.

    mode:
      - "all":    everything
      - "system": context.origin != "manual_execute" (or missing)
      - "manual": context.origin == "manual_execute"
    """
    if mode == "all":
        return list(events)
    if mode == "manual":
        return [
            e for e in events
            if (e.get("context", {}) or {}).get("origin") == "manual_execute"
        ]
    # system
    return [
        e for e in events
        if (e.get("context", {}) or {}).get("origin") != "manual_execute"
    ]


def build_notifications_page() -> None:
    state = AppState.get()
    vault = state.vault

    ui.label("Notifications").style(
        f"font-size: 22px; font-weight: 800; color: {THEME['text']}; margin-bottom: 20px;"
    )

    with ui.tabs().classes("w-full") as tabs:
        tab_log     = ui.tab("Manual Logs")
        tab_pending = ui.tab("Pending Actions")

    with ui.tab_panels(tabs, value=tab_log).classes("w-full bg-transparent"):

        # ── EVENT LOG TAB ──────────────────────────────────────────────────────
        with ui.tab_panel(tab_log):
            ui.label("Live Manual Logs — auto-refreshes every 2 seconds.").style(
                f"font-size: 13px; color: {THEME['text_muted']}; margin-bottom: 16px;"
            )

            # Toolbar
            with ui.row().style("gap: 10px; align-items: center; margin-bottom: 12px;"):
                filter_input = ui.input(placeholder="Filter events...").style(
                    f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
                    f"border-radius: 8px; padding: 6px 10px; color: {THEME['text']}; width: 280px;"
                )
                level_filter = ui.select(
                    options={"": "All Levels", "INFO": "Info", "SUCCESS": "Success", "WARNING": "Warning", "ERROR": "Error"},
                    label="Level",
                ).style("min-width: 130px;")
                level_filter.value = ""

                origin_filter = ui.select(
                    options={"all": "All Events", "system": "System Only", "manual": "Manual Only"},
                    label="Show",
                ).style("min-width: 160px;")
                origin_filter.value = "all"

                # Clear log button
                def _clear_log():
                    log_path = _get_log_path(vault)
                    if log_path and log_path.exists():
                        log_path.write_text("", encoding="utf-8")
                        ui.notify("Manual Logs cleared.", type="positive")
                        _log_table.refresh()

                ui.button("Clear Log", icon="delete", on_click=_clear_log).style(
                    f"background: {THEME['surface2']}; color: {THEME['text']}; "
                    f"border: 1px solid {THEME['border']}; border-radius: 8px; font-size: 12px;"
                )

            log_container = ui.column().classes("w-full")

            @ui.refreshable
            def _log_table():
                events = _load_events(vault, max_lines=200)
                # Apply origin filter (All / System / Manual) first so the rest
                # of the rendering only sees the relevant subset.
                events = _filter_events(events, origin_filter.value or "all")
                q = filter_input.value.lower() if filter_input.value else ""
                lv = level_filter.value or ""

                filtered = [
                    e for e in events
                    if (not q or q in e.get("message", "").lower() or q in e.get("category", "").lower())
                    and (not lv or e.get("level", "") == lv)
                ]

                if not filtered:
                    ui.label("No events yet — events appear as the system processes scrolls and shadows.").style(
                        f"color: {THEME['text_muted']}; font-style: italic; padding: 20px;"
                    )
                    return

                with ui.element("table").style(
                    f"width: 100%; border-collapse: collapse; background: {THEME['surface']}; "
                    f"border: 1px solid {THEME['border']}; border-radius: 12px; overflow: hidden;"
                ):
                    with ui.element("thead"):
                        with ui.element("tr"):
                            for col in ["Time", "Level", "Category", "Message"]:
                                with ui.element("th").style(
                                    f"background: {THEME['surface2']}; color: {THEME['text_muted']}; "
                                    f"font-size: 11px; font-weight: 600; text-transform: uppercase; "
                                    f"letter-spacing: 0.08em; padding: 10px 16px; text-align: left; "
                                    f"border-bottom: 1px solid {THEME['border']};"
                                ):
                                    ui.label(col)

                    with ui.element("tbody"):
                        # Reverse for newest-first
                        for evt in reversed(filtered[-100:]):
                            level    = evt.get("level", "INFO")
                            color    = _LEVEL_COLOR.get(level, THEME["text"])
                            cat      = evt.get("category", "system")
                            icon     = _CAT_ICON.get(cat, "push_pin")
                            ts_raw   = evt.get("ts", "")
                            ts_disp  = ts_raw[11:19] if len(ts_raw) >= 19 else ts_raw  # HH:MM:SS
                            msg      = evt.get("message", "")

                            with ui.element("tr").style(
                                f"border-bottom: 1px solid {THEME['border']};"
                            ):
                                _td_cell(ts_disp, muted=True, mono=True)
                                with ui.element("td").style("padding: 10px 14px;"):
                                    ui.html(
                                        f'<span style="background: color-mix(in srgb, {color} 20%, transparent); '
                                        f'color: {color}; font-size: 10px; font-weight: 700; '
                                        f'padding: 2px 8px; border-radius: 4px; letter-spacing: 0.05em;">'
                                        f'{level}</span>'
                                    )
                                with ui.element("td").style("padding: 10px 14px;"):
                                    with ui.row().style("gap: 6px; align-items: center; flex-wrap: nowrap;"):
                                        ui.icon(icon).classes("s-muted").style(
                                            "font-size: 14px;"
                                        )
                                        ui.label(cat).style(
                                            f"font-size: 12px; color: {THEME['text_muted']}; font-weight: 600;"
                                        )
                                _td_cell(msg)

            _log_table()

            # Hook filter inputs
            filter_input.on("input", lambda _: _log_table.refresh())
            level_filter.on("update:model-value", lambda _: _log_table.refresh())
            origin_filter.on("update:model-value", lambda _: _log_table.refresh())

            # Auto-refresh timer — polls event_log.jsonl every 2 seconds.
            # safe_timer wraps the callback so post-navigation ticks don't
            # spam the log with `parent slot deleted` RuntimeErrors.
            from systemu.interface.ui_helpers import safe_timer
            safe_timer(2.0, _log_table.refresh)

        # ── PENDING ACTIONS TAB ───────────────────────────────────────────────
        with ui.tab_panel(tab_pending):
            @ui.refreshable
            def _pending_panel():
                # Phase 3 Batch 3 (render unification): gate rows owned by the
                # Inbox (decisions queue, context.kind=="gate") render via the
                # UNIFIED card so this surface is no longer split-brained from
                # /inbox. The legacy `notifications` index (scroll/forge/dep
                # notifications) is a SEPARATE store — those rows keep the
                # legacy `_pending_card` fallback so nothing vanishes mid-
                # migration.
                from systemu.interface.pages.inbox_page import (
                    render_inbox_gate_cards,
                )
                n_gates = render_inbox_gate_cards(
                    vault, on_resolved=_pending_panel.refresh)

                pending = _load_pending_notifications(vault)

                if not pending and not n_gates:
                    ui.label("No pending actions. Everything is up to date.").style(
                        f"color: {THEME['text_muted']}; font-style: italic; padding: 20px;"
                    )
                    return

                for notif in pending:
                    _pending_card(notif, vault, _pending_panel)

            _pending_panel()

            # Refresh pending every 5s (safe_timer = silent on post-nav ticks)
            from systemu.interface.ui_helpers import safe_timer
            safe_timer(5.0, _pending_panel.refresh)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _td_cell(text: str, *, muted: bool = False, mono: bool = False) -> None:
    style = (
        f"padding: 10px 14px; font-size: 13px; "
        f"color: {THEME['text_muted'] if muted else THEME['text']};"
    )
    if mono:
        style += " font-family: monospace;"
    with ui.element("td").style(style):
        ui.label(text)


def _get_log_path(vault) -> Optional[Path]:
    """Return the path to event_log.jsonl."""
    try:
        path = Path(vault.root) / "notifications" / "event_log.jsonl"
        return path
    except Exception:
        return None


def _load_events(vault, max_lines: int = 200) -> List[Dict[str, Any]]:
    """Read the last N lines of event_log.jsonl."""
    log_path = _get_log_path(vault)
    if not log_path or not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        events = []
        for line in lines[-max_lines:]:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return events
    except Exception as exc:
        logger.warning("[Notifications] Could not read event log: %s", exc)
        return []


def build_events_log_pane(max_rows: int = 50, height_px: int = 320) -> None:
    """Compact event-log pane for the v0.8.8 Console right column.

    Renders the last ``max_rows`` events from the same file-tail source the
    /insights Events tab uses (``_load_events``), color-coded by level, inside
    a fixed-height scroll area. Refreshes every 2s via ui.timer.

    Self-contained: drop it into any column and it wires its own refresh.
    """
    from systemu.interface.dashboard_state import AppState
    state = AppState.get()

    @ui.refreshable
    def _pane():
        events = _load_events(state.vault)
        events = events[-max_rows:]
        events = list(reversed(events))   # v0.8.9: newest first
        if not events:
            ui.label("No events yet.").style(
                f"color: {THEME['text_muted']}; font-size: 12px;"
            )
            return
        from systemu.interface.components.live_events_pane import _format_event_time
        for ev in events:
            _level = (ev.get("level") or "INFO").upper()
            _color = {
                "ERROR":   THEME["danger"],
                "WARNING": THEME["warning"],
                "SUCCESS": THEME["success"],
            }.get(_level, THEME["text_muted"])
            tstr = _format_event_time(ev.get("ts") or ev.get("timestamp") or ev.get("time"))
            with ui.row().style("gap: 8px; align-items: baseline; padding: 2px 0;"):
                if tstr:
                    ui.label(tstr).style(
                        f"color: {THEME['text_muted']}; font-size: 11px; "
                        f"font-family: monospace; min-width: 62px;"
                    )
                ui.label(f"[{_level}]").style(
                    f"color: {_color}; font-size: 11px; font-weight: 700; min-width: 70px;"
                )
                ui.label(str(ev.get("message", ""))[:200]).style(
                    f"color: {THEME['text']}; font-size: 12px;"
                )

    with ui.scroll_area().style(f"height: {height_px}px; width: 100%;"):
        _pane()
    ui.timer(2.0, _pane.refresh)


def _load_pending_notifications(vault) -> List[Dict[str, Any]]:
    """Load pending notifications from the vault."""
    try:
        all_notifs = vault.load_index("notifications")
        return [n for n in all_notifs if n.get("status") == "pending"]
    except Exception:
        return []


def _pending_card(notif: Dict[str, Any], vault, refresh_fn) -> None:
    """Render a pending notification card with action buttons."""
    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 18px; margin-bottom: 12px; width: 100%;"
    ):
        with ui.row().style("align-items: flex-start; gap: 12px;"):
            ui.label("⚠").style("font-size: 24px; padding-top: 2px;")
            with ui.column().style("flex: 1; gap: 6px;"):
                ui.label(notif.get("title", "Notification")).style(
                    f"font-size: 15px; font-weight: 700; color: {THEME['text']};"
                )
                ui.label(notif.get("message", "")).style(
                    f"font-size: 13px; color: {THEME['text_muted']}; line-height: 1.5; "
                    f"white-space: pre-wrap;"
                )
                ts = notif.get("created_at", "")
                if ts:
                    ui.label(f"Created: {ts[:19].replace('T', ' ')}").style(
                        f"font-size: 11px; color: {THEME['text_muted']}; font-family: monospace;"
                    )

        actions  = notif.get("actions", ["OK"])
        notif_id = notif.get("id", "")
        ctx      = notif.get("context", {})

        with ui.row().style("gap: 8px; margin-top: 12px;"):
            for action in actions:
                btn_color = (
                    THEME["success"] if action.lower() in ("approve", "forge", "ok", "awaken")
                    else THEME["danger"] if action.lower() in ("reject", "skip")
                    else THEME["surface2"]
                )
                btn_text_color = "white" if action.lower() in ("approve", "forge", "ok", "awaken", "reject", "skip") else THEME["text"]

                def _do_action(_, a=action, nid=notif_id, n_ctx=ctx):
                    try:
                        vault.resolve_notification(nid, a)
                        _dispatch_notification_action(a, n_ctx, vault, refresh_fn)
                    except Exception as exc:
                        logger.exception("[Notifications] Action dispatch failed")
                        ui.notify(f"Error: {exc}", type="negative")

                ui.button(action, on_click=_do_action).style(
                    f"background: {btn_color}; color: {btn_text_color}; "
                    f"border-radius: 8px; font-size: 13px; font-weight: 600; padding: 6px 16px;"
                )


# ─────────────────────────────────────────────────────────────────────────────
#  Notification action dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch_notification_action(action: str, ctx: dict, vault, refresh_fn) -> None:
    """Route a notification button click to the appropriate pipeline action."""
    notif_type = ctx.get("notification_type", "")
    a = action.lower()

    if notif_type == "scroll_approval" and a == "approve":
        scroll_id = ctx.get("scroll_id")
        if not scroll_id:
            ui.notify("Notification missing scroll_id — cannot approve.", type="negative")
            return
        # Phase 6 Batch 2 (6e): route through the unified inspect-before-approve
        # gate instead of the retired blind approve. open_scroll_review_dialog
        # renders the SAME unified Inbox card (risk pill, INSPECT,
        # WHAT-APPROVE-DOES) and resolves through the proven executor chain;
        # on_resolved re-renders this notifications list after the operator
        # picks an option. We return here so the trailing refresh_fn.refresh()
        # (which assumes a synchronous action completed) does not also fire.
        open_scroll_review_dialog(
            scroll_id,
            on_resolved=(lambda: refresh_fn.refresh()) if refresh_fn else None,
        )
        return

    elif notif_type == "forge_tool" and a == "forge":
        tool_id = ctx.get("tool_id")
        if not tool_id:
            ui.notify("Notification missing tool_id — cannot open forge.", type="negative")
            return
        # v0.9: the unified Decisions Inbox is the single forge-on-approve
        # executor for auto-proposed tools. Resolving the matching forge:<id>
        # gate runs the SAME executor the Inbox uses (resolve_gate ->
        # forge_tool_from_spec), so Forge here genuinely generates the code
        # exactly once. Fall back to the legacy spec-review dialog when no gate
        # row exists (e.g. a pre-migration proposed tool).
        if not _forge_via_inbox(tool_id, vault):
            _open_forge_dialog(tool_id)

    elif notif_type == "dep_approval" and action.startswith("Install "):
        package = action[len("Install "):].strip()
        tool_id = (ctx.get("pkg_tool_map") or {}).get(package)
        if not tool_id:
            ui.notify(f"No tool mapped for package '{package}'.", type="negative")
            return
        # v0.9: the unified Decisions Inbox is now the primary dep-approval
        # surface; this remains a working install-once fallback (the gate
        # dedups on dep:<package>, so it won't double-install).
        from systemu.runtime.dep_approvals import approve_and_install
        try:
            approve_and_install(tool_id=tool_id, package=package, source="dashboard")
            ui.notify(f"Approved + installed '{package}'. Re-running dry-run…", type="positive")
        except Exception as exc:
            logger.exception("[Notifications] dep approve+install failed")
            ui.notify(f"Install failed for '{package}': {exc}", type="negative")

    elif notif_type == "dep_reminder" and action.lower().startswith("review"):
        # v0.8.13 Fix 6d: route the operator into the spec/code review dialog.
        tool_id = ctx.get("tool_id")
        if not tool_id:
            ui.notify("Notification missing tool_id.", type="negative")
            return
        _open_forge_dialog(tool_id)

    else:
        # Informational notifications (OK / Reject / Skip / unknown types) — just dismiss
        ui.notify(f"Action '{action}' recorded.", type="positive")

    if refresh_fn:
        refresh_fn.refresh()


def _forge_via_inbox(tool_id: str, vault) -> bool:
    """Resolve the matching ``forge:<tool_id>`` gate via the unified Inbox.

    Returns True if a gate row was found and resolved (the SAME executor the
    Inbox uses runs: resolve_gate -> forge_tool_from_spec), so the tool forges
    exactly once. Returns False (no gate row / error) so the caller can fall
    back to the legacy spec-review dialog without double-forging."""
    try:
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.interface.command.inbox import resolve_gate

        queue = OperatorDecisionQueue(vault)
        dedup = f"forge:{tool_id}"
        match = next(
            (d for d in queue.list_pending() if d.dedup_key == dedup),
            None,
        )
        if match is None:
            return False
        resolved = queue.resolve(match.id, choice="Forge")
        resolve_gate(resolved, vault=vault)
        ui.notify("Forge approved; code generation started.", type="positive")
        return True
    except Exception as exc:
        logger.exception("[Notifications] Inbox forge resolve failed")
        ui.notify(f"Inbox forge failed ({exc}); opening review dialog",
                  type="warning")
        return False


def _open_forge_dialog(tool_id: str) -> None:
    """Open the Gate 1 → Gate 2 spec review dialog for a PROPOSED tool."""
    try:
        from systemu.interface.pages.tools import _show_spec_review_dialog
        _show_spec_review_dialog(tool_id)
    except Exception as exc:
        logger.exception("[Notifications] Could not open forge dialog")
        ui.notify(f"Could not open forge dialog: {exc}", type="negative")
