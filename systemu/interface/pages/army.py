"""NiceGUI Dashboard — Shadow Army page.

Card grid of all Shadows with:
  * Status badge, avatar emoji, description preview
  * Skill / Tool counts
  * Execute button (dry-run) + Show Details dialog
  * Awaken dialog with persona dimension sliders
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME, status_badge_html


_STATUS_ICON = {
    "awakened": "⚡",
    "dormant":  "💤",
    "retired":  "🪦",
}


def build_army_page() -> None:
    state = AppState.get()
    vault = state.vault

    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 20px;"):
        ui.label("👥 Shadow Army").style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )
        ui.button("+ Awaken New Shadow", on_click=_show_awaken_dialog).style(
            f"background: {THEME['primary']}; color: white; border-radius: 8px;"
        )

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
            _detail_section("Skills", " · ".join(shadow.skill_ids))
        if shadow.available_tool_ids:
            _detail_section("Tools", " · ".join(shadow.available_tool_ids))
        if shadow.assigned_activity_ids:
            _detail_section("Activities", " · ".join(shadow.assigned_activity_ids))

        # operator-labelled specialty tag (routing preference).
        specialty = str(getattr(shadow, "specialty", "") or "")
        if specialty:
            _detail_section("Specialty", specialty)

        # surface the per-shadow Intelligent Supervisor opt-in.
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

        # Shadow success metrics by intent_hash.  Operator can see
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

        # embedded recovery panel for this shadow
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
            from systemu.interface.jobs import JobManager
            import sys
            jm = JobManager.get()
            cwd = state.project_root
            cmd = [
                sys.executable, "-m", "sharing_on",
                "army", "execute", shadow_id, scroll_select.value,
            ]
            if dry_run.value:
                cmd.append("--dry-run")
            jm.start_job(f"Execute: {scroll_select.value[:12]}", "execute", cmd, cwd)
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
            from systemu.interface.jobs import JobManager
            import sys
            jm = JobManager.get()
            cwd = state.project_root
            cmd = [
                sys.executable, "-m", "sharing_on",
                "army", "awaken",
                "--name",            name_input.value.strip(),
                "--creativity",      str(int(sl_creativity.value)),
                "--professionalism", str(int(sl_professionalism.value)),
                "--techie",          str(int(sl_techie.value)),
                "--thinking",        str(int(sl_thinking.value)),
            ]
            if act_select.value:
                cmd.extend(["--activity", act_select.value])
            jm.start_job(f"Awaken: {name_input.value.strip()}", "awaken", cmd, cwd)
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
