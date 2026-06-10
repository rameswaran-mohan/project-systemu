"""NiceGUI Dashboard — Shadows page (route /shadows, storage key shadow_army).

Card grid of all Shadows with:
  * Status badge, avatar emoji, description preview
  * Skill / Tool counts
  * Execute button (dry-run) + Show Details dialog
  * Awaken dialog with persona dimension sliders
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME, status_badge_html
from systemu.interface.jobs import JobManager, JobStatus, Job
from systemu.interface.components.entity_edit import open_shadow_edit_dialog
from systemu.interface.name_resolver import resolve_names


_STATUS_ICON = {
    "awakened": "⚡",
    "dormant":  "💤",
    "retired":  "🪦",
}


def _memory_consolidation_route() -> str:
    """Phase 5 Slice 4d-2 — entry point for the memory-consolidation surface.

    Per the IA (spec §5), consolidation belongs to Shadows.  The surface
    (``build_memory_consolidation_page`` — Run-All + per-shadow Consolidate)
    still lives at this route; the Shadows page just surfaces it from here.
    """
    return "/insights?tab=memory"


def _build_execute_jobs_panel_view_model(job_manager) -> dict:
    """Pure-data view model for the Recent Execute Jobs panel.

    Returns:
      {
        "queued":    [{id, name, position, dedup_key, type, ...}, ...],
        "running":   [{id, name, started_at, type, ...}, ...],
        "completed": [{id, name, status, finished_at, type, ...}, ...],
      }

    Total entries across the three groups is capped at 30 (spec acceptance #5).
    Non-execute jobs (capture/forge/etc.) are filtered out.
    """
    queued: list = []
    running: list = []
    completed: list = []
    for j in job_manager.jobs.values():
        if j.type != "execute":
            continue
        entry = {
            "id":         j.id,
            "name":       j.name,
            "type":       j.type,
            "status":     j.status.value,
            "dedup_key":  getattr(j, "dedup_key", ""),
            "start_time": j.start_time.isoformat(),
        }
        if j.status == JobStatus.QUEUED:
            queued.append(entry)
        elif j.status in (JobStatus.RUNNING, JobStatus.STOPPING):
            running.append(entry)
        else:
            completed.append(entry)
    # Sort: queued oldest-first (position #1 = next to run); completed newest-first
    queued.sort(key=lambda e: e["start_time"])
    running.sort(key=lambda e: e["start_time"])
    completed.sort(key=lambda e: e["start_time"], reverse=True)
    # Position numbers for queued
    for i, e in enumerate(queued):
        e["position"] = i + 1
    # Cap total at 30: keep all queued + running, fill rest with most-recent completed
    available = 30 - len(queued) - len(running)
    if available < 0:
        available = 0
    completed = completed[:available]
    return {"queued": queued, "running": running, "completed": completed}


@ui.refreshable
def _render_execute_jobs_panel() -> None:
    """v0.8.6: Recent Execute Jobs panel — queued/running/completed."""
    jm = JobManager.get()
    vm = _build_execute_jobs_panel_view_model(jm)

    with ui.row().style("gap: 12px; width: 100%; margin-bottom: 16px;"):
        for label, key, color in (
            ("⏳ Queued", "queued", THEME["warning"]),
            ("▶️ Running", "running", THEME["primary"]),
            ("✓ Completed", "completed", THEME["success"]),
        ):
            with ui.card().style(
                f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
                f"border-radius: 12px; padding: 16px; flex: 1; min-height: 120px;"
            ):
                ui.label(f"{label} ({len(vm[key])})").style(
                    f"font-size: 13px; font-weight: 700; color: {color}; margin-bottom: 8px;"
                )
                for entry in vm[key]:
                    with ui.row().style("gap: 6px; align-items: center; margin-bottom: 4px;"):
                        if key == "queued":
                            ui.label(f"#{entry['position']}").style(
                                f"color: {THEME['text_muted']}; font-size: 11px; min-width: 24px;"
                            )
                        ui.label(entry["name"]).style(
                            f"color: {THEME['text']}; font-size: 12px; flex: 1;"
                        )
                        if key in ("queued", "running"):
                            def _make_cancel(jid=entry["id"], status=entry["status"]):
                                def _click(_e=None):
                                    if status == "queued":
                                        ok = jm.cancel_queued(jid)
                                    else:
                                        jm.cancel_job_hard(jid)
                                        ok = True
                                    if ok:
                                        ui.notify("Cancelled", type="positive")
                                        _render_execute_jobs_panel.refresh()
                                return _click
                            ui.button("Cancel", on_click=_make_cancel()).style(
                                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                                f"font-size: 10px; padding: 2px 8px; border-radius: 4px;"
                            )
                if not vm[key]:
                    ui.label("—").style(f"color: {THEME['text_muted']}; font-size: 12px;")


def _format_relative_time(target_dt) -> str:
    """Return 'in 23 min' / 'in 2h 14m' / 'now' / 'overdue'."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = target_dt - now
    secs = int(delta.total_seconds())
    if secs < 0:
        return "overdue"
    if secs < 60:
        return "now"
    mins = secs // 60
    if mins < 60:
        return f"in {mins} min"
    hours = mins // 60
    rem = mins % 60
    if hours < 24:
        return f"in {hours}h {rem}m"
    days = hours // 24
    return f"in {days}d {hours % 24}h"


def _build_schedules_list_view_model(vault) -> list:
    """Pure-data view model for the Schedules list panel."""
    from systemu.scheduler.schedule_registry import list_active_schedules
    shadows = {s.get("id"): s.get("name", s.get("id")) for s in vault.list_shadows()}
    try:
        scrolls = {s["id"]: s.get("name", s["id"]) for s in vault.load_index("scrolls")}
    except Exception:
        scrolls = {}
    out = []
    for sched in list_active_schedules(vault):
        out.append({
            "id":                  sched.id,
            "shadow_id":           sched.shadow_id,
            "scroll_id":           sched.scroll_id,
            "shadow_name":         shadows.get(sched.shadow_id, sched.shadow_id),
            "scroll_name":         scrolls.get(sched.scroll_id, sched.scroll_id),
            "mode":                sched.mode.value,
            "interval_minutes":    sched.interval_minutes,
            "next_fire_at":        sched.next_fire_at.isoformat(),
            "next_fire_relative":  _format_relative_time(sched.next_fire_at),
            "dry_run":             sched.dry_run,
        })
    return out


@ui.refreshable
def _render_schedules_list_panel() -> None:
    """v0.8.6: Active Schedules panel on /army."""
    state = AppState.get()
    vm = _build_schedules_list_view_model(state.vault)

    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 16px; margin-bottom: 16px;"
    ):
        with ui.row().style("align-items: center; gap: 8px; margin-bottom: 8px;"):
            ui.label("📅 Active Schedules").style(
                f"font-size: 14px; font-weight: 700; color: {THEME['text']};"
            )
            ui.label(f"({len(vm)})").style(f"color: {THEME['text_muted']};")

        if not vm:
            ui.label("No active schedules.").style(
                f"color: {THEME['text_muted']}; font-size: 12px;"
            )
            return

        for entry in vm:
            with ui.row().style(
                f"gap: 12px; align-items: center; padding: 6px 0; "
                f"border-bottom: 1px solid {THEME['border']};"
            ):
                ui.label(entry["shadow_name"]).style(
                    f"color: {THEME['text']}; font-size: 12px; font-weight: 600; min-width: 100px;"
                )
                ui.label("→").style(f"color: {THEME['text_muted']};")
                ui.label(entry["scroll_name"]).style(
                    f"color: {THEME['text']}; font-size: 12px; flex: 1;"
                )
                mode_label = "Once" if entry["mode"] == "once" else f"Every {entry['interval_minutes']}m"
                ui.label(mode_label).style(
                    f"color: {THEME['text_muted']}; font-size: 11px;"
                )
                ui.label(entry["next_fire_relative"]).style(
                    f"color: {THEME['primary']}; font-size: 11px; min-width: 80px;"
                )
                def _make_cancel(sid=entry["id"]):
                    def _click(_e=None):
                        from systemu.scheduler.schedule_registry import cancel_schedule
                        if cancel_schedule(sid, state.vault):
                            ui.notify("Schedule cancelled", type="positive")
                            _render_schedules_list_panel.refresh()
                    return _click
                ui.button("Cancel", on_click=_make_cancel()).style(
                    f"background: {THEME['surface2']}; color: {THEME['text']}; "
                    f"font-size: 10px; padding: 2px 8px; border-radius: 4px;"
                )


