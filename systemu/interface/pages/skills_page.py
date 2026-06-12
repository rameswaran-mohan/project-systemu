"""NiceGUI Dashboard — Skills page.

Lists all skills in the vault with category, proficiency level, required tools,
and evidence scrolls. Clicking a row expands the full SKILL.md content.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME, status_badge_html
from systemu.interface.name_resolver import resolve_name, short_id
# v0.9 Phase-5 3a: the canonical skill-row renderer lives in entity_rows
# (ONE renderer per entity). The page-local _skill_row/_skill_detail below now
# delegate to it; they stay importable for back-compat.
from systemu.interface.components.entity_rows import (  # noqa: F401
    render_skill_row,
    _skill_detail,
    _toggle_detail,
    _safe_tool_name,
)

logger = logging.getLogger(__name__)

_CATEGORY_COLORS: dict[str, str] = {
    "browser":       "#3b82f6",
    "file_ops":      "#f59e0b",
    "devops":        "#10b981",
    "data":          "#8b5cf6",
    "productivity":  "#06b6d4",
    "communication": "#ec4899",
    "code":          "#f97316",
    "system":        "#6b7280",
    "finance":       "#22c55e",
    "general":       "#94a3b8",
}

_PROFICIENCY_COLORS: dict[str, str] = {
    "beginner":     "#22c55e",
    "intermediate": "#f59e0b",
    "expert":       "#ef4444",
}


def build_skills_page() -> None:
    state = AppState.get()
    vault = state.vault

    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 20px;"):
        with ui.column().style("gap: 2px;"):
            ui.label("Skills Registry").style(
                f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
            )
            from systemu.interface.design.glossary import lore_sublabel
            ui.label(lore_sublabel("skills")).classes("s-muted")

    # ── Category filter ───────────────────────────────────────────────────────
    all_skills = vault.load_index("skills")

    categories = sorted({s.get("category", "general") for s in all_skills})
    category_options = {"": "All Categories"} | {c: c.replace("_", " ").title() for c in categories}

    with ui.row().style("gap: 10px; align-items: center; margin-bottom: 16px;"):
        cat_filter = ui.select(
            options=category_options,
            label="Category",
        ).style("min-width: 180px;")
        cat_filter.value = ""

        search_input = ui.input(placeholder="Search skills...").style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 8px; padding: 6px 10px; color: {THEME['text']}; width: 260px;"
        )

    from systemu.interface.components.list_filter import filter_rows

    @ui.refreshable
    def _skills_table():
        skills = vault.load_index("skills")
        # Shared listing filter: search over name/description + a category select
        # (same mechanism as the status select on scrolls/activities/shadows).
        filtered = filter_rows(
            skills, search_input.value or "", cat_filter.value or "all",
            search_keys=("name", "description"), select_key="category",
        )

        if not filtered:
            ui.label("No skills found." if skills else "No skills yet — process a scroll to extract skills.").style(
                f"color: {THEME['text_muted']}; font-style: italic; padding: 20px;"
            )
            return

        ui.label(f"{len(filtered)} skill{'s' if len(filtered) != 1 else ''} found").style(
            f"font-size: 12px; color: {THEME['text_muted']}; margin-bottom: 12px;"
        )

        for s in filtered:
            render_skill_row(s, vault)

    _skills_table()
    cat_filter.on("update:model-value", lambda _: _skills_table.refresh())
    search_input.on("input", lambda _: _skills_table.refresh())


# ─────────────────────────────────────────────────────────────────────────────
#  Skill row — delegates to the shared canonical renderer (entity_rows)
# ─────────────────────────────────────────────────────────────────────────────

def _skill_row(s: dict, vault) -> None:
    """Back-compat shim: the collapsed card + expandable detail now live in the
    ONE shared renderer ``entity_rows.render_skill_row``. Kept importable so any
    legacy caller (and the v0.8.12 import-smoke test) still resolves."""
    render_skill_row(s, vault)
