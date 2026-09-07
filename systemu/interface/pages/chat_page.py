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
import time
from datetime import datetime
from typing import Any, Dict

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME
from systemu.interface.name_resolver import resolve_name, short_id

logger = logging.getLogger(__name__)


# Terminal chat statuses. Note both "failed" (extraction/decision crash, set by
# direct_task) and "failure" (ShadowRuntime result status) occur in practice.
_TERMINAL_STATUSES = {"success", "failure", "failed", "partial", "skipped_no_shadow",
                      "cancelled", "spend_cap_reached"}

# D6: one vocabulary for "not finished", owned by the runtime module that owns
# chat-task state — the page must not keep a second, drifting copy.
from systemu.runtime.chat_task_registry import (          # noqa: E402
    ACTIVE_CHAT_STATUSES as _ACTIVE_STATUSES,
)

# D5: how long the page waits for the submission event before telling the
# operator it could not submit. Generous — the workflow lane refines a Scroll
# (one LLM round-trip) before its chat-history row exists — but BOUNDED, because
# an unbounded wait is the silence this defect is about.
_ACK_TIMEOUT_SECONDS = 180.0
_ACK_POLL_SECONDS = 0.35


def _live_supervisor():
    """The process Supervisor, or None when it was never started here."""
    try:
        from systemu.runtime.supervisor import Supervisor
        return Supervisor.get()
    except Exception:
        return None


# ── D5/D6 (dogfood 0.10.28) — the submit + stop contract ────────────────────
#
# D5: the operator clicked Run Task twice and watched the composer empty itself
# while no task was ever created and nothing said why. The rule that closes it:
# the composer clears ONLY once the submission has been ACKNOWLEDGED — an id
# came back AND the submission event (the chat-history row) was witnessed. Every
# other outcome — exception, timeout, no id, refused because something else is
# running — leaves the text exactly where the operator typed it and names the
# failure out loud.
#
# D6: a task parked on a gate swallowed new submissions, /continue and the Stop
# button alike. Refusals now say WHY and what to do, Stop really cancels through
# the runtime cancel entry, and a RUNNING row that no worker owns is labelled as
# restored rather than dressed up as live work.

_SUBMIT_FAILED_SUFFIX = "Your text is still here."


def submit_failed_message(reason: str) -> str:
    """The one failure line for every unacknowledged submission (D5). ASCII."""
    reason = (str(reason) or "").strip() or "the task was not created"
    if reason.endswith("."):
        reason = reason[:-1]
    return f"Could not submit: {reason}. {_SUBMIT_FAILED_SUFFIX}"


def _task_when(entry: Dict[str, Any]) -> str:
    return str(entry.get("ts") or "")[:19].replace("T", " ")


def find_active_chat_task(entries) -> Any:
    """The newest chat-history row that has not finished, or None (D6a).

    This is what makes a refusal POSSIBLE to explain: 0.10.28 had no notion of
    "something is already running", so a submission that went nowhere looked
    exactly like one that went somewhere.
    """
    from systemu.runtime.chat_task_registry import ACTIVE_CHAT_STATUSES
    for entry in reversed(list(entries or [])):
        if isinstance(entry, dict) and entry.get("status") in ACTIVE_CHAT_STATUSES:
            return entry
    return None


def active_task_toast(entry: Dict[str, Any], *, live: bool) -> str:
    """D6(a): why this submission was refused, and what to do about it. ASCII."""
    from systemu.runtime.chat_task_registry import task_title
    head = f"A task is running ({task_title(entry)}, since {_task_when(entry)})."
    if live:
        return head + " Stop it or wait."
    return (head + " Nothing is working on it - it was restored after a restart."
            " Stop it to submit a new task.")


def continue_waiting_toast(entry: Dict[str, Any]) -> str:
    """D6(c): what /continue is actually waiting on, and where to resolve it."""
    from systemu.runtime.chat_task_registry import task_title
    title = task_title(entry)
    status = entry.get("status")
    if status == "pending_decision":
        return (f"That task ({title}) is waiting on your approval. Resolve its "
                f"card in Notifications, then it continues on its own.")
    if status == "waiting_on_tools":
        missing = ", ".join(list(entry.get("missing_tools") or [])[:4])
        tail = f": {missing}" if missing else ""
        return (f"That task ({title}) is waiting on tools{tail}. Enable them in "
                f"the Tools Registry, then it continues on its own.")
    if status == "needs_input":
        return (f"That task ({title}) is waiting on an answer from you. Resolve "
                f"its card in Notifications, then it continues on its own.")
    return (f"That task ({title}) is still running, so /continue has nothing to "
            f"extend yet. Wait for it, or stop it first.")