def build_army_page() -> None:
    state = AppState.get()
    vault = state.vault

    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 20px;"):
        ui.label("👥 Shadows").style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )
        with ui.row().style("gap: 8px; align-items: center;"):
            # Phase 5 Slice 4d-2: consolidation belongs to Shadows (IA §5).
            # The surface still lives at _memory_consolidation_route(); this
            # button just makes it discoverable from here.  Uses the token-class
            # button primitive (no inline f-string style — lint 0 new).
            from systemu.interface.design.primitives import button as _token_button
            _token_button(
                "🧠 Memory consolidation",
                variant="ghost",
                on_click=lambda: ui.navigate.to(_memory_consolidation_route()),
            )
            ui.button("+ Awaken New Shadow", on_click=_show_awaken_dialog).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px;"
            )

    # v0.8.6: recent execute jobs panel
    _render_execute_jobs_panel()
    ui.timer(2.0, _render_execute_jobs_panel.refresh)

    # v0.8.6: active schedules panel
    _render_schedules_list_panel()
    ui.timer(30.0, _render_schedules_list_panel.refresh)

    shadows = vault.load_index("shadow_army")

    if not shadows:
        ui.label("No shadows yet — process a scroll to create your first shadow.").style(
            f"color: {THEME['text_muted']}; font-style: italic; padding: 20px;"
        )
        return

    # Card grid
    with ui.row().classes("w-full flex-wrap gap-5"):
        for sh in shadows:
            _shadow_card(sh)


