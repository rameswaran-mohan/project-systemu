"""NiceGUI Dashboard — Scrolls page.

Shows a searchable, filterable table of Scrolls.
Click any row to open a slide-out panel with full Scroll detail
(narrative, action blocks, linked activity).  Phase 5 Slice 2b: the blind
✓ Approve is retired — pending scrolls get "Review & Approve", which opens
the unified Inbox gate card (inspect-before-approve) via
``scroll_gate.open_scroll_review_dialog``.
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import AppState
from systemu.interface.design import card
from systemu.interface.design.primitives import status_pill_html
from systemu.interface.scroll_gate import open_scroll_review_dialog
from systemu.interface.scroll_rebuild import open_scroll_rebuild_dialog


def build_scrolls_page() -> None:
    state = AppState.get()
    vault = state.vault

    # Header
    with ui.row().classes("w-full items-center justify-between q-mb-md"):
        ui.label("📜 Scrolls").classes("s-page-title")
        ui.button("+ Refine New Session", on_click=_show_refine_dialog).classes("s-btn s-btn--primary")

    # Search
    search_input = ui.input(
        placeholder="Search scrolls...",
    ).classes("s-input s-search")

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
            ui.label("No scrolls found.").classes("s-muted q-pa-md")
            return

        with ui.element("table").classes("s-table"):
            with ui.element("thead"):
                with ui.element("tr"):
                    for col in ["Name", "Status", "Session", "Tags", "Actions"]:
                        with ui.element("th"):
                            ui.label(col)

            with ui.element("tbody"):
                for s in filtered:
                    with ui.element("tr").classes("cursor-pointer"):
                        _td(s.get("name", s["id"]), bold=True)
                        with ui.element("td"):
                            badge_html = status_pill_html(s.get("status", "?"))
                            # v0.6.5-g: warning badge for scrolls with trace warnings
                            if s.get("has_warnings"):
                                badge_html += (
                                    " <span title='Pipeline warnings — click View for detail'"
                                    " class='s-warn-badge'>⚠</span>"
                                )
                            ui.html(badge_html)
                        _td(s.get("source_session_id", "—"))
                        _td(", ".join(s.get("tags", [])) or "—")
                        with ui.element("td"):
                            sid = s["id"]
                            status = s.get("status", "")
                            with ui.row().classes("q-gutter-xs"):
                                if status == "pending_approval":
                                    # Slice 2b: inspect-before-approve — the
                                    # unified gate card replaces blind approve.
                                    def _on_review(_, i=sid):
                                        open_scroll_review_dialog(
                                            i,
                                            on_resolved=lambda: _scroll_table
                                            .refresh(search_input.value),
                                        )
                                    ui.button(
                                        "Review & Approve",
                                        on_click=_on_review,
                                    ).classes("s-btn s-btn--primary")
                                else:
                                    ui.button(
                                        "View",
                                        on_click=lambda _, i=sid: _show_scroll_detail(i),
                                    ).classes("s-btn s-btn--ghost")
                                # Phase 6 Slice 6f: edit-in-place — the Workshop
                                # Scrolls rebuild now opens as a dialog right here
                                # (the /workshop route is gone).
                                def _on_edit(_, i=sid):
                                    open_scroll_rebuild_dialog(
                                        i,
                                        on_saved=lambda: _scroll_table
                                        .refresh(search_input.value),
                                    )
                                ui.button(
                                    "✏️ Edit",
                                    on_click=_on_edit,
                                ).classes("s-btn s-btn--ghost")

    search_input.on("input", lambda e: _scroll_table.refresh(
        e.value if hasattr(e, 'value') and isinstance(e.value, str) else ""
    ))
    _scroll_table()



def _td(text: str, bold: bool = False) -> None:
    classes = "s-cell" + (" s-cell--bold" if bold else "")
    with ui.element("td").classes(classes):
        ui.label(text)


def _show_scroll_detail(scroll_id: str) -> None:
    state = AppState.get()
    try:
        scroll = state.vault.get_scroll(scroll_id)
    except KeyError:
        ui.notify("Scroll not found.", type="negative")
        return

    with ui.dialog() as dlg, card(classes="s-dialog q-pa-lg"):
        ui.label(scroll.name).classes("s-dialog-title")
        ui.html(status_pill_html(scroll.status.value if hasattr(scroll.status, 'value') else str(scroll.status)))
        # Linked activity, resolved to its name (v0.8.12 names-not-ids).
        if scroll.activity_id:
            from systemu.interface.name_resolver import resolve_name
            ui.label(
                f"Linked activity: {resolve_name(scroll.activity_id, state.vault)}"
            ).classes("s-muted")
        ui.separator().classes("s-sep")

        ui.label("Narrative").classes("s-section-head")
        ui.markdown(scroll.narrative_md or "*No narrative available.*").classes("s-cell")

        if scroll.action_blocks:
            ui.separator().classes("s-sep")
            ui.label(f"Action Blocks ({len(scroll.action_blocks)})").classes("s-section-head")
            for ab in scroll.action_blocks:
                with ui.row().classes("s-row-box items-center q-mb-xs"):
                    ui.label(f"#{ab.step_number}").classes("s-section-head s-step-num")
                    with ui.column().classes("q-gutter-none"):
                        ui.label(f"{ab.action} → {ab.target}").classes("s-cell s-cell--bold")
                        ui.label(ab.expected_outcome[:80]).classes("s-muted")

        # v0.6.5-g: Pipeline Trace panel — show per-stage decisions/warnings
        trace = list(getattr(scroll, "pipeline_trace", []) or [])
        ui.separator().classes("s-sep")
        ui.label(f"📋 Pipeline Trace ({len(trace)})").classes("s-section-head")
        if not trace:
            ui.label("(no trace events recorded)").classes("s-muted s-italic")
        else:
            for ev in trace:
                level_cls = {
                    "info":  "s-muted",
                    "warn":  "s-text-warn",
                    "error": "s-text-danger",
                }.get(ev.level, "s-cell")
                level_icon = {"info": "•", "warn": "⚠", "error": "🚫"}.get(ev.level, "·")
                with ui.row().classes("s-row-box items-start q-mb-xs"):
                    ui.label(level_icon).classes(f"{level_cls} s-trace-icon")
                    with ui.column().classes("q-gutter-none col-grow"):
                        ui.label(f"[{ev.stage}] {ev.message}").classes(f"{level_cls} s-cell--bold")
                        if ev.detail:
                            keys = ", ".join(
                                f"{k}={str(v)[:60]}" for k, v in ev.detail.items()
                                if k not in ("blockers", "proposed_revision")
                            )
                            if keys:
                                ui.label(keys).classes("s-muted")
                        try:
                            ts_str = ev.ts.strftime("%H:%M:%S")
                        except Exception:
                            ts_str = str(getattr(ev, "ts", ""))[:8]
                        ui.label(ts_str).classes("s-muted")

        # v0.6.8-b: embedded recovery panel for this scroll
        ui.separator().classes("s-sep")
        try:
            from systemu.interface.pages.recover import render_recovery_panel
            render_recovery_panel("scroll", scroll.id)
        except Exception:
            pass

        ui.button("Close", on_click=dlg.close).classes("s-btn s-btn--ghost q-mt-md")
    dlg.open()


def _show_refine_dialog() -> None:
    with ui.dialog() as dlg, card(classes="s-dialog-sm q-pa-lg"):
        ui.label("Refine Capture Session").classes("s-dialog-title q-mb-md")
        session_input = ui.input(
            label="Session directory path",
            placeholder="captures/my_task_abc123/",
        ).classes("s-input s-input-full")
        auto_approve = ui.checkbox("Auto-approve (skip user prompt)").classes("s-cell")

        def _do_refine():
            from pathlib import Path
            from systemu.interface.command.dispatch import dispatch
            from systemu.interface.dashboard_state import AppState

            session_dir = session_input.value.strip()
            if not session_dir or not Path(session_dir).exists():
                ui.notify("Invalid session directory path.", type="warning")
                return

            state = AppState.get()
            cwd = state.project_root

            args = [session_dir] + (["--auto"] if auto_approve.value else [])
            dispatch("scrolls refine", args, cwd=cwd, stream=True,
                     job_type="refine", dedup_key=f"refine:{session_dir}")
            ui.notify(f"Dispatched background scroll refinement.", type="positive")
            dlg.close()

        with ui.row().classes("q-gutter-sm q-mt-md"):
            ui.button("Refine", on_click=_do_refine).classes("s-btn s-btn--primary")
            ui.button("Cancel", on_click=dlg.close).classes("s-btn s-btn--ghost")
    dlg.open()
