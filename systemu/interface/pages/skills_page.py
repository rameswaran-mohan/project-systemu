"""NiceGUI Dashboard — Skills page.

Lists all skills in the vault with category, proficiency level, required tools,
and evidence scrolls. Clicking a row expands the full SKILL.md content.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME, status_badge_html

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
        ui.label("🧠 Skills Registry").style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )

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

    @ui.refreshable
    def _skills_table():
        skills = vault.load_index("skills")
        q = search_input.value.lower() if search_input.value else ""
        cat = cat_filter.value or ""

        filtered = [
            s for s in skills
            if (not cat or s.get("category", "") == cat)
            and (not q or q in s.get("name", "").lower() or q in s.get("description", "").lower())
        ]

        if not filtered:
            ui.label("No skills found." if skills else "No skills yet — process a scroll to extract skills.").style(
                f"color: {THEME['text_muted']}; font-style: italic; padding: 20px;"
            )
            return

        ui.label(f"{len(filtered)} skill{'s' if len(filtered) != 1 else ''} found").style(
            f"font-size: 12px; color: {THEME['text_muted']}; margin-bottom: 12px;"
        )

        for s in filtered:
            _skill_row(s, vault)

    _skills_table()
    cat_filter.on("update:model-value", lambda _: _skills_table.refresh())
    search_input.on("input", lambda _: _skills_table.refresh())


# ─────────────────────────────────────────────────────────────────────────────
#  Skill row — collapsed card + expandable detail
# ─────────────────────────────────────────────────────────────────────────────

def _skill_row(s: dict, vault) -> None:
    skill_id   = s.get("id", "")
    name       = s.get("name", skill_id)
    category   = s.get("category", "general")
    desc       = s.get("description", "")
    created_at = s.get("created_at", "")
    evidence   = s.get("evidence_scroll_ids", [])

    cat_color  = _CATEGORY_COLORS.get(category, "#94a3b8")

    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 0; margin-bottom: 10px; width: 100%; overflow: hidden;"
    ):
        # ── Collapsed header row ──────────────────────────────────────────────
        detail_col = ui.column().classes("w-full").style("display: none;")

        with ui.row().classes("w-full items-center").style(
            f"padding: 14px 18px; cursor: pointer; gap: 14px;"
        ).on("click", lambda _, dc=detail_col: _toggle_detail(dc)):

            # Category pill
            ui.html(
                f'<span style="background: color-mix(in srgb, {cat_color} 20%, transparent); '
                f'color: {cat_color}; font-size: 10px; font-weight: 700; '
                f'padding: 3px 10px; border-radius: 12px; letter-spacing: 0.06em; '
                f'white-space: nowrap;">{category.upper()}</span>'
            )

            # Name + description
            with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
                ui.label(name).style(
                    f"font-size: 14px; font-weight: 700; color: {THEME['text']}; line-height: 1.2;"
                )
                if desc:
                    ui.label(desc[:120] + ("…" if len(desc) > 120 else "")).style(
                        f"font-size: 12px; color: {THEME['text_muted']}; line-height: 1.4;"
                    )

            # Evidence count
            if evidence:
                ui.html(
                    f'<span style="font-size: 11px; color: {THEME["text_muted"]}; white-space: nowrap;">'
                    f'📜 {len(evidence)} scroll{"s" if len(evidence) != 1 else ""}</span>'
                )

            # Expand chevron
            ui.label("▾").style(f"color: {THEME['text_muted']}; font-size: 14px;")

        # ── Expanded detail panel ─────────────────────────────────────────────
        with detail_col:
            ui.separator().style(f"border-color: {THEME['border']}; margin: 0;")
            with ui.column().style("padding: 16px 18px; gap: 12px;"):
                _skill_detail(skill_id, vault)


def _toggle_detail(detail_col) -> None:
    current = detail_col.style or ""
    if "display: none" in current:
        detail_col.style("display: flex; flex-direction: column;")
    else:
        detail_col.style("display: none;")


def _skill_detail(skill_id: str, vault) -> None:
    """Render the expanded detail section for a skill."""
    try:
        skill = vault.get_skill(skill_id)
    except KeyError:
        ui.label("Skill not found in vault.").style(f"color: {THEME['danger']}; font-size: 12px;")
        return

    proficiency  = getattr(skill, "proficiency_level", "") or ""
    tool_names   = getattr(skill, "required_tool_names", []) or []
    tool_ids     = getattr(skill, "required_tool_ids", []) or []
    instructions = getattr(skill, "instructions_md", "") or ""
    skill_md_path = getattr(skill, "skill_md_path", "") or ""
    evidence     = getattr(skill, "evidence_scroll_ids", []) or []

    prof_color = _PROFICIENCY_COLORS.get(proficiency, "#94a3b8")

    with ui.row().classes("w-full flex-wrap").style("gap: 24px;"):
        # Left column — metadata
        with ui.column().style("gap: 8px; min-width: 200px;"):
            if proficiency:
                with ui.row().style("gap: 6px; align-items: center;"):
                    ui.label("Proficiency:").style(f"font-size: 12px; color: {THEME['text_muted']}; font-weight: 600;")
                    ui.html(
                        f'<span style="background: color-mix(in srgb, {prof_color} 20%, transparent); '
                        f'color: {prof_color}; font-size: 11px; font-weight: 700; '
                        f'padding: 2px 8px; border-radius: 8px;">{proficiency}</span>'
                    )

            display_tools = tool_names or [vault.get_tool(tid).name for tid in tool_ids[:5] if _safe_tool_name(tid, vault)]
            if display_tools:
                ui.label("Required Tools:").style(f"font-size: 12px; color: {THEME['text_muted']}; font-weight: 600;")
                with ui.row().classes("flex-wrap").style("gap: 4px;"):
                    for t in display_tools:
                        ui.html(
                            f'<span style="background: {THEME["surface2"]}; color: {THEME["text"]}; '
                            f'font-size: 11px; font-family: monospace; '
                            f'padding: 2px 8px; border-radius: 6px; border: 1px solid {THEME["border"]};">'
                            f'{t}</span>'
                        )

            if evidence:
                ui.label("Evidence Scrolls:").style(f"font-size: 12px; color: {THEME['text_muted']}; font-weight: 600;")
                for sid in evidence:
                    ui.label(f"• {sid}").style(f"font-size: 11px; color: {THEME['text_muted']}; font-family: monospace;")

        # Right column — instructions / SKILL.md
        if instructions or skill_md_path:
            with ui.column().style("flex: 1; min-width: 300px; gap: 6px;"):
                ui.label("Instructions:").style(f"font-size: 12px; color: {THEME['text_muted']}; font-weight: 600;")

                # Prefer full SKILL.md over the short instructions_md
                content = instructions
                if skill_md_path and Path(skill_md_path).exists():
                    try:
                        content = Path(skill_md_path).read_text(encoding="utf-8")
                    except OSError:
                        pass

                ui.code(content, language="markdown").style(
                    f"width: 100%; max-height: 260px; overflow: auto; "
                    f"border-radius: 8px; font-size: 11px; background: {THEME['surface2']};"
                )


def _safe_tool_name(tool_id: str, vault) -> str:
    try:
        return vault.get_tool(tool_id).name
    except Exception:
        return ""