def _shadow_card(sh: dict) -> None:
    status  = sh.get("status", "dormant")
    icon    = _STATUS_ICON.get(status, "🤖")
    color   = _status_color(status)
    skills  = len(sh.get("skill_ids", []))
    tools   = len(sh.get("tool_ids", []))
    acts    = sh.get("activity_count", 0)

    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 14px; padding: 22px; min-width: 260px; max-width: 300px; "
        f"flex: 1; transition: border-color 0.2s, box-shadow 0.2s; cursor: default;"
    ).on("mouseover", None).on("mouseout", None):

        # Avatar + name row
        with ui.row().style("align-items: center; gap: 12px; margin-bottom: 10px;"):
            ui.label(icon).style(
                f"font-size: 32px; width: 48px; height: 48px; display: flex; "
                f"align-items: center; justify-content: center; background: "
                f"color-mix(in srgb, {color} 15%, transparent); border-radius: 12px;"
            )
            with ui.column().style("gap: 2px;"):
                ui.label(sh.get("name", sh["id"])).style(
                    f"font-size: 16px; font-weight: 700; color: {THEME['text']};"
                )
                ui.html(status_badge_html(status))

        # Description
        desc = sh.get("description", "")
        ui.label(desc[:100] + "…" if len(desc) > 100 else desc or "No description.").style(
            f"font-size: 13px; color: {THEME['text_muted']}; line-height: 1.5; "
            f"margin-bottom: 12px; min-height: 40px;"
        )

        # Stats row
        with ui.row().style(
            f"gap: 16px; padding: 10px 0; border-top: 1px solid {THEME['border']}; "
            f"border-bottom: 1px solid {THEME['border']}; margin-bottom: 12px;"
        ):
            _mini_stat("🧠", str(skills), "Skills")
            _mini_stat("🔧", str(tools),  "Tools")
            _mini_stat("📋", str(acts),   "Activities")

        # Action buttons
        with ui.row().style("gap: 8px;"):
            uid = sh["id"]
            ui.button(
                "👁 Details",
                on_click=lambda _, i=uid: _show_shadow_detail(i),
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; "
                f"font-size: 12px; flex: 1;"
            )
            ui.button(
                "🧠 Memory",
                on_click=lambda _, i=uid: ui.navigate.to(f"/memory/{i}"),
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; "
                f"font-size: 12px; flex: 1;"
            )
            ui.button(
                "⚡ Execute",
                on_click=lambda _, i=uid: _show_execute_dialog(i),
            ).style(
                f"background: {color}; color: white; border-radius: 8px; "
                f"font-size: 12px; flex: 1;"
            )
            ui.button(
                "📅 Schedule",
                on_click=lambda _, sid=uid: _show_schedule_dialog(sid),
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 6px; "
                f"padding: 4px 10px; font-size: 12px; margin-left: 6px;"
            )
            # Phase 5 Slice 4c: edit-in-place — the Workshop Shadows tab is gone;
            # open the shared shadow editor (active-lock enforced) right here.
            ui.button(
                "✏️ Edit",
                on_click=lambda _, sid=uid: open_shadow_edit_dialog(
                    AppState.get().vault.get_shadow(sid), AppState.get().vault
                ),
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 6px; "
                f"padding: 4px 10px; font-size: 12px; margin-left: 6px;"
            )


