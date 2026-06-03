"""Chat Page — direct free-text task interface.

Users type a natural-language task; the system runs it through the full
pipeline (scroll_refiner → activity_extractor → shadow_decision → runtime)
and shows live progress.

Prefix a message with /continue to link it to the most recent chat Scroll.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME
from systemu.interface.name_resolver import resolve_name, short_id

logger = logging.getLogger(__name__)


# Terminal chat statuses. Note both "failed" (extraction/decision crash, set by
# direct_task) and "failure" (ShadowRuntime result status) occur in practice.
_TERMINAL_STATUSES = {"success", "failure", "failed", "partial", "skipped_no_shadow", "cancelled"}


def _stale_terminal_ts(entries) -> set:
    """Timestamps of terminal entries that are NOT the newest entry — i.e. old,
    finished tasks that should not read as the current state. `entries` are in
    chat-history file order (oldest first)."""
    if not entries:
        return set()
    newest_ts = entries[-1].get("ts")
    return {
        e.get("ts") for e in entries
        if e.get("ts") != newest_ts and e.get("status") in _TERMINAL_STATUSES
    }


def _should_schedule_refresh(client) -> bool:
    """True iff the NiceGUI client is still connected.

    ui.timer fails ASYNCHRONOUSLY (inside _can_start -> client.connected())
    when the client was deleted, so a synchronous try/except around the timer
    cannot catch it. We must pre-check the connected flag before scheduling.
    Missing attribute / any error -> treat as not-connected (skip safely).
    """
    try:
        return bool(getattr(client, "has_socket_connection", False))
    except Exception:
        return False


def _default_dispatch_mode() -> str:
    """Pick a sensible default for the Run-now / Queue radio.

    local       → Run now (single machine, instant feedback)
    docker-*    → Queue   (workers run in separate containers/hosts)
    """
    mode = os.environ.get("SYSTEMU_MODE", "local").lower()
    return "queue" if mode.startswith("docker") else "run_now"


def build_chat_page() -> None:
    """Render the chat page."""
    state  = AppState.get()
    vault  = state.vault
    config = state.config

    ui.label("Chat").style(
        f"font-size: 28px; font-weight: 800; color: {THEME['text']}; margin-bottom: 4px;"
    )
    ui.label(
        "Type a task in plain English. Prefix with /continue to extend the previous task."
    ).style(f"color: {THEME['text_muted']}; font-size: 14px; margin-bottom: 24px;")

    # ── History panel ─────────────────────────────────────────────────────────
    history_col = ui.column().classes("w-full").style("gap: 10px; margin-bottom: 20px;")
    status_label = ui.label("").style(
        f"font-size: 13px; color: {THEME['text_muted']}; min-height: 20px;"
    )

    def _render_history() -> None:
        history_col.clear()
        entries = vault.load_chat_history(limit=20)
        if not entries:
            with history_col:
                ui.label("No chat tasks yet — type one below.").style(
                    f"color: {THEME['text_muted']}; font-size: 14px;"
                )
            return
        stale = _stale_terminal_ts(entries)
        with history_col:
            with ui.row().classes("w-full justify-end"):
                ui.button(
                    "Clear history",
                    on_click=lambda: (vault.clear_chat_history(), _render_history()),
                ).props("flat dense").style(
                    f"color: {THEME['text_muted']}; font-size: 11px;"
                )
            for entry in reversed(entries):
                _render_entry(entry, is_stale=entry.get("ts") in stale)

    def _render_entry(entry: Dict[str, Any], is_stale: bool = False) -> None:
        status   = entry.get("status", "?")
        prompt   = entry.get("prompt", "")
        ts       = entry.get("ts", "")[:19].replace("T", " ")
        sid      = entry.get("shadow_id", "")
        exec_id  = entry.get("execution_id", "")

        status_color = {
            "success":          THEME.get("success", "#22c55e"),
            "partial":          THEME.get("warning", "#f59e0b"),
            "failed":           "#ef4444",
            "running":          THEME.get("primary", "#6366f1"),
            "skipped_no_shadow":"#94a3b8",
            "waiting_on_tools": THEME.get("warning", "#f59e0b"),
            "pending_decision": THEME.get("warning", "#f59e0b"),
        }.get(status, THEME.get("text_muted", "#94a3b8"))

        card_opacity = "opacity: 0.55; " if is_stale else ""
        with ui.card().classes("w-full").style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 12px; padding: 14px 18px; {card_opacity}"
        ):
            with ui.row().classes("w-full items-start justify-between"):
                with ui.column().style("gap: 4px; flex: 1;"):
                    ui.label(prompt[:120] + ("…" if len(prompt) > 120 else "")).style(
                        f"font-size: 15px; font-weight: 600; color: {THEME['text']};"
                    )
                    meta = ("previous · " + ts) if is_stale else ts
                    if sid:
                        meta += f"  ·  shadow: {resolve_name(sid, vault)}"
                    if exec_id:
                        meta += f"  ·  exec: {short_id(exec_id)}"
                    miss = entry.get("missing_tools") or []
                    if status == "waiting_on_tools" and miss:
                        meta += "  ·  needs: " + ", ".join(miss[:4])
                    ui.label(meta).style(
                        f"font-size: 11px; color: {THEME['text_muted']};"
                    )
                ui.badge(status.upper().replace("_", " ")).style(
                    f"background: {status_color}; color: white; "
                    f"border-radius: 6px; font-size: 11px; padding: 3px 8px; white-space: nowrap;"
                )

            # v0.8.22 (C): if this entry is parked on a pending operator decision,
            # render the inline card so the operator can resolve in chat.
            if entry.get("status") == "pending_decision" and entry.get("decision_id"):
                _render_pending_decision_inline(vault, entry)

    def _render_pending_decision_inline(vlt, entry):
        from systemu.interface.components.pending_decision_card import build_pending_decision_card
        from systemu.approval.decision_queue import OperatorDecisionQueue
        try:
            queue = OperatorDecisionQueue(vlt)
            dec = vlt.get_decision(entry["decision_id"])
            if dec.status != "pending":
                return  # already resolved elsewhere; nothing to render
            build_pending_decision_card(
                dec.to_dict(), queue,
                on_resolved=lambda: _render_history(),
            )
        except Exception as exc:
            ui.label(f"[card unavailable: {exc}]").style(
                f"font-size: 11px; color: {THEME['text_muted']};"
            )

    _render_history()

    # v0.8.22 (C): live refresh when decisions are posted/resolved for any
    # chat-tied submission. Cheap: just re-render the history.
    try:
        from systemu.interface.event_bus import EventBus
        def _on_event(ev):
            cat = ev.get("category")
            if cat in ("operator_decision_posted", "operator_decision_resolved"):
                try:
                    ui.timer(0, _render_history, once=True)
                except Exception:
                    pass
        unsubscribe = EventBus.get().subscribe(_on_event, replay=False)
        try:
            from nicegui import app
            app.on_disconnect(lambda: unsubscribe())
        except Exception:
            pass
    except Exception:
        pass

    ui.separator().style(f"background: {THEME['border']}; margin: 8px 0;")

    # ── Dispatch-mode toggle (Run now vs Queue) ───────────────────────────────
    default_mode = _default_dispatch_mode()
    deployment = os.environ.get("SYSTEMU_MODE", "local").lower()
    with ui.row().classes("w-full items-center").style("gap: 12px; margin-bottom: 8px;"):
        ui.label("Dispatch:").style(
            f"font-size: 12px; color: {THEME['text_muted']}; font-weight: 600;"
        )
        dispatch = ui.radio(
            options={"run_now": "Run now (synchronous)", "queue": "Queue (via Supervisor)"},
            value=default_mode,
        ).props("inline dense").style(f"color: {THEME['text']};")
        ui.label(f"mode: {deployment}").style(
            f"font-size: 11px; color: {THEME['text_muted']}; margin-left: auto;"
        )

    # ── Input area ────────────────────────────────────────────────────────────
    with ui.row().classes("w-full items-end").style("gap: 10px;"):
        prompt_input = ui.textarea(
            placeholder=(
                "Type a task, e.g.  take a screenshot of example.com and save to ~/Desktop/\n"
                "Use /continue to extend the previous task."
            )
        ).style(
            f"flex: 1; background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 10px; padding: 12px; font-size: 14px; "
            f"color: {THEME['text']}; resize: vertical; min-height: 80px;"
        )

        submit_btn = ui.button("▶ Run Task").style(
            f"background: {THEME['primary']}; color: white; border-radius: 10px; "
            f"font-weight: 700; padding: 12px 20px; font-size: 14px; align-self: flex-end;"
        )

    def _on_submit() -> None:
        raw = prompt_input.value.strip()
        if not raw:
            ui.notify("Please enter a task.", type="warning")
            return

        queue_mode = (dispatch.value == "queue")
        prompt_input.set_value("")
        status_label.set_text("⏳ Queued — see Systemu Chat for progress" if queue_mode else "⏳ Running…")
        submit_btn.set_enabled(False)

        # Capture the NiceGUI client and target slot in the MAIN UI thread
        # while the slot stack is still set up.  The background thread below
        # has no slot context of its own, so re-entering the captured client
        # via `with client:` is how we make ui.timer (and any other UI ops)
        # work from inside the thread without `RuntimeError: The current
        # slot cannot be determined because the slot stack for this task is
        # empty.`
        client = ui.context.client

        def _run() -> None:
            try:
                from systemu.pipelines.direct_task import run_direct_task
                run_direct_task(
                    raw, config, vault,
                    route_through_supervisor=queue_mode,
                )
            except Exception as exc:
                logger.error("[ChatPage] run_direct_task failed: %s", exc)
            finally:
                # v0.8.11 RC2: ui.timer fails asynchronously when the client
                # navigated away — a sync try/except can't catch it. Pre-check
                # the connection so the post-run refresh is skipped cleanly.
                try:
                    if _should_schedule_refresh(client):
                        with client:
                            ui.timer(0.1, _on_done, once=True)
                except Exception:
                    logger.debug("[ChatPage] post-run UI refresh skipped — client unavailable")

        def _on_done() -> None:
            status_label.set_text("")
            submit_btn.set_enabled(True)
            _render_history()

        threading.Thread(target=_run, daemon=True).start()

    submit_btn.on_click(_on_submit)
    prompt_input.on("keydown.ctrl.enter", _on_submit)


# ── v0.7.2: tabbed wrapper — Compose + Live Events ─────────────────────────
# The Live tab calls the systemu_chat builder (formerly its own /systemu-chat
# route).  Lazy import keeps the chat_page module importable in environments
# where the supervisor's EventBus stack isn't installed (e.g. lightweight
# pytest collection).

_VALID_CHAT_TABS = ("compose", "live")


def build_chat_tabs(default_tab: str = "compose") -> None:
    """Two-tab chat: Compose (this page) + Live (supervisor event feed).

    Args:
        default_tab: ``"compose"`` or ``"live"``.  Anything else falls back
                     to ``"compose"``.
    """
    if default_tab not in _VALID_CHAT_TABS:
        default_tab = "compose"

    # Local import — systemu_chat pulls EventBus + Supervisor symbols that
    # are heavier than the chat-page surface needs at module import time.
    from systemu.interface.pages.systemu_chat import build_systemu_chat_page

    with ui.tabs().style(
        f"background: {THEME['surface']}; border-bottom: 1px solid {THEME['border']};"
    ) as tabs:
        ui.tab("compose", label="💬 Compose")
        ui.tab("live", label="📡 Live Events")

    with ui.tab_panels(tabs, value=default_tab).classes("w-full").style(
        "padding-top: 16px;"
    ):
        with ui.tab_panel("compose"):
            build_chat_page()
        with ui.tab_panel("live"):
            build_systemu_chat_page()