def restored_task_note(entry: Dict[str, Any], *, live: bool) -> str:
    """D6(d): the card note for a non-terminal row that no worker owns. ASCII."""
    from systemu.runtime.chat_task_registry import ACTIVE_CHAT_STATUSES
    if live or entry.get("status") not in ACTIVE_CHAT_STATUSES:
        return ""
    return "restored; not being worked - stop or resume"


def stop_result_message(report: Dict[str, Any]):
    """Turn a ``chat_task_registry.cancel_chat_task`` report into (text, type).

    D6(b): the operator either sees "Stopped <title>" or the exact reason it
    could not be stopped. There is no third, silent outcome.
    """
    title = report.get("title") or "the task"
    if not report.get("stopped"):
        reason = report.get("reason") or "the runtime gave no reason"
        return f"Could not stop {title}: {reason}", "warning"
    msg = f"Stopped {title}."
    if not report.get("live"):
        msg += " It was not being worked - the record is cleared."
    note = report.get("gate_note") or ""
    if note:
        msg += f" Its pending approval card is left with a note: {note}."
    return msg, "positive"


def submission_witnessed(entries, task_id: str) -> bool:
    """D5 acknowledgement: has the submission event landed for ``task_id``?

    The chat-history row IS the submission event — both lanes write it (the
    quick lane immediately, the workflow lane the moment its Scroll refines) and
    both key it on the id the page generated. A run that dies before that row
    exists produced no task, which is precisely the operator's witness.
    """
    if not task_id:
        return False
    for entry in list(entries or []):
        if isinstance(entry, dict) and entry.get("ts") == task_id:
            return True
    return False


def handle_chat_submit(*, text, entries, start, await_ack, notify, clear):
    """THE chat submit chokepoint (D5 + D6a/c). Returns the acknowledged id or None.

    Order is the contract, not a detail:
      1. refuse an empty prompt;
      2. refuse while another task is unfinished, saying WHY (and, for
         /continue, what that task is waiting on) - the text is kept;
      3. ``start()`` -> an id, or a named failure with the text kept;
      4. ``await_ack(id)`` -> the submission event witnessed, or a named failure
         with the text kept;
      5. ONLY THEN ``clear()``.

    ``start`` / ``await_ack`` / ``notify`` / ``clear`` are injected so this seam
    is exercised without a NiceGUI client, a vault, an LLM or a daemon.
    """
    text = (text or "").strip()
    if not text:
        notify("Please enter a task.", type="warning")
        return None

    active = find_active_chat_task(entries)
    if active is not None:
        from systemu.runtime.chat_task_registry import chat_task_is_live
        if text.lower().startswith("/continue"):
            message = continue_waiting_toast(active)
        else:
            message = active_task_toast(active, live=chat_task_is_live(active))
        notify(message, type="warning")
        return None

    try:
        task_id = start()
    except Exception as exc:
        notify(submit_failed_message(exc), type="negative")
        return None
    if not task_id:
        notify(submit_failed_message("no task id came back"), type="negative")
        return None

    try:
        acknowledged, reason = await_ack(task_id)
    except Exception as exc:
        acknowledged, reason = False, str(exc)
    if not acknowledged:
        notify(submit_failed_message(reason), type="negative")
        return None

    clear()
    notify("Task submitted.", type="positive")
    return task_id