def _mini_stat(icon: str, value: str, label: str) -> None:
    with ui.column().style("align-items: center; gap: 1px;"):
        ui.label(f"{icon} {value}").style(
            f"font-size: 14px; font-weight: 700; color: {THEME['text']};"
        )
        ui.label(label).style(f"font-size: 10px; color: {THEME['text_muted']};")


def _status_color(status: str) -> str:
    return {
        "awakened": THEME["primary"],
        "dormant":  THEME["text_muted"],
        "retired":  THEME["danger"],
    }.get(status, THEME["info"])


def _show_shadow_detail(shadow_id: str) -> None:
    state = AppState.get()
    try:
        shadow = state.vault.get_shadow(shadow_id)
    except KeyError:
        ui.notify("Shadow not found.", type="negative")
        return

    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 16px; min-width: 680px; max-width: 800px; max-height: 80vh; "
        f"overflow-y: auto; padding: 28px;"
    ):
        ui.label(shadow.name).style(f"font-size: 20px; font-weight: 800; color: {THEME['text']};")
        ui.html(status_badge_html(shadow.status.value if hasattr(shadow.status, "value") else str(shadow.status)))
        ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")

        ui.label(shadow.description).style(
            f"font-size: 14px; color: {THEME['text']}; line-height: 1.6; margin-bottom: 12px;"
        )

        _detail_section("System Prompt (preview)", shadow.system_prompt[:600] + "…" if len(shadow.system_prompt) > 600 else shadow.system_prompt)

        if shadow.skill_ids:
            _detail_section("Skills", " · ".join(resolve_names(shadow.skill_ids, state.vault)) or "—")
        if shadow.available_tool_ids:
            _detail_section("Tools", " · ".join(resolve_names(shadow.available_tool_ids, state.vault)) or "—")
        if shadow.assigned_activity_ids:
            _detail_section("Activities", " · ".join(resolve_names(shadow.assigned_activity_ids, state.vault)) or "—")

        # v0.4.3-b: operator-labelled specialty tag (routing preference).
        specialty = str(getattr(shadow, "specialty", "") or "")
        if specialty:
            _detail_section("Specialty", specialty)

        # v0.4.2-b: surface the per-shadow Intelligent Supervisor opt-in.
        # Editable in the Workshop (separate page); read-only here so the
        # operator can see at a glance which shadows have it enabled.
        supervisor_on = bool(getattr(shadow, "supervisor_enabled", False))
        with ui.row().style("gap: 8px; align-items: center; margin-bottom: 10px;"):
            ui.label("Intelligent Supervisor:").style(
                f"font-size: 11px; font-weight: 700; color: {THEME['text_muted']}; "
                f"text-transform: uppercase; letter-spacing: 0.08em;"
            )
            color = THEME["success"] if supervisor_on else THEME["text_muted"]
            ui.label("🧠 ENABLED" if supervisor_on else "⏸ disabled").style(
                f"font-size: 12px; font-weight: 700; color: {color}; "
                f"padding: 2px 8px; border-radius: 999px; "
                f"background: color-mix(in srgb, {color} 15%, transparent);"
            )
            ui.label("(edit via Workshop)").style(
                f"font-size: 11px; color: {THEME['text_muted']};"
            )

        # v0.4.4-b: Shadow success metrics by intent_hash.  Operator can see
        # what kinds of work this shadow has succeeded / failed on.
        try:
            from systemu.runtime.shadow_metrics import get_shadow_metrics
            ms = get_shadow_metrics()
            # Find all rows for this shadow across intent_hashes.
            data_path = ms.path
            rows_by_intent = []
            if data_path.exists():
                import json as _json
                raw_data = _json.loads(data_path.read_text(encoding="utf-8"))
                for row in raw_data.get("rows", {}).values():
                    if row.get("shadow_id") == shadow.id:
                        rows_by_intent.append(row)
            if rows_by_intent:
                ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")
                ui.label("Execution metrics by intent").style(
                    f"font-size: 13px; font-weight: 700; color: {THEME['text_muted']};"
                )
                for row in rows_by_intent[:6]:
                    succ = int(row.get("successes", 0))
                    total = int(row.get("executions", 0))
                    rate = (succ / total) if total > 0 else 0.0
                    rate_color = (THEME["success"] if rate >= 0.7
                                  else THEME["warning"] if rate >= 0.4
                                  else THEME["danger"])
                    with ui.row().style("gap: 10px; align-items: center; margin-top: 6px;"):
                        ui.label(f"intent {row.get('intent_hash', '?')}").style(
                            f"font-family: monospace; font-size: 11px; color: {THEME['text_muted']};"
                        )
                        ui.label(f"{succ}/{total}").style(
                            f"font-size: 12px; color: {THEME['text']};"
                        )
                        ui.label(f"({rate*100:.0f}%)").style(
                            f"font-size: 12px; color: {rate_color}; font-weight: 700;"
                        )
        except Exception:
            pass  # Metrics surfacing is best-effort

        if shadow.execution_log:
            ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")
            ui.label("Execution Log").style(f"font-size: 13px; font-weight: 700; color: {THEME['text_muted']};")
            for entry in shadow.execution_log[-5:]:
                color = {"success": THEME["success"], "failure": THEME["danger"], "partial": THEME["warning"]}.get(
                    entry.get("status", ""), THEME["text_muted"]
                )
                with ui.row().style(f"gap: 10px; align-items: center; margin-top: 6px;"):
                    ui.label(f"[{entry.get('status', '?').upper()}]").style(f"color: {color}; font-size: 12px; font-weight: 700;")
                    ui.label(entry.get("summary", "")[:80]).style(f"font-size: 12px; color: {THEME['text']};")

        # v0.6.8-b: embedded recovery panel for this shadow
        ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")
        try:
            from systemu.interface.pages.recover import render_recovery_panel
            render_recovery_panel("shadow", shadow.id)
        except Exception:
            pass

        ui.button("Close", on_click=dlg.close).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px; margin-top: 16px;"
        )
    dlg.open()


