"""NiceGUI Dashboard — Activities page.

Dedicated page for all Activities:
  - Searchable table of activities with status, scroll, skill count, tool count
  - Click row → detail panel:
      * Derived scroll (clickable)
      * Skills list with instructions_md snippet + required_tools badges
      * Tools list with status badges
      * Missing tools warning
      * Edit dialog (rename, notes)
  - Status badges match the ActivityStatus enum
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME, status_badge_html
from systemu.interface.name_resolver import resolve_name, short_id

logger = logging.getLogger(__name__)

_ACT_STATUS_COLOR = {
    "unassigned": "#6b7280",
    "partial":    "#f59e0b",
    "assigned":   "#3b82f6",
    "executable": "#22c55e",
}


def build_activities_page() -> None:
    state = AppState.get()
    vault = state.vault

    # ── Header ────────────────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 20px;"):
        ui.label("📋 Activities").style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )

    # ── Search bar ────────────────────────────────────────────────────────────
    search_input = ui.input(placeholder="Search activities...").style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 8px; padding: 8px 12px; color: {THEME['text']}; width: 360px; "
        f"margin-bottom: 16px;"
    )

    # ── Refreshable table ─────────────────────────────────────────────────────
    @ui.refreshable
    def _activity_table(query: str = ""):
        activities = vault.load_index("activities")
        filtered = [
            a for a in activities
            if not query or query.lower() in a.get("name", "").lower()
        ]

        if not filtered:
            ui.label("No activities found.").style(
                f"color: {THEME['text_muted']}; font-style: italic; padding: 20px;"
            )
            return

        # Count cards
        with ui.row().style("gap: 16px; margin-bottom: 20px;"):
            _stat_pill(str(len(activities)), "Total", THEME["primary"])
            _stat_pill(
                str(sum(1 for a in activities if a.get("status") == "assigned")),
                "Assigned", THEME["success"]
            )
            _stat_pill(
                str(sum(1 for a in activities if a.get("status") == "partial")),
                "Partial (missing tools)", "#f59e0b"
            )
            _stat_pill(
                str(sum(1 for a in activities if a.get("status") == "unassigned")),
                "Unassigned", THEME["text_muted"]
            )

        # Table
        with ui.element("table").style(
            f"width: 100%; border-collapse: collapse; background: {THEME['surface']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 12px; overflow: hidden;"
        ):
            with ui.element("thead"):
                with ui.element("tr"):
                    for col in ["Activity", "Status", "Scroll", "Skills", "Tools", "Shadow", "Actions"]:
                        with ui.element("th").style(
                            f"background: {THEME['surface2']}; color: {THEME['text_muted']}; "
                            f"font-size: 11px; font-weight: 600; text-transform: uppercase; "
                            f"letter-spacing: 0.08em; padding: 10px 16px; text-align: left; "
                            f"border-bottom: 1px solid {THEME['border']};"
                        ):
                            ui.label(col)

            with ui.element("tbody"):
                for a in filtered:
                    status     = a.get("status", "unassigned")
                    color      = _ACT_STATUS_COLOR.get(status, THEME["text_muted"])
                    n_skills   = len(a.get("required_skill_ids", []))
                    n_tools    = len(a.get("required_tool_ids", []))
                    n_missing  = len(a.get("missing_tools", []))
                    scroll_id  = a.get("scroll_id", "—")
                    shadow_id  = a.get("assigned_shadow_id") or "—"
                    aid        = a["id"]

                    with ui.element("tr").style(
                        f"border-bottom: 1px solid {THEME['border']}; cursor: pointer; "
                        f"transition: background 0.15s;"
                    ):
                        # Name
                        with ui.element("td").style("padding: 12px 16px;"):
                            ui.label(a.get("name", aid)).style(
                                f"font-size: 14px; font-weight: 600; color: {THEME['text']};"
                            )

                        # Status badge
                        with ui.element("td").style("padding: 12px 16px;"):
                            ui.html(
                                f'<span style="background: color-mix(in srgb, {color} 20%, transparent); '
                                f'color: {color}; font-size: 10px; font-weight: 700; '
                                f'padding: 3px 10px; border-radius: 6px; letter-spacing: 0.05em; '
                                f'text-transform: uppercase;">{status}</span>'
                            )

                        # Scroll
                        with ui.element("td").style(f"padding: 12px 16px; font-size: 13px; color: {THEME['text_muted']};"):
                            if scroll_id and scroll_id != "—":
                                with ui.column().style("gap: 0;"):
                                    ui.label(resolve_name(scroll_id, vault)).style(
                                        f"color: {THEME['text']}; font-size: 13px;")
                                    ui.label(short_id(scroll_id)).style(
                                        f"color: {THEME['text_muted']}; font-size: 11px; font-family: monospace;")
                            else:
                                ui.label("—")

                        # Skills count with warning if missing tools
                        with ui.element("td").style("padding: 12px 16px;"):
                            with ui.row().style("align-items: center; gap: 6px;"):
                                ui.label(f"🧠 {n_skills}").style(f"font-size: 13px; color: {THEME['text']};")

                        # Tools count
                        with ui.element("td").style("padding: 12px 16px;"):
                            with ui.row().style("align-items: center; gap: 6px;"):
                                ui.label(f"🔧 {n_tools}").style(f"font-size: 13px; color: {THEME['text']};")
                                if n_missing > 0:
                                    ui.html(
                                        f'<span style="background: color-mix(in srgb, #f59e0b 20%, transparent); '
                                        f'color: #f59e0b; font-size: 10px; font-weight: 700; '
                                        f'padding: 2px 6px; border-radius: 4px;">{n_missing} missing</span>'
                                    )

                        # Shadow
                        with ui.element("td").style(f"padding: 12px 16px; font-size: 12px; color: {THEME['text_muted']};"):
                            if shadow_id and shadow_id != "—":
                                with ui.column().style("gap: 0;"):
                                    ui.label(resolve_name(shadow_id, vault)).style(
                                        f"color: {THEME['text']}; font-size: 13px;")
                                    ui.label(short_id(shadow_id)).style(
                                        f"color: {THEME['text_muted']}; font-size: 11px; font-family: monospace;")
                            else:
                                ui.label("—")

                        # Actions
                        with ui.element("td").style("padding: 12px 16px;"):
                            with ui.row().style("gap: 6px;"):
                                ui.button(
                                    "🔍 Detail",
                                    on_click=lambda _, i=aid: _show_activity_detail(i, vault),
                                ).style(
                                    f"background: {THEME['surface2']}; color: {THEME['text']}; "
                                    f"border: 1px solid {THEME['border']}; border-radius: 6px; "
                                    f"font-size: 12px; padding: 4px 10px;"
                                )

    search_input.on("input", lambda e: _activity_table.refresh(
        e.value if hasattr(e, "value") and isinstance(e.value, str) else ""
    ))
    _activity_table()


# ─── Activity Detail Dialog ───────────────────────────────────────────────────

def _show_activity_detail(activity_id: str, vault) -> None:
    try:
        activity = vault.get_activity(activity_id)
    except (KeyError, AttributeError):
        ui.notify("Activity not found.", type="negative")
        return

    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 16px; min-width: 720px; max-width: 880px; max-height: 85vh; "
        f"overflow-y: auto; padding: 28px;"
    ):
        # Header
        with ui.row().style("align-items: center; gap: 12px; margin-bottom: 6px;"):
            ui.label(activity.name).style(
                f"font-size: 20px; font-weight: 800; color: {THEME['text']}; flex: 1;"
            )
            status = activity.status.value if hasattr(activity.status, "value") else str(activity.status)
            color = _ACT_STATUS_COLOR.get(status, THEME["text_muted"])
            ui.html(
                f'<span style="background: color-mix(in srgb, {color} 20%, transparent); '
                f'color: {color}; font-size: 10px; font-weight: 700; '
                f'padding: 4px 12px; border-radius: 8px; letter-spacing: 0.05em; '
                f'text-transform: uppercase;">{status}</span>'
            )

        ui.label(f"ID: {activity.id}").style(
            f"font-size: 11px; font-family: monospace; color: {THEME['text_muted']}; margin-bottom: 16px;"
        )

        ui.separator().style(f"background: {THEME['border']}; margin: 0 0 16px 0;")

        # Scroll link
        _section_header("📜 Source Scroll")
        with ui.row().style("align-items: center; gap: 10px; margin-bottom: 16px;"):
            with ui.column().style("gap: 0;"):
                ui.label(resolve_name(activity.scroll_id, vault)).style(
                    f"font-size: 13px; color: {THEME['text']};"
                )
                ui.label(short_id(activity.scroll_id)).style(
                    f"font-size: 11px; color: {THEME['text_muted']}; font-family: monospace;"
                )
            ui.button("View Scroll", on_click=lambda: _view_scroll(activity.scroll_id, vault, dlg)).style(
                f"background: {THEME['primary']}; color: white; border-radius: 6px; "
                f"font-size: 12px; padding: 4px 10px;"
            )

        # Skills section
        _section_header(f"🧠 Skills ({len(activity.required_skill_ids)})")
        if activity.required_skill_ids:
            for sid in activity.required_skill_ids:
                _skill_card(sid, vault)
        else:
            ui.label("No skills linked.").style(f"color: {THEME['text_muted']}; font-size: 13px; margin-bottom: 12px;")

        ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")

        # Tools section
        _section_header(f"🔧 Tools ({len(activity.required_tool_ids)})")
        if activity.missing_tools:
            ui.html(
                f'<div style="background: color-mix(in srgb, #f59e0b 15%, transparent); '
                f'border: 1px solid #f59e0b; border-radius: 8px; padding: 10px 14px; '
                f'color: #f59e0b; font-size: 13px; margin-bottom: 12px;">'
                f'⚠️ {len(activity.missing_tools)} tool(s) not yet forged: '
                f'{", ".join(activity.missing_tools)}</div>'
            )
        if activity.required_tool_ids:
            for tid in activity.required_tool_ids:
                _tool_card(tid, vault)
        else:
            ui.label("No tools linked.").style(f"color: {THEME['text_muted']}; font-size: 13px; margin-bottom: 12px;")

        ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")

        # Shadow assignment
        _section_header("👤 Assigned Shadow")
        if activity.assigned_shadow_id:
            with ui.column().style("gap: 0; margin-bottom: 16px;"):
                ui.label(resolve_name(activity.assigned_shadow_id, vault)).style(
                    f"font-size: 13px; color: {THEME['text']};"
                )
                ui.label(short_id(activity.assigned_shadow_id)).style(
                    f"font-size: 11px; color: {THEME['text_muted']}; font-family: monospace;"
                )
        else:
            ui.label("No shadow assigned yet.").style(
                f"font-size: 13px; color: {THEME['text_muted']}; margin-bottom: 16px;"
            )

        ui.button("Close", on_click=dlg.close).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px; margin-top: 8px;"
        )

    dlg.open()


def _skill_card(skill_id: str, vault) -> None:
    """Render a skill card showing name, description snippet, and required tools."""
    try:
        skill = vault.get_skill(skill_id)
    except (KeyError, AttributeError):
        ui.label(f"Skill {skill_id} not found.").style(f"color: {THEME['danger']}; font-size: 12px;")
        return

    with ui.card().style(
        f"background: {THEME['surface2']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 10px; padding: 14px; margin-bottom: 10px; width: 100%;"
    ):
        with ui.row().style("align-items: center; gap: 8px; margin-bottom: 6px;"):
            ui.label(skill.name).style(
                f"font-size: 14px; font-weight: 700; color: {THEME['text']}; flex: 1;"
            )
        p_color = THEME["primary"]
        ui.html(
            f'<span style="background: color-mix(in srgb, {p_color} 15%, transparent); '
            f'color: {p_color}; font-size: 10px; font-weight: 600; '
            f'padding: 2px 8px; border-radius: 4px;">{skill.category}</span>'
        )
        ui.html(
            f'<span style="background: color-mix(in srgb, #6b7280 20%, transparent); '
            f'color: #9ca3af; font-size: 10px; padding: 2px 8px; border-radius: 4px;">'
            f'{skill.proficiency_level}</span>'
        )

        ui.label(skill.description[:120] + "…" if len(skill.description) > 120 else skill.description).style(
            f"font-size: 12px; color: {THEME['text_muted']}; line-height: 1.5; margin-bottom: 8px;"
        )

        # Instructions snippet
        if skill.instructions_md:
            preview = skill.instructions_md[:160] + "…" if len(skill.instructions_md) > 160 else skill.instructions_md
            with ui.row().style(
                f"background: color-mix(in srgb, {THEME['primary']} 8%, transparent); "
                f"border-radius: 6px; padding: 8px 12px; margin-bottom: 8px;"
            ):
                ui.label(f"📖 {preview}").style(
                    f"font-size: 12px; color: {THEME['text']}; font-style: italic; line-height: 1.5;"
                )

        # Required tools badges
        if skill.required_tool_ids:
            with ui.row().style("flex-wrap: wrap; gap: 6px;"):
                ui.label("Requires:").style(f"font-size: 11px; color: {THEME['text_muted']}; align-self: center;")
                for tid in skill.required_tool_ids:
                    try:
                        t = vault.get_tool(tid)
                        t_status = t.status.value if hasattr(t.status, "value") else str(t.status)
                        t_color = {"deployed": "#22c55e", "forged": "#3b82f6", "proposed": "#f59e0b"}.get(t_status, "#6b7280")
                        ui.html(
                            f'<span style="background: color-mix(in srgb, {t_color} 15%, transparent); '
                            f'color: {t_color}; font-size: 10px; font-weight: 600; '
                            f'padding: 2px 8px; border-radius: 4px; border: 1px solid color-mix(in srgb, {t_color} 40%, transparent);">'
                            f'🔧 {t.name}</span>'
                        )
                    except (KeyError, AttributeError):
                        t_muted = THEME["text_muted"]
                        t_surf = THEME["surface"]
                        ui.html(
                            f'<span style="color: {t_muted}; font-size: 10px; '
                            f'padding: 2px 8px; border-radius: 4px; background: {t_surf};">'
                            f'{tid}</span>'
                        )


def _tool_card(tool_id: str, vault) -> None:
    """Render a tool card showing name, type, status badge."""
    try:
        tool = vault.get_tool(tool_id)
    except (KeyError, AttributeError):
        ui.label(f"Tool {tool_id} not found.").style(f"color: {THEME['danger']}; font-size: 12px;")
        return

    t_status = tool.status.value if hasattr(tool.status, "value") else str(tool.status)
    t_color = {"deployed": "#22c55e", "forged": "#3b82f6", "proposed": "#f59e0b", "tested": "#8b5cf6"}.get(t_status, "#6b7280")
    t_type = tool.tool_type.value if hasattr(tool.tool_type, "value") else str(tool.tool_type)

    with ui.card().style(
        f"background: {THEME['surface2']}; border: 1px solid {THEME['border']}; "
        f"border-left: 3px solid {t_color}; "
        f"border-radius: 10px; padding: 12px 14px; margin-bottom: 8px; width: 100%;"
    ):
        with ui.row().style("align-items: center; gap: 8px;"):
            ui.label(f"🔧 {tool.name}").style(
                f"font-size: 14px; font-weight: 700; color: {THEME['text']}; flex: 1;"
            )
            ui.html(
                f'<span style="background: color-mix(in srgb, {t_color} 20%, transparent); '
                f'color: {t_color}; font-size: 10px; font-weight: 700; '
                f'padding: 2px 8px; border-radius: 4px; text-transform: uppercase;">{t_status}</span>'
            )
            t_muted = THEME["text_muted"]
            t_surf  = THEME["surface"]
            ui.html(
                f'<span style="color: {t_muted}; font-size: 10px; '
                f'padding: 2px 8px; border-radius: 4px; background: {t_surf};">{t_type}</span>'
            )
        ui.label(tool.description[:100] + "…" if len(tool.description) > 100 else tool.description).style(
            f"font-size: 12px; color: {THEME['text_muted']}; margin-top: 4px; line-height: 1.5;"
        )


def _view_scroll(scroll_id: str, vault, parent_dlg) -> None:
    """Open a nested dialog showing the scroll details."""
    try:
        scroll = vault.get_scroll(scroll_id)
    except (KeyError, AttributeError):
        ui.notify("Scroll not found.", type="negative")
        return

    with ui.dialog() as sdlg, ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 14px; min-width: 640px; max-width: 800px; max-height: 80vh; "
        f"overflow-y: auto; padding: 24px;"
    ):
        ui.label(scroll.name).style(f"font-size: 18px; font-weight: 800; color: {THEME['text']}; margin-bottom: 8px;")
        ui.html(status_badge_html(scroll.status.value if hasattr(scroll.status, "value") else str(scroll.status)))
        ui.separator().style(f"background: {THEME['border']}; margin: 12px 0;")
        ui.markdown(scroll.narrative_md or "*No narrative.*").style(
            f"color: {THEME['text']}; font-size: 13px; line-height: 1.6;"
        )
        ui.button("Close", on_click=sdlg.close).style(
            f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px; margin-top: 12px;"
        )
    sdlg.open()


# ─── UI Helpers ───────────────────────────────────────────────────────────────

def _section_header(title: str) -> None:
    ui.label(title).style(
        f"font-size: 13px; font-weight: 700; color: {THEME['text']}; "
        f"margin-bottom: 10px; margin-top: 4px;"
    )


def _stat_pill(value: str, label: str, color: str) -> None:
    with ui.card().style(
        f"background: color-mix(in srgb, {color} 12%, {THEME['surface']}); "
        f"border: 1px solid color-mix(in srgb, {color} 30%, transparent); "
        f"border-radius: 10px; padding: 12px 20px; text-align: center;"
    ):
        ui.label(value).style(f"font-size: 24px; font-weight: 800; color: {color}; line-height: 1;")
        ui.label(label).style(f"font-size: 11px; color: {THEME['text_muted']}; margin-top: 4px;")
