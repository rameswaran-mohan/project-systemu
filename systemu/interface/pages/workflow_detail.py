"""Workflow detail page — one workflow's full timeline.

Route: ``/workflow/<workflow_id>``

Shows where a specific workflow sits in the pipeline, the timestamps
of each stage transition, and links to the underlying scroll /
activity / shadow / execution.
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import THEME
from systemu.runtime.workflow_tracker import STAGES, WorkflowTracker


_STAGE_ICONS = {
    "capture":   "🎙️",
    "scroll":    "📜",
    "activity":  "📋",
    "execution": "⚙️",
    "done":      "✅",
    "failed":    "⚠️",
}

_STAGE_COLORS = {
    "capture":   THEME["info"],
    "scroll":    THEME["primary"],
    "activity":  "#a78bfa",
    "execution": THEME["warning"],
    "done":      THEME["success"],
    "failed":    THEME["danger"],
}


def build_workflow_detail_page(workflow_id: str) -> None:
    """Render the timeline + summary for a single workflow."""
    tracker = WorkflowTracker.get()
    snap = tracker.get_workflow(workflow_id)

    if snap is None:
        ui.label("Workflow not found").style(
            f"font-size: 20px; font-weight: 700; color: {THEME['text']}; "
            f"margin-bottom: 8px;"
        )
        ui.label(
            f"The tracker has no record of workflow_id={workflow_id!r}. "
            f"It may be from before the tracker was started, or the daemon "
            f"may have restarted recently."
        ).style(f"color: {THEME['text_muted']};")
        ui.button(
            "← Back to Overview",
            on_click=lambda: ui.navigate.to("/"),
        ).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 8px; "
            f"font-size: 12px; padding: 8px 14px; margin-top: 16px;"
        )
        return

    color = _STAGE_COLORS.get(snap.stage, THEME["text_muted"])
    icon  = _STAGE_ICONS.get(snap.stage, "•")

    # ── Header ─────────────────────────────────────────────────────────
    with ui.row().style("align-items: center; gap: 14px; margin-bottom: 8px;"):
        ui.label(icon).style("font-size: 28px;")
        ui.label(snap.title).style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )
    ui.label(f"workflow_id: {snap.workflow_id}").style(
        f"font-family: monospace; font-size: 12px; color: {THEME['text_muted']}; "
        f"margin-bottom: 16px;"
    )

    # ── Status row ─────────────────────────────────────────────────────
    with ui.row().classes("w-full gap-3 flex-wrap").style("margin-bottom: 20px;"):
        _stat("Stage",    snap.stage,    color)
        _stat("Status",   snap.status,   THEME["text"])
        _stat("Started",  _short_ts(snap.started_at), THEME["text_muted"])
        _stat("Updated",  _short_ts(snap.updated_at), THEME["text_muted"])

    # ── Stage timeline ─────────────────────────────────────────────────
    ui.label("Pipeline timeline").style(
        f"font-size: 15px; font-weight: 700; color: {THEME['text']}; margin-bottom: 12px;"
    )

    with ui.column().classes("w-full").style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 16px; gap: 0;"
    ):
        current_rank = STAGES.index(snap.stage) if snap.stage in STAGES else -1
        for idx, stage in enumerate(STAGES):
            entered_at = snap.timeline.get(stage)
            reached    = idx <= current_rank or entered_at is not None
            _timeline_row(stage, reached=reached, entered_at=entered_at)

    # ── Linked entities ────────────────────────────────────────────────
    ui.label("Linked entities").style(
        f"font-size: 15px; font-weight: 700; color: {THEME['text']}; margin: 24px 0 12px;"
    )
    with ui.column().classes("w-full").style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 4px 0; gap: 0; overflow: hidden;"
    ):
        if snap.scroll_id:
            _link_row("📜", "Scroll", snap.scroll_id, "/scrolls")
        if snap.activity_id:
            _link_row("📋", "Activity", snap.activity_id, "/activities")
        if snap.shadow_id:
            _link_row("👤", "Shadow", snap.shadow_id, "/army")
        if snap.execution_id:
            _link_row("⚙️", "Execution", snap.execution_id, "/systemu-chat")

    # ── Affinity log entries for this shadow (v0.4.4-b) ───────────────
    # Operator visibility into past TERMINATEs that affect routing.
    if snap.shadow_id:
        _build_affinity_log_panel(snap.shadow_id)

    # ── Supervisor Decision panel (v0.4.1-b) ──────────────────────────
    # Surfaces when the Intelligent Supervisor TERMINATEd this execution.
    # Three actions: retry with a different shadow, discard, or inspect the
    # audit file.  The supervisor recorded the termination to the affinity
    # log, so retry-with-different-shadow uses that to exclude the bad
    # specialist automatically.
    if snap.execution_id:
        _build_supervisor_decision_panel(
            execution_id=snap.execution_id,
            scroll_id=snap.scroll_id,
            shadow_id=snap.shadow_id,
            activity_id=snap.activity_id,
        )

    # ── Back button ────────────────────────────────────────────────────
    ui.button(
        "← Back to Overview",
        on_click=lambda: ui.navigate.to("/"),
    ).style(
        f"background: {THEME['surface2']}; color: {THEME['text']}; "
        f"border: 1px solid {THEME['border']}; border-radius: 8px; "
        f"font-size: 12px; padding: 8px 14px; margin-top: 24px;"
    )


def _stat(label: str, value: str, color: str) -> None:
    with ui.column().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 10px; padding: 10px 14px; min-width: 140px; flex: 1; gap: 2px;"
    ):
        ui.label(label).style(
            f"font-size: 10px; color: {THEME['text_muted']}; font-weight: 600; "
            f"letter-spacing: 0.08em; text-transform: uppercase;"
        )
        ui.label(value).style(
            f"font-size: 15px; font-weight: 700; color: {color};"
        )


def _timeline_row(stage: str, *, reached: bool, entered_at: str | None) -> None:
    color = _STAGE_COLORS.get(stage, THEME["text_muted"]) if reached else THEME["border"]
    icon  = _STAGE_ICONS.get(stage, "•")
    with ui.row().style(
        f"width: 100%; gap: 16px; padding: 10px 0; align-items: center;"
    ):
        ui.label(icon).style(f"font-size: 18px; opacity: {'1' if reached else '0.35'};")
        ui.label(stage.upper()).style(
            f"font-size: 12px; font-weight: 700; color: {color}; "
            f"letter-spacing: 0.06em; min-width: 90px;"
        )
        if entered_at:
            ui.label(_short_ts(entered_at)).style(
                f"font-size: 11px; color: {THEME['text_muted']}; font-family: monospace;"
            )
        elif not reached:
            ui.label("pending").style(
                f"font-size: 11px; color: {THEME['text_muted']}; font-style: italic;"
            )


def _link_row(icon: str, label: str, entity_id: str, route: str) -> None:
    with ui.row().style(
        f"width: 100%; gap: 12px; padding: 10px 16px; align-items: center; "
        f"border-bottom: 1px solid {THEME['border']}; cursor: pointer;"
    ).on("click", lambda _: ui.navigate.to(route)):
        ui.label(icon).style("font-size: 14px; min-width: 18px;")
        ui.label(label).style(
            f"font-size: 12px; color: {THEME['text_muted']}; font-weight: 700; "
            f"letter-spacing: 0.06em; min-width: 80px; text-transform: uppercase;"
        )
        ui.label(entity_id).style(
            f"font-family: monospace; font-size: 12px; color: {THEME['text']};"
        )


def _short_ts(iso: str) -> str:
    """Trim the ISO timestamp to a human-readable form (seconds precision)."""
    return (iso or "")[:19].replace("T", " ")


def _build_affinity_log_panel(shadow_id: str) -> None:
    """— show this shadow's recent TERMINATE entries in the
    affinity log.  Empty when the shadow has no terminations on record.
    """
    try:
        from systemu.runtime.affinity_log import get_affinity_log
        log = get_affinity_log()
        recent = log.recent_terminations(shadow_id=shadow_id, window_hours=168)  # 7 days
    except Exception:
        return
    if not recent:
        return

    ui.label("🔁 Recent affinity exclusions").style(
        f"font-size: 14px; font-weight: 700; color: {THEME['warning']}; "
        f"margin: 24px 0 8px;"
    )
    with ui.column().classes("w-full").style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 8px 0; gap: 0; overflow: hidden;"
    ):
        for t in recent[:6]:
            with ui.row().style(
                f"width: 100%; gap: 12px; padding: 8px 16px; align-items: center; "
                f"border-bottom: 1px solid {THEME['border']};"
            ):
                ui.label("⛔").style("font-size: 12px;")
                ui.label(f"intent {t.intent_hash}").style(
                    f"font-family: monospace; font-size: 11px; color: {THEME['text_muted']};"
                )
                ui.label(_short_ts(t.ts_iso)).style(
                    f"font-size: 11px; color: {THEME['text_muted']};"
                )
                ui.label(t.reason or "—").style(
                    f"font-size: 11px; color: {THEME['text']};"
                )
    ui.label(
        "Future routing will exclude this shadow from matching intent_hashes "
        "for 48 hours."
    ).style(f"font-size: 11px; color: {THEME['text_muted']}; margin-top: 6px;")


def _build_supervisor_decision_panel(
    *,
    execution_id: str,
    scroll_id:    str | None,
    shadow_id:    str | None,
    activity_id:  str | None,
) -> None:
    """— TERMINATE resolution UX.

    Shown only when the supervisor's audit file for this execution contains
    a TERMINATE entry.  Offers three operator actions:

      1. Retry with different shadow — re-queues the activity, excluding
         the shadow that just gave up (via affinity log).
      2. Discard — leaves the execution dead.
      3. Inspect audit — surfaces the per-execution audit log inline.

    Empty when there's no TERMINATE entry (most workflows).
    """
    from pathlib import Path as _P
    audit = _P("data") / "audit" / f"exec_{execution_id}" / "supervisor.jsonl"
    has_terminate = False
    rationale = ""
    if audit.exists():
        try:
            import json as _json
            for line in audit.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if row.get("action") == "TERMINATE":
                    has_terminate = True
                    rationale = row.get("rationale", "") or rationale
        except Exception:
            pass

    if not has_terminate:
        return

    ui.label("⚠️ Supervisor Decision").style(
        f"font-size: 15px; font-weight: 700; color: {THEME['warning']}; "
        f"margin: 24px 0 12px;"
    )
    with ui.column().classes("w-full").style(
        f"background: color-mix(in srgb, {THEME['warning']} 8%, {THEME['surface']}); "
        f"border: 1px solid color-mix(in srgb, {THEME['warning']} 40%, transparent); "
        f"border-radius: 12px; padding: 16px; gap: 12px;"
    ):
        ui.label(
            "The Intelligent Supervisor decided this execution could not "
            "succeed and called TERMINATE."
        ).style(f"font-size: 13px; color: {THEME['text']};")
        if rationale:
            ui.label(f"Reason: {rationale[:300]}").style(
                f"font-size: 12px; color: {THEME['text_muted']}; font-style: italic;"
            )

        with ui.row().style("gap: 10px; flex-wrap: wrap; margin-top: 4px;"):
            ui.button(
                "Retry with different shadow",
                on_click=lambda: _on_retry_different_shadow(
                    activity_id, shadow_id, scroll_id,
                ),
            ).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px; "
                f"font-size: 12px; font-weight: 600; padding: 8px 14px;"
            )
            ui.button(
                "Discard",
                on_click=lambda: ui.notify(
                    "Execution discarded — left at terminal state.", type="info",
                ),
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; "
                f"font-size: 12px; padding: 8px 14px;"
            )
            ui.button(
                "Inspect audit",
                on_click=lambda p=str(audit): ui.notify(
                    f"Audit log at {p} — open in your editor for the full timeline.",
                    type="info", multi_line=True,
                ),
            ).style(
                f"background: transparent; color: {THEME['text_muted']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; "
                f"font-size: 12px; padding: 8px 14px;"
            )


def _on_retry_different_shadow(
    activity_id: str | None,
    shadow_id:   str | None,
    scroll_id:   str | None,
) -> None:
    """Re-queue the activity, excluding the bad shadow via the affinity log.

    Supervisor.submit() now auto-consults the affinity log AND
    accepts ``exclude_shadow_id`` to force a swap.  The retry-button path
    just passes the bad shadow id and trusts the supervisor to pick an
    alternative whose skill_ids overlap with the activity's requirements.
    """
    if not activity_id:
        ui.notify("No activity_id on this workflow — cannot re-queue.", type="negative")
        return
    try:
        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.get()
        sub_id = sup.submit(
            activity_id=activity_id,
            shadow_id=shadow_id or "",
            priority=2,
            reason="operator_retry_different_shadow",
            retry_count=1,
            exclude_shadow_id=shadow_id,
            scroll_id=scroll_id,
        )
        ui.notify(
            f"Activity {activity_id[:12]}… re-queued ({sub_id}). "
            f"Supervisor swapped excluded shadow {(shadow_id or 'unknown')[:12]}… "
            "for an alternative.",
            type="positive", multi_line=True,
        )
    except Exception as exc:
        ui.notify(f"Retry failed: {exc}", type="negative")