def _detail_section(title: str, content: str) -> None:
    with ui.column().style("margin-bottom: 10px;"):
        ui.label(title).style(f"font-size: 11px; font-weight: 700; color: {THEME['text_muted']}; text-transform: uppercase; letter-spacing: 0.08em;")
        ui.label(content).style(f"font-size: 13px; color: {THEME['text']}; margin-top: 4px;")


def _show_execute_dialog(shadow_id: str) -> None:
    state = AppState.get()
    scrolls = state.vault.load_index("scrolls")

    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 16px; padding: 28px; min-width: 420px;"
    ):
        ui.label("Execute Scroll via Shadow").style(
            f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 16px;"
        )

        scroll_select = ui.select(
            label="Select Scroll",
            options={s["id"]: s.get("name", s["id"]) for s in scrolls},
        ).style("width: 100%;")
        dry_run = ui.checkbox("Dry-run (no real tool execution)").style(f"color: {THEME['text']};")
        dry_run.value = True

        def _do_execute():
            if not scroll_select.value:
                ui.notify("Please select a scroll.", type="warning")
                return
            from systemu.interface.command.dispatch import dispatch
            cwd = state.project_root
            args = [shadow_id, scroll_select.value] + (["--dry-run"] if dry_run.value else [])
            dispatch("army execute", args, cwd=cwd, stream=True,
                     job_type="execute", dedup_key=f"army-execute:{shadow_id}")
            ui.notify("Shadow execution dispatched in background.", type="positive")
            dlg.close()

        with ui.row().style("gap: 10px; margin-top: 16px;"):
            ui.button("▶ Execute", on_click=_do_execute).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px;"
            )
            ui.button("Cancel", on_click=dlg.close).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px;"
            )
    dlg.open()


