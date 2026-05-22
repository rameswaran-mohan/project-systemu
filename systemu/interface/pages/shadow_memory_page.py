"""NiceGUI Dashboard — Shadow Memory page.

Renders a single shadow's SHADOW_MEMORY.md plus the count of buffered lesson
candidates. Provides a manual consolidation trigger so a user can fold the
buffer into the canonical store on demand instead of waiting for the daily job.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME

logger = logging.getLogger(__name__)


def build_shadow_memory_page(shadow_id: str) -> None:
    state = AppState.get()
    vault = state.vault

    try:
        shadow = vault.get_shadow(shadow_id)
    except KeyError:
        ui.label(f"Shadow {shadow_id!r} not found.").style(
            f"color: {THEME['danger']}; font-style: italic; padding: 20px;"
        )
        return

    md_text, buffer_entries = vault.load_shadow_memory(shadow_id)

    # ── Header ────────────────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 18px;"):
        with ui.column().style("gap: 4px;"):
            ui.label(f"🧠 Memory — {shadow.name}").style(
                f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
            )
            ui.label(shadow.description[:160] + ("…" if len(shadow.description) > 160 else "")).style(
                f"font-size: 13px; color: {THEME['text_muted']};"
            )

        with ui.row().style("gap: 8px;"):
            ui.button(
                "🔄 Consolidate now",
                on_click=lambda _, sid=shadow_id: _trigger_consolidation(sid),
            ).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px; font-size: 13px;"
            )
            ui.button(
                "← Back to Army",
                on_click=lambda _: ui.navigate.to("/army"),
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; font-size: 13px;"
            )

    # ── Buffer status strip ───────────────────────────────────────────────────
    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 10px; padding: 12px 18px; margin-bottom: 16px; width: 100%;"
    ):
        with ui.row().style("gap: 24px; align-items: center;"):
            _stat("📝", str(len(buffer_entries)), "Buffered lessons")
            _stat("📜", str(len(shadow.execution_log)), "Execution log entries")
            _stat("⏱", _last_consolidated_label(md_text), "Last consolidated")

    # ── Buffer preview (collapsible) ──────────────────────────────────────────
    if buffer_entries:
        with ui.expansion("📥 Pending lesson buffer", icon="inbox").style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 10px; margin-bottom: 16px; width: 100%;"
        ):
            for entry in buffer_entries[-30:]:
                cat = entry.get("category", "?")
                lesson = entry.get("lesson", "")
                exec_id = entry.get("exec_id", "")
                with ui.row().style("gap: 10px; padding: 6px 0; align-items: flex-start;"):
                    ui.html(
                        f'<span style="font-size: 10px; font-weight: 700; '
                        f'color: {THEME["info"]}; padding: 2px 8px; '
                        f'background: color-mix(in srgb, {THEME["info"]} 15%, transparent); '
                        f'border-radius: 6px; white-space: nowrap;">{cat.upper()}</span>'
                    )
                    ui.label(lesson).style(
                        f"font-size: 12px; color: {THEME['text']}; flex: 1; line-height: 1.5;"
                    )
                    if exec_id:
                        ui.label(exec_id).style(
                            f"font-size: 10px; color: {THEME['text_muted']}; font-family: monospace;"
                        )

    # ── Canonical MEMORY.md render ────────────────────────────────────────────
    if md_text.strip():
        ui.label("Consolidated Memory (SHADOW_MEMORY.md)").style(
            f"font-size: 11px; font-weight: 700; color: {THEME['text_muted']}; "
            f"text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px;"
        )
        ui.code(md_text, language="markdown").style(
            f"width: 100%; max-height: 540px; overflow: auto; "
            f"border-radius: 10px; font-size: 12px; background: {THEME['surface2']};"
        )
    else:
        ui.label("No memory persisted yet — this shadow has not been consolidated.").style(
            f"color: {THEME['text_muted']}; font-style: italic; padding: 20px;"
        )


def _stat(icon: str, value: str, label: str) -> None:
    with ui.column().style("align-items: flex-start; gap: 1px;"):
        ui.label(f"{icon} {value}").style(
            f"font-size: 14px; font-weight: 700; color: {THEME['text']};"
        )
        ui.label(label).style(f"font-size: 10px; color: {THEME['text_muted']};")


def _last_consolidated_label(md_text: str) -> str:
    import re
    m = re.search(r"^last_consolidated:\s*(.+)$", md_text or "", re.MULTILINE)
    if not m:
        return "never"
    raw = m.group(1).strip()
    return raw.split("T")[0] if "T" in raw else raw


def _trigger_consolidation(shadow_id: str) -> None:
    """Run consolidation for one shadow synchronously and show the result."""
    state = AppState.get()
    vault = state.vault
    config = state.config

    try:
        shadow = vault.get_shadow(shadow_id)
    except KeyError:
        ui.notify("Shadow not found.", type="negative")
        return

    md_text, buffer_entries = vault.load_shadow_memory(shadow_id)
    if not buffer_entries:
        ui.notify("No buffered lessons to consolidate.", type="warning")
        return

    ui.notify(f"Consolidating {len(buffer_entries)} lesson(s)…", type="info")

    try:
        # Reuse the scheduler's consolidation primitive so behaviour stays in sync
        from systemu.scheduler.jobs import _consolidate_one, _graduate_memory_to_skills
        new_md = _consolidate_one(shadow, md_text, buffer_entries, config)
        if not new_md or not new_md.lstrip().startswith("---"):
            ui.notify("Consolidation produced invalid output — buffer left intact.", type="negative")
            return
        vault.save_shadow_memory(shadow_id, new_md)
        vault.clear_memory_buffer(shadow_id)
        try:
            _graduate_memory_to_skills(shadow, new_md, vault)
        except Exception as exc:
            logger.warning("Skill graduation failed: %s", exc)
        ui.notify("Memory consolidated.", type="positive")
        # Force the page to re-render with new state
        ui.navigate.to(f"/memory/{shadow_id}")
    except Exception as exc:
        logger.error("Manual consolidation failed: %s", exc)
        ui.notify(f"Consolidation failed: {exc}", type="negative")
