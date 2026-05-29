"""NiceGUI Dashboard — Scrolls page.

Shows a searchable, filterable table of Scrolls.
Click any row to open a slide-out panel with full Scroll detail
(narrative, action blocks, linked activity, and an Approve button).
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME, status_badge_html
from systemu.interface.nav_helpers import workshop_deeplink


def build_scrolls_page() -> None:
    state = AppState.get()
    vault = state.vault

    # Header
    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 20px;"):
        ui.label("📜 Scrolls").style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )
        ui.button("+ Refine New Session", on_click=_show_refine_dialog).style(
            f"background: {THEME['primary']}; color: white; border-radius: 8px;"
        )

    # Search
    search_input = ui.input(
        placeholder="Search scrolls...",
    ).style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 8px; padding: 8px 12px; color: {THEME['text']}; width: 360px;"
    )

    # Refreshable table — reloads from vault on every render
    @ui.refreshable
    def _scroll_table(query: str = ""):
        scrolls = vault.load_index("scrolls")  # Fresh read each time
        filtered = [
            s for s in scrolls
            if query.lower() in s.get("name", "").lower()
            or query.lower() in " ".join(s.get("tags", [])).lower()
        ]
        if not filtered:
            ui.label("No scrolls found.").style(f"color: {THEME['text_muted']}; padding: 20px;")
            return

        with ui.element("table").style(
            f"width: 100%; border-collapse: collapse; background: {THEME['surface']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 12px; overflow: hidden;"
        ):
            with ui.element("thead"):
                with ui.element("tr"):
                    for col in ["Name", "Status", "Session", "Tags", "Actions"]:
                        with ui.element("th").style(
                            f"background: {THEME['surface2']}; color: {THEME['text_muted']}; "
                            f"font-size: 11px; font-weight: 600; text-transform: uppercase; "
                            f"letter-spacing: 0.08em; padding: 10px 16px; text-align: left; "
                            f"border-bottom: 1px solid {THEME['border']};"
                        ):
                            ui.label(col)

            with ui.element("tbody"):
                for s in filtered:
                    with ui.element("tr").style("cursor: pointer;"):
                        _td(s.get("name", s["id"]), bold=True)
                        with ui.element("td").style(f"padding: 12px 16px;"):
                            badge_html = status_badge_html(s.get("status", "?"))
                            # v0.6.5-g: warning badge for scrolls with trace warnings
                            if s.get("has_warnings"):
                                badge_html += (
                                    " <span title='Pipeline warnings — click View for detail'"
                                    " style='color:#fbbf24; font-size:1.1em; margin-left:6px;'>⚠</span>"
                                )
                            ui.html(badge_html)
                        _td(s.get("source_session_id", "—"))
                        _td(", ".join(s.get("tags", [])) or "—")
                        with ui.element("td").style("padding: 12px 16px;"):
                            sid = s["id"]
                            status = s.get("status", "")
                            with ui.row().style("gap: 6px;"):
                                if status == "pending_approval":
                                    def _on_approve(_, i=sid):
                                        _approve_scroll(i)
                                        _scroll_table.refresh(search_input.value)
                                    ui.button(
                                        "✓ Approve",
                                        on_click=_on_approve,
                                    ).style(
                                        f"background: {THEME['success']}; color: white; "
                                        f"border-radius: 6px; font-size: 12px; padding: 4px 10px;"
                                    )
                                else:
                                    ui.button(
                                        "View",
                                        on_click=lambda _, i=sid: _show_scroll_detail(i),
                                    ).style(
                                        f"background: {THEME['surface2']}; color: {THEME['text']}; "
                                        f"border: 1px solid {THEME['border']}; "
                                        f"border-radius: 6px; font-size: 12px; padding: 4px 10px;"
                                    )
                                # v0.8.8: deep-link into Workshop Scrolls tab (pre-selected)
                                ui.button(
                                    "✏️ Edit",
                                    on_click=lambda _, i=sid: ui.navigate.to(workshop_deeplink("scroll", i)),
                                ).style(
                                    f"background: {THEME['surface2']}; color: {THEME['text']}; "
                                    f"border: 1px solid {THEME['border']}; "
                                    f"border-radius: 6px; font-size: 12px; padding: 4px 10px;"
                                )

    search_input.on("input", lambda e: _scroll_table.refresh(
        e.value if hasattr(e, 'value') and isinstance(e.value, str) else ""
    ))
    _scroll_table()



def _td(text: str, bold: bool = False) -> None:
    style = (
        f"padding: 12px 16px; border-bottom: 1px solid {THEME['border']}; "
        f"color: {THEME['text']}; font-size: 14px;"
        + (" font-weight: 600;" if bold else "")
    )
    with ui.element("td").style(style):
        ui.label(text)
def _approve_scroll(scroll_id: str) -> None:
    from systemu.interface.jobs import JobManager
    import sys
    from pathlib import Path
    from systemu.interface.dashboard_state import AppState
    jm = JobManager.get()
    state = AppState.get()
    try:
        cwd = state.project_root
        cmd = [sys.executable, "-m", "sharing_on", "scrolls", "approve", scroll_id]
        
        jm.start_job(f"Approving Scroll: {scroll_id[:8]}", "approve", cmd, cwd)
        ui.notify(f"Dispatched background approval for Scroll {scroll_id[:8]}", type="positive")
    except Exception as exc:
        ui.notify(f"Error: {exc}", type="negative")


def _show_scroll_detail(scroll_id: str) -> None:
    state = AppState.get()
    try:
        scroll = state.vault.get_scroll(scroll_id)
    except KeyError:
        ui.notify("Scroll not found.", type="negative")
        return

    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 16px; min-width: 680px; max-width: 800px; max-height: 80vh; "
        f"overflow-y: auto; padding: 28px;"
    ):
        ui.label(scroll.name).style(
            f"font-size: 20px; font-weight: 800; color: {THEME['text']};"
        )
        ui.html(status_badge_html(scroll.status.value if hasattr(scroll.status, 'value') else str(scroll.status)))
        ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")

        ui.label("Narrative").style(f"font-size: 13px; font-weight: 700; color: {THEME['text_muted']}; margin-bottom: 6px;")
        ui.markdown(scroll.narrative_md or "*No narrative available.*").style(
            f"color: {THEME['text']}; font-size: 14px; line-height: 1.6;"
        )

        if scroll.action_blocks:
            ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")
            ui.label(f"Action Blocks ({len(scroll.action_blocks)})").style(
                f"font-size: 13px; font-weight: 700; color: {THEME['text_muted']}; margin-bottom: 6px;"
            )
            for ab in scroll.action_blocks:
                with ui.row().style(
                    f"background: {THEME['surface2']}; border-radius: 8px; padding: 10px 14px; "
                    f"margin-bottom: 6px; align-items: center; gap: 12px;"
                ):
                    ui.label(f"#{ab.step_number}").style(
                        f"font-size: 11px; font-weight: 700; color: {THEME['primary']}; min-width: 24px;"
                    )
                    with ui.column().style("gap: 2px;"):
                        ui.label(f"{ab.action} → {ab.target}").style(
                            f"font-size: 13px; font-weight: 600; color: {THEME['text']};"
                        )
                        ui.label(ab.expected_outcome[:80]).style(
                            f"font-size: 12px; color: {THEME['text_muted']};"
                        )

        # v0.6.5-g: Pipeline Trace panel — show per-stage decisions/warnings
        trace = list(getattr(scroll, "pipeline_trace", []) or [])
        ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")
        ui.label(f"📋 Pipeline Trace ({len(trace)})").style(
            f"font-size: 13px; font-weight: 700; color: {THEME['text_muted']}; margin-bottom: 6px;"
        )
        if not trace:
            ui.label("(no trace events recorded)").style(
                f"color: {THEME['text_muted']}; font-style: italic; font-size: 12px;"
            )
        else:
            for ev in trace:
                level_color = {
                    "info":  THEME["text_muted"],
                    "warn":  "#fbbf24",
                    "error": "#ef4444",
                }.get(ev.level, THEME["text"])
                level_icon = {"info": "•", "warn": "⚠", "error": "🚫"}.get(ev.level, "·")
                with ui.row().style(
                    f"background: {THEME['surface2']}; border-radius: 8px; padding: 8px 12px; "
                    f"margin-bottom: 4px; align-items: flex-start; gap: 10px;"
                ):
                    ui.label(level_icon).style(
                        f"color: {level_color}; font-size: 14px; min-width: 16px;"
                    )
                    with ui.column().style("gap: 2px; flex-grow: 1;"):
                        ui.label(f"[{ev.stage}] {ev.message}").style(
                            f"color: {level_color}; font-size: 12px; font-weight: 600;"
                        )
                        if ev.detail:
                            keys = ", ".join(
                                f"{k}={str(v)[:60]}" for k, v in ev.detail.items()
                                if k not in ("blockers", "proposed_revision")
                            )
                            if keys:
                                ui.label(keys).style(
                                    f"color: {THEME['text_muted']}; font-size: 11px;"
                                )
                        try:
                            ts_str = ev.ts.strftime("%H:%M:%S")
                        except Exception:
                            ts_str = str(getattr(ev, "ts", ""))[:8]
                        ui.label(ts_str).style(
                            f"color: {THEME['text_muted']}; font-size: 10px;"
                        )

        # v0.6.8-b: embedded recovery panel for this scroll
        ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")
        try:
            from systemu.interface.pages.recover import render_recovery_panel
            render_recovery_panel("scroll", scroll.id)
        except Exception:
            pass

        ui.button("Close", on_click=dlg.close).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; "
            f"border-radius: 8px; margin-top: 16px;"
        )
    dlg.open()


def _show_refine_dialog() -> None:
    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 16px; padding: 28px; min-width: 420px;"
    ):
        ui.label("Refine Capture Session").style(
            f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 16px;"
        )
        session_input = ui.input(
            label="Session directory path",
            placeholder="captures/my_task_abc123/",
        ).style(f"width: 100%;")
        auto_approve = ui.checkbox("Auto-approve (skip user prompt)").style(
            f"color: {THEME['text']};"
        )

        def _do_refine():
            from systemu.interface.jobs import JobManager
            import sys
            import os
            from pathlib import Path
            from systemu.interface.dashboard_state import AppState
            
            session_dir = session_input.value.strip()
            if not session_dir or not Path(session_dir).exists():
                ui.notify("Invalid session directory path.", type="warning")
                return
                
            jm = JobManager.get()
            state = AppState.get()
            cwd = state.project_root
            
            cmd = [sys.executable, "-m", "sharing_on", "scrolls", "refine", session_dir]
            if auto_approve.value:
                cmd.append("--auto")
                
            jm.start_job(f"Refining: {Path(session_dir).name}", "refine", cmd, cwd)
            ui.notify(f"Dispatched background scroll refinement.", type="positive")
            dlg.close()

        with ui.row().style("gap: 10px; margin-top: 16px;"):
            ui.button("Refine", on_click=_do_refine).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px;"
            )
            ui.button("Cancel", on_click=dlg.close).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px;"
            )
    dlg.open()