def _validate_schedule_payload(mode: str, scheduled_at: str, interval_minutes: int | None,
                                end_at: str | None) -> tuple[bool, str]:
    """Return (ok, error_message). Used by the Schedule dialog before persistence."""
    from datetime import datetime, timezone
    try:
        sched_dt = datetime.fromisoformat(scheduled_at)
    except Exception:
        return False, "scheduled_at must be a valid ISO datetime"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sched_dt_naive = sched_dt.replace(tzinfo=None) if sched_dt.tzinfo else sched_dt
    if sched_dt_naive <= now:
        return False, "scheduled_at is in the past — pick a future time"
    if mode == "recurring":
        if not interval_minutes:
            return False, "interval_minutes is required for recurring schedules"
        if interval_minutes < 5:
            return False, "interval_minutes must be at least 5 minutes"
    return True, ""


def _show_schedule_dialog(shadow_id: str) -> None:
    """v0.8.6: dialog for creating a one-time or recurring scheduled execute."""
    state = AppState.get()
    scrolls = state.vault.load_index("scrolls")

    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 16px; padding: 28px; min-width: 480px;"
    ):
        ui.label("📅 Schedule Execution").style(
            f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 16px;"
        )

        scroll_select = ui.select(
            label="Scroll",
            options={s["id"]: s.get("name", s["id"]) for s in scrolls},
        ).style("width: 100%;")
        dry_run = ui.checkbox("Dry-run (no real tool execution)").style(f"color: {THEME['text']};")
        dry_run.value = True

        mode_radio = ui.radio(
            options={"once": "One-time", "recurring": "Recurring"},
            value="once",
        ).style("margin-top: 12px;")

        from datetime import datetime, timedelta
        default_when = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        scheduled_at_input = ui.input(
            label="Run at (local time)",
            value=default_when,
        ).props("type=datetime-local").style("width: 100%; margin-top: 12px;")

        interval_input = ui.number(
            label="Every (minutes)",
            value=60, min=5,
        ).style("width: 100%; margin-top: 12px;").bind_visibility_from(mode_radio, "value",
                                                                       lambda v: v == "recurring")

        end_at_input = ui.input(
            label="End date (optional, recurring only)",
        ).props("type=date").style("width: 100%; margin-top: 12px;").bind_visibility_from(
            mode_radio, "value", lambda v: v == "recurring",
        )

        def _do_schedule():
            mode_val = mode_radio.value
            if not scroll_select.value:
                ui.notify("Please select a scroll.", type="warning")
                return
            sched_iso = scheduled_at_input.value
            interval = int(interval_input.value) if mode_val == "recurring" else None
            end_iso = end_at_input.value if (mode_val == "recurring" and end_at_input.value) else None

            ok, err = _validate_schedule_payload(mode_val, sched_iso, interval, end_iso)
            if not ok:
                ui.notify(err, type="negative")
                return

            from datetime import datetime
            from systemu.scheduler.schedule_registry import create_schedule
            from systemu.core.models import ScheduleMode

            sched_dt = datetime.fromisoformat(sched_iso)
            end_dt = datetime.fromisoformat(end_iso) if end_iso else None
            mode_enum = ScheduleMode.RECURRING if mode_val == "recurring" else ScheduleMode.ONCE

            try:
                sched = create_schedule(
                    shadow_id=shadow_id,
                    scroll_id=scroll_select.value,
                    mode=mode_enum,
                    interval_minutes=interval,
                    scheduled_at=sched_dt,
                    end_at=end_dt,
                    dry_run=dry_run.value,
                    vault=state.vault,
                )
                ui.notify(
                    f"Scheduled — next fire: {sched.next_fire_at.isoformat(timespec='minutes')}",
                    type="positive",
                )
                dlg.close()
            except Exception as exc:
                ui.notify(f"Error: {exc}", type="negative")

        with ui.row().style("gap: 10px; margin-top: 20px;"):
            ui.button("📅 Schedule", on_click=_do_schedule).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px;"
            )
            ui.button("Cancel", on_click=dlg.close).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px;"
            )
    dlg.open()


