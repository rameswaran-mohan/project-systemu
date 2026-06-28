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


def _make_chat_stop_handler(ts: str):
    """Click handler: cooperative-cancel the chat task registered under `ts`."""
    def _stop(_=None):
        from systemu.runtime import chat_task_registry as _reg
        ok = False
        try:
            ok = _reg.request_cancel(ts)
        except Exception:
            ok = False
        try:
            from nicegui import ui as _ui
            _ui.notify("Stopping…" if ok else "Task is no longer running.",
                       type="warning" if ok else "info")
        except Exception:
            pass
    return _stop


def _work_link_for(activity) -> str:
    """Phase 6 Batch 2 (6g): the live Work-spine deep link for a completed task.

    ``run_direct_task`` returns the Activity (or None on early pipeline
    failure); the Activity's ``scroll_id`` doubles as its workflow_id, so the
    workflow detail page lives at ``/workflow/<scroll_id>``.  Falls back to the
    Work list ``/work`` when there is no scroll_id (None activity, or a shape
    without the field) so the link is always safe to render.
    """
    scroll_id = getattr(activity, "scroll_id", None)
    if scroll_id:
        return f"/workflow/{scroll_id}"
    return "/work"


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


def build_chat_page(prefill: str = "") -> None:
    """Render the chat page. ``prefill`` lands in the composer (W10.4)."""
    state  = AppState.get()
    vault  = state.vault
    config = state.config

    ui.label("Chat").style(
        f"font-size: 28px; font-weight: 800; color: {THEME['text']}; margin-bottom: 4px;"
    )
    ui.label(
        "Type a task in plain English — Quick mode answers in seconds."
    ).style(f"color: {THEME['text_muted']}; font-size: 14px; margin-bottom: 16px;")

    # ── Lane control + composer — FIRST (W11.2) ──────────────────────────────
    # Field report (2026-06-12): with any history the composer sat below the
    # fold and the operator had to scroll past up to 20 cards to type. The
    # input is the page's purpose — it renders before the history, autofocused.
    deployment = os.environ.get("SYSTEMU_MODE", "local").lower()
    with ui.row().classes("w-full items-center").style("gap: 12px; margin-bottom: 8px;"):
        ui.label("Mode:").style(
            f"font-size: 12px; color: {THEME['text_muted']}; font-weight: 600;"
        )
        lane = ui.radio(
            options={
                "quick":   "Quick answer (seconds)",
                "run_now": "Workflow — run now",
                "queue":   "Workflow — queue",
            },
            value="run_now",
        ).props("inline dense").style(f"color: {THEME['text']};")
        ui.label(f"mode: {deployment}").style(
            f"font-size: 11px; color: {THEME['text_muted']}; margin-left: auto;"
        )

    with ui.row().classes("w-full items-end").style("gap: 10px;"):
        prompt_input = ui.textarea(
            placeholder=(
                "Type a task, e.g.  take a screenshot of example.com and save to ~/Desktop/\n"
                "Use /continue to extend the previous task."
            )
        ).props("autofocus").style(
            f"flex: 1; background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 10px; padding: 12px; font-size: 14px; "
            f"color: {THEME['text']}; resize: vertical; min-height: 80px;"
        )

        submit_btn = ui.button("▶ Run Task").style(
            f"background: {THEME['primary']}; color: white; border-radius: 10px; "
            f"font-weight: 700; padding: 12px 20px; font-size: 14px; align-self: flex-end;"
        )

    ui.label("Enter to run  ·  Shift+Enter for a newline  ·  /continue extends the previous task").classes(
        "s-muted"
    ).style("font-size: 11px; margin-bottom: 4px;")
    status_label = ui.label("").style(
        f"font-size: 13px; color: {THEME['text_muted']}; min-height: 20px;"
    )

    ui.separator().style(f"background: {THEME['border']}; margin: 8px 0;")

    # ── History panel (below the composer; newest first) ─────────────────────
    history_col = ui.column().classes("w-full s-chat-history").style("gap: 10px; margin-bottom: 20px;")

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
            # W8.3: quick lane asked the operator a question.
            "needs_input":      THEME["warning"],
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
                with ui.column().classes("items-end").style("gap: 6px;"):
                    ui.badge(status.upper().replace("_", " ")).style(
                        f"background: {status_color}; color: white; "
                        f"border-radius: 6px; font-size: 11px; padding: 3px 8px; white-space: nowrap;"
                    )
                    # v0.9.50 (6c): per-job switches on the card's top-right — KILL a
                    # running job (cooperative cancel via the chat_task_registry token
                    # keyed on its ts, v0.9.32 D3.3), or RESTART a finished / killed /
                    # stuck job by re-running its prompt through the same path.
                    with ui.row().style("gap: 4px;"):
                        # KILL when the job is still in flight — a workflow run
                        # spends its active time at waiting_on_tools/pending_decision,
                        # not "running", so gate on all non-terminal states (the
                        # cancel_event is honored by both lanes). RESTART when terminal.
                        if status in ("running", "waiting_on_tools",
                                      "pending_decision", "needs_input"):
                            _raw_ts = entry.get("ts", "")
                            ui.button(icon="stop_circle",
                                      on_click=_make_chat_stop_handler(_raw_ts)).props(
                                "flat dense round size=sm color=negative"
                            ).tooltip("Kill this job")
                        elif status in ("success", "partial", "failed", "cancelled",
                                        "skipped_no_shadow"):
                            def _restart(_=None, _p=entry.get("prompt", "")):
                                try:
                                    prompt_input.set_value(_p)
                                    _on_submit()
                                except Exception:
                                    logger.debug("[ChatPage] restart re-submit failed", exc_info=True)
                            ui.button(icon="restart_alt", on_click=_restart).props(
                                "flat dense round size=sm color=primary"
                            ).tooltip("Run this task again")

            # W8.3: quick-lane entries carry the FULL answer — render it as
            # rich markdown (no 120-char truncation), list produced files,
            # and offer promotion into the factory pipeline.
            # W8.4: produced files render for EVERY entry that has them
            # (workflow runs now carry real files_produced too).
            for _f in (entry.get("files_produced") or []):
                ui.label(_f).classes("s-mono")

            if entry.get("lane") == "quick":
                _summary = entry.get("summary") or ""
                if _summary:
                    ui.markdown(_summary).classes("s-cell w-full")
                if entry.get("status") == "success":
                    async def _save_as_workflow(_=None, p=entry.get("prompt", "")):
                        import asyncio
                        # W7.1 pattern: promotion is an LLM call — run it off
                        # the loop and re-enter the captured client after.
                        try:
                            client = ui.context.client
                        except Exception:
                            client = None
                        ui.notify("Saving as workflow…", type="info")
                        try:
                            from systemu.pipelines.quick_task import promote_to_workflow
                            scroll = await asyncio.to_thread(
                                promote_to_workflow, p, config, vault)
                            msg = (f"Workflow '{getattr(scroll, 'name', '?')}' "
                                   "saved — review it in Work.")
                            typ = "positive"
                        except Exception as exc:
                            msg, typ = f"Could not save workflow: {exc}", "negative"
                        if client is not None:
                            try:
                                with client:
                                    ui.notify(msg, type=typ)
                            except Exception:
                                pass

                    from systemu.interface.design.primitives import button as _ds_btn
                    _ds_btn("Save as workflow", variant="ghost",
                            on_click=_save_as_workflow)

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
        # W7.2: per-client on_delete, NOT the global app.on_disconnect (any
        # client's transient drop killed this subscription process-wide).
        try:
            ui.context.client.on_delete(unsubscribe)
        except Exception:
            pass
    except Exception:
        pass

    # W10.4: a ?prefill= starter lands in the composer, ready to Run.
    if prefill:
        prompt_input.set_value(prefill)

    def _on_submit() -> None:
        raw = prompt_input.value.strip()
        if not raw:
            ui.notify("Please enter a task.", type="warning")
            return

        mode = lane.value or "quick"
        queue_mode = (mode == "queue")
        prompt_input.set_value("")
        status_label.set_text({
            "quick":   "Answering…",
            "queue":   "Queued — see Systemu Chat for progress",
            "run_now": "Running…",
        }.get(mode, "Running…"))
        # W7.4: do NOT disable the submit button — each submission runs in its
        # own thread, so concurrent chat tasks are fine. Disabling it for the
        # whole sync run made the UI itself serialize task submission.

        # Capture the NiceGUI client and target slot in the MAIN UI thread
        # while the slot stack is still set up.  The background thread below
        # has no slot context of its own, so re-entering the captured client
        # via `with client:` is how we make ui.timer (and any other UI ops)
        # work from inside the thread without `RuntimeError: The current
        # slot cannot be determined because the slot stack for this task is
        # empty.`
        client = ui.context.client

        # Phase 6 Batch 2 (6g): capture the run_direct_task return (the
        # Activity) so the completion handler can surface a live Work link.
        # A 1-slot list lets the daemon thread hand the result to _on_done
        # without a nonlocal/closure-rebind dance.
        result_holder: list = [None]

        # v0.9.32 (D3.2): register a cancel token for THIS chat submission so the
        # per-entry Stop button (chat_task_registry.request_cancel(ts)) can halt it.
        from datetime import datetime as _dt
        from systemu.runtime import chat_task_registry as _reg
        # v0.9.32 review fix 3A: MICROSECOND precision (not seconds). This id is
        # the cancel-registry key AND the chat-history entry id; at second
        # granularity two submissions within the same wall-clock second collide
        # — they share one cancel token (register is idempotent) and clobber each
        # other's chat-history rows. Microsecond keeps it a valid sortable
        # isoformat while making same-second submissions distinct.
        task_ts = _dt.now().isoformat()
        cancel_event = _reg.register(task_ts)

        def _run() -> None:
            try:
                if mode == "quick":
                    # W8.3: the fast lane — bounded ReAct loop, no scroll/
                    # activity/shadow creation. submit_quick_task keeps the
                    # chat-history contract so the thread below renders it.
                    # v0.9.32 fix: pass the SAME canonical `task_ts` as chat_ts so
                    # the appended chat-history entry id == the cancel-registry key
                    # — otherwise the per-entry Stop button never matches.
                    from systemu.pipelines.quick_task import submit_quick_task
                    submit_quick_task(raw, config, vault,
                                      chat_ts=task_ts, cancel_event=cancel_event)
                    return
                from systemu.pipelines.direct_task import run_direct_task
                result_holder[0] = run_direct_task(
                    raw, config, vault,
                    route_through_supervisor=queue_mode,
                    chat_ts=task_ts,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                logger.error("[ChatPage] task run failed: %s", exc)
            finally:
                # v0.9.32: always drop the cancel token (registry-leak guard).
                try:
                    _reg.unregister(task_ts)
                except Exception:
                    pass
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
            _render_history()
            # 6g: surface a live link into the Work spine for the task we just
            # created. Synchronous runs return the finished Activity; queued
            # runs return the queued Activity too (scroll_id known either way).
            # Skipped only when run_direct_task returned None (early failure).
            activity = result_holder[0]
            if activity is not None:
                link = _work_link_for(activity)
                ui.notify(
                    "Task submitted — open it in Work.",
                    type="positive",
                    actions=[{
                        "label": "View in Work",
                        "color": "white",
                        "handler": lambda: ui.navigate.to(link),
                    }],
                )

        threading.Thread(target=_run, daemon=True).start()

        # v0.9.50 (6b): surface the running task immediately — re-render the
        # history a few times so the just-appended "running" entry appears, and
        # scroll it into view so the operator sees the job without scrolling.
        def _surface(_=None) -> None:
            try:
                _render_history()
                ui.run_javascript(
                    "var el=document.querySelector('.s-chat-history'); "
                    "if(el){el.scrollIntoView({behavior:'smooth', block:'start'});}")
            except Exception:
                pass
        for _delay in (0.4, 1.2, 2.5):
            try:
                ui.timer(_delay, _surface, once=True)
            except Exception:
                pass

    submit_btn.on_click(_on_submit)

    def _on_enter(e) -> None:
        # v0.9.50: Enter runs the task; Shift+Enter inserts a newline (chat UX).
        # _on_submit reads the value at keydown (before any newline) then clears
        # the field, so the default newline on plain Enter is harmless.
        if not (getattr(e, "args", None) or {}).get("shiftKey"):
            _on_submit()
    prompt_input.on("keydown.enter", _on_enter, args=["shiftKey"])


# ── v0.7.2: tabbed wrapper — Compose + Live Events ─────────────────────────
# The Live tab calls the systemu_chat builder (formerly its own /systemu-chat
# route).  Lazy import keeps the chat_page module importable in environments
# where the supervisor's EventBus stack isn't installed (e.g. lightweight
# pytest collection).

_VALID_CHAT_TABS = ("compose", "live")


def build_chat_tabs(default_tab: str = "compose", prefill: str = "") -> None:
    """Two-tab chat: Compose (this page) + Live (supervisor event feed).

    Args:
        default_tab: ``"compose"`` or ``"live"``.  Anything else falls back
                     to ``"compose"``.
        prefill:     W10.4 — starter prompt landed in the composer (the
                     operator still clicks Run; never auto-submitted).
    """
    if default_tab not in _VALID_CHAT_TABS:
        default_tab = "compose"

    # Local import — systemu_chat pulls EventBus + Supervisor symbols that
    # are heavier than the chat-page surface needs at module import time.
    from systemu.interface.pages.systemu_chat import build_systemu_chat_page

    with ui.tabs().style(
        f"background: {THEME['surface']}; border-bottom: 1px solid {THEME['border']};"
    ) as tabs:
        ui.tab("compose", label="Compose")
        ui.tab("live", label="Live Events")

    with ui.tab_panels(tabs, value=default_tab).classes("w-full").style(
        "padding-top: 16px;"
    ):
        with ui.tab_panel("compose"):
            build_chat_page(prefill=prefill)
        with ui.tab_panel("live"):
            build_systemu_chat_page()