def _make_chat_stop_handler(ts: str, vault: Any = None, on_done=None):
    """Click handler: STOP the chat task recorded under `ts` (D6b).

    0.10.28 only set an in-process flag, so a task restored after a daemon
    restart - the exact case the operator hit - could not be stopped at all and
    was told "Task is no longer running" while its row stayed RUNNING forever.
    The handler now goes through the runtime cancel entry, which also withdraws
    the pending gate card and writes the terminal state durably.
    """
    def _stop(_=None):
        from systemu.runtime.chat_task_registry import cancel_chat_task
        from systemu.interface.dashboard_state import AppState
        vlt = vault
        if vlt is None:
            try:
                vlt = AppState.get().vault
            except Exception:
                vlt = None
        try:
            supervisor = None
            try:
                from systemu.runtime.supervisor import Supervisor
                supervisor = Supervisor.get()
            except Exception:
                supervisor = None
            report = cancel_chat_task(vlt, ts, supervisor=supervisor)
        except Exception as exc:   # never leave the click unanswered
            report = {"stopped": False, "title": ts, "live": False,
                      "gate_note": "", "reason": str(exc)}
        message, kind = stop_result_message(report)
        try:
            from nicegui import ui as _ui
            _ui.notify(message, type=kind)
        except Exception:
            pass
        if report.get("stopped") and on_done is not None:
            try:
                on_done()
            except Exception:
                logger.debug("[ChatPage] post-stop refresh failed", exc_info=True)
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
            # R-P3b: reached its spend cap — a budget stop, not a failure.
            "spend_cap_reached": THEME["warning"],
        }.get(status, THEME.get("text_muted", "#94a3b8"))

        # D6(d): ask the runtime whether anything is actually working this row.
        try:
            from systemu.runtime.chat_task_registry import chat_task_is_live
            _is_live = chat_task_is_live(entry, supervisor=_live_supervisor())
        except Exception:
            _is_live = True   # unsure -> do not accuse a live run of being dead

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
                    # R-P3a: the quick-lane per-run cost drill-down. The Home daily
                    # total spans BOTH lanes; without this the quick lane was the one
                    # lane with no per-run cost surface (its runs aren't Work rows).
                    # Key on the entry's DURABLE cost rows (persisted onto the entry so
                    # the chip survives a reload); fall back to the live ledger by eid.
                    try:
                        from systemu.runtime import costing as _costing
                        _chip = _costing.cost_chip_for(entry.get("cost") or exec_id)
                        if _chip:
                            meta += "  ·  " + _chip
                    except Exception:
                        pass
                    miss = entry.get("missing_tools") or []
                    if status == "waiting_on_tools" and miss:
                        meta += "  ·  needs: " + ", ".join(miss[:4])
                    ui.label(meta).style(
                        f"font-size: 11px; color: {THEME['text_muted']};"
                    )
                    # D6(d): a row that came back RUNNING after a daemon restart
                    # with no worker behind it must not read as live work.
                    # The warn tint comes from the `s-text-warn` token class
                    # (var(--color-warn)) — no inline colour, no raw hex, so the
                    # palette stays editable in design/tokens.py alone.
                    _note = restored_task_note(entry, live=_is_live)
                    if _note:
                        ui.label(_note).classes("s-text-warn").style(
                            "font-size: 11px; font-weight: 600;"
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
                        if status in _ACTIVE_STATUSES:
                            _raw_ts = entry.get("ts", "")
                            ui.button(
                                icon="stop_circle",
                                on_click=_make_chat_stop_handler(
                                    _raw_ts, vault, on_done=_render_history),
                            ).props(
                                "flat dense round size=sm color=negative"
                            ).tooltip("Stop this task")
                        elif status in ("success", "partial", "failed", "cancelled",
                                        "skipped_no_shadow", "spend_cap_reached"):
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
        # D5: the composer is NOT cleared here. `handle_chat_submit` clears it,
        # and only once the submission has been acknowledged.
        raw = (prompt_input.value or "").strip()

        mode = lane.value or "quick"
        queue_mode = (mode == "queue")
        # W7.4: do NOT disable the submit button — each submission runs in its
        # own thread, so concurrent chat tasks are fine. Disabling it for the
        # whole sync run made the UI itself serialize task submission.

        # Capture the NiceGUI client and target slot in the MAIN UI thread
        # while the slot stack is still set up.  The background threads below
        # have no slot context of their own, so re-entering the captured client
        # via `with client:` is how we make ui.timer (and any other UI ops)
        # work from inside a thread without `RuntimeError: The current
        # slot cannot be determined because the slot stack for this task is
        # empty.`
        client = ui.context.client

        def _ui_call(fn) -> None:
            """Run `fn` inside the captured client, from any thread."""
            try:
                if not _should_schedule_refresh(client):
                    return
                with client:
                    ui.timer(0.01, fn, once=True)
            except Exception:
                logger.debug("[ChatPage] UI call skipped — client unavailable")

        def _notify(message, **kwargs) -> None:
            _ui_call(lambda: ui.notify(message, **kwargs))

        def _clear() -> None:
            # Only wipe what the operator actually submitted: if they typed a
            # new draft while the first submission was being acknowledged,
            # clearing would eat it.
            def _do():
                if (prompt_input.value or "").strip() == raw.strip():
                    prompt_input.set_value("")
            _ui_call(_do)

        # Phase 6 Batch 2 (6g): capture the run_direct_task return (the
        # Activity) so the completion handler can surface a live Work link.
        # A 1-slot list lets the daemon thread hand the result to _on_done
        # without a nonlocal/closure-rebind dance.
        result_holder: list = [None]
        run_error: list = []
        finished = threading.Event()

        from datetime import datetime as _dt
        from systemu.runtime import chat_task_registry as _reg

        def _run(task_ts, cancel_event) -> None:
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
                run_error.append(exc)
            finally:
                finished.set()
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

        def _start() -> str:
            """Begin the run and return the id it will be recorded under."""
            # v0.9.32 review fix 3A: MICROSECOND precision (not seconds). This id
            # is the cancel-registry key AND the chat-history entry id; at second
            # granularity two submissions within the same wall-clock second
            # collide — they share one cancel token (register is idempotent) and
            # clobber each other's chat-history rows.
            task_ts = _dt.now().isoformat()
            cancel_event = _reg.register(task_ts)
            try:
                threading.Thread(target=_run, args=(task_ts, cancel_event),
                                 name=f"chat-submit-{task_ts}", daemon=True).start()
            except Exception:
                _reg.unregister(task_ts)
                raise
            _ui_call(lambda: status_label.set_text({
                "quick":   "Answering…",
                "queue":   "Queued — see Systemu Chat for progress",
                "run_now": "Running…",
            }.get(mode, "Running…")))
            return task_ts

        def _await_ack(task_id):
            """Wait for the submission event — the chat-history row (D5).

            Both lanes write that row from the id we just handed them, so its
            appearance is the first honest evidence that a task EXISTS. A run
            that dies before it (the witnessed defect: scroll refinement failing)
            reaches the `finished` branch and the operator keeps their text.

            R-UX2: this waits, so it must never wait on the event loop. It is
            only ever reached through ``handle_chat_submit`` from ``_submit``,
            and ``_submit`` is only ever started on the ``chat-submit-gate``
            daemon thread below — there is no other call site. Every UI effect
            it produces (``_notify`` / ``_clear``) is handed back to the loop
            through ``_ui_call``, never touched from here.
            """
            def _witnessed():
                return submission_witnessed(vault.load_chat_history(limit=20), task_id)

            deadline = time.monotonic() + _ACK_TIMEOUT_SECONDS
            while True:
                try:
                    if _witnessed():
                        return True, ""
                except Exception as exc:
                    return False, f"could not read the task list ({exc})"
                if finished.is_set():
                    # Re-read once: the row may have landed between the two
                    # checks above. Only then call it a non-submission.
                    try:
                        if _witnessed():
                            return True, ""
                    except Exception:
                        pass
                    if run_error:
                        return False, str(run_error[0])
                    return False, "the pipeline stopped before creating the task"
                if time.monotonic() >= deadline:
                    return False, "timed out waiting for the task to be created"
                # offload-lint: ok — runs on the "chat-submit-gate" daemon
                # thread started at the foot of _on_submit
                # (threading.Thread(target=_submit, name="chat-submit-gate")),
                # never on the event loop; see this function's docstring.
                time.sleep(_ACK_POLL_SECONDS)

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
                    "Task finished — open it in Work.",
                    type="positive",
                    actions=[{
                        "label": "View in Work",
                        "color": "white",
                        "handler": lambda: ui.navigate.to(link),
                    }],
                )

        def _submit() -> None:
            try:
                entries = vault.load_chat_history(limit=20)
            except Exception as exc:
                logger.debug("[ChatPage] chat history unreadable at submit", exc_info=True)
                _notify(submit_failed_message(f"the task list is unreadable ({exc})"),
                        type="negative")
                return
            handle_chat_submit(
                text=raw, entries=entries,
                start=_start, await_ack=_await_ack,
                notify=_notify, clear=_clear,
            )

        # The whole chokepoint runs off the UI thread: the blocker read touches
        # disk and the acknowledgement waits on the pipeline, and doing either
        # inline is exactly the "renderer freeze" the operator reported.
        threading.Thread(target=_submit, name="chat-submit-gate", daemon=True).start()

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