def _show_awaken_dialog() -> None:
    """Awaken dialog with 4 independent persona dimension sliders.

    Creativity, Professionalism, Techie, Thinking — each 0-100 independently.
    Values are passed as CLI args and affect only this shadow's system_prompt.
    """
    state = AppState.get()

    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 16px; padding: 28px; min-width: 560px; max-width: 640px;"
    ):
        ui.label("⚡ Awaken New Shadow").style(
            f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 6px;"
        )
        ui.label(
            "Shape your shadow's personality using the dimension sliders below. "
            "These affect only this shadow's system prompt."
        ).style(
            f"font-size: 13px; color: {THEME['text_muted']}; margin-bottom: 20px; line-height: 1.5;"
        )

        name_input = ui.input(label="Shadow Name", placeholder="e.g. FinanceTracker").style(
            "width: 100%; margin-bottom: 16px;"
        )

        activities = state.vault.load_index("activities")
        act_select = ui.select(
            label="Assign to Activity (optional)",
            options={"": "— None —", **{a["id"]: a.get("name", a["id"]) for a in activities}},
        ).style("width: 100%; margin-bottom: 24px;")
        act_select.value = ""

        # ── Persona Dimension Sliders ────────────────────────────────────────
        ui.separator().style(f"background: {THEME['border']}; margin-bottom: 20px;")
        ui.label("🎭 Persona Dimensions").style(
            f"font-size: 14px; font-weight: 700; color: {THEME['text']}; margin-bottom: 4px;"
        )
        ui.label("Each axis is independent (0 = minimum, 100 = maximum). Default 50 = balanced.").style(
            f"font-size: 12px; color: {THEME['text_muted']}; margin-bottom: 16px;"
        )

        def _dim_slider(icon: str, label: str, low_label: str, high_label: str, default: int = 50):
            """Build a labelled slider returning the slider element."""
            with ui.column().style("width: 100%; margin-bottom: 14px; gap: 4px;"):
                with ui.row().style("align-items: center; justify-content: space-between;"):
                    ui.label(f"{icon} {label}").style(
                        f"font-size: 13px; font-weight: 600; color: {THEME['text']};"
                    )
                    val_lbl = ui.label(str(default)).style(
                        f"font-size: 13px; font-weight: 700; color: {THEME['primary']}; "
                        f"min-width: 30px; text-align: right;"
                    )
                sl = ui.slider(min=0, max=100, value=default).style("width: 100%;")
                sl.on("update:model-value", lambda e, v=val_lbl: v.set_text(str(int(e.args))))
                with ui.row().style("justify-content: space-between; margin-top: 2px;"):
                    ui.label(low_label).style(f"font-size: 10px; color: {THEME['text_muted']};")
                    ui.label(high_label).style(f"font-size: 10px; color: {THEME['text_muted']};")
            return sl

        sl_creativity      = _dim_slider("🎨", "Creativity",      "Methodical",    "Highly Creative",    50)
        sl_professionalism = _dim_slider("🎯", "Professionalism", "Casual",        "Highly Formal",      50)
        sl_techie          = _dim_slider("💻", "Techie",          "Non-Technical", "Deep Technical",     50)
        sl_thinking        = _dim_slider("🧠", "Thinking",        "Action-first",  "Deep Deliberative",  50)

        ui.separator().style(f"background: {THEME['border']}; margin: 4px 0 20px 0;")

        def _do_awaken():
            if not name_input.value.strip():
                ui.notify("Please enter a shadow name.", type="warning")
                return
            from systemu.interface.command.dispatch import dispatch
            cwd = state.project_root
            args = [
                "--name",            name_input.value.strip(),
                "--creativity",      str(int(sl_creativity.value)),
                "--professionalism", str(int(sl_professionalism.value)),
                "--techie",          str(int(sl_techie.value)),
                "--thinking",        str(int(sl_thinking.value)),
            ]
            if act_select.value:
                args.extend(["--activity", act_select.value])
            dispatch("army awaken", args, cwd=cwd, stream=True,
                     job_type="awaken", dedup_key=f"army-awaken:{name_input.value.strip()}")
            ui.notify(f"Shadow '{name_input.value.strip()}' awakening dispatched.", type="positive")
            dlg.close()

        with ui.row().style("gap: 10px;"):
            ui.button("⚡ Awaken Shadow", on_click=_do_awaken).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px; font-weight: 600;"
            )
            ui.button("Cancel", on_click=dlg.close).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px;"
            )
    dlg.open()
